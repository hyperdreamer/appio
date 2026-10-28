"""Read-through client for the AppIO daemon socket (design §4.2, §5; task 11).

The GUI must render daemon-owned data without ever becoming a second writer and
without trusting an endpoint that merely *looks* like the daemon. This module is
the reader half of that contract. It owns no store, opens no database and writes
nothing to disk: every value it produces is either a live snapshot already
committed by the daemon or a historical row read through the daemon's own query
surface.

Four threats shape the design, and each has a named countermeasure.

**A non-root endpoint.** A socket file with ``root:appio 0660`` proves a mode, not
an identity. :meth:`IodClient._attempt_connect` therefore verifies the runtime
directory/socket metadata with the injected ``path_verifier`` (production:
:func:`appio.ipc_security.verify_runtime_paths`) and reads the peer's kernel
credentials with the injected ``peer_reader`` (production:
:func:`appio.ipc_security.read_peer`), then calls
:func:`appio.ipc_security.require_root_server`. A refusal closes the socket
before any request is written and before any callback or cache entry exists, so a
spoofed endpoint can never manufacture state.

**A delayed frame from an earlier authority.** :class:`CursorTracker` accepts one
boot/epoch generation at a time. A generation the client has left is *retired*
and can never become current again, a lower epoch of the same boot is refused,
and sequence numbers must advance monotonically inside a generation. A new
connection also drops authority: no frame is trusted until that connection has
its own status reply, so trust cannot be inherited across a reconnect.

**A loss rendered as continuity.** A reconnect either resumes from the exact
cursor the client actually accepted or asks the daemon for an explicit ``gap``.
Known bounds are forwarded verbatim; unknown bounds stay ``None`` instead of
being invented as ``[0, now)``. A transport loss produces one :class:`GapNotice`
with an unknown end, never a period of measured zero.

**A silent or absent daemon.** Absence, path refusal, peer refusal, framing
damage and a five-second silence all produce ``verified=False`` and
``answering=False`` with a machine-readable ``last_error``. Historical queries
fail closed rather than resolving to an empty result, so "no daemon" can never be
painted as "no activity".

Transport is nonblocking and single-threaded: :meth:`IodClient.pump` is the only
driver, it never sleeps, and it never blocks the Qt thread. Reconnection is a
clock-driven ladder (0.25, 0.5, 1, 2, then 5 s), so a virtual clock can exercise
the whole schedule. Framing, peer authorization and result decoding are consumed
from :mod:`appio.ipc_protocol`, :mod:`appio.ipc_security` and
:mod:`appio.iod_history`; nothing about them is re-derived here.
"""

from __future__ import annotations

import errno
import grp
import json
import os
import select
import socket
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import Window, WindowUnavailableError
from .identity import validate_app_key
from .iod_history import from_wire
from .ipc_models import (
    Capabilities,
    QueryRequest,
    StreamCursor,
    validate_request,
    validate_server_message,
)
from .ipc_protocol import MAX_INBOUND, MAX_OUTBOUND, FrameDecoder, ProtocolError, encode_frame
from .ipc_security import Peer, PeerRejected, read_peer, require_root_server, verify_runtime_paths

__all__ = [
    "AppRegistry",
    "BACKOFF_SECONDS",
    "CACHE_BYTES",
    "CONNECT_DEADLINE_NS",
    "ClientClosed",
    "ClientState",
    "ClientUnavailable",
    "ConnectionLost",
    "CursorTracker",
    "GapNotice",
    "IodClient",
    "IodClientError",
    "MAX_PENDING_REQUESTS",
    "MAX_RESPONSE_BYTES",
    "MAX_RETIRED_BOOTS",
    "NO_ANSWER_NS",
    "NS",
    "RemoteError",
    "REQUEST_LIFETIME_NS",
    "RequestRejected",
]

NS = 1_000_000_000

BACKOFF_SECONDS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 5.0)
"""Bounded reconnect ladder (D4). The last rung repeats; it never grows."""

REQUEST_LIFETIME_NS = 5 * NS
"""A historical request that is unanswered for five seconds settles as a failure."""

NO_ANSWER_NS = 5 * NS
"""A connection that answers nothing for five seconds is reported as not answering."""

CONNECT_DEADLINE_NS = 2 * NS
"""How long an in-progress AF_UNIX connect may stay incomplete before it is dropped."""

MAX_PENDING_REQUESTS = 64
"""Outstanding historical queries one client keeps, matching the daemon's client cap."""

MAX_RESPONSE_BYTES = 256 * 1024
"""Client-side result cap, matching the daemon's per-frame egress bound (D4)."""

CACHE_BYTES = 4 * 1024 * 1024
"""Byte budget for the read-through result cache; entries are evicted whole."""

MAX_GAP_RECORDS = 32
"""Distinct daemon gap frames remembered for de-duplication."""

MAX_RETIRED_BOOTS = 8
"""Boot identities that may never become current again once the client leaves them."""

MAX_APPS = 65_536
"""Upper bound on locally allocated presentation AppKeys."""

READ_BUDGET = 65_536
"""Bytes read from the socket per ``recv`` call."""

