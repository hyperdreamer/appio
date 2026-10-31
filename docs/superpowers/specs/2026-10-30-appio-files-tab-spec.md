# appio Files tab — frontier implementation specification

**Status:** revision 3; resolves the two findings in spec review 2, preserving the revision-2 contracts except for the clarifications and stronger fixtures below
**Authority:** `docs/superpowers/specs/2026-10-30-appio-files-tab-design.md`, revision 8, commit `0e92f44`, SHA-256 `ed64c5e36ddcdeebaa3e1495473225d58e6903897321b737e53e6a2aacf40492`.
**Candidate under audit:** `dd8b042` (`fix(router): serve hourly file windows across disclosed gaps`).
**Scope of this document:** the Files query, provenance, disclosure, and label-preservation work only.

**Documentary correction:** the approved design itself previously specified the superseded persisted-gap-row served measure in revision 7. It was corrected in revision 8: the served figure is this application's retained served-hour reducer; the requested scalar is a different existing reducer; pruned hourly evidence gives `None`, never an invented zero. This specification follows corrected design revision 8, not revision 7.

This document is normative for implementation lanes. Where it says **must**, an implementation that differs is not an alternative interpretation; it is a contract defect. No implementation may broaden the product scope below by adding a schema, migration, wire field, protocol message, ABI field, collection cadence, or Overview behaviour.

## 1. Scope and non-goals

### 1.1 Background and scope copied from the approved design

The Files tab has never rendered on the target host, and the header has misreported the daemon. File rows are hourly, so a file query must use an hour-aligned interval. The retained store contains approximately 710 legacy gaps; consequently every useful hour-aligned request can contain a gap. The longest gap-free stretch before the watermark is 614 seconds, below the hourly granularity. Router provenance over the whole request therefore refuses, and provenance over the served interval still refuses while an unproved or overlap-uncertain segment remains inside that interval.

The approved design's intended change is to serve an hour-aligned interval across known, countable gaps when all of the following hold: ownership is unambiguous; no overlap uncertainty is inside the served interval; every unproved segment is fully covered by known non-overlap gaps; and each such unproved segment is hour-aligned. Rows are rendered for the interval actually served, not for the larger requested window.

The header behaviour is a preservation target, not a repair target. Existing refresh logic promotes a verified, answering live connection over a silent one-shot probe. The activation probe override is deliberate. Existing unchanged code produces the health transitions that earlier drafts incorrectly called missing. This specification adds pins for the two uncovered transitions without changing precedence, witness authority, ownership reconciliation, `display_state`, or `decide_owner`.

The two scope clauses are named here so they cannot be lost during implementation:

1. **Honest-window clause:** for a requested window, the Files tab renders rows from the interval it served and discloses that interval plus this application's served-hour gap evidence and the existing requested-window coverage scalar. It must not present requested-window bounds as the bounds of the data.
2. **Label-preservation clause:** the header's existing truthful behaviour and precedence are preserved, and the two uncovered status transitions are pinned. No label repair is claimed without a concrete failing observation.

### 1.2 Goals

1. For a requested window, the Files tab renders the rows of the interval it served and discloses that interval, this application's served-hour gap evidence, and the existing requested-window coverage scalar.
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

For an hourly result, a retained `file_coverage` hour is a row for this application and one finalized file hour. The row's `gap_intervals` is application evidence and is not a persisted `gap`-table count.

A persisted `gap` row remains a provenance input. Its intersection with an interval may admit an unproved segment or establish overlap uncertainty, but persisted gap-row counts are never substituted for either app-level display fact.

The two app-level reducers are deliberately different:

- `gaps_in_served_interval` is the served-hour reducer: sum `gap_intervals` over this application's retained `file_coverage` hours that intersect the served interval.
- `gaps_in_requested_window` is the existing `coverage.gap_intervals` scalar returned by `file_coverage` for this application. Its current semantics are the served-hour contribution used by that query plus the existing requested-window overlay. It is not the served-hour reducer recomputed over a larger interval.

Neither reducer is a boot-wide `gap`-row count, a deduplicated union length, or a byte total. The disclosure must name the reducer and scope so the two values are not presented as comparable measurements.

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
| `served_interval` | `Window \| None` | The effective interval used by the file query. For an hourly query this is the selected largest contiguous hour-aligned interval. It is `None` when no interval is servable. It may equal the requested `range` or `since_boot` scope even when `coverage.complete is False`; completeness is not the definition of a shortened interval. For a successful totals-only `since_boot` or `lifetime` query after hourly pruning, it is the existing requested `Window`, with no invented monotonic endpoints. |
| `gaps_in_served_interval` | `int \| None` | For an hourly served scope, the sum of this application's `file_coverage.gap_intervals` over retained hourly rows intersecting the served interval. It does not count persisted `gap` rows. Return `None` when no served interval exists or when the relevant hourly rows are unavailable, including a valid totals-only/pruned `since_boot` or `lifetime` scope; never manufacture `0` from absent rows. Return `0` only when the hourly evidence needed for the served result is retained and its measured sum is zero. |
| `gaps_in_requested_window` | `int` | Exactly the existing `coverage.gap_intervals` scalar from this application's `file_coverage` result for the requested `Window`. Preserve its current served-hour-plus-requested-window-overlay reducer, including its value when the served interval is shortened. Do not recompute it as `gaps_in_served_interval` at a larger scope and do not replace it with a persisted `gap`-row count. |

The method has an explicit store-local application identity:

```python
def file_serve_facts(self, app_id: int, window: Window) -> FileServeFacts:
    ...
```

`app_id` is the positive SQLite database-local surrogate, not an `AppKey`. The local caller must resolve the selected external `AppKey` to this store-local `app_id` in the same pinned read snapshot, pass that `app_id`, and verify that the returned table, coverage, and facts all describe that application. A facts call without an application identity is a contract defect because two applications can have different served-hour sums in the same window.

The method must use the same read snapshot and the same requested bounds as the corresponding `file_applications`, `file_table`, and `file_coverage` calls. The local worker must keep those calls in one read transaction or an equivalent snapshot-bound query context; it must not call the method against a second database state. It must not be serialized or added to any daemon response.

