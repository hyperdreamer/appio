"""Rootless drain adapter for the P4 per-file eBPF producer.

The privileged half lives in ``appio_iod/bpf/``: ``fileio_loader.c`` owns the BPF
object, the attach, and the double-buffered maps, and speaks a fixed-width
command stream on its stdin/stdout. This module is the other end of that stream.
It performs no privileged operation of its own -- no ``bpf()`` syscall, no
``/proc`` or ``/sys`` read, no process spawn -- so it can be exercised end to end
from a synthetic frame vector on an unprivileged test host.

A drain is acknowledged, and only then returned, when the loader has quiesced
the in-flight counter, enumerated every retired-slot key, and cleared that slot.
Anything less -- a refused flip, a quiescence timeout, a failed clear, a record
that cannot be decoded inside the durable range -- raises :class:`FileSourceError`
and marks the source unavailable. The caller therefore records an *unmeasured*
file interval, never a zero one, and never a partial set of records presented as
a complete drain.

Protocol notes that are not obvious from ``fileio_protocol.h``:

* Every response starts with ``abi_version:u16, status:u16``. A rejected command
  (unsupported ABI, bad payload length) is answered with the bare 12-byte ack
  from the loader's command loop; a drain that fails *inside* ``drain()`` is
  answered with the 52-byte drain response, which echoes ``(epoch, seq)``. When
  the reported ABI is not ours the frame is treated as a refusal: the loader
  never ran our command, so nothing after the prefix can be trusted.
* ``cgroup_id == 0`` (``FILEIO_CGROUP_UNKNOWN``) is the loader's reserved
  unknown-owner overflow bucket. It may appear in a stats record and never in a
  file record, because an event with no owner must not become an app row.
* The 40-byte name field is the producer's truncation cap, so ``name_len == 40``
  is a truncated name, not a violation. ``name_len == 0`` is what the BPF program
  writes when the dentry was already gone; this layer keeps the bytes it was
  given and leaves weak-identity accounting to attribution.
"""

from __future__ import annotations

import errno as errno_module
import os
import re
import struct
from dataclasses import dataclass, fields
from typing import BinaryIO, Protocol, Sequence, runtime_checkable

from appio.contracts import HookHits, validate_sqlite_integer
from appio.durable import Receipt

# ---------------------------------------------------------------------------
# Wire constants, copied from appio_iod/bpf/fileio_protocol.h
# ---------------------------------------------------------------------------

ABI_VERSION = 1
NAME_CAP = 40
MAX_MAP_ENTRIES = 262144
RESERVED_CGROUP_ID = 0
HOOK_MASK_ALL = 0x3F
DIAGNOSTIC_CAP = 512
DRAIN_COMPLETE = 1

#: ``file.generation`` is persisted as a 32-bit value (durable ``FileEntry``).
MAX_GENERATION = 2**32 - 1
#: ``major()``/``minor()`` are stored in ``__u32`` on the wire.
MAX_DEV_PART = 2**32 - 1

OP_HELLO = 1
OP_FLIP_DRAIN = 2
OP_CLOSE = 3

STATUS_OK = 0
STATUS_ERROR = 1

COMMAND_HEADER = struct.Struct("<HHI")
FLIP_REQUEST = struct.Struct("<QQ")
HELLO_RESPONSE = struct.Struct("<HHIIiI")
ACK_RESPONSE = struct.Struct("<HHiI")
DRAIN_RESPONSE = struct.Struct("<HHIIiIQQIIII")
FILE_RECORD = struct.Struct("<QIIQIQQQQI40s")
CGROUP_STATS = struct.Struct("<QQQQQQQQQQQQ")

#: Hook bit order, aligned with the six ``fileio_vfs_*`` programs and with the
#: ``HookHits`` field order that the durable coverage object persists.
HOOK_NAMES: tuple[str, ...] = tuple(field.name for field in fields(HookHits))

_DEV = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z")

# Index positions inside one unpacked DRAIN_RESPONSE, named once so the header
# offsets are not re-derived at each use.
_DRAIN_EVENT_COUNT = 8
_DRAIN_STATS_COUNT = 9
_DRAIN_DIAGNOSTIC_LEN = 11
_HELLO_VERIFIER_LEN = 5
_ACK_DIAGNOSTIC_LEN = 3


# ---------------------------------------------------------------------------
# Errors and availability
# ---------------------------------------------------------------------------


def _errno_text(value: int) -> str:
    return os.strerror(-value if value < 0 else value)


def _bounded(text: str) -> str:
    """Bound administrator-facing text without splitting a code point."""
    if len(text) <= DIAGNOSTIC_CAP:
        return text
    return f"{text[: DIAGNOSTIC_CAP - 1]}\u2026"


class FileSourceError(Exception):
    """A drain that cannot be trusted as a measurement.

    ``errno`` and ``verifier`` carry the loader's own report so a bounded
    administrator message never loses the cause: ``str()`` is truncated to
    :data:`DIAGNOSTIC_CAP`, the attributes are not.
    """

    def __init__(self, errno: int | None, verifier: str | None, detail: str) -> None:
        if errno is not None and (isinstance(errno, bool) or not isinstance(errno, int)):
            raise ValueError(f"errno must be an integer or None, got {type(errno).__name__}")
        if verifier is not None:
            if not isinstance(verifier, str):
                raise ValueError(
                    f"verifier must be a string or None, got {type(verifier).__name__}"
                )
            if len(verifier) > DIAGNOSTIC_CAP:
                raise ValueError(
                    f"verifier exceeds the {DIAGNOSTIC_CAP}-character bound: {len(verifier)}"
                )
        if not isinstance(detail, str) or detail == "":
            raise ValueError("detail must be a non-empty string")
        self.errno = errno
        self.verifier = verifier
        self.detail = detail
        parts: list[str] = []
        if errno is not None:
            parts.append(f"errno {errno} ({_errno_text(errno)})")
        parts.append(detail)
        message = ": ".join(parts)
        if verifier:
            message = f"{message}: {verifier}"
        super().__init__(_bounded(message))


