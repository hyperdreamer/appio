# appio Files Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking; leave them unticked because the controller pins this document's digest.

**Goal:** Render truthful Files results for the interval actually served, preserve the existing status-label authority, and prove both against the signed-off specification.

**Architecture:** Keep the requested `Window` on the transported table and coverage records, expose the existing effective scope through a computed property, and compute application-specific served-hour facts inside the local query snapshot. Admit one store only after every candidate has undergone the existing stage-based query/provenance checks; pass a local-only optional gap count to the view, which renders the five ordered outcomes and canonical disclosures. Preserve label implementation and add transition pins rather than redesigning witness precedence.

**Tech Stack:** Python 3.14.8, SQLite schema v1, frozen dataclasses, existing protocol 1 and HMAC cursors, PyQt6, pytest 9.1.1, pytest-qt 4.5.0, synthetic committed SQLite fixtures, and one frozen real-store acceptance fixture.

### Authority and scope map

- Sole scope: `docs/superpowers/specs/2026-10-30-appio-files-tab-spec.md`, revision 3, commit `bfbb5f664b758c36847708a44d4f31aa0f47826d`, SHA-256 `95d6c9c4b767ed96337746e572d67c5c1de2d149a5212a412c81526361e51b30`.
- Approved design: `docs/superpowers/specs/2026-10-30-appio-files-tab-design.md`, revision 8, commit `0e92f44`, SHA-256 `ed64c5e36ddcdeebaa3e1495473225d58e6903897321b737e53e6a2aacf40492`.
- Candidate, not an adopted dependency: `dd8b042c31ff4189a0710bf9baeda0e880fcf77b`. It is not an ancestor of the planning base. Do not cherry-pick its three-file change as a substitute for the audit.
- There are exactly 24 tasks: one for each row of specification §8. Reordering below puts the candidate gate first and respects interface dependencies. A task may be tests-only when its behavior already exists; do not fabricate a red baseline or rewrite correct production code merely to make it look like a feature change.

| Specification §8 contract pin | Task |
| --- | --- |
| Candidate adoption | 1 (audit decision); its conditional repair/replacement is executed and re-audited in 5–9 |
| Served interval propagation and outside rows | 2 |
| `FileServeFacts` reducers and types | 3 |
| FileCoverage wire shape | 4 |
| Requested `_gap_ranges` and process isolation | 5 |
| Unproved segment clipping | 6 |
| Overlap uncertainty | 7 |
| Per-store candidate selection | 8 |
| Fatal versus skippable candidate failures | 9 |
| Local result shape and identity | 10 |
| Remote result shape | 11 |
| Complete result | 12 |
| Totals-only/pruned semantics | 13 |
| No authoritative source | 14 |
| Scope formatter and bounds | 15 |
| Ordered rows outcome | 16 |
| Filtered zero outcome | 17 |
| Complete empty outcome | 18 |
| Partial empty outcome | 19 |
| Exact disclosure placement | 20 |
| Stale reply | 21 |
| Label probe-only truth | 22 |
| Label recovery | 23 |
| End-to-end acceptance (real database) | 24 |

### File structure and responsibility

- `src/appio/contracts.py`: only the O(1) computed `FileCoverage.served_interval` property; no fields or transport records change.
- `src/appio/query.py`: local `FileServeFacts` value and `file_serve_facts(app_id, window)`; convert internal effective-scope readers without changing interval selection or existing reducers.
- `src/appio/history_router.py`: requested-process gap isolation, candidate-local provenance admission, local optional facts propagation, and remote unknown-gap propagation. Keep executor and continuation bounds.
- `src/appio/ui/files_view.py`: pure scope formatting, ordered outcome rendering, and coverage-line disclosure. Do not import query/router production modules into this thin UI layer.
- `tests/p4/files_tab_support.py`: committed synthetic SQLite cases and direct local-worker driver, not a production query oracle.
- `tests/p4/files_tab_view_cases.py`: contract-shaped view results, canonical expected text, and offscreen widget construction.
- Each `tests/p4/test_files_tab_*.py` named below owns a single contract pin. Existing regression files are changed only where a signature or specified copy changes.
- `tests/p3/test_collector_window.py`: existing `FileViewResult` fixture arity conversion and the two label transition pins; no collector production edits are authorized.
- `tests/fixtures/p4/files-tab-real.sqlite`, `.sha256`, `.json`, and `.oracle.json`: real immutable snapshot, pin, capture/query manifest, and independently derived raw-row expectations.
- `tests/p4/capture_files_tab_snapshot.py`, `files_tab_real_oracle.py`, and `test_files_tab_real_acceptance.py`: acceptance-only capture/verification, raw SQL oracle, and offscreen checks.
- `docs/superpowers/reviews/2026-10-30-appio-files-tab-candidate-audit.md` and `2026-10-30-appio-files-tab-acceptance.md`: scoped evidence, not new product requirements.

### Declared dependency edges and parallelism

These are immediate directed edges; transitive prerequisites are mandatory too:

```text
1 -> 2
2 -> 3, 4
3 -> 5, 13
5 -> 6
6 -> 7, 8
8 -> 9
3, 7, 9 -> 10
10 -> 11, 12, 14, 22
11, 12, 13, 14 -> 15
15 -> 16 -> 17 -> 18 -> 19 -> 20
11, 20 -> 21
22 -> 23
4, 21, 23 -> 24
```

- Task 1 must finish with a reviewed, explicit audit disposition before any task imports or relies on candidate behavior. `repair` is not adoption: unresolved findings stay blocking until 5–9 re-audit them. `replace` means execute the specification's mechanism without taking candidate bytes. No implicit whole-commit adoption is permitted.
- After 2, wire pin 4 can run independently of 3: it writes only its new test file. After 3, totals-only pin 13 can run independently of the router work at 5–11. Its production allowlist is query-only, not router/UI.
- After 6, overlap pin 7 and candidate-selection pin 8 can execute in parallel only if neither needs a production repair. Both can modify `history_router.py`; if either needs that allowance, serialize the router edits, then rerun both pins. The default controller executes numerically, which is safe.
- After 10, tasks 11 (router/remote), 12 (new complete-case test), 14 (unavailable UI), and 22 (P3 label tests) have independent tests. Tasks 11 and 14 both have a router repair allowance: serialize them if that allowance is exercised. Task 10's P3 fixture conversion must be integrated before 22. Task 23 follows 22 because both touch the same label-test file.
- Tasks 15–20 deliberately serialize: they modify the same rendering functions and compatibility assertions. Task 21 consumes their final disclosure plus remote generation checks. Task 24 is the sink for every branch, not an early smoke check.
- Do not claim all query, router, and UI tasks are parallel lanes. Their signatures, shared files, and candidate acceptance order make that false. Temporary mutation experiments also run serially per checkout.

### Execution readiness and explicit decisions

- No product contradiction has been established. There is a concrete **acceptance input block**: at planning time the required fixture is absent, `/tmp/iod-snapshot.db` is absent, and `/var/lib/appio/iod.db` exists but is not readable by this session (`root:appio`, mode `0600`). A plan must not turn that into an instruction to use root.
- Task 24 requires a readable, already-consistent frozen capture supplied by the target-store custodian, together with its capture identity. The plan chooses `/tmp/appio-files-tab-capture/iod.sqlite` and `/tmp/appio-files-tab-capture/capture.json` as the handoff paths. These are input artifacts, not permission to synthesize data. If they are absent, unreadable, lack provenance, or cannot supply a qualifying partial result, stop Task 24 as blocked. Do not skip its test or declare acceptance.
- Compute and retain the real snapshot hash when capturing that input; do not invent a planning-time SHA-256 for bytes not available to the planner. The specification requires a hash-pinned implementation deliverable, not a fictitious digest in this plan.
- Other implementation choices left open by the spec are fixed here: put `FileServeFacts` in `appio.query`; give the third `FileViewResult` field no implicit zero/default; use private module-level `_scope_bounds` and `_scope_text` in `files_view.py`; keep the existing unavailable footer marker rather than display numeric zeroes; select an acceptance case deterministically from the raw snapshot, never by a production reducer. No new runtime dependency is needed.

## Global Constraints

- Normative scope is revision 3 of `docs/superpowers/specs/2026-10-30-appio-files-tab-spec.md` (SHA-256 `95d6c9c4b767ed96337746e572d67c5c1de2d149a5212a412c81526361e51b30`), following approved design revision 8 (`0e92f44`, SHA-256 `ed64c5e36ddcdeebaa3e1495473225d58e6903897321b737e53e6a2aacf40492`). Read those files in each fresh task before interpreting its contract.
- No schema change, no migration, no new table, no wire-protocol or ABI change, no Overview redesign, no collection-cadence change, no new user-facing copy beyond the exact approved disclosures in specification §3.
- Preserve existing ownership, overlap, unavailable-source, and zero-versus-unknown honesty semantics. `_gap_ranges` remains scoped to the requested process-history window; file admission uses separate known-gap evidence. A green suite is not evidence of adoption.
- All intervals are half-open `[start, end)`. `FILE_HOUR_NS` is `3_600_000_000_000`. Do not change `_largest_contiguous_file_interval` or its latest-end tie-break; do not shorten an interval merely to evade provenance refusal.
- `FileTable.window == FileCoverage.window == requested_window`; `FileCoverage.served_interval` is computed from the existing compatibility carrier, never a dataclass/wire field. Rows and observed bytes use that served scope. Completeness and shortening are different facts.
- `gaps_in_served_interval` sums this application's retained hourly coverage evidence; `gaps_in_requested_window` is the unchanged served-hour-plus-requested-overlay scalar. Assert BOTH canonical apps: A `(S,1,4)`, B `(S,2,5)`, persisted-row counts S=`1`, R=`3`. `None` is never `0`.
- Every Files result binds one requested scope, effective scope, selected application, and pinned read snapshot. Resolve external AppKey to positive store-local `app_id` in that snapshot; map to the presentation registry only after validation. Remote `served_gap_count` is always `None`.
- Candidate skips are stage-based: missing receipt; typed query/result-validation unavailability before provenance; or a mirror only AFTER successful provenance. Provenance, unexpected reads/planning failures, and multiple validated candidates are fatal. Never return the first provisional success.
- Label work is preservation-only: retain `refresh`, `_apply_body`, `display_state`, `decide_owner`, reconciliation, activation overrides, unprivileged status authority, and current verified+answering promotion. No production label repair is in any allowlist. Preserve `keep_pending`.
- Use exactly this pinned interpreter for every test: `/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python` (Python 3.14.8, pytest 9.1.1, pytest-qt 4.5.0, PyQt6). Fail preflight if unavailable; do not silently substitute an interpreter or install packages.
- Every test command begins with `/usr/bin/env QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1` and uses `-p no:cacheprovider`. Commands are literal argv arrays run from repository root with `shell=False`; no shell pipelines, redirections, command substitution, glob expansion, or chained shell commands.
- Do not run the full suite, use root/sudo/pkexec, load BPF, query real `/proc` or `/sys`, contact a live daemon, or use network. Existing in-process AF_UNIX `socketpair` harnesses with injected peer credentials are allowed; they do not need a root peer or an external service. Synthetic databases use committed observations/receipts, never a real collector.
- Each task may change only its exact file allowlist. Listed line ranges refer to planning base `bfbb5f6`; anchors, not stale absolute line numbers, locate moved code. Read-only references are not edit authority. Generated test data must stay inside pytest `tmp_path`/temporary directories, not tracked fixtures, except Task 24's explicitly allowed real artifacts.
- Tests first: record the unmodified baseline; add the named assertion; observe a behavioral red failure when missing. If already green, prove it with the named single-mechanism mutation instead. Each mutation runs in a disposable isolated copy or through an in-process test patch, never in a shared worktree; record the exact assertion failure, restore byte-for-byte, then rerun the positive pin. An import/fixture/setup failure is not a killing assertion. No test may delete the assertion that detects its mutant.
- Keep targeted baseline/mutant/restored logs and SHA-256s in the controller's per-task evidence directory outside tracked source. Verify imported module `__file__` belongs to the disposable tree when using a copied checkout (repository `pythonpath=["src"]` and inherited `PYTHONPATH` must not load the original code).
- Only Task 1 may write the candidate-audit report until its verdict is committed. Its source/test assessment is read-only. Tasks 5–9 append their re-audit evidence only after Task 1 permits repair or replacement. Any contradiction in the approved design stops the affected lane for an explicitly reviewed decision.
- Required acceptance input is the custodian-supplied frozen real target capture `/tmp/appio-files-tab-capture/iod.sqlite` plus `capture.json`. Missing/unreadable input, a hash mismatch, synthetic substitution, no qualifying partial case, or a production-derived oracle is a hard Task 24 block/failure, never `pytest.skip`.
- Commit only each task's allowlisted deliverables with its exact commit message. Do not edit this plan, the spec/design, schema/migrations, `files.py`, collection runtime, or protocol implementation. Leave checkbox state unchanged.

## Task 1: Read-only candidate audit and disposition gate

**Implementer tier:** Capable

**Contract:** Candidate adoption, specification §§6 and 8. This is an assessment gate, not permission to change or adopt the candidate.

**Depends on:** Nothing. Every subsequent task depends transitively on this gate.

**Files (exclusive modification allowlist):**
- Create: `docs/superpowers/reviews/2026-10-30-appio-files-tab-candidate-audit.md`.
- Source and tests: read-only, including the complete `git show dd8b042` and its callers. No tracked source/test edits or whole-suite run.

**Interfaces:**
- Consumes: candidate `dd8b042c31ff4189a0710bf9baeda0e880fcf77b`; `HistoryRouter._read_local_files(paths, window, key, app_id, sort, direction, search) -> FileViewResult`; `_plan(window: Window) -> None`; process consumers `_combine_coverage(rows)` and `_materialize`.
- Produces: a committed audit report with exactly one `Verdict: pass`, `Verdict: repair`, or `Verdict: replace`, a mechanism/finding table, and `Gate: assessed; candidate adoption pending` unless an evidenced pass is possible. Repair/replacement findings name their implementing task and killing assertion.

- [ ] **Step 1: Establish the read-only test inventory before assessing the diff.**

Record the candidate tests `test_hour_aligned_file_window_serves_rows_across_known_gap_and_discloses_interval`, `test_hour_aligned_file_window_still_refuses_ambiguous_ownership`, `test_hour_aligned_file_window_without_file_coverage_is_unavailable`, and `test_gap_free_hour_aligned_file_window_remains_complete` from the candidate tree, not names guessed from HEAD. Map each to the admission/refusal it actually exercises. Assess baseline pins `test_unproven_served_interval_still_refuses`, `test_ambiguous_served_interval_ownership_still_refuses`, and `test_no_proven_file_interval_renders_unavailable`. Write the report with these headings (use H3 inside the report if quoting it in this plan):

```text
Verdict: repair
Gate: assessed; candidate adoption pending
Audit scope: served-interval selection/propagation; candidate-local provenance; known gaps/overlap; requested-process gaps; route disclosure; test mechanisms
Source pins: full candidate SHA; base SHA; spec/design SHA-256s
Mechanism | changed bytes/callers | evidence | pass/defect | repair task
Non-negotiable 1: restore requested-window _gap_ranges scoping
Non-negotiable 2: a green suite is not evidence
Re-audit/adoption prerequisites: Tasks 5–9 and later independent acceptance Task 24
```

The initial `repair` is the expected disposition because the visible diff deletes the requested-window assignment. Change it to `pass` only with a cited equivalent assignment elsewhere in that candidate, not because the base already has the assignment. Change to `replace` if its served-gap design cannot satisfy §§5.1–5.7 without reinterpretation. State explicitly that the full new facts API/wire inventory/exact rendering are later contracts, not grounds to pretend the candidate implemented the whole spec.

