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

from PyQt6.QtCore import QPointF, QRectF, Qt
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
from PyQt6.QtWidgets import QLabel, QWidget

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
    """One validated source bucket in chart-local form."""

    __slots__ = (
        "start",
        "end",
        "read_bytes",
        "write_bytes",
        "peak_read_bps",
        "peak_write_bps",
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
        starts_run: bool,
    ) -> None:
        self.start = start
        self.end = end
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes
        self.peak_read_bps = peak_read_bps
        self.peak_write_bps = peak_write_bps
        self.starts_run = starts_run


def _task_samples(points: Sequence[SeriesPoint]) -> tuple[_Sample, ...]:
    samples: list[_Sample] = []
    previous_end: int | None = None
    previous_suspend = False
    for point in points:
        if not isinstance(point, SeriesPoint):
            raise ValueError("set_task_points requires SeriesPoint values")
        start = point.bucket_start_mono_ns
        end = start + point.bucket_secs * _NANOSECONDS
        if previous_end is not None and start < previous_end:
            raise ValueError("series points must be ordered and non-overlapping")
        starts_run = (
            previous_end is None or start != previous_end or previous_suspend
        )
        samples.append(
            _Sample(
                start,
                end,
                point.read_bytes,
                point.write_bytes,
                point.peak_read_bps,
                point.peak_write_bps,
                starts_run,
            )
        )
        previous_end = end
        previous_suspend = point.spans_suspend
    return tuple(samples)


def _device_samples(points: Sequence[DeviceSeriesPoint]) -> tuple[_Sample, ...]:
    samples: list[_Sample] = []
    previous_end: int | None = None
    for point in points:
        if not isinstance(point, DeviceSeriesPoint):
            raise ValueError("set_device_points requires DeviceSeriesPoint values")
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
                previous_end is None or start != previous_end,
            )
        )
        previous_end = end
    return tuple(samples)


def _group_runs(samples: tuple[_Sample, ...]) -> tuple[tuple[int, ...], ...]:
    runs: list[list[int]] = []
    current: list[int] = []
    for index, sample in enumerate(samples):
        if sample.starts_run and current:
            runs.append(current)
            current = []
        current.append(index)
    if current:
        runs.append(current)
    return tuple(tuple(run) for run in runs)


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


def _step_polygon(
    intervals: list[list[int]],
    first_start: int,
    span: int,
    left: float,
    x_span: float,
    epsilon: float,
    centre: float,
    heights: Sequence[float],
) -> tuple[tuple[float, float], ...]:
    """Build one closed step-fill polygon covering just this run's support."""

    pieces: list[tuple[float, float, float]] = []
    last = len(intervals) - 1
    for index, (start, end, column_index) in enumerate(intervals):
        start_x = left + (start - first_start) * x_span / span
        end_x = left + (end - first_start) * x_span / span
        if index == 0:
            start_x += epsilon
        if index == last:
            end_x -= epsilon
        pieces.append((start_x, end_x, heights[column_index]))

    polygon: list[tuple[float, float]] = [(pieces[0][0], centre)]
    current: float | None = None
    for start_x, end_x, y in pieces:
        if current is None:
            polygon.append((start_x, y))
        elif y != current:
            polygon.append((start_x, current))
            polygon.append((start_x, y))
        else:
            polygon.append((start_x, y))
        polygon.append((end_x, y))
        current = y
    polygon.append((pieces[-1][1], centre))
    polygon.append(polygon[0])
    return tuple(polygon)


