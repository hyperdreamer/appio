# appio console dead scope controls: technical specification

Date: 2026-11-04. Status: implementation contract for the approved design.
Scope: remove the console's never-actionable Launch offer, show Range only for a displayed supported range, and clear both detail surfaces on a direct window refusal. No query, stored frame, total, schema, daemon, wire, retention, tier, or recovery policy changes.

## 1. Authority, provenance, and boundaries

Behavioural authority: `docs/superpowers/specs/2026-11-04-appio-console-dead-scope-controls-design.md`, sections 1–9. This specification is subordinate to that approved design. It supplies the implementation contract without requiring an engineer to read the design first. Section 6 records the approved amendments to earlier specifications; section 8 isolates the small decisions needed to make the contract executable.

The drafting baseline is commit `1db25c5c1b251e428af037412a508fd563165f0b`. All source citations describe the working tree read at that baseline, before implementation. Tests below are required implementation checks, not reported results; no application tests were run for this drafting task.

The words **MUST**, **MUST NOT**, and **SHOULD** are normative. **R01–R30 are the 30 numbered normative requirements.** A requirement's referenced tables, ordered algorithms, and test vectors are part of that requirement. Historical wording quoted in the amendment table is not an additional requirement.

**R01 — Authority and change boundary.** The implementation MUST satisfy this document within the approved UI change. It MUST preserve the exact requested `Window`, every query-layer refusal, error reason token and message, and all sampling, storage and query results. It MUST NOT silently select, query, or label a wider replacement window. Refusing a window clears a presentation surface; it does not adopt a supported scope. The standalone/embedded Files ownership split remains in force.

Existing evidence and its implementation consequence:

| Verified source at the drafting baseline | Consequence |
| --- | --- |
| `src/appio/ui/main_window.py:399` constructs Launch; `src/appio/ui/main_window.py:402` disables it. Only Since boot and Range have click connections at `src/appio/ui/main_window.py:444` and `src/appio/ui/main_window.py:445`. | Remove Launch rather than inventing an action. |
| `src/appio/ui/main_window.py:1098` re-applies the current supported range; `src/appio/ui/collector_window.py:1118` constructs only since-boot targets. | Range remains a same-window re-query control; no picker or new production range entry path is introduced. |
| `src/appio/ui/main_window.py:621` returns on a disabled window before the owned Files synchronization at `src/appio/ui/main_window.py:626`. | A direct refusal needs explicit Files cleanup, regardless of Files owner. |
| `src/appio/ui/files_view.py:590` retires delivery generations; `src/appio/ui/files_view.py:807` rejects stale generations. Selector items built at `src/appio/ui/files_view.py:968` carry the previous scope's byte totals. | Refusal retires deliveries and clears scope, subject, selector, model and numeric footer state. |
| `src/appio/ui/collector_window.py:630` constructs the embedded console without boot arguments; `src/appio/ui/collector_window.py:730` hides Files before the first applied shell frame. | Direct embedded refusal still clears Files, and selects Overview so the exact reason is readable before any frame. |

## 2. Scope controls and state table

**R02 — The two offering rules.** A never-actionable scope MUST have no control: Launch is never constructed, styled, checked, or offered elsewhere in the console. A sometimes-actionable scope MUST have exactly one persistent control, shown only while actionable, with the sole retained Since boot placeholder interpretation recorded in decision D1. A scope is actionable when a click requests a window the layers can answer; a same-window re-query counts as actionable. Consequently Range remains one widget even though it opens no new range, and appears only for a supported displayed range.

For this control contract, a **supported range** is a well-formed range `Window` for which `window_disabled_reason(window) is None`. Its duration is at most `MAX_RANGE_NS`, including equality. A range exceeding that constant is refused. `MAX_RANGE_NS` is exactly `365 * 86400 * 1_000_000_000`, or `31536000000000000` ns (`src/appio/contracts.py:94`). This control eligibility predicate does not promise successful retained data or change operational-error handling.

**R03 — Complete scope-row state table.** Construction without boot arguments and every `set_window()` call MUST produce the following state. “Present” means the same constructed widget still exists; “hidden” means explicitly hidden, disabled, and unchecked. “Supplied” includes the standalone constructor's boot window or an earlier `set_window(since_boot)` call.

| State | `Since boot` | `Launch` | `Range` | Reason label under the row |
| --- | --- | --- | --- | --- |
| No window yet (empty console) | present, disabled, unchecked | absent | present, hidden, disabled, unchecked | none |
| `since_boot` | present, enabled, checked | absent | present, hidden, disabled, unchecked | none |
| supported `range` | present, enabled only if a since-boot window was supplied, unchecked | absent | present, visible, enabled, checked | none |
| `lifetime` | present, enabled only if a since-boot window was supplied, unchecked | absent | present, hidden, disabled, unchecked | none |
| `range` longer than `MAX_RANGE_NS` | present, enabled only if a since-boot window was supplied, unchecked | absent | present, hidden, disabled, unchecked | exact A22 range text, visible |
| `launch` (stale selection or direct call) | present, enabled only if a since-boot window was supplied, unchecked | absent | present, hidden, disabled, unchecked | none; the detail pane renders the exact A22 launch text |

