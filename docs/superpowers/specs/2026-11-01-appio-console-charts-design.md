# appio console: calm redraw, tabs, drawn charts

Date: 2026-11-01
Status: revised after three independent design reviews (D-1…D-13, R-1…R-9, S-1…S-8)
Supersedes nothing. Extends `2026-10-21-app-io-monitor-design.md` §8 for the P3 shell.

Behavioural authority is this document. Layout authority is the three mockups in
`assets/`. Where they disagree, layout follows the mockup and behaviour follows this
text, and the disagreement is listed in §9.

## 1. Background

Three faults were reported against the shipped P3 shell.

### 1.1 The window flickers, and freezes

| Cause | Evidence | Effect |
| --- | --- | --- |
| The ranking list is emptied and rebuilt on every refresh | `AppIoWindow.refresh()` calls `self._list.clear()` then re-adds every row (`src/appio/ui/main_window.py:203-219`) | Rows blink and the scroll position resets. The selected row is *not* lost: the same call recomputes its index and calls `setCurrentRow(target)` (`:219`). The flicker comes from item destruction and re-creation |
| The measurement window advances every 10 s and is treated as a new scope | `CollectorWindow._current_window()` computes `end = (now // RAW_BUCKET_NS + 1) * RAW_BUCKET_NS`; `RAW_BUCKET_NS` is 10 s (`src/appio/contracts.py:97`). `_render_history()` calls `history.prepare(window)` at `src/appio/ui/collector_window.py:720`, checks readiness at `:721`, and otherwise calls `_hide_view()` at `:724` and shows `loading history (...)` at `:725` | The whole detail area disappears and returns every 10 s |
| The refresh tick blocks the drawing thread | `CollectorWindow.refresh()` calls `setup.refresh()` (`:618`) → `production_probe()` → `subprocess.run(timeout=5)` for `systemctl show` (`:1000`) and a daemon socket round-trip with a 2 s deadline (`:1006`), synchronously inside the `QTimer` slot | The interface freezes for the duration of both calls |
| Every invalidation re-prepares the same scope | `CollectorCoordinator.reconcile()` always calls `history.invalidate()` (`src/appio/collector_owner.py:196`) and `tick_local()` invalidates again on every sampled frame (`:208`); `HistoryRouter.invalidate()` also runs for imported sources (`src/appio/history_router.py:429`) and transport gaps (`:447`), and sets `view_state = "unavailable"` (`:413-421`) | With local collection active, a preparation is retired and restarted about once per second |

### 1.2 Files has no return path

`files_nav` is built at `src/appio/ui/collector_window.py:466`, connected at `:522`,
and its slot switches the stacked page at `:554`. The Files page is built at `:491`
with the table, message and settings button added at `:507-510`, and it carries no
control that returns to the overview. The approved design never had a Files page: it
describes a *detail pane with `Overview` and `Files` tabs*.

### 1.3 The infographics were never built

§8 of the 2026-10-21 design and `assets/2026-10-21-detail-view.png` promise a ranking
list with inline bars and an Overview with a dual-sided `TOTAL` lane, one lane per
disk, a read/write donut, an attributed-share-by-disk panel and a signed
reconciliation strip. The shipped window renders labels and numbers only
(`src/appio/ui/main_window.py:128-160`).

The hour-of-day intensity strip is **out of scope by choice, not for lack of data**:
`SeriesPoint` carries `wall_start_seconds` (`src/appio/contracts.py:430`), populated
from the persisted clock map (`src/appio/query.py:657-665`). §9 records that the
approved chart-set mockup's caption says otherwise.

## 2. Goals

1. No visible flicker. Ranking rows, selection, scroll position and the drawn frame
   survive every refresh tick.
2. Overview and Files are one click apart and both stay usable while overview history
   loads or is replaced.
3. The Overview draws the approved infographics from data the contracts already return.
4. No new runtime dependency, no new measurement, no daemon, protocol or schema change.

## 3. Non-goals

- The hour-of-day intensity strip (data exists; excluded to bound the change).
- Range chips and the launch window: both stay disabled with their existing A22
  reasons (`src/appio/ui/formatting.py:76-87`).
- Search in the ranking list.
- Tray behaviour, autostart, per-file BPF scope, retention, cadence settings.
- Any change to `QueryService`, `HistoryRouter`, storage or schema **contracts**. This
  change may call them differently; it does not change their signatures or guarantees.

## 4. Decisions taken with the user

| Decision | Choice | Record |
| --- | --- | --- |
| Chart set | **A**: list bars, dual-sided TOTAL lane, one lane per disk, read/write donut, attributed share by disk, reconciliation strip | `assets/2026-11-01-chart-sets.png`, option 1 |
| Navigation | **A**: `Overview` and `Files` as tabs inside the detail pane | `assets/2026-11-01-navigation.png`, option 1 |
| Target state | Combined view of both | `assets/2026-11-01-target-state.png` |

## 5. Architecture

### 5.1 Components

