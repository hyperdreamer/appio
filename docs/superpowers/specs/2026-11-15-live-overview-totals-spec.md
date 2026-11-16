# appio live Overview lane and retained totals: technical specification

Date: 2026-11-15. Status: implementation contract for the approved design, revision 7.

## 1. Authority, provenance, and boundaries

Behavioural authority: `docs/superpowers/specs/2026-11-15-live-overview-totals-design.md`, §§1–13. The task's approval establishes that file as authority despite its historical `draft for review` header. Drafting baseline: `413f35aa53997ca543b01a878b96aac17490efc5`; design Git blob: `77e62e363d340c292911d4e905e87b94ffa2b43a`; design SHA-256: `8d6941b545ba15c9338f94df63d9d5c6b0b861e7cc7f14f8e85e2a675a6d70d9`.

This document amends exactly the eighteen locations enumerated by design §9. Section 11 maps each amendment to its replacement contract here. All other contracts in the monitor, Files, charts, silent-daemon recovery, and dead-scope-controls specifications remain in force. In particular, recovery-spec §4.4 transition ownership and §4.5 preparation-error ownership are unchanged. A **specification decision** below chooses a mechanism necessary to implement an approved decision; it does not authorise another feature.

Requirements are prospective. Source references describe the drafting baseline; test tables describe required implementation evidence, not executed results. No application suite is to be run as part of drafting this specification.

### 1.1 Checked implementation facts

| Existing evidence | Consequence |
| --- | --- |
| `history_router.py::prepare` resets `_remote_parts`, `_cached_parts`, `_queue`, and `_requested_parts`; `_request_part` sends directly and has its own callback writer. | Replace detail publication with one settlement function and retain scope-owned records independently of frame preparation. |
| `_request_remote_files` sends the first page, continuation, and coverage directly. | All three enter the same scheduler as ranking and subject detail; existing Files acceptance predicates remain mandatory. |
| `_read_local` constructs totals and series for every ranked row; `ready` waits for the entire remote queue. | Prepare ranking and ownership only; construct selected detail lazily and exclude it from frame readiness. |
| `IodClient._expire` removes a pending request and fails its Future without cancelling the daemon job. | Track transport completion separately from Future completion and retain correlation after expiry. |
| `IodClient._generation_token` contains boot and epoch but not sequence; decoded totals/series omit their external subject metadata. | Use the full cursor in the cache key and preserve external subjects without changing wire records. |
| `QueryService.connection` is long-lived; private worker connections are opened for backup. | Observe source positions on the registered connection, outside transactions, rather than on a new worker connection. |
| `SeriesChart` reaggregates all samples into time-pixel bins; `_apply_timeline` uses full setters. | Add bucket-anchored append state and per-run polylines, preserving exact sums, independent scales, and hole exclusion. |
| `CollectorWindow` owns `_pending`, `_displayed`, the recovery submission gate, and the transport pump; `AppIoWindow` uses a structural query protocol. | Keep those owners, add source certification, and expose detail/provenance through a structural optional interface. |

### 1.2 Implementation boundary and module inventory

Only the following production modules acquire interfaces or changed behaviour. Existing unlisted public symbols retain their contracts.

| Module | Authorised responsibility |
| --- | --- |
| `src/appio/history_types.py` (new) | Immutable, Qt-free position, scope, acceptance, retained-record, series metadata, slot-state, and frame-provenance records and pure scope/digest helpers (§2). |
| `src/appio/history_validation.py` (new) | Shared pure request/selector validation before client caching and router acceptance; no publication or I/O (§5.4). |
| `src/appio/history_router.py` | Scope-owned detail lifecycle, lazy local reads, positions, one query scheduler and settlement writer (§§3–6). |
| `src/appio/iodclient.py` | Internal query-to-transport observation, expired correlation, connection retirement, cursor-complete caching, decoded subject metadata (§5). |
| `src/appio/ui/render_model.py` | Per-dependency provenance, measured-window admission, independent availability and lane composition (§7). |
| `src/appio/ui/main_window.py` | Optional retained capability, UI-local fallback adapter, detail-only updates, append callers, measurement observations (§§6–9). |
| `src/appio/ui/charts.py` | Append methods/state, canonical intervals, curves and closed fills (§8). |
| `src/appio/ui/collector_window.py` | Source-driven preparation, certification, continued polling, one status composer and separate age label (§§6, 9). |

**Specification decision:** the neutral `history_types.py` module imports only the standard library and `appio.contracts`. It owns no I/O, registry, concrete query service, client, router, Qt object, Future, or mutable global cache. `history_validation.py` imports only the standard library, `appio.contracts`, `appio.ipc_models.QueryRequest`, and the neutral helpers; it never imports client/router/Qt or inspects owner state. The pure renderer may additionally import the neutral records/helpers; this is solely the provenance representation required by design §9, not permission to import a router or producer. No field or signature is added to `appio.contracts`, `appio.query.QueryService`, `QueryRequest`, the store, or any wire/durable record.

No dependency, schema migration, sampling change, device-attribution relaxation, new scope control, privileged action, service repair, or host transition is part of this change. Direct-detail refusal still clears both presentation surfaces as specified by the dead-scope-controls specification; a shell preparation refusal still preserves its last frame.

## 2. Exact neutral data structures and validation

### 2.1 Common rules, scope, and accepted identity

Every record shown in this section is `@dataclass(frozen=True)` with exactly the listed fields and `__post_init__(self) -> None`. Invalid direct or nested construction raises `ValueError`, never an assertion. Integer fields exclude `bool`; counters and monotonic timestamps are unbounded nonnegative Python integers unless a stricter lower bound is stated. Text is nonempty and UTF-8 encodable. Collections are tuples. Existing contract values retain their own validation; these records do not coerce a failure to zero or a missing identity to a string.

```python
PartKind = Literal["totals", "series"]
AnswerKind = Literal["ranking", "totals", "series"]
PartPhase = Literal["absent", "pending", "ok", "failed"]
RetentionScope = (
    tuple[str, Literal["range"], str, int, int]
    | tuple[str, Literal["since_boot"], str]
    | tuple[str, Literal["lifetime"]]
    | tuple[str, Literal["launch"]]
)

@dataclass(frozen=True)
class AcceptedIdentity:
    kind: AnswerKind
    app_key: str
    window: Window
    data_digest: str


def retention_scope(app_key: str, window: Window) -> RetentionScope: ...
def applied_endpoint(window: Window) -> int | None: ...
def local_answer_digest(kind: AnswerKind, app_key: str,
                        window: Window, value: AppTotal | DiskShare | Series) -> str: ...
```

`retention_scope` returns exactly:

| Window kind | Tuple |
| --- | --- |
| `range` | `(app_key, "range", window.boot_id, window.start_mono_ns, window.end_mono_ns)` |
| `since_boot` | `(app_key, "since_boot", window.boot_id)` |
| `lifetime` | `(app_key, "lifetime")` |
| `launch` | `(app_key, "launch")` |

Tuple length, tag, subject and embedded bounds must agree with the supplied valid `Window`. Range bounds remain half-open and strictly increasing. `applied_endpoint` returns `end_mono_ns` for range/since-boot and `None` for lifetime/launch. A lifetime window never acquires a boot or endpoint. The launch key supplies no launch semantics: A22 refuses launch before admission.

`AcceptedIdentity` is the complete logical four-tuple `(kind, external subject, exact Window, data digest)`; its equality includes all five fields of `Window`, including `None` fields. A digest is exactly 64 lowercase hexadecimal ASCII characters. For a remote answer it is the returned daemon `snapshot`, not a hash of that snapshot, a cursor, or a locally guessed database identity. For a local answer use `local_answer_digest`. Presentation `app_id` is not an external identity.

**Specification decision — local digest encoding.** Hash the UTF-8 encoding of canonical JSON `["appio-local-answer-v1", kind, app_key, encoded_window, encoded_value]` with SHA-256. JSON uses `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`, `allow_nan=False`. Encode a contract dataclass as a field-name object recursively; encode tuples as arrays and include `None` explicitly. Encode the exact Window with its five fields. Omit `app_id` from AppTotal/Series/DiskShare and their nested records; retain AppTotal's `app_key` and require it to equal the external subject. No path, connection identity, acceptance time, or registration epoch enters this payload digest. Retain array order as returned/validated; do not sort samples into acceptability. Hash every remaining contract field, including coverage, optional peaks, suspension, device and residual records. Validate the value type for `kind` and its subject/window association first. Nonfinite values that cannot be encoded are invalid results. This is payload hashing on the worker/result path, not table hashing for local positions (§4).

### 2.2 Source positions and local copy bracket

```python
@dataclass(frozen=True)
class RemotePosition:
    boot_id: str
    iod_epoch: int
    seq: int

@dataclass(frozen=True)
class LocalPosition:
    name: str
    owner: Literal["gui", "iod"]
    registration_epoch: int
    data_version: int
    total_changes: int

@dataclass(frozen=True)
class LocalPositionVector:
    sources: tuple[LocalPosition, ...]

@dataclass(frozen=True)
class LocalBracket:
    opening: LocalPositionVector
    closing: LocalPositionVector

SourcePosition = RemotePosition | LocalPositionVector
```

| Record/field | Validation and meaning |
| --- | --- |
| `RemotePosition.boot_id` | Nonempty boot identity, obeying the existing StreamCursor boot-text validation. |
| `RemotePosition.iod_epoch`, `seq` | Epoch is at least 1; sequence is at least 0, with the same integer validation as StreamCursor. Conversion copies the full `ClientState.cursor` triple. |
| `LocalPosition.name` | Exact registered source name, including `local` for the initial source. |
| `owner` | Exactly `gui` or `iod`, the registered owner; not a display-state token. |
| `registration_epoch` | At least 1, allocated by this router's strictly increasing registration counter; never reused within its lifetime. |
| `data_version` | At least 1, scalar result of `PRAGMA main.data_version` on this registered service's connection. |
| `total_changes` | At least 0, that same connection's `total_changes` property. |
| `LocalPositionVector.sources` | Only LocalPosition records, unique names, strictly sorted by `name.encode("utf-8")`. Include every registered source, not only `local` or the currently contributing store. The open router always has its primary source. |
| `LocalBracket` | Two independently observed vectors; equality is not required for construction. An unequal bracket is valid evidence of a race, never a successful position certificate. |

Source positions are tagged by their record types. Local and remote positions never compare equal. No cursor sequence change is a connection-authority change. Conversely, a local registration change or remote connection/boot/epoch change is an authority change even if a payload or raw counter repeats.

### 2.3 Per-dependency provenance, retained record, and settled state

```python
@dataclass(frozen=True)
class AnswerProvenance:
    kind: AnswerKind
    app_key: str
    scope: RetentionScope
    measured_window: Window
    accepted_identity: AcceptedIdentity | None
    first_seen_at_ns: int | None
    answered_source: SourcePosition | None
    local_bracket: LocalBracket | None

@dataclass(frozen=True)
class RetainedRecord:
    value: DiskShare | Series
    provenance: AnswerProvenance

@dataclass(frozen=True)
class PartState:
    kind: PartKind
    app_key: str
    scope: RetentionScope
    state: PartPhase
    request_id: int | None
    requested_window: Window | None
    record: RetainedRecord | None
    reason: str | None

@dataclass(frozen=True)
class FrameProvenance:
    generation: int
    window: Window
    requested_source: SourcePosition
    answered_source: SourcePosition | None
    local_bracket: LocalBracket | None
    certified: bool

@dataclass(frozen=True)
class SeriesMetadata:
    app_id: int
    app_key: str
    measured_window: Window
    tier: Tier
    coverage: Coverage
```

Exact field obligations:

| Field | Rule |
| --- | --- |
| `AnswerProvenance.kind`, `app_key`, `scope` | Legal kind, nonempty external key, and `scope == retention_scope(app_key, measured_window)`. Ranking provenance is per subject row, not an identity for a whole anonymous list. |
| `measured_window` | The actual requested Window accepted at settlement. It is never changed to the newest preparation or applied window. |
| `accepted_identity`, `first_seen_at_ns` | Both present or both `None`. When present, kind/subject/window exactly match the provenance and the stamp is nonnegative. Production router acceptances always provide both. The UI fallback may lack both. |
| `answered_source` | The issue-time remote cursor retained by the correlated query, or an equal local bracket's vector. `None` means no position is proved. A source is provenance, not an observation-time claim. |
| `local_bracket` | `None` for remote answers. For local answers retain both observations; when equal, `answered_source` equals that vector; when unequal, `answered_source is None`. |
| `RetainedRecord.value` | DiskShare iff provenance kind is totals, Series iff series; positive presentation subject ID and `value.window == provenance.measured_window`. Ranking cannot be stored as a detail record. Revalidate nested contracts before publication. |
| `PartState.kind`, `app_key`, `scope` | Exact slot key. If a requested window exists, its retention scope equals this key. |
| `request_id` | Router-wide positive, never-reused request token number; distinct from a daemon wire request ID. |
| `requested_window` | Immutable exact Window of that token. It is independent of any newer applied endpoint. |
| `record` | Present only for state ok; its kind/key/scope/window match the slot and token. Pending does not embed an older record; the separate retained accessor supplies it. |
| `reason` | Nonempty rendered reason only for failed; `None` for the other states. Preserve error text as specified in §3.4. |
| `FrameProvenance.generation`, `window` | Nonnegative frame generation and exact prepared window. |
| `requested_source` | Captured at the accepted `_submit_prepare` call, before enqueuing remote ranking or submitting the local copy. |
| `certified` | Exact bool; true iff ranking succeeded, answered_source is present, and equals requested_source. An unequal local bracket cannot certify. Remote failure/local fallback cannot certify a remote requested position. |
| `SeriesMetadata` | Positive presentation app_id, validated external app_key, exact measured Window, legal contract Tier and revalidated Coverage; `tier == coverage.tier`. It describes the successful effective Series from which the model projects points, including an available-empty or lifetime Series. It is not inferred from a coverage label or from share's possibly different tier. |

The PartState combinations are exhaustive:

| State | `request_id` / `requested_window` | `record` | `reason` |
| --- | --- | --- | --- |
| absent | Both None | None | None |
| pending | Both present | None | None |
| ok | Both present | Present | None |
| failed | Both present | None | Present |

Malformed combinations raise ValueError. An exact-window cache miss is not a transition to absent or failed. A PartState is an immutable observation, not a mutable cache handle.

### 2.4 First-seen stamps and memory lifetime

On the first successful settlement of an AcceptedIdentity in a live `(kind, app_key, scope)` slot, stamp the injected monotonic clock. Reacceptance of the identical identity reuses its stamp, including local frame reapplication; a different Window with the same snapshot is a different identity. Keep the identity-to-stamp lookup for the current slot/scope lifetime so a repeated identity is not restamped merely because another value was accepted in between. Clear that lookup when the slot's subject/scope/authority is retired or its endpoint moves backwards. A current failure clears the record but does not change historical stamps for identities in the same still-live slot; only a newly accepted success may recreate a record.

**Specification decision:** this lookup is UI-session memory, not persisted history or an unbounded cross-subject cache. Retain identities only for the live selected subject/scope; frame-ranking identities are retained only for that same subject/scope and cannot supply a PEAK in another frame. There is no age inferred from the daemon cursor, bucket end, wall clock, or file modification time.

## 3. HistoryRouter interface and detail lifetime

### 3.1 Exact public surface

Existing constructor positional arguments retain their meanings. Add only an optional clock for deterministic acceptance stamps:

```python
class HistoryRouter:
    def __init__(self, local: QueryService, client: IodClient,
                 registry: AppRegistry, *,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None: ...
    def prepare(self, window: Window) -> None: ...
    def ready(self, window: Window) -> bool: ...
    def ranking(self, window: Window, metric: str) -> list[AppTotal]: ...
    def series(self, app_id: int, window: Window) -> Series: ...
    def disk_share(self, app_id: int, window: Window) -> DiskShare: ...
    def select_detail(self, app_key: str | None, window: Window | None) -> None: ...
    def request_detail(self, kind: PartKind, app_id: int, window: Window) -> PartState: ...
    def part_state(self, kind: PartKind, app_key: str,
                   scope: RetentionScope) -> PartState: ...
    def retained(self, kind: PartKind, app_key: str, scope: RetentionScope,
                 applied_endpoint: int | None) -> RetainedRecord | None: ...
    def ranking_provenance(self, app_key: str, window: Window) -> AnswerProvenance | None: ...
    def frame_provenance(self, window: Window) -> FrameProvenance | None: ...
    def preparation_source(self, window: Window) -> SourcePosition | None: ...
    def local_position(self, source_name: str) -> LocalPosition: ...
    def source_position(self) -> SourcePosition: ...
    def drain(self) -> bool: ...
    @property
    def pending_work(self) -> bool: ...
    @property
    def local_read_in_flight(self) -> bool: ...
```

Existing `add_source`, invalidation, Files query and source-status methods, diagnostics, and close semantics remain, with the necessary scheduler/registration changes specified below. The new methods are structural capabilities, not a requirement to import this class in the view.

| Symbol | Contract |
| --- | --- |
| `select_detail` | Called for the selected subject and applied Window before composing it. Both arguments None retires the selected detail lifetime; exactly one None is invalid. Same subject/scope with a nondecreasing endpoint preserves tokens and records. Changed subject/scope or backward endpoint retires them synchronously before rendering. It does not query. |
| `request_detail` | Resolve app_id through the router registry, require the selected applied subject/scope, validate exact Window/A22, and admit at most one live token for this kind/scope. An in-flight older-window request survives and is returned as pending; remember only the latest eligible desired applied Window as its successor. It does not replace a live request on each forward frame. |
| `part_state` | Pure read of the current slot, or an absent state for a valid but never-requested slot. Never submits, drains, clears, or changes a record. Reject mismatched scope/key rather than relabelling it. |
| `retained` | Pure read of the newest eligible accepted record for the live selected key. Check endpoint eligibility against the caller's **applied** endpoint. Return None for a retired slot, a current failed state, or no record. Pending may return an older accepted record. For range/since-boot require a nonnegative integer endpoint and measured end <= applied endpoint; for lifetime/launch require None. Reject wrong argument types or key/tag combinations. An ineligible future-end record returns None and is never restamped. |
| `ranking_provenance` | Return provenance only for a successful row of the matching current completed frame generation and external key; never fetch or use a previous generation to fill an unfinished one. The view captures this alongside its applied ranking rows and keeps it only for that applied frame. |
| `frame_provenance` | Return the current generation's completed certificate for its exact prepared Window, or None until that generation has completed. An older completed frame with the same Window cannot substitute. Preparation metadata alone is not a certificate. |
| `preparation_source` | Pure read of the source captured by the currently accepted preparation for this exact Window, or None if there is no matching accepted preparation. Available before readiness; never certify, query or reread current counters/cursor here. |
| `local_position` / `source_position` | Observe according to §4, on the owner thread, without mutating a store. Closed router or invalid source is an error, never a fabricated unchanged token. |
| `drain` | Collect completed local/remote jobs, settle each once, reap terminal transport slots, dispatch eligible queued work, and report whether visible frame/slot/file state changed. No wait, target calculation, prepare, Qt event processing, or widget mutation. |
| `pending_work` | True iff a frame read/query, live queued or outstanding detail, Files job/page/coverage, uncollected completion, or occupied transport slot (including expired/retired publication) still needs draining. It includes queued/running local detail and Files workers. False only when all such work is absent. |
| `local_read_in_flight` | Unchanged: `_local_future is not None and not _local_future.done()`. This is the frame-read Future, including queued work. Never substitute Future.running() or a detail-record state. |

`series`/`disk_share` keep exact-window semantics. They return only a successfully accepted answer for the caller's exact Window and subject, with its original Window. If that exact answer is pending/missing, request it when admitted and raise HistoryPending; a retained answer for an older endpoint is not an exact answer. A current exact request's failure raises its stored original exception (with existing reason/message), rather than returning old data or looping silently. A successful original local synchronous internal query path may still be used inside the worker; it does not bypass settlement when publishing to the UI router.

The view uses `request_detail`, `part_state`, and `retained`, so an exact getter mismatch cannot remove an eligible retained value. `request_detail` does not require the router's currently preparing Window to equal the applied Window; its admission evidence is the selected applied scope and the successfully admitted ranking/ownership context of that frame. Keep that applied context separately until a replacement applies. This avoids making detail inaccessible whenever a newer preparation is loading.

### 3.2 Scope ownership and retirement

A detail owner is `(app_key, kind, retention_scope, lifetime_id)`. `lifetime_id` is a router-wide increasing positive integer, allocated on adopting a new selected subject/scope/authority or resetting a backward endpoint. It is not the frame generation. Keep a last applied endpoint per live scope and retire before display on any decrease, even if every stored measurement happened to end earlier than the new endpoint.

Authority is exactly one of:

- Remote: `("remote", client.generation, boot_id, iod_epoch)` from a currently verified answering connection. Sequence is excluded.
- Local: `("local", ((name, owner, registration_epoch), ...))` for all registered sources in vector order. Data-version and total-change counters are excluded.

