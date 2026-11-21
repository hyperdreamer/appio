"""Ownership-bound asynchronous Files views, including real protocol-1 futures.

Falsifiers: basename merge across stores; query on UI-thread SQLite; stale
future publication; dropping HMAC continuations or snapshot/scope checks;
claiming an unavailable source is a complete empty table.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import time

import pytest

# Pin PyQt6 before pytest-qt builds its qapp fixture (see tests/test_ui.py).
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtCore import Qt

from appio.bpf_fileio import BpfFileSource, VectorLoader
from appio.contracts import FILE_HOUR_NS as H, Window, WindowUnavailableError
from appio.durable import canonical_json
from appio.file_runtime import FileRuntime
from appio.files import FileIdentityResolver
from appio.history_router import FileViewResult, HistoryRouter
from appio.iod_history import HistoryRpc
from appio.iodclient import (
    AppRegistry,
    ClientState,
    ClientUnavailable,
    ConnectionLost,
    QueryTransportState,
    RemoteError,
    RequestRejected,
)
from appio.ipc_models import Capabilities, QueryRequest, StreamCursor, validate_request
from appio.query import QueryService
from appio.store import AppIoStore
from appio.ui.files_view import FilesView
from tests.p3.test_iodclient import Harness, snapshot_frame
from tests.p4.test_file_runtime import APP, ZERO, Frames, event
from live_overview_support import (
    add_managed_source, managed_router, wait_for_capture_admission, wait_for_local_frame,
)


@pytest.fixture(autouse=True)
def managed_sources():
    yield

SORTS = ("bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen")
WINDOW = Window("since_boot", "boot-A", end_mono_ns=H)


class Client:
    state = ClientState()

    def query(self, request):
        raise AssertionError("unexpected remote request")


@pytest.fixture
def retained(tmp_path):
    f = Frames(tmp_path / "retained.db")
    loader = VectorLoader()
    rt = FileRuntime(f.db, source_factory=lambda: BpfFileSource(loader),
                     identity_resolver=FileIdentityResolver(lambda dev: "fs"))
    rt.start()
    loader.queue(0, events=[replace(event(inode=i, amount=10 + i), basename=name, name_len=len(name), reads=i, writes=8-i)
                           for i, name in enumerate([b"Cafe\xcc\x81", b"Caf\xc3\xa9", b"alpha", b"zeta"])])
    rt.consume(*f.tick(0))
    rt.consume(*f.tick(H, amount=200))
    yield f
    f.db.close()


def inject_requested_gap_evidence(db, app_id=1, gaps=1):
    """Commit synthetic hourly gap evidence before ``HistoryRpc`` pins a snapshot.

    The requested-window scalar is the app's retained hourly ``gap_intervals``
    contribution plus the requested overlay. Bumping an hourly row proves the
    remote route still transports a nonzero scalar while the served-hour reducer
    stays unprovable because the hourly rows themselves never cross the wire.
    """
    rows = db.execute(
        "SELECT boot_id,ts_mono_ns,coverage_json FROM file_coverage WHERE app_id=?", (app_id,)
    ).fetchall()
    assert rows, "the retained fixture must keep hourly file coverage"
    for boot, ts, text in rows:
        payload = json.loads(text)
        payload["gap_intervals"] = gaps
        db.execute(
            "UPDATE file_coverage SET coverage_json=? WHERE boot_id=? AND ts_mono_ns=? AND app_id=?",
            (canonical_json(payload), boot, ts, app_id),
        )
    db.commit()
    assert not db.in_transaction


def managed_router(store, client, registry=None, **options):
    from live_overview_support import make_managed_router

    return make_managed_router(
        QueryService(store), client, registry or AppRegistry(), **options)


def local_router(retained):
    router = managed_router(retained.db, Client())
    router.prepare(WINDOW)
    wait_for_local_frame(router)
    assert router.ready(WINDOW)
    return router


def id_for(router, key=APP):
    return router.registry.id_for(key)


def drive(router, future, *, timeout=15.0):
    """Owner-thread drain loop for one outward Files Future.

    Files settlement is lazy by design (spec §§3.3-3.4): a worker only enqueues
    an immutable outcome, and the owner's ``drain`` performs the closing reads
    and publishes. A fixture that blocks on the outward Future therefore has to
    act as the owner thread. Every assertion still observes the same published
    result or failure; only the delivery driver changed.
    """

    deadline = time.monotonic() + timeout
    while not future.done():
        router.drain()
        if future.done():
            break
        assert time.monotonic() < deadline, "the outward Files future never settled"
        time.sleep(0.001)
    return future.result(timeout=0)


@pytest.mark.parametrize("sort", SORTS)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_local_file_futures_delegate_every_sort_and_NFC_casefold_search(retained, sort, direction):
    router = local_router(retained)
    try:
        expected = QueryService(retained.db).file_table(1, WINDOW, sort, direction, "CAFÉ")
        expected_cov = QueryService(retained.db).file_coverage(1, WINDOW)
        view = drive(router, router.request_file_view(
            id_for(router), WINDOW, sort, direction, "CAFÉ"))
        assert isinstance(view, FileViewResult)
        assert view.table == replace(expected, app_id=id_for(router))
        assert view.coverage == replace(expected_cov, app_id=id_for(router))
        assert len(view.table.rows) == 2
    finally:
        router.close()


@pytest.mark.parametrize("sort", [
    "bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen",
])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_managed_files_worker_preserves_served_evidence(retained, sort, direction):
    """A Files worker uses the immutable image and keeps served-gap evidence."""

    from appio.history_source import ManagedSourceRegistry
    from appio.history_types import ManagedSourceSpec

    path = retained.db.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    registry = ManagedSourceRegistry(wake=lambda: None)
    captured = None
    try:
        registry.register(ManagedSourceSpec(
            "local", "iod", Path(path).as_uri() + "?mode=ro", ())).result(timeout=10)
        captured = registry.capture().result(timeout=15)
        result = HistoryRouter._read_local_files(
            captured, WINDOW, APP, 1, sort, direction, None,
            worker_id=3, request=QueryRequest("files", WINDOW, APP, sort=sort, direction=direction))
        direct = QueryService(retained.db)
        assert result.table == direct.file_table(1, WINDOW, sort, direction, None)
        assert result.coverage == direct.file_coverage(1, WINDOW)
        assert result.served_gap_count == direct.file_serve_facts(1, WINDOW).gaps_in_served_interval
        assert result.snapshot.capture_id == captured.capture_id
        assert result.snapshot.snapshot_digest == captured.snapshot_digest
    finally:
        if captured is not None:
            registry.release_capture(captured.capture_id)
        registry.close()


def test_managed_files_settlement_rejects_foreign_capture(retained):
    from pathlib import Path
    from types import SimpleNamespace
    from appio.history_router import LocalSettlement, RequestToken
    from appio.history_source import ManagedSourceRegistry
    from appio.history_types import ManagedSourceSpec

    path = retained.db.execute("PRAGMA database_list").fetchone()[2]
    registry = ManagedSourceRegistry(wake=lambda: None)
    captured = None
    try:
        receipt = registry.register(ManagedSourceSpec(
            "local", "iod", Path(path).as_uri() + "?mode=ro", ())).result(timeout=10)
        captured = registry.capture().result(timeout=15)
        request = QueryRequest("files", WINDOW, APP)
        outcome = HistoryRouter._read_local_files(
            captured, WINDOW, APP, 1, "bytes_total", "desc", None,
            worker_id=3, request=request)
        token = RequestToken(
            3, request, "local", None, "files", 1, None,
            ("local", (("local", "iod", receipt.registration_epoch),)), captured.opening)
        binding = SimpleNamespace(token_id=3, worker_id=3, request=request,
                                  snapshot=outcome.snapshot)
        router = object.__new__(HistoryRouter)
        router._worker_bindings = {}
        router._files_bindings = {3: binding}
        accepted = []
        rejected = []
        router._accept_files_local = lambda *args: accepted.append(args) or True
        router._reject_token = lambda *args: rejected.append(args) or False
        forged = replace(outcome, snapshot=replace(outcome.snapshot, snapshot_digest="f" * 64))
        assert not router._settle_local(token, LocalSettlement(3, outcome=forged))
        assert len(rejected) == 1 and accepted == []
        assert router._settle_local(token, LocalSettlement(3, outcome=outcome))
        assert len(accepted) == 1
    finally:
        if captured is not None:
            registry.release_capture(captured.capture_id)
        registry.close()


def test_local_query_runs_on_bounded_executor_and_invalidated_future_cannot_publish(retained, monkeypatch):
    router = local_router(retained)
    entered, release = threading.Event(), threading.Event()
    original = QueryService.file_table
    main_thread = threading.get_ident()
    ui_sql = []
    retained.db.set_trace_callback(ui_sql.append)

    def blocked(service, *args):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(5)
        return original(service, *args)

    monkeypatch.setattr(QueryService, "file_table", blocked)
    try:
        future = router.request_file_view(id_for(router), WINDOW, "bytes_total", "desc", None)
        wait_for_capture_admission(router)
        assert entered.wait(5)
        # Admission captures one O(1) owner-thread position PRAGMA for its token;
        # no Files query (table/coverage/facts) may touch the UI connection.
        assert all(statement.startswith("PRAGMA main.data_version") for statement in ui_sql), ui_sql
        assert not any(
            keyword in statement.upper()
            for statement in ui_sql
            for keyword in ("SELECT", "FROM", "ROWID")
        ), ui_sql
        router.invalidate()
        release.set()
        with pytest.raises((WindowUnavailableError, RemoteError)) as caught:
            future.result(timeout=10)
        assert caught.value.reason == "file_source_unavailable"
    finally:
        release.set()
        retained.db.set_trace_callback(None)
        router.close()


def test_local_SQL_failure_settles_as_explicit_unavailable_not_an_empty_table(retained, monkeypatch):
    import sqlite3
    router = local_router(retained)
    def broken(*args):
        raise sqlite3.OperationalError("synthetic read failure")
    monkeypatch.setattr(QueryService, "file_table", broken)
    try:
        with pytest.raises(WindowUnavailableError) as caught:
            drive(router, router.request_file_view(
                id_for(router), WINDOW, "bytes_total", "desc", None))
        assert caught.value.reason == "file_source_unavailable"
        assert not router.file_source_status().available
    finally:
        router.close()


def test_zero_F_is_complete_only_with_authoritative_hour_and_missing_or_pruned_is_explicit(retained):
    router = local_router(retained)
    try:
        retained.db.execute("DELETE FROM sample")
        retained.db.execute("DELETE FROM sample_dev")
        retained.db.commit()
        # F remains representable when 24-hour raw process observations are gone.
        zero = drive(router, router.request_file_view(
            id_for(router, ZERO), WINDOW, "bytes_total", "desc", None))
        assert zero.table.rows == () and zero.coverage.observed_bytes_r == 0
        assert zero.table.coverage.complete and zero.coverage.coverage.complete
        retained.db.execute("DELETE FROM file_coverage WHERE app_id=2")
        retained.db.execute("DELETE FROM app_boot_file_coverage WHERE app_id=2")
        retained.db.commit()
        # A bounded range uses hourly units, not the since-boot retained total.
        scoped = Window("range", "boot-A", 0, H)
        router.prepare(scoped)
        wait_for_local_frame(router)
        assert router.ready(scoped)
        with pytest.raises(WindowUnavailableError) as caught:
            drive(router, router.request_file_view(
                id_for(router, ZERO), scoped, "bytes_total", "desc", None))
        assert caught.value.reason == "not_retained"
    finally:
        router.close()


def test_missing_F_capability_is_not_a_zero_table_and_A22_precedes_bad_app_id(tmp_path):
    f = Frames(tmp_path / "empty.db")
    f.tick(0)
    f.tick(H)
    router = local_router(f)
    try:
        with pytest.raises(WindowUnavailableError) as caught:
            drive(router, router.request_file_view(
                id_for(router), WINDOW, "bytes_total", "desc", None))
        assert caught.value.reason == "file_source_unavailable"
        assert not router.file_source_status().available
        with pytest.raises(WindowUnavailableError) as disabled:
            router.request_file_view(-1, Window("launch", "boot-A", launch_id=1), "invalid", "bad", None)
        assert disabled.value.reason == "launch_attribution"
    finally:
        router.close()
        f.db.close()


def test_mixed_or_excluded_ownership_never_merges_database_local_paths(retained, tmp_path):
    other = Frames(tmp_path / "other.db")
    other.tick(0)
    other.tick(H)
    router = managed_router(retained.db, Client())
    add_managed_source(router, other.db, name="other", owner="gui")

    # Distinct epochs constitute unproved concurrent ownership even for matching AppKey.
    other.db.execute("UPDATE collector_interval SET source='gui',epoch=7")
    other.db.commit()
    try:
        router.prepare(WINDOW)
        wait_for_local_frame(router)
        assert router.ready(WINDOW)
        with pytest.raises(WindowUnavailableError) as caught:
            drive(router, router.request_file_view(
                id_for(router), WINDOW, "basename", "asc", None))
        assert caught.value.reason == "file_source_unavailable"
    finally:
        router.close()
        other.db.close()


def test_retained_local_file_history_is_queried_without_answering_daemon(retained, qtbot):
    router = local_router(retained)
    view = FilesView(router)
    qtbot.addWidget(view)
    try:
        view.set_window(WINDOW)

        def settled():
            # The Files view has no poll of its own: the owner thread (here the
            # test) must drain the lazy settlement that publishes its Future.
            router.drain()
            return view.state != "loading"

        qtbot.waitUntil(settled, timeout=10_000)
        assert view.state == "ready", (
            f"screen_state={view.state}; request_count={len(view.requests)}"
        )
        assert len(view.requests) == 1
        rendered = [
            view.model.data(view.model.index(row, 0), Qt.ItemDataRole.DisplayRole)
            for row in range(view.model.rowCount())
        ]
        assert "alpha" in rendered
    finally:
        view.close()
        router.close()


class Remote:
    """Drive real IodClient encoding/decoding plus real HistoryRpc HMAC cursors."""
    def __init__(self, tmp_path, retained, *, window=WINDOW):
        self.h = Harness()
        self.h.handshake()
        msg = snapshot_frame(1, boot_id="boot-A", epoch=1)
        msg["capabilities"] = dict(proc_all=True, dev_stat=True, file_io=True, bpf_loaded=True)
        self.h.send(msg)
        self.h.client.pump()
        self.rpc = HistoryRpc(retained.db)
        self.store = AppIoStore(tmp_path / "local-empty.db")
        self.router = managed_router(self.store, self.h.client)

        self.requests = []
        self.replies = []
        self.router.prepare(window)
        wait_for_local_frame(self.router)
        for _ in range(15):
            self.respond()
            if self.router.ready(window):
                break
        assert self.router.ready(window) and self.router._remote is not None

    def respond(self, mutate=None):
        self.h.client.pump()
        frames = self.h.requests(deadline=0.03)
        for wire in frames:
            assert wire["type"] == "query"
            request = validate_request(wire).query
            self.requests.append(request)
            # Force genuine server continuation tokens, even for four rows.
            reply = self.rpc.execute(replace(request, limit=1) if request.kind == "files" else request,
                                     request_id=wire["request_id"])
            if mutate:
                mutate(reply)
            self.replies.append(reply)
            self.h.send(reply)
        self.h.client.pump()
        # Files settlement is lazy: the owner drain admits the next page and
        # publishes the outward Future. Drive it in the same turn as the reply.
        self.router.drain()

    def finish(self, future):
        for _ in range(20):
            self.respond()
            if future.done():
                return future.result(timeout=1)
        raise AssertionError("remote file request did not settle")

    def close(self):
        self.router.close()
        self.h.client.close()
        for server in self.h.servers:
            server.close()
        self.store.connection.close()


@pytest.mark.parametrize("sort", SORTS)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_remote_real_IodClient_pages_preserve_HMAC_snapshot_sort_search_and_coverage(retained, tmp_path, sort, direction):
    # A nonzero hourly scalar proves the served slot never reuses the requested one.
    inject_requested_gap_evidence(retained.db)
    remote = Remote(tmp_path, retained)
    try:
        aid = id_for(remote.router)
        future = remote.router.request_file_view(aid, WINDOW, sort, direction, "CAFÉ")
        view = remote.finish(future)
        expected = QueryService(retained.db).file_table(1, WINDOW, sort, direction, "CAFÉ")
        assert view.table.rows == tuple(replace(row, path_id=0) for row in expected.rows)
        assert view.table.coverage == expected.coverage
        assert view.coverage.observed_bytes_r == 46
        assert view.coverage.coverage.gap_intervals == 1
        assert type(view.coverage.coverage.gap_intervals) is int
        assert view.served_gap_count is None
        files = [r for r in remote.requests if r.kind == "files"]
        assert len(files) == 2 and files[0].cursor is None and files[1].cursor
        assert all((r.sort, r.direction, r.search) == (sort, direction, "CAFÉ") for r in files)
        assert any(r.kind == "coverage" and r.coverage_kind == "file" for r in remote.requests)
        assert remote.router.file_source_status().available
    finally:
        remote.close()


@pytest.mark.parametrize("fault", ["snapshot", "cursor", "stale", "coverage_snapshot", "mixed_tier", "scope", "advanced_cursor", "app", "coverage_app", "page_served_scope", "page_requested_window"])
def test_remote_scope_snapshot_stale_cursor_and_invalidate_fail_closed(retained, tmp_path, fault):
    remote = Remote(tmp_path, retained)
    try:
        future = remote.router.request_file_view(id_for(remote.router), WINDOW, "bytes_total", "desc", None)
        if fault == "stale":
            remote.router.invalidate()
        if fault == "advanced_cursor":
            remote.h.send(snapshot_frame(2, boot_id="boot-A", epoch=1))
            remote.h.client.pump()
        cursor_corrupted = False
        files_seen = 0
        def corrupt(reply):
            nonlocal cursor_corrupted, files_seen
            if reply.get("kind") == "files":
                files_seen += 1
            if fault == "snapshot" and reply.get("kind") == "files":
                reply["payload"][0]["snapshot"] = "other"
            if fault == "cursor" and reply.get("kind") == "files" and not cursor_corrupted:
                reply["payload"][0]["next_cursor"] = "forged-hmac-token"
                cursor_corrupted = True
            if fault == "coverage_snapshot" and reply.get("kind") == "coverage":
                reply["payload"]["snapshot"] = "other"
            if fault == "scope" and reply.get("kind") == "files":
                reply["payload"][0]["value"]["search"] = "DIFFERENT"
            if fault == "app" and reply.get("kind") == "files":
                reply["payload"][0]["value"]["app_key"] = ZERO
            if fault == "coverage_app" and reply.get("kind") == "coverage":
                reply["payload"]["value"]["app_key"] = ZERO
            if fault == "mixed_tier" and reply.get("kind") == "coverage":
                coverage = reply["payload"]["value"]["coverage"]
                coverage["tier"] = "totals_only" if coverage["tier"] == "raw" else "raw"
            if fault == "page_requested_window" and reply.get("kind") == "files" and files_seen > 1:
                # A later page may not widen or shorten the requested window.
                reply["payload"][0]["value"]["window"]["end_mono_ns"] = H - 1
            if fault == "page_served_scope" and reply.get("kind") == "files" and files_seen > 1:
                # A later page may not claim a different served scope than page one.
                reply["payload"][0]["value"]["unknown_owner_overflow"]["window"].update(
                    kind="range", start_mono_ns=0, end_mono_ns=H)
        for _ in range(15):
            remote.respond(corrupt)
            if future.done():
                break
        # Fail-closed is the invariant. A malformed answer the client's own
        # pre-cache validator now rightly refuses surfaces as the typed
        # invalid_result refusal; faults that only the router's Files owner
        # predicates can see stay WindowUnavailableError; a forged server HMAC
        # stays the daemon's stale_cursor RemoteError. No arm may publish a
        # partial or zero-filled table.
        with pytest.raises((WindowUnavailableError, RemoteError, RequestRejected)) as caught:
            future.result(timeout=5)
        client_refused = ("snapshot", "coverage_snapshot", "scope", "app",
                          "coverage_app", "page_requested_window")
        if fault == "cursor":
            assert caught.value.reason == "stale_cursor"
        elif fault in client_refused:
            assert isinstance(caught.value, RequestRejected), fault
            assert caught.value.reason == "invalid_result"
        else:
            assert caught.value.reason == "file_source_unavailable", fault
        assert future.exception() is caught.value
        if fault in ("page_served_scope", "page_requested_window"):
            assert files_seen > 1, "the later-page fault must have been delivered"
    finally:
        remote.close()


@pytest.mark.parametrize("reason", ["query_busy", "stale_cursor", "not_retained", "file_source_unavailable"])
def test_remote_error_preserves_explicit_reason_without_returning_zeroes(retained, tmp_path, reason):
    from tests.p3.test_iodclient import error_frame
    remote = Remote(tmp_path, retained)
    try:
        future = remote.router.request_file_view(id_for(remote.router), WINDOW, "bytes_total", "desc", None)
        def rejected(reply):
            request_id = reply["request_id"]
            reply.clear()
            reply.update(error_frame(request_id, reason, "synthetic denial"))
        remote.respond(rejected)
        with pytest.raises(RemoteError) as caught:
            future.result(timeout=5)
        assert caught.value.reason == reason
        assert remote.h.client.state.last_error is None
    finally:
        remote.close()


@pytest.mark.parametrize("file_io", [False, True], ids=["no-file-io", "no-bpf-loaded"])
def test_remote_missing_capability_has_diagnostic_and_never_sends_files_query(retained, tmp_path, file_io):
    remote = Remote(tmp_path, retained)
    try:
        remote.h.send(snapshot_frame(2, boot_id="boot-A", epoch=1, caps=dict(
            proc_all=True, dev_stat=True, file_io=file_io, bpf_loaded=False)))
        remote.h.client.pump()
        future = remote.router.request_file_view(id_for(remote.router), WINDOW, "bytes_total", "desc", None)
        with pytest.raises(WindowUnavailableError) as caught:
            future.result(timeout=5)
        assert caught.value.reason == "file_source_unavailable"
        assert not remote.router.file_source_status().available
        assert not [r for r in remote.requests if r.kind == "files"]
    finally:
        remote.close()


def test_local_cancellation_cannot_turn_bounded_executor_into_unbounded_queue(retained, monkeypatch):
    router = local_router(retained)
    entered, release = threading.Event(), threading.Event()
    original = router._read_local_files
    calls = []
    def held(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(router, "_read_local_files", held)
    futures = []
    try:
        # Hold the router's single worker at a barrier before cancellation;
        # otherwise canceled-work skipping could make admission pass vacuously.
        futures.append(router.request_file_view(id_for(router), WINDOW, "bytes_total", "desc", None))
        wait_for_capture_admission(router)
        assert entered.wait(5)
        futures[0].cancel()
        for _ in range(23):
            future = router.request_file_view(id_for(router), WINDOW, "bytes_total", "desc", None)
            futures.append(future)
            future.cancel()
        # One bounded ledger is shared by the frame lane and the background
        # Files lane: canceled outward jobs never admit an unbounded queue.
        assert router._executor._work_queue.qsize() <= 8
        assert router._background_charge("files") <= 8
        assert len(router._worker_jobs) <= 10
    finally:
        release.set()
        router.close()


def test_remote_query_errors_are_typed_without_rewriting_data_reason(tmp_path):
    from tests.p3.test_history_router import source, window as process_window

    cases = (
        (RemoteError("remote_fault"), "remote_error", "remote_fault"),
        (ClientUnavailable("daemon_unavailable"), "unavailable", "daemon_unavailable"),
        (RequestRejected("too_many_requests"), "rejected", "too_many_requests"),
        (ConnectionLost("connection_lost"), "transport", "connection_lost"),
        (ValueError("  internal failure  "), "internal", "internal failure"),
    )

    class FailedQueryClient:
        generation = 0
        state = ClientState(
            verified=True, answering=True, cursor=StreamCursor("boot-A", 1, 1))

        def __init__(self, error):
            self.error = error
            self.bound = {}

        def query(self, request):
            future = Future()
            self.bound[future] = request
            future.set_exception(self.error)
            return future

        def query_transport(self, future):
            # A terminal issue-time failure owns no wire slot and no source.
            return QueryTransportState(
                self.bound[future], 0, None, "terminal", None, None, None)

    for index, (error, kind, reason) in enumerate(cases):
        store, _ = source(tmp_path / f"remote-{index}.db")
        router = managed_router(store, FailedQueryClient(error))

        scope = process_window(100, 110)
        try:
            router.prepare(scope)
            wait_for_local_frame(router)
            assert router._not_retained is False
            assert router.ready(scope)
            rows = router.ranking(scope, "total_bytes")
            assert rows[0].coverage.unavailable_reason is None
            assert router._not_retained is False
            assert router.daemon_answer_error.kind == kind
            assert router.daemon_answer_error.reason == reason
        finally:
            router.close()
            store.connection.close()

    pruned, _ = source(tmp_path / "pruned.db")
    pruned.connection.execute("DELETE FROM collector_interval")
    pruned.connection.commit()
    router = managed_router(pruned, Client())

    scope = Window("lifetime")
    try:
        router.prepare(scope)
        wait_for_local_frame(router)
        assert router.ready(scope)
        rows = router.ranking(scope, "total_bytes")
        assert rows[0].coverage.unavailable_reason == "not_retained"
    finally:
        router.close()
        pruned.connection.close()


def test_retirement_and_cancellation_do_not_set_history_failure_flags(retained):
    router = managed_router(retained.db, Client())

    try:
        with pytest.raises(WindowUnavailableError):
            router.prepare(Window("launch", "boot-A", launch_id=1))
        assert router._not_retained is False
        assert router._daemon_answer_error is None

        router.invalidate()
        assert router._not_retained is False
        assert router._daemon_answer_error is None

        marker = object()
        router._not_retained = True
        router._daemon_answer_error = marker
        router.invalidate()
        assert router._not_retained is True
        assert router._daemon_answer_error is marker

        router.prepare(WINDOW)
        assert router._not_retained is False
        assert router._daemon_answer_error is None
    finally:
        router.close()

    unavailable = Future()
    unavailable.set_exception(
        WindowUnavailableError("file_source_unavailable", "query window retired")
    )

    class UnavailableQueryClient:
        generation = 0
        state = ClientState(
            verified=True, answering=True, cursor=StreamCursor("boot-A", 1, 1))

        def __init__(self):
            self.calls = 0
            self.bound = {}

        def query(self, request):
            self.calls += 1
            self.bound[unavailable] = request
            return unavailable

        def query_transport(self, future):
            return QueryTransportState(
                self.bound[future], 0, None, "terminal", None, None, None)

    unavailable_client = UnavailableQueryClient()
    router = managed_router(retained.db, unavailable_client)

    try:
        router.prepare(WINDOW)
        wait_for_local_frame(router)
        assert router.ready(WINDOW)
        assert unavailable_client.calls == 1
        assert router._pending is None
        assert router._not_retained is False
        assert router.daemon_answer_error is None
    finally:
        router.close()

    cancelled = Future()
    cancelled.cancel()

    class CancelledQueryClient:
        generation = 0
        state = ClientState(
            verified=True, answering=True, cursor=StreamCursor("boot-A", 1, 1))

        def __init__(self):
            self.calls = 0
            self.bound = {}

        def query(self, request):
            self.calls += 1
            self.bound[cancelled] = request
            return cancelled

        def query_transport(self, future):
            return QueryTransportState(
                self.bound[future], 0, None, "terminal", None, None, None)

    cancelled_client = CancelledQueryClient()
    router = managed_router(retained.db, cancelled_client)

    try:
        router.prepare(WINDOW)
        wait_for_local_frame(router)
        assert router.ready(WINDOW)
        assert cancelled_client.calls == 1
        assert router._pending is None
        assert router._not_retained is False
        assert router._daemon_answer_error is None
    finally:
        router.close()


def test_local_read_in_flight_tracks_every_unsettled_local_read(retained):
    router = local_router(retained)
    try:
        router._local_future = None
        assert router.local_read_in_flight is False

        completed = Future()
        completed.set_result(None)
        router._local_future = completed
        assert router.local_read_in_flight is False

        cancelled = Future()
        cancelled.cancel()
        router._local_future = cancelled
        assert router.local_read_in_flight is False

        blocker_started = threading.Event()
        release_blocker = threading.Event()
        queued_started = threading.Event()
        release_queued = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            blocker = executor.submit(
                lambda: (blocker_started.set(), release_blocker.wait(5))
            )
            assert blocker_started.wait(2)
            queued = executor.submit(
                lambda: (queued_started.set(), release_queued.wait(5))
            )
            try:
                router._local_future = queued
                assert router.local_read_in_flight is True
                assert queued.running() is False
                assert queued.done() is False

                release_blocker.set()
                assert queued_started.wait(2)
                assert router.local_read_in_flight is True
                assert queued.running() is True
                assert queued.done() is False

                release_queued.set()
                queued.result(timeout=2)
                blocker.result(timeout=2)
                assert router.local_read_in_flight is False
            finally:
                release_blocker.set()
                release_queued.set()
    finally:
        router.close()


def test_queued_local_read_blocks_every_replacement_submission(retained, qtbot, monkeypatch):
    from tests.p3.test_collector_window import OFF, FakeCoordinator, FakeSetup

    from appio.ui.collector_window import CollectorWindow

    history = managed_router(retained.db, Client())

    current = Window("since_boot", "boot-A", end_mono_ns=H)
    explicit = Window("range", "boot-A", 0, H)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    blocker = history._executor.submit(
        lambda: (blocker_started.set(), release_blocker.wait(5))
    )
    assert blocker_started.wait(2)

    prepare_calls = []
    original_prepare = history.prepare

    def record_prepare(window):
        prepare_calls.append(window)
        return original_prepare(window)

    monkeypatch.setattr(history, "prepare", record_prepare)
    coordinator = FakeCoordinator()
    window = CollectorWindow(
        coordinator, history, FakeSetup(OFF), clock_ns=lambda: H - 1,
        refresh_interval_ms=1000,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window.settings)
    window.show()
    window._timer.stop()
    monkeypatch.setattr(window, "_submit_probe", lambda **_kwargs: False)
    try:
        # Seed a genuine router preparation behind the occupied single worker.
        history.prepare(current)
        wait_for_capture_admission(history)
        queued = history._local_future
        assert queued is not None and queued.running() is False and not queued.done()
        assert history.local_read_in_flight is True

        for _ in range(5):
            window._tick_target()
        window.refresh()
        window.show_window(explicit)
        assert prepare_calls == [current]
        assert window._pending is None
        assert window._deferred_target == explicit

        release_blocker.set()
        blocker.result(timeout=5)
        try:
            queued.result(timeout=10)
        except Exception:
            if not queued.cancelled():
                raise
        qtbot.waitUntil(lambda: not history.local_read_in_flight, timeout=5000)
        window._tick_target()
        assert prepare_calls == [current, explicit]
        assert window._pending == explicit
    finally:
        release_blocker.set()
        try:
            blocker.result(timeout=5)
            if history._local_future is not None:
                history._local_future.result(timeout=10)
        except Exception:
            pass
        window.shutdown()


def test_daemon_answer_error_rejects_invalid_kind_and_empty_reason(retained):
    from appio.history_router import DaemonAnswerError

    router = local_router(retained)
    state = (router.view_state, router._generation, router._not_retained)
    try:
        for kind, reason in (
            ("unknown", "reason"),
            ("internal", ""),
            ("internal", None),
        ):
            with pytest.raises(ValueError):
                DaemonAnswerError(kind, reason)
        assert (router.view_state, router._generation, router._not_retained) == state
    finally:
        router.close()
