# appio Files tab — frontier implementation specification

**Status:** implementation specification drafted from approved design revision 7
**Authority:** `docs/superpowers/specs/2026-10-30-appio-files-tab-design.md`, revision 7, commit `b60e0d5`, SHA-256 `8b92de1b08307aff51b5ca569478bdf4f905f50082138463c322a54749021cf3`.
**Candidate under audit:** `dd8b042` (`fix(router): serve hourly file windows across disclosed gaps`).
**Scope of this document:** the Files query, provenance, disclosure, and label-preservation work only.

This document is normative for implementation lanes. Where it says **must**, an implementation that differs is not an alternative interpretation; it is a contract defect. No implementation may broaden the product scope below by adding a schema, migration, wire field, protocol message, ABI field, collection cadence, or Overview behaviour.

## 1. Scope and non-goals

### 1.1 Background and scope copied from the approved design

The Files tab has never rendered on the target host, and the header has misreported the daemon. File rows are hourly, so a file query must use an hour-aligned interval. The retained store contains approximately 710 legacy gaps; consequently every useful hour-aligned request can contain a gap. The longest gap-free stretch before the watermark is 614 seconds, below the hourly granularity. Router provenance over the whole request therefore refuses, and provenance over the served interval still refuses while an unproved or overlap-uncertain segment remains inside that interval.

The approved design's intended change is to serve an hour-aligned interval across known, countable gaps when all of the following hold: ownership is unambiguous; no overlap uncertainty is inside the served interval; every unproved segment is fully covered by known non-overlap gaps; and each such unproved segment is hour-aligned. Rows are rendered for the interval actually served, not for the larger requested window.

The header behaviour is a preservation target, not a repair target. Existing refresh logic promotes a verified, answering live connection over a silent one-shot probe. The activation probe override is deliberate. Existing unchanged code produces the health transitions that earlier drafts incorrectly called missing. This specification adds pins for the two uncovered transitions without changing precedence, witness authority, ownership reconciliation, `display_state`, or `decide_owner`.

The two scope clauses are named here so they cannot be lost during implementation:

1. **Honest-window clause:** for a requested window, the Files tab renders rows from the interval it served and discloses that interval plus the gaps inside it. It must not present requested-window bounds as the bounds of the data.
2. **Label-preservation clause:** the header's existing truthful behaviour and precedence are preserved, and the two uncovered status transitions are pinned. No label repair is claimed without a concrete failing observation.

### 1.2 Goals

1. For a requested window, the Files tab renders the rows of the interval it served and discloses that interval and the gaps inside it.
2. The header's existing truthful behaviour is preserved and its two uncovered transitions are pinned; no precedence or ownership change is made.
3. The served interval is exposed under an honest name without changing the wire contract.
4. No honesty rule weakens. In particular, an unavailable, unproved, ambiguous, or overlap-uncertain source is not converted into a zero-valued answer.

### 1.3 Non-goals copied from the approved design

No schema change, no migration, no new table, no wire-protocol or ABI change, no Overview redesign, no collection-cadence change, no new user-facing copy.

The implementation must preserve existing ownership, overlap, unavailable-source, and zero-versus-unknown honesty semantics. Required served-scope and gap disclosures below are specification of the already-approved disclosure goal, not authority to add unrelated user-facing concepts.

## 2. Data model contracts

### 2.1 Terminology

All intervals are half-open: `[start, end)`. A requested window is the `Window` supplied by the caller. For a successful Files result, the served interval is the effective interval from which `FileTable.rows`, `FileCoverage.observed_bytes_r`, and `FileCoverage.observed_bytes_w` were read.

For an hourly range, an interval is hour-aligned when both endpoints are multiples of `FILE_HOUR_NS` (`3_600_000_000_000` nanoseconds). The interval must also lie within the requested bounds and not extend beyond the file finalization watermark.

A **gap row** is a persisted `gap` record whose interval has a non-empty intersection with the interval being counted. Counts are counts of intersecting persisted records, not byte totals and not a deduplicated union length. Overlap-uncertain records remain separately visible through `overlap_uncertain_intervals` and are never treated as known non-overlap gaps for provenance admission.

### 2.2 `FileServeFacts`

Add a local, non-transported frozen value object owned by `QueryService` (the exact module placement may follow repository conventions, but the public shape is fixed):

```python
@dataclass(frozen=True)
class FileServeFacts:
    served_interval: Window | None
    gaps_in_served_interval: int | None
    gaps_in_requested_window: int
```

The fields mean exactly:

| Field | Type | Semantics |
| --- | --- | --- |
| `served_interval` | `Window \| None` | The effective interval used by the file query. For an hourly query this is the selected largest contiguous hour-aligned interval. It is `None` when no interval is servable. For a complete result it equals the requested scope. For non-hourly/totals-only scopes, it is the requested `Window` when that scope is available; it is not converted into invented monotonic endpoints. |
| `gaps_in_served_interval` | `int \| None` | The number of persisted gap rows intersecting `served_interval`. It is `None` exactly when `served_interval is None` because there is no interval to count. It is `0` when a served interval exists and no gap row intersects it. Zero is evidence of a measured gap-free served interval; it is not the same as “not known.” |
| `gaps_in_requested_window` | `int` | The number of persisted gap rows intersecting the original requested window, including rows outside the served interval. It is always an integer, including when no interval is servable. `0` means no intersecting persisted gap row was found; it does not turn a missing ownership or missing file source into a complete result. |

The method is:

```python
def file_serve_facts(self, window: Window) -> FileServeFacts:
    ...
```

It must use the same read snapshot and the same requested bounds as the corresponding file query. It must not perform a second, differently scoped query that can produce facts for a different database state. It must not be serialized or added to any daemon response.

For hourly `range` and `since_boot` requests, the method computes `served_interval` from the same selected interval used by `file_applications`, `file_table`, `file_coverage`, and `unknown_owner_file_coverage`. A reader that computes rows using one interval and facts using another is a contract defect.

When no interval exists, the query layer may still construct an internal unavailable `FileCoverage` for error propagation, but the router must refuse it and the view must render outcome 1 below. No caller may interpret the unavailable placeholder window as a served interval.

### 2.3 `FileCoverage` and its computed `served_interval`

`FileCoverage` remains the exact existing dataclass field set. In particular, these fields remain unchanged:

- `app_id`
- `window`
- all omission, overflow, skipped-object, weak-identity, hook, and observed-byte fields
- `unknown_owner_overflow`
- `coverage`

`FileCoverage.window` continues to mean the original requested window. It must not be overwritten with the served range.

Expose `served_interval` as a computed property, not a dataclass field and not a serialized field:

```python
@property
def served_interval(self) -> Window | None:
    ...
```

For a successful `FileCoverage`, the property returns the interval represented by the existing `unknown_owner_overflow.window` field. It returns `None` for an unavailable lower-level result. A complete result has `served_interval == window`; a partial hourly result has a `range` `Window` with the selected served bounds. The property must not change the dataclass field order, equality shape, durable encoder, decoder, or IPC representation.

The `unknown_owner_overflow.window` field remains the compatibility carrier for the served scope. Its meaning is corrected by this specification: it is the interval served, not an assertion that the interval is an unknown-owner overflow interval. The new computed property is the honest API name; no wire rename is permitted.

### 2.4 `FileViewResult`

`HistoryRouter.FileViewResult` is local-only and gains exactly one field:

```python
@dataclass(frozen=True)
class FileViewResult:
    table: FileTable
    coverage: FileCoverage
    served_gap_count: int | None
```

`served_gap_count` has these exact semantics:

- On the local-store route, it is `FileServeFacts.gaps_in_served_interval`. Therefore it is a non-negative integer for a result with a served interval; `0` means the local route proved no gap row intersects that interval.
- On the daemon/remote route, it is always `None`. Protocol 1 transports `FileTable` and `FileCoverage`, but not the local gap-row layout needed to prove this count. `None` means “not provable on this route,” not zero.
- It is never a substitute for `coverage.coverage.gap_intervals`, which remains the requested-window coverage scalar.
- It is never transported, encoded, decoded, cached as a wire field, or used to alter `FileCoverage` equality.

The router must verify that the table, coverage, served interval, and served-gap count were obtained for one snapshot and one scope. A result that combines a requested-window table with served-interval coverage is invalid.

### 2.5 Existing `FileCoverage` transport contract

The state `FileCoverage` and its nested `Coverage`, `HookHits`, and `UnknownOwnerOverflow` values are transported exactly as before. The encoder and decoder field sets must not change. Exact-field-set decoding remains mandatory. No `served_interval`, `gaps_in_served_interval`, or `served_gap_count` key may be added to durable JSON, IPC payloads, HMAC cursors, protocol messages, or ABI records.

A computed property is allowed because it has no serialized representation. A local `FileServeFacts` or `FileViewResult.served_gap_count` field is allowed because neither crosses the route boundary.

### 2.6 Requested versus served values

The following invariant is mandatory:

```text
FileTable.window == FileCoverage.window == requested_window
FileCoverage.served_interval == effective query interval
FileTable.rows and observed_bytes_* are scoped to FileCoverage.served_interval
```

