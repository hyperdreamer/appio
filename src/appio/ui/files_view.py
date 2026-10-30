"""The Files surface: basenames and coverage the collector can actually prove.

This view is deliberately thin. Every fact it renders comes from the shipped
Files query contract -- ``FileViewResult.table.rows`` for the rows and
``FileViewResult.coverage`` for the footer -- and it never invents one:

* ``FILE`` is the *captured basename* only. No path is reconstructed from
  ``dev``/``inode``, and a truncated or unresolved capture carries an explicit
  marker instead of a plausible-looking name.
* ``LAST`` is the stored ``last_seen_mono_ns`` rendered as an elapsed time after
  the boot that observed it. A row from another boot names that boot rather than
  comparing two monotonic clocks.
* Sorting is a *query* parameter. The table preserves the order the contract
  returned, so a column that cannot be sorted locally (``last_seen``) is still
  sorted correctly and no reply can be re-ordered or filtered on its way to the
  screen.
* An unavailable source renders ``unavailable`` plus its reason. It never
  renders zeroes, because "we could not read the file source" and "no file I/O
  happened" are different claims.
* The footer keeps counted omissions (top-100/quota), uncounted BPF map
  overflow, unknown-owner overflow, skipped non-file/unsupported-filesystem
  objects, weak identity rows and the per-hook hit counters in *separate*
  fields. A dead hook therefore reads as a hook that never fired, not as
  "no file I/O".

The module imports only ``appio.contracts`` and ``appio.ui.formatting``. It never
imports the daemon, BPF, the file runtime, or any setup/privilege module: the
Files surface is a client of the query contract, not part of the producer.
"""

from __future__ import annotations

from collections import deque
from dataclasses import fields as dataclass_fields
from typing import TYPE_CHECKING, NamedTuple

import unicodedata

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..contracts import Direction, FileRow, FileSort, HookHits, Window, WindowUnavailableError
from .formatting import UNAVAILABLE as UNAVAILABLE_TEXT
from .formatting import format_bytes

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from ..history_router import FileViewResult

__all__ = [
    "COLUMNS",
    "COLUMN_SORTS",
    "FileTableModel",
    "FilesView",
    "HOOK_NAMES",
    "LOADING_TEXT",
    "STATE_IDLE",
    "STATE_INCOMPLETE",
    "STATE_LOADING",
    "STATE_READY",
    "STATE_UNAVAILABLE",
]

#: The exact ratified column set and order from the signed-off design.
COLUMNS = ("FILE", "DISK", "READS", "WRITES", "BYTES R", "BYTES W", "LAST")

#: The contract sort key each column owns. Every column is sortable, including
#: ``LAST``/``last_seen``, which the store sorts by
#: ``(boot.boot_order, last_seen_mono_ns)`` rather than by display text.
COLUMN_SORTS: tuple[FileSort, ...] = (
    "basename",
    "dev",
    "reads",
    "writes",
    "bytes_r",
    "bytes_w",
    "last_seen",
)

#: ``bytes_total`` has no column of its own but is still a supported sort key.
SORT_KEYS: tuple[str, ...] = ("bytes_total",) + COLUMN_SORTS

#: Display name for the active sort in the caption above the table.
SORT_LABELS = {
    "bytes_total": "traffic",
    "bytes_r": "BYTES R",
    "bytes_w": "BYTES W",
    "reads": "READS",
    "writes": "WRITES",
    "basename": "FILE",
    "dev": "DISK",
    "last_seen": "LAST",
}

#: The design's standing explanation: file activity is logical VFS bytes, it is
#: never netted against task-accounted bytes, and the ranking is retained
#: observations (a per-hour top-N can hide a file that led the whole window).
FOOTER_NOTE = (
    "FILE shows the basename the kernel captured - never a reconstructed path. "
    "File activity is logical VFS bytes, so it is never subtracted from the "
    "task-accounted total. Ranking = retained observations, so a per-hour top-N "
    "can hide a file that led the full window."
)

#: The six hook counters, in the contract's declaration order.
HOOK_NAMES = tuple(field.name for field in dataclass_fields(HookHits))

STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_INCOMPLETE = "incomplete"
STATE_UNAVAILABLE = "unavailable"

LOADING_TEXT = "loading"

_ROW_HEIGHT = 22
_COLUMN_WIDTHS = (240, 90, 70, 70, 90, 90, 150)

_BASENAME_MARKERS = {"truncated": "[truncated]", "unresolved": "[unresolved]"}


