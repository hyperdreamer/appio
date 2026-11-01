# appio console: calm redraw, tabs, drawn charts

Date: 2026-11-01
Status: revised after the first independent design review (11 blockers, D-1 to D-11)
Supersedes nothing. Extends `2026-10-21-app-io-monitor-design.md` §8 for the P3 shell.

Behavioural authority is this document. Layout authority is the three mockups in
`assets/`. Where they disagree, layout follows the mockup and behaviour follows this
text, and the disagreement is listed in §9.

## 1. Background

Three faults were reported against the shipped P3 shell.

### 1.1 The window flickers, and freezes

| Cause | Evidence | Effect |
| --- | --- | --- |
| The ranking list is emptied and rebuilt on every refresh | `AppIoWindow.refresh()` calls `self._list.clear()` then re-adds every row (`src/appio/ui/main_window.py:203-219`) | Rows blink and the scroll position resets. The selected row is *not* lost: the same call recomputes its index and calls `setCurrentRow(target)` (`:219`), so the selection is restored within the tick. The flicker comes from item destruction and re-creation, not from a lost selection |
| The measurement window advances every 10 s and is treated as a new scope | `CollectorWindow._current_window()` computes `end = (now // RAW_BUCKET_NS + 1) * RAW_BUCKET_NS`; `RAW_BUCKET_NS` is 10 s (`src/appio/contracts.py:97`). `_render_history()` then calls `history.prepare(window)`, and while `history.ready(window)` is false it calls `_hide_view()` and shows `loading history (...)` (`src/appio/ui/collector_window.py:683-712`) | The whole detail area disappears and returns every 10 s |
| The refresh tick blocks the drawing thread | `CollectorWindow.refresh()` calls `setup.refresh()` (`:618`) → `production_probe()` → `subprocess.run(timeout=5)` for `systemctl show` (`:1000`) and a daemon socket round-trip with a 2 s deadline (`:1006`), synchronously inside the `QTimer` slot | The interface freezes for the duration of both calls |

### 1.2 Files has no return path

`files_nav` swaps `QStackedWidget` pages (`src/appio/ui/collector_window.py:466`);
the Files page holds `files_view`, `files_message` and `files_open_settings` and no
control that returns to the overview (`:546`). The approved design (§8 of the
2026-10-21 document) never had a Files page: it describes a *detail pane with
`Overview` and `Files` tabs*, so the return path was structural.

### 1.3 The infographics were never built

§8 of the 2026-10-21 design and `assets/2026-10-21-detail-view.png` promise a ranking
list with inline bars and an Overview with a dual-sided `TOTAL` lane, one lane per
disk, a read/write donut, an attributed-share-by-disk panel and a signed
reconciliation strip. The shipped window renders labels and numbers only
(`src/appio/ui/main_window.py:128-160`).

The hour-of-day intensity strip in that mockup is **out of scope by choice, not for
lack of data**: `SeriesPoint` carries `wall_start_seconds` (`src/appio/contracts.py:430`),
populated from the persisted clock map (`src/appio/query.py:657-665`), so an
hour-of-day aggregation is buildable from data that already exists. It is excluded to
keep this change small.

## 2. Goals

1. No visible flicker. The ranking rows, the selection, the scroll position and the
   drawn frame survive every refresh tick.
2. Overview and Files are reachable from each other in one click, and both stay usable
   while the overview history is loading or being replaced.
3. The Overview draws the approved infographics from the series and disk-share data
   the query contract already returns.
4. No new runtime dependency, no new measurement, no daemon, protocol or schema change.

## 3. Non-goals

- The hour-of-day intensity strip (data exists; excluded to bound this change).
- Range chips (`1h`/`6h`/`24h`/`30d`) and the launch window: both stay disabled with
  their existing A22 reasons (`src/appio/ui/formatting.py:76-87`).
- Search in the ranking list.
- Tray behaviour, autostart, per-file BPF scope, retention, cadence settings.
- Any change to `QueryService`, `HistoryRouter`, storage or schema **contracts**. This
  change may call them differently; it does not change their signatures or guarantees.

## 4. Decisions taken with the user