```
src/appio/ui/
├── main_window.py      AppIoWindow: ranking list + tab host, render-model diffing (rewritten)
├── charts.py           SeriesChart, DualSeriesChart, DeviceLane, DonutChart, ShareBars (new)
├── render_model.py     RankingModel, OverviewModel and their builders (new, pure data)
├── theme.py            palette and pen constants shared by the charts (new, small)
├── files_view.py       FilesView: transient-scope classification, scope admission, union population
└── collector_window.py CollectorWindow: banner, probe worker, target/displayed/pending
```

`render_model.py` imports only `appio.contracts`, `appio.ui.formatting` and the
standard library. `charts.py` and `theme.py` import PyQt6 (the project's only runtime
dependency) and `appio.contracts`. No new module imports `HistoryRouter`, the daemon,
BPF or setup modules.

### 5.2 Render model

```python
@dataclass(frozen=True)
class RankingRow:
    app_id: int
    app_key: str
    label: str            # "chromium  383.2 GB"
    total_bytes: int
    share: float          # total_bytes / max_total_bytes in 0..1; 0.0 when max is 0

@dataclass(frozen=True)
class RankingModel:
    rows: tuple[RankingRow, ...]
    empty_text: str | None
    unavailable: str | None

@dataclass(frozen=True)
class Kpi:
    caption: str          # "TOTAL" | "READ" | "WRITE" | "PEAK" | "DEVICE"
    text: str             # formatted value, or formatting.UNAVAILABLE

@dataclass(frozen=True)
class DeviceLaneModel:
    dev: str                                  # raw boot-scoped "major:minor"
    points: tuple[DeviceSeriesPoint, ...]
    read_bytes: int
    write_bytes: int

@dataclass(frozen=True)
class OverviewModel:
    app_id: int
    kpis: tuple[Kpi, ...]
    task_points: tuple[SeriesPoint, ...]      # empty when unavailable
    device_lanes: tuple[DeviceLaneModel, ...] # at most three, ordered by total bytes
    more_devices: int
    series_unavailable: str | None
    share: DiskShare | None
    share_unavailable: str | None
    reconciliation: str | None
    coverage_line: str
    detail_line: str
```

Both builders are pure; assertions can be made without a widget.

### 5.3 Ranking list and redraw discipline

`AppIoWindow._apply_ranking(model)`:

1. Compare by the tuple `(app_id, label, total_bytes, share)` — **not** by label
   alone, because `format_bytes` rounds: a largest total moving from 1 500 to 1 530
   bytes leaves every label unchanged while the shares of the other positive rows change
   (the leader stays at 1.0, zero rows stay at 0.0).
2. Same id order: update item text and stored share in place, repaint only rows whose
   share changed. No `clear()`, no model reset, no selection write.
3. Different id order: rebuild, then restore the selection by `app_id` and the
   `QScrollBar` value captured before the rebuild.

Bars are drawn by a `QStyledItemDelegate` reading the share from
`Qt.ItemDataRole.UserRole + 1`; it never parses label text.

### 5.4 Tabs, and one subject

The detail pane is a `QTabWidget` with exactly two tabs, `Overview` then `Files`, to
the right of the ranking list.

**One subject.** The window keeps a single `_subject_app_id`, updated from either the
ranking list or the Files selector. The Overview and the Files table both render that
subject.

**One highlight contract.** The ranking list highlight follows the subject exactly:
a ranked subject highlights its row; a subject that is not in the ranking clears the
highlight (no row selected) while the list keeps its scroll position. There is no
second highlight state.

**Files population is a union.** `FilesView` replaces `_load_ranking()` with
`_load_choices()`:

1. ranked applications, in ranking order, from `history.ranking(window, "total_bytes")`;
2. then every application from `history.local.file_applications(window)` whose
   `registry.id_for(app_key)` has no ranking row, appended by name.

`QueryService.file_applications()` returns `(app_key, name, total_bytes)` tuples
(`src/appio/query.py:939`, `:981-983`), keyed through `registry.id_for(app_key)`
exactly as `_load_ranking()` already does for the ranking-empty case
(`src/appio/ui/files_view.py:683-690`). The union is what makes a file-only
application reachable in a **mixed** window; the existing fallback only covers a
window whose entire ranking is empty.

`FilesView` owns the selector's visibility: `app_selector.setVisible(len(choices) >
len(ranked))`. When hidden, the ranking list is the only selector, as the mockup shows.

**Subject not in the ranking.** When the subject comes from a file-only row, the
Overview KPIs read `unavailable` with the reason `not in the process ranking`, the
lanes draw nothing, and the ranking highlight is cleared per the rule above.

**Files stays alive across a replacement, and a discarded result never clears it.**

```python
class FilesView:
    def set_transient_scope(self, transient: bool) -> None: ...  # classification switch
    @contextmanager
    def transient_scope(self) -> Iterator[None]: ...             # set in, clear in a finally
    def set_scope_ready(self, ready: bool) -> None: ...          # query admission and controls
    def refresh_wanted(self) -> bool: ...
    def flush_refresh(self) -> None: ...                         # at most one query
