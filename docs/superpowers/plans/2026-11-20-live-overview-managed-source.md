# Live Overview managed-source implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Leave the checkboxes unchanged. Controller state tracks execution.

**Goal:** Replace unprovable pathname-reopened local history with managed source actors that own their observing connections, persist transport gaps through the registry-owned GUI writer, hand immutable snapshots to local workers, and carry exact source and snapshot provenance through router settlement and the live Overview callers.

**Architecture:** `ManagedSourceRegistry` owns one source actor and one source connection incarnation per registered source. The primary GUI registration also owns its paired gap writer and observer. The actor observes positions and performs serialized backups on the same connections, while workers receive only immutable SQLite image bytes and `SnapshotProvenance`. `HistoryRouter` consumes the registry through its revised constructor, fences transport gaps before preparation, settles all local frame, detail, and Files results through one provenance-validating boundary, and refuses unmanaged compatibility inputs. The interactive shell, export import path, test harnesses, and every router construction use the managed route before any frame work starts.

**Tech Stack:** Python >=3.14, SQLite, standard-library `dataclasses`, `Future`, threads, executors and queues, the existing AppIO store and query reducers, PyQt6, pytest and pytest-qt, and the documented development-only mypy command.

**Authority:** `docs/superpowers/specs/2026-11-15-live-overview-totals-spec.md`, revision 8, SHA-256 `03f1d2adbc3f8a67b94429a4d61ef61008c8761c37ce78624ba4f12c6874b0c3`. The approved design remains unchanged at SHA-256 `8d6941b545ba15c9338f94df63d9d5c6b0b861e7cc7f14f8e85e2a675a6d70d9`. The completed earlier live-overview plan, including its repair tasks, is an input and is not re-planned here.

## Global Constraints

- Implement exactly the managed-source contract in specification revision 8. Preserve the completed retained-value, recovery, chart, status, transport, Files, store, producer, sampling, wire-format and schema contracts unless revision 8 explicitly changes their source ownership or provenance.
- A managed source is the authority for its own connection incarnation. A source actor opens, uses, observes, backs up and closes its connection on its own thread. No `sqlite3.Connection`, `QueryService`, source actor, source pathname, temporary destination pathname, mutable registry or materialised database crosses into a worker result.
- Do not use pathname text, `stat()`, device/inode values, equal backup bytes, table hashes, row maxima, counters from another connection, or a second observer as a physical-source certificate. The Python `sqlite3` API cannot prove that an arbitrary supplied connection remains bound to a pathname.
- The primary GUI registration must use the approved writer/observer shared-cache admission challenge, query-only and authorizer enforcement, committed observer visibility checks, and the finite retry/error semantics in §4.5. Fail closed when the runtime cannot provide the required pairing evidence. Do not alter journal mode or the store schema.
- `ManagedSourceSpec` is an admission description. It is immutable scalar data and never becomes a connection handle. Registration epochs are positive, strictly increasing and never reused during the router process lifetime. Replacement retires the prior registration before admitting the new one.
- The registry actor never blocks `IodClient.pump`, a Qt slot, or the router owner on a source Future or SQLite lock. Router and worker shutdown retain `wait=False, cancel_futures=True`, and late completions remain terminal and are reaped without publication.
- The router constructor is `HistoryRouter(sources: ManagedSourceRegistry, client: IodClient, registry: AppRegistry, *, clock_ns: Callable[[], int] = time.monotonic_ns)`. The compatibility entry point may accept only `ManagedSourceSpec` and forward to `add_managed_source`. A `QueryService` or raw `sqlite3.Connection` raises synchronously with `SourceAdmissionError("history source is not managed")`.
- Every production and test caller is migrated in the same integration task that narrows the constructor. No partially migrated constructor baseline may be committed. The bootstrap `AppIoStore` used by interactive startup is temporary setup only and is never passed to the router, registry or gap writer.
- Local source positions use every active managed registration in UTF-8 name order and exactly `(name, owner, registration_epoch, data_version, total_changes)` read from that registration's own connection outside a transaction. Local captures use the same connections for the opening vector, backups and closing vector.
- Workers use private temporary SQLite files created from immutable capture bytes, verify every image digest, run existing reducers, close and remove their private files in `finally`, and return immutable outcomes with matching `SnapshotProvenance`. Unequal brackets produce valid worker input with `answered_source is None`, an uncertified frame and exactly one coalesced replacement.
- `HistoryRouter._settle` is the only outward publication boundary for frame, detail and Files results. It revalidates exact request, selector, worker binding, capture ID/digest, registration incarnations, nested result contracts and source authority before publishing. Local inputs issue zero `IodClient.query` or `query_transport` calls.
- Gap notices are appended immediately on the router owner thread. The registry-owned writer persists only bounded qualifying notices, returns a Future, observes the result on the registered observer, and exposes errors through `gap_persistence_error`. Preparation fences all qualifying gap submissions accepted before its owner-thread call before remote ranking or local capture.
- Keep the existing BOOTTIME clock for Window and recovery deadlines. Use the injected monotonic clock for transport deadlines, accepted-identity stamps and age. The interactive composition passes one age clock to client, router and view/adapter.
- Test mutation arms run only in disposable copies. A mutation kill requires a named assertion failure with a passing baseline, a controlled mechanism-specific mutation, and a restored green control. Import, collection, fixture setup and generic exceptions are not kills. Do not fabricate a red run for an already-green preservation control.
- Run implementation commands from the repository root with `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 PYTHONPATH="$PWD/src:$PWD/tests"`. Do not install dependencies or execute host power, session, display-manager or graphical-target actions. If a host transition is somehow required, write the specified `reboot-required.json` at the worktree root, report it and stop.
- Each task owns only the files listed in its Files block and commits its completed work. Existing frozen test names have one top-level definition. Later tasks extend an existing named test instead of duplicating it.

## Task 1: Extend immutable managed-source and snapshot provenance records

**Implementer tier:** Advanced

