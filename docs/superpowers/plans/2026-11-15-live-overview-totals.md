# appio Live Overview and Retained Totals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Leave the checkboxes unchanged; controller state tracks execution.

**Goal:** Keep accepted Overview measurements visible during forward refreshes, expose honest measurement age, and append mirrored curves while preserving accounting, source certification, and recovery ownership.

**Architecture:** Immutable neutral records and one pure selector validator feed client transport observations and typed local worker outcomes. A scope-owned router settlement writer publishes frame/detail/Files results; the structural view composes independent retained inputs and typed series metadata, while CollectorWindow owns source-driven preparation and the status/age choice. Charts maintain consecutive bucket groups and per-run cached curves and fills.

**Tech Stack:** Python >=3.14, PyQt6>=6.6, SQLite, standard-library dataclasses/Futures/executors/queues, pytest/pytest-qt, and the existing development-only mypy gate.

**Authority:** `docs/superpowers/specs/2026-11-15-live-overview-totals-spec.md`, approved revision 7, SHA-256 `526acaf2ce14aa8c13de530457d43efe5ff4583682d3c0a701a4a3931ae5c948`; approved design `docs/superpowers/specs/2026-11-15-live-overview-totals-design.md`, SHA-256 `8d6941b545ba15c9338f94df63d9d5c6b0b861e7cc7f14f8e85e2a675a6d70d9`. Inspected implementation baseline: `6624162ea8594021de74c0bae5ec26fedee36e30`. Section references below mean the approved specification unless explicitly prefixed “recovery”. Baseline source line ranges are navigation anchors; locate the named symbols again after earlier tasks land.

> **Epoch 2 recovery note:** This is a plan-only repair within the approved design/specification and the existing controller fence. The frozen task bodies remain unchanged except for Task 14's predecessor line. Tasks 17 and 18 are serial regression prerequisites for the existing acceptance sequence: they repair fixture ownership and settlement evidence before Task 14 is dispatched. This session adds no implementation, test, controller-state, design, specification, runtime/package dependency, or host-transition change.

## Global Constraints

- “No dependency, schema migration, sampling change, device-attribution relaxation, new scope control, privileged action, service repair, or host transition is part of this change.” (§1.2.) Preserve Python >=3.14 and PyQt6>=6.6 from `pyproject.toml`; do not edit its runtime dependencies.
- “No field or signature is added to `appio.contracts`, `appio.query.QueryService`, `QueryRequest`, the store, or any wire/durable record.” (§1.2.) Only the eight production modules in the file map below may change.
- “`history_types.py` imports only the standard library and `appio.contracts`.” “`history_validation.py` imports only the standard library, `appio.contracts`, `appio.ipc_models.QueryRequest`, and the neutral helpers.” (§1.2.) The view remains structural and imports no concrete router/store/producer/client. The renderer stays pure and may import neutral records/helpers.
- “Keep MAX_INFLIGHT == 1 for **wire-active historical requests**.” “Only `HistoryRouter._dispatch(self) -> None` calls `client.query` for router-owned historical work.” (§5.1.) Priority is ranking=0, selected series/totals=1, each Files page/coverage=2, already-supported other work=3, stable by admission serial, without preemption.
- “REQUEST_LIFETIME_NS stays `5_000_000_000`.” “NO_ANSWER_NS = `5_000_000_000`.” (§5.2.) Expire at `now >= issued_at_ns + REQUEST_LIFETIME_NS`; retire a still-expired connection at `now >= issued_at_ns + REQUEST_LIFETIME_NS + NO_ANSWER_NS`. Queue admission has no wire deadline. Future completion/cancellation never proves daemon termination.
- “Use a dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="appio-frame")` only for frame ranking/ownership copies.” “Use a different `ThreadPoolExecutor(max_workers=1, thread_name_prefix="appio-detail-files")` for lazy detail and existing local Files jobs.” (§3.3.) One charged frame Future; at most two charged detail Futures and eight charged Files Futures, ten background Futures total, including retired and completed-but-undrained jobs. Shutdown stays `wait=False, cancel_futures=True`.
- “`local_read_in_flight`” remains “`_local_future is not None and not _local_future.done()`.” (§3.1.) Never substitute `running()`. Callbacks only enqueue immutable outcomes; owner-thread drain observes registered connections, settles, reaps, and then notifies. No Qt-thread waits, sleeps, synchronous event processing, or worker-held router lock.
- “The **table/column hash inventory is empty**.” (§4.1.) Local position is exactly `(name, owner, registration_epoch, data_version, total_changes)` on each registered connection outside transactions, ordered by `name.encode("utf-8")`. Payload digesting is separate. No observer connection, latest-row scan, schema change, or writer callback.
- “A **current failure clears it**” before notification; a pending retry cannot restore it. (§§2.4, 3.4.) Ordinary preparation/sequence/counter advance preserves selected scope owners. Subject/scope/backward endpoint/authority retirement clears records and stamp lookup synchronously. Preserve original measured Window; exact getters never substitute an older retained record.
- Recovery-spec `docs/superpowers/specs/2026-11-03-silent-daemon-console-recovery-spec.md` §§4.4–4.5 remain unchanged: `_submit_prepare` owns admission and PrepareRetry; `_retire_pending` alone retires pending and arms the 30 s floor; `_apply_frame` alone applies. `_refresh_after_apply` is set only after committed local tick in `_apply_facts`, and consumed/invalidation occurs only at the end of `_apply_frame`. New presentation/source fields do not acquire recovery-field writers.
- Age/acceptance/transport deadlines use one injected MONOTONIC `age_clock_ns`; Window/recovery predicates use existing BOOTTIME `_clock_ns`. `CollectorWindow(..., age_clock_ns=time.monotonic_ns)` is additive. Production gives the same age clock to client, router, embedded view/adapter, and composer. (§9.1.) Never subtract clocks from different domains.
- Preserve exact A22 refusal precedence, direct Overview/Files cleanup, shell refusal frame preservation, file-only/empty-union selection, DEVICE whole-window policy, signed residuals, None versus zero, Files requested/served windows, served_gap_count None, page/cursor/snapshot/ownership proofs, probe arbitration, and persistent tabs. Only §11 A01–A18 expressly amended expectations may change.
- Tests use synthetic committed stores, controlled Events/Futures, an injected ns clock, real offscreen production widgets, and socketpair peers where transport matters. No real daemon, root, systemd operation, power/session transition, or wall-clock sleep is required. (§12.) Do not execute `reboot`, `shutdown`, `poweroff`, `halt`, `kexec`, any `systemctl` power/sleep verb, `init 0|6`, `telinit 0|6`, display-manager/graphical-target stop/restart, or any signal to the PM hosting process tree. If a host transition is concluded necessary, stop implementation and return `reboot-required.json` containing `{schema: 1, trigger, evidence: [{path, sha256}], provenance: {run_id, lane, attempt, dispatch_key, observed_boot_id}}` through the PM evidence channel; do not perform that transition.
- Run future implementation checks from the repository root with `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 PYTHONPATH="$PWD/src:$PWD/tests"`. Commands below use the resulting `python`. Do not install into the host interpreter or change dependencies; use the PM's development environment and record its actual versions. This planning task runs no application tests.
- Each task adds its named tests as executable assertions, runs its focused RED/GREEN cycle, and commits only its Files allowlist. If a required observation already passes, report a preservation control and prove the mechanism later with a disposable mutation; never invent an initial failure. Keep one top-level definition per required test name; later tasks extend it rather than creating duplicates. No skip/xfail, empty name-only tests, weakened ownership assertions, or import/collection errors counted as mutation kills.
- §§12.2–12.7 enumerate **91 distinct required test names**. Final acceptance is after Tasks 12 and 13: router/client discriminators in Task 14, view/status discriminators in Task 15, chart cost discriminators and full gates in Task 16. Earlier focused unit/contract checks establish the implementation; they do not claim the final integrated inventory ran early.

### File structure and write ownership

| File | Responsibility and owning tasks |
| --- | --- |
| `src/appio/history_types.py` (new) | Neutral immutable records, scope/endpoint/canonical local-answer digest: 1. |
| `src/appio/history_validation.py` (new) | Pure request/selector/nested chronology validation: 2. |
| `src/appio/iodclient.py` | Associated Future transport observation, tombstones, pre-cache validation, external subjects, versioned cache eviction: 3. |
| `src/appio/history_router.py` | Typed workers/capacity: 4; registered positions/brackets: 5; authority/detail/frame settlement and scheduler: 6; Files settlement integration: 7. |
| `src/appio/ui/render_model.py` | Provenance/metadata/availability: 8; status/refusal/age records and age function: 11. |
| `src/appio/ui/main_window.py` | Retained capability/exact adapter/query-free observation: 9; refusal/measurement/age presentation: 11; append callers: 13. |
| `src/appio/ui/charts.py` | Append decision table, aggregate/run caches, curves and closed fills: 10. |
| `src/appio/ui/collector_window.py` | Typed refusal transitions: 11; source-driven gate/apply/poll, one composer, production age-clock wiring: 12. |
| `tests/live_overview_support.py` (new) | Qt-free Clock/contract factories in 1, complete production integration harness in 13; import with the tests directory on PYTHONPATH. |
| `tests/live_overview_transport.py` (new) | Real client/socketpair peer and controlled historical job fixture: 3. |
| `tests/p3/test_live_overview_router.py` (new) | §12.2 and first four §12.4 tests: 1, 4–7, 14. |
| `tests/p3/test_live_overview_client.py` (new) | §12.3 tests, pure selector controls first: 2, 3, 7, 14. |
| `tests/test_ui.py` | §12.5 and first three §12.7 tests: 8, 9, 11, 13, 15. |
| `tests/test_charts.py` | §12.6 tests and amended grouping/shape controls: 10, 16. |
| `tests/p3/test_collector_window.py` | Last eight §12.4 and last ten §12.7 tests, existing recovery controls: 11, 12, 15. |
| Existing `tests/p3/test_iodclient.py`, `tests/p3/test_history_router.py`, `tests/p4/test_file_history_router.py`, `tests/p4/test_files_tab_local_result.py`, `tests/p4/test_files_tab_remote_result.py` | Adapt existing fixtures to the new transport/lazy delivery shape, preserving all substantive assertions: 3, 6, 7 respectively. |

### Shared consumed interfaces carried in every task brief

These are signature/type references for consumers; their implementing tasks below own validation and code. A task never needs another task's private brief to learn a neighbour's signature. All neutral record field schemas are §2; each consumer below repeats the fields it uses. `PartKind=Literal["totals","series"]`, `AnswerKind=Literal["ranking","totals","series"]`, `PartPhase=Literal["absent","pending","ok","failed"]`; `RetentionScope` is `(key,"range",boot,start,end) | (key,"since_boot",boot) | (key,"lifetime") | (key,"launch")`; `SourcePosition=RemotePosition(boot_id,iod_epoch,seq) | LocalPositionVector(sources: tuple[LocalPosition,...])` with `LocalPosition(name,owner,registration_epoch,data_version,total_changes)`. All are frozen/validated with the types specified in Task 1 and §§2.1–2.3; no mutable request/context crosses an owner boundary.

| Consumer boundary | Exact callable/result contract |
| --- | --- |
| Pure helpers | `retention_scope(app_key: str, window: Window) -> RetentionScope`; `applied_endpoint(window: Window) -> int | None`; `local_answer_digest(kind: AnswerKind, app_key: str, window: Window, value: AppTotal | DiskShare | Series) -> str`; `validate_query_result(request: QueryRequest, response: Mapping[str, object]) -> None`; local `_normalize_request(request: QueryRequest) -> QueryRequest`. |
| Client transport | `query(request: QueryRequest) -> Future[dict]`; `query_transport(future: Future[dict]) -> QueryTransportState`; `retire_query_connection(future: Future[dict]) -> bool`; `reject_query_result(future: Future[dict]) -> None`. QueryTransportState fields exactly `request`, `connection_generation`, `request_id`, `phase`, `issued_at_ns`, `issued_source`, `answered_source` as typed in §5.2/Task 3. |
| Router observation | `prepare(window: Window) -> None`; `ready(window: Window) -> bool`; `drain() -> bool`; `source_position() -> SourcePosition`; `local_position(source_name: str) -> LocalPosition`; `preparation_source(window: Window) -> SourcePosition | None`; `frame_provenance(window: Window) -> FrameProvenance | None`. Read-only `pending_work: bool` and `local_read_in_flight: bool`. |
| Structural retained capability | `select_detail(app_key: str | None, window: Window | None) -> None`; `request_detail(kind: PartKind, app_id: int, window: Window) -> PartState`; `part_state(kind: PartKind, app_key: str, scope: RetentionScope) -> PartState`; `retained(kind: PartKind, app_key: str, scope: RetentionScope, applied_endpoint: int | None) -> RetainedRecord | None`; `ranking_provenance(app_key: str, window: Window) -> AnswerProvenance | None`. Exact-only optional `exact_answer_identity(kind: AnswerKind, app_key: str, window: Window) -> AcceptedIdentity | None`; exact required methods remain ranking/series/disk_share from §6.2. |
| View and shell | `reset_query_authority() -> None`; `refresh_measurements() -> None`; `measurement_status() -> MeasurementStatus`; `show_overview_age(text: str | None) -> None`; `apply_ready_frame(*, now: int | None = None, context: Literal["ordinary","poll"]="ordinary") -> bool`; `_compose_overview_status_choice(now_ns: int, age_now_ns: int) -> OverviewStatusChoice`; text wrapper/updater capture one age instant. |
| Chart consumer | `set_task_points(points: Sequence[SeriesPoint], *, identity: object=None) -> None`; `append_task_points(points: Sequence[SeriesPoint], *, identity: object) -> None`; device setters/appends substitute DeviceSeriesPoint and their exact method names; `columns() -> tuple[ChartColumn,...]`, `curves() -> tuple[ChartCurve,...]`, `segments() -> tuple[ChartSegment,...]`. |
| Test transport | `QueryTransportHarness.query(request: QueryRequest) -> Future[dict]`; `only_future(kind: str) -> Future[dict]`; `reply(future: Future[dict], value: object, *, snapshot: str, next_cursor: str | None=None) -> None`; `error(future: Future[dict], reason: str, text: str) -> None`; `pump() -> None`; `advance_ns(delta: int) -> None`; `stream(seq: int) -> None`; `disconnect() -> None`; actual `observed_historical_occupants: list[int]`, `query_kinds: list[str]`. |
| Test live integration | `LiveOverviewHarness.apply(window: Window) -> None`; `ordinary_refresh() -> None`; `poll() -> None`; `notify() -> None`; `request(window: Window) -> None`; `advance_ns(delta: int, *, domain: Literal["age","recovery","both"]="both") -> None`; `hold(kind: Literal["ranking","series","totals","files","coverage"]) -> None`; `release(kind: Literal["ranking","series","totals","files","coverage"]) -> None`; `block_detail_computation()` and `block_local_delivery()` return context managers. Test-only `.router`, `.view`, `.shell`, `.transport`, actual event/submission/charge spies and registered-store barriers; no record/gate fabrication. |

### Scheduler dependencies and concurrency

Each task has an exact `Lane` and explicit predecessor list. Same-lane task order and all explicit dependencies are mandatory. Do not infer dependency from task number alone. After foundation 1→2, Tasks 3 (`transport`) and 4 (`router`) may overlap; their write sets are disjoint. Task 5 waits for both. Router 5→6→7 precedes presentation 8→9. After 9, chart Task 10 can overlap presentation Task 11. Tasks 12 and 13 both wait for 10 and 11, then can overlap because 13 writes main_window/UI/support files while 12 writes only collector_window/collector tests. Task 14 waits for both 12 and 13, then acceptance 14→15→16 is serial. Those are the only parallel groups; tasks in different lanes with unmet dependencies remain blocked. Fixtures and protected source files outside each write set are read-only inputs. Final discriminator fixes stay in their explicitly listed production allowlist and required contract, without editing the design/specification/plan.

## Task 1: Define neutral retained-answer and source records

**Implementer tier:** Advanced

**Lane:** `foundation`
**Depends on:** none
**Specification:** §§1.2, 2.1–2.4; §12.2 `test_live_records_validate_every_field_and_phase_combination`.

**Files:**

- Create: `src/appio/history_types.py`
- Create: `tests/live_overview_support.py`
- Create/Test: `tests/p3/test_live_overview_router.py`

**Interfaces:**

- Consumes existing immutable `Window(kind, boot_id, start_mono_ns, end_mono_ns, launch_id)`, AppTotal, DiskShare, Series, Coverage, Tier from `appio.contracts`.
- Produces `PartKind = Literal["totals", "series"]`, `AnswerKind = Literal["ranking", "totals", "series"]`, `PartPhase = Literal["absent", "pending", "ok", "failed"]` and the exact RetentionScope union below.
- Produces `retention_scope(app_key: str, window: Window) -> RetentionScope`, `applied_endpoint(window: Window) -> int | None`, `local_answer_digest(kind: AnswerKind, app_key: str, window: Window, value: AppTotal | DiskShare | Series) -> str`.
- Produces the following **frozen dataclasses with exactly these fields**, each with validating `__post_init__(self) -> None`; these signatures are the interface for every later task:

```python
RetentionScope = (tuple[str, Literal["range"], str, int, int]
                  | tuple[str, Literal["since_boot"], str]
                  | tuple[str, Literal["lifetime"]]
                  | tuple[str, Literal["launch"]])
# name(field: type, ...) specifies dataclass fields in order, not a new factory.
# AcceptedIdentity(kind: AnswerKind, app_key: str, window: Window, data_digest: str)
# RemotePosition(boot_id: str, iod_epoch: int, seq: int)
# LocalPosition(name: str, owner: Literal["gui", "iod"], registration_epoch: int,
#               data_version: int, total_changes: int)
# LocalPositionVector(sources: tuple[LocalPosition, ...])
# LocalBracket(opening: LocalPositionVector, closing: LocalPositionVector)
SourcePosition = RemotePosition | LocalPositionVector
# AnswerProvenance(kind: AnswerKind, app_key: str, scope: RetentionScope,
#                  measured_window: Window, accepted_identity: AcceptedIdentity | None,
#                  first_seen_at_ns: int | None, answered_source: SourcePosition | None,
#                  local_bracket: LocalBracket | None)
# RetainedRecord(value: DiskShare | Series, provenance: AnswerProvenance)
# PartState(kind: PartKind, app_key: str, scope: RetentionScope, state: PartPhase,
#           request_id: int | None, requested_window: Window | None,
#           record: RetainedRecord | None, reason: str | None)
# FrameProvenance(generation: int, window: Window, requested_source: SourcePosition,
#                 answered_source: SourcePosition | None, local_bracket: LocalBracket | None,
#                 certified: bool)
# SeriesMetadata(app_id: int, app_key: str, measured_window: Window, tier: Tier,
#                coverage: Coverage)
```

- Test-only support produces `Clock(ns: int = 0)` with callable current ns and `advance_ns(delta: int) -> None`, plus `coverage(**changes) -> Coverage`, `total(app_id: int, key: str, *, read: int = 17, write: int = 5, peak: int | None = None) -> AppTotal`, `share(app_id: int, window: Window, *, read: int = 17, write: int = 5, device: bool = True) -> DiskShare`, and `series(app_id: int, window: Window, *, tier: Tier = "raw") -> Series`. Use existing frozen contract fields, a valid `unit:alpha.service` key, and valid matching device rows/residuals; do not import Qt in these factories.

- [ ] **Step 1: Add field/phase and canonical-digest assertions.** Define `test_live_records_validate_every_field_and_phase_combination` once. Parameterize every field's wrong type, bool-as-int, negative/below-minimum, wrong enum/UTF-8/digest, nested corruption, wrong scope/window/subject, and all absent/pending/ok/failed combinations. Include empty/duplicate/unsorted vector controls (an open-router vector must contain its primary source), unequal-bracket evidence, SeriesMetadata tier/coverage disagreement, immutable-field inventory and frozen assignment rejection. Do not add fields to frozen contracts. Start with this concrete assertion body and add the field matrix to the same test:

```python
w = Window("since_boot", "boot-a", end_mono_ns=20_000_000_000)
scope = retention_scope("unit:alpha.service", w)
assert scope == ("unit:alpha.service", "since_boot", "boot-a")
assert applied_endpoint(w) == 20_000_000_000
assert applied_endpoint(Window("lifetime")) is None
p = RemotePosition("boot-a", 1, 0)
with pytest.raises(ValueError):
    RemotePosition("boot-a", True, 0)
with pytest.raises(ValueError):
    PartState("totals", "unit:alpha.service", scope, "pending", 1, w, None, "bad")
a = share(7, w)
assert local_answer_digest("totals", "unit:alpha.service", w, a) == local_answer_digest(
    "totals", "unit:alpha.service", w, replace(a, app_id=91))
assert local_answer_digest("totals", "unit:alpha.service", w, a) != local_answer_digest(
    "totals", "unit:alpha.service", w, replace(a, task_read_bytes=18, task_total_bytes=23))