**R04 — Since boot availability.** The implementation MUST keep the existing rule at `src/appio/ui/main_window.py:597`: only supplying a since-boot window establishes `_since_boot` and enables `scope_since_boot`. A later range, lifetime, or refused window MUST NOT invent a since-boot window or discard one already supplied. The embedded constructor supplies none. The standalone constructor with both boot arguments continues its initial since-boot render. Since boot is checked only for an unrefused active since-boot window.

## 3. Exact UI removals and Range lifecycle

**R05 — Exact Launch removal set.** In `src/appio/ui/main_window.py`, the implementation MUST remove every item in this table. The object names `scope_launch` and `launch_reason` MUST no longer exist anywhere in the constructed console's widget tree, and neither `_launch_control` nor `_launch_reason` remains as a window member. Removing only visibility or enabled state does not satisfy this requirement.

| Removal | Verified location |
| --- | --- |
| The `A22_LAUNCH_REASON` import from `appio.ui.formatting`, now unused in this module | `src/appio/ui/main_window.py:45` |
| `_launch_control = self._scope_control("Launch", "scope_launch")` | `src/appio/ui/main_window.py:399` |
| `_launch_control.setEnabled(False)` | `src/appio/ui/main_window.py:402` |
| `_launch_control.setAccessibleDescription(A22_LAUNCH_REASON)` | `src/appio/ui/main_window.py:403` |
| `_launch_control.setToolTip(A22_LAUNCH_REASON)` | `src/appio/ui/main_window.py:404` |
| `_launch_control` membership in the scope-row insertion loop | `src/appio/ui/main_window.py:407` |
| The entire `_launch_reason` label construction, object-name assignment, word-wrap setup and layout insertion | starts at `src/appio/ui/main_window.py:414`; object name at `src/appio/ui/main_window.py:415` |
| `_launch_control.setChecked(False)` in `set_window()` | `src/appio/ui/main_window.py:607` |

**R06 — Construction and widget identity.** `_scope_control(caption: str, object_name: str) -> QPushButton` at `src/appio/ui/main_window.py:556` MUST keep its current behavior and construct exactly two scope controls: `Since boot` / `scope_since_boot` and `Range` / `scope_range`. Range MUST be constructed once and immediately initialized hidden, disabled and unchecked in `_build_ui()`, alongside the existing disabled Since boot initialization at `src/appio/ui/main_window.py:401`. Hiding Range only on the first `set_window()` call is insufficient because the empty console receives no such call. Later state transitions MUST reuse the Range widget, never destroy or replace it; direct calls to its handler remain valid in an empty console.

**R07 — Range state calculation.** Every `AppIoWindow.set_window()` call MUST apply the following state calculation before the refusal/supported branches. It replaces the current visibility-independent enabling rule at `src/appio/ui/main_window.py:610`:

```python
supported_range = reason is None and window.kind == "range"
over_long_range = reason == A22_RANGE_REASON
self._range_control.setVisible(supported_range)
self._range_control.setEnabled(supported_range)
self._range_control.setChecked(supported_range)
self._range_reason.setText(A22_RANGE_REASON if over_long_range else "")
self._range_reason.setVisible(over_long_range)
```

The existing Range accessible-description/tooltip assignments at `src/appio/ui/main_window.py:612` follow decision D2. The visible range reason is the existing `range_reason` label, not a new label or a replacement control.

**R08 — Leaving a range.** Starting separately from a displayed supported range, `set_window(since_boot)`, `set_window(lifetime)`, `set_window(over_long_range)`, and `set_window(launch)` MUST each hide, disable and uncheck that same Range widget. The over-long destination alone shows `range_reason` with the exact range refusal text. All other destinations clear its text and hide it. Returning to a supported range restores visible, enabled and checked state. None of these transitions changes the Since boot supply rule in R04.

**R09 — Click behavior.** `_on_since_boot_clicked(self) -> None` and `_on_range_clicked(self) -> None` MUST retain their existing bodies and request semantics (`src/appio/ui/main_window.py:1091`, `src/appio/ui/main_window.py:1098`). Since boot re-applies the supplied boot window, or unchecks and returns when none exists. Range unchecks and returns when no active window exists, the active kind is not `range`, or `_disabled_reason` is non-`None`; otherwise it calls `set_window()` with that exact active range. A supported same-window click remains a re-query, with the existing Files ownership/adoption behavior. Neither handler opens a picker, synthesizes bounds, or requests a disabled scope.

## 4. Interfaces, refusal cleanup, and caption ownership

**R10 — Exact public interfaces.** The interfaces MUST have these signatures and return `None`:

| Class | Exact signature | Contract |
| --- | --- | --- |
| `AppIoWindow` | `set_window(self, window: Window) -> None` | Existing API. Retain `_active_window = window`, set `_disabled_reason = window_disabled_reason(window)`, update the scope controls, then follow the refused or supported branch below. A refusal is rendered, not raised by this UI method. |
| `FilesView` | `refuse(self, reason: str) -> None` | New public method. Perform R13–R16 in their numbered order, synchronously on the UI thread, without adopting a window or dispatching a query. Pass the selected reason text through unchanged; argument handling is decision D5. |

