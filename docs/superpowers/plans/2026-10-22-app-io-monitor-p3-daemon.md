# App I/O Monitor P3 Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax; leave the plan unchanged during execution.

**Goal:** Deliver an optional root `appio-iod` service, secure read-only IPC and durable history, explicit collector ownership, and authenticated enable/disable and activation flows for the existing unprivileged GUI.

**Architecture:** Reuse the delivered P1/P2 sampler, transactional store, ownership rules and typed query contracts. The daemon owns `/var/lib/appio/iod.db`; the GUI reads daemon history over a bounded Unix socket and collects locally only after proving the service absent/inactive and acquiring its per-UID lease. A small fixed-verb polkit helper owns system changes, activation probes and history exports; all privileged operations have injected adapters so ordinary verification runs as an unprivileged user.

**Tech Stack:** Python >=3.14, PyQt6>=6.6, standard-library `socket`/`selectors`/`sqlite3`, Linux `SO_PEERCRED`, systemd, polkit, pytest>=8 and pytest-qt>=4.4. No new runtime Python dependencies.

## Global Constraints

### Authorities and scope

- Read `docs/superpowers/specs/2026-10-21-app-io-monitor-design.md` (revision 5), especially §§3, 4.2, 5, 7, 9–11 and 14. The ratified A01–A22 contracts in `docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md` govern the existing data model; A11/A22 operator rulings take precedence over contradictory design prose.
- P3 includes service/polkit runtime artifacts, daemon history and export, framing/security/query/reconnect, handover, delegation/activation and the single GUI owner switch. P4 owns BPF/per-file production. P5 owns Overview charts, the Files table, tray, distributable packages and installers. P3 may expose an unavailable Files entry and a settings deep link; it must not implement a Files table or a BPF loader.
- Python `requires-python = ">=3.14"`; runtime dependency `PyQt6>=6.6`; test dependencies `pytest>=8` and `pytest-qt>=4.4`. Keep the `src/appio` layout. Use SQLite >=3.37.0 with JSON functions and the existing schema-v1 STRICT tables; no schema fork or migrations added merely for P3.
- Preserve all four counters in order `(rchar, wchar, read_bytes, write_bytes)`, integer range `[0, 9223372036854775807]`, boot-scoped `CLOCK_BOOTTIME` nanoseconds, half-open intervals, signed device residuals, nullable unavailable values, exact A22 errors and coverage overlays. Never prorate a cumulative counter delta or infer a measured zero from missing evidence.
- `AppIoStore.record_tick(frame, next_state)` owns its outer `BEGIN IMMEDIATE`; receipt savepoints join that transaction. Publish sampler state, sequence and socket snapshots only after commit. Rejected/stale frames mutate nothing; A21 permits only the separately committed overflow diagnostic after overflow rollback.
- Reuse `appio.durable.HandoverState`, `ProcessCheckpoint`, `Credit`, `DeviceBaseline`, `canonical_json` and `decode_handover(text, expected_boot_id, expected_cutoff)`. The durable JSON format remains version 1. Its missing per-process rate/reset provenance requires conservative `rate_unknown`/`reap_incomplete`; a successful transfer cannot invent that provenance.
- Existing `LiveCollector` accepts only `source="gui"` despite its annotated Literal. Task 5 must deliberately extend this check; constructing it with `source="iod"` currently fails. Its boot-row source check also needs attention when opening an export that contains the other source. Do not claim daemon reuse works without those changes.
- `AppKey` is the external identity. Database-local `app_id`, `launch_id` and `path_id` never cross stores or the wire as identifiers. A GUI adapter may allocate its own positive presentation IDs, translate them back to AppKey, and remap launch/path surrogates on import.
- Preserve `tests/acceptance/test_no_later_scope.py` byte-for-byte. Direct use of `AppIoWindow` and the legacy P1/P2 fixture/`--once` path must not import `appio` modules whose name components begin with `daemon`, `iod`, `socket`, `bpf`, `polkit`, `privilege`, `setup`, `tray` or `packaging`, or import socket/polkit libraries indirectly. Add the P3 window/composition separately and load it lazily only in the interactive entry path.

### Execution, ownership and verification

- Every task states `depends_on`, `lane` and `sequence`. A lane runs sequentially; dependencies must be integrated before its consumer starts. Different lanes may run concurrently only when their complete file ownership sets are disjoint. Listed Create/Modify/Test paths are the ownership allowlist; no task may edit a dependency's files to adjust an interface silently.
- Task 6 alone changes the daemon console-script section of `pyproject.toml`; no other task changes that file. Task 13 alone changes `src/appio/__main__.py`. Task 4 alone changes the store's handover API, Task 5 alone changes `live.py`, and Task 14 adds acceptance tests/documentation only. Resolve interface mismatches through the producing task's review before integrating the consumer.
- Every ordinary test runs without root, an actual system/user manager, polkit agent, BPF, live host PIDs or writes outside its temporary tree. Inject peer credentials, account/group lookup, effective UID, trusted-file metadata, proc/cgroup roots, clocks, command runner, fsync/rename failure points and session state. An actual AF_UNIX socketpair is fine; tests must inject root credentials instead of requiring a real root peer.
- The helper's temporary root and fake effective UID exist only in its Python dependency object. Production argv remains exactly one verb: no `--root`, path, username, unit-name or test-mode options, and no environment variable that enables those seams.
- For each task: add the shown test and listed adversarial vectors, run the exact focused command and observe a missing-behavior failure, implement, rerun and then commit only its owned files. A missing import can establish the initial red phase, but also break the stated mechanism after green to prove the decisive assertion fails. Never edit this immutable plan to tick boxes.
- Focused test command prefix throughout is `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q`. Use explicit filenames after that prefix. No privileged checks run under this command. All expected PASS results mean no failures, rather than a fixed test count.
- Real-root integration is opt-in acceptance, documented in Task 14 and outside the default pytest discovery path. It requires an expendable login session and explicit selection of the destructive manager-restart choice; unit tests must never restart a real manager or reboot.

### Design decisions and questions to preserve for review

- **D1 — query command conflict.** Design §4.2 and N2 require historical `query`; §5 says the only requests are `status`/`subscribe`. This plan follows §4.2/N2 with exactly three read-only request types: `status`, `subscribe`, `query`. `query` accepts typed data selectors only, never SQL, paths, shell text, collector controls or setup commands. Review question: may §5's allowlist be corrected to include this read-only request?
- **D2 — activation probe filesystem conflict.** The prescribed tmpfs direct write cannot prove block-device `io.stat` accounting. Use an isolated 16 MiB direct-write probe on a verified supported block-backed filesystem under the fixed `/var/lib/appio/probes/<uid>/` root, with a root-owned non-writable parent and caller-owned pre-created file. A tmpfs/unsupported/unprovable target yields `delegation present, accounting inactive`, never success. Review question: approve this fixed block-backed target instead of `<tmpfs>/p`? Keep the same 5% check and record measured/expected bytes and device identity.
- **D3 — handover transport and authority.** The design defines durable baselines but supplies no client-to-root transfer/acknowledgement message or trusted cross-database transaction. Keep the public socket read-only. Implement exact handover installation when a validated outgoing checkpoint and complete preceding ownership chain are available (daemon-to-GUI via helper export where that evidence exists, and in-process tests), and conservative unclean transfer otherwise. In particular, GUI-to-daemon enable establishes daemon-owned fresh baselines and exposes any unmeasured gap/uncertain reap provenance; it does not upload GUI-controlled counters into the root database or advertise lossless continuity. Review question: is this explicit degraded enable acceptable, or is a separately authenticated checkpoint-transfer contract required for clean GUI-to-daemon handover? Do not add such a privileged ingestion surface during this plan without a design ruling.
- **D4 — policy values not pinned by the design.** This plan chooses a 4-byte unsigned network-order length prefix, 1 MiB inbound payload cap, 256 KiB outbound payload cap, 512 KiB per-client queued-byte cap, 64 clients, one in-flight historical query/client, a 200 ms historical-query execution budget checked before/after domain calls, by SQLite progress and by streaming loops, and a 2 s incomplete-frame deadline. Two consecutive logical snapshots still pending transmission disconnect that client. Review question: are these concrete budgets acceptable? Enforce both row/point limits and byte limits; never truncate an accounting result silently.
- **D5 — machine-wide daemon and user ownership.** The root daemon is shared; the design grants appio-group members read access to all daemon data and permits enable/disable through `auth_admin`. Local GUI collection uses a per-UID lease so a second GUI cannot become a second writer. Review question: confirm that any authorized administrator may stop the machine-wide daemon and that exports contain the same global dataset members can query. Do not invent per-UID data filtering that would change totals.
- These questions accompany a concrete conservative plan. Unresolved D2/D3 outcomes remain visible limitations in acceptance evidence; passing mocked seams must never be reported as proof of working host accounting or clean bidirectional handover.

### File responsibilities and dependency graph

| Task | Lane / sequence | depends_on | Owned implementation surface | Tier |
| --- | --- | --- | --- | --- |
| 1 | protocol / 1 | [] | `ipc_protocol.py`, `ipc_models.py` | Advanced |
| 2 | transport / 1 | [1] | `ipc_security.py`, `ipc_queue.py` | Advanced |
| 3 | history / 1 | [1] | `iod_history.py` | Capable |
| 4 | ownership / 1 | [] | `handover.py`, store handover methods | Capable |
| 5 | ownership / 2 | [4] | `live.py` source/resume seams | Advanced |
| 6 | daemon / 1 | [1,2,3,5] | `iod.py`, service unit, `pyproject.toml` | Capable |
| 7 | privilege / 1 | [] | `setup_boundary.py`, fixed helper shim, polkit policy | Advanced |
| 8 | privilege / 2 | [7] | `setup_files.py`, `setup_delegation.py` | Advanced |
| 9 | activation / 1 | [8] | `activation.py` | Advanced |
| 10 | privilege / 3 | [6,8,9] | `setup_service.py`, `history_export.py` | Capable |
| 11 | client / 1 | [1,2,3,4,6] | `iodclient.py` | Advanced |
| 12 | ownership / 3 | [4,5,10,11] | `collector_owner.py`, `history_router.py` | Capable |
| 13 | gui / 1 | [9,10,12] | `setup_client.py`, P3 window, interactive entry | Advanced |
| 14 | acceptance / 1 | [1,2,3,4,5,6,7,8,9,10,11,12,13] | new P3 acceptance tests and opt-in runbook | Advanced |

The exact test paths and modification anchors are repeated in each task. New directories need no separate scaffolding task. Existing source, specs and the P1/P2 plan are read-only unless a task explicitly names a source modification.

## Task 1: Define protocol-1 framing and typed read-only messages

**Implementer tier:** Advanced

**Scheduling:** `depends_on: []`; `lane: protocol`; `sequence: 1`.

**Files:**

- Create: `src/appio/ipc_protocol.py`
- Create: `src/appio/ipc_models.py`
- Test: `tests/p3/test_ipc_protocol.py`

**Interfaces:**

- Consumes: existing `Window`, `validate_window`, `validate_sqlite_integer`, metric/sort/direction Literals and AppKey validation; no Qt, daemon or setup imports.
- Produces: `encode_frame(message: dict, *, max_bytes: int = 262144) -> bytes`; `FrameDecoder(*, max_bytes: int = 1048576)` with `feed(data: bytes) -> tuple[dict, ...]` and `eof() -> None`; `ProtocolError(ValueError)` with string `reason`.
- Produces: `validate_request(message: dict) -> Request`, a frozen record with `request_id: str`, `type: Literal["status","subscribe","query"]`, `since: StreamCursor | None`, `query: QueryRequest | None`; `StreamCursor(boot_id: str, iod_epoch: int, seq: int)`; `Capabilities(proc_all: bool, dev_stat: bool, file_io: bool, bpf_loaded: bool)`.
- Produces: `QueryRequest(kind: str, window: Window, app_key: str | None = None, metric: str = "total_bytes", sort: str = "bytes_total", direction: str = "desc", search: str | None = None, cursor: str | None = None, limit: int = 500, coverage_kind: Literal["process","file","unknown_owner"] = "process")`; kind allowlist `ranking`, `series`, `files`, `coverage`, `totals`. `coverage_kind` selects only the coverage result and is default-only on other query kinds; `totals` returns typed disk share for one AppKey. No other selector dispatches.
- Produces: `validate_server_message(message: dict) -> dict`, validating `status`, `snapshot`, `gap`, `result`, `error` envelopes before a consumer sees data. Every envelope carries `protocol=1`, a type, and request correlation where applicable; snapshot identity is `(boot_id, source="iod", iod_epoch, seq)`.

