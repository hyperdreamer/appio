"""Shared P1 contract types for :mod:`appio`.

The vocabulary here is normative: names, order, optionality, and error messages
come from the signed-off specification (sections 4.2, 4.3, 8.1-8.3, and the A22
message table). Later modules import these types instead of redefining them.

Two rules are enforced at construction time rather than left to convention:

* every integer is a real ``int`` inside the SQLite ``INTEGER`` range -- ``bool``
  is rejected even though it is an ``int`` subclass, and floats are rejected so
  byte counters can never be coerced to ``REAL``;
* a declared derived total is the exact integer sum it claims to be, so a caller
  cannot substitute a zero, a rounded value, or a differently computed total.

``CounterVector`` always means ``(rchar, wchar, read_bytes, write_bytes)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "AppKey",
    "AppTotal",
    "CounterName",
    "CounterOverflowError",
    "CounterVector",
    "Coverage",
    "DeviceSeriesPoint",
    "DeviceShareRow",
    "DeviceState",
    "Direction",
    "FILE_HOUR_NS",
    "FileCoverage",
    "FileCoverageInput",
    "FileObservation",
    "FileRow",
    "FileSort",
    "FileState",
    "FileTable",
    "HISTORICAL_BOOT_PREFIX_MESSAGE",
    "HookHits",
    "HourlyFileSnapshot",
    "LAUNCH_WINDOW_MESSAGE",
    "MAX_RANGE_NS",
    "Metric",
    "RANGE_TOO_LONG_MESSAGE",
    "RAW_BUCKET_NS",
    "ResidualPoint",
    "SQLITE_INT_MAX",
    "DEFAULT_SCAN_BUDGET",
    "SQLITE_INT_MIN",
    "Series",
    "SeriesPoint",
    "Tier",
    "UnavailableReason",
    "UnknownOwnerInput",
    "UnknownOwnerOverflow",
    "Window",
    "WindowKind",
    "WindowUnavailableError",
    "validate_enum",
    "validate_search",
    "validate_sqlite_integer",
    "validate_window",
]

AppKey = str
"""External application identity. Never a database-local surrogate."""

CounterVector = tuple[int, int, int, int]
"""Counters in the fixed order ``(rchar, wchar, read_bytes, write_bytes)``."""

CounterName = Literal["rchar", "wchar", "read_bytes", "write_bytes"]
Metric = Literal["total_bytes", "read_bytes", "write_bytes", "rchar", "wchar"]
Tier = Literal["raw", "rollup_60s", "rollup_3600s", "totals_only"]
FileSort = Literal[
    "bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen"
]
Direction = Literal["asc", "desc"]
UnavailableReason = Literal[
    "not_retained", "unsupported_window", "launch_attribution", "file_source_unavailable"
]
DeviceState = Literal["available", "not_delegated", "incomplete"]
FileState = Literal["resolved", "truncated", "unresolved"]
WindowKind = Literal["range", "since_boot", "launch", "lifetime"]

SQLITE_INT_MAX = 9223372036854775807
"""Largest value a SQLite ``INTEGER`` column can hold."""

#: Row budget for one historical query scope. A source tier whose retained row
#: count exceeds it is escalated to a coarser retained tier rather than being
#: rejected, so a long window stays answerable without scanning raw history.
DEFAULT_SCAN_BUDGET = 50_000

SQLITE_INT_MIN = -9223372036854775808
"""Smallest value a SQLite ``INTEGER`` column can hold."""

MAX_RANGE_NS = 365 * 86400 * 1_000_000_000
"""Longest supported bounded range, in ``CLOCK_BOOTTIME`` nanoseconds."""

RAW_BUCKET_NS = 10_000_000_000
"""Raw process-sample bucket width (10 seconds)."""

FILE_HOUR_NS = 3_600_000_000_000
"""File-observation bucket width (one hour)."""

LAUNCH_WINDOW_MESSAGE = (
    "Launch window disabled: device and file activity are not stored per launch."
)
RANGE_TOO_LONG_MESSAGE = (
    "Ranges longer than 365 days are disabled: retained rollups cannot represent "
    "arbitrary ranges."
)
HISTORICAL_BOOT_PREFIX_MESSAGE = (
    "Historical boot prefix unavailable: totals cover the full collected boot."
)

_WINDOW_KINDS: tuple[str, ...] = ("range", "since_boot", "launch", "lifetime")
_TIER_VALUES: tuple[str, ...] = ("raw", "rollup_60s", "rollup_3600s", "totals_only")
_UNAVAILABLE_REASONS: tuple[str, ...] = (
    "not_retained",
    "unsupported_window",
    "launch_attribution",
    "file_source_unavailable",
)
_DEVICE_STATES: tuple[str, ...] = ("available", "not_delegated", "incomplete")
_FILE_STATES: tuple[str, ...] = ("resolved", "truncated", "unresolved")
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
_HOOK_NAMES: tuple[str, ...] = (
    "vfs_read",
    "vfs_write",
    "vfs_readv",
    "vfs_writev",
    "vfs_iter_read",
    "vfs_iter_write",
)


class WindowUnavailableError(ValueError):
    """A query window is disabled and carries the exact A22 reason text."""

    def __init__(self, reason: UnavailableReason, message: str) -> None:
        validate_enum(reason, _UNAVAILABLE_REASONS, "reason")
        if not isinstance(message, str) or message == "":
            raise ValueError("message must be a non-empty string")
        super().__init__(message)
        self.reason = reason


class CounterOverflowError(OverflowError):
    """A counter fell outside the SQLite ``INTEGER`` range; nothing was stored."""

    def __init__(self, field: str) -> None:
        if not isinstance(field, str) or field == "":
            raise ValueError("field must be a non-empty string")
        super().__init__(f"Counter outside SQLite INTEGER range: {field}")
        self.field = field


def validate_sqlite_integer(value: object, field: str, nonnegative: bool = True) -> int:
    """Return ``value`` if it is an integer inside the SQLite ``INTEGER`` range.

    ``bool`` is rejected explicitly: it passes ``isinstance(value, int)`` but a
    boolean counter is a type error in this contract, not the number one.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer, got {type(value).__name__}")
    if nonnegative:
        if value < 0:
            raise ValueError(f"{field} must be nonnegative, got {value}")
    elif value < SQLITE_INT_MIN:
        raise ValueError(f"{field} is below the SQLite INTEGER range: {value}")
    if value > SQLITE_INT_MAX:
        raise ValueError(f"{field} is above the SQLite INTEGER range: {value}")
    return value