`FilesView` keeps its current imports and structural history interface. This API needs no dependency on the shell, a concrete router, the producer, the store, or a setup module.

**R11 — `AppIoWindow.set_window()` refused branch.** After recording the requested window and updating controls, a non-`None` reason MUST execute this branch in exactly this order, for every refusal, regardless of `_owns_files_scope`:

```python
if reason is not None:
    self._render_unavailable(reason)
    self.detail_tabs.setCurrentIndex(0)
    self.files_view.refuse(reason)
    if self._owns_files_scope:
        self.files_message.setText(reason)
        self.files_message.setVisible(True)
    return
```

Index `0` is the existing Overview page; index `1` is Files. The refusal MUST select Overview even when Files was selected, or when the embedded shell has not displayed its first frame. It MUST return before `refresh()` and `_sync_owned_files()`, so no ranking, series, disk-share, file-choice, or file-view query is dispatched for the refused scope. Calling `refuse()` only inside the ownership condition violates the embedded contract.

**R12 — Supported continuation.** A supported `set_window()` call MUST clear the standalone refusal caption before the existing `refresh()` call:

```python
if self._owns_files_scope:
    self.files_message.setText("")
    self.files_message.setVisible(False)
self.refresh()
if self._owns_files_scope:
    self._sync_owned_files()
```

This retains the existing one-owner synchronization at `src/appio/ui/main_window.py:660` and its deferred-refresh flush at `src/appio/ui/main_window.py:673`. Embedded adoption remains shell-owned. Successful adoption does not add a tab-selection change; the forced Overview selection belongs to refusal only.

**R13 — `FilesView.refuse()`, duty 1: retire before clearing refresh demand.** The first duty MUST call `self.retire_deliveries()` and then set `self._refresh_wanted = False`. `retire_deliveries()` increments `_generation` and clears `_outstanding`; its current behavior can set `_refresh_wanted` for an outstanding request, so reversing those two operations is incorrect. A settled delivery queued before refusal MUST fail the existing generation comparison in `_on_delivered()` and publish no rows, totals, state or replacement demand afterward. This retires publication; it adds no worker cancellation or query.

**R14 — Duty 2: forget the adopted and deferred scopes.** The next duty MUST set `self._window = None` and `self._deferred_window = None`. Refusal MUST NOT install the launch or over-long window as a Files scope. A later adoption of the same previously displayed supported window is therefore a changed-scope adoption, not merely a deferred refresh. Both fields are asserted directly immediately after refusal. Request count alone is insufficient: `set_app_id(app_id, request=False)` can set `_refresh_wanted` at `src/appio/ui/files_view.py:673`, and the existing owner flush can issue exactly one request even if the scope reset was omitted.

**R15 — Duty 3: clear the subject and selector.** The next duty MUST clear `_app_id` to `None`, `_choices_loaded` to `False`, all `app_selector` items, and the selector's visibility. Selector mutation and hiding MUST occur with its signals blocked, following decision D3. A hidden selector retaining old items is insufficient: its item labels contain old byte totals. The cleared selector has zero items and no current application selection; cleanup does not emit a new subject selection or trigger a request.

**R16 — Duty 4: render unavailable through the existing boundary.** The final duty MUST call exactly `self._set_unavailable(None, reason)` (`src/appio/ui/files_view.py:1094`). It MUST NOT substitute `_handle_failure()`, whose transient/admission paths can retain data. The call clears `last_result`, clears the model, sets every label returned by `_footer_labels()` at `src/appio/ui/files_view.py:483` to the literal `unavailable`, sets `FilesView.state` to `"unavailable"`, and shows the existing state label through `_set_state()`. The numeric/status footer labels are `files_footer_state`, `files_footer_observed`, `files_footer_counted`, `files_footer_uncounted`, `files_footer_unknown_owner`, `files_footer_skipped`, `files_footer_weak`, and `files_footer_hooks`. Footer/static-text and prefix interpretation is recorded in D4. `_unavailable_reason` is `None`, so `unavailable_text()` returns the supplied reason with no added reason token. Existing `state_changed` emission is retained.

**R17 — Refusal is independent of adoption admission.** R13–R16 MUST run for a direct refused `set_window()` even when Files is embedded, hidden, transient, or not ready to adopt a scope. At return, no previous Files row, numeric footer, selector item or pending delivery can republish the old window. `refuse()` MUST NOT call `FilesView.set_window()`, `set_app_id()`, `request_refresh()`, `flush_refresh()`, `history.prepare()`, a choice lookup, or any query. It MUST NOT change sorting, direction, search, scope-readiness policy, shell target/displayed/pending fields, or recovery/stall policy. The only scope/subject changes are the explicit resets above.

