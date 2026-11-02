"""Offscreen contract tests for the thin P2 application window.

The window must render only what the query contracts return. Every query is
served by a fake object that has no connection, no store, and no SQL, so a
window that reached past the contract would fail at import or at call time.
"""

from __future__ import annotations

import inspect
import math

import pytest

# This host installs both PySide6 and PyQt6. pytest-qt's default guess prefers
# PySide6, which would make its QtBot reject PyQt6 widgets. The project runtime
# is PyQt6, so pin the backend before the qapp/qtbot fixtures are created.
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtWidgets import (
    QAbstractButton,
    QLabel,
    QListWidget,
    QSystemTrayIcon,
    QTabWidget,
)

from appio.contracts import (
    AppTotal,
    Coverage,
    DeviceSeriesPoint,
    DeviceShareRow,
    DiskShare,
    HookHits,
    MAX_RANGE_NS,
    Series,
    SeriesPoint,
    Window,
)
from appio.ui import formatting
from appio.ui.formatting import (
    A22_LAUNCH_REASON,
    A22_RANGE_REASON,
    format_bytes,
    format_peak,
    format_residual,
)
from appio.ui.charts import ChartColumn, ChartSegment
from appio.ui.main_window import AppIoWindow
from appio.ui.render_model import (
    DeviceLaneModel,
    Kpi,
    OverviewModel,
    RankingModel,
    RankingRow,
    build_overview,
    build_ranking,
    scope_text,
)

BOOT = "11111111-2222-4333-8444-555555555555"
END = 7_200_000_000_000

# Literal A22 message text from the signed-off specification, section 8.3.
A22_LAUNCH_TEXT = (
    "Launch window disabled: device and file activity are not stored per launch."
)
A22_RANGE_TEXT = (
    "Ranges longer than 365 days are disabled: retained rollups cannot represent "
    "arbitrary ranges."
)


def coverage(**overrides: object) -> Coverage:
    values: dict[str, object] = {
        "tier": "raw",
        "complete": True,
        "unavailable_reason": None,
        "inaccessible_pids": 0,
        "resets": 0,
        "reap_corrections": 0,
        "reap_unapplied": (0, 0, 0, 0),
        "reap_unobserved": (0, 0, 0, 0),
        "partial_intervals": 0,
        "gap_intervals": 0,
        "overlap_uncertain_intervals": 0,
        "spans_suspend_intervals": 0,
        "spans_suspend": False,
        "reap_incomplete": False,
        "counter_overflow": False,
        "device_state": "available",
        "counted_omitted_files": 0,
        "counted_omitted_bytes_r": 0,
        "counted_omitted_bytes_w": 0,
        "uncounted_overflow_files": 0,
        "uncounted_overflow_bytes_r": 0,
        "uncounted_overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
        "weak_identity_rows": 0,
        "hook_hits": HookHits(0, 0, 0, 0, 0, 0),
    }
    values.update(overrides)
    return Coverage(**values)  # type: ignore[arg-type]


def app_total(
    app_id: int,
    key: str,
    name: str,
    total_bytes: int,
    *,
    peak_bps: int | None = None,
) -> AppTotal:
    read = total_bytes // 2
    write = total_bytes - read
    return AppTotal(
        app_id,
        key,
        name,
        read,
        write,
        total_bytes,
        read,
        write,
        peak_bps,
        peak_bps,
        peak_bps,
        coverage(),
    )


def disk_share(
    app_id: int,
    *,
    window: Window | None = None,
    task_total: int = 100,
    residual_total: int | None = 0,
    cov: Coverage | None = None,
) -> DiskShare:
    task_read = task_total // 2
    task_write = task_total - task_read
    if residual_total is None:
        device_read = device_write = device_total = None
        residual_read = residual_write = None
    else:
        residual_read = residual_total // 2
        residual_write = residual_total - residual_read
        device_read = task_read - residual_read
        device_write = task_write - residual_write
        device_total = device_read + device_write
    rows: tuple[DeviceShareRow, ...] = ()
    if device_total is not None and device_total:
        rows = (DeviceShareRow(BOOT, "8:0", device_read or 0, device_write or 0, device_total),)
    return DiskShare(
        app_id,
        window or Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END),
        task_read,
        task_write,
        task_total,
        rows,
        device_read,
        device_write,
        device_total,
        residual_read,
        residual_write,
        residual_total,
        cov or coverage(),
    )


