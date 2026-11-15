"""History selection uses durable intervals, never matching local surrogate IDs."""
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
import sqlite3
import threading

import pytest

from appio.contracts import HistoryPending, Window, WindowUnavailableError
from appio.handover import HandoverOffer, baseline_seed, capture_handover
from appio.history_router import DaemonAnswerError, HistoryRouter, HistorySegment
from appio.iodclient import AppRegistry, ClientState
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.query import QueryService
from appio.sampler import TickInput, aggregate_tick
from appio.store import AppIoStore

NS = 1_000_000_000
BOOT = "boot"


class Client:
    state = ClientState()

    def __init__(self):
        self.pending = []

    def query(self, request):
        future = Future()
        self.pending.append((request, future))
        return future


def source(path, owner="gui", *, boot=BOOT, app_id=17, key="/usr/bin/shared",
           start=100, end=110, amount=20, epoch=1):
    store = AppIoStore(path)
    store.start_boot(boot, 1, 0, 1000.0, owner)
    store.connection.execute("INSERT INTO app VALUES (?,?,?,NULL,0,0,'user')",
                             (app_id, "exe:" + key, key))
    store.connection.commit()
    store.start_collector(owner, boot, start * NS)
    state = None
    for seq, at, counters in ((0, start, (100, 100, 100, 100)),
                              (1, end, (100+amount, 100+amount, 100+amount, 100+amount))):
        proc = StableProcRecord(ProcessIdentity(boot, 410, 100), 1, 1000, key, "/fixture",
            AppKeyInputs("/fixture", key), IoReadOutcome("readable", counters, None, None))
        observation = TickInput(boot, owner, epoch, seq, at * NS, (1000+at)*NS, (proc,), ())
        frame, state = baseline_seed(observation) if seq == 0 else aggregate_tick(state, observation)
        store.record_tick(frame, state)
    return store, state


def router(store):
    return HistoryRouter(QueryService(store), Client(), AppRegistry())


def window(start=100, end=140, boot=BOOT):
    return Window("range", boot, start*NS, end*NS)


def result(router, scope):
    router.prepare(scope)
    work = getattr(router, "_local_future", None)
    if work is not None:
        work.result(timeout=10)
    assert router.ready(scope)
    return router.ranking(scope, "total_bytes")


def test_local_settled_totals_remain_answerable_and_ids_are_presentational(tmp_path):
    store, _ = source(tmp_path / "gui.db")
    history = router(store)
    rows = result(history, Window("lifetime"))
    assert [(row.app_key, row.read_bytes, row.write_bytes) for row in rows] == [
        ("exe:/usr/bin/shared", 20, 20)]
    assert rows[0].app_id != 17
    assert history.registry.key_for(rows[0].app_id) == rows[0].app_key


def test_adjacent_sources_merge_by_appkey_not_database_ids(tmp_path):
    local, _ = source(tmp_path / "local.db", app_id=17)
    root, _ = source(tmp_path / "root.db", "iod", app_id=3, start=110, end=130, amount=30)
    history = router(local)
    history.add_source(QueryService(root), collector_id="export", owner="iod")
    rows = result(history, window())
    assert len(rows) == 1
    assert (rows[0].rchar, rows[0].wchar, rows[0].read_bytes, rows[0].write_bytes) == (50, 50, 50, 50)
    assert [(s.owner, s.start_ns, s.end_ns, s.proven) for s in history.segments][:2] == [
        ("gui", 100*NS, 110*NS, True), ("iod", 110*NS, 130*NS, True)]
    assert history.boundaries == ((BOOT, 110*NS, "gui", "iod"),)
    assert rows[0].coverage.gap_intervals == 1  # [130,140) is not proved
    assert not rows[0].coverage.complete


def test_same_surrogate_in_two_stores_is_not_same_app(tmp_path):
    local, _ = source(tmp_path / "local.db", app_id=3, key="/bin/first")
    root, _ = source(tmp_path / "root.db", "iod", app_id=3, key="/bin/second", start=110, end=130)
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    rows = result(history, window())
    assert {row.app_key for row in rows} == {"exe:/bin/first", "exe:/bin/second"}
    assert len({row.app_id for row in rows}) == 2
    for row in rows:
        share = history.disk_share(row.app_id, window())
        assert share.task_read_bytes == 20
        assert share.device_read_bytes is share.residual_read is None
        series = history.series(row.app_id, window())
        assert sum(point.read_bytes for point in series.points) == 20