def validate_enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    """Return ``value`` if it is a string in ``allowed``."""

    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def validate_search(search: str | None) -> str | None:
    """Return ``search`` after rejecting a NUL that a query could truncate on."""

    if search is None:
        return None
    if not isinstance(search, str):
        raise ValueError(f"search must be a string or None, got {type(search).__name__}")
    if "\x00" in search:
        raise ValueError("search must not contain NUL")
    return search


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    return value


def validate_file_uuid(fs_uuid: object, identity_weak: bool | int, field: str) -> str:
    """Missing UUIDs are represented by empty text and an explicitly weak identity."""
    if not isinstance(fs_uuid, str):
        raise ValueError(f"{field}.fs_uuid must be a string")
    if fs_uuid == "" and not identity_weak:
        raise ValueError(f"{field}.identity_weak must be true when fs_uuid is empty")
    return fs_uuid


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _require_positive(value: object, field: str) -> int:
    number = validate_sqlite_integer(value, field)
    if number == 0:
        raise ValueError(f"{field} must be positive, got 0")
    return number


def _require_counter_vector(value: object, field: str) -> CounterVector:
    if not isinstance(value, tuple) or len(value) != 4:
        raise ValueError(f"{field} must be a 4-tuple of integers")
    for index, item in enumerate(value):
        validate_sqlite_integer(item, f"{field}[{index}]")
    return value


