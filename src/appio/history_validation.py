"""Shared pure request and selector validation for history answers.

This module is the single acceptance boundary that client cache admission (D05)
and the router settlement writer both run before an answer is trusted. It
performs no I/O, publishes nothing and inspects no owner state; it imports only
the standard library, :mod:`appio.contracts` and
:mod:`appio.ipc_models.QueryRequest` (§1.2).

Two entry points matter:

* :func:`validate_query_result` — the shared remote-envelope validator.
* :func:`_normalize_request` — the pure local worker-admission normalizer.

The private selector-value helpers accept the actual local value/key/request
directly, so a local caller never manufactures a remote envelope or snapshot
while still sharing these chronology predicates.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass

from .contracts import (
    LAUNCH_WINDOW_MESSAGE,
    MAX_RANGE_NS,
    RANGE_TOO_LONG_MESSAGE,
    AppTotal,
    Coverage,
    DiskShare,
    FileCoverage,
    FileTable,
    Series,
    UnknownOwnerOverflow,
    Window,
    WindowUnavailableError,
    validate_window,
)
from .ipc_models import QueryRequest

__all__ = ["validate_query_result"]

_QUERY_KINDS: tuple[str, ...] = ("ranking", "series", "files", "coverage", "totals")
_COVERAGE_KINDS: tuple[str, ...] = ("process", "file", "unknown_owner")
_METRICS: tuple[str, ...] = ("total_bytes", "read_bytes", "write_bytes", "rchar", "wchar")
_FILE_SORTS: tuple[str, ...] = (
    "bytes_total",
    "bytes_r",
    "bytes_w",
    "reads",
    "writes",
    "basename",
    "dev",
    "last_seen",
)
_DIRECTIONS: tuple[str, ...] = ("asc", "desc")

#: Per-selector `limit` bounds, mirroring the protocol table. A selector that
#: has no `limit` accepts only the protocol default.
_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "ranking": (1, 500),
    "series": (1, 4_000),
    "files": (1, 500),
}
_DEFAULT_LIMIT = 500
_MAX_SEARCH_BYTES = 256
_MAX_CURSOR_BYTES = 512

#: Tier -> the fixed bucket duration every point of that tier must carry.
_TIER_BUCKET_SECS: dict[str, int] = {
    "raw": 10,
    "rollup_60s": 60,
    "rollup_3600s": 3_600,
}

_LOWER_HEX = frozenset("0123456789abcdef")

_APP_KEY_KINDS: tuple[str, ...] = ("unit", "desktop", "exe", "scope")
_APP_KEY_PATH_KINDS: tuple[str, ...] = ("exe", "scope")
_APP_KEY_SAFE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    b"abcdefghijklmnopqrstuvwxyz"
    b"0123456789/._:@+-"
)
_APP_KEY_HEX = frozenset(b"0123456789ABCDEF")

_MISSING = object()

_RESULT_KEYS = frozenset({"kind", "result", "snapshot", "next_cursor", "app_key"})
_REQUIRED_RESULT_KEYS = frozenset({"kind", "result", "snapshot", "next_cursor"})


# ---------------------------------------------------------------------------
# Primitive validation
# ---------------------------------------------------------------------------


def _enum_text(value: object, allowed: tuple[str, ...], field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _canonical_app_key(key: object) -> str:
    """The ratified A07 AppKey grammar, duplicated here without ``appio.identity``.

    Canonicality is exact: uppercase ``%HH`` only, never an escape for a byte
    that could pass through, never a raw byte outside the safe set, and a valid
    UTF-8 payload whose shape matches its kind.
    """

    if not isinstance(key, str) or key == "":
        raise ValueError("an AppKey must be a non-empty string")
    kind, separator, payload = key.partition(":")
    if separator == "":
        raise ValueError(f"an AppKey requires a kind prefix: {key!r}")
    if kind not in _APP_KEY_KINDS:
        raise ValueError(f"unknown AppKey kind {kind!r}")
    if payload == "":
        raise ValueError("an AppKey payload must be non-empty")

    raw = payload.encode("utf-8")
    decoded = bytearray()
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == 0x25:  # '%'
            if index + 2 >= len(raw):
                raise ValueError(f"truncated percent escape in {key!r}")
            high, low = raw[index + 1], raw[index + 2]
            if high not in _APP_KEY_HEX or low not in _APP_KEY_HEX:
                raise ValueError(f"percent escape must use uppercase %HH in {key!r}")
            byte = int(bytes((high, low)), 16)
            if byte in _APP_KEY_SAFE:
                raise ValueError(f"safe byte unnecessarily escaped in {key!r}")
            decoded.append(byte)
            index += 3
            continue
        if value not in _APP_KEY_SAFE:
            raise ValueError(f"unsafe byte {value:#04x} must be percent-encoded in {key!r}")
        decoded.append(value)
        index += 1

    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"percent escapes do not decode as UTF-8 in {key!r}") from error

    if kind in _APP_KEY_PATH_KINDS:
        if not text.startswith("/"):
            raise ValueError(f"a {kind} payload must decode to an absolute path: {key!r}")
    elif "/" in text:
        raise ValueError(f"a {kind} payload must not decode to a path: {key!r}")
    return key


def _revalidate_tree(value: object) -> None:
    """Re-run every nested frozen record's own validation.

    A forged object with rewritten fields is not proof of validity, so the walk
    re-enters ``__post_init__`` at each dataclass node instead of trusting the
    top-level ``isinstance`` check. Non-finite floats are never valid values.
    """

    if isinstance(value, tuple):
        for item in value:
            _revalidate_tree(item)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("history values must be finite numbers")
        return
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, (list, dict, set)):
        raise ValueError("history records carry tuples, not mutable collections")
    if isinstance(value, type) or not is_dataclass(value):
        raise ValueError(f"unsupported history value {type(value).__name__}")
    type(value).__post_init__(value)  # type: ignore[attr-defined]
    for field in fields(value):  # type: ignore[arg-type]
        _revalidate_tree(getattr(value, field.name))


def _validate_snapshot(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise ValueError("a remote snapshot is exactly 64 lowercase hexadecimal characters")


def _validate_continuation(value: object, *, allow: bool) -> None:
    if value is None:
        return
    if not allow:
        raise ValueError("this selector answers a single page")
    if not isinstance(value, str) or value == "":
        raise ValueError("a continuation must be a nonempty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("a continuation must be valid UTF-8 text") from error
    if size > _MAX_CURSOR_BYTES:
        raise ValueError("a continuation is too long")


def _check_echo(echo: object, app_key: str | None) -> None:
    if echo is _MISSING or echo != app_key:
        raise ValueError("the external subject echo must equal the request subject")


# ---------------------------------------------------------------------------
# Request normalization (local worker admission)
# ---------------------------------------------------------------------------


def _normalize_request(request: QueryRequest) -> QueryRequest:
    """Validate one selector and its defaults without any I/O or client import.

    A22 refuses a launch window before subject or selector resolution, exactly as
    the wire path does. Fields that do not select anything for the chosen
    ``kind`` are rejections rather than silently discarded options.
    """

    if not isinstance(request, QueryRequest):
        raise ValueError("a QueryRequest is required")
    kind = _enum_text(request.kind, _QUERY_KINDS, "query.kind")

    window = request.window
    if not isinstance(window, Window):
        raise ValueError("query.window must be a Window")
    if window.kind == "launch":
        raise WindowUnavailableError("launch_attribution", LAUNCH_WINDOW_MESSAGE)
    validate_window(window)
    if window.kind == "range" and window.end_mono_ns - window.start_mono_ns > MAX_RANGE_NS:
        # A22 precedence: the range refusal precedes subject/selector resolution
        # and the exact MAX_RANGE_NS boundary stays valid, as the wire path has it.
        raise WindowUnavailableError("unsupported_window", RANGE_TOO_LONG_MESSAGE)

    app_key = request.app_key
    if app_key is not None:
        _canonical_app_key(app_key)
    if kind in ("series", "totals", "files") and app_key is None:
        raise ValueError(f"a {kind} query requires an external subject")
    if kind == "ranking" and app_key is not None:
        raise ValueError("a ranking query selects no subject")

    coverage_kind = _enum_text(
        request.coverage_kind, _COVERAGE_KINDS, "query.coverage_kind"
    )
    if kind != "coverage" and coverage_kind != "process":
        raise ValueError("coverage_kind selects only the coverage query")
    if kind == "coverage":
        if coverage_kind == "file" and app_key is None:
            raise ValueError("file coverage requires an external subject")
        if coverage_kind == "unknown_owner" and app_key is not None:
            raise ValueError("unknown-owner overflow selects no application subject")

    metric = _enum_text(request.metric, _METRICS, "query.metric")
    if kind == "ranking":
        sort = request.sort
        if not isinstance(sort, str) or sort not in _METRICS + ("bytes_total",):
            raise ValueError("query.sort must be a metric for a ranking")
    else:
        if metric != "total_bytes":
            raise ValueError("metric selects only a ranking")
        _enum_text(
            request.sort,
            _FILE_SORTS if kind == "files" else ("bytes_total",),
            "query.sort",
        )

    direction = _enum_text(request.direction, _DIRECTIONS, "query.direction")
    if kind not in ("ranking", "files") and direction != "desc":
        raise ValueError("direction selects only ranking and files")

    search = request.search
    if search is not None:
        if not isinstance(search, str) or "\x00" in search:
            raise ValueError("query.search must be NUL-free text or None")
        try:
            search_bytes = len(search.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("query.search must be valid UTF-8 text") from error
        if search_bytes > _MAX_SEARCH_BYTES:
            raise ValueError("query.search is too long")
        if kind != "files":
            raise ValueError("search selects only files")

    cursor = request.cursor
    if cursor is not None:
        if not isinstance(cursor, str) or cursor == "":
            raise ValueError("query.cursor must be nonempty text or None")
        if "\x00" in cursor:
            raise ValueError("query.cursor must not contain NUL")
        try:
            cursor_bytes = len(cursor.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("query.cursor must be valid UTF-8 text") from error
        if cursor_bytes > _MAX_CURSOR_BYTES:
            raise ValueError("query.cursor is too long")
        if kind not in ("ranking", "files"):
            raise ValueError("this selector answers a single page")

    limit = request.limit
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("query.limit must be an integer")
    bounds = _LIMIT_BOUNDS.get(kind)
    if bounds is None:
        if limit != _DEFAULT_LIMIT:
            raise ValueError("limit selects only ranking, series and files")
    elif not bounds[0] <= limit <= bounds[1]:
        raise ValueError("query.limit is outside the selector's page bounds")

    return request


# ---------------------------------------------------------------------------
# Selector-value predicates
# ---------------------------------------------------------------------------


def _validate_ranking(value: object, *, allow_tuple: bool = False) -> None:
    if allow_tuple:
        if not isinstance(value, (list, tuple)):
            raise ValueError("a ranking result is a sequence of AppTotal rows")
    elif not isinstance(value, list):
        # The public remote selector answers a JSON array; the tuple shape
        # belongs only to an internal local worker outcome.
        raise ValueError("a remote ranking result is a list of AppTotal rows")
    seen: set[str] = set()
    for row in value:  # type: ignore[union-attr]
        if not isinstance(row, AppTotal):
            raise ValueError("a ranking result contains only AppTotal rows")
        _revalidate_tree(row)
        key = _canonical_app_key(row.app_key)
        if key in seen:
            raise ValueError("a ranking result carries duplicate external identities")
        seen.add(key)


def _validate_totals(value: object, request: QueryRequest) -> None:
    if not isinstance(value, DiskShare):
        raise ValueError("a totals result is exactly a DiskShare")
    _revalidate_tree(value)
    if value.window != request.window:
        raise ValueError("a totals value must carry the exact requested window")


def _series_sequence(points: tuple[object, ...]) -> None:
    last_start: int | None = None
    last_end: int | None = None
    for point in points:
        start = point.bucket_start_mono_ns  # type: ignore[attr-defined]
        if last_start is not None:
            if start <= last_start:
                raise ValueError("series starts must strictly increase")
            if last_end is not None and start < last_end:
                raise ValueError("series buckets must not overlap")
        last_start = start
        last_end = start + point.bucket_secs * 10**9  # type: ignore[attr-defined]


def _device_sequence(points: tuple[object, ...]) -> None:
    """Global ``(start, dev bytes)`` order plus a per-device nonoverlap map."""

    last_order: tuple[int, bytes] | None = None
    last_end_by_dev: dict[str, int] = {}
    for point in points:
        start = point.bucket_start_mono_ns  # type: ignore[attr-defined]
        order = (start, point.dev.encode("utf-8"))  # type: ignore[attr-defined]
        if last_order is not None and order <= last_order:
            raise ValueError("device points must strictly increase by (start, device bytes)")
        if start < last_end_by_dev.get(point.dev, 0):  # type: ignore[attr-defined]
            raise ValueError("one device's buckets must not overlap")
        last_order = order
        last_end_by_dev[point.dev] = start + point.bucket_secs * 10**9  # type: ignore[attr-defined]


def _validate_series(value: object, request: QueryRequest, app_key: str) -> None:
    if not isinstance(value, Series):
        raise ValueError("a series result is exactly a Series")
    _revalidate_tree(value)
    _canonical_app_key(app_key)
    if value.window != request.window:
        raise ValueError("a series value must carry the exact requested window")

    tier = value.tier
    if tier == "totals_only":
        if value.points or value.device_points or value.residual_points:
            raise ValueError("a totals_only series carries no point sequences")
    else:
        secs = _TIER_BUCKET_SECS[tier]
        for sequence in (value.points, value.device_points, value.residual_points):
            for point in sequence:
                if point.bucket_secs != secs:
                    raise ValueError("every point duration must match the series tier")
    if value.coverage.tier != tier:
        raise ValueError("the series tier must equal its coverage tier")
    for point in value.points:
        wall = point.wall_start_seconds
        if wall is not None and not math.isfinite(wall):
            raise ValueError("a series wall time must be finite")

    _series_sequence(value.points)
    _series_sequence(value.residual_points)
    _device_sequence(value.device_points)


def _validate_served_bounds(served: Window | None, request: QueryRequest) -> None:
    """A served interval is either the requested Window or a bounded range in it.

    §5.4 preserves the successful totals-only since-boot/lifetime forms: those
    retain the exact requested Window and invent no endpoints. A bounded served
    range must instead carry the request's boot and lie inside its bounds.
    """

    if served is None:
        raise ValueError("file context must define its served interval")
    if not isinstance(served, Window):
        raise ValueError("a served interval is a Window")
    validate_window(served)
    window = request.window
    if window.kind == "lifetime":
        # The lifetime compatibility carrier is that same lifetime Window; an
        # arbitrary range (on any boot) or a launch Window is another scope.
        if served != window:
            raise ValueError("a lifetime file context retains the requested lifetime window")
        return
    if served == window:
        return
    if served.kind != "range":
        raise ValueError("a bounded served interval is a range window")
    if served.boot_id != window.boot_id:
        raise ValueError("a served interval must have the request's boot")
    if window.kind == "range":
        if served.start_mono_ns < window.start_mono_ns:
            raise ValueError("a served interval must lie inside the requested range")
    if served.end_mono_ns > window.end_mono_ns:
        raise ValueError("a served interval must lie inside the requested bounds")


def _validate_files(value: object, request: QueryRequest) -> None:
    if not isinstance(value, FileTable):
        raise ValueError("a files result is exactly a FileTable")
    _revalidate_tree(value)
    if value.window != request.window:
        raise ValueError("a files page must carry the exact requested window")
    if value.sort != request.sort:
        raise ValueError("a files page must echo the requested sort")
    if value.direction != request.direction:
        raise ValueError("a files page must echo the requested direction")
    if value.search != request.search:
        raise ValueError("a files page must echo the normalized requested search")
    if len(value.rows) > request.limit:
        raise ValueError("a files page exceeds the requested page limit")
    _validate_served_bounds(value.unknown_owner_overflow.window, request)


def _validate_process_coverage(value: object, request: QueryRequest) -> None:
    if request.app_key is not None:
        if not isinstance(value, Coverage):
            raise ValueError("a scoped process coverage is exactly a Coverage")
        _revalidate_tree(value)
        return
    if not isinstance(value, Mapping):
        raise ValueError("an unscoped process coverage is an AppKey -> Coverage mapping")
    for key, item in value.items():
        _canonical_app_key(key)
        if not isinstance(item, Coverage):
            raise ValueError("coverage values must be Coverage records")
        _revalidate_tree(item)


def _validate_coverage(value: object, request: QueryRequest, echo: object) -> None:
    coverage_kind = request.coverage_kind
    if coverage_kind == "file":
        if not isinstance(value, FileCoverage):
            raise ValueError("file coverage is exactly a FileCoverage")
        _revalidate_tree(value)
        if value.window != request.window:
            raise ValueError("file coverage must carry the exact requested window")
        _check_echo(echo, request.app_key)
        _validate_served_bounds(value.served_interval, request)
        return
    if echo is not _MISSING:
        raise ValueError("this coverage form carries no external subject echo")
    if coverage_kind == "unknown_owner":
        if not isinstance(value, UnknownOwnerOverflow):
            raise ValueError("unknown-owner overflow is exactly an UnknownOwnerOverflow")
        _revalidate_tree(value)
        _validate_served_bounds(value.window, request)
        return
    _validate_process_coverage(value, request)


# ---------------------------------------------------------------------------
# Public and local entry points
# ---------------------------------------------------------------------------


def validate_query_result(
    request: QueryRequest, response: Mapping[str, object]
) -> None:
    """Raise ``ValueError`` for any shape, nested or selector failure.

    The caller maps this failure to its existing rejection
    (``invalid_result``); this function never publishes, caches or repairs.
    """

    normalized = _normalize_request(request)
    if not isinstance(response, Mapping):
        raise ValueError("a query result must be a mapping")
    keys = set(response)
    if not _REQUIRED_RESULT_KEYS <= keys:
        raise ValueError("a query result carries kind, result, snapshot and next_cursor")
    if keys - _RESULT_KEYS:
        raise ValueError("a query result carries an unknown field")

    kind = _enum_text(response["kind"], _QUERY_KINDS, "result.kind")
    if kind != normalized.kind:
        raise ValueError("the result kind must match the request kind")
    snapshot = response["snapshot"]
    next_cursor = response["next_cursor"]
    result = response["result"]
    echo = response.get("app_key", _MISSING)

    if normalized.kind == "coverage":
        if normalized.coverage_kind == "file":
            # File coverage joins the Files page sequence, whose owner needs a
            # valid snapshot before any cache insertion or owner settlement.
            _validate_snapshot(snapshot)
        elif snapshot is not None:
            _validate_snapshot(snapshot)
        _validate_continuation(next_cursor, allow=False)
        _validate_coverage(result, normalized, echo)
        return

    _validate_snapshot(snapshot)
    if normalized.kind == "ranking":
        if echo is not _MISSING:
            raise ValueError("a ranking result carries no external subject echo")
        _validate_continuation(next_cursor, allow=True)
        _validate_ranking(result)
        return
    if normalized.kind == "totals":
        _check_echo(echo, normalized.app_key)
        _validate_continuation(next_cursor, allow=False)
        _validate_totals(result, normalized)
        return
    if normalized.kind == "series":
        _check_echo(echo, normalized.app_key)
        _validate_continuation(next_cursor, allow=False)
        _validate_series(result, normalized, normalized.app_key)
        return
    _check_echo(echo, normalized.app_key)
    _validate_continuation(next_cursor, allow=True)
    _validate_files(result, normalized)


def _validate_local_ranking(request: QueryRequest, value: object) -> None:
    """Validate a local worker ranking row sequence, without a remote envelope.

    ``LocalFrameOutcome.ranking`` is a tuple, so the local form keeps that shape;
    the public remote selector accepts only its JSON array. Empty local ranking
    is structurally valid, and the remote ownership restrictions stay in the
    router.
    """

    normalized = _normalize_request(request)
    if normalized.kind != "ranking":
        raise ValueError("a local ranking value needs a ranking request")
    _validate_ranking(value, allow_tuple=True)


def _validate_local_series(request: QueryRequest, app_key: str, value: object) -> None:
    """Validate a local series value against its exact request, without a client."""

    normalized = _normalize_request(request)
    if normalized.kind != "series":
        raise ValueError("a local series value needs a series request")
    if normalized.app_key != app_key:
        raise ValueError("the local value must match the request subject")
    _validate_series(value, normalized, app_key)


def _validate_local_totals(request: QueryRequest, app_key: str, value: object) -> None:
    """Validate a local totals value against its exact request, without a client."""

    normalized = _normalize_request(request)
    if normalized.kind != "totals":
        raise ValueError("a local totals value needs a totals request")
    if normalized.app_key != app_key:
        raise ValueError("the local value must match the request subject")
    _canonical_app_key(app_key)
    _validate_totals(value, normalized)


def _validate_local_coverage(
    request: QueryRequest, app_key: str | None, value: object
) -> None:
    """Validate a local process-coverage value, without a remote envelope."""

    normalized = _normalize_request(request)
    if normalized.kind != "coverage":
        raise ValueError("a local coverage value needs a coverage request")
    if normalized.coverage_kind != "process":
        raise ValueError("local coverage is the process selector")
    if normalized.app_key != app_key:
        raise ValueError("the local value must match the request subject")
    if app_key is not None:
        _canonical_app_key(app_key)
    _validate_process_coverage(value, normalized)
