"""Mapped-age retention and physical quota tests with observable deletion falsifiers."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from appio.contracts import FILE_HOUR_NS
from appio.durable import canonical_json
from appio.file_store import FileHourStore
from appio.files import HourlyAccumulator
from appio.quota import QuotaPruner, physical_store_bytes
from test_file_store import db
from test_files import APP, R, ZERO, observation

DAY = 86_400_000_000_000


def coverage_json(**changes):
    # An independent exact-shape fixture; '{}' is deliberately used only in corrupt-state tests.
    vector = dict(rchar=0, wchar=0, read_bytes=0, write_bytes=0)
    value = dict(inaccessible_pids=0, resets=0, reap_corrections=0,
                 reap_unapplied=vector, reap_unobserved=vector, partial_intervals=0,
                 gap_intervals=0, overlap_uncertain_intervals=0, spans_suspend_intervals=0,
                 reap_incomplete=0, counter_overflow=0, device_state="incomplete",
                 file=dict(counted_omitted_files=0, counted_omitted_bytes_r=0,
                           counted_omitted_bytes_w=0, uncounted_overflow_files=0,
                           uncounted_overflow_bytes_r=0, uncounted_overflow_bytes_w=0,
                           skipped_nonfile=0, skipped_unsupported_fs=0, weak_identity_rows=0,
                           hook_hits=dict(vfs_read=0, vfs_write=0, vfs_readv=0, vfs_writev=0,
                                          vfs_iter_read=0, vfs_iter_write=0)))
    value.update(changes)
    return canonical_json(value)


def totals(db):
    db.execute("INSERT INTO app_boot_total VALUES('boot-A',1,0,0,0,0,NULL,NULL,NULL,?)", (coverage_json(),))
    db.execute("INSERT INTO app_lifetime_total VALUES(1,0,0,0,0,?,'boot-A',0)", (coverage_json(),))
    db.execute("INSERT INTO app_boot_dev_total VALUES('boot-A',1,'259:0',0,0)")


def rollup(db, start, secs=60, app_id=1):
    db.execute("INSERT INTO rollup_app VALUES('boot-A',?,?,?,0,0,0,0,NULL,NULL,NULL,?)", (app_id, start, secs, coverage_json()))
    db.execute("INSERT INTO rollup_dev VALUES('boot-A',?,?,?,'259:0',0,0)", (app_id, start, secs))


def file_hour(db):
    acc = HourlyAccumulator("boot-A", 0)
    cov = replace(ZERO, counted_omitted_files=1, counted_omitted_bytes_r=20,
                  uncounted_overflow_files=3, uncounted_overflow_bytes_r=7)
    acc.add_observation(R, APP, observation(bytes_r=50, bytes_w=10), cov)
    FileHourStore(db).finalize_hour(acc.finalize())


def seed_core_with_materialization(db):
    # A zero-byte core observation still has one receipt and rollup/totals coverage.
    db.execute("INSERT INTO sample VALUES('boot-A','gui',1,0,1,0,0,0,NULL,NULL,1000.0,0,0,0,0,'normal',0,0)")
    db.execute("INSERT INTO sample VALUES('boot-A','gui',1,0,1,0,1,1,NULL,NULL,1000.0,0,0,0,0,'normal',0,0)")
    db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,0,?)", (coverage_json(),))
    db.execute("INSERT INTO sample_dev VALUES('boot-A','gui',1,0,1,0,'259:0',0,0)")
    for secs in (60, 3600):
        rollup(db, 0, secs)
    totals(db)
    db.commit()


@pytest.mark.parametrize("ordered", [(60_000_000_000, 0), (0, 60_000_000_000)])
def test_quota_wall_collision_uses_complete_primary_key_and_checkpoints_each_unit(db, tmp_path, ordered):
    # Falsifier: remove bucket_start_mono_ns from the final key, retaining insertion/SQL row order.
    db.execute("INSERT INTO clock_map VALUES('boot-A',60000000000,940000000000)")
    for start in ordered:
        rollup(db, start)
    totals(db)
    db.commit()
    database = Path(db.execute("PRAGMA database_list").fetchone()[2])
    # A real measured sidecar is removed by the first successful checkpoint after deletion.
    extra = tmp_path / "quota-marker"
    extra.write_bytes(b"x" * 500_000)
    db_size = physical_store_bytes(database)
    # WITHOUT ROWID scans otherwise happen to order tied buckets correctly even
    # with the primary-key suffix removed. Reverse the unspecified scan order.
    db.execute("PRAGMA reverse_unordered_selects=ON")
    pruner = QuotaPruner(db, [database, extra], quota_bytes=db_size + 200_000)
    events = []
    def trace(sql):
        events.append(sql)
        if sql.upper().startswith("DELETE FROM ROLLUP_APP"):
            extra.unlink(missing_ok=True)
    db.set_trace_callback(trace)
    result = pruner.shrink_if_needed()
    db.set_trace_callback(None)
    assert [u.primary_key for u in result.deleted] == [("boot-A", 1, 60, 0)]
    assert db.execute("SELECT bucket_start_mono_ns FROM rollup_app").fetchall() == [(60_000_000_000,)]
    assert db.execute("SELECT bucket_start_mono_ns FROM rollup_dev").fetchall() == [(60_000_000_000,)]
    delete_at = next(i for i, sql in enumerate(events) if sql.upper().startswith("DELETE FROM ROLLUP_APP"))
    assert any("COMMIT" in sql.upper() for sql in events[delete_at+1:])
    assert any("WAL_CHECKPOINT" in sql.upper() for sql in events[delete_at+1:])
    assert not result.sampling_paused


def test_physical_size_sums_database_wal_shm_and_missing_sidecars(tmp_path):
    # Falsifier: page_count or only the DB is used for physical quota.
    main = tmp_path / "store"
    main.write_bytes(b"a" * 11)
    Path(str(main) + "-wal").write_bytes(b"b" * 7)
    Path(str(main) + "-shm").write_bytes(b"c" * 3)
    assert physical_store_bytes(main) == 21
    Path(str(main) + "-wal").unlink()
    assert physical_store_bytes([main, main]) == 14


def test_expiry_uses_independent_strict_24h_14d_30d_365d_boundaries(db):
    # Falsifier: process expiry erases file hours; <= replaces strict < at the age boundary.
    seed_core_with_materialization(db)
    file_hour(db)
    paths = [Path(db.execute("PRAGMA database_list").fetchone()[2])]
    pruner = QuotaPruner(db, paths)
    assert not pruner.expire("boot-A", DAY).deleted
    result = pruner.expire("boot-A", DAY + 1)
    assert [u.kind for u in result.deleted] == ["core"]
    assert db.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM sample_dev").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM file_coverage").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM file_sample").fetchone()[0] == 1
    pruner.expire("boot-A", 14 * DAY)
    assert db.execute("SELECT count(*) FROM file_sample").fetchone()[0] == 1
    result = pruner.expire("boot-A", 14 * DAY + 1)
    assert sorted(u.kind for u in result.deleted) == ["file", "file_unknown"]
    assert db.execute("SELECT count(*) FROM file_sample").fetchone()[0] == 0
    assert db.execute("SELECT bytes_r,bytes_w FROM app_boot_file_total").fetchone() == (50, 10)
    assert db.execute("SELECT count(*) FROM file_coverage").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM file_overflow_unknown").fetchone()[0] == 0
    pruner.expire("boot-A", 30 * DAY)
    assert db.execute("SELECT bucket_secs FROM rollup_app ORDER BY bucket_secs").fetchall() == [(60,), (3600,)]
    pruner.expire("boot-A", 30 * DAY + 1)
    assert db.execute("SELECT bucket_secs FROM rollup_app").fetchall() == [(3600,)]
    pruner.expire("boot-A", 365 * DAY)
    assert db.execute("SELECT count(*) FROM rollup_app").fetchone()[0] == 1
    pruner.expire("boot-A", 365 * DAY + 1)
    assert db.execute("SELECT count(*) FROM rollup_app").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM app_boot_total").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM app_lifetime_total").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM boot").fetchone()[0] == 1


def test_prune_accounting_keeps_deleted_measured_rows_omissions_and_overflow_distinct(db):
    # Falsifier: add failed attempts to a distinct-file count or count overflow bytes as retained rows.
    file_hour(db)
    result = QuotaPruner(db, []).expire("boot-A", 15 * DAY)
    dropped = next(u for u in result.deleted if u.kind == "file")
    assert (dropped.retained_rows, dropped.bytes_r, dropped.bytes_w) == (1, 50, 10)
    assert (dropped.counted_omitted_files, dropped.counted_omitted_bytes_r, dropped.uncounted_overflow_files, dropped.uncounted_overflow_bytes_r) == (1, 20, 3, 7)
    coverage = json.loads(db.execute("SELECT coverage_json FROM app_boot_file_coverage").fetchone()[0])
    assert coverage["file"]["counted_omitted_files"] == 1
    assert coverage["file"]["uncounted_overflow_files"] == 3


@pytest.mark.parametrize("missing_now", [False, True])
def test_missing_clock_mapping_stops_before_any_delete_and_marks_incomplete(db, missing_now):
    # Falsifier: fallback to boot started_wall or delete mapped rows before discovering unmapped units.
    rollup(db, 0)
    db.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) VALUES('boot-missing',2,0,1.0,'gui')")
    db.execute("INSERT INTO rollup_app VALUES('boot-missing',1,0,60,0,0,0,0,NULL,NULL,NULL,'{}')")
    db.commit()
    before = db.execute("SELECT * FROM rollup_app ORDER BY boot_id").fetchall()
    pruner = QuotaPruner(db, [])
    result = pruner.expire("boot-missing" if missing_now else "boot-A", 400 * DAY)
    assert not result.complete
    assert result.reason == "missing_clock_mapping"
    assert not result.deleted
    assert not pruner.coverage_complete
    assert db.execute("SELECT * FROM rollup_app ORDER BY boot_id").fetchall() == before


def test_missing_materialization_prevents_raw_or_file_deletion(db):
    # Falsifier: delete source units without their durable totals/rollups, or replay additive UPSERTs.
    db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,0,'{}')")
    db.commit()
    result = QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    assert not result.complete
    assert result.reason == "missing_materialization"
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 1


def test_paths_gc_only_when_unreferenced_and_keeps_totals_aliases_and_pending_names(db):
    # Falsifier: delete all paths after hourly expiry or lose names referenced by pending state.
    file_hour(db)
    db.execute("INSERT INTO path VALUES(2,'x','259:0',20,0,'orphan','resolved',0,'boot-A',0,'boot-A',0)")
    db.execute("INSERT INTO path_alias VALUES(2,'orphan-alias','boot-A',0,'boot-A',0)")
    db.commit()
    acc = HourlyAccumulator("boot-A", FILE_HOUR_NS)
    acc.add_observation(R, APP, observation(99, basename="pending", last_seen_mono_ns=FILE_HOUR_NS+1), ZERO)
    FileHourStore(db).apply_pending(acc.finalize())
    pruner = QuotaPruner(db, [])
    pruner.expire("boot-A", 15 * DAY)
    assert db.execute("SELECT basename FROM path ORDER BY basename").fetchall() == [("file-1",), ("pending",)]
    assert db.execute("SELECT count(*) FROM path_alias").fetchone()[0] == 0
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_quota_exhaustion_pauses_and_only_fresh_successful_size_check_resumes(db, tmp_path):
    # Falsifier: report quota met after only logical deletion or resume without measuring actual bytes.
    main = Path(db.execute("PRAGMA database_list").fetchone()[2])
    pruner = QuotaPruner(db, [main], quota_bytes=1)
    result = pruner.shrink_if_needed()
    assert result.sampling_paused
    assert result.reason == "quota_exhausted"
    assert pruner.sampling_paused
    assert result.physical_bytes > 1
    pruner.quota_bytes = 1_500_000_000
    result = pruner.shrink_if_needed()
    assert not result.sampling_paused
    assert not pruner.sampling_paused


def test_gap_without_persisted_tier_coverage_is_never_erased(db):
    # Falsifier: deleting raw gap evidence while all durable tier coverage still reports no gaps.
    seed_core_with_materialization(db)
    db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,0,1,'collector_gap')")
    db.commit()
    result = QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    assert not result.complete
    assert result.reason == "missing_materialization"
    assert db.execute("SELECT count(*) FROM gap").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 1


def test_gap_with_persisted_tier_coverage_expires_without_losing_its_effects(db):
    # Falsifier: either gaps never expire, or expiry erases their accumulated uncertainty.
    seed_core_with_materialization(db)
    gap_json = coverage_json(gap_intervals=1)
    for table in ("app_boot_total", "app_lifetime_total", "rollup_app"):
        db.execute(f"UPDATE {table} SET coverage_json=?", (gap_json,))
    db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,0,1,'collector_gap')")
    db.commit()
    result = QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    assert result.complete
    assert [unit.kind for unit in result.deleted] == ["core", "gap"]
    assert db.execute("SELECT count(*) FROM gap").fetchone()[0] == 0
    assert json.loads(db.execute("SELECT coverage_json FROM app_boot_total").fetchone()[0])["gap_intervals"] == 1
    assert [json.loads(row[0])["gap_intervals"] for row in db.execute("SELECT coverage_json FROM rollup_app")] == [1, 1]


@pytest.mark.parametrize("reason", ["collector_gap", "overlap_uncertain"])
def test_b3_gap_expiry_preserves_hourly_and_boot_file_uncertainty(db, reason):
    # Falsifier: process materialization alone lets F flip from (False, 1) to (True, 0).
    from appio.contracts import Window
    from appio.query import QueryService

    seed_core_with_materialization(db)
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(bytes_r=50), ZERO)
    FileHourStore(db).finalize_hour(acc.finalize())
    gap_json = coverage_json(gap_intervals=1, overlap_uncertain_intervals=int(reason == "overlap_uncertain"))
    for table in ("app_boot_total", "app_lifetime_total", "rollup_app"):
        db.execute(f"UPDATE {table} SET coverage_json=?", (gap_json,))
    db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,0,1,?)", (reason,))
    db.commit()
    service = QueryService(db)
    hourly = Window("range", "boot-A", 0, FILE_HOUR_NS)
    lifetime = Window("lifetime")

    def coverages(window):
        return (service.file_coverage(1, window).coverage,
                service.file_table(1, window, "bytes_total", "desc", None).coverage,
                service.unknown_owner_file_coverage(window).coverage)

    for window in (hourly, lifetime):
        assert [(c.complete, c.gap_intervals) for c in coverages(window)] == [(False, 1)] * 3
    pruner = QuotaPruner(db, [])
    result = pruner.expire("boot-A", 2 * DAY)
    assert result.complete, result.reason
    assert db.execute("SELECT count(*) FROM gap").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM file_coverage").fetchone()[0] == 1
    for window in (hourly, lifetime):
        after = coverages(window)
        assert [(c.complete, c.gap_intervals) for c in after] == [(False, 1)] * 3
        assert [c.overlap_uncertain_intervals for c in after] == [int(reason == "overlap_uncertain")] * 3
    before = [db.execute(f"SELECT coverage_json FROM {table}").fetchall() for table in
              ("file_coverage", "app_boot_file_coverage", "file_overflow_unknown", "boot_file_overflow_unknown")]
    assert pruner.expire("boot-A", 2 * DAY).complete
    assert before == [db.execute(f"SELECT coverage_json FROM {table}").fetchall() for table in
                      ("file_coverage", "app_boot_file_coverage", "file_overflow_unknown", "boot_file_overflow_unknown")]
    assert pruner.expire("boot-A", 15 * DAY).complete
    assert db.execute("SELECT count(*) FROM file_coverage").fetchone()[0] == 0
    assert [(c.complete, c.gap_intervals) for c in coverages(lifetime)] == [(False, 1)] * 3


def test_b2_lagging_collector_cannot_prune_bytes_before_late_overlap_is_resolved(tmp_path):
    # Falsifier: raw history disappears while another active stream can still reveal its overlap.
    from appio.store import AppIoStore
    from test_store import BOOT, NS, process, record, snapshot, tick

    store = AppIoStore(tmp_path / "lagging-owner.sqlite")
    db = store.connection
    try:
        store.start_boot(BOOT, 1, 0, 1000.0, "gui")
        for source in ("gui", "iod"):
            store.start_collector(source, BOOT, 0)
        _, gui = record(store, None, tick(0, 0, [process()]))
        _, iod = record(store, None, tick(0, 0, [process()], source="iod"))
        record(store, gui, tick(1, 20 * NS, [process(counters=(10, 0, 10, 0))]))
        before = snapshot(db)
        pruned = QuotaPruner(db, []).expire(BOOT, 2 * DAY)
        assert not pruned.complete
        assert pruned.reason == "missing_materialization"
        assert pruned.deleted == ()
        assert snapshot(db) == before
        record(store, iod, tick(1, 2 * DAY + 20 * NS, [process(counters=(10, 0, 10, 0))], source="iod"))
        assert db.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 0
        assert db.execute("SELECT read_bytes FROM app_lifetime_total").fetchone()[0] == 0
        assert QuotaPruner(db, []).expire(BOOT, 2 * DAY + 21 * NS).complete
    finally:
        db.close()


def test_b3_gap_transfer_survives_pending_hour_finalization(db):
    # Falsifier: a gap deleted before finalization leaves pending and boot file coverage complete.
    from appio.contracts import Window
    from appio.durable import decode_file_hour_state
    from appio.query import QueryService

    seed_core_with_materialization(db)
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(bytes_r=50), ZERO)
    files = FileHourStore(db)
    files.apply_pending(acc.finalize())
    for table in ("app_boot_total", "app_lifetime_total", "rollup_app"):
        db.execute(f"UPDATE {table} SET coverage_json=?", (coverage_json(gap_intervals=1),))
    db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,0,1,'collector_gap')")
    db.commit()
    assert QuotaPruner(db, []).expire("boot-A", 2 * DAY).complete
    text, = db.execute("SELECT state_json FROM file_hour_state").fetchone()
    state = decode_file_hour_state(text, "boot-A", 0)
    assert (state.apps[0].coverage.complete, state.apps[0].coverage.gap_intervals) == (0, 1)
    assert (state.unknown_owner.coverage.complete, state.unknown_owner.coverage.gap_intervals) == (0, 1)
    assert files.finalize_hour(HourlyAccumulator.from_state(state).finalize())
    service = QueryService(db)
    for window in (Window("range", "boot-A", 0, FILE_HOUR_NS), Window("lifetime")):
        result = service.file_coverage(1, window)
        assert (result.coverage.complete, result.coverage.gap_intervals) == (False, 1)
        unknown = service.unknown_owner_file_coverage(window)
        assert (unknown.coverage.complete, unknown.coverage.gap_intervals) == (False, 1)


def test_b3_gap_delete_fault_rolls_back_file_transfer_before_retry(db):
    # Falsifier: coverage updates survive a failed deletion and a retry counts the gap twice.
    import sqlite3

    seed_core_with_materialization(db)
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(), ZERO)
    FileHourStore(db).finalize_hour(acc.finalize())
    for table in ("app_boot_total", "app_lifetime_total", "rollup_app"):
        db.execute(f"UPDATE {table} SET coverage_json=?", (coverage_json(gap_intervals=1),))
    db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,0,1,'collector_gap')")
    db.execute("CREATE TEMP TRIGGER fail_gap_delete BEFORE DELETE ON main.gap BEGIN SELECT RAISE(ABORT,'gap delete fault'); END")
    db.commit()
    tables = ("file_coverage", "app_boot_file_coverage", "file_overflow_unknown", "boot_file_overflow_unknown")
    before = [db.execute(f"SELECT * FROM {table}").fetchall() for table in tables]
    with pytest.raises(sqlite3.IntegrityError, match="gap delete fault"):
        QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    assert not db.in_transaction
    assert db.execute("SELECT count(*) FROM gap").fetchone()[0] == 1
    assert [db.execute(f"SELECT * FROM {table}").fetchall() for table in tables] == before
    db.execute("DROP TRIGGER fail_gap_delete")
    db.commit()
    assert QuotaPruner(db, []).expire("boot-A", 2 * DAY).complete
    assert [json.loads(db.execute(f"SELECT coverage_json FROM {table}").fetchone()[0])["gap_intervals"] for table in tables] == [1] * 4


def test_long_gap_requires_coverage_in_every_overlapped_bucket(db):
    # Falsifier: preserving only the end bucket still loses uncertainty in earlier buckets.
    seed_core_with_materialization(db)
    gap_json = coverage_json(gap_intervals=1)
    for table in ("app_boot_total", "app_lifetime_total"):
        db.execute(f"UPDATE {table} SET coverage_json=?", (gap_json,))
    db.execute("UPDATE rollup_app SET coverage_json=? WHERE bucket_secs=3600", (gap_json,))
    for start in (60_000_000_000, 120_000_000_000):
        rollup(db, start)
        db.execute("UPDATE rollup_app SET coverage_json=? WHERE bucket_secs=60 AND bucket_start_mono_ns=?", (gap_json, start))
    db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,0,120000000000,'collector_gap')")
    db.commit()
    result = QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    assert result.reason == "missing_materialization"
    assert result.deleted == ()
    assert db.execute("SELECT count(*) FROM gap").fetchone()[0] == 1


def test_nonzero_raw_cannot_be_deleted_just_because_empty_totals_exist(db):
    # Falsifier: presence checks accept undercounted committed totals and lose measured bytes.
    seed_core_with_materialization(db)
    db.execute("UPDATE sample SET read_bytes=5 WHERE ts_mono_ns=0")
    db.commit()
    result = QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    assert not result.complete
    assert result.reason == "missing_materialization"
    assert db.execute("SELECT SUM(read_bytes) FROM sample").fetchone()[0] == 5


def test_rollup_without_boot_and_lifetime_totals_survives_quota_exhaustion(db):
    # Falsifier: quota deletes a rollup even though its promised never-pruned totals are absent.
    rollup(db, 0)
    db.commit()
    result = QuotaPruner(db, [], quota_bytes=1).shrink_if_needed()
    assert result.sampling_paused
    assert result.reason == "missing_materialization"
    assert db.execute("SELECT count(*) FROM rollup_app").fetchone()[0] == 1


def test_pruning_never_commits_a_callers_open_transaction(db):
    # Falsifier: implicit COMMIT during checkpoint or VACUUM publishes unrelated caller writes.
    db.execute("UPDATE app SET name='uncommitted'")
    with pytest.raises(ValueError, match="transaction"):
        QuotaPruner(db, []).expire("boot-A", 400 * DAY)
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT name FROM app").fetchone()[0] == "Demo"
