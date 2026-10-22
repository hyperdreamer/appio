"""Deterministic property-style checks for query arithmetic and nullability."""
import random

from appio.contracts import FILE_HOUR_NS, RAW_BUCKET_NS, Window
from appio.migrations import open_database
from appio.query import QueryService
from test_quota import coverage_json


def test_seeded_raw_reconciliation_preserves_signed_component_equations(tmp_path):
    seed = 730291
    rng = random.Random(seed)
    db = open_database(tmp_path / "reconciliation-properties.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,'unit:app-property.service','Property',NULL,1000.0,1000.0,'user')")
    expected_task = [0, 0]
    expected_device = [0, 0]
    for seq in range(64):
        task_read, task_write = rng.randrange(0, 1000), rng.randrange(0, 1000)
        device_read, device_write = rng.randrange(0, 1500), rng.randrange(0, 1500)
        ts = seq + 1
        expected_task[0] += task_read
        expected_task[1] += task_write
        expected_device[0] += device_read
        expected_device[1] += device_write
        db.execute(
            "INSERT INTO sample VALUES('boot-A','gui',1,?,1,0,?,?,NULL,NULL,1000.0,0,0,?,?, 'normal',0,0)",
            (seq, ts, ts, task_read, task_write),
        )
        db.execute(
            "INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)",
            (seq, ts, coverage_json(device_state="available")),
        )
        db.execute(
            "INSERT INTO sample_dev VALUES('boot-A','gui',1,?,1,?,'259:0',?,?)",
            (seq, ts, device_read, device_write),
        )
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.commit()
    try:
        result = QueryService(db).disk_share(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
        message = f"seed={seed}"
        assert result.coverage.complete, message
        assert (result.task_read_bytes, result.task_write_bytes) == tuple(expected_task), message
        assert result.device_read_bytes is not None and result.device_write_bytes is not None, message
        assert (result.device_read_bytes, result.device_write_bytes) == tuple(expected_device), message
        assert result.residual_read == expected_task[0] - expected_device[0], message
        assert result.residual_write == expected_task[1] - expected_device[1], message
        assert result.residual_total == result.residual_read + result.residual_write, message
        assert (result.task_total_bytes == result.task_read_bytes + result.task_write_bytes), message
        assert (result.device_total_bytes == result.device_read_bytes + result.device_write_bytes), message
    finally:
        db.close()


def test_seeded_missing_device_attribution_never_enters_residual_arithmetic(tmp_path):
    seed = 92817
    rng = random.Random(seed)
    db = open_database(tmp_path / "nullable-properties.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,'unit:app-nullable.service','Nullable',NULL,1000.0,1000.0,'user')")
    for seq in range(24):
        ts = seq + 1
        read_bytes, write_bytes = rng.randrange(0, 1_000_000), rng.randrange(0, 1_000_000)
        db.execute(
            "INSERT INTO sample VALUES('boot-A','gui',1,?,1,0,?,?,NULL,NULL,1000.0,0,0,?,?,'normal',0,0)",
            (seq, ts, ts, read_bytes, write_bytes),
        )
        db.execute(
            "INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)",
            (seq, ts, coverage_json(device_state="not_delegated")),
        )
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.commit()
    try:
        result = QueryService(db).disk_share(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
        assert result.task_total_bytes == result.task_read_bytes + result.task_write_bytes, f"seed={seed}"
        assert result.device_read_bytes is None and result.device_write_bytes is None, f"seed={seed}"
        assert result.device_total_bytes is None, f"seed={seed}"
        assert result.residual_read is None and result.residual_write is None, f"seed={seed}"
        assert result.residual_total is None, f"seed={seed}"
        if result.device_total_bytes is not None:
            raise AssertionError(f"seed={seed}: unavailable D was represented as numeric")
    finally:
        db.close()


def test_bucket_partition_uses_only_wholly_contained_intervals(tmp_path):
    # Falsifier: query includes a clipped edge bucket, prorates it, or fills a missing bucket with zero.
    seed = 620719
    rng = random.Random(seed)
    db = open_database(tmp_path / "partition.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,'unit:partition.service','Partition',NULL,1000.0,1000.0,'user')")
    values = [rng.randrange(1, 10000) for _ in range(8)]
    for seq, value in enumerate(values):
        ts = seq * RAW_BUCKET_NS
        db.execute("INSERT INTO sample VALUES('boot-A','gui',1,?,1,0,?,?,NULL,NULL,1000.0,0,0,?,0,'normal',0,0)", (seq, ts, ts, value))
        db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)", (seq, ts, coverage_json(device_state="available")))
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,80000000000)")
    db.commit()
    try:
        for _ in range(32):
            start = rng.randrange(0, 4 * RAW_BUCKET_NS)
            end = start + 2 * RAW_BUCKET_NS + 1
            expected = [(i * RAW_BUCKET_NS, amount) for i, amount in enumerate(values)
                        if start <= i * RAW_BUCKET_NS and (i + 1) * RAW_BUCKET_NS <= end]
            result = QueryService(db).series(1, Window("range", "boot-A", start, end))
            assert [(point.bucket_start_mono_ns, point.read_bytes) for point in result.points] == expected, f"seed={seed} start={start}"
            assert not result.coverage.complete, f"seed={seed}: clipped edge became complete"
    finally:
        db.close()


