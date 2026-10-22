"""Offscreen contract tests for the thin P2 application window.

The window must render only what the query contracts return. Every query is
served by a fake object that has no connection, no store, and no SQL, so a
window that reached past the contract would fail at import or at call time.
"""

from __future__ import annotations

import inspect

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
    DeviceShareRow,
    DiskShare,
    HookHits,
    MAX_RANGE_NS,
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
from appio.ui.main_window import AppIoWindow

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
    """Only the two query-contract methods. No connection, store, or SQL."""

    def __init__(self, totals: list[AppTotal], shares: dict[int, DiskShare]) -> None:
        self._totals = totals
        self._shares = shares
        self.ranking_calls: list[tuple[Window, str]] = []
        self.disk_share_calls: list[tuple[int, Window]] = []

    def ranking(self, window: Window, metric: str) -> list[AppTotal]:
        self.ranking_calls.append((window, metric))
        return list(self._totals)

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        self.disk_share_calls.append((app_id, window))
        return self._shares[app_id]


def build(
    qtbot,
    totals: list[AppTotal],
    shares: dict[int, DiskShare] | None = None,
) -> tuple[AppIoWindow, FakeQueryService]:
    if shares is None:
        shares = {
            row.app_id: disk_share(row.app_id, task_total=row.total_bytes)
            for row in totals
        }
    service = FakeQueryService(totals, shares)
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
