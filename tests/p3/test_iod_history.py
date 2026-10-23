"""T-003: bounds fail closed and remote results retain the P1 contracts."""
import json
import sqlite3
from dataclasses import replace

import pytest

from appio.contracts import (
    HISTORICAL_BOOT_PREFIX_MESSAGE, LAUNCH_WINDOW_MESSAGE, MAX_RANGE_NS,
    RAW_BUCKET_NS, Window,
)
from appio.ipc_models import QueryRequest, validate_request, validate_server_message
from appio.ipc_protocol import ProtocolError, encode_frame
from appio.iod_history import HistoryRpc, QueryLimits, from_wire, to_wire
from appio.migrations import migrate_empty
from appio.query import QueryService

APP = "unit:app-demo.service"
BOOT = "boot-A"
DAY = 86400 * 10**9
SHORT = Window("range", BOOT, 0, RAW_BUCKET_NS)


def coverage_json(**changes):
    vector = dict(rchar=0, wchar=0, read_bytes=0, write_bytes=0)
    value = dict(
        inaccessible_pids=0, resets=0, reap_corrections=0,
        reap_unapplied=vector, reap_unobserved=vector, partial_intervals=0,
        gap_intervals=0, overlap_uncertain_intervals=0, spans_suspend_intervals=0,
        reap_incomplete=0, counter_overflow=0, device_state="incomplete",
        file=dict(counted_omitted_files=0, counted_omitted_bytes_r=0,
                  counted_omitted_bytes_w=0, uncounted_overflow_files=0,
                  uncounted_overflow_bytes_r=0, uncounted_overflow_bytes_w=0,
                  skipped_nonfile=0, skipped_unsupported_fs=0, weak_identity_rows=0,
                  hook_hits=dict(vfs_read=0, vfs_write=0, vfs_readv=0,
                                 vfs_writev=0, vfs_iter_read=0, vfs_iter_write=0)))
    value.update(changes)
    return json.dumps(value)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    migrate_empty(connection)
    connection.execute("INSERT INTO boot VALUES(?,1,0,0,1000.0,'iod')", (BOOT,))
    connection.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    connection.execute("INSERT INTO collector_interval VALUES('iod',?,1,0,?)", (BOOT, RAW_BUCKET_NS))
    connection.execute("INSERT INTO clock_map VALUES(?,0,1000000000000)", (BOOT,))
    connection.commit()
    yield connection
    connection.close()


def raw(db, *, count=1, state="available", rich=False):
    cov = json.loads(coverage_json(device_state=state))
    if rich:
        cov.update(reap_corrections=2, spans_suspend_intervals=1,
                   reap_unapplied=dict(rchar=1, wchar=2, read_bytes=3, write_bytes=4),
                   reap_unobserved=dict(rchar=5, wchar=6, read_bytes=7, write_bytes=8))
        cov["file"]["counted_omitted_files"] = 2
        cov["file"]["hook_hits"]["vfs_readv"] = 7
    for index in range(count):
        start = index * RAW_BUCKET_NS
        db.execute(
            "INSERT INTO sample VALUES(?,'iod',1,?,1,0,?,?,?, ?,1000.0,11,13,4,3,'normal',0,?)",
            (BOOT, index, start, start + RAW_BUCKET_NS, start, RAW_BUCKET_NS, int(rich)),
        )
        db.execute("INSERT INTO sample_coverage VALUES(?,'iod',1,?,1,?,?)",
                   (BOOT, index, start, json.dumps(cov)))
        db.execute("INSERT INTO sample_dev VALUES(?,'iod',1,?,1,?,'259:0',9,8)", (BOOT, index, start))
    db.execute("UPDATE collector_interval SET to_mono_ns=?", (count * RAW_BUCKET_NS,))
    db.commit()


def decode(reply, kind):
    assert reply["type"] == "result", reply
    validate_server_message(reply)
    encode_frame(reply)
    return from_wire(reply["payload"], app_ids=lambda key: 1 if key == APP else 900, result_kind=kind)


def test_remote_launch_rejection_precedes_unknown_app_lookup():
    connection = sqlite3.connect(":memory:")
    migrate_empty(connection)
    try:
        reply = HistoryRpc(connection).execute(
            QueryRequest(kind="series", window=Window(kind="launch", boot_id="b", launch_id=99),
                         app_key="exe:/missing"), request_id="q1")
        assert reply["type"] == "error"
        assert reply["reason"] == "launch_attribution"
        assert reply["message"] == LAUNCH_WINDOW_MESSAGE
        assert "app_id" not in str(reply)
    finally:
        connection.close()