For hourly `range` and `since_boot` requests, the method uses the same selected interval as `file_applications`, `file_table`, `file_coverage`, and `unknown_owner_file_coverage`. For a valid totals-only result, `served_interval` follows the existing requested scope, while `gaps_in_served_interval` remains `None` because the hourly rows needed for the served-hour reducer are not available. A reader that computes rows using one interval, app identity, or snapshot and facts using another is a contract defect.

When no interval exists, the query layer may still construct an internal unavailable `FileCoverage` for error propagation, but the router must reject that candidate under section 5.7 and never publish its placeholder as a successful result. If no other candidate can be accepted, or a fatal refusal fires, the view must render outcome 1 below. No caller may interpret the unavailable placeholder window as a served interval or its absent hourly rows as zero gap evidence.

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

For a successful `FileCoverage`, the property returns the interval represented by the existing `unknown_owner_overflow.window` field. It returns `None` for an unavailable lower-level result. Equality with the requested window means that the effective query scope was not shortened; it does **not** imply `coverage.complete is True`. An hourly result with `coverage.complete is False` may still have `served_interval == window` (notably a full requested `since_boot` scope). Call it a **shortened hourly result** only when an hourly query's effective bounds differ from the requested scope; in that case the property returns a `range` `Window` with the selected served bounds. The property must not change the dataclass field order, equality shape, durable encoder, decoder, or IPC representation.

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

- On the local-store route, it is `FileServeFacts.gaps_in_served_interval` for the explicitly selected store-local `app_id`. It is an integer only when a served interval and the required hourly app evidence are available; a totals-only/pruned scope may therefore carry `None` even when `served_interval` equals the request. `0` is a proved measured zero, not a fallback for missing rows.
- On the daemon/remote route, it is always `None`. Protocol 1 transports `FileTable` and `FileCoverage`, but not the local hourly source rows or gap-row layout needed to prove the served-hour reducer. `None` means “not provable from this route or retained scope,” not zero.
- It is never a substitute for `coverage.coverage.gap_intervals`, which remains the existing requested-window coverage scalar.
- It is never transported, encoded, decoded, cached as a wire field, or used to alter `FileCoverage` equality.

The router must verify that the table, coverage, served interval, application identity, and served-gap count were obtained for one snapshot and one scope. A result that combines a requested-window table with served-interval coverage is invalid.

### 2.5 Existing `FileCoverage` transport contract

The state `FileCoverage` and its nested `Window`, `Coverage`, `HookHits`, and `UnknownOwnerOverflow` values are transported exactly as before. The encoder and decoder field sets must not change. Exact-field-set decoding remains mandatory. No `served_interval`, `gaps_in_served_interval`, or `served_gap_count` key may be added to durable JSON, IPC payloads, HMAC cursors, protocol messages, or ABI records.

The implementation must pin the encoder with literal expected field sets, not only a round trip. For a representative `FileCoverage`, `to_wire` must produce exactly these sets (all include the `record` key):

```python
WINDOW_FIELDS = {
    "record", "kind", "boot_id", "start_mono_ns", "end_mono_ns",
}
HOOK_HITS_FIELDS = {
    "record", "vfs_read", "vfs_write", "vfs_readv", "vfs_writev",
    "vfs_iter_read", "vfs_iter_write",
}
COVERAGE_FIELDS = {
    "record", "tier", "complete", "unavailable_reason", "inaccessible_pids",
    "resets", "reap_corrections", "reap_unapplied", "reap_unobserved",
    "partial_intervals", "gap_intervals", "overlap_uncertain_intervals",
    "spans_suspend_intervals", "spans_suspend", "reap_incomplete",
    "counter_overflow", "device_state", "counted_omitted_files",
    "counted_omitted_bytes_r", "counted_omitted_bytes_w", "uncounted_overflow_files",
    "uncounted_overflow_bytes_r", "uncounted_overflow_bytes_w", "skipped_nonfile",
    "skipped_unsupported_fs", "weak_identity_rows", "hook_hits",
}
UNKNOWN_OWNER_FIELDS = {
    "record", "window", "owner_key", "overflow_files", "overflow_bytes_r",
    "overflow_bytes_w", "coverage",
}
FILE_COVERAGE_FIELDS = {
    "record", "window", "counted_omitted_files", "counted_omitted_bytes_r",
    "counted_omitted_bytes_w", "uncounted_overflow_files",
    "uncounted_overflow_bytes_r", "uncounted_overflow_bytes_w", "skipped_nonfile",
    "skipped_unsupported_fs", "weak_identity_rows", "hook_hits", "observed_bytes_r",
    "observed_bytes_w", "unknown_owner_overflow", "coverage", "app_key",
}
```

`launch_id` is intentionally absent from the wire `Window` set because the existing codec omits database-local launch identity. The decoder must reject both an added key and a missing key at the top level and at each nested record level (`Window`, `Coverage`, `HookHits`, and `UnknownOwnerOverflow`). A computed property is allowed because it has no serialized representation. A local `FileServeFacts` or `FileViewResult.served_gap_count` field is allowed because neither crosses the route boundary.

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

Successful Files results use only the supported `Window` kinds `range`, `since_boot`, and `lifetime`:

- For `range`, format any monotonic scope exactly as `T0={elapsed(T0)} through T1={elapsed(T1)} after boot`.
- For `since_boot`, format the requested scope as `since_boot through T1={elapsed(end_mono_ns)} after boot`; the required end bound must remain visible. If an hourly result is shortened, format its served `range` with the `T0`/`T1` form and retain the requested `since_boot` endpoint beside it. If the totals-only boot scope is selected, still say `since_boot`, never the internal query label `boot`.
- For `lifetime`, say `lifetime` and do not invent `T0` or `T1` values. A valid totals-only lifetime result has the requested lifetime as its served scope.
- `launch` is rejected by the existing Files query precheck, so it has no successful scope formatter. `boot` is an internal selection scope, not a valid `Window.kind`, and must never appear as one.

The canonical scope text is:

```text
served interval{ only if served != requested}: {served_bounds_or_scope}; requested window: {requested_bounds_or_scope}; this application's served-hour gap evidence in served interval: {served_gap_text}; this application's requested-window coverage scalar: {requested_gap_scalar}
```