The current authority changes when the selected source mode, remote connection/boot/epoch, or a local registration identity changes. Reconciliation compares the selected tagged authority **and** the registered `(name,owner,registration_epoch)` vector used by local evidence; a registration change retires affected owners even when remote mode's tuple is unchanged. The captured worker registration binding and owner generation/lifetime guards prevent its old local data from publishing. Losing a verified answering remote authority is also a change: remember the old authority before observing local fallback or a replacement connection. Authority loss retires old detail owners and records, even if a lifetime scope tuple itself is unchanged. An ordinary same-authority frame preparation, forward clock endpoint, sequence advancement, local commit, or routine invalidation for a committed local tick does not retire a detail owner. Invalidation still retires the frame and Files deliveries as before.

**Frame authority transition (D01).** On the owner thread, reconcile authority before collecting outcomes in every `drain`/`ready` pass and before source comparison or dispatch. If an admitted, not-yet-applied frame belongs to the lost/replaced authority, enter its administrative retirement once (idempotent per frame owner_id, not once per local/remote token), advance the current frame generation once as the existing invalidation seam does, remove unsent frame queries, mark its issued query and local-copy tokens publication-retired; discard staged frame evidence/certification; set `_ready=False` and `view_state="unavailable: authority changed"` through the existing invalidation/unavailable seam. Retain the actual `_local_future` until completion/drain and retain a sent transport occupant until terminal/lost. If reconciliation/invalidation already retired that owner, do not bump again or replace the unavailable marker merely because another token is observed. Retire Files deliveries through their administrative settlement arm and detail owners through this section. No old ranking result or `ConnectionLost` is needed to finish this transition, and no old completion can certify the new authority.

The shell's next apply/target pass observes that unavailable frame and invokes its existing guarded `_retire_pending(now)` exactly once (§6.1): that transition alone clears pending, sets the deferred target and arms the 30 s floor. The router never writes shell recovery fields, submits a replacement or restarts the floor on repeated observations. A completed/applied frame can stay visible under frozen preservation rules, but its old source cannot certify the new authority. An already retired generation is not retired again. Sequence/data-counter changes are coalesced source changes, never this authority transition.

Retire by removing unsent tokens and deferred successors, marking already-issued tokens publication-retired, clearing records/stamp lookups, and resetting slot observations for the new lifetime. Do not release an occupied transport slot or pretend a running local worker was cancelled. Its eventual completion is drained but cannot publish. A newly accepted measurement supersedes an older record; any older token thereby superseded loses publication rights. Superseded failure clears nothing.

`prepare` must not clear detail maps, live detail tokens, retained records, or stamp lookups merely because frame generation or exact Window changes. Preparation for another desired scope does not itself adopt that scope in the view; the old applied frame stays displayed under the frozen shell rules. `select_detail` at application/subject change owns the displayed-scope reset. Authority changes are applied immediately and need not wait for a new frame.

### 3.3 Frame preparation and lazy local detail

`prepare(window)` preserves its A22 precheck, generation bump, Files retirement, existing failure seam, single `_local_future`, and view-state meanings. Its local worker returns ranking, ownership/materialisation evidence, boundaries, gaps, overlap and retention flags, and file-source availability; it does **not** loop over ranking rows to build series/totals. Remote ranking success does not prefetch the first subject. The view's actual selected subject is the only automatic detail consumer.

`ready(window)` first drains available work, then returns true exactly when the matching **live, nonretired** frame's local ranking/ownership preparation has completed and its own remote ranking has reached a terminal accepted/failure/fallback outcome (or no remote ranking was admitted). Authority retirement is a separate terminal owner outcome: `ready` returns false and the unavailable seam triggers shell retirement/replacement, rather than waiting for a now-ineligible remote completion. A held series, totals, Files page, Files continuation, Files coverage, or publication-retired wire request is not a readiness gate once that frame's ranking requirements are met. Dispatch waiting for a prior transport occupant is allowed; local detail/Files computation cannot delay frame execution capacity.

**Specification decision — bounded independent local lanes (D03).** Use a dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="appio-frame")` only for frame ranking/ownership copies, with `_local_future` still the single frame Future governed by the frozen admission gate. Use a different `ThreadPoolExecutor(max_workers=1, thread_name_prefix="appio-detail-files")` for lazy detail and existing local Files jobs. No shared executor, worker permit, or detail-held router lock may serialize a frame behind detail/Files. Store-local SQLite lock waits keep their existing bounded worker policy; there is no global lock held during backup/query.

A detail worker copies the registered file-backed sources, plans/materialises using the existing ownership rules, resolves the external key independently in each private snapshot, and computes only the requested kind for that subject and exact Window. Do not use a store-local ID from another source or a registry ID as a database key. Preserve existing merge, tier, peak, residual and coverage reducers. Reuse helper decomposition without keeping worker SQLite connections on the Qt thread or sharing them across threads. Each frame/detail copy has its own §4 observation bracket; a later detail copy may measure a later commit and cannot certify the frame.

Capacity is charged from worker submission until its completion is drained, including queued, completed-but-uncollected, and publication-retired jobs. The frame lane holds at most one charged Future, including a retired running frame; drain/reap a completed old frame before replacing `_local_future`, and the unchanged `not done()` gate prevents submission behind a running one. On the background lane at most two detail Futures total (one per live selected kind when capacity permits) and at most eight Files worker Futures may be charged, for a hard maximum of ten. Retired running jobs count against these same totals, not an extra allowance per selection/lifetime. Cancel/remove retired unsent jobs where possible, but release their capacity only when their terminal completion is drained. An admitted current detail with no capacity stays logically pending without a submitted Future; keep at most one current request and one latest desired-window successor per selected kind. Drain schedules it when capacity frees. Files retains its maximum of eight outward jobs and rejects excess with its existing queue-full outcome. Repeated subject/scope changes cannot grow the worker queue without bound. Router close retires owners and shuts down both executors with the existing nonblocking `wait=False, cancel_futures=True` semantics; running completions retain resource accounting until reaped, publish nothing and cannot arm a closed UI.

Callbacks enqueue immutable token-bound outcomes in an owner-drained `queue.SimpleQueue`; they never publish, inspect registered connections, or mutate router maps/widgets. Owner-thread drain performs closing reads and settlement. A real worker-barrier fixture must hold a detail **inside computation**, submit a later frame, and observe that frame's ranking/ownership complete and apply before releasing the detail; a delivery-only barrier is an additional ordering control, not evidence for independent execution capacity. No local job blocks the Qt thread waiting for a worker or database lock.

### 3.4 One settlement function

All callback, `ready`, `drain`, cached-answer, Files-page, and local-result paths enter this writer in `history_router.py`:

```python
@dataclass(frozen=True)
class RequestToken:
    token_id: int
    request: QueryRequest
    origin: Literal["remote", "local"]
    scope: RetentionScope | None
    owner: Literal["frame", "detail", "files"]
    owner_id: int
    slot_id: int | None
    authority: tuple
    requested_source: SourcePosition

@dataclass(frozen=True)
class LocalFrameOutcome:
    worker_id: int
    request: QueryRequest
    ranking: tuple[AppTotal, ...]
    segments: tuple[HistorySegment, ...]
    boundaries: tuple[tuple[str, int, str, str], ...]
    gaps: tuple[tuple[str, int, int], ...]
    known_gaps: tuple[tuple[str, int, int], ...]
    overlaps: tuple[tuple[str, int, int], ...]
    has_local_history: bool
    not_retained: bool
    chronology_unproved: bool
    file_available: bool

@dataclass(frozen=True)
class LocalPartOutcome:
    worker_id: int
    request: QueryRequest
    app_key: str
    value: DiskShare | Series
    data_digest: str

@dataclass(frozen=True)
class LocalFilesOutcome:
    worker_id: int
    request: QueryRequest
    app_key: str
    table: FileTable
    coverage: FileCoverage
    served_gap_count: int | None

LocalOutcome = LocalFrameOutcome | LocalPartOutcome | LocalFilesOutcome

@dataclass(frozen=True)
class RemoteSettlement:
    future: Future[dict]
    response: Mapping[str, object] | None = None
    error: Exception | None = None

@dataclass(frozen=True)
class LocalSettlement:
    worker_id: int
    outcome: LocalOutcome | None = None
    error: Exception | None = None
    local_bracket: LocalBracket | None = None

SettlementInput = RemoteSettlement | LocalSettlement

def _settle(self, token: RequestToken, outcome: SettlementInput | None = None,
            *, retiring: bool = False) -> bool: ...
```

These are private immutable router records with exactly the listed fields. The existing `HistorySegment` remains in this module; no worker record moves it or a Future into history_types. RequestToken.request is an immutable normalized QueryRequest with exact kind, external subject, Window, metric, sort, direction, search, limit, coverage kind and continuation. Remote normalization is the client's §5.2 normalization; local normalization applies the same selector defaults/prechecks without calling the client. A local request is bound to a worker, not a fictitious client Future. `scope` is present for a detail; frame/files use None and their exact request scopes. token_id and owner_id are positive and never reused; frame owner_id is its generation, detail owner_id its lifetime_id, Files owner_id its delivery generation. slot_id is positive only for remote work and None for local work. Authority obeys §3.2; requested_source is captured at owner admission. A frame can own one local and one remote token with the same owner_id. The callback's RemoteSettlement.response/error is captured from that exact Future's terminal result/exception; it cannot substitute a separately supplied mapping/error. Require a done Future, and compare any queued wrapper with that bound outcome before acceptance. query_transport supplies its original answered_source; no wrapper chooses a newer cursor.

Before submitting local work, register an immutable worker binding `(worker_id=token.token_id, token_id, request, authority, captured registrations/paths, opening_vector)` on the owner thread. Only frame/detail bindings need an opening vector; Files retains its existing private-snapshot/served-interval proofs. The submitted worker and its completion must carry that exact worker_id/request. The binding and Future are tracked until drained even after retirement. No worker may consult the current selected key/window to construct its completion. A successful frame/detail completion receives the closing read and LocalBracket on the owner thread before `_settle`; acceptance requires that bracket to be present, its opening equal to the immutable worker binding's opening_vector and its closing equal to that owner-thread read. A closing-read error becomes LocalSettlement.error through the existing read-error seam. Files local_bracket is None. Callback code only wraps/queues returned immutable values/errors. Deep-freeze worker collections before enqueueing; no connection, QueryService, worker router, mutable dict/list or materialised database escapes the worker.

LocalFrameOutcome contains **all** copied routing evidence formerly returned by `_read_local`, without eager parts or database handles. Its request is ranking for the exact copied Window. Validate tuple element/nested contracts, unique ranking external identities, exact bool flags, HistorySegment intervals, nonempty boot identifiers, nonempty half-open gap/overlap intervals, and boundaries `(boot, at_ns, from_owner, to_owner)` with nonnegative at_ns and gui/iod owner labels supported by the adjacent proven segments. Preserve planner boot chronology and within-boot segment/boundary order; known-gap evidence can overlap across sources and gains no new global nonoverlap/sort rule. `known_gaps` carries the worker's known-gap evidence rather than an invented remote envelope. Preserve the existing planner's ordering/reducers; do not sort malformed outcomes into validity. LocalPartOutcome requires totals -> DiskShare or series -> Series, exact external app_key/request binding after private-store key resolution, exact value.window, and `data_digest == local_answer_digest(kind, app_key, window, value)`. Its store/worker numeric ID is remapped only after external validation. LocalFilesOutcome is the immutable content of the existing FileViewResult plus request/key/worker binding; its table/coverage retain exact requested windows, served_interval, coverage, tiers, unknown-owner evidence and served_gap_count (nonnegative int excluding bool, or None only where the existing local contract allows it). The worker must run the existing fatal-vs-skippable candidate/provenance proofs in its private snapshot before emitting success; those are pure acceptance predicates without outward publication. `_settle` revalidates the returned table/coverage/served evidence and owns outward acceptance. It is not a remote FileTable envelope.

The writer runs on the router/client owner thread. Reconcile authority through §3.2 **before** ordinary outcome guards so an obsolete-authority current frame has already retired. The `retiring=True` administrative arm requires outcome=None and explicitly retires that owner: a matching outward Files Future receives the existing `WindowUnavailableError("file_source_unavailable", "File history generation retired.")`; detail retirement clears only that lifetime's records; frame retirement discards its staged evidence and takes the unavailable seam. These actions never release transport/worker capacity early or clear another lifetime's records/diagnostic. All owner-data writes, including administrative frame/Files retirement, go through this boundary; the shell alone handles its own recovery fields. For ordinary outcomes:

1. Ignore closed, already-settled, publication-retired, noncurrent-token, dead-owner, wrong-authority, or retired-scope completions. A frame token must match the live current frame generation; a detail token must match the live selected lifetime independent of frame generation; Files must pass its generation/authority guards. No ignored completion clears a pending marker/record. Reap its actual terminal resources separately.
2. Require exactly one result or Exception and an input variant matching token.origin. For RemoteSettlement, require the exact bound Future, `query_transport(future).request == token.request` and matching connection generation/issue association. A success then passes §5.4's shared request/selector validation and owner predicates. For LocalSettlement, require its tracked worker binding and worker_id/request/origin/owner/authority association; a success must have the appropriate LocalFrame/Part/FilesOutcome for the token owner and validate/normalize as §5.4 specifies. An associated Exception follows its current failure arm without requiring result fields/bracket. Local work never calls query_transport or requires a remote snapshot, echoed kind, or invented envelope. Malformed current inputs fail invalid_result; they are never absent.
3. On successful detail acceptance, remap the validated external subject to the router's positive presentation ID without changing Window or quantities. Build identity/provenance and stable first-seen stamp; replace the retained record; install matching ok PartState; clear only this token's pending marker and supersede only older measurements/tokens in that slot.
4. On current detail failure, clear its record **before notification**, install failed with its original requested Window and exact reason, and clear only its pending marker. Retain the original exception privately for exact getters. A pending retry cannot restore the record.
5. A local frame success stages ranking **and all routing/materialisation evidence** atomically for its frame owner; no `_consume_local` side publication remains. Drain defers a completed remote frame-ranking success's owner-context acceptance until that same frame's local evidence has staged, so default/old local flags cannot prove remote ownership. This completion waits privately without becoming a readiness success or blocking genuine terminal transport reaping. Then apply the mixed-store/overlap ownership predicates to that local evidence; a rejected cached candidate uses §5.3's bound-entry eviction. Once this live frame's local and admitted remote ranking outcomes are terminal, finish/select its ranking and publish FrameProvenance through this writer. A local frame exception/invalid outcome uses the existing unavailable/readiness error seam, never empty ranking. Remote ranking failure uses existing local-fallback/DaemonAnswerError classification. Local Files success runs the same Files predicates on its table/coverage/served evidence, then completes the outward Future with FileViewResult. Remote Files pages accumulate privately only after acceptance; the final checked table/coverage completes that same outward seam. Detail/Files failures do not become frame-wide ranking failure or alter `_not_retained`.
6. Return whether visible state changed. Notify the view on the Qt thread after the complete drain/settlement batch, including with no frame applied. Only then admit a successful detail's single deferred newer-window successor. Failure never retries from its own notification; a later ordinary refresh/explicit action may do so.

Existing `_settle_file` is only the outward-Future completion helper called by this writer after acceptance/failure/administrative retirement, never another acceptance path; `_cancel_file_requests` enters the administrative arm per owner. `_consume_local`, cached/local detail delivery and Files local-worker delivery only construct inputs for this writer. Transport reaping is independent: ignored stale data may release its own slot only after a genuine terminal/lost observation.

Failure reason text is `str(error)` unchanged when nonempty, otherwise the exception class name. Current selector/association violations created by this boundary use `RequestRejected("invalid_result", "invalid_result")`, so the exact slot reason is `invalid_result`. The exact-only adapter has no client import: its internal invalid-provenance/selector failure is `ValueError("invalid_result")`, which yields the same reason through its failed slot. A decoder's existing RemoteError retains its actual machine reason and human detail; it is not rewritten to a coverage reason. WindowUnavailableError keeps its exact refusal text. Catch Exception, not BaseException. Navigation retirement is not a new current failure.

## 4. Local position function and snapshot discipline

### 4.1 Position algorithm; no table hashing

For each registered source, on its existing `QueryService.connection`, require `connection.in_transaction is False`, execute and fully consume:

```python
data_version = connection.execute("PRAGMA main.data_version").fetchone()[0]
total_changes = connection.total_changes
position = LocalPosition(name, owner, registration_epoch,
                         data_version, total_changes)
```

The **table/column hash inventory is empty**. No table or column is hashed for local_position in approved revision 7. In particular, do not hash sampler-checkpoint rows, receipt rows, all table contents, `max(rowid)`, latest sample time, file size, schema version, or a new observer connection's counters. Several tables are WITHOUT ROWID; latest-row scans miss deletion and in-place updates. The exact local position is the five-field tuple above. Payload hashing for accepted local identities (§2.1) is a separate operation.

Order the complete vector by the UTF-8 bytes of registered source names, collecting the owner and registration epoch alongside the two counters. A change confined to an imported/non-primary store changes the vector. Adding/removing a source changes its entries; replacement/reopen changes its epoch even when the two raw counters repeat. Never compare raw SQLite counters across registrations.

Own-connection writes advance total_changes; another connection's commits advance data_version. This detects the existing sampler, gap/overlap, handover/receipt, epoch, rollup and quota paths without changing any writer. Counters are conservative change tokens, not a count of relevant measurements; e.g. a rolled-back write may cause extra work. Equality, not numeric subtraction or ordering across connections, is the comparison. Each source observation costs O(1); sorting a changed registration list costs O(S log S), caching its order is permitted, and reading a vector costs O(S) for S sources.

### 4.2 Registration epoch mutation points

`HistoryRouter` owns `_next_registration_epoch: int`, initially 1, and a registration record per `_sources` entry. Allocate and then increment the counter:

1. When constructing the initial `local` registration.
2. On every successful `add_source` installation, including replacement of the same collector_id/service/owner. Keep add_source's existing invalidation.
3. When a registered QueryService object or its connection object is replaced/reopened, even under the same name, owner and path.
4. When a registration is rebound to another physical database, including a path replacement with a different filesystem `(st_dev, st_ino)` while the source name and owner remain equal.
5. On re-adding a removed source; removing an entry changes the vector immediately and never frees its epoch for reuse.

**Specification decision:** registration bookkeeping remembers service/connection object identity and the canonical main database path plus `(st_dev, st_ino)` obtained at registration. Before source observation/copy admission, compare those identities (one stat per registered file); on change perform a registration replacement before observing counters and copying. If the registered connection still addresses an old unlinked file while the path now denotes another file, refuse the copy as an operational source error until the registration has been rebound to a connection for that file. Never observe the old connection and copy a different file. Do not claim that stat alone proves a new connection is correctly bound. Owners explicitly using add_source/reopen refresh this bookkeeping at that point. There is no new store identity column, writer callback or persistent epoch.

A failed installation/identity check cannot publish a vector for the proposed source. Do not reset an epoch on commit, backup, query, frame generation, worker creation, owner-fact display changes, or an unchanged observation.

### 4.3 Two observations and certification

1. On the owner thread, immediately before submitting a frame copy, verify every registered observer has no active transaction, capture the ordered registration/path list and opening vector, and retain both with the frame token. An in-transaction source is an operational prepare failure with the existing `history source has an uncommitted transaction` text; do not commit or roll back a caller's transaction.
2. The worker opens its own read-only file connections and uses SQLite backup into private databases, as today. Each backup is internally consistent; ownership planning/materialisation and ranking operate on these private copies. Registered live connections never cross to the worker.
3. When the copy/ranking result is consumed, on the owner thread, recheck registration identities, require no active observer transaction, and capture the closing vector **before publication**. No BEGIN, pinned read transaction, or shared read context may enclose either observer pass. Fully finish each PRAGMA cursor. Do not borrow QueryService._snapshot for an observer pass.
4. Preserve LocalBracket(opening, closing). If equal, the local ranking's answered_source is that vector. If unequal because of ordinary data-version/total-change movement under the same registrations, answered_source is None; the live frame may display its own honestly qualified data but cannot advance displayed_source. A registration-set/identity change also makes the bracket unequal **and** takes §3.2's stronger authority-retirement transition before publication; that retired frame cannot apply and its single replacement follows the frozen floor/gate. Retain its bracket only as retirement evidence. Operational observation failure propagates through the existing preparation/readiness error seam rather than certifying a sentinel token.

The closing observer read is the conservative boundary, not the backup's cutoff. A commit during backup and a commit after backup but before the closing read both yield an uncertified frame and one coalesced replacement. A commit after the closing read leaves that frame certified and is detected by the next current-vector comparison. Do not put a second transaction around the opening and closing reads that would conceal the intervening commit. Lazy detail copies use the same bracket for provenance; they do not certify the frame, even with an equal bracket. A registered-source observation error for a current local detail settles that dependency as failed with the exact error reason; only frame preparation/readiness observation errors use the frame-wide error seam. No local result or bracket exception bypasses `_settle`.

## 5. One remote scheduler, transport ownership, and accepted shapes

### 5.1 Queue and scheduler slot

Keep MAX_INFLIGHT == 1 for **wire-active historical requests**. Only `HistoryRouter._dispatch(self) -> None` calls `client.query` for router-owned historical work. Enqueue calls dispatch immediately when free; otherwise the continued poll/drain does so. Neither exact getters, callbacks, Files helpers, nor prepare sends directly.

Queue ordering is `(priority, admission_serial)` with increasing serials and no preemption of an issued request:

| Priority | Work |
| --- | --- |
| 0 | Current frame's ranking |
| 1 | Displayed subject's series/totals, in admission order (the view asks series then totals) |
| 2 | Files first page, continuation and coverage, each separately enqueued when eligible |
| 3 | Any already-supported other historical work, stable in admission order |

Deduplicate identical live detail owners. When an older same-scope detail is outstanding, keep it and one latest desired applied-window successor; do not perpetually supersede it. Drop retired unsent queue entries before selecting a sender. Connection cursor sequence advancement alone neither retires a scope-owned detail nor invalidates its exact Window. Files retains its stricter existing cursor/snapshot/page guards.

A scheduler occupant contains its RequestToken, normalized QueryRequest, bound Future, connection generation and client transport observation. Its lifecycle is total:

| Event | Publication | Transport slot |
| --- | --- | --- |
| Queued, not dispatched | pending for a live detail | Free; no wire deadline yet |
| Immediate cache hit or local client refusal, no wire request | Settle once after association/shape checks | Release immediately; no daemon job exists |
| Request sent | Remains pending | Held |
| Daemon result or correlated error, even one rejected by decode/shape validation | Accept or fail current owner once | Release after terminal envelope is consumed |
| Client five-second expiry while daemon job still exists | Current detail fails, record cleared; frame failure uses its frozen diagnostic/fallback | Mark expired and keep occupied |
| Publication retired while on wire | Ignore eventual data/failure | Keep occupied until terminal outcome, loss or retirement |
| Late result/error for expired request | No second settlement, no resurrection, no late success cache insertion | Consume its correlation tombstone and release |
| Connection loss | Reconcile/retire affected authority owners first (§3.2); old completion cannot publish. Any still-live same-authority error follows its current failure arm once. | Release only after that socket is closed/detached |
| Expired request reaches the retirement deadline without a terminal outcome | No second failure if already expired | Retire that connection before releasing and before dispatching any successor |
| Router closes | Ignore future publication, retire queued work | Normal client/router teardown; no new dispatch |

Cancelling a Future, clearing `_inflight`, superseding a token, a live snapshot, an unrelated status reply, or the Future merely being done is not a terminal daemon outcome.

### 5.2 Exact internal client seam and timeout arithmetic

**Specification decision:** keep `IodClient.query(request) -> Future[dict]` unchanged and add the following in-process observation seam. It is not serialized and does not alter protocol 1.

```python
@dataclass(frozen=True)
class QueryTransportState:
    request: QueryRequest
    connection_generation: int
    request_id: str | None
    phase: Literal["not_sent", "active", "expired", "terminal", "lost"]
    issued_at_ns: int | None
    issued_source: RemotePosition | None
    answered_source: RemotePosition | None