class LoaderFrameError(Exception):
    """The loader's response frame could not be delimited or read.

    Raised by the transport (:class:`StreamLoader`), never by the decoder: a
    declared payload that cannot exist is a framing fault, not a drain result.
    """


@dataclass(frozen=True)
class FileAvailability:
    """What the daemon may advertise about F, plus a bounded reason."""

    file_io: bool
    bpf_loaded: bool
    diagnostic: str | None

    def __post_init__(self) -> None:
        for name in ("file_io", "bpf_loaded"):
            if not isinstance(getattr(self, name), bool):
                observed = type(getattr(self, name)).__name__
                raise ValueError(
                    f"file_availability.{name} must be a bool, got {observed}"
                )
        if self.diagnostic is not None:
            if not isinstance(self.diagnostic, str):
                raise ValueError("file_availability.diagnostic must be a string or None")
            if len(self.diagnostic) > DIAGNOSTIC_CAP:
                raise ValueError(
                    "file_availability.diagnostic exceeds the "
                    f"{DIAGNOSTIC_CAP}-character bound: {len(self.diagnostic)}"
                )


# ---------------------------------------------------------------------------
# Decoded records
# ---------------------------------------------------------------------------


def _validate_dev(dev: object, field: str = "file_event.dev") -> str:
    if not isinstance(dev, str):
        raise ValueError(f"{field} must be a string, got {type(dev).__name__}")
    if _DEV.fullmatch(dev) is None:
        raise ValueError(f"{field} must be a canonical decimal 'major:minor' string, got {dev!r}")
    major, minor = dev.split(":")
    if int(major) > MAX_DEV_PART or int(minor) > MAX_DEV_PART:
        raise ValueError(f"{field} is above the kernel dev_t range: {dev!r}")
    return dev


def _validate_name(basename: object, name_len: object, field: str = "file_event") -> None:
    if not isinstance(basename, bytes):
        raise ValueError(f"{field}.basename must be bytes, got {type(basename).__name__}")
    validate_sqlite_integer(name_len, f"{field}.name_len")
    if name_len > NAME_CAP:  # type: ignore[operator]
        raise ValueError(f"{field}.name_len exceeds the {NAME_CAP}-byte wire field: {name_len}")
    if name_len != len(basename):  # type: ignore[operator]
        raise ValueError(
            f"{field}.name_len {name_len} does not match {len(basename)} basename bytes"
        )
    if b"\0" in basename:
        raise ValueError(f"{field}.basename must not contain a NUL byte")


def _validate_generation(value: object, field: str) -> int:
    validate_sqlite_integer(value, field)
    if value > MAX_GENERATION:  # type: ignore[operator]
        raise ValueError(f"{field} is above the durable 2**32-1 limit: {value}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class FileEvent:
    """One producer key's merged counters, exactly as the loader reported them."""

    cgroup_id: int
    dev: str
    inode: int
    generation: int
    basename: bytes
    name_len: int
    reads: int
    writes: int
    bytes_r: int
    bytes_w: int

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.cgroup_id, "file_event.cgroup_id")
        if self.cgroup_id == RESERVED_CGROUP_ID:
            raise ValueError(
                "file_event.cgroup_id must not be the reserved unknown-owner key 0"
            )
        _validate_dev(self.dev)
        validate_sqlite_integer(self.inode, "file_event.inode")
        _validate_generation(self.generation, "file_event.generation")
        _validate_name(self.basename, self.name_len)
        for name in ("reads", "writes", "bytes_r", "bytes_w"):
            validate_sqlite_integer(getattr(self, name), f"file_event.{name}")


@dataclass(frozen=True)
class CgroupFileStats:
    """One cgroup's hook hits, overflow and skipped counters for a drained slot.

    ``cgroup_id`` 0 is the loader's reserved unknown-owner overflow bucket. It is
    the only cgroup identity that may be zero and must never be read as a real
    process cgroup; :attr:`is_unknown_owner` names the distinction.
    """

    cgroup_id: int
    hook_hits: HookHits
    overflow_files: int
    overflow_bytes_r: int
    overflow_bytes_w: int
    skipped_nonfile: int
    skipped_unsupported_fs: int

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.cgroup_id, "cgroup_stats.cgroup_id")
        if not isinstance(self.hook_hits, HookHits):
            raise ValueError("cgroup_stats.hook_hits must be a HookHits")
        for name in ("overflow_files", "overflow_bytes_r", "overflow_bytes_w",
                     "skipped_nonfile", "skipped_unsupported_fs"):
            validate_sqlite_integer(getattr(self, name), f"cgroup_stats.{name}")

    @property
    def is_unknown_owner(self) -> bool:
        return self.cgroup_id == RESERVED_CGROUP_ID


