# AppIO Files-Tab Candidate Audit

Verdict: repair

Gate: assessed; candidate adoption pending

Audit scope: served-interval selection/propagation; candidate-local provenance; known gaps/overlap; requested-process gaps; route disclosure; test mechanisms

Source pins:

- Candidate: `dd8b042c31ff4189a0710bf9baeda0e880fcf77b`
- Base: `67493e28c3512174362ab7a8b5bb0ead20b98249`
- Spec SHA-256: `95d6c9c4b767ed96337746e572d67c5c1de2d149a5212a412c81526361e51b30`
- Approved design SHA-256: `ed64c5e36ddcdeebaa3e1495473225d58e6903897321b737e53e6a2aacf40492`

| Mechanism | Changed bytes/callers | Evidence | Pass/defect | Repair task |
|---|---|---|---|---|
| Served-interval selection and propagation | `src/appio/history_router.py::_read_local_files` reads each store's `FileTable` and `FileCoverage`, then derives the candidate scope from that result's `unknown_owner_overflow.window` and plans provenance for that scope. The query layer already uses the per-store file calendar for the returned rows. | The diff does not intersect calendars into one global scope. `_check_file_result` retains the requested `window` identity across the table and coverage. This is compatible with per-store selection by inspection, but the candidate tests do not use a shortened served scope with outside rows. | Mechanism appears aligned; acceptance is not established. The compatibility carrier is not the later computed `FileCoverage.served_interval` API. | T-002 and T-003; independent result binding in T-010. |
| Candidate-local provenance and failure stages | `history_router.py::_read_local_files` performs result validation inside the narrow `WindowUnavailableError` catch, then runs provenance outside it. It continues on missing receipts and typed pre-provenance unavailability, tests provenance before skipping a mirror, accumulates candidates through the scan, and refuses more than one validated result. | Inspection of the candidate code shows `worker._plan(served)` is run per result/store; a fatal provenance refusal is not caught as a skip, and a provisional result cannot short-circuit the final candidate count. Candidate tests do not include the mixed valid/invalid candidates in both orders or the separate typed-unavailability, missing-receipt, and mirror controls. | Code structure appears aligned with §5.7; the critical precedence behavior is unproven by the candidate tests. | T-008 and T-009. |
| Known-gap coverage and clipping | `history_router.py::_plan` populates `_known_gap_ranges`; `_read_local_files` plans against the candidate scope and unions known gaps to cover each unproved segment. | `_plan(served)` clips the exposed segments and known-gap ranges before the coverage loop. The loop requires gap-union coverage without a hole and checks hour alignment on the clipped segment endpoints. `overlap_uncertain` is excluded from known gaps; intersecting `_overlap_ranges` is refused. The cross-gap test covers a fully covered unproved interval whose requested and served scopes are equal. It does not cover boundary-straddling raw segments, a hole in the gap union, overlap-uncertain evidence, or zero-byte overlap. | Admission design appears repairable and consistent with §§5.3–5.5 by inspection; required edge cases and overlap independence are not proven. This does not justify `replace`. | T-006 and T-007. |
| Requested-window process gap isolation | The diff adds `_known_gap_ranges` but deletes the final `_gap_ranges = [(s.boot_id, s.start_ns, s.end_ns) for s in scoped if not s.proven]` assignment in `_plan`. The earlier append at candidate `history_router.py:509` is over unscoped selected intervals. `_materialize` returns `_gap_ranges`; `_combine_coverage` consumes it for process coverage. | No equivalent requested-window reassignment exists in the candidate. A gap outside the requested process window can therefore reach process coverage. The candidate tests assert no process-gap scope distinction. | Defect; non-negotiable. The candidate cannot be adopted as-is. | T-005. `M_requested_gap_scope_deleted` must be killed by the outside-request control in the process-scope pin. |
| Route disclosure and gap-count scope | `src/appio/ui/files_view.py::_render_coverage` adds served-bound copy and labels `inner.gap_intervals` as a count “inside served interval,” including on the generic remote route. | `QueryService._file_app_coverage` obtains the app coverage rows for the selected file query bounds but applies `_file_overlays` using the requested `Window`. Thus `coverage.coverage.gap_intervals` retains the requested-window reducer (served-hour contribution plus requested-window overlay); it is not the served-hour reducer. The candidate's disclosure test uses a request equal to the served scope, so reusing that scalar passes. Protocol 1 also cannot prove local served-hour evidence. | Defect in the changed footer behavior. It mislabels the existing requested-window scalar as a served-interval count and does not represent remote/pruned unavailability. The future `FileServeFacts`, local `served_gap_count`, and exact scope formatter/wire rules remain later contracts; their absence is not evidence that the candidate implemented the full spec. | T-010 for local result binding; global Tasks 11 and 15–20 for remote/local disclosure and footer placement. |
| Candidate and baseline test mechanisms | Candidate adds four tests in `tests/p4/test_file_history_router.py`; existing baseline pins are in `tests/p4/test_file_served_interval.py`. | Candidate tests: `test_hour_aligned_file_window_serves_rows_across_known_gap_and_discloses_interval` exercises a fully covered known-gap admission, rows, incomplete coverage, and a footer when request equals served scope; `test_hour_aligned_file_window_still_refuses_ambiguous_ownership` exercises an ownership refusal but not a zero-counter overlap mutant; `test_hour_aligned_file_window_without_file_coverage_is_unavailable` exercises no file calendar; `test_gap_free_hour_aligned_file_window_remains_complete` is the complete control. The three baseline pins (`test_unproven_served_interval_still_refuses`, `test_ambiguous_served_interval_ownership_still_refuses`, and `test_no_proven_file_interval_renders_unavailable`) ran as the specified targeted group: 7 passed. The four candidate tests also ran from an unmodified archive of `dd8b042`: 4 passed. These green results are not mutation evidence. | The inventory supplies useful positive controls but does not falsify the critical selection, clipping, overlap, requested-process-scope, reducer, or failure-stage mechanisms. | T-005 through T-009 provide the router pins; T-010 and Tasks 11, 15–20 provide result and disclosure pins. |