For a partial result, `observed_bytes_*` must not be labelled “whole window.” For a complete result, the requested and served scopes coincide and either “requested window” or “served interval” is truthful, but the implementation must use the canonical text in section 3 rather than infer scope from whether rows happen to be non-empty.

## 3. Ordered rendering outcomes and exact disclosure

### 3.1 Scope text formatter

The view must use one canonical scope disclosure in every populated or empty terminal result. It must not recover bounds by looking at the caller's current window after a result has arrived.

For a served range, format bounds exactly as:

```text
T0={elapsed(T0)} through T1={elapsed(T1)} after boot
```

For a non-range scope, use its existing canonical window name (`since_boot`, `boot`, or `lifetime`) and do not invent T0/T1 values. The following canonical scope text is used for hourly range results:

```text
served interval{ only if served != requested}: {served_bounds}; requested window: {requested_bounds}; gaps inside served interval: {served_gap_text}; gaps in requested window: {requested_gap_count}
```

There is no space before the colon after `interval`; the `only` form is literally `served interval only:`. `served_gap_text` is:

- the decimal local integer, including `0`, on the local route; or
- exactly `unavailable on daemon route` when `served_gap_count is None`.

For example, a local result served from 2 hours through 4 hours for a request ending at 6 hours must contain exactly:

```text
served interval only: T0=2h 0m 0s through T1=4h 0m 0s after boot; requested window: T0=0s through T1=6h 0m 0s after boot; gaps inside served interval: 1; gaps in requested window: 3
```

A result whose served and requested ranges are equal must say `served interval:`, not `served interval only:`.

The canonical scope text is placed in `footer_state`. The same served/requested scope names must also be present in `footer_observed` and in every coverage-counter footer line that reports omission, overflow, unknown-owner, skipped-object, weak-identity, or hook data. This prevents a footer line from silently reverting to whole-request language.

### 3.2 Five outcomes, evaluated in order

The renderer evaluates these predicates top-to-bottom. The first matching rule is the only outcome. The internal names are normative test labels; the visible strings are normative copy.

#### Outcome 1 — `unavailable`

**Predicate:** no `served_interval` exists, or any router/query refusal fires, or the returned coverage has an unavailable reason.

**Visible placement:** `state_label`; clear the model and all numeric footer lines. Do not render rows, zeroes, a served interval, or a gap count.

**Exact disclosure:**

```text
file history unavailable: {reason}: {message}
```

`{reason}` is the contract reason when present (`not_retained`, `unsupported_window`, `launch_attribution`, or `file_source_unavailable`). If no typed reason is available, omit the `: {reason}` segment and use:

```text
file history unavailable: {message}
```

The existing `WindowUnavailableError` reason and message must be preserved. In particular, a failed file read is not rendered as an empty table.

#### Outcome 2 — `rows`

**Predicate:** a valid served interval exists and `len(FileTable.rows) > 0`.

**Visible placement:** rows remain in the query-returned order; `footer_state` is the state disclosure; `footer_observed` reports observed bytes and the number of retained rows. Search-filtered rows still use this outcome when at least one row matches.

**Exact `footer_state` disclosure when no filter is active:**

```text
rows retained; {scope_text}; coverage: {complete_or_incomplete}
```

**Exact `footer_state` disclosure when a filter is active:**

```text
rows retained; matching {search!r}; {scope_text}; coverage: {complete_or_incomplete}
```

`{complete_or_incomplete}` is exactly `complete` or `incomplete` from the returned coverage. The scope text must be inserted unchanged from section 3.1.

**Exact observed-line prefix:**

```text
observed (served interval{ only if served != requested}): {read_bytes} read, {write_bytes} written; {row_count} retained file row(s); {scope_text}
```

The existing byte formatter supplies `{read_bytes}` and `{write_bytes}`. A search suffix may follow exactly as today:

```text
; table filtered to {row_count} row(s) matching {search!r}
```

The observed line must not call the bytes “whole window” when the served interval differs from the request.

#### Outcome 3 — `no_matches`

**Predicate:** a valid served interval exists, a search filter is active, and `len(FileTable.rows) == 0`.

A filter is active when the normalized query search value is not `None`; whitespace-only input is not active.

**Visible placement:** `footer_state` is the empty-result state. The table is empty. The observed line still reports aggregate observed bytes separately from the filtered row count.

**Exact `footer_state` disclosure:**

```text
no retained file matches {search!r}; {scope_text}; coverage: {complete_or_incomplete}
```

This claims only that no retained file matches the filter. It must not say that no file activity occurred. The observed line must end with the existing filtered-count form, with zero rows, and must retain the served/requested scope names.

#### Outcome 4 — `empty_proven`

