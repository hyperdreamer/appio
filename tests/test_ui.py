"""Offscreen contract tests for the thin P2 application window.

The window must render only what the query contracts return. Every query is
served by a fake object that has no connection, no store, and no SQL, so a
window that reached past the contract would fail at import or at call time.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
import inspect
import math
import threading
from types import SimpleNamespace

import pytest

# This host installs both PySide6 and PyQt6. pytest-qt's default guess prefers
# PySide6, which would make its QtBot reject PyQt6 widgets. The project runtime
# is PyQt6, so pin the backend before the qapp/qtbot fixtures are created.
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtCore import QObject, QRect, Qt, pyqtSlot
from PyQt6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QLabel,
    QListWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSystemTrayIcon,
    QTabWidget,
)

from appio.contracts import (
    AppTotal,
    Coverage,
    DeviceSeriesPoint,
    DeviceShareRow,
    DiskShare,
    HistoryPending,
    HookHits,
    MAX_RANGE_NS,
    Series,
    SeriesPoint,
    Window,
    WindowUnavailableError,
)
from appio.history_types import (
    AcceptedIdentity,
    AnswerProvenance,
    RetainedRecord,
    SeriesMetadata,
    retention_scope,
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
from appio.ui.collector_window import CollectorWindow
from appio.ui.files_view import FilesView
from appio.ui.main_window import AppIoWindow, RankingDelegate, _device_unavailable_text
from appio.ui.render_model import (
    MAX_DEVICE_LANES,
    DeviceLaneModel,
    Kpi,
    MeasurementStatus,
    OverviewModel,
    OverviewStatusChoice,
    PresentationRefusal,
    RankingModel,
    RankingRow,
    SHARE_UNAVAILABLE_REASON,
    _choose_overview_status,
    _coverage_text,
    build_overview,
    build_ranking,
    oldest_answer_age,
    scope_text,
)
from appio.ui.theme import BAR_HEIGHT, READ_COLOR, TRACK_COLOR

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

    def set_totals(self, totals: list[AppTotal]) -> None:
        """Replace the ranking the next refresh will read."""

        self._totals = list(totals)

    def set_series(self, app_id: int, series: Series) -> None:
        """Configure one subject's exact series result."""

        self._series[app_id] = series

    def set_share(self, app_id: int, share: DiskShare) -> None:
        """Replace one subject's disk-share result."""

        self._shares[app_id] = share

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
    files_view: FilesView | None = None,
) -> tuple[AppIoWindow, FakeQueryService]:
    if shares is None:
        shares = {
            row.app_id: disk_share(row.app_id, task_total=row.total_bytes)
            for row in totals
        }
    service = FakeQueryService(totals, shares, series=series)
    window = AppIoWindow(service, BOOT, END, files_view=files_view)
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
    control = window.findChild(QAbstractButton, "scope_range")
    assert control is not None and control.isHidden()
    assert not control.isEnabled() and not control.isChecked()
    requested = Window("range", BOOT, 0, MAX_RANGE_NS)
    window.set_window(requested)
    detail = label(window, "detail_state").text()
    assert detail.startswith("window: range")
    assert "since boot" not in detail
    assert window._active_window is requested
    assert window.findChild(QAbstractButton, "scope_range") is control
    assert control.isVisible() and control.isEnabled() and control.isChecked()
    assert label(window, "range_reason").text() == ""
    assert label(window, "range_reason").isHidden()
    assert control.toolTip() == control.accessibleDescription() == ""


# --------------------------------------------------------------------------
# 5. Launch is not offered; disabled ranges keep the exact A22 text.
# --------------------------------------------------------------------------


def test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text(qtbot) -> None:
    total = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    window, service = build(qtbot, [total], series={1: series})
    assert listing(window).count() == 1
    assert window._total_chart.segments()
    assert window._donut.shares() is not None
    assert window._share_bars.fractions()
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert A22_LAUNCH_REASON == A22_LAUNCH_TEXT
    before = (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    requested = Window("launch", BOOT, launch_id=1)
    window.set_window(requested)
    assert window._active_window is requested
    assert before == (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert label(window, "range_reason").text() == ""
    assert label(window, "range_reason").isHidden()


def test_over_365_day_range_shows_the_reason_and_hides_the_control(qtbot) -> None:
    total = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    window, service = build(qtbot, [total], series={1: series})
    assert listing(window).count() == 1
    assert window._total_chart.segments()
    assert window._donut.shares() is not None
    assert window._share_bars.fractions()
    before = (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    requested = Window("range", BOOT, 0, MAX_RANGE_NS + 1)
    window.set_window(requested)
    control = window.findChild(QAbstractButton, "scope_range")
    assert control is not None
    assert control.isHidden() and not control.isVisible()
    assert not control.isEnabled() and not control.isChecked()
    assert A22_RANGE_REASON == A22_RANGE_TEXT
    assert control.toolTip() == A22_RANGE_TEXT
    assert control.accessibleDescription() == A22_RANGE_TEXT
    assert label(window, "range_reason").text() == A22_RANGE_TEXT
    assert label(window, "range_reason").isVisible()
    assert window._active_window is requested
    assert before == (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    _assert_refused_overview(window, A22_RANGE_TEXT)


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
            "appio.history_router",
            "appio.iodclient",
            "/proc",
            "SELECT ",
        ):
            assert forbidden not in source, (module.__name__, forbidden)


def test_no_files_tab_tray_daemon_or_privileged_action(qtbot) -> None:
    """Exactly the approved two-tab host, one Files surface, no stray controls."""

    totals = [app_total(1, "unit:/alpha.service", "Alpha", 100)]
    window, _ = build(qtbot, totals)

    tabs = window.detail_tabs
    assert window.findChildren(QTabWidget) == [tabs]
    assert tabs.objectName() == "detail_tabs"
    assert tabs.count() == 2
    assert [tabs.tabText(index) for index in range(tabs.count())] == [
        "Overview",
        "Files",
    ]

    # Exactly one Files surface, inside the shared console and not Overview.
    files_surfaces = window.findChildren(FilesView)
    assert len(files_surfaces) == 1
    assert files_surfaces[0] is window.files_view
    files_page = tabs.widget(1)
    assert files_page is not None
    assert files_page is files_surfaces[0] or files_page.isAncestorOf(files_surfaces[0])
    assert not tabs.widget(0).isAncestorOf(files_surfaces[0])
    assert window.files_message.objectName() == "files_message"
    assert window.files_open_settings.objectName() == "files_open_settings"

    assert window.findChildren(QSystemTrayIcon) == []

    verbs = ("files", "tray", "daemon", "setup", "polkit", "pkexec", "privilege")
    for button in window.findChildren(QAbstractButton):
        text = button.text().casefold()
        for verb in verbs:
            assert verb not in text, (button.objectName(), text)

    assert [action.text() for action in window.actions()] == []


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
        Series(1, SINCE_BOOT, "raw", task, devices, (), coverage(gap_intervals=1)),
        share,
    )
    assert "series coverage: coverage: complete" in differing.detail_line
    assert differing.coverage_line.startswith("coverage: complete;")


def test_coverage_diagnostic_composes_with_contract_reason(qtbot) -> None:
    base = (
        "coverage: complete; device attribution: available; tier: raw; "
        "gaps: 0; suspends: False"
    )
    diagnostic = "daemon reported an error (bad_query)"
    data_coverage = coverage(
        complete=False,
        device_state="incomplete",
        gap_intervals=3,
        unavailable_reason="not_retained",
    )
    data_prefix = (
        "coverage: incomplete; device attribution: incomplete; tier: raw; "
        "gaps: 3; suspends: False"
    )
    assert _coverage_text(coverage()) == base
    assert _coverage_text(data_coverage) == f"{data_prefix}; reason: not_retained"
    assert _coverage_text(coverage(), diagnostic) == f"{base}; reason: {diagnostic}"
    assert _coverage_text(data_coverage, diagnostic) == (
        f"{data_prefix}; reason: not_retained; {diagnostic}"
    )

    total = replace(
        app_total(1, "unit:/alpha.service", "Alpha", 200),
        coverage=data_coverage,
    )
    series = Series(1, SINCE_BOOT, "raw", (), (), (), data_coverage)
    normal = build_overview(
        1, SINCE_BOOT, total, series, None, coverage_diagnostic=diagnostic
    )
    assert normal.coverage_diagnostic == diagnostic
    assert normal.coverage_line == f"{data_prefix}; reason: not_retained; {diagnostic}"
    assert build_overview(1, SINCE_BOOT, total, series, None).coverage_diagnostic is None

    file_only = build_overview(
        1, SINCE_BOOT, None, None, None, coverage_diagnostic=diagnostic
    )
    assert file_only.coverage_line == f"coverage: unavailable; reason: {diagnostic}"
    refused = build_overview(
        1,
        SINCE_BOOT,
        None,
        None,
        None,
        ranking_error="window unavailable",
        coverage_diagnostic=diagnostic,
    )
    assert refused.coverage_line == f"coverage: unavailable; reason: {diagnostic}"

    whitespace = build_overview(
        1, SINCE_BOOT, None, None, None, coverage_diagnostic="  "
    )
    assert whitespace.coverage_diagnostic == "  "
    for invalid in ("", 17, "\ud800"):
        with pytest.raises(ValueError):
            build_overview(
                1, SINCE_BOOT, None, None, None, coverage_diagnostic=invalid
            )
        with pytest.raises(ValueError):
            replace(file_only, coverage_diagnostic=invalid)

    alpha = app_total(1, "unit:/alpha.service", "Alpha", 200)
    file_key = "unit:retained-only.service"
    files = FakeFilesHistory(
        ranking_rows=(alpha,),
        local_applications=((file_key, "Retained only", 40),),
        registry_ids={file_key: 99},
    )
    window, service = build(qtbot, [alpha], files_view=FilesView(files))
    assert window._overview_model.coverage_diagnostic is None
    service.daemon_answer_error = SimpleNamespace(kind="remote_error", reason="bad_query")

    window.refresh()
    assert window._overview_model.coverage_diagnostic == diagnostic
    assert label(window, "coverage_label").text().endswith(f"; reason: {diagnostic}")

    selector = window.files_view.app_selector
    selector.setCurrentIndex(selector.findData(99))
    file_model = window._overview_model
    assert file_model.coverage_diagnostic == diagnostic
    assert file_model.coverage_line == f"coverage: unavailable; reason: {diagnostic}"

    selector.setCurrentIndex(selector.findData(1))
    mismatched = Series(
        1,
        Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END + 1),
        "raw",
        (),
        (),
        (),
        coverage(),
    )
    service.set_series(1, mismatched)
    window.refresh()
    refusal_model = window._overview_model
    assert refusal_model.coverage_diagnostic == diagnostic
    assert label(window, "coverage_label").text().endswith(f"; reason: {diagnostic}")


def test_each_diagnostic_kind_matches_coverage_and_device_wording(qtbot) -> None:
    cases = (
        ("remote_error", "bad_query", "daemon reported an error (bad_query)"),
        ("unavailable", "no_daemon", "no answering daemon (no_daemon)"),
        ("rejected", "too_many_requests", "history query failed (too_many_requests)"),
        ("transport", "connection_lost", "connection lost before the answer (connection_lost)"),
        ("internal", "database failure", "history query failed (database failure)"),
    )
    data = coverage(
        complete=False,
        device_state="incomplete",
        unavailable_reason="not_retained",
    )
    total = app_total(1, "unit:/alpha.service", "Alpha", 200)
    share = disk_share(1, task_total=200, residual_total=None, cov=data)
    window, service = build(qtbot, [total], {1: share})

    for kind, reason, clause in cases:
        service.daemon_answer_error = SimpleNamespace(kind=kind, reason=reason)
        window.refresh()
        assert label(window, "coverage_label").text().endswith(
            f"; reason: not_retained; {clause}"
        )
        assert label(window, "device_reason").text() == (
            f"device attribution unavailable: not_retained; {clause}"
        )

    absent = build_overview(
        1, SINCE_BOOT, None, None, None, coverage_diagnostic=cases[2][2]
    )
    assert _device_unavailable_text(absent) == (
        "device attribution unavailable: not in the process ranking; "
        "history query failed (too_many_requests)"
    )
    split_unavailable = replace(absent, share_unavailable=SHARE_UNAVAILABLE_REASON)
    assert _device_unavailable_text(split_unavailable) == (
        f"device attribution unavailable: {SHARE_UNAVAILABLE_REASON}; "
        "history query failed (too_many_requests)"
    )
    no_share_reason = replace(absent, share_unavailable=None)
    assert _device_unavailable_text(no_share_reason) == (
        "device attribution unavailable: history query failed (too_many_requests)"
    )
    no_diagnostic = build_overview(1, SINCE_BOOT, None, None, None)
    assert _device_unavailable_text(no_diagnostic) == ""

    share_failure = build_overview(
        1,
        SINCE_BOOT,
        total,
        None,
        None,
        share_error="share query unavailable",
        coverage_diagnostic=cases[2][2],
    )
    assert _device_unavailable_text(share_failure) == (
        "device attribution unavailable: share query unavailable; "
        "history query failed (too_many_requests)"
    )

    available = build_overview(
        1,
        SINCE_BOOT,
        total,
        None,
        disk_share(1, task_total=200),
        coverage_diagnostic=cases[2][2],
    )
    assert _device_unavailable_text(available) == ""


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


def test_build_overview_requires_available_device_coverage_in_series_and_share() -> None:
    task, devices = series_points()
    total = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)
    share = disk_share(1, window=SINCE_BOOT, task_total=200)
    assert share.coverage.device_state == "available"

    available = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    model = build_overview(1, SINCE_BOOT, total, available, share)
    assert [lane.dev for lane in model.device_lanes] == ["8:0"]

    # Section 2.2: lanes need available attribution in *both* successful
    # coverages. An attributed share cannot make an unattributed series
    # drawable, even when that series still carries device points.
    unattributed = Series(
        1,
        SINCE_BOOT,
        "raw",
        task,
        devices,
        (),
        coverage(complete=False, device_state="incomplete"),
    )
    model = build_overview(1, SINCE_BOOT, total, unattributed, share)
    assert model.device_lanes == ()
    assert model.more_devices == 0
    # Only the lanes are withheld: the task points and the share's DEVICE
    # total stay independently measured.
    assert model.task_points == task
    assert model.kpis[4].text == format_bytes(share.device_total_bytes)

    # The converse: an attributed series cannot rescue an unattributed share,
    # whose undefined device total also fails the lane condition.
    incomplete_share = disk_share(
        1,
        window=SINCE_BOOT,
        task_total=200,
        residual_total=None,
        cov=coverage(complete=False, device_state="incomplete"),
    )
    model = build_overview(1, SINCE_BOOT, total, available, incomplete_share)
    assert model.device_lanes == ()
    assert model.more_devices == 0


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
    series = Series(1, lifetime, "rollup_3600s", points, devices, (), coverage(tier="rollup_3600s"))
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


# --------------------------------------------------------------------------
# 8. The persistent two-tab console (design 7.3, spec §9.4).
# --------------------------------------------------------------------------

SHARE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
KPI_NAMES = ("total_value", "read_value", "write_value", "peak_value", "device_value")
UNAVAILABLE_RESIDUAL = "Residual: unavailable - difference between accounting points"


class _LocalChoices:
    """The structural ``history.local`` seam: retained file applications."""

    def __init__(self, history: FakeFilesHistory) -> None:
        self.history = history

    def file_applications(self, window: Window):
        self.history.choice_calls.append(window)
        return tuple(self.history.local_applications)


class _RegistryIds:
    """The structural ``history.registry`` seam: stable application ids."""

    def __init__(self, ids: dict[str, int]) -> None:
        self.ids = dict(ids)

    def id_for(self, key: str) -> int:
        return self.ids[key]


class FakeFilesHistory:
    """The shipped Files query surface, as a structural fake.

    It answers only ``ranking``, ``file_source_status`` and
    ``request_file_view`` (plus the optional ``local``/``registry`` choice
    seams), so an injected ``FilesView`` can build its union with no database,
    socket or producer behind it. Every request stays pending until a test
    settles it, which keeps the retirement generation checks observable.
    """

    def __init__(
        self,
        *,
        ranking_rows: tuple[AppTotal, ...] = (),
        local_applications: tuple[tuple[str, str, int], ...] = (),
        registry_ids: dict[str, int] | None = None,
    ) -> None:
        self.ranking_rows = ranking_rows
        self.local_applications = local_applications
        self.ranking_calls: list[tuple[Window, str]] = []
        self.choice_calls: list[Window] = []
        self.requests: list[tuple[object, ...]] = []
        self.pending: list[Future] = []
        self.view_state = "ready"
        if local_applications or registry_ids is not None:
            self.local = _LocalChoices(self)
            self.registry = _RegistryIds(registry_ids or {})

    def ranking(self, window: Window, metric: str) -> list[AppTotal]:
        self.ranking_calls.append((window, metric))
        return list(self.ranking_rows)

    def file_source_status(self):
        return SimpleNamespace(available=True, reason=None, diagnostic=None)

    def file_source_diagnostic(self) -> str | None:
        return None

    def ready(self, window: Window) -> bool:
        return True

    def request_file_view(self, app_id: int, window: Window, sort: str,
                          direction: str, search: str | None) -> Future:
        self.requests.append((app_id, window, sort, direction, search))
        future: Future = Future()
        self.pending.append(future)
        return future


def retained_file_result(window: Window):
    from appio.contracts import (
        FileCoverage,
        FileRow,
        FileTable,
        HookHits,
        UnknownOwnerOverflow,
    )
    from appio.history_router import FileViewResult

    inner = coverage()
    overflow = UnknownOwnerOverflow(
        window, "overflow_owner_unknown", 0, 0, 0, inner
    )
    row = FileRow(
        1, "report.txt", "resolved", False, "259:0", 42, 7, 3, 2,
        100, 50, 150, BOOT, END - 1,
    )
    table = FileTable(
        1, window, (row,), "bytes_total", "desc", None, overflow, inner
    )
    file_coverage = FileCoverage(
        1, window, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        HookHits(0, 0, 0, 0, 0, 0), 100, 50, overflow, inner,
    )
    return FileViewResult(table, file_coverage, None)


def _assert_refused_overview(window: AppIoWindow, reason: str) -> None:
    assert window.detail_tabs.currentIndex() == 0
    assert label(window, "detail_state").isVisible()
    assert label(window, "detail_state").text() == reason
    assert listing(window).count() == 0
    assert window.selected_app_id() is None
    assert window._totals == {}
    assert kpi_texts(window) == ["unavailable"] * 5
    assert window._total_chart.is_available() is False
    assert window._total_chart.columns() == ()
    assert window._total_chart.segments() == ()
    assert all(lane.segments() == () for lane in window._device_lanes)
    assert all(not lane.isVisible() for lane in window._device_lanes)
    assert window._donut.shares() is None
    assert window._share_bars.fractions() == ()
    assert label(window, "residual_value").text() == UNAVAILABLE_RESIDUAL


def _assert_refused_files(view: FilesView, reason: str) -> None:
    assert view.state == "unavailable"
    assert view.last_result is None and view.model.rowCount() == 0
    assert [item.text() for item in view._footer_labels()] == ["unavailable"] * 8
    assert [item.objectName() for item in view._footer_labels()] == [
        "files_footer_state", "files_footer_observed", "files_footer_counted",
        "files_footer_uncounted", "files_footer_unknown_owner", "files_footer_skipped",
        "files_footer_weak", "files_footer_hooks",
    ]
    assert view._window is None
    assert view._deferred_window is None
    assert view._app_id is None
    assert view._choices_loaded is False
    assert view.app_selector.count() == 0
    assert view.app_selector.currentIndex() == -1
    assert view.app_selector.currentData() is None
    assert view.app_selector.isHidden()
    assert view.refresh_wanted() is False
    assert view._outstanding is False
    assert view._unavailable_reason is None
    assert view.unavailable_text() == reason
    assert view.state_label.text() == f"file history unavailable: {reason}"
    assert not view.state_label.isHidden()