There is no space before the colon after `interval`; the `only` form is literally `served interval only:`. `served_gap_text` is the decimal local integer, including `0`, when the app-hour evidence is retained and measured, or exactly `unavailable` when the value is `None` because the hourly source is pruned/unavailable or the remote route cannot prove it. The text must not imply that the two values are comparable or that `unavailable` means zero. `{requested_gap_scalar}` is the existing `coverage.coverage.gap_intervals` value for this application.

For app A in the canonical local fixture in section 5.5, with app evidence `1` in the served hours and the existing requested-window reducer equal to `4`, the disclosure must contain exactly:

```text
served interval only: T0=2h 0m 0s through T1=4h 0m 0s after boot; requested window: T0=0s through T1=6h 0m 0s after boot; this application's served-hour gap evidence in served interval: 1; this application's requested-window coverage scalar: 4
```

A result whose served and requested scopes are equal must say `served interval:`, not `served interval only:`. The two labels describe different reducers; no copy such as “the same gaps at a larger scope” is permitted.

The canonical scope text is placed in `footer_state`. The same served/requested scope names and reducer labels must also be present in `footer_observed` and in every coverage-counter footer line that reports omission, overflow, unknown-owner, skipped-object, weak-identity, or hook data. This prevents a footer line from silently reverting to whole-request language or a boot-wide gap-row claim.

### 3.2 Five outcomes, evaluated in order

The renderer evaluates these predicates top-to-bottom. The first matching rule is the only outcome. The internal names are normative test labels; the visible strings are normative copy.

#### Outcome 1 — `unavailable`

**Predicate:** no `served_interval` exists for an accepted result, or a request-level router/query refusal fires after the permitted candidate skips in section 5.7, or the returned coverage has an unavailable reason.

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
no retained file activity for this application in the served interval; {scope_text}; coverage: complete
```

This is a per-application statement about the served scope. It does not claim that no application was observed, and it does not replace the separate no-application selector state.

#### Outcome 5 — `empty_partial`

**Predicate:** a valid served interval exists, no search filter is active, `len(FileTable.rows) == 0`, and `coverage.complete is False`.

**Visible placement:** `footer_state` is the empty state; the model remains empty; both requested-window and served-interval gap disclosures remain visible. The incomplete coverage state must not be collapsed into `empty_proven`.

**Exact `footer_state` disclosure:**

```text
no retained file activity in the served interval; coverage: incomplete; {scope_text}
```

This exact form is used even when `served_gap_count` is `0`; incomplete coverage can arise from facts other than a known gap. When `served_gap_count is None`, it must contain the canonical `this application's served-hour gap evidence in served interval: unavailable` wording, not `0`.

### 3.3 Separate empty-application selector state

If the app selector has no applications before a file-table query is issued, retain the existing selector state:

```text
coverage: not applicable; no applications observed in this window
```

This state is not one of the five per-application file-table outcomes. It must not be used to hide an unavailable file source, and it must not be inferred from a zero-row result for a selected application.

## 4. Per-route disclosure and no wire change

### 4.1 Local-store route

The local route reads one or more local/exported SQLite stores through `QueryService`. It may inspect:

- the requested bounds;
- the effective served bounds;
- persisted gap rows for provenance admission and overlap/refusal predicates; and
- ownership and overlap predicates from the local provenance plan.

Those persisted rows are not the app-level display reducers. The local route must resolve the selected external `AppKey` to the positive store-local `app_id` in the same snapshot, then call `file_serve_facts(app_id, window)` against the snapshot used to obtain the table and coverage. It sets `FileViewResult.served_gap_count` to `facts.gaps_in_served_interval`. It uses `facts.gaps_in_requested_window` (the existing `coverage.coverage.gap_intervals` scalar) for the requested-window disclosure and must not replace that scalar with the served count.

### 4.2 Daemon/remote route

The remote route receives only the existing `FileTable` and `FileCoverage` response records and must preserve their snapshot and window checks. It can disclose the served bounds carried by the existing `unknown_owner_overflow.window` compatibility field. It cannot prove the served-gap reducer because the protocol does not transport the local hourly `file_coverage` rows or an equivalent field.

The remote route therefore sets:

```python
FileViewResult.served_gap_count = None
```

The view must display the canonical `this application's served-hour gap evidence in served interval: unavailable` text for this value. It may display the existing requested-window coverage scalar. It must not copy that scalar into the served-gap slot, infer zero from a complete-looking response, or hide the missing scope.

### 4.3 Scope unprovable

If a route cannot prove a valid served interval, a route cannot bind the rows and coverage to the same snapshot/window, or a required request-level provenance refusal fires, the route returns `WindowUnavailableError` and the view uses outcome 1. On the local route, section 5.7 governs candidate skips before this terminal outcome; no skip authorises ignoring a fatal provenance refusal. The route does not return a zero-row `FileViewResult` as a proxy for “nothing happened.”

No field is added to `QueryRequest`, the server response, `FileTable`, `FileCoverage`, the durable JSON, the HMAC cursor, or any ABI. A local-only `FileServeFacts` and a local-only `FileViewResult.served_gap_count` are the complete addition.

## 5. Provenance predicates

The router evaluates provenance over a store-specific served interval, not automatically over the requested interval. Let `R` be the requested half-open interval and let `S_i` be the candidate interval selected independently from store `i`. A source segment intersects `S_i` when its interval has a non-empty half-open intersection with that store's candidate interval.

Selection is per store, not global: each store selects its own `S_i` and finalized-through bound from its file calendar. The existing ownership plan over all registered sources is evaluated for that candidate's `S_i`, with segments and known gaps clipped to that interval. This does not mean “discard every candidate whose provenance fails”: section 5.7 distinguishes skippable exclusions from fatal request refusals. Only after all candidates have been examined without a fatal refusal may the router accept exactly one fully validated candidate and use its `S_i` for rows, coverage, facts, and disclosure. Two fully validated stores are ambiguous and refuse even when their `S_i` intervals differ. Evidence outside a candidate's `S_i` cannot fail that candidate's predicates; this scoping rule does not authorise ignoring a fatal refusal from another candidate.

