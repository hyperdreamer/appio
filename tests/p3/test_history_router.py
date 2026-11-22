"""History selection uses durable intervals, never matching local surrogate IDs."""
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from appio.contracts import HistoryPending, Window, WindowUnavailableError
from appio.handover import HandoverOffer, baseline_seed, capture_handover
from appio.history_router import DaemonAnswerError, HistoryRouter, HistorySegment
from appio.history_source import ManagedSourceRegistry, SourceAdmissionError
from appio.history_types import ManagedSourceSpec, RemotePosition, retention_scope
from appio.iodclient import AppRegistry, ClientState, QueryTransportState
from appio.ipc_models import StreamCursor
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.query import QueryService
from appio.sampler import TickInput, aggregate_tick
from appio.store import AppIoStore

NS = 1_000_000_000
BOOT = "boot"
#: A valid 64-lowercase-hex accepted identity for scripted remote answers.
SNAPSHOT = "a" * 64


class Client:
    """A controlled client whose returned Futures carry transport association."""

    state = ClientState()

    def __init__(self):
        self.pending = []
        self.generation = 0
        self.rejected = []
        self.retired = []

    def _source(self):
        state = self.state
        if (state is None or not state.verified or not state.answering
                or state.cursor is None):
            return None
        return RemotePosition(state.cursor.boot_id, state.cursor.iod_epoch,
                              state.cursor.seq)

    def query(self, request):
        future = Future()
        self.pending.append((request, future))
        return future

    def query_transport(self, future):
        for index, (request, candidate) in enumerate(self.pending):
            if candidate is future:
                break
        else:
            raise ValueError("this Future is not bound to a client query")
        source = self._source()
        succeeded = future.done() and future.exception() is None
        return QueryTransportState(
            request=request,
            connection_generation=self.generation,
            request_id=f"q{self.generation}-{index}",
            phase="terminal" if future.done() else "active",
            issued_at_ns=0,
            issued_source=source,
            answered_source=source if succeeded else None,
        )

    def retire_query_connection(self, future):
        for _request, candidate in self.pending:
            if candidate is future:
                if not future.done():
                    self.retired.append(future)
                    self.generation += 1
                    return True
                return False
        raise ValueError("this Future is not bound to a client query")

    def reject_query_result(self, future):
        if not any(candidate is future for _request, candidate in self.pending):
            raise ValueError("this Future is not bound to a client query")
        self.rejected.append(future)


def answering_remote(history, *, seq=1):
    """Give the controlled client a verified answering connection with a cursor."""

    history.client.state = ClientState(
        verified=True, answering=True, cursor=StreamCursor(BOOT, 1, seq))
    return history.client


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
    from live_overview_support import make_managed_router
    return make_managed_router(QueryService(store), Client(), AppRegistry())


@pytest.fixture(autouse=True)
def managed_history_cleanup(monkeypatch):
    import live_overview_support

    create = live_overview_support.make_managed_router
    histories = []

    def tracked(*args, **options):
        history = create(*args, **options)
        histories.append(history)
        return history

    monkeypatch.setattr(live_overview_support, "make_managed_router", tracked)
    yield
    for history in histories:
        history.close()


def window(start=100, end=140, boot=BOOT):
    return Window("range", boot, start*NS, end*NS)


def result(router, scope):
    from live_overview_support import wait_for_local_frame

    router.prepare(scope)
    wait_for_local_frame(router)
    assert router.ready(scope)
    return router.ranking(scope, "total_bytes")


def wait_local(router):
    from live_overview_support import wait_for_local_frame

    return wait_for_local_frame(router)


def add_managed_source(history, store, *, collector_id, owner):
    path = store.connection.execute("PRAGMA database_list").fetchone()[2]
    mode = "?mode=rw&cache=shared" if owner == "gui" and collector_id == "local" else "?mode=ro"
    spec = ManagedSourceSpec(collector_id, owner, Path(path).as_uri() + mode, ())
    return history.add_source(spec).result(timeout=10)