**Predicate:** a valid served interval exists, no search filter is active, `len(FileTable.rows) == 0`, and `coverage.complete is True`.

**Visible placement:** `footer_state` is the empty state; the model remains empty; coverage counters remain visible and are not replaced by unavailable markers.

**Exact `footer_state` disclosure:**

```text
no retained file activity for this application in this window; {scope_text}; coverage: complete
```

This is a per-application statement. It does not claim that no application was observed, and it does not replace the separate no-application selector state.

#### Outcome 5 — `empty_partial`

**Predicate:** a valid served interval exists, no search filter is active, `len(FileTable.rows) == 0`, and `coverage.complete is False`.

**Visible placement:** `footer_state` is the empty state; the model remains empty; both requested-window and served-interval gap disclosures remain visible. The incomplete coverage state must not be collapsed into `empty_proven`.

**Exact `footer_state` disclosure:**

```text
no retained file activity in the served interval; coverage: incomplete; {scope_text}
```

This exact form is used even when `served_gap_count` is `0`; incomplete coverage can arise from facts other than a known gap. On the remote route it must contain `gaps inside served interval: unavailable on daemon route`, not `0`.

### 3.3 Separate empty-application selector state

If the app selector has no applications before a file-table query is issued, retain the existing selector state:

```text
coverage: not applicable; no applications observed in this window
```

This state is not one of the five per-application file-table outcomes. It must not be used to hide an unavailable file source, and it must not be inferred from a zero-row result for a selected application.

## 4. Per-route disclosure and no wire change

### 4.1 Local-store route

The local route reads one or more local/exported SQLite stores through `QueryService`. It may prove:

- the requested bounds;
- the effective served bounds;
- the persisted gap rows intersecting the served interval;
- the persisted gap rows intersecting the requested interval;
- ownership and overlap predicates from the local provenance plan.

The local route must call `file_serve_facts` against the same snapshot used to obtain the table and coverage. It sets `FileViewResult.served_gap_count` to the facts' `gaps_in_served_interval`. It uses the existing requested-window coverage scalar for the requested-window disclosure and must not replace that scalar with the served count.

### 4.2 Daemon/remote route

The remote route receives only the existing `FileTable` and `FileCoverage` response records and must preserve their snapshot and window checks. It can disclose the served bounds carried by the existing `unknown_owner_overflow.window` compatibility field. It cannot prove the served-gap count because the protocol does not transport the local `gap` rows or an equivalent field.

The remote route therefore sets:

```python
FileViewResult.served_gap_count = None
```

The view must display `unavailable on daemon route` for the served-gap value. It may display the existing requested-window coverage scalar. It must not copy that scalar into the served-gap slot, infer zero from a complete-looking response, or hide the missing scope.

### 4.3 Scope unprovable

If a route cannot prove a valid served interval, a route cannot bind the rows and coverage to the same snapshot/window, or a required provenance refusal fires, the route returns `WindowUnavailableError` and the view uses outcome 1. It does not return a zero-row `FileViewResult` as a proxy for “nothing happened.”

No field is added to `QueryRequest`, the server response, `FileTable`, `FileCoverage`, the durable JSON, the HMAC cursor, or any ABI. A local-only `FileServeFacts` and a local-only `FileViewResult.served_gap_count` are the complete addition.

## 5. Provenance predicates

The router evaluates provenance over the served interval, not automatically over the requested interval. Let `R` be the requested half-open interval and `S` be the selected served interval. A source segment intersects `S` when its interval has a non-empty half-open intersection with `S`.

### 5.1 Existence and requested-scope facts

`S` must exist before any file rows are accepted. For an hourly request, `S` is the interval selected by `_largest_contiguous_file_interval` and must satisfy:

```text
S.start >= R.start
S.end <= R.end
S.start < S.end
S.start % FILE_HOUR_NS == 0
S.end % FILE_HOUR_NS == 0
S.end <= file_finalized_through_mono_ns
```

The selected interval is the largest contiguous run of present finalized file hours; ties use the existing helper's deterministic end-bound tie-break. No implementation may choose a shorter interval merely to avoid a provenance refusal.

### 5.2 Single-owner predicate

The single-owner predicate is true exactly when:

```text
there is exactly one collector_id among proven segments intersecting S,
no second collector_id has proven coverage intersecting S,
and all rows used for S come from that one collector_id.
```

Mirrors of the same collector receipt do not become additive owners. A source or owner outside `S` does not disqualify the result. A handover boundary inside `S` is allowed only when the router's existing ownership plan proves the boundary and still reduces to one authoritative store for the complete served result; otherwise it is an ownership ambiguity and refuses.

Failure reports:

```text
reason = "file_source_unavailable"
message = "File ownership cannot be represented by one store."
```

The existing local “no authoritative file app receipt” error remains the report when no source has an app receipt at all:

```text
reason = "file_source_unavailable"
message = "No authoritative file app receipt."
```

### 5.3 Proven-segment predicate

For every segment intersecting `S`, one of these must hold:

1. the segment is proven by the single authoritative collector; or
2. the segment is unproved, its entire interval is covered by the union of known non-overlap gap rows for the same boot, and both segment endpoints are hour-aligned.

Coverage is full interval coverage, not “there is a gap row somewhere in the segment.” The union must have no uncovered hole. A gap row with reason `overlap_uncertain` is not a known non-overlap gap and cannot satisfy this predicate.

An unproved segment outside `S` does not disqualify the file result. The router must not widen `S` to include it.

Failure reports the same `file_source_unavailable` / `File ownership cannot be represented by one store.` pair. The result must not expose rows from the otherwise unproved segment.

### 5.4 Overlap-uncertainty predicate

The overlap predicate is true exactly when there is no overlap uncertainty intersecting `S`:

```text
worker._overlap_ranges has no non-empty intersection with S
and no persisted gap row with reason "overlap_uncertain" intersects S.
```

An ambiguous candidate set from two owners/epochs is overlap uncertainty even if the resulting counters happen to be zero. The router must refuse rather than select a winner by row order, freshness, byte total, or a green test result.

Failure reports `file_source_unavailable` with `File ownership cannot be represented by one store.`

### 5.5 Gap-coverage predicate

The gap-coverage predicate is true exactly when every unproved segment intersecting `S` is fully covered by known non-overlap gaps and has hour-aligned endpoints. It is evaluated independently of the requested-window gap count.

For the canonical fixture:

```text
requested R = [0h, 6h)
served S   = [2h, 4h)
gaps       = [1h, 2h), [3h, 4h), [4h, 5h)
```

`gaps_in_served_interval == 1` and `gaps_in_requested_window == 3`. The interval `[1h,2h)` and `[4h,5h)` must not be mistaken for gaps inside `S`; a request-and-serve-same-interval fixture is insufficient evidence.

### 5.6 Process-history gap scoping

The file route may keep separate known-gap ranges for its served-interval admission, but `_gap_ranges` must remain scoped to the requested process-history window. `_gap_ranges` feeds process coverage and must not be replaced by the served interval's ranges. A separate `_known_gap_ranges` (or an equivalent local name) may be used for file admission.

This separation is mandatory even when the file result is partial. A gap outside the requested process window must never mark process history incomplete; a gap outside the served file interval may be disclosed as part of the requested-window count but must not be counted as a served gap.

## 6. Adoption of `dd8b042`

### 6.1 Audit scope

The candidate audit is read-only and limited to:

- selection and propagation of the served interval;
- local provenance over that interval;
- known-gap coverage and overlap refusal;
- preservation of requested-window process-history coverage;
- local and remote disclosure of bounds and gap counts;
- the candidate tests' mechanism, not just their assertions.

The audit does not treat the candidate as an implementation of this whole specification. It must inspect the diff and its callers, then compare each mechanism with sections 2–5 and the approved design.

The audit must not load BPF, use root, modify source or tests, or run the full test suite during this drafting/assessment gate.

### 6.2 Allowed outcomes

- **Pass:** the candidate satisfies every contract in this specification, including requested-window `_gap_ranges` scoping, and its tests falsify the mechanisms rather than only asserting a green result. No source repair is made.
- **Repair:** a defect is local and the design mechanism is sound. Repair only the defective mechanism, add or correct the pin that falsifies it, and re-audit the changed bytes and scope.
- **Replace:** the mechanism is unsound or cannot be repaired without reinterpreting the design. Implement the section 5 mechanism afresh and record why the candidate was not adopted.

The visible `dd8b042` diff deletes the assignment that scopes `_gap_ranges` to `scoped` and introduces a separate known-gap list. Therefore it cannot be accepted as-is: the audit must at least produce a **repair** verdict for that deletion, unless the audit proves the candidate branch has an equivalent requested-window assignment elsewhere. A replacement is required if the served-gap mechanism itself cannot satisfy the predicates above.

### 6.3 Non-negotiable adoption conditions

1. **Restore requested-window `_gap_ranges` scoping.** The candidate's served-gap admission logic must not widen or narrow `_gap_ranges`, because those ranges feed process-history coverage at the existing process paths. Add a regression pin in which a gap outside the served interval but inside/outside the requested process scope proves the distinction.
2. **A green suite is not evidence.** Correctness must be shown by targeted contract pins, falsifying mutations, and the real-database acceptance check required by section 8. The preceding live-host investigation had multiple green-but-wrong changes; a passing aggregate suite cannot substitute for mechanism evidence.

