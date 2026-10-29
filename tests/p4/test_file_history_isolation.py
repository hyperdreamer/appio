"""File availability failures must not suppress independent process history."""
import sqlite3

import pytest

from appio.contracts import Window
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry
from appio.query import QueryService
from tests.p3.test_handover import APP, BOOT, NS, group, initialized, outgoing, record, tick
from tests.p4.test_file_history_router import Client, retained


def _measured_store(path):
    store = initialized(path)
    state = outgoing(store)
    record(store, state, tick(2, 120 * NS, (110, 24, 88, 12),
                              devices=(group(1010, 204),)))
    return store


def _deny_file_watermark(monkeypatch, *, only_process_source=False):
    # Let SQLite itself reject only the file metadata read. All ownership,
    # process, device, snapshot and router operations remain real.
    initialize = QueryService.__init__

    def denied(action, table, column, database, trigger):
        if (action == sqlite3.SQLITE_READ and table == "boot"
                and column == "file_finalized_through_mono_ns"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def authorized_service(service, store_or_connection):
        initialize(service, store_or_connection)
        if only_process_source:
            # The independently retained file store remains readable; deny
            # only snapshots containing the synthetic process fixture app.
            if service.connection.execute(
                    "SELECT 1 FROM app WHERE key=?", (APP,)).fetchone() is None:
                return
        service.connection.set_authorizer(denied)

    # Do not authorize schema-reference connections: CREATE TABLE also reads
    # CHECK-constrained columns. Install only after the real service is built.
    monkeypatch.setattr(QueryService, "__init__", authorized_service)


def test_R01_boot_lookup_failure_preserves_process_T_D_history(tmp_path, monkeypatch):
    # Removing the per-source availability guard must fail this preparation,
    # not merely change a mock call count or a source-text assertion.
    store = _measured_store(tmp_path / "process.db")
    router = HistoryRouter(QueryService(store), Client(), AppRegistry())
    window = Window("range", BOOT, 100 * NS, 120 * NS)
    _deny_file_watermark(monkeypatch)
    try:
        router.prepare(window)
        router._local_future.result(timeout=10)
        assert router.ready(window)
        row, = router.ranking(window, "total_bytes")
        assert (row.app_key, row.read_bytes, row.write_bytes) == (APP, 8, 2)
        share = router.disk_share(row.app_id, window)
        assert (share.task_read_bytes, share.task_write_bytes) == (8, 2)
        assert (share.device_read_bytes, share.device_write_bytes) == (10, 4)
        series = router.series(row.app_id, window)
        assert sum(point.read_bytes for point in series.points) == 8
        assert sum(point.rbytes for point in series.device_points) == 10
        status = router.file_source_status()
        assert not status.available
        assert status.reason == "file_source_unavailable"
    finally:
        router.close()
        store.connection.close()


@pytest.mark.parametrize("bad_first", [True, False], ids=["bad-first", "bad-last"])
def test_boot_lookup_failure_does_not_hide_another_sources_file_receipt(
        tmp_path, retained, monkeypatch, bad_first):
    # A whole-loop try/except would let a bad-first source mask the good one;
    # resetting availability on a later failure would mask good-first instead.
    store = _measured_store(tmp_path / "process.db")
    bad, good = QueryService(store), QueryService(retained.db)
    local, other = (bad, good) if bad_first else (good, bad)
    router = HistoryRouter(local, Client(), AppRegistry())
    router.add_source(other, collector_id="other", owner="iod")
    window = Window("range", BOOT, 100 * NS, 120 * NS)
    _deny_file_watermark(monkeypatch, only_process_source=True)
    try:
        router.prepare(window)
        router._local_future.result(timeout=10)
        assert router.ready(window)
        assert router.file_source_status().available
        assert router.file_source_status().reason is None
    finally:
        router.close()
        store.connection.close()


@pytest.mark.parametrize("close_registered", [False, True], ids=["open", "closed"])
def test_R02_failed_preparation_marks_files_unavailable(tmp_path, close_registered):
    # This failure precedes the file lookup, so guarding that query alone
    # cannot make the test green. Never depend on a fabricated failed Future.
    store = _measured_store(tmp_path / "broken.db")
    router = HistoryRouter(QueryService(store), Client(), AppRegistry())
    window = Window("range", BOOT, 100 * NS, 120 * NS)
    store.connection.execute("DROP TABLE collector_interval")
    store.connection.commit()
    try:
        router.prepare(window)
        with pytest.raises(sqlite3.OperationalError, match="no such table: collector_interval"):
            router._local_future.result(timeout=10)
        if close_registered:
            store.connection.close()
        with pytest.raises(sqlite3.OperationalError, match="no such table: collector_interval"):
            router.ready(window)
        status = router.file_source_status()
        assert not status.available
        assert status.reason == "file_source_unavailable"
    finally:
        router.close()
        store.connection.close()