def test_router_requires_managed_sources_and_accepts_only_specs(tmp_path, monkeypatch):
    local, _ = source(tmp_path / "local.db")
    imported, _ = source(tmp_path / "imported.db", "iod")
    sources = ManagedSourceRegistry(wake=lambda: None)

    def spec(name, store, owner):
        path = store.connection.execute("PRAGMA database_list").fetchone()[2]
        mode = "?mode=rw&cache=shared" if owner == "gui" else "?mode=ro"
        return ManagedSourceSpec(name, owner, Path(path).as_uri() + mode, ())

    local_spec = spec("local", local, "gui")
    sources.register(local_spec).result(timeout=10)
    history = HistoryRouter(sources, Client(), AppRegistry())
    try:
        imported_spec = spec("imported", imported, "iod")
        history.add_source(imported_spec).result(timeout=10)
        assert {name for name, _, _ in sources.registration_vector()} == {"local", "imported"}
        calls = []
        register = sources.register
        def observed_register(spec):
            calls.append(spec)
            return register(spec)
        monkeypatch.setattr(sources, "register", observed_register)
        identities = sources.registration_vector()
        for invalid in (QueryService(local), local.connection):
            with pytest.raises(SourceAdmissionError, match="^history source is not managed$"):
                history.add_source(invalid)
        assert calls == []
        assert sources.registration_vector() == identities
        # The compatibility entry point forwards the exact immutable spec.
        forwarded = []
        response = Future()
        monkeypatch.setattr(history, "add_managed_source",
                            lambda spec: forwarded.append(spec) or response)
        assert history.add_source(imported_spec) is response
        assert forwarded == [imported_spec]
    finally:
        history.close()
        assert sources._closed
        assert sources.registration_vector() == ()
        sources.close()

    for invalid in (QueryService(local), local.connection):
        with pytest.raises(SourceAdmissionError, match="^history source is not managed$"):
            HistoryRouter(invalid, Client(), AppRegistry())



def detail(router, app_id, method, scope, *, key):
    """Select, admit and drain one local detail until it is accepted."""

    router.select_detail(key, scope)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            return getattr(router, method)(app_id, scope)
        except HistoryPending:
            router.drain()
            time.sleep(0.005)
    raise AssertionError(f"detail {method} for {key!r} did not settle")


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
    add_managed_source(history, root, collector_id="export", owner="iod")

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
    add_managed_source(history, root, collector_id="root", owner="iod")

    rows = result(history, window())
    assert {row.app_key for row in rows} == {"exe:/bin/first", "exe:/bin/second"}
    assert len({row.app_id for row in rows}) == 2
    for row in rows:
        share = detail(history, row.app_id, "disk_share", window(), key=row.app_key)
        assert share.task_read_bytes == 20
        assert share.device_read_bytes is share.residual_read is None
        series = detail(history, row.app_id, "series", window(), key=row.app_key)
        assert sum(point.read_bytes for point in series.points) == 20


def test_overlap_without_handover_is_a_gap_not_an_owner_guess(tmp_path):
    local, _ = source(tmp_path / "local.db", start=100, end=120)
    root, _ = source(tmp_path / "root.db", "iod", start=110, end=130)
    history = router(local)
    add_managed_source(history, root, collector_id="root", owner="iod")

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
        add_managed_source(history, root, collector_id=name, owner="iod")

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
    add_managed_source(history, root, collector_id="root", owner="iod")

    result(history, Window("lifetime"))
    assert "not_retained" in history.view_state
    assert not any(s.proven and s.collector_id == "local" for s in history.segments)