class IodClient:
    def query_transport(self, future: Future[dict]) -> QueryTransportState: ...
    def retire_query_connection(self, future: Future[dict]) -> bool: ...
    def reject_query_result(self, future: Future[dict]) -> None: ...
```

Every Future returned by query, including immediate cache/refusal Futures, is bound to one immutable normalized QueryRequest. Normalization is the existing validated wire-selector/default normalization, reconstructed as a frozen QueryRequest without changing wire defaults or allowed keys. Store the association before settling an immediate Future. `query_transport` returns it without a send/wait and raises ValueError for an unrelated Future. The client may retain these associations weakly by Future; outstanding wire requests/tombstones retain them strongly until reaped.

`connection_generation` is the existing nonnegative client generation at issue/decision. request_id is nonempty for sent requests and None for never-sent/cache outcomes. issued_at_ns is present for sent requests; issued_source is the then-current verified cursor captured once, or None when that connection has no cursor to prove. It is never replaced with the cursor at reply time. answered_source is the original issue source for a successful remote result, including an originally cached one, and None for a failed/no-answer or cursor-less outcome. A router frame selects remote source mode only when that cursor exists; a cursor-less generic client query cannot certify a position. A cache hit has phase terminal, request_id/issued_at_ns None, and retains the cached answer's source; it does not invent a new issue. A local refusal has phase not_sent with no answered source. Active/expired require a sent request; terminal means the corresponding daemon result/error was consumed or a cache hit completed; lost means its carrying connection was closed. All integer/text/source fields validate as above.

At `issued_at_ns + REQUEST_LIFETIME_NS`, inclusive, settle the Future with the existing `RequestRejected("query_timeout", "the daemon did not answer within five seconds")`. REQUEST_LIFETIME_NS stays `5_000_000_000`. Keep a transport tombstone containing request ID, generation, normalized request and deadline; do not evict it because the Future failed. Late correlated envelopes can still mark the request terminal.

The scheduler retires a still-held expired connection at:

```text
retire_at_ns = issued_at_ns + REQUEST_LIFETIME_NS + NO_ANSWER_NS
NO_ANSWER_NS = 5_000_000_000
```

This is five seconds after client expiry, with an inclusive `now >= retire_at_ns` comparison; the existing connection-wide silence check may close it sooner. **Specification decision:** unrelated status/snapshot traffic cannot extend this request-specific retirement deadline. This preserves the five-second Future contract while bounding a held job independently of streamed traffic. No timer is attached to queue admission time.

`retire_query_connection` returns True only when this Future still owns a sent active/expired request on the current connection; it invokes the existing connection-drop path with `daemon_not_answering`, closes/detaches the socket, fails other current work as today, clears that connection's cache authority, and allows the existing backoff/reverification path. A stale-generation, terminal, never-sent or already-lost Future returns False and cannot drop a newer socket. It is not permanent client.close(), a service restart, or an elevation. The scheduler verifies the old transport is lost/terminal before considering the slot free. Record/update transport phase before owner completion callbacks/wakes so reentrant dispatch cannot run behind a still-active job.

### 5.3 Cache and decode metadata

Cache key is exactly the existing canonical complete query JSON paired with the full issue cursor tuple `(boot_id, iod_epoch, seq)` (None when no cursor exists, which cannot certify a position). Keep the existing byte budget, whole-entry LRU eviction, read-through semantics, and failure exclusion. A connection authority change retains its existing cache-drop rules. Each successful cached entry also stores its original normalized request and answered_source. Copy those on a hit. Do not restamp them from current ClientState. At an unchanged cursor, a hit may certify that cursor; at an advanced cursor an older-key hit must be impossible. Still check provenance on settlement, including structural/fault-injected older-cache results.

**Cache admission (D05).** `_apply_result` first verifies live request-id/generation/normalized-request association, decodes and preserves external metadata, constructs the response mapping, and runs `history_validation.validate_query_result(pending.request, response)` **before** `_cache_put` and Future success. The pure validator has signature `validate_query_result(request: QueryRequest, response: Mapping[str, object]) -> None`; malformed shape/selector/request content raises ValueError, translated here to `RequestRejected("invalid_result", "invalid_result")`. Wrong kind, echoed key/Window, incompatible Coverage variant, duplicate ranking identity, malformed nested value or request/decoder association inserts zero entries and fails the Future. Decoder failures retain their existing decode error classification and also insert nothing. Mark the consumed envelope terminal even on validation failure so the scheduler can release its real wire slot.

No expired, cancelled, lost or publication-correlated-to-another-request answer enters cache; a tombstoned late result only closes its transport observation. Revalidate the cached entry's original request and pure selector validity on a hit; a corrupted/misassociated entry is evicted and treated as a miss before returning success. There is no cached invalid_result/error entry. At the same cursor, malformed response -> retry must therefore send a real second query envelope, whose valid result alone can be cached/accepted.

Owner-specific checks (mixed-store ranking authority, Files ranking-snapshot/page-sequence/cursor guards) also run in `_settle`. If a current remote success fails any such acceptance or its bound association, call `client.reject_query_result(future)` before notifying. This seam removes only the cache entry version produced/returned by this exact bound Future; it never evicts a newer replacement under the same key, drops transport, or publishes. Unknown Future raises ValueError; a known uncached/previously evicted outcome is a no-op. Retain an internal entry-version association for sent and hit Futures. Superseded owner delivery alone need not evict an otherwise valid answer for its original exact request/source. Client validation/cache storage never writes router owner data and does not replace the sole settlement writer.

Decoded response dictionaries keep existing `kind`, `result`, `snapshot`, `next_cursor`. Extend the existing external `app_key` metadata to DiskShare and Series, using the decoder's registry mapping after successful decode just as for FileTable/FileCoverage. Do not compare decoder-local numeric IDs with router IDs or repair a contradictory key by replacing its app_id. This metadata is not a new DiskShare/Series field or wire key. Scoped process Coverage has no echoed subject or Window on the wire; its validated immutable QueryRequest association supplies that binding. Do not invent an echo requirement the protocol cannot meet.

### 5.4 Per-selector acceptance table

Remote current answers require a response mapping with `kind == request.kind`, a valid snapshot where the existing route requires one, legal continuation shape and an immutable client association equal to the token's normalized request. The shared pure validator enforces the table's shape, nested-content and exact request-field predicates; the expressly identified ranking-ownership and Files-owner predicates stay contextual even when an individual check needs only the request/response. `_settle` reruns it, then applies the table's owner-context predicates (ranking local ownership; Files ranking snapshot, cursor authority and page history). Revalidate nested contracts, not merely the broad from_wire union. Remote external keys use the existing AppKey validator; ID remapping occurs only after external validation.

Local inputs normalize **without** a remote envelope or snapshot: LocalFrameOutcome.ranking is the ranking selector's row sequence, with its exact worker request/Window, unique AppKeys and routing evidence checked together; empty local ranking is valid, while the nonempty/no-mixed-store restrictions below apply only to remote ownership. LocalPartOutcome.value/app_key supply totals/series result/key, with the exact requested Window, nested/sequence rules below and local digest supplying accepted identity. LocalFilesOutcome.table and coverage supply the combined Files result and its local served-gap evidence; enforce exact request/key/sort/direction/search, matching table/coverage/tiers/unknown-owner/served interval and existing ownership-candidate proofs, then construct FileViewResult. Remote snapshot/continuation/ranking-cursor requirements do not apply to local Files; a local continuation is forbidden. Local frame/detail answered_source comes only from its closing bracket, never from a client observation. All extra frame evidence and Files evidence publish atomically with their validated owner result in `_settle`. Malformed local binding, variant or content fails that owner before any of its evidence publishes.

| Selector | Exact accepted result and association |
| --- | --- |
| `totals` | DiskShare; response.app_key equals request.app_key; value.window equals request.window exactly; nested device/coverage contracts valid; next_cursor is None. Require a valid snapshot for accepted identity. Do not require the snapshot to equal a separately requested ranking snapshot. |
| `series` | Series; same external-key/exact-Window/snapshot rules for remote origin, or §3.4's local binding/digest. Apply the exact chronology predicates below, including device nonoverlap **per dev**. next_cursor is None. Reject partial/paged series; never require its snapshot or tier to equal totals/ranking. |
| `ranking` | A list of AppTotal, every nested contract valid, unique external keys and decoder subject associations, with a valid snapshot. No Window echo is present or required. Keep the existing remote-ownership conditions: nonempty rows, no continuation, no lifetime remote authority, no conflicting local ownership/history contribution or overlap exclusion. Refusing an empty/paginated/mixed-store remote ranking retains locally proved fallback and certifies no daemon position. Do not add ranking pagination. |
| `files`, first page | FileTable for request.app_key; exact requested Window, sort, direction, normalized search; valid snapshot bound to this Files owner's ranking snapshot and existing connection/cursor authority; requested scope is not replaced by served scope. Validate existing nested contracts and page limits. |
| `files`, continuation | Same shape and exact first-page snapshot/subject/Window/sort/direction/search/coverage/unknown-owner and other non-row metadata. `replace(page, rows=first.rows) == first` after subject validation/remapping. Next cursor is None or a nonempty unvisited string with a nonempty page; forward opaque HMAC unchanged. At most 128 pages and 50,000 accumulated rows; excess remains query_too_large. No missing/repeated cursor, skipped page, local continuation, or mixed-snapshot assembly. |
| `coverage`, `coverage_kind="process"`, app_key set | Exactly Coverage, not FileCoverage, UnknownOwnerOverflow or a dict. The correlated normalized request supplies subject and Window; no nonexistent response echo is required. No continuation. |
| `coverage`, `coverage_kind="process"`, no app_key | A mapping of valid unique external AppKeys to Coverage values only; empty mapping is structurally valid. Reject a scalar Coverage and every non-Coverage value. No continuation. JSON duplicate-key rejection remains in the existing decoder. |
| `coverage`, `coverage_kind="file"` | Exactly FileCoverage; response.app_key equals request.app_key, exact requested Window, no continuation; snapshot/cursor/connection/Files-owner guards match the page sequence. Combined table/coverage pass existing `_check_file_result`, including matching coverage, tiers, unknown_owner_overflow and a defined served_interval. Remote served_gap_count remains None. |
| `coverage`, `coverage_kind="unknown_owner"` | Exactly UnknownOwnerOverflow, literal owner_key `overflow_owner_unknown`, valid nested Coverage, no continuation. No application surrogate/subject is invented. Its Window follows the existing file served-interval semantics, not a new exact-request echo requirement. Preserve existing unknown-owner context/scope validation and keep these bytes outside all application totals. |

**Series chronology predicates (D06), for both origins.** `Series.tier == Series.coverage.tier`. raw/rollup_60s/rollup_3600s require bucket_secs 10/60/3600 respectively for every task, device and residual point; totals_only requires all three sequences empty. Validate all nested point fields, including finite optional wall-time values. Do not require Series.tier to equal the independent share/ranking tier. For each task/residual sequence separately, starts are strictly increasing and `previous_start + previous_bucket_secs * 10**9 <= next_start`; repeated or overlapping buckets fail invalid_result. Device sequence ordering is strictly increasing by `(bucket_start_mono_ns, dev.encode("utf-8"))`, since durations are fixed by tier. Keep one last end per raw dev: the next point **of that dev** must start at or after its last end. A repeated `(start, dev)` or same-device overlap fails; simultaneous different devices are valid. Empty device/residual companions remain allowed and are not fabricated. Do not impose a new task-to-device bucket correspondence or equality of independently reported byte sums: validate each sequence's chronology at its stated tier. Never apply a single-device chart's global nonoverlap check to the interleaved full device tuple or sort an invalid tuple into validity. Filtering for a single DeviceLane subsequently yields its ordinary ordered nonoverlapping sequence.

Normative raw positive fixture: task buckets `(0 ns,10 s)` and `(10_000_000_000 ns,10 s)`, with device order `(0,"8:0"),(0,"8:16"),(10_000_000_000,"8:0"),(10_000_000_000,"8:16")`, both devices with nonzero valid integer bytes. It is accepted unchanged. Duplicate `(0,"8:0")`, an added `(5_000_000_000,"8:0")` of duration 10 s, and swapping the first two device entries are separate current invalid_result cases; no owner record publishes and no client cache entry is inserted. These controls distinguish simultaneous devices from overlap and catch both bucket-order and device-order errors.

For unknown-owner context, a bounded served range must have the request's boot and lie within its requested range/since-boot bounds. Existing successful totals-only since-boot/lifetime forms retain the requested Window with no invented endpoints. Lifetime file context follows the existing FileCoverage/FileTable compatibility carrier. The table does not relax any existing file-interval or ownership proof.

A wrong kind, incompatible coverage variant, wrong external key, wrong exact Window where echoed, duplicate ranking identity, malformed nested value, or broken request association is failed invalid_result for a current owner. Existing Files validation refusals retain their established WindowUnavailableError reason/message and outward FileViewResult failure classification. Both paths are the acceptance step of `_settle`, not a second writer. A malformed superseded outcome has no publication effect.

## 6. Source-driven frames, view capability, and continued polling

### 6.1 Requested, answered and displayed source

Add these shell fields without changing the existing recovery fields or their writers:

```python
self._pending_source: SourcePosition | None = None
self._displayed_source: SourcePosition | None = None
self._displayed_certified: bool = False
```

At an accepted `_submit_prepare(wanted, origin, *, now)` call, `history.prepare(wanted)` captures the requested source after its unchanged Window/A22 precheck and before submitting either the local copy or remote ranking. This capture is part of that one gate call; the shell reads the exact stored value through `history.preparation_source(wanted)` after prepare succeeds and records it as `_pending_source`. Do not independently recapture the cursor/vector in the shell and router or require an unspecified setter. A refused preparation reaches its original precheck before any source observation; a rejected gate/failure never advances displayed_source. source_position selects remote only for a verified answering connection with a valid cursor; otherwise it observes the local vector. For local mode this capture is the opening observer vector in §4.3, not a third observation. The live boot for a lifetime request lives in remote/frame provenance, not in Window.

When `_apply_frame` obtains FrameProvenance for its ready Window, require its generation to equal the router preparation being applied and install `_displayed` through its existing single path. If certified and requested_source equals that frame's captured pending source, set `_displayed_source` to requested_source and `_displayed_certified=True`. Otherwise set `_displayed_certified=False` and leave the last proved displayed_source unchanged; that old token cannot certify the newly applied frame. Scope reset/no-target construction clears displayed_source and its flag. Clear pending_source on the same acceptance/retirement/application transitions that own `_pending`; no callback promotes it separately. Detail answers never write either field.

Replace only the endpoint-driven part of the preparation predicate. Subject-independent frame retention scope uses the same Window tags/boot/range bounds as §2.1 without app_key. Through the unchanged recovery submission gate, preparation is due when any is true:

1. No applied frame or desired frame retention scope differs from the applied scope.
2. The applied frame is uncertified, or its proved displayed source differs from the current source selected by source_position.
3. A verified answering daemon has a current full cursor different from the displayed remote source, or local mode has a current vector different from the displayed local vector (the mode-specific forms of rule 2).
4. A frozen retirement/retry, operational clock retry, or explicit deferred request requires preparation.

An otherwise unchanged certified since-boot source with only a later clock endpoint causes no preparation. Do not mark the unqueried larger Window as applied. A different fixed range is a different scope and always requires its own query. Explicit requests may requery the identical Window/source. Ordinary same-authority sequence/counter observations while a nonretired preparation is pending coalesce; after it applies, prepare the latest due source at most once through the existing gate. An unequal local bracket makes exactly one such replacement due, not one per table/commit. **Authority loss/change is the §3.2 retirement transition instead of coalescing**: before ignoring old completions, mark the current affected generation unavailable. The shell then runs its unchanged guarded retirement once, preserves the last displayed frame, clears pending_source together with pending, and sets the existing floor. Reverification of the same boot/epoch with a new client generation requires this transition even if the frozen coordinator history key is unchanged. Replacement waits for the floor and actual frame Future to be free; it never certifies the old remote position.

The recovery `_refresh_after_apply` flag still has its sole set site after a committed local tick and its sole consume/invalidate site at the end of `_apply_frame`. Source observations do not acquire another writer for that flag. Same-authority sequence/counter changes while pending coalesce on the next target pass without retiring frame/detail or arming a floor; §3.2's authority transition instead uses the existing retirement owner. Explicit deferred scopes, pending retirement, stalled worker gates, retry deadlines, facts promotion and probe reconciliation keep their frozen ownership.

### 6.2 Structural view interfaces and fallback

The required UI-local QueryService protocol keeps exactly its existing ranking/series/disk_share methods. Add a separate optional protocol in `main_window.py`:

```python
class RetainedQueryService(Protocol):
    def select_detail(self, app_key: str | None, window: Window | None) -> None: ...
    def request_detail(self, kind: PartKind, app_id: int, window: Window) -> PartState: ...
    def part_state(self, kind: PartKind, app_key: str,
                   scope: RetentionScope) -> PartState: ...
    def retained(self, kind: PartKind, app_key: str, scope: RetentionScope,
                 applied_endpoint: int | None) -> RetainedRecord | None: ...
    def ranking_provenance(self, app_key: str, window: Window) -> AnswerProvenance | None: ...