def _refusal_query_counts(service: FakeQueryService, history: FakeFilesHistory):
    return (
        len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls),
        len(history.ranking_calls), len(history.choice_calls), len(history.requests),
    )


def _populated_refusal_console(qtbot):
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)
    key = "unit:retained-only.service"
    history = FakeFilesHistory(ranking_rows=(alpha,),
        local_applications=((key, "Retained only", 40),), registry_ids={key: 99})
    window, service = build(qtbot, [alpha], files_view=FilesView(history))
    view = window.files_view
    assert len(history.requests) == 1
    history.pending[-1].set_result(retained_file_result(SINCE_BOOT))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert view.last_result is not None and view.app_selector.count() == 2
    assert view.app_selector.findData(99) >= 0
    assert "40 B" in view.app_selector.itemText(view.app_selector.findData(99))
    assert not view.app_selector.isHidden()
    assert all(item.text() != "unavailable" for item in view._footer_labels())
    view.request_refresh()
    assert len(history.requests) == 2
    assert not history.pending[-1].done() and view._outstanding is True
    return window, service, history


REFUSAL_SCOPE_KINDS = ("launch", "over-long-range")


def refused_scope(scope_kind: str) -> tuple[Window, str]:
    """The two A22 refusals whose Files cleanup must be identical."""

    if scope_kind == "launch":
        return Window("launch", BOOT, launch_id=1), A22_LAUNCH_TEXT
    assert scope_kind == "over-long-range"
    return Window("range", BOOT, 0, MAX_RANGE_NS + 1), A22_RANGE_TEXT


class _QueuedFileDeliveryWitness(QObject):
    def __init__(self, view: FilesView) -> None:
        super().__init__(view)
        self.deliveries: list[object] = []
        view._bridge.delivered.connect(self.observe, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object)
    def observe(self, payload: object) -> None:
        self.deliveries.append(payload)


def _queue_success_without_pumping(view: FilesView, pending: Future, result):
    witness = _QueuedFileDeliveryWitness(view)
    worker = threading.Thread(target=pending.set_result, args=(result,), daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and pending.done()
    assert witness.deliveries == []
    assert view._outstanding is True
    return witness


def test_default_console_offers_only_since_boot(qtbot) -> None:
    service = FakeQueryService([], {})
    window = AppIoWindow(service)
    qtbot.addWidget(window)
    window.show()
    boot = window.findChild(QAbstractButton, "scope_since_boot")
    control = window.findChild(QAbstractButton, "scope_range")
    assert boot is not None and boot.isVisible()
    assert not boot.isEnabled() and not boot.isChecked()
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert control is not None and control.isHidden()
    assert not control.isEnabled() and not control.isChecked()
    assert sorted(button.objectName() for button in window.findChildren(QAbstractButton)
                  if button.objectName().startswith("scope_")) == [
        "scope_range", "scope_since_boot",
    ]
    assert window._active_window is window._since_boot is None
    assert label(window, "range_reason").text() == ""
    assert label(window, "range_reason").isHidden()
    window._on_since_boot_clicked()
    window._on_range_clicked()
    assert service.ranking_calls == service.series_calls == service.disk_share_calls == []
    assert window._active_window is window._since_boot is None
    assert window.has_frame is False


def test_leaving_a_range_hides_the_control(qtbot) -> None:
    supported = Window("range", BOOT, 0, MAX_RANGE_NS)
    destinations = (SINCE_BOOT, Window("lifetime"),
                    Window("range", BOOT, 0, MAX_RANGE_NS + 1),
                    Window("launch", BOOT, launch_id=1))
    for supplied_boot in (False, True):
        for destination in destinations:
            service = FakeQueryService([], {})
            window = (AppIoWindow(service, BOOT, END) if supplied_boot
                      else AppIoWindow(service))
            qtbot.addWidget(window)
            window.show()
            boot = window.findChild(QAbstractButton, "scope_since_boot")
            control = window.findChild(QAbstractButton, "scope_range")
            assert boot is not None and control is not None
            remembered = window._since_boot
            window.set_window(supported)
            assert control.isVisible() and control.isEnabled() and control.isChecked()
            assert boot.isEnabled() is supplied_boot and not boot.isChecked()
            assert window._since_boot is remembered
            before = len(service.ranking_calls)
            window._on_range_clicked()
            assert len(service.ranking_calls) == before + 1
            assert service.ranking_calls[-1][0] is supported

            window.set_window(destination)

            assert window._active_window is destination
            assert window.findChild(QAbstractButton, "scope_range") is control
            assert control.isHidden() and not control.isEnabled() and not control.isChecked()
            over_long = destination.kind == "range"
            reason = label(window, "range_reason")
            assert reason.text() == (A22_RANGE_TEXT if over_long else "")
            assert reason.isVisible() is over_long
            assert control.toolTip() == (A22_RANGE_TEXT if over_long else "")
            assert control.accessibleDescription() == (A22_RANGE_TEXT if over_long else "")
            expected_boot = destination if destination.kind == "since_boot" else remembered
            assert window._since_boot is expected_boot
            assert boot.isEnabled() is (expected_boot is not None)
            assert boot.isChecked() is (destination.kind == "since_boot")
            calls = (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
            window._on_range_clicked()
            assert calls == (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))

            window.set_window(supported)
            assert window.findChild(QAbstractButton, "scope_range") is control
            assert control.isVisible() and control.isEnabled() and control.isChecked()
            assert reason.text() == "" and reason.isHidden()
            assert control.toolTip() == control.accessibleDescription() == ""
            assert window._since_boot is expected_boot
            assert boot.isEnabled() is (expected_boot is not None)
            before = len(service.ranking_calls)
            window._on_since_boot_clicked()
            if expected_boot is None:
                assert len(service.ranking_calls) == before
                assert window._active_window is supported
            else:
                assert len(service.ranking_calls) == before + 1
                assert service.ranking_calls[-1][0] is expected_boot
                assert window._active_window is expected_boot
                assert control.isHidden() and not control.isEnabled() and not control.isChecked()


@pytest.mark.parametrize("signals_blocked", [False, True])
@pytest.mark.parametrize("scope_kind", REFUSAL_SCOPE_KINDS)
def test_refused_window_clears_files_and_shows_the_reason(
    qtbot, signals_blocked, scope_kind
) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    refused, reason = refused_scope(scope_kind)
    assert A22_LAUNCH_REASON == A22_LAUNCH_TEXT
    assert A22_RANGE_REASON == A22_RANGE_TEXT
    view = window.files_view
    window.detail_tabs.setCurrentIndex(1)
    assert window.detail_tabs.currentIndex() == 1 and view.model.rowCount() == 1
    assert view._outstanding
    view.set_scope_ready(False)
    assert view._outstanding
    selections, subjects, states = [], [], []
    view.app_selector.currentIndexChanged.connect(selections.append)
    view.subject_changed.connect(subjects.append)
    view.state_changed.connect(states.append)
    view.app_selector.blockSignals(signals_blocked)
    before = _refusal_query_counts(service, history)
    generation = view._generation
    policy = (view._scope_ready, view._transient, view._sort, view._direction,
              view.search(), view._updating_scope, view._stalled_caption, view.footer_note.text())

    window.set_window(refused)

    _assert_refused_overview(window, reason)
    _assert_refused_files(view, reason)
    assert view._generation == generation + 1
    assert view.app_selector.signalsBlocked() is signals_blocked
    assert selections == subjects == []
    assert states == ["unavailable"]
    assert policy == (view._scope_ready, view._transient, view._sort, view._direction,
                      view.search(), view._updating_scope, view._stalled_caption, view.footer_note.text())
    assert _refusal_query_counts(service, history) == before
    assert window.files_message.objectName() == "files_message"
    assert window.files_message.text() == reason
    assert not window.files_message.isHidden()
    window.detail_tabs.setCurrentIndex(1)
    assert window.files_message.isVisible()


def test_refusal_clears_files_while_scope_is_transient(qtbot) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    view = window.files_view
    assert view.model.rowCount() == 1 and view.app_selector.count() == 2
    assert all(item.text() != "unavailable" for item in view._footer_labels())
    replacement = history.pending[-1]
    witness = _queue_success_without_pumping(
        view, replacement, retained_file_result(SINCE_BOOT)
    )
    view.set_transient_scope(True)
    assert view._transient is True and view._outstanding
    before = _refusal_query_counts(service, history)

    window.set_window(Window("launch", BOOT, launch_id=1))

    assert view._transient is True
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert witness.deliveries == []
    qtbot.waitUntil(lambda: len(witness.deliveries) == 1)
    assert witness.deliveries[0][1] is replacement
    assert view._transient is True
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert _refusal_query_counts(service, history) == before
    assert window.files_message.text() == A22_LAUNCH_TEXT
    assert not window.files_message.isHidden()
    window.detail_tabs.setCurrentIndex(1)
    assert window.files_message.isVisible()


@pytest.mark.parametrize("scope_kind", REFUSAL_SCOPE_KINDS)
def test_late_delivery_after_refusal_never_republishes(qtbot, scope_kind) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    refused, reason = refused_scope(scope_kind)
    view = window.files_view
    replacement = history.pending[-1]
    witness = _queue_success_without_pumping(view, replacement, retained_file_result(SINCE_BOOT))
    before = _refusal_query_counts(service, history)

    window.set_window(refused)

    assert witness.deliveries == []
    assert view.model.rowCount() == 0 and view.state == "unavailable"
    qtbot.waitUntil(lambda: len(witness.deliveries) == 1)
    assert witness.deliveries[0][1] is replacement
    _assert_refused_files(view, reason)
    _assert_refused_overview(window, reason)
    assert _refusal_query_counts(service, history) == before


def test_refusal_finishes_every_duty_before_publishing_unavailable(qtbot) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    view = window.files_view
    # Seed a non-None deferred scope and a wanted refresh through the existing
    # closed-admission seam, so the duty-2 and refresh-clear assertions are not
    # vacuous at the moment "unavailable" is emitted.
    view.set_scope_ready(False)
    view.set_window(SINCE_BOOT)
    assert view._deferred_window == SINCE_BOOT
    assert view.refresh_wanted()
    view.set_scope_ready(True)
    assert view._outstanding and view.model.rowCount() == 1
    assert view.app_selector.count() == 2
    generation = view._generation
    states: list[str] = []
    selections: list[int] = []
    subjects: list[int] = []
    captured: list[tuple[object, ...]] = []

    def capture(state: str) -> None:
        states.append(state)
        if state != "unavailable":
            return
        captured.append((
            view._generation,
            view._outstanding,
            view.refresh_wanted(),
            view._window,
            view._deferred_window,
            view._app_id,
            view._choices_loaded,
            view.app_selector.count(),
            view.app_selector.currentIndex(),
            view.app_selector.isHidden(),
            view.model.rowCount(),
            tuple(item.text() for item in view._footer_labels()),
            view._state,
        ))

    view.app_selector.currentIndexChanged.connect(selections.append)
    view.subject_changed.connect(subjects.append)
    view.state_changed.connect(capture)
    before = _refusal_query_counts(service, history)

    window.set_window(Window("launch", BOOT, launch_id=1))

    assert states == ["unavailable"]
    assert captured == [(
        generation + 1,
        False,
        False,
        None,
        None,
        None,
        False,
        0,
        -1,
        True,
        0,
        ("unavailable",) * 8,
        "unavailable",
    )], captured
    assert selections == subjects == []
    assert _refusal_query_counts(service, history) == before
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)


def test_returning_to_the_refused_window_reloads_once(qtbot) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    view = window.files_view
    supported = window._active_window
    assert isinstance(supported, Window)
    view.set_scope_ready(False)
    view.set_window(supported)
    assert view._window is supported and view._deferred_window is supported
    assert view.refresh_wanted()
    window.set_window(Window("launch", BOOT, launch_id=1))
    assert view._window is None
    assert view._deferred_window is None
    assert view._app_id is None
    assert view._choices_loaded is False
    assert view.refresh_wanted() is False
    requests = len(history.requests)
    view.set_scope_ready(True)
    assert len(history.requests) == requests

    window.set_window(supported)

    assert window._active_window is supported and view._window is supported
    assert len(history.requests) == requests + 1
    assert history.requests[-1][1] is supported
    history.pending[-1].set_result(retained_file_result(supported))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert view.state == "ready" and view.app_selector.count() == 2
    assert view.refresh_wanted() is False
    assert all(item.text() != "unavailable" for item in view._footer_labels())


def test_embedded_direct_refusal_still_clears_files(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 200)
    key = "unit:retained-only.service"
    history = FakeFilesHistory(ranking_rows=(alpha,),
        local_applications=((key, "Retained only", 40),), registry_ids={key: 99})
    history.close = lambda: None
    history.series = lambda *args: pytest.fail("refusal queried series")
    history.disk_share = lambda *args: pytest.fail("refusal queried disk share")
    shell = CollectorWindow(SimpleNamespace(close=lambda: None), history, SimpleNamespace())
    shell._timer.stop()
    shell._poll_timer.stop()
    qtbot.addWidget(shell)
    qtbot.addWidget(shell.settings)
    shell.show()
    window = shell.view
    assert window is not None
    assert window._active_window is window._since_boot is None
    assert window._owns_files_scope is False and shell._displayed is None
    view = window.files_view
    view.set_window(SINCE_BOOT)
    history.pending[-1].set_result(retained_file_result(SINCE_BOOT))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert view.app_selector.count() == 2
    assert "40 B" in view.app_selector.itemText(view.app_selector.findData(99))
    assert all(item.text() != "unavailable" for item in view._footer_labels())
    view.request_refresh()
    assert view._outstanding
    view.set_scope_ready(False)
    window.detail_tabs.setCurrentIndex(1)
    witness = _queue_success_without_pumping(view, history.pending[-1], retained_file_result(SINCE_BOOT))
    queries = (len(history.ranking_calls), len(history.choice_calls), len(history.requests))
    shell_fields = (shell._target, shell._displayed, shell._pending)

    window.set_window(Window("launch", BOOT, launch_id=1))

    assert witness.deliveries == []
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert view.isVisible() is False and shell._displayed is None
    assert shell.files_message.text() == shell._files_status_text() == "waiting for the first sample"
    assert shell.files_message.text() != A22_LAUNCH_TEXT
    qtbot.waitUntil(lambda: len(witness.deliveries) == 1)
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert queries == (len(history.ranking_calls), len(history.choice_calls), len(history.requests))
    assert shell_fields == (shell._target, shell._displayed, shell._pending)
    shell.shutdown()


def test_supported_adoption_clears_the_refusal_message(qtbot) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    supported = window._active_window
    assert isinstance(supported, Window)
    window.set_window(Window("launch", BOOT, launch_id=1))
    assert window.files_message.text() == A22_LAUNCH_TEXT
    assert not window.files_message.isHidden()
    window.detail_tabs.setCurrentIndex(1)
    before = _refusal_query_counts(service, history)

    window.set_window(supported)

    assert window.files_message.text() == ""
    assert window.files_message.isHidden()
    assert window.detail_tabs.currentIndex() == 1
    assert window._active_window is supported
    assert _refusal_query_counts(service, history) == tuple(count + 1 for count in before)
    history.pending[-1].set_result(retained_file_result(supported))
    qtbot.waitUntil(lambda: window.files_view.model.rowCount() == 1)
    assert window.files_view.state == "ready"


def retained_pending_files_view(qtbot):
    total = app_total(1, "unit:/alpha.service", "Alpha", 200)
    history = FakeFilesHistory(ranking_rows=(total,))
    view = FilesView(history)
    qtbot.addWidget(view)
    view.set_window(SINCE_BOOT)
    assert len(history.pending) == 1
    history.pending[0].set_result(retained_file_result(SINCE_BOOT))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    view.request_refresh()
    assert len(history.pending) == 2
    assert view._outstanding is True
    return view, history


def files_noncaption_snapshot(view: FilesView, history: FakeFilesHistory):
    return (
        tuple(view.model._cells),
        tuple(label.text() for label in view._footer_labels()),
        view._window,
        view._app_id,
        view._scope_ready,
        view._state,
        view.last_result,
        view.last_request,
        tuple(view.requests),
        view._generation,
        view._outstanding,
        view._refresh_wanted,
        view._updating_scope,
        view._deferred_window,
        len(history.pending),
    )


def test_files_stall_caption_preserves_retained_table_and_footer(qtbot) -> None:
    view, history = retained_pending_files_view(qtbot)
    original_caption = view.files_updating.text()
    assert original_caption == "updating to since_boot"
    before = files_noncaption_snapshot(view, history)
    stalled_text = "history read has not completed (loading); the reader is still busy"

    view.set_stalled(True, stalled_text)

    assert view.files_updating.text() == stalled_text
    assert not view.files_updating.isHidden()
    assert files_noncaption_snapshot(view, history) == before

    view.set_stalled(False, "ignored while clearing")

    assert view.files_updating.text() == original_caption
    assert not view.files_updating.isHidden()
    assert files_noncaption_snapshot(view, history) == before


def test_files_set_stalled_rejects_invalid_arguments(qtbot) -> None:
    view = FilesView(FakeFilesHistory())
    qtbot.addWidget(view)
    view.set_stalled(True, "history read has not completed (loading); the reader is still busy")
    before = (view.files_updating.text(), view.files_updating.isHidden())

    for stalled, text in ((1, "bad"), ("yes", "bad"), (True, ""), (True, None),
                          (False, None)):
        with pytest.raises(ValueError):
            view.set_stalled(stalled, text)
        assert (view.files_updating.text(), view.files_updating.isHidden()) == before

    view.set_stalled(False, "this argument is ignored")
    assert view.files_updating.text() == ""
    assert view.files_updating.isHidden()


def kpi_texts(window: AppIoWindow) -> list[str]:
    return [label(window, name).text() for name in KPI_NAMES]


def test_identical_refresh_preserves_item_identity_and_current_row(qtbot) -> None:
    totals = [
        app_total(1, "unit:/alpha.service", "Alpha", 300),
        app_total(2, "unit:/beta.service", "Beta", 200),
        app_total(3, "unit:/gamma.service", "Gamma", 100),
    ]
    window, service = build(qtbot, totals)
    widget = listing(window)
    widget.setCurrentRow(1)
    assert window.selected_app_id() == 2
    items = [widget.item(index) for index in range(widget.count())]
    assert len(items) == 3

    cleared: list[int] = []
    resets: list[int] = []
    selection_writes: list[int] = []
    selection_signals: list[tuple[object, object]] = []
    widget.clear = lambda: cleared.append(1)  # type: ignore[method-assign]
    widget.reset = lambda: resets.append(1)  # type: ignore[method-assign]
    widget.setCurrentRow = lambda row: selection_writes.append(row)  # type: ignore[method-assign]
    widget.currentItemChanged.connect(
        lambda current, previous: selection_signals.append((current, previous))
    )

    ranking_before = len(service.ranking_calls)
    series_before = len(service.series_calls)
    share_before = len(service.disk_share_calls)
    window.refresh()

    assert all(widget.item(index) is items[index] for index in range(len(items)))
    assert widget.count() == len(items)
    assert widget.currentRow() == 1
    assert widget.currentItem() is items[1]
    assert window.selected_app_id() == 2
    assert cleared == []
    assert resets == []
    assert selection_writes == []
    assert selection_signals == []
    # The one refresh re-read the ranking and the unchanged subject once each.
    assert len(service.ranking_calls) == ranking_before + 1
    assert len(service.series_calls) == series_before + 1
    assert len(service.disk_share_calls) == share_before + 1