@pytest.mark.parametrize("state", ["available", "not_delegated", "incomplete"])
def test_all_numeric_optional_and_coverage_fields_match_domain(db, state):
    # Falsifier: clamp negative residuals, coerce None to zero, or reorder reap vectors.
    raw(db, state=state, rich=True)
    service, rpc = QueryService(db), HistoryRpc(db)
    for kind, expected in (
        ("ranking", service.ranking(SHORT, "total_bytes")),
        ("series", service.series(1, SHORT)),
        ("totals", service.disk_share(1, SHORT)),
        ("coverage", service.disk_share(1, SHORT).coverage),
    ):
        reply = rpc.execute(QueryRequest(kind, SHORT, app_key=None if kind == "ranking" else APP), request_id=kind)
        assert decode(reply, kind) == expected
        assert "app_id" not in json.dumps(reply)
        assert not db.in_transaction
    share = service.disk_share(1, SHORT)
    if state == "available":
        assert (share.residual_read, share.residual_write, share.residual_total) == (-5, -5, -10)
    else:
        assert share.device_total_bytes is None and share.residual_total is None


@pytest.mark.parametrize("duration,tier,want", [(RAW_BUCKET_NS, "raw", 4), (2*DAY, "rollup_60s", 100), (31*DAY, "rollup_3600s", 1000)])
def test_rpc_uses_domain_tier_without_blending(db, duration, tier, want):
    # Falsifier: independently select a tier or add raw and rollup amounts together.
    raw(db)
    for seconds, amount in ((60, 100), (3600, 1000)):
        db.execute("INSERT INTO rollup_app VALUES(?,1,0,?,?,0,0,0,NULL,NULL,NULL,?)",
                   (BOOT, seconds, amount, coverage_json()))
    db.commit()
    window = Window("range", BOOT, 0, duration)
    service, rpc = QueryService(db), HistoryRpc(db)
    for kind, expected in (("ranking", service.ranking(window, "total_bytes")),
                           ("series", service.series(1, window)),
                           ("totals", service.disk_share(1, window)),
                           ("coverage", service.disk_share(1, window).coverage)):
        result = decode(rpc.execute(QueryRequest(kind, window, None if kind == "ranking" else APP), request_id="tier"), kind)
        assert result == expected
    row, = service.ranking(window, "total_bytes")
    assert row.read_bytes == want and row.coverage.tier == tier


def test_expired_raw_window_stays_unavailable(db):
    db.execute("INSERT INTO rollup_app VALUES(?,1,0,60,999,0,0,0,NULL,NULL,NULL,?)", (BOOT, coverage_json()))
    db.commit()
    result = decode(HistoryRpc(db).execute(QueryRequest("series", SHORT, APP), request_id="old"), "series")
    assert result.points == () and result.tier == "raw"
    assert result.coverage.unavailable_reason == "not_retained"


def test_lifetime_and_exact_boot_use_summaries_but_prefix_does_not(db):
    cutoff = MAX_RANGE_NS + 2
    db.execute("INSERT INTO sampler_checkpoint VALUES('iod',?,1,1,?,'{}')", (BOOT, cutoff))
    db.execute("INSERT INTO app_boot_total VALUES(?,1,7,8,9,10,1,2,3,?)", (BOOT, coverage_json()))
    db.execute("INSERT INTO app_lifetime_total VALUES(1,7,8,9,10,?,?,0)", (coverage_json(), BOOT))
    db.commit()
    rpc = HistoryRpc(db)
    for window in (Window("lifetime"), Window("since_boot", BOOT, end_mono_ns=cutoff)):
        actual = decode(rpc.execute(QueryRequest("totals", window, APP), request_id="total"), "totals")
        assert actual == QueryService(db).disk_share(1, window)
        assert actual.task_total_bytes == 15 and actual.coverage.tier == "totals_only"
    reply = rpc.execute(QueryRequest("totals", Window("since_boot", BOOT, end_mono_ns=cutoff-1), "exe:/missing"), request_id="prefix")
    assert reply["reason"] == "unsupported_window"
    assert reply["message"] == HISTORICAL_BOOT_PREFIX_MESSAGE


