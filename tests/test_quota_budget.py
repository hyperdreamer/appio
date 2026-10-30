"""Retention budget probes: real SQLite, synthetic evidence, no live inputs."""
from time import perf_counter

import pytest

from appio.quota import QuotaPruner
from test_file_store import db
from test_quota import DAY, coverage_json, seed_core_with_materialization


def test_one_bad_unit_does_not_block_ready_units(db):
    seed_core_with_materialization(db)
    db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,99,1,1,'{}')")
    db.commit()
    result = QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    assert [(u.kind, u.primary_key) for u in result.deleted] == [
        ("core", ("boot-A", "gui", 1, 0, 1))
    ]
    assert db.execute("SELECT seq FROM sample_coverage").fetchall() == [(99,)]
    assert not result.complete and result.reason == "missing_materialization"
    assert [(u.kind, u.primary_key, u.reason) for u in result.skipped] == [
        ("core", ("boot-A", "gui", 1, 99, 1), "missing_materialization")
    ]


def _measure(db, rows):
    seed_core_with_materialization(db)
    # The same app/boot and covering index as the expiring rows. Filtering by a
    # different boot or skipping coverage decoding cannot make this probe pass.
    db.executemany(
        "INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,'{}')",
        ((i + 1, DAY + i) for i in range(rows)),
    )
    db.commit()
    steps = 0
    def progress():
        nonlocal steps
        steps += 1
    db.set_progress_handler(progress, 1)
    start = perf_counter()
    try:
        result = QuotaPruner(db, []).expire("boot-A", 2 * DAY)
    finally:
        elapsed = perf_counter() - start
        db.set_progress_handler(None, 0)
    assert result.complete, result.reason
    assert len(result.deleted) == 1
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone() == (rows,)
    assert db.execute("SELECT min(ts_mono_ns) FROM sample_coverage").fetchone() == (DAY,)
    return elapsed, steps


def test_retention_cost_stays_bounded_with_ten_times_retained_rows(db, capsys):
    # VM steps make the scaling claim stable under shared-host timing noise;
    # wall time is also bounded and printed for the acceptance receipt.
    small = _measure(db, 20_000)
    db.execute("DELETE FROM sample_coverage")
    db.execute("DELETE FROM sample")
    db.execute("DELETE FROM sample_dev")
    db.execute("DELETE FROM rollup_dev")
    db.execute("DELETE FROM rollup_app")
    db.execute("DELETE FROM app_boot_dev_total")
    db.execute("DELETE FROM app_boot_total")
    db.execute("DELETE FROM app_lifetime_total")
    db.commit()
    large = _measure(db, 200_000)
    with capsys.disabled():
        print(f"\nretention scaling: 20000 rows {small[0]*1000:.3f} ms / {small[1]} VM steps; "
              f"200000 rows {large[0]*1000:.3f} ms / {large[1]} VM steps; "
              f"time ratio {large[0]/small[0]:.3f}; step ratio {large[1]/max(small[1],1):.3f}")
    assert large[1] <= max(20_000, small[1] * 3)
    assert large[0] < max(0.25, small[0] * 4)


def test_bad_prefix_cannot_starve_ready_units_beyond_the_batch(db):
    seed_core_with_materialization(db)
    db.execute("DELETE FROM sample_coverage")
    db.executemany("INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,0,'{}')",
                   ((i,) for i in range(40)))
    db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,40,1,0,?)", (coverage_json(),))
    db.commit()
    pruner = QuotaPruner(db, [], batch_size=8)
    deleted = []
    for _ in range(8):
        result = pruner.expire("boot-A", 2 * DAY)
        assert len(result.deleted) + len(result.skipped) <= 8
        deleted.extend(u.primary_key for u in result.deleted)
    assert ("boot-A", "gui", 1, 40, 1) in deleted
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone() == (40,)