def test_overlap_without_handover_is_a_gap_not_an_owner_guess(tmp_path):
    local, _ = source(tmp_path / "local.db", start=100, end=120)
    root, _ = source(tmp_path / "root.db", "iod", start=110, end=130)
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    rows = result(history, window())
    assert sum(row.read_bytes for row in rows) == 0  # both indivisible deltas straddle uncertainty
    assert any(not segment.proven and segment.start_ns == 110*NS for segment in history.segments)
    assert all(row.coverage.overlap_uncertain_intervals > 0 for row in rows)
    assert local.connection.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 20


def test_duplicate_exports_do_not_duplicate_receipts(tmp_path):
    local, _ = source(tmp_path / "local.db")
    root, _ = source(tmp_path / "root.db", "iod", start=110, end=130)
    history = router(local)
    for name in ("export-a", "export-b"):
        history.add_source(QueryService(root), collector_id=name, owner="iod")
    rows = result(history, window())
    assert sum(row.read_bytes for row in rows) == 40


@pytest.mark.parametrize("outgoing,incoming_owner", [("gui", "iod"), ("iod", "gui")])
def test_recorded_handover_selects_both_sides_and_excludes_late_writer(tmp_path, outgoing, incoming_owner):
    store, state = source(tmp_path / "both.db", outgoing)
    offer = HandoverOffer(outgoing, incoming_owner, state.epoch, state.last_seq, 100*NS, capture_handover(state))
    store.install_handover(offer, wall_ns=1110*NS)
    incoming = store.restore_checkpoint(incoming_owner, BOOT)
    proc = replace(incoming.processes[0])
    record = StableProcRecord(proc.identity, 1, 1000, "/usr/bin/shared", "/fixture",
        AppKeyInputs("/fixture", "/usr/bin/shared"), IoReadOutcome("readable", (150,)*4, None, None))
    for initial, owner in ((incoming, incoming_owner), (state, outgoing)):
        frame, staged = aggregate_tick(initial, TickInput(BOOT, owner, 1, initial.last_seq+1,
            130*NS, 1130*NS, (record,), ()))
        store.record_tick(frame, staged)
    history = router(store)
    rows = result(history, window())
    assert sum(row.read_bytes for row in rows) == 50
    assert history.boundaries == ((BOOT, 110*NS, outgoing, incoming_owner),)


def test_unproved_samples_cannot_be_served_as_measured_bytes(tmp_path):
    store, _ = source(tmp_path / "local.db")
    store.connection.execute("DELETE FROM collector_interval")
    store.connection.commit()
    history = router(store)
    rows = result(history, window())
    assert sum(row.read_bytes for row in rows) == 0
    assert history.segments and not any(s.proven for s in history.segments)


def test_lifetime_bytes_without_any_ownership_interval_are_not_reported_ready(tmp_path):
    local, _ = source(tmp_path / "legacy.db")
    local.connection.execute("DELETE FROM collector_interval")
    local.connection.commit()
    history = router(local)
    rows = result(history, Window("lifetime"))
    assert "not_retained" in history.view_state
    assert all(not row.coverage.complete and row.coverage.unavailable_reason == "not_retained" for row in rows)
    assert local.connection.execute("SELECT read_bytes FROM app_lifetime_total").fetchone()[0] == 20


def test_old_mixed_totals_without_raw_corrections_are_unavailable(tmp_path):
    local, _ = source(tmp_path / "local.db", end=120)
    root, _ = source(tmp_path / "root.db", "iod", start=110, end=130)
    local.connection.execute("DELETE FROM sample")
    local.connection.commit()
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    result(history, Window("lifetime"))
    assert "not_retained" in history.view_state
    assert not any(s.proven and s.collector_id == "local" for s in history.segments)