class SeriesChart(QWidget):
    """A cached read-above/write-below time lane with explicit run geometry."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption = _TOTAL_CAPTION
        self._samples: tuple[_Sample, ...] = ()
        self._runs: tuple[tuple[int, ...], ...] = ()
        self._available = True
        self._reason: str | None = None
        self._read_total = 0
        self._write_total = 0
        self._columns: tuple[ChartColumn, ...] = ()
        self._segments: tuple[ChartSegment, ...] = ()
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

    def set_task_points(self, points: Sequence[SeriesPoint]) -> None:
        """Replace the immutable source buckets and rebuild geometry once."""

        samples = _task_samples(points)
        self._samples = samples
        self._runs = _group_runs(samples)
        self._available = True
        self._reason = None
        self._sync_message()
        self._rebuild_geometry()
        self.update()

    def set_unavailable(self, reason: str) -> None:
        """Discard the drawn state and expose the exact reason."""

        reason = _require_text(reason, "reason")
        self._samples = ()
        self._runs = ()
        self._available = False
        self._reason = reason
        self._read_total = 0
        self._write_total = 0
        self._columns = ()
        self._segments = ()
        self._sync_message()
        self.update()

    # -- observations ----------------------------------------------------

    def columns(self) -> tuple[ChartColumn, ...]:
        """Return the current size's aggregated columns, unchanged."""

        self._ensure_geometry()
        return self._columns

    def segments(self) -> tuple[ChartSegment, ...]:
        """Return the exact closed polygons the next paint uses."""

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

    def _ensure_geometry(self) -> None:
        if self._geometry_size != (self.width(), self.height()):
            self._rebuild_geometry()

    def _rebuild_geometry(self) -> None:
        self._geometry_size = (self.width(), self.height())
        left, top, right, bottom = self.plot_rect()
        self._plot = (left, top, right, bottom)
        self._columns = ()
        self._segments = ()

        budget = int(math.floor(right - left))
        samples = self._samples
        if budget <= 0 or bottom <= top or not samples:
            return
        first_start = samples[0].start
        last_end = samples[-1].end
        span = last_end - first_start
        if span <= 0:
            return

        bins: list[list[int]]
        if len(samples) <= budget:
            bins = [[index] for index in range(len(samples))]
        else:
            buckets: list[list[int]] = [[] for _ in range(budget)]
            for index, sample in enumerate(samples):
                position = min(
                    budget - 1, ((sample.start - first_start) * budget) // span
                )
                buckets[position].append(index)
            bins = buckets

        columns: list[ChartColumn] = []
        durations: list[int] = []
        column_of = [0] * len(samples)
        for members in bins:
            if not members:
                continue
            column_index = len(columns)
            start = samples[members[0]].start
            end = samples[members[0]].end
            read_bytes = 0
            write_bytes = 0
            peak_read: int | None = None
            peak_write: int | None = None
            duration = 0
            starts_run = False
            for member in members:
                sample = samples[member]
                column_of[member] = column_index
                start = min(start, sample.start)
                end = max(end, sample.end)
                read_bytes += sample.read_bytes
                write_bytes += sample.write_bytes
                if sample.peak_read_bps is not None and (
                    peak_read is None or sample.peak_read_bps > peak_read
                ):
                    peak_read = sample.peak_read_bps
                if sample.peak_write_bps is not None and (
                    peak_write is None or sample.peak_write_bps > peak_write
                ):
                    peak_write = sample.peak_write_bps
                duration += sample.end - sample.start
                starts_run = starts_run or sample.starts_run
            columns.append(
                ChartColumn(
                    start,
                    end,
                    read_bytes,
                    write_bytes,
                    peak_read,
                    peak_write,
                    starts_run,
                )
            )
            durations.append(duration)
        self._columns = tuple(columns)

        centre = (top + bottom) / 2.0
        half = (bottom - top) / 2.0
        read_scale = _rate_maximum(columns, durations, "read_bytes")
        write_scale = _rate_maximum(columns, durations, "write_bytes")
        read_y = [
            _mapped_y(column.read_bytes, duration, read_scale, centre, -half, top, centre)
            for column, duration in zip(columns, durations)
        ]
        write_y = [
            _mapped_y(
                column.write_bytes, duration, write_scale, centre, half, centre, bottom
            )
            for column, duration in zip(columns, durations)
        ]

        x_span = right - left
        sides: tuple[tuple[Literal["read", "write"], list[float]], ...] = (
            ("read", read_y),
            ("write", write_y),
        )
        segments: list[ChartSegment] = []
        for run_index, run_members in enumerate(self._runs):
            run_start_x = (
                left + (samples[run_members[0]].start - first_start) * x_span / span
            )
            run_end_x = left + (samples[run_members[-1]].end - first_start) * x_span / span
            run_span = run_end_x - run_start_x
            epsilon = min(0.25, run_span / 4.0) if run_span > 0 else 0.0
            for side, heights in sides:
                if all(heights[column_of[member]] == centre for member in run_members):
                    continue
                intervals: list[list[int]] = []
                for member in run_members:
                    sample = samples[member]
                    column_index = column_of[member]
                    if intervals and intervals[-1][2] == column_index:
                        intervals[-1][1] = sample.end
                    else:
                        intervals.append([sample.start, sample.end, column_index])
                segments.append(
                    ChartSegment(
                        side,
                        run_index,
                        _step_polygon(
                            intervals,
                            first_start,
                            span,
                            left,
                            x_span,
                            epsilon,
                            centre,
                            heights,
                        ),
                    )
                )
        self._segments = tuple(segments)

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
            if not self._available or not self._samples or not self._columns:
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
            painter.setPen(theme.MUTED_COLOR)
            gutter = (
                f"r {_format_bytes(self._read_total)} · "
                f"w {_format_bytes(self._write_total)}"
            )
            painter.drawText(
                QPointF(right + theme.PLOT_MARGIN, top + metrics.ascent()), gutter
            )
        finally:
            painter.end()

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        self._place_message()
        self._rebuild_geometry()
        super().resizeEvent(event)
        self.update()