def test_all_expired_batches_use_oldest_first_and_keep_the_strict_boundary(db):
    seed_core_with_materialization(db)
    db.executemany("INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)",
                   ((i, i, coverage_json()) for i in reversed(range(1, 30))))
    db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,100,1,?,'{}')", (DAY,))
    db.commit()
    pruner = QuotaPruner(db, [], batch_size=7)
    deleted = []
    for _ in range(8):
        result = pruner.expire("boot-A", 2 * DAY)
        assert len(result.deleted) <= 7
        deleted.extend(u.primary_key[3] for u in result.deleted)
        if result.complete:
            break
    assert deleted == list(range(30))
    assert db.execute("SELECT seq,ts_mono_ns FROM sample_coverage").fetchall() == [(100, DAY)]


def test_gap_scan_is_bounded_and_end_time_order_survives_reversed_starts(db):
    seed_core_with_materialization(db)
    text = coverage_json(gap_intervals=1)
    for table in ("app_boot_total", "app_lifetime_total", "rollup_app"):
        db.execute(f"UPDATE {table} SET coverage_json=?", (text,))
    db.executemany("INSERT INTO gap VALUES(?,'boot-A','gui',1,?, ?,?,'collector_gap')",
                   ((i, i, i, 100 - i) for i in range(1, 13)))
    db.commit()
    pruner = QuotaPruner(db, [], batch_size=3)
    deleted = []
    budgets = []
    for _ in range(40):
        steps = 0
        def progress():
            nonlocal steps
            steps += 1
        db.set_progress_handler(progress, 1)
        try:
            result = pruner.expire("boot-A", 2 * DAY)
        finally:
            db.set_progress_handler(None, 0)
        budgets.append(steps)
        assert len(result.deleted) <= 3
        deleted.extend((u.kind, u.primary_key) for u in result.deleted)
        if result.complete:
            break
    assert deleted == [("core", ("boot-A", "gui", 1, 0, 1)),
                       *(("gap", (i,)) for i in reversed(range(1, 13)))]
    assert max(budgets) < 10_000
    assert db.execute("SELECT count(*) FROM gap").fetchone() == (0,)


def test_quota_skips_bad_oldest_unit_and_checkpoints_ready_deletion(db, tmp_path):
    from appio.quota import physical_store_bytes

    seed_core_with_materialization(db)
    for table in ("sample", "sample_dev", "sample_coverage"):
        db.execute(f"UPDATE {table} SET seq=5")
    db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,0,'{}')")
    db.commit()
    main = tmp_path / "appio.sqlite"
    marker = tmp_path / "quota-marker"
    marker.write_bytes(b"x" * 500_000)
    pruner = QuotaPruner(db, [main, marker], quota_bytes=physical_store_bytes(main) + 200_000)
    events = []
    def trace(sql):
        events.append(sql)
        if sql.upper().startswith("DELETE FROM SAMPLE_COVERAGE"):
            marker.unlink(missing_ok=True)
    db.set_trace_callback(trace)
    try:
        result = pruner.shrink_if_needed()
    finally:
        db.set_trace_callback(None)
    assert [u.primary_key for u in result.deleted] == [("boot-A", "gui", 1, 5, 1)]
    assert [u.primary_key for u in result.skipped] == [("boot-A", "gui", 1, 0, 1)]
    assert not result.sampling_paused
    assert result.reason == "missing_materialization"
    assert db.execute("SELECT seq FROM sample_coverage").fetchall() == [(0,)]
    deletion = next(i for i, sql in enumerate(events) if sql.upper().startswith("DELETE FROM SAMPLE_COVERAGE"))
    assert any("COMMIT" in sql.upper() for sql in events[deletion + 1:])
    assert any("WAL_CHECKPOINT" in sql.upper() for sql in events[deletion + 1:])