def test_a_frame_prefetches_only_its_default_subject_and_loads_the_rest_on_demand(tmp_path):
    """Falsifier: the frame prefetched two parts for every ranked app.

    Two answers per app is 400+ round trips for a live boot. That frame outlived
    the daemon's 10 s commit cadence, so the snapshot moved underneath it and the
    whole frame was discarded as "answered a different scope or snapshot". The
    ranking already carries every row's totals, so only the default subject's own
    parts are needed to paint, and any other subject loads when it is selected.
    """

    remote, _ = source(tmp_path / "remote.db", "iod")
    coverage = remote.connection.execute(
        "SELECT coverage_json FROM app_boot_total WHERE app_id=17").fetchone()[0]
    remote.connection.execute(
        "INSERT INTO app VALUES(3,'exe:/usr/bin/other','other',NULL,0,0,'user')")
    remote.connection.execute(
        "INSERT INTO app_boot_total VALUES('boot',3,0,0,0,0,NULL,NULL,NULL,?)", (coverage,))
    remote.connection.commit()
    # A whole-boot scope ranks from the retained totals, so both apps are rows.
    scope = Window("since_boot", BOOT, end_mono_ns=200 * NS)
    service = QueryService(remote)
    expected = service.ranking(scope, "total_bytes")
    assert [row.app_key for row in expected] == ["exe:/usr/bin/shared", "exe:/usr/bin/other"]

    history = router(AppIoStore(tmp_path / "empty.db"))
    history.client.state = ClientState(verified=True, answering=True)
    history.prepare(scope)
    history._local_future.result(timeout=10)
    ranking, ranking_future = history.client.pending[0]
    assert ranking.kind == "ranking"
    ranking_future.set_result({"kind": "ranking", "result": expected, "snapshot": "fixed"})
    assert not history.ready(scope)
    # Exactly one part in flight, and it belongs to the default subject.
    assert [(request.kind, request.app_key) for request, _ in history.client.pending[1:]] == [
        ("totals", "exe:/usr/bin/shared")]

    def answer_next():
        for request, future in reversed(history.client.pending):
            if future.done():
                continue
            app_id = remote.connection.execute(
                "SELECT app_id FROM app WHERE key=?", (request.app_key,)).fetchone()[0]
            method = "disk_share" if request.kind == "totals" else "series"
            future.set_result({"kind": request.kind, "result": getattr(service, method)(app_id, scope),
                               "snapshot": "fixed"})
            return request

    for _ in range(4):
        if history.ready(scope):
            break
        assert answer_next() is not None
    assert history.ready(scope)
    assert [row.app_key for row in history.ranking(scope, "total_bytes")] == [
        "exe:/usr/bin/shared", "exe:/usr/bin/other"]

    # A subject outside the prefetch is requested on demand, never fabricated.
    other = history.registry.id_for("exe:/usr/bin/other")
    with pytest.raises(HistoryPending):
        history.series(other, scope)
    requested, pending = history.client.pending[-1]
    assert (requested.kind, requested.app_key) == ("series", "exe:/usr/bin/other")
    app_id = remote.connection.execute(
        "SELECT app_id FROM app WHERE key='exe:/usr/bin/other'").fetchone()[0]
    pending.set_result({"kind": "series", "result": service.series(app_id, scope), "snapshot": "fixed"})
    assert history.ready(scope)
    assert history.series(other, scope).app_id == other


