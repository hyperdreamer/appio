# appio console: a scope row that only offers what it can do

Date: 2026-11-04
Status: revised after design review 1
Amends the consumer-state cells of A22 in `2026-10-21-app-io-monitor-spec.md` and one
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
| A22 mandates the disabled chip | `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md:1031,1035` | The dead control is a specification requirement, not drift |

### 1.2 `Range` is a refresh button for a scope nothing opens

| Cause | Evidence | Effect |
| --- | --- | --- |
| It re-applies the window already displayed | `src/appio/ui/main_window.py:1098-1107` calls `set_window(window)` with the same range window | It selects nothing; it re-runs the current scope |
| It is enabled while a range is displayed, and also before any window is applied | `src/appio/ui/main_window.py:610` enables it only for a range window, but construction disables only `Since boot` and `Launch` (`:401-402`), so it starts enabled in the empty console | It cannot get a reader *into* a range, and its one enabled-looking state has nothing to act on |
| No production path displays a range | `src/appio/ui/collector_window.py:1118` builds only `since_boot`; the one explicit-window seam `show_window()` at `:985` has no production caller | In the shipped app `Range` never becomes actionable |
| A picker was deliberately never built | `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md:299` | The chip was kept only to re-apply an existing range |

### 1.3 The cost of the two dead controls

The scope row and the sentence under it are the first things a reader sees. Two of
the three controls there cannot act, so the console's opening statement is "this
does not work", for a scope that cannot be requested. The limitation is real but
unreachable: `sample` stores `launch_id` (`src/appio/schema.py:47-71`), while
`sample_dev` (`:84-95`) and `file_sample` (`:125-140`) carry no launch dimension, so
per-launch device and file attribution genuinely does not exist.

## 2. Goals

1. No control is shown for a scope that cannot act at the moment it is shown, and no
   control is ever constructed for a scope that can never act.
2. `Launch` is no longer offered anywhere in the console.
3. `Range` is shown only while a supported range is displayed.
4. A22's refusal text survives, verbatim, at every surface a stale caller or a
   persisted selection can still reach.
5. No query, no frame, and no total changes behaviour; this is a control-surface
   change only.

## 3. Non-goals

- Storing per-launch device or file attribution, or enabling launch queries. The
  schema deliberately has no launch dimension on `sample_dev` or `file_sample`
  (`docs/superpowers/plans/2026-10-21-app-io-monitor-p1-p2.md:222`).
- Building a range picker. §1.2's last row remains in force.
- Clearing a previously rendered frame when the shell refuses a window. That is
  existing behaviour (`src/appio/ui/collector_window.py:922-934` leaves the view
  untouched) and this change neither widens nor narrows it.
- The `appio setup failed (exit 127): Error accessing /usr/bin/appio-setup` banner.
  That is a separate defect on a host without the packaged helper
  (`packaging/arch/PKGBUILD:46` installs it at exactly that path); it is out of scope
  and keeps its present behaviour.
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
the refusal text where a stale caller can reach it". §9 records the amendment.

The user's stated reason for A: a control that can never be selected carries no
decision value, while the refusal keeps working at the layer where it can actually
be hit.

## 5. Architecture

### 5.1 Two rules

> **Never-actionable scopes have no control.** `Launch` is removed from the console:
> it is never constructed, never styled, and never checked.
>
> **Sometimes-actionable scopes have one control, shown only while actionable.**
> `Range` keeps its widget and is shown only while a supported range is displayed.
> `Since boot` keeps its widget and is shown always, enabled once a since-boot window
> exists.

A scope is **actionable** when a click issues a request the layers will answer.
Re-querying the window already displayed counts: `Range`'s same-window re-apply is
actionable by this definition, which is why the widget persists rather than
disappearing per state.

### 5.2 The row's states

Every row is reached through `set_window` (`src/appio/ui/main_window.py:593`) or the
constructor's empty-console branch (`:383-385`). `Kind` values are
`WindowKind` (`src/appio/contracts.py:86`).

| State | `Since boot` | `Launch` | `Range` | Reason label under the row |
| --- | --- | --- | --- | --- |
| No window yet (empty console) | present, disabled, unchecked | absent | present, hidden, disabled, unchecked | none |
| `since_boot` | present, enabled, checked | absent | present, hidden, disabled, unchecked | none |
| supported `range` | present, enabled only if a since-boot window was supplied, unchecked | absent | present, visible, enabled, checked | none |
| `lifetime` | present, enabled only if a since-boot window was supplied, unchecked | absent | present, hidden | none |
| `range` longer than `MAX_RANGE_NS` | present, enabled only if a since-boot window was supplied, unchecked | absent | present, hidden | exact A22 range text, visible |
| `launch` (stale selection or direct call) | present, enabled only if a since-boot window was supplied, unchecked | absent | present, hidden | none; the detail pane renders the exact A22 launch text |