- [ ] **Step 2: Execute targeted read-only baseline tests and inspect the candidate bytes.**

Use `git show dd8b042`, `git show dd8b042:src/appio/history_router.py`, and `git show dd8b042:tests/p4/test_file_history_router.py` as argv commands. Trace the deleted `_gap_ranges = ... for s in scoped` assignment to process coverage. Inspect per-store selection, catch boundaries, clipping before alignment, gap-union coverage, and remote disclosure. Candidate-only tests may run from an unmodified disposable archive of that commit; never apply the candidate to the integration checkout. Record missing mechanism pins as findings, not as a passing mutation run.

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_file_served_interval.py::test_unproven_served_interval_still_refuses","tests/p4/test_file_served_interval.py::test_ambiguous_served_interval_ownership_still_refuses","tests/p4/test_file_served_interval.py::test_no_proven_file_interval_renders_unavailable"]
```

Expected: report the actual targeted results; passing them alone cannot yield adoption.

- [ ] **Step 3: State falsifiers and close the assessment, not implementation.**

The report must name `M_requested_gap_scope_deleted` (Task 5 must fail its outside-R control), `M_provenance_failure_becomes_skip` (Task 9 must refuse A/B in both orders), pre-clipping alignment/any-gap-hit mutants (Task 6), overlap bypass (Task 7), and first-candidate/global-S mutants (Task 8/9). Record which candidate assertions cannot kill them. No unexecuted mutation is described as observed evidence. This read-only gate may use in-memory inspection/patching but may not modify source/tests to manufacture evidence. Later implementation tasks supply missing executable pins and re-audit changed bytes. The reviewed report unlocks that work; it does not unlock use of uncorrected candidate bytes.

- [ ] **Step 4: Commit**

Stage only the report. Exact commit message:

```text
docs: audit Files served-interval candidate
```

## Task 2: Preserve requested identity and expose the effective served scope

**Implementer tier:** Advanced

**Contract:** Served interval propagation and outside rows, §§2.3, 2.6 and 8.

**Depends on:** 1 (reviewed disposition).

**Files (exclusive modification allowlist):**
- Modify: `src/appio/contracts.py:681-727` (`FileCoverage` methods only).
- Modify: `src/appio/query.py:930-1065` (effective-scope readers only).
- Create: `tests/p4/files_tab_support.py`.
- Create: `tests/p4/test_files_tab_propagation.py`.
- Modify: `tests/test_query.py:540-718` (convert served-scope assertions without weakening them).

**Interfaces:**
- Consumes: existing `FileTable`, `FileCoverage`, `UnknownOwnerOverflow`, `Window`; `_largest_contiguous_file_interval(start, end, watermark, hours) -> tuple[int,int] | None` unchanged.
- Produces: `FileCoverage.served_interval: Window | None`, a property; test helper `tab_store` fixture yielding `make(name="local", *, hours=(0,2,3,5), app_gaps=None, gaps=((H,2*H,"collector_gap"),(3*H,4*H,"collector_gap"),(4*H,5*H,"collector_gap")), owner="gui", start=0, end=6*H, apps=(1,2)) -> sqlite3.Connection`; constants `H`, `BOOT="boot-A"`, `A="unit:app-demo.service"`, `B="unit:app-b.service"`, `R=Window("range",BOOT,0,6*H)`, `S=Window("range",BOOT,2*H,4*H)`; `read_files(stores, window=R, key=A, app_id=101) -> FileViewResult`, with `stores` a tuple of `(collector_id, connection, owner)`.

- [ ] **Step 1: Add committed synthetic fixture support and the propagation pin.**

Use the existing producer helpers, not hand-written coverage JSON. The support module contains:

```python
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import pytest
from appio.contracts import FILE_HOUR_NS as H, Window
from appio.durable import Receipt
from appio.file_store import FileHourStore
from appio.files import HourlyAccumulator
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry
from appio.migrations import open_database
from appio.query import QueryService
from test_files import APP as A, ZERO, observation

BOOT = "boot-A"
B = "unit:app-b.service"
R = Window("range", BOOT, 0, 6 * H)
S = Window("range", BOOT, 2 * H, 4 * H)

@pytest.fixture
def tab_store(tmp_path):
    connections = []
    def make(name="local", *, hours=(0, 2, 3, 5), app_gaps=None,
             gaps=((H, 2*H, "collector_gap"),
                   (3*H, 4*H, "collector_gap"),
                   (4*H, 5*H, "collector_gap")),
             owner="gui", start=0, end=6*H, apps=(1, 2)):
        db = open_database(tmp_path / (name + ".sqlite"))
        connections.append(db)
        db.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) VALUES(?,1,0,1000.0,?)", (BOOT, owner))
        for aid in apps:
            key = A if aid == 1 else B
            db.execute("INSERT INTO app VALUES(?,?,?,NULL,1000.0,1000.0,'user')", (aid, key, "A" if aid == 1 else "B"))
        db.execute("INSERT INTO collector_interval VALUES(?,?,1,?,?)", (owner, BOOT, start, end))
        db.commit()
        counts = {(1, 2): 0, (1, 3): 1, (2, 2): 1, (2, 3): 1} if app_gaps is None else app_gaps
        for hour in hours:
            acc = HourlyAccumulator(BOOT, hour * H)
            for aid in apps:
                key = A if aid == 1 else B
                count = counts.get((aid, hour), 0)
                cov = replace(ZERO, gap_intervals=count, complete=(count == 0))
                acc.add_observation(Receipt(owner, 1, hour), key,
                    observation(100*aid + hour, app_key=key,
                        basename=f"app-{aid}-hour-{hour}.txt",
                        bytes_r=aid*(hour+1)*10,
                        last_seen_mono_ns=hour*H + 100_000_000_000), cov)
            FileHourStore(db).finalize_hour(acc.finalize())
        for index, (left, right, reason) in enumerate(gaps, 1):
            db.execute("INSERT INTO gap VALUES(?,?,?,?,?,?,?,?)", (index, BOOT, owner, 1, index, left, right, reason))
        db.commit()
        assert not db.in_transaction
        return db
    yield make
    for db in connections:
        db.close()

def read_files(stores, window=R, key=A, app_id=101):
    paths = tuple((name, db.execute("PRAGMA database_list").fetchone()[2], owner)
                  for name, db, owner in stores)
    return HistoryRouter._read_local_files(paths, window, key, app_id,
                                          "bytes_total", "desc", None)
```

The propagation test imports `tab_store`, `A`, `B`, `BOOT`, `H`, `R`, `S` from that module (importing a fixture explicitly registers it). Its core is:

```python
def test_served_propagation_excludes_outside_rows_for_both_apps(tab_store):
    db = tab_store()
    service = QueryService(db)
    with service._snapshot():
        assert dict((key, total) for key, name, total in service.file_applications(R)) == {A: 70, B: 140}
        for aid, expected in ((1, 70), (2, 140)):
            table = service.file_table(aid, R, "bytes_total", "desc", None)
            cov = service.file_coverage(aid, R)
            assert table.app_id == cov.app_id == aid
            assert table.window == cov.window == R
            assert cov.served_interval == S
            assert table.unknown_owner_overflow.window == S
            assert {row.basename for row in table.rows} == {f"app-{aid}-hour-2.txt", f"app-{aid}-hour-3.txt"}
            assert sum(row.bytes_r for row in table.rows) == cov.observed_bytes_r == expected
            assert cov.observed_bytes_w == 0
```

Add `test_unavailable_coverage_has_no_served_interval` by replacing a contract-shaped coverage with `unavailable_reason="not_retained"`; assert the property is `None` even if the compatibility carrier still contains a placeholder request. Include an incomplete/full-scope control: `complete=False` alone must not make the property `None` or shorten it.

- [ ] **Step 2: Run the failing pins before implementation.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_propagation.py","tests/test_query.py"]
```

Expected new red: missing property; existing selection assertions may already be green.

- [ ] **Step 3: Add only the property and convert readers to the honest scope name.**

```python
@property
def served_interval(self) -> Window | None:
    if (self.coverage.unavailable_reason is not None or
            self.unknown_owner_overflow.coverage.unavailable_reason is not None):
        return None
    return self.unknown_owner_overflow.window
```

Put this method on `FileCoverage`, not a field. In query code retain `_unknown_owner` as the compatibility producer; name its effective window/bounds explicitly before calling `_file_rows`, `_file_app_coverage`, or selector SQL. Do not construct `FileCoverage` merely to read a property inside `file_applications`; the producer necessarily reads `unknown.window`. Outside that compatibility producer, public consumers use `coverage.served_interval`. Keep request `window` in every returned record and leave the largest-interval helper untouched.

- [ ] **Step 4: Falsify and restore, then rerun validation.**

Named mutations for the new propagation test: `M_rows_use_requested_bounds` (force only `_file_rows` back to R), `M_applications_use_requested_bounds` (selector uses R), and `M_coverage_uses_requested_bounds` (observed bytes use R). Each must fail its own row/70-or-140 assertion rather than merely a footer. `M_unavailable_carrier_is_served` must fail the `None` assertion. Run both applications under every relevant variant; do not replace B with a duplicate of A.

- [ ] **Step 5: Commit**

```text
feat(files): expose effective served scope without changing records
```

## Task 3: Add application-specific snapshot-local serve facts

**Implementer tier:** Advanced

**Contract:** `FileServeFacts` reducers and types, §§2.2, 5.5 and 8.

**Depends on:** 2.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/query.py:1-40,930-1073` (local dataclass and method).
- Create: `tests/p4/test_files_tab_facts.py`.

**Interfaces:**
- Consumes: `FileCoverage.served_interval: Window | None` from 2; `tab_store` fixture and `H,BOOT,A,B,R,S` from `tests.p4.files_tab_support`; `QueryService.file_coverage(app_id:int, window:Window) -> FileCoverage`; `_snapshot()`, `_file_query_context(window)`, `_file_coverage_rows(scope,boot_id,start,end,app_id)`.
- Produces in `appio.query`: frozen `FileServeFacts(served_interval: Window | None, gaps_in_served_interval: int | None, gaps_in_requested_window: int)` and `QueryService.file_serve_facts(self, app_id:int, window:Window) -> FileServeFacts`. No transport registration.

- [ ] **Step 1: Write both-app reducer and immutability pins.**

```python
from dataclasses import FrozenInstanceError, fields
import pytest
from appio.query import QueryService, FileServeFacts
from tests.p4.files_tab_support import tab_store, BOOT, H, R, S

def test_facts_distinguish_app_hour_reducers_from_persisted_rows(tab_store):
    db = tab_store()
    service = QueryService(db)
    with service._snapshot():
        count = lambda w: db.execute("SELECT count(*) FROM gap WHERE boot_id=? AND from_mono_ns<? AND to_mono_ns>?", (BOOT, w.end_mono_ns, w.start_mono_ns)).fetchone()[0]
        assert count(S) == 1
        assert count(R) == 3
        facts_A = service.file_serve_facts(1, R)
        facts_B = service.file_serve_facts(2, R)
        assert facts_A == FileServeFacts(S, 1, 4)
        assert facts_B == FileServeFacts(S, 2, 5)
        assert facts_B.gaps_in_served_interval != count(S)
        for aid, facts, requested in ((1, facts_A, 4), (2, facts_B, 5)):
            table = service.file_table(aid, R, "bytes_total", "desc", None)
            cov = service.file_coverage(aid, R)
            assert table.app_id == cov.app_id == aid
            assert table.window == cov.window == R
            assert table.unknown_owner_overflow.window == cov.served_interval == facts.served_interval == S
            assert cov.coverage.gap_intervals == facts.gaps_in_requested_window == requested
            assert type(facts.gaps_in_served_interval) is int
            assert type(facts.gaps_in_requested_window) is int
    assert [f.name for f in fields(FileServeFacts)] == ["served_interval", "gaps_in_served_interval", "gaps_in_requested_window"]
    with pytest.raises(FrozenInstanceError):
        facts_A.gaps_in_served_interval = 99

def test_no_servable_interval_facts_are_unknown_not_zero(tab_store):
    db = tab_store(hours=(), app_gaps={}, gaps=())
    facts = QueryService(db).file_serve_facts(1, R)
    assert facts.served_interval is None
    assert facts.gaps_in_served_interval is None
    assert type(facts.gaps_in_requested_window) is int
```

Add `test_facts_validate_positive_local_app_id`: bool, zero, and negative IDs must not be accepted; an absent positive local ID follows the existing `_require_app` behavior. Add pruned `since_boot` and `lifetime` cases by creating complete hours `(0,1)`, `end=2*H`, deleting only hourly `file_sample`, `file_coverage`, `file_overflow_unknown`, committing, and retaining existing aggregates. Assert `(requested, None, existing_scalar)`; Task 13 independently pins their full semantics.

- [ ] **Step 2: Run the named tests and observe missing API failure.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_facts.py","tests/p4/test_files_tab_propagation.py"]
```

- [ ] **Step 3: Implement the local-only value and exact reducers.**

```python
@dataclass(frozen=True)
class FileServeFacts:
    served_interval: Window | None
    gaps_in_served_interval: int | None
    gaps_in_requested_window: int
```

The method's algorithm is deliberately not a persisted-gap count:

```python
def file_serve_facts(self, app_id: int, window: Window) -> FileServeFacts:
    self._precheck(window)
    with self._snapshot():
        coverage = self.file_coverage(app_id, window)
        served = coverage.served_interval
        _, scope, _, _, _ = self._file_query_context(window)
        measured = None
        if served is not None and scope == "hourly":
            boot, start, end = _bounds(served)
            rows = self._file_coverage_rows("hourly", boot, start, end, app_id)
            first = _ceil_bucket(start, FILE_HOUR_NS)
            last = (end // FILE_HOUR_NS) * FILE_HOUR_NS
            expected = (last - first) // FILE_HOUR_NS
            if expected > 0 and len({row[0] for row in rows}) == expected:
                measured = self._file_reduced(rows)["gap_intervals"]
        return FileServeFacts(served, measured, coverage.coverage.gap_intervals)
```

Reuse the existing strict coverage decoder/reducer; do not `json.get(...,0)` missing evidence. `_snapshot` nests under the caller's transaction and must not commit it. The method may query coverage internally in the same snapshot; no new result field/cache is needed. It never calls the provenance planner or transports the facts.

- [ ] **Step 4: Run the named falsifiers and restore.**

`M_served_uses_persisted_rows` must fail B's served `2`; `M_B_reuses_A_facts` must fail B's complete `(S,2,5)` assertion. Also run `M_requested_scalar_is_persisted_3` (fail A=`4`/B=`5`), `M_facts_use_R` (fail scope equality), and `M_missing_hourly_becomes_zero` (fail no-served/pruned `is None`). Preserve actual failing assertion lines in evidence, not just nonzero exits. Rerun validation after restoration.

- [ ] **Step 5: Commit**

```text
feat(query): report app-specific Files serve facts in one snapshot
```

## Task 4: Pin literal nested transport field inventories

**Implementer tier:** Standard

**Contract:** FileCoverage wire shape, §§2.3–2.5 and 8.