def _require_signed(value: object | None, field: str) -> int | None:
    if value is None:
        return None
    return validate_sqlite_integer(value, field, nonnegative=False)


@dataclass(frozen=True)
class Window:
    """A supported query window. Every form is half-open and immutable."""

    kind: WindowKind
    boot_id: str | None = None
    start_mono_ns: int | None = None
    end_mono_ns: int | None = None
    launch_id: int | None = None

    def __post_init__(self) -> None:
        validate_window(self)


def validate_window(window: Window) -> Window:
    """Reject a malformed window combination with ``ValueError``."""

    if not isinstance(window, Window):
        raise ValueError("validate_window requires a Window")
    kind = validate_enum(window.kind, _WINDOW_KINDS, "window.kind")

    if kind == "range":
        _require_text(window.boot_id, "window.boot_id")
        start = validate_sqlite_integer(window.start_mono_ns, "window.start_mono_ns")
        end = validate_sqlite_integer(window.end_mono_ns, "window.end_mono_ns")
        if start >= end:
            raise ValueError(
                f"window.start_mono_ns must be less than window.end_mono_ns, got {start} >= {end}"
            )
        if window.launch_id is not None:
            raise ValueError("a range window must not carry launch_id")
        return window

    if kind == "since_boot":
        _require_text(window.boot_id, "window.boot_id")
        if window.start_mono_ns is not None:
            raise ValueError("a since_boot window must not carry start_mono_ns")
        _require_positive(window.end_mono_ns, "window.end_mono_ns")
        if window.launch_id is not None:
            raise ValueError("a since_boot window must not carry launch_id")
        return window

    if kind == "launch":
        _require_text(window.boot_id, "window.boot_id")
        _require_positive(window.launch_id, "window.launch_id")
        if window.start_mono_ns is not None or window.end_mono_ns is not None:
            raise ValueError("a launch window must not carry monotonic endpoints")
        return window

    # kind == "lifetime"
    if window.boot_id is not None:
        raise ValueError("a lifetime window must not carry boot_id")
    if window.start_mono_ns is not None or window.end_mono_ns is not None:
        raise ValueError("a lifetime window must not carry monotonic endpoints")
    if window.launch_id is not None:
        raise ValueError("a lifetime window must not carry launch_id")
    return window


@dataclass(frozen=True)
class HookHits:
    """Observed BPF hook hits. Zero means no observed hit, not proof of none."""

    vfs_read: int
    vfs_write: int
    vfs_readv: int
    vfs_writev: int
    vfs_iter_read: int
    vfs_iter_write: int

    def __post_init__(self) -> None:
        for name in _HOOK_NAMES:
            validate_sqlite_integer(getattr(self, name), f"hook_hits.{name}")