### 5.1 Existence and requested-scope facts

For each candidate store `i`, `S_i` must exist before any file rows are accepted. For an hourly request, `S_i` is the interval selected by `_largest_contiguous_file_interval` from that store's own file calendar and must satisfy:

```text
S_i.start >= R.start
S_i.end <= R.end
S_i.start < S_i.end
S_i.start % FILE_HOUR_NS == 0
S_i.end % FILE_HOUR_NS == 0
S_i.end <= store_i.file_finalized_through_mono_ns
```

The selected interval is the largest contiguous run of present finalized file hours in store `i`; ties use the existing helper's deterministic end-bound tie-break. No implementation may choose a shorter `S_i` merely to avoid a provenance refusal. The router must not intersect candidate intervals from different stores to manufacture one global `S`.

### 5.2 Single-owner predicate

The single-owner predicate is true exactly when:

```text
there is exactly one collector_id among proven segments intersecting S_i,
no second collector_id has proven coverage intersecting S_i,
and all rows used for S_i come from that one collector_id.
```

Mirrors of the same collector receipt do not become additive owners. Evidence of a source or owner wholly outside `S_i` does not fail this candidate's single-owner predicate. A handover boundary inside `S_i` is allowed only when the router's existing ownership plan proves the boundary and still reduces to one authoritative store for the complete served result; otherwise it is an ownership ambiguity and refuses. The mirror skip applies only after all provenance predicates pass; it is not permission to discard a provenance failure. Section 5.7 defines fatal/skippable handling and the final candidate-count check.

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

For each store candidate `i`, clip every raw source segment and every known-gap interval to `S_i` before evaluating file admission:

```text
clipped_segment = raw_segment ∩ S_i
clipped_gap = known_gap ∩ S_i
```

Empty intersections are discarded. Alignment and full-coverage checks are then applied to the non-empty clipped segment, against the union of clipped known non-overlap gaps. The raw segment's portion outside `S_i` is not allowed to fail or widen the candidate.

For every non-empty segment intersecting `S_i`, one of these must hold:

1. the clipped segment is proven by the single authoritative collector; or
2. the clipped segment is unproved, its entire interval is covered by the union of clipped known non-overlap gap rows for the same boot, and both clipped endpoints are hour-aligned.

Coverage is full interval coverage, not “there is a gap row somewhere in the segment.” The union must have no uncovered hole. A gap row with reason `overlap_uncertain` is not a known non-overlap gap and cannot satisfy this predicate.

A boundary-straddling fixture is mandatory: with `S_i = [2h,4h)`, raw unproved segment `[1h30m,3h)` clips to `[2h,3h)` and is admitted only when `[2h,3h)` is fully covered by a known non-overlap gap; raw unproved segment `[1h30m,3h30m)` clips to `[2h,3h30m)` and refuses because the clipped endpoint is not hour-aligned. An unproved segment wholly outside `S_i` does not fail this candidate's proven-segment predicate. The router must not widen `S_i` to include it.

Failure reports the same `file_source_unavailable` / `File ownership cannot be represented by one store.` pair. The result must not expose rows from the otherwise unproved segment.

### 5.4 Overlap-uncertainty predicate

The overlap predicate is true exactly when there is no overlap uncertainty intersecting `S_i`:

```text
store_i.worker._overlap_ranges has no non-empty intersection with S_i
and no persisted gap row with reason "overlap_uncertain" intersects S_i.
```

An ambiguous candidate set from two owners/epochs is overlap uncertainty even if the resulting counters happen to be zero. The router must refuse rather than select a winner by row order, freshness, byte total, or a green test result.

Failure reports `file_source_unavailable` with `File ownership cannot be represented by one store.`

### 5.5 Gap-coverage predicate

The gap-coverage predicate is true exactly when every non-empty clipped unproved segment for `S_i` is fully covered by clipped known non-overlap gaps and has hour-aligned endpoints. It is evaluated independently of the requested-window coverage scalar.

The canonical fixture contains **two applications in the same store and snapshot**, not two alternative descriptions of one application:

```text
requested R = [0h, 6h)
served S_i = [2h, 4h) for both apps
retained file_coverage.gap_intervals by app and hour:
                  [2h,3h)  [3h,4h)  served-hour sum
  app A (app_id=1)     0         1          1
  app B (app_id=2)     1         1          2
persisted gap rows used by the requested-window overlay:
  [1h,2h), [3h,4h), [4h,5h), all known non-overlap gaps
persisted rows intersecting S_i = 1
persisted rows intersecting R   = 3
```

Commit the synthetic hourly evidence and gap rows before querying. Retain both apps' required hourly rows; their different application counters must not be derived from the persisted gap-row count. Query both app IDs explicitly in the same pinned snapshot as their corresponding tables and coverage. The required assertions are:

```python
facts_A = service.file_serve_facts(1, R)
facts_B = service.file_serve_facts(2, R)
assert facts_A == FileServeFacts(S_i, 1, 4)  # 0 + 1; (0 + 1) + 3
assert facts_B == FileServeFacts(S_i, 2, 5)  # 1 + 1; (1 + 1) + 3
```

Also assert that each app's table and coverage carry that app's identity, retain requested `window == R`, and report `S_i` through `table.unknown_owner_overflow.window` and `coverage.served_interval`. The existing requested-window `coverage.coverage.gap_intervals` scalars must be respectively `4` and `5`. Independently assert the persisted-row counts `1` in `S_i` and `3` in `R` so the fixture's distinguishing evidence is pinned.

These assertions must kill the following named mutations:

- **`M_served_uses_persisted_rows` — “counted persisted rows instead”:** replacing the served-hour sum with the number of persisted rows intersecting `S_i` returns `1` for B instead of `2`. B's served-value assertion must fail. A's served `1` alone cannot kill this mutation.
- **`M_B_reuses_A_facts` — “returned app A's facts for app B”:** ignoring B's explicit `app_id` and returning A's facts yields `FileServeFacts(S_i, 1, 4)` instead of B's required `FileServeFacts(S_i, 2, 5)`. B's facts assertion must fail; merely creating B without asserting its result is not evidence.

