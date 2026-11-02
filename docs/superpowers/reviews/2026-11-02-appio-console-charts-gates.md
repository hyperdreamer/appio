# appio console charts — Task 5 migration and gate receipt (observed)

**Provenance:** `run_id=pm-20261101-175829-4410c030`; `lane=none`; `attempt=1`; task 5 of
`docs/superpowers/plans/2026-11-02-appio-console-charts-replan.md`. Every statement below
was read from this worktree or from a command run in the recorded environment. No plan
Task 5 gate item remains deferred: the mutation sweep, the offscreen grab inspection and
the baseline-to-final protected-boundary diff were produced afterwards and are recorded
below with their observed results, including one observed visual limitation that is not
claimed as a pass. Items that cannot be produced are listed as deferred, not as passed.

**Status: UNBLOCKED — all reported gates green.** The one upstream production defect
(`src/appio/ui/collector_window.py`, owner Task 4) is fixed in
`fix(ui): keep helper failures visible across clock ticks` (parent `9d1484f`); the two
legacy denial assertions and the full suite now pass, and the six-file typecheck gate
still passes. Sections below record the pre-fix observation and the post-fix re-run
separately; historical exit codes are not rewritten. The three gate items this receipt
previously deferred are now produced in “Gates produced in this receipt”.

## Toolchain (observed)

```
$ python -c 'import sys, importlib.metadata as m; print(sys.version); print({p: m.version(p) for p in ("PyQt6","pytest","pytest-qt","mypy")})'
3.14.8 (main, Oct  2 2026, 10:23:31) [GCC 16.2.1 20260810]
{'PyQt6': '6.11.0', 'pytest': '9.1.1', 'pytest-qt': '4.5.0', 'mypy': '2.4.1'}
$ python -m mypy --version
mypy 2.4.1 (compiled: yes)
```

Environment for every command below:
`. /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate` with
`QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6`.

## Revisions and working tree

| Item | Value |
| --- | --- |
| Planning baseline | `0eb173eb28dcba81df33e9261dbaa25d778422c0` |
| HEAD at the first receipt | `53a2c4c5fab9723bf583838cce167286f835dbf5` (`fix(ui): close the shell deterministically and pin frame ownership`) |
| Parent revision for the deferred gates and review fixes | `9c5f0648aba336d1d6f07d16f877899f5834c1da` (`fix(ui): keep helper failures visible across clock ticks`) |
| Files changed by this task | `tests/p3/test_collector_window.py`, `tests/p3/acceptance_harness.py`, `tests/p3/test_p3_adversarial.py`, `tests/p3/test_history_overlap_entry.py`, `tests/acceptance/test_no_later_scope.py`, this file |
| Production/contract files changed by this task | none (`git status` lists only the files above) |

`tests/p3/test_p3_acceptance.py` needed no local edit: both named driver tests
(`test_daemon_survives_gui_close_and_disable_exports_final_commits`,
`test_fixture_tick_sqlite_socket_client_router_and_window_match`) drive
`open_gui`/`advance`/`query`/`render` only, and those are the migrated harness sites.
Contract/query/router/storage/schema/registry/helper-command sources in this task's diff
are byte-unchanged. The mutation sweep and the grab inspection ran on `9c5f064` plus the
uncommitted review fixes to `tests/p3/test_collector_window.py` (the only working-tree
change at that time); the disposable copies came from that same tree.

## Commands and results

### Targeted migrated modules — exit 1

```
$ python -m pytest -q tests/p3/test_collector_window.py tests/p3/test_p3_acceptance.py \
    tests/p3/test_p3_adversarial.py tests/p3/test_history_overlap_entry.py \
    tests/acceptance/test_no_later_scope.py tests/acceptance/test_p1_p2.py
...
FAILED tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership[126]
FAILED tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership[127]
2 failed, 225 passed in 31.71s
```

### Full suite — exit 1

