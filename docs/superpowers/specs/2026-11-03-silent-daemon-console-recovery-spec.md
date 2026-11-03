# appio silent-daemon console recovery: technical specification

Date: 2026-11-03. Status: implementation contract for the approved design.

## 1. Authority, provenance, and boundaries

Behavioural authority: `docs/superpowers/specs/2026-11-03-silent-daemon-console-recovery-design.md`, sections 1-9. This specification is subordinate to that approved design and remains consistent with:

- `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md`
- `docs/superpowers/specs/2026-10-21-app-io-monitor-design.md`
- `docs/superpowers/specs/2026-10-30-appio-files-tab-spec.md`
- `docs/superpowers/specs/2026-11-01-appio-console-charts-spec.md`
- `docs/superpowers/specs/2026-11-01-appio-console-charts-design.md`

The drafting baseline is commit `503f09707af8e42bae26a93b258f1fb89d22cfe2`. A regression classification below means that the specified assertion is expected to fail against that source state; the test suite was not run for this drafting task.

This change is limited to the UI shell, its owner/router coordination, typed presentation diagnostics, and related tests. It adds no daemon or wire behavior, data contract, schema, dependency, retention rule, sampling cadence, collection ownership rule, or daemon-recovery action. The existing Overview/Files tab, single-subject behavior, A22 refusal strings, owner-state strings, and retention semantics remain in force.

### 1.1 Existing evidence

| Existing evidence at the drafting baseline | Contract consequence |
| --- | --- |
| `CollectorCoordinator.reconcile()` invalidates on every successful call, and `tick_local()` invalidates after every local tick. | Replace probe-fact equality with one source-selection key; return tick advancement to the window and remove tick-owned invalidation. |
| `HistoryRouter.invalidate()` retires the prepared generation and sets `view_state` to `unavailable`; `prepare()` starts a local worker and may dispatch remote queries. | Preserve an in-flight read through same-source probe deltas and coalesce sample freshness behind an applied frame. |
| `CollectorWindow` prepares from both `_tick_target()` and `show_window()`; current state has `_target`, `_displayed`, `_pending`, and `_invalidated`. | All preparation submissions, including explicit requests, use one gate. The displayed frame remains separate from requested scopes. |
| `HistoryRouter._not_retained` is a data-coverage flag but is also set when a remote query fails. | Keep it for actual retention evidence; add a separate typed display diagnostic for remote-query failures. |
| `render_model._coverage_text()` appends `Coverage.unavailable_reason`; `_device_unavailable_text()` renders only data-side attribution reasons. | Add the client diagnostic without overwriting a contract reason, and render its matching clause in both surfaces. |
| `FilesView._update_updating_label()` owns the normal `updating to <kind>` caption. | Add a stall override which does not change table, footer quantities, scope admission, or query state. |
| `parse_unit_facts()` accepts exactly `UnitFileState` and `ActiveState`; `ServiceFacts` currently has five positional fields. | Keep those required facts and positional meanings; append three defaulted optional evidence fields. |

## 2. Coordinator staleness and local ticks

### 2.1 Exact history key

Add the following owner-local aliases and field:

```python
OwnerKind = Literal["gui", "iod", "unknown"]
HistoryKey = tuple[OwnerKind, bool, bool, bool]
# (decision.owner, decision.may_collect_local, facts.daemon_verified, facts.answering)

CollectorCoordinator._history_key: HistoryKey | None = None
```

`None` means no successful reconcile has established the key for the currently stored history. After computing `decision = decide_owner(facts)`, compute exactly:

```python
key: HistoryKey = (
    decision.owner,
    decision.may_collect_local,
    facts.daemon_verified,
    facts.answering,
)
```

After the existing ownership transition completes successfully, perform one and only one probe-driven comparison:

```python
if key != self._history_key:
    self.history.invalidate()
self._history_key = key
```

The comparison occurs after successful owner/local-writer reconciliation and before `reconcile()` returns. Store `key` only after any required invalidation succeeds. A same-key reconcile stores the equal key without changing the router generation or `view_state`. The display string, `facts.enabled`, `facts.active`, `facts.pending_activation`, `sub_state`, `result`, and `restarts` are not key components. In particular, `enabled, not answering` and `unavailable` may differ in display while selecting the same owner/data source.

This comparison is the **only probe-driven invalidation** in `CollectorCoordinator`. `accept_export()` continues to invalidate through `HistoryRouter.add_source()`. Transport-gap invalidation remains in `HistoryRouter.record_transport_gap()`. A local tick is invalidated by the window as specified in section 4, not by the coordinator.

**Rejection rules:** do not invalidate for a changed fact whose computed key is equal; do not add a second equality check on all `ServiceFacts`; do not include display state, enabled/active state, or systemd evidence in the key; do not store the new key when the ownership transition or required invalidation raises.

### 2.2 `tick_local()` return contract and callers

Exact signature:

```python
def tick_local(self) -> bool: ...
```

Return `True` only when this call invokes the permitted local collector and its `tick()` returns successfully, committing the sample/checkpoint. Return `False` when local collection is not permitted or there is no local collector. A tick exception propagates unchanged; it is not converted to `False`. Do not inspect `TickResult.rows_written`: a successful tick advances query-visible checkpoint/cutoff state even when no application rows were written. `tick_local()` itself never calls `history.invalidate()`.

The production caller in `CollectorWindow._apply_facts()` consumes the bool and follows section 4.4. Existing direct callers which ignore the return value continue to tick and receive the same exception behavior, but no longer get an implicit router invalidation. Test doubles implement the bool return and must not invent an invalidation side effect.

## 3. HistoryRouter data and diagnostic state

### 3.1 Exact record and read-only properties

Add the following frozen record in `src/appio/history_router.py`:

```python
@dataclass(frozen=True)
class DaemonAnswerError:
    kind: Literal["remote_error", "unavailable", "rejected", "transport", "internal"]
    reason: str
```

`kind` must be one of those five exact tokens. `reason` must be a nonempty string. Invalid construction raises `ValueError`. `HistoryRouter.daemon_answer_error` is a read-only property of type `DaemonAnswerError | None`.

Add this read-only property:

```python
@property
def local_read_in_flight(self) -> bool: ...
```

It is `True` exactly when `_local_future is not None and not _local_future.done()` — that
is, **unsettled**: queued *or* running. It is `False` for a future that is done, cancelled,
or absent. It observes the router's single local-history worker and performs no wait or
mutation.

**specification decision:** "in flight" means unsettled, not `Future.running()`. A queued
future that has not started is still work on the single worker, so admitting a second
submission while one is merely queued would put two jobs on that worker. Making the
predicate `not done()` is what lets §4.2 invariant 7 hold without an atomic reservation:
the window calls `prepare()` only when no local read is unsettled, so `prepare()`'s own
`_local_future.cancel()` is always a no-op and two local reads can never coexist.

### 3.2 Classification, set/clear points, and retention flag

Classify an exception only when the current-generation remote history query settles in `HistoryRouter.ready()`:

| Settled exception class | `kind` |
| --- | --- |
| `RemoteError` | `remote_error` |
| `ClientUnavailable` | `unavailable` |
| `RequestRejected` | `rejected` |
| `ConnectionLost` | `transport` |
| `WindowUnavailableError` or `concurrent.futures.CancelledError` | No record; these are not daemon-answer failures. |
| Any other `Exception` from that remote-query settlement/response validation | `internal` |

Use `IodClientError.reason` for the record's reason when the exception is an `IodClientError`. For an `internal` exception without that field, use stripped `str(error)`, or the exception class name if that text is empty. Catch `RemoteError`, `ClientUnavailable`, `RequestRejected`, and `ConnectionLost` before the generic `Exception` case; check `WindowUnavailableError` and `CancelledError` before that generic case as no-record outcomes. Do not catch `BaseException` for classification.

Set or replace `_daemon_answer_error` in the current-generation remote-query failure path in `ready()`. Do not classify a local worker failure, a Files query failure, a stale-generation response, or an error raised before a remote query is settled. On remote failure, discard remote contributions/queued remote detail requests as today and retain locally proved rows; the diagnostic is additive and does not alter any `Coverage` field.

`prepare()` clears `_daemon_answer_error` and resets `_not_retained` for the new preparation. `invalidate()` does not set or clear either flag. A `WindowUnavailableError` and `concurrent.futures.CancelledError` do not set either flag. In particular, a navigation/retirement/cancellation outcome is not a daemon failure.

`_not_retained` remains exclusively data-side evidence: it is set by actual local materialization failures where retained intervals cannot be corrected or ownership receipts are absent, by the local materialization result carrying that evidence, or by returned coverage whose `unavailable_reason == "not_retained"`. Clear it only when a new `prepare()` begins, as today. A remote client exception must not set it or stamp `unavailable_reason="not_retained"`; keep the existing coverage reason when genuine data evidence supplied it.

## 4. CollectorWindow read/retry state machine

### 4.1 Fields and clock

Add these exact fields to `CollectorWindow`:

```python
PENDING_BUDGET_NS: int = 30 * 10**9

self._pending_since: int | None = None
self._stalled: bool = False
self._retry_after: int | None = None
self._deferred_target: Window | None = None
self._refresh_after_apply: bool = False
```

`now` is a single value from `_clock_ns()` for the state transition being evaluated; it is monotonic nanoseconds. The deadline comparison is inclusive: `now - _pending_since >= PENDING_BUDGET_NS`. The deadline follows elapsed clock time, not `Window` identity or bucket changes.

### 4.2 Transition table

In the table, `wanted` is always `_deferred_target or _target`. A submission pass submits at most one `history.prepare(wanted)`. No path outside the submission gate may call `history.prepare()`.

| Event and precondition | Required state updates | Submission and visible result |
| --- | --- | --- |
| Initial load; `_pending is None`, `_deferred_target is None`, `_retry_after is None`, target exists, local reader idle | No stall fields change before `prepare()` succeeds. On successful acceptance set `_pending = wanted`, `_pending_since = now`, `_stalled = False`, `_retry_after = None`. | Submit the first preparation in this pass. The gate must not block initial load. |
| A gated preparation is accepted | Set `_pending_since = now`; clear `_stalled` and `_retry_after`. Keep `_deferred_target` until the matching scope is applied. | Arm the existing readiness poll and render pending status. |
| `_pending is not None`, `_pending_since is not None`, and elapsed time reaches the budget | Set `_stalled = True`; preserve `_pending`, `_pending_since`, displayed frame, and router generation. | Do not cancel, clear, replace, or submit behind the read. Render the derived busy label while `local_read_in_flight` remains true. |
| Router reports unavailable for a pending preparation | Set `_deferred_target = wanted`; clear `_pending` and `_pending_since`; set `_retry_after = now + PENDING_BUDGET_NS`. Do not clear `_stalled`. | Do not submit from the retirement transition, even if the worker is already free. Render busy while the worker remains in flight; otherwise render the retry-floor label while the floor is future. |
| Router is unavailable but `_pending is None` (for example, an immediate invalidation after a sample) | Do not treat this as retirement of a pending read; do not create a retry floor. | The ordinary submission gate may prepare immediately if its other conditions hold. |
| Submission gate pass | Preconditions are all true: `_pending is None`; `local_read_in_flight is False` (no unsettled local read); `_retry_after is None or now >= _retry_after`; `wanted is not None`. | Submit exactly one preparation. If any condition is false, submit none. |
| Explicit `show_window(window)` request | Set `_deferred_target = window`; do not alter `_target`, `_retry_after`, `_stalled`, `_pending_since`, or a busy label. | Run the same gate. If A is pending or a local read/floor blocks, do not call `prepare()`; B remains deferred. |
| Committed local tick when no live preparation is pending | If `tick_local()` returned `True`, invalidate immediately. Do not set `_refresh_after_apply`. | The next target pass uses the submission gate; with no floor it can submit in that pass. |
| Committed local tick while a non-retired preparation is pending | If `tick_local()` returned `True`, set `_refresh_after_apply = True`; leave router generation unchanged. If the router is already unavailable because another transition retired it, do not set the flag. A `False` return changes nothing. | Keep the preparation; multiple committed ticks coalesce into the same bool flag. |
| A frame for scope `applied` becomes ready | Apply that frame once; clear `_pending` and `_pending_since`; clear `_stalled` and `_retry_after`. Clear `_deferred_target` only if `applied == _deferred_target`. | Clear the stall caption. If `_refresh_after_apply` is true, consume it and invalidate exactly once after applying the frame; the next target pass prepares the next scope through the gate. |
| Deferred target differs from the frame just applied | Leave `_deferred_target` unchanged. | The next gate pass prepares the deferred scope exactly once when idle and past the floor. |
| Floor is future and local worker is idle | Preserve `_deferred_target`, `_retry_after`, and `_stalled`. | Do not submit; display `retrying in <n> s`, where `n` is positive ceiling seconds remaining. |
| Floor has elapsed but worker is still running | Preserve deferred target and floor until a preparation is accepted. | Do not submit; display the busy label. A later pass can submit as soon as the worker is idle. |

Every path which observes `_history_retired()` while `_pending` is non-`None`, including `_tick_target()` and `apply_ready_frame()`, performs the same retirement transition once: capture `wanted`, clear `_pending` and `_pending_since`, arm `_retry_after`, preserve `_stalled`, then recompute the status/caption. The transition is guarded by `_pending is not None`; repeated observation after retirement does not restart the floor. An unavailable router with no pending scope never arms a floor.

The only set site for `_refresh_after_apply` is `CollectorWindow._apply_facts()`, inside its existing `files_view.transient_scope()`, immediately after `coordinator.tick_local()` returns. If the return is `True` and `_pending is None`, invalidate history immediately. If it is `True`, `_pending is not None`, and the router has not reported that preparation retired, set `_refresh_after_apply = True` and do not invalidate. If reconcile changed the key and already retired that pending read before the tick returned, do not set a new flag for that tick. A `False` return does neither. This order prevents a second invalidation for the tick whose read was just retired by reconcile.

