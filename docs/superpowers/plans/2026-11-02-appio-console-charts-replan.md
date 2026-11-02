# Appio Console Charts — Parent-Dispatched Replan, Epoch 2

**Date:** 2026-11-02. **Goal:** Keep the console responsive and its last measured frame visible while history is replaced, place Overview and Files in one persistent detail pane, and draw the approved charts from existing query contracts.

**Execution:** The PM parent dispatches one implementer session and one reviewer session for each of the five tasks below, in dependency order. The implementer completes the task's coherent file slice and runs its listed validation commands; the reviewer checks that slice against its cited specification and validation evidence. The PM handles review feedback with that task's implementer before dispatching the next task. Do not launch an SDD lane controller or depend on a child session dispatching grandchildren. The previous run dispatched zero children; this replan changes execution granularity, not feature scope.

**Architecture:** Pure frozen render models separate query outcomes from Qt updates. Cached QPainter geometry and equality checks update persistent widgets. One shared subject and one Files query owner connect the two tabs. The shell separates target, pending, and displayed scopes and accepts single-worker service probes only after ticket and setup-generation checks on Qt.

**Provenance:** `run_id=pm-20261101-175829-4410c030`; `lane=none`; `attempt=1`; `dispatch_key=pm:pm-20261101-175829-4410c030:role-planner:lane-none:task-none:epoch-2:attempt-1`; `observed_boot_id=c753ab5f-87a1-4285-9c4e-d749ce9ac758`. Planning baseline: `0eb173eb28dcba81df33e9261dbaa25d778422c0`.

Read authorities, retained unchanged:

- Approved design: `docs/superpowers/specs/2026-11-01-appio-console-charts-design.md`; approval commit `f444c862e607e388271ffb8a17e059c7b7dd2d91`, blob `d4cb671b28ae4baf565864ef02d341c28bfdea8d`, SHA-256 `acd2e9f81096b24bcc3a7d05eb3130945bd8da837fe9c38835ca29516512ef6d`.
- Signed-off specification: `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md`; SHA-256 `cf7c3140cab5734cc4badc9d643a9eee7dd3341f03a68b3762be8f90a054aae2`. Every reference to “spec” below means this file. Its signatures, algorithms, strings and test inventories are normative.
- Historical 13-task plan: `docs/superpowers/plans/2026-11-01-appio-console-charts.md`; SHA-256 `764d0bcfde3b75067a45dae6e10d8f1dad420b29b7a18028a06ef72406ec3d8d`. It is source material and history, not an execution dependency or skill instruction for this replan.

## Global Constraints

- This planning assignment creates only `docs/superpowers/plans/2026-11-02-appio-console-charts-replan.md`. Do not edit design, specification, historical plan, source, tests or configuration; do not commit or run the suite during planning. The task instructions and commands below are prospective implementation work.
- All implementation work occurs in `/data/home/henry-arch/.local/state/pi/project-manager/worktrees/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-20261101-175829-4410c030/integration`. Each task has an exclusive write allowlist; every unlisted file is read-only. Sequential ownership of `tests/test_ui.py` and `tests/p3/test_collector_window.py` is deliberate. Return an upstream defect to its owning task through the PM instead of widening an allowlist.
- Use Python >=3.14, existing PyQt6 >=6.6, dataclasses, concurrent.futures and QPainter. Keep `pyproject.toml`, versions, runtime dependencies, entry points, producer/protocol/storage/schema/registry/helper-command contracts and concrete QueryService/HistoryRouter signatures unchanged. No new measurement, daemon revision or migration is authorised (spec §§1, 10).
- The layout references are `docs/superpowers/specs/assets/2026-11-01-target-state.png`, `docs/superpowers/specs/assets/2026-11-01-navigation.png`, and option A in `docs/superpowers/specs/assets/2026-11-01-chart-sets.png`. Apply design §9 corrections: conditional Files selector, raw device tokens, three chronological device lanes maximum, and only provable time breaks. Preserve unrelated table/dialog styling.
- Keep main-window and Files imports producer-free. Render models import only standard library, contracts and existing formatting; charts only standard library, PyQt6, contracts and theme; theme only standard library, PyQt6 and contracts. Add `series` only to the UI-local protocol. Structural query fakes need no database, daemon or concrete router.
- Preserve exact spec §7 text, A22 refusals, supported explicit ranges, negative residuals, measured zero versus unavailable, Files served/requested interval disclosure and every quantity/footer line. Exclude hour-of-day strip, enabled range chips/launch windows, ranking search, tray/autostart, per-file BPF scope, retention and cadence settings.
- One persistent tab host, one FilesView, one subject and one Files query owner are mandatory. Tabs never prepare/query/reconcile/elevate. Never block Qt on probes, subprocesses, socket connect/recv or sleep. Keep existing local sampling, boot lookup, preparation bookkeeping and nonblocking client pumping on Qt at their existing frequencies.
- Future widget validation uses `/tmp/appio-console-charts-pm-20261101-175829-4410c030-venv`, `QT_QPA_PLATFORM=offscreen`, and `PYTEST_QT_API=pyqt6`. Task 1 provisions the absent environment outside the checkout. Later sessions activate that same environment and record actual versions. Test workers use controlled Events/Futures and Qt event pumping, not arbitrary GUI-thread sleeps.
- Every task supplies a future commit message and ends with its validation commands. The PM owns dispatch, review and integration. A task is not complete on an expected failure, skipped new assertion, import/collection error or invented successful output. Task 4 deliberately validates the 28 new shell tests while Task 5 owns migration of the old shell bodies; Task 5 must pass the entire module and suite.
- Never execute reboot, shutdown, poweroff, halt, kexec, systemctl power/sleep verbs, `init 0|6`, or `telinit 0|6`; never stop/restart the display manager or graphical.target, terminate the session, or signal the PM process tree. Do not invoke actual privileged setup/host service operations for these tests. No host transition is required. If one unexpectedly becomes necessary, write `reboot-required.json` describing the required transition and reason, return it to the PM, and stop rather than performing the transition.

## Task 1: Pure render models, palette and chart widgets

**Implementer tier:** Capable

**Lane/category:** rendering / pure models and reusable Qt primitives.

**Depends on:** none.

**Exclusive write allowlist:** Create `src/appio/ui/render_model.py`, `src/appio/ui/theme.py`, `src/appio/ui/charts.py`, and `tests/test_charts.py`; modify `tests/test_ui.py` only its imports, `FakeQueryService`, compatible `build` fixture support, and appended builder/dataclass tests. Do not migrate existing window test bodies in this task.

**Read-only inputs:** Spec §§1–3.2, 6–8, 9.1–9.3, 9.7, 10; design §§2–3, 5–6, 7.1–7.2, 9 and the three asset paths above; `src/appio/contracts.py`, `src/appio/ui/formatting.py`, `src/appio/ui/main_window.py`, `pyproject.toml`.

**Consumes:** Existing `Window`, `Coverage`, `AppTotal`, `Series`, `SeriesPoint`, `DeviceSeriesPoint`, `DiskShare`; `format_bytes`, `format_peak`, `format_residual`, `UNAVAILABLE`; the existing query-only fake and test helpers. Contracts and formatters remain unchanged.

**Produces:** The seven exact frozen dataclasses of spec §2.1: `RankingRow`, `RankingModel`, `Kpi`, `DeviceLaneModel`, `OverviewModel` in render_model and `ChartColumn`, `ChartSegment` in charts. Preserve their exact field lists, tuple storage and `__post_init__(self) -> None` rejection rules. Builders are `build_ranking(rows: Sequence[AppTotal], *, unavailable: str | None = None) -> RankingModel`, `build_overview(app_id: int, window: Window, total: AppTotal | None, series: Series | None, share: DiskShare | None, *, ranking_error: str | None = None, series_error: str | None = None, share_error: str | None = None) -> OverviewModel`, and `scope_text(window: Window) -> str` returning `window.kind`. Produce the exact theme constants and all five widget surfaces in spec §§3.1–3.2: `SeriesChart`, `DualSeriesChart`, `DeviceLane`, `DonutChart`, `ShareBars`, including `columns`, `segments`, `plot_rect`, availability/reason getters, totals/device/reconciliation setters and actual child `chart_message` labels. Extend `FakeQueryService` compatibly with optional series results, independent ranking/share/series errors and call logs; default series is valid empty data for that app/window, not a silently rewritten configured result.