def test_changed_shares_with_identical_labels_repaint_without_rebuild(qtbot) -> None:
    totals = [
        app_total(1, "unit:/alpha.service", "Alpha", 1500),
        app_total(2, "unit:/beta.service", "Beta", 500),
        app_total(3, "unit:/zero.service", "Zero", 0),
    ]
    window, service = build(qtbot, totals)
    widget = listing(window)
    items = [widget.item(index) for index in range(widget.count())]
    labels = [item.text() for item in items]
    # The rounding trap: a leader moving 1500 -> 1530 keeps every label.
    assert format_bytes(1500) == format_bytes(1530)
    assert labels == [
        f"Alpha  {format_bytes(1500)}",
        f"Beta  {format_bytes(500)}",
        f"Zero  {format_bytes(0)}",
    ]
    assert [item.data(SHARE_ROLE) for item in items] == [1.0, 500 / 1500, 0.0]

    cleared: list[int] = []
    widget.clear = lambda: cleared.append(1)  # type: ignore[method-assign]
    widget.repaint_requests.clear()

    service.set_totals(
        [
            app_total(1, "unit:/alpha.service", "Alpha", 1530),
            app_total(2, "unit:/beta.service", "Beta", 500),
            app_total(3, "unit:/zero.service", "Zero", 0),
        ]
    )
    window.refresh()

    assert all(widget.item(index) is items[index] for index in range(len(items)))
    assert [item.text() for item in items] == labels
    assert cleared == []
    assert [item.data(SHARE_ROLE) for item in items] == [1.0, 500 / 1530, 0.0]
    # Only the subordinate share changed: exactly that row is repainted.
    repainted = list(widget.repaint_requests)
    assert widget.visualItemRect(items[1]) in repainted
    assert widget.visualItemRect(items[0]) not in repainted
    assert widget.visualItemRect(items[2]) not in repainted


def test_changed_order_restores_subject_and_scroll(qtbot) -> None:
    totals = [
        app_total(app_id, f"unit:/app{app_id}.service", f"App{app_id}", 10_000 - app_id)
        for app_id in range(1, 41)
    ]
    window, service = build(qtbot, totals)
    window.resize(900, 420)
    widget = listing(window)
    bar = widget.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=1000)

    widget.setCurrentRow(12)
    subject = window.selected_app_id()
    assert subject == 13
    bar.setValue(9)
    assert bar.value() == 9

    ranking_before = len(service.ranking_calls)
    series_before = len(service.series_calls)
    share_before = len(service.disk_share_calls)
    service.set_totals(
        [
            app_total(app_id, f"unit:/app{app_id}.service", f"App{app_id}", app_id * 100)
            for app_id in range(1, 41)
        ]
    )
    window.refresh()

    assert window.selected_app_id() == subject
    ids = [row.app_id for row in window._ranking_model.rows]
    # The order really changed: App13 moved from index 12 to index 27.
    assert ids == list(range(40, 0, -1))
    assert ids.index(subject) == 27
    assert ids.index(subject) != 12
    assert widget.item(0).text().startswith("App40")
    assert widget.currentRow() == 27
    assert widget.currentItem() is widget.item(27)
    assert bar.value() == 9
    # The subject survived the rebuild, so no feedback selection re-queried it.
    assert len(service.ranking_calls) == ranking_before + 1
    assert len(service.series_calls) == series_before + 1
    assert len(service.disk_share_calls) == share_before + 1


def test_detail_tabs_are_exactly_overview_then_files(qtbot) -> None:
    service = FakeQueryService([], {})
    window = AppIoWindow(service)
    qtbot.addWidget(window)
    window.show()

    tabs = window.detail_tabs
    assert window.findChildren(QTabWidget) == [tabs]
    assert tabs.objectName() == "detail_tabs"
    assert [tabs.tabText(index) for index in range(tabs.count())] == [
        "Overview",
        "Files",
    ]
    assert len(window.findChildren(FilesView)) == 1
    assert window.findChildren(FilesView)[0] is window.files_view

    # Empty construction queries nothing and leaves both scopes unset.
    assert service.ranking_calls == []
    assert window._active_window is None
    assert window._since_boot is None
    since_boot = window.findChild(QAbstractButton, "scope_since_boot")
    assert since_boot is not None and not since_boot.isEnabled()
    assert window.has_frame is False

    # The scope handlers are safe before any supplied window.
    window.refresh()
    window._on_since_boot_clicked()
    window._on_range_clicked()
    assert service.ranking_calls == []
    assert window.has_frame is False

    # A supplied standalone window still renders the initial since-boot frame.
    standalone, _service = build(qtbot, [app_total(1, "unit:/a.service", "A", 100)])
    assert [
        standalone.detail_tabs.tabText(index)
        for index in range(standalone.detail_tabs.count())
    ] == ["Overview", "Files"]
    assert standalone.has_frame is True
    # A process-only fake has no file capabilities: the Files tab says so
    # explicitly instead of inventing an empty table.
    assert standalone.files_view.state == "unavailable"
    assert standalone.files_view.unavailable_text() == "the file source is unavailable"
    assert standalone.files_view.model.rowCount() == 0


def test_two_tab_switches_preserve_subject_without_scope_or_query_calls(qtbot) -> None:
    totals = [
        app_total(1, "unit:/alpha.service", "Alpha", 300),
        app_total(2, "unit:/beta.service", "Beta", 200),
    ]
    window, service = build(qtbot, totals)
    widget = listing(window)
    widget.setCurrentRow(1)
    subject = window.selected_app_id()
    row = widget.currentRow()
    assert subject == 2

    scope_calls: list[Window] = []
    original_set_window = window.set_window

    def spy_set_window(window_arg: Window) -> None:
        scope_calls.append(window_arg)
        original_set_window(window_arg)

    window.set_window = spy_set_window  # type: ignore[method-assign]
    files_scope_calls: list[Window] = []
    original_files_window = window.files_view.set_window

    def spy_files_window(window_arg: Window) -> None:
        files_scope_calls.append(window_arg)
        original_files_window(window_arg)

    window.files_view.set_window = spy_files_window  # type: ignore[method-assign]
    files_requests: list[object] = []
    original_request = window.files_view.request_refresh

    def spy_request() -> None:
        files_requests.append(1)
        original_request()

    window.files_view.request_refresh = spy_request  # type: ignore[method-assign]

    ranking_before = len(service.ranking_calls)
    series_before = len(service.series_calls)
    share_before = len(service.disk_share_calls)

    window.detail_tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.detail_tabs.currentIndex() == 1, timeout=1000)
    window.detail_tabs.setCurrentIndex(0)
    window.detail_tabs.setCurrentIndex(1)

    assert window.detail_tabs.currentIndex() == 1
    assert window.selected_app_id() == subject
    assert widget.currentRow() == row
    assert scope_calls == []
    assert files_scope_calls == []
    assert files_requests == []
    assert len(service.ranking_calls) == ranking_before
    assert len(service.series_calls) == series_before
    assert len(service.disk_share_calls) == share_before
    # The console owns no preparation/collector path a tab switch could drive.
    assert not hasattr(window, "prepare")
    assert not hasattr(window, "_start")


def test_files_selector_visibility_depends_on_union_extras(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 300)
    beta = app_total(2, "unit:/beta.service", "Beta", 100)
    file_key = "unit:retained-only.service"

    # Ranked A plus file-only B: the selector has to offer both.
    mixed_files = FakeFilesHistory(
        ranking_rows=(alpha,),
        local_applications=((file_key, "Retained only", 40),),
        registry_ids={file_key: 99},
    )
    mixed, _ = build(qtbot, [alpha, beta], files_view=FilesView(mixed_files))
    selector = mixed.files_view.app_selector
    assert mixed.selected_app_id() == 1
    assert not selector.isHidden()
    assert [selector.itemData(index) for index in range(selector.count())] == [1, 99]
    assert selector.itemText(1).startswith("Retained only")
    assert mixed_files.choice_calls == [
        Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
    ]

    # A fully ranked file population hides the selector again.
    ranked_files = FakeFilesHistory(
        ranking_rows=(alpha, beta),
        local_applications=((file_key, "Retained only", 40),),
        registry_ids={file_key: 2},
    )
    ranked, _ = build(qtbot, [alpha, beta], files_view=FilesView(ranked_files))
    hidden = ranked.files_view.app_selector
    assert hidden.isHidden()
    assert [hidden.itemData(index) for index in range(hidden.count())] == [1, 2]


def test_file_only_selection_clears_highlight_preserves_scroll_and_explains_overview(
    qtbot,
) -> None:
    totals = [
        app_total(app_id, f"unit:/app{app_id}.service", f"App{app_id}", 10_000 - app_id)
        for app_id in range(1, 41)
    ]
    file_key = "unit:retained-only.service"
    files = FakeFilesHistory(
        ranking_rows=tuple(totals),
        local_applications=((file_key, "Retained only", 40),),
        registry_ids={file_key: 99},
    )
    window, service = build(qtbot, totals, files_view=FilesView(files))
    widget = listing(window)
    bar = widget.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=1000)
    widget.setCurrentRow(2)
    assert window.selected_app_id() == 3
    # Scroll the selected row out of view: clearing the highlight later would
    # otherwise snap the list back to its current index.
    bar.setValue(bar.maximum())
    scrolled = bar.value()
    assert scrolled > 0

    series_before = len(service.series_calls)
    share_before = len(service.disk_share_calls)
    selector = window.files_view.app_selector
    selector.setCurrentIndex(selector.findData(99))

    assert window.selected_app_id() == 99
    assert widget.currentRow() == -1
    assert widget.currentItem() is None
    assert widget.selectedItems() == []
    assert bar.value() == scrolled
    assert kpi_texts(window) == ["unavailable"] * 5
    assert label(window, "detail_state").text() == "not in the process ranking"
    assert window._total_chart.is_available() is False
    assert window._total_chart.segments() == ()
    # A file-only subject has no share/series query of its own, and the
    # selector's own query for B was issued exactly once.
    assert len(service.series_calls) == series_before
    assert len(service.disk_share_calls) == share_before
    assert [request[0] for request in files.requests].count(99) == 1


def test_unavailable_series_is_not_rendered_as_zero(qtbot) -> None:
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=None)]
    share = disk_share(1, task_total=200)
    window, service = build(qtbot, totals, {1: share})
    service.series_error = WindowUnavailableError("unsupported_window", "series refused")
    window.refresh()

    chart = window._total_chart
    assert chart.is_available() is False
    assert chart.unavailable_reason() == "series refused"
    assert chart.columns() == ()
    assert chart.segments() == ()
    message = chart.findChild(QLabel, "chart_message")
    assert message is not None
    assert message.text() == "unavailable: series refused"

    assert kpi_texts(window) == [
        format_bytes(200),
        format_bytes(100),
        format_bytes(100),
        "unavailable",
        format_bytes(share.device_total_bytes),
    ]
    assert label(window, "peak_value").text() == "unavailable"
    assert "0" not in label(window, "peak_value").text()
    # The residual strip keeps its signed short form; the accountability
    # equation itself is the attribution panel's annotation (spec 7).
    assert label(window, "residual_value").text() == (
        "Residual: 0 B - difference between accounting points"
    )
    assert window._overview_model is not None
    assert window._overview_model.reconciliation is not None
    assert "attributed + (0 B) residual =" in window._overview_model.reconciliation


def test_ranking_error_marks_all_overview_dependencies_unavailable(qtbot) -> None:
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 300)]
    window, service = build(qtbot, totals)
    series_before = len(service.series_calls)
    share_before = len(service.disk_share_calls)

    reason = "window is unavailable"
    service.ranking_error = WindowUnavailableError("unsupported_window", reason)
    window.refresh()

    assert listing(window).count() == 0
    assert window.selected_app_id() is None
    assert kpi_texts(window) == ["unavailable"] * 5
    assert window._total_chart.unavailable_reason() == reason
    assert window._total_chart.columns() == ()
    assert window._total_chart.segments() == ()
    assert [lane.unavailable_reason() for lane in window._device_lanes] == [reason] * 3
    assert window._donut.unavailable_reason() == reason
    assert window._donut.shares() is None
    assert window._share_bars.unavailable_reason() == reason
    assert window._share_bars.fractions() == ()
    assert label(window, "detail_state").text() == reason
    assert label(window, "coverage_label").text() == "coverage: unavailable"
    assert label(window, "residual_value").text() == UNAVAILABLE_RESIDUAL
    # A failed ranking is not a partial success: no subject result was read.
    assert len(service.series_calls) == series_before
    assert len(service.disk_share_calls) == share_before
    assert window.has_frame is True


def test_share_error_keeps_task_lane_and_marks_dependent_values_unavailable(qtbot) -> None:
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)]
    share = disk_share(1, task_total=200)
    window, service = build(qtbot, totals, {1: share}, series={1: series})

    service.share_error = WindowUnavailableError("not_retained", "disk split refused")
    window.refresh()

    assert kpi_texts(window) == [
        "unavailable",
        "unavailable",
        "unavailable",
        format_peak(100),
        "unavailable",
    ]
    # The task lane is independently successful and still draws.
    assert window._total_chart.is_available() is True
    assert window._total_chart.segments() != ()
    # The donut fails with the query's reason; it is never a fallback zero.
    assert window._donut.shares() is None
    assert window._donut.unavailable_reason() == "disk split refused"
    assert window._share_bars.unavailable_reason() == "disk split refused"
    assert window._share_bars.fractions() == ()
    assert "; share: disk split refused" in label(window, "detail_state").text()


@pytest.mark.parametrize("fresh", [False, True])
def test_total_gutter_is_unavailable_on_share_failure_then_recovers(
    qtbot, monkeypatch, fresh
) -> None:
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    total = app_total(1, "unit:/alpha.service", "Alpha", 200)
    service = FakeQueryService([total], {1: disk_share(1, task_total=200)},
                               series={1: series})
    failure = WindowUnavailableError("not_retained", "disk split refused")
    if fresh:
        service.share_error = failure
    window = AppIoWindow(service, BOOT, END)
    qtbot.addWidget(window)
    window.show()
    if not fresh:
        service.share_error = failure
        window.refresh()

    # Observe the actual painted gutter, not just a new availability flag.
    drawn = []
    original = QPainter.drawText

    def record(painter, *args):
        if args and isinstance(args[-1], str):
            drawn.append(args[-1])
        return original(painter, *args)

    monkeypatch.setattr(QPainter, "drawText", record)
    chart = window._total_chart
    chart.grab()
    assert "r unavailable · w unavailable" in drawn
    assert "r 0 B · w 0 B" not in drawn
    assert "r 100 B · w 100 B" not in drawn
    assert chart.is_available() is True and chart.segments()

    # Measured zero is different from unavailable, and a later nonzero total
    # comes from the exact share result rather than retained series samples.
    service.share_error = None
    for measured, expected in [(0, "r 0 B · w 0 B"),
                               (600, "r 300 B · w 300 B")]:
        service.set_share(1, disk_share(1, task_total=measured))
        window.refresh()
        drawn.clear()
        chart.grab()
        assert expected in drawn
        assert "r unavailable · w unavailable" not in drawn
        assert chart.is_available() is True and chart.segments()


def test_series_failure_and_empty_series_are_distinct(qtbot) -> None:
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=0)]
    share = disk_share(1, task_total=200)
    window, service = build(qtbot, totals, {1: share})

    service.series_error = WindowUnavailableError("not_retained", "series refused")
    window.refresh()
    chart = window._total_chart
    assert chart.is_available() is False
    assert chart.unavailable_reason() == "series refused"
    assert chart.segments() == ()
    assert kpi_texts(window) == [
        format_bytes(200),
        format_bytes(100),
        format_bytes(100),
        "0 B/s",
        format_bytes(share.device_total_bytes),
    ]

    # An empty successful series is available, shapes nothing and is not zero.
    service.series_error = None
    service.set_series(
        1, Series(1, SINCE_BOOT, "raw", (), (), (), coverage())
    )
    window.refresh()
    assert chart.is_available() is True
    assert chart.unavailable_reason() is None
    assert chart.columns() == ()
    assert chart.segments() == ()
    message = chart.findChild(QLabel, "chart_message")
    assert message is not None and message.text() == "no samples in this window"
    assert kpi_texts(window) == [
        format_bytes(200),
        format_bytes(100),
        format_bytes(100),
        "0 B/s",
        format_bytes(share.device_total_bytes),
    ]


def test_device_unavailable_keeps_task_composition(qtbot) -> None:
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    totals = [app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)]
    incomplete = coverage(
        complete=False, device_state="incomplete", unavailable_reason="not_retained"
    )
    share = disk_share(1, task_total=200, residual_total=None, cov=incomplete)
    window, _ = build(qtbot, totals, {1: share}, series={1: series})

    # The task donut and the task lane survive their own valid inputs.
    assert window._donut.unavailable_reason() is None
    assert window._donut.shares() == (0.5, 0.5)
    assert window._total_chart.is_available() is True
    assert window._total_chart.segments() != ()
    # Device attribution is withheld, never rendered as a zero.
    assert label(window, "device_value").text() == "unavailable"
    assert window._share_bars.fractions() == ()
    assert window._share_bars.unavailable_reason() == "not_retained"
    assert window._device_reason.text() == "device attribution unavailable: not_retained"
    assert label(window, "residual_value").text() == UNAVAILABLE_RESIDUAL
    assert window._overview_model is not None
    assert window._overview_model.reconciliation is None
    assert all(lane.isVisible() is False for lane in window._device_lanes)


def test_empty_ranking_has_no_shapes_or_zero_kpis(qtbot) -> None:
    empty, service = build(qtbot, [])
    assert listing(empty).count() == 0
    assert empty.selected_app_id() is None
    assert kpi_texts(empty) == ["unavailable"] * 5
    assert label(empty, "detail_state").text() == (
        "window: since_boot; no applications observed"
    )
    assert empty._total_chart.is_available() is False
    assert empty._total_chart.columns() == ()
    assert empty._total_chart.segments() == ()
    assert all(lane.segments() == () for lane in empty._device_lanes)
    assert empty._donut.shares() is None
    assert empty._share_bars.fractions() == ()
    assert empty.has_frame is True
    assert service.series_calls == []
    assert service.disk_share_calls == []

    # Emptying both populations clears the prior shared and Files subjects, and
    # a late old-subject delivery cannot publish over the measured emptiness.
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 300)
    files = FakeFilesHistory(ranking_rows=(alpha,))
    window, service = build(
        qtbot, [alpha], files_view=FilesView(files)
    )
    widget = listing(window)
    assert window.selected_app_id() == 1
    assert widget.currentRow() == 0
    assert files.requests and files.requests[0][0] == 1
    stale = files.pending[-1]
    stale_generation = window.files_view._generation
    assert window.files_view._outstanding is True

    service.set_totals([])
    files.ranking_rows = ()
    window.set_window(SINCE_BOOT)

    assert window.selected_app_id() is None
    assert widget.count() == 0
    assert widget.currentRow() == -1
    assert window.files_view.state == "ready"
    assert window.files_view.model.rowCount() == 0
    assert window.files_view.footer_state.text() == (
        "coverage: not applicable; no applications observed in this window"
    )
    assert window.files_view._generation > stale_generation

    stale.set_exception(
        WindowUnavailableError(
            "file_source_unavailable", "File history generation retired."
        )
    )
    qtbot.waitUntil(lambda: stale.done(), timeout=1000)
    assert window.selected_app_id() is None
    assert window.files_view.model.rowCount() == 0
    assert window.files_view.state == "ready"
    assert window.files_view.footer_state.text() == (
        "coverage: not applicable; no applications observed in this window"
    )


