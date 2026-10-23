"""Typed, read-only protocol-1 messages for the AppIO local socket.

The socket has **no command surface**. A client may ask for

* ``status``    — enabled/answering state plus the committed high-water cursor,
* ``subscribe`` — the live snapshot stream, optionally resumed from a boot-scoped
  ``(boot_id, iod_epoch, seq)`` cursor,
* ``query``     — one of five historical selectors answered from the daemon's
  durable store,

and nothing else. ``enable``, ``disable``, ``exec``, ``handover`` and every other
verb are rejected before dispatch, because accepting one here would turn a
read-only socket into a privileged ingestion surface (design §5, D3).

Query selectors are a literal table, never SQL, paths, shell text, collector
controls or setup commands. A field that does not belong to the selected
``kind`` is a rejection (``invalid_selector``) rather than a silently ignored
option, so a client cannot probe for capabilities by watching which extras are
tolerated.

Two shapes deserve an explicit note:

* **Disabled launch windows.** A22 makes every launch window unqueryable, even
  when raw launch rows survive. The wire form therefore carries only
  ``kind``/``boot_id``; :func:`validate_request` constructs the already-disabled
  :class:`~appio.contracts.Window` with an internal placeholder launch id and
  raises the ratified :class:`~appio.contracts.WindowUnavailableError` *before*
  any identity or ID lookup. No database launch surrogate is ever transmitted or
  accepted.
* **Legacy cursors.** A subscription cursor needs boot identity, because
  ``(iod_epoch, seq)`` alone cannot distinguish two boots. A legacy cursor that
  omits ``boot_id`` is accepted as a *resync request*: :attr:`Request.since` is
  ``None`` and :attr:`Request.resync_reason` is ``"cursor_boot_unknown"``, so the
  server answers with an explicit gap instead of replaying another boot's stream.
  ``resync_reason`` is additive to the pinned four-field record; consumers that
  only read ``since`` are unaffected.

Server envelopes are validated before a consumer sees data. Row payloads are
owned by their producers; these validators pin the envelope identity — protocol,
type, correlation, boot/epoch/seq, capabilities, coverage presence and the
half-open gap bounds — so a consumer never has to guess whether an identity is
trustworthy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from .contracts import (
    LAUNCH_WINDOW_MESSAGE,
    Window,
    WindowUnavailableError,
    validate_search,
    validate_sqlite_integer,
    validate_window,
)
from .identity import validate_app_key
from .ipc_protocol import PROTOCOL_VERSION, ProtocolError

__all__ = [
    "Capabilities",
    "CoverageKind",
    "PROTOCOL_VERSION",
    "QueryKind",
    "QueryRequest",
    "Request",
    "StreamCursor",
    "validate_request",
    "validate_server_message",
]

QueryKind = Literal["ranking", "series", "files", "coverage", "totals"]
CoverageKind = Literal["process", "file", "unknown_owner"]

_REQUEST_TYPES: tuple[str, ...] = ("status", "subscribe", "query")
_QUERY_KINDS: tuple[str, ...] = ("ranking", "series", "files", "coverage", "totals")
_COVERAGE_KINDS: tuple[str, ...] = ("process", "file", "unknown_owner")
_SERVER_TYPES: tuple[str, ...] = ("status", "snapshot", "gap", "result", "error")

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
_WINDOW_KINDS: tuple[str, ...] = ("range", "since_boot", "launch", "lifetime")

#: Keys accepted on a `query` object, per selector. `window` is mandatory for
#: every kind; `app_key` is mandatory where the answer is app-scoped.
_KIND_REQUIRED: dict[str, frozenset[str]] = {
    "ranking": frozenset({"window"}),
    "series": frozenset({"window", "app_key"}),
    "files": frozenset({"window", "app_key"}),
    "coverage": frozenset({"window"}),
    "totals": frozenset({"window", "app_key"}),
}
_KIND_OPTIONAL: dict[str, frozenset[str]] = {
    # `sort` is either a Metric or the pinned default `bytes_total`.
    # `cursor` is required for wire continuation: a paged ranking request carries the
    # cursor the previous page returned (see appio.iod_history), so omitting it here
    # rejected every continuation and closed the connection mid-query.
    "ranking": frozenset({"metric", "sort", "direction", "limit", "coverage_kind", "cursor"}),
    "series": frozenset({"limit", "cursor", "coverage_kind"}),
    "files": frozenset({"sort", "direction", "search", "cursor", "limit", "coverage_kind"}),
    "coverage": frozenset({"app_key", "coverage_kind"}),
    "totals": frozenset({"coverage_kind"}),
}

#: Per-selector `limit` bounds: the design's files page, series point budget and
#: a defensive row bound for a ranking. A result is never silently truncated.
_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "ranking": (1, 500),
    "series": (1, 4_000),
    "files": (1, 500),
}

_MAX_REQUEST_ID_BYTES = 64
_MAX_SEARCH_BYTES = 256
_MAX_CURSOR_BYTES = 512
_MAX_BOOT_ID_BYTES = 256
_MAX_ERROR_REASON_BYTES = 128
_MAX_ERROR_MESSAGE_BYTES = 512

#: The placeholder the disabled-launch path uses to build the already-rejected
#: `Window`. It never leaves this module.
_PLACEHOLDER_LAUNCH_ID = 1

_WINDOW_WIRE_KEYS = frozenset(
    {"kind", "boot_id", "start_mono_ns", "end_mono_ns", "launch_id"}
)

#: Every key any query selector understands. A known key on the wrong selector is
#: `invalid_selector`; a key no selector knows is `unknown_key`.
_ALL_QUERY_KEYS = (
    frozenset().union(
        *(_KIND_REQUIRED[kind] | _KIND_OPTIONAL[kind] for kind in _QUERY_KINDS)
    )
    | {"kind"}
)


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------


def _require_keys(
    message: dict[str, Any],
    required: frozenset[str] | set[str] | tuple[str, ...],
    allowed: frozenset[str] | set[str] | tuple[str, ...],
    where: str,
) -> None:
    for key in message:
        if key not in allowed:
            raise ProtocolError("unknown_key", f"{where} does not accept {key!r}")
    for key in required:
        if key not in message:
            raise ProtocolError("missing_key", f"{where} requires {key!r}")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(
            "invalid_field", f"{field_name} must be a string, got {type(value).__name__}"
        )
    return value


def _utf8_len(text: str, field_name: str) -> int:
    """Return the UTF-8 length, rejecting text that has no UTF-8 encoding.

    ``json.loads`` accepts a lone-surrogate escape such as ``"\\ud800"`` and
    produces a ``str`` that cannot be encoded. A hostile frame must still leave
    through the typed rejection path, so the encoding error is converted here
    instead of escaping as an unhandled exception.
    """

    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProtocolError(
            "invalid_field", f"{field_name} is not valid Unicode text"
        ) from exc


def _bounded_text(
    value: object, field_name: str, max_bytes: int, *, allow_empty: bool = False
) -> str:
    text = _text(value, field_name)
    if not text and not allow_empty:
        raise ProtocolError("invalid_field", f"{field_name} must be non-empty")
    if "\x00" in text:
        raise ProtocolError("invalid_field", f"{field_name} must not contain NUL")
    if _utf8_len(text, field_name) > max_bytes:
        raise ProtocolError(
            "invalid_field", f"{field_name} exceeds {max_bytes} UTF-8 bytes"
        )
    return text


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(
            "invalid_field", f"{field_name} must be an integer, got {type(value).__name__}"
        )
    try:
        validate_sqlite_integer(value, field_name)
    except ValueError as exc:
        raise ProtocolError("invalid_field", str(exc)) from exc
    if value < minimum:
        raise ProtocolError("invalid_field", f"{field_name} must be at least {minimum}")
    return value


def _optional_integer(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field_name)


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(
            "invalid_field", f"{field_name} must be a boolean, got {type(value).__name__}"
        )
    return value


def _choices(value: object, allowed: tuple[str, ...], field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise ProtocolError(
            "invalid_field", f"{field_name} must be one of {', '.join(allowed)}"
        )
    return value


def _plain_object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(
            "invalid_field", f"{field_name} must be an object, got {type(value).__name__}"
        )
    return value


def _object_list(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProtocolError(
            "invalid_field", f"{field_name} must be an array, got {type(value).__name__}"
        )
    for item in value:
        if not isinstance(item, dict):
            raise ProtocolError("invalid_field", f"{field_name} entries must be objects")
    return value


def _protocol_version(message: dict[str, Any]) -> None:
    if "protocol" not in message:
        raise ProtocolError("missing_key", "a frame requires 'protocol'")
    value = message["protocol"]
    if isinstance(value, bool) or not isinstance(value, int) or value != PROTOCOL_VERSION:
        raise ProtocolError(
            "protocol_mismatch", f"'protocol' must be the integer {PROTOCOL_VERSION}"
        )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamCursor:
    """A boot-scoped ``(boot_id, iod_epoch, seq)`` stream position.

    The boot identity is part of the key, not decoration: ``(epoch, seq)`` alone
    cannot be compared across boots, and comparing it would replay another boot's
    frames into a gap row that claims continuity.
    """

    boot_id: str
    iod_epoch: int
    seq: int

    def __post_init__(self) -> None:
        _bounded_text(self.boot_id, "cursor.boot_id", _MAX_BOOT_ID_BYTES)
        _integer(self.iod_epoch, "cursor.iod_epoch", minimum=1)
        _integer(self.seq, "cursor.seq")


@dataclass(frozen=True)
class Capabilities:
    """What the daemon can currently observe, independent of what it stores."""

    proc_all: bool
    dev_stat: bool
    file_io: bool
    bpf_loaded: bool

    def __post_init__(self) -> None:
        for name in ("proc_all", "dev_stat", "file_io", "bpf_loaded"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"capabilities.{name} must be a boolean")


@dataclass(frozen=True)
class QueryRequest:
    """One typed historical selector. Never SQL, a path, or a command."""

    kind: QueryKind
    window: Window
    app_key: str | None = None
    metric: str = "total_bytes"
    sort: str = "bytes_total"
    direction: str = "desc"
    search: str | None = None
    cursor: str | None = None
    limit: int = 500
    coverage_kind: CoverageKind = "process"


@dataclass(frozen=True)
class Request:
    """A validated read-only client request."""

    request_id: str
    type: Literal["status", "subscribe", "query"]
    since: StreamCursor | None
    query: QueryRequest | None
    #: Additive field: set when a cursor lacked boot identity and the server must
    #: answer with an explicit gap/resync rather than replay across boots.
    resync_reason: str | None = None


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _request_id(value: object) -> str:
    try:
        return _bounded_text(value, "request_id", _MAX_REQUEST_ID_BYTES)
    except ProtocolError as exc:
        raise ProtocolError("invalid_request_id", exc.detail or str(exc)) from exc


def _cursor(value: object, field_name: str) -> StreamCursor:
    cursor = _plain_object(value, field_name)
    _require_keys(
        cursor, frozenset({"boot_id", "iod_epoch", "seq"}), frozenset({"boot_id", "iod_epoch", "seq"}), field_name
    )
    return StreamCursor(
        boot_id=_text(cursor["boot_id"], f"{field_name}.boot_id"),
        iod_epoch=cursor["iod_epoch"],
        seq=cursor["seq"],
    )


def _optional_cursor(value: object, field_name: str) -> StreamCursor | None:
    if value is None:
        return None
    return _cursor(value, field_name)


def _since(value: object) -> tuple[StreamCursor | None, str | None]:
    """Parse a subscribe cursor, distinguishing a legacy cursor from no cursor."""

    if value is None:
        return None, None
    since = _plain_object(value, "since")
    _require_keys(
        since,
        frozenset(),
        frozenset({"boot_id", "iod_epoch", "seq"}),
        "since",
    )
    if "boot_id" not in since:
        # (epoch, seq) without a boot cannot be replayed safely. Accept it as a
        # resync request; the server emits an explicit gap for it.
        _require_keys(
            since,
            frozenset({"iod_epoch", "seq"}),
            frozenset({"iod_epoch", "seq"}),
            "since",
        )
        _integer(since["iod_epoch"], "since.iod_epoch", minimum=1)
        _integer(since["seq"], "since.seq")
        return None, "cursor_boot_unknown"
    return _cursor(since, "since"), None


def _wire_window(value: object) -> Window:
    """Build a :class:`Window` from its wire form, honouring A22 first."""

    wire = _plain_object(value, "query.window")
    kind = wire.get("kind")
    if isinstance(kind, bool) or not isinstance(kind, str) or kind not in _WINDOW_KINDS:
        raise ProtocolError(
            "invalid_window",
            f"window.kind must be one of {', '.join(_WINDOW_KINDS)}",
        )
    if kind == "launch":
        # A22: always disabled, before any identity or ID lookup. The request form
        # carries no launch id, so nothing here can smuggle a database surrogate.
        raise WindowUnavailableError("launch_attribution", LAUNCH_WINDOW_MESSAGE)
    for key in wire:
        if key not in _WINDOW_WIRE_KEYS:
            raise ProtocolError("unknown_key", f"query.window does not accept {key!r}")
    try:
        window = Window(
            kind=kind,  # type: ignore[arg-type]
            boot_id=wire.get("boot_id"),  # type: ignore[arg-type]
            start_mono_ns=wire.get("start_mono_ns"),  # type: ignore[arg-type]
            end_mono_ns=wire.get("end_mono_ns"),  # type: ignore[arg-type]
            launch_id=wire.get("launch_id"),  # type: ignore[arg-type]
        )
        return validate_window(window)
    except ValueError as exc:
        raise ProtocolError("invalid_window", str(exc)) from exc


def _app_key(value: object) -> str:
    try:
        return validate_app_key(value)
    except ValueError as exc:
        raise ProtocolError("invalid_app_key", str(exc)) from exc


def _search(value: object) -> str | None:
    try:
        search = validate_search(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ProtocolError("invalid_field", str(exc)) from exc
    if search is None or search == "":
        return search
    if _utf8_len(search, "query.search") > _MAX_SEARCH_BYTES:
        raise ProtocolError(
            "invalid_field", f"search exceeds {_MAX_SEARCH_BYTES} UTF-8 bytes"
        )
    return search


def _page_cursor(value: object) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, "query.cursor", _MAX_CURSOR_BYTES)


def _limit(kind: str, value: object) -> int:
    minimum, maximum = _LIMIT_BOUNDS[kind]
    number = _integer(value, "query.limit")
    if not minimum <= number <= maximum:
        raise ProtocolError(
            "invalid_field", f"query.limit for {kind} must be {minimum}..{maximum}"
        )
    return number


def _sort(kind: str, value: object) -> str:
    if kind == "ranking":
        if value == "bytes_total":
            return "bytes_total"
        return _choices(value, _METRICS, "query.sort")
    return _choices(value, _FILE_SORTS, "query.sort")


def _query(value: object) -> QueryRequest:
    query = _plain_object(value, "query")
    if "kind" not in query:
        raise ProtocolError("missing_key", "query requires 'kind'")
    kind = query["kind"]
    if isinstance(kind, bool) or not isinstance(kind, str) or kind not in _QUERY_KINDS:
        raise ProtocolError(
            "unsupported_query_kind",
            f"query.kind must be one of {', '.join(_QUERY_KINDS)}",
        )
    allowed = _KIND_REQUIRED[kind] | _KIND_OPTIONAL[kind] | {"kind"}
    for key in query:
        if key not in _ALL_QUERY_KEYS:
            raise ProtocolError("unknown_key", f"query does not accept {key!r}")
        if key not in allowed:
            raise ProtocolError(
                "invalid_selector", f"{key!r} does not select anything for a {kind} query"
            )
    _require_keys(query, _KIND_REQUIRED[kind] | {"kind"}, allowed, "query")

    if "coverage_kind" in query:
        coverage_kind = query["coverage_kind"]
        if kind != "coverage" and coverage_kind != "process":
            # Only the coverage result is selected by this field; elsewhere the
            # default is the only accepted value.
            raise ProtocolError(
                "invalid_selector",
                "coverage_kind selects only the coverage query; elsewhere it is default-only",
            )
        coverage_kind = _choices(coverage_kind, _COVERAGE_KINDS, "query.coverage_kind")
    else:
        coverage_kind = "process"

    return QueryRequest(
        kind=kind,  # type: ignore[arg-type]
        window=_wire_window(query["window"]),
        app_key=_app_key(query["app_key"]) if "app_key" in query else None,
        metric=_choices(query["metric"], _METRICS, "query.metric")
        if "metric" in query
        else "total_bytes",
        sort=_sort(kind, query["sort"]) if "sort" in query else "bytes_total",
        direction=_choices(query["direction"], _DIRECTIONS, "query.direction")
        if "direction" in query
        else "desc",
        search=_search(query["search"]) if "search" in query else None,
        cursor=_page_cursor(query["cursor"]) if "cursor" in query else None,
        limit=_limit(kind, query["limit"]) if "limit" in query else 500,
        coverage_kind=coverage_kind,  # type: ignore[arg-type]
    )


def validate_request(message: dict[str, Any]) -> Request:
    """Validate a complete inbound request before anything dispatches on it."""

    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", "a request must be a JSON object")
    _protocol_version(message)
    kind = message.get("type")
    if isinstance(kind, bool) or not isinstance(kind, str) or kind not in _REQUEST_TYPES:
        raise ProtocolError(
            "unsupported_type",
            f"the only request types are {', '.join(_REQUEST_TYPES)}",
        )

    if kind == "status":
        _require_keys(message, frozenset({"protocol", "type", "request_id"}), frozenset({"protocol", "type", "request_id"}), "status")
        return Request(
            request_id=_request_id(message["request_id"]),
            type="status",
            since=None,
            query=None,
        )
    if kind == "subscribe":
        _require_keys(
            message,
            frozenset({"protocol", "type", "request_id", "since"}),
            frozenset({"protocol", "type", "request_id", "since"}),
            "subscribe",
        )
        since, resync_reason = _since(message["since"])
        return Request(
            request_id=_request_id(message["request_id"]),
            type="subscribe",
            since=since,
            query=None,
            resync_reason=resync_reason,
        )
    _require_keys(
        message,
        frozenset({"protocol", "type", "request_id", "query"}),
        frozenset({"protocol", "type", "request_id", "query"}),
        "query",
    )
    return Request(
        request_id=_request_id(message["request_id"]),
        type="query",
        since=None,
        query=_query(message["query"]),
    )


# ---------------------------------------------------------------------------
# Server envelope validation
# ---------------------------------------------------------------------------


def _capabilities(value: object) -> Capabilities:
    capabilities = _plain_object(value, "snapshot.capabilities")
    names = frozenset({"proc_all", "dev_stat", "file_io", "bpf_loaded"})
    _require_keys(capabilities, names, names, "snapshot.capabilities")
    try:
        return Capabilities(
            proc_all=_boolean(capabilities["proc_all"], "capabilities.proc_all"),
            dev_stat=_boolean(capabilities["dev_stat"], "capabilities.dev_stat"),
            file_io=_boolean(capabilities["file_io"], "capabilities.file_io"),
            bpf_loaded=_boolean(capabilities["bpf_loaded"], "capabilities.bpf_loaded"),
        )
    except ValueError as exc:
        raise ProtocolError("invalid_field", str(exc)) from exc


def _finite_number(value: object, field_name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("invalid_field", f"{field_name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError("invalid_field", f"{field_name} must be finite")
    return value


def _status(message: dict[str, Any]) -> dict[str, Any]:
    names = frozenset(
        {
            "protocol",
            "type",
            "request_id",
            "enabled",
            "answering",
            "pending_activation",
            "ownership_start_mono_ns",
            "cursor",
        }
    )
    _require_keys(message, names, names, "status")
    normalized = dict(message)
    normalized["request_id"] = _request_id(message["request_id"])
    for name in ("enabled", "answering", "pending_activation"):
        normalized[name] = _boolean(message[name], f"status.{name}")
    normalized["ownership_start_mono_ns"] = _optional_integer(
        message["ownership_start_mono_ns"], "status.ownership_start_mono_ns"
    )
    normalized["cursor"] = _optional_cursor(message["cursor"], "status.cursor")
    return normalized


def _snapshot(message: dict[str, Any]) -> dict[str, Any]:
    names = frozenset(
        {
            "protocol",
            "type",
            "boot_id",
            "iod_epoch",
            "seq",
            "ts_mono_ns",
            "ts_wall",
            "source",
            "capabilities",
            "procs",
            "devs",
            "files",
            "coverage",
        }
    )
    _require_keys(message, names, names, "snapshot")
    normalized = dict(message)
    normalized["boot_id"] = _bounded_text(
        message["boot_id"], "snapshot.boot_id", _MAX_BOOT_ID_BYTES
    )
    normalized["iod_epoch"] = _integer(
        message["iod_epoch"], "snapshot.iod_epoch", minimum=1
    )
    normalized["seq"] = _integer(message["seq"], "snapshot.seq")
    normalized["ts_mono_ns"] = _integer(message["ts_mono_ns"], "snapshot.ts_mono_ns")
    normalized["ts_wall"] = _finite_number(message["ts_wall"], "snapshot.ts_wall")
    if message["source"] != "iod":
        raise ProtocolError("invalid_field", "snapshot.source must be 'iod'")
    normalized["capabilities"] = _capabilities(message["capabilities"])
    for name in ("procs", "devs", "files"):
        normalized[name] = _object_list(message[name], f"snapshot.{name}")
    normalized["coverage"] = _plain_object(message["coverage"], "snapshot.coverage")
    return normalized


def _gap(message: dict[str, Any]) -> dict[str, Any]:
    names = frozenset(
        {
            "protocol",
            "type",
            "request_id",
            "boot_id",
            "iod_epoch",
            "from_mono_ns",
            "to_mono_ns",
            "previous",
            "current",
            "reason",
        }
    )
    _require_keys(
        message,
        frozenset(
            {
                "protocol",
                "type",
                "boot_id",
                "iod_epoch",
                "from_mono_ns",
                "to_mono_ns",
                "previous",
                "current",
                "reason",
            }
        ),
        names,
        "gap",
    )
    normalized = dict(message)
    normalized["boot_id"] = _bounded_text(
        message["boot_id"], "gap.boot_id", _MAX_BOOT_ID_BYTES
    )
    normalized["iod_epoch"] = _integer(message["iod_epoch"], "gap.iod_epoch", minimum=1)
    start = _optional_integer(message["from_mono_ns"], "gap.from_mono_ns")
    end = _optional_integer(message["to_mono_ns"], "gap.to_mono_ns")
    if start is not None and end is not None and start >= end:
        # Bounds are half-open: an empty interval cannot be a gap.
        raise ProtocolError(
            "invalid_field", "gap bounds must be half-open with from_mono_ns < to_mono_ns"
        )
    normalized["from_mono_ns"] = start
    normalized["to_mono_ns"] = end
    normalized["previous"] = _optional_cursor(message["previous"], "gap.previous")
    normalized["current"] = _optional_cursor(message["current"], "gap.current")
    normalized["reason"] = _bounded_text(
        message["reason"], "gap.reason", _MAX_ERROR_REASON_BYTES
    )
    if "request_id" in message:
        normalized["request_id"] = _request_id(message["request_id"])
    return normalized


def _result(message: dict[str, Any]) -> dict[str, Any]:
    names = frozenset({"protocol", "type", "request_id", "kind", "payload"})
    _require_keys(message, names, names, "result")
    kind = message["kind"]
    if isinstance(kind, bool) or not isinstance(kind, str) or kind not in _QUERY_KINDS:
        raise ProtocolError(
            "unsupported_query_kind",
            f"result.kind must be one of {', '.join(_QUERY_KINDS)}",
        )
    payload = message["payload"]
    if kind in ("ranking", "series", "files"):
        if not isinstance(payload, list):
            raise ProtocolError("invalid_field", f"result.{kind} payload must be an array")
    elif not isinstance(payload, dict):
        raise ProtocolError("invalid_field", f"result.{kind} payload must be an object")
    normalized = dict(message)
    normalized["request_id"] = _request_id(message["request_id"])
    return normalized


def _error(message: dict[str, Any]) -> dict[str, Any]:
    names = frozenset({"protocol", "type", "request_id", "reason", "message"})
    _require_keys(
        message,
        frozenset({"protocol", "type", "reason", "message"}),
        names,
        "error",
    )
    normalized = dict(message)
    normalized["reason"] = _bounded_text(
        message["reason"], "error.reason", _MAX_ERROR_REASON_BYTES
    )
    normalized["message"] = _bounded_text(
        message["message"], "error.message", _MAX_ERROR_MESSAGE_BYTES, allow_empty=True
    )
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in normalized["message"] + normalized["reason"]
    ):
        # A traceback or raw stderr must never reach a peer.
        raise ProtocolError(
            "invalid_field", "error reason/message must not contain control characters"
        )
    if "request_id" in message:
        normalized["request_id"] = _request_id(message["request_id"])
    return normalized


_ENVELOPES = {
    "status": _status,
    "snapshot": _snapshot,
    "gap": _gap,
    "result": _result,
    "error": _error,
}


def validate_server_message(message: dict[str, Any]) -> dict[str, Any]:
    """Validate one server envelope and return its normalized identity."""

    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", "a server message must be a JSON object")
    _protocol_version(message)
    kind = message.get("type")
    if isinstance(kind, bool) or not isinstance(kind, str) or kind not in _SERVER_TYPES:
        raise ProtocolError(
            "unsupported_type",
            f"the only server envelopes are {', '.join(_SERVER_TYPES)}",
        )
    return _ENVELOPES[kind](message)
