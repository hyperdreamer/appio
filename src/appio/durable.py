"""Canonical durable JSON for the version-1 handover, checkpoint and hour state.

Specification section 5.4 makes the encoding part of the store contract, not a
serialization detail: the same value MUST always produce the same bytes, and a
stored object MUST be rejected unless it re-encodes to exactly the text it came
from. Three rules follow from that and are enforced here:

* parsing uses an ``object_pairs_hook`` that rejects duplicate keys, so a
  duplicated ``boot_id`` can never be silently resolved to one of its values;
* every integer field is checked with :func:`validate_sqlite_integer`, which
  rejects ``bool`` (an ``int`` subclass) and values outside the SQLite range;
* array order is validated against the declared typed order, independently of
  the sorted object-key encoding.

The decoders return frozen, typed records rather than raw dictionaries. Every
record validates itself in ``__post_init__``, so a caller cannot construct an
invalid state and encode it; :func:`canonical_json` is then the single encoder
for both freshly built state and a validated decode.
"""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
import json
from typing import Any

from .contracts import (
    FILE_HOUR_NS,
    AppKey,
    CounterVector,
    HookHits,
    validate_file_uuid,
    validate_sqlite_integer,
)
from .identity import validate_app_key

__all__ = [
    "Credit",
    "DeviceBaseline",
    "FileEntry",
    "FileHourApp",
    "FileHourCoverage",
    "FileHourState",
    "HandoverState",
    "ProcessCheckpoint",
    "ProcessRef",
    "Receipt",
    "SamplerCheckpointRow",
    "SamplerCheckpointState",
    "UnknownOwnerCoverage",
    "UnknownOwnerState",
    "canonical_json",
    "decode_file_hour_state",
    "decode_handover",
    "decode_sampler_checkpoint",
]

SOURCE_VALUES: tuple[str, ...] = ("gui", "iod")
"""Reserved P1 collector sources; anything else is a new identity, not an alias."""

FILE_STATE_VALUES: tuple[str, ...] = ("resolved", "truncated", "unresolved")
HOOK_NAMES: tuple[str, ...] = (
    "vfs_read",
    "vfs_write",
    "vfs_readv",
    "vfs_writev",
    "vfs_iter_read",
    "vfs_iter_write",
)
COUNTER_NAMES: tuple[str, ...] = ("rchar", "wchar", "read_bytes", "write_bytes")
"""``CounterVector`` order; the JSON objects name the same four counters."""

_HANDOVER_KEYS = ("version", "boot_id", "cutoff_mono_ns", "processes", "devices")
_CHECKPOINT_KEYS = (
    "version",
    "boot_id",
    "source",
    "epoch",
    "last_seq",
    "checkpoint_mono_ns",
    "processes",
    "devices",
)
_PROCESS_KEYS = (
    "pid",
    "start_ticks",
    "ppid",
    "parent",
    "app_key",
    "launch_root",
    "launch_id",
    "baseline",
    "credits",
)
_REF_KEYS = ("pid", "start_ticks")
_CREDIT_KEYS = (
    "child",
    "disappearance_epoch",
    "disappearance_seq",
    "remaining",
    "correction_counted",
)
_DEVICE_KEYS = ("cgroup_id", "dev", "rbytes", "wbytes")
_FILE_HOUR_KEYS = ("version", "boot_id", "hour_start_mono_ns", "apps", "unknown_owner")
_APP_KEYS = ("app_key", "files", "coverage", "last_receipt")
_FILE_KEYS = (
    "fs_uuid",
    "dev",
    "inode",
    "generation",
    "identity_weak",
    "basename",
    "aliases",
    "state",
    "reads",
    "writes",
    "bytes_r",
    "bytes_w",
    "last_seen_mono_ns",
)
_COVERAGE_SCALARS = (
    "counted_omitted_files",
    "counted_omitted_bytes_r",
    "counted_omitted_bytes_w",
    "uncounted_overflow_files",
    "uncounted_overflow_bytes_r",
    "uncounted_overflow_bytes_w",
    "skipped_nonfile",
    "skipped_unsupported_fs",
    "weak_identity_rows",
    "gap_intervals",
    "overlap_uncertain_intervals",
)
_COVERAGE_KEYS = _COVERAGE_SCALARS + ("hook_hits", "complete")
_RECEIPT_KEYS = ("source", "epoch", "seq")
_UNKNOWN_OWNER_KEYS = (
    "overflow_files",
    "overflow_bytes_r",
    "overflow_bytes_w",
    "coverage",
    "last_receipt",
)
_UNKNOWN_COVERAGE_KEYS = ("gap_intervals", "overlap_uncertain_intervals", "complete")