The only consume site is the end of `_apply_frame()`: after installing `_displayed`, rendering the applied frame with `_show_ready(window)`, flushing the normal Files refresh, clearing the updating scope, and rendering the Files message, but before stopping the readiness poll and returning. If the flag is true, clear it and call `history.invalidate()` exactly once inside `files_view.transient_scope()` so file deliveries retired by this refresh remain transient. Do not call `history.prepare()` inline from `_apply_frame()`; the next `_tick_target()` pass goes through the submission gate. No probe, timer, explicit request, or retirement clears the flag.

`show_window()` becomes an explicit deferred-target request; it must not call `history.prepare()` directly. If A is pending and `show_window(B)` is called, A remains pending; applying A does not clear B; B is then prepared once. If `show_window(B)` is called while no read is pending and the worker/floor permit, B can be prepared during that call's gate pass.

### 4.3 Derived status and exact labels

Before the first frame, the ordinary non-stall status remains `loading history (<view_state>)`; with a displayed frame it remains `updating (<view_state>)`. A stall label replaces either ordinary status only when the pending read has crossed its budget, or when a retirement has armed a retry floor. The text is derived on every status update; it is not cached as the source of truth:

| Predicate | Exact `history_state` / Files stall text |
| --- | --- |
| Stall label eligible and `local_read_in_flight is True` | `history read has not completed (<view_state>); the reader is still busy` |
| Otherwise, `_retry_after is not None and now < _retry_after` | `history read has not completed (<view_state>); retrying in <n> s` |
| Neither | No stall label; use the ordinary loading/updating status or hide it when idle. |

`<n>` is `ceil((_retry_after - now) / 1_000_000_000)` using integer arithmetic. Therefore it is never `0` while `now < _retry_after`. If the reader is idle and the floor is reached, the retry label disappears and the gate may submit.

**State invariants:**

1. A successful accepted preparation has one `_pending` scope and one `_pending_since` timestamp; no submission is accepted while `_pending` is non-`None`.
2. Retirement and frame application clear `_pending_since` at the same transition that clears `_pending`.
3. `_retry_after` is set only by retirement of a pending read; a pending preparation has no active retry floor; explicit requests never change the floor.
4. `_stalled` becomes true only when an accepted pending read reaches its budget. Retirement does not clear it. Acceptance and successful application do.
5. `_deferred_target` is cleared only when a frame for that exact scope is applied. Explicit requests and pending retirement set/replace it; applying another scope leaves it armed.
6. `_refresh_after_apply` is a coalescing bool. A committed tick during a live pending read sets it; only `_apply_frame()` consumes it and performs its single invalidation.
7. An unsettled local read — queued or running — prevents every new preparation, including an initial replacement and an explicit scope request. Because §3.1 defines the predicate as `not done()`, at most one local read can ever exist on the worker, and no duplicate can queue behind it.
8. An unchanged source key does not retire the pending frame. A changed key may retire it, in which case the retirement transition owns the retry floor.

### 4.4 Transition-owned field writes

| Field | Permitted writes |
| --- | --- |
| `_pending_since` | Set to `now` only after a gated `prepare()` is accepted. Clear when that pending preparation is retired or its frame is applied. |
| `_stalled` | Set true only by the elapsed-budget transition. Set false only on accepting a preparation or applying a frame. |
| `_retry_after` | Set to `now + PENDING_BUDGET_NS` only when a pending preparation is retired. Clear on accepted preparation or applied frame. Explicit refresh leaves it unchanged. |
| `_deferred_target` | Set by retirement to `wanted` or by explicit request to its requested `Window`. Clear only when a frame for exactly that scope is applied. |
| `_refresh_after_apply` | Set only after a committed local tick while a live, non-retired preparation is pending. Consumed only in `_apply_frame()` after the frame is rendered/applied; consumption clears it and invalidates once. |

**specification decision:** clearing `_pending_since` on retirement is paired with clearing `_pending`; otherwise a timestamp for a nonexistent pending read would make later deadline checks ambiguous. A previously set `_stalled` is deliberately retained through retirement as required by design rule 3.

**specification decision:** the budget is the threshold for calling a still-pending read stalled. Before `_stalled` becomes true, an ordinary pending local read keeps the existing loading/updating text; after the threshold, the label content is derived from worker/floor state. A retirement creates a floor and makes the derived busy/retry label eligible immediately. This resolves the design's otherwise immediate-busy-label bullet against its explicit 30-second pending budget.

**specification decision:** countdown `n` uses ceiling integer seconds, so any positive remaining floor displays at least `1 s`; no float rounding or zero-second retry message is permitted. A floor 1 ns away prints `1 s`, a floor exactly 1 s away prints `1 s`, a floor 1 s + 1 ns away prints `2 s`, and an elapsed floor prints no retry label at all.

### 4.5 Preparation failures stay owned by the ratified retry seam

The stall/retry fields in §4.2 describe a *retirement*; a `history.prepare()` that raises
is a different event and keeps the ratified ownership from
`2026-11-01-appio-console-charts-spec.md` §4.2:

Every submission goes through one helper with an explicit origin —
`_submit_prepare(wanted, origin: Literal["clock", "explicit"])` — because two failure
categories need different owners. The category is decided by **this call**, never by the
router's `view_state`, which a previous failure can leave at `"loading"`.

| Event | Required state | Visible result |
| --- | --- | --- |
| `prepare(wanted)` raises `WindowUnavailableError` from its precheck (window or A22 validation) | This is a pre-loading refusal: `prepare()` runs `local._precheck(window)` before bumping the generation or setting `view_state = "loading"` (`src/appio/history_router.py:745`, `:757`), so no router state changed. Re-raise unchanged; create no `PrepareRetry`, no `_pending`, no stall state; arm no floor. | The ratified A22/validation text verbatim, with no `appio:` prefix and no rewriting — even when a previous failure left `view_state == "loading"`. |
| `prepare(wanted)` raises any other exception (operational) called with `origin == "clock"` | No frame is installed. Record `PrepareRetry(wanted, owner="clock", since=now)`; do not set `_pending`, `_stalled`, `_deferred_target`, or `_retry_after` (the floor belongs to retirement only). | `appio: <str(error)>`, or the exception class name when empty; the retained frame stays; the clock path retries. |
| `prepare(wanted)` raises any other exception called with `origin == "explicit"` | Same as above but record `PrepareRetry(wanted, owner="explicit", since=now)`, matching the ratified seam (`2026-11-01-appio-console-charts-spec.md:427-433`). | `appio: <str(error)>`; **no timer retry**; only a later explicit request may clear it. |
| A `PrepareRetry` with `owner == "clock"` is due and the submission gate holds | Submit exactly one preparation through the gate with `origin="clock"`; the gate still blocks while a local read is unsettled or a retirement floor is future. | Retry status per §4.3. |
| A `PrepareRetry` with `owner == "explicit"` exists | Do not timer-retry. Only a new explicit request (`show_window`, Recheck) may clear it, and that request submits with `origin="explicit"` again. | The explicit failure text remains until that request. |

`_prepare_retry` and the stall fields never substitute for one another: a failed
preparation arms no retirement floor, and a retirement creates no `PrepareRetry`.

## 5. Overview diagnostic and exact coverage composition

### 5.1 Ratified render-model amendment

Append this field to `OverviewModel`:

```python
coverage_diagnostic: str | None = None
```

Extend the existing builder with this keyword-only argument:

```python
def build_overview(
    app_id: int,
    window: Window,
    total: AppTotal | None,
    series: Series | None,
    share: DiskShare | None,
    *,
    ranking_error: str | None = None,
    series_error: str | None = None,
    share_error: str | None = None,
    coverage_diagnostic: str | None = None,
) -> OverviewModel: ...
```

`coverage_diagnostic` accepts `None` or a nonempty UTF-8-encodable `str`, validated by the same optional-text rule as `series_unavailable` and `share_unavailable`. `OverviewModel.__post_init__` applies that validation to direct construction as well as `build_overview`. Reject non-strings, `""`, and non-UTF-8-encodable text with `ValueError`; whitespace-only text is nonempty and is retained verbatim. The default is `None`. Carry the value through every builder outcome, including ranking failure and file-only/no-total outcomes. It is display text, not a contract value, and never substitutes for `Coverage.unavailable_reason`.

The input string is the already-rendered per-kind clause, e.g. `history query failed (too_many_requests)`, without a `reason: ` prefix. `AppIoWindow` reads `HistoryRouter.daemon_answer_error`, maps its `kind` using section 5.2, and passes the result to `build_overview`. Read the property after the selected subject's series/share query attempts, and pass the same diagnostic through the file-only, normal, and contradiction-refusal builder branches. If a query-service test double or alternate service does not expose `daemon_answer_error`, treat it as `None`; lack of this optional presentation property does not fail the data path. No query/model constructor infers or mutates a contract coverage reason.

**specification decision:** read the diagnostic after the query attempts and treat a missing service property as `None`. This keeps the new presentation seam optional for existing query-service doubles and alternate services.

### 5.2 `_coverage_text()` and the three cause combinations

Extend the helper signature:

```python
def _coverage_text(
    coverage: Coverage,
    coverage_diagnostic: str | None = None,
) -> str: ...
```

The unchanged prefix is exactly:

```text
coverage: <complete-or-incomplete>; device attribution: <device_state>; tier: <tier>; gaps: <gap_intervals>; suspends: <spans_suspend>
```

The text conversions are the existing Python conversions: `complete` is `complete` iff true, otherwise `incomplete`; `spans_suspend` is `True`/`False`; numeric fields use base-10 `str()`; enum/reason text is preserved verbatim. The no-cause output is the prefix exactly as printed above. The only three cause-bearing outputs are:

| Causes present | Exact full output |
| --- | --- |
| Data reason only | `coverage: <complete-or-incomplete>; device attribution: <device_state>; tier: <tier>; gaps: <gap_intervals>; suspends: <spans_suspend>; reason: <coverage.unavailable_reason>` |
| Client diagnostic only | `coverage: <complete-or-incomplete>; device attribution: <device_state>; tier: <tier>; gaps: <gap_intervals>; suspends: <spans_suspend>; reason: <coverage_diagnostic>` |
| Both | `coverage: <complete-or-incomplete>; device attribution: <device_state>; tier: <tier>; gaps: <gap_intervals>; suspends: <spans_suspend>; reason: <coverage.unavailable_reason>; <coverage_diagnostic>` |

With neither cause, append nothing. Do not insert extra spaces, reorder the causes, drop a contract token, or repeat `reason:` before the second clause. If no successful Coverage source exists, preserve `coverage: unavailable`; append `; reason: <coverage_diagnostic>` only when a diagnostic exists.

The client diagnostic clauses are exact:

| `DaemonAnswerError.kind` | Clause passed to `build_overview` |
| --- | --- |
| `remote_error` | `daemon reported an error (<reason>)` |
| `unavailable` | `no answering daemon (<reason>)` |
| `rejected` | `history query failed (<reason>)` |
| `transport` | `connection lost before the answer (<reason>)` |
| `internal` | `history query failed (<reason>)` |

For example, data reason `not_retained` plus `RemoteError(reason="bad_query")` yields a coverage suffix exactly `; reason: not_retained; daemon reported an error (bad_query)`.

`_device_unavailable_text()` reads `OverviewModel.coverage_diagnostic`. With no share and no `share_unavailable`, a diagnostic returns `device attribution unavailable: <diagnostic>`; with both no share and a `share_unavailable` value it returns `device attribution unavailable: <share_unavailable>; <diagnostic>`. With a share whose device attribution is unavailable and a diagnostic it returns `device attribution unavailable: <coverage.unavailable_reason or device_state>; <diagnostic>`. Without a diagnostic, preserve the existing behavior: no share returns an empty string, available device attribution returns an empty string, and an unavailable share returns `device attribution unavailable: <coverage.unavailable_reason or device_state>`. The data/query reason and the client diagnostic remain distinct and in that order.

**specification decision:** append the model field after `detail_line` with a `None` default. This preserves the existing positional field meanings and makes the additive interface usable by existing direct constructors.

**specification decision:** keep the full coverage prefix even when no data reason exists; diagnostic-only output appends `; reason: <clause>`. When no Coverage source exists, append the diagnostic to the existing `coverage: unavailable` text rather than suppressing it.

## 6. Systemd evidence and `silence_cause`

### 6.1 Probe argv, `ServiceFacts`, and parser

The exact argv constant becomes:

```python
UNIT_SHOW_ARGV = (
    "/usr/bin/systemctl",
    "show",
    "appio-iod.service",
    "--property=UnitFileState,ActiveState,SubState,Result,NRestarts",
    "--no-pager",
)
```

Append the following defaulted fields to `ServiceFacts`, after `pending_activation`, so the existing five positional arguments keep their meanings:

```python
sub_state: str | None = None
result: str | None = None
restarts: int | None = None
```

For `sub_state` and `result`, accept `None` or nonempty strings; strip systemctl output before construction. For `restarts`, accept `None` or an exact nonnegative `int`; reject bool, non-int, or negative values. Existing required-field validation remains unchanged.

The parser signature and extended tuple are:

```python
def parse_unit_facts(
    stdout: str,
) -> tuple[bool | None, str, str | None, str | None, int | None]: ...
# (enabled, active, sub_state, result, restarts)
```

Required keys remain exactly `UnitFileState` and `ActiveState`; optional keys are `SubState`, `Result`, and `NRestarts`. Blank lines remain ignored. A malformed nonblank line, duplicate key (including an optional key), missing required key, or unrequested/unknown key rejects the whole response as `(None, "unknown", None, None, None)`. Required key values use the current mappings. Optional text values are stripped and become `None` when absent or empty. `NRestarts` is accepted only when its stripped value consists entirely of ASCII decimal digits; otherwise it becomes `None` without poisoning required facts. Absence of any optional key is valid.

Required value mappings remain exactly:

| Input key/value | Parsed value |
| --- | --- |
| `UnitFileState` in `enabled`, `enabled-runtime`, `linked`, `linked-runtime`, `alias` | `enabled=True` |
| `UnitFileState` in `disabled`, `masked`, `masked-runtime`, `static`, `indirect` | `enabled=False` |
| Any other `UnitFileState` value | `enabled=None` |
| `ActiveState=active` | `active="active"` |
| `ActiveState=inactive` | `active="inactive"` |
| `ActiveState=failed` | `active="inactive"` |
| `ActiveState=absent` or `ActiveState=not-found` | `active="absent"` |
| Any other `ActiveState` value | `active="unknown"` |