```

**Specification decision:** detect this capability once by requiring every listed method to be callable. Do not use `isinstance(service, HistoryRouter)`, import the router, or force exact-only services to implement new methods. A partially supplied capability uses the exact-only adapter; a supplied method raising an error is a real method failure, not a reason to switch silently to fallback.

`_ExactDetailAdapter` is private to `main_window.py`, constructed as `_ExactDetailAdapter(service: QueryService, *, clock_ns: Callable[[], int], authority_epoch: int)`, where authority_epoch is a positive UI-owned registration counter. Its RetainedQueryService methods implement the same selection, slot, record, failure clearing and endpoint rules using exact queries; HistoryPending becomes pending, each other Exception becomes failed, and each successful exact result is validated before acceptance. A pending exact request retains its original Window across forward frames: at most once per ordinary refresh, observe that same getter until it settles; remember only one latest applied-window successor. Do not abandon A merely because B applied. Query-free notifications never call getter observations. Without trustworthy accepted identity, retain measured_window but identity/first_seen/source as None and show age unknown; record retention/failure/reset rules are otherwise identical. The adapter imports no concrete router/store/producer/client.

**Optional exact provenance (D09).** An exact-only service may additionally provide this callable structural method:

```python
class ExactAnswerIdentityService(Protocol):
    def exact_answer_identity(self, kind: AnswerKind, app_key: str,
                              window: Window) -> AcceptedIdentity | None: ...

class _ExactDetailAdapter:
    def capture_ranking(self, rows: Sequence[AppTotal], window: Window) -> None: ...
    def reset_authority(self, authority_epoch: int) -> None: ...

class AppIoWindow:
    def reset_query_authority(self) -> None: ...
```

Detect only this callable optional method; plain three-method services remain valid. Immediately after a successful exact totals/series getter, before another getter or adoption, ask once for the same kind/external key/exact Window. For ranking, the view calls adapter.capture_ranking(rows, window) immediately after that successful applied ranking call and before any detail query; this validates row identities, captures per-row optional identities/stamps and supplies ranking_provenance for that applied frame without another ranking call. The capability must return the identity of that successful call, or None; validate its type, digest and exact kind/key/Window, never infer one from numeric IDs/rounded text. A malformed/raising capability makes the current dependency failed invalid_result (ranking uses its normal error path); it is not silently downgraded to identity-less. Returned None is valid and unknown. The adapter owns the first-seen lookup/clock (§2.4), reusing the stamp of a supplied identity across reacceptance; the capability supplies no guessed source certificate. All fallback answered_source/local_bracket fields remain None.

`AppIoWindow` starts its `_exact_authority_epoch` at 1 and passes it to the adapter. `reset_query_authority()` synchronously increments it and calls reset_authority(new_epoch) **before** notification/adoption/getter observation. The adapter requires a strictly greater positive epoch, retires pending observations/successors/selected lifetime, clears records and stamp lookups, and rejects queued old-lifetime notifications; the next select_detail establishes a fresh lifetime even for an identical lifetime Window. This method issues no query and changes no shell recovery field. The embedded owner invokes it on source-mode, remote connection/boot/epoch, registered service/connection/reopen/rebinding changes using §3.2's authority reconciliation; repeated same-authority sequence/counter changes do not invoke it. A standalone exact-only service's owner calls it when replacing/reopening/rebinding that service or when it learns of such a source change. The frozen three return types and identity method do not expose hidden connection/registration changes, so the adapter cannot detect an unreported change from an unchanged Window or digest. Owners reporting reset must ensure subsequent exact calls address the current authority; the adapter never resumes an old getter observation after reset. No change to concrete QueryService is required.

For a ranked selected subject, `_load_selected` adopts its applied scope, admits series then totals at most once per refresh, reads each PartState independently, and composes from retained(kind, key, scope, **applied_endpoint**). The map is totals -> share, series -> series. A successful older-window record remains effective while a successor is pending. A failed slot never supplies an effective record. Do not catch HistoryPending as series_error/share_error. Pass a failed dependency's exact reason to the builder, retain independent successful inputs, and keep PEAK from the current frame's ranking row only.

A subject change calls select_detail before rendering the new subject. Every path that clears the shared subject, including ranking-unavailable and empty-union rendering, calls select_detail(None, None) so old detail cannot remain effective without an eligible subject. A file-only subject retires old detail, queries no process detail, clears ranking highlight as before, and preserves `not in the process ranking`. Ranking failure/empty rendering supplies no stale ranking PEAK. Direct A22 refusal clears adapter/router selection with both arguments None as part of presentation reset; it still follows the frozen refusal and Files-owner rules.

### 6.3 Detail notifications, poll and apply interface

Add `AppIoWindow.refresh_measurements(self) -> None`: reread slot/retained/provenance observations for its selected **applied** Window, rebuild/diff Overview only, and issue no ranking, detail admission, Files, prepare, probe, or setup query. It is the notification target for a settled batch. Failures are therefore visible before any later refresh admits a retry. `_apply_overview` retains widget identity and equality diffing; task/device data changes use append methods with §8.2 identities. Donut, bars and KPI text keep existing value diffing.

Give `apply_ready_frame` an explicit call context while preserving ordinary callers:

```python
def apply_ready_frame(self, *, now: int | None = None,
                      context: Literal["ordinary", "poll"] = "ordinary") -> bool: ...
```

Its order is exact. (1) Return false if closed; otherwise capture one now, call history.drain, deliver its query-free measurement notification, and update status/age even on early-return paths. (2) If the existing pending frame became unavailable, run `_retire_pending(now)` once, compose again and return false; never submit inline from retirement. (3) If a still-owned explicit PrepareRetry exists, return false without retrying or consuming it. (4) With no pending frame, an **ordinary** call with `_deferred_target` may invoke the established `_tick_target(now=now)` once, preserving its target calculation, origin and full submission gate. If it submits, continue to readiness/application in this same call. No-deferred ordinary calls and **all poll calls** with no pending frame return false after drain/notification; they compute no target and submit nothing, even with a deferred target. (5) For an existing/newly gated live pending frame, evaluate the frozen elapsed budget and readiness, apply it through `_apply_frame` at most once, and compose status/age. If readiness discovers retirement, use step 2. Return true only for an actual frame application.

Thus `_pending=None, _deferred_target=B` with a free gate produces exactly one prepare on ordinary apply and zero on poll apply. With a held frame Future or future retry floor both produce zero; ordinary calls preserve the deferred request and poll calls do not even consult a submission target. A still-owned explicit PrepareRetry produces zero in both contexts. None of these paths writes/clears recovery ownership beyond the existing gate, retirement and application transitions.

`_poll_history` retains its transport-only role: pump the nonblocking client in the existing Files transient scope, call `apply_ready_frame(now=now, context="poll")`, compose status/age, and stop only when `_pending is None` and history.pending_work is false. A deferred target alone does not arm a pump or authorise a poll submission; the next ordinary target/apply/explicit gate handles it. A held expired slot, local detail, page continuation, or ready-but-undelivered completion keeps polling armed. Add `AppIoWindow.measurement_work_requested = pyqtSignal()` and emit it after a selection/ordinary refresh admits pending detail, without another query. Connect it once to CollectorWindow._arm_poll; also call _arm_poll from the existing Files state-change handling when a request starts. Thus subject/Files admission after application arms the same 100 ms poll immediately. No new busy-loop or per-detail timer is introduced. The normal 1 s refresh updates source/age; a later ordinary view refresh, frame application or explicit action may admit an eligible detail retry, never the query-free notification itself. Notification does not wait for that tick. No code here sleeps, waits on a Future, or processes Qt events synchronously.

## 7. Render-model amendment and availability

### 7.1 Exact additional fields and builder arguments

Keep every existing OverviewModel field, in order, including recovery's defaulted coverage_diagnostic. Append exactly:

```python
ranking_provenance: AnswerProvenance | None = None
series_provenance: AnswerProvenance | None = None
share_provenance: AnswerProvenance | None = None
device_lanes_unavailable: str | None = None
requested_window: Window | None = None
app_key: str | None = None
series_metadata: SeriesMetadata | None = None
```

No other frozen render dataclass changes. Extend build_overview with the following keyword-only arguments after coverage_diagnostic; its existing positional and error arguments remain:

```python
def build_overview(
    app_id: int, window: Window, total: AppTotal | None,
    series: Series | None, share: DiskShare | None, *,
    ranking_error: str | None = None, series_error: str | None = None,
    share_error: str | None = None, coverage_diagnostic: str | None = None,
    ranking_provenance: AnswerProvenance | None = None,
    series_provenance: AnswerProvenance | None = None,
    share_provenance: AnswerProvenance | None = None,
) -> OverviewModel: ...
```

The renderer stays pure. Successful input/provenance pairs must match kind, app key, positive app_id association and exact measured Window. A total's external key is the selected external identity; supplied series/share provenance must agree with it. Successful series/share need the requested retention scope and measured endpoint <= requested endpoint, **not** equality with the requested Window. The supplied provenance.measured_window must still equal that value's own exact Window. Range retention requires the same complete range tuple. Lifetime has no endpoint comparison. A newer-endpoint part, wrong subject/boot/start/range/scope, incompatible provenance, or value supplied with its own error raises ValueError; it is never rewritten.

With no provenance supplied, an old structural caller may supply an exact-window successful value as before; its measured Window is known but its accepted identity/stamp are unknown. The builder emits identity-less provenance where subject binding is available, using the current total's app_key and matching exact Window. It must not accept a nonexact value without explicit provenance, because that would silently weaken old callers' validation. An absent input has no provenance; no provenance alone can create a value.

`ranking_provenance` always describes the current **applied** frame's ranking row and exact requested Window. Capture it with the ranking on application; detail-only notification keeps that captured row/provenance. It cannot borrow a newer preparing/completed generation or carry an older row into a new applied frame. Optional text follows UTF-8/nonempty validation including device_lanes_unavailable.

**Model metadata (D04).** Every builder outcome sets requested_window to its window argument (the view's applied frame Window, never a pending preparation); a ranked subject also sets app_key to total.app_key. Every successful effective Series, including available-empty/lifetime, produces SeriesMetadata from its own app_id, selected external key, exact measured Window, tier and Coverage. Missing/failed series has metadata/provenance None. A successful share is still stored in full. The builder derives this metadata from validated input values rather than adding caller-supplied metadata arguments. Early unavailable/file-only outcomes can lack app_key and contain no accepted dependent data/provenance. No label is a metadata source.

Direct construction has two explicit modes. **Legacy direct mode** means all three provenances, requested_window, app_key and series_metadata are None. Preserve all frozen direct-constructor checks on represented points/share/lanes/KPIs/text and defaults, but do not claim to verify facts absent from that shape (series tier/coverage, external identity or requested endpoint), known age, or append identity. `_apply_timeline` uses the full setter with identity None for such a model. **Extended direct mode** (any of those fields supplied) requires a valid requested_window; whenever successful data/provenance is present it also requires app_key, and any successful series projection requires SeriesMetadata. Check positive model/metadata/share IDs, identical external keys across metadata/provenance, metadata measured Window/tier/coverage and the projected points' tier durations/chronology, provenance-to-dependency Window, same requested retention scope, and `measured_end <= requested_end` for every bounded detail, even when ranking_provenance is None. Extended successful chronology (`series_unavailable is None`, including available-empty) requires SeriesMetadata; a missing share or series cannot carry that dependency's provenance. Ranking provenance, if supplied, is exactly at requested_window. Identity-less provenance does not waive association checks. Successful series with unsupported lifetime chronology may retain metadata/provenance, but has no plotted points/lanes.

Any extended model with device_lanes must prove both coverage.device_state values available using series_metadata and share, share.device_total_bytes defined, supported same-boot chronology and equal exact measured endpoints. Direct unavailable series coverage with populated lanes raises ValueError; future-endpoint detail provenance/metadata with no ranking provenance raises ValueError. Both are construction errors, not silently removed lanes. For normal ranked models, extended device_lanes_unavailable must agree with the same prerequisite/reason order as the builder and requires zero lanes/more_devices when present. Frozen ranking-error/file-only early branches keep their fixed text, no accepted detail and a None lane reason rather than adding a clause to those branches. Existing lane uniqueness/order/cap, KPI order and unavailable/empty invariants remain. Optional legacy metadata absence grants no inferred certificate.

### 7.2 Effective-input and availability table

The view resolves effective inputs first. For each detail dependency, use its current accepted value or eligible retained record while pending; failed and absent supply no value. A retained accessor is the authority for effectiveness, not an exact getter. Incomplete Coverage alone does not turn a successful measured task value into an error.

| Output | Required effective inputs; exact consequences |
| --- | --- |
| TOTAL, READ, WRITE | Share; format task_total_bytes, task_read_bytes, task_write_bytes exactly with format_bytes. All three accept measured zero. Missing/failed share gives `unavailable`, never ranking totals substituted for share. |
| Donut and task gutter totals | Share's task read/write, independent of series and device availability. A failed share clears them. Zero/zero uses the frozen measured-empty donut text. |
| PEAK | Current frame's AppTotal.peak_total_bps; None -> unavailable, 0 -> measured zero using format_peak. Never retain a previous frame's ranking value. |
| DEVICE and attribution bars | Share with coverage.device_state exactly `available` and device_total_bytes not None. Series failure/pending/device-incomplete does not withhold these. |
| Residual strip/reconciliation annotation | The preceding share device predicate plus residual_total not None. A missing residual alone withholds reconciliation, not DEVICE/bars. Signed residual arithmetic/text remain unchanged. |
| Task lane | Effective series and supported single-boot chronology. A series error does not suppress share KPIs/donut. Empty successful series is available-empty. |
| Device lanes | Share **and** series; both device_state available; share.device_total_bytes defined; their exact measured endpoints equal; both belong to the same valid single-boot scope/chronology. Equal full ranges already have equal endpoints. Lifetime is unavailable regardless of absent endpoints. |

Do not infer device availability from nonempty device rows, zero gap count, series availability alone or retained intervals only. A newly accepted share with device_state incomplete/other nonavailable or an undefined device total immediately removes DEVICE, residual, bars and lanes in the same model application while updating task totals/donut. A newly accepted device-incomplete series affects only the device lanes. An older retained share plus newer series and the reverse are valid independent effective inputs; their task lane and share outputs display their own measurements and their device lanes remain withheld until endpoints agree.

### 7.3 Composition and exact disclosures

Preserve frozen early branches: ranking error makes the Overview unavailable with its reason and no accepted detail; no current total for a file-only subject gives `not in the process ranking`. Preserve the exact five KPI order, `unavailable`, `series unavailable`, `task/device split unavailable`, `lifetime chronology unavailable`, `no samples in this window`, byte/peak/residual formatters, residual equation, and the independent coverage diagnostic composition.

Device lanes select only positive active share rows for this single boot, associate same-device Series.device_points, order by descending share read+write bytes then raw UTF-8 token, show at most three and `and <n> more` for the remainder. Their gutters are the share row's exact read/write totals, never sums of partial samples. An eligible device with no retained points remains a caption with available-empty geometry. For lifetime, time lanes are unavailable; valid share totals, donut and boot-qualified bars remain. Bars retain all share rows and the existing lifetime ` [<boot_id>]` suffix. Unknown-owner/file bytes never enter these quantities.

**Specification decision — device-lane reason order.** Set device_lanes_unavailable to the first applicable reason below, and no lanes/more count when a reason exists:

1. Task chronology/series unavailable reason, including a real series failure or lifetime chronology.
2. Missing/failed share's share_unavailable reason.
3. Share device coverage reason (`coverage.unavailable_reason` if present, otherwise device_state), or `device total unavailable` when attribution is available but device_total_bytes is None.
4. Series device coverage reason by the same coverage-reason/state rule.
5. `device measurements have different endpoints` when both inputs are otherwise eligible but measured endpoints differ.

The exact clause is appended to detail_line as `; device lanes: <reason>` and passed to any device-lane unavailable rendering. This does not replace share/device-strip explanations or withhold independent values. When eligible, device_lanes_unavailable is None. An unavailable lane stack has zero lanes and more_devices == 0.

Coverage-line source preference remains successful effective share, otherwise series, otherwise current ranking coverage. Preserve all quantities and diagnostic suffixes independently; for any other successful source with different Coverage, keep the frozen `; <source> coverage: <complete coverage text>` detail clause. Do not merge their counts or reuse a data reason as a client reason.

**Specification decision — measured-window disclosure.** After the existing normal `window: <kind>; tier: <tier>` and its existing series/share/differing-coverage clauses, append, in fixed order share then series, `; <dependency> measured: <scope>` for every effective detail whose measured Window differs from the requested Window. `<scope>` is exactly `since_boot <boot_id> through <end_mono_ns> ns`, `range <boot_id> [<start_mono_ns>, <end_mono_ns>) ns`, or `lifetime` as applicable, using decimal integers and raw boot identity. Omit an equality clause rather than claiming a new measurement. Append the device-lane clause after these. This is provenance disclosure, not a new query/range feature. Refusal, file-only and empty-ranking strings retain their frozen early-branch text.

## 8. Append chart interface, state and geometry

### 8.1 Exact chart interface

Keep ChartColumn and ChartSegment fields/validation exactly as frozen. Add:

```python
@dataclass(frozen=True)
class ChartCurve:
    side: Literal["read", "write"]
    run_index: int
    vertices: tuple[tuple[float, float], ...]

class SeriesChart(QWidget):
    def set_task_points(self, points: Sequence[SeriesPoint], *,
                        identity: object = None) -> None: ...
    def append_task_points(self, points: Sequence[SeriesPoint], *,
                           identity: object) -> None: ...
    def curves(self) -> tuple[ChartCurve, ...]: ...
    def columns(self) -> tuple[ChartColumn, ...]: ...
    def segments(self) -> tuple[ChartSegment, ...]: ...
    def set_unavailable(self, reason: str) -> None: ...

class DeviceLane(DualSeriesChart):
    def set_device_points(self, points: Sequence[DeviceSeriesPoint], *,
                          identity: object = None) -> None: ...
    def append_device_points(self, points: Sequence[DeviceSeriesPoint], *,
                             identity: object) -> None: ...
