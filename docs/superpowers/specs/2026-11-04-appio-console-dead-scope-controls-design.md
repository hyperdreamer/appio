# appio console: a scope row that only offers what it can do

Date: 2026-11-04
Status: draft for review
Amends one consumer clause of A22 in `2026-10-21-app-io-monitor-spec.md` and one
sentence of `2026-11-01-appio-console-charts-spec.md` §3.3; see §9.

## 1. Background

A user screenshot showed the console's scope row with `Since boot` selected and
`Launch` and `Range` greyed out, above a permanent full-width sentence:

> Launch window disabled: device and file activity are not stored per launch.

The user's question was "So Launch button is meaningless, right?". The answer is
yes, and the same is true of `Range` in that row.

### 1.1 `Launch` can never be selected

| Cause | Evidence | Effect |
| --- | --- | --- |
| The control is disabled at construction and re-enabled nowhere | `src/appio/ui/main_window.py:402`; only `_since_boot_control` and `_range_control` receive `clicked` connections (`:443-445`) | No state exists in which `Launch` is clickable |
| The query layer refuses every launch window regardless of stored rows | `src/appio/tier.py:28-29`; `src/appio/ipc_models.py:410-412` refuse before any identity or ID lookup | Even a direct API call cannot produce a launch result |
| A22 mandates the disabled chip | `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md:1031` | The dead control is a specification requirement, not drift |

### 1.2 `Range` is a refresh button for a scope nothing opens

| Cause | Evidence | Effect |
| --- | --- | --- |
| It re-applies the window already displayed | `src/appio/ui/main_window.py:1099-1107` calls `set_window(window)` with the same range window | It selects nothing; it re-runs the current scope |
| It is enabled only while a range is already displayed | `src/appio/ui/main_window.py:610` (`reason is None and window.kind == "range"`) | It cannot get a reader *into* a range |
| No production path displays a range | `src/appio/ui/collector_window.py:1118` builds only `since_boot`; the one explicit-window seam `show_window()` at `:985` has no production caller | In the shipped app `Range` is disabled forever |
| A picker was deliberately never built | `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md:299` | The chip was kept only to re-apply an existing range |

### 1.3 The cost of the two dead controls

The scope row and the sentence under it are the first things a reader sees. Two of
the three controls there cannot act, so the console's opening statement is "this
does not work", for a scope that cannot be requested. The limitation is real but
unreachable: `sample` stores `launch_id` (`src/appio/schema.py:47-71`), while
`sample_dev` (`:84-95`) and `file_sample` (`:125-140`) carry no launch dimension, so
per-launch device and file attribution genuinely does not exist.

## 2. Goals

1. The scope row offers exactly the scopes that are actionable in the current state.
2. `Launch` is no longer offered anywhere in the console.
3. `Range` appears only while a supported range is actually displayed.
4. A22's refusal text survives, verbatim, at every surface that a stale caller or a
   persisted selection can still reach.
5. No query, no frame, and no total changes behaviour; this is a control-surface
   change only.

## 3. Non-goals

- Storing per-launch device or file attribution, or enabling launch queries. The
  schema deliberately has no launch dimension on `sample_dev` or `file_sample`
  (`docs/superpowers/plans/2026-10-21-app-io-monitor-p1-p2.md:222`).
- Building a range picker. §1.2's last row remains in force.
- The `appio setup failed (exit 127): Error accessing /usr/bin/appio-setup` banner.
  That is a separate defect on a host without the packaged helper
  (`packaging/arch/PKGBUILD:46` installs it at exactly that path); it is out of scope
  for this change and keeps its present behaviour.
- Any daemon, wire, schema, retention, tier, or Files behaviour.

## 4. Decisions taken with the user

**The user chose option A — drop the dead controls** — after seeing three drawn
variants and then a direct A-vs-B comparison. The rejected B kept three chips and
moved the two reason strings behind a "Why are these disabled?" link.

The record of that choice is
[`assets/2026-11-04-scope-row-ab.png`](assets/2026-11-04-scope-row-ab.png)
(A above, B below).

The user also confirmed the consequence: **A22's consumer clause is reversed** from
"render a disabled chip with the reason visible" to "do not offer the scope; keep
the refusal text at the detail fallback". §9 records the amendment.