def test_a_frame_prefetches_only_its_default_subject_and_loads_the_rest_on_demand(tmp_path):
    """Falsifier: the frame prefetched two parts for every ranked app.

    A frame copy proves ranking and ownership only; the selected subject's part
    is the only automatic detail consumer, so an unselected row is requested on
    demand and never fabricated.
    """

    remote_store, _ = source(tmp_path / "remote.db", "iod")
    coverage_json = remote_store.connection.execute(
        "SELECT coverage_json FROM app_boot_total WHERE app_id=17").fetchone()[0]
    remote_store.connection.execute(
        "INSERT INTO app VALUES(3,'exe:/usr/bin/other','other',NULL,0,0,'user')")
    remote_store.connection.execute(
        "INSERT INTO app_boot_total VALUES('boot',3,0,0,0,0,NULL,NULL,NULL,?)", (coverage_json,))
    remote_store.connection.commit()
    # A whole-boot scope ranks from the retained totals, so both apps are rows.
    scope = Window("since_boot", BOOT, end_mono_ns=200 * NS)
    service = QueryService(remote_store)
    expected = service.ranking(scope, "total_bytes")
    assert [row.app_key for row in expected] == ["exe:/usr/bin/shared", "exe:/usr/bin/other"]

    history = router(AppIoStore(tmp_path / "empty.db"))
    client = answering_remote(history)
    history.prepare(scope)
    wait_local(history)
    ranking, ranking_future = client.pending[0]
    assert ranking.kind == "ranking"
    ranking_future.set_result({"kind": "ranking", "result": expected, "snapshot": SNAPSHOT,
                               "next_cursor": None})
    assert history.ready(scope)
    # A frame prefetches no subject detail at all.
    assert [request.kind for request, _ in client.pending[1:]] == []
    assert [row.app_key for row in history.ranking(scope, "total_bytes")] == [
        "exe:/usr/bin/shared", "exe:/usr/bin/other"]

    # A subject outside the default selection is requested on demand.
    other_key = "exe:/usr/bin/other"
    other = history.registry.id_for(other_key)
    history.select_detail(other_key, scope)
    with pytest.raises(HistoryPending):
        history.series(other, scope)
    requested, pending = client.pending[-1]
    assert (requested.kind, requested.app_key) == ("series", other_key)
    app_id = remote_store.connection.execute(
        "SELECT app_id FROM app WHERE key=?", (other_key,)).fetchone()[0]
    pending.set_result({"kind": "series", "result": service.series(app_id, scope),
                        "snapshot": SNAPSHOT, "next_cursor": None, "app_key": other_key})
    assert history.drain() is True
    assert history.series(other, scope).app_id == other
    # No further prefetch was admitted for the unselected ranked row.
    assert [request.kind for request, _ in client.pending].count("series") == 1


def test_a_part_binds_its_window_and_never_blanks_the_ranking(tmp_path):
    """Falsifier: a subject's part was treated as evidence about the whole frame.

    A part answering another window is a current failure for its own slot only:
    the frame ranking stands, no owner record publishes, and the reason is the
    explicit invalid_result boundary token.
    """

    local = AppIoStore(tmp_path / "empty.db")
    remote_store, _ = source(tmp_path / "remote.db", "iod")
    scope = Window("since_boot", BOOT, end_mono_ns=200 * NS)
    elsewhere = Window("since_boot", BOOT, end_mono_ns=300 * NS)
    service = QueryService(remote_store)
    expected = service.ranking(scope, "total_bytes")
    key = expected[0].app_key

    def answer(history, client, *, part_window=None):
        history.prepare(scope)
        wait_local(history)
        ranking, ranking_future = client.pending[0]
        assert ranking.kind == "ranking"
        ranking_future.set_result({"kind": "ranking", "result": expected,
                                   "snapshot": SNAPSHOT, "next_cursor": None})
        assert history.ready(scope)
        presentation = history.registry.id_for(key)
        history.select_detail(key, scope)
        assert history.request_detail("totals", presentation, scope).state == "pending"
        request, future = [item for item in client.pending if not item[1].done()][-1]
        assert (request.kind, request.app_key) == ("totals", key)
        app_id = remote_store.connection.execute(
            "SELECT app_id FROM app WHERE key=?", (key,)).fetchone()[0]
        value = service.disk_share(app_id, scope)
        if part_window is not None:
            value = replace(value, window=part_window)
        future.set_result({"kind": "totals", "result": value, "snapshot": SNAPSHOT,
                           "next_cursor": None, "app_key": key})
        return presentation

    trailing = router(local)
    client = answering_remote(trailing)
    presentation = answer(trailing, client)
    assert trailing.drain() is True
    slot = trailing.part_state("totals", key, retention_scope(key, scope))
    assert slot.state == "ok"
    assert trailing.retained("totals", key, retention_scope(key, scope),
                             scope.end_mono_ns) is not None
    assert trailing.ready(scope)
    assert len(trailing.ranking(scope, "total_bytes")) == 1
    assert trailing.daemon_answer_error is None

    # A part that answers another window fails its own slot; ranking survives.
    contradictory = router(AppIoStore(tmp_path / "empty2.db"))
    client = answering_remote(contradictory)
    answer(contradictory, client, part_window=elsewhere)
    assert contradictory.drain() is True
    failed = contradictory.part_state("totals", key, retention_scope(key, scope))
    assert failed.state == "failed" and failed.reason == "invalid_result"
    assert contradictory.retained("totals", key, retention_scope(key, scope),
                                  scope.end_mono_ns) is None
    assert contradictory.ready(scope)
    assert contradictory.daemon_answer_error is None
    assert len(contradictory.ranking(scope, "total_bytes")) == 1