```

ChartCurve is frozen, with exactly these fields; side/read-write, nonnegative nonboolean run index and at least two finite float coordinate pairs are required. Vertices are time-ordered stroke coordinates, not a closed polygon; nondecreasing X is required. Repeated X is valid for subpixel runs. `curves` returns precisely the polylines used by the next paint, without mutation or I/O. `segments` continues to return closed fill outlines satisfying ChartSegment, one for each emitted curve, not a historical step reconstruction. No smoothing spline or interpolation may overshoot these polylines or bridge their supports.

Full setters always validate, replace input/identity, clear reason and rebuild once, even for previously unavailable or identical input. They remain explicit rebuild/resize entries. Append methods receive the **complete newest sequence**, not merely a delta, and apply §8.3. All calls validate the frozen point type/order/nonoverlap/bytes/peaks and copy the accepted raw sequence to immutable retained input. Device sequences contain one raw device token only and acquire no task suspension metadata. Preserve every source field in sequence equality: None vs 0, peak_total_bps, wall_start_seconds and spans_suspend differences cannot be ignored just because a geometry projection omits them.

### 8.2 Caller identity and retained state

**Specification decision — UI identity tokens.** The view passes these immutable tuples:

```text
task:   ("task", external_app_key, retention_scope, measured_start, series.tier)
device: ("device", external_app_key, retention_scope, measured_start, series.tier, dev)
```

measured_start is `0` for since_boot and the exact range start for range. `_apply_timeline(model, previous)` obtains external key and requested scope from model.app_key/requested_window and measured_start/tier from model.series_metadata.measured_window/tier, after §7.1 association checks. It does not use share.coverage.tier, parse coverage/detail text, or reread a query service. The metadata and provenance measured Windows agree; an identity-less builder result still has typed metadata for an append identity. A legacy direct model lacking metadata uses the full setter with identity None. A view fixture with a since_boot window through 180_000_000_000 ns, share.coverage.tier raw and a same-endpoint Series with tier/coverage rollup_60s and 60-second buckets must send rollup_60s in both task and device identities. Lifetime chronology is withheld, so no lifetime plotting identity is invented. Do not include the advancing measured end, snapshot digest, first-seen stamp, frame generation or cursor sequence: they would force a rebuild every update. A device widget reused for another token receives a different identity. The chart treats identity as opaque equality-comparable caller data; it does not parse these tuples. Callers must not mutate a token after passing it. The optional full-setter identity defaults to None and establishes that exact identity for subsequent appends.

Retain exactly the following conceptual state; private container names are implementation details:

| State | Required contents |
| --- | --- |
| Raw accepted input | Full immutable contract-point sequence, mode (task/device), opaque identity, availability and optional unavailable reason. |
| Grouping | Current integer width budget W, smallest power-of-two group size g, consecutive group member bounds `(first_index, last_index)` inclusive, anchored at raw index 0. No per-column list of every member index. |
| Aggregate cache | ChartColumn per group plus sum of **observed durations** in ns per group; raw integer bytes, optional peaks, envelope and OR run flag. |
| Run cache | Source-run boundaries and each column's canonical `(run_index, start_ns, end_ns)` interval list in temporal order. Retain sufficient local run-end metadata to update the final group without rescanning the prefix. |
| Paint cache | Exact per-side maximum rate pairs, curve tuples and corresponding fill segment tuples for the current plot rectangle. |

`set_unavailable(reason)` validates before mutation, then clears raw sequence, identity, group bounds, duration/aggregate/run caches, curves, fills and numeric gutters as frozen; records the exact reason and repaints once. No append state can revive it without a valid new input. A successful empty append clears every data/geometry/group cache and reason, establishes available-empty and the supplied identity. Constructor remains available-empty with identity None.

### 8.3 Total append decision table

Validate the whole incoming sequence first, without changing retained state. Then evaluate these rows top-down, exactly. Let `old` and `new` be full raw contract sequences, `n=len(old)`, `m=len(new)`, and equality be full contract equality; all predicates after row 1 assume validation succeeded.

| Row | Exact predicate | Action |
| --- | --- | --- |
| 1 | Any point/type/order/overlap/numeric validation fails | Raise ValueError; every retained state and repaint count unchanged. |
| 2 | Retained availability is false | Full rebuild from new, including new == () or new equal to a pre-unavailable historical sequence; store identity, clear reason. |
| 3 | Incoming identity != retained identity | Full rebuild; store identity. |
| 4 | `m == 0` | Become available-empty. If already available-empty, same identity, and no reason, no-op/no repaint. |
| 5 | `n == 0 and m > 0` | Append all points; no old tail exists. |
| 6 | `new == old and available and reason is None` | No-op; no aggregation, geometry rebuild or repaint. |
| 7 | `m == n and n > 0 and new[:-1] == old[:-1] and new[-1] != old[-1]` | Replace the old tail, rebuilding its one group from member bounds. |
| 8 | `m > n and new[:n] == old` | Append only. |
| 9 | `m > n and n > 0 and new[:n-1] == old[:-1] and new[n-1] != old[-1]` | Replace old tail group, then append the suffix beginning at index n. |
| 10 | `m < n` | Full rebuild. |
| 11 | Every remaining valid case | Full rebuild, including interior/prefix contradiction at equal or increased length. |

Rows 7/8/9 are disjoint. Row 10 excludes empty new by earlier row 4; its test uses a nonempty shortening. State invariants preclude available-with-reason, so there is no hidden additional branch. A same-data full setter followed by append uses the setter's stored identity and cached grouping; it need not rebuild again. No tolerance, rounded text, rate-only or digest-only equality replaces exact prefix comparison.

### 8.4 Group updates, promotion and scale

For positive plot area let `W = max(0, floor(right-left))` as frozen; exclude captions/gutter/margins. For n > 0, g is the smallest power of two such that `ceil(n/g) <= W`; use g=1 for empty state. Group j holds indices `j*g .. min((j+1)*g, n)-1`. Grouping is by consecutive buckets anchored at the retained sequence start, **not** moving time-pixel bin assignment. Forward since-boot appends do not move the grouping origin.

Append to the open group until full, then open the next group. If the bound would exceed W, double g and merge adjacent groups pairwise, leaving a final unpaired group if necessary; repeat if a multi-point append crosses multiple thresholds. Bytes and observed durations add, defined peaks take max (all None remains None), starts_run ORs, envelope takes min start/max end, and bounds concatenate using first/last indices. Merge canonical intervals by §8.5. Copy O(columns) bounds, not O(n) member-index arrays. Run IDs remain the original temporal run order; do not renumber the whole prefix for an ordinary append.

A tail replacement revalidates its interval relative to the preceding point and rebuilds **that group** from its at most g members, including bytes, durations, peaks, starts_run and canonical intervals. It must remove a lowered/removed sole peak and recompute a changed tail suspension boundary before a later append decides the next run. Never subtract a previous maximum heuristically or treat None as zero.

For each nonempty group, read/write rates are exact `bytes / observed_duration_ns` ratios. Duration is the sum of bucket_secs*10**9, not envelope width across gaps. Recompute each side's exact maximum from cached groups on every changed input/promotion. Compare ratios by integer cross multiplication; convert to finite floats only for final pixel mapping. No hysteresis or lingering old maximum is permitted. Map the largest nonzero read rate to the upper half-height and the largest nonzero write rate to the lower half-height, subject only to the existing half-pen inset; a zero side has zero height and no division. Column bytes and optional peak maxima stay exact independently of plotted average amplitudes.

At W==0 or zero plot height, retain validated inputs/identity/availability but expose no columns/curves/fills until resized. The collapsed path may rebuild from raw data on a later positive resize. A width change, shrink, identity change, contradiction or explicit setter is a declared full rebuild; it does not have to retain the old g.

### 8.5 Canonical run intervals

A task run begins at the first point, after nonadjacency, or immediately after a point with spans_suspend True. Keep the suspended point in the preceding run, including its bytes and peaks. A device run begins only at the first point or nonadjacency. Coverage gap counts never locate a hole.

For each column retain the temporal list of observed intervals clipped to that column's member support and each contributing run's own `[start,end)`. Coalesce touching intervals **only when run_index is equal**. There are no zero-width intervals, overlaps, holes filled by an envelope, or merged distinct run IDs. Adjacent task runs split by suspension stay separate even when their endpoints touch.

Pairwise group promotion concatenates lists in time order and coalesces only same-run intervals touching at the seam. Cross-column fragments stay distinct vertices, because neighbouring column heights may differ. A dense contiguous series has exactly one canonical interval per column, not one retained interval per source point. A fragmented series may have far more intervals/curves than W; never remove a break to fit the column budget.

### 8.6 Monotone inset curve and closed fill construction

Map time linearly from first raw start through last raw end into plot left..right. Use the column's scaled rate height for every one of its run fragments. For each run and side:

1. Let `[s,e]` be that run's mapped observed outer support and `inset=min(0.25,(e-s)/4)`. Set `L=s+inset`, `U=e-inset`.
2. For every canonical cross-column fragment of the run, clip its support to both the column and run, and take the midpoint of that **fragment interval** as candidate x. Never take the midpoint of the column envelope. Its candidate y is the column's height for this side.
3. If the entire run has one fragment, emit two vertices `(L,y),(U,y)`; this makes a single-fragment run a visible horizontal tick when nonzero.
4. Otherwise order candidates temporally/by x. Raise the first to at least L and cap at U. Lower the last to at most U. In a forward pass set each subsequent x to `max(previous_x, min(candidate_x,U))`, using the already-lowered last candidate. This is the monotone clamp of the design. Keep every y unchanged. Vertices may share x for tiny runs; do not sort heights or drop the maximum to repair geometry.
5. If all heights for this run/side are zero, emit no curve/segment for it. Otherwise retain one ChartCurve through its final vertices. Fill outline is exactly `((first_x,centre), *vertices, (last_x,centre), (first_x,centre))`; ChartSegment remains a closed polygon with at least four coordinates. Baseline closure is excluded from forward-X monotonicity checks.

Paint the cached closed fills and their cached polyline strokes, read above/write below, with the frozen palette/layout. The entire min/max X-support of each curve and fill is strictly inside that run's outer support at holes. No stroke, fill edge, spline or baseline connector crosses between runs. Repeated X for an eight-fragment subpixel run is legal; negative/backward X is not. Candidate clipping plus per-run construction, not the column envelope or starts_run flag alone, proves this exclusion. `paintEvent` never scans source samples or creates per-source-point drawing objects.

### 8.7 Cost accounting

For one ordinary append/tail replacement let C be emitted column count (C<=W), g current group capacity, F emitted run-fragment/vertex complexity and E total canonical per-column intervals processed, including intervals merged on promotion:

| Work | Bound and exclusion |
| --- | --- |
| Whole-sequence input snapshot, validation and exact prefix/difference decision | O(n) admitted separately by design AC-5. This may inspect raw fields but must not reaggregate all historical samples, rebuild their run membership, or recreate per-sample geometry. |
| Changed/open group, including tail replacement | O(g) for the affected group's members; a new one-point append is no worse. |
| Group-promotion bounds/aggregate metadata | O(C) per promotion; interval merging is counted in E. No O(n) copied membership lists. |
| Recompute exact scales | O(C), using aggregate/duration cache only. |
| Rebuild size-dependent curves/fills from interval cache | O(C+F+E); F and E are not assumed <=W. |
| Multi-point append | Apply the same bounds across touched groups/promotions; charge each appended bucket and actual promotion/interval processing, not an asserted O(1) batch. |
| Full setter, identity reset, contradiction, shortening, resize/collapsed recovery | Full raw rebuild exempt from append cost bound; still conserve bytes and obey geometry. |
| Paint | Cached geometry/labels only, proportional to actually drawn curves/fills/columns, with no historical-sample aggregation. |

**Counted events and fixed oracles (D11).** AppendProbe instruments production work in separate phases; count every invocation/visit, not unique indices and not elapsed time. `validate_point(i)` is an incoming-point validation visit; `compare_point(i)` is a full raw equality/prefix comparison; `aggregate_member(i)` is a visit using raw quantities/duration/peak/suspend data to construct or reconstruct group/run aggregates. Pure validation/equality may inspect fields but may not sum them, build run membership or emit geometry. Spy on the aggregation primitives and their raw access so an alternate full-prefix reducer is charged too; do not add a hook only to the intended fast path. `scale_column(j)` reads one cached column's pair of rates; `bound_read`/`bound_write` count input/output two-integer bounds during promotion; `geometry_column`, `interval_visit`, `vertex_emit`, and `closure_emit` count cached geometry traversal and every emitted coordinate (three closure coordinates per fill). Geometry/paint raw-member aggregation is forbidden. Logical E is the retained canonical interval count; V is total curve vertices over both sides (the concrete F contribution); R is emitted curves. Record actual interval-merge visits separately from geometry visits.

All fixtures below have positive height, exact plot W=8, raw ten-second buckets, nonzero read/write, no suspension, identity unchanged; index i is zero-based. Original bytes are read=1/write=2 per bucket, optional peaks None and wall times None. Reset counters after the initial full setter. Replacing a tail means changing only its read to 5 and write to 1, retaining its interval. Exact output totals are checked independently of the work counters.

| Fixture / operation | Numerical and structural observations required |
| --- | --- |
| Dense 31 contiguous buckets, start `i*10_000_000_000`; replace index 30 | g=4, C=8, bounds `[(0,3),(4,7),...,(24,27),(28,30)]`. Exactly 31 validation visits; at most 124 raw equality comparisons, counted separately. `aggregate_member` trace exactly `[28,29,30]` once each, no index below 28, no promotion-bound copies. Exactly 8 scale-column visits; total read=35/write=61. E=8, R=2, V=16, 2 closed fills with 11 coordinates each. Geometry_column<=16, interval_visit<=16, vertex_emit=16, closure_emit=6; total geometry counted events<=54. No raw-member visits during paint. |
| Dense 32 contiguous buckets; append index 32 unchanged read=1/write=2 | Before: g=4,C=8. After: g=8,C=5, bounds exactly `(0,7),(8,15),(16,23),(24,31),(32,32)`, read=33/write=66. Exactly 33 validation visits, comparisons<=132; aggregate_member exactly `[32]`, zero prefix reaggregation. Promotion reads at most 9 two-integer bounds and writes at most 5 (10 output integers); no member-index vector is allocated/copied. Merge-input interval visits<=9, final E=5; exactly 5 scale visits, R=2,V=10. Geometry_column<=10, interval_visit<=10, closure_emit=6, total geometry events<=36. |
| Fragmented 31 buckets, start `i*20_000_000_000` (ten-second hole after each); replace index 30 | Same g=4,C=8 and aggregate trace `[28,29,30]`, 31 validation visits/comparisons<=124, 8 scale visits and read=35/write=61. E=31 (four intervals in each full group, three in tail), R=62 (one curve per run/side), V=124, each fill has 5 coordinates, closure_emit=186. Geometry_column<=16, interval_visit<=62, vertex_emit=124, total geometry events<=388. Thus V>W and E>W are preserved rather than capped; every run retains its own inset support. |
| Equal-input no-op on the dense 31-bucket fixture | Validation=31/comparisons<=124; aggregate, scale, promotion, geometry and repaint counts all zero. |

For geometry the stated ceilings are `2*C + 2*E + V + 3*R`, allowing a per-side cached traversal; promotion interval merging is separately charged as above. These are implementation-independent counted-event ceilings plus exact structural outputs, not an assertion that all helpers run twice. A full-prefix aggregation mutation must fail the `[28,29,30]`/`[32]` traces even if output bytes match; a copied-member-list mutation must fail the bounds shape/element-copy counts; a run-collapse mutation must fail E/R/V and support assertions. Admitted O(n) validation/comparison counts cannot absorb any of those forbidden events.

## 9. Status composer and independent age label

### 9.1 Measurement observation and widget ownership

CollectorWindow alone chooses the embedded Overview status text. AppIoWindow renders that supplied text and exposes a pure observation for the composer; it does not overwrite the embedded status after a detail refresh. Add:

```python
@dataclass(frozen=True)
class MeasurementStatus:
    has_displayed_values: bool
    ranking_empty: bool
    share_state: PartPhase
    series_state: PartPhase
    share_reason: str | None
    series_reason: str | None
    displayed_answers: tuple[AnswerProvenance, ...]

@dataclass(frozen=True)
class PresentationRefusal:
    origin: Literal["shell_prepare", "direct_view"]
    window: Window
    reason: str
    text: str

@dataclass(frozen=True)
class OverviewStatusChoice:
    row: Literal["1", "2", "3", "4", "5a", "5b", "6a", "6b", "6c", "6d", "6e", "6f"]
    text: str
    age_text: str | None