The `[1h,2h)` and `[4h,5h)` records are not app-hour evidence inside `S_i`, and the requested scalar is not a second app-hour sum. Do not deduplicate or compare the two reducers. A request-and-serve-same-interval fixture, an A-only assertion, or equal served sums for both apps is insufficient evidence.

### 5.6 Process-history gap scoping

The file route may keep separate known-gap ranges for a store candidate's served-interval admission, but `_gap_ranges` must remain scoped to the requested process-history window `R`. `_gap_ranges` feeds process coverage and must not be replaced by `S_i` or by the candidate's file-admission ranges. A separate `_known_gap_ranges` (or an equivalent local name) may be used for file admission.

This separation is mandatory even when the file result is partial. In the required fixture, use `R = [2h,6h)` and `S_i = [2h,4h)`, with a process gap `[4h,5h)` outside `S_i` but inside `R`, and a control gap `[0h,1h)` outside `R`. The process reducer must retain `[4h,5h)` and exclude `[0h,1h)`; the file served-hour reducer must not count `[4h,5h)` because it has no intersecting app-hour row in `S_i`. A second control with only `[0h,1h)` must leave process coverage complete. A gap outside the requested process window must never mark process history incomplete, and a gap outside the served file interval must not become a served gap merely because it is in the request.

### 5.7 Candidate failure classification and acceptance order

The failure **stage**, not the exception's reason string, determines whether another store may be tried. Preserve the existing narrow `WindowUnavailableError` catch around the table/coverage query and `_check_file_result` in `HistoryRouter._read_local_files` (`src/appio/history_router.py:199–211`), and the provenance refusal outside that catch (`:218–231`). The approved known-gap admission in sections 5.3–5.5 is the only authorised relaxation of provenance; there is no authority to turn any remaining provenance refusal into a candidate skip.

| Stage or condition | Classification | Required handling |
| --- | --- | --- |
| The selected external `AppKey` has no app receipt in this store. | **Skippable** | Continue without inventing rows or updating the remembered query error. |
| The table/coverage query or existing result validation raises `WindowUnavailableError` before provenance, including an unavailable/no-served-interval result. | **Skippable** | Remember that typed query/result-validation error and continue, as the existing catch does. A later fully validated candidate may answer. The same `file_source_unavailable` reason can instead be fatal when raised by provenance below; do not classify by reason alone. |
| All provenance predicates pass, but the single authoritative collector/store is not the store supplying this candidate's rows (`names != {name}`). | **Skippable discarded mirror** | Continue without adding its rows or replacing the remembered query error. The mismatch is skippable only after the other provenance predicates have passed. |
| A candidate reaches provenance but fails single-owner, non-empty segment, chronology, clipped proven/known-gap coverage, alignment, or overlap-uncertainty requirements. | **Fatal to the request** | Raise `WindowUnavailableError("file_source_unavailable", "File ownership cannot be represented by one store.")`. Do not discard the failed candidate and try to answer from another one. An already accumulated valid result cannot mask this refusal. |
| An unexpected read, snapshot, query, or planning exception is outside the existing typed query/result-validation skip. | **Fatal to the request** | Abort and retain the existing outward error propagation/normalization. Do not broaden the catch to turn SQL, I/O, malformed evidence, or planning failures into skips or numeric empty results. |
| The scan finishes without a fatal refusal and more than one fully validated candidate remains. | **Fatal ambiguity** | Refuse with the same ownership error pair, even if the candidates serve disjoint intervals. Never return the first valid result before the scan finishes. |

After the scan, exactly one fully validated candidate is returned only if no fatal refusal fired. If none remains, raise the last remembered typed query/result-validation error, or the existing `file_source_unavailable` / `No authoritative file app receipt.` default if none was remembered. Neither a missing receipt nor a discarded mirror replaces that remembered error. This preserves existing fallback and refusal behaviour rather than adding a new candidate ranking or error-precedence policy.

**Mandatory mixed valid/invalid-candidate fixture:** request `R=[0h,6h)` for the same external `AppKey` in two committed stores. A's file calendar selects `S_A=[2h,4h)` and B's selects `S_B=[5h,6h)`; both candidates' table/coverage queries pass result validation. A has one proven owner on `[3h,4h)` but an unproved `[2h,3h)` segment with no known-gap union covering it. B has valid ownership throughout `[5h,6h)` and no intersecting overlap uncertainty. No B evidence intersects A's served interval. The result **must refuse** with the ownership error pair, not accept B. Run in both A-then-B and B-then-A orders: in the latter, B's provisional success must still be overridden by A's fatal refusal. B alone must succeed as the positive control.

Pin the skippable controls separately: with A's pre-provenance query returning typed unavailability (for example, no servable file calendar), B is accepted; with no app receipt in A, B is accepted; a discarded mirror is skipped only after successful provenance. Include a mirror-order refusal control: A still serves `[2h,4h)` with uncovered unproved `[2h,3h)`, but its lone proven collector on `[3h,4h)` is B (`names == {B}`, not `{A}`); B's own `[5h,6h)` candidate is valid. A's provenance failure must remain fatal before the mirror-mismatch skip can run. These controls distinguish preserving existing fallback from making every candidate failure fatal or moving the mirror skip ahead of provenance. The mixed fixture must kill **`M_provenance_failure_becomes_skip`**, which catches A's provenance error and continues to B, and the B-first ordering must kill an early-return-on-first-valid-result mutation.

## 6. Adoption of `dd8b042`

### 6.1 Audit scope

The candidate audit is read-only and limited to:

- selection and propagation of the served interval;
- local provenance over that interval, including the fatal/skippable candidate handling in section 5.7;
- known-gap coverage and overlap refusal;
- preservation of requested-window process-history coverage;
- local and remote disclosure of bounds and gap counts;
- the candidate tests' mechanism, not just their assertions.

The audit does not treat the candidate as an implementation of this whole specification. It must inspect the diff and its callers, then compare each mechanism with sections 2–5 and the approved design.

The audit must not load BPF, use root, modify source or tests, or run the full test suite during this drafting/assessment gate.

