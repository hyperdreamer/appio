"""Shared pure selector validation: coverage unions and series chronology.

These are foundation checks for :func:`validate_query_result` and the private
local-value helpers; later tasks extend the same two tests with real client and
router routes.
"""

from __future__ import annotations

from concurrent.futures import Future
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
from appio.history_types import RemotePosition
from appio.history_validation import (
    _normalize_request,
    _validate_local_coverage,
    _validate_local_ranking,
    _validate_local_series,
    _validate_local_totals,
    validate_query_result,
)
from appio.history_router import FileViewResult, HistoryRouter
from appio.iodclient import AppRegistry, IodClientError, RequestRejected
from appio.ipc_models import QueryRequest
from appio.query import QueryService
from appio.store import AppIoStore
from live_overview_support import coverage, series, share, total
from live_overview_transport import QueryTransportHarness

KEY = "unit:alpha.service"
OTHER_KEY = "unit:beta.service"
SNAPSHOT = "a" * 64
BOOT = "boot-a"
RAW_SECS = 10
NS = 1_000_000_000


def _reason(future) -> str:
    assert future.done(), "the future must settle without blocking the caller"
    with pytest.raises(IodClientError) as caught:  # noqa: PT011 - reason is asserted below
        future.result(timeout=0)
    return caught.value.reason

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

    # A files page must echo the normalized requested search: the same table
    # shape is valid for a search-less request and rejected for a searched one.
    search_request = QueryRequest("files", file_window, app_key=KEY, search="needle")
    search_table = _response("files", _file_table(file_window), app_key=KEY)
    validate_query_result(at_limit_request, search_table)
    with pytest.raises(ValueError):
        validate_query_result(search_request, search_table)

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
    # A ranking result may not repeat an external identity, even when every row
    # is otherwise a valid AppTotal.
    with pytest.raises(ValueError):
        validate_query_result(
            ranking_request,
            _response("ranking", [total(7, KEY), total(8, KEY)]),
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
    # task sequence predicate can reject it. The strict-increase predicate owns
    # its own diagnostic even though the positive-duration overlap predicate
    # would also refuse an out-of-order start.
    rejected(replace(value, points=tuple(reversed(value.points))))
    with pytest.raises(ValueError) as reversed_order:
        validate_query_result(
            request, {**response, "result": replace(value, points=tuple(reversed(value.points)))}
        )
    assert "series starts must strictly increase" in str(reversed_order.value)
    with pytest.raises(ValueError) as reversed_order_local:
        _validate_local_series(request, KEY, replace(value, points=tuple(reversed(value.points))))
    assert "series starts must strictly increase" in str(reversed_order_local.value)
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
        # The totals value must carry the exact requested Window, not merely a
        # valid one: a differing end is a different answer.
        validate_query_result(
            totals_request,
            _response(
                "totals",
                share(7, Window("range", BOOT, 0, 30_000_000_000)),
                app_key=KEY,
            ),
        )
    with pytest.raises(ValueError):
        _validate_local_totals(totals_request, OTHER_KEY, totals_value)


# ---------------------------------------------------------------------------
# Transport association, cursor-complete caching and decoded subjects (Task 3)
# ---------------------------------------------------------------------------


def test_cache_and_refusal_futures_keep_immutable_request_association() -> None:
    """Every returned Future, including immediate outcomes, stays query-bound."""

    transport = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
    window = Window("range", BOOT, 0, 20 * NS)
    request = QueryRequest("series", window, app_key=KEY, limit=100)

    first = transport.query(request)
    issued = transport.client.query_transport(first)
    assert issued.phase == "active"
    assert issued.request == request
    assert issued.connection_generation == transport.client.generation
    assert issued.request_id is not None
    assert issued.issued_at_ns == transport.clock.ns
    assert issued.issued_source == RemotePosition(BOOT, 7, 3)
    assert issued.answered_source is None

    observed: list[object] = []

    def _capture(done: Future) -> None:
        observed.append(transport.client.query_transport(done))

    first.add_done_callback(_capture)
    transport.reply(first, series(91, window), snapshot=SNAPSHOT)
    assert [state.phase for state in observed] == ["terminal"]
    assert observed[0].request == request
    assert observed[0].request_id == issued.request_id
    assert observed[0].issued_at_ns == issued.issued_at_ns
    assert observed[0].answered_source == issued.issued_source
    assert first.result(timeout=0)["app_key"] == KEY

    # An immediate cache hit is bound before any callback runs, consumes no
    # wire slot and keeps the original answered source without a new issue.
    envelopes = len(transport.query_kinds)
    hit = transport.query(request)
    assert hit.done()
    hit_capture: list[object] = []
    hit.add_done_callback(lambda done: hit_capture.append(transport.client.query_transport(done)))
    hit_state = transport.client.query_transport(hit)
    assert hit_state.phase == "terminal"
    assert hit_state.request == request
    assert hit_state.request_id is None and hit_state.issued_at_ns is None
    assert hit_state.issued_source is None
    assert hit_state.answered_source == RemotePosition(BOOT, 7, 3)
    assert [state.phase for state in hit_capture] == ["terminal"]
    assert hit_capture[0].request == request
    assert len(transport.query_kinds) == envelopes
    assert transport.client.pending_requests == 0
    assert transport.observed_historical_occupants[-1] == 0

    # A selector-inapplicable request is a local refusal: it keeps its exact
    # immutable request and is never adopted under some other selector.
    malformed = QueryRequest("series", window, app_key=KEY, search="x")
    refused = transport.query(malformed)
    assert _reason(refused) == "invalid_selector"
    refused_state = transport.client.query_transport(refused)
    assert refused_state.phase == "not_sent"
    assert refused_state.request == malformed
    assert refused_state.request.kind == "series"
    assert refused_state.request_id is None and refused_state.issued_at_ns is None
    assert refused_state.issued_source is None and refused_state.answered_source is None

    # A22's disabled launch window is refused locally too, before any wire
    # request, and the association still names the submitted selector.
    launch = QueryRequest("ranking", Window("launch", BOOT, launch_id=7))
    refused_launch = transport.query(launch)
    assert _reason(refused_launch) == "launch_attribution"
    launch_state = transport.client.query_transport(refused_launch)
    assert launch_state.phase == "not_sent"
    assert launch_state.request == launch
    assert launch_state.request_id is None and launch_state.issued_at_ns is None
    assert len(transport.query_kinds) == envelopes

    # An unrelated Future was never bound by this client.
    unrelated = Future()
    with pytest.raises(ValueError):
        transport.client.query_transport(unrelated)
    with pytest.raises(ValueError):
        transport.client.retire_query_connection(unrelated)
    with pytest.raises(ValueError):
        transport.client.reject_query_result(unrelated)

    # A terminal (sent and consumed) Future may not retire the live connection:
    # only currently carrying active/expired work owns that decision.
    assert transport.client.retire_query_connection(first) is False
    assert transport.client.query_transport(first).phase == "terminal"
    assert transport.client.state.verified is True

    # The envelope must answer the request the binding still names. Corrupting
    # exactly one association field makes a current contradiction, which is a
    # terminal invalid_result with no cache insertion.
    def corrupt_and_deliver(mutate) -> None:
        window = Window("range", BOOT, 0, 22 * NS)
        future = transport.query(QueryRequest("totals", window, app_key=KEY))
        state = transport.client.query_transport(future)
        assert state.request_id is not None
        pending = transport.client._pending[state.request_id]
        mutate(pending)
        entries = transport.client.cached_responses
        transport.reply_for(
            state.request_id, state.request, share(96, window), snapshot=SNAPSHOT
        )
        assert _reason(future) == "invalid_result"
        assert transport.client.query_transport(future).phase == "terminal"
        assert transport.client.cached_responses == entries

    corrupt_and_deliver(lambda pending: setattr(pending.binding, "request_id", "q-other"))
    corrupt_and_deliver(lambda pending: setattr(pending.binding, "generation", 99))
    corrupt_and_deliver(
        lambda pending: setattr(
            pending.binding, "issued_source", RemotePosition(BOOT, 7, 99)
        )
    )
    # The Future must still be associated with this exact binding object; a lost
    # observation association is itself a current contradiction.
    unbound_window = Window("range", BOOT, 0, 25 * NS)
    unbound = transport.query(QueryRequest("totals", unbound_window, app_key=KEY))
    unbound_state = transport.client.query_transport(unbound)
    assert unbound_state.request_id is not None
    unbound_pending = transport.client._pending[unbound_state.request_id]
    entries = transport.client.cached_responses
    del transport.client._transports[unbound_pending.future]
    transport.reply_for(
        unbound_state.request_id,
        unbound_state.request,
        share(99, unbound_window),
        snapshot=SNAPSHOT,
    )
    assert _reason(unbound) == "invalid_result"
    assert transport.client.cached_responses == entries
    # The canonical wire request is part of the issue evidence too: a selector
    # field that the pure validator does not inspect for this kind still breaks
    # the association and must not enter the cache.
    canonical_window = Window("range", BOOT, 0, 23 * NS)
    canonical = transport.query(QueryRequest("series", canonical_window, app_key=KEY))
    canonical_state = transport.client.query_transport(canonical)
    assert canonical_state.request_id is not None
    canonical_pending = transport.client._pending[canonical_state.request_id]
    canonical_pending.binding.request = replace(canonical_state.request, limit=100)
    entries = transport.client.cached_responses
    transport.reply_for(
        canonical_state.request_id,
        canonical_state.request,
        series(97, canonical_window),
        snapshot=SNAPSHOT,
    )
    assert _reason(canonical) == "invalid_result"
    assert transport.client.cached_responses == entries
    # Valid control: the identical wire shape is accepted when the association
    # is intact, so these negatives pin correlation rather than the payload.
    control_window = Window("range", BOOT, 0, 24 * NS)
    control = transport.query(QueryRequest("totals", control_window, app_key=KEY))
    assert transport.client.query_transport(control).phase == "active"
    entries = transport.client.cached_responses
    transport.reply(control, share(98, control_window), snapshot=SNAPSHOT)
    assert control.result(timeout=0)["app_key"] == KEY
    assert transport.client.cached_responses == entries + 1

    # A daemon that never became available or a deliberately closed client still
    # binds its local refusal to the exact immutable request.
    offline = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
    offline.disconnect()
    unavailable = offline.query(request)
    assert _reason(unavailable) == "daemon_unavailable"
    unavailable_state = offline.client.query_transport(unavailable)
    assert unavailable_state.phase == "not_sent"
    assert unavailable_state.request == request
    assert unavailable_state.request_id is None and unavailable_state.issued_at_ns is None
    transport.client.close()
    closed = transport.query(request)
    assert _reason(closed) == "client_closed"
    closed_state = transport.client.query_transport(closed)
    assert closed_state.phase == "not_sent"
    assert closed_state.request == request
    assert closed_state.request_id is None and closed_state.issued_at_ns is None

    # The observation record enforces its own UTF-8 text contract, with a
    # positive id and a never-sent phase control.
    with pytest.raises(ValueError):
        replace(issued, request_id="\ud800")
    assert replace(issued, request_id="q-ünïcode").request_id == "q-ünïcode"
    assert replace(
        issued,
        phase="not_sent",
        request_id=None,
        issued_at_ns=None,
        issued_source=None,
    ).phase == "not_sent"


def test_full_cursor_cache_key_and_original_answered_source() -> None:
    """The cache key carries the whole cursor; a hit never restamps its source."""

    transport = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
    window = Window("range", BOOT, 0, 20 * NS)
    request = QueryRequest("totals", window, app_key=KEY)

    first = transport.query(request)
    issued = transport.client.query_transport(first)
    assert issued.phase == "active"
    assert issued.issued_source == RemotePosition(BOOT, 7, 3)
    transport.reply(first, share(91, window), snapshot=SNAPSHOT)
    assert transport.client.query_transport(first).phase == "terminal"
    settled = transport.client.query_transport(first)
    assert settled.request == issued.request
    assert settled.request_id == issued.request_id
    assert settled.issued_at_ns == issued.issued_at_ns
    assert settled.answered_source == RemotePosition(BOOT, 7, 3)

    envelopes = len(transport.query_kinds)
    hit = transport.query(request)
    hit_state = transport.client.query_transport(hit)
    assert hit_state.phase == "terminal"
    assert hit_state.request_id is None and hit_state.issued_at_ns is None
    assert hit_state.answered_source == RemotePosition(BOOT, 7, 3)
    assert len(transport.query_kinds) == envelopes, "an unchanged-cursor hit costs no round trip"
    assert hit.result(timeout=0)["app_key"] == KEY

    # reject_query_result evicts exactly the entry version this Future produced
    # and never a later replacement under the same key.
    assert transport.client.cached_responses == 1
    transport.client.reject_query_result(first)
    assert transport.client.cached_responses == 0
    assert len(transport.query_kinds) == envelopes
    retried = transport.query(request)
    retried_state = transport.client.query_transport(retried)
    assert retried_state.phase == "active"
    assert retried_state.issued_source == RemotePosition(BOOT, 7, 3)
    assert len(transport.query_kinds) == envelopes + 1, "a rejection forces a real read"
    transport.reply(retried, share(92, window), snapshot=SNAPSHOT)
    assert transport.client.cached_responses == 1
    transport.client.reject_query_result(first)
    assert transport.client.cached_responses == 1, "an old version never evicts its successor"

    # A corrupted cached value is revalidated on the hit, evicted, and the read
    # becomes a real miss instead of serving unvalidated provenance.
    from appio.iodclient import _cache_key, _query_wire

    current_key = _cache_key(_query_wire(request), RemotePosition(BOOT, 7, 3))
    transport.client._cache[current_key].value["result"] = share(
        93, Window("range", BOOT, 0, 21 * NS)
    )
    envelopes = len(transport.query_kinds)
    corrupted = transport.query(request)
    corrupted_state = transport.client.query_transport(corrupted)
    assert corrupted_state.phase == "active"
    assert len(transport.query_kinds) == envelopes + 1
    transport.reply(corrupted, share(93, window), snapshot=SNAPSHOT)
    assert transport.client.cached_responses == 1

    # A cached entry whose stored request no longer names this query is evicted
    # even when its value/echo would validate for that other subject.
    transport.client._cache[current_key].request = replace(request, app_key=OTHER_KEY)
    transport.client._cache[current_key].value["app_key"] = OTHER_KEY
    envelopes = len(transport.query_kinds)
    misfiled = transport.query(request)
    misfiled_state = transport.client.query_transport(misfiled)
    assert misfiled_state.phase == "active"
    assert len(transport.query_kinds) == envelopes + 1
    transport.reply(misfiled, share(90, window), snapshot=SNAPSHOT)
    assert transport.client.cached_responses == 1

    # The cursor sequence is part of the key: the next committed sequence is a
    # miss and issues a new source, never the previous answer's source.
    transport.stream(4)
    fresh = transport.query(request)
    fresh_state = transport.client.query_transport(fresh)
    assert fresh_state.phase == "active"
    assert fresh_state.issued_source == RemotePosition(BOOT, 7, 4)
    assert len(transport.query_kinds) == envelopes + 2

    # The reply cursor may advance while the request is on the wire; the
    # answered source stays the source captured once at issue.
    transport.stream(5)
    transport.reply(fresh, share(94, window), snapshot=SNAPSHOT)
    answered = transport.client.query_transport(fresh)
    assert answered.phase == "terminal"
    assert answered.answered_source == RemotePosition(BOOT, 7, 4)

    # A structurally older cache payload planted under the newer cursor key
    # cannot certify the newer requested source: it is evicted and the read
    # misses instead of resurrecting the old provenance.
    transport.inject_stale_cached_answer(
        request, share(95, window), answered_source=RemotePosition(BOOT, 7, 4)
    )
    envelopes = len(transport.query_kinds)
    stale = transport.query(request)
    stale_state = transport.client.query_transport(stale)
    assert stale_state.phase == "active"
    assert stale_state.issued_at_ns is not None
    assert stale_state.issued_source == RemotePosition(BOOT, 7, 5)
    assert len(transport.query_kinds) == envelopes + 1
    transport.reply(stale, share(96, window), snapshot=SNAPSHOT)
    accepted = transport.client.query_transport(stale)
    assert accepted.phase == "terminal"
    assert accepted.answered_source == RemotePosition(BOOT, 7, 5)

    # Preserved controls: whole-entry byte bound and failure exclusion.
    assert transport.client.cached_bytes <= transport.client.cache_bytes
    before = transport.client.cached_responses
    other_window = Window("range", BOOT, 0, 21 * NS)
    failing = transport.query(QueryRequest("totals", other_window, app_key=KEY))
    transport.error(failing, "history_unavailable", "the daemon is busy")
    assert _reason(failing) == "history_unavailable"
    assert transport.client.cached_responses == before, "a failure is never cached"
    sent = len(transport.query_kinds)
    retry = transport.query(QueryRequest("totals", other_window, app_key=KEY))
    assert transport.client.query_transport(retry).phase == "active"
    assert len(transport.query_kinds) == sent + 1, "a failed read sends a real retry"

    # Request-specific retirement detaches that connection, marks its work lost
    # and clears that connection's cache authority.
    assert transport.client.cached_responses == before
    assert transport.observed_historical_occupants[-1] == 1
    assert transport.client.retire_query_connection(retry) is True
    assert transport.client.query_transport(retry).phase == "lost"
    assert transport.client._sock is None
    assert transport.client.cached_responses == 0, "retirement clears that connection's cache"
    # The peer's EOF is pending on the detached connection when the replacement
    # is opened: the replacement observation retires those jobs rather than
    # inheriting them as occupied.
    transport.connect()
    assert transport.client.state.verified is True
    assert transport.observed_historical_occupants[-1] == 0, (
        "a replacement connection inherits no retired jobs"
    )
    successor = transport.query(QueryRequest("totals", other_window, app_key=KEY))
    assert transport.client.query_transport(successor).phase == "active"
    assert transport.observed_historical_occupants[-1] == 1
    # A real EOF on the current connection retires its held job as well.
    assert transport.client.retire_query_connection(successor) is True
    transport.pump()
    assert transport.observed_historical_occupants[-1] == 0

    # Expiry releases the owner's Future but not the peer job while the socket
    # remains live; only a real detach retires it.
    held = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
    held_future = held.query(QueryRequest("totals", window, app_key=KEY))
    held.stream(4)
    held.advance_ns(5 * NS)
    held.pump()
    assert held.client.query_transport(held_future).phase == "expired"
    assert held.observed_historical_occupants[-1] == 1

    # A fresh cursor-less connection has no position to prove, and a reverified
    # cursor-less replacement must not inherit the previous connection's
    # retained cursor as current issue evidence.
    bare = QueryTransportHarness(cursor=None)
    bare_future = bare.query(QueryRequest("totals", window, app_key=KEY))
    bare_state = bare.client.query_transport(bare_future)
    assert bare_state.phase == "active"
    assert bare_state.issued_source is None
    transport.connect(cursor=None)
    replaced = transport.query(QueryRequest("totals", window, app_key=KEY))
    replaced_state = transport.client.query_transport(replaced)
    assert replaced_state.phase == "active"
    assert replaced_state.issued_source is None
    # A successful cursor-less answer invents no source, and its cache entry
    # keeps that original null provenance rather than inheriting the old cursor.
    transport.reply(replaced, share(97, window), snapshot=SNAPSHOT)
    replaced_answer = transport.client.query_transport(replaced)
    assert replaced_answer.phase == "terminal"
    assert replaced_answer.answered_source is None
    hit_again = transport.query(QueryRequest("totals", window, app_key=KEY))
    hit_again_state = transport.client.query_transport(hit_again)
    assert hit_again_state.phase == "terminal"
    assert hit_again_state.issued_source is None
    assert hit_again_state.answered_source is None


def test_totals_and_series_decode_preserve_external_subject() -> None:
    """Decoded totals/series keep their external subject, not a wire/peer id."""

    transport = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
    window = Window("range", BOOT, 0, 20 * NS)
    peer_totals_id = 91
    peer_series_id = 92
    router_totals_id = 5
    router_series_id = 6

    totals_future = transport.query(QueryRequest("totals", window, app_key=KEY))
    transport.reply(totals_future, share(peer_totals_id, window), snapshot=SNAPSHOT)
    totals = totals_future.result(timeout=0)
    assert totals["kind"] == "totals"
    assert totals["app_key"] == KEY
    assert totals["result"].window == window
    assert totals["result"].app_id not in (peer_totals_id, router_totals_id)

    series_future = transport.query(QueryRequest("series", window, app_key=OTHER_KEY))
    transport.reply(series_future, series(peer_series_id, window), snapshot=SNAPSHOT)
    answer = series_future.result(timeout=0)
    assert answer["kind"] == "series"
    assert answer["app_key"] == OTHER_KEY
    assert answer["result"].window == window
    assert answer["result"].app_id not in (peer_series_id, router_series_id)
    assert answer["result"].app_id != totals["result"].app_id

    # A wrong wire subject for an otherwise valid value is rejected at the
    # external-key boundary, before any presentation remap could hide it. A
    # distinct window keeps these arms off the earlier accepted answer's cache.
    accepted_entries = transport.client.cached_responses
    wrong_window = Window("range", BOOT, 0, 21 * NS)
    transport.register_subject(peer_totals_id, OTHER_KEY)
    wrong_totals = transport.query(QueryRequest("totals", wrong_window, app_key=KEY))
    transport.reply(wrong_totals, share(peer_totals_id, wrong_window), snapshot=SNAPSHOT)
    assert _reason(wrong_totals) == "invalid_result"
    assert transport.client.query_transport(wrong_totals).phase == "terminal"
    assert transport.client.cached_responses == accepted_entries

    transport.register_subject(peer_series_id, KEY)
    wrong_series = transport.query(QueryRequest("series", wrong_window, app_key=OTHER_KEY))
    transport.reply(wrong_series, series(peer_series_id, wrong_window), snapshot=SNAPSHOT)
    assert _reason(wrong_series) == "invalid_result"
    assert transport.client.cached_responses == accepted_entries


# ---------------------------------------------------------------------------
# Router-driven priority scheduling and Files settlement (Tasks 6-7)
# ---------------------------------------------------------------------------


def _scheduler_router(tmp_path, *, name="scheduler.db"):
    """A production router over the real socketpair transport, Qt-free."""

    store = AppIoStore(tmp_path / name)
    store.connection.commit()
    transport = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
    # One peer snapshot proves the file capability set and a cursor; a status
    # reply alone carries no capabilities.
    transport.stream(4)
    router = HistoryRouter(QueryService(store), transport.client, AppRegistry(),
                           clock_ns=transport.clock)
    return router, transport, store


def _ready_remote_frame(router, transport, window):
    """Prepare one frame, settle local ownership then the remote ranking."""

    router.prepare(window)
    transport.pump()
    assert transport.query_kinds[-1] == "ranking"
    token, future = router._occupant
    assert token.owner == "frame"
    assert router._local_future.result(timeout=20) is not None
    transport.reply(future, [total(17, KEY)], snapshot=SNAPSHOT)
    assert router.ready(window) is True
    assert router._remote is not None
    return router.registry.id_for(KEY)


def _answer_next(router, transport, value, *, snapshot=SNAPSHOT, next_cursor=None):
    transport.pump()
    token, future = router._occupant
    transport.reply(future, value, snapshot=snapshot, next_cursor=next_cursor)
    router.drain()
    # Drain the peer side so the next dispatched envelope is observable too.
    transport.pump()
    return token, future


def _evict_query(transport, request):
    """Drop one exact request/cursor cache entry so an arm takes the wire."""

    from appio.iodclient import _cache_key, _query_wire

    key = _cache_key(_query_wire(request), transport.client._current_source())
    transport.client._cache.pop(key, None)


def _start_files(router, transport, presentation, window):
    """Admit one Files view that must begin with a real page-one envelope."""

    _evict_query(transport, QueryRequest(
        "files", window, KEY, sort="bytes_total", direction="desc", search=None))
    _evict_query(transport, QueryRequest(
        "coverage", window, KEY, coverage_kind="file"))
    return router.request_file_view(presentation, window, "bytes_total", "desc", None)


def _page(window, *, rows=1, app_id=7, overflow_window=None):
    context = window if overflow_window is None else overflow_window
    overflow = UnknownOwnerOverflow(context, "overflow_owner_unknown", 0, 0, 0, coverage())
    row = FileRow(0, "alpha.txt", "resolved", False, "8:0", 11, 1, 1, 1, 17, 5, 22, BOOT, 1)
    return FileTable(app_id, window, tuple(row for _ in range(rows)), "bytes_total",
                     "desc", None, overflow, coverage())


def test_all_history_queries_share_one_priority_stable_scheduler(tmp_path):
    """Every historical selector shares one occupant, priority and stable order.

    Ranking, the displayed subject's series then totals, a Files first page, its
    continuation and the final file coverage each produce exactly one real
    envelope at a time. Priority (0 ranking, 1 detail, 2 Files) and admission
    serial decide dispatch, a continuation is newly admitted rather than sent
    recursively, and no self-generated ``query_busy`` ever appears.
    """

    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    router, transport, store = _scheduler_router(tmp_path)
    try:
        # Ranking (priority 0) is the first and only wire occupant.
        router.prepare(window)
        transport.pump()
        assert transport.query_kinds == ["ranking"]
        ranking_token, ranking_future = router._occupant
        assert ranking_token.request.kind == "ranking"

        # While ranking is held, series and totals queue in admission order and
        # never become a second envelope.
        router.select_detail(KEY, window)
        presentation = router.registry.id_for(KEY)
        assert router.request_detail("series", presentation, window).state == "pending"
        assert router.request_detail("totals", presentation, window).state == "pending"
        assert len(router._queue) == 2
        assert transport.query_kinds == ["ranking"]

        # Completing ranking dispatches the earlier detail admission first.
        assert router._local_future.result(timeout=20) is not None
        transport.reply(ranking_future, [total(17, KEY)], snapshot=SNAPSHOT)
        assert router.ready(window) is True
        transport.pump()
        assert transport.query_kinds == ["ranking", "series"]

        # A Files page admitted while series is on the wire waits as priority 2.
        files_future = router.request_file_view(
            presentation, window, "bytes_total", "desc", None)
        assert transport.query_kinds == ["ranking", "series"]

        # Series terminal -> totals runs before the queued Files page.
        _answer_next(router, transport,
                     series(1, window), snapshot=SNAPSHOT)
        assert transport.query_kinds == ["ranking", "series", "totals"]

        # Totals terminal -> the Files first page is the next envelope.
        _answer_next(router, transport,
                     share(1, window), snapshot=SNAPSHOT)
        assert transport.query_kinds == ["ranking", "series", "totals", "files"]
        files_token, files_request = router._occupant[0], router._occupant[0].request
        assert files_request.cursor is None

        # A continuation is a newly admitted priority-2 slot, never a recursive
        # send inside the page callback.
        _answer_next(router, transport, _page(window), next_cursor="opaque-2")
        continuation = router._occupant[0].request
        assert continuation.kind == "files" and continuation.cursor == "opaque-2"
        assert transport.query_kinds == [
            "ranking", "series", "totals", "files", "files"]

        # The final file coverage is the last separately admitted slot.
        _answer_next(router, transport, _page(window), next_cursor=None)
        coverage_request = router._occupant[0].request
        assert coverage_request.kind == "coverage"
        assert coverage_request.coverage_kind == "file"
        assert transport.query_kinds == [
            "ranking", "series", "totals", "files", "files", "coverage"]

        _answer_next(router, transport, _file_coverage(window))
        assert files_future.done() is True
        view = files_future.result(timeout=0)
        assert view.served_gap_count is None
        assert [row.basename for row in view.table.rows] == ["alpha.txt", "alpha.txt"]
        assert transport.observed_historical_occupants and max(
            transport.observed_historical_occupants) == 1
        assert transport.client.pending_requests == 0
        assert transport.client.state.last_error is None

        # Every supported coverage selector shape is carried by the same client
        # with at most one real historical job; each answer validates or fails
        # on its own contract, never with a self-generated query_busy.
        # A distinct window keeps these off the Files chain's cache entries, so
        # each arm must produce a real envelope rather than a cache hit.
        arm_window = Window("since_boot", BOOT, end_mono_ns=150 * NS)
        coverage_arms = (
            (QueryRequest("coverage", arm_window, KEY, coverage_kind="process"),
             _file_coverage(arm_window).coverage),
            (QueryRequest("coverage", arm_window, None, coverage_kind="process"),
             {KEY: coverage()}),
            (QueryRequest("coverage", arm_window, KEY, coverage_kind="file"),
             _file_coverage(arm_window)),
            (QueryRequest("coverage", arm_window, None, coverage_kind="unknown_owner"),
             _file_coverage(arm_window).unknown_owner_overflow),
        )
        for request, value in coverage_arms:
            before = len(transport.query_kinds)
            future = transport.query(request)
            assert len(transport.query_kinds) == before + 1
            assert transport.observed_historical_occupants[-1] == 1
            transport.reply(future, value, snapshot=SNAPSHOT)
            assert transport.client.query_transport(future).phase == "terminal"
            assert transport.observed_historical_occupants[-1] == 0
            assert transport.client.state.last_error is None
    finally:
        router.close()
        store.connection.close()


def test_files_pages_and_coverage_use_single_settlement_acceptance(tmp_path):
    """Files pages and coverage settle once and refuse every malformed shape.

    A valid paged chain publishes only its final assembled table with remote
    ``served_gap_count = None``. Wrong key/Window/snapshot/cursor-generation,
    changed later-page metadata, bad coverage/served evidence and the 128-page
    and 50,000-row bounds each fail the one owning view without a partial table.
    """

    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    router, transport, store = _scheduler_router(tmp_path)
    try:
        presentation = _ready_remote_frame(router, transport, window)

        # -- valid paged chain ------------------------------------------------
        future = _start_files(router, transport, presentation, window)
        _answer_next(router, transport, _page(window, rows=2), next_cursor="opaque-c1")
        continuation = router._occupant[0].request
        assert continuation.cursor == "opaque-c1"
        assert continuation.sort == "bytes_total" and continuation.direction == "desc"
        assert continuation.search is None
        _answer_next(router, transport, _page(window, rows=1), next_cursor=None)
        _answer_next(router, transport, _file_coverage(window))
        assert future.done() is True
        view = future.result(timeout=0)
        assert isinstance(view, FileViewResult)
        assert view.table.app_id == view.coverage.app_id == presentation
        assert view.table.window == view.coverage.window == window
        assert len(view.table.rows) == 3
        assert view.served_gap_count is None
        assert router._file_owners == {}
        frame_state = (router._not_retained, router._remote_snapshot,
                       router.daemon_answer_error, list(router._ranking))

        def assert_failed(failure, label, *, reason=None):
            with pytest.raises(Exception) as caught:
                failure.result(timeout=0)
            actual = getattr(caught.value, "reason", None)
            if reason is not None:
                assert actual == reason, (label, type(caught.value).__name__,
                                          str(caught.value), actual)
            else:
                assert actual in ("invalid_result", "file_source_unavailable",
                                  "stale_cursor"), (label, actual)
            # No partial assembled table or frame mutation survives an owner
            # refusal: the outward view is the only failed surface.
            assert router._file_owners == {}, label
            assert (router._not_retained, router._remote_snapshot,
                    router.daemon_answer_error, list(router._ranking)) == frame_state, label

        # Wrong external key: the client refuses the malformed answer before it
        # can be cached or assembled.
        transport.register_subject(99, OTHER_KEY)
        future = _start_files(router, transport, presentation, window)
        _answer_next(router, transport, _page(window, app_id=99))
        assert_failed(future, "wrong key", reason="invalid_result")

        # Wrong requested Window is a malformed page, not a widened answer.
        future = _start_files(router, transport, presentation, window)
        other = Window("since_boot", BOOT, end_mono_ns=150 * NS)
        _answer_next(router, transport, _page(other))
        assert_failed(future, "wrong window", reason="invalid_result")

        # A different (valid-looking) snapshot cannot replace the owner's
        # ranking snapshot.
        future = _start_files(router, transport, presentation, window)
        _answer_next(router, transport, _page(window), snapshot="b" * 64)
        assert_failed(future, "snapshot", reason="file_source_unavailable")

        # A later page may not change its non-row metadata (served scope).
        future = _start_files(router, transport, presentation, window)
        _answer_next(router, transport, _page(window), next_cursor="opaque-c2")
        served = Window("range", BOOT, 0, window.end_mono_ns)
        _answer_next(router, transport, _page(window, overflow_window=served),
                     next_cursor=None)
        assert_failed(future, "page metadata", reason="file_source_unavailable")

        # A committed cursor advance makes the pending chain stale rather than
        # silently certifying the newer position with an older answer.
        future = _start_files(router, transport, presentation, window)
        _answer_next(router, transport, _page(window), next_cursor="opaque-c3")
        assert router._occupant[0].request.cursor == "opaque-c3"
        transport.stream(5)
        _answer_next(router, transport, _page(window), next_cursor=None)
        assert_failed(future, "cursor generation", reason="file_source_unavailable")

        # The rebuilt frame restores a valid Files owner for coverage arms.
        presentation = _ready_remote_frame(router, transport, window)
        coverage_arms = (
            ("wrong coverage window", _file_coverage(other)),
            ("wrong coverage variant", _file_coverage(window).coverage),
            ("unavailable served evidence", _file_coverage(window, unavailable=True)),
        )
        for label, coverage_value in coverage_arms:
            future = _start_files(router, transport, presentation, window)
            _answer_next(router, transport, _page(window), next_cursor=None)
            _answer_next(router, transport, coverage_value)
            assert_failed(future, label)

        # A coverage snapshot must equal the Files owner's ranking snapshot.
        future = _start_files(router, transport, presentation, window)
        _answer_next(router, transport, _page(window), next_cursor=None)
        _answer_next(router, transport, _file_coverage(window), snapshot="c" * 64)
        assert_failed(future, "coverage snapshot", reason="file_source_unavailable")

        # -- 128-page bound ---------------------------------------------------
        future = _start_files(router, transport, presentation, window)
        for index in range(128):
            _answer_next(router, transport, _page(window, rows=1),
                         next_cursor=f"opaque-limit-{index}")
        _answer_next(router, transport, _page(window, rows=1), next_cursor=None)
        with pytest.raises(RequestRejected) as too_many_pages:
            future.result(timeout=0)
        assert too_many_pages.value.reason == "query_too_large"
        assert router._file_owners == {}

        # -- 50,000-row bound -------------------------------------------------
        future = _start_files(router, transport, presentation, window)
        for index in range(100):
            _answer_next(router, transport, _page(window, rows=500),
                         next_cursor=f"opaque-rows-{index}")
        _answer_next(router, transport, _page(window, rows=500), next_cursor=None)
        with pytest.raises(RequestRejected) as too_many_rows:
            future.result(timeout=0)
        assert too_many_rows.value.reason == "query_too_large"
        assert router._file_owners == {}
        assert (router._not_retained, router._remote_snapshot,
                router.daemon_answer_error, list(router._ranking)) == frame_state
    finally:
        router.close()
        store.connection.close()


def test_frame_failure_or_local_fallback_certifies_no_remote_position(tmp_path):
    """No remote failure, fallback or replaced generation certifies a position.

    A remote ranking that completes before the held local ownership evidence
    stays a private candidate; the local mixed-store proof then rejects it and
    evicts its bound cache entry while the transport has already been reaped.
    Empty, paginated and failed remote rankings keep the locally proved rows and
    neither certify a cursor nor lend an older frame's certificate or PEAK.
    """

    import threading

    from test_live_overview_router import _committed_store as _process_store

    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    mixed_key = "exe:/mixed"
    local = _process_store(tmp_path / "mixed.db", ((mixed_key, 17, 20),))
    transport = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
    transport.stream(4)
    router = HistoryRouter(QueryService(local), transport.client, AppRegistry(),
                           clock_ns=transport.clock)
    try:
        release = threading.Event()
        entered = threading.Event()
        real_read = router._read_local

        def held_copy(paths, wanted):
            entered.set()
            assert release.wait(20), "the local ownership copy was never released"
            return real_read(paths, wanted)

        router._read_local = held_copy
        router.prepare(window)
        transport.pump()
        assert transport.query_kinds == ["ranking"]
        ranking_token, ranking_future = router._occupant
        assert ranking_token.owner == "frame"
        assert entered.wait(10)

        # Remote completion first: the ranking answer waits privately for the
        # held local ownership evidence; the wire slot is already reaped.
        transport.reply(ranking_future, [total(17, mixed_key)], snapshot=SNAPSHOT)
        assert router.ready(window) is False
        frame = router._frame
        assert frame.remote_candidate is not None
        assert frame.remote_applied is False
        assert router._remote is None and router._remote_answered is None
        assert router._occupant is None
        assert transport.observed_historical_occupants[-1] == 0

        # Local mixed-store evidence rejects the candidate; the rejection also
        # evicts the cache entry this exact Future produced.
        router._read_local = real_read
        release.set()
        assert router._local_future.result(timeout=20) is not None
        assert router.ready(window) is True
        assert router._remote is None and router._remote_answered is None
        assert router.daemon_answer_error is not None
        assert router.daemon_answer_error.kind == "internal"
        assert "mixed-store" in router.daemon_answer_error.reason
        assert transport.client.cached_responses == 0
        certificate = router.frame_provenance(window)
        assert certificate is not None
        assert certificate.requested_source == RemotePosition(BOOT, 7, 4)
        assert certificate.answered_source is None
        assert certificate.certified is False
        assert router.ranking_provenance(mixed_key, window).accepted_identity.kind == "ranking"

        # A new equal-Window/source generation inherits no old certificate/PEAK.
        old_generation = certificate.generation
        router.prepare(window)
        assert router.frame_provenance(window) is None
        assert router.ranking_provenance(mixed_key, window) is None
        with pytest.raises(RuntimeError):
            router.ranking(window, "total_bytes")
        transport.pump()
        token, future = router._occupant
        assert router._local_future.result(timeout=20) is not None
        transport.reply(future, [total(17, mixed_key)], snapshot=SNAPSHOT)
        assert router.ready(window) is True
        replaced = router.frame_provenance(window)
        assert replaced is not None and replaced.generation != old_generation
        assert replaced.certified is False

        # A failed remote ranking is typed and certifies no remote position.
        router.prepare(window)
        transport.pump()
        token, failed = router._occupant
        assert router._local_future.result(timeout=20) is not None
        transport.error(failed, "daemon_error", "collector exploded")
        assert router.ready(window) is True
        assert router._remote is None and router._remote_answered is None
        assert router.daemon_answer_error.kind == "remote_error"
        assert router.daemon_answer_error.reason == "daemon_error"
        failed_certificate = router.frame_provenance(window)
        assert failed_certificate.certified is False
        assert failed_certificate.answered_source is None
        assert router._ranking  # locally proved rows remain
    finally:
        release.set()
        router.close()
        local.connection.close()

    # Empty and paginated remote rankings stay refusals, never certificates.
    other = Window("since_boot", BOOT, end_mono_ns=160 * NS)
    for index, (value, cursor, reason) in enumerate((
        ([], None, "remote ownership evidence required"),
        ([total(17, KEY)], "more-pages", "paginated remote ownership is not proved atomically"),
    )):
        empty_store = AppIoStore(tmp_path / f"empty-{index}.db")
        empty_store.connection.commit()
        empty_transport = QueryTransportHarness(cursor=RemotePosition(BOOT, 7, 3))
        empty_transport.stream(4)
        empty_router = HistoryRouter(
            QueryService(empty_store), empty_transport.client, AppRegistry(),
            clock_ns=empty_transport.clock)
        try:
            empty_router.prepare(other)
            empty_transport.pump()
            token, future = empty_router._occupant
            assert empty_router._local_future.result(timeout=20) is not None
            empty_transport.reply(future, value, snapshot=SNAPSHOT,
                                  next_cursor=cursor)
            assert empty_router.ready(other) is True
            assert empty_router._remote is None
            assert empty_router.daemon_answer_error is not None
            assert empty_router.daemon_answer_error.reason == reason
            certificate = empty_router.frame_provenance(other)
            assert certificate.certified is False
            assert certificate.answered_source is None
            assert empty_transport.client.cached_responses == 0
        finally:
            empty_router.close()
            empty_store.connection.close()