`Since boot`'s enabled state is the shipped one (`:597-600`): enabling happens only
when a `since_boot` window is supplied, and the embedded console
(`src/appio/ui/collector_window.py:630`) supplies none at construction. This change
does not alter that rule.

### 5.3 What is removed

In `src/appio/ui/main_window.py`:

- `_launch_control` construction, its `setEnabled(False)`, its accessible
  description and tooltip, and its membership in the scope-row loop
  (`:399`, `:402-409`);
- the `_launch_reason` label (`:414-417`);
- `_launch_control.setChecked(False)` in `set_window` (`:607`);
- the now-unused `A22_LAUNCH_REASON` import (`:45`).

`_scope_control` (`:556-560`) keeps building exactly two controls, `Since boot` and
`Range`. The Range widget is created once and hidden by state; it is never destroyed,
because `_on_range_clicked` (`:1098-1107`) dereferences it and
`tests/test_ui.py:1769` calls that handler on the empty console.

### 5.4 What is kept

- `A22_LAUNCH_REASON` in `src/appio/ui/formatting.py:12-14` and
  `src/appio/contracts.py:104`, and `window_disabled_reason()`
  (`src/appio/ui/formatting.py:78-89`), which is how `set_window` learns the reason.
- `_render_unavailable(reason)`: a launch window passed directly to `set_window`
  still renders the exact A22 text in `detail_state`, five `unavailable` KPIs,
  `unavailable` charts, and no rows.
- The whole query-layer refusal chain (`tier.py`, `ipc_models.py`,
  `history_router.py:341`, `iod.py:538`) and the five other A22 strings.

### 5.5 `Range`'s new lifetime

`set_window` currently computes `range_disabled = reason == A22_RANGE_REASON` and
enables the chip only for a range window (`:610`). Under §5.1 the rule becomes
visibility plus an explicit disabled state in the empty console:

```python
supported_range = reason is None and window.kind == "range"
over_long_range = reason == A22_RANGE_REASON
self._range_control.setVisible(supported_range)
self._range_control.setEnabled(supported_range)
self._range_control.setChecked(supported_range)
self._range_reason.setText(A22_RANGE_REASON if over_long_range else "")
self._range_reason.setVisible(over_long_range)
```

Construction gains the state the empty console never had — `_build_ui` disables the
Range control alongside `Since boot` (`:401`), so an unbuilt console offers nothing
clickable. An over-365-day range shows the exact reason and no control, because
re-applying it could only reproduce the refusal. `_on_range_clicked` keeps its
current body: its guard already unchecks and returns unless a supported range is
displayed, which is now the only state in which the control is visible.

## 6. Error handling

The exact A22 text is reachable from three surfaces. All three are unchanged by this
design; only the control that used to sit in front of them is gone.

| Surface | Path | Behaviour |
| --- | --- | --- |
| Query layer | `tier.py:28`, `ipc_models.py:410`, `history_router.py:341` | Raises `WindowUnavailableError` with the exact message and `reason` token before any lookup |
| Console shell | `collector_window.py:925-934` | `prepare()` refusal prints the exact `str(error)` in the shell's error line and re-raises; the previously rendered frame stays, as it does today |
| Detail pane | `main_window.py` `set_window` → `_render_unavailable` | A launch window passed directly renders the exact text in `detail_state` with five `unavailable` KPIs and no rows |

The detail-pane fallback's text is the visible content of a `QLabel`, which is what
assistive technology reads; no separate accessible description is added, because the
removed chip was the only element that carried one. Tests assert the label exists,
is visible, and equals the exact A22 string.

No new error state, label, or log line is added.

## 7. Test strategy

### 7.1 Rewritten in `tests/test_ui.py`

| Current test | New test | Pins |
| --- | --- | --- |
| `test_disabled_launch_control_uses_exact_a22_reason` (`:432`) | `test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text` | `findChild("scope_launch")` and `findChild("launch_reason")` are both `None`; `set_window(launch)` dispatches no ranking or share call, sets `detail_state` to `A22_LAUNCH_REASON`, `total_value` to `unavailable`, leaves the list empty, and the `detail_state` label is visible |
| `test_over_365_day_range_control_uses_exact_a22_reason` (`:461`) | `test_over_365_day_range_shows_the_reason_and_hides_the_control` | `scope_range` exists and `isVisible()` is `False`; `range_reason` is visible with the exact text; no query is dispatched |