# --------------------------------------------------------------------------
# 9. Follow-up pins for the console window/diffing rules (review CR-1..CR-9).
# --------------------------------------------------------------------------


def device_share(
    entries: tuple[tuple[str, str, int], ...],
    *,
    window: Window | None = None,
    app_id: int = 1,
    cov: Coverage | None = None,
) -> DiskShare:
    """A DiskShare whose device rows are exactly ``(boot_id, dev, total)``."""

    rows = tuple(
        DeviceShareRow(boot, dev, total // 2, total - total // 2, total)
        for boot, dev, total in entries
    )
    device_total = sum(total for _boot, _dev, total in entries)
    task_read = device_total // 2
    return DiskShare(
        app_id,
        window or SINCE_BOOT,
        task_read,
        device_total - task_read,
        device_total,
        rows,
        task_read,
        device_total - task_read,
        device_total,
        0,
        0,
        0,
        cov or coverage(),
    )


def device_points_for(
    devs: tuple[str, ...], *, start: int = 1_000_000_000, bucket_secs: int = 10
) -> tuple[DeviceSeriesPoint, ...]:
    """One ordered device bucket per token so lanes have drawable samples."""

    return tuple(
        DeviceSeriesPoint(start, bucket_secs, dev, 512, 256) for dev in devs
    )


def test_same_order_refresh_updates_changed_labels_in_place(qtbot) -> None:
    totals = [
        app_total(1, "unit:/alpha.service", "Alpha", 300),
        app_total(2, "unit:/beta.service", "Beta", 200),
        app_total(3, "unit:/gamma.service", "Gamma", 100),
    ]
    window, service = build(qtbot, totals)
    widget = listing(window)
    widget.setCurrentRow(1)
    items = [widget.item(index) for index in range(widget.count())]
    assert window.selected_app_id() == 2
    cleared: list[int] = []
    widget.clear = lambda: cleared.append(1)  # type: ignore[method-assign]
    text_writes: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        original = item.setText

        def spy(text: str, index: int = index, original=original) -> None:
            text_writes.append((index, text))
            original(text)

        item.setText = spy  # type: ignore[method-assign]

    # The ID order is unchanged, so the diff branch must run and carry the
    # renamed label into the existing item object (spec 3.3).
    service.set_totals(
        [
            app_total(1, "unit:/alpha.service", "Alpha renamed", 300),
            app_total(2, "unit:/beta.service", "Beta", 200),
            app_total(3, "unit:/gamma.service", "Gamma", 100),
        ]
    )
    window.refresh()

    expected = [
        f"Alpha renamed  {format_bytes(300)}",
        f"Beta  {format_bytes(200)}",
        f"Gamma  {format_bytes(100)}",
    ]
    assert [item.text() for item in items] == expected
    assert all(widget.item(index) is items[index] for index in range(len(items)))
    # Only the changed row's text is written; an unchanged row is untouched.
    assert text_writes == [(0, expected[0])]
    assert cleared == []
    assert widget.currentRow() == 1
    assert window.selected_app_id() == 2


def test_ranking_selection_requests_files_once_for_new_subject(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 300)
    beta = app_total(2, "unit:/beta.service", "Beta", 200)
    files = FakeFilesHistory(ranking_rows=(alpha, beta))
    window, service = build(qtbot, [alpha, beta], files_view=FilesView(files))
    widget = listing(window)
    assert window.selected_app_id() == 1
    assert [request[0] for request in files.requests] == [1]
    ranking_before = len(service.ranking_calls)

    widget.setCurrentRow(1)

    assert window.selected_app_id() == 2
    # A user's ranking selection passes request_files=True, so Files follows
    # the subject with exactly one query (spec 3.3, `_set_subject`).
    assert [request[0] for request in files.requests] == [1, 2]
    assert window.files_view.app_selector.currentData() == 2
    assert len(service.ranking_calls) == ranking_before


def test_refresh_retains_file_only_subject(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 300)
    beta = app_total(2, "unit:/beta.service", "Beta", 100)
    file_key = "unit:retained-only.service"
    files = FakeFilesHistory(
        ranking_rows=(alpha, beta),
        local_applications=((file_key, "Retained only", 40),),
        registry_ids={file_key: 99},
    )
    window, _ = build(qtbot, [alpha, beta], files_view=FilesView(files))
    selector = window.files_view.app_selector
    selector.setCurrentIndex(selector.findData(99))
    assert window.selected_app_id() == 99
    assert listing(window).currentRow() == -1
    requests_before = len(files.requests)

    window.refresh()

    # A valid file-only subject survives a process redraw (spec 5.3 rule 5).
    assert window.selected_app_id() == 99
    assert listing(window).currentRow() == -1
    assert kpi_texts(window) == ["unavailable"] * 5
    assert label(window, "detail_state").text() == "not in the process ranking"
    assert len(files.requests) == requests_before


def test_applied_frame_issues_at_most_one_files_query(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 300)
    beta = app_total(2, "unit:/beta.service", "Beta", 200)
    files = FakeFilesHistory(ranking_rows=(alpha, beta))
    window, service = build(qtbot, [alpha, beta], files_view=FilesView(files))
    assert [request[0] for request in files.requests] == [1]

    # A changed scope: adoption satisfies the frame's one query (spec 5.2).
    range_window = Window(
        kind="range", boot_id=BOOT, start_mono_ns=0, end_mono_ns=MAX_RANGE_NS
    )
    service.set_share(1, disk_share(1, window=range_window, task_total=300))
    before = len(files.requests)
    window.set_window(range_window)
    assert [request[0] for request in files.requests[before:]] == [1]

    # An unchanged scope whose delivery was retired: the flush issues the one
    # request the frame needs, and nothing issues a second (spec 5.2 step 4).
    window.files_view.retire_deliveries()
    assert window.files_view.refresh_wanted() is True
    before = len(files.requests)
    window.set_window(range_window)
    assert [request[0] for request in files.requests[before:]] == [1]


def test_repeated_process_redraws_issue_no_files_query(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 300)
    beta = app_total(2, "unit:/beta.service", "Beta", 200)
    files = FakeFilesHistory(ranking_rows=(alpha, beta))
    window, service = build(qtbot, [alpha, beta], files_view=FilesView(files))
    before = len(files.requests)
    ranking_before = len(service.ranking_calls)

    for _ in range(3):
        window.refresh()

    assert len(service.ranking_calls) == ranking_before + 3
    # Repeated process redraws re-read the ranking and the subject only.
    assert len(files.requests) == before
    assert window.files_view.refresh_wanted() is False


def test_device_lanes_and_more_caption_follow_the_model(qtbot) -> None:
    devs = ("8:0", "8:16", "8:32", "8:48", "8:64")
    entries = tuple(
        (BOOT, dev, total) for dev, total in zip(devs, (500, 400, 300, 200, 100))
    )
    task, _devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, device_points_for(devs), (), coverage())
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 1500)
    window, service = build(
        qtbot, [alpha], {1: device_share(entries)}, series={1: series}
    )
    lanes = window._device_lanes
    more = label(window, "more_devices")
    assert len(lanes) == MAX_DEVICE_LANES

    # Active lanes are shown and captioned with their own raw tokens; the
    # remaining devices are counted, never silently dropped.
    assert [lane._caption for lane in lanes] == ["8:0", "8:16", "8:32"]
    assert [lane.isVisible() for lane in lanes] == [True, True, True]
    assert more.text() == "and 2 more"
    assert more.isHidden() is False

    # A later tick re-captions the reused lanes; a stale caption is a defect.
    service.set_share(
        1,
        device_share(
            tuple(
                (BOOT, dev, total)
                for dev, total in zip(devs, (100, 200, 50, 500, 400))
            )
        ),
    )
    window.refresh()
    assert [lane._caption for lane in lanes] == ["8:48", "8:64", "8:16"]
    assert more.text() == "and 2 more"

    # Fewer active devices: the spare lane hides and the count disappears.
    service.set_share(1, device_share(((BOOT, "8:0", 100), (BOOT, "8:16", 200))))
    window.refresh()
    assert [lane._caption for lane in lanes[:2]] == ["8:16", "8:0"]
    assert lanes[2].isVisible() is False
    assert more.isHidden() is True


def test_attribution_panel_keeps_rows_and_boot_qualifies_lifetime_labels(
    qtbot,
) -> None:
    lifetime = Window(kind="lifetime")
    entries = (
        ("boot-a", "8:0", 500),
        ("boot-b", "8:0", 400),
        ("boot-a", "8:16", 300),
        ("boot-b", "8:16", 200),
        ("boot-a", "8:32", 100),
    )
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 1500)
    window, service = build(qtbot, [alpha], {})
    service.set_share(1, device_share(entries, window=lifetime))
    window.set_window(lifetime)

    rows = [row_label for row_label, _fraction in window._share_bars.fractions()]
    # Every share row is kept (the three-lane cap is for time lanes only) and
    # repeated timeless tokens stay distinct across boots (spec 6.4).
    assert rows == [
        "8:0 [boot-a]",
        "8:0 [boot-b]",
        "8:16 [boot-a]",
        "8:16 [boot-b]",
        "8:32 [boot-a]",
    ]

    # A single-boot scope keeps the raw token without a boot qualifier.
    service.set_share(1, device_share(((BOOT, "8:0", 500), (BOOT, "8:16", 300))))
    window.set_window(SINCE_BOOT)
    rows = [row_label for row_label, _fraction in window._share_bars.fractions()]
    assert rows == ["8:0", "8:16"]


def test_later_ticks_update_coverage_device_strip_and_reconciliation(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 200)
    window, service = build(
        qtbot, [alpha], {1: disk_share(1, task_total=200, residual_total=0)}
    )
    bars = window._share_bars
    first = bars._reconciliation
    assert first is not None
    assert first == "Σ disks 200 B + residual (0 B) = 200 B total"
    assert "gaps: 0" in label(window, "coverage_label").text()

    # Same attributed rows, a different residual: the annotation changes even
    # though the row tuple did not (spec 3.3 `_apply_overview`).
    service.set_share(1, disk_share(1, task_total=220, residual_total=20))
    window.refresh()
    assert window._overview_model is not None
    assert bars._reconciliation == "Σ disks 200 B + residual (20 B) = 220 B total"
    assert bars._reconciliation != first
    assert "(20 B)" in bars._reconciliation
    assert "220 B total" in bars._reconciliation

    # Coverage is not frozen at the first frame.
    service.set_share(
        1,
        disk_share(
            1, task_total=220, residual_total=20, cov=coverage(gap_intervals=2)
        ),
    )
    window.refresh()
    assert "gaps: 2" in label(window, "coverage_label").text()

    # The device strip appears on the later tick that loses attribution.
    unavailable = coverage(
        complete=False, device_state="incomplete", unavailable_reason="not_retained"
    )
    service.set_share(
        1, disk_share(1, task_total=220, residual_total=None, cov=unavailable)
    )
    window.refresh()
    reason = label(window, "device_reason")
    assert reason.text() == "device attribution unavailable: not_retained"
    assert reason.isVisible() is True


@pytest.mark.parametrize("residual", [200, 0, -20, None])
def test_share_panel_uses_its_own_exact_signed_equation(qtbot, residual) -> None:
    total = app_total(1, "unit:/alpha.service", "Alpha", 200)
    share = disk_share(1, task_total=200, residual_total=residual)
    window, _ = build(qtbot, [total], {1: share})
    expected = None if residual is None else (
        f"Σ disks {format_bytes(share.device_total_bytes)} + residual "
        f"({format_bytes(residual)}) = 200 B total"
    )
    assert window._share_bars._reconciliation == expected
    if residual == 200:
        assert window._share_bars.fractions() == ()
        annotation = window._share_bars.findChild(QLabel, "share_reconciliation")
        assert annotation is not None and annotation.isVisible()
        assert annotation.text() == expected
    if residual is not None:
        # Preserve the separate render-model / residual-strip contract.
        assert window._overview_model.reconciliation.startswith("Residual:")
        assert label(window, "residual_value").text().startswith("Residual:")


def test_ranking_delegate_draws_bar_and_reserves_its_height(qtbot) -> None:
    view = QListWidget()
    qtbot.addWidget(view)
    model = QStandardItemModel()
    model.appendRow(QStandardItem(f"Alpha  {format_bytes(300)}"))
    index = model.index(0, 0)

    delegate = RankingDelegate(view)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 200, 40)
    option.state = QStyle.StateFlag.State_Enabled
    option.widget = view
    option.font = view.font()

    def painted(share: float) -> QImage:
        model.setData(index, share, SHARE_ROLE)
        image = QImage(200, 40, QImage.Format.Format_RGB32)
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        try:
            delegate.paint(painter, option, index)
        finally:
            painter.end()
        return image

    def pixels_of(image: QImage, colour: QColor) -> int:
        target = colour.rgb() & 0xFFFFFF
        return sum(
            1
            for y in range(image.height())
            for x in range(image.width())
            if (image.pixel(x, y) & 0xFFFFFF) == target
        )

    full = painted(1.0)
    empty = painted(0.0)
    # The inline fill comes from SHARE_ROLE; a zero share paints only track.
    assert pixels_of(full, READ_COLOR) > 0
    assert pixels_of(empty, READ_COLOR) == 0
    assert pixels_of(empty, TRACK_COLOR) > 0

    # sizeHint reserves one bar row under the text (spec 3.3).
    base = QStyledItemDelegate().sizeHint(option, index)
    hinted = delegate.sizeHint(option, index)
    assert hinted.width() == base.width()
    assert hinted.height() >= base.height() + BAR_HEIGHT


# --------------------------------------------------------------------------
# 12.5 Measured provenance, typed series metadata and endpoint availability
# (spec §7.1–§7.3, plan Task 8).
# --------------------------------------------------------------------------

BOOT_A = "aaaaaaaa-1111-4111-8111-111111111111"
BOOT_B = "bbbbbbbb-2222-4222-8222-222222222222"
WINDOW_A = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=20_000_000_000)
WINDOW_B = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=30_000_000_000)
SUBJECT_KEY = "unit:/alpha.service"
DEVICE_ENDPOINTS_REASON = "device measurements have different endpoints"
DEVICE_TOTAL_REASON = "device total unavailable"


def measured_share(
    window: Window,
    *,
    boot: str = BOOT_A,
    task_read: int = 17,
    task_write: int = 5,
    device_read: int = 10,
    device_write: int = 4,
    cov: Coverage | None = None,
) -> DiskShare:
    """The §12.5 fixture share: read 17, write 5, total 22, device 14."""

    residual_read = task_read - device_read
    residual_write = task_write - device_write
    return DiskShare(
        1,
        window,
        task_read,
        task_write,
        task_read + task_write,
        (DeviceShareRow(boot, "8:0", device_read, device_write, device_read + device_write),),
        device_read,
        device_write,
        device_read + device_write,
        residual_read,
        residual_write,
        residual_read + residual_write,
        cov or coverage(),
    )


def measured_series(window: Window) -> Series:
    """One raw 10 s task bucket with its matching ``8:0`` device bucket."""

    return Series(
        1,
        window,
        "raw",
        (SeriesPoint(0, 10, None, 512, 256, 1024, None, None, False),),
        (DeviceSeriesPoint(0, 10, "8:0", 10, 4),),
        (),
        coverage(),
    )


def measured_provenance(
    kind: str,
    app_key: str,
    window: Window,
    *,
    first_seen: int | None = None,
) -> AnswerProvenance:
    """An identity-less provenance, or one carrying an accepted identity+stamp."""

    identity = None
    if first_seen is not None:
        digest = {"ranking": "a", "totals": "b", "series": "c"}[kind]
        identity = AcceptedIdentity(kind, app_key, window, digest * 64)
    return AnswerProvenance(
        kind,
        app_key,
        retention_scope(app_key, window),
        window,
        identity,
        first_seen,
        None,
        None,
    )