def test_a_part_binds_its_window_and_never_blanks_the_ranking(tmp_path):
    """Falsifier: a subject's part was treated as evidence about the whole frame.

    A part answering another window (the scope ticked while the operator was
    selecting) raised like a contradictory ranking, dropped every remote row and
    reset the subject to the first app -- which is why only the first entry ever
    showed a detail. The part must be dropped and re-requested; the ranking stands.
    """

    local = AppIoStore(tmp_path / "empty.db")
    remote, _ = source(tmp_path / "remote.db", "iod")
    scope = Window("since_boot", BOOT, end_mono_ns=200 * NS)
    elsewhere = Window("since_boot", BOOT, end_mono_ns=300 * NS)
    service = QueryService(remote)
    expected = service.ranking(scope, "total_bytes")

    def answer(history, *, part_window=None, snapshot="s2"):
        ranking, ranking_future = history.client.pending[0]
        ranking_future.set_result({"kind": "ranking", "result": expected, "snapshot": "s1"})
        for _ in range(6):
            if history.ready(scope):
                return history
            if not history.client.pending:
                continue
            request, future = [item for item in history.client.pending if not item[1].done()][0]
            app_id = remote.connection.execute(
                "SELECT app_id FROM app WHERE key=?", (request.app_key,)).fetchone()[0]
            method = "disk_share" if request.kind == "totals" else "series"
            value = getattr(service, method)(app_id, scope)
            if part_window is not None:
                value = replace(value, window=part_window)
            future.set_result({"kind": request.kind, "result": value, "snapshot": snapshot})
        return history

    trailing = router(local)
    trailing.client.state = ClientState(verified=True, answering=True)
    trailing.prepare(scope)
    trailing._local_future.result(timeout=10)
    answer(trailing)
    assert trailing.ready(scope)
    assert len(trailing.ranking(scope, "total_bytes")) == 1
    assert trailing.daemon_answer_error is None

    # A part that answers another window is dropped, and the ranking survives.
    contradictory = router(AppIoStore(tmp_path / "empty2.db"))
    contradictory.client.state = ClientState(verified=True, answering=True)
    contradictory.prepare(scope)
    contradictory._local_future.result(timeout=10)
    answer(contradictory, part_window=elsewhere)
    assert contradictory.ready(scope)
    assert contradictory.daemon_answer_error is None
    assert len(contradictory.ranking(scope, "total_bytes")) == 1
    assert ("series", "exe:/usr/bin/shared") not in contradictory._remote_parts
    assert ("series", "exe:/usr/bin/shared") not in contradictory._requested_parts


def test_old_scope_remote_response_never_populates_new_scope(tmp_path):
    local, _ = source(tmp_path / "local.db")
    history = router(local)
    history.client.state = ClientState(verified=True, answering=True)
    first, second = window(), window(100, 150)
    history.prepare(first)
    assert not history.ready(first)
    request, pending = history.client.pending[0]
    history.prepare(second)
    pending.set_result({"kind": "ranking", "result": QueryService(local).ranking(first, "total_bytes"),
                        "next_cursor": None})
    assert not history.ready(second)
    assert len(history.client.pending) == 2
    with pytest.raises(RuntimeError, match="ready"):
        history.ranking(second, "total_bytes")


def test_two_boots_keep_monotonic_time_and_device_names_boot_scoped(tmp_path):
    local, _ = source(tmp_path / "local.db", boot="boot-a")
    root, _ = source(tmp_path / "root.db", "iod", boot="boot-b", app_id=3, amount=30)
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    rows = result(history, Window("lifetime"))
    assert sum(row.read_bytes for row in rows) == 50
    assert {s.boot_id for s in history.segments} == {"boot-a", "boot-b"}


def test_shared_boot_order_evidence_beats_the_order_sources_were_added(tmp_path):
    local, _ = source(tmp_path / "local.db", boot="boot-new")
    root, _ = source(tmp_path / "root.db", "iod", boot="boot-old")
    root.start_boot("boot-new", 2, 0, 1000.0, "iod")
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    result(history, Window("lifetime"))
    assert [segment.boot_id for segment in history.segments] == ["boot-old", "boot-new"]


def test_disjoint_boot_orders_are_explicitly_not_a_proven_global_chronology(tmp_path):
    local, _ = source(tmp_path / "local.db", boot="boot-a")
    root, _ = source(tmp_path / "root.db", "iod", boot="boot-b")
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    rows = result(history, Window("lifetime"))
    assert sum(row.read_bytes for row in rows) == 40
    assert "chronology" in history.view_state
    assert not rows[0].coverage.complete


def test_discarding_one_uncorrectable_boot_keeps_other_boot_lifetime_bytes(tmp_path):
    local, _ = source(tmp_path / "local.db", end=120)
    root, _ = source(tmp_path / "root.db", "iod", start=110, end=130)
    local.start_boot("boot-b", 2, 0, 1000.0, "gui")
    epoch = local.start_collector("gui", "boot-b", 100*NS).epoch
    state = None
    for seq, at, counters in ((0, 100, (100,)*4), (1, 110, (130,)*4)):
        proc = StableProcRecord(ProcessIdentity("boot-b", 410, 100), 1, 1000, "/usr/bin/shared", "/fixture",
            AppKeyInputs("/fixture", "/usr/bin/shared"), IoReadOutcome("readable", counters, None, None))
        observation = TickInput("boot-b", "gui", epoch, seq, at*NS, (1000+at)*NS, (proc,), ())
        frame, state = baseline_seed(observation) if state is None else aggregate_tick(state, observation)
        local.record_tick(frame, state)
    local.connection.execute("DELETE FROM sample WHERE boot_id=?", (BOOT,))
    local.connection.commit()
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    rows = result(history, Window("lifetime"))
    assert sum(row.read_bytes for row in rows) == 30
    assert "not_retained" in history.view_state


