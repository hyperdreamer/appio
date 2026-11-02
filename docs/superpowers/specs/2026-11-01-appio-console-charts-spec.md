# appio console charts: technical specification

Date: 2026-11-01. Status: implementation contract for the approved design.

## 1. Authority, provenance, and boundaries

Behavioural authority: `docs/superpowers/specs/2026-11-01-appio-console-charts-design.md`, sections 1–9. Approved commit: `f444c862e607e388271ffb8a17e059c7b7dd2d91`; design blob: `d4cb671b28ae4baf565864ef02d341c28bfdea8d`; design SHA-256: `acd2e9f81096b24bcc3a7d05eb3130945bd8da837fe9c38835ca29516512ef6d`. These identifiers were checked against this worktree before drafting.

Layout authority: `docs/superpowers/specs/assets/2026-11-01-target-state.png`, `2026-11-01-navigation.png`, and chart-set A in `2026-11-01-chart-sets.png` in that same directory. Apply the corrections in design section 9: conditional Files selector, raw device tokens, three device lanes maximum, and only provable time breaks.

All requirements below are prospective unless explicitly introduced as **Existing evidence**. A detail marked **specification decision** fills an implementation detail left open by the design; it does not change a design decision. No producer, protocol, storage, registry, query, or history-router signature changes are authorised.

### 1.1 Checked implementation facts

| Existing evidence | Consequence for this implementation |
| --- | --- |
| `src/appio/ui/main_window.py:40-48` defines a UI-local `QueryService` protocol with ranking and disk share; `:203-219` clears and recreates list items. | Extend the UI protocol for `series`; replace destructive refresh with render-model diffing. This is not a change to `appio.query.QueryService`. |
| `src/appio/ui/main_window.py:67-82` requires a since-boot window before constructing the view; `src/appio/ui/collector_window.py:744-761` constructs it only when history is ready. | Permit a persistent empty console and tabs before a boot or frame exists. |
| `src/appio/ui/collector_window.py:466-522` builds `files_nav` and two stacked pages; `:546-557` switches pages and requests Files again. | Remove that navigation structure and its extra query; retain the compatibility slot as tab selection only. |
| `src/appio/ui/collector_window.py:610-646` probes synchronously, promotes verified connection state, reconciles, samples, and renders in one timer callback. | Split worker observation from Qt-thread fact application while preserving the verified-connection promotion. |
| `src/appio/ui/collector_window.py:683-706` derives the boot from a verified cursor or the newest local boot row and rounds the clock up to the next raw bucket. | Preserve this target calculation and its trust boundary. |
| `src/appio/ui/collector_window.py:708-742` conflates requested and displayed windows and stops polling when the old view is visible. | Separate target/displayed/pending and poll readiness even with a displayed frame. |
| `src/appio/history_router.py:137-158` settles retired Files futures with `WindowUnavailableError`; `:413-421` invalidates to an unavailable view state; `:742-774` prepares a new generation. | Detect retirement from router state; protect Files from both synchronous and worker-queued retirement deliveries. |
| `src/appio/ui/files_view.py:494-522` clears on scope adoption; `:559-592` issues one future; `:608-627` drops mismatched generations and otherwise clears on failures. | Add scope admission, delivery retirement, and transient failure classification without changing the Files result contract. |
| `src/appio/ui/files_view.py:673-710` consults local file applications only when the process ranking is empty; `src/appio/query.py:939-983` returns `(app_key, name, total_bytes)` for local file choices. | Populate the union even in a mixed process/file window. |
| `src/appio/contracts.py:430-458` gives task buckets duration, bytes, optional peaks, and a suspend flag; `:461-476` gives device buckets no suspend or boot field; `:497-517` gives `Series` no gap ranges; `:521-541` scopes device-share tokens by boot. | Do not infer missing gap locations, device suspension breaks, device names, mount points, or cross-boot chronology. |
| `src/appio/query.py:642-678` supplies series through the existing query contract, and `:680-717` supplies task/device totals and signed residuals. | Consume these results; add no measurements. |
| `src/appio/ui/formatting.py:30-49` formats binary byte units using integer arithmetic; `:52-75` preserves undefined peaks and residuals as `unavailable`; `:78-89` supplies A22 reasons. | Reuse these formatters and exact reasons. |

## 2. Render dataclasses and pure builders

### 2.1 Common construction rules

All seven dataclasses below use `@dataclass(frozen=True)` and have `__post_init__(self) -> None`. **specification decision:** use explicit `ValueError` for invalid values, including malformed nested contents, rather than assertions. Integers exclude `bool`. Numeric quantities stated as nonnegative must be finite integers at least zero; app IDs must be positive. Collections are tuples, not mutable lists. Optional text is either `None` or a nonempty string. No model normalises unavailable quantities to zero.

The dataclasses in this section have exactly the fields shown. No stored QWidget, history object, connection, future, painter, or mutable mapping may be added to them.

```python
@dataclass(frozen=True)
class RankingRow:
    app_id: int
    app_key: str
    label: str
    total_bytes: int
    share: float

@dataclass(frozen=True)
class RankingModel:
    rows: tuple[RankingRow, ...]
    empty_text: str | None
    unavailable: str | None

@dataclass(frozen=True)
class Kpi:
    caption: str
    text: str

@dataclass(frozen=True)
class DeviceLaneModel:
    dev: str
    points: tuple[DeviceSeriesPoint, ...]
    read_bytes: int
    write_bytes: int

@dataclass(frozen=True)
class OverviewModel:
    app_id: int
    kpis: tuple[Kpi, ...]
    task_points: tuple[SeriesPoint, ...]
    device_lanes: tuple[DeviceLaneModel, ...]
    more_devices: int
    series_unavailable: str | None
    share: DiskShare | None
    share_unavailable: str | None
    reconciliation: str | None
    coverage_line: str
    detail_line: str

@dataclass(frozen=True)
class ChartColumn:
    start_mono_ns: int
    end_mono_ns: int
    read_bytes: int
    write_bytes: int
    peak_read_bps: int | None
    peak_write_bps: int | None
    starts_run: bool

@dataclass(frozen=True)
class ChartSegment:
    side: Literal["read", "write"]
    run_index: int
    polygon: tuple[tuple[float, float], ...]
```

`RankingRow`, `RankingModel`, `Kpi`, `DeviceLaneModel`, and `OverviewModel` live in `render_model.py`; `ChartColumn` and `ChartSegment` live in `charts.py`. **specification decision:** `ChartSegment` stores widget-local logical-pixel polygon coordinates, not QPoints or a painter path.

| Class | Invariants and rejection rules |
| --- | --- |
| `RankingRow` | Positive ID; nonempty UTF-8-encodable key and label; nonnegative total; finite `float` share in `[0.0, 1.0]`. Reject NaN, infinity, booleans, negative totals, and empty identity. The row alone cannot verify the denominator; the containing model does. |
| `RankingModel` | Unique app IDs and keys, sorted by `(-total_bytes, app_key.encode("utf-8"))`. Shares equal `total_bytes / largest_total`, or `0.0` for every row when the largest is zero, within `1e-12`. Nonempty rows imply both text fields are `None`. An empty success has `empty_text == "no applications observed"` and `unavailable is None`. An unavailable model has no rows, nonempty `unavailable`, and `empty_text is None`. Reject conflicting states or duplicate identities. |
| `Kpi` | Caption is exactly one of `TOTAL`, `READ`, `WRITE`, `PEAK`, `DEVICE`; text is nonempty. Reject unknown captions and empty text. A builder, not string parsing in this class, decides whether the value is measured or unavailable. |
| `DeviceLaneModel` | Nonempty raw `dev`; nonnegative read/write totals; every point has that same device, and points have strictly increasing starts and nonoverlapping bucket intervals. Gaps are allowed. Reject mixed devices, unsorted/overlapping points, or invalid nested types. Totals are the device-share totals, not a recomputation from possibly retained/partial points. |
| `OverviewModel` | Positive subject ID; exactly five KPIs in the fixed caption order above; at most three distinct device lanes, sorted by descending `read_bytes + write_bytes`, then UTF-8 device token; nonnegative `more_devices`. Task points are ordered, nonoverlapping contract points. `series_unavailable` implies no task points, no device lanes, and `more_devices == 0`. `share_unavailable` implies `share is None` and `reconciliation is None`. A present share has the subject ID; `coverage_line` and `detail_line` are nonempty strings. Device lanes require a present share with available attribution and a defined device total; `more_devices > 0` requires exactly three displayed lanes. Reconciliation is allowed only with available device attribution and defined device totals/residual. Reject conflicting availability, duplicate lanes, extra KPIs, or malformed points. Empty-but-available series may have no points and `series_unavailable is None`. |
| `ChartColumn` | `0 <= start_mono_ns < end_mono_ns`; nonnegative byte totals; peaks are nonnegative integers or `None`; `starts_run` is a real boolean. A column may cover several source runs: its temporal envelope is not proof of continuity. Reject invalid endpoints, negative peaks/bytes, or nonboolean run flags. |
| `ChartSegment` | Side is read/write; run index is nonnegative; polygon has at least four finite `(x, y)` float pairs and closes by repeating its first point. Reject invalid side/index, NaN/infinity, malformed tuples, or open polygons. Widget geometry additionally obeys section 6; it cannot be validated from this dataclass alone. |

### 2.2 `src/appio/ui/render_model.py` surface

Imports are limited to the standard library, `appio.contracts`, and `appio.ui.formatting`. No PyQt6 import.

```python
def build_ranking(
    rows: Sequence[AppTotal], *, unavailable: str | None = None
) -> RankingModel: ...

def build_overview(
    app_id: int,
    window: Window,
    total: AppTotal | None,
    series: Series | None,
    share: DiskShare | None,
    *,
    ranking_error: str | None = None,
    series_error: str | None = None,
    share_error: str | None = None,
) -> OverviewModel: ...

def scope_text(window: Window) -> str: ...
```

These exact builder argument shapes are **specification decisions**. `scope_text` returns the `Window.kind` token; the existing detailed Files scope descriptions remain separate.

- `build_ranking`: validate input contracts/identities, sort deterministically, make labels `f"{row.name}  {format_bytes(row.total_bytes)}"`, and derive shares from the largest total. With `unavailable`, require empty input and return only the error state; do not disguise failed ranking as empty success.
- `build_overview`: validate matching IDs and exact `window` on successful series/share inputs; reject an object supplied together with its error argument. No queries or I/O. Failed inputs are independently represented so a share failure cannot suppress a successful task series.
- Five KPI sources: `TOTAL/READ/WRITE` are `share.task_total_bytes/task_read_bytes/task_write_bytes`; `PEAK` is `format_peak(total.peak_total_bps)`; `DEVICE` is `share.device_total_bytes` only when `share.coverage.device_state == "available"` and the total is not `None`. Format the remaining numeric byte values with `format_bytes`. A share failure makes its four dependent byte KPIs unavailable; a known ranking peak may remain. A series failure does not invalidate otherwise measured KPIs.
- When `total is None`, return five unavailable KPIs, empty lanes, `series_unavailable == "not in the process ranking"`, no share or reconciliation, and that same reason in `detail_line`. The window uses this for a file-only subject; an empty list without a subject uses the separate empty state. `ranking_error` takes precedence over this file-only reason.
- A ranking error yields five unavailable KPIs and that reason for every chart. A missing series without a supplied reason defaults to `series unavailable`; a missing share defaults to `task/device split unavailable`. An empty successful series is available but has no shapes and the caption `no samples in this window`.
- Device lanes require available attribution in both successful series and share coverage, a defined share device total, and available single-boot chronology. Select active share rows where total bytes are positive, keep each raw device token, associate the same-device series points, order by descending share total then token, retain three, and set `more_devices = max(0, active_device_count - 3)`. A selected device without retained points has a caption and no shape. **specification decision:** when the lane stack is unavailable, return no device lanes and `more_devices == 0`; there are no displayed lanes to qualify with an additional-device count. Do not invent zero buckets.
- **specification decision:** chronological lanes are unavailable for a lifetime `Window`: points have no per-point boot identity. Use `lifetime chronology unavailable`; keep valid totals, donut, boot-qualified share bars, and coverage. Do not sort unrelated boots' monotonic clocks into a continuous trace.
- With a defined available device total and residual, `reconciliation` is the exact template in section 7. Otherwise it is `None`; the strip displays the device/share reason separately. Preserve negative residuals and their existing wording. Do not subtract logical file bytes.
- **specification decision:** coverage text uses the successful share coverage, otherwise series coverage, otherwise ranking coverage; it reports that source's complete flag, device state, tier, gaps, suspend flag, and reason. If another successful source has different coverage, append its complete coverage description to `detail_line`, prefixed with its source name; never silently combine counts from different contracts.

## 3. Module surface and widget ownership

Only symbols listed here and in sections 2, 4, and 5 change. Unlisted consent, settings, helper-command, formatting, table-column, file-result, and collector APIs keep their contracts. Private implementation decomposition may introduce ordinary local variables, but no extra cross-module service or lifecycle API is required.

### 3.1 `src/appio/ui/theme.py`

**specification decision:** expose these constants, with no theme manager or settings:

```python
READ_COLOR: QColor = QColor("#397fd1")
WRITE_COLOR: QColor = QColor("#e47e38")
TEXT_COLOR: QColor = QColor("#25303d")
MUTED_COLOR: QColor = QColor("#63758a")
GRID_COLOR: QColor = QColor("#d5deea")
TRACK_COLOR: QColor = QColor("#e6ecf3")
BORDER_COLOR: QColor = QColor("#cbd7e5")
AXIS_PEN_WIDTH: float = 1.0
BAR_HEIGHT: int = 6
PLOT_MARGIN: int = 8
GUTTER_WIDTH: int = 144
```