@dataclass(frozen=True)
class Coverage:
    """Stored coverage quantities plus the query-derived tier and completeness."""

    tier: Tier
    complete: bool
    unavailable_reason: UnavailableReason | None
    inaccessible_pids: int
    resets: int
    reap_corrections: int
    reap_unapplied: CounterVector
    reap_unobserved: CounterVector
    partial_intervals: int
    gap_intervals: int
    overlap_uncertain_intervals: int
    spans_suspend_intervals: int
    spans_suspend: bool
    reap_incomplete: bool
    counter_overflow: bool
    device_state: DeviceState
    counted_omitted_files: int
    counted_omitted_bytes_r: int
    counted_omitted_bytes_w: int
    uncounted_overflow_files: int
    uncounted_overflow_bytes_r: int
    uncounted_overflow_bytes_w: int
    skipped_nonfile: int
    skipped_unsupported_fs: int
    weak_identity_rows: int
    hook_hits: HookHits

    def __post_init__(self) -> None:
        validate_enum(self.tier, _TIER_VALUES, "coverage.tier")
        _require_bool(self.complete, "coverage.complete")
        if self.unavailable_reason is not None:
            validate_enum(
                self.unavailable_reason, _UNAVAILABLE_REASONS, "coverage.unavailable_reason"
            )
        for name in (
            "inaccessible_pids",
            "resets",
            "reap_corrections",
            "partial_intervals",
            "gap_intervals",
            "overlap_uncertain_intervals",
            "spans_suspend_intervals",
            "counted_omitted_files",
            "counted_omitted_bytes_r",
            "counted_omitted_bytes_w",
            "uncounted_overflow_files",
            "uncounted_overflow_bytes_r",
            "uncounted_overflow_bytes_w",
            "skipped_nonfile",
            "skipped_unsupported_fs",
            "weak_identity_rows",
        ):
            validate_sqlite_integer(getattr(self, name), f"coverage.{name}")
        _require_counter_vector(self.reap_unapplied, "coverage.reap_unapplied")
        _require_counter_vector(self.reap_unobserved, "coverage.reap_unobserved")
        _require_bool(self.spans_suspend, "coverage.spans_suspend")
        _require_bool(self.reap_incomplete, "coverage.reap_incomplete")
        _require_bool(self.counter_overflow, "coverage.counter_overflow")
        validate_enum(self.device_state, _DEVICE_STATES, "coverage.device_state")
        if not isinstance(self.hook_hits, HookHits):
            raise ValueError("coverage.hook_hits must be a HookHits")


@dataclass(frozen=True)
class AppTotal:
    """One app's task-accounted totals for a ranking query."""

    app_id: int
    app_key: str
    name: str
    read_bytes: int
    write_bytes: int
    total_bytes: int
    rchar: int
    wchar: int
    peak_read_bps: int | None
    peak_write_bps: int | None
    peak_total_bps: int | None
    coverage: Coverage

    def __post_init__(self) -> None:
        _require_positive(self.app_id, "app_total.app_id")
        _require_text(self.app_key, "app_total.app_key")
        if not isinstance(self.name, str):
            raise ValueError("app_total.name must be a string")
        read_bytes = validate_sqlite_integer(self.read_bytes, "app_total.read_bytes")
        write_bytes = validate_sqlite_integer(self.write_bytes, "app_total.write_bytes")
        total_bytes = validate_sqlite_integer(self.total_bytes, "app_total.total_bytes")
        validate_sqlite_integer(self.rchar, "app_total.rchar")
        validate_sqlite_integer(self.wchar, "app_total.wchar")
        if total_bytes != read_bytes + write_bytes:
            raise ValueError(
                "app_total.total_bytes must equal read_bytes + write_bytes, "
                f"got {total_bytes} != {read_bytes} + {write_bytes}"
            )
        for name in ("peak_read_bps", "peak_write_bps", "peak_total_bps"):
            value = getattr(self, name)
            if value is not None:
                validate_sqlite_integer(value, f"app_total.{name}")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("app_total.coverage must be a Coverage")


@dataclass(frozen=True)
class SeriesPoint:
    """One task-series bucket. Peaks are ``None`` when no rate is defined."""

    bucket_start_mono_ns: int
    bucket_secs: int
    wall_start_seconds: float | None
    read_bytes: int
    write_bytes: int
    peak_read_bps: int | None
    peak_write_bps: int | None
    peak_total_bps: int | None
    spans_suspend: bool

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.bucket_start_mono_ns, "series_point.bucket_start_mono_ns")
        _require_positive(self.bucket_secs, "series_point.bucket_secs")
        if self.wall_start_seconds is not None:
            if isinstance(self.wall_start_seconds, bool) or not isinstance(
                self.wall_start_seconds, (int, float)
            ):
                raise ValueError("series_point.wall_start_seconds must be a number or None")
        validate_sqlite_integer(self.read_bytes, "series_point.read_bytes")
        validate_sqlite_integer(self.write_bytes, "series_point.write_bytes")
        for name in ("peak_read_bps", "peak_write_bps", "peak_total_bps"):
            value = getattr(self, name)
            if value is not None:
                validate_sqlite_integer(value, f"series_point.{name}")
        _require_bool(self.spans_suspend, "series_point.spans_suspend")