**Tests to make pass:** All of `tests/test_charts.py` and `tests/test_ui.py` at this stage, including the seven exact spec §9.2 names and eleven exact §9.3 names listed below; existing UI regressions keep their current observations.

**Commit message:** `feat(ui): add render models and cached console chart widgets`

### Implementation slice

- [ ] Provision the isolated tools once, outside the checkout, with Python >=3.14. If the venv is absent, run `python -m venv --system-site-packages /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv`, activate it, run `python -m ensurepip --upgrade`, then `python -m pip install 'mypy==2.4.1' 'pytest-qt==4.5.0' 'pytest==9.1.1'`. Inherit installed PyQt6; do not alter project dependencies. Record resolved Python/PyQt6/pytest/pytest-qt/mypy versions for the final receipt.
- [ ] Write assertion-bearing tests, then implement all model/builders and widgets as this single rendering slice. Invalid values raise ValueError, including bool counts, nonfinite values, mutable collections, malformed nested contract points, duplicate IDs/keys/devices, wrong five-KPI order, conflicting availability, invalid endpoints and open polygons. Models store no live Qt/history/future objects. Labels use two spaces and existing formatters; shares divide by largest ranking total, with all-zero shares 0.0. Validate exact subject/window matches and object-plus-error conflicts (spec §§2.1–2.2).
- [ ] Keep five KPI sources independent: share supplies TOTAL/READ/WRITE/DEVICE, ranking supplies PEAK. A share error invalidates its four byte KPIs and both composition widgets but leaves known PEAK and successful task series; a series error preserves measured KPIs. Ranking error takes precedence over file-only state. File-only subjects get five unavailable KPIs and `not in the process ranking`. Empty available series has no shapes and `no samples in this window`. Successful coverage comes from share, then series, then ranking; describe differing successful coverage independently in detail text without combining counters. Preserve signed reconciliation exactly (spec §§2.2, 7–8).
- [ ] Select positive device-share rows, order by descending share total then UTF-8 raw token, keep three lanes and count remaining active devices. Empty retained points retain the lane caption/exact share gutters. Require independently available series/share device coverage and defined device total for lanes; unavailable lanes have no more count. Lifetime chronology is explicitly unavailable because points have no boot identity; valid totals/donut and boot-qualified share bars remain. An undefined residual alone suppresses reconciliation, not independent device totals (spec §§2.2, 6.4).
- [ ] Install literal palette `#397fd1`, `#e47e38`, `#25303d`, `#63758a`, `#d5deea`, `#e6ecf3`, `#cbd7e5` for READ/WRITE/TEXT/MUTED/GRID/TRACK/BORDER, and `AXIS_PEN_WIDTH=1.0`, `BAR_HEIGHT=6`, `PLOT_MARGIN=8`, `GUTTER_WIDTH=144`. Do not add a global stylesheet (spec §3.1).
- [ ] Validate/copy setter data to tuples. Cache raw source runs once: task boundaries occur at the first point, nonadjacency or immediately after a spans_suspend point; the suspended point keeps its bytes/peaks. Device runs use only first point/nonadjacency. Coverage counts never locate holes. At zero plot width or height retain inputs but expose no columns/segments (spec §§6.1–6.2).
- [ ] For positive area, set `W=max(0,floor(right-left))` excluding caption/gutter/margins. With at most W points emit one column per point. Otherwise bin each point exactly once by `min(W-1, ((start-first_start)*W)//(last_end-first_start))`; emit nonempty bins, exact summed bytes, min/max endpoints, maximum defined peaks (all-None remains None), and starts_run if any contributor starts a run. Preserve private per-run membership/observed intervals alongside columns; a bin envelope or starts_run flag does not prove continuity. Keep O(points+W) rebuilding on data/size changes (spec §6.2).
- [ ] Compute lane rates from byte sums divided by summed observed durations, not missing elapsed time; use exact integer/rational arithmetic until pixel mapping. Map full time extents to plot bounds, read above and write below centre, independently normalised against each side's maximum. Build one closed positive-amplitude step-fill polygon per raw run/side, clipped to that run's observed support even when several runs share a subpixel column. Inset outer run endpoints by `min(0.25, run_pixel_span/4)` so entire X support lies strictly outside mapped holes. Preserve zero-side conventions and pen half-pixel bounds. `segments()` must expose the exact polygons paint uses. Paint cached geometry only; no I/O, source scans, per-source allocations or widget recreation in paintEvent (spec §6.3).
- [ ] Donut denominator is task read+write; retain exact byte labels and one-decimal percentages. Measured (0,0) gives (0.0,0.0), no sectors and `no task I/O in this window`; unavailable gives None and exact reason. ShareBars preserve every supplied row/order and divide by largest device total, with all-zero widths 0.0. Keep lifetime boot-qualified tokens, exact captions/gutters and Σ annotation; the time-lane cap never truncates share bars. Unavailable setters discard old shapes and numeric labels and expose visible reasons. Resize rebuilds geometry from immutable inputs (spec §§3.2, 6.4, 7).

### Exact new test inventory and review observations

Append each pure test exactly once to `tests/test_ui.py`:

- `test_build_ranking_orders_by_total_then_key_and_normalises_share`
- `test_build_ranking_distinguishes_empty_and_unavailable`
- `test_build_overview_has_five_ordered_kpis_and_none_peak_is_unavailable`
- `test_build_overview_limits_device_lanes_and_counts_more`
- `test_build_overview_reconciles_only_available_device_attribution`
- `test_render_dataclasses_reject_invalid_values_and_availability_conflicts`
- `test_lifetime_series_does_not_join_unrelated_boot_clocks`

Implement each chart test exactly once in `tests/test_charts.py`:

- `test_contiguous_points_produce_one_run_per_nonzero_side`
- `test_one_bucket_hole_splits_runs_without_points_or_edges_in_hole`
- `test_task_suspend_splits_after_point_but_device_does_not_inherit_it`
- `test_set_unavailable_clears_shapes_and_exposes_exact_reason`
- `test_downsampling_preserves_bytes_peak_maxima_and_run_starts`
- `test_downsampling_across_gap_never_spans_its_horizontal_interval`
- `test_donut_shares_and_device_bar_fractions_use_their_defined_denominators`
- `test_each_chart_grabs_nonempty_pixmap_and_renders_reason_text`
- `test_chart_resize_rebuilds_observations_and_keeps_geometry_in_plot`
- `test_empty_available_series_draws_no_zero_line`
- `test_coverage_counts_do_not_invent_chart_breaks`

Use spec §9.1 `chart_points`: at least four nonzero task buckets, optional true hole/suspend, defined/None peaks, matching device points with no suspend field. For downsampling use more than 2W points and force both sides of a hole into one bin; check complete polygon min/max X support, exact byte conservation, peak maxima, run starts, half-plane/scaling and resize bounds. Verify coverage-count-only variants stay continuous. The seven-dataclass matrix covers all rejection rules. Show/resize/grab all five widgets; inspect visible labels, shapes and reason labels, saving transient grabs outside the checkout.

### Validation commands

```bash
cd /data/home/henry-arch/.local/state/pi/project-manager/worktrees/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-20261101-175829-4410c030/integration
. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
python -c 'import sys, importlib.metadata as m; print(sys.version); print({p: m.version(p) for p in ("PyQt6", "pytest", "pytest-qt", "mypy")})'
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_ui.py tests/test_charts.py
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py
```

## Task 2: Files admission, retirement and the ranked/file-only union

**Implementer tier:** Advanced

**Lane/category:** Files / admission, asynchronous result ownership and subject membership.

**Depends on:** 1.

**Exclusive write allowlist:** `src/appio/ui/files_view.py` and `tests/p4/test_files_view.py`. Preserve table columns, FileTableModel formatting, search/sort vocabulary, request-log bound, footer quantities and import-layer guard. Confine production changes to FilesView state, control wiring, admission/choice/request/delivery methods and the added helpers.