**Depends on:** 2. This tests-only task can run alongside 3.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_wire_shape.py`.
- No codec, contracts, protocol, cursor, or durable source modification is authorized.

**Interfaces:**
- Consumes: `to_wire(value, *, app_keys:Mapping[int,str]) -> object` and `from_wire(value, *, app_ids:Callable[[str],int], result_kind:str) -> object` from `appio.iod_history`; `file_coverage()` fixture constructor from `tests.p4.test_files_view`; `FileCoverage.served_interval` from 2.
- Produces: exact literal inventory tests including nested added/missing-key rejection; no new wire interface.

- [ ] **Step 1: Write literal expected sets, not sets derived from dataclass reflection.**

Copy `WINDOW_FIELDS`, `HOOK_HITS_FIELDS`, `COVERAGE_FIELDS`, `UNKNOWN_OWNER_FIELDS`, and `FILE_COVERAGE_FIELDS` **literally from specification §2.5** into this test. They are the actual required test data, not an invitation to derive expectations from `_RECORDS`, `fields()`, or encoder output. Include `record`, omit `launch_id` from Window and local `app_id`/`path_id` surrogates. Test code:

```python
from copy import deepcopy
import pytest
from appio.iod_history import to_wire, from_wire
from tests.p4.test_files_view import file_coverage

PATHS = (
    ((), "FILE_COVERAGE_FIELDS"),
    (("window",), "WINDOW_FIELDS"),
    (("coverage",), "COVERAGE_FIELDS"),
    (("hook_hits",), "HOOK_HITS_FIELDS"),
    (("coverage", "hook_hits"), "HOOK_HITS_FIELDS"),
    (("unknown_owner_overflow",), "UNKNOWN_OWNER_FIELDS"),
    (("unknown_owner_overflow", "window"), "WINDOW_FIELDS"),
    (("unknown_owner_overflow", "coverage"), "COVERAGE_FIELDS"),
    (("unknown_owner_overflow", "coverage", "hook_hits"), "HOOK_HITS_FIELDS"),
)

def at(value, path):
    for key in path:
        value = value[key]
    return value

def test_literal_file_wire_sets_and_round_trip():
    cov = file_coverage()
    wire = to_wire(cov, app_keys={1: "unit:app-demo.service"})
    for path, name in PATHS:
        assert set(at(wire, path)) == globals()[name]
    assert from_wire(wire, app_ids=lambda key: 1, result_kind="coverage") == cov
    assert not any(key in repr(wire) for key in ("served_interval", "served_gap_count", "gaps_in_served_interval"))

@pytest.mark.parametrize("path,name", PATHS)
@pytest.mark.parametrize("operation", ["add", "remove"])
def test_each_nested_record_rejects_extra_and_missing_fields(path, name, operation):
    wire = deepcopy(to_wire(file_coverage(), app_keys={1: "unit:app-demo.service"}))
    node = at(wire, path)
    if operation == "add":
        node["served_gap_count"] = 0
    else:
        node.pop(sorted(globals()[name] - {"record"})[0])
    with pytest.raises(ValueError):
        from_wire(wire, app_ids=lambda key: 1, result_kind="coverage")
```

Also pin `dataclasses.asdict(cov)` to the existing local field names (`FILE_COVERAGE_FIELDS - {"record","app_key"} | {"app_id"}`) and equality before/after property access, so durable field enumeration cannot include the computed property. The plan chooses tests-only because the exact codec already implements this behavior.

- [ ] **Step 2: Run the positive tests; do not claim they were expected to fail.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_wire_shape.py"]
```

- [ ] **Step 3: Falsify every record-level mechanism without changing production.**

`M_wire_encodes_property`: inject `served_interval` into encoded FileCoverage; the literal inventory must fail. For each PATHS record class run `M_decode_extra_<class>` and `M_decode_missing_<class>` by relaxing only that class's exact-field check in a disposable process/copy; the corresponding `pytest.raises` must fail. Keep the encoder inventory unchanged during decoder probes. An unchanged round trip alone is not proof.

- [ ] **Step 4: Restore byte-for-byte and rerun validation.**

Expected: all literal inventory, nested mutation-input rejection, durable-field, and equality assertions pass. If the existing codec genuinely violates the fixed shape, stop for a reviewed allowlist decision rather than silently modifying a forbidden source file.

- [ ] **Step 5: Commit**

```text
test(files): pin exact nested coverage transport fields
```

## Task 5: Restore requested-window process gap isolation

**Implementer tier:** Advanced

**Contract:** Requested `_gap_ranges` and process isolation, §§5.6 and 6.3.

**Depends on:** 3; the audit disposition from 1 must permit this repair/replacement work.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/history_router.py:109-120,505-524,666-684` (`_plan` requested scoping and reset paths only).
- Create: `tests/p4/test_files_tab_process_scope.py`.
- Modify: `docs/superpowers/reviews/2026-10-30-appio-files-tab-candidate-audit.md` (this finding's re-audit evidence).

**Interfaces:**
- Consumes: `_plan(window:Window) -> None`, `_combine_coverage(rows:list[Coverage]) -> Coverage`; local `file_serve_facts(app_id:int,window:Window) -> FileServeFacts` from 3; `tab_store` fixture; `coverage(**overrides) -> Coverage` from `tests.p4.test_files_view`.
- Produces: `_gap_ranges` requested-process scoped, with file facts unaffected; no changed public signature. Any future `_known_gap_ranges` remains independent.

- [ ] **Step 1: Add the exact outside-S/inside-R and outside-R controls.**

```python
from types import SimpleNamespace
from appio.contracts import Window
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry
from appio.query import QueryService
from tests.p4.test_files_view import coverage
from tests.p4.files_tab_support import tab_store, BOOT, H, S

def test_requested_process_gaps_are_not_file_admission_ranges(tab_store):
    R2 = Window("range", BOOT, 2*H, 6*H)
    db = tab_store(hours=(2,3), app_gaps={},
        gaps=((4*H,5*H,"collector_gap"),(0,H,"collector_gap")))
    router = HistoryRouter(QueryService(db), SimpleNamespace(state=None), AppRegistry())
    try:
        router._window = R2
        router._plan(R2)
        assert router._gap_ranges == [(BOOT,4*H,5*H)]
        process = router._combine_coverage([coverage()])
        assert process.gap_intervals == 1
        assert not process.complete
        facts = QueryService(db).file_serve_facts(1, R2)
        assert facts.served_interval == S
        assert facts.gaps_in_served_interval == 0
    finally:
        router.close()

def test_outside_requested_gap_keeps_process_complete(tab_store):
    R2 = Window("range", BOOT, 2*H, 6*H)
    db = tab_store(hours=(2,3), app_gaps={}, gaps=((0,H,"collector_gap"),))
    router = HistoryRouter(QueryService(db), SimpleNamespace(state=None), AppRegistry())
    try:
        router._plan(R2)
        assert router._gap_ranges == []
        process = router._combine_coverage([coverage()])
        assert process.gap_intervals == 0
        assert process.complete
    finally:
        router.close()
```

- [ ] **Step 2: Run baseline and pin the candidate deletion with a mutant.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_process_scope.py"]
```

The base already scopes these ranges. If it is green, demonstrate `M_requested_gap_scope_deleted` by removing only the scoped assignment in an isolated mutant; the outside-R complete assertion must fail. `M_process_uses_S` must lose `[4h,5h)` and fail the first test. `M_served_counts_requested_gap` must fail the served-count `0` assertion.

- [ ] **Step 3: Preserve or restore exactly the requested assignment.**

```python
self._gap_ranges = [(s.boot_id, s.start_ns, s.end_ns)
                    for s in scoped if not s.proven]
```

If the candidate mechanism will be reused, keep this line even when adding its separate known-gap list later. Do not change process coverage reducers, materialization, or collection cadence. Record that a base-preserving pin is not a fabricated repair of already-correct HEAD.

- [ ] **Step 4: Re-audit the changed/preserved bytes and rerun validation.**

Append the exact assignment, process consumer locations, both controls' evidence, and mutation failure lines to the audit report. This closes the `_gap_ranges` finding only, not adoption as a whole.

- [ ] **Step 5: Commit**

```text
fix(router): preserve requested process gap scope during Files admission
```

## Task 6: Clip unproved segments before known-gap admission

**Implementer tier:** Capable

**Contract:** Unproved segment clipping, §§5.2–5.5, with audit-directed adoption/repair/replacement of this mechanism only.

**Depends on:** 5. No candidate mechanism is consumed before the audit gate and process-scope pin.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/history_router.py:26-38,109-120,190-238,505-524,666-684` (file provenance, separate known gaps, initialization/reset).
- Create: `tests/p4/test_files_tab_clipping.py`.
- Modify: `docs/superpowers/reviews/2026-10-30-appio-files-tab-candidate-audit.md` (clipping/admission re-audit).

**Interfaces:**
- Consumes: `FileCoverage.served_interval:Window|None`; `_read_local_files(paths,window,key,app_id,sort,direction,search) -> FileViewResult`; `_plan(window)` and `HistorySegment(collector_id,owner,boot_id,start_ns,end_ns,proven)`; `tab_store`, `read_files(stores,window=R,key=A,app_id=101)`.
- Produces: private `HistoryRouter._check_file_provenance(worker, served:Window, supplying_name:str) -> bool`: returns `True` for accepted provenance, `False` only for a post-validation discarded mirror, otherwise raises the exact ownership `WindowUnavailableError`. `_known_gap_ranges:list[tuple[str,int,int]]` excludes `overlap_uncertain` and is distinct from requested `_gap_ranges`.

- [ ] **Step 1: Build committed boundary-straddling, full-union, hole, and uncertain cases.**

```python
import pytest
from appio.contracts import WindowUnavailableError
from tests.p4.files_tab_support import tab_store, read_files, BOOT, H, R, S

