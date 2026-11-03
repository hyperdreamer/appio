"""The appio console: one ranking, one subject, Overview and Files tabs.

The window consumes only the query contract: a ``QueryService``-shaped object
with ``ranking(window, metric)``, ``disk_share(app_id, window)`` and
``series(app_id, window)``. It opens no store, reads no process files and
contains no tray, daemon or privilege flow. One ``_subject_app_id`` drives both
tabs, and every unavailable value is rendered explicitly: a ``None`` residual,
peak or device total is never shown as zero.

The detail pane is a persistent two-tab host (``Overview`` then ``Files``) that
exists before the first sample. ``refresh`` re-reads the ranking once and then
the shared subject's series and disk share independently, and the frozen render
models are diffed into the widgets so an identical tick touches nothing.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Protocol, Sequence

from PyQt6 import sip
from PyQt6.QtCore import QModelIndex, QObject, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from appio.contracts import AppTotal, DiskShare, Series, Window, WindowUnavailableError
from appio.ui import theme
from appio.ui.charts import DeviceLane, DonutChart, DualSeriesChart, ShareBars
from appio.ui.files_view import FilesView
from appio.ui.formatting import (
    A22_LAUNCH_REASON,
    A22_RANGE_REASON,
    UNAVAILABLE,
    format_bytes,
    format_residual,
    window_disabled_reason,
)
from appio.ui.render_model import (
    COVERAGE_UNAVAILABLE,
    EMPTY_RANKING_TEXT,
    KPI_CAPTIONS,
    MAX_DEVICE_LANES,
    SHARE_UNAVAILABLE_REASON,
    OverviewModel,
    RankingModel,
    RankingRow,
    build_overview,
    build_ranking,
)

RESIDUAL_NOTE = "difference between accounting points"
TOTAL_METRIC = "total_bytes"

#: `Qt.ItemDataRole.UserRole` stores the application id; the next role the share.
SUBJECT_ROLE = int(Qt.ItemDataRole.UserRole)
SHARE_ROLE = SUBJECT_ROLE + 1

_KPI_OBJECT_NAMES: dict[str, str] = {
    "TOTAL": "total_value",
    "READ": "read_value",
    "WRITE": "write_value",
    "PEAK": "peak_value",
    "DEVICE": "device_value",
}


class QueryService(Protocol):
    """The only surface this window is allowed to depend on."""

    def ranking(self, window: Window, metric: str) -> Sequence[AppTotal]:
        """Return application totals for the window, ordered by total bytes."""

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        """Return the task/device split and signed residual for one app."""

    def series(self, app_id: int, window: Window) -> Series:
        """Return that app's contract buckets for the exact window."""


def _error_text(error: BaseException) -> str:
    """A nonempty reason for an operational failure (spec §7)."""

    return str(error) or type(error).__name__


def _coverage_diagnostic(service: object) -> str | None:
    error = getattr(service, "daemon_answer_error", None)
    if error is None:
        return None
    clauses = {
        "remote_error": "daemon reported an error",
        "unavailable": "no answering daemon",
        "rejected": "history query failed",
        "transport": "connection lost before the answer",
        "internal": "history query failed",
    }
    clause = clauses.get(getattr(error, "kind", None))
    reason = getattr(error, "reason", None)
    if clause is None or not isinstance(reason, str) or not reason:
        return None
    return f"{clause} ({reason})"


class _ProcessOnlyFilesSource:
    """The explicit file-source-unavailable surface for a process-only fake.

    This window requires only the process query protocol. A structural query
    fake without file-query capabilities must still leave the Files tab saying
    the file source is unavailable rather than inventing an empty table
    (spec 3.3).
    """

    available = False
    reason: str | None = None
    diagnostic = "the file source is unavailable"

    def file_source_status(self) -> "_ProcessOnlyFilesSource":
        return self

    def ready(self, window: Window) -> bool:
        return False

    def ranking(self, window: Window, metric: str) -> Sequence[AppTotal]:
        return ()

    def request_file_view(self, *args: object) -> object:
        raise WindowUnavailableError(
            "file_source_unavailable", "the file source is unavailable"
        )