class AppIoWindow(QMainWindow):
    measurement_work_requested = pyqtSignal()
    presentation_refusal_changed = pyqtSignal(object)
    def __init__(self, query_service: QueryService,
                 boot_id: str | None = None, end_mono_ns: int | None = None, *,
                 files_view: FilesView | None = None,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None: ...
    def measurement_status(self) -> MeasurementStatus: ...
    def refresh_measurements(self) -> None: ...
    def show_overview_age(self, text: str | None) -> None: ...

def oldest_answer_age(displayed_answers: Sequence[AnswerProvenance],
                      now_ns: int) -> str | None: ...

class CollectorWindow(QMainWindow):
    def _compose_overview_status_choice(self, now_ns: int,
                                        age_now_ns: int) -> OverviewStatusChoice: ...
    def _compose_overview_status(self, now_ns: int) -> str: ...
    def _update_overview_status(self, now_ns: int) -> None: ...
```

**Typed refusal ownership (D08).** PresentationRefusal and OverviewStatusChoice are UI-local immutable records in render_model.py. Validate origin/row enums, exact Window and nonempty UTF-8 reason/text; OverviewStatusChoice.text is empty iff row is 6f, and age_text is `None` exactly when no displayed measurement exists, otherwise the exact §9.3 label. `_compose_overview_status` and `_update_overview_status` capture the age clock **once** per call and pass that same instant as `age_now_ns`, so the text wrapper and the label updater never read two different instants. `_update_overview_status` consumes the single returned choice for both the status label and `show_overview_age(choice.age_text)`. CollectorWindow owns `self._presentation_refusal: PresentationRefusal | None = None` for its embedded console. AppIoWindow owns the corresponding local state only for standalone composition; embedded direct refusal/adoption emits `presentation_refusal_changed(PresentationRefusal | None)` synchronously to the connected shell before composing. The shell validates that input and updates its own state, so detail notification cannot overwrite the category with text. Shell prepare refusal sets a shell_prepare record directly; the direct UI uses window_disabled_reason's exact refusal reason/text (including A22) as a direct_view record. Neither surface infers exception class, origin or Window from error_label/detail text.

| Event | Presentation state and exact clearing lifecycle |
| --- | --- |
| Shell prepare precheck raises WindowUnavailableError for W | Set shell_prepare refusal(W, error.reason, unchanged rendered error); preserve last applied frame/measurements and rethrow as frozen. The existing seam clears PrepareRetry/_prepare_error, creates no floor/pending, and owns those writes. |
| Direct set_window(W) is refused | Set/emit direct_view refusal for W after the frozen cleanup of both Overview and Files, selection retirement and unavailable rendering. This replaces any prior presentation refusal. The shell's retained `_displayed` field is not permission to recreate the cleared view/age. No query occurs for W. |
| Ordinary tick, detail settlement/notification, status-only probe, blocked gate, or retry-floor passage | Preserve the refusal, including its Window and exact text; only age/model may change. Repeated observation is not a new category/clear. A supported request that cannot pass its existing gate does not clear a refusal. |
| A gated supported shell prepare is accepted | Clear a prior shell_prepare refusal and its mirrored error widget in that acceptance transition. A direct_view refusal stays until a supported view adoption; starting a background preparation does not undo its cleanup. Existing retry-marker clearing stays in its original owner. |
| Shell prepare raises an operational exception for W | The frozen seam records PrepareRetry(W, origin, now) and exact `_prepare_error`. Supersede a prior shell_prepare refusal with this operational attempt; a direct_view refusal stays until supported adoption and still wins row 1 while present. No new floor or retry writer. |
| A supported AppIoWindow.set_window(W) is adopted (including shell `_show_ready`) | Clear/emit None for the refusal before composing the supported model, and clear the corresponding refusal error widget. This clears either previous origin; failed/blocked preparation is not adoption. |
| Explicit named retry or another explicit request | Only frozen show_window/_submit_prepare may retry/supersede PrepareRetry. Do not clear a direct_view refusal merely for a blocked request. Successful acceptance/adoption or a new typed failure follows the rows above. |
| Frozen no-target reset or close | Clear the refusal with that presentation reset. The no-target branch clears operational state only where the frozen retry seam already permits it; an owned explicit retry is not consumed by notification/poll. |

Row 2 is derived **directly** from `_prepare_retry is not None` and its still-owned `_prepare_error` (exact `appio: …` text set by the same failing call), not from a second operational-presentation flag. The record's Window/origin/since remain the association. Existing marker set/clear sites in _submit_prepare, named show_window supersession and the permitted no-target branch remain sole writers. If that marker exists, `_prepare_error` must be nonempty; a detached old error label cannot win row 2. The composer does not clear either field. Clearing a mirrored refusal/error widget uses its typed owner/category, never string-prefix classification. Standalone direct refusal uses the same typed record/lifecycle and row 1; it has no shell PrepareRetry.

MeasurementStatus is UI-local pure immutable data in render_model.py, with exactly these fields; booleans must be exact bool, phases legal, reasons present exactly for failed states, displayed_answers a tuple of distinct dependency provenances in fixed ranking, totals, series order. It contains no queries or widgets. `ranking_empty` is true only for a successfully applied empty ranking, not a failed/not-yet-applied list. `has_displayed_values` means at least one of TOTAL/READ/WRITE/DEVICE or a task lane currently renders from accepted effective input. Defined PEAK alone does **not** make it true. A nonempty accepted task-point sequence counts even if its measured bytes are zero; an empty no-samples caption does not.

`displayed_answers` includes only dependencies actually contributing a rendered measurement: share for its measured KPIs/composition/gutters; series for a rendered task lane or eligible device lanes; current ranking only when PEAK is defined/displayed. Coverage/status text, an unsupported lifetime series, failed/absent inputs and an available-empty series alone do not count. PEAK-only display can therefore have an age while has_displayed_values is false. For identity-less fallback values include identity-less AnswerProvenance rather than dropping a contributing dependency. Missing optional ranking provenance is represented identically when PEAK is displayed.

Build one persistent QLabel in Overview named `overview_age`, next to the existing status label. `show_overview_age(None)` clears/hides it; other nonempty text sets/shows it without affecting data/status. The age label is independent of the status label's visibility, refusal, busy, retry or live text. The AppIoWindow clock keyword is additive; all existing positional arguments and Files ownership remain. **Clock domains.** Two clocks exist and are never mixed:

| Domain | Members | Used for |
| --- | --- | --- |
| Age/acceptance (monotonic) | `IodClient(clock_ns)`, `HistoryRouter(clock_ns)`, the view/adapter acceptance stamp, and the composer's age `now` | accepted-identity `first_seen_at_ns`, `oldest_answer_age`, transport `issued_at_ns`, expiry and retirement deadlines |
| Window/recovery (BOOTTIME) | the shell's existing `_clock_ns` | `_current_window` bucket boundaries, `PENDING_BUDGET_NS`, `_stalled`, `_retry_after` |

`CollectorWindow` therefore gains one additive keyword, `age_clock_ns: Callable[[], int] = time.monotonic_ns`, independent of the frozen `clock_ns`. Production composition passes the **same** `age_clock_ns` to `IodClient`, `HistoryRouter`, and the embedded view/adapter, and the composer derives its age `now` from it; it never passes the BOOTTIME `clock_ns` into `oldest_answer_age`. The router's acceptance clock and the client's deadline clock are the same domain by construction, so a request's `issued_at_ns` is comparable with a retirement deadline and an answer's stamp is comparable with the composer's age `now`. A BOOTTIME `now` reaching age subtraction, or a client deadline compared with a BOOTTIME instant, is a contract violation, not an implementation choice. Standalone AppIoWindow uses the same pure status/age rules with no shell frame-acquisition/stall inputs and its injected/default clock; it does not gain a collector or timer issuing queries. `oldest_answer_age` lives in render_model.py, returns exactly §9.3's text/None, performs no I/O, and raises ValueError for malformed provenance or a noninteger/boolean/negative now_ns. The embedded status owner remains CollectorWindow.

### 9.2 Ordered, exhaustive status decision tree

Two instants are in scope for one update and are never interchanged: `now_ns` is the **BOOTTIME recovery instant** used by the stalled/busy/retry predicates below, and `age_now_ns` is the **MONOTONIC age instant** passed only to `oldest_answer_age`. `<state>` is exact `str(history.view_state)`. Import the frozen predicates unchanged:

```text
stall_eligible = self._stalled or self._retry_after is not None
busy = stall_eligible and history.local_read_in_flight is True
any_work = (_pending is not None or history.pending_work)
```

Include queued/held detail and Files work in history.pending_work as §3.1 specifies; a setup probe alone is not history work. Compose exactly one return in this order:

| Row | Predicate (only if earlier rows did not return) | Exact status label |
| --- | --- | --- |
| 1 | `_presentation_refusal is not None` (or the standalone view's owned equivalent) | Its typed record's exact frozen text, verbatim; no prefix |
| 2 | `_prepare_retry is not None`, with its associated `_prepare_error` | That call's exact `appio: <error>` text |
| 3 | busy | `history read has not completed (<state>); the reader is still busy` |
| 4 | `_retry_after is not None and now_ns < _retry_after` | `history read has not completed (<state>); retrying in <n> s` |
| 5a | No applied frame and no target or no sample | `waiting for the first sample` |
| 5b | No applied frame, otherwise | `loading history (<state>)` |
| 6a | Applied frame, has_displayed_values and any_work | `updating (<state>)` |
| 6b | Applied frame, has_displayed_values | `live` |
| 6c | Applied frame, successfully empty ranking | `no applications observed` |
| 6d | Applied frame, share_state or series_state pending | `loading measurements (<state>)` |
| 6e | Applied frame, neither dependency pending, at least one failed | `measurements unavailable: <reason>`; first failed reason in fixed share, series order |
| 6f | Applied frame, otherwise | Empty string; status label hidden |

Retry `<n>` stays `(retry_after-now_ns + 999_999_999)//1_000_000_000`, positive only while the floor is future. An expired floor plus still-held reader uses row 3; there is no zero-second retry. Rows 1–4 precede all ordinary frame/measurement text but never suppress the separate age label. Files receives the frozen stall caption only for rows 3/4; clearing it restores its ordinary updating caption and changes no footer/query/admission state.

Rows 6c–6f prevent an applied empty/unranked/failed/unsupported-chronology panel from claiming live merely because a frame exists. For an ok lifetime series, failed share and undefined PEAK, the unsupported task lane is not a displayed value: row 6e displays the share failure. A settled file-only subject in a nonempty ranking uses row 6f, with its normal panel reason. A pending retry after a failure can use row 6d but cannot resurrect the cleared value.

Use the same composer on target ticks, deadline/retirement transitions, prepare success/failure/refusal, frame application and measurement notification. `_compose_overview_status_choice(now_ns, age_now_ns)` is the production decision function: `now_ns` is the frozen BOOTTIME recovery instant used by rows 3–4 and `age_now_ns` is the monotonic age instant passed to `oldest_answer_age`. It snapshots typed presentation/recovery/measurement inputs without queries and returns the **actual selected row ID, text and age text** as OverviewStatusChoice. `_compose_overview_status(now_ns)` is its text-only wrapper; `_update_overview_status` consumes that same choice to set visibility (`text != ""`) and the Files stall caption only for row 3/4. No second tree or test-only branch counter is allowed. Raw predicates may overlap (6a also satisfies 6b); assert the expected row/text for concrete inputs in §12.7, never a count of true predicates. No other code assigns ordinary loading/updating/live status independently. Composition is presentation only and cannot write recovery fields, clear retry markers or admit work.

### 9.3 Exact age arithmetic

When displayed_answers is empty, age text is None and the label is absent. If any contributing answer lacks its accepted identity or first_seen stamp, the exact text is `age unknown`; otherwise:

```text
first_seen = min(p.first_seen_at_ns for p in displayed_answers)
elapsed_ns = max(0, now_ns - first_seen)

if elapsed_ns < 90 * 1_000_000_000:
    age = f"{(elapsed_ns + 1_000_000_000 - 1)//1_000_000_000} s"
elif elapsed_ns < 90 * 60 * 1_000_000_000:
    age = f"{(elapsed_ns + 60*1_000_000_000 - 1)//(60*1_000_000_000)} min"
else:
    age = f"{(elapsed_ns + 3600*1_000_000_000 - 1)//(3600*1_000_000_000)} h"
text = f"oldest answer {age} ago"
```

Threshold selection uses **raw elapsed ns**, then integer ceiling in the chosen unit, not a rounded previous unit. Exact vectors: 0 -> `0 s`; 1 ns -> `1 s`; 999,999,999 ns -> `1 s`; 89.999 s -> `90 s`; 90 s -> `2 min`; 89.999 min -> `90 min`; 90 min -> `2 h`. No floats, nearest rounding, truncation, wall-clock subtraction or collector-age wording. max(0, …) defensively handles a test clock behind a stamp; production clocks/stamps share one monotonic domain.

Update age on every ordinary clock tick and every model change without queries, repainting only changed text. Reapplying the same accepted identity cannot reset its stamp. A changed exact selector Window with the same snapshot does reset to the first acceptance of that distinct identity. Use the oldest **displayed** dependency, including defined PEAK, not the youngest successful result or the oldest requested/discarded dependency. **Specification decision:** any unidentified contributor yields age unknown because the oldest displayed answer cannot then be established reliably.

## 10. Error behaviour and reproducing fixtures

Error text below is nonempty original str(error), or its class name when empty, except the explicit invalid_result boundary token. Existing unavailable values are always literal `unavailable`; no failure produces numeric zero.

| Situation | Required data/presentation result | Fixture / owning test |
| --- | --- | --- |
| No frame, acquisition pending | Tree 5a/5b; no fabricated values or age | ControlledFrame, `test_live_status_initial_frame_pending` |
| Applied measured values, detail/frame/Files pending | Retained inputs remain effective, tree 6a, independent oldest-answer age | LiveOverviewHarness, `test_pending_refresh_keeps_retained_dependency_values` |
| First detail pending, no values | Tree 6d; PEAK independently governed; no age unless PEAK is defined | `test_live_status_first_and_retry_details_pending_without_peak` |
| Current RemoteError, refusal, transport failure, query timeout or invalid_result on a still-live owner | Failed slot, record cleared before notification; exact dependent reason shown; only a new success restores it. Connection loss that changes authority instead retires the owner/record before its obsolete error is drained (§3.2). | `test_current_detail_failure_clears_record_before_view_notification` |
| Success -> current failure -> pending retry | No value restoration; independent other dependency remains; undefined PEAK cannot mask the loss | `test_failed_record_cannot_be_revived_by_pending_retry` |
| Share ok, series failed/pending | Share KPIs/donut and eligible DEVICE/residual/bars survive; task lane follows its own state, device lanes withheld | `test_share_outputs_survive_series_failure_or_pending` |
| Share ok, series device-incomplete | Share-derived DEVICE, residual, bars remain; task lane remains; device lanes unavailable | `test_series_device_incomplete_only_withholds_device_lanes` |
| Share failed, series ok | Task lane remains; share KPIs/composition/reconciliation unavailable with share reason | `test_share_failure_keeps_independent_task_lane` |
| Share device-incomplete or undefined device total | Update task outputs; remove DEVICE/residual/bars/lanes in same application | `test_new_share_device_unavailability_removes_old_device_values` |
| Undefined residual with otherwise available device total | DEVICE and bars survive, reconciliation absent | `test_undefined_residual_does_not_remove_device_total_or_bars` |
| Mixed measured endpoints | Each independent output stays with own provenance; device-lane endpoint disclosure; no restamp | `test_mixed_endpoint_inputs_keep_independent_outputs` |
| Lifetime chronology | Valid share composition/totals survive; task/device time lanes unavailable | `test_lifetime_detail_keeps_totals_without_chronology` |
| Superseded result/error or duplicate settlement | No state/record/age/marker mutation; terminal wire outcome may release only its own slot | `test_settlement_superseded_or_duplicate_has_no_publication_effect` |
| Wrong selector type/subject/Window/association | Current invalid_result failure, never absent/restamped | `test_settlement_rejects_each_selector_shape_or_binding_violation` |
| Slow scope-owned detail crosses forward frame generations | Accept exact requested Window once, immediately readable through retained even with a replacement pending | `test_remote_detail_liveness_across_ten_second_commit` |
| Subject/scope/boot/backward endpoint/authority change | Clear old records before new-scope rendering; pending/late results cannot repopulate them | `test_retention_resets_before_subject_scope_boot_or_backward_display` |
| Future timeout without daemon terminal outcome | Failure is visible but transport stays occupied; successor waits or old connection retires first | `test_query_timeout_keeps_transport_slot_until_terminal_or_retirement` |
| Local observer active transaction or identity mismatch | Operational prepare/readiness error, no false source certification or caller transaction commit | `test_local_observations_require_committed_registered_connections` |
| Available-empty chart | No shapes/zero line, `no samples in this window`; availability remains true | `test_live_append_row_4_empty_transition_and_noop` |
| Append contradiction or invalid point | Valid contradiction rebuilds; invalid input raises ValueError with retained state unchanged | `test_live_append_row_11_interior_contradiction_rebuilds`, `test_live_append_row_1_invalid_preserves_state` |
| Shell precheck refusal / operational prepare failure | Exact refusal / appio text with unchanged frozen origin, retry-floor and retained-frame ownership | `test_live_status_refusal_and_operational_error_precedence` |

A local worker exception never becomes a successful empty measurement. Remote ranking fallback certifies no daemon source; Files scheduling does not relax its fatal-vs-skippable candidate rules, table/footer semantics, served-gap None rules or snapshot guards. Recovery diagnostic classification remains ranking-owned as frozen; detail failures have their own slot reasons.

## 11. Exhaustive frozen-contract amendment ledger

Rows A01–A18 correspond one-for-one, in order, to design §9. They are effective amendments recorded here; none requires rewriting the frozen documents.

| ID / frozen location | Precise replacement | Still required |
| --- | --- | --- |
| A01 charts-spec §3.2 task setter as sole entry | §8.1 adds task/device append and optional setter identity; §8.3 pins total decision table | Full validation, explicit rebuild setters, unavailable clearing |
| A02 charts-spec §3.2 segments as sole geometry accessor | §8.1 ChartCurve/curves for strokes; segments remains the exact closed fill outline | ChartSegment fields/validation and observable fill geometry |
| A03 charts-spec §2.2 exact requested-Window parts | §7 admits per-dependency measured Windows in the same scope and not beyond applied/requested endpoint, with exact provenance | Subject contradiction refusal, independent dependencies, exact bytes |
| A04 charts-design §6 / charts-spec §8 errors | §§3.4, 6.2, 7 retain pending values, clear a current failure and preserve unaffected dependencies | Unavailable is not zero; independent current-frame PEAK |
| A05 charts-spec §6.2 step 2 bins | §8.4 consecutive power-of-two groups, pairwise promotion, bounds and interval cache | C<=W, each sample contributes once, peaks/envelopes/starts_run |
| A06 charts-spec §6.3 step shape | §8.6 per-run midpoint polylines plus closed fills and monotone inset | Per-side exact scale, maxima, holes, finite coordinates, exact arithmetic |
| A07 charts-spec §7 / recovery-spec §§4.3, 8 status | §9 one ordered status tree and separate age widget; add updating/live/loading-measurements/measurements-unavailable templates | Frozen refusal, operational error, busy, retry, initial loading/waiting/empty strings; recovery §4.4 unchanged |
| A08 charts-design §§5.5–5.6 / charts-spec §4.2 trigger | §6.1 suppresses certified endpoint-only triggers, adds source/uncertified triggers | Submission gate, retirement, coalescing, floors, explicit owner, probe rules |
| A09 charts-spec §1 router boundary / eager local detail | §§2–4 explicit slot/retained/provenance/clock interface, typed local outcomes and independent bounded frame/background execution | Existing query/refusal guarantees and no concrete QueryService change |
| A10 new retained accessor | §3.1 exact retained signature, eligibility and publication-before-notification | Exact-window getters and HistoryPending |
| A11 charts-spec §2.1 / §3.3 model and UI protocol | §§2, 6.2, 7.1 per-part provenance, SeriesMetadata, explicit requested Window/external key, legacy-direct defaults and optional retained/identity/reset capabilities | Old positional model meanings, no concrete router in view, exact-only services valid |
| A12 new local source position | §4 O(1) `(name,owner,registration_epoch,data_version,total_changes)` and ordered full vector/bracket | No schema or writer change; no table/rowid hashing |
| A13 charts-design §§5.5–5.6 / charts-spec §§3.5, 4.2, 4.5 poll/apply | §6.3 context-specific no-frame drain/notify, ordinary deferred gate, poll drain/apply only while work exists | Pump transport-only, one frame-apply path, unchanged recovery §4.5 error owner |
| A14 client boot/epoch cache key | §§5.3–5.4 canonical query plus full cursor/original answered_source, shared validation before insertion and bound-result rejection eviction | Existing cache cap, eviction, read-through behaviour and failure exclusion |
| A15 client Future expiry/transport ownership | §§5.1–5.2 terminal/lost/retired release, tombstones, deadline and guarded retirement seam | Five-second Future settlement and unchanged wire |
| A16 Series/DiskShare decode metadata | §5.3 preserves external app_key and binds correlated immutable QueryRequest | Existing decoded/wire field contracts |
| A17 Files direct dispatch | §§5.1, 5.4 scheduler for pages/continuations/coverage and one acceptance writer | Files requested/served identity, paging, snapshot, counters, ownership, rendering |
| A18 new local registration identity | §4.2 epoch changes on installation, replacement, reopening and physical rebinding | Existing add_source generation invalidation |

No amendment authorises changing recovery-field set/clear sites, DEVICE completeness policy, lifetime Window validity, Files accounting, QueryService/store schema, daemon query selectors, root ownership, sampling cadence, consent, launch/range controls, wire format or daemon cache identity implementation. The cursor-complete client cache is not a rewrite of the daemon's database snapshot digest.

## 12. Test inventory and implementation gates

All entries are required later implementation tests, not results. Use synthetic committed SQLite stores, controlled Futures/Events, an injected monotonic ns clock, and offscreen production Qt widgets. No real daemon, root, systemd operation, power/session transition or wall-clock sleep is required. A fake daemon job may be a socket-pair/protocol harness driving real IodClient decode and correlation; it must not invoke a service. Tests exercising a race need a barrier for the specified ordering, not repeated timing guesses.

### 12.1 Shared fixture contracts

- `LiveOverviewHarness`: production HistoryRouter, IodClient where transport is under test, AppIoWindow and CollectorWindow with timers stopped/explicitly driven; synthetic registry subjects A/B; independent ranking peak None by default; real retained accessor, queued notification and UI rendering. Hold frame/series/totals/Files outcomes separately. Include both a delivery-only local barrier and a **real detail worker computation barrier** on the background executor; while the latter is held, submit/complete/apply a later frame on its independent executor before releasing the detail. Count worker submissions, charged capacity including retired jobs, frame retirements/replacements/applications and publication/clear/notification order.
- `QueryTransportHarness`: real client framing/normalization/cache/timeout path, virtual clock, correlated result/error injection and a fake peer whose one historical job remains occupied after caller expiry. It may continue streaming snapshots/status while that job is held. Count actual query envelopes, connection generations, terminal events and socket retirement.
- `LocalPositionStores`: two file-backed synthetic stores, persistent registered QueryService connections, independent external-writer connections, and explicit registration replacements. No copy-observer connection is substituted for a registered observer. Expose backup-complete and closing-read barriers plus committed deletion/update fixtures.
- `AppendProbe`: production charts with exact plot W=8 and the fixed 31/32-bucket fixtures/counted events/ceilings of §8.7, raw validation/comparison and aggregation counters kept separate, observable cache geometry and update/repaint calls. Instrument actual raw aggregation and bound element copies, including alternate/rebuild paths; no public debug interface or hidden extra scans.
- `ExactOnlyQuery`: structural frozen query service with independent exact results/HistoryPending/errors and no retained methods. Identity-bearing variant implements exactly `exact_answer_identity` (§6.2); identity-less variant omits it. Drive actual adapter/UI authority epochs, including reset with an unchanged lifetime Window. Both use the real UI adapter.
- `status_cases`: §12.7's explicit typed input/expected-row/text/visibility table plus shell lifecycle traces. Drive the production choice function and real labels, count decision calls and query/prepare events per specified action, and assert age separately; never substitute a true-predicate count.

### 12.2 Records, settlement and retained failures — `tests/p3/test_live_overview_router.py`

| Exact test name | Required observation |
| --- | --- |
| `test_live_records_validate_every_field_and_phase_combination` | Validate §2 fields including SeriesMetadata, bool-as-int, digest/source/identity/window validation and PartState truth table; local private outcome variants also reject malformed field combinations. |
| `test_local_outcomes_share_settlement_for_frame_part_and_files` | One positive arm per local variant uses actual workers: frame ranking plus every routing/file-availability field publishes atomically; selected part publishes its local digest/exact Window; local Files publishes table+coverage+served_gap_count unchanged as FileViewResult. Assert zero client.query/query_transport calls for local inputs, one settlement/publication per token and no side writer. Malformed arms independently corrupt worker_id/request association and, for each variant, frame evidence, part key/type/Window/digest, or Files table/coverage/served gap; current invalid_result/readiness-or-Files failure before any owner evidence publishes. Stale local success/error is drained without publication. |
| `test_settlement_rejects_each_selector_shape_or_binding_violation` | Separate single-fault cases for wrong kind/key/Window/request association, Coverage for totals, duplicate ranking identities, file coverage in process slot, scalar in unscoped process slot, and invalid UnknownOwnerOverflow. The unscoped process-mapping arms are exactly the three mappings from §12.3 with every other envelope/request field valid: `{"unit:/alpha.service": <valid Coverage>}` (key only), `{"unit:alpha.service": "not a Coverage"}` (value only), and `{"unit:alpha.service": <valid Coverage>}` as the passing control. Each current rejection fails before publication. |
| `test_settlement_superseded_or_duplicate_has_no_publication_effect` | Superseded generation for frame only, dead detail lifetime, connection authority, scope, boot and subject; delayed success and failure; duplicate settlement after success/failure. No record/stamp/marker mutation; release only matching terminal wire slot. |
| `test_scope_owned_detail_survives_forward_frame_generations` | Ordinary prepare does not cancel pending detail, clear a record or invalidate owner_id; delayed exact A-window result is retained under A while B is applied/pending, never relabelled. |
| `test_current_detail_failure_clears_record_before_view_notification` | Parameterise RemoteError, WindowUnavailableError, injected ConnectionLost with the token still authority-current, query_timeout and invalid_result; record absent inside notification, exact failed reason/getter exception. Actual connection/authority loss uses the separately tested retirement transition, never a failed observation in a new lifetime. |
| `test_failed_record_cannot_be_revived_by_pending_retry` | Real retained accessor + production view: success -> current failure -> pending retry with undefined PEAK and successful independent other dependency; cleared outputs remain unavailable until a new success. |
| `test_exact_getters_reject_older_retained_window_without_hiding_record` | Exact getter raises HistoryPending for B, retained returns accepted A for applied B, and no Window/app_id/external-key repair masks a mismatch. |
| `test_retained_accessor_uses_applied_endpoint_and_is_pure` | Preparing a forward or backward replacement cannot change eligibility before adoption; no sends/drain/stamp on accessor; future endpoint returns None and malformed scope raises ValueError. |
| `test_superseded_failure_does_not_clear_newer_accepted_record` | Retire/supersede old token, accept newer, deliver old error, then pending; new record and first-seen stamp stay exact. |
| `test_ranking_readiness_excludes_all_detail_and_files_work` | Hold each series/totals/local-detail/Files page/continuation/coverage independently after frame ranking/ownership completes; ready true without settling held work; no eager first-subject/all-row parts. |
| `test_local_detail_is_lazy_selected_subject_only` | Multiple ranked subjects cause no detail construction before selection; selecting one builds only requested kinds; private-snapshot/store-local identity resolution and existing ownership reducers preserved. |
| `test_remote_detail_liveness_across_ten_second_commit` | Exact schedule in §12.8; every completed totals reply retained/readable and displayed with own Window, including while replacement frame is pending. |
| `test_held_local_detail_accepts_once_after_forward_frames` | Delivery-barrier arm: release the computed A outcome after B applies, accept once with A Window/digest. Computation-barrier arm: hold a real local detail inside its worker, use an explicit supported request or committed source advance to submit newer frame B, await its ranking/ownership completion and application on the frame executor **before releasing detail**; then release A and drain, exactly one detail settlement/publication, no duplicate job and correct original Window. |
| `test_retention_resets_before_subject_scope_boot_or_backward_display` | Design §12 row 12 transitions, including backward-then-forward and two distinct ranges; clear before render, not on a later tick. |
| `test_authority_change_clears_details_without_conflating_cursor_advance` | Connection/boot/epoch or local registration change retires lifetime/records; same-authority seq/counter advancement preserves them. |
| `test_authority_change_retires_pending_frame_once_and_replacement_applies` | Complete local frame A evidence, hold its remote ranking Future, then lose/reverify the connection to the same answering boot/epoch before draining ConnectionLost (new client generation, unchanged frozen history key). First drain retires A once; apply observes unavailable once, no old remote certification. Before the 30 s floor: replacement prepare count 0; at floor with reader free: exactly 1 replacement B; complete B and assert exactly 1 application/certificate for B. Repeated old success/error/drains cause 0 extra retirements/replacements and cannot strand loading. Same-authority seq/counter control coalesces with retirement count 0. |
| `test_local_worker_capacity_counts_retired_jobs_and_keeps_frame_independent` | Hold two charged detail jobs and eight Files workers including retired running/queued jobs; repeated selection changes submit no eleventh background Future. Waiting current detail stays pending with at most one successor/kind. Release/drain one charged job, admit at most one eligible replacement. Frame work still has its own single Future, and a held retired frame blocks another frame through the frozen gate rather than allowing a duplicate. |

### 12.3 Client, scheduling and selector routes — `tests/p3/test_live_overview_client.py`

| Exact test name | Required observation |
| --- | --- |
| `test_all_history_queries_share_one_priority_stable_scheduler` | Ranking, selected series/totals, Files first/next page and all coverage variants produce at most one actual outstanding historical envelope; priority and stable serial ordering exact; no self-generated query_busy. |
| `test_query_timeout_keeps_transport_slot_until_terminal_or_retirement` | Expire Future with peer job held; successor remains unsent until a late result/error or closed old socket. Snapshot/status traffic cannot release it. Assert immediate failed-record notification separately from slot lifetime. |
| `test_expired_query_retires_at_request_specific_silence_deadline` | At expiry+NO_ANSWER_NS-1 no forced retirement; at equality retire before successor. Earlier connection-wide silence may close sooner. Stale Future cannot retire new connection. |
| `test_cancelled_or_publication_retired_future_keeps_wire_occupant` | Future cancellation, owner retirement and generation clearing do not free transport; terminal or real socket loss does. |
| `test_late_expired_terminal_releases_without_second_publication_or_cache` | Correlated late result and error reaped once; no success resurrection, stamp update, stale error overwrite or late successful cache insertion. |
| `test_cache_and_refusal_futures_keep_immutable_request_association` | Immediate outcomes expose exact normalized request before callbacks; unknown Future rejected; cache hit consumes no wire slot; malformed request/shape cannot be adopted under another selector. |
| `test_full_cursor_cache_key_and_original_answered_source` | Same cursor hit preserves provenance/certifies; next seq cannot hit old entry; injected old-seq cache payload cannot certify new requested source. Preserve byte LRU bounds and failure exclusion. |
| `test_malformed_result_is_not_cached_and_same_cursor_retry_sends` | At fixed cursor, inject wrong kind/key/Window, each incompatible coverage variant, `{"unit:/alpha.service": <valid Coverage>}` (invalid AppKey only), `{"unit:alpha.service": "not a Coverage"}` (invalid value only), duplicate ranking identities and a broken request association as separate single-fault arms, with `{"unit:alpha.service": <valid Coverage>}` as the passing positive control for both malformed arms. First Future fails invalid_result, router has no accepted value and cached_responses remains 0. Retry identical request/cursor: actual query-envelope count increases from 1 to 2; inject valid answer, accept once, cached_responses=1; third identical read is the first hit and sends no third envelope. Expired reply adds no entry. A current owner-context rejection evicts only its bound entry version; it cannot remove a later replacement. |
| `test_totals_and_series_decode_preserve_external_subject` | Actual wire encode/decode for A/B with differing decoder/router IDs; external key survives and wrong key is rejected before presentation remap. |
| `test_each_coverage_selector_accepts_only_its_contract_shape` | Subject process scalar, unscoped key->Coverage mapping, FileCoverage and UnknownOwnerOverflow positive controls plus every incompatible union alternative; no invented echoes for plain Coverage. |
| `test_series_accepts_simultaneous_devices_and_rejects_per_device_overlap_or_order` | §5.4's exact two-bucket/two-device raw fixture accepts unchanged on remote and local routes. Repeated same-dev bucket, same-dev overlap, reversed UTF-8 device order within a bucket and reversed bucket order each fail invalid_result before publication/cache insertion; duration/tier and coverage-tier mismatches are separate negative arms. No global nonoverlap check rejects the simultaneous-device positive. |
| `test_files_pages_and_coverage_use_single_settlement_acceptance` | Valid paged remote result/cursor chain and all existing key/Window/snapshot/cursor-generation/metadata/coverage/served-interval/128-page/50,000-row refusals; no partial publish and remote served_gap_count None. |
| `test_frame_failure_or_local_fallback_certifies_no_remote_position` | Failed remote ranking and local fallback never promote remote cursor. Remote-completion-first arm: local ownership evidence is held; ranking success cannot publish yet, transport may reap; complete local mixed-store/overlap evidence, reject remote candidate and evict its bound cache entry. An older completed equal-Window/source frame supplies neither the new generation's certificate nor PEAK; nonempty/mixed-store/pagination restrictions remain. |

### 12.4 Local positions and source-trigger integration — `tests/p3/test_live_overview_router.py`, `tests/p3/test_collector_window.py`

| Exact test name | Required observation |
| --- | --- |
| `test_local_vector_observes_each_registered_connection_without_hashing` | Exact PRAGMA/property values and name order over two sources; constant per-source observation; no table scan/hash/rowid/new observer connection. |
| `test_local_vector_changes_for_every_existing_commit_path` | Separate committed older-row deletion, rollup deletion, in-place collector_epoch update, receipt/handover update and non-primary-only write, using both own-connection and external commits. Row maxima remaining unchanged cannot defeat the token. |
| `test_local_registration_add_remove_replace_and_reopen_never_repeat_token` | Addition/removal, same-name/owner replacement, same-counter new connection and physical rebinding each change vector; failed rebinding cannot certify old observer/new copied file. |
| `test_local_observations_require_committed_registered_connections` | Both opening and closing observers reject active transactions; no caller commit/rollback or pinned transaction hides a commit; missing/invalid observer/physical binding yields operational error. |
| `test_commit_during_backup_is_uncertified_with_one_replacement` | Commit behind backup barrier before completion; unequal bracket, own data may apply, displayed_source not promoted, exactly one later replacement. |
| `test_commit_after_backup_before_closing_read_is_uncertified` | Distinct schedule with backup already finished; closing boundary still observes change and coalesces exactly one replacement. |
| `test_commit_after_closing_read_is_caught_by_next_comparison` | Frame certifies its equal bracket; later commit does not retroactively alter it, next tick prepares once. |
| `test_unchanged_certified_local_or_remote_source_is_quiescent` | Repeated endpoint-only ticks, both modes, issue no prepare; displayed Window remains its measured Window. Source change/explicit request each permit one gated prepare. |
| `test_pending_source_advances_coalesce_without_retiring_detail` | Multiple cursor/vector changes while a frame is pending yield one replacement after apply and preserve old detail lifetime; only frame ranking promotes displayed_source. |
| `test_recovery_transition_ownership_survives_source_driven_trigger` | Frozen local queued-reader gate, 30 s budget, retirement floor, explicit retry owner, `_refresh_after_apply` sole sites, and same-source probe reconciliation remain; no source comparison bypass. |
| `test_no_frame_poll_drains_details_and_files_and_notifies_immediately` | With `_pending is None`, series/totals/Files completion and held expired slot keep poll active; accept/fail reaches the view before 1 s tick. Zero prepares/target calculations in poll context, even with a deferred target; explicit PrepareRetry stays owned. Poll stops only when work is absent. |
| `test_no_frame_deferred_apply_uses_ordinary_gate_but_poll_never_submits` | Separate fresh fixtures with `_pending=None,_deferred_target=B`: free gate ordinary apply prepares exactly 1 (and applies at most 1 if immediately ready), poll prepares 0 and leaves B; held frame Future or future floor gives 0 prepares in both contexts and preserves B/floor. At expired floor with reader idle an ordinary call prepares 1, poll 0. Explicit PrepareRetry arm gives 0 both. Every arm drains/notifies completed detail first; no new recovery-field set/clear site. |

### 12.5 Pure model and view independence — `tests/test_ui.py`

`preserve_exact` arms below are preservation controls whose core numeric availability already holds in the unchanged renderer; a green run is not new-retention evidence. Run each discriminator arm separately through the actual retained accessor/adapter and production view. Unless a row states otherwise, A is since_boot boot-a through 20_000_000_000 ns, B the same scope through 30_000_000_000 ns, PEAK is None, accepted A-share read=17/write=5/total=22/device=14, first_seen=1 s and the B observation occurs at t=3 s. Its age is `oldest answer 2 s ago`; A's exact measured disclosure is `; share measured: since_boot boot-a through 20000000000 ns`. Use valid matching device rows/residuals (or the explicitly undefined residual arm). Do not present a baseline-green arm as an initial red or a kill for scope retention/settlement/provenance.

| Exact test name | Required observation |
| --- | --- |
| `test_overview_provenance_accepts_older_same_scope_rejects_contradictions` | Independent measured windows/stamps carried unchanged; future endpoint, other boot/range/subject, mismatched provenance or object+own-error raises ValueError; exact legacy calls valid and identity-less. |
| `test_overview_direct_metadata_validates_series_coverage_and_requested_endpoint` | Extended direct construction: otherwise eligible share/lanes plus series_metadata.coverage.device_state incomplete raises ValueError; metadata/provenance ending at 30 s with requested Window ending at 20 s and ranking_provenance None raises ValueError. Include metadata/key/ID/tier mismatch negatives, valid older same-scope control, and all-default frozen direct control. Builder always supplies typed requested Window and successful series metadata. |
| `test_pending_refresh_keeps_retained_dependency_values` | Refresh in flight preserves task/share outputs and widget identity, old exact measured Window and stamp; no HistoryPending passed as error or chart-unavailable repaint. |
| `test_share_outputs_survive_series_failure_or_pending` | `preserve_exact`: baseline-green exact-window share TOTAL/READ/WRITE/donut/DEVICE/bars/residual survive series failure. `retained_A_pending_B` discriminator: accepted share A (20 s) with totals 22/read17/write5/device14 stays through pending B (30 s) and failed/pending series, same A provenance/stamp and exact measured disclosure; no chart/device lane fabricated. |
| `test_series_device_incomplete_only_withholds_device_lanes` | `preserve_exact`: baseline-green share DEVICE/bars/reconciliation and task lane survive incomplete series. `series_coverage_reason` discriminator: eligible share with newer incomplete series withholds only device lanes and has exact `; device lanes: incomplete` (fixture unavailable_reason None), typed series coverage/tier retained; share-derived DEVICE stays 14. |
| `test_share_failure_keeps_independent_task_lane` | `preserve_exact`: baseline-green successful task lane with failed share/undefined PEAK. `failure_pending` discriminator: accepted A-series remains at A while B-share current failure then pending retry clears all share outputs, retains exact failure/absence record semantics and A measured disclosure/stamp. |
| `test_new_share_device_unavailability_removes_old_device_values` | Available -> incomplete and available-but-undefined-total cases; same-update DEVICE/bars/residual/device-lane clearing, task outputs remain. |
| `test_undefined_residual_does_not_remove_device_total_or_bars` | `preserve_exact`: baseline-green device total/bars with residual None. `retained_A_pending_B` discriminator: that A share remains DEVICE=14/bars while B pending, reconciliation absent, A Window/stamp/disclosure unchanged. |
| `test_mixed_endpoint_inputs_keep_independent_outputs` | Retained share/newer series and retained series/newer share; exact provenance and task outputs, no device lanes, exact endpoint disclosure. |
| `test_lifetime_detail_keeps_totals_without_chronology` | `preserve_exact`: baseline-green lifetime totals/donut/boot-qualified bars, unavailable time lanes. `failure_pending` discriminator: accepted lifetime share then current failure then pending retry leaves TOTAL/READ/WRITE/DEVICE unavailable with no restored record/age; a new accepted success alone restores them. |
| `test_exact_only_adapter_matches_retention_failure_and_reset_rules` | Plain exact-only success/pending/current failure/retry and scope/backward transitions match router retention; age unknown. Partial retained capability falls back, real capability errors do not. |
| `test_exact_only_identity_reacceptance_reuses_stamp` | exact_answer_identity returns a valid totals identity after successful A getter at t=1 s, repeat at t=3 s: stamp remains 1 s and age=2 s. Wrong kind/key/Window/digest or raising optional method fails current input; None remains identity-less. No capability query on pending/failure/notification. |
| `test_exact_only_authority_reset_retires_unchanged_lifetime` | Accept lifetime share at epoch1/t=1 s, leave series pending; reset_query_authority at t=3 s without changing Window. Old record/stamp/owner is cleared, notification sends no getters, late old notification publishes zero. Adopt same lifetime at epoch2 and accept new result at t=4 s: new stamp=4 s/age=0 s, exactly one acceptance. Same-authority forward refresh does not reset. |
| `test_detail_notification_is_query_free_and_preserves_other_dependency` | refresh_measurements queries neither ranking nor Files/admission/prepare; it rereads state/record, applies failure immediately, preserves independently successful other output and existing widgets. |
| `test_main_window_uses_data_identity_for_task_and_device_appends` | Tokens exactly §8.2 from model metadata; share raw plus series rollup_60s sends rollup_60s in task/device identities. End/snapshot alone retains identity; subject/scope/start/series-tier/dev change resets. Legacy-direct metadata absence uses full setters, no label parsing or task suspend flags on DeviceLane. |

### 12.6 Chart table, geometry and costs — `tests/test_charts.py`

| Exact test name | Required observation |
| --- | --- |
| `test_live_append_row_1_invalid_preserves_state` | Each frozen invalid point/ordering/type/peak case leaves input/identity/availability/reason/cache/geometry/repaint unchanged. |
| `test_live_append_row_2_unavailable_rebuilds_even_empty_or_same_points` | unavailable -> empty and unavailable -> previously equal points restore successful availability and clear reason/state correctly. |
| `test_live_append_row_3_identity_change_rebuilds` | Same points/new identity rebuild; device widget reused for another token cannot inherit state. |
| `test_live_append_row_4_empty_transition_and_noop` | Populated -> available-empty clears caches; empty -> same-identity empty makes no repaint; no samples text and no zero line. |
| `test_live_append_row_5_empty_to_nonempty_has_no_tail` | Append all into initial groups without accessing nonexistent old tail, including device variant. |
| `test_live_append_row_6_equal_input_is_noop` | Same complete raw points/identity, including setter->append, causes no aggregate/geometry/repaint work. |
| `test_live_append_row_7_tail_replacement_rebuilds_one_group` | Equal length/different final point; lowered sole peak, removed peak None and measured zero distinguished; old maximum not inherited. |
| `test_live_append_row_8_exact_prefix_appends_only` | Old exact prefix and new suffix change only open/new groups plus declared scale/geometry/promotion work. |
| `test_live_append_row_9_tail_change_then_append` | Changed shared tail, longer input: suspension change then append updates run boundary; None vs zero/wall/peak fields included in exact comparison. |
| `test_live_append_row_10_nonempty_shortening_rebuilds` | Shorter nonempty input rebuilds, reselects g and removes stale columns/peaks/runs. |
| `test_live_append_row_11_interior_contradiction_rebuilds` | Non-tail prefix contradiction at equal/longer length cannot be treated as append; new exact bytes/runs win. |
| `test_live_grouping_is_minimal_power_of_two_and_promotes_bounds` | §8.7 dense 32->33 fixture: g4->8,C8->5, exact five bounds, input-bound reads<=9/output-bound writes<=5 (10 integers), zero copied member-index vectors; appended raw visit `[32]` only. Include W=8 lengths 8/9/16/17/32/33 -> g=1/2/2/4/4/8 and C=8/5/8/5/8/5, exact byte conservation. |
| `test_live_tail_replacement_recomputes_duration_peak_and_run_state` | Changed tail duration/peak/suspend at promotion boundary rebuilds only its group; subsequent append uses changed run flags and durations. |
| `test_live_canonical_intervals_coalesce_only_touching_same_run` | Contiguous data one interval per column; within-column holes and touching different suspension runs remain separate; seam-only merge correct. |
| `test_live_curve_and_fill_support_never_bridge_multiple_runs_in_one_column` | Force at least two separated runs into one column; entire curve and closed-fill min/max X strictly belongs to one mapped inset run; read/write independently checked. |
| `test_live_inset_handles_eight_fragment_subpixel_and_two_fragment_runs` | Eight-fragment tiny run, two-fragment run and unequal heights: finite monotone X in [L,U], unchanged maxima, repeated X legal and exact baseline closure. |
| `test_live_single_fragment_nonzero_run_draws_tick` | Exactly two support-edge-inset vertices at same height, closed fill; zero side emits neither. |
| `test_live_scale_maxima_reach_each_half_after_append_merge_and_tail` | Independent read/write maximum reaches its own half-height after all three updates; zero stays zero and negative/NaN heights absent. |
| `test_live_append_cost_counts_fragmented_output_and_cached_aggregation` | Execute every §8.7 numerical fixture against production paths: dense/fragmented 31-tail visits exactly `[28,29,30]`, promotion append visits `[32]`; validate/comparison counts separate. Assert exact E/R/V/fill counts, scale counts and geometry ceilings 54/36/388; full-prefix reaggregation, member-vector copying or collapsed fragmented runs must fail their named observation. |
| `test_live_rebuild_resize_and_unavailable_reset_preserve_honesty` | Exempt full rebuild/resize reestablish grouping/scale/support; collapsed area retains raw data; unavailable clears identity/state/gutters and cannot leave old stroke/fill. |

### 12.7 Age and exhaustive status — `tests/test_ui.py`, `tests/p3/test_collector_window.py`

| Exact test name | Required observation |
| --- | --- |
| `test_first_seen_age_uses_raw_thresholds_and_ceiling_in_every_unit` | All seven exact boundary vectors in §9.3 plus max(0, negative test delta); no float/truncation arithmetic. |
| `test_accepted_identity_includes_exact_selector_despite_same_snapshot` | Changed Window/same snapshot creates distinct identity/stamp; repeated same remote/local identity does not restamp, including after reapplication and while forward preparation is pending. |
| `test_age_uses_oldest_displayed_dependency_and_unknown_contributor` | Mixed ranking/share/series ages; defined PEAK included, undefined PEAK and unsupported/failed/undisplayed series excluded; any identity-less displayed contributor -> age unknown; no measurements -> hidden. |
| `test_live_status_initial_frame_pending` | No target/sample -> waiting; target/no frame -> exact loading history, neither claims live; no measured age. |
| `test_live_status_first_and_retry_details_pending_without_peak` | First pending and success->failure->pending retry with undefined PEAK -> loading measurements; retry restores no record/age for removed dependency. |
| `test_live_status_tree_selects_exactly_one_reachable_row` | For every case in the explicit table below call the production choice function once and assert exact row ID, text and label visibility plus independent age. Count zero queries/prepares/recovery writes from composition; the expected row, not an unconditional branch count, is the oracle. |
| `test_refusal_presentation_lifecycle_preserves_age_and_recovery_owners` | Execute all three numbered traces below with exact prepare-attempt/send counts, typed origin/Window/marker ownership and status/age at each step. |
| `test_live_status_unsupported_lifetime_and_unranked_subject_are_settled` | Ok-series/failed-share/undefined-PEAK lifetime -> measurements unavailable; file-only subject/nonempty ranking -> hidden status and panel reason, never indefinite loading/live. |
| `test_live_status_refusal_and_operational_error_precedence` | Exact A22/general WindowUnavailableError without prefix wins over operational error, both win over stall/pending/live as applicable; frozen origin/rethrow/frame preservation intact. |
| `test_live_status_early_retirement_expired_floor_still_busy` | Early retirement makes stall eligible; at expired floor with held worker still exact busy; no prepare/zero-second retry; label not cleared by explicit request. |
| `test_age_label_survives_busy_retry_refusal_and_idle_status` | Independent real QLabel text/visibility for busy, floor, refusal, updating and live/idle; PEAK-only measured branch; absent where no measurement exists. |
| `test_clock_only_age_update_issues_no_queries_or_reacceptance` | Age tick changes only age/status text; no query/prepare/Files/probe/admission from formatter/model notification, stamps and widget identities stable. |
| `test_native_composition_binds_one_age_clock_domain` | Compose the real `CollectorWindow` + `HistoryRouter` + `IodClient` with two independent clock providers separated by a fixed non-zero offset (recovery clock 300 s ahead of the age clock). Accept one answer and assert: age is exactly `oldest answer 0 s ago` at acceptance in that instant; one second of age clock later it is `oldest answer 1 s ago`; the offset never appears in the age. Advance only the recovery clock and assert the retry floor text changes while the age does not. Drive a client expiry and a router retirement deadline with the age clock and assert both compare in one domain: a BOOTTIME instant never satisfies a client deadline, and a monotonic instant never arms `_retry_after`. A control that injects one identical fake into every component must not be the only evidence. |

The following cases are the normative status oracle. Unless overridden: now=40_000_000_000 ns, history.view_state=`ready`, an applied frame/target/sample exist, ranking is nonempty, no refusal/PrepareRetry/stall/floor/local reader/pending frame/history work exists, both dependency states are absent with reasons None, and no displayed answer exists. `values` supplies an accepted share with first_seen=38_000_000_000 ns and has_displayed_values=True; `peak` supplies only current ranking provenance with that stamp and a defined PEAK, leaving has_displayed_values=False. All supplied identities/provenance are valid. Failed-state reasons are the literal tokens given. Each row makes one production choice call; `_update_overview_status` is checked with the same inputs against real label text/visibility. Compose/update send **0** queries, prepare **0** frames and change **0** recovery fields. A nonempty status is visible; age visibility is independently stated, including when the status is hidden.

| Case / concrete overrides | Expected row | Exact status text (visible unless empty) | Exact age text; visibility |
| --- | --- | --- | --- |
| S01: Shell_prepare launch refusal; values; history work pending | 1 | `Launch window disabled: device and file activity are not stored per launch.` | `oldest answer 2 s ago`; visible |
| S02: Direct_view launch refusal after cleanup; still-owned explicit PrepareRetry with `_prepare_error="appio: disk read failed"` | 1 | `Launch window disabled: device and file activity are not stored per launch.` | None; hidden |
| S03: Same shell refusal shape with reason unsupported_window and its exact over-long range text; values | 1 | `Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.` | `oldest answer 2 s ago`; visible |
| S04: Explicit PrepareRetry and `_prepare_error="appio: disk read failed"`; values and history work pending | 2 | `appio: disk read failed` | `oldest answer 2 s ago`; visible |
| S05: `_retry_after=now`, local_read_in_flight=True, history state `unavailable` (ordinary same-authority retirement at t=10 s); values | 3 | `history read has not completed (unavailable); the reader is still busy` | `oldest answer 2 s ago`; visible |
| S06: `_stalled=True`, reader=True, pending frame=True with pending_since=10 s, history state `loading`; no values | 3 | `history read has not completed (loading); the reader is still busy` | None; hidden |
| S07: `_retry_after=now+1_000_000_001`, reader=False, history state `unavailable` after same-authority retirement; values | 4 | `history read has not completed (unavailable); retrying in 2 s` | `oldest answer 2 s ago`; visible |
| S08: No applied frame, no target; separate arm target exists but no sample | 5a | `waiting for the first sample` | None; hidden |
| S09: No applied frame, target/sample exist, pending frame=True, history state `loading` | 5b | `loading history (loading)` | None; hidden |
| S10: values, history work=True for detail; history state `ready` | 6a | `updating (ready)` | `oldest answer 2 s ago`; visible |
| S11: values, pending frame=True, history state `loading` | 6a | `updating (loading)` | `oldest answer 2 s ago`; visible |
| S12: values, no work | 6b | `live` | `oldest answer 2 s ago`; visible |
| S13: Successfully empty applied ranking, no values; history work=True for Files | 6c | `no applications observed` | None; hidden |
| S14: share pending, series absent, undefined PEAK (first load or retry after failure) | 6d | `loading measurements (ready)` | None; hidden |
| S15: share pending, series failed with series_bad, undefined PEAK | 6d | `loading measurements (ready)` | None; hidden |
| S16: share failed share_bad, series failed series_bad | 6e | `measurements unavailable: share_bad` | None; hidden |
| S17: share absent, series failed series_bad | 6e | `measurements unavailable: series_bad` | None; hidden |
| S18: Lifetime series ok but chronology unavailable, share failed share_bad, undefined PEAK | 6e | `measurements unavailable: share_bad` | None; hidden |
| S19: File-only selected subject in nonempty ranking, both dependencies absent | 6f | Empty string | None; hidden |
| S20: peak only, share pending, no effective series | 6d | `loading measurements (ready)` | `oldest answer 2 s ago`; visible |
| S21: peak only, both dependencies absent | 6f | Empty string | `oldest answer 2 s ago`; visible |

Use the following case IDs S01–S21 as the required parameter IDs; they identify separate expected observations even when several select the same status row. The named tests above use these inputs: initial-frame-pending -> S08/S09; first-and-retry-details-pending -> S14 plus success/failure/pending sequence; tree -> all S01–S21; unsupported-lifetime/unranked -> S18/S19; refusal/operational precedence -> S01–S04; early-retirement-expired-floor -> S05 with an explicit B request still held by the reader gate (prepare count 0); age-label-survival -> S01/S05/S07/S10/S12/S21, plus S06/S08/S14 hidden-age controls; clock-only-age -> S12 at now=40 s then 41 s (`live` unchanged, age `2 s` then `3 s`, query/prepare/acceptance count 0). Refusal lifecycle uses the three traces below. Cost tests use §8.7's four exact counted fixtures and the grouping thresholds in §12.6. No named cost/status test is satisfied by a count with no corresponding input and expected output.

The values+work case must select 6a even though 6b's raw predicate is also true; pending+failed must select 6d over 6e; direct refusal plus PrepareRetry must select 1 over 2. Targeted mutations swapping those row priorities, choosing series failure first, or defining displayed values from an applied frame/unsupported lifetime series must fail the corresponding exact row/text assertion. Changing only a test-side branch counter supplies no evidence.

The named status tests also exercise these concrete production traces; timestamps are injected ns-clock seconds for readability, PEAK is undefined and all data quantities are as §12.5:

1. **Shell refusal -> tick/notification -> supported request/application.** Begin with accepted/displayed A-share stamped at t=0. At t=2 explicit show_window(launch) reaches one refused prepare attempt (0 query sends) and sets shell_prepare/launch. Status is the exact launch text and age `oldest answer 2 s ago`. At t=3 run the ordinary target tick with that deferred launch; the existing gate re-encounters the precheck (attempt count=2, supported acceptances=0, sends=0), preserves refusal and age becomes `oldest answer 3 s ago`. At t=4 refresh_measurements changes neither attempt/send count nor refusal and age is `oldest answer 4 s ago`. At t=5 explicitly request supported same-scope B: attempt count=3, accepted prepares=1, shell refusal None, row 6a `updating (loading)` with age `oldest answer 5 s ago`. Hold detail successor, finish/apply B at t=6: refusal stays None, row 6a `updating (ready)` with that pending work and retained A age `oldest answer 6 s ago`. At t=7 settle both new B dependencies with identities stamped at t=7, then row 6b `live` with `oldest answer 0 s ago`; this is a new accepted Window, not a restamp of A. Only the ordinary gate/application own recovery changes; refusal creates no floor/PrepareRetry.
2. **Operational failure -> named retry.** With A displayed/stamped t=0, explicit supported B fails at t=2 with OSError("disk read failed"): one attempt, no accepted prepare, typed PrepareRetry(B,explicit,2 s), row 2 `appio: disk read failed`, age `oldest answer 2 s ago`, no retirement floor. At t=3 ordinary tick and poll/drain leave the marker, attempts=1 and row 2/age `oldest answer 3 s ago`. At t=4 only show_window(B) is the named retry: attempts=2, accepted prepares=1 and marker/error cleared by that seam; row 6a `updating (loading)`, age `oldest answer 4 s ago`. A different-window explicit request is a separate supersession arm with 0 retry of the old B; detail notifications never clear the marker.
3. **Direct refusal -> supported adoption.** With A displayed, direct set_window(launch) at t=2 performs frozen Overview/Files cleanup, emits direct_view/launch, retires selection and makes age hidden; exact launch status remains through status-only tick and detail notification, with 0 refusal/detail query sends and no late old-record restoration. At t=4 direct supported adoption of A clears/emits None before building a supported model. Supply fresh valid identity-bearing share acceptance at t=4 (new lifetime/stamp); status row 6b `live` when all controlled work is settled, age `oldest answer 0 s ago`, refusal/error widget cleared. The frozen direct adoption's one ranking/series/totals observation per refresh is counted; a notification adds zero getter observations. The shell's `_displayed` preservation never reconstructs the directly cleared model before that adoption.

### 12.8 Exact slow-detail fixture and design coverage map

The remote liveness fixture is normative. At virtual t=0.0 s admit ranking, answer it at 0.2 s, and issue series at 0.2 s. Answer series exactly 4.95 s after issue, at 5.15 s. Issue totals immediately after series terminal settlement; answer it 4.95 s later, at 10.10 s. At 10.0 s begin the next forward frame preparation. Every individual wire request remains below its five-second timeout; totals' **queue/owner lifetime**, not its wire deadline, crosses the commit. Keep PEAK undefined. Assert every completed totals reply settles into its original requested Window and TOTAL/READ/WRITE become visible. Keep the replacement frame pending when totals arrives, assert the notification makes totals visible immediately, then complete/apply the replacement and assert the older measured record remains readable while its own newer detail is pending. The single transport slot prevents that replacement's uncached ranking from passing the still-held totals job; do not require an impossible second historical request on the wire to make the fixture pass. Run both separately named held-local arms: delivery after later frames applies proves completion ordering, and a detail held **inside worker computation** while a later frame completes/applies proves independent execution capacity (§§3.3, 12.2). Do not simulate the case by making one wire query exceed five seconds.

| Design §12 row | Required coverage here |
| --- | --- |
| 1 Settlement validation | §12.2 records/shape/supersession; §12.3 selector and actual decode/correlation positives/negatives |
| 2 Failure clearing/no retry resurrection | §12.2 real accessor + view failure-order and retry tests, undefined PEAK and successful independent other dependency |
| 3 Dependency independence | §12.5 individual DEVICE, bars, residual, task/device lanes, undefined device total/residual, lifetime and both mixed-endpoint directions |
| 4 Continuous-commit liveness | §12.8 exact 0.0/0.2/5.15/10.0/10.1 schedule and held-local test |
| 5 Certification | §12.3 same/older-cursor cache and fallback; §12.4 every local mutation and three distinct bracket schedules, unchanged quiescence |
| 6 Scheduling/slots | §12.3 real wire count, held-timeout/retirement and continuation/coverage; §12.2 frame readiness |
| 7 Chart decision table | §12.6 eleven separately named row tests plus lowered/removed peak, None/zero, suspend-then-append, setter->append and contradictions |
| 8 Chart geometry/cost | §12.6 multiple runs/column, eight/two fragments, unequal heights, closed fill, monotone inset, tick, canonical dense/fragmented intervals and bound-copy accounting |
| 9 Scale | §12.6 exact per-side maxima after append/promotion/tail and zero controls |
| 10 Age | §12.7 exact ceiling boundaries, selector/snapshot identity, repeated local application, oldest contributor and unknown identity |
| 11 Status and independent age | §12.7 exhaustive reachable tree, named initial/first/retry/lifetime/unranked/refusal/operational/early-retirement cases and real separate QLabel |
| 12 Backward/range/subject/boot | §12.2 pre-render reset including backward-then-forward and both distinct ranges; adapter parity in §12.5 |

### 12.9 Retained regression controls and later implementation gates

1. Preserve existing frozen tests for exact A22 launch/range reasons and precedence, direct refusal cleanup, file-only subject/union selection, unavailable versus zero, None versus zero peak, signed residuals, DEVICE completeness, Files served/requested windows and served_gap_count None. Only tests asserting the expressly amended step/bin/exact-part/pending-error/endpoint-trigger mechanisms may change those expectations; keep their substantive honesty assertions.
2. Keep the recovery-spec tests for same history key, queued local read gates, 30-second stall/floor arithmetic, explicit deferred/retry ownership, probe-generation arbitration, sole `_refresh_after_apply` writers, operational prepare failures and Files transient retirement. Add source/detail assertions without rewriting those controls to weaker fakes.
3. After implementation run the focused new/extended inventory, then `python -m pytest` under the repository offscreen environment. Run `python -m pytest --collect-only -q` to verify exact named test inventory and parameter cases; an omitted row is not satisfied by a broad green suite. Report `preserve_exact` arms as preservation controls, with separate discriminator outcomes; baseline-green availability is never a fabricated initial red. Do not run these implementation gates during specification drafting.
4. Extend the existing development-only typecheck gate to `python -m mypy --follow-imports=skip --ignore-missing-imports src/appio/history_types.py src/appio/history_validation.py src/appio/history_router.py src/appio/iodclient.py src/appio/ui/render_model.py src/appio/ui/charts.py src/appio/ui/theme.py src/appio/ui/main_window.py src/appio/ui/files_view.py src/appio/ui/collector_window.py`. Keep runtime dependencies unchanged; record the actual checker version in later evidence.
5. With passing targeted controls, falsify the mechanisms in a disposable copy: bypass external/request/selector validation (including before cache insertion); **remove only the unscoped process-mapping Coverage-value predicate while leaving AppKey, request, kind and every other check enabled** (the value-only arm must fail, the valid mapping control must pass, and the restored rerun must pass; the exercised boundary is the shared validator invoked by both the client pre-cache step and router settlement, so neither can mask the mutation); tie detail lifetime to generation; clear records in prepare; preserve a record on failure or restore it on pending; drop frame authority retirement; share the frame executor with detail; bypass a local outcome's binding; release transport on Future.done; omit seq from cache or restamp answered_source; observe only primary/latest rows, hold observer transaction or omit registration epoch; trigger on endpoint alone; let poll enter the deferred gate; omit typed model metadata or use share tier for chart identity; fail to reset the exact adapter on authority notification; reaggregate history on append; copy member-index vectors; merge runs by envelope; retain lowered peak/scale; truncate age; reverse status priorities or concatenate age into frozen texts. Every claimed kill names the exact discriminator parameter/assertion, with a passing control and restored rerun; import/collection failure is not a kill. Baseline-green preservation arms may not be credited for new retention/settlement/provenance mechanisms.
6. Inspect offscreen grabs for a mirrored streaming curve, separate readable age, exact visible status/reason text and persistent Overview/Files navigation. Geometry and byte/state tests remain the nonvisual evidence; a screenshot alone cannot prove no bridge, scheduler serialization or source certification.
7. Before integration, compare changed symbol/record inventories against §11's eighteen amendments and report any missing evidence as not run. No daemon/service manipulation, host transition, live privileged acceptance or unrelated earlier gate is introduced by this specification.

Implementation sequencing: establish neutral records and shared selector validation first, then the client pre-cache/association/transport seams and typed local worker outcomes/independent capacity. Implement authority retirement and the single writer before wiring router/view notifications. Add model metadata and adapter identity/reset before timeline append callers, and typed refusal state before switching all status writers to the composer. Only then run the liveness, malformed-retry, direct-model, deferred-context and status/cost discriminator inventory. Do not demonstrate AC-4 with a delivery-only fixture while the executors still share capacity.

The following are mandatory exact mutation-to-discriminator mappings for review findings 10–11 (additional mutations above remain required where applicable):

| Mutation | Distinguishing test arm and assertion; preservation evidence excluded |
| --- | --- |
| Clear share record on forward prepare / treat pending exact B as error | `test_share_outputs_survive_series_failure_or_pending[retained_A_pending_B]` and `test_undefined_residual_does_not_remove_device_total_or_bars[retained_A_pending_B]`: TOTAL=22/READ=17/WRITE=5/DEVICE=14 as applicable, exact A measured Window/stamp/disclosure while B pending. `preserve_exact` does not kill these mechanisms. |
| Retain failed dependency or restore its record on pending | `test_share_failure_keeps_independent_task_lane[failure_pending]`: all share outputs unavailable, real retained share None, A-series bytes/Window/stamp still displayed. `test_lifetime_detail_keeps_totals_without_chronology[failure_pending]`: cleared TOTAL/READ/WRITE/DEVICE and no restored age. |
| Use share coverage/tier for series lanes or omit the series-specific reason | `test_series_device_incomplete_only_withholds_device_lanes[series_coverage_reason]`: zero device lanes/more count, exact `; device lanes: incomplete`, series metadata incomplete, share DEVICE=14/bars unchanged. `test_main_window_uses_data_identity_for_task_and_device_appends`: actual identity tuple contains rollup_60s while share tier is raw. Numeric `preserve_exact` availability alone is not a new-reason kill. |
| Relabel retained A as requested B / restamp on display | The retained_A_pending_B arms above and `test_mixed_endpoint_inputs_keep_independent_outputs`: `provenance.measured_window == A`, first_seen unchanged, exact A disclosure; mixed endpoints have zero lanes and `device measurements have different endpoints`. |
| Full-prefix reaggregation on tail or append | `test_live_append_cost_counts_fragmented_output_and_cached_aggregation`: aggregation trace exactly `[28,29,30]` / `[32]`, **no** earlier member visit; validation/comparison counters remain separate. |
| Promotion copies membership arrays / collapses runs to fit W | `test_live_grouping_is_minimal_power_of_two_and_promotes_bounds`: two integers per bound, reads<=9/writes<=5, no copied member indices. Cost test fragmented arm: E=31,R=62,V=124, geometry<=388, independent run-support assertions. |
| Wrong status precedence / count all raw predicates instead of selecting the expected row | §12.7 values+work ->6a, pending+failed ->6d, direct refusal+PrepareRetry ->1, dual failed ->share_bad. `test_live_status_tree_selects_exactly_one_reachable_row` asserts actual row/text/visibility with zero query/prepare/recovery writes; changing a branch counter cannot satisfy it. |

All of this is prospective implementation evidence. The review's baseline observations are preservation evidence only; no full application suite, new inventory test or future API test is claimed to have run during this revision.

## 13. Specification decisions and exclusions

The mechanisms chosen here remain the neutral immutable records and local canonical payload digest; explicit scope-selection/request/retained/provenance/drain methods; monotonic injected router clock; a single typed settlement writer and immutable token; worker-only selected local detail copies; source registration epochs and observer brackets; an in-process client transport observation with expired tombstones and a guarded retirement deadline; optional structural retained capability and exact-only adapter; per-dependency model fields/disclosures; opaque caller chart identities, raw equality and consecutive group/run caches; finite per-run polyline/fill geometry; and one status/independent age observation. They implement design decisions without changing any accounting result or wire field.

The following decision register records the review corrections. Each entry selects one mechanism; its owning sections and tests are normative.

| Decision / review finding | Chosen mechanism and owning sections |
| --- | --- |
| D01 / 1 | Owner-thread authority reconciliation retires an affected frame exactly once through router-unavailable/shell-retirement, before ignoring old completions; ordinary sequence/counter advances coalesce (§§3.2–3.4, 6.1). |
| D02 / 2 | Immutable, associated local frame/part/Files outcomes and origin-tagged settlement inputs normalize into the same publication writer; only remote inputs require client transport association (§§3.3–3.4, 5.4). |
| D03 / 3 | A dedicated single-worker frame executor is independent of the bounded detail/Files executor; retired jobs retain their capacity charges until drained (§3.3). |
| D04 / 4 | Add typed SeriesMetadata plus explicit model requested Window and external key, with defaults and a defined legacy-direct mode; timeline identities use typed series metadata (§§2.3, 7.1, 8.2). |
| D05 / 5 | A shared pure request/selector validator runs before client cache insertion and again at settlement; owner-specific acceptance remains solely in settlement (§§1.2, 5.3–5.4). |
| D06 / 6 | Validate series task/residual chronology and device ordering separately, with nonoverlap per device and simultaneous devices allowed (§5.4). |
| D07 / 7 | Ordinary apply calls may pass the existing deferred gate; poll calls use an explicit drain/apply-only context (§6.3). |
| D08 / 8 | Typed refusal presentation state has explicit ownership/window/lifecycle; operational status is derived directly from the existing still-owned PrepareRetry (§9.1). |
| D09 / 9 | An optional exact-provenance method and UI-owned authority notifications/reset define adapter stamps and unchanged-lifetime retirement (§6.2). |
| D10 / 10 | Mark baseline-green dependency arms as preservation controls; require separate retained-window/failure/provenance discriminators and exact mutation-to-assertion mappings (§§12.5, 12.9). |
| D11 / 11 | Fixed chart fixtures count aggregation, bounds, intervals and vertices separately; a typed status result is checked against explicit expected rows/text/visibility (§§8.7, 9.2, 12.6–12.7). |

The collector's ten-second cadence, DEVICE whole-window attribution rule, retention/tiering, store schema/writers, concrete QueryService, ownership/receipts, daemon protocol, Files rendering/paging/accounting, scope-control availability, helper/consent behaviour, session and display-manager lifetime remain outside this work. No reboot, shutdown, power, sleep, service restart, process-tree signal, or host repair is needed to draft or implement these contracts.
