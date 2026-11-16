"""Real client/socketpair transport fixture for the live Overview lane.

The harness drives a production :class:`appio.iodclient.IodClient` across a real
AF_UNIX ``socketpair``. Peer credentials and path metadata are injected exactly
as the existing client tests do, so no daemon, listener, root credential,
service invocation or sleep is involved.

Every historical reply is encoded with :func:`appio.iod_history.to_wire` and the
shared protocol framing and is delivered through the real peer socket. The
client therefore exercises its real normalization, correlation, decode and cache
path; ``reply`` never calls a private result setter.

The peer also models its half of the transport slot: each received query opens a
held historical job that stays held until the harness answers it, an error
arrives, or the socket is detached. ``observed_historical_occupants`` samples
that peer-side occupancy after each public operation, which is what makes an
expired-but-still-held request observable.

Like the rest of the lane's fixtures this module is Qt-free: it imports the
client, the wire codec and the clock only.
"""

from __future__ import annotations

import select
import socket
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from appio.history_types import RemotePosition
from appio.iod_history import to_wire
from appio.iodclient import IodClient
from appio.ipc_models import QueryRequest
from appio.ipc_protocol import FrameDecoder, ProtocolError, encode_frame
from appio.ipc_security import Peer
from live_overview_support import Clock

__all__ = ["QueryTransportHarness", "SNAPSHOT"]

SNAPSHOT = "a" * 64
"""A valid 64-lowercase-hex accepted identity for scripted peer answers."""

NS = 1_000_000_000

_MISSING = object()

#: A real snapshot frame is large; the scripted peer answers every kind with the
#: same canonical envelope, and `validate_server_message` only needs these keys.
_CAPABILITIES = {
    "proc_all": True,
    "dev_stat": True,
    "file_io": True,
    "bpf_loaded": True,
}


def _status_frame(*, request_id: str, cursor: RemotePosition | None) -> dict[str, Any]:
    """One validated status envelope advertising the committed cursor."""

    return {
        "protocol": 1,
        "type": "status",
        "request_id": request_id,
        "enabled": True,
        "answering": True,
        "pending_activation": False,
        "ownership_start_mono_ns": None,
        "cursor": None
        if cursor is None
        else {
            "boot_id": cursor.boot_id,
            "iod_epoch": cursor.iod_epoch,
            "seq": cursor.seq,
        },
    }


def _snapshot_frame(seq: int, *, boot_id: str, epoch: int) -> dict[str, Any]:
    """One validated snapshot envelope that advances the accepted cursor."""

    return {
        "protocol": 1,
        "type": "snapshot",
        "boot_id": boot_id,
        "iod_epoch": epoch,
        "seq": seq,
        "ts_mono_ns": 1_000 + seq,
        "ts_wall": 1_700_000_000.5 + seq,
        "source": "iod",
        "capabilities": dict(_CAPABILITIES),
        "procs": [],
        "devs": [],
        "files": [],
        "coverage": {},
    }


def _error_frame(request_id: str, reason: str, message: str) -> dict[str, Any]:
    return {
        "protocol": 1,
        "type": "error",
        "request_id": request_id,
        "reason": reason,
        "message": message,
    }


