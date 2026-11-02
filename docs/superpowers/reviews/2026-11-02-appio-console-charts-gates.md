# appio console charts — Task 5 migration and gate receipt (observed)

**Provenance:** `run_id=pm-20261101-175829-4410c030`; `lane=none`; `attempt=1`; task 5 of
`docs/superpowers/plans/2026-11-02-appio-console-charts-replan.md`. Every statement below
was read from this worktree or from a command run in the recorded environment. Items that
could not be produced because a gate is red are listed as deferred, not as passed.

**Status: BLOCKED — not implementation-ready.** One upstream production defect
(`src/appio/ui/collector_window.py`, owner Task 4) keeps two legacy denial assertions red.
The six-file typecheck gate passes.

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
| HEAD at this receipt | `53a2c4c5fab9723bf583838cce167286f835dbf5` (`fix(ui): close the shell deterministically and pin frame ownership`) |
| Files changed by this task | `tests/p3/test_collector_window.py`, `tests/p3/acceptance_harness.py`, `tests/p3/test_p3_adversarial.py`, `tests/p3/test_history_overlap_entry.py`, `tests/acceptance/test_no_later_scope.py`, this file |
| Production/contract files changed by this task | none (`git status` lists only the files above) |

`tests/p3/test_p3_acceptance.py` needed no local edit: both named driver tests
(`test_daemon_survives_gui_close_and_disable_exports_final_commits`,
`test_fixture_tick_sqlite_socket_client_router_and_window_match`) drive
`open_gui`/`advance`/`query`/`render` only, and those are the migrated harness sites.
Contract/query/router/storage/schema/registry/helper-command sources in this task's diff
are byte-unchanged.

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

## Deferred because the gate is red

These plan Task 5 gate items were **not** produced; they are not claimed:

- Race-mechanism mutation sweep in disposable copies (quiesce wrapper, export wrapper,
  conditional exit retirement, prepare force flag, retire-before-ready ordering,
  unconditional-exit control) with restored controls.
- Offscreen grab inspection of populated/empty/unavailable/pending console and chart
  fixtures.
- Baseline-to-final protected-boundary diff review (only the working-tree check above was
  done: this task changed no production, configuration or contract file).

## Verdict

- Six-file mypy gate: **PASS** (exit 0).
- Full `python -m pytest`: **FAIL** (exit 1) — 2 failed, 2637 passed, 2 skipped, and both
  failures are the Task 4 defect recorded above.
- Task 5 is **blocked** on Task 4 remediation; this receipt must be reissued after that fix
  and a fresh full-suite run. Not implementation-ready.