def _value_label(object_name: str) -> QLabel:
    label = QLabel(UNAVAILABLE)
    label.setObjectName(object_name)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _item_app_id(item: QListWidgetItem | None) -> int | None:
    if item is None:
        return None
    data = item.data(SUBJECT_ROLE)
    if isinstance(data, int) and not isinstance(data, bool):
        return data
    return None


def _device_available(share: DiskShare) -> bool:
    """Device attribution is usable only when its state *and* total are defined."""

    return (
        share.coverage.device_state == "available"
        and share.device_total_bytes is not None
    )


def _device_unavailable_text(model: OverviewModel | None) -> str:
    """The device-attribution strip, distinct from the DEVICE KPI value."""

    if model is None:
        return ""
    diagnostic = model.coverage_diagnostic
    if model.share is None:
        if diagnostic is None:
            return ""
        if model.share_unavailable is None:
            return f"device attribution unavailable: {diagnostic}"
        return (
            f"device attribution unavailable: {model.share_unavailable}; "
            f"{diagnostic}"
        )
    share = model.share
    if _device_available(share):
        return ""
    reason = share.coverage.unavailable_reason or share.coverage.device_state
    if diagnostic is not None:
        reason = f"{reason}; {diagnostic}"
    return f"device attribution unavailable: {reason}"


def _share_unavailable_reason(model: OverviewModel) -> str:
    """The reason the attribution panel cannot draw, or its own query error."""

    share = model.share
    if share is None:
        return model.share_unavailable or SHARE_UNAVAILABLE_REASON
    return share.coverage.unavailable_reason or share.coverage.device_state


def _share_reconciliation_text(model: OverviewModel) -> str | None:
    """The panel's compact §7 equation, not the residual-strip annotation."""

    share = model.share
    if (share is None or not _device_available(share)
            or share.residual_total is None):
        return None
    return (
        f"Σ disks {format_bytes(share.device_total_bytes)} + residual "
        f"({format_bytes(share.residual_total)}) = "
        f"{format_bytes(share.task_total_bytes)} total"
    )


def _share_rows(model: OverviewModel) -> tuple[tuple[str, int], ...] | None:
    """Every attributed share row, in contract order, or ``None`` if withheld."""

    share = model.share
    if share is None or not _device_available(share):
        return None
    lifetime = share.window.kind == "lifetime"
    rows: list[tuple[str, int]] = []
    for row in share.devices:
        # Timeless tokens repeat across boots; the boot id keeps them distinct.
        label = f"{row.dev} [{row.boot_id}]" if lifetime else row.dev
        rows.append((label, row.total_bytes))
    return tuple(rows)


def _residual_text(model: OverviewModel | None) -> str:
    """The signed residual strip: the short form, unavailable when undefined.

    The model's full accounting equation is the attribution panel's annotation
    (:meth:`ShareBars.set_reconciliation`); this strip keeps the existing signed
    wording so a single measured line does not repeat it (spec 7).
    """

    share = model.share if model is not None else None
    if share is None or not _device_available(share):
        residual: int | None = None
    else:
        residual = share.residual_total
    return f"Residual: {format_residual(residual, residual is not None)} - {RESIDUAL_NOTE}"


class RankingList(QListWidget):
    """The ranking list, recording the row rectangles it requests a repaint for.

    Qt exposes no repaint signal, so the list records the exact viewport
    rectangles it asks Qt to redraw. That lets tests prove a changed share
    repaints its row instead of rebuilding the list.
    """

    #: Bounded observation log of the row rectangles this list repainted.
    REPAINT_LOG_SIZE = 64

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repaint_requests: deque[QRect] = deque(maxlen=self.REPAINT_LOG_SIZE)

    def repaint_row(self, rect: QRect) -> None:
        """Ask Qt to repaint one row's viewport rectangle and record it."""

        if not rect.isValid():
            return
        viewport = self.viewport()
        if viewport is None:  # pragma: no cover - a list view owns a viewport
            return
        self.repaint_requests.append(QRect(rect))
        viewport.update(rect)