## 7. Label preservation only

### 7.1 Existing precedence that must not change

The implementation must preserve all of the following:

1. `CollectorWindow.refresh()` obtains `facts = setup.refresh()` from the unprivileged periodic status probe.
2. When the current `IodClient` connection is both `verified` and `answering`, refresh promotes `daemon_verified=True` and `answering=True` for the displayed facts. The live status round-trip is stronger evidence than a silent one-shot probe.
3. The root activation probe's answer may refine activation state, but `_apply_body` must not use the root helper's connection as the daemon-answering witness.
4. `display_state(facts, probe_state)` continues to start with `decide_owner(facts).state`, and an activation state overrides it only under the existing `probe_state in ACTIVATION_STATES and facts.enabled is not False` condition.
5. `coordinator.reconcile()` continues to use `decide_owner`; display refinement must not split ownership reconciliation from the owner decision.
6. `display_state` and `decide_owner` behaviour must not change in this task.

### 7.2 Label witness

The witness that may assert daemon answering is the **unprivileged status-socket observation** produced by `production_probe` / `SetupClient.refresh()` and the verified answering `IodClient` status connection. It is not the root activation probe. The root helper may report activation/accounting state but cannot establish that the GUI's daemon status socket is answering for this label.

### 7.3 Two transitions to pin

The implementation must add or preserve these two transition tests:

| Pin | Sequence | Required result |
| --- | --- | --- |
| Probe-only truth | A verified connection reaches EOF; the unprivileged status probe answers on each of the next three refreshes. | The label remains an answering state throughout. A silent stream alone must not demote a healthy daemon witnessed by the status probe. |
| Recovery | Connected; the connection reaches EOF and the status probe is silent for one refresh; the label becomes exactly `enabled, not answering`; the status probe answers again. | On the following refresh, the label returns to answering without restart. The connection must also become verified after reconnection; a label-only probe result is insufficient. |

The recovery test must assert the intermediate demotion. A test that sees only “answering” before and after cannot detect a frozen label or stale facts. Existing tests for explicit recheck/activation, verified status precedence, and pending activation remain required.

The mutation battery must freeze refresh or retain stale answering facts and must make the corresponding pin fail. No label repair is claimed by this specification.

## 8. Test plan per contract

All fixtures below are synthetic unless explicitly marked “real database snapshot.” They must use committed SQLite state and synthetic receipts/hourly observations; they must not load BPF, use root, or depend on a live daemon.