```
$ python -m pytest -q
...
FAILED tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership[126]
FAILED tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership[127]
2 failed, 2637 passed, 2 skipped in 143.28s (0:02:23)
```

The same two parametrisations are the only failures; every other module, including the
whole of `tests/p3/test_collector_window.py` (170), `tests/p3/test_p3_acceptance.py` (10),
`tests/p3/test_history_overlap_entry.py` (1), `tests/acceptance/test_no_later_scope.py`
(2), `tests/test_ui.py`, `tests/test_charts.py` and `tests/p4/test_files_view.py`, passes.

### Six-file typecheck — exit 0

```
$ python -m mypy --follow-imports=skip --ignore-missing-imports \
    src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py \
    src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py
Success: no issues found in 6 source files
```

### Post-fix re-run (after the Task 4 fix) — exit 0

Same environment, same command text as the failing runs above; only
`src/appio/ui/collector_window.py::_tick_target` changed.

```
$ python -m pytest -q tests/p3/test_p3_adversarial.py
32 passed in 7.18s

$ python -m pytest -q tests/p3/test_collector_window.py
170 passed in 15.37s

$ python -m pytest -q
2639 passed, 2 skipped in 133.87s (0:02:13)
```

The six-file typecheck was re-run unchanged and still reports
`Success: no issues found in 6 source files` (exit 0).

## 59-test inventory against the signed specification

Names were parsed from the tables of `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md`
§§9.2–9.5 and compared with Python `ast` top-level function names in the assigned modules
(not with names derived from the implementation):

| Spec section | Module | Spec names | Missing | Not exactly once |
| --- | --- | --- | --- | --- |
| 9.2 pure model | `tests/test_ui.py` | 7 | none | none |
| 9.3 chart | `tests/test_charts.py` | 11 | none | none |
| 9.4 window | `tests/test_ui.py` | 13 | none | none |
| 9.5 shell | `tests/p3/test_collector_window.py` | 28 | none | none |
| **Total** | | **59** | | |

No name-only wrappers, skips or xfails were added by this task.

## Migration coverage

All assertions named in plan Task 5 / spec §9.6 are retained; only the driver timing
changed, because a probe now completes on the worker executor and is applied through the
queued bridge.

### `tests/p3/acceptance_harness.py`

- `until(condition, *, seconds=5, process_events=True)` gained the exact typed signature and
  the `QApplication` import. It keeps the deadline, `pump(2)` and the 0.001 s transport
  worker yield, drains Qt delivery only when `process_events=True`, and never refreshes a
  window or computes a target.
- `settle(gui=None)` now has two phases: capture `facts_applied`, refresh/pump/process
  events until a *new* fact is applied, then poll the applied frame until
  `target is None` or `pending is None and prepare_retry is None and displayed == target`,
  and stop the readiness poll. The captured `history._window` shortcut and its
  scope-is-`None` early exit are deleted. While a preparation is pending it consumes
  readiness (`apply_ready_frame`) instead of submitting another probe each turn, so the
  helper never retires the read it is waiting for; `refresh()` still drives the target
  when nothing is pending.
- `query` keeps direct `prepare`/readiness/ranking on the requested window and waits with
  `until(..., process_events=False)`; it refreshes nothing and applies no facts.
- `render` keeps the exact requested `Window`: `show_window(since)` then an event-free
  `apply_ready_frame` loop until `displayed == since`, then `show_window(requested)` and
  the same loop. Direct `_show_ready`, direct `view.set_window` and intervening queries are
  gone; neither loop ticks a target or processes Qt events.

### `tests/p3/test_collector_window.py`

- `build(...)` keeps its signature (`facts`, `ready`, `setup`, `events`; an optional
  `history=` seam), stops the periodic timer so driver sites own their probe counts, and
  `FakeSetup` protects its facts/counter behind a lock because `refresh()` runs on the
  worker. New helpers `refresh_and_settle` / `complete_and_settle` capture
  `facts_applied` before the action and wait for the applied fact after it.