```

- [ ] **Step 2: Run** `python -m pytest -q tests/p3/test_live_overview_router.py::test_live_records_validate_every_field_and_phase_combination`; confirm the missing neutral API is the RED cause.
- [ ] **Step 3: Implement the exact record schema and truth tables.** Validate with ValueError, tuple collections, exact bools, nonempty UTF-8 text; Python nonnegative integer counters exclude bool and have no SQLite ceiling. Remote boot text follows StreamCursor's UTF-8 byte bound 256 and boot validation without importing ipc_models. Epoch/registration/data_version >=1; seq/total_changes/stamps >=0. Identity digest is `[0-9a-f]{64}` and includes the complete five-field Window. Validate nested frozen objects again; a forged object with changed fields is not proof of validity. RetainedRecord permits totals/DiskShare or series/Series only, positive presentation ID, matching provenance Window; slot record is present only for ok; pending/failed retain their token's exact Window. Answer identity/stamp are paired, local bracket equality/source rules hold, and FrameProvenance certification is exact source equality with successful completed ranking supplied by its producer.
- [ ] **Step 4: Implement scope and payload hashing.** Preserve range's complete tuple, since_boot's boot, and boot-less lifetime/launch tuples. Revalidate kind/value/external-key/exact-Window binding before encoding. Canonical recursion uses dataclass field objects, ordered arrays, explicit None and removes only presentation `app_id` recursively; no sorting of points. AppTotal retains and must match app_key. Use this exact encoding, with finite numeric/nested validation before hashing:

```python
payload = ["appio-local-answer-v1", kind, app_key, encoded_window, encoded_value]
data_digest = hashlib.sha256(json.dumps(
    payload, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False, allow_nan=False,
).encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Run the same test GREEN**, including direct nested corruption and digest vectors for coverage, optional peaks/None, suspend, device and residual changes. Confirm importing the neutral module imports no client/router/query/Qt. Task 4 extends this same named test for private router records.
- [ ] **Step 6: Commit.**

```bash
git add src/appio/history_types.py tests/live_overview_support.py tests/p3/test_live_overview_router.py
git commit -m "feat(history): define immutable retained answer and source records"
```

## Task 2: Establish shared pure selector validation

**Implementer tier:** Advanced

**Lane:** `foundation`
**Depends on:** Task 1
**Specification:** §§1.2, 5.3–5.4 (D05/D06); §12.3 `test_each_coverage_selector_accepts_only_its_contract_shape`, `test_series_accepts_simultaneous_devices_and_rejects_per_device_overlap_or_order`.

**Files:**

- Create: `src/appio/history_validation.py`
- Create/Test: `tests/p3/test_live_overview_client.py`

**Interfaces:**

- Consumes `QueryRequest(kind, window, app_key=None, metric="total_bytes", sort="bytes_total", direction="desc", search=None, cursor=None, limit=500, coverage_kind="process")`, contract values, and `retention_scope(app_key: str, window: Window) -> RetentionScope` / `local_answer_digest(kind, app_key, window, value) -> str` from the neutral module.
- Produces `validate_query_result(request: QueryRequest, response: Mapping[str, object]) -> None`, raising ValueError for shape/nested/selector failure. This is the **same function** used by client pre-cache and remote router settlement.
- Produces private pure `_normalize_request(request: QueryRequest) -> QueryRequest` for local worker admission; mirror the existing `_query_wire` selector defaults/validation without I/O or a client import. A22 prechecks precede subject/selector resolution. Client remote normalization still comes from its validated wire/default path.
- Optional private selector-value helpers accept the actual local value/key/request directly; local callers must not manufacture a remote envelope/snapshot. Keep these helpers in this module, with the same chronology predicates as validate_query_result.

- [ ] **Step 1: Add the two exact tests above with pure positive/negative matrices.** Coverage cases are scoped Coverage, unscoped AppKey→Coverage mapping, file FileCoverage and literal `overflow_owner_unknown` UnknownOwnerOverflow. Test incompatible union alternatives independently. The independent mapping predicates must be observable:

```python
request = QueryRequest("coverage", Window("lifetime"))
valid = {"kind": "coverage", "snapshot": "a" * 64, "next_cursor": None,
         "result": {"unit:alpha.service": coverage()}}
validate_query_result(request, valid)
for mapping in ({"unit:/alpha.service": coverage()},
                {"unit:alpha.service": "not a Coverage"}):
    with pytest.raises(ValueError):
        validate_query_result(request, {**valid, "result": mapping})
```

For series, construct the exact raw task buckets 0/10_000_000_000 with 10-second durations and ordered device tuple `(0,"8:0"),(0,"8:16"),(10_000_000_000,"8:0"),(10_000_000_000,"8:16")`, nonzero integer bytes. Assert unchanged acceptance. Independently duplicate `(0,"8:0")`, insert same-dev start 5_000_000_000, swap the first two device entries, reverse bucket order, mismatch tier/coverage or tier duration, introduce NaN wall time, and paginate series; each raises ValueError. Task 14 adds real local/remote-route assertions to these same tests.
- [ ] **Step 2: Run** `python -m pytest -q tests/p3/test_live_overview_client.py`; confirm missing shared validator is RED.
- [ ] **Step 3: Implement exact request and result predicates.** Revalidate all contract fields recursively; require mapping kind=request.kind, legal snapshot/continuation where the selector requires them, exact external key/Window for totals/series/Files, no continuation for scalar coverage/totals/series. Ranking is a list of valid AppTotal with unique canonical external keys; its nonempty/mixed-store/pagination ownership refusal remains contextual in the router. Validate unscoped process values **as Coverage separately from validating each key**. Scoped process coverage has no wire key/Window echo; association supplies them. Preserve file served-interval/unknown-owner forms and do not require an exact requested Window where the protocol uses a served Window. Page-owner snapshot/history checks remain in settlement.
- [ ] **Step 4: Keep the import boundary and external-key grammar exact.** The approved imports exclude `appio.identity`; implement a private pure canonical AppKey check here using its existing byte grammar: prefixes unit/desktop/exe/scope, nonempty payload, literal safe bytes `A-Z a-z 0-9 /._:@+-`, uppercase `%HH` only for non-safe bytes, valid decoded UTF-8, exe/scope decoded absolute paths, unit/desktop no decoded slash. Do not change identity.py. Mirror selector applicability/defaults and range/launch refusals in the private local normalizer; never widen accepted selectors or silently discard nondefault irrelevant fields.
- [ ] **Step 5: Implement chronology without rejecting simultaneous devices.** Validate task/residual starts strictly increasing and nonoverlap separately; tier raw/rollup_60s/rollup_3600s requires 10/60/3600 seconds respectively, totals_only has all three sequences empty, and series.tier equals coverage.tier. Device global ordering is `(start, dev.encode("utf-8"))`; nonoverlap is **per dev**, using a last-end map:

```python
last_order = None
last_end_by_dev = {}
for point in value.device_points:
    order = (point.bucket_start_mono_ns, point.dev.encode("utf-8"))
    if last_order is not None and order <= last_order:
        raise ValueError("invalid_result")
    if point.bucket_start_mono_ns < last_end_by_dev.get(point.dev, 0):
        raise ValueError("invalid_result")
    last_order = order
    last_end_by_dev[point.dev] = point.bucket_start_mono_ns + point.bucket_secs * 10**9
```

Do not sort malformed input or invent task/device byte correspondence. Local Files validation uses the actual combined table/coverage/served evidence; remote owner checks remain separate.
- [ ] **Step 6: Run the same tests GREEN**, with the valid mapping as a passing control for **each** malformed mapping. These are foundation checks, not a claim that client cache or router publication has been tested yet.
- [ ] **Step 7: Commit.**

```bash
git add src/appio/history_validation.py tests/p3/test_live_overview_client.py
git commit -m "feat(history): validate selector results through one pure boundary"
```

## Task 3: Bind client Futures to transport and validated cache entries

**Implementer tier:** Capable

**Lane:** `transport`
**Depends on:** Task 2
**Specification:** §§5.1–5.4; §12.3 `test_cache_and_refusal_futures_keep_immutable_request_association`, `test_full_cursor_cache_key_and_original_answered_source`, `test_totals_and_series_decode_preserve_external_subject`. Final timeout/malformed-retry discriminators are Task 14.

**Files:**

- Modify: `src/appio/iodclient.py:507-599` (`_query_wire`/normalization), `src/appio/iodclient.py:642-910` (pending/cache/query surface), `src/appio/iodclient.py:1014-1027` (drop), `src/appio/iodclient.py:1159-1310` (result/error/expiry/cache)
- Create: `tests/live_overview_transport.py`
- Modify/Test: `tests/p3/test_live_overview_client.py` (Task 2 file)
- Modify/Test: `tests/p3/test_iodclient.py:59-250` (wire fixture helpers and Client harness; retain existing substantive controls through line 1076)

**Interfaces:**

- Consumes `validate_query_result(request: QueryRequest, response: Mapping[str, object]) -> None`, `RemotePosition(boot_id: str, iod_epoch: int, seq: int)`, the existing `IodClient.query(request: QueryRequest) -> Future[dict]`, generation, byte-budget LRU and connection-drop/backoff mechanisms.
- Produces frozen `QueryTransportState(request: QueryRequest, connection_generation: int, request_id: str | None, phase: Literal["not_sent", "active", "expired", "terminal", "lost"], issued_at_ns: int | None, issued_source: RemotePosition | None, answered_source: RemotePosition | None)` with validation and exactly these fields.
- Produces `IodClient.query_transport(future: Future[dict]) -> QueryTransportState`, `retire_query_connection(future: Future[dict]) -> bool`, `reject_query_result(future: Future[dict]) -> None`. Unknown Futures raise ValueError; stale/terminal/never-sent retirement returns False.
- Test fixture `QueryTransportHarness` owns a real IodClient, socketpair peer, injected Clock, wire-envelope count, connection generations and held-job state. Its API is `query(request) -> Future[dict]`, `reply(future, value, *, snapshot: str, next_cursor: str | None = None) -> None`, `error(future, reason: str, text: str) -> None`, `pump() -> None`, `advance_ns(delta: int) -> None`, `stream(seq: int) -> None`, and `disconnect() -> None`. `reply` encodes real wire payloads, never calls a client private result setter; `only_future(kind: str) -> Future[dict]` returns that kind's single currently held associated Future for test control. `observed_historical_occupants: list[int]` and `query_kinds: list[str]` record actual peer job occupancy and received query envelopes. One peer historical job remains occupied after client expiry. It supplies valid root peer credentials only through the injected peer_reader, with no service invocation.

- [ ] **Step 1: Add association/cache/subject tests and real socketpair fixture.** Preserve canonical complete query/defaults. Observe association on immediate cache/refusal outcomes, including closed/unavailable/disabled request Futures, before callbacks can fire. Assert unknown Future rejection; cache hit is terminal with no request_id/issue instant and original answered_source; local refusal is not_sent with none. For actual totals/series decode, give decoder IDs different from router presentation IDs and assert external app_key survives. Use 64-lowercase-hex snapshots for accepted identities; update old fixture literals/shapes to valid wire selectors without deleting timeout/LRU/authority assertions.

```python
f = transport.query(QueryRequest("series", w, "unit:alpha.service"))
issued = transport.client.query_transport(f)
assert issued.phase == "active"
assert issued.request == QueryRequest("series", w, "unit:alpha.service")
transport.reply(f, series(91, w), snapshot="a" * 64)
assert transport.client.query_transport(f).phase == "terminal"
assert f.result()["app_key"] == "unit:alpha.service"
assert transport.client.query_transport(f).answered_source == issued.issued_source
```

- [ ] **Step 2: Run** `python -m pytest -q tests/p3/test_live_overview_client.py -k 'cache_and_refusal or full_cursor or decode_preserve'`; confirm the missing transport association/metadata assertions fail.
- [ ] **Step 3: Implement normalized request association and total transport phases.** Bind each returned Future before an immediate completion, reconstruct the immutable normalized QueryRequest from validated wire/default values, and retain the exact issue cursor/generation/time. Keep sent bindings strongly until real terminal/lost observation; weak Future associations may outlive cache eviction. QueryTransportState validation forbids sent phases without ID/time, fabricated source on failed/refused outcomes, or changing issue association after a reply. On daemon envelope receipt set terminal **before** Future settlement/wake; on real socket detach set lost before reentrant callbacks. Cancellation leaves transport active; expiry settles once and changes active→expired while retaining a request-id tombstone. Late error/result consumes that tombstone without decoding into success/cache or resettling the Future.
- [ ] **Step 4: Implement request-specific guarded connection retirement.** Only the currently carrying active/expired sent Future may invoke `_drop(now, "daemon_not_answering")`; close/detach before a successor is possible, clear its connection cache and preserve backoff/reverification. A terminal/stale-generation Future cannot affect a new socket. The client uses MONOTONIC time throughout; the router will apply the inclusive request-specific deadline.
- [ ] **Step 5: Implement pre-cache validation and entry-version eviction.** Cache key is `(canonical_complete_query_json, (boot_id, iod_epoch, seq) | None)`. Store original normalized request, original answered_source and a unique entry version alongside existing value/size; preserve whole-entry LRU/bytes. Decode external subjects for DiskShare/Series as for FileTable/FileCoverage. Validate the full response against pending.request before `_cache_put`/Future success:

```python
try:
    validate_query_result(pending.request, value)
except ValueError:
    _settle(pending.future, error=RequestRejected("invalid_result", "invalid_result"))
else:
    self._cache_put(pending.key, value, size)
    _settle(pending.future, value=dict(value))
```

Integrate this inside the existing live generation/request-id/result-decoder association and terminal bookkeeping, not as a second decode path. Preserve existing decoder RemoteError classification. Revalidate cached original request/value on hit; evict corruption and send a miss. `reject_query_result(future)` evicts only that exact Future's produced/returned entry **version**, never a later same-key entry; no-op for known uncached/evicted result. Expired/cancelled/lost/misassociated replies insert nothing.
- [ ] **Step 6: Run the targeted new tests and** `python -m pytest -q tests/p3/test_iodclient.py`. Record preserved LRU and five-second behavior; do not claim a done Future means a router slot was released. Task 14 supplies that integrated discriminator.
- [ ] **Step 7: Commit.**

```bash
git add src/appio/iodclient.py tests/live_overview_transport.py tests/p3/test_live_overview_client.py tests/p3/test_iodclient.py
git commit -m "feat(client): track historical transport lifetime and validate cache admission"
```

## Task 4: Split typed local workers and independent execution capacity

**Implementer tier:** Capable

**Lane:** `router`
**Depends on:** Task 2
**Specification:** §§3.3–3.4 (D02/D03), 5.4; §12.2 `test_live_records_validate_every_field_and_phase_combination`, `test_local_detail_is_lazy_selected_subject_only`. Owner settlement and full capacity/liveness acceptance follow in Tasks 6–7 and 14.

**Files:**

- Modify: `src/appio/history_router.py:17-169` (imports/private records/executors), `src/appio/history_router.py:288-402` (local Files worker outcome), `src/appio/history_router.py:501-512` (close), `src/appio/history_router.py:668-868` (materialization/read decomposition), `src/appio/history_router.py:1066-1150` (private local reducers)
- Modify/Test: `tests/p3/test_live_overview_router.py`

**Interfaces:**

- Consumes `_normalize_request(request: QueryRequest) -> QueryRequest`, pure local selector predicates, `local_answer_digest(kind: AnswerKind, app_key: str, window: Window, value: AppTotal | DiskShare | Series) -> str`; existing HistorySegment and FileViewResult(table, coverage, served_gap_count), ownership planner/retraction/reducers.
- Produces private **frozen dataclasses with exactly the fields in §3.4**:

```python
# RequestToken(token_id: int, request: QueryRequest, origin: Literal["remote", "local"],
#              scope: RetentionScope | None, owner: Literal["frame", "detail", "files"],
#              owner_id: int, slot_id: int | None, authority: tuple,
#              requested_source: SourcePosition)
# LocalFrameOutcome(worker_id: int, request: QueryRequest, ranking: tuple[AppTotal, ...],
#   segments: tuple[HistorySegment, ...], boundaries: tuple[tuple[str, int, str, str], ...],
#   gaps: tuple[tuple[str, int, int], ...], known_gaps: tuple[tuple[str, int, int], ...],
#   overlaps: tuple[tuple[str, int, int], ...], has_local_history: bool, not_retained: bool,
#   chronology_unproved: bool, file_available: bool)
# LocalPartOutcome(worker_id: int, request: QueryRequest, app_key: str,
#                  value: DiskShare | Series, data_digest: str)
# LocalFilesOutcome(worker_id: int, request: QueryRequest, app_key: str,
#                   table: FileTable, coverage: FileCoverage, served_gap_count: int | None)
LocalOutcome = LocalFrameOutcome | LocalPartOutcome | LocalFilesOutcome
# RemoteSettlement(future: Future[dict], response: Mapping[str, object] | None = None,
#                  error: Exception | None = None)
# LocalSettlement(worker_id: int, outcome: LocalOutcome | None = None,
#                 error: Exception | None = None, local_bracket: LocalBracket | None = None)
SettlementInput = RemoteSettlement | LocalSettlement
```

- Private worker inputs are immutable path registrations `tuple[tuple[str, str, str], ...]` (source name/canonical path/owner), normalized request and worker_id. Pin signatures `_read_local(paths, window: Window, *, worker_id: int, request: QueryRequest) -> LocalFrameOutcome`, `_read_local_part(paths, request: QueryRequest, *, worker_id: int, app_key: str) -> LocalPartOutcome`, and `_read_local_files(paths, window, key, app_id, sort, direction, search, *, worker_id: int, request: QueryRequest) -> LocalFilesOutcome` for the owner integration in Tasks 5–7. Keep numeric ID remapping outside external validation; Files request app_id remains presentation-only.

- [ ] **Step 1: Extend the existing record test for these exact private variants.** Wrong worker/request, mutable collections, wrong result variant, mismatched key/Window/digest, bool flags and malformed interval evidence must be rejected. Validate tuple/nested contracts, unique ranking external AppKeys and exact bool flags. Every boundary `(boot, at_ns, from_owner, to_owner)` needs a nonempty boot, nonnegative nonbool timestamp and gui/iod owners supported by adjacent proven segments; gap/overlap intervals are nonempty half-open with nonempty boots. Preserve chronology ordering and allow overlapping known-gap evidence from distinct sources. Add the exact lazy test using synthetic multi-subject stores and spies on actual QueryService series/disk_share computations: frame worker produces no parts; a selected worker computes one requested kind/key, resolving each private store's ID by AppKey rather than registry numeric ID.

```python
frame = HistoryRouter._read_local(paths, w, worker_id=1,
                                  request=QueryRequest("ranking", w))
assert isinstance(frame, LocalFrameOutcome)
assert not hasattr(frame, "parts")
assert detail_computations == []
part = HistoryRouter._read_local_part(
    paths, QueryRequest("totals", w, "unit:alpha.service"),
    worker_id=2, app_key="unit:alpha.service")
assert part.worker_id == 2 and part.value.window == w
assert part.data_digest == local_answer_digest("totals", part.app_key, w, part.value)
assert detail_computations == [("totals", "unit:alpha.service", w)]
```

Here `paths` and `detail_computations` are fixture-owned committed source paths and actual reducer spies; they are not router production fields.
- [ ] **Step 2: Run** `python -m pytest -q tests/p3/test_live_overview_router.py -k 'live_records or local_detail_is_lazy'`; confirm eager parts/untyped outcome assertions fail.
- [ ] **Step 3: Extract worker-only computations.** Preserve `_plan`, `_materialize`, within-boot order, gap/overlap/boundary evidence, merge/tier/peak/residual/coverage reducers. Frame copy returns **all** copied routing evidence including known_gaps and file availability, never eager detail/database handles. Detail snapshots privately plan/materialize, resolve external key independently in each private source and compute only one kind. Synchronous internal reducers remain usable inside workers; UI exact getters will use owner settlement in Task 6. Local Files returns typed table/coverage/served_gap evidence after its existing fatal-versus-skippable candidate/provenance proofs. Always close worker connections privately.
- [ ] **Step 4: Create the two independent executors and charged-job bookkeeping.** Frame executor is only for `_local_future`; background executor accepts lazy detail and Files. Track submitted Future bindings until owner drain reaps terminal completion; do not release counts from done callbacks or on publication retirement. At admission reserve one slot/kind, two detail max, eight Files max, ten combined, with retired jobs included. Current detail without capacity remains logically pending with one latest successor; do not submit unbounded stale work. A callback's only operation after constructing its immutable result/error wrapper is:

```python
self._completed.put((token, settlement_input))
```

Use `queue.SimpleQueue`; callback code never reads registered SQLite connections or mutates maps/status/Futures/widgets. Owner drain wiring is Task 6. Frame/background execution primitives must already be independently testable with a held real computation; full apply assertion is reserved for Task 14.
- [ ] **Step 5: Run the focused tests GREEN.** Verify the actual frame worker finishes while a background computation Event is held, without releasing that Event first; test-side bounded Event waits are synchronization, never GUI production waits. Close uses both executors' nonblocking shutdown and retires queued publications. No task here adds a view notification or an alternate publication writer.
- [ ] **Step 6: Commit.**

```bash
git add src/appio/history_router.py tests/p3/test_live_overview_router.py
git commit -m "refactor(history): return typed lazy worker outcomes on independent lanes"
```

## Task 5: Observe registered local positions and bind copy brackets

**Implementer tier:** Advanced

**Lane:** `router`
**Depends on:** Task 3, Task 4
**Specification:** §§2.2, 4.1–4.3; §12.4 first four exact tests listed below. Frame replacement after bracket races is completed in Tasks 6/12/14.

**Files:**

- Modify: `src/appio/history_router.py:131-169` (registration/binding state), `src/appio/history_router.py:465-482` (source install/invalidation), `src/appio/history_router.py:738-855` (opening/closing observation admission/drain seams)
- Modify/Test: `tests/p3/test_live_overview_router.py`

**Interfaces:**

- Consumes LocalPosition(name, owner, registration_epoch, data_version, total_changes), LocalPositionVector(sources), LocalBracket(opening, closing); normalized QueryRequest and typed LocalFrame/Part/FilesOutcome from Task 4.
- Produces `HistoryRouter.local_position(source_name: str) -> LocalPosition`, `source_position() -> SourcePosition` and immutable registered-source/worker bindings `(worker_id, token_id, request, authority, captured registrations/paths, opening_vector)`. source_position returns RemotePosition(full cursor) only for verified answering connection **with** cursor, otherwise full local vector.
- Opening/closing owner observations for frame/detail are outside transactions. Closing observation creates `LocalSettlement(worker_id, outcome, error, local_bracket)`; Files retains local_bracket=None and existing snapshot/served proofs. No observation publishes owner data.

**Exact tests to add:** `test_local_vector_observes_each_registered_connection_without_hashing`; `test_local_vector_changes_for_every_existing_commit_path`; `test_local_registration_add_remove_replace_and_reopen_never_repeat_token`; `test_local_observations_require_committed_registered_connections`.

- [ ] **Step 1: Add LocalPositionStores fixtures locally in the router test file.** Two file-backed stores have persistent registered QueryService connections and separate external-writer connections. Trace SQL and reject table/hash/rowid reads during `local_position`; assertions compare actual PRAGMA/property values, not fake counters. Include both own-connection and external commits, old-row and rollup deletion, collector_epoch update, handover/receipt update, non-primary-only writes, source removal/re-add, same-name replacement/reopen and physical path rebinding.

```python
first = history.local_position("local")
assert first.data_version == local.connection.execute("PRAGMA main.data_version").fetchone()[0]
assert first.total_changes == local.connection.total_changes
external.execute("UPDATE collector_epoch SET epoch=epoch+1")
external.commit()
second = history.local_position("local")
assert second.registration_epoch == first.registration_epoch
assert second != first
local.connection.execute("BEGIN")
with pytest.raises(ValueError, match="history source has an uncommitted transaction"):
    history.local_position("local")
assert local.connection.in_transaction
local.connection.rollback()  # Fixture cleanup, never production observer behavior.
```

- [ ] **Step 2: Run** `python -m pytest -q tests/p3/test_live_overview_router.py -k 'local_vector or local_registration or local_observations'` RED.
- [ ] **Step 3: Implement registration replacement and constant-cost observation.** Allocate `_next_registration_epoch` starting at 1, increment on initial local installation, every successful add_source including same-name replacement, service/connection replacement/reopen, path/physical identity rebound and re-add. Retain service and connection identity, canonical main path and stat `(st_dev, st_ino)` at install; before observation/copy compare one stat/source and registration bindings. A failed install never publishes a proposed vector. An old unlinked connection paired with a replacement path refuses copying until rebound; stat alone is no proof the connection addresses that new file. Data commits/frame generation/query do not reset epoch. Cache only source-name order if useful.

```python
if connection.in_transaction:
    raise ValueError("history source has an uncommitted transaction")
cursor = connection.execute("PRAGMA main.data_version")
try:
    data_version = cursor.fetchone()[0]
    cursor.fetchall()
finally:
    cursor.close()
position = LocalPosition(name, owner, registration_epoch,
                         data_version, connection.total_changes)
```

Never open an observer connection or use QueryService._snapshot around observation. Invalid/closed source is an error, not a sentinel.
- [ ] **Step 4: Bind copy admission/closing reads.** Capture exact registrations/paths plus opening vector immediately before submitting frame/detail; no worker sees live connections/current selection. On owner-thread completion recheck identities/transaction status and read closing vector before publication. Equal bracket can provide answered_source; ordinary unequal counters give answered_source=None. Registration mismatch is also stronger authority retirement for Task 6. Closing read errors become associated LocalSettlement.error and use the owning frame/detail error seam. No worker/callback returns a live service/router/materialized DB or rewrites the token's original request.
- [ ] **Step 5: Run the four tests GREEN** and Task 4's worker controls; record exact observation SQL and preserved active transaction. Final three backup/closing schedules are Task 14, after source-driven shell application exists.
- [ ] **Step 6: Commit.**

```bash
git add src/appio/history_router.py tests/p3/test_live_overview_router.py
git commit -m "feat(history): certify registered source positions with copy brackets"
```

## Task 6: Implement authority retirement and the sole frame/detail writer

**Implementer tier:** Capable

**Lane:** `router`
**Depends on:** Task 5
**Specification:** §§2.4, 3.1–3.4, 5.1–5.4, 6.1 (D01/D02); §12.2 exact tests below. Files owner acceptance completes in Task 7 before any view notification wiring.

**Files:**

- Modify: `src/appio/history_router.py:131-211` (owner state/drain), `src/appio/history_router.py:465-512` (administrative retirement/close), `src/appio/history_router.py:794-1150` (prepare/readiness/settlement/scheduler/getters)
- Modify/Test: `tests/p3/test_live_overview_router.py`
- Modify/Test: `tests/p3/test_history_router.py:20-80` (client/result helpers), `tests/p3/test_history_router.py:254-326` (detail/scope fixtures), remaining affected lazy getter call sites through line 657

**Interfaces:**

- Consumes `query_transport(future) -> QueryTransportState`, `retire_query_connection(future) -> bool`, `reject_query_result(future) -> None`; QueryTransportState's exact request/generation/phase/issue-source fields from Task 3; private RequestToken, LocalOutcome and SettlementInput schemas from Task 4; Task 5 worker bindings/brackets; shared pure validation.
- Produces `_settle(self, token: RequestToken, outcome: SettlementInput | None = None, *, retiring: bool = False) -> bool` on owner thread; `drain(self) -> bool`; nonblocking `pending_work` and unchanged `local_read_in_flight` properties.
- Produces exact optional-clock constructor `HistoryRouter(local: QueryService, client: IodClient, registry: AppRegistry, *, clock_ns: Callable[[], int] = time.monotonic_ns)`.
- Produces `select_detail(app_key: str | None, window: Window | None) -> None`, `request_detail(kind: PartKind, app_id: int, window: Window) -> PartState`, `part_state(kind: PartKind, app_key: str, scope: RetentionScope) -> PartState`, `retained(kind: PartKind, app_key: str, scope: RetentionScope, applied_endpoint: int | None) -> RetainedRecord | None`.
- Produces `ranking_provenance(app_key: str, window: Window) -> AnswerProvenance | None`, `frame_provenance(window: Window) -> FrameProvenance | None`, `preparation_source(window: Window) -> SourcePosition | None`. Existing ranking/series/disk_share signatures remain; exact getters return only their exact requested Window or original stored failure/HistoryPending.

**Exact tests:** `test_settlement_rejects_each_selector_shape_or_binding_violation`; `test_settlement_superseded_or_duplicate_has_no_publication_effect`; `test_scope_owned_detail_survives_forward_frame_generations`; `test_current_detail_failure_clears_record_before_view_notification`; `test_exact_getters_reject_older_retained_window_without_hiding_record`; `test_retained_accessor_uses_applied_endpoint_and_is_pure`; `test_superseded_failure_does_not_clear_newer_accepted_record`; `test_retention_resets_before_subject_scope_boot_or_backward_display`; `test_authority_change_clears_details_without_conflating_cursor_advance`. Notification here is a test observer after the completed drain batch, not new Qt wiring.

- [ ] **Step 1: Add direct router assertions for every named test.** Use live registered stores and bound transport Futures; wrong kind/key/Window/association, wrong Coverage variant, duplicate ranking, and the two **separate** malformed process mappings fail current invalid_result without any evidence publication. Positive mapping is a passing control. For failure clearing parameterize RemoteError, WindowUnavailableError, injected still-authority-current ConnectionLost, query_timeout and invalid_result; preserve actual original exception and reason `str(error)` or class name, not a fabricated coverage reason.

```python
history.select_detail(key, a)
state_a = history.request_detail("totals", app_id, a)
owner = state_a.request_id
history.prepare(b)  # Forward same-scope preparation must not replace this owner.
assert history.part_state("totals", key, scope).request_id == owner
transport.reply(transport.only_future("totals"), share(91, a), snapshot="a" * 64)
history.drain()
record = history.retained("totals", key, scope, b.end_mono_ns)
assert record.value.window == a
assert record.provenance.measured_window == a
with pytest.raises(HistoryPending):
    history.disk_share(app_id, b)
assert history.retained("totals", key, scope, b.end_mono_ns) == record
```

Fixture completion goes through the real associated Future/worker, never direct retained-map insertion. Add pure accessor send/drain/stamp spies, and subject/scope/boot/backward-then-forward/two-range resets before rendering/adoption.
- [ ] **Step 2: Run** `python -m pytest -q tests/p3/test_live_overview_router.py -k 'settlement or scope_owned or current_detail or exact_getters or retained_accessor or superseded_failure or retention_resets or authority_change_clears'` RED.
- [ ] **Step 3: Reconcile authority before collecting/dispatching/comparing sources.** Remote authority is `("remote", client.generation, boot_id, iod_epoch)`; local authority is `("local", ((name, owner, registration_epoch), ...))`. Separately compare registration identity even in remote mode. Sequence/data counters never retire an owner. Remember lost remote authority before observing fallback/new connection. Changed source mode/connection/boot/epoch/registration retires selected lifetime and affected admitted frame once. Administrative `_settle(token, retiring=True)` requires no outcome, marks all issued frame tokens retired, removes unsent frame queries, discards staged evidence/certificate, advances generation **once per frame owner**, `_ready=False`, `view_state="unavailable: authority changed"`; retain actual running frame Future and sent occupant. Do not touch shell floor/recovery state or require the old result/ConnectionLost to arrive first.
- [ ] **Step 4: Implement scope slots and stable stamps independently of preparation.** Positive never-reused router token/lifetime IDs; a forward selected endpoint preserves live request and record, stores only the latest eligible successor. Changed subject/scope/backward endpoint retires synchronously. prepare does not adopt the view's desired scope or erase detail maps/stamps. Keep the successfully admitted **applied frame's** ownership context separately from a newer preparation so request_detail for the displayed Window remains legal. Scope slots keep historical identity→first_seen within selected lifetime, even across intervening success or current failure. Ranking identity history is retained only for that same selected key/scope; current frame per-row provenance cannot borrow a previous generation's PEAK or create an unbounded cross-subject identity cache. Exact getters request their exact Window when eligible and raise HistoryPending for old-record mismatch; current exact failure raises stored original exception. Pure accessors neither send nor drain nor restamp; a future measured endpoint returns None.
- [ ] **Step 5: Implement guarded single settlement.** Administrative writes and normal accepted owner writes share this method. Ordinary guard order is closed/settled/retired/current-token/live-owner/authority/scope, then bound input association, then selector and owner predicates. A remote wrapper must carry that exact done Future/result/exception and normalized request/connection/issue association; it cannot substitute a supplied response mapping or newer cursor. Local success must match immutable worker_id/request/registration binding, outcome variant and owner-read bracket; exceptions need no successful fields/bracket. Invalid current input becomes `RequestRejected("invalid_result", "invalid_result")`; ignore stale completion without clearing another owner. Remote owner-context rejection calls reject_query_result **before notification**. Local paths never call query_transport or invent snapshots. Revalidate external keys before presentation remap.

```python
# Atomic successful detail publication, after all guards and validation:
identity = AcceptedIdentity(kind, app_key, token.request.window, digest)
if identity not in slot_stamps:
    slot_stamps[identity] = self._clock_ns()
first_seen = slot_stamps[identity]
provenance = AnswerProvenance(kind, app_key, token.scope, token.request.window,
                              identity, first_seen, answered_source, local_bracket)
record = RetainedRecord(remapped_value, provenance)
# Install record and ok PartState together; remove only this token's pending marker.
# On current failure: remove retained record first, install failed with original Window,
# retain original exception, then let drain notify after the batch.
```

Read the acceptance clock only for a missing identity. Failure does not restore stamps/records via pending. Catch Exception, not BaseException.
- [ ] **Step 6: Make frame readiness and dispatch owner-specific.** Stage local ranking and every routing flag atomically through _settle. A remote ranking completion received first waits privately for this same frame's local ownership evidence, but its actual terminal transport can reap. Reject mixed-store/overlap/lifetime/empty/paginated remote authority, evict the bound candidate, preserve proven local fallback and ranking-owned DaemonAnswerError/not_retained classification. Frame certificate is completed-generation-only and exact requested/answered source equality; local fallback cannot certify a requested remote source. prepare captures requested_source after A22 and before worker/query admission, using local opening observation without a third read. `ready` drains then gates only matching live frame local/remote ranking terminal outcomes, never selected parts/Files.
- [ ] **Step 7: Route remote detail and ranking through `_dispatch`.** Queue entries carry immutable owner tokens, priority/serial, and no admission-time deadline. One sent occupant remains across timeout/cancellation/retirement until query_transport is terminal/lost; at expired retirement deadline call guarded retirement and observe detach before release. Cache/refusal never-sent outcomes settle once then release immediately. `drain` reconciles, collects immutable inputs, closes local brackets, settles once, reaps **actual** resources, dispatches, reports visible change. Only after complete batch notification may successful detail admit one deferred successor; failure never retries from its notification. Expose pending_work for queued/running/completed-undrained/retired-held work. No view notification is wired until Task 9/12.
- [ ] **Step 8: Run the exact targeted tests GREEN and existing** `python -m pytest -q tests/p3/test_history_router.py`. Update old client fakes to expose normalized transport association, and use controlled detail drain waits where eager synchronous UI getters were assumed; preserve accounting/ownership and original Window assertions. No fabricated terminal flag solely because a fake Future is done.
- [ ] **Step 9: Commit.**

```bash
git add src/appio/history_router.py tests/p3/test_live_overview_router.py tests/p3/test_history_router.py
git commit -m "feat(history): settle scope-owned detail and retire obsolete authority once"
```

## Task 7: Integrate Files deliveries into scheduling and settlement

**Implementer tier:** Capable

**Lane:** `router`
**Depends on:** Task 6
**Specification:** §§3.1, 3.3–3.4, 5.1, 5.4; §§12.2–12.3 exact tests below; retained Files specification acceptance predicates remain binding.

**Files:**

- Modify: `src/appio/history_router.py:175-237` (pending/outward helper), `src/appio/history_router.py:288-475` (local/remote Files paths and retirement), Task 6's `_settle`/`_dispatch`/`drain` sections
- Modify/Test: `tests/p3/test_live_overview_router.py`, `tests/p3/test_live_overview_client.py`
- Modify/Test: `tests/p4/test_file_history_router.py`, `tests/p4/test_files_tab_local_result.py`, `tests/p4/test_files_tab_remote_result.py` (existing fake/worker delivery assumptions only; preserve entire assertion inventory)

**Interfaces:**

- Consumes `_settle(token: RequestToken, outcome: SettlementInput | None = None, *, retiring: bool = False) -> bool`, `_dispatch() -> None`, `drain() -> bool`, pending_work, typed LocalFilesOutcome(worker_id, request, app_key, table, coverage, served_gap_count), bound QueryTransportState and shared validate_query_result.
- Retains `request_file_view(app_id: int, window: Window, sort: FileSort, direction: Direction, search: str | None) -> Future[FileViewResult]`; `_settle_file` becomes outward completion only, invoked by _settle after acceptance/failure/admin retirement. No second callback acceptance/publication remains.
- Produces Files owner page accumulator with captured ranking snapshot, connection/full cursor, request selectors, first-page metadata, visited opaque cursors, page/row count and outward Future; each page/coverage has its own immutable token and scheduler slot.

**Exact tests:** router `test_local_outcomes_share_settlement_for_frame_part_and_files`, `test_ranking_readiness_excludes_all_detail_and_files_work`, `test_local_worker_capacity_counts_retired_jobs_and_keeps_frame_independent`; client `test_all_history_queries_share_one_priority_stable_scheduler`, `test_files_pages_and_coverage_use_single_settlement_acceptance`, `test_frame_failure_or_local_fallback_certifies_no_remote_position`.

- [ ] **Step 1: Add one actual-worker positive and associated malformed case for every LocalOutcome.** Verify local frame publishes every evidence field together, detail publishes exact Window/digest, Files returns unchanged table/coverage/served_gap_count as FileViewResult, zero local client.query/query_transport calls and one writer/publication per token. Independently corrupt worker_id/request, frame routing fields, part key/type/Window/digest and file table/coverage/served gap. Errors fail the owning frame/detail/Files only; no partial flag publication. Add held series/totals/page/continuation/coverage/local-detail readiness arms with no eager first-subject prefetch.
- [ ] **Step 2: Add scheduler and Files controls.** Use QueryTransportHarness to count real envelopes; enqueue ranking, series then totals, first page/continuation/coverage, and supported coverage variants, then assert maximum one active historical job and stable priority order. A continuation is newly priority-2 admitted, not recursively sent. Add local eight-job queue-full and ten-background-charge control including retired queued/running jobs. Do not release Future charges until drain.

```python
assert max(transport.observed_historical_occupants) == 1
assert transport.query_kinds[:3] == ["ranking", "series", "totals"]
assert history.ready(frame_window)  # All frame ranking/ownership evidence settled.
assert held_detail_or_files.done() is False
history.invalidate()
assert outward.exception().reason == "file_source_unavailable"
assert str(outward.exception()) == "File history generation retired."
```

Use fresh per-arm Fixtures; actual no-wait readiness is tested while the independently held work remains held.
- [ ] **Step 3: Run** `python -m pytest -q tests/p3/test_live_overview_router.py -k 'local_outcomes or ranking_readiness or local_worker_capacity'` and `python -m pytest -q tests/p3/test_live_overview_client.py -k 'all_history or files_pages or frame_failure'` RED.
- [ ] **Step 4: Replace all Files direct sends/callback writers.** `_request_remote_files` constructs owner context and enqueues first page; _settle validates/appends each accepted page privately and enqueues continuation or final file coverage. Match subject/exact requested Window/sort/direction/normalized search, ranking snapshot, connection/cursor, first-page metadata, coverage/tier/overflow/served interval. Preserve opaque HMAC, no repeated/missing cursor or empty continuation page, 128-page and 50,000-row bounds with query_too_large, remote served_gap_count=None. Remap external identity only after validation. Rejection calls bound cache eviction before outward failure; never publish a partial assembled table.
- [ ] **Step 5: Route local Files and administrative retirement through the same writer.** Worker does existing fatal/skippable ownership-candidate proofs in private snapshots; LocalSettlement uses actual worker binding with no fictitious remote envelope/bracket. _settle rechecks table/coverage/served evidence before _settle_file. _cancel_file_requests enters administrative settlement per Files owner, rather than completing outward Futures from another path. Current detail/Files failure cannot alter frame `_not_retained` or ranking diagnostic. Keep canceled outward jobs' actual worker charges; close retires publication without pretending running work stopped.
- [ ] **Step 6: Run targeted tests GREEN and** `python -m pytest -q tests/p4/test_file_history_router.py tests/p4/test_files_tab_local_result.py tests/p4/test_files_tab_remote_result.py`. Confirm a real held background computation cannot consume frame executor capacity. The full production apply-while-computation-held and authority-loss shell replacement checks still belong to Task 14.
- [ ] **Step 7: Commit.**

```bash
git add src/appio/history_router.py tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py tests/p4/test_file_history_router.py tests/p4/test_files_tab_local_result.py tests/p4/test_files_tab_remote_result.py
git commit -m "feat(history): settle Files pages and local jobs through the shared scheduler"
```

## Task 8: Extend the pure model with measured provenance and series metadata

**Implementer tier:** Advanced

**Lane:** `presentation`
**Depends on:** Task 7
**Specification:** §§2.3, 7.1–7.3 (D04); §12.5 exact model/availability tests below.

**Files:**

- Modify: `src/appio/ui/render_model.py:1-35` (neutral imports), `src/appio/ui/render_model.py:200-310` (OverviewModel), `src/appio/ui/render_model.py:407-636` (builder/lanes/exports)
- Modify/Test: `tests/test_ui.py:88-248` (factories), existing builder/direct-model tests and new tests appended after line 3018

**Interfaces:**

- Consumes `AnswerProvenance(kind, app_key, scope, measured_window, accepted_identity, first_seen_at_ns, answered_source, local_bracket)`, `SeriesMetadata(app_id, app_key, measured_window, tier, coverage)`, `retention_scope(app_key: str, window: Window) -> RetentionScope`, `applied_endpoint(window: Window) -> int | None`, and existing AppTotal/Series/DiskShare/formatters.
- Produces these **additional OverviewModel fields, in exactly this order after coverage_diagnostic**, with no other frozen render-dataclass changes:

```python
ranking_provenance: AnswerProvenance | None = None
series_provenance: AnswerProvenance | None = None
share_provenance: AnswerProvenance | None = None
device_lanes_unavailable: str | None = None
requested_window: Window | None = None
app_key: str | None = None
series_metadata: SeriesMetadata | None = None
```

- Produces `build_overview(app_id: int, window: Window, total: AppTotal | None, series: Series | None, share: DiskShare | None, *, ranking_error: str | None = None, series_error: str | None = None, share_error: str | None = None, coverage_diagnostic: str | None = None, ranking_provenance: AnswerProvenance | None = None, series_provenance: AnswerProvenance | None = None, share_provenance: AnswerProvenance | None = None) -> OverviewModel`. No caller metadata parameter is added; derive typed metadata from successful Series.

**Exact tests:** `test_overview_provenance_accepts_older_same_scope_rejects_contradictions`; `test_overview_direct_metadata_validates_series_coverage_and_requested_endpoint`; `test_new_share_device_unavailability_removes_old_device_values`; `test_mixed_endpoint_inputs_keep_independent_outputs`. Task 15 extends these through the integrated retained view.

- [ ] **Step 1: Add builder/direct-constructor tests.** A=since_boot boot-a through 20 s, B through 30 s; use independent older share/newer series and the reverse, same exact range controls and boot-less lifetime. Match external keys and presentation IDs. Reject future detail endpoint, other subject/boot/range/start/scope, wrong provenance kind/Window and object with its own error. Add extended direct model with ranking_provenance=None but future-endpoint metadata/share, series coverage incomplete with populated lanes, and key/ID/tier mismatch. Preserve all-default legacy direct construction and exact old builder calls.

```python
model = build_overview(app_id, b, row_b, series_b, share_a,
                       series_provenance=series_p_b, share_provenance=share_p_a)
assert model.requested_window == b
assert model.app_key == row_b.app_key
assert model.share.window == a
assert model.share_provenance.measured_window == a
assert model.series_metadata == SeriesMetadata(app_id, row_b.app_key, b,
                                               series_b.tier, series_b.coverage)
assert model.device_lanes == () and model.more_devices == 0
assert model.device_lanes_unavailable == "device measurements have different endpoints"
assert "; share measured: since_boot boot-a through 20000000000 ns" in model.detail_line
with pytest.raises(ValueError):
    replace(model, requested_window=a)  # B series is now beyond requested endpoint.
```

- [ ] **Step 2: Run** `python -m pytest -q tests/test_ui.py -k 'overview_provenance or overview_direct_metadata or new_share_device_unavailability or mixed_endpoint_inputs'` RED; report any already-green numeric availability as preservation only.
- [ ] **Step 3: Implement legacy and extended validation.** Legacy direct mode is exactly all three provenances/requested_window/app_key/series_metadata None; retain frozen represented-data checks, do not infer absent metadata. Any extended field requires valid requested_window, and successful dependency/provenance requires external app_key; successful series projection, including available-empty, requires SeriesMetadata. Validate nested Coverage/tier durations/chronology, positive matching IDs, external key association, provenance measured Window, identical requested retention scope and every bounded measured end<=requested end, even without ranking provenance. A dependency missing/error cannot carry its provenance; provenance cannot fabricate data. Unsupported lifetime chronology may keep successful metadata but no plotted points/lanes. Early ranking-error/file-only branches retain their fixed text and no accepted detail or lane reason.
- [ ] **Step 4: Implement independent outputs and ordered disclosures.** Share owns TOTAL/READ/WRITE/donut and gutters; defined current-row PEAK only; share's available device state+defined device total owns DEVICE/bars, and defined residual additionally owns reconciliation. Series independently owns task chronology; incomplete series device coverage only withholds device lanes. Device lanes additionally require equal exact measured endpoints and supported same-boot chronology. Select active positive share rows by bytes descending then UTF-8 dev, max 3/more count, gutter from share rows; do not sum partial samples for gutters. Lifetime retains totals/donut and boot-qualified bars, with no time lanes.

Device reason first applicable: task chronology/series failure; share failure/missing; share coverage reason/state or `device total unavailable`; series coverage reason/state; `device measurements have different endpoints`. A reason gives zero lanes/more. Preserve coverage source preference share→series→ranking and separate differing coverage clauses. Append measured clauses share then series only if Window differs, then `; device lanes: <reason>`:

```python
if measured.kind == "since_boot":
    disclosure = f"since_boot {measured.boot_id} through {measured.end_mono_ns} ns"
elif measured.kind == "range":
    disclosure = (f"range {measured.boot_id} "
                  f"[{measured.start_mono_ns}, {measured.end_mono_ns}) ns")
else:
    disclosure = "lifetime"
```

Do not parse text to recover metadata, substitute ranking bytes for missing share, equate independent tiers, or normalize unavailable to zero.
- [ ] **Step 5: Run targeted tests GREEN and existing pure builder/direct-constructor controls in** `python -m pytest -q tests/test_ui.py -k 'build_overview or build_ranking or render_dataclasses or lifetime_series or overview_provenance or overview_direct_metadata or new_share_device_unavailability or mixed_endpoint_inputs'`.
- [ ] **Step 6: Commit.**

```bash
git add src/appio/ui/render_model.py tests/test_ui.py
git commit -m "feat(ui): model independent measured windows and typed series provenance"
```

## Task 9: Adopt retained capability and exact-only identity/reset semantics

**Implementer tier:** Capable

**Lane:** `presentation`
**Depends on:** Task 8
**Specification:** §§2.4, 6.2–6.3 (D09), 7.1; §12.5 adapter/notification tests below.

**Files:**

- Modify: `src/appio/ui/main_window.py:17-145` (imports/protocol/adapter), `src/appio/ui/main_window.py:332-387` (clock/capability state), `src/appio/ui/main_window.py:554-621` (ranking capture/adoption), `src/appio/ui/main_window.py:671-716` (selection), `src/appio/ui/main_window.py:790-923` (detail loading/notification), `src/appio/ui/main_window.py:1031-1084` (clear paths)
- Modify/Test: `tests/test_ui.py` (structural service fixtures and new adapter/view tests)

**Interfaces:**

- Keeps required UI-local QueryService exactly `ranking(window: Window, metric: str) -> Sequence[AppTotal]`, `series(app_id: int, window: Window) -> Series`, `disk_share(app_id: int, window: Window) -> DiskShare`.
- Adds optional `RetainedQueryService(Protocol)` with all five exact callables: `select_detail(app_key: str | None, window: Window | None) -> None`; `request_detail(kind: PartKind, app_id: int, window: Window) -> PartState`; `part_state(kind: PartKind, app_key: str, scope: RetentionScope) -> PartState`; `retained(kind: PartKind, app_key: str, scope: RetentionScope, applied_endpoint: int | None) -> RetainedRecord | None`; `ranking_provenance(app_key: str, window: Window) -> AnswerProvenance | None`.
- Adds optional `ExactAnswerIdentityService(Protocol).exact_answer_identity(kind: AnswerKind, app_key: str, window: Window) -> AcceptedIdentity | None`.
- Adds private `_ExactDetailAdapter(service: QueryService, *, clock_ns: Callable[[], int], authority_epoch: int)` implementing those five retained methods plus `capture_ranking(rows: Sequence[AppTotal], window: Window) -> None`, `reset_authority(authority_epoch: int) -> None`.
- Adds `AppIoWindow(..., *, files_view: FilesView | None = None, clock_ns: Callable[[], int] = time.monotonic_ns)`; `reset_query_authority(self) -> None`; `refresh_measurements(self) -> None`; `measurement_work_requested = pyqtSignal()`.
- `refresh_measurements` consumes current PartState and eligible RetainedRecord for the **applied** Window plus already captured ranking row/provenance; it issues zero ranking/detail admission/getter/Files/prepare/probe/setup queries.

**Exact tests:** `test_pending_refresh_keeps_retained_dependency_values`; `test_exact_only_adapter_matches_retention_failure_and_reset_rules`; `test_exact_only_identity_reacceptance_reuses_stamp`; `test_exact_only_authority_reset_retires_unchanged_lifetime`; `test_detail_notification_is_query_free_and_preserves_other_dependency`.

- [ ] **Step 1: Add exact-only and retained structural fixtures.** Detect full retained capability once with `all(callable(getattr(service, name, None)) for name in names)`; partial capability falls back. A callable capability that raises is a real failure, never silent fallback. Plain three-method services remain valid. ExactOnlyQuery controls separate kind/window results, pending/errors, getter counters and optional exact identity. Require one successful-result identity call immediately after that getter, no calls on pending/failure/notification; capture ranking once immediately after ranking succeeds and before detail queries.

```python
view.set_window(a)  # Identity-bearing exact service returns A at clock=1 s.
old_widget = view._total_chart
old = view._overview_model.share_provenance
clock.advance_ns(2_000_000_000)
view.refresh()
assert view._overview_model.share_provenance.first_seen_at_ns == old.first_seen_at_ns
assert view._total_chart is old_widget
before = dict(service.calls)
view.refresh_measurements()
assert service.calls == before
view.reset_query_authority()
assert view._exact_authority_epoch == 2
assert view._detail_service.retained("totals", key, retention_scope(key, a), None) is None
```

Use lifetime `a` for this reset body so the accessor endpoint is correctly None; bounded tests pass their applied end. `_detail_service` is the view's stored selected retained capability/adapter (pin that private name for these tests). Add malformed kind/key/Window/digest and raising optional identity negatives, valid None identity and unknown age contributor, and unchanged-lifetime epoch1→epoch2 with old pending notification ignored.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_ui.py -k 'pending_refresh_keeps or exact_only or detail_notification'` RED.
- [ ] **Step 3: Implement adapter scope/token/failure lifetime.** Match router retention/reset/endpoint rules; validation of each successful exact result and optional identity occurs before record acceptance, without importing client/router. Invalid provenance/selector becomes ValueError("invalid_result"), slot reason invalid_result; catch Exception only. Without identity retain measured Window but identity/stamp/source/bracket None. Valid identity stamps use injected clock and per-live-scope lookup, repeated identity reuses stamp even after another success/current failure. At most one in-flight exact observation per kind; a pending A is observed through the same A getter once per ordinary refresh while B is applied, with one latest successor B. Query-free notification never observes a getter. Preserve original exception privately for exact failure semantics and clear failed record before model notification; pending retry restores no record.
- [ ] **Step 4: Implement UI authority epochs and selection order.** `_exact_authority_epoch=1`; reset_query_authority increments before notifications/getter/adoption and, when an exact adapter is installed, calls reset_authority(new strictly greater positive epoch), clears its pending/successors/records/stamps/selection and queued old-lifetime applicability, issues no query and changes no shell recovery state. A full retained capability keeps its router-owned retirement; do not call an adapter-only method on it. Owners must report connection/service/mode/rebinding authority changes, including unchanged lifetime; sequence/counter advance is not reset. The adapter cannot infer unreported hidden service changes from equal data, and must not resume an old getter after reset.
- [ ] **Step 5: Replace `_load_selected` exact-getter effectiveness.** For ranked subject adopt `(key, applied_window)` first, request series then totals at most once per ordinary refresh, read states independently, get effective values via retained with **applied_endpoint**. Failed provides exact reason; pending is not an error and may retain A. Keep current applied ranking/PEAK/provenance captured at ranking refresh/adoption, never borrow a newer router generation on notification. Each dependency's invalid observation removes that dependency only; a broad builder error handler must not erase an independently valid other input. Ranking failure/empty union/no-subject/file-only paths call select_detail(None,None); file-only issues no process detail and keeps fixed reason/highlight behavior.
- [ ] **Step 6: Add query-free overview diff and work signal.** refresh_measurements rereads only slot/retained observations, rebuilds Overview and diffs existing widgets; no retry/admission. Emit measurement_work_requested after ordinary selection/refresh admits pending detail; do not query again to emit. Shell will connect it once in Task 12. Direct refusal resets selection before unavailable rendering; typed refusal category/age presentation is added in Task 11. No new status writer here.
- [ ] **Step 7: Run named tests GREEN**, plus `python -m pytest -q tests/test_ui.py -k 'identical_refresh or file_only or empty_choice or two_tab or refusal or exact_only or pending_refresh_keeps or detail_notification'`. Assert accepted A Window/stamp and widget identities survive B pending, and failure immediately removes only its dependents.
- [ ] **Step 8: Commit.**

```bash
git add src/appio/ui/main_window.py tests/test_ui.py
git commit -m "feat(ui): retain exact and structural detail with identity-aware authority resets"
```

## Task 10: Maintain bucket-anchored append caches and per-run curves

**Implementer tier:** Capable

**Lane:** `charts`
**Depends on:** Task 9
**Specification:** §§8.1–8.7; §12.6 eleven row tests plus structural/geometry tests below. Task 16 adds the final counted discriminator inventory after composer integration.

**Files:**

- Modify: `src/appio/ui/charts.py:99-339` (ChartCurve/validation/aggregate and geometry helpers), `src/appio/ui/charts.py:339-691` (SeriesChart/DualSeriesChart/DeviceLane state/setters/paint)
- Modify/Test: `tests/test_charts.py:180-694` (expressly amended bin/step expectations), new append/geometry tests after line 1013

**Interfaces:**

- Consumes unchanged SeriesPoint, DeviceSeriesPoint, ChartColumn and ChartSegment fields/validation, existing palette/layout and exact duration/byte arithmetic.
- Produces frozen `ChartCurve(side: Literal["read", "write"], run_index: int, vertices: tuple[tuple[float, float], ...])` with exactly those fields, finite float pairs, nonnegative nonbool run_index, >=2 vertices and nondecreasing X.
- Produces `SeriesChart.set_task_points(points: Sequence[SeriesPoint], *, identity: object = None) -> None`, `append_task_points(points: Sequence[SeriesPoint], *, identity: object) -> None`, `curves() -> tuple[ChartCurve, ...]`; retains columns/segments/set_unavailable. DeviceLane produces `set_device_points(points: Sequence[DeviceSeriesPoint], *, identity: object = None) -> None` and `append_device_points(points: Sequence[DeviceSeriesPoint], *, identity: object) -> None`. Incoming append is the **whole new sequence**; identity is opaque equality data.
- Private retained state: immutable full raw input/mode/identity/availability/reason; W/g and two-integer inclusive group bounds; cached ChartColumn+observed_duration_ns; canonical `(run_index,start_ns,end_ns)` interval lists with local tail/run-end metadata; exact per-side max ratios; cached curves/fills. Do not add a public debug API.

**Exact tests to add:** `test_live_append_row_1_invalid_preserves_state`; `test_live_append_row_2_unavailable_rebuilds_even_empty_or_same_points`; `test_live_append_row_3_identity_change_rebuilds`; `test_live_append_row_4_empty_transition_and_noop`; `test_live_append_row_5_empty_to_nonempty_has_no_tail`; `test_live_append_row_6_equal_input_is_noop`; `test_live_append_row_7_tail_replacement_rebuilds_one_group`; `test_live_append_row_8_exact_prefix_appends_only`; `test_live_append_row_9_tail_change_then_append`; `test_live_append_row_10_nonempty_shortening_rebuilds`; `test_live_append_row_11_interior_contradiction_rebuilds`; `test_live_tail_replacement_recomputes_duration_peak_and_run_state`; `test_live_canonical_intervals_coalesce_only_touching_same_run`; `test_live_curve_and_fill_support_never_bridge_multiple_runs_in_one_column`; `test_live_inset_handles_eight_fragment_subpixel_and_two_fragment_runs`; `test_live_single_fragment_nonzero_run_draws_tick`; `test_live_scale_maxima_reach_each_half_after_append_merge_and_tail`; `test_live_rebuild_resize_and_unavailable_reset_preserve_honesty`.

- [ ] **Step 1: Add separate test functions for all eleven append rows.** Parameterize task/device as appropriate, preserving raw None versus zero, peak_total_bps, wall_start_seconds and suspension fields. Invalid append snapshots every retained cache/identity/reason/repaint state and proves no mutation. Full setter always rebuilds even on equal/unavailable input; setter→append equal identity is a no-op. Example using existing task_point/make_chart helpers:

```python
chart = make_chart(SeriesChart, qtbot)
old = (task_point(0, read_bytes=1, write_bytes=2),
       task_point(10_000_000_000, read_bytes=3, write_bytes=4))
chart.set_task_points(old, identity=("task", "a"))
before = chart.columns(), chart.curves(), chart.segments()
updates = []
monkeypatch.setattr(chart, "update", lambda: updates.append(True))
chart.append_task_points(old, identity=("task", "a"))
assert (chart.columns(), chart.curves(), chart.segments()) == before
assert updates == []
new = old + (task_point(20_000_000_000, read_bytes=5, write_bytes=6),)
chart.append_task_points(new, identity=("task", "a"))
assert sum(c.read_bytes for c in chart.columns()) == 9
assert sum(c.write_bytes for c in chart.columns()) == 12
```

Add the support/scale/canonical interval tests listed above with holes within a single column, touching suspension-separated task runs, device runs without task suspension, tiny eight-fragment/two-fragment runs with unequal heights and single nonzero-fragment ticks.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_charts.py -k 'live_append or live_tail or live_canonical or live_curve or live_inset or live_single or live_scale or live_rebuild'` RED.
- [ ] **Step 3: Implement validate-before-mutate and the total branch table.** Fully snapshot/validate incoming contract points before changing state. Branch in this exact order: invalid raises; unavailable rebuilds; changed identity rebuilds; empty becomes available-empty or exact empty no-op; old empty appends all; full equal no-op; equal-length exact-prefix-through-penultimate replaces one tail group; longer full-prefix appends; longer prefix-through-old-penultimate with changed old tail replaces tail then appends suffix at old length; shorter nonempty rebuilds; all other valid contradictions rebuild. Rows 7/8/9 are disjoint. Full setters explicitly store identity and rebuild once. set_unavailable validates reason before clearing **all** raw/aggregate/run/paint/identity/gutter state and repainting once; it cannot revive old data. Constructor is available-empty/identity None.
- [ ] **Step 4: Implement anchored group updates.** For positive area `W=max(0,floor(right-left))`, minimal power-of-two g satisfies ceil(n/g)<=W; g=1 when empty. Group bounds are `(j*g,min((j+1)*g,n)-1)`, anchored index 0, not time-pixel bins. Append visits only new/open members. At overflow pairwise merge cached adjacent groups, adding bytes/durations, max defined peaks (all None remains None), OR starts_run, envelope extrema and concatenated bounds. Repeat doubling for large suffixes. Tail replacement scans only its at-most-g group, recomputes removed/lowered sole maxima and changed suspend/end state before future append. Never store/copy per-column member-index arrays or rebuild prefix run membership for an ordinary append.
- [ ] **Step 5: Implement canonical run intervals and exact independent scales.** Task run begins first/nonadjacent/immediately after suspended point; suspended point belongs to preceding run. Device run begins first/nonadjacent only. Coalesce touching intervals only within same run; promotion seam coalesces same-run touching intervals, never fills holes or joins distinct run IDs. Dense contiguous input keeps one interval/column; fragmented intervals may exceed W. Group rate is raw bytes/observed duration, excluding holes in envelope. Compare candidate maxima by integer cross multiplication, recompute from cached columns every changed update, convert only at final finite pixel mapping. Each nonzero read/write maximum independently reaches its half-height after pen inset; zero has no division/shape.
- [ ] **Step 6: Build per-run polyline strokes and exact closed fills from cached intervals.** Map time first raw start→last raw end. For each run's mapped outer support `[s,e]`, inset=min(0.25,(e-s)/4), L=s+inset,U=e-inset. Each fragment uses its own interval midpoint and its column height; never envelope midpoint. Single fragment emits `(L,y),(U,y)`; multiple fragments clamp first>=L and <=U, lower last<=U, then forward monotonically clamp every next X while preserving all heights. Repeated X is legal. Zero side emits nothing; otherwise emit one ChartCurve per run/side and fill exactly:

```python
polygon = ((vertices[0][0], centre), *vertices,
           (vertices[-1][0], centre), (vertices[0][0], centre))
```

No curve/fill edge bridges runs or extends outside inset support. `curves()` exposes actual next-paint strokes, segments actual fills, without mutations/I/O. paintEvent uses cached geometry/labels only, no raw sample iteration/aggregation or spline overshoot. Resize/identity/shrink/contradiction/full setter are declared full rebuilds. Collapsed width/height retains valid raw state but exposes no columns/curves/fills until positive resize.
- [ ] **Step 7: Run tests GREEN and** `python -m pytest -q tests/test_charts.py`. Migrate only obsolete time-bin/step expectations such as `test_downsampling_bins_points_by_the_full_time_extent`; retain exact byte/peak/hole/scale/unavailable/empty honesty assertions. AppendProbe final counts and promotion-bound discriminator are Task 16, with aggregation and O(n) validation/diff counted separately.
- [ ] **Step 8: Commit.**

```bash
git add src/appio/ui/charts.py tests/test_charts.py
git commit -m "feat(charts): append anchored groups and draw inset per-run curves"
```

## Task 11: Establish typed refusal, measurement observation, and independent age

**Implementer tier:** Advanced

**Lane:** `presentation`
**Depends on:** Task 9
**Specification:** §§6.2, 9.1–9.3 (D08/D09); first three §12.7 age tests, and lifecycle fixture controls for Task 15.

**Files:**

- Modify: `src/appio/ui/render_model.py:1-35` (imports), append UI-local records/age helper before exports at baseline `src/appio/ui/render_model.py:615-636`
- Modify: `src/appio/ui/main_window.py:332-387` (state/signals), `src/appio/ui/main_window.py:436-456` (age widget), `src/appio/ui/main_window.py:554-654` (refusal/adoption/status observation), Task 9's notification and clear paths
- Modify: `src/appio/ui/collector_window.py:517-586` (typed presentation state), `src/appio/ui/collector_window.py:588-655` (refusal signal connection), `src/appio/ui/collector_window.py:914-971` (existing refusal/operational/acceptance transitions), `src/appio/ui/collector_window.py:995-1015` (explicit retry error widget ownership), `src/appio/ui/collector_window.py:1448-1470` (presentation reset on close)
- Modify/Test: `tests/test_ui.py`, `tests/p3/test_collector_window.py` (typed refusal transition controls only; composer inventory is Task 15)

**Interfaces:**

- Consumes AnswerProvenance/PartPhase and Task 9 retained adapter plus `refresh_measurements() -> None` / `reset_query_authority() -> None`.
- Produces frozen **UI-local** records in render_model.py with exactly these fields:

```python
# MeasurementStatus(has_displayed_values: bool, ranking_empty: bool,
#   share_state: PartPhase, series_state: PartPhase, share_reason: str | None,
#   series_reason: str | None, displayed_answers: tuple[AnswerProvenance, ...])
# PresentationRefusal(origin: Literal["shell_prepare", "direct_view"], window: Window,
#                     reason: str, text: str)
# OverviewStatusChoice(row: Literal["1", "2", "3", "4", "5a", "5b", "6a", "6b",
#                                    "6c", "6d", "6e", "6f"],
#                      text: str, age_text: str | None)
```

- Produces pure `oldest_answer_age(displayed_answers: Sequence[AnswerProvenance], now_ns: int) -> str | None`, `AppIoWindow.measurement_status() -> MeasurementStatus`, `show_overview_age(text: str | None) -> None`, `presentation_refusal_changed = pyqtSignal(object)`; persistent QLabel stored as `self.overview_age`, with objectName `overview_age`, beside history_state. Shell owns `_presentation_refusal: PresentationRefusal | None`; standalone owns its local equivalent.
- Produces private pure `_choose_overview_status(*, refusal: PresentationRefusal | None, prepare_error: str | None, busy: bool, retry_after: int | None, now_ns: int, has_frame: bool, has_target: bool, has_sample: bool, any_work: bool, view_state: str, measurement: MeasurementStatus, age_now_ns: int) -> OverviewStatusChoice` in render_model.py. It is the single decision tree for standalone composition and Task 12's shell production choice method. `prepare_error` is passed only from a still-owned PrepareRetry, never a detached label or a new operational flag. Task 12 switches shell status writers after this typed state/tree exists.

**Exact age tests:** `test_first_seen_age_uses_raw_thresholds_and_ceiling_in_every_unit`; `test_accepted_identity_includes_exact_selector_despite_same_snapshot`; `test_age_uses_oldest_displayed_dependency_and_unknown_contributor`.

- [ ] **Step 1: Add age and typed record assertions.** Test exact fields, bool/phase/reason/text/Window/provenance validation, fixed distinct dependency order ranking/totals/series, row6f iff text empty, and legal exact age label text. A pure choice has no displayed-measurement count field, so producers enforce age None iff no contributing answers; do not invent another field. Use this exact arithmetic oracle:

```python
vectors = [(0, "0 s"), (1, "1 s"), (999_999_999, "1 s"),
           (89_999_000_000, "90 s"), (90_000_000_000, "2 min"),
           (5_399_940_000_000, "90 min"), (5_400_000_000_000, "2 h")]
for delta, text in vectors:
    assert oldest_answer_age((p,), p.first_seen_at_ns + delta) == f"oldest answer {text} ago"
assert oldest_answer_age((), 0) is None
assert oldest_answer_age((replace(p, accepted_identity=None, first_seen_at_ns=None),), 0) == "age unknown"
assert oldest_answer_age((p,), 0) == "oldest answer 0 s ago"
with pytest.raises(ValueError):
    oldest_answer_age((p,), True)
```

Use an accepted p stamp>=0, local and remote reapplication plus changed exact Window/same snapshot controls, and a mixed oldest ranking/share/series example. Defined PEAK contributes age but not has_displayed_values; unsupported lifetime series/empty series/coverage text do not create a displayed measurement.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_ui.py -k 'first_seen_age or accepted_identity_includes or age_uses_oldest'` RED.
- [ ] **Step 3: Implement pure validation and integer ceiling age.** Require valid provenance, nonnegative nonbool now; no answers→None, any missing identity/stamp→age unknown. Otherwise subtract min stamp with max(0,delta), branch on **raw ns** <90 s / <90 min / hours, then ceiling-divide by chosen unit. No float, wall time or rounded intermediate threshold.
- [ ] **Step 4: Implement measurement observation and persistent label.** Determine contributing dependencies from the actual effective model: share for measured KPIs/donut/gutters; successful series for rendered nonempty task lane or eligible device lanes; current ranking only when PEAK defined. Preserve identity-less provenances for unidentified contributors. has_displayed_values is any share TOTAL/READ/WRITE/DEVICE or nonempty accepted task points (even zero-byte points), not PEAK/frame/no-samples/unsupported lifetime alone. ranking_empty means successful applied empty ranking, never failure/unapplied. show_overview_age(None) hides/clears; valid nonempty label text shows and diffs only changed text, independent of status visibility. No queries/stamp acceptance from observation or label change.
- [ ] **Step 5: Implement typed refusal lifecycle without switching the status tree yet.** Shell WindowUnavailable precheck sets shell_prepare(window,reason,exact text), preserves last frame and rethrows through existing seam with no floor/pending/PrepareRetry. Direct refusal performs frozen both-surface cleanup, select_detail(None,None), then synchronously emits direct_view to connected shell before composing; it hides age and cannot be reconstructed from shell `_displayed`. Ordinary tick/notification/probe/floor/blocked request preserves typed refusal. Accepted supported shell prepare clears shell_prepare and only its mirrored error widget, while direct_view persists until supported **view adoption**. Supported set_window clears/emits None before model composition and clears the corresponding refusal widget. Operational attempt supersedes only shell_prepare; direct_view still wins. No-target/close clears presentation only within frozen operational reset rules. For direct A22 records use the contract reason `launch_attribution` or `unsupported_window` with window_disabled_reason's unchanged exact text; never classify a label. Error widget clearing uses typed category/owner, never `startswith("appio:")` or error/detail-label classification. Existing PrepareRetry set/clear sites remain sole operational owners.
- [ ] **Step 6: Implement the one pure choice tree after typed refusal exists and use it for standalone status.** The signature is pinned above. The shell will snapshot its actual inputs in Task 12; standalone passes no PrepareRetry/busy/floor/acquisition inputs and uses its own injected age clock, without a collector or query timer. Embedded view only exposes measurement/refusal observations and renders supplied status. Implement the shared tree with these exact returns (all input validation occurs before choosing):

```python
age = oldest_answer_age(measurement.displayed_answers, age_now_ns)
if refusal is not None:
    return OverviewStatusChoice("1", refusal.text, age)
if prepare_error is not None:
    return OverviewStatusChoice("2", prepare_error, age)
if busy:
    return OverviewStatusChoice("3", f"history read has not completed ({view_state}); the reader is still busy", age)
if retry_after is not None and now_ns < retry_after:
    n = (retry_after - now_ns + 999_999_999) // 1_000_000_000
    return OverviewStatusChoice("4", f"history read has not completed ({view_state}); retrying in {n} s", age)
if not has_frame:
    if not has_target or not has_sample:
        return OverviewStatusChoice("5a", "waiting for the first sample", age)
    return OverviewStatusChoice("5b", f"loading history ({view_state})", age)
if measurement.has_displayed_values:
    if any_work:
        return OverviewStatusChoice("6a", f"updating ({view_state})", age)
    return OverviewStatusChoice("6b", "live", age)
if measurement.ranking_empty:
    return OverviewStatusChoice("6c", "no applications observed", age)
if "pending" in (measurement.share_state, measurement.series_state):
    return OverviewStatusChoice("6d", f"loading measurements ({view_state})", age)
for phase, reason in ((measurement.share_state, measurement.share_reason),
                      (measurement.series_state, measurement.series_reason)):
    if phase == "failed":
        return OverviewStatusChoice("6e", f"measurements unavailable: {reason}", age)
return OverviewStatusChoice("6f", "", age)
```

- [ ] **Step 7: Run age tests GREEN and existing refusal cleanup controls** `python -m pytest -q tests/test_ui.py -k 'first_seen_age or accepted_identity_includes or age_uses_oldest or refusal or refused_window or supported_adoption'` and `python -m pytest -q tests/p3/test_collector_window.py -k 'prepare_failure_ownership or deferred_explicit_prepare_failure or precheck_refusal'`. Do not claim the final status-row discriminator inventory ran yet.
- [ ] **Step 8: Commit.**

```bash
git add src/appio/ui/render_model.py src/appio/ui/main_window.py src/appio/ui/collector_window.py tests/test_ui.py tests/p3/test_collector_window.py
git commit -m "feat(ui): own refusal categories and expose independent measurement age"
```

## Task 12: Wire source-driven shell preparation, continued drain, and one composer

**Implementer tier:** Capable

**Lane:** `shell`
**Depends on:** Task 10, Task 11
**Specification:** §§3.2, 6.1, 6.3, 9.1–9.3 (D01/D07/D08); §12.4 shell tests and §12.7 native clocks below; recovery §§4.4–4.5 preserved.

**Files:**

- Modify: `src/appio/ui/collector_window.py:517-655` (source/age clock and signal connection), `src/appio/ui/collector_window.py:720-1081` (target/apply/poll/composer), `src/appio/ui/collector_window.py:1188-1250` (authority notification alongside existing reconcile/committed-tick owners), `src/appio/ui/collector_window.py:1448-1470` (close), `src/appio/ui/collector_window.py:1615-1682` (production composition)
- Modify/Test: `tests/p3/test_collector_window.py:80-171` (FakeHistory typed source/drain/certificate interface), controlled-frame fixture and new integration tests

**Interfaces:**

- Consumes router `prepare(window) -> None`, `ready(window) -> bool`, `source_position() -> SourcePosition`, `preparation_source(window) -> SourcePosition | None`, `frame_provenance(window) -> FrameProvenance | None`, `drain() -> bool`, pending_work, unchanged local_read_in_flight, view `refresh_measurements() -> None`, `measurement_status() -> MeasurementStatus`, `reset_query_authority() -> None`, measurement_work_requested and presentation_refusal_changed signals, and `show_overview_age(text: str | None) -> None`.
- Consumes the Task 11 single pure `_choose_overview_status(*, refusal: PresentationRefusal | None, prepare_error: str | None, busy: bool, retry_after: int | None, now_ns: int, has_frame: bool, has_target: bool, has_sample: bool, any_work: bool, view_state: str, measurement: MeasurementStatus, age_now_ns: int) -> OverviewStatusChoice`; delegate this exact signature after snapshotting shell-owned inputs. Do not duplicate its decision tree or modify main_window/render_model in this task.
- Produces shell `_pending_source: SourcePosition | None=None`, `_displayed_source: SourcePosition | None=None`, `_displayed_certified: bool=False`; additive constructor `age_clock_ns: Callable[[], int] = time.monotonic_ns` beside unchanged BOOTTIME clock_ns.
- Produces `apply_ready_frame(self, *, now: int | None = None, context: Literal["ordinary", "poll"] = "ordinary") -> bool`; `_compose_overview_status_choice(self, now_ns: int, age_now_ns: int) -> OverviewStatusChoice`, `_compose_overview_status(self, now_ns: int) -> str`, `_update_overview_status(self, now_ns: int) -> None`.
- Composer consumes typed refusal/current PrepareRetry+associated _prepare_error, actual pending/displayed/target/sample/work/read/retry inputs and view MeasurementStatus; it cannot query, submit, mutate recovery or infer category from label strings.

**Exact tests to establish:** `test_unchanged_certified_local_or_remote_source_is_quiescent`; `test_pending_source_advances_coalesce_without_retiring_detail`; `test_recovery_transition_ownership_survives_source_driven_trigger`; `test_no_frame_poll_drains_details_and_files_and_notifies_immediately`; `test_no_frame_deferred_apply_uses_ordinary_gate_but_poll_never_submits`; `test_native_composition_binds_one_age_clock_domain`. The three bracket-replacement tests are Task 14; exhaustive status/refusal traces are Task 15.

- [ ] **Step 1: Extend FakeHistory and add real-router controls.** A fake has explicit normalized SourcePosition/certified FrameProvenance and `drain`, not auto-ready based on Window equality; preserve old assertions for queued reader, same history key, 30 s stall/floor, PrepareRetry origin, probe arbitration, Files transient scope and `_refresh_after_apply` sole sites. Use live router/client for source/certification controls. No target computation is allowed in poll's no-pending path:

```python
shell._pending = None
shell._deferred_target = b
before = history.prepare_count
shell.apply_ready_frame(now=now, context="poll")
assert history.prepare_count == before
assert shell._deferred_target == b
shell.apply_ready_frame(now=now, context="ordinary")
assert history.prepare_count == before + 1
```

Use fresh fixtures for free gate, held queued/running frame Future, future/expired floor and still-owned explicit PrepareRetry; every arm drains/notifies completed detail first. Add separate clock providers, recovery 300 s ahead of age, actual client/router/view wiring, age 0→1 s, recovery-only floor change with stable age, and age-domain client expiry/retirement with no recovery-clock deadline comparison.
- [ ] **Step 2: Run** `python -m pytest -q tests/p3/test_collector_window.py -k 'unchanged_certified or pending_source_advances or recovery_transition_ownership or no_frame_poll or no_frame_deferred or native_composition'` RED.
- [ ] **Step 3: Replace only endpoint-based preparation need.** Same desired frame retention scope and certified unchanged source is quiescent even as since_boot clock endpoint advances; keep actual applied Window unchanged. Different fixed range, no frame, uncertified/moved source, explicit/deferred/retry/stalled/invalidation cause still passes unchanged `_submit_prepare` gate. Source changes while a live preparation is pending coalesce; authority changes first retire router frame once, then shell `_retire_pending(now)` once, floor once, no inline replacement. On accepted prepare read **stored** preparation_source and set pending_source with pending; do not independently recapture. `_apply_frame` reads that generation's completed certificate, compares requested source to captured pending source, promotes displayed_source only if certified; otherwise flag false while retaining last proved token. Clear source fields only alongside their corresponding existing scope/pending transitions. Detail never promotes frame source. Notify exact adapter authority reset before adoption/notifications for mode/connection/boot/epoch/registration changes; no reset for seq/counter change.
- [ ] **Step 4: Implement apply/drain/poll order literally.** (1) Closed→False; otherwise capture one BOOTTIME now, drain, query-free view notification after changed batch, status/age even on early exits. (2) Retired pending→guarded retirement once, compose, return False. (3) Still-owned explicit PrepareRetry→False without retry/consume. (4) No pending: ordinary+deferred may `_tick_target(now)` once through gate then continue; no-deferred ordinary and **all poll** return after notification without target calculation. (5) Live pending evaluates frozen elapsed/readiness; apply at most once, or handle readiness-discovered retirement via (2). Return True only for application. In all paths no hidden new recovery writer.

`_poll_history` pumps nonblocking transport inside existing Files transient scope, invokes `apply_ready_frame(...,context="poll")`, updates status/age, and stays armed while pending frame or pending_work, including expired transport and queued/running/completed detail/Files. Deferred target alone neither arms poll nor permits poll submission. Connect measurement_work_requested once to _arm_poll; Files state-change handler also arms on work start. A successful application cannot unconditionally stop a pump that still has historical work. No per-detail timer; existing 100 ms poll and normal 1 s refresh remain.
- [ ] **Step 5: Switch every ordinary status writer to the single typed production choice.** Snapshot these ordered inputs and delegate to Task 11's `_choose_overview_status`, using exact str(history.view_state); the table below pins the result, not another implementation of the tree. `stall_eligible = _stalled or _retry_after is not None`; `busy=stall_eligible and history.local_read_in_flight is True`; `any_work=(_pending is not None or history.pending_work)`.

| Row | Predicate after earlier rows fail | Exact text |
| --- | --- | --- |
| 1 | typed refusal present | refusal.text verbatim |
| 2 | still-owned PrepareRetry | associated exact _prepare_error (`appio: <error>`) |
| 3 | busy | `history read has not completed (<state>); the reader is still busy` |
| 4 | future _retry_after | `history read has not completed (<state>); retrying in <n> s` |
| 5a | no applied frame and no target or no sample | `waiting for the first sample` |
| 5b | no applied frame otherwise | `loading history (<state>)` |
| 6a | displayed values and any_work | `updating (<state>)` |
| 6b | displayed values | `live` |
| 6c | successfully empty ranking | `no applications observed` |
| 6d | share or series pending | `loading measurements (<state>)` |
| 6e | failed dependency, none pending | `measurements unavailable: <first share, then series reason>` |
| 6f | otherwise | empty string |

Retry n=`(retry_after-now_ns+999_999_999)//1_000_000_000`, only future; expired floor+held reader is row3. Return the actual row ID with exact text and independently computed age. Text wrapper/updater capture **one** age-clock instant and pass it to that choice; updater sets both labels from that single result. Only rows3/4 set Files stall caption, clearing restores ordinary updating caption without query/footer changes. Replace direct history_state assignments in target/status/apply/show_ready and early returns; Task 11 already gives standalone view the same pure choice function without shell acquisition/stall inputs. Embedded detail refresh never overwrites shell-supplied status. No second tree or test-only branch counter.
- [ ] **Step 6: Wire the production clock once.** In run_interactive choose `age_clock_ns=time.monotonic_ns`; explicitly pass that callable to IodClient(clock_ns=...), HistoryRouter(clock_ns=...), CollectorWindow(age_clock_ns=...), then embedded AppIoWindow(clock_ns=...). BOOTTIME remains shell clock_ns and recovery target/floor. Native composition test must inspect real argument flow with unequal clocks, not a fixture injecting one identical fake everywhere.
- [ ] **Step 7: Run focused tests GREEN and existing recovery/collector tests** `python -m pytest -q tests/p3/test_collector_window.py`. Only expressly amended endpoint/pending/status expectations change; source comparisons cannot weaken frozen retry/probe/queued reader assertions. Task 15 exercises all S01–S21 rows and refusal traces after append callers exist.
- [ ] **Step 8: Commit.**

```bash
git add src/appio/ui/collector_window.py tests/p3/test_collector_window.py
git commit -m "feat(console): drive certified sources and compose status with independent age"
```

## Task 13: Use series metadata for append identities and finish integration fixtures

**Implementer tier:** Advanced

**Lane:** `presentation`
**Depends on:** Task 10, Task 11
**Specification:** §§6.2–6.3, 7.1, 8.2; §12.1 harness contracts, §12.5 `test_main_window_uses_data_identity_for_task_and_device_appends`.

**Files:**

- Modify: `src/appio/ui/main_window.py:923-984` (timeline callers), Task 9/11's model diff/standalone presentation integration if required by these exact callers
- Modify/Test: `tests/test_ui.py`
- Modify: `tests/live_overview_support.py` (complete integration fixture classes; no production debug API)

**Interfaces:**

- Consumes OverviewModel.requested_window/app_key/series_metadata/provenances, `SeriesMetadata(app_id, app_key, measured_window, tier, coverage)`, `retention_scope(app_key, window) -> RetentionScope`; chart `set_task_points(points, *, identity=None)`, `append_task_points(points, *, identity)`, DeviceLane analogous setters/appends; Task 9 retained observations and Task 11 typed measurement/refusal/age records.
- Produces `_apply_timeline(model: OverviewModel, previous: OverviewModel | None) -> None` using exact opaque tokens below. Legacy direct models without typed metadata use full setters with identity None. No query/label parsing is an identity source.
- Test-only `LiveOverviewHarness` composes production HistoryRouter, AppIoWindow, CollectorWindow, real IodClient when transport is tested, synthetic subjects A/B, stopped timers/explicit Qt driving, independent Clock age/recovery domains and default PEAK=None. Support exposes `apply(window: Window) -> None` (only ordinary production gate/application), `ordinary_refresh() -> None`, `poll() -> None`, `notify() -> None` (view.refresh_measurements only), `request(window: Window) -> None` (shell.show_window), `advance_ns(delta: int, *, domain: Literal["age", "recovery", "both"]="both") -> None`, `hold(kind: Literal["ranking", "series", "totals", "files", "coverage"]) -> None`, `release(kind) -> None`, `block_detail_computation() -> context manager`, `block_local_delivery() -> context manager`. It exposes router/view/shell/transport, getter/send/prepare/retire/apply/settle/notify order counters and worker charge observations through test spies. No fixture may complete a frame without production readiness or directly write an accepted record. `LocalPositionStores` adds backup-complete/closing-read barriers and committed mutation helpers. `ExactOnlyQuery` has independent exact outcomes, counts and optional identity variant. Fixtures clean up socketpairs/private stores/widgets/held Events through their own explicit teardown, never signal the host/PM.

- [ ] **Step 1: Add the exact caller test with raw share and rollup series.** Use applied since_boot boot-a through 180_000_000_000 ns, share coverage.tier=raw, Series tier/coverage rollup_60s with valid 60-second buckets, same endpoint and available attribution. Spy actual chart methods, assert both task/device identity use rollup_60s; no text-derived identity or share-tier substitution. Check same end/digest/first_seen/frame/cursor change does not enter identity; key/scope/start/series-tier/device changes do. Legacy direct model invokes full setter(identity=None).

```python
expected_task = ("task", key, retention_scope(key, requested), 0, "rollup_60s")
expected_dev = ("device", key, retention_scope(key, requested), 0, "rollup_60s", "8:0")
view._apply_overview(model)
assert task_calls[-1]["identity"] == expected_task
assert device_calls[-1]["identity"] == expected_dev
assert model.series_metadata.tier == "rollup_60s"
assert model.share.coverage.tier == "raw"
```

- [ ] **Step 2: Run** `python -m pytest -q tests/test_ui.py::test_main_window_uses_data_identity_for_task_and_device_appends` RED.
- [ ] **Step 3: Implement typed caller identities and persistent diff behavior.** `measured_start=0` for since_boot, exact range start for range; requested retention scope comes from model requested_window+external key; tier/start from series_metadata's measured Window/tier. Never include advancing measured end, snapshot, stamp, frame generation or sequence. Lifetime never plots a token. Call append with the full sequence when eligible data/identity changes; unavailable→success may append (chart rebuilds by row2), and legacy mode explicitly full sets. Include identity changes in timeline/lanes diff even when raw points match; reused DeviceLane dev changes reset identity. Keep donut/bars/KPI value diffing and widget identity. Use typed device_lanes_unavailable for withheld lanes, no task suspend metadata for device input.
- [ ] **Step 4: Complete the shared harness without running the final inventory.** Reuse pure factories from Task 1 and QueryTransportHarness from tests/live_overview_transport.py. Implement the precise helper API above as wrappers around production calls, real worker barriers and wire protocol; do not fake settlement with map writes or gate bypass. A computation barrier holds inside actual background reducer before it returns; a delivery barrier holds only queued delivery and remains a separate fixture. Capture observed ordering and capacity by spies on real admission/settlement/reap, not test-only counters incremented without production events. Add cleanup to every fixture path. This preparation is part of §12.1 and enables Tasks 14–16 to run independently from the plan plus their cited specification sections.
- [ ] **Step 5: Run the caller test GREEN and** `python -m pytest -q tests/test_ui.py -k 'main_window_uses_data_identity or identical_refresh or changed_shares or detail_notification'`. Validate only fixture construction/teardown smoke here; the final discriminator execution waits for Task 12 and is owned by Tasks 14–16.
- [ ] **Step 6: Commit.**

```bash
git add src/appio/ui/main_window.py tests/test_ui.py tests/live_overview_support.py
git commit -m "feat(ui): append timelines with series metadata identities"
```

## Task 14: Prove integrated router, transport, and source liveness

**Implementer tier:** Capable

**Lane:** `acceptance`
**Depends on:** Task 12, Task 13, Task 18
**Specification:** §§12.1–12.4, 12.8–12.9, especially D01/D02/D03/D05/D06/D07. This is the first full router/client/source discriminator pass after implementation sequencing is complete.

**Files:**

- Modify/Test: `tests/p3/test_live_overview_router.py`, `tests/p3/test_live_overview_client.py`
- Modify/Test: `tests/p3/test_collector_window.py` (three bracket race schedules, and source/deferred-context verification only)
- Modify if a named discriminator proves a contract defect: `src/appio/history_types.py`, `src/appio/history_validation.py`, `src/appio/iodclient.py`, `src/appio/history_router.py`, `src/appio/ui/collector_window.py` (only the corresponding already-specified behavior)
- Read-only fixtures: `tests/live_overview_support.py`, `tests/live_overview_transport.py`

**Interfaces:**

- Consumes the LiveOverviewHarness and QueryTransportHarness signatures reproduced in Global Constraints, registered LocalPositionStores barriers, `_settle(token: RequestToken, outcome: SettlementInput | None = None, *, retiring: bool = False) -> bool`, and the router/client source/retained/frame/transport signatures in that same injected catalog. Tests exercise real protocol association and production publication, not another task's private fixture assumptions.
- Produces executable integration assertions for **all 22 router, all 13 client and all 12 source-trigger tests**, preserving the names already defined. New names and cross-component observations are below; reports contain actual assertion/control evidence, never a new public production API.

**Exact additional router tests:** `test_failed_record_cannot_be_revived_by_pending_retry`; `test_remote_detail_liveness_across_ten_second_commit`; `test_held_local_detail_accepts_once_after_forward_frames`; `test_authority_change_retires_pending_frame_once_and_replacement_applies`. Extend all existing §12.2 tests from Tasks 1/4/6/7 to their full §12.2 observations rather than adding duplicate definitions.

**Exact additional client tests:** `test_query_timeout_keeps_transport_slot_until_terminal_or_retirement`; `test_expired_query_retires_at_request_specific_silence_deadline`; `test_cancelled_or_publication_retired_future_keeps_wire_occupant`; `test_late_expired_terminal_releases_without_second_publication_or_cache`; `test_malformed_result_is_not_cached_and_same_cursor_retry_sends`. Extend the remaining eight §12.3 tests already defined in Tasks 2/3/7 to actual local/remote/cache/owner-context routes.

**Exact additional collector tests:** `test_commit_during_backup_is_uncertified_with_one_replacement`; `test_commit_after_backup_before_closing_read_is_uncertified`; `test_commit_after_closing_read_is_caught_by_next_comparison`. Run the first four §12.4 router position tests and the five source/deferred-context tests established by Task 12, plus native-clock evidence there; do not rename or duplicate them.

- [ ] **Step 1: Add the normative 10-second slow-detail schedule.** Use age/recovery clocks and the real one-job wire peer. Rank admitted t=0.0, answered t=0.2; series issued t=0.2 and answered t=5.15 (4.95 s after issue); totals immediately issued after series terminal and answered t=10.10 (4.95 s after its issue). Begin B preparation at t=10.0, keep B pending when totals lands; B ranking must queue behind totals rather than impossible simultaneous wire work. PEAK None. Assert accepted A totals retains original Window/digest/stamp and becomes visible on notification before any one-second tick; applying B later retains A while successor pending.

```python
# At 10.0 s production request(B) has admitted B, totals(A) is the wire occupant.
assert h.shell._pending == b
assert h.router.part_state("totals", key, scope).requested_window == a
# At 10.10 s inject the correlated A-totals result then poll, not an ordinary refresh.
h.advance_ns(100_000_000, domain="both")
h.transport.reply(h.transport.only_future("totals"), share(91, a), snapshot="a" * 64)
h.poll()
record = h.router.retained("totals", key, scope, a.end_mono_ns)
assert record is not None and record.value.window == a
assert h.view._total_value.text() == "22 B"
assert h.view._read_value.text() == "17 B"
assert h.view._write_value.text() == "5 B"
assert h.shell._pending == b
```

The fixture sets the exact absolute instants 0.0/0.2/5.15/10.0/10.10; do not advance an extra ten seconds after prior clock movement. Calls above are the post-schedule assertions, not permission to turn one individual query into a >5 s request.
- [ ] **Step 2: Add two separately identified local arms.** `test_held_local_detail_accepts_once_after_forward_frames` uses parameter IDs `delivery_barrier` and `computation_barrier`. Delivery arm releases a computed A result after B applies and accepts A once. Computation arm holds a real detail **inside** reducer computation, then explicit supported request/committed source advance submits B; independently complete and apply B on frame executor **before** releasing A. Assert one A job/settlement/publication, original Window/digest and no shared-capacity stall. Capacity test holds two charged details+eight Files including retired jobs, changes selection repeatedly, never submits an eleventh background Future, and after drain frees one charge admits at most one replacement. A retired running frame still gates a second frame Future.
- [ ] **Step 3: Add authority and bracket schedules.** Local frame evidence for A completes while remote ranking held; lose/reverify same answering boot/epoch with new client generation before draining ConnectionLost. First drain marks A unavailable once; shell retires once, preserves last applied frame and arms floor once; before 30 s no replacement; at equality with actual reader free exactly one B prepare/apply/certificate. Repeated old successes/errors/drains cannot certify or re-retire. Same-authority seq/counter control retires zero times. Three distinct committed-store schedules hold during backup, after backup-before-closing, and after closing; first two display only their own uncertified data, never advance displayed_source, and produce one gated replacement; third certifies then next comparison detects new commit. Registration changes instead retire, not merely uncertify. Run with non-primary source too, without a transaction enclosing observations.
- [ ] **Step 4: Complete transport/selector/cache tests.** Expired Future immediately fails current detail/clears record but peer job holds slot; stream status/snapshots without extending request-specific deadline. At deadline-1 no forced retirement, at equality close old socket before successor; stale Future cannot retire new generation. Test late result and late error, cancellation, publication retirement, unrelated status reply and immediate cache/refusal as distinct arms. Cache miss retry at same cursor must send envelope1 invalid→zero cached/accepted, envelope2 valid→one accepted/cache, third read first hit→no third send. Reject owner-context candidate and evict only its exact entry version; a newer replacement survives.

Repeat the exact valid and single-fault unscoped process mappings from Task 2 on both pre-cache and settlement boundaries. To expose the **shared value predicate** independently of decoder checks, a boundary arm may fault-inject only the decoded candidate returned by from_wire while preserving real framing/request association, valid key/kind/snapshot and every other predicate; retain a separate genuine wire encode/decode positive. Removing only the shared Coverage-value predicate must make that arm's expected rejection fail at **both** callers, rather than be masked by a duplicate client/router check. Keep invalid-key-only and invalid-value-only controls separate. Actual totals/series external-ID positive and all simultaneous-device chronology controls must traverse both origins; no numeric ID repair hides a contradictory external key.
- [ ] **Step 5: Run the integrated inventory.**

```bash
python -m pytest -q tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py
python -m pytest -q tests/p3/test_collector_window.py -k 'commit_during_backup or commit_after_backup or commit_after_closing or unchanged_certified or pending_source_advances or recovery_transition_ownership or no_frame_poll or no_frame_deferred or native_composition'
```

Expected: every named observation passes; each held-work assertion is made before release and each failure clears before production notification. A new defect may be repaired only in this task's listed contract-owned modules, with focused failing assertion and restored pass; no scope expansion.
- [ ] **Step 6: Falsify the following mechanisms in separate disposable copies with passing controls.** Copy current working source/tests/config to a fresh temporary root, excluding `.git`, `.superpowers`, virtualenv/cache/build/runtime artifacts; do not mutation-edit the active worktree. Create the copy and capture an explicit import-proof command as follows; save its returned absolute path as test-runner state, rather than using a placeholder path in commands:

```python
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
copy_root = Path(tempfile.mkdtemp(prefix="appio-live-mutant-"))
shutil.copytree(Path.cwd(), copy_root, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", ".superpowers", ".venv", "venv",
                                             "__pycache__", ".pytest_cache", ".mypy_cache",
                                             ".codegraph", "build", "dist"))
mutant_env = os.environ.copy()
mutant_env["PYTHONPATH"] = os.pathsep.join((str(copy_root / "src"), str(copy_root / "tests")))
proof = subprocess.run([sys.executable, "-c",
    "from pathlib import Path; import appio.history_router as m; "
    "assert Path(m.__file__).is_relative_to(Path.cwd()); print(m.__file__)"],
    cwd=copy_root, env=mutant_env, text=True, capture_output=True)
assert proof.returncode == 0, proof.stdout + proof.stderr
pytest_argv = [sys.executable, "-m", "pytest", "-q", "-o",
               f"pythonpath={copy_root / 'src'}"]
```

Append the exact selected node ID to pytest_argv and invoke with cwd=copy_root/env=mutant_env. Retain artifact logs then remove the disposable directory in fixture `finally` cleanup. Verify every mutated import comes from the copy. Copy before changing, apply one mutation, require the named **assertion** failure (not import/collection), restore and rerun the identical control GREEN. The assigned task report records exact mutation, node/parameter/assertion, exit/result and restored run.

| Mutation | Required discriminator/control |
| --- | --- |
| Bypass external/request/selector validation before cache or settlement | malformed same-cursor retry cache count/send count and settlement single-fault invalid_result tests; valid associated answer control |
| Remove **only** unscoped process mapping Coverage-value predicate; leave AppKey/kind/request/other checks | value-only `unit:alpha.service`→`not a Coverage` arms in malformed-result retry and settlement tests fail; valid mapping control and restored run pass; key-only arm remains invalid |
| Bind detail owner to frame generation or clear records in prepare | scope_owned and exact t=10.10 remote liveness accepted-A Window/value assertions; held local arms control |
| Preserve failed record/restore on pending | failed_record_cannot_be_revived and current_failure clear-before-notification with independent other dependency/PEAK None |
| Drop frame authority retirement | authority_change_retires_pending_frame_once_and_replacement_applies unavailable/retirement/floor/B-application assertion |
| Share frame/background executor | held local `computation_barrier` B applies while A held; `delivery_barrier` alone is not a kill |
| Bypass local worker binding/variant checks | local_outcomes_share_settlement's worker_id/request/frame/part/Files single-fault arms fail before evidence publishes |
| Release scheduler on Future.done/cancel/publication retirement | timeout/cancelled_or_publication_retired peer occupancy and unsent successor assertions |
| Omit seq cache key or restamp answered source | full_cursor_cache_key original position and next-seq miss; injected older-cache frame cannot certify |
| Observe only primary/latest rows, ignore deletion/update, keep observer transaction, or omit registration epoch | exact vector mutation paths, old-row/rollup/epoch/receipt/non-primary updates, active opening/closing transaction refusal, same-counter replacement, and distinct bracket schedules |
| Trigger on endpoint alone / allow poll deferred submission | unchanged_certified no prepares/applied Window unchanged; no_frame_deferred ordinary=1 versus poll=0 plus held/floor/explicit retry controls |

No hidden second guard may mask the value-only shared-validator mutation; prove the exercised boundary as above. Rejected/equivalent/unexercised mutations are reported accurately, never counted as kills.
- [ ] **Step 7: Commit test implementation and any evidenced contract fixes.** Stage only files actually changed from this task's Files block; do not stage temporary probes/reports or unchanged production paths.

```bash
git add tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py tests/p3/test_collector_window.py
git commit -m "test(history): discriminate transport ownership source certification and slow detail liveness"
```

If production repair was necessary, include those exact repaired files in that commit's explicit git add before committing.

## Task 15: Prove retained independence, typed refusal lifecycle, and all status rows

**Implementer tier:** Capable

**Lane:** `acceptance`
**Depends on:** Task 14
**Specification:** §§7, 9, 10, 12.5, 12.7, 12.9 (D04/D08/D09/D10/D11). Preserve recovery §§4.4–4.5 and direct refusal duties.

**Files:**

- Modify/Test: `tests/test_ui.py`, `tests/p3/test_collector_window.py`
- Modify only for a proved named contract defect: `src/appio/ui/render_model.py`, `src/appio/ui/main_window.py`, `src/appio/ui/collector_window.py`
- Read-only: shared integration/transport support from Tasks 1/3/13

**Interfaces:**

- Consumes production LiveOverviewHarness/ExactOnlyQuery; optional five-method retained capability and exact_answer_identity; reset_query_authority/refresh_measurements/measurement_status/show_overview_age; typed OverviewModel/SeriesMetadata; `_compose_overview_status_choice(now_ns: int, age_now_ns: int) -> OverviewStatusChoice`, whose row/text/age are **actual production choice**, plus wrapper/updater using one captured age instant.
- Produces all **18 required UI tests** (§12.5's 15 and §12.7's first 3) and **all 10 required collector status/clock tests** (§12.7 remaining names). Extend existing named definitions, retaining separate preservation and discriminator parameters.

**Exact additional UI tests:** `test_share_outputs_survive_series_failure_or_pending`; `test_series_device_incomplete_only_withholds_device_lanes`; `test_share_failure_keeps_independent_task_lane`; `test_undefined_residual_does_not_remove_device_total_or_bars`; `test_lifetime_detail_keeps_totals_without_chronology`. All other §12.5 names are established in Tasks 8/9/13; all three pure age names are in Task 11 and must be extended/run here.

**Exact additional collector tests:** `test_live_status_initial_frame_pending`; `test_live_status_first_and_retry_details_pending_without_peak`; `test_live_status_tree_selects_exactly_one_reachable_row`; `test_refusal_presentation_lifecycle_preserves_age_and_recovery_owners`; `test_live_status_unsupported_lifetime_and_unranked_subject_are_settled`; `test_live_status_refusal_and_operational_error_precedence`; `test_live_status_early_retirement_expired_floor_still_busy`; `test_age_label_survives_busy_retry_refusal_and_idle_status`; `test_clock_only_age_update_issues_no_queries_or_reacceptance`. The tenth, `test_native_composition_binds_one_age_clock_domain`, already exists from Task 12 and remains mandatory.

- [ ] **Step 1: Complete all independent-output discriminator parameters through actual retained accessor/adapter/view.** A=since_boot boot-a through 20_000_000_000 ns; B=same scope through 30_000_000_000 ns; PEAK=None; A-share read17/write5/total22/device14 with matching devices/residuals; first_seen=1 s, observation t=3 s. Expected retained age `oldest answer 2 s ago`, exact disclosure `; share measured: since_boot boot-a through 20000000000 ns`. Preserve identity/Window/stamp/widget assertions and test each dependency separately.

| Named test | Required parameter IDs and observations |
| --- | --- |
| share_outputs_survive_series_failure_or_pending | `preserve_exact`: exact share outputs survive; `retained_A_pending_B`: A TOTAL22/READ17/WRITE5/DEVICE14/donut/bars/residual remain with failed/pending series, measured disclosure and original stamp, no fabricated lane |
| series_device_incomplete_only_withholds_device_lanes | `preserve_exact`: share DEVICE/bars/residual and task lane remain; `series_coverage_reason`: exact `; device lanes: incomplete`, metadata incomplete, zero lanes/more, DEVICE14 unchanged |
| share_failure_keeps_independent_task_lane | `preserve_exact`: task lane remains; `failure_pending`: A-series remains while B-share fails then retry pending, all share outputs unavailable and real retained share None, original A-series Window/stamp/disclosure |
| undefined_residual_does_not_remove_device_total_or_bars | `preserve_exact`: defined device total/bars, no reconciliation; `retained_A_pending_B`: same retained A DEVICE14/bars with residual None and unchanged Window/stamp/disclosure |
| lifetime_detail_keeps_totals_without_chronology | `preserve_exact`: totals/donut/boot-qualified bars, no chronology; `failure_pending`: accepted lifetime share→current failure→pending retry keeps TOTAL/READ/WRITE/DEVICE unavailable and no restored age until new success |

Run both mixed-endpoint directions, new share device-incomplete and undefined-device-total removal in the same application, no stale ranking PEAK, extended direct metadata endpoint/coverage negatives without ranking provenance, optional identity malformed/raising/None outcomes, exact authority reset with identical lifetime, and notifications with **zero** getters/ranking/Files/admission/prepare/probe calls. A green `preserve_exact` never proves retention or supplies its RED/kill evidence.
- [ ] **Step 2: Materialize `status_cases` with exact IDs S01–S21.** Defaults: recovery now=40 s, age now=40 s, state ready, applied frame/target/sample, nonempty ranking, no refusal/retry/stall/floor/reader/pending/work, share/series absent, no displayed answers. `values` is accepted share first_seen=38 s with has_displayed_values=True. `peak` is only current ranking with that stamp/defined PEAK and has_displayed_values=False. R=typed exact refusal, E=still-owned PrepareRetry+associated error. One production choice call per case, and independent real-label updater assertion; zero queries/prepares/recovery writes. No counting overlapping raw predicates.

| ID | Override | Expected row | Exact text | Exact age |
| --- | --- | --- | --- | --- |
| S01 | shell_prepare launch R; values; work | 1 | `Launch window disabled: device and file activity are not stored per launch.` | `oldest answer 2 s ago` |
| S02 | direct_view launch R after cleanup; E=`appio: disk read failed` | 1 | `Launch window disabled: device and file activity are not stored per launch.` | None |
| S03 | shell_prepare unsupported_window long-range R; values | 1 | `Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.` | `oldest answer 2 s ago` |
| S04 | E=`appio: disk read failed`; values; work | 2 | `appio: disk read failed` | `oldest answer 2 s ago` |
| S05 | retry_after=now; reader=True; state unavailable; values | 3 | `history read has not completed (unavailable); the reader is still busy` | `oldest answer 2 s ago` |
| S06 | stalled=True; reader=True; pending_since=10 s; pending; state loading | 3 | `history read has not completed (loading); the reader is still busy` | None |
| S07 | retry_after=now+1_000_000_001 ns; reader=False; state unavailable; values | 4 | `history read has not completed (unavailable); retrying in 2 s` | `oldest answer 2 s ago` |
| S08 | no applied frame/no target; initial-frame test adds fresh target/no-sample arm | 5a | `waiting for the first sample` | None |
| S09 | no applied frame; target/sample; pending; state loading | 5b | `loading history (loading)` | None |
| S10 | values; detail work | 6a | `updating (ready)` | `oldest answer 2 s ago` |
| S11 | values; pending frame; state loading | 6a | `updating (loading)` | `oldest answer 2 s ago` |
| S12 | values; no work | 6b | `live` | `oldest answer 2 s ago` |
| S13 | successful empty ranking; Files work | 6c | `no applications observed` | None |
| S14 | share pending; series absent; PEAK undefined | 6d | `loading measurements (ready)` | None |
| S15 | share pending; series failed series_bad | 6d | `loading measurements (ready)` | None |
| S16 | share failed share_bad; series failed series_bad | 6e | `measurements unavailable: share_bad` | None |
| S17 | share absent; series failed series_bad | 6e | `measurements unavailable: series_bad` | None |
| S18 | lifetime series ok/chronology unavailable; share failed share_bad; PEAK undefined | 6e | `measurements unavailable: share_bad` | None |
| S19 | file-only subject/nonempty ranking; both details absent | 6f | empty string | None |
| S20 | peak only; share pending/no effective series | 6d | `loading measurements (ready)` | `oldest answer 2 s ago` |
| S21 | peak only; both details absent | 6f | empty string | `oldest answer 2 s ago` |

For `test_live_status_tree_selects_exactly_one_reachable_row`, parameterize pytest.param(case,id="Sxx") for those exact 21 IDs; expected row/text are literal fixture fields from this table. A nonempty status is visible; an age is visible independently. Core body:

```python
before = snapshot_query_prepare_and_recovery_state(shell)
choice = shell._compose_overview_status_choice(case.now_ns, case.age_now_ns)
assert (choice.row, choice.text, choice.age_text) == (case.row, case.text, case.age)
assert snapshot_query_prepare_and_recovery_state(shell) == before
shell._update_overview_status(case.now_ns)
assert shell.history_state.text() == case.text
assert (not shell.history_state.isHidden()) is bool(case.text)
assert shell.view.overview_age.isHidden() is (case.age is None)
assert shell.view.overview_age.text() == (case.age or "")
```

Define the local pure snapshot helper to return observed query/prepare counts plus values of `_pending`, `_pending_since`, `_displayed`, `_stalled`, `_retry_after`, `_deferred_target`, `_deferred_origin`, `_prepare_retry`, `_prepare_error`, `_refresh_after_apply`; it must not read/write store state. Use these exact visibility assertions in the executable test.
- [ ] **Step 3: Implement each named status test's concrete schedules.** Initial=S08/S09 including target-with-no-sample fresh arm. First/retry pending=S14 plus actual success→failure→pending with no restored record. Unsupported lifetime/file-only=S18/S19. Refusal/error precedence=S01–S04. Early retired held reader=S05 plus explicit B prepare count0 even at expired floor. Independent QLabel survival=S01/S05/S07/S10/S12/S21 and hidden controls S06/S08/S14. Clock-only=S12 at now40 then41 with text live unchanged, age2→3 s, queries/prepares/acceptances0. Native unequal-clock test remains exactly Task12's production composition, not one shared fake clock substituted everywhere.
- [ ] **Step 4: Run all three refusal lifecycle traces in the named lifecycle test.** Timers stopped, PEAK undefined, quantities as Step1. Preserve exact typed origin/Window, marker/error owner, attempts/accepted prepares/sends and labels at every step:

1. A-share accepted/stamped t0. Explicit launch at t2: attempts1, accepted0, sends0, shell_prepare/launch, exact A22 status and age2. Ordinary tick t3 re-encounters precheck: attempts2, accepted0/sends0, same refusal age3. Notification t4: counts unchanged, refusal/age4. Explicit supported B t5: attempts3, accepted1, shell refusal/error cleared, updating(loading)/age5. Apply B t6 while successor detail held: updating(ready)/age6 using A. Both B details newly accepted t7: live/age0 with new identities, not A restamped. Refusal creates no floor/PrepareRetry.
2. A accepted t0. Supported explicit B raises OSError("disk read failed") t2: attempts1, accepted0, PrepareRetry(B,explicit,2s), exact appio error/age2, no floor. Tick+poll t3 preserve attempts1/marker/age3. Named show_window(B) t4 only retry: attempts2, accepted1, original seam clears marker/error, updating(loading)/age4. Separate different-window explicit request supersedes without retrying old B. Notification never clears operational owner.
3. Direct set_window(launch) t2 clears Overview+Files+selection before emits direct_view/launch, hides age; status-only tick/notification and late old completion restore nothing and send no refusal/detail query. Supported direct adoption A t4 clears/emits None before model; new accepted identity-bearing share in new lifetime has stamp4/age0/live with settled work and refusal widget cleared. Count frozen one ranking/series/totals observation per ordinary refresh; notification adds zero getter observations. Shell `_displayed` cannot recreate the directly cleared view before adoption.

- [ ] **Step 5: Run integrated tests.**

```bash
python -m pytest -q tests/test_ui.py tests/p3/test_collector_window.py
```

Any contract fix uses this task's production allowlist and exact failing assertion; keep preservation/control outcomes distinct. Verify label visibility independent of parent visibility with isHidden/isVisibleTo as appropriate for stopped offscreen widgets, without weakening explicit shown/hidden semantics.
- [ ] **Step 6: Run isolated mutations with passing controls/restoration.** Use Task14's disposable-copy/import verification procedure. Exact required mappings:

| Mutation | Required failing discriminator assertion |
| --- | --- |
| Clear A-share on forward prepare / treat pending exact B as error | share_outputs `[retained_A_pending_B]` and undefined_residual `[retained_A_pending_B]`: TOTAL22/READ17/WRITE5/DEVICE14 as applicable; A Window/stamp/exact disclosure preserved |
| Retain failed dependency / restore on pending | share_failure `[failure_pending]`: all share outputs unavailable, retained share None, A-series bytes/Window/stamp; lifetime `[failure_pending]`: no resurrected totals/device/age |
| Use share tier for series identity / omit series reason | series_device `[series_coverage_reason]`: zero lanes/more, exact incomplete clause, metadata incomplete, DEVICE14; actual append identity contains rollup_60s while share raw |
| Relabel retained A as B / restamp on display | both retained_A_pending_B arms and both mixed-endpoint directions: exact A Window/stamp/disclosure and zero lanes with exact endpoint reason |
| Omit metadata or bypass extended direct endpoint/coverage checks | direct_metadata test future-endpoint-without-ranking-provenance and incomplete-series-with-lanes ValueError arms; all-default legacy positive still passes |
| Omit exact adapter authority reset | exact_only_authority_reset unchanged-lifetime old record/stamp absent, late old notify zero, epoch2 stamp4/age0; same-authority forward control preserved |
| Truncate/round age or use youngest/unknown omission | raw seven-boundary age test plus oldest/identity-less contributor and clock-only no reacceptance |
| Swap status precedence, take series failure first, count predicates instead of choose | S10/S11 values+work→6a; S15 pending+failed→6d; S02 direct refusal+PrepareRetry→1; S16 dual failed→share_bad; row/text/visibility are production results with zero side effects |
| Treat any applied frame/unsupported lifetime series as displayed values / concatenate age into status | S13/S18/S19/S21 and separate age QLabel checks; frozen refusal/busy/retry text remains exact |
| Clear refusal from tick/notification or accept blocked request as adoption | three typed lifecycle traces' attempt/send/marker/refusal/age assertions |

Every kill names the exact collected node/parameter/assertion, passing control and restored rerun; baseline-green preserve_exact is not credited for new mechanisms.
- [ ] **Step 7: Commit test implementation and any explicit contract fixes.**

```bash
git add tests/test_ui.py tests/p3/test_collector_window.py
git commit -m "test(console): discriminate retained dependencies refusal lifecycle and status rows"
```

Include an explicitly named repaired production file before commit only if it changed under this task's allowlist; no generated screenshots or controller reports are staged.

## Task 16: Prove append costs and finish the exact acceptance gates

**Implementer tier:** Capable

**Lane:** `acceptance`
**Depends on:** Task 15
**Specification:** §§8.3–8.7, 11, 12.6, 12.9 (D10/D11). This task implements the remaining counted chart acceptance tests and the required gate evidence; it adds no feature or production instrumentation API.

**Files:**

- Modify/Test: `tests/test_charts.py`
- Modify only for an evidenced §8 defect: `src/appio/ui/charts.py:99-691`
- Read-only: all other source/tests/design/specification/plan; screenshots, collection output, mutation copies and final evidence go to the assigned controller run/report paths, not new repository deliverables

**Interfaces:**

- Consumes ChartCurve(side,run_index,vertices), unchanged ChartColumn/ChartSegment, full setters/append methods, `columns()/curves()/segments()`, retained private group bounds/durations/run interval caches and actual production aggregation/paint calls.
- Produces `AppendProbe` test fixture with `validate_point`, `compare_point`, `aggregate_member`, `scale_column`, `bound_read`, `bound_write`, `geometry_column`, `interval_visit`, `vertex_emit`, `closure_emit`, promotion-merge counts and repaint observations. Spy actual primitives/raw access on **all** paths, not a hook only on the intended fast path. Snapshot equality/validation has its own phase; summing/building run membership/emitting geometry cannot hide in that phase. No public debug interface or elapsed-time performance oracle.
- Exact additional tests: `test_live_grouping_is_minimal_power_of_two_and_promotes_bounds`; `test_live_append_cost_counts_fragmented_output_and_cached_aggregation`. Extend all 18 other §12.6 tests from Task10 and run the complete **20-name** chart inventory.

- [ ] **Step 1: Add the four fixed AppendProbe operations.** Exact plot W=8, positive height, raw ten-second buckets, identity unchanged, nonzero read/write=1/2 per original bucket, all optional peaks/wall times None, no suspension. Reset counters after initial setter. Tail replacement changes only last read/write to5/1. Dense starts i*10_000_000_000; fragmented starts i*20_000_000_000. Count invocations/visits, not unique indices. Assert outputs independently of work counters.

| Fixture/operation | Exact/capped observations |
| --- | --- |
| Dense31 replace index30 | g4,C8,bounds `(0,3),(4,7),...,(24,27),(28,30)`; validation31, comparisons<=124; raw aggregate trace exactly `[28,29,30]` once each, no earlier index; no promotion-bound copy; scale8; read35/write61; E8,R2,V16; two fills length11; geometry_column<=16, interval_visit<=16, vertex_emit16, closure_emit6, total geometry<=54; paint raw visits0 |
| Dense32 append index32 | g4,C8→g8,C5; exact bounds `(0,7),(8,15),(16,23),(24,31),(32,32)`; validation33, comparisons<=132; aggregate trace exactly `[32]`; read33/write66; bound reads<=9 two-integer bounds, writes<=5 bounds/10 output integers; zero member-index-vector copies; merge-input interval visits<=9; E5,scale5,R2,V10; geometry_column<=10, interval_visit<=10, closure6,total geometry<=36 |
| Fragmented31 replace index30 | g4,C8; aggregate `[28,29,30]`; validation31, comparisons<=124; scale8; read35/write61; E31,R62,V124; every fill length5, closure186; geometry_column<=16, interval_visit<=62, vertex_emit124,total geometry<=388; every run inset support checked; preserve E/V>W |
| Dense31 equal input | validation31, comparisons<=124; aggregate/scale/promotion/geometry/repaint **all zero** |

Grouping test additionally checks W8 lengths8/9/16/17/32/33 → g1/2/2/4/4/8 and C8/5/8/5/8/5, exact bytes. Instrument bounds as pairs, not arrays of member indices. Geometry ceiling is `2*C+2*E+V+3*R`; promotion interval merging counted separately.

```python
chart.set_task_points(dense31, identity=identity)
probe.reset()
changed = dense31[:-1] + (replace(dense31[-1], read_bytes=5, write_bytes=1),)
chart.append_task_points(changed, identity=identity)
assert probe.aggregate_member_indices == [28, 29, 30]
assert probe.validation_visits == 31
assert probe.comparison_visits <= 124
assert probe.scale_column_visits == 8
assert sum(c.read_bytes for c in chart.columns()) == 35
assert sum(c.write_bytes for c in chart.columns()) == 61
assert sum(len(c.vertices) for c in chart.curves()) == 16
assert [len(s.polygon) for s in chart.segments()] == [11, 11]
assert probe.geometry_events <= 54
probe.reset()
chart.grab()
assert probe.aggregate_member_indices == []
```

These are test fixture counters backed by actual production operations; define their names exactly as shown and count alternate full-rebuild reducers too. `dense31` is the exact factory defined in this step, with plot_rect W asserted before resetting counters.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_charts.py -k 'live_grouping or live_append_cost'`. If implementation already meets the contract, record GREEN and falsify below; do not fabricate RED from a preservation control.
- [ ] **Step 3: Make only evidenced §8 fixes, then run all chart tests.** Reaggregation trace/geometry/support failures are contract defects even when output bytes match. Preserve every raw equality field and all 11 top-down branches, independent scale maxima after append/promotion/tail, lowered/removed peaks, None versus zero, suspend-then-append, one-fragment ticks, two/eight-fragment monotone inset and no bridging, collapsed/full resize resets, setters and unavailable clearing. O(n) validation/comparison cannot absorb forbidden aggregate/run work. Run `python -m pytest -q tests/test_charts.py` GREEN.
- [ ] **Step 4: Run disposable chart mutations and restore controls.** Apply one at a time in Task14's isolated import-verified copy: full-prefix aggregation→exact raw trace `[28,29,30]`/`[32]` fails; membership-vector promotion→two-integer bounds/read<=9/write<=5 assertions fail; collapse runs to W/envelope→fragmented E31/R62/V124 and inset support fail; bridge hole→curve+fill support tests fail; retain lowered/removed peak or old scale→tail/independent half-height assertions fail; change row ordering/ignore wall/None/peak/suspend fields→the corresponding separately named row test fails. Passing baseline/control and restored rerun required per claimed kill, no import failure credit.
- [ ] **Step 5: Verify the complete named inventory and parameter cases.** Run collect-only and compare **source definitions and collected nodes**, not just a total count. The following executable gate extracts the 91 names from §§12.2–12.7, requires each top-level definition exactly once across the five prescribed files, and checks S01–S21 and required preservation/discriminator IDs. It changes no source/test file:

```bash
python -m pytest --collect-only -q
python - <<'PY'
import ast
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys
spec = Path('docs/superpowers/specs/2026-11-15-live-overview-totals-spec.md').read_text()
section = spec.split('### 12.2 ', 1)[1].split('### 12.8 ', 1)[0]
required = re.findall(r'^\| `(test_[^`]+)` \|', section, re.M)
assert len(required) == len(set(required)) == 91
files = ['tests/p3/test_live_overview_router.py', 'tests/p3/test_live_overview_client.py',
         'tests/p3/test_collector_window.py', 'tests/test_ui.py', 'tests/test_charts.py']
definitions = Counter()
defined_in_file = {}
for name in files:
    tree = ast.parse(Path(name).read_text(), filename=name)
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    definitions.update(names)
    defined_in_file[name] = set(names)
assert {n: definitions[n] for n in required if definitions[n] != 1} == {}
inventory = spec[spec.index('### 12.2 '):spec.index('### 12.8 ')]
blocks = {int(n): re.findall(r'^\| `(test_[^`]+)` \|', body, re.M)
          for n, body in re.findall(r'^### 12\.([2-7]) [^\n]*\n(.*?)(?=^### |\Z)',
                                    inventory, re.M | re.S)}
expected_files = {
    files[0]: blocks[2] + blocks[4][:4],
    files[1]: blocks[3],
    files[2]: blocks[4][4:] + blocks[7][3:],
    files[3]: blocks[5] + blocks[7][:3],
    files[4]: blocks[6],
}
for path, names in expected_files.items():
    assert set(names) <= defined_in_file[path], (path, set(names) - defined_in_file[path])
run = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', *files],
                     text=True, capture_output=True)