class FakeQueryService:
    """Only the three query-contract methods. No connection, store, or SQL."""

    def __init__(
        self,
        totals: list[AppTotal],
        shares: dict[int, DiskShare],
        *,
        series: dict[int, Series] | None = None,
    ) -> None:
        self._totals = totals
        self._shares = shares
        self._series = dict(series) if series is not None else {}
        self.ranking_calls: list[tuple[Window, str]] = []
        self.disk_share_calls: list[tuple[int, Window]] = []
        self.series_calls: list[tuple[int, Window]] = []
        self.ranking_error: BaseException | None = None
        self.share_error: BaseException | None = None
        self.series_error: BaseException | None = None

    def ranking(self, window: Window, metric: str) -> list[AppTotal]:
        self.ranking_calls.append((window, metric))
        if self.ranking_error is not None:
            raise self.ranking_error
        return list(self._totals)

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        self.disk_share_calls.append((app_id, window))
        if self.share_error is not None:
            raise self.share_error
        return self._shares[app_id]

    def series(self, app_id: int, window: Window) -> Series:
        self.series_calls.append((app_id, window))
        if self.series_error is not None:
            raise self.series_error
        configured = self._series.get(app_id)
        if configured is not None:
            # A configured result keeps its own window so mismatch tests stay
            # falsifiable instead of being rewritten to the requested one.
            return configured
        return self._default_series(app_id, window)

    def _default_series(self, app_id: int, window: Window) -> Series:
        """A valid empty series for the requested app/window."""

        for row in self._totals:
            if row.app_id == app_id:
                cov = row.coverage
                return Series(app_id, window, cov.tier, (), (), (), cov)
        return Series(app_id, window, "raw", (), (), (), coverage())


def build(
    qtbot,
    totals: list[AppTotal],
    shares: dict[int, DiskShare] | None = None,
    *,
    series: dict[int, Series] | None = None,
) -> tuple[AppIoWindow, FakeQueryService]:
    if shares is None:
        shares = {
            row.app_id: disk_share(row.app_id, task_total=row.total_bytes)
            for row in totals
        }
    service = FakeQueryService(totals, shares, series=series)
    window = AppIoWindow(service, BOOT, END)
    qtbot.addWidget(window)
    window.show()
    return window, service


def listing(window: AppIoWindow) -> QListWidget:
    widget = window.findChild(QListWidget, "app_list")
    assert widget is not None
    return widget


def item_texts(window: AppIoWindow) -> list[str]:
    widget = listing(window)
    return [widget.item(index).text() for index in range(widget.count())]


def label(window: AppIoWindow, name: str) -> QLabel:
    widget = window.findChild(QLabel, name)
    assert widget is not None, name
    return widget


# --------------------------------------------------------------------------
# 1. List renders query-contract totals and sorts by total descending.
# --------------------------------------------------------------------------


def test_list_renders_totals_from_query_contract_sorted_descending(qtbot) -> None:
    totals = [
        app_total(1, "unit:/alpha.service", "Alpha", 100),
        app_total(2, "unit:/beta.service", "Beta", 300),
        app_total(3, "unit:/gamma.service", "Gamma", 200),
    ]
    window, service = build(qtbot, totals)

    assert len(service.ranking_calls) == 1
    queried_window, metric = service.ranking_calls[0]
    assert queried_window == Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
    assert metric == "total_bytes"

    texts = item_texts(window)
    assert len(texts) == 3
    assert "Beta" in texts[0] and format_bytes(300) in texts[0]
    assert "Gamma" in texts[1] and format_bytes(200) in texts[1]
    assert "Alpha" in texts[2] and format_bytes(100) in texts[2]
    assert window.selected_app_id() == 2
    assert listing(window).currentRow() == 0

    # Selecting another row reads only the selected app's disk share.
    ranking_before = len(service.ranking_calls)
    listing(window).setCurrentRow(2)
    assert window.selected_app_id() == 1
    assert len(service.ranking_calls) == ranking_before
    assert [app_id for app_id, _ in service.disk_share_calls] == [2, 1]


# --------------------------------------------------------------------------
# 2. Signed residual wording.
# --------------------------------------------------------------------------


def test_negative_residual_renders_signed_wording(qtbot) -> None:
    row = app_total(1, "unit:/alpha.service", "Alpha", 100)
    share = disk_share(1, task_total=100, residual_total=-20)
    window, _ = build(qtbot, [row], {1: share})

    text = label(window, "residual_value").text()
    assert "attributed exceeds task accounting by 20 B" in text
    assert "difference between accounting points" in text


def test_format_residual_variants() -> None:
    assert format_residual(-20, True) == "attributed exceeds task accounting by 20 B"
    assert format_residual(20, True) == "+20 B"
    assert format_residual(0, True) == "0 B"
    assert format_residual(None, True) == "unavailable"
    assert format_residual(20, False) == "unavailable"
    assert format_residual(None, False) == "unavailable"


# --------------------------------------------------------------------------
# 3. Unavailable device state renders an explicit unavailable residual.
# --------------------------------------------------------------------------


def test_incomplete_device_coverage_renders_unavailable_without_zero(qtbot) -> None:
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 100)]
    unavailable = coverage(
        complete=False,
        device_state="incomplete",
        unavailable_reason="not_retained",
    )
    share = disk_share(1, task_total=100, residual_total=None, cov=unavailable)
    window, _ = build(qtbot, totals, {1: share})

    residual = label(window, "residual_value")
    assert "unavailable" in residual.text()
    assert "0" not in residual.text()
    assert label(window, "device_value").text() == "unavailable"
    # Task totals stay numeric even when device attribution is unavailable.
    assert label(window, "total_value").text() == format_bytes(100)
    assert "incomplete" in label(window, "coverage_label").text()
    assert "device attribution: incomplete" in label(window, "coverage_label").text()