`production_probe()` uses the five-value parse result when the command succeeds. A failed command, timeout, OS error, malformed required response, or unexpected key retains unknown required facts and optional values `None`, then observes the socket as before. When status answers, propagate the parsed systemd values into the returned `ServiceFacts`; when it does not, retain the parsed systemd values while `daemon_verified=False`, `answering=False`, and `pending_activation=None`.

**specification decision:** extend the existing parser's tuple by appending the three optional values, preserving the first two tuple positions. Unknown output keys still fail closed; malformed values of only the three recognized optional properties are ignored as `None`.

### 6.2 Exact cause clauses and joining

Exact signature:

```python
def silence_cause(
    facts: ServiceFacts,
    client_state: ClientState | None,
) -> str | None: ...
```

The owner state is `decide_owner(facts).state`. The function returns `None` for every state other than `enabled, not answering`, except the enabled-but-failed `off` case below. It also returns `None` in an eligible state when neither client nor systemd provides a clause. It does not change `display_state`, `decide_owner`, or any of the five fixed banner state strings.

Socket observations map exactly:

| Client observation | Exact socket clause |
| --- | --- |
| `client_state is None` | Omit socket clause. |
| `verified is True`, `answering is False`, `last_error is None` | `daemon status reports not answering` |
| `last_error == "daemon_absent"` | `socket: nothing listening at /run/appio/iod.sock` |
| `last_error == "connect_failed"` | `socket: connect failed` |
| `last_error == "path_unverifiable"` | `socket: socket path could not be verified` |
| `last_error == "peer_unreadable"` | `socket: peer credentials unreadable` |
| `last_error == "daemon_not_answering"` | `socket: connected, no answer within 5 s` |
| `last_error == "connection_lost"` | `socket: connection lost` |
| `last_error == "transport_lost"` | `socket: transport closed by the daemon` |
| Any `PeerRejected.reason` token, including `socket_not_connected` | `socket: <token>` |
| No client observation (`last_error is None`, and not verified/non-answering) | Omit socket clause. |
| Any other `last_error` token not identified by the client as one of the preceding observations or a `PeerRejected.reason` | Omit socket clause; do not invent or echo a cause. |

The complete `PeerRejected.reason` token set published by the current client connection paths is: `appio_group_missing`, `peercred_unsupported`, `socket_not_connected`, `peercred_unavailable`, `invalid_peercred`, `server_uid_nonzero`, `directory_missing`, `directory_metadata_invalid`, `directory_not_directory`, `directory_owner`, `directory_group`, `directory_mode`, `socket_missing`, `socket_metadata_invalid`, `path_symlink`, `socket_not_socket`, `socket_owner`, `socket_group`, and `socket_mode`. Each uses the generic `socket: <token>` clause.

Do not display `CONNECT_DEADLINE_NS` as a silence duration. The only client-side silence wording is the exact 5-second token clause above.

Systemd evidence clauses are ordered by this table:

| Evidence | Exact clause |
| --- | --- |
| `restarts is not None and restarts > 0` | `<restarts> restarts` |
| `result is not None and result not in ("success", "none")` | `failed: <result>` |
| `sub_state == "auto-restart"` | `restarting` |

For ordinary `enabled, not answering`, join the available socket clause first, then the systemd clauses in table order, using exactly `; ` between adjacent clauses. Omit unavailable clauses; do not emit empty separators.

The second eligible case is owner state `off` with `facts.enabled is True` and failure evidence: `sub_state == "failed"` or a present `result` other than `success`/`none`. Its leading clause is `enabled but not running: failed: <result>` when `result` is a failure value, or `enabled but not running: failed` when only `sub_state == "failed"` proves failure. Append any socket clause next, then any nonduplicate remaining systemd evidence clause(s), using `; `.

The banner's left column is `QVBoxLayout(banner_status, banner_detail)` inside the existing horizontal banner layout. `banner_detail` is word-wrapped, is hidden iff `silence_cause()` returns `None`, and otherwise contains exactly the returned string with no prefix. `_render()` updates it beside `banner_status`; state and settings labels remain unchanged.

**specification decision:** `parse_unit_facts()` returns an extended five-tuple because no new parsed-record type was approved and the current parser returns a tuple.

**specification decision:** join clauses with `; `; for the enabled-but-failed `off` case, place its special failure clause first, then the socket clause, then other systemd clauses in table order. If only SubState proves failure and no failure Result is present, use `enabled but not running: failed`.

**specification decision:** the two eligibility classes use `decide_owner(facts).state`, since the exact function signature has no activation probe argument. An eligible class with no observations still returns `None`, preserving the design's hidden-detail behavior when there is no evidence to render.

**specification decision:** unrecognized `last_error` strings outside the enumerated client observations and `PeerRejected.reason` tokens are omitted; they are not echoed or converted into guessed causes.

## 7. Files stall caption

Add this method to `FilesView`:

```python
def set_stalled(self, stalled: bool, text: str) -> None: ...
```

When `stalled is True`, `text` must be a nonempty string and becomes the exact visible `files_updating` caption. The window passes the same complete string currently shown in `history_state`, including the `history read has not completed (...)` prefix. When called as `set_stalled(False, "")`, clear the stall override and restore the existing `_update_updating_label()` result, including `updating to <scope.kind>` when ordinary update conditions hold.

The window updates the Files caption whenever it recomputes pending/stall status, including deadline expiry, retirement, retry-floor wait, explicit deferred request, successful acceptance, no-stall status, and frame application. It passes `False, ""` after application and whenever the derived stall label disappears. A busy label is not cleared by `show_window()` or an explicit refresh.

`set_stalled()` must not call `set_window()`, change `_updating_scope`, modify scope readiness or control enabled states, set/clear `_refresh_wanted`, retire deliveries, start/settle a request, change `last_result`, change the table model, or alter any footer quantity. It changes only the stall-caption override/visibility and then repaints that caption.

Invalid `stalled` type or invalid `text` type raises `ValueError`; a true state with empty text is rejected. False state clears the override; its text argument is otherwise unused.

**specification decision:** invalid argument types and a true state with empty text raise `ValueError`; `set_stalled(False, text)` clears the override for any string, while the window uses the canonical call `set_stalled(False, "")`. Requiring an empty string on false would add no behavior and would make the public setter needlessly brittle.

## 8. Error handling and exact visible text

This table covers feature-specific errors. `<...>` denotes an interpolated value; all surrounding text and separators are literal.

