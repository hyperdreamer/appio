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