def test_overview_provenance_accepts_older_same_scope_rejects_contradictions() -> None:
    total_b = app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)
    series_b = measured_series(WINDOW_B)
    share_a = measured_share(WINDOW_A)
    ranking_p_b = measured_provenance(
        "ranking", SUBJECT_KEY, WINDOW_B, first_seen=3_000_000_000
    )
    series_p_b = measured_provenance(
        "series", SUBJECT_KEY, WINDOW_B, first_seen=3_000_000_000
    )
    share_p_a = measured_provenance(
        "totals", SUBJECT_KEY, WINDOW_A, first_seen=1_000_000_000
    )

    model = build_overview(
        1,
        WINDOW_B,
        total_b,
        series_b,
        share_a,
        ranking_provenance=ranking_p_b,
        series_provenance=series_p_b,
        share_provenance=share_p_a,
    )
    assert model.requested_window == WINDOW_B
    assert model.app_key == SUBJECT_KEY
    assert model.ranking_provenance is ranking_p_b
    assert model.series_provenance is series_p_b
    assert model.share_provenance is share_p_a
    # The older same-scope share keeps its own Window, identity and stamp.
    assert model.share is share_a
    assert model.share.window == WINDOW_A
    assert model.share_provenance.measured_window == WINDOW_A
    assert model.share_provenance.accepted_identity is not None
    assert model.share_provenance.accepted_identity.window == WINDOW_A
    assert model.share_provenance.first_seen_at_ns == 1_000_000_000
    assert model.series_metadata == SeriesMetadata(
        1, SUBJECT_KEY, WINDOW_B, "raw", coverage()
    )
    assert model.task_points == series_b.points
    assert [kpi.text for kpi in model.kpis[:3]] == [
        format_bytes(22),
        format_bytes(17),
        format_bytes(5),
    ]
    assert model.device_lanes == () and model.more_devices == 0
    assert model.device_lanes_unavailable == DEVICE_ENDPOINTS_REASON
    assert (
        f"; share measured: since_boot {BOOT_A} through 20000000000 ns"
        in model.detail_line
    )
    assert "series measured:" not in model.detail_line

    # Exact legacy calls stay valid and carry identity-less provenance.
    legacy = build_overview(
        1,
        WINDOW_A,
        app_total(1, SUBJECT_KEY, "Alpha", 22),
        measured_series(WINDOW_A),
        measured_share(WINDOW_A),
    )
    assert legacy.requested_window == WINDOW_A
    assert legacy.app_key == SUBJECT_KEY
    assert legacy.series_metadata == SeriesMetadata(
        1, SUBJECT_KEY, WINDOW_A, "raw", coverage()
    )
    for provenance, kind in (
        (legacy.ranking_provenance, "ranking"),
        (legacy.series_provenance, "series"),
        (legacy.share_provenance, "totals"),
    ):
        assert provenance is not None
        assert provenance.kind == kind
        assert provenance.app_key == SUBJECT_KEY
        assert provenance.measured_window == WINDOW_A
        assert provenance.accepted_identity is None
        assert provenance.first_seen_at_ns is None
    assert [lane.dev for lane in legacy.device_lanes] == ["8:0"]
    assert legacy.device_lanes_unavailable is None
    assert "measured:" not in legacy.detail_line

    # Frozen early branches carry the requested Window but no external key,
    # no accepted detail and no lane reason.
    refused = build_overview(
        1, WINDOW_A, None, None, None, ranking_error="window is unavailable"
    )
    file_only = build_overview(1, WINDOW_A, None, None, None)
    for early in (refused, file_only):
        assert early.requested_window == WINDOW_A
        assert early.app_key is None
        assert early.ranking_provenance is None
        assert early.series_provenance is None
        assert early.share_provenance is None
        assert early.series_metadata is None
        assert early.device_lanes_unavailable is None
    assert refused.detail_line == "window is unavailable"
    assert file_only.detail_line == FILE_ONLY_REASON

    # A measured endpoint after the requested endpoint is never rewritten.
    with pytest.raises(ValueError, match="after the requested"):
        build_overview(
            1,
            WINDOW_A,
            app_total(1, SUBJECT_KEY, "Alpha", 22),
            measured_series(WINDOW_B),
            measured_share(WINDOW_A),
            series_provenance=series_p_b,
        )
    # Another boot, start or complete range tuple is another retention scope.
    series_boot_b = measured_series(
        Window(kind="since_boot", boot_id=BOOT_B, end_mono_ns=20_000_000_000)
    )
    with pytest.raises(ValueError, match="retention scope"):
        build_overview(
            1,
            WINDOW_A,
            app_total(1, SUBJECT_KEY, "Alpha", 22),
            series_boot_b,
            measured_share(WINDOW_A),
            series_provenance=measured_provenance(
                "series", SUBJECT_KEY, series_boot_b.window
            ),
        )
    range_full = Window(
        kind="range", boot_id=BOOT_A, start_mono_ns=0, end_mono_ns=20_000_000_000
    )
    range_short = Window(
        kind="range", boot_id=BOOT_A, start_mono_ns=0, end_mono_ns=10_000_000_000
    )
    range_late = Window(
        kind="range", boot_id=BOOT_A, start_mono_ns=5_000_000_000, end_mono_ns=20_000_000_000
    )
    for measured in (range_short, range_late):
        with pytest.raises(ValueError, match="retention scope"):
            build_overview(
                1,
                range_full,
                app_total(1, SUBJECT_KEY, "Alpha", 22),
                measured_series(measured),
                measured_share(range_full),
                series_provenance=measured_provenance("series", SUBJECT_KEY, measured),
            )
    # A range control with the complete tuple remains exact and eligible.
    full_range = build_overview(
        1,
        range_full,
        app_total(1, SUBJECT_KEY, "Alpha", 22),
        measured_series(range_full),
        measured_share(range_full),
        series_provenance=measured_provenance("series", SUBJECT_KEY, range_full),
        share_provenance=measured_provenance("totals", SUBJECT_KEY, range_full),
    )
    assert full_range.series_metadata.measured_window == range_full
    assert full_range.series_provenance.measured_window == range_full
    assert [lane.dev for lane in full_range.device_lanes] == ["8:0"]
    assert full_range.device_lanes_unavailable is None
    # A boot-less lifetime control keeps typed metadata and no time lanes.
    lifetime = Window(kind="lifetime")
    eternal_series = measured_provenance("series", SUBJECT_KEY, lifetime, first_seen=1)
    eternal_share = measured_provenance("totals", SUBJECT_KEY, lifetime, first_seen=1)
    eternal = build_overview(
        1,
        lifetime,
        app_total(1, SUBJECT_KEY, "Alpha", 22),
        measured_series(lifetime),
        measured_share(lifetime, boot=BOOT_A),
        series_provenance=eternal_series,
        share_provenance=eternal_share,
    )
    assert eternal.series_metadata == SeriesMetadata(
        1, SUBJECT_KEY, lifetime, "raw", coverage()
    )
    assert eternal.series_provenance is eternal_series
    assert eternal.series_unavailable == LIFETIME_REASON
    assert eternal.task_points == ()
    assert eternal.device_lanes == () and eternal.more_devices == 0
    assert eternal.device_lanes_unavailable == LIFETIME_REASON
    assert "measured:" not in eternal.detail_line

    # Wrong subject, wrong dependency kind and a provenance/value window
    # mismatch are contradictions, never silently repaired values.
    with pytest.raises(ValueError, match="external subject"):
        build_overview(
            1,
            WINDOW_B,
            total_b,
            series_b,
            share_a,
            series_provenance=measured_provenance(
                "series", "unit:/beta.service", WINDOW_B
            ),
            share_provenance=share_p_a,
        )
    with pytest.raises(ValueError, match="provenance must describe"):
        build_overview(
            1,
            WINDOW_B,
            total_b,
            series_b,
            share_a,
            series_provenance=share_p_a,
            share_provenance=share_p_a,
        )
    with pytest.raises(ValueError, match="measured window must match"):
        build_overview(
            1,
            WINDOW_B,
            total_b,
            series_b,
            share_a,
            share_provenance=measured_provenance("totals", SUBJECT_KEY, WINDOW_B),
        )
    # The ranking provenance describes the applied frame and is exactly at
    # the requested Window; an older ranking row cannot be borrowed.
    with pytest.raises(ValueError, match="requested window"):
        build_overview(
            1,
            WINDOW_B,
            total_b,
            series_b,
            measured_share(WINDOW_B),
            ranking_provenance=measured_provenance("ranking", SUBJECT_KEY, WINDOW_A),
        )
    # Provenance without its value would fabricate accepted data.
    with pytest.raises(ValueError, match="without its value"):
        build_overview(
            1, WINDOW_B, total_b, None, share_a, series_provenance=series_p_b
        )
    with pytest.raises(ValueError, match="without its value"):
        build_overview(
            1, WINDOW_B, total_b, series_b, None, share_provenance=share_p_a
        )
    # An object with its own error stays a contradiction even with provenance.
    with pytest.raises(ValueError):
        build_overview(
            1,
            WINDOW_B,
            total_b,
            series_b,
            share_a,
            series_error="series refused",
            series_provenance=series_p_b,
            share_provenance=share_p_a,
        )
    # A nonexact value without explicit provenance cannot silently weaken the
    # old structural callers' exact-window validation.
    with pytest.raises(ValueError, match="exact"):
        build_overview(
            1,
            WINDOW_B,
            total_b,
            measured_series(WINDOW_A),
            measured_share(WINDOW_B),
        )


def test_overview_direct_metadata_validates_series_coverage_and_requested_endpoint() -> None:
    total_b = app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)
    series_b = measured_series(WINDOW_B)
    share_a = measured_share(WINDOW_A)
    series_p_b = measured_provenance(
        "series", SUBJECT_KEY, WINDOW_B, first_seen=3_000_000_000
    )
    share_p_a = measured_provenance("totals", SUBJECT_KEY, WINDOW_A, first_seen=1)

    model = build_overview(
        1,
        WINDOW_B,
        total_b,
        series_b,
        share_a,
        series_provenance=series_p_b,
        share_provenance=share_p_a,
    )
    assert model.requested_window == WINDOW_B
    assert model.app_key == SUBJECT_KEY
    assert model.share.window == WINDOW_A
    assert model.share_provenance.measured_window == WINDOW_A
    assert model.series_metadata == SeriesMetadata(
        1, SUBJECT_KEY, WINDOW_B, "raw", coverage()
    )
    assert model.device_lanes == () and model.more_devices == 0
    assert model.device_lanes_unavailable == DEVICE_ENDPOINTS_REASON
    assert (
        f"; share measured: since_boot {BOOT_A} through 20000000000 ns"
        in model.detail_line
    )
    # A ranking row from another applied frame cannot be replaced in place.
    with pytest.raises(ValueError, match="exactly at the requested"):
        replace(
            model,
            ranking_provenance=measured_provenance("ranking", SUBJECT_KEY, WINDOW_A),
        )

    # An otherwise eligible model with populated lanes cannot claim a
    # device-incomplete successful series.
    eligible = build_overview(
        1,
        WINDOW_A,
        app_total(1, SUBJECT_KEY, "Alpha", 22),
        measured_series(WINDOW_A),
        measured_share(WINDOW_A),
    )
    assert [lane.dev for lane in eligible.device_lanes] == ["8:0"]
    # The gutter is the share row's exact bytes, never a device-point sum.
    assert (
        eligible.device_lanes[0].read_bytes,
        eligible.device_lanes[0].write_bytes,
    ) == (10, 4)
    assert eligible.device_lanes_unavailable is None
    with pytest.raises(ValueError, match="device lanes"):
        replace(eligible, device_lanes_unavailable=SERIES_REASON)
    with pytest.raises(ValueError, match="device lanes"):
        replace(
            eligible,
            series_metadata=replace(
                eligible.series_metadata,
                coverage=coverage(complete=False, device_state="incomplete"),
            ),
        )

    # A 30 s detail with a 20 s requested Window is rejected even with no
    # ranking provenance at all.
    no_ranking = replace(model, ranking_provenance=None)
    assert no_ranking.ranking_provenance is None
    with pytest.raises(ValueError, match="after the requested"):
        replace(no_ranking, requested_window=WINDOW_A)
    share_model = build_overview(
        1,
        WINDOW_B,
        total_b,
        measured_series(WINDOW_A),
        measured_share(WINDOW_B),
        series_provenance=measured_provenance("series", SUBJECT_KEY, WINDOW_A),
        share_provenance=measured_provenance("totals", SUBJECT_KEY, WINDOW_B),
    )
    with pytest.raises(ValueError, match="after the requested"):
        replace(replace(share_model, ranking_provenance=None), requested_window=WINDOW_A)

    # Typed metadata is positive, associated and tier-consistent.
    with pytest.raises(ValueError, match="app_id"):
        replace(model, series_metadata=replace(model.series_metadata, app_id=2))
    with pytest.raises(ValueError, match="series metadata subject"):
        replace(
            model,
            series_metadata=replace(model.series_metadata, app_key="unit:/beta.service"),
        )
    with pytest.raises(ValueError, match="duration"):
        replace(
            model,
            series_metadata=replace(
                model.series_metadata,
                tier="rollup_60s",
                coverage=coverage(tier="rollup_60s"),
            ),
        )
    with pytest.raises(ValueError, match="tier"):
        replace(
            model,
            series_metadata=replace(
                model.series_metadata, coverage=coverage(tier="rollup_60s")
            ),
        )
    # A dependency's provenance must agree with the selected external subject.
    with pytest.raises(ValueError, match="external subject"):
        replace(
            model,
            share_provenance=measured_provenance(
                "totals", "unit:/beta.service", WINDOW_A
            ),
        )
    # A series provenance cannot outlive its typed metadata (the successful
    # chronology guard itself is isolated in its own test below).
    with pytest.raises(ValueError, match="series metadata"):
        replace(eligible, series_metadata=None)
    # A missing dependency cannot keep that dependency's provenance.
    with pytest.raises(ValueError, match="missing share"):
        replace(eligible, share=None, device_lanes=(), more_devices=0, reconciliation=None)

    # The frozen all-default direct shape stays a valid legacy model.
    legacy = OverviewModel(
        1,
        eligible.kpis,
        (),
        (),
        0,
        None,
        None,
        None,
        None,
        eligible.coverage_line,
        "window: since_boot; tier: raw",
    )
    assert legacy.requested_window is None
    assert legacy.app_key is None
    assert legacy.ranking_provenance is None
    assert legacy.series_provenance is None
    assert legacy.share_provenance is None
    assert legacy.device_lanes_unavailable is None
    assert legacy.series_metadata is None
    assert legacy.task_points == ()


def test_overview_direct_future_endpoint_rejected_without_ranking_provenance() -> None:
    """The endpoint guard rejects on its own, with no ranking row to mask it."""

    model = build_overview(
        1,
        WINDOW_B,
        app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None),
        measured_series(WINDOW_B),
        measured_share(WINDOW_A),
        series_provenance=measured_provenance("series", SUBJECT_KEY, WINDOW_B),
        share_provenance=measured_provenance("totals", SUBJECT_KEY, WINDOW_A),
    )
    no_ranking = replace(model, ranking_provenance=None)
    assert no_ranking.ranking_provenance is None
    assert no_ranking.device_lanes == () and no_ranking.more_devices == 0
    # B's series ends after A's requested endpoint. With the endpoint guard
    # removed this replacement would construct, so the failure is direct.
    with pytest.raises(ValueError, match="after the requested"):
        replace(no_ranking, requested_window=WINDOW_A)


def test_overview_direct_available_empty_series_requires_metadata_alone() -> None:
    """The metadata guard fails alone when no lane reason can mask it."""

    model = build_overview(
        1,
        WINDOW_A,
        app_total(1, SUBJECT_KEY, "Alpha", 22),
        measured_series(WINDOW_A),
        measured_share(WINDOW_A),
    )
    assert model.series_metadata is not None
    assert model.device_lanes and model.device_lanes_unavailable is None
    # An available-empty series keeps the lane reason that the missing series
    # measurement itself implies, and exposes no lanes or points, so only the
    # typed-metadata guard can reject this shape.
    with pytest.raises(ValueError, match="typed series metadata"):
        replace(
            model,
            ranking_provenance=None,
            task_points=(),
            series_provenance=None,
            series_metadata=None,
            device_lanes=(),
            more_devices=0,
            device_lanes_unavailable=SERIES_REASON,
        )


def test_build_overview_rejects_ranking_total_for_another_subject() -> None:
    """The subject identity must agree across the ranking and detail inputs."""

    with pytest.raises(ValueError, match="ranking total app_id 2"):
        build_overview(
            1,
            WINDOW_A,
            app_total(2, SUBJECT_KEY, "Alpha", 22),
            measured_series(WINDOW_A),
            measured_share(WINDOW_A),
        )


def test_overview_direct_early_outcome_rejects_task_points() -> None:
    """A subjectless early outcome cannot expose accepted task points."""

    file_only = build_overview(1, WINDOW_A, None, None, None)
    assert file_only.app_key is None
    assert file_only.series_metadata is None
    with pytest.raises(ValueError, match="task points"):
        replace(
            file_only,
            task_points=measured_series(WINDOW_A).points,
            series_unavailable=None,
        )


def test_overview_direct_lifetime_rejects_task_points() -> None:
    """Lifetime policy withholds chronology even when points are injected."""

    lifetime = Window(kind="lifetime")
    eternal = build_overview(
        1,
        lifetime,
        app_total(1, SUBJECT_KEY, "Alpha", 22),
        measured_series(lifetime),
        measured_share(lifetime, boot=BOOT_A),
    )
    assert eternal.series_unavailable == LIFETIME_REASON
    assert eternal.task_points == ()
    assert eternal.series_metadata is not None
    with pytest.raises(ValueError, match="lifetime chronology cannot project"):
        replace(
            eternal,
            task_points=measured_series(lifetime).points,
            series_unavailable=None,
        )


def test_new_share_device_unavailability_removes_old_device_values() -> None:
    total = app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)
    series = measured_series(WINDOW_A)
    available = build_overview(1, WINDOW_A, total, series, measured_share(WINDOW_A))
    assert available.kpis[4].text == format_bytes(14)
    assert [lane.dev for lane in available.device_lanes] == ["8:0"]
    assert available.more_devices == 0
    assert available.reconciliation is not None
    assert available.device_lanes_unavailable is None

    # Available -> incomplete: DEVICE, bars, residual strip and device lanes
    # clear in the same model application while task outputs stay measured.
    incomplete_share = measured_share(
        WINDOW_A, cov=coverage(complete=False, device_state="incomplete")
    )
    incomplete = build_overview(1, WINDOW_A, total, series, incomplete_share)
    assert incomplete.kpis[4].text == "unavailable"
    assert incomplete.device_lanes == () and incomplete.more_devices == 0
    assert incomplete.reconciliation is None
    assert incomplete.device_lanes_unavailable == "incomplete"
    assert [kpi.text for kpi in incomplete.kpis[:3]] == [
        format_bytes(22),
        format_bytes(17),
        format_bytes(5),
    ]
    assert incomplete.task_points == series.points
    assert incomplete.series_metadata == available.series_metadata
    assert incomplete.share is incomplete_share
    assert incomplete.share_provenance.measured_window == WINDOW_A
    assert "device lanes: incomplete" in incomplete.detail_line

    # A coverage reason wins over the raw state token.
    reasoned = build_overview(
        1,
        WINDOW_A,
        total,
        series,
        measured_share(
            WINDOW_A,
            cov=coverage(
                complete=False,
                device_state="incomplete",
                unavailable_reason="not_retained",
            ),
        ),
    )
    assert reasoned.device_lanes_unavailable == "not_retained"
    assert reasoned.device_lanes == () and reasoned.more_devices == 0

    # Available attribution with an undefined device total is the second
    # same-update case: DEVICE and bars clear, totals keep measuring.
    undefined_total = DiskShare(
        1, WINDOW_A, 17, 5, 22, (), None, None, None, None, None, None, coverage()
    )
    undefined = build_overview(1, WINDOW_A, total, series, undefined_total)
    assert undefined.kpis[4].text == "unavailable"
    assert undefined.device_lanes == () and undefined.more_devices == 0
    assert undefined.reconciliation is None
    assert undefined.device_lanes_unavailable == DEVICE_TOTAL_REASON
    assert [kpi.text for kpi in undefined.kpis[:3]] == [
        format_bytes(22),
        format_bytes(17),
        format_bytes(5),
    ]
    assert undefined.task_points == series.points
    assert undefined.series_metadata == available.series_metadata
    assert "device lanes: device total unavailable" in undefined.detail_line
    # The old device values are gone from the new model, not retained.
    assert format_bytes(14) not in [kpi.text for kpi in undefined.kpis]
    assert all(lane.dev != "8:0" for lane in undefined.device_lanes)


def test_mixed_endpoint_inputs_keep_independent_outputs() -> None:
    total = app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)
    series_a = measured_series(WINDOW_A)
    series_b = measured_series(WINDOW_B)
    share_a = measured_share(WINDOW_A)
    share_b = measured_share(
        WINDOW_B, task_read=19, task_write=7, device_read=11, device_write=5
    )

    # A retained share A with a newer requested series B.
    share_old = build_overview(
        1,
        WINDOW_B,
        total,
        series_b,
        share_a,
        series_provenance=measured_provenance(
            "series", SUBJECT_KEY, WINDOW_B, first_seen=3_000_000_000
        ),
        share_provenance=measured_provenance(
            "totals", SUBJECT_KEY, WINDOW_A, first_seen=1_000_000_000
        ),
    )
    assert share_old.requested_window == WINDOW_B
    assert share_old.share.window == WINDOW_A
    assert share_old.share_provenance.measured_window == WINDOW_A
    assert share_old.share_provenance.first_seen_at_ns == 1_000_000_000
    assert share_old.series_metadata.measured_window == WINDOW_B
    assert share_old.series_metadata.tier == "raw"
    assert share_old.task_points == series_b.points
    assert [kpi.text for kpi in share_old.kpis[:3]] == [
        format_bytes(22),
        format_bytes(17),
        format_bytes(5),
    ]
    assert share_old.kpis[4].text == format_bytes(14)
    assert share_old.reconciliation is not None
    assert share_old.device_lanes == () and share_old.more_devices == 0
    assert share_old.device_lanes_unavailable == DEVICE_ENDPOINTS_REASON
    assert (
        f"; share measured: since_boot {BOOT_A} through 20000000000 ns"
        in share_old.detail_line
    )
    assert "series measured:" not in share_old.detail_line
    assert share_old.detail_line.endswith(
        "; device lanes: device measurements have different endpoints"
    )

    # A retained series A with a newer requested share B.
    series_old = build_overview(
        1,
        WINDOW_B,
        total,
        series_a,
        share_b,
        series_provenance=measured_provenance(
            "series", SUBJECT_KEY, WINDOW_A, first_seen=1_000_000_000
        ),
        share_provenance=measured_provenance(
            "totals", SUBJECT_KEY, WINDOW_B, first_seen=3_000_000_000
        ),
    )
    assert series_old.share.window == WINDOW_B
    assert series_old.share_provenance.measured_window == WINDOW_B
    assert series_old.series_metadata.measured_window == WINDOW_A
    assert series_old.series_provenance.measured_window == WINDOW_A
    assert series_old.series_provenance.first_seen_at_ns == 1_000_000_000
    assert series_old.task_points == series_a.points
    assert [kpi.text for kpi in series_old.kpis[:3]] == [
        format_bytes(26),
        format_bytes(19),
        format_bytes(7),
    ]
    assert series_old.kpis[4].text == format_bytes(16)
    assert series_old.device_lanes == () and series_old.more_devices == 0
    assert series_old.device_lanes_unavailable == DEVICE_ENDPOINTS_REASON
    assert (
        f"; series measured: since_boot {BOOT_A} through 20000000000 ns"
        in series_old.detail_line
    )
    assert "share measured:" not in series_old.detail_line
    assert series_old.detail_line.endswith(
        "; device lanes: device measurements have different endpoints"
    )