**R18 — One caption writer per Files owner.** `files_message` MUST be written only by the Files owner. In a standalone `AppIoWindow`, `_owns_files_scope` is true (`src/appio/ui/main_window.py:360`), and R11/R12 own refusal-caption set/clear. In an embedded console it is false; `AppIoWindow.set_window()` MUST NOT write that caption. The existing shell connection from `files_view.state_changed` invokes `_render_files_message()`, which derives its caption from `_files_status_text()`, `files_view.state` and `files_view.unavailable_text()` (`src/appio/ui/collector_window.py:697`, `src/appio/ui/collector_window.py:716`, `src/appio/ui/collector_window.py:720`). With a displayed, ready shell frame and an unavailable Files state, that caption remains `file activity is unavailable: <reason>`. The shell retains all existing precedence/visibility conditions, including hiding the entire Files view while `_displayed is None`; it receives no second writer or new special refusal path. Overview's visible `detail_state` guarantees readable refusal text under those conditions.

## 5. Error handling and exact visible text

**R19 — Exact retained strings.** The implementation MUST keep the following strings, constants and existing reachability byte-identical, including punctuation and capitalization. Removing the Launch import from `main_window.py` MUST NOT remove the constant, its export, the contracts message, or the formatter's refusal branch.

| Exact retained string | Definitions and reachable surfaces |
| --- | --- |
| `Launch window disabled: device and file activity are not stored per launch.` | `A22_LAUNCH_REASON` at `src/appio/ui/formatting.py:12`; `LAUNCH_WINDOW_MESSAGE` at `src/appio/contracts.py:103`. Still returned by `window_disabled_reason()` at `src/appio/ui/formatting.py:78`; returned as the exact query exception text for `launch_attribution`; shown by shell prepare refusal; rendered as exact `detail_state` text on direct `set_window(launch)`; preserved as the Files reason payload and standalone `files_message` text. |
| `Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.` | `A22_RANGE_REASON` at `src/appio/ui/formatting.py:15`; `RANGE_TOO_LONG_MESSAGE` at `src/appio/contracts.py:106`. Still returned by the formatter and query refusals for `unsupported_window`; visible in `range_reason` for an over-long range, in direct-refusal `detail_state`, and at the same shell/Files refusal surfaces as the launch text. |
| `Historical boot prefix unavailable: totals cover the full collected boot.` | `HISTORICAL_BOOT_PREFIX_MESSAGE` at `src/appio/contracts.py:110`; the existing non-exact historical boot-prefix refusal at `src/appio/tier.py:41` remains unchanged. This feature does not broaden the UI formatter's refusal predicate. |
| `unavailable` | Existing literal for all five refused Overview KPIs and all eight Files footer labels listed in R16. No zero or retained number substitutes. |

The Files state label retains its existing composition `file history unavailable: <reason>` (`src/appio/ui/files_view.py:1107`); the shell caption retains `file activity is unavailable: <reason>` when its existing displayed/ready conditions expose that state. The complete A22 reason is an unchanged substring of each prefixed surface. The standalone `files_message` and Overview `detail_state` contain only the exact A22 reason.

**R20 — Direct-detail fallback and accessibility.** `_render_unavailable(reason)` at `src/appio/ui/main_window.py:1037` MUST keep its existing unavailable rendering: empty ranking, cleared shared subject and totals, five `unavailable` KPIs (`total_value`, `read_value`, `write_value`, `peak_value`, `device_value`), unavailable charts with no stale numeric shapes, and the exact reason in the visible `detail_state` `QLabel`. R11/R13–R16 add Files cleanup and Overview selection to that path. The visible label text is the assistive-technology surface; the implementation MUST NOT add a new accessible description, label, error state or log line to replace the removed Launch chip's description.

**R21 — Three refusal surfaces and error behavior.** The implementation MUST preserve the error classification, exact text, and propagation in this table. Query-layer and shell prepare-refusal behavior are unchanged; direct-detail refusal retains its existing text/unavailable Overview result and gains only the specified visibility and Files cleanup.

| Surface | Trigger and verified path | Required result | Change status |
| --- | --- | --- | --- |
| Query layer | Any well-formed launch or over-long bounded range; tier refusal at `src/appio/tier.py:29` and `src/appio/tier.py:35`, wire launch refusal at `src/appio/ipc_models.py:412`, router prechecks at `src/appio/history_router.py:342` and `src/appio/history_router.py:776`, daemon error forwarding at `src/appio/iod.py:539` | `WindowUnavailableError` retains the exact A22 `reason` token and `str(error)`, with disabled-window precedence before identity/ID lookup. No data, replacement scope or query-side mutation. | Unchanged. |
| Console shell prepare refusal | `CollectorWindow.show_window()` reaches `_submit_prepare()` and `history.prepare()` refuses; catch at `src/appio/ui/collector_window.py:927` | Show exact `str(error)` in the existing shell error line and re-raise unchanged. Retain the previously rendered frame. No invocation of the new direct-detail cleanup on this shell-only path. Existing transient retirement and state handling remain intact. | Unchanged; refusal here does not clear the prior frame or select a new detail tab. |
| Direct detail pane | `AppIoWindow.set_window()` receives a launch or over-long range directly | Render exact reason in `detail_state`, select Overview, withhold ranking/series/share queries, clear Overview numeric state, and call `FilesView.refuse(reason)` for either owner. The Files state carries the same reason; the owner renders its caption according to R18. | Existing exact text and unavailable Overview behavior preserved; Files cleanup and guaranteed Overview placement added. |