- Behavioural migrations (observation unchanged, wait added): verified status round trip,
  three-refresh probe-only truth, label recovery, current-link verification, the four
  honest states, GUI-probe-over-helper-body, unknown probe, pending activation, the switch
  and consent family, failed dismissal (exit codes and retryability), restored ON state,
  container-host and missing-helper text, quiesce-before-elevation (now also proves a
  spanning probe is dropped, `facts_applied == 0`, `_dropped_facts == 1`), failed quiesce,
  restart choice and its second confirmation, reboot choice's allowed-call list, dismissed
  choice, disable export identity, disable without export metadata, explicit recheck
  verdict, three-refresh/no-helper accounting, one-operation-at-a-time, Qt-thread reply
  delivery, prepared-before-render, retired/invalidated preparation, no-boot-row wait, and
  the error-instead-of-crash timer callback (now also proves the callback survives and the
  prior frame/state are retained).
- Structural replacements: `files_page`/`stack` identity became `window.view.detail_tabs`
  Files-tab identity; the blanket `QTabWidget` ban became "exactly one `detail_tabs`
  host"; the Files surface assertion proves the single real surface sits in the Files tab
  and outside the Overview tab; tab navigation asserts **zero** additional file queries.
  `files_view.isVisible() is False` became the ratified Files contract for an
  unadopted/unavailable surface (`state == "idle"`, `rowCount() == 0`, `last_result is
  None`) so "no zero table" is still proved.
- `test_pending_history_yields_to_new_scope_ownership_or_invalidation` was replaced by the
  three named functions: `..._retains_a_until_applied_before_b` (A stays pending while the
  target advances, A is displayed, only a later tick prepares/applies B, for both time and
  boot changes), `..._invalidation_reprepares_once` (retired pending A, exactly one
  replacement, eventual application) and `..._changed_ownership_reprepares_once` (changed
  facts applied with the original prepared/target/reconcile/tick assertions). Both use
  `ControlledHistory` so readiness is per-`Window`.
- `test_window_keeps_in_flight_history_for_the_same_scope` keeps `pumps > 1`, `probes > 1`,
  `ranking_settled > 0` and its applied-frame ranking assertion, and now also waits for
  `facts_applied >= 2` so two distinct probes were accepted and applied.
- No test writes `_prepared` or calls `_show_ready`.

### `tests/p3/test_history_overlap_entry.py`

- The auxiliary `ThreadPoolExecutor`/`status_reader` loop is deleted. `status_reader()`
  now performs the captured blocking `raw_status()` read on the shell's probe worker and no
  daemon ownership work.
- `settle()` drives, on the test owner thread only, `daemon.step()`, the live client pump
  and Qt events, calls `window.refresh()` for target/preparation/readiness, and waits for a
  new applied fact plus a ready displayed frame that equals the target. A final
  `finish_probe()` waits for the worker callable to finish without delivering a fact that
  would reconcile and retire the just-asserted frame.
- The former standalone and live-ranking `daemon.step()` calls route through
  `step_daemon()`; every step records its thread id. The test asserts all owner steps share
  the Qt thread and that probe/status thread ids are disjoint from it.
- Real sockets, the live router, the production entry point and every ownership, export,
  overlap (`[(BOOT, 10*NS, 20*NS)]`), coverage (`(0,0,0,1)` filtered vs `(100,60,40,0)`
  live) and displayed `0 B` assertion are retained.

### `tests/acceptance/test_no_later_scope.py`

- Exactly one `QTabWidget` named `detail_tabs` with two tabs captioned `Overview`, `Files`;
  exactly one `files_view` widget, which is tab 1 or its descendant; `files` is the only
  name removed from the forbidden tuple; the tray/menu/daemon/socket/bpf/polkit/privilege/
  setup bans, object-name and button-text scans and the empty `window.actions()` assertion
  are unchanged. The fresh-process import guard is untouched.

### `tests/p3/test_p3_adversarial.py`