**Read-only inputs:** Spec §§1.1, 3.4, 5.1–5.5, 7–8, 9.1, 10; design §§4, 6–7, 9; `src/appio/contracts.py`, `src/appio/ui/formatting.py`, `src/appio/history_router.py`, `src/appio/query.py`, `src/appio/iodclient.py` (which defines AppRegistry).

**Consumes:** Structural history `ready(window)`, `file_source_status()`, `ranking(window, "total_bytes")`, `request_file_view(app_id, window, sort, direction, search) -> Future[FileViewResult]`, optional `history.local.file_applications(window)` and `history.registry.id_for(key)`. Existing FileViewResult, sort/direction/query-key semantics and source diagnostics remain unchanged. Keep FileViewResult under TYPE_CHECKING; import no concrete producer/router at runtime.

**Produces:** `set_transient_scope(transient: bool) -> None`; `transient_scope(retire_on_exit: bool = False) -> ContextManager[None]` implemented with @contextmanager and generator `Iterator[None]`; `set_scope_ready(ready: bool) -> None`; `set_updating_scope(window: Window | None) -> None`; `retire_deliveries() -> None`; `refresh_wanted() -> bool`; `flush_refresh() -> None`; `set_app_id(app_id: int, *, request: bool = True) -> None`; `_load_choices() -> None`; `_handle_failure(reason: str | None, message: str) -> None`; `subject_changed = pyqtSignal(object)` emitting only positive int or None. Preserve `set_window`, `set_sort`, `request_refresh`, table/result APIs and state_changed. Add actual `files_updating` QLabel after the footer.

**Tests to make pass:** All of `tests/p4/test_files_view.py`, with the named additions and migrations below.

**Commit message:** `fix(ui): gate Files requests and preserve results across scope retirement`

### Implementation slice

- [ ] Add `_transient=False`, `_scope_ready=True`, `_refresh_wanted=False`, `_outstanding=False`, `_updating_scope=None`, `_deferred_window=None`, retaining existing generation/window/subject/key/result state. Validate bool flags; changing transient/admission never directly queries. Disable selector/search/direction/refresh and clickable header while blocked, preserving table, scroll, result and every footer quantity (spec §5.1).
- [ ] Implement nested transient scopes by restoring the previous classification boolean in finally. On exit retire only if `retire_on_exit` or the injected history view_state starts with unavailable. Prepare callers will use True because router preparation leaves loading; default healthy pump/tick scopes must not retire. `retire_deliveries` always increments generation; an outstanding request, even a completed Future awaiting delivery, sets wanted before outstanding clears. Closed views schedule no replacement (spec §§5.1, 5.5).
- [ ] Gate every programmatic entry point before any source-status, choice or query read. While blocked, set_window records deferred/wanted without adoption/clearing; valid subject/sort/search/direction changes store the latest key and wanted. Retire an outstanding old key before replacing it, including while blocked. Same subject is idempotent. `set_app_id(request=False)` synchronises selector and marks wanted without a query. `set_updating_scope` changes destination only, never the adopted `_window` (spec §5.2).
- [ ] Admitted changed-window adoption discards stale deferred scope in favour of the canonical supplied frame, loads choices with signals blocked, retains old rows/quantities, and owns exactly one request if source/subject exists. Every admitted applied set_window, including equal Window, loads the union once. Equal-window membership refresh alone makes no row query; a fallback marks wanted for the later flush. flush returns without consuming wanted when closed/blocked/not ready/missing scope or subject, otherwise makes at most one attempt and clears wanted as it begins. Admitted failure is terminal, not a retry loop. Initial request uses existing loading text; replacement shows `updating to` while retaining quantities (spec §§5.1–5.2, 7).
- [ ] Replace ranking-empty fallback with `_load_choices`: ranked rows retain query order; when local and registry both exist, call file_applications once, map all keys to stable IDs, deduplicate against ranked and prior extras, and sort extras by `(name, app_key.encode("utf-8"))`. Never use a basename as identity. Selector visible iff extras exist. Retain valid current subject, else first ranked, first file-only, None. Emit subject_changed only on actual change, once after blocked-signal population; selector user action emits and requests once. Empty available union retires old delivery, clears subject/selection/table and uses the unchanged no-applications footer; source/choice failure must not become empty success (spec §5.3).
- [ ] Centralise failure handling exactly as `self._transient or not self._scope_ready`, after the closed/generation guard. Apply it to failed Futures, synchronous status/choice/request failures, `_settle`, and `_publish` refusal of unavailable coverage or absent served interval. Transient failure preserves model/result/footer, marks wanted and shows destination; terminal failure clears using the existing unavailable state and all eight unavailable quantity labels. Stale/closed success/error never clears a newer outstanding slot or consumes wanted. Valid old-scope success while blocked may update only that still-displayed matching key; it cannot adopt pending scope or remove its destination. Valid empty success is measured empty, not unavailable (spec §§5.4–5.5).

### Tests and exact migrations

Extend FakeRouter with controlled readiness, view_state, pending Futures, status/choice/request counters and optional local/registry seams. Keep existing tests for basename-only rows, stored last-seen ordering/other-boot labels, NFC search, server sorting, incomplete/measured-empty states, exact source/A22 refusals, separate counted omissions/uncounted overflow, hooks/weak/skipped counts, served intervals and import boundaries.

Add these exact assertion-bearing tests:

- `test_scope_admission_defers_all_entry_points_and_flushes_latest_key_once`
- `test_transient_context_retires_only_invalidating_or_forced_exits`
- `test_file_delivery_outcomes_preserve_or_clear_the_correct_state`
- `test_retired_file_delivery_does_not_release_a_new_request`
- `test_changed_query_key_retires_old_success_while_blocked`
- `test_valid_empty_file_success_replaces_rows_without_becoming_unavailable`
- `test_choices_union_preserves_ranked_order_and_sorted_file_extras`
- `test_same_window_adoption_refreshes_choices_without_a_table_query`
- `test_vanished_subject_falls_back_with_one_deferred_query`
- `test_empty_choice_union_retires_subject_and_notifies_once`

Replace `test_a_new_window_requeries_and_clears_the_previous_rows` with `test_a_new_window_requeries_and_preserves_previous_rows_until_settled`. Update `test_app_selection_requests_the_selected_app_and_ranks_once_per_window` and `test_file_selector_uses_retained_file_apps_when_process_ranking_is_empty` for the union and once-per-applied-preparation rule, keeping their query/identity observations. Sort/search actions do not refresh choices.

Parameterise blocked window/app/sort/request/search/direction/header entry points, each transient predicate term separately and neither, failed Future/synchronous error/invalid-result outcomes, stale success/error after newer request, nested/forced/healthy transient exits, and fallback ranked→file-only→None. Snapshot rows, last_result identity, all eight footer quantities, note/scope text and scroll. Same-Window sequence ranked A→new file-only B→new ranked C→vanished A proves membership freshness with no duplicate table query. Capture the pre-next-tick stale-delivery case after transient exit while admission is still true; default conditional retirement must protect it, while a healthy outstanding request remains untouched.

### Validation commands

```bash
cd /data/home/henry-arch/.local/state/pi/project-manager/worktrees/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-20261101-175829-4410c030/integration
. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_files_view.py
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/files_view.py
```

## Task 3: The two-tab console, subject and frame diffing

**Implementer tier:** Advanced

**Lane/category:** console / persistent navigation, shared subject and render-model diffing.

**Depends on:** 2.

**Exclusive write allowlist:** `src/appio/ui/main_window.py` and `tests/test_ui.py`. In tests, extend the compatible query fake/build fixtures, append the thirteen spec §9.4 tests and migrate the named old tab guard; preserve all Task 1 builder/dataclass tests and existing measurement/A22/contract-only assertions.

**Read-only inputs:** Spec §§1.1, 2.1–2.2, 3.1–3.4, 5.2–5.3, 6.4, 7–8, 9.1, 9.4, 9.6, 10; design §§2–4, 6, 7.3, 9 and the three asset paths above; `src/appio/ui/render_model.py`, `src/appio/ui/charts.py`, `src/appio/ui/theme.py`, `src/appio/ui/files_view.py`, `src/appio/ui/formatting.py`, `src/appio/contracts.py`.