Non-negotiable 1: restore requested-window `_gap_ranges` scoping

The candidate's `_known_gap_ranges` is a separate admission input, but it does not replace the process consumer's need for `_gap_ranges` scoped to the requested window. T-005 must prove both that a gap inside the requested process window but outside the served file interval remains in process coverage, and that a gap outside the requested process window does not make process history incomplete.

Non-negotiable 2: a green suite is not evidence

The targeted baseline result (7 passed), candidate-only result (4 passed), and any aggregate green result do not establish the changed mechanisms. No candidate mutation was run in this read-only audit. The missing pins below must be executed as isolated, named mutations with the exact killing assertion recorded; no mutation is represented here as observed evidence.

Required falsifiers and current status:

- `M_requested_gap_scope_deleted`: required Task 5 outside-request process-scope control; not run in this audit.
- `M_provenance_failure_becomes_skip`: required Task 9 mixed-candidate refusal in both store orders; not run.
- Pre-clipping alignment and any-gap-hit mutations: required Task 6 boundary-straddling and gap-union assertions; not run.
- Overlap bypass: required Task 7 intersecting zero-counter overlap assertion; not run.
- First-candidate return and global-served-scope mutations: required Tasks 8–9 per-store selection/order assertions; not run.
- Reuse of the requested scalar as served-gap evidence: candidate's equal-scope fixture cannot kill it; the later facts/result and exact-disclosure pins must distinguish the two reducers and remote `None`.

Re-audit/adoption prerequisites: Tasks 5–9 and later independent acceptance Task 24

`repair` is not adoption. The report permits the specified repair work to proceed; the findings remain blocking until the named pins and re-audit evidence are completed. Do not import or cherry-pick the candidate as an adopted implementation before those gates. Task 24's independent, hash-pinned real-database acceptance remains required.