class DualSeriesChart(SeriesChart):
    """The TOTAL lane: the primitive geometry plus an exact right-hand gutter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption = _TOTAL_CAPTION

    def set_totals(self, read_bytes: int, write_bytes: int) -> None:
        """Set the exact gutter totals independently of the plotted samples."""

        self._read_total = _require_int(read_bytes, "read_bytes", minimum=0)
        self._write_total = _require_int(write_bytes, "write_bytes", minimum=0)
        self.update()


class DeviceLane(DualSeriesChart):
    """One device time lane, captioned with the raw ``major:minor`` token."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption = ""

    def set_device_points(self, points: Sequence[DeviceSeriesPoint]) -> None:
        """Replace the source buckets without inheriting task suspension."""

        samples = _device_samples(points)
        self._samples = samples
        self._runs = _group_runs(samples)
        self._available = True
        self._reason = None
        self._sync_message()
        self._rebuild_geometry()
        self.update()

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

        self.resize(280, 150)
        self._sync_message()

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
        self.update()

    def set_reconciliation(self, text: str | None) -> None:
        self._reconciliation = _require_optional_text(text, "reconciliation")
        self.update()

    def set_unavailable(self, reason: str) -> None:
        reason = _require_text(reason, "reason")
        self._rows = ()
        self._reconciliation = None
        self._available = False
        self._reason = reason
        self._sync_message()
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
                _SHARE_CAPTION,
            )
            if not self._available or not self._rows:
                return
            fractions = self.fractions()
            row_height = max(theme.BAR_HEIGHT + metrics.height() + 4, 14)
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
            if self._reconciliation is not None:
                painter.setPen(theme.MUTED_COLOR)
                painter.drawText(
                    QPointF(float(theme.PLOT_MARGIN), y + metrics.ascent()),
                    self._reconciliation,
                )
        finally:
            painter.end()


__all__ = [
    "ChartColumn",
    "ChartSegment",
    "DeviceLane",
    "DonutChart",
    "DualSeriesChart",
    "SeriesChart",
    "ShareBars",
]
