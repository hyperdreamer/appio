# Appio Console Charts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax. The controller owns progress; do not tick
> boxes or otherwise edit this plan during a pinned execution.

**Goal:** Keep the console responsive and its last measured frame visible while history is replaced, put Overview and Files in one persistent detail pane, and draw the approved charts from existing query contracts.

**Architecture:** Pure frozen render models separate query outcomes from Qt updates. Cached QPainter geometry and model equality drive small updates to persistent widgets; a single subject and a single Files query owner connect the two tabs. The shell separates target, pending, and displayed scopes, and accepts service facts from one probe worker only after ticket and setup-generation checks on the Qt thread.

**Tech Stack:** Python >=3.14, PyQt6 >=6.6, standard-library dataclasses/concurrent.futures, QPainter, pytest/pytest-qt, and development-only mypy. No new runtime dependency.

### Authority and file map

Read `docs/superpowers/specs/2026-11-01-appio-console-charts-design.md` and `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md` completely before implementing. Below, “spec §N” means the latter. Design §9 corrects the mockups: conditional Files selector, raw device tokens, three time lanes maximum, and task-only suspension boundaries. Layout references are `docs/superpowers/specs/assets/2026-11-01-target-state.png`, `2026-11-01-navigation.png`, and option A in `2026-11-01-chart-sets.png` in that directory.

The design approval is commit `f444c862e607e388271ffb8a17e059c7b7dd2d91`, blob `d4cb671b28ae4baf565864ef02d341c28bfdea8d`, SHA-256 `acd2e9f81096b24bcc3a7d05eb3130945bd8da837fe9c38835ca29516512ef6d`. The signed-off specification is the implementation contract.

| File | Responsibility | Writer tasks |
| --- | --- | --- |
| `src/appio/ui/render_model.py` | Five frozen data models, pure builders, coverage/detail/reconciliation text | 1 |
| `src/appio/ui/theme.py` | Literal shared palette and drawing dimensions | 2 |
| `src/appio/ui/charts.py` | Two frozen observations, cached time geometry, lanes, donut, share bars | 2, then 3 (named classes only) |
| `src/appio/ui/files_view.py` | Admission/delivery classification, then union population | 4, then 5 (named methods only) |
| `src/appio/ui/main_window.py` | Persistent two-tab console, one subject, ranking/model diffing | 6 |
| `src/appio/ui/collector_window.py` | Frame lifecycle, probe arbitration, operational invalidation wrappers | 7, then 8, then 9 (named methods only) |
| `tests/test_ui.py` | Pure models, seven-dataclass rejection matrix, window contracts and old UI regressions | 1, 2, 6 (disjoint tests and named fixture edits) |
| `tests/test_charts.py` | Time geometry and composition/paint tests | 2, then 3 (disjoint tests) |
| `tests/p4/test_files_view.py` | Admission/outcome matrix, union membership, preserved Files semantics | 4, then 5 (named tests/fixtures) |
| `tests/p3/test_collector_window.py` | Frame, probe, cancellation schedules, then legacy test migration | 7, 8, 9, 10 (specified sections) |
| `tests/p3/acceptance_harness.py` | Fact-driven settle and event-free explicit read/render drivers | 11 |
| `tests/p3/test_p3_acceptance.py` | Existing end-to-end callers and preserved accounting/export assertions | 11 |
| `tests/p3/test_p3_adversarial.py` | Post-helper ownership assertions after fresh applied facts | 11 |
| `tests/p3/test_history_overlap_entry.py` | Real production-entry driver with one daemon owner thread | 11 |
| `tests/acceptance/test_no_later_scope.py` | Allow exactly the new tabs/Files surface, retain import/privilege boundaries | 12 |
| `docs/superpowers/reviews/2026-11-01-appio-console-charts-gates.md` | Recorded implementation toolchain, complete gates and visual/inventory evidence | 13 |

Line ranges below refer to the planning baseline; named symbols are authoritative after preceding tasks add lines. Tasks sharing a file are sequential and list the existing functions they may edit. No task may broaden its file allowlist to repair another task silently.

## Global Constraints

- Behaviour follows `docs/superpowers/specs/2026-11-01-appio-console-charts-design.md` and its signed-off implementation contract `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md`; use their exact section 7 strings and preserve design §9 corrections.
- Python floor is `>=3.14`; the runtime dependency remains `PyQt6>=6.6`. Do not change `pyproject.toml`, runtime requirements, entry points, or package version. Development tools are provisioned outside the checkout.
- No producer, protocol, storage, schema, registry, concrete `appio.query.QueryService`, or `HistoryRouter` signature changes. Existing query/router implementations are read-only; add `series` only to the UI-local protocol. No daemon revision, migration, new measurement, or host transition is required.
- `render_model.py` imports only the standard library, `appio.contracts`, and `appio.ui.formatting`. `charts.py` imports only the standard library, PyQt6, contracts, and local theme; `theme.py` only the standard library/PyQt6/contracts. Main-window and Files imports stay producer-free. Do not import concrete history/setup/daemon modules into these modules, including for runtime type annotations.
- All seven render/chart dataclasses are frozen, have exactly the spec §2 fields, and raise `ValueError` in `__post_init__` on malformed values; reject booleans as integers, nonfinite numbers, mutable collections, invalid nested objects, and availability conflicts. Never turn unavailable input into measured zero.
- Build one persistent `detail_tabs` with exactly `Overview`, `Files`, one FilesView, and stable chart widgets. One `_subject_app_id` drives both surfaces. Tabs never prepare, query, reconcile, elevate, or change scope. Embedded scope adoption belongs to CollectorWindow; standalone adoption belongs to AppIoWindow, never both.
- Preserve supported explicit range rendering and the exact A22 launch/over-365-day refusal reasons. Exclude hour-of-day strip, enabled range chips/launch window, ranking search, tray/autostart, per-file BPF scope, retention, and cadence changes.
- No blocking probe work, subprocess, socket connect/recv, or sleep in a Qt timer/apply callback. Use one `ThreadPoolExecutor(max_workers=1)`, immutable captured probe tickets/generations, and a queued bridge. Existing local sampling, boot lookup, prepare bookkeeping and nonblocking client pump remain on Qt at existing frequencies.
- Files failure classification is exactly `self._transient or not self._scope_ready`; `_handle_failure` is the common boundary. Default transient contexts retire deliveries on unavailable-router exit only; preparation uses `retire_on_exit=True` because it leaves `loading`. Healthy ticks do not retire requests. Apply-time retirement precedes restoring admission.
- Run future widget tests with `QT_QPA_PLATFORM=offscreen` and `PYTEST_QT_API=pyqt6`. Use controlled Events/Futures and Qt event pumping; no arbitrary Qt-thread sleeps. The one slow-probe worker may be held for at least 300 ms. Preserve all pre-existing substantive accounting, ownership, export, helper, and liveness assertions during migration.
- Implementation tool environment: `/tmp/appio-console-charts-pm-20261101-175829-4410c030-venv`. Every worker activates it with `. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate`, then `export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6`. If absent, first run `python -m venv --system-site-packages /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv`, activate it, then run `python -m ensurepip --upgrade` and `python -m pip install 'mypy==2.4.1' 'pytest-qt==4.5.0' 'pytest==9.1.1'`. Inherit installed PyQt6 from system site packages and record its actual version. These exact commands apply on every worker host; do not install tools into the system interpreter.
- Implementation checks in this plan are prospective. The planning worker writes only this plan, runs no test suite, and makes no commit. Implementation workers commit only their listed files with the stated messages; the PM controls integration and publication.
- Never execute reboot/shutdown/poweroff/halt/kexec, systemctl power/sleep verbs, `init 0|6`, or `telinit 0|6`. Never stop/restart a display manager or graphical target, terminate the session, or signal the process tree hosting the PM. Do not perform host service or privilege operations to test this UI; use the existing synthetic seams. If an unexpected host transition becomes necessary, stop and return the PM-required `reboot-required.json` handoff rather than taking that action.

## Task 1: Implement pure render models and the process-query fixture

**Implementer tier:** Standard

**Lane/category:** models / pure data. **Depends on:** none. **Spec:** §§2.1–2.2, 7, 8, 9.1–9.2.

**Files:**

- Create: `src/appio/ui/render_model.py`.
- Modify: `tests/test_ui.py:1-214` only imports, `FakeQueryService.__init__`, `.ranking`, `.disk_share`, new `.series`, and `build`; append the six builder tests listed below. Do not edit the existing widget test bodies.
- Read only: `src/appio/contracts.py:248-561`, `src/appio/ui/formatting.py:1-89`.

**Interfaces:**

- Consumes: existing `AppTotal`, `Window`, `Coverage`, `Series`, `SeriesPoint`, `DeviceSeriesPoint`, `DiskShare`; `format_bytes(value: int) -> str`, `format_peak(value: int | None) -> str`, `format_residual(value: int | None, complete: bool) -> str` and `UNAVAILABLE`.
- Produces: frozen `RankingRow(app_id: int, app_key: str, label: str, total_bytes: int, share: float)`; `RankingModel(rows: tuple[RankingRow, ...], empty_text: str | None, unavailable: str | None)`; `Kpi(caption: str, text: str)`; `DeviceLaneModel(dev: str, points: tuple[DeviceSeriesPoint, ...], read_bytes: int, write_bytes: int)`; `OverviewModel(app_id: int, kpis: tuple[Kpi, ...], task_points: tuple[SeriesPoint, ...], device_lanes: tuple[DeviceLaneModel, ...], more_devices: int, series_unavailable: str | None, share: DiskShare | None, share_unavailable: str | None, reconciliation: str | None, coverage_line: str, detail_line: str)`; each has `__post_init__(self) -> None`.
- Produces: `build_ranking(rows: Sequence[AppTotal], *, unavailable: str | None = None) -> RankingModel`; `build_overview(app_id: int, window: Window, total: AppTotal | None, series: Series | None, share: DiskShare | None, *, ranking_error: str | None = None, series_error: str | None = None, share_error: str | None = None) -> OverviewModel`; `scope_text(window: Window) -> str` returning `window.kind`.
- Produces fixture extension: `FakeQueryService(totals: list[AppTotal], shares: dict[int, DiskShare], *, series: dict[int, Series] | None = None)`; `series(self, app_id: int, window: Window) -> Series`; independent `ranking_error`, `share_error`, `series_error` attributes of type `BaseException | None`, and `series_calls: list[tuple[int, Window]]`. Keep the original positional constructor arguments and existing call logs.

### Steps

- [ ] **Step 1: Provision the isolated implementation tools**

The inspected host has venv/ensurepip and system PyQt6, but no system pip, mypy, or pytest-qt. Run these commands only during implementation:

```bash
python -m venv --system-site-packages /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv
. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate
python -m ensurepip --upgrade
python -m pip install 'mypy==2.4.1' 'pytest-qt==4.5.0' 'pytest==9.1.1'
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
python -c 'import sys, importlib.metadata as m; print(sys.version); print({p: m.version(p) for p in ("PyQt6", "pytest", "pytest-qt", "mypy")})'
```

Record the resolved Python/PyQt6/tool versions in the controller report for Task 13. This creates no repository dependency/configuration file.

- [ ] **Step 2: Add the pure builder tests and extend the structural fake**

Implement these exact functions once each in `tests/test_ui.py`:

| Test | Required assertions |
| --- | --- |
| `test_build_ranking_orders_by_total_then_key_and_normalises_share` | UTF-8 key tie-break, exact double-space formatted labels, largest share 1.0, subordinate fraction, zero share, all-zero denominator finite. |
| `test_build_ranking_distinguishes_empty_and_unavailable` | Empty success says `no applications observed`; error has no rows/empty text; nonempty rows plus unavailable is rejected. |
| `test_build_overview_has_five_ordered_kpis_and_none_peak_is_unavailable` | TOTAL/READ/WRITE from share, PEAK from ranking, DEVICE only from available attribution; exact five-caption order; None and measured-zero peak differ. |
| `test_build_overview_limits_device_lanes_and_counts_more` | Four positive share devices give three ordered raw tokens and more_devices 1; zero-total rows do not count; selected devices with no samples keep empty points. |
| `test_build_overview_reconciles_only_available_device_attribution` | Positive, negative and zero signed residual templates; unavailable DEVICE is never 0; undefined residual alone preserves defined device totals and bars. |
| `test_lifetime_series_does_not_join_unrelated_boot_clocks` | `lifetime chronology unavailable`, no lanes/more count, measured totals and boot-qualified share rows remain available. |