class FileRequest(NamedTuple):
    """One exact Files query: all four control fields plus the window."""

    app_id: int
    window: Window
    sort: FileSort
    direction: Direction
    search: str | None


# --------------------------------------------------------------------------- #
# Pure row/footer text helpers (deterministic, so offscreen tests pin them)
# --------------------------------------------------------------------------- #


def elapsed_text(nanoseconds: int) -> str:
    """Render a boot-relative duration without a wall clock or cross-boot math."""

    if not isinstance(nanoseconds, int) or isinstance(nanoseconds, bool) or nanoseconds < 0:
        raise ValueError("elapsed nanoseconds must be a nonnegative integer")
    seconds = nanoseconds // 1_000_000_000
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def basename_text(row: FileRow) -> str:
    """The captured basename with an explicit marker for a non-resolved name.

    ``dev``/``inode`` are never joined to it: the kernel captured a basename, so
    a full path would be an invention, and the unresolved placeholder is not
    doubled.
    """

    marker = _BASENAME_MARKERS.get(row.state)
    if marker is None:
        return row.basename
    if row.basename == marker:
        return marker
    return f"{row.basename} {marker}"


def disk_text(row: FileRow) -> str:
    """The captured device, marked when the filesystem identity is only weak."""

    return f"{row.dev} [weak identity]" if row.identity_weak else row.dev


def last_seen_text(row: FileRow, window: Window) -> str:
    """The stored monotonic last observation, scoped to the boot that saw it."""

    elapsed = elapsed_text(row.last_seen_mono_ns)
    boot_id = window.boot_id
    if boot_id is not None and row.last_seen_boot_id == boot_id:
        return f"{elapsed} after boot"
    # Another boot's monotonic clock is not comparable with this window's.
    # Name the boot instead of pretending one timeline exists.
    return f"{elapsed} after boot {row.last_seen_boot_id}"


def row_texts(row: FileRow, window: Window) -> tuple[str, ...]:
    """The seven display cells for one row, in ratified column order."""

    return (
        basename_text(row),
        disk_text(row),
        str(row.reads),
        str(row.writes),
        format_bytes(row.bytes_r),
        format_bytes(row.bytes_w),
        last_seen_text(row, window),
    )


def hooks_text(hits: HookHits) -> str:
    """Every hook counter, with zero labelled as "never fired".

    A hook that never fires must not be indistinguishable from a hook that fired
    and observed no I/O, so zeros are marked rather than omitted.
    """

    parts = []
    for name in HOOK_NAMES:
        count = getattr(hits, name)
        parts.append(f"{name} {count}" + ("" if count else " (never fired)"))
    body = " · ".join(parts)
    if not any(getattr(hits, name) for name in HOOK_NAMES):
        return (
            "hook hits: no hook has fired yet - not proof of no file I/O; " + body
        )
    return "hook hits: " + body


class FileTableModel(QAbstractTableModel):
    """A read-only model over ``FileTable.rows`` in the order the store returned."""

    HEADERS = COLUMNS

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cells: list[tuple[str, ...]] = []

    # -- Qt surface -------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._cells)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._cells[index.row()][index.column()]

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return str(section + 1)

    # -- data -------------------------------------------------------------

    def set_rows(self, rows: tuple[FileRow, ...], window: Window) -> None:
        """Replace the table with the contract's rows, preserving their order."""

        self.beginResetModel()
        self._cells = [row_texts(row, window) for row in rows]
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._cells = []
        self.endResetModel()


class _ResultBridge(QObject):
    """Carries a completed future from a worker thread onto the Qt thread."""

    delivered = pyqtSignal(object)