@dataclass(frozen=True)
class DeviceSeriesPoint:
    """One device-series bucket, keyed by the raw ``major:minor`` token."""

    bucket_start_mono_ns: int
    bucket_secs: int
    dev: str
    rbytes: int
    wbytes: int

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.bucket_start_mono_ns, "device_series_point.bucket_start_mono_ns")
        _require_positive(self.bucket_secs, "device_series_point.bucket_secs")
        _require_text(self.dev, "device_series_point.dev")
        validate_sqlite_integer(self.rbytes, "device_series_point.rbytes")
        validate_sqlite_integer(self.wbytes, "device_series_point.wbytes")


@dataclass(frozen=True)
class ResidualPoint:
    """Task-minus-device residual for one bucket; ``None`` where D is unavailable."""

    bucket_start_mono_ns: int
    bucket_secs: int
    total: int | None
    read: int | None
    write: int | None

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.bucket_start_mono_ns, "residual_point.bucket_start_mono_ns")
        _require_positive(self.bucket_secs, "residual_point.bucket_secs")
        _require_signed(self.total, "residual_point.total")
        _require_signed(self.read, "residual_point.read")
        _require_signed(self.write, "residual_point.write")


@dataclass(frozen=True)
class Series:
    """A task series with its device and residual companions."""

    app_id: int
    window: Window
    tier: Tier
    points: tuple[SeriesPoint, ...]
    device_points: tuple[DeviceSeriesPoint, ...]
    residual_points: tuple[ResidualPoint, ...]
    coverage: Coverage

    def __post_init__(self) -> None:
        _require_positive(self.app_id, "series.app_id")
        if not isinstance(self.window, Window):
            raise ValueError("series.window must be a Window")
        validate_enum(self.tier, _TIER_VALUES, "series.tier")
        _require_tuple_of(self.points, SeriesPoint, "series.points")
        _require_tuple_of(self.device_points, DeviceSeriesPoint, "series.device_points")
        _require_tuple_of(self.residual_points, ResidualPoint, "series.residual_points")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("series.coverage must be a Coverage")


@dataclass(frozen=True)
class DeviceShareRow:
    """One device's share for one boot; ``major:minor`` is boot-scoped."""

    boot_id: str
    dev: str
    rbytes: int
    wbytes: int
    total_bytes: int

    def __post_init__(self) -> None:
        _require_text(self.boot_id, "device_share_row.boot_id")
        _require_text(self.dev, "device_share_row.dev")
        rbytes = validate_sqlite_integer(self.rbytes, "device_share_row.rbytes")
        wbytes = validate_sqlite_integer(self.wbytes, "device_share_row.wbytes")
        total = validate_sqlite_integer(self.total_bytes, "device_share_row.total_bytes")
        if total != rbytes + wbytes:
            raise ValueError(
                "device_share_row.total_bytes must equal rbytes + wbytes, "
                f"got {total} != {rbytes} + {wbytes}"
            )


@dataclass(frozen=True)
class DiskShare:
    """Task versus device split for one app and window."""

    app_id: int
    window: Window
    task_read_bytes: int
    task_write_bytes: int
    task_total_bytes: int
    devices: tuple[DeviceShareRow, ...]
    device_read_bytes: int | None
    device_write_bytes: int | None
    device_total_bytes: int | None
    residual_read: int | None
    residual_write: int | None
    residual_total: int | None
    coverage: Coverage

    def __post_init__(self) -> None:
        _require_positive(self.app_id, "disk_share.app_id")
        if not isinstance(self.window, Window):
            raise ValueError("disk_share.window must be a Window")
        read_bytes = validate_sqlite_integer(self.task_read_bytes, "disk_share.task_read_bytes")
        write_bytes = validate_sqlite_integer(self.task_write_bytes, "disk_share.task_write_bytes")
        total = validate_sqlite_integer(self.task_total_bytes, "disk_share.task_total_bytes")
        if total != read_bytes + write_bytes:
            raise ValueError(
                "disk_share.task_total_bytes must equal task_read_bytes + task_write_bytes, "
                f"got {total} != {read_bytes} + {write_bytes}"
            )
        _require_tuple_of(self.devices, DeviceShareRow, "disk_share.devices")
        for name in ("device_read_bytes", "device_write_bytes", "device_total_bytes"):
            value = getattr(self, name)
            if value is not None:
                validate_sqlite_integer(value, f"disk_share.{name}")
        for name in ("residual_read", "residual_write", "residual_total"):
            _require_signed(getattr(self, name), f"disk_share.{name}")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("disk_share.coverage must be a Coverage")


