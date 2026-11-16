"""Shared pure selector validation: coverage unions and series chronology.

These are foundation checks for :func:`validate_query_result` and the private
local-value helpers; later tasks extend the same two tests with real client and
router routes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from appio.contracts import (
    MAX_RANGE_NS,
    RANGE_TOO_LONG_MESSAGE,
    DeviceSeriesPoint,
    FileCoverage,
    FileRow,
    FileTable,
    HookHits,
    ResidualPoint,
    Series,
    SeriesPoint,
    UnknownOwnerOverflow,
    Window,
    WindowUnavailableError,
)
from appio.history_validation import (
    _normalize_request,
    _validate_local_coverage,
    _validate_local_ranking,
    _validate_local_series,
    _validate_local_totals,
    validate_query_result,
)
from appio.ipc_models import QueryRequest
from live_overview_support import coverage, share, total

KEY = "unit:alpha.service"
OTHER_KEY = "unit:beta.service"
SNAPSHOT = "a" * 64
BOOT = "boot-a"
RAW_SECS = 10

_MISSING = object()


def _file_coverage(window: Window, *, app_id: int = 7, unavailable: bool = False,
                   served: Window | None = None) -> FileCoverage:
    return FileCoverage(
        app_id=app_id,
        window=window,
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
        observed_bytes_r=17,
        observed_bytes_w=5,
        unknown_owner_overflow=_unknown_owner(
            served if served is not None else window,
            unavailable=unavailable,
        ),
        coverage=coverage(unavailable_reason="not_retained") if unavailable else coverage(),
    )


def _unknown_owner(window: Window, *, unavailable: bool = False) -> UnknownOwnerOverflow:
    return UnknownOwnerOverflow(
        window=window,
        owner_key="overflow_owner_unknown",
        overflow_files=0,
        overflow_bytes_r=0,
        overflow_bytes_w=0,
        coverage=coverage(unavailable_reason="not_retained") if unavailable else coverage(),
    )


def _file_row(path_id: int) -> FileRow:
    return FileRow(
        path_id=path_id,
        basename=f"file-{path_id}.txt",
        state="resolved",
        identity_weak=False,
        dev="8:0",
        inode=1_000 + path_id,
        generation=1,
        reads=1,
        writes=1,
        bytes_r=17,
        bytes_w=5,
        bytes_total=22,
        last_seen_boot_id=BOOT,
        last_seen_mono_ns=1,
    )


def _file_table(window: Window, *, rows: int = 0,
                served: Window | None = None) -> FileTable:
    return FileTable(
        app_id=7,
        window=window,
        rows=tuple(_file_row(path_id) for path_id in range(1, rows + 1)),
        sort="bytes_total",
        direction="desc",
        search=None,
        unknown_owner_overflow=_unknown_owner(
            served if served is not None else window
        ),
        coverage=coverage(),
    )


def _response(kind: str, result: object, *, app_key: object = _MISSING,
              next_cursor: str | None = None) -> dict[str, object]:
    response: dict[str, object] = {
        "kind": kind,
        "result": result,
        "snapshot": SNAPSHOT,
        "next_cursor": next_cursor,
    }
    if app_key is not _MISSING:
        response["app_key"] = app_key
    return response


def _raw_series(app_id: int, window: Window) -> Series:
    """§5.4's normative fixture: two 10-second buckets, two devices per bucket."""

    points = (
        SeriesPoint(0, RAW_SECS, 1_700_000_000.0, 7, 3, None, None, None, False),
        SeriesPoint(10_000_000_000, RAW_SECS, 1_700_000_010.0, 7, 3, None, None, None, False),
    )
    device_points = (
        DeviceSeriesPoint(0, RAW_SECS, "8:0", 2, 1),
        DeviceSeriesPoint(0, RAW_SECS, "8:16", 2, 1),
        DeviceSeriesPoint(10_000_000_000, RAW_SECS, "8:0", 2, 1),
        DeviceSeriesPoint(10_000_000_000, RAW_SECS, "8:16", 2, 1),
    )
    residual_points = (
        ResidualPoint(0, RAW_SECS, 4, 3, 1),
        ResidualPoint(10_000_000_000, RAW_SECS, 4, 3, 1),
    )
    return Series(
        app_id=app_id,
        window=window,
        tier="raw",
        points=points,
        device_points=device_points,
        residual_points=residual_points,
        coverage=coverage(tier="raw"),
    )