**Lane:** `foundation`
**Depends on:** none
**Specification:** §§2.2–2.4, 3.4, 12.2. This task extends the already integrated neutral records and does not re-plan their earlier retained-answer work.

**Files:**

- Modify: `src/appio/history_types.py`
- Modify/Test: `tests/p3/test_live_overview_router.py`

**Interfaces:**

- Preserve all existing frozen `Window`, `AcceptedIdentity`, `RemotePosition`, `LocalPosition`, `LocalPositionVector`, `LocalBracket`, `AnswerProvenance`, `RetainedRecord`, `PartState`, `FrameProvenance` and `SeriesMetadata` fields and validation already established by the earlier plan, except for the revision-8 additions below.
- Add the exact frozen records `ManagedSourceSpec(name: str, owner: Literal["gui", "iod"], database_uri: str, connect_options: tuple[tuple[str, str], ...])`, `CapturedSourceImage(name: str, registration_epoch: int, image: bytes, image_digest: str)`, `CapturedSourceSet(capture_id: int, opening: LocalPositionVector, closing: LocalPositionVector, images: tuple[CapturedSourceImage, ...], snapshot_digest: str)`, and `SnapshotProvenance(capture_id: int, snapshot_digest: str, bracket: LocalBracket)`.
- Add `snapshot: SnapshotProvenance | None` to `AnswerProvenance` and require local accepted identities to carry matching snapshot evidence. Keep remote provenance free of a local bracket and snapshot.
- Keep `FrameProvenance(generation, window, requested_source, answered_source, local_bracket, snapshot, certified)` exact. Local requested or answered sources require a local bracket. Remote answered sources cannot carry one. Equal brackets require `answered_source == bracket.opening`. Unequal brackets require `answered_source is None`. `certified=True` requires an answered source, successful ranking evidence and all source-incarnation checks supplied by the producer.
- Validate immutable scalar text and tuple fields, UTF-8 bounds, positive capture and registration IDs, SHA-256 lowercase digests, image name uniqueness and byte ordering, equal image/source membership, complete source vectors, and nested frozen values. `connect_options` accepts only immutable scalar text pairs and never a mutable factory object.

- [ ] **Step 1: Extend the existing named record test without changing its ownership.** Add field and truth-table arms to `test_live_records_validate_every_field_and_phase_combination`. Cover bad owner, URI, option shape, capture IDs, image bytes and digests, duplicate or unsorted images, missing vector members, snapshot/bracket mismatches, remote/local phase combinations, bool-as-int values and direct frozen-assignment rejection. Keep the existing baseline controls for retained records and `SeriesMetadata`.
- [ ] **Step 2: Run the focused test before implementation.**

```bash
python -m pytest -q tests/p3/test_live_overview_router.py::test_live_records_validate_every_field_and_phase_combination
```

Record whether the new arms are already covered by the existing implementation. A green preservation arm is evidence of preservation and must not be reported as an initial failure.
- [ ] **Step 3: Implement only the revision-8 record fields and validation.** Re-enter nested validation in each `__post_init__`. Do not import SQLite, Qt, the client, router or query service into `history_types.py`. Keep `SourcePosition = RemotePosition | LocalPositionVector` and the existing `PartState` absent/pending/ok/failed truth table exhaustive.
- [ ] **Step 4: Run the complete focused record test and a neutral-import check.**

```bash
python -m pytest -q tests/p3/test_live_overview_router.py::test_live_records_validate_every_field_and_phase_combination
python -c 'import appio.history_types as m; names=set(m.__dict__); assert "sqlite3" not in names and "PyQt6" not in names'
```

- [ ] **Step 5: Commit the record extension.**

```bash
git add src/appio/history_types.py tests/p3/test_live_overview_router.py
git commit -m "feat(history): add managed source and snapshot provenance records"
```

## Task 2: Build the managed source registry and actor-owned observations

**Implementer tier:** Capable

**Lane:** `source`
**Depends on:** Task 1
**Specification:** §§2.2, 4.1–4.2, 4.4, 12.4. The registry and source actors must be complete enough for later capture and writer tasks to consume without passing external connections.

**Files:**

- Create/Modify: `src/appio/history_source.py`
- Modify/Test: `tests/live_overview_support.py`
- Modify/Test: `tests/p3/test_live_overview_router.py`

**Interfaces:**

- Produce `SourceAdmissionError(RuntimeError)` and the registry surface `ManagedSourceRegistry(*, wake: Callable[[], None])`, `register(spec: ManagedSourceSpec) -> Future`, `replace(name: str, spec: ManagedSourceSpec) -> Future`, `retire(name: str, reason: str) -> None`, `observe() -> LocalPositionVector`, `capture() -> Future`, `close() -> None`.
- The internal registration receipt may contain only immutable name, owner, registration epoch and actor status needed by the owner thread. It must not expose a connection, URI handle, pathname, store or actor object.
- Each active registration has one source actor thread and one long-lived observing connection. The actor opens the connection, validates schema, enables query-only mode, installs the read-only authorizer, confirms it is outside a transaction, reads `PRAGMA main.data_version` and `connection.total_changes`, and only then publishes the registration.
- `observe()` serializes on the actors and returns every active `LocalPosition` sorted by `name.encode("utf-8")`. It never hashes tables, scans rows, opens a replacement observer, reads a counter from another connection or commits/rolls back an active caller transaction.
- Registration visibility is delayed until the first same-connection observation succeeds. Replacement retires the old name and epoch before opening the new spec. A failed replacement leaves the name retired and consumes a new epoch on a later attempt. `close()` rejects new work and closes every actor-owned connection on its owning thread.
- Source admission accepts only file-backed managed specs and immutable scalar connect options. It does not certify a pathname or reopen a caller-supplied connection. Primary GUI writer pairing is an explicit admission hook completed in Task 4, with fail-closed registration until that hook succeeds.

