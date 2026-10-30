import json

import pytest

from appio.contracts import (
    FILE_HOUR_NS,
    HISTORICAL_BOOT_PREFIX_MESSAGE,
    MAX_RANGE_NS,
    RAW_BUCKET_NS,
    Window,
    WindowUnavailableError,
)
from appio.file_store import FileHourStore
from appio.files import HourlyAccumulator
from appio.migrations import open_database
from appio.query import QueryService, select_tier
from test_files import APP as FILE_APP, R, ZERO, observation
from test_file_store import db as file_db
from test_quota import coverage_json

APP = "unit:app-demo.service"
HOUR = 3_600_000_000_000
DAY = 86_400_000_000_000


def test_tier_boundaries_are_disjoint_inclusive_and_do_not_switch_to_totals():
    # Falsifier: overlapping duration checks choose different sources at exact boundaries.
    assert select_tier(Window("range", "boot-A", 0, 24 * HOUR), None) == "raw"
    assert select_tier(Window("range", "boot-A", 0, 24 * HOUR + 1), None) == "rollup_60s"
    assert select_tier(Window("range", "boot-A", 0, 30 * DAY), None) == "rollup_60s"
    assert select_tier(Window("range", "boot-A", 0, 30 * DAY + 1), None) == "rollup_3600s"
    assert select_tier(Window("range", "boot-A", 0, MAX_RANGE_NS), None) == "rollup_3600s"
    with pytest.raises(WindowUnavailableError) as error:
        select_tier(Window("range", "boot-A", 0, MAX_RANGE_NS + 1), None)
    assert error.value.reason == "unsupported_window"
    assert str(error.value) == (
        "Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges."
    )
    assert select_tier(
        Window("since_boot", "boot-A", end_mono_ns=MAX_RANGE_NS + 1),
        MAX_RANGE_NS + 1,
    ) == "totals_only"
    with pytest.raises(WindowUnavailableError) as historical:
        select_tier(
            Window("since_boot", "boot-A", end_mono_ns=MAX_RANGE_NS + 1),
            MAX_RANGE_NS + 2,
        )
    assert historical.value.reason == "unsupported_window"
    assert str(historical.value) == HISTORICAL_BOOT_PREFIX_MESSAGE
    assert select_tier(Window("lifetime"), None) == "totals_only"


def test_every_public_query_rejects_launch_before_lookup_with_exact_a22_message(tmp_path):
    # Falsifier: one method performs an ID lookup or picks a replacement tier first.
    db = open_database(tmp_path / "launch.sqlite")
    service = QueryService(db)
    window = Window("launch", "boot-A", launch_id=999)
    queries = (
        lambda: service.ranking(window, "total_bytes"),
        lambda: service.series(999, window),
        lambda: service.disk_share(999, window),
        lambda: service.file_table(999, window, "bytes_total", "desc", None),
        lambda: service.file_coverage(999, window),
        lambda: service.unknown_owner_file_coverage(window),
    )
    try:
        for query in queries:
            with pytest.raises(WindowUnavailableError) as error:
                query()
            assert error.value.reason == "launch_attribution"
            assert str(error.value) == (
                "Launch window disabled: device and file activity are not stored per launch."
            )
    finally:
        db.close()


def test_every_public_query_rejects_overlong_bounded_range_with_exact_a22_message(tmp_path):
    # Falsifier: queries silently widen to full-boot/lifetime totals.
    db = open_database(tmp_path / "long.sqlite")
    service = QueryService(db)
    window = Window("range", "boot-A", 0, MAX_RANGE_NS + 1)
    queries = (
        lambda: service.ranking(window, "total_bytes"),
        lambda: service.series(999, window),
        lambda: service.disk_share(999, window),
        lambda: service.file_table(999, window, "bytes_total", "desc", None),
        lambda: service.file_coverage(999, window),
        lambda: service.unknown_owner_file_coverage(window),
    )
    try:
        for query in queries:
            with pytest.raises(WindowUnavailableError) as error:
                query()
            assert error.value.reason == "unsupported_window"
            assert str(error.value) == (
                "Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges."
            )
    finally:
        db.close()


@pytest.mark.parametrize("missing_receipts", [False, True])
def test_b2_explicit_uncertain_overlap_excludes_both_raw_contributions(file_db, missing_receipts):
    # Falsifier: treating an overlap gap as coverage-only returns 20 rather than 0.
    for source in ("gui", "iod"):
        file_db.execute(
            "INSERT INTO sample VALUES('boot-A',?,1,0,1,0,10000000000,20000000000,0,20000000000,"
            "1010.0,10,0,10,0,'normal',0,0)", (source,),
        )
        file_db.execute("INSERT INTO sample_coverage VALUES('boot-A',?,1,0,1,20000000000,?)",
                        (source, coverage_json()))
        file_db.execute("INSERT INTO collector_interval VALUES(?,'boot-A',1,0,20000000000)", (source,))
    file_db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,0,0,20000000000,'overlap_uncertain')")
    if missing_receipts:
        file_db.execute("DELETE FROM sample_coverage")
    file_db.commit()
    result, = QueryService(file_db).ranking(Window("range", "boot-A", 0, 30_000_000_000), "read_bytes")
    assert result.read_bytes == 0
    assert result.rchar == 0
    assert not result.coverage.complete
    assert result.coverage.overlap_uncertain_intervals == 1


def test_b2_writer_excludes_overlap_from_every_tier_and_preserves_uncertainty(tmp_path):
    # Falsifier: filtering raw queries alone leaves rollups/totals at 20 after pruning.
    from appio.quota import QuotaPruner
    from appio.store import AppIoStore
    from test_store import BOOT, NS, process, record, tick

    store = AppIoStore(tmp_path / "overlap.sqlite")
    store.start_boot(BOOT, 1, 0, 1000.0, "gui")
    for source in ("gui", "iod"):
        store.start_collector(source, BOOT, 0)
    states = {}
    for source in ("gui", "iod"):
        _, states[source] = record(store, None, tick(0, 0, [process()], source=source))
    for source in ("gui", "iod"):
        record(store, states[source], tick(1, 20 * NS, [process(counters=(10, 0, 10, 0))], source=source))
    db = store.connection
    service = QueryService(db)
    windows = (
        Window("range", BOOT, 0, 30 * NS),
        Window("range", BOOT, 0, 2 * DAY),
        Window("range", BOOT, 0, 31 * DAY),
        Window("lifetime"),
    )
    try:
        for window in windows:
            result, = service.ranking(window, "read_bytes")
            assert result.read_bytes == 0
            assert result.rchar == 0
            assert not result.coverage.complete
            assert result.coverage.overlap_uncertain_intervals >= 1
        assert db.execute("SELECT count(*) FROM gap WHERE reason='overlap_uncertain'").fetchone()[0] == 1
        for age in (2 * DAY, 31 * DAY, 366 * DAY):
            pruned = QuotaPruner(db, []).expire(BOOT, age)
            assert pruned.complete, pruned.reason
            result, = service.ranking(Window("lifetime"), "read_bytes")
            assert result.read_bytes == 0
            assert result.coverage.overlap_uncertain_intervals >= 1
            assert not result.coverage.complete
        assert db.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM gap").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM rollup_app").fetchone()[0] == 0
        assert db.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 0
    finally:
        db.close()