@dataclass(frozen=True)
class FileDrain:
    """All records and stats of one acknowledged, fully quiesced slot.

    A ``FileDrain`` is only constructed for an acknowledged drain, so ``complete``
    is True for everything the source returns; an unacknowledged drain raises
    instead. ``stats`` keeps the reserved unknown-owner row in payload order and
    :attr:`cgroup_stats` is the subset a consumer may attribute to processes.
    """

    receipt: Receipt
    events: tuple[FileEvent, ...]
    stats: tuple[CgroupFileStats, ...]
    complete: bool
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, Receipt):
            raise ValueError("file_drain.receipt must be a Receipt")
        if not isinstance(self.events, tuple):
            raise ValueError("file_drain.events must be a tuple")
        for index, event in enumerate(self.events):
            if not isinstance(event, FileEvent):
                raise ValueError(f"file_drain.events[{index}] must be a FileEvent")
        if not isinstance(self.stats, tuple):
            raise ValueError("file_drain.stats must be a tuple")
        seen: set[int] = set()
        for index, item in enumerate(self.stats):
            if not isinstance(item, CgroupFileStats):
                raise ValueError(f"file_drain.stats[{index}] must be a CgroupFileStats")
            if item.cgroup_id in seen:
                raise ValueError(
                    "file_drain.stats contains a duplicate cgroup_id "
                    f"{item.cgroup_id}"
                )
            seen.add(item.cgroup_id)
        if not isinstance(self.complete, bool):
            raise ValueError("file_drain.complete must be a bool")
        if self.diagnostic is not None:
            if not isinstance(self.diagnostic, str):
                raise ValueError("file_drain.diagnostic must be a string or None")
            if len(self.diagnostic) > DIAGNOSTIC_CAP:
                raise ValueError(
                    f"file_drain.diagnostic exceeds the {DIAGNOSTIC_CAP}-character bound"
                )

    @property
    def cgroup_stats(self) -> tuple[CgroupFileStats, ...]:
        """Stats a consumer may attribute to real process cgroups."""
        return tuple(item for item in self.stats if not item.is_unknown_owner)

    @property
    def unknown_owner_stats(self) -> tuple[CgroupFileStats, ...]:
        """The reserved ``overflow_owner_unknown`` rows, never an app's counters."""
        return tuple(item for item in self.stats if item.is_unknown_owner)


# ---------------------------------------------------------------------------
# Validation and decoding
# ---------------------------------------------------------------------------


def _durable(value: int, field: str) -> int:
    try:
        validate_sqlite_integer(value, field)
    except ValueError:
        raise FileSourceError(
            None, None, f"{field} is outside the durable integer range: raw {value}"
        ) from None
    return value


def _durable_generation(value: int) -> int:
    _durable(value, "file_event.generation")
    if value > MAX_GENERATION:
        raise FileSourceError(
            None,
            None,
            f"file_event.generation is above the durable 2**32-1 limit: raw {value}",
        )
    return value


def _wire_errno(value: int) -> int | None:
    return None if value == 0 else value