These are shared drawing values, not an application-wide stylesheet. Import PyQt6 and the standard library only; contracts may be imported if needed. Never import a producer or history/setup implementation. Leave unrelated table and dialog styling unchanged.

### 3.2 `src/appio/ui/charts.py`

Dependencies: standard library, PyQt6, contracts, and the local theme constants. `Sequence` is from `typing`/`collections.abc`; events are `QPaintEvent` and `QResizeEvent`. Define the two chart dataclasses from section 2.

```python
class SeriesChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None: ...
    def set_task_points(self, points: Sequence[SeriesPoint]) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def columns(self) -> tuple[ChartColumn, ...]: ...
    def segments(self) -> tuple[ChartSegment, ...]: ...
    def is_available(self) -> bool: ...
    def unavailable_reason(self) -> str | None: ...
    def plot_rect(self) -> tuple[float, float, float, float]: ...
    def paintEvent(self, event: QPaintEvent) -> None: ...
    def resizeEvent(self, event: QResizeEvent) -> None: ...

class DualSeriesChart(SeriesChart):
    def __init__(self, parent: QWidget | None = None) -> None: ...
    def set_totals(self, read_bytes: int, write_bytes: int) -> None: ...

class DeviceLane(DualSeriesChart):
    def __init__(self, parent: QWidget | None = None) -> None: ...
    def set_device_points(self, points: Sequence[DeviceSeriesPoint]) -> None: ...
    def set_device(self, dev: str) -> None: ...

class DonutChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None: ...
    def set_shares(self, read_bytes: int, write_bytes: int) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def shares(self) -> tuple[float, float] | None: ...
    def unavailable_reason(self) -> str | None: ...
    def paintEvent(self, event: QPaintEvent) -> None: ...

class ShareBars(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None: ...
    def set_rows(self, rows: Sequence[tuple[str, int]]) -> None: ...
    def set_reconciliation(self, text: str | None) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def fractions(self) -> tuple[tuple[str, float], ...]: ...
    def unavailable_reason(self) -> str | None: ...
    def paintEvent(self, event: QPaintEvent) -> None: ...
```

One-line contracts:

| Symbol | Contract |
| --- | --- |
| `SeriesChart.__init__` | Build an empty, available chart with no columns or segments. |
| `set_task_points` | Validate ordered, nonoverlapping task buckets, replace immutable source data, clear the unavailable reason, and rebuild geometry once. |
| `set_unavailable` (all chart classes) | Require nonempty reason, discard prior shapes/composition values, expose the reason, and schedule one repaint. |
| `columns` | Return exactly the current size's aggregated columns without mutation or I/O. |
| `segments` | Return the exact closed polygons the next paint uses, including split runs. |
| `is_available` | Distinguish an available empty/zero series from an unavailable series. |
| `unavailable_reason` | Return `None` for available data, otherwise the exact supplied reason. |
| `plot_rect` | Return `(left, top, right, bottom)` in logical pixels for the current plotting area, clamping collapsed extents so right >= left and bottom >= top. |
| `paintEvent` | Paint cached geometry, labels, and state only; never query, mutate source data, or allocate one object per source point. |
| `resizeEvent` | Recompute size-dependent columns/polygons from retained immutable inputs and schedule repaint. |
| `DualSeriesChart.__init__` | Use the primitive's centre-line read/write geometry with a right gutter. |
| `set_totals` | Validate nonnegative exact totals for gutter labels independently of retained plotted samples. |
| `DeviceLane.__init__` | Build the same geometry with a raw-device caption. |
| `set_device_points` | Convert device bucket byte fields into the common geometry input without adding suspend metadata. |
| `set_device` | Set a nonempty raw device-token caption; never resolve a friendly name or mount. |
| `DonutChart.__init__` | Start available and empty; create the local state label. |
| `set_shares` | Validate nonnegative integer task bytes, retain exact bytes for labels, and calculate composition fractions. |
| `shares` | Return read/write fractions, `(0.0, 0.0)` for measured zero/zero, or `None` when unavailable. |
| `ShareBars.__init__` | Start available with no rows and no reconciliation annotation. |
| `set_rows` | Preserve input device-row order and derive widths relative to the largest nonnegative row total. |
| `set_reconciliation` | Set/clear the panel's task/device accounting annotation without changing row data. |
| `fractions` | Return `(label, relative_width)` for every row, or an empty tuple when unavailable/empty. |

`plot_rect`, `set_totals`, `set_device`, `set_reconciliation`, and the zero-donut convention are **specification decisions**. All QWidget subclasses use a child QLabel named `chart_message` for visible empty/unavailable text, so smoke tests can observe actual rendered text as well as geometry. An unavailable setter clears stale numeric gutter/composition labels.

### 3.3 `src/appio/ui/main_window.py`

Keep the UI-local `QueryService` protocol with the exact surface below; ranking and disk share retain the signatures checked at `src/appio/ui/main_window.py:40-48`. `ranking` returns process totals for the exact window/metric; `disk_share` returns one subject's task/device split and signed residual; the added `series` returns that subject's contract buckets for the exact window. Structural fakes may supply only this process-query protocol; their Files tab reports the normal explicit source-unavailable state if file-query capabilities are absent. Do not import the concrete history router.

```python
class QueryService(Protocol):
    def ranking(self, window: Window, metric: str) -> Sequence[AppTotal]: ...
    def disk_share(self, app_id: int, window: Window) -> DiskShare: ...
    def series(self, app_id: int, window: Window) -> Series: ...

class RankingDelegate(QStyledItemDelegate):
    def __init__(self, parent: QObject | None = None) -> None: ...
    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None: ...
    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize: ...

class AppIoWindow(QMainWindow):
    def __init__(
        self, query_service: QueryService,
        boot_id: str | None = None, end_mono_ns: int | None = None,
        *, files_view: FilesView | None = None,
    ) -> None: ...
    def _build_ui(self) -> None: ...
    def refresh(self) -> None: ...
    def set_window(self, window: Window) -> None: ...
    def selected_app_id(self) -> int | None: ...
    def _apply_ranking(self, model: RankingModel) -> None: ...
    def _apply_overview(self, model: OverviewModel) -> None: ...
    def _set_subject(self, app_id: int | None, *, request_files: bool) -> None: ...
    def _on_current_item_changed(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None: ...
    def _on_files_subject_changed(self, app_id: int | None) -> None: ...
    def _on_since_boot_clicked(self) -> None: ...
    def _on_range_clicked(self) -> None: ...
    def _load_selected(self, app_id: int) -> None: ...
    def _render_unavailable(self, reason: str) -> None: ...
    def _render_empty(self) -> None: ...
    def show_overview_status(self, message: str, *, keep_frame: bool) -> None: ...
    @property
    def has_frame(self) -> bool: ...
```