- [ ] **Step 1: Add actor/registry tests around the existing local-position fixtures.** Extend `LocalPositionStores` so each store has a persistent registered observer and an independent writer. Add or extend the exact tests `test_local_vector_observes_each_registered_connection_without_hashing`, `test_local_vector_changes_for_every_existing_commit_path`, `test_local_registration_add_remove_replace_and_reopen_never_repeat_token`, and `test_local_observations_require_committed_registered_connections`. Include DELETE-journal and WAL fixtures, deletion and in-place update paths, non-primary writes, rollback, a read-only write rejection, and active-transaction refusal. Use barriers and injected scheduling rather than sleeps.
- [ ] **Step 2: Run the source tests against the current baseline.**

```bash
python -m pytest -q tests/p3/test_live_overview_router.py -k 'local_vector_observes_each_registered_connection_without_hashing or local_vector_changes_for_every_existing_commit_path or local_registration_add_remove_replace_and_reopen_never_repeat_token or local_observations_require_committed_registered_connections'
```

- [ ] **Step 3: Implement the actor and registry.** Use a private request queue per source actor or an equivalent serialized actor loop. Open, query, backup and close on that actor thread. Verify `connection.in_transaction` is false before every observation. Cache only registered-name order. Ensure actor failures complete the owner Future and invoke `wake` as notification without touching router or Qt state.
- [ ] **Step 4: Implement replacement, retirement and close ordering.** Revoke old authority before any new registration becomes visible. Keep old actor work terminal for cleanup only. Never reactivate an old epoch or adopt a completed old capture. Close connections on the actor and make repeated close idempotent.
- [ ] **Step 5: Run the exact four local-position tests and thread-affinity assertions.** Confirm that no test uses a fresh copy observer in place of a persistent registration and that every observed token changes for the specified committed path or registration event.
- [ ] **Step 6: Commit the source actor seam.**

```bash
git add src/appio/history_source.py tests/live_overview_support.py tests/p3/test_live_overview_router.py
git commit -m "feat(history): own local source observations in managed actors"
```

## Task 3: Serialize captures and convert local workers to immutable snapshots

**Implementer tier:** Capable

**Lane:** `source`
**Depends on:** Task 2
**Specification:** §§2.2, 3.4, 4.3–4.4, 12.2, 12.4, 12.7–12.8. This task changes private local worker inputs before the router constructor cutover.

**Files:**

- Modify: `src/appio/history_source.py`
- Modify: `src/appio/history_router.py`
- Modify/Test: `tests/live_overview_support.py`
- Modify/Test: `tests/p3/test_live_overview_router.py`
- Modify/Test: `tests/p4/test_file_history_router.py`
- Modify/Test: `tests/p4/test_file_history_isolation.py`
- Modify/Test: `tests/p4/test_file_served_interval.py`

**Interfaces:**

- Complete `ManagedSourceRegistry.capture() -> Future[CapturedSourceSet]`. Capture is serialized with observe, registration, retirement, replacement and close on the source actor.
- The actor takes an opening vector from the same active connections, backs up every source in UTF-8 name order into a private temporary destination, reads and hashes the complete image, validates the private image schema, removes the destination, takes the closing vector from those same connections, and returns one immutable `CapturedSourceSet` with a fresh positive capture ID and canonical snapshot digest.
- A capture lease retains every active registration until every dependent frame, detail and Files terminal completion has been drained. Retirement invalidates publication authority but does not release a lease early.
- Convert `_read_local`, `_read_local_files` and their private worker helpers in `src/appio/history_router.py` to consume immutable image bytes and capture metadata. A worker writes its own private SQLite file, verifies the digest, opens it on its own thread, runs the existing planners/reducers and closes/removes the file in `finally`.
- `LocalFrameOutcome`, `LocalPartOutcome` and `LocalFilesOutcome` carry exact worker binding plus `SnapshotProvenance`. Local Files keeps served interval, coverage, tiers, unknown-owner evidence and `served_gap_count` unchanged. Local frame keeps the existing routing evidence, chronology and fatal-vs-skippable proofs without eager parts or database handles.

- [ ] **Step 1: Strengthen the real barrier fixtures.** Keep `SqliteProxy` and `_Barrier` as observer/backup instrumentation, but make the source actor's captured bytes the only worker input. Add tests for capture ID/digest matching, name and epoch ordering, image deletion, lease retention after retirement, digest mismatch refusal, and worker thread affinity. Keep `test_local_detail_is_lazy_selected_subject_only` as a preservation control.
- [ ] **Step 2: Add bracket schedule tests.** Extend the exact tests `test_commit_during_backup_is_uncertified_with_one_replacement`, `test_commit_after_backup_before_closing_read_is_uncertified`, and `test_commit_after_closing_read_is_caught_by_next_comparison`. The first two must apply their immutable image only as uncertified data and schedule exactly one replacement. The third must certify the equal bracket and detect the later commit on the next observation.
- [ ] **Step 3: Implement serialized capture and lease accounting.** Do not use `stat`, URI reopening, a second observer, equal bytes or counters from another connection. Busy/locked retries use a finite injected monotonic deadline and never create a second source connection. The actor fully consumes cursors and clears temporary destinations in all paths.
- [ ] **Step 4: Replace pathname-based local worker binding.** Remove the existing `...?mode=ro` source opens from `_read_local` and `_read_local_files`. Do not let a result carry source or temporary path. Preserve existing planner order, source ownership, process/file retention, aggregate consistency and Files fatal-vs-skippable behavior.
- [ ] **Step 5: Re-enter outcome validation in the worker and owner boundary.** Reject wrong capture ID, digest, registration vector, request, worker ID, exact app key, exact Window, local answer digest, table, coverage or served-gap value before publication. Keep the result immutable and ensure a retired capture is reaped without clearing another owner.
- [ ] **Step 6: Run focused source and Files tests.**

```bash
python -m pytest -q tests/p3/test_live_overview_router.py -k 'local_detail_is_lazy_selected_subject_only or commit_during_backup or commit_after_backup or commit_after_closing'
python -m pytest -q tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py tests/p4/test_file_served_interval.py
```

- [ ] **Step 7: Commit immutable snapshot workers.**