@dataclass(frozen=True)
class FileRow:
    """One retained file observation, with ``bytes_total = bytes_r + bytes_w``."""

    path_id: int
    basename: str
    state: FileState
    identity_weak: bool
    dev: str
    inode: int
    generation: int
    reads: int
    writes: int
    bytes_r: int
    bytes_w: int
    bytes_total: int
    last_seen_boot_id: str
    last_seen_mono_ns: int

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.path_id, "file_row.path_id")
        if not isinstance(self.basename, str):
            raise ValueError("file_row.basename must be a string")
        validate_enum(self.state, _FILE_STATES, "file_row.state")
        _require_bool(self.identity_weak, "file_row.identity_weak")
        _require_text(self.dev, "file_row.dev")
        validate_sqlite_integer(self.inode, "file_row.inode")
        validate_sqlite_integer(self.generation, "file_row.generation")
        validate_sqlite_integer(self.reads, "file_row.reads")
        validate_sqlite_integer(self.writes, "file_row.writes")
        bytes_r = validate_sqlite_integer(self.bytes_r, "file_row.bytes_r")
        bytes_w = validate_sqlite_integer(self.bytes_w, "file_row.bytes_w")
        bytes_total = validate_sqlite_integer(self.bytes_total, "file_row.bytes_total")
        if bytes_total != bytes_r + bytes_w:
            raise ValueError(
                "file_row.bytes_total must equal bytes_r + bytes_w, "
                f"got {bytes_total} != {bytes_r} + {bytes_w}"
            )
        _require_text(self.last_seen_boot_id, "file_row.last_seen_boot_id")
        validate_sqlite_integer(self.last_seen_mono_ns, "file_row.last_seen_mono_ns")


@dataclass(frozen=True)
class UnknownOwnerOverflow:
    """Overflow that cannot be assigned to any app. Never added to app totals."""

    window: Window
    owner_key: Literal["overflow_owner_unknown"]
    overflow_files: int
    overflow_bytes_r: int
    overflow_bytes_w: int
    coverage: Coverage

    def __post_init__(self) -> None:
        if not isinstance(self.window, Window):
            raise ValueError("unknown_owner_overflow.window must be a Window")
        if self.owner_key != "overflow_owner_unknown":
            raise ValueError(
                "unknown_owner_overflow.owner_key must be 'overflow_owner_unknown'"
            )
        validate_sqlite_integer(self.overflow_files, "unknown_owner_overflow.overflow_files")
        validate_sqlite_integer(self.overflow_bytes_r, "unknown_owner_overflow.overflow_bytes_r")
        validate_sqlite_integer(self.overflow_bytes_w, "unknown_owner_overflow.overflow_bytes_w")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("unknown_owner_overflow.coverage must be a Coverage")