### 6.2 Allowed outcomes

- **Pass:** the candidate satisfies the mechanisms within the audit scope in section 6.1: per-store served-interval selection and propagation, local provenance over that interval, known-gap coverage and overlap refusal, requested-window `_gap_ranges` preservation, and the candidate's route disclosure behavior. Its tests must falsify those mechanisms rather than only assert a green result. Pass is not a claim that the candidate already implements every contract in sections 2–5 or 8; the full `FileServeFacts` signature, nested wire-field inventory, exact formatter, totals-only semantics, and frozen-snapshot acceptance remain separate implementation gates unless the candidate directly changes them.
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

All fixtures below are synthetic unless explicitly marked “real database snapshot.” Synthetic fixtures must use committed SQLite state and synthetic receipts/hourly observations; they must not load BPF, use root, or depend on a live daemon. The real snapshot is opened read-only and is never mutated by the test.

| Contract pin | Fixture shape | Falsifying mutation | Real database snapshot? |
| --- | --- | --- | --- |
| `FileServeFacts` reducers and types | Use the two-app canonical fixture in section 5.5: `R=[0h,6h)`, `S_i=[2h,4h)`, A's hourly counters `0,1`, B's `1,1`, and persisted overlay rows `[1h,2h)`, `[3h,4h)`, `[4h,5h)`. Explicitly call and assert **both** `file_serve_facts(1,R) == FileServeFacts(S_i,1,4)` and `file_serve_facts(2,R) == FileServeFacts(S_i,2,5)`, matching each app's table/coverage. Assert persisted counts `1` in S and `3` in R; B's served sum `2` must differ from the served persisted-row count `1`. Add no-served and totals-only/pruned `since_boot`/`lifetime` fixtures: no-served facts have interval/count `None`; valid totals-only facts have the existing requested scope and served count `None`; requested scalar remains an integer. | `M_served_uses_persisted_rows` must fail B's served `2` assertion; `M_B_reuses_A_facts` must fail B's `(S_i,2,5)` assertion. Also falsify reused requested bounds, no-hourly evidence turned into `0`, and the old requested count `3`. A-only assertions are insufficient. | No. |
| Served interval propagation and outside rows | Request `[0h,6h)` with file rows at hours 0, 2, 3, and 5, while `S_i=[2h,4h)`. Assert `FileTable.window` and `FileCoverage.window` remain the request, `FileCoverage.served_interval=S_i`, and table rows, observed bytes, and app totals contain only hours 2 and 3. | Leave `file_applications`, `file_table`, or `file_coverage` on the requested bounds; query rows outside `S_i`; use `unknown_owner_overflow.window` only for the footer while querying the request. | No. |
| Complete result | One hour-aligned request with all finalized hours and no gaps. Assert served equals requested, `served_gap_count == 0`, and `coverage.complete is True`. | Treat `0` as unknown/`None`; mark a result partial merely because no unknown-owner rows exist. | No. |
| Totals-only/pruned semantics | Return a valid `lifetime` and a valid `since_boot` aggregate after hourly rows have been pruned. Assert `served_interval == requested Window`, no invented endpoints or `boot` label, `gaps_in_served_interval is None`, and requested scalar is preserved. | Infer a zero served gap from aggregate completeness; fabricate `T0`/`T1`; expose the internal `boot` scope. | No. |
| FileCoverage wire shape | Encode/decode a representative `FileCoverage` with nested `Window`, `Coverage`, `HookHits`, and `UnknownOwnerOverflow`; compare each literal field set in section 2.5 and round-trip values. Mutate each nested record once by adding a key and once by removing a key. | Add any served field to the durable/IPC object; relax exact-field-set decode at one nested level; encode a computed property; test only top-level fields. | No. |
| Local result shape and identity | Local `HistoryRouter` resolves an external `AppKey` to a store-local `app_id`, calls `file_serve_facts(app_id, window)` in one snapshot, and returns `FileViewResult(table, coverage, served_gap_count)`. | Omit the explicit app argument; derive the served count from `coverage.gap_intervals`; combine results from different snapshots. | No. |
| Remote result shape | Drive the existing remote harness through real `IodClient` encoding/decoding and HMAC cursors. Assert bounds are disclosed, `served_gap_count is None`, and the requested coverage scalar remains available. | Set remote served count to `coverage.gap_intervals`; invent zero; add a wire field. | No. |
| No authoritative source | Delete the file app receipt or remove the file calendar so no store can supply a validated candidate; assert typed refusal, not a numeric empty result. | Return a zero-row successful result; use `unsupported_window`/`not_retained` as a numeric empty result without the typed refusal. | No. |
| Per-store candidate selection | Give store A a fully validated `S_A=[2h,4h)` and store B a different fully validated `S_B=[4h,6h)`; assert two candidates refuse. Add an owner/gap outside A's `S_A` as a control that does not fail A's predicates when A is the only accepted candidate and no other candidate raises a fatal refusal. | Compute one global intersection/union interval; accept the first store; let evidence outside `S_i` fail that candidate's predicates. | No. |
| Fatal versus skippable candidate failures | Use section 5.7's mixed fixture: A's `S_A=[2h,4h)` has uncovered unproved `[2h,3h)`, while B's `S_B=[5h,6h)` is fully valid. Both queries pass validation. Assert the exact fatal ownership reason/message in **both store orders**, and B-alone success. Separately assert B succeeds when A has typed pre-provenance query unavailability or no app receipt. Assert a valid mirror is skipped, while the mirror-order control in section 5.7 refuses before a mismatch can skip failed provenance. | `M_provenance_failure_becomes_skip` accepts B instead of refusing; early return accepts B in B-first order; widening fatal handling to all query unavailability breaks the fallback controls; skipping a mirror before checking its provenance bypasses a fatal refusal. | No. |
| Unproved segment clipping | With `S_i=[2h,4h)`, use raw unproved `[1h30m,3h)` and a clipped known gap `[2h,3h)` for acceptance; use raw `[1h30m,3h30m)` for refusal because clipped `[2h,3h30m)` is not aligned. Include a hole and an overlap-uncertain row. | Evaluate alignment/coverage before clipping; accept any gap row somewhere in the raw segment; accept overlap-uncertain rows as known gaps. | No. |
| Overlap uncertainty | Add an `overlap_uncertain` row intersecting `S_i`, with byte totals that happen to be zero, and a control row outside `S_i`. | Ignore overlap ranges because totals are zero; use requested-window overlap state without intersection testing. | No. |
| Requested `_gap_ranges` and process isolation | Request process window `R=[2h,6h)`, use `S_i=[2h,4h)`, put one gap at `[4h,5h)` inside R but outside S, and a second at `[0h,1h)` outside R. Assert process coverage sees only `[4h,5h)`, file served-hour facts do not count it, and a control with only `[0h,1h)` remains process-complete. | Replace `_gap_ranges` with served ranges; let the outside-request gap mark process history incomplete; let the outside-served gap become a served app gap. | No. |
| Ordered rows outcome | Return rows `zeta` with `bytes_total=10` and `alpha` with `bytes_total=5`, request `bytes_total desc`, and separately exercise a partial and search-filtered result. Assert displayed order is `zeta, alpha`, outcome 2 wins, and the view does not reorder. | Replace the query's order with `basename asc` (which changes this fixture to `alpha, zeta`); sort/filter rows in the view; label observed bytes as whole-window bytes. | No. |
| Filtered zero outcome | Valid result, active normalized search, zero matching rows, and aggregate `observed_bytes_r=7`, `observed_bytes_w=0`. | Use `empty_partial` for a filtered zero; claim no file activity; represent “unknown” with a non-existent byte value; omit the filter from the state string. | No. |
| Complete empty outcome | Valid complete result for an app with no retained file rows but complete file coverage. | Render unavailable or “no applications observed”; use partial wording. | No. |
| Partial empty outcome | Valid served interval, no filter, zero rows, `coverage.complete=False`; cover local count `0`, totals-only `None`, and remote `None`. | Treat zero rows as complete; omit one of the two gap scopes; render either `None` as `0`. | No. |
| Scope formatter and bounds | Exercise full/shortened `range`, full/shortened `since_boot`, and `lifetime`; assert real `since_boot` end bounds, no invented lifetime bounds, no `boot` text, and launch refusal. Assert `served interval only:` appears only when scopes differ. | Drop the `since_boot` end bound; use `boot`; invent lifetime endpoints; derive labels from the current request after a stale reply. | No. |
| Exact disclosure placement | Assert `footer_state`, `footer_observed`, and each populated coverage footer line contain the correct served/requested names and reducer labels; assert empty-state text is in `footer_state` and `unavailable` is never rendered as `0`. | Put scope only in the table header; leave the observed line saying “whole window”; use “gaps in requested window” for the served-hour reducer. | No. |
| Stale reply | Defer two requests; retire the first and publish the second. | Publish a retired generation's rows or served facts. | No. |
| Label probe-only truth | Fake the status socket to answer after stream EOF for three refreshes. | Let EOF demote despite a healthy status witness; let the root activation probe assert answering. | No. |
| Label recovery | Fake a connected state, one silent status refresh after EOF, then an answering status refresh and reconnection. Assert the intermediate exact label and later verified connection. | Freeze the label; retain stale `answering=True`; assert only the final label without verified reconnection. | No. |
| Candidate adoption | Read-only diff audit plus targeted candidate tests, per-store candidate cases including section 5.7's mixed fatal/skippable cases, boundary-clipping case, and the `_gap_ranges` mutation. | Declare adoption solely because the aggregate suite is green. | No. |
| End-to-end acceptance | Use the required read-only artifact `tests/fixtures/p4/files-tab-real.sqlite`, identified by `tests/fixtures/p4/files-tab-real.sha256` and `tests/fixtures/p4/files-tab-real.json`. The manifest must pin the snapshot SHA-256, capture/source identity, schema/user version, selected external `AppKey`, requested `Window`, surviving store, and expected `S_i`. The snapshot must be copied from that identified target store before acceptance and must not receive test inserts. An independent oracle reads the raw snapshot rows and derives `S_i`, app-hour `gaps_in_served_interval`, requested-window `coverage.gap_intervals` semantics, rows, bounds, and provenance outcome without calling production query helpers or rendering code. The offscreen Files view then asserts those expectations and label answering with a stubbed verified status, not a live daemon. | Use a handcrafted database or synthetic tree at the acceptance path; omit the hash/manifest; derive expectations through `QueryService.file_serve_facts`, `file_coverage`, or the view; use a request whose served interval equals the request; assert only that the widget is non-empty. | **Yes.** This is the only required acceptance pin that uses a real database snapshot rather than an in-memory/synthetic fixture. |