# --------------------------------------------------------------------------
# 4. Peak None is unavailable; a measured zero is zero.
# --------------------------------------------------------------------------


def test_peak_none_is_unavailable_and_measured_zero_is_zero(qtbot) -> None:
    totals = [
        app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=None),
        app_total(2, "unit:/beta.service", "Beta", 100, peak_bps=0),
    ]
    shares = {
        1: disk_share(1, task_total=200),
        2: disk_share(2, task_total=100),
    }
    window, _ = build(qtbot, totals, shares)

    peak = label(window, "peak_value")
    assert peak.text() == "unavailable"

    listing(window).setCurrentRow(1)
    assert peak.text() == "0 B/s"

    assert format_peak(None) == "unavailable"
    assert format_peak(0) == "0 B/s"


def test_empty_supported_range_labels_the_active_window(qtbot) -> None:
    window, _ = build(qtbot, [])
    assert "window: since_boot" in label(window, "detail_state").text()

    window.set_window(
        Window(
            kind="range",
            boot_id=BOOT,
            start_mono_ns=0,
            end_mono_ns=MAX_RANGE_NS,
        )
    )

    detail = label(window, "detail_state").text()
    assert detail.startswith("window: range")
    assert "since boot" not in detail


# --------------------------------------------------------------------------
# 5. Disabled launch and over-365-day ranges carry the exact A22 text.
# --------------------------------------------------------------------------


def test_disabled_launch_control_uses_exact_a22_reason(qtbot) -> None:
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 100)]
    window, service = build(qtbot, totals)

    launch = window.findChild(QAbstractButton, "scope_launch")
    reason = label(window, "launch_reason")
    assert launch is not None
    assert not launch.isEnabled()
    assert not launch.isChecked()
    assert reason.text() == A22_LAUNCH_TEXT
    assert A22_LAUNCH_REASON == A22_LAUNCH_TEXT
    assert launch.accessibleDescription() == A22_LAUNCH_TEXT
    assert not reason.isHidden()

    # A persisted launch selection must render unavailable and must not query.
    ranking_before = len(service.ranking_calls)
    share_before = len(service.disk_share_calls)
    window.set_window(Window(kind="launch", boot_id=BOOT, launch_id=1))
    assert len(service.ranking_calls) == ranking_before
    assert len(service.disk_share_calls) == share_before
    assert not launch.isChecked()
    assert label(window, "detail_state").text() == A22_LAUNCH_REASON
    assert "unavailable" in label(window, "residual_value").text()
    # No stale numeric totals may survive under the disabled label.
    assert listing(window).count() == 0
    assert label(window, "total_value").text() == "unavailable"
    assert not reason.isHidden()


def test_over_365_day_range_control_uses_exact_a22_reason(qtbot) -> None:
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 100)]
    window, service = build(qtbot, totals)

    too_long = Window(
        kind="range",
        boot_id=BOOT,
        start_mono_ns=0,
        end_mono_ns=MAX_RANGE_NS + 1,
    )
    ranking_before = len(service.ranking_calls)
    share_before = len(service.disk_share_calls)

    window.set_window(too_long)

    control = window.findChild(QAbstractButton, "scope_range")
    reason = label(window, "range_reason")
    assert control is not None
    assert not control.isEnabled()
    assert not control.isChecked()
    assert reason.text() == A22_RANGE_TEXT
    assert A22_RANGE_REASON == A22_RANGE_TEXT
    assert control.accessibleDescription() == A22_RANGE_TEXT
    assert not reason.isHidden()
    assert label(window, "detail_state").text() == A22_RANGE_REASON
    # No stale numeric totals may survive under the disabled label.
    assert listing(window).count() == 0
    assert label(window, "total_value").text() == "unavailable"
    assert len(service.ranking_calls) == ranking_before
    assert len(service.disk_share_calls) == share_before


# --------------------------------------------------------------------------
# 6. Query-contract-only consumption, and no out-of-scope UI surfaces.
# --------------------------------------------------------------------------


def test_window_consumes_only_query_contracts(qtbot) -> None:
    totals = [
        app_total(1, "unit:/alpha.service", "Alpha", 100),
        app_total(2, "unit:/beta.service", "Beta", 300),
    ]
    window, service = build(qtbot, totals)

    # The fake offers nothing but ranking() and disk_share().
    assert not hasattr(service, "connection")
    assert not hasattr(service, "store")

    listing(window).setCurrentRow(1)
    assert len(service.ranking_calls) == 1
    assert [app_id for app_id, _ in service.disk_share_calls] == [2, 1]
    for _, window_arg in service.disk_share_calls:
        assert window_arg == Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)

    for module in (inspect.getmodule(AppIoWindow), formatting):
        assert module is not None
        source = inspect.getsource(module)
        for forbidden in (
            "sqlite3",
            "appio.query",
            "appio.store",
            "/proc",
            "SELECT ",
        ):
            assert forbidden not in source, (module.__name__, forbidden)