```bash
git add src/appio/history_source.py src/appio/history_router.py tests/live_overview_support.py tests/p3/test_live_overview_router.py tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py tests/p4/test_file_served_interval.py
git commit -m "feat(history): route local workers through immutable managed snapshots"
```

## Task 4: Add the registry-owned GUI gap writer and preparation fence

**Implementer tier:** Capable

**Lane:** `source`
**Depends on:** Task 2
**Specification:** §4.5, D12, §§3.1, 3.2, 6.1, 12.4, 12.9, 13.

**Files:**

- Modify: `src/appio/history_source.py`
- Modify: `src/appio/history_router.py`
- Modify/Test: `tests/live_overview_support.py`
- Modify/Test: `tests/p3/test_live_overview_router.py`
- Modify/Test: `tests/p3/test_collector_window.py`
- Modify/Test: `tests/p3/test_history_router.py`

**Interfaces:**

- Add the immutable `GapWriteResult(registration_epoch: int, disposition: Literal["inserted", "duplicate", "unknown_boot"], boot_id: str, from_mono_ns: int, to_mono_ns: int)` with the exact validation in §4.5.
- Complete `ManagedSourceRegistry.__init__(*, wake: Callable[[], None])` and `record_transport_gap(notice: GapNotice) -> Future[GapWriteResult]`. The writer is registry-owned and paired with the primary `name="local", owner="gui"` observer. It accepts no connection, store, callback or pathname from a caller.
- Add `HistoryRouter.record_transport_gap(notice: GapNotice) -> None`, `HistoryRouter.retry_transport_gaps() -> None`, and read-only `HistoryRouter.gap_persistence_error -> Exception | None`. The router appends each notice immediately, enqueues qualifying bounded notices without waiting, and retains exact failed notices for same-epoch retry.
- Admission opens the writer and observer from the same immutable shared-cache URI description, verifies `cache=shared` pairing with the random uncommitted-row challenge, validates schema and query-only/authorizer controls, and publishes the pair only after outside-transaction observation succeeds. The observer remains read-only. Imported `iod` sources are observer-only.
- The actor performs `BEGIN IMMEDIATE`, boot existence and exact duplicate checks, one `gui_absent` insertion with the active GUI epoch or a no-write unknown-boot outcome, commit/rollback, and same-observer visibility verification. It distinguishes `SQLITE_BUSY` and `SQLITE_LOCKED`, retries under the finite monotonic budget, preserves ambiguous post-commit state for deduplication, and never converts failure into success.
- A preparation fences all notices accepted before its owner-thread call. No remote ranking or local capture starts before the fence reaches terminal successful or unknown-boot outcomes and prior invalidation runs. A gap accepted later follows the normal replacement path.

- [ ] **Step 1: Add deterministic writer and fence tests before changing the router.** Include valid inserted, duplicate and unknown-boot notices, empty identity, missing bounds and negative-bound no-write arms. Add real DELETE-journal and WAL paired connections, active reader/writer transactions, commit visibility, query-only write refusal and authorizer refusal. Use an injected monotonic clock and barriers for busy/locked retries.
- [ ] **Step 2: Implement paired primary admission.** The challenge must use a fresh unpredictable boot identity and a temporary writer transaction, must be invisible through an ordinary private-cache connection, and must be rolled back with `read_uncommitted=OFF` restored before visibility. Reject any failed binding or restoration with `SourceAdmissionError("gap writer binding is unprovable")`. Do not repeat the challenge for ordinary writes.
- [ ] **Step 3: Implement the transaction and observer check.** Use a direct actor-owned SQL insert because `AppIoStore.record_gap` starts its own transaction. Check exact `(boot_id, source='gui', from_mono_ns, to_mono_ns, reason='gui_absent')` under `BEGIN IMMEDIATE`, roll back no-write arms, verify the result through the registered observer and return the exact disposition. Do not mutate schema or journal mode.
- [ ] **Step 4: Implement router callbacks, retries and closure.** The client/Qt callback only invokes `record_transport_gap`. Owner-thread draining records success/failure and invalidates the prior frame/Files delivery once for inserted and duplicate outcomes. `unknown_boot` does not invalidate. Poll drains but does not auto-retry. Close drains already admitted writer work on the actor, closes writer and observer there, and rejects new work.
- [ ] **Step 5: Implement the preparation fence without adding a second frame writer.** Preserve `prepare()` reset ownership, `_retired_tokens`, `_local_future`, `pending_work`, the source-generation rules and the existing recovery `_refresh_after_apply` writers. A local requested source is the capture opening vector after the fence, never a third shell observation.
- [ ] **Step 6: Run the exact writer and ordering controls.**

```bash
python -m pytest -q tests/p3/test_live_overview_router.py -k 'transport_gap'
python -m pytest -q tests/p3/test_collector_window.py -k 'gap or recovery_transition_ownership or no_frame_poll or no_frame_deferred'
python -m pytest -q tests/p3/test_history_router.py -k 'transport_loss_is_local or local_settled_totals'
```

- [ ] **Step 7: Commit the source-owned gap path.**

```bash
git add src/appio/history_source.py src/appio/history_router.py tests/live_overview_support.py tests/p3/test_live_overview_router.py tests/p3/test_collector_window.py tests/p3/test_history_router.py
git commit -m "feat(history): persist transport gaps through the managed GUI source"
```

## Task 5: Cut over the router surface and every production/test caller

**Implementer tier:** Advanced

**Lane:** `integration`
**Depends on:** Task 3, Task 4
**Specification:** §§3.1, 3.2, 3.4, 4.4–4.5, 6.1, 12.2, 12.4, 12.8–12.10, amendment rows A09, A12 and A18. This is one coordinated integration task. Do not commit a narrower constructor while any listed caller still passes an unmanaged connection.

**Files:**