**Consumes:** `build_ranking` and `build_overview` with their exact spec §2.2 signatures, frozen model fields, all chart classes/setters from spec §3.2, and Files `subject_changed`, `set_app_id(..., request=False)`, `set_window`, `flush_refresh`, readiness/wanted helpers. Query input is structural only; Files capabilities may be absent.

**Produces:** UI-local `QueryService` retains `ranking(self, window: Window, metric: str) -> Sequence[AppTotal]` and `disk_share(self, app_id: int, window: Window) -> DiskShare`, adds `series(self, app_id: int, window: Window) -> Series`; no concrete service signature changes. `AppIoWindow(query_service: QueryService, boot_id: str | None = None, end_mono_ns: int | None = None, *, files_view: FilesView | None = None)`; `set_window`, `refresh`, `selected_app_id`, `_apply_ranking`, `_apply_overview`, `_set_subject(app_id: int | None, *, request_files: bool)`, `_on_files_subject_changed`, `_load_selected`, `show_overview_status(message: str, *, keep_frame: bool)`, and getter-only `has_frame` as spec §3.3. Produce `RankingDelegate(parent: QObject | None = None)` with the exact paint/sizeHint signatures there. Expose one `detail_tabs` QTabWidget, one `files_view` and existing-named message/settings-link/history-status widgets for shell aliasing. `_subject_app_id` is the sole subject; state includes models, totals, `_owns_files_scope`, optional `_active_window`/`_since_boot`.

**Tests to make pass:** All of `tests/test_ui.py`, `tests/test_charts.py`, `tests/p4/test_files_view.py`.

**Commit message:** `feat(ui): retain a two-tab console and diff ranking and overview frames`

### Implementation slice

- [ ] Build the persistent host once: ranking list with existing objectName, exactly `Overview` then `Files` in named `detail_tabs`, five fixed-order KPI labels with existing names, dual TOTAL, three reusable device lanes, donut/share panel, reconciliation/coverage/detail/status, and one Files surface/message/settings link. Reuse an injected FilesView exactly once; otherwise own one. Optional boot/end must both be absent or both valid. Empty construction creates no query, leaves both scopes None and Since boot disabled; both supplied retain standalone initial since-boot rendering. refresh and scope handlers safely return before a scope exists (spec §3.3).
- [ ] Extend the UI protocol with series while keeping ranking and disk_share exact. Reuse builders/formatters and producer-free imports. Structural process-only fakes receive the normal Files source-unavailable surface. Apply A22 before queries, preserve exact requested ranges, remember an actually supplied since-boot scope, and never invent/enable a new range-chip feature (spec §§1.1, 3.3, 10).
- [ ] The delegate uses Qt style for selection/background/text and a BAR_HEIGHT track/fill from UserRole+1, with extra bar-row sizeHint; never parse labels. Ranking diff compares `(app_id, label, total_bytes, share)` and stores ID at UserRole. Unchanged ID order preserves item identity and selection with no clear/reset/setCurrentRow; set only changed text/share and repaint changed-share row viewport rectangles. Changed order saves subject/scroll, rebuilds under blocked signals, restores row or clears both current index and selection, restores scroll clamped only to a smaller new maximum and restores the previous signal-block state (spec §3.3).
- [ ] Validate positive subject IDs or None; unchanged subject is a no-op. Ranking user action passes request_files=True; frame synchronization and Files selector notification pass False because the selector owns its one request. Keep a valid file-only subject and clear ranking highlight/current index while preserving scroll; explain `not in the process ranking` with five unavailable KPIs and no time shapes. Fallback current-valid→first ranked→first file-only→None. None clears Overview/highlight without calling positive-only Files.set_app_id. Connect subject_changed exactly once (spec §§3.3, 5.2–5.3).
- [ ] Each refresh ranks once; for a ranked subject query series and share independently at most once each, catching errors at their dependency boundaries with nonempty text/class-name fallback. If subject selection already loaded it, do not load again. No Files query on repeated process redraw. Embedded set_window renders process models only; the shell owns adoption. Standalone set_window adopts owned Files once after subject choice and flushes only when adoption did not already query. Tab changes drive no scope/query/helper/collector work (spec §§3.3, 5.2).
- [ ] Equality-diff OverviewModel, touching only changed labels/chart inputs. Use raw tokens, exact independent gutters, at most three active lanes, positive more count only, and every share row; lifetime share rows append boot IDs and do not draw cross-boot chronology. Keep exact signed reconciliation and device-reason strip distinct from the unavailable DEVICE value. Apply the complete spec §8 failure table, including no fallback donut after share failure. Empty/unavailable KPIs are unavailable, never fabricated 0; empty detail retains active kind. Applied empty/error states count as frames. Pending `show_overview_status(keep_frame=True)` changes status only, with no chart unavailable setter or data replacement; False keeps tabs and the initial waiting surface. Remove old `_render_share` (spec §§2.2, 3.3, 6.4, 7–8).

### Exact test inventory and migration

Implement each spec §9.4 function exactly once:

- `test_identical_refresh_preserves_item_identity_and_current_row`
- `test_changed_shares_with_identical_labels_repaint_without_rebuild`
- `test_changed_order_restores_subject_and_scroll`
- `test_detail_tabs_are_exactly_overview_then_files`
- `test_two_tab_switches_preserve_subject_without_scope_or_query_calls`
- `test_files_selector_visibility_depends_on_union_extras`
- `test_file_only_selection_clears_highlight_preserves_scroll_and_explains_overview`
- `test_unavailable_series_is_not_rendered_as_zero`
- `test_ranking_error_marks_all_overview_dependencies_unavailable`
- `test_share_error_keeps_task_lane_and_marks_dependent_values_unavailable`
- `test_series_failure_and_empty_series_are_distinct`
- `test_device_unavailable_keeps_task_composition`
- `test_empty_ranking_has_no_shapes_or_zero_kpis`

Use actual item/widget/model identities and spies on clear/reset/setCurrentRow/currentItemChanged, not matching text alone. In the rounded-label case change leader 1500→1530: text stays equal, subordinate share changes and repaint rectangles prove the update. Tab-switch spies cover prepare/set_window/file requests/collector/helper counts. Empty-union transition must clear both subjects/highlight and drop a late old-subject result. Use the spec §9.1 independent fake errors and a structural mixed A/B history; preserve exact residual, None/measured-zero peak, supported-empty-range kind, A22 and query-only observations.

Migrate `test_no_files_tab_tray_daemon_or_privileged_action` in place, retaining its name: assert exactly the approved two-tab host and one Files surface while preserving no tray/daemon/privilege observations. The separate later-scope acceptance guard belongs to Task 5.

### Validation commands

```bash
cd /data/home/henry-arch/.local/state/pi/project-manager/worktrees/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-20261101-175829-4410c030/integration
. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_ui.py tests/test_charts.py tests/p4/test_files_view.py
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/main_window.py src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/files_view.py
```

## Task 4: Shell frame state machine and probe worker

**Implementer tier:** Frontier

**Lane/category:** shell / frame preparation, asynchronous probes and Files invalidation integration.

**Depends on:** 3.

**Exclusive write allowlist:** `src/appio/ui/collector_window.py`; `tests/p3/test_collector_window.py` imports, compatible FakeHistory extensions, new ControlledHistory/ControlledProbe/mixed_history/settle_probe and real-router race fixtures, and the 28 new tests listed below. Existing collector-window test bodies, `build` and FakeSetup sequencing migrations belong to Task 5. Keep old FakeHistory no-argument become_ready compatibility while adding exact-scope readiness and series support. No producer/router/coordinator/setup-client changes.

**Read-only inputs:** Spec §§1.1, 3.3–3.5, 4.1–4.7, 5.1–5.5, 7–8, 9.1, 9.5, 9.6, 10; design §§3–7, 9; `src/appio/ui/main_window.py`, `src/appio/ui/files_view.py`, `src/appio/contracts.py`, `src/appio/collector_owner.py`, `src/appio/history_router.py`, `src/appio/setup_client.py`, `src/appio/iodclient.py`, `tests/p3/test_history_router.py`, `tests/p4/test_file_history_router.py`, `tests/p3/test_iod.py`.