def test_old_scope_remote_response_never_populates_new_scope(tmp_path):
    local, _ = source(tmp_path / "local.db")
    history = router(local)
    client = answering_remote(history)
    first, second = window(), window(100, 150)
    history.prepare(first)
    assert not history.ready(first)
    request, pending = client.pending[0]
    assert request.window == first
    wait_local(history)
    history.prepare(second)
    # The single wire occupant is not preempted; the replacement ranking waits.
    assert len(client.pending) == 1
    pending.set_result({"kind": "ranking",
                        "result": QueryService(local).ranking(first, "total_bytes"),
                        "snapshot": SNAPSHOT, "next_cursor": None})
    assert not history.ready(second)
    # Only after that terminal occupant is consumed may the successor be sent.
    assert len(client.pending) == 2
    successor, _ = client.pending[1]
    assert successor.window == second
    with pytest.raises(RuntimeError, match="ready"):
        history.ranking(second, "total_bytes")


def test_two_boots_keep_monotonic_time_and_device_names_boot_scoped(tmp_path):
    local, _ = source(tmp_path / "local.db", boot="boot-a")
    root, _ = source(tmp_path / "root.db", "iod", boot="boot-b", app_id=3, amount=30)
    history = router(local)
    add_managed_source(history, root, collector_id="root", owner="iod")

    rows = result(history, Window("lifetime"))
    assert sum(row.read_bytes for row in rows) == 50
    assert {s.boot_id for s in history.segments} == {"boot-a", "boot-b"}


def test_shared_boot_order_evidence_beats_the_order_sources_were_added(tmp_path):
    local, _ = source(tmp_path / "local.db", boot="boot-new")
    root, _ = source(tmp_path / "root.db", "iod", boot="boot-old")
    root.start_boot("boot-new", 2, 0, 1000.0, "iod")
    history = router(local)
    add_managed_source(history, root, collector_id="root", owner="iod")

    result(history, Window("lifetime"))
    assert [segment.boot_id for segment in history.segments] == ["boot-old", "boot-new"]