- Modify: `src/appio/history_router.py`
- Modify: `src/appio/collector_owner.py`
- Modify: `src/appio/ui/collector_window.py`
- Modify/Test: `tests/live_overview_support.py`
- Modify/Test: `tests/p3/acceptance_harness.py`
- Modify/Test: `tests/p3/test_collector_owner.py`
- Modify/Test: `tests/p3/test_collector_window.py`
- Modify/Test: `tests/p3/test_history_router.py`
- Modify/Test: `tests/p3/test_live_overview_router.py`
- Modify/Test: `tests/p3/test_live_overview_client.py`
- Modify/Test: `tests/p4/test_file_history_router.py`
- Modify/Test: `tests/p4/test_file_history_isolation.py`
- Modify/Test: `tests/p4/test_files_tab_process_scope.py`
- Modify/Test: `tests/p4/test_files_tab_real_store.py`
- Modify/Test: `tests/p4/test_files_tab_clipping.py`
- Modify: `src/appio/history_export.py` only if a caller signature must be adapted. Preserve its producer/export format and source-security behavior.
- Read-only compatibility input: `src/appio/iodclient.py`, `src/appio/iod_history.py`, `src/appio/query.py`, `src/appio/migrations.py`, `src/appio/schema.py`

**Interfaces:**

- Set the constructor to `HistoryRouter(sources: ManagedSourceRegistry, client: IodClient, registry: AppRegistry, *, clock_ns: Callable[[], int] = time.monotonic_ns)` and remove the arbitrary local `QueryService` source authority.
- Implement `add_managed_source(spec: ManagedSourceSpec) -> Future`, `retire_managed_source(name: str, reason: str) -> None`, `replace_managed_source(name: str, spec: ManagedSourceSpec) -> None`, `local_position(source_name: str) -> LocalPosition`, `source_position(...) -> SourcePosition`, `preparation_source(wanted) -> SourcePosition | None`, `frame_provenance(...)`, `ranking_provenance(...)`, and the existing public detail/Files methods against the registry.
- Keep `add_source` as a synchronous fail-closed compatibility entry point for a `ManagedSourceSpec` only. `QueryService` and raw `sqlite3.Connection` inputs raise the exact `SourceAdmissionError("history source is not managed")` before inspecting or certifying them. No compatibility path calls `connection`, `backup`, `stat`, or source pathname logic.
- `RequestToken` includes requested source, owner and authority. `LocalFrameOutcome`, `LocalPartOutcome` and `LocalFilesOutcome` use the exact snapshot-bound interfaces from Task 3. `RemoteSettlement` preserves the complete remote cursor captured at accepted prepare. `LocalSettlement` carries capture and outcome.
- `_settle(self, token, outcome=None, *, retiring=False) -> bool` is the sole owner-thread writer for local and remote outcome publication. It re-enters `__post_init__` validation, checks exact request/app key/selector, worker binding, capture ID/digest, snapshot, registration epochs, token authority and source equality, then publishes one result or `RequestRejected("invalid_result", "invalid_result")` without a client query for local inputs.
- `CollectorCoordinator._accept_export` preserves read-only checkpoint and handover validation, then registers the immutable export `ManagedSourceSpec` with owner `"iod"`. Any temporary validation connection is closed on its creating thread and is never passed to router or registry. `_exports` tracks managed registration cleanup and never stores a source connection.
- `run_interactive` bootstraps schema with a temporary `AppIoStore(database_path)`, registers the primary managed GUI spec asynchronously, constructs client/router/coordinator/window only after the registration Future completes on the owner thread, and preserves the current `_current_window` boot fallback through a private router-owned actor query without exposing a connection. Startup failure exits through the existing application path after closing partial resources and re-raises the original exception after `exec()`.
- The shell keeps `_pending_source`, `_displayed_source` and `_displayed_certified`. `_submit_prepare` reads `history.preparation_source(wanted)` after prepare and after each owner-thread drain if initially absent. Local requested source is the capture opening vector after the gap fence. `_apply_frame` requires matching router generation, advances displayed source only for a matching certified answer, and otherwise leaves the last proved displayed source unchanged.
- `LiveOverviewHarness`, `acceptance_harness`, `CollectorCoordinator` fixtures, P3 acceptance queries and all P4 Files routers use `ManagedSourceRegistry` and managed specs. `stores.attach`, `replace`, export import and interactive composition never pass a `QueryService` or raw connection into `HistoryRouter`.

- [ ] **Step 1: Inventory and add fail-closed constructor tests.** Enumerate every `HistoryRouter(`, `.add_source(` and `.remove_source(` call in the Files block. Add a valid managed-spec control and synchronous invalid `QueryService` and raw-connection arms with the exact exception text. The control must prove that the compatibility path forwards only the spec.
- [ ] **Step 2: Update all test composition before changing the constructor.** Give each harness one registry and source wake, register the primary GUI source, register imported sources through managed specs, and retain independent external writer connections only for fixture commits. Preserve actual queued local completion delivery and the no-double-attach guard. Update `acceptance_harness.py` at the existing query, import and render construction sites.
- [ ] **Step 3: Narrow the constructor and migrate production composition atomically.** Change `run_interactive`, export acceptance, coordinator close, harness setup and every P3/P4 construction in one edit wave. Remove the current `HistoryRouter(local_service, ...)` and `add_source(QueryService(...))` paths. Use the exact managed source names, owners and file-backed URIs needed by each fixture. Keep the existing export checkpoint tests, candidate-offer semantics, boundary issue text and owner-thread/reentrant guards.
- [ ] **Step 4: Wire source-triggered prepare and shell provenance.** Preserve A22 precheck ordering, source observation refusal, recovery poll ownership, no-pending poll behaviour and `_refresh_after_apply` ownership. Verify that a refused Window never observes a source or advances displayed provenance. Keep `_current_window`'s verified-answering remote cursor path and use the actor-backed local boot fallback only when the daemon is not answering.
- [ ] **Step 5: Integrate all local settlement variants.** Route frame, selected totals/series and Files through the same owner-thread settlement and capture lease release. Assert zero remote calls for local inputs. Make stale success/error, retired source, duplicate settlement and malformed worker result terminal without publication or capacity release of another owner.
- [ ] **Step 6: Run the constructor and composition inventory before broader tests.**

