"""Qt-free fixtures for the live Overview lane.

Only the standard library and the frozen P1 contracts appear here, so importing
this module never pulls in Qt, a client, a router or a query service. The lane's
later harness imports these factories to build committed-value fixtures whose
fields stay exactly the production contract fields.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from appio.contracts import (
    AppTotal,
    Coverage,
    DeviceSeriesPoint,
    DeviceShareRow,
    DiskShare,
    HookHits,
    ResidualPoint,
    Series,
    SeriesPoint,
    Tier,
    Window,
)

__all__ = [
    "Clock",
    "coverage",
    "series",
    "share",
    "total",
]

DEFAULT_KEY = "unit:alpha.service"

#: One device split used by :func:`share`; 9 + 5 == 14, the §12.5 device total.
_DEVICE_READ = 9
_DEVICE_WRITE = 5

#: Task bytes for the normative raw series fixture (7 + 3 == 10 per bucket).
_TASK_READ = 7
_TASK_WRITE = 3

#: Per-device bytes (2 + 1 == 3, two devices per bucket == 6 of the task 10).
_DEVICE_POINT_READ = 2
_DEVICE_POINT_WRITE = 1


@dataclass
class Clock:
    """Injected monotonic nanosecond clock: the lane's only age time source."""

    ns: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.ns, bool) or not isinstance(self.ns, int) or self.ns < 0:
            raise ValueError("clock.ns must be a nonnegative integer")

    def __call__(self) -> int:
        return self.ns

    def advance_ns(self, delta: int) -> None:
        if isinstance(delta, bool) or not isinstance(delta, int) or delta < 0:
            raise ValueError("clock.advance_ns requires a nonnegative integer delta")
        self.ns += delta


def coverage(**changes: object) -> Coverage:
    """A complete, available raw-tier Coverage, with explicit overrides."""

    base = Coverage(
        tier="raw",
        complete=True,
        unavailable_reason=None,
        inaccessible_pids=0,
        resets=0,
        reap_corrections=0,
        reap_unapplied=(0, 0, 0, 0),
        reap_unobserved=(0, 0, 0, 0),
        partial_intervals=0,
        gap_intervals=0,
        overlap_uncertain_intervals=0,
        spans_suspend_intervals=0,
        spans_suspend=False,
        reap_incomplete=False,
        counter_overflow=False,
        device_state="available",
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
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def total(
    app_id: int,
    key: str,
    *,
    read: int = 17,
    write: int = 5,
    peak: int | None = None,
) -> AppTotal:
    """One ranking row whose subject matches ``key`` exactly."""

    return AppTotal(
        app_id=app_id,
        app_key=key,
        name=key.partition(":")[2] or key,
        read_bytes=read,
        write_bytes=write,
        total_bytes=read + write,
        rchar=read,
        wchar=write,
        peak_read_bps=peak,
        peak_write_bps=peak,
        peak_total_bps=peak,
        coverage=coverage(),
    )


def share(
    app_id: int,
    window: Window,
    *,
    read: int = 17,
    write: int = 5,
    device: bool = True,
) -> DiskShare:
    """A task/device split with valid matching device rows and residuals."""

    if not isinstance(window, Window):
        raise ValueError("share requires a Window")
    boot = window.boot_id if window.boot_id is not None else "boot-a"
    if device:
        devices = (
            DeviceShareRow(
                boot_id=boot,
                dev="8:0",
                rbytes=_DEVICE_READ,
                wbytes=_DEVICE_WRITE,
                total_bytes=_DEVICE_READ + _DEVICE_WRITE,
            ),
        )
        device_read: int | None = _DEVICE_READ
        device_write: int | None = _DEVICE_WRITE
        device_total: int | None = _DEVICE_READ + _DEVICE_WRITE
        residual_read: int | None = read - _DEVICE_READ
        residual_write: int | None = write - _DEVICE_WRITE
        residual_total: int | None = residual_read + residual_write
        state = "available"
    else:
        devices = ()
        device_read = device_write = device_total = None
        residual_read = residual_write = residual_total = None
        state = "not_delegated"
    return DiskShare(
        app_id=app_id,
        window=window,
        task_read_bytes=read,
        task_write_bytes=write,
        task_total_bytes=read + write,
        devices=devices,
        device_read_bytes=device_read,
        device_write_bytes=device_write,
        device_total_bytes=device_total,
        residual_read=residual_read,
        residual_write=residual_write,
        residual_total=residual_total,
        coverage=coverage(device_state=state),
    )


def series(app_id: int, window: Window, *, tier: Tier = "raw") -> Series:
    """The §5.4 normative raw fixture: two buckets, two simultaneous devices.

    Device order is ``(0, 8:0), (0, 8:16), (10s, 8:0), (10s, 8:16)``: two
    different devices share a bucket while every single device sequence keeps
    its own strict bucket order.
    """

    if not isinstance(window, Window):
        raise ValueError("series requires a Window")
    if tier == "totals_only":
        return Series(
            app_id=app_id,
            window=window,
            tier=tier,
            points=(),
            device_points=(),
            residual_points=(),
            coverage=coverage(tier=tier),
        )
    if tier not in ("raw", "rollup_60s", "rollup_3600s"):
        raise ValueError(f"unsupported series tier {tier!r}")
    secs = {"raw": 10, "rollup_60s": 60, "rollup_3600s": 3600}[tier]
    starts = (0, secs * 10**9)
    points = tuple(
        SeriesPoint(
            bucket_start_mono_ns=start,
            bucket_secs=secs,
            wall_start_seconds=1_700_000_000.0 + start / 1_000_000_000,
            read_bytes=_TASK_READ,
            write_bytes=_TASK_WRITE,
            peak_read_bps=None,
            peak_write_bps=None,
            peak_total_bps=None,
            spans_suspend=False,
        )
        for start in starts
    )
    device_points = tuple(
        DeviceSeriesPoint(
            bucket_start_mono_ns=start,
            bucket_secs=secs,
            dev=dev,
            rbytes=_DEVICE_POINT_READ,
            wbytes=_DEVICE_POINT_WRITE,
        )
        for start in starts
        for dev in ("8:0", "8:16")
    )
    device_total = 2 * (_DEVICE_POINT_READ + _DEVICE_POINT_WRITE)
    device_read = 2 * _DEVICE_POINT_READ
    device_write = 2 * _DEVICE_POINT_WRITE
    residual_points = tuple(
        ResidualPoint(
            bucket_start_mono_ns=start,
            bucket_secs=secs,
            total=(_TASK_READ + _TASK_WRITE) - device_total,
            read=_TASK_READ - device_read,
            write=_TASK_WRITE - device_write,
        )
        for start in starts
    )
    return Series(
        app_id=app_id,
        window=window,
        tier=tier,
        points=points,
        device_points=device_points,
        residual_points=residual_points,
        coverage=coverage(tier=tier),
    )