def test_each_coverage_selector_accepts_only_its_contract_shape() -> None:
    lived = Window("lifetime")

    # Unscoped process coverage: a mapping of valid AppKeys to Coverage values.
    request = QueryRequest("coverage", Window("lifetime"))
    valid = {
        "kind": "coverage",
        "snapshot": "a" * 64,
        "next_cursor": None,
        "result": {"unit:alpha.service": coverage()},
    }
    validate_query_result(request, valid)
    for mapping in (
        {"unit:/alpha.service": coverage()},
        {"unit:alpha.service": "not a Coverage"},
    ):
        with pytest.raises(ValueError):
            validate_query_result(request, {**valid, "result": mapping})
    # Independent single-fault controls: both malformed mappings keep every
    # other envelope/request field valid, and the passing control stays accepted.
    assert validate_query_result(request, valid) is None
    validate_query_result(request, {**valid, "result": {}})
    validate_query_result(
        request,
        {
            **valid,
            "result": {"unit:alpha.service": coverage(), "unit:beta.service": coverage(resets=1)},
        },
    )
    for alternative in (
        coverage(),  # a scalar in the unscoped slot
        _file_coverage(lived),
        _unknown_owner(lived),
        ["unit:alpha.service"],
        "unit:alpha.service",
        None,
        7,
    ):
        with pytest.raises(ValueError):
            validate_query_result(request, {**valid, "result": alternative})
    with pytest.raises(ValueError):
        validate_query_result(request, {**valid, "kind": "files"})
    with pytest.raises(ValueError):
        validate_query_result(request, {**valid, "next_cursor": "opaque"})

    # Scoped process coverage: exactly Coverage; the request supplies the
    # subject and Window, so no invented wire echo is required.
    scoped = QueryRequest("coverage", lived, app_key=KEY)
    scoped_valid = _response("coverage", coverage())
    validate_query_result(scoped, scoped_valid)
    validate_query_result(scoped, _response("coverage", coverage(tier="rollup_60s")))
    for alternative in (
        {"unit:alpha.service": coverage()},
        _file_coverage(lived),
        _unknown_owner(lived),
        "not a Coverage",
        None,
        7,
    ):
        with pytest.raises(ValueError):
            validate_query_result(scoped, {**scoped_valid, "result": alternative})
    with pytest.raises(ValueError):
        validate_query_result(scoped, {**scoped_valid, "next_cursor": "opaque"})

    # File coverage: exact requested Window, echoed subject and a defined
    # served interval inside the requested bounds.
    file_window = Window("range", BOOT, 1_000_000_000, 20_000_000_000)
    file_request = QueryRequest(
        "coverage", file_window, app_key=KEY, coverage_kind="file"
    )
    served = Window("range", BOOT, 2_000_000_000, 10_000_000_000)
    file_valid = _response(
        "coverage", _file_coverage(file_window, served=served), app_key=KEY
    )
    validate_query_result(file_request, file_valid)
    for alternative in (
        coverage(),
        _unknown_owner(file_window),
        {"unit:alpha.service": coverage()},
        None,
        7,
    ):
        with pytest.raises(ValueError):
            validate_query_result(file_request, {**file_valid, "result": alternative})
    with pytest.raises(ValueError):
        validate_query_result(
            file_request,
            {
                **file_valid,
                "result": _file_coverage(
                    Window("range", BOOT, 1_000_000_000, 30_000_000_000)
                ),
            },
        )
    with pytest.raises(ValueError):
        validate_query_result(file_request, {**file_valid, "app_key": OTHER_KEY})
    without_echo = {key: value for key, value in file_valid.items() if key != "app_key"}
    with pytest.raises(ValueError):
        validate_query_result(file_request, without_echo)
    with pytest.raises(ValueError):
        validate_query_result(
            file_request,
            {**file_valid, "result": _file_coverage(file_window, unavailable=True)},
        )
    with pytest.raises(ValueError):
        validate_query_result(
            file_request,
            {
                **file_valid,
                "result": _file_coverage(
                    file_window, served=Window("range", "boot-b", 2_000_000_000, 10_000_000_000)
                ),
            },
        )
    with pytest.raises(ValueError):
        validate_query_result(
            file_request,
            {
                **file_valid,
                "result": _file_coverage(
                    file_window, served=Window("range", BOOT, 2_000_000_000, 25_000_000_000)
                ),
            },
        )

    # Unknown-owner overflow: the literal owner key, valid nested Coverage and
    # the existing served-interval window semantics.
    overflow_request = QueryRequest(
        "coverage", file_window, coverage_kind="unknown_owner"
    )
    overflow_valid = _response(
        "coverage", _unknown_owner(Window("range", BOOT, 2_000_000_000, 10_000_000_000))
    )
    validate_query_result(overflow_request, overflow_valid)
    for alternative in (
        coverage(),
        _file_coverage(file_window),
        {"unit:alpha.service": coverage()},
        None,
    ):
        with pytest.raises(ValueError):
            validate_query_result(overflow_request, {**overflow_valid, "result": alternative})
    with pytest.raises(ValueError):
        validate_query_result(
            overflow_request,
            {**overflow_valid, "result": _unknown_owner(Window("range", "boot-b", 2_000_000_000, 10_000_000_000))},
        )
    with pytest.raises(ValueError):
        validate_query_result(
            overflow_request,
            {**overflow_valid, "result": _unknown_owner(Window("range", BOOT, 2_000_000_000, 25_000_000_000))},
        )
    with pytest.raises(ValueError):
        validate_query_result(overflow_request, {**overflow_valid, "next_cursor": "opaque"})
    # A lifetime request has no boot bounds; the existing compatibility carrier
    # still requires a valid served Window.
    lifetime_overflow = QueryRequest("coverage", lived, coverage_kind="unknown_owner")
    validate_query_result(lifetime_overflow, _response("coverage", _unknown_owner(lived)))

    # -- supported whole-boot / lifetime served carriers (all selectors) ----- #
    # A totals-only since-boot answer retains the exact requested Window and
    # invents no endpoints; this is the producer's own carrier.
    boot_window = Window("since_boot", BOOT, end_mono_ns=20_000_000_000)
    inside = Window("range", BOOT, 2_000_000_000, 10_000_000_000)
    boot_file_request = QueryRequest(
        "coverage", boot_window, app_key=KEY, coverage_kind="file"
    )
    boot_file = _response("coverage", _file_coverage(boot_window), app_key=KEY)
    validate_query_result(boot_file_request, boot_file)
    validate_query_result(
        boot_file_request,
        {**boot_file, "result": _file_coverage(boot_window, served=inside)},
    )
    boot_overflow_request = QueryRequest(
        "coverage", boot_window, coverage_kind="unknown_owner"
    )
    boot_overflow = _response("coverage", _unknown_owner(boot_window))
    validate_query_result(boot_overflow_request, boot_overflow)
    validate_query_result(
        boot_overflow_request,
        {**boot_overflow, "result": _unknown_owner(inside)},
    )
    boot_files_request = QueryRequest("files", boot_window, app_key=KEY)
    boot_table = _response("files", _file_table(boot_window), app_key=KEY)
    validate_query_result(boot_files_request, boot_table)
    # A bounded served range must keep the request's boot and lie inside the
    # requested since-boot bounds.
    wrong_boot = Window("range", "boot-b", 2_000_000_000, 10_000_000_000)
    beyond_boot = Window("range", BOOT, 2_000_000_000, 25_000_000_000)
    for wrong in (wrong_boot, beyond_boot):
        with pytest.raises(ValueError):
            validate_query_result(
                boot_file_request,
                {**boot_file, "result": _file_coverage(boot_window, served=wrong)},
            )
        with pytest.raises(ValueError):
            validate_query_result(
                boot_overflow_request,
                {**boot_overflow, "result": _unknown_owner(wrong)},
            )
        with pytest.raises(ValueError):
            validate_query_result(
                boot_files_request,
                {**boot_table, "result": _file_table(boot_window, served=wrong)},
            )

    # The lifetime carrier must remain the requested lifetime Window: an
    # arbitrary range (any boot) or a launch Window is another scope.
    lifetime_file_request = QueryRequest(
        "coverage", lived, app_key=KEY, coverage_kind="file"
    )
    lifetime_file = _response("coverage", _file_coverage(lived), app_key=KEY)
    validate_query_result(lifetime_file_request, lifetime_file)
    lifetime_files_request = QueryRequest("files", lived, app_key=KEY)
    lifetime_table = _response("files", _file_table(lived), app_key=KEY)
    validate_query_result(lifetime_files_request, lifetime_table)
    for wrong in (
        Window("range", BOOT, 2_000_000_000, 10_000_000_000),
        Window("launch", BOOT, launch_id=7),
    ):
        with pytest.raises(ValueError):
            validate_query_result(
                lifetime_file_request,
                {**lifetime_file, "result": _file_coverage(lived, served=wrong)},
            )
        with pytest.raises(ValueError):
            validate_query_result(
                lifetime_overflow,
                _response("coverage", _unknown_owner(wrong)),
            )
        with pytest.raises(ValueError):
            validate_query_result(
                lifetime_files_request,
                {**lifetime_table, "result": _file_table(lived, served=wrong)},
            )

    # File coverage joins the Files page sequence, so it needs a valid snapshot
    # even though plain process coverage does not carry one at this boundary.
    validate_query_result(file_request, file_valid)
    with pytest.raises(ValueError):
        validate_query_result(file_request, {**file_valid, "snapshot": None})
    with pytest.raises(ValueError):
        validate_query_result(file_request, {**file_valid, "snapshot": "g" * 64})
    validate_query_result(scoped, {**scoped_valid, "snapshot": None})

    # A remote Files page obeys the normalized request's page limit; the
    # remote-only bound is exactly-at-limit, not a smaller page.
    at_limit_request = QueryRequest("files", file_window, app_key=KEY, limit=2)
    at_limit = _response("files", _file_table(file_window, rows=2), app_key=KEY)
    validate_query_result(at_limit_request, at_limit)
    validate_query_result(
        at_limit_request, {**at_limit, "result": _file_table(file_window, rows=1)}
    )
    with pytest.raises(ValueError):
        validate_query_result(
            at_limit_request, {**at_limit, "result": _file_table(file_window, rows=3)}
        )
    one_request = QueryRequest("files", file_window, app_key=KEY, limit=1)
    with pytest.raises(ValueError):
        # The review's case: an otherwise valid two-row page for limit=1.
        validate_query_result(one_request, at_limit)

    # A remote ranking result is a JSON list; only the local worker outcome
    # keeps the tuple shape, and an empty local ranking stays valid.
    ranking_request = QueryRequest("ranking", lived, limit=50)
    ranking_rows = [total(7, KEY), total(8, OTHER_KEY)]
    ranking_response = _response("ranking", ranking_rows)
    validate_query_result(ranking_request, ranking_response)
    with pytest.raises(ValueError):
        validate_query_result(
            ranking_request, {**ranking_response, "result": tuple(ranking_rows)}
        )
    _validate_local_ranking(ranking_request, tuple(ranking_rows))
    _validate_local_ranking(ranking_request, [])
    with pytest.raises(ValueError):
        _validate_local_ranking(ranking_request, ({"app_key": KEY},))

    # The private local helpers accept the actual value/key/request directly.
    _validate_local_coverage(request, None, {"unit:alpha.service": coverage()})
    _validate_local_coverage(scoped, KEY, coverage())
    with pytest.raises(ValueError):
        _validate_local_coverage(scoped, KEY, {"unit:alpha.service": coverage()})
    with pytest.raises(ValueError):
        _validate_local_coverage(scoped, OTHER_KEY, coverage())

    # Selector applicability and the A22 refusal live in the local normalizer.
    with pytest.raises(ValueError):
        _normalize_request(QueryRequest("coverage", lived, coverage_kind="file"))
    with pytest.raises(ValueError):
        _normalize_request(QueryRequest("coverage", lived, app_key=KEY, coverage_kind="unknown_owner"))
    with pytest.raises(ValueError):
        _normalize_request(QueryRequest("series", lived, app_key=KEY, coverage_kind="file"))
    with pytest.raises(WindowUnavailableError):
        _normalize_request(QueryRequest("coverage", Window("launch", BOOT, launch_id=7)))
    # A22's overlong-range refusal precedes subject/selector resolution and the
    # exact MAX_RANGE_NS boundary stays valid, as on the wire path.
    overlong = Window("range", BOOT, 0, MAX_RANGE_NS + 1)
    with pytest.raises(WindowUnavailableError) as refusal:
        _normalize_request(QueryRequest("totals", overlong, app_key="not a key"))
    assert refusal.value.reason == "unsupported_window"
    assert str(refusal.value) == RANGE_TOO_LONG_MESSAGE
    boundary = Window("range", BOOT, 0, MAX_RANGE_NS)
    assert _normalize_request(QueryRequest("totals", boundary, app_key=KEY)) == QueryRequest(
        "totals", boundary, app_key=KEY
    )
    # A cursor with NUL is rejected locally exactly as the wire path rejects it.
    for kind, window in (("files", file_window), ("ranking", lived)):
        with pytest.raises(ValueError):
            _normalize_request(
                QueryRequest(kind, window, app_key=KEY if kind == "files" else None,
                             cursor="x\x00y")
            )
    assert _normalize_request(
        QueryRequest("files", file_window, app_key=KEY, cursor="opaque")
    ).cursor == "opaque"
    assert _normalize_request(request) == request