- `test_helper_denial_and_absence_preserve_gui_ownership` captures `facts_applied` before
  `_begin("enable")` and now waits for the post-helper facts to be applied before asserting
  the coordinator's local ownership and last banner. Denial exit code/message, absence
  (exit 127), no elevation, failure injection and the export tests are otherwise unchanged.

## Blocker (upstream, owner Task 4)

**Failing tests (the only two failures in the full suite):**

```
tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership[126]
tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership[127]
```

```
>           assert f"exit {status}" in gui.window.error_label.text()
E           AssertionError: assert 'exit 127' in ''
E            +  where '' = <built-in method text of QLabel ...>.text
tests/p3/test_p3_adversarial.py:408: AssertionError
```

**Cause.** `CollectorWindow._tick_target()` clears the whole error label after a successful
clock-tick preparation (`src/appio/ui/collector_window.py:743`):

```python
            self._prepare_retry = None
            self._prepare_error = None
            self._pending = prepare_window
            self._clear_error()          # line 743: unscoped clear
            self._show_pending_status()
```

Task 4 made the helper completion submit a fresh probe (§4.6). On a locally collecting GUI
the accepted facts run `coordinator.tick_local()` → `history.invalidate()`, so the very next
tick re-prepares the same target and line 743 wipes the helper-failure message that `_fail`
just wrote. The user never sees the denial/absence reason. Spec §4.2's normative
`_tick_target` pseudocode resets only `_prepare_retry`/`_prepare_error` and does not clear
the error label; the baseline cleared it only in `_begin`/`_on_reply`
(`git show 0187c64:src/appio/ui/collector_window.py`). The existing spec §8 rows for
"Probe raises" likewise require the error surface to be shown and the last state retained.

**Reproducing evidence (disposable copy outside the checkout).** A copy of this checkout
(`/tmp/appio-gates-probe4`, `__pycache__` purged) was run offscreen; the checkout itself was
never mutated:

| Run | Production in the disposable copy | Command | Result |
| --- | --- | --- | --- |
| 1 baseline | unchanged `_clear_error()` at line 743 | `pytest -q tests/p3/test_p3_adversarial.py::test_helper_denial_and_absence_preserve_gui_ownership` | **2 failed** in 1.10 s |
| 2 mutation | clear only when the shell owned the preparation error (`owned_prepare_error = self._prepare_error is not None` captured before the reset, `if owned_prepare_error: self._clear_error()`) | same | **2 passed** in 0.90 s |
| 2b control | same mutation | `pytest -q tests/p3/test_collector_window.py::test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick` | **18 passed** in 7.31 s |
| 3 restore | original line 743 restored | same as run 1 | **2 failed** in 0.66 s |

Barrier/order observed with a stack trace at `_clear_error`: `_fail` (helper exit 126/127)
sets the label → probe delivery `_apply_facts` → `_tick_target` → successful
`history.prepare` → `_clear_error`. The killing assertion is the `exit <code>` text; the
mechanism is line 743, not the test timing (the migrated test already waits for the applied
fact before asserting).

**Owning task:** Task 4 (`src/appio/ui/collector_window.py`, `_tick_target`). Task 5 may not
edit production, so no fix was made here. The scoped clear above keeps Task 4's own
`test_prepare_failure_after_loading_keeps_frame_closed_and_retries_on_later_tick` green,
which is the constraint the fix must satisfy.

**Resolution (observed).** The unscoped `_clear_error()` is gone from the successful-prepare
path. `_tick_target` now captures the preparation error it is about to resolve and clears
the label only when that error is still the text on screen:

```python
resolved_error = self._prepare_error
self._prepare_retry = None
self._prepare_error = None
self._pending = prepare_window
# Clear only the preparation error this tick resolved; a helper or
# probe failure that is still on screen must stay visible.
if resolved_error is not None and self.error_label.text() == resolved_error:
    self._clear_error()
```