| Contract pin | Fixture shape | Falsifying mutation | Real database snapshot? |
| --- | --- | --- | --- |
| `FileServeFacts` field/type semantics | Request `[0h,6h)`; present/served hours `2h,3h`; persisted gaps `[1h,2h)`, `[3h,4h)`, `[4h,5h)`; assert served `[2h,4h)`, served count `1`, requested count `3`. Add a no-served fixture and assert `served_interval is None`, served count `None`, requested count is an integer. | Reuse requested bounds for all queries; count only gaps in `S`; turn no-served count into `0`; use a request equal to served. | No. |
| Served interval propagation | Use rows at hours 2 and 3 and a request through hour 6. Assert `FileTable.window` and `FileCoverage.window` remain the request, `FileCoverage.served_interval` is `[2h,4h)`, and observed bytes/rows contain only hours 2 and 3. | Leave `file_applications`, `file_table`, or `file_coverage` on the requested bounds; use `unknown_owner_overflow.window` only for the footer while querying rows from the request. | No. |
| Complete result | One hour-aligned request with all finalized hours and no gaps. Assert served equals requested, `served_gap_count == 0`, `coverage.complete is True`. | Treat `0` as unknown/`None`; mark complete result partial merely because no unknown-owner rows exist. | No. |
| FileCoverage wire shape | Encode/decode a representative `FileCoverage` with nested coverage and unknown-owner values; compare exact field sets and round-trip values. | Add any served field to the durable/IPC object; relax exact-field-set decode; encode a computed property. | No. |
| Local result shape | Local `HistoryRouter` returns `FileViewResult(table, coverage, served_gap_count)` with an integer count and one snapshot. | Omit the facts call; derive count from `coverage.gap_intervals`; combine results from different snapshots. | No. |
| Remote result shape | Drive the existing remote harness through real `IodClient` encoding/decoding and HMAC cursors. Assert bounds are disclosed, `served_gap_count is None`, and requested coverage count remains available. | Set remote served count to `coverage.gap_intervals`; invent zero; add a wire field. | No. |
| No authoritative source | Delete the file app receipt or remove the file calendar so no interval is servable. | Return a zero-row successful result; use `unsupported_window`/`not_retained` as a numeric empty result without the typed refusal. | No. |
| Ambiguous owner | Add overlap, split, and disjoint second-store ownership fixtures. Keep an unrelated owner outside `S` as a control that must not refuse. | Choose a store by freshness, row order, or byte total; let any owner outside `S` refuse the served interval. | No. |
| Unproved segment | Remove an ownership interval or add a gap hole inside `S`; separately add a known non-overlap gap that fully covers an hour-aligned unproved segment. | Accept any gap row somewhere in the segment; accept overlap-uncertain rows as known gaps; accept a non-hour-aligned unproved segment. | No. |
| Overlap uncertainty | Add an `overlap_uncertain` row intersecting `S`, with byte totals that happen to be zero, and a control row outside `S`. | Ignore overlap ranges because totals are zero; use requested-window overlap state for a served interval without intersection testing. | No. |
| Requested `_gap_ranges` | Request a process window larger than `S`; put one gap inside the request but outside `S`, then a second gap outside the request. Assert process coverage reflects only the requested scope while file served count reflects only `S`. | Replace `_gap_ranges` with served ranges; let the outside-request gap mark process history incomplete; let the outside-served gap become a served gap. | No. |
| Ordered rows outcome | Return at least one row, including a partial result and a search-filtered result. Assert outcome 2 wins over every empty outcome and order is preserved. | Run the empty predicates before rows; sort/filter rows in the view; label observed bytes as whole-window bytes. | No. |
| Filtered zero outcome | Valid result, active normalized search, zero matching rows, aggregate observed bytes nonzero or unknown. | Use `empty_partial` for a filtered zero; claim no file activity; omit the filter from the state string. | No. |
| Complete empty outcome | Valid complete result for an app with no retained file rows but complete file coverage. | Render unavailable or “no applications observed”; use partial wording. | No. |
| Partial empty outcome | Valid served interval, no filter, zero rows, `coverage.complete=False`; local count `0` and remote count `None` must both be covered. | Treat zero rows as complete; omit one of the two gap scopes; render remote `None` as `0`. | No. |
| Exact disclosure placement | Assert `footer_state`, `footer_observed`, and each populated coverage footer line contain the correct served/requested names and bounds; assert empty-state text is in `footer_state`. | Put the scope only in the table header or only in `footer_state`; leave the observed line saying “whole window”; derive labels from the current request after a stale reply. | No. |
| Stale reply | Defer two requests; retire the first and publish the second. | Publish a retired generation's rows or served facts. | No. |
| Label probe-only truth | Fake the status socket to answer after stream EOF for three refreshes. | Let EOF demote despite a healthy status witness; let the root activation probe assert answering. | No. |
| Label recovery | Fake a connected state, one silent status refresh after EOF, then an answering status refresh and reconnection. Assert the intermediate exact label and later verified connection. | Freeze the label; retain stale `answering=True`; assert only the final label without verified reconnection. | No. |
| Candidate adoption | Read-only diff audit plus targeted candidate tests and the `_gap_ranges` mutation. | Declare adoption solely because the aggregate suite is green. | No. |
| End-to-end acceptance | Offscreen Files view over a frozen SQLite snapshot containing the canonical request/served/gap fixture; assert rows, bounds, both gap scopes, and label answering while the stream is verified. | Use a request whose served interval equals the request; use a live daemon as the only evidence; assert only that the widget is non-empty. | **Yes.** This is the only required acceptance pin that uses a real database snapshot rather than an in-memory/synthetic fixture. |

The canonical request/served fixture is mandatory for every relevant test group. A request-and-serve-same-interval test does not count as evidence for the two gap scopes because a reused requested count would pass unchanged.

Every mechanism mutation must be run against a clean baseline, observed to fail at the named assertion, and reverted byte-for-byte before the positive result is recorded. A passing test that was already green before the mutation is not a falsification.

## 9. Risk register

### 9.1 Risks carried from the approved design

1. **Hour-aligned eligibility narrows what serves.** Selection serves whole-hour intervals and accepts unproved segments only when they are hour-aligned and fully covered by known non-overlap gaps. The result is hour-granular, not minute-granular. A store whose recent history is one sub-hour gap can serve nothing until an eligible hour boundary exists.
2. **Computed-property cost.** Keeping `served_interval` computed protects the wire contract but requires a pure derivation from existing fields. It must be O(1), side-effect free, and must not requery SQLite. `FileServeFacts` is the one snapshot-local computation of gap counts; the view must not scan gap rows.
3. **Remote unprovability.** The daemon route can disclose bounds but cannot prove the served-gap count with protocol 1. The UI must expose `unavailable on daemon route`, not silently reuse the requested count or claim zero.
4. **Label authority.** The two-witness rule resolves both directions but makes “answering” a statement about the daemon status witness, not merely the GUI stream. Later copy must not imply that it means the stream is healthy without revisiting the label rule.

