"""Offscreen proof that one switch owns privilege and everything else navigates.

Every test injects a fake setup client, a fake coordinator and a fake history
router. The assertions are about *mechanism*: which probe text is displayed,
which argv is elevated, which coordinator transition runs, and — for every deep
link — that none of them ran at all.
"""

from __future__ import annotations

from concurrent.futures import Future
import threading
from types import SimpleNamespace

import pytest

from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QMenu,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
)

from appio.collector_owner import ServiceFacts
from appio.contracts import RAW_BUCKET_NS, Window
from appio.history_export import ExportResult
from appio.setup_boundary import CommandResult
from appio.setup_client import SetupReply
from appio.ui import collector_window as cw
from appio.ui.collector_window import (
    ACTIVATION_INACTIVE,
    COLLECTOR_STATE_NEEDS_ACTIVATION,
    COLLECTOR_STATE_OFF,
    COLLECTOR_STATE_ON_ANSWERING,
    COLLECTOR_STATE_SILENT,
    COLLECTOR_STATE_UNAVAILABLE,
    FILES_UNAVAILABLE,
    CollectorWindow,
    consent_text,
)

OFF = ServiceFacts(False, "inactive", False, False, False)
ON = ServiceFacts(True, "active", True, True, False)
SILENT = ServiceFacts(True, "unknown", False, False, None)
NEEDS_ACTIVATION = ServiceFacts(True, "active", True, True, True)
UNKNOWN = ServiceFacts(None, "unknown", False, False, None)

BOOT = "boot-1"
END = 8 * RAW_BUCKET_NS


class FakeConnection:
    def __init__(self, boot_id: str | None = BOOT):
        self.boot_id = boot_id
        self.statements: list[str] = []

    def execute(self, sql: str, params=()):  # noqa: ANN001 - sqlite-shaped seam
        self.statements.append(sql)
        row = None if self.boot_id is None else (self.boot_id,)
        return SimpleNamespace(fetchone=lambda: row)


class FakeHistory:
    """Router-shaped; the ready gate is flipped by the test, never implied."""

    def __init__(self, *, ready: bool = False, boot_id: str | None = BOOT, events=None):
        self.local = SimpleNamespace(connection=FakeConnection(boot_id))
        self.view_state = "loading"
        self.actions: list[tuple] = []
        self.events = events if events is not None else []
        self.prepared: Window | None = None
        self.ranking_calls: list[Window] = []
        self._ready = ready
        self.rows: list[object] = []

    def prepare(self, window: Window) -> None:
        self.actions.append(("prepare", window))
        self.events.append(("prepare", window))
        self.prepared = window
        self.view_state = "loading"

    def ready(self, window: Window) -> bool:
        return self._ready and window == self.prepared

    def become_ready(self):
        self._ready = True
        self.view_state = "complete"

    def ranking(self, window: Window, metric: str):
        if not self.ready(window):
            raise RuntimeError("history is not ready for this window")
        self.ranking_calls.append(window)
        return list(self.rows)

    def disk_share(self, app_id: int, window: Window):
        raise AssertionError("these tests render an empty ranking")


class FakeCoordinator:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.reconciles: list[ServiceFacts] = []
        self.ticks = 0
        self.exports: list[ExportResult] = []
        self.actions: list[tuple] = []

    def reconcile(self, facts: ServiceFacts):
        from appio.collector_owner import decide_owner

        self.reconciles.append(facts)
        self.actions.append(("reconcile", facts))
        self.events.append(("reconcile", facts))
        return decide_owner(facts)

    def tick_local(self) -> None:
        self.ticks += 1
        self.actions.append(("tick_local",))
        self.events.append(("tick_local",))

    def accept_export(self, result: ExportResult) -> None:
        self.exports.append(result)
        self.actions.append(("accept_export", result))
        self.events.append(("accept_export", result))


class FakeSetup:
    """A setup client that records calls and hands the test the completion."""

    def __init__(self, facts: ServiceFacts = OFF, events=None):
        self.facts = facts
        self.events = events if events is not None else []
        self.calls: list[str] = []
        self.pending: dict[str, list[Future]] = {}
        self.refresh_calls = 0

    def refresh(self) -> ServiceFacts:
        self.refresh_calls += 1
        self.events.append(("probe",))
        return self.facts

    def invoke(self, verb: str) -> Future:
        self.calls.append(verb)
        self.events.append(("invoke", verb))
        done: Future = Future()
        self.pending.setdefault(verb, []).append(done)
        return done

    def complete(self, verb: str, reply: SetupReply, index: int = 0) -> None:
        self.pending[verb][index].set_result(reply)