This matches spec §4.2 (the normative `_tick_target` resets only `_prepare_retry` and
`_prepare_error`; the label is not cleared by a tick) and spec §8, where both the
"Preparation raises" and "Probe raises" rows keep the error surface shown while the last
frame is retained. Baseline `0187c64` likewise cleared the label only in `_begin` and
`_on_reply`. No other `_clear_error()` call site changed: the no-target clock-marker reset
inside `_tick_target` (`:691`), `show_window`'s explicit retry success (`:813`), `_begin`
(`:1045`) and `_on_reply` (`:1102`, `:1106`) keep their prior behaviour, as do the
clock-owned marker retry, its renewal and the no-target error reset.

## Review-finding corrections (observed)

### MR-1 — changed ownership prepares one replacement

`test_pending_history_changed_ownership_reprepares_once` now uses `controlled_build`, so it
owns a `ControlledCoordinator`. It stops both timers, applies the initial ON probe, then
sets `coordinator.invalidate_on = "reconcile"` before the second `refresh_and_settle`.
The changed-ownership reconcile therefore invalidates the router generation exactly as a
live sample or transport invalidation does, and spec §4.4 replaces the retained pending
preparation once. The case now asserts `len(history.prepare_calls) == 2`,
`len(history.actions) == 2` and `window._pending == original_window`, and still asserts
`history.prepared == original_window`, `len(coordinator.reconciles) == 2`,
`coordinator.ticks == 2`, `coordinator.reconciles[-1] == OFF` and the displayed OFF state.
The old `len(history.actions) == 1` wording was not simply restored: the plain
`FakeCoordinator.reconcile` never invalidates and §4.4 forbids a re-prepare there.

Falsification in `/tmp/mr12-probe` (a disposable copy): with the seam set and only the old
`len(history.actions) == 1` assertion restored, the selected test failed with
`AssertionError: assert 2 == 1` at that assertion (two `prepare` entries for the same
window). The corrected test passes, and `tests/p3/test_collector_window.py` reports 170
passed.

### MR-2 — quiesce ordering proven after initial facts land

`test_enable_quiesces_local_collection_before_the_first_elevation` now starts with an
unheld `ControlledSetup(OFF)`, applies the first probe, and asserts `facts_applied == 1`
and the OFF state before clearing `setup.started`, setting `hold = True` and taking the
spanning probe. The quiesce-before-invoke order, `decide_owner` verdict, exact invoke
event and spanning-probe drop assertions are unchanged; after release it waits for
`_dropped_facts == 1` and asserts `facts_applied == 1` (the spanning facts are dropped and
the initial applied fact stands) before completing the helper.

Falsification in the same disposable copy: removing the initial settle and keeping the
new assertion failed with `assert 0 == 1` on `window.facts_applied`, i.e. the old fixture
never applied initial facts. The corrected test passes.

## Gates produced in this receipt (observed)

### Race-mechanism mutation sweep in disposable copies

Setup, run once for the whole sweep:

```
$ rm -rf /tmp/appio-gates-sweep /tmp/appio-gates-sweep-pristine
$ rsync -a --exclude .git --exclude __pycache__ --exclude '*.pyc' <checkout>/ /tmp/appio-gates-sweep/
$ mkdir /tmp/appio-gates-sweep-pristine
$ cp /tmp/appio-gates-sweep/src/appio/ui/files_view.py \
     /tmp/appio-gates-sweep/src/appio/ui/collector_window.py /tmp/appio-gates-sweep-pristine/
$ cd /tmp/appio-gates-sweep
$ . /tmp/appio-console-charts-pm-20261101-175829-4410c030-venv/bin/activate
$ export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6
$ python -m pytest -q \
    tests/p3/test_collector_window.py::test_worker_wins_cancellation_delivery_is_transient_while_pending \
    tests/p3/test_collector_window.py::test_transient_scope_exit_retires_worker_delivery_before_next_target_tick \
    tests/p3/test_collector_window.py::test_late_retired_file_delivery_after_ready_is_dropped_and_requeried_once
17 passed in 5.76s
```

