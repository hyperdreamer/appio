"""Offscreen geometry and composition tests for the appio chart widgets.

Every chart exposes its drawn state as immutable data -- aggregated ``columns()``,
run-clipped ``segments()`` and composition ``shares()``/``fractions()`` -- so these
tests assert geometry and conservation without reading pixels. Pixels are only
inspected for the "no zero line" rule and for visible reason text.
"""

from __future__ import annotations

import math

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
    # The outer X support spans the whole time extent minus the run inset.
    read_xs = [x for x, _ in reads[0].polygon]
    assert min(read_xs) > left
    assert max(read_xs) < right
    assert min(read_xs) == pytest.approx(left + min(0.25, rect_width / 4), abs=1e-6)


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
    first = full[0].bucket_start_mono_ns
    last_end = full[-1].bucket_start_mono_ns + full[-1].bucket_secs * 10**9

    def bin_of(start_ns: int) -> int:
        return min(budget - 1, ((start_ns - first) * budget) // (last_end - first))

    # Remove one bucket whose neighbours share a pixel bin: the downsampled
    # column then holds contributors from both sides of the hole.
    hole_at = next(
        index
        for index in range(1, len(full) - 1)
        if bin_of(full[index - 1].bucket_start_mono_ns)
        == bin_of(full[index + 1].bucket_start_mono_ns)
        and bin_of(full[index - 1].bucket_start_mono_ns) > 0
    )
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
    first = full[0].bucket_start_mono_ns
    last_end = full[-1].bucket_start_mono_ns + full[-1].bucket_secs * 10**9

    def bin_of(start_ns: int) -> int:
        return min(budget - 1, ((start_ns - first) * budget) // (last_end - first))

    # Remove one bucket whose neighbours share a pixel bin: the downsampled
    # column then holds contributors from both sides of the hole.
    hole_at = next(
        index
        for index in range(1, len(full) - 1)
        if bin_of(full[index - 1].bucket_start_mono_ns)
        == bin_of(full[index + 1].bucket_start_mono_ns)
        and bin_of(full[index - 1].bucket_start_mono_ns) > 0
    )
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

    # Every retained bucket is identical, so every honestly downsampled column
    # has the same rate and must reach its own full half-height.
    full = [
        task_point(BASE_NS + index * BUCKET_NS, read_bytes=1024, write_bytes=512)
        for index in range(3 * budget)
    ]
    # Drop the middle bucket of the first column: its neighbours keep their
    # bytes and share one pixel column across a real one-bucket hole. That
    # column's envelope is three buckets wide, but its contributors were only
    # ever observed for two of them.
    points = tuple(full[:1] + full[2:])
    chart.set_task_points(points)

    columns = chart.columns()
    assert len(columns) == budget
    mixed = columns[0]
    assert mixed.starts_run is True
    assert mixed.end_mono_ns - mixed.start_mono_ns == 3 * BUCKET_NS
    assert (
        sum(
            1
            for point in points
            if mixed.start_mono_ns <= point.bucket_start_mono_ns < mixed.end_mono_ns
        )
        == 2
    )
    assert (mixed.read_bytes, mixed.write_bytes) == (2048, 1024)

    plot = chart.plot_rect()
    _, top, _, bottom = plot
    centre = (top + bottom) / 2.0
    tolerance = 1e-6
    segments = chart.segments()
    assert_closed_finite_polygons(segments)
    assert_inside_plot(segments, plot)

    # Averaging over the summed observed durations keeps the isolated bucket
    # before the hole at the full rate. Dividing by the envelope would leave
    # the first run - the one built only from that mixed column - two thirds
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


def test_downsampling_bins_points_by_the_full_time_extent(qtbot) -> None:
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

    # Section 6.2: bin = min(W - 1, ((start - first_start) * W) //
    # (last_end - first_start)). With 127 buckets over 40 columns the exact
    # read prefix below follows from including the final bucket's end in the
    # denominator; a start-only denominator shifts 26 of the 40 columns.
    expected_read = [
        10, 18, 27, 36, 45, 74, 66, 75, 84, 93,
        102, 150, 123, 132, 141, 150, 159, 226, 180, 189,
        198, 207, 290, 228, 237, 246, 255, 264, 366, 285,
        294, 303, 312, 321, 442, 342, 351, 360, 369, 378,
    ]
    expected_write = [3 * value for value in expected_read]

    columns = chart.columns()
    assert len(columns) == budget
    assert [column.read_bytes for column in columns] == expected_read
    assert [column.write_bytes for column in columns] == expected_write
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

    columns = chart.columns()
    # Only bins 0..3 and 36..39 hold retained buckets; the other 32 bins are
    # holes and must not become synthetic columns.
    assert len(columns) == 8
    assert len(columns) <= budget
    assert [column.read_bytes for column in columns] == [
        28,
        77,
        126,
        94,
        1265,
        1813,
        1862,
        1635,
    ]
    assert [column.write_bytes for column in columns] == [
        56,
        154,
        252,
        188,
        2530,
        3626,
        3724,
        3270,
    ]
    assert all(column.read_bytes > 0 and column.write_bytes > 0 for column in columns)
    assert sum(column.read_bytes for column in columns) == sum(
        point.read_bytes for point in points
    )
    # The unmeasured gap stays a gap between columns; nothing fills it.
    assert [column.starts_run for column in columns] == (
        [True] + [False] * 3 + [True] + [False] * 3
    )
    assert columns[4].start_mono_ns - columns[3].end_mono_ns == 225 * BUCKET_NS


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