def ok(verb: str, body: dict | None = None) -> SetupReply:
    return SetupReply(0, "", {"version": 1, "verb": verb, "uid": 1000, "state": "off",
                              "export_path": None, "export_cutoff": None,
                              "error": None, **(body or {})})


def build(qtbot, *, facts=OFF, ready=False, setup=None, events=None):
    events = events if events is not None else []
    coordinator = FakeCoordinator(events)
    history = FakeHistory(ready=ready, events=events)
    setup = setup if setup is not None else FakeSetup(facts, events)
    setup.events = events
    window = CollectorWindow(coordinator, history, setup,
                             clock_ns=lambda: END - RAW_BUCKET_NS // 2)
    qtbot.addWidget(window)
    qtbot.addWidget(window.settings)
    window.show()
    return window, coordinator, history, setup


def click(control: QAbstractButton) -> None:
    control.click()


def assert_no_state_change(coordinator: FakeCoordinator, history: FakeHistory,
                           setup: FakeSetup) -> None:
    assert setup.calls == []
    assert coordinator.actions == []
    assert history.actions == []


# --------------------------------------------------------------------------- #
# Navigation never changes state
# --------------------------------------------------------------------------- #


def test_banner_and_files_deep_links_focus_settings_without_changing_state(qtbot):
    window, coordinator, history, setup = build(qtbot)
    assert_no_state_change(coordinator, history, setup)

    click(window.banner_open_settings)
    assert window.settings.isVisible()
    assert window.settings.focused_row == "system_services"
    assert window.settings.focusWidget() is window.settings.switch
    assert_no_state_change(coordinator, history, setup)

    window.open_files_unavailable()
    assert window.files_page.isVisible()
    assert FILES_UNAVAILABLE in window.files_message.text()
    click(window.files_open_settings)
    assert window.settings.focused_row == "system_services"
    assert window.settings.isVisible()
    assert_no_state_change(coordinator, history, setup)


def test_open_settings_rejects_an_unknown_focus_row(qtbot):
    window, coordinator, history, setup = build(qtbot)
    with pytest.raises(ValueError):
        window.open_settings(focus="activation")
    assert_no_state_change(coordinator, history, setup)


def test_the_switch_is_the_only_root_control_and_sits_inside_settings(qtbot):
    window, coordinator, history, setup = build(qtbot)
    switches = [button for button in window.findChildren(QAbstractButton)
                if "System services (root)" in button.text() and button.isCheckable()]
    assert len(switches) == 1
    assert window.settings.isAncestorOf(switches[0])
    assert isinstance(switches[0], QCheckBox)
    assert window.settings.switch is switches[0]
    # No tray, no menu, no second screen with a copy of the switch.
    assert window.findChildren(QSystemTrayIcon) == []
    assert window.findChildren(QMenu) == []
    assert window.findChildren(QTabWidget) == []
    assert window.findChildren(QTableWidget) == []
    assert [button.text() for button in window.findChildren(QAbstractButton)].count(
        "System services (root)") == 1


# --------------------------------------------------------------------------- #
# The switch follows the probe, never the click
# --------------------------------------------------------------------------- #


def test_the_switch_never_shows_the_click_before_the_probe(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    assert window.settings.switch.isChecked() is False

    click(window.settings.switch)
    assert window.settings.switch.isChecked() is False, "the click must not become the state"
    assert window.consent is not None and window.consent.isVisible()
    assert setup.calls == []


@pytest.mark.parametrize(
    ("facts", "state"),
    [
        (OFF, COLLECTOR_STATE_OFF),
        (ON, COLLECTOR_STATE_ON_ANSWERING),
        (SILENT, COLLECTOR_STATE_SILENT),
        (NEEDS_ACTIVATION, COLLECTOR_STATE_NEEDS_ACTIVATION),
    ],
)
def test_the_four_honest_states_render_distinctly(qtbot, facts, state):
    window, coordinator, history, setup = build(qtbot, facts=facts)
    window.refresh()
    assert window.state_text == state
    assert window.banner_status.text() == state
    assert window.settings.status_label.text() == state
    assert window.settings.switch.isChecked() is facts.enabled
    assert window.settings.switch.isEnabled() is True


def test_the_four_states_are_pairwise_distinct():
    assert len({COLLECTOR_STATE_OFF, COLLECTOR_STATE_ON_ANSWERING,
                COLLECTOR_STATE_SILENT, COLLECTOR_STATE_NEEDS_ACTIVATION}) == 4


def test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("install-delegation", ok("install-delegation",
                                            {"answering": True, "pending_activation": True}))
    window.choice.choose("reboot")
    # The helper claims answering; this session\'s own probe cannot reach the socket.
    setup.facts = SILENT
    setup.complete("enable", ok("enable", {"answering": True, "pending_activation": True,
                                           "state": "on, answering"}))
    assert window.state_text == COLLECTOR_STATE_SILENT
    assert window.settings.switch.isChecked() is True


def test_an_unknown_probe_disables_the_switch_instead_of_claiming_off(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=UNKNOWN)
    window.refresh()
    assert window.settings.switch.isEnabled() is False
    assert window.state_text == "unavailable"


def test_pending_activation_stays_visible_across_a_restart(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=NEEDS_ACTIVATION)
    window.refresh()
    assert window.state_text == COLLECTOR_STATE_NEEDS_ACTIVATION
    assert window.settings.switch.isChecked() is True


# --------------------------------------------------------------------------- #
# Consent, dismissal and exact commands
# --------------------------------------------------------------------------- #


def test_consent_lists_the_exact_commands_before_any_elevation(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    before = coordinator.actions[:]
    click(window.settings.switch)
    text = window.consent.body_text()
    assert "/usr/bin/pkexec /usr/bin/appio-setup install-delegation" in text
    assert "/usr/bin/pkexec /usr/bin/appio-setup enable" in text
    assert text.index("install-delegation") < text.index("appio-setup enable")
    for phrase in ("every process on this machine", "machine-wide collection",
                   "persistent root-owned history", "IO accounting",
                   "delegation", "appio group", "user-readable export"):
        assert phrase in text, phrase
    assert setup.calls == []
    assert coordinator.actions == before


def test_cancelled_consent_changes_nothing(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    before = coordinator.actions[:]
    click(window.settings.switch)
    window.consent.reject()
    assert setup.calls == []
    assert coordinator.actions == before
    assert window.settings.switch.isChecked() is False
    assert window.settings.switch.isEnabled() is True


@pytest.mark.parametrize(
    ("reply", "code", "line", "retryable"),
    [
        (SetupReply(126, "Authentication dismissed", None), "126", "Authentication dismissed", True),
        (SetupReply(127, "appio-setup not found", None), "127", "appio-setup not found", False),
        (SetupReply(1, "enable:daemon_not_answering", None), "1", "enable:daemon_not_answering", True),
    ],
)
def test_a_failed_dismissal_reverts_the_switch_and_shows_the_exit_code(
        qtbot, reply, code, line, retryable):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("install-delegation", reply)
    assert window.settings.switch.isChecked() is False
    assert window.settings.switch.isEnabled() is retryable
    assert f"exit {code}" in window.error_label.text()
    assert line in window.error_label.text()


def test_a_dismissal_after_a_previous_success_reverts_to_the_probed_on_state(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON)
    window.refresh()
    assert window.settings.switch.isChecked() is True
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("disable", SetupReply(126, "Authentication dismissed", None))
    assert window.settings.switch.isChecked() is True
    assert "exit 126" in window.error_label.text()


def test_a_container_host_reason_disables_the_switch_and_is_shown_verbatim(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("install-delegation",
                   SetupReply(1, "unsupported:container_host",
                              {"version": 1, "verb": "install-delegation", "uid": 1000,
                               "state": "unknown", "error": "unsupported:container_host"}))
    assert "unsupported:container_host" in window.error_label.text()
    assert window.settings.switch.isEnabled() is False


def test_a_missing_helper_reports_one_hundred_twenty_seven(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("install-delegation",
                   SetupReply(127, "appio-setup is not installed", None))
    assert "exit 127" in window.error_label.text()
    assert "appio-setup is not installed" in window.error_label.text()
    assert window.settings.switch.isEnabled() is False


# --------------------------------------------------------------------------- #
# Enable: quiesce, delegation, choice, pending activation
# --------------------------------------------------------------------------- #


def test_enable_quiesces_local_collection_before_the_first_elevation(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    events = setup.events
    events.clear()
    click(window.settings.switch)
    window.consent.accept()
    first_invoke = next(index for index, event in enumerate(events) if event[0] == "invoke")
    assert first_invoke > 0, "the local writer is quiesced before elevating"
    quiesce = next(event[1] for event in events[:first_invoke] if event[0] == "reconcile")
    from appio.collector_owner import decide_owner
    assert decide_owner(quiesce).may_collect_local is False
    assert decide_owner(quiesce).owner == "unknown"
    assert decide_owner(quiesce).state == COLLECTOR_STATE_UNAVAILABLE
    assert events[first_invoke] == ("invoke", "install-delegation")


def test_a_failed_quiesce_aborts_the_elevation(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    real = coordinator.reconcile

    def reconcile(facts):
        if facts == cw.QUIESCE_FACTS:
            raise RuntimeError("collector transition/tick is already in progress")
        return real(facts)

    coordinator.reconcile = reconcile
    click(window.settings.switch)
    window.consent.accept()
    assert setup.calls == []
    assert "already in progress" in window.error_label.text()
    assert window.busy is False


def test_restart_choice_warns_and_needs_a_distinct_confirmation(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    assert setup.calls == ["install-delegation"]
    setup.complete("install-delegation", ok("install-delegation", {"enabled": False}))
    assert window.choice is not None and window.choice.isVisible()
    assert cw.RESTART_CHOICE_WARNING in window.choice.body_text()
    assert cw.CHOICE_RESTART_LABEL in window.choice.body_text()

    window.choice.choose("restart")
    assert setup.calls == ["install-delegation", "enable"]
    setup.complete("enable", ok("enable", {"pending_activation": True,
                                           "state": COLLECTOR_STATE_NEEDS_ACTIVATION}))
    # Distinct, second confirmation: no restart command yet, and it ends the session.
    assert setup.calls == ["install-delegation", "enable"]
    assert window.restart_confirmation is not None
    assert window.restart_confirmation.isVisible()
    assert cw.RESTART_CHOICE_WARNING in window.restart_confirmation.body_text()
    window.restart_confirmation.accept()
    assert setup.calls == ["install-delegation", "enable", "restart-user-manager"]


def test_reboot_choice_never_runs_restart_or_reboot(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("install-delegation", ok("install-delegation", {"enabled": False}))
    window.choice.choose("reboot")
    setup.facts = NEEDS_ACTIVATION
    setup.complete("enable", ok("enable", {"pending_activation": True,
                                           "state": COLLECTOR_STATE_NEEDS_ACTIVATION}))
    assert setup.calls == ["install-delegation", "enable"]
    assert window.restart_confirmation is None
    for verb in setup.calls:
        assert verb not in {"restart-user-manager", "reboot", "restart"}
    window.refresh()
    assert window.state_text == COLLECTOR_STATE_NEEDS_ACTIVATION


def test_a_dismissed_choice_leaves_the_switch_on_the_probe(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("install-delegation", ok("install-delegation", {"enabled": False}))
    window.choice.reject()
    assert setup.calls == ["install-delegation"]
    assert window.settings.switch.isChecked() is False


# --------------------------------------------------------------------------- #
# Disable: own consent, verified export metadata only
# --------------------------------------------------------------------------- #


def test_disable_requires_its_own_consent_and_imports_verified_metadata(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON)
    window.refresh()
    click(window.settings.switch)
    text = window.consent.body_text()
    assert "/usr/bin/pkexec /usr/bin/appio-setup disable" in text
    assert "install-delegation" not in text
    window.consent.accept()
    assert setup.calls == ["disable"]

    setup.facts = OFF
    setup.complete("disable", ok("disable", {
        "enabled": False,
        "export_path": "/home/u/.local/share/appio/iod-export-1.db",
        "export_cutoff": {"boot_id": BOOT, "iod_epoch": 3, "seq": 9, "cutoff_mono_ns": 4 * RAW_BUCKET_NS},
    }))
    assert len(coordinator.exports) == 1
    exported = coordinator.exports[0]
    assert exported.path.name == "iod-export-1.db"
    assert (exported.boot_id, exported.iod_epoch, exported.seq, exported.cutoff_mono_ns) == (
        BOOT, 3, 9, 4 * RAW_BUCKET_NS)
    assert window.settings.switch.isChecked() is False


def test_disable_without_verified_export_metadata_imports_nothing(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    setup.complete("disable", ok("disable", {"enabled": False, "export_path": None}))
    assert coordinator.exports == []
    assert window.settings.switch.isChecked() is True, "the probe still says enabled"
    assert "export" in window.error_label.text().lower()


# --------------------------------------------------------------------------- #
# Explicit status recheck vs periodic refresh
# --------------------------------------------------------------------------- #


def test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=NEEDS_ACTIVATION)
    window.refresh()
    click(window.settings.recheck)
    text = window.consent.body_text()
    assert "/usr/bin/pkexec /usr/bin/appio-setup status" in text
    assert setup.calls == []
    window.consent.accept()
    assert setup.calls == ["status"]
    setup.complete("status", ok("status", {"state": ACTIVATION_INACTIVE}))
    assert window.state_text == ACTIVATION_INACTIVE
    assert window.banner_status.text() == ACTIVATION_INACTIVE


def test_periodic_refresh_never_invokes_the_helper(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON)
    for _ in range(3):
        window.refresh()
    assert setup.calls == []
    assert setup.refresh_calls == 3
    assert coordinator.ticks == 3


# --------------------------------------------------------------------------- #
# One operation at a time, never freezing Qt
# --------------------------------------------------------------------------- #


def test_a_pending_operation_rejects_a_second_one_across_a_qt_heartbeat(qtbot):
    window, coordinator, history, setup = build(qtbot)
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    assert setup.calls == ["install-delegation"]
    assert window.busy is True
    assert window.settings.switch.isEnabled() is False

    qtbot.wait(60)
    click(window.settings.switch)
    click(window.settings.recheck)
    qtbot.wait(20)
    assert setup.calls == ["install-delegation"]
    assert window.settings.switch.isChecked() is False

    setup.complete("install-delegation", ok("install-delegation", {"enabled": False}))
    assert window.busy is False
    assert window.settings.switch.isEnabled() is True


class RecordingWindow(CollectorWindow):
    """Records the thread that delivers each helper reply, without touching code."""

    def __init__(self, *args, **kwargs):
        self.reply_threads: list[int] = []
        super().__init__(*args, **kwargs)

    def _on_reply(self, future):
        self.reply_threads.append(threading.get_ident())
        super()._on_reply(future)


def test_a_reply_completed_on_a_worker_thread_updates_only_on_the_qt_thread(qtbot):
    events = []
    coordinator, history = FakeCoordinator(events), FakeHistory(events=events)
    setup = FakeSetup(OFF, events)
    window = RecordingWindow(coordinator, history, setup,
                             clock_ns=lambda: END - RAW_BUCKET_NS // 2)
    qtbot.addWidget(window)
    qtbot.addWidget(window.settings)
    window.show()
    window.refresh()
    click(window.settings.switch)
    window.consent.accept()
    thread = threading.Thread(target=setup.complete,
                              args=("install-delegation", ok("install-delegation", {"enabled": False})))
    thread.start()
    thread.join()
    qtbot.waitUntil(lambda: window.busy is False, timeout=2000)
    assert window.choice is not None and window.choice.isVisible()
    # Every reply, including one completed on a worker, mutated widgets on Qt's thread.
    assert window.reply_threads and set(window.reply_threads) == {threading.get_ident()}


# --------------------------------------------------------------------------- #
# History gating: stale numbers are never rendered as current
# --------------------------------------------------------------------------- #


def test_history_is_prepared_before_rendering_and_stale_detail_is_hidden(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=False)
    window.refresh()
    assert [action[0] for action in history.actions] == ["prepare"]
    assert history.prepared == Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
    assert window.history_visible is False
    assert "loading" in window.history_state.text()
    assert history.ranking_calls == []

    history.become_ready()
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    assert history.ranking_calls == [history.prepared]


def test_no_boot_row_yet_waits_instead_of_querying_an_empty_scope(qtbot):
    window, coordinator, history, setup = build(qtbot)
    history.local = SimpleNamespace(connection=FakeConnection(None))
    window.refresh()
    assert history.actions == []
    assert window.history_visible is False
    assert window.history_state.text() == "waiting for the first sample"


def test_a_refresh_failure_is_reported_instead_of_crashing_the_timer(qtbot):
    window, coordinator, history, setup = build(qtbot)

    def explode(facts):
        raise RuntimeError("collector transition/tick is already in progress")

    coordinator.reconcile = explode
    window.refresh()
    assert "already in progress" in window.error_label.text()


# --------------------------------------------------------------------------- #
# Production probe mapping (no systemd, no socket in the test)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("unit_state", "expected"),
    [("enabled", True), ("enabled-runtime", True), ("linked", True),
     ("disabled", False), ("masked", False), ("indirect", False), ("mystery", None)],
)
def test_unit_file_states_map_to_probed_enablement(unit_state, expected):
    enabled, active = cw.parse_unit_facts(f"UnitFileState={unit_state}\nActiveState=active\n")
    assert enabled is expected
    assert active == "active"


def test_a_malformed_or_failed_unit_query_is_unknown_not_off():
    assert cw.parse_unit_facts("ActiveState=active\n") == (None, "unknown")
    # A trailing blank line is not a malformed reply.
    assert cw.parse_unit_facts("UnitFileState=enabled\nActiveState=inactive\n\n") == (True, "inactive")
    assert cw.parse_unit_facts("UnitFileState=off\nActiveState=dead\n") == (None, "unknown")
    facts = cw.production_probe(
        command=lambda argv: CommandResult(1, "", "no systemd"),
        status_reader=lambda: None)
    assert facts == ServiceFacts(None, "unknown", False, False, None)
    assert cw.production_probe(
        command=lambda argv: (_ for _ in ()).throw(OSError("missing")),
        status_reader=lambda: None) == ServiceFacts(None, "unknown", False, False, None)


def test_the_production_probe_uses_the_daemon_status_for_answering_and_pending():
    facts = cw.production_probe(
        command=lambda argv: CommandResult(0, "UnitFileState=enabled\nActiveState=active\n", ""),
        status_reader=lambda: {"answering": True, "pending_activation": True})
    assert facts == ServiceFacts(True, "active", True, True, True)
    silent = cw.production_probe(
        command=lambda argv: CommandResult(0, "UnitFileState=enabled\nActiveState=active\n", ""),
        status_reader=lambda: None)
    assert silent.answering is False and silent.daemon_verified is False
    assert silent.pending_activation is None


# --------------------------------------------------------------------------- #
# Real router integration: the gate is the router's own ready(), not a fake
# --------------------------------------------------------------------------- #


def test_real_history_router_gates_rendering_until_ready(qtbot, tmp_path):
    from appio.history_router import HistoryRouter
    from appio.iodclient import AppRegistry, ClientState
    from appio.query import QueryService
    from appio.store import AppIoStore

    class IdleClient:
        state = ClientState()

        def query(self, request):
            raise AssertionError("no remote source in this test")

    store = AppIoStore(tmp_path / "history.db")
    store.start_boot(BOOT, 1, 0, 1000.0, "gui")
    store.connection.commit()
    history = HistoryRouter(QueryService(store), IdleClient(), AppRegistry())
    coordinator = FakeCoordinator()
    setup = FakeSetup(OFF)
    window = CollectorWindow(coordinator, history, setup,
                             clock_ns=lambda: END - RAW_BUCKET_NS // 2)
    qtbot.addWidget(window)
    qtbot.addWidget(window.settings)
    window.show()
    try:
        window.refresh()
        assert window._prepared == Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
        if not window.history_visible:
            assert "loading" in window.history_state.text()
        qtbot.waitUntil(lambda: window.history_visible is True, timeout=5000)
        assert history.ready(window._prepared)
        assert window.history_state.text() == ""
    finally:
        history.close()
        store.connection.close()


def test_consent_text_is_verb_specific_and_never_mentions_a_path():
    enable = consent_text("enable")
    disable = consent_text("disable")
    status = consent_text("status")
    assert "appio-setup install-delegation" in enable
    assert "appio-setup enable" in enable
    assert "appio-setup disable" not in enable
    assert "appio-setup disable" in disable
    assert "appio-setup status" in status
    assert "/home/" not in enable + disable + status
    assert "--root" not in enable + disable + status
    with pytest.raises(ValueError):
        consent_text("export-history --path /tmp/x")


def test_the_displayed_state_constants_are_the_owner_decision_vocabulary():
    from appio.collector_owner import decide_owner

    assert decide_owner(OFF).state == COLLECTOR_STATE_OFF
    assert decide_owner(ON).state == COLLECTOR_STATE_ON_ANSWERING
    assert decide_owner(SILENT).state == COLLECTOR_STATE_SILENT
    assert decide_owner(NEEDS_ACTIVATION).state == COLLECTOR_STATE_NEEDS_ACTIVATION
    assert cw.ACTIVATION_INACTIVE == "delegation present, accounting inactive"