**Threat closed:** malformed or oversized local messages must not allocate unbounded memory, exploit JSON coercion, smuggle duplicate keys or extend the root service's verb surface.

- [ ] **Step 1: Write the failing test**

```python
import struct
import pytest
from appio.ipc_protocol import FrameDecoder, ProtocolError, encode_frame
from appio.ipc_models import validate_request


def test_fragmented_frames_are_not_newline_delimited():
    first = {"protocol": 1, "type": "status", "request_id": "a"}
    second = {"protocol": 1, "type": "status", "request_id": "b"}
    decoder = FrameDecoder()
    wire = encode_frame(first) + encode_frame(second)
    assert decoder.feed(wire[:3]) == ()
    assert decoder.feed(wire[3:]) == (first, second)
    decoder.eof()


def test_oversize_is_rejected_from_header_without_waiting_for_body():
    decoder = FrameDecoder()
    with pytest.raises(ProtocolError, match="frame_too_large"):
        decoder.feed(struct.pack("!I", 1048577))


@pytest.mark.parametrize("verb", ["enable", "disable", "exec", "handover"])
def test_root_mutation_verbs_never_enter_dispatch(verb):
    with pytest.raises(ProtocolError, match="unsupported_type"):
        validate_request({"protocol": 1, "type": verb, "request_id": "a"})
```

Add byte-by-byte fragmentation, coalescing, UTF-8 splits, zero length, invalid UTF-8, duplicate object keys at every nesting level, NaN/Infinity, top-level arrays, EOF in header/body, boolean integer fields, protocol other than integer 1, unknown keys and invalid AppKeys/windows. Subscription `since` requires boot identity; a legacy epoch/seq-only cursor receives an explicit gap/resync response, never cross-boot replay. Disabled launch windows use only `kind`/boot on the wire and are rejected with the exact A22 error before any ID lookup; an internal placeholder launch ID may construct the already-disabled `Window`, but no database launch surrogate is transmitted. Request IDs are nonempty UTF-8 strings <=64 bytes without NUL. Search is <=256 UTF-8 bytes; cursor <=512 bytes. Bound JSON nesting to 32 levels and aggregate container members to 16384, with bounded parse-failure handling for `RecursionError`.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_ipc_protocol.py`

Expected: FAIL because the protocol modules do not exist.

- [ ] **Step 3: Write the minimal implementation**

Use `struct.Struct("!I")`, strict UTF-8 and `json.loads` with duplicate-key detection and rejected nonfinite constants; reject bool where an integer is required. Validate a complete message before dispatch. In the incremental decoder, consume each four-byte header and check its length *before* retaining body bytes; process one frame at a time so a large concatenated `feed()` does not duplicate all input into an internal buffer.

```python
HEADER = struct.Struct("!I")
MAX_INBOUND = 1 << 20
MAX_OUTBOUND = 256 << 10


def encode_frame(message, *, max_bytes=MAX_OUTBOUND):
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    if not payload or len(payload) > max_bytes:
        raise ProtocolError("frame_too_large")
    return HEADER.pack(len(payload)) + payload
```

Define exact per-type key sets in `ipc_models.py`; dispatch through a literal lookup table. Snapshot fields include the design's timestamps, capabilities, procs/devs/files and coverage; P3 always sends `files=[]`, `file_io=False`, `bpf_loaded=False`, with explicit file-source-unavailable state. Status adds `enabled`, `answering`, `pending_activation`, ownership start and committed high-water cursor. Gaps carry boot/epoch identity, half-open monotonic bounds when known, previous/current cursors and a reason; unknown bounds remain null and coverage is incomplete. Error replies carry a bounded reason/message, never tracebacks or unrestricted stderr from the root process. Keep durable JSON encoding separate: wire timestamps may contain a finite float, while durable handover uses the existing canonical encoder.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Temporarily change the header limit check to `>` 2 MiB: the oversized-header test must fail without sending a body. Restore the check before commit.

- [ ] **Step 5: Commit**

```bash
git add src/appio/ipc_protocol.py src/appio/ipc_models.py tests/p3/test_ipc_protocol.py
git commit -m "feat(ipc): define bounded protocol-1 read-only messages"
```

## Task 2: Verify Unix peers and bound slow-client queues

**Implementer tier:** Advanced

**Scheduling:** `depends_on: [1]`; `lane: transport`; `sequence: 1`.

**Files:**

- Create: `src/appio/ipc_security.py`
- Create: `src/appio/ipc_queue.py`
- Test: `tests/p3/test_ipc_security.py`
- Test: `tests/p3/test_ipc_queue.py`

**Interfaces:**

- Consumes: Task 1 `encode_frame(message: dict, *, max_bytes: int = 262144) -> bytes`, `ProtocolError.reason`, and `StreamCursor(boot_id: str, iod_epoch: int, seq: int)`.
- Produces: `Peer(pid: int, uid: int, gid: int)`; `read_peer(sock: socket.socket) -> Peer` using `SO_PEERCRED`; `authorize_client(peer: Peer, *, appio_gid: int, supplementary_groups: Callable[[int], frozenset[int]]) -> bool`; `require_root_server(peer: Peer) -> None` raising `PeerRejected(PermissionError)`.
- Produces: `verify_runtime_paths(path: Path, *, metadata: Callable[[Path], object], appio_gid: int) -> None`, checking `/run/appio` root:appio 0750 and socket root:appio 0660 without following symlinks. Metadata injection is test-only.
- Produces: `ClientQueue(max_bytes: int = 524288, max_pending_snapshots: int = 2)` with `push(frame: bytes, *, snapshot: StreamCursor | None = None) -> None`, `peek(max_bytes: int) -> memoryview`, `consume(sent: int) -> None`, `queued_bytes: int`, `closed: bool`. Capacity/second pending snapshot raises `SlowClientError` and clears only that client's queue.

**Threat closed:** filesystem permissions alone must not allow unauthorized clients or a forged non-root server; an authorized client that stops reading must not stall collection or grow root memory without bound.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from appio.ipc_models import StreamCursor
from appio.ipc_security import Peer, PeerRejected, authorize_client, require_root_server
from appio.ipc_queue import ClientQueue, SlowClientError


def test_supplementary_membership_and_root_server_are_independent():
    peer = Peer(pid=73, uid=1000, gid=1000)
    assert authorize_client(peer, appio_gid=987,
                            supplementary_groups=lambda uid: frozenset({987}))
    assert not authorize_client(peer, appio_gid=987,
                                supplementary_groups=lambda uid: frozenset())
    with pytest.raises(PeerRejected):
        require_root_server(peer)
    require_root_server(Peer(pid=74, uid=0, gid=0))


def test_partial_send_does_not_reset_unread_snapshot_count():
    queue = ClientQueue(max_bytes=524288)
    queue.push(b"abcdef", snapshot=StreamCursor("boot", 1, 0))
    queue.consume(1)
    with pytest.raises(SlowClientError):
        queue.push(b"next", snapshot=StreamCursor("boot", 1, 1))
    assert queue.closed
    assert queue.queued_bytes == 0
```

Add root client, primary-group member, account missing, lookup failure, PID credential races, invalid `SO_PEERCRED` size, non-root/forged socket path, symlink parents and supplementary group failure. Never accept the server's own process group list as the peer's list. Test exact byte boundary including four-byte prefixes, queue accounting after partial writes, complete drain resetting the snapshot count, query-only queue flooding and one slow client beside one healthy client.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_ipc_security.py tests/p3/test_ipc_queue.py`

Expected: FAIL on missing modules.

- [ ] **Step 3: Write the minimal implementation**

Read kernel peer credentials once at accept/connect. Root is allowed; otherwise require the peer's primary appio group or resolve its UID with `pwd.getpwuid` and `os.getgrouplist` on that account. Failure denies access and records a bounded refusal log. New group membership takes effect for new authenticated connections; do not authorize a claimed UID from JSON. Client verifies both path metadata and root peer UID before decoding status or historical data.

```python
if self.queued_bytes + len(frame) > self.max_bytes:
    self.disconnect("queue_budget")
if snapshot is not None and self.pending_snapshots + 1 >= 2:
    self.disconnect("slow_client")
```

Keep offsets into immutable frames; decrement bytes by the actual nonblocking send count, and clear a snapshot only when its complete frame is drained. Define `disconnect` to clear retained buffers and raise `SlowClientError`. Queue limits apply to status/results/errors as well as snapshots; error reporting cannot bypass the budget. The service event loop must limit socket reads/writes per scheduling turn, addressed in Task 6.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Replace supplementary group lookup with an empty set and prove its positive assertion fails; disable the snapshot counter and prove the partial-send test fails. Restore both mechanisms.

- [ ] **Step 5: Commit**

```bash
git add src/appio/ipc_security.py src/appio/ipc_queue.py tests/p3/test_ipc_security.py tests/p3/test_ipc_queue.py
git commit -m "feat(ipc): authenticate peers and enforce client queue budgets"
```

## Task 3: Serve bounded historical queries without changing query semantics

**Implementer tier:** Capable

**Scheduling:** `depends_on: [1]`; `lane: history`; `sequence: 1`.

**Files:**

- Create: `src/appio/iod_history.py`
- Test: `tests/p3/test_iod_history.py`

**Interfaces:**

- Consumes: Task 1 `QueryRequest(kind, window, app_key=None, metric="total_bytes", sort="bytes_total", direction="desc", search=None, cursor=None, limit=500, coverage_kind="process")` and protocol result/error envelopes; existing `QueryService(connection)` methods `ranking(window, metric)`, `series(app_id, window)`, `disk_share(app_id, window)`, `file_table(app_id, window, sort, direction, search)`, `file_coverage(app_id, window)`, `unknown_owner_file_coverage(window)` and exact typed `WindowUnavailableError.reason`/message.
- Produces: `QueryLimits(max_points: int = 4000, max_file_rows: int = 500, max_payload_bytes: int = 262144, max_scan_rows: int = 50000, timeout_ns: int = 200000000)`; `HistoryRpc(connection: sqlite3.Connection, *, limits: QueryLimits = QueryLimits(), clock_ns: Callable[[], int] = time.monotonic_ns)` with `execute(request: QueryRequest, *, request_id: str) -> dict`.
- Produces: `to_wire(value: object, *, app_keys: Mapping[int, str]) -> object` and `from_wire(value: object, *, app_ids: Callable[[str], int], result_kind: str) -> object`. These explicitly translate typed P1 results without exposing surrogate IDs or dropping coverage, None, signed residuals or exact integer counters.

**Threat closed:** authenticated readers must not submit SQL or cause unbounded root-side history scans, allocations, result frames or concurrent work; cursor manipulation must not change the requested scope or pagination order.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
from appio.contracts import LAUNCH_WINDOW_MESSAGE, Window
from appio.ipc_models import QueryRequest
from appio.iod_history import HistoryRpc
from appio.migrations import migrate_empty


def test_remote_launch_rejection_precedes_unknown_app_lookup():
    connection = sqlite3.connect(":memory:")
    migrate_empty(connection)
    reply = HistoryRpc(connection).execute(
        QueryRequest(kind="series", window=Window(kind="launch", boot_id="b", launch_id=99),
                     app_key="exe:/missing"), request_id="q1")
    assert reply["type"] == "error"
    assert reply["reason"] == "launch_attribution"
    assert reply["message"] == LAUNCH_WINDOW_MESSAGE
    assert "app_id" not in str(reply)
```