The user's stated reason for A: a control that can never be selected carries no
decision value, while the refusal keeps working at the layer where it can actually
be hit.

## 5. Architecture

### 5.1 One rule

> A scope control exists if and only if its scope is actionable in the current state.

A scope is actionable when a click would change what is displayed and the query
layer would answer it. `Since boot` is actionable from the first applied frame.
`Range` is actionable only while a supported range is already the displayed window,
where a click re-applies it. `Launch` is never actionable, so it is never
constructed.

### 5.2 The row's four states

| Displayed window | `Since boot` | `Launch` | `Range` | Reason label under the row |
| --- | --- | --- | --- | --- |
| `since_boot` (every shipped state) | present, enabled, checked | absent | absent | none |
| supported `range` (only via `show_window`) | present, enabled, unchecked | absent | present, enabled, checked | none |
| `range` longer than `MAX_RANGE_NS` | present, enabled, unchecked | absent | absent | exact A22 range text, visible |
| `launch` (stale selection or direct call) | present, enabled, unchecked | absent | absent | none; the detail pane renders the exact A22 launch text |

Every row is reached through the existing `set_window` path; no new driver, field,
or state is introduced.

### 5.3 What is removed

In `src/appio/ui/main_window.py`:

- `_launch_control` construction, its `setEnabled(False)`, its accessible
  description and tooltip, and its membership in the scope-row loop
  (`:399`, `:402-409`);
- the `_launch_reason` label (`:414-417`);
- `_launch_control.setChecked(False)` in `set_window` (`:607`);
- the now-unused `A22_LAUNCH_REASON` import (`:45`).

`_scope_control` (`:556-560`) stays; it still builds `Since boot` and `Range`.

### 5.4 What is kept

- `A22_LAUNCH_REASON` in `src/appio/ui/formatting.py:12-14` and
  `src/appio/contracts.py:104`, and `window_disabled_reason()`
  (`src/appio/ui/formatting.py:78-89`), which is how `set_window` learns the reason.
- `_render_unavailable(reason)`: a launch window still renders the exact A22 text in
  `detail_state`, five `unavailable` KPIs, `unavailable` charts, and no rows.
- The whole query-layer refusal chain (`tier.py`, `ipc_models.py`,
  `history_router.py:341`, `iod.py:538`) and the five other A22 strings.

### 5.5 `Range`'s new lifetime

`set_window` currently computes `range_disabled = reason == A22_RANGE_REASON` and
then enables the chip only when the displayed window is already a range
(`:610`). Under §5.1 the rule becomes visibility rather than enablement:

```python
supported_range = reason is None and window.kind == "range"
over_long_range = reason == A22_RANGE_REASON
self._range_control.setVisible(supported_range)
self._range_control.setEnabled(supported_range)
self._range_control.setChecked(supported_range)
self._range_reason.setText(A22_RANGE_REASON if over_long_range else "")
self._range_reason.setVisible(over_long_range)
```

An over-365-day range therefore shows the exact reason and no control, because
re-applying it could only reproduce the refusal. `_on_range_clicked` keeps its
current body: its guard already unchecks and returns unless a supported range is
displayed, which is now the only state in which the control can be clicked.

## 6. Error handling

| Situation | Behaviour |
| --- | --- |
| Persisted or direct `launch` window reaches `set_window` | No control exists to be selected or mislabelled; the detail pane and every KPI render `unavailable` with the exact A22 text; zero ranking, series, share, or file queries are dispatched |
| `range` beyond `MAX_RANGE_NS` | No control; the exact A22 range text is the only reason shown; zero queries |
| Fresh console before its first frame | `Since boot` only; no reason text; no query |
| Click on a visible, enabled `Range` | Re-applies the displayed range through the existing `set_window(window)` path |

No new error state, label, or log line is added.

## 7. Test strategy

### 7.1 Rewritten in `tests/test_ui.py`

