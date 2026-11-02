"""Offscreen proof that one switch owns privilege and everything else navigates.

Most tests inject setup, coordinator and history fakes. The liveness regression
also runs the real coordinator, router and client against a temporary Unix-socket
daemon. The assertions cover probe text, elevated argv, ownership transitions,
and navigation that leaves collection and privilege state alone.
"""

from __future__ import annotations

from concurrent.futures import Future
import threading
import time
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
        self.ranking_reads = 0
        self.file_application_reads = 0
        self.status_reads = 0
        self.hold_file_requests = False
        self.gaps = []
        self.sources = []

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
        self.ranking_reads += 1
        if not self.ready(window):
            raise RuntimeError("history is not ready for this window")
        self.ranking_calls.append(window)
        return list(self.rows)

    def file_source_status(self) -> FileAvailability:
        self.status_reads += 1
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

    def close(self) -> None:
        """The in-memory fake has no external resources to release."""

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

    def close(self) -> None:
        """The fake coordinator owns no writer or lease."""

    def accept_export(self, result: ExportResult) -> None:
        self.exports.append(result)
        self.actions.append(("accept_export", result))
        self.events.append(("accept_export", result))


class FakeSetup:
    """A setup client that records calls and hands the test the completion.

    ``refresh()`` now runs on the shell's probe worker, so the configured facts
    and the call counter are shared controls protected by a lock. Widget state
    is only ever mutated by the delivery that lands back on the Qt thread.
    """

    def __init__(self, facts: ServiceFacts = OFF, events=None):
        self._facts = facts
        self._facts_lock = threading.Lock()
        self.events = events if events is not None else []
        self.calls: list[str] = []
        self.pending: dict[str, list[Future]] = {}
        self.refresh_calls = 0

    @property
    def facts(self) -> ServiceFacts:
        with self._facts_lock:
            return self._facts

    @facts.setter
    def facts(self, value: ServiceFacts) -> None:
        with self._facts_lock:
            self._facts = value

    def refresh(self) -> ServiceFacts:
        with self._facts_lock:
            self.refresh_calls += 1
            facts = self._facts
        # Worker observation only: no widget, coordinator or router mutation.
        self.events.append(("probe",))
        return facts

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
    return FileViewResult(table, coverage, None)


def app_total(app_id: int = 1) -> AppTotal:
    """One ranking row, so the Files app selector has an app to select."""

    return AppTotal(1, "unit:alpha.service", "alpha", 150, 150, 300, 150, 150,
                    None, None, None, file_coverage())


def build(qtbot, *, facts=OFF, ready=False, setup=None, events=None, history=None):
    """Compose the fake shell without driving any probe.

    The periodic timer is stopped so every legacy driver site owns its probe
    submissions explicitly and the counts stay deterministic; the liveness
    test that needs periodic refreshes starts the timer itself.
    """

    events = events if events is not None else []
    coordinator = FakeCoordinator(events)
    history = history if history is not None else FakeHistory(ready=ready, events=events)
    setup = setup if setup is not None else FakeSetup(facts, events)
    setup.events = events
    window = CollectorWindow(coordinator, history, setup,
                             clock_ns=lambda: END - RAW_BUCKET_NS // 2)
    qtbot.addWidget(window)
    qtbot.addWidget(window.settings)
    window.show()
    window._timer.stop()
    return window, coordinator, history, setup


def refresh_and_settle(qtbot, window):
    """Drive one probe from submission to its applied fact.

    The probe runs on the shell's worker executor now, so a driver site that
    used to read the state right after ``refresh()`` waits for ``facts_applied``
    to advance instead. Exactly one fact lands, so per-cycle counts stay honest.
    """

    after = window.facts_applied
    window.refresh()
    settle_probe(qtbot, window, after=after)
    assert window.facts_applied == after + 1, "one refresh must apply one probe"


def complete_and_settle(qtbot, window, setup, verb, reply, index=0):
    """Complete one helper reply and await the fresh probe it triggers."""

    after = window.facts_applied
    setup.complete(verb, reply, index)
    settle_probe(qtbot, window, after=after)


def test_verified_status_round_trip_clears_silent_header_while_window_open(qtbot):
    from test_iodclient import Harness

    window, coordinator, history, setup = build(qtbot, facts=SILENT)
    harness = Harness()
    history.client = harness.client
    window._pump = harness.client.pump
    try:
        refresh_and_settle(qtbot, window)
        assert window.state_text == COLLECTOR_STATE_SILENT
        harness.one_request("status")
        # A real, correlated, validated response on the GUI's own connection.
        # The separate one-shot service probe remains silent throughout.
        harness.status()
        # The next probe observes this window's own verified round-trip.
        refresh_and_settle(qtbot, window)
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert window.banner_status.text() == COLLECTOR_STATE_ON_ANSWERING
        assert window.settings.status_label.text() == COLLECTOR_STATE_ON_ANSWERING
        assert coordinator.reconciles[-1].daemon_verified
        assert coordinator.reconciles[-1].answering
        assert setup.calls == []
        # Disconnect clears current verification; a retained cursor/status must
        # not keep the successful label latched forever.
        harness.eof()
        refresh_and_settle(qtbot, window)
        assert window.state_text == COLLECTOR_STATE_SILENT
    finally:
        window._timer.stop()
        harness.client.close()
        for server in harness.servers:
            server.close()


def test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF(qtbot):
    """A dead stream must not demote a daemon the unprivileged probe still reaches.

    The window's own connection is gone, so the only remaining witness for
    "does the status socket answer" is ``SetupClient.refresh()``. Three
    separate refreshes are checked, each with its own invocation count, so a
    loop that never re-probes cannot pass as a steady state.
    """

    from test_iodclient import Harness

    window, coordinator, history, setup = build(qtbot, facts=ON)
    window._timer.stop()
    harness = Harness()
    history.client = harness.client
    window._pump = harness.client.pump
    try:
        harness.handshake()
        refresh_and_settle(qtbot, window)
        assert harness.client.state.verified and harness.client.state.answering
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert len(harness.servers) == 1

        harness.eof()
        harness.client.pump()
        assert not harness.client.state.verified
        assert not harness.client.state.answering

        probes_before = setup.refresh_calls
        ticks_before = coordinator.ticks
        for tick in range(1, 4):
            setup.facts = ON
            refresh_and_settle(qtbot, window)
            # The loop really ran the probe and really completed a refresh tick.
            assert setup.refresh_calls == probes_before + tick
            assert coordinator.ticks == ticks_before + tick
            # ... and the stream is still dead, so the probe is the only witness.
            assert not harness.client.state.verified
            assert len(harness.servers) == 1
            assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
            assert window.banner_status.text() == COLLECTOR_STATE_ON_ANSWERING
            assert window.settings.status_label.text() == COLLECTOR_STATE_ON_ANSWERING
            assert coordinator.reconciles[-1].daemon_verified
            assert coordinator.reconciles[-1].answering
        # No root helper was ever consulted: the unprivileged probe is enough.
        assert setup.calls == []
    finally:
        window._timer.stop()
        harness.client.close()
        for server in harness.servers:
            server.close()


def test_label_recovers_after_silent_refresh_and_connection_reverification(qtbot):
    """Demote on one silent probe, then prove recovery is a *verified* link.

    The intermediate ``enabled, not answering`` is the load-bearing step: an
    answering/label-only trace cannot tell a live label from a frozen one. The
    final state is deliberately not satisfied by the probe alone -- the probe
    answers before any reconnection, so the pin only passes once this window's
    new connection completes its own status exchange.
    """

    from test_iodclient import Harness

    window, coordinator, history, setup = build(qtbot, facts=ON)
    window._timer.stop()
    label = window.settings.status_label
    harness = Harness()
    history.client = harness.client
    window._pump = harness.client.pump
    try:
        harness.handshake()
        refresh_and_settle(qtbot, window)
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert harness.client.state.verified and harness.client.state.answering
        assert len(harness.servers) == 1

        harness.eof()
        harness.client.pump()
        probes_before = setup.refresh_calls
        setup.facts = SILENT
        refresh_and_settle(qtbot, window)
        # Observable intermediate demotion: one silent probe is enough to
        # withdraw the answering claim while the stream is gone.
        assert setup.refresh_calls == probes_before + 1
        assert window.state_text == "enabled, not answering"
        assert window.state_text == COLLECTOR_STATE_SILENT
        assert window.banner_status.text() == "enabled, not answering"
        assert window.settings.status_label.text() == "enabled, not answering"
        assert not harness.client.state.verified
        assert coordinator.reconciles[-1].daemon_verified is False
        assert coordinator.reconciles[-1].answering is False

        # The probe answers again. The label answers from the probe, but this
        # window's stream is still unverified -- so the label alone is not the
        # recovery evidence the next assertions require.
        setup.facts = ON
        refresh_and_settle(qtbot, window)
        assert setup.refresh_calls == probes_before + 2
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert not harness.client.state.verified

        # Reconnect on the harness clock, then complete the new link's own
        # status exchange before calling the connection verified.
        harness.clock.advance(60)
        harness.client.pump()
        assert len(harness.servers) == 2, "a new connection must be established"
        assert not harness.client.state.verified, "nothing is proven yet"
        status_request = harness.one_request("status")
        assert status_request["type"] == "status"
        harness.status()
        harness.client.pump()
        assert harness.client.state.verified and harness.client.state.answering

        refresh_and_settle(qtbot, window)
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert window.settings.status_label.text() == COLLECTOR_STATE_ON_ANSWERING
        # Recovered in place: no restart, no replacement window or label.
        assert window.settings.status_label is label
        assert window.isVisible()
        assert setup.calls == []
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
    refresh_and_settle(qtbot, window)
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
    view = window.view
    assert view is not None
    assert_no_state_change(coordinator, history, setup)

    click(window.banner_open_settings)
    assert window.settings.isVisible()
    assert window.settings.focused_row == "system_services"
    assert window.settings.focusWidget() is window.settings.switch
    assert_no_state_change(coordinator, history, setup)

    window.open_files_unavailable()
    # Deep links navigate the one persistent tab host; they never prepare or
    # query an application scope.
    assert view.detail_tabs.currentIndex() == 1
    # No window has been prepared yet, so the page says so instead of showing a
    # zero table; the deep link is still the only action offered.
    assert window.files_message.isVisible()
    assert "waiting for the first sample" in window.files_message.text()
    assert window.files_view.state == "idle"
    assert window.files_view.model.rowCount() == 0
    assert window.files_view.last_result is None
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
    refresh_and_settle(qtbot, window)
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    # The Files surface is prepared by the same exact Window the Overview
    # renders, and adopted by the visible tab.
    assert history.file_requests[-1][1] == history.prepared
    assert window.view.detail_tabs.currentIndex() == 0
    assert window.files_view.model.rowCount() == 1
    before_actions = list(coordinator.actions)
    before_calls = list(setup.calls)
    issued = len(history.file_requests)
    assert issued >= 1, "canonical adoption queries the shared scope"

    window.open_files_unavailable()
    assert window.view.detail_tabs.currentIndex() == 1
    assert window.files_view.isVisible() and window.files_view.table.isVisible()
    assert window.files_message.isVisible() is False
    # Tab navigation issues no second query: adoption already owned it.
    assert len(history.file_requests) == issued
    assert coordinator.actions == before_actions
    assert setup.calls == before_calls
    assert window.settings.switch.isChecked() is False


def test_a_degraded_file_source_is_reported_while_history_is_still_loading(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=False)
    history.file_status = FileAvailability(
        False, False, "file_source_unavailable", "bpf load failed: EPERM"
    )
    refresh_and_settle(qtbot, window)
    assert window.history_visible is False
    before_actions = list(coordinator.actions)
    before_calls = list(setup.calls)

    window.open_files_unavailable()
    assert window.view.detail_tabs.currentIndex() == 1
    assert window.files_message.isVisible()
    assert FILES_UNAVAILABLE in window.files_message.text()
    assert "bpf load failed: EPERM" in window.files_message.text()
    # The unavailable table never appears: the view holds no rows and no
    # settled result while the source is degraded and history is still loading.
    assert window.files_view.state == "idle"
    assert window.files_view.model.rowCount() == 0
    assert window.files_view.last_result is None
    assert history.file_requests == []
    assert coordinator.actions == before_actions
    assert setup.calls == before_calls


def test_files_page_keeps_an_explicit_unavailable_message_and_settings_link(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=True)
    history.file_status = FileAvailability(
        False, False, "file_source_unavailable", "bpf load failed: EPERM"
    )
    refresh_and_settle(qtbot, window)
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    before_actions = list(coordinator.actions)
    before_calls = list(setup.calls)

    window.open_files_unavailable()
    assert window.view.detail_tabs.currentIndex() == 1
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
    refresh_and_settle(qtbot, window)
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    assert window.view is not None
    overview = window.view.detail_tabs.widget(0)
    files_tab = window.view.detail_tabs.widget(1)
    assert overview is not None and files_tab is not None
    # The Overview tab owns no table; the single real Files surface lives in
    # the shared console's Files tab, never inside the Overview frame.
    assert overview.findChildren(QTableView) == []
    assert not overview.isAncestorOf(window.files_view)
    assert files_tab.isAncestorOf(window.files_view)
    assert window.findChildren(QTableView) == [window.files_view.table]
    # Exactly one detail tab host: the persistent two-tab console.
    assert window.findChildren(QTabWidget) == [window.view.detail_tabs]


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
    # No tray, no menu, no second screen with a copy of the switch. The detail
    # host is a tab widget now; that is the console, not a privilege surface.
    assert window.findChildren(QSystemTrayIcon) == []
    assert window.findChildren(QMenu) == []
    assert window.findChildren(QTableWidget) == []
    assert [button.text() for button in window.findChildren(QAbstractButton)].count(
        "System services (root)") == 1


# --------------------------------------------------------------------------- #
# The switch follows the probe, never the click
# --------------------------------------------------------------------------- #


def test_the_switch_never_shows_the_click_before_the_probe(qtbot):
    window, coordinator, history, setup = build(qtbot)
    refresh_and_settle(qtbot, window)
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
    refresh_and_settle(qtbot, window)
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
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "install-delegation", ok(
        "install-delegation", {"answering": True, "pending_activation": True}))
    assert window.choice is not None and window.choice.isVisible()
    window.choice.choose("reboot")
    # The helper claims answering; this session\'s own probe cannot reach the socket.
    setup.facts = SILENT
    probes_before = setup.refresh_calls
    complete_and_settle(qtbot, window, setup, "enable", ok(
        "enable", {"answering": True, "pending_activation": True,
                   "state": "on, answering"}))
    # The unprivileged probe ran after the helper replied, and its silence wins.
    assert setup.refresh_calls > probes_before
    assert window.state_text == COLLECTOR_STATE_SILENT
    assert window.banner_status.text() == COLLECTOR_STATE_SILENT
    assert window.settings.status_label.text() == COLLECTOR_STATE_SILENT
    assert coordinator.reconciles[-1].daemon_verified is False
    assert coordinator.reconciles[-1].answering is False
    assert window.settings.switch.isChecked() is True