@pytest.mark.parametrize("kind", ["ranking", "files"])
def test_limit_501_rejected_before_any_database_query(db, kind):
    # Falsifier: dispatch first and slice the resulting list to the requested page size.
    statements = []
    db.set_trace_callback(statements.append)
    try:
        reply = HistoryRpc(db).execute(QueryRequest(kind, SHORT, APP if kind != "ranking" else None, limit=501), request_id="limit")
    finally:
        db.set_trace_callback(None)
    assert reply["reason"] == "invalid_request"
    assert statements == []


@pytest.mark.parametrize("changes", [dict(metric="SELECT * FROM app"), dict(direction="sideways"), dict(search="x\0y"), dict(sort="sql"), dict(coverage_kind="other")])
def test_bad_typed_requests_do_not_reach_identity_lookup(db, changes):
    reply = HistoryRpc(db).execute(QueryRequest("series", SHORT, "exe:/missing", **changes), request_id="bad")
    assert reply["reason"] == "invalid_request"


def test_point_rejection_precedes_domain_allocation_and_totals_still_work(db, monkeypatch):
    # Falsifier: delete the distinct-bucket preflight and allow QueryService.series to allocate.
    raw(db, count=4001, state="not_delegated")
    window = Window("range", BOOT, 0, 4001 * RAW_BUCKET_NS)
    def forbidden(*args):
        pytest.fail("unbounded series allocation happened before rejection")
    monkeypatch.setattr(QueryService, "series", forbidden)
    rpc = HistoryRpc(db, limits=QueryLimits(timeout_ns=10**10))
    reply = rpc.execute(QueryRequest("series", window, APP, limit=4000), request_id="points")
    assert reply["reason"] == "query_too_large"
    assert decode(rpc.execute(QueryRequest("totals", window, APP), request_id="same-scope"), "totals").task_total_bytes == 28007
    assert decode(rpc.execute(QueryRequest("coverage", window, APP), request_id="coverage"), "coverage").tier == "raw"


def test_scan_budget_rejects_before_materializing_and_is_scoped(db, monkeypatch):
    raw(db, count=8)
    window = Window("range", BOOT, 0, 8 * RAW_BUCKET_NS)
    def forbidden(*args):
        pytest.fail("domain materialization preceded scan rejection")
    monkeypatch.setattr(QueryService, "disk_share", forbidden)
    reply = HistoryRpc(db, limits=QueryLimits(max_scan_rows=5)).execute(QueryRequest("totals", window, APP), request_id="scan")
    assert reply["reason"] == "query_too_large"
    assert not db.in_transaction


def test_ranking_pages_preserve_all_501_rows_and_full_utf8_ties(db):
    # Falsifier: LIMIT before sorting, truncate to 500, or use local app_id as a tie key.
    db.execute("DELETE FROM app")
    for index in range(501):
        aid = 501 - index
        key = f"unit:app-{index:04}.service"
        db.execute("INSERT INTO app VALUES(?,?,?,NULL,0.0,0.0,'user')", (aid, key, key))
        db.execute("INSERT INTO app_lifetime_total VALUES(?,10,2,3,4,?,?,0)", (aid, coverage_json(), BOOT))
    db.commit()
    rpc = HistoryRpc(db, limits=QueryLimits(timeout_ns=10**10))
    request = QueryRequest("ranking", Window("lifetime"))
    seen, pages = [], 0
    while True:
        reply = rpc.execute(request, request_id="rank")
        rows = decode(reply, "ranking")
        assert 0 < len(rows) <= 500
        seen.extend(row.app_key for row in rows)
        page, = reply["payload"]
        pages += 1
        assert not db.in_transaction
        if page["next_cursor"] is None:
            break
        request = replace(request, cursor=page["next_cursor"])
        assert pages < 100
    assert seen == [f"unit:app-{index:04}.service" for index in range(501)]
    assert pages > 1