Use the existing `app_total`, `disk_share`, and `coverage` fixtures. A minimum ranking oracle is:

```python
def test_build_ranking_orders_by_total_then_key_and_normalises_share():
    from appio.ui.render_model import build_ranking
    rows = [
        app_total(2, "unit:/beta.service", "Beta", 50),
        app_total(1, "unit:/alpha.service", "Alpha", 100),
        app_total(3, "unit:/zero.service", "Zero", 0),
    ]
    model = build_ranking(rows)
    assert [r.app_id for r in model.rows] == [1, 2, 3]
    assert [r.share for r in model.rows] == [1.0, 0.5, 0.0]
    assert model.rows[0].label == f"Alpha  {format_bytes(100)}"
    assert model.empty_text is model.unavailable is None
```

Add tie and all-zero cases to this same test. The fake's default series is a valid empty `Series` for the requested app/window with the corresponding row's coverage; configured successful objects must keep their actual window so mismatch tests remain falsifiable. Do not import SQLite/router/daemon into this fake.

- [ ] **Step 3: Observe the new tests fail at the missing model boundary**

Run `python -m pytest tests/test_ui.py -k 'test_build_ or test_lifetime_series'`. Establish the missing module/symbol as the initial failure, then implement rather than weakening the assertions.

- [ ] **Step 4: Implement frozen validation and deterministic ranking**

Implement all five dataclasses and their exact spec §2.1 invariants, including nested contract types, duplicate IDs/keys/devices, ordering, interval nonoverlap, availability relationships, and the `1e-12` denominator tolerance. Counts exclude bool; optional text is None or nonempty. Shares must be finite floats. Use `ValueError`, never assert-based public validation. Ranking construction follows:

```python
ordered = sorted(rows, key=lambda row: (-row.total_bytes, row.app_key.encode("utf-8")))
largest = ordered[0].total_bytes if ordered else 0
rendered = tuple(
    RankingRow(
        row.app_id, row.app_key,
        f"{row.name}  {format_bytes(row.total_bytes)}",
        row.total_bytes,
        row.total_bytes / largest if largest else 0.0,
    )
    for row in ordered
)
```

A nonempty input and unavailable argument conflict. Empty successful input returns exactly `RankingModel((), "no applications observed", None)`.

- [ ] **Step 5: Implement independently available Overview inputs and exact text**

Validate subject IDs and exact window identity on successful series/share, and reject object-plus-error pairs. Use ranking error before the file-only reason. A missing ranked total produces five unavailable KPIs and `not in the process ranking`; do not manufacture ID 0. A share error leaves a known PEAK/task series intact; a series error leaves valid byte KPIs/composition intact. Use the exact default reasons and lifetime rule in spec §2.2.

For available single-boot device chronology, order positive share rows by descending attributable total and UTF-8 token, map same-device points, keep three, and count remaining active devices. Both series and share attribution must be available and the device total defined. Missing lane samples remain empty, never synthetic zero buckets. Suppressed lanes imply more_devices 0.

Choose coverage from successful share, then series, then ranking. Render `coverage: <complete or incomplete>; device attribution: <device_state>; tier: <tier>; gaps: <n>; suspends: <True or False>` plus the optional reason; use `coverage: unavailable` if none succeeded. Append differing successful source coverage to detail with the source name rather than merging counts. Once device attribution and both device total/residual are defined, build the exact reconciliation string as follows; the formatter's second argument expresses this established availability, rather than the overall history completeness flag:

```python
residual_text = format_residual(share.residual_total, True)
reconciliation = (
    f"Residual: {residual_text} - difference between accounting points; "
    f"{format_bytes(share.device_total_bytes)} attributed + "
    f"({format_bytes(share.residual_total)}) residual = "
    f"{format_bytes(share.task_total_bytes)} total"
)
```

No logical file quantity participates in the equation.

- [ ] **Step 6: Verify this task**

Run `python -m pytest tests/test_ui.py -k 'test_build_ or test_lifetime_series'` and `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/render_model.py`. Expect no failures/errors. The seven-class construction rejection test is added in Task 2 once both chart dataclasses exist.

- [ ] **Step 7: Commit**

```bash
git add src/appio/ui/render_model.py tests/test_ui.py
git commit -m "feat(ui): add pure console render models"
```

## Task 2: Implement the palette and lossless time-chart geometry

**Implementer tier:** Advanced

**Lane/category:** charts / geometry. **Depends on:** 1. **Spec:** §§2.1, 3.1–3.2, 6.1–6.3, 7, 9.1–9.3.

**Files:**

- Create: `src/appio/ui/theme.py`, `src/appio/ui/charts.py`, `tests/test_charts.py`.
- Modify: `tests/test_ui.py` imports and append only `test_render_dataclasses_reject_invalid_values_and_availability_conflicts`; do not change Task 1's builder tests.
- Read only: `src/appio/ui/formatting.py:22-49` as the integer-formatting oracle.
- This task owns `ChartColumn`, `ChartSegment`, `SeriesChart`, `DualSeriesChart`, `DeviceLane`, and the private `_format_bytes` helper; Task 3 later adds composition classes and reuses that helper.

**Interfaces:**

- Consumes: contract `SeriesPoint`, `DeviceSeriesPoint`, the five render dataclasses from Task 1, and the read-only `format_bytes(value: int) -> str` algorithm as the visible-label oracle. Keep the chart module's specified import surface; its byte formatting is the private helper below.
- Produces frozen `ChartColumn(start_mono_ns: int, end_mono_ns: int, read_bytes: int, write_bytes: int, peak_read_bps: int | None, peak_write_bps: int | None, starts_run: bool)` and `ChartSegment(side: Literal["read", "write"], run_index: int, polygon: tuple[tuple[float, float], ...])`, each with `__post_init__(self) -> None`.
- Produces `SeriesChart(parent: QWidget | None = None)`, with `set_task_points(points: Sequence[SeriesPoint]) -> None`, `set_unavailable(reason: str) -> None`, `columns() -> tuple[ChartColumn, ...]`, `segments() -> tuple[ChartSegment, ...]`, `is_available() -> bool`, `unavailable_reason() -> str | None`, `plot_rect() -> tuple[float, float, float, float]`, `paintEvent(event: QPaintEvent) -> None`, `resizeEvent(event: QResizeEvent) -> None`.
- Produces `DualSeriesChart(SeriesChart)` with the same constructor and `set_totals(read_bytes: int, write_bytes: int) -> None`; `DeviceLane(DualSeriesChart)` with the same constructor, `set_device_points(points: Sequence[DeviceSeriesPoint]) -> None`, `set_device(dev: str) -> None`.
- Produces theme `READ_COLOR`, `WRITE_COLOR`, `TEXT_COLOR`, `MUTED_COLOR`, `GRID_COLOR`, `TRACK_COLOR`, `BORDER_COLOR` as QColor; `AXIS_PEN_WIDTH: float`, `BAR_HEIGHT`, `PLOT_MARGIN`, `GUTTER_WIDTH` as int.
- Produces local test fixture `chart_points` containing at least four positive task buckets, configurable actual hole/task suspension, mixed defined/None peaks, and matching device buckets without suspension metadata.

### Steps

- [ ] **Step 1: Add the exact geometry tests and construction rejection matrix**

Create these nine exact tests in `tests/test_charts.py`: `test_contiguous_points_produce_one_run_per_nonzero_side`, `test_one_bucket_hole_splits_runs_without_points_or_edges_in_hole`, `test_task_suspend_splits_after_point_but_device_does_not_inherit_it`, `test_set_unavailable_clears_shapes_and_exposes_exact_reason`, `test_downsampling_preserves_bytes_peak_maxima_and_run_starts`, `test_downsampling_across_gap_never_spans_its_horizontal_interval`, `test_chart_resize_rebuilds_observations_and_keeps_geometry_in_plot`, `test_empty_available_series_draws_no_zero_line`, `test_coverage_counts_do_not_invent_chart_breaks`.

At positive plot area, verify closed finite polygons, independent read/write half-height scales, and one run per nonzero side for contiguous input. Test a hole with both sides of it deliberately sharing a bin after downsampling. This is the support oracle; use it on every read and write polygon, including the forced shared-bin case:

```python
left, top, right, bottom = chart.plot_rect()
first = points[0].bucket_start_mono_ns
last = points[-1].bucket_start_mono_ns + points[-1].bucket_secs * 10**9
hole_left = left + (hole_start - first) * (right - left) / (last - first)
hole_right = left + (hole_end - first) * (right - left) / (last - first)
for segment in chart.segments():
    xs = [x for x, _ in segment.polygon]
    assert max(xs) < hole_left or min(xs) > hole_right
```

Assert byte conservation and peak maxima independently of geometry; all-None peaks stay None. Resize across small/large/collapsed dimensions and recheck bounds, budget, and hole support. Counts in Coverage alone must not split adjacent buckets. Suspension splits after the marked task bucket, retaining its bytes; adjacent device buckets remain one run.

Add `test_render_dataclasses_reject_invalid_values_and_availability_conflicts` once in `tests/test_ui.py`. Parameterise all seven classes: invalid nested types/tuples, bool/negative/noninteger counts, duplicate identities, NaN/infinity shares/coordinates, wrong KPI order, conflicting availability, mixed/overlapping device points, invalid endpoints/peaks/run flags, invalid side/run index, and nonclosed polygons. Include valid controls so rejection cannot be implemented by rejecting everything.

- [ ] **Step 2: Run the new tests at the missing chart boundary**

Run `python -m pytest tests/test_charts.py tests/test_ui.py -k 'test_render_dataclasses or test_contiguous_points or test_one_bucket_hole or test_task_suspend or test_set_unavailable or test_downsampling or test_chart_resize or test_empty_available_series or test_coverage_counts'`. Confirm the chart module/symbol failure before implementation.

- [ ] **Step 3: Transcribe the literal shared palette and validate observations**

Create `theme.py` with exactly these values, without a theme manager or application stylesheet:

```python
from PyQt6.QtGui import QColor

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

Implement the two chart dataclasses with their exact fields/invariants. Keep polygon coordinates immutable float pairs, not QPoint or painter state. Validate ordered/nonoverlapping setters and copy to tuples. Build child `QLabel` named `chart_message` in each chart widget.

Use this private integer-only formatter in charts.py for exact gutter/composition bytes. It transcribes the existing display algorithm within the chart module's allowed imports; `render_model.py` and `main_window.py` continue to import the existing formatter normally. This adds no public or cross-module API:

```python
def _format_bytes(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("value must be an integer")
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude < 1024:
        return f"{sign}{magnitude} B"
    unit, scale = 0, 1
    while unit < len(units) - 1 and magnitude >= scale * 1024:
        unit += 1
        scale *= 1024
    whole, fraction = divmod((magnitude * 10) // scale, 10)
    if fraction:
        return f"{sign}{whole}.{fraction} {units[unit]}"
    return f"{sign}{whole} {units[unit]}"
```

Within the geometry or smoke tests, compare actual gutter/byte labels with the existing formatter for zero, 1023, 1024, 1530 and `2**80` so integer precision and unit boundaries remain observable.

- [ ] **Step 4: Cache source runs separately from aggregated columns**

Calculate intervals as `start + bucket_secs * 10**9`; a task run ends after a `spans_suspend` point or at a nonadjacent next bucket; a device run uses nonadjacency only. Do not consult coverage counts to position breaks. Let `W = max(0, floor(right - left))`, excluding margins/caption/gutter. With collapsed width/height retain inputs but expose empty columns/segments. With at most W points preserve one column per point; otherwise use the exact bin formula:

```python
bin_index = min(W - 1, ((start - first_start) * W) // (last_end - first_start))
```

Each nonempty bin has min start/max end, exact summed read/write bytes, maximum defined peak on each side (or None), and starts_run true if any contributor starts a run. Preserve private run membership and each contributor's observed duration; never use a column envelope as continuity evidence. Work is O(source points + W) on data/size changes, not on paints.

- [ ] **Step 5: Build and paint run-clipped polygons and exact gutters**

Derive each column rate from bytes divided by the sum of observed contributor durations, not elapsed span across holes; keep integer/rational arithmetic until finite coordinate mapping. Normalise read and write independently about the plot centre. For each positive side/run, use the column amplitude only on that run's observed support and close to the centre at its own endpoints. Inset outer X endpoints by `min(0.25, run_pixel_span / 4)` before closure, so entire polygon support excludes holes strictly. Multiple runs may share a subpixel bin; retain the separate fragments.

`paintEvent` uses cached polygons/theme/QPainter only. `resizeEvent` rebuilds from retained source tuples. `set_unavailable` empties observations/numeric labels and shows exactly `unavailable: <reason>`; empty available points show `no samples in this window`, no zero line. Dual TOTAL caption is `TOTAL · read above, write below`; gutters are `r <bytes> · w <bytes>` using supplied totals, not sampled sums. Device caption is the raw token. Do not create one widget or paint-time object per source point.

- [ ] **Step 6: Verify geometry and models**

Run `python -m pytest tests/test_charts.py` and `python -m pytest tests/test_ui.py -k 'test_build_ or test_lifetime_series or test_render_dataclasses'`; then `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py`. Expect no failures/errors.

- [ ] **Step 7: Commit**

```bash
git add src/appio/ui/theme.py src/appio/ui/charts.py tests/test_charts.py tests/test_ui.py
git commit -m "feat(ui): draw lossless time lanes with explicit run geometry"
```

## Task 3: Add task composition and device-share painting

**Implementer tier:** Standard

**Lane/category:** charts / composition. **Depends on:** 2. **Spec:** §§3.2, 6.4, 7, 9.3.

**Files:**

- Modify: `src/appio/ui/charts.py`, adding only `DonutChart` and `ShareBars` plus the imports they require; preserve existing time-chart dataclasses/classes/geometry functions.
- Modify: `tests/test_charts.py`, adding only the two named tests below and their composition data fixtures/imports.

**Interfaces:**

- Consumes the exact palette and existing private `_format_bytes(value: int) -> str` helper produced in Task 2; keep the chart module's standard-library/PyQt6/contracts/theme import boundary.
- Produces `DonutChart(parent: QWidget | None = None)` with `set_shares(read_bytes: int, write_bytes: int) -> None`, `set_unavailable(reason: str) -> None`, `shares() -> tuple[float, float] | None`, `unavailable_reason() -> str | None`, `paintEvent(event: QPaintEvent) -> None`.
- Produces `ShareBars(parent: QWidget | None = None)` with `set_rows(rows: Sequence[tuple[str, int]]) -> None`, `set_reconciliation(text: str | None) -> None`, `set_unavailable(reason: str) -> None`, `fractions() -> tuple[tuple[str, float], ...]`, `unavailable_reason() -> str | None`, `paintEvent(event: QPaintEvent) -> None`.
- Both widgets start available and empty and expose an actual child QLabel named `chart_message`; observations return immutable values without I/O.

### Steps

- [ ] **Step 1: Add denominator, measured-zero, unavailable, and paint tests**

Add exact `test_donut_shares_and_device_bar_fractions_use_their_defined_denominators` and `test_each_chart_grabs_nonempty_pixmap_and_renders_reason_text`. Use this numeric oracle:

```python
donut.set_shares(30, 10)
assert donut.shares() == (0.75, 0.25)
bars.set_rows((("259:0", 30), ("259:1", 15)))
assert bars.fractions() == (("259:0", 1.0), ("259:1", 0.5))
donut.set_shares(0, 0)
assert donut.shares() == (0.0, 0.0)
donut.set_unavailable("split refused")
assert donut.shares() is None
assert donut.unavailable_reason() == "split refused"
```

Cover zero rows, empty rows, negative/bool byte rejection, preservation of row order, and no NaN. Show/resize/grab all five widget classes, assert non-null/nonempty pixmaps, then unavailable reason QLabel text/visibility; after unavailable, no stale numeric labels remain. Save any inspected grabs under the test's temporary directory, not source/assets.

- [ ] **Step 2: Observe the missing classes fail**

Run `python -m pytest tests/test_charts.py -k 'test_donut_shares or test_each_chart_grabs'` before adding the classes.

- [ ] **Step 3: Paint composition from exact task bytes and relative disk totals**

Donut divides by task read+write, keeps exact byte labels, and formats percentages to one decimal place. For zero/zero draw no sectors and show `no task I/O in this window`; unavailable returns None and shows the exact reason. Caption is `READ vs WRITE` with `<bytes> read`, `<bytes> write`, `<read percent>% / <write percent>%`.

ShareBars preserves supplied row order and normalises against its largest device total, with all-zero widths 0.0. Keep every supplied row; the time-lane cap does not apply here. Caption is `ATTRIBUTED SHARE BY DISK`. `set_reconciliation` only changes the annotation, permitting `Σ disks <device bytes> + residual (<signed residual bytes>) = <task bytes> total`. Accept boot-qualified labels as supplied by the window; never resolve device names/mounts. Unavailable clears prior rows/composition/annotations and numeric text. Reuse local drawing constants, not a new stylesheet.

- [ ] **Step 4: Verify all chart tests and typecheck**

Run `python -m pytest tests/test_charts.py` and `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/charts.py src/appio/ui/theme.py`. Expect no failures/errors; inspect the five grabs for visible text and meaningful shapes.

- [ ] **Step 5: Commit**

```bash
git add src/appio/ui/charts.py tests/test_charts.py
git commit -m "feat(ui): draw task composition and attributed disk shares"
```

## Task 4: Make Files admission and delivery retirement preserve the displayed result

**Implementer tier:** Advanced

**Lane/category:** Files / asynchronous result ownership. **Depends on:** 1. **Spec:** §§3.4, 5.1–5.2, 5.4–5.5, 7, 8.

**Files:**

- Modify: `src/appio/ui/files_view.py:319-835`: `FilesView.__init__`, `_build_ui`, `set_window`, `set_app_id`, `set_sort`, `request_refresh`, `_on_delivered`, `_settle`, `_publish`, `_on_app_changed`, `_on_search_changed`, `_on_direction_clicked`, `closeEvent`; add the admission/transient methods below and `_handle_failure`. In `_load_ranking`, change only its two exception handlers to use `_handle_failure`; Task 5 owns replacement of its population algorithm. Preserve table/formatting/coverage implementations. Task 5 explicitly revisits `__init__`, `set_window`, `_load_ranking`, and `_on_app_changed` to add union population/notifications.
- Modify: `tests/p4/test_files_view.py`: `FakeRouter.__init__`, new `.ready`, relevant request/status counters; replace `test_a_new_window_requeries_and_clears_the_previous_rows` with `test_a_new_window_requeries_and_preserves_previous_rows_until_settled`; append the six new unit tests below. Existing row/footer/sort/search assertions stay.

**Interfaces:**

- Consumes structural history `ready(window: Window) -> bool`, `file_source_status()`, `ranking(window: Window, metric: str)`, `request_file_view(app_id: int, window: Window, sort: FileSort, direction: Direction, search: str | None) -> Future[FileViewResult]`; existing FileViewResult remains a TYPE_CHECKING-only annotation in this module.
- Produces `set_transient_scope(transient: bool) -> None`; decorated `transient_scope(retire_on_exit: bool = False) -> ContextManager[None]` with generator annotation `Iterator[None]`; `set_scope_ready(ready: bool) -> None`; `set_updating_scope(window: Window | None) -> None`; `retire_deliveries() -> None`; `refresh_wanted() -> bool`; `flush_refresh() -> None`; `set_app_id(app_id: int, *, request: bool = True) -> None`.
- Preserves `set_window(window: Window) -> None`, `set_sort(sort: FileSort, direction: Direction | None = None) -> None`, `request_refresh() -> None`; produces `_handle_failure(reason: str | None, message: str) -> None` as the sole classification boundary for `_on_delivered(payload: tuple[int, Future[FileViewResult]])`, `_settle(generation: int, *, error: BaseException)`, and `_publish(result: FileViewResult)` refusals.

### Steps

- [ ] **Step 1: Add the admission/outcome unit battery**

Add `test_scope_admission_defers_all_entry_points_and_flushes_latest_key_once`, `test_transient_context_retires_only_invalidating_or_forced_exits`, `test_file_delivery_outcomes_preserve_or_clear_the_correct_state`, `test_retired_file_delivery_does_not_release_a_new_request`, `test_changed_query_key_retires_old_success_while_blocked`, and `test_valid_empty_file_success_replaces_rows_without_becoming_unavailable`.

Extend FakeRouter with `ready(self, window: Window) -> bool` and a configurable readiness boolean, `view_state`, controlled pending Futures, and status/ranking/request counters. Parameterise entry points (window, app, sort, request, search/direction/header) while blocked and prove they perform zero status/choice/query work, preserve the displayed table/footer, and leave one wanted refresh for the final key. Inspect header clickability as well as widget enablement.

Use this snapshot in preservation tests, including every existing footer line:

```python
def displayed_files(view):
    return (
        tuple(tuple(row) for row in texts(view)),
        view.last_result,
        tuple(label.text() for label in view._footer_labels()),
        view.table.verticalScrollBar().value(),
    )
```

Parameterise current failure with transient true/admission true, transient false/admission false, and both false; include failed Futures, synchronous request/status errors, invalid served interval and unavailable inner coverage. The first two preserve the snapshot and set wanted; terminal failure clears rows/last_result, uses all eight unavailable footer values, and does not retry. Closed/stale generations neither clear nor release a newer outstanding request. Include successful nonempty and valid empty deliveries.

- [ ] **Step 2: Observe failures and initialise state without changing measurements**

Run `python -m pytest tests/p4/test_files_view.py -k 'test_scope_admission or test_transient_context or test_file_delivery_outcomes or test_retired_file_delivery or test_changed_query_key or test_valid_empty_file_success or test_a_new_window_requeries'` before implementation.

Add `_transient=False`, `_scope_ready=True`, `_refresh_wanted=False`, `_outstanding=False`, `_updating_scope=None`, `_deferred_window=None`. Add child `files_updating` after the existing footer; never replace `_footer_labels`, FOOTER_NOTE, row formatting, counted/uncounted quantities, or served/requested-window disclosure.

- [ ] **Step 3: Implement conditional transient exits and guarded classification**

Use this context control, including nested previous-value restoration:

```python
@contextmanager
def transient_scope(self, retire_on_exit: bool = False) -> Iterator[None]:
    previous = self._transient
    self.set_transient_scope(True)
    try:
        yield
    finally:
        self.set_transient_scope(previous)
        if retire_on_exit or getattr(self._history, "view_state", "").startswith("unavailable"):
            self.retire_deliveries()
```

Validate bool flags. `retire_deliveries` always advances the generation; an outstanding delivery, including a Future already done but queued to Qt, sets wanted before clearing outstanding. Closed retirement schedules nothing. Preserve result/model/footer/key. `_handle_failure` tests exactly `self._transient or not self._scope_ready`, retaining measured state plus `updating to <scope>` and wanted only when true; otherwise call existing `_set_unavailable`. Classify by state, never by exception class or retirement message. Both delivery and synchronous settlement check closed/generation first and mark only the matching current request settled.

- [ ] **Step 4: Defer every entry point and give adoption/flush one request**

False admission disables selector/search/direction/refresh/header clicks and makes programmatic entry points return before source/choice/query reads. Validate/store app/sort/search changes, retire old outstanding keys before storing replacements, record deferred window/wanted, and retain old displayed data. `set_updating_scope` changes only the footer destination, not `_window`.

When ready, different-window adoption uses the canonical applied window, discards a stale deferred window, populates choices with signals blocked, and issues one request if a source/subject exists; consume wanted as that attempt starts. Equal-window adoption does not issue a row query merely due to adoption; Task 5 adds its mandatory choice refresh. `set_app_id(..., request=False)` synchronises and marks wanted, with same ID a no-op. `flush_refresh` does not consume wanted when closed/blocked/missing app/window/not ready; otherwise issues at most one request. A failed admitted attempt is terminal rather than an automatic retry loop.

A replacement request with a last_result keeps every existing footer quantity and row until valid settlement, showing updating instead of replacing quantities with loading. Initial requests keep exact `loading`/`loading file history`. A current old-scope success while blocked can replace only that still-displayed scope/matching key and cannot remove the pending destination. Request logs remain bounded at 64.

- [ ] **Step 5: Verify all Files unit tests**

Run `python -m pytest tests/p4/test_files_view.py` and `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/files_view.py`. Preserve exact unavailable diagnostics, NFC search, contract sorting, basename-only display, boot-scoped last-seen, measured-empty and served-interval tests. Do not modify the import-layer guard.

- [ ] **Step 6: Commit**

```bash
git add src/appio/ui/files_view.py tests/p4/test_files_view.py
git commit -m "fix(ui): preserve Files results across transient scope retirement"
```

## Task 5: Refresh the ranked and file-only application union on every applied frame

**Implementer tier:** Standard

**Lane/category:** Files / subject membership. **Depends on:** 4. **Spec:** §§3.4, 5.2–5.3, 7, 9.1.

**Files:**

- Modify: `src/appio/ui/files_view.py`, only `FilesView.__init__`, `set_window`, the replacement of `_load_ranking` by `_load_choices`, `_on_app_changed`, and `_render_no_applications`; add `subject_changed` beside the existing signal. Import edits are limited to types used by these methods.
- Modify: `tests/p4/test_files_view.py`, only FakeRouter union fixtures/local-registry seams, `test_app_selection_requests_the_selected_app_and_ranks_once_per_window`, `test_file_selector_uses_retained_file_apps_when_process_ranking_is_empty`, and the four new tests below.

**Interfaces:**

- Consumes Task 4 `set_scope_ready(ready: bool) -> None`, `retire_deliveries() -> None`, `refresh_wanted() -> bool`, `flush_refresh() -> None`, `set_app_id(app_id: int, *, request: bool = True) -> None`.
- Consumes existing structural `history.ranking(window: Window, metric: str)`, `history.local.file_applications(window: Window) -> Sequence[tuple[str, str, int]]`, and `history.registry.id_for(key: str) -> int` when local/registry exist; no concrete imports.
- Produces `_load_choices(self) -> None`, `subject_changed = pyqtSignal(object)` emitting only positive int or None; `set_window(window: Window) -> None` refreshes choices once per admitted applied preparation, including equal Window values, without an extra row query.

### Steps

- [ ] **Step 1: Add union and same-window membership tests**

Add `test_choices_union_preserves_ranked_order_and_sorted_file_extras`, `test_same_window_adoption_refreshes_choices_without_a_table_query`, `test_vanished_subject_falls_back_with_one_deferred_query`, and `test_empty_choice_union_retires_subject_and_notifies_once`.

Use ranked A plus local file-only B, duplicate ranked/local IDs and duplicate file keys, name ties with differing UTF-8 keys, and stable registry IDs. Assert ranked order stays first, extras sort by `(name, key.encode("utf-8"))`, visibility equals `len(choices) > len(ranked)`, and labels preserve two spaces. Repeat the same Window with newly file-only B, newly ranked C, and disappearing A. A valid current subject remains selected with zero automatic table query solely for membership refresh. Vanished subject falls back ranked → file-only → None; at most one later query, and exactly one nullable notification on empty. A source/choice failure cannot masquerade as empty success.

- [ ] **Step 2: Observe the mixed-union and same-window cases fail**

Run `python -m pytest tests/p4/test_files_view.py -k 'test_choices_union or test_same_window_adoption or test_vanished_subject or test_empty_choice_union'` before replacing population.

- [ ] **Step 3: Replace the ranking-empty fallback with the union algorithm**

Always read ranked choices once, and, if both structural local/registry exist, read local file applications once and append deduplicated extras. Do not derive identity from FileRow names. Check admission before either operation and route errors through Task 4's `_handle_failure`.

Populate with signals blocked; retain a valid subject, otherwise use the documented fallback. Set `app_selector.setVisible(len(choices) > len(ranked))`; emit subject_changed only for a real subject change after population. `_on_app_changed` validates, updates/retire keys as required, emits the subject, and requests exactly once; host listeners never request again. Empty union retires outstanding work, clears subject/selection/table, retains the existing no-applications footer, and emits None once.

On every admitted `set_window`, run `_load_choices` once, including an equal-window replacement. Preserve the table during a nonempty refresh. Equal-window membership updates mark wanted only when subject fallback needs a replacement; they do not themselves issue a row request. Changed-window adoption still owns one query and consumes wanted.

- [ ] **Step 4: Verify all Files tests**

Run `python -m pytest tests/p4/test_files_view.py` and `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/files_view.py`. Keep “ranks once per applied window” assertions separate from sort/search actions, which never refresh choices.

- [ ] **Step 5: Commit**

```bash
git add src/appio/ui/files_view.py tests/p4/test_files_view.py
git commit -m "feat(ui): keep ranked and file-only subjects in one Files union"
```

## Task 6: Build the persistent two-tab console and diff measured frames

**Implementer tier:** Advanced

**Lane/category:** console / subject and rendering. **Depends on:** 1, 3, 5. **Spec:** §§3.3, 5.2–5.3, 6.4, 7–8, 9.4, structural and unchanged regressions in 9.6.

**Files:**

- Modify: `src/appio/ui/main_window.py:1-380`, specifically the UI-local `QueryService`, new `RankingDelegate`, `AppIoWindow.__init__`, `_build_ui`, `refresh`, `set_window`, `selected_app_id`, `_on_current_item_changed`, `_load_selected`, `_render_unavailable`, `_render_empty`, `_on_since_boot_clicked`, `_on_range_clicked`; replace `_render_share` with `_apply_overview`; add `_apply_ranking`, `_set_subject`, `_on_files_subject_changed`, `show_overview_status`, `has_frame`. Preserve `_scope_control` A22 behaviour and existing KPI/list object names.
- Modify: `tests/test_ui.py`, extending only the named `FakeQueryService`/`build` fixture for widget cases, adding the thirteen exact window tests below, and migrating `test_no_files_tab_tray_daemon_or_privileged_action`. Preserve Task 1/2 pure tests and existing residual/peak/window/A22 assertions.

**Interfaces:**

- Consumes UI protocol `ranking(window: Window, metric: str) -> Sequence[AppTotal]`, `disk_share(app_id: int, window: Window) -> DiskShare`, new `series(app_id: int, window: Window) -> Series`; no concrete query/history import.
- Consumes `build_ranking(rows: Sequence[AppTotal], *, unavailable: str | None = None) -> RankingModel`, `build_overview(app_id: int, window: Window, total: AppTotal | None, series: Series | None, share: DiskShare | None, *, ranking_error: str | None = None, series_error: str | None = None, share_error: str | None = None) -> OverviewModel`, and frozen fields from Task 1.
- Consumes chart constructors with `parent: QWidget | None = None`; `DualSeriesChart.set_task_points(points: Sequence[SeriesPoint]) -> None`, `set_totals(read_bytes: int, write_bytes: int) -> None`, `set_unavailable(reason: str) -> None`; `DeviceLane.set_device(dev: str) -> None`, `set_device_points(points: Sequence[DeviceSeriesPoint]) -> None`, plus the inherited totals/unavailable setters; `DonutChart.set_shares(read_bytes: int, write_bytes: int) -> None`, `set_unavailable(reason: str) -> None`; `ShareBars.set_rows(rows: Sequence[tuple[str, int]]) -> None`, `set_reconciliation(text: str | None) -> None`, `set_unavailable(reason: str) -> None`.
- Consumes Files `set_window(window: Window) -> None`, `set_app_id(app_id: int, *, request: bool = True) -> None`, `flush_refresh() -> None`, `set_scope_ready(ready: bool) -> None`, and `subject_changed = pyqtSignal(object)` whose payload is a positive int or None.
- Produces `AppIoWindow(query_service: QueryService, boot_id: str | None = None, end_mono_ns: int | None = None, *, files_view: FilesView | None = None)`; `refresh() -> None`, `set_window(window: Window) -> None`, `selected_app_id() -> int | None`, `_apply_ranking(model: RankingModel) -> None`, `_apply_overview(model: OverviewModel) -> None`, `_set_subject(app_id: int | None, *, request_files: bool) -> None`, `_on_files_subject_changed(app_id: int | None) -> None`, `_load_selected(app_id: int) -> None`, `show_overview_status(message: str, *, keep_frame: bool) -> None`, `has_frame: bool` read-only property.
- Produces `RankingDelegate(parent: QObject | None = None)`, `paint(painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None`, `sizeHint(option: QStyleOptionViewItem, index: QModelIndex) -> QSize`.
- Exposes one `detail_tabs: QTabWidget` named `detail_tabs`, one `files_view: FilesView`, and the Files message/settings-link widgets that Task 7 aliases. Main window owns `_subject_app_id`, `_ranking_model`, `_overview_model`, `_totals`, `_owns_files_scope`, `_active_window`, `_since_boot`; no second selected-ID field.

### Steps

- [ ] **Step 1: Add all thirteen required window tests**

Implement exactly these functions in `tests/test_ui.py`, retaining the existing test helper object-name accessors:

| Exact test | Required oracle |
| --- | --- |
| `test_identical_refresh_preserves_item_identity_and_current_row` | Capture actual QListWidgetItem objects; spy currentItemChanged, clear/reset and setCurrentRow; unchanged IDs/data cause none of those writes/signals. |
| `test_changed_shares_with_identical_labels_repaint_without_rebuild` | Leader total 1500 → 1530 rounds to the same label; subordinate shares change; store new UserRole+1 and repaint row rectangles without replacing items. Leader/zero stay 1.0/0.0. |
| `test_changed_order_restores_subject_and_scroll` | Reorder IDs, preserve selected app and scroll where range allows, no signal-feedback or duplicate query. |
| `test_detail_tabs_are_exactly_overview_then_files` | Exactly one two-tab host/FilesView, including constructor without boot/window; no query; disabled/safe initial scope handlers. |
| `test_two_tab_switches_preserve_subject_without_scope_or_query_calls` | Switch Overview→Files→Overview; selection/highlight and all set_window/prepare/file-query/helper/collector counters unchanged. |
| `test_files_selector_visibility_depends_on_union_extras` | Fully ranked choices hide selector; ranked A/file-only B show both. |
| `test_file_only_selection_clears_highlight_preserves_scroll_and_explains_overview` | Selecting B clears current index and selection, keeps scroll, gives five unavailable KPIs and `not in the process ranking`, no time shapes, one Files query. |
| `test_unavailable_series_is_not_rendered_as_zero` | Exact reason, no shapes/false zero, independently valid KPIs stay measured. |
| `test_ranking_error_marks_all_overview_dependencies_unavailable` | Five unavailable KPIs/all chart reasons, no false empty success. |
| `test_share_error_keeps_task_lane_and_marks_dependent_values_unavailable` | TOTAL/READ/WRITE/DEVICE unavailable; known PEAK/task lane survive; donut/share bars show share reason. |
| `test_series_failure_and_empty_series_are_distinct` | Error reason versus available no-samples text, no zero line, independent values preserved. |
| `test_device_unavailable_keeps_task_composition` | Task donut/lane survive; DEVICE/residual and device graphics unavailable with exact reason. |
| `test_empty_ranking_has_no_shapes_or_zero_kpis` | Empty active-window label correct; with empty process and file union, both subjects/highlight clear and late old-subject success is dropped. |

For the share-only repaint case, spy `viewport().update` rectangles, not only final rendered text. Identity tests must distinguish an idempotent diff from rebuilding identical objects/text.

- [ ] **Step 2: Establish failing cases, then build the empty host once**

Run `python -m pytest tests/test_ui.py -k 'test_identical_refresh or test_changed_shares or test_detail_tabs or test_two_tab_switches or test_file_only_selection'` before implementation.

Require boot/end both absent or both valid. Empty construction performs no query; `_active_window` and `_since_boot` begin None and Since boot stays disabled. Both-present standalone construction retains initial since-boot rendering. Create ranking/delegate, two tabs, five KPI labels, TOTAL, three reusable DeviceLane widgets, donut/share panel, reconciliation/coverage/detail/status labels, and Files/message/settings-link surface once. Reuse injected FilesView exactly once; otherwise own one. Keep unrelated styling untouched.

- [ ] **Step 3: Diff ranking values without needless selection writes**

Items store ID at UserRole and share at UserRole+1; the delegate paints styled selection/text plus a BAR_HEIGHT track/fill and reserves the bar row in sizeHint. Never parse labels for quantities.

For identical ID order, compare `(app_id, label, total_bytes, share)`, set changed text/share only, and repaint the row rectangle when its share changes. Do not clear/reset/rebuild or call setCurrentRow, even redundantly. For changed ID order, save subject/scroll, block signals, rebuild, restore the subject or clear both current index and selection, clamp only against a smaller new maximum, then restore the prior signal-block state.

- [ ] **Step 4: Make one subject and one Files query owner**

Validate `_set_subject` inputs and make unchanged subjects a no-op. Ranking selection uses `request_files=True`. Frame synchronization and Files selector notification use False; a Files selector action already owns its one request. File-only B remains the subject while its list highlight is cleared and Overview explains the missing ranking. Fallback is current valid → first ranked → first file-only → None. None clears Overview/highlight without calling positive-only set_app_id.

`refresh` ranks once for the active prepared window, then independently queries series/share at most once for a ranked subject. Avoid a second `_load_selected` if selecting the subject already loaded it. Catch errors independently with nonempty str(error) or class-name fallback. No file query on repeated process redraws. Structural query-only fakes get the ordinary explicit Files source-unavailable surface.

Embedded `set_window` renders process models and synchronises subject with request=False; it never adopts Files scope. Standalone set_window owns one Files adoption after subject choice and flushes only if adoption did not already query. No tab-index signal is connected to scope/query actions. Existing A22 guards run before queries; retain exact supplied window and safe None guards for scope buttons.

- [ ] **Step 5: Apply frozen Overview differences and preserve pending frames**

Equality-diff the model, changing only relevant labels/chart inputs. Rank/share/series dependency errors follow spec §8, including no fallback donut on share error. Device lanes use available raw-device share totals for gutters and at most three active lanes; show `and <n> more` only when positive. Keep every device share row; lifetime labels append ` [<boot_id>]`, with no chronological lifetime lanes. Use exact residual/coverage/detail text and a device-attribution reason distinct from the unavailable DEVICE value.

Empty and unavailable states have five unavailable KPI texts and no false measurements; empty detail remains `window: <kind>; no applications observed`. `has_frame` includes applied empty/error frames. `show_overview_status(..., keep_frame=True)` changes only status, never invokes chart unavailable setters or destroys data widgets. False retains tabs and shows the initial placeholder.

- [ ] **Step 6: Migrate the original standalone tab guard and verify**

Update `test_no_files_tab_tray_daemon_or_privileged_action` to assert one detail tab host with `Overview`, `Files`, one Files surface, and unchanged no-tray/no-daemon/no-privilege observations. Keep the old test's name as a migration locator. Preserve all existing signed residual, None/measured-zero peak, empty supported-range, A22 exact reason, and contract-only access tests.

Run `python -m pytest tests/test_ui.py tests/test_charts.py tests/p4/test_files_view.py` and `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/main_window.py src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/files_view.py`. Expect no failures/errors. The separate acceptance guard is migrated in Task 12.

- [ ] **Step 7: Commit**

```bash
git add src/appio/ui/main_window.py tests/test_ui.py
git commit -m "feat(ui): keep a persistent console and diff ranking frames"
```

## Task 7: Separate requested scopes from displayed frames in the shell

**Implementer tier:** Capable

**Lane/category:** shell / history state machine. **Depends on:** 6. **Spec:** §§3.5, 4.1–4.4, 5.2, 7–8, frame-related tests in 9.5.

**Files:**

- Modify: `src/appio/ui/collector_window.py:400-775`, specifically `CollectorWindow.__init__`, `_build_ui`, `refresh`'s history decision calls, `_sync_files`, `_files_status_text`, `open_files_unavailable`, `_poll_history`, `_show_ready`, `_ensure_view`, `_hide_view`, `view`, `history_visible`; replace `_render_history` with `_history_retired`, `_tick_target`, `show_window`, `apply_ready_frame`, `_apply_frame`, read-only `_prepared`; add frozen `PrepareRetry`. Preserve `_current_window`, helper/consent logic, and synchronous fact observation until Task 8 replaces that path.
- Modify: `tests/p3/test_collector_window.py`: imports; `FakeHistory.__init__`, `.prepare`, `.ready`, `.become_ready`, new `.series`; add `ControlledHistory`, `mixed_history`, and frame tests below. Do not migrate the old tests yet.

**Interfaces:**

- Consumes persistent `AppIoWindow(query_service: QueryService, boot_id: str | None = None, end_mono_ns: int | None = None, *, files_view: FilesView | None = None)`, `set_window(window: Window) -> None`, `show_overview_status(message: str, *, keep_frame: bool) -> None`, `has_frame: bool`, `selected_app_id() -> int | None`, `detail_tabs` and Files aliases; shell construction passes history and the one injected FilesView, leaving boot/end absent.
- Consumes Files `transient_scope(retire_on_exit: bool = False) -> ContextManager[None]`, `set_scope_ready(ready: bool) -> None`, `set_updating_scope(window: Window | None) -> None`, `retire_deliveries() -> None`, `set_app_id(app_id: int, *, request: bool = True) -> None`, `set_window(window: Window) -> None`, `refresh_wanted() -> bool`, `flush_refresh() -> None`.
- Produces `PrepareRetry(window: Window, owner: Literal["clock", "explicit"], since: int)` frozen with a nonnegative monotonic diagnostic timestamp; `_target`, `_displayed`, `_pending: Window | None`, `_invalidated: bool`, `_prepare_retry: PrepareRetry | None`, `_prepare_error: str | None`.
- Produces `_history_retired() -> bool`, `_tick_target() -> None`, `apply_ready_frame() -> bool`, `show_window(window: Window) -> None`, `_apply_frame() -> None`, `_prepared: Window | None` getter-only alias of displayed; `_sync_files(*, request: bool = False) -> None`; `_ensure_view(window: Window | None = None) -> AppIoWindow`; compatibility `open_files_unavailable() -> None` is tab selection only.
- Produces `ControlledHistory` with prepare call log, exact ready scope, view_state retirement, configurable post-loading failure, controlled file Futures; `become_ready(window: Window | None = None) -> None` remains compatible with old no-argument calls. `mixed_history` has ranked A/file-only B with registry ID mapping.

### Steps

- [ ] **Step 1: Add the eleven exact frame/host tests**

Add `test_replacement_keeps_frame_and_eventually_displays_new_scope`, `test_applied_scope_is_not_prepared_again_without_change_or_retirement`, `test_retired_pending_preparation_is_replaced_once`, `test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick`, `test_mixed_ranking_and_file_only_union_queries_file_only_subject`, `test_same_window_frame_refreshes_union_for_file_only_ranked_and_vanished_apps`, `test_files_compatibility_navigation_selects_tab_without_banner_button`, `test_no_target_restores_admission_without_hiding_tabs`, `test_files_source_failure_preserves_exact_message_and_link`, `test_explicit_show_window_driver_does_not_compute_clock_target`, `test_frame_apply_sets_displayed_before_files_sync`.

Use stopped timers and explicit controlled transitions for this task, so no asynchronous probe is required yet. Also include the retain-A/advance-target-to-B schedule below within the replacement test. The essential application oracle is:

```python
window.show_window(a)
assert window._pending == a
assert window._prepared is None
history.become_ready(a)
assert window.apply_ready_frame() is True
view = window.view
window.show_window(b)
assert window._prepared == a
assert window.view is view
assert window.history_visible is True
history.become_ready(b)
assert window.apply_ready_frame() is True
assert window._displayed == window._prepared == b
assert window._pending is None
assert window.apply_ready_frame() is False
```

Record widget/model identity, Files snapshot and exact query count as well as Window values. In the target-advance case, keep A pending while current target becomes B, display A first, and prepare B only on a subsequent target tick. Parameterise real retirement sources reconcile/tick_local/add_source/transport gap, using existing real-router fixtures for invalidation evidence and ControlledHistory for deterministic readiness.

For preparation failure, exercise all three real post-loading boundaries: source connection in_transaction, missing file-backed database path, and executor.submit raising. Assert local `preparation_error`, no pending scope/no fabricated readiness, retained frame/footer/admission closed. Repeat through show_window and check owner=explicit; clock/apply do not retry or consume it, including when target becomes None. A same-window explicit retry and a different-window superseding explicit request are distinct cases. Pre-loading validation remains its existing error.

- [ ] **Step 2: Observe frame tests fail and build one persistent shell host**

Run `python -m pytest tests/p3/test_collector_window.py -k 'test_replacement_keeps_frame or test_applied_scope_is_not or test_retired_pending_preparation or test_prepare_failure_after_loading or test_frame_apply_sets_displayed or test_no_target_restores'` before implementing.

Remove files_nav, the outer QStackedWidget, and independent history/files pages. Inject exactly one shell-owned FilesView into the empty AppIoWindow and attach it once. Alias `files_view`, `files_message`, `files_open_settings`, and `history_state` to its widgets, retaining existing Settings deep links/control names. `_ensure_view` never prepares/renders; `_hide_view` never hides the tabs or an already displayed frame. history_visible means an applied displayed frame.

- [ ] **Step 3: Implement the target/preparation/apply algorithm in spec order**

Copy spec §4.2's `_tick_target`, `show_window`, and `apply_ready_frame` ordering exactly, without event processing. Derive retirement from `history.view_state.startswith("unavailable")`, not an invalidation latch at each call site. `_tick_target` updates only the target when a nonretired pending scope exists; the readiness poll can apply that pending scope while old data remains visible. The 100 ms poll is active only while pending, independent of the 1 s target/probe timer.

Every prepare closes Files admission first and runs inside `transient_scope(retire_on_exit=True)`. Capture post-loading exceptions in PrepareRetry; a clock marker retries once only on the next later target tick, replacing its timestamp on another failure. An explicit marker is owned only by a subsequent show_window call, blocks clock retry even with no current target, and prevents apply_ready_frame from consuming it. A different explicit scope supersedes rather than retries the old one. Never treat router `loading` after an exception as success.

Implement the mandatory successful apply order literally:

```python
window = self._pending
if window is None:
    return
self._displayed = window
self._pending = None
self._invalidated = False
self.files_view.retire_deliveries()
self.files_view.set_scope_ready(True)
self._show_ready(window)
self.files_view.flush_refresh()
```

Clear update labels after adoption and stop the poll. Only construction/no-target reset and `_apply_frame` assign displayed; only `_apply_frame` calls `_show_ready`. `_prepared` has no setter/backing requested state. Initial loading text is `loading history (<view_state>)`; replacements use `updating (<view_state>)` inside Overview and retain the data.

- [ ] **Step 4: Synchronise Files once with the newly displayed scope**

`_show_ready` uses view.set_window and `_sync_files(request=False)` without changing displayed. `_sync_files` reads the prepared alias only when ready/not pending, passes the canonical displayed window to Files, and avoids a second request if adoption queried. Legacy request=True can request an unchanged scope once; navigation never uses it. Preserve table visibility during replacement; let its updating label describe the destination.

Before the first frame with target/pending, degraded source diagnostic/settings link outranks generic loading. With no scope, use `waiting for the first sample`; no-target clears clock retry/error state, restores admission and retains accessible tabs. File-only/membership refresh applies the same-window union contract from Task 5 with no extra table query. The explicit poll driver does not compute the clock target or process Qt events.

- [ ] **Step 5: Verify the new frame tests**

Run `python -m pytest tests/p3/test_collector_window.py -k 'test_replacement_keeps_frame or test_applied_scope_is_not or test_retired_pending_preparation or test_prepare_failure_after_loading or test_mixed_ranking_and_file_only_union or test_same_window_frame_refreshes_union or test_files_compatibility_navigation or test_no_target_restores or test_files_source_failure or test_explicit_show_window_driver or test_frame_apply_sets_displayed'`. Expect no failures. Legacy structural/probe sequencing tests are explicitly migrated in Task 10; do not weaken them to make this intermediate commit appear fully integrated.

- [ ] **Step 6: Commit**

```bash
git add src/appio/ui/collector_window.py tests/p3/test_collector_window.py
git commit -m "fix(ui): retain displayed history while replacement scopes prepare"
```

## Task 8: Move service probes off Qt and arbitrate setup generations

**Implementer tier:** Capable

**Lane/category:** shell / probe and helper concurrency. **Depends on:** 7. **Spec:** §§3.5, 4.1, 4.5–4.7, 7–8, probe tests in 9.1/9.5.

**Files:**

- Modify: `src/appio/ui/collector_window.py`, only imports, new `ProbePayload`/`_ProbeBridge`, `CollectorWindow.__init__`, `refresh`, new `_submit_probe`, `_probe_completed`, `_apply_facts`, `facts_applied`, `_start`, `_on_reply`, `_fail`, `_after_enable`, `_after_disable` and `_after_status` scheduling tails, and `shutdown`. Preserve Task 7's frame algorithm; Task 9 adds the quiesce/export invalidation wrappers. Changes to helper callbacks remove duplicate synchronous refresh/probe scheduling only, retaining their existing effects.
- Modify: `tests/p3/test_collector_window.py`, only imports/new `ControlledProbe`, `settle_probe`, controlled setup completion seams, and the ten new probe tests below. Do not rewrite the legacy test bodies until Task 10.

**Interfaces:**

- Consumes existing `setup.refresh() -> ServiceFacts`, `setup.invoke(verb: str) -> Future[SetupReply]`, ServiceFacts, SetupReply, coordinator `reconcile(facts)`/`tick_local()`, live history client state and Task 7 `_tick_target() -> None`, `apply_ready_frame() -> bool`.
- Produces frozen `ProbePayload(facts: ServiceFacts | None, error: str | None, ticket: int, generation: int)` with `__post_init__(self) -> None`; `_ProbeBridge.delivered = pyqtSignal(object)` connected with `Qt.ConnectionType.QueuedConnection`.
- Produces `_submit_probe(*, replace_pending: bool = False) -> bool`, `_probe_completed(ticket: int, generation: int, future: Future[ServiceFacts]) -> None`, `_apply_facts(payload: ProbePayload) -> None`, `facts_applied: int` getter-only property and idempotent `shutdown() -> None`.
- State: `_facts_applied=0`, `_dropped_facts=0`, `_probe_ticket=0`, `_setup_generation=0`, `_probe_inflight: Future[ServiceFacts] | None`, `_probe_successor: Future[ServiceFacts] | None`, one `_probe_executor`, one `_probe_bridge`; all acceptance/state writes on Qt, immutable captured ticket/generation to workers.
- Produces `settle_probe(qtbot: QtBot, window: CollectorWindow, *, after: int) -> None`; ControlledProbe uses Events, records worker IDs/submissions/running/logical successor/cancelled queued work and yields ServiceFacts or a controlled exception.

### Steps

- [ ] **Step 1: Add the exact probe/concurrency tests**

Add these exact functions: `test_slow_probe_does_not_block_pump_or_pending_frame_application`, `test_unchanged_facts_skip_reconcile_while_preparation_is_pending`, `test_probe_spanning_setup_is_dropped_and_completion_probe_is_applied`, `test_off_probe_during_enable_never_reacquires_local_writer`, `test_old_probe_ticket_cannot_overwrite_or_release_replacement_probe`, `test_probe_payload_rejects_malformed_combinations_and_counters`, `test_repeated_setup_stages_replace_one_queued_successor_while_probe_runs`, `test_helper_completion_categories_advance_generation_before_busy_clears_and_reprobe`, `test_close_during_probe_drops_all_late_effects`, `test_probe_error_keeps_last_banner_and_frame`.

The payload matrix rejects both/neither facts/error, wrong facts type, empty/whitespace/nonstring error, bool/zero/negative/noninteger ticket, bool/negative/noninteger generation; valid fact and error controls construct. The slow worker is held at least 300 ms, while measured refresh returns under 100 ms, client pump advances and a pending frame applies before release. Periodic refresh submits only once while occupied, including a completed Future awaiting queued delivery.

Hold an old OFF probe through enable/chained helper stages. Complete two stages while it runs; the first queued successor is cancelled and replaced with exactly one newest live successor. Observe cancelled queue entries separately and prove their callables never run. Deliver stale success/error before and after the newest result; only current ticket may clear a slot or change facts/banner/error/frame/ownership. Deliver OFF while busy and prove no local writer/lease reacquisition. Parameterise helper completion success, nonzero exit, Future exception, synchronous invoke exception; each must advance generation before busy clears, then schedule the same one-fresh-probe policy.

Close while the worker is held; assert shutdown returns without waiting, timers stop and all counters/widgets/ownership remain unchanged after releasing late success/error. Release every held worker in a test finally block. For expected stale/error deliveries use dropped/error observations rather than waiting for facts_applied.

- [ ] **Step 2: Observe missing asynchronous behaviour and add the shared helper**

Run `python -m pytest tests/p3/test_collector_window.py -k 'test_slow_probe or test_probe_payload_rejects or test_probe_spanning_setup or test_repeated_setup_stages or test_helper_completion_categories or test_close_during_probe'` before implementation.

```python
def settle_probe(qtbot, window, *, after: int) -> None:
    qtbot.waitUntil(lambda: window.facts_applied > after, timeout=2000)
```

Do not make this helper call refresh repeatedly: its caller owns submission, and the helper only pumps Qt until the requested accepted-facts edge. Use a suitable bounded timeout override within the specific slow/held-worker tests rather than sleeping on Qt.

- [ ] **Step 3: Add immutable payload validation and a queued completion bridge**

Initialise one `ThreadPoolExecutor(max_workers=1)` without a constructor probe. Workers run only setup.refresh, never live client pump/ownership/sample/Qt/SQLite. Capture ticket and setup generation in callback arguments, never a mutable latest-generation field. `_probe_completed` translates the settled Future to exactly one fact/error payload (nonempty exception text or class name), emits the explicitly queued bridge, and mutates no shell/widget acceptance state. Swallow only QObject-destruction emission errors after close.

`_submit_probe` returns False closed/busy or occupied without replacement; a done Future awaiting delivery still occupies the slot. Increment ticket for submission, stamp generation, release/show error on submission failure. For setup replacement allocate the newer ticket first, cancel/clear the old logical successor, and submit one successor behind a running old probe. Future.cancel does not remove executor work items; the requirement is one running callable plus at most one noncancelled successor. Periodic ticks submit neither while any current live slot remains. Never forcibly terminate the running probe.

- [ ] **Step 4: Implement acceptance and Qt-thread execution order**

Start `_apply_facts` with exactly this predicate and matching-ticket release discipline:

```python
if self._closed:
    return
if (
    payload.ticket != self._probe_ticket
    or payload.generation != self._setup_generation
    or self._busy
):
    self._dropped_facts += 1
    if payload.ticket == self._probe_ticket:
        self._probe_inflight = None
    return
```

A stale/busy payload increments once even if multiple conditions fail. A closed payload mutates no counter. A current error releases its own slot, shows `appio: <error>`, keeps prior facts/banner/frame and does not increment facts_applied.

For accepted facts, first promote a currently verified answering history connection over silent one-shot facts, preserving pending_activation; never promote an unverified retained cursor. Compute `keep_pending = (promoted_facts == self._facts and self._pending is not None)` before assigning facts. Inside default Files transient classification, reconcile unless keep_pending and always call tick_local; no verification/answering/loading-string extra condition is allowed. Only after successful coordinator work assign facts, increment accepted count, render banner/switch, tick target, apply ready frame, and flush deferred Files if ready/no frame applied. Coordinator exceptions retain the previous banner/frame/count and use the existing error surface.

`refresh` returns closed, pumps in default transient context, ticks target and applies pending readiness before attempting a new probe; if no frame applied but displayed scope is ready/no pending, flush once. Busy/inflight skips submission only, not pump/readiness. Catch operational exceptions without killing the timer. No sleep, system command, connect/recv, or synchronous setup.refresh remains in these Qt paths.

- [ ] **Step 5: Fence every helper edge and shut down without waiting**

`_start` increments setup generation immediately before setting busy/invoking each helper. Every completion category increments it before busy clears; synchronous invoke failure shares the completion epilogue rather than clearing busy early. Preserve helper body/choice/error/export effects; answering truth still comes from unprivileged facts. The common completion path submits exactly one `_submit_probe(replace_pending=True)` when live and no chained helper made it busy again. Remove redundant refresh/probe tails from `_fail`, `_after_enable`, `_after_disable`, `_after_status` so they cannot double-submit; preserve their messages and choice/import semantics.

`shutdown` sets closed first, stops both timers, closes dialogs, retires Files, calls executor.shutdown(wait=False, cancel_futures=True), then preserves coordinator/history close ordering. Ignore late payloads without applied/dropped count changes. Do not block/join a running probe on Qt.

- [ ] **Step 6: Verify probe and frame behaviour**

Run `python -m pytest tests/p3/test_collector_window.py -k 'test_slow_probe or test_unchanged_facts_skip or test_probe_spanning_setup or test_off_probe_during_enable or test_old_probe_ticket or test_probe_payload_rejects or test_repeated_setup_stages or test_helper_completion_categories or test_close_during_probe or test_probe_error_keeps or test_replacement_keeps_frame or test_frame_apply_sets_displayed or test_prepare_failure_after_loading'`; run `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/collector_window.py`. Expect no failures/errors. Existing immediate-fact assertions are migrated in Task 10, not deleted.

- [ ] **Step 7: Commit**

```bash
git add src/appio/ui/collector_window.py tests/p3/test_collector_window.py
git commit -m "fix(ui): apply service probes asynchronously with setup generation fences"
```

## Task 9: Prove Files preservation across every live invalidation schedule

**Implementer tier:** Capable

**Lane/category:** integration / Files cancellation races. **Depends on:** 8. **Spec:** §§3.5, 4.5, 5.4–5.5, required race tests in 9.5.

**Files:**

- Modify: `src/appio/ui/collector_window.py`, only `_quiesce_local` and `_after_disable` to wrap the existing reconcile/accept_export operations in default `files_view.transient_scope()`; preserve their return values, failure messages, and Task 8's completion-probe ownership. No change to the frame or probe algorithms in this task.
- Modify: `tests/p3/test_collector_window.py`, only new real-router cancellation fixtures and the seven named integration tests below. Existing frame/probe/legacy test bodies are outside this task's write scope.
- Read only: `src/appio/history_router.py:137-158,413-447,742-774`, `src/appio/collector_owner.py:150-252`; use existing tmp-path SQLite/router/export seams, never modify the producer/router to make a schedule easier.

**Interfaces:**

- Consumes `FilesView.transient_scope(retire_on_exit: bool = False) -> ContextManager[None]`, `set_scope_ready(ready: bool) -> None`, `retire_deliveries() -> None`, `refresh_wanted() -> bool`, `flush_refresh() -> None`; current-generation FileViewResult/Future contracts.
- Consumes shell `_tick_target() -> None`, `show_window(window: Window) -> None`, `apply_ready_frame() -> bool`, `_apply_facts(payload: ProbePayload) -> None`, `facts_applied: int`; coordinator reconcile/tick_local/accept_export operations and the real router's existing invalidation boundaries.
- Produces no new cross-module API. `_quiesce_local() -> bool` and `_after_disable(reply: SetupReply) -> None` retain their signatures and effects with transient classification.

### Steps

- [ ] **Step 1: Add exact race tests with controlled delivery barriers**

Add `test_pump_transport_gap_preserves_files_through_replacement`, `test_disable_export_import_preserves_files_through_add_source`, `test_worker_wins_cancellation_delivery_is_transient_while_pending`, `test_transient_scope_exit_retires_worker_delivery_before_next_target_tick`, `test_late_retired_file_delivery_after_ready_is_dropped_and_requeried_once`, `test_pending_scope_refuses_all_file_entry_points_then_flushes_once`, and `test_current_terminal_file_failure_clears_but_transient_failure_does_not`.

Use a real HistoryRouter invalidation path with tmp-path file-backed local history and a controlled Files Future/result bridge. Queue delivery explicitly through a capture/release seam around the existing bridge delivery handler, while retaining the production `_on_delivered` for release. Preserve rows, last_result identity, every footer quantity, sort/search/subject and scroll in snapshots; spy model.clear and request count.

- [ ] **Step 2: Drive all three required timing schedules and the healthy control**

Parameterise the SR-1 pre-next-tick schedule across pump→on_gap→record_transport_gap, fact-path reconcile, sampled tick_local, enable quiesce reconcile, disable accept_export/add_source, and both preparation callers. Settle the worker between router generation bump and cancel, queue delivery, return from the context and deliver while `_scope_ready` is still true before `_tick_target` runs. Assert default scopes retired because view_state is unavailable; prepare scopes forced retirement despite loading. Failure stays stale with wanted true and unchanged table/footer; the next ready application owns exactly one replacement request.

Also exercise (a) synchronous retirement inside the invalidating call, (b) worker delivery after transient exit while admission is false, and (c) late failure after replacement ready/application, where retire-before-ready ordering must protect a newer request. An ordinary pump/tick with no invalidation must call retire_deliveries zero times and issue no replacement, even with an outstanding healthy Future. This control prevents unconditional retirement from passing the race tests.

The admission test parameterises set_app_id, set_sort, set_window, request_refresh and ranking selection; no status/choice/query work while blocked; one flush of the latest key. Terminal-versus-transient test exercises each predicate term separately and neither, including invalid FileViewResult refusal and synchronous failure.

- [ ] **Step 3: Observe missing wrappers fail, then wrap quiesce/export**

Run `python -m pytest tests/p3/test_collector_window.py -k 'test_pump_transport_gap_preserves or test_disable_export_import_preserves or test_worker_wins_cancellation or test_transient_scope_exit_retires or test_late_retired_file_delivery or test_pending_scope_refuses or test_current_terminal_file_failure'` before applying the wrapper changes. If earlier tasks already satisfy a schedule, record its passing baseline and prove the relevant wrapper/ordering is observable with the isolated mutation below rather than inventing a red run.

Wrap only `_quiesce_local`'s `reconcile(QUIESCE_FACTS)` and `_after_disable`'s validated `accept_export` call in default transient_scope. Pump and fact-path scopes already exist from Task 8; prepare scopes already force retirement from Task 7. Do not classify all errors as transient or swallow terminal Files failures.

- [ ] **Step 4: Verify the schedules can detect their mechanisms**

In a disposable copy outside the active checkout, separately remove the quiesce wrapper, export wrapper, conditional exit retirement, prepare's force flag, and apply-time retire-before-ready ordering. Run the named corresponding test for each mutation and require its table/footer/query assertion to fail, not an import/collection error. Restore and confirm the same targeted test passes. Also mutate conditional exit retirement to unconditional and require the healthy-tick control to fail. Do not mutate active source or count unrelated failures as evidence.

- [ ] **Step 5: Verify Files and the targeted shell battery**

Run `python -m pytest tests/p4/test_files_view.py` and the exact Task 9 targeted shell command from Step 3. Run `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/files_view.py src/appio/ui/collector_window.py`. Record barrier order, killing test/assertion, and restored controls in the controller report.

- [ ] **Step 6: Commit**

```bash
git add src/appio/ui/collector_window.py tests/p3/test_collector_window.py
git commit -m "test(ui): pin Files preservation across live history retirement"
```

## Task 10: Migrate every existing collector-window test to applied-fact and frame semantics

**Implementer tier:** Advanced

**Lane/category:** regression migration / shell tests. **Depends on:** 9. **Spec:** §9.6 (design §7.5), with §§4.2–4.6 semantics.

**Files:**

- Modify: `tests/p3/test_collector_window.py` only the existing test functions named in the checklist below, `build`, and `FakeSetup.__init__`, `.refresh`, `.invoke`, plus imports required for those adaptations. Preserve the new Task 7–9 tests, ControlledHistory/ControlledProbe and settle_probe except to route existing build callers to those established fixtures. No production file changes.

**Interfaces:**

- Consumes `settle_probe(qtbot: QtBot, window: CollectorWindow, *, after: int) -> None`, `window.facts_applied: int`, `_pending`/`_target` requested scopes, getter-only `_prepared == _displayed`, `show_window(window: Window) -> None`, `apply_ready_frame() -> bool`, `detail_tabs` and the preserved Settings/helper APIs.
- Preserves `build(qtbot, *, facts=OFF, ready=False, setup=None, events=None)` positional/keyword call compatibility. Existing FakeSetup.refresh is now called on a worker; keep worker observations separate from Qt mutation and protect any shared control seam with Events or a lock rather than timing assumptions.
- Produces no production API. Legacy tests retain their substantive safety/accounting/liveness assertions and use applied-fact edges instead of “refresh returned.”

### Steps

- [ ] **Step 1: Add event-driven sequencing at the named assertions**

At each submission capture `after = window.facts_applied`, invoke the action, then `settle_probe(qtbot, window, after=after)` before fact-dependent assertions. Helper tests wait for the post-completion fresh probe, not just busy false; use ControlledProbe where proving an intermediate busy/stale edge is necessary. Dropped/error paths wait for the corresponding observation, not an accepted count. Do not add wall-clock sleeps or relax helper call lists.

Migrate every function in this checklist:

| Existing function in `tests/p3/test_collector_window.py` | Observations that must survive |
| --- | --- |
| `test_verified_status_round_trip_clears_silent_header_while_window_open` | Initial SILENT, verified banner/Settings recovery, no helper; disconnect clears verification. |
| `test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF` | Three distinct refresh/submission/application cycles, answering truth and per-cycle probe/tick counts. |
| `test_label_recovers_after_silent_refresh_and_connection_reverification` | Each silent/reverified probe count and rendered fact transition. |
| `test_current_link_status_requires_verification_and_preserves_activation` | Verified current connection only; activation preservation. |
| `test_the_four_honest_states_render_distinctly` | Exact distinct banner/switch states after facts. |
| `test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers` | Fresh GUI probe controls answering, never helper body. |
| `test_an_unknown_probe_disables_the_switch_instead_of_claiming_off` | Unknown disables rather than claiming OFF. |
| `test_pending_activation_stays_visible_across_a_restart` | Pending activation remains visible after the fresh probe. |
| `test_periodic_refresh_never_invokes_the_helper` | Three separately applied probes; `setup.calls == []`. |
| `test_the_switch_never_shows_the_click_before_the_probe` | Initial probed switch, consent shown, no elevation on click alone. |
| `test_consent_lists_the_exact_commands_before_any_elevation` | Exact commands and disclosure before invocation. |
| `test_cancelled_consent_changes_nothing` | No new calls/actions, restored probed switch. |
| `test_a_failed_dismissal_reverts_the_switch_and_shows_the_exit_code` | Post-completion facts, exit code, retryability. |
| `test_a_dismissal_after_a_previous_success_reverts_to_the_probed_on_state` | Each stable ON probe and dismissal result. |
| `test_a_container_host_reason_disables_the_switch_and_is_shown_verbatim` | Exact unsupported-host reason and disabled switch. |
| `test_a_missing_helper_reports_one_hundred_twenty_seven` | Exit 127, unchanged exact message, disabled switch. |
| `test_enable_quiesces_local_collection_before_the_first_elevation` | Initial facts and quiesce-before-invoke order; spanning probe separately pinned by Task 8. |
| `test_a_failed_quiesce_aborts_the_elevation` | No helper invocation, error, not busy. |
| `test_restart_choice_warns_and_needs_a_distinct_confirmation` | Exact invocation sequence, warning, separate confirmation. |
| `test_reboot_choice_never_runs_restart_or_reboot` | Permitted helper calls only and async activation state; no host transition. |
| `test_a_dismissed_choice_leaves_the_switch_on_the_probe` | Invocation list and final probed switch. |
| `test_disable_requires_its_own_consent_and_imports_verified_metadata` | Consent/call, verified export identity, final OFF facts. |
| `test_disable_without_verified_export_metadata_imports_nothing` | No import, retained ON facts. |
| `test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict` | Status consent/call, fresh rendered verdict. |
| `test_a_pending_operation_rejects_a_second_one_across_a_qt_heartbeat` | Single invocation, busy/disabled state across an event turn. |
| `test_a_reply_completed_on_a_worker_thread_updates_only_on_the_qt_thread` | Initial facts, activation choice, reply thread IDs all Qt. |
| `test_a_refresh_failure_is_reported_instead_of_crashing_the_timer` | Coordinator exception rendered, prior facts/frame retained, callback alive. |

- [ ] **Step 2: Replace obsolete navigation structure assertions only**

Migrate these exact functions:

| Existing function | Replacement assertion; retained meaning |
| --- | --- |
| `test_the_switch_is_the_only_root_control_and_sits_inside_settings` | Remove blanket QTabWidget ban; keep exactly one root control in Settings. |
| `test_banner_and_files_deep_links_focus_settings_without_changing_state` | Files-tab identity instead of stack/page; keep deep links, no collector state change and pre-first-frame message. |
| `test_opening_files_shows_the_real_table_and_changes_only_query_state` | Select detail_tabs Files, no files_nav/stack identity; tab switching adds zero query. |
| `test_a_degraded_file_source_is_reported_while_history_is_still_loading` | Tab identity only; keep degraded message and hidden unavailable table. |
| `test_files_page_keeps_an_explicit_unavailable_message_and_settings_link` | Tab identity; keep exact diagnostic/unavailable text, no-query and Settings focus. |
| `test_the_files_surface_never_appears_inside_the_overview_window` | One Files surface exists inside shared console and is outside the Overview tab. |

Do not replace the table with a message-only stub to make structural assertions pass.

- [ ] **Step 3: Rewrite the pending-history behavioural test as three real schedules**

Replace `test_pending_history_yields_to_new_scope_ownership_or_invalidation` with three explicit tests named `test_pending_history_retains_a_until_applied_before_b`, `test_pending_history_invalidation_reprepares_once`, and `test_pending_history_changed_ownership_reprepares_once`. Retain the original ownership/reconcile/tick observations; the former “time/boot always forces second prepare” expectation is intentionally superseded.

Retain schedule: prepare A, advance target to B, prove only A is pending/one prepare; make A ready/apply, then tick/prepare B and apply B. Invalidation schedule: retire A, one replacement for current A, eventually apply. Ownership schedule: change facts, await the fact application, one replacement and original owner assertions. Do not conflate a target change with retirement.

- [ ] **Step 4: Migrate the real-router and liveness callers without weakening them**

For `test_history_is_prepared_before_rendering_and_stale_detail_is_hidden`, inspect pending for the initial request and displayed/prepared only after application; keep initial hidden data/loading and exact scope assertions. For `test_real_history_router_gates_rendering_until_ready`, likewise split requested versus displayed checks and drive the apply path.

For `test_window_keeps_in_flight_history_for_the_same_scope`, preserve `pumps > 1`, `probes > 1`, `ranking_settled > 0` and the final applied-frame assertion formerly near line 1379. Its test harness must process queued facts and call refresh/apply with the established ordering; no direct `_show_ready` or assignment to `_prepared`. Use at least two distinct accepted probes to make the liveness observation meaningful.

- [ ] **Step 5: Verify the entire collector-window regression module**

Run `python -m pytest tests/p3/test_collector_window.py`. Expect no failures. Inspect the diff against the checklist: no deleted ownership/export/helper/liveness assertion, no skip/xfail, no broad “eventually some state” replacement for an exact expected edge. Each old asynchronous assumption must now name the fact/frame it awaits.

- [ ] **Step 6: Commit**

```bash
git add tests/p3/test_collector_window.py
git commit -m "test(ui): migrate shell regressions to queued fact and frame application"
```

## Task 11: Migrate acceptance drivers and preserve one daemon owner thread

**Implementer tier:** Capable

**Lane/category:** acceptance / history and production-entry integration. **Depends on:** 10. **Spec:** §§4.2–4.7, all harness/caller entries in 9.6.

**Files:**

- Modify: `tests/p3/acceptance_harness.py:1-50,383-461,491-503,552-567`, only imports, `P3Harness.until`, `.open_gui` timer-driving setup, `.settle`, `.advance`, `.query`, `.render`; preserve `.pump`, root/write guards, SQL/exports/helper transport and fixture accounting.
- Modify: `tests/p3/test_p3_acceptance.py`, only driver/wait sites in `test_daemon_survives_gui_close_and_disable_exports_final_commits` and `test_fixture_tick_sqlite_socket_client_router_and_window_match`; keep result/ownership/export/accounting assertions.
- Modify: `tests/p3/test_p3_adversarial.py`, only `test_helper_denial_and_absence_preserve_gui_ownership` post-helper driving/waits; preserve failure injection/export tests.
- Modify: `tests/p3/test_history_overlap_entry.py`, only imports, nested `status_reader`, `run_event_loop`/`settle` driver and the daemon-step sites within `test_live_incomplete_reply_cannot_resurrect_export_overlap_in_production_entry`, plus its old auxiliary status executor lifecycle; no assertion deletions.

**Interfaces:**

- Produces exact `P3Harness.until(self, condition: Callable[[], bool], *, seconds: float = 5, process_events: bool = True) -> None`.
- Preserves `settle(self, gui=None)`, `advance(self, seconds: int)`, `query(self, window: Window, *, gui=None)`, `render(self, window: Window, *, gui=None)` call shapes. `query` returns exact ranking; render applies through the shell.
- Consumes `facts_applied: int`, `_target`, `_displayed`, getter-only `_prepared`, `refresh() -> None`, `show_window(window: Window) -> None`, `apply_ready_frame() -> bool`. No acceptance caller calls `_show_ready` or assigns `_prepared`.
- Consumes the real daemon/HistoryRpc/IodClient/production entry unchanged; probe worker only performs the blocking raw status read, and the owner thread in the test's main drive loop performs all daemon.step calls.

### Steps

- [ ] **Step 1: Update until without making it an implicit window driver**

Add the exact typed signature and QApplication import. Preserve transport pumping/deadline and use the event flag literally:

```python
def until(self, condition: Callable[[], bool], *, seconds: float = 5,
          process_events: bool = True) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "acceptance transport/history did not settle"
        self.pump(2)
        if process_events:
            QApplication.processEvents()
        time.sleep(0.001)
```

The existing short worker-yield remains confined to this test transport loop. until itself does not refresh a window or compute a target; production and Qt timer callbacks remain sleep-free.

Change query to `history.prepare(window)` followed by `until(lambda: history.ready(window), process_events=False)` and ranking read. It stays undriven and event-free: delivering a queued fact during a direct query could reconcile/retire that read. Do not “fix” query by calling window.refresh.

- [ ] **Step 2: Make settle and advance wait for an applied fact before a no-target decision**

Delete the captured history._window/scope-is-None early exit. Use this two-phase settle driver so the required new fact edge precedes the no-target decision:

```python
def settle(self, gui=None):
    gui = gui or self.gui
    after = gui.window.facts_applied

    def fresh_facts():
        gui.window.refresh()
        return gui.window.facts_applied > after

    self.until(fresh_facts)

    def current_frame():
        gui.window.refresh()
        return gui.window._target is None or (
            gui.window._pending is None
            and gui.window._prepare_retry is None
            and gui.window._displayed == gui.window._target
        )

    self.until(current_frame)
    gui.window._poll_timer.stop()
```

In advance, keep the existing per-two-second counter/clock/daemon/client work, then run settle(gui) for each open GUI after its refresh. Keep open_gui's long refresh interval and stopped automatic timers; the manual driver stops the poll again if a preparation armed it.

The two phases must remain observable in tests, especially the crashed-GUI case with no target. A previously non-None target or busy false is not evidence that fresh facts were applied. After helper denial/absence, capture facts_applied before the helper action and wait for its completion probe before checking coordinator._local, since helper completion merely submits the fresh probe.

- [ ] **Step 3: Replace render with the event-free explicit driver**

Implement the following path, retaining the exact requested window; each condition drives only readiness/application and uses the harness transport pump from until:

```python
gui = gui or self.gui
since = Window("since_boot", window.boot_id, end_mono_ns=window.end_mono_ns)
gui.window.show_window(since)

def applied_since():
    gui.window.apply_ready_frame()
    return gui.window._displayed == since

self.until(applied_since, process_events=False)
gui.window.show_window(window)

def applied_requested():
    gui.window.apply_ready_frame()
    return gui.window._displayed == window

self.until(applied_requested, process_events=False)
```

Delete direct `_show_ready`, view.set_window and the intervening query. No Qt events, fact delivery, refresh or `_tick_target` in these explicit waits; otherwise the clock could displace the supported range. Prove the resulting displayed/prepared scope is requested, not just that a standalone view label changed.

- [ ] **Step 4: Serialize real production-entry daemon work on its owner thread**

In the overlap integration test, the new setup probe already runs on its dedicated worker. `status_reader` must call the captured blocking `raw_status` there and perform no daemon ownership work. Remove its old inner status executor/wait loop that calls daemon.step; do not add a second daemon owner. Keep all daemon.start/step/stop/database-export operations on the owner thread used by the test's settle loop, including the former standalone step around line 113 and the live-ranking reply loop. Preserve the live client pump.

The settle loop must drive window.refresh and apply_ready_frame, process Qt events, step daemon and client on that owner thread, and wait for the required newly applied facts/frame instead of treating mere host visibility as ready. Record/assert owner thread IDs at each test-owned step wrapper and assert probe/status thread differs. Keep raw blocking status off Qt. Release/shut down all test workers before final daemon teardown.

- [ ] **Step 5: Preserve every caller's accounting and safety oracle**

Keep all spec §9.6 cited acceptance observations, including the former line sites 43–44, 66, 85–89, 110–114, 120, 127, 141, 163, 169, 175, 210; requested-state assertions use pending/target, displayed assertions use prepared alias. Keep daemon totals `(160, 80, 48, 32)` and `(200, 100, 60, 40)`, final cutoff `(BOOT, 1, 4, 18 * NS)`, export table inventories, disable command/no restart, and GUI-close/daemon persistence tests.

In the overlap test, retain production entry, no local collector reacquisition, verified export/two sources, overlap `[(BOOT, 10 * NS, 20 * NS)]`, locally filtered `(0, 0, 0, 1)` versus live remote `(100, 60, 40, 0)`, incomplete coverage, zero filtered series, and displayed measured `0 B` values. Do not replace the real socket/router with a fake. In the adversarial test, only sequence the local-ownership assertion after new facts; preserve cancellation/absence and no unwanted elevation semantics.

- [ ] **Step 6: Verify the acceptance migration**

Run `python -m pytest tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_history_overlap_entry.py tests/p3/test_collector_window.py`. Expect no failures. Confirm query/render waits are event-free, settle/advance process events and drive refresh, and no direct `_show_ready` caller remains outside its production `_apply_frame` path.

- [ ] **Step 7: Commit**

```bash
git add tests/p3/acceptance_harness.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_history_overlap_entry.py
git commit -m "test(ui): drive acceptance through applied facts and explicit frames"
```

## Task 12: Narrow the later-scope guard to permit only the approved tab surface

**Implementer tier:** Fast

**Lane/category:** acceptance / dependency boundary. **Depends on:** 6, 11. **Spec:** §§1.1, 3.3, 9.6 structural checklist, 10.

**Files:**

- Modify: `tests/acceptance/test_no_later_scope.py:10-30`, only the widgets import and `test_window_has_no_later_plan_widgets_actions_or_flows`. Leave `test_fresh_process_imports_and_executes_only_p1_p2_modules` and its complete subprocess import guard unchanged.

**Interfaces:**

- Consumes one named `detail_tabs: QTabWidget` with two pages/captions `Overview`, `Files` and one QWidget named `files_view` from Task 6.
- Produces no API. Permits the approved Files/tab widgets while preserving no tray/menu/actions/daemon/socket/BPF/polkit/privilege/setup controls and the fresh-process layer boundary.

### Steps

- [ ] **Step 1: Confirm the obsolete structural assertion fails**

Run `python -m pytest tests/acceptance/test_no_later_scope.py::test_window_has_no_later_plan_widgets_actions_or_flows`. Expected failure is the old tab ban or forbidden `files` token, not a process import or actual prohibited control.

- [ ] **Step 2: Apply this exact structural replacement**

Replace the blanket tab assertion and forbidden tuple with:

```python
tabs, = window.findChildren(QTabWidget)
assert tabs.objectName() == "detail_tabs"
assert tabs.count() == 2
assert [tabs.tabText(index) for index in range(tabs.count())] == ["Overview", "Files"]
files = window.findChildren(QWidget, "files_view")
assert len(files) == 1
assert files[0] is tabs.widget(1) or tabs.widget(1).isAncestorOf(files[0])
forbidden = ("daemon", "socket", "bpf", "polkit", "privilege", "setup", "tray")
```

Keep the existing QSystemTrayIcon/QMenu bans, objectName and button-text scans, and empty window.actions assertion verbatim. Remove only `files` from the shared forbidden tuple. Do not weaken the fresh-process guard or its forbidden import prefixes to make the new Files view import.

- [ ] **Step 3: Verify both boundary tests and P1/P2 acceptance**

Run `python -m pytest tests/acceptance/test_no_later_scope.py tests/acceptance/test_p1_p2.py`. Expect no failures, including the fresh subprocess. The new modules/FilesView must remain structural contract consumers; any imported producer is an implementation defect, not a reason to remove a guard.

- [ ] **Step 4: Commit**

```bash
git add tests/acceptance/test_no_later_scope.py
git commit -m "test(ui): allow the approved Files tabs in the scope boundary guard"
```

## Task 13: Run complete implementation gates and record reviewable evidence

**Implementer tier:** Standard

**Lane/category:** integration / verification. **Depends on:** 1–12. **Spec:** §9.7 and the complete §§2–10 inventory.

**Files:**

- Create: `docs/superpowers/reviews/2026-11-01-appio-console-charts-gates.md` only after collecting actual evidence.
- Read only: every source/test file in the file map, design/spec/mockups, pyproject.toml, and the implementation Git diff. No production/test/configuration edits in this task; return any failure with its exact owning task/file so the PM/controller can route the already authorised fix.

**Interfaces:**

- Consumes all Task 1–12 signatures, exact spec §§9.2–9.5 test inventories and §9.6 migration checklist.
- Produces a checked-in implementation gate receipt containing actual tool versions, baseline/head revisions, required test inventory, exact gate commands/results, visual observations, protected-boundary diff evidence and any unresolved failure. This is a project deliverable distinct from the controller's run-root report.
- No new runtime API; success requires both required commands and the inventory/boundary/visual checks below, not an “all clear” without evidence.

### Steps

- [ ] **Step 1: Activate and record the actual toolchain**

Activate or provision the environment with the exact commands in Global Constraints. Run `python -c 'import sys, importlib.metadata as m; print(sys.version); print({p: m.version(p) for p in ("PyQt6", "pytest", "pytest-qt", "mypy")})'` and `python -m mypy --version`. Record Python, PyQt6, pytest, pytest-qt and pinned mypy 2.4.1; record the implementation revision and base revision. No runtime-requirement change is allowed to install mypy. Use the same interpreter for tests and typechecking.

- [ ] **Step 2: Verify the exact inventory and migration closure before full gates**

Use Python AST on `tests/test_ui.py`, `tests/test_charts.py`, and `tests/p3/test_collector_window.py` to count each exact required top-level function from spec §§9.2–9.5 exactly once in its assigned file. Check real assertion-bearing bodies, not empty/name-only wrappers. Expected inventories come directly from the signed-off specification, not from the implementation being inspected. Ensure every §8 fixture/outcome and §5.5 timing schedule has a named test. Walk all §9.6 checklist rows against the final diff, including the separate production-entry status-thread item and post-helper adversarial ownership wait.

Read the final changes to the six UI files and confirm produced/consumed signatures match this plan/spec. Search `_show_ready` callers and `_prepared` writes: only `_apply_frame` may invoke the former; the latter is read-only. Check non-None displayed assignments occur only at successful apply. Confirm wrappers exist for pump, fact reconcile/sample, enable quiesce, disable import and both prepare callers, with conditional versus forced exit semantics. Confirm no healthy tick is retired and no tab switch drives work.

- [ ] **Step 3: Run the exact full-suite gate**

From the repository root with the activated implementation environment:

```bash
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
python -m pytest
```

Record command, exit code, test totals/skips and duration from the actual output. No new skip/xfail, import/collection failure, or targeted-only run substitutes for this full suite. Resolve implementation defects through their owning task allowlists before claiming the gate passes; retain any environmental failure as an explicit blocker, never recast it as a pass.

- [ ] **Step 4: Run the specification's exact typecheck gate**

```bash
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py
```

Record its exit/output and pinned checker version. Do not replace this command with compileall, editor diagnostics, a smaller file list, broad module ignores, or changes to runtime requirements.

- [ ] **Step 5: Inspect offscreen grabs and protected boundaries**

Use the chart smoke-test grabs and a controlled populated console fixture to inspect the approved arrangement: ranking inline bars, five KPIs, dual TOTAL, up to three raw-token device lanes with more count, task donut/percentages, every attributed share row, signed reconciliation/coverage and persistent two-tab navigation. Also inspect empty, unavailable and pending-replacement states. Record which fixtures/grabs were inspected and visible findings; store transient image files outside the checkout and do not alter approved mockups. Geometry/identity/race tests provide the nonvisual evidence.

Compare the final Git diff to the file map. Require unchanged producer/query/router/storage/schema/registry/helper-command/consent/packaging/runtime dependency files and unchanged excluded features. Review helper/consent text and command vectors against baseline. Record the exact evidence, not a speculative absence of risk. No host transition is part of either gate.

- [ ] **Step 6: Write the gate receipt from observed results**

Create the listed Markdown file with title, actual toolchain/revisions, the two exact command lines and observed results, inventory/checklist coverage, mutation/control evidence from Task 9, visual fixtures/findings, and protected-boundary diff results. Clearly identify any blocker; do not prefill successful output. Ensure the full suite and exact mypy gate are both successful before recording implementation-ready status. Return the controller's required task report separately under its assigned run root.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/reviews/2026-11-01-appio-console-charts-gates.md
git commit -m "docs(ui): record console charts implementation gates"
```