| Decision | Choice | Record |
| --- | --- | --- |
| Chart set | **A**: list bars, dual-sided TOTAL lane, one lane per disk, read/write donut, attributed share by disk, reconciliation strip | `assets/2026-11-01-chart-sets.png`, option 1 |
| Navigation | **A**: `Overview` and `Files` as tabs inside the detail pane, ranking list always on screen | `assets/2026-11-01-navigation.png`, option 1 |
| Target state | Combined view of both choices | `assets/2026-11-01-target-state.png` |

## 5. Architecture

### 5.1 Components

```
src/appio/ui/
├── main_window.py      AppIoWindow: ranking list + tab host, render-model diffing (rewritten)
├── charts.py           SeriesChart, DualSeriesChart, DeviceLane, DonutChart, ShareBars (new)
├── render_model.py     RankingModel, OverviewModel and their builders (new, pure data)
├── theme.py            palette and pen constants shared by the charts (new, small)
├── files_view.py       FilesView: behaviour unchanged, selector visibility driven from outside
└── collector_window.py CollectorWindow: banner, probe worker, displayed/pending window logic
```

`render_model.py` imports only `appio.contracts`, `appio.ui.formatting` and the
standard library. `charts.py` and `theme.py` import PyQt6 (the project's only runtime
dependency) plus `appio.contracts`. None of the new modules imports `HistoryRouter`,
the daemon, BPF or setup modules; the window stays a client of the query contract, the
rule `files_view.py` already follows.

### 5.2 Render model

```python
@dataclass(frozen=True)
class RankingRow:
    app_id: int
    app_key: str
    label: str            # "chromium  383.2 GB"
    total_bytes: int
    share: float          # total_bytes / max_total_bytes in 0..1; 0.0 when the max is 0

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
    dev: str                                  # raw boot-scoped "major:minor" token
    points: tuple[DeviceSeriesPoint, ...]
    read_bytes: int
    write_bytes: int

@dataclass(frozen=True)
class OverviewModel:
    app_id: int
    kpis: tuple[Kpi, ...]
    task_points: tuple[SeriesPoint, ...]      # empty when unavailable
    device_lanes: tuple[DeviceLaneModel, ...] # at most three, ordered by total bytes
    more_devices: int                         # lanes omitted by the three-lane cap
    series_unavailable: str | None
    share: DiskShare | None
    share_unavailable: str | None
    reconciliation: str | None
    coverage_line: str
    detail_line: str
```

Both builders are pure, so every assertion can be made against them without a widget.

### 5.3 Ranking list and redraw discipline

The list keeps its items between ticks. `AppIoWindow._apply_ranking(model)`:

1. Compare the current rows with the model by the tuple
   `(app_id, label, total_bytes, share)` — **not** by label alone, because
   `format_bytes` rounds: a largest total moving from 1 500 to 1 530 bytes leaves every
   label unchanged while every bar's share changes.
2. If the id order is unchanged: update each `QListWidgetItem`'s text and stored share
   in place, and repaint only the rows whose `share` changed. No `clear()`, no model
   reset, no selection write.
3. If the id order changed: rebuild the list, then restore the selection by `app_id`
   and the `QScrollBar` value captured before the rebuild.

Bars are drawn by a `QStyledItemDelegate` that reads the share from
`Qt.ItemDataRole.UserRole + 1`; it never parses the label text. `QListWidget` stays; a
`QAbstractListModel` rewrite is not introduced, because it buys nothing this change
needs and would rewrite tests that are out of scope.

`AppIoWindow.set_overview(model)` applies the same discipline to the KPI labels and
the chart widgets: widgets receive new data and repaint; none is destroyed per tick.

### 5.4 Tabs

The detail pane becomes a `QTabWidget` holding exactly two tabs, `Overview` and
`Files`, in that order, to the right of the ranking list so the list is visible on both.

- The banner loses `files_nav`. `CollectorWindow.open_files_unavailable()` keeps its
  name and signature and selects the Files tab, so no external caller breaks.
- `FilesView` keeps its API and its `app_selector`. The host hides the selector
  **only** when every application with retained file rows also has a process-ranking
  row. When `QueryService.file_applications()` returns an id that has no ranking row —
  the retained-file-only case already covered by
  `tests/test_query.py::test_file_applications_come_from_retained_rows_without_process_ranking`
  and `tests/p4/test_files_view.py::test_file_selector_uses_retained_file_apps_when_process_ranking_is_empty` —
  the selector is shown with the caption `applications with file history only`, so
  those applications stay reachable. Selecting one leaves the ranking selection
  untouched and puts the Overview KPIs into the `not in the process ranking` state.