@pytest.mark.parametrize("change", ["tamper", "metric", "window", "direction", "data"])
def test_cursor_cannot_change_scope_order_or_snapshot(db, change):
    for aid in (2, 3):
        db.execute("INSERT INTO app VALUES(?,?,?,NULL,0.0,0.0,'user')", (aid, f"unit:a{aid}.service", "App"))
    for aid in (1, 2, 3):
        db.execute("INSERT INTO app_lifetime_total VALUES(?,1,0,0,0,?,?,0)", (aid, coverage_json(), BOOT))
    db.commit()
    rpc = HistoryRpc(db)
    req = QueryRequest("ranking", Window("lifetime"), limit=1)
    first = rpc.execute(req, request_id="first")
    cursor = first["payload"][0]["next_cursor"]
    assert cursor
    if change == "tamper":
        req = replace(req, cursor=("A" if cursor[0] != "A" else "B") + cursor[1:])
    elif change == "metric":
        req = replace(req, cursor=cursor, metric="rchar")
    elif change == "window":
        req = replace(req, cursor=cursor, window=SHORT)
    elif change == "direction":
        req = replace(req, cursor=cursor, direction="asc")
    else:
        req = replace(req, cursor=cursor)
        db.execute("UPDATE app SET name='Changed' WHERE app_id=1")
        db.commit()
    reply = rpc.execute(req, request_id="next")
    assert reply["reason"] == "stale_cursor"
    assert "restart" in reply["message"].lower()


def test_byte_overflow_returns_small_explicit_error_and_preserves_connection(db):
    raw(db)
    db.execute("UPDATE app SET name=?", ("é" * 140000,))
    db.commit()
    reply = HistoryRpc(db).execute(QueryRequest("ranking", SHORT), request_id="bytes")
    assert reply["reason"] == "query_too_large"
    assert len(encode_frame(reply)) < 512
    assert not db.in_transaction


def test_oversized_name_rejected_before_ranking_but_totals_remain_available(db, monkeypatch):
    raw(db)
    db.execute("UPDATE app SET name=?", ("é" * 140000,))
    db.commit()
    def forbidden(*args):
        pytest.fail("oversized text reached domain allocation")
    monkeypatch.setattr(QueryService, "ranking", forbidden)
    rpc = HistoryRpc(db)
    assert rpc.execute(QueryRequest("ranking", SHORT), request_id="name")["reason"] == "query_too_large"
    assert decode(rpc.execute(QueryRequest("totals", SHORT, APP), request_id="small"), "totals").task_total_bytes == 7


def test_tight_payload_cap_is_enforced_without_removing_coverage(db):
    raw(db, rich=True)
    reply = HistoryRpc(db, limits=QueryLimits(max_payload_bytes=1000)).execute(QueryRequest("totals", SHORT, APP), request_id="bytes")
    assert reply["reason"] == "query_too_large"
    assert len(encode_frame(reply)) - 4 <= 1000


def test_late_domain_result_is_discarded_and_busy_timeout_restored(db, monkeypatch):
    raw(db)
    now = [0]
    original = QueryService.disk_share
    def late(self, *args):
        result = original(self, *args)
        now[0] = 201000000
        return result
    monkeypatch.setattr(QueryService, "disk_share", late)
    before = db.execute("PRAGMA busy_timeout").fetchone()[0]
    reply = HistoryRpc(db, clock_ns=lambda: now[0]).execute(QueryRequest("totals", SHORT, APP), request_id="late")
    assert reply["reason"] == "query_timeout"
    assert not db.in_transaction
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == before
    assert db.execute("SELECT 1").fetchone() == (1,)


def test_sql_progress_interrupts_expensive_domain_work(db, monkeypatch):
    raw(db)
    calls = [0]
    armed = [False]
    def clock():
        if armed[0]:
            calls[0] += 1
        return calls[0] * 50000000
    def expensive(self, *args):
        armed[0] = True
        self.connection.execute("WITH RECURSIVE x(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM x WHERE n<10000000) SELECT sum(n) FROM x").fetchone()
        pytest.fail("SQL was not interrupted")
    monkeypatch.setattr(QueryService, "disk_share", expensive)
    reply = HistoryRpc(db, clock_ns=clock).execute(QueryRequest("totals", SHORT, APP), request_id="interrupt")
    assert reply["reason"] == "query_timeout"
    assert not db.in_transaction and db.execute("SELECT 1").fetchone() == (1,)