**Consumes:** Persistent empty AppIoWindow with optional boot/end, injected FilesView, set_window, status/has_frame/subject and detail_tabs; Files transient/admission/updating/retirement/wanted/flush APIs from spec §3.4. Existing setup `refresh() -> ServiceFacts`, `invoke(verb) -> Future[SetupReply]`, coordinator reconcile/tick_local/accept_export, router prepare/ready/view_state, and verified client-state contracts retain signatures.

**Produces:** Exact spec §3.5 CollectorWindow signature, persistent tab-host aliases, getter-only `_prepared == _displayed`, getter-only `facts_applied`, `_history_retired() -> bool`, `_tick_target() -> None`, `show_window(window: Window) -> None`, `apply_ready_frame() -> bool`, `_apply_frame() -> None`, poll-only `_poll_history`, `_ensure_view(window: Window | None = None) -> AppIoWindow`, `_sync_files(*, request: bool = False) -> None`, tab-only `open_files_unavailable`, idempotent shutdown. Frozen `PrepareRetry(window: Window, owner: Literal["clock", "explicit"], since: int)` and validated frozen `ProbePayload(facts: ServiceFacts | None, error: str | None, ticket: int, generation: int)`; `_ProbeBridge.delivered = pyqtSignal(object)` explicitly queued. Probe methods are `_submit_probe(*, replace_pending: bool = False) -> bool`, `_probe_completed(ticket: int, generation: int, future: Future[ServiceFacts]) -> None`, `_apply_facts(payload: ProbePayload) -> None`. Preserve all exact state fields in spec §4.1, including target/displayed/pending, retry/error, ticket/generation/applied/dropped counts, one executor and logical successor. Fixtures expose exact ready scope, prepare log/retirement/failure seams, controlled file Futures, worker IDs/cancelled successors, and `settle_probe(qtbot, window, *, after)` waiting only for facts_applied to advance.

**Tests to make pass:** The 28 new spec §9.5 tests in `tests/p3/test_collector_window.py`, selected explicitly in the final command, plus complete `tests/test_ui.py`, `tests/test_charts.py`, `tests/p4/test_files_view.py`. Unmigrated old shell assumptions may still fail until Task 5; report that boundary, do not delete or weaken them.

**Commit message:** `fix(ui): apply shell frames and service probes with explicit ownership`

### Implementation slice: persistent host and frame ownership

- [ ] Replace files_nav, outer QStackedWidget and independent pages with one persistent empty AppIoWindow containing the one shell-owned FilesView. Alias files_view/files_message/files_open_settings/history_state to its widgets; preserve Settings and all helper/consent names, text and command vectors. `_ensure_view` only constructs/returns host, never prepares/renders; `_hide_view` cannot hide tabs or a retained frame; history_visible means an applied frame. Compatibility Files navigation merely selects tab, updates messages and performs no query, including before any sample (spec §§3.5, 7).
- [ ] Implement spec §4.2 algorithms in their normative order without Qt event processing. Preserve `_current_window` verified-cursor/local-newest-boot trust boundary and raw-bucket rounding. `_history_retired` is view_state.startswith("unavailable"), not per-call invalidation latches. Clock ticks update target while a nonretired pending A remains; advancing target to B cannot replace A. Apply A first, then prepare B on a later tick. Retired pending gets one current-target replacement; poll may discard retired pending but leaves replacement to the next target tick. Unchanged applied scope without retirement is not prepared again (spec §§4.1–4.4).
- [ ] Before each prepare close Files admission and set destination; wrap both `_tick_target` and show_window prepare calls in transient_scope(retire_on_exit=True). On a post-loading exception keep displayed/data/footer unchanged, pending None, admission closed, preparation_error and tagged PrepareRetry. Clock owner retries once only on the next later tick; repeated failure renews the timestamp. Explicit show_window clears prior marker and re-raises its post-loading exception after recording explicit ownership. Clock/apply never retry/consume that marker, even when target becomes None. Same-window explicit call retries; different-window call supersedes as a new request. Pre-loading validation errors keep existing behavior (spec §§4.2–4.4, 8).
- [ ] Only construction/no-target reset and successful `_apply_frame` assign displayed; only `_apply_frame` calls `_show_ready`. Apply order is: capture pending; assign displayed; clear pending; clear invalidated; retire Files deliveries; set_scope_ready(True); `_show_ready(window)`; flush_refresh; clear updating labels and stop poll. `_show_ready` renders view.set_window then `_sync_files(request=False)` with displayed already visible through prepared alias; it neither assigns displayed nor prepares. `_prepared` has no setter or requested-state backing field. Remove `_render_history(keep_pending=...)`; keep `_current_window`, `_render`, `_show_error` and `_clear_error` semantics. Canonical adoption owns one changed-window query; equal-window membership refresh leaves one wanted flush. Legacy request=True cannot add a second adoption query (spec §§3.5, 4.2, 5.2).
- [ ] Keep 1 s refresh and 100 ms pending-only readiness poll; poll still applies while old frame visible and computes no clock target or probe. Initial status is loading history; replacements use updating inside Overview and retain all data. Before first frame, degraded Files source diagnostic/settings link outranks generic loading if target/pending exists. No-target waiting restores admission and accessible tabs, clearing clock retry/error but retaining explicit marker/admission closure (spec §§3.5, 4.2–4.4, 7–8).

### Implementation slice: probe and helper arbitration

- [ ] Construct one ThreadPoolExecutor(max_workers=1) and queued probe bridge without a synchronous constructor probe. Worker callable only runs setup.refresh; completion converts settled facts/error to immutable payload and emits. Never pump/reconcile/sample/access Qt/live SQLite from worker. Capture immutable ticket/generation arguments. Payload ValueError matrix rejects both/neither facts/error, wrong facts, blank/whitespace/nonstring error, bool/nonpositive/noninteger ticket, bool/negative/noninteger generation (spec §§4.1, 4.6).
- [ ] `_submit_probe` returns False closed/busy/occupied without replacement, including done Future awaiting queued delivery. New submission increments ticket, stamps setup generation and stores current Future; submit failure releases and reports error. Setup completion replacement allocates newer ticket first, cancels/clears old logical successor, and submits exactly one new successor behind any running old probe. At most one running callable and one noncancelled successor; cancelled executor entries may remain physically queued but never execute setup.refresh. Periodic ticks cannot pile up work. Never forcibly stop a running probe (spec §4.6).
- [ ] `_apply_facts` first returns closed without any state/counter effect. Otherwise reject ticket mismatch OR generation mismatch OR busy, increment dropped once, and release a slot only when payload ticket matches current ticket. Older success/error cannot clear newer slot or overwrite facts/banner/error/frame/ownership. Current accepted error releases only its slot, reports `appio: error`, preserves accepted state and never increments applied count. For current facts, release matching slot and follow the exact accepted-facts sequence below (spec §4.6).
- [ ] Refresh on Qt: closed guard; pump in default transient_scope; tick target; apply readiness even busy/probe-occupied; if no frame applied and displayed ready/no pending, flush once; submit only if not busy and eligible. Catch operational exceptions so timers survive. Accepted facts: promote only a currently verified answering connection, preserving activation; compute `keep_pending = (promoted_facts == self._facts and self._pending is not None)` before assignment, without extra verification/answering/loading conditions. In default transient_scope reconcile unless keep_pending, and always tick_local. Only after successful coordinator work assign facts/increment applied, render banner/switch, tick target/apply frame and ready/no-frame flush. Coordinator errors retain prior banner/frame/count. A running probe must not stall pump or pending application (spec §4.5).
- [ ] Each helper start increments setup generation immediately before busy/invoke; every successful/nonzero/exceptional/synchronous-failure completion increments again before busy clears. Synchronous invoke failure shares completion epilogue. Preserve helper-body, consent, choice/restart warning, verified export/import and unsupported/missing-helper effects. Completion owns exactly one replacement probe when live and no chained start made busy again; remove duplicate refresh/probe tails from _fail/_after_enable/_after_disable/_after_status. No helper body supplies unprivileged answering truth (spec §§3.5, 4.6–4.7).
- [ ] Shutdown sets closed first, stops both timers, closes dialogs and retires Files, calls executor.shutdown(wait=False, cancel_futures=True), then preserves coordinator/history close order. Late success/error changes no widgets, ownership, applied or dropped counts. Do not join a worker on Qt. Every controlled held worker is released in test finally cleanup (spec §4.7).