Each mutation below was applied only in `/tmp/appio-gates-sweep`; the checkout stayed
untouched. Every run collected and executed the named test (no import/collection error)
and failed at the listed assertion line. After each mutation both production files were
restored from `/tmp/appio-gates-sweep-pristine` and the same selector had to pass again.

Barrier/order observed: `HeldFileDelivery` replaces `HistoryRouter._read_local_files` with
a worker held on a `threading.Event` and captures only the real `_deliver` payload;
`win_cancellation` hooks `_cancel_file_requests` to release that worker, wait for the
captured bridge payload and then cancel while the invalidating call is still running.
Default-exit routes (`pump`, `reconcile`, `tick_local`, `quiesce`, `import`) run through
`invalidate_shell`; prepare routes run through `_tick_target`/`show_window`, which close
Files admission before `prepare()` and pass `retire_on_exit=True` because `prepare()`
leaves `view_state == "loading"`. The last test re-enters `set_scope_ready(True)` with a
pre-apply generation payload, which is exactly the ordering hazard.

| Mutation (disposable copy only) | Selector | Observed failure (killing assertion) |
| --- | --- | --- |
| Remove the `transient_scope()` wrapper around `_quiesce_local`'s `reconcile(QUIESCE_FACTS)` | `...[quiesce]` | `assert held.transient_at_settlement == [True]` → `[False]` |
| Remove the `transient_scope()` wrapper around `_after_disable`'s `accept_export` | `...[import]` | `assert held.transient_at_settlement == [True]` → `[False]` |
| Conditional exit retirement removed (`if retire_on_exit or self._history_view_unavailable():` → `if retire_on_exit:`) | `...test_transient_scope_exit_retires_worker_delivery_before_next_target_tick[reconcile]` | `assert shell.view._generation > held.generation` → `6 > 6` |
| Prepare force flag removed (`transient_scope(retire_on_exit=True)` → `transient_scope()` at both prepare callers) | `...[clock_prepare]` | `assert shell.view._generation > held.generation` → `5 > 5` |
| Retire-before-ready order swapped (`retire_deliveries()` moved after `set_scope_ready(True)`/`_show_ready`) | `test_late_retired_file_delivery_after_ready_is_dropped_and_requeried_once` | `assert files_snapshot(shell.view) == held.snapshot`; `last_result` became `None` while the retained result had rows |
| Exit retirement made unconditional (`if True:`) | `...test_transient_scope_exit_retires_worker_delivery_before_next_target_tick[healthy_pump]` | `assert shell.view._generation == held.generation` → `8 == 6` |

Restored controls: after both files were restored byte-for-byte
(`diff -q` against the pristine copies) the full 17-test selector passed again in 5.12 s.

### Offscreen grab inspection

Fixtures were generated offscreen by `/tmp/appio-grabs/make_grabs.py` against the same
tree (`QApplication` with `QT_QPA_PLATFORM=offscreen`); PNGs and the generator stayed
outside the checkout in `/tmp/appio-grabs/`. Chart smoke grabs are 320 x 170
(`SeriesChart`, `DualSeriesChart`, `DeviceLane`, `DonutChart`, `ShareBars`) and the console
grabs are 1180 px wide (`Overview`/`Files`); the wide `ShareBars` grab is 1100 x 240.