- `RankingDelegate` constructor installs no timers; `paint` uses the Qt style for normal/selected text/background plus a bottom inline track and fill from `UserRole + 1`; `sizeHint` reserves one bar row under the text. Reject no input by parsing labels: labels are never a data source.
- Constructor: **specification decision**, optional boot/end must be both absent or both valid. Both absent create the persistent two-tab host without querying. Both present preserve the existing standalone initial since-boot render. An injected FilesView is reused exactly once; otherwise construct one owned FilesView. Track `_owns_files_scope: bool` so standalone scope syncing and shell scope syncing cannot both own queries.
- `_build_ui`: create the ranking list and `detail_tabs: QTabWidget` named `detail_tabs` with exactly `Overview`, `Files`. Overview owns KPIs, TOTAL, up to three reusable DeviceLane widgets, DonutChart, ShareBars, the reconciliation strip, coverage/detail/status labels. Files owns exactly one FilesView and the existing message/settings-link surface. Build once, including before the first sample. Preserve existing KPI/list object names checked at `src/appio/ui/main_window.py:109-160`.
- Keep `_subject_app_id: int | None` as the sole subject, `_ranking_model: RankingModel | None`, `_overview_model: OverviewModel | None`, and `_totals: dict[int, AppTotal]` for the active contract rows. `selected_app_id()` returns the subject, including a file-only subject. Do not keep a second selected-ID field. **specification decision:** `_active_window: Window | None` and `_since_boot: Window | None` begin as None in the empty constructor. Before any active window exists, `refresh` makes no queries and both scope handlers return safely; the Since boot control stays disabled until an actual since-boot window is supplied. `set_window` records a supplied since-boot window for that control. An explicit range does not invent a since-boot scope.
- `refresh`: query ranking once for the active prepared window, build/apply ranking, then load the shared ranked subject's series and share independently, each at most once in that refresh. If choosing a changed subject already invoked `_load_selected`, do not invoke it a second time. Catch each failure at its dependency boundary. No Files refresh on repeated process redraws; no tab-index handler calls `set_window`, `prepare`, a collector operation, or `request_refresh`.
- `_apply_ranking`: compare `(app_id, label, total_bytes, share)`. If ID order is unchanged, preserve every item object and selection; set text only when changed, stored share only when changed, and repaint that row's viewport rectangle when share changed. Never clear/reset or call `setCurrentRow` in this branch. Store IDs at `UserRole`, shares at `UserRole + 1`.
- If ID order changes, capture scroll value and subject, block list signals, rebuild, restore the subject row or clear both current index and selection if absent, restore the scroll value (clamped only if Qt's new maximum is smaller), unblock signals. Scroll preservation also applies when clearing a file-only subject's highlight.
- `_set_subject`: validate a positive ID or `None`, update one subject, align highlight without feedback, load the appropriate Overview, and synchronise Files. **specification decision:** its `request_files` parameter distinguishes user selection from frame-application synchronisation. User ranking selection uses `True`; frame refresh and a Files selector signal use `False` to avoid a second file request. Unchanged subject is an idempotent no-op. For None, clear the Overview/highlight without calling the positive-ID-only `FilesView.set_app_id`; an empty-union Files notification has already cleared its own subject.
- `FilesView.subject_changed` is connected once to `_on_files_subject_changed`; that slot changes the subject/highlight/Overview but never reissues the query already owned by the selector. A None notification clears the shared subject after the union becomes empty. When the subject is absent from ranking but exists among file choices, keep it, clear highlight, and render `not in the process ranking`. Otherwise retain an existing subject when still valid, fall back to the first ranked choice, then the first file-only choice, then `None`.
- `_load_selected`: call `series` and `disk_share` independently only for ranked subjects, build/apply an OverviewModel, and leave Files selection/query ownership alone. A failed share still permits the task lane to draw. A failed series never converts a known KPI into an invented zero.
- `_apply_overview`: equality-diff the frozen model and touch only changed text/data; stable widget identity across ticks. A pending preparation uses `show_overview_status(..., keep_frame=True)` and does not call any chart unavailable setter.
- `set_window`: apply the existing A22 guard before queries; retain the exact requested window. In embedded mode it renders ranking/Overview only; shell `_sync_files` owns Files adoption. In standalone mode, synchronise the owned FilesView once after subject selection and flush only if adoption did not already query. This division is a **specification decision** to keep the design's one-owner rule.
- `_render_unavailable`: install an unavailable ranking state, five unavailable KPIs, and unavailable charts with the supplied reason. `_render_empty`: use no subject, five unavailable KPIs, no shapes, and `no applications observed`; when an active window exists, preserve the detail label `window: <kind>; no applications observed`. Do not manufacture an app ID of zero.
- `show_overview_status`: render the supplied status inside Overview; with `keep_frame=True`, preserve every data widget and its state. With `False`, show the initial placeholder while retaining the tab host and Files navigation. `has_frame` reports whether an applied Overview frame, including an applied empty/error frame, exists.
- Remove the old `_render_share` implementation; its duties move to the builder and `_apply_overview`. Keep `_scope_control` and the existing A22 semantics of `_on_since_boot_clicked` and `_on_range_clicked` (`src/appio/ui/main_window.py:182-189,239-269,286-294`), adding only the None guards required by empty construction. The former reapplies a supplied since-boot scope; the latter reapplies only an existing supported range. Do not enable a new range-chip feature.

### 3.4 `src/appio/ui/files_view.py`

Keep imports producer-free. Use `TYPE_CHECKING` for the existing FileViewResult annotation, following `src/appio/ui/files_view.py:58-59`. The `history` argument remains a structural object, not a new concrete-router dependency.

```python
class FilesView(QWidget):
    state_changed = pyqtSignal(str)
    subject_changed = pyqtSignal(object)  # positive int, or None for an empty union
    def __init__(self, history: object, parent: QWidget | None = None) -> None: ...
    def _build_ui(self) -> None: ...
    def set_transient_scope(self, transient: bool) -> None: ...
    def transient_scope(self, retire_on_exit: bool = False) -> ContextManager[None]: ...
    def set_scope_ready(self, ready: bool) -> None: ...
    def set_updating_scope(self, window: Window | None) -> None: ...
    def retire_deliveries(self) -> None: ...
    def refresh_wanted(self) -> bool: ...
    def flush_refresh(self) -> None: ...
    def set_window(self, window: Window) -> None: ...
    def set_app_id(self, app_id: int, *, request: bool = True) -> None: ...
    def set_sort(self, sort: FileSort, direction: Direction | None = None) -> None: ...
    def request_refresh(self) -> None: ...
    def _load_choices(self) -> None: ...
    def _on_app_changed(self, index: int) -> None: ...
    def _on_delivered(self, payload: tuple[int, Future[FileViewResult]]) -> None: ...
    def _settle(self, generation: int, *, error: BaseException) -> None: ...
    def _handle_failure(self, reason: str | None, message: str) -> None: ...
    def _publish(self, result: FileViewResult) -> None: ...
    def closeEvent(self, event: QCloseEvent) -> None: ...
```

The generator implementation of `transient_scope`, under `@contextmanager`, has `Iterator[None]` as its generator return annotation; its decorated public call returns `ContextManager[None]`. The optional `request` parameter, `set_updating_scope`, `_handle_failure`, and nullable subject notification are **specification decisions**. `__init__` initialises the fields in section 5 and builds/connects the existing table controls and result bridge once; `_build_ui` adds the updating label without replacing the table/footer or wiring tab changes to queries. `subject_changed` emits only a positive int or None, the latter allowing an empty union to clear the shared window subject while `set_app_id` keeps its existing positive-integer validation. Their remaining contracts and all other Files changes are in section 5. Existing search normalisation, sort vocabulary, query keys, footer quantities, and table columns remain those in `src/appio/ui/files_view.py:100-146,484-492,645-669,737-803`.

### 3.5 `src/appio/ui/collector_window.py`

Keep the existing constructor signature (`src/appio/ui/collector_window.py:405-414`) and Settings/consent APIs. Add a single-worker `ThreadPoolExecutor` and a dedicated queued signal bridge for probe results; retain the existing helper-result bridge.

```python
@dataclass(frozen=True)
class ProbePayload:
    facts: ServiceFacts | None
    error: str | None
    ticket: int
    generation: int

class _ProbeBridge(QObject):
    delivered = pyqtSignal(object)

class CollectorWindow(QMainWindow):
    def __init__(
        self, coordinator: CollectorCoordinator, history: HistoryRouter,
        setup: SetupClient, *, clock_ns: Callable[[], int] | None = None,
        pump: Callable[[], None] | None = None, refresh_interval_ms: int = 1000,
    ) -> None: ...
    def _build_ui(self) -> None: ...
    def refresh(self) -> None: ...
    def _submit_probe(self, *, replace_pending: bool = False) -> bool: ...
    def _probe_completed(
        self, ticket: int, generation: int, future: Future[ServiceFacts]
    ) -> None: ...
    def _apply_facts(self, payload: ProbePayload) -> None: ...
    def _history_retired(self) -> bool: ...
    def _tick_target(self) -> None: ...
    def apply_ready_frame(self) -> bool: ...
    def show_window(self, window: Window) -> None: ...
    def _apply_frame(self) -> None: ...
    @property
    def _prepared(self) -> Window | None: ...
    @property
    def facts_applied(self) -> int: ...
    def _poll_history(self) -> None: ...
    def _show_ready(self, window: Window) -> None: ...
    def _ensure_view(self, window: Window | None = None) -> AppIoWindow: ...
    def _hide_view(self) -> None: ...
    @property
    def view(self) -> AppIoWindow | None: ...
    @property
    def history_visible(self) -> bool: ...
    def _sync_files(self, *, request: bool = False) -> None: ...
    def _files_status_text(self) -> str | None: ...
    def open_files_unavailable(self) -> None: ...
    def _quiesce_local(self) -> bool: ...
    def _start(
        self, verb: str, on_success: Callable[[SetupReply], None] | None
    ) -> None: ...
    def _on_reply(self, future: Future[SetupReply]) -> None: ...
    def _fail(self, failure: SetupReply | str) -> None: ...
    def _after_enable(self, reply: SetupReply) -> None: ...
    def _after_disable(self, reply: SetupReply) -> None: ...
    def _after_status(self, reply: SetupReply) -> None: ...
    def shutdown(self) -> None: ...
```

- `__init__` preserves the existing constructor inputs, initialises section 4's state and one probe executor/bridge, then builds the persistent host and wires the two existing timer roles without synchronously probing.
- `_build_ui` creates banner/settings link, error label, one persistent AppIoWindow and its two tabs; it injects exactly one shell-owned FilesView. Remove `files_nav`, its connection, the outer QStackedWidget, and independent history/files stack pages. Expose shell aliases `files_view`, `files_message`, `files_open_settings`, and `history_state` to the corresponding widgets inside the tab host. Preserve Settings control names and deep links.
- `_ensure_view` returns the existing view, constructing the empty host only if not yet constructed; it never prepares or renders a window. `_hide_view` hides only initial Overview data/placeholder content, never the console/tab host or a previously displayed replacement frame. `view` retains its optional annotation for callers; after construction it is present. `history_visible` reports an applied displayed frame, not mere visibility of the persistent host.
- `_prepared` is a getter-only alias returning `_displayed`; no setter and no backing requested-scope field. `facts_applied` is a getter-only observation of `_facts_applied`.
- `_show_ready` is private to `_apply_frame`: render the given prepared window through `view.set_window`, hide the Overview loading label, and call `_sync_files(request=False)`. It never assigns `_displayed` or starts a preparation.
- `_sync_files` adopts `_prepared` only when ready and not pending; it does not hide an existing table during replacement. Its legacy `request=True` requests an unchanged displayed scope only if no adoption already issued that request. The navigation slot never passes `True`.
- `_files_status_text` gives the existing degraded-source message priority before the first frame when a target/pending scope exists; if no scope exists, use `waiting for the first sample`. Once a frame exists and replacement is pending, leave the table visible and let its updating footer explain the pending scope; transient router retirement is not a terminal source outage.
- `open_files_unavailable` selects the Files tab, updates message visibility, and performs no query, ownership operation, or helper invocation. It is safe before the first sample.
- `_poll_history` invokes `apply_ready_frame` even while the old frame is visible; no target computation or probe. The 100 ms poll timer is active only while pending; refresh remains the 1 s target/probe tick. Both are stopped on close.
- `_quiesce_local` wraps its existing `reconcile(QUIESCE_FACTS)` in Files transient classification. `_after_disable` wraps `accept_export` likewise. Preserve their existing errors and no-elevation/no-import failure guarantees (`src/appio/ui/collector_window.py:814-826,935-947`).
- `_start`, `_on_reply`, `_submit_probe`, `_probe_completed`, `_apply_facts`, and `shutdown` obey section 4. `_fail`, `_after_enable`, `_after_disable`, and `_after_status` retain their helper/choice/error effects, but post-helper probe scheduling has one owner: `_on_reply`'s completion path. Remove their redundant synchronous-refresh dependencies. They may ask for a tick; they must not cause duplicate completion probes.
- Remove `_render_history(keep_pending=...)`; its decision/apply responsibilities move to `_tick_target` and `apply_ready_frame`. Keep `_current_window`, `_render`, `_show_error`, and `_clear_error` semantics checked at `src/appio/ui/collector_window.py:664-706`.

## 4. Shell state machine and probe arbitration

### 4.1 Exact fields

```python
_target: Window | None = None
_displayed: Window | None = None
_pending: Window | None = None
_facts: ServiceFacts | None = None
_dropped_facts: int = 0
_facts_applied: int = 0
_probe_inflight: Future[ServiceFacts] | None = None
_probe_ticket: int = 0
_setup_generation: int = 0
_invalidated: bool = False
```

Additional mechanical fields: `_closed: bool`, `_busy: bool`, `_probe_executor: ThreadPoolExecutor`, `_probe_bridge: _ProbeBridge`, `_probe_successor: Future[ServiceFacts] | None`, `_prepare_retry: PrepareRetry | None`, and `_prepare_error: str | None`. Ticket/generation/counters are nonnegative unbounded Python integers; no wraparound. All shell fields are read/written on the Qt thread except that a worker receives immutable captured ticket/generation values. `_invalidated` is a cached derivation of router state, not a latch set by individual invalidation call sites.

```python
@dataclass(frozen=True)
class PrepareRetry:
    window: Window
    owner: Literal["clock", "explicit"]
    since: int
```

`PrepareRetry.since` is the nonnegative monotonic timestamp captured by `_clock_ns()` when the failed preparation is recorded. It is diagnostic state, not permission for another caller to retry.

`_history_retired()` returns `self.history.view_state.startswith("unavailable")`. Existing router evidence for this predicate is `src/appio/history_router.py:413-421,742-757`.

### 4.2 Target/preparation/apply algorithms

The following ordering is normative. `_tick_target`, `show_window`, and apply paths return immediately when `_closed`. No method below processes the Qt event loop.

```python
def _tick_target(self) -> None:
    if self._closed:
        return
    if self._pending is not None and self._history_retired():
        self._pending = None
    self._target = self._current_window()
    retry = self._prepare_retry
    if self._target is None:
        if retry is not None and retry.owner == "explicit":
            self.files_view.set_scope_ready(False)
            self._invalidated = False
            self._sync_files()
            # The explicit caller still owns the failed scope and its retry.
            return
        self._displayed = None
        self._pending = None
        self._prepare_retry = None
        self._prepare_error = None
        self._invalidated = False
        self.files_view.set_scope_ready(True)
        self._sync_files()
        # Overview: waiting for the first sample; tabs remain available.
        return
    if retry is not None and retry.owner == "explicit":
        self.files_view.set_updating_scope(retry.window)
        self.files_view.set_scope_ready(False)
        self._invalidated = self._history_retired() and self._displayed is not None
        return
    retrying = retry is not None and retry.owner == "clock"
    self.files_view.set_scope_ready(self._pending is None and not retrying)
    self._invalidated = self._history_retired() and self._displayed is not None
    if self._pending is None and (
        retrying or self._invalidated or self._displayed is None or self._target != self._displayed
    ):
        prepare_window = self._target
        self.files_view.set_updating_scope(prepare_window)
        self.files_view.set_scope_ready(False)
        try:
            with self.files_view.transient_scope(retire_on_exit=True):
                self.history.prepare(prepare_window)
        except Exception as error:
            if self.history.view_state != "loading":
                raise
            # The router may be left in loading after prepare() has failed.
            self._pending = None
            self._prepare_retry = PrepareRetry(
                prepare_window, "clock", self._clock_ns()
            )
            self._prepare_error = f"appio: {error}"
            self._show_error(self._prepare_error)
            return
        self._prepare_retry = None
        self._prepare_error = None
        self._pending = prepare_window
        # Start the readiness poll; initial loading or in-place updating label.

def apply_ready_frame(self) -> bool:
    if self._closed:
        return False
    if self._prepare_retry is not None and self._prepare_retry.owner == "explicit":
        return False
    if self._pending is None:
        return False
    if self._history_retired():
        self._pending = None
        return False
    if self.history.ready(self._pending):
        self._apply_frame()
        return True
    return False

def show_window(self, window: Window) -> None:
    if self._closed:
        return
    self._prepare_retry = None
    self._prepare_error = None
    self.files_view.set_updating_scope(window)
    self.files_view.set_scope_ready(False)
    try:
        with self.files_view.transient_scope(retire_on_exit=True):
            self.history.prepare(window)
    except Exception as error:
        if self.history.view_state != "loading":
            raise
        self._pending = None
        self._prepare_retry = PrepareRetry(window, "explicit", self._clock_ns())
        self._prepare_error = f"appio: {error}"
        self._show_error(self._prepare_error)
        raise
    self._prepare_retry = None
    self._prepare_error = None
    self._pending = window
    # Arm the same readiness poll and status display. Do not assign _target.


def _apply_frame(self) -> None:
    # Precondition: _pending is not None and ready(_pending) has just succeeded.
    window = self._pending
    self._displayed = window
    self._pending = None
    self._invalidated = False
    self.files_view.retire_deliveries()
    self.files_view.set_scope_ready(True)
    self._show_ready(window)
    self.files_view.flush_refresh()
    # Clear updating labels after adoption; stop the readiness poll.
```

**specification decision:** operational exceptions in these algorithms are caught by `refresh`/`_poll_history` and shown with `_show_error(f"appio: {error}")`; do not clear the last frame or synthesize readiness. A `history.prepare()` failure after the router has set `view_state = "loading"` (the existing reset occurs at `src/appio/history_router.py:757`, and the file-transaction, file-backed-source, and executor submission failures are at `:763`, `:766`, and `:769`) enters the shell's named `preparation_error` state. Exceptions from the pre-loading Window validation remain the existing validation errors rather than entering this state (`src/appio/history_router.py:745`). `_pending` remains `None`, `_displayed` and every rendered data widget remain unchanged, `_prepare_retry` records a `PrepareRetry` marker, and Files admission remains closed; the router's `loading` value is not treated as readiness. The error label shows `appio: <error>` and no loading frame replaces an existing frame. A marker with `owner == "clock"` is retried exactly once by the next later `_tick_target()`; a repeated failure replaces its `since` marker and waits for the following tick. `show_window(window)` always clears any previous retry marker before starting the new explicit preparation. When an explicit marker is pending, only a subsequent `show_window(marker.window)` call is the named retry of that marker; a different-window `show_window(other)` is a new explicit request that supersedes and clears the old marker, but it does not retry the old scope. A marker with `owner == "explicit"` is never retried by `_tick_target()` or consumed by `apply_ready_frame()`. If the target becomes `None`, a clock-owned marker and error state are cleared and admission is restored; an explicit marker remains untouched and keeps admission closed. Invalid `Window` inputs retain the existing query/A22 validation.

Retry ownership and the visible state while waiting are exhaustive:

| Marker owner | Who may retry | Retry call | State while waiting |
| --- | --- | --- | --- |
| `clock` | The host timer | The next later `_tick_target()`; it retries the recorded/current target and replaces the marker on another failure | Keep the last displayed frame and Files quantities; show `appio: <error>`, keep Files admission closed, and do not claim readiness. With no displayed frame, keep the persistent tabs and the error/initial status surface. |
| `explicit` | The caller that requested `show_window()` | A subsequent `show_window(marker.window)` call is the named explicit retry; a different-window `show_window()` supersedes the old marker as a new request; `_tick_target()` and `apply_ready_frame()` cannot retry or clear it | Keep the last displayed frame and Files quantities, or the persistent tabs if no frame exists; show `appio: <error>`, keep Files admission closed, and never replace the frame with loading. |

`_apply_frame` installs `_displayed` before `_show_ready` because `_show_ready` calls `_sync_files`, which reads `_prepared`. This order is mandatory for initial and replacement frames, including an unchanged-window replacement.

### 4.3 Legal transitions

This table exhausts live display/preparation transitions; `(D, P)` means displayed and pending scope. A target change alone does not change either member.

| Before / event | Call and after-state | Visible/query effect |
| --- | --- | --- |
| Initial `(None, None)`, no boot/cursor | `_tick_target` → same, target None | Initial waiting text; Files fallback message; tabs present. |
| No displayed frame, target A | `_tick_target` → `(None, A)` | Prepare A once; initial loading inside Overview; refuse Files queries. |
| `(D, None)`, target D and router not retired | `_tick_target` → same | No preparation; flush deferred Files refresh only if router is ready. |
| `(D, None)`, target A differs | `_tick_target` → `(D, A)` | Prepare A once; preserve D and show updating. |
| `(D, None)`, preparation raises after the router enters loading | `_tick_target` → `(D, None)` with local `preparation_error` and a `PrepareRetry(owner="clock")` marker | Keep D and all Files quantities; show the error, keep admission closed, and let only the next later target tick retry once. |
| Any live state, explicit `show_window(E)` raises after the router enters loading | `show_window(E)` → `(D, None)` with a `PrepareRetry(owner="explicit", window=E)` marker | Keep D (or the persistent empty host), keep admission closed, and retry only when the explicit caller invokes `show_window(E)` again. |
| `(D, A)`, target changes to B without retirement | `_tick_target` → `(D, A)` | Record B as target, retain A; no second prepare. |
| `(D, A)`, router not ready | `apply_ready_frame` → same; returns False | No display assignment; table retained, controls disabled. |
| `(D, A)`, router ready for A | `apply_ready_frame` → `_apply_frame` → `(A, None)` | Exactly one frame application and at most one Files query. |
| `(A, None)`, target B still differs after applying A | next `_tick_target` → `(A, B)` | Prepare B, then display only after its readiness. |
| `(D, A)`, router retired | next `_tick_target` retires A then prepares the current target C → `(D, C)` | One replacement on the same tick, including C == A. |
| `(D, A)`, retirement first noticed by readiness poll | `apply_ready_frame` → `(D, None)`; returns False | No apply; next target tick owns replacement. Keep admission closed until that decision. |
| `(D, None)`, router retired | `_tick_target` derives invalidated then → `(D, target)` | Reprepare even if target equals D. |
| Any live state, explicit caller requests E | `show_window(E)` → `(D, E)` | Explicitly supersede any old preparation via prepare; clear any previous retry marker before preparing, and leave the clock target untouched. If the old marker is explicit, only `show_window(marker.window)` is its retry; another window is a new request that supersedes it. |
| Any live state, explicit retry marker is pending | `_tick_target` / `apply_ready_frame` → same | Leave the explicit marker untouched; do not retry, consume, or replace it from the clock path. |
| Any live state with no explicit retry marker, target becomes None | `_tick_target` → `(None, None)` | Waiting state; clear only a clock-owned retry marker, restore admission, and do not claim a stale scope as current. |
| Any live state, shutdown | `shutdown` → closed terminal state | Stop both timers, refuse all future apply/query/submission paths. |

The no-target reset and construction are the only assignments to `_displayed` outside `_apply_frame`. `show_window` is an intentional explicit-scope override, not the timer path; the timer never replaces a nonretired pending scope merely because the target advanced.

### 4.4 Illegal transitions and prohibited callers

| Illegal transition/action | Required prevention |
| --- | --- |
| Assign `_displayed` on prepare, submit, or a not-ready poll | Only `_apply_frame` assigns a non-None displayed scope. |
| Reprepare a nonretired pending scope from a tick | `_pending is None` guard in `_tick_target`. A clock retry marker is the only retry marker a tick may consume. |
| Let the clock retry an explicit preparation failure | `_tick_target` returns while `_prepare_retry.owner == "explicit"`; only the explicit caller's subsequent `show_window(marker.window)` clears that marker. |
| Apply a retired scope or apply the same preparation twice | Retirement check and clearing `_pending` before render. |
| Hide the last frame while replacing it | Status overlays change; data widgets retain their model. |
| Assign `_prepared` | Read-only property; callers use `_pending`/`_target` for requested scopes. |
| Call `_show_ready` from a test, harness, timer, or reply handler | Drive `show_window`/`apply_ready_frame`, or the timer pair. |
| Compute a new target from `apply_ready_frame` | Poll only the stored pending window. |
| Reconcile unchanged facts while `_pending is not None` | Evaluate the keep-pending rule before assigning `_facts`. |
| Query Files while scope admission is false | Every programmatic entry point checks admission before status/ranking/query work. |
| Deliver an old file result after readiness is restored | `_apply_frame` retires delivery generation before restoring readiness. |
| Apply a probe while busy/stale/closed | Acceptance predicate in section 4.6. |
| Clear the current probe slot when an older ticket arrives | Only the matching current ticket may release `_probe_inflight`. |
| Mutate widgets from a worker, or process Qt events in an explicit-scope driver | Queued bridges and event-free explicit loops. |

### 4.5 Refresh and fact-application execution order

`refresh()` on the Qt thread, once per timer tick:

1. Return if closed.
2. If a pump exists, call it inside `files_view.transient_scope()`. This covers `on_gap` → `record_transport_gap` inside the pump.
3. Call `_tick_target()`, then `apply_ready_frame()`, whether or not a probe is in flight. If neither applied a frame and the displayed scope is ready with no pending preparation, call `files_view.flush_refresh()`.
4. If not busy, call `_submit_probe()`; an occupied current probe slot skips only this submission.
5. Catch timer-path exceptions with the existing `appio: <error>` error surface; never terminate the timer callback.

The worker runs only `setup.refresh()` and creates the completion payload. It does not reconcile, sample, pump the live client, access Qt, or query live SQLite objects.

For an accepted fact payload, on the Qt thread:

1. Promote a currently verified, answering history connection over silent one-shot facts, preserving its `pending_activation`. The existing promotion is at `src/appio/ui/collector_window.py:619-627`; do not promote a retained unverified cursor.
2. Compute `keep_pending = (promoted_facts == self._facts and self._pending is not None)`. Do not require daemon verification, answering, or a loading-state string.
3. Inside `files_view.transient_scope()`, call `coordinator.reconcile(promoted_facts)` unless keep-pending, then call `coordinator.tick_local()`. A real sample may invalidate; detect that from router state, not an invented flag.
4. Assign `_facts`, increment `_facts_applied`, render the banner/settings switch, then run `_tick_target()` and `apply_ready_frame()`. Flush Files on the same ready/no-frame-applied condition as the timer.
5. If coordinator work raises, show the error, do not claim a new applied-facts count, and retain the prior banner/frame. Any resulting router retirement is repaired by the next target tick.

The live operational wrappers are exhaustive. `_pump()` -> `on_gap()` -> `record_transport_gap()` (`src/appio/ui/collector_window.py:1125-1128` -> `src/appio/history_router.py:431-447`), `reconcile()` on the `_apply_facts()` ownership path before its `_tick_target()`/`apply_ready_frame()` step (`src/appio/collector_owner.py:165-198`), `tick_local()` when it samples (`src/appio/collector_owner.py:201-210`), `_begin("enable")` -> `_quiesce_local()` -> `reconcile(QUIESCE_FACTS)` (`src/appio/ui/collector_window.py:801-822`), and `_after_disable()` -> `accept_export()` -> `add_source()` (`src/appio/ui/collector_window.py:935-947` -> `src/appio/collector_owner.py:212-252`) use the default `transient_scope()` exit rule: they retire on exit when the router reports `history.view_state.startswith("unavailable")`, but do not retire a healthy request when no invalidation occurred. `prepare()` from both `_tick_target()` and `show_window()` (`src/appio/history_router.py:742-769`) passes `retire_on_exit=True`, because `prepare()` itself retires file requests and leaves `view_state == "loading"`. The quiesce path is listed separately because it reaches `reconcile()` outside the normal fact-application flow. The `tick_local()` path is listed separately because its sample calls `HistoryRouter.invalidate()` even when reconciliation is skipped. `prepare()` is listed separately because it retires file futures after setting the new generation. Shutdown/close invalidation remains outside the table-preservation guarantee.

An ordinary timer tick therefore does not retire a healthy in-flight request: its pump scope uses the default flag, and with no invalidation the router is not in an unavailable state at exit. The apply-time `files_view.retire_deliveries()` in `_apply_frame()` remains unchanged and is the only retirement on a successful frame application.

No refresh tick calls blocking `subprocess`, socket connect/recv, or `time.sleep` on the Qt thread. Local sampling, the one-row boot lookup, prepare/readiness bookkeeping, and nonblocking client sends remain on that thread at the design's existing frequencies; do not claim these are offloaded.

### 4.6 Probe payload, slot ownership, and exact drop rules

`ProbePayload` is immutable. Exactly one of `facts` and `error` is present. Facts must be ServiceFacts; error text must be nonempty. Ticket must be a positive integer; generation must be nonnegative; booleans are rejected. Its `__post_init__(self) -> None` raises ValueError for malformed values. **specification decision:** store exception text, not exception objects or a live Future, in the queued payload.

`_submit_probe(replace_pending=False) -> bool`:

- Return False when closed or busy, or when a current slot exists and replacement was not requested. A completed Future awaiting Qt delivery still occupies its slot.
- For submission, increment `_probe_ticket`, capture that ticket and the current `_setup_generation`, submit `setup.refresh` to `ThreadPoolExecutor(max_workers=1)`, retain the new Future as `_probe_inflight`, and attach `_probe_completed(ticket, generation, future)`. Return True only after successful submission. Handle submission errors through `_show_error` and release the slot.
- `_probe_completed` reads the settled future, converts success/error into the payload, and emits `_probe_bridge.delivered`. Connect this signal using `Qt.ConnectionType.QueuedConnection`, even when an already-complete Future calls back on the GUI thread. It performs no widget mutation and no shell-state acceptance decision. Swallow only QObject-destruction emission errors after close.
- **specification decision resolving the setup-overlap schedule:** a helper-completion replacement may supersede the tracked older probe even while its worker is running. Allocate the newer ticket first. The "one queued successor" bound is a logical live-work bound, not a promise about the private executor queue: there is at most one running `setup.refresh` and at most one non-cancelled successor Future. `ThreadPoolExecutor.submit` appends a work item, and `Future.cancel()` does not remove that work item. `_probe_successor` records the one logical successor; replacing it first cancels/marks the old successor and clears that logical slot, then submits exactly one new successor. The cancelled work item may remain in the executor queue, but when dequeued it must not invoke `setup.refresh` or emit an accepted payload. Periodic ticks cannot submit while a running probe or logical successor exists. A running older probe is not forcibly terminated. Further setup stages therefore cancel/replace one successor instead of building unbounded live work. This is still one worker, with no concurrent `setup.refresh` calls.

`_apply_facts(payload)` uses exactly this acceptance predicate before promotion or ownership changes:

```python
if self._closed:
    return
if (
    payload.ticket != self._probe_ticket
    or payload.generation != self._setup_generation
    or self._busy
):
    self._dropped_facts += 1
    # Release only if this is the current ticket; never release a newer slot.
    return
```

Each stale/busy delivery increments `_dropped_facts` once, even if several conditions fail. Closed deliveries perform no state mutation, including no counter increment. A current accepted error releases its slot and calls `_show_error(f"appio: {payload.error}")`, but neither reconciles nor increments `_facts_applied`. A current accepted facts result releases its slot and follows section 4.5. An older-ticket result/error must not clear a newer `_probe_inflight`, overwrite facts/banner/error text, reacquire a writer, or render a frame.

`_start` increments `_setup_generation` immediately before marking busy and invoking each helper operation (`src/appio/ui/collector_window.py:828-844`). Every completion path increments it again before clearing busy: successful reply, nonzero helper exit, Future exception, and synchronous invocation failure (`src/appio/ui/collector_window.py:846-866`). The synchronous `setup.invoke` failure uses the same completion epilogue as `_on_reply`; it is not allowed to clear `_busy` first. `_on_reply` retains the existing helper-body/choice/export logic, then submits exactly one fresh probe with `replace_pending=True` when not closed and no chained helper has made it busy again. The synchronous-failure path follows the same fresh-probe policy after its error is shown. A chained `_start` owns its own start/completion generation edges. Setup body data never substitutes for unprivileged answering facts.

Legal probe/helper transitions: idle → submitted by `_submit_probe`; submitted → applied/error/dropped by matching `_apply_facts`; submitted → superseded by forced completion submission; not-busy → busy with generation increment by `_start`; busy → not-busy with generation increment by completion; any → closed by `shutdown`. No other call releases a newer slot or changes the accepted-facts counter.

### 4.7 Teardown

`shutdown()` is idempotent: set `_closed` first; stop both timers; close existing dialogs and retire Files deliveries; call executor `shutdown(wait=False, cancel_futures=True)`; retain existing coordinator/history close ordering. A bounded running probe may finish later, but its delivery applies nothing. Never block the Qt thread waiting for that probe. A test may join/release its controlled worker after the window closes. No system power, session, or display-manager operation is part of this feature or its verification.

## 5. Files admission, union choices, and delivery outcomes

### 5.1 State and classification helpers

Add `_transient: bool = False`, `_scope_ready: bool = True`, `_refresh_wanted: bool = False`, `_outstanding: bool = False`, `_updating_scope: Window | None = None`, and `_deferred_window: Window | None = None`. Keep the existing `_generation`, `_window`, `_app_id`, request key, last-result, and bounded request log (`src/appio/ui/files_view.py:324-349`). `_outstanding` remains true until delivery is handled or retired, even if its Future has already completed on a worker.

- `set_transient_scope(transient)` accepts only bool and assigns the idempotent classification switch. It does not issue a query, clear a table, or bump the generation.
- **specification decision:** `transient_scope(retire_on_exit=False)` sets `_transient` true on entry and, in its `finally`, restores the previous boolean. It calls `retire_deliveries()` on exit only when `retire_on_exit` is true or the injected history router reports `history.view_state.startswith("unavailable")` at that exit. The host passes `retire_on_exit=True` only around the `history.prepare()` call; `prepare()` itself retires file requests and leaves `view_state == "loading"`, so the explicit flag is required there. Pump, fact-application, quiesce, disable/export, and other ordinary invalidation scopes use the default flag and rely on the unavailable-state check. A normal timer tick with no invalidation therefore does not retire a healthy request or trigger a replacement query. Nested scopes restore the outer classification value; each exit applies only its own flag/state rule, and no outstanding-request counter or hold mechanism is introduced.
- `set_scope_ready(ready)` accepts only bool; changes admission and enables/disables selector, search, direction/refresh controls, and clickable sort header. It never automatically queries. False keeps the existing row model, scroll, last result, and footer quantities. True is not sufficient to publish an old delivery; generation retirement still governs.
- `set_updating_scope(window)` changes only the destination used by the footer; it never adopts `_window`. The dedicated QLabel `files_updating` is appended after the existing footer and reads `updating to <scope>` while a refresh is deferred or replacement is pending. It preserves all existing numeric/footer lines. Fall back to `_window`'s kind only if no explicit updating scope exists.
- `retire_deliveries()` increments `_generation` on every call. If `_outstanding`, set `_refresh_wanted = True`; then clear `_outstanding`. Keep the model, last_result, footer, sort, search, and subject. Closed views do not schedule a replacement.
- `refresh_wanted()` returns the deferred flag without consuming it.
- `flush_refresh()` returns without consuming the flag if closed, admission is false, no app/window exists, or `history.ready(_window)` is false. Otherwise, if wanted, issue at most one request and clear the wanted flag as that attempt begins. If no flag is set, it is a no-op. A failed admitted attempt becomes an explicit failure state, not a spin-loop retry.

### 5.2 Programmatic entry points and one query owner

All entry points check admission before reading source status, choices, or query results. Widget disabling alone is insufficient.

| Entry point | While `_scope_ready` is False | When ready |
| --- | --- | --- |
| `set_window(window)` | Record `_deferred_window`, set wanted, return without adopting, clearing, ranking, or querying. | On every applied preparation, refresh the choice union even when the Window equals `_window`. If different from `_window`, adopt it, load the union, and issue exactly one table query when an app/source exists; clear wanted because adoption satisfied it. If equal, retain the displayed table and do not issue a table query merely because choices were refreshed; a wanted refresh is owned by the later flush. |
| `set_app_id(app_id, request=True)` | Validate/store subject and synchronise selector, set wanted, issue no query. | On a changed ID, query once when `request=True`. `request=False` only synchronises and marks wanted for the frame owner's adoption/flush. Same ID is a no-op. |
| `set_sort(sort, direction=None)` | Validate/store sort/direction and update indicator, set wanted, issue no query. | Query once after storing a changed query key. Invalid sort/direction keeps the existing ValueError contract. |
| `request_refresh()` | Set wanted and return. | Validate source, record one exact request, set outstanding, and attach its generation-stamped delivery. No implicit second request. |
| Search/direction/header/refresh handlers | Disabled to user input; programmatic invocation still follows the entry-point guard. | Keep existing NFC/search and server-sort semantics; one request per action. |

**specification decision:** changing an app/query key while a request is outstanding retires that delivery before storing the new key, even while admission is closed. This prevents an old app's successful result publishing under the newly selected subject. Retirement preserves the old displayed table with its updating label until the admitted replacement settles.

**specification decision:** choice refresh is separate from table-scope adoption: `_load_choices()` runs once for every applied preparation, including a replacement whose `Window` value is unchanged. It may replace the selector union, but it does not clear the retained table or issue a row query solely because the union changed. A newly file-only application is appended after ranked choices and, when a valid current subject remains, becomes selectable without displacing that subject automatically; the normal first-choice fallback still applies when no subject exists. A newly ranked application enters the ranked order and may hide the Files selector when no file-only extras remain. If the current subject vanishes, choose the first remaining ranked choice, then the first remaining file-only choice, then `None`; the fallback causes at most one deferred query for the new subject, or clears the table and notifies the host when the union is empty. A non-current vanished choice only disappears. This same-Window refresh contract keeps union membership current without creating a second query on frame application.

A changed-window adoption discards a stale `_deferred_window` in favour of the host's canonical displayed scope. A delayed caller is not authorised to redirect the scope after the host applied a different frame. During adoption, load choices and synchronise the subject with query signals blocked; do not call the querying version of `set_app_id` from choice-population signals.

An applied frame issues at most one query:

1. `_apply_frame` retires prior deliveries, then opens admission.
2. `_show_ready` renders the ranking/Overview and synchronises subject with `request=False`.
3. `_sync_files` calls `set_window(displayed)`. A changed scope queries once and consumes wanted.
4. `flush_refresh` is a no-op after that adoption, or issues the one request needed for an unchanged scope.

A user tab switch performs none of these steps. When no frame applied but a deferred same-scope update becomes admissible again, the next ready timer/fact path flushes once.

### 5.3 Union selector population

`_load_choices` replaces `_load_ranking`; no standalone ranking-empty fallback remains. It is called once per applied preparation, including a same-Window replacement; the selector refresh is not gated by Window equality.

1. Read `history.ranking(_window, "total_bytes")`, preserving its order; collect ranked IDs.
2. If both structural `history.local` and `history.registry` exist, read `local.file_applications(_window)` once, map every key through `registry.id_for(key)`, remove IDs already ranked or previously added, and append all remaining choices sorted by `(name, app_key.encode("utf-8"))`. The tie-break is a **specification decision**.
3. Labels remain `f"{name}  {format_bytes(total_bytes)}"`; no FileRow basename becomes an application identity.
4. Set `app_selector.setVisible(len(choices) > len(ranked))`. A mixed A-ranked/B-file-only scope shows both choices. A fully ranked file population hides the selector.
5. Retain the valid existing subject; otherwise choose first ranked, then first file-only, then None. Emit `subject_changed(app_id)` only for an actual subject change, including clearing to None, after population, without triggering a duplicate query. `_on_app_changed` validates a positive choice, emits that signal, and requests exactly once; its host listener does not request again.
6. If both populations are empty, retire any outstanding old-subject delivery, clear `_app_id` and the selector selection with signals blocked, use the existing no-applications footer, and clear the table. Notify the host once via rule 5 if this removed its subject. A source-status failure precedes this empty-success claim. A genuine ranking/local-choice failure uses normal failure classification; do not silently hide file-only choices.

### 5.4 Delivery decision table

First apply the existing closed/generation guard. For a current delivery or synchronous request failure, the transient predicate is exactly:

```python
transient_failure = self._transient or not self._scope_ready
```

`_handle_failure` is the single classification boundary used by `_on_delivered`, `_settle`, and invalid-result refusal in `_publish`. Neither exception type nor the literal message `File history generation retired.` is needed to classify a failure.

| Delivery/outcome | Model and footer effect | Request effect |
| --- | --- | --- |
| View closed | Ignore; no clear, publish, label change, or Qt mutation. | No replacement scheduled. |
| Generation differs | Ignore, including successful/error payloads and delivery after readiness restored. | Do not clear a newer outstanding slot or consume wanted. |
| Current success with valid served interval and no unavailable reason | Publish exact rows/result/footer; a valid empty result may replace prior rows with measured empty state. | Mark the matching request settled; clear its updating label unless a newer deferred update still exists. |
| Current failed future with `_transient` True | Preserve rows, last_result, and every existing footer quantity; show updating destination. | Set wanted; mark this request settled. |
| Current failed future while scope admission False | Same preservation, even when a worker-queued failure arrives after the transient context returned. | Same wanted flag; no immediate query. |
| Current failure when neither transient condition holds | Existing `_set_unavailable(reason, message)`: clear model/last_result; unavailable footer/state. | No automatic retry. |
| Current invalid FileViewResult (unavailable inner reason or missing served interval) | Route through the same transient predicate; preserve when transient, otherwise clear as terminal unavailable. | Defer only in transient state. |
| Synchronous request/status/choice error | Same classification as a failed delivery. | No retry loop; one deferred flag only when transient. |
| Scope adoption while an existing table is displayed | Preserve the last table/footer with updating until a current valid result or terminal failure settles. | Exactly one adopted-scope request. |
| Union is genuinely empty with an available source | Clear model, use no-applications state. | No file request without a subject. |

A valid old-scope success received while pending may update only that still-displayed scope and matching subject/query key; it cannot adopt the pending scope or remove the updating destination. Changed keys have already retired the delivery. A generation-dropped failure is not reclassified and never clears anything.

### 5.5 Required race schedules

- Synchronous retirement: the live invalidating call settles the file Future on the calling Qt thread inside the default `transient_scope`; because the router is `unavailable` at exit, the scope retires the delivery generation, keeps the table, and sets wanted.
- Worker wins cancellation: worker settles between the router generation increment and cancellation; queue its delivery until after context exit but **before the next `_tick_target()` closes admission**. At that instant `_transient` is false and `_scope_ready` is still true. The invalidating scope exits with `history.view_state.startswith("unavailable")`, so its conditional exit retirement already makes the delivery generation stale; the queued delivery is dropped without clearing the table or footer and does not rely on an unconditional retirement rule.
- Late after readiness: queue the same failure, apply the replacement frame first, retire delivery generation before restoring readiness, issue exactly one replacement request, then deliver the old failure; generation guard discards it.

Prepare scopes are the separate case: they pass `retire_on_exit=True` because `history.prepare()` has already retired requests and leaves the router in `loading`, not `unavailable`.

Do not replace these three schedules with one broad try/except, suppress only a reason string, add a permanent hold, or postpone queries indefinitely.

## 6. Chart transformation, painting, and geometry

### 6.1 Inputs and source runs

All setters validate contracts and copy their input to tuples. Reject wrong point types, nonincreasing starts, overlapping intervals, and invalid byte/peak values. Zero is measured data. `set_task_points(())` and `set_device_points(())` are available empty series; they clear prior reasons and polygons. No call consults a database, clock, wall-time converter, device resolver, or Files result.

For a point, `start = bucket_start_mono_ns`, `end = start + bucket_secs * 10**9`. A task run starts at the first point, after nonadjacency, or immediately after a point whose `spans_suspend` is true. The suspended point itself remains in the preceding run; do not remove its measured bytes or peaks. A device run starts only at the first point or nonadjacency. It never inherits task suspension flags.

Coverage counts are annotations only. A populated bucket with a recorded gap count remains populated; do not invent a gap location or use evenly spaced gap markers. Lifetime chronology follows section 2.2.

### 6.2 Columns and lossless downsampling

**specification decision:** use the plot's integer logical-pixel width `W = max(0, floor(right - left))` as the column budget. At W == 0 or with a zero-height plotting area, expose no columns/segments until resized; retain input data. These collapsed-widget rules are specification decisions; positive-area geometry obeys all conservation and run-count requirements below. Exclude caption/gutter/margins from this width.

1. With at most W points, one ChartColumn represents each point. `starts_run` reflects the raw run boundary; byte fields remain exact.
2. With more than W points, assign each point once to a time-pixel bin by its start:
   `bin = min(W - 1, ((start - first_start) * W) // (last_end - first_start))`.
3. Emit one ChartColumn per nonempty bin in bin order. Its envelope is minimum start through maximum end of its contributors; bytes are their sums; each peak is maximum of defined contributor peaks, or None if all contributor peaks are None. `starts_run` is True if any contributor begins a source run, including a boundary inside the bin.
4. Empty bins are not measured-zero buckets. Do not emit synthetic byte-bearing columns for them. Output has at most W columns; each raw byte contributes exactly once to the summed column byte fields.
5. **Critical:** preserve the raw run membership and per-run time intervals alongside column aggregation. One column can contain contributors from both sides of a hole; its start/end envelope must never be painted as a continuous filled rectangle or one polygon across that hole. `starts_run` alone is not enough geometry information.

The auxiliary source/run membership is private cached chart state, not extra ChartColumn fields. Work is O(number of source points + W) per data/size change; do not do a source-point scan in every paint event.

### 6.3 Polygons, scales, and hole exclusion

**specification decision:** for each column compute its average read/write rate using the column byte sums divided by the sum of its contributors' observed durations, not elapsed time across missing buckets. Use exact integer/rational arithmetic until mapping to finite pixel coordinates. These are downsampled lane amplitudes, not additional observations or a replacement for the exact KPI/peak totals.

Map `first_start..last_end` linearly to `plot_rect.left..right`. Let `centre = (top + bottom) / 2`, `H = (bottom - top) / 2`. Independently normalise read heights against the largest nonnegative read column rate, and write heights against the largest write column rate. When a side's maximum is zero, its heights are zero; never divide by zero.

Construct one step-fill polygon per source run and side that has positive amplitude. Partition the raw time intervals by their column membership and use that column's height only inside the member run's observed support. Close each polygon to the centre line at the run's own endpoints. A column shared by two runs contributes separate clipped pieces to those runs; never join the pieces. Multiple source runs may occupy subpixel spans in one column; retain them rather than deleting a break to satisfy the column budget.

For exact testable exclusion, **specification decision:** inset each run's outer X endpoints by `epsilon = min(0.25, run_pixel_span / 4)` logical pixels before polygon closure. This makes every polygon belonging to a run left of a hole strictly left of mapped hole-start, and every right-run polygon strictly right of mapped hole-end. An isolated suspended last point does not add an invented time-width hole; it only prevents a connecting polygon to the next point.

Tests may assert these invariants using `segments()` and `plot_rect()`:

- All coordinates are finite; each polygon is closed, has nondecreasing forward support along X before its baseline closure, and stays inside the plotting rectangle.
- Read vertices satisfy `top <= y <= centre`; write vertices satisfy `centre <= y <= bottom`. A nonzero maximum on each side reaches its own available half-height, subject only to the pen's half-pixel inset.
- Contiguous nonzero samples yield exactly one polygon per nonzero side. Run indices follow raw temporal run order; an all-zero side may omit its degenerate polygon.
- For a missing interval `[hole_start, hole_end)`, each segment's **entire** X support is strictly left of mapped hole-start or strictly right of mapped hole-end. Inspect min/max X for each polygon, not merely whether any vertex happens to fall inside the hole. No edge may bridge the interval.
- Task suspension creates a run boundary after that point. Device geometry for the same adjacent buckets remains one run unless its own buckets are missing.
- A bin spanning a break has `starts_run=True`, summed bytes and maximum peaks, while `segments()` still has separate run support. `len(columns()) <= W` does not bound the number of run fragments.
- Unavailable state: `columns() == ()`, `segments() == ()`, `is_available() is False`, and the exact reason is observable and painted. Available empty state: empty columns/segments, availability True, no reason, and no zero line.

`paintEvent` draws these cached polygons with QPainter/QPainterPath and the theme palette. No background animation, external charting package, screenshots masquerading as charts, or per-tick widget recreation.

### 6.4 Donut, shares, gutters, and labels

- Donut fractions are `read/(read+write)` and `write/(read+write)` from task bytes in a successful DiskShare, even if device attribution is unavailable. The share-query-error case is different: both composition widgets show that error, as design section 6 requires.
- For measured `(0, 0)`, `shares() == (0.0, 0.0)` and no sectors are drawn; show `no task I/O in this window`. This is measured emptiness, not unavailable. Keep exact byte labels; percentage labels have one decimal place and `%`.
- ShareBars normalise each device total against the largest device total, not the task total and not summed devices. Empty input returns no fractions/shapes. All-zero rows have 0.0 widths. No NaN/infinite widths. Negative row totals are rejected.
- Single-boot device labels are raw `major:minor`. **specification decision:** for lifetime share rows spanning several boots, append ` [<boot_id>]` to each token to keep same-token devices distinct; do not collapse them. Their labels still claim no device name or mount.
- TOTAL caption: `TOTAL · read above, write below`. Device captions are raw tokens. Gutters use `r <formatted read> · w <formatted write>`, using exact task/device totals, not summed retained samples.
- Keep every share row in the share panel; the three-lane cap applies only to the time-lane stack. Beneath the stack show `and <n> more` only when n > 0.
- Unavailable device attribution (a non-available share device state or undefined share device total) suppresses device bars and lanes but preserves the task donut and task lane when their own inputs remain valid. An undefined residual alone suppresses reconciliation; it does not invalidate independently defined device totals.

## 7. Exact visible strings and ownership

Values in angle brackets below are interpolations, not literal placeholders. Error text is nonempty `str(error)` or its class name if empty. Boolean suspend text uses Python `True`/`False`. Scope text for loading/updating is the exact `Window.kind` token; detailed served/requested Files scope wording remains unchanged.

| Owner | Exact string or template | Condition |
| --- | --- | --- |
| `formatting.py` | `unavailable` | Sole standalone unavailable value spelling. Existing constant: `src/appio/ui/formatting.py:19`. |
| `formatting.py` | `Launch window disabled: device and file activity are not stored per launch.` | Existing A22 launch reason, retained verbatim (`:12-14`). |
| `formatting.py` | `Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.` | Existing A22 overlong-range reason, retained verbatim (`:15-18`). |
| `formatting.py` | `attributed exceeds task accounting by <bytes>`; `+<bytes>`; `0 B`; `unavailable` | Existing negative, positive, measured-zero, undefined residual text (`:61-75`). |
| `render_model.py` | `<name>  <formatted bytes>` | Ranking/choice label, two spaces. |
| `render_model.py` | `no applications observed` | Empty successful ranking. |
| `render_model.py` | `not in the process ranking` | File-only subject. |
| `render_model.py` | `series unavailable`; `task/device split unavailable`; `lifetime chronology unavailable` | Default missing-input/unsupported chronology reasons. |
| `render_model.py` | `coverage: <complete or incomplete>; device attribution: <device_state>; tier: <tier>; gaps: <n>; suspends: <True or False>` | Successful coverage summary; append `; reason: <reason>` only when supplied by that coverage. |
| `render_model.py` | `coverage: unavailable` | No successful coverage source. |
| `render_model.py` | `window: <kind>; tier: <tier>` | Normal detail line; append independent errors as `; series: <reason>` / `; share: <reason>`. |
| `render_model.py` | `Residual: <format_residual result> - difference between accounting points; <device bytes> attributed + (<signed residual bytes>) residual = <task bytes> total` | Reconciliation with available attribution and defined residual. `signed residual bytes` uses `format_bytes` and preserves a minus sign. |
| `main_window.py` | `window: <kind>; no applications observed` | Empty successful active-window detail; preserve the exact requested kind. |
| `main_window.py` | `Residual: unavailable - difference between accounting points` | Empty/ranking-unavailable residual strip. |
| `main_window.py` | `device attribution unavailable: <reason>` | Device attribution unavailable; reason is coverage unavailable reason if present, otherwise device_state. DEVICE value is separately `unavailable`. |
| `main_window.py` | `Overview`; `Files`; `TOTAL`; `READ`; `WRITE`; `PEAK`; `DEVICE` | Tab and KPI captions in fixed order. |
| `charts.py` | `TOTAL · read above, write below`; `r <bytes> · w <bytes>` | Lane title and gutters. |
| `charts.py` | `unavailable: <reason>` | Unavailable chart's actual visible `chart_message` label. |
| `charts.py` | `no samples in this window` | Empty successful task/device series. No zero line. |
| `charts.py` | `no task I/O in this window` | Successful measured zero/zero donut. |
| `charts.py` | `READ vs WRITE`; `<bytes> read`; `<bytes> write`; `<read percent>% / <write percent>%` | Donut caption and byte/percentage labels. |
| `charts.py` | `ATTRIBUTED SHARE BY DISK`; `Σ disks <device bytes> + residual (<signed residual bytes>) = <task bytes> total` | Share panel caption and available reconciliation annotation. |
| `main_window.py` | `and <n> more` | Hidden active device-lane count. |
| `collector_window.py` | `waiting for the first sample` | No target/sample, including Files page fallback. Existing text at `src/appio/ui/collector_window.py:485,573,713`. |
| `collector_window.py` | `loading history (<view_state>)` | Before the first applied frame only. Existing template at `:584,725`; never replace an existing frame with it. |
| `collector_window.py` | `updating (<view_state>)` | Replacement pending with an existing Overview frame. |
| `files_view.py` | `updating to <scope>` | Deferred/pending Files update in `files_updating`, without overwriting existing footer quantities. |
| `files_view.py` | `loading`; `loading file history` | Initial admitted file request footer/state; existing at `src/appio/ui/files_view.py:150,831`. For a replacement with an existing result, retain prior quantities and show updating instead. |
| `files_view.py` | `the file source is unavailable` | Default source diagnostic (`:479,513,575`). |
| `files_view.py` | `<reason>: <message>` or `<message>` when reason absent | `unavailable_text` (`:478-482`). |
| `files_view.py` | `file history unavailable: <unavailable_text>` | Terminal state label (`:827`). |
| `files_view.py` | `file history is not available for this window` | Unavailable/missing-served-scope result refusal (`:720,729`). |
| `files_view.py` | `coverage: not applicable; no applications observed in this window` | Available Files source, empty union (`:805-813`). |
| `collector_window.py` | `file activity is unavailable` | Existing FILES_UNAVAILABLE prefix (`src/appio/ui/collector_window.py:107`). |
| `collector_window.py` | `file activity is unavailable: <reason>: <diagnostic>` | Degraded source with reason; omit `<reason>: ` when absent. Diagnostic defaults to `the file source is unavailable`; keep producer diagnostic verbatim (`:578-583`). |
| `collector_window.py` | `file activity is unavailable: <files_view.unavailable_text()>` | Terminal Files unavailable state (`:585-586`). |
| `collector_window.py` | `appio: <error>` | Probe/timer/ownership exception error surface (`:646,825,839`). Last valid banner/frame stay on probe failure. |
| `collector_window.py` | `off`; `on, answering`; `enabled, not answering`; `on, needs activation`; `unavailable` | Existing banner-state vocabulary (`:96-100`). |
| `collector_window.py` | `Open settings` | Banner and Files deep-link buttons (`:463,501`). |

Unchanged Files coverage/refusal text remains exact: `_scope_bounds`/`_scope_text` (`src/appio/ui/files_view.py:61-85`), table/header captions (`:100-137`), full FOOTER_NOTE (`:138-146`), retained/no-match/empty/incomplete row-state templates (`:742-760`), and observed/omission/overflow/skipped/weak/hook lines (`:761-803`). These are the source-defined templates for this feature, not text to abbreviate during tab embedding. All eight quantity footer labels read exactly `unavailable` on terminal failure (`:815-822`); transient failures do not replace them. Preserve `served interval`, requested-window disclosure, and the difference between counted omissions and uncounted overflow.

The new reason/default/caption/updating templates and placement above are **specification decisions** where the design supplied only the semantic message. Existing consent, helper failure, unsupported-host and missing-helper strings stay byte-for-byte as implemented at `src/appio/ui/collector_window.py:273-315,882-901,939-946`; this change adds no new privilege prompt or command.

## 8. Error behaviour and reproducing fixtures

This table covers every row of approved design section 6. Fixture names here are normative test helpers to implement in the indicated test module, not claims that the helpers already exist.

| Condition | Required outcome | Reproducing fixture and owning test |
| --- | --- | --- |
| `ranking()` raises WindowUnavailableError | List reason; five unavailable KPIs; lanes/donut/share bars `unavailable: <reason>`. No false empty success. | `tests/test_ui.py`: extend `FakeQueryService` with a configurable ranking exception; `test_ranking_error_marks_all_overview_dependencies_unavailable`. |
| `disk_share()` raises | TOTAL/READ/WRITE/DEVICE unavailable, known ranking PEAK may survive; donut/share panel show reason; independently successful task lane draws. | Same fake's share exception; `test_share_error_keeps_task_lane_and_marks_dependent_values_unavailable`. |
| `series()` raises or is empty | Raised reason marks lanes unavailable; empty success says `no samples in this window`; no zero line. Valid independent KPIs remain valid. | Fake series exception and `Series(..., points=(), ...)`; `test_series_failure_and_empty_series_are_distinct`. |
| Device attribution unavailable | Task donut and task lane survive; DEVICE/residual unavailable; no device bars/lanes; strip gives device reason. | DiskShare with `device_state="incomplete"`, device totals/residual None; `test_device_unavailable_keeps_task_composition`. |
| Recorded gaps | Coverage gives counts/suspend/completeness; geometry breaks only at missing buckets or task suspension. | `chart_points` with both contiguous counted-gap and actual-hole variants; `test_coverage_counts_do_not_invent_chart_breaks`. |
| Empty ranking | No-subject state says `no applications observed`; KPIs unavailable; no charts. File-only choices remain reachable separately. | Empty fake ranking and empty file choices; `test_empty_ranking_has_no_shapes_or_zero_kpis`. |
| File-only subject | One subject, cleared highlight, preserved scroll, KPIs unavailable with `not in the process ranking`; Files queries B. | `mixed_history` with ranked A and file-only B; `test_file_only_selection_clears_highlight_preserves_scroll_and_explains_overview`. |
| Pending replacement | Retain last frame; Overview updating text; Files table/footer retained, controls disabled, queries refused. | `ControlledHistory` exposing pending and ready events; `test_replacement_keeps_frame_and_eventually_displays_new_scope`. |
| Retired preparation | Detect unavailable router state, prepare current target exactly once on next tick, and eventually display it. | ControlledHistory plus real-router invalidation variants; `test_retired_pending_preparation_is_replaced_once`. |
| Preparation raises after `view_state` becomes loading | Enter local `preparation_error`; keep the previous displayed frame (or the empty persistent host), leave `_pending` empty, keep Files admission closed, and record a tagged marker. A clock-owned marker retries once on the next later target tick; an explicit-owned marker is not retried by the clock and waits for the explicit caller's subsequent `show_window(marker.window)` call without claiming readiness. | Controlled failure after `src/appio/history_router.py:763`, `:766`, or `:769`; `test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick`. |
| Files source unavailable | Exact existing page message/diagnostic and settings link, no fabricated zero table or query against a refused source. | Existing fake source-status seam extended as necessary; `test_files_source_failure_preserves_exact_message_and_link`. Existing seam is checked at `tests/p3/test_collector_window.py:122-135,521-565`. |
| Probe raises | Show error; retain last accepted facts/banner/frame; release current slot; next eligible tick may submit again. | `ControlledProbe` exception Future; `test_probe_error_keeps_last_banner_and_frame`. |
| Close during probe | Stop both timers; shut executor without Qt-thread wait; release worker in test; ignore late success/error with no exception. | ControlledProbe Events and closed window; `test_close_during_probe_drops_all_late_effects`. |

A genuine terminal Files failure clears the table as today; retirement classified as transient does not. Do not globally swallow failures or display zero in place of unavailable input.

## 9. Test inventory and implementation gates

These are required implementation tests, not test results. Do not run the suite as part of specification drafting. All widget tests run offscreen using the project's existing pytest-qt/PyQt6 setup. **specification decision:** use controlled Events/Futures and Qt event pumping instead of arbitrary sleeps; a worker may wait 300 ms in the one slow-probe test, but no Qt-thread sleep is permitted.

### 9.1 Shared fixture contracts

- Extend `tests/test_ui.py::FakeQueryService` (existing at `:158-174`) with `series` results/calls and independent ranking/share/series failure injection. It remains a structural query-contract fake, with no SQLite/daemon import.
- `ControlledHistory` in `tests/p3/test_collector_window.py` exposes prepare calls, exact ready scope, view_state, configurable retirement, file request Futures, and a ready transition independent of probe completion. Extend the existing FakeHistory where appropriate (`tests/p3/test_collector_window.py:85-145`).
- `ControlledProbe` uses Events to hold/release a worker, records worker thread IDs, submissions, the running probe and logical queued successor, and returns exact ServiceFacts or an exception. It must expose cancelled queued work separately so tests can prove that an executor queue entry was cancelled even though `ThreadPoolExecutor` did not remove it. Do not mutate widgets from it.
- `settle_probe(qtbot: QtBot, window: CollectorWindow, *, after: int) -> None` waits with `qtbot.waitUntil` for `window.facts_applied > after`; use a separate dropped/error observation when no facts are expected. This helper processes Qt events and never sleeps in the GUI thread.
- `mixed_history` exposes ranked A plus local file-only B and a registry mapping B's key to a stable ID.
- `chart_points` makes at least four nonzero task buckets, optional hole/suspend, defined peaks mixed with None, and matching device points without suspend fields.

### 9.2 Pure model tests — `tests/test_ui.py` (design 7.1)

| Exact test name | Required observation |
| --- | --- |
| `test_build_ranking_orders_by_total_then_key_and_normalises_share` | Sorted IDs/keys, labels, `[1.0, subordinate/max, 0.0]`, and all-zero denominator without NaN. |
| `test_build_ranking_distinguishes_empty_and_unavailable` | Empty success text versus unavailable reason with no rows; rejects conflicting state. |
| `test_build_overview_has_five_ordered_kpis_and_none_peak_is_unavailable` | Exact caption order and formatter outputs; None peak differs from measured zero. |
| `test_build_overview_limits_device_lanes_and_counts_more` | Four active raw devices yield top three ordered by attributable bytes and more_devices == 1. |
| `test_build_overview_reconciles_only_available_device_attribution` | Positive/negative/zero signed residual text; unavailable device total yields DEVICE unavailable and reconciliation None. |
| `test_render_dataclasses_reject_invalid_values_and_availability_conflicts` | All seven dataclasses' invalid types, booleans-as-counts, duplicate identities, NaN, wrong KPI order, invalid endpoints, and open polygon rejection. |
| `test_lifetime_series_does_not_join_unrelated_boot_clocks` | Valid lifetime totals and boot-qualified shares survive while chronology is explicitly unavailable. |

### 9.3 Chart tests — `tests/test_charts.py` (design 7.2)

| Exact test name | Required observation |
| --- | --- |
| `test_contiguous_points_produce_one_run_per_nonzero_side` | One read and one write run, closed polygons, correct half-plane/scales. |
| `test_one_bucket_hole_splits_runs_without_points_or_edges_in_hole` | Two runs per side and complete X-support exclusion using min/max, not just vertex inspection. |
| `test_task_suspend_splits_after_point_but_device_does_not_inherit_it` | Task run boundary after marked point; byte retention; matching contiguous device series stays one run. |
| `test_set_unavailable_clears_shapes_and_exposes_exact_reason` | Empty columns/segments, false availability, exact reason and visible chart_message text. |
| `test_downsampling_preserves_bytes_peak_maxima_and_run_starts` | More than 2W points yield at most W columns; summed byte fields equal raw sums; peaks are maxima with all-None preserved; internal break column starts_run True. |
| `test_downsampling_across_gap_never_spans_its_horizontal_interval` | Force contributors from both sides of hole into one bin; assert each run's entire X support lies strictly on one side, independently for read and write; columns still conserve bytes/peaks. |
| `test_donut_shares_and_device_bar_fractions_use_their_defined_denominators` | Donut divides by task read+write; share bars divide by largest disk; zero and unavailable conventions differ. |
| `test_each_chart_grabs_nonempty_pixmap_and_renders_reason_text` | Grab SeriesChart, DualSeriesChart, DeviceLane, DonutChart, ShareBars; pixmaps non-null/nonempty; actual unavailable QLabel text visible. |
| `test_chart_resize_rebuilds_observations_and_keeps_geometry_in_plot` | Columns obey changed W, geometry finite/in bounds, no cached obsolete hole coordinates. |
| `test_empty_available_series_draws_no_zero_line` | Availability true with no shapes and exact no-samples message. |
| `test_coverage_counts_do_not_invent_chart_breaks` | Contiguous points with gap/suspend counts only remain contiguous; actual task suspend field and missing buckets alone split. |

### 9.4 Window tests — `tests/test_ui.py` (design 7.3)

| Exact test name | Required observation |
| --- | --- |
| `test_identical_refresh_preserves_item_identity_and_current_row` | Same QListWidgetItem objects, no reset/clear, no selection signal or selection write. |
| `test_changed_shares_with_identical_labels_repaint_without_rebuild` | Largest bytes 1500 → 1530 leaves rounded labels identical; subordinate shares change and row repaint rectangles are requested; items stay identical. Leader/zero shares remain 1.0/0.0. |
| `test_changed_order_restores_subject_and_scroll` | Reordered IDs retain selected app ID and scrollbar value when range permits; no feedback query. |
| `test_detail_tabs_are_exactly_overview_then_files` | One detail QTabWidget with those two captions and one FilesView descendant, including empty construction; no query or scope-handler failure before the first supplied window. |
| `test_two_tab_switches_preserve_subject_without_scope_or_query_calls` | Subject/highlight stable; spy set_window, prepare, request_file_view and collector/helper calls unchanged across switches. |
| `test_files_selector_visibility_depends_on_union_extras` | Hidden with fully ranked file population, shown for ranked A plus file-only B. |
| `test_file_only_selection_clears_highlight_preserves_scroll_and_explains_overview` | B becomes sole subject, no selected ranking row/current index, scroll stable, five unavailable KPIs, correct reason, no time shapes. |
| `test_unavailable_series_is_not_rendered_as_zero` | Exact lane reason, no shapes; None/unavailable KPI fields contain no fabricated 0. Independently measured KPIs remain valid. |
| `test_ranking_error_marks_all_overview_dependencies_unavailable` | Error-state behaviour in section 8. |
| `test_share_error_keeps_task_lane_and_marks_dependent_values_unavailable` | Error-state behaviour in section 8, including donut failure rather than fallback. |
| `test_series_failure_and_empty_series_are_distinct` | Error reason versus empty available caption and preserved independent KPIs. |
| `test_device_unavailable_keeps_task_composition` | Task donut survives while DEVICE/residual/device graphics unavailable. |
| `test_empty_ranking_has_no_shapes_or_zero_kpis` | Explicit empty success, exact active-window kind in detail, no fabricated measurements; when both populations empty, prior shared/Files subjects and highlight clear and a late old-subject result cannot publish. |

### 9.5 Shell tests — `tests/p3/test_collector_window.py` (design 7.4)

| Exact test name | Required observation |
| --- | --- |
| `test_slow_probe_does_not_block_pump_or_pending_frame_application` | Hold worker for at least 300 ms; timed refresh returns under 100 ms in the controlled fixture, only one periodic submission exists, pump count advances, and make history ready/apply while worker remains held. |
| `test_replacement_keeps_frame_and_eventually_displays_new_scope` | Advance A → B; old frame/widgets/tabs remain, no loading-history replacement after first frame, final displayed B and prepared alias B. |
| `test_applied_scope_is_not_prepared_again_without_change_or_retirement` | Multiple ticks on unchanged target preserve prepare count; target change and actual retirement each permit one new preparation. |
| `test_unchanged_facts_skip_reconcile_while_preparation_is_pending` | Equal accepted facts with pending read do not reconcile, including local/unverified facts; sampling policy still follows section 4.5. |
| `test_retired_pending_preparation_is_replaced_once` | Parameterise reconcile, sampling tick_local, add_source, transport-gap invalidation; prepare A, retire it, next tick one replacement for current A, eventually display A. |
| `test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick` | Exercise a clock-owned failure at each of the three existing post-loading failure points (`src/appio/history_router.py:763`, `:766`, `:769`); assert `preparation_error`, `_pending is None`, the previous frame and Files quantities are unchanged, admission is false, no ready frame is synthesized, and the next later target tick retries and can apply after the failure is released. Exercise the same failure through `show_window(E)` and assert the marker owner is `explicit`, clock ticks and `apply_ready_frame()` neither retry nor consume it, and only a subsequent `show_window(E)` retries it. |
| `test_probe_spanning_setup_is_dropped_and_completion_probe_is_applied` | Hold OFF probe across enable completion, submit fresh probe, release old; old increments dropped once and never changes ownership/facts; fresh facts_applied increments. |
| `test_off_probe_during_enable_never_reacquires_local_writer` | Deliver old OFF while busy; no lease/local-factory reacquisition. |
| `test_pump_transport_gap_preserves_files_through_replacement` | Real pump/on_gap/record_transport_gap synchronous retirement leaves model/footer quantities and updating destination intact. |
| `test_disable_export_import_preserves_files_through_add_source` | accept_export/add_source retirement wrapped in transient context; same preservation and one later replacement query. |
| `test_worker_wins_cancellation_delivery_is_transient_while_pending` | Worker settles between generation increment and cancel; deliver after transient context exits, before readiness; no clear and wanted flag true. |
| `test_transient_scope_exit_retires_worker_delivery_before_next_target_tick` | Keep the SR-1 parameterized invalidating schedules across pump/on-gap/transport-gap, fact-path `reconcile`, sampled `tick_local`, enable `_quiesce_local`, disable `accept_export`/`add_source`, and `prepare`: settle between the router generation bump and cancel, queue delivery, exit the scope, and deliver it while `_scope_ready` is still true but before the next `_tick_target()`; the default invalidating scopes retire because `view_state` is unavailable, while the prepare scope passes `retire_on_exit=True`, so the table/footer quantities stay unchanged, `_refresh_wanted` is true, and the subsequent tick owns exactly one replacement query. Also run an ordinary tick scope with no invalidation and assert that `retire_deliveries` is not called and the request count does not change: a healthy in-flight request is neither retired nor replaced. |
| `test_late_retired_file_delivery_after_ready_is_dropped_and_requeried_once` | Queue failure, apply unchanged-scope frame, assert retire-before-ready ordering and exactly one replacement query, then deliver old failure and prove table unchanged. |
| `test_pending_scope_refuses_all_file_entry_points_then_flushes_once` | Parameterise set_app_id, set_sort, set_window, request_refresh, and ranking selection; no query/status/choice work while blocked, controls disabled, one admitted flush with latest key. |
| `test_old_probe_ticket_cannot_overwrite_or_release_replacement_probe` | Deliver superseded probe after newest ticket exists, including after new facts applied; old cannot overwrite facts/error/banner or clear newer slot; dropped increases. |
| `test_probe_payload_rejects_malformed_combinations_and_counters` | `ProbePayload` rejects both/neither facts and error, non-`ServiceFacts` facts, non-string or empty/whitespace error, boolean/zero/negative/non-integer ticket, and boolean/negative/non-integer generation; valid facts and valid nonempty error payloads remain constructible. |
| `test_repeated_setup_stages_replace_one_queued_successor_while_probe_runs` | Hold an older probe running, complete two setup stages, and verify the first queued successor is cancelled/replaced by exactly one newest logical successor; no concurrent helper call occurs, cancelled executor work never invokes `setup.refresh`, and only the newest ticket can apply after the older worker releases. |
| `test_helper_completion_categories_advance_generation_before_busy_clears_and_reprobe` | Parameterise successful reply, nonzero helper exit, Future exception, and synchronous `setup.invoke` failure; observe each completion increments `_setup_generation` before `_busy` becomes false, then follows the same one-fresh-probe policy with a new ticket/generation and no stale accepted facts. |
| `test_mixed_ranking_and_file_only_union_queries_file_only_subject` | Ranked A and file-only B both listed; B ID from registry; query B once and Overview/highlight follow. |
| `test_same_window_frame_refreshes_union_for_file_only_ranked_and_vanished_apps` | Apply the same `Window` first with ranked A, then with newly file-only B, then newly ranked C, then with A vanished; assert B becomes selectable without auto-query, C enters ranked order, a vanished current subject falls back to the remaining ranked/file-only choice with at most one query, and no second table query is caused by union refresh alone. |
| `test_close_during_probe_drops_all_late_effects` | Timers stopped, no blocking shutdown wait, no applied/dropped counter mutation after close, no late widget/ownership effects. |
| `test_files_compatibility_navigation_selects_tab_without_banner_button` | No files_nav; compatibility slot selects Files even before first frame and does not query. |
| `test_probe_error_keeps_last_banner_and_frame` | Error payload shows appio error, retains accepted state, and releases current slot for retry. |
| `test_no_target_restores_admission_without_hiding_tabs` | No boot/cursor: target/displayed/pending None, waiting text, Files fallback, two accessible tabs. |
| `test_files_source_failure_preserves_exact_message_and_link` | Degraded-source string/diagnostic and Settings focus preserved before first frame; no table or query claimed available. |
| `test_current_terminal_file_failure_clears_but_transient_failure_does_not` | Exercise both transient predicate terms separately and neither; check row-model clear calls, last_result, quantities, and wanted state. |
| `test_explicit_show_window_driver_does_not_compute_clock_target` | Explicit range E is prepared/applied via poll-only path with no Qt event processing or target tick; clock target cannot replace E. |
| `test_frame_apply_sets_displayed_before_files_sync` | Observe prepared alias inside _sync_files and prove it names newly ready window; one Files adoption/query. |

### 9.6 Migration checklist — design 7.5

Every unchecked item is implementation work, not permission to weaken the original ownership, safety, export, liveness, or query assertions. Names and current locations below were checked in this worktree; design line offsets may differ.

Structural:

- [ ] `tests/test_ui.py:440::test_no_files_tab_tray_daemon_or_privileged_action`: replace blanket tab ban with exactly two tabs; preserve no tray/daemon/privilege controls.
- [ ] `tests/p3/test_collector_window.py:588::test_the_switch_is_the_only_root_control_and_sits_inside_settings`: remove only blanket QTabWidget prohibition; retain sole-root-control/Settings assertion.
- [ ] `tests/p3/test_collector_window.py:471::test_banner_and_files_deep_links_focus_settings_without_changing_state`: replace stack identity with Files-tab identity; retain no-state-change/deep-link and pre-first-frame message.
- [ ] `tests/p3/test_collector_window.py:496::test_opening_files_shows_the_real_table_and_changes_only_query_state`: remove files_nav and stack assertions; switching tabs must not add a query.
- [ ] `tests/p3/test_collector_window.py:521::test_a_degraded_file_source_is_reported_while_history_is_still_loading`: migrate stack identity only; preserve degraded message and hidden unavailable table.
- [ ] `tests/p3/test_collector_window.py:542::test_files_page_keeps_an_explicit_unavailable_message_and_settings_link`: migrate stack identity; preserve unavailable text, no-query, diagnostic and link.
- [ ] `tests/p3/test_collector_window.py:567::test_the_files_surface_never_appears_inside_the_overview_window`: assert Files is not inside the Overview tab; it is now inside the shared window.
- [ ] `tests/acceptance/test_no_later_scope.py:15::test_window_has_no_later_plan_widgets_actions_or_flows`: exactly two tabs; remove only `files` from forbidden object-name tuple; positively assert one Files surface; retain tray/menu/daemon/socket/bpf/polkit/privilege/setup prohibitions.

Harness and callers:

- [ ] `tests/p3/acceptance_harness.py:383::P3Harness.until`: exact changed signature `until(self, condition: Callable[[], bool], *, seconds: float = 5, process_events: bool = True) -> None`; call QApplication.processEvents each iteration only when true; retain transport pump/deadline; it does not drive the window itself.
- [ ] `tests/p3/acceptance_harness.py:552::P3Harness.query`: preserve direct prepare/readiness/ranking semantics but use `until(..., process_events=False)`; do not refresh, apply facts, or compute a target in the wait.
- [ ] `tests/p3/acceptance_harness.py:457::P3Harness.settle`: capture facts_applied; loop with events plus window.refresh until at least one new fact result applied; only then return if target None, otherwise continue until displayed == target. Delete captured history._window and its scope-is-None early exit.
- [ ] `tests/p3/acceptance_harness.py:491::P3Harness.advance`: after refresh, use the same two phases and applied-facts-before-no-target rule; keep daemon/client stepping and stopped automatic timers.
- [ ] `tests/p3/acceptance_harness.py:558::P3Harness.render`: `show_window(since)` then event-free apply_ready_frame loop until displayed since; `show_window(requested)` then the same loop until displayed requested. Delete direct _show_ready, view.set_window, and intervening query calls. No _tick_target or Qt events in these explicit loops.
- [ ] `tests/p3/test_history_overlap_entry.py:30,71-76,108-123::test_live_incomplete_reply_cannot_resurrect_export_overlap_in_production_entry`: keep this real production-entry integration test as its own migration item. `status_reader()` keeps the blocking `raw_status` read on the probe worker but no daemon ownership work; serialize every `daemon.step()` through the one owner thread used by `settle()` (`:108-121`, including the step at `:113`) and preserve the live client pump. Moving `setup.refresh()` to the probe worker must not create a second daemon owner or move the blocking status read to Qt. Preserve the production-entry, no-local-reacquire, verified-export, two-source, and overlap/coverage assertions at `:122-150`.
- [ ] Preserve assertions in `tests/p3/test_p3_acceptance.py:43-44,66,85-89,110-114,120,127,141,163,169,175,210`, `tests/p3/test_history_overlap_entry.py:110,125`, and `tests/p3/test_collector_window.py:1379`; drive them through the migrated harness. `_prepared` means displayed; read `_pending`/`_target` where the assertion is about requested state.

Behavioural rewrite:

- [ ] `tests/p3/test_collector_window.py:1017::test_pending_history_yields_to_new_scope_ownership_or_invalidation`: split into retain A while target B advances (display A, then prepare/display B), invalidation of A (one replacement), and changed ownership (one replacement, original ownership assertions preserved).

Sequencing changes: use the shared event-pumping probe helper before assertions whose facts are now asynchronous. Keep the named substantive observations.

- [ ] `tests/p3/test_collector_window.py:1152::test_window_keeps_in_flight_history_for_the_same_scope`: retain pumps > 1, probes > 1, ranking_settled > 0, and final applied-frame assertion at :1379.
- [ ] `tests/p3/test_collector_window.py:275::test_verified_status_round_trip_clears_silent_header_while_window_open`: await initial SILENT and subsequent verified recovery.
- [ ] `tests/p3/test_collector_window.py:309::test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF`: drive each of three distinct probes and applied states.
- [ ] `tests/p3/test_collector_window.py:362::test_label_recovers_after_silent_refresh_and_connection_reverification`: await each probe count/state transition.
- [ ] `tests/p3/test_collector_window.py:443::test_current_link_status_requires_verification_and_preserves_activation`: await facts before state assertion.
- [ ] `tests/p3/test_collector_window.py:630::test_the_four_honest_states_render_distinctly`: await banner/switch render.
- [ ] `tests/p3/test_collector_window.py:645::test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers`: await post-helper facts, not helper body.
- [ ] `tests/p3/test_collector_window.py:668::test_an_unknown_probe_disables_the_switch_instead_of_claiming_off`: await unknown probe application.
- [ ] `tests/p3/test_collector_window.py:675::test_pending_activation_stays_visible_across_a_restart`: await activation state.
- [ ] `tests/p3/test_collector_window.py:925::test_periodic_refresh_never_invokes_the_helper`: await each of three probes; retain setup.calls == [].
- [ ] `tests/p3/test_collector_window.py:999::test_history_is_prepared_before_rendering_and_stale_detail_is_hidden`: use pending for initial request, displayed/prepared for applied frame; retain pre-first-frame hidden data expectation.
- [ ] `tests/p3/test_collector_window.py:1058::test_a_refresh_failure_is_reported_instead_of_crashing_the_timer`: await error delivery or coordinator error in apply.
- [ ] `tests/p3/test_collector_window.py:1116::test_real_history_router_gates_rendering_until_ready`: distinguish pending requested scope from later displayed alias.
- [ ] `tests/p3/test_collector_window.py:610::test_the_switch_never_shows_the_click_before_the_probe`: await initial probed switch; retain consent/no-elevation assertions.
- [ ] `tests/p3/test_collector_window.py:687::test_consent_lists_the_exact_commands_before_any_elevation`: await initial facts; keep exact commands/disclosure.
- [ ] `tests/p3/test_collector_window.py:704::test_cancelled_consent_changes_nothing`: await stable probe; retain unchanged actions/calls/switch.
- [ ] `tests/p3/test_collector_window.py:724::test_a_failed_dismissal_reverts_the_switch_and_shows_the_exit_code`: await completion probe; retain retryability/exit code.
- [ ] `tests/p3/test_collector_window.py:737::test_a_dismissal_after_a_previous_success_reverts_to_the_probed_on_state`: await each probed ON/restored state.
- [ ] `tests/p3/test_collector_window.py:748::test_a_container_host_reason_disables_the_switch_and_is_shown_verbatim`: await reason/switch state without changing text.
- [ ] `tests/p3/test_collector_window.py:761::test_a_missing_helper_reports_one_hundred_twenty_seven`: await completion; preserve exit 127, message, disabled switch.
- [ ] `tests/p3/test_collector_window.py:778::test_enable_quiesces_local_collection_before_the_first_elevation`: await initial facts; preserve quiesce-before-invoke order and add spanning-probe coverage.
- [ ] `tests/p3/test_collector_window.py:795::test_a_failed_quiesce_aborts_the_elevation`: await initial facts; retain no invocation, error, not busy.
- [ ] `tests/p3/test_collector_window.py:813::test_restart_choice_warns_and_needs_a_distinct_confirmation`: await initial/completion facts; retain sequence, warning, separate confirmation.
- [ ] `tests/p3/test_collector_window.py:837::test_reboot_choice_never_runs_restart_or_reboot`: await activation; retain permitted call list and absence of power/session operations.
- [ ] `tests/p3/test_collector_window.py:855::test_a_dismissed_choice_leaves_the_switch_on_the_probe`: await the new probe; preserve invocation list/switch state.
- [ ] `tests/p3/test_collector_window.py:871::test_disable_requires_its_own_consent_and_imports_verified_metadata`: await OFF facts after reply; preserve consent/call/export identity.
- [ ] `tests/p3/test_collector_window.py:895::test_disable_without_verified_export_metadata_imports_nothing`: await retained ON facts; preserve no-import assertion.
- [ ] `tests/p3/test_collector_window.py:911::test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict`: await post-status probe; preserve consent and call.
- [ ] `tests/p3/test_collector_window.py:939::test_a_pending_operation_rejects_a_second_one_across_a_qt_heartbeat`: await initial facts; preserve single invocation/busy/disabled assertions.
- [ ] `tests/p3/test_p3_adversarial.py:388::test_helper_denial_and_absence_preserve_gui_ownership`: after helper busy clears, process events until fresh facts apply before asserting coordinator._local is present; helper completion alone now only submits that probe.
- [ ] `tests/p3/test_collector_window.py:972::test_a_reply_completed_on_a_worker_thread_updates_only_on_the_qt_thread`: await initial probe; preserve Qt-thread delivery and activation choice observations.

The unchanged tests for signed residuals, None versus measured-zero peaks, the active-window label for an empty supported range, A22 reasons, and contract-only query access remain regression requirements (`tests/test_ui.py:251-410`). Extend their structural fake for series instead of removing their observations.

### 9.7 Gates for the later implementation

1. Run full `python -m pytest` with the repository's offscreen test environment after implementation, not during this drafting task.
2. **specification decision:** the plan's exact typecheck gate is `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py`. Mypy is a development-only check, not a new runtime dependency. The plan provisions the checker if absent and pins its version in its recorded toolchain; do not change runtime requirements for it.
3. Inspect offscreen grabs for the approved arrangement, readable labels/percentages, visible gap support, and persistent tab navigation; geometry and identity tests provide the nonvisual assertions.
4. Before audit, compare implementation against the design/spec inventory and prove no changes to producer/protocol/storage/query contracts or unrelated privilege flows.

## 10. Not in this change

- The hour-of-day intensity strip: data exists, but it is excluded to bound this change.
- Range chips and the launch window: both stay disabled with their existing A22 reasons. Existing explicit supported-range query/render acceptance remains available; this feature adds no enabled range-chip control.
- Search in the ranking list.
- Tray behaviour, autostart, per-file BPF scope, retention, and cadence settings.
- Any change to QueryService, HistoryRouter, storage, or schema contracts. Calling those APIs differently is permitted; changing their signatures or guarantees is not.

No host transition, new runtime dependency, new measurement, daemon/protocol revision, or data migration is needed to implement this specification.
