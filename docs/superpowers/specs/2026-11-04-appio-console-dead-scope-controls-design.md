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
6. A refused window leaves no stale rows in either detail pane, so the refusal text
   is visible whichever tab the reader is looking at.

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
  `LAUNCH_WINDOW_MESSAGE` in `src/appio/contracts.py:103-105`, and
  `window_disabled_reason()`
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

### 6.1 A refused window clears the Files pane it owns

`_render_unavailable` (`src/appio/ui/main_window.py:1037-1053`) writes only
`_detail_state`, which lives in the Overview page (`:521-524`), and a refused
`set_window` returns at `:621` before `self._sync_owned_files()` (`:626`). With the
Files tab selected, a refused window would therefore leave the previous window's
file rows on screen — a numeric series under a refused label, which A22's "no stale
numeric series/totals" forbids. The always-visible chip sentence used to hide that
failure; removing it makes it reachable.

**Ownership.** Only the owner clears. `AppIoWindow` owns the Files scope when it was
constructed with boot arguments (`self._owns_files_scope`,
`src/appio/ui/main_window.py:360`), which is the standalone case. In the embedded
console the shell owns Files (`src/appio/ui/collector_window.py:732`
`_sync_files`), its own refusal path never reaches `set_window` (§6 row 2), and
`_render_files_message` (`:719-728`) already derives its caption from
`files_view.state` and `unavailable_text()`. No second owner is introduced.

**Standalone `set_window`.**

```python
if reason is not None:
    self._render_unavailable(reason)
    if self._owns_files_scope:
        self.files_view.refuse(reason)      # new public method
        self.files_message.setText(reason)
        self.files_message.setVisible(True)
    return
if self._owns_files_scope:
    # A supported adoption retires the refusal caption.
    self.files_message.setText("")
    self.files_message.setVisible(False)
self.refresh()
...
```

**`FilesView.refuse(reason)` contract.** It performs four duties in one call, in this
order, because each guards a different way old data could reappear:

1. `self.retire_deliveries()` (`src/appio/ui/files_view.py:590-596`) bumps the
generation, so a delivery already in flight cannot publish (`_on_delivered` drops a
stale generation at `:807`), and then `self._refresh_wanted = False`, because no
replacement is owed for a refused scope.
2. `self._window = None` and `self._deferred_window = None`. Without this a later
   adoption of the *same* window computes `changed = window != self._window` as false
   (`:638`) and issues no query, leaving Files permanently unavailable.
3. `self._app_id = None`, `self._choices_loaded = False`, and the application
   selector is cleared and hidden inside a `blockSignals` guard, because its items
   embed the previous scope's byte totals (`:966-974`).
4. `self._set_unavailable(None, reason)` (`:1094-1102`), which clears the model,
   resets every footer label to `unavailable`, and shows the state label.

The exact A22 text therefore appears in the Overview status line, the Files state
line, and `files_message`, whichever tab is selected, and no row, total, or selector
entry from a previous window survives.

This is a defect the change exposes rather than a new feature: the same stale rows
could appear before, hidden only by the chip's permanent sentence. The design records
it because goal 4 cannot be met otherwise.

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
- New `test_leaving_a_range_hides_the_control`: from a displayed supported range,
  `set_window` with `since_boot`, `lifetime`, an over-long range, and a launch window
  each leaves `scope_range` hidden, disabled and unchecked. This pins the transition
  direction, which entry-only assertions cannot catch.
- New `test_refused_window_clears_files_and_shows_the_reason`: with a standalone
  window, the Files tab selected (`tests/test_ui.py:1828` shows the
  `setCurrentIndex(1)` idiom), a populated table, a populated application selector,
  and one in-flight delivery, `set_window(launch)` leaves the table empty, the
  selector hidden and empty, every footer at `unavailable`,
  `FilesView.state == "unavailable"`, `files_message` visible with the exact A22
  text, and no query dispatched.
- New `test_late_delivery_after_refusal_never_republishes`: a delivery settled before
  the refusal, delivered after `refuse()`, changes no row and no footer.
- New `test_returning_to_the_refused_window_reloads_once`: supported W → launch → the
  same W issues exactly one new file query and restores rows, proving `refuse()`
  forgot the adopted scope (`src/appio/ui/files_view.py:638`).
- New `test_supported_adoption_clears_the_refusal_message`: refusal followed by a
  supported window leaves `files_message` hidden with empty text.

### 7.3 Changed in `tests/acceptance/test_p1_p2.py`

`test_disabled_windows_raise_exact_a22_errors_and_disable_controls` (`:429`) becomes
`test_disabled_windows_raise_exact_a22_errors_and_offer_no_control`, and its
`control_name` parameter becomes `control_state`. Every query-raises-exact-text
assertion and the detail/KPI assertions stay. The control assertion becomes one rule
per row, because the two scopes now differ:

| Parameter row | Control assertion |
| --- | --- |
| `launch` (`:426`) | `findChild(QPushButton, "scope_launch") is None` |
| over-long `range` (`:427`) | the `scope_range` widget exists, is hidden, disabled, and unchecked |

Both rows keep: the exact message rendered, `detail_state == message`, and
`total_value == "unavailable"`.

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
- The full suite's collected count: §7.1 rewrites two tests one-for-one, §7.2
  extends one existing test and adds six new tests, and §7.3 renames one. Record the
  observed before/after collected counts in the implementation report rather than
  relying on arithmetic.
- Mutation checks: re-adding a disabled `scope_launch` button fails
  `test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text`; making
  `scope_range` visible unconditionally fails the empty-console and over-long-range
  assertions; removing the `setVisible(False)` from the empty console fails
  `test_default_console_offers_only_since_boot`; keeping the range control visible
  after a later non-range window fails `test_leaving_a_range_hides_the_control`;
  dropping the `files_view.refuse(...)` call fails
  `test_refused_window_clears_files_and_shows_the_reason`.

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

Amended by the user's ruling: the requirement becomes "no control offered for a
scope that is never actionable; the exact reason text remains visible in the detail
view". The amendment touches exactly four places, and every store outcome — the
exception type, reason token, and message — stays byte-identical:

| Location | Current text | Amended text |
| --- | --- | --- |
| Prose (`:1031`) | "MUST render a disabled, unselected chip/control with that text visible as its reason (also exposed to accessibility)" | "MUST NOT offer a control for a scope that is never actionable; MUST keep the exact text visible in the detail view" |
| Launch cell (`:1035`) | "`Launch` control disabled and unselected; reason text exactly as the error message; queried detail state unavailable" | "No launch control; reason text exactly as the error message; queried detail state unavailable" |
| Range cell (`:1036`) | "That range control disabled and unselected; …" | "No control for an over-long range; …" |
| Decision table row (`:1063`) | "The requested control is disabled with the exact reason" | "No control is offered for the requested scope; the exact reason is shown and the scope is never widened" |

The prose's prohibitions on dispatching a disabled scope and on silently selecting or
labelling a wider window survive unchanged and now have nothing to dispatch.

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
`2026-11-03-silent-daemon-console-recovery-design.md` is replaced, with one
qualification: the earlier design's sentence requiring a "chip with a stated reason
(`launch_attribution`)" (`2026-10-21-app-io-monitor-design.md:392`) is superseded for
the launch scope only, by the same user ruling as §9.1. The A06 launch recording
rules, the tier tables, the retention rules, and the recovery state machine stand as
ratified.