```

Three rules replace any hold or counter:

1. **Transient classification.** `set_transient_scope(True)` is set immediately before a
   call that can invalidate the router and cleared immediately after it in a
   `try/finally`. While it is true, a delivery that fails with `WindowUnavailableError`
   does **not** call `_set_unavailable()`: the row model and footer stay as they are, the
   footer gains `updating to <scope>`, and `_refresh_wanted` is set. Outside that window
   the existing behaviour stands unchanged (`_set_unavailable()` clears the table,
   `src/appio/ui/files_view.py:815-817`). Being an idempotent boolean set and cleared
   inside one call, it cannot leak or become unbalanced.
2. **Query admission.** `set_scope_ready(False)` while a preparation is pending. While it
   is false, every programmatic entry point — `set_window()` when adopting,
   `set_app_id()`, `set_sort()` and `request_refresh()` — records `_refresh_wanted` and
   returns without issuing a query, and the selector, sort and search controls are
   disabled. This covers the ranking list changing the shared subject while a window is
   pending, which disabling widgets alone does not.
3. **One flush, one owner.** After `_apply_frame()` the host calls
   `set_scope_ready(True)` and then `flush_refresh()`, which issues **exactly one** query
   when `_refresh_wanted` is set and the router is ready for the displayed window, and
   clears the flag. If no frame was applied and the router is ready again, the host flushes
   on the next tick. `set_window()` keeps its current semantics (a different window adopts
   *and* queries), and `_apply_frame()` never adds a second `request_refresh()` for the same
   window.

Why a discarded result must be classified rather than ignored: `HistoryRouter.invalidate()`
and `prepare()` retire file futures with
`WindowUnavailableError("file_source_unavailable", "File history generation retired.")`
(`src/appio/history_router.py:137-153`, called from `:415` and `:749`), and they settle
those futures on the calling thread, so the delivery reaches `_on_delivered()`
(`src/appio/ui/files_view.py:609-617`) **inside** the invalidating call itself.

**Every invalidating call site is wrapped:** `_pump()` → `on_gap()` →
`record_transport_gap()` (`src/appio/ui/collector_window.py:1125-1128` →
`src/appio/history_router.py:447`), `reconcile()` (`src/appio/collector_owner.py:196`),
`tick_local()` when it samples (`:208`), `prepare()` (`src/appio/history_router.py:742`),
and `_after_disable()` → `accept_export()` → `add_source()`
(`src/appio/ui/collector_window.py:944` → `src/appio/collector_owner.py:252` →
`src/appio/history_router.py:429`).

### 5.5 Target, displayed and pending

| Field | Meaning |
| --- | --- |
| `_target: Window \| None` | the window the clock and boot row currently imply |
| `_displayed: Window \| None` | the window whose render model is on screen |
| `_pending: Window \| None` | the window whose preparation is in flight |
| `_invalidated: bool` | derived, not asserted: the router reports a retired state |

**`_invalidated` is derived from the router, never tracked by call site.** Every
invalidation path ends in `HistoryRouter.invalidate()`, which sets
`view_state = "unavailable"` (`src/appio/history_router.py:413-421`), and every
preparation sets `view_state = "loading"` (`:757`). The window therefore derives:

```python
def _history_retired(self) -> bool:
    return self.history.view_state.startswith("unavailable")
```

That single predicate covers `reconcile()`, a sampling `tick_local()`, `add_source()`
and `record_transport_gap()` without the host needing to enumerate them.

Every tick:

```
1. _target = _current_window()
   files_view.set_scope_ready(_pending is None)
   if _target is None:
       _displayed = None; _pending = None
       show "waiting for the first sample" inside the Overview tab; return
2. if _pending is not None:
       if _history_retired():                 # the in-flight preparation was retired
           _pending = None                    # fall through to step 3
       elif history.ready(_pending):
           _apply_frame(); return
       else:
           return
3. _invalidated = _history_retired() and _displayed is not None
   if _pending is None and (_invalidated or _displayed is None or _target != _displayed):
       with files_view.transient_scope():      # §5.4 rule 1
           history.prepare(_target)
       _pending = _target
       files_view.set_scope_ready(False)
       if history.ready(_pending):
           _apply_frame()

_apply_frame():
    apply the render model for _pending through _show_ready(_pending)
    _displayed = _pending; _pending = None; _invalidated = False
    files_view.set_scope_ready(True)
    if the Files table's window != _displayed:
        files_view.set_window(_displayed)      # adopts and queries once
    files_view.flush_refresh()                 # at most one query, only if wanted