def test_no_files_tab_tray_daemon_or_privileged_action(qtbot) -> None:
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 100)]
    window, _ = build(qtbot, totals)

    assert window.findChildren(QTabWidget) == []
    assert window.findChildren(QSystemTrayIcon) == []

    verbs = ("files", "tray", "daemon", "setup", "polkit", "pkexec", "privilege")
    for button in window.findChildren(QAbstractButton):
        text = button.text().casefold()
        for verb in verbs:
            assert verb not in text, (button.objectName(), text)


def test_format_bytes_is_deterministic_and_signed() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(20) == "20 B"
    assert format_bytes(1024) == "1 KiB"
    assert format_bytes(1536) == "1.5 KiB"
    assert format_bytes(-20) == "-20 B"
    with pytest.raises(ValueError):
        format_bytes(True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 7. Pure render models (design 7.1, spec §9.2).
# --------------------------------------------------------------------------

SINCE_BOOT = Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
SERIES_REASON = "series unavailable"
SHARE_REASON = "task/device split unavailable"
FILE_ONLY_REASON = "not in the process ranking"
LIFETIME_REASON = "lifetime chronology unavailable"


def series_points(
    start: int = 1_000_000_000,
    count: int = 4,
    *,
    bucket_secs: int = 10,
    dev: str = "8:0",
    suspend_at: int | None = None,
) -> tuple[tuple[SeriesPoint, ...], tuple[DeviceSeriesPoint, ...]]:
    """Ordered task buckets with matching device buckets and no suspend field there."""

    task: list[SeriesPoint] = []
    devices: list[DeviceSeriesPoint] = []
    for index in range(count):
        bucket_start = start + index * bucket_secs * 10**9
        read = 512 * (index + 1)
        task.append(
            SeriesPoint(
                bucket_start,
                bucket_secs,
                None,
                read,
                read // 2,
                read * 2,
                None,
                None,
                index == suspend_at,
            )
        )
        devices.append(DeviceSeriesPoint(bucket_start, bucket_secs, dev, read // 2, read // 4))
    return tuple(task), tuple(devices)


def test_build_ranking_orders_by_total_then_key_and_normalises_share() -> None:
    rows = [
        app_total(2, "unit:/beta.service", "Beta", 50),
        app_total(1, "unit:/alpha.service", "Alpha", 100),
        app_total(3, "unit:/zero.service", "Zero", 0),
    ]
    model = build_ranking(rows)

    assert [(row.app_id, row.app_key) for row in model.rows] == [
        (1, "unit:/alpha.service"),
        (2, "unit:/beta.service"),
        (3, "unit:/zero.service"),
    ]
    assert [row.share for row in model.rows] == [1.0, 0.5, 0.0]
    assert [row.total_bytes for row in model.rows] == [100, 50, 0]
    assert model.rows[0].label == f"Alpha  {format_bytes(100)}"
    assert model.rows[2].label == f"Zero  {format_bytes(0)}"
    assert model.empty_text is None
    assert model.unavailable is None
    # The caller's sequence is not reordered in place.
    assert [row.app_id for row in rows] == [2, 1, 3]

    # Equal totals fall back to the UTF-8 key bytes.
    tied = build_ranking(
        [
            app_total(2, "unit:/b.service", "B", 10),
            app_total(1, "unit:/a.service", "A", 10),
        ]
    )
    assert [row.app_key for row in tied.rows] == ["unit:/a.service", "unit:/b.service"]
    assert [row.share for row in tied.rows] == [1.0, 1.0]

    # An all-zero denominator yields finite 0.0 shares, never NaN.
    zeros = build_ranking(
        [
            app_total(5, "unit:/x.service", "X", 0),
            app_total(4, "unit:/y.service", "Y", 0),
        ]
    )
    assert [row.share for row in zeros.rows] == [0.0, 0.0]
    assert all(math.isfinite(row.share) for row in zeros.rows)


def test_build_ranking_distinguishes_empty_and_unavailable() -> None:
    empty = build_ranking([])
    assert empty.rows == ()
    assert empty.empty_text == "no applications observed"
    assert empty.unavailable is None

    failed = build_ranking([], unavailable="window is unavailable")
    assert failed.rows == ()
    assert failed.empty_text is None
    assert failed.unavailable == "window is unavailable"

    # A failed ranking with rows would disguise an error as partial success.
    with pytest.raises(ValueError):
        build_ranking([app_total(1, "unit:/alpha.service", "Alpha", 100)], unavailable="boom")
    with pytest.raises(ValueError):
        RankingModel((), "no applications observed", "boom")
    with pytest.raises(ValueError):
        RankingModel((), None, None)
    with pytest.raises(ValueError):
        RankingModel((), "something else", None)
    with pytest.raises(ValueError):
        build_ranking([], unavailable="")


def test_build_overview_has_five_ordered_kpis_and_none_peak_is_unavailable() -> None:
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    share = disk_share(1, window=SINCE_BOOT, task_total=200)

    model = build_overview(
        1,
        SINCE_BOOT,
        app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=None),
        series,
        share,
    )
    assert [kpi.caption for kpi in model.kpis] == ["TOTAL", "READ", "WRITE", "PEAK", "DEVICE"]
    assert [kpi.text for kpi in model.kpis] == [
        format_bytes(200),
        format_bytes(100),
        format_bytes(100),
        "unavailable",
        format_bytes(share.device_total_bytes),
    ]
    assert model.app_id == 1
    assert model.share is share
    assert model.share_unavailable is None
    assert model.series_unavailable is None
    assert model.task_points == task
    assert model.coverage_line.startswith(
        "coverage: complete; device attribution: available"
    )
    assert "device attribution: available" in model.coverage_line
    assert "gaps: 0" in model.coverage_line
    assert "suspends: False" in model.coverage_line
    assert model.detail_line.startswith("window: since_boot; tier: raw")
    assert scope_text(SINCE_BOOT) == "since_boot"

    # A measured zero peak is a value, not unavailable.
    measured_zero = build_overview(
        1,
        SINCE_BOOT,
        app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=0),
        series,
        share,
    )
    assert measured_zero.kpis[3].text == "0 B/s"
    assert measured_zero.kpis[3].text != "unavailable"

    # Differing successful source coverage is described, never merged.
    differing = build_overview(
        1,
        SINCE_BOOT,
        app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=0),
        Series(1, SINCE_BOOT, "raw", task, devices, (), coverage(tier="rollup_60s")),
        share,
    )
    assert "series coverage: coverage: complete" in differing.detail_line
    assert differing.coverage_line.startswith("coverage: complete;")