def test_series_accepts_simultaneous_devices_and_rejects_per_device_overlap_or_order() -> None:
    window = Window("range", BOOT, 0, 40_000_000_000)
    value = _raw_series(7, window)
    assert [(point.bucket_start_mono_ns, point.bucket_secs) for point in value.points] == [
        (0, RAW_SECS),
        (10_000_000_000, RAW_SECS),
    ]
    assert [(point.bucket_start_mono_ns, point.dev) for point in value.device_points] == [
        (0, "8:0"),
        (0, "8:16"),
        (10_000_000_000, "8:0"),
        (10_000_000_000, "8:16"),
    ]
    assert all(point.rbytes > 0 and point.wbytes > 0 for point in value.device_points)
    assert all(point.bucket_secs == RAW_SECS for point in value.residual_points)

    request = QueryRequest("series", window, app_key=KEY)
    response = _response("series", value, app_key=KEY)
    # Remote and local routes accept the identical fixture unchanged.
    validate_query_result(request, response)
    assert response["result"] is value
    _validate_local_series(request, KEY, value)
    assert _normalize_request(request) == request

    def rejected(candidate: Series) -> None:
        with pytest.raises(ValueError):
            validate_query_result(request, {**response, "result": candidate})
        with pytest.raises(ValueError):
            _validate_local_series(request, KEY, candidate)

    # Duplicate (0, 8:0).
    rejected(
        replace(
            value,
            device_points=(
                value.device_points[0],
                value.device_points[0],
                value.device_points[1],
                value.device_points[2],
                value.device_points[3],
            ),
        )
    )
    # Same-device overlap: an extra 8:0 bucket starting inside the first one.
    rejected(
        replace(
            value,
            device_points=(
                value.device_points[0],
                value.device_points[1],
                DeviceSeriesPoint(5_000_000_000, RAW_SECS, "8:0", 2, 1),
                value.device_points[2],
                value.device_points[3],
            ),
        )
    )
    # Reversed UTF-8 device order within a bucket.
    rejected(
        replace(
            value,
            device_points=(
                value.device_points[1],
                value.device_points[0],
                value.device_points[2],
                value.device_points[3],
            ),
        )
    )
    # Reversed bucket order in every sequence at once.
    rejected(
        replace(
            value,
            points=tuple(reversed(value.points)),
            device_points=tuple(reversed(value.device_points)),
            residual_points=tuple(reversed(value.residual_points)),
        )
    )
    # Corrupt task chronology alone; device/residual stay valid, so only the
    # task sequence predicate can reject it.
    rejected(replace(value, points=tuple(reversed(value.points))))
    # Corrupt residual chronology alone.
    rejected(replace(value, residual_points=tuple(reversed(value.residual_points))))
    # Task-bucket overlap is refused separately from device chronology.
    rejected(
        replace(
            value,
            points=(
                value.points[0],
                replace(value.points[1], bucket_start_mono_ns=5_000_000_000),
            ),
            residual_points=(
                value.residual_points[0],
                replace(value.residual_points[1], bucket_start_mono_ns=5_000_000_000),
            ),
        )
    )
    # Tier/coverage-tier disagreement.
    rejected(replace(value, coverage=coverage(tier="rollup_60s")))
    # Tier duration alone: 60-second buckets with correctly spaced starts under
    # the raw tier (10 s). Every sequence stays ordered and non-overlapping and
    # tier/coverage agree, so only the duration predicate can reject it.
    rejected(
        replace(
            value,
            points=(
                replace(value.points[0], bucket_secs=60),
                replace(value.points[1], bucket_start_mono_ns=60_000_000_000, bucket_secs=60),
            ),
            device_points=(
                replace(value.device_points[0], bucket_secs=60),
                replace(value.device_points[1], bucket_secs=60),
                replace(
                    value.device_points[2],
                    bucket_start_mono_ns=60_000_000_000,
                    bucket_secs=60,
                ),
                replace(
                    value.device_points[3],
                    bucket_start_mono_ns=60_000_000_000,
                    bucket_secs=60,
                ),
            ),
            residual_points=(
                replace(value.residual_points[0], bucket_secs=60),
                replace(
                    value.residual_points[1],
                    bucket_start_mono_ns=60_000_000_000,
                    bucket_secs=60,
                ),
            ),
        )
    )
    # Non-finite wall time.
    rejected(
        replace(
            value,
            points=(replace(value.points[0], wall_start_seconds=float("nan")), value.points[1]),
        )
    )
    # A nonempty series declared totals_only.
    rejected(replace(value, tier="totals_only", coverage=coverage(tier="totals_only")))
    # Partial/paged series are refused, both on the request and the envelope.
    with pytest.raises(ValueError):
        validate_query_result(
            QueryRequest("series", window, app_key=KEY, cursor="opaque"), response
        )
    with pytest.raises(ValueError):
        validate_query_result(request, {**response, "next_cursor": "opaque"})
    # Binding arms: wrong external key echo and wrong exact Window.
    with pytest.raises(ValueError):
        validate_query_result(request, {**response, "app_key": OTHER_KEY})
    with pytest.raises(ValueError):
        validate_query_result(
            request,
            {**response, "result": _raw_series(7, Window("range", BOOT, 0, 30_000_000_000))},
        )

    # Totals share the same snapshot/echo/no-continuation binding.
    totals_request = QueryRequest("totals", window, app_key=KEY)
    totals_value = share(7, window)
    totals_response = _response("totals", totals_value, app_key=KEY)
    validate_query_result(totals_request, totals_response)
    _validate_local_totals(totals_request, KEY, totals_value)
    with pytest.raises(ValueError):
        validate_query_result(totals_request, {**totals_response, "next_cursor": "opaque"})
    with pytest.raises(ValueError):
        validate_query_result(totals_request, {**totals_response, "app_key": OTHER_KEY})
    with pytest.raises(ValueError):
        validate_query_result(totals_request, _response("totals", totals_value))
    with pytest.raises(ValueError):
        _validate_local_totals(totals_request, OTHER_KEY, totals_value)