def test_files_and_unknown_owner_remain_explicitly_unavailable(db):
    rpc, service = HistoryRpc(db), QueryService(db)
    for sort in ("bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen"):
        for direction in ("asc", "desc"):
            request = QueryRequest("files", SHORT, APP, sort=sort, direction=direction, search="Cafe\u0301")
            result = decode(rpc.execute(request, request_id="files"), "files")
            assert result == service.file_table(1, SHORT, sort, direction, request.search)
            assert result.rows == () and result.coverage.unavailable_reason == "file_source_unavailable"
    for coverage_kind, expected in (("file", service.file_coverage(1, SHORT)), ("unknown_owner", service.unknown_owner_file_coverage(SHORT))):
        reply = rpc.execute(QueryRequest("coverage", SHORT, APP if coverage_kind == "file" else None, coverage_kind=coverage_kind), request_id="coverage")
        assert decode(reply, "coverage") == expected


def test_appkeys_remap_deliberately_different_database_ids(db):
    raw(db, rich=True)
    other = sqlite3.connect(":memory:")
    migrate_empty(other)
    other.execute("INSERT INTO app VALUES(77,?,'Demo',NULL,0.0,0.0,'user')", (APP,))
    try:
        local_ids = lambda key: other.execute("SELECT app_id FROM app WHERE key=?", (key,)).fetchone()[0]
        for kind, result in (("series", QueryService(db).series(1, SHORT)), ("totals", QueryService(db).disk_share(1, SHORT)), ("ranking", QueryService(db).ranking(SHORT, "total_bytes"))):
            wire = json.loads(json.dumps(to_wire(result, app_keys={1: APP})))
            assert "app_id" not in json.dumps(wire)
            actual = from_wire(wire, app_ids=local_ids, result_kind=kind)
            expected = [replace(row, app_id=77) for row in result] if kind == "ranking" else replace(result, app_id=77)
            assert actual == expected
    finally:
        other.close()


def test_existing_transaction_is_never_rolled_back_by_rpc(db):
    db.execute("UPDATE app SET name='Uncommitted'")
    reply = HistoryRpc(db).execute(QueryRequest("totals", SHORT, APP), request_id="transaction")
    assert reply["reason"] == "history_unavailable"
    assert db.in_transaction and db.execute("SELECT name FROM app").fetchone()[0] == "Uncommitted"
    db.rollback()


def test_sqlite_error_is_bounded_and_does_not_leak_sql_or_paths(db):
    db.execute("DROP TABLE sample")
    reply = HistoryRpc(db).execute(QueryRequest("series", SHORT, APP), request_id="broken")
    assert reply["reason"] == "history_unavailable"
    assert "sample" not in reply["message"] and "SELECT" not in reply["message"]
    validate_server_message(reply)


@pytest.fixture
def retained_files(db):
    # Existing P1 retained input, not a P3 file producer or BPF test.
    hour = 3600 * 10**9
    db.execute("UPDATE boot SET file_finalized_through_mono_ns=?", (hour,))
    db.execute("UPDATE collector_interval SET to_mono_ns=?", (hour,))
    db.execute("INSERT INTO app_boot_file_coverage VALUES(?,1,?)", (BOOT, coverage_json(device_state="available")))
    db.execute("INSERT INTO boot_file_overflow_unknown VALUES(?,'overflow_owner_unknown',2,3,4,?)", (BOOT, coverage_json()))
    names = ("Café", "Cafe\u0301", "zeta", "alpha", "alpha", "alpha", "alpha", "beta")
    for index, name in enumerate(names):
        path_id = 100 - index
        dev = "8:0" if index < 5 else "8:1"
        inode, generation = 10 + index % 3, index // 3
        db.execute("INSERT INTO path VALUES(?,'fs',?,?,?,?, 'resolved',0,?,0,?,1)",
                   (path_id, dev, inode, generation, name, BOOT, BOOT))
        db.execute("INSERT INTO app_boot_file_total VALUES(?,1,?,10,5,2,3,?)", (BOOT, path_id, index + 1))
    db.commit()
    return db