- Switching tabs never clears the selection, re-prepares the window, or re-queries.

### 5.5 Two window identities: displayed and pending

`CollectorWindow` keeps two fields:

- `_displayed`: the window whose render model is on screen (what used to be `_prepared`),
- `_pending`: the window currently being prepared, or `None`.

Rules:

| Situation | Behaviour |
| --- | --- |
| `_current_window()` differs from `_pending` | call `history.prepare(new_window)` and set `_pending` — **regardless of whether a frame is displayed**. A displayed frame never suppresses preparing its replacement |
| History invalidated with no window change (`coordinator.reconcile()` at `src/appio/collector_owner.py:196`, local sampling at `:208`, `HistoryRouter.invalidate()` at `src/appio/history_router.py:413`) | call `history.prepare(_displayed)` again and set `_pending = _displayed` |
| `history.ready(_pending)` | build the render model, apply it, set `_displayed = _pending`, `_pending = None` |
| No frame yet and `_pending` not ready | the Overview tab shows `loading history (<view_state>)` **inside its own tab**; the tab bar itself is never hidden |
| A frame exists and `_pending` is not ready | the frame stays; a single label above the tabs reads `updating (<view_state>)`; nothing is blanked |

`_hide_view()` is therefore removed from the refresh path; only the Overview tab
content swaps between "loading text" and "tab body".

**Files while a replacement is pending.** `prepare()` replaces `_window` and clears
readiness, so a `FilesView` bound to the old window can no longer issue queries
(`HistoryRouter.request_file_view()` refuses an unready window,
`src/appio/history_router.py:322-906`). While `_pending is not None and _pending != _displayed`:

- the Files tab keeps its last rendered table and footer on screen;
- one footer line reads `updating to <scope>: controls resume when the new window is ready`;
- the controls that would issue a query (sort, search, selector, refresh) are disabled;
- they re-enable when `_pending` becomes `_displayed`.

This is honest and bounded: the replacement window is milliseconds of work, and a
disabled control cannot silently clear the table through `_set_unavailable()`.

**Files before the first frame.** The Files tab is reachable and keeps today's exact
page-level text: `waiting for the first sample` while no window exists, and the
degraded-source message `file activity is unavailable: <reason>: <diagnostic>` when
`history.file_source_status()` is unavailable (`src/appio/ui/collector_window.py:559-575`).
Those strings do not change.

### 5.6 Probes off the drawing thread, and what stays on it

The refresh tick becomes:

1. `self._pump()` — unchanged, first, every tick. It drives the daemon client's reads,
   writes, deadlines and reconnects (`IodClient.pump()`, `src/appio/iodclient.py:793`).
2. Poll `_pending` readiness and dispatch queued history work (`history.ready`) — every
   tick, **independent of the probe**. The old `_poll_history` early-return when the
   view is visible is deleted; a pending replacement must progress while a frame is
   displayed.
3. If no probe is in flight, submit `setup.refresh()` to a single-slot
   `ThreadPoolExecutor`; otherwise skip the submission only. The tick never returns
   early because of a probe.
4. The completed `ServiceFacts` arrives on the Qt thread through a queued signal and is
   applied by `_apply_facts(facts)`.

**What this guarantees.** No `subprocess`, no blocking socket connect/recv, and no
`time.sleep` runs on the Qt thread from a refresh tick. That is the whole claim.

**What it does not guarantee.** These calls still run on the Qt thread and are
explicitly out of scope for this change:

| Call | What it does | Why it stays |
| --- | --- | --- |
| `coordinator.tick_local()` (`src/appio/collector_owner.py:201`) | reads process and cgroup files, aggregates, commits a sampler frame (`src/appio/live.py:534-588`) | local collection is the window's own work and already bounded per tick |
| `_current_window()` (`src/appio/ui/collector_window.py:690-703`) | one `SELECT boot_id FROM boot` on the local connection | one indexed row |
| `history.prepare()` (`src/appio/history_router.py:742`) | validates the window, retires file requests, resets router state, one `PRAGMA database_list` per source, submits local work, may dispatch a remote request | once per window change (10 s), not per tick |
| `history.ready()` (`src/appio/history_router.py:841`) | consumes completed futures, remaps ranking rows, queues detail requests, dispatches them | bounded by the number of applications |
| non-blocking socket sends in `IodClient` (`src/appio/iodclient.py:813-1307`) | `sock.send()` on a non-blocking socket | does not block; the blocking probe round-trip is what moves off-thread |

**Arbitration with setup operations.** `_busy` keeps its current meaning: a helper
operation is in flight. `_apply_facts()` must:

- if `_busy` is true, store the facts as `_deferred_facts` and change nothing — no
  `coordinator.reconcile()`, no `tick_local()`, no history rendering. This preserves
  the quiesce contract (`src/appio/ui/collector_window.py:814`,
  `src/appio/collector_owner.py:175`), where an OFF probe completing during an enable
  must not reacquire the local writer;
- if `_busy` is false, apply `_deferred_facts` first when present, then the current
  facts;
- keep the existing promotion of a verified, answering history connection over a
  silent one-shot probe (`src/appio/ui/collector_window.py:619-623`) inside
  `_apply_facts`, before `coordinator.reconcile()`.

`window.shutdown()` stops both timers, requests executor shutdown, and every
application path checks `_closed`, so a probe completing after close mutates nothing.

### 5.7 Charts

`charts.py` holds one primitive and three widgets. Each exposes its paint state as
data, so an offscreen test can assert behaviour without reading pixels.

```python
class SeriesChart(QWidget):
    def set_task_points(self, points: Sequence[SeriesPoint]) -> None: ...
    def set_unavailable(self, reason: str) -> None: ...
    def segments(self) -> tuple[ChartSegment, ...]   # (side, run index, polygon points)
    def is_available(self) -> bool: ...
```

- `DualSeriesChart` draws read above a centre line and write below it, each side scaled
  to its own maximum inside the lane, with a right-hand gutter label
  (`r 41.0 · w 342.2`).
- `DeviceLane` is the same widget fed with `DeviceSeriesPoint` tuples; the gutter reads
  `r <rbytes> · w <wbytes>`.
- `DonutChart` draws read vs write for the selected application with both percentages.
- `ShareBars` draws one row per device, bar width relative to the largest device, with
  `Σ disks <x> + residual <y> = <total>` beneath.

**Breaks are limited to what the data can prove.** `Coverage` carries *counts*
(`gap_intervals`, `spans_suspend_intervals`, `overlaps`), not interval locations
(`src/appio/contracts.py:317-334`), and `Series` carries no gap ranges
(`:497-517`). Therefore:

| Lane | Break rule |
| --- | --- |
| `TOTAL` (task) lane | a break between consecutive `SeriesPoint`s when
`next.bucket_start_mono_ns != current.bucket_start_mono_ns + current.bucket_secs * 10**9`, and after any point whose `spans_suspend` is true |
| device lane | a break on the same bucket-adjacency rule only. `DeviceSeriesPoint` has no `spans_suspend` field (`src/appio/contracts.py:460-477`), so a suspension is **not** drawn as a break in a device lane |

A recorded gap that still produced a populated bucket is not locatable from these
contracts, so it is reported in the coverage line (`gaps: <n>`) instead of being drawn.
The design never claims otherwise, and §9 corrects the mockup.

**Downsampling.** When points exceed the lane's pixel width, buckets are aggregated per
pixel column: bytes are summed, peaks take the maximum, and a column containing a break
starts a new run. No point is dropped silently and no gap is bridged.

**Empty, unavailable, and no-zero.** An empty series draws the lane caption and no
shape. An unavailable series draws `unavailable: <reason>` centred in the lane.
Neither draws a zero line.

**Device lanes.** Ordered by attributable bytes descending, capped at three, with
`and <n> more` in the caption when more devices have activity. A device's label is its
raw boot-scoped `major:minor` token (`DeviceSeriesPoint.dev`, `DeviceShareRow.dev`);
no name or mount point exists in the contracts, so none is invented.

### 5.8 Data flow