def test_unsupported_scope_preserves_a22_error(tmp_path):
    local, _ = source(tmp_path / "local.db")
    with pytest.raises(WindowUnavailableError, match="Launch window disabled"):
        router(local).prepare(Window("launch", BOOT, launch_id=1))


def test_sqlite_history_work_does_not_run_on_the_ui_thread(tmp_path, monkeypatch):
    local, _ = source(tmp_path / "local.db")
    ui_thread = threading.get_ident()
    original = HistoryRouter._materialize
    ran = threading.Event()
    def checked(self):
        assert threading.get_ident() != ui_thread
        ran.set()
        return original(self)
    monkeypatch.setattr(HistoryRouter, "_materialize", checked)
    history = router(local)
    result(history, window())
    assert ran.is_set()
    history.close()


def test_unknown_boot_is_an_explicit_gap_not_a_complete_empty_history(tmp_path):
    local, _ = source(tmp_path / "local.db")
    history = router(local)
    rows = result(history, window(100, 130, "unknown-boot"))
    assert rows == []
    assert history.segments == (HistorySegment("unproved", "unknown", "unknown-boot",
                                               100*NS, 130*NS, False),)
    assert "gap" in history.view_state


@pytest.mark.parametrize("old_name,new_name", [("a-old", "z-new"), ("z-old", "a-new")])
def test_pruned_mirror_is_not_a_second_contributor_or_a_false_retention_gap(tmp_path, old_name, new_name):
    local, _ = source(tmp_path / "local.db")
    old, _ = source(tmp_path / "old.db", "iod", start=110, end=130)
    new, _ = source(tmp_path / "new.db", "iod", start=110, end=130)
    old.connection.execute("DELETE FROM sample")
    old.connection.commit()
    history = router(local)
    history.add_source(QueryService(old), collector_id=old_name, owner="iod")
    history.add_source(QueryService(new), collector_id=new_name, owner="iod")
    rows = result(history, Window("lifetime"))
    assert sum(row.read_bytes for row in rows) == 40
    assert "not_retained" not in history.view_state


def test_settled_pruned_lifetime_summary_is_not_replaced_with_zero(tmp_path):
    local, _ = source(tmp_path / "local.db")
    local.connection.execute("DELETE FROM sample")
    local.connection.commit()
    history = router(local)
    rows = result(history, Window("lifetime"))
    assert sum(row.read_bytes for row in rows) == 20
    assert "not_retained" not in history.view_state


def test_transport_loss_is_local_and_does_not_override_a_complete_export(tmp_path):
    from appio.iodclient import GapNotice
    local, _ = source(tmp_path / "local.db")
    root, _ = source(tmp_path / "root.db", "iod", start=110, end=130)
    history = router(local)
    notice = GapNotice(BOOT, 1, 1, 110*NS, 130*NS, "disconnect")
    history.record_transport_gap(notice)
    history.record_transport_gap(notice)
    assert local.connection.execute("SELECT count(*) FROM gap WHERE reason='gui_absent'").fetchone()[0] == 1
    assert root.connection.execute("SELECT count(*) FROM gap").fetchone()[0] == 0
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    rows = result(history, Window("lifetime"))
    assert sum(row.read_bytes for row in rows) == 40
    assert all(row.coverage.gap_intervals == 0 for row in rows)