Validation record:

- `git show --stat dd8b042` reported commit `dd8b042c31ff4189a0710bf9baeda0e880fcf77b`, subject `fix(router): serve hourly file windows across disclosed gaps`, and three changed files: `src/appio/history_router.py` (38 lines changed), `src/appio/ui/files_view.py` (34 lines changed), and `tests/p4/test_file_history_router.py` (121 insertions); total 183 insertions and 10 deletions.
- Targeted baseline command from the plan: 7 passed in 1.19s.
- Candidate-only tests from an unmodified archive: 4 passed in 1.35s.

## T-005 Re-Audit Addendum: Requested-Window Process Gap Isolation

The base implementation preserves the exact requested-window assignment in
`src/appio/history_router.py:511`:

```python
self._gap_ranges = [(s.boot_id, s.start_ns, s.end_ns) for s in scoped if not s.proven]
```

The file is byte-identical to lane base `67493e28c3512174362ab7a8b5bb0ead20b98249`
(SHA-256 `a18f94f9b9a51ed782c7ab00e2e2025673f0679bd6e6e4bc834c0a2d7177a46a`).
The earlier unscoped append at `:488` is superseded by this requested-scope
assignment. `_read_local` returns `_gap_ranges` at `:650`, `_consume_local`
installs those returned gaps at `:708`, and `_combine_coverage` counts their
merged ranges for process coverage at `:735`. Initialization and request reset
remain at `:118` and `:671`. The candidate archive lacks the final assignment;
this re-audit confirms the repair target is already correct on the lane base,
so no production source change was needed.

The new `tests/p4/test_files_tab_process_scope.py` contains both required
controls. The final validation command passed: `2 passed in 0.49s`. Isolated probe
controls and restored runs each passed both tests:

- `M_requested_gap_scope_deleted` removed only the scoped assignment. Both
  tests failed at their `_gap_ranges` assertions: the first included the
  outside-request `[0h,1h)` range; the second also observed that range instead
  of an empty set. See `evidence/T-005/mutants/M_requested_gap_scope_deleted/`.
- `M_process_uses_S` substituted `S` for `R2` at the process-planner call
  boundary in the disposable test copy. The first assertion observed no
  `[4h,5h)` range (`[]` instead of the expected range). This is a caller-scope
  substitution probe; the tracked production test and source were restored
  byte-identically. See `evidence/T-005/mutants/M_process_uses_S/`.
- `M_served_counts_requested_gap` changed only the disposable query copy to
  populate `gaps_in_served_interval` from the requested-window scalar. The
  first test failed at the served-count assertion (`1 != 0`). See
  `evidence/T-005/mutants/M_served_counts_requested_gap/`.

Each probe recorded its unmutated control, mutation diff, module origin,
mutant failure, restored run, and restored SHA-256. The relevant imports came
from each disposable tree. This closes the requested-process-scope finding
only; the `repair` verdict remains in force and candidate adoption remains
pending Tasks 6-9 and independent acceptance Task 24.

## T-006 Re-Audit Addendum: Clipped Known-Gap Admission

**Disposition:** repaired/reworked this admission mechanism only. The candidate
used an inline check over the planner's already-scoped segments. The lane now
has the explicit `_check_file_provenance(worker, served, supplying_name)`
predicate: it clips `_all_segments` and known gap intervals to the candidate's
own served scope, validates the full aligned union, rejects intersecting
overlap/unknown chronology and non-single-owner plans, then returns `False`
for a mirror only after those predicates pass. Fatal provenance refusal is
retained and raised after scanning every candidate, so later candidates are
examined without suppressing the request-level refusal. This is not adoption
of the candidate commit. The request-level process `_gap_ranges` assignment
from T-005 remains unchanged; `_known_gap_ranges` is separate and excludes
`overlap_uncertain` rows.