| Grab | Controlled fixture | Observed |
| --- | --- | --- |
| `charts_series.png` | 7-of-8 task buckets, hole at bucket 3 | `TOTAL · read above, write below`; blue above/orange below with a visible unfilled bucket span |
| `charts_dual.png` | same series plus totals 6 MiB/3 MiB | same caption and gap support; gutter `r 6 MiB · w 3 MiB` |
| `charts_lane.png` | device 8:0 points, hole at bucket 3 | lane caption `8:0` and the same visible gap |
| `charts_donut.png` | task shares 6 MiB read / 3 MiB write | `READ vs WRITE`, sectors, `6 MiB read`, `3 MiB write`, `66.7% / 33.3%` |
| `charts_bars.png` | rows 4/2/1/0.5 MiB | `ATTRIBUTED SHARE BY DISK`, four proportional bars, rows 8:0, 8:16, 8:32, 8:48 |
| `console_populated_overview.png` | 4 ranked apps (9/6/3/1.5 MiB); subject 4 MiB read + 2 MiB write; four device rows; series 7-of-8 buckets; share coverage incomplete, 2 gaps | ranking bars for all four apps; five KPIs `TOTAL 6 MiB`, `READ 4 MiB`, `WRITE 2 MiB`, `PEAK unavailable`, `DEVICE 6.3 MiB`; dual TOTAL with the visible gap; three device lanes `8:0`, `8:16`, `8:32` with raw-token captions and byte gutters `r 3 MiB · w 1 MiB`, `r 768 KiB · w 512 KiB`, `r 512 KiB · w 256 KiB`; `and 1 more`; donut `66.7% / 33.3%`; all four share rows; residual strip `Residual: attributed exceeds task accounting by 384 KiB - difference between accounting points`; coverage `coverage: incomplete; device attribution: available; tier: raw; gaps: 2; suspends: False`; both persistent tabs |
| `console_share_bars_wide.png` | same share model at 1100 px | full signed equation: `Residual: attributed exceeds task accounting by 384 KiB - difference between accounting points; 6.3 MiB attributed + (-384 KiB) residual = 6 MiB total` |
| `console_populated_files.png` | two retained file rows for the selected subject | seven ratified columns, both rows (`alpha-report.bin`, `alpha-cache.tmp`), footer quantities and both persistent tabs |
| `console_empty_overview.png` | available but zero task I/O and an empty series | KPIs `0 B` (device `0 B`, `PEAK unavailable`); `no samples in this window` and no zero line; donut `0 B read`, `0 B write`, `0.0% / 0.0%`, `no task I/O in this window`; no share rows; `Residual: 0 B`; `coverage: complete` |
| `console_unavailable_overview.png` | ranking raises `daemon is not answering on the socket` | five `unavailable` KPIs; all three chart panels say `unavailable: daemon is not answering on the socket`; `Residual: unavailable`; `coverage: unavailable`; no numeric zeroes |
| `console_pending_overview.png` | populated frame retained, `history_state` set to `updating (loading)`, Files admission closed | the full retained populated frame plus the explicit `updating (loading)` line above the KPIs |
| `console_pending_files.png` | same pending scope | retained rows/footer, disabled search/direction/refresh controls, `updating to since_boot` |

Observed visual limitation (recorded, not claimed as a pass): in the 1180 px console the
`ShareBars` reconciliation annotation is drawn as one painter text line and is horizontally
clipped at the panel edge (`difference b…`). The complete signed equation is visible in
the 1100 px standalone grab of the same model (table above), and the short signed residual
strip plus both coverage lines are fully readable at the console width. The chart smoke
fixtures set points without totals, so their `SeriesChart`/`DeviceLane` gutters read
`r 0 B · w 0 B`; the console lanes use the share totals and show the values above.

### Baseline-to-final protected-boundary diff

Commands, run in the checkout:

```
$ BASE=0eb173eb28dcba81df33e9261dbaa25d778422c0
$ git diff --name-status $BASE
A  docs/superpowers/plans/2026-11-02-appio-console-charts-replan.execution-graph.dot
A  docs/superpowers/plans/2026-11-02-appio-console-charts-replan.execution-graph.yaml
A  docs/superpowers/plans/2026-11-02-appio-console-charts-replan.md
A  docs/superpowers/reviews/2026-11-02-appio-console-charts-gates.md
A  src/appio/ui/charts.py
M  src/appio/ui/collector_window.py
M  src/appio/ui/files_view.py
M  src/appio/ui/main_window.py
A  src/appio/ui/render_model.py
A  src/appio/ui/theme.py
M  tests/acceptance/test_no_later_scope.py
M  tests/p3/acceptance_harness.py
M  tests/p3/test_collector_window.py
M  tests/p3/test_history_overlap_entry.py
M  tests/p3/test_p3_adversarial.py
M  tests/p4/test_files_view.py
A  tests/test_charts.py
M  tests/test_ui.py
$ git diff --quiet $BASE -- $(git ls-tree -r --name-only $BASE \
    | grep -v -E '^(src/appio/ui/(collector_window|files_view|main_window)\.py|src/appio/ui/(charts|render_model|theme)\.py|tests/|docs/)')
$ echo $?
0
```

The only changed implementation paths are the six UI files the spec §3/§9.7 inventory
lists; every other baseline path is byte-identical, including
`contracts.py`, `query.py`, `history_router.py`, `store.py`, `file_store.py`,
`schema.py`, `migrations.py`, `ipc_models.py`, `ipc_protocol.py`, `iodclient.py`,
`iod.py`, `iod_history.py`, `bpf_fileio.py`, `collector_owner.py`, `setup_client.py`,
`setup_boundary.py`, `setup_delegation.py`, `setup_files.py`, `setup_service.py`,
`file_runtime.py`, `activation.py`, `ownership.py`, `identity.py`, `pyproject.toml`,
`packaging/` and `appio_iod/` (the `git diff --quiet` gate above exits 0; the packaging
and runtime-dependency diff is 0 lines). `git status` during the sweep listed only the
uncommitted review fix in `tests/p3/test_collector_window.py`.

Helper/consent boundary checked against the same baseline by AST extraction:
`consent_text` is byte-identical, the exact `('_start', verb)` literal vector is identical
(`disable`, `enable`, `install-delegation`, `restart-user-manager`, `status`; nothing
added or removed), `CONSENT_COMMAND_PREFIX` is unchanged and `setup_client.py` is
byte-identical. The only consent-adjacent docstring change is the module docstring
describing the persistent two-tab console; no user-visible consent or command text moved.
A scan of added `src/appio/ui` lines found no hour-of-day intensity strip, autostart,
retention/cadence control, tray widget, or enabled launch/range control; the only `tray`
match is the existing docstring phrase `contains no tray, daemon or privilege flow`.

### Gate re-run after the review fixes

Run in the working tree that contained the two test corrections (uncommitted at run time):

```
$ python -m pytest -q
2639 passed, 2 skipped in 128.38s (0:02:08)
$ python -m pytest -q tests/p3/test_collector_window.py
170 passed in 17.11s
$ python -m mypy --follow-imports=skip --ignore-missing-imports \
    src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py \
    src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py
Success: no issues found in 6 source files
$ echo $?
0
```

## Verdict

- Six-file mypy gate: **PASS** (exit 0, re-run after the fix and after the review
  corrections).
- Full `python -m pytest`: **PASS** (exit 0, re-run after the fix and after the review
  corrections) — 2639 passed, 2 skipped. Both former failures
  (`test_helper_denial_and_absence_preserve_gui_ownership[126]` and `[127]`) pass, and
  `tests/p3/test_collector_window.py` still has 170 passing tests.
- Mutation sweep: **produced** — six separate mutations each killed by its named
  preservation/request-count assertion, with byte-restored controls passing again.
- Offscreen grabs: **produced and inspected** — populated, empty, unavailable, pending,
  populated Files, chart smoke and the wide share panel; one annotation-clipping limitation
  is recorded above and not claimed as a pass.
- Protected-boundary diff: **produced** — the non-UI implementation tree is byte-identical
  to `0eb173e`; only the six listed UI implementation files, this task's test sites and
  these docs changed, helper/consent text and argv are unchanged, no excluded feature was
  added, and there is no packaging/runtime dependency diff.
- The Task 4 defect recorded above is **fixed**; no plan Task 5 gate item remains deferred.