def test_b4_totals_only_boot_peaks_are_unavailable_despite_persisted_values(file_db):
    # Falsifier: only lifetime peaks are suppressed, exposing the stored (0, 4, 5).
    from appio.ui.formatting import format_peak

    cutoff = MAX_RANGE_NS + 2
    file_db.execute("INSERT INTO app_boot_total VALUES('boot-A',1,7,8,9,10,0,4,5,?)", (coverage_json(),))
    file_db.execute("INSERT INTO sampler_checkpoint VALUES('gui','boot-A',1,0,?,'{}')", (cutoff,))
    file_db.commit()
    service = QueryService(file_db)
    window = Window("since_boot", "boot-A", end_mono_ns=cutoff)
    result, = service.ranking(window, "total_bytes")
    assert result.coverage.tier == "totals_only"
    assert (result.peak_read_bps, result.peak_write_bps, result.peak_total_bps) == (None, None, None)
    assert format_peak(result.peak_total_bps) == "unavailable"
    assert service.series(1, window).points == ()
    assert file_db.execute("SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM app_boot_total").fetchone() == (0, 4, 5)


def test_raw_query_keeps_task_totals_numeric_when_device_attribution_is_missing(tmp_path):
    # Falsifier: unknown D is converted to zero and yields a fabricated residual.
    db = open_database(tmp_path / "unknown-device.sqlite")
    db.execute(
        "INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')"
    )
    db.execute(
        "INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,)
    )
    coverage = json.loads(coverage_json(device_state="not_delegated"))
    db.execute(
        "INSERT INTO sample VALUES('boot-A','gui',1,0,1,0,10,10,NULL,NULL,1000.0,"
        "5,7,11,13,'normal',0,0)"
    )
    db.execute(
        "INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,1,?)",
        (coverage_json(device_state="not_delegated"),),
    )
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10)")
    db.commit()

    try:
        service = QueryService(db)
        window = Window("range", "boot-A", 0, RAW_BUCKET_NS)
        result, = service.ranking(window, "total_bytes")
        assert (result.read_bytes, result.write_bytes, result.total_bytes) == (11, 13, 24)
        assert result.coverage.tier == "raw"
        assert result.coverage.device_state == "not_delegated"
        detail = service.disk_share(1, window)
        assert (detail.task_read_bytes, detail.task_write_bytes, detail.task_total_bytes) == (11, 13, 24)
        assert detail.device_read_bytes is None
        assert detail.device_write_bytes is None
        assert detail.device_total_bytes is None
        assert detail.residual_read is None
        assert detail.residual_write is None
        assert detail.residual_total is None
        assert detail.coverage.tier == "raw"
    finally:
        db.close()


def test_short_old_window_does_not_substitute_a_wider_tier(tmp_path):
    # Falsifier: raw retention loss causes rollup reads for a raw-duration request.
    db = open_database(tmp_path / "no-substitution.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    db.execute(
        "INSERT INTO rollup_app VALUES('boot-A',1,0,60,999,0,999,0,NULL,NULL,NULL,'{}')"
    )
    db.commit()
    try:
        result = QueryService(db).series(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
        assert result.tier == "raw"
        assert result.points == ()
        assert result.coverage.tier == "raw"
        assert result.coverage.complete is False
        assert result.coverage.unavailable_reason == "not_retained"
    finally:
        db.close()


def test_rollup_tiers_are_selected_without_blending_raw_or_other_rollups(tmp_path):
    # Falsifier: aggregation adds values from a non-selected tier or falls back when clipped.
    db = open_database(tmp_path / "tier-isolation.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    db.execute(
        "INSERT INTO sample VALUES('boot-A','gui',1,0,1,0,0,1,NULL,NULL,1000.0,0,0,1,0,'normal',0,0)"
    )
    db.execute(
        "INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,1,?)",
        (coverage_json(device_state="not_delegated"),),
    )
    db.execute("INSERT INTO rollup_app VALUES('boot-A',1,0,60,100,0,0,0,NULL,NULL,NULL,?)", (coverage_json(),))
    db.execute("INSERT INTO rollup_app VALUES('boot-A',1,0,3600,1000,0,0,0,NULL,NULL,NULL,?)", (coverage_json(),))
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,9223372036854775807)")
    db.commit()
    try:
        service = QueryService(db)
        short = service.ranking(Window("range", "boot-A", 0, RAW_BUCKET_NS), "read_bytes")
        medium = service.ranking(Window("range", "boot-A", 0, 24 * HOUR + 1), "read_bytes")
        long = service.ranking(Window("range", "boot-A", 0, 30 * DAY + 1), "read_bytes")
        assert short[0].read_bytes == 1 and short[0].coverage.tier == "raw"
        assert medium[0].read_bytes == 100 and medium[0].coverage.tier == "rollup_60s"
        assert long[0].read_bytes == 1000 and long[0].coverage.tier == "rollup_3600s"
    finally:
        db.close()


def test_file_resolution_constant_is_one_hour_and_raw_resolution_is_ten_seconds():
    # Falsifier: file queries accidentally inherit process-rollup resolution.
    assert FILE_HOUR_NS == HOUR
    assert RAW_BUCKET_NS == 10_000_000_000


def test_exact_full_boot_cutoff_uses_totals_but_historical_prefix_does_not(tmp_path):
    # Falsifier: a same-length prefix is answered with full-boot totals.
    db = open_database(tmp_path / "cutoff.sqlite")
    cutoff = MAX_RANGE_NS + 2
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    db.execute(
        "INSERT INTO app_boot_total VALUES('boot-A',1,7,8,9,10,0,4,5,?)",
        (coverage_json(device_state="available"),),
    )
    db.execute(
        "INSERT INTO sampler_checkpoint VALUES('gui','boot-A',1,0,?,'{}')", (cutoff,)
    )
    db.commit()
    try:
        service = QueryService(db)
        exact = service.ranking(Window("since_boot", "boot-A", end_mono_ns=cutoff), "total_bytes")
        assert [(item.read_bytes, item.write_bytes) for item in exact] == [(7, 8)]
        assert exact[0].coverage.tier == "totals_only"
        assert (exact[0].peak_read_bps, exact[0].peak_write_bps, exact[0].peak_total_bps) == (None, None, None)
        with pytest.raises(WindowUnavailableError) as error:
            service.ranking(Window("since_boot", "boot-A", end_mono_ns=cutoff - 1), "total_bytes")
        assert error.value.reason == "unsupported_window"
        assert str(error.value) == HISTORICAL_BOOT_PREFIX_MESSAGE
    finally:
        db.close()