def test_an_unknown_probe_disables_the_switch_instead_of_claiming_off(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=UNKNOWN)
    refresh_and_settle(qtbot, window)
    assert window.settings.switch.isEnabled() is False
    assert window.state_text == "unavailable"


def test_pending_activation_stays_visible_across_a_restart(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=NEEDS_ACTIVATION)
    refresh_and_settle(qtbot, window)
    assert window.state_text == COLLECTOR_STATE_NEEDS_ACTIVATION
    assert window.settings.switch.isChecked() is True


# --------------------------------------------------------------------------- #
# Consent, dismissal and exact commands
# --------------------------------------------------------------------------- #


def test_consent_lists_the_exact_commands_before_any_elevation(qtbot):
    window, coordinator, history, setup = build(qtbot)
    refresh_and_settle(qtbot, window)
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
    refresh_and_settle(qtbot, window)
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
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "install-delegation", reply)
    assert window.settings.switch.isChecked() is False
    assert window.settings.switch.isEnabled() is retryable
    assert f"exit {code}" in window.error_label.text()
    assert line in window.error_label.text()


def test_a_dismissal_after_a_previous_success_reverts_to_the_probed_on_state(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON)
    refresh_and_settle(qtbot, window)
    assert window.settings.switch.isChecked() is True
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "disable",
                        SetupReply(126, "Authentication dismissed", None))
    assert window.settings.switch.isChecked() is True
    assert "exit 126" in window.error_label.text()


def test_a_container_host_reason_disables_the_switch_and_is_shown_verbatim(qtbot):
    window, coordinator, history, setup = build(qtbot)
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "install-delegation",
                        SetupReply(1, "unsupported:container_host",
                                   {"version": 1, "verb": "install-delegation", "uid": 1000,
                                    "state": "unknown", "error": "unsupported:container_host"}))
    assert "unsupported:container_host" in window.error_label.text()
    assert window.settings.switch.isEnabled() is False


def test_a_missing_helper_reports_one_hundred_twenty_seven(qtbot):
    window, coordinator, history, setup = build(qtbot)
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "install-delegation",
                        SetupReply(127, "appio-setup is not installed", None))
    assert "exit 127" in window.error_label.text()
    assert "appio-setup is not installed" in window.error_label.text()
    assert window.settings.switch.isEnabled() is False


# --------------------------------------------------------------------------- #
# Enable: quiesce, delegation, choice, pending activation
# --------------------------------------------------------------------------- #


def test_enable_quiesces_local_collection_before_the_first_elevation(qtbot):
    setup = ControlledSetup(OFF)
    window, coordinator, history, setup = build(qtbot, setup=setup)
    try:
        # Initial facts land before any elevation: the ordering below is proven
        # on an already-collected window, not only on a cold start.
        refresh_and_settle(qtbot, window)
        assert window.facts_applied == 1
        assert window.state_text == COLLECTOR_STATE_OFF
        # A probe spanning the helper start belongs to the pre-helper generation.
        setup.started.clear()
        setup.hold = True
        window.refresh()
        assert setup.started.wait(2), "the spanning probe must have reached its worker"
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
        # Release the spanning probe: the helper start advanced the setup
        # generation, so its result is dropped instead of applied.
        setup.release.set()
        qtbot.waitUntil(lambda: window._dropped_facts == 1, timeout=2000)
        # The spanning probe's facts never land; the initial applied fact stands.
        assert window.facts_applied == 1
        complete_and_settle(qtbot, window, setup, "install-delegation",
                            SetupReply(1, "enable:daemon_not_answering", None))
    finally:
        setup.release.set()