def test_disjoint_boot_orders_are_explicitly_not_a_proven_global_chronology(tmp_path):
    local, _ = source(tmp_path / "local.db", boot="boot-a")
    root, _ = source(tmp_path / "root.db", "iod", boot="boot-b")
    history = router(local)
    add_managed_source(history, root, collector_id="root", owner="iod")

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
    add_managed_source(history, root, collector_id="root", owner="iod")

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
    add_managed_source(history, old, collector_id=old_name, owner="iod")
    add_managed_source(history, new, collector_id=new_name, owner="iod")

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


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_transport_gap_unknown_boot_bounds_and_epoch_fallback(tmp_path, monkeypatch, journal_mode):
    """No-write outcomes preserve positions and successful callbacks invalidate."""
    from appio.iodclient import GapNotice
    from live_overview_support import GapSqlHarness, drain_transport_gaps, make_managed_router

    local, _ = source(tmp_path / "gaps.db")
    local.connection.execute(f"PRAGMA journal_mode={journal_mode}")
    local.connection.execute("DELETE FROM collector_epoch WHERE source='gui'")
    local.connection.commit()
    sql = GapSqlHarness(monkeypatch)
    history = make_managed_router(QueryService(local), Client(), AppRegistry())
    sources = history._capture_provider
    notifications = []
    monkeypatch.setattr(history, "_notify", lambda: notifications.append(history._generation))
    notice = GapNotice(BOOT, 9, 77, 110 * NS, 120 * NS, "disconnect")
    try:
        assert callable(getattr(history, "retry_transport_gaps", None))
        initial = sources.observe_async().result(timeout=10)
        generation = history._generation
        ignored = (GapNotice("", 0, 0, 0, NS, "disconnect"),
                   replace(notice, from_mono_ns=None), replace(notice, to_mono_ns=None),
                   replace(notice, from_mono_ns=-1))
        for item in ignored:
            assert history.record_transport_gap(item) is None
        history.record_transport_gap(replace(notice, boot_id="missing"))
        unknown = history._gap_writes[-1].future.result(timeout=10)
        assert unknown.disposition == "unknown_boot"
        drain_transport_gaps(history)
        writer = next(probe for probe in sql.connections if probe.role == "writer")
        assert writer.sql[-1] == "ROLLBACK", "unknown boot must roll back its no-write transaction"
        assert history._generation == generation
        assert sources.observe_async().result(timeout=10) == initial
        assert history.gap_persistence_error is None
        assert local.connection.execute("SELECT count(*) FROM gap").fetchone() == (0,)
        history.record_transport_gap(notice)
        drain_transport_gaps(history)
        assert history._generation == generation + 1
        assert local.connection.execute(
            "SELECT boot_id,source,epoch,seq,from_mono_ns,to_mono_ns,reason FROM gap"
        ).fetchall() == [(BOOT, "gui", 1, 0, 110 * NS, 120 * NS, "gui_absent")]
        inserted = sources.observe_async().result(timeout=10)
        assert inserted != initial
        duplicate = replace(notice, iod_epoch=10, seq=99, reason="cursor_not_retained")
        history.record_transport_gap(duplicate)
        duplicate_result = history._gap_writes[-1].future.result(timeout=10)
        assert duplicate_result.disposition == "duplicate"
        assert writer.sql[-1] == "ROLLBACK", "duplicate must roll back its no-write transaction"
        for item in history._gap_writes:
            item.future.exception(timeout=10)
        history.retry_transport_gaps()
        drain_transport_gaps(history)
        assert history._generation == generation + 2
        assert notifications == [generation + 1, generation + 2]
        assert sources.observe_async().result(timeout=10) == inserted
        assert history.transport_issues == [*ignored, replace(notice, boot_id="missing"),
                                            notice, duplicate]
        local.connection.execute("INSERT INTO collector_epoch VALUES('gui',?,4,NULL,0)", (BOOT,))
        local.connection.commit()
        history.record_transport_gap(replace(notice, from_mono_ns=121 * NS, to_mono_ns=122 * NS))
        drain_transport_gaps(history)
        assert local.connection.execute("SELECT epoch,seq FROM gap ORDER BY gap_id").fetchall() == [
            (1, 0), (4, 0)]
    finally:
        history.close()
        local.connection.close()


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
@pytest.mark.parametrize("winner", ["external", "actor", "reader", "race"])
def test_transport_gap_atomic_dedup_with_concurrent_writers(
        tmp_path, monkeypatch, journal_mode, winner):
    """The duplicate check shares the insert's immediate transaction."""
    from appio.iodclient import GapNotice
    from live_overview_support import (Clock, GapSqlHarness, _Barrier,
                                      drain_transport_gaps, make_managed_router)

    local, _ = source(tmp_path / "contended.db")
    local.connection.execute(f"PRAGMA journal_mode={journal_mode}")
    local.connection.commit()
    sql = GapSqlHarness(monkeypatch)
    clock = Clock()
    retry = _Barrier()
    waits = []

    def retry_wait(seconds):
        waits.append(seconds)
        clock.advance_ns(int(seconds * NS))
        if not retry.entered.is_set():
            retry._pause()

    history = make_managed_router(QueryService(local), Client(), AppRegistry(),
        source_options={"clock_ns": clock, "retry_wait": retry_wait})
    external = sqlite3.connect(tmp_path / "contended.db", isolation_level=None, timeout=0)
    notice = GapNotice(BOOT, 9, 8, 110 * NS, 120 * NS, "disconnect")
    duplicate_checks = []

    def observe_duplicate_check(probe, statement, parameters):
        if probe.role == "writer" and statement.startswith("SELECT 1 FROM gap"):
            duplicate_checks.append(probe.in_transaction)

    sql.before_execute = observe_duplicate_check
    try:
        before = history._capture_provider.observe_async().result(timeout=10)
        if winner == "race":
            with sql.hold("BEGIN IMMEDIATE") as beginning:
                history.record_transport_gap(notice)
                beginning.wait()
                external.execute("BEGIN IMMEDIATE")
                external.execute("INSERT INTO gap(boot_id,source,epoch,seq,from_mono_ns,to_mono_ns,reason) "
                    "VALUES(?,'gui',1,0,?,?,'gui_absent')", (BOOT, 110 * NS, 120 * NS))
                external.execute("UPDATE app SET name='collector committed' WHERE app_id=17")
                external.execute("COMMIT")
                beginning.release()
                drain_transport_gaps(history)
        elif winner in ("external", "reader"):
            external.execute("BEGIN IMMEDIATE" if winner == "external" else "BEGIN")
            external.execute("SELECT * FROM boot").fetchall()
            history.record_transport_gap(notice)
            if winner == "reader" and journal_mode == "WAL":
                drain_transport_gaps(history)
                assert external.execute("SELECT count(*) FROM gap").fetchone() == (0,)
            else:
                retry.wait()
                assert history.transport_issues == [notice]
                assert history.pending_work
                assert not history.drain()
            if winner == "external":
                external.execute("INSERT INTO gap(boot_id,source,epoch,seq,from_mono_ns,to_mono_ns,reason) "
                    "VALUES(?,'gui',1,0,?,?,'gui_absent')", (BOOT, 110 * NS, 120 * NS))
                external.execute("UPDATE app SET name='collector committed' WHERE app_id=17")
            external.execute("COMMIT")
            retry.release()
        else:
            with sql.hold("COMMIT") as committing:
                history.record_transport_gap(notice)
                committing.wait()
                with pytest.raises(sqlite3.OperationalError) as caught:
                    external.execute("BEGIN IMMEDIATE")
                assert caught.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
                committing.release()
                drain_transport_gaps(history)
            external.execute("BEGIN IMMEDIATE")
            duplicate = external.execute("SELECT 1 FROM gap WHERE boot_id=? AND source='gui' "
                "AND from_mono_ns=? AND to_mono_ns=? AND reason='gui_absent'",
                (BOOT, 110 * NS, 120 * NS)).fetchall()
            assert duplicate == [(1,)]
            external.execute("UPDATE app SET name='collector committed' WHERE app_id=17")
            external.execute("COMMIT")
        drain_transport_gaps(history)
        assert history.gap_persistence_error is None
        assert local.connection.execute("SELECT source,epoch,seq,from_mono_ns,to_mono_ns,reason FROM gap").fetchall() == [
            ("gui", 1, 0, 110 * NS, 120 * NS, "gui_absent")]
        assert duplicate_checks and all(duplicate_checks)
        if winner != "reader":
            assert local.connection.execute("SELECT name FROM app WHERE app_id=17").fetchone() == (
                "collector committed",)
        if journal_mode == "WAL":
            external.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        after = history._capture_provider.observe_async().result(timeout=10)
        assert after.sources[0].data_version != before.sources[0].data_version
        assert len(waits) <= 9 and all(0 <= wait <= 0.01 for wait in waits)
    finally:
        retry.release()
        external.close()
        history.close()
        local.connection.close()