def test_raw_coverage_reduces_keyed_reap_vectors_and_overflow_flags(tmp_path):
    # Falsifier: coverage vectors are treated as positional lists or flags are not OR-reduced.
    db = open_database(tmp_path / "coverage.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    first = json.loads(coverage_json())
    first["reap_unapplied"] = {"rchar": 1, "wchar": 2, "read_bytes": 3, "write_bytes": 4}
    first["reap_unobserved"] = {"rchar": 5, "wchar": 6, "read_bytes": 7, "write_bytes": 8}
    second = json.loads(coverage_json())
    second["reap_unapplied"] = {"rchar": 9, "wchar": 8, "read_bytes": 7, "write_bytes": 6}
    second["counter_overflow"] = 1
    second["device_state"] = "available"
    for seq, coverage in enumerate((first, second)):
        db.execute(
            "INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)",
            (seq, seq + 1, json.dumps(coverage, separators=(",", ":"))),
        )
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.commit()
    try:
        result = QueryService(db).series(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
        coverage = result.coverage
        assert coverage.reap_unapplied == (10, 10, 10, 10)
        assert coverage.reap_unobserved == (5, 6, 7, 8)
        assert coverage.counter_overflow is True
        assert coverage.complete is False
        assert coverage.device_state == "incomplete"
    finally:
        db.close()


def test_peaks_distinguish_undefined_from_defined_zero(tmp_path):
    # Falsifier: baseline-only samples become zero peaks or valid sub-byte rates become None.
    db = open_database(tmp_path / "peaks.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.executemany(
        "INSERT INTO app VALUES(?,?,?,NULL,1000.0,1000.0,'user')",
        ((1, APP, "Measured"), (2, "unit:baseline.service", "Baseline")),
    )
    for app_id, amount, interval_start, observed_ns, kind in (
        (1, 1, 0, RAW_BUCKET_NS, "normal"),
        (2, 4, None, None, "baseline"),
    ):
        db.execute(
            "INSERT INTO sample VALUES('boot-A','gui',1,0,?,0,0,?,?,?,?,0,0,?,0,?,0,0)",
            (app_id, RAW_BUCKET_NS, interval_start, observed_ns, 1000.0, amount, kind),
        )
        db.execute(
            "INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,?,?,?)",
            (app_id, 1, coverage_json(device_state="not_delegated")),
        )
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.execute("INSERT INTO clock_map VALUES('boot-A',0,1000000000000)")
    db.commit()
    try:
        service = QueryService(db)
        window = Window("range", "boot-A", 0, RAW_BUCKET_NS)
        measured = service.series(1, window).points[0]
        baseline = service.series(2, window).points[0]
        assert measured.peak_read_bps == 0
        assert measured.peak_total_bps == 0
        assert baseline.peak_read_bps is None
        assert baseline.peak_total_bps is None
    finally:
        db.close()


def test_disk_share_keeps_signed_residual_and_guards_unavailable_arithmetic(tmp_path):
    # Falsifier: negative residuals are clamped or None is added as if it were zero.
    db = open_database(tmp_path / "residual.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    db.execute(
        "INSERT INTO sample VALUES('boot-A','gui',1,0,1,0,1,1,NULL,NULL,1000.0,0,0,4,3,'normal',0,0)"
    )
    db.execute(
        "INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,1,?)",
        (coverage_json(device_state="available"),),
    )
    db.execute("INSERT INTO sample_dev VALUES('boot-A','gui',1,0,1,1,'259:0',9,8)")
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.commit()
    try:
        service = QueryService(db)
        window = Window("range", "boot-A", 0, RAW_BUCKET_NS)
        complete = service.disk_share(1, window)
        assert complete.coverage.complete
        assert (complete.residual_read, complete.residual_write, complete.residual_total) == (-5, -5, -10)
        db.execute("UPDATE sample_coverage SET coverage_json=?", (coverage_json(device_state="incomplete"),))
        db.commit()
        unavailable = service.disk_share(1, window)
        assert (unavailable.task_read_bytes, unavailable.task_write_bytes) == (4, 3)
        assert unavailable.device_total_bytes is None
        assert unavailable.residual_total is None
        if unavailable.residual_total is not None:
            pytest.fail("unavailable residual must not enter numeric reconciliation")
        db.execute("UPDATE collector_interval SET to_mono_ns=5000000000 WHERE boot_id='boot-A'")
        db.commit()
        interval_gap = service.disk_share(1, window)
        assert interval_gap.coverage.device_state == "incomplete"
        assert interval_gap.device_total_bytes is None
        assert interval_gap.residual_total is None
    finally:
        db.close()


def test_a21_diagnostic_marks_window_incomplete_without_a_surviving_raw_row(tmp_path):
    # Falsifier: diagnostics disappear from coverage when the rejected observation rolled back.
    db = open_database(tmp_path / "diagnostic.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.execute(
        "INSERT INTO store_diagnostic VALUES(1,'boot-A','gui',1,0,1,5,'counter_overflow','read_bytes')"
    )
    db.commit()
    try:
        result = QueryService(db).series(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
        assert result.points == ()
        assert result.coverage.counter_overflow is True
        assert result.coverage.complete is False
    finally:
        db.close()


def test_invalid_persisted_coverage_integers_do_not_coerce_booleans(tmp_path):
    # Falsifier: JSON booleans are accepted as integer coverage counters.
    db = open_database(tmp_path / "invalid-coverage.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    coverage = json.loads(coverage_json())
    coverage["inaccessible_pids"] = True
    db.execute(
        "INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,0,?)",
        (json.dumps(coverage, separators=(",", ":")),),
    )
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.commit()
    try:
        result = QueryService(db).series(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
        assert result.coverage.inaccessible_pids == 0
        assert result.coverage.complete is False
    finally:
        db.close()


def test_unknown_app_is_key_error_after_supported_window_validation(tmp_path):
    # Falsifier: unsupported windows are masked by an app lookup, or valid IDs invent rows.
    db = open_database(tmp_path / "unknown-app.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.commit()
    try:
        with pytest.raises(KeyError):
            QueryService(db).series(99, Window("range", "boot-A", 0, RAW_BUCKET_NS))
    finally:
        db.close()


def test_hourly_file_queries_search_sort_and_unknown_owner_are_independent_of_process_rows(file_db):
    # Falsifier: file queries read process tiers, normalize returned spelling, or assign global overflow to the app.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(1, basename="Cafe\u0301.txt", dev="259:0", bytes_r=30, bytes_w=10, last_seen_mono_ns=100_000_000_000), ZERO)
    acc.add_observation(R, FILE_APP, observation(2, basename="zeta.txt", dev="259:1", bytes_r=20, bytes_w=20, last_seen_mono_ns=200_000_000_000), ZERO)
    from appio.contracts import UnknownOwnerInput
    acc.add_unknown_owner(R, UnknownOwnerInput(2, 11, 13, 0, 0, True))
    FileHourStore(file_db).finalize_hour(acc.finalize())
    service = QueryService(file_db)
    window = Window("range", "boot-A", 0, FILE_HOUR_NS)
    result = service.file_table(1, window, "bytes_total", "desc", "Caf\u00e9")
    assert len(result.rows) == 1
    assert result.rows[0].basename == "Cafe\u0301.txt"
    assert result.coverage.tier == "raw"
    assert result.unknown_owner_overflow.overflow_files == 2
    assert (result.unknown_owner_overflow.overflow_bytes_r, result.unknown_owner_overflow.overflow_bytes_w) == (11, 13)
    assert result.coverage.complete is False
    file_coverage = service.file_coverage(1, window)
    assert (file_coverage.observed_bytes_r, file_coverage.observed_bytes_w) == (50, 30)
    assert file_coverage.uncounted_overflow_files == 0
    unknown = service.unknown_owner_file_coverage(window)
    assert (unknown.overflow_files, unknown.overflow_bytes_r, unknown.overflow_bytes_w) == (2, 11, 13)
    assert unknown.coverage.complete
    assert file_db.execute("SELECT count(*) FROM sample").fetchone()[0] == 0


def test_file_overflow_diagnostic_is_an_overlay_but_app_diagnostics_do_not_pollute_unknown_owner(file_db):
    # Falsifier: A21 diagnostics are omitted from F or app-specific diagnostics contaminate global unknown-owner coverage.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(1, bytes_r=7), ZERO)
    FileHourStore(file_db).finalize_hour(acc.finalize())
    file_db.execute(
        "INSERT INTO store_diagnostic VALUES(1,'boot-A','gui',1,1,1,1,'counter_overflow','read_bytes')"
    )
    file_db.commit()
    service = QueryService(file_db)
    window = Window("range", "boot-A", 0, FILE_HOUR_NS)
    app_coverage = service.file_coverage(1, window).coverage
    unknown_coverage = service.unknown_owner_file_coverage(window).coverage
    assert app_coverage.counter_overflow is True
    assert app_coverage.complete is False
    assert unknown_coverage.counter_overflow is False
    assert unknown_coverage.complete


def test_file_table_direction_reverses_only_primary_sort_and_empty_search_matches_all(file_db):
    # Falsifier: descending order reverses stable identity ties or an empty query filters names.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(2, basename="same", dev="259:1", bytes_r=20, last_seen_mono_ns=200_000_000_000), ZERO)
    acc.add_observation(R, FILE_APP, observation(1, basename="same", dev="259:0", bytes_r=20, last_seen_mono_ns=100_000_000_000), ZERO)
    FileHourStore(file_db).finalize_hour(acc.finalize())
    result = QueryService(file_db).file_table(1, Window("range", "boot-A", 0, FILE_HOUR_NS), "bytes_r", "desc", "")
    assert [(row.dev, row.inode) for row in result.rows] == [("259:0", 1), ("259:1", 2)]
    assert [row.basename for row in result.rows] == ["same", "same"]


def test_pending_file_hour_is_incomplete_but_keeps_finalized_hour_observations(file_db):
    # Falsifier: include pending rows as finalized data or discard prior finalized evidence.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(1, basename="done", bytes_r=7), ZERO)
    FileHourStore(file_db).finalize_hour(acc.finalize())
    file_db.execute("UPDATE boot SET file_finalized_through_mono_ns=? WHERE boot_id='boot-A'", (FILE_HOUR_NS,))
    file_db.commit()
    result = QueryService(file_db).file_table(
        1, Window("range", "boot-A", 0, 2 * FILE_HOUR_NS + 1), "bytes_total", "desc", None
    )
    assert [row.basename for row in result.rows] == ["done"]
    assert result.coverage.complete is False


def test_missing_finalized_file_hour_returns_empty_unavailable_window(file_db):
    # Falsifier: partial finalized rows are returned as though they reconstruct the full requested window.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(1, basename="kept", bytes_r=7), ZERO)
    FileHourStore(file_db).finalize_hour(acc.finalize())
    file_db.execute("UPDATE boot SET file_finalized_through_mono_ns=? WHERE boot_id='boot-A'", (2 * FILE_HOUR_NS,))
    file_db.commit()
    result = QueryService(file_db).file_table(
        1, Window("range", "boot-A", 0, 2 * FILE_HOUR_NS), "bytes_total", "desc", None
    )
    assert result.rows == ()
    assert result.coverage.complete is False
    assert result.coverage.unavailable_reason == "not_retained"
    values = QueryService(file_db).file_coverage(1, Window("range", "boot-A", 0, 2 * FILE_HOUR_NS))
    assert values.coverage.complete is False
    assert values.coverage.unavailable_reason == "not_retained"
    assert (values.observed_bytes_r, values.observed_bytes_w) == (0, 0)
    unknown = QueryService(file_db).unknown_owner_file_coverage(Window("range", "boot-A", 0, 2 * FILE_HOUR_NS))
    assert unknown.coverage.complete is False
    assert unknown.coverage.unavailable_reason == "not_retained"


def test_since_boot_file_window_serves_covered_suffix_and_names_its_boundary(file_db, monkeypatch):
    # Falsifier: return a false complete boot total or discard all rows because
    # file collection began after boot.
    service = QueryService(file_db)
    start = 2 * FILE_HOUR_NS
    end = 4 * FILE_HOUR_NS
    for hour in (start, start + FILE_HOUR_NS):
        accumulator = HourlyAccumulator("boot-A", hour)
        accumulator.add_observation(
            R, FILE_APP,
            observation(1, bytes_r=17, last_seen_mono_ns=hour + 100_000_000_000),
            ZERO,
        )
        FileHourStore(file_db).finalize_hour(accumulator.finalize())
    file_db.commit()
    monkeypatch.setattr(service, "_available_cutoff", lambda _boot_id: end)

    requested = Window("since_boot", "boot-A", end_mono_ns=end)
    table = service.file_table(1, requested, "bytes_total", "desc", None)

    assert sum(row.bytes_r for row in table.rows) == 34
    assert len(table.rows) == 1
    assert table.coverage.complete is False
    assert table.coverage.unavailable_reason is None
    assert table.unknown_owner_overflow.window == Window(
        "range", "boot-A", start_mono_ns=start, end_mono_ns=end
    )
    observed = service.file_coverage(1, requested)
    assert (observed.observed_bytes_r, observed.observed_bytes_w) == (34, 0)
    assert observed.coverage.complete is False
    assert observed.window == requested

    last_24h = Window("range", "boot-A", start_mono_ns=FILE_HOUR_NS, end_mono_ns=end)
    range_table = service.file_table(1, last_24h, "bytes_total", "desc", None)
    assert sum(row.bytes_r for row in range_table.rows) == 34
    assert range_table.coverage.complete is False
    assert range_table.unknown_owner_overflow.window == Window(
        "range", "boot-A", start_mono_ns=start, end_mono_ns=end
    )


def test_complete_since_boot_file_window_keeps_its_requested_scope(file_db, monkeypatch):
    service = QueryService(file_db)
    end = 2 * FILE_HOUR_NS
    for hour in (0, FILE_HOUR_NS):
        accumulator = HourlyAccumulator("boot-A", hour)
        accumulator.add_observation(
            R, FILE_APP,
            observation(1, bytes_r=17, last_seen_mono_ns=hour + 100_000_000_000),
            ZERO,
        )
        FileHourStore(file_db).finalize_hour(accumulator.finalize())
    file_db.commit()
    monkeypatch.setattr(service, "_available_cutoff", lambda _boot_id: end)

    requested = Window("since_boot", "boot-A", end_mono_ns=end)
    table = service.file_table(1, requested, "bytes_total", "desc", None)

    assert sum(row.bytes_r for row in table.rows) == 34
    assert table.coverage.complete is True
    assert table.unknown_owner_overflow.window == requested


def test_no_coverage_since_boot_file_window_remains_unavailable(file_db, monkeypatch):
    service = QueryService(file_db)
    end = 2 * FILE_HOUR_NS
    monkeypatch.setattr(service, "_available_cutoff", lambda _boot_id: end)
    requested = Window("since_boot", "boot-A", end_mono_ns=end)

    result = service.file_table(1, requested, "bytes_total", "desc", None)

    assert result.rows == ()
    assert result.coverage.unavailable_reason in {
        "file_source_unavailable",
        "not_retained",
    }


def test_missing_interior_file_hour_is_not_reported_as_collection_start(file_db):
    for hour in (0, 2 * FILE_HOUR_NS):
        accumulator = HourlyAccumulator("boot-A", hour)
        accumulator.add_observation(
            R, FILE_APP,
            observation(1, bytes_r=17, last_seen_mono_ns=hour + 100_000_000_000),
            ZERO,
        )
        FileHourStore(file_db).finalize_hour(accumulator.finalize())
    file_db.commit()
    window = Window("range", "boot-A", 0, 3 * FILE_HOUR_NS)

    result = QueryService(file_db).file_table(1, window, "bytes_total", "desc", None)

    assert result.rows == ()
    assert result.coverage.unavailable_reason == "not_retained"


def test_full_boot_file_totals_survive_hourly_row_pruning(file_db):
    # Falsifier: a full-boot request loses exact file totals after hourly retention pruning.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(1, bytes_r=17, bytes_w=9, reads=3), ZERO)
    FileHourStore(file_db).finalize_hour(acc.finalize())
    file_db.execute("DELETE FROM file_sample")
    file_db.execute("DELETE FROM file_coverage")
    file_db.execute("DELETE FROM file_overflow_unknown")
    file_db.commit()
    # The boot's exact process collection cutoff is the durable sampler checkpoint.
    file_db.execute("INSERT INTO sampler_checkpoint VALUES('gui','boot-A',1,0,?, '{}')", (FILE_HOUR_NS,))
    file_db.commit()
    result = QueryService(file_db).file_coverage(1, Window("since_boot", "boot-A", end_mono_ns=FILE_HOUR_NS))
    assert (result.observed_bytes_r, result.observed_bytes_w) == (17, 9)
    assert result.coverage.tier == "totals_only"
    assert result.coverage.complete


def test_exact_full_boot_file_totals_mark_an_unfinalized_tail_incomplete(file_db):
    # Falsifier: exact boot totals conceal file bytes still pending beyond the finalization watermark.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(1, bytes_r=17, bytes_w=9, reads=3), ZERO)
    FileHourStore(file_db).finalize_hour(acc.finalize())
    file_db.execute("DELETE FROM file_sample")
    file_db.execute("DELETE FROM file_coverage")
    file_db.execute("DELETE FROM file_overflow_unknown")
    file_db.execute("INSERT INTO sampler_checkpoint VALUES('gui','boot-A',1,0,?, '{}')", (FILE_HOUR_NS + 1,))
    file_db.commit()
    result = QueryService(file_db).file_coverage(
        1, Window("since_boot", "boot-A", end_mono_ns=FILE_HOUR_NS + 1)
    )
    assert (result.observed_bytes_r, result.observed_bytes_w) == (17, 9)
    assert result.coverage.complete is False


def test_unrepresented_file_window_returns_empty_and_zero_observed_bytes(file_db):
    # Falsifier: missing hourly history is presented as measured zero F.
    file_db.execute("UPDATE boot SET file_finalized_through_mono_ns=? WHERE boot_id='boot-A'", (FILE_HOUR_NS,))
    file_db.commit()
    window = Window("range", "boot-A", 0, FILE_HOUR_NS)
    result = QueryService(file_db).file_table(1, window, "bytes_total", "desc", None)
    assert result.rows == ()
    assert result.coverage.complete is False
    assert result.coverage.unavailable_reason == "not_retained"
    values = QueryService(file_db).file_coverage(1, window)
    assert (values.observed_bytes_r, values.observed_bytes_w) == (0, 0)
    assert values.coverage.complete is False


@pytest.fixture
def query_db(tmp_path):
    db = open_database(tmp_path / "query-evidence.sqlite")
    db.execute("INSERT INTO boot VALUES('boot-A',1,0,0,1000.0,'gui')")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    db.execute("INSERT INTO collector_interval VALUES('gui','boot-A',1,0,10000000000)")
    db.commit()
    try:
        yield db
    finally:
        db.close()


def seed_raw(db, seq, timestamp, *, read=7, write=3, observed=None, coverage=True, launch=0, spans=False):
    observed = timestamp if observed is None else observed
    db.execute(
        "INSERT INTO sample VALUES('boot-A','gui',1,?,1,?,?,?,NULL,NULL,1000.0,0,0,?,?,'normal',0,?)",
        (seq, launch, timestamp, observed, read, write, int(spans)),
    )
    if coverage:
        db.execute(
            "INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)",
            (seq, observed, coverage_json(device_state="available")),
        )


def test_raw_coverage_joins_suspend_attribution_by_receipt_and_deduplicates_launches(query_db):
    # Falsifier: joining by timestamp loses/duplicates the post-suspend receipt for pre-suspend bytes.
    seed_raw(query_db, 0, 1, observed=RAW_BUCKET_NS + 1, spans=True)
    seed_raw(query_db, 0, 2, observed=RAW_BUCKET_NS + 1, coverage=False, launch=1)
    cov = json.loads(coverage_json(device_state="available", inaccessible_pids=2, spans_suspend_intervals=1))
    query_db.execute("UPDATE sample_coverage SET coverage_json=?", (json.dumps(cov),))
    query_db.commit()
    result, = QueryService(query_db).ranking(Window("range", "boot-A", 0, RAW_BUCKET_NS), "read_bytes")
    assert (result.read_bytes, result.write_bytes) == (14, 6)
    assert result.coverage.inaccessible_pids == 2
    assert result.coverage.spans_suspend
    assert result.coverage.unavailable_reason is None


def test_missing_matching_raw_receipt_keeps_bytes_and_marks_device_unavailable(query_db):
    # Falsifier: an unrelated receipt at the same timestamp proves a missing observation's coverage.
    seed_raw(query_db, 1, 1, coverage=False)
    query_db.execute(
        "INSERT INTO sample_coverage VALUES('boot-A','gui',1,0,1,1,?)",
        (coverage_json(device_state="available"),),
    )
    query_db.commit()
    result = QueryService(query_db).disk_share(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
    assert (result.task_read_bytes, result.task_write_bytes) == (7, 3)
    assert not result.coverage.complete
    assert result.coverage.unavailable_reason == "not_retained"
    assert result.device_total_bytes is None and result.residual_total is None


@pytest.mark.parametrize("offset,affects", [(-1, False), (0, True), (RAW_BUCKET_NS - 1, True), (RAW_BUCKET_NS, False)])
def test_diagnostic_overlay_obeys_exact_half_open_window(query_db, offset, affects):
    # Falsifier: widening diagnostic lookup to adjacent buckets changes an unaffected window.
    start = RAW_BUCKET_NS
    query_db.execute("UPDATE collector_interval SET to_mono_ns=?", (3 * RAW_BUCKET_NS,))
    seed_raw(query_db, 0, start)
    query_db.execute(
        "INSERT INTO store_diagnostic VALUES(1,'boot-A','gui',1,1,1,?,'counter_overflow','read_bytes')",
        (start + offset,),
    )
    query_db.commit()
    result, = QueryService(query_db).ranking(Window("range", "boot-A", start, start + RAW_BUCKET_NS), "read_bytes")
    assert result.coverage.counter_overflow is affects
    assert result.coverage.complete is (not affects)


@pytest.mark.parametrize("defect", ["missing_file", "missing_hook", "extra_key", "flag_two", "float", "list_vector", "duplicate_key"])
def test_malformed_persisted_coverage_fails_closed(query_db, defect):
    # Falsifier: malformed evidence silently defaults to measured zero/complete coverage.
    seed_raw(query_db, 0, 1)
    value = json.loads(coverage_json(device_state="available"))
    if defect == "missing_file":
        value.pop("file")
    elif defect == "missing_hook":
        value["file"]["hook_hits"].pop("vfs_read")
    elif defect == "extra_key":
        value["extra"] = 0
    elif defect == "flag_two":
        value["reap_incomplete"] = 2
    elif defect == "float":
        value["resets"] = 0.0
    elif defect == "list_vector":
        value["reap_unapplied"] = [0, 0, 0, 0]
    text = json.dumps(value)
    if defect == "duplicate_key":
        text = '{"resets":0,' + text[1:]
    query_db.execute("UPDATE sample_coverage SET coverage_json=?", (text,))
    query_db.commit()
    result = QueryService(query_db).disk_share(1, Window("range", "boot-A", 0, RAW_BUCKET_NS))
    assert not result.coverage.complete, defect
    assert result.coverage.device_state == "incomplete", defect
    assert result.device_total_bytes is None and result.residual_total is None, defect


def test_partial_raw_pruning_preserves_points_but_cannot_claim_complete(query_db):
    # Falsifier: one surviving bucket masks missing historical buckets under a continuous collector interval.
    query_db.execute("UPDATE collector_interval SET to_mono_ns=?", (3 * RAW_BUCKET_NS,))
    for seq in range(3):
        seed_raw(query_db, seq, seq * RAW_BUCKET_NS)
    query_db.commit()
    service = QueryService(query_db)
    window = Window("range", "boot-A", 0, 3 * RAW_BUCKET_NS)
    assert service.series(1, window).coverage.complete
    query_db.execute("DELETE FROM sample WHERE seq=1")
    query_db.execute("DELETE FROM sample_coverage WHERE seq=1")
    query_db.commit()
    result = service.series(1, window)
    assert [row.bucket_start_mono_ns for row in result.points] == [0, 2 * RAW_BUCKET_NS]
    assert not result.coverage.complete
    assert result.coverage.unavailable_reason == "not_retained"


def test_collector_interval_union_and_intersecting_gap_are_independent_overlays(query_db):
    # Falsifier: adjacent collector intervals are rejected or one interval is mistaken for full coverage.
    seed_raw(query_db, 0, 1)
    query_db.execute("UPDATE collector_interval SET to_mono_ns=5000000000")
    query_db.execute("INSERT INTO collector_interval VALUES('iod','boot-A',1,5000000000,10000000000)")
    query_db.commit()
    service = QueryService(query_db)
    window = Window("range", "boot-A", 0, RAW_BUCKET_NS)
    assert service.ranking(window, "read_bytes")[0].coverage.complete
    query_db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,5,6,'overlap_uncertain')")
    query_db.commit()
    result = service.ranking(window, "read_bytes")[0].coverage
    assert not result.complete
    assert (result.gap_intervals, result.overlap_uncertain_intervals) == (1, 1)


def test_ranking_orders_tied_metrics_by_app_key_and_empty_stays_empty(query_db):
    # Falsifier: insertion/app-ID order resolves tied metrics, or an empty window invents a row.
    seed_raw(query_db, 0, 1)
    query_db.execute("INSERT INTO app VALUES(2,'unit:a.service','A',NULL,1000.0,1000.0,'user')")
    query_db.execute(
        "INSERT INTO sample SELECT boot_id,source,epoch,seq,2,launch_id,ts_mono_ns,observed_mono_ns,"
        "interval_start_mono_ns,observed_ns,ts_wall,rchar,wchar,read_bytes,write_bytes,delta_kind,first_seen_partial,spans_suspend FROM sample"
    )
    query_db.execute("INSERT INTO sample_coverage SELECT boot_id,source,epoch,seq,2,ts_mono_ns,coverage_json FROM sample_coverage")
    query_db.commit()
    service = QueryService(query_db)
    result = service.ranking(Window("range", "boot-A", 0, RAW_BUCKET_NS), "total_bytes")
    assert [row.app_id for row in result] == [2, 1]
    assert all(row.total_bytes == row.read_bytes + row.write_bytes for row in result)
    assert service.ranking(Window("range", "boot-A", RAW_BUCKET_NS, 2 * RAW_BUCKET_NS), "total_bytes") == []


def test_totals_only_preserves_boot_device_identity_and_has_no_series_points(query_db):
    # Falsifier: lifetime D merges equal dev tokens across boots or series borrows raw points.
    cov = coverage_json(device_state="available")
    query_db.execute("INSERT INTO boot VALUES('boot-B',2,0,0,2000.0,'gui')")
    query_db.execute("INSERT INTO app_lifetime_total VALUES(1,10,20,30,40,?,'boot-B',10)", (cov,))
    query_db.executemany(
        "INSERT INTO app_boot_dev_total VALUES(?,1,'259:0',?,?)", [("boot-A", 7, 8), ("boot-B", 9, 10)]
    )
    seed_raw(query_db, 0, 1, read=999)
    query_db.commit()
    service = QueryService(query_db)
    result = service.disk_share(1, Window("lifetime"))
    assert [(row.boot_id, row.dev, row.rbytes) for row in result.devices] == [("boot-A", "259:0", 7), ("boot-B", "259:0", 9)]
    assert (result.task_read_bytes, result.device_read_bytes, result.residual_read) == (10, 16, -6)
    series = service.series(1, Window("lifetime"))
    assert series.tier == "totals_only" and series.coverage.tier == "totals_only"
    assert series.points == series.device_points == series.residual_points == ()
    total, = service.ranking(Window("lifetime"), "total_bytes")
    assert (total.peak_read_bps, total.peak_write_bps, total.peak_total_bps) == (None, None, None)


def test_persisted_coverage_fields_reduce_by_sum_component_sum_and_or(query_db):
    # Falsifier: any persisted count/vector/flag is dropped, borrowed, or duplicated per launch.
    counts = ("inaccessible_pids", "resets", "reap_corrections", "partial_intervals",
              "gap_intervals", "overlap_uncertain_intervals", "spans_suspend_intervals")
    expected = dict.fromkeys(counts, 0)
    vectors = ("reap_unapplied", "reap_unobserved")
    vector_names = ("rchar", "wchar", "read_bytes", "write_bytes")
    expected_vectors = {key: [0, 0, 0, 0] for key in vectors}
    for seq in range(2):
        seed_raw(query_db, seq, seq, spans=bool(seq))
        values = json.loads(coverage_json(device_state="available" if seq else "not_delegated"))
        for index, name in enumerate(counts):
            values[name] = (seq + 1) * (index + 1)
            expected[name] += values[name]
        for offset, key in enumerate(vectors):
            for index, name in enumerate(vector_names):
                value = (seq + 1) * (index + 1 + offset)
                values[key][name] = value
                expected_vectors[key][index] += value
        values["reap_incomplete"] = seq
        values["counter_overflow"] = seq
        query_db.execute("UPDATE sample_coverage SET coverage_json=? WHERE seq=?", (json.dumps(values), seq))
    query_db.commit()
    result, = QueryService(query_db).ranking(Window("range", "boot-A", 0, RAW_BUCKET_NS), "total_bytes")
    for name, expected_count in expected.items():
        assert getattr(result.coverage, name) == expected_count, name
    for key, vector in expected_vectors.items():
        assert getattr(result.coverage, key) == tuple(vector), key
    assert result.coverage.reap_incomplete and result.coverage.counter_overflow
    assert result.coverage.spans_suspend and result.coverage.device_state == "incomplete"
    assert not result.coverage.complete


@pytest.mark.parametrize("timestamp,affects", [(RAW_BUCKET_NS - 1, False), (RAW_BUCKET_NS, True), (RAW_BUCKET_NS + 1, True), (3 * RAW_BUCKET_NS - 1, True), (3 * RAW_BUCKET_NS, False)])
def test_a21_diagnostics_intersect_observation_buckets_for_clipped_windows(query_db, timestamp, affects):
    # Falsifier: filtering the exact observation timestamp misses a diagnostic in a clipped intersecting bucket.
    query_db.execute("INSERT INTO store_diagnostic VALUES(1,'boot-A','gui',1,1,1,?,'counter_overflow','read_bytes')", (timestamp,))
    query_db.commit()
    window = Window("range", "boot-A", RAW_BUCKET_NS + 1, 3 * RAW_BUCKET_NS - 1)
    result = QueryService(query_db).series(1, window)
    assert result.coverage.counter_overflow is affects


def test_all_public_queries_use_one_read_snapshot_without_mutations(query_db):
    # Falsifier: any query runs outside a transaction or commits/mutates caller-owned state.
    service = QueryService(query_db)
    window = Window("range", "boot-A", 0, RAW_BUCKET_NS)
    calls = (
        lambda: service.ranking(window, "total_bytes"), lambda: service.series(1, window),
        lambda: service.disk_share(1, window),
        lambda: service.file_table(1, window, "bytes_total", "desc", None),
        lambda: service.file_coverage(1, window), lambda: service.unknown_owner_file_coverage(window),
    )
    for call in calls:
        statements = []
        query_db.set_trace_callback(statements.append)
        call()
        query_db.set_trace_callback(None)
        assert statements[0] == "BEGIN" and statements[-1] == "ROLLBACK"
        assert sum(statement == "BEGIN" for statement in statements) == 1
        assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE", "COMMIT")) for statement in statements)
        assert not query_db.in_transaction
    query_db.execute("BEGIN")
    service.series(1, window)
    assert query_db.in_transaction
    query_db.rollback()


def test_file_diagnostic_flags_survive_incomplete_or_absent_base(file_db):
    # Falsifier: short-circuiting completeness skips diagnostics when no file rows survived.
    file_db.execute("UPDATE boot SET file_finalized_through_mono_ns=?", (FILE_HOUR_NS,))
    file_db.execute("INSERT INTO store_diagnostic VALUES(1,'boot-A','gui',1,0,NULL,1,'counter_overflow','bytes_r')")
    file_db.commit()
    service = QueryService(file_db)
    window = Window("range", "boot-A", 0, FILE_HOUR_NS)
    assert service.file_coverage(1, window).coverage.counter_overflow
    assert service.unknown_owner_file_coverage(window).coverage.counter_overflow


def test_lifetime_file_pending_state_marks_tail_incomplete_without_process_rows(file_db):
    # Falsifier: pending file evidence is ignored when no process checkpoint gives a cutoff.
    finalized = HourlyAccumulator("boot-A", 0)
    finalized.add_observation(R, FILE_APP, observation(1, bytes_r=17), ZERO)
    store = FileHourStore(file_db)
    store.finalize_hour(finalized.finalize())
    pending = HourlyAccumulator("boot-A", FILE_HOUR_NS)
    pending.add_observation(R, FILE_APP, observation(2, bytes_r=99, last_seen_mono_ns=FILE_HOUR_NS + 1), ZERO)
    store.apply_pending(pending.finalize())
    result = QueryService(file_db).file_coverage(1, Window("lifetime"))
    assert result.observed_bytes_r == 17
    assert not result.coverage.complete
    assert result.coverage.unavailable_reason == "file_source_unavailable"


def test_missing_boot_file_window_reports_source_unavailable_without_crashing(file_db):
    # Falsifier: missing watermark rows are indexed, or an absent file source is labelled retained zero.
    service = QueryService(file_db)
    window = Window("range", "unknown-boot", 0, FILE_HOUR_NS)
    result = service.file_table(1, window, "bytes_total", "desc", None)
    assert result.rows == () and not result.coverage.complete
    assert result.coverage.unavailable_reason == "file_source_unavailable"
    assert not result.unknown_owner_overflow.coverage.complete


def test_file_totals_last_seen_follows_selected_boot_chronology_and_ignores_alias_history(file_db):
    # Falsifier: global path.last_mono_ns or cross-boot boottime determines a selected row's LAST.
    from dataclasses import replace
    first = HourlyAccumulator("boot-A", 0)
    first.add_observation(R, FILE_APP, observation(1, basename="same", bytes_r=7, last_seen_mono_ns=200), ZERO)
    FileHourStore(file_db).finalize_hour(first.finalize())
    file_db.execute("INSERT INTO boot VALUES('boot-B',2,0,0,2000.0,'gui')")
    file_db.execute("INSERT INTO clock_map VALUES('boot-B',0,2000000000000)")
    file_db.commit()
    second = HourlyAccumulator("boot-B", 0)
    second.add_observation(R, FILE_APP, replace(observation(1, basename="same", bytes_r=11), last_seen_mono_ns=50), ZERO)
    FileHourStore(file_db).finalize_hour(second.finalize())
    service = QueryService(file_db)
    row, = service.file_table(1, Window("lifetime"), "last_seen", "desc", None).rows
    assert (row.bytes_r, row.last_seen_boot_id, row.last_seen_mono_ns) == (18, "boot-B", 50)
    lifetime = service.file_coverage(1, Window("lifetime"))
    assert lifetime.coverage.tier == lifetime.unknown_owner_overflow.coverage.tier == "totals_only"
    assert service.file_table(1, Window("lifetime"), "bytes_total", "desc", None).coverage.tier == "totals_only"
    file_db.execute("DELETE FROM boot_file_overflow_unknown WHERE boot_id='boot-B'")
    file_db.commit()
    assert not service.unknown_owner_file_coverage(Window("lifetime")).coverage.complete
    row, = service.file_table(1, Window("range", "boot-A", 0, FILE_HOUR_NS), "last_seen", "desc", None).rows
    assert (row.bytes_r, row.last_seen_boot_id, row.last_seen_mono_ns) == (7, "boot-A", 200)


def test_file_counted_omissions_and_weak_identity_preserve_byte_completeness(file_db):
    # Falsifier: exact omitted bytes/weak identity invalidate F or are added to retained observations.
    from dataclasses import replace
    from appio.contracts import HookHits
    acc = HourlyAccumulator("boot-A", 0)
    cov = replace(ZERO, hook_hits=HookHits(1, 2, 3, 4, 5, 6))
    acc.add_observation(R, FILE_APP, observation(1, bytes_r=9, identity_weak=True), cov)
    acc.add_observation(R, FILE_APP, observation(2, bytes_r=3), cov)
    FileHourStore(file_db).finalize_hour(acc.finalize(1))
    result = QueryService(file_db).file_coverage(1, Window("range", "boot-A", 0, FILE_HOUR_NS))
    assert result.coverage.complete
    assert (result.observed_bytes_r, result.counted_omitted_bytes_r) == (9, 3)
    assert result.weak_identity_rows == 1
    assert result.hook_hits == HookHits(1, 2, 3, 4, 5, 6)


def test_lifetime_gap_overlay_applies_to_process_and_file_totals(file_db):
    # Falsifier: exact lifetime totals ignore independently persisted gaps after materialization.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, FILE_APP, observation(1), ZERO)
    FileHourStore(file_db).finalize_hour(acc.finalize())
    file_db.execute("INSERT INTO app_lifetime_total VALUES(1,7,3,0,0,?,'boot-A',1)", (coverage_json(device_state="available"),))
    file_db.execute("INSERT INTO gap VALUES(1,'boot-A','gui',1,1,5,6,'overlap_uncertain')")
    file_db.commit()
    service = QueryService(file_db)
    process, = service.ranking(Window("lifetime"), "total_bytes")
    file = service.file_coverage(1, Window("lifetime"))
    for result in (process.coverage, file.coverage, file.unknown_owner_overflow.coverage):
        assert not result.complete
        assert result.gap_intervals == result.overlap_uncertain_intervals == 1


def test_public_parameter_validation_does_not_coerce_identifiers_or_search(query_db):
    # Falsifier: bool/float IDs, unsupported enums, or a NUL search reach SQL as valid inputs.
    service = QueryService(query_db)
    window = Window("range", "boot-A", 0, RAW_BUCKET_NS)
    for app_id in (True, 1.0, 0, -1, 2**63):
        for call in (service.series, service.disk_share, service.file_coverage):
            with pytest.raises(ValueError):
                call(app_id, window)
        with pytest.raises(ValueError):
            service.file_table(app_id, window, "bytes_total", "asc", None)
    with pytest.raises(ValueError):
        service.ranking(window, "bogus")
    for sort, direction, search in (("bogus", "asc", None), ("bytes_r", "bogus", None), ("bytes_r", "asc", "x\0y")):
        with pytest.raises(ValueError):
            service.file_table(1, window, sort, direction, search)
    for call in (service.series, service.disk_share, service.file_coverage):
        with pytest.raises(KeyError):
            call(999, window)
    with pytest.raises(KeyError):
        service.file_table(999, window, "bytes_total", "asc", None)
