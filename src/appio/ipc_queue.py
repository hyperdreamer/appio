"""Bounded per-client egress queue for the AppIO daemon (design §4.2, D4).

A connected client is not a fast client. The daemon samples and commits on a
schedule, so it can never afford to block on one peer's receive window:

* a frame that would exceed :data:`MAX_QUEUED_BYTES` becomes a disconnect, not a
  wait, so a stalled reader cannot stall collection;
* the budget covers **every** frame type — status, snapshots, query results and
  errors — so error reporting is not an escape hatch around the memory bound;
* a snapshot stays counted as pending until its *complete* frame has drained, and
  a second still-pending snapshot disconnects that client, which is the design's
  explicit slow-client rule.

Accounting is byte-exact against the count a nonblocking ``send`` actually
accepted, because a short write is normal on a socket. The queue never copies a
frame after enqueue: :meth:`ClientQueue.peek` returns a ``memoryview`` over the
immutable frame at the current offset, so the daemon's send path allocates
nothing per turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, NoReturn

from .ipc_models import StreamCursor

__all__ = [
    "ClientQueue",
    "MAX_PENDING_SNAPSHOTS",
    "MAX_QUEUED_BYTES",
    "SlowClientError",
]

MAX_QUEUED_BYTES: Final = 512 * 1024
"""D4: at most 512 KiB of egress may be waiting for one client."""

MAX_PENDING_SNAPSHOTS: Final = 2
"""D4: the pending count at which a new snapshot disconnects the client.