@pytest.mark.parametrize("fault", ["busy_begin", "busy_commit", "locked", "io", "after_commit"])
def test_transport_gap_writer_failure_retry_and_commit_ambiguity(tmp_path, monkeypatch, fault):
    """Exact errors fence preparation and retries deduplicate ambiguous commits."""
    from appio.iodclient import GapNotice
    from live_overview_support import (Clock, GapSqlHarness, drain_transport_gaps,
                                      make_managed_router, wait_for_local_frame)

    local, _ = source(tmp_path / "failures.db")
    sql = GapSqlHarness(monkeypatch)
    clock = Clock()
    waits = []

    def retry_wait(seconds):
        waits.append(seconds)
        clock.advance_ns(200_000_000)

    history = make_managed_router(QueryService(local), Client(), AppRegistry(),
        source_options={"clock_ns": clock, "retry_wait": retry_wait})
    code = (sqlite3.SQLITE_BUSY if fault.startswith("busy") else
            sqlite3.SQLITE_LOCKED if fault == "locked" else sqlite3.SQLITE_IOERR)
    error = sqlite3.OperationalError(f"injected {fault}")
    error.sqlite_errorcode = code
    attempts = []
    active = True

    def fail(probe, statement, parameters):
        target = "COMMIT" if fault in ("busy_commit", "after_commit") else "BEGIN IMMEDIATE"
        if active and probe.role == "writer" and statement == target:
            attempts.append(statement)
            raise error

    if fault == "after_commit":
        sql.after_execute = fail
    else:
        sql.before_execute = fail
    notice = GapNotice(BOOT, 9, 7, 110 * NS, 120 * NS, "disconnect")
    scope = Window("lifetime")
    try:
        history.record_transport_gap(notice)
        history.prepare(scope)
        drain_transport_gaps(history)
        assert history.gap_persistence_error is error
        assert not history._capture_jobs and not history._frame_jobs
        with pytest.raises(sqlite3.OperationalError) as caught:
            history.ready(scope)
        assert caught.value is error
        assert not history.frame_provenance(scope)
        assert 1 <= len(attempts) <= 10
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            assert len(attempts) > 1 and waits and clock() <= NS
        first_attempts = len(attempts)
        for _ in range(4):
            history.drain()
        assert len(attempts) == first_attempts
        assert local.connection.execute("SELECT count(*) FROM gap").fetchone() == (
            1 if fault == "after_commit" else 0,)
        active = False
        if fault == "io":
            second = replace(notice, from_mono_ns=121 * NS, to_mono_ns=122 * NS)
            second_error = sqlite3.OperationalError("second notice failed")

            def fail_second(probe, statement, parameters):
                if (probe.role == "writer" and statement.startswith("INSERT INTO gap")
                        and parameters[2] == 121 * NS):
                    raise second_error

            sql.before_execute = fail_second
            history.record_transport_gap(second)
            drain_transport_gaps(history)
            assert history.gap_persistence_error is error
            history.retry_transport_gaps()
            drain_transport_gaps(history)
            assert history.gap_persistence_error is second_error
            sql.before_execute = lambda *args: None
        history.retry_transport_gaps()
        drain_transport_gaps(history)
        assert history.gap_persistence_error is None
        expected_rows = 2 if fault == "io" else 1
        assert local.connection.execute("SELECT count(*) FROM gap").fetchone() == (expected_rows,)
        if fault == "io":
            active = True
            sql.before_execute = fail
            history.record_transport_gap(replace(notice, from_mono_ns=123 * NS, to_mono_ns=124 * NS))
            drain_transport_gaps(history)
            assert history.gap_persistence_error is error
            active = False
            sources = history._capture_provider
            spec = sources._active["local"].spec
            sources.replace("local", spec).result(timeout=10)
            history.retry_transport_gaps()
            assert history.gap_persistence_error is error
            assert all(item.future is None for item in history._gap_writes)
        history.prepare(scope)
        wait_for_local_frame(history)
        assert history.ready(scope)
        assert history.frame_provenance(scope).certified
    finally:
        history.close()
        local.connection.close()