The test fixture observes the raw unproved segment `[1.5h,3h)` and its clipped
candidate segment `[2h,3h)`. Before the admission change, the baseline had
`2 failed, 6 passed`: both aligned full-union acceptance cases were refused by
the strict existing gate. Separately, the scan-order pin was red before the
deferred-error change: `checked` contained only `first`, not both candidates.
The final clipping tests pass `7 passed in 1.10s`; the combined clipping and
process-scope validation passes `9 passed in 1.43s`.

Six origin-checked disposable probes each had a 9-test green control, an
assertion-level mutant failure, byte-identical restore, and a 9-test restored
green run:

- `M_align_raw_before_clip`: both aligned acceptance cases and the outside-S
  case were refused when the raw segment was tested without clipping.
- `M_any_gap_hit_is_enough`: the hole case was admitted; its expected refusal
  assertion failed.
- `M_overlap_gap_is_known`: the explicit classification assertion failed
  because the `overlap_uncertain` row appeared in `_known_gap_ranges`.
- `M_widen_S_to_raw_segment`: both accepted cases failed when provenance was
  planned against requested `R` instead of served `S`.
- `M_no_alignment_check`: the `[2h,3.5h)` case was admitted; its expected
  refusal assertion failed.
- `M_stop_scan_after_first_provenance_error`: the scan-order assertion failed
  because only `first` was checked after the fatal refusal.

Evidence is under `evidence/T-006/mutants/`, including each module origin,
mutation diff, control/mutant/restored logs, and checked restore hashes. This
re-audit covers clipped known-gap admission only. The overall `repair` verdict
and pending candidate-adoption gate remain unchanged; overlap, candidate
selection/error-classification tasks and independent acceptance Task 24 remain
to be reviewed.

## T-007 Re-Audit Addendum: Served-Scope Overlap Refusal

**Disposition:** no router source repair was required. The T-006 predicate
already refuses when planner overlap ranges intersect `served`, and the planner
marks either persisted `overlap_uncertain` intervals or competing owner/epoch
candidates as overlap uncertainty. These tests independently pin both sources;
zero byte totals do not change the refusal.

`test_overlap_uncertainty_is_intersection_scoped_even_with_zero_bytes` covers
an `overlap_uncertain` row intersecting `S=[2h,4h)` and an outside row
`[4h,5h)`. Both set file sample read/write counters to zero. The inside row
refuses with the exact `file_source_unavailable` pair; the outside row succeeds
with served interval `S` and zero observed read bytes. The separate
`test_overlapping_owner_epochs_refuse_without_persisted_gap_row` commits GUI
epoch 1 over `[0h,6h)` and IOD epoch 2 over `[2h,4h)`, verifies there is no
persisted overlap row, and observes the same refusal.

The overlap tests pass `3 passed in 0.36s`; the combined overlap/clipping
validation passes `10 passed in 0.52s`. Three disposable, origin-checked probes
each had a 10-test green control, the named assertion failure, byte-identical
restore, and a 10-test restored green run:

- `M_overlap_ignored_when_zero` removed intersecting `overlap_uncertain` rows
  from per-segment persisted evidence; the inside-S `pytest.raises` assertion
  failed because the table was returned.
- `M_overlap_uses_R` substituted requested `R` for served `S`; the outside-S
  acceptance assertion failed, along with the T-006 straddling acceptances.
- `M_ignore_planned_epoch_overlap` forced the competing-owner predicate false;
  the no-persisted-row epoch test's refusal assertion failed.

An initial weaker probe that changed only the planner's `uncertain` boolean
survived because the separate persisted-gap `missing` predicate still rejected
the segment. It is retained under
`evidence/T-007/mutants/M_overlap_ignored_when_zero/attempt-1-survived/` and is
not counted as a killed mutant; the final probe removes the persisted overlap
evidence from the per-segment classifier and is killed as required. No
production file changed in T-007. This closes only the overlap predicate gate;
the candidate `repair` verdict and remaining adoption/acceptance gates persist.