| Failure/condition | Required data/state handling | Exact visible text |
| --- | --- | --- |
| `systemctl show` fails, times out, or parser rejects malformed/duplicate/missing/unknown keys | Required facts are `enabled=None`, `active="unknown"`; optional facts are `None`. Socket status is still observed. | No systemd cause is invented. The owner banner retains the existing `display_state(facts, probe_state)` token; `banner_detail` is hidden absent independent cause evidence. |
| A required property has an unrecognized value but the key/value response is structurally valid | Keep the other required mapping; map the unrecognized value to `enabled=None` or `active="unknown"` per section 6.1. | No unsupported systemd cause is invented; independently observed socket clauses can still render. |
| Optional `SubState`, `Result`, or `NRestarts` is absent/malformed | Ignore only that optional value; keep required facts and other valid optionals. | No clause for the missing value. Any valid socket/systemd clause uses the exact clause tables in section 6. |
| Client has never connected and has no published error | Do not invent a socket refusal. | Systemd clauses only, joined in order; otherwise no detail line (hidden). |
| Pending read has not reached its budget | Keep the preparation and old applied frame. | Existing `loading history (<view_state>)` before a frame, or `updating (<view_state>)` with a frame. |
| Pending read reaches its budget and local worker is still running | Keep `_pending`; submit nothing else. | `history read has not completed (<view_state>); the reader is still busy` |
| Pending read is retired and local worker remains running | Record the wanted target and retry floor; retirement is not a daemon failure. | `history read has not completed (<view_state>); the reader is still busy` |
| Pending read is retired, worker is idle, retry floor is future | Do not submit before the floor. | `history read has not completed (<view_state>); retrying in <n> s` with positive ceiling seconds. |
| Read never returns | Keep it pending and do not queue another preparation behind the worker. | `history read has not completed (<view_state>); the reader is still busy` |
| Remote query settles with `RemoteError` | Set `DaemonAnswerError("remote_error", reason)`; do not set `_not_retained`; retain local rows. | Coverage suffix `; reason: daemon reported an error (<reason>)`; device strip per the conditional rule below. |
| Remote query settles with `ClientUnavailable` | Set kind `unavailable`; do not set `_not_retained`; retain local rows. | Coverage suffix `; reason: no answering daemon (<reason>)`; device strip per the conditional rule below. |
| Remote query settles with `RequestRejected` | Set kind `rejected`; do not set `_not_retained`; retain local rows. | Coverage suffix `; reason: history query failed (<reason>)`; e.g. `RequestRejected("too_many_requests")` renders `history query failed (too_many_requests)`, never a daemon-blame clause. |
| Remote query settles with `ConnectionLost` | Set kind `transport`; do not set `_not_retained`; retain local rows. | Coverage suffix `; reason: connection lost before the answer (<reason>)`. |
| Other current remote-query exception/response validation failure | Set kind `internal`; do not set `_not_retained`; retain local rows. | Coverage suffix `; reason: history query failed (<reason>)`. |
| Client diagnostic and actual data reason coexist | Preserve the data reason and diagnostic independently. | `; reason: <contract reason>; <per-kind clause>` exactly. |
| `invalidate()`, `WindowUnavailableError`, or query cancellation | Neither set nor clear `_not_retained` or `daemon_answer_error`. | No client-failure reason is added. Normal read/retry status is used if a preparation was pending. |
| Genuine pruned/unowned history | Set `_not_retained` only from actual materialized/returned data evidence. | Coverage appends `; reason: not_retained` (or the contract's exact token); existing device-strip rule remains data-owned. |
| `history.prepare()` raises `WindowUnavailableError` from its precheck (pre-loading window/A22 refusal) | Classified by the exception, never by `view_state`. Re-raise unchanged; create no `PrepareRetry`, no `_pending`, no stall state, no floor — even when a previous failure left `view_state == "loading"`. | The ratified A22/validation text verbatim, with no `appio:` prefix and no rewriting. |
| `history.prepare()` raises any other exception, submitted by the clock path | Do not install a frame or claim readiness; keep the prior frame; record `PrepareRetry(scope, owner="clock")`; arm no retirement floor. | `appio: <str(error)>`, or the exception class name when `str(error)` is empty. |
| `history.prepare()` raises any other exception, submitted by an explicit request (`show_window`, Recheck) | Same, but record `PrepareRetry(scope, owner="explicit")`. | `appio: <str(error)>`; no timer retry; only a later explicit request retries. |
| Local history readiness raises | Do not apply the pending frame; retain the old frame and let later target handling decide. | Existing timer/poll error surface `appio: <str(error)>`, or the exception class name if empty. |
| Invalid `coverage_diagnostic` or invalid `FilesView.set_stalled()` arguments | Reject with `ValueError`; do not mutate a render model or widget state. | No user-facing string is produced by the rejected call. |

**Device-strip rule (§8 applies §5.2 to the error table).** `_device_unavailable_text()` never
prints a client clause alone when an existing share or coverage reason is present. With an
unavailable share it prints `device attribution unavailable: <existing reason>; <client clause>`,
in that order; with no existing reason it prints
`device attribution unavailable: <client clause>`. An available share, or no share with no
diagnostic, still prints nothing.

No client-query failure may be represented as `not_retained`, an empty successful result, or a numeric zero. No stall label claims a bounded worker; a permanently running future remains honestly reported as busy.

## 9. Test inventory and implementation gates

These are required tests, not test results. A **Regression** is expected red against source commit `503f09707af8e42bae26a93b258f1fb89d22cfe2`. A **Control** is explicitly labelled and preserves behavior that should already work; it is not evidence for a new mechanism. Use virtual nanosecond clocks and Events/Futures for state transitions; do not sleep on the Qt thread. This drafting task did not run tests.

### 9.1 Coordinator - `tests/p3/test_collector_owner.py`

| Exact test name | Required assertion | Baseline class |
| --- | --- | --- |
| `test_reconcile_same_history_key_does_not_invalidate` | `reconcile(SILENT)` invalidates once; replacing `SILENT` with `active="active"` leaves invalidate count and `view_state` unchanged. | Regression |
| `test_reconcile_changed_history_key_invalidates_once_per_change` | Starting with no key, `SILENT -> OFF -> ON` invalidates exactly three times (once for the initial key and once for each changed key) and preserves the existing owner decisions. | Control |
| `test_reconcile_publishes_history_key_only_after_invalidation` | When key-change invalidation raises, `_history_key` remains unchanged; the next reconcile retries invalidation, and only success publishes the new key. | Regression |
| `test_tick_local_returns_committed_advance_without_invalidation` | A successful committed tick returns `True`, including when it writes zero application rows; an ineligible/no-local tick returns `False`, neither call invalidates, and exceptions propagate. | Regression |

### 9.2 Window liveness and retry - `tests/p3/test_collector_window.py`

The two liveness mechanisms below each have separate proof arms. The real-coordinator arm proves production invalidation policy; the window arm uses a production-compatible coordinator double and proves window behavior. Neither arm substitutes for the other.

| Exact test name | Required assertion | Baseline class |
| --- | --- | --- |
| `test_real_coordinator_probe_delta_liveness` | Ten real `CollectorCoordinator.reconcile()` calls alternating `SILENT` and `replace(SILENT, active="active")` produce one invalidation total, on the first call, and no later `view_state` movement. | Regression |
| `test_window_probe_delta_liveness_control` | With a held read and a coordinator double implementing the fixed same-key rule, the read survives the alternating facts, generation is unchanged, and no second prepare occurs. This isolates the window from the production-key arm. | Control |
| `test_real_coordinator_tick_advance_is_not_invalidation` | A real permitted `tick_local()` returns `True` and does not invalidate its counting history. | Regression |
| `test_window_local_tick_coalesces_invalidation_until_apply` | With local sampling active and one held preparation, repeated committed ticks keep `_pending` and generation unchanged, set one `_refresh_after_apply`, and cause one invalidation/replacement only after the held frame is applied. | Regression |
| `test_pending_read_budget_shows_busy_without_retiring` | At exactly `PENDING_BUDGET_NS`, `history_state` is `history read has not completed (loading); the reader is still busy`; `_pending` is identical and prepare count does not increase. | Regression |
| `test_retirement_while_local_worker_busy_defers_without_submission` | Retire a stalled read while the local worker remains active; `_deferred_target` and `_retry_after` are set, busy wording remains, and no preparation occurs even after retirement until worker/floor gates allow it. | Regression |
| `test_retirement_retry_floor_blocks_then_submits_once` | With worker idle, retirement shows the exact `retrying in <n> s` label and makes zero submissions before the floor; at/after the floor exactly one is submitted. The countdown asserts ceiling, not truncation: with 1 ns remaining the label is `1 s`, with exactly 1 s remaining `1 s`, with 1 s + 1 ns remaining `2 s`, and with the floor elapsed there is no retry label at all. | Regression |
| `test_prepare_failure_ownership_follows_submission_origin` | A clock-path `prepare()` failure records `PrepareRetry(owner="clock")` and retries from the timer; an explicit `show_window`/Recheck failure records `owner="explicit"` and produces no timer retry; neither arms `_retry_after`, `_stalled`, or `_deferred_target`. | Regression |
| `test_precheck_refusal_propagates_after_a_prior_loading_failure` | After an operational failure leaves `view_state == "loading"`, a window/A22 `WindowUnavailableError` from `prepare()`'s precheck still propagates verbatim, creates no `PrepareRetry`, and is not prefixed with `appio:` — proving classification is by the current call, not by router state. | Regression |
| `test_explicit_refresh_during_stall_preserves_deferred_target_and_floor` | With the local worker busy, five committed ticks and one explicit refresh submit zero preparations; deferred target and `_retry_after` survive and the busy label remains. | Regression |
| `test_show_window_during_stall_uses_submission_gate` | `show_window(B)` while A's worker is busy arms B, does not prepare B, and does not clear/change the floor or busy label. | Regression |
| `test_initial_read_passes_submission_gate_control` | No deferred target/floor and an idle worker submits one initial preparation; a second pass while it is pending submits none. | Control |
| `test_deferred_explicit_target_survives_other_frame_apply` | Request B while A is pending; A applies without clearing B; B is prepared once and only B's applied frame clears `_deferred_target`. | Regression |
| `test_apply_and_reprepare_clear_stall_state` | Applying a frame clears `_stalled` and `_retry_after`; accepting the post-retirement preparation clears them too; verify exact stall label/caption disappearance. | Regression |

### 9.3 Router and rendering - `tests/p4/test_file_history_router.py`, `tests/test_ui.py`

| Exact test name | Required assertion | Baseline class |
| --- | --- | --- |
| `test_remote_query_errors_are_typed_without_rewriting_data_reason` | One remote-settlement case per class maps `RemoteError`, `ClientUnavailable`, `RequestRejected`, `ConnectionLost`, and unknown `Exception` to the five exact kinds. A client failure leaves `Coverage.unavailable_reason` unchanged; genuine pruned-row evidence still yields `not_retained`. | Regression |
| `test_retirement_and_cancellation_do_not_set_history_failure_flags` | `invalidate()`, a `WindowUnavailableError`, and a cancelled query set neither data nor diagnostic flags; invalidation does not clear pre-existing values, while a new `prepare()` clears both flags. | Regression |
| `test_local_read_in_flight_tracks_every_unsettled_local_read` | An absent, done, or cancelled local future reports false; a **queued** and a **running** future both report true; the property never waits or mutates the Future. | Regression |
| `test_queued_local_read_blocks_every_replacement_submission` | Barrier-control a local read so its future is queued but unstarted; five ticks, one refresh, and one `show_window` submit zero preparations and arm `_deferred_target`; releasing the barrier to done admits exactly one. | Regression |
| `test_daemon_answer_error_rejects_invalid_kind_and_empty_reason` | Constructing `DaemonAnswerError` with an unknown kind, an empty reason, or a non-string reason raises `ValueError` and leaves router state unchanged. | Regression |
| `test_coverage_diagnostic_composes_with_contract_reason` | Assert the no-cause output and all three exact full cause-bearing outputs, default `None`, carry-through on all builder branches, direct-model validation, and rejection of invalid diagnostic values; no diagnostic can replace a contract reason. | Regression |
| `test_each_diagnostic_kind_matches_coverage_and_device_wording` | Assert all five exact clauses in `_coverage_text` and `_device_unavailable_text`, including absent/unavailable shares, preserved share/data reason ordering, and `RequestRejected("too_many_requests")` => `history query failed (too_many_requests)`. | Regression |

### 9.4 Probe, banner, and Files - `tests/p3/test_collector_window.py`, `tests/test_ui.py`

| Exact test name | Required assertion | Baseline class |
| --- | --- | --- |
| `test_parse_unit_facts_accepts_optional_systemd_properties` | Parse the five-property output; preserve required mappings and existing five positional `ServiceFacts` arguments; absent/malformed optional values become `None`; invalid direct optional fields are rejected; duplicate keys, malformed lines, missing required keys, and unknown keys fail closed. | Regression |
| `test_silence_cause_maps_every_published_socket_observation` | Drive each socket row from actual `ClientState` publications, including `daemon_not_answering`, all exact refusal tokens, omission with no observation, and exact clause strings. | Regression |
| `test_enabled_but_failed_unit_gets_off_cause_only_with_failure_evidence` | Enabled/inactive with failure `Result` or `SubState` renders `off` with the exact enabled-but-not-running clause; enabled/inactive without failure renders `off` and no cause. | Regression |
| `test_silence_cause_orders_systemd_evidence_and_combines_socket` | Test each evidence row and the combined socket/restart/result/substate string, including the special off-case order and exact `; ` separator. | Regression |
| `test_banner_detail_layout_wraps_and_is_state_gated` | `banner_detail` is below `banner_status` in the vertical status column, hidden iff the cause is `None`, and does not clip at shipped or minimum window width. | Regression |
| `test_files_stall_caption_preserves_retained_table_and_footer` | On the real `FilesView` with retained rows, `set_stalled(True, text)` overrides generic `updating to <kind>` exactly; `set_stalled(False, "")` restores it; rows, footer quantities, scope and request state remain unchanged. | Regression |
| `test_files_set_stalled_rejects_invalid_arguments` | A non-bool `stalled`, a non-string text, or `set_stalled(True, "")` raises `ValueError` and leaves the current caption unchanged; `set_stalled(False, <any string>)` clears the override. | Regression |
| `test_collector_window_wires_stall_caption_to_real_files_view` | Use a real `CollectorWindow` and its real `FilesView`, hold a local history read through the budget, observe the exact stall caption, then apply the frame and observe the caption clear without losing retained rows. | Regression |

### 9.5 Existing tests to migrate or retain

| Exact existing test name | Required update/assertion | Baseline class |
| --- | --- | --- |
| `test_pending_history_retains_a_until_applied_before_b` (`tests/p3/test_collector_window.py`) | Retain the assertions that target advance does not retire A, A applies before B, and B is then prepared and applied. | Control |
| `test_pending_history_invalidation_reprepares_once` (`tests/p3/test_collector_window.py`) | Replace immediate-reprepare expectations with the retry floor; assert no submission before the floor and exactly one after it. | Regression |
| `test_pending_history_changed_ownership_reprepares_once` (`tests/p3/test_collector_window.py`) | Keep owner transition assertions; remove fake-knob claims about reconcile invalidation and update reprepare timing to the retry floor. Key-change invalidation belongs to `test_reconcile_changed_history_key_invalidates_once_per_change`. | Regression |
| `test_disabled_launch_control_uses_exact_a22_reason` (`tests/test_ui.py`) | Preserve the exact A22 launch-window refusal string while adding client diagnostics elsewhere. | Control |
| `test_real_history_router_gates_rendering_until_ready` (`tests/p3/test_collector_window.py`) | Keep real-router readiness gating and assert no frame is visible before `ready()` succeeds. | Control |
| `test_window_keeps_in_flight_history_for_the_same_scope` (`tests/p3/test_collector_window.py`) | Keep the real-client liveness control: a responsive daemon populates the open window without an external refresh. | Control |
| `test_applied_scope_is_not_prepared_again_without_change_or_retirement` (`tests/p3/test_collector_window.py`) | Keep no-repeat behavior for an applied scope; when the router becomes unavailable with no pending preparation, allow one immediate gated refresh without arming a pending-read retry floor. | Control |
| `test_unchanged_facts_skip_reconcile_while_preparation_is_pending` (`tests/p3/test_collector_window.py`) | Keep the fast path: unchanged facts do not reconcile while pending, while the local tick count still advances. | Control |
| `test_retired_pending_preparation_is_replaced_once` (`tests/p3/test_collector_window.py`) | Replace immediate reprepare expectations with a deferred target, worker/floor gates, and exactly one submission after the floor. | Regression |
| `test_unit_file_states_map_to_probed_enablement` (`tests/p3/test_collector_window.py`) | Update tuple unpacking to five values and retain all existing `UnitFileState`/`ActiveState` mappings. | Regression |
| `test_a_malformed_or_failed_unit_query_is_unknown_not_off` (`tests/p3/test_collector_window.py`) | Update exact parser/probe tuple expectations while preserving fail-closed required facts and no false `off`. | Regression |
| `test_the_production_probe_uses_the_daemon_status_for_answering_and_pending` (`tests/p3/test_collector_window.py`) | Assert parsed systemd evidence is retained on both status-answering and silent paths; preserve status socket authority and pending activation checks. | Regression |
| `test_later_ticks_update_coverage_device_strip_and_reconciliation` (`tests/test_ui.py`) | Keep the later-frame refresh assertion: changed coverage and attribution repaint in place and reconciliation updates without changing existing item identity contracts. | Control |

### 9.6 Gates

1. Run the focused inventory after implementation, then the existing full suite under the repository's pinned environment. Do not reinterpret a control as proof of a new regression mechanism.
2. Prove both real-coordinator and window arms for probe deltas and sample ticks; the production-key result alone does not prove window coalescing, and the window-double result alone does not prove the production key.
3. Keep router classification separate from the data-reason tests. A failure must not pass by returning local data while silently stamping `not_retained`.
4. The stall wiring test must instantiate production `CollectorWindow` and production `FilesView`; a setter-only test or a fake caption is insufficient.
5. Existing production probe seams must cover all systemd and socket behaviour without root or systemd dependency; no privileged setup is a required *unit* gate.
6. Two live acceptance checks are still required before delivery, exactly as the approved design's §7.6 states, and they are separate checks:
   a. **Cause line** — on a host with the daemon installed, a deliberately crash-looping unit renders `enabled, not answering` with the socket clause, and after the start limit gives up it renders `off` with `enabled but not running: failed: …`.
   b. **Stall** — with the local reader deliberately held (large store or a database lock), the console leaves `updating (loading)` for `the reader is still busy` within the budget while the retained frame stays on screen. Stopping the daemon alone does not exercise this path.
7. A check whose evidence is missing is reported as not run; it is never inferred from a unit test that covers a different mechanism.

## 10. Specification decisions and exclusions

The following decisions fill only details the approved design leaves open:

1. **specification decision:** `_history_key` starts as `None` and is stored after successful owner reconciliation and any required invalidation; an exception cannot publish a key for history that was not successfully transitioned.
2. **specification decision:** `tick_local()` returns `False` for disallowed/no-local no-op and `True` only after a successful local tick commit; tick exceptions propagate unchanged.
3. **specification decision:** `_pending_since` is cleared when retirement clears `_pending`; a timestamp never outlives the pending read it measures.
4. **specification decision:** a still-pending read is not labelled stalled until the 30-second threshold; after retirement, worker/floor state immediately determines busy/retry wording. This preserves the explicit wait budget while keeping the status derived.
5. **specification decision:** retry countdown uses positive ceiling integer seconds.
6. **specification decision:** `DaemonAnswerError` is a frozen dataclass with exactly `kind` and `reason`; `IodClientError.reason` is authoritative and an untyped internal exception uses stripped text or class name.
7. **specification decision:** append optional parser values to the current return tuple, reject unknown systemctl keys, ignore malformed recognized optional values, and keep the required-key parser fail-closed. Direct `ServiceFacts` optional strings are nonempty when present, and `restarts` is an exact nonnegative int (not bool); `NRestarts` accepts ASCII decimal digits only.
8. **specification decision:** the `OverviewModel.coverage_diagnostic` field is appended with default `None` to preserve existing positional meanings; builder diagnostic-only output retains `coverage: unavailable` and adds the diagnostic reason suffix; device-strip composition retains an existing share/data reason before the diagnostic.
9. **specification decision:** the off/failure cause clause leads, clauses join with `; `, and a SubState-only failure uses `enabled but not running: failed`.
10. **specification decision:** `silence_cause()` uses `decide_owner(facts).state` because its ratified signature has no activation probe argument; with no available evidence it returns `None` and the detail widget stays hidden.
11. **specification decision:** invalid `FilesView.set_stalled` argument types and a true state with empty text raise `ValueError`; false state clears the override for any string, while the window uses the canonical empty string.
12. **specification decision:** the window does not set `_refresh_after_apply` when a reconcile has already retired the pending read; that key-change invalidation already covers the committed tick.
13. **specification decision:** consume `_refresh_after_apply` at the end of `_apply_frame()` after ordinary overview/Files synchronization, wrap its invalidation in `FilesView.transient_scope()`, and let the next `_tick_target()` pass prepare through the common gate.
14. **specification decision:** query-service doubles or alternate services without `daemon_answer_error` yield no coverage diagnostic; production reads the property after the selected subject's series/share attempts.
15. **specification decision:** unknown client `last_error` tokens outside the enumerated published observations are omitted rather than echoed or interpreted.

The read deadline does not terminate or reclaim a Python worker. The implementation reports a busy worker and does not submit behind it. It does not restart/repair `appio-iod`, alter systemd policy, invent connection causes, convert client failures into retention reasons, or change any ratified data/wire vocabulary.