### Implementation slice: Files invalidation integration and race evidence

- [ ] Wrap all live invalidators exactly: pump/on_gap/record_transport_gap; accepted-facts reconcile and sampled tick_local; `_quiesce_local` reconcile(QUIESCE_FACTS); `_after_disable` validated accept_export/add_source use default transient_scope. Both preparation callers use forced exit retirement. Keep quiesce/import return/error/no-elevation guarantees. Healthy default exits retire nothing. Retirement-before-ready ordering remains mandatory at successful apply (spec §§3.5, 4.5, 5.5).
- [ ] Build real-router tmp-path/file-backed fixtures plus captured/released Files bridge delivery retaining production `_on_delivered`. Parameterise SR-1 pre-next-tick schedules across pump/gap, fact reconcile, sampled tick_local, enable quiesce, disable import and both prepare callers. Worker settles between router generation bump and cancellation; release queued delivery after context exit while transient=False and admission still True, before next target tick. Conditional unavailable exit retirement protects default paths; force flag protects loading prepare paths. Also drive synchronous retirement, queued delivery after admission closes, and failure delivered after replacement is applied/new request exists. Preserve rows/result identity/all footer quantities/sort/search/subject/scroll and exactly one replacement request. Healthy pump/tick with an outstanding Future must show zero retirements/replacements (spec §§5.4–5.5, 9.5).
- [ ] Make each race/control assertion independently observe its mechanism: quiesce wrapper, export wrapper, conditional exit retirement, prepare force flag, retire-before-ready ordering and the healthy-exit exception. Keep deterministic barriers and named preservation/request-count assertions so Task 5 can verify each mechanism in disposable copies using these fixtures, without an undocumented handoff or checkout mutation.

### Exact new shell tests and their fixture obligations

Implement each name exactly once in `tests/p3/test_collector_window.py`:

- `test_slow_probe_does_not_block_pump_or_pending_frame_application`
- `test_replacement_keeps_frame_and_eventually_displays_new_scope`
- `test_applied_scope_is_not_prepared_again_without_change_or_retirement`
- `test_unchanged_facts_skip_reconcile_while_preparation_is_pending`
- `test_retired_pending_preparation_is_replaced_once`
- `test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick`
- `test_probe_spanning_setup_is_dropped_and_completion_probe_is_applied`
- `test_off_probe_during_enable_never_reacquires_local_writer`
- `test_pump_transport_gap_preserves_files_through_replacement`
- `test_disable_export_import_preserves_files_through_add_source`
- `test_worker_wins_cancellation_delivery_is_transient_while_pending`
- `test_transient_scope_exit_retires_worker_delivery_before_next_target_tick`
- `test_late_retired_file_delivery_after_ready_is_dropped_and_requeried_once`
- `test_pending_scope_refuses_all_file_entry_points_then_flushes_once`
- `test_old_probe_ticket_cannot_overwrite_or_release_replacement_probe`
- `test_probe_payload_rejects_malformed_combinations_and_counters`
- `test_repeated_setup_stages_replace_one_queued_successor_while_probe_runs`
- `test_helper_completion_categories_advance_generation_before_busy_clears_and_reprobe`
- `test_mixed_ranking_and_file_only_union_queries_file_only_subject`
- `test_same_window_frame_refreshes_union_for_file_only_ranked_and_vanished_apps`
- `test_close_during_probe_drops_all_late_effects`
- `test_files_compatibility_navigation_selects_tab_without_banner_button`
- `test_probe_error_keeps_last_banner_and_frame`
- `test_no_target_restores_admission_without_hiding_tabs`
- `test_files_source_failure_preserves_exact_message_and_link`
- `test_current_terminal_file_failure_clears_but_transient_failure_does_not`
- `test_explicit_show_window_driver_does_not_compute_clock_target`
- `test_frame_apply_sets_displayed_before_files_sync`

`settle_probe` waits with qtbot.waitUntil for facts_applied > after and never repeatedly refreshes; submission belongs to caller. Dropped/error paths await their own observations. ControlledHistory readiness is independent of probe completion, with exact Window, prepare logs and post-loading exceptions. Test all three real failure boundaries: source in_transaction, missing file-backed database and executor.submit; repeat through explicit driver, no-target and superseding explicit scopes. Mixed history proves ranked A/file-only B registry identity plus same-Window newly file-only/newly-ranked/vanished sequences.

Hold the slow probe at least 300 ms on its worker; measured refresh is under 100 ms while pump advances and pending frame applies before release. Include done-awaiting-delivery slot occupancy. Hold OFF across helper start/completion and across two successive helper stages; observe cancelled successor entries separately and prove callables never ran. Parameterise helper completion success, nonzero exit, Future exception and synchronous invoke exception. Deliver stale facts/errors before and after newest acceptance; only current ticket may release. Close while held and prove nonwaiting teardown and zero late effects. Preserve exact diagnostics, model/widget/table identity, prepared alias inside Files synchronization and file request counts, not only final labels.

### Validation commands

```bash
cd /data/home/henry-arch/.local/state/pi/project-manager/worktrees/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-20261101-175829-4410c030/integration
. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_ui.py tests/test_charts.py tests/p4/test_files_view.py
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_collector_window.py -k 'test_slow_probe_does_not_block_pump_or_pending_frame_application or test_replacement_keeps_frame_and_eventually_displays_new_scope or test_applied_scope_is_not_prepared_again_without_change_or_retirement or test_unchanged_facts_skip_reconcile_while_preparation_is_pending or test_retired_pending_preparation_is_replaced_once or test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick or test_probe_spanning_setup_is_dropped_and_completion_probe_is_applied or test_off_probe_during_enable_never_reacquires_local_writer or test_pump_transport_gap_preserves_files_through_replacement or test_disable_export_import_preserves_files_through_add_source or test_worker_wins_cancellation_delivery_is_transient_while_pending or test_transient_scope_exit_retires_worker_delivery_before_next_target_tick or test_late_retired_file_delivery_after_ready_is_dropped_and_requeried_once or test_pending_scope_refuses_all_file_entry_points_then_flushes_once or test_old_probe_ticket_cannot_overwrite_or_release_replacement_probe or test_probe_payload_rejects_malformed_combinations_and_counters or test_repeated_setup_stages_replace_one_queued_successor_while_probe_runs or test_helper_completion_categories_advance_generation_before_busy_clears_and_reprobe or test_mixed_ranking_and_file_only_union_queries_file_only_subject or test_same_window_frame_refreshes_union_for_file_only_ranked_and_vanished_apps or test_close_during_probe_drops_all_late_effects or test_files_compatibility_navigation_selects_tab_without_banner_button or test_probe_error_keeps_last_banner_and_frame or test_no_target_restores_admission_without_hiding_tabs or test_files_source_failure_preserves_exact_message_and_link or test_current_terminal_file_failure_clears_but_transient_failure_does_not or test_explicit_show_window_driver_does_not_compute_clock_target or test_frame_apply_sets_displayed_before_files_sync'
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/collector_window.py src/appio/ui/files_view.py
```

## Task 5: Migrate the remaining tests and run the gates

**Implementer tier:** Capable

**Lane/category:** integration / regression and acceptance migration, complete implementation gates.

**Depends on:** 4.

**Exclusive write allowlist:** Modify `tests/p3/test_collector_window.py` only the legacy tests, compatible `build`/FakeSetup fixture and imports listed below; modify `tests/p3/acceptance_harness.py` only imports, until/open_gui/settle/advance/query/render driver sites; modify driver/wait sites in `tests/p3/test_p3_acceptance.py`, `tests/p3/test_p3_adversarial.py`, `tests/p3/test_history_overlap_entry.py`; modify only the widget import and structural test in `tests/acceptance/test_no_later_scope.py`; create `docs/superpowers/reviews/2026-11-02-appio-console-charts-gates.md`. Preserve Task 4's new tests/fixtures and all substantive legacy assertions. No production, configuration, dependency, design, spec, historical plan or other test edits; route a production failure to its earlier owning task through the PM.