### 7.2 Extended in `tests/test_ui.py`

- `test_empty_supported_range_labels_the_active_window` (`:409`) gains: after
  `set_window(range)`, `scope_range` is visible, enabled and checked; before it,
  hidden.
- New `test_default_console_offers_only_since_boot`: a default `AppIoWindow` has
  `scope_since_boot` disabled, no `scope_launch`, and `scope_range` hidden and
  disabled; `_on_since_boot_clicked()` and `_on_range_clicked()` remain safe
  (`tests/test_ui.py:1768-1769` already exercises both handlers).

### 7.3 Changed in `tests/acceptance/test_p1_p2.py`

`test_disabled_windows_raise_exact_a22_errors_and_disable_controls` (`:429`) becomes
`test_disabled_windows_raise_exact_a22_errors_and_offer_no_control`, and its
`control_name` parameter becomes `absent_control_name`. Every query-raises-exact-text
assertion and the detail/KPI assertions stay; the control assertions become "the
named control is absent" and "the exact message is rendered".

### 7.4 Unchanged

- `tests/p3/test_p3_acceptance.py:135` (`test_a22_exact_messages_in_client_router_and_window`)
  asserts the message in rendered label text and `total_value == "unavailable"`
  (`:149-150`); it passes unchanged.
- `tests/test_contracts.py:317-346`, `tests/test_query.py:72`,
  `tests/p3/test_history_router.py:257`, `tests/p4/test_files_view.py:761`,
  `tests/p4/test_files_tab_scope_text.py:72` exercise the query and formatting
  layers, which this change does not touch.
- `tests/test_ui.py:2423-2437` sets a `lifetime` window and reads share lanes; §5.2
  adds that row without changing any label it asserts.
- Every ranking, series, share, Files, and recovery test keeps its present
  expectations.

### 7.5 Gates

- `python -m pytest tests/test_ui.py tests/acceptance/test_p1_p2.py` passes.
- The full suite passes with the same count minus the two tests merged in §7.1 and
  plus the two added in §7.2.
- Mutation checks: re-adding a disabled `scope_launch` button fails
  `test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text`; making
  `scope_range` visible unconditionally fails the empty-console and over-long-range
  assertions; removing the `setVisible(False)` from the empty console fails
  `test_default_console_offers_only_since_boot`.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| A reader who never clicks anything loses the visible statement that per-launch device/file attribution does not exist | Medium, accepted | The exact text still appears the moment a launch window is rendered, which is the only time it describes something a reader asked for |
| A future feature re-adds a launch chip with the wrong state | Low | §5.1 states the rule and §7.1 pins its absence |
| `Range` becomes invisible in practice because nothing displays a range | None new | It is already unreachable; hiding it removes a control that claimed to be available |
| A22's reversal is read as licence to drop other refusal texts | Low | §9 amends two consumer-state cells and keeps every refusal string, reason token, and query-layer check byte-identical |
| Hiding rather than destroying `Range` leaves a hidden focusable widget | Low | A hidden widget does not take focus; §7.2 asserts `isVisible()`, and the empty-console test asserts `isEnabled()` |

## 9. Relationship to earlier designs

### 9.1 A22's consumer-state cells

`2026-10-21-app-io-monitor-spec.md:1031` carries the A22 prose, and the table at
`:1033-1038` carries two consumer-state cells — the launch row (`:1035`) and the
over-long-range row (`:1036`) — that require a disabled control.

Amended by the user's ruling: those two cells' consumer state becomes "no control
offered; the exact reason text remains available on the detail fallback". Every
store outcome in those cells — the exact exception type, reason token, and message —
is unchanged, as is the prose's prohibition on dispatching a disabled scope and on
silently selecting or labelling a wider window.

### 9.2 Charts-spec sentence amendment

`2026-11-01-appio-console-charts-spec.md:299` requires keeping "the existing A22
semantics of `_on_since_boot_clicked` and `_on_range_clicked`, adding only the None
guards required by empty construction", and states "the latter reapplies only an
existing supported range". `_on_range_clicked` keeps those semantics and gains no
new behaviour; only the control's visibility and construction-time enabled state
change, per §5.5.

### 9.3 What this document does not supersede

No decision of `2026-10-21-app-io-monitor-design.md`,
`2026-11-01-appio-console-charts-design.md`, or
`2026-11-03-silent-daemon-console-recovery-design.md` is replaced. The A06 launch
recording rules, the tier tables, the retention rules, and the recovery state machine
stand as ratified.