@pytest.mark.parametrize("resume,gaps,accepted", [
    (3*H, ((2*H,3*H,"collector_gap"),), True),
    (3*H, ((2*H,5*H//2,"collector_gap"),(5*H//2,3*H,"collector_gap")), True),
    (7*H//2, ((2*H,7*H//2,"collector_gap"),), False),
    (3*H, ((2*H,12*H//5,"collector_gap"),(13*H//5,3*H,"collector_gap")), False),
    (3*H, ((2*H,3*H,"overlap_uncertain"),), False),
])
def test_clipped_unproved_segment_requires_aligned_complete_known_gap_union(tab_store, resume, gaps, accepted):
    db = tab_store(hours=(2,3), app_gaps={}, gaps=gaps)
    db.execute("DELETE FROM collector_interval")
    db.execute("INSERT INTO collector_interval VALUES('gui',?,1,0,?)", (BOOT,3*H//2))
    db.execute("INSERT INTO collector_interval VALUES('gui',?,2,?,?)", (BOOT,resume,6*H))
    db.commit()
    if accepted:
        result = read_files((("local",db,"gui"),))
        assert result.coverage.served_interval == S
        assert result.coverage.observed_bytes_r == 70
    else:
        with pytest.raises(WindowUnavailableError) as caught:
            read_files((("local",db,"gui"),))
        assert caught.value.reason == "file_source_unavailable"
        assert str(caught.value) == "File ownership cannot be represented by one store."
```

Add `test_unproved_segment_wholly_outside_served_interval_is_irrelevant`: use known/unproved evidence outside S, retain a sole proven owner throughout S, and assert the same 70-byte result. Observe raw `_all_segments` includes `[1h30m,3h)` in the first case while candidate segments clip to `[2h,3h)`, so the acceptance does not accidentally test an already-aligned raw input.

- [ ] **Step 2: Run the new pins before admission changes.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_clipping.py","tests/p4/test_files_tab_process_scope.py"]
```

Expected behavioral red on aligned known-gap acceptance if the strict existing refuser is still in place.

- [ ] **Step 3: Implement only the approved admission predicate.**

Collect raw persisted non-overlap gaps during `_plan`; clip them per boot/request, initialize and reset the independent list wherever planner state resets, and keep Task 5's assignment. Evaluate the existing ownership plan over **each candidate's own** served scope. Clip nonempty segments and known gaps to that scope before this union test:

```python
def fully_covered(segment, known):
    if segment.start_ns % FILE_HOUR_NS or segment.end_ns % FILE_HOUR_NS:
        return False
    cursor = segment.start_ns
    for boot, left, right in sorted(known):
        if boot != segment.boot_id or right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= segment.end_ns:
            return True
    return False
```

`_check_file_provenance` uses the clipped segments, not `_all_segments` without clipping. Reject no segments, chronology unproved, intersecting overlap, zero/multiple proven collector IDs, or any unproved segment not fully covered by that union. Error pair is exactly `file_source_unavailable` / `File ownership cannot be represented by one store.` The function returns `names == {supplying_name}` only **after** all those predicates pass, so mirror mismatch cannot hide failure. Existing handover/epoch planning stays authoritative; do not add a winner heuristic or a global intersection of store calendars.

Call it outside the existing typed table/result-validation catch. Keep the candidate scan running until all candidates have been examined. This task does not relax error classification; Tasks 8–9 pin that preservation. Use `result.coverage.served_interval`; if `None`, reject under the narrow pre-provenance query-validation skip, never plan the placeholder request as if served.

- [ ] **Step 4: Falsify, restore, and re-audit.**

`M_align_raw_before_clip` must fail the accepted straddling case; `M_any_gap_hit_is_enough` must fail the hole case; `M_overlap_gap_is_known` must fail the uncertain-row case; `M_widen_S_to_raw_segment` must fail served scope/row checks; `M_no_alignment_check` must fail `[2h,3h30m)` refusal. Re-audit only the actual adopted/rewritten mechanism and record whether repair or replacement was used. Do not claim full candidate adoption until 7–9 have passed their mechanism gates.

- [ ] **Step 5: Commit**

```text
fix(router): admit only clipped hour-aligned known-gap segments
```

## Task 7: Refuse intersecting overlap even when counters are zero

**Implementer tier:** Advanced

**Contract:** Overlap uncertainty, §5.4 and §8.

**Depends on:** 6.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_overlap.py`.
- Modify: `src/appio/history_router.py:190-238,385-524` (only overlap intersection/admission if the pin exposes a defect).
- Modify: `docs/superpowers/reviews/2026-10-30-appio-files-tab-candidate-audit.md` (overlap evidence).

**Interfaces:**
- Consumes: `_check_file_provenance(worker,served:Window,supplying_name:str) -> bool` from 6; `tab_store`, `read_files`, `H,BOOT,R,S` from support.
- Produces: independent overlap-refusal and outside-S controls, with existing public signatures unchanged.

- [ ] **Step 1: Write zero-counter and outside-scope controls.**

```python
import pytest
from appio.contracts import WindowUnavailableError
from tests.p4.files_tab_support import tab_store, read_files, H, S

@pytest.mark.parametrize("left,right,refuses", [(2*H,3*H,True),(4*H,5*H,False)])
def test_overlap_uncertainty_is_intersection_scoped_even_with_zero_bytes(tab_store, left, right, refuses):
    db = tab_store(hours=(2,3), app_gaps={}, gaps=((left,right,"overlap_uncertain"),))
    db.execute("UPDATE file_sample SET bytes_r=0,bytes_w=0")
    db.commit()
    if refuses:
        with pytest.raises(WindowUnavailableError) as caught:
            read_files((("local",db,"gui"),))
        assert (caught.value.reason, str(caught.value)) == ("file_source_unavailable", "File ownership cannot be represented by one store.")
    else:
        result = read_files((("local",db,"gui"),))
        assert result.coverage.served_interval == S
        assert result.coverage.observed_bytes_r == 0
```

Add `test_overlapping_owner_epochs_refuse_without_persisted_gap_row`: committed `collector_interval` evidence for two distinct owners/epochs intersects S, no `overlap_uncertain` row, zero counters; assert the same exact pair. This separately exercises `_overlap_ranges`, not just persisted reason filtering.

- [ ] **Step 2: Run the tests; if green, proceed directly to mechanism falsification.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_overlap.py","tests/p4/test_files_tab_clipping.py"]
```

- [ ] **Step 3: Preserve intersection checks or repair only the defective predicate.**

For each candidate S, require no nonempty intersection from either the planner's overlap ranges or persisted `overlap_uncertain` gaps. Do not condition refusal on observed byte counts. Do not use a requested-window boolean instead of clipping. The new test's hourly table rows may remain present with zero bytes; that is not proof of ownership.

- [ ] **Step 4: Kill the two distinct mutants and re-audit.**

`M_overlap_ignored_when_zero` must fail the inside-S refusal assertion. `M_overlap_uses_R` must fail outside-S acceptance. `M_ignore_planned_epoch_overlap` must fail the two-epoch case independently of persisted-gap filtering. Restore and rerun validation; append exact assertion evidence to the audit report.

- [ ] **Step 5: Commit**

```text
test(router): pin served-scope overlap refusal independent of bytes
```

## Task 8: Select candidates independently per store and reject multiple survivors

**Implementer tier:** Advanced

**Contract:** Per-store candidate selection, §§5.1–5.2 and §8.

**Depends on:** 6. May parallelize its new tests with 7, but serialize any shared router repair.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_candidates.py`.
- Modify: `src/appio/history_router.py:190-238` (candidate selection/count only).
- Modify: `docs/superpowers/reviews/2026-10-30-appio-files-tab-candidate-audit.md` (per-store evidence).

**Interfaces:**
- Consumes: `tab_store`, `read_files(stores,window=R,key=A,app_id=101)`; `FileCoverage.served_interval`; `_check_file_provenance(worker,served,supplying_name)->bool` from 6.
- Produces: exactly one survivor after a complete scan, never one manufactured global served interval.

- [ ] **Step 1: Write the disjoint-survivors and outside-owner controls.**

```python
import pytest
from appio.contracts import WindowUnavailableError, Window
from appio.query import QueryService
from tests.p4.files_tab_support import tab_store, read_files, BOOT, H, R, S

def test_two_valid_stores_with_different_served_intervals_are_ambiguous(tab_store):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), start=2*H, end=4*H)
    b = tab_store("b", hours=(4,5), app_gaps={}, gaps=(), owner="iod", start=4*H)
    assert QueryService(a).file_coverage(1,R).served_interval == S
    assert QueryService(b).file_coverage(1,R).served_interval == Window("range",BOOT,4*H,6*H)
    for stores in ((("A",a,"gui"),("B",b,"iod")), (("B",b,"iod"),("A",a,"gui"))):
        with pytest.raises(WindowUnavailableError) as caught:
            read_files(stores)
        assert (caught.value.reason,str(caught.value)) == ("file_source_unavailable","File ownership cannot be represented by one store.")

def test_outside_candidate_owner_and_gap_do_not_fail_the_only_survivor(tab_store):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), start=2*H, end=4*H)
    b = tab_store("b", hours=(), app_gaps={}, gaps=((5*H,6*H,"collector_gap"),), owner="iod", start=5*H)
    result = read_files((("A",a,"gui"),("B",b,"iod")))
    assert result.coverage.served_interval == S
    assert result.coverage.observed_bytes_r == 70
```

Add `test_largest_calendar_run_and_latest_end_tie_break_are_not_provenance_choices`: use hours `(0,1,3,4)` with equal-length runs and committed evidence; assert the selected interval is `[3h,5h)` before provenance. If that largest run refuses provenance, assert refusal instead of silently serving `[0h,2h)`. Selection tests must compare each store's own finalization watermark and requested bounds.

- [ ] **Step 2: Execute the contract pins.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_candidates.py"]
```

- [ ] **Step 3: Preserve per-store selection and the post-scan count check.**

Keep `file_table`/`file_coverage` inside the per-store loop; use each accepted result's own served property for `_plan`. Append only fully validated candidates. After scanning, zero uses the remembered error, one returns, two refuse. Never intersect/union candidates' S intervals, choose by byte totals/freshness, or retry shorter runs after a fatal provenance finding.

- [ ] **Step 4: Falsify and re-audit.**

`M_global_candidate_interval` must fail independent S assertions or ambiguity; `M_accept_first_store` must fail the two-survivor assertion in both orders; `M_outside_S_owner_fails` must fail the positive control; `M_shorter_run_avoids_refusal` must fail the largest-run refusal pin. Restore, rerun validation, and append the evidence.

- [ ] **Step 5: Commit**

```text
test(router): pin per-store Files interval selection and ambiguity
```

## Task 9: Pin fatal versus skippable candidate handling by stage

**Implementer tier:** Capable

**Contract:** Fatal versus skippable candidate failures, §5.7 and §8.

**Depends on:** 8. Integrate 7 before the overall adoption conclusion at 10.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_candidate_failures.py`.
- Modify: `src/appio/history_router.py:190-238` (catch boundaries and final acceptance only).
- Modify: `docs/superpowers/reviews/2026-10-30-appio-files-tab-candidate-audit.md` (classification/adoption re-audit).

**Interfaces:**
- Consumes: `_read_local_files(paths,window,key,app_id,sort,direction,search)->FileViewResult`; `_check_file_provenance(worker,served,supplying_name)->bool`; `tab_store`, `read_files`, `H,BOOT,R,S`.
- Produces: preserved narrow typed query/result-validation skip, fatal provenance/unexpected exceptions, post-provenance mirror skip, last-query-error fallback.

- [ ] **Step 1: Write both orders of the exact mixed valid/invalid fixture.**

```python
import pytest
from appio.contracts import WindowUnavailableError, Window
from appio.query import QueryService
from tests.p4.files_tab_support import tab_store, read_files, BOOT, H, R, S

@pytest.mark.parametrize("reverse", [False,True])
def test_provenance_failure_is_fatal_even_when_another_candidate_is_valid(tab_store, reverse):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), start=3*H, end=4*H)
    b = tab_store("b", hours=(5,), app_gaps={}, gaps=(), owner="iod", start=5*H)
    for db, served in ((a,S),(b,Window("range",BOOT,5*H,6*H))):
        table = QueryService(db).file_table(1,R,"bytes_total","desc",None)
        cov = QueryService(db).file_coverage(1,R)
        assert cov.served_interval == served
        assert table.coverage.unavailable_reason is None
        assert cov.coverage.unavailable_reason is None
    assert read_files((("B",b,"iod"),)).coverage.observed_bytes_r == 60
    stores = (("A",a,"gui"),("B",b,"iod"))
    with pytest.raises(WindowUnavailableError) as caught:
        read_files(tuple(reversed(stores)) if reverse else stores)
    assert (caught.value.reason,str(caught.value)) == ("file_source_unavailable","File ownership cannot be represented by one store.")
```

Add separate named tests with these exact fixture operations and assertions:

- `test_typed_preprovenance_query_unavailability_skips_to_valid_B`: A retains its app row but has `hours=()`; B is the committed `[5h,6h)` positive control; assert B's served bounds and 60 bytes. Parameterize the typed pre-provenance failure reason as `not_retained` and `file_source_unavailable` with an injected `WindowUnavailableError` in A's table/result-validation stage; both skip. A provenance failure with that same reason must still refuse.
- `test_missing_app_receipt_skips_without_replacing_last_query_error`: A has `apps=()`, B succeeds; then no valid B and one earlier typed query failure followed by missing-receipt A must re-raise the remembered exact reason/message, not the default.
- `test_valid_mirror_is_skipped_only_after_provenance`: same-owner/epoch copies with equal bounds, use name `local` for the authoritative source so existing mirror preference is deterministic; poison only the mirror's file bytes to 999 and assert accepted authoritative bytes remain 70, never 1069 or 999.
- `test_mirror_mismatch_cannot_hide_earlier_provenance_failure`: A's calendar remains `[2h,4h)`; give A's only proven `[3h,4h)` ownership to B by keeping B's source interval there plus `[5h,6h)`, while A's `[2h,3h)` remains uncovered; B's own calendar is `[5h,6h)`. Assert exact ownership refusal in both source orders, not a mirror skip. Use two committed B `collector_interval` epochs for the disjoint intervals; no B evidence intersects A's uncovered `[2h,3h)`.
- `test_unexpected_read_or_planning_error_is_fatal_not_a_candidate_skip`: inject a `sqlite3.OperationalError` after A's receipt lookup/query or a `ValueError` in planning; raw worker aborts; the public future retains existing explicit normalization rather than returning B or an empty result.

- [ ] **Step 2: Run every classification control.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_candidate_failures.py","tests/p4/test_files_tab_candidates.py"]
```

- [ ] **Step 3: Keep the stage boundary executable and narrow.**

The loop shape is fixed; fill it with the existing queries/provenance helper, not a reason-string policy:

```python
# Missing receipt: continue; do not overwrite unavailable.
try:
    # One snapshot: table, coverage, and later Task 10 facts; validate result.
    result = query_and_validate_this_candidate()
except WindowUnavailableError as error:
    unavailable = error
    continue
# No catch surrounding this call: provenance failures are fatal.
if not HistoryRouter._check_file_provenance(worker, result.coverage.served_interval, name):
    continue  # Only a successfully validated mirror gets here.
results.append(result)
# After the entire loop: 0 -> remembered error; 1 -> return; >1 -> ownership refusal.
```

`query_and_validate_this_candidate()` above denotes the **inline existing try-block**, not a new public or undefined runtime function: retain the actual `file_table`, `file_coverage`, `_check_file_result` statements at that location. Do not introduce that pseudocode identifier into source. Preserve the outer resource-closing `finally` and current public error normalization. Do not catch unexpected exceptions here.

- [ ] **Step 4: Falsify every ordering rule, restore, and close the adoption re-audit.**

`M_provenance_failure_becomes_skip` must fail the mixed fixture in both orders; `M_return_first_valid` must fail B-first; `M_every_unavailability_is_fatal` must fail query-unavailability fallback; `M_mirror_skip_before_provenance` must fail the mirror-order refusal; `M_missing_receipt_replaces_error` must fail remembered-error identity/message; `M_catch_all_reads_and_continue` must fail unexpected-error abortion. Record exact failing assertions. Append a final scoped re-audit conclusion for Tasks 5–9, respecting the Task 1 pass/repair/replace decision. State separately that facts/rendering/real acceptance are still pending and green tests alone are not adoption evidence.

- [ ] **Step 5: Commit**

```text
fix(router): preserve stage-based Files candidate refusals
```

## Task 10: Bind local result identity, snapshot, and served-hour count

**Implementer tier:** Advanced

**Contract:** Local result shape and identity, §§2.2, 2.4, 4.1 and §8.

**Depends on:** 3, 7, 9 (including re-audit disposition).

**Files (exclusive modification allowlist):**
- Modify: `src/appio/history_router.py:95-100,172-238,283-310` (`FileViewResult`, validation, local worker, explicit remote construction compatibility).
- Modify: `tests/p4/test_files_view.py:173-219` (fixture's third-field argument only).
- Modify: `tests/p3/test_collector_window.py:238-251` (fixture's third-field argument only).
- Create: `tests/p4/test_files_tab_local_result.py`.

**Interfaces:**
- Consumes: `FileServeFacts(served_interval:Window|None,gaps_in_served_interval:int|None,gaps_in_requested_window:int)` and `file_serve_facts(app_id:int,window:Window)->FileServeFacts` from 3; `FileCoverage.served_interval`; `read_files` support driver.
- Produces: `@dataclass(frozen=True) class FileViewResult: table:FileTable; coverage:FileCoverage; served_gap_count:int|None` (exactly one added field, no default). `_check_file_result(table,coverage,served_gap_count: int|None = None)->FileViewResult` may default its **argument** for existing remote callers, but the dataclass field remains explicit. Test `file_view(..., served_gap_count:int|None=None, **overrides)->FileViewResult` consumes the keyword separately from FileCoverage overrides.

- [ ] **Step 1: Add both-app local mapping and snapshot pins.**

```python
from dataclasses import fields
from appio.history_router import FileViewResult
from appio.query import QueryService
from tests.p4.files_tab_support import tab_store, read_files, A, B, R, S

def test_local_result_uses_explicit_store_app_and_one_snapshot(tab_store, monkeypatch):
    db = tab_store()
    calls = []
    original = QueryService.file_serve_facts
    def facts(service, aid, window):
        assert service.connection.in_transaction
        calls.append((aid, window, id(service.connection)))
        return original(service, aid, window)
    monkeypatch.setattr(QueryService, "file_serve_facts", facts)
    for key, local_id, presentation, served_count, requested in ((A,1,101,1,4),(B,2,202,2,5)):
        result = read_files((("local",db,"gui"),), key=key, app_id=presentation)
        assert result.table.app_id == result.coverage.app_id == presentation
        assert result.table.window == result.coverage.window == R
        assert result.coverage.served_interval == S
        assert result.served_gap_count == served_count
        assert result.coverage.coverage.gap_intervals == requested
        assert calls[-1][0:2] == (local_id, R)
    assert [f.name for f in fields(FileViewResult)] == ["table","coverage","served_gap_count"]
```

Add `test_local_table_coverage_and_facts_share_outer_transaction`: instrument all three method entry points, assert each enters with `connection.in_transaction`, the same connection identity and selected local app ID, and no intervening transaction end. Commit a source-database coverage change between table and facts through the fixture's original connection; the worker's pinned copy must retain its earlier row/facts combination. Separately mutate returned facts' requested scalar, served scope, or local app binding and assert candidate validation rejects rather than mixing them. Keep the existing `test_local_query_runs_on_bounded_executor_and_invalidated_future_cannot_publish` as a UI-thread isolation control.

- [ ] **Step 2: Run tests before changing the result shape.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_local_result.py","tests/p4/test_file_history_router.py::test_local_query_runs_on_bounded_executor_and_invalidated_future_cannot_publish"]
```

- [ ] **Step 3: Add the field and compute facts within the existing outer snapshot.**

```python
with service._snapshot():
    row = service.connection.execute("SELECT app_id FROM app WHERE key=?", (key,)).fetchone()
    if row is None:
        continue
    table = service.file_table(row[0], window, sort, direction, search)
    coverage = service.file_coverage(row[0], window)
    facts = service.file_serve_facts(row[0], window)
    if (table.app_id != row[0] or coverage.app_id != row[0] or
            table.window != window or coverage.window != window or
            facts.served_interval != coverage.served_interval or
            facts.gaps_in_requested_window != coverage.coverage.gap_intervals):
        raise WindowUnavailableError("file_source_unavailable", "File coverage does not match the table snapshot.")
    result = HistoryRouter._check_file_result(
        replace(table, app_id=app_id), replace(coverage, app_id=app_id),
        facts.gaps_in_served_interval)
```

Keep this inside the narrow typed query/result-validation stage, and keep provenance outside the catch. `_check_file_result` additionally rejects a missing served property and mismatched table/coverage application, requested window, tier, coverage, and compatibility carrier. It must not compute a gap count. The separate facts call is once per candidate in this outer transaction; no second database connection/state is queried. Update both test constructors explicitly (`None` for unprovable fake routes, never a blanket zero), plus remote construction to pass `None`; Task 11 supplies its independent route pin. Do not change the wire codec to accommodate this local field.

- [ ] **Step 4: Falsify and restore.**

`M_omit_local_app_argument`/`M_use_registry_id_as_store_id` must fail explicit local IDs/B assertions; `M_local_served_is_requested_scalar` must fail `1`/`2`; `M_facts_outside_outer_snapshot` must fail transaction entry; `M_second_snapshot_after_source_commit` must fail original snapshot coherence. Scope mismatch mutants must fail validation, not silently get remapped. Rerun validation and the literal wire pin from Task 4 if integrated.

- [ ] **Step 5: Commit**

```text
feat(router): carry snapshot-bound local Files serve facts
```

## Task 11: Keep remote served-hour evidence explicitly unavailable

**Implementer tier:** Advanced

**Contract:** Remote result shape, §§2.4, 4.2 and §8.

**Depends on:** 10.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/history_router.py:279-347` (remote construction/checks only, if needed).
- Modify: `tests/p4/test_file_history_router.py:220-349` (existing real protocol harness assertions and fault variants).
- Create: `tests/p4/test_files_tab_remote_result.py`.

**Interfaces:**
- Consumes: `FileViewResult(table:FileTable,coverage:FileCoverage,served_gap_count:int|None)`; `_request_remote_files(out,generation,app_id,key,window,sort,direction,search)`; existing test `Remote(tmp_path,retained)`, `.finish(future)->FileViewResult`, `.respond(mutate=None)`, `.requests`, `.close()`; `retained` fixture and `id_for(router)` from `tests.p4.test_file_history_router`.
- Produces: remote results always explicitly carry `None`, retaining existing HMAC/snapshot/scope metadata and requested scalar. No transport changes.

- [ ] **Step 1: Extend the real IodClient/HistoryRpc harness pin.**

```python
from tests.p4.test_file_history_router import retained, Remote, id_for, WINDOW

def test_remote_real_codec_pages_never_invent_served_gap_evidence(retained, tmp_path):
    remote = Remote(tmp_path, retained)
    try:
        future = remote.router.request_file_view(id_for(remote.router), WINDOW,
                                                "bytes_total", "desc", "CAFÉ")
        result = remote.finish(future)
        assert result.served_gap_count is None
        assert result.coverage.served_interval == result.table.unknown_owner_overflow.window
        assert result.table.window == result.coverage.window == WINDOW
        assert type(result.coverage.coverage.gap_intervals) is int
        pages = [r for r in remote.requests if r.kind == "files"]
        assert len(pages) == 2
        assert pages[0].cursor is None and pages[1].cursor
        assert all((r.sort,r.direction,r.search)==("bytes_total","desc","CAFÉ") for r in pages)
    finally:
        remote.close()
```

Extend the existing paginated test with nonzero requested `gap_intervals` (commit synthetic gap evidence before creating `HistoryRpc`) and assert it remains nonzero while the served count stays `None`. Existing fault tests already exercise forged HMAC, changed snapshot, app, search, mixed tier, generation/cursor. Add `page_served_scope` and `page_requested_window` faults on a later page and assert typed refusal. Do not emulate remote pagination with an already-decoded fake future alone.

- [ ] **Step 2: Run both route-shape and existing protocol regression pins.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_remote_result.py","tests/p4/test_file_history_router.py"]
```

- [ ] **Step 3: Preserve or repair the local-only construction.**

Remote covered callback constructs/checks `FileViewResult(table,coverage,None)` via `_check_file_result`; no truthiness fallback, requested-scalar copy, or remote fact-query RPC is allowed. Preserve first-page metadata, exact requested window, sort/direction/search, snapshot/app checks, connection generation/cursor, opaque continuations, bounded row/page budget, and coverage-stage match.

- [ ] **Step 4: Run route and guard falsifiers.**

`M_remote_served_is_requested_scalar` and `M_remote_served_is_zero` must fail `is None`, even on complete-looking responses. `M_skip_hmac_page` must fail row/page assertions; `M_accept_later_page_scope_change` must fail the new scope fault; `M_ignore_remote_snapshot` must fail the existing changed-snapshot control. Restore byte-for-byte and rerun validation.

- [ ] **Step 5: Commit**

```text
test(router): pin unknown remote Files served-hour evidence
```

## Task 12: Prove complete retained scope yields measured zero

**Implementer tier:** Standard

**Contract:** Complete result, §8 (distinct from complete-empty rendering).

**Depends on:** 10.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_complete.py`.
- No production edits: complete hourly behavior should already be provided by 2, 3, and 10. A genuine defect returns to the owning task rather than silently expanding this allowlist.

**Interfaces:**
- Consumes: `tab_store`, `read_files`, `H,BOOT`; `file_serve_facts(app_id:int,window:Window)->FileServeFacts`; `FileViewResult(...,served_gap_count:int|None)`.
- Produces: an independent measured-zero/complete pin for retained hourly evidence, including a full-scope incomplete control.

- [ ] **Step 1: Write exact full-scope assertions for both supported hourly request kinds.**

```python
import pytest
from appio.contracts import Window
from appio.query import QueryService, FileServeFacts
from tests.p4.files_tab_support import tab_store, read_files, H, BOOT

@pytest.mark.parametrize("window", [Window("range",BOOT,0,2*H), Window("since_boot",BOOT,end_mono_ns=2*H)])
def test_complete_retained_hours_have_measured_zero_gap_and_equal_scope(tab_store, window):
    db = tab_store(hours=(0,1), app_gaps={}, gaps=(), end=2*H)
    service = QueryService(db)
    for aid in (1,2):
        assert service.file_serve_facts(aid,window) == FileServeFacts(window,0,0)
    result = read_files((("local",db,"gui"),), window=window)
    assert result.coverage.served_interval == window
    assert result.served_gap_count == 0
    assert type(result.served_gap_count) is int
    assert result.coverage.coverage.complete is True
    assert result.coverage.unknown_owner_overflow.overflow_files == 0
```

Add `test_full_scope_incomplete_is_not_automatically_shortened`: same complete calendar, set app coverage incomplete via a non-gap uncertainty already supported by the producer (for example an uncounted overflow counter); assert effective scope still equals the request while coverage is false. Do not use absence of unknown-owner overflow rows as absence of the unknown-owner **calendar**, which is required evidence.

- [ ] **Step 2: Run the already-supported behavior.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_complete.py"]
```

- [ ] **Step 3: Falsify zero-versus-unknown and completeness independently.**

`M_measured_zero_becomes_None` must fail exact facts and local result count; `M_no_unknown_overflow_means_partial` must fail `complete is True`; `M_incomplete_always_shortened` must fail full-scope incomplete equality. No production refactor is justified by an already-green baseline.

- [ ] **Step 4: Restore and rerun validation.**

Expected: retained evidence proves integer zero; absence of overflow activity does not erase complete calendar evidence.

- [ ] **Step 5: Commit**

```text
test(files): distinguish measured zero from missing hourly evidence
```

## Task 13: Preserve totals-only scope without inventing hourly evidence

**Implementer tier:** Advanced

**Contract:** Totals-only/pruned semantics, §§2.2–2.3 and §8.

**Depends on:** 3. This query-only task is independent of router changes after 5.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_totals.py`.
- Modify: `src/appio/query.py:699-810,1029-1073` (only facts/pruned effective-scope handling if necessary).
- No schema, aggregate-writing, property-field, or protocol change.

**Interfaces:**
- Consumes: `FileServeFacts(served_interval:Window|None,gaps_in_served_interval:int|None,gaps_in_requested_window:int)`; `file_serve_facts(app_id:int,window:Window)->FileServeFacts`; `tab_store`, `H,BOOT`.
- Produces: pruned `since_boot` and `lifetime` retain the original requested Window and aggregate scalar, with served-hour count `None`.

- [ ] **Step 1: Prune only committed hourly evidence and query both aggregate kinds.**

```python
import pytest
from appio.contracts import Window
from appio.query import QueryService
from tests.p4.files_tab_support import tab_store, H, BOOT

@pytest.mark.parametrize("window", [Window("since_boot",BOOT,end_mono_ns=2*H), Window("lifetime")])
def test_pruned_totals_keep_requested_scope_and_unknown_served_hour_count(tab_store, window):
    db = tab_store(hours=(0,1), app_gaps={}, gaps=(), end=2*H)
    for table in ("file_sample","file_coverage","file_overflow_unknown"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    service = QueryService(db)
    with service._snapshot():
        for aid, expected in ((1,30),(2,60)):
            table = service.file_table(aid,window,"bytes_total","desc",None)
            cov = service.file_coverage(aid,window)
            facts = service.file_serve_facts(aid,window)
            assert table.window == cov.window == facts.served_interval == window
            assert cov.served_interval == window
            assert cov.coverage.tier == "totals_only"
            assert cov.observed_bytes_r == expected
            assert facts.gaps_in_served_interval is None
            assert facts.gaps_in_requested_window == cov.coverage.gap_intervals == 0
```

Add an aggregate with retained nonzero coverage scalar before hourly pruning and assert that exact scalar survives, while served count remains `None`. Test no invented range/zero-length scope: equality is to the actual `since_boot`/`lifetime` value, not a fabricated Window. Formatter labels are independently pinned in 15.

- [ ] **Step 2: Run targeted pruned semantics and existing pruning regressions.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_totals.py","tests/test_query.py::test_full_boot_file_totals_survive_hourly_row_pruning","tests/test_query.py::test_file_totals_last_seen_follows_selected_boot_chronology_and_ignores_alias_history"]
```

- [ ] **Step 3: Preserve aggregate paths; repair only facts if exposed.**

A valid `scope in ("boot","lifetime")` remains the requested Window and uses existing totals/coverage. `boot` is internal only. The facts method leaves `measured=None` for these scopes even when aggregate coverage is complete; no per-hour rows means no served-hour measurement. Do not make valid aggregates unavailable to force the count to be an integer.

- [ ] **Step 4: Falsify and restore.**

`M_totals_complete_means_zero` must fail `is None`; `M_totals_fabricate_range` must fail Window equality; `M_pruned_resets_requested_scalar` must fail the nonzero scalar case; `M_totals_require_hourly_rows` must fail valid aggregate availability/bytes. Restore, rerun validation, and integrate query changes before UI formatter work.

- [ ] **Step 5: Commit**

```text
test(query): preserve pruned Files aggregate scope and unknown gaps
```

## Task 14: Refuse unavailable sources instead of publishing numeric emptiness

**Implementer tier:** Advanced

**Contract:** No authoritative source and unavailable outcome 1, §§3.2, 3.3, 4.3 and §8.

**Depends on:** 10. Serialize shared router repairs with 11.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_unavailable.py`.
- Modify: `src/appio/history_router.py:172-238` (missing served/result validation only).
- Modify: `src/appio/ui/files_view.py:692-707,818-832` (`_publish` guard and unavailable rendering; line anchors may move).
- Modify: `tests/p4/test_files_view.py:640-721` (unavailable/selector copy assertions only).

**Interfaces:**
- Consumes: `read_files`, `tab_store`, `R`; `WindowUnavailableError(reason:str,message:str)`; view `FakeRouter`, `build(qtbot,router)`, `APPS`, `file_view`, `coverage` from `tests.p4.test_files_view`; `FileCoverage.served_interval`.
- Produces: typed source refusal; `_publish` never renders numbers without a valid served scope; existing selector state remains distinct.

- [ ] **Step 1: Write no-receipt/no-calendar and exact unavailable-view pins.**

```python
import pytest
from appio.contracts import WindowUnavailableError
from tests.p4.files_tab_support import tab_store, read_files, R
from tests.p4.test_files_view import FakeRouter, build, APPS

@pytest.mark.parametrize("missing", ["receipt","calendar"])
def test_no_authoritative_candidate_is_typed_refusal_not_numeric_empty(tab_store, missing):
    db = tab_store(apps=() if missing == "receipt" else (1,2), hours=() if missing == "calendar" else (2,3), app_gaps={}, gaps=())
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("local",db,"gui"),))
    if missing == "receipt":
        assert (caught.value.reason,str(caught.value)) == ("file_source_unavailable","No authoritative file app receipt.")
    else:
        assert caught.value.reason in {"not_retained","file_source_unavailable","unsupported_window"}

@pytest.mark.parametrize("reason", ["not_retained","unsupported_window","launch_attribution","file_source_unavailable"])
def test_unavailable_view_preserves_reason_and_clears_numeric_results(qtbot, reason):
    router = FakeRouter(error=WindowUnavailableError(reason,"synthetic refusal"), ranking_rows=APPS)
    view = build(qtbot,router)
    view.set_window(R)
    assert view.state_label.text() == f"file history unavailable: {reason}: synthetic refusal"
    assert view.model.rowCount() == 0
    assert view.last_result is None
    for label in view._footer_labels():
        assert "0 read" not in label.text()
        assert "served interval" not in label.text()
```

Add `test_untyped_failure_has_no_fabricated_reason_segment` expecting `file history unavailable: synthetic failure`, `test_placeholder_coverage_with_no_served_scope_never_publishes`, and `test_empty_application_selector_remains_separate`: successful available source with no apps keeps exactly `coverage: not applicable; no applications observed in this window`, sends no per-app query; unavailable capability must outrank that selector state.

- [ ] **Step 2: Run the unavailable pins before any UI guard adjustment.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_unavailable.py","tests/p4/test_files_view.py::test_an_absent_file_capability_is_unavailable_before_any_request","tests/p4/test_files_view.py::test_no_observed_applications_is_stated_instead_of_an_empty_zero_table"]
```

- [ ] **Step 3: Keep refusal before any row/zero rendering.**

`_publish` checks `coverage.served_interval is None` or returned unavailability before setting model rows or coverage text. Existing `_set_unavailable(reason,message)` clears the model and all numeric footer lines with the established unavailable marker; preserve typed messages and omit the reason segment only when no typed reason exists. Do not interpret an unavailable placeholder's nested window as an accepted scope. Router query-validation remains inside the permitted candidate skip, with request-level refusal only after scan/fatal error.

- [ ] **Step 4: Falsify and restore.**

`M_unavailable_returns_empty_success` must fail typed refusal; `M_placeholder_is_served` must fail no publication; `M_unavailable_numeric_zero` must fail footer checks; `M_unavailable_hidden_by_no_apps` must fail capability precedence. Restore and rerun validation; don't change the existing no-app selector copy.

- [ ] **Step 5: Commit**

```text
fix(files): keep unavailable history distinct from empty activity
```

## Task 15: Implement canonical served/requested scope formatting

**Implementer tier:** Advanced

**Contract:** Scope formatter and bounds, §3.1 and §8.

**Depends on:** 11, 12, 13, 14.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/ui/files_view.py:1-80,707-788` (private pure formatters and scope construction).
- Create: `tests/p4/files_tab_view_cases.py`.
- Create: `tests/p4/test_files_tab_scope_text.py`.

**Interfaces:**
- Consumes: `FileCoverage.served_interval:Window|None`, `FileViewResult(...,served_gap_count:int|None)`, existing `elapsed_text(ns:int)->str`; existing test constructors `file_view`, `coverage`, `unknown_owner`, `FakeRouter`, `build`, `APPS`.
- Produces: `_scope_bounds(window:Window)->str` and `_scope_text(coverage:FileCoverage,served_gap_count:int|None)->str` in `appio.ui.files_view`. Test helper `result(*, requested=R, served=S, gap=1, requested_gaps=4, complete=False, rows=(), search=None, observed=(7,0), tier="raw")->FileViewResult`; `show(qtbot,value:FileViewResult)->FilesView`; literal `SCOPE_A` below. These are private/test seams, not wire types.

- [ ] **Step 1: Create contract-shaped view cases and literal scope expectations.**

```python
# tests/p4/files_tab_view_cases.py
from dataclasses import replace
from tests.p4.files_tab_support import R, S
from tests.p4.test_files_view import file_view, coverage, unknown_owner, FakeRouter, build, APPS

SCOPE_A = ("served interval only: T0=2h 0m 0s through T1=4h 0m 0s after boot; "
           "requested window: T0=0s through T1=6h 0m 0s after boot; "
           "this application's served-hour gap evidence in served interval: 1; "
           "this application's requested-window coverage scalar: 4")

def result(*, requested=R, served=S, gap=1, requested_gaps=4,
           complete=False, rows=(), search=None, observed=(7,0), tier="raw"):
    inner = coverage(complete=complete, gap_intervals=requested_gaps, tier=tier)
    unknown = replace(unknown_owner(cov=inner), window=served)
    return file_view(rows, window=requested, search=search, cov=inner,
                     unknown_owner_overflow=unknown, served_gap_count=gap,
                     observed_bytes_r=observed[0], observed_bytes_w=observed[1])

def show(qtbot, value):
    view = build(qtbot, FakeRouter(result=value, ranking_rows=APPS))
    view.set_window(value.table.window)
    qtbot.waitUntil(lambda: view.state != "loading", timeout=1000)
    return view
```

```python
from appio.contracts import Window
from appio.ui.files_view import _scope_bounds, _scope_text
from tests.p4.files_tab_support import H, BOOT, R, S
from tests.p4.files_tab_view_cases import result, SCOPE_A

def test_canonical_partial_range_scope_is_exact():
    value = result()
    assert _scope_text(value.coverage,value.served_gap_count) == SCOPE_A

def test_scope_formats_since_boot_end_lifetime_and_unknown_gap():
    sb = Window("since_boot",BOOT,end_mono_ns=6*H)
    assert _scope_bounds(sb) == "since_boot through T1=6h 0m 0s after boot"
    assert _scope_bounds(Window("lifetime")) == "lifetime"
    for requested in (R,sb,Window("lifetime")):
        value = result(requested=requested,served=requested,gap=None,tier="totals_only" if requested.kind=="lifetime" else "raw")
        text = _scope_text(value.coverage,None)
        assert text.startswith("served interval: ")
        assert "served interval only:" not in text
        assert "this application's served-hour gap evidence in served interval: unavailable" in text
```

Add full/shortened `range`, full/shortened `since_boot`, pruned `since_boot`, and pruned lifetime exact expected strings (do not get expected text by invoking `_scope_bounds`). Assert no internal `boot` **kind label** (the required phrase `after boot` is allowed). Launch queries still refuse with their existing A22 typed reason/message; test `QueryService.file_serve_facts(1,Window("launch",BOOT,launch_id=1))`, not an invented successful launch footer. Assert `only` depends on scope inequality, not `complete` or row count.

- [ ] **Step 2: Run the exact-text tests and observe the missing formatter failure.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_scope_text.py"]
```

- [ ] **Step 3: Implement pure formatting, independent of the view's current request.**

```python
def _scope_bounds(window: Window) -> str:
    if window.kind == "range":
        return (f"T0={elapsed_text(window.start_mono_ns)} through "
                f"T1={elapsed_text(window.end_mono_ns)} after boot")
    if window.kind == "since_boot":
        return f"since_boot through T1={elapsed_text(window.end_mono_ns)} after boot"
    if window.kind == "lifetime":
        return "lifetime"
    raise ValueError("No successful Files scope for this Window kind.")

def _scope_text(coverage: FileCoverage, served_gap_count: int | None) -> str:
    served = coverage.served_interval
    if served is None:
        raise ValueError("Unavailable Files result has no scope disclosure.")
    only = " only" if served != coverage.window else ""
    gap = "unavailable" if served_gap_count is None else str(served_gap_count)
    return (f"served interval{only}: {_scope_bounds(served)}; "
            f"requested window: {_scope_bounds(coverage.window)}; "
            f"this application's served-hour gap evidence in served interval: {gap}; "
            f"this application's requested-window coverage scalar: {coverage.coverage.gap_intervals}")
```

Use existing imports from `appio.contracts`, not a new router/query import (the view's layer-import test must stay true). Compute this string once per accepted render and pass it to later footer changes. No database scan or stateful requery occurs in a formatter.

- [ ] **Step 4: Falsify and restore.**

`M_since_boot_drops_end`, `M_totals_label_is_boot`, `M_lifetime_invents_bounds`, `M_only_follows_incomplete`, `M_None_is_zero`, and `M_formatter_uses_current_request` must fail corresponding literal assertions. For the last mutation render an accepted old-scope value after changing the view's request in an isolated direct formatter check; neither formatter signature accepts mutable current-window state. Task 21 independently proves stale response retirement. Rerun validation.

- [ ] **Step 5: Commit**

```text
feat(files): format canonical served and requested scope disclosures
```

## Task 16: Render retained rows in query order with truthful observed bytes

**Implementer tier:** Advanced

**Contract:** Ordered rows outcome 2, §3.2 and §8.

**Depends on:** 15.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/ui/files_view.py:692-806` (rows branch and observed text).
- Create: `tests/p4/test_files_tab_rows.py`.
- Modify: `tests/p4/test_files_view.py:343-365,432-449,520-536` (superseded partial/observed copy assertions only).
- Modify: `tests/p4/test_file_served_interval.py:222-239` (superseded scope copy assertions only).

**Interfaces:**
- Consumes: `_scope_text(coverage:FileCoverage,served_gap_count:int|None)->str`; `result`/`show` helpers and `SCOPE_A`; `file_row(**overrides)->FileRow` and `texts(view)->list[list[str]]`.
- Produces: rows branch exact `footer_state`; `_observed_text(coverage:FileCoverage,table:FileTable,scope_text:str)->str` with the specified bytes/row count/scope; model preserves query order. Empty branches remain owned by 17–19.

- [ ] **Step 1: Write adversarial row ordering, partial, and filtered pins.**

```python
import pytest
from tests.p4.test_files_view import file_row, texts
from tests.p4.files_tab_view_cases import result, show, SCOPE_A

@pytest.mark.parametrize("search", [None,"a"])
def test_rows_outcome_keeps_query_order_and_served_byte_scope(qtbot, search):
    rows = (file_row(basename="zeta",bytes_r=10,bytes_w=0),
            file_row(path_id=2,inode=43,basename="alpha",bytes_r=5,bytes_w=0))
    value = result(rows=rows,search=search,observed=(15,0))
    view = show(qtbot,value)
    assert [row[0] for row in texts(view)] == ["zeta","alpha"]
    matching = "" if search is None else "; matching 'a'"
    assert view.footer_state.text() == f"rows retained{matching}; {SCOPE_A}; coverage: incomplete"
    observed = f"observed (served interval only): 15 B read, 0 B written; 2 retained file row(s); {SCOPE_A}"
    if search is not None:
        observed += "; table filtered to 2 row(s) matching 'a'"
    assert view.footer_observed.text() == observed
    assert "whole window" not in view.footer_observed.text()
```

Add full/complete row variant: served=requested, gap=0, complete true; exact state uses complete and observed uses `(served interval)` without `only`. Keep bytes_total descending as query selector, not just a coincidentally alphabetical fixture. Both zeta and alpha match search `a`, so a view-side filter/order mutation is detectable.

- [ ] **Step 2: Run the new exact-output pins and existing sort regressions.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_rows.py","tests/p4/test_files_view.py::test_sorting_keeps_the_server_row_order_and_never_drops_rows","tests/p4/test_files_view.py::test_last_seen_sorts_by_stored_value_not_by_display_text"]
```

- [ ] **Step 3: Implement only the rows predicate and shared observed form.**

After the unavailable guard, set rows directly from `result.table.rows`; do not sort/filter locally. If rows exist:

```python
status = "complete" if inner.complete else "incomplete"
matching = "" if result.table.search is None else f"; matching {result.table.search!r}"
self.footer_state.setText(f"rows retained{matching}; {scope}; coverage: {status}")
```

Use `scope = _scope_text(coverage,result.served_gap_count)` from the result. `_observed_text` returns:

```python
only = " only" if coverage.served_interval != coverage.window else ""
count = len(table.rows)
text = (f"observed (served interval{only}): "
        f"{format_bytes(coverage.observed_bytes_r)} read, "
        f"{format_bytes(coverage.observed_bytes_w)} written; "
        f"{count} retained file row(s); {scope_text}")
if table.search is not None:
    text += f"; table filtered to {count} row(s) matching {table.search!r}"
return text
```

The literal `row(s)` follows §3, not old singular/plural copy. Update only assertions superseded by the specification, retaining row/count/zero honesty assertions. Other coverage-counter lines are completed by 20; do not silently omit that dependency.

- [ ] **Step 4: Falsify and restore.**

`M_view_basename_sort` must change zeta/alpha to alpha/zeta and fail order; `M_view_local_filter` must fail row identity/count in an independently supplied query result; `M_observed_whole_window` must fail exact prefix; `M_rows_require_complete` must fail the partial/filtered rows branch. Rerun validation.

- [ ] **Step 5: Commit**

```text
feat(files): render retained rows with exact served-scope bytes
```

## Task 17: Distinguish a filtered zero from absence of activity

**Implementer tier:** Standard

**Contract:** Filtered zero outcome 3, §§3.2–3.3 and §8.

**Depends on:** 16.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/ui/files_view.py:707-788` (empty filtered state branch only).
- Create: `tests/p4/test_files_tab_no_matches.py`.

**Interfaces:**
- Consumes: `result(...,rows=(),search:str|None,observed=(7,0))->FileViewResult`, `show(qtbot,value)`, literal `SCOPE_A`; `_observed_text(coverage,table,scope_text)->str` from 16; existing normalized `FilesView.search()->str|None`.
- Produces: exact no-matches state before either unfiltered empty branch.

- [ ] **Step 1: Add a zero-row filtered result with nonzero aggregate bytes.**

```python
from tests.p4.files_tab_view_cases import result, show, SCOPE_A

def test_filtered_zero_is_no_matches_with_independent_observed_bytes(qtbot):
    view = show(qtbot,result(search="absent",rows=(),observed=(7,0)))
    assert view.model.rowCount() == 0
    assert view.footer_state.text() == f"no retained file matches 'absent'; {SCOPE_A}; coverage: incomplete"
    assert view.footer_observed.text() == (f"observed (served interval only): 7 B read, 0 B written; 0 retained file row(s); {SCOPE_A}; table filtered to 0 row(s) matching 'absent'")
    assert "no retained file activity" not in view.footer_state.text()
```

Parameterize complete/incomplete returned coverage and assert no_matches wins in both. Add `test_whitespace_only_search_is_not_a_filter` through `search_field`, `search()` and captured request (`None`), retaining NFC normalization; it must route to the later no-filter branch, not emit a no-matches claim for spaces.

- [ ] **Step 2: Run the new pins and existing search-normalization test.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_no_matches.py","tests/p4/test_files_view.py::test_search_sends_nfc_normalized_text_and_none_when_blank"]
```

- [ ] **Step 3: Add outcome 3 after rows and before unfiltered emptiness.**

```python
elif result.table.search is not None:
    self.footer_state.setText(
        f"no retained file matches {result.table.search!r}; {scope}; coverage: {status}")
```

Use the normalized **returned** search, not mutable widget text. Aggregate `observed_bytes_*` remain separate from filtered row count; no invented “unknown bytes” value or unfiltered retained-population claim.

- [ ] **Step 4: Falsify and restore.**

`M_filtered_zero_is_empty_partial` must fail exact state; `M_filter_zero_means_no_activity` must fail no-activity exclusion; `M_filter_hides_aggregate_bytes` must fail `7 B`; `M_spaces_are_active` must fail request `None`. Restore and rerun validation.

- [ ] **Step 5: Commit**

```text
feat(files): disclose zero search matches without claiming no activity
```

## Task 18: Render complete empty activity for the selected application

**Implementer tier:** Standard

**Contract:** Complete empty outcome 4, §3.2 and §8.

**Depends on:** 17.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/ui/files_view.py:707-788` (complete unfiltered empty branch only).
- Create: `tests/p4/test_files_tab_empty_proven.py`.
- Modify: `tests/p4/test_files_view.py:698-707` (complete-empty assertions only).

**Interfaces:**
- Consumes: `result`, `show` helpers with `requested=served`, `gap=0`, `requested_gaps=0`, `complete=True`, empty rows/no search; `_scope_text(coverage,count)->str`.
- Produces: exact selected-application empty-proven disclosure, leaving the selector's no-app state distinct.

- [ ] **Step 1: Assert complete emptiness and visible measured counters.**

```python
from appio.ui.files_view import _scope_text
from tests.p4.files_tab_support import S
from tests.p4.files_tab_view_cases import result, show

def test_complete_empty_is_per_application_not_unavailable_or_no_apps(qtbot):
    value = result(requested=S,served=S,gap=0,requested_gaps=0,complete=True,rows=(),observed=(0,0))
    view = show(qtbot,value)
    scope = ("served interval: T0=2h 0m 0s through T1=4h 0m 0s after boot; requested window: T0=2h 0m 0s through T1=4h 0m 0s after boot; this application's served-hour gap evidence in served interval: 0; this application's requested-window coverage scalar: 0")
    assert view.footer_state.text() == f"no retained file activity for this application in the served interval; {scope}; coverage: complete"
    assert view.state == "ready"
    assert view.model.rowCount() == 0
    assert "no applications observed" not in view.footer_state.text()
    assert "0 B read" in view.footer_observed.text()
    assert "counted omissions" in view.footer_counted.text()
```

Also create a committed synthetic app hour with no supported file observations using `HourlyAccumulator.add_coverage`'s existing signature (read `files.py` before calling), so the result is not merely a fake empty table. If the producer signature is `add_app_coverage` instead, use that existing method; do not add a new producer API. Keep another app busy and assert the selected app's empty proof does not claim the selector was empty.

- [ ] **Step 2: Run the complete-empty pin.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_empty_proven.py","tests/p4/test_files_view.py::test_empty_rows_with_complete_zero_coverage_is_ready_not_unavailable"]
```

- [ ] **Step 3: Add the complete unfiltered empty branch.**

```python
elif inner.complete:
    self.footer_state.setText(
        "no retained file activity for this application in the served interval; "
        f"{scope}; coverage: complete")
```

Keep model empty and numeric coverage lines visible. Do not invoke `_render_no_applications` or `_set_unavailable` for this valid result.

- [ ] **Step 4: Falsify and restore.**

`M_empty_complete_is_unavailable`, `M_empty_complete_is_no_apps`, and `M_empty_complete_uses_partial_copy` must fail their exact placement/state assertions. Restore and rerun validation.

- [ ] **Step 5: Commit**

```text
feat(files): disclose proved empty activity per selected application
```

## Task 19: Render incomplete empty results without collapsing unknown evidence

**Implementer tier:** Standard

**Contract:** Partial empty outcome 5, §3.2 and §8.

**Depends on:** 18.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/ui/files_view.py:707-788` (final incomplete empty branch only).
- Create: `tests/p4/test_files_tab_empty_partial.py`.

**Interfaces:**
- Consumes: `result`/`show` helpers, `FileViewResult(...,served_gap_count:int|None)`, `_scope_text(coverage,count)->str`; valid pruned scope behavior from 13.
- Produces: exact final outcome for no search, zero rows, incomplete coverage; integer zero stays zero and unknown stays unavailable.

- [ ] **Step 1: Add local-zero, pruned-None, and remote-None controls.**

```python
import pytest
from appio.contracts import Window
from tests.p4.files_tab_support import R, S
from tests.p4.files_tab_view_cases import result, show

@pytest.mark.parametrize("route,gap,requested,served,tier", [
    ("local",0,R,S,"raw"),
    ("pruned",None,Window("lifetime"),Window("lifetime"),"totals_only"),
    ("remote",None,R,S,"raw"),
])
def test_partial_empty_keeps_both_scopes_and_zero_unknown_distinction(qtbot, route, gap, requested, served, tier):
    value = result(requested=requested,served=served,gap=gap,rows=(),complete=False,tier=tier)
    view = show(qtbot,value)
    text = view.footer_state.text()
    assert text.startswith("no retained file activity in the served interval; coverage: incomplete; ")
    expected_gap = "unavailable" if gap is None else "0"
    assert f"this application's served-hour gap evidence in served interval: {expected_gap}" in text
    assert "this application's requested-window coverage scalar: 4" in text
    assert "requested window:" in text and "served interval" in text
    assert view.state == "incomplete"
    assert view.model.rowCount() == 0
```

Add full-scope incomplete with measured zero due to non-gap uncertainty; it must not say `only` merely because coverage is false. Assert no `coverage: complete`, no selector no-app copy, and no unavailable state label for all valid cases.

- [ ] **Step 2: Run the partial-empty pins.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_empty_partial.py"]
```

- [ ] **Step 3: Finish the ordered outcome chain with exact copy.**

```python
else:
    self.footer_state.setText(
        "no retained file activity in the served interval; coverage: incomplete; "
        f"{scope}")
```

At this point the chain is unavailable guard, rows, normalized active search, complete empty, incomplete empty. Exactly one terminal outcome owns the state. Keep coverage counters visible for partial empty values, including unknown served count.

- [ ] **Step 4: Falsify and restore.**

`M_zero_rows_implies_complete` must fail incomplete state/copy; `M_partial_omits_requested_scalar` must fail the second label; `M_partial_None_is_zero` must fail each None variant; `M_incomplete_equals_shortened` must fail the full-scope control. Restore and rerun validation.

- [ ] **Step 5: Commit**

```text
feat(files): disclose incomplete empty activity with explicit unknown gaps
```

## Task 20: Apply canonical disclosure to every coverage footer line

**Implementer tier:** Advanced

**Contract:** Exact disclosure placement, §§3.1–3.3 and §8.

**Depends on:** 19.

**Files (exclusive modification allowlist):**
- Modify: `src/appio/ui/files_view.py:707-806` (coverage-counter suffixes and shared scope construction).
- Create: `tests/p4/test_files_tab_disclosure.py`.
- Modify: `tests/p4/test_files_view.py:343-365,520-565,723-845` (superseded footer copy assertions; retain all independent counter checks).
- Modify: `tests/p4/test_file_served_interval.py:222-239` (exact footer copy assertions).

**Interfaces:**
- Consumes: `_scope_text(coverage:FileCoverage,count:int|None)->str`, `_observed_text(coverage,table,scope_text)->str`; helpers `result`, `show`, `SCOPE_A`.
- Produces: one computed scope string consistently placed in `footer_state`, `footer_observed`, `footer_counted`, `footer_uncounted`, `footer_unknown_owner`, `footer_skipped`, `footer_weak`, `footer_hooks`.

- [ ] **Step 1: Test every footer, not an aggregate joined string alone.**

```python
import pytest
from tests.p4.files_tab_view_cases import result, show, SCOPE_A

@pytest.mark.parametrize("gap", [1,0,None])
def test_each_coverage_footer_contains_canonical_scope_and_reducer_labels(qtbot, gap):
    value = result(gap=gap)
    view = show(qtbot,value)
    scope = SCOPE_A.replace("in served interval: 1;", "in served interval: " + ("unavailable" if gap is None else str(gap)) + ";")
    for name in ("footer_state","footer_observed","footer_counted","footer_uncounted","footer_unknown_owner","footer_skipped","footer_weak","footer_hooks"):
        text = getattr(view,name).text()
        assert scope in text, (name,text)
        assert "this application's served-hour gap evidence in served interval:" in text
        assert "this application's requested-window coverage scalar: 4" in text
        assert "whole window" not in text
```

Parameterize populated rows, no_matches, complete_empty, and partial_empty in additional cases. Assert empty-state copy resides in `footer_state`, unavailable copy in `state_label`, and no numeric/unscoped old footer remains after replacing a valid result with refusal. Keep current counted-vs-uncounted, unknown-owner, skipped, weak, and every-hook values separately asserted; scope repetition must not combine their counters.

- [ ] **Step 2: Run the exact placement pins and the bounded UI regression file.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_disclosure.py","tests/p4/test_files_view.py","tests/p4/test_file_served_interval.py"]
```

This is a targeted three-file group, not the full suite.

- [ ] **Step 3: Reuse exactly one canonical string for the entire render.**

Replace the old partial-only `scope_prefix`. Leave existing counter descriptions/formatting unchanged and append `f"; {scope}"` to each counter line. State and observed already consume the identical string from 15–19. The UI does not scan gaps or recalculate app reducers. For `None`, use exactly `unavailable`, not omission or zero. Convert obsolete tests demanding `collection has gaps in the requested window` to the normative app-level labels; keep their scope/row facts.

- [ ] **Step 4: Falsify placement and retain counter honesty.**

For each footer independently run `M_drop_scope_<footer>`; its own per-label assertion must fail. `M_served_label_is_requested_gaps` must fail exact label; `M_observed_whole_window` must fail observed; `M_mix_counted_uncounted` must still fail the existing separate-counter test. Restore and rerun the full targeted validation group, including the layer-import pin that forbids new production-module imports.

- [ ] **Step 5: Commit**

```text
feat(files): repeat exact scope disclosure across coverage footers
```

## Task 21: Retire stale rows and facts together

**Implementer tier:** Advanced

**Contract:** Stale reply, §8; preserve the existing `keep_pending` behavior.

**Depends on:** 11, 20.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/test_files_tab_stale.py`.
- Modify: `src/appio/ui/files_view.py:548-627` (generation delivery checks only if a defect is proved).
- Modify: `src/appio/history_router.py:130-156,279-347` (file generation settlement only if a defect is proved).
- No collector-window production modification.

**Interfaces:**
- Consumes: `FileViewResult(table,coverage,served_gap_count)`; `_scope_text`; existing view fake router `.pending:list[Future]`, `defer=True`, `build(qtbot,router)`, `APPS`; existing real remote generation/cursor fault harness.
- Produces: stale-generation exclusion covers rows, compatibility scope, requested scalar, and served count atomically, not just the table.

- [ ] **Step 1: Defer two distinguishable replies and drain both deliveries.**

```python
from dataclasses import replace
from tests.p4.test_files_view import FakeRouter, build, APPS, file_row
from tests.p4.files_tab_view_cases import result
from tests.p4.files_tab_support import R, S, H, BOOT
from appio.contracts import Window

def test_retired_reply_cannot_replace_rows_or_serve_facts(qtbot):
    router = FakeRouter(defer=True,ranking_rows=APPS)
    view = build(qtbot,router)
    view.set_window(R)
    first = router.pending[-1]
    next_window = Window("range",BOOT,0,8*H)
    view.set_window(next_window)
    second = router.pending[-1]
    current = result(requested=next_window,served=S,gap=2,requested_gaps=5,
                     rows=(file_row(basename="current",bytes_r=20,bytes_w=0),),observed=(20,0))
    stale = result(rows=(file_row(basename="retired",bytes_r=99,bytes_w=0),),gap=1,requested_gaps=4,observed=(99,0))
    second.set_result(current)
    qtbot.waitUntil(lambda: view.last_result is current,timeout=1000)
    delivered = []
    view._bridge.delivered.connect(lambda payload: delivered.append(payload[0]))
    first.set_result(stale)
    qtbot.waitUntil(lambda: len(delivered) >= 1,timeout=1000)
    assert view.last_result is current
    assert view.model.rows == current.table.rows
    assert "served-hour gap evidence in served interval: 2" in view.footer_state.text()
    assert "requested-window coverage scalar: 5" in view.footer_state.text()
    assert "T1=8h 0m 0s" in view.footer_state.text()
```

Use the model's actual existing row accessor (`_rows` if no public `rows`) after reading it; do not add an API solely for this assertion. Also parameterize old reply arriving first while the second is still pending: it must leave loading, not publish old facts. Preserve the existing router invalidation/cursor and same-scope in-flight-history tests. Use signal/event barriers so an assertion cannot run before the stale callback actually drains.

- [ ] **Step 2: Run generation and request-churn regressions.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_stale.py","tests/p4/test_file_history_router.py::test_remote_scope_snapshot_stale_cursor_and_invalidate_fail_closed","tests/p3/test_collector_window.py::test_window_keeps_in_flight_history_for_the_same_scope"]
```

- [ ] **Step 3: Preserve whole-result retirement.**

Generation/closed/connection checks happen before `_publish`, so no stale member of `FileViewResult` escapes. Do not add independent facts callbacks or caches. Preserve current in-flight same-scope history rather than replacing it every refresh. Repair only a demonstrated generation guard inside the listed allowlist.

- [ ] **Step 4: Falsify after the queue barrier, restore, and rerun.**

`M_publish_retired_view_generation` must fail `last_result is current` or scope/count assertion **after delivery**; `M_publish_retired_remote_generation` must fail existing remote invalidation; `M_stale_facts_only` must fail reducer labels even if rows remain current; `M_refresh_replaces_pending` must fail the existing keep-pending regression. A passing assertion before callback dispatch is not evidence.

- [ ] **Step 5: Commit**

```text
test(files): retire stale rows and serve facts as one result
```

## Task 22: Preserve probe-only answering truth after stream EOF

**Implementer tier:** Advanced

**Contract:** Label probe-only truth, §§7.1–7.3 and §8. Preservation only, not a label repair.

**Depends on:** 10 (P3 fake-result constructor converted).

**Files (exclusive modification allowlist):**
- Modify: `tests/p3/test_collector_window.py:275-341,516-531` (new transition pin and witness controls).
- No production label/ownership/setup/IodClient code modifications.

**Interfaces:**
- Consumes: existing `build(qtbot,*,facts,ready=False,setup=None,events=None)->(CollectorWindow,FakeCoordinator,FakeHistory,FakeSetup)`; `FakeSetup.facts:ServiceFacts`; `Harness()` with `.handshake()`, `.eof()`, `.client`, `.clock`, `.servers`; `CollectorWindow.refresh()->None`, `.state_text`, `.banner_status`, `.settings.status_label`; `ON`, `SILENT`, `COLLECTOR_STATE_ON_ANSWERING`, `COLLECTOR_STATE_SILENT`.
- Produces: a real protocol-status/EOF, stubbed unprivileged-probe transition test. Root activation helper is never the witness.

- [ ] **Step 1: Write a deterministic three-refresh trace.**

```python
from tests.p3.test_iodclient import Harness

def test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF(qtbot):
    window, coordinator, history, setup = build(qtbot,facts=ON)
    h = Harness()
    window._timer.stop()
    history.client = h.client
    window._pump = h.client.pump
    try:
        h.handshake()
        window.refresh()
        assert h.client.state.verified and h.client.state.answering
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        h.eof()
        h.client.pump()
        assert not h.client.state.verified
        for _ in range(3):
            setup.facts = ON
            window.refresh()
            assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
            assert window.banner_status.text() == COLLECTOR_STATE_ON_ANSWERING
            assert window.settings.status_label.text() == COLLECTOR_STATE_ON_ANSWERING
            assert coordinator.reconciles[-1].daemon_verified
            assert coordinator.reconciles[-1].answering
        assert setup.calls == []
    finally:
        window._timer.stop()
        h.client.close()
        for server in h.servers:
            server.close()
```

The fake `setup.refresh()` represents the unprivileged status witness. Keep a separate `test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers` control with a root helper body claiming answering while the current unprivileged probe is silent; it must not establish answering. Keep activation state/pending precedence tests unchanged. Record refresh/probe invocation counts so a loop that never refreshes cannot pass unnoticed.

- [ ] **Step 2: Run new and existing authority pins.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p3/test_collector_window.py::test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF","tests/p3/test_collector_window.py::test_the_gui_probe_not_the_helper_body_decides_whether_the_daemon_answers","tests/p3/test_collector_window.py::test_current_link_status_requires_verification_and_preserves_activation","tests/p3/test_collector_window.py::test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict","tests/p3/test_collector_window.py::test_pending_activation_stays_visible_across_a_restart"]
```

- [ ] **Step 3: Do not implement a label repair.**

Read `CollectorWindow.refresh`, `_apply_body`, `display_state`, and coordinator `decide_owner` use; record that existing behavior supplies the trace. If the pin exposes a real contradiction/failure, stop with the observation; this task's allowlist does not authorize changing precedence. No helper invocation, daemon/root probe, or real system status is needed.

- [ ] **Step 4: Falsify the authority distinction and restore.**

`M_EOF_always_demotes` must fail one of the three exact answering assertions. `M_root_body_can_assert_answering` must fail the silent-unprivileged witness control. A refresh-freeze mutation can leave this steady-state trace unchanged, so it is **not** claimed as killed here; Task 23 must kill it through observable demotion/recovery. Restore and rerun validation.

- [ ] **Step 5: Commit**

```text
test(ui): preserve answering status from unprivileged probe after EOF
```

## Task 23: Pin intermediate demotion and verified connection recovery

**Implementer tier:** Advanced

**Contract:** Label recovery, §§7.2–7.3 and §8. Preservation only.

**Depends on:** 22.

**Files (exclusive modification allowlist):**
- Modify: `tests/p3/test_collector_window.py:275-341` (new recovery transition test).
- No production modification.

**Interfaces:**
- Consumes: `build(qtbot,*,facts=OFF,ready=False,setup=None,events=None)->(CollectorWindow,FakeCoordinator,FakeHistory,FakeSetup)`; `FakeSetup.facts:ServiceFacts`; `Harness()` with `.handshake()->tuple[dict,dict]`, `.eof()->None`, `.clock.advance(seconds:float)->None`, `.one_request(kind:str)->dict`, `.status()->None`, `.client.pump()->None`, `.client.state.verified:bool`, `.client.state.answering:bool`, and `.servers:list[socket.socket]`; `CollectorWindow.refresh()->None`, `.state_text`, `.banner_status`, `.settings.status_label`; `ON`, `SILENT`, `COLLECTOR_STATE_ON_ANSWERING`.
- Produces: answering -> exact silent -> answering trace plus actual new-connection verification, without restarting the window.

- [ ] **Step 1: Write the complete trace, including both witnesses.**

```python
from tests.p3.test_iodclient import Harness

def test_label_recovers_after_silent_refresh_and_connection_reverification(qtbot):
    window, coordinator, history, setup = build(qtbot,facts=ON)
    h = Harness()
    window._timer.stop()
    history.client = h.client
    window._pump = h.client.pump
    try:
        h.handshake()
        window.refresh()
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert h.client.state.verified
        h.eof()
        h.client.pump()
        setup.facts = SILENT
        window.refresh()
        assert window.state_text == "enabled, not answering"
        assert window.banner_status.text() == "enabled, not answering"
        assert not h.client.state.verified
        setup.facts = ON
        window.refresh()
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        # An answering label alone is insufficient: complete this new link's status exchange.
        h.clock.advance(60)
        h.client.pump()
        h.one_request("status")
        h.status()
        h.client.pump()
        window.refresh()
        assert h.client.state.verified and h.client.state.answering
        assert window.state_text == COLLECTOR_STATE_ON_ANSWERING
        assert window.settings.status_label.text() == COLLECTOR_STATE_ON_ANSWERING
        assert setup.calls == []
    finally:
        window._timer.stop()
        h.client.close()
        for server in h.servers:
            server.close()
```

Use the harness's virtual reconnect deadline/backoff, not wall sleeps or a real daemon. Assert window identity remains unchanged and a new harness connection was established (`len(h.servers)` increases), so retaining an old verified flag cannot masquerade as reconnection. Add probe/refresh count assertions. The exact silent string is normative even if the test also compares `COLLECTOR_STATE_SILENT`.

- [ ] **Step 2: Run both new transitions plus existing precedence/activation pins.**

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p3/test_collector_window.py::test_label_recovers_after_silent_refresh_and_connection_reverification","tests/p3/test_collector_window.py::test_probe_only_truth_stays_answering_for_three_refreshes_after_EOF","tests/p3/test_collector_window.py::test_verified_status_round_trip_clears_silent_header_while_window_open","tests/p3/test_collector_window.py::test_an_explicit_recheck_invokes_status_and_shows_the_probe_verdict","tests/p3/test_collector_window.py::test_pending_activation_stays_visible_across_a_restart"]
```

- [ ] **Step 3: Preserve all production label and owner bytes.**

No implementation change is planned. Capture production file hashes before/after the task and assert equality. A failing trace must be reported with its concrete observation for a separately reviewed decision, not repaired by weakening the intermediate assertion, skipping verification, or changing display-state priority.

- [ ] **Step 4: Kill freeze/stale-facts/probe-only-recovery mutants.**

`M_freeze_refresh_after_answering` and `M_keep_stale_answering_facts` must fail the intermediate exact silent label. `M_freeze_refresh_after_silent` must fail next-refresh recovery. `M_probe_only_recovery_no_stream_verification` must fail the new-connection verified assertion even if the label answers. Restore byte-for-byte and rerun validation; report these as preservation pins, not claimed label fixes.

- [ ] **Step 5: Commit**

```text
test(ui): pin silent demotion and verified daemon recovery
```

## Task 24: Accept against a hash-pinned real target-store snapshot

**Implementer tier:** Capable

**Contract:** End-to-end acceptance, §8. This is the only mandatory real-database contract pin; synthetic success cannot replace it.

**Depends on:** 4, 21, 23 and all their transitive prerequisites. All candidate findings and targeted mutation gates must have evidence before acceptance.

**Files (exclusive modification allowlist):**
- Create: `tests/p4/capture_files_tab_snapshot.py`.
- Create: `tests/p4/files_tab_real_oracle.py`.
- Create: `tests/p4/test_files_tab_real_acceptance.py`.
- Create: `tests/fixtures/p4/files-tab-real.sqlite`.
- Create: `tests/fixtures/p4/files-tab-real.sha256`.
- Create: `tests/fixtures/p4/files-tab-real.json`.
- Create: `tests/fixtures/p4/files-tab-real.oracle.json`.
- Create: `docs/superpowers/reviews/2026-10-30-appio-files-tab-acceptance.md`.
- No production, spec/design, schema, or existing fixture modification. The real snapshot receives no test inserts, deletions, pruning, VACUUM, or migrations.

**Interfaces:**
- Consumes: custodian-supplied `/tmp/appio-files-tab-capture/iod.sqlite` and `capture.json`, a consistent frozen target-store capture readable without root; final `HistoryRouter`/`FilesView`/`CollectorWindow` public behavior from earlier tasks.
- Produces: `capture_files_tab_snapshot.py` CLI `--source PATH --capture PATH --output-dir PATH`; `files_tab_real_oracle.py` CLI `--database PATH --manifest PATH --output PATH --verify` and pure `derive(database:Path,manifest:dict)->dict` using only stdlib raw SQLite/JSON/hash logic; required pinned snapshot/manifest/oracle output and acceptance evidence. No production helper is imported by the oracle or capture tool.

- [ ] **Step 1: Fail closed on the real-input precondition before authoring acceptance expectations.**

At planning time the fixture does not exist and the target DB is not readable. If the two supplied handoff files are missing/unreadable, stop this task as **BLOCKED: readable frozen target capture required**. Do not use root, change permissions, install a collector, contact a daemon, create an empty DB at the acceptance path, use a synthetic tree, or mark the test skipped. The capture descriptor must identify `capture_id`, UTC capture time, source host identity, absolute source database path (`/var/lib/appio/iod.db` for this target), capture method, original/frozen byte hash, and source receipt/boot identity. Require a consistent frozen single-file database with no required WAL sidecar; a naïve copy of a live main DB is not a capture. The supplying custodian, not this task, establishes access to the source.

The capture tool copies the supplied frozen bytes once, without test edits. Before opening the delivered DB, compute SHA-256 in chunks and compare the input descriptor's frozen hash; write the identical digest to `.sha256` and manifest. Then open `mode=ro` with `PRAGMA query_only=ON`, check `integrity_check`, `PRAGMA user_version`, and a raw `sqlite_schema` signature. Do not invoke `migrate_empty`, `open_database`, a production schema validator, or any modifying PRAGMA on the artifact. The fixture is large; it remains the exact required artifact, not an LFS pointer, URL download, or seed script.

- [ ] **Step 2: Write the independent raw-row oracle and retain its result.**

The manifest's exact top-level contract is:

```text
snapshot_sha256: 64 lowercase hexadecimal digits matching .sha256 and database bytes
capture: {capture_id, captured_at_utc, source_host, source_database, method, source_identity, frozen_sha256}
schema: {user_version, sqlite_schema_sha256}
selection: {app_key, requested_window, surviving_store, expected_served_interval}
query: {sort: bytes_total, direction: desc, search: null}
oracle_sha256: SHA-256 of the retained .oracle.json bytes
```

`requested_window`/`expected_served_interval` are explicit dictionaries of Window values, not production objects. `source_identity` includes the recorded boot IDs, collector source/epoch/receipt bounds, and capture source identity; `surviving_store` names this captured target and its source role. Fill those values from the input descriptor/raw snapshot, not this plan's synthetic R/S.

The oracle must not import `appio`, the production interval/reducer/provenance helpers, or rendering/test fake constructors. Derive independently:

1. Read `boot.file_finalized_through_mono_ns`, `file_overflow_unknown` calendar, `app`, `collector_interval`, `gap`, `handover`, and the relevant raw coverage/sample rows in one read-only transaction. Enumerate real external AppKeys and supported range requests deterministically by boot chronology, then app key; require a nonempty retained table and **served != requested**. Prefer `R=[0, file_finalized_through_mono_ns)` for a boot within the existing range limit; if it has no qualifying accepted partial case, enumerate committed calendar runs and enclosing hour-aligned requests in latest-end order without changing any data. Do not select a shorter served run to evade a provenance failure: for each R use its actual longest/latest-end calendar run. If no real case satisfies the spec's predicates, fail/block with that finding rather than weaken them.
2. Implement an independent longest-run scan of finalized present hours. Use maximal length then latest end; assert S is hour-aligned, nonempty, inside R, and below watermark. Store both R and S in manifest/output; do not ask `QueryService` what they should be.
3. Resolve the chosen external key with raw `app` SQL. Read app `file_coverage.coverage_json` for S, validate the required integer fields (bool is not int evidence), and sum that app's hourly `gap_intervals`. Derive the **existing requested scalar** independently as that served-hour app contribution plus the count of persisted gap rows intersecting R; preserve the existing overlap/diagnostic/unknown-owner completeness contributions separately. Retain the actual app-hour vector and persisted intersections in oracle output so the derivation is inspectable. Never substitute the persisted S-row count for the served reducer.
4. Read raw `file_sample JOIN path JOIN boot` rows in S, group by the existing path identity, sum counters, choose last-seen by `(boot_order,monotonic_ns)`, and order by bytes_total descending with the existing literal tie-break `(dev UTF-8,inode,generation,basename UTF-8,path_id)` ascending. Independently derive `file_applications` aggregate total, observed bytes, row count, captured basename/state/identity fields, requested/carrier bounds, coverage completeness and counter values. Retain full expected row identities, not only totals or a nonempty boolean.
5. Derive provenance from raw collector intervals, source/epoch identities, handover cuts if present, gaps and overlaps. Clip to candidate S first; require a single proven authoritative store/owner, no intersecting overlap/uncertain epoch, and every unproved clipped segment fully covered by the non-overlap-gap union with hour-aligned endpoints. Independently check chronology. For this single-captured-store acceptance, record the sole registered source and why its owner/receipts prove the surviving candidate; do not assume a root-owned filename alone proves provenance. Reject unsupported/malformed evidence rather than silently simplify it. Output the provenance outcome and exact expected refusal if invalid; an invalid selected case cannot satisfy the successful acceptance pin.
6. Independently assemble the canonical expected scope/state/observed strings using the specification's literal grammar and independently formatted elapsed/byte values. Do not call `_scope_text`, `_observed_text`, `file_serve_facts`, `file_coverage`, `_largest_contiguous_file_interval`, `_plan`, or an oracle that calls them indirectly. Store this JSON before running the view and pin its hash in the manifest.

The capture tool chooses/finalizes the manifest using the oracle's raw selection; subsequent `--verify` recomputes the same expectations and must compare equal, not overwrite a failing expected result with production output. Retain a separate oracle-output hash in acceptance evidence. This is an implementation/test artifact format decision, not a new product protocol.

- [ ] **Step 3: Write hash-first and offscreen real-router acceptance tests.**

```python
# The test performs these assertions using the final oracle/manifest data.
def test_real_snapshot_hash_is_checked_before_sqlite_open():
    assert len(expected_sha256) == 64
    assert sha256_file(snapshot_path) == expected_sha256 == manifest["snapshot_sha256"]
    assert sha256_file(oracle_path) == manifest["oracle_sha256"]

def test_real_snapshot_offscreen_files_matches_independent_raw_oracle(qtbot):
    assert expected["served_interval"] != expected["requested_window"]
    assert expected["rows"]
    assert expected["provenance"]["accepted"] is True
    assert actual_result.table.window == requested_window
    assert actual_result.coverage.window == requested_window
    assert window_dict(actual_result.coverage.served_interval) == expected["served_interval"]
    assert actual_result.served_gap_count == expected["gaps_in_served_interval"]
    assert actual_result.coverage.coverage.gap_intervals == expected["gaps_in_requested_window"]
    assert normalized_actual_rows == expected["rows"]
    assert actual_result.coverage.observed_bytes_r == expected["observed_bytes_r"]
    assert actual_result.coverage.observed_bytes_w == expected["observed_bytes_w"]
    assert view.footer_state.text() == expected["footer_state"]
    assert view.footer_observed.text() == expected["footer_observed"]
    assert collector.state_text == COLLECTOR_STATE_ON_ANSWERING
    assert setup.facts.daemon_verified and setup.facts.answering
```

Implement all names above locally in the test, with no hidden globals: `sha256_file(Path)->str` streams bytes; `window_dict(Window)->dict` maps fields; `normalized_actual_rows` explicitly maps the returned dataclass row fields to raw-oracle names. Load/hash files in a session fixture **before any sqlite connection to the snapshot**, then return manifest/expectations/paths. After that check, open the source with `sqlite3.connect(snapshot_path.resolve().as_uri()+"?mode=ro",uri=True)` and `query_only`, construct real `QueryService`/`HistoryRouter`, and keep snapshot read-only. The router may make its existing private memory copy; do not mutate the fixture. Drive its real asynchronous Files request with the selected external key/presentation ID and event completion. For the Files app selector, use the real retained file-app fallback or explicitly select the manifest's key; do not stub table/coverage/results.

Compose `CollectorWindow` using a stub coordinator and unprivileged setup-status witness plus the real router. This task fixes the witness choice: use the existing `ON` `ServiceFacts` shape (`daemon_verified=True`, `answering=True`, no pending activation) returned by the setup stub's `refresh()`, not a verified history-link stub that would select remote Files routing. Keep the router's client disconnected (`state=None`) and make its `query()` raise `AssertionError("unexpected remote query")`; the local snapshot must answer every Files query. The setup stub exposes `facts:ServiceFacts`, `refresh()->ServiceFacts`, and fails on any privileged `request(...)`. Register the captured source under the manifest's `surviving_store` name and raw receipt source role in both router `_sources` and `_source_paths`; do not label an `iod` receipt as `gui` merely because the snapshot is now readable locally.

Use a test-only `monkeypatch.setattr(collector,"_current_window",lambda: requested_window)` to hold the manifest's selected range rather than the shell's default live since-boot window. This is only frozen UI-window selection: do not patch query, result, provenance, availability, or readiness functions. Stop periodic timers, call the real `refresh()`/readiness flow, show the actual `collector.files_view`, select the external key, and wait for its real asynchronous result. The status witness may establish the banner/settings answering labels but may not answer a Files query or replace the independent oracle. Retain the same collector instance, assert both labels and the setup facts, and hash the DB again after all queries/renders to assert unchanged bytes.

Add `test_real_snapshot_rejects_missing_hash_manifest_or_mismatch` using temporary file copies and a spy around SQLite connect; each invalid pin must fail **before opening**. Add `test_real_oracle_has_no_production_imports` using AST/import inspection and a deny-import execution control; importing production query/render/ownership helpers is a failure even if the JSON coincides. Missing required real artifacts fails collection/fixture explicitly, never skips.

- [ ] **Step 4: Capture, independently verify, falsify, and run the acceptance pin.**

Capture argv (only after the readable real input is supplied):

```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","tests/p4/capture_files_tab_snapshot.py","--source","/tmp/appio-files-tab-capture/iod.sqlite","--capture","/tmp/appio-files-tab-capture/capture.json","--output-dir","tests/fixtures/p4"]
```

Independent oracle verification argv:

```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","tests/p4/files_tab_real_oracle.py","--database","tests/fixtures/p4/files-tab-real.sqlite","--manifest","tests/fixtures/p4/files-tab-real.json","--output","tests/fixtures/p4/files-tab-real.oracle.json","--verify"]
```

**Validation (argv):**
```json
["/usr/bin/env","QT_QPA_PLATFORM=offscreen","PYTHONDONTWRITEBYTECODE=1","/data/home/henry-arch/.local/state/pi/project-manager/runs/ff4052076ce484e2c2a11776fd034304240f1c4457b5db14d60e70059561fa4a/pm-run-20261028-165501-f889f2e4/tooling/venv/bin/python","-m","pytest","-q","-p","no:cacheprovider","tests/p4/test_files_tab_real_acceptance.py"]
```

Run `M_real_acceptance_uses_requested_rows`, `M_real_served_is_requested_scalar`, and `M_real_ignores_provenance` against isolated production copies with this exact immutable snapshot and independent expected JSON. Each must fail its named rows/scope/count/provenance assertion; for a provenance-bypass mutation that cannot be killed by the positive real case alone, pair the mandatory synthetic fatal-refusal pins from 7/9 and **do not claim the positive snapshot killed it**. Separately falsify missing/hash-mismatch checks and production-derived-oracle detection. Never mutate real fixture bytes to invent a more convenient case; corrupt-hash tests use disposable copies only.

Record snapshot/capture/manifest/oracle digests, selected real AppKey, requested/served Windows, surviving source, independent reducer derivation, expected/actual row identities/bytes/footer text, label witness, targeted baseline/mutant/restored evidence, and before/after snapshot hash in the acceptance report. Report any blocker/refusal honestly. Do not run the full suite or root/live-BPF acceptance. Review the final bounded evidence against every §8 row; the frozen real acceptance is mandatory in addition to synthetic pins, not a replacement for them.

- [ ] **Step 5: Commit**

Only after the real input, hash/oracle verification, offscreen acceptance, and targeted falsification evidence pass, stage exactly this task's allowlist. If input is blocked, do not make a success/fixture commit. Exact successful commit message:

```text
test(files): accept honest scope against frozen real database
```