def _decode_text(payload: bytes, field: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FileSourceError(
            None, None, f"{field} is not valid UTF-8 (byte {error.start})"
        ) from None


def format_dev(major: int, minor: int) -> str:
    """Canonical decimal ``major:minor``; the only spelling this layer accepts."""
    for value, field in ((major, "dev.major"), (minor, "dev.minor")):
        validate_sqlite_integer(value, field)
        if value > MAX_DEV_PART:
            raise ValueError(f"{field} is above the kernel dev_t range: {value}")
    return f"{major}:{minor}"


def pack_file_record(event: FileEvent) -> bytes:
    """The exact inverse of :func:`decode_file_record`, for vectors and shims."""
    if not isinstance(event, FileEvent):
        raise TypeError("pack_file_record requires a FileEvent")
    major, minor = event.dev.split(":")
    return FILE_RECORD.pack(
        event.cgroup_id, int(major), int(minor), event.inode, event.generation,
        event.reads, event.writes, event.bytes_r, event.bytes_w, event.name_len,
        event.basename.ljust(NAME_CAP, b"\0"),
    )


def pack_cgroup_stats(stats: CgroupFileStats) -> bytes:
    """The exact inverse of :func:`decode_cgroup_stats`, for vectors and shims."""
    if not isinstance(stats, CgroupFileStats):
        raise TypeError("pack_cgroup_stats requires a CgroupFileStats")
    hits = stats.hook_hits
    return CGROUP_STATS.pack(
        stats.cgroup_id,
        hits.vfs_read, hits.vfs_write, hits.vfs_readv,
        hits.vfs_writev, hits.vfs_iter_read, hits.vfs_iter_write,
        stats.overflow_files, stats.overflow_bytes_r, stats.overflow_bytes_w,
        stats.skipped_nonfile, stats.skipped_unsupported_fs,
    )


def decode_file_record(payload: bytes) -> FileEvent:
    """Decode one 104-byte record, refusing anything the durable schema cannot hold."""
    if len(payload) != FILE_RECORD.size:
        raise FileSourceError(
            None, None, f"file record is {len(payload)} bytes, expected {FILE_RECORD.size}"
        )
    (cgroup_id, major, minor, inode, generation, reads, writes, bytes_r, bytes_w,
     name_len, name) = FILE_RECORD.unpack(payload)
    if name_len > NAME_CAP:
        raise FileSourceError(
            None,
            None,
            f"file record name_len is {name_len}, above the {NAME_CAP}-byte wire field",
        )
    if cgroup_id == RESERVED_CGROUP_ID:
        raise FileSourceError(
            None,
            None,
            "a file record may not use the reserved unknown-owner cgroup key "
            f"{RESERVED_CGROUP_ID}",
        )
    try:
        return FileEvent(
            cgroup_id=_durable(cgroup_id, "file_event.cgroup_id"),
            dev=format_dev(major, minor),
            inode=_durable(inode, "file_event.inode"),
            generation=_durable_generation(generation),
            basename=name[:name_len],
            name_len=name_len,
            reads=_durable(reads, "file_event.reads"),
            writes=_durable(writes, "file_event.writes"),
            bytes_r=_durable(bytes_r, "file_event.bytes_r"),
            bytes_w=_durable(bytes_w, "file_event.bytes_w"),
        )
    except ValueError as error:
        raise FileSourceError(None, None, str(error)) from None


def decode_cgroup_stats(payload: bytes) -> CgroupFileStats:
    """Decode one 96-byte cgroup-stats record."""
    if len(payload) != CGROUP_STATS.size:
        raise FileSourceError(
            None,
            None,
            f"cgroup stats record is {len(payload)} bytes, expected {CGROUP_STATS.size}",
        )
    values = CGROUP_STATS.unpack(payload)
    cgroup_id = _durable(values[0], "cgroup_stats.cgroup_id")
    hit_values = values[1:7]
    for name, value in zip(HOOK_NAMES, hit_values, strict=True):
        _durable(value, f"cgroup_stats.hook_hits.{name}")
    counters = {
        name: _durable(value, f"cgroup_stats.{name}")
        for name, value in zip(
            ("overflow_files", "overflow_bytes_r", "overflow_bytes_w",
             "skipped_nonfile", "skipped_unsupported_fs"),
            values[7:12],
            strict=True,
        )
    }
    try:
        return CgroupFileStats(
            cgroup_id=cgroup_id,
            hook_hits=HookHits(*hit_values),
            **counters,
        )
    except ValueError as error:
        raise FileSourceError(None, None, f"cgroup stats record: {error}") from None


def _missing_hooks(mask: int) -> str:
    names = [name for index, name in enumerate(HOOK_NAMES) if not mask & (1 << index)]
    if names:
        return f"the loader attached {mask:#04x}, missing {', '.join(names)}"
    return f"the loader reported unknown attach bits {mask:#04x}"


def _decode_hello(frame: bytes) -> None:
    """Fail closed unless the loader is ABI-compatible, ready, and fully attached."""
    if len(frame) < 4:
        raise FileSourceError(
            None, None, f"HELLO response is {len(frame)} bytes, shorter than the ABI prefix"
        )
    abi, _status = struct.unpack_from("<HH", frame)
    if abi != ABI_VERSION:
        raise FileSourceError(
            -errno_module.EPROTO, None, f"loader ABI version {abi} != {ABI_VERSION}"
        )
    if len(frame) < HELLO_RESPONSE.size:
        raise FileSourceError(
            -errno_module.EPROTO,
            None,
            f"HELLO response is {len(frame)} bytes, shorter than the "
            f"{HELLO_RESPONSE.size}-byte header",
        )
    _abi, status, hook_mask, map_capacity, error, verifier_len = HELLO_RESPONSE.unpack_from(frame)
    if verifier_len > DIAGNOSTIC_CAP:
        raise FileSourceError(
            _wire_errno(error),
            None,
            f"verifier text of {verifier_len} bytes exceeds the {DIAGNOSTIC_CAP}-byte bound",
        )
    if len(frame) != HELLO_RESPONSE.size + verifier_len:
        raise FileSourceError(
            _wire_errno(error),
            None,
            f"HELLO payload is {len(frame)} bytes, expected "
            f"{HELLO_RESPONSE.size + verifier_len}",
        )
    verifier = _decode_text(frame[HELLO_RESPONSE.size:], "verifier") if verifier_len else None
    if status != STATUS_OK:
        raise FileSourceError(
            _wire_errno(error), verifier, "the loader did not report ready"
        )
    if error != 0:
        raise FileSourceError(
            _wire_errno(error), verifier, "the loader reported an error with a ready status"
        )
    if hook_mask != HOOK_MASK_ALL:
        raise FileSourceError(None, verifier, _missing_hooks(hook_mask))
    if map_capacity != MAX_MAP_ENTRIES:
        raise FileSourceError(
            None, verifier, f"map capacity {map_capacity} != {MAX_MAP_ENTRIES}"
        )


def _decode_drain_response(frame: bytes, receipt: Receipt) -> FileDrain:
    """Decode one acknowledged drain response; never partially."""
    if len(frame) < 4:
        raise FileSourceError(
            None, None, f"drain response is {len(frame)} bytes, shorter than the ABI prefix"
        )
    abi, _status = struct.unpack_from("<HH", frame)
    if abi != ABI_VERSION:
        raise FileSourceError(
            -errno_module.EPROTO, None, f"loader drain response ABI version {abi} != {ABI_VERSION}"
        )
    if len(frame) < DRAIN_RESPONSE.size:
        raise FileSourceError(
            -errno_module.EPROTO,
            None,
            f"drain response is {len(frame)} bytes, shorter than the "
            f"{DRAIN_RESPONSE.size}-byte header",
        )

    (_abi, status, hook_mask, map_capacity, error, drained_slot, epoch, seq,
     event_count, stats_count, complete, diagnostic_len) = DRAIN_RESPONSE.unpack_from(frame)

    if drained_slot not in (0, 1):
        raise FileSourceError(
            None, None, f"drained slot {drained_slot} is outside the two-slot double buffer"
        )
    if hook_mask != HOOK_MASK_ALL:
        raise FileSourceError(None, None, _missing_hooks(hook_mask))
    if map_capacity != MAX_MAP_ENTRIES:
        raise FileSourceError(None, None, f"map capacity {map_capacity} != {MAX_MAP_ENTRIES}")
    if event_count > MAX_MAP_ENTRIES or stats_count > MAX_MAP_ENTRIES:
        raise FileSourceError(
            None,
            None,
            f"the loader declared {event_count} records and {stats_count} stats, "
            f"above the {MAX_MAP_ENTRIES}-entry map capacity",
        )
    if diagnostic_len > DIAGNOSTIC_CAP:
        raise FileSourceError(
            _wire_errno(error),
            None,
            f"diagnostic of {diagnostic_len} bytes exceeds the {DIAGNOSTIC_CAP}-byte bound",
        )
    if (epoch, seq) != (receipt.epoch, receipt.seq):
        raise FileSourceError(
            None,
            None,
            f"the drain response does not echo the requested receipt: "
            f"({epoch}, {seq}) != ({receipt.epoch}, {receipt.seq})",
        )

    expected = (DRAIN_RESPONSE.size + event_count * FILE_RECORD.size
                + stats_count * CGROUP_STATS.size + diagnostic_len)
    if len(frame) != expected:
        raise FileSourceError(
            None,
            None,
            f"drain payload is {len(frame)} bytes, expected {expected} for "
            f"{event_count} records, {stats_count} stats and {diagnostic_len} "
            "diagnostic bytes",
        )

    offset = DRAIN_RESPONSE.size
    event_payload = frame[offset:offset + event_count * FILE_RECORD.size]
    offset += event_count * FILE_RECORD.size
    stats_payload = frame[offset:offset + stats_count * CGROUP_STATS.size]
    offset += stats_count * CGROUP_STATS.size
    diagnostic = _decode_text(frame[offset:], "drain diagnostic") if diagnostic_len else None

    if status != STATUS_OK or error != 0 or complete != DRAIN_COMPLETE:
        raise FileSourceError(
            _wire_errno(error),
            diagnostic,
            "the drain was not acknowledged as quiesced and fully cleared: "
            f"status {status}, complete {complete}",
        )

    events = tuple(
        decode_file_record(event_payload[index:index + FILE_RECORD.size])
        for index in range(0, len(event_payload), FILE_RECORD.size)
    )
    stats = tuple(
        decode_cgroup_stats(stats_payload[index:index + CGROUP_STATS.size])
        for index in range(0, len(stats_payload), CGROUP_STATS.size)
    )
    try:
        return FileDrain(receipt, events, stats, True, diagnostic)
    except ValueError as failed:
        raise FileSourceError(None, None, str(failed)) from None


def _decode_ack(frame: bytes, operation: str) -> None:
    """Decode a bare acknowledgement response (CLOSE today)."""
    if len(frame) < 4:
        raise FileSourceError(
            None, None, f"{operation} response is {len(frame)} bytes, shorter than the ABI prefix"
        )
    abi, _status = struct.unpack_from("<HH", frame)
    if abi != ABI_VERSION:
        raise FileSourceError(
            -errno_module.EPROTO,
            None,
            f"loader {operation} response ABI version {abi} != {ABI_VERSION}",
        )
    if len(frame) < ACK_RESPONSE.size:
        raise FileSourceError(
            -errno_module.EPROTO,
            None,
            f"{operation} response is {len(frame)} bytes, shorter than the "
            f"{ACK_RESPONSE.size}-byte header",
        )
    _abi, status, error, diagnostic_len = ACK_RESPONSE.unpack_from(frame)
    if diagnostic_len > DIAGNOSTIC_CAP:
        raise FileSourceError(
            _wire_errno(error),
            None,
            f"{operation} diagnostic of {diagnostic_len} bytes exceeds the "
            f"{DIAGNOSTIC_CAP}-byte bound",
        )
    if len(frame) != ACK_RESPONSE.size + diagnostic_len:
        raise FileSourceError(
            _wire_errno(error),
            None,
            f"{operation} payload is {len(frame)} bytes, expected "
            f"{ACK_RESPONSE.size + diagnostic_len}",
        )
    diagnostic = (
        _decode_text(frame[ACK_RESPONSE.size:], f"{operation} diagnostic")
        if diagnostic_len
        else None
    )
    if status != STATUS_OK or error != 0:
        raise FileSourceError(
            _wire_errno(error), diagnostic, f"the loader refused {operation}"
        )


# ---------------------------------------------------------------------------
# Loader seam
# ---------------------------------------------------------------------------


@runtime_checkable
class LoaderProtocol(Protocol):
    """One complete response frame per command.

    Each method sends its command on the loader's command stream and returns the
    whole response frame -- header and every declared payload byte -- so the
    decoder in this module owns all protocol interpretation. A frame that cannot
    be delimited raises :class:`LoaderFrameError`; a transport write/read failure
    raises :class:`OSError`.
    """

    def hello(self) -> bytes:
        """Send HELLO and return its response frame."""

    def flip_drain(self, epoch: int, seq: int) -> bytes:
        """Send FLIP_DRAIN with the caller's receipt and return its frame."""

    def close(self) -> bytes:
        """Send CLOSE and return its acknowledgement frame."""


class StreamLoader:
    """The real command stream: loader stdin/stdout over binary file objects."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer

    # -- duplex primitives ---------------------------------------------------

    def _read_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self._reader.read(remaining)
            if not chunk:
                raise LoaderFrameError(
                    f"the loader command stream ended {size - remaining} bytes into a "
                    f"{size}-byte frame"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send(self, opcode: int, payload: bytes) -> None:
        self._writer.write(
            COMMAND_HEADER.pack(ABI_VERSION, opcode, len(payload)) + payload
        )
        self._writer.flush()

    def _prefix(self) -> tuple[bytes, int, int]:
        prefix = self._read_exact(4)
        abi, status = struct.unpack("<HH", prefix)
        return prefix, abi, status

    # -- commands ------------------------------------------------------------

    def hello(self) -> bytes:
        self._send(OP_HELLO, b"")
        prefix, abi, _status = self._prefix()
        if abi != ABI_VERSION:
            # The loader rejected the command before running it: bare ack only.
            return prefix + self._read_exact(ACK_RESPONSE.size - 4)
        header = prefix + self._read_exact(HELLO_RESPONSE.size - 4)
        verifier_len = HELLO_RESPONSE.unpack(header)[_HELLO_VERIFIER_LEN]
        if verifier_len > DIAGNOSTIC_CAP:
            raise LoaderFrameError(
                f"the loader declared {verifier_len} verifier bytes, above the "
                f"{DIAGNOSTIC_CAP}-byte bound"
            )
        return header + (self._read_exact(verifier_len) if verifier_len else b"")

    def flip_drain(self, epoch: int, seq: int) -> bytes:
        self._send(OP_FLIP_DRAIN, FLIP_REQUEST.pack(epoch, seq))
        prefix, abi, _status = self._prefix()
        if abi != ABI_VERSION:
            return prefix + self._read_exact(ACK_RESPONSE.size - 4)
        header = prefix + self._read_exact(DRAIN_RESPONSE.size - 4)
        fields = DRAIN_RESPONSE.unpack(header)
        event_count = fields[_DRAIN_EVENT_COUNT]
        stats_count = fields[_DRAIN_STATS_COUNT]
        diagnostic_len = fields[_DRAIN_DIAGNOSTIC_LEN]
        if event_count > MAX_MAP_ENTRIES or stats_count > MAX_MAP_ENTRIES:
            raise LoaderFrameError(
                f"the loader declared {event_count} records and {stats_count} stats, "
                f"above the {MAX_MAP_ENTRIES}-entry inner map"
            )
        if diagnostic_len > DIAGNOSTIC_CAP:
            raise LoaderFrameError(
                f"the loader declared {diagnostic_len} diagnostic bytes, above the "
                f"{DIAGNOSTIC_CAP}-byte bound"
            )
        size = (event_count * FILE_RECORD.size + stats_count * CGROUP_STATS.size
                + diagnostic_len)
        return header + (self._read_exact(size) if size else b"")

    def close(self) -> bytes:
        self._send(OP_CLOSE, b"")
        header = self._read_exact(ACK_RESPONSE.size)
        if ACK_RESPONSE.unpack(header)[0] != ABI_VERSION:
            return header
        diagnostic_len = ACK_RESPONSE.unpack(header)[_ACK_DIAGNOSTIC_LEN]
        if diagnostic_len > DIAGNOSTIC_CAP:
            raise LoaderFrameError(
                f"the loader declared {diagnostic_len} diagnostic bytes, above the "
                f"{DIAGNOSTIC_CAP}-byte bound"
            )
        return header + (self._read_exact(diagnostic_len) if diagnostic_len else b"")


class VectorLoader:
    """Deterministic in-memory loader used by tests and by daemon injection.

    It models the loader's own state -- two map slots, the active index, the
    flip, old-slot enumeration and clearing, receipt echo -- so a test exercises
    the adapter's mechanism rather than its dataclasses. Every fault the real
    loader can report is selectable, including a quiescence timeout and a clear
    failure, because those are the two ways a drain stops being lossless.
    """

    def __init__(
        self,
        *,
        abi_version: int = ABI_VERSION,
        hello_abi_version: int | None = None,
        drain_abi_version: int | None = None,
        hook_mask: int = HOOK_MASK_ALL,
        map_capacity: int = MAX_MAP_ENTRIES,
        drain_hook_mask: int | None = None,
        drain_map_capacity: int | None = None,
        close_abi_version: int | None = None,
        hello_trailing: bytes = b"",
        hello_status: int = STATUS_OK,
        hello_error: int = 0,
        verifier: bytes = b"",
        verifier_len_report: int | None = None,
        quiescence_failure: int | None = None,
        clear_failure: int | None = None,
        close_failure: int | None = None,
        complete: int = DRAIN_COMPLETE,
        status_report: int | None = None,
        error_report: int | None = None,
        diagnostic: bytes = b"",
        diagnostic_len_report: int | None = None,
        drained_slot_report: int | None = None,
        echo_epoch: int | None = None,
        echo_seq: int | None = None,
        event_count_report: int | None = None,
        stats_count_report: int | None = None,
    ) -> None:
        self.abi_version = abi_version
        self.hello_abi_version = hello_abi_version
        self.drain_abi_version = drain_abi_version
        self.hook_mask = hook_mask
        self.map_capacity = map_capacity
        self.drain_hook_mask = drain_hook_mask
        self.drain_map_capacity = drain_map_capacity
        self.close_abi_version = close_abi_version
        self.hello_trailing = hello_trailing
        self.hello_status = hello_status
        self.hello_error = hello_error
        self.verifier = verifier
        self.verifier_len_report = verifier_len_report
        self.quiescence_failure = quiescence_failure
        self.clear_failure = clear_failure
        self.close_failure = close_failure
        self.complete = complete
        self.status_report = status_report
        self.error_report = error_report
        self.diagnostic = diagnostic
        self.diagnostic_len_report = diagnostic_len_report
        self.drained_slot_report = drained_slot_report
        self.echo_epoch = echo_epoch
        self.echo_seq = echo_seq
        self.event_count_report = event_count_report
        self.stats_count_report = stats_count_report

        # Slot payloads are raw wire bytes; the active slot is what the producer
        # would write into, and a flip retires the other one.
        self.slots: dict[int, tuple[list[bytes], list[bytes]]] = {0: ([], []), 1: ([], [])}
        self.active_slot = 0
        self.hello_requests = 0
        self.flip_requests: list[tuple[int, int]] = []
        self.close_requests = 0
        self.closed = False

    # -- vector authoring ----------------------------------------------------

    def queue(
        self,
        slot: int,
        *,
        events: Sequence[FileEvent | bytes] = (),
        stats: Sequence[CgroupFileStats | bytes] = (),
    ) -> None:
        """Put records in one slot. ``bytes`` items are used verbatim, so a test
        can inject a malformed width the typed helpers would refuse."""
        if slot not in (0, 1):
            raise ValueError(f"slot must be 0 or 1, got {slot!r}")
        pending_events, pending_stats = self.slots[slot]
        for item in events:
            pending_events.append(item if isinstance(item, bytes) else pack_file_record(item))
        for item in stats:
            pending_stats.append(item if isinstance(item, bytes) else pack_cgroup_stats(item))

    # -- loader protocol -----------------------------------------------------

    def hello(self) -> bytes:
        self.hello_requests += 1
        abi = self.abi_version if self.hello_abi_version is None else self.hello_abi_version
        verifier_len = (len(self.verifier) if self.verifier_len_report is None
                        else self.verifier_len_report)
        header = HELLO_RESPONSE.pack(
            abi, self.hello_status, self.hook_mask, self.map_capacity,
            self.hello_error, verifier_len,
        )
        return header + self.verifier[:verifier_len] + self.hello_trailing

    def flip_drain(self, epoch: int, seq: int) -> bytes:
        self.flip_requests.append((epoch, seq))
        retired = self.active_slot
        self.active_slot = 1 - retired
        abi = self.abi_version if self.drain_abi_version is None else self.drain_abi_version

        if self.quiescence_failure is not None:
            return self._error_frame(abi, self.quiescence_failure, retired, epoch, seq)
        if self.clear_failure is not None:
            # Enumeration succeeded and the records are gone, but the slot was not
            # cleared under acknowledgement: this must never read as a drain.
            self.slots[retired] = ([], [])
            return self._error_frame(abi, self.clear_failure, retired, epoch, seq)

        events, stats = self.slots[retired]
        self.slots[retired] = ([], [])
        echoed_epoch = epoch if self.echo_epoch is None else self.echo_epoch
        echoed_seq = seq if self.echo_seq is None else self.echo_seq
        status = STATUS_OK if self.status_report is None else self.status_report
        error = 0 if self.error_report is None else self.error_report
        return self._frame(
            abi, status, error, retired, echoed_epoch, echoed_seq,
            b"".join(events), b"".join(stats), self.complete, self.diagnostic,
        )

    def close(self) -> bytes:
        self.close_requests += 1
        self.closed = True
        error = 0 if self.close_failure is None else self.close_failure
        status = STATUS_OK if self.close_failure is None else STATUS_ERROR
        diagnostic_len = (len(self.diagnostic) if self.diagnostic_len_report is None
                          else self.diagnostic_len_report)
        abi = self.abi_version if self.close_abi_version is None else self.close_abi_version
        header = ACK_RESPONSE.pack(abi, status, error, diagnostic_len)
        return header + self.diagnostic[:diagnostic_len]

    # -- frame assembly ------------------------------------------------------

    def _frame(
        self,
        abi: int,
        status: int,
        error: int,
        drained_slot: int,
        epoch: int,
        seq: int,
        event_payload: bytes,
        stats_payload: bytes,
        complete: int,
        diagnostic: bytes,
    ) -> bytes:
        reported_slot = (
            drained_slot if self.drained_slot_report is None else self.drained_slot_report
        )
        hook_mask = self.hook_mask if self.drain_hook_mask is None else self.drain_hook_mask
        map_capacity = (
            self.map_capacity if self.drain_map_capacity is None else self.drain_map_capacity
        )
        event_count = (len(event_payload) // FILE_RECORD.size
                       if self.event_count_report is None else self.event_count_report)
        stats_count = (len(stats_payload) // CGROUP_STATS.size
                       if self.stats_count_report is None else self.stats_count_report)
        diagnostic_len = (len(diagnostic) if self.diagnostic_len_report is None
                          else self.diagnostic_len_report)
        header = DRAIN_RESPONSE.pack(
            abi, status, hook_mask, map_capacity, error, reported_slot,
            epoch, seq, event_count, stats_count, complete, diagnostic_len,
        )
        return header + event_payload + stats_payload + diagnostic[:diagnostic_len]

    def _error_frame(self, abi: int, error: int, drained_slot: int, epoch: int, seq: int) -> bytes:
        return self._frame(abi, STATUS_ERROR, error, drained_slot, epoch, seq,
                           b"", b"", 0, self.diagnostic)


# ---------------------------------------------------------------------------
# The drain source
# ---------------------------------------------------------------------------


class BpfFileSource:
    """Drives one loader through HELLO, FLIP_DRAIN and CLOSE.

    Failure is sticky by design: after a protocol fault, timeout, clear failure,
    counter overflow or diagnostic-size violation the source stays unavailable
    and answers every drain with the recorded :class:`FileSourceError` until the
    daemon explicitly calls :meth:`start` again (or discards the source).
    """

    def __init__(self, loader: LoaderProtocol) -> None:
        for name in ("hello", "flip_drain", "close"):
            if not callable(getattr(loader, name, None)):
                raise TypeError(f"a LoaderProtocol must provide {name}()")
        self._loader = loader
        self._started = False
        self._closed = False
        self._available = False
        self._error: FileSourceError | None = None
        self._availability = FileAvailability(False, False, None)
        self._last_receipt: Receipt | None = None

    # -- observable state ----------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def availability(self) -> FileAvailability:
        return self._availability

    @property
    def error(self) -> FileSourceError | None:
        """The typed cause of the current unavailability, when there is one."""
        return self._error

    @property
    def diagnostic(self) -> str | None:
        return self._availability.diagnostic

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> FileAvailability:
        """Attach through the loader and report what F may advertise.

        Loader-reported faults close the source instead of raising, because F
        availability is data the daemon publishes; the typed cause stays on
        :attr:`error` and every later drain raises.
        """
        if self._closed:
            raise FileSourceError(None, None, "the file source is closed")
        self._started = True
        self._available = False
        self._error = None
        self._availability = FileAvailability(False, False, None)
        try:
            frame = self._loader.hello()
        except LoaderFrameError as failure:
            self._fail(FileSourceError(
                None, None, f"the loader's HELLO response could not be read: {failure}"))
            return self._availability
        except OSError as failure:
            error = FileSourceError(
                None, None, f"the HELLO command could not be written: {failure}"
            )
            self._fail(error)
            return self._availability
        try:
            _decode_hello(frame)
        except FileSourceError as failure:
            self._fail(failure)
            return self._availability
        self._available = True
        self._availability = FileAvailability(True, True, None)
        return self._availability

    def drain(self, receipt: Receipt) -> FileDrain:
        """Return one acknowledged slot, or raise instead of measuring."""
        if not isinstance(receipt, Receipt):
            raise TypeError("drain requires a validated Receipt")
        self._require_open()
        self._check_receipt(receipt)
        try:
            frame = self._loader.flip_drain(receipt.epoch, receipt.seq)
        except LoaderFrameError as failure:
            error = FileSourceError(
                None, None, f"the loader's drain response could not be read: {failure}"
            )
            self._fail(error)
            raise error from None
        except OSError as failure:
            error = FileSourceError(
                -failure.errno if failure.errno else None,
                None,
                f"the FLIP_DRAIN command could not be written: {failure}",
            )
            self._fail(error)
            raise error from None
        try:
            drain = _decode_drain_response(frame, receipt)
        except FileSourceError as failure:
            self._fail(failure)
            raise
        self._last_receipt = receipt
        return drain

    def close(self) -> None:
        """Close the loader once; a refused close is reported, never hidden."""
        if self._closed:
            return None
        self._closed = True
        self._available = False
        if not self._started:
            return None
        try:
            frame = self._loader.close()
        except LoaderFrameError as failure:
            error = FileSourceError(
                None, None, f"the loader's CLOSE response could not be read: {failure}"
            )
            self._fail(error)
            raise error from None
        except OSError as failure:
            error = FileSourceError(
                None, None, f"the CLOSE command could not be written: {failure}"
            )
            self._fail(error)
            raise error from None
        try:
            _decode_ack(frame, "CLOSE")
        except FileSourceError as failure:
            self._fail(failure)
            raise
        return None

    # -- internals -----------------------------------------------------------

    def _fail(self, failure: FileSourceError) -> None:
        self._available = False
        self._error = failure
        parts: list[str] = []
        if failure.errno is not None:
            parts.append(f"errno {failure.errno} ({_errno_text(failure.errno)})")
        parts.append(failure.detail)
        if failure.verifier:
            parts.append(failure.verifier)
        self._availability = FileAvailability(False, False, _bounded(": ".join(parts)))

    def _require_open(self) -> None:
        if self._closed:
            raise FileSourceError(None, None, "the file source is closed")
        if not self._started:
            raise FileSourceError(None, None, "the file source has not been started")
        if not self._available:
            failure = self._error
            detail = "the file source is unavailable until it is started again"
            if failure is not None:
                detail = f"{detail}: {failure.detail}"
            raise FileSourceError(
                None if failure is None else failure.errno,
                None if failure is None else failure.verifier,
                detail,
            )

    def _check_receipt(self, receipt: Receipt) -> None:
        """Refuse a replayed or rewound receipt before touching the loader."""
        last = self._last_receipt
        if last is None:
            return
        if receipt.epoch < last.epoch or (
            receipt.epoch == last.epoch and receipt.seq <= last.seq
        ):
            raise FileSourceError(
                None,
                None,
                f"receipt ({receipt.epoch}, {receipt.seq}) does not advance the "
                f"acknowledged ({last.epoch}, {last.seq})",
            )