| Current test | New test | Pins |
| --- | --- | --- |
| `test_disabled_launch_control_uses_exact_a22_reason` (`:432`) | `test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text` | `findChild("scope_launch")` and `findChild("launch_reason")` are both `None`; `set_window(launch)` dispatches no ranking or share call, sets `detail_state` to `A22_LAUNCH_REASON`, `total_value` to `unavailable`, and leaves the list empty |
| `test_over_365_day_range_control_uses_exact_a22_reason` (`:464`) | `test_over_365_day_range_shows_the_reason_and_offers_no_control` | `findChild("scope_range")` is `None`; `range_reason` is visible with the exact text; no query is dispatched |

### 7.2 Extended in `tests/test_ui.py`

- `test_empty_supported_range_labels_the_active_window` (`:409`) gains: after
  `set_window(range)`, `scope_range` exists, is visible, enabled and checked; on the
  default console it does not exist.
- New `test_default_console_offers_only_since_boot`: a default `AppIoWindow` has
  `scope_since_boot` and neither `scope_launch` nor `scope_range`.

### 7.3 Changed in `tests/acceptance/test_p1_p2.py`

`test_disabled_windows_raise_exact_a22_errors_and_disable_controls` (`:425-455`)
keeps every query-raises-exact-text assertion and the detail/KPI assertions, and
replaces the control assertions with: the named control is absent, and the exact
message is rendered. The `control_name` parameter becomes the expected absence.

### 7.4 Unchanged

- `tests/p3/test_p3_acceptance.py:129` asserts only rendered label text and
  `total_value`; it passes unchanged.
- `tests/test_contracts.py:317-346`, `tests/test_query.py:72`,
  `tests/p3/test_history_router.py:257`, `tests/p4/test_files_view.py:761`,
  `tests/p4/test_files_tab_scope_text.py:72` exercise the query and formatting
  layers, which this change does not touch.
- Every ranking, series, share, Files, and recovery test keeps its present
  expectations.

### 7.5 Gates

- `python -m pytest tests/test_ui.py tests/acceptance/test_p1_p2.py` passes.
- The full suite passes with the same count minus the two tests merged in §7.1 and
  plus the two added in §7.2.
- A mutation check: re-adding a disabled `scope_launch` button makes
  `test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text` fail, and
  hiding `scope_range` unconditionally makes the §7.2 range assertion fail.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| A reader who never clicks anything loses the visible statement that per-launch device/file attribution does not exist | Medium, accepted | The exact text still appears the moment a launch window is requested, which is the only time it describes something the reader asked for |
| A future feature re-adds a launch chip with the wrong state | Low | §5.1 is stated as a rule, and §7.1 pins its absence |
| `Range` becomes unreachable in practice because nothing displays a range | None new | It is already unreachable; hiding it removes a control that lied about being available |
| A22's reversal is read as licence to drop other refusal texts | Low | §9 amends one consumer clause and keeps every refusal string, reason token, and query-layer check byte-identical |

## 9. Relationship to earlier designs

### 9.1 Consumer-state clause amendment

`2026-10-21-app-io-monitor-spec.md:1031` (A22) currently reads: a consumer "MUST
render a disabled, unselected chip/control with that text visible as its reason
(also exposed to accessibility), MUST NOT dispatch it as an enabled choice, and
MUST NOT silently select or label a wider replacement window."

Amended by the user's ruling: for a scope that is **never** actionable, a consumer
MUST NOT offer a control for it, and MUST keep the exact refusal text at the detail
fallback. The prohibitions on dispatching a disabled scope and on silently selecting
or labelling a wider window are unchanged, and the query-layer table at `:1031` is
unchanged in full.

### 9.2 Charts-spec sentence amendment

`2026-11-01-appio-console-charts-spec.md:299` requires keeping "the existing A22
semantics of `_on_since_boot_clicked` and `_on_range_clicked`, adding only the None
guards required by empty construction", and states "the latter reapplies only an
existing supported range". `_on_range_clicked` keeps those semantics and gains no
new behaviour; only the control's visibility changes, per §5.5.

### 9.3 What this document does not supersede

No decision of `2026-10-21-app-io-monitor-design.md`, `2026-11-01-appio-console-charts-design.md`,
or `2026-11-03-silent-daemon-console-recovery-design.md` is replaced. The A06 launch
recording rules, the tier tables, the retention rules, and the recovery state machine
stand as ratified.