def test_a_failed_quiesce_aborts_the_elevation(qtbot):
    window, coordinator, history, setup = build(qtbot)
    refresh_and_settle(qtbot, window)
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
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    assert setup.calls == ["install-delegation"]
    complete_and_settle(qtbot, window, setup, "install-delegation",
                        ok("install-delegation", {"enabled": False}))
    assert window.choice is not None and window.choice.isVisible()
    assert cw.RESTART_CHOICE_WARNING in window.choice.body_text()
    assert cw.CHOICE_RESTART_LABEL in window.choice.body_text()

    window.choice.choose("restart")
    assert setup.calls == ["install-delegation", "enable"]
    complete_and_settle(qtbot, window, setup, "enable", ok(
        "enable", {"pending_activation": True,
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
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "install-delegation",
                        ok("install-delegation", {"enabled": False}))
    assert window.choice is not None and window.choice.isVisible()
    window.choice.choose("reboot")
    setup.facts = NEEDS_ACTIVATION
    complete_and_settle(qtbot, window, setup, "enable", ok(
        "enable", {"pending_activation": True,
                   "state": COLLECTOR_STATE_NEEDS_ACTIVATION}))
    assert setup.calls == ["install-delegation", "enable"]
    assert window.restart_confirmation is None
    for verb in setup.calls:
        assert verb not in {"restart-user-manager", "reboot", "restart"}
    refresh_and_settle(qtbot, window)
    assert window.state_text == COLLECTOR_STATE_NEEDS_ACTIVATION


def test_a_dismissed_choice_leaves_the_switch_on_the_probe(qtbot):
    window, coordinator, history, setup = build(qtbot)
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "install-delegation",
                        ok("install-delegation", {"enabled": False}))
    assert window.choice is not None and window.choice.isVisible()
    after = window.facts_applied
    window.choice.reject()
    settle_probe(qtbot, window, after=after)
    assert setup.calls == ["install-delegation"]
    assert window.settings.switch.isChecked() is False


# --------------------------------------------------------------------------- #
# Disable: own consent, verified export metadata only
# --------------------------------------------------------------------------- #


def test_disable_requires_its_own_consent_and_imports_verified_metadata(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON)
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    text = window.consent.body_text()
    assert "/usr/bin/pkexec /usr/bin/appio-setup disable" in text
    assert "install-delegation" not in text
    window.consent.accept()
    assert setup.calls == ["disable"]

    setup.facts = OFF
    complete_and_settle(qtbot, window, setup, "disable", ok("disable", {
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
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    complete_and_settle(qtbot, window, setup, "disable",
                        ok("disable", {"enabled": False, "export_path": None}))
    assert coordinator.exports == []
    assert window.settings.switch.isChecked() is True, "the probe still says enabled"
    assert "export" in window.error_label.text().lower()


# --------------------------------------------------------------------------- #
# Explicit status recheck vs periodic refresh
# --------------------------------------------------------------------------- #


def test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=NEEDS_ACTIVATION)
    refresh_and_settle(qtbot, window)
    click(window.settings.recheck)
    text = window.consent.body_text()
    assert "/usr/bin/pkexec /usr/bin/appio-setup status" in text
    assert setup.calls == []
    window.consent.accept()
    assert setup.calls == ["status"]
    complete_and_settle(qtbot, window, setup, "status",
                        ok("status", {"state": ACTIVATION_INACTIVE}))
    assert window.state_text == ACTIVATION_INACTIVE
    assert window.banner_status.text() == ACTIVATION_INACTIVE


def test_periodic_refresh_never_invokes_the_helper(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON)
    for _ in range(3):
        refresh_and_settle(qtbot, window)
    assert setup.calls == []
    assert setup.refresh_calls == 3
    assert coordinator.ticks == 3


# --------------------------------------------------------------------------- #
# One operation at a time, never freezing Qt
# --------------------------------------------------------------------------- #


def test_a_pending_operation_rejects_a_second_one_across_a_qt_heartbeat(qtbot):
    window, coordinator, history, setup = build(qtbot)
    refresh_and_settle(qtbot, window)
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

    complete_and_settle(qtbot, window, setup, "install-delegation",
                        ok("install-delegation", {"enabled": False}))
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
    window._timer.stop()
    refresh_and_settle(qtbot, window)
    click(window.settings.switch)
    window.consent.accept()
    after = window.facts_applied
    thread = threading.Thread(target=setup.complete,
                              args=("install-delegation", ok("install-delegation", {"enabled": False})))
    thread.start()
    thread.join()
    qtbot.waitUntil(lambda: window.busy is False, timeout=2000)
    settle_probe(qtbot, window, after=after)
    assert window.choice is not None and window.choice.isVisible()
    # Every reply, including one completed on a worker, mutated widgets on Qt's thread.
    assert window.reply_threads and set(window.reply_threads) == {threading.get_ident()}


# --------------------------------------------------------------------------- #
# History gating: stale numbers are never rendered as current
# --------------------------------------------------------------------------- #


def test_history_is_prepared_before_rendering_and_stale_detail_is_hidden(qtbot):
    window, coordinator, history, setup = build(qtbot, ready=False)
    window.refresh()
    requested = Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
    # The requested scope is pending/target; the displayed alias only exists
    # after the frame is applied.
    assert window._pending == requested
    assert window._target == requested
    assert window._prepared is None
    assert [action[0] for action in history.actions] == ["prepare"]
    assert history.prepared == requested
    assert window.history_visible is False
    assert "loading" in window.history_state.text()
    assert history.ranking_calls == []

    history.become_ready()
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    assert window._prepared == requested
    # Two read-only consumers rank the same prepared window: the Files app
    # selector and the Overview list. Neither widens the scope.
    assert history.ranking_calls
    assert set(history.ranking_calls) == {history.prepared}


@pytest.mark.parametrize("change", ["time", "boot"])
def test_pending_history_retains_a_until_applied_before_b(qtbot, change):
    window, coordinator, history, setup = build(
        qtbot, facts=ON, history=ControlledHistory())
    window._timer.stop()
    window._poll_timer.stop()
    refresh_and_settle(qtbot, window)
    original_window = history.prepared
    assert window._pending == original_window
    assert window._displayed is None
    assert len(history.actions) == 1

    # Advance the requested scope without retiring the pending preparation.
    if change == "time":
        window._clock_ns = lambda: END + RAW_BUCKET_NS // 2
        advanced = Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END + RAW_BUCKET_NS)
    else:
        history.local.connection.boot_id = "next-boot"
        advanced = Window(kind="since_boot", boot_id="next-boot", end_mono_ns=END)

    window.refresh()
    # Target advance is not retirement: A stays pending and is prepared once.
    assert window._target == advanced
    assert window._pending == original_window
    assert len(history.actions) == 1
    assert coordinator.ticks == 1

    # A is still the only ready scope, so it becomes the displayed frame...
    history.become_ready(original_window)
    assert window.apply_ready_frame() is True
    assert window._displayed == original_window
    assert window._prepared == original_window
    # ... and only the later tick prepares and applies B.
    refresh_and_settle(qtbot, window)
    assert history.prepared == advanced
    assert len(history.actions) == 2
    history.become_ready(advanced)
    assert window.apply_ready_frame() is True
    assert window._displayed == advanced


def test_pending_history_invalidation_reprepares_once(qtbot):
    window, coordinator, history, setup = build(
        qtbot, facts=ON, history=ControlledHistory())
    window._timer.stop()
    window._poll_timer.stop()
    refresh_and_settle(qtbot, window)
    original_window = history.prepared
    assert window._pending == original_window
    assert len(history.actions) == 1

    history.view_state = "unavailable"
    refresh_and_settle(qtbot, window)
    # Retirement drops the stale pending preparation and replaces it once.
    assert len(history.actions) == 2
    assert history.prepared == original_window
    assert window._pending == original_window

    history.become_ready(original_window)
    assert window.apply_ready_frame() is True
    assert window._displayed == original_window
    assert window._prepared == original_window


def test_pending_history_changed_ownership_reprepares_once(qtbot):
    window, coordinator, history, setup = controlled_build(qtbot, facts=ON)
    window._timer.stop()
    window._poll_timer.stop()
    refresh_and_settle(qtbot, window)
    original_window = history.prepared
    assert len(history.actions) == 1
    assert len(coordinator.reconciles) == 1
    assert coordinator.ticks == 1
    assert window.state_text == COLLECTOR_STATE_ON_ANSWERING

    setup.facts = OFF
    # Changed ownership is reconciled as the new facts are applied, and that
    # reconcile invalidates the router generation: the retained preparation is
    # replaced exactly once while the requested scope is unchanged.
    coordinator.invalidate_on = "reconcile"
    refresh_and_settle(qtbot, window)
    assert history.prepared == original_window
    assert len(history.prepare_calls) == 2
    assert len(history.actions) == 2
    assert window._pending == original_window
    assert len(coordinator.reconciles) == 2
    assert coordinator.ticks == 2
    assert coordinator.reconciles[-1] == OFF
    assert window.state_text == COLLECTOR_STATE_OFF


def test_no_boot_row_yet_waits_instead_of_querying_an_empty_scope(qtbot):
    window, coordinator, history, setup = build(qtbot)
    history.local = SimpleNamespace(connection=FakeConnection(None))
    refresh_and_settle(qtbot, window)
    assert history.actions == []
    assert window.history_visible is False
    assert window.history_state.text() == "waiting for the first sample"


def test_a_refresh_failure_is_reported_instead_of_crashing_the_timer(qtbot):
    window, coordinator, history, setup = build(qtbot, facts=ON, ready=True)
    refresh_and_settle(qtbot, window)
    qtbot.waitUntil(lambda: window.history_visible is True, timeout=2000)
    displayed, state = window._displayed, window.state_text
    calls = []

    def explode(facts):
        calls.append(facts)
        raise RuntimeError("collector transition/tick is already in progress")

    coordinator.reconcile = explode
    # Await the error delivery from the fact application, not a synchronous
    # callback; the prior facts/frame stay visible and the callback survives.
    window.refresh()
    qtbot.waitUntil(lambda: "already in progress" in window.error_label.text(), timeout=2000)
    assert len(calls) == 1
    assert window._displayed == displayed
    assert window.state_text == state
    window.refresh()
    qtbot.waitUntil(lambda: len(calls) == 2, timeout=2000)
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
    window._timer.stop()
    try:
        window.refresh()
        requested = Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
        # The requested scope is pending; no displayed alias exists yet.
        assert window._pending == requested
        assert window._target == requested
        assert window._prepared is None
        if not window.history_visible:
            assert "loading" in window.history_state.text()
        qtbot.waitUntil(lambda: window.history_visible is True, timeout=5000)
        assert window._prepared == requested
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
        # At least two distinct probes were accepted and applied in place; the
        # periodic driver owns probe submission now, not a synchronous refresh.
        qtbot.waitUntil(lambda: window.facts_applied >= 2, timeout=2000)
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


# --------------------------------------------------------------------------- #
# Task 4: explicit shell ownership, queued probes and Files invalidation
# --------------------------------------------------------------------------- #


def test_probe_payload_rejects_malformed_combinations_and_counters():
    """The queued probe boundary accepts exactly one immutable outcome."""

    valid = cw.ProbePayload(OFF, None, 1, 0)
    assert valid.facts == OFF
    assert valid.error is None
    assert valid.ticket == 1
    assert valid.generation == 0
    with pytest.raises(ValueError):
        cw.ProbePayload(None, None, 1, 0)
    with pytest.raises(ValueError):
        cw.ProbePayload(OFF, "failure", 1, 0)
    with pytest.raises(ValueError):
        cw.ProbePayload(object(), None, 1, 0)
    with pytest.raises(ValueError):
        cw.ProbePayload(None, "  ", 1, 0)
    with pytest.raises(ValueError):
        cw.ProbePayload(None, "failure", True, 0)
    with pytest.raises(ValueError):
        cw.ProbePayload(None, "failure", 0, 0)
    with pytest.raises(ValueError):
        cw.ProbePayload(None, "failure", 1, True)
    with pytest.raises(ValueError):
        cw.ProbePayload(None, "failure", 1, -1)


class ControlledCoordinator(FakeCoordinator):
    def __init__(self):
        super().__init__()
        self.history = None
        self.invalidate_on = None

    def _invalidate_if(self, operation):
        if self.invalidate_on == operation:
            self.invalidate_on = None
            self.history.invalidate()

    def reconcile(self, facts):
        result = super().reconcile(facts)
        self._invalidate_if("reconcile")
        return result

    def tick_local(self):
        super().tick_local()
        self._invalidate_if("tick_local")

    def accept_export(self, result):
        super().accept_export(result)
        self._invalidate_if("accept_export")


class ControlledHistory(FakeHistory):
    def __init__(self, *, ready=False, boot_id=BOOT):
        super().__init__(ready=ready, boot_id=boot_id)
        self.prepare_calls: list[Window] = []
        self.prepare_failures: list[BaseException] = []
        self.ready_windows: set[Window] = set()
        self.ready_immediately = ready
        self.file_futures: list[Future] = []
        self.registry = SimpleNamespace(id_for=lambda key: 2 if key == "file:beta" else 3)
        self.file_apps: list[tuple[str, str, int]] = []
        self.local.file_applications = self._file_applications

    def _file_applications(self, _window):
        self.file_application_reads += 1
        return list(self.file_apps)

    def prepare(self, window: Window) -> None:
        self.prepare_calls.append(window)
        super().prepare(window)
        self._ready = False
        self.ready_windows.discard(window)
        if self.ready_immediately and len(self.prepare_calls) == 1:
            self.ready_windows.add(window)
        if self.prepare_failures:
            raise self.prepare_failures.pop(0)

    def ready(self, window: Window) -> bool:
        return window in self.ready_windows

    def become_ready(self, window: Window) -> None:
        self.prepared = window
        self.ready_windows.add(window)
        self.view_state = "ready"

    def request_file_view(self, app_id, window, sort, direction, search):
        self.file_requests.append((app_id, window, sort, direction, search))
        future = Future()
        self.file_futures.append(future)
        if not self.hold_file_requests and self.file_result is not None:
            future.set_result(self.file_result)
        return future

    def invalidate(self) -> None:
        self.view_state = "unavailable: synthetic invalidation"
        self.ready_windows.clear()
        for future in self.file_futures:
            if not future.done():
                future.set_exception(WindowUnavailableError(
                    "file_source_unavailable", "synthetic generation retired"
                ))

    def record_transport_gap(self, notice) -> None:
        self.gaps.append(notice)
        self.invalidate()

    def add_source(self, service, *, collector_id, owner) -> None:
        self.sources.append((service, collector_id, owner))
        self.invalidate()


class ControlledSetup(FakeSetup):
    def __init__(self, facts=OFF, *, hold=False):
        super().__init__(facts)
        self.started = threading.Event()
        self.release = threading.Event()
        self.hold = hold
        self.worker_ids: list[int] = []
        self.completed = 0

    def refresh(self) -> ServiceFacts:
        self.refresh_calls += 1
        self.worker_ids.append(threading.get_ident())
        facts = self.facts
        self.started.set()
        if self.hold:
            assert self.release.wait(5), "probe barrier was not released"
        self.completed += 1
        return facts


def controlled_build(qtbot, *, facts=OFF, ready=False, setup=None, history=None, pump=None):
    coordinator = ControlledCoordinator()
    history = history or ControlledHistory(ready=ready)
    coordinator.history = history
    setup = setup or ControlledSetup(facts)
    window = CollectorWindow(
        coordinator, history, setup,
        clock_ns=lambda: END - RAW_BUCKET_NS // 2,
        pump=pump,
        refresh_interval_ms=1000,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window.settings)
    window.show()
    return window, coordinator, history, setup


def settle_probe(qtbot, window, *, after):
    qtbot.waitUntil(lambda: window.facts_applied > after, timeout=2000)


def scope_window(end=END, *, boot=BOOT):
    return Window(kind="since_boot", boot_id=boot, end_mono_ns=end)


def apply_scope(window, history, target):
    history.become_ready(target)
    assert window.apply_ready_frame() is True
    assert window._displayed == target


def test_slow_probe_does_not_block_pump_or_pending_frame_application(qtbot):
    setup = ControlledSetup(OFF, hold=True)
    pumps = []
    window, _, history, setup = controlled_build(
        qtbot, setup=setup, pump=lambda: pumps.append(threading.get_ident())
    )
    try:
        window.refresh()
        assert setup.started.wait(1)
        qtbot.wait(310)
        assert setup.worker_ids == [setup.worker_ids[0]]
        assert setup.worker_ids[0] != threading.get_ident()
        started = time.monotonic()
        window.refresh()
        elapsed = time.monotonic() - started
        assert elapsed < 0.1
        assert len(pumps) == 2
        assert len(history.prepare_calls) == 1
        pending = window._pending
        history.become_ready(pending)
        assert window.apply_ready_frame() is True
        assert window._displayed == pending
        assert setup.completed == 0
    finally:
        setup.release.set()
        window._timer.stop()
        window._poll_timer.stop()


def test_replacement_keeps_frame_and_eventually_displays_new_scope(qtbot):
    window, _, history, _ = controlled_build(qtbot, ready=True)
    window.refresh()
    qtbot.waitUntil(lambda: window.history_visible, timeout=2000)
    first = window._displayed
    host = window.view
    history.local.connection.boot_id = "boot-2"
    window.refresh()
    assert window._displayed == first
    assert window._pending == scope_window(boot="boot-2")
    assert window.view is host
    assert window.history_visible
    assert "updating" in window.history_state.text()
    apply_scope(window, history, window._pending)
    assert window._prepared == scope_window(boot="boot-2")


def test_applied_scope_is_not_prepared_again_without_change_or_retirement(qtbot):
    window, _, history, _ = controlled_build(qtbot, ready=True)
    window.refresh()
    qtbot.waitUntil(lambda: window.history_visible, timeout=2000)
    count = len(history.prepare_calls)
    for _ in range(3):
        after = window.facts_applied
        window.refresh()
        settle_probe(qtbot, window, after=after)
    assert len(history.prepare_calls) == count
    history.view_state = "unavailable: test retirement"
    after = window.facts_applied
    window.refresh()
    settle_probe(qtbot, window, after=after)
    assert len(history.prepare_calls) == count + 1


def test_unchanged_facts_skip_reconcile_while_preparation_is_pending(qtbot):
    window, coordinator, history, _ = controlled_build(qtbot)
    window.refresh()
    qtbot.waitUntil(lambda: window.facts_applied == 1, timeout=2000)
    assert window._pending is not None
    reconciles = len(coordinator.reconciles)
    after = window.facts_applied
    window.refresh()
    settle_probe(qtbot, window, after=after)
    assert len(coordinator.reconciles) == reconciles
    assert coordinator.ticks == 2


def test_retired_pending_preparation_is_replaced_once(qtbot):
    window, _, history, _ = controlled_build(qtbot)
    window.refresh()
    target = window._pending
    assert target is not None
    history.view_state = "unavailable: ownership changed"
    window.refresh()
    assert history.prepare_calls == [target, target]
    window.refresh()
    assert history.prepare_calls == [target, target]
    apply_scope(window, history, target)
    assert window._displayed == target


@pytest.mark.parametrize("owner", ["clock", "explicit"])
@pytest.mark.parametrize("boundary", ["transaction", "memory_source", "submit"])
@pytest.mark.parametrize("resolution", ["retry", "no_target", "supersede"])
def test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick(
    real_files_shell, monkeypatch, owner, boundary, resolution
):
    import sqlite3
    from appio.query import QueryService

    shell = real_files_shell
    window, history = shell.window, shell.history
    snapshot = files_snapshot(shell.view)
    displayed = window._displayed
    target = shell.target
    prepare = history.prepare
    calls = []

    def record(scope):
        calls.append(scope)
        return prepare(scope)

    monkeypatch.setattr(history, "prepare", record)
    history.invalidate()
    memory = None
    try:
        with monkeypatch.context() as failure:
            if boundary == "transaction":
                shell.frames.db.execute("BEGIN")
                message = "uncommitted transaction"
            elif boundary == "memory_source":
                memory = sqlite3.connect(":memory:")
                history._sources["memory"] = (QueryService(memory), "gui")
                message = "file-backed SQLite"
            else:
                def reject(*_args, **_kwargs):
                    raise RuntimeError("synthetic submit refusal")
                failure.setattr(history._executor, "submit", reject)
                message = "synthetic submit refusal"
            if owner == "clock":
                window.refresh()
            else:
                with pytest.raises((ValueError, RuntimeError), match=message):
                    window.show_window(target)
            assert history.view_state == "loading"
            assert window._pending is None
            marker = window._prepare_retry
            assert marker.owner == owner and marker.window == target
            assert message in window._prepare_error
            assert not shell.view._scope_ready
            assert window._displayed == displayed
            assert files_snapshot(shell.view) == snapshot
            assert window.apply_ready_frame() is False
            assert calls == [target]
            if owner == "explicit":
                window._tick_target()
                assert calls == [target] and window._prepare_retry is marker
            else:
                # A repeated clock failure renews, rather than consumes, its marker.
                window._tick_target()
                assert calls == [target, target]
                assert window._prepare_retry is not marker
    finally:
        if shell.frames.db.in_transaction:
            shell.frames.db.rollback()
        if memory is not None:
            history._sources.pop("memory")
            memory.close()
    previous_calls = len(calls)
    if resolution == "no_target":
        with monkeypatch.context() as no_target:
            no_target.setattr(window, "_current_window", lambda: None)
            marker = window._prepare_retry
            window._tick_target()
            assert len(calls) == previous_calls
            if owner == "explicit":
                assert window._prepare_retry is marker
                assert not shell.view._scope_ready
                assert window._displayed == displayed
            else:
                assert window._prepare_retry is None and window._prepare_error is None
                assert window.error_label.text() == ""
                assert shell.view._scope_ready
                assert window._displayed is None
    if owner == "explicit":
        if resolution == "supersede":
            target = Window("since_boot", "boot-A",
                end_mono_ns=target.end_mono_ns + RAW_BUCKET_NS)
        window.show_window(target)
    else:
        window._tick_target()
    assert len(calls) == previous_calls + 1
    assert calls[-1] == target and window._pending == target
    assert window._prepare_retry is None and window._prepare_error is None
    assert window.error_label.text() == ""
    history._local_future.result(timeout=5)
    assert window.apply_ready_frame()
    assert window._displayed == target


def test_probe_spanning_setup_is_dropped_and_completion_probe_is_applied(qtbot):
    window, coordinator, _, _ = controlled_build(qtbot)
    window._probe_ticket = 2
    window._probe_inflight = Future()
    window._busy = True
    before = window.facts_applied
    window._apply_facts(cw.ProbePayload(OFF, None, 1, 0))
    assert window.facts_applied == before
    assert window._dropped_facts == 1
    assert window._probe_inflight is not None
    window._busy = False
    window._apply_facts(cw.ProbePayload(SILENT, None, 2, 0))
    assert window.facts_applied == before + 1
    assert coordinator.reconciles[-1] == SILENT


@pytest.mark.parametrize("outcome", ["facts", "error"])
def test_old_probe_ticket_cannot_overwrite_or_release_replacement_probe(qtbot, outcome):
    window, coordinator, history, _ = controlled_build(qtbot)
    current = Future()
    window._probe_ticket = 2
    window._probe_inflight = window._probe_successor = current
    window._facts = ON
    window._render(ON)
    window._show_error("appio: prior notice")
    stale = (cw.ProbePayload(OFF, None, 1, 0) if outcome == "facts"
             else cw.ProbePayload(None, "obsolete probe failure", 1, 0))

    def state():
        return (window._facts, window.state_text, window.error_label.text(),
                window.facts_applied, window._displayed, window._pending,
                tuple(coordinator.actions), tuple(history.prepare_calls))

    before = state()
    window._apply_facts(stale)
    assert state() == before
    assert window._probe_inflight is current and window._probe_successor is current
    assert window._dropped_facts == 1
    window._apply_facts(cw.ProbePayload(SILENT, None, 2, 0))
    assert window._facts == SILENT
    assert window._probe_inflight is None and window._probe_successor is None
    accepted = state()
    window._apply_facts(stale)
    assert state() == accepted
    assert window._dropped_facts == 2


def test_probe_error_keeps_last_banner_and_frame(qtbot):
    window, _, history, setup = controlled_build(qtbot, ready=True)
    window.refresh()
    qtbot.waitUntil(lambda: window.history_visible and window.facts_applied == 1,
                    timeout=2000)
    frame = window._displayed
    state = window.state_text
    window._probe_ticket += 1
    ticket = window._probe_ticket
    window._probe_inflight = Future()
    window._apply_facts(cw.ProbePayload(None, "probe unavailable", ticket, 0))
    assert window.state_text == state
    assert window._displayed == frame
    assert window.error_label.text() == "appio: probe unavailable"
    assert window._probe_inflight is None
    assert setup.completed == 1


def test_files_compatibility_navigation_selects_tab_without_banner_button(qtbot):
    window, _, history, _ = controlled_build(qtbot)
    view = window.view
    assert view is not None
    assert not hasattr(window, "files_nav")
    assert view.detail_tabs.count() == 2
    assert view.files_view is window.files_view
    assert view.detail_tabs.tabText(0) == "Overview"
    assert view.detail_tabs.tabText(1) == "Files"
    assert not hasattr(window, "stack")
    assert not hasattr(window, "files_page")
    before = len(history.file_requests)
    window.open_files_unavailable()
    assert view.detail_tabs.currentIndex() == 1
    assert len(history.file_requests) == before


def test_no_target_restores_admission_without_hiding_tabs(qtbot):
    window, _, _, _ = controlled_build(qtbot, history=ControlledHistory(boot_id=None))
    window.refresh()
    assert window._target is None
    assert window._displayed is None
    assert window._pending is None
    assert window.history_state.text() == "waiting for the first sample"
    assert window.view.detail_tabs.count() == 2
    assert window.files_view._scope_ready


def test_files_source_failure_preserves_exact_message_and_link(qtbot):
    window, _, history, _ = controlled_build(qtbot)
    history.file_status = FileAvailability(
        False, False, "file_source_unavailable", "bpf load failed: EPERM"
    )
    window.refresh()
    window.open_files_unavailable()
    assert (history.file_status.diagnostic, window._displayed, window._pending,
            window._target, window._files_status_text()) == (
                "bpf load failed: EPERM", None, scope_window(), scope_window(),
                "file activity is unavailable: file_source_unavailable: bpf load failed: EPERM",
            )
    assert window.files_message.text() == (
        "file activity is unavailable: file_source_unavailable: bpf load failed: EPERM"
    )
    assert window.files_open_settings.isVisible()
    assert history.file_requests == []


def test_explicit_show_window_driver_does_not_compute_clock_target(qtbot, monkeypatch):
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtWidgets import QApplication

    window, _, history, _ = controlled_build(qtbot)
    explicit = Window(kind="range", start_mono_ns=2, end_mono_ns=END, boot_id=BOOT)
    monkeypatch.setattr(window, "_current_window", lambda: pytest.fail("clock target computed"))
    def forbid_events(*_args, **_kwargs):
        pytest.fail("explicit scope driver pumped Qt events")
    with monkeypatch.context() as no_events:
        no_events.setattr(QCoreApplication, "processEvents", forbid_events)
        no_events.setattr(QApplication, "processEvents", forbid_events)
        window.show_window(explicit)
        history.become_ready(explicit)
        assert window.apply_ready_frame() is True
    assert window._displayed == explicit


def test_frame_apply_sets_displayed_before_files_sync(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window.refresh()
    target = window._pending
    seen = []
    original = window._sync_files

    def observe(*, request=False):
        seen.append(window._prepared)
        return original(request=request)

    monkeypatch.setattr(window, "_sync_files", observe)
    apply_scope(window, history, target)
    assert seen == [target]


@pytest.mark.parametrize("completion", ["success", "nonzero", "future_error", "invoke_error"])
def test_helper_completion_categories_advance_generation_before_busy_clears_and_reprobe(
    qtbot, monkeypatch, completion
):
    setup = ControlledSetup(hold=True)
    window, _, _, _ = controlled_build(qtbot, setup=setup)
    window._timer.stop()
    observed = []
    original_submit = window._submit_probe

    def observe_submit(*, replace_pending=False):
        observed.append((window._setup_generation, window.busy, replace_pending))
        return original_submit(replace_pending=replace_pending)

    monkeypatch.setattr(window, "_submit_probe", observe_submit)
    try:
        if completion == "invoke_error":
            def reject(_verb):
                raise RuntimeError("invoke failed")
            monkeypatch.setattr(setup, "invoke", reject)
        window._start("status", None)
        if completion != "invoke_error":
            assert window._setup_generation == 1 and window.busy
            future = setup.pending["status"][0]
            if completion == "future_error":
                future.set_exception(RuntimeError("helper future failed"))
            elif completion == "nonzero":
                future.set_result(SetupReply(1, "permission denied", None))
            else:
                future.set_result(ok("status"))
            qtbot.waitUntil(lambda: bool(observed))
        assert observed == [(2, False, True)]
        assert window._setup_generation == 2
        assert window._probe_ticket == 1
    finally:
        setup.release.set()
        window._timer.stop()
        window._poll_timer.stop()
        window._probe_executor.shutdown(wait=False, cancel_futures=True)


def populated_shell(qtbot, monkeypatch):
    history = ControlledHistory()
    history.rows = [app_total()]
    history.file_status = FileAvailability(True, True)
    history.file_result = file_view_result()
    window, coordinator, _, setup = controlled_build(qtbot, history=history)
    window._timer.stop()
    window._poll_timer.stop()
    monkeypatch.setattr(window, "_submit_probe", lambda **_kwargs: False)
    window.show_window(scope_window())
    apply_scope(window, history, scope_window())
    qtbot.waitUntil(lambda: window.files_view.last_result is not None)
    return window, coordinator, history, setup


def files_snapshot(view):
    return (view.model, view.table, view.last_result, tuple(view.model._cells),
            tuple(label.text() for label in view._footer_labels()),
            view._sort, view._direction, view.search(), view._app_id,
            view.table.verticalScrollBar().value())


def test_pending_scope_refuses_all_file_entry_points_then_flushes_once(qtbot, monkeypatch):
    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    view = window.files_view
    before = files_snapshot(view)
    target = scope_window(END + RAW_BUCKET_NS)
    window.show_window(target)
    reads = (history.ranking_reads, history.file_application_reads, history.status_reads)
    requests = len(history.file_requests)
    view.set_window(target)
    view.set_app_id(1)
    view.request_refresh()
    view.set_sort("bytes_w", "asc")
    view.search_field.setText("alpha")
    view._on_app_changed(view.app_selector.currentIndex())
    view._on_direction_clicked()
    view.flush_refresh()
    assert (history.ranking_reads, history.file_application_reads, history.status_reads) == reads
    assert len(history.file_requests) == requests
    assert files_snapshot(view)[:5] == before[:5]
    assert view._refresh_wanted
    apply_scope(window, history, target)
    assert len(history.file_requests) == requests + 1
    view.flush_refresh()
    assert len(history.file_requests) == requests + 1
    assert history.file_requests[-1] == (1, target, "bytes_w", "desc", "alpha")


def test_mixed_ranking_and_file_only_union_queries_file_only_subject(qtbot, monkeypatch):
    from dataclasses import replace

    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    view = window.files_view
    history.file_apps = [("file:beta", "Beta", 10)]
    history.file_result = replace(history.file_result,
        table=replace(history.file_result.table, app_id=2),
        coverage=replace(history.file_result.coverage, app_id=2))
    window.show_window(scope_window())
    apply_scope(window, history, scope_window())
    assert [view.app_selector.itemData(i) for i in range(view.app_selector.count())] == [1, 2]
    requests = len(history.file_requests)
    view.app_selector.setCurrentIndex(1)
    assert history.file_requests[-1][0] == history.registry.id_for("file:beta") == 2
    assert len(history.file_requests) == requests + 1
    assert window.view.selected_app_id() == 2
    model = window.view._overview_model
    assert all(kpi.text == "unavailable" for kpi in model.kpis)
    assert model.app_id == 2


def test_same_window_frame_refreshes_union_for_file_only_ranked_and_vanished_apps(qtbot, monkeypatch):
    from dataclasses import replace

    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    view = window.files_view
    table = view.table
    model = view.model
    history.file_apps = [("file:beta", "Beta", 10)]
    for ranked, extras, expected in [
        ([app_total()], history.file_apps, [1, 2]),
        ([app_total(), replace(app_total(), app_id=2, name="Beta")], [], [1, 2]),
        ([app_total()], [], [1]),
    ]:
        history.rows, history.file_apps = ranked, extras
        requests = len(history.file_requests)
        window.show_window(scope_window())
        apply_scope(window, history, scope_window())
        assert [view.app_selector.itemData(i) for i in range(view.app_selector.count())] == expected
        assert len(history.file_requests) == requests + 1
        assert view.table is table and view.model is model
        if 2 in expected:
            view.set_app_id(2)
    assert view._app_id == 1
    assert window.view.selected_app_id() == 1


def test_current_terminal_file_failure_clears_but_transient_failure_does_not(qtbot, monkeypatch):
    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    view = window.files_view
    before = files_snapshot(view)
    with view.transient_scope():
        view._handle_failure("file_source_unavailable", "retired")
    assert files_snapshot(view) == before
    assert view._refresh_wanted
    view._handle_failure("not_retained", "current evidence expired")
    assert view.model.rowCount() == 0
    assert view.last_result is None
    assert view.state == "unavailable"
    assert "current evidence expired" in view.unavailable_text()


def test_off_probe_during_enable_never_reacquires_local_writer(qtbot):
    setup = ControlledSetup(OFF, hold=True)
    window, coordinator, _, _ = controlled_build(qtbot, setup=setup)
    window._timer.stop()
    try:
        assert window._submit_probe()
        assert setup.started.wait(1)
        old = window._probe_inflight
        window._begin("enable")
        assert coordinator.reconciles == [cw.QUIESCE_FACTS]
        setup.facts = SILENT
        setup.release.set()
        old.result(timeout=2)
        qtbot.waitUntil(lambda: window._dropped_facts == 1)
        assert coordinator.reconciles == [cw.QUIESCE_FACTS]
        setup.complete("install-delegation", ok("install-delegation"))
        qtbot.waitUntil(lambda: window._probe_ticket == 2)
        settle_probe(qtbot, window, after=0)
        assert OFF not in coordinator.reconciles
        assert coordinator.reconciles[-1] == SILENT
    finally:
        setup.release.set()
        window._timer.stop()
        window._poll_timer.stop()
        window._probe_executor.shutdown(wait=False, cancel_futures=True)


def test_repeated_setup_stages_replace_one_queued_successor_while_probe_runs(qtbot, monkeypatch):
    setup = ControlledSetup(OFF, hold=True)
    window, _, _, _ = controlled_build(qtbot, setup=setup)
    window._timer.stop()
    submissions = []
    executor_submit = window._probe_executor.submit

    def observe_submit(*args, **kwargs):
        submissions.append((window._probe_ticket, window._probe_successor))
        return executor_submit(*args, **kwargs)

    monkeypatch.setattr(window._probe_executor, "submit", observe_submit)
    try:
        assert window._submit_probe()
        assert setup.started.wait(1)
        running = window._probe_inflight
        window._start("status", None)
        setup.complete("status", ok("status"))
        qtbot.waitUntil(lambda: window._probe_ticket == 2)
        first_successor = window._probe_inflight
        assert not first_successor.running()
        window._start("status", None)
        setup.complete("status", ok("status"), index=1)
        qtbot.waitUntil(lambda: window._probe_ticket == 3)
        newest = window._probe_inflight
        assert submissions[-2:] == [(2, None), (3, None)]
        assert first_successor.cancelled()
        assert window._probe_successor is newest
        for _ in range(3):
            assert window._submit_probe() is False
        assert setup.refresh_calls == 1
        setup.facts = SILENT
        setup.release.set()
        running.result(timeout=2)
        newest.result(timeout=2)
        # A settled Future still owns its slot until its queued payload applies.
        assert window._submit_probe() is False
        settle_probe(qtbot, window, after=0)
        assert setup.refresh_calls == 2
        assert window._facts == SILENT
    finally:
        setup.release.set()
        window._timer.stop()
        window._poll_timer.stop()
        window._probe_executor.shutdown(wait=False, cancel_futures=True)


def test_close_during_probe_drops_all_late_effects(qtbot, monkeypatch):
    setup = ControlledSetup(OFF, hold=True)
    window, coordinator, history, _ = controlled_build(qtbot, setup=setup)
    window._timer.stop()
    closed = []
    monkeypatch.setattr(coordinator, "close", lambda: closed.append("coordinator"), raising=False)
    monkeypatch.setattr(history, "close", lambda: closed.append("history"), raising=False)
    try:
        window._submit_probe()
        assert setup.started.wait(1)
        future = window._probe_inflight
        before = (window.facts_applied, window._dropped_facts, window.state_text,
                  window.error_label.text(), window._displayed, tuple(coordinator.actions))
        started = time.monotonic()
        window.shutdown()
        assert time.monotonic() - started < 0.1
        assert closed == ["coordinator", "history"]
        window.shutdown()
        assert closed == ["coordinator", "history"]
        setup.release.set()
        future.result(timeout=2)
        qtbot.wait(30)
        window._apply_facts(cw.ProbePayload(None, "late failure", 1, 0))
        assert (window.facts_applied, window._dropped_facts, window.state_text,
                window.error_label.text(), window._displayed, tuple(coordinator.actions)) == before
    finally:
        setup.release.set()


@pytest.fixture
def real_files_shell(qtbot, tmp_path, monkeypatch):
    from dataclasses import replace
    from appio.bpf_fileio import BpfFileSource, VectorLoader
    from appio.contracts import FILE_HOUR_NS
    from appio.file_runtime import FileRuntime
    from appio.files import FileIdentityResolver
    from appio.history_router import HistoryRouter
    from appio.iodclient import AppRegistry
    from appio.query import QueryService
    from tests.p3.test_iodclient import Harness
    from tests.p4.test_file_runtime import Frames, event

    path = tmp_path / "real-files.db"
    frames = Frames(path)
    loader = VectorLoader()
    runtime = FileRuntime(frames.db, source_factory=lambda: BpfFileSource(loader),
                          identity_resolver=FileIdentityResolver(lambda _dev: "fs"))
    assert runtime.start().available
    loader.queue(0, events=[replace(event(inode=i + 1, amount=i + 10),
        basename=f"alpha-{i:02}".encode(), name_len=8) for i in range(24)])
    runtime.consume(*frames.tick(0))
    runtime.consume(*frames.tick(FILE_HOUR_NS, amount=200))
    harness = Harness()
    history = HistoryRouter(QueryService(frames.db), harness.client, AppRegistry())
    harness.client._on_gap = history.record_transport_gap
    harness.client.pump()
    window, coordinator, _, setup = controlled_build(qtbot, history=history)
    window._timer.stop()
    window._poll_timer.stop()
    window._clock_ns = lambda: FILE_HOUR_NS - 1
    monkeypatch.setattr(window, "_submit_probe", lambda **_kwargs: False)
    target = Window("since_boot", "boot-A", end_mono_ns=FILE_HOUR_NS)
    window.show_window(target)
    history._local_future.result(timeout=5)
    assert window.apply_ready_frame()
    view = window.files_view
    qtbot.waitUntil(lambda: view.last_result is not None, timeout=5000)
    window.open_files_unavailable()
    view.set_sort("bytes_r", "asc")
    view.search_field.setText("alpha")
    qtbot.waitUntil(lambda: not view._outstanding, timeout=5000)
    assert view.model.rowCount() == 24
    view.table.setFixedHeight(100)
    qtbot.wait(10)
    view.table.verticalScrollBar().setValue(view.table.verticalScrollBar().maximum())
    assert view.table.verticalScrollBar().value() > 0
    owner = cw.CollectorCoordinator(local_factory=lambda: pytest.fail("unexpected writer"),
        client=harness.client, history=history, lease=cw.GuiLease(tmp_path / "owner.lock"),
        clock=lambda: (FILE_HOUR_NS, 1000 * 10**9 + FILE_HOUR_NS))
    shell = SimpleNamespace(window=window, coordinator=coordinator, history=history,
        view=view, harness=harness, frames=frames, owner=owner, target=target, path=path)
    try:
        yield shell
    finally:
        window._timer.stop()
        window._poll_timer.stop()
        view.retire_deliveries()
        window._probe_executor.shutdown(wait=False, cancel_futures=True)
        owner.close()
        history.close()
        for peer in harness.servers:
            peer.close()
        frames.db.close()


class HeldFileDelivery:
    """Hold a real file worker, capture only its bridge, replay real handler."""

    def __init__(self, shell, monkeypatch):
        self.shell = shell
        self.release = threading.Event()
        self.started = threading.Event()
        self.delivered = threading.Event()
        self.payloads = []
        self.transient_at_settlement = []
        history, view = shell.history, shell.view
        read = history._read_local_files

        def held(*args):
            self.started.set()
            assert self.release.wait(5), "file worker not released"
            return read(*args)

        def capture(generation, future):
            self.transient_at_settlement.append(view._transient)
            self.payloads.append((generation, future))
            self.delivered.set()

        monkeypatch.setattr(history, "_read_local_files", held)
        monkeypatch.setattr(view, "_deliver", capture)
        view.request_refresh()
        assert self.started.wait(1)
        self.future = next(iter(history._file_jobs))
        self.generation = view._generation
        self.history_generation = history._generation
        self.requests = len(view.requests)
        self.snapshot = files_snapshot(view)

    def win_cancellation(self, monkeypatch, *, synchronous=False):
        history, view = self.shell.history, self.shell.view
        cancel = history._cancel_file_requests

        def after_generation_bump():
            assert history._generation > self.history_generation
            self.release.set()
            assert self.delivered.wait(2), "worker did not settle before cancellation"
            if synchronous:
                view._on_delivered(self.payloads[0])
            cancel()

        monkeypatch.setattr(history, "_cancel_file_requests", after_generation_bump)

    def replay(self):
        assert self.delivered.wait(2)
        self.shell.view._on_delivered(self.payloads[0])


def invalidate_shell(shell, route, monkeypatch, *, defer_tick=True):
    window, history, coordinator = shell.window, shell.history, shell.coordinator
    if defer_tick and route not in ("clock_prepare", "explicit_prepare"):
        monkeypatch.setattr(window, "_tick_target", lambda: None)
    if route in ("pump", "healthy_pump"):
        if route == "pump":
            from appio.ipc_models import StreamCursor
            from tests.p3.test_iodclient import gap_frame
            shell.harness.send(gap_frame(previous=None,
                current=StreamCursor("boot-A", shell.frames.epoch, 1), start=0, end=1))
            shell.harness.eof()
        window._pump = shell.harness.client.pump
        window.refresh()
    elif route in ("reconcile", "tick_local", "healthy_tick"):
        if route != "healthy_tick":
            coordinator.invalidate_on = route
        window._probe_ticket += 1
        window._apply_facts(cw.ProbePayload(OFF, None, window._probe_ticket,
                                          window._setup_generation))
    elif route == "quiesce":
        coordinator.invalidate_on = "reconcile"
        assert window._quiesce_local()
    elif route == "import":
        monkeypatch.setattr(coordinator, "accept_export", shell.owner.accept_export)
        frames = shell.frames
        window._after_disable(ok("disable", {"export_path": str(shell.path),
            "export_cutoff": {"boot_id": frames.boot_id, "iod_epoch": frames.epoch,
                              "seq": frames.seq,
                              "cutoff_mono_ns": frames.state.checkpoint_mono_ns}}))
        assert len(shell.owner._exports) == 1, window.error_label.text()
        assert len(history._sources) == 2
    elif route == "clock_prepare":
        shell.target = Window("since_boot", "boot-A",
            end_mono_ns=shell.target.end_mono_ns + RAW_BUCKET_NS)
        window._clock_ns = lambda: shell.target.end_mono_ns - 1
        window._tick_target()
    elif route == "explicit_prepare":
        window.show_window(shell.target)
    else:
        raise AssertionError(route)


def apply_replacement(shell):
    window, history, view = shell.window, shell.history, shell.view
    if window._pending is None:
        # Call the class driver because the default-invalidator tests defer the
        # instance driver until after their pre-next-tick delivery observation.
        CollectorWindow._tick_target(window)
    assert window._pending == shell.target
    history._local_future.result(timeout=5)
    assert window.apply_ready_frame()
    assert not view._transient and view._scope_ready


def test_pump_transport_gap_preserves_files_through_replacement(real_files_shell, monkeypatch):
    shell = real_files_shell
    held = HeldFileDelivery(shell, monkeypatch)
    try:
        held.win_cancellation(monkeypatch, synchronous=True)
        invalidate_shell(shell, "pump", monkeypatch)
        assert shell.history.transport_issues
        assert files_snapshot(shell.view) == held.snapshot
        assert len(shell.view.requests) == held.requests
        apply_replacement(shell)
        assert len(shell.view.requests) == held.requests + 1
    finally:
        held.release.set()


def test_disable_export_import_preserves_files_through_add_source(real_files_shell, monkeypatch):
    shell = real_files_shell
    held = HeldFileDelivery(shell, monkeypatch)
    try:
        held.win_cancellation(monkeypatch, synchronous=True)
        invalidate_shell(shell, "import", monkeypatch)
        assert files_snapshot(shell.view) == held.snapshot
        assert len(shell.view.requests) == held.requests
        apply_replacement(shell)
        assert len(shell.view.requests) == held.requests + 1
    finally:
        held.release.set()


@pytest.mark.parametrize("route", ["pump", "reconcile", "tick_local", "quiesce", "import",
                                    "clock_prepare", "explicit_prepare"])
def test_worker_wins_cancellation_delivery_is_transient_while_pending(
    real_files_shell, monkeypatch, route
):
    shell = real_files_shell
    held = HeldFileDelivery(shell, monkeypatch)
    try:
        held.win_cancellation(monkeypatch, synchronous=True)
        invalidate_shell(shell, route, monkeypatch)
        assert held.transient_at_settlement == [True]
        assert files_snapshot(shell.view) == held.snapshot
        assert shell.view.refresh_wanted()
        assert len(shell.view.requests) == held.requests
        apply_replacement(shell)
        assert files_snapshot(shell.view) == held.snapshot
        assert len(shell.view.requests) == held.requests + 1
    finally:
        held.release.set()


@pytest.mark.parametrize("route", ["pump", "reconcile", "tick_local", "quiesce", "import",
    "clock_prepare", "explicit_prepare", "healthy_pump", "healthy_tick"])
def test_transient_scope_exit_retires_worker_delivery_before_next_target_tick(
    real_files_shell, monkeypatch, route
):
    shell = real_files_shell
    held = HeldFileDelivery(shell, monkeypatch)
    try:
        if route.startswith("healthy"):
            invalidate_shell(shell, route, monkeypatch)
            assert shell.view._generation == held.generation
            assert not shell.view.refresh_wanted()
            assert shell.view._outstanding
            assert len(shell.view.requests) == held.requests
            held.release.set()
            held.replay()
            assert not shell.view._outstanding
            assert len(shell.view.requests) == held.requests
            return
        held.win_cancellation(monkeypatch)
        invalidate_shell(shell, route, monkeypatch)
        assert not shell.view._transient
        if route not in ("clock_prepare", "explicit_prepare"):
            assert shell.view._scope_ready  # SR-1: readiness has not closed yet.
            assert shell.window._pending is None
        assert shell.view._generation > held.generation
        held.replay()
        assert files_snapshot(shell.view) == held.snapshot
        assert shell.view.refresh_wanted()
        assert len(shell.view.requests) == held.requests
        apply_replacement(shell)
        assert files_snapshot(shell.view) == held.snapshot
        assert len(shell.view.requests) == held.requests + 1
    finally:
        held.release.set()


def test_late_retired_file_delivery_after_ready_is_dropped_and_requeried_once(
    real_files_shell, monkeypatch
):
    shell = real_files_shell
    held = HeldFileDelivery(shell, monkeypatch)
    try:
        held.win_cancellation(monkeypatch)
        invalidate_shell(shell, "explicit_prepare", monkeypatch)
        # Delivery carrying the pre-apply Files generation must already be
        # retired when readiness is restored, even if delivered reentrantly.
        failure = Future()
        failure.set_exception(WindowUnavailableError("file_source_unavailable", "old scope"))
        before_apply = shell.view._generation
        restore = shell.view.set_scope_ready

        def reentrant_ready(ready):
            restore(ready)
            if ready:
                shell.view._on_delivered((before_apply, failure))

        monkeypatch.setattr(shell.view, "set_scope_ready", reentrant_ready)
        apply_replacement(shell)
        assert files_snapshot(shell.view) == held.snapshot
        assert len(shell.view.requests) == held.requests + 1
        newest = shell.view.last_request
        assert shell.view._outstanding
        held.replay()
        assert shell.view._outstanding
        assert shell.view.last_request is newest
        assert not shell.view.refresh_wanted()
        assert files_snapshot(shell.view) == held.snapshot
        shell.view.flush_refresh()
        assert len(shell.view.requests) == held.requests + 1
    finally:
        held.release.set()


# Task 4 review regressions. Each M-number names the review ledger mutation
# independently falsified by that test; all entry points use the real shell.


def test_task4_m006_closed_target_tick_is_inert(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window.show_window(scope_window())
    window.close()
    before = (window._displayed, window._pending, tuple(history.prepare_calls))
    clocks = []
    monkeypatch.setattr(window, "_current_window", lambda: clocks.append(True) or None)
    window._tick_target()
    assert clocks == []
    assert (window._displayed, window._pending, tuple(history.prepare_calls)) == before


def test_task4_m017_closed_apply_refuses_ready_pending_frame(qtbot):
    window, _, history, _ = controlled_build(qtbot)
    target = scope_window()
    window.show_window(target)
    history.become_ready(target)
    window.close()
    assert window.apply_ready_frame() is False
    assert window._displayed is None and window._pending == target


def test_task4_m021_closed_explicit_request_is_inert(qtbot):
    window, _, history, _ = controlled_build(qtbot)
    window.close()
    before = (window._displayed, window._pending, tuple(history.prepare_calls))
    window.show_window(scope_window())
    assert (window._displayed, window._pending, tuple(history.prepare_calls)) == before


def test_task4_m031_closed_refresh_never_pumps(qtbot):
    pumps = []
    window, _, history, _ = controlled_build(qtbot, pump=lambda: pumps.append(True))
    window.close()
    window.refresh()
    assert pumps == []
    assert history.prepare_calls == []
    assert window._probe_ticket == 0


def test_task4_m078_close_enters_shutdown_and_stops_both_timers(qtbot, monkeypatch):
    window, coordinator, history, _ = controlled_build(qtbot)
    closed = []
    monkeypatch.setattr(coordinator, "close", lambda: closed.append("coordinator"))
    monkeypatch.setattr(history, "close", lambda: closed.append("history"))
    window.show_window(scope_window())
    assert window._timer.isActive() and window._poll_timer.isActive()
    try:
        assert window.close()
        assert window._closed
        assert not window._timer.isActive() and not window._poll_timer.isActive()
        window.shutdown()
        assert closed == ["coordinator", "history"]
    finally:
        window._timer.stop()
        window._poll_timer.stop()


def test_task4_m081_close_cancels_queued_probe_without_waiting(qtbot):
    setup = ControlledSetup(OFF, hold=True)
    window, _, _, _ = controlled_build(qtbot, setup=setup)
    window._timer.stop()
    try:
        assert window._submit_probe()
        assert setup.started.wait(1)
        running = window._probe_inflight
        assert window._submit_probe(replace_pending=True)
        successor = window._probe_inflight
        assert successor is window._probe_successor and not successor.running()
        started = time.monotonic()
        window.close()
        assert time.monotonic() - started < 0.1
        assert successor.cancelled()
        setup.release.set()
        running.result(timeout=2)
        assert setup.refresh_calls == 1
        qtbot.waitUntil(lambda: window._probe_executor._threads.isdisjoint(threading.enumerate()))
        assert window.facts_applied == 0 and window._dropped_facts == 0
    finally:
        setup.release.set()
        window.shutdown()


def test_task4_closed_shell_refuses_helpers(qtbot):
    window, _, _, setup = controlled_build(qtbot)
    window.shutdown()
    generation = window._setup_generation
    window._start("status", None)
    assert setup.calls == []
    assert window._setup_generation == generation and not window.busy


def test_task4_m079_shutdown_retires_then_closes_files_before_resources(qtbot, monkeypatch):
    window, coordinator, history, _ = populated_shell(qtbot, monkeypatch)
    view = window.files_view
    events = []
    retire = view.retire_deliveries
    close = view.close

    def retire_files():
        events.append("retire")
        retire()

    def close_files():
        events.append("files")
        return close()

    def close_history():
        assert view._closed
        events.append("history")

    monkeypatch.setattr(view, "retire_deliveries", retire_files)
    monkeypatch.setattr(view, "close", close_files)
    monkeypatch.setattr(coordinator, "close", lambda: events.append("coordinator"))
    monkeypatch.setattr(history, "close", close_history)
    window.shutdown()
    assert events == ["retire", "files", "coordinator", "history"]
    reads = (history.ranking_reads, history.file_application_reads, history.status_reads)
    requests = len(history.file_requests)
    view.request_refresh()
    view.flush_refresh()
    view.set_window(scope_window())
    assert len(history.file_requests) == requests
    assert (history.ranking_reads, history.file_application_reads, history.status_reads) == reads
    window.shutdown()
    assert events == ["retire", "files", "coordinator", "history"]


def test_task4_stable_target_flushes_without_reloading_union(qtbot, monkeypatch):
    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    reads = (history.ranking_reads, history.file_application_reads, history.status_reads)
    requests = len(history.file_requests)
    prepares = tuple(history.prepare_calls)
    for _ in range(2):
        window._tick_target()
    assert (history.ranking_reads, history.file_application_reads, history.status_reads) == reads
    assert len(history.file_requests) == requests
    assert tuple(history.prepare_calls) == prepares
    view = window.files_view
    view.set_scope_ready(False)
    view.request_refresh()
    assert view.refresh_wanted()
    window._tick_target()
    assert len(history.file_requests) == requests + 1
    assert not view.refresh_wanted()
    assert (history.ranking_reads, history.file_application_reads) == reads[:2]
    window._tick_target()
    assert len(history.file_requests) == requests + 1
    assert tuple(history.prepare_calls) == prepares


@pytest.mark.parametrize("source", ["unavailable", "available", "raises"])
def test_task4_no_scope_files_message_waits_for_first_sample(qtbot, monkeypatch, source):
    history = ControlledHistory(boot_id=None)
    if source == "available":
        history.file_status = FileAvailability(True, True)
    window, _, _, _ = controlled_build(qtbot, history=history)
    window._timer.stop()
    reads = history.status_reads
    if source == "raises":
        def fail_status():
            raise RuntimeError("status unavailable")
        monkeypatch.setattr(history, "file_source_status", fail_status)
    window._tick_target()
    assert window._target is window._pending is window._displayed is None
    assert window._files_status_text() == "waiting for the first sample"
    assert window.files_message.text() == "waiting for the first sample"
    assert history.status_reads == reads


def test_task4_m008_advancing_target_keeps_nonretired_pending_scope(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    pending, target = scope_window(), scope_window(END + RAW_BUCKET_NS)
    window.show_window(pending)
    monkeypatch.setattr(window, "_current_window", lambda: target)
    calls = tuple(history.prepare_calls)
    window._tick_target()
    assert window._target == target and window._pending == pending
    assert tuple(history.prepare_calls) == calls
    assert window._displayed is None


def test_task4_m012_explicit_retry_survives_nonempty_clock_target(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    history.prepare_failures.append(RuntimeError("explicit failure"))
    with pytest.raises(RuntimeError, match="explicit failure"):
        window.show_window(scope_window())
    marker = window._prepare_retry
    calls = tuple(history.prepare_calls)
    monkeypatch.setattr(window, "_current_window", lambda: scope_window(END + RAW_BUCKET_NS))
    window._tick_target()
    assert window._prepare_retry is marker and marker.owner == "explicit"
    assert window._pending is None and not window.files_view._scope_ready
    assert tuple(history.prepare_calls) == calls


def test_task4_m016_validation_error_before_loading_propagates(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    history.view_state = "ready"

    def invalid(_window):
        raise ValueError("invalid window")

    monkeypatch.setattr(history, "prepare", invalid)
    with pytest.raises(ValueError, match="invalid window"):
        window._tick_target()
    assert window._prepare_retry is None and window._prepare_error is None


def test_task4_m019_apply_discards_retired_pending_work(qtbot):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    window.show_window(scope_window())
    history.invalidate()
    assert window.apply_ready_frame() is False
    assert window._pending is None and window._displayed is None


def test_task4_m022_explicit_scope_leaves_stored_clock_target(qtbot):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()
    window._target = scope_window()
    window.show_window(scope_window(END + RAW_BUCKET_NS))
    assert window._target == scope_window()
    assert window._pending == scope_window(END + RAW_BUCKET_NS)


def test_task4_m098_target_preparation_never_renders_ready(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    renders = []
    monkeypatch.setattr(window, "_show_ready", lambda target: renders.append(target))
    window._tick_target()
    assert renders == []
    assert window._displayed is None and window._pending == scope_window()
    assert history.prepare_calls == [scope_window()]


def test_task4_m100_explicit_request_clears_retry_before_prepare(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    history.prepare_failures.append(RuntimeError("prepare failed"))
    with pytest.raises(RuntimeError, match="prepare failed"):
        window.show_window(scope_window())
    assert window._prepare_retry is not None and window._prepare_error is not None
    observed = []
    prepare = history.prepare

    def checked(target):
        observed.append((window._prepare_retry, window._prepare_error))
        prepare(target)

    monkeypatch.setattr(history, "prepare", checked)
    window.show_window(scope_window(END + RAW_BUCKET_NS))
    assert observed == [(None, None)]
    assert window._pending == scope_window(END + RAW_BUCKET_NS)


def test_task4_m029_frame_application_stops_readiness_poll(qtbot):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    window.show_window(scope_window())
    history.become_ready(scope_window())
    assert window._poll_timer.isActive()
    assert window.apply_ready_frame()
    assert not window._poll_timer.isActive()


def test_task4_m030_poll_never_ticks_clock_target(qtbot, monkeypatch):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()
    window.show_window(scope_window())
    ticks = []
    monkeypatch.setattr(window, "_tick_target", lambda: ticks.append(True))
    window._poll_history()
    assert ticks == []
    assert window._pending == scope_window() and window._displayed is None


def test_task4_m034_refresh_pumps_then_ticks_then_applies_then_probes(qtbot, monkeypatch):
    events = []
    window, _, _, _ = controlled_build(qtbot, pump=lambda: events.append("pump"))
    window._timer.stop()
    monkeypatch.setattr(window, "_tick_target", lambda: events.append("tick"))
    monkeypatch.setattr(window, "apply_ready_frame", lambda: events.append("apply") or False)
    monkeypatch.setattr(window, "_submit_probe", lambda: events.append("probe") or False)
    window.refresh()
    assert events == ["pump", "tick", "apply", "probe"]


def task4_deferred_same_scope(qtbot, monkeypatch):
    window, coordinator, history, setup = populated_shell(qtbot, monkeypatch)
    # Isolate each caller's no-apply flush from the target driver's own flush.
    monkeypatch.setattr(window, "_tick_target", lambda: None)
    view = window.files_view
    view.set_scope_ready(False)
    view.request_refresh()
    view.set_scope_ready(True)
    assert view.refresh_wanted()
    return window, coordinator, history, setup


def test_task4_m035_refresh_flushes_deferred_files_without_new_frame(qtbot, monkeypatch):
    window, _, history, _ = task4_deferred_same_scope(qtbot, monkeypatch)
    requests = len(history.file_requests)
    window.refresh()
    assert len(history.file_requests) == requests + 1
    assert not window.files_view.refresh_wanted()
    window.refresh()
    assert len(history.file_requests) == requests + 1


def test_task4_m036_refresh_contains_operational_errors(qtbot, monkeypatch):
    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    displayed = window._displayed
    snapshot = files_snapshot(window.files_view)

    def fail():
        raise RuntimeError("pump failed")

    monkeypatch.setattr(window, "_pump", fail)
    window.refresh()
    assert window.error_label.text() == "appio: pump failed"
    assert window._displayed == displayed and files_snapshot(window.files_view) == snapshot
    assert history.prepare_calls == [displayed]


def test_task4_m045_facts_flush_deferred_files_without_new_frame(qtbot, monkeypatch):
    window, _, history, _ = task4_deferred_same_scope(qtbot, monkeypatch)
    requests = len(history.file_requests)
    window._probe_ticket = 1
    window._apply_facts(cw.ProbePayload(OFF, None, 1, 0))
    assert len(history.file_requests) == requests + 1
    assert not window.files_view.refresh_wanted()
    window._probe_ticket = 2
    window._apply_facts(cw.ProbePayload(OFF, None, 2, 0))
    assert len(history.file_requests) == requests + 1


def test_task4_m097_poll_applies_replacement_while_old_frame_visible(qtbot, monkeypatch):
    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    first = window._displayed
    target = scope_window(END + RAW_BUCKET_NS)
    window.show_window(target)
    assert window._displayed == first and window.history_visible
    history.become_ready(target)
    window._poll_history()
    assert window._displayed == target and window._pending is None
    assert not window._poll_timer.isActive()


def task4_rejected_probe(qtbot, *, busy, generation):
    window, coordinator, history, _ = controlled_build(qtbot)
    window._timer.stop()
    window._probe_ticket = 7
    window._setup_generation = 9
    window._busy = busy
    window._probe_inflight = window._probe_successor = Future()
    before = (window.facts_applied, window._facts, window.state_text,
              tuple(coordinator.actions), tuple(history.prepare_calls))
    window._apply_facts(cw.ProbePayload(OFF, None, 7, generation))
    assert (window.facts_applied, window._facts, window.state_text,
            tuple(coordinator.actions), tuple(history.prepare_calls)) == before
    assert window._dropped_facts == 1
    assert window._probe_inflight is None and window._probe_successor is None


def test_task4_m065_generation_rejection_with_current_ticket_and_idle_shell(qtbot):
    task4_rejected_probe(qtbot, busy=False, generation=8)


def test_task4_m066_busy_rejection_with_current_ticket_and_generation(qtbot):
    task4_rejected_probe(qtbot, busy=True, generation=9)


def task4_completion_edges(qtbot, monkeypatch, *, synchronous):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()
    window._busy = True
    window._setup_generation = 3
    window._operation = ("status", None)
    events = []
    original = cw.CollectorWindow.__setattr__

    def record(self, name, value):
        if self is window and name in ("_busy", "_setup_generation"):
            events.append((name, value))
        return original(self, name, value)

    monkeypatch.setattr(cw.CollectorWindow, "__setattr__", record)
    monkeypatch.setattr(window, "_submit_probe", lambda **_kwargs: False)
    if synchronous:
        window._complete_start_failure(RuntimeError("helper failed"))
    else:
        future = Future()
        future.set_exception(RuntimeError("helper failed"))
        window._on_reply(future)
    assert events == [("_setup_generation", 4), ("_busy", False)]


def test_task4_m073_reply_advances_generation_before_clearing_busy(qtbot, monkeypatch):
    task4_completion_edges(qtbot, monkeypatch, synchronous=False)


def test_task4_m074_invoke_failure_advances_generation_before_clearing_busy(qtbot, monkeypatch):
    task4_completion_edges(qtbot, monkeypatch, synchronous=True)


def test_task4_m061_already_complete_probe_waits_for_queued_delivery(qtbot, monkeypatch):
    window, coordinator, _, _ = controlled_build(qtbot)
    window._timer.stop()

    def submit(*_args, **_kwargs):
        future = Future()
        future.set_result(OFF)
        return future

    monkeypatch.setattr(window._probe_executor, "submit", submit)
    assert window._submit_probe()
    assert window.facts_applied == 0 and coordinator.actions == []
    assert window._probe_inflight is not None and window._probe_inflight.done()
    assert window._submit_probe() is False
    qtbot.waitUntil(lambda: window.facts_applied == 1)
    assert window._probe_inflight is None


def task4_connection_promotion(qtbot, *, verified):
    from dataclasses import replace

    window, coordinator, history, _ = controlled_build(qtbot)
    window._timer.stop()
    facts = replace(OFF, enabled=True, daemon_verified=False,
                    answering=False, pending_activation=False)
    history.client = SimpleNamespace(state=SimpleNamespace(
        verified=verified, answering=True, pending_activation=True, cursor=None))
    expected = (replace(facts, daemon_verified=True, answering=True, pending_activation=True)
                if verified else facts)
    window._probe_ticket = 1
    window._apply_facts(cw.ProbePayload(facts, None, 1, 0))
    assert window._facts == expected
    assert coordinator.reconciles == [expected]
    assert window.facts_applied == 1


def test_task4_m037_unverified_answer_cannot_promote_probe(qtbot):
    task4_connection_promotion(qtbot, verified=False)


def test_task4_m038_verified_answer_promotes_silent_probe(qtbot):
    task4_connection_promotion(qtbot, verified=True)


def test_task4_m039_connection_promotion_preserves_pending_activation(qtbot):
    task4_connection_promotion(qtbot, verified=True)


def test_task4_m044_reconcile_failure_preserves_facts_banner_and_frame(qtbot, monkeypatch):
    window, coordinator, _, _ = populated_shell(qtbot, monkeypatch)
    window._facts = ON
    window._render(ON)
    before = (window.facts_applied, window._facts, window.state_text, window._displayed)
    snapshot = files_snapshot(window.files_view)

    def fail(_facts):
        raise RuntimeError("reconcile failed")

    monkeypatch.setattr(coordinator, "reconcile", fail)
    window._probe_ticket = 1
    window._apply_facts(cw.ProbePayload(OFF, None, 1, 0))
    assert (window.facts_applied, window._facts, window.state_text, window._displayed) == before
    assert files_snapshot(window.files_view) == snapshot
    assert window.error_label.text() == "appio: reconcile failed"


def test_task4_m099_unchanged_pending_facts_skip_reconcile_outside_loading(qtbot):
    window, coordinator, history, _ = controlled_build(qtbot)
    window._timer.stop()
    window.show_window(scope_window())
    history.view_state = "ready"
    window._facts = OFF
    window._probe_ticket = 1
    window._apply_facts(cw.ProbePayload(OFF, None, 1, 0))
    assert coordinator.reconciles == [] and coordinator.ticks == 1
    assert window._pending == scope_window()
    assert window.facts_applied == 1


def test_task4_m057_busy_shell_without_slot_refuses_probe_submit(qtbot, monkeypatch):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()
    submissions = []
    monkeypatch.setattr(window._probe_executor, "submit",
                        lambda *_args, **_kwargs: submissions.append(True) or Future())
    window._busy = True
    assert window._probe_inflight is None
    assert window._submit_probe() is False
    assert submissions == [] and window._probe_ticket == 0


def test_task4_m059_executor_submission_failure_is_visible_and_releases_slot(qtbot, monkeypatch):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()

    def fail(*_args, **_kwargs):
        raise RuntimeError("submit failed")

    monkeypatch.setattr(window._probe_executor, "submit", fail)
    assert window._submit_probe() is False
    assert window.error_label.text() == "appio: submit failed"
    assert window._probe_inflight is None and window._probe_successor is None


def test_task4_m062_blank_probe_exception_uses_class_name(qtbot):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()
    window._probe_ticket = 1
    future = Future()
    future.set_exception(RuntimeError())
    window._probe_completed(1, 0, future)
    qtbot.waitUntil(lambda: window.error_label.text() == "appio: RuntimeError")
    assert window.facts_applied == 0


def test_task4_m070_error_payload_never_counts_as_applied_facts(qtbot):
    window, coordinator, _, _ = controlled_build(qtbot)
    window._timer.stop()
    window._probe_ticket = 1
    window._probe_inflight = window._probe_successor = Future()
    window._apply_facts(cw.ProbePayload(None, "probe failed", 1, 0))
    assert window.facts_applied == 0 and coordinator.actions == []
    assert window._probe_inflight is None and window._probe_successor is None
    assert window.error_label.text() == "appio: probe failed"


def test_task4_m004_prepare_retry_is_immutable():
    from dataclasses import FrozenInstanceError

    retry = cw.PrepareRetry(scope_window(), "clock", 1)
    with pytest.raises(FrozenInstanceError):
        retry.since = 2


def test_task4_m005_clock_retry_captures_current_timestamp(qtbot, monkeypatch):
    window, _, history, _ = controlled_build(qtbot)
    window._timer.stop()
    monkeypatch.setattr(window, "_clock_ns", lambda: 1234567)
    history.prepare_failures.append(RuntimeError("clock failure"))
    window._tick_target()
    assert window._prepare_retry.since == 1234567
    assert window._prepare_retry.owner == "clock"


def test_task4_m050_probe_payload_is_immutable():
    from dataclasses import FrozenInstanceError

    payload = cw.ProbePayload(OFF, None, 1, 0)
    with pytest.raises(FrozenInstanceError):
        payload.ticket = 2


def test_task4_m101_prepared_observation_is_getter_only(qtbot):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()
    with pytest.raises(AttributeError):
        window._prepared = scope_window()
    assert window._displayed is None


def test_task4_m102_facts_applied_observation_is_getter_only(qtbot):
    window, _, _, _ = controlled_build(qtbot)
    window._timer.stop()
    with pytest.raises(AttributeError):
        window.facts_applied = 12
    assert window.facts_applied == 0


def test_task4_m089_invalid_file_result_preserves_inner_failure_reason(qtbot, monkeypatch):
    from dataclasses import replace

    window, _, history, _ = populated_shell(qtbot, monkeypatch)
    result = history.file_result
    inner = replace(result.coverage.coverage, complete=False, unavailable_reason="not_retained")
    result = replace(result, coverage=replace(result.coverage, coverage=inner))
    classified = []
    handle = window.files_view._handle_failure

    def classify(reason, message):
        classified.append((reason, message))
        handle(reason, message)

    monkeypatch.setattr(window.files_view, "_handle_failure", classify)
    window.files_view._publish(result)
    assert classified == [("not_retained", "file history is not available for this window")]
    assert window.files_view.last_result is None
    assert window.files_view.model.rowCount() == 0
    assert window.files_view.unavailable_text() == (
        "not_retained: file history is not available for this window"
    )