def test_seeded_file_retained_bytes_and_omissions_equal_hour_inputs(tmp_path):
    # Falsifier: hourly top-N, aliases, search, or unknown-owner context changes the per-app byte equation.
    from appio.contracts import UnknownOwnerInput
    from appio.file_store import FileHourStore
    from appio.files import HourlyAccumulator
    from test_files import APP, R, ZERO, observation
    seed = 891302
    rng = random.Random(seed)
    db = open_database(tmp_path / "file-properties.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Files',NULL,1000.0,1000.0,'user')", (APP,))
    db.execute("INSERT INTO clock_map VALUES('boot-A',0,1000000000000)")
    db.commit()
    expected = [0, 0]
    try:
        for hour in range(3):
            acc = HourlyAccumulator("boot-A", hour * FILE_HOUR_NS)
            for inode in range(1, 111):
                rb, wb = rng.randrange(0, 10000), rng.randrange(0, 10000)
                expected[0] += rb
                expected[1] += wb
                acc.add_observation(R, APP, observation(inode, bytes_r=rb, bytes_w=wb,
                    last_seen_mono_ns=hour * FILE_HOUR_NS + 1), ZERO)
            acc.add_unknown_owner(R, UnknownOwnerInput(1, 5, 7, 0, 0, True))
            FileHourStore(db).finalize_hour(acc.finalize())
        service = QueryService(db)
        window = Window("range", "boot-A", 0, 3 * FILE_HOUR_NS)
        rows = service.file_table(1, window, "bytes_total", "desc", None).rows
        coverage = service.file_coverage(1, window)
        assert (coverage.observed_bytes_r, coverage.observed_bytes_w) == (
            sum(row.bytes_r for row in rows), sum(row.bytes_w for row in rows)), f"seed={seed}"
        assert coverage.observed_bytes_r + coverage.counted_omitted_bytes_r == expected[0], f"seed={seed}"
        assert coverage.observed_bytes_w + coverage.counted_omitted_bytes_w == expected[1], f"seed={seed}"
        assert coverage.unknown_owner_overflow.overflow_files == 3, f"seed={seed}"
        assert not coverage.coverage.complete, f"seed={seed}"
    finally:
        db.close()