**Read-only inputs:** Spec §§1–10, especially 4.2–4.7, 5.2–5.5, 8, 9.1–9.7; design §§1–9 and the three asset paths above; `src/appio/ui/render_model.py`, `src/appio/ui/charts.py`, `src/appio/ui/theme.py`, `src/appio/ui/main_window.py`, `src/appio/ui/files_view.py`, `src/appio/ui/collector_window.py`, `src/appio/contracts.py`, `src/appio/history_router.py`, `src/appio/collector_owner.py`, `src/appio/iod.py`, `src/appio/iod_history.py`, `src/appio/iodclient.py`, `src/appio/setup_client.py`, `tests/test_ui.py`, `tests/test_charts.py`, `tests/p4/test_files_view.py`, `tests/acceptance/test_p1_p2.py`, `pyproject.toml`. This task obtains gate evidence directly from these files and its own commands; no earlier worker report is required.

**Consumes:** Getter-only facts_applied and prepared/displayed observation, target/pending/retry, `refresh()`, `show_window(window)`, `apply_ready_frame()`, detail_tabs/Files aliases, and `settle_probe(qtbot, window, *, after)` from Task 4. Uses real daemon/RPC/client/production-entry contracts unchanged. Only the probe worker performs blocking raw status; all test-owned daemon work remains on its owner thread.

**Produces:** Exact `P3Harness.until(self, condition: Callable[[], bool], *, seconds: float = 5, process_events: bool = True) -> None`; preserves settle(gui=None), advance(seconds: int), query(window: Window, *, gui=None), render(window, *, gui=None) call compatibility. Produces migrated assertions tied to applied-fact/frame edges, positive approved tab boundary checks, and the observed gate receipt at the allowlisted 2026-11-02 path. No runtime API or measurement changes.

**Tests to make pass:** Complete `tests/p3/test_collector_window.py`, `tests/p3/test_p3_acceptance.py`, `tests/p3/test_p3_adversarial.py`, `tests/p3/test_history_overlap_entry.py`, `tests/acceptance/test_no_later_scope.py`, `tests/acceptance/test_p1_p2.py`, followed by the full suite and exact six-file mypy gate below.

**Commit message:** `test(ui): migrate asynchronous console drivers and record complete gates`

### Legacy shell sequencing checklist

Keep `build(qtbot, *, facts=OFF, ready=False, setup=None, events=None)` compatible. FakeSetup.refresh now runs on a worker: separate worker observations from Qt mutation and protect shared controls with Events/locks. At a fact-dependent action capture after=facts_applied, invoke the action/submission and wait with settle_probe before assertions. After helper completion await its fresh applied probe, not busy=False alone; stale/error cases await dropped/error observations. Preserve exact helper calls and accounting, no sleeps or broad eventual-state replacements.

Migrate every named function below, preserving the listed observations (spec §9.6):

| Existing function | Required retained observation and new wait |
| --- | --- |
| `test_verified_status_round_trip_clears_silent_header_while_window_open` | Initial SILENT, verified banner/Settings recovery without helper; disconnect clears verification. |
| `test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF` | Three distinct submissions/applications, answering truth and per-cycle probe/tick counts. |
| `test_label_recovers_after_silent_refresh_and_connection_reverification` | Each silent/reverified probe count and rendered transition. |
| `test_current_link_status_requires_verification_and_preserves_activation` | Current verified connection only; activation preserved. |
| `test_the_four_honest_states_render_distinctly` | Exact four distinct banner/switch states after facts. |
| `test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers` | Post-helper GUI probe, never helper body, supplies answering truth. |
| `test_an_unknown_probe_disables_the_switch_instead_of_claiming_off` | Applied unknown disables rather than claiming OFF. |
| `test_pending_activation_stays_visible_across_a_restart` | Applied activation remains visible. |
| `test_periodic_refresh_never_invokes_the_helper` | Three separately applied probes and setup.calls == []. |
| `test_the_switch_never_shows_the_click_before_the_probe` | Initial stable probed switch, consent/no elevation on click alone. |
| `test_consent_lists_the_exact_commands_before_any_elevation` | Stable initial facts; exact command disclosure before invocation. |
| `test_cancelled_consent_changes_nothing` | Stable probe, unchanged calls/actions/switch. |
| `test_a_failed_dismissal_reverts_the_switch_and_shows_the_exit_code` | Completion probe, exact exit code and retryability. |
| `test_a_dismissal_after_a_previous_success_reverts_to_the_probed_on_state` | Each applied ON/restored state and dismissal result. |
| `test_a_container_host_reason_disables_the_switch_and_is_shown_verbatim` | Exact unsupported-host text and disabled switch after application. |
| `test_a_missing_helper_reports_one_hundred_twenty_seven` | Exit 127, exact message and disabled switch. |
| `test_enable_quiesces_local_collection_before_the_first_elevation` | Applied initial facts and quiesce-before-invoke ordering; retain spanning-probe coverage. |
| `test_a_failed_quiesce_aborts_the_elevation` | Applied initial state; no invocation, error and not busy. |
| `test_restart_choice_warns_and_needs_a_distinct_confirmation` | Initial/completion facts; exact calls, warning and separate confirmation. |
| `test_reboot_choice_never_runs_restart_or_reboot` | Activation application, allowed helper calls only; no host power/session action. |
| `test_a_dismissed_choice_leaves_the_switch_on_the_probe` | Fresh probe, exact invocation list/final switch. |
| `test_disable_requires_its_own_consent_and_imports_verified_metadata` | Consent/call/export identity and final applied OFF facts. |
| `test_disable_without_verified_export_metadata_imports_nothing` | No import and retained ON facts. |
| `test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict` | Consent/status call and fresh applied verdict. |
| `test_a_pending_operation_rejects_a_second_one_across_a_qt_heartbeat` | Stable initial facts, one invocation, busy/disabled across an event turn. |
| `test_a_reply_completed_on_a_worker_thread_updates_only_on_the_qt_thread` | Initial facts, activation choice and reply/widget thread IDs remain Qt. |
| `test_a_refresh_failure_is_reported_instead_of_crashing_the_timer` | Await coordinator/error observation; prior facts/frame retained, callback remains live. |

Migrate these structural tests without changing their substantive assertions:

| Existing function | Structural replacement |
| --- | --- |
| `test_the_switch_is_the_only_root_control_and_sits_inside_settings` | Remove only blanket QTabWidget ban; keep exactly one root control in Settings. |
| `test_banner_and_files_deep_links_focus_settings_without_changing_state` | Files-tab identity replaces stack identity; keep deep links, no state change and pre-first-frame message. |
| `test_opening_files_shows_the_real_table_and_changes_only_query_state` | Use detail_tabs Files instead of files_nav/stack; tab navigation adds zero query. |
| `test_a_degraded_file_source_is_reported_while_history_is_still_loading` | Tab identity only; exact degraded diagnostic and hidden unavailable table survive. |
| `test_files_page_keeps_an_explicit_unavailable_message_and_settings_link` | Tab identity; exact unavailable message/diagnostic, no-query and Settings focus. |
| `test_the_files_surface_never_appears_inside_the_overview_window` | One real Files surface inside shared console, outside the Overview tab. |

Replace `test_pending_history_yields_to_new_scope_ownership_or_invalidation` with three explicit functions: `test_pending_history_retains_a_until_applied_before_b` (retain A when target advances B, apply A, later prepare/apply B), `test_pending_history_invalidation_reprepares_once` (retire A, one replacement, eventual application), and `test_pending_history_changed_ownership_reprepares_once` (await changed facts, one replacement and original ownership/reconcile/tick assertions). Target advance is not retirement.