```bash
rg -n 'HistoryRouter\(|\.add_source\(|\.remove_source\(' src tests
python -m pytest -q tests/p3/test_collector_owner.py tests/p3/test_history_router.py tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py
```

- [ ] **Step 7: Commit the coordinated caller cutover.** Stage every migrated caller and fixture from the Files block in one commit. Do not leave a temporary compatibility constructor or unmanaged source path in the tree.

```bash
git add src/appio/history_router.py src/appio/collector_owner.py src/appio/ui/collector_window.py tests/live_overview_support.py tests/p3/acceptance_harness.py tests/p3/test_collector_owner.py tests/p3/test_collector_window.py tests/p3/test_history_router.py tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py tests/p4/test_files_tab_process_scope.py tests/p4/test_files_tab_real_store.py tests/p4/test_files_tab_clipping.py
if git diff --name-only -- src/appio/history_export.py | grep -q .; then git add src/appio/history_export.py; fi
git commit -m "refactor(history): migrate router callers to managed sources"
```

## Task 6: Complete settlement, source-retirement and startup regression coverage

**Implementer tier:** Capable

**Lane:** `regression`
**Depends on:** Task 5
**Specification:** §§2.3–2.4, 3.2–3.4, 4.3–4.5, 6.1, 12.2, 12.4, 12.8–12.10. This task adds mechanism-specific acceptance evidence after the complete cutover.

**Files:**

- Modify/Test: `tests/p3/test_live_overview_router.py`
- Modify/Test: `tests/p3/test_live_overview_client.py`
- Modify/Test: `tests/p3/test_collector_window.py`
- Modify/Test: `tests/p3/test_collector_owner.py`
- Modify/Test: `tests/p3/acceptance_harness.py`
- Modify/Test: `tests/live_overview_support.py`
- Modify/Test: `tests/p3/test_history_router.py`
- Modify/Test: `tests/p4/test_file_history_router.py`
- Modify/Test: `tests/p4/test_file_history_isolation.py`
- Modify/Test: `tests/p4/test_file_served_interval.py`

**Interfaces:**

- `test_local_outcomes_share_settlement_for_frame_part_and_files` has one positive arm for each local variant, zero `client.query` and `query_transport` calls, exact frame routing evidence, exact part digest and Window, exact Files table/coverage/served-gap evidence, matching snapshot provenance, and one publication per token. Independently corrupt worker ID/request association, frame evidence, part key/type/Window/digest and Files table/coverage/served gap. Each arm fails before owner evidence publishes.
- `test_settlement_rejects_each_selector_shape_or_binding_violation` includes the exact three unscoped process mappings with every other field valid. `{"unit:/alpha.service": <valid Coverage>}` is the key-only fault, `{"unit:alpha.service": "not a Coverage"}` is the value-only fault, and `{"unit:alpha.service": <valid Coverage>}` is the passing control. Keep selector, key, kind, Window, duplicate identity and UnknownOwnerOverflow faults independent.
- `test_settlement_superseded_or_duplicate_has_no_publication_effect` covers superseded generation, dead detail lifetime, source authority, scope, boot, subject, delayed success/error and duplicate settlement. No unrelated record, stamp, diagnostic, pending marker or capacity charge changes.
- Source and shell regression coverage includes `test_held_local_detail_accepts_once_after_forward_frames`, `test_authority_change_clears_details_without_conflating_cursor_advance`, `test_authority_change_retires_pending_frame_once_and_replacement_applies`, `test_local_worker_capacity_counts_retired_jobs_and_keeps_frame_independent`, `test_unchanged_certified_local_or_remote_source_is_quiescent`, `test_pending_source_advances_coalesce_without_retiring_detail`, `test_no_frame_poll_drains_details_and_files_and_notifies_immediately`, and `test_no_frame_deferred_apply_uses_ordinary_gate_but_poll_never_submits`.
- Preserve the old router controls `test_local_settled_totals_remain_answerable_and_ids_are_presentational`, `test_adjacent_sources_merge_by_appkey_not_database_ids`, `test_shared_boot_order_evidence_beats_the_order_sources_were_added`, and `test_transport_loss_is_local_and_does_not_override_a_complete_export`.
- Preserve export tests `test_export_is_opened_read_only_and_must_match_its_checkpoint`, `test_export_from_a_running_daemon_is_not_a_resume_permit`, `test_inactive_export_retires_unsampled_gui_and_installs_before_the_first_tick`, and the owner-thread, reentrant, close and boundary issue assertions. Verify the registered export is managed and no export connection is retained by the coordinator.
- Add the exact startup lifecycle test `test_transport_gap_startup_and_callback_shutdown_order`. Delay and fail primary registration through the real wake object, prove no pump or prepare starts before admission, bootstrap closure remains on its creating thread, startup errors surface and re-raise, and shutdown closes coordinator/client before the registry actor while an admitted writer finishes or fails without touching the closed window.

- [ ] **Step 1: Make the fixture expose real source authority and capture barriers.** `LocalPositionStores` must retain observer connections across frames, provide independent committed writers and explicit registration replacement, and keep `_DeliveryQueue` completions queued until the owner drains. `SqliteProxy` may pause backup only to expose the specified bracket schedule. No fixture may manufacture a source position or substitute a worker observer.
- [ ] **Step 2: Add local settlement discriminators.** Extend the exact three settlement names and test all malformed arms independently. Assert the production publication state before and after each drain, including stale success/error and a duplicate terminal completion.
- [ ] **Step 3: Add provenance and retirement schedules.** For local and remote sources, test equal source certification, unequal backup/closing brackets, commit-after-closing detection, source replacement with a fresh epoch, stale capture cleanup, and same-authority sequence/counter advancement. Prove one replacement, one retirement and no old certification under the specified barriers.
- [ ] **Step 4: Add asynchronous startup and export tests.** Drive registration completion through the owner-thread queue. Prove the router/client/coordinator are not constructed before primary registration, bootstrap store closure occurs on its creating thread, startup errors exit and re-raise, interactive `_current_window` still finds the latest local boot, and export checkpoint validation remains read-only before managed registration.
- [ ] **Step 5: Run the focused regression inventory.**