```

Properties this guarantees, each with a test in §7.4:

- A frame is applied at most once per preparation, and no preparation is issued while
  one is in flight (no reprepare loop).
- An invalidation during a preparation retires that preparation and causes exactly one
  replacement preparation for the current target — the case that previously stalled
  forever, because `ready()` can never become true for a retired generation.
- After a frame is applied and nothing invalidates, no further preparation is issued
  until the target changes.
- With local collection active, each sampled frame invalidates, so one replacement
  preparation follows each sample; the frame stays on screen throughout.

**Reconcile policy.** `reconcile()` is skipped when `facts == _facts` **and** `_pending
is not None`, because reconciliation would retire the generation the window is waiting
for. This deliberately generalizes today's `keep_pending` guard
(`src/appio/ui/collector_window.py:628-640`), which additionally requires
`daemon_verified`, `answering` and `view_state == "loading"`.

**Files while a replacement is pending.** The tabs stay visible in every state. While
`_pending is not None`, the Files tab keeps the table it already has, the footer reads
`updating to <scope>`, and `set_scope_ready(False)` disables its controls and refuses new
queries; `_apply_frame()` re-enables them and flushes at most one query. Before the first
frame, Files shows today's exact page-level texts: `waiting for the first sample`, or the
degraded-source message built at `src/appio/ui/collector_window.py:578-583`.

### 5.6 Probes off the drawing thread, and what stays on it

Every tick, in this order:

1. `self._pump()` inside `files_view.transient_scope()` — the pump can invalidate the
   router through `on_gap()`, and those futures settle inside the call. It drives the
   client's reads, writes, deadlines and reconnects (`IodClient.pump()`,
   `src/appio/iodclient.py:793`).
2. §5.5 steps 1–3: compute `_target`, poll the pending preparation (retiring it when
   the router reports a retired state), and prepare exactly one replacement when
   needed. These run **every tick**, independently of the probe; the old
   `_poll_history` early-return when the view is visible is deleted.
3. If no probe is in flight, submit `setup.refresh()` to a single-slot
   `ThreadPoolExecutor`. If one is in flight, skip **only the submission**.
4. Completion arrives on the Qt thread through a queued signal → `_apply_facts(payload)`,
   which wraps its own `reconcile()` and `tick_local()` calls in
   `files_view.transient_scope()` for the same reason.

**Guarantee.** No `subprocess`, no blocking socket connect/recv and no `time.sleep`
runs on the Qt thread from a refresh tick.

**Not claimed.** These still run on the Qt thread, unchanged in frequency:

| Call | Work | Why it stays |
| --- | --- | --- |
| `coordinator.tick_local()` (`src/appio/collector_owner.py:201`) | process and cgroup reads, aggregation, one sampler commit (`src/appio/live.py:534-588`) | the window's own local collection |
| `_current_window()` (`src/appio/ui/collector_window.py:690-703`) | one `SELECT boot_id FROM boot` | one indexed row |
| `history.prepare()` (`src/appio/history_router.py:742`) | window validation, file-request retirement, state reset, one `PRAGMA database_list` per source, local submit, possible remote dispatch | once per window change or invalidation |
| `history.ready()` (`src/appio/history_router.py:841`) | consumes completed futures, remaps ranking rows, queues and dispatches detail requests | bounded by the application count |
| non-blocking send in `IodClient._flush()` (`src/appio/iodclient.py:1313`) | `sock.send()` on a non-blocking socket | does not block |

**Arbitration with setup operations: a submission ticket, not just `_busy`.**

| Field | Meaning |
| --- | --- |
| `_setup_generation: int` | incremented when a helper operation starts **and** when it completes |
| `_probe_ticket: int` | incremented for every submission; only the newest ticket may be applied |

Each submission hands the executor a payload `(facts, ticket, generation)` captured at
submission time, and `_apply_facts(payload)` applies it only when **all** of these hold:

- `payload.ticket == self._probe_ticket` — no newer submission exists;
- `payload.generation == self._setup_generation` — no setup operation happened since;
- `_busy` is false.

Anything else is dropped and counted in `_dropped_facts`.

The ticket is what closes the case a `_busy`-only guard misses: a probe that starts
before an enable, observes OFF, stays in flight through the whole helper operation, and
delivers after `_on_reply()` cleared `_busy`. `_on_reply()` increments
`_setup_generation`, clears `_busy`, and immediately submits a fresh probe, which
allocates a **new** ticket; the older probe's ticket is then behind `_probe_ticket`, so
its result is rejected even though it arrives after the operation ended. A single
generation field cannot do this, because the fresh submission would overwrite it.

Dropping matters because `decide_owner()` grants local collection for inactive/absent
facts (`src/appio/collector_owner.py:58-64`) and `reconcile()` can acquire the lease and
build the local collector (`:177-190`).

When a result **is** applied, the existing promotion of a verified, answering history
connection over a silent one-shot probe runs first
(`src/appio/ui/collector_window.py:624-627`), then `reconcile()`, then `tick_local()`,
then the §5.5 window step.

`window.shutdown()` stops both timers, shuts the executor down, and `_closed` guards
every apply path.

### 5.7 Charts

`charts.py` holds one primitive and three widgets. Each exposes its paint state as
data, so an offscreen test can assert behaviour without reading pixels.

```python
@dataclass(frozen=True)
class ChartColumn:            # one drawn column, after downsampling
    start_mono_ns: int
    end_mono_ns: int
    read_bytes: int
    write_bytes: int
    peak_read_bps: int | None
    peak_write_bps: int | None
    starts_run: bool          # True when this column begins a new run