Migrate `test_history_is_prepared_before_rendering_and_stale_detail_is_hidden` and `test_real_history_router_gates_rendering_until_ready`: requested scope is pending/target, displayed/prepared is set only after apply; keep initial hidden data/loading and exact scope assertions. Migrate `test_window_keeps_in_flight_history_for_the_same_scope` with at least two distinct accepted probes, preserving pumps>1, probes>1, ranking_settled>0 and final applied-frame assertion. No direct `_show_ready` calls or `_prepared` writes.

### Acceptance harness and callers

- [ ] `P3Harness.until` gains the exact produced typed signature and QApplication import. Preserve deadline, transport `pump(2)` and existing 0.001 s transport-worker yield; process QApplication events each iteration iff process_events=True. until itself never refreshes a window or computes a target. Keep this existing test-loop yield out of production Qt callbacks (spec §9.6).
- [ ] query remains direct `history.prepare(window)`, wait with `until(lambda: history.ready(window), process_events=False)`, read ranking. No window refresh, queued fact application, readiness apply or target calculation during that wait.
- [ ] settle uses two phases: capture after=window.facts_applied, loop via until with events and window.refresh until facts_applied>after; only then loop refresh until target is None OR pending is None AND prepare_retry is None AND displayed==target. Stop poll timer afterward. Delete captured history._window and scope-is-None shortcut. In advance preserve each two-second clock/counter/daemon/client step, then use this settle for each GUI. Keep open_gui's genuinely empty local gui.db, derive daemon boot identity only from verified daemon status, and retain its long refresh interval and stopped automatic timers; stop poll again after manual drivers arm it. Do not seed a local boot row to bypass readiness. A prior target or busy=False is not evidence of a new applied fact, including crashed-GUI/no-target cases.
- [ ] render keeps the exact requested Window: construct since_boot using requested boot/end; show_window(since); event-free until loop calls apply_ready_frame until displayed==since; show_window(requested); same loop until displayed==requested. Delete direct `_show_ready`, direct view.set_window and intervening query calls. Neither explicit loop processes Qt events, refreshes or calls `_tick_target`; only transport pump and stored readiness/application run (spec §§4.2, 9.6).
- [ ] In `tests/p3/test_p3_acceptance.py`, migrate drivers/waits in `test_daemon_survives_gui_close_and_disable_exports_final_commits` and `test_fixture_tick_sqlite_socket_client_router_and_window_match`, preserving every accounting/export/ownership assertion. In `tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership`, capture applied count before action and wait after helper completion for fresh facts before local-owner assertion; preserve denial/absence/no-elevation/failure injection and export tests.
- [ ] In `tests/p3/test_history_overlap_entry.py::test_live_incomplete_reply_cannot_resurrect_export_overlap_in_production_entry`, remove the old auxiliary status executor and status_reader loop that steps daemon. The new setup worker calls captured blocking raw_status directly and performs no daemon ownership work. Serialise all daemon.start/step/stop/database-export operations on the test owner thread, including the former standalone step and live-ranking reply loop. Its settle drives window.refresh/apply_ready_frame, Qt events, daemon step and live client pump on that owner thread, awaiting a new applied fact and ready displayed frame rather than mere host visibility. Record/assert thread IDs at every test-owned daemon-step wrapper; probe/status ID differs. Release/finish controlled workers before final daemon teardown. Preserve real sockets/router/production entry; no fake replacement (spec §9.6).

Preserve all spec §9.6 caller sites formerly at `test_p3_acceptance.py:43–44,66,85–89,110–114,120,127,141,163,169,175,210`, overlap-entry `:110,125`, collector-window `:1379`; names outrank baseline offsets after edits. Keep daemon totals `(160,80,48,32)` and `(200,100,60,40)`, cutoff `(BOOT,1,4,18*NS)`, export inventories, disable/no restart, and GUI-close daemon persistence. Overlap keeps no-local-reacquire, verified export/two sources, `[(BOOT,10*NS,20*NS)]`, filtered `(0,0,0,1)` versus live `(100,60,40,0)`, incomplete coverage, zero filtered series, and displayed measured `0 B` values.

### Later-scope guard migration

In `tests/acceptance/test_no_later_scope.py::test_window_has_no_later_plan_widgets_actions_or_flows`, assert exactly one QTabWidget named detail_tabs with captions `["Overview", "Files"]` and count 2; exactly one QWidget named files_view which is tab 1 or its descendant. Remove only `files` from forbidden tuple, retaining `("daemon", "socket", "bpf", "polkit", "privilege", "setup", "tray")`. Keep QSystemTrayIcon/QMenu bans, objectName/button-text scans and empty window.actions assertion. Leave `test_fresh_process_imports_and_executes_only_p1_p2_modules` and forbidden import prefixes unchanged. Import-layer failures are production defects, not permission to weaken the guard (spec §§1.1, 9.6, 10).

### Complete gates and observed receipt

- [ ] Before gates, use Python AST to count the exact spec §§9.2–9.5 top-level test functions once in their assigned modules: 7 pure + 11 chart + 13 window + 28 shell = 59. Compare against signed spec, not names derived solely from implementation. Check real assertion-bearing bodies, all spec §8 outcomes, §5.5 timings and every §9.6 migration. No name-only wrappers, new skips/xfails or assertion deletion.
- [ ] Review the six UI files against their exact spec interfaces. Check `_prepared` has no writes, only `_apply_frame` calls `_show_ready`, and non-None displayed assignments occur only at apply. Verify every default/forced transient wrapper, healthy no-retirement control, retire-before-ready and one Files query ownership. Confirm no tab-switch work, blocking Qt probe or second daemon owner.
- [ ] Verify the race mechanisms in disposable copies outside the checkout. Run a clean passing baseline, separately remove the quiesce wrapper, export wrapper, conditional exit retirement, prepare force flag and retire-before-ready ordering, then separately make exit retirement unconditional. For each change, run the corresponding real-router race/healthy-control test from `tests/p3/test_collector_window.py` with the offscreen venv, require its named preservation/request-count assertion to fail, restore and require the same test to pass. Record exact commands, barrier order, killing assertion and restored result. Import/collection failures are not kills. The checkout allowlist stays fixed; only these disposable copies may contain production mutations, and they are never integrated.
- [ ] Run the targeted migrated modules, then full pytest with the same venv/offscreen environment and the exact six-file mypy command below. Record actual exit codes, complete test totals/skips/duration, checker output and Python/PyQt6/pytest/pytest-qt/mypy versions; mypy is pinned 2.4.1. No targeted run substitutes for full suite. A failed gate is reported with owning task/file; do not add production fixes outside this allowlist or prefill a pass.
- [ ] Inspect offscreen grabs from chart smoke tests and controlled populated, empty, unavailable and pending console fixtures. Keep transient images outside checkout. Record visible ranking bars, five KPIs, dual TOTAL, raw-token three-lane/more count, task donut percentages, all device share rows, signed reconciliation/coverage, clear gap support and persistent tabs. Geometry/identity/race assertions supplement, not replace, actual grab inspection.
- [ ] Compare baseline `0eb173eb28dcba81df33e9261dbaa25d778422c0` to final implementation diff. Require unchanged producer/query/router/storage/schema/registry/protocol/helper-command/consent/packaging/runtime dependency contracts/files, and no excluded features. Verify helper/consent text and argv against baseline, with UI changes confined to listed surfaces.
- [ ] Create `docs/superpowers/reviews/2026-11-02-appio-console-charts-gates.md` from observed evidence only: toolchain, base/current revisions and working-tree state, exact commands/results, 59-test inventory and migration coverage, this task's observed mutation/barrier/restored-control evidence, visual fixture observations and protected-boundary diff. Explicitly name any blocker; record implementation-ready only after both full-suite and six-file mypy gates pass. Return the PM task report separately through its report channel, without creating an additional repository file.

### Validation commands

```bash
cd /data/home/henry-arch/.local/state/pi/project-manager/worktrees/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-20261101-175829-4410c030/integration
. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
python -c 'import sys, importlib.metadata as m; print(sys.version); print({p: m.version(p) for p in ("PyQt6", "pytest", "pytest-qt", "mypy")})'
python -m mypy --version
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_collector_window.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_history_overlap_entry.py tests/acceptance/test_no_later_scope.py tests/acceptance/test_p1_p2.py
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py
```
