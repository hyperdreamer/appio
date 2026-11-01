# appio console: calm redraw, tabs, drawn charts

Date: 2026-11-01
Status: design approved by the user; awaiting independent review
Supersedes nothing. Extends `2026-10-21-app-io-monitor-design.md` §8 for the P3 shell.

Behavioural authority is this document. Layout authority is the three mockups in
`assets/`. Where they disagree, layout follows the mockup and behaviour follows this
text, and the disagreement is listed in §9.

## 1. Background

Three faults were reported against the shipped P3 shell.

**1.1 The window flickers.** Three independent causes stack:

| Cause | Where | Effect |
| --- | --- | --- |
| The ranking list is emptied and rebuilt on every refresh | `AppIoWindow.refresh()` calls `QListWidget.clear()` then re-adds every row | Rows blink; the selected row and the scroll position are lost, then restored one tick later |
| The measurement window advances every 10 s and is treated as a new scope | `CollectorWindow._current_window()` computes `end = (now // RAW_BUCKET_NS + 1) * RAW_BUCKET_NS`; `Raw_BUCKET_NS` is 10 s (`contracts.py`), so the `Window` identity changes every 10 s, `_render_history()` calls `history.prepare()`, and while `history.ready(window)` is false the code calls `_hide_view()` | The whole detail area disappears, `loading history (...)` is shown, then the view returns |
| The refresh tick blocks the drawing thread | `CollectorWindow.refresh()` calls `setup.refresh()` → `production_probe()`, which runs `systemctl show` through `subprocess.run(timeout=5)` and opens a daemon socket with a 2 s deadline, synchronously inside the `QTimer` slot | The interface freezes for the duration of both calls, then catches up in a jump |

**1.2 Files has no return path.** `files_nav` swaps `QStackedWidget` pages; the Files
page carries `files_view`, `files_message` and `files_open_settings` and no control
that returns to the overview. The approved design (§8 of the 2026-10-21 document)
never had a Files page: it describes a *detail pane with `Overview` and `Files`
tabs*, so the return path was structural.

**1.3 The infographics were never built.** §8 and `assets/2026-10-21-detail-view.png`
promise a ranking list with inline bars and an Overview with a dual-sided `TOTAL`
lane, one lane per disk, a read/write donut, an attributed-share-by-disk panel and a
signed reconciliation strip. The shipped window renders labels and numbers only.
One element of that mockup — the hour-of-day intensity strip — has no query contract
anywhere in the codebase, so it cannot be built without new measurement.

## 2. Goals

1. No visible flicker. The ranking rows, the selection, the scroll position and the
   drawn frame survive every refresh tick.
2. Overview and Files are reachable from each other in one click from any scroll
   position, and switching never loses the selected application.
3. The Overview draws the approved infographics from the series and disk-share data
   the query contract already returns.
4. No new runtime dependency, no new measurement, no daemon or protocol change.

## 3. Non-goals

- Hour-of-day intensity strip: no data source exists.
- Range chips (`1h`/`6h`/`24h`/`30d`) and the launch window: both stay disabled with
  their existing A22 reasons (`formatting.A22_RANGE_REASON`, `A22_LAUNCH_REASON`).
- Search in the ranking list: not present today, not added here.
- Tray behaviour, autostart, per-file BPF scope, retention, cadence settings.
- Any change to `query.QueryService`, `HistoryRouter` contracts, storage or schema.

## 4. Decisions taken with the user

| Decision | Choice | Record |
| --- | --- | --- |
| Chart set | **A**: list bars, dual-sided TOTAL lane, one lane per disk, read/write donut, attributed share by disk, reconciliation strip. Hour-of-day strip excluded. | `assets/2026-11-01-chart-sets.png`, option 1 |
| Navigation | **A**: `Overview` and `Files` as tabs inside the detail pane; the ranking list stays on screen and drives both tabs | `assets/2026-11-01-navigation.png`, option 1 |
| Target state | Combined view of both choices | `assets/2026-11-01-target-state.png` |

## 5. Architecture

### 5.1 Components