With this default, one snapshot may be outstanding; enqueueing the second one
while the first is still unread disconnects ("two consecutive logical snapshots
still pending transmission").
"""


class SlowClientError(RuntimeError):
    """One client was disconnected for refusing to keep up.

    ``reason`` is the stable token the daemon logs (``queue_budget``,
    ``slow_client``, ``queue_closed``); ``str(error)`` is bounded detail that
    always contains it.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        if not isinstance(reason, str) or reason == "":
            raise ValueError("reason must be a non-empty string")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("detail must be a string or None")
        text = reason if detail is None else f"{reason}: {detail}"
        super().__init__(text)
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class _Entry:
    """One enqueued frame plus how much of it a nonblocking send has accepted."""

    frame: bytes
    snapshot: StreamCursor | None = None
    offset: int = field(default=0)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


class ClientQueue:
    """A bounded, nonblocking, per-client egress buffer.

    Every method either succeeds in place or disconnects this one client by
    raising :class:`SlowClientError`. Nothing here touches another client's state,
    the collector, or the database.
    """

    __slots__ = (
        "_closed",
        "_entries",
        "_max_bytes",
        "_max_pending_snapshots",
        "_pending_snapshots",
        "_queued_bytes",
    )

    def __init__(
        self,
        max_bytes: int = MAX_QUEUED_BYTES,
        max_pending_snapshots: int = MAX_PENDING_SNAPSHOTS,
    ) -> None:
        """
        ``max_pending_snapshots`` is the **disconnect threshold**, not the number
        of snapshots that may be pending at once. Enqueueing a snapshot whose
        pending count would reach the threshold disconnects the client, so the
        default of 2 means "one snapshot may be pending; enqueueing the second one
        while the first is still unread disconnects". That is the plan's
        ``pending_snapshots + 1 >= 2`` rule with the bound factored out. Values
        below 2 are rejected because they would permit no streaming at all.
        """

        self._max_bytes = _positive_int(max_bytes, "max_bytes")
        self._max_pending_snapshots = _positive_int(
            max_pending_snapshots, "max_pending_snapshots"
        )
        if self._max_pending_snapshots < 2:
            # A limit of one would disconnect on the first snapshot, i.e. permit no
            # streaming at all. That is a misconfiguration, not a policy.
            raise ValueError("max_pending_snapshots must be at least 2")
        self._entries: list[_Entry] = []
        self._queued_bytes = 0
        self._pending_snapshots = 0
        self._closed = False

    # -- inspection ------------------------------------------------------- #

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def max_pending_snapshots(self) -> int:
        return self._max_pending_snapshots

    @property
    def queued_bytes(self) -> int:
        """Bytes still waiting for the socket, including unread frame prefixes."""

        return self._queued_bytes

    @property
    def closed(self) -> bool:
        """True once this client has been disconnected; the queue is then inert."""

        return self._closed

    @property
    def pending_snapshots(self) -> int:
        """Snapshot frames enqueued but not yet completely drained."""

        return self._pending_snapshots

    @property
    def pending_cursors(self) -> tuple[StreamCursor, ...]:
        """The cursors of the snapshots still pending, oldest first (diagnostics)."""

        return tuple(
            entry.snapshot for entry in self._entries if entry.snapshot is not None
        )

    # -- producer side ---------------------------------------------------- #

    def push(self, frame: bytes, *, snapshot: StreamCursor | None = None) -> None:
        """Enqueue one already-encoded frame.

        Raises :class:`SlowClientError` and disconnects this client when the frame
        would exceed the byte budget or when this snapshot would be one snapshot
        too many still pending. A zero-length or non-bytes frame is a programming
        error (:class:`TypeError` / :class:`ValueError`), not a client fault.
        """

        if self._closed:
            raise SlowClientError("queue_closed", "the client was already disconnected")
        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise TypeError("frame must be bytes-like")
        if not isinstance(snapshot, (StreamCursor, type(None))):
            raise TypeError("snapshot must be a StreamCursor or None")
        data = bytes(frame)
        if not data:
            raise ValueError("frame must not be empty")

        if self._queued_bytes + len(data) > self._max_bytes:
            self.disconnect(
                "queue_budget",
                f"{self._queued_bytes + len(data)} bytes exceeds the {self._max_bytes}-byte budget",
            )

        if snapshot is not None and self._pending_snapshots + 1 >= self._max_pending_snapshots:
            self.disconnect(
                "slow_client",
                f"snapshot would be pending snapshot "
                f"{self._pending_snapshots + 1} of {self._max_pending_snapshots}",
            )

        self._entries.append(_Entry(frame=data, snapshot=snapshot))
        self._queued_bytes += len(data)
        if snapshot is not None:
            self._pending_snapshots += 1

    # -- consumer side ---------------------------------------------------- #

    def peek(self, max_bytes: int) -> memoryview:
        """A view of up to ``max_bytes`` of the head frame, without consuming it.

        The view reads the immutable frame in place starting at the current send
        offset, so the caller can hand it straight to a nonblocking ``send``. A
        closed or empty queue yields an empty view rather than raising, because
        the disconnect path already reported the reason.
        """

        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        if self._closed or not self._entries:
            return memoryview(b"")
        head = self._entries[0]
        return memoryview(head.frame)[head.offset : head.offset + max_bytes]

    def consume(self, sent: int) -> None:
        """Advance the queue by the number of bytes a send actually accepted.

        Consuming past the queued bytes would fabricate delivery, so it is a
        :class:`ValueError` (a caller bug) rather than a silent clamp. A snapshot's
        pending count drops only when its final byte is consumed.
        """

        if isinstance(sent, bool) or not isinstance(sent, int):
            raise ValueError("sent must be an integer")
        if sent < 0:
            raise ValueError("sent must not be negative")
        if sent > self._queued_bytes:
            raise ValueError(
                f"sent {sent} exceeds the {self._queued_bytes} bytes still queued"
            )
        if sent == 0:
            return

        remaining = sent
        while remaining:
            head = self._entries[0]
            available = len(head.frame) - head.offset
            step = available if available <= remaining else remaining
            head.offset += step
            remaining -= step
            self._queued_bytes -= step
            if head.offset == len(head.frame):
                self._entries.pop(0)
                if head.snapshot is not None:
                    # Only a *complete* frame clears the pending count; a partial
                    # write leaves the client behind and still counted.
                    self._pending_snapshots -= 1

    # -- disconnect ------------------------------------------------------- #

    def disconnect(self, reason: str, detail: str | None = None) -> NoReturn:
        """Clear this client's retained frames and raise :class:`SlowClientError`.

        Only this queue's state is touched: no shared buffer, collector or other
        client is affected. The reason is preserved on the raised error so the
        daemon can log a bounded, specific refusal instead of a generic close.
        """

        error = SlowClientError(reason, detail)
        self._entries.clear()
        self._queued_bytes = 0
        self._pending_snapshots = 0
        self._closed = True
        raise error

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"ClientQueue(queued_bytes={self._queued_bytes}, "
            f"pending_snapshots={self._pending_snapshots}, closed={self._closed})"
        )