@dataclass(frozen=True)
class FileTable:
    """Sorted, searched file rows plus the window's unknown-owner context."""

    app_id: int
    window: Window
    rows: tuple[FileRow, ...]
    sort: FileSort
    direction: Direction
    search: str | None
    unknown_owner_overflow: UnknownOwnerOverflow
    coverage: Coverage

    def __post_init__(self) -> None:
        _require_positive(self.app_id, "file_table.app_id")
        if not isinstance(self.window, Window):
            raise ValueError("file_table.window must be a Window")
        _require_tuple_of(self.rows, FileRow, "file_table.rows")
        validate_enum(self.sort, _FILE_SORTS, "file_table.sort")
        validate_enum(self.direction, _DIRECTIONS, "file_table.direction")
        validate_search(self.search)
        if not isinstance(self.unknown_owner_overflow, UnknownOwnerOverflow):
            raise ValueError(
                "file_table.unknown_owner_overflow must be an UnknownOwnerOverflow"
            )
        if not isinstance(self.coverage, Coverage):
            raise ValueError("file_table.coverage must be a Coverage")


@dataclass(frozen=True)
class FileCoverage:
    """File-source coverage for one app and window."""

    app_id: int
    window: Window
    counted_omitted_files: int
    counted_omitted_bytes_r: int
    counted_omitted_bytes_w: int
    uncounted_overflow_files: int
    uncounted_overflow_bytes_r: int
    uncounted_overflow_bytes_w: int
    skipped_nonfile: int
    skipped_unsupported_fs: int
    weak_identity_rows: int
    hook_hits: HookHits
    observed_bytes_r: int
    observed_bytes_w: int
    unknown_owner_overflow: UnknownOwnerOverflow
    coverage: Coverage

    @property
    def served_interval(self) -> Window | None:
        if (self.coverage.unavailable_reason is not None or
                self.unknown_owner_overflow.coverage.unavailable_reason is not None):
            return None
        return self.unknown_owner_overflow.window

    def __post_init__(self) -> None:
        _require_positive(self.app_id, "file_coverage.app_id")
        if not isinstance(self.window, Window):
            raise ValueError("file_coverage.window must be a Window")
        for name in (
            "counted_omitted_files",
            "counted_omitted_bytes_r",
            "counted_omitted_bytes_w",
            "uncounted_overflow_files",
            "uncounted_overflow_bytes_r",
            "uncounted_overflow_bytes_w",
            "skipped_nonfile",
            "skipped_unsupported_fs",
            "weak_identity_rows",
            "observed_bytes_r",
            "observed_bytes_w",
        ):
            validate_sqlite_integer(getattr(self, name), f"file_coverage.{name}")
        if not isinstance(self.hook_hits, HookHits):
            raise ValueError("file_coverage.hook_hits must be a HookHits")
        if not isinstance(self.unknown_owner_overflow, UnknownOwnerOverflow):
            raise ValueError(
                "file_coverage.unknown_owner_overflow must be an UnknownOwnerOverflow"
            )
        if not isinstance(self.coverage, Coverage):
            raise ValueError("file_coverage.coverage must be a Coverage")


# --- Transport records for synthetic future-producer data --------------------
#
# These carry validated observations between the (future) producer and the store.
# They contain no database-local identifiers and load no BPF.


@dataclass(frozen=True)
class FileObservation:
    """One file's observations inside one app-hour."""

    app_key: str
    fs_uuid: str
    dev: str
    inode: int
    generation: int
    basename: str
    state: FileState
    identity_weak: bool
    reads: int
    writes: int
    bytes_r: int
    bytes_w: int
    last_seen_mono_ns: int

    def __post_init__(self) -> None:
        _require_text(self.app_key, "file_observation.app_key")
        _require_bool(self.identity_weak, "file_observation.identity_weak")
        validate_file_uuid(self.fs_uuid, self.identity_weak, "file_observation")
        _require_text(self.dev, "file_observation.dev")
        validate_sqlite_integer(self.inode, "file_observation.inode")
        validate_sqlite_integer(self.generation, "file_observation.generation")
        if not isinstance(self.basename, str):
            raise ValueError("file_observation.basename must be a string")
        validate_enum(self.state, _FILE_STATES, "file_observation.state")
        _require_bool(self.identity_weak, "file_observation.identity_weak")
        validate_sqlite_integer(self.reads, "file_observation.reads")
        validate_sqlite_integer(self.writes, "file_observation.writes")
        validate_sqlite_integer(self.bytes_r, "file_observation.bytes_r")
        validate_sqlite_integer(self.bytes_w, "file_observation.bytes_w")
        validate_sqlite_integer(self.last_seen_mono_ns, "file_observation.last_seen_mono_ns")