The canonical request/served fixture remains mandatory for every relevant test group. Its reducer/identity assertions must cover **both** applications and B's served-hour/persisted-row inequality. A request-and-serve-same-interval test does not count as evidence for the two gap scopes because a reused requested count would pass unchanged. A fixture that asserts only A cannot falsify either of the two named reducer/identity mutations in section 5.5.

Every mechanism mutation must be run against a clean baseline, observed to fail at the named assertion, and reverted byte-for-byte before the positive result is recorded. A passing test that was already green before the mutation is not a falsification.

The real acceptance artifact is a deliverable of implementation, not a permission to synthesize a database for the test. The hash file must be checked before opening the database; the manifest and independent oracle output must be retained with the acceptance evidence. A hash mismatch, missing artifact, or oracle that imports the production reducer is an acceptance failure.

## 9. Risk register

### 9.1 Risks carried from the approved design

1. **Hour-aligned eligibility narrows what serves.** Selection serves whole-hour intervals and accepts unproved segments only when they are hour-aligned and fully covered by known non-overlap gaps. The result is hour-granular, not minute-granular. A store whose recent history is one sub-hour gap can serve nothing until an eligible hour boundary exists.
2. **Computed-property cost.** Keeping `served_interval` computed protects the wire contract but requires a pure derivation from existing fields. It must be O(1), side-effect free, and must not requery SQLite. `FileServeFacts` is the one snapshot-local computation of gap counts; the view must not scan gap rows.
3. **Remote unprovability.** The daemon route can disclose bounds but cannot prove the served-hour app reducer with protocol 1. The UI must expose the canonical `unavailable` served-gap text, not silently reuse the requested scalar or claim zero. Totals-only/pruned local scopes have the same `None` rule even though their route is local.
4. **Label authority.** The two-witness rule resolves both directions but makes “answering” a statement about the daemon status witness, not merely the GUI stream. Later copy must not imply that it means the stream is healthy without revisiting the label rule.