```
QTimer tick (1 s)
  → _pump()                                   # always first
  → poll: _pending ready? apply model; _displayed = _pending
  → probe in flight? skip submission only
  → executor.submit(setup.refresh) → queued signal → _apply_facts(facts)
  → _apply_facts: if _busy: store and return
                  else: promote verified connection → reconcile → tick_local
                        → banner + switch → _current_window()
                        → if window != _pending: history.prepare(window); _pending = window
```

### 5.9 Window and state fields

| Field | Meaning | Lifecycle |
| --- | --- | --- |
| `_displayed: Window \| None` | the window whose model is on screen | set when a render model is applied |
| `_pending: Window \| None` | the window being prepared | set when `prepare()` is called; cleared on apply |
| `_facts: ServiceFacts \| None` | last applied facts | unchanged |
| `_deferred_facts: ServiceFacts \| None` | facts that arrived while `_busy` | applied once `_busy` clears |
| `_probe_inflight: bool` | one probe at a time | set before submit, cleared on delivery |

## 6. Error handling

| Condition | Behaviour |
| --- | --- |
| `ranking()` raises `WindowUnavailableError` | list shows the reason; KPIs print `unavailable`; lanes, donut and share bars show `unavailable: <reason>` |
| `disk_share()` raises | dependent KPIs print `unavailable`; share panel and donut show the reason; the task lane still draws |
| `series()` raises or returns no points | lanes show `unavailable: <reason>` or `no samples in this window`; no shape drawn |
| Device attribution unavailable (`coverage.device_state != "available"`) | the donut still shows task read/write; DEVICE KPI and residual print `unavailable`; the strip reads `device attribution unavailable: <reason>` |
| Incomplete coverage or recorded gaps | the coverage line states `gaps: <n>`, `suspends: <bool>`, `complete/incomplete`; lanes break only where §5.7 proves a hole |
| Empty ranking | list shows `no applications observed`; Overview shows `unavailable` KPIs and no charts |
| Pending scope not ready | last frame stays; `updating (...)` label; Files controls disabled per §5.5 |
| Files source unavailable | existing page-level message unchanged |
| Probe raises | existing `_show_error` path; last banner and frame stay |
| Close while a probe runs | timers stop, executor shuts down, `_closed` guards every apply path |

No value is replaced by zero. `formatting.UNAVAILABLE` stays the only spelling.

## 7. Test strategy

All tests are offscreen (`pytest-qt`, PyQt6 pinned as `tests/test_ui.py` and
`tests/p3/test_collector_window.py` already do).

### 7.1 Pure model tests (`tests/test_ui.py`, new cases)

- `build_ranking` orders by `(-total_bytes, app_key)`, computes `share` against the
  largest row, and yields `empty_text` for an empty sequence.
- `build_overview` produces five KPIs in fixed order, `unavailable` for a `None` peak
  and an unavailable device total, at most three device lanes ordered by bytes with the
  correct `more_devices`, and a reconciliation string only when device attribution is
  available.

### 7.2 Chart tests (`tests/test_charts.py`, new file)

Geometry:

- contiguous points produce exactly one run per side;
- a one-bucket hole produces two runs and no polygon point inside the hole;
- a `spans_suspend` point splits the task run;
- `set_unavailable` leaves `segments()` empty and `is_available()` false;
- downsampling over `2 × width` points keeps at most `width` columns, sums bytes, keeps
  the maximum peak, and preserves breaks;
- `DonutChart` reports its two shares and `ShareBars` its per-device fractions.

Painting (the only pixel-level check, kept minimal): each widget renders a non-empty
`QPixmap` through `grab()` and a chart with `set_unavailable` renders its reason text;
assertions on colour are avoided.

### 7.3 Window tests (`tests/test_ui.py`)

- identical data does not replace list items (item identity preserved) and does not
  change the current row;
- **changed shares with identical labels** repaint the affected rows and rebuild
  nothing (this is the case `format_bytes` rounding hides);
- a changed order keeps the selected `app_id` and the scroll value;
- the tab widget holds exactly `Overview` then `Files`;
- switching tabs twice preserves the selection and does not call `set_window` again;
- the Files selector is hidden when every file application is ranked, and shown when a
  retained-file-only application exists; selecting one leaves the ranking selection
  untouched;
