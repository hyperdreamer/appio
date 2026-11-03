"""The single P3 shell: one privileged switch, navigation everywhere else.

Ownership of privilege is deliberate and narrow. Exactly one checkable control --
``System services (root)`` in Settings -- can change machine-wide collection, and
it only ever *requests* a change through a consent dialog and
``/usr/bin/pkexec /usr/bin/appio-setup <verb>``. Every other affordance in this
window (the status banner, the ``Files`` entry) selects a page, issues one
read-only query, or focuses that one control; none of them changes any state.

The switch renders the *probe*, never the last click: clicking restores the
probed position immediately, and the displayed state is recomputed from
``SetupClient.refresh()`` after every operation. The four honest service states
(``off``, ``on, answering``, ``enabled, not answering``, ``on, needs activation``)
come from :func:`appio.collector_owner.decide_owner`; a helper's accounting
verdict may refine the activation dimension. A verified answer on the GUI's
current history connection is also a socket observation and supersedes a silent
one-shot probe.

The console host and its Overview and Files tabs persist before the first sample
and throughout replacement preparations. Numeric frames are applied only after
:meth:`HistoryRouter.ready` is true for their exact window; a replacement keeps
the previous frame visible until the new frame is ready.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Callable, Literal

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..activation import (
    ACTIVATION_CHOICES,
    CHOICE_REBOOT_LABEL,
    CHOICE_RESTART_LABEL,
    RESTART_CHOICE_WARNING,
    STATE_INACTIVE,
    STATE_NOT_DELEGATED,
)
from ..collector_owner import CollectorCoordinator, GuiLease, ServiceFacts, decide_owner
from ..contracts import RAW_BUCKET_NS, Window, WindowUnavailableError
from ..history_export import ExportResult
from ..history_router import HistoryRouter
from ..iodclient import ClientState
from ..ipc_models import validate_server_message
from ..ipc_protocol import FrameDecoder, MAX_OUTBOUND, ProtocolError, encode_frame
from ..ipc_security import read_peer, require_root_server
from ..live import LiveCollector, SystemClock
from ..setup_boundary import CommandResult
from ..setup_client import SetupClient, SetupReply
from .files_view import STATE_UNAVAILABLE as FILES_STATE_UNAVAILABLE
from .files_view import FilesView

if TYPE_CHECKING:
    from .main_window import AppIoWindow

__all__ = [
    "ACTIVATION_INACTIVE",
    "ACTIVATION_STATES",
    "CHOICE_REBOOT_LABEL",
    "CHOICE_RESTART_LABEL",
    "COLLECTOR_STATE_NEEDS_ACTIVATION",
    "COLLECTOR_STATE_OFF",
    "COLLECTOR_STATE_ON_ANSWERING",
    "COLLECTOR_STATE_SILENT",
    "CollectorWindow",
    "ConsentDialog",
    "FILES_UNAVAILABLE",
    "RESTART_CHOICE_WARNING",
    "SOCKET_PATH",
    "SettingsDialog",
    "consent_text",
    "display_state",
    "parse_unit_facts",
    "production_probe",
    "run_interactive",
    "silence_cause",
]

#: The four honest service states the switch may display. They are the exact
#: strings :func:`appio.collector_owner.decide_owner` produces, so no second
#: vocabulary can drift from the owner decision.
COLLECTOR_STATE_OFF = "off"
COLLECTOR_STATE_ON_ANSWERING = "on, answering"
COLLECTOR_STATE_SILENT = "enabled, not answering"
COLLECTOR_STATE_NEEDS_ACTIVATION = "on, needs activation"
COLLECTOR_STATE_UNAVAILABLE = "unavailable"

#: The activation verdicts a root probe may add. They never claim the daemon is
#: answering: only the GUI's own socket observation may do that.
ACTIVATION_INACTIVE = STATE_INACTIVE
ACTIVATION_STATES = frozenset({STATE_INACTIVE, STATE_NOT_DELEGATED})

FILES_UNAVAILABLE = "file activity is unavailable"

#: The fixed socket. The GUI never builds this from user input.
SOCKET_PATH = Path("/run/appio/iod.sock")
UNIT_SHOW_ARGV = (
    "/usr/bin/systemctl",
    "show",
    "appio-iod.service",
    "--property=UnitFileState,ActiveState,SubState,Result,NRestarts",
    "--no-pager",
)
HELPER_TIMEOUT_SECONDS = 120.0
STATUS_TIMEOUT_SECONDS = 2.0
PENDING_BUDGET_NS: int = 30 * 10**9
MAX_HELPER_OUTPUT = 64 * 1024
CONSENT_COMMAND_PREFIX = "/usr/bin/pkexec /usr/bin/appio-setup"

_ENABLED_STATES: dict[str, bool] = {
    "enabled": True,
    "enabled-runtime": True,
    "linked": True,
    "linked-runtime": True,
    "alias": True,
    "disabled": False,
    "masked": False,
    "masked-runtime": False,
    "static": False,
    "indirect": False,
}
_ACTIVE_STATES: dict[str, str] = {
    "active": "active",
    "inactive": "inactive",
    "failed": "inactive",
    "absent": "absent",
    "not-found": "absent",
}

#: A quiesce request, not a claim about the service: local collection stops and
#: ownership stays unknown until the helper and the probe report something real.
QUIESCE_FACTS = ServiceFacts(None, "unknown", False, False, None)


# --------------------------------------------------------------------------- #
# Consent and question dialogs (plain QDialogs; never exec(), never modal-block)
# --------------------------------------------------------------------------- #


class _TextDialog(QDialog):
    """A dialog whose body text can be asserted without reading pixels."""

    def __init__(self, parent: QWidget | None, title: str, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._text = text
        self._layout = QVBoxLayout(self)
        self._label = QLabel(text)
        self._label.setObjectName(f"{title.lower().replace(' ', '_')}_body")
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)
        self._body = QVBoxLayout()
        self._layout.addLayout(self._body)
        self._buttons = QHBoxLayout()
        self._layout.addLayout(self._buttons)

    def body_text(self) -> str:
        return self._text

    def add_body_widget(self, widget: QWidget) -> None:
        self._body.addWidget(widget)

    def add_button(self, caption: str, object_name: str) -> QPushButton:
        button = QPushButton(caption)
        button.setObjectName(object_name)
        button.setAutoDefault(False)
        self._buttons.addWidget(button)
        return button


class ConsentDialog(_TextDialog):
    """One consent dialog, naming the exact commands before any elevation."""

    def __init__(self, parent: QWidget | None, verb: str) -> None:
        captions = {
            "enable": "Install delegation and enable",
            "disable": "Disable system services",
            "status": "Run the activation check",
        }
        super().__init__(parent, "Consent", consent_text(verb))
        self.verb = verb
        self.accept_button = self.add_button(captions[verb], "consent_accept")
        self.cancel_button = self.add_button("Cancel", "consent_cancel")
        self.accept_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)


class ActivationChoiceDialog(_TextDialog):
    """Restart now or activate after a reboot; the warning stays next to the label."""

    chosen = pyqtSignal(str)

    def __init__(self, parent: QWidget | None) -> None:
        lines = [
            "The IO accounting drop-ins are installed. They only take effect for a user",
            "manager started after installation, and this host keeps your user manager",
            "running with Linger, so logging out and back in changes nothing.",
            "",
            "Choose how to pick them up. Nothing restarts or reboots on its own; the",
            "choice only decides which commands run after enabling succeeds.",
            "",
        ]
        for _key, label, warning in ACTIVATION_CHOICES:
            lines.append(f"- {label}" + (f" — {warning}" if warning else ""))
        super().__init__(parent, "Activation", "\n".join(lines))
        self._radios: dict[str, QRadioButton] = {}
        for key, label, warning in ACTIVATION_CHOICES:
            radio = QRadioButton(label)
            radio.setObjectName(f"activation_choice_{key}")
            radio.setToolTip(warning or "")
            self.add_body_widget(radio)
            if warning:
                warning_label = QLabel(warning)
                warning_label.setObjectName(f"activation_choice_{key}_warning")
                warning_label.setWordWrap(True)
                self.add_body_widget(warning_label)
            self._radios[key] = radio
        self._radios["restart"].setChecked(True)
        self.continue_button = self.add_button("Continue", "activation_continue")
        self.not_now_button = self.add_button("Not now", "activation_cancel")
        self.continue_button.clicked.connect(self._accept_choice)
        self.not_now_button.clicked.connect(self.reject)

    def choose(self, key: str) -> None:
        if key not in self._radios:
            raise ValueError("unknown activation choice")
        self._radios[key].setChecked(True)
        self._accept_choice()

    def selected(self) -> str:
        for key, radio in self._radios.items():
            if radio.isChecked():
                return key
        return "restart"

    def _accept_choice(self) -> None:
        self.chosen.emit(self.selected())
        self.accept()


class RestartConfirmationDialog(_TextDialog):
    """A second, distinct confirmation for the command that ends the session."""

    def __init__(self, parent: QWidget | None) -> None:
        text = (
            f"{CHOICE_RESTART_LABEL} runs "
            "/usr/bin/pkexec /usr/bin/appio-setup restart-user-manager, which restarts "
            f"your user manager as root.\n\n{RESTART_CHOICE_WARNING}\n\n"
            "Everything open in this session closes, including this window. Enablement "
            "has already succeeded, so the service stays enabled after the restart."
        )
        super().__init__(parent, "Restart confirmation", text)
        self.accept_button = self.add_button("Restart user manager now", "restart_accept")
        self.cancel_button = self.add_button("Keep it pending", "restart_cancel")
        self.accept_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)


def consent_text(verb: str) -> str:
    """The exact, verb-specific disclosure shown before any elevation."""

    if verb == "enable":
        return (
            "Turning on System services lets appio measure I/O for every process on this "
            "machine, not only the programs that belong to you. It installs machine-wide "
            "collection with persistent root-owned history: device and per-application "
            "counters are written to a root database and shared with the appio group.\n\n"
            "This changes the system manager's IO accounting and delegation drop-ins and "
            "gives your account the appio group, so you can read the shared history. Group "
            "membership applies to new sessions only. Turning the service off later stops "
            "it and writes a user-readable export of the daemon's history to your home "
            "directory, so the record is never confiscated.\n\n"
            "These exact commands run through polkit, with no path, username or test "
            "argument:\n"
            f"  {CONSENT_COMMAND_PREFIX} install-delegation\n"
            f"  {CONSENT_COMMAND_PREFIX} enable\n\n"
            "After they succeed you choose, in a separate question, whether to restart your "
            "user manager now or activate after a reboot. Nothing restarts or reboots "
            "without that explicit choice."
        )
    if verb == "disable":
        return (
            "Turning off System services stops the machine-wide daemon and disables it at "
            "boot. The delegation drop-ins and the appio group membership stay installed; "
            "only the service is stopped. Before it stops, its history is exported to a "
            "database you own in your home directory, so the record remains readable.\n\n"
            "This exact command runs through polkit, with no path, username or test "
            "argument:\n"
            f"  {CONSENT_COMMAND_PREFIX} disable"
        )
    if verb == "status":
        return (
            "Checking activation runs one accounting probe as root. It writes 16 MiB to a "
            "fixed probe file and measures the device bytes your user manager accounted "
            "for, then records whether IO accounting actually works for your account. It "
            "changes no setting and does not start, stop or restart anything.\n\n"
            "This exact command runs through polkit, with no path, username or test "
            "argument:\n"
            f"  {CONSENT_COMMAND_PREFIX} status"
        )
    raise ValueError("consent is defined only for the fixed verbs enable, disable and status")


def display_state(facts: ServiceFacts, probe_state: str | None = None) -> str:
    """The banner/status text: the owner decision, refined only by a root probe.

    ``probe_state`` may add ``delegation present, accounting inactive`` or
    ``not delegated`` because the GUI cannot observe device accounting itself.
    It may never change whether the daemon answers: that is this process's own
    socket observation, and a root helper being able to connect says nothing
    about whether this session can.
    """

    state = decide_owner(facts).state
    if probe_state is not None and probe_state in ACTIVATION_STATES and facts.enabled is not False:
        return probe_state
    return state


_SOCKET_CAUSES: dict[str, str] = {
    "daemon_absent": "socket: nothing listening at /run/appio/iod.sock",
    "connect_failed": "socket: connect failed",
    "path_unverifiable": "socket: socket path could not be verified",
    "peer_unreadable": "socket: peer credentials unreadable",
    "daemon_not_answering": "socket: connected, no answer within 5 s",
    "connection_lost": "socket: connection lost",
    "transport_lost": "socket: transport closed by the daemon",
}
_PEER_REJECTION_CAUSES = frozenset({
    "appio_group_missing", "peercred_unsupported", "socket_not_connected",
    "peercred_unavailable", "invalid_peercred", "server_uid_nonzero",
    "directory_missing", "directory_metadata_invalid", "directory_not_directory",
    "directory_owner", "directory_group", "directory_mode", "socket_missing",
    "socket_metadata_invalid", "path_symlink", "socket_not_socket", "socket_owner",
    "socket_group", "socket_mode",
})


def silence_cause(facts: ServiceFacts, client_state: ClientState | None) -> str | None:
    """Describe only observed socket or systemd causes for a silent owner state."""

    owner_state = decide_owner(facts).state
    enabled_failure = owner_state == COLLECTOR_STATE_OFF and facts.enabled is True and (
        facts.sub_state == "failed"
        or facts.result is not None and facts.result not in ("success", "none")
    )
    if owner_state != COLLECTOR_STATE_SILENT and not enabled_failure:
        return None

    socket_clause = None
    if client_state is not None:
        last_error = client_state.last_error
        if last_error is None and client_state.verified is True and client_state.answering is False:
            socket_clause = "daemon status reports not answering"
        elif isinstance(last_error, str):
            socket_clause = _SOCKET_CAUSES.get(last_error)
            if socket_clause is None and last_error in _PEER_REJECTION_CAUSES:
                socket_clause = f"socket: {last_error}"

    clauses: list[str] = []
    if enabled_failure:
        if facts.result is not None and facts.result not in ("success", "none"):
            clauses.append(f"enabled but not running: failed: {facts.result}")
        else:
            clauses.append("enabled but not running: failed")
    if owner_state == COLLECTOR_STATE_SILENT and socket_clause is not None:
        clauses.append(socket_clause)
    elif enabled_failure and socket_clause is not None:
        clauses.append(socket_clause)

    if facts.restarts is not None and facts.restarts > 0:
        clauses.append(f"{facts.restarts} restarts")
    if (facts.result is not None and facts.result not in ("success", "none")
            and not enabled_failure):
        clauses.append(f"failed: {facts.result}")
    if facts.sub_state == "auto-restart":
        clauses.append("restarting")

    return "; ".join(clauses) or None


# --------------------------------------------------------------------------- #
# Settings: the one place that owns the switch
# --------------------------------------------------------------------------- #


class SettingsDialog(QDialog):
    """``Settings`` owns the single privileged control and its probed status line."""

    ROWS: tuple[str, ...] = ("system_services",)
    STATUS_CAPTION = "System services (root)"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(False)
        self.focused_row = ""
        layout = QVBoxLayout(self)

        row = QWidget()
        row.setObjectName("system_services_row")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.switch = QCheckBox(self.STATUS_CAPTION)
        self.switch.setObjectName("system_services_switch")
        self.switch.setAccessibleName(self.STATUS_CAPTION)
        self.switch.setToolTip(
            "Probed state of the machine-wide collector. Only this control can change it."
        )
        self.status_label = QLabel(COLLECTOR_STATE_UNAVAILABLE)
        self.status_label.setObjectName("system_services_status")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.recheck = QPushButton("Check activation")
        self.recheck.setObjectName("system_services_recheck")
        self.recheck.setToolTip("Ask root to probe accounting once; no setting changes.")
        row_layout.addWidget(self.switch)
        row_layout.addWidget(self.status_label, 1)
        row_layout.addWidget(self.recheck)
        layout.addWidget(row)
        layout.addStretch(1)

    def focus_row(self, key: str) -> None:
        if key not in self.ROWS:
            raise ValueError("unknown settings row")
        self.focused_row = key
        self.switch.setFocus(Qt.FocusReason.TabFocusReason)

    def show_switch(self, enabled: bool | None, *, busy: bool, unavailable: bool) -> None:
        """Render the probe. Signal blocking keeps a refresh from re-elevating."""

        self.switch.blockSignals(True)
        try:
            self.switch.setChecked(enabled is True)
        finally:
            self.switch.blockSignals(False)
        self.switch.setEnabled(enabled is not None and not busy and not unavailable)


# --------------------------------------------------------------------------- #
# The shell
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbePayload:
    facts: ServiceFacts | None
    error: str | None
    ticket: int
    generation: int

    def __post_init__(self) -> None:
        if (self.facts is None) == (self.error is None):
            raise ValueError("probe payload must contain exactly one outcome")
        if self.facts is not None and not isinstance(self.facts, ServiceFacts):
            raise ValueError("probe payload facts must be ServiceFacts")
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error.strip()
        ):
            raise ValueError("probe payload error must be nonempty text")
        if type(self.ticket) is not int or self.ticket <= 0:
            raise ValueError("probe payload ticket must be a positive integer")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("probe payload generation must be a nonnegative integer")


@dataclass(frozen=True)
class PrepareRetry:
    window: Window
    owner: Literal["clock", "explicit"]
    since: int

    def __post_init__(self) -> None:
        if not isinstance(self.window, Window):
            raise ValueError("prepare retry window must be a Window")
        if self.owner not in ("clock", "explicit"):
            raise ValueError("prepare retry owner must be clock or explicit")
        if type(self.since) is not int or self.since < 0:
            raise ValueError("prepare retry timestamp must be a nonnegative integer")


class _ReplyBridge(QObject):
    """Carries a completed helper future from a worker thread to the Qt thread."""

    delivered = pyqtSignal(object)


class _ProbeBridge(QObject):
    """Carries immutable probe outcomes to the Qt thread."""

    delivered = pyqtSignal(object)


class CollectorWindow(QMainWindow):
    """The shell owns applied history frames and all Qt-side service decisions."""

    def __init__(
        self,
        coordinator: CollectorCoordinator,
        history: HistoryRouter,
        setup: SetupClient,
        *,
        clock_ns: Callable[[], int] | None = None,
        pump: Callable[[], None] | None = None,
        refresh_interval_ms: int = 1000,
    ) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.history = history
        self.setup = setup
        self._clock_ns = clock_ns or (lambda: time.clock_gettime_ns(time.CLOCK_BOOTTIME))
        self._pump = pump
        self._facts: ServiceFacts | None = None
        self._facts_applied = 0
        self._dropped_facts = 0
        self._probe_state: str | None = None
        self._probe_ticket = 0
        self._setup_generation = 0
        self._probe_inflight: Future[ServiceFacts] | None = None
        self._probe_successor: Future[ServiceFacts] | None = None
        self._probe_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="appio-probe")
        self._busy = False
        self._operation: tuple[str, Callable[[SetupReply], None] | None] | None = None
        self._activation_choice: str | None = None
        self._unsupported_reason: str | None = None
        self._target: Window | None = None
        self._displayed: Window | None = None
        self._pending: Window | None = None
        self._pending_since: int | None = None
        self._stalled = False
        self._retry_after: int | None = None
        self._deferred_target: Window | None = None
        self._refresh_after_apply = False
        self._invalidated = False
        self._prepare_retry: PrepareRetry | None = None
        self._prepare_error: str | None = None
        self._closed = False
        self.state_text = COLLECTOR_STATE_UNAVAILABLE

        self.consent: ConsentDialog | None = None
        self.choice: ActivationChoiceDialog | None = None
        self.restart_confirmation: RestartConfirmationDialog | None = None

        self.setWindowTitle("appio")
        self._build_ui()

        self._bridge = _ReplyBridge(self)
        # The packaged PyQt stub omits the supported connection-type argument.
        self._bridge.delivered.connect(  # type: ignore[call-arg]
            self._on_reply, Qt.ConnectionType.QueuedConnection
        )
        self._probe_bridge = _ProbeBridge(self)
        self._probe_bridge.delivered.connect(  # type: ignore[call-arg]
            self._apply_facts, Qt.ConnectionType.QueuedConnection
        )

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_history)
        self._timer = QTimer(self)
        self._timer.setInterval(max(0, int(refresh_interval_ms)))
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # -- construction -----------------------------------------------------

    def _build_ui(self) -> None:
        from .main_window import AppIoWindow

        central = QWidget(self)
        outer = QVBoxLayout(central)

        banner = QWidget()
        banner.setObjectName("status_banner")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(0, 0, 0, 0)
        status_column = QWidget(banner)
        status_layout = QVBoxLayout(status_column)
        status_layout.setContentsMargins(0, 0, 0, 0)
        self.banner_status = QLabel(COLLECTOR_STATE_UNAVAILABLE)
        self.banner_status.setObjectName("banner_status")
        self.banner_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.banner_detail = QLabel("")
        self.banner_detail.setObjectName("banner_detail")
        self.banner_detail.setWordWrap(True)
        self.banner_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.banner_detail.setVisible(False)
        status_layout.addWidget(self.banner_status)
        status_layout.addWidget(self.banner_detail)
        self.banner_open_settings = QPushButton("Open settings")
        self.banner_open_settings.setObjectName("banner_open_settings")
        self.banner_open_settings.setToolTip("Opens Settings at the System services switch.")
        banner_layout.addWidget(status_column, 1)
        banner_layout.addWidget(self.banner_open_settings)
        outer.addWidget(banner)

        self.error_label = QLabel("")
        self.error_label.setObjectName("setup_error")
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_label.setVisible(False)
        outer.addWidget(self.error_label)

        self._view = AppIoWindow(self.history, files_view=FilesView(self.history))
        self._view.setObjectName("console_view")
        self.files_view = self._view.files_view
        self.files_message = self._view.files_message
        self.files_open_settings = self._view.files_open_settings
        self.history_state = self._view.history_state
        self.files_view.state_changed.connect(self._render_files_message)
        # The persistent console can be taller than a small desktop. Preserve
        # each panel's useful minimum size, but scroll instead of growing the
        # top-level shell beyond its requested 620-pixel height.
        console_scroll = QScrollArea(central)
        console_scroll.setObjectName("console_scroll")
        console_scroll.setWidgetResizable(True)
        console_scroll.setWidget(self._view)
        outer.addWidget(console_scroll, 1)

        self.setCentralWidget(central)
        self.resize(980, 620)

        self.settings = SettingsDialog(self)
        self.settings.recheck.clicked.connect(self._on_recheck_clicked)
        self.settings.switch.clicked.connect(self._on_switch_clicked)
        self.banner_open_settings.clicked.connect(self._open_settings_default)
        self.files_open_settings.clicked.connect(self._open_settings_default)
        self._render_files_message()

    def _open_settings_default(self) -> None:
        self.open_settings()

    # -- public navigation surface ----------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def facts_applied(self) -> int:
        return self._facts_applied

    @property
    def _prepared(self) -> Window | None:
        return self._displayed

    def open_settings(self, *, focus: str = "system_services") -> None:
        if focus not in SettingsDialog.ROWS:
            raise ValueError("unknown settings row")
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()
        self.settings.focus_row(focus)

    def open_files_unavailable(self) -> None:
        """Compatibility navigation: select Files without issuing a query."""

        self._view.detail_tabs.setCurrentIndex(1)
        self._render_files_message()

    # -- Files ownership --------------------------------------------------

    def _history_ready(self, window: Window | None) -> bool:
        if window is None:
            return False
        try:
            return bool(self.history.ready(window))
        except Exception:
            return False

    def _files_status_text(self) -> str | None:
        window = self._displayed or self._pending or self._target
        if window is None:
            return "waiting for the first sample"
        if self._displayed is None:
            try:
                status = self.history.file_source_status()
            except Exception as error:
                return f"{FILES_UNAVAILABLE}: {str(error) or type(error).__name__}"
            if not getattr(status, "available", False):
                reason = getattr(status, "reason", None)
                detail = getattr(status, "diagnostic", None) or "the file source is unavailable"
                prefix = f"{reason}: " if reason else ""
                return f"{FILES_UNAVAILABLE}: {prefix}{detail}"
            return f"loading history ({self.history.view_state})"
        if self._pending is not None or self._prepare_retry is not None:
            return None
        if not self._history_ready(self._displayed):
            return None
        if self.files_view.state == FILES_STATE_UNAVAILABLE:
            return f"{FILES_UNAVAILABLE}: {self.files_view.unavailable_text()}"
        return None

    def _render_files_message(self, *_args) -> None:
        try:
            text = self._files_status_text()
        except Exception as error:
            text = f"appio: {error}"
        self.files_message.setText(text or "")
        self.files_message.setVisible(text is not None)
        # A caption does not make an idle table unavailable: before the first
        # applied frame there is no numeric Files surface to show. Pending
        # replacement windows retain the previously displayed table.
        self.files_view.setVisible(self._displayed is not None)

    def _sync_files(self, *, request: bool = False) -> None:
        window = self._displayed
        if (window is None or self._pending is not None or self._prepare_retry is not None
                or not self._history_ready(window)):
            self._render_files_message()
            return
        previous = self.files_view._window
        self.files_view.set_window(window)
        if request and previous == window:
            self.files_view.request_refresh()
        self._render_files_message()

    # -- timer and target state machine -----------------------------------

    def refresh(self) -> None:
        """Pump, advance/apply stored history work, then submit one probe."""

        if self._closed:
            return
        try:
            if self._pump is not None:
                with self.files_view.transient_scope():
                    self._pump()
            self._tick_target()
            applied = self.apply_ready_frame()
            if (
                not applied
                and self._pending is None
                and self._prepare_retry is None
                and self._displayed is not None
                and self._history_ready(self._displayed)
            ):
                self.files_view.flush_refresh()
            if not self._busy:
                self._submit_probe()
        except WindowUnavailableError as error:
            self._show_error(str(error) or type(error).__name__)
        except Exception as error:
            self._show_error(f"appio: {str(error) or type(error).__name__}")

    def _history_retired(self) -> bool:
        return str(getattr(self.history, "view_state", "")).startswith("unavailable")

    def _tick_target(self, *, now: int | None = None) -> None:
        if self._closed:
            return
        now = self._clock_ns() if now is None else now
        self._target = self._current_window(now)
        retry = self._prepare_retry

        if self._pending is not None:
            if self._history_retired():
                self._retire_pending(now)
                return
            if (
                self._pending_since is not None
                and now - self._pending_since >= PENDING_BUDGET_NS
            ):
                self._stalled = True
            self._invalidated = False
            self._show_pending_status(now)
            self._arm_poll()
            return

        if retry is not None and retry.owner == "explicit":
            self.files_view.set_updating_scope(retry.window)
            self.files_view.set_scope_ready(False)
            self._invalidated = self._history_retired() and self._displayed is not None
            self._show_pending_status(now)
            self._render_files_message()
            return

        wanted = self._deferred_target or self._target
        self._invalidated = self._history_retired() and self._displayed is not None
        if wanted is None:
            if retry is not None:
                self._prepare_retry = None
                self._prepare_error = None
                self._clear_error()
            self._displayed = None
            self._pending_since = None
            self._invalidated = False
            self.files_view.set_updating_scope(None)
            self.files_view.set_scope_ready(True)
            self.history_state.setText("waiting for the first sample")
            self.history_state.setVisible(True)
            self._view.show_overview_status("waiting for the first sample", keep_frame=False)
            self.files_view.set_stalled(False, "")
            self._poll_timer.stop()
            self._sync_files()
            return

        retrying_clock = retry is not None and retry.owner == "clock"
        needs_prepare = (
            self._displayed is None
            or self._invalidated
            or retrying_clock
            or self._deferred_target is not None
            or self._stalled
            or self._retry_after is not None
            or wanted != self._displayed
        )
        if not needs_prepare:
            self.files_view.set_scope_ready(True)
            self.files_view.set_updating_scope(None)
            if self._history_ready(self._displayed):
                self.files_view.flush_refresh()
            self._render_files_message()
            self.history_state.setText("")
            self.history_state.setVisible(False)
            self.files_view.set_stalled(False, "")
            self._poll_timer.stop()
            return

        self.files_view.set_updating_scope(wanted)
        self.files_view.set_scope_ready(False)
        self.files_view.request_refresh()
        if not self._submit_prepare(wanted, "clock", now=now):
            self._show_pending_status(now)
            self._poll_timer.stop()

    def _retire_pending(self, now: int) -> None:
        if self._pending is None:
            return
        wanted = self._deferred_target or self._target
        if wanted is not None:
            self._deferred_target = wanted
        self._pending = None
        self._pending_since = None
        self._retry_after = now + PENDING_BUDGET_NS
        self._invalidated = self._displayed is not None
        self._poll_timer.stop()
        self._show_pending_status(now)

    def _show_pending_status(self, now: int | None = None) -> None:
        now = self._clock_ns() if now is None else now
        view_state = str(self.history.view_state)
        if self._displayed is None:
            text = f"loading history ({view_state})"
        else:
            text = f"updating ({view_state})"

        stall_eligible = self._stalled or self._retry_after is not None
        local_read_in_flight = getattr(self.history, "local_read_in_flight", False)
        if stall_eligible and local_read_in_flight is True:
            text = (
                f"history read has not completed ({view_state}); "
                "the reader is still busy"
            )
        elif self._retry_after is not None and now < self._retry_after:
            remaining = self._retry_after - now
            seconds = (remaining + 1_000_000_000 - 1) // 1_000_000_000
            text = (
                f"history read has not completed ({view_state}); "
                f"retrying in {seconds} s"
            )

        self.history_state.setText(text)
        self.history_state.setVisible(True)
        if stall_eligible and text.startswith("history read has not completed"):
            self.files_view.set_stalled(True, text)
        else:
            self.files_view.set_stalled(False, "")
        self._render_files_message()

    def _arm_poll(self) -> None:
        if self._pending is not None and not self._poll_timer.isActive():
            self._poll_timer.start()

    def _history_source_key_ready(self) -> bool:
        coordinator = self.coordinator
        if not isinstance(coordinator, CollectorCoordinator):
            return True
        facts = coordinator._facts
        if facts is None:
            return False
        client_state = getattr(getattr(self.history, "client", None), "state", None)
        if client_state is not None and (
            facts.answering != client_state.answering
            or (facts.daemon_verified and not client_state.verified)
        ):
            return False
        if client_state is not None and client_state.verified and client_state.answering:
            facts = replace(
                facts,
                daemon_verified=True,
                answering=True,
                pending_activation=client_state.pending_activation,
            )
            decision = decide_owner(facts)
        else:
            decision = coordinator._decision
        return coordinator._history_key == (
            decision.owner,
            decision.may_collect_local,
            facts.daemon_verified,
            facts.answering,
        )

    def _submit_prepare(
        self, wanted: Window, origin: Literal["clock", "explicit"], *, now: int | None = None
    ) -> bool:
        if self._closed:
            return False
        now = self._clock_ns() if now is None else now
        if (
            self._pending is not None
            or not self._history_source_key_ready()
            or getattr(self.history, "local_read_in_flight", False) is not False
            or (self._retry_after is not None and now < self._retry_after)
            or wanted is None
        ):
            return False

        retry_error = self._prepare_error
        self._prepare_retry = None
        self._prepare_error = None
        try:
            with self.files_view.transient_scope(retire_on_exit=True):
                self.history.prepare(wanted)
        except WindowUnavailableError as error:
            self._pending = None
            self._pending_since = None
            self._prepare_retry = None
            self._prepare_error = None
            self._show_error(str(error) or type(error).__name__)
            self._show_pending_status(now)
            self._poll_timer.stop()
            self._render_files_message()
            raise
        except Exception as error:
            self._pending = None
            self._pending_since = None
            self._prepare_retry = PrepareRetry(wanted, origin, now)
            self._prepare_error = f"appio: {str(error) or type(error).__name__}"
            self._show_error(self._prepare_error)
            self._show_pending_status(now)
            self._poll_timer.stop()
            self._render_files_message()
            if origin == "explicit":
                raise
            return False

        self._pending = wanted
        self._pending_since = now
        self._stalled = False
        self._retry_after = None
        self._prepare_retry = None
        self._prepare_error = None
        if retry_error is not None and self.error_label.text() == retry_error:
            self._clear_error()
        self._show_pending_status(now)
        self._arm_poll()
        return True

    def apply_ready_frame(self, *, now: int | None = None) -> bool:
        if self._closed:
            return False
        if self._prepare_retry is not None and self._prepare_retry.owner == "explicit":
            return False
        now = self._clock_ns() if now is None else now
        if self._pending is None and self._deferred_target is not None:
            self._tick_target(now=now)
            if self._pending is None:
                return False
        if self._pending is None:
            return False
        if self._history_retired():
            self._retire_pending(now)
            return False
        if self._pending_since is not None and now - self._pending_since >= PENDING_BUDGET_NS:
            self._stalled = True
        if self.history.ready(self._pending):
            self._apply_frame()
            return True
        self._show_pending_status(now)
        return False

    def show_window(self, window: Window) -> None:
        if self._closed:
            return
        now = self._clock_ns()
        retry = self._prepare_retry
        self._deferred_target = window
        if retry is not None and (retry.owner != "explicit" or retry.window != window):
            self._prepare_retry = None
            self._prepare_error = None
            if self.error_label.text().startswith("appio: "):
                self._clear_error()

        self.files_view.set_updating_scope(window)
        self.files_view.set_scope_ready(False)
        self.files_view.request_refresh()
        accepted = self._submit_prepare(window, "explicit", now=now)
        if not accepted:
            self._show_pending_status(now)
            self._render_files_message()

    def _apply_frame(self) -> None:
        window = self._pending
        if window is None:
            return
        self._displayed = window
        self._pending = None
        self._pending_since = None
        self._stalled = False
        self._retry_after = None
        if self._deferred_target == window:
            self._deferred_target = None
        self._invalidated = False
        self.files_view.retire_deliveries()
        self.files_view.set_scope_ready(True)
        self._show_ready(window)
        self.files_view.flush_refresh()
        self.files_view.set_updating_scope(None)
        self._render_files_message()
        self.files_view.set_stalled(False, "")
        if self._refresh_after_apply:
            with self.files_view.transient_scope():
                self._refresh_after_apply = False
                self.history.invalidate()
        self._poll_timer.stop()

    def _poll_history(self) -> None:
        if self._closed or self._pending is None:
            self._poll_timer.stop()
            return
        now = self._clock_ns()
        try:
            applied = self.apply_ready_frame(now=now)
            if not applied and self._pending is None:
                self._poll_timer.stop()
        except WindowUnavailableError as error:
            self._show_error(str(error) or type(error).__name__)
        except Exception as error:
            self._show_error(f"appio: {str(error) or type(error).__name__}")

    def _show_ready(self, window: Window) -> None:
        view = self._ensure_view()
        view.set_window(window)
        self.history_state.setText("")
        self.history_state.setVisible(False)
        self._sync_files(request=False)

    def _ensure_view(self, window: Window | None = None) -> "AppIoWindow":
        del window
        if self._view is None:
            from .main_window import AppIoWindow

            self._view = AppIoWindow(self.history, files_view=self.files_view)
        return self._view

    def _hide_view(self) -> None:
        if self._displayed is None:
            self._view.show_overview_status("waiting for the first sample", keep_frame=False)

    @property
    def view(self) -> "AppIoWindow | None":
        return self._view

    @property
    def history_visible(self) -> bool:
        return self._displayed is not None

    # -- rendering --------------------------------------------------------

    def _render(self, facts: ServiceFacts) -> None:
        state = display_state(facts, self._probe_state)
        self.state_text = state
        self.banner_status.setText(state)
        client_state = getattr(getattr(self.history, "client", None), "state", None)
        cause = silence_cause(facts, client_state)
        self.banner_detail.setText("" if cause is None else cause)
        self.banner_detail.setVisible(cause is not None)
        self.settings.status_label.setText(state)
        self.settings.show_switch(
            facts.enabled,
            busy=self._busy,
            unavailable=self._unsupported_reason is not None,
        )

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def _clear_error(self) -> None:
        self.error_label.setText("")
        self.error_label.setVisible(False)

    def _current_window(self, now_ns: int | None = None) -> Window | None:
        client = getattr(self.history, "client", None)
        state = getattr(client, "state", None)
        if state is not None and state.verified and state.answering:
            cursor = state.cursor
            if cursor is None:
                return None
            boot_id = cursor.boot_id
        else:
            connection = getattr(getattr(self.history, "local", None), "connection", None)
            if connection is None:
                return None
            row = connection.execute(
                "SELECT boot_id FROM boot ORDER BY boot_order DESC LIMIT 1"
            ).fetchone()
            if row is None or not row[0]:
                return None
            boot_id = str(row[0])
        now = self._clock_ns() if now_ns is None else now_ns
        end = (now // RAW_BUCKET_NS + 1) * RAW_BUCKET_NS
        return Window(kind="since_boot", boot_id=boot_id, end_mono_ns=end)

    # -- probe worker and acceptance -------------------------------------

    def _submit_probe(self, *, replace_pending: bool = False) -> bool:
        if self._closed or self._busy:
            return False
        previous = self._probe_inflight
        if previous is not None and not replace_pending:
            return False
        previous_running = previous is not None and previous.running()
        has_running_predecessor = previous_running or (
            previous is not None and self._probe_successor is previous
        )
        self._probe_ticket += 1
        ticket = self._probe_ticket
        generation = self._setup_generation
        if previous is not None and not previous.done() and not previous_running:
            previous.cancel()
        self._probe_successor = None
        try:
            future: Future[ServiceFacts] = self._probe_executor.submit(self.setup.refresh)
        except Exception as error:
            self._probe_inflight = None
            self._probe_successor = None
            self._show_error(f"appio: {str(error) or type(error).__name__}")
            return False
        self._probe_inflight = future
        self._probe_successor = future if has_running_predecessor else None
        def completed(done: Future[ServiceFacts]) -> None:
            self._probe_completed(ticket, generation, done)

        future.add_done_callback(completed)
        return True

    def _probe_completed(
        self, ticket: int, generation: int, future: Future[ServiceFacts]
    ) -> None:
        if future.cancelled():
            return
        try:
            facts = future.result()
            payload = ProbePayload(facts, None, ticket, generation)
        except BaseException as error:
            payload = ProbePayload(
                None, str(error).strip() or type(error).__name__, ticket, generation
            )
        try:
            self._probe_bridge.delivered.emit(payload)
        except RuntimeError:
            if not self._closed:
                raise

    def _apply_facts(self, payload: ProbePayload) -> None:
        if self._closed:
            return
        if (
            payload.ticket != self._probe_ticket
            or payload.generation != self._setup_generation
            or self._busy
        ):
            self._dropped_facts += 1
            if payload.ticket == self._probe_ticket:
                self._probe_inflight = None
                self._probe_successor = None
            return
        if payload.ticket == self._probe_ticket:
            self._probe_inflight = None
            self._probe_successor = None
        if payload.error is not None:
            self._show_error(f"appio: {payload.error}")
            return
        facts = payload.facts
        assert facts is not None
        client = getattr(self.history, "client", None)
        state = getattr(client, "state", None)
        if state is not None and state.verified and state.answering:
            facts = replace(
                facts, daemon_verified=True, answering=True,
                pending_activation=state.pending_activation,
            )
        keep_pending = facts == self._facts and self._pending is not None
        try:
            with self.files_view.transient_scope():
                if not keep_pending:
                    self.coordinator.reconcile(facts)
                committed = self.coordinator.tick_local()
                if committed:
                    if self._pending is None:
                        self.history.invalidate()
                    elif not self._history_retired():
                        self._refresh_after_apply = True
        except Exception as error:
            self._show_error(f"appio: {str(error) or type(error).__name__}")
            return
        self._facts = facts
        self._facts_applied += 1
        self._render(facts)
        try:
            self._tick_target()
            applied = self.apply_ready_frame()
            if (
                not applied
                and self._pending is None
                and self._prepare_retry is None
                and self._displayed is not None
                and self._history_ready(self._displayed)
            ):
                self.files_view.flush_refresh()
        except WindowUnavailableError as error:
            self._show_error(str(error) or type(error).__name__)
        except Exception as error:
            self._show_error(f"appio: {str(error) or type(error).__name__}")

    # -- the switch: probe first, consent, then one operation --------------

    def _on_switch_clicked(self) -> None:
        if self._facts is not None:
            self._render(self._facts)
        if self._busy:
            return
        wanted = "disable" if self._facts is not None and self._facts.enabled is True else "enable"
        self._open_consent(wanted)

    def _on_recheck_clicked(self) -> None:
        if self._busy or self._unsupported_reason is not None:
            return
        self._open_consent("status")

    def _open_consent(self, verb: str) -> None:
        if self._busy:
            return
        self.consent = ConsentDialog(self, verb)
        self.consent.accepted.connect(lambda: self._begin(verb))
        self.consent.show()
        self.consent.raise_()
        self.consent.activateWindow()

    def _begin(self, verb: str) -> None:
        if self._busy:
            return
        self._clear_error()
        if verb == "enable":
            if not self._quiesce_local():
                return
            self._start("install-delegation", self._after_install)
        elif verb == "disable":
            self._start("disable", self._after_disable)
        else:
            self._start("status", self._after_status)

    def _quiesce_local(self) -> bool:
        try:
            with self.files_view.transient_scope():
                self.coordinator.reconcile(QUIESCE_FACTS)
        except Exception as error:
            self._show_error(f"appio: {str(error) or type(error).__name__}")
            return False
        return True

    def _start(self, verb: str, on_success: Callable[[SetupReply], None] | None) -> None:
        if self._closed or self._busy:
            return
        self._setup_generation += 1
        self._busy = True
        self._operation = (verb, on_success)
        if self._facts is not None:
            self._render(self._facts)
        try:
            future = self.setup.invoke(verb)
        except BaseException as error:
            self._complete_start_failure(error)
            return
        future.add_done_callback(self._bridge.delivered.emit)

    def _complete_start_failure(self, error: BaseException) -> None:
        self._setup_generation += 1
        self._operation = None
        self._busy = False
        self._show_error(f"appio: {str(error) or type(error).__name__}")
        if self._facts is not None:
            self._render(self._facts)
        if not self._closed:
            self._submit_probe(replace_pending=True)

    def _on_reply(self, future: Future[SetupReply]) -> None:
        if self._closed:
            return
        operation, self._operation = self._operation, None
        self._setup_generation += 1
        self._busy = False
        _verb, on_success = operation if operation is not None else ("", None)
        try:
            reply = future.result()
        except Exception as error:
            self._fail(str(error))
        else:
            if self._apply_body(reply.body):
                self._clear_error()
            if reply.exit_code != 0:
                self._fail(reply)
            else:
                self._clear_error()
                try:
                    if on_success is not None:
                        on_success(reply)
                except Exception as error:
                    self._show_error(f"appio: {str(error) or type(error).__name__}")
                self._render(self._facts if self._facts is not None else QUIESCE_FACTS)
        if not self._closed and not self._busy:
            self._submit_probe(replace_pending=True)

    def _apply_body(self, body: dict | None) -> bool:
        if body is None:
            return False
        state = body.get("state")
        if state in ACTIVATION_STATES:
            self._probe_state = state
            return True
        if body.get("enabled") is False:
            self._probe_state = None
        return False

    def _fail(self, failure: SetupReply | str) -> None:
        self._activation_choice = None
        if isinstance(failure, SetupReply):
            reply = failure
            body = reply.body if isinstance(reply.body, dict) else {}
            detail = reply.stderr_first_line or str(body.get("error") or "") or "no detail"
            self._show_error(f"Appio setup failed (exit {reply.exit_code}): {detail}")
            reason = body.get("error")
            if isinstance(reason, str) and reason.startswith("unsupported:"):
                self._unsupported_reason = reason
            if reply.exit_code == 127:
                self._unsupported_reason = "appio-setup is not available"
        else:
            self._show_error(f"appio: {failure}")
        self._render(self._facts if self._facts is not None else QUIESCE_FACTS)

    # -- enable, choice and restart ---------------------------------------

    def _after_install(self, reply: SetupReply) -> None:
        self.choice = ActivationChoiceDialog(self)
        self.choice.chosen.connect(self._on_choice)
        self.choice.rejected.connect(self._on_choice_cancelled)
        self.choice.show()
        self.choice.raise_()
        self.choice.activateWindow()

    def _on_choice(self, key: str) -> None:
        self._activation_choice = key
        self._start("enable", self._after_enable)

    def _on_choice_cancelled(self) -> None:
        self._activation_choice = None
        self.refresh()

    def _after_enable(self, reply: SetupReply) -> None:
        if self._activation_choice == "restart":
            self.restart_confirmation = RestartConfirmationDialog(self)
            self.restart_confirmation.accepted.connect(self._restart_confirmed)
            self.restart_confirmation.show()
            self.restart_confirmation.raise_()
            self.restart_confirmation.activateWindow()

    def _restart_confirmed(self) -> None:
        self._start("restart-user-manager", None)

    # -- disable and export import ---------------------------------------

    def _after_disable(self, reply: SetupReply) -> None:
        body = reply.body or {}
        exported = self._export_from_body(body)
        if exported is None:
            self._show_error(
                "appio: the helper reported no verified export metadata; nothing was imported"
            )
        else:
            try:
                with self.files_view.transient_scope():
                    self.coordinator.accept_export(exported)
            except Exception as error:
                self._show_error(f"appio: export import refused: {error}")

    @staticmethod
    def _export_from_body(body: dict) -> ExportResult | None:
        path = body.get("export_path")
        cutoff = body.get("export_cutoff")
        if not isinstance(path, str) or not path:
            return None
        if not isinstance(cutoff, dict):
            return None
        boot_id = cutoff.get("boot_id")
        epoch = cutoff.get("iod_epoch")
        seq = cutoff.get("seq")
        cutoff_ns = cutoff.get("cutoff_mono_ns")
        if not isinstance(boot_id, str) or not boot_id:
            return None
        for value in (epoch, seq, cutoff_ns):
            if type(value) is not int or value < 0:
                return None
        return ExportResult(path=Path(path), boot_id=boot_id, iod_epoch=epoch, seq=seq,
                            cutoff_mono_ns=cutoff_ns)

    # -- explicit status recheck ------------------------------------------

    def _after_status(self, reply: SetupReply) -> None:
        return None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for timer in (self._timer, self._poll_timer):
            timer.stop()
        for dialog in (self.consent, self.choice, self.restart_confirmation, self.settings):
            if dialog is not None:
                dialog.close()
        self.files_view.retire_deliveries()
        self.files_view.close()
        self._probe_executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.coordinator.close()
        finally:
            self.history.close()


# --------------------------------------------------------------------------- #
# Unprivileged production probe (systemd unit state + socket status)
# --------------------------------------------------------------------------- #


def parse_unit_facts(
    stdout: str,
) -> tuple[bool | None, str, str | None, str | None, int | None]:
    """Map required service fields and optional observed failure details."""

    if not isinstance(stdout, str):
        return None, "unknown", None, None, None
    rows: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue  # systemctl may end its output with a blank line
        key, separator, value = line.partition("=")
        if not separator or key in rows:
            return None, "unknown", None, None, None
        rows[key] = value.strip()
    required = {"UnitFileState", "ActiveState"}
    allowed = required | {"SubState", "Result", "NRestarts"}
    if not required.issubset(rows) or not set(rows).issubset(allowed):
        return None, "unknown", None, None, None
    raw_restarts = rows.get("NRestarts")
    restarts = (
        int(raw_restarts)
        if raw_restarts is not None and raw_restarts.isascii() and raw_restarts.isdecimal()
        else None
    )
    return (
        _ENABLED_STATES.get(rows["UnitFileState"]),
        _ACTIVE_STATES.get(rows["ActiveState"], "unknown"),
        rows.get("SubState") or None,
        rows.get("Result") or None,
        restarts,
    )


def _run_systemctl_show(argv: tuple[str, ...]) -> CommandResult:
    completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=5.0)
    return CommandResult(completed.returncode, completed.stdout[:MAX_HELPER_OUTPUT],
                         completed.stderr[:MAX_HELPER_OUTPUT])


def _read_daemon_status(*, timeout_seconds: float = STATUS_TIMEOUT_SECONDS) -> dict | None:
    """Observe the root daemon from *this* session. No root, no queue, no writes."""

    import socket

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    request_id = "gui-status"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.001, deadline - time.monotonic()))
        client.connect(str(SOCKET_PATH))
        require_root_server(read_peer(client))
        client.sendall(encode_frame({"protocol": 1, "type": "status", "request_id": request_id}))
        decoder = FrameDecoder(max_bytes=MAX_OUTBOUND)
        while time.monotonic() < deadline:
            client.settimeout(max(0.001, deadline - time.monotonic()))
            data = client.recv(65536)
            if not data:
                return None
            for message in decoder.feed(data):
                value = validate_server_message(message)
                if value["type"] != "status" or value.get("request_id") != request_id:
                    return None
                return value
    return None


def production_probe(
    *,
    command: Callable[[tuple[str, ...]], CommandResult] | None = None,
    status_reader: Callable[[], dict | None] | None = None,
) -> ServiceFacts:
    """The periodic probe: unprivileged, bounded, and honest about ignorance."""

    run = command or _run_systemctl_show
    read_status = status_reader or _read_daemon_status
    enabled: bool | None = None
    active = "unknown"
    sub_state = result = None
    restarts = None
    try:
        command_result = run(UNIT_SHOW_ARGV)
    except (OSError, subprocess.SubprocessError):
        command_result = None
    if isinstance(command_result, CommandResult) and command_result.returncode == 0:
        enabled, active, sub_state, result, restarts = parse_unit_facts(command_result.stdout)
    status: object = None
    try:
        status = read_status()
    except (OSError, ValueError, ProtocolError):
        status = None
    if isinstance(status, dict) and status.get("answering") is True:
        pending = status.get("pending_activation")
        return ServiceFacts(enabled, active, True, True,
                            pending if type(pending) is bool else None,
                            sub_state, result, restarts)
    return ServiceFacts(enabled, active, False, False, None, sub_state, result, restarts)


# --------------------------------------------------------------------------- #
# Interactive entry (the only place that composes the P3 objects)
# --------------------------------------------------------------------------- #


def _launch_helper(argv: tuple[str, ...]) -> Future:
    """Run one fixed pkexec vector in a worker, bounded, never through a shell."""

    reply: Future = Future()

    def work() -> None:
        try:
            completed = subprocess.run(list(argv), capture_output=True, text=True,
                                       timeout=HELPER_TIMEOUT_SECONDS)
            reply.set_result(CommandResult(
                completed.returncode,
                (completed.stdout or "")[:MAX_HELPER_OUTPUT],
                (completed.stderr or "")[:MAX_HELPER_OUTPUT],
            ))
        except BaseException as error:
            reply.set_exception(error)

    threading.Thread(target=work, name="appio-setup", daemon=True).start()
    return reply


def _wake_object() -> QObject:
    class _Wake(QObject):
        woke = pyqtSignal()

    return _Wake()


def _gui_lease_path() -> Path:
    """The per-user runtime directory; a private subdirectory holds the lease."""

    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    directory = Path(runtime) / "appio"
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass
    return directory / "collector.lock"


def run_interactive(args) -> int:
    """Compose the P3 window and run Qt. Only this path imports P3 modules."""

    from PyQt6.QtWidgets import QApplication

    from ..iodclient import AppRegistry, IodClient
    from ..query import QueryService
    from ..store import AppIoStore

    application = QApplication.instance() or QApplication([sys.argv[0]])
    application.setDesktopFileName("appio")

    database_path = Path(args.db)
    store = AppIoStore(database_path)
    local_service = QueryService(store)
    registry = AppRegistry()
    wake = _wake_object()
    router_holder: dict[str, HistoryRouter] = {}

    def on_gap(notice) -> None:
        router = router_holder.get("router")
        if router is not None:
            router.record_transport_gap(notice)

    client = IodClient(SOCKET_PATH, on_gap=on_gap, on_change=wake.woke.emit)
    router = HistoryRouter(local_service, client, registry)
    router_holder["router"] = router

    def local_factory() -> LiveCollector:
        # The coordinator's lazy factory owns the decision to collect locally.
        # An accepted export is a pending incoming handover, not a reason to
        # discard the persisted cutoff and start a fresh baseline.
        offer = coordinator.resume_handover
        return LiveCollector(
            proc_root=args.proc_root,
            cgroup_root=args.cgroup_root,
            database_path=database_path,
            source="gui",
            baseline_only=offer is None,
            resume_handover=offer,
        )

    coordinator = CollectorCoordinator(
        local_factory=local_factory,
        client=client,
        history=router,
        lease=GuiLease(_gui_lease_path()),
        clock=SystemClock(),
    )
    window = CollectorWindow(
        coordinator,
        router,
        SetupClient(launch=_launch_helper, probe=production_probe),
        pump=client.pump,
        refresh_interval_ms=max(1, int(round(args.interval * 1000))),
    )
    wake.woke.connect(window.refresh)
    window.show()
    window.refresh()
    try:
        return application.exec()
    finally:
        window.shutdown()
        store.connection.close()