def test_restart_preserves_both_daemon_epochs_and_marks_the_restart_gap(tmp_path):
    from test_live_sources import make_tree, write_all, SequenceClock, BOOT_ID, WALL
    from appio.live import LiveCollector
    proc, group = make_tree(tmp_path)
    path = tmp_path / "root.db"
    clock = SequenceClock(*((at*NS, WALL+at*NS) for at in (100, 101, 110, 120, 121, 130)))
    write_all(proc, (100,)*4)
    daemon = LiveCollector(proc, group, path, clock, source="iod", baseline_only=True)
    daemon.tick()
    write_all(proc, (110,)*4)
    daemon.tick()
    daemon.close()
    daemon = LiveCollector(proc, group, path, clock, source="iod", baseline_only=True)
    daemon.tick()
    write_all(proc, (120,)*4)
    daemon.tick()
    daemon.close()
    root = AppIoStore(path)
    local, _ = source(tmp_path / "local.db", boot="different-boot")
    history = router(local)
    history.add_source(QueryService(root), collector_id="root", owner="iod")
    scope = Window("range", BOOT_ID, 100*NS, 140*NS)
    rows = result(history, scope)
    assert sum(row.read_bytes for row in rows) == 80
    assert any(not s.proven and s.start_ns == 110*NS and s.end_ns == 121*NS
               for s in history.segments)
    app_id = next(row.app_id for row in rows if row.read_bytes)
    assert history.disk_share(app_id, scope).task_read_bytes == 80
    assert sum(point.read_bytes for point in history.series(app_id, scope).points) == 80


def test_straddling_subsecond_bucket_preserves_device_rows_and_nullable_residual(tmp_path):
    from test_handover import initialized, outgoing, offer_for, record, tick, group
    store = initialized(tmp_path / "both.db")
    gui = outgoing(store)
    _, gui = record(store, gui, tick(2, 110*NS+NS//4, (110, 24, 88, 12),
                                    devices=(group(1010, 204),)))
    store.install_handover(offer_for(gui, "iod"), wall_ns=1110*NS)
    iod = store.restore_checkpoint("iod", BOOT)
    # Both sides contribute to the same ten-second raw bucket at the cutoff.
    _, iod = record(store, iod, tick(1, 111*NS, (115, 26, 92, 13), source="iod",
                                 devices=(group(1050, 220),)))
    record(store, iod, tick(2, 120*NS, (115, 26, 92, 13), source="iod",
                           devices=(group(1050, 220),)))
    history = router(store)
    scope = window(100, 120)
    row, = result(history, scope)
    assert row.read_bytes == 12
    share = history.disk_share(row.app_id, scope)
    assert sum(device.rbytes for device in share.devices) == 50
    # A v1 handover seed lacks device/rate provenance; do not upgrade it.
    assert share.device_read_bytes is None
    assert share.residual_read is None
    assert share.coverage.device_state == "incomplete"
    series = history.series(row.app_id, scope)
    assert sum(point.read_bytes for point in series.points) == 12
    assert sum(point.rbytes for point in series.device_points) == 50
    assert all(point.read is None for point in series.residual_points)


def test_signed_residuals_are_not_clamped_when_device_evidence_is_available(tmp_path):
    from test_handover import initialized, outgoing, record, tick, group
    store = initialized(tmp_path / "measured.db")
    state = outgoing(store)
    record(store, state, tick(2, 120*NS, (110, 24, 88, 12),
                              devices=(group(1010, 204),)))
    history = router(store)
    scope = window(100, 120)
    row, = result(history, scope)
    share = history.disk_share(row.app_id, scope)
    assert share.device_read_bytes == 10
    assert share.residual_read == -2
    timeline = history.series(row.app_id, scope)
    assert sum(point.read for point in timeline.residual_points) == -2


@pytest.mark.parametrize("variant", ["empty", "paginated"])
def test_remote_evidence_rejections_have_no_numeric_fallback(tmp_path, variant):
    local = AppIoStore(tmp_path / "empty.db")
    local.start_boot(BOOT, 1, 0, 1000.0, "gui")
    remote, _ = source(tmp_path / "remote.db", "iod")
    scope = window()
    rows = QueryService(remote).ranking(scope, "total_bytes")
    if variant == "empty":
        rows = []
    if variant == "paginated":
        rows = [replace(row, coverage=replace(row.coverage, complete=True)) for row in rows]
    history = router(local)
    history.client.state = ClientState(verified=True, answering=True)
    history.prepare(scope)
    history.client.pending[0][1].set_result({"kind": "ranking", "result": rows,
        "snapshot": "fixed", "next_cursor": "more" if variant == "paginated" else None})
    history._local_future.result(timeout=10)
    assert history.ready(scope)
    ranked = history.ranking(scope, "total_bytes")
    assert ranked == []
    assert isinstance(history.daemon_answer_error, DaemonAnswerError)
    assert history.daemon_answer_error.kind == "internal"
    assert "not_retained" not in history.view_state