## 6. Amendments to earlier specifications

**R22 — Exact A22 consumer amendment.** This specification MUST supersede exactly the following four consumer-wording locations in `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md`. This is an effective amendment recorded here; it does not require editing the older file as part of this drafting task. The prose fragment's surrounding prohibitions on dispatching a disabled scope, silently selecting/labeling a wider replacement, and showing stale numeric detail remain in force. “No control” for an over-long range means no offered/visible control; the reusable hidden Range widget still exists under R06.

| Location | Current text | Amended text |
| --- | --- | --- |
| A22 prose, `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md:1031` | “MUST render a disabled, unselected chip/control with that text visible as its reason (also exposed to accessibility)” | “MUST NOT offer a control for a scope that is never actionable; MUST keep the exact text visible in the detail view” |
| Launch consumer-state cell, `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md:1035` | “`Launch` control disabled and unselected; reason text exactly as the error message; queried detail state unavailable” | “No launch control; reason text exactly as the error message; queried detail state unavailable” |
| Over-long-range consumer-state cell, `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md:1036` | “That range control disabled and unselected; reason text exactly as the error message; queried detail state unavailable” | “No control for an over-long range; reason text exactly as the error message; queried detail state unavailable” |
| Disabled-window decision-table row's visible consequence, `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md:1063` | “The requested control is disabled with the exact reason; detail data is unavailable and the scope is never widened.” | “No control is offered for the requested scope; the exact reason is shown and the scope is never widened.” |

**R23 — Every store outcome unchanged.** The implementation MUST NOT change any store/query outcome when applying either amended consumer cell or the decision-table amendment. The two A22 table rows retain these exact outcomes, and the decision-table row retains “No query-side mutation; raise the exact A22 exception”:

| Requested scope | Exact unchanged outcome |
| --- | --- |
| Any well-formed `kind="launch"`, regardless of retained rows or known/unknown launch ID | `WindowUnavailableError(reason="launch_attribution", message="Launch window disabled: device and file activity are not stored per launch.")` |
| `kind="range"` with `end_mono_ns - start_mono_ns > MAX_RANGE_NS` | `WindowUnavailableError(reason="unsupported_window", message="Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.")` |

All six existing public query entry points retain A22 precedence: `ranking`, `series`, `disk_share`, `file_table`, `file_coverage`, and `unknown_owner_file_coverage`. Exact supported full-boot/lifetime totals, the historical-prefix refusal, and all other existing query strings and reason tokens remain unchanged. There is no new launch query or attribution path.

**R24 — Earlier UI and design clauses.** The handler-semantics clause at `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md:299` MUST continue to mean that `_on_since_boot_clicked` re-applies a supplied since-boot window and `_on_range_clicked` re-applies only the existing supported range. Its control presentation is amended only by R06–R08: explicit construction-time Range disable/hide and visibility tied to supported ranges. The earlier design's requirement for a launch “chip with a stated reason (`launch_attribution`)” at `docs/superpowers/specs/2026-10-21-app-io-monitor-design.md:392` is superseded for the Launch offer only. A06 launch recording, tier/retention rules, console rendering contracts and the silent-daemon recovery state machine remain ratified. No approved design file is rewritten by this specification.

## 7. Test inventory and implementation gates

These are test contracts, not execution results. The new/changed behavior is expected to require implementation against the drafting baseline. Retained controls demonstrate existing behavior and do not substitute for proof of a new cleanup or visibility mechanism.

**R25 — Existing tests to rewrite, rename, or extend.** The implementation MUST make these four migrations using exactly the current and resulting names shown. One extension keeps its name; the other three replace the old test names one-for-one.