class SeriesChart(QWidget):
    def set_task_points(self, points: Sequence[SeriesPoint]) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def columns(self) -> tuple[ChartColumn, ...]      # aggregated values actually drawn
    def segments(self) -> tuple[ChartSegment, ...]    # (side, run index, polygon points)
    def is_available(self) -> bool: ...
    def unavailable_reason(self) -> str | None: ...

class DonutChart(QWidget):
    def set_shares(self, read_bytes: int, write_bytes: int) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def shares(self) -> tuple[float, float] | None    # read fraction, write fraction
    def unavailable_reason(self) -> str | None: ...

class ShareBars(QWidget):
    def set_rows(self, rows: Sequence[tuple[str, int]]) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def fractions(self) -> tuple[tuple[str, float], ...]
    def unavailable_reason(self) -> str | None: ...
```

`columns()` is the observation surface for aggregation, `segments()` the geometry
surface, and `shares()` / `fractions()` the composition surface; every widget also
reports why it is unavailable.

- `DualSeriesChart` draws read above a centre line and write below, each side scaled to
  its own maximum, with a right-hand gutter (`r 41.0 · w 342.2`).
- `DeviceLane` is the same widget fed with `DeviceSeriesPoint` tuples; the gutter reads
  `r <rbytes> · w <wbytes>`.
- `DonutChart` draws read vs write with both percentages.
- `ShareBars` draws one row per device, width relative to the largest, with
  `Σ disks <x> + residual <y> = <total>` beneath.

**Breaks are limited to what the data can prove.** `Coverage` carries counts only —
`gap_intervals`, `overlap_uncertain_intervals`, `spans_suspend_intervals`,
`spans_suspend` (`src/appio/contracts.py:335-338`) — and `Series` carries no gap ranges
(`:497-517`). Therefore:

| Lane | Break rule |
| --- | --- |
| `TOTAL` (task) | break between consecutive `SeriesPoint`s when `next.bucket_start_mono_ns != current.bucket_start_mono_ns + current.bucket_secs * 10**9`, and after any point whose `spans_suspend` is true |
| device | break on the same bucket-adjacency rule only; `DeviceSeriesPoint` has no `spans_suspend` (`src/appio/contracts.py:460-477`) |

A recorded gap that still produced a populated bucket is not locatable from these
contracts; the coverage line reports `gaps: <n>` instead. §9 corrects the mockup.

**Downsampling.** Above the lane's pixel width, buckets aggregate per column: bytes
summed, peaks maximum, and a column containing a break starts a new run. Nothing is
dropped silently and no gap is bridged.

**Empty, unavailable, no-zero.** An empty series draws the caption and no shape; an
unavailable series draws `unavailable: <reason>`; neither draws a zero line.

**Device lanes.** Ordered by attributable bytes descending, capped at three, with
`and <n> more` when more devices have activity. The label is the raw boot-scoped
`major:minor` token (`DeviceSeriesPoint.dev`, `DeviceShareRow.dev`); no name or mount
point exists in the contracts.

### 5.8 Data flow

```
QTimer tick (1 s)
  → transient_scope(): _pump()                [on_gap may invalidate inside this call]
  → §5.5 steps 1-3 (target → poll pending → retire → prepare)
  → probe in flight? skip the submission only
  → executor.submit(setup.refresh)             [payload = (facts, ticket, generation)]
  → queued signal → _apply_facts(payload)
  → _apply_facts: ticket or generation stale, or _busy? drop, count, return
                  promote verified connection
                  → transient_scope(): reconcile (unless keep-pending) + tick_local
                  → banner/switch → §5.5 steps 1-3 again