assert run.returncode == 0, run.stdout + run.stderr
nodes = [line.strip() for line in run.stdout.splitlines() if '::test_' in line]
collected = {node.split('::')[-1].split('[', 1)[0] for node in nodes}
assert set(required) <= collected, sorted(set(required) - collected)
status = 'test_live_status_tree_selects_exactly_one_reachable_row'
for i in range(1, 22):
    assert any(f'::{status}[S{i:02d}]' in node for node in nodes), i
arms = {
 'test_share_outputs_survive_series_failure_or_pending': ['preserve_exact', 'retained_A_pending_B'],
 'test_series_device_incomplete_only_withholds_device_lanes': ['preserve_exact', 'series_coverage_reason'],
 'test_share_failure_keeps_independent_task_lane': ['preserve_exact', 'failure_pending'],
 'test_undefined_residual_does_not_remove_device_total_or_bars': ['preserve_exact', 'retained_A_pending_B'],
 'test_lifetime_detail_keeps_totals_without_chronology': ['preserve_exact', 'failure_pending'],
 'test_held_local_detail_accepts_once_after_forward_frames': ['delivery_barrier', 'computation_barrier'],
}
for name, ids in arms.items():
    for parameter in ids:
        assert any(f'::{name}[' in node and parameter in node for node in nodes), (name, parameter)