def test_seeded_tier_boundaries_and_nullable_peak_reduction(tmp_path):
    # Falsifier: a boundary borrows another tier, None is replaced with zero, or peaks sum across intervals.
    from appio.contracts import MAX_RANGE_NS, WindowUnavailableError
    from appio.query import select_tier
    seed = 571093
    rng = random.Random(seed)
    day = 24 * 3600 * 1_000_000_000
    for edge, before, after in ((day, "raw", "rollup_60s"), (30 * day, "rollup_60s", "rollup_3600s"), (MAX_RANGE_NS, "rollup_3600s", None)):
        for delta in (-1, 0, 1):
            start = rng.randrange(day)
            window = Window("range", "boot-A", start, start + edge + delta)
            if after is None and delta == 1:
                try:
                    select_tier(window, start + edge + delta)
                except WindowUnavailableError as error:
                    assert error.reason == "unsupported_window", f"seed={seed}"
                else:
                    raise AssertionError(f"seed={seed}: overlong range was enabled")
            else:
                assert select_tier(window, None) == (before if delta <= 0 else after), f"seed={seed} edge={edge} delta={delta}"
    db = open_database(tmp_path / "peaks.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,'unit:peaks.service','Peaks',NULL,1000.0,1000.0,'user')")
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,60000000000)")
    expected = []
    for seq in range(6):
        rb, wb = rng.randrange(100), rng.randrange(100)
        duration = None if seq % 3 == 0 else rng.randrange(1_000_000_000, 3_000_000_000)
        ts = seq * RAW_BUCKET_NS
        db.execute("INSERT INTO sample VALUES('boot-A','gui',1,?,1,0,?,?,?, ?,1000.0,0,0,?,?,'normal',0,0)",
                   (seq, ts, ts, None if duration is None else max(0, ts - duration), duration, rb, wb))
        db.execute("INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)", (seq, ts, coverage_json(device_state="available")))
        expected.append(tuple(None if duration is None else amount * 1_000_000_000 // duration for amount in (rb, wb, rb + wb)))
    db.commit()
    try:
        service = QueryService(db)
        window = Window("range", "boot-A", 0, 6 * RAW_BUCKET_NS)
        series = service.series(1, window)
        assert [(point.peak_read_bps, point.peak_write_bps, point.peak_total_bps) for point in series.points] == expected, f"seed={seed}"
        ranking, = service.ranking(window, "total_bytes")
        assert (ranking.peak_read_bps, ranking.peak_write_bps, ranking.peak_total_bps) == tuple(
            max(row[i] for row in expected if row[i] is not None) for i in range(3)), f"seed={seed}"
    finally:
        db.close()


def test_seeded_sampler_resets_reuse_credit_and_dedupe_survive_query_reduction():
    # Falsifier: queries duplicate reap credit, subtract a reset, baseline PID reuse, or count a replay.
    import pytest
    from appio.sampler import StaleFrameError
    from test_store import BOOT, NS, T0, initialized, process, record, tick
    seed = 472193
    rng = random.Random(seed)
    for reset_component in range(4):
        store = initialized()
        parent = [rng.randrange(100, 1000) for _ in range(4)]
        child = [rng.randrange(10, 100) for _ in range(4)]
        own = [rng.randrange(1, 100) for _ in range(4)]
        reused = [rng.randrange(1, 100) for _ in range(4)]
        later = [rng.randrange(1, 100) for _ in range(4)]
        after_reap = [p + c + n for p, c, n in zip(parent, child, own)]
        after_reset = [0 if i == reset_component else value + later[i] for i, value in enumerate(after_reap)]
        observations = (
            tick(0, T0, [process(), process(411, 410, 101)]),
            tick(1, T0 + 10 * NS, [process(counters=tuple(parent)), process(411, 410, 101, tuple(child))]),
            tick(2, T0 + 20 * NS, [process(counters=tuple(after_reap))]),
            tick(3, T0 + 30 * NS, [process(counters=tuple(after_reap)), process(411, 410, 901, tuple(reused))]),
            tick(4, T0 + 40 * NS, [process(counters=tuple(after_reset)), process(411, 410, 901, tuple(reused))]),
        )
        prior = None
        try:
            for observation in observations:
                frame, prior = record(store, prior, observation)
            expected = [p + c + n + r + (0 if i == reset_component else later[i])
                        for i, (p, c, n, r) in enumerate(zip(parent, child, own, reused))]
            service = QueryService(store)
            window = Window("range", BOOT, T0, T0 + 50 * NS)
            total, = service.ranking(window, "total_bytes")
            message = f"seed={seed} reset_component={reset_component}"
            assert (total.rchar, total.wchar, total.read_bytes, total.write_bytes) == tuple(expected), message
            assert total.coverage.reap_corrections >= 1 and total.coverage.resets >= 1, message
            assert total.coverage.reap_unapplied == (0, 0, 0, 0), message
            assert not total.coverage.complete, message
            with pytest.raises(StaleFrameError):
                store.record_tick(frame, prior)
            assert service.ranking(window, "total_bytes") == [total], message
        finally:
            store.connection.close()