| File and verified current location | Current name | Resulting name | Required assertions and falsifying mutation |
| --- | --- | --- | --- |
| `tests/test_ui.py:432` | `test_disabled_launch_control_uses_exact_a22_reason` | `test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text` | Assert `findChild(QAbstractButton, "scope_launch") is None` and `findChild(QLabel, "launch_reason") is None`. From a populated frame, direct `set_window(launch)` makes no additional ranking, series or share call, empties the ranking, leaves all five KPIs unavailable and exact `A22_LAUNCH_REASON` in visible `detail_state`. Keep equality against the existing literal A22 text, not only against a possibly edited constant. Re-adding a disabled `scope_launch` button or the `launch_reason` label fails absence assertions; suppressing/rewriting the detail fallback fails visibility/text assertions. |
| `tests/test_ui.py:461` | `test_over_365_day_range_control_uses_exact_a22_reason` | `test_over_365_day_range_shows_the_reason_and_hides_the_control` | Use `MAX_RANGE_NS + 1`. Range exists but is hidden, disabled and unchecked; `range_reason` is visible with the exact literal range text; `detail_state` is exact, ranking is empty, KPIs unavailable, and no additional query is dispatched. Retain the existing tooltip/accessibility assertions under D2. Making Range unconditionally visible or dispatching the refused range fails this test. |
| `tests/test_ui.py:409` | `test_empty_supported_range_labels_the_active_window` | `test_empty_supported_range_labels_the_active_window` | Preserve the existing active-window label assertions. Before the range, `scope_range` is hidden; after an empty supported range of exactly `MAX_RANGE_NS`, it is visible, enabled and checked. A mutation that hides/disables Range for every state, never checks it, or changes the inclusive supported boundary fails this extension. |
| `tests/acceptance/test_p1_p2.py:429` | `test_disabled_windows_raise_exact_a22_errors_and_disable_controls` | `test_disabled_windows_raise_exact_a22_errors_and_offer_no_control` | Rename parameter `control_name` to `control_state`; use D6's row values. Retain all six real-query exception/type/reason/exact-message assertions, unchanged store snapshot, `detail_state == message`, and `total_value == "unavailable"`. The launch row asserts no `scope_launch`; the over-long-range row asserts an existing hidden, disabled, unchecked `scope_range`. Remove the common requirement that both scopes possess a disabled control with tooltip/accessibility text; the Range-only checks remain covered by its UI test. Restoring a disabled Launch widget or exposing/enabling/checking the over-long Range fails the corresponding parameter row. |

The acceptance parameter rows have these required meanings:

| `kind` | `reason` | `control_state` | Control assertion |
| --- | --- | --- | --- |
| `launch` | `launch_attribution` | `absent` | `window.findChild(QPushButton, "scope_launch") is None` |
| `range` | `unsupported_window` | `hidden` | `scope_range` exists and is hidden, disabled and unchecked |

**R26 — New tests.** The implementation MUST add exactly these seven named tests in `tests/test_ui.py`. They SHOULD reuse the existing `FakeFilesHistory` (`tests/test_ui.py:1458`) and `retained_file_result()` (`tests/test_ui.py:1507`) seams, with production `AppIoWindow`, `FilesView`, and, for the embedded test, production `CollectorWindow`. No fake cleanup method or fake Files widget can prove these contracts. The “mutation” column defines a defect which the named test must detect; it is not a change to ship.

| Exact new test name | Required setup and assertions | Mutation that must fail it |
| --- | --- | --- |
| `test_default_console_offers_only_since_boot` | Show a default `AppIoWindow(service)` without boot arguments. Since boot exists, is visible, disabled and unchecked; Launch and `launch_reason` are absent; Range exists, is hidden, disabled and unchecked. `_active_window` and `_since_boot` remain `None`. Direct calls to both scope handlers are safe, query nothing, and leave `has_frame` false. | Remove the construction-time `setVisible(False)` for Range; independently, leave Range enabled at construction. Making its visibility unconditional also fails. |
| `test_leaving_a_range_hides_the_control` | Re-enter a supported displayed range before each destination: since_boot, lifetime, over-long range, launch. The same Range widget becomes hidden, disabled and unchecked in every destination; only over-long range shows the exact `range_reason`. Returning to a supported range exposes the same widget again. Exercise the cached-boot and no-supplied-boot state-table distinction without inventing a boot window. | Keep Range visible after a non-range/refused destination, or omit disabling/unchecking on exit. Entry-only assertions do not count as transition coverage. |
| `test_refused_window_clears_files_and_shows_the_reason` | Use a standalone console with a populated Files model, a populated selector that includes a file-only choice, numeric footer values, Files selected (`setCurrentIndex(1)`), and one outstanding delivery. Close Files scope admission before refusal, preserving the outstanding request. Direct `set_window(launch)` empties the table and selector, hides the selector, clears `_app_id` and `_choices_loaded`, sets every numeric/status footer to `unavailable`, and sets Files state to `unavailable`. Assert `refresh_wanted()` false and `_outstanding` false; no additional ranking, series, share, choice-union or file request occurs. Overview is selected and its visible `detail_state` is exact. The Files state text contains the same reason. `files_message.text()` is exact and `not files_message.isHidden()`; then select Files and assert `files_message.isVisible()`. Selector clearing emits no new selection; D3's signal-block state is restored. | Drop `files_view.refuse(reason)`, replace its terminal `_set_unavailable()` with transient failure handling, fail to clear/hide the selector, or clear `_refresh_wanted` before a retirement that re-arms it. Omit `setCurrentIndex(0)` and the tab assertion fails. |
| `test_late_delivery_after_refusal_never_republishes` | Start with rendered rows and an outstanding replacement. Settle the replacement before refusal but arrange for its Qt delivery to run only after `refuse()` has completed. Drain that delivery and assert the unavailable state, empty model/selector, all unavailable footers and false refresh-wanted flag survive, with no replacement request. The queued-delivery protocol is fixed by D6. | Omit `retire_deliveries()` in `refuse()` or bypass `_on_delivered()`'s stale-generation guard; the old successful result then republishes and fails the post-delivery assertions. |
| `test_returning_to_the_refused_window_reloads_once` | Supported W → launch → the identical W. Immediately after refusal, assert `_window is None`, `_deferred_window is None`, `_app_id is None`, `_choices_loaded is False` and no refresh is wanted. D6 ensures the deferred field was non-`None` before refusal. Re-admit scope adoption, return to W, assert exactly one additional Files request, settle it, and observe rows restored. | Omit `_window = None` in `refuse()`; the direct state assertion fails even if request count remains one through the old deferred-refresh path. Independently omit `_deferred_window = None`; its seeded-state assertion fails. |
| `test_embedded_direct_refusal_still_clears_files` | Construct the real shell's embedded console with no boot arguments and leave shell `_displayed is None`. Through the existing fake-history seam, seed previous Files rows, selector totals and an outstanding delivery without installing a shell frame. Select Files, call `shell.view.set_window(launch)` directly, and assert empty rows/selector, hidden selector, all unavailable footers, unavailable Files state and no query for refusal. Drain the pending old delivery and prove it cannot republish. Overview is selected and exact `detail_state` is visible even though `files_view.isVisible()` remains false before the first shell frame. Assert `files_message.text()` equals the shell's existing `_files_status_text()` result and that the embedded console does not overwrite it with the direct reason, as R18/D4 require. | Guard `files_view.refuse(reason)` with `_owns_files_scope`; the embedded case then retains stale data. Omitting Overview selection fails the pre-frame readable-label assertion. |
| `test_supported_adoption_clears_the_refusal_message` | In a standalone console, refuse a window, then adopt a supported window. Assert `files_message.text() == ""` and `files_message.isHidden()` while the supported frame/adoption follows the existing single-query ownership path. | Omit either the standalone supported-path `setText("")` or `setVisible(False)`; the corresponding assertion fails. |