@pytest.mark.parametrize("sort", ["bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_file_pages_follow_full_domain_sort_with_all_tie_keys(retained_files, sort, direction):
    expected = QueryService(retained_files).file_table(1, Window("lifetime"), sort, direction, None)
    rpc = HistoryRpc(retained_files)
    request = QueryRequest("files", Window("lifetime"), APP, sort=sort, direction=direction, limit=3)
    seen = []
    for _ in range(10):
        reply = rpc.execute(request, request_id="file-page")
        actual = decode(reply, "files")
        assert actual.coverage == expected.coverage
        assert actual.unknown_owner_overflow == expected.unknown_owner_overflow
        assert 0 < len(actual.rows) <= 3
        assert "path_id" not in json.dumps(reply)
        seen.extend(actual.rows)
        cursor = reply["payload"][0]["next_cursor"]
        if cursor is None:
            break
        request = replace(request, cursor=cursor)
    assert seen == [replace(row, path_id=0) for row in expected.rows]
    assert len(seen) == 8


def test_nfc_file_search_precedes_paging_and_cursor_binds_search(retained_files):
    rpc = HistoryRpc(retained_files)
    request = QueryRequest("files", Window("lifetime"), APP, search="Cafe\u0301", limit=1)
    first = rpc.execute(request, request_id="first")
    first_rows = decode(first, "files").rows
    cursor = first["payload"][0]["next_cursor"]
    assert len(first_rows) == 1 and cursor
    second = decode(rpc.execute(replace(request, cursor=cursor), request_id="second"), "files")
    assert len(second.rows) == 1
    assert {row.basename for row in (*first_rows, *second.rows)} == {"Café", "Cafe\u0301"}
    changed = rpc.execute(replace(request, cursor=cursor, search="alpha"), request_id="changed")
    assert changed["reason"] == "stale_cursor"


@pytest.mark.parametrize("kind", ["ranking", "series", "files", "coverage", "totals"])
def test_small_error_cap_handles_escaped_request_id_without_trimming_reason(db, kind):
    raw(db)
    rpc = HistoryRpc(db, limits=QueryLimits(max_payload_bytes=256))
    reply = rpc.execute(QueryRequest(kind, SHORT, APP if kind != "ranking" else None), request_id="\\" * 64)
    assert reply["type"] == "error" and reply["reason"] == "query_too_large"
    validate_server_message(reply)
    assert len(encode_frame(reply)) - 4 <= 256
    rejected = rpc.execute(QueryRequest("series", Window("launch", BOOT, launch_id=99), "exe:/missing"), request_id="\\" * 64)
    assert rejected["message"] == LAUNCH_WINDOW_MESSAGE
    assert len(encode_frame(rejected)) - 4 <= 256


@pytest.mark.parametrize("kind,changes", [("files", {"sort": "rchar"}), ("series", {"search": "ignored"}), ("files", {"metric": "rchar"}), ("totals", {"sort": "dev"})])
def test_inapplicable_typed_selectors_reject_before_sql(db, kind, changes):
    statements = []
    db.set_trace_callback(statements.append)
    try:
        reply = HistoryRpc(db).execute(QueryRequest(kind, SHORT, APP, **changes), request_id="selectors")
    finally:
        db.set_trace_callback(None)
    assert reply["reason"] == "invalid_request" and not statements


def test_reentrant_query_cannot_start_or_rollback_the_active_snapshot(db, monkeypatch):
    raw(db)
    rpc, original = HistoryRpc(db), QueryService.disk_share
    def nested(self, *args):
        assert self.connection.in_transaction
        rejection = rpc.execute(QueryRequest("totals", SHORT, APP), request_id="overlap")
        assert rejection["reason"] == "history_unavailable"
        assert self.connection.in_transaction
        return original(self, *args)
    monkeypatch.setattr(QueryService, "disk_share", nested)
    assert decode(rpc.execute(QueryRequest("totals", SHORT, APP), request_id="outer"), "totals").task_total_bytes == 7
    assert not db.in_transaction


def test_closed_connection_returns_a_bounded_error(db):
    rpc = HistoryRpc(db)
    db.close()
    reply = rpc.execute(QueryRequest("totals", SHORT, APP), request_id="closed")
    assert reply["reason"] == "history_unavailable"


def test_rpc_joins_one_pinned_wal_snapshot_and_detects_external_commit(db, tmp_path, monkeypatch):
    raw(db)
    for aid in (1, 2):
        if aid == 2:
            db.execute("INSERT INTO app VALUES(2,'exe:/other','Other',NULL,0.0,0.0,'user')")
        db.execute("INSERT INTO app_lifetime_total VALUES(?,1,0,0,0,?,?,0)", (aid, coverage_json(), BOOT))
    db.commit()
    reader = sqlite3.connect(tmp_path / "history.sqlite")
    db.backup(reader)
    reader.execute("PRAGMA journal_mode=WAL")
    writer = sqlite3.connect(tmp_path / "history.sqlite")
    try:
        rpc = HistoryRpc(reader)
        request = QueryRequest("ranking", Window("lifetime"), limit=1)
        first = rpc.execute(request, request_id="page")
        cursor = first["payload"][0]["next_cursor"]
        assert cursor and not reader.in_transaction
        writer.execute("UPDATE app SET name='external' WHERE app_id=2")
        writer.commit()
        assert rpc.execute(replace(request, cursor=cursor), request_id="stale")["reason"] == "stale_cursor"
        original = QueryService.ranking
        def commit_during_domain(self, *args):
            writer.execute("UPDATE sample SET read_bytes=90")
            writer.commit()
            return original(self, *args)
        monkeypatch.setattr(QueryService, "ranking", commit_during_domain)
        pinned = decode(rpc.execute(QueryRequest("ranking", SHORT), request_id="pinned"), "ranking")
        assert pinned[0].read_bytes == 4
        monkeypatch.setattr(QueryService, "ranking", original)
        fresh = decode(rpc.execute(QueryRequest("ranking", SHORT), request_id="fresh"), "ranking")
        assert fresh[0].read_bytes == 90
        assert not reader.in_transaction
    finally:
        writer.close()
        reader.close()


def test_half_open_clipped_window_does_not_prorate_or_fill_missing_points(db):
    raw(db, count=3)
    window = Window("range", BOOT, RAW_BUCKET_NS//2, 5*RAW_BUCKET_NS//2)
    result = decode(HistoryRpc(db).execute(QueryRequest("series", window, APP), request_id="edges"), "series")
    assert result == QueryService(db).series(1, window)
    assert [(point.bucket_start_mono_ns, point.read_bytes) for point in result.points] == [(RAW_BUCKET_NS, 4)]
    assert not result.coverage.complete


def test_maximum_sqlite_counter_stays_an_exact_integer(db):
    raw(db)
    maximum = 9223372036854775807
    db.execute("UPDATE sample SET read_bytes=?,write_bytes=0", (maximum,))
    db.commit()
    actual = decode(HistoryRpc(db).execute(QueryRequest("totals", SHORT, APP), request_id="integer"), "totals")
    assert type(actual.task_total_bytes) is int and actual.task_total_bytes == maximum
    assert actual == QueryService(db).disk_share(1, SHORT)


def test_totals_only_does_not_materialize_expired_raw_rows(db):
    cutoff = MAX_RANGE_NS + 2
    db.execute("INSERT INTO sampler_checkpoint VALUES('iod',?,1,1,?,'{}')", (BOOT, cutoff))
    db.execute("INSERT INTO app_boot_total VALUES(?,1,7,8,9,10,1,2,3,?)", (BOOT, coverage_json()))
    db.executemany("INSERT INTO sample VALUES(?,'iod',1,?,1,0,0,0,NULL,NULL,1000.0,0,0,999,0,'normal',0,0)",
                   ((BOOT, index) for index in range(50001)))
    db.commit()
    window = Window("since_boot", BOOT, end_mono_ns=cutoff)
    rpc = HistoryRpc(db)
    total = decode(rpc.execute(QueryRequest("totals", window, APP), request_id="summary"), "totals")
    coverage = decode(rpc.execute(QueryRequest("coverage", window, APP), request_id="coverage"), "coverage")
    assert total.task_total_bytes == 15 and coverage.tier == "totals_only"


def test_protocol_producer_ranking_cursor_gap_is_explicit(db):
    # This test used to CHARACTERIZE the dependency blocker: ranking + cursor was
    # rejected with invalid_selector, which closed the connection mid-continuation.
    # The defect is now fixed (cursor is a ranking selector and the client sends it),
    # so the characterization inverts: the request must be accepted and the cursor
    # must survive validation, or paging silently restarts at page one.
    message = {"protocol": 1, "type": "query", "request_id": "page",
               "query": {"kind": "ranking", "window": {"kind": "lifetime"}, "cursor": "opaque"}}
    request = validate_request(message)
    assert request.query.kind == "ranking"
    assert request.query.cursor == "opaque"