# --------------------------------------------------------------------------
# §6.2-6.3 / §12.5 retained capability: the real exact-only adapter, identity
# reacceptance, authority reset and the query-free measurement notification.
# --------------------------------------------------------------------------


class ExactOnlyClock:
    """An injected MONOTONIC age clock in ns; never the wall clock."""

    def __init__(self, now_ns: int = 0) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance_ns(self, delta: int) -> None:
        self.now_ns += delta


class ExactOnlyQuery:
    """A structural three-method exact service with independent kind answers.

    It has no retained methods, no identity method, no store and no connection:
    the window's real private adapter owns selection, slot, record, failure and
    reset semantics. Results are keyed by ``(kind, app_id, window)`` so one
    kind/window can be pending or failed without disturbing the other, and
    every query/identity call is recorded.
    """

    def __init__(self) -> None:
        self.rows: list[AppTotal] = []
        self.results: dict[tuple[str, int, Window], object] = {}
        self.calls: dict[str, int] = {
            "ranking": 0,
            "series": 0,
            "totals": 0,
            "identity": 0,
        }
        self.events: list[tuple] = []
        self.ranking_error: BaseException | None = None

    # -- configuration ----------------------------------------------------

    def set_share(self, window: Window, value: DiskShare) -> None:
        self.results[("totals", value.app_id, window)] = value

    def set_series(self, window: Window, value: Series) -> None:
        self.results[("series", value.app_id, window)] = value

    def set_pending(self, kind: str, app_id: int, window: Window) -> None:
        self.results[(kind, app_id, window)] = HistoryPending("still loading")

    def set_error(
        self, kind: str, app_id: int, window: Window, error: BaseException
    ) -> None:
        self.results[(kind, app_id, window)] = error

    # -- the three exact getters ------------------------------------------

    def ranking(self, window: Window, metric: str) -> list[AppTotal]:
        self.calls["ranking"] += 1
        self.events.append(("ranking", window, metric))
        if self.ranking_error is not None:
            raise self.ranking_error
        return list(self.rows)

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        return self._exact("totals", app_id, window)

    def series(self, app_id: int, window: Window) -> Series:
        return self._exact("series", app_id, window)

    def _exact(self, kind: str, app_id: int, window: Window):
        self.calls[kind] += 1
        self.events.append(("get", kind, app_id, window))
        configured = self.results.get((kind, app_id, window))
        if isinstance(configured, BaseException):
            raise configured
        if configured is not None:
            return configured
        if kind == "series":
            return measured_series(window)
        return measured_share(window)


class IdentityExactOnlyQuery(ExactOnlyQuery):
    """Adds exactly the one optional ``exact_answer_identity`` callable."""

    def __init__(self) -> None:
        super().__init__()
        self.identities: dict[tuple[str, str, Window], object] = {}

    def exact_answer_identity(
        self, kind: str, app_key: str, window: Window
    ) -> object:
        self.calls["identity"] += 1
        self.events.append(("identity", kind, app_key, window))
        configured = self.identities.get((kind, app_key, window))
        if isinstance(configured, BaseException):
            raise configured
        return configured


class PartialRetainedQuery(ExactOnlyQuery):
    """Four of the five retained methods: a partial capability must fall back."""
    def __init__(self) -> None:
        super().__init__()
        self.retained_calls: list[tuple] = []

    def _unused(self, name: str, *args: object):
        self.retained_calls.append((name, args))
        raise AssertionError(f"a partial retained capability must not be used: {name}")

    def select_detail(self, app_key, window):
        return self._unused("select_detail", app_key, window)

    def request_detail(self, kind, app_id, window):
        return self._unused("request_detail", kind, app_id, window)

    def part_state(self, kind, app_key, scope):
        return self._unused("part_state", kind, app_key, scope)

    def retained(self, kind, app_key, scope, applied_endpoint_at):
        return self._unused("retained", kind, app_key, scope, applied_endpoint_at)


class RecordingRetainedQuery(ExactOnlyQuery):
    """The full five-method capability, recording its owner-side retirement."""

    def __init__(self) -> None:
        super().__init__()
        self.select_calls: list[tuple] = []
        self.retained_calls: list[tuple] = []
        self.records: dict[str, object] = {}
        self._selected: tuple[str | None, Window | None] = (None, None)
        self._requests = 0

    def select_detail(self, app_key, window) -> None:
        self.select_calls.append((app_key, window))
        self._selected = (app_key, window)

    def request_detail(self, kind, app_id, window):
        from appio.history_types import PartState

        self.retained_calls.append(("request_detail", kind, app_id, window))
        self._requests += 1
        key = self._selected[0]
        assert key is not None
        return PartState(
            kind,
            key,
            retention_scope(key, window),
            "pending",
            self._requests,
            window,
            None,
            None,
        )

    def part_state(self, kind, app_key, scope):
        from appio.history_types import PartState

        self.retained_calls.append(("part_state", kind, app_key, scope))
        return PartState(kind, app_key, scope, "absent", None, None, None, None)

    def retained(self, kind, app_key, scope, applied_endpoint_at):
        self.retained_calls.append(("retained", kind, app_key, scope))
        return self.records.get(kind)

    def ranking_provenance(self, app_key, window):
        self.retained_calls.append(("ranking_provenance", app_key, window))
        return None


class BrokenRetainedQuery(ExactOnlyQuery):
    """All five retained methods are callable and genuinely fail."""

    def _broken(self, name: str, *args: object):
        raise RuntimeError(f"retained capability failed: {name}")

    def select_detail(self, app_key, window):
        return self._broken("select_detail", app_key, window)

    def request_detail(self, kind, app_id, window):
        return self._broken("request_detail", kind, app_id, window)

    def part_state(self, kind, app_key, scope):
        return self._broken("part_state", kind, app_key, scope)

    def retained(self, kind, app_key, scope, applied_endpoint_at):
        return self._broken("retained", kind, app_key, scope, applied_endpoint_at)

    def ranking_provenance(self, app_key, window):
        return self._broken("ranking_provenance", app_key, window)


def test_pending_refresh_keeps_retained_dependency_values(qtbot) -> None:
    clock = ExactOnlyClock(1_000_000_000)
    service = IdentityExactOnlyQuery()
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    service.set_series(WINDOW_A, measured_series(WINDOW_A))
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "totals", SUBJECT_KEY, WINDOW_A, "b" * 64
    )
    service.identities[("series", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "series", SUBJECT_KEY, WINDOW_A, "c" * 64
    )
    view = AppIoWindow(
        service, WINDOW_A.boot_id, WINDOW_A.end_mono_ns, clock_ns=clock
    )
    qtbot.addWidget(view)
    view.show()

    accepted = view._overview_model
    total_widget = view._total_chart
    task_points = accepted.task_points
    signals: list[int] = []
    view.measurement_work_requested.connect(lambda: signals.append(1))
    assert task_points == measured_series(WINDOW_A).points
    assert accepted.share_provenance.measured_window == WINDOW_A
    assert accepted.share_provenance.first_seen_at_ns == 1_000_000_000
    assert accepted.series_provenance.measured_window == WINDOW_A

    # B is applied while both exact getters are still pending: the accepted A
    # dependency values, widget identity and stamps survive unchanged.
    clock.advance_ns(2_000_000_000)
    service.set_pending("totals", 1, WINDOW_B)
    service.set_pending("series", 1, WINDOW_B)
    view.set_window(WINDOW_B)

    # One admission-bearing selection emitted the work signal exactly once; a
    # query-free notification never does.
    assert signals == [1]

    model = view._overview_model
    assert model is not None
    assert model.requested_window == WINDOW_B
    assert model.share is not None and model.share.window == WINDOW_A
    assert model.share_provenance.measured_window == WINDOW_A
    assert model.share_provenance.first_seen_at_ns == 1_000_000_000
    assert model.series_provenance.measured_window == WINDOW_A
    assert model.series_provenance.first_seen_at_ns == 1_000_000_000
    assert model.task_points == task_points
    # Pending is not an error: neither dependency carries a reason, and the
    # chart is not repainted as unavailable over the retained task lane.
    assert model.series_unavailable is None
    assert model.share_unavailable is None
    assert "still loading" not in model.detail_line
    assert view._total_chart is total_widget
    assert total_widget.is_available() is True
    assert total_widget.unavailable_reason() is None
    assert total_widget.columns() != ()
    assert [kpi.text for kpi in model.kpis[:3]] == [
        format_bytes(22),
        format_bytes(17),
        format_bytes(5),
    ]
    assert model.kpis[3].text == "unavailable"  # undefined PEAK is not zero
    assert model.kpis[4].text == format_bytes(14)
    assert (
        f"; share measured: since_boot {BOOT_A} through 20000000000 ns"
        in model.detail_line
    )
    assert (
        f"; series measured: since_boot {BOOT_A} through 20000000000 ns"
        in model.detail_line
    )
    # One observation per admitted kind for the newly applied Window.
    assert [
        event for event in service.events if event[0] == "get" and event[3] == WINDOW_B
    ] == [
        ("get", "series", 1, WINDOW_B),
        ("get", "totals", 1, WINDOW_B),
    ]
    view.refresh_measurements()
    assert signals == [1]


def test_exact_only_adapter_matches_retention_failure_and_reset_rules(
    qtbot,
) -> None:
    clock = ExactOnlyClock(1_000_000_000)
    service = ExactOnlyQuery()
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    service.set_series(WINDOW_A, measured_series(WINDOW_A))
    view = AppIoWindow(
        service, WINDOW_A.boot_id, WINDOW_A.end_mono_ns, clock_ns=clock
    )
    qtbot.addWidget(view)
    view.show()
    adapter = view._detail_service
    assert adapter is not service

    # A plain exact success retains the measured Window, but no identity or
    # stamp: age is unknown rather than guessed.
    accepted = view._overview_model
    assert accepted.share is not None and accepted.share.task_total_bytes == 22
    assert accepted.share_provenance.measured_window == WINDOW_A
    assert accepted.share_provenance.accepted_identity is None
    assert accepted.share_provenance.first_seen_at_ns is None
    assert accepted.share_provenance.answered_source is None
    assert accepted.share_provenance.local_bracket is None

    # A successful exact result that contradicts its selector is failed before
    # acceptance as invalid_result; the other dependency is not erased.
    service.results[("totals", 1, WINDOW_A)] = measured_share(WINDOW_B)
    view.refresh()
    refused = view._overview_model
    assert refused.share is None
    assert refused.share_unavailable == "invalid_result"
    assert refused.series_metadata is not None
    service.results[("series", 1, WINDOW_A)] = replace(
        measured_series(WINDOW_A), app_id=2
    )
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    view.refresh()
    mismatched = view._overview_model
    assert mismatched.series_metadata is None
    assert mismatched.series_unavailable == "invalid_result"
    assert mismatched.share is not None
    service.set_series(WINDOW_A, measured_series(WINDOW_A))
    view.refresh()
    assert view._overview_model.series_metadata is not None

    # A forward applied Window with both getters pending keeps A effective and
    # reports pending through the slot, never a failure.
    service.set_pending("totals", 1, WINDOW_B)
    service.set_pending("series", 1, WINDOW_B)
    view.set_window(WINDOW_B)
    scope_b = retention_scope(SUBJECT_KEY, WINDOW_B)
    endpoint_b = WINDOW_B.end_mono_ns
    retained_a = adapter.retained("totals", SUBJECT_KEY, scope_b, endpoint_b)
    assert retained_a is not None
    assert retained_a.provenance.measured_window == WINDOW_A
    assert adapter.part_state("totals", SUBJECT_KEY, scope_b).state == "pending"
    assert view._overview_model.share is not None
    assert view._overview_model.share_unavailable is None

    # A current failure clears the record before notification and keeps its own
    # exact reason; the pending series stays an independent retained value.
    service.set_error("totals", 1, WINDOW_B, RuntimeError("share_bad"))
    view.refresh()
    failed = view._overview_model
    assert failed.share is None and failed.share_unavailable == "share_bad"
    failed_state = adapter.part_state("totals", SUBJECT_KEY, scope_b)
    assert failed_state.state == "failed"
    assert failed_state.reason == "share_bad"
    assert adapter.retained("totals", SUBJECT_KEY, scope_b, endpoint_b) is None
    assert failed.series_metadata is not None
    assert failed.series_metadata.measured_window == WINDOW_A

    # A pending retry cannot restore the failed record.
    service.set_pending("totals", 1, WINDOW_B)
    view.refresh()
    retried = view._overview_model
    assert retried.share is None
    assert retried.share_unavailable == SHARE_UNAVAILABLE_REASON
    assert adapter.retained("totals", SUBJECT_KEY, scope_b, endpoint_b) is None
    assert adapter.part_state("totals", SUBJECT_KEY, scope_b).state == "pending"

    # Only a new accepted success restores a measured record.
    service.set_share(
        WINDOW_B,
        measured_share(
            WINDOW_B, task_read=19, task_write=7, device_read=11, device_write=5
        ),
    )
    view.refresh()
    restored = view._overview_model
    assert restored.share is not None and restored.share.window == WINDOW_B
    assert restored.share_provenance.measured_window == WINDOW_B

    # A pending observation keeps its own exact Window while a newer Window is
    # applied: the same A getter is observed once per ordinary refresh (never
    # abandoned for B), and only one latest successor B is remembered.
    between = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=25_000_000_000)
    adapter.select_detail(SUBJECT_KEY, between)
    service.set_pending("totals", 1, between)
    adapter.request_detail("totals", 1, between)
    assert service.events[-1] == ("get", "totals", 1, between)
    adapter.select_detail(SUBJECT_KEY, WINDOW_B)
    adapter.request_detail("totals", 1, WINDOW_B)
    assert service.events[-1] == ("get", "totals", 1, between)
    between_scope = retention_scope(SUBJECT_KEY, between)
    assert adapter.part_state("totals", SUBJECT_KEY, between_scope).state == "pending"
    # Settling A publishes its own measured Window; the successor is not
    # discarded, mutated into B or used to relabel A.
    service.set_share(between, measured_share(between))
    adapter.request_detail("totals", 1, WINDOW_B)
    assert service.events[-1] == ("get", "totals", 1, between)
    settled_a = adapter.retained(
        "totals", SUBJECT_KEY, retention_scope(SUBJECT_KEY, WINDOW_B), endpoint_b
    )
    assert settled_a is not None
    assert settled_a.provenance.measured_window == between
    # The next ordinary call admits the remembered successor B.
    adapter.request_detail("totals", 1, WINDOW_B)
    assert service.events[-1] == ("get", "totals", 1, WINDOW_B)

    # A backward endpoint, a changed subject and the both-None retirement all
    # retire records synchronously and issue no query.
    total_calls = service.calls["totals"]
    back = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=15_000_000_000)
    adapter.select_detail(SUBJECT_KEY, back)
    assert (
        adapter.retained(
            "totals", SUBJECT_KEY, retention_scope(SUBJECT_KEY, back), back.end_mono_ns
        )
        is None
    )
    adapter.select_detail("unit:/other.service", WINDOW_A)
    assert adapter.retained("totals", SUBJECT_KEY, scope_b, endpoint_b) is None
    adapter.select_detail(None, None)
    assert adapter.part_state("totals", SUBJECT_KEY, scope_b).state == "absent"
    assert service.calls["totals"] == total_calls

    # A partial capability falls back to the real adapter; none of the four
    # supplied retained methods is ever called and the exact values render.
    partial = PartialRetainedQuery()
    partial.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    partial.set_share(WINDOW_A, measured_share(WINDOW_A))
    partial.set_series(WINDOW_A, measured_series(WINDOW_A))
    partial_view = AppIoWindow(
        partial, WINDOW_A.boot_id, WINDOW_A.end_mono_ns, clock_ns=clock
    )
    qtbot.addWidget(partial_view)
    partial_view.show()
    assert partial_view._detail_service is not partial
    assert partial.retained_calls == []
    assert partial_view._overview_model.share is not None
    assert partial_view._overview_model.share_provenance.accepted_identity is None

    # A full retained capability keeps its own retirement: a reset calls its
    # select_detail(None, None) and never an adapter-only method.
    recording = RecordingRetainedQuery()
    recording.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    retained_view = AppIoWindow(
        recording, WINDOW_A.boot_id, WINDOW_A.end_mono_ns, clock_ns=clock
    )
    qtbot.addWidget(retained_view)
    retained_view.show()
    assert retained_view._detail_service is recording
    assert (SUBJECT_KEY, WINDOW_A) in recording.select_calls
    selections = list(recording.select_calls)
    queries = dict(recording.calls)
    retained_view.reset_query_authority()
    assert recording.select_calls == selections + [(None, None)]
    assert recording.calls == queries
    assert retained_view._exact_authority_epoch == 2

    # A builder refusal of one dependency's observation removes only that
    # dependency; the independently valid other input still renders.
    from appio.history_types import RetainedRecord

    contradictory = RecordingRetainedQuery()
    contradictory.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    future = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=31_000_000_000)
    contradictory.records["series"] = RetainedRecord(
        measured_series(WINDOW_A), measured_provenance("series", SUBJECT_KEY, WINDOW_A)
    )
    contradictory.records["totals"] = RetainedRecord(
        measured_share(future),
        measured_provenance("totals", SUBJECT_KEY, future),
    )
    contradictory_view = AppIoWindow(
        contradictory, WINDOW_A.boot_id, WINDOW_A.end_mono_ns, clock_ns=clock
    )
    qtbot.addWidget(contradictory_view)
    contradictory_view.show()
    kept = contradictory_view._overview_model
    assert kept.share is None
    assert kept.share_unavailable is not None
    assert kept.share_unavailable != SHARE_UNAVAILABLE_REASON
    assert kept.series_metadata is not None
    assert kept.task_points == measured_series(WINDOW_A).points

    # A full capability whose method raises is a real failure, never silent
    # fallback to an adapter.
    broken = BrokenRetainedQuery()
    broken.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    broken.set_share(WINDOW_A, measured_share(WINDOW_A))
    broken.set_series(WINDOW_A, measured_series(WINDOW_A))
    with pytest.raises(RuntimeError, match="retained capability failed"):
        AppIoWindow(
            broken, WINDOW_A.boot_id, WINDOW_A.end_mono_ns, clock_ns=clock
        )