print('91 required definitions and collected names present; required discriminator/status parameters present')
PY
```

Also compare each name's file ownership with §12: §12.2 router18 + §12.4 router4 =22; §12.3 client13; §12.5 UI15 + §12.7 UI3 =18; §12.6 charts20; §12.4 collector8 + §12.7 collector10 =18; total91. Do not move names to an unrelated file to satisfy mere presence.
- [ ] **Step 6: Run focused inventory, full suite and exact development typecheck in order.** With the repository offscreen environment from Global Constraints:

```bash
python -c 'import sys, importlib.metadata as m; print(sys.version); print({p: m.version(p) for p in ("PyQt6", "pytest", "pytest-qt", "mypy")})'
python -m mypy --version
python -m pytest -q tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py tests/p3/test_collector_window.py tests/test_ui.py tests/test_charts.py
python -m pytest
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/history_types.py src/appio/history_validation.py src/appio/history_router.py src/appio/iodclient.py src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py
```

Record actual interpreter/checker/library versions, commands, results/counts/skips and revision; missing evidence is `not run`, never an inferred PASS. theme.py/files_view.py are check targets only, outside production edit scope. Preserve existing A22/file-only/DEVICE/Files ownership/recovery regression suite, and full-suite control; no privileged/live daemon/service gate is introduced.
- [ ] **Step 7: Inspect offscreen grabs and the amendment inventory.** Use existing grab fixtures and shared LiveOverviewHarness widgets to capture mirrored streaming curves, independent readable overview_age, exact visible status/reason, and persistent Overview/Files navigation into controller artifacts. Inspect pixels plus numeric/support/byte/state tests; screenshots alone prove no scheduler/certification/bridge invariant. Compare `git diff --name-only 6624162ea8594021de74c0bae5ec26fedee36e30...HEAD` plus `git diff --name-only` and `git diff --cached --name-only` to the production eight-module allowlist (test/fixture changes are separately allowed above); compare changed/new symbols/record fields against §11's A01–A18 and §13 D01–D11. No contract/store/query/protocol/schema/dependency or forbidden runtime changes. Collect mutation/control evidence from Tasks14–16 in the assigned controller report, not a new repository report document.
- [ ] **Step 8: Commit final counted tests and any evidenced chart-only fix.**

```bash
git add tests/test_charts.py
git commit -m "test(charts): enforce append work bounds and complete live Overview gates"
```

If chart code changed, explicitly add `src/appio/ui/charts.py` before committing. Return actual final gate/inventory/mutation/visual/amendment evidence to the deterministic controller/PM; do not execute implementation or integrations from this planning session.

### Specification coverage and final review index

This is part of Task16's acceptance brief, not another scheduler task. It is also the planning self-review map: §§1–2→1/2; §§3–4→4/5/6/7; §5→2/3/6/7; §6→9/12; §7→8; §8→10/13; §9→11/12; §10 and §§12.1–12.8→14/15/16; §12.9 gates→14/15/16; §13 exclusions→Global Constraints. Amendment coverage: A01/A02/A05/A06→10/13/16; A03/A04→6/8/9/15; A07→11/12/15; A08→5/6/12/14; A09/A10→1/4/6/14; A11→1/8/9/13/15; A12/A18→5/14; A13→9/12/14; A14/A15/A16→2/3/6/14; A17→7/14. D01→6/12/14; D02/D03→4/6/7/14; D04→1/8/13/15; D05/D06→2/3/6/14; D07→12/14; D08→11/12/15; D09→9/11/15; D10→15; D11→10/12/15/16. All execution evidence is prospective until those tasks run.
## Task 17: Repair P3 fixture clock and production settlement ownership

**Implementer tier:** Capable

**Lane:** `regression`
**Depends on:** Task 12, Task 13
**Specification:** §§1.1–1.2, 3.1–3.4, 5.1–5.4, 6, 9.1, 12.1–12.4 and 12.8–12.9 (D01/D02/D03/D05/D06/D07), and 13; design §9 and the preserved router accounting/exact-query contracts; recovery §§4.4–4.5. This is an implementation/plan-ownership correction plus a fixture and acceptance-consumer repair, under the unchanged approved contracts.

**Files:**

- Modify: `src/appio/history_router.py`, limited to private routing, detail ownership, settlement and associated cache rejection.
- Modify/Test: `tests/p3/acceptance_harness.py:1-674`, `tests/p3/test_history_overlap_entry.py:1-228`, `tests/p3/test_p3_acceptance.py:1-224`, `tests/p3/test_p3_adversarial.py:1-468`; additionally `tests/p3/test_live_overview_router.py` only as needed for deterministic focused regression coverage.
- Read-only: `src/appio/iod.py`, `src/appio/iod_history.py`, `src/appio/iodclient.py`, `src/appio/ui/collector_window.py`, `tests/p3/test_iodclient.py`, `tests/p3/test_history_router.py`, and the approved design/specification. Read the pre-feature `6624162ea8594021de74c0bae5ec26fedee36e30:src/appio/history_router.py` `_app_parts` and `_apply_response` ownership/fallback paths.
- Keep public interfaces, Files/protocol code, producer, daemon payload schema, socket framing, fixture/durable store schema, dependencies, sampling, device accounting and production poll behavior unchanged. `theme.py` and `files_view.py` remain typecheck targets, not edit scope.

**Interfaces:**

- Consumes the real `P3Harness` fixture lifecycle: `enable()`, `open_gui()`, `pump()`, `until()`, `settle()`, `rpc()`, `query()`, and `wire()`. The harness must continue to use its real daemon, AF_UNIX socket, `IodClient`, `HistoryRouter`, `CollectorWindow`, SQLite fixture, and original daemon payload assertions.
- Consumes `IodClient.query_transport(future) -> QueryTransportState` and `retire_query_connection(future) -> bool`; observe the phase and request identity of every router-owned historical Future instead of inferring completion from `Future.done()` alone. A direct test-owned RPC is allowed only after the shell/router and every associated historical operation have reached terminal ownership.
- Consumes `HistoryRouter.drain() -> bool`, `HistoryRouter.ready(window) -> bool`, `HistoryRouter.pending_work`, `CollectorWindow.refresh()`, `CollectorWindow.apply_ready_frame()`, and the existing production poll/readiness path. Do not add a synchronous production getter, a fixture-completed record, a fake successful response/certificate, a global scheduler bypass, an increased timeout, or a `query_busy` retry that hides unsolicited overlap.
- Produces one test-only bounded settlement observation, if a helper is needed, that records progress, submitted request identities, and per-request transport phases. It drives one initial production `refresh()` (including its normal probe), never resubmits probes while waiting, never calls direct detail getters, and never fabricates readiness. After an expired retry floor, it may invoke the ordinary `_tick_target()` gate only when its held reader is free and the gate is eligible, with that condition re-evaluated after subsequent real pumps. Poll itself remains drain/notify only. The helper must stop with a diagnostic when GUI timers/probes keep enqueueing work instead of looping forever.
- The supplied client age clock is forwarded to `IodClient` exactly once. The fixture has separate BOOTTIME recovery and MONOTONIC age/transport clocks. In the production `run_interactive` factory, preserve the supplied `clock_ns` already present in `IodClient` kwargs; `client_factory` must not pass a second `clock_ns=` alongside `**kwargs`. In `P3Harness.open_gui()`, use one test-controlled age-clock callable for `IodClient(clock_ns=...)`, `HistoryRouter(clock_ns=...)`, and `CollectorWindow(age_clock_ns=...)`, while `CollectorWindow(clock_ns=...)` remains the recovery clock. Preserve the daemon-only `HistoryRpc` execution-clock partial at `P3Harness.__enter__` and the overlap-entry test; it controls the daemon query budget and is not the duplicate keyword source. Do not collapse BOOTTIME recovery and MONOTONIC age/transport domains.

- Never execute `reboot`, `shutdown`, `poweroff`, `halt`, `kexec`, any `systemctl` power or sleep verb, `init 0|6`, or `telinit 0|6`; never restart or stop the display manager or `graphical.target`; never terminate your session or signal the process tree that hosts the PM. If you conclude a host transition is required, write `reboot-required.json` with `{schema: 1, trigger, evidence: [{path, sha256}], provenance: {run_id, lane, attempt, dispatch_key, observed_boot_id}}`, then report it and stop.

- Preserve exact ownership when routing lazy detail. The selected *applied* frame's validated `LocalFrameOutcome` supplies the exact requested Window's existing segments, local-history and overlap evidence, together with the actual winning ranking origin and source registration/authority association. Keep a private immutable association for an admitted detail token and its exact Window before a subsequent `prepare()` resets router-wide scratch flags. Keep the specification's exact `RequestToken`, `LocalFrameOutcome`, neutral/public record and wire field inventories unchanged, and release the private association once it is no longer needed and its bound work has been settled or retired and reaped. A frame or authority being prepared is not evidence that the displayed Window is remote-only. A remote detail is eligible only when the same exact Window's ownership and ranking selection permit the pre-feature remote path. When the selected frame falls back to local ranking, route both totals and series for that selected subject through the existing `_read_local_part` worker and single `_settle` writer, preserving the local reducer's overlap exclusions, coverage, exact Window and independent dependency behavior. Remote-only valid totals and series must still work. Do not wait for detail in frame readiness.
- Check that association again at remote detail settlement before publishing or notifying, as well as at scheduling. Reuse `_settle`'s request/Future/generation/authority checks and `IodClient.reject_query_result(future)` for exactly that Future's cached entry when a current candidate fails the ownership predicate. Reap the real transport and settle that rejected current candidate through the existing `invalid_result` failure arm, clearing its record before notification. Do not retry or admit a successor from a failure notification. A later ordinary refresh or explicit action may issue the same exact request through the existing local worker/settlement path when the proven route is local. A real local exception follows the existing current-failure path. A superseded or retired success/failure is ignored under §3.4 and cannot publish or clear a newer record. A candidate rejected while current has its bound cache entry evicted before notification, without evicting a replacement entry. Never publish a rejected value or synthesize zero, and ensure the ordinary local route obtains the proved local answer rather than leaving a successfully reducible selection unavailable.
- A normal forward `prepare(B)` neither retires nor relabels a live A detail. Its result and any successor use their own exact-Window ownership evidence, and their real lifetime, source/registration authority and original measurement identity. Do not use B's overlap flags to certify A or A's flags to certify B. Retain a valid A result and its first-seen stamp across a forward B as required by §§3.2 and 12.8. If ownership/registration authority changes, take the existing authority-retirement path before new-scope rendering and drain old work without publication. If newly observed ownership evidence calls the earlier exact Window into question, resolve that Window with its own valid evidence and the existing current/superseded rules before allowing an unsafe candidate to be effective. No second acceptance writer or public record/signature is introduced.

- Add this concrete owner-thread trace to the fixture helper or the focused mechanism test without turning it into a production API:

```python
def settle_production_owner(self, *, gui=None):
    gui = gui or self.gui
    window, history, client = gui.window, gui.history, gui.client
    observed = []
    real_query = client.query

    def remember(future):
        if not any(bound is future for bound, initial in observed):
            observed.append((future, client.query_transport(future)))

    def capture_router_query(request):
        future = real_query(request)
        remember(future)
        return future

    # MAX_INFLIGHT is one; retain any occupant admitted before this helper.
    if history._occupant is not None:
        remember(history._occupant[1])
    client.query = capture_router_query
    try:
        window._timer.stop()
        window._poll_timer.stop()
        after = window.facts_applied
        window.refresh()              # one production admission, never an idle-loop refresh
        deadline = time.monotonic() + 5.0
        last_progress = None

        def owner_step():
            nonlocal last_progress
            QApplication.processEvents()
            floor = window._retry_after
            clock_ns = window._clock_ns
            if floor is not None and clock_ns() < floor:
                # Advance the virtual BOOTTIME recovery clock, never the age clock.
                window._clock_ns = lambda clock_ns=clock_ns, floor=floor: max(clock_ns(), floor)
            # Poll drains/notifies only; it cannot admit the deferred target.
            window.apply_ready_frame(context="poll")
            window._poll_timer.stop()  # the fixture explicitly drives this stopped timer's path
            # The reader may still have owned the gate on the first floor turn.
            # Recheck the ordinary gate after a later pump once that reader releases.
            floor = window._retry_after
            if (floor is not None and window._clock_ns() >= floor
                    and window._pending is None and window._prepare_retry is None
                    and not history.local_read_in_flight):
                window._tick_target()
            trace = []
            for index, (future, initial) in enumerate(observed):
                state = client.query_transport(future)
                assert (state.request, state.connection_generation, state.request_id) == (
                    initial.request, initial.connection_generation, initial.request_id)
                if future.done() and not future.cancelled():
                    error = future.exception()
                    assert getattr(error, "reason", None) != "query_busy", (state, error)
                trace.append((index, state.request_id, state.phase))
            progress = (window.facts_applied, window._pending, window._prepare_retry,
                        history.pending_work, window._displayed, window._target,
                        window._probe_inflight, window._probe_successor, tuple(trace))
            previous = last_progress
            last_progress = progress
            if time.monotonic() >= deadline:
                raise AssertionError({"previous": previous, "current": progress,
                                      "last_request_phase": trace})
            if (window._pending is not None or history.pending_work
                    or window._probe_inflight is not None or window._probe_successor is not None):
                return False
            displayed = window._displayed
            ready = (window._target is None or (
                displayed is not None and displayed == window._target
                and history.ready(displayed)))
            return (window.facts_applied > after and ready
                    and window._prepare_retry is None and previous == progress
                    and all(phase == "terminal" for _, _, phase in trace))

        self.until(owner_step, seconds=5.0, process_events=True)
        window._timer.stop()
        window._poll_timer.stop()
        states = tuple(client.query_transport(future) for future, initial in observed)
        assert all(state.phase == "terminal" for state in states), states
        return states
    finally:
        client.query = real_query