class RankingDelegate(QStyledItemDelegate):
    """Draw one ranking row: Qt-styled text plus an inline share bar.

    The share is read from :data:`SHARE_ROLE`; label text is never parsed, so a
    rounded byte total cannot be mistaken for a share.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def paint(  # type: ignore[override]
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        widget = option.widget
        if widget is not None and sip.isdeleted(widget):
            # A paint queued for a destroyed view must not touch Qt state.
            return
        # The Qt style draws the background and text for both normal and
        # selected rows; nothing is parsed out of that text.
        super().paint(painter, option, index)

        share = index.data(SHARE_ROLE)
        fraction = 0.0
        if isinstance(share, (int, float)) and not isinstance(share, bool):
            value = float(share)
            if math.isfinite(value):
                fraction = min(1.0, max(0.0, value))

        rect = option.rect
        margin = 4.0
        bar_height = float(theme.BAR_HEIGHT)
        track = QRectF(
            rect.left() + margin,
            rect.bottom() - bar_height - 2.0,
            max(0.0, rect.width() - 2.0 * margin),
            bar_height,
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.TRACK_COLOR)
        painter.drawRoundedRect(track, 2.0, 2.0)
        if fraction > 0.0:
            painter.setBrush(theme.READ_COLOR)
            painter.drawRoundedRect(
                QRectF(track.left(), track.top(), track.width() * fraction, track.height()),
                2.0,
                2.0,
            )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        # Reserve the inline bar row under the text (spec 3.3).
        return QSize(size.width(), size.height() + theme.BAR_HEIGHT + 12)


class AppIoWindow(QMainWindow):
    """A persistent two-tab console with one shared subject."""

    def __init__(
        self,
        query_service: QueryService,
        boot_id: str | None = None,
        end_mono_ns: int | None = None,
        *,
        files_view: FilesView | None = None,
    ) -> None:
        super().__init__()
        if (boot_id is None) != (end_mono_ns is None):
            raise ValueError(
                "boot_id and end_mono_ns must both be present or both be absent"
            )
        self._service = query_service
        self._since_boot: Window | None = None
        self._active_window: Window | None = None
        self._disabled_reason: str | None = None
        self._subject_app_id: int | None = None
        self._totals: dict[int, AppTotal] = {}
        self._ranking_model: RankingModel | None = None
        self._overview_model: OverviewModel | None = None
        self._has_frame = False
        # A standalone window owns its Files scope; the shell owns an embedded
        # console's one scope and calls this window's `set_window` only.
        self._owns_files_scope = boot_id is not None

        self.setWindowTitle("appio")
        if files_view is not None:
            self.files_view = files_view
        else:
            source: object = self._service
            if not callable(getattr(source, "file_source_status", None)):
                # A process-only query fake still gets the honest Files surface.
                source = _ProcessOnlyFilesSource()
            self.files_view = FilesView(source)
        self.files_view.setObjectName("files_view")
        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("detail_tabs")
        self._build_ui()
        self.files_view.subject_changed.connect(self._on_files_subject_changed)

        if boot_id is not None and end_mono_ns is not None:
            self._since_boot = Window(
                kind="since_boot", boot_id=boot_id, end_mono_ns=end_mono_ns
            )
            self._since_boot_control.setEnabled(True)
            self.set_window(self._since_boot)
        else:
            # The persistent empty console: two tabs, no query, no scope.
            self.show_overview_status("", keep_frame=False)

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
        self._since_boot_control.setEnabled(False)
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
        self._list = RankingList()
        self._list.setObjectName("app_list")
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.setItemDelegate(RankingDelegate(self._list))
        left.addWidget(self._list, 1)
        body.addLayout(left, 2)

        self.detail_tabs.addTab(self._build_overview_tab(), "Overview")
        self.detail_tabs.addTab(self._build_files_tab(), "Files")
        body.addWidget(self.detail_tabs, 5)
        outer.addLayout(body, 1)

        self.setCentralWidget(central)
        self.resize(980, 620)

        self._list.currentItemChanged.connect(self._on_current_item_changed)
        self._since_boot_control.clicked.connect(self._on_since_boot_clicked)
        self._range_control.clicked.connect(self._on_range_clicked)

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        # The shell's one status line for loading/updating; a frame never hides
        # itself behind it (spec 3.3).
        self.history_state = QLabel("")
        self.history_state.setObjectName("history_state")
        self.history_state.setWordWrap(True)
        self.history_state.setVisible(False)
        layout.addWidget(self.history_state)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)

        kpis = QGridLayout()
        self._kpi_labels: list[QLabel] = []
        for column, caption in enumerate(KPI_CAPTIONS):
            value = _value_label(_KPI_OBJECT_NAMES[caption])
            caption_label = QLabel(caption)
            caption_label.setObjectName(f"{value.objectName()}_caption")
            kpis.addWidget(caption_label, 0, column)
            kpis.addWidget(value, 1, column)
            self._kpi_labels.append(value)
        self._total_value, self._read_value, self._write_value, self._peak_value, (
            self._device_value
        ) = self._kpi_labels
        body_layout.addLayout(kpis)

        self._total_chart = DualSeriesChart()
        self._total_chart.setMinimumHeight(120)
        body_layout.addWidget(self._total_chart)

        self._device_lanes: list[DeviceLane] = []
        for _index in range(MAX_DEVICE_LANES):
            lane = DeviceLane()
            lane.setMinimumHeight(72)
            lane.setVisible(False)
            self._device_lanes.append(lane)
            body_layout.addWidget(lane)
        self._more_devices = QLabel("")
        self._more_devices.setObjectName("more_devices")
        self._more_devices.setVisible(False)
        body_layout.addWidget(self._more_devices)

        composition = QHBoxLayout()
        self._donut = DonutChart()
        self._donut.setMinimumHeight(160)
        self._share_bars = ShareBars()
        self._share_bars.setMinimumHeight(150)
        composition.addWidget(self._donut, 1)
        composition.addWidget(self._share_bars, 1)
        body_layout.addLayout(composition)

        self._residual_value = QLabel(f"Residual: {UNAVAILABLE} - {RESIDUAL_NOTE}")
        self._residual_value.setObjectName("residual_value")
        self._residual_value.setWordWrap(True)
        self._residual_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body_layout.addWidget(self._residual_value)

        self._device_reason = QLabel("")
        self._device_reason.setObjectName("device_reason")
        self._device_reason.setWordWrap(True)
        self._device_reason.setVisible(False)
        body_layout.addWidget(self._device_reason)

        self._coverage_label = QLabel(COVERAGE_UNAVAILABLE)
        self._coverage_label.setObjectName("coverage_label")
        self._coverage_label.setWordWrap(True)
        body_layout.addWidget(self._coverage_label)

        self._detail_state = QLabel("")
        self._detail_state.setObjectName("detail_state")
        self._detail_state.setWordWrap(True)
        body_layout.addWidget(self._detail_state)
        body_layout.addStretch(1)

        layout.addWidget(body, 1)
        self._overview_body = body
        return page

    def _build_files_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.files_view, 1)
        self.files_view.setVisible(False)

        self.files_message = QLabel("")
        self.files_message.setObjectName("files_message")
        self.files_message.setWordWrap(True)
        self.files_message.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.files_message.setVisible(False)
        layout.addWidget(self.files_message)

        self.files_open_settings = QPushButton("Open settings")
        self.files_open_settings.setObjectName("files_open_settings")
        self.files_open_settings.setToolTip(
            "Opens Settings at the System services switch. Changes no state."
        )
        self.files_open_settings.setAutoDefault(False)
        layout.addWidget(self.files_open_settings)
        return page

    @staticmethod
    def _scope_control(caption: str, object_name: str) -> QPushButton:
        control = QPushButton(caption)
        control.setObjectName(object_name)
        control.setCheckable(True)
        control.setAutoDefault(False)
        return control

    # -- public interface -------------------------------------------------

    def refresh(self) -> None:
        """Re-read the active window through the query contract, once per source."""

        window = self._active_window
        if window is None:
            # A persistent console before its first frame queries nothing.
            return
        reason = self._disabled_reason
        if reason is not None:
            self._render_unavailable(reason)
            return

        try:
            rows = list(self._service.ranking(window, TOTAL_METRIC))
            model = build_ranking(rows)
        except Exception as error:
            self._totals = {}
            self._apply_ranking(build_ranking((), unavailable=_error_text(error)))
            return

        self._totals = {row.app_id: row for row in rows}
        subject_before = self._subject_app_id
        self._apply_ranking(model)
        if self._subject_app_id is not None and self._subject_app_id == subject_before:
            # The subject did not change, so only this refresh can update its
            # Overview; a changed subject already loaded it once (spec 3.3).
            self._load_selected(self._subject_app_id)

    def set_window(self, window: Window) -> None:
        """Render a requested window without widening or silently replacing it."""

        reason = window_disabled_reason(window)
        if window.kind == "since_boot":
            self._since_boot = window
            self._since_boot_control.setEnabled(True)
        self._active_window = window
        self._disabled_reason = reason

        self._since_boot_control.setChecked(
            reason is None and window.kind == "since_boot"
        )
        self._range_control.setChecked(reason is None and window.kind == "range")
        self._launch_control.setChecked(False)

        range_disabled = reason == A22_RANGE_REASON
        self._range_control.setEnabled(reason is None and window.kind == "range")
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
        if self._owns_files_scope:
            self._sync_owned_files()

    def selected_app_id(self) -> int | None:
        """Return the shared subject's id, including a file-only subject."""

        return self._subject_app_id

    @property
    def has_frame(self) -> bool:
        """Whether an applied Overview frame (empty/error included) exists."""

        return self._has_frame

    def show_overview_status(self, message: str, *, keep_frame: bool) -> None:
        """Render the shell's status line inside Overview.

        ``keep_frame=True`` changes only that line, so a pending replacement
        never puts an unavailable setter or new data over the last frame.
        """

        if not isinstance(message, str):
            raise ValueError("message must be a string")
        if not isinstance(keep_frame, bool):
            raise ValueError("keep_frame must be a bool")
        self.history_state.setText(message)
        self.history_state.setVisible(bool(message))
        if keep_frame:
            return
        self._has_frame = False
        self._overview_body.setVisible(False)
        self.files_view.setVisible(False)

    # -- Files scope ownership --------------------------------------------

    def _sync_owned_files(self) -> None:
        """Standalone mode: adopt the owned Files scope after subject choice."""

        window = self._active_window
        if window is None:
            return
        subject = self._subject_app_id
        if subject is not None:
            self.files_view.set_app_id(subject, request=False)
        self.files_view.set_window(window)
        self.files_view.setVisible(True)
        # Adoption already issued its one query when the scope changed; the
        # flush is only needed for the unchanged-scope deferred request.
        self.files_view.flush_refresh()

    # -- ranking ----------------------------------------------------------

    def _apply_ranking(self, model: RankingModel) -> None:
        """Diff one ranking model into the list and follow its subject."""

        self._ranking_model = model
        if model.unavailable is not None:
            self._render_unavailable(model.unavailable)
            return

        rows = model.rows
        ranked_ids = tuple(row.app_id for row in rows)
        subject = self._choose_subject(ranked_ids)
        self._sync_items(rows, subject)

        if subject is None:
            self._subject_app_id = None
            self._align_highlight(None)
            self._render_empty()
            return
        if subject == self._subject_app_id:
            return
        self._set_subject(subject, request_files=False)

    def _choose_subject(self, ranked_ids: Sequence[int]) -> int | None:
        """Retain a valid subject, else first ranked, first file-only, then None."""

        file_only = self._file_choices()
        current = self._subject_app_id
        if current is not None and (current in ranked_ids or current in file_only):
            return current
        if ranked_ids:
            return ranked_ids[0]
        if file_only:
            return file_only[0]
        return None

    def _file_choices(self) -> tuple[int, ...]:
        """The Files union's application ids, in the selector's order."""

        selector = self.files_view.app_selector
        choices: list[int] = []
        for index in range(selector.count()):
            data = selector.itemData(index)
            if isinstance(data, int) and not isinstance(data, bool) and data > 0:
                choices.append(data)
        return tuple(choices)

    def _sync_items(self, rows: Sequence[RankingRow], subject: int | None) -> None:
        """Update or rebuild the list without disturbing an unchanged selection."""

        widget = self._list
        same_order = widget.count() == len(rows) and all(
            _item_app_id(widget.item(index)) == row.app_id
            for index, row in enumerate(rows)
        )
        if same_order:
            for index, row in enumerate(rows):
                item = widget.item(index)
                if item is None:  # pragma: no cover - count checked above
                    continue
                if item.text() != row.label:
                    item.setText(row.label)
                if item.data(SHARE_ROLE) != row.share:
                    item.setData(SHARE_ROLE, row.share)
                    widget.repaint_row(widget.visualItemRect(item))
            return

        bar = widget.verticalScrollBar()
        assert bar is not None  # a list view always owns a vertical scroll bar
        scroll = bar.value()
        blocked = widget.blockSignals(True)
        try:
            widget.clear()
            for row in rows:
                item = QListWidgetItem(row.label)
                item.setData(SUBJECT_ROLE, row.app_id)
                item.setData(SHARE_ROLE, row.share)
                widget.addItem(item)
            index = self._index_of(subject)
            if index >= 0:
                widget.setCurrentRow(index)
            else:
                widget.setCurrentRow(-1)
                widget.clearSelection()
        finally:
            widget.blockSignals(blocked)
        bar.setValue(min(scroll, bar.maximum()))

    def _index_of(self, app_id: int | None) -> int:
        if app_id is None:
            return -1
        for index in range(self._list.count()):
            if _item_app_id(self._list.item(index)) == app_id:
                return index
        return -1

    def _align_highlight(self, app_id: int | None) -> None:
        """Point the one highlight at the subject without a feedback selection."""

        index = self._index_of(app_id)
        bar = self._list.verticalScrollBar()
        assert bar is not None  # a list view always owns a vertical scroll bar
        scroll = bar.value()
        blocked = self._list.blockSignals(True)
        try:
            if index >= 0:
                if self._list.currentRow() != index:
                    self._list.setCurrentRow(index)
            else:
                self._list.setCurrentRow(-1)
                self._list.clearSelection()
        finally:
            self._list.blockSignals(blocked)
        if index < 0:
            # Clearing the current index may scroll; a file-only subject keeps
            # the ranking's scroll position (spec 3.3).
            bar.setValue(min(scroll, bar.maximum()))

    # -- subject ----------------------------------------------------------

    def _on_current_item_changed(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        del previous
        if current is None or self._disabled_reason is not None:
            return
        app_id = _item_app_id(current)
        if app_id is None or app_id <= 0:
            return
        self._set_subject(app_id, request_files=True)

    def _on_files_subject_changed(self, app_id: object) -> None:
        """Follow the Files selector; its own query is never reissued."""

        if app_id is None:
            self._set_subject(None, request_files=False)
            return
        if isinstance(app_id, int) and not isinstance(app_id, bool) and app_id > 0:
            self._set_subject(app_id, request_files=False)

    def _set_subject(self, app_id: int | None, *, request_files: bool) -> None:
        """Install the one subject, its highlight, Overview and Files selection."""

        if app_id is not None and (
            not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0
        ):
            raise ValueError("subject app_id must be a positive integer or None")
        if not isinstance(request_files, bool):
            raise ValueError("request_files must be a bool")
        if app_id == self._subject_app_id:
            return
        self._subject_app_id = app_id
        self._align_highlight(app_id)
        if app_id is None:
            self._render_empty()
            return
        # `set_app_id` is positive-id only, so an empty-union notification has
        # already cleared the Files subject without this call.
        self.files_view.set_app_id(app_id, request=request_files)
        self._load_selected(app_id)

    def _load_selected(self, app_id: int) -> None:
        """Load one subject's series and disk share, each at most once."""

        window = self._active_window
        if window is None or self._disabled_reason is not None:
            return
        total = self._totals.get(app_id)
        if total is None:
            # A file-only subject: one subject, five unavailable KPIs.
            diagnostic = _coverage_diagnostic(self._service)
            self._apply_overview(build_overview(
                app_id,
                window,
                None,
                None,
                None,
                coverage_diagnostic=diagnostic,
            ))
            return

        series: Series | None = None
        series_error: str | None = None
        try:
            series = self._service.series(app_id, window)
        except Exception as error:
            series_error = _error_text(error)

        share: DiskShare | None = None
        share_error: str | None = None
        try:
            share = self._service.disk_share(app_id, window)
        except Exception as error:
            share_error = _error_text(error)

        diagnostic = _coverage_diagnostic(self._service)
        try:
            model = build_overview(
                app_id,
                window,
                total,
                series,
                share,
                series_error=series_error,
                share_error=share_error,
                coverage_diagnostic=diagnostic,
            )
        except ValueError as error:
            # A result that contradicts its own subject/window is a refusal to
            # render, never a rewritten value.
            refused = _error_text(error)
            model = build_overview(
                app_id,
                window,
                total,
                None,
                None,
                series_error=refused,
                share_error=refused,
                coverage_diagnostic=diagnostic,
            )
        self._apply_overview(model)

    # -- overview rendering ------------------------------------------------

    def _apply_overview(self, model: OverviewModel) -> None:
        """Diff one frozen Overview model into the labels and chart inputs."""

        previous = self._overview_model
        self._overview_model = model
        self._has_frame = True
        self._overview_body.setVisible(True)

        old_kpis = previous.kpis if previous is not None else None
        for index, kpi in enumerate(model.kpis):
            if old_kpis is None or old_kpis[index].text != kpi.text:
                self._kpi_labels[index].setText(kpi.text)

        if previous is None or _residual_text(previous) != _residual_text(model):
            self._residual_value.setText(_residual_text(model))
        if previous is None or previous.coverage_line != model.coverage_line:
            self._coverage_label.setText(model.coverage_line)
        if previous is None or previous.detail_line != model.detail_line:
            self._detail_state.setText(model.detail_line)

        device_reason = _device_unavailable_text(model)
        if previous is None or _device_unavailable_text(previous) != device_reason:
            self._device_reason.setText(device_reason)
            self._device_reason.setVisible(bool(device_reason))

        self._apply_timeline(model, previous)
        self._apply_composition(model, previous)

    def _apply_timeline(
        self, model: OverviewModel, previous: OverviewModel | None
    ) -> None:
        """Touch the TOTAL lane and the three reused device lanes only on change."""

        totals = (
            (model.share.task_read_bytes, model.share.task_write_bytes)
            if model.share is not None
            else None
        )
        old_totals = (
            (previous.share.task_read_bytes, previous.share.task_write_bytes)
            if previous is not None and previous.share is not None
            else None
        )
        timeline_changed = (
            previous is None
            or previous.task_points != model.task_points
            or previous.series_unavailable != model.series_unavailable
            or totals != old_totals
        )
        if timeline_changed:
            if model.series_unavailable is not None:
                self._total_chart.set_unavailable(model.series_unavailable)
            else:
                points_changed = (
                    previous is None
                    or previous.series_unavailable is not None
                    or previous.task_points != model.task_points
                )
                if points_changed:
                    self._total_chart.set_task_points(model.task_points)
                if totals is not None:
                    self._total_chart.set_totals(*totals)
                else:
                    self._total_chart.set_totals_unavailable()

        lanes_changed = (
            previous is None
            or previous.device_lanes != model.device_lanes
            or previous.series_unavailable != model.series_unavailable
        )
        if lanes_changed:
            if model.series_unavailable is not None:
                for lane in self._device_lanes:
                    lane.set_unavailable(model.series_unavailable)
                    lane.setVisible(False)
            else:
                for index, lane in enumerate(self._device_lanes):
                    if index < len(model.device_lanes):
                        lane_model = model.device_lanes[index]
                        lane.set_device(lane_model.dev)
                        lane.set_device_points(lane_model.points)
                        lane.set_totals(lane_model.read_bytes, lane_model.write_bytes)
                        lane.setVisible(True)
                    else:
                        lane.set_device_points(())
                        lane.setVisible(False)
        if previous is None or previous.more_devices != model.more_devices:
            self._more_devices.setText(f"and {model.more_devices} more")
            self._more_devices.setVisible(model.more_devices > 0)

    def _apply_composition(
        self, model: OverviewModel, previous: OverviewModel | None
    ) -> None:
        """Touch the donut and the attribution panel only on change."""

        share = model.share
        old_share = previous.share if previous is not None else None
        new_totals = (
            (share.task_read_bytes, share.task_write_bytes)
            if share is not None
            else None
        )
        old_totals = (
            (old_share.task_read_bytes, old_share.task_write_bytes)
            if old_share is not None
            else None
        )
        if (
            previous is None
            or new_totals != old_totals
            or previous.share_unavailable != model.share_unavailable
        ):
            if share is not None:
                self._donut.set_shares(
                    share.task_read_bytes, share.task_write_bytes
                )
            else:
                self._donut.set_unavailable(
                    model.share_unavailable or SHARE_UNAVAILABLE_REASON
                )

        rows = _share_rows(model)
        old_rows = _share_rows(previous) if previous is not None else None
        reconciliation = _share_reconciliation_text(model)
        old_reconciliation = (
            _share_reconciliation_text(previous) if previous is not None else None
        )
        if previous is None or rows != old_rows:
            if rows is None:
                self._share_bars.set_unavailable(_share_unavailable_reason(model))
            else:
                self._share_bars.set_rows(rows)
                self._share_bars.set_reconciliation(reconciliation)
        elif old_reconciliation != reconciliation:
            self._share_bars.set_reconciliation(reconciliation)

    def _render_unavailable(self, reason: str) -> None:
        """Install one unavailable state: no list, no subject, five KPIs."""

        self._totals = {}
        self._subject_app_id = None
        self._ranking_model = None
        self._overview_model = None
        self._has_frame = True
        self._overview_body.setVisible(True)

        blocked = self._list.blockSignals(True)
        try:
            self._list.clear()
        finally:
            self._list.blockSignals(blocked)
        self._render_empty_charts(reason, f"Residual: {UNAVAILABLE} - {RESIDUAL_NOTE}")
        self._detail_state.setText(reason)

    def _render_empty(self) -> None:
        """Install the no-subject state without inventing a measurement."""

        self._subject_app_id = None
        self._overview_model = None
        self._has_frame = True
        self._overview_body.setVisible(True)
        self._render_empty_charts(
            EMPTY_RANKING_TEXT, f"Residual: {UNAVAILABLE} - {RESIDUAL_NOTE}"
        )
        window = self._active_window
        if window is None:
            self._detail_state.setText(EMPTY_RANKING_TEXT)
        else:
            self._detail_state.setText(f"window: {window.kind}; {EMPTY_RANKING_TEXT}")

    def _render_empty_charts(self, reason: str, residual: str) -> None:
        """The intersection of the unavailable and empty states."""

        for label in self._kpi_labels:
            label.setText(UNAVAILABLE)
        self._residual_value.setText(residual)
        self._coverage_label.setText(COVERAGE_UNAVAILABLE)
        self._device_reason.setText("")
        self._device_reason.setVisible(False)
        self._more_devices.setText("")
        self._more_devices.setVisible(False)
        self._total_chart.set_unavailable(reason)
        for lane in self._device_lanes:
            lane.set_unavailable(reason)
            lane.setVisible(False)
        self._donut.set_unavailable(reason)
        self._share_bars.set_unavailable(reason)

    # -- scope controls ----------------------------------------------------

    def _on_since_boot_clicked(self) -> None:
        supplied = self._since_boot
        if supplied is None:
            self._since_boot_control.setChecked(False)
            return
        self.set_window(supplied)

    def _on_range_clicked(self) -> None:
        window = self._active_window
        if (
            window is None
            or window.kind != "range"
            or self._disabled_reason is not None
        ):
            self._range_control.setChecked(False)
            return
        self.set_window(window)


__all__ = ["AppIoWindow", "QueryService", "RankingDelegate", "RankingList"]