def test_exact_only_identity_reacceptance_reuses_stamp(qtbot) -> None:
    clock = ExactOnlyClock(1_000_000_000)
    service = IdentityExactOnlyQuery()
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    service.set_series(WINDOW_A, measured_series(WINDOW_A))
    identity = AcceptedIdentity("totals", SUBJECT_KEY, WINDOW_A, "b" * 64)
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = identity
    view = AppIoWindow(
        service, WINDOW_A.boot_id, WINDOW_A.end_mono_ns, clock_ns=clock
    )
    qtbot.addWidget(view)
    view.show()

    def identity_events() -> list[tuple]:
        return [
            event
            for event in service.events
            if event[0] == "identity" and event[1] == "totals"
        ]

    # Exactly one identity call, immediately after the successful totals getter
    # and before any other getter or adoption.
    events = list(service.events)
    assert [event[0] for event in events[:6]] == [
        "ranking",
        "identity",
        "get",
        "identity",
        "get",
        "identity",
    ]
    assert events[1] == ("identity", "ranking", SUBJECT_KEY, WINDOW_A)
    getter_at = events.index(("get", "totals", 1, WINDOW_A))
    assert events[getter_at + 1] == (
        "identity",
        "totals",
        SUBJECT_KEY,
        WINDOW_A,
    )
    assert identity_events() == [("identity", "totals", SUBJECT_KEY, WINDOW_A)]
    accepted = view._overview_model
    assert accepted.share_provenance.accepted_identity == identity
    assert accepted.share_provenance.first_seen_at_ns == 1_000_000_000
    assert accepted.series_provenance.accepted_identity is None

    # Reaccepting the identical identity at t=3 s reuses its 1 s stamp: the age
    # is 2 s in the one injected clock domain.
    clock.advance_ns(2_000_000_000)
    view.refresh()
    again = view._overview_model
    assert again.share is not None and again.share.task_total_bytes == 22
    assert again.share_provenance.accepted_identity == identity
    assert again.share_provenance.first_seen_at_ns == 1_000_000_000
    assert clock.now_ns - again.share_provenance.first_seen_at_ns == 2_000_000_000
    assert service.calls["totals"] == 2

    # A malformed, contradicting or raising identity fails only that current
    # dependency as invalid_result; the independent series stays accepted.
    contradictions: tuple[object, ...] = (
        AcceptedIdentity("series", SUBJECT_KEY, WINDOW_A, "c" * 64),
        AcceptedIdentity("totals", "unit:/other.service", WINDOW_A, "d" * 64),
        AcceptedIdentity("totals", SUBJECT_KEY, WINDOW_B, "e" * 64),
        SimpleNamespace(
            kind="totals",
            app_key=SUBJECT_KEY,
            window=WINDOW_A,
            data_digest="not-a-digest",
        ),
        RuntimeError("identity capability failed"),
    )
    for supplied in contradictions:
        service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = supplied
        view.refresh()
        rejected = view._overview_model
        assert rejected.share is None
        assert rejected.share_unavailable == "invalid_result"
        assert rejected.series_metadata is not None
        assert rejected.series_provenance.measured_window == WINDOW_A

    # A valid None identity remains identity-less and unknown.
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = None
    view.refresh()
    identity_less = view._overview_model
    assert identity_less.share is not None
    assert identity_less.share_provenance.accepted_identity is None
    assert identity_less.share_provenance.first_seen_at_ns is None

    # Pending and failing getters never ask the optional capability, and the
    # query-free notification asks nothing at all.
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = identity
    before_pending = len(identity_events())
    service.set_pending("totals", 1, WINDOW_B)
    service.set_pending("series", 1, WINDOW_B)
    view.set_window(WINDOW_B)
    assert len(identity_events()) == before_pending
    service.set_error("totals", 1, WINDOW_B, RuntimeError("share_bad"))
    view.refresh()
    assert len(identity_events()) == before_pending
    calls = dict(service.calls)
    event_log = list(service.events)
    view.refresh_measurements()
    assert service.calls == calls
    assert service.events == event_log

    # A malformed ranking identity uses the ranking's normal error path and no
    # row, subject or stale PEAK survives it.
    service.identities[("ranking", SUBJECT_KEY, WINDOW_A)] = SimpleNamespace(
        kind="ranking", app_key=SUBJECT_KEY, window=WINDOW_A, data_digest="bad"
    )
    view.set_window(WINDOW_A)
    assert view.selected_app_id() is None
    assert listing(view).count() == 0
    assert view._total_chart.unavailable_reason() == "invalid_result"
    service.identities[("ranking", SUBJECT_KEY, WINDOW_A)] = None


def test_exact_only_authority_reset_retires_unchanged_lifetime(qtbot) -> None:
    clock = ExactOnlyClock(1_000_000_000)
    lifetime = Window(kind="lifetime")
    service = IdentityExactOnlyQuery()
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    service.set_share(lifetime, measured_share(lifetime))
    service.set_pending("series", 1, lifetime)
    first_identity = AcceptedIdentity("totals", SUBJECT_KEY, lifetime, "b" * 64)
    service.identities[("totals", SUBJECT_KEY, lifetime)] = first_identity
    view = AppIoWindow(service, clock_ns=clock)
    qtbot.addWidget(view)
    view.show()
    view.set_window(lifetime)
    scope = retention_scope(SUBJECT_KEY, lifetime)
    adapter = view._detail_service

    accepted = view._overview_model
    assert accepted.share is not None and accepted.share.task_total_bytes == 22
    assert accepted.share_provenance.measured_window == lifetime
    assert accepted.share_provenance.first_seen_at_ns == 1_000_000_000
    assert view._exact_authority_epoch == 1
    assert adapter.retained("totals", SUBJECT_KEY, scope, None) is not None
    stale_series = adapter.part_state("series", SUBJECT_KEY, scope)
    assert stale_series.state == "pending"
    assert stale_series.request_id is not None

    # An authority reset at t=3 s retires records, stamps and the selection for
    # the unchanged lifetime Window, issues no query and changes no shell field.
    clock.advance_ns(2_000_000_000)
    calls_before = dict(service.calls)
    view.reset_query_authority()
    assert view._exact_authority_epoch == 2
    assert service.calls == calls_before
    assert adapter.retained("totals", SUBJECT_KEY, scope, None) is None
    assert adapter.part_state("totals", SUBJECT_KEY, scope).state == "absent"
    assert adapter.part_state("series", SUBJECT_KEY, scope).state == "absent"
    with pytest.raises(ValueError):
        adapter.reset_authority(2)
    with pytest.raises(ValueError):
        adapter.reset_authority(0)

    # The query-free notification publishes zero old values and asks nothing.
    view.refresh_measurements()
    assert service.calls == calls_before
    stale = view._overview_model
    assert stale.share is None and stale.share_provenance is None
    assert stale.series_metadata is None and stale.series_provenance is None
    assert stale.share_unavailable == SHARE_UNAVAILABLE_REASON
    assert view._total_chart.is_available() is False

    # Adopting the same lifetime at epoch 2 establishes a fresh lifetime; a new
    # accepted success alone restores values, stamped at t=4 s with age 0 s.
    clock.advance_ns(1_000_000_000)
    second_identity = AcceptedIdentity("totals", SUBJECT_KEY, lifetime, "f" * 64)
    service.set_share(
        lifetime,
        measured_share(
            lifetime, task_read=19, task_write=7, device_read=11, device_write=5
        ),
    )
    service.identities[("totals", SUBJECT_KEY, lifetime)] = second_identity
    totals_before = service.calls["totals"]
    view.refresh()
    restored = view._overview_model
    assert service.calls["totals"] == totals_before + 1
    assert restored.share is not None and restored.share.task_total_bytes == 26
    assert restored.share_provenance.accepted_identity == second_identity
    assert restored.share_provenance.first_seen_at_ns == 4_000_000_000
    assert clock.now_ns - restored.share_provenance.first_seen_at_ns == 0
    # The series stayed pending across the reset: no restored record/age.
    assert restored.series_metadata is None
    assert restored.task_points == ()
    assert restored.series_provenance is None

    # A same-authority forward refresh is not a reset and reuses the stamp.
    epoch = view._exact_authority_epoch
    view.refresh()
    assert view._exact_authority_epoch == epoch
    assert service.calls["totals"] == totals_before + 2
    assert view._overview_model.share_provenance.first_seen_at_ns == 4_000_000_000


def test_detail_notification_is_query_free_and_preserves_other_dependency(
    qtbot,
) -> None:
    clock = ExactOnlyClock(1_000_000_000)
    service = IdentityExactOnlyQuery()
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    service.set_series(WINDOW_A, measured_series(WINDOW_A))
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "totals", SUBJECT_KEY, WINDOW_A, "b" * 64
    )
    service.identities[("series", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "series", SUBJECT_KEY, WINDOW_A, "c" * 64
    )
    files = FakeFilesHistory(ranking_rows=(service.rows[0],))
    view = AppIoWindow(
        service,
        WINDOW_A.boot_id,
        WINDOW_A.end_mono_ns,
        files_view=FilesView(files),
        clock_ns=clock,
    )
    qtbot.addWidget(view)
    view.show()

    accepted = view._overview_model
    assert accepted.share is not None and accepted.series_metadata is not None
    chart, donut, bars = view._total_chart, view._donut, view._share_bars

    # A query-free notification rereads states/records only: no ranking, no
    # detail admission, no Files request, no identity probe, no widget rebuild.
    calls = dict(service.calls)
    event_log = list(service.events)
    file_requests = len(files.requests)
    view.refresh_measurements()
    assert service.calls == calls
    assert service.events == event_log
    assert len(files.requests) == file_requests
    same = view._overview_model
    assert view._total_chart is chart
    assert view._donut is donut
    assert view._share_bars is bars
    assert same.share == accepted.share
    assert same.share_provenance == accepted.share_provenance
    assert same.series_provenance == accepted.series_provenance
    assert same.task_points == accepted.task_points

    # A newer applied Window keeps the accepted A share while its own detail is
    # pending; the current series failure then lands through the real adapter.
    service.set_share(WINDOW_B, measured_share(WINDOW_B))
    service.set_pending("totals", 1, WINDOW_B)
    service.set_pending("series", 1, WINDOW_B)
    view.set_window(WINDOW_B)
    assert view._overview_model.share.window == WINDOW_A
    assert view._overview_model.share_provenance.first_seen_at_ns == 1_000_000_000
    service.set_error("series", 1, WINDOW_B, RuntimeError("series_bad"))
    view._detail_service.request_detail("series", 1, WINDOW_B)

    # The notification applies that failure immediately and preserves the
    # independently accepted share and every existing widget.
    failed_calls = dict(service.calls)
    view.refresh_measurements()
    assert service.calls == failed_calls
    failed = view._overview_model
    assert failed.series_unavailable == "series_bad"
    assert failed.series_metadata is None and failed.task_points == ()
    assert failed.series_provenance is None
    assert failed.share is not None and failed.share.window == WINDOW_A
    assert failed.share_provenance.measured_window == WINDOW_A
    assert failed.share_provenance.first_seen_at_ns == 1_000_000_000
    assert [kpi.text for kpi in failed.kpis[:3]] == [
        format_bytes(22),
        format_bytes(17),
        format_bytes(5),
    ]
    assert view._donut is donut and donut.shares() is not None
    assert view._share_bars is bars and bars.fractions() != ()
    assert view._total_chart is chart
    assert chart.is_available() is False
    assert chart.unavailable_reason() == "series_bad"
    # Pending is not failure: the share's newer slot is still pending, and no
    # pending text was rendered as an error.
    assert (
        view._detail_service.part_state(
            "totals", SUBJECT_KEY, retention_scope(SUBJECT_KEY, WINDOW_B)
        ).state
        == "pending"
    )
    assert "still loading" not in failed.detail_line


# --------------------------------------------------------------------------
# 12.7 Age and the independent label (spec §9.1–§9.3, plan Task 11).
# --------------------------------------------------------------------------

WINDOW_40 = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=40_000_000_000)


def test_first_seen_age_uses_raw_thresholds_and_ceiling_in_every_unit() -> None:
    from dataclasses import fields as dataclass_fields

    # The pure records carry exactly these fields (spec §9.1): no second
    # displayed-measurement counter, no widget and no query handle.
    assert [field.name for field in dataclass_fields(MeasurementStatus)] == [
        "has_displayed_values",
        "ranking_empty",
        "share_state",
        "series_state",
        "share_reason",
        "series_reason",
        "displayed_answers",
    ]
    assert [field.name for field in dataclass_fields(PresentationRefusal)] == [
        "origin",
        "window",
        "reason",
        "text",
    ]
    assert [field.name for field in dataclass_fields(OverviewStatusChoice)] == [
        "row",
        "text",
        "age_text",
    ]

    p = measured_provenance(
        "totals", SUBJECT_KEY, WINDOW_A, first_seen=1_000_000_000
    )
    assert p.accepted_identity is not None and p.first_seen_at_ns == 1_000_000_000

    # The exact §9.3 oracle: raw-ns threshold, then integer ceiling per unit.
    vectors = [
        (0, "0 s"),
        (1, "1 s"),
        (999_999_999, "1 s"),
        (89_999_000_000, "90 s"),
        (90_000_000_000, "2 min"),
        (5_399_940_000_000, "90 min"),
        (5_400_000_000_000, "2 h"),
    ]
    for delta, text in vectors:
        assert oldest_answer_age((p,), p.first_seen_at_ns + delta) == (
            f"oldest answer {text} ago"
        )
    assert oldest_answer_age((), 0) is None
    assert oldest_answer_age(
        (replace(p, accepted_identity=None, first_seen_at_ns=None),), 0
    ) == "age unknown"
    assert oldest_answer_age((p,), 0) == "oldest answer 0 s ago"
    # max(0, ...) defensively handles a test clock behind the stamp.
    stamped_ahead = replace(p, first_seen_at_ns=5_000_000_000)
    assert oldest_answer_age((stamped_ahead,), 0) == "oldest answer 0 s ago"
    for now in (True, -1, 1.5, "0", None):
        with pytest.raises(ValueError):
            oldest_answer_age((p,), now)  # type: ignore[arg-type]
    for answers in ((object(),), (p, "not provenance")):
        with pytest.raises(ValueError):
            oldest_answer_age(answers, 0)  # type: ignore[arg-type]

    # MeasurementStatus: exact bools, phases, reasons only for failure, and the
    # fixed distinct dependency order ranking, totals, series.
    base = MeasurementStatus(False, False, "absent", "absent", None, None, ())
    assert base.displayed_answers == ()
    series_prov = measured_provenance(
        "series", SUBJECT_KEY, WINDOW_A, first_seen=3_000_000_000
    )
    ranking_prov = measured_provenance(
        "ranking", SUBJECT_KEY, WINDOW_A, first_seen=1_000_000_000
    )
    ordered = MeasurementStatus(
        True, False, "ok", "ok", None, None,
        (ranking_prov, p, series_prov),
    )
    assert ordered.displayed_answers == (ranking_prov, p, series_prov)
    failed = MeasurementStatus(
        False, False, "failed", "failed", "share_bad", "series_bad", ()
    )
    assert (failed.share_reason, failed.series_reason) == ("share_bad", "series_bad")
    for invalid in (
        (1, False, "absent", "absent", None, None, ()),
        (False, "no", "absent", "absent", None, None, ()),
        (False, False, "ready", "absent", None, None, ()),
        (False, False, "failed", "absent", None, None, ()),
        (False, False, "ok", "absent", "unexpected", None, ()),
        (False, False, "absent", "failed", None, None, ()),
        (False, False, "absent", "ok", None, "unexpected", ()),
        (False, False, "absent", "absent", None, None, [p]),
        (False, False, "absent", "absent", None, None, (p, object())),
        (False, False, "absent", "absent", None, None, (p, p)),
        (False, False, "absent", "absent", None, None, (p, ranking_prov)),
        (False, False, "absent", "absent", None, None, (series_prov, p)),
    ):
        with pytest.raises(ValueError):
            MeasurementStatus(*invalid)  # type: ignore[arg-type]

    # PresentationRefusal: legal origin, exact Window and nonempty text.
    refusal = PresentationRefusal(
        "shell_prepare", WINDOW_A, "launch_attribution", A22_LAUNCH_TEXT
    )
    assert refusal.window == WINDOW_A and refusal.text == A22_LAUNCH_TEXT
    for invalid in (
        ("direct", WINDOW_A, "launch_attribution", A22_LAUNCH_TEXT),
        ("direct_view", "not-a-window", "launch_attribution", A22_LAUNCH_TEXT),
        ("direct_view", WINDOW_A, "", A22_LAUNCH_TEXT),
        ("direct_view", WINDOW_A, None, A22_LAUNCH_TEXT),
        ("direct_view", WINDOW_A, 7, A22_LAUNCH_TEXT),
        ("direct_view", WINDOW_A, "launch_attribution", ""),
        ("direct_view", WINDOW_A, "launch_attribution", None),
        ("direct_view", WINDOW_A, "launch_attribution", 7),
    ):
        with pytest.raises(ValueError):
            PresentationRefusal(*invalid)  # type: ignore[arg-type]

    # OverviewStatusChoice: legal row IDs, row 6f iff the status text is empty,
    # and only the exact §9.3 age label shapes.
    for row in ("1", "2", "3", "4", "5a", "5b", "6a", "6b", "6c", "6d", "6e"):
        assert OverviewStatusChoice(row, "status", None).row == row
    hidden = OverviewStatusChoice("6f", "", None)
    assert (hidden.row, hidden.text, hidden.age_text) == ("6f", "", None)
    for age in (
        "age unknown",
        "oldest answer 0 s ago",
        "oldest answer 90 s ago",
        "oldest answer 2 min ago",
        "oldest answer 2 h ago",
    ):
        assert OverviewStatusChoice("6b", "live", age).age_text == age
    for invalid in (
        ("6f", "nonempty", None),
        ("6b", "", None),
        ("7", "status", None),
        ("5a", "status", 2),
        ("6b", "live", "2 s"),
        ("6b", "live", ""),
        ("6b", "live", "oldest answer 2 seconds ago"),
        ("6b", "live", "oldest answer -1 s ago"),
        ("6b", "live", "oldest answer 02 s ago"),
        ("6b", "live", "oldest answer  2 s ago"),
    ):
        with pytest.raises(ValueError):
            OverviewStatusChoice(*invalid)  # type: ignore[arg-type]

    # The one pure tree validates every input before choosing, and its row 6f
    # carries the empty status text that hides the label.
    absent = MeasurementStatus(False, False, "absent", "absent", None, None, ())
    inputs = dict(
        refusal=None,
        prepare_error=None,
        busy=False,
        retry_after=None,
        now_ns=40_000_000_000,
        has_frame=True,
        has_target=True,
        has_sample=True,
        any_work=False,
        view_state="ready",
        measurement=absent,
        age_now_ns=40_000_000_000,
    )
    choice = _choose_overview_status(**inputs)
    assert (choice.row, choice.text, choice.age_text) == ("6f", "", None)
    for override in (
        {"refusal": object()},
        {"prepare_error": ""},
        {"busy": 1},
        {"retry_after": -1},
        {"now_ns": True},
        {"has_frame": "yes"},
        {"has_target": 0},
        {"has_sample": None},
        {"any_work": 1},
        {"view_state": ""},
        {"measurement": object()},
        {"age_now_ns": -1},
    ):
        with pytest.raises(ValueError):
            _choose_overview_status(**{**inputs, **override})