@dataclass(frozen=True)
class FileCoverageInput:
    """Per-app-hour file coverage as supplied by the producer."""

    counted_omitted_files: int
    counted_omitted_bytes_r: int
    counted_omitted_bytes_w: int
    uncounted_overflow_files: int
    uncounted_overflow_bytes_r: int
    uncounted_overflow_bytes_w: int
    skipped_nonfile: int
    skipped_unsupported_fs: int
    weak_identity_rows: int
    hook_hits: HookHits
    gap_intervals: int
    overlap_uncertain_intervals: int
    complete: bool

    def __post_init__(self) -> None:
        for name in (
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
        ):
            validate_sqlite_integer(getattr(self, name), f"file_coverage_input.{name}")
        if not isinstance(self.hook_hits, HookHits):
            raise ValueError("file_coverage_input.hook_hits must be a HookHits")
        _require_bool(self.complete, "file_coverage_input.complete")


@dataclass(frozen=True)
class UnknownOwnerInput:
    """Unknown-owner overflow as supplied by the producer for one window or hour."""

    overflow_files: int
    overflow_bytes_r: int
    overflow_bytes_w: int
    gap_intervals: int
    overlap_uncertain_intervals: int
    complete: bool

    def __post_init__(self) -> None:
        for name in (
            "overflow_files",
            "overflow_bytes_r",
            "overflow_bytes_w",
            "gap_intervals",
            "overlap_uncertain_intervals",
        ):
            validate_sqlite_integer(getattr(self, name), f"unknown_owner_input.{name}")
        _require_bool(self.complete, "unknown_owner_input.complete")


@dataclass(frozen=True)
class HourlyFileSnapshot:
    """One finalized ``(boot, AppKey, hour)`` snapshot.

    ``apps`` is ordered by ``AppKey`` bytes and each entry is
    ``(app_key, observations, coverage)``. The store's canonical JSON encoder
    turns this into the version-1 file-hour state.
    """

    boot_id: str
    hour_start_mono_ns: int
    apps: tuple[tuple[AppKey, tuple[FileObservation, ...], FileCoverageInput], ...]
    unknown_owner: UnknownOwnerInput

    def __post_init__(self) -> None:
        _require_text(self.boot_id, "hourly_file_snapshot.boot_id")
        validate_sqlite_integer(self.hour_start_mono_ns, "hourly_file_snapshot.hour_start_mono_ns")
        if not isinstance(self.apps, tuple):
            raise ValueError("hourly_file_snapshot.apps must be a tuple")
        for index, entry in enumerate(self.apps):
            if not isinstance(entry, tuple) or len(entry) != 3:
                raise ValueError(
                    f"hourly_file_snapshot.apps[{index}] must be (app_key, observations, coverage)"
                )
            app_key, observations, coverage = entry
            _require_text(app_key, f"hourly_file_snapshot.apps[{index}].app_key")
            _require_tuple_of(
                observations, FileObservation, f"hourly_file_snapshot.apps[{index}].observations"
            )
            if not isinstance(coverage, FileCoverageInput):
                raise ValueError(
                    f"hourly_file_snapshot.apps[{index}].coverage must be a FileCoverageInput"
                )
        if not isinstance(self.unknown_owner, UnknownOwnerInput):
            raise ValueError("hourly_file_snapshot.unknown_owner must be an UnknownOwnerInput")


def _require_tuple_of(value: object, item_type: type, field: str) -> tuple[object, ...]:
    """Reject a non-tuple or a tuple holding the wrong element type."""

    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise ValueError(f"{field}[{index}] must be a {item_type.__name__}")
    return value