_MAX_GENERATION = 2**32 - 1


def _counter_field() -> Any:
    """Mark a ``CounterVector`` field that encodes as a named JSON object."""

    return dataclasses.field(metadata={"counter_vector": True})


# --------------------------------------------------------------------------
# Strict JSON helpers
# --------------------------------------------------------------------------


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object, rejecting a repeated key instead of keeping the last."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_float(token: str) -> float:
    raise ValueError(f"durable JSON requires integer tokens, got {token!r}")


def _reject_constant(token: str) -> float:
    raise ValueError(f"durable JSON must not contain the constant {token!r}")


def _parse_object(text: object, field: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError(f"{field} must be JSON text, got {type(text).__name__}")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"{field} is not valid JSON: {error.msg}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _exact_object(value: object, field: str, keys: tuple[str, ...]) -> dict[str, Any]:
    """Require exactly ``keys``: no missing key and no extra key."""

    obj = _object(value, field)
    expected = set(keys)
    observed = set(obj)
    if observed != expected:
        details = []
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise ValueError(f"{field} must have exactly {', '.join(keys)}: {'; '.join(details)}")
    return obj


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array")
    return value


def _integer(
    value: object,
    field: str,
    *,
    positive: bool = False,
    maximum: int | None = None,
) -> int:
    """Return a strict SQLite-range integer; reject bool and non-integers."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer, got {type(value).__name__}")
    number = validate_sqlite_integer(value, field)
    if positive and number == 0:
        raise ValueError(f"{field} must be positive, got 0")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be at most {maximum}, got {number}")
    return number


def _bool_integer(value: object, field: str) -> int:
    return _integer(value, field, maximum=1)


def _enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")
    if not allow_empty and value == "":
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _version(value: object, field: str) -> int:
    return _integer(value, field, maximum=1, positive=True)


def _tuple_of(value: object, item_type: type, field: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise ValueError(f"{field}[{index}] must be a {item_type.__name__}")
    return value


def _strict_order(items: tuple[Any, ...], key: Any, field: str) -> None:
    """Reject a duplicate or out-of-order entry in a declared typed order."""

    previous: Any = None
    for index, item in enumerate(items):
        current = key(item)
        if previous is not None and current <= previous:
            raise ValueError(
                f"{field}[{index}] must be ordered by its typed identity; "
                f"{current!r} does not follow {previous!r}"
            )
        previous = current


def _counter_vector(value: object, field: str) -> CounterVector:
    if not isinstance(value, tuple) or len(value) != 4:
        raise ValueError(f"{field} must be a CounterVector tuple of four integers")
    for index, item in enumerate(value):
        _integer(item, f"{field}[{index}]")
    return value


def _utf8(text: str) -> bytes:
    return text.encode("utf-8")


# --------------------------------------------------------------------------
# Canonical encoding
# --------------------------------------------------------------------------


def _json_value(value: object, field: str) -> Any:
    """Convert typed state into JSON values; reject anything not representable."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError(f"{field} must be an integer, not a floating-point number")
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, f"{field}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        encoded: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field} has a non-string object key")
            encoded[key] = _json_value(item, f"{field}.{key}")
        return encoded
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        encoded = {}
        for descriptor in dataclasses.fields(value):
            item = getattr(value, descriptor.name)
            if descriptor.metadata.get("counter_vector") and item is not None:
                encoded[descriptor.name] = dict(zip(COUNTER_NAMES, item))
            else:
                encoded[descriptor.name] = _json_value(
                    item, f"{field}.{descriptor.name}"
                )
        return encoded
    raise TypeError(f"{field} is not JSON-encodable: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Encode ``value`` as the section-5.4 canonical UTF-8 JSON text.

    Sorted object keys, ``ensure_ascii=False``, ``(',', ':')`` separators,
    integer tokens only, and no trailing newline. The result is validated as
    UTF-8 so a lone surrogate can never leave the codec.
    """

    encoded = json.dumps(
        _json_value(value, "value"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded.encode("utf-8")
    return encoded


def _require_canonical(text: str, state: object) -> None:
    """Reject any text that is not byte-for-byte the canonical re-encoding."""

    if canonical_json(state) != text:
        raise ValueError(
            "durable JSON is not canonical: the object's canonical re-encoding differs"
        )


# --------------------------------------------------------------------------
# Shared typed entries (handover and sampler checkpoint)
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProcessRef:
    """A ``(pid, start_ticks)`` identity, never a pid alone."""

    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        _integer(self.pid, "process_ref.pid", positive=True)
        _integer(self.start_ticks, "process_ref.start_ticks")


@dataclasses.dataclass(frozen=True)
class Credit:
    """One FIFO disappearance credit with its correction-counted bit."""

    child: ProcessRef
    disappearance_epoch: int
    disappearance_seq: int
    remaining: CounterVector = _counter_field()
    correction_counted: int

    def __post_init__(self) -> None:
        if not isinstance(self.child, ProcessRef):
            raise ValueError("credit.child must be a ProcessRef")
        _integer(self.disappearance_epoch, "credit.disappearance_epoch", positive=True)
        _integer(self.disappearance_seq, "credit.disappearance_seq")
        _counter_vector(self.remaining, "credit.remaining")
        _bool_integer(self.correction_counted, "credit.correction_counted")

    @property
    def order_key(self) -> tuple[int, int, int, int]:
        return (
            self.disappearance_epoch,
            self.disappearance_seq,
            self.child.pid,
            self.child.start_ticks,
        )


@dataclasses.dataclass(frozen=True)
class ProcessCheckpoint:
    """One open process identity as serialized by handover or checkpoint state."""

    pid: int
    start_ticks: int
    ppid: int
    parent: ProcessRef | None
    app_key: AppKey
    launch_root: ProcessRef | None
    launch_id: int
    baseline: CounterVector | None = _counter_field()
    credits: tuple[Credit, ...]

    def __post_init__(self) -> None:
        _integer(self.pid, "process.pid", positive=True)
        _integer(self.start_ticks, "process.start_ticks")
        _integer(self.ppid, "process.ppid")
        for name in ("parent", "launch_root"):
            reference = getattr(self, name)
            if reference is not None and not isinstance(reference, ProcessRef):
                raise ValueError(f"process.{name} must be a ProcessRef or null")
        validate_app_key(self.app_key)
        _integer(self.launch_id, "process.launch_id")
        if self.baseline is not None:
            _counter_vector(self.baseline, "process.baseline")
        _tuple_of(self.credits, Credit, "process.credits")
        _strict_order(self.credits, lambda credit: credit.order_key, "process.credits")

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.start_ticks)


@dataclasses.dataclass(frozen=True)
class DeviceBaseline:
    """One cgroup/device baseline entry."""

    cgroup_id: int
    dev: str
    rbytes: int
    wbytes: int

    def __post_init__(self) -> None:
        _integer(self.cgroup_id, "device.cgroup_id")
        _text(self.dev, "device.dev")
        _integer(self.rbytes, "device.rbytes")
        _integer(self.wbytes, "device.wbytes")

    @property
    def order_key(self) -> tuple[int, bytes]:
        return (self.cgroup_id, _utf8(self.dev))


def _decode_optional_ref(value: object, field: str) -> ProcessRef | None:
    if value is None:
        return None
    obj = _exact_object(value, field, _REF_KEYS)
    return ProcessRef(
        pid=_integer(obj["pid"], f"{field}.pid", positive=True),
        start_ticks=_integer(obj["start_ticks"], f"{field}.start_ticks"),
    )


def _decode_required_ref(value: object, field: str) -> ProcessRef:
    reference = _decode_optional_ref(value, field)
    if reference is None:
        raise ValueError(f"{field} must be a (pid, start_ticks) object")
    return reference


def _decode_counter(value: object, field: str) -> CounterVector:
    obj = _exact_object(value, field, COUNTER_NAMES)
    return _counter_vector(
        tuple(_integer(obj[name], f"{field}.{name}") for name in COUNTER_NAMES),
        field,
    )


def _decode_optional_counter(value: object, field: str) -> CounterVector | None:
    if value is None:
        return None
    return _decode_counter(value, field)


def _decode_credits(value: object, field: str) -> tuple[Credit, ...]:
    credits = []
    for index, item in enumerate(_array(value, field)):
        entry_field = f"{field}[{index}]"
        obj = _exact_object(item, entry_field, _CREDIT_KEYS)
        credits.append(
            Credit(
                child=_decode_required_ref(obj["child"], f"{entry_field}.child"),
                disappearance_epoch=_integer(
                    obj["disappearance_epoch"],
                    f"{entry_field}.disappearance_epoch",
                    positive=True,
                ),
                disappearance_seq=_integer(
                    obj["disappearance_seq"], f"{entry_field}.disappearance_seq"
                ),
                remaining=_decode_counter(
                    obj["remaining"], f"{entry_field}.remaining"
                ),
                correction_counted=_bool_integer(
                    obj["correction_counted"], f"{entry_field}.correction_counted"
                ),
            )
        )
    return tuple(credits)


def _decode_processes(value: object, field: str) -> tuple[ProcessCheckpoint, ...]:
    processes = []
    for index, item in enumerate(_array(value, field)):
        entry_field = f"{field}[{index}]"
        obj = _exact_object(item, entry_field, _PROCESS_KEYS)
        processes.append(
            ProcessCheckpoint(
                pid=_integer(obj["pid"], f"{entry_field}.pid", positive=True),
                start_ticks=_integer(obj["start_ticks"], f"{entry_field}.start_ticks"),
                ppid=_integer(obj["ppid"], f"{entry_field}.ppid"),
                parent=_decode_optional_ref(obj["parent"], f"{entry_field}.parent"),
                app_key=validate_app_key(obj["app_key"]),
                launch_root=_decode_optional_ref(
                    obj["launch_root"], f"{entry_field}.launch_root"
                ),
                launch_id=_integer(obj["launch_id"], f"{entry_field}.launch_id"),
                baseline=_decode_optional_counter(
                    obj["baseline"], f"{entry_field}.baseline"
                ),
                credits=_decode_credits(obj["credits"], f"{entry_field}.credits"),
            )
        )
    return tuple(processes)


def _decode_devices(value: object, field: str) -> tuple[DeviceBaseline, ...]:
    devices = []
    for index, item in enumerate(_array(value, field)):
        entry_field = f"{field}[{index}]"
        obj = _exact_object(item, entry_field, _DEVICE_KEYS)
        devices.append(
            DeviceBaseline(
                cgroup_id=_integer(obj["cgroup_id"], f"{entry_field}.cgroup_id"),
                dev=_text(obj["dev"], f"{entry_field}.dev"),
                rbytes=_integer(obj["rbytes"], f"{entry_field}.rbytes"),
                wbytes=_integer(obj["wbytes"], f"{entry_field}.wbytes"),
            )
        )
    return tuple(devices)


# --------------------------------------------------------------------------
# Version-1 handover
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HandoverState:
    """``handover.baselines_json`` at one cutoff."""

    boot_id: str
    cutoff_mono_ns: int
    processes: tuple[ProcessCheckpoint, ...]
    devices: tuple[DeviceBaseline, ...]
    version: int = 1

    def __post_init__(self) -> None:
        _version(self.version, "handover.version")
        _text(self.boot_id, "handover.boot_id")
        _integer(self.cutoff_mono_ns, "handover.cutoff_mono_ns")
        _tuple_of(self.processes, ProcessCheckpoint, "handover.processes")
        _strict_order(
            self.processes, lambda process: process.identity, "handover.processes"
        )
        _tuple_of(self.devices, DeviceBaseline, "handover.devices")
        _strict_order(self.devices, lambda device: device.order_key, "handover.devices")


def decode_handover(
    text: str, expected_boot_id: str, expected_cutoff: int
) -> HandoverState:
    """Validate a version-1 handover object against its row keys.

    The boot and cutoff are compared with the row the object belongs to, so a
    syntactically valid object copied from another cutoff cannot be restored.
    """

    boot_id = _text(expected_boot_id, "expected_boot_id")
    cutoff = _integer(expected_cutoff, "expected_cutoff")
    root = _exact_object(_parse_object(text, "handover"), "handover", _HANDOVER_KEYS)
    state = HandoverState(
        version=_version(root["version"], "handover.version"),
        boot_id=_text(root["boot_id"], "handover.boot_id"),
        cutoff_mono_ns=_integer(root["cutoff_mono_ns"], "handover.cutoff_mono_ns"),
        processes=_decode_processes(root["processes"], "handover.processes"),
        devices=_decode_devices(root["devices"], "handover.devices"),
    )
    if state.boot_id != boot_id:
        raise ValueError(
            f"handover.boot_id {state.boot_id!r} does not match the row {boot_id!r}"
        )
    if state.cutoff_mono_ns != cutoff:
        raise ValueError(
            f"handover.cutoff_mono_ns {state.cutoff_mono_ns} does not match the row {cutoff}"
        )
    _require_canonical(text, state)
    return state


# --------------------------------------------------------------------------
# Version-1 sampler checkpoint
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SamplerCheckpointRow:
    """The five ``sampler_checkpoint`` columns the state root must echo."""

    source: str
    boot_id: str
    epoch: int
    seq: int
    checkpoint_mono_ns: int

    def __post_init__(self) -> None:
        _enum(self.source, SOURCE_VALUES, "checkpoint_row.source")
        _text(self.boot_id, "checkpoint_row.boot_id")
        _integer(self.epoch, "checkpoint_row.epoch", positive=True)
        _integer(self.seq, "checkpoint_row.seq")
        _integer(self.checkpoint_mono_ns, "checkpoint_row.checkpoint_mono_ns")


@dataclasses.dataclass(frozen=True)
class SamplerCheckpointState:
    """``sampler_checkpoint.state_json`` for one committed frame."""

    boot_id: str
    source: str
    epoch: int
    last_seq: int
    checkpoint_mono_ns: int
    processes: tuple[ProcessCheckpoint, ...]
    devices: tuple[DeviceBaseline, ...]
    version: int = 1

    def __post_init__(self) -> None:
        _version(self.version, "checkpoint.version")
        _text(self.boot_id, "checkpoint.boot_id")
        _enum(self.source, SOURCE_VALUES, "checkpoint.source")
        _integer(self.epoch, "checkpoint.epoch", positive=True)
        _integer(self.last_seq, "checkpoint.last_seq")
        _integer(self.checkpoint_mono_ns, "checkpoint.checkpoint_mono_ns")
        _tuple_of(self.processes, ProcessCheckpoint, "checkpoint.processes")
        _strict_order(
            self.processes, lambda process: process.identity, "checkpoint.processes"
        )
        _tuple_of(self.devices, DeviceBaseline, "checkpoint.devices")
        _strict_order(self.devices, lambda device: device.order_key, "checkpoint.devices")


def _row_field(row: object, name: str) -> object:
    """Read one column from a sqlite3.Row, a mapping, or a record object."""

    if isinstance(row, Mapping):
        if name in row:
            return row[name]
    elif hasattr(row, "keys"):
        try:
            return row[name]  # type: ignore[index]
        except (KeyError, IndexError):
            pass
    elif hasattr(row, name):
        return getattr(row, name)
    raise ValueError(f"expected_row is missing the {name} column")


def _coerce_checkpoint_row(row: object) -> SamplerCheckpointRow:
    if isinstance(row, SamplerCheckpointRow):
        return row
    values = {
        name: _row_field(row, name)
        for name in ("source", "boot_id", "epoch", "seq", "checkpoint_mono_ns")
    }
    return SamplerCheckpointRow(**values)  # type: ignore[arg-type]


def decode_sampler_checkpoint(
    text: str, expected_row: object
) -> SamplerCheckpointState:
    """Validate a checkpoint against the exact row columns that stored it.

    A checkpoint with a preceding epoch is still restorable for the same boot;
    a checkpoint from a different boot is rejected rather than partially
    restored (A08).
    """

    row = _coerce_checkpoint_row(expected_row)
    root = _exact_object(
        _parse_object(text, "sampler checkpoint"), "checkpoint", _CHECKPOINT_KEYS
    )
    state = SamplerCheckpointState(
        version=_version(root["version"], "checkpoint.version"),
        boot_id=_text(root["boot_id"], "checkpoint.boot_id"),
        source=_enum(root["source"], SOURCE_VALUES, "checkpoint.source"),
        epoch=_integer(root["epoch"], "checkpoint.epoch", positive=True),
        last_seq=_integer(root["last_seq"], "checkpoint.last_seq"),
        checkpoint_mono_ns=_integer(
            root["checkpoint_mono_ns"], "checkpoint.checkpoint_mono_ns"
        ),
        processes=_decode_processes(root["processes"], "checkpoint.processes"),
        devices=_decode_devices(root["devices"], "checkpoint.devices"),
    )
    observed = (
        state.source,
        state.boot_id,
        state.epoch,
        state.last_seq,
        state.checkpoint_mono_ns,
    )
    expected = (row.source, row.boot_id, row.epoch, row.seq, row.checkpoint_mono_ns)
    if observed != expected:
        raise ValueError(
            "checkpoint state does not match its row columns: "
            f"state {observed!r} != row {expected!r}"
        )
    _require_canonical(text, state)
    return state


# --------------------------------------------------------------------------
# Version-1 pending file-hour state
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Receipt:
    """The stream identity of the last authoritative contribution."""

    source: str
    epoch: int
    seq: int

    def __post_init__(self) -> None:
        _enum(self.source, SOURCE_VALUES, "receipt.source")
        _integer(self.epoch, "receipt.epoch", positive=True)
        _integer(self.seq, "receipt.seq")


@dataclasses.dataclass(frozen=True)
class FileHourCoverage:
    """Per-app-hour file coverage; the 11 counters plus six hook hits."""

    counted_omitted_files: int
    counted_omitted_bytes_r: int
    counted_omitted_bytes_w: int
    uncounted_overflow_files: int
    uncounted_overflow_bytes_r: int
    uncounted_overflow_bytes_w: int
    skipped_nonfile: int
    skipped_unsupported_fs: int
    weak_identity_rows: int
    gap_intervals: int
    overlap_uncertain_intervals: int
    hook_hits: HookHits
    complete: int

    def __post_init__(self) -> None:
        for name in _COVERAGE_SCALARS:
            _integer(getattr(self, name), f"file_coverage.{name}")
        if not isinstance(self.hook_hits, HookHits):
            raise ValueError("file_coverage.hook_hits must be a HookHits")
        _bool_integer(self.complete, "file_coverage.complete")


@dataclasses.dataclass(frozen=True)
class FileEntry:
    """One aggregation identity's counters inside one app-hour."""

    fs_uuid: str
    dev: str
    inode: int
    generation: int
    identity_weak: int
    basename: str
    aliases: tuple[str, ...]
    state: str
    reads: int
    writes: int
    bytes_r: int
    bytes_w: int
    last_seen_mono_ns: int

    def __post_init__(self) -> None:
        _text(self.fs_uuid, "file.fs_uuid", allow_empty=True)
        _text(self.dev, "file.dev")
        _integer(self.inode, "file.inode")
        _integer(self.generation, "file.generation", maximum=_MAX_GENERATION)
        _bool_integer(self.identity_weak, "file.identity_weak")
        validate_file_uuid(self.fs_uuid, self.identity_weak, "file")
        _text(self.basename, "file.basename")
        if not isinstance(self.aliases, tuple):
            raise ValueError("file.aliases must be a tuple")
        previous: bytes | None = None
        basename_bytes = _utf8(self.basename)
        for index, alias in enumerate(self.aliases):
            _text(alias, f"file.aliases[{index}]")
            encoded = _utf8(alias)
            if encoded <= basename_bytes:
                raise ValueError(
                    "file.aliases must be bytewise greater than the canonical basename"
                )
            if previous is not None and encoded <= previous:
                raise ValueError(
                    f"file.aliases[{index}] must be unique and UTF-8-byte sorted"
                )
            previous = encoded
        if not isinstance(self.state, str) or self.state not in FILE_STATE_VALUES:
            raise ValueError(
                f"file.state must be one of {', '.join(FILE_STATE_VALUES)}, got {self.state!r}"
            )
        for name in ("reads", "writes", "bytes_r", "bytes_w", "last_seen_mono_ns"):
            _integer(getattr(self, name), f"file.{name}")

    @property
    def identity(self) -> tuple[bytes, bytes, int, int, int]:
        return (
            _utf8(self.fs_uuid),
            _utf8(self.dev),
            self.inode,
            self.generation,
            self.identity_weak,
        )


@dataclasses.dataclass(frozen=True)
class FileHourApp:
    """One app's pending files, coverage and last receipt for an hour."""

    app_key: AppKey
    files: tuple[FileEntry, ...]
    coverage: FileHourCoverage
    last_receipt: Receipt

    def __post_init__(self) -> None:
        validate_app_key(self.app_key)
        _tuple_of(self.files, FileEntry, "file_hour_app.files")
        _strict_order(self.files, lambda entry: entry.identity, "file_hour_app.files")
        if not isinstance(self.coverage, FileHourCoverage):
            raise ValueError("file_hour_app.coverage must be a FileHourCoverage")
        if not isinstance(self.last_receipt, Receipt):
            raise ValueError("file_hour_app.last_receipt must be a Receipt")


@dataclasses.dataclass(frozen=True)
class UnknownOwnerCoverage:
    """The explicit zero-or-more unknown-owner coverage object."""

    gap_intervals: int
    overlap_uncertain_intervals: int
    complete: int

    def __post_init__(self) -> None:
        _integer(self.gap_intervals, "unknown_owner.coverage.gap_intervals")
        _integer(
            self.overlap_uncertain_intervals,
            "unknown_owner.coverage.overlap_uncertain_intervals",
        )
        _bool_integer(self.complete, "unknown_owner.coverage.complete")


@dataclasses.dataclass(frozen=True)
class UnknownOwnerState:
    """Unknown-owner overflow, present even when every counter is zero."""

    overflow_files: int
    overflow_bytes_r: int
    overflow_bytes_w: int
    coverage: UnknownOwnerCoverage
    last_receipt: Receipt | None

    def __post_init__(self) -> None:
        for name in ("overflow_files", "overflow_bytes_r", "overflow_bytes_w"):
            _integer(getattr(self, name), f"unknown_owner.{name}")
        if not isinstance(self.coverage, UnknownOwnerCoverage):
            raise ValueError("unknown_owner.coverage must be an UnknownOwnerCoverage")
        if self.last_receipt is not None and not isinstance(self.last_receipt, Receipt):
            raise ValueError("unknown_owner.last_receipt must be a Receipt or null")


@dataclasses.dataclass(frozen=True)
class FileHourState:
    """``file_hour_state.state_json`` for one ``(boot, hour)`` accumulator."""

    boot_id: str
    hour_start_mono_ns: int
    apps: tuple[FileHourApp, ...]
    unknown_owner: UnknownOwnerState
    version: int = 1

    def __post_init__(self) -> None:
        _version(self.version, "file_hour.version")
        _text(self.boot_id, "file_hour.boot_id")
        hour_start = _integer(self.hour_start_mono_ns, "file_hour.hour_start_mono_ns")
        if hour_start % FILE_HOUR_NS != 0:
            raise ValueError(
                f"file_hour.hour_start_mono_ns must be hour-aligned, got {hour_start}"
            )
        _tuple_of(self.apps, FileHourApp, "file_hour.apps")
        _strict_order(self.apps, lambda app: _utf8(app.app_key), "file_hour.apps")
        if not isinstance(self.unknown_owner, UnknownOwnerState):
            raise ValueError("file_hour.unknown_owner must be an UnknownOwnerState")
        hour_end = hour_start + FILE_HOUR_NS
        for app_index, app in enumerate(self.apps):
            for file_index, entry in enumerate(app.files):
                if not hour_start <= entry.last_seen_mono_ns < hour_end:
                    raise ValueError(
                        f"file_hour.apps[{app_index}].files[{file_index}]"
                        ".last_seen_mono_ns must be inside the half-open hour"
                    )


def _decode_hook_hits(value: object, field: str) -> HookHits:
    obj = _exact_object(value, field, HOOK_NAMES)
    return HookHits(
        **{name: _integer(obj[name], f"{field}.{name}") for name in HOOK_NAMES}
    )


def _decode_coverage(value: object, field: str) -> FileHourCoverage:
    obj = _exact_object(value, field, _COVERAGE_KEYS)
    return FileHourCoverage(
        **{name: _integer(obj[name], f"{field}.{name}") for name in _COVERAGE_SCALARS},
        hook_hits=_decode_hook_hits(obj["hook_hits"], f"{field}.hook_hits"),
        complete=_bool_integer(obj["complete"], f"{field}.complete"),
    )


def _decode_receipt(value: object, field: str) -> Receipt:
    obj = _exact_object(value, field, _RECEIPT_KEYS)
    return Receipt(
        source=_enum(obj["source"], SOURCE_VALUES, f"{field}.source"),
        epoch=_integer(obj["epoch"], f"{field}.epoch", positive=True),
        seq=_integer(obj["seq"], f"{field}.seq"),
    )


def _decode_files(value: object, field: str) -> tuple[FileEntry, ...]:
    entries = []
    for index, item in enumerate(_array(value, field)):
        entry_field = f"{field}[{index}]"
        obj = _exact_object(item, entry_field, _FILE_KEYS)
        aliases = _array(obj["aliases"], f"{entry_field}.aliases")
        entries.append(
            FileEntry(
                fs_uuid=_text(obj["fs_uuid"], f"{entry_field}.fs_uuid", allow_empty=True),
                dev=_text(obj["dev"], f"{entry_field}.dev"),
                inode=_integer(obj["inode"], f"{entry_field}.inode"),
                generation=_integer(
                    obj["generation"],
                    f"{entry_field}.generation",
                    maximum=_MAX_GENERATION,
                ),
                identity_weak=_bool_integer(
                    obj["identity_weak"], f"{entry_field}.identity_weak"
                ),
                basename=_text(obj["basename"], f"{entry_field}.basename"),
                aliases=tuple(
                    _text(alias, f"{entry_field}.aliases[{position}]")
                    for position, alias in enumerate(aliases)
                ),
                state=_enum(obj["state"], FILE_STATE_VALUES, f"{entry_field}.state"),
                reads=_integer(obj["reads"], f"{entry_field}.reads"),
                writes=_integer(obj["writes"], f"{entry_field}.writes"),
                bytes_r=_integer(obj["bytes_r"], f"{entry_field}.bytes_r"),
                bytes_w=_integer(obj["bytes_w"], f"{entry_field}.bytes_w"),
                last_seen_mono_ns=_integer(
                    obj["last_seen_mono_ns"], f"{entry_field}.last_seen_mono_ns"
                ),
            )
        )
    return tuple(entries)


def _decode_apps(value: object, field: str) -> tuple[FileHourApp, ...]:
    apps = []
    for index, item in enumerate(_array(value, field)):
        entry_field = f"{field}[{index}]"
        obj = _exact_object(item, entry_field, _APP_KEYS)
        apps.append(
            FileHourApp(
                app_key=validate_app_key(obj["app_key"]),
                files=_decode_files(obj["files"], f"{entry_field}.files"),
                coverage=_decode_coverage(obj["coverage"], f"{entry_field}.coverage"),
                last_receipt=_decode_receipt(
                    obj["last_receipt"], f"{entry_field}.last_receipt"
                ),
            )
        )
    return tuple(apps)


def _decode_unknown_owner(value: object, field: str) -> UnknownOwnerState:
    obj = _exact_object(value, field, _UNKNOWN_OWNER_KEYS)
    coverage_obj = _exact_object(obj["coverage"], f"{field}.coverage", _UNKNOWN_COVERAGE_KEYS)
    receipt = obj["last_receipt"]
    return UnknownOwnerState(
        overflow_files=_integer(obj["overflow_files"], f"{field}.overflow_files"),
        overflow_bytes_r=_integer(obj["overflow_bytes_r"], f"{field}.overflow_bytes_r"),
        overflow_bytes_w=_integer(obj["overflow_bytes_w"], f"{field}.overflow_bytes_w"),
        coverage=UnknownOwnerCoverage(
            gap_intervals=_integer(
                coverage_obj["gap_intervals"], f"{field}.coverage.gap_intervals"
            ),
            overlap_uncertain_intervals=_integer(
                coverage_obj["overlap_uncertain_intervals"],
                f"{field}.coverage.overlap_uncertain_intervals",
            ),
            complete=_bool_integer(
                coverage_obj["complete"], f"{field}.coverage.complete"
            ),
        ),
        last_receipt=(
            None if receipt is None else _decode_receipt(receipt, f"{field}.last_receipt")
        ),
    )


def decode_file_hour_state(
    text: str, expected_boot_id: str, expected_hour_start: int
) -> FileHourState:
    """Validate a version-1 pending-hour object against its row keys."""

    boot_id = _text(expected_boot_id, "expected_boot_id")
    hour_start = _integer(expected_hour_start, "expected_hour_start")
    root = _exact_object(
        _parse_object(text, "file-hour state"), "file hour state", _FILE_HOUR_KEYS
    )
    state = FileHourState(
        version=_version(root["version"], "file_hour.version"),
        boot_id=_text(root["boot_id"], "file_hour.boot_id"),
        hour_start_mono_ns=_integer(
            root["hour_start_mono_ns"], "file_hour.hour_start_mono_ns"
        ),
        apps=_decode_apps(root["apps"], "file_hour.apps"),
        unknown_owner=_decode_unknown_owner(
            root["unknown_owner"], "file_hour.unknown_owner"
        ),
    )
    if state.boot_id != boot_id:
        raise ValueError(
            f"file_hour.boot_id {state.boot_id!r} does not match the row {boot_id!r}"
        )
    if state.hour_start_mono_ns != hour_start:
        raise ValueError(
            "file_hour.hour_start_mono_ns "
            f"{state.hour_start_mono_ns} does not match the row {hour_start}"
        )
    _require_canonical(text, state)
    return state