Add a fully populated small store and compare every returned numeric, optional and coverage field with `QueryService`. Cover negative residuals, device unavailable, lifetime/exact historical boot, expired tiers, file-source-unavailable, all sorting tie keys, NFC search, AppKey mapping with deliberately different IDs in two databases, cursor tampering and cursor reuse after the underlying snapshot changes. A >4000-point series must receive `query_too_large` without silent resampling or substitution. A 501-app ranking must use pages <=500 with the original full sort order; file requests remain explicitly unavailable in P3, with the single unknown-owner coverage result per window. Reject a requested page limit above 500 before dispatch. Test >256 KiB serialization, deadline interruption and that rejection precedes a large `fetchall()`/domain-list allocation.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_iod_history.py`

Expected: FAIL because `iod_history` is missing.

- [ ] **Step 3: Write the minimal implementation**

Validate window/enum/search before app lookup, call the existing A22 precheck, then resolve AppKey to a local app ID inside one explicit read transaction. `QueryService._snapshot()` joins that transaction, so bounded preflight, query and serialization see identical rows. Do not mutate the schema or duplicate metric/coverage formulas.

```python
connection.execute("BEGIN")
try:
    connection.set_progress_handler(check_deadline, 1000)
    # Validate scope, count relevant input rows with bounded/indexed SELECTs,
    # resolve AppKey, and delegate to the existing typed query methods.
    # Encode with the Task 1 cap before returning any bytes to the event loop.
finally:
    connection.set_progress_handler(None, 0)
    connection.execute("ROLLBACK")
```

Preflight raw/rollup/hourly input counts in the selected scope before calling a materializing P1 method; enforce the 50000-row scan budget and check the 200 ms budget before/after each domain call as well as in SQLite progress. Set busy waits below the remaining deadline and interrupt expired SQL. This is cooperative cancellation of historical work, not a hard guarantee of Python thread preemption; a late result is discarded and must not block collection. Use bounded `fetchmany`/`LIMIT budget+1` for AppKey maps and page enumeration. Aggregate totals/coverage must query retained summary sources rather than scanning expired raw rows; a large series/files rejection must not disable totals/coverage for the same retained window. File-page assembly must aggregate the full allowed retained input before paging, never take LIMIT before aggregation or before the exact full sort key. Use deterministic opaque cursors bound to kind, canonical window, app, sort/direction/search and the committed store high-water identity; changed data returns `stale_cursor` and a restart indication. Cap cursor state/cache as part of the per-client budget; no persistent transactions held while a client pauses between pages. If result text would exceed the byte cap, return an explicit bounded error or a smaller exact page with a continuation cursor. Never trim coverage or bytes to fit.

Map errors to `invalid_request`, exact A22 reason/message, `unknown_app`, `query_too_large`, `query_timeout`, `stale_cursor` or bounded `history_unavailable`; never return SQLite paths or SQL. Files adapters only expose existing P1 contracts with unavailable F in P3; no new per-file producer.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command and `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_query.py tests/test_query_properties.py`.

Expected: PASS. Disable the scope precheck and ensure the launch/unknown-app test fails; raise scan/byte limits under a constrained test and ensure the corresponding resource-bound assertion fails. Restore the production limits.

- [ ] **Step 5: Commit**

```bash
git add src/appio/iod_history.py tests/p3/test_iod_history.py
git commit -m "feat(iod): expose bounded history queries with stable app identities"
```

## Task 4: Persist atomic handover baselines and exclusive ownership

**Implementer tier:** Capable

**Scheduling:** `depends_on: []`; `lane: ownership`; `sequence: 1`.

**Files:**

- Create: `src/appio/handover.py`
- Modify: `src/appio/store.py:181-478` (transactional handover entry point and extraction of the existing tick transaction body)
- Test: `tests/p3/test_handover.py`

**Interfaces:**

- Consumes: existing `HandoverState(boot_id, cutoff_mono_ns, processes, devices, version=1)`, `decode_handover(text, expected_boot_id, expected_cutoff)`, `canonical_json(value)`, `SamplerState.to_checkpoint() -> dict`, `TickInput`, `AggregatedFrame`, `CollectorEpoch`, `allocate_epoch(connection, source, boot_id, allocated_mono_ns)`, and the existing `AppIoStore.record_tick(frame, next_state)`/`record_gap(...)` transaction contracts. Use `ownership_exclusions`, `uncertain_intervals` and whole-delta `intersects` unchanged.
- Produces: `HandoverOffer(from_source: str, to_source: str, outgoing_epoch: int, outgoing_seq: int, interval_start_mono_ns: int, state: HandoverState)`; it carries evidence, not a claim that arbitrary callers are authenticated.
- Produces: `capture_handover(state: SamplerState) -> HandoverState`; `seed_handover(offer: HandoverOffer, *, incoming_epoch: int, wall_ns: int) -> tuple[AggregatedFrame, SamplerState]`; `baseline_seed(tick: TickInput, *, previous: SamplerState | None = None) -> tuple[AggregatedFrame, SamplerState]`. Both seeds have seq=0, zero emitted deltas, real process/device baselines, per-app coverage and matching version-1 checkpoint JSON.
- Produces: `AppIoStore.install_handover(offer: HandoverOffer, *, wall_ns: int) -> CollectorEpoch`, atomically installing the row, outgoing interval evidence, new incoming epoch, zero-byte baseline receipt and checkpoint. A caller must bootstrap the boot row first. `AppIoStore.record_tick` remains compatible and never joins an unrelated outer transaction.

**Threat closed:** crash/retry or forged chronology must not cause a partial cutoff, new epoch without its baseline, receipt without rows, lost pending reap credits or two sources contributing the same bytes. This API is internal; it is never a public socket request.

- [ ] **Step 1: Write the failing test**

```python
from appio.durable import Credit, HandoverState, ProcessCheckpoint, ProcessRef
from appio.handover import HandoverOffer, seed_handover


def test_seed_carries_credit_but_emits_no_pre_cutoff_bytes():
    process = ProcessCheckpoint(
        pid=410, start_ticks=100, ppid=1, parent=None,
        app_key="exe:/usr/bin/fixture", launch_root=ProcessRef(410, 100),
        launch_id=72, baseline=(100, 20, 80, 10),
        credits=(Credit(ProcessRef(411, 101), 3, 9, (4, 2, 3, 1), 1),))
    handover = HandoverState("boot", 20_000_000_000, (process,), ())
    offer = HandoverOffer("gui", "iod", 3, 10, 10_000_000_000, handover)
    frame, state = seed_handover(offer, incoming_epoch=7,
                                 wall_ns=1_700_000_000_000_000_000)
    assert (frame.source, frame.epoch, frame.seq) == ("iod", 7, 0)
    assert frame.deltas == ()
    assert state.processes[0].baseline == (100, 20, 80, 10)
    assert state.processes[0].credits[0].remaining == (4, 2, 3, 1)
    assert state.processes[0].credits[0].correction_counted is True
    assert state.processes[0].reap_incomplete is True
    assert state.to_checkpoint() == frame.checkpoint_state
```

Add transactional tests on a temporary schema-v1 database: crash injection after handover insert, epoch allocation, seed receipt and checkpoint; compare every table and published sampler state before/after rollback. Reapply an identical offer without allocating another epoch or rewinding an advanced checkpoint; reject conflicting same-cutoff JSON and boot/epoch/cutoff mismatches. Exercise gui→iod and iod→gui pure transfers with deliberately colliding local launch IDs, carry-forward credit consumed across two parent increments, parent disappearance, EACCES and PID reuse. Test adjacent cutoffs, a straddling device/task delta, uncovered intervals and missing handover: overlap creates `overlap_uncertain`, both contributors are excluded as whole deltas, and no prorated byte appears in raw/rollup/boot/lifetime results.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_handover.py`

Expected: FAIL because handover orchestration/seed APIs do not exist.

- [ ] **Step 3: Write the minimal implementation**

Capture typed state through the existing canonical checkpoint representation, convert only its process/device arrays to `HandoverState`, and validate canonical bytes against the exact boot/cutoff. Seed a new source/epoch explicitly; feeding the outgoing `SamplerState` directly to `aggregate_tick` is invalid because that function rejects a changed source. Do not use seq=-1: the existing state contract forbids negative sequences. The baseline receipt is seq=0; the first subsequent measured frame is seq=1.

```python
seed = SamplerState(
    boot_id=offer.state.boot_id, source=offer.to_source,
    epoch=incoming_epoch, last_seq=0,
    checkpoint_mono_ns=offer.state.cutoff_mono_ns,
    processes=converted_processes, launches=converted_launches,
    device_baselines=converted_devices,
    last_observed_mono_ns=offer.state.cutoff_mono_ns,
)
frame = AggregatedFrame(
    seed.boot_id, seed.source, seed.epoch, 0, seed.checkpoint_mono_ns,
    wall_ns - seed.checkpoint_mono_ns, (), coverage, (), seed.to_checkpoint())
```

Convert each process using stable boot/PID/start ticks, parent and launch root; build `LaunchIdentity(root, app_key)` in pure state (its `launch_id` is 0), let the store assign actual surrogate rows locally, retain FIFO credit order/correction bits, and mark rate/reset provenance unknown where v1 lacks it. Do not infer a readable baseline for `baseline=None`. New identities discovered during a source change are baselined, with partial coverage, rather than emitting lifetime cumulative bytes. `baseline_seed` may reuse verified same-identity credits from the same authority, but must not subtract an unverified cross-authority credit.

Extract the current `record_tick` transaction body into a private method that requires an already-open store-owned transaction; both public entry points still own their outer transaction. Preserve rejection-before-mutation and A21 overflow handling, including the failure path in which diagnostic persistence itself fails. `install_handover` validates chronology before entering mutation, persists the canonical row and proven outgoing interval, allocates the incoming epoch, installs its baseline frame with the same private writer, and publishes `store.sampler_state` only after COMMIT. Reject a cutoff that would require repairing already-pruned contributions, or an incomplete preceding ownership chain that would make the first stored handover retroactively assign the wrong owner before its cutoff; return an explicit incomplete transition instead of inventing corrections. When no complete authenticated outgoing offer exists, do not call this method: Task 12 records the unclean gap/overlap path.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command and `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_store.py tests/test_store_overflow.py tests/test_stream.py tests/test_sampler.py tests/test_durable.py`.

Expected: PASS. Drop the credit conversion and move one seed write outside the transaction in separate temporary probes; the credit test and rollback comparison must each fail. Restore both mechanisms.

- [ ] **Step 5: Commit**

```bash
git add src/appio/handover.py src/appio/store.py tests/p3/test_handover.py
git commit -m "feat(ownership): commit handover cutoffs and baseline receipts atomically"
```

## Task 5: Extend the existing collector for daemon and handover starts

**Implementer tier:** Advanced

**Scheduling:** `depends_on: [4]`; `lane: ownership`; `sequence: 2`.

**Files:**

- Modify: `src/appio/live.py:199-407` (source guard, initialization, tick publication and resume/stop APIs)
- Test: `tests/p3/test_live_sources.py`

**Interfaces:**

- Consumes: existing `LiveCollector(proc_root, cgroup_root, database_path, clock, *, source="gui")`, `SystemClock() -> tuple[int, int]`, `TickResult`, `CoverageSummary`, `AppIoStore.record_tick(frame, next_state)`; Task 4 `HandoverOffer(...)`, `baseline_seed(tick, *, previous=None)`, `capture_handover(state)`, `AppIoStore.install_handover(offer, *, wall_ns)`.
- Produces: backwards-compatible `LiveCollector(..., *, source: Literal["gui","iod"] = "gui", baseline_only: bool = False, resume_handover: HandoverOffer | None = None)`; `quiesce() -> HandoverOffer` for a running collector whose destination is the other source; read-only `last_frame: AggregatedFrame | None`; `next_seq` accounts for a seq=0 seed. `tick()` still returns `TickResult` and `close()` remains idempotent.
- Produces: exact publication order: read → stage → commit → assign state/frame/next_seq → notify. Quiesce prevents any later tick and captures the last committed cutoff; a collection never started cannot create a clean offer.

- [ ] **Step 1: Write the failing test**