```bash
python -m pytest -q tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py tests/p3/test_collector_window.py tests/p3/test_collector_owner.py tests/p3/test_history_router.py
python -m pytest -q tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py tests/p4/test_file_served_interval.py tests/p4/test_files_tab_process_scope.py tests/p4/test_files_tab_real_store.py tests/p4/test_files_tab_clipping.py
```

- [ ] **Step 6: Commit only regression fixtures and tests.**

```bash
git add tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py tests/p3/test_collector_window.py tests/p3/test_collector_owner.py tests/p3/acceptance_harness.py tests/live_overview_support.py tests/p3/test_history_router.py tests/p4/test_file_history_router.py tests/p4/test_file_history_isolation.py tests/p4/test_file_served_interval.py tests/p4/test_files_tab_process_scope.py tests/p4/test_files_tab_real_store.py tests/p4/test_files_tab_clipping.py
git commit -m "test(history): prove source retirement snapshot settlement and startup ordering"
```

## Task 7: Prove the gap writer and managed-source boundaries with isolated mutations

**Implementer tier:** Advanced

**Lane:** `verification`
**Depends on:** Task 6
**Specification:** §13, §12.1–§12.9 and §12.10, with preservation of §§4.3–4.5, 6.1 and the prior recovery specification. This task also closes the specification's own named-test inventory: every test name listed in §12 tables must exist exactly once at its specified file, and every preservation gate named by the brief must pass at lane head.

**Files:**

- Modify/Test: `tests/p3/test_live_overview_router.py`
- Modify/Test: `tests/p3/test_collector_window.py`
- Modify/Test: `tests/p3/test_collector_owner.py`
- Modify/Test: `tests/p3/test_history_router.py`
- Modify/Test: `tests/live_overview_support.py`
- Modify/Test: `tests/p3/test_live_overview_client.py`
- Modify/Test: `tests/test_ui.py`
- Modify/Test: `tests/p3/test_history_overlap_entry.py`
- Read-only during mutation probes: `src/appio/history_source.py`, `src/appio/history_router.py`, `src/appio/ui/collector_window.py`, `src/appio/collector_owner.py`

**Interfaces:**

- Exact mutation tests and controls are named below. Each run uses a fresh disposable copy, proves imports resolve into that copy, applies one source mutation, requires a failure at the named assertion, restores the mutation and reruns the identical control green.
- The isolated mutation harness must preserve actual SQLite connections, actor thread identity, barrier ordering and queued owner-thread delivery. It must never count collection errors, missing dependencies, setup exceptions or a fixture timing accident as a kill.

- [ ] **Step 1: Create the disposable-copy runner and import proof.** Copy the worktree excluding `.git`, `.superpowers`, virtual environments, caches, build artifacts and runtime artifacts. Set `PYTHONPATH` to the copy's `src` and `tests`, run `from pathlib import Path; import appio.history_source as m; assert Path(m.__file__).is_relative_to(Path.cwd())`, and retain the absolute copy path in the runner state. Remove the copy in `finally` after logs and assertions are captured.
- [ ] **Step 2: Add and run the exact gap-writer acceptance names.** The production and test inventory must contain and exercise `test_transport_gap_atomic_dedup_with_concurrent_writers`, `test_transport_gap_writer_registration_retirement_and_close`, `test_transport_gap_unknown_boot_bounds_and_epoch_fallback`, `test_transport_gap_observer_visibility_rejects_divergent_pair`, `test_transport_gap_callback_persists_before_frame_capture`, `test_transport_gap_fence_error_and_later_gap_ordering`, `test_transport_gap_writer_failure_retry_and_commit_ambiguity`, and `test_transport_gap_startup_and_callback_shutdown_order`.
- [ ] **Step 3: Run the §13 mutation battery.** In separate copies, mutate one mechanism at a time and retain the named failing assertion.

  - Move the duplicate check after `BEGIN IMMEDIATE`. The concurrent-writer test must fail on duplicate insertion or its exact cardinality assertion.
  - Remove query-only/authorizer enforcement or the shared-cache binding challenge. `test_transport_gap_writer_registration_retirement_and_close` or `test_transport_gap_observer_visibility_rejects_divergent_pair` must fail at the write-refusal, thread-identity, visibility or divergent-pair assertion.
  - Allow an unknown boot, empty identity or invalid bound to write. `test_transport_gap_unknown_boot_bounds_and_epoch_fallback` must fail the no-write and disposition assertion.
  - Omit the same-observer post-commit visibility check or accept a divergent writer/observer pair. `test_transport_gap_observer_visibility_rejects_divergent_pair` must fail before source certification.
  - Bypass the preparation fence or invalidate a newer fenced generation with an older gap completion. `test_transport_gap_callback_persists_before_frame_capture` and `test_transport_gap_fence_error_and_later_gap_ordering` must fail at their barrier ordering and capture-count assertions.
  - Ignore commit failure, classify an ambiguous commit as unknown boot, or skip same-epoch retry. `test_transport_gap_writer_failure_retry_and_commit_ambiguity` must fail the retained error, retry and deduplication assertions.
  - Deliver an old writer completion to a replacement epoch or close the actor-owned connection on the owner thread. `test_transport_gap_writer_registration_retirement_and_close` must fail its epoch, terminal error or thread-identity assertion.
  - Remove the real client callback wiring. `test_transport_gap_callback_persists_before_frame_capture` must fail because the callback no longer persists before the next capture.
  - Remove local worker binding, source-incarnation or snapshot validation. `test_local_outcomes_share_settlement_for_frame_part_and_files` must fail before evidence publication.
  - Remove the shared unscoped process-mapping Coverage-value predicate while preserving key, kind, request, Window and framing validity. The value-only `"not a Coverage"` arm in both `test_settlement_rejects_each_selector_shape_or_binding_violation` and the malformed-result retry control must fail. The valid mapping control and key-only fault must remain independently classified.
  - Restamp an identical accepted local identity, omit the full remote sequence from a cache key, or certify an older source. `test_unchanged_certified_local_or_remote_source_is_quiescent` and the existing full-cursor cache/provenance controls must fail their source and age assertions.
  - Allow a poll-only deferred submission or share the frame/background executor. `test_no_frame_deferred_apply_uses_ordinary_gate_but_poll_never_submits` and the held local-detail capacity arm must fail at their prepare or executor-isolation assertions.