def test_accepted_identity_includes_exact_selector_despite_same_snapshot(
    qtbot,
) -> None:
    # -- Remote exact answers: the same snapshot under another exact Window is
    # a distinct accepted identity with its own first-seen stamp. -------------
    clock = ExactOnlyClock(1_000_000_000)
    service = IdentityExactOnlyQuery()
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    service.set_pending("series", 1, WINDOW_A)
    identity_a = AcceptedIdentity("totals", SUBJECT_KEY, WINDOW_A, "b" * 64)
    # The identical result payload at a different exact selector is a distinct
    # identity even though its data digest (snapshot) is byte-identical.
    identity_b = AcceptedIdentity("totals", SUBJECT_KEY, WINDOW_B, "b" * 64)
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = identity_a
    service.identities[("totals", SUBJECT_KEY, WINDOW_B)] = identity_b
    view = AppIoWindow(service, clock_ns=clock)
    qtbot.addWidget(view)
    view.show()
    view.set_window(WINDOW_A)

    first = view._overview_model.share_provenance
    assert first.accepted_identity == identity_a
    assert first.first_seen_at_ns == 1_000_000_000
    status = view.measurement_status()
    assert [answer.kind for answer in status.displayed_answers] == ["totals"]
    assert oldest_answer_age(status.displayed_answers, clock.now_ns) == (
        "oldest answer 0 s ago"
    )

    # Reapplying the accepted record without a query never restamps it.
    view.refresh_measurements()
    reapplied = view._overview_model.share_provenance
    assert reapplied.accepted_identity == identity_a
    assert reapplied.first_seen_at_ns == 1_000_000_000

    # Re-observing the same exact selector at t=3 s reuses the 1 s stamp.
    clock.advance_ns(2_000_000_000)
    view.refresh()
    repeated = view._overview_model.share_provenance
    assert repeated.accepted_identity == identity_a
    assert repeated.first_seen_at_ns == 1_000_000_000
    assert clock.now_ns - repeated.first_seen_at_ns == 2_000_000_000

    # The changed Window keeps the same snapshot digest yet creates a distinct
    # identity, stamped at its own first acceptance (t=4 s).
    clock.advance_ns(1_000_000_000)
    service.set_share(WINDOW_B, measured_share(WINDOW_B))
    service.set_pending("series", 1, WINDOW_B)
    view.set_window(WINDOW_B)
    assert identity_a != identity_b
    assert identity_a.data_digest == identity_b.data_digest
    changed = view._overview_model.share_provenance
    assert changed.accepted_identity == identity_b
    assert changed.accepted_identity.window == WINDOW_B
    assert changed.first_seen_at_ns == 4_000_000_000

    # While the forward Window's own detail is still pending, the accepted B
    # record and both stamps survive an ordinary refresh and a notification.
    clock.advance_ns(1_000_000_000)
    service.set_pending("totals", 1, WINDOW_40)
    service.set_pending("series", 1, WINDOW_40)
    view.set_window(WINDOW_40)
    pending = view._overview_model.share_provenance
    assert pending.accepted_identity == identity_b
    assert pending.first_seen_at_ns == 4_000_000_000
    part = view._detail_service.part_state(
        "totals", SUBJECT_KEY, retention_scope(SUBJECT_KEY, WINDOW_40)
    )
    assert part.state == "pending" and part.request_id is not None
    view.refresh()
    view.refresh_measurements()
    still = view._overview_model.share_provenance
    assert still.accepted_identity == identity_b
    assert still.first_seen_at_ns == 4_000_000_000
    assert view._detail_service.part_state(
        "totals", SUBJECT_KEY, retention_scope(SUBJECT_KEY, WINDOW_40)
    ).request_id == part.request_id

    # -- Local retained answers: reapplication preserves the provenance stamp;
    # a distinct exact selector with the same digest has a distinct stamp. ----
    local = RecordingRetainedQuery()
    local.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    record_a = RetainedRecord(
        measured_share(WINDOW_A),
        measured_provenance(
            "totals", SUBJECT_KEY, WINDOW_A, first_seen=1_000_000_000
        ),
    )
    record_c = RetainedRecord(
        measured_share(WINDOW_40),
        measured_provenance(
            "totals", SUBJECT_KEY, WINDOW_40, first_seen=4_000_000_000
        ),
    )
    local.records["totals"] = record_a
    local_clock = ExactOnlyClock(10_000_000_000)
    local_view = AppIoWindow(local, clock_ns=local_clock)
    qtbot.addWidget(local_view)
    local_view.show()
    local_view.set_window(WINDOW_40)
    local_first = local_view._overview_model.share_provenance
    assert local_first.measured_window == WINDOW_A
    assert local_first.first_seen_at_ns == 1_000_000_000
    calls = dict(local.calls)
    local_view.refresh_measurements()
    local_again = local_view._overview_model.share_provenance
    assert local_again.accepted_identity == local_first.accepted_identity
    assert local_again.first_seen_at_ns == 1_000_000_000
    assert local.calls == calls

    local.records["totals"] = record_c
    local_view.refresh_measurements()
    local_changed = local_view._overview_model.share_provenance
    assert local_changed.measured_window == WINDOW_40
    assert local_changed.first_seen_at_ns == 4_000_000_000
    assert (
        local_changed.accepted_identity.data_digest
        == local_first.accepted_identity.data_digest
    )
    assert local_changed.accepted_identity != local_first.accepted_identity
    # Hide both offscreen windows before pytest-qt's post-test event pump; a
    # queued paint on a window being torn down must not reach the delegate.
    view.close()
    local_view.close()


def test_age_uses_oldest_displayed_dependency_and_unknown_contributor(
    qtbot,
) -> None:
    # -- One real adapter-backed view: each dependency is accepted at its own
    # instant so the oldest displayed stamp is unambiguous. ------------------
    clock = ExactOnlyClock(1_000_000_000)
    service = IdentityExactOnlyQuery()
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=500)]
    service.set_pending("totals", 1, WINDOW_A)
    service.set_pending("series", 1, WINDOW_A)
    service.identities[("ranking", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "ranking", SUBJECT_KEY, WINDOW_A, "a" * 64
    )
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "totals", SUBJECT_KEY, WINDOW_A, "b" * 64
    )
    service.identities[("series", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "series", SUBJECT_KEY, WINDOW_A, "c" * 64
    )
    view = AppIoWindow(service, clock_ns=clock)
    qtbot.addWidget(view)
    view.show()
    view.set_window(WINDOW_A)

    # Defined PEAK is a displayed measurement for age, but it does not make the
    # panel claim displayed values (spec §9.1).
    peak_only = view.measurement_status()
    assert peak_only.has_displayed_values is False
    assert [answer.kind for answer in peak_only.displayed_answers] == ["ranking"]
    assert peak_only.share_state == "pending"
    assert peak_only.series_state == "pending"
    assert peak_only.ranking_empty is False
    clock.advance_ns(9_000_000_000)
    assert oldest_answer_age(peak_only.displayed_answers, clock.now_ns) == (
        "oldest answer 9 s ago"
    )
    view.show_overview_age(
        oldest_answer_age(peak_only.displayed_answers, clock.now_ns)
    )
    age_label = label(view, "overview_age")
    assert age_label.text() == "oldest answer 9 s ago"
    assert not age_label.isHidden()

    # The share then the series accepted at t=2 s and t=3 s; the fixed order is
    # ranking, totals, series and the oldest stamp stays the ranking's 1 s.
    clock.advance_ns(-8_000_000_000)  # back to t=2 s for the share acceptance
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    view.refresh()
    clock.advance_ns(1_000_000_000)
    service.set_series(WINDOW_A, measured_series(WINDOW_A))
    view.refresh()

    mixed = view.measurement_status()
    assert mixed.has_displayed_values is True
    assert [answer.kind for answer in mixed.displayed_answers] == [
        "ranking",
        "totals",
        "series",
    ]
    assert [answer.first_seen_at_ns for answer in mixed.displayed_answers] == [
        1_000_000_000,
        2_000_000_000,
        3_000_000_000,
    ]
    clock.advance_ns(7_000_000_000)  # now t=10 s
    assert oldest_answer_age(mixed.displayed_answers, clock.now_ns) == (
        "oldest answer 9 s ago"
    )

    # Any identity-less displayed contributor makes age unknown, even while the
    # other accepted dependencies carry exact stamps.
    service.identities[("ranking", SUBJECT_KEY, WINDOW_A)] = None
    view.refresh()
    unknown = view.measurement_status()
    assert [answer.kind for answer in unknown.displayed_answers] == [
        "ranking",
        "totals",
        "series",
    ]
    assert unknown.displayed_answers[0].accepted_identity is None
    assert oldest_answer_age(unknown.displayed_answers, clock.now_ns) == "age unknown"

    # An undefined PEAK is not a displayed answer: the oldest contributing
    # dependency is then the 2 s share, and the ranking leaves the tuple.
    service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    service.identities[("ranking", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "ranking", SUBJECT_KEY, WINDOW_A, "a" * 64
    )
    view.refresh()
    no_peak = view.measurement_status()
    assert [answer.kind for answer in no_peak.displayed_answers] == [
        "totals",
        "series",
    ]
    assert oldest_answer_age(no_peak.displayed_answers, clock.now_ns) == (
        "oldest answer 8 s ago"
    )

    # A current series failure clears its recorded dependency and removes it
    # from the age tuple; the share's own stamp remains the oldest.
    service.set_error("series", 1, WINDOW_A, RuntimeError("series_bad"))
    view.refresh()
    failed_series = view.measurement_status()
    assert failed_series.series_state == "failed"
    assert failed_series.series_reason == "series_bad"
    assert [answer.kind for answer in failed_series.displayed_answers] == ["totals"]
    assert oldest_answer_age(failed_series.displayed_answers, clock.now_ns) == (
        "oldest answer 8 s ago"
    )
    assert view._overview_model.series_unavailable == "series_bad"

    # With no displayed measurement at all the label is hidden and the age is
    # absent; the status/coverage text is not a measurement.
    service.set_error("totals", 1, WINDOW_A, RuntimeError("share_bad"))
    view.refresh()
    nothing = view.measurement_status()
    assert nothing.has_displayed_values is False
    assert nothing.displayed_answers == ()
    assert view._overview_model.coverage_line != ""
    assert view._overview_model.detail_line != ""
    assert oldest_answer_age(nothing.displayed_answers, clock.now_ns) is None
    view.show_overview_age(None)
    assert age_label.text() == "" and age_label.isHidden()
    with pytest.raises(ValueError):
        view.show_overview_age("")
    with pytest.raises(ValueError):
        view.show_overview_age(7)  # type: ignore[arg-type]

    # A current success with no accepted identity is displayed but unidentified:
    # the exact text is "age unknown", never a guessed age.
    clock.advance_ns(1_000_000_000)
    service.set_share(WINDOW_A, measured_share(WINDOW_A))
    service.identities[("totals", SUBJECT_KEY, WINDOW_A)] = None
    view.refresh()
    identity_less = view.measurement_status()
    assert identity_less.has_displayed_values is True
    assert [answer.kind for answer in identity_less.displayed_answers] == ["totals"]
    assert identity_less.displayed_answers[0].accepted_identity is None
    assert oldest_answer_age(identity_less.displayed_answers, clock.now_ns) == (
        "age unknown"
    )

    # -- An unsupported lifetime chronology and an available-empty series are
    # not displayed measurements either. ------------------------------------
    lifetime = Window(kind="lifetime")
    lifetime_clock = ExactOnlyClock(2_000_000_000)
    lifetime_service = IdentityExactOnlyQuery()
    lifetime_service.rows = [
        app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)
    ]
    lifetime_service.set_series(lifetime, measured_series(lifetime))
    lifetime_service.set_pending("totals", 1, lifetime)
    lifetime_service.identities[("series", SUBJECT_KEY, lifetime)] = AcceptedIdentity(
        "series", SUBJECT_KEY, lifetime, "c" * 64
    )
    lifetime_view = AppIoWindow(lifetime_service, clock_ns=lifetime_clock)
    qtbot.addWidget(lifetime_view)
    lifetime_view.show()
    lifetime_view.set_window(lifetime)
    lifetime_model = lifetime_view._overview_model
    assert lifetime_model.series_unavailable == "lifetime chronology unavailable"
    assert lifetime_model.task_points == () and lifetime_model.device_lanes == ()
    lifetime_status = lifetime_view.measurement_status()
    assert lifetime_status.series_state == "ok"
    assert lifetime_status.displayed_answers == ()
    assert lifetime_status.has_displayed_values is False
    assert oldest_answer_age(lifetime_status.displayed_answers, 2_000_000_000) is None

    empty_clock = ExactOnlyClock(3_000_000_000)
    empty_service = IdentityExactOnlyQuery()
    empty_service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    empty_service.set_series(
        WINDOW_A,
        Series(1, WINDOW_A, "raw", (), (), (), coverage()),
    )
    empty_service.set_pending("totals", 1, WINDOW_A)
    empty_service.identities[("series", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "series", SUBJECT_KEY, WINDOW_A, "c" * 64
    )
    empty_view = AppIoWindow(empty_service, clock_ns=empty_clock)
    qtbot.addWidget(empty_view)
    empty_view.show()
    empty_view.set_window(WINDOW_A)
    empty_model = empty_view._overview_model
    assert empty_model.task_points == () and empty_model.device_lanes == ()
    empty_status = empty_view.measurement_status()
    assert empty_status.series_state == "ok"
    assert empty_status.displayed_answers == ()
    assert empty_status.has_displayed_values is False
    assert oldest_answer_age(empty_status.displayed_answers, 3_000_000_000) is None

    # A nonempty accepted task point is a displayed measurement even when every
    # measured byte value is zero; an empty sequence is not.
    zero_clock = ExactOnlyClock(4_000_000_000)
    zero_service = IdentityExactOnlyQuery()
    zero_service.rows = [app_total(1, SUBJECT_KEY, "Alpha", 22, peak_bps=None)]
    zero_service.set_series(
        WINDOW_A,
        Series(
            1,
            WINDOW_A,
            "raw",
            (SeriesPoint(0, 10, None, 0, 0, None, None, None, False),),
            (),
            (),
            coverage(),
        ),
    )
    zero_service.set_pending("totals", 1, WINDOW_A)
    zero_service.identities[("series", SUBJECT_KEY, WINDOW_A)] = AcceptedIdentity(
        "series", SUBJECT_KEY, WINDOW_A, "c" * 64
    )
    zero_view = AppIoWindow(zero_service, clock_ns=zero_clock)
    qtbot.addWidget(zero_view)
    zero_view.show()
    zero_view.set_window(WINDOW_A)
    zero_model = zero_view._overview_model
    assert [point.read_bytes for point in zero_model.task_points] == [0]
    assert [point.write_bytes for point in zero_model.task_points] == [0]
    zero_status = zero_view.measurement_status()
    assert zero_status.has_displayed_values is True
    assert [answer.kind for answer in zero_status.displayed_answers] == ["series"]
    assert oldest_answer_age(zero_status.displayed_answers, 4_000_000_000) == (
        "oldest answer 0 s ago"
    )
    for created in (view, lifetime_view, empty_view, zero_view):
        created.close()


def test_main_window_uses_data_identity_for_task_and_device_appends(qtbot, monkeypatch) -> None:
    """Chart identities follow typed series data, even when points do not change."""
    from live_overview_support import series as fixture_series, share as fixture_share

    view, _ = build(qtbot, [], {})
    task_calls = []
    device_calls = []
    task_chart = view._total_chart
    device_chart = view._device_lanes[0]

    def spy(chart, method, calls):
        original = getattr(chart, method)

        def wrapped(points, *, identity=None):
            calls.append((method, tuple(points), identity))
            return original(points, identity=identity)

        monkeypatch.setattr(chart, method, wrapped)

    for method in ("set_task_points", "append_task_points"):
        spy(task_chart, method, task_calls)
    for method in ("set_device_points", "append_device_points"):
        spy(device_chart, method, device_calls)

    key = "unit:alpha.service"
    requested = Window(kind="since_boot", boot_id="boot-a", end_mono_ns=180_000_000_000)

    def model(window, *, app_key=key, tier="rollup_60s", dev="8:0", empty_device=False,
              byte_delta=0, stamp=1_000_000_000):
        source = fixture_series(1, window, tier=tier)
        start = window.start_mono_ns or 0
        points = tuple(p for p in source.points if p.bucket_start_mono_ns >= start)
        if byte_delta:
            points = points[:-1] + (replace(points[-1], read_bytes=points[-1].read_bytes + byte_delta),)
        device_points = tuple(
            replace(p, dev=dev) for p in source.device_points
            if p.dev == "8:0" and p.bucket_start_mono_ns >= start and not empty_device
        )
        source = replace(source, points=points, device_points=device_points, residual_points=())
        split = fixture_share(1, window)
        split = replace(split, devices=(replace(split.devices[0], dev=dev),))
        row = app_total(1, app_key, "Alpha", 22)
        return build_overview(
            1, window, row, source, split,
            ranking_provenance=measured_provenance("ranking", app_key, window, first_seen=stamp),
            series_provenance=measured_provenance("series", app_key, window, first_seen=stamp),
            share_provenance=measured_provenance("totals", app_key, window, first_seen=stamp),
        )

    first = model(requested)
    view._apply_overview(first)
    scope = retention_scope(key, requested)
    task_identity = ("task", key, scope, 0, "rollup_60s")
    device_identity = ("device", key, scope, 0, "rollup_60s", "8:0")
    assert first.series_metadata.tier == "rollup_60s"
    assert first.share.coverage.tier == "raw"
    assert task_calls[-1][0] == "append_task_points"
    assert task_calls[-1][2] == task_identity
    assert device_calls[-1][0] == "append_device_points"
    assert device_calls[-1][2] == device_identity
    assert all(isinstance(point, DeviceSeriesPoint) for point in device_calls[-1][1])

    # A later endpoint and new data retain the token; provenance and text alone
    # never force either chart to rebuild.
    later = replace(requested, end_mono_ns=240_000_000_000)
    advanced = model(later, byte_delta=1, stamp=3_000_000_000)
    view._apply_overview(advanced)
    assert task_calls[-1][2] == task_identity
    assert device_calls[-1][2] == device_identity
    before = (len(task_calls), len(device_calls))
    view._apply_overview(replace(advanced, detail_line="arbitrary rollup text"))
    assert (len(task_calls), len(device_calls)) == before
    restamped = model(later, byte_delta=1, stamp=4_000_000_000)
    view._apply_overview(restamped)
    assert (len(task_calls), len(device_calls)) == before

    # Equal points for another subject still reach both charts with new tokens.
    other = "unit:beta.service"
    view._apply_overview(model(later, app_key=other, byte_delta=1))
    assert task_calls[-1][2] == ("task", other, retention_scope(other, later), 0, "rollup_60s")
    assert device_calls[-1][2] == ("device", other, retention_scope(other, later), 0, "rollup_60s", "8:0")

    # Scope and range start, then tier, each change the identity independently.
    ranged = Window(kind="range", boot_id="boot-a", start_mono_ns=10_000_000_000,
                    end_mono_ns=180_000_000_000)
    view._apply_overview(model(ranged, app_key=other))
    assert task_calls[-1][2] == ("task", other, retention_scope(other, ranged),
                                  10_000_000_000, "rollup_60s")
    shifted = replace(ranged, start_mono_ns=20_000_000_000)
    shifted_model = model(shifted, app_key=other)
    assert shifted_model.task_points == view._overview_model.task_points
    view._apply_overview(shifted_model)
    assert task_calls[-1][2] == ("task", other, retention_scope(other, shifted),
                                  20_000_000_000, "rollup_60s")
    raw = model(requested, tier="raw")
    view._apply_overview(raw)
    assert task_calls[-1][2][-1] == "raw"
    assert device_calls[-1][2][-2:] == ("raw", "8:0")

    # A reused lane with equal empty points must still receive the new device.
    empty_a = model(requested, empty_device=True)
    empty_b = model(requested, dev="8:16", empty_device=True)
    view._apply_overview(empty_a)
    assert empty_a.device_lanes[0].points == empty_b.device_lanes[0].points == ()
    count = len(device_calls)
    view._apply_overview(empty_b)
    assert len(device_calls) == count + 1
    assert device_calls[-1][2] == ("device", key, scope, 0, "rollup_60s", "8:16")

    # Direct legacy models have no typed identity and use full setters.
    legacy = replace(empty_b, requested_window=None, app_key=None, series_metadata=None,
                     ranking_provenance=None, series_provenance=None, share_provenance=None)
    view._apply_overview(legacy)
    assert task_calls[-1][0] == "set_task_points" and task_calls[-1][2] is None
    assert device_calls[-1][0] == "set_device_points" and device_calls[-1][2] is None
