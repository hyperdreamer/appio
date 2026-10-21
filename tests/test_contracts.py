"""Contract tests for the shared P1 vocabulary in :mod:`appio.contracts`.

Each test pins one declared contract from specification section 8.1/8.2 and the
A22 message table. A test that could still pass while the contract was violated
is not listed here on purpose.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from appio.contracts import (
    FILE_HOUR_NS,
    HISTORICAL_BOOT_PREFIX_MESSAGE,
    LAUNCH_WINDOW_MESSAGE,
    MAX_RANGE_NS,
    RANGE_TOO_LONG_MESSAGE,
    RAW_BUCKET_NS,
    SQLITE_INT_MAX,
    AppTotal,
    CounterOverflowError,
    Coverage,
    DeviceSeriesPoint,
    DeviceShareRow,
    DiskShare,
    FileCoverage,
    FileCoverageInput,
    FileObservation,
    FileRow,
    FileTable,
    HookHits,
    HourlyFileSnapshot,
    ResidualPoint,
    Series,
    SeriesPoint,
    UnknownOwnerInput,
    UnknownOwnerOverflow,
    Window,
    WindowUnavailableError,
    validate_enum,
    validate_search,
    validate_sqlite_integer,
    validate_window,
)


def make_coverage(**overrides: object) -> Coverage:
    """A completely populated, available coverage value with no anomalies."""

    fields: dict[str, object] = {
        "tier": "raw",
        "complete": True,
        "unavailable_reason": None,
        "inaccessible_pids": 0,
        "resets": 0,
        "reap_corrections": 0,
        "reap_unapplied": (0, 0, 0, 0),
        "reap_unobserved": (0, 0, 0, 0),
        "partial_intervals": 0,
        "gap_intervals": 0,
        "overlap_uncertain_intervals": 0,
        "spans_suspend_intervals": 0,
        "spans_suspend": False,
        "reap_incomplete": False,
        "counter_overflow": False,
        "device_state": "available",
        "counted_omitted_files": 0,
        "counted_omitted_bytes_r": 0,
        "counted_omitted_bytes_w": 0,
        "uncounted_overflow_files": 0,
        "uncounted_overflow_bytes_r": 0,
        "uncounted_overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
        "weak_identity_rows": 0,
        "hook_hits": HookHits(0, 0, 0, 0, 0, 0),
    }
    fields.update(overrides)
    return Coverage(**fields)  # type: ignore[arg-type]


def make_series_point(**overrides: object) -> SeriesPoint:
    fields: dict[str, object] = {
        "bucket_start_mono_ns": 10_000_000_000,
        "bucket_secs": 10,
        "wall_start_seconds": 1_700_000_000.0,
        "read_bytes": 40,
        "write_bytes": 20,
        "peak_read_bps": 4,
        "peak_write_bps": 2,
        "peak_total_bps": 6,
        "spans_suspend": False,
    }
    fields.update(overrides)
    return SeriesPoint(**fields)  # type: ignore[arg-type]


def make_file_row(**overrides: object) -> FileRow:
    fields: dict[str, object] = {
        "path_id": 1,
        "basename": "notes.txt",
        "state": "resolved",
        "identity_weak": False,
        "dev": "259:0",
        "inode": 42,
        "generation": 1,
        "reads": 3,
        "writes": 4,
        "bytes_r": 400,
        "bytes_w": 200,
        "bytes_total": 600,
        "last_seen_boot_id": "boot-a",
        "last_seen_mono_ns": 20_000_000_000,
    }
    fields.update(overrides)
    return FileRow(**fields)  # type: ignore[arg-type]


def make_unknown_owner_overflow(**overrides: object) -> UnknownOwnerOverflow:
    fields: dict[str, object] = {
        "window": Window(kind="since_boot", boot_id="boot-a", end_mono_ns=1),
        "owner_key": "overflow_owner_unknown",
        "overflow_files": 2,
        "overflow_bytes_r": 30,
        "overflow_bytes_w": 40,
        "coverage": make_coverage(),
    }
    fields.update(overrides)
    return UnknownOwnerOverflow(**fields)  # type: ignore[arg-type]


# --- Window shapes -----------------------------------------------------------


@pytest.mark.parametrize(
    "window",
    [
        Window(kind="range", boot_id="boot-a", start_mono_ns=0, end_mono_ns=10),
        Window(kind="since_boot", boot_id="boot-a", end_mono_ns=10),
        Window(kind="launch", boot_id="boot-a", launch_id=7),
        Window(kind="lifetime"),
    ],
)
def test_window_accepts_each_valid_form(window: Window) -> None:
    """Fails if any valid form is rejected or its fields are coerced."""

    assert window.kind in {"range", "since_boot", "launch", "lifetime"}
    validate_window(window)


def test_window_range_keeps_its_declared_fields() -> None:
    """Fails if a constructor drops, reorders, or retypes a declared field."""

    window = Window(kind="range", boot_id="boot-a", start_mono_ns=0, end_mono_ns=10)
    assert window.boot_id == "boot-a"
    assert window.start_mono_ns == 0
    assert window.end_mono_ns == 10
    assert window.launch_id is None


def test_window_range_requires_boot_id() -> None:
    """Fails if a range without a boot identity is accepted."""

    with pytest.raises(ValueError):
        Window(kind="range", start_mono_ns=0, end_mono_ns=10)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (0, 0),
        (10, 9),
        (-1, 10),
        (0, SQLITE_INT_MAX + 1),
    ],
)
def test_window_rejects_nonpositive_reversed_or_out_of_range_range(
    start: int, end: int
) -> None:
    """Fails if the half-open range invariant or the SQLite bound is unenforced."""

    with pytest.raises(ValueError):
        Window(kind="range", boot_id="boot-a", start_mono_ns=start, end_mono_ns=end)


def test_window_since_boot_rejects_an_explicit_start() -> None:
    """Fails if since_boot silently accepts a start that changes its meaning."""

    with pytest.raises(ValueError):
        Window(kind="since_boot", boot_id="boot-a", start_mono_ns=5, end_mono_ns=10)


def test_window_since_boot_rejects_a_nonpositive_end() -> None:
    """Fails if a since_boot window can end at boot zero."""

    with pytest.raises(ValueError):
        Window(kind="since_boot", boot_id="boot-a", end_mono_ns=0)


@pytest.mark.parametrize("launch_id", [0, -1, None])
def test_window_launch_requires_a_positive_launch_id(launch_id: int | None) -> None:
    """Fails if a launch window is accepted without a real launch identity."""

    with pytest.raises(ValueError):
        Window(kind="launch", boot_id="boot-a", launch_id=launch_id)


def test_window_launch_rejects_endpoints() -> None:
    """Fails if a launch window can also carry monotonic endpoints."""

    with pytest.raises(ValueError):
        Window(kind="launch", boot_id="boot-a", launch_id=1, end_mono_ns=10)


def test_window_lifetime_rejects_a_boot_or_endpoint() -> None:
    """Fails if lifetime can smuggle in a boot or a bounded endpoint."""

    with pytest.raises(ValueError):
        Window(kind="lifetime", boot_id="boot-a")
    with pytest.raises(ValueError):
        Window(kind="lifetime", end_mono_ns=10)
    with pytest.raises(ValueError):
        Window(kind="lifetime", start_mono_ns=0)


def test_window_range_rejects_a_launch_id() -> None:
    """Fails if a range silently accepts launch attribution it cannot provide."""

    with pytest.raises(ValueError):
        Window(kind="range", boot_id="boot-a", start_mono_ns=0, end_mono_ns=10, launch_id=1)


def test_window_rejects_an_unknown_kind() -> None:
    """Fails if an out-of-vocabulary window kind is accepted."""

    with pytest.raises(ValueError):
        Window(kind="rolling")  # type: ignore[arg-type]


def test_window_rejects_boolean_integers() -> None:
    """Fails if a bool is accepted where an integer is required."""

    with pytest.raises(ValueError):
        Window(kind="range", boot_id="boot-a", start_mono_ns=True, end_mono_ns=10)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Window(kind="since_boot", boot_id="boot-a", end_mono_ns=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Window(kind="launch", boot_id="boot-a", launch_id=True)  # type: ignore[arg-type]


# --- Validators --------------------------------------------------------------


def test_validate_enum_rejects_a_value_outside_the_literal_set() -> None:
    """Fails if an enum validator only checks type, not membership."""

    assert validate_enum("asc", ("asc", "desc"), "direction") == "asc"
    with pytest.raises(ValueError):
        validate_enum("sideways", ("asc", "desc"), "direction")
    with pytest.raises(ValueError):
        validate_enum(True, ("asc", "desc"), "direction")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_validate_sqlite_integer_rejects_non_integers(value: object) -> None:
    """Fails if bool, float, str, or None passes an integer contract."""

    with pytest.raises(ValueError):
        validate_sqlite_integer(value, "rchar")


@pytest.mark.parametrize("value", [-1, -(2**63) - 1, SQLITE_INT_MAX + 1])
def test_validate_sqlite_integer_rejects_negative_or_out_of_range(value: int) -> None:
    """Fails if the SQLite INTEGER bound or nonnegative rule is unenforced."""

    with pytest.raises(ValueError):
        validate_sqlite_integer(value, "rchar")


def test_validate_sqlite_integer_accepts_declared_bounds() -> None:
    """Fails if an in-range integer, including both bounds, is rejected."""

    assert validate_sqlite_integer(0, "rchar") == 0
    assert validate_sqlite_integer(SQLITE_INT_MAX, "rchar") == SQLITE_INT_MAX


def test_validate_sqlite_integer_allows_signed_values_when_asked() -> None:
    """Fails if signed residuals cannot use the signed SQLite range."""

    assert validate_sqlite_integer(-1, "residual_read", nonnegative=False) == -1
    assert validate_sqlite_integer(-(2**63), "residual_read", nonnegative=False) == -(2**63)
    with pytest.raises(ValueError):
        validate_sqlite_integer(-(2**63) - 1, "residual_read", nonnegative=False)


def test_validate_search_rejects_nul_and_accepts_none_and_text() -> None:
    """Fails if a NUL can reach a query string or a plain search is rejected."""

    assert validate_search(None) is None
    assert validate_search("notes") == "notes"
    with pytest.raises(ValueError):
        validate_search("no\x00tes")
    with pytest.raises(ValueError):
        validate_search(7)  # type: ignore[arg-type]


# --- Exact A22 errors and messages ------------------------------------------


def test_window_unavailable_error_exposes_reason_and_exact_message() -> None:
    """Fails if .reason is missing or str() does not equal the A22 message."""

    error = WindowUnavailableError(
        "launch_attribution",
        "Launch window disabled: device and file activity are not stored per launch.",
    )
    assert isinstance(error, ValueError)
    assert error.reason == "launch_attribution"
    assert str(error) == (
        "Launch window disabled: device and file activity are not stored per launch."
    )


def test_window_unavailable_error_rejects_an_unknown_reason() -> None:
    """Fails if a reason outside the A22 vocabulary is silently accepted."""

    with pytest.raises(ValueError):
        WindowUnavailableError("because", "message")  # type: ignore[arg-type]


def test_counter_overflow_error_exposes_field_and_exact_message() -> None:
    """Fails if .field is missing or the message is reworded."""

    error = CounterOverflowError("read_bytes")
    assert isinstance(error, OverflowError)
    assert error.field == "read_bytes"
    assert str(error) == "Counter outside SQLite INTEGER range: read_bytes"


def test_a22_message_constants_are_exact() -> None:
    """Fails if any exact A22/query message is reworded by one character."""

    assert LAUNCH_WINDOW_MESSAGE == (
        "Launch window disabled: device and file activity are not stored per launch."
    )
    assert RANGE_TOO_LONG_MESSAGE == (
        "Ranges longer than 365 days are disabled: retained rollups cannot "
        "represent arbitrary ranges."
    )
    assert HISTORICAL_BOOT_PREFIX_MESSAGE == (
        "Historical boot prefix unavailable: totals cover the full collected boot."
    )


def test_shared_range_and_bucket_constants_are_exact() -> None:
    """Fails if the SQLite bound, 365-day bound, or bucket widths drift."""

    assert SQLITE_INT_MAX == 9223372036854775807
    assert MAX_RANGE_NS == 365 * 86400 * 1_000_000_000
    assert RAW_BUCKET_NS == 10_000_000_000
    assert FILE_HOUR_NS == 3_600_000_000_000


# --- Frozen result containers ------------------------------------------------


def test_coverage_constructs_with_every_declared_field() -> None:
    """Fails if Coverage drops a counter, a reap tuple, or a flag."""

    coverage = make_coverage(
        tier="rollup_60s",
        complete=False,
        unavailable_reason="not_retained",
        inaccessible_pids=1,
        resets=2,
        reap_corrections=3,
        reap_unapplied=(1, 2, 3, 4),
        reap_unobserved=(5, 6, 7, 8),
        partial_intervals=9,
        gap_intervals=10,
        overlap_uncertain_intervals=11,
        spans_suspend_intervals=12,
        spans_suspend=True,
        reap_incomplete=True,
        counter_overflow=True,
        device_state="not_delegated",
        counted_omitted_files=13,
        counted_omitted_bytes_r=14,
        counted_omitted_bytes_w=15,
        uncounted_overflow_files=16,
        uncounted_overflow_bytes_r=17,
        uncounted_overflow_bytes_w=18,
        skipped_nonfile=19,
        skipped_unsupported_fs=20,
        weak_identity_rows=21,
        hook_hits=HookHits(1, 2, 3, 4, 5, 6),
    )
    assert coverage.tier == "rollup_60s"
    assert coverage.unavailable_reason == "not_retained"
    assert coverage.reap_unapplied == (1, 2, 3, 4)
    assert coverage.reap_unobserved == (5, 6, 7, 8)
    assert coverage.device_state == "not_delegated"
    assert coverage.hook_hits == HookHits(1, 2, 3, 4, 5, 6)


def test_coverage_rejects_bad_tier_and_negative_counters() -> None:
    """Fails if Coverage accepts an unknown tier or a negative counter."""

    with pytest.raises(ValueError):
        make_coverage(tier="hourly")
    with pytest.raises(ValueError):
        make_coverage(gap_intervals=-1)
    with pytest.raises(ValueError):
        make_coverage(device_state="maybe")
    with pytest.raises(ValueError):
        make_coverage(reap_unapplied=(0, 0, 0))


def test_app_total_total_bytes_is_integer_read_plus_write() -> None:
    """Fails if the total is trusted, coerced, or not read+write."""

    total = AppTotal(
        app_id=1,
        app_key="scope:/session/unknown",
        name="unknown",
        read_bytes=400,
        write_bytes=200,
        total_bytes=600,
        rchar=1_000,
        wchar=500,
        peak_read_bps=None,
        peak_write_bps=None,
        peak_total_bps=None,
        coverage=make_coverage(),
    )
    assert total.total_bytes == total.read_bytes + total.write_bytes
    with pytest.raises(ValueError):
        AppTotal(
            app_id=1,
            app_key="scope:/session/unknown",
            name="unknown",
            read_bytes=400,
            write_bytes=200,
            total_bytes=601,
            rchar=1_000,
            wchar=500,
            peak_read_bps=None,
            peak_write_bps=None,
            peak_total_bps=None,
            coverage=make_coverage(),
        )
    with pytest.raises(ValueError):
        AppTotal(
            app_id=1,
            app_key="scope:/session/unknown",
            name="unknown",
            read_bytes=400,
            write_bytes=200,
            total_bytes=600.0,  # type: ignore[arg-type]
            rchar=1_000,
            wchar=500,
            peak_read_bps=None,
            peak_write_bps=None,
            peak_total_bps=None,
            coverage=make_coverage(),
        )


def test_series_retains_none_residuals_and_empty_device_points() -> None:
    """Fails if an unavailable residual is replaced by a zero."""

    series = Series(
        app_id=1,
        window=Window(kind="since_boot", boot_id="boot-a", end_mono_ns=10),
        tier="raw",
        points=(make_series_point(),),
        device_points=(),
        residual_points=(
            ResidualPoint(
                bucket_start_mono_ns=10_000_000_000,
                bucket_secs=10,
                total=None,
                read=None,
                write=None,
            ),
        ),
        coverage=make_coverage(complete=False, device_state="not_delegated"),
    )
    residual = series.residual_points[0]
    assert residual.total is None
    assert residual.read is None
    assert residual.write is None
    assert series.device_points == ()


def test_series_point_keeps_none_peaks_for_unavailable_rates() -> None:
    """Fails if an unavailable peak rate becomes an integer zero."""

    point = make_series_point(peak_read_bps=None, peak_write_bps=None, peak_total_bps=None)
    assert point.peak_read_bps is None
    assert point.peak_write_bps is None
    assert point.peak_total_bps is None


def test_device_series_point_keeps_raw_dev_token() -> None:
    """Fails if a major:minor token is rewritten or merged across boots."""

    point = DeviceSeriesPoint(
        bucket_start_mono_ns=0, bucket_secs=10, dev="259:0", rbytes=40, wbytes=20
    )
    assert point.dev == "259:0"


def test_device_share_row_total_bytes_is_rbytes_plus_wbytes() -> None:
    """Fails if a device row total is not the exact integer sum."""

    row = DeviceShareRow(boot_id="boot-a", dev="259:0", rbytes=40, wbytes=20, total_bytes=60)
    assert row.total_bytes == 60
    with pytest.raises(ValueError):
        DeviceShareRow(boot_id="boot-a", dev="259:0", rbytes=40, wbytes=20, total_bytes=61)


def test_disk_share_retains_none_device_and_residual_values() -> None:
    """Fails if an unavailable device split is silently zero-filled."""

    share = DiskShare(
        app_id=1,
        window=Window(kind="lifetime"),
        task_read_bytes=200,
        task_write_bytes=100,
        task_total_bytes=300,
        devices=(),
        device_read_bytes=None,
        device_write_bytes=None,
        device_total_bytes=None,
        residual_read=None,
        residual_write=None,
        residual_total=None,
        coverage=make_coverage(complete=False, device_state="not_delegated"),
    )
    assert share.task_total_bytes == 300
    assert share.device_total_bytes is None
    assert share.residual_total is None


def test_disk_share_task_total_is_read_plus_write() -> None:
    """Fails if the task total is not the integer sum of the task bytes."""

    with pytest.raises(ValueError):
        DiskShare(
            app_id=1,
            window=Window(kind="lifetime"),
            task_read_bytes=200,
            task_write_bytes=100,
            task_total_bytes=301,
            devices=(),
            device_read_bytes=None,
            device_write_bytes=None,
            device_total_bytes=None,
            residual_read=None,
            residual_write=None,
            residual_total=None,
            coverage=make_coverage(),
        )


def test_file_row_bytes_total_is_bytes_r_plus_bytes_w() -> None:
    """Fails if a file row total is not the exact integer sum."""

    row = make_file_row()
    assert row.bytes_total == row.bytes_r + row.bytes_w
    with pytest.raises(ValueError):
        make_file_row(bytes_total=601)
    with pytest.raises(ValueError):
        make_file_row(bytes_total=600.0)


def test_file_row_rejects_an_unknown_state() -> None:
    """Fails if a file row state outside the declared literal set is accepted."""

    with pytest.raises(ValueError):
        make_file_row(state="partial")


def test_file_table_constructs_with_rows_sort_and_search() -> None:
    """Fails if FileTable drops the row tuple, sort, direction, or search."""

    table = FileTable(
        app_id=1,
        window=Window(kind="since_boot", boot_id="boot-a", end_mono_ns=10),
        rows=(make_file_row(),),
        sort="bytes_total",
        direction="desc",
        search="notes",
        unknown_owner_overflow=make_unknown_owner_overflow(),
        coverage=make_coverage(),
    )
    assert table.rows[0].basename == "notes.txt"
    assert table.sort == "bytes_total"
    assert table.direction == "desc"
    assert table.search == "notes"
    assert table.unknown_owner_overflow.owner_key == "overflow_owner_unknown"


def test_file_table_rejects_a_sort_outside_the_literal_set() -> None:
    """Fails if an unknown file sort key reaches the query contract."""

    with pytest.raises(ValueError):
        FileTable(
            app_id=1,
            window=Window(kind="lifetime"),
            rows=(),
            sort="bytes",
            direction="desc",
            search=None,
            unknown_owner_overflow=make_unknown_owner_overflow(),
            coverage=make_coverage(),
        )


def test_file_table_rejects_nul_in_search() -> None:
    """Fails if a NUL search string is accepted by a result constructor."""

    with pytest.raises(ValueError):
        FileTable(
            app_id=1,
            window=Window(kind="lifetime"),
            rows=(),
            sort="bytes_total",
            direction="desc",
            search="no\x00tes",
            unknown_owner_overflow=make_unknown_owner_overflow(),
            coverage=make_coverage(),
        )


def test_file_coverage_constructs_with_observed_and_overflow_fields() -> None:
    """Fails if FileCoverage drops observed bytes or the unknown-owner context."""

    file_coverage = FileCoverage(
        app_id=1,
        window=Window(kind="since_boot", boot_id="boot-a", end_mono_ns=10),
        counted_omitted_files=1,
        counted_omitted_bytes_r=2,
        counted_omitted_bytes_w=3,
        uncounted_overflow_files=4,
        uncounted_overflow_bytes_r=5,
        uncounted_overflow_bytes_w=6,
        skipped_nonfile=7,
        skipped_unsupported_fs=8,
        weak_identity_rows=9,
        hook_hits=HookHits(1, 2, 3, 4, 5, 6),
        observed_bytes_r=10,
        observed_bytes_w=11,
        unknown_owner_overflow=make_unknown_owner_overflow(),
        coverage=make_coverage(),
    )
    assert file_coverage.observed_bytes_r == 10
    assert file_coverage.observed_bytes_w == 11
    assert file_coverage.unknown_owner_overflow.owner_key == "overflow_owner_unknown"


def test_unknown_owner_overflow_rejects_a_different_owner_key() -> None:
    """Fails if unknown-owner bytes can be assigned to an app key."""

    with pytest.raises(ValueError):
        make_unknown_owner_overflow(owner_key="scope:/app/x")


# --- Transport dataclasses ---------------------------------------------------


def test_file_observation_constructs_without_database_local_ids() -> None:
    """Fails if FileObservation requires a path_id or drops a declared field."""

    observation = FileObservation(
        app_key="scope:/session/unknown",
        fs_uuid="fs-1",
        dev="259:0",
        inode=42,
        generation=1,
        basename="notes.txt",
        state="resolved",
        identity_weak=False,
        reads=3,
        writes=4,
        bytes_r=400,
        bytes_w=200,
        last_seen_mono_ns=20_000_000_000,
    )
    assert observation.bytes_r == 400
    assert not hasattr(observation, "path_id")


def test_file_coverage_input_and_unknown_owner_input_construct() -> None:
    """Fails if either transport coverage record drops a declared field."""

    coverage_input = FileCoverageInput(
        counted_omitted_files=1,
        counted_omitted_bytes_r=2,
        counted_omitted_bytes_w=3,
        uncounted_overflow_files=4,
        uncounted_overflow_bytes_r=5,
        uncounted_overflow_bytes_w=6,
        skipped_nonfile=7,
        skipped_unsupported_fs=8,
        weak_identity_rows=9,
        hook_hits=HookHits(1, 2, 3, 4, 5, 6),
        gap_intervals=10,
        overlap_uncertain_intervals=11,
        complete=False,
    )
    unknown_owner_input = UnknownOwnerInput(
        overflow_files=1,
        overflow_bytes_r=2,
        overflow_bytes_w=3,
        gap_intervals=4,
        overlap_uncertain_intervals=5,
        complete=False,
    )
    assert coverage_input.hook_hits == HookHits(1, 2, 3, 4, 5, 6)
    assert coverage_input.complete is False
    assert unknown_owner_input.overflow_files == 1
    assert unknown_owner_input.complete is False


def test_hourly_file_snapshot_constructs_with_unknown_owner() -> None:
    """Fails if the hourly snapshot drops its boot, hour, apps, or owner block."""

    observation = FileObservation(
        app_key="scope:/session/unknown",
        fs_uuid="fs-1",
        dev="259:0",
        inode=42,
        generation=1,
        basename="notes.txt",
        state="resolved",
        identity_weak=False,
        reads=3,
        writes=4,
        bytes_r=400,
        bytes_w=200,
        last_seen_mono_ns=20_000_000_000,
    )
    coverage_input = FileCoverageInput(
        counted_omitted_files=0,
        counted_omitted_bytes_r=0,
        counted_omitted_bytes_w=0,
        uncounted_overflow_files=0,
        uncounted_overflow_bytes_r=0,
        uncounted_overflow_bytes_w=0,
        skipped_nonfile=0,
        skipped_unsupported_fs=0,
        weak_identity_rows=0,
        hook_hits=HookHits(0, 0, 0, 0, 0, 0),
        gap_intervals=0,
        overlap_uncertain_intervals=0,
        complete=True,
    )
    snapshot = HourlyFileSnapshot(
        boot_id="boot-a",
        hour_start_mono_ns=FILE_HOUR_NS,
        apps=(("scope:/session/unknown", (observation,), coverage_input),),
        unknown_owner=UnknownOwnerInput(
            overflow_files=0,
            overflow_bytes_r=0,
            overflow_bytes_w=0,
            gap_intervals=0,
            overlap_uncertain_intervals=0,
            complete=True,
        ),
    )
    assert snapshot.hour_start_mono_ns == 3_600_000_000_000
    assert snapshot.apps[0][0] == "scope:/session/unknown"
    assert snapshot.apps[0][1] == (observation,)
    assert snapshot.unknown_owner.overflow_files == 0


# --- Frozenness --------------------------------------------------------------


def test_contract_values_are_frozen() -> None:
    """Fails if a result container can be mutated after construction."""

    window = Window(kind="lifetime")
    with pytest.raises(FrozenInstanceError):
        window.start_mono_ns = 1  # type: ignore[misc]
    coverage = make_coverage()
    with pytest.raises(FrozenInstanceError):
        coverage.complete = False  # type: ignore[misc]
    row = make_file_row()
    with pytest.raises(FrozenInstanceError):
        row.bytes_total = 0  # type: ignore[misc]
