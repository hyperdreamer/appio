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
from appio.iodclient import IodClientError
from appio.ipc_models import QueryRequest
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

    # The cursor sequence is part of the key: the next committed sequence is a
    # miss and issues a new source, never the previous answer's source.
    transport.stream(4)
    fresh = transport.query(request)
    fresh_state = transport.client.query_transport(fresh)
    assert fresh_state.phase == "active"
    assert fresh_state.issued_source == RemotePosition(BOOT, 7, 4)
    assert len(transport.query_kinds) == envelopes + 1

    # A structurally older cache payload planted under the newer cursor key
    # cannot certify the newer requested source: it is evicted and the read
    # misses instead of resurrecting the old provenance.
    transport.inject_stale_cached_answer(
        request, share(93, window), answered_source=RemotePosition(BOOT, 7, 3)
    )
    stale = transport.query(request)
    stale_state = transport.client.query_transport(stale)
    assert stale_state.phase == "active"
    assert stale_state.issued_at_ns is not None
    assert stale_state.issued_source == RemotePosition(BOOT, 7, 4)
    assert len(transport.query_kinds) == envelopes + 2
    transport.reply(stale, share(94, window), snapshot=SNAPSHOT)
    accepted = transport.client.query_transport(stale)
    assert accepted.phase == "terminal"
    assert accepted.answered_source == RemotePosition(BOOT, 7, 4)

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