def test_build_overview_limits_device_lanes_and_counts_more() -> None:
    specs = [("8:4", 400), ("8:1", 1000), ("8:0", 1500), ("8:2", 700), ("8:9", 0)]
    rows = tuple(
        DeviceShareRow(BOOT, dev, total // 2, total - total // 2, total)
        for dev, total in specs
    )
    device_read = sum(row.rbytes for row in rows)
    device_write = sum(row.wbytes for row in rows)
    device_total = device_read + device_write
    task_total = device_total + 100
    share = DiskShare(
        1,
        SINCE_BOOT,
        task_total // 2,
        task_total - task_total // 2,
        task_total,
        rows,
        device_read,
        device_write,
        device_total,
        50,
        50,
        100,
        coverage(),
    )
    device_points = (
        DeviceSeriesPoint(1_000_000_000, 10, "8:0", 750, 750),
        DeviceSeriesPoint(1_000_000_000, 10, "8:1", 500, 500),
    )
    series = Series(1, SINCE_BOOT, "raw", (), device_points, (), coverage())

    model = build_overview(
        1,
        SINCE_BOOT,
        app_total(1, "unit:/alpha.service", "Alpha", task_total),
        series,
        share,
    )
    assert [lane.dev for lane in model.device_lanes] == ["8:0", "8:1", "8:2"]
    assert [lane.read_bytes + lane.write_bytes for lane in model.device_lanes] == [
        1500,
        1000,
        700,
    ]
    assert model.more_devices == 1
    assert model.device_lanes[0].points == (device_points[0],)
    assert model.device_lanes[1].points == (device_points[1],)
    # A selected device without retained points keeps a caption and no shape.
    assert model.device_lanes[2].points == ()
    assert all(lane.dev != "8:9" for lane in model.device_lanes)


def test_build_overview_reconciles_only_available_device_attribution() -> None:
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    total = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)

    positive = disk_share(1, window=SINCE_BOOT, task_total=200, residual_total=20)
    model = build_overview(1, SINCE_BOOT, total, series, positive)
    assert model.reconciliation == (
        "Residual: +20 B - difference between accounting points; "
        f"{format_bytes(positive.device_total_bytes)} attributed + (20 B) residual = "
        f"{format_bytes(200)} total"
    )
    assert model.kpis[4].text == format_bytes(positive.device_total_bytes)

    negative = disk_share(1, window=SINCE_BOOT, task_total=200, residual_total=-20)
    model = build_overview(1, SINCE_BOOT, total, series, negative)
    assert model.reconciliation.startswith(
        "Residual: attributed exceeds task accounting by 20 B - difference between accounting points; "
    )
    assert "(-20 B) residual = " in model.reconciliation

    measured_zero = disk_share(1, window=SINCE_BOOT, task_total=200, residual_total=0)
    model = build_overview(1, SINCE_BOOT, total, series, measured_zero)
    assert model.reconciliation.startswith(
        "Residual: 0 B - difference between accounting points; "
    )
    assert "(0 B) residual = " in model.reconciliation

    # An undefined residual alone suppresses reconciliation, not device totals.
    undefined_residual = DiskShare(
        1,
        SINCE_BOOT,
        100,
        100,
        200,
        (DeviceShareRow(BOOT, "8:0", 90, 90, 180),),
        90,
        90,
        180,
        None,
        None,
        None,
        coverage(),
    )
    model = build_overview(1, SINCE_BOOT, total, series, undefined_residual)
    assert model.reconciliation is None
    assert model.kpis[4].text == format_bytes(180)
    assert [lane.dev for lane in model.device_lanes] == ["8:0"]

    # Unavailable device attribution: DEVICE is unavailable, never zero.
    unavailable = disk_share(
        1,
        window=SINCE_BOOT,
        task_total=200,
        residual_total=None,
        cov=coverage(complete=False, device_state="incomplete"),
    )
    model = build_overview(1, SINCE_BOOT, total, series, unavailable)
    assert model.kpis[4].text == "unavailable"
    assert model.reconciliation is None
    assert model.device_lanes == ()
    assert model.more_devices == 0