```python
import shutil
from pathlib import Path
from appio.live import LiveCollector


def test_iod_baseline_does_not_import_pre_start_cumulative_bytes(tmp_path):
    fixtures = Path(__file__).parents[1] / "fixtures"
    proc = tmp_path / "proc"
    group = tmp_path / "cgroup"
    shutil.copytree(fixtures / "proc", proc, symlinks=True)
    shutil.copytree(fixtures / "cgroup" / "normal-t0", group, symlinks=True)
    clock = iter([(10_000_000_000, 1_700_000_000_000_000_000),
                  (11_000_000_000, 1_700_000_001_000_000_000)])
    with LiveCollector(proc, group, tmp_path / "iod.db", lambda: next(clock),
                       source="iod", baseline_only=True) as collector:
        collector.tick()
        assert collector.last_frame.source == "iod"
        assert collector.last_frame.deltas == ()
        assert collector.next_seq == 1
        assert collector.store.connection.execute(
            "SELECT sum(read_bytes+write_bytes) FROM app_boot_total").fetchone()[0] in (None, 0)
```

Add second-tick positive deltas, same-boot daemon restart with increasing epoch and preserved credit, new-boot epoch reset, installed handover seq=0 followed by seq=1, quiesce rejecting another tick, and commit failure leaving `last_frame`/state/next_seq unchanged. Reopen an exported root-origin boot for source gui without rewriting its immutable `boot.source`; source ownership comes from stream/interval records, not that original-creation field. Preserve the original GUI initial-first-seen A01 behavior when `baseline_only=False` and no transfer was requested.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_live_sources.py`

Expected: FAIL on the unrecognized `baseline_only` option or the current gui-only source guard.

- [ ] **Step 3: Write the minimal implementation**

Keep parsing/identity/aggregation logic shared; accept only the two existing source tokens. Restore same-authority checkpoints through the existing API. For baseline-only starts, defer epoch allocation until the actual first sampled baseline timestamp rather than using constructor time. For an incoming offer, `install_handover` allocates the epoch itself; do not call `start_collector` first and allocate twice. A source-change baseline commits via Task 4's zero-byte seed; a normal GUI cold start continues through the existing `aggregate_tick` path. Start the first incoming ownership interval at its actual measured baseline cutoff, and retain a known absence as an explicit gap; do not claim coverage from helper invocation time.

If an incoming offer's cutoff precedes the first real sample, advance process/credit bookkeeping through that sample, omit its entire cross-gap task/device deltas, retain partial/reap-incomplete coverage and persist the measured gap. This transition behavior belongs here so Task 12 never needs to edit `live.py`. An offer without a valid ownership chain must use a fresh conservative baseline instead of installing a misleading handover row.

```python
self.store.record_tick(frame, next_state)
self._state = next_state
self._last_frame = frame
self._next_seq = frame.seq + 1
```

Use one timestamp pair for each sampled baseline/cutoff; do not create extra clock reads that invalidate deterministic clocks. `quiesce()` runs under the collector's single-thread owner, freezes further ticks, and captures only durable state. Ordinary same-source restarts keep the store's gap and conservative A07 behavior. Keep all imports P1/P2-compatible: `handover.py` itself must use only existing pure/store types, without socket, daemon or privilege dependencies.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command and `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_live.py tests/acceptance/test_no_later_scope.py`.

Expected: PASS. Route baseline-only startup through normal first-seen cumulative emission and ensure the zero-byte test fails. Restore it, then verify commit-failure publication assertions.

- [ ] **Step 5: Commit**

```bash
git add src/appio/live.py tests/p3/test_live_sources.py
git commit -m "feat(collector): support iod streams and explicit baseline starts"
```

## Task 6: Run the root daemon with durable history and a fair socket loop

**Implementer tier:** Capable

**Scheduling:** `depends_on: [1,2,3,5]`; `lane: daemon`; `sequence: 1`.

**Files:**

- Create: `src/appio/iod.py`
- Create: `data/systemd/appio-iod.service`
- Modify: `pyproject.toml:14-15` (add `appio-iod = "appio.iod:main"` only)
- Test: `tests/p3/test_iod.py`
- Test: `tests/p3/test_iod_service.py`

**Interfaces:**

- Consumes: Task 1 `FrameDecoder.feed(data)->tuple[dict,...]`, `encode_frame(message, *, max_bytes=262144)`, `validate_request(message)->Request`, `Capabilities(...)`, `StreamCursor(...)`; Task 2 `read_peer`, `authorize_client`, `verify_runtime_paths`, `ClientQueue`; Task 3 `HistoryRpc(connection, *, limits, clock_ns).execute(request, *, request_id)->dict`; Task 5 extended `LiveCollector`, `last_frame`, `quiesce` and `sampler_state`.
- Produces: `DaemonPaths(database: Path = Path("/var/lib/appio/iod.db"), socket: Path = Path("/run/appio/iod.sock"), proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup"))`.
- Produces: `Daemon(paths: DaemonPaths, *, clock: Callable[[], tuple[int,int]], peer_reader: Callable, group_lookup: Callable, filesystem: object, selector: object, query_worker: object)` with `start() -> None`, `step() -> None`, `stop() -> None` and `committed_cursor: StreamCursor | None`; `main(argv: list[str] | None = None) -> int` using only production dependencies and root enforcement.
- Produces: fixed service runtime artifacts. GUI close/reconnect never controls daemon lifetime; service remains shipped disabled, with no socket-activation unit that would start it implicitly.

**Threat closed:** a busy or silent client, stale socket path or expensive query must not block the collector, spoof ownership, expose a writable database or extend shutdown indefinitely.

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
import tomllib


def test_service_is_root_owned_runtime_with_no_gui_or_bpf_dependency():
    root = Path(__file__).parents[2]
    unit = (root / "data/systemd/appio-iod.service").read_text()
    assert "ExecStart=/usr/bin/appio-iod" in unit
    assert "User=root" in unit
    assert "UMask=0077" in unit
    assert "StateDirectory=appio" in unit
    assert "RuntimeDirectory=appio" in unit
    assert "PrivateUsers=yes" not in unit
    assert "ProcSubset=pid" not in unit
    assert "ConditionPathExists=/sys/kernel/btf" not in unit
    scripts = tomllib.loads((root / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["appio-iod"] == "appio.iod:main"
```

In `test_iod.py` construct `Daemon` with a temporary database/socket, fixture proc/cgroup trees, fake root-owned metadata, fake root/member/refused credentials, virtual clock and deterministic query worker. Two fake peers must observe committed seq=0 baseline then measured seq=1; close all GUI peers and advance >=5 s, reopen the DB and prove data remains. Inject COMMIT failure and assert no snapshot/high-water advance. With one peer never reading and another querying, collection still commits on schedule and only the slow peer is disconnected. Test stale socket removal refusing symlink/foreign owner, a second daemon refused by the data-writer lock before touching DB/socket state, 65th connection refusal, header/body deadline, malformed frames, SIGTERM-equivalent final flush, crash/restart, and recovery gap. Stop/start without any GUI must still collect.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_iod.py tests/p3/test_iod_service.py`

Expected: FAIL on missing runtime artifacts.

- [ ] **Step 3: Write the minimal implementation**

Acquire a nonblocking exclusive `flock` on the fixed root-owned 0600 `/var/lib/appio/iod.lock` before opening the DB or changing the socket; keep its descriptor held and never unlink the lock file. This prevents manual and systemd starts from becoming two writers. Run a `selectors` loop with a 2 s sampling target and never more than 5 s between scheduled durable flushes during healthy operation. Every tick commits through `LiveCollector(source="iod", baseline_only=True)`; do not build an independent writer or replay received GUI counters. Set SQLite WAL and an explicit durability policy (`synchronous=FULL`), retain the existing rollup/quota paths, and preserve database/WAL/SHM 0600 root ownership. If sampling/storage exceeds its bound or fails, expose the failure and a gap rather than claiming punctual accounting.

Authenticate before reading/dispatch. Limit each socket to 64 KiB input/output and eight parsed requests per turn; enforce 64 clients, queue/deadline caps, one outstanding query/client and at most two running history jobs plus a bounded waiting set of 64. Historical jobs own separate read-only connections in worker threads, use Task 3 interruption/deadline checks and never share the collector's SQLite connection. On disconnect cancel queued work; running work reaches its deadline. Shutdown interrupts jobs and joins with a finite budget, then closes resources. Collectors never wait for client transmission or a historical query.

Maintain a bounded replay ring of the last 64 committed snapshots with a 4 MiB global byte cap. Replay only matching boot/epoch cursors retained in the ring; stale/unknown/new-boot cursors receive an explicit gap plus current state. If a committed snapshot cannot fit one 256 KiB frame, send a bounded resync gap/status with its cursor; durable history remains queryable. Never split an observation into independently deduplicated snapshots or silently omit process/device data. Cap logged message text and never log raw untrusted payloads.

```ini
[Unit]
Description=Application I/O collector
After=local-fs.target

[Service]
Type=simple
User=root
Group=appio
ExecStart=/usr/bin/appio-iod
UMask=0077
StateDirectory=appio
StateDirectoryMode=0711
RuntimeDirectory=appio
RuntimeDirectoryMode=0750
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
ReadWritePaths=/var/lib/appio /run/appio
Restart=on-failure
TimeoutStopSec=5s

[Install]
WantedBy=multi-user.target
```

`Group=appio` supplies the directory group; daemon explicitly chmods only the socket to 0660 after binding and verifying its parent. The 0711 state directory permits the invoking UID to traverse to its fixed probe file; DB/WAL/SHM/setup-state remain root-owned 0600 and staging directories 0700. Status obtains per-UID pending state from bounded, root-owned `/var/lib/appio/setup-state.json` with exact shape `{"version":1,"pending_by_uid":{"1000":true}}`; the UID comes from `SO_PEERCRED`. Missing/invalid state yields unknown pending, never successful activation, and this reader uses stdlib only, not a dependency on later setup modules. Avoid hidepid/proc namespace settings that defeat all-PID collection. Audit the chosen hardening against fixture behavior; do not add BPF permissions, attach code, desktop dependencies, installation hooks or an automatic enable step.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Also parse the service with `systemd-analyze verify data/systemd/appio-iod.service` when available, treating a missing not-yet-installed `/usr/bin/appio-iod` as a recorded artifact-installation limitation, not permission to install it during tests. Disable post-COMMIT publication and the fairness read budget in separate probes; corresponding persistence/fairness tests must fail.

- [ ] **Step 5: Commit**

```bash
git add src/appio/iod.py data/systemd/appio-iod.service pyproject.toml tests/p3/test_iod.py tests/p3/test_iod_service.py
git commit -m "feat(iod): run optional durable collector and bounded Unix service"
```

## Task 7: Constrain the polkit helper entry boundary

**Implementer tier:** Advanced

**Scheduling:** `depends_on: []`; `lane: privilege`; `sequence: 1`.

**Files:**

- Create: `src/appio/setup_boundary.py`
- Create: `data/libexec/appio-setup`
- Create: `data/polkit/io.github.appio.setup.policy`
- Test: `tests/p3/test_setup_boundary.py`

**Interfaces:**

- Consumes: Python `pwd.getpwuid`, root-owned interpreter/import paths and the design's exact six verbs. No daemon/socket imports are needed.
- Produces: `VERBS = frozenset({"install-delegation", "enable", "disable", "status", "restart-user-manager", "export-history"})`; `SetupIdentity(uid: int, gid: int, username: str, home: Path)`; `Invocation(verb: str, identity: SetupIdentity)`; `InvocationError(exit_code: int, reason: str)`.
- Produces: `parse_invocation(argv: list[str], *, environ: Mapping[str,str], euid: int, getpwuid: Callable[[int], object]) -> Invocation`. `argv` includes the executable; exactly two elements are required. Invalid argv exits 2 before any filesystem/command operation. Missing/invalid PKEXEC identity or non-root execution fails closed with nonzero status.
- Produces: `CommandResult(returncode: int, stdout: str, stderr: str)`; `CommandRunner.run(argv: tuple[str,...], *, uid: int = 0, gid: int = 0, env: Mapping[str,str] | None = None, timeout_seconds: float = 5.0) -> CommandResult`; production runner never uses a shell.
- Produces: `SetupDeps(root: Path, runner: CommandRunner, effective_uid: Callable[[],int], getpwuid: Callable[[int],object], metadata: Callable[[int],object], chown: Callable[[int,int,int],None], clock_ns: Callable[[],int], event: Callable[[str],None])`, where `root` defaults to `/` only in the production factory. The test event callback injects failures and records operations; it never appears in argv/environment. `metadata` receives a pinned descriptor and defaults to `os.fstat`, allowing tests to inject trusted ownership without changing the host; `chown` also receives an already-open descriptor and defaults to `os.fchown`, so export never reopens a user-controlled pathname to change ownership.
- Produces: `validate_trusted_runtime(paths: tuple[Path,...], *, metadata: Callable[[int],object], caller_uid: int) -> None`, covering interpreter, appio module closure, parent directories and future BPF-object path only if such an object is actually used in P4.

**Threat closed:** an administrator prompt must not become an arbitrary root command/file/user selector or a loader for the invoking user's Python code. `getuid()` inside pkexec is root and must never select the beneficiary.

- [ ] **Step 1: Write the failing test**

```python
from types import SimpleNamespace
import pytest
from appio.setup_boundary import InvocationError, parse_invocation


def test_pkexec_identity_wins_over_user_and_effective_uid():
    account = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="alice",
                              pw_dir="/home/alice")
    call = parse_invocation(["/usr/bin/appio-setup", "enable"],
        environ={"PKEXEC_UID": "1000", "USER": "root", "HOME": "/root"},
        euid=0, getpwuid=lambda uid: account if uid == 1000 else None)
    assert call.identity.uid == 1000
    assert call.identity.username == "alice"
    assert str(call.identity.home) == "/home/alice"


def test_extra_argument_is_rejected_before_account_lookup():
    def forbidden_lookup(uid):
        raise AssertionError("argv rejection must precede account lookup")
    with pytest.raises(InvocationError) as caught:
        parse_invocation(["/usr/bin/appio-setup", "enable", "/tmp/evil"],
                         environ={}, euid=0, getpwuid=forbidden_lookup)
    assert caught.value.exit_code == 2
```

Add every allowed verb; unknown/uninstall verb; absent, negative, whitespace, float, boolean-like, huge or unknown `PKEXEC_UID`; non-root helper invocation; unsafe username/NUL/home from fake NSS; root caller UID 0 behavior; unexpected argv options and shell metacharacters. Trusted-runtime tests reject invoking-user-owned/writable modules and any writable ancestor. Parse the policy XML: exact action ID/path, `auth_admin` for active/inactive/any, no `auth_admin_keep`, and no broad wildcard rule.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_setup_boundary.py`

Expected: FAIL on missing boundary module/artifacts.

- [ ] **Step 3: Write the minimal implementation**

Validate argc and literal verb before consulting account state; accept only a canonical decimal UID within the platform UID range, resolve it with `getpwuid`, and derive home/username/gid solely from that result. Check the returned UID agrees. Ignore USER, HOME, PATH, PYTHONPATH, inherited DBus addresses and argv for identity. Reject NUL/newline/option-like account fields that cannot be safely placed in the fixed argv; require an absolute home path.

```python
if len(argv) != 2 or argv[1] not in VERBS:
    raise InvocationError(2, "invalid_verb_or_argument_count")
if euid != 0:
    raise InvocationError(1, "root_required")
```

Ship the canonical helper shim with `#!/usr/bin/python -I`. Its stdlib-only bootstrap checks argc/verb, discovers the installed top-level appio package without executing it, and validates its complete module closure/ancestry before importing `appio.setup_service.main`; importing the protected validator from unverified appio first would defeat the check. The system interpreter/stdlib/site directories are installation prerequisites with root-owned non-writable ancestry. The shim is installed as `/usr/bin/appio-setup`; Task 10 supplies that main. Do not generate a second, less constrained setuptools helper entry point. Polkit binds action `io.github.appio.setup` with `org.freedesktop.policykit.exec.path=/usr/bin/appio-setup` and requires `auth_admin` for enable and disable alike. Runtime installation must make interpreter/module ancestry root-owned and non-writable by callers; validate that closure before dispatching privileged operations. The test checkout is allowed only through injected metadata.

The production command runner uses absolute argv, a sanitized minimal environment, closed extraneous descriptors, captured bounded output and finite timeouts. Process timeout must terminate/reap the child and return a distinct failure; it must not leave an unbounded wait or a shell grandchild. Commands that intentionally terminate the session are dispatched only by the explicitly consented restart verb.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Substitute `euid` for `PKEXEC_UID` in a temporary probe and prove the beneficiary assertion fails; relax argc and prove the extra-argument test fails. Restore both checks.

- [ ] **Step 5: Commit**

```bash
git add src/appio/setup_boundary.py data/libexec/appio-setup data/polkit/io.github.appio.setup.policy tests/p3/test_setup_boundary.py
git commit -m "feat(setup): bind fixed helper verbs to validated pkexec identity"
```

## Task 8: Install delegation and pending state with safe atomic writes

**Implementer tier:** Advanced

**Scheduling:** `depends_on: [7]`; `lane: privilege`; `sequence: 2`.

**Files:**

- Create: `src/appio/setup_files.py`
- Create: `src/appio/setup_delegation.py`
- Test: `tests/p3/test_setup_files.py`
- Test: `tests/p3/test_setup_delegation.py`

**Interfaces:**

- Consumes: Task 7 `SetupIdentity(uid, gid, username, home)`, `CommandRunner.run(argv, *, uid=0, gid=0, env=None, timeout_seconds=5.0)->CommandResult`, and `SetupDeps(root, runner, effective_uid, getpwuid, metadata, chown, clock_ns, event)`.
- Produces: `SafeRootFS(deps: SetupDeps)` with `atomic_write(path: str, content: bytes, *, mode: int = 0o644) -> None`, `read_bounded(path: str, *, max_bytes: int) -> bytes`, and `locked() -> ContextManager[None]`. Only the fixed configuration/state/probe/export roots in this plan may be addressed by production callers.
- Produces: `SetupState(version: int, pending_by_uid: Mapping[int,bool])`; `load_setup_state(fs: SafeRootFS) -> SetupState`; `set_pending(fs: SafeRootFS, uid: int, pending: bool) -> None`, persisted at `/var/lib/appio/setup-state.json` root:root 0600 under a helper-operation lock.
- Produces: `install_delegation(identity: SetupIdentity, *, deps: SetupDeps, fs: SafeRootFS) -> dict`, returning versioned JSON with `delegation_installed`, `pending_activation`, `uid` and bounded diagnostics; no `answering=True` claim.

**Threat closed:** symlink/path races, hostile pre-existing files, partial writes or lost fsyncs must not redirect root configuration writes; group installation must not remove other supplementary memberships or target a claimed username.

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from types import SimpleNamespace
import os
import pytest
from appio.setup_boundary import SetupDeps
from appio.setup_files import SafeRootFS


def test_config_symlink_cannot_overwrite_a_victim(tmp_path):
    target = tmp_path / "etc/systemd/system/user@.service.d/10-appio-io.conf"
    target.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    target.symlink_to(victim)
    def metadata(fd):
        row = os.fstat(fd)
        return SimpleNamespace(st_uid=0, st_gid=0, st_mode=row.st_mode,
                               st_ino=row.st_ino, st_dev=row.st_dev, st_nlink=row.st_nlink)
    deps = SetupDeps(tmp_path, None, lambda: 0, lambda uid: None, metadata,
                     lambda path, uid, gid: None, lambda: 1, lambda name: None)
    with pytest.raises(PermissionError):
        SafeRootFS(deps).atomic_write(
            "/etc/systemd/system/user@.service.d/10-appio-io.conf", b"new")
    assert victim.read_bytes() == b"preserve"
```

Add symlink/writable/non-root-owned ancestor, target hardlink/nonregular file, parent rename attempt, existing expected/unexpected config, failure at write/file-fsync/rename/parent-fsync, mode preservation and repeated install. Record operation order and assert file fsync precedes rename and directory fsync follows it. Fake runner must record exactly `/usr/bin/usermod -aG appio <validated-user>` and `/usr/bin/systemctl daemon-reload`, never `-G` alone. Group absence is a clear installation prerequisite failure; P5 packaging creates group appio. Installation failure/pending state cannot accidentally mark accounting active.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_setup_files.py tests/p3/test_setup_delegation.py`

Expected: FAIL because safe file/delegation modules do not exist.

- [ ] **Step 3: Write the minimal implementation**

Anchor traversal at the injected root descriptor and walk fixed components with `O_DIRECTORY|O_NOFOLLOW`; verify root ownership and no group/world write through pinned descriptors. Open existing targets with `O_NOFOLLOW`, require a regular root-owned file with link count one, and reject unexpected content instead of overwriting an administrator's unrelated file. Create a random exclusive sibling, write completely, set its fixed mode/ownership, fsync it, rename within the same pinned parent, then fsync that directory. Clean the temporary on failure; never truncate the live target before replace. No caller-controlled basename or path is accepted.

```ini
# /etc/systemd/system/user@.service.d/10-appio-io.conf
[Service]
Delegate=cpu io memory pids
IOAccounting=yes
```

```ini
# /etc/systemd/user.conf.d/10-appio.conf
[Manager]
DefaultIOAccounting=yes
```

Before changing the delegate list, inspect the existing effective delegated controllers using fixed systemctl argv and preserve any additional controllers in a stable sorted union; never remove an existing delegation to add io. Persist `pending_activation=True` before installing either file. The shared on-disk state is exactly `{"version":1,"pending_by_uid":{"1000":true}}` with canonical decimal UID keys and boolean values, capped at 64 KiB on read and write. Establish `/var/lib/appio` root-owned 0711 (including before the first daemon start) so the fixed caller probe can be traversed; keep its state file 0600 and private staging subdirectories 0700. Serialize helper operations using a fixed root-owned lock under `/run/appio-setup/`, independent of the daemon's runtime directory. Ensure both writes and `usermod -aG` succeed before returning installed; run daemon-reload after file changes. Two file replacements cannot be a cross-file atomic transaction: on failure preserve pending status and report which fixed operation failed, allowing safe idempotent retry. Do not restart a user manager as a side effect of installation. Disable in Task 10 deliberately retains these files and memberships.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Bypass `O_NOFOLLOW` in a temporary probe and ensure the victim/symlink assertion fails; omit parent fsync and ensure the recorded-order test fails. Restore both protections.

- [ ] **Step 5: Commit**

```bash
git add src/appio/setup_files.py src/appio/setup_delegation.py tests/p3/test_setup_files.py tests/p3/test_setup_delegation.py
git commit -m "feat(setup): install delegation with durable pending activation state"
```

## Task 9: Prove accounting activation and require an explicit session choice

**Implementer tier:** Advanced

**Scheduling:** `depends_on: [8]`; `lane: activation`; `sequence: 1`.

**Files:**

- Create: `src/appio/activation.py`
- Test: `tests/p3/test_activation.py`

**Interfaces:**

- Consumes: Task 7 `SetupIdentity(...)`, `SetupDeps(...)`, `CommandRunner.run(...)`; Task 8 `SafeRootFS`, `set_pending(fs, uid, pending)` and `SetupState`; existing cgroup parser for device-byte counters.
- Produces: `ProbeEvidence(delegation_present: bool, filesystem_supported: bool, expected_bytes: int, observed_bytes: int | None, device: str | None, command_exit: int | None, cleanup_complete: bool)`; `activation_passes(evidence: ProbeEvidence) -> bool`; `ActivationResult(pending_activation: bool, state: str, evidence: ProbeEvidence | None)`.
- Produces: `probe_accounting(identity: SetupIdentity, *, deps: SetupDeps, fs: SafeRootFS) -> ActivationResult`; `choose_activation(choice: Literal["restart","reboot"], identity: SetupIdentity, *, deps: SetupDeps, fs: SafeRootFS) -> ActivationResult`. The reboot choice only persists pending; it never calls reboot. The restart choice invokes the fixed root systemctl unit for that UID only after separate UI consent.

**Threat closed:** controller presence, a stale file or a successful `dd` exit must not falsely prove working device accounting, and installation/polling must never end a session without an explicit user choice.

- [ ] **Step 1: Write the failing test**

```python
from dataclasses import replace
from appio.activation import ProbeEvidence, activation_passes


def test_controller_without_device_bytes_never_passes():
    evidence = ProbeEvidence(True, True, 16 * 1024 * 1024, 0,
                             "259:0", 0, True)
    assert not activation_passes(evidence)
    assert activation_passes(replace(evidence, observed_bytes=16 * 1024 * 1024))
    assert not activation_passes(replace(evidence, filesystem_supported=False))
    assert not activation_passes(replace(evidence, cleanup_complete=False))


def test_five_percent_comparison_is_exact_integer_arithmetic():
    evidence = ProbeEvidence(True, True, 100, 95, "259:0", 0, True)
    assert activation_passes(evidence)
    assert activation_passes(replace(evidence, observed_bytes=105))
    assert not activation_passes(replace(evidence, observed_bytes=94))
    assert not activation_passes(replace(evidence, observed_bytes=106))
```

Add fake transient-unit lifecycle tests for absent controller, present/no bytes, correct bytes, dd error, timeout, unsupported/tmpfs target, wrong device, missing user bus, misleading stale io.stat and cleanup failure. Force every failure point and assert the unit/file are cleaned in `finally`. For `Linger=yes`, reboot choice must record pending and issue no restart; restart choice must use `/usr/bin/systemctl restart user@1000.service` exactly. New-boot status stays pending until a new successful accounting probe, even when io appears in cgroup.controllers.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_activation.py`

Expected: FAIL on missing activation module.

- [ ] **Step 3: Write the minimal implementation**

Use the D2 fixed block-backed directory, verifying filesystem/device rather than assuming `/var/lib` has the right backing. Create root-owned traversal-only parents and a fresh caller-owned 0600 file through pinned descriptors; the caller may write the file but cannot replace it with a symlink. Run `/usr/bin/systemd-run --user --unit=appio-probe-<numeric-nonce> --property=Type=oneshot --property=RemainAfterExit=yes` as the validated caller, using runner uid/gid and a freshly derived `/run/user/<uid>`/user bus address. Pass `/usr/bin/dd if=/dev/zero of=<fixed-file> bs=1M count=16 oflag=direct` as a fixed argument vector, with a finite 5 s deadline. No root user-manager bus or inherited session environment may be substituted.

Keep the transient unit around long enough to read its own cgroup's before/after `io.stat`; the fresh unit's initial counters may be treated as zero only after proving the nonce/unit did not exist before creation. `RemainAfterExit=yes` retains the service until evidence is read and explicit stop/reset; if this host still removes the accounting cgroup early, fail the probe rather than infer bytes. The transient unit must not use automatic collection before evidence is read. Select the verified filesystem block device once, not every stacked parent and child device, and use the write-byte delta for that unit. Stop/reset the exact unit and unlink the exact probe file in `finally`. Failure to find or retain evidence is accounting inactive.

```python
def activation_passes(evidence):
    return (evidence.delegation_present and evidence.filesystem_supported
            and evidence.command_exit == 0 and evidence.cleanup_complete
            and evidence.device is not None and evidence.expected_bytes > 0
            and evidence.observed_bytes is not None
            and abs(evidence.observed_bytes - evidence.expected_bytes) * 100
                <= evidence.expected_bytes * 5)
```

A successful probe atomically clears only that UID's pending flag. All other outcomes retain it with the exact state `delegation present, accounting inactive` when delegation exists, or `not delegated` otherwise. Expose measured values/exit for diagnosis without claiming T or D zero. `choose_activation("reboot", ...)` persists pending and returns `on, needs activation`. `choose_activation("restart", ...)` persists pending before executing the disruptive command; success of systemctl alone never clears pending. The UI must say `Restart my user manager now` and `Activate after reboot`, with the first choice's explicit warning that it ends the session.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Replace the proof with controller-presence-only and ensure the first test fails; remove cleanup from one failure path and ensure the lifecycle test fails. Restore both.

- [ ] **Step 5: Commit**

```bash
git add src/appio/activation.py tests/p3/test_activation.py
git commit -m "feat(activation): verify device accounting and persist explicit activation choice"
```

## Task 10: Implement enable/disable, durable user exports and helper dispatch

**Implementer tier:** Capable

**Scheduling:** `depends_on: [6,8,9]`; `lane: privilege`; `sequence: 3`.

**Files:**

- Create: `src/appio/history_export.py`
- Create: `src/appio/setup_service.py`
- Test: `tests/p3/test_history_export.py`
- Test: `tests/p3/test_setup_service.py`

**Interfaces:**

- Consumes: Task 7 `parse_invocation(argv, *, environ, euid, getpwuid)->Invocation`, `SetupDeps(...)`, `CommandRunner.run(...)`, `validate_trusted_runtime(...)`; Task 8 `SafeRootFS`, `install_delegation(identity, *, deps, fs)->dict`, `load_setup_state`; Task 9 `probe_accounting(...)->ActivationResult`, `choose_activation(choice, identity, *, deps, fs)->ActivationResult`; Task 6 fixed service/database/socket paths and protocol status.
- Produces: `ExportResult(path: Path, boot_id: str | None, iod_epoch: int | None, seq: int | None, cutoff_mono_ns: int | None)`; `export_history(identity: SetupIdentity, *, deps: SetupDeps, source: Path = Path("/var/lib/appio/iod.db")) -> ExportResult`. Only trusted production code selects the fixed source; argv cannot select it.
- Produces: `dispatch(invocation: Invocation, *, deps: SetupDeps) -> dict`; `main(argv: list[str] | None = None, *, deps: SetupDeps | None = None) -> int`. Main prints one bounded version-1 JSON result to stdout, diagnostics to stderr and the actual nonzero exit on failure. The production shim supplies no dependency overrides.
- Produces: result keys `version`, `verb`, `uid`, `enabled`, `answering`, `pending_activation`, `state`, `export_path`, `export_cutoff`, `error`; unavailable values are null. `answering` distinguishes the helper's root probe from an invoking GUI's successful verified connection; the UI always performs its own direct probe.

**Threat closed:** disabling must not confiscate durable history, follow a caller-controlled export symlink, replace arbitrary user/root files, execute SQL from a user database or claim a partial export is complete.

- [ ] **Step 1: Write the failing test**

```python
from types import SimpleNamespace
import pytest
from appio.setup_service import main


def test_invalid_invocation_does_not_construct_privileged_adapters(capsys):
    touched = []
    deps = SimpleNamespace(effective_uid=lambda: 0,
        getpwuid=lambda uid: touched.append("lookup"),
        runner=SimpleNamespace(run=lambda *args, **kwargs: touched.append("command")),
        event=lambda name: touched.append(name))
    assert main(["/usr/bin/appio-setup", "disable", "--anything"], deps=deps) == 2
    assert touched == []
    assert capsys.readouterr().out == ""
```

Create root and destination trees under `tmp_path`, fake metadata/chown/runner and a real schema-v1 SQLite DB in WAL mode. Commit rows only in WAL, export, reopen independently and compare every table plus checkpoint/cutoff and `PRAGMA integrity_check`; copying only the main file must fail this test. Test identical timestamps/collisions without overwrite, symlinked destination/ancestor, foreign-owned home, backup/disk/fsync/ownership/publication failure and absence of a source DB. Enable/disable runner tests assert absolute fixed argv and exact exit/stderr propagation, no disable on pkexec dismissal, delegation retained, final flush included and root DB preserved on export failure. Two administrators' concurrent helpers serialize, with no mixed cutoff/result JSON.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_history_export.py tests/p3/test_setup_service.py`

Expected: FAIL on missing service/export modules.

- [ ] **Step 3: Write the minimal implementation**

Validate invocation before creating privileged adapters, then validate trusted runtime and acquire the fixed helper lock. Dispatch through the literal six-verb table. `install-delegation` calls Task 8. `status` performs Task 9's explicit authenticated accounting check. `restart-user-manager` calls only the consented restart choice. `enable` requires installed delegation, invokes `/usr/bin/systemctl enable --now appio-iod.service`, and performs a bounded five-second protocol-1 root-peer status probe; a command exit 0 alone is not answering. Keep pending state visible even while T collection is running. Detect unsupported/container environments before mutations and return their concrete reason.

`disable` preflights export feasibility, invokes `/usr/bin/systemctl disable --now appio-iod.service`, waits for bounded graceful shutdown/final commit, and then exports the stable DB. Preserve delegation files and group memberships. If shutdown or export fails, return its error and the *probed actual state*, retain the root DB, and never say disable/export succeeded. A forced stop has uncertain final coverage and cannot be called a clean handover. `export-history` takes a consistent online backup without disabling the service.

```python
source_connection.backup(stage_connection, pages=256, progress=check_deadline)
# Close/check the trusted staged schema-v1 database and integrity/checkpoint state.
# Publish completed bytes, never an open SQLite connection, into the user's tree.
```

Use SQLite's backup API into a root-only staging area, with busy/deadline bounds and no user-controlled SQL/SQLite extension loading. Validate schema and canonical checkpoints after backup. Derive destination exclusively from `getpwuid(PKEXEC_UID).pw_dir`, fixed `.local/share/appio`, and `iod-export-<integer-UTC-ns>[-<suffix>].db`. Pin every destination directory with no symlink traversal and permit only root/caller-owned ancestry; create missing user directories with the caller's ownership, not root-owned obstacles. Never open a user-supplied existing database as root.

For atomic no-replace publication on the user's filesystem, create an anonymous `O_TMPFILE` there, copy the already-complete trusted backup, fsync, set owner to the validated uid/gid and mode 0600, and link it to the final unused basename with `linkat(AT_EMPTY_PATH)`; fsync the parent. Retry name collisions without replacement. If the filesystem lacks this safe mechanism, fail with preserved source and a precise export error. A named temporary in a user-writable directory is not a safe root rename source. Test chown through the injected callback; normal tests never require real chown to UID 0/1000. Publish final root checkpoint evidence in the helper result so the receiving GUI can validate it; do not transfer user-selected baselines back into the root store.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Substitute `shutil.copyfile` for the online backup and prove the WAL-only data comparison fails; replace no-replace publication with overwrite and prove the collision test fails. Restore both mechanisms.

- [ ] **Step 5: Commit**

```bash
git add src/appio/history_export.py src/appio/setup_service.py tests/p3/test_history_export.py tests/p3/test_setup_service.py
git commit -m "feat(setup): orchestrate daemon lifecycle and preserve history on disable"
```

## Task 11: Build a verified reconnecting read-through daemon client

**Implementer tier:** Advanced

**Scheduling:** `depends_on: [1,2,3,4,6]`; `lane: client`; `sequence: 1`.

**Files:**

- Create: `src/appio/iodclient.py`
- Test: `tests/p3/test_iodclient.py`

**Interfaces:**

- Consumes: Task 1 `FrameDecoder`, `encode_frame`, `StreamCursor`, `validate_server_message`, `QueryRequest`; Task 2 `read_peer(sock)->Peer`, `require_root_server(peer)`, `verify_runtime_paths(...)`; Task 3 `from_wire(value, *, app_ids, result_kind)->object`; Task 4 canonical handover validation and Task 6 committed status/snapshot/replay behavior.
- Produces: `CursorTracker()` with `accept(cursor: StreamCursor) -> bool`, `current: StreamCursor | None`, `begin_connection() -> None`, `accept_status(message: dict) -> None`. Accepts only the root-verified status-advertised boot/epoch, and monotone seq within that generation; a new connection must re-establish authority before samples are accepted.
- Produces: `GapNotice(boot_id: str, iod_epoch: int, seq: int, from_mono_ns: int | None, to_mono_ns: int | None, reason: str)`; `ClientState(verified: bool, answering: bool, cursor: StreamCursor | None, capabilities: Capabilities | None, pending_activation: bool | None, last_error: str | None)`.
- Produces: `IodClient(socket_path: Path, *, peer_reader: Callable, path_verifier: Callable, clock_ns: Callable[[],int], on_gap: Callable[[GapNotice],None], on_change: Callable[[],None])` with `connect() -> None`, `pump() -> None`, `query(request: QueryRequest) -> concurrent.futures.Future[dict]`, `close() -> None`, `state: ClientState`. Transport is nonblocking, with injected connectors in tests; futures never block the Qt thread.
- Produces: `AppRegistry.id_for(app_key: str) -> int` and `AppRegistry.key_for(app_id: int) -> str`, stable presentation IDs for local/remote AppKeys; bounded read-through result cache keyed by complete request and committed remote generation, not by local integer IDs.

**Threat closed:** a non-root endpoint, delayed old-epoch reply, repeated snapshot or reconnect gap must not be trusted as current data, double-applied or interpreted as a period of measured zero.

- [ ] **Step 1: Write the failing test**

```python
from appio.iodclient import CursorTracker
from appio.ipc_models import StreamCursor


def test_duplicate_old_epoch_and_old_boot_cannot_reenter_current_stream():
    tracker = CursorTracker()
    tracker.accept_status({"protocol": 1, "type": "status", "boot_id": "boot-a",
                           "iod_epoch": 7, "seq": 0, "source": "iod"})
    assert tracker.accept(StreamCursor("boot-a", 7, 1))
    assert not tracker.accept(StreamCursor("boot-a", 7, 1))
    assert not tracker.accept(StreamCursor("boot-a", 6, 999))
    tracker.begin_connection()
    tracker.accept_status({"protocol": 1, "type": "status", "boot_id": "boot-b",
                           "iod_epoch": 1, "seq": 0, "source": "iod"})
    assert tracker.accept(StreamCursor("boot-b", 1, 1))
    assert not tracker.accept(StreamCursor("boot-a", 8, 999))
```

The tracker unit above receives the already-validated identity portion; full envelope tests must exercise Task 1 validation first. Add fake-server/non-root refusal before callback/cache mutation, fragmented status/snapshot/error frames, stale since→gap, current since→replay once, EOF/reconnect backoff with a virtual clock, malformed capabilities, failed-query futures, an out-of-order old connection result, response-size cap and five-second no-answer state. Known monotonic gap bounds emit one callback; unknown bounds must not become `[0,now)`. Missing file capability remains unavailable, not zero files. A cache never writes daemon sample rows into the GUI store.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_iodclient.py`

Expected: FAIL on missing client module.

- [ ] **Step 3: Write the minimal implementation**

Verify directory/socket metadata and server UID 0 before trusting any reply. Request status, establish the currently advertised boot/epoch, and subscribe using the last matching cursor. Increment a connection generation and reject results/callbacks from older generations. Historical replies have request IDs and snapshot identity; they do not advance subscription seq. Duplicate/outdated snapshots are ignored before any effect. A new boot may legitimately reset epoch/seq, but an old connection may never switch authority back.

```python
if cursor.boot_id != self.active_boot or cursor.iod_epoch != self.active_epoch:
    return False
if self.current is not None and cursor.seq <= self.current.seq:
    return False
self.current = cursor
return True
```

Use bounded exponential reconnect delays of 0.25, 0.5, 1, 2, then 5 seconds, driven by `pump()`/clock with no sleeps. Cap 64 pending requests, 4 MiB cached responses and five-second per-request lifetime; evict cache entries whole, retaining coverage/error state. A verified status is required to label `answering`; service enablement alone cannot set it. Emit transport gap notices for Task 12 to persist as local read-through provenance. Historical queries may recover transport-missed data from the daemon DB; never convert a transport loss into invented permanent loss in that authoritative DB. Preserve exact remote A22 errors when decoding typed results.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Remove root-peer verification and prove the fake-server test fails; remove connection-generation/seq rejection and prove duplicate/old-result tests fail. Restore both.

- [ ] **Step 5: Commit**

```bash
git add src/appio/iodclient.py tests/p3/test_iodclient.py
git commit -m "feat(client): verify daemon authority and reconnect without duplicate observations"
```

## Task 12: Coordinate one collector and route history by proven ownership

**Implementer tier:** Capable

**Scheduling:** `depends_on: [4,5,10,11]`; `lane: ownership`; `sequence: 3`.

**Files:**

- Create: `src/appio/collector_owner.py`
- Create: `src/appio/history_router.py`
- Test: `tests/p3/test_collector_owner.py`
- Test: `tests/p3/test_history_router.py`

**Interfaces:**

- Consumes: Task 4 `HandoverOffer`, `capture_handover`, canonical validation and `AppIoStore.install_handover`; Task 5 `LiveCollector(..., source="gui", baseline_only=False, resume_handover=None)`, `tick`, `quiesce`, `close`; Task 10 `ExportResult`/versioned helper result; Task 11 `IodClient.state`, `query(request)->Future[dict]`, `GapNotice`, `AppRegistry`; existing `QueryService` methods and `Window`/typed result containers.
- Produces: `ServiceFacts(enabled: bool | None, active: Literal["absent","inactive","active","unknown"], daemon_verified: bool, answering: bool, pending_activation: bool | None)`; `OwnerDecision(owner: Literal["gui","iod","unknown"], may_collect_local: bool, state: str)`; `decide_owner(facts: ServiceFacts) -> OwnerDecision`.
- Produces: `GuiLease(path: Path)` with `acquire() -> bool`, `release() -> None`, holding an exclusive `flock` on a per-UID runtime file through a pinned safe parent; lease failure yields a viewer, never a second writer.
- Produces: `CollectorCoordinator(*, local_factory: Callable[[],LiveCollector], client: IodClient, history: HistoryRouter, lease: GuiLease, clock: Callable[[],tuple[int,int]])` with `reconcile(facts: ServiceFacts) -> OwnerDecision`, `tick_local() -> None`, `accept_export(result: ExportResult) -> None`, `close() -> None`. Transitions are serialized on one owner thread; local construction is lazy.
- Produces: `HistorySegment(collector_id: str, owner: str, boot_id: str, start_ns: int, end_ns: int, proven: bool)`; `HistoryRouter(local: QueryService, client: IodClient, registry: AppRegistry)` with `prepare(window: Window) -> None`, `ready(window: Window) -> bool`, `ranking(window: Window, metric: str) -> list[AppTotal]`, `disk_share(app_id: int, window: Window) -> DiskShare`, `series(app_id: int, window: Window) -> Series`, and `view_state: str`. The synchronous accessors read completed cached results; `prepare` schedules remote work and never blocks Qt.

**Threat closed:** socket failure, stale enablement or two GUIs must not create a second writer for daemon-owned intervals, and cross-store ID collisions/overlapping totals must not silently double count history.

- [ ] **Step 1: Write the failing test**

```python
from appio.collector_owner import ServiceFacts, decide_owner


def test_silent_enabled_service_is_not_permission_to_collect_locally():
    decision = decide_owner(ServiceFacts(True, "unknown", False, False, None))
    assert decision.owner == "unknown"
    assert not decision.may_collect_local
    assert decision.state == "enabled, not answering"


def test_only_proven_absence_allows_gui_fallback():
    local = decide_owner(ServiceFacts(False, "inactive", False, False, False))
    remote = decide_owner(ServiceFacts(True, "active", True, True, False))
    assert local.owner == "gui" and local.may_collect_local
    assert remote.owner == "iod" and not remote.may_collect_local
```

Add a fake local factory that counts opens/ticks: verified daemon startup opens none; a disabled/inactive daemon opens exactly one collector with a held lease; a second GUI opens none. On enable, cancel/await the current local tick, quiesce once, then admit no further GUI frame. On a silent daemon preserve unknown ownership instead of fallback. After verified shutdown/export, validate root cutoff, seed incoming GUI state and preserve/consume pending credits without counting bytes in the unmeasured transition. Inject crashes before/after quiesce, root baseline, export publication and incoming install; reconcile with gaps/uncertain overlap, not a guessed cutoff.

History fixtures must allocate the same app at local ID 17 and daemon ID 3, and different apps at the same ID in each store. Query across a cutoff, a straddling raw bucket, negative residual, root interval missed by the client, two boots, pruned raw evidence and unavailable device data. Assert no duplicate receipt/bytes in ranking, series, boot/lifetime totals and coverage. A remote query completing for an old window must never populate the current window's cache.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_collector_owner.py tests/p3/test_history_router.py`

Expected: FAIL on missing ownership/router modules.

- [ ] **Step 3: Write the minimal implementation**

Use service-active state plus a verified socket, not a click or merely an absent socket pathname. Active or unknown service state never authorizes local fallback. While transitioning or awaiting history, present unavailable/loading state with no stale numbers under a changed scope. Closing the GUI only closes client/local lease; it never disables the daemon.

```python
if facts.daemon_verified and facts.answering:
    return OwnerDecision("iod", False,
                         "on, needs activation" if facts.pending_activation
                         else "on, answering")
if facts.active in ("absent", "inactive"):
    return OwnerDecision("gui", True, "off")
return OwnerDecision("unknown", False,
                     "enabled, not answering" if facts.enabled else "unavailable")
```

Serialize collection and transition requests; local quiesce must finish before helper enable starts. Do not upload the resulting GUI checkpoint to the root service (D3). The daemon takes fresh all-PID/device baselines at its own actual start. Use its verified ownership start to retain only provably earlier local contributions; exclude any straddling delta whole and record missing time/uncertain overlap. On disable, use the final exported checkpoint only if it matches the helper's boot/epoch/seq/cutoff, an orderly stopped service and the complete preceding ownership chain. If an earlier unclean enable left that chain unavailable, retain the export read-only and start GUI collection with a conservative fresh baseline/gap instead of inserting a first handover that would invalidate earlier GUI history. Never overwrite the user's local history DB with an export: open the export read-only as another history source.

When the first incoming sample is later than the outgoing cutoff, persist the exact transition gap; advance baseline/credit bookkeeping from known evidence but discard the entire cross-gap task/device delta. Preserve remaining known credits and mark uncertain conservation/rates incomplete. Without a valid offer, baseline fresh identities and expose the unclean transition. No path may claim clean continuity merely because both processes survived.

Persist bounded transport gap notices in the GUI's own provenance/gap rows using the existing reason vocabulary; they must not mutate the root DB or override complete authoritative history subsequently fetched from it. Route each boot-scoped interval by proven `collector_interval`/handover evidence, choosing daemon ownership where established. AppKey joins and boot chronology are mandatory. Reduce coverage with the existing operators and preserve signed residuals; sum only disjoint owned byte contributions, take maxima of the original interval peaks and never add unrelated local surrogate IDs.

Do not add two precomputed whole-window totals and subtract an estimated overlap. Apply cutoff corrections while raw evidence remains; for an older mixed rollup whose exact subinterval cannot be reconstructed, return remaining observed data with incomplete/not-retained coverage, or explicit unavailability when the result contract cannot represent it. A supported scope must never silently widen. Keep legacy untouched whole-boot/lifetime summaries answerable where ownership is already settled. Successful root history queries repair missed transport visibility; absence of a GUI does not become a root collection gap.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command. Expected: PASS. Allow local collection on socket timeout alone and prove the silent-service/factory-count tests fail; use local numeric IDs for joins and prove the two-store collision fixture fails. Restore both invariants.

- [ ] **Step 5: Commit**

```bash
git add src/appio/collector_owner.py src/appio/history_router.py tests/p3/test_collector_owner.py tests/p3/test_history_router.py
git commit -m "feat(ownership): coordinate one collector and reconcile proven history intervals"
```

## Task 13: Add the single settings switch, consent and status deep links

**Implementer tier:** Advanced

**Scheduling:** `depends_on: [9,10,12]`; `lane: gui`; `sequence: 1`.

**Files:**

- Create: `src/appio/setup_client.py`
- Create: `src/appio/ui/collector_window.py`
- Modify: `src/appio/__main__.py:23-123` (lazy P3 interactive composition, preserving legacy once execution)
- Test: `tests/p3/test_setup_client.py`
- Test: `tests/p3/test_collector_window.py`
- Test: `tests/p3/test_p3_entry.py`

**Interfaces:**

- Consumes: Task 9 activation states/choice wording and integer evidence; Task 10 helper result JSON/six verbs; Task 12 `CollectorCoordinator.reconcile(facts)`, `tick_local`, `accept_export`, `HistoryRouter.prepare/ready/ranking/disk_share/series`; existing `AppIoWindow(query_service, boot_id, end_mono_ns)` as an unchanged P2 child view.
- Produces: `SetupReply(exit_code: int, stderr_first_line: str, body: dict | None)`; `SetupClient(*, launch: Callable[[tuple[str,...]], Future[CommandResult]], probe: Callable[[],ServiceFacts])` with `invoke(verb: str) -> Future[SetupReply]`, `refresh() -> ServiceFacts`. Only explicit consent handlers call invoke; periodic refresh uses unprivileged service/socket checks, not repeated pkexec authentication.
- Produces: `CollectorWindow(coordinator: CollectorCoordinator, history: HistoryRouter, setup: SetupClient)` with `open_settings(*, focus: str = "system_services") -> None`, `open_files_unavailable() -> None`, `refresh() -> None`; exactly one checkable `System services (root)` control, located in Settings.
- Produces: `run_legacy_once(args: argparse.Namespace) -> int` in `__main__.py`, extracting only the existing collector construction/tick/finally-close branch; `run_interactive(args: argparse.Namespace) -> int` in the new P3 window module. Interactive code must branch before constructing any local collector; the coordinator's lazy factory owns that decision.
- Produces: a P3 interactive launcher using asynchronous helper/client work and Qt queued signals, leaving `appio --once` and direct `AppIoWindow` construction in the P1/P2 execution boundary. `--offscreen` is still applied before Qt construction.

**Threat closed:** navigation, stale clicks or a dismissed prompt must not invoke privilege changes; an unresponsive helper must not freeze Qt, duplicate an operation or display an unprobed successful switch.

- [ ] **Step 1: Write the failing test**

```python
from concurrent.futures import Future
from appio.setup_boundary import CommandResult
from appio.setup_client import SetupClient
from appio.collector_owner import ServiceFacts


def test_polkit_dismissal_retains_exact_code_stderr_and_probed_off_state():
    calls = []
    def launch(argv):
        calls.append(argv)
        done = Future()
        done.set_result(CommandResult(126, "", "Authentication dismissed\nextra"))
        return done
    client = SetupClient(launch=launch,
        probe=lambda: ServiceFacts(False, "inactive", False, False, False))
    result = client.invoke("enable").result(timeout=1)
    assert calls == [("/usr/bin/pkexec", "/usr/bin/appio-setup", "enable")]
    assert result.exit_code == 126
    assert result.stderr_first_line == "Authentication dismissed"
    assert client.refresh().enabled is False
```

In offscreen pytest-qt tests click main-banner `Open settings` and the Files-unavailable deep link; assert the settings focus changes and the helper invocation log, daemon state and local collector count do not. Assert there is exactly one root switch and no new tray/Overview/Files-table widgets. Toggle with cancelled consent, pkexec 126/127/failure, enabled-but-silent probe, pending activation, helper missing and container reason; every displayed state must follow the probe. While a Future is pending, process a Qt heartbeat and reject a second operation. Restart choice must display the session-ending warning and need a distinct confirmation; reboot choice never runs restart/reboot. Finish with a fresh-process `--once` import guard and the unchanged legacy acceptance suite.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_setup_client.py tests/p3/test_collector_window.py tests/p3/test_p3_entry.py`

Expected: FAIL on missing P3 UI/client modules.

- [ ] **Step 3: Write the minimal implementation**

Use a fixed argv vector through QProcess or a bounded subprocess worker; no shell/interpolated command strings. Decode at most 64 KiB helper stdout and bounded stderr, require version/verb/UID consistency, and retain the actual exit plus first stderr line. Serialize operations and keep the switch disabled until the Future completes and service/socket probe refreshes. The switch reflects enabled/probed state rather than last click; rendering uses signal blocking so a refresh cannot trigger another pkexec.

The first enable consent explains all-PID access, machine-wide collection, persistent root history, delegation/group changes and the user-readable export on disable. After acceptance: quiesce local collection, invoke `install-delegation`, offer `Restart my user manager now` or `Activate after reboot`, and invoke `enable`. If restart was chosen, ensure enable succeeds before explicitly confirmed `restart-user-manager`, since that command ends the running GUI's session. Reboot choice leaves pending; no automatic reboot command exists. On subsequent explicit activation recheck invoke `status`; normal periodic state refresh does not start a write probe or produce authentication prompts. Disable requires its own consent/pkexec, then imports only verified export metadata and reconciles the actual service state.

Show exact status text `off`, `on, answering`, `enabled, not answering`, `on, needs activation`, and `delegation present, accounting inactive` where applicable. Display pending activation even across GUI restart via authenticated status/helper evidence; unknown state stays unknown. Current-process socket permission failures after group installation remain visible until activation refreshes the session; a root helper being able to connect does not prove the existing GUI can connect.

Wrap the unchanged P2 view in a separate P3 shell with a status banner and Settings dialog. Add an unavailable Files route whose `Open settings` action only navigates/focuses the single settings switch. It must say file activity is unavailable and must not promise that enabling the P3 daemon supplies F. `HistoryRouter.prepare(window)` runs before rendering; while `ready(window)` is false hide/clear stale numeric detail and show loading/unavailable text. Use queued Qt updates on worker completion; all widget mutation stays on Qt's thread.

```python
if args.once:
    return run_legacy_once(args)  # only P1/P2 imports on this branch
from appio.ui.collector_window import run_interactive
return run_interactive(args)
```

Define `run_interactive(args: argparse.Namespace) -> int` in the new P3 window module; it constructs the client/coordinator/router and returns the Qt exit code. Preserve command-line database/proc/cgroup/interval controls as unprivileged GUI inputs only; never pass arbitrary paths through the root helper or daemon wire. Do not edit P2 window object names/actions or weaken `test_no_later_scope.py`.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command and `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_ui.py tests/test_live.py tests/acceptance/test_no_later_scope.py`.

Expected: PASS. Wire a deep link to `invoke("enable")` and prove the navigation/no-operation test fails; initialize the switch from the last click and prove the dismissal/probe assertion fails. Restore both behaviors.

- [ ] **Step 5: Commit**

```bash
git add src/appio/setup_client.py src/appio/ui/collector_window.py src/appio/__main__.py tests/p3/test_setup_client.py tests/p3/test_collector_window.py tests/p3/test_p3_entry.py
git commit -m "feat(gui): add consented collector settings and status navigation"
```

## Task 14: Verify the P3 round trip and document separate privileged acceptance

**Implementer tier:** Advanced

**Scheduling:** `depends_on: [1,2,3,4,5,6,7,8,9,10,11,12,13]`; `lane: acceptance`; `sequence: 1`.

**Files:**

- Create: `tests/p3/acceptance_harness.py`
- Test: `tests/p3/test_p3_acceptance.py`
- Test: `tests/p3/test_p3_adversarial.py`
- Create: `docs/acceptance/appio-p3-root.md`
- Create: `tools/acceptance/appio-p3-root.py`

**Interfaces:**

- Consumes: Task 6 `Daemon.start/step/stop`/committed cursor, Task 10 `dispatch(invocation, *, deps)->dict` and export, Task 11 verified client, Task 12 coordinator/router, Task 13 GUI entry/consent; their exact public signatures remain as declared in those tasks. This task changes no implementation or earlier test files.
- Produces: test-only `P3Harness(root: Path)` context manager with `enable() -> None`, `advance(seconds: int) -> None`, `open_gui() -> None`, `close_gui() -> None`, `disable_and_export() -> ExportResult`, `daemon_rows() -> tuple`, `export_rows(path: Path) -> tuple`, `query(window: Window) -> object`, and `commands: list[tuple[str,...]]`. It composes real production service/store/protocol/client/coordinator code, fixture proc/cgroup data, actual unprivileged AF_UNIX transports, virtual time and injected credentials/filesystem metadata/command runner. All simulated privilege/session side effects are recorded rather than executed.
- Produces: an optional root acceptance script outside pytest discovery; dry-run by default. Real execution requires `--run`; destructive user-manager testing additionally requires `--allow-session-termination`. These flags belong to the acceptance script only and are not helper options.

- [ ] **Step 1: Write the failing test**

```python
from acceptance_harness import P3Harness


def test_daemon_survives_gui_close_and_disable_exports_final_commits(tmp_path):
    with P3Harness(tmp_path) as harness:
        harness.enable()
        harness.open_gui()
        harness.advance(2)
        harness.close_gui()
        before = harness.daemon_rows()
        harness.advance(6)
        after = harness.daemon_rows()
        assert len(after) > len(before)
        result = harness.disable_and_export()
        assert harness.export_rows(result.path) == harness.daemon_rows()
        assert result.cutoff_mono_ns is not None
        assert ("/usr/bin/systemctl", "disable", "--now", "appio-iod.service") in harness.commands
        assert not any("restart" in item for item in harness.commands)
```

The harness must generate a positive per-tick fixture counter increment, compare every schema-v1 data table in canonical primary-key order including checkpoint/receipt/coverage, and drive actual production composition rather than returning expected canned rows. Extend acceptance with enable/pending/reboot-choice→accounting success→disable→GUI fallback, two GUIs, daemon crash/restart, false root server, malformed/oversized frames, exact 4000/500/256 KiB limits, slow/healthy clients, stale cursor/gap, helper 126, export collision/WAL and all failure injection points. Include a nonzero-credit cutoff and a raw/rollup-crossing query; lossless pure handover and D3 degraded live enable must be separate named cases with exact coverage expectations.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py`

Expected: FAIL before the new harness/tests are implemented; once the harness exists, temporarily suppress the real daemon's post-GUI-close ticks and confirm the first assertion fails. A harness-only import failure is insufficient final falsification evidence.

- [ ] **Step 3: Write the minimal implementation**

Implement the harness using production dependency seams and deterministic clocks, with no patched sampler/store/query algorithms. Restrict fake command handling to the exact expected absolute argv tuples; an unexpected privileged command must raise immediately. Assert every filesystem path opened for mutation stays within the temporary root. The helper's tests simulate root ownership/chown without requiring a root process. Test verified-peer outcomes through injected kernel metadata and also exercise the actual socket transport/framing. Do not skip a case merely because the host lacks systemd, polkit, root or a user bus.

Write the root runbook with prerequisites and exact installation destinations for the P3 runtime artifacts, explicitly disabled initial service, root/group/file modes and trusted interpreter/modules. P5 packaging remains deferred. The script's dry-run prints the proposed fixed operations and performs none. Its optional real execution must verify:

1. Initial service is disabled; ordinary GUI fallback is single-owner and has a visible absent-daemon banner.
2. An actual pkexec consent installs fixed drop-ins and appends the invoking account to appio, preserving existing groups. Both restart and reboot choices are shown. Default validation selects reboot and proves no session restart occurred.
3. A fresh user manager/session and a block-backed probe show expected 16 MiB write bytes within 5%, with transient unit/file cleanup. Controller presence alone is insufficient; record D2 target/filesystem and actual evidence.
4. Service becomes answering through a verified root socket with correct modes. Only iod frames advance while authoritative; pending/probe failures remain visible. Distinguish a D3 explicit degraded enable from an exact clean handover.
5. GUI closes for >5 s, daemon data advances, and reopening queries historical data it did not subscribe to. Replay/gaps carry boot/epoch/seq, and no missing time becomes zero.
6. Disable has its own polkit authorization, stops/disables the service, retains delegation and publishes a complete caller-owned 0600 export including final WAL state. Root history remains preserved. GUI resumes only after inactive proof/lease and records any actual transition gap.
7. The optional destructive branch plainly states that the session will end and runs only when `--allow-session-termination` was explicitly supplied. No automatic reboot is part of the script.

The runbook records actual versus unrun checks and all D1–D5 rulings/limitations. A unit-test pass cannot close D2's real-accounting evidence or D3's missing trusted upload protocol. If a final test exposes a production defect outside this task's allowlist, report it to the producer's review and rerun after that correction; do not broaden this task's writes.

- [ ] **Step 4: Run the test and confirm it passes**

Run the Step 2 command, then the complete unprivileged suite once:

```bash
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q
python tools/acceptance/appio-p3-root.py
```

Expected: PASS with no privileged operation or real session dependency; the second command is dry-run only. Preserve the complete command/exit evidence. Rerun focused tests after any new change/failure; do not run the privileged script with `--run` as part of the default suite. For decisive falsification, remove only the live daemon's GUI-independent scheduling, only root-peer validation, only the handover transaction barrier and only parent-directory fsync in separate throwaway working copies; each corresponding acceptance assertion must fail. Restore production mechanisms before integration.

- [ ] **Step 5: Commit**

```bash
git add tests/p3/acceptance_harness.py tests/p3/test_p3_acceptance.py tests/p3/test_p3_adversarial.py docs/acceptance/appio-p3-root.md tools/acceptance/appio-p3-root.py
git commit -m "test(p3): verify root-free lifecycle and document opt-in host acceptance"
```