### 9.2 Implementation-specific additions

5. **Requested/served scope regression.** A helper may accidentally reuse the selected served interval for process-history coverage. Keep `_gap_ranges` requested-scoped and give file admission its own known-gap collection.
6. **Reducer distinction.** The served-hour reducer sums this application's retained hourly `file_coverage.gap_intervals`; the requested-window reducer preserves the existing app-level `coverage.gap_intervals` scalar and its served-hour-plus-overlay semantics. They may differ because they are different reducers, not because one is a boot-wide persisted-row count. Assert A's `1` versus `4` and B's `2` versus `5` from the canonical fixture, including B's inequality with the one persisted served-scope row; do not deduplicate or substitute sources.
7. **App coverage versus provenance coverage.** `coverage.gap_intervals` is the existing requested-window app scalar and includes its served-hour contribution plus requested-window overlay. Do not replace it with `served_gap_count`, which is the retained app-hour reducer for `S_i`; the two values answer different reducer/scope questions and may legitimately differ.
8. **Unknown-owner compatibility carrier.** Renaming or replacing `unknown_owner_overflow.window` in the wire/durable model would be a schema/protocol change. Use the computed property and local facts instead.
9. **Snapshot skew.** Calling `file_table`, `file_coverage`, and `file_serve_facts` in separate snapshots can disclose bounds for one state and rows for another. The local worker must keep one committed snapshot for the result.
10. **Remote pagination.** A paginated remote table must preserve the first page's metadata, snapshot, requested window, sort, direction, and search. A later page must not widen the served interval or invent gap evidence.
11. **Tie-breaking and empty intervals.** The largest-contiguous helper's tie-break and the `None` distinction are externally observable. Do not turn “no interval” into a zero-length `Window` or a zero gap count.
12. **Footer repetition.** Updating only the state footer can leave observed/omission lines saying “whole window.” Scope text must be generated once and applied consistently to all coverage lines.
13. **Zero-row ambiguity.** A monitored app with no supported file observations can produce partial coverage with zero rows. The implementation must reach `empty_partial`, not `unavailable` or `empty_proven`, when the source and interval are otherwise valid.
14. **Status-refresh churn.** Replacing an in-flight history request on every refresh can prevent a result from ever publishing. Preserve the existing `keep_pending` behaviour while adding the label pins.

## 10. Approved-design ambiguities resolved by this specification

Design revision 8 is the authority for this specification. Revision 7 of the approved design itself specified the superseded persisted-gap-row served measure; revision 8, finalized at `0e92f44`, corrects it to an app-level served-hour reducer, preserves the requested scalar as a different reducer, and requires `None` when the hourly source is pruned. This is a documentary correction to the approved design, not an ambiguity that this specification may resolve in favour of revision 7.

The specification fills out the field types, no-served-interval rule, exact copy, and candidate-failure precedence without changing that design:

- `served_interval` is `Window | None`; `gaps_in_served_interval` is `int | None`; `gaps_in_requested_window` is always `int`.
- `gaps_in_served_interval` is the sum of this application's retained hourly `file_coverage.gap_intervals` rows intersecting the served interval. Persisted gap rows are provenance inputs only.
- `gaps_in_requested_window` is the unchanged `coverage.gap_intervals` scalar for this application: its existing served-hour contribution plus requested-window overlay. It is not the served reducer at a larger scope, and the two values must not be described as comparable.
- A local `served_gap_count` of `0` is a proved zero. `None` means hourly evidence is unavailable/pruned or the route cannot prove the reducer; it is never converted to zero.
- The requested `FileCoverage.window` remains distinct from the computed served interval and the compatibility carrier's nested window.
- The five visible outcome strings and their placement are the exact strings in section 3. The scope formatter preserves real `since_boot` end bounds, uses no invented lifetime bounds, and never displays the internal `boot` label.
- Each store selects its own candidate `S_i` before router acceptance. Only missing-app receipts, typed query/result-validation unavailability, and a mirror discarded after successful provenance are skippable, as specified in section 5.7. Provenance failure is fatal to the request even when another candidate is valid; one fully validated candidate is accepted only after the scan finishes without a fatal refusal, and two are ambiguous. Segment alignment and gap coverage are evaluated after clipping non-empty segments and known gaps to the candidate's `S_i`.
- `_gap_ranges` remains requested-process-window scoped; file admission may use a separate known-gap collection.
- `FileServeFacts` is local snapshot-bound query evidence with an explicit store-local `app_id`. `FileViewResult` transports only the local `served_gap_count` value inside the process; neither is a protocol field.
- The canonical reducer fixture must assert both applications: A has served/requested values `1`/`4`, B has `2`/`5`, and exactly one persisted gap row intersects their common served interval. B's assertions must kill both the persisted-row served reducer and reuse of A's facts for B; A-only assertions do not satisfy this gate.
- Exact nested wire field sets are pinned literally, with added-key and missing-key rejection at every nested record level.
- The real-database acceptance uses the named, hash-pinned frozen snapshot and independently derived expectations required by section 8; synthetic fixtures cannot satisfy that gate.

These are spec-level decisions; no product-scope expansion, schema change, migration, wire change, or protocol change is permitted.

## 11. Open items

None. The gap sources follow corrected approved design revision 8: the served value uses this application's retained hourly `file_coverage` rows, the requested value keeps the existing different scalar reducer, and pruned hourly evidence yields `None`. Candidate handling preserves the existing stage-based fallback and fatal provenance refusals under section 5.7. Neither review finding requires a new product-behaviour decision.

No unresolved product or implementation question is required to execute this specification. If implementation discovers a contradiction in the approved design rather than a defect in the code, it must stop the affected lane and obtain an explicitly reviewed decision instead of silently choosing a broader scope.