def test_incomplete_remote_history_is_retained_with_honest_coverage(tmp_path):
    local = AppIoStore(tmp_path / "empty.db")
    remote, _ = source(tmp_path / "remote.db", "iod")
    scope = window(100, 120)
    service = QueryService(remote)
    expected = service.ranking(scope, "total_bytes")
    assert expected and not expected[0].coverage.complete

    history = router(local)
    history.client.state = ClientState(verified=True, answering=True)
    history.prepare(scope)

    def answer_pending():
        for request, future in history.client.pending:
            if future.done():
                continue
            if request.kind == "ranking":
                result = expected
            else:
                app_id = remote.connection.execute(
                    "SELECT app_id FROM app WHERE key=?", (request.app_key,)
                ).fetchone()[0]
                result = getattr(service, "disk_share" if request.kind == "totals" else "series")(
                    app_id, scope
                )
            future.set_result({"kind": request.kind, "result": result, "snapshot": "fixed"})

    for _ in range(4):
        answer_pending()
        work = getattr(history, "_local_future", None)
        if work is not None:
            work.result(timeout=10)
        if history.ready(scope):
            break
    assert history.ready(scope)
    rows = history.ranking(scope, "total_bytes")
    assert [(row.app_key, row.read_bytes, row.write_bytes) for row in rows] == [
        ("exe:/usr/bin/shared", 20, 20)]
    assert not rows[0].coverage.complete
    assert rows[0].coverage.unavailable_reason is None
    assert "not_retained" not in history.view_state


@pytest.mark.parametrize("local_boot,start,end,expected", [
    (BOOT, 100, 110, 0),  # An owned, measured zero is still local history.
    ("other-boot", 100, 110, 40),
    (BOOT, 90, 100, 40),  # Half-open ownership ends at this window's start.
    (BOOT, 120, 130, 40),  # Ownership starts at this window's end.
])
def test_remote_fallback_uses_scoped_ownership_not_filtered_counters(
    tmp_path, local_boot, start, end, expected,
):
    local, _ = source(tmp_path / "local.db", boot=local_boot, start=start, end=end, amount=0)
    remote, _ = source(tmp_path / "remote.db", "iod")
    history = router(local)
    service, scope = QueryService(remote), window(100, 120)
    history.client.state = ClientState(verified=True, answering=True)
    try:
        history.prepare(scope)
        history._local_future.result(timeout=10)
        for _ in range(4):
            for request, future in history.client.pending:
                if future.done():
                    continue
                if request.kind == "ranking":
                    value = service.ranking(scope, "total_bytes")
                else:
                    app_id = remote.connection.execute(
                        "SELECT app_id FROM app WHERE key=?", (request.app_key,)
                    ).fetchone()[0]
                    value = getattr(service, "disk_share" if request.kind == "totals" else "series")(
                        app_id, scope)
                future.set_result({"kind": request.kind, "result": value, "snapshot": "fixed"})
            if history.ready(scope):
                break
        assert history.ready(scope)
        row, = history.ranking(scope, "total_bytes")
        assert row.total_bytes == expected
        assert row.coverage.overlap_uncertain_intervals == 0
        assert not row.coverage.complete
    finally:
        history.close()
        local.connection.close()
        remote.connection.close()


def test_empty_or_partial_remote_answer_cannot_replace_proved_local_bytes(tmp_path):
    local, _ = source(tmp_path / "local.db")
    history = router(local)
    history.client.state = ClientState(verified=True, answering=True)
    scope = window()
    history.prepare(scope)
    history.client.pending[0][1].set_result({"kind": "ranking", "result": [], "next_cursor": None})
    work = getattr(history, "_local_future", None)
    if work is not None:
        work.result(timeout=10)
    assert history.ready(scope)
    rows = history.ranking(scope, "total_bytes")
    assert sum(row.read_bytes for row in rows) == 20
    expected = QueryService(local).ranking(scope, "total_bytes")
    assert [row.coverage.unavailable_reason for row in rows] == [
        row.coverage.unavailable_reason for row in expected]
    assert isinstance(history.daemon_answer_error, DaemonAnswerError)
    assert history.daemon_answer_error.kind == "internal"
    assert "not_retained" not in history.view_state
