"""The thin P2 application window.

The window consumes only the query contract: a ``QueryService``-shaped object
with ``ranking(window, metric)`` and ``disk_share(app_id, window)``. It opens no
database, reads no process files, and contains no tray, daemon, Files tab, or
privilege flow. Unavailable values are rendered explicitly: a ``None`` residual,
peak, or device total is never shown as zero.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from appio.contracts import AppTotal, DiskShare, Window, WindowUnavailableError
from appio.ui.formatting import (
    A22_LAUNCH_REASON,
    A22_RANGE_REASON,
    UNAVAILABLE,
    format_bytes,
    format_peak,
    format_residual,
    window_disabled_reason,
)

RESIDUAL_NOTE = "difference between accounting points"
TOTAL_METRIC = "total_bytes"


class QueryService(Protocol):
    """The only surface this window is allowed to depend on."""

    def ranking(self, window: Window, metric: str) -> Sequence[AppTotal]:
        """Return application totals for the window, ordered by total bytes."""

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        """Return the task/device split and signed residual for one app."""


def _value_label(object_name: str) -> QLabel:
    label = QLabel(UNAVAILABLE)
    label.setObjectName(object_name)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class AppIoWindow(QMainWindow):
    """A compact since-boot ranking with a KPI and residual detail strip."""

    def __init__(
        self,
        query_service: QueryService,
        boot_id: str,
        end_mono_ns: int,
    ) -> None:
        super().__init__()
        self._service = query_service
        self._since_boot = Window(
            kind="since_boot", boot_id=boot_id, end_mono_ns=end_mono_ns
        )
        self._active_window = self._since_boot
        self._disabled_reason: str | None = None
        self._selected_app_id: int | None = None
        self._totals: dict[int, AppTotal] = {}

        self.setWindowTitle("appio")
        self._build_ui()
        self.set_window(self._since_boot)

    # -- construction -----------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)

        title = QLabel("appio")
        title.setObjectName("title")
        outer.addWidget(title)

        scope_row = QHBoxLayout()
        self._since_boot_control = self._scope_control("Since boot", "scope_since_boot")
        self._launch_control = self._scope_control("Launch", "scope_launch")
        self._range_control = self._scope_control("Range", "scope_range")
        self._launch_control.setEnabled(False)
        self._launch_control.setAccessibleDescription(A22_LAUNCH_REASON)
        self._launch_control.setToolTip(A22_LAUNCH_REASON)
        for control in (
            self._since_boot_control,
            self._launch_control,
            self._range_control,
        ):
            scope_row.addWidget(control)
        scope_row.addStretch(1)
        outer.addLayout(scope_row)

        self._launch_reason = QLabel(A22_LAUNCH_REASON)
        self._launch_reason.setObjectName("launch_reason")
        self._launch_reason.setWordWrap(True)
        outer.addWidget(self._launch_reason)

        self._range_reason = QLabel("")
        self._range_reason.setObjectName("range_reason")
        self._range_reason.setWordWrap(True)
        self._range_reason.setVisible(False)
        outer.addWidget(self._range_reason)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("applications"))
        self._list = QListWidget()
        self._list.setObjectName("app_list")
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.setUniformItemSizes(True)
        left.addWidget(self._list, 1)
        body.addLayout(left, 3)

        right = QVBoxLayout()
        kpis = QGridLayout()
        self._total_value = _value_label("total_value")
        self._read_value = _value_label("read_value")
        self._write_value = _value_label("write_value")
        self._peak_value = _value_label("peak_value")
        self._device_value = _value_label("device_value")
        for column, (caption, value) in enumerate(
            (
                ("TOTAL", self._total_value),
                ("READ", self._read_value),
                ("WRITE", self._write_value),
                ("PEAK", self._peak_value),
                ("DEVICE", self._device_value),
            )
        ):
            caption_label = QLabel(caption)
            caption_label.setObjectName(f"{value.objectName()}_caption")
            kpis.addWidget(caption_label, 0, column)
            kpis.addWidget(value, 1, column)
        right.addLayout(kpis)

        self._residual_value = QLabel("")
        self._residual_value.setObjectName("residual_value")
        self._residual_value.setWordWrap(True)
        right.addWidget(self._residual_value)

        self._coverage_label = QLabel("")
        self._coverage_label.setObjectName("coverage_label")
        self._coverage_label.setWordWrap(True)
        right.addWidget(self._coverage_label)

        self._detail_state = QLabel("")
        self._detail_state.setObjectName("detail_state")
        self._detail_state.setWordWrap(True)
        right.addWidget(self._detail_state)
        right.addStretch(1)
        body.addLayout(right, 4)

        outer.addLayout(body, 1)
        self.setCentralWidget(central)
        self.resize(880, 520)

        self._list.currentItemChanged.connect(self._on_current_item_changed)
        self._since_boot_control.clicked.connect(self._on_since_boot_clicked)
        self._range_control.clicked.connect(self._on_range_clicked)

    @staticmethod
    def _scope_control(caption: str, object_name: str) -> QPushButton:
        control = QPushButton(caption)
        control.setObjectName(object_name)
        control.setCheckable(True)
        control.setAutoDefault(False)
        return control

    # -- public interface -------------------------------------------------

    def refresh(self) -> None:
        """Re-read the active window through the query contract."""

        reason = self._disabled_reason
        if reason is not None:
            self._render_unavailable(reason)
            return

        try:
            rows = list(self._service.ranking(self._active_window, TOTAL_METRIC))
        except WindowUnavailableError as error:
            self._render_unavailable(str(error))
            return

        rows.sort(key=lambda row: (-row.total_bytes, row.app_key.encode("utf-8")))
        self._totals = {row.app_id: row for row in rows}

        target = 0
        for index, row in enumerate(rows):
            if row.app_id == self._selected_app_id:
                target = index
                break

        self._list.blockSignals(True)
        try:
            self._list.clear()
            for row in rows:
                item = QListWidgetItem(
                    f"{row.name}  {format_bytes(row.total_bytes)}"
                )
                item.setData(Qt.ItemDataRole.UserRole, row.app_id)
                self._list.addItem(item)
            if rows:
                self._list.setCurrentRow(target)
        finally:
            self._list.blockSignals(False)

        if not rows:
            self._selected_app_id = None
            self._render_empty()
            return

        selected = rows[target]
        self._selected_app_id = selected.app_id
        self._load_selected(selected.app_id)

    def selected_app_id(self) -> int | None:
        """Return the selected application's database-local id, if any."""

        return self._selected_app_id

    def set_window(self, window: Window) -> None:
        """Render a requested window without widening or silently replacing it."""

        reason = window_disabled_reason(window)
        self._active_window = window
        self._disabled_reason = reason

        self._since_boot_control.setChecked(
            reason is None and window.kind == "since_boot"
        )
        self._range_control.setChecked(reason is None and window.kind == "range")
        self._launch_control.setChecked(False)

        range_disabled = reason == A22_RANGE_REASON
        self._range_control.setEnabled(
            reason is None and window.kind == "range"
        )
        if range_disabled:
            self._range_control.setAccessibleDescription(A22_RANGE_REASON)
            self._range_control.setToolTip(A22_RANGE_REASON)
        else:
            self._range_control.setAccessibleDescription("")
            self._range_control.setToolTip("")

        self._range_reason.setText(A22_RANGE_REASON if range_disabled else "")
        self._range_reason.setVisible(range_disabled)

        if reason is not None:
            self._render_unavailable(reason)
            return
        self.refresh()

    # -- selection --------------------------------------------------------

    def _on_current_item_changed(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        del previous
        if current is None or self._disabled_reason is not None:
            return
        app_id = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(app_id, int):
            return
        self._selected_app_id = app_id
        self._load_selected(app_id)

    def _on_since_boot_clicked(self) -> None:
        self.set_window(self._since_boot)

    def _on_range_clicked(self) -> None:
        if self._active_window.kind != "range" or self._disabled_reason is not None:
            self._range_control.setChecked(False)
            return
        self.set_window(self._active_window)

    def _load_selected(self, app_id: int) -> None:
        if self._disabled_reason is not None:
            return
        try:
            share = self._service.disk_share(app_id, self._active_window)
        except WindowUnavailableError as error:
            self._render_unavailable(str(error))
            return
        self._render_share(share, self._totals.get(app_id))

    # -- rendering --------------------------------------------------------

    def _render_share(self, share: DiskShare, total: AppTotal | None) -> None:
        coverage = share.coverage
        device_available = coverage.device_state == "available"

        self._total_value.setText(format_bytes(share.task_total_bytes))
        self._read_value.setText(format_bytes(share.task_read_bytes))
        self._write_value.setText(format_bytes(share.task_write_bytes))
        self._peak_value.setText(
            format_peak(total.peak_total_bps if total is not None else None)
        )

        device_total = share.device_total_bytes if device_available else None
        self._device_value.setText(
            format_bytes(device_total) if device_total is not None else UNAVAILABLE
        )

        residual = share.residual_total if device_available else None
        complete = device_available and residual is not None
        self._residual_value.setText(
            f"Residual: {format_residual(residual, complete)} - {RESIDUAL_NOTE}"
        )

        state = "complete" if coverage.complete else "incomplete"
        reason = (
            f"; reason: {coverage.unavailable_reason}"
            if coverage.unavailable_reason is not None
            else ""
        )
        self._coverage_label.setText(
            f"coverage: {state}; device attribution: {coverage.device_state}{reason}"
        )
        self._detail_state.setText(
            f"window: {self._active_window.kind}; tier: {coverage.tier}"
        )

    def _render_unavailable(self, reason: str) -> None:
        self._totals = {}
        self._selected_app_id = None
        self._list.blockSignals(True)
        try:
            self._list.clear()
        finally:
            self._list.blockSignals(False)

        for label in (
            self._total_value,
            self._read_value,
            self._write_value,
            self._peak_value,
            self._device_value,
        ):
            label.setText(UNAVAILABLE)
        self._residual_value.setText(
            f"Residual: {UNAVAILABLE} - {RESIDUAL_NOTE}"
        )
        self._coverage_label.setText("coverage: unavailable")
        self._detail_state.setText(reason)

    def _render_empty(self) -> None:
        for label in (
            self._total_value,
            self._read_value,
            self._write_value,
            self._peak_value,
            self._device_value,
        ):
            label.setText(UNAVAILABLE)
        self._residual_value.setText(
            f"Residual: {UNAVAILABLE} - {RESIDUAL_NOTE}"
        )
        self._coverage_label.setText("coverage: unavailable")
        self._detail_state.setText(
            f"window: {self._active_window.kind}; no applications observed"
        )


__all__ = ["AppIoWindow", "QueryService"]