```

The test-owned wrapper binds each observed Future to its actual initial `QueryTransportState`, seeds any existing wire occupant, and is removed before a direct test RPC. `P3Harness.until()` supplies the real `self.pump(2)` transport turns, Qt delivery, and existing worker yield between owner steps; `time` and `QApplication` are the existing harness imports. Preserve the existing virtual retry-floor driver without advancing the age clock. Do not consume `history.drain()`'s change bit before the shell sees it: unconditional `apply_ready_frame(context="poll")` owns that drain and its refresh notification, including pending-frame-free lazy detail delivery. The floor condition is re-evaluated after each real pump: if the local reader was still held when the clock first reached the floor, wait for its actual completion, then call the ordinary target gate once when eligible. Assert the no-pending poll makes zero `prepare()` calls before and after releasing the reader, the later ordinary gate admits exactly once, and no extra probe is submitted. Preserve explicit retry ownership and leave recovery production code unchanged. Two identical quiescent progress observations must have an intervening real `until()` pump/yield. The focused sent-request arm must assert a nonempty observation and an actual request id, so an empty cache/no-request path cannot be its evidence; legitimate no-target refusal cases retain their existing assertions. For disconnect/refusal arms, bind an explicitly expected `lost`/`not_sent` outcome to its real request and original assertion rather than demanding an impossible terminal reply or accepting `active`/`expired`. `Future.done()` above only diagnoses a completed error; it never grants RPC permission. A `query_busy` result is an assertion failure, not a retry.

- Preserve every existing test name and substantive assertion in all four files, including the byte/ownership/A22/cursor/restart/baseline/paging assertions. In particular, retain `test_live_incomplete_reply_cannot_resurrect_export_overlap_in_production_entry`, all six `test_p3_acceptance` tests, all adversarial test names and parameter cases, the exact daemon payload fields, the single-owner-thread assertion, the `[unit:acceptance-0100..0600] + [APP]` continuation order, and the mutation-path confinement check.
- Add these focused regression names exactly once: `test_p3_fixture_settlement_drives_production_owner_to_terminal_before_rpc` and `test_p3_fixture_retry_floor_waits_for_reader_then_admits_once` in `tests/p3/test_history_overlap_entry.py`, and `test_p3_fixture_uses_one_age_clock_without_duplicate_constructor_keywords` in `tests/p3/test_p3_acceptance.py`. They supplement the existing acceptance tests.
- Add exactly one top-level definition for each of these focused router tests in `tests/p3/test_live_overview_router.py`, with distinct parameter IDs for the specified arms: `test_overlap_detail_routes_locally_for_totals_and_series`, `test_remote_only_detail_routes_and_settles_unchanged`, `test_detail_settlement_rejects_ineligible_remote_candidate`, `test_forward_detail_uses_original_window_ownership`, and `test_ownership_change_retires_inflight_detail`.

- [ ] **Step 1: Reproduce and classify the existing fixture failures without changing source.** Run `python -m pytest -q tests/p3/test_history_overlap_entry.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py`. Record the duplicate `clock_ns` constructor failure separately from empty-ranking/`HistoryPending` failures. Capture the full collected node-ID inventory for the focused P3 and router modules listed in Step 8 before adding tests, including parameter IDs, for comparison after the repair. The independent baseline at `6624162ea8594021de74c0bae5ec26fedee36e30` and current replay at `a983927251213dc3518a95d8365450a74ffa2e9f` are preservation and diagnosis evidence, not substitutes for the fresh RED/GREEN run in Step 5. If a named observation is already green in the current checkout, record it as a preservation control and use the mutations in Step 7; never fabricate a RED run.
- [ ] **Step 2: Repair the harness clock composition.** Adapt the four named P3 consumers to current symbols and line locations, not stale report lines. In the live `run_interactive` factory test, keep the `clock_ns` callable supplied through `kwargs` and remove the second `clock_ns=` argument from `client_factory`; assert the exact callable identity survives into `IodClient`. Catch constructor `TypeError` at that test-owned boundary and assert its absence before returning the client. Share that test-only construction boundary with the focused clock regression so restoring the duplicate keyword fails its owned constructor-success assertion, not an uncaught setup error. Inside the existing factory, using its existing `raw_client`, `socket_path`, `kwargs`, `verify`, and `Peer` bindings:

```python
supplied_clock = kwargs["clock_ns"]
constructor_error = None
client = None
try:
    client = raw_client(socket_path, **kwargs, peer_reader=lambda sock: Peer(88, 0, 987),
                        path_verifier=verify)