class FilesView(QWidget):
    """Searchable, sortable, honest per-file table with a coverage footer."""

    state_changed = pyqtSignal(str)

    def __init__(self, history, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history = history
        self._window: Window | None = None
        self._app_id: int | None = None
        self._sort: FileSort = "bytes_total"
        self._direction: Direction = "desc"
        self._generation = 0
        self._state = STATE_IDLE
        self._closed = False
        self._unavailable_reason: str | None = None
        self._unavailable_message: str | None = None

        #: Test seams: the last settled result and the last issued query.
        self.last_result: FileViewResult | None = None
        self.last_request: FileRequest | None = None
        # Bounded, so a long-lived window cannot accumulate one tuple per
        # keystroke; the last 64 queries are enough to audit a session.
        self.requests: deque[FileRequest] = deque(maxlen=64)

        self.setObjectName("files_view")
        self._build_ui()
        self._bridge = _ResultBridge(self)
        self._bridge.delivered.connect(self._on_delivered)

    # -- construction -----------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_row = QVBoxLayout(controls)
        controls_row.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.app_selector = QComboBox()
        self.app_selector.setObjectName("files_app_selector")
        self.app_selector.setToolTip("Application whose retained file observations are shown.")
        self.app_selector.setAccessibleName("Application")
        self.search_field = QLineEdit()
        self.search_field.setObjectName("files_search")
        self.search_field.setPlaceholderText("search basenames (case-insensitive)")
        self.search_field.setToolTip(
            "Filters retained rows by basename. Matching is Unicode NFC and case-insensitive."
        )
        self.search_field.setAccessibleName("Search files")
        self.search_field.setClearButtonEnabled(True)
        self.direction_button = QPushButton("▼")
        self.direction_button.setObjectName("files_direction")
        self.direction_button.setToolTip("Sort direction: descending. Click to reverse.")
        self.direction_button.setAccessibleName("Reverse sort direction")
        self.direction_button.setAutoDefault(False)
        self.refresh_button = QPushButton("⟳")
        self.refresh_button.setObjectName("files_refresh")
        self.refresh_button.setToolTip("Reload the retained file observations for this window.")
        self.refresh_button.setAccessibleName("Reload file observations")
        self.refresh_button.setAutoDefault(False)
        row.addWidget(self.app_selector, 2)
        row.addWidget(self.search_field, 3)
        row.addWidget(self.direction_button)
        row.addWidget(self.refresh_button)
        self.sort_caption = QLabel("")
        self.sort_caption.setObjectName("files_sort_caption")
        self.sort_caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sort_caption.setWordWrap(True)
        # The caption must never set a floor on the page width; it wraps instead.
        self.sort_caption.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self.sort_caption, 2)
        controls_row.addLayout(row)
        self.state_label = QLabel("")
        self.state_label.setObjectName("files_view_state")
        self.state_label.setWordWrap(True)
        self.state_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.state_label.setVisible(False)
        controls_row.addWidget(self.state_label)
        outer.addWidget(controls)

        self.model = FileTableModel(self)
        self.table = QTableView()
        self.table.setObjectName("files_table")
        self.table.setModel(self.model)
        self.table.setSortingEnabled(False)  # sorting is a query, never local
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        # Unframed inside the existing page: the shell already provides the
        # only border, and no second design system is introduced.
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # The name column absorbs slack and elides; numeric columns keep an
        # opaque, fixed-ish width so a late value cannot reflow the page.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        for column, width in enumerate(_COLUMN_WIDTHS):
            self.table.setColumnWidth(column, width)
        header.sectionClicked.connect(self.sort_by_column)
        outer.addWidget(self.table, 1)

        self.footer_state = self._footer_label("files_footer_state")
        self.footer_observed = self._footer_label("files_footer_observed")
        self.footer_counted = self._footer_label("files_footer_counted")
        self.footer_uncounted = self._footer_label("files_footer_uncounted")
        self.footer_unknown_owner = self._footer_label("files_footer_unknown_owner")
        self.footer_skipped = self._footer_label("files_footer_skipped")
        self.footer_weak = self._footer_label("files_footer_weak")
        self.footer_hooks = self._footer_label("files_footer_hooks")
        self.footer_note = self._footer_label("files_footer_note")
        self.footer_note.setText(FOOTER_NOTE)
        for label in self._footer_labels():
            outer.addWidget(label)
        outer.addWidget(self.footer_note)

        self.app_selector.currentIndexChanged.connect(self._on_app_changed)
        self.search_field.textChanged.connect(self._on_search_changed)
        self.direction_button.clicked.connect(self._on_direction_clicked)
        self.refresh_button.clicked.connect(self.request_refresh)
        self.setMinimumWidth(0)
        self._update_sort_indicator()

    @staticmethod
    def _footer_label(object_name: str) -> QLabel:
        label = QLabel("")
        label.setObjectName(object_name)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _footer_labels(self) -> tuple[QLabel, ...]:
        return (
            self.footer_state,
            self.footer_observed,
            self.footer_counted,
            self.footer_uncounted,
            self.footer_unknown_owner,
            self.footer_skipped,
            self.footer_weak,
            self.footer_hooks,
        )

    # -- public interface -------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def unavailable_text(self) -> str:
        message = self._unavailable_message or "the file source is unavailable"
        if self._unavailable_reason:
            return f"{self._unavailable_reason}: {message}"
        return message

    def search(self) -> str | None:
        """The NFC-normalized search text, or ``None`` for a blank field.

        Matching itself is Unicode NFC plus casefold, performed by the shipped
        query contract; the view only normalizes the bytes it sends.
        """

        text = unicodedata.normalize("NFC", self.search_field.text()).strip()
        return text or None

    def set_window(self, window: Window) -> None:
        """Adopt a prepared window; a different window re-ranks and re-queries."""

        if window == self._window:
            return
        self._window = window
        self.last_result = None
        self.model.clear()
        self._set_state(STATE_LOADING)
        # "The file source is unavailable" outranks "no applications observed":
        # the second is a claim about apps, not about whether F could be read.
        try:
            status = self._history.file_source_status()
        except Exception as error:
            self._set_unavailable(None, str(error) or type(error).__name__)
            return
        if not getattr(status, "available", False):
            self._set_unavailable(
                getattr(status, "reason", None),
                getattr(status, "diagnostic", None) or "the file source is unavailable",
            )
            return
        self._load_ranking()
        if self._state == STATE_UNAVAILABLE:
            return
        if self._app_id is None:
            self._render_no_applications()
            return
        self.request_refresh()

    def set_app_id(self, app_id: int) -> None:
        if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
            raise ValueError("app_id must be a positive integer")
        self._app_id = app_id
        index = self.app_selector.findData(app_id)
        self.app_selector.blockSignals(True)
        try:
            self.app_selector.setCurrentIndex(index)
        finally:
            self.app_selector.blockSignals(False)
        self.request_refresh()

    def set_sort(self, sort: FileSort, direction: Direction | None = None) -> None:
        if sort not in SORT_KEYS:
            raise ValueError(f"unknown file sort key: {sort!r}")
        self._sort = sort  # type: ignore[assignment]
        if direction is not None:
            if direction not in ("asc", "desc"):
                raise ValueError(f"unknown sort direction: {direction!r}")
            self._direction = direction
        self._update_sort_indicator()
        self.request_refresh()

    def sort_by_column(self, column: int) -> None:
        """A header click selects that column's contract sort key."""

        if not 0 <= column < len(COLUMN_SORTS):
            return
        sort = COLUMN_SORTS[column]
        if self._sort == sort:
            direction: Direction = "desc" if self._direction == "asc" else "asc"
        else:
            direction = "desc"
        self.set_sort(sort, direction)

    def request_refresh(self) -> None:
        """Issue exactly one query for the current app/window/sort/direction/search."""

        if self._closed or self._window is None or self._app_id is None:
            return
        try:
            status = self._history.file_source_status()
        except Exception as error:  # a socket-backed source may fail mid-session
            self._set_unavailable(None, str(error) or type(error).__name__)
            return
        if not getattr(status, "available", False):
            self._set_unavailable(
                getattr(status, "reason", None),
                getattr(status, "diagnostic", None) or "the file source is unavailable",
            )
            return
        self._generation += 1
        generation = self._generation
        request = FileRequest(
            self._app_id, self._window, self._sort, self._direction, self.search()
        )
        self.last_request = request
        self.requests.append(request)
        for label in self._footer_labels():
            label.setText(LOADING_TEXT)
        self._set_state(STATE_LOADING)
        try:
            future = self._history.request_file_view(*request)
        except Exception as error:  # a rejected request is still an explicit state
            self._settle(generation, error=error)
            return
        future.add_done_callback(
            lambda done: self._deliver(generation, done)
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._closed = True
        super().closeEvent(event)

    # -- request plumbing -------------------------------------------------

    def _deliver(self, generation: int, future) -> None:
        """Hand a settled future to the Qt thread without touching widgets here."""

        try:
            self._bridge.delivered.emit((generation, future))
        except RuntimeError:  # the widget was destroyed while the job ran
            pass

    def _on_delivered(self, payload) -> None:
        generation, future = payload
        if self._closed or generation != self._generation:
            return  # a retired generation can never publish
        try:
            result = future.result()
        except WindowUnavailableError as error:
            self._set_unavailable(error.reason, str(error))
        except Exception as error:
            self._set_unavailable(None, str(error) or type(error).__name__)
        else:
            self._publish(result)

    def _settle(self, generation: int, *, error: BaseException) -> None:
        if generation != self._generation:
            return
        self._set_unavailable(getattr(error, "reason", None),
                              str(error) or type(error).__name__)

    # -- controls ---------------------------------------------------------

    def _on_app_changed(self, index: int) -> None:
        if index < 0:
            return
        app_id = self.app_selector.itemData(index)
        if not isinstance(app_id, int) or isinstance(app_id, bool):
            return
        self._app_id = app_id
        self.request_refresh()

    def _on_search_changed(self, _text: str) -> None:
        self.request_refresh()

    def _on_direction_clicked(self) -> None:
        direction: Direction = "desc" if self._direction == "asc" else "asc"
        self.set_sort(self._sort, direction)

    def _update_sort_indicator(self) -> None:
        header = self.table.horizontalHeader()
        direction_glyph = "▼" if self._direction == "desc" else "▲"
        self.direction_button.setText(direction_glyph)
        self.direction_button.setToolTip(
            "Sort direction: "
            + ("descending" if self._direction == "desc" else "ascending")
            + ". Click to reverse."
        )
        # A header indicator cannot express the default ``bytes_total`` sort,
        # which owns no column, so the caption always names the active key.
        self.sort_caption.setText(
            f"sorted by {SORT_LABELS[self._sort]} {direction_glyph} "
            f"({self._sort}, {self._direction})"
        )
        if self._sort not in COLUMN_SORTS:
            header.setSortIndicatorShown(False)
            return
        header.setSortIndicatorShown(True)
        header.setSortIndicator(
            COLUMN_SORTS.index(self._sort),
            Qt.SortOrder.DescendingOrder
            if self._direction == "desc"
            else Qt.SortOrder.AscendingOrder,
        )

    # -- ranking and app choice -------------------------------------------

    def _load_ranking(self) -> None:
        """Read the app selector's source once per window; never per row."""

        try:
            rows = list(self._history.ranking(self._window, "total_bytes"))
        except Exception as error:
            self._set_unavailable(getattr(error, "reason", None),
                                  str(error) or type(error).__name__)
            return
        choices = [(row.name, row.total_bytes, row.app_id) for row in rows]
        if not rows:
            local = getattr(self._history, "local", None)
            registry = getattr(self._history, "registry", None)
            if local is not None and registry is not None:
                try:
                    choices = [
                        (name, total_bytes, registry.id_for(key))
                        for key, name, total_bytes in local.file_applications(self._window)
                    ]
                except Exception as error:
                    self._set_unavailable(getattr(error, "reason", None),
                                          str(error) or type(error).__name__)
                    return
        self.app_selector.blockSignals(True)
        try:
            self.app_selector.clear()
            for name, total_bytes, app_id in choices:
                self.app_selector.addItem(
                    f"{name}  {format_bytes(total_bytes)}", app_id
                )
            if self._app_id is None and choices:
                self._app_id = choices[0][2]
            index = -1
            if self._app_id is not None:
                index = self.app_selector.findData(self._app_id)
            self.app_selector.setCurrentIndex(index)
        finally:
            self.app_selector.blockSignals(False)

    # -- rendering --------------------------------------------------------

    def _publish(self, result: FileViewResult) -> None:
        self.last_result = result
        coverage = result.coverage
        inner = coverage.coverage
        if inner.unavailable_reason is not None:
            # The router raises for these; a result carrying one is still never
            # rendered as numbers.
            self._set_unavailable(inner.unavailable_reason,
                                  "file history is not available for this window")
            return
        window = result.table.window
        self.model.set_rows(result.table.rows, window)
        self._render_coverage(result, window)
        self._set_state(STATE_READY if inner.complete else STATE_INCOMPLETE)

    def _render_coverage(self, result: FileViewResult, window: Window) -> None:
        coverage = result.coverage
        inner = coverage.coverage
        covered = coverage.unknown_owner_overflow.window
        partial_interval = (
            (covered.start_mono_ns, covered.end_mono_ns)
            if covered != coverage.window and covered.kind == "range"
            and covered.boot_id == coverage.window.boot_id
            else None
        )
        scope_prefix = (
            f"served interval only (T0={elapsed_text(partial_interval[0])} through "
            f"T1={elapsed_text(partial_interval[1])} after boot): "
            if partial_interval is not None else ""
        )
        if inner.complete:
            self.footer_state.setText(
                f"coverage: complete; tier: {inner.tier}; window: {window.kind}"
            )
        else:
            disclosure = (
                f"; served interval only (T0={elapsed_text(partial_interval[0])} through "
                f"T1={elapsed_text(partial_interval[1])} after boot); "
                f"collection has gaps in the requested window"
                if partial_interval is not None else ""
            )
            self.footer_state.setText(
                f"coverage: incomplete; tier: {inner.tier}; window: {window.kind}; "
                f"gaps: {inner.gap_intervals}; "
                f"overlaps: {inner.overlap_uncertain_intervals}; "
                f"reason: {inner.unavailable_reason or 'unspecified'}{disclosure}"
            )
        self.footer_observed.setText(
            self._observed_text(coverage, result.table)
        )
        self.footer_counted.setText(
            f"{scope_prefix}counted omissions (top-100/quota): "
            f"{coverage.counted_omitted_files} files, "
            f"{format_bytes(coverage.counted_omitted_bytes_r)} read, "
            f"{format_bytes(coverage.counted_omitted_bytes_w)} written"
        )
        # Counted omissions above and this field below are never added: one was
        # measured and dropped by a deterministic top-N, the other was lost when
        # a map insert failed and cannot be measured at all.
        self.footer_uncounted.setText(
            f"{scope_prefix}uncounted overflow (BPF map full, not measured): "
            f"{coverage.uncounted_overflow_files} files, "
            f"{format_bytes(coverage.uncounted_overflow_bytes_r)} read, "
            f"{format_bytes(coverage.uncounted_overflow_bytes_w)} written"
        )
        overflow = coverage.unknown_owner_overflow
        self.footer_unknown_owner.setText(
            f"{scope_prefix}unknown owner overflow (unassigned, never added to an app): "
            f"{overflow.overflow_files} files, "
            f"{format_bytes(overflow.overflow_bytes_r)} read, "
            f"{format_bytes(overflow.overflow_bytes_w)} written"
        )
        self.footer_skipped.setText(
            f"{scope_prefix}skipped objects (excluded, never counted as file I/O): "
            f"{coverage.skipped_nonfile} non-file, "
            f"{coverage.skipped_unsupported_fs} unsupported filesystem"
        )
        self.footer_weak.setText(
            f"{scope_prefix}weak identity rows (dev+inode only, no filesystem UUID): "
            f"{coverage.weak_identity_rows}"
        )
        self.footer_hooks.setText(scope_prefix + hooks_text(coverage.hook_hits))

    @staticmethod
    def _observed_text(coverage, table) -> str:
        """Observed bytes use the coverage scope; row count is displayed.

        A search filter narrows the table but not ``observed_bytes_*``, so the
        two facts are labelled separately rather than implying that the shown
        rows sum to the displayed byte total.
        """

        count = len(table.rows)
        noun = "row" if count == 1 else "rows"
        prefix = (
            "observed (counted, whole window): "
            f"{format_bytes(coverage.observed_bytes_r)} read, "
            f"{format_bytes(coverage.observed_bytes_w)} written"
        )
        covered = coverage.unknown_owner_overflow.window
        if (covered != coverage.window and covered.kind == "range"
                and covered.boot_id == coverage.window.boot_id):
            prefix = (
                f"observed (counted, served interval only ("
                f"T0={elapsed_text(covered.start_mono_ns)} through "
                f"T1={elapsed_text(covered.end_mono_ns)} after boot)): "
                f"{format_bytes(coverage.observed_bytes_r)} read, "
                f"{format_bytes(coverage.observed_bytes_w)} written"
            )
        if table.search:
            return f"{prefix}; table filtered to {count} {noun} matching {table.search!r}"
        return f"{prefix}; {count} retained file {noun}"

    def _render_no_applications(self) -> None:
        for label in self._footer_labels():
            label.setText(UNAVAILABLE_TEXT)
        self.footer_state.setText(
            "coverage: not applicable; no applications observed in this window"
        )
        self.last_result = None
        self.model.clear()
        self._set_state(STATE_READY)

    def _set_unavailable(self, reason: str | None, message: str) -> None:
        self.last_result = None
        self.model.clear()
        for label in self._footer_labels():
            label.setText(UNAVAILABLE_TEXT)
        self._unavailable_reason = reason
        self._unavailable_message = message
        self._set_state(STATE_UNAVAILABLE)

    def _set_state(self, state: str) -> None:
        self._state = state
        if state == STATE_UNAVAILABLE:
            self.state_label.setText(f"file history unavailable: {self.unavailable_text()}")
            self.state_label.setVisible(True)
        elif state == STATE_LOADING:
            self.state_label.setText("loading file history")
            self.state_label.setVisible(True)
        else:
            self.state_label.setText("")
            self.state_label.setVisible(False)
        self.state_changed.emit(state)
