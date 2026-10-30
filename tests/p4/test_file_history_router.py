"""Ownership-bound asynchronous Files views, including real protocol-1 futures.

Falsifiers: basename merge across stores; query on UI-thread SQLite; stale
future publication; dropping HMAC continuations or snapshot/scope checks;
claiming an unavailable source is a complete empty table.
"""
from dataclasses import replace
import threading

import pytest

# Pin PyQt6 before pytest-qt builds its qapp fixture (see tests/test_ui.py).
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtCore import Qt

from appio.bpf_fileio import BpfFileSource, VectorLoader
from appio.contracts import FILE_HOUR_NS as H, Window, WindowUnavailableError
from appio.file_runtime import FileRuntime
from appio.files import FileIdentityResolver
from appio.history_router import FileViewResult, HistoryRouter
from appio.iod_history import HistoryRpc
from appio.iodclient import AppRegistry, ClientState, RemoteError
from appio.ipc_models import Capabilities, QueryRequest, validate_request
from appio.query import QueryService
from appio.store import AppIoStore
from appio.ui.files_view import FilesView
from tests.p3.test_iodclient import Harness, snapshot_frame
from tests.p4.test_file_runtime import APP, ZERO, Frames, event

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


def local_router(retained):
    router = HistoryRouter(QueryService(retained.db), Client(), AppRegistry())
    router.prepare(WINDOW)
    router._local_future.result(timeout=10)
    assert router.ready(WINDOW)
    return router


def id_for(router, key=APP):
    return router.registry.id_for(key)


@pytest.mark.parametrize("sort", SORTS)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_local_file_futures_delegate_every_sort_and_NFC_casefold_search(retained, sort, direction):
    router = local_router(retained)
    try:
        expected = QueryService(retained.db).file_table(1, WINDOW, sort, direction, "CAFÉ")
        expected_cov = QueryService(retained.db).file_coverage(1, WINDOW)
        view = router.request_file_view(id_for(router), WINDOW, sort, direction, "CAFÉ").result(timeout=10)
        assert isinstance(view, FileViewResult)
        assert view.table == replace(expected, app_id=id_for(router))
        assert view.coverage == replace(expected_cov, app_id=id_for(router))
        assert len(view.table.rows) == 2
    finally:
        router.close()


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
        assert entered.wait(5)
        assert ui_sql == [], "a Files request must not execute even metadata SQL on the UI connection"
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
            router.request_file_view(id_for(router), WINDOW, "bytes_total", "desc", None).result(timeout=10)
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
        zero = router.request_file_view(id_for(router, ZERO), WINDOW, "bytes_total", "desc", None).result(timeout=10)
        assert zero.table.rows == () and zero.coverage.observed_bytes_r == 0
        assert zero.table.coverage.complete and zero.coverage.coverage.complete
        retained.db.execute("DELETE FROM file_coverage WHERE app_id=2")
        retained.db.execute("DELETE FROM app_boot_file_coverage WHERE app_id=2")
        retained.db.commit()
        # A bounded range uses hourly units, not the since-boot retained total.
        scoped = Window("range", "boot-A", 0, H)
        router.prepare(scoped)
        router._local_future.result(timeout=10)
        assert router.ready(scoped)
        with pytest.raises(WindowUnavailableError) as caught:
            router.request_file_view(id_for(router, ZERO), scoped, "bytes_total", "desc", None).result(timeout=10)
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
            router.request_file_view(id_for(router), WINDOW, "bytes_total", "desc", None).result(timeout=10)
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
    router = HistoryRouter(QueryService(retained.db), Client(), AppRegistry())
    router.add_source(QueryService(other.db), collector_id="other", owner="gui")
    # Distinct epochs constitute unproved concurrent ownership even for matching AppKey.
    other.db.execute("UPDATE collector_interval SET source='gui',epoch=7")
    other.db.commit()
    try:
        router.prepare(WINDOW)
        router._local_future.result(timeout=10)
        assert router.ready(WINDOW)
        with pytest.raises(WindowUnavailableError) as caught:
            router.request_file_view(id_for(router), WINDOW, "basename", "asc", None).result(timeout=10)
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
        qtbot.waitUntil(lambda: view.state != "loading", timeout=10_000)
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
    def __init__(self, tmp_path, retained):
        self.h = Harness()
        self.h.handshake()
        msg = snapshot_frame(1, boot_id="boot-A", epoch=1)
        msg["capabilities"] = dict(proc_all=True, dev_stat=True, file_io=True, bpf_loaded=True)
        self.h.send(msg)
        self.h.client.pump()
        self.rpc = HistoryRpc(retained.db)
        self.store = AppIoStore(tmp_path / "local-empty.db")
        self.router = HistoryRouter(QueryService(self.store), self.h.client, AppRegistry())
        self.requests = []
        self.router.prepare(WINDOW)
        self.router._local_future.result(timeout=10)
        for _ in range(15):
            self.respond()
            if self.router.ready(WINDOW):
                break
        assert self.router.ready(WINDOW) and self.router._remote is not None

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
            self.h.send(reply)
        self.h.client.pump()

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
    remote = Remote(tmp_path, retained)
    try:
        aid = id_for(remote.router)
        future = remote.router.request_file_view(aid, WINDOW, sort, direction, "CAFÉ")
        view = remote.finish(future)
        expected = QueryService(retained.db).file_table(1, WINDOW, sort, direction, "CAFÉ")
        assert view.table.rows == tuple(replace(row, path_id=0) for row in expected.rows)
        assert view.table.coverage == expected.coverage
        assert view.coverage.observed_bytes_r == 46
        files = [r for r in remote.requests if r.kind == "files"]
        assert len(files) == 2 and files[0].cursor is None and files[1].cursor
        assert all((r.sort, r.direction, r.search) == (sort, direction, "CAFÉ") for r in files)
        assert any(r.kind == "coverage" and r.coverage_kind == "file" for r in remote.requests)
        assert remote.router.file_source_status().available
    finally:
        remote.close()


@pytest.mark.parametrize("fault", ["snapshot", "cursor", "stale", "coverage_snapshot", "mixed_tier", "scope", "advanced_cursor", "app", "coverage_app"])
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
        def corrupt(reply):
            nonlocal cursor_corrupted
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
        for _ in range(15):
            remote.respond(corrupt)
            if future.done():
                break
        with pytest.raises((WindowUnavailableError, RemoteError)) as caught:
            future.result(timeout=5)
        assert caught.value.reason in ("file_source_unavailable", "stale_cursor")
        if fault == "cursor":
            assert caught.value.reason == "stale_cursor"
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
    def held(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(router, "_read_local_files", held)
    futures = []
    try:
        # Hold the router's single worker at a barrier before cancellation;
        # otherwise canceled-work skipping could make admission pass vacuously.
        futures.append(router.request_file_view(id_for(router), WINDOW, "bytes_total", "desc", None))
        assert entered.wait(5)
        futures[0].cancel()
        for _ in range(23):
            future = router.request_file_view(id_for(router), WINDOW, "bytes_total", "desc", None)
            futures.append(future)
            future.cancel()
        assert router._executor._work_queue.qsize() <= 8
    finally:
        release.set()
        router.close()