except TypeError as error:
    constructor_error = error
assert constructor_error is None, ("client constructor rejected supplied clock", constructor_error)
assert client is not None and client._clock_ns is supplied_clock
```

In `P3Harness.open_gui()`, provide one injected MONOTONIC age clock to `IodClient`, `HistoryRouter`, and `CollectorWindow(age_clock_ns=...)`, with the BOOTTIME fixture clock passed only as `CollectorWindow(clock_ns=...)`. Preserve the separate `HistoryRpc` `clock_ns=lambda: 0` patch used for deterministic daemon query-budget tests; it is not the duplicate constructor keyword. Keep the real daemon-side `HistoryRpc` construction, query execution, socket peer identity, virtual filesystem, SQLite writes, restart/disable commands-as-data, and all existing schema/payload checks. Do not replace a clock with wall time or make the age clock serve BOOTTIME recovery predicates.
- [ ] **Step 3: Make settlement an owner-thread production trace.** Before `P3Harness.rpc()` or a test-owned `client.query()` is used, drive the live shell through its actual `refresh()`/poll path, pump the real client, drain the real router through the shell owner path, and apply a stored frame only through `apply_ready_frame()`. Await lazy detail/series/totals work through `HistoryRouter.drain()` and `ready(window)`; direct getters remain lazy and are not used as a wait primitive. The terminal precondition must show a new applied fact, `_pending is None`, `_prepare_retry is None`, `history.pending_work is False`, no outstanding/successor probe, the displayed target is ready (or the original no-target refusal), and every observed historical request has the expected identity and terminal `QueryTransportState`. Preserve explicitly expected disconnect/refusal outcomes as described above. Do not call `refresh()` on every loop turn when a pending frame is waiting for readiness, and do not re-submit a probe that would retire the preparation being settled.
- [ ] **Step 4: Prevent unsolicited overlap while preserving the real transport.** Keep the existing stopped test timers and explicit Qt driving only where the fixture already owns them; do not disable the production router drain or replace the shell poll path with a global event/scheduler bypass. A bounded helper may use a monotonic safety deadline, but it must assert a progress token and report the newly submitted request and its `QueryTransportState` when progress stalls. A `query_busy` response is a test failure with the observed state, not a reason to retry until the overlap disappears. Direct test-owned RPC is performed once after the terminal precondition, then its original wire/client result and payload assertions run unchanged.
- [ ] **Step 5: Demonstrate the production ownership regression after the fixture repair.** With Steps 2–4 in place and `history_router.py` still at the integrated baseline, run `python -m pytest -q tests/p3/test_history_overlap_entry.py::test_live_incomplete_reply_cannot_resurrect_export_overlap_in_production_entry`. Observe the real synthetic daemon, AF_UNIX client, router and production view after owner settlement. Record the call-phase owned assertion and actual ranking, retained totals/series, coverage, labels and transport evidence. The expected preserved local ranking and totals are `(0,0,0,1)`, series bytes are zero with one overlap-uncertain interval, and the GUI shows `0 B` with incomplete coverage. The independent replay observed remote totals `(100,60,40,0)` displayed as `100 B`; if the exact unmodified live test instead already passes after the fixture fix, record that fact as a green preservation control and reproduce the diagnosed mismatch with a controlled production-entry test before touching the router. Never count a duplicate-clock error, missing row, `HistoryPending`, setup/collection error, or a timeout as this RED.
- [ ] **Step 6: Implement and discriminate exact-Window detail ownership.** Add the five named router tests above before the router change. Use committed local/export stores and the existing reducer, selected subject, real `prepare`/`ready`/`select_detail`/`request_detail`/`drain` path, bound transport observations and view notifications. Cover:
  1. Both totals and series for overlapping local/export sources without a handover, in both local-frame/remote-ranking completion orders. Assert the actual local-selected path, no remote-detail query, zero excluded bytes, overlap-uncertain coverage, original Window, local retained provenance, and no remote-detail publication. The unchanged live production-entry test is the final GUI and real-socket oracle.
  2. A valid remote-only frame with nonempty ranking and valid totals and series, including nonzero data and unchanged Window, source and first-seen identity. Prove both are accepted through the bound remote transport and still appear in the view.
  3. A controlled misrouted remote candidate for a locally owned exact Window, delivered through an associated Future at the real settlement boundary. Assert no candidate value enters the retained record or notification, its bound cache entry is rejected without deleting a newer replacement, and the current `invalid_result` failure clears that dependency before notification with no notification-driven retry. Then use a later ordinary refresh or explicit action to start the existing local worker and publish its correctly reduced result. Exercise both detail kinds. Use a real client/cache control for eviction and a test-bound routing seam to force the race, not a fabricated accepted record. Assert malformed inputs retain `invalid_result` and a real local failure clears only its own dependency before notification.
  4. Same-scope A then forward B with explicit ownership evidence for each exact Window. Hold an A local detail inside its worker while B completes/applies, then deliver A and prove its original A provenance and excluded-byte result survive while B's successor remains independent. In a separate arm, accept A before applying B and hold B's successor pending while asserting that the retained A identity and first-seen stamp remain unchanged across that forward application. Exercise reacceptance of an identical identity while its endpoint remains eligible, without navigating backward. Separately hold a valid remote-only A across B preparation and settle A before B can use the single wire slot; allow B to apply only after its own ranking finishes and falls back locally. Choose A's valid bytes outside B's overlap so the valid A record remains usable without importing B's excluded bytes. Assert B's evidence never substitutes for A's or vice versa, and preserve the normative slow-detail and forward-scope controls.
  5. Change source registration/ownership while an old detail is outstanding. Assert retirement before rendering, genuine transport/worker draining, no late old success or failure publication, and no clearing of a newer accepted record. The client retains its existing authority-change cache rules, and ignored superseded deliveries alone need not evict otherwise valid entries. Also exercise a same-authority superseded failure and the independent other dependency.

  Implement the private association and settlement ordering specified in Interfaces, using the existing ownership reducer and single writer. Release every held worker/Future barrier in `finally`; never make an impossible demand that two historical requests occupy the wire together. Run the actual live test and each new discriminator GREEN after the fix. Compare the live test's substantive assertions and local accounting against the unchanged pre-feature behavior; include the exact current-versus-superseded and cache observations in the task evidence.
- [ ] **Step 7: Add falsifiable controls in disposable copies.** In one disposable copy, restore the duplicate `clock_ns=` argument inside the Step 2 `try` while `kwargs` already contains the supplied callable; prove the focused clock regression fails at the owned `assert constructor_error is None`, after catching the expected `TypeError`. Its green/restored arms also assert exact clock identity. For settlement, use controlled Events/Futures at the existing real worker/completion-delivery boundary and observe an actual sent request before checking the RPC gate. Assert that completed-but-undrained work still forbids RPC, that `active` and `expired` request identities still forbid RPC, and that no new request/probe appears on an owner-only turn. In separate disposable copies remove the owner drain, substitute `Future.done()` for readiness, submit a fresh probe on every loop turn, or admit RPC during `active`/`expired`; each must fail the corresponding owned publication/phase/submission assertion under that controlled schedule, not merely exhaust the deadline. Factor the helper's owner step/predicate into test-only observations if necessary to inspect these states before releasing each barrier in `finally`. Restore each mutation and rerun its green control. No uncaught constructor/setup/collection/import error, timeout-only result, inflated timeout, fake certificate/response, or unobserved transport counts as a kill. Return at least two important negatives with their baseline/mutated/restored assertion evidence to the independent task review.

  Independently mutate the production detail selection to choose remote for a mixed applied frame and mutate the settlement ownership check so an already-bound ineligible candidate publishes. The selection discriminator must fail its owned no-remote-detail-query/route assertion even if the settlement guard still protects the final value. The settlement discriminator must fail its cache/record/notification assertion through the forced real settlement boundary. Also test a mutation that binds A to B's latest frame evidence, using the forward-Window discriminator. Capture the exact pytest node/parameter, call-phase assertion location and outcome in structured results, with a real passing control. A collection/import/setup failure or timeout-only outcome is not a kill, and do not weaken another guard merely to claim one.
- [ ] **Step 8: Run the focused P3 and router gates and verify the preserved inventory.** Run the following full modules with no `-k`, then collect the same modules with `--collect-only -q`:

```bash
python -m pytest -q tests/p3/test_history_overlap_entry.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_history_router.py tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py
python -m pytest --collect-only -q tests/p3/test_history_overlap_entry.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_history_router.py tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py
```

  Compare node IDs and parameter cases with the Step 1 inventory, confirm all eight new names occur once as top-level definitions and every original name/case remains, and inspect unchanged daemon payload/schema/socket assertions. Record real owner/transport/clock traces, both kinds' local and remote controls, all mutation results and the live GUI oracle. Keep the A22, file-only, DEVICE, Files ownership and recovery controls intact. The full `python -m pytest`, exact overall collected inventory, and the §12.9 mypy gate including check-only `theme.py`/`files_view.py` remain fresh later acceptance gates, run after this task and Task 18; do not claim earlier results as fresh evidence.
- [ ] **Step 9: Commit only the authorized router and P3 regression repair.**

```bash
git add src/appio/history_router.py tests/p3/acceptance_harness.py tests/p3/test_history_overlap_entry.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_live_overview_router.py
git commit -m "fix(p3): preserve overlap ownership in settled detail"
```

Return the duplicate-clock diagnosis, floor/terminal transport-state traces, live RED/GREEN results, local/remote/race and preservation controls, exact collected-name inventory, disposable mutation results and exact revision to the deterministic controller. Keep files/protocol code, producers, the approved design/specification and the plan read-only during implementation.

## Task 18: Pin P4 owner-thread Files settlement and committed-store provenance

**Implementer tier:** Capable

**Lane:** `regression`
**Depends on:** Task 17
**Specification:** §§3.1, 3.3-3.4, 5.4, 6.4, 12.8-12.9 (D02/D03/D05/D06/D07); recovery §§4.4-4.5. This task repairs only P4 regression ownership and test settlement evidence before Task 14; it does not redefine Files coverage, served intervals, store schemas, or the wire protocol. Do not add production timers/getters or edit the FilesView, query, or store contracts; shorten/remove required real-store assertions; or fabricate a capture.

- Never execute `reboot`, `shutdown`, `poweroff`, `halt`, `kexec`, any `systemctl` power or sleep verb, `init 0|6`, or `telinit 0|6`; never restart or stop the display manager or `graphical.target`; never terminate your session or signal the process tree that hosts the PM. If you conclude a host transition is required, write `reboot-required.json` with `{schema: 1, trigger, evidence: [{path, sha256}], provenance: {run_id, lane, attempt, dispatch_key, observed_boot_id}}`, then report it and stop.

**Files:**

- Modify/Test: `tests/p4/test_file_history_isolation.py:1-194`, `tests/p4/test_file_served_interval.py:1-260`, `tests/p4/test_files_tab_real_store.py:1-193`
- Read-only: `tests/p4/test_file_history_router.py:1-807` for its existing owner-drain pattern; `src/appio/history_router.py`, `src/appio/ui/files_view.py`, `tests/p4/acceptance/files_tab_pivot_case.sqlite.gz`, its manifest/capture/oracle artifacts, `src/appio/files.py`, the file store/BPF producers, `tests/p4/test_files_tab_local_result.py`, `tests/p4/test_files_tab_no_matches.py`, `tests/p4/test_files_tab_unavailable.py`, and the approved design/specification
- Do not change file protocol fields, SQLite schema/migrations, BPF/file producers, capture bytes, oracle expectations, or any P3 source/fixture file. If correctly driven consumers contradict a production contract, stop and report the exact failing boundary evidence for a separate reviewed plan change; this task authorizes no production edits.

**Interfaces:**

- Consumes `HistoryRouter.request_file_view(...)`, `HistoryRouter.drain()`, `HistoryRouter.ready(window)`, `HistoryRouter.pending_work`, `_consume_local()`, and the real local/remote transport state. `HistoryRouter.drain()` is the owner-thread settlement point: worker callbacks only enqueue immutable outcomes; the owner drain observes completion, performs closing reads, publishes the result, reaps work, and notifies.
- Consumes `FilesView.request_refresh()`, `FilesView.flush_refresh()`, `_deliver(...)`, `set_window(...)`, `FileViewResult(table, coverage, served_gap_count)`, and the existing failure boundary. Files has no independent poll loop. Direct detail getters, including `_exact_part`, remain lazy and must be awaited through the production drain/readiness gate rather than made synchronous.
- Produces exactly these focused regression names once: `test_files_settlement_requires_owner_thread_drain_before_publication` in `tests/p4/test_file_served_interval.py` and `test_committed_files_capture_preserves_served_interval_and_watermark_isolation` in `tests/p4/test_files_tab_real_store.py`. Preserve every existing test name and substantive assertion in the three modified files and the read-only router control, including `test_R01_boot_lookup_failure_preserves_process_T_D_history`, both `bad_first` cases, all R02/prepare-failure cases, `test_real_router_disclosure_names_the_served_interval`, `test_gap_covered_mid_hour_unproved_segment_is_served`, and `test_real_capture_serves_the_same_interval_when_available`.

```python
def await_files_result(router, future, qtbot, *, request):
    deadline = time.monotonic() + 5.0
    progress = []

    def owner_step():
        changed = router.drain()      # publication and reaping on the test owner thread
        progress.append((changed, router.pending_work, future.done()))
        assert time.monotonic() < deadline, {"request": request, "progress": progress[-2:]}
        return future.done() and not router.pending_work

    qtbot.waitUntil(owner_step, timeout=5_000)  # yields Qt/worker delivery between turns
    return future.result(timeout=0)