**R27 — Retained tests and expectations.** The following existing controls MUST keep their names and present expectations, rather than being rewritten to accommodate stale state:

| File and current name | Retained contract |
| --- | --- |
| `tests/p3/test_p3_acceptance.py:135` — `test_a22_exact_messages_in_client_router_and_window` | Unchanged. Client and router preserve exact launch/range errors. Its direct embedded `view.set_window(scope)` at `tests/p3/test_p3_acceptance.py:148` still exposes the exact text and unavailable total; it requires no control to exist. |
| `tests/test_ui.py:1742` — `test_detail_tabs_are_exactly_overview_then_files` | Unchanged. Exactly Overview then Files, persistent empty construction, no initial query, and safe empty handlers. Its existing direct handler calls at `tests/test_ui.py:1768` and `tests/test_ui.py:1769` remain valid. |
| `tests/test_ui.py:2423` — `test_attribution_panel_keeps_rows_and_boot_qualifies_lifetime_labels` | Unchanged. Lifetime share-lane labels and boot qualification remain correct; adding the explicit lifetime control-state row changes none of those labels. |

The existing query and formatting coverage in `tests/test_contracts.py`, `tests/test_query.py`, `tests/p3/test_history_router.py`, `tests/p4/test_files_view.py`, and `tests/p4/test_files_tab_scope_text.py` retains its current expectations. All other ranking, series, share, Files, and recovery tests do likewise.

**R28 — Implementation gates and evidence.** After implementation, the engineer MUST:

1. Run `python -m pytest tests/test_ui.py tests/acceptance/test_p1_p2.py` successfully in the repository environment.
2. Run the existing full suite with `python -m pytest`, including the unchanged P3 acceptance control. This drafting task adds no new environment or privileged integration requirement.
3. Observe and record full-suite collection counts before and after implementation with `python -m pytest --collect-only -q`. There are two UI rewrites, one UI extension, seven new UI tests and one acceptance rename; report actual collected counts, since parameterization makes arithmetic alone insufficient evidence.
4. Run the required mutation checks against a passing targeted baseline in a disposable copy and record the named failing assertion. At minimum: reintroducing a disabled Launch button fails `test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text`; unconditional Range visibility fails the empty/over-long-range assertions; removing empty-construction hide fails `test_default_console_offers_only_since_boot`; retaining Range visibility on exit fails `test_leaving_a_range_hides_the_control`; dropping `files_view.refuse()` fails `test_refused_window_clears_files_and_shows_the_reason`; omitting `_window` reset fails `test_returning_to_the_refused_window_reloads_once`. Also prove the new-test mutations specified in R26. Restore each mutation and verify the targeted baseline passes again. An import/collection failure does not establish a contract assertion failure.
5. Report missing evidence as not run. No passing test result or mutation kill is inferred from the test inventory in this document.

## 8. Specification decisions

These are the only choices filling details left implicit by the approved design. They select the smallest behavior consistent with its explicit table, algorithms and unchanged paths; none adds a feature.