def test_lifetime_series_does_not_join_unrelated_boot_clocks() -> None:
    lifetime = Window(kind="lifetime")
    first_start = 1_000_000_000
    second_start = 20_000_000_000  # an unrelated boot's monotonic clock
    points = (
        SeriesPoint(first_start, 10, None, 512, 256, 1024, None, None, False),
        SeriesPoint(first_start + 10 * 10**9, 10, None, 512, 256, 1024, None, None, False),
        SeriesPoint(second_start, 10, None, 1024, 512, 2048, None, None, False),
    )
    devices = (
        DeviceSeriesPoint(first_start, 10, "8:0", 256, 128),
        DeviceSeriesPoint(second_start, 10, "8:0", 512, 256),
    )
    series = Series(1, lifetime, "rollup_3600s", points, devices, (), coverage())
    rows = (
        DeviceShareRow(BOOT, "8:0", 300, 150, 450),
        DeviceShareRow("99999999-8888-4777-8666-555555555555", "8:0", 700, 350, 1050),
    )
    task_total = 1600
    device_total = 1500
    share = DiskShare(
        1,
        lifetime,
        800,
        800,
        task_total,
        rows,
        1000,
        500,
        device_total,
        100,
        0,
        100,
        coverage(),
    )

    model = build_overview(
        1,
        lifetime,
        app_total(1, "unit:/alpha.service", "Alpha", task_total),
        series,
        share,
    )
    assert model.series_unavailable == LIFETIME_REASON
    assert model.task_points == ()
    assert model.device_lanes == ()
    assert model.more_devices == 0
    # Measured totals, the boot-qualified share rows and coverage survive.
    assert model.kpis[0].text == format_bytes(task_total)
    assert model.kpis[4].text == format_bytes(device_total)
    assert model.share is share
    assert model.share_unavailable is None
    assert model.coverage_line.startswith("coverage: ")
    assert LIFETIME_REASON in model.detail_line


def undefined_residual_share() -> DiskShare:
    """Defined device totals with an undefined residual."""

    return DiskShare(
        1,
        SINCE_BOOT,
        100,
        100,
        200,
        (DeviceShareRow(BOOT, "8:0", 90, 90, 180),),
        90,
        90,
        180,
        None,
        None,
        None,
        coverage(),
    )