- an unavailable series leaves the lane marked unavailable and no KPI reading `0`.

### 7.4 Shell tests (`tests/p3/test_collector_window.py`)

New:

- a slow probe does not block the tick: with a probe sleeping 300 ms, a tick returns
  promptly, only one submission is in flight, **and** the client is still pumped and a
  pending history window still becomes ready during the probe;
- a window advance replaces the frame: the old frame stays visible, `loading history`
  never appears once a frame exists, and the *new* window is eventually displayed (a
  stale frame must fail the test);
- close during a probe applies nothing and raises nothing;
- Files during preparation keeps its last table, shows the `updating to ...` line, and
  has its query controls disabled until the new window is applied;
- `open_files_unavailable()` selects the Files tab, and the banner has no `files_nav`.

### 7.5 Existing tests that must change

These assertions contradict the new structure or the new asynchronous tick. Each line
below is a deliberate migration, not a deletion: the behavioural assertion is kept and
the observation point moves to after probe completion.

Structural (tabs replace the stacked pages):

| Test | Assertions to migrate |
| --- | --- |
| `tests/test_ui.py::test_no_files_tab_tray_daemon_or_privileged_action` `:444` | forbids any `QTabWidget`; replaced by "no tray, no daemon control, and exactly two tabs" |
| `test_the_switch_is_the_only_root_control_and_sits_inside_settings` `:599` | the blanket `QTabWidget` prohibition; keep the root-control assertions at `:592` |
| `test_banner_and_files_deep_links_focus_settings_without_changing_state` `:482-489` | Files page/stack identity; keep the deep-link and no-state-change assertions, keep the pre-first-frame Files message |
| `test_opening_files_shows_the_real_table_and_changes_only_query_state` `:505-515` | `files_nav` click, stack page identity, and the extra query on open — a tab switch must not re-query |
| `test_a_degraded_file_source_is_reported_while_history_is_still_loading` `:532-537` | stack page identity; keep the degraded message and hidden table |
| `test_files_page_keeps_an_explicit_unavailable_message_and_settings_link` `:553-564` | stack page identity; keep the unavailable text, no-query, diagnostic and settings link |
| `test_the_files_surface_never_appears_inside_the_overview_window` `:572-578` | "Files is not a descendant of the overview window" — now Files *is* inside the window by design; replace with "Files is not inside the Overview **tab**" |

Sequencing (immediate application after `refresh()` becomes "after probe completion"):