```

### 5.9 State fields

| Field | Meaning | Set by |
| --- | --- | --- |
| `_target` | window implied by clock and boot row | each tick |
| `_displayed` | window whose model is on screen | apply; `None` when no target |
| `_pending` | preparation in flight | prepare; `None` on apply or retirement |
| `_invalidated` | router reports a retired state | derived each tick from `view_state` |
| `_facts` | last applied facts | `_apply_facts` |
| `_dropped_facts` | counter of results dropped for staleness or busy | `_apply_facts` |
| `_probe_inflight` / `_probe_ticket` | single probe, and the ticket of the newest submission | submit / delivery |
| `_setup_generation` | setup-operation generation | `_start`, `_on_reply` |

## 6. Error handling

| Condition | Behaviour |
| --- | --- |
| `ranking()` raises `WindowUnavailableError` | list shows the reason; KPIs `unavailable`; lanes, donut, share bars `unavailable: <reason>` |
| `disk_share()` raises | dependent KPIs `unavailable`; share panel and donut show the reason; task lane still draws |
| `series()` raises or returns no points | lanes `unavailable: <reason>` or `no samples in this window` |
| Device attribution unavailable | donut still shows task read/write; DEVICE KPI and residual `unavailable`; strip reads `device attribution unavailable: <reason>` |
| Recorded gaps | coverage line states `gaps: <n>`, `suspends: <bool>`, complete/incomplete; lanes break only where §5.7 proves a hole |
| Empty ranking | `no applications observed`; Overview KPIs `unavailable`, no charts |
| File-only subject | KPIs `unavailable`, reason `not in the process ranking`; ranking highlight cleared |
| Pending replacement | last frame stays; `updating (<view_state>)`; Files keeps its table with queries refused and controls disabled |
| Retired preparation | retirement detected from `view_state`, one replacement preparation, frame unchanged meanwhile |
| Files source unavailable | existing message at `src/appio/ui/collector_window.py:578-583` |
| Probe raises | existing `_show_error` path; last banner and frame stay |
| Close while a probe runs | timers stop, executor shuts down, `_closed` guards |

No value is replaced by zero; `formatting.UNAVAILABLE` stays the only spelling.

## 7. Test strategy

All tests are offscreen (`pytest-qt`, PyQt6 pinned as existing tests do).

### 7.1 Pure model tests (`tests/test_ui.py`)

- `build_ranking` orders by `(-total_bytes, app_key)`, computes `share` against the
  largest row, yields `empty_text` for an empty sequence.
- `build_overview` produces five KPIs in fixed order, `unavailable` for a `None` peak
  and an unavailable device total, at most three device lanes ordered by bytes with the
  right `more_devices`, and a reconciliation string only when device attribution is
  available.

### 7.2 Chart tests (`tests/test_charts.py`)

- contiguous points produce exactly one run per side;
- a one-bucket hole produces two runs, and no polygon point lies inside the hole;
- a `spans_suspend` point splits the task run;
- `set_unavailable` leaves `segments()` empty, `is_available()` false, and
  `unavailable_reason()` equal to the reason;
- **downsampling**: over `2 × width` points, `columns()` has at most `width` entries,
  byte sums are preserved, `peak_read_bps`/`peak_write_bps` are the maxima of their
  buckets, and a column straddling a break has `starts_run == True`;
- **downsampling across a gap**: with a hole inside a downsampled range, assert per side
  that the runs split at the hole and that no run's polygon spans the hole's horizontal
  interval — every point of a run lies strictly left of `hole_start` or strictly right of
  `hole_end`. "At least two runs" alone is not sufficient, because a polygon can bridge a
  hole without a vertex inside it (guards U-6);
- `DonutChart.shares()` matches the byte fractions and `ShareBars.fractions()` matches
  per-device totals against the largest device;
- painting smoke: each widget `grab()`s to a non-empty pixmap, and an unavailable chart
  renders its reason text.

### 7.3 Window tests (`tests/test_ui.py`)

- identical data does not replace list items and does not change the current row;
- **changed shares with identical labels** repaint affected rows and rebuild nothing;
- a changed order keeps the selected `app_id` and the scroll value;
- the tab widget holds exactly `Overview` then `Files`;
- switching tabs twice preserves the subject and does not call `set_window` again;
- the Files selector is hidden when every file application is ranked, and shown when a
  file-only application exists;
- selecting a file-only application sets the subject, **clears the ranking highlight**,
  keeps the list scroll position, and puts the Overview in the `not in the process
  ranking` state;
- an unavailable series leaves the lane marked unavailable and no KPI reading `0`.

### 7.4 Shell tests (`tests/p3/test_collector_window.py`)

- a slow probe does not block the tick: with a probe sleeping 300 ms a tick returns
  promptly, only one submission is in flight, **and** the client is still pumped and a
  pending history window still becomes ready during the probe;
- a window advance replaces the frame: the old frame stays, `loading history` never
  appears once a frame exists, and the **new** window is eventually displayed;
- after a frame is applied, no further `prepare()` is issued until the target changes or
  the router is invalidated;
- unchanged facts while a read is in flight do not call `reconcile()`;
- **invalidation retires a pending preparation**: prepare A, invalidate the router
  (through `reconcile()`, a sampling `tick_local()`, `add_source()` or
  `record_transport_gap()`), and assert that the next tick issues exactly one
  replacement preparation for A and that A is displayed when it becomes ready (guards
  S-1, which previously stalled forever);
- **a probe spanning a setup operation is rejected**: start a slow probe, run and
  complete an enable, then deliver the old probe result; it increments `_dropped_facts`
  and applies nothing, while the probe submitted at operation completion is applied
  (guards S-3);
- an OFF probe completing during enable never reacquires the local writer;
- Files across a replacement: with an outstanding file future, an invalidation produced
  by **`_pump()` → `on_gap()` → `record_transport_gap()`**, and one produced by
  `accept_export()` → `add_source()`, each leave the table intact with the footer reading
  `updating to ...`, because the call was wrapped in `transient_scope()` (guards U-1, U-2);
- query admission while a scope is pending: `set_app_id()`, `set_sort()`, `set_window()`
  and a ranking-list subject change set `_refresh_wanted` and issue **no** query;
  `flush_refresh()` after the frame is applied issues exactly one (guards U-3, U-4);
- a probe that spans a setup operation and delivers **after** the replacement probe was
  submitted is rejected by ticket, does not overwrite the newer result, and increments
  `_dropped_facts` (guards T-3);
- mixed window: ranked A plus file-only B — the selector lists both and B is queried;
- close during a probe applies nothing and raises nothing;
- `open_files_unavailable()` selects the Files tab; the banner has no `files_nav`.

### 7.5 Existing tests that must change

Structural (tabs replace the stacked pages):

| Test | Assertions to migrate |
| --- | --- |
| `tests/test_ui.py::test_no_files_tab_tray_daemon_or_privileged_action` `:444` | forbids any `QTabWidget`; replace with "no tray, no daemon control, exactly two tabs" |
| `test_the_switch_is_the_only_root_control_and_sits_inside_settings` `:599` | the blanket `QTabWidget` prohibition; keep `:592` |
| `test_banner_and_files_deep_links_focus_settings_without_changing_state` `:482-489` | Files page/stack identity; keep the deep-link and no-state-change assertions and the pre-first-frame Files message |
| `test_opening_files_shows_the_real_table_and_changes_only_query_state` `:505-515` | `files_nav` click, stack identity, and the extra query on open — a tab switch must not re-query |
| `test_a_degraded_file_source_is_reported_while_history_is_still_loading` `:532-537` | stack identity; keep the degraded message and hidden table |
| `test_files_page_keeps_an_explicit_unavailable_message_and_settings_link` `:553-564` | stack identity; keep the unavailable text, no-query, diagnostic and settings link |
| `test_the_files_surface_never_appears_inside_the_overview_window` `:572-578` | "Files is not a descendant of the overview window" — Files now lives in the window; replace with "Files is not inside the Overview **tab**" |
| `tests/acceptance/test_no_later_scope.py::test_window_has_no_later_plan_widgets_actions_or_flows` `:18` | asserts `findChildren(QTabWidget) == []` on the real window built by `tests/acceptance/test_p1_p2.py:201`; replace with "exactly two tabs, no tray and no later-plan widget" |

**Supported unchanged, no migration needed.** `_prepared` survives as a read-only
property alias for `_displayed`, and `_show_ready(window)` remains the name of the apply
step, so the acceptance harness keeps working without edits
(`tests/p3/acceptance_harness.py:552`). Three readers also keep working unchanged:
`tests/p3/test_p3_acceptance.py:43-44`, `tests/p3/test_history_overlap_entry.py:110,125`,
and `tests/p3/test_collector_window.py:1379`. The alias reports **display** state only; a
test that needs the requested scope reads `_pending` or `_target` instead.

Behavioural rewrite (not a sequencing change):

| Test | Required new phases |
| --- | --- |
| `test_pending_history_yields_to_new_scope_ownership_or_invalidation` `:1035-1046` | The new algorithm **retains** a pending preparation instead of replacing it. Rewrite as three cases: (a) *retain* — prepare A, change the target to B, assert A is still `_pending` and no second prepare happened; make A ready and assert A is displayed; then assert B is prepared and displayed; (b) *invalidation* — prepare A, invalidate, assert one replacement preparation for A and that it displays; (c) *ownership* — reconcile with changed facts, assert one replacement preparation. Keep the original ownership assertions. |

Sequencing (immediate application becomes "after probe completion"):

| Test | Assertions to migrate |
| --- | --- |
| `test_window_keeps_in_flight_history_for_the_same_scope` `:1152`, `:1379` | assertions unchanged, including its liveness checks (`pumps > 1`, `probes > 1`, `ranking_settled > 0`); `_prepared` is supported by the alias, but confirm the loop still observes asynchronous probe application |
| `test_verified_status_round_trip_clears_silent_header_while_window_open` `:285` | immediate SILENT state; keep `:292-295` |
| `test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF` `:343-352` | probe count and immediate state per direct refresh |
| `test_label_recovers_after_silent_refresh_and_connection_reverification` `:383,394-409,425-426` | immediate probe counts and applied facts |
| `test_current_link_status_requires_verification_and_preserves_activation` `:451` | state immediately after refresh |
| `test_the_four_honest_states_render_distinctly` `:633-637` | immediate banner/switch rendering |
| `test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers` `:659-665` | probe and reconciled result after helper completion |
| `test_an_unknown_probe_disables_the_switch_instead_of_claiming_off` `:671-672` | switch state before the probe lands |
| `test_pending_activation_stays_visible_across_a_restart` `:678-679` | activation state after refresh |
| `test_periodic_refresh_never_invokes_the_helper` `:929-931` | three refreshes ⇒ three probes; keep `setup.calls == []` |
| `test_history_is_prepared_before_rendering_and_stale_detail_is_hidden` `:1002-1005` | `_prepared` and loading text immediately after refresh; the hidden-view expectation stays |
| `test_a_refresh_failure_is_reported_instead_of_crashing_the_timer` `:1066` | error text immediately after refresh |
| `test_real_history_router_gates_rendering_until_ready` `:1141` | `_prepared` equals the requested window; split into `_pending` (requested) and `_displayed` (after readiness) |
| `test_the_switch_never_shows_the_click_before_the_probe` `:613-618` | probed switch state, consent visible, no elevation |
| `test_consent_lists_the_exact_commands_before_any_elevation` `:693-701` | exact commands and disclosure |
| `test_cancelled_consent_changes_nothing` `:710-713` | unchanged calls/actions and restored switch |
| `test_a_failed_dismissal_reverts_the_switch_and_shows_the_exit_code` `:731-734` | switch state, retryability, exit code |
| `test_a_dismissal_after_a_previous_success_reverts_to_the_probed_on_state` `:740-745` | probed ON state, dismissal result |
| `test_a_container_host_reason_disables_the_switch_and_is_shown_verbatim` `:757-758` | exact reason, disabled switch |
| `test_a_missing_helper_reports_one_hundred_twenty_seven` `:768-770` | exit 127, message, disabled switch |
| `test_enable_quiesces_local_collection_before_the_first_elevation` `:786-792` | quiesce precedes invocation; plus the spanning-probe case from 7.4 |
| `test_a_failed_quiesce_aborts_the_elevation` `:808-810` | no elevation, error, not busy |
| `test_restart_choice_warns_and_needs_a_distinct_confirmation` `:818-834` | invocation sequence, warning, confirmation |
| `test_reboot_choice_never_runs_restart_or_reboot` `:847-852` | permitted calls only; activation state after the async refresh |
| `test_a_dismissed_choice_leaves_the_switch_on_the_probe` `:862-863` | invocation list, switch state |
| `test_disable_requires_its_own_consent_and_imports_verified_metadata` `:876-892` | disable consent/call, export identity, final OFF switch |
| `test_disable_without_verified_export_metadata_imports_nothing` `:901-903` | no export, retained ON state |
| `test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict` `:916-922` | status consent/call and rendered verdict |
| `test_a_pending_operation_rejects_a_second_one_across_a_qt_heartbeat` `:944-957` | single invocation, busy/disabled state |
| `test_a_reply_completed_on_a_worker_thread_updates_only_on_the_qt_thread` `:989-991` | choice and Qt-thread delivery after the initial async probe |

All use a shared helper that pumps the Qt event loop until a probe completes, so no
test sleeps on wall-clock time.

### 7.6 Gates

Full suite (`python -m pytest`) and the plan's typecheck command must pass before the
audit.

## 8. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| A preparation retired mid-flight stalls the window | retirement is detected from `view_state` and replaced with exactly one fresh preparation (§5.5, tested) |
| A preparation loop re-preparing the displayed window | step 3 requires a changed target or a retired state; tested |
| Reconciliation retiring the generation under a read | keep-pending rule in §5.5 and a test |
| A probe that spans a setup operation applying stale facts | setup generation stamped at submission; tested |
| Files table cleared by a retired file future | transient-scope classification wraps every invalidating call (§5.4, tested) |
| Queries or controls active while a replacement is pending | `set_scope_ready(False)` refuses queries, records one flush, and disables the controls (§5.4-5.5, tested) |
| A stale probe overwriting the fresh one | per-submission ticket plus setup generation, not a single mutable field (§5.6, tested) |
| `QTabWidget` changes focus order and existing tests | every affected assertion is named in §7.5 |
| Late probe callbacks after close | executor shutdown, `_closed`, close-during-probe test |
| Repaint cost at 1 Hz | one `paintEvent` per widget per tick, downsampled, no per-point allocation |
| Charts implying precision the data lacks | breaks only where provable, coverage line always visible, unavailable never zero |
| Qt-thread work that remains | named and tabulated in §5.6; frequency unchanged |

## 9. Mockup disagreements

- `assets/2026-10-21-detail-view.png` shows range chips and an hour-of-day intensity
  strip; both are non-goals in §3.
- That mockup also shows a search field in the ranking list; this change keeps the list
  without one.
- `assets/2026-11-01-chart-sets.png`, option A, captions the omission as "Hour-of-day
  strip is left out because no data source exists for it." That caption is superseded:
  §1.3 establishes that wall-mapped series data exists, and the strip is excluded as a
  scope choice, not for lack of data.
- The target-state mockup says the in-page application dropdown is gone. This design
  keeps it, hidden by default and shown only when an application has retained file
  history but no process-ranking row (§5.4).
- The target-state mockup draws a record gap as a break in every lane. Per §5.7 a
  suspension break is drawn only in the task lane, because `DeviceSeriesPoint` carries
  no `spans_suspend`; device lanes break only where buckets are missing.
- The mockup labels devices `nvme0n1 · / and /data`. Contracts expose only the raw
  boot-scoped `major:minor` token, so lanes are labelled with that token.
- The mockups show one lane per disk; this design caps drawn lanes at three with
  `and <n> more`.
- Everything else — inline bars, the dual-sided TOTAL lane, one lane per disk, the donut,
  the attributed-share panel and the reconciliation strip — is drawn as approved.