class QueryTransportHarness:
    """Drive :class:`IodClient` against a scripted, real protocol-1 peer.

    ``cursor`` is the committed position the peer advertises in its status
    reply; ``None`` models a proven-but-cursor-less connection.
    """

    def __init__(
        self,
        *,
        boot_id: str = "boot-a",
        epoch: int = 7,
        cursor: object = _MISSING,
        clock: Clock | None = None,
        **client_options: Any,
    ) -> None:
        self.clock = Clock(ns=10 * NS) if clock is None else clock
        self.boot_id = boot_id
        self.epoch = epoch
        self.cursor = (
            RemotePosition(boot_id, epoch, 3) if cursor is _MISSING else cursor  # type: ignore[arg-type]
        )
        self.gaps: list[object] = []
        self.changes = 0
        self.servers: list[socket.socket] = []
        self.decoders: list[FrameDecoder] = []
        self.received: list[dict[str, Any]] = []
        self.query_frames: list[dict[str, Any]] = []
        self.query_kinds: list[str] = []
        self.connection_generations: list[int] = []
        self.observed_historical_occupants: list[int] = []
        self.subjects: dict[int, str] = {}
        self._jobs: dict[str, str] = {}
        self._futures: dict[str, Future[dict]] = {}
        self._peer_closed = False
        self.client = IodClient(
            Path("/run/appio/iod.sock"),
            peer_reader=self._peer,
            path_verifier=self._verify,
            clock_ns=self.clock,
            on_gap=self.gaps.append,
            on_change=self._changed,
            connector=self._connect,
            **client_options,
        )
        self.connect()

    # -- injected client seams --------------------------------------------- #

    def _changed(self) -> None:
        self.changes += 1

    @staticmethod
    def _verify(path: Path) -> None:
        # A verified role never needs host metadata on a socketpair fixture.
        return None

    def _peer(self, sock: socket.socket) -> Peer:
        return Peer(pid=4321, uid=0, gid=0)

    def _connect(self, path: Path) -> socket.socket:
        client_end, server_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client_end.setblocking(False)
        server_end.settimeout(2.0)
        self.servers.append(server_end)
        self.decoders.append(FrameDecoder())
        self._peer_closed = False
        return client_end

    # -- scripted peer ------------------------------------------------------ #

    @property
    def server(self) -> socket.socket:
        return self.servers[-1]

    def connect(self, *, cursor: object = _MISSING) -> None:
        """Open one verified connection and complete status+subscribe."""

        if cursor is not _MISSING:
            self.cursor = cursor  # type: ignore[assignment]
        self.client.connect()
        self._drain()
        status = self._last_frame("status")
        assert status is not None, "the client must request status on a new connection"
        self.server.sendall(
            encode_frame(_status_frame(request_id=status["request_id"], cursor=self.cursor))  # type: ignore[arg-type]
        )
        self.pump()
        assert self._last_frame("subscribe") is not None, "the client must subscribe"
        self.connection_generations.append(self.client.generation)

    def _last_frame(self, kind: str) -> dict[str, Any] | None:
        for frame in reversed(self.received):
            if frame.get("type") == kind:
                return frame
        return None

    def _drain(self) -> None:
        """Read every frame the client has already written to the peer."""

        if self._peer_closed:
            return
        server = self.server
        while True:
            try:
                ready, _, _ = select.select([server], [], [], 0)
            except (OSError, ValueError):  # pragma: no cover - closed peer fixture
                return
            if not ready:
                return
            try:
                data = server.recv(65536)
            except (BlockingIOError, InterruptedError, OSError):
                return
            if not data:  # pragma: no cover - only after an unexpected detach
                self._peer_closed = True
                return
            try:
                frames = self.decoders[-1].feed(data)
            except ProtocolError:  # pragma: no cover - the client never sends garbage
                return
            for frame in frames:
                self.received.append(frame)
                if frame.get("type") == "query":
                    request_id = frame.get("request_id")
                    self.query_frames.append(frame)
                    self.query_kinds.append(frame["query"]["kind"])
                    if isinstance(request_id, str):
                        self._jobs[request_id] = frame["query"]["kind"]

    # -- public test surface ------------------------------------------------ #

    def query(self, request: QueryRequest) -> Future[dict]:
        """Issue one historical selector through the real client."""

        future = self.client.query(request)
        self._drain()
        state = self.client.query_transport(future)
        if state.request_id is not None:
            self._futures[state.request_id] = future
        self._observe()
        return future

    def only_future(self, kind: str) -> Future[dict]:
        """The single currently held wire Future for one query kind."""

        held = [request_id for request_id, job_kind in self._jobs.items() if job_kind == kind]
        assert len(held) == 1, f"expected exactly one held {kind} job, saw {held}"
        return self._futures[held[0]]

    def reply(
        self,
        future: Future[dict],
        value: object,
        *,
        snapshot: str,
        next_cursor: str | None = None,
    ) -> None:
        """Answer one held Future with a real encoded wire payload."""

        state = self.client.query_transport(future)
        request_id = state.request_id
        assert request_id is not None and request_id in self._jobs, (
            "reply requires a currently held wire request"
        )
        request = state.request
        message = {
            "protocol": 1,
            "type": "result",
            "request_id": request_id,
            "kind": request.kind,
            "payload": self._payload(request, value, snapshot=snapshot, next_cursor=next_cursor),
        }
        self.server.sendall(encode_frame(message))
        self._jobs.pop(request_id, None)
        self.pump()

    def error(self, future: Future[dict], reason: str, text: str) -> None:
        """Answer one held Future with a real correlated error envelope."""

        state = self.client.query_transport(future)
        request_id = state.request_id
        assert request_id is not None and request_id in self._jobs, (
            "error requires a currently held wire request"
        )
        self.server.sendall(encode_frame(_error_frame(request_id, reason, text)))
        self._jobs.pop(request_id, None)
        self.pump()

    def pump(self) -> None:
        """Drive the client once and observe peer-sided occupancy."""

        self.client.pump()
        self._drain()
        self._observe()

    def advance_ns(self, delta: int) -> None:
        self.clock.advance_ns(delta)

    def stream(self, seq: int) -> None:
        """Deliver one peer snapshot that advances the accepted cursor."""

        self.server.sendall(
            encode_frame(_snapshot_frame(seq, boot_id=self.boot_id, epoch=self.epoch))
        )
        self.pump()

    def disconnect(self) -> None:
        """Detach the peer socket, as a daemon process death would."""

        self._jobs.clear()
        self._peer_closed = True
        try:
            self.server.close()
        except OSError:  # pragma: no cover - already-closed fixture socket
            pass
        self.pump()

    def register_subject(self, app_id: int, app_key: str) -> None:
        """The peer's own app table: encode ``app_id`` under ``app_key``."""

        self.subjects[app_id] = app_key

    def inject_stale_cached_answer(
        self,
        request: QueryRequest,
        value: object,
        *,
        answered_source: RemotePosition | None,
        snapshot: str = SNAPSHOT,
    ) -> None:
        """Fault injection: plant a cache entry under today's cursor key.

        The planted entry claims ``answered_source`` (typically an older cursor),
        reproducing a structural older-cache hit that the client's provenance
        revalidation must refuse rather than certify.
        """

        response: dict[str, Any] = {
            "kind": request.kind,
            "result": value,
            "snapshot": snapshot,
            "next_cursor": None,
        }
        if request.app_key is not None:
            response["app_key"] = request.app_key
        # Imported lazily so the fixture describes the production seam only
        # once the client provides it.
        from appio.iodclient import _cache_key, _query_wire

        key = _cache_key(_query_wire(request), self.client._current_source())
        self.client._cache_put(  # noqa: SLF001 - deliberate cache fault injection
            key,
            response,
            1024,
            request=request,
            answered_source=answered_source,
        )

    # -- payload encoding --------------------------------------------------- #

    def _wire_keys(self, request: QueryRequest, value: object) -> dict[int, str]:
        keys = dict(self.subjects)
        if isinstance(value, (list, tuple)):
            for row in value:
                app_id = getattr(row, "app_id", None)
                if app_id is not None:
                    keys.setdefault(app_id, getattr(row, "app_key", request.app_key))
        else:
            app_id = getattr(value, "app_id", None)
            if app_id is not None and request.app_key is not None:
                keys.setdefault(app_id, request.app_key)
        return keys

    def _payload(
        self,
        request: QueryRequest,
        value: object,
        *,
        snapshot: str,
        next_cursor: str | None,
    ) -> object:
        wire = to_wire(value, app_keys=self._wire_keys(request, value))
        if request.kind == "ranking":
            return [{"rows": wire, "next_cursor": next_cursor, "snapshot": snapshot}]
        if request.kind in ("series", "files"):
            page: dict[str, Any] = {"value": wire, "snapshot": snapshot}
            if next_cursor is not None:
                page["next_cursor"] = next_cursor
            return [page]
        return {"value": wire, "snapshot": snapshot}

    # -- observation -------------------------------------------------------- #

    def _observe(self) -> None:
        self.observed_historical_occupants.append(len(self._jobs))