future = router.request_file_view(router.registry.id_for(APP), REQUESTED,
                                  "bytes_total", "desc", None)
result = await_files_result(router, future, qtbot,
                            request=(APP, REQUESTED, "bytes_total", "desc", None))
assert router.pending_work is False
assert result.coverage.window == REQUESTED
assert result.served_gap_count is None or isinstance(result.served_gap_count, int)
```

Use the existing `time`/`qtbot` bindings (add the test-only import/fixture parameter where needed); no tight fixed-iteration or busy-wait loop. For a `FilesView` fixture, call `view.set_window(window)`/`view.request_refresh()` through the real scope path, provide the owner pump that calls `router.drain()`, and let Qt deliver `_deliver(...)` before inspecting `view.last_result`; include the view's settled state in that predicate. Do not use private worker `Future.done()` as publication. The helper must report the last request, owner-thread drain result, and pending state when the safety deadline expires; these bounded diagnostics are not mutation-kill evidence.

- [ ] **Step 1: Reproduce the P4 failures at the owning boundary.** Run `env -u APPIO_ACCEPTANCE_REQUIRED -u APPIO_ACCEPTANCE_CAPTURE python -m pytest -q tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py tests/p4/test_file_served_interval.py tests/p4/test_files_tab_real_store.py`. Separate lazy-publication/owner-drain failures from watermark isolation and committed-capture/oracle failures. The committed gzip fixture already fails for absence/hash drift without required mode. Preserve the distinct optional external-capture test and its existing skip/required-failure semantics; never set the required flag on the full module or require a new `/tmp` capture.
- [ ] **Step 2: Pin local Files settlement to the real owner drain.** Use the actual `request_file_view()` or `FilesView` refresh/flush path, then drive the owner thread with `router.drain()` until the registered outward Future is published. A private worker Future becoming done is only worker completion; the outward Future is completed by owner publication. Assert the owner-thread drain observes the immutable outcome, performs the final scope/position checks, and leaves no pending work before reading `FileViewResult`. A bounded yielding test safety deadline is allowed; no Qt-thread production wait/sleep, direct synchronous getter, fake result, or scheduler bypass is allowed.
- [ ] **Step 3: Pin FilesView delivery and failure semantics.** Exercise `request_refresh()`/`flush_refresh()` and `_deliver()` with the production scope/generation path. Prove that a pending local Files page is delivered only after owner-thread drain, stale generation results cannot publish, an unavailable source is not an empty successful table, and a current file-source failure clears availability without suppressing independent process history. Keep the exact `served_gap_count` None-versus-integer contract and `FileViewResult` field inventory.
- [ ] **Step 4: Preserve watermark isolation and served-interval disclosure.** Keep the authorizer-based denial of `boot.file_finalized_through_mono_ns` and assert process ranking/T/D history, task/device shares, series totals, and the exact `file_source_unavailable` reason remain visible. Keep the `bad_first` ordering cases. For the served-interval fixtures, retain `SERVED = [T0=2h,T1=4h]` versus `REQUESTED = [T0=0,T1=6h]`, the eight footer labels containing `served interval only` and `T0=2h 0m 0s through T1=4h 0m 0s after boot`, the `rows retained;` disclosure, gap evidence 0, coverage scalar 2, `view.state == "incomplete"`, and the requested-window result/coverage bindings.
- [ ] **Step 5: Verify the committed acceptance fixture independently of the app implementation.** Preserve the committed `tests/p4/acceptance/files_tab_pivot_case.sqlite.gz` bytes and manifest digest. Re-run its standard-library-only oracle without importing `appio`; open the snapshot through a read-only SQLite URI, use the real router local preparation and `_consume_local() is True`, and drive the Files view through the owner drain until its loading state settles. Assert the oracle's served interval, unknown-owner overflow window, retained rows, gap evidence, coverage disclosures, and application set exactly. Keep committed absence/digest/oracle failures loud without required mode, and do not add/remove/relabel any existing skip. Separately, inside the new committed-fixture regression run an isolated subprocess selected ONLY at `tests/p4/test_files_tab_real_store.py::test_real_capture_serves_the_same_interval_when_available`: give that child `APPIO_ACCEPTANCE_REQUIRED=1` and `APPIO_ACCEPTANCE_CAPTURE` pointing at a deliberately absent directory under its test `tmp_path` (so its `iod.sqlite` is absent). Use `subprocess.run([sys.executable, "-m", "pytest", "-q", nodeid], env=child_env, capture_output=True, text=True, check=False, timeout=10)` with that exact single-test node id; set `child_env = dict(os.environ)` explicitly before import (the module reads required mode at import), set the two child-only values, and do not mutate the parent's environment or recursively select the new regression. Assert return code 1, exactly that test's `FAILED` result, and the existing `frozen capture missing at` diagnostic, with no skip, collection/import error, or timeout credited. The counterpart child with the flag removed from its environment and the SAME absent directory must report its existing skip and exit 0. This preserves the external-capture policy without creating a capture, touching a service, or imposing it on committed-fixture tests.
- [ ] **Step 6: Falsify the ownership and provenance mechanisms in disposable copies.** Remove the owner-thread `router.drain()` publication step and prove the new settlement test fails its owned publication assertion after a controlled worker-completion barrier, not just a timeout; substitute the requested interval for the served interval and prove the disclosure test fails; make a file watermark lookup failure suppress process history and prove both R01 ordering cases fail; turn an unavailable source into an empty success and prove the explicit-unavailable tests fail. Separately mutate required external-capture absence to skip and prove Step 5's parent assertion on the selected child's failure/diagnostic rejects it; committed-gzip absence/hash controls remain flag-independent. Restore each mutation and rerun its green control. Return at least two important negatives with baseline/mutated/restored owned-assertion evidence to the independent task review. Do not count setup/collection/import errors, timeout-only results, fake tables, direct worker Future waits, or altered oracle/capture data as kills.
- [ ] **Step 7: Run the focused P4 gate and exact name inventory.** Run `env -u APPIO_ACCEPTANCE_REQUIRED -u APPIO_ACCEPTANCE_CAPTURE python -m pytest -q tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py tests/p4/test_file_served_interval.py tests/p4/test_files_tab_real_store.py`, then `env -u APPIO_ACCEPTANCE_REQUIRED -u APPIO_ACCEPTANCE_CAPTURE python -m pytest --collect-only -q` for the same files. The required-mode flag belongs only to Step 5's isolated single-node negative control, never this full-module command. Confirm both new names occur exactly once, all original names and parameter cases remain collected, `HistoryRouter.pending_work` is false only after owner settlement, and no Files-specific poll or synchronous detail getter was introduced.
- [ ] **Step 8: Commit only the three P4 regression consumer files.**

```bash
git add tests/p4/test_file_history_isolation.py tests/p4/test_file_served_interval.py tests/p4/test_files_tab_real_store.py
git commit -m "test(files): pin owner drain and served interval provenance"
```

Return the owner-drain trace, isolation/provenance assertions, committed-fixture digest/oracle evidence, focused results, preserved-name inventory, disposable mutation results, and the exact revision to the deterministic controller. Do not change or claim a fix in production, the design/specification, protocol/schema/producers, P3 files, or the plan.
