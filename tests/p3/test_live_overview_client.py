"""Shared pure selector validation: coverage unions and series chronology.

These are foundation checks for :func:`validate_query_result` and the private
local-value helpers; later tasks extend the same two tests with real client and
router routes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from appio.contracts import (
    DeviceSeriesPoint,
    FileCoverage,
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
    _validate_local_series,
    _validate_local_totals,
    validate_query_result,
)
from appio.ipc_models import QueryRequest
from live_overview_support import coverage, share

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
    # Reversed bucket order.
    rejected(
        replace(
            value,
            points=tuple(reversed(value.points)),
            device_points=tuple(reversed(value.device_points)),
            residual_points=tuple(reversed(value.residual_points)),
        )
    )
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
    # Tier duration disagreement, with otherwise well-ordered buckets.
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
                replace(value.device_points[2], bucket_secs=60),
                replace(value.device_points[3], bucket_secs=60),
            ),
            residual_points=(
                replace(value.residual_points[0], bucket_secs=60),
                replace(value.residual_points[1], bucket_start_mono_ns=60_000_000_000, bucket_secs=60),
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