def test_transport_loss_is_local_and_does_not_override_a_complete_export(tmp_path):
    from appio.iodclient import GapNotice
    local, _ = source(tmp_path / "local.db")
    root, _ = source(tmp_path / "root.db", "iod", start=110, end=130)
    history = router(local)
    notice = GapNotice(BOOT, 1, 1, 110*NS, 130*NS, "disconnect")
    from live_overview_support import drain_transport_gaps
    history.record_transport_gap(notice)
    history.record_transport_gap(notice)
    drain_transport_gaps(history)
    assert local.connection.execute("SELECT count(*) FROM gap WHERE reason='gui_absent'").fetchone()[0] == 1
    assert root.connection.execute("SELECT count(*) FROM gap").fetchone()[0] == 0
    add_managed_source(history, root, collector_id="root", owner="iod")

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
    add_managed_source(history, root, collector_id="root", owner="iod")

    scope = Window("range", BOOT_ID, 100*NS, 140*NS)
    rows = result(history, scope)
    assert sum(row.read_bytes for row in rows) == 80
    assert any(not s.proven and s.start_ns == 110*NS and s.end_ns == 121*NS
               for s in history.segments)
    row = next(row for row in rows if row.read_bytes)
    assert detail(history, row.app_id, "disk_share", scope,
                  key=row.app_key).task_read_bytes == 80
    assert sum(point.read_bytes for point in detail(
        history, row.app_id, "series", scope, key=row.app_key).points) == 80


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
    share = detail(history, row.app_id, "disk_share", scope, key=row.app_key)
    assert sum(device.rbytes for device in share.devices) == 50
    # A v1 handover seed lacks device/rate provenance; do not upgrade it.
    assert share.device_read_bytes is None
    assert share.residual_read is None
    assert share.coverage.device_state == "incomplete"
    series = detail(history, row.app_id, "series", scope, key=row.app_key)
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
    share = detail(history, row.app_id, "disk_share", scope, key=row.app_key)
    assert share.device_read_bytes == 10
    assert share.residual_read == -2
    timeline = detail(history, row.app_id, "series", scope, key=row.app_key)
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
    answering_remote(history)
    history.prepare(scope)
    history.client.pending[0][1].set_result({"kind": "ranking", "result": rows,
        "snapshot": SNAPSHOT, "next_cursor": "more" if variant == "paginated" else None})
    wait_local(history)

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
    answering_remote(history)
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
            future.set_result({"kind": request.kind, "result": result,
                               "snapshot": SNAPSHOT, "next_cursor": None})

    for _ in range(4):
        answer_pending()
        work = getattr(history, "_local_future", None)
        if work is not None:
            wait_local(history)
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
    answering_remote(history)
    try:
        history.prepare(scope)
        wait_local(history)
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
                future.set_result({"kind": request.kind, "result": value,
                                   "snapshot": SNAPSHOT, "next_cursor": None})
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
    answering_remote(history)
    scope = window()
    history.prepare(scope)
    history.client.pending[0][1].set_result({"kind": "ranking", "result": [],
        "snapshot": SNAPSHOT, "next_cursor": None})
    wait_local(history)
    assert history.ready(scope)
    rows = history.ranking(scope, "total_bytes")
    assert sum(row.read_bytes for row in rows) == 20
    expected = QueryService(local).ranking(scope, "total_bytes")
    assert [row.coverage.unavailable_reason for row in rows] == [
        row.coverage.unavailable_reason for row in expected]
    assert isinstance(history.daemon_answer_error, DaemonAnswerError)
    assert history.daemon_answer_error.kind == "internal"
    assert "not_retained" not in history.view_state