def test_render_dataclasses_reject_invalid_values_and_availability_conflicts() -> None:
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    share = disk_share(1, window=SINCE_BOOT, task_total=200)
    total = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)

    # -- RankingRow -------------------------------------------------------
    row = RankingRow(1, "unit:/alpha.service", f"Alpha  {format_bytes(100)}", 100, 1.0)
    assert row.share == 1.0
    for kwargs in (
        {"app_id": 0},
        {"app_id": True},
        {"app_key": ""},
        {"label": ""},
        {"total_bytes": -1},
        {"total_bytes": True},
        {"share": float("nan")},
        {"share": float("inf")},
        {"share": -0.1},
        {"share": 1.1},
        {"share": True},
        {"share": "1.0"},
    ):
        values = {
            "app_id": 1,
            "app_key": "unit:/alpha.service",
            "label": "Alpha",
            "total_bytes": 100,
            "share": 1.0,
        }
        values.update(kwargs)
        with pytest.raises(ValueError):
            RankingRow(**values)  # type: ignore[arg-type]

    # -- RankingModel -----------------------------------------------------
    assert RankingModel((row,), None, None).rows == (row,)
    with pytest.raises(ValueError):
        RankingModel([row], None, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RankingModel((row,), None, "boom")
    with pytest.raises(ValueError):
        RankingModel(
            (row, RankingRow(1, "unit:/other.service", "Other", 100, 1.0)), None, None
        )
    with pytest.raises(ValueError):
        RankingModel(
            (row, RankingRow(2, "unit:/alpha.service", "Other", 100, 1.0)), None, None
        )
    with pytest.raises(ValueError):
        RankingModel(
            (
                RankingRow(2, "unit:/b.service", "B", 50, 0.5),
                RankingRow(1, "unit:/a.service", "A", 100, 1.0),
            ),
            None,
            None,
        )
    with pytest.raises(ValueError):
        RankingModel((RankingRow(1, "unit:/a.service", "A", 100, 0.5),), None, None)
    assert RankingModel((), "no applications observed", None).empty_text == (
        "no applications observed"
    )

    # -- Kpi --------------------------------------------------------------
    assert Kpi("TOTAL", "1 KiB").caption == "TOTAL"
    for caption, text in (
        ("TOTALITY", "1 KiB"),
        ("total", "1 KiB"),
        ("TOTAL", ""),
        ("TOTAL", 3),
    ):
        with pytest.raises(ValueError):
            Kpi(caption, text)  # type: ignore[arg-type]

    # -- DeviceLaneModel --------------------------------------------------
    points = (
        DeviceSeriesPoint(0, 10, "8:0", 10, 5),
        DeviceSeriesPoint(10 * 10**9, 10, "8:0", 10, 5),
    )
    lane = DeviceLaneModel("8:0", points, 20, 10)
    assert lane.dev == "8:0"
    for args in (
        ("", points, 20, 10),
        ("8:0", points, -1, 10),
        ("8:0", points, 20, True),
        ("8:0", list(points), 20, 10),
        ("8:0", (DeviceSeriesPoint(0, 10, "8:1", 1, 1),), 1, 1),
        (
            "8:0",
            (
                DeviceSeriesPoint(10 * 10**9, 10, "8:0", 1, 1),
                DeviceSeriesPoint(0, 10, "8:0", 1, 1),
            ),
            2,
            2,
        ),
        (
            "8:0",
            (
                DeviceSeriesPoint(0, 20, "8:0", 1, 1),
                DeviceSeriesPoint(10 * 10**9, 10, "8:0", 1, 1),
            ),
            2,
            2,
        ),
        ("8:0", (SeriesPoint(0, 10, None, 1, 1, None, None, None, False),), 2, 2),
    ):
        with pytest.raises(ValueError):
            DeviceLaneModel(*args)  # type: ignore[arg-type]
    # A selected device with no retained samples is a valid, empty lane.
    assert DeviceLaneModel("8:0", (), 20, 10).points == ()

    # -- OverviewModel ----------------------------------------------------
    model = build_overview(1, SINCE_BOOT, total, series, share)
    assert model.kpis[0].caption == "TOTAL"

    def perturbed(**overrides: object) -> OverviewModel:
        values: dict[str, object] = {
            "app_id": model.app_id,
            "kpis": model.kpis,
            "task_points": model.task_points,
            "device_lanes": model.device_lanes,
            "more_devices": model.more_devices,
            "series_unavailable": model.series_unavailable,
            "share": model.share,
            "share_unavailable": model.share_unavailable,
            "reconciliation": model.reconciliation,
            "coverage_line": model.coverage_line,
            "detail_line": model.detail_line,
        }
        values.update(overrides)
        return OverviewModel(**values)  # type: ignore[arg-type]

    assert perturbed().kpis == model.kpis
    assert model.device_lanes  # the valid control has a lane
    unavailable_share = disk_share(
        1,
        window=SINCE_BOOT,
        task_total=200,
        residual_total=None,
        cov=coverage(complete=False, device_state="incomplete"),
    )
    for overrides in (
        {"app_id": 0},
        {"app_id": True},
        {"kpis": (model.kpis[0],)},
        {"kpis": tuple(reversed(model.kpis))},
        {"kpis": model.kpis + (Kpi("DEVICE", "1 B"),)},
        {"kpis": list(model.kpis)},
        {"task_points": list(model.task_points)},
        {"task_points": (task[1], task[0])},
        {"device_lanes": model.device_lanes + model.device_lanes},
        {"device_lanes": tuple(reversed(model.device_lanes)) * 2},
        {"more_devices": -1},
        {"more_devices": 1},
        {"series_unavailable": SERIES_REASON},
        {"share_unavailable": SHARE_REASON},
        {"coverage_line": ""},
        {"detail_line": ""},
        {"device_lanes": (DeviceLaneModel("8:0", (), 1, 1),), "share": None},
    ):
        with pytest.raises(ValueError):
            perturbed(**overrides)

    # Device lanes need a present share with available attribution.
    with pytest.raises(ValueError):
        perturbed(share=unavailable_share, share_unavailable=None)
    # Reconciliation needs a defined residual as well as a device total.
    with pytest.raises(ValueError):
        perturbed(share=undefined_residual_share(), reconciliation="Residual: 0 B")

    # A file-only subject keeps five unavailable KPIs and that reason.
    file_only = build_overview(1, SINCE_BOOT, None, None, None)
    assert [kpi.text for kpi in file_only.kpis] == ["unavailable"] * 5
    assert file_only.series_unavailable == FILE_ONLY_REASON
    assert file_only.detail_line == FILE_ONLY_REASON
    assert file_only.share is None
    assert file_only.reconciliation is None
    assert file_only.device_lanes == ()

    # Ranking errors take precedence over the file-only reason.
    failed = build_overview(
        1, SINCE_BOOT, None, None, None, ranking_error="window is unavailable"
    )
    assert failed.series_unavailable == "window is unavailable"
    assert failed.share_unavailable == "window is unavailable"
    assert failed.detail_line == "window is unavailable"
    assert [kpi.text for kpi in failed.kpis] == ["unavailable"] * 5

    # Defaults for missing independent inputs, and object-plus-error conflicts.
    partial = build_overview(1, SINCE_BOOT, total, None, None)
    assert partial.series_unavailable == SERIES_REASON
    assert partial.share_unavailable == SHARE_REASON
    assert partial.kpis[3].text != "unavailable"  # the ranking peak survives
    assert "; series: series unavailable" in partial.detail_line
    assert "; share: task/device split unavailable" in partial.detail_line
    for kwargs in (
        {"series": series, "series_error": SERIES_REASON},
        {"share": share, "share_error": SHARE_REASON},
        {"total": total, "ranking_error": "boom"},
    ):
        with pytest.raises(ValueError):
            build_overview(
                1,
                SINCE_BOOT,
                kwargs.get("total"),  # type: ignore[arg-type]
                kwargs.get("series"),  # type: ignore[arg-type]
                kwargs.get("share"),  # type: ignore[arg-type]
                ranking_error=kwargs.get("ranking_error"),  # type: ignore[arg-type]
                series_error=kwargs.get("series_error"),  # type: ignore[arg-type]
                share_error=kwargs.get("share_error"),  # type: ignore[arg-type]
            )
    # Failed inputs stay independent: a share error keeps the series lane.
    share_failed = build_overview(
        1, SINCE_BOOT, total, series, None, share_error="disk split refused"
    )
    assert share_failed.series_unavailable is None
    assert share_failed.task_points == task
    assert share_failed.share_unavailable == "disk split refused"
    assert share_failed.kpis[3].text == format_peak(100)
    assert share_failed.kpis[0].text == "unavailable"
    # A series error keeps the measured byte KPIs.
    series_failed = build_overview(
        1, SINCE_BOOT, total, None, share, series_error="series refused"
    )
    assert series_failed.series_unavailable == "series refused"
    assert series_failed.kpis[0].text == format_bytes(200)
    assert series_failed.device_lanes == ()
    assert series_failed.more_devices == 0
    # An empty successful series is available with no shape.
    empty_series = Series(1, SINCE_BOOT, "raw", (), (), (), coverage())
    empty = build_overview(1, SINCE_BOOT, total, empty_series, share)
    assert empty.series_unavailable is None
    assert empty.task_points == ()
    # Wrong subjects and windows are rejected rather than silently rewritten.
    with pytest.raises(ValueError):
        build_overview(2, SINCE_BOOT, total, series, share)
    with pytest.raises(ValueError):
        build_overview(
            1, Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END + 1), total, series, share
        )
    with pytest.raises(ValueError):
        build_overview(1, SINCE_BOOT, total, series, disk_share(2, window=SINCE_BOOT))
    with pytest.raises(ValueError):
        scope_text("since_boot")  # type: ignore[arg-type]

    # -- ChartColumn ------------------------------------------------------
    column = ChartColumn(0, 10, 4, 2, None, None, True)
    assert column.starts_run is True
    for values in (
        (-1, 10, 0, 0, None, None, True),
        (10, 10, 0, 0, None, None, True),
        (0, 10, -1, 0, None, None, True),
        (0, 10, 0, True, None, None, True),
        (0, 10, 0, 0, -1, None, True),
        (0, 10, 0, 0, None, 1.5, True),
        (0, 10, 0, 0, None, None, 1),
        (0, 10, 0, 0, None, None, "yes"),
    ):
        with pytest.raises(ValueError):
            ChartColumn(*values)  # type: ignore[arg-type]

    # -- ChartSegment -----------------------------------------------------
    square = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0))
    assert ChartSegment("read", 0, square).polygon[-1] == square[0]
    for side, run_index, polygon in (
        ("total", 0, square),
        ("read", -1, square),
        ("read", True, square),
        ("read", 0, square[:-1]),
        ("read", 0, ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0))),
        ("read", 0, ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.5, 0.5))),
        ("read", 0, ((0.0, float("nan")), (0.0, 1.0), (1.0, 1.0), (0.0, 0.0))),
        ("read", 0, ((0.0, 0.0), (0.0, float("inf")), (1.0, 1.0), (0.0, 0.0))),
    ):
        with pytest.raises(ValueError):
            ChartSegment(side, run_index, polygon)  # type: ignore[arg-type]