def test_late_ready_tick_is_retried_after_a_bad_batch_wraps(db):
    seed_core_with_materialization(db)
    db.execute("UPDATE sample_coverage SET coverage_json='{}'")
    db.executemany("INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,0,'{}')",
                   ((i,) for i in range(1, 9)))
    db.commit()
    pruner = QuotaPruner(db, [], batch_size=4)
    assert pruner.expire("boot-A", 2 * DAY).reason == "missing_materialization"
    db.execute("UPDATE sample_coverage SET coverage_json=? WHERE seq=0", (coverage_json(),))
    db.commit()
    deleted = []
    for _ in range(4):
        deleted.extend(pruner.expire("boot-A", 2 * DAY).deleted)
    assert [u.primary_key for u in deleted] == [("boot-A", "gui", 1, 0, 1)]


def test_tied_bad_prefix_uses_keyset_seek_instead_of_rescanning(db):
    seed_core_with_materialization(db)
    db.execute("DELETE FROM sample_coverage")
    db.executemany("INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,0,'{}')",
                   ((i,) for i in range(20_000)))
    db.commit()
    pruner = QuotaPruner(db, [], batch_size=8)
    # Jump well into an equal-time prefix through public passes; a SQL OFFSET
    # or a residual tuple filter burns increasing work even though LIMIT is 8.
    for _ in range(10):
        pruner.expire("boot-A", 2 * DAY)
    steps = 0
    def progress():
        nonlocal steps
        steps += 1
    db.set_progress_handler(progress, 1)
    try:
        result = pruner.expire("boot-A", 2 * DAY)
    finally:
        db.set_progress_handler(None, 0)
    assert [u.primary_key[3] for u in result.skipped] == list(range(80, 88))
    assert steps < 5_000


def test_expiry_uses_each_clock_segment_through_forward_and_backward_steps(db):
    seed_core_with_materialization(db)
    db.execute("INSERT INTO clock_map VALUES('boot-A',20000000000,?)", (1_000_000_000_000 + 2 * DAY,))
    db.execute("INSERT INTO clock_map VALUES('boot-A',40000000000,?)", (1_000_000_000_000 - 2 * DAY,))
    db.executemany("INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)",
                   ((i, mono, coverage_json()) for i, mono in
                    ((1, 10_000_000_000), (2, 20_000_000_000), (3, 40_000_000_000))))
    db.commit()
    result = QuotaPruner(db, [], batch_size=2).expire("boot-A", 2 * DAY + 40_000_000_000)
    assert [u.primary_key for u in result.deleted] == [("boot-A", "gui", 1, 3, 1)]
    assert db.execute("SELECT seq FROM sample_coverage ORDER BY seq").fetchall() == [(0,), (1,), (2,)]


def test_file_wall_collision_progress_uses_complete_primary_key_across_batches(db):
    from appio.contracts import FILE_HOUR_NS
    from appio.file_store import FileHourStore
    from appio.files import HourlyAccumulator
    from test_files import APP, R, ZERO, observation

    for i, hour in enumerate((0, FILE_HOUR_NS)):
        acc = HourlyAccumulator("boot-A", hour)
        acc.add_observation(R, APP, observation(i + 1, last_seen_mono_ns=hour + 1), ZERO)
        FileHourStore(db).finalize_hour(acc.finalize())
    db.execute("DELETE FROM file_overflow_unknown")
    db.execute("INSERT INTO clock_map VALUES('boot-A',?,?)", (FILE_HOUR_NS, 1_000_000_000_000 - FILE_HOUR_NS))
    db.commit()
    pruner = QuotaPruner(db, [], batch_size=1)
    first = pruner.expire("boot-A", 15 * DAY)
    second = pruner.expire("boot-A", 15 * DAY)
    assert [(u.kind, u.primary_key) for u in (*first.deleted, *second.deleted)] == [
        ("file", ("boot-A", 1, 0)), ("file", ("boot-A", 1, FILE_HOUR_NS)),
    ]
    assert db.execute("SELECT count(*) FROM file_coverage").fetchone() == (0,)