_MAX_DETAIL = 200
"""Longest remote detail string copied into a client exception."""


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class IodClientError(RuntimeError):
    """A client-side failure carrying a stable machine ``reason``.

    ``message`` is the bounded human detail. For a daemon ``error`` envelope the
    exact wire text is preserved, because A22 reasons are shown verbatim to the
    operator (:data:`appio.contracts.LAUNCH_WINDOW_MESSAGE` for a launch window).
    ``str(error)`` therefore returns only the message, so a caller that renders
    ``str(error)`` cannot wrap or paraphrase a ratified reason.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        if not isinstance(reason, str) or reason == "":
            raise ValueError("reason must be a non-empty string")
        if message is not None and not isinstance(message, str):
            raise ValueError("message must be a string or None")
        self.reason = reason
        self.message = message
        super().__init__(message if message else reason)

    def __str__(self) -> str:
        return self.message if self.message else self.reason


class RemoteError(IodClientError):
    """An ``error`` envelope produced by the daemon; reason and text are verbatim."""


class ClientUnavailable(IodClientError):
    """No verified, answering daemon connection exists for this request."""


class RequestRejected(IodClientError):
    """The client refused the request locally.

    Covers the pending-request cap, an oversized reply, a locally refused
    selector (A22's ``launch_attribution`` keeps its ratified reason and text) and
    an expired request deadline.
    """


class ConnectionLost(IodClientError):
    """The connection that carried this request ended before an answer arrived."""


class ClientClosed(IodClientError):
    """The client was closed by its owner."""


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GapNotice:
    """One recorded discontinuity, for Task 12 to persist as local provenance.

    ``boot_id``/``iod_epoch``/``seq`` identify the position the gap is anchored
    to; the empty/zero triple means the client had never accepted a sample. Bounds
    are half-open and stay ``None`` when they are unknown, so an unknown gap can
    never be stored as a concrete interval such as ``[0, now)``.
    """

    boot_id: str
    iod_epoch: int
    seq: int
    from_mono_ns: int | None
    to_mono_ns: int | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.boot_id, str):
            raise ValueError("gap.boot_id must be a string")
        for name in ("iod_epoch", "seq"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"gap.{name} must be a non-negative integer")
        if self.boot_id == "" and (self.iod_epoch != 0 or self.seq != 0):
            raise ValueError("an unidentified gap must carry the empty identity triple")
        for name in ("from_mono_ns", "to_mono_ns"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"gap.{name} must be an integer or None")
        if self.from_mono_ns is not None and self.to_mono_ns is not None:
            if self.from_mono_ns >= self.to_mono_ns:
                raise ValueError("gap bounds must be half-open with from_mono_ns < to_mono_ns")
        if not isinstance(self.reason, str) or self.reason == "":
            raise ValueError("gap.reason must be a non-empty string")
        if len(self.reason.encode("utf-8")) > 128:
            raise ValueError("gap.reason exceeds 128 UTF-8 bytes")


@dataclass(frozen=True)
class ClientState:
    """Everything the GUI may display about the daemon link.

    ``verified`` means the endpoint proved its identity *on the current
    connection* and answered with a validated status. ``answering`` additionally
    requires that status to report a working collector; service enablement alone
    never sets it.

    ``cursor`` is the newest position the client knows about: the last snapshot it
    accepted, or — before the first snapshot of a generation — the committed
    cursor that status advertised, which is a position the daemon has durably
    written but this client has not yet received. It survives a disconnect as
    resume knowledge, so callers must gate it on ``verified`` and must not read it
    as "data up to here is on screen".

    ``capabilities`` stays ``None`` until a snapshot proves what is observable: an
    unavailable file source is a capability, never an empty file table.
    """

    verified: bool = False
    answering: bool = False
    cursor: StreamCursor | None = None
    capabilities: Capabilities | None = None
    pending_activation: bool | None = None
    last_error: str | None = None


class AppRegistry:
    """Stable presentation IDs for local and remote :data:`AppKey` values.

    A database-local ``app_id`` never leaves its store, so a GUI that merges live
    daemon rows with its own rows needs an adapter. IDs are allocated on first
    sight and never reused, which keeps a table row, a selection and a chart
    series pointing at the same application for the life of the process.
    """

    __slots__ = ("_by_key", "_by_id", "_next")

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._by_id: dict[int, str] = {}
        self._next = 1

    def id_for(self, app_key: str) -> int:
        key = validate_app_key(app_key)
        existing = self._by_key.get(key)
        if existing is not None:
            return existing
        if self._next > MAX_APPS:
            raise ValueError(f"the AppKey registry is full at {MAX_APPS} applications")
        identifier = self._next
        self._next += 1
        self._by_key[key] = identifier
        self._by_id[identifier] = key
        return identifier

    def key_for(self, app_id: int) -> str:
        if isinstance(app_id, bool) or not isinstance(app_id, int):
            raise TypeError("app_id must be an integer")
        try:
            return self._by_id[app_id]
        except KeyError:
            raise KeyError(f"unknown presentation AppKey id {app_id}") from None

    def __len__(self) -> int:
        return len(self._by_key)


# --------------------------------------------------------------------------- #
# Cursor authority
# --------------------------------------------------------------------------- #


def _identity_of(message: Mapping[str, Any]) -> tuple[str, int, int] | None:
    """Read the advertised ``(boot_id, iod_epoch, seq)`` from a status message.

    Accepts both the validated status envelope (identity in ``cursor``) and the
    bare identity record the tracker's unit contract uses. ``cursor: None`` is the
    honest answer of a daemon that has committed nothing yet, and carries no
    generation at all.
    """

    cursor = message.get("cursor", _MISSING)
    if cursor is not _MISSING:
        if cursor is None:
            return None
        if isinstance(cursor, StreamCursor):
            return (cursor.boot_id, cursor.iod_epoch, cursor.seq)
        if isinstance(cursor, Mapping):
            return (
                str(cursor["boot_id"]),
                int(cursor["iod_epoch"]),
                int(cursor["seq"]),
            )
        raise ValueError("status.cursor must be a cursor record or None")
    if "boot_id" not in message:
        return None
    return (str(message["boot_id"]), int(message["iod_epoch"]), int(message["seq"]))


_MISSING = object()


class CursorTracker:
    """Decide which stream position may become current, and which may not.

    Invariants, each pinned by a test:

    * before a connection's status exchange, no snapshot is trusted at all;
    * a boot the client has left is retired permanently — a machine cannot reboot
      back into an earlier boot, so such a frame is stale by construction;
    * a lower epoch of the current boot never takes over;
    * inside one generation the sequence must advance, except that the first
      sample of a newly established generation is allowed to sit exactly on the
      advertised floor (the daemon replays its committed snapshot at that cursor);
    * :attr:`resume` is only ever a cursor actually accepted, so a client never
      claims to have received data it never saw.
    """

    __slots__ = ("_accepted", "_authority", "_current", "_retired", "_seen", "_status_seen")

    def __init__(self, *, max_retired: int = MAX_RETIRED_BOOTS) -> None:
        if isinstance(max_retired, bool) or not isinstance(max_retired, int) or max_retired < 1:
            raise ValueError("max_retired must be a positive integer")
        self._accepted: StreamCursor | None = None
        self._authority: tuple[str, int] | None = None
        self._current: StreamCursor | None = None
        self._retired: deque[str] = deque(maxlen=max_retired)
        self._seen = False
        self._status_seen = False

    # -- inspection -------------------------------------------------------- #

    @property
    def current(self) -> StreamCursor | None:
        """Last accepted position, or the provisional floor of the generation."""

        return self._current

    @property
    def resume(self) -> StreamCursor | None:
        """The last cursor actually accepted; the only safe ``since`` value."""

        return self._accepted

    @property
    def authority(self) -> tuple[str, int] | None:
        return self._authority

    # -- connection lifetime ---------------------------------------------- #

    def begin_connection(self) -> None:
        """Drop authority for a new connection while keeping resume knowledge."""

        self._authority = None
        self._status_seen = False

    def _retire(self, boot_id: str) -> None:
        if boot_id not in self._retired:
            self._retired.append(boot_id)

    # -- authority establishment ------------------------------------------ #

    def accept_status(self, message: Mapping[str, Any]) -> None:
        """Adopt (or refuse) the generation a status message advertises."""

        self._status_seen = True
        identity = _identity_of(message)
        if identity is None:
            return
        boot_id, epoch, seq = identity
        generation = (boot_id, epoch)
        if boot_id in self._retired:
            return
        if self._authority is not None:
            if generation == self._authority:
                if not self._seen:
                    floor = self._current
                    if floor is None or (floor.boot_id, floor.iod_epoch) != generation:
                        self._current = StreamCursor(boot_id, epoch, seq)
                    elif seq > floor.seq:
                        self._current = StreamCursor(boot_id, epoch, seq)
                return
            if boot_id != self._authority[0] or epoch <= self._authority[1]:
                # A boot may only change on a fresh connection, and only a later
                # epoch of the same boot may replace the current one.
                return
            self._retire(self._authority[0])
        self._authority = generation
        floor = self._current
        if floor is not None and (floor.boot_id, floor.iod_epoch) == generation:
            if not self._seen and seq > floor.seq:
                self._current = StreamCursor(boot_id, epoch, seq)
            return
        if floor is not None and floor.boot_id != boot_id:
            self._retire(floor.boot_id)
        self._current = StreamCursor(boot_id, epoch, seq)
        self._seen = False

    def accept_gap(self, message: Mapping[str, Any]) -> None:
        """Anchor the tracker after the daemon declared an explicit discontinuity."""

        boot_id = str(message["boot_id"])
        epoch = int(message["iod_epoch"])
        generation = (boot_id, epoch)
        if boot_id in self._retired:
            return
        if self._authority is not None and generation != self._authority:
            self._retire(self._authority[0])
        self._authority = generation
        previous = message.get("previous")
        if isinstance(previous, StreamCursor) and (previous.boot_id, previous.iod_epoch) == generation:
            if self._current is None or self._current.seq < previous.seq:
                self._current = previous
            self._seen = True
            return
        current = self._current
        if current is not None and (current.boot_id, current.iod_epoch) == generation:
            self._seen = True
            return
        if current is not None and current.boot_id != boot_id:
            self._retire(current.boot_id)
        self._current = None
        self._seen = False

    # -- samples ----------------------------------------------------------- #

    def accept(self, cursor: StreamCursor) -> bool:
        """Accept one snapshot cursor, or refuse it without any side effect."""

        if not isinstance(cursor, StreamCursor):
            raise TypeError("accept requires a StreamCursor")
        generation = (cursor.boot_id, cursor.iod_epoch)
        if cursor.boot_id in self._retired:
            return False
        if self._authority is None:
            if not self._status_seen:
                return False
            self._authority = generation
            current = self._current
            if current is not None and (current.boot_id, current.iod_epoch) != generation:
                if current.boot_id != cursor.boot_id:
                    self._retire(current.boot_id)
                self._current = None
                self._seen = False
        if generation != self._authority:
            return False
        floor = self._current
        if self._seen:
            if floor is not None and cursor.seq <= floor.seq:
                return False
        elif floor is not None and cursor.seq < floor.seq:
            # The provisional floor is the daemon's own committed position; a
            # snapshot below it is older than what we were told was committed.
            return False
        self._current = cursor
        self._accepted = cursor
        self._seen = True
        return True


# --------------------------------------------------------------------------- #
# Request serialization
# --------------------------------------------------------------------------- #


def _window_wire(window: Window) -> dict[str, Any]:
    if not isinstance(window, Window):
        raise TypeError("query.window must be a Window")
    wire: dict[str, Any] = {"kind": window.kind}
    for name in ("boot_id", "start_mono_ns", "end_mono_ns", "launch_id"):
        value = getattr(window, name)
        if value is not None:
            wire[name] = value
    return wire


_SELECTOR_DEFAULTS: dict[str, Any] = {
    "metric": "total_bytes",
    "sort": "bytes_total",
    "direction": "desc",
    "search": None,
    "cursor": None,
    "limit": 500,
    "coverage_kind": "process",
}

_KIND_SELECTORS: dict[str, frozenset[str]] = {
    "ranking": frozenset({"metric", "sort", "direction", "limit", "cursor"}),
    "series": frozenset({"limit", "cursor"}),
    "files": frozenset({"sort", "direction", "search", "cursor", "limit"}),
    "coverage": frozenset({"coverage_kind"}),
    "totals": frozenset(),
}


def _query_wire(request: QueryRequest) -> dict[str, Any]:
    """Serialize one typed selector, including only the fields it selects.

    A field that does not belong to the selector is a rejection rather than a
    silently ignored option, so a caller that asks for a search on a series, or a
    coverage kind on a ranking, gets an error instead of a different answer than
    the one it requested. This mirrors the protocol's per-kind table and then
    re-validates the result through :func:`validate_request`; that second pass is
    deliberate, because it turns any drift in the shared validation table into a
    loud local failure instead of a daemon-side disconnect.
    """

    if not isinstance(request, QueryRequest):
        raise TypeError("query requires a QueryRequest")
    selectors = _KIND_SELECTORS.get(request.kind)
    if selectors is None:
        raise ProtocolError("unsupported_query_kind", f"unknown query kind {request.kind!r}")
    for name, default in _SELECTOR_DEFAULTS.items():
        if name in selectors:
            continue
        value = getattr(request, name)
        if value != default:
            raise ProtocolError(
                "invalid_selector",
                f"{name!r} does not select anything for a {request.kind} query",
            )
    query: dict[str, Any] = {"kind": request.kind, "window": _window_wire(request.window)}
    if request.app_key is not None:
        query["app_key"] = request.app_key
    if request.kind == "ranking":
        query["metric"] = request.metric
        query["sort"] = request.sort
        query["direction"] = request.direction
        query["limit"] = request.limit
        if request.cursor is not None:
            # Continuation: a paged ranking must carry the cursor the previous
            # page returned, or the server answers page one again forever.
            query["cursor"] = request.cursor
    elif request.kind in ("series", "files"):
        query["limit"] = request.limit
        if request.cursor is not None:
            query["cursor"] = request.cursor
        if request.kind == "files":
            query["sort"] = request.sort
            query["direction"] = request.direction
            if request.search is not None:
                query["search"] = request.search
    else:  # coverage, totals
        if request.kind == "coverage":
            query["coverage_kind"] = request.coverage_kind
    frame = {
        "protocol": 1,
        "type": "query",
        # The self-check needs a *valid* request id; the real one is assigned at
        # send time from the connection generation.
        "request_id": "self-check",
        "query": query,
    }
    validate_request(frame)
    return query


def _bounded_detail(text: object) -> str:
    rendered = str(text)
    return rendered if len(rendered) <= _MAX_DETAIL else rendered[:_MAX_DETAIL] + "…"


def _verify_production_paths(path: Path) -> None:
    """Production path check: ``root:appio`` directory and socket, via ``lstat``."""

    try:
        gid = grp.getgrnam("appio").gr_gid
    except KeyError as error:  # pragma: no cover - depends on host packaging
        raise PeerRejected("appio_group_missing", "the appio group is not defined") from error
    verify_runtime_paths(path, appio_gid=gid)


def _production_connector(path: Path) -> socket.socket:
    """Open a nonblocking AF_UNIX stream to ``path``; never blocks the caller."""

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        code = sock.connect_ex(os.fspath(path))
        if code not in (0, errno.EINPROGRESS, errno.EAGAIN, errno.EWOULDBLOCK):
            raise OSError(code, os.strerror(code))
    except BaseException:
        sock.close()
        raise
    return sock


def _connected(sock: socket.socket) -> bool:
    try:
        sock.getpeername()
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


@dataclass
class _Pending:
    future: Future[dict]
    key: tuple[str, str]
    generation: int
    deadline_ns: int


@dataclass
class _Cached:
    value: dict[str, Any]
    size: int


class IodClient:
    """Nonblocking read-through client; :meth:`pump` is the only driver.

    Construction takes every environmental dependency as a parameter, so a test
    can inject root credentials on a socketpair and a virtual clock without root,
    a manager, a daemon or a filesystem socket. Production passes the defaults.

    The client is intentionally single-threaded: one thread owns ``pump`` and
    ``query``. A caller running ``pump`` on a worker thread must marshal state
    changes back to the GUI thread through ``on_change``; no lock is held across a
    callback, and no method here blocks.
    """

    def __init__(
        self,
        socket_path: Path | str,
        *,
        peer_reader: Callable[[socket.socket], Peer] = read_peer,
        path_verifier: Callable[[Path], None] = _verify_production_paths,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        on_gap: Callable[[GapNotice], None],
        on_change: Callable[[], None],
        connector: Callable[[Path], socket.socket] | None = None,
        max_pending: int | None = None,
        cache_bytes: int | None = None,
        max_response_bytes: int | None = None,
    ) -> None:
        for name, value in (("peer_reader", peer_reader), ("path_verifier", path_verifier),
                            ("clock_ns", clock_ns), ("on_gap", on_gap), ("on_change", on_change)):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        if connector is not None and not callable(connector):
            raise TypeError("connector must be callable or None")
        self._socket_path = Path(socket_path)
        self._peer_reader = peer_reader
        self._path_verifier = path_verifier
        self._clock_ns = clock_ns
        self._on_gap = on_gap
        self._on_change = on_change
        self._connect_with = connector
        self.max_pending = _positive(max_pending, MAX_PENDING_REQUESTS, "max_pending")
        self.cache_bytes = _positive(cache_bytes, CACHE_BYTES, "cache_bytes")
        self.max_response_bytes = _positive(
            max_response_bytes, MAX_RESPONSE_BYTES, "max_response_bytes"
        )

        self._tracker = CursorTracker()
        self._state = ClientState()
        self._apps = AppRegistry()
        self._sock: socket.socket | None = None
        self._pending_sock: socket.socket | None = None
        self._pending_since: int = 0
        self._decoder: FrameDecoder | None = None
        self._out = bytearray()

        self._generation = 0
        self._sequence = 0
        self._backoff = 0
        self._next_attempt_ns = 0
        self._closed = False

        self._peer_verified = False
        self._status_ok = False
        self._subscribed = False
        self._daemon_answering = False
        self._pending_activation: bool | None = None
        self._capabilities: Capabilities | None = None
        self._last_snapshot_ts: int | None = None
        self._last_error: str | None = None
        self._status_request_id = ""
        self._subscribe_request_id = ""
        self._first_send_since_rx: int | None = None
        self._outage_gap_emitted = False

        self._pending: dict[str, _Pending] = {}
        self._cache: OrderedDict[tuple[str, str], _Cached] = OrderedDict()
        self._cached_bytes = 0
        self._gap_keys: OrderedDict[tuple[Any, ...], None] = OrderedDict()
        self._refused = 0
        self._duplicates = 0
        self._stale_results = 0
        self._hits = 0

    # -- diagnostics ------------------------------------------------------- #

    @property
    def state(self) -> ClientState:
        return self._state

    @property
    def generation(self) -> int:
        """Connection generation; it increases on every verified connect."""

        return self._generation

    @property
    def pending_requests(self) -> int:
        return len(self._pending)

    @property
    def cached_responses(self) -> int:
        return len(self._cache)

    @property
    def cached_bytes(self) -> int:
        return self._cached_bytes

    @property
    def refused_requests(self) -> int:
        return self._refused

    @property
    def duplicate_snapshots(self) -> int:
        return self._duplicates

    @property
    def stale_results(self) -> int:
        return self._stale_results

    @property
    def cache_hits(self) -> int:
        return self._hits

    # -- public operations ------------------------------------------------- #

    def connect(self) -> None:
        """Attempt a connection now, ignoring any pending backoff delay."""

        if self._closed:
            return
        self._backoff = 0
        self._next_attempt_ns = 0
        now = self._clock_ns()
        if self._sock is None:
            self._attempt(now)
        self._publish()

    def pump(self) -> None:
        """Drive I/O, deadlines and reconnection. Never blocks and never sleeps."""

        if self._closed:
            return
        now = self._clock_ns()
        self._expire(now)
        if self._pending_sock is not None:
            self._finish_pending(now)
        elif self._sock is None:
            if now >= self._next_attempt_ns:
                self._attempt(now)
        if self._sock is not None:
            self._flush()
            if self._sock is not None:
                self._read(now)
            if self._sock is not None:
                self._check_no_answer(now)
        self._publish()

    def query(self, request: QueryRequest) -> Future[dict]:
        """Send one historical selector; the future settles without blocking.

        Every outcome — cache hit, refusal, daemon error, timeout — is delivered
        through the returned future, so a Qt slot can connect to it and return.
        A malformed selector is a programming error and raises immediately.
        """

        future: Future[dict] = Future()
        # A22 is a local policy decision, not a daemon answer: a window the product has
        # disabled must report its ratified reason whether or not a daemon is reachable.
        # Checking connectivity first reported daemon_unavailable for a request that
        # could never be served, which is the wrong explanation and hides the disabled
        # control the UI is supposed to render.
        try:
            query = _query_wire(request)
        except WindowUnavailableError as error:
            future.set_exception(RequestRejected(error.reason, str(error)))
            return future
        except ProtocolError as error:
            future.set_exception(RequestRejected(error.reason, _bounded_detail(error)))
            return future
        if self._closed:
            future.set_exception(ClientClosed("client_closed"))
            return future
        if not (self._peer_verified and self._status_ok) or self._sock is None:
            future.set_exception(ClientUnavailable("daemon_unavailable"))
            return future
        now = self._clock_ns()
        self._expire(now)
        if len(self._pending) >= self.max_pending:
            self._refused += 1
            future.set_exception(RequestRejected("too_many_requests", f"at most {self.max_pending}"))
            return future
        # The key is the *complete* request plus the committed remote generation.
        # Presentation IDs never appear here, and a result read under an older
        # boot/epoch is unreachable rather than "still valid": its key can never
        # be reconstructed again, and the byte budget evicts the entry whole.
        key = (
            json.dumps(query, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            self._generation_token(),
        )
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self._hits += 1
            _settle(future, value=dict(cached.value))
            return future

        request_id = f"q{self._generation}-{self._sequence}"
        self._sequence += 1
        self._pending[request_id] = _Pending(
            future=future,
            key=key,
            generation=self._generation,
            deadline_ns=now + REQUEST_LIFETIME_NS,
        )
        self._send(
            {
                "protocol": 1,
                "type": "query",
                "request_id": request_id,
                "query": query,
            }
        )
        return future

    def close(self) -> None:
        """Stop the session: fail outstanding work and stop reconnecting."""

        if self._closed:
            return
        self._closed = True
        self._fail_pending(ClientClosed("client_closed"))
        self._discard(self._sock)
        self._sock = None
        self._decoder = None
        self._discard(self._pending_sock)
        self._pending_sock = None
        self._out.clear()
        self._peer_verified = False
        self._status_ok = False
        self._subscribed = False
        # The owner asked for this, so the link is not "failing": reporting the
        # last transport fault after a deliberate close would be a lie.
        self._last_error = None
        self._drop_cache()
        self._publish()

    # -- connection attempts ----------------------------------------------- #

    def _attempt(self, now: int) -> None:
        if self._closed or self._sock is not None or self._pending_sock is not None:
            return
        try:
            self._path_verifier(self._socket_path)
        except PeerRejected as error:
            self._failed(now, error.reason)
            return
        except (OSError, ValueError):
            self._failed(now, "path_unverifiable")
            return
        connector = self._connect_with or _production_connector
        try:
            sock = connector(self._socket_path)
        except PeerRejected as error:
            self._failed(now, error.reason)
            return
        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError):
            self._failed(now, "daemon_absent")
            return
        except OSError:
            self._failed(now, "connect_failed")
            return
        if not isinstance(sock, socket.socket):
            raise TypeError("the connector must return a socket")
        try:
            sock.setblocking(False)
        except OSError:
            self._discard(sock)
            self._failed(now, "connect_failed")
            return
        if not _connected(sock):
            self._pending_sock = sock
            self._pending_since = now
            self._finish_pending(now)
            return
        self._establish(sock, now)

    def _finish_pending(self, now: int) -> None:
        sock = self._pending_sock
        if sock is None:
            return
        if _connected(sock):
            self._pending_sock = None
            self._establish(sock, now)
            return
        if now - self._pending_since >= CONNECT_DEADLINE_NS:
            self._pending_sock = None
            self._discard(sock)
            self._failed(now, "connect_failed")
            return
        try:
            readable, writable, _ = select.select([sock], [sock], [], 0)
        except OSError:
            self._pending_sock = None
            self._discard(sock)
            self._failed(now, "connect_failed")
            return
        if readable or writable:
            code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if code:
                self._pending_sock = None
                self._discard(sock)
                self._failed(now, "daemon_absent" if code == errno.ECONNREFUSED else "connect_failed")

    def _establish(self, sock: socket.socket, now: int) -> None:
        try:
            peer = self._peer_reader(sock)
            require_root_server(peer)
        except PeerRejected as error:
            self._discard(sock)
            self._failed(now, error.reason)
            return
        except OSError:
            self._discard(sock)
            self._failed(now, "peer_unreadable")
            return
        self._sock = sock
        self._decoder = FrameDecoder(max_bytes=MAX_INBOUND)
        self._out.clear()
        self._generation += 1
        self._tracker.begin_connection()
        self._subscribed = False
        self._status_ok = False
        self._peer_verified = True
        self._first_send_since_rx = None
        self._status_request_id = f"st{self._generation}"
        self._subscribe_request_id = f"sb{self._generation}"
        self._send(
            {"protocol": 1, "type": "status", "request_id": self._status_request_id}
        )

    def _failed(self, now: int, reason: str) -> None:
        self._last_error = reason
        self._peer_verified = False
        self._status_ok = False
        self._schedule(now)

    def _schedule(self, now: int) -> None:
        rung = min(self._backoff, len(BACKOFF_SECONDS) - 1)
        self._backoff += 1
        self._next_attempt_ns = now + int(BACKOFF_SECONDS[rung] * NS)

    def _drop(self, now: int, reason: str) -> None:
        self._discard(self._sock)
        self._sock = None
        self._decoder = None
        self._out.clear()
        self._peer_verified = False
        self._status_ok = False
        self._subscribed = False
        self._first_send_since_rx = None
        self._last_error = reason
        self._fail_pending(ConnectionLost(reason))
        self._record_transport_gap(reason)
        self._schedule(now)

    @staticmethod
    def _discard(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            sock.close()
        except OSError:  # pragma: no cover - closing an already-closed socket
            pass

    # -- reading ----------------------------------------------------------- #

    def _read(self, now: int) -> None:
        sock = self._sock
        decoder = self._decoder
        if sock is None or decoder is None:
            return
        while self._sock is not None:
            try:
                data = sock.recv(READ_BUDGET)
            except BlockingIOError:
                return
            except OSError:
                self._drop(now, "connection_lost")
                return
            if not data:
                self._drop(now, "transport_lost")
                return
            self._first_send_since_rx = None
            try:
                frames = decoder.feed(data)
            except ProtocolError as error:
                # Framing damage poisons the stream: the next frame boundary is
                # unknown, so the connection cannot be trusted any further.
                self._drop(now, error.reason)
                return
            for frame in frames:
                if self._sock is None:
                    return
                self._frame(frame, now)

    def _frame(self, message: Mapping[str, Any], now: int) -> None:
        try:
            normalized = validate_server_message(dict(message))
        except ProtocolError as error:
            self._drop(now, error.reason)
            return
        kind = normalized["type"]
        if kind == "status":
            self._apply_status(normalized)
        elif kind == "snapshot":
            self._apply_snapshot(normalized)
        elif kind == "gap":
            self._apply_gap(normalized)
        elif kind == "result":
            self._apply_result(normalized)
        else:
            self._apply_error(normalized)

    def _apply_status(self, message: Mapping[str, Any]) -> None:
        if message.get("request_id") not in (self._status_request_id, self._subscribe_request_id):
            # A status that does not answer this connection's own request is not
            # evidence about this connection.
            self._stale_results += 1
            return
        self._tracker.accept_status(message)
        self._status_ok = True
        self._last_error = None
        self._daemon_answering = bool(message["answering"])
        self._pending_activation = bool(message["pending_activation"])
        self._backoff = 0
        self._outage_gap_emitted = False
        if not self._subscribed:
            self._subscribed = True
            self._send_subscribe()

    def _send_subscribe(self) -> None:
        resume = self._tracker.resume
        authority = self._tracker.authority
        if resume is not None and authority is not None and (resume.boot_id, resume.iod_epoch) != authority:
            # A cursor from another boot/epoch is not a resume point; asking for it
            # would claim continuity the client cannot prove.
            resume = None
        self._send(
            {
                "protocol": 1,
                "type": "subscribe",
                "request_id": self._subscribe_request_id,
                "since": None if resume is None else _cursor_wire(resume),
            }
        )

    def _apply_snapshot(self, message: Mapping[str, Any]) -> None:
        cursor = StreamCursor(message["boot_id"], message["iod_epoch"], message["seq"])
        if not self._tracker.accept(cursor):
            self._duplicates += 1
            return
        self._capabilities = message["capabilities"]
        self._last_snapshot_ts = message["ts_mono_ns"]

    def _apply_gap(self, message: Mapping[str, Any]) -> None:
        self._tracker.accept_gap(message)
        current = message.get("current")
        previous = message.get("previous")
        if isinstance(current, StreamCursor):
            seq = current.seq
        elif isinstance(previous, StreamCursor):
            seq = previous.seq
        else:  # pragma: no cover - a gap without any cursor identity is refused upstream
            seq = 0
        notice = GapNotice(
            boot_id=str(message["boot_id"]),
            iod_epoch=int(message["iod_epoch"]),
            seq=seq,
            from_mono_ns=message["from_mono_ns"],
            to_mono_ns=message["to_mono_ns"],
            reason=str(message["reason"]),
        )
        key = (notice.boot_id, notice.iod_epoch, notice.seq, notice.from_mono_ns,
               notice.to_mono_ns, notice.reason)
        if key in self._gap_keys:
            return
        self._gap_keys[key] = None
        if len(self._gap_keys) > MAX_GAP_RECORDS:
            self._gap_keys.popitem(last=False)
        self._on_gap(notice)

    def _apply_result(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("request_id")
        pending = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
        if pending is None:
            self._stale_results += 1
            return
        if pending.generation != self._generation:
            self._stale_results += 1
            _settle(pending.future, error=ConnectionLost("stale_connection"))
            return
        # Bound the decoded payload (the accounting part of the frame), so a
        # daemon that packs an oversized answer behind a small envelope is still
        # refused rather than cached.
        payload = message["payload"]
        try:
            size = len(
                json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
            )
        except (TypeError, ValueError):  # pragma: no cover - validated upstream
            size = self.max_response_bytes + 1
        if size > self.max_response_bytes:
            _settle(
                pending.future,
                error=RequestRejected("response_too_large", f"the result is {size} bytes"),
            )
            return
        kind = message["kind"]
        try:
            result = from_wire(payload, app_ids=self._apps.id_for, result_kind=kind)
        except (KeyError, TypeError, ValueError) as error:
            _settle(pending.future, error=RemoteError("invalid_result", _bounded_detail(error)))
            return
        meta = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else payload
        snapshot = meta.get("snapshot") if isinstance(meta, Mapping) else None
        value = {
            "kind": kind,
            "result": result,
            "snapshot": snapshot,
            "next_cursor": meta.get("next_cursor") if isinstance(meta, Mapping) else None,
        }
        self._cache_put(pending.key, value, size)
        _settle(pending.future, value=dict(value))

    def _apply_error(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("request_id")
        pending = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
        error = RemoteError(str(message["reason"]), str(message["message"]))
        if pending is not None:
            _settle(pending.future, error=error)
            return
        self._last_error = error.reason
        self._stale_results += 1

    # -- deadlines --------------------------------------------------------- #

    def _expire(self, now: int) -> None:
        expired = [
            (request_id, pending)
            for request_id, pending in self._pending.items()
            if now >= pending.deadline_ns
        ]
        for request_id, pending in expired:
            self._pending.pop(request_id, None)
            _settle(
                pending.future,
                error=RequestRejected(
                    "query_timeout", "the daemon did not answer within five seconds"
                ),
            )

    def _check_no_answer(self, now: int) -> None:
        since = self._first_send_since_rx
        if since is None or now - since < NO_ANSWER_NS:
            return
        self._drop(now, "daemon_not_answering")

    def _fail_pending(self, error: Exception) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for entry in pending:
            _settle(entry.future, error=_clone(error))

    # -- gap provenance ---------------------------------------------------- #

    def _record_transport_gap(self, reason: str) -> None:
        """Record one connection loss as provenance, with no invented bounds.

        The end of the outage is unknown until data resumes, so ``to_mono_ns``
        stays ``None``; a consumer that needs a closed interval must wait for the
        daemon's own ``gap`` frame rather than extrapolate one from here. A loss
        before any accepted sample carries the empty identity triple, because the
        client genuinely does not know which boot it was watching.
        """

        if self._outage_gap_emitted:
            return
        self._outage_gap_emitted = True
        cursor = self._tracker.resume
        self._on_gap(
            GapNotice(
                boot_id="" if cursor is None else cursor.boot_id,
                iod_epoch=0 if cursor is None else cursor.iod_epoch,
                seq=0 if cursor is None else cursor.seq,
                from_mono_ns=self._last_snapshot_ts,
                to_mono_ns=None,
                reason=reason,
            )
        )

    # -- cache ------------------------------------------------------------- #

    def _generation_token(self) -> str:
        authority = self._tracker.authority
        return "" if authority is None else f"{authority[0]}|{authority[1]}"

    def _cache_put(self, key: tuple[str, str], value: dict[str, Any], size: int) -> None:
        """Store one successful read-through answer, or nothing at all.

        Entries carry the decoded P1 records, so a cached hit still exposes the
        coverage overlay that travelled with the answer; failures are never
        cached, which is why a hit can never present an error or a missing file
        source as an empty-but-complete result.
        """

        if size > self.cache_bytes:
            return
        existing = self._cache.pop(key, None)
        if existing is not None:
            self._cached_bytes -= existing.size
        while self._cache and self._cached_bytes + size > self.cache_bytes:
            _, evicted = self._cache.popitem(last=False)
            self._cached_bytes -= evicted.size
        self._cache[key] = _Cached(value=value, size=size)
        self._cached_bytes += size

    def _drop_cache(self) -> None:
        self._cache.clear()
        self._cached_bytes = 0

    # -- writing ----------------------------------------------------------- #

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._sock is None:
            return
        self._out.extend(encode_frame(dict(message), max_bytes=MAX_OUTBOUND))
        if self._first_send_since_rx is None:
            self._first_send_since_rx = self._clock_ns()
        self._flush()

    def _flush(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while self._out:
            try:
                sent = sock.send(self._out)
            except BlockingIOError:
                return
            except OSError:
                self._drop(self._clock_ns(), "connection_lost")
                return
            if sent <= 0:  # pragma: no cover - zero-length send cannot make progress
                return
            del self._out[:sent]

    # -- state publication -------------------------------------------------- #

    def _publish(self) -> None:
        verified = bool(self._peer_verified and self._status_ok and self._sock is not None)
        state = ClientState(
            verified=verified,
            answering=bool(verified and self._daemon_answering),
            cursor=self._tracker.current,
            capabilities=self._capabilities,
            pending_activation=self._pending_activation,
            last_error=self._last_error,
        )
        if state != self._state:
            self._state = state
            self._on_change()


def _settle(future: Future, *, value: dict | None = None, error: Exception | None = None) -> None:
    """Settle a caller future unless it is already done.

    A GUI may cancel a query when the operator navigates away. ``Future.set_result``
    on a cancelled future raises ``InvalidStateError``, which would escape from
    ``pump`` and break the single-threaded driver, so a caller that has given up is
    simply left alone while the answer is still cached for the next identical read.
    """

    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)


def _cursor_wire(cursor: StreamCursor) -> dict[str, Any]:
    return {"boot_id": cursor.boot_id, "iod_epoch": cursor.iod_epoch, "seq": cursor.seq}


def _positive(value: int | None, default: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _clone(error: Exception) -> Exception:
    """Give every waiter its own exception instance, so tracebacks do not merge."""

    clone = type(error)(error.reason, error.message) if isinstance(error, IodClientError) else error
    return clone