### 9.2 Implementation-specific additions

5. **Requested/served scope regression.** A helper may accidentally reuse the selected served interval for process-history coverage. Keep `_gap_ranges` requested-scoped and give file admission its own known-gap collection.
6. **Legacy gap multiplicity.** Counting merged coverage ranges instead of persisted gap rows can change the disclosure and the canonical `1` versus `3` fixture. Define and test the count; do not deduplicate silently.
7. **App coverage versus provenance coverage.** `coverage.gap_intervals` includes the existing app/file coverage overlays. Do not replace it with `served_gap_count`; the two values answer different scopes and may legitimately differ.
8. **Unknown-owner compatibility carrier.** Renaming or replacing `unknown_owner_overflow.window` in the wire/durable model would be a schema/protocol change. Use the computed property and local facts instead.
9. **Snapshot skew.** Calling `file_table`, `file_coverage`, and `file_serve_facts` in separate snapshots can disclose bounds for one state and rows for another. The local worker must keep one committed snapshot for the result.
10. **Remote pagination.** A paginated remote table must preserve the first page's metadata, snapshot, requested window, sort, direction, and search. A later page must not widen the served interval or invent gap evidence.
11. **Tie-breaking and empty intervals.** The largest-contiguous helper's tie-break and the `None` distinction are externally observable. Do not turn “no interval” into a zero-length `Window` or a zero gap count.
12. **Footer repetition.** Updating only the state footer can leave observed/omission lines saying “whole window.” Scope text must be generated once and applied consistently to all coverage lines.
13. **Zero-row ambiguity.** A monitored app with no supported file observations can produce partial coverage with zero rows. The implementation must reach `empty_partial`, not `unavailable` or `empty_proven`, when the source and interval are otherwise valid.
14. **Status-refresh churn.** Replacing an in-flight history request on every refresh can prevent a result from ever publishing. Preserve the existing `keep_pending` behaviour while adding the label pins.

## 10. Approved-design ambiguities resolved by this specification

The approved design does not give a complete field type table, exact copy for every ordered outcome, or an explicit `None` rule for the no-served gap count. This specification resolves those points as follows:

- `served_interval` is `Window | None`; `gaps_in_served_interval` is `int | None`; `gaps_in_requested_window` is always `int`.
- A local `served_gap_count` of `0` is a proved zero; a remote `None` is not provable and must be disclosed as such.
- The requested `FileCoverage.window` remains distinct from the computed served interval and the compatibility carrier's nested window.
- The five visible outcome strings and their placement are the exact strings in section 3.
- A served-gap count is a count of intersecting persisted gap records, while overlap uncertainty remains a separate refusal/coverage fact.
- Non-hourly/totals-only scopes use the requested `Window` as their served scope when available; they do not receive invented monotonic bounds.
- `FileServeFacts` is local query evidence. `FileViewResult` transports only the local `served_gap_count` value inside the process; neither is a protocol field. The view receives the requested count from the existing coverage scalar and the served count from `FileViewResult`.

**Ambiguity in the approved design:** it names `FileServeFacts.gaps_in_requested_window` as a fact computed alongside the served-gap count, while also saying that the displayed requested-window count comes from the existing `coverage.gap_intervals` scalar. It does not state whether the new fact must equal that app-level scalar or whether the two are distinct measures (raw persisted gap rows versus aggregated file-coverage gap evidence). This specification defines `FileServeFacts.gaps_in_requested_window` as the number of persisted gap rows intersecting the requested interval and retains the existing `coverage.gap_intervals` scalar as the UI's requested-window count, but **OPEN: Should the public requested-window disclosure be sourced from raw persisted gap-row count or the existing aggregated `coverage.gap_intervals` scalar, and must these values be equal?** Until this is resolved, tests must assert each source's defined value separately and must not assert equality between them.

These are spec-level decisions except for the explicit `OPEN:` above; no other product-scope expansion is permitted.

## 11. Open items

- **OPEN:** Should the public requested-window disclosure be sourced from raw persisted gap-row count or the existing aggregated `coverage.gap_intervals` scalar, and must these values be equal?

No other unresolved product or implementation question is required to execute this specification. If implementation discovers a contradiction in the approved design rather than a defect in the code, it must stop the affected lane and add a new, explicitly reviewed `OPEN:` item instead of silently choosing a broader scope.
