"""Cached, lossless chart widgets for the appio console.

The time lanes aggregate source buckets into at most one column per logical
pixel, but keep the raw run membership beside the columns: a column envelope is
never treated as proof of continuity. ``paintEvent`` only draws cached
geometry, so a repaint performs no I/O, source scan or per-source allocation.

This module keeps its import surface at the standard library, PyQt6, the
contracts and the local theme constants; its byte formatter is the private
transcription of the display algorithm below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QPaintEvent,
    QPainter,
    QPalette,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PyQt6.QtWidgets import QLabel, QSizePolicy, QWidget

from appio.contracts import DeviceSeriesPoint, SeriesPoint
from appio.ui import theme

_NANOSECONDS = 10**9
_TOTAL_CAPTION = "TOTAL · read above, write below"
_NO_SAMPLES = "no samples in this window"
_NO_TASK_IO = "no task I/O in this window"
_DONUT_CAPTION = "READ vs WRITE"
_SHARE_CAPTION = "ATTRIBUTED SHARE BY DISK"
_UNAVAILABLE_PREFIX = "unavailable: "
_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def _format_bytes(value: int) -> str:
    """Render an exact byte count with binary units, matching the display oracle."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("value must be an integer")
    units = _UNITS
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude < 1024:
        return f"{sign}{magnitude} B"
    unit, scale = 0, 1
    while unit < len(units) - 1 and magnitude >= scale * 1024:
        unit += 1
        scale *= 1024
    whole, fraction = divmod((magnitude * 10) // scale, 10)
    if fraction:
        return f"{sign}{whole}.{fraction} {units[unit]}"
    return f"{sign}{whole} {units[unit]}"


def _require_int(value: object, field: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _require_finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


# --------------------------------------------------------------------------
# Frozen observations (spec §2.1).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChartColumn:
    """One drawn column after lossless downsampling.

    A column may cover several source runs; ``starts_run`` says a run begins
    somewhere inside it, and its envelope is not proof of continuity.
    """

    start_mono_ns: int
    end_mono_ns: int
    read_bytes: int
    write_bytes: int
    peak_read_bps: int | None
    peak_write_bps: int | None
    starts_run: bool

    def __post_init__(self) -> None:
        start = _require_int(self.start_mono_ns, "chart_column.start_mono_ns", minimum=0)
        end = _require_int(self.end_mono_ns, "chart_column.end_mono_ns", minimum=0)
        if start >= end:
            raise ValueError("chart_column.start_mono_ns must be less than end_mono_ns")
        _require_int(self.read_bytes, "chart_column.read_bytes", minimum=0)
        _require_int(self.write_bytes, "chart_column.write_bytes", minimum=0)
        for name in ("peak_read_bps", "peak_write_bps"):
            value = getattr(self, name)
            if value is not None:
                _require_int(value, f"chart_column.{name}", minimum=0)
        if not isinstance(self.starts_run, bool):
            raise ValueError("chart_column.starts_run must be a boolean")


@dataclass(frozen=True)
class ChartSegment:
    """One closed step-fill polygon for one source run and one side."""

    side: Literal["read", "write"]
    run_index: int
    polygon: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if self.side not in ("read", "write"):
            raise ValueError("chart_segment.side must be 'read' or 'write'")
        _require_int(self.run_index, "chart_segment.run_index", minimum=0)
        if not isinstance(self.polygon, tuple):
            raise ValueError("chart_segment.polygon must be a tuple")
        if len(self.polygon) < 4:
            raise ValueError("chart_segment.polygon must have at least four points")
        points: list[tuple[float, float]] = []
        for pair in self.polygon:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("chart_segment.polygon must contain (x, y) pairs")
            x = _require_finite(pair[0], "chart_segment.x")
            y = _require_finite(pair[1], "chart_segment.y")
            points.append((x, y))
        if points[0] != points[-1]:
            raise ValueError("chart_segment.polygon must close on its first point")
        object.__setattr__(self, "polygon", tuple(points))


class _Sample:
    """One validated source bucket in chart-local form.

    Every source field is retained verbatim so full-sequence equality can
    distinguish ``None`` from zero, a defined ``peak_total_bps`` from an
    absent one, and a wall-clock stamp from its absence even though the
    plotted geometry ignores those fields.
    """

    __slots__ = (
        "start",
        "end",
        "read_bytes",
        "write_bytes",
        "peak_read_bps",
        "peak_write_bps",
        "peak_total_bps",
        "wall_start_seconds",
        "spans_suspend",
        "starts_run",
    )

    def __init__(
        self,
        start: int,
        end: int,
        read_bytes: int,
        write_bytes: int,
        peak_read_bps: int | None,
        peak_write_bps: int | None,
        peak_total_bps: int | None,
        wall_start_seconds: float | None,
        spans_suspend: bool,
        starts_run: bool,
    ) -> None:
        self.start = start
        self.end = end
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes
        self.peak_read_bps = peak_read_bps
        self.peak_write_bps = peak_write_bps
        self.peak_total_bps = peak_total_bps
        self.wall_start_seconds = wall_start_seconds
        self.spans_suspend = spans_suspend
        self.starts_run = starts_run


def _point_sequence(points: object) -> tuple[object, ...]:
    """Copy the incoming sequence once, before any retained state changes."""

    if isinstance(points, (str, bytes)):
        raise ValueError("points must be a sequence of chart points")
    try:
        return tuple(points)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("points must be a sequence of chart points") from None


def _admit_task_point(point: object) -> SeriesPoint:
    """Validate one incoming task point's frozen type.

    This is the single admission primitive charged once per incoming point;
    it deliberately sits on the shared validation path so an alternate
    full-prefix reducer cannot bypass the cost observation.
    """

    if not isinstance(point, SeriesPoint):
        raise ValueError("task time lanes require SeriesPoint values")
    return point


def _admit_device_point(point: object) -> DeviceSeriesPoint:
    """Validate one incoming device point's frozen type."""

    if not isinstance(point, DeviceSeriesPoint):
        raise ValueError("device time lanes require DeviceSeriesPoint values")
    return point


def _task_samples(points: Sequence[SeriesPoint]) -> tuple[_Sample, ...]:
    samples: list[_Sample] = []
    previous_end: int | None = None
    previous_suspend = False
    for incoming in _point_sequence(points):
        point = _admit_task_point(incoming)
        start = point.bucket_start_mono_ns
        end = start + point.bucket_secs * _NANOSECONDS
        if previous_end is not None and start < previous_end:
            raise ValueError("series points must be ordered and non-overlapping")
        starts_run = previous_end is None or start != previous_end or previous_suspend
        samples.append(
            _Sample(
                start,
                end,
                point.read_bytes,
                point.write_bytes,
                point.peak_read_bps,
                point.peak_write_bps,
                point.peak_total_bps,
                point.wall_start_seconds,
                point.spans_suspend,
                starts_run,
            )
        )
        previous_end = end
        previous_suspend = point.spans_suspend
    return tuple(samples)


def _device_samples(points: Sequence[DeviceSeriesPoint]) -> tuple[_Sample, ...]:
    samples: list[_Sample] = []
    previous_end: int | None = None
    for incoming in _point_sequence(points):
        point = _admit_device_point(incoming)
        start = point.bucket_start_mono_ns
        end = start + point.bucket_secs * _NANOSECONDS
        if previous_end is not None and start < previous_end:
            raise ValueError("device points must be ordered and non-overlapping")
        # Device buckets carry no suspension metadata: only missing buckets split.
        samples.append(
            _Sample(
                start,
                end,
                point.rbytes,
                point.wbytes,
                None,
                None,
                None,
                None,
                False,
                previous_end is None or start != previous_end,
            )
        )
        previous_end = end
    return tuple(samples)


def _samples_equal_at(
    left: tuple[_Sample, ...], right: tuple[_Sample, ...], index: int
) -> bool:
    """Full raw contract equality for one accepted bucket, without tolerance."""

    first = left[index]
    second = right[index]
    return (
        first.start == second.start
        and first.end == second.end
        and first.read_bytes == second.read_bytes
        and first.write_bytes == second.write_bytes
        and first.peak_read_bps == second.peak_read_bps
        and first.peak_write_bps == second.peak_write_bps
        and first.peak_total_bps == second.peak_total_bps
        and first.wall_start_seconds == second.wall_start_seconds
        and first.spans_suspend == second.spans_suspend
    )


def _matching_prefix(
    old: tuple[_Sample, ...], new: tuple[_Sample, ...]
) -> int:
    """Return the length of the exact full-contract common prefix."""

    limit = min(len(old), len(new))
    index = 0
    while index < limit and _samples_equal_at(old, new, index):
        index += 1
    return index


def _defined_max(left: int | None, right: int | None) -> int | None:
    """Combine two optional peaks: two ``None`` values stay undefined."""

    if left is None:
        return right
    if right is None:
        return left
    return left if left >= right else right


class _Group:
    """One retained aggregate column.

    Only two integer bounds are stored per column; there is deliberately no
    member-index vector, so promotion cannot copy O(n) membership.
    """

    __slots__ = (
        "first_index",
        "last_index",
        "start",
        "end",
        "read_bytes",
        "write_bytes",
        "peak_read_bps",
        "peak_write_bps",
        "starts_run",
        "duration_ns",
        "intervals",
        "first_run",
        "last_run",
    )

    def __init__(
        self,
        *,
        first_index: int,
        last_index: int,
        start: int,
        end: int,
        read_bytes: int,
        write_bytes: int,
        peak_read_bps: int | None,
        peak_write_bps: int | None,
        starts_run: bool,
        duration_ns: int,
        intervals: tuple[tuple[int, int, int], ...],
        first_run: int,
        last_run: int,
    ) -> None:
        self.first_index = first_index
        self.last_index = last_index
        self.start = start
        self.end = end
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes
        self.peak_read_bps = peak_read_bps
        self.peak_write_bps = peak_write_bps
        self.starts_run = starts_run
        self.duration_ns = duration_ns
        self.intervals = intervals
        self.first_run = first_run
        self.last_run = last_run


def _aggregate_members(
    samples: tuple[_Sample, ...], first: int, last: int, first_run: int
) -> _Group:
    """Build one group from its raw member bounds, in time order."""

    sample = samples[first]
    start = sample.start
    end = sample.end
    read_bytes = 0
    write_bytes = 0
    peak_read: int | None = None
    peak_write: int | None = None
    duration = 0
    starts_run = False
    run = first_run
    intervals: list[tuple[int, int, int]] = []
    for index in range(first, last + 1):
        current = samples[index]
        if index > first and current.starts_run:
            run += 1
        if current.start < start:
            start = current.start
        if current.end > end:
            end = current.end
        read_bytes += current.read_bytes
        write_bytes += current.write_bytes
        peak_read = _defined_max(peak_read, current.peak_read_bps)
        peak_write = _defined_max(peak_write, current.peak_write_bps)
        duration += current.end - current.start
        starts_run = starts_run or current.starts_run
        if (
            intervals
            and intervals[-1][0] == run
            and intervals[-1][2] == current.start
        ):
            previous = intervals[-1]
            intervals[-1] = (previous[0], previous[1], current.end)
        else:
            intervals.append((run, current.start, current.end))
    return _Group(
        first_index=first,
        last_index=last,
        start=start,
        end=end,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        peak_read_bps=peak_read,
        peak_write_bps=peak_write,
        starts_run=starts_run,
        duration_ns=duration,
        intervals=tuple(intervals),
        first_run=first_run,
        last_run=run,
    )


def _bound_pair(group: _Group) -> tuple[int, int]:
    """Read one group's two-integer member bound, once per promotion pass."""

    return (group.first_index, group.last_index)


def _merge_groups(
    left: _Group,
    right: _Group,
    first_index: int,
    last_index: int,
) -> _Group:
    """Merge adjacent aggregates, coalescing only touching same-run seams."""

    intervals = list(left.intervals)
    for interval in right.intervals:
        if (
            intervals
            and intervals[-1][0] == interval[0]
            and intervals[-1][2] == interval[1]
        ):
            previous = intervals[-1]
            intervals[-1] = (previous[0], previous[1], interval[2])
        else:
            intervals.append(interval)
    return _Group(
        first_index=first_index,
        last_index=last_index,
        start=min(left.start, right.start),
        end=max(left.end, right.end),
        read_bytes=left.read_bytes + right.read_bytes,
        write_bytes=left.write_bytes + right.write_bytes,
        peak_read_bps=_defined_max(left.peak_read_bps, right.peak_read_bps),
        peak_write_bps=_defined_max(left.peak_write_bps, right.peak_write_bps),
        starts_run=left.starts_run or right.starts_run,
        duration_ns=left.duration_ns + right.duration_ns,
        intervals=tuple(intervals),
        first_run=left.first_run,
        last_run=right.last_run,
    )


def _promote_groups(groups: list[_Group]) -> list[_Group]:
    """Double the group size by merging adjacent pair aggregates."""

    bounds = [_bound_pair(group) for group in groups]
    promoted: list[_Group] = []
    for position in range(0, len(groups), 2):
        if position + 1 == len(groups):
            promoted.append(groups[position])
        else:
            promoted.append(
                _merge_groups(
                    groups[position],
                    groups[position + 1],
                    bounds[position][0],
                    bounds[position + 1][1],
                )
            )
    return promoted


def _group_size(count: int, width: int) -> int:
    """Smallest power of two whose group count fits the integer pixel width."""

    if count <= 0 or width <= 0 or width >= count:
        return 1
    group = 1
    while (count + group - 1) // group > width:
        group *= 2
    return group


def _build_groups(samples: tuple[_Sample, ...], width: int) -> tuple[_Group, ...]:
    """Build index-anchored consecutive groups for the current width."""

    count = len(samples)
    if count == 0:
        return ()
    group_size = _group_size(count, width)
    groups: list[_Group] = []
    previous_run = -1
    index = 0
    while index < count:
        first_run = (
            0
            if index == 0
            else previous_run + (1 if samples[index].starts_run else 0)
        )
        built = _aggregate_members(
            samples, index, min(index + group_size, count) - 1, first_run
        )
        groups.append(built)
        previous_run = built.last_run
        index = built.last_index + 1
    return tuple(groups)


@dataclass(frozen=True)
class ChartCurve:
    """One per-run, per-side stroke polyline in plot coordinates.

    Vertices are time-ordered and nondecreasing in X; a repeated X is legal
    for a subpixel run. This is a stroke, never a closed polygon.
    """

    side: Literal["read", "write"]
    run_index: int
    vertices: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if self.side not in ("read", "write"):
            raise ValueError("chart_curve.side must be 'read' or 'write'")
        _require_int(self.run_index, "chart_curve.run_index", minimum=0)
        if not isinstance(self.vertices, tuple):
            raise ValueError("chart_curve.vertices must be a tuple")
        if len(self.vertices) < 2:
            raise ValueError("chart_curve.vertices must have at least two points")
        points: list[tuple[float, float]] = []
        for pair in self.vertices:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("chart_curve.vertices must contain (x, y) pairs")
            x = _require_finite(pair[0], "chart_curve.x")
            y = _require_finite(pair[1], "chart_curve.y")
            points.append((x, y))
        for previous, current in zip(points, points[1:]):
            if current[0] < previous[0]:
                raise ValueError("chart_curve.vertices must be nondecreasing in X")
        object.__setattr__(self, "vertices", tuple(points))


def _rate_maximum(
    columns: Sequence[ChartColumn], durations: Sequence[int], field: str
) -> tuple[int, int] | None:
    """Return the largest (bytes, duration) rate as an exact pair."""

    best: tuple[int, int] | None = None
    for column, duration in zip(columns, durations):
        amount = getattr(column, field)
        if amount <= 0 or duration <= 0:
            continue
        if best is None or amount * best[1] > best[0] * duration:
            best = (amount, duration)
    return best


def _mapped_y(
    amount: int,
    duration: int,
    scale: tuple[int, int] | None,
    centre: float,
    half: float,
    low: float,
    high: float,
) -> float:
    """Map one column rate to a pixel height with exact rational arithmetic."""

    if scale is None or amount <= 0 or duration <= 0:
        return centre
    scale_amount, scale_duration = scale
    fraction = (amount * scale_duration) / (duration * scale_amount)
    if fraction > 1.0:
        fraction = 1.0
    y = centre + half * fraction
    if y < low:
        return low
    if y > high:
        return high
    return y


def _curve_geometry(
    columns: tuple[ChartColumn, ...],
    durations: tuple[int, ...],
    groups: tuple[_Group, ...],
    samples: tuple[_Sample, ...],
    plot: tuple[float, float, float, float],
) -> tuple[tuple[ChartCurve, ...], tuple[ChartSegment, ...]]:
    """Build exact per-run inset strokes and closed fills from cached intervals."""

    left, top, right, bottom = plot
    if not columns or not samples:
        return (), ()
    first_start = samples[0].start
    last_end = samples[-1].end
    span = last_end - first_start
    if span <= 0 or right <= left or bottom <= top:
        return (), ()
    x_span = right - left
    centre = (top + bottom) / 2.0
    half = (bottom - top) / 2.0
    read_scale = _rate_maximum(columns, durations, "read_bytes")
    write_scale = _rate_maximum(columns, durations, "write_bytes")
    heights = {
        "read": tuple(
            _mapped_y(column.read_bytes, duration, read_scale, centre, -half, top, centre)
            for column, duration in zip(columns, durations)
        ),
        "write": tuple(
            _mapped_y(column.write_bytes, duration, write_scale, centre, half, centre, bottom)
            for column, duration in zip(columns, durations)
        ),
    }

    fragments: dict[int, list[tuple[int, int, int]]] = {}
    for column_index, group in enumerate(groups):
        for run_index, start, end in group.intervals:
            fragments.setdefault(run_index, []).append((column_index, start, end))

    curves: list[ChartCurve] = []
    segments: list[ChartSegment] = []
    for run_index in sorted(fragments):
        members = fragments[run_index]
        first_time = min(fragment[1] for fragment in members)
        last_time = max(fragment[2] for fragment in members)
        outer_start = left + (first_time - first_start) * x_span / span
        outer_end = left + (last_time - first_start) * x_span / span
        if outer_end <= outer_start:
            continue
        inset = min(0.25, (outer_end - outer_start) / 4.0)
        low = outer_start + inset
        high = outer_end - inset
        for side in ("read", "write"):
            side_heights = heights[side]
            ordered = sorted(
                (
                    left
                    + (((start + end) / 2.0) - first_start) * x_span / span,
                    side_heights[column_index],
                )
                for column_index, start, end in members
            )
            if all(y == centre for _, y in ordered):
                continue
            if len(ordered) == 1:
                y = ordered[0][1]
                vertices: tuple[tuple[float, float], ...] = ((low, y), (high, y))
            else:
                xs = [x for x, _ in ordered]
                ys = [y for _, y in ordered]
                xs[0] = min(max(xs[0], low), high)
                xs[-1] = min(xs[-1], high)
                for position in range(1, len(xs)):
                    xs[position] = max(xs[position - 1], min(xs[position], high))
                vertices = tuple(zip(xs, ys))
            curves.append(ChartCurve(side, run_index, vertices))
            segments.append(
                ChartSegment(
                    side,
                    run_index,
                    (
                        (vertices[0][0], centre),
                        *vertices,
                        (vertices[-1][0], centre),
                        (vertices[0][0], centre),
                    ),
                )
            )
    return tuple(curves), tuple(segments)


class SeriesChart(QWidget):
    """A cached read-above/write-below time lane with explicit run geometry."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption = _TOTAL_CAPTION
        self._samples: tuple[_Sample, ...] = ()
        self._identity: object | None = None
        self._groups: tuple[_Group, ...] = ()
        self._group_width = 1
        self._columns: tuple[ChartColumn, ...] = ()
        self._durations: tuple[int, ...] = ()
        self._curves: tuple[ChartCurve, ...] = ()
        self._segments: tuple[ChartSegment, ...] = ()
        self._available = True
        self._reason: str | None = None
        # Share totals and time-series samples have independent availability.
        # None is unavailable; (0, 0) is an explicitly measured zero.
        self._totals: tuple[int, int] | None = None
        self._plot: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self._geometry_size: tuple[int, int] | None = None

        self._message = QLabel("", self)
        self._message.setObjectName("chart_message")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._message.hide()

        self.resize(320, 140)
        self._sync_message()

    # -- data ------------------------------------------------------------

    def set_task_points(
        self, points: Sequence[SeriesPoint], *, identity: object = None
    ) -> None:
        """Replace the immutable source buckets and rebuild geometry once."""

        self._install(_task_samples(points), identity)

    def append_task_points(
        self, points: Sequence[SeriesPoint], *, identity: object
    ) -> None:
        """Apply the complete newest sequence through the append decision table."""

        self._append(_task_samples(points), identity)

    def _install(self, samples: tuple[_Sample, ...], identity: object | None) -> None:
        """Full rebuild: validate first, replace input/identity, repaint once."""

        self._samples = samples
        self._identity = identity
        self._available = True
        self._reason = None
        self._rebuild_all()
        self._sync_message()
        self.update()

    def _append(self, samples: tuple[_Sample, ...], identity: object | None) -> None:
        """Apply spec §8.3's decision table in its exact top-down order."""

        old = self._samples
        count = len(old)
        incoming = len(samples)
        if not self._available:  # row 2
            self._install(samples, identity)
            return
        if identity != self._identity:  # row 3
            self._install(samples, identity)
            return
        if incoming == 0:  # row 4
            if count == 0 and self._reason is None:
                return
            self._samples = ()
            self._identity = identity
            self._available = True
            self._reason = None
            self._rebuild_all()
            self._sync_message()
            self.update()
            return
        prefix = _matching_prefix(old, samples)
        if count == 0:  # row 5
            self._install(samples, identity)
            return
        if incoming == count and prefix == count and self._reason is None:  # row 6
            return
        if incoming == count and prefix == count - 1:  # row 7
            self._change(samples, identity, replace_tail=True, start=count)
            return
        if incoming > count and prefix == count:  # row 8
            self._change(samples, identity, replace_tail=False, start=count)
            return
        if incoming > count and prefix == count - 1:  # row 9
            self._change(samples, identity, replace_tail=True, start=count)
            return
        if incoming < count:  # row 10
            self._install(samples, identity)
            return
        self._install(samples, identity)  # row 11

    def _change(
        self,
        samples: tuple[_Sample, ...],
        identity: object | None,
        *,
        replace_tail: bool,
        start: int,
    ) -> None:
        self._samples = samples
        self._identity = identity
        self._available = True
        self._reason = None
        if not self._geometry_active():
            self._rebuild_all()
        else:
            width = int(math.floor(self._plot[2] - self._plot[0]))
            groups = list(self._groups)
            group_width = self._group_width
            if replace_tail:
                groups = self._replace_tail(groups, samples)
            groups, group_width = self._append_members(
                groups, samples, start, group_width, width
            )
            self._groups = tuple(groups)
            self._group_width = group_width
            self._refresh_geometry()
        self._sync_message()
        self.update()

    @staticmethod
    def _replace_tail(
        groups: list[_Group], samples: tuple[_Sample, ...]
    ) -> list[_Group]:
        """Rebuild the old tail group from its members only."""

        if not groups:
            return groups
        last = groups[-1]
        previous_run = groups[-2].last_run if len(groups) > 1 else -1
        first_index = last.first_index
        first_run = (
            0
            if first_index == 0
            else previous_run + (1 if samples[first_index].starts_run else 0)
        )
        groups[-1] = _aggregate_members(samples, first_index, last.last_index, first_run)
        return groups

    @staticmethod
    def _append_members(
        groups: list[_Group],
        samples: tuple[_Sample, ...],
        start: int,
        group_width: int,
        width: int,
    ) -> tuple[list[_Group], int]:
        """Visit only the open/new groups, promoting pairwise past the bound."""

        index = start
        incoming = len(samples)
        while index < incoming:
            if (
                groups
                and groups[-1].last_index - groups[-1].first_index + 1 < group_width
            ):
                open_group = groups[-1]
                groups[-1] = _aggregate_members(
                    samples, open_group.first_index, index, open_group.first_run
                )
            else:
                previous_run = groups[-1].last_run if groups else -1
                first_run = (
                    0
                    if index == 0
                    else previous_run + (1 if samples[index].starts_run else 0)
                )
                groups.append(
                    _aggregate_members(samples, index, index, first_run)
                )
            if len(groups) > width:
                group_width *= 2
                groups = _promote_groups(groups)
            index += 1
        return groups, group_width

    def set_unavailable(self, reason: str) -> None:
        """Discard every drawn value and expose the exact reason."""

        reason = _require_text(reason, "reason")
        self._samples = ()
        self._identity = None
        self._groups = ()
        self._group_width = 1
        self._available = False
        self._reason = reason
        self._totals = None
        self._columns = ()
        self._durations = ()
        self._curves = ()
        self._segments = ()
        self._geometry_size = (self.width(), self.height())
        self._plot = self.plot_rect()
        self._sync_message()
        self.update()

    # -- observations ----------------------------------------------------

    def columns(self) -> tuple[ChartColumn, ...]:
        """Return the current size's aggregated columns, unchanged."""

        self._ensure_geometry()
        return self._columns

    def curves(self) -> tuple[ChartCurve, ...]:
        """Return the exact per-run stroke polylines the next paint uses."""

        self._ensure_geometry()
        return self._curves

    def segments(self) -> tuple[ChartSegment, ...]:
        """Return the exact closed fill polygons the next paint uses."""

        self._ensure_geometry()
        return self._segments

    def is_available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str | None:
        return self._reason

    def plot_rect(self) -> tuple[float, float, float, float]:
        """Return the plotting area, excluding the caption, gutter and margins."""

        margin = float(theme.PLOT_MARGIN)
        caption = float(self.fontMetrics().height() + 4)
        left = margin
        right = float(self.width() - theme.GUTTER_WIDTH - theme.PLOT_MARGIN)
        top = caption + margin
        bottom = float(self.height() - theme.PLOT_MARGIN)
        if right < left:
            right = left
        if bottom < top:
            bottom = top
        return (left, top, right, bottom)

    # -- geometry --------------------------------------------------------

    def _geometry_active(self) -> bool:
        left, top, right, bottom = self._plot
        return (
            self._geometry_size == (self.width(), self.height())
            and right > left
            and bottom > top
        )

    def _ensure_geometry(self) -> None:
        if self._geometry_size != (self.width(), self.height()):
            self._rebuild_all()

    def _rebuild_all(self) -> None:
        self._geometry_size = (self.width(), self.height())
        self._plot = self.plot_rect()
        self._groups = ()
        self._group_width = 1
        self._columns = ()
        self._durations = ()
        self._curves = ()
        self._segments = ()
        left, top, right, bottom = self._plot
        if right <= left or bottom <= top or not self._samples:
            return
        width = int(math.floor(right - left))
        self._group_width = _group_size(len(self._samples), width)
        self._groups = _build_groups(self._samples, width)
        self._refresh_geometry()

    def _refresh_geometry(self) -> None:
        self._columns = tuple(
            ChartColumn(
                group.start,
                group.end,
                group.read_bytes,
                group.write_bytes,
                group.peak_read_bps,
                group.peak_write_bps,
                group.starts_run,
            )
            for group in self._groups
        )
        self._durations = tuple(group.duration_ns for group in self._groups)
        self._curves, self._segments = _curve_geometry(
            self._columns, self._durations, self._groups, self._samples, self._plot
        )

    # -- message ---------------------------------------------------------

    def _sync_message(self) -> None:
        if not self._available:
            self._set_message(f"{_UNAVAILABLE_PREFIX}{self._reason}")
        elif not self._samples:
            self._set_message(_NO_SAMPLES)
        else:
            self._set_message("")

    def _set_message(self, text: str) -> None:
        self._message.setText(text)
        if text:
            self._message.setVisible(True)
        else:
            self._message.setVisible(False)

    def _place_message(self) -> None:
        self._message.setGeometry(0, 0, self.width(), self.height())

    # -- painting --------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        del event
        self._ensure_geometry()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(
                self.rect(), self.palette().color(QPalette.ColorRole.Base)
            )
            metrics = painter.fontMetrics()
            painter.setPen(theme.TEXT_COLOR)
            painter.drawText(
                QPointF(float(theme.PLOT_MARGIN), float(theme.PLOT_MARGIN + metrics.ascent())),
                self._caption,
            )
            left, top, right, bottom = self._plot
            if not self._available:
                return
            painter.setPen(theme.MUTED_COLOR)
            gutter = (
                "r unavailable · w unavailable" if self._totals is None else
                f"r {_format_bytes(self._totals[0])} · "
                f"w {_format_bytes(self._totals[1])}"
            )
            painter.drawText(
                QRectF(right + theme.PLOT_MARGIN, top,
                       float(theme.GUTTER_WIDTH - theme.PLOT_MARGIN), bottom - top),
                int(Qt.TextFlag.TextWordWrap), gutter,
            )
            if not self._samples or not self._columns:
                return
            centre = (top + bottom) / 2.0
            painter.setPen(QPen(theme.BORDER_COLOR, theme.AXIS_PEN_WIDTH))
            painter.drawLine(QPointF(left, centre), QPointF(right, centre))
            painter.setPen(Qt.PenStyle.NoPen)
            for segment in self._segments:
                color = theme.READ_COLOR if segment.side == "read" else theme.WRITE_COLOR
                painter.setBrush(QBrush(color))
                painter.drawPolygon(
                    QPolygonF([QPointF(x, y) for x, y in segment.polygon])
                )
            for curve in self._curves:
                color = theme.READ_COLOR if curve.side == "read" else theme.WRITE_COLOR
                painter.setPen(QPen(color, theme.AXIS_PEN_WIDTH))
                painter.drawPolyline(
                    QPolygonF([QPointF(x, y) for x, y in curve.vertices])
                )
        finally:
            painter.end()

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        self._place_message()
        self._rebuild_all()
        super().resizeEvent(event)
        self.update()


class DualSeriesChart(SeriesChart):
    """The TOTAL lane: the primitive geometry plus an exact right-hand gutter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption = _TOTAL_CAPTION

    def set_totals(self, read_bytes: int, write_bytes: int) -> None:
        """Set the exact gutter totals independently of the plotted samples."""

        self._totals = (
            _require_int(read_bytes, "read_bytes", minimum=0),
            _require_int(write_bytes, "write_bytes", minimum=0),
        )
        self.update()

    def set_totals_unavailable(self) -> None:
        """Withhold the gutter without discarding independently valid samples."""

        self._totals = None
        self.update()


class DeviceLane(DualSeriesChart):
    """One device time lane, captioned with the raw ``major:minor`` token."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption = ""

    def set_device_points(
        self, points: Sequence[DeviceSeriesPoint], *, identity: object = None
    ) -> None:
        """Replace the source buckets without inheriting task suspension."""

        self._install(_device_samples(points), identity)

    def append_device_points(
        self, points: Sequence[DeviceSeriesPoint], *, identity: object
    ) -> None:
        """Apply a complete device sequence through the same decision table."""

        self._append(_device_samples(points), identity)

    def set_device(self, dev: str) -> None:
        """Set the raw device token caption; no name or mount is resolved."""

        self._caption = _require_text(dev, "dev")
        self.update()


class DonutChart(QWidget):
    """Task read/write composition with exact byte and one-decimal labels."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._read: int | None = 0
        self._write: int | None = 0
        self._available = True
        self._reason: str | None = None

        self._message = QLabel("", self)
        self._message.setObjectName("chart_message")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._message.hide()

        self.resize(220, 160)
        self._sync_message()

    def set_shares(self, read_bytes: int, write_bytes: int) -> None:
        read = _require_int(read_bytes, "read_bytes", minimum=0)
        write = _require_int(write_bytes, "write_bytes", minimum=0)
        self._read = read
        self._write = write
        self._available = True
        self._reason = None
        self._sync_message()
        self.update()

    def set_unavailable(self, reason: str) -> None:
        reason = _require_text(reason, "reason")
        self._read = None
        self._write = None
        self._available = False
        self._reason = reason
        self._sync_message()
        self.update()

    def shares(self) -> tuple[float, float] | None:
        if not self._available or self._read is None or self._write is None:
            return None
        total = self._read + self._write
        if total <= 0:
            return (0.0, 0.0)
        return (self._read / total, self._write / total)

    def unavailable_reason(self) -> str | None:
        return self._reason

    def _sync_message(self) -> None:
        if not self._available:
            self._set_message(f"{_UNAVAILABLE_PREFIX}{self._reason}")
        elif self.shares() == (0.0, 0.0):
            self._set_message(_NO_TASK_IO)
        else:
            self._set_message("")

    def _set_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    def _place_message(self) -> None:
        self._message.setGeometry(0, 0, self.width(), self.height())

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        self._place_message()
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(
                self.rect(), self.palette().color(QPalette.ColorRole.Base)
            )
            metrics = painter.fontMetrics()
            painter.setPen(theme.TEXT_COLOR)
            painter.drawText(
                QPointF(float(theme.PLOT_MARGIN), float(theme.PLOT_MARGIN + metrics.ascent())),
                _DONUT_CAPTION,
            )
            shares = self.shares()
            if shares is None or self._read is None or self._write is None:
                return
            read_fraction, write_fraction = shares
            text_x = max(float(theme.PLOT_MARGIN), self.width() * 0.60)
            line_y = float(theme.PLOT_MARGIN + metrics.ascent() + metrics.height())
            for line in (
                f"{_format_bytes(self._read)} read",
                f"{_format_bytes(self._write)} write",
                f"{read_fraction * 100:.1f}% / {write_fraction * 100:.1f}%",
            ):
                painter.drawText(QPointF(text_x, line_y), line)
                line_y += metrics.height()
            if read_fraction + write_fraction <= 0.0:
                return
            centre = QPointF(max(24.0, self.width() * 0.34), self.height() * 0.58)
            radius = max(
                6.0,
                min(self.width() * 0.24, max(1.0, self.height() - 48.0) * 0.40),
            )
            thickness = max(4.0, radius * 0.42)
            ring = QRectF(
                centre.x() - radius, centre.y() - radius, radius * 2.0, radius * 2.0
            )
            painter.setPen(QPen(theme.TRACK_COLOR, thickness))
            painter.drawEllipse(ring)
            read_span = int(round(-read_fraction * 360.0 * 16.0))
            if read_fraction > 0.0:
                painter.setPen(QPen(theme.READ_COLOR, thickness))
                painter.drawArc(ring, 90 * 16, read_span)
            if write_fraction > 0.0:
                painter.setPen(QPen(theme.WRITE_COLOR, thickness))
                write_span = int(round(-write_fraction * 360.0 * 16.0))
                painter.drawArc(ring, 90 * 16 + read_span, write_span)
        finally:
            painter.end()


class ShareBars(QWidget):
    """Attributed share by disk, normalised against the largest device total."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[tuple[str, int], ...] = ()
        self._reconciliation: str | None = None
        self._available = True
        self._reason: str | None = None

        self._message = QLabel("", self)
        self._message.setObjectName("chart_message")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._message.hide()

        self._reconciliation_label = QLabel("", self)
        self._reconciliation_label.setObjectName("share_reconciliation")
        self._reconciliation_label.setTextFormat(Qt.TextFormat.PlainText)
        self._reconciliation_label.setWordWrap(True)
        self._reconciliation_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self._reconciliation_label.hide()
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.resize(280, 150)
        self._sync_message()
        self._place_content()

    def set_rows(self, rows: Sequence[tuple[str, int]]) -> None:
        parsed: list[tuple[str, int]] = []
        for row in rows:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise ValueError("share rows must be (label, total_bytes) pairs")
            label = _require_text(row[0], "row label")
            total = _require_int(row[1], "row total", minimum=0)
            parsed.append((label, total))
        self._rows = tuple(parsed)
        self._available = True
        self._reason = None
        self._sync_message()
        self._place_content()
        self.update()

    def set_reconciliation(self, text: str | None) -> None:
        self._reconciliation = _require_optional_text(text, "reconciliation")
        self._reconciliation_label.setText(self._reconciliation or "")
        self._place_content()
        self.update()

    def set_unavailable(self, reason: str) -> None:
        reason = _require_text(reason, "reason")
        self._rows = ()
        self._reconciliation = None
        self._reconciliation_label.clear()
        self._available = False
        self._reason = reason
        self._sync_message()
        self._place_content()
        self.update()

    def fractions(self) -> tuple[tuple[str, float], ...]:
        if not self._available or not self._rows:
            return ()
        largest = max(total for _, total in self._rows)
        return tuple(
            (label, total / largest if largest else 0.0) for label, total in self._rows
        )

    def unavailable_reason(self) -> str | None:
        return self._reason

    def _sync_message(self) -> None:
        if not self._available:
            self._set_message(f"{_UNAVAILABLE_PREFIX}{self._reason}")
        else:
            self._set_message("")

    def _set_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    def _row_height(self) -> int:
        return max(theme.BAR_HEIGHT + self.fontMetrics().height() + 4, 14)

    def _annotation_top(self) -> int:
        metrics = self.fontMetrics()
        return (theme.PLOT_MARGIN + metrics.ascent() + metrics.height()
                + len(self._rows) * self._row_height())

    def heightForWidth(self, width: int) -> int:
        annotation_height = 0
        if self._available and self._reconciliation is not None:
            annotation_height = self._reconciliation_label.heightForWidth(
                max(1, width - 2 * theme.PLOT_MARGIN)
            )
        return max(150, self._annotation_top() + annotation_height + theme.PLOT_MARGIN)

    def sizeHint(self) -> QSize:
        return QSize(280, self.heightForWidth(280))

    def minimumSizeHint(self) -> QSize:
        return QSize(
            self.fontMetrics().horizontalAdvance(_SHARE_CAPTION) + 2 * theme.PLOT_MARGIN,
            self.heightForWidth(self.width()),
        )

    def _place_content(self) -> None:
        margin = theme.PLOT_MARGIN
        annotation = self._reconciliation_label
        width = max(1, self.width() - 2 * margin)
        annotation.setGeometry(margin, self._annotation_top(), width,
                               max(0, annotation.heightForWidth(width)))
        annotation.setVisible(self._available and self._reconciliation is not None)
        # Layout minima must follow the current row count and wrapped text, so
        # a parent either allocates the full panel or exposes a scroll path.
        self.setMinimumHeight(self.heightForWidth(self.width()))
        self.updateGeometry()
        self._message.setGeometry(0, 0, self.width(), self.height())

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        self._place_content()
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(
                self.rect(), self.palette().color(QPalette.ColorRole.Base)
            )
            metrics = painter.fontMetrics()
            painter.setPen(theme.TEXT_COLOR)
            painter.drawText(
                QPointF(float(theme.PLOT_MARGIN), float(theme.PLOT_MARGIN + metrics.ascent())),
                _SHARE_CAPTION,
            )
            if not self._available:
                return
            fractions = self.fractions()
            row_height = self._row_height()
            y = float(theme.PLOT_MARGIN + metrics.ascent() + metrics.height())
            bar_area = max(1.0, self.width() - 2.0 * theme.PLOT_MARGIN)
            for (label, _total), (_, fraction) in zip(self._rows, fractions):
                painter.setPen(theme.TEXT_COLOR)
                painter.drawText(QPointF(float(theme.PLOT_MARGIN), y), label)
                painter.setPen(Qt.PenStyle.NoPen)
                bar_y = y + 3.0
                painter.setBrush(QBrush(theme.TRACK_COLOR))
                painter.drawRect(
                    QRectF(float(theme.PLOT_MARGIN), bar_y, bar_area, theme.BAR_HEIGHT)
                )
                painter.setBrush(QBrush(theme.READ_COLOR))
                painter.drawRect(
                    QRectF(
                        float(theme.PLOT_MARGIN),
                        bar_y,
                        bar_area * fraction,
                        theme.BAR_HEIGHT,
                    )
                )
                y += row_height
        finally:
            painter.end()


__all__ = [
    "ChartColumn",
    "ChartCurve",
    "ChartSegment",
    "DeviceLane",
    "DonutChart",
    "DualSeriesChart",
    "SeriesChart",
    "ShareBars",
]