| Test | Assertions to migrate |
| --- | --- |
| `test_verified_status_round_trip_clears_silent_header_while_window_open` `:285` | immediate SILENT state; keep `:292-295` |
| `test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF` `:343-352` | probe count and immediate state per direct refresh |
| `test_label_recovers_after_silent_refresh_and_connection_reverification` `:383,394-409,425-426` | immediate probe counts and applied facts |
| `test_current_link_status_requires_verification_and_preserves_activation` `:451` | state immediately after refresh |
| `test_the_four_honest_states_render_distinctly` `:633-637` | immediate banner/switch rendering |
| `test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers` `:659-665` | new probe and reconciled result immediately after helper completion |
| `test_an_unknown_probe_disables_the_switch_instead_of_claiming_off` `:671-672` | switch state before the probe lands |
| `test_pending_activation_stays_visible_across_a_restart` `:678-679` | activation state after refresh |
| `test_periodic_refresh_never_invokes_the_helper` `:929-931` | three refreshes ⇒ three probes; keep `setup.calls == []` and assert three completed probes |
| `test_history_is_prepared_before_rendering_and_stale_detail_is_hidden` `:1002-1005` | `_prepared` and loading text immediately after refresh; the hidden-view expectation stays |
| `test_pending_history_yields_to_new_scope_ownership_or_invalidation` `:1035-1046` | two preparations/ticks and ownership state from direct calls |
| `test_a_refresh_failure_is_reported_instead_of_crashing_the_timer` `:1066` | error text immediately after refresh |
| `test_real_history_router_gates_rendering_until_ready` `:1141` | `_prepared` immediately equals the requested window; split into `_pending` (requested) and `_displayed` (after readiness) |
| `test_the_switch_never_shows_the_click_before_the_probe` `:613-618` | probed switch state, consent visible, no elevation |
| `test_consent_lists_the_exact_commands_before_any_elevation` `:693-701` | exact commands and disclosure text |
| `test_cancelled_consent_changes_nothing` `:710-713` | unchanged calls/actions and restored switch |
| `test_a_failed_dismissal_reverts_the_switch_and_shows_the_exit_code` `:731-734` | switch state, retryability, exit code |
| `test_a_dismissal_after_a_previous_success_reverts_to_the_probed_on_state` `:740-745` | probed ON state, dismissal result |
| `test_a_container_host_reason_disables_the_switch_and_is_shown_verbatim` `:757-758` | exact reason and disabled switch |
| `test_a_missing_helper_reports_one_hundred_twenty_seven` `:768-770` | exit 127, message, disabled switch |
| `test_enable_quiesces_local_collection_before_the_first_elevation` `:786-792` | quiesce precedes invocation; **plus a new case**: an OFF probe completing during elevation is deferred and does not reacquire the writer |
| `test_a_failed_quiesce_aborts_the_elevation` `:808-810` | no elevation, error, not busy |
| `test_restart_choice_warns_and_needs_a_distinct_confirmation` `:818-834` | invocation sequence, warning, confirmation |
| `test_reboot_choice_never_runs_restart_or_reboot` `:847-852` | permitted calls only; activation state after the async refresh |
| `test_a_dismissed_choice_leaves_the_switch_on_the_probe` `:862-863` | invocation list, switch state |
| `test_disable_requires_its_own_consent_and_imports_verified_metadata` `:876-892` | disable consent/call, export identity, final OFF switch |
| `test_disable_without_verified_export_metadata_imports_nothing` `:901-903` | no export, retained ON state |
| `test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict` `:916-922` | status consent/call and rendered verdict |
| `test_a_pending_operation_rejects_a_second_one_across_a_qt_heartbeat` `:944-957` | single invocation, busy/disabled state |
| `test_a_reply_completed_on_a_worker_thread_updates_only_on_the_qt_thread` `:989-991` | choice and Qt-thread delivery after the initial async probe |

All of these use a shared helper that pumps the Qt event loop until a probe completes,
so the tests stay deterministic without sleeping on wall-clock time.

### 7.6 Gates

The full suite (`python -m pytest`) and the plan's typecheck command must pass before
the audit.

## 8. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| `QTabWidget` inside the detail pane changes focus order and the existing `files_nav` tests | §7.5 names every affected assertion; they are migrated, never deleted |
| Threading a probe into a Qt window invites late callbacks after close | executor shutdown in `shutdown()`, `_closed` guards, close-during-probe test |
| Asynchronous facts crossing a setup operation | `_busy` defers facts (§5.6) and a dedicated test pins the quiesce case |
| A stale frame passing the "no loading text" criterion | the window-advance test asserts the new window is displayed, not only that nothing flickered |
| Repaint cost at 1 Hz | one `paintEvent` per widget per tick, downsampled to pixel width, no per-point allocation in `paintEvent` |
| Charts implying precision the data lacks | breaks only where buckets are provably missing, coverage line always visible, unavailable never becomes zero |
| Qt-thread work that remains (§5.6 table) | named, bounded, and unchanged in frequency from today; this change does not add to it |

## 9. Mockup disagreements

- `assets/2026-10-21-detail-view.png` shows range chips and an hour-of-day intensity
  strip. This design keeps the window at `since boot` and drops the strip; both are
  non-goals in §3.
- The 2026-10-21 mockup shows a search field in the ranking list. This change keeps the
  list without one.
- The target-state mockup draws the record gap as a break in **every** lane. Per §5.7 a
  suspension break is drawn only in the task lane, because `DeviceSeriesPoint` carries
  no `spans_suspend`; device lanes break only where buckets are missing.
- The target-state mockup labels devices `nvme0n1 · / and /data`. Contracts expose only
  the raw boot-scoped `major:minor` token, so lanes are labelled with that token.
- The mockups show one lane per disk; this design caps the drawn lanes at three with
  `and <n> more`.
- Everything else — inline bars, the dual-sided TOTAL lane, one lane per disk, the
  donut, the attributed-share panel and the reconciliation strip — is drawn as approved.
