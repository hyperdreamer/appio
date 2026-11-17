"""Offscreen geometry and composition tests for the appio chart widgets.

Every chart exposes its drawn state as immutable data -- aggregated ``columns()``,
run-clipped ``segments()`` and composition ``shares()``/``fractions()`` -- so these
tests assert geometry and conservation without reading pixels. Pixels are only
inspected for the "no zero line" rule and for visible reason text.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import replace

import pytest

# This host installs both PySide6 and PyQt6. pytest-qt's default guess prefers
# PySide6, which would make its QtBot reject PyQt6 widgets. The project runtime
# is PyQt6, so pin the backend before the qapp/qtbot fixtures are created.
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QLabel

from appio.contracts import DeviceSeriesPoint, SeriesPoint
from appio.ui import charts as charts_module
from appio.ui import theme
from appio.ui.charts import (
    DeviceLane,
    DonutChart,
    DualSeriesChart,
    SeriesChart,
    ShareBars,
)
from appio.ui.formatting import format_bytes

BASE_NS = 4_000_000_000_000_000
BUCKET_SECS = 10
BUCKET_NS = BUCKET_SECS * 10**9

NO_SAMPLES = "no samples in this window"
NO_TASK_IO = "no task I/O in this window"


def task_point(
    start_ns: int,
    *,
    read_bytes: int = 2048,
    write_bytes: int = 1024,
    peak_read_bps: int | None = None,
    peak_write_bps: int | None = None,
    spans_suspend: bool = False,
    bucket_secs: int = BUCKET_SECS,
) -> SeriesPoint:
    return SeriesPoint(
        start_ns,
        bucket_secs,
        None,
        read_bytes,
        write_bytes,
        peak_read_bps,
        peak_write_bps,
        None,
        spans_suspend,
    )


def device_point(
    start_ns: int,
    *,
    dev: str = "8:0",
    rbytes: int = 1024,
    wbytes: int = 512,
    bucket_secs: int = BUCKET_SECS,
) -> DeviceSeriesPoint:
    return DeviceSeriesPoint(start_ns, bucket_secs, dev, rbytes, wbytes)


def chart_points(
    count: int = 6,
    *,
    hole_at: int | None = None,
    suspend_at: int | None = None,
    dev: str = "8:0",
    define_read_peaks: bool = True,
    define_write_peaks: bool = True,
) -> tuple[tuple[SeriesPoint, ...], tuple[DeviceSeriesPoint, ...]]:
    """At least four nonzero task buckets with matching device buckets.

    ``hole_at`` removes one real bucket so the neighbours are non-adjacent;
    ``suspend_at`` marks one bucket ``spans_suspend``. Device points carry no
    suspension metadata, exactly like the contract.
    """

    assert count >= 4
    task: list[SeriesPoint] = []
    device: list[DeviceSeriesPoint] = []
    index = 0
    for slot in range(count):
        if hole_at is not None and slot == hole_at:
            index += 2
        start = BASE_NS + index * BUCKET_NS
        read = 2048 * (slot + 1)
        write = 1024 * (slot + 1)
        task.append(
            task_point(
                start,
                read_bytes=read,
                write_bytes=write,
                peak_read_bps=read * 2 if (define_read_peaks and slot % 2 == 0) else None,
                peak_write_bps=write * 3 if (define_write_peaks and slot % 3 == 0) else None,
                spans_suspend=slot == suspend_at,
            )
        )
        device.append(device_point(start, dev=dev, rbytes=read // 2, wbytes=write // 2))
        index += 1
    return tuple(task), tuple(device)


def make_chart(cls, qtbot, *, width: int = 320, height: int = 160):
    chart = cls()
    qtbot.addWidget(chart)
    chart.resize(width, height)
    chart.show()
    return chart


def message(chart) -> QLabel:
    label = chart.findChild(QLabel, "chart_message")
    assert label is not None
    return label


def by_side(segments, side):
    return tuple(segment for segment in segments if segment.side == side)


def assert_closed_finite_polygons(segments) -> None:
    for segment in segments:
        polygon = segment.polygon
        assert len(polygon) >= 4, segment
        assert polygon[0] == polygon[-1], segment
        for x, y in polygon:
            assert math.isfinite(x) and math.isfinite(y), segment
        xs = [x for x, _ in polygon]
        # Forward support along X before the baseline closure.
        assert xs[:-1] == sorted(xs[:-1]), segment


def assert_inside_plot(segments, plot) -> None:
    left, top, right, bottom = plot
    centre = (top + bottom) / 2.0
    tolerance = 1e-9
    for segment in segments:
        xs = [x for x, _ in segment.polygon]
        ys = [y for _, y in segment.polygon]
        assert all(left - tolerance <= x <= right + tolerance for x in xs), segment
        assert all(top - tolerance <= y <= bottom + tolerance for y in ys), segment
        if segment.side == "read":
            assert all(y <= centre + tolerance for y in ys), segment
        else:
            assert all(y >= centre - tolerance for y in ys), segment


def mapped_x(time_ns: int, points, plot) -> float:
    left, _, right, _ = plot
    first = points[0].bucket_start_mono_ns
    last = points[-1].bucket_start_mono_ns + points[-1].bucket_secs * 10**9
    return left + (time_ns - first) * (right - left) / (last - first)


def width_of(plot) -> int:
    left, _, right, _ = plot
    return int(math.floor(right - left))


# --------------------------------------------------------------------------
# Append, grouping and run-geometry helpers
# --------------------------------------------------------------------------

# Ten-second buckets are the frozen fixture width, and a 168 px widget leaves
# an exact eight-pixel plot so power-of-two grouping is easy to state.
TEN_NS = 10 * 10**9
WIDE = 168


def dense_points(
    count: int,
    *,
    start_ns: int = 0,
    bucket_secs: int = BUCKET_SECS,
    read_bytes: int = 1,
    write_bytes: int = 2,
) -> tuple[SeriesPoint, ...]:
    """One contiguous run of ``count`` buckets with constant raw bytes."""

    return tuple(
        task_point(
            start_ns + index * bucket_secs * 10**9,
            read_bytes=read_bytes,
            write_bytes=write_bytes,
            bucket_secs=bucket_secs,
        )
        for index in range(count)
    )


def group_bounds(chart) -> list[tuple[int, int]]:
    return [(group.first_index, group.last_index) for group in chart._groups]


def stored_intervals(chart) -> list[list[tuple[int, int, int]]]:
    return [list(group.intervals) for group in chart._groups]


def retained_state(chart) -> dict:
    return {
        "samples": chart._samples,
        "identity": chart._identity,
        "available": chart._available,
        "reason": chart._reason,
        "groups": chart._groups,
        "group_width": chart._group_width,
        "columns": chart._columns,
        "durations": chart._durations,
        "curves": chart._curves,
        "segments": chart._segments,
        "plot": chart._plot,
        "geometry_size": chart._geometry_size,
        "totals": chart._totals,
    }


def watch_aggregation(monkeypatch):
    """Record every aggregate-member visit, including alternate reducers."""

    calls: list[tuple[int, int]] = []
    visits: list[int] = []
    original = charts_module._aggregate_members

    def wrapper(samples, first, last, first_run):
        calls.append((first, last))
        visits.extend(range(first, last + 1))
        return original(samples, first, last, first_run)

    monkeypatch.setattr(charts_module, "_aggregate_members", wrapper)
    return calls, visits


def watch_build_groups(monkeypatch):
    calls: list[tuple[int, int]] = []
    original = charts_module._build_groups

    def wrapper(samples, width):
        calls.append((len(samples), width))
        return original(samples, width)

    monkeypatch.setattr(charts_module, "_build_groups", wrapper)
    return calls


def watch_scale(monkeypatch):
    visits: list[int] = []
    original = charts_module._scale_maxima

    def wrapper(columns, durations):
        visits.append(len(columns))
        return original(columns, durations)

    monkeypatch.setattr(charts_module, "_scale_maxima", wrapper)
    return visits


def watch_validation(monkeypatch):
    visits: list[int] = []
    original = charts_module._admit_task_point

    def wrapper(point):
        visits.append(len(visits))
        return original(point)

    monkeypatch.setattr(charts_module, "_admit_task_point", wrapper)
    return visits


def watch_updates(chart, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(chart, "update", lambda: calls.append(True))
    return calls


class AppendProbe:
    """Charge the actual production work of one §8.7 append fixture.

    Every counter is raised by a production primitive or by a raw
    ``_Sample``/``_Group`` attribute read, never by a test-side hand count:

    * ``_admit_task_point`` validates an incoming point;
    * ``_samples_equal_at`` compares one full raw prefix position;
    * ``_aggregate_members`` visits the raw members of one group;
    * ``_scale_maxima`` reads one cached column's rate pair;
    * ``_bound_pair`` reads one promotion input pair and ``_merge_groups``
      writes one promotion output pair;
    * ``_mapped_y`` reads one cached column for geometry, cached interval
      iteration is charged to geometry or to promotion merging, and emitted
      curve/closure coordinates are counted from the returned geometry.

    Raw member reads performed while no aggregation, comparison or geometry
    phase is active land in ``unaccounted_raw_reads``. That is what charges an
    alternate full-prefix reducer inserted in ``_append_members`` even though
    it never calls ``_aggregate_members``. A group bound read made while
    ``_promote_groups`` is running outside ``_bound_pair`` lands in
    ``direct_promotion_bound_reads``, and a per-group member range built during
    promotion lands in ``promotion_ranges``, so a copied member-index vector
    cannot hide behind the expected pair counts.
    """

    RAW_FIELDS = (
        "start",
        "end",
        "read_bytes",
        "write_bytes",
        "peak_read_bps",
        "peak_write_bps",
        "peak_total_bps",
        "wall_start_seconds",
        "spans_suspend",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.validation_visits = 0
        self.comparison_visits = 0
        self.aggregate_member_indices: list[int] = []
        self.scale_column_visits = 0
        self.bound_reads: list[tuple[int, int]] = []
        self.bound_writes: list[tuple[int, int]] = []
        self.promotion_output_bounds: list[tuple[int, int]] = []
        self.merge_interval_visits = 0
        self.geometry_column_visits = 0
        self.interval_visits = 0
        self.vertex_emit = 0
        self.closure_emit = 0
        self.unaccounted_raw_reads: list[str] = []
        self.direct_promotion_bound_reads: list[str] = []
        self.promotion_ranges: list[tuple[int, tuple[int, ...]]] = []
        self._phase: str | None = None
        self._promotions = 0

    # -- installation ---------------------------------------------------

    def install(self, monkeypatch) -> "AppendProbe":
        probe = self
        base_sample = charts_module._Sample
        base_group = charts_module._Group
        builtin_range = range

        class CountedSample(base_sample):  # type: ignore[misc, valid-type]
            __slots__ = ()

        for field in self.RAW_FIELDS:
            descriptor = getattr(base_sample, field)

            def counted_read(sample, field=field, descriptor=descriptor):
                probe._raw_read(field)
                return descriptor.__get__(sample, type(sample))

            def counted_write(sample, value, descriptor=descriptor):
                descriptor.__set__(sample, value)

            setattr(CountedSample, field, property(counted_read, counted_write))

        class CountedIntervals(tuple):
            __slots__ = ()

            def __iter__(self):
                for position in builtin_range(len(self)):
                    probe._interval_visit()
                    yield tuple.__getitem__(self, position)

        class CountedGroup(base_group):  # type: ignore[misc, valid-type]
            __slots__ = ()

            @property
            def first_index(self):
                probe._bound_read("first_index")
                return base_group.first_index.__get__(self, type(self))

            @first_index.setter
            def first_index(self, value):
                base_group.first_index.__set__(self, value)

            @property
            def last_index(self):
                probe._bound_read("last_index")
                return base_group.last_index.__get__(self, type(self))

            @last_index.setter
            def last_index(self, value):
                base_group.last_index.__set__(self, value)

            @property
            def intervals(self):
                return base_group.intervals.__get__(self, type(self))

            @intervals.setter
            def intervals(self, value):
                base_group.intervals.__set__(self, CountedIntervals(value))

        monkeypatch.setattr(charts_module, "_Sample", CountedSample)
        monkeypatch.setattr(charts_module, "_Group", CountedGroup)

        original_admit = charts_module._admit_task_point

        def counted_admit(point):
            probe.validation_visits += 1
            return original_admit(point)

        monkeypatch.setattr(charts_module, "_admit_task_point", counted_admit)

        original_equal = charts_module._samples_equal_at

        def counted_equal(left, right, index):
            probe.comparison_visits += 1
            with probe._phase_of("compare"):
                return original_equal(left, right, index)

        monkeypatch.setattr(charts_module, "_samples_equal_at", counted_equal)

        original_aggregate = charts_module._aggregate_members

        def counted_aggregate(samples, first, last, first_run):
            probe.aggregate_member_indices.extend(builtin_range(first, last + 1))
            with probe._phase_of("aggregate"):
                return original_aggregate(samples, first, last, first_run)

        monkeypatch.setattr(charts_module, "_aggregate_members", counted_aggregate)

        original_scale = charts_module._scale_maxima

        def counted_scale(columns, durations):
            probe.scale_column_visits += len(columns)
            return original_scale(columns, durations)

        monkeypatch.setattr(charts_module, "_scale_maxima", counted_scale)

        original_bound = charts_module._bound_pair

        def counted_bound(group):
            with probe._phase_of("bound_pair"):
                pair = original_bound(group)
            probe.bound_reads.append(pair)
            return pair

        monkeypatch.setattr(charts_module, "_bound_pair", counted_bound)

        original_merge = charts_module._merge_groups

        def counted_merge(left, right, first_index, last_index):
            with probe._phase_of("merge"):
                merged = original_merge(left, right, first_index, last_index)
            probe.bound_writes.append(
                (
                    base_group.first_index.__get__(merged, type(merged)),
                    base_group.last_index.__get__(merged, type(merged)),
                )
            )
            return merged

        monkeypatch.setattr(charts_module, "_merge_groups", counted_merge)

        original_promote = charts_module._promote_groups

        def counted_promote(groups):
            probe._promotions += 1
            try:
                with probe._phase_of("promote"):
                    promoted = original_promote(groups)
            finally:
                probe._promotions -= 1
            probe.promotion_output_bounds.extend(
                (
                    base_group.first_index.__get__(group, type(group)),
                    base_group.last_index.__get__(group, type(group)),
                )
                for group in promoted
            )
            return promoted

        monkeypatch.setattr(charts_module, "_promote_groups", counted_promote)

        original_mapped = charts_module._mapped_y

        def counted_mapped(*args):
            probe.geometry_column_visits += 1
            return original_mapped(*args)

        monkeypatch.setattr(charts_module, "_mapped_y", counted_mapped)

        original_geometry = charts_module._curve_geometry

        def counted_geometry(columns, durations, groups, samples, plot):
            with probe._phase_of("geometry"):
                curves, segments = original_geometry(
                    columns, durations, groups, samples, plot
                )
            probe.vertex_emit = sum(len(curve.vertices) for curve in curves)
            probe.closure_emit = 3 * len(segments)
            return curves, segments

        monkeypatch.setattr(charts_module, "_curve_geometry", counted_geometry)

        def counted_range(*args):
            result = builtin_range(*args)
            if probe._promotions:
                probe.promotion_ranges.append((len(result), args))
            return result

        monkeypatch.setattr(charts_module, "range", counted_range, raising=False)
        return self

    # -- counters --------------------------------------------------------

    @contextmanager
    def _phase_of(self, phase: str):
        previous = self._phase
        self._phase = phase
        try:
            yield
        finally:
            self._phase = previous

    def _raw_read(self, field: str) -> None:
        if self._phase in ("aggregate", "compare", "geometry"):
            return
        self.unaccounted_raw_reads.append(field)

    def _bound_read(self, field: str) -> None:
        if self._promotions and self._phase != "bound_pair":
            self.direct_promotion_bound_reads.append(field)

    def _interval_visit(self) -> None:
        if self._phase == "merge":
            self.merge_interval_visits += 1
        elif self._phase == "geometry":
            self.interval_visits += 1


def assert_no_curve_pixels(chart) -> None:
    """Prove no read/write stroke or fill survived as a painted pixel."""

    grab = chart.grab()
    assert isinstance(grab, QPixmap) and not grab.isNull()
    image = grab.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    data = bytes(image.constBits().asstring(image.sizeInBytes()))
    read_rgba = bytes(
        (theme.READ_COLOR.red(), theme.READ_COLOR.green(), theme.READ_COLOR.blue(), 255)
    )
    write_rgba = bytes(
        (theme.WRITE_COLOR.red(), theme.WRITE_COLOR.green(), theme.WRITE_COLOR.blue(), 255)
    )
    pixels = [data[i : i + 4] for i in range(0, len(data), 4)]
    assert read_rgba not in pixels
    assert write_rgba not in pixels


# --------------------------------------------------------------------------
# Time-lane geometry
# --------------------------------------------------------------------------


def test_contiguous_points_produce_one_run_per_nonzero_side(qtbot) -> None:
    task, _ = chart_points(6)
    chart = make_chart(DualSeriesChart, qtbot)
    chart.set_totals(4096, 2048)
    chart.set_task_points(task)

    plot = chart.plot_rect()
    assert plot[2] > plot[0]
    assert plot[3] > plot[1]
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert message(chart).isVisible() is False

    columns = chart.columns()
    assert len(columns) == len(task)
    assert [column.starts_run for column in columns] == [True] + [False] * 5
    assert columns[0].read_bytes == task[0].read_bytes
    assert columns[-1].write_bytes == task[-1].write_bytes

    segments = chart.segments()
    assert_closed_finite_polygons(segments)
    assert_inside_plot(segments, plot)

    reads = by_side(segments, "read")
    writes = by_side(segments, "write")
    assert len(reads) == 1
    assert len(writes) == 1
    assert reads[0].run_index == 0
    assert writes[0].run_index == 0

    # Read above, write below, each normalised against its own maximum: the
    # fixture makes read rates twice the write rates, so a shared scale would
    # leave the write side at half height.
    left, top, right, bottom = plot
    centre = (top + bottom) / 2.0
    assert min(y for _, y in reads[0].polygon) == pytest.approx(top, abs=1e-6)
    assert max(y for _, y in writes[0].polygon) == pytest.approx(bottom, abs=1e-6)
    rect_width = right - left
    # The outer X support is the first and last cross-column fragment midpoints,
    # clamped inside the run inset; it is not a step spanning the raw extent.
    read_xs = [x for x, _ in reads[0].polygon]
    assert min(read_xs) > left
    assert max(read_xs) < right
    assert min(read_xs) == pytest.approx(left + rect_width / 12, abs=1e-6)
    assert max(read_xs) == pytest.approx(left + rect_width * 11 / 12, abs=1e-6)


def test_one_bucket_hole_splits_runs_without_points_or_edges_in_hole(qtbot) -> None:
    task, _ = chart_points(6, hole_at=3)
    chart = make_chart(SeriesChart, qtbot)
    chart.set_task_points(task)

    plot = chart.plot_rect()
    hole_start = task[2].bucket_start_mono_ns + task[2].bucket_secs * 10**9
    hole_end = task[3].bucket_start_mono_ns
    assert hole_end > hole_start
    hole_left = mapped_x(hole_start, task, plot)
    hole_right = mapped_x(hole_end, task, plot)

    segments = chart.segments()
    assert_closed_finite_polygons(segments)
    assert_inside_plot(segments, plot)
    for side in ("read", "write"):
        side_segments = by_side(segments, side)
        assert len(side_segments) == 2, side_segments
        assert [segment.run_index for segment in side_segments] == [0, 1]
        for segment in side_segments:
            xs = [x for x, _ in segment.polygon]
            # Entire support, not just sampled vertices, excludes the hole.
            assert max(xs) < hole_left or min(xs) > hole_right, (side, segment, hole_left, hole_right)

    # Bytes in the buckets themselves are untouched by the split.
    assert sum(column.read_bytes for column in chart.columns()) == sum(
        point.read_bytes for point in task
    )
    assert sum(column.write_bytes for column in chart.columns()) == sum(
        point.write_bytes for point in task
    )


def test_task_suspend_splits_after_point_but_device_does_not_inherit_it(qtbot) -> None:
    task, device = chart_points(6, suspend_at=2)
    chart = make_chart(DualSeriesChart, qtbot)
    chart.set_task_points(task)

    segments = chart.segments()
    assert_closed_finite_polygons(segments)
    for side in ("read", "write"):
        side_segments = by_side(segments, side)
        assert len(side_segments) == 2, side_segments
        assert [segment.run_index for segment in side_segments] == [0, 1]

    # The suspended bucket keeps its measured bytes and peaks.
    columns = chart.columns()
    assert columns[2].read_bytes == task[2].read_bytes
    assert columns[2].write_bytes == task[2].write_bytes
    assert columns[2].peak_read_bps == task[2].peak_read_bps
    assert columns[3].starts_run is True

    lane = make_chart(DeviceLane, qtbot)
    lane.set_device("8:0")
    lane.set_device_points(device)
    lane_segments = lane.segments()
    assert_closed_finite_polygons(lane_segments)
    for side in ("read", "write"):
        assert len(by_side(lane_segments, side)) == 1


def test_set_unavailable_clears_shapes_and_exposes_exact_reason(qtbot) -> None:
    task, device = chart_points(5)
    widgets = [SeriesChart(), DualSeriesChart(), DeviceLane()]
    for widget in widgets:
        qtbot.addWidget(widget)
        widget.resize(320, 160)
        widget.show()
        if isinstance(widget, DeviceLane):
            widget.set_device_points(device)
        else:
            widget.set_task_points(task)
        assert widget.columns() != ()
        assert widget.segments() != ()

    reason = "series unavailable"
    for widget in widgets:
        widget.set_unavailable(reason)
        assert widget.columns() == ()
        assert widget.segments() == ()
        assert widget.is_available() is False
        assert widget.unavailable_reason() == reason
        label = message(widget)
        assert label.text() == f"unavailable: {reason}"
        assert label.isVisible() is True
        with pytest.raises(ValueError):
            widget.set_unavailable("")

    # Returning to real data clears the reason and rebuilds the geometry.
    chart = widgets[0]
    chart.set_task_points(task)
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert chart.segments() != ()
    assert chart.findChild(QLabel, "chart_message").isVisible() is False


def test_downsampling_preserves_bytes_peak_maxima_and_run_starts(qtbot) -> None:
    chart = make_chart(DualSeriesChart, qtbot, width=200, height=180)
    plot = chart.plot_rect()
    budget = width_of(plot)
    assert budget > 0

    full = [
        task_point(
            BASE_NS + index * BUCKET_NS,
            read_bytes=1024 + index,
            write_bytes=512 + 2 * index,
            peak_read_bps=4096 + index if index % 2 == 0 else None,
            peak_write_bps=None,
        )
        for index in range(3 * budget + 5)
    ]

    # Groups are consecutive index anchors of four points here. Remove one
    # bucket from the middle of a group so that column holds contributors from
    # both sides of a real hole while the run break lands inside that column.
    hole_at = 17
    assert (hole_at - 1) // 4 == hole_at // 4
    points = tuple(full[:hole_at] + full[hole_at + 1 :])
    chart.set_task_points(points)

    assert len(points) > 2 * budget
    columns = chart.columns()
    assert 0 < len(columns) <= budget
    assert sum(column.read_bytes for column in columns) == sum(
        point.read_bytes for point in points
    )
    assert sum(column.write_bytes for column in columns) == sum(
        point.write_bytes for point in points
    )
    assert sum(column.end_mono_ns - column.start_mono_ns for column in columns) > 0

    defined_reads = [point.peak_read_bps for point in points if point.peak_read_bps is not None]
    assert defined_reads
    assert max(
        column.peak_read_bps for column in columns if column.peak_read_bps is not None
    ) == max(defined_reads)
    # All contributor peaks are None on the write side, so the columns stay None.
    assert all(column.peak_write_bps is None for column in columns)

    assert columns[0].starts_run is True
    internal = [column for column in columns[1:] if column.starts_run]
    assert len(internal) == 1
    # The internal break column carries contributors from both sides of the hole.
    left = points[hole_at - 1]
    right = points[hole_at]
    break_column = internal[0]
    assert break_column.start_mono_ns <= left.bucket_start_mono_ns
    assert break_column.end_mono_ns >= right.bucket_start_mono_ns + right.bucket_secs * 10**9


def test_downsampling_across_gap_never_spans_its_horizontal_interval(qtbot) -> None:
    chart = make_chart(DualSeriesChart, qtbot, width=200, height=180)
    plot = chart.plot_rect()
    budget = width_of(plot)
    assert budget > 0

    full = [
        task_point(
            BASE_NS + index * BUCKET_NS,
            read_bytes=2048 + 3 * index,
            write_bytes=1024 + index,
            peak_read_bps=8192 + index,
            peak_write_bps=2048 + index,
        )
        for index in range(3 * budget + 7)
    ]

    # A four-member group spans the hole when its second and third members are
    # non-adjacent; that column then carries two canonical fragments.
    hole_at = 17
    points = tuple(full[:hole_at] + full[hole_at + 1 :])
    chart.set_task_points(points)

    hole_start = points[hole_at - 1].bucket_start_mono_ns + points[
        hole_at - 1
    ].bucket_secs * 10**9
    hole_end = points[hole_at].bucket_start_mono_ns
    assert hole_end > hole_start
    hole_left = mapped_x(hole_start, points, plot)
    hole_right = mapped_x(hole_end, points, plot)

    columns = chart.columns()
    assert len(columns) <= budget
    left = points[hole_at - 1]
    right = points[hole_at]
    shared = [
        column
        for column in columns
        if column.starts_run
        and column.start_mono_ns <= left.bucket_start_mono_ns
        and column.end_mono_ns >= right.bucket_start_mono_ns + right.bucket_secs * 10**9
    ]
    assert shared, "a column must hold both sides of the hole"
    assert sum(column.read_bytes for column in columns) == sum(
        point.read_bytes for point in points
    )
    assert sum(column.write_bytes for column in columns) == sum(
        point.write_bytes for point in points
    )

    segments = chart.segments()
    assert_closed_finite_polygons(segments)
    assert_inside_plot(segments, plot)
    for side in ("read", "write"):
        side_segments = by_side(segments, side)
        assert len(side_segments) >= 2, side_segments
        for segment in side_segments:
            xs = [x for x, _ in segment.polygon]
            assert max(xs) < hole_left or min(xs) > hole_right, (
                side,
                segment,
                hole_left,
                hole_right,
            )
        assert [segment.run_index for segment in side_segments] == sorted(
            segment.run_index for segment in side_segments
        )


def test_chart_resize_rebuilds_observations_and_keeps_geometry_in_plot(qtbot) -> None:
    task, _ = chart_points(120, hole_at=60)
    chart = make_chart(DualSeriesChart, qtbot, width=600, height=200)
    chart.set_task_points(task)

    wide_plot = chart.plot_rect()
    wide = chart.columns()
    assert len(wide) == len(task)
    assert len(wide) <= width_of(wide_plot)

    chart.resize(240, 200)
    narrow_plot = chart.plot_rect()
    narrow = chart.columns()
    assert 0 < len(narrow) <= width_of(narrow_plot)
    assert len(narrow) < len(wide)

    # A collapsed width exposes no observations but retains the source data.
    chart.resize(120, 200)
    assert width_of(chart.plot_rect()) == 0
    assert chart.columns() == ()
    assert chart.segments() == ()

    # A collapsed height does the same.
    chart.resize(400, 10)
    collapsed = chart.plot_rect()
    assert collapsed[3] == collapsed[1]
    assert chart.columns() == ()
    assert chart.segments() == ()

    # Restored geometry is rebuilt from the retained points with no stale hole.
    chart.resize(300, 180)
    plot = chart.plot_rect()
    columns = chart.columns()
    assert 0 < len(columns) <= width_of(plot)
    assert sum(column.read_bytes for column in columns) == sum(
        point.read_bytes for point in task
    )
    hole_start = task[59].bucket_start_mono_ns + task[59].bucket_secs * 10**9
    hole_end = task[60].bucket_start_mono_ns
    hole_left = mapped_x(hole_start, task, plot)
    hole_right = mapped_x(hole_end, task, plot)
    segments = chart.segments()
    assert_closed_finite_polygons(segments)
    assert_inside_plot(segments, plot)
    for segment in segments:
        xs = [x for x, _ in segment.polygon]
        assert max(xs) < hole_left or min(xs) > hole_right, segment
    assert chart.columns() == columns  # stable when the size is unchanged


def test_empty_available_series_draws_no_zero_line(qtbot) -> None:
    chart = make_chart(SeriesChart, qtbot)
    chart.set_task_points(())

    assert chart.columns() == ()
    assert chart.segments() == ()
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    label = message(chart)
    assert label.text() == NO_SAMPLES
    assert label.isVisible() is True

    grab = chart.grab()
    assert isinstance(grab, QPixmap) and not grab.isNull()
    image = grab.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    data = bytes(image.constBits().asstring(image.sizeInBytes()))
    read_rgba = bytes(
        (theme.READ_COLOR.red(), theme.READ_COLOR.green(), theme.READ_COLOR.blue(), 255)
    )
    write_rgba = bytes(
        (theme.WRITE_COLOR.red(), theme.WRITE_COLOR.green(), theme.WRITE_COLOR.blue(), 255)
    )
    pixels = [data[i : i + 4] for i in range(0, len(data), 4)]
    assert read_rgba not in pixels
    assert write_rgba not in pixels

    # A previously unavailable chart returns to the available-empty state.
    chart.set_unavailable("series unavailable")
    chart.set_task_points(())
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert chart.columns() == ()
    assert message(chart).text() == NO_SAMPLES


def test_coverage_counts_do_not_invent_chart_breaks(qtbot) -> None:
    task, _ = chart_points(6)
    # Coverage counts are not part of a SeriesPoint, so the lane cannot place them.
    assert not hasattr(task[0], "gap_intervals")
    assert not hasattr(task[0], "spans_suspend_intervals")

    contiguous = make_chart(SeriesChart, qtbot)
    contiguous.set_task_points(task)
    for side in ("read", "write"):
        assert len(by_side(contiguous.segments(), side)) == 1

    suspended_task, _ = chart_points(6, suspend_at=1)
    suspended = make_chart(SeriesChart, qtbot)
    suspended.set_task_points(suspended_task)
    for side in ("read", "write"):
        assert len(by_side(suspended.segments(), side)) == 2

    holed_task, _ = chart_points(6, hole_at=2)
    holed = make_chart(SeriesChart, qtbot)
    holed.set_task_points(holed_task)
    for side in ("read", "write"):
        assert len(by_side(holed.segments(), side)) == 2

    # Adjacent buckets with a suspension-count-only coverage stay contiguous.
    assert [column.starts_run for column in contiguous.columns()] == [True] + [False] * 5


def test_downsampled_column_rate_divides_by_observed_durations_across_hole(
    qtbot,
) -> None:
    chart = make_chart(DualSeriesChart, qtbot, width=200, height=180)
    budget = width_of(chart.plot_rect())
    assert budget == 40

    # Every retained bucket is identical, so every honestly aggregated column
    # has the same rate and must reach its own full half-height.
    full = [
        task_point(BASE_NS + index * BUCKET_NS, read_bytes=1024, write_bytes=512)
        for index in range(3 * budget)
    ]
    # Drop one bucket inside the first four-member group: its neighbours keep
    # their bytes and share one column across a real one-bucket hole. That
    # column's envelope is five buckets wide, but its contributors were only
    # ever observed for four of them.
    points = tuple(full[:1] + full[2:])
    chart.set_task_points(points)

    columns = chart.columns()
    assert len(columns) == 30
    assert len(columns) <= budget
    assert chart._group_width == 4
    mixed = columns[0]
    assert mixed.starts_run is True
    assert mixed.end_mono_ns - mixed.start_mono_ns == 5 * BUCKET_NS
    assert (
        sum(
            1
            for point in points
            if mixed.start_mono_ns <= point.bucket_start_mono_ns < mixed.end_mono_ns
        )
        == 4
    )
    assert (mixed.read_bytes, mixed.write_bytes) == (4096, 2048)

    plot = chart.plot_rect()
    _, top, _, bottom = plot
    centre = (top + bottom) / 2.0
    tolerance = 1e-6
    segments = chart.segments()
    assert_closed_finite_polygons(segments)
    assert_inside_plot(segments, plot)

    # Averaging over the summed observed durations keeps the isolated bucket
    # before the hole at the full rate. Dividing by the envelope would leave
    # the first run - the one built only from that mixed column - four fifths
    # tall, at a height strictly between the centre line and the plot edge.
    for segment in segments:
        ys = [y for _, y in segment.polygon]
        if segment.side == "read":
            assert min(ys) == pytest.approx(top, abs=tolerance), segment
            assert all(
                min(abs(y - top), abs(y - centre)) <= tolerance for y in ys
            ), segment
        else:
            assert max(ys) == pytest.approx(bottom, abs=tolerance), segment
            assert all(
                min(abs(y - bottom), abs(y - centre)) <= tolerance for y in ys
            ), segment


def test_grouping_anchors_power_of_two_buckets_at_sequence_start(qtbot) -> None:
    chart = make_chart(DualSeriesChart, qtbot, width=200, height=180)
    budget = width_of(chart.plot_rect())
    assert budget == 40

    count = 127
    assert count > budget
    points = tuple(
        task_point(
            BASE_NS + index * BUCKET_NS,
            read_bytes=index + 1,
            write_bytes=3 * (index + 1),
        )
        for index in range(count)
    )
    chart.set_task_points(points)

    # The minimal power-of-two group with ceil(127/g) <= 40 is g=4. Group j
    # holds the consecutive indices 4j .. 4j+3, anchored at the sequence start;
    # there is no time-pixel bin and the final group is short.
    expected_read = [16 * index + 10 for index in range(31)] + [125 + 126 + 127]
    expected_write = [3 * value for value in expected_read]

    columns = chart.columns()
    assert chart._group_width == 4
    assert len(columns) == 32
    assert len(columns) <= budget
    assert [column.read_bytes for column in columns] == expected_read
    assert [column.write_bytes for column in columns] == expected_write
    assert [column.start_mono_ns for column in columns] == [
        BASE_NS + 4 * index * BUCKET_NS for index in range(32)
    ]
    assert [column.end_mono_ns for column in columns] == [
        BASE_NS + (4 * index + 4) * BUCKET_NS for index in range(31)
    ] + [BASE_NS + 127 * BUCKET_NS]
    assert group_bounds(chart) == [(4 * index, 4 * index + 3) for index in range(31)] + [(124, 126)]
    # Every point contributes to exactly one column.
    assert sum(expected_read) == count * (count + 1) // 2


def test_empty_bins_emit_no_zero_byte_columns(qtbot) -> None:
    chart = make_chart(DualSeriesChart, qtbot, width=200, height=180)
    plot = chart.plot_rect()
    budget = width_of(plot)
    assert budget == 40

    # Twenty-five contiguous buckets, a long recorded gap, then twenty-five
    # more. The gap is unmeasured time, not a run of measured-zero buckets.
    slots = [*range(25), *range(250, 275)]
    points = tuple(
        task_point(
            BASE_NS + slot * BUCKET_NS,
            read_bytes=slot + 1,
            write_bytes=2 * (slot + 1),
        )
        for slot in slots
    )
    assert len(points) > budget
    chart.set_task_points(points)

    assert chart._group_width == 2
    columns = chart.columns()
    # Twenty-five index-anchored pairs. No synthetic column is created for the
    # 225 unmeasured buckets between the two runs.
    assert len(columns) == 25
    assert len(columns) <= budget
    expected_read = (
        [4 * index + 3 for index in range(12)]
        + [25 + 251]
        + [4 * index + 453 for index in range(13, 25)]
    )
    assert [column.read_bytes for column in columns] == expected_read
    assert [column.write_bytes for column in columns] == [
        2 * value for value in expected_read
    ]
    assert all(column.read_bytes > 0 and column.write_bytes > 0 for column in columns)
    assert sum(column.read_bytes for column in columns) == sum(
        point.read_bytes for point in points
    )
    assert sum(column.write_bytes for column in columns) == sum(
        point.write_bytes for point in points
    )
    assert [column.starts_run for column in columns] == (
        [True] + [False] * 11 + [True] + [False] * 12
    )
    # The gap is carried inside one column envelope, but its canonical run
    # intervals stay two separate supports and nothing is filled between them.
    shared = columns[12]
    assert shared.start_mono_ns == BASE_NS + 24 * BUCKET_NS
    assert shared.end_mono_ns == BASE_NS + 251 * BUCKET_NS
    assert stored_intervals(chart)[12] == [
        (0, BASE_NS + 24 * BUCKET_NS, BASE_NS + 25 * BUCKET_NS),
        (1, BASE_NS + 250 * BUCKET_NS, BASE_NS + 251 * BUCKET_NS),
    ]
    hole_left = mapped_x(BASE_NS + 25 * BUCKET_NS, points, plot)
    hole_right = mapped_x(BASE_NS + 250 * BUCKET_NS, points, plot)
    for segment in chart.segments():
        xs = [x for x, _ in segment.polygon]
        assert max(xs) < hole_left or min(xs) > hole_right, segment


def test_device_points_map_rbytes_to_read_and_wbytes_to_write(qtbot) -> None:
    lane = make_chart(DeviceLane, qtbot)
    lane.set_device("8:0")
    device = (
        device_point(BASE_NS, rbytes=7000, wbytes=11),
        device_point(BASE_NS + BUCKET_NS, rbytes=500, wbytes=90000),
        device_point(BASE_NS + 2 * BUCKET_NS, rbytes=0, wbytes=0),
    )
    lane.set_device_points(device)

    columns = lane.columns()
    assert len(columns) == len(device)
    assert [column.read_bytes for column in columns] == [7000, 500, 0]
    assert [column.write_bytes for column in columns] == [11, 90000, 0]
    assert sum(column.read_bytes for column in columns) == 7500
    assert sum(column.write_bytes for column in columns) == 90011


def test_setters_reject_wrong_point_types_unordered_points_and_invalid_totals(
    qtbot,
) -> None:
    task, device = chart_points(4)

    class DuckSeriesPoint:
        """Structurally plausible, but not a contract SeriesPoint."""

        bucket_start_mono_ns = BASE_NS
        bucket_secs = BUCKET_SECS
        read_bytes = 1
        write_bytes = 1
        peak_read_bps = None
        peak_write_bps = None
        spans_suspend = False

    class DuckDevicePoint:
        """Structurally plausible, but not a contract DeviceSeriesPoint."""

        bucket_start_mono_ns = BASE_NS
        bucket_secs = BUCKET_SECS
        dev = "8:0"
        rbytes = 1
        wbytes = 1

    chart = make_chart(SeriesChart, qtbot)
    chart.set_task_points(task)
    assert len(chart.columns()) == len(task)
    for wrong in (DuckSeriesPoint(), device[0], object()):
        with pytest.raises(ValueError):
            chart.set_task_points((wrong,))  # type: ignore[arg-type]
    # Equal starts are non-increasing; an overlong bucket overlaps its successor.
    with pytest.raises(ValueError):
        chart.set_task_points((task[0], task[0]))
    with pytest.raises(ValueError):
        chart.set_task_points((task[1], task[0]))
    with pytest.raises(ValueError):
        chart.set_task_points(
            (task_point(BASE_NS, bucket_secs=20), task_point(BASE_NS + BUCKET_NS))
        )

    lane = make_chart(DeviceLane, qtbot)
    lane.set_device_points(device)
    assert len(lane.columns()) == len(device)
    for wrong in (DuckDevicePoint(), task[0], object()):
        with pytest.raises(ValueError):
            lane.set_device_points((wrong,))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        lane.set_device_points((device[0], device[0]))
    with pytest.raises(ValueError):
        lane.set_device_points((device[1], device[0]))
    with pytest.raises(ValueError):
        lane.set_device_points(
            (
                DeviceSeriesPoint(BASE_NS, 20, "8:0", 1, 1),
                DeviceSeriesPoint(BASE_NS + BUCKET_NS, 10, "8:0", 1, 1),
            )
        )

    dual = make_chart(DualSeriesChart, qtbot)
    dual.set_totals(0, 0)  # measured zero is a value
    with pytest.raises(ValueError):
        dual.set_totals(-1, 0)
    with pytest.raises(ValueError):
        dual.set_totals(0, -1)

    with pytest.raises(ValueError):
        lane.set_device("")
    lane.set_device("8:1")


# --------------------------------------------------------------------------
# Live append decision table, grouping, run intervals and cost
# --------------------------------------------------------------------------


def test_live_append_row_1_invalid_preserves_state(qtbot, monkeypatch) -> None:
    points = dense_points(5)
    identity = ("task", "app", ("range", 1, 2, 0, 100), 0, "raw")
    chart = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=identity)
    updates = watch_updates(chart, monkeypatch)
    before = retained_state(chart)

    class DuckSeriesPoint:
        """Structurally plausible, but not a contract SeriesPoint."""

        bucket_start_mono_ns = 0
        bucket_secs = 10
        read_bytes = 1
        write_bytes = 1
        peak_read_bps = None
        peak_write_bps = None
        peak_total_bps = None
        wall_start_seconds = None
        spans_suspend = False

    invalid_sequences = [
        (object(),),
        (device_point(0),),
        (DuckSeriesPoint(),),
        (points[0], points[0]),
        (points[1], points[0]),
        (task_point(0, bucket_secs=20), task_point(BUCKET_NS)),
    ]
    for invalid in invalid_sequences:
        with pytest.raises(ValueError):
            chart.append_task_points(invalid, identity=identity)  # type: ignore[arg-type]
        after = retained_state(chart)
        assert after == before
        assert chart._samples is before["samples"]
        assert chart._groups is before["groups"]
        assert chart._columns is before["columns"]
        assert chart._curves is before["curves"]
        assert chart._segments is before["segments"]
    with pytest.raises(ValueError):
        chart.append_task_points(None, identity=identity)  # type: ignore[arg-type]
    assert retained_state(chart) == before
    assert updates == []

    # An unavailable chart is equally immune to an invalid append.
    chart.set_unavailable("series unavailable")
    unavailable = retained_state(chart)
    unavailable_updates = list(updates)
    with pytest.raises(ValueError):
        chart.append_task_points((points[1], points[0]), identity=identity)
    assert retained_state(chart) == unavailable
    assert updates == unavailable_updates

    lane = make_chart(DeviceLane, qtbot, width=WIDE, height=180)
    device = tuple(device_point(index * BUCKET_NS) for index in range(4))
    lane.set_device_points(device, identity=("device", "a"))
    lane_updates = watch_updates(lane, monkeypatch)
    lane_before = retained_state(lane)
    for invalid in (
        (task_point(0),),
        (object(),),
        (device[0], device[0]),
        (device[1], device[0]),
        (None,),
    ):
        with pytest.raises(ValueError):
            lane.append_device_points(invalid, identity=("device", "a"))  # type: ignore[arg-type]
        assert retained_state(lane) == lane_before
        assert lane._groups is lane_before["groups"]
        assert lane._curves is lane_before["curves"]
        assert lane._segments is lane_before["segments"]
    assert lane_updates == []


def test_live_append_row_2_unavailable_rebuilds_even_empty_or_same_points(
    qtbot, monkeypatch
) -> None:
    points = dense_points(6)
    identity = ("task", "app", ("since_boot", 3), 0, "raw")
    chart = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=identity)
    chart.set_unavailable("series unavailable")
    updates = watch_updates(chart, monkeypatch)

    chart.append_task_points(points, identity=identity)
    assert updates == [True]
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert chart._identity == identity
    assert chart.columns() != ()
    assert chart.curves() != ()
    assert sum(column.read_bytes for column in chart.columns()) == 6
    assert sum(column.write_bytes for column in chart.columns()) == 12
    assert message(chart).isVisible() is False

    chart.set_unavailable("series unavailable")
    chart.append_task_points((), identity=identity)
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert chart.columns() == ()
    assert chart.curves() == ()
    assert chart.segments() == ()
    assert chart._samples == ()
    assert chart._identity == identity
    assert message(chart).text() == NO_SAMPLES
    assert message(chart).isVisible() is True


def test_live_append_row_3_identity_change_rebuilds(qtbot, monkeypatch) -> None:
    points = dense_points(10)
    chart = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=("task", "a"))
    before = chart.columns()
    calls, visits = watch_aggregation(monkeypatch)
    updates = watch_updates(chart, monkeypatch)
    chart.append_task_points(points, identity=("task", "b"))
    assert calls
    assert visits == list(range(10))
    assert updates == [True]
    assert chart._identity == ("task", "b")
    assert chart.columns() == before

    lane = make_chart(DeviceLane, qtbot, width=WIDE, height=180)
    lane.set_device("8:0")
    first = tuple(device_point(index * BUCKET_NS, dev="8:0") for index in range(5))
    second = tuple(
        device_point(index * BUCKET_NS, dev="8:1", rbytes=99, wbytes=7)
        for index in range(3)
    )
    lane.set_device_points(first, identity=("device", "a", "8:0"))
    lane.set_device("8:1")
    lane.append_device_points(second, identity=("device", "a", "8:1"))
    assert lane._identity == ("device", "a", "8:1")
    assert [column.read_bytes for column in lane.columns()] == [99, 99, 99]
    assert [column.write_bytes for column in lane.columns()] == [7, 7, 7]
    assert sum(column.read_bytes for column in lane.columns()) == 297


def test_live_append_row_4_empty_transition_and_noop(qtbot, monkeypatch) -> None:
    identity = ("task", "a")
    points = dense_points(6)
    chart = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=identity)
    updates = watch_updates(chart, monkeypatch)

    chart.append_task_points((), identity=identity)
    assert updates == [True]
    assert chart._samples == ()
    assert chart._groups == ()
    assert chart._group_width == 1
    assert chart.columns() == ()
    assert chart.curves() == ()
    assert chart.segments() == ()
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert message(chart).text() == NO_SAMPLES
    assert message(chart).isVisible() is True
    assert_no_curve_pixels(chart)

    updates.clear()
    calls, visits = watch_aggregation(monkeypatch)
    scale = watch_scale(monkeypatch)
    chart.append_task_points((), identity=identity)
    assert updates == []
    assert calls == [] and visits == [] and scale == []

    # A different identity is not the empty no-op row.
    chart.append_task_points((), identity=("task", "b"))
    assert updates == [True]
    assert chart._identity == ("task", "b")
    assert chart.columns() == ()


def test_live_append_row_5_empty_to_nonempty_has_no_tail(qtbot, monkeypatch) -> None:
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(())
    calls, visits = watch_aggregation(monkeypatch)
    updates = watch_updates(chart, monkeypatch)
    points = dense_points(5, read_bytes=3, write_bytes=4)
    chart.append_task_points(points, identity=identity)
    assert updates == [True]
    assert visits == [0, 1, 2, 3, 4]
    assert len(chart.columns()) == 5
    assert sum(column.read_bytes for column in chart.columns()) == 15
    assert sum(column.write_bytes for column in chart.columns()) == 20
    assert chart.curves() != () and chart.segments() != ()
    assert chart._identity == identity
    assert message(chart).isVisible() is False

    lane = make_chart(DeviceLane, qtbot, width=WIDE, height=180)
    lane.set_device_points(())
    lane.append_device_points(
        tuple(device_point(index * BUCKET_NS, rbytes=9, wbytes=5) for index in range(4)),
        identity=("device", "a"),
    )
    assert len(lane.columns()) == 4
    assert sum(column.read_bytes for column in lane.columns()) == 36
    assert sum(column.write_bytes for column in lane.columns()) == 20
    assert lane.curves() != ()


def test_live_append_row_6_equal_input_is_noop(qtbot, monkeypatch) -> None:
    points = dense_points(31)
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=identity)
    state = retained_state(chart)
    updates = watch_updates(chart, monkeypatch)
    calls, visits = watch_aggregation(monkeypatch)
    scale = watch_scale(monkeypatch)
    builds = watch_build_groups(monkeypatch)

    chart.append_task_points(points, identity=identity)
    assert updates == []
    assert calls == [] and visits == [] and scale == [] and builds == []
    assert retained_state(chart) == state
    assert chart._samples is state["samples"]
    assert chart._groups is state["groups"]
    assert chart._columns is state["columns"]
    assert chart._curves is state["curves"]
    assert chart._segments is state["segments"]

    # A full setter followed by an equal append reuses the stored grouping.
    chart.set_task_points(points, identity=identity)
    after_setter = retained_state(chart)
    updates.clear()
    calls.clear()
    visits.clear()
    scale.clear()
    builds.clear()
    chart.append_task_points(points, identity=identity)
    assert updates == []
    assert calls == [] and visits == [] and scale == [] and builds == []
    assert retained_state(chart) == after_setter
    assert chart._groups is after_setter["groups"]


def test_live_append_row_7_tail_replacement_rebuilds_one_group(
    qtbot, monkeypatch
) -> None:
    points = list(dense_points(31))
    points[28] = replace(points[28], peak_read_bps=1000)
    points[29] = replace(points[29], peak_write_bps=3000)
    points[30] = replace(points[30], peak_read_bps=9000, peak_write_bps=8000)
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(tuple(points), identity=identity)
    assert group_bounds(chart) == [
        (0, 3),
        (4, 7),
        (8, 11),
        (12, 15),
        (16, 19),
        (20, 23),
        (24, 27),
        (28, 30),
    ]
    before = chart.columns()
    calls, visits = watch_aggregation(monkeypatch)
    updates = watch_updates(chart, monkeypatch)

    changed_tail = replace(
        points[30], read_bytes=5, write_bytes=1, peak_read_bps=100, peak_write_bps=None
    )
    chart.append_task_points(tuple(points[:30]) + (changed_tail,), identity=identity)
    assert calls == [(28, 30)]
    assert visits == [28, 29, 30]
    assert updates == [True]
    after = chart.columns()
    assert after[:7] == before[:7]
    tail = after[7]
    assert (tail.read_bytes, tail.write_bytes) == (7, 5)
    # The old sole maxima are gone, including a lowered peak and a removed one.
    assert before[7].peak_read_bps == 9000
    assert before[7].peak_write_bps == 8000
    assert tail.peak_read_bps == 1000
    assert tail.peak_write_bps == 3000

    # A measured zero stays a zero in the retained raw input, while the tail
    # column's bytes are the exact member sum and its peaks stay defined by
    # the other members rather than by the zero point.
    zero_tail = replace(points[30], read_bytes=0, write_bytes=0, peak_read_bps=None, peak_write_bps=None)
    chart.append_task_points(tuple(points[:30]) + (zero_tail,), identity=identity)
    zeroed = chart.columns()[7]
    assert (zeroed.read_bytes, zeroed.write_bytes) == (2, 4)
    assert (chart._samples[30].read_bytes, chart._samples[30].write_bytes) == (0, 0)
    assert chart._samples[30].peak_read_bps is None
    assert zeroed.peak_read_bps == 1000
    assert zeroed.peak_write_bps == 3000
    assert chart.columns() == after[:7] + (zeroed,)


def test_live_append_row_8_exact_prefix_appends_only(qtbot, monkeypatch) -> None:
    points = dense_points(31)
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=identity)
    before = chart.columns()
    calls, visits = watch_aggregation(monkeypatch)
    builds = watch_build_groups(monkeypatch)
    updates = watch_updates(chart, monkeypatch)

    appended = points + (task_point(31 * TEN_NS, read_bytes=5, write_bytes=7),)
    chart.append_task_points(appended, identity=identity)
    assert calls == [(28, 31)]
    assert visits == [28, 29, 30, 31]
    assert builds == []
    assert updates == [True]
    assert chart._group_width == 4
    assert len(chart.columns()) == 8
    assert chart.columns()[:7] == before[:7]
    assert (chart.columns()[7].read_bytes, chart.columns()[7].write_bytes) == (8, 13)
    assert sum(column.read_bytes for column in chart.columns()) == 36
    assert sum(column.write_bytes for column in chart.columns()) == 69
    assert group_bounds(chart)[-1] == (28, 31)


def test_live_append_row_9_tail_change_then_append(qtbot, monkeypatch) -> None:
    original = list(dense_points(31))
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(tuple(original), identity=identity)
    calls, visits = watch_aggregation(monkeypatch)
    builds = watch_build_groups(monkeypatch)

    changed_tail = task_point(30 * TEN_NS, read_bytes=9, write_bytes=3)
    longer = (
        tuple(original[:30])
        + (changed_tail,)
        + (
            task_point(31 * TEN_NS, read_bytes=5, write_bytes=7),
            task_point(32 * TEN_NS, read_bytes=6, write_bytes=8),
        )
    )
    chart.append_task_points(longer, identity=identity)
    assert calls == [(28, 30), (28, 31), (32, 32)]
    assert visits == [28, 29, 30, 28, 29, 30, 31, 32]
    assert builds == []
    assert chart._group_width == 8
    assert group_bounds(chart) == [(0, 7), (8, 15), (16, 23), (24, 31), (32, 32)]
    assert sum(column.read_bytes for column in chart.columns()) == 50
    assert sum(column.write_bytes for column in chart.columns()) == 78

    # Every raw field participates in exact prefix equality: changing only an
    # absent wall stamp or an absent total peak is an interior contradiction.
    other = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    base = dense_points(6)
    other.set_task_points(base, identity=identity)
    builds.clear()
    with_wall = (replace(base[0], wall_start_seconds=0.0),) + base[1:]
    other.append_task_points(with_wall, identity=identity)
    assert builds == [(6, 8)]
    with_peak_total = (replace(base[0], peak_total_bps=0),) + base[1:]
    other.append_task_points(with_peak_total, identity=identity)
    assert builds == [(6, 8), (6, 8)]
    assert sum(column.read_bytes for column in other.columns()) == 6


def test_live_append_row_10_nonempty_shortening_rebuilds(qtbot, monkeypatch) -> None:
    points = []
    for index in range(10):
        start = index * BUCKET_NS + (BUCKET_NS if index >= 7 else 0)
        points.append(
            task_point(
                start,
                read_bytes=10 + index,
                write_bytes=20 + index,
                peak_read_bps=100 + index,
            )
        )
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(tuple(points), identity=identity)
    assert len({curve.run_index for curve in chart.curves() if curve.side == "read"}) == 2

    calls, visits = watch_aggregation(monkeypatch)
    builds = watch_build_groups(monkeypatch)
    short = tuple(points[:6])
    chart.append_task_points(short, identity=identity)
    assert builds == [(6, 8)]
    assert visits == list(range(6))
    assert chart._group_width == 1
    assert group_bounds(chart) == [(index, index) for index in range(6)]
    assert len(chart.columns()) == 6
    assert [column.peak_read_bps for column in chart.columns()] == [
        100,
        101,
        102,
        103,
        104,
        105,
    ]
    assert [column.starts_run for column in chart.columns()] == [True] + [False] * 5
    assert len({curve.run_index for curve in chart.curves() if curve.side == "read"}) == 1
    assert sum(column.read_bytes for column in chart.columns()) == sum(
        point.read_bytes for point in short
    )


def test_live_append_row_11_interior_contradiction_rebuilds(qtbot, monkeypatch) -> None:
    points = dense_points(31)
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=identity)
    changed = list(points)
    changed[5] = task_point(
        5 * TEN_NS, read_bytes=50, write_bytes=60, spans_suspend=True
    )
    calls, visits = watch_aggregation(monkeypatch)
    builds = watch_build_groups(monkeypatch)

    chart.append_task_points(tuple(changed), identity=identity)
    assert builds == [(31, 8)]
    assert len(visits) == 31
    assert set(visits) == set(range(31))
    assert [column.starts_run for column in chart.columns()] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    assert sum(column.read_bytes for column in chart.columns()) == 30 + 50
    assert sum(column.write_bytes for column in chart.columns()) == 30 * 2 + 60

    longer_points = list(changed)
    longer_points[10] = task_point(10 * TEN_NS, read_bytes=11, write_bytes=12)
    longer = tuple(longer_points) + (task_point(31 * TEN_NS, read_bytes=7, write_bytes=8),)
    calls.clear()
    visits.clear()
    builds.clear()
    chart.append_task_points(longer, identity=identity)
    assert builds == [(32, 8)]
    assert len(visits) == 32
    assert set(visits) == set(range(32))
    assert sum(column.read_bytes for column in chart.columns()) == 29 + 50 + 11 + 7


def test_live_tail_replacement_recomputes_duration_peak_and_run_state(
    qtbot, monkeypatch
) -> None:
    points = dense_points(31)
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=identity)
    assert len(chart.curves()) == 2

    changed_tail = task_point(
        30 * TEN_NS,
        bucket_secs=20,
        read_bytes=10,
        write_bytes=5,
        peak_read_bps=7000,
        peak_write_bps=9000,
        spans_suspend=True,
    )
    appended = tuple(points[:30]) + (
        changed_tail,
        task_point(30 * TEN_NS + 20 * 10**9, read_bytes=3, write_bytes=4),
    )
    calls, visits = watch_aggregation(monkeypatch)
    chart.append_task_points(appended, identity=identity)
    assert calls == [(28, 30), (28, 31)]
    tail = chart._groups[-1]
    assert (tail.first_index, tail.last_index) == (28, 31)
    assert tail.duration_ns == 50 * 10**9
    assert (tail.peak_read_bps, tail.peak_write_bps) == (7000, 9000)
    assert tail.starts_run is True
    assert tail.intervals == (
        (0, 28 * TEN_NS, 30 * TEN_NS + 20 * 10**9),
        (1, 30 * TEN_NS + 20 * 10**9, 30 * TEN_NS + 30 * 10**9),
    )
    assert {curve.run_index for curve in chart.curves() if curve.side == "read"} == {0, 1}
    assert sum(column.read_bytes for column in chart.columns()) == 30 + 10 + 3
    assert sum(column.write_bytes for column in chart.columns()) == 60 + 5 + 4

    # The changed suspension and duration survive a later promotion, and the
    # next contiguous append joins the changed run rather than the old one.
    calls.clear()
    visits.clear()
    promoted = appended + (
        task_point(30 * TEN_NS + 30 * 10**9, read_bytes=1, write_bytes=2),
    )
    chart.append_task_points(promoted, identity=identity)
    assert chart._group_width == 8
    assert group_bounds(chart) == [(0, 7), (8, 15), (16, 23), (24, 31), (32, 32)]
    assert chart._groups[3].intervals == (
        (0, 24 * TEN_NS, 30 * TEN_NS + 20 * 10**9),
        (1, 30 * TEN_NS + 20 * 10**9, 30 * TEN_NS + 30 * 10**9),
    )
    assert chart._groups[4].first_run == 1
    assert chart._groups[4].intervals == (
        (1, 30 * TEN_NS + 30 * 10**9, 30 * TEN_NS + 40 * 10**9),
    )


def test_live_canonical_intervals_coalesce_only_touching_same_run(qtbot) -> None:
    identity = ("task", "a")
    dense = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    dense_points_ = dense_points(6)
    dense.set_task_points(dense_points_, identity=identity)
    assert stored_intervals(dense) == [
        [(0, index * TEN_NS, (index + 1) * TEN_NS)] for index in range(6)
    ]

    fragmented_points = tuple(task_point(index * 2 * TEN_NS) for index in range(31))
    fragmented = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    fragmented.set_task_points(fragmented_points, identity=identity)
    groups = stored_intervals(fragmented)
    assert [len(group) for group in groups] == [4] * 7 + [3]
    all_intervals = [interval for group in groups for interval in group]
    assert [interval[0] for interval in all_intervals] == list(range(31))
    assert all(interval[2] - interval[1] == 10 * 10**9 for interval in all_intervals)

    suspended_points = tuple(
        replace(point, spans_suspend=(index == 2))
        for index, point in enumerate(dense_points(6))
    )
    suspended = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    suspended.set_task_points(suspended_points, identity=identity)
    intervals = [group[0] for group in stored_intervals(suspended)]
    assert [interval[0] for interval in intervals] == [0, 0, 0, 1, 1, 1]
    assert intervals[2][2] == intervals[3][1]
    assert intervals[2][0] != intervals[3][0]

    thirty_two = dense_points(32)
    merged_dense = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    merged_dense.set_task_points(thirty_two, identity=identity)
    merged_dense.append_task_points(
        thirty_two + (task_point(32 * TEN_NS),), identity=identity
    )
    assert merged_dense._groups[0].intervals == ((0, 0, 8 * TEN_NS),)

    split = tuple(
        replace(point, spans_suspend=(index == 3))
        for index, point in enumerate(thirty_two)
    )
    merged_split = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    merged_split.set_task_points(split, identity=identity)
    merged_split.append_task_points(split + (task_point(32 * TEN_NS),), identity=identity)
    assert merged_split._groups[0].intervals == (
        (0, 0, 4 * TEN_NS),
        (1, 4 * TEN_NS, 8 * TEN_NS),
    )
    assert [len(group.intervals) for group in merged_split._groups] == [2, 1, 1, 1, 1]


def test_live_curve_and_fill_support_never_bridge_multiple_runs_in_one_column(
    qtbot,
) -> None:
    times = [0, 10 * 10**9] + [30 * 10**9 + index * 10 * 10**9 for index in range(29)]
    points = tuple(
        task_point(start, read_bytes=100 + index, write_bytes=50 + index)
        for index, start in enumerate(times)
    )
    chart = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=("task", "a"))
    assert len(points) == 31
    assert chart._group_width == 4
    first_group = chart._groups[0]
    assert [interval[0] for interval in first_group.intervals] == [0, 1]
    assert first_group.start == 0
    assert first_group.end == 50 * 10**9
    assert first_group.starts_run is True

    left, top, right, bottom = chart.plot_rect()
    first = points[0].bucket_start_mono_ns
    last_end = points[-1].bucket_start_mono_ns + points[-1].bucket_secs * 10**9

    def mapped(time_ns: int) -> float:
        return left + (time_ns - first) * (right - left) / (last_end - first)

    supports = {
        0: (mapped(0), mapped(20 * 10**9)),
        1: (mapped(30 * 10**9), mapped(last_end)),
    }
    hole_left = mapped(20 * 10**9)
    hole_right = mapped(30 * 10**9)
    assert hole_left < hole_right

    for side in ("read", "write"):
        side_curves = [curve for curve in chart.curves() if curve.side == side]
        side_fills = [segment for segment in chart.segments() if segment.side == side]
        assert [curve.run_index for curve in side_curves] == [0, 1]
        assert len(side_fills) == 2
        for curve, fill in zip(side_curves, side_fills):
            outer_start, outer_end = supports[curve.run_index]
            inset = min(0.25, (outer_end - outer_start) / 4.0)
            xs = [x for x, _ in curve.vertices]
            assert xs == sorted(xs)
            assert all(outer_start < x < outer_end for x in xs)
            assert all(outer_start + inset - 1e-9 <= x <= outer_end - inset + 1e-9 for x in xs)
            fill_xs = [x for x, _ in fill.polygon]
            assert all(outer_start < x < outer_end for x in fill_xs)
            assert min(fill_xs) >= outer_start + inset - 1e-9
            assert max(fill_xs) <= outer_end - inset + 1e-9
            assert max(xs) < hole_left or min(xs) > hole_right


def test_live_inset_handles_eight_fragment_subpixel_and_two_fragment_runs(qtbot) -> None:
    # Eight one-second fragments inside a subpixel support, then far-apart
    # single-bucket runs that make the total time span enormous.
    starts = list(range(8)) + [1000 + index * 1000 for index in range(32)]
    points = tuple(
        task_point(
            start * 10**9,
            bucket_secs=1 if index < 8 else 10,
            read_bytes=500 + index * 100 if index < 8 else 10 + index,
            write_bytes=300 + index * 50 if index < 8 else 5 + index,
        )
        for index, start in enumerate(starts)
    )
    chart = make_chart(SeriesChart, qtbot, width=200, height=180)
    chart.set_task_points(points, identity=("task", "a"))
    assert chart._group_width == 1
    assert len(chart.columns()) == 40
    first = points[0].bucket_start_mono_ns
    last_end = points[-1].bucket_start_mono_ns + points[-1].bucket_secs * 10**9
    left, top, right, bottom = chart.plot_rect()
    centre = (top + bottom) / 2.0

    def mapped(time_ns: int) -> float:
        return left + (time_ns - first) * (right - left) / (last_end - first)

    run_zero = [curve for curve in chart.curves() if curve.run_index == 0]
    assert sorted(curve.side for curve in run_zero) == ["read", "write"]
    outer_start = mapped(0)
    outer_end = mapped(8 * 10**9)
    assert outer_end - outer_start < 1.0
    inset = min(0.25, (outer_end - outer_start) / 4.0)
    low = outer_start + inset
    high = outer_end - inset
    for curve in run_zero:
        xs = [x for x, _ in curve.vertices]
        ys = [y for _, y in curve.vertices]
        assert len(curve.vertices) == 8
        assert xs == sorted(xs)
        assert len(set(xs)) < len(xs)
        assert all(low - 1e-12 <= x <= high + 1e-12 for x in xs)
        assert all(math.isfinite(y) for y in ys)
        assert len(set(ys)) > 1
        if curve.side == "read":
            assert min(ys) == pytest.approx(top, abs=1e-6)
        else:
            assert max(ys) == pytest.approx(bottom, abs=1e-6)
        fill = next(
            segment
            for segment in chart.segments()
            if segment.run_index == 0 and segment.side == curve.side
        )
        expected_polygon = (
            (curve.vertices[0][0], centre),
            *curve.vertices,
            (curve.vertices[-1][0], centre),
            (curve.vertices[0][0], centre),
        )
        assert fill.polygon == expected_polygon

    # A two-fragment run with unequal heights: monotone X inside the inset
    # support, both support edges reached from the fragment midpoints.
    two_times = [0, 100 * 10**9, 200 * 10**9, 210 * 10**9, 220 * 10**9, 230 * 10**9, 400 * 10**9, 500 * 10**9, 600 * 10**9]
    two_reads = [10, 10, 100, 200, 300, 400, 10, 10, 10]
    two_points = tuple(
        task_point(start, read_bytes=read, write_bytes=read // 2)
        for start, read in zip(two_times, two_reads)
    )
    two = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    two.set_task_points(two_points, identity=("task", "a"))
    assert two._group_width == 2
    assert [len(group.intervals) for group in two._groups] == [2, 1, 1, 2, 1]
    run_two = next(curve for curve in two.curves() if curve.run_index == 2 and curve.side == "read")
    assert len(run_two.vertices) == 2
    two_first = two_points[0].bucket_start_mono_ns
    two_last_end = two_points[-1].bucket_start_mono_ns + two_points[-1].bucket_secs * 10**9
    left2, top2, right2, bottom2 = two.plot_rect()

    def two_mapped(time_ns: int) -> float:
        return left2 + (time_ns - two_first) * (right2 - left2) / (two_last_end - two_first)

    run_start = two_mapped(200 * 10**9)
    run_end = two_mapped(240 * 10**9)
    run_inset = min(0.25, (run_end - run_start) / 4.0)
    (x0, y0), (x1, y1) = run_two.vertices
    assert x0 >= run_start + run_inset - 1e-9
    assert x1 <= run_end - run_inset + 1e-9
    assert x0 <= x1
    assert y0 != y1
    assert min(y0, y1) >= top2 - 1e-9 and max(y0, y1) <= (top2 + bottom2) / 2.0 + 1e-9
    assert min(y for curve in two.curves() if curve.side == "read" for _, y in curve.vertices) == pytest.approx(top2, abs=1e-6)


def test_live_single_fragment_nonzero_run_draws_tick(qtbot) -> None:
    points = tuple(
        task_point(index * 2 * TEN_NS, read_bytes=100 * (index + 1), write_bytes=0)
        for index in range(6)
    )
    chart = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(points, identity=("task", "a"))
    assert chart._group_width == 1
    left, top, right, bottom = chart.plot_rect()
    centre = (top + bottom) / 2.0
    first = points[0].bucket_start_mono_ns
    last_end = points[-1].bucket_start_mono_ns + points[-1].bucket_secs * 10**9
    reads = [curve for curve in chart.curves() if curve.side == "read"]
    assert [curve.run_index for curve in reads] == list(range(6))
    assert [curve for curve in chart.curves() if curve.side == "write"] == []
    assert [segment for segment in chart.segments() if segment.side == "write"] == []
    read_fills = [segment for segment in chart.segments() if segment.side == "read"]
    assert len(read_fills) == 6
    for curve, fill in zip(reads, read_fills):
        start = points[curve.run_index].bucket_start_mono_ns
        end = start + points[curve.run_index].bucket_secs * 10**9
        mapped_start = left + (start - first) * (right - left) / (last_end - first)
        mapped_end = left + (end - first) * (right - left) / (last_end - first)
        inset = min(0.25, (mapped_end - mapped_start) / 4.0)
        assert len(curve.vertices) == 2
        (x0, y0), (x1, y1) = curve.vertices
        assert x0 == pytest.approx(mapped_start + inset)
        assert x1 == pytest.approx(mapped_end - inset)
        assert x0 < x1
        assert y0 == pytest.approx(y1)
        assert y0 != pytest.approx(centre)
        assert fill.polygon == (
            (x0, centre),
            (x0, y0),
            (x1, y1),
            (x1, centre),
            (x0, centre),
        )
    assert min(y for curve in reads for _, y in curve.vertices) == pytest.approx(top)


def test_live_scale_maxima_reach_each_half_after_append_merge_and_tail(
    qtbot, monkeypatch
) -> None:
    base = tuple(
        task_point(index * TEN_NS, read_bytes=1 + index, write_bytes=2)
        for index in range(31)
    )
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    chart.set_task_points(base, identity=identity)
    left, top, right, bottom = chart.plot_rect()
    centre = (top + bottom) / 2.0
    half = (bottom - top) / 2.0

    def read_y() -> float:
        return min(
            y for curve in chart.curves() if curve.side == "read" for _, y in curve.vertices
        )

    def write_y() -> float:
        return max(
            y for curve in chart.curves() if curve.side == "write" for _, y in curve.vertices
        )

    assert read_y() == pytest.approx(top, abs=1e-6)
    assert write_y() == pytest.approx(bottom, abs=1e-6)

    appended = base + (task_point(31 * TEN_NS, read_bytes=100000, write_bytes=50000),)
    chart.append_task_points(appended, identity=identity)
    assert read_y() == pytest.approx(top, abs=1e-6)
    assert write_y() == pytest.approx(bottom, abs=1e-6)

    lowered = appended[:31] + (task_point(31 * TEN_NS, read_bytes=1, write_bytes=2),)
    chart.append_task_points(lowered, identity=identity)
    assert len(chart._groups) == 8
    assert read_y() == pytest.approx(top, abs=1e-6)
    run_curve = next(curve for curve in chart.curves() if curve.side == "read")
    # Group [24..27] now holds the exact maximum (25+26+27+28 = 106) while the
    # rebuilt tail group holds 29+30+31+1 = 91. A lingering old 100000 scale
    # would leave the tail essentially on the centre line.
    assert run_curve.vertices[-1][1] == pytest.approx(
        centre - half * (91 / 106), abs=1e-6
    )

    promoted = lowered + (task_point(32 * TEN_NS, read_bytes=1, write_bytes=2),)
    chart.append_task_points(promoted, identity=identity)
    assert chart._group_width == 8
    assert len(chart._groups) == 5
    assert read_y() == pytest.approx(top, abs=1e-6)
    run_curve = next(curve for curve in chart.curves() if curve.side == "read")
    assert len(run_curve.vertices) == 5
    assert run_curve.vertices[3][1] == pytest.approx(top, abs=1e-6)
    assert run_curve.vertices[2][1] == pytest.approx(
        centre - half * (164 / 197), abs=1e-6
    )
    assert all(
        y == pytest.approx(bottom, abs=1e-6)
        for curve in chart.curves()
        if curve.side == "write"
        for _, y in curve.vertices
    )

    zero = make_chart(SeriesChart, qtbot, width=WIDE, height=180)
    zero_points = tuple(
        task_point(index * TEN_NS, read_bytes=0, write_bytes=5 + index)
        for index in range(6)
    )
    zero.set_task_points(zero_points, identity=("task", "a"))
    assert [curve for curve in zero.curves() if curve.side == "read"] == []
    assert [segment for segment in zero.segments() if segment.side == "read"] == []
    for curve in zero.curves():
        for _, y in curve.vertices:
            assert math.isfinite(y)
            assert top <= y <= bottom
    assert max(
        y for curve in zero.curves() if curve.side == "write" for _, y in curve.vertices
    ) == pytest.approx(bottom, abs=1e-6)


def test_live_append_work_stays_inside_changed_groups(qtbot, monkeypatch) -> None:
    assert "members" not in charts_module._Group.__slots__

    tail_chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    points = dense_points(31)
    tail_chart.set_task_points(points, identity=("task", "a"))
    assert group_bounds(tail_chart)[-1] == (28, 30)

    calls, visits = watch_aggregation(monkeypatch)
    builds = watch_build_groups(monkeypatch)
    scale = watch_scale(monkeypatch)
    validation = watch_validation(monkeypatch)
    updates = watch_updates(tail_chart, monkeypatch)
    changed = tuple(points[:30]) + (
        replace(points[30], read_bytes=5, write_bytes=1),
    )
    tail_chart.append_task_points(changed, identity=("task", "a"))
    assert len(validation) == 31
    assert calls == [(28, 30)]
    assert visits == [28, 29, 30]
    assert builds == []
    assert scale == [8]
    assert updates == [True]
    assert sum(column.read_bytes for column in tail_chart.columns()) == 35
    assert sum(column.write_bytes for column in tail_chart.columns()) == 61

    updates.clear()
    calls.clear()
    visits.clear()
    scale.clear()
    tail_chart.append_task_points(changed, identity=("task", "a"))
    # Full raw validation is admitted O(n); no aggregation, scale or paint work.
    assert len(validation) == 62
    assert updates == []
    assert calls == [] and visits == [] and scale == []
    assert builds == []

    promoted_chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    thirty_two = dense_points(32)
    promoted_chart.set_task_points(thirty_two, identity=("task", "a"))
    assert promoted_chart._group_width == 4
    assert len(promoted_chart.columns()) == 8

    calls, visits = watch_aggregation(monkeypatch)
    builds = watch_build_groups(monkeypatch)
    scale = watch_scale(monkeypatch)
    validation = watch_validation(monkeypatch)
    updates = watch_updates(promoted_chart, monkeypatch)
    bound_reads: list[tuple[int, int]] = []
    original_bound = charts_module._bound_pair

    def watch_bound(group):
        pair = original_bound(group)
        bound_reads.append(pair)
        return pair

    monkeypatch.setattr(charts_module, "_bound_pair", watch_bound)

    promoted_chart.append_task_points(
        thirty_two + (task_point(32 * TEN_NS, read_bytes=1, write_bytes=2),),
        identity=("task", "a"),
    )
    assert len(validation) == 33
    assert calls == [(32, 32)]
    assert visits == [32]
    assert builds == []
    assert group_bounds(promoted_chart) == [
        (0, 7),
        (8, 15),
        (16, 23),
        (24, 31),
        (32, 32),
    ]
    assert promoted_chart._group_width == 8
    assert len(promoted_chart._groups) == 5
    assert bound_reads == [
        (0, 3),
        (4, 7),
        (8, 11),
        (12, 15),
        (16, 19),
        (20, 23),
        (24, 27),
        (28, 31),
        (32, 32),
    ]
    assert len(bound_reads) == 9
    assert scale == [5]
    assert updates == [True]
    assert sum(column.read_bytes for column in promoted_chart.columns()) == 33
    assert sum(column.write_bytes for column in promoted_chart.columns()) == 66
    assert promoted_chart._groups[0].intervals == ((0, 0, 8 * TEN_NS),)
    assert all(
        isinstance(group.first_index, int) and isinstance(group.last_index, int)
        for group in promoted_chart._groups
    )


def test_live_grouping_is_minimal_power_of_two_and_promotes_bounds(
    qtbot, monkeypatch
) -> None:
    # No retained per-sample membership vector exists to copy during promotion.
    assert "members" not in charts_module._Group.__slots__

    threshold_cases = {
        8: (1, 8),
        9: (2, 5),
        16: (2, 8),
        17: (4, 5),
        32: (4, 8),
        33: (8, 5),
    }
    probe = AppendProbe().install(monkeypatch)
    chart = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    assert width_of(chart.plot_rect()) == 8

    for length, (group_width, column_count) in threshold_cases.items():
        points = dense_points(length)
        chart.set_task_points(points, identity=("task", "grouping"))
        assert chart._group_width == group_width
        assert len(chart.columns()) == column_count
        assert group_bounds(chart) == [
            (first, min(first + group_width, length) - 1)
            for first in range(0, length, group_width)
        ]
        assert sum(column.read_bytes for column in chart.columns()) == length
        assert sum(column.write_bytes for column in chart.columns()) == 2 * length

    # §8.7 dense 32 -> 33: g4/C8 -> g8/C5 with exactly five two-integer bounds.
    thirty_two = dense_points(32)
    chart.set_task_points(thirty_two, identity=("task", "promotion"))
    assert chart._group_width == 4
    assert len(chart.columns()) == 8
    probe.reset()
    chart.append_task_points(
        thirty_two + (task_point(32 * TEN_NS, read_bytes=1, write_bytes=2),),
        identity=("task", "promotion"),
    )

    assert chart._group_width == 8
    assert len(chart.columns()) == 5
    assert group_bounds(chart) == [(0, 7), (8, 15), (16, 23), (24, 31), (32, 32)]
    # The append itself aggregates only the new one-point group.
    assert probe.aggregate_member_indices == [32]
    assert probe.unaccounted_raw_reads == []
    assert probe.validation_visits == 33
    assert probe.comparison_visits <= 132
    # Promotion reads exactly one two-integer pair per input column.
    assert probe.bound_reads == [
        (0, 3),
        (4, 7),
        (8, 11),
        (12, 15),
        (16, 19),
        (20, 23),
        (24, 27),
        (28, 31),
        (32, 32),
    ]
    assert len(probe.bound_reads) <= 9
    # Promotion writes merge pairs only: four merged groups, the odd tail kept.
    assert probe.bound_writes == [(0, 7), (8, 15), (16, 23), (24, 31)]
    assert len(probe.bound_writes) <= 5
    assert probe.promotion_output_bounds == [
        (0, 7),
        (8, 15),
        (16, 23),
        (24, 31),
        (32, 32),
    ]
    assert all(
        len(bounds) == 2 and all(isinstance(value, int) for value in bounds)
        for bounds in probe.promotion_output_bounds
    )
    # A direct group-bound read or a per-group member range inside promotion
    # would be a copied member-index vector, not a pair copy.
    assert probe.direct_promotion_bound_reads == []
    assert probe.promotion_ranges == [(5, (0, 9, 2))]
    assert probe.merge_interval_visits <= 9
    assert sum(column.read_bytes for column in chart.columns()) == 33
    assert sum(column.write_bytes for column in chart.columns()) == 66


def test_live_append_cost_counts_fragmented_output_and_cached_aggregation(
    qtbot, monkeypatch
) -> None:
    probe = AppendProbe().install(monkeypatch)

    # -- dense 31 buckets: replace the tail, then the equal-input no-op ----
    dense = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    assert width_of(dense.plot_rect()) == 8
    thirty_one = dense_points(31)
    dense.set_task_points(thirty_one, identity=("task", "cost"))
    assert dense._group_width == 4
    assert group_bounds(dense) == [
        (0, 3),
        (4, 7),
        (8, 11),
        (12, 15),
        (16, 19),
        (20, 23),
        (24, 27),
        (28, 30),
    ]
    changed = thirty_one[:30] + (
        replace(thirty_one[30], read_bytes=5, write_bytes=1),
    )
    probe.reset()
    dense.append_task_points(changed, identity=("task", "cost"))

    assert probe.validation_visits == 31
    assert probe.comparison_visits <= 124
    assert probe.aggregate_member_indices == [28, 29, 30]
    assert probe.unaccounted_raw_reads == []
    assert probe.bound_reads == [] and probe.bound_writes == []
    assert probe.promotion_output_bounds == []
    assert probe.promotion_ranges == []
    assert probe.scale_column_visits == 8
    assert sum(column.read_bytes for column in dense.columns()) == 35
    assert sum(column.write_bytes for column in dense.columns()) == 61
    expected_e = 8
    expected_r = 2
    expected_v = 16
    assert sum(len(group.intervals) for group in dense._groups) == expected_e
    assert len(dense.curves()) == expected_r
    assert sum(len(curve.vertices) for curve in dense.curves()) == expected_v
    assert [len(segment.polygon) for segment in dense.segments()] == [11, 11]
    assert probe.geometry_column_visits == 16
    assert probe.interval_visits == expected_e
    assert probe.interval_visits <= 16
    assert probe.vertex_emit == expected_v
    assert probe.closure_emit == 6
    assert (
        probe.geometry_column_visits
        + probe.interval_visits
        + probe.vertex_emit
        + probe.closure_emit
        <= 54
    )

    # Equal input is a no-op: validation/comparison only, no cache work.
    probe.reset()
    dense.append_task_points(changed, identity=("task", "cost"))
    assert probe.validation_visits == 31
    assert probe.comparison_visits <= 124
    assert probe.aggregate_member_indices == []
    assert probe.unaccounted_raw_reads == []
    assert probe.scale_column_visits == 0
    assert probe.bound_reads == [] and probe.promotion_ranges == []
    assert probe.geometry_column_visits == 0
    assert probe.interval_visits == 0
    assert probe.vertex_emit == 0
    assert probe.closure_emit == 0

    # Painting reads cached geometry only: no raw member aggregation.
    probe.reset()
    grab = dense.grab()
    assert grab is not None and not grab.isNull()
    assert probe.aggregate_member_indices == []
    assert probe.unaccounted_raw_reads == []
    assert probe.scale_column_visits == 0
    assert probe.geometry_column_visits == 0
    assert probe.vertex_emit == 0 and probe.closure_emit == 0

    # -- dense 32 -> 33 promotion: only index 32 is reaggregated ----------
    dense.set_task_points(dense_points(32), identity=("task", "cost"))
    probe.reset()
    dense.append_task_points(
        dense_points(32) + (task_point(32 * TEN_NS, read_bytes=1, write_bytes=2),),
        identity=("task", "cost"),
    )
    assert probe.validation_visits == 33
    assert probe.comparison_visits <= 132
    assert probe.aggregate_member_indices == [32]
    assert probe.unaccounted_raw_reads == []
    assert len(probe.bound_reads) <= 9
    assert len(probe.bound_writes) <= 5
    assert probe.promotion_output_bounds == [
        (0, 7),
        (8, 15),
        (16, 23),
        (24, 31),
        (32, 32),
    ]
    assert probe.direct_promotion_bound_reads == []
    assert probe.promotion_ranges == [(5, (0, 9, 2))]
    assert probe.merge_interval_visits <= 9
    assert probe.scale_column_visits == 5
    assert sum(column.read_bytes for column in dense.columns()) == 33
    assert sum(column.write_bytes for column in dense.columns()) == 66
    assert sum(len(group.intervals) for group in dense._groups) == 5
    assert len(dense.curves()) == 2
    assert sum(len(curve.vertices) for curve in dense.curves()) == 10
    assert [len(segment.polygon) for segment in dense.segments()] == [8, 8]
    assert probe.geometry_column_visits == 10
    assert probe.interval_visits == 5
    assert probe.interval_visits <= 10
    assert probe.vertex_emit == 10
    assert probe.closure_emit == 6
    assert (
        probe.geometry_column_visits
        + probe.interval_visits
        + probe.vertex_emit
        + probe.closure_emit
        <= 36
    )

    # -- fragmented 31 buckets: every measured run keeps its own fragment --
    fragmented = make_chart(DualSeriesChart, qtbot, width=WIDE, height=180)
    fragmented_thirty_one = tuple(
        task_point(index * 2 * TEN_NS, read_bytes=1, write_bytes=2)
        for index in range(31)
    )
    fragmented.set_task_points(fragmented_thirty_one, identity=("task", "fragmented"))
    assert fragmented._group_width == 4
    assert len(fragmented.columns()) == 8
    fragmented_changed = fragmented_thirty_one[:30] + (
        replace(fragmented_thirty_one[30], read_bytes=5, write_bytes=1),
    )
    probe.reset()
    fragmented.append_task_points(
        fragmented_changed, identity=("task", "fragmented")
    )

    assert probe.validation_visits == 31
    assert probe.comparison_visits <= 124
    assert probe.aggregate_member_indices == [28, 29, 30]
    assert probe.unaccounted_raw_reads == []
    assert probe.bound_reads == [] and probe.promotion_ranges == []
    assert probe.scale_column_visits == 8
    assert sum(column.read_bytes for column in fragmented.columns()) == 35
    assert sum(column.write_bytes for column in fragmented.columns()) == 61
    expected_e = 31
    expected_r = 62
    expected_v = 124
    assert sum(len(group.intervals) for group in fragmented._groups) == expected_e
    assert len(fragmented.curves()) == expected_r
    assert sum(len(curve.vertices) for curve in fragmented.curves()) == expected_v
    assert [len(segment.polygon) for segment in fragmented.segments()] == [5] * expected_r
    assert probe.merge_interval_visits == 0
    assert probe.geometry_column_visits == 16
    assert probe.interval_visits == expected_e
    assert probe.interval_visits <= 62
    assert probe.vertex_emit == expected_v
    assert probe.closure_emit == 3 * expected_r
    assert probe.closure_emit == 186
    assert (
        probe.geometry_column_visits
        + probe.interval_visits
        + probe.vertex_emit
        + probe.closure_emit
        <= 388
    )
    assert expected_v > 8 and expected_e > 8  # F and E are not capped at W


def test_live_rebuild_resize_and_unavailable_reset_preserve_honesty(
    qtbot, monkeypatch
) -> None:
    task, _ = chart_points(120, hole_at=60)
    identity = ("task", "a")
    chart = make_chart(DualSeriesChart, qtbot, width=600, height=200)
    chart.set_totals(4096, 2048)
    chart.set_task_points(task, identity=identity)
    wide = chart.columns()
    assert len(wide) == len(task) == 120
    assert chart._group_width == 1

    chart.resize(240, 200)
    narrow = chart.columns()
    assert chart._group_width == 2
    assert len(narrow) == 60
    assert sum(column.read_bytes for column in narrow) == sum(
        point.read_bytes for point in task
    )

    chart.resize(120, 200)
    assert width_of(chart.plot_rect()) == 0
    assert chart.columns() == ()
    assert chart.curves() == ()
    assert chart.segments() == ()
    assert len(chart._samples) == 120
    assert chart._identity == identity
    assert chart._groups == ()

    chart.resize(300, 180)
    restored = chart.columns()
    assert chart._group_width == 1
    assert len(restored) == 120
    assert chart.curves() != () and chart.segments() != ()
    assert sum(column.read_bytes for column in restored) == sum(
        point.read_bytes for point in task
    )

    chart.set_unavailable("series unavailable")
    assert chart._identity is None
    assert chart._samples == ()
    assert chart._groups == ()
    assert chart._group_width == 1
    assert chart.columns() == ()
    assert chart.curves() == ()
    assert chart.segments() == ()
    assert chart._totals is None
    assert chart.unavailable_reason() == "series unavailable"
    assert message(chart).text() == "unavailable: series unavailable"
    assert_no_curve_pixels(chart)

    fresh = chart_points(6)[0]
    chart.append_task_points(fresh, identity=("task", "b"))
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert chart._identity == ("task", "b")
    assert chart.curves() != ()
    assert sum(column.read_bytes for column in chart.columns()) == sum(
        point.read_bytes for point in fresh
    )


# --------------------------------------------------------------------------
# Composition widgets
# --------------------------------------------------------------------------


def test_donut_shares_and_device_bar_fractions_use_their_defined_denominators(qtbot) -> None:
    donut = DonutChart()
    qtbot.addWidget(donut)
    donut.resize(240, 160)
    donut.show()

    donut.set_shares(30, 10)
    assert donut.shares() == (0.75, 0.25)
    assert donut.unavailable_reason() is None
    assert message(donut).isVisible() is False

    donut.set_shares(0, 0)
    assert donut.shares() == (0.0, 0.0)
    assert message(donut).text() == NO_TASK_IO
    assert message(donut).isVisible() is True

    bars = ShareBars()
    qtbot.addWidget(bars)
    bars.resize(240, 160)
    bars.show()

    bars.set_rows((("259:0", 30), ("259:1", 15)))
    assert bars.fractions() == (("259:0", 1.0), ("259:1", 0.5))
    # Row order is preserved even when a later row is the largest.
    bars.set_rows((("8:1", 10), ("8:0", 40), ("8:2", 0)))
    assert bars.fractions() == (("8:1", 0.25), ("8:0", 1.0), ("8:2", 0.0))
    # All-zero rows are zero-width, not NaN.
    bars.set_rows((("8:0", 0), ("8:1", 0)))
    assert bars.fractions() == (("8:0", 0.0), ("8:1", 0.0))
    bars.set_rows(())
    assert bars.fractions() == ()

    donut.set_unavailable("split refused")
    assert donut.shares() is None
    assert donut.unavailable_reason() == "split refused"
    assert message(donut).text() == "unavailable: split refused"

    bars.set_rows((("8:0", 10),))
    bars.set_unavailable("split refused")
    assert bars.fractions() == ()
    assert bars.unavailable_reason() == "split refused"
    assert message(bars).text() == "unavailable: split refused"

    with pytest.raises(ValueError):
        donut.set_shares(-1, 0)
    with pytest.raises(ValueError):
        donut.set_shares(True, 0)
    with pytest.raises(ValueError):
        donut.set_shares(1.0, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        bars.set_rows((("8:0", -1),))
    with pytest.raises(ValueError):
        bars.set_rows((("", 1),))
    with pytest.raises(ValueError):
        bars.set_rows((("8:0", True),))
    with pytest.raises(ValueError):
        bars.set_reconciliation("")


@pytest.mark.parametrize("row_count", [4, 8])
@pytest.mark.parametrize("width", [300, 327])
def test_share_panel_sizes_for_every_painted_row_and_wrapped_annotation(
    qtbot, monkeypatch, row_count, width
) -> None:
    bars = ShareBars()
    qtbot.addWidget(bars)
    bars.resize(width, 160)
    rows = tuple((f"8:{index * 16}", (row_count - index) * 1024)
                 for index in range(row_count))
    bars.set_rows(rows)
    text = "Σ disks 36 KiB + residual (-4 KiB) = 32 KiB total"
    bars.set_reconciliation(text)
    bars.show()
    qtbot.wait(0)
    assert bars.sizeHint().height() > 160
    assert bars.height() >= bars.heightForWidth(bars.width())

    # Watch every row actually painted and verify its full text/bar support,
    # rather than treating fractions() or a non-null grab as visibility proof.
    baselines = {}
    original = QPainter.drawText

    def record(painter, *args):
        if len(args) == 2 and isinstance(args[0], QPointF):
            baselines[args[1]] = args[0].y()
        return original(painter, *args)

    monkeypatch.setattr(QPainter, "drawText", record)
    bars.grab()
    metrics = bars.fontMetrics()
    for label, _ in rows:
        y = baselines[label]
        assert y - metrics.ascent() >= 0
        assert y + max(metrics.descent(), 3 + theme.BAR_HEIGHT) < bars.height()

    annotation = bars.findChild(QLabel, "share_reconciliation")
    assert annotation is not None and annotation.isVisible()
    assert annotation.text() == text and annotation.wordWrap()
    assert annotation.y() > max(baselines[label] for label, _ in rows)
    assert bars.rect().contains(annotation.geometry())
    needed = annotation.fontMetrics().boundingRect(
        annotation.contentsRect(), int(Qt.TextFlag.TextWordWrap), text
    )
    assert needed.height() <= annotation.height()
    assert needed.width() <= annotation.width()


def test_empty_share_rows_still_paint_defined_reconciliation(qtbot) -> None:
    bars = ShareBars()
    qtbot.addWidget(bars)
    bars.resize(500, 150)
    bars.set_rows(())
    bars.show()
    before = bars.grab().toImage()
    text = "Σ disks 0 B + residual (6 MiB) = 6 MiB total"
    bars.set_reconciliation(text)
    after = bars.grab().toImage()
    assert bars.fractions() == ()
    assert before != after, "defined empty-device reconciliation must be painted"
    annotation = bars.findChild(QLabel, "share_reconciliation")
    assert annotation is not None and annotation.isVisible()
    assert annotation.text() == text
    assert bars.rect().contains(annotation.geometry())

    bars.set_unavailable("disk split refused")
    assert annotation.isVisible() is False
    assert annotation.text() == ""


def test_each_chart_grabs_nonempty_pixmap_and_renders_reason_text(
    qtbot, tmp_path
) -> None:
    # charts.py transcribes the integer byte formatter locally so it keeps its
    # stdlib/PyQt6/contracts/theme import surface; the outputs must agree.
    for value in (0, 1023, 1024, 1530, 2**80):
        assert charts_module._format_bytes(value) == format_bytes(value)

    task, device = chart_points(6, hole_at=3)
    widgets = [
        ("series", SeriesChart(), task),
        ("dual", DualSeriesChart(), task),
        ("lane", DeviceLane(), device),
        ("donut", DonutChart(), None),
        ("bars", ShareBars(), None),
    ]
    for name, widget, data in widgets:
        qtbot.addWidget(widget)
        widget.resize(280, 150)
        widget.show()
        if name == "lane":
            widget.set_device("8:0")
            widget.set_device_points(data)
        elif data is not None:
            if name == "dual":
                widget.set_totals(4096, 2048)
            widget.set_task_points(data)
        elif name == "donut":
            widget.set_shares(4096, 2048)
        else:
            widget.set_rows((("8:0", 4096), ("8:1", 2048)))
            widget.set_reconciliation("Σ disks 6 KiB + residual (0 B) = 6 KiB total")

        grabbed = widget.grab()
        assert isinstance(grabbed, QPixmap)
        assert not grabbed.isNull(), name
        assert grabbed.width() > 0 and grabbed.height() > 0, name
        assert not grabbed.toImage().isNull(), name
        saved = tmp_path / f"{name}.png"
        assert grabbed.save(str(saved))
        assert saved.stat().st_size > 0

        widget.set_unavailable("series unavailable")
        label = message(widget)
        assert label.text() == "unavailable: series unavailable"
        assert label.isVisible() is True
        unavailable_grab = widget.grab()
        assert not unavailable_grab.isNull()
        assert unavailable_grab.save(str(tmp_path / f"{name}-unavailable.png"))

    # A stale reconciliation annotation is dropped by the unavailable state.
    bars = widgets[4][1]
    bars.set_rows((("8:0", 4096),))
    bars.set_unavailable("split refused")
    assert bars.fractions() == ()