- [ ] **Step 4: Restore and rerun every control.** Record mutation path, source edit, test node, expected assertion, observed exit, restored exit and whether the arm was killed, equivalent or unexercised. Do not report an operational setup failure as a mechanism kill.
- [ ] **Step 5: Run the preservation gates and exact collection inventory.**

```bash
python -m pytest -q tests/p3/test_history_overlap_entry.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_history_router.py tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py
python -m pytest
python -m pytest --collect-only -q
python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/history_types.py src/appio/history_validation.py src/appio/history_router.py src/appio/iodclient.py src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py
```

Compare the exact named-test inventory with the revision-8 specification. If the optional development-only mypy executable is unavailable, record that environmental result without weakening the pytest gates or installing a dependency.
- [ ] **Step 5b: Complete the specification's §12 named-test inventory and migrate the production-entry integration test.**
  Parse every backticked `test_*` name from the specification's §§12.1–12.9 tables, collect the top-level definitions with `ast`, and reconcile the two sets. Author a top-level test at its specified file for every name that is absent, driving the production seam the §12 row describes with real fixtures and at least one failing-if-removed assertion. Add no duplicate definition and rename nothing that already exists.

  Then migrate `tests/p3/test_history_overlap_entry.py::test_live_incomplete_reply_cannot_resurrect_export_overlap_in_production_entry`: `run_interactive` now admits its managed source asynchronously and creates the window after admission, so the replaced event loop must wait for owner-thread admission and completion instead of assuming a synchronously constructed `CollectorWindow`, and the legacy `history.local.connection` access must use the managed source position. Preserve the test's scenario: an incomplete live reply must not resurrect an export overlap in the production entry path.

  The preservation gate must pass at lane head, not be reported as an out-of-scope failure.

- [ ] **Step 6: Commit the verified mutation, inventory and preservation tests.**

```bash
git add tests/p3/test_live_overview_router.py tests/p3/test_collector_window.py tests/p3/test_collector_owner.py tests/p3/test_history_router.py tests/live_overview_support.py
git commit -m "test(history): prove managed source and gap writer boundaries"
```

## Task 8: Final integration review and evidence package

**Implementer tier:** Frontier

**Lane:** `acceptance`
**Depends on:** Task 7
**Specification:** §§1–4, 6, 12.2, 12.4, 12.8–12.10, 13 and all revision-8 amendments.

**Files:**

- Modify only for a proved contract defect: `src/appio/history_types.py`, `src/appio/history_source.py`, `src/appio/history_router.py`, `src/appio/collector_owner.py`, `src/appio/ui/collector_window.py`
- Modify only for a directly corresponding failing assertion: files in Tasks 1, 4, 5, 6 and 7
- Read-only: specification, approved design, prior plan, `src/appio/history_export.py`, `src/appio/iodclient.py`, `src/appio/query.py`, `src/appio/migrations.py`, `src/appio/schema.py`, `src/appio/iod_history.py`, `src/appio/ui/theme.py`, `src/appio/ui/files_view.py`

**Interfaces:**

- The final review consumes all prior task outputs, exact named test inventory, source-actor evidence, gap-writer mutation ledger, constructor/caller inventory and the recovery preservation gates.
- It produces a self-contained final evidence record stating the spec digest, changed files, task commits, focused/full/collection/typecheck results, each mutation status and any exact unresolved environmental limitation. It may repair only a directly evidenced contract defect in the listed ownership files and must rerun the affected focused test, the complete full suite and the exact mutation control before committing.

- [ ] **Step 1: Re-read the binding specification and inspect the complete diff.** Verify no pathname certificate, unmanaged source path, worker connection, schema/wire/sampling change, new recovery writer, extra source authority, or host-transition action entered the implementation. Verify the primary writer/observer pairing and actor shutdown order from source, not from test names alone.
- [ ] **Step 2: Re-run the complete gates after any evidence-based repair.** Use the focused six-module inventory, full offscreen pytest, exact `--collect-only -q` comparison, and the documented mypy command. Run all repaired mutation controls from fresh copies. Do not stop at a focused green run if a full or collection gate remains outstanding.
- [ ] **Step 3: Verify the production entry points.** Exercise the interactive composition through its startup queue, managed primary registration, client callback, local source capture, export registration, owner-thread drain and clean shutdown. Exercise the acceptance harness and every P4 Files route with managed specs. Keep application tests offscreen and deterministic.
- [ ] **Step 4: Record the final result and commit only if a repair was required.** A repair commit must name the failing assertion and source contract it restores. Otherwise leave the implementation commits intact and attach the evidence record to the PM run. Never add generated logs, disposable copies, probes or reports to the repository.

```bash
git status --short
python -m pytest -q tests/p3/test_history_overlap_entry.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py tests/p3/test_history_router.py tests/p3/test_live_overview_router.py tests/p3/test_live_overview_client.py
python -m pytest
python -m pytest --collect-only -q
```

- [ ] **Step 5: Commit the final evidence package.** The final report must state whether all revision-8 requirements and exact tests passed, list any environmental typecheck limitation precisely, and identify any mutation that was equivalent or unexercised. If a repair was required, commit only the directly evidenced source/test repair and name the failing assertion it restores. Otherwise leave the implementation commits intact and attach the evidence record to the PM run. Never add generated logs, disposable copies, probes or reports to the repository.
