"""Collector epoch allocation and high-water frame receipts (A08, section 3.4).

A stored observation is namespaced by ``(boot_id, source, epoch, seq)``. Each
collector process start allocates an epoch transactionally -- ``previous + 1``
for the same boot, otherwise ``1`` -- and resets ``last_seq`` to ``NULL``.
A frame is accepted only while its source, boot and epoch are the active ones
and its sequence is above the committed high-water mark; anything else is a
duplicate or stale frame and is rejected **before** the caller mutates a
baseline, row or total.

Both functions use a SQLite savepoint rather than a bare ``BEGIN``. Used on its
own, releasing the outermost savepoint commits the change; called inside the
frame transaction that the store owns (Task 8), releasing the inner savepoint
leaves that caller's transaction open, so the receipt and the frame's rows,
totals, file-hour state and ``sampler_checkpoint`` still commit or roll back
together. On any failure the savepoint is rolled back and the error re-raised.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import dataclasses
import sqlite3

from .contracts import validate_sqlite_integer

__all__ = [
    "CollectorEpoch",
    "FrameIdentity",
    "accept_frame",
    "allocate_epoch",
]

SOURCE_VALUES: tuple[str, ...] = ("gui", "iod")
"""Reserved P1 collector sources from the ratified stream identity."""


def _source(value: object, field: str) -> str:
    if not isinstance(value, str) or value not in SOURCE_VALUES:
        raise ValueError(f"{field} must be one of {', '.join(SOURCE_VALUES)}, got {value!r}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _integer(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer, got {type(value).__name__}")
    number = validate_sqlite_integer(value, field)
    if positive and number == 0:
        raise ValueError(f"{field} must be positive, got 0")
    return number


@contextmanager
def _savepoint(connection: sqlite3.Connection, name: str) -> Iterator[None]:
    """Run one unit of work atomically, joining a caller's transaction if open."""

    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {name}")
        connection.execute(f"RELEASE {name}")
        raise
    else:
        connection.execute(f"RELEASE {name}")


@dataclasses.dataclass(frozen=True)
class CollectorEpoch:
    """The active epoch row for one source, as allocated by this module."""

    source: str
    boot_id: str
    epoch: int
    last_seq: int | None
    allocated_mono_ns: int

    def __post_init__(self) -> None:
        _source(self.source, "collector_epoch.source")
        _text(self.boot_id, "collector_epoch.boot_id")
        _integer(self.epoch, "collector_epoch.epoch", positive=True)
        if self.last_seq is not None:
            _integer(self.last_seq, "collector_epoch.last_seq")
        _integer(self.allocated_mono_ns, "collector_epoch.allocated_mono_ns")


@dataclasses.dataclass(frozen=True)
class FrameIdentity:
    """The ``(boot_id, source, epoch, seq)`` identity of one frame."""

    boot_id: str
    source: str
    epoch: int
    seq: int

    def __post_init__(self) -> None:
        _text(self.boot_id, "frame.boot_id")
        _source(self.source, "frame.source")
        _integer(self.epoch, "frame.epoch", positive=True)
        _integer(self.seq, "frame.seq")


def _active_row(
    connection: sqlite3.Connection, source: str
) -> tuple[str, int, int | None] | None:
    row = connection.execute(
        "SELECT boot_id, epoch, last_seq FROM collector_epoch WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1], row[2])


def allocate_epoch(
    connection: sqlite3.Connection,
    source: str,
    boot_id: str,
    allocated_mono_ns: int,
) -> CollectorEpoch:
    """Allocate the next epoch for ``source`` and clear the high-water mark.

    ``previous + 1`` for the same boot, ``1`` for a new boot or the first start.
    The boot row must already exist: boot chronology is owned by the store, and
    the ``collector_epoch`` foreign key rejects an unknown boot here.
    """

    source = _source(source, "source")
    boot_id = _text(boot_id, "boot_id")
    allocated_mono_ns = _integer(allocated_mono_ns, "allocated_mono_ns")

    with _savepoint(connection, "appio_allocate_epoch"):
        active = _active_row(connection, source)
        if active is None or active[0] != boot_id:
            epoch = 1
        else:
            epoch = _integer(
                active[1] + 1, "collector_epoch.epoch", positive=True
            )
        connection.execute(
            "INSERT INTO collector_epoch "
            "(source, boot_id, epoch, last_seq, allocated_mono_ns) "
            "VALUES (?, ?, ?, NULL, ?) "
            "ON CONFLICT (source) DO UPDATE SET "
            "boot_id = excluded.boot_id, epoch = excluded.epoch, "
            "last_seq = NULL, allocated_mono_ns = excluded.allocated_mono_ns",
            (source, boot_id, epoch, allocated_mono_ns),
        )
    return CollectorEpoch(source, boot_id, epoch, None, allocated_mono_ns)


def accept_frame(connection: sqlite3.Connection, frame: FrameIdentity) -> bool:
    """Commit one frame's sequence receipt, or refuse it as duplicate/stale.

    Returns ``True`` when ``frame.seq`` advanced the high-water mark for the
    active ``(source, boot, epoch)``, ``False`` when the source has no active
    epoch, the frame belongs to another boot or epoch, or the sequence is at or
    below the committed high-water mark. A ``False`` result performs no write:
    the caller must not have staged a baseline before calling this.
    """

    if not isinstance(frame, FrameIdentity):
        raise TypeError("accept_frame requires a FrameIdentity")
    advanced = False
    with _savepoint(connection, "appio_accept_frame"):
        active = _active_row(connection, frame.source)
        if (
            active is not None
            and active[0] == frame.boot_id
            and active[1] == frame.epoch
            and (active[2] is None or frame.seq > active[2])
        ):
            cursor = connection.execute(
                "UPDATE collector_epoch SET last_seq = ? "
                "WHERE source = ? AND boot_id = ? AND epoch = ? "
                "AND (last_seq IS NULL OR last_seq < ?)",
                (
                    frame.seq,
                    frame.source,
                    frame.boot_id,
                    frame.epoch,
                    frame.seq,
                ),
            )
            advanced = cursor.rowcount == 1
    return advanced