| Decision | Implicit point | Chosen interpretation |
| --- | --- | --- |
| **D1 — Since boot placeholder** | The general “shown only while actionable” rule coexists with the explicit table's present-but-disabled Since boot control before any boot window is supplied. | The explicit six-row table governs: Since boot remains visible, disabled and unchecked before supply. Range alone uses conditional visibility. No new exception applies to Launch or any other scope. |
| **D2 — Hidden Range metadata** | The design removes Launch metadata explicitly, but its Range pseudocode does not mention the existing Range tooltip and accessible description. | Preserve those assignments: both equal `A22_RANGE_REASON` for an over-long range and are cleared to `""` in every other state, exactly as the current code does. The hidden control is not the visible explanation; `range_reason` carries it. |
| **D3 — Signal-block restoration** | The selector cleanup requires a blocked-signals guard without specifying its restoration form or another subject notification. | Save the bool returned by `app_selector.blockSignals(True)`; clear `_app_id`, `_choices_loaded`, selector items and selector visibility inside `try`; restore that saved bool in `finally`. Add no manual `subject_changed` emission. Preserve the normal `state_changed` notification from `_set_unavailable()`. |
| **D4 — Existing display composition** | “Every footer” and “exact text on Files” need an executable reading against the existing static note, prefixed state label, and shell caption gating. | Reset exactly the eight `_footer_labels()` entries, leaving the standing `footer_note` explanation and existing updating/stall policy intact. Preserve `file history unavailable: <reason>` on the Files state label, literal reason-only text on standalone `files_message`, and existing shell caption priority. Before a first shell frame the caption can still describe waiting/loading and Files remains hidden; Overview is the guaranteed exact visible refusal. No shell rewrite forces the caption to show a refused scope. |
| **D5 — Refusal input and admission guards** | The design defines the ordered cleanup but adds no argument-validation or closed-view API branch. | Accept the reason string already chosen by `window_disabled_reason()` and pass it unchanged to `_set_unavailable(None, reason)`. Add no coercion, validation exception, or new closed/transient/readiness guard to `refuse()`; its four duties implement the direct refusal. Existing delivery rejection for a closed/stale view remains unchanged. |
| **D6 — Test-only discriminators** | The acceptance parameter's replacement values, deterministic late-delivery ordering, refusal during closed admission, and a non-vacuous deferred-scope reset fixture are unnamed. | Use `control_state="absent"` for launch and `"hidden"` for the over-long range. For late delivery, settle a real pending Future on a worker while the UI thread has not yet processed its queued bridge delivery; refuse, then drain that delivery before assertions. A Future merely being done is not proof that the Qt delivery ran. For standalone cleanup, close Files admission with `set_scope_ready(False)` before refusal so transient failure handling cannot masquerade as unconditional cleanup. For the W-return test, close scope admission and call the existing Files `set_window(W)` seam to seed a non-`None` deferred W before refusal; assert both scope resets, then reopen admission before returning to W. These are fixture choices only and add no production state or API. |

## 9. Non-goals and acceptance criteria

**R29 — Non-goals.** The implementation MUST NOT add a range picker, a new way to enter a range, a lifetime control, a replacement Launch explanation link, a launch query, or per-launch device/file storage. It MUST NOT change A06 launch bookkeeping, the `sample_dev`/`file_sample` dimensions, any daemon/wire/schema/retention/tier/query policy, totals, frame contents, source ownership, or recovery behavior. It MUST NOT clear a previously rendered frame on the shell's own `prepare()` refusal, alter the separate `appio setup failed (exit 127): Error accessing /usr/bin/appio-setup` banner, add dependencies, refactor unrelated code, or introduce new error states, labels or log lines. The only extra cleanup beyond scope-control presentation is R11–R17's direct-refusal Files cleanup, which adopts and queries nothing.

**R30 — Acceptance.** The implementation MUST meet every criterion below, with R28's observed test and mutation evidence:

| Criterion | Evidence required |
| --- | --- |
| Launch is not offered, constructed, styled or checked; its permanent sentence is gone. | Exact R05 removals; both removed object names absent; rewritten Launch test passes and detects reintroduction. |
| Exactly two persistent scope widgets remain, with all six specified states and transitions. | Construction is hidden/disabled/unchecked for Range; supported-range entry and exit tests; correct remembered Since boot availability; Range re-query semantics preserved. |
| Exact refusal strings remain reachable and no scope is widened. | Both real-query acceptance rows, unchanged client/router/P3 coverage, visible exact Overview text, over-long-range reason, and owner-specific Files text. |
| A direct refusal cannot retain or later republish old detail data. | Empty ranking/Files model/selector, unavailable KPIs/footer/charts, retired delivery generation, false refresh demand, and direct `_window`/`_deferred_window` reset assertions; late-delivery and embedded tests pass. |
| Refusal is readable from either prior tab and before the first frame. | Refusal selects Overview with visible exact `detail_state`; standalone caption is explicitly shown and becomes visible on selecting Files; embedded Files remains correctly shell-gated. |
| A supported window can be adopted after refusal without stale captions or duplicate Files requests. | W-return reloads exactly once and restores rows; supported adoption leaves standalone `files_message` hidden with empty text; existing owner tests keep their expectations. |
| Earlier contracts are amended only to the approved extent. | Four A22 consumer amendments recorded with unchanged store outcomes; charts handler semantics, A06, retention and recovery preserved; no unrelated code changes. |