```
src/appio/ui/
├── main_window.py      AppIoWindow: list + tab host, render-model diffing (rewritten)
├── charts.py           SeriesChart, DualSeriesChart, DonutChart, ShareBars (new, pure painting)
├── render_model.py     RankingModel, OverviewModel and their builders (new, pure data)
├── theme.py            palette and pen/brush constants used by the charts (new, small)
├── files_view.py       FilesView: unchanged behaviour, selector driven from outside
└── collector_window.py CollectorWindow: banner, probe worker, window/prepare logic
```

`render_model.py` and `charts.py` import only `appio.contracts` and the standard
library. They never import `HistoryRouter`, the daemon, BPF or setup modules; the
window is a client of the query contract (the same rule `files_view.py` already
follows).

### 5.2 Render model

`build_ranking(rows)` returns `RankingModel`:

```python
@dataclass(frozen=True)
class RankingRow:
    app_id: int
    app_key: str
    label: str            # "chromium  383.2 GB"
    total_bytes: int
    share: float          # total_bytes / max_total_bytes, 0..1; 0.0 when max is 0

@dataclass(frozen=True)
class RankingModel:
    rows: tuple[RankingRow, ...]
    empty_text: str | None
    unavailable: str | None
```

`build_overview(share, series, total, window)` returns `OverviewModel`:

```python
@dataclass(frozen=True)
class Kpi:
    caption: str          # "TOTAL" | "READ" | "WRITE" | "PEAK" | "DEVICE"
    text: str             # formatted value, or formatting.UNAVAILABLE

@dataclass(frozen=True)
class OverviewModel:
    app_id: int
    kpis: tuple[Kpi, ...]
    total_series: Series | None
    device_series: tuple[tuple[str, DeviceSeriesPoint], ...]   # device name, points
    series_unavailable: str | None
    share: DiskShare | None
    share_unavailable: str | None
    reconciliation: str | None
    coverage_line: str
    detail_line: str      # "window: since_boot; tier: raw"
```

Both builders are pure; every assertion in the test suite can be made against them
without a widget.

### 5.3 Ranking list and redraw discipline

The list keeps its rows between ticks. `AppIoWindow._apply_ranking(model)`:

1. Compute the current app-id order from the model.
2. If the order and every label are unchanged, do nothing. No model reset, no
   `clear()`, no selection write.
3. If only labels changed, update the item text in place, keeping the item objects.
4. If the order changed, rebuild the list but restore the selection by `app_id` and
   the vertical scroll value captured before the rebuild.

`QListWidget` stays; a `QAbstractListModel` is not introduced, because a full model
view rewrite buys nothing this change needs and would rewrite tests that are not in
scope.

`AppIoWindow.set_overview(model)` applies the same discipline to the KPIs and the
charts: each chart widget receives new data and repaints itself; no widget is
destroyed or re-created between ticks.

### 5.4 Tabs

The detail pane becomes a `QTabWidget` with exactly two tabs, `Overview` and
`Files`, in that order. The tab widget is inside the detail pane, to the right of
the ranking list, so the list is visible on both tabs.

- The banner loses `files_nav`. `CollectorWindow.open_files_unavailable()` keeps its
  name and signature but selects the Files tab, so no external caller breaks.
- `FilesView` stays the same widget. Its `app_selector` combo is hidden in the tab
  layout because the ranking list is now the only selector; the host calls
  `files_view.set_app_id(app_id)` whenever the ranking selection changes. The
  combo remains constructible so existing tests and callers still work.
- Switching tabs never clears the selection, re-prepares the window, or re-queries.

### 5.5 Staleness: the frame survives a window advance

`CollectorWindow._render_history()` changes to:

| Situation | Behaviour |
| --- | --- |
| No frame yet, window not ready | show `loading history (<view_state>)` in `history_state`, hide the tab pane (today's behaviour) |
| A frame is on screen, new window not ready | **keep the frame**, keep the tabs live, and set the existing `history_state` label to `updating (<view_state>)` above the pane; do not hide or blank anything |
| New window ready | apply the render model; clear `history_state` |

`_hide_view()` is therefore called only when no frame has ever been shown for the
current application scope. A window advance from 10 s to 20 s must produce zero
visible change when the numbers are unchanged.

### 5.6 Probes off the drawing thread

`CollectorWindow.refresh()` becomes a non-blocking tick:

1. If a probe is already in flight, do nothing this tick (coalesce).
2. Otherwise submit `setup.refresh()` to a single-slot
   `concurrent.futures.ThreadPoolExecutor`.
3. The completed `ServiceFacts` is delivered to the Qt thread through the existing
   `_ReplyBridge`-style queued signal and applied by `_apply_facts(facts)`.
4. `_apply_facts()` performs `coordinator.reconcile()`, `coordinator.tick_local()`,
   `toolbar/banner` rendering and the history readiness check — all of which are
   already non-blocking (`HistoryRouter.ready()` only inspects completed futures).

Consequences that must hold:

- No `subprocess`, socket, or `time.sleep` call runs on the Qt thread on a refresh
  tick.
- A probe that outlives its interval is never queued twice.
- `window.shutdown()` stops the timer and shuts the executor down without leaving a
  probe thread that mutates widgets after close.
- The opening `refresh()` in `run_interactive()` still renders the first banner
  without waiting on a probe; the first probe result arrives asynchronously.

### 5.7 Charts

`charts.py` holds three widgets plus one primitive. Each exposes its paint state as
data so an offscreen test can assert behaviour without reading pixels.

```python
class SeriesChart(QWidget):
    def set_series(self, points, *, kind: str) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def segments(self) -> tuple[ChartSegment, ...]:  # (kind, polygon points in widget coords)
    def is_available(self) -> bool: ...
```

- `DualSeriesChart` draws read above a centre line and write below it, each side
  scaled to its own maximum inside the lane (self-scaled, as the design mockup
  states), with a labelled right-hand gutter (`r 41.0 · w 342.2`).
- `DeviceLane` is the same widget with a single device's `DeviceSeriesPoint` tuple.
- `DonutChart` draws read vs write for the selected application with the two
  percentages beside it.
- `ShareBars` draws one row per device, bar width relative to the largest device,
  with the `Σ disks + residual = total` line beneath.

**Gap rule.** A gap is drawn as a break, never bridged:

- a gap exists between consecutive points when
  `next.bucket_start_mono_ns != current.bucket_start_mono_ns + current.bucket_secs * 10**9`;
- a point whose `spans_suspend` is true ends the current segment and starts a new one
  after it;
- `segments()` returns one entry per contiguous run, so a test can assert the count.

**Downsampling.** When the number of points exceeds the lane's pixel width, buckets
are aggregated per pixel column: bytes are summed, peaks take the maximum. No point
is dropped silently and the aggregation never bridges a gap.

**Empty and unavailable.** An empty series draws the lane's caption and no shape. An
unavailable series draws `unavailable: <reason>` centred in the lane. Neither draws
a zero line, because zero and unknown are different claims.

**Device selection for lanes.** Devices are ordered by attributable bytes descending;
the window draws at most three lanes and states `and N more` in the caption when
more devices have activity. Devices with no bytes in the window are not drawn.

### 5.8 Data flow

```
QTimer tick (1 s)
  → probe already in flight? yes → return
  → executor.submit(setup.refresh)
      ↓ queued signal
  → _apply_facts(facts): reconcile → tick_local → banner + switch → history check
  → _current_window() (10 s bucket boundary)
  → if changed and no frame yet: history.prepare(window)
  → if history.ready(window): _prepared = window; render model; diff-apply
```

`history.prepare()` and `history.ready()` are non-blocking; the only SQLite work on
the GUI thread is the `PRAGMA database_list` and the local precheck inside
`prepare()`, which run once per window advance (every 10 s), not on every tick.

## 6. Error handling

| Condition | Behaviour |
| --- | --- |
| `ranking()` raises `WindowUnavailableError` | list shows its empty text plus the reason; every KPI prints `unavailable`; lanes, donut and share bars show `unavailable: <reason>` |
| `disk_share()` raises | KPIs that depend on it print `unavailable`; the share panel and donut show the reason; the series chart still draws |
| `series()` raises or returns no points | lanes show `unavailable: <reason>` or `no samples in this window`; no shape is drawn |
| Device attribution unavailable (`coverage.device_state != "available"`) | donut falls back to task read/write; DEVICE KPI and residual print `unavailable`; the strip says `device attribution unavailable: <reason>` |
| Incomplete coverage / gaps | a gap is drawn as a break; the coverage line states the gap count and the state word from `Coverage` |
| Empty ranking | list shows `no applications observed`; Overview shows `unavailable` KPIs and no charts (today's `_render_empty`) |
| Files source unavailable | the existing page-level message is unchanged (`FILES_UNAVAILABLE` text plus reason) |
| Probe raises | the existing `_show_error` path; the last good banner and frame stay on screen |
| Window close while a probe runs | timers stop, executor shuts down, late completion is ignored (`_closed`) |

No value is replaced by zero. `formatting.UNAVAILABLE` remains the only spelling.

## 7. Test strategy

All tests are offscreen (`pytest-qt`, PyQt6 pinned exactly as `tests/test_ui.py` and
`tests/p3/test_collector_window.py` already do).

**7.1 Pure model tests** (`tests/test_ui.py`, new cases):

- `build_ranking` orders by `(-total_bytes, app_key)`, computes `share` against the
  largest row, and yields `empty_text` for an empty sequence.
- `build_overview` produces five KPIs in the fixed order, `unavailable` for a `None`
  peak and an unavailable device total, and a reconciliation string only when the
  device attribution is available.

**7.2 Chart geometry tests** (`tests/test_charts.py`, new file):

- contiguous points produce exactly one segment per side;
- a one-bucket hole produces two segments, and the polygon contains no point whose
  x lies inside the hole;
- `spans_suspend` splits a segment;
- `set_unavailable` leaves `segments()` empty and marks `is_available()` false;
- downsampling over `2 × width` points keeps the bucket count at the width, sums
  bytes and keeps the maximum peak;
- a device lane orders devices by bytes and caps at three.

**7.3 Window tests** (`tests/test_ui.py`):

- refreshing with identical data does not replace list items (identity of the
  `QListWidgetItem` objects is preserved) and does not change the current row;
- refreshing with a changed order keeps the selected `app_id` selected and preserves
  the scroll value;
- the tab widget holds exactly `Overview` and `Files`, in that order;
- switching tabs twice preserves the selection and does not call `set_window` on the
  view again;
- changing the ranking selection calls `files_view.set_app_id` with the new id;
- an unavailable series leaves the lane marked unavailable and no KPI showing `0`.

**7.4 Shell tests** (`tests/p3/test_collector_window.py`):

- a probe that blocks for longer than the interval does not block the tick: the
  test's probe sleeps and the tick returns promptly, with only one probe in flight
  ever observed;
- a window advance with an unchanged answer keeps `self._view.isVisible()` true and
  never shows `loading history` once a frame exists;
- `open_files_unavailable()` selects the Files tab and the banner has no `files_nav`
  button;
- the existing setup/consent/export/reconcile tests keep passing unchanged except
  where they assert the removed button or page swap; the diff lists each such test.

**7.5 Gates.** `python -m pytest` (full suite) and the typecheck command the plan
names must both pass before the audit.

## 8. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| `QTabWidget` inside the detail pane changes focus order and the existing `files_nav` tests | the tab widget is the only navigation; affected tests are named in the plan and updated deliberately, not deleted |
| Threading a probe into a Qt window invites late callbacks after close | the executor is shut down in `shutdown()`, `_closed` guards every application path, and a test covers close-during-probe |
| Repaint cost at 1 Hz with many buckets | one `paintEvent` per widget per tick, downsampled to the pixel width, no allocation of per-point objects in `paintEvent` |
| Charts could imply precision the data does not have | gaps break, unavailable never becomes zero, and the coverage line stays visible under the charts |
| `HistoryRouter.prepare()` does two small SQLite calls on the GUI thread | it runs once per 10 s window advance, not per tick; the measured cost stays out of the 1 s path |

## 9. Mockup disagreements

- `assets/2026-10-21-detail-view.png` shows range chips (`1h`, `6h`, `24h`, `30d`) and
  an hour-of-day intensity strip. This design keeps the window at `since boot` and
  drops the intensity strip; both are recorded as non-goals in §3.
- The 2026-10-21 mockup shows the ranking list with a search field. This change keeps
  the list without a search field.
- Everything else in that mockup — inline bars, the dual-sided TOTAL lane, one lane
  per disk, the donut, the attributed-share panel, the reconciliation strip — is
  drawn as approved.
