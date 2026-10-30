"""Offscreen proof that one switch owns privilege and everything else navigates.

Most tests inject setup, coordinator and history fakes. The liveness regression
also runs the real coordinator, router and client against a temporary Unix-socket
daemon. The assertions cover probe text, elevated argv, ownership transitions,
and navigation that leaves collection and privilege state alone.
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
    QTableView,
    QTableWidget,
)

from appio.collector_owner import ServiceFacts
from appio.contracts import (
    RAW_BUCKET_NS,
    AppTotal,
    Coverage,
    DiskShare,
    FileCoverage,
    FileRow,
    FileTable,
    HookHits,
    UnknownOwnerOverflow,
    Window,
    WindowUnavailableError,
)
from appio.file_runtime import FileAvailability
from appio.history_export import ExportResult
from appio.history_router import FileViewResult
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
        # Files query surface: absent by default so a page that is not asked
        # for a view cannot silently acquire one.
        self.file_status = FileAvailability(False, False, "file_source_unavailable", None)
        self.file_result: FileViewResult | None = None
        self.file_requests: list[tuple] = []

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

    def file_source_status(self) -> FileAvailability:
        return self.file_status

    def request_file_view(self, app_id: int, window: Window, sort: str,
                          direction: str, search: str | None) -> Future:
        self.file_requests.append((app_id, window, sort, direction, search))
        done: Future = Future()
        if self.file_result is None:
            done.set_exception(
                WindowUnavailableError("file_source_unavailable", "no file source")
            )
        else:
            done.set_result(self.file_result)
        return done

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        row = next(row for row in self.rows if row.app_id == app_id)
        # No device attribution in this fake: None is rendered as unavailable.
        return DiskShare(
            app_id, window, row.read_bytes, row.write_bytes, row.total_bytes,
            (), None, None, None, None, None, None,
            file_coverage(device_state="not_delegated"),
        )


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


def file_coverage(**overrides: object) -> Coverage:
    values: dict[str, object] = {
        "tier": "raw",
        "complete": True,
        "unavailable_reason": None,
        "inaccessible_pids": 0,
        "resets": 0,
        "reap_corrections": 0,
        "reap_unapplied": (0, 0, 0, 0),
        "reap_unobserved": (0, 0, 0, 0),
        "partial_intervals": 0,
        "gap_intervals": 0,
        "overlap_uncertain_intervals": 0,
        "spans_suspend_intervals": 0,
        "spans_suspend": False,
        "reap_incomplete": False,
        "counter_overflow": False,
        "device_state": "available",
        "counted_omitted_files": 0,
        "counted_omitted_bytes_r": 0,
        "counted_omitted_bytes_w": 0,
        "uncounted_overflow_files": 0,
        "uncounted_overflow_bytes_r": 0,
        "uncounted_overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
        "weak_identity_rows": 0,
        "hook_hits": HookHits(1, 1, 0, 0, 0, 0),
    }
    values.update(overrides)
    return Coverage(**values)  # type: ignore[arg-type]


def file_view_result(window: Window | None = None) -> FileViewResult:
    """One synthetic retained row, shaped exactly like the query contract."""

    window = window or Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
    inner = file_coverage()
    row = FileRow(1, "report.txt", "resolved", False, "259:0", 42, 7, 3, 2, 100, 50,
                  150, BOOT, END - RAW_BUCKET_NS)
    overflow = UnknownOwnerOverflow(window, "overflow_owner_unknown", 0, 0, 0, inner)
    table = FileTable(1, window, (row,), "bytes_total", "desc", None, overflow, inner)
    coverage = FileCoverage(
        1, window, 0, 0, 0, 0, 0, 0, 0, 0, 0, inner.hook_hits, 100, 50, overflow, inner
    )
    return FileViewResult(table, coverage)


def app_total(app_id: int = 1) -> AppTotal:
    """One ranking row, so the Files app selector has an app to select."""

    return AppTotal(1, "unit:alpha.service", "alpha", 150, 150, 300, 150, 150,
                    None, None, None, file_coverage())


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


def test_verified_status_round_trip_clears_silent_header_while_window_open(qtbot):
    from test_iodclient import Harness

    window, coordinator, history, setup = build(qtbot, facts=SILENT)
    harness = Harness()
    history.client = harness.client
    window._pump = harness.client.pump
    window._timer.setInterval(10)
    try:
        window.refresh()
        assert window.state_text == COLLECTOR_STATE_SILENT
        harness.one_request("status")
        # A real, correlated, validated response on the GUI's own connection.
        # The separate one-shot service probe remains silent throughout.
        harness.status()
        qtbot.waitUntil(lambda: window.state_text == COLLECTOR_STATE_ON_ANSWERING,
                        timeout=1000)
        assert window.banner_status.text() == COLLECTOR_STATE_ON_ANSWERING
        assert window.settings.status_label.text() == COLLECTOR_STATE_ON_ANSWERING
        assert coordinator.reconciles[-1].daemon_verified
        assert coordinator.reconciles[-1].answering
        assert setup.calls == []
        # Disconnect clears current verification; a retained cursor/status must
        # not keep the successful label latched forever.
        harness.eof()
        qtbot.waitUntil(lambda: window.state_text == COLLECTOR_STATE_SILENT,
                        timeout=1000)
    finally:
        window._timer.stop()
        harness.client.close()
        for server in harness.servers:
            server.close()


@pytest.mark.parametrize("verified,answering,pending,expected", [
    (False, True, False, COLLECTOR_STATE_SILENT),
    (True, False, False, COLLECTOR_STATE_SILENT),
    (True, True, True, COLLECTOR_STATE_NEEDS_ACTIVATION),
])
def test_current_link_status_requires_verification_and_preserves_activation(
    qtbot, verified, answering, pending, expected,
):
    window, _, history, _ = build(qtbot, facts=SILENT)
    history.client = SimpleNamespace(state=SimpleNamespace(
        verified=verified, answering=answering, pending_activation=pending, cursor=None,
    ))
    window.refresh()
    assert window.state_text == expected
    window._timer.stop()


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
    assert window.stack.currentWidget() is window.files_page
    # No window has been prepared yet, so the page says so instead of showing a
    # zero table; the deep link is still the only action offered.
    assert window.files_message.isVisible()
    assert "waiting for the first sample" in window.files_message.text()
    assert window.files_view.isVisible() is False
    assert history.file_requests == []
    click(window.files_open_settings)
    assert window.settings.focused_row == "system_services"
    assert window.settings.isVisible()
    assert_no_state_change(coordinator, history, setup)


def test_opening_files_shows_the_real_table_and_changes_only_query_state(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=True)
    history.rows = [app_total()]
    history.file_status = FileAvailability(True, True)
    history.file_result = file_view_result()
    window.refresh()
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    # The page is prepared by the same exact Window the Overview renders.
    assert history.file_requests[-1][1] == history.prepared
    assert window.files_view.isVisibleTo(window.files_page)
    assert window.files_view.model.rowCount() == 1
    before_actions = list(coordinator.actions)
    before_calls = list(setup.calls)
    issued = len(history.file_requests)

    window.open_files_unavailable()
    assert window.stack.currentWidget() is window.files_page
    assert window.files_view.isVisible() and window.files_view.table.isVisible()
    assert window.files_message.isVisible() is False
    assert len(history.file_requests) == issued + 1
    assert coordinator.actions == before_actions
    assert setup.calls == before_calls
    assert window.settings.switch.isChecked() is False


def test_a_degraded_file_source_is_reported_while_history_is_still_loading(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=False)
    history.file_status = FileAvailability(
        False, False, "file_source_unavailable", "bpf load failed: EPERM"
    )
    window.refresh()
    assert window.history_visible is False
    before_actions = list(coordinator.actions)
    before_calls = list(setup.calls)

    window.open_files_unavailable()
    assert window.stack.currentWidget() is window.files_page
    assert window.files_message.isVisible()
    assert FILES_UNAVAILABLE in window.files_message.text()
    assert "bpf load failed: EPERM" in window.files_message.text()
    assert window.files_view.isVisible() is False
    assert history.file_requests == []
    assert coordinator.actions == before_actions
    assert setup.calls == before_calls


def test_files_page_keeps_an_explicit_unavailable_message_and_settings_link(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=True)
    history.file_status = FileAvailability(
        False, False, "file_source_unavailable", "bpf load failed: EPERM"
    )
    window.refresh()
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    before_actions = list(coordinator.actions)
    before_calls = list(setup.calls)

    window.open_files_unavailable()
    assert window.stack.currentWidget() is window.files_page
    assert history.file_requests == [], "an unavailable source is never queried"
    assert window.files_view.state == "unavailable"
    assert window.files_view.model.rowCount() == 0
    assert window.files_message.isVisible()
    assert FILES_UNAVAILABLE in window.files_message.text()
    assert "bpf load failed: EPERM" in window.files_message.text()
    assert coordinator.actions == before_actions
    assert setup.calls == before_calls
    click(window.files_open_settings)
    assert window.settings.focused_row == "system_services"
    assert coordinator.actions == before_actions and setup.calls == before_calls


def test_the_files_surface_never_appears_inside_the_overview_window(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=True)
    window.refresh()
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    assert window.view is not None
    assert window.view.findChildren(QTableView) == []
    assert window.stack.currentWidget() is window.history_page
    # The Files surface lives on its own page, never inside the Overview's page
    # or the P1/P2 AppIoWindow the acceptance guard inspects.
    assert not window.view.isAncestorOf(window.files_view)
    assert not window.history_page.isAncestorOf(window.files_view)
    assert window.files_view.parent() is window.files_page


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
    # Two read-only consumers rank the same prepared window: the Files app
    # selector and the Overview list. Neither widens the scope.
    assert history.ranking_calls
    assert set(history.ranking_calls) == {history.prepared}


@pytest.mark.parametrize("change", ["time", "boot", "owner", "invalidated", "ready", "local"])
def test_pending_history_yields_to_new_scope_ownership_or_invalidation(qtbot, change):
    window, coordinator, history, setup = build(qtbot, facts=OFF if change == "local" else ON)
    window._timer.stop()
    window._poll_timer.stop()
    window.refresh()
    original_window = history.prepared
    if change == "time":
        window._clock_ns = lambda: END + RAW_BUCKET_NS // 2
    elif change == "boot":
        history.local.connection.boot_id = "next-boot"
    elif change == "owner":
        setup.facts = OFF
    elif change == "invalidated":
        history.view_state = "unavailable"
    elif change == "ready":
        history.become_ready()

    window.refresh()
    assert len(history.actions) == 2
    assert coordinator.ticks == 2
    if change == "time":
        assert history.prepared.end_mono_ns == END + RAW_BUCKET_NS
    elif change == "boot":
        assert history.prepared.boot_id == "next-boot"
    else:
        assert history.prepared == original_window
        assert len(coordinator.reconciles) == 2
    if change == "owner":
        assert coordinator.reconciles[-1] == OFF
        assert window.state_text == COLLECTOR_STATE_OFF


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


def test_window_keeps_in_flight_history_for_the_same_scope(qtbot, tmp_path, monkeypatch):
    """A responsive daemon must populate an open window without a manual refresh."""
    from collections import deque
    import socket
    import sqlite3
    import time

    from appio.history_router import HistoryRouter
    from appio.iod_history import HistoryRpc
    from appio.iodclient import AppRegistry, IodClient
    from appio.ipc_models import StreamCursor, validate_request
    from appio.ipc_protocol import FrameDecoder, encode_frame
    from appio.ipc_security import Peer
    from appio.query import QueryService
    from appio.store import AppIoStore
    from test_history_router import source
    from test_iodclient import snapshot_frame, status_frame

    trace = deque(maxlen=64)
    local = AppIoStore(tmp_path / "gui.db")
    local.start_boot(BOOT, 1, 0, 1000.0, "gui")
    local.connection.commit()
    daemon_store, _ = source(
        tmp_path / "daemon.db", owner="iod", boot=BOOT, start=100, end=110,
    )
    daemon_store.connection.close()
    errors = deque(maxlen=8)
    socket_path = tmp_path / "iod.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    listener.settimeout(0.1)
    stopping = threading.Event()

    def fake_daemon():
        peer = None
        connection = None
        try:
            connection = sqlite3.connect(tmp_path / "daemon.db")
            rpc = HistoryRpc(connection)
            while not stopping.is_set():
                try:
                    peer, _ = listener.accept()
                    trace.append(("daemon-accepted",))
                    break
                except socket.timeout:
                    continue
            if peer is None:
                return
            peer.settimeout(0.05)
            decoder = FrameDecoder()
            while not stopping.is_set():
                try:
                    data = peer.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                for message in decoder.feed(data):
                    trace.append(("daemon-recv", message["type"]))
                    if message["type"] == "status":
                        reply = status_frame(
                            cursor=StreamCursor(BOOT, 1, 0),
                            request_id=message["request_id"],
                        )
                    elif message["type"] == "subscribe":
                        reply = snapshot_frame(1, boot_id=BOOT, epoch=1, ts=120_000_000_000)
                    elif message["type"] == "query":
                        trace.append(("daemon-query", message["query"]["kind"], time.monotonic_ns()))
                        request = validate_request(message)
                        reply = rpc.execute(request.query, request_id=request.request_id)
                    else:  # pragma: no cover - the production client sends no other requests
                        raise AssertionError(f"unexpected client message: {message['type']}")
                    peer.sendall(encode_frame(reply))
        except Exception as error:
            if not stopping.is_set():
                errors.append(("daemon-error", repr(error)))
        finally:
            if peer is not None:
                peer.close()
            if connection is not None:
                connection.close()

    server = threading.Thread(target=fake_daemon, name="fake-iod", daemon=True)
    server.start()

    history_ref = []
    client = IodClient(
        socket_path,
        peer_reader=lambda _sock: Peer(pid=4321, uid=0, gid=0),
        path_verifier=lambda _path: None,
        on_gap=lambda notice: history_ref[0].record_transport_gap(notice),
        on_change=lambda: None,
    )
    history = HistoryRouter(QueryService(local), client, AppRegistry())
    read_local = history._read_local

    def slow_local_snapshot(*args):
        # Model a loaded history worker; the daemon itself answers immediately.
        stopping.wait(0.18)
        return read_local(*args)

    monkeypatch.setattr(history, "_read_local", slow_local_snapshot)
    history_ref.append(history)
    coordinator = cw.CollectorCoordinator(
        local_factory=lambda: pytest.fail("a daemon-owned window must not collect locally"),
        client=client, history=history, lease=cw.GuiLease(tmp_path / "gui.lock"),
        clock=lambda: (120_000_000_000 - 1, 1_700_000_000_000_000_000),
    )
    setup = FakeSetup(SILENT)
    now = 120_000_000_000 - 1
    window = CollectorWindow(
        coordinator, history, setup,
        clock_ns=lambda: now,
        pump=client.pump,
        refresh_interval_ms=50,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window.settings)

    original_prepare = history.prepare
    original_ready = history.ready
    original_query = client.query
    original_pump = client.pump
    original_probe = setup.refresh
    original_show_error = window._show_error
    observed = {
        "pumps": 0, "probes": 0, "prepares": 0, "settled_local": 0, "ranking_settled": 0,
    }

    def instrumented_error(message):
        errors.append(("window-error", message))
        original_show_error(message)

    def instrumented_pump():
        observed["pumps"] += 1
        trace.append(("pump", time.monotonic_ns()))
        original_pump()
        trace.append(("client", client.state.verified, client.state.answering,
                      client.state.last_error, client.state.cursor))

    def instrumented_probe():
        observed["probes"] += 1
        facts = original_probe()
        trace.append(("status", client.state.verified, client.state.answering,
                      client.state.last_error, client.state.cursor))
        return facts

    def instrumented_prepare(scope):
        observed["prepares"] += 1
        trace.append(("prepare", history._generation + 1, scope))
        original_prepare(scope)
        future = history._local_future
        if future is not None:
            generation = history._generation

            def local_settled(done):
                observed["settled_local"] += 1
                trace.append(("local-future", generation, done.cancelled(),
                              done.exception() if not done.cancelled() else None))

            future.add_done_callback(local_settled)

    def instrumented_ready(scope):
        try:
            ready = original_ready(scope)
        except Exception as error:
            trace.append(("ready-error", type(error).__name__, str(error)))
            raise
        pending = history._pending
        trace.append(("ready", history._generation, ready,
                      None if pending is None else pending[1].kind,
                      None if pending is None else pending[2].done(), history.view_state))
        return ready

    def instrumented_query(request):
        future = original_query(request)

        def settled(done):
            try:
                done.result()
            except Exception as error:
                trace.append(("query-failed", request.kind, type(error).__name__, str(error)))
            else:
                if request.kind == "ranking":
                    observed["ranking_settled"] += 1
                trace.append(("query-settled", request.kind))

        future.add_done_callback(settled)
        return future

    client.pump = instrumented_pump
    setup.refresh = instrumented_probe
    history.prepare = instrumented_prepare
    history.ready = instrumented_ready
    client.query = instrumented_query
    window._pump = instrumented_pump
    window._show_error = instrumented_error
    wake = cw._wake_object()
    wake.woke.connect(window.refresh)
    client._on_change = wake.woke.emit
    window.show()

    def populated():
        return (
            window.history_visible
            and client.state.verified
            and client.state.answering
            and window._view is not None
            and window._view._list.count() == 1
            and history._remote is not None
            and bool(history._remote)
        )

    try:
        try:
            qtbot.waitUntil(populated, timeout=1800)
        except Exception as error:
            pytest.fail(
                f"history did not populate without an external refresh: {error}; "
                f"observed={observed}; state={window.state_text}; errors={list(errors)}; "
                f"trace={list(trace)}"
            )
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert observed["pumps"] > 1 and observed["probes"] > 1
        assert observed["ranking_settled"] > 0
        assert [(row.app_key, row.total_bytes) for row in history.ranking(
            window._prepared, "total_bytes",
        )] == [("exe:/usr/bin/shared", 40)]
        assert not errors, list(errors)
    finally:
        stopping.set()
        window.shutdown()
        listener.close()
        server.join(timeout=1)
        local.connection.close()
        assert not server.is_alive()


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
