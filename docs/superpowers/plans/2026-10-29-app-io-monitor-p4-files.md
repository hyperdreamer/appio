# P4 Per-File eBPF Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking; leave the plan unchanged during execution.

**Goal:** Add an opt-in, loss-accounted eBPF file collector that materialises per-app hourly file observations, preserves the existing process/device collector when F is unavailable, exposes the existing file query contract, and replaces the P3 unavailable Files page with an honest offscreen-testable Files surface.

**Architecture:** A CO-RE/libbpf loader owns six VFS `fexit` hooks and a lock-free-at-the-boundary double-buffered map drain. A rootless Python adapter validates the loader vector, a file attribution layer maps cgroup IDs to the committed frame's app ownership, and the existing `HourlyAccumulator`/`FileHourStore` remain the only aggregation and SQLite materialisation path. The daemon runs this F pipeline beside `LiveCollector` on the same owner loop; F errors update only F capability/status and coverage, while T/D cadence, recovery, handover, and IPC continuity retain their current contracts. `HistoryRouter` adds an asynchronous file-view request over the already validated local query/RPC surfaces, and `CollectorWindow` renders the Files page without importing the daemon or BPF modules into `AppIoWindow`.

**Tech Stack:** Python 3.14, SQLite schema-v1, PyQt6, pytest/pytest-qt, C23-compatible system compiler, clang 23.1.3 BPF backend, libbpf 1.7, Linux BTF/CO-RE, and the existing Unix IPC/HMAC cursor protocol. No new Python runtime dependency is permitted.

## Global Constraints

- Design revision 5 and ratified specification decisions A01-A22 are authoritative; operator scope in this plan supersedes the design/P3 prose that assigns the Files table to P5.
- P4 owns BPF/per-file production and the Files surface inside `CollectorWindow`; P5 owns no P4 Files implementation in this plan, and `AppIoWindow` remains the P1/P2 Overview surface.
- Preserve `tests/acceptance/test_no_later_scope.py` byte-for-byte and preserve its fresh-process import guard: `AppIoWindow` must not import daemon, socket, BPF, polkit, privilege, setup, tray, packaging, or Files implementation modules.
- Python `>=3.14`, PyQt6 `>=6.6`, pytest `>=8`, pytest-qt `>=4.4`, and no new runtime dependencies.
- Tests are rootless and offline by default. No test may require a live BPF load, root, a live `/proc`, or a live `/sys`; use synthetic loader vectors, fixture records, and injected filesystem/clock seams. Task 7 is the single opt-in privileged acceptance task.
- The host BPF prerequisites are clang 23.1.3, libbpf headers/libraries, readable `/sys/kernel/btf/vmlinux`, and `unprivileged_bpf_disabled=2`; rootless compilation must be checked, while loading is gated separately by `APPIO_P4_BPF_ACCEPTANCE=1` and an effective-root check.
- The daemon remains the single writer/owner loop. `LiveCollector` and its `AppIoStore.record_tick` keep the existing outer `BEGIN IMMEDIATE`, receipt-savepoint, publish-after-commit, stale-frame, recovery-backoff, and 10-second persistence cadence invariants.
- F is independent from T/D: a failed build, attach, drain, counter conversion, filesystem identity lookup, or F SQLite operation must not make `last_error` claim T/D failure, stop T/D ticks, enter T/D handover, or advertise an empty file table as evidence of no file activity.
- Every accepted monotonic endpoint carries its boot identity. Use persisted `boot.boot_order` plus within-boot monotonic order for path/path-alias first/last endpoints; reject missing or conflicting chronology and never compare monotonic values across boots or infer order from wall time.
- Count only positive VFS return values. Keep counted top-100 omissions, uncounted BPF map-overflow bytes/files, skipped non-files, skipped unsupported filesystems, weak identity rows, and unknown-owner overflow as separate fields. Never turn an unassigned event into an app row.
- File observations are hourly and independent of raw process retention: one finalized snapshot per `(boot_id, AppKey, hour)`, top 100 only after all drains for that hour, file rows and file coverage retained for 14 days, raw process observations retained for 24 hours, and A13 deletion units/order preserved exactly.
- Preserve unavailable-is-`None` and coverage-in-every-result semantics. Empty rows with complete zero coverage are valid only after an authoritative F hour was observed; unavailable or unrepresentable windows must carry an explicit reason and must not be rendered as zeroes.
- Each task's `Create`, `Modify`, and `Test` paths are its ownership allowlist. Tasks execute sequentially within a lane; no task may edit another task's owned files to repair an interface silently. Every task must commit only its allowlist after its focused test command passes.

## Task 1: Build the CO-RE VFS producer and loader ABI

**Implementer tier:** Frontier

**Files:**

- Create: `appio_iod/bpf/fileio.bpf.c`
- Create: `appio_iod/bpf/fileio_loader.c`
- Create: `appio_iod/bpf/fileio_protocol.h`
- Create: `appio_iod/bpf/fileio_types.h`
- Create: `appio_iod/bpf/Makefile`
- Test: `tests/p4/test_bpf_program.py`
- Test: `tests/p4/test_bpf_build.py`

**Interfaces:**

- Consumes: Linux BTF CO-RE types for `struct file`, `struct inode`, `struct super_block`, `struct dentry`, and the six fexit entry points `vfs_read`, `vfs_write`, `vfs_readv`, `vfs_writev`, `vfs_iter_read`, and `vfs_iter_write`.
- Produces: ABI version `1` in `fileio_protocol.h`; a minimal `fileio_types.h` containing only the CO-RE-preserved declarations needed for the six hook signatures and file/dentry/inode/superblock fields (so the build does not depend on unavailable `bpftool` or a generated `vmlinux.h`); a loader command/response stream with `HELLO`, `FLIP_DRAIN`, and `CLOSE` opcodes; a `HELLO` response with attach bitmask, map capacity, errno, and bounded verifier text; and a `DRAIN` response containing fixed-width file records, per-cgroup counters, hook hits, skipped counts, overflow counts, the drained slot, and the caller-supplied `(epoch, seq)`.
- Produces: two inner `BPF_MAP_TYPE_HASH` maps of `max_entries=262144` behind one `BPF_MAP_TYPE_ARRAY_OF_MAPS`, with no LRU eviction; a control/active-slot map; per-cgroup overflow values including reserved cgroup key `0`; per-cgroup six-hook hit counters; and per-cgroup skipped-nonfile/skipped-unsupported counters.
- Produces: a userspace loader that reports an error rather than acknowledging a drain until the old slot has been quiesced, fully enumerated, and cleared.

- [ ] **Step 1: Write the failing tests**

  Add static contract tests that read `fileio.bpf.c` and assert all six exact `SEC("fexit/<hook>")` declarations, a positive-return guard before counting, `S_ISREG`/regular-mode filtering, `sb->s_bdev != NULL` filtering, `bpf_get_current_cgroup_id`, the four-part file key including `i_generation`, a 40-byte name buffer with length, two array-of-maps slots, `BPF_MAP_TYPE_HASH`, `max_entries=262144`, no `BPF_MAP_TYPE_LRU_HASH`, and an explicit failed-update path that increments overflow counters. Assert the source does not call the iter hook from the readv/writev hook bodies.

  Add a build test that invokes `make -C appio_iod/bpf check` in a temporary output directory and asserts the BPF object and loader compile without root. The test must inspect the command's stderr on failure so a missing clang/libbpf/BTF prerequisite is actionable rather than silently skipped. Add a protocol test that packs and unpacks one event and one cgroup-stat record using the exact little-endian layouts declared in `fileio_protocol.h`, including a 40-byte name and a zero reserved cgroup key.

  Falsifiers: removing one hook declaration, changing `ret > 0` to a nonpositive-inclusive test, changing the map to LRU, lowering the map capacity, dropping `i_generation`, making `vfs_readv` call `vfs_iter_read`, omitting the failed-update counter, or changing any wire field width must make a named assertion fail. A build that only succeeds when executed as root or that emits no object/loader is a failure.

- [ ] **Step 2: Run the tests and confirm they fail**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_bpf_program.py tests/p4/test_bpf_build.py
  ```

  Expected result: FAIL because the BPF source, protocol header, Makefile, loader, and test fixtures do not exist yet. Do not mark the build test skipped for an ordinary compiler or header failure on this host.

- [ ] **Step 3: Write the minimal implementation**

  Implement `fileio.bpf.c` with one shared accounting helper called by each fexit program. Include the checked-in `fileio_types.h` CO-RE declarations, marked with libbpf's preserve-access-index attribute, for the hook argument types and only the fields actually read. The declarations must be verified against the host BTF during the opt-in load test; they are not a substitute for runtime BTF validation. The helper must first reject `ret <= 0`, increment the hook counter for the current cgroup, obtain the file/inode/dentry/superblock from the hook-specific arguments, increment the skipped counter for non-regular or non-block-backed objects, and only then update the active inner hash. Store counts as unsigned 64-bit fields for reads, writes, read bytes, write bytes, `name_len`, and `name[40]`; use the current dentry basename without attempting path reconstruction. Convert the kernel device number to a major/minor pair in the loader, not by using a display name. A failed new-key update must increment that cgroup's overflow file/byte counters and must never evict an existing key.

  Make each hook increment a global in-flight counter before reading the active slot and decrement it after the map update. Make the loader flip the active slot, wait for the old-slot in-flight counter to reach zero with a bounded polling deadline, enumerate every old-slot key and cgroup-stat key, clear each key only after it has been copied to the response, and acknowledge the response only after the old slot is empty. Reject a second flip while the prior slot is still draining. This is the quiescence barrier that makes double buffering lossless at the flip boundary; a timeout is an F error, not a successful empty drain.

  Define fixed packed wire structs in `fileio_protocol.h`: a versioned command header; a `HELLO` header with six-hook bitmask, map capacity, errno, and verifier length; a `DRAIN` header with epoch, seq, slot, event/stat counts, completion flag, and diagnostic length; a file record with cgroup ID, major, minor, inode, generation, four counters, name length, and 40-byte name; and a cgroup-stat record with six hook counters, overflow file/byte counters, and skipped counters. Bound diagnostic and verifier payloads before writing them. `fileio_loader.c` must send no human log text on stdout; diagnostics are framed protocol payloads and stderr only for build/runtime administrator logs.

  Make `make check` compile the BPF object with the host clang BPF target, compile the loader against libbpf/libelf, and write all outputs under a caller-provided temporary directory. Do not install, load, attach, or alter a system service from the Makefile.

- [ ] **Step 4: Run the tests and confirm they pass**

  Run the focused pytest command from Step 2, then run:

  ```bash
  make -C appio_iod/bpf check OUT_DIR="$(mktemp -d)"
  ```

  Expected result: the static contract tests pass, the protocol round-trip passes, clang emits the BPF object, and the loader links without root. The test must show the loader's six-hook mask and map capacity in its compiled contract inspection; no privileged load is performed here.

- [ ] **Step 5: Commit**

  ```bash
  git add appio_iod/bpf/fileio.bpf.c appio_iod/bpf/fileio_loader.c appio_iod/bpf/fileio_protocol.h appio_iod/bpf/fileio_types.h appio_iod/bpf/Makefile tests/p4/test_bpf_program.py tests/p4/test_bpf_build.py
  git commit -m "feat(bpf): add CO-RE per-file accounting producer"
  ```

**Scheduling:** `depends_on: []; lane: bpf; sequence: 1`.

## Task 2: Add the rootless lossless drain adapter

**Implementer tier:** Advanced

**Files:**

- Create: `src/appio/bpf_fileio.py`
- Test: `tests/p4/test_bpf_drain.py`

**Interfaces:**

- Consumes: Task 1 ABI version `1`, `fileio_protocol.h` fixed-width command/response records, and the six-hook attach bitmask.
- Produces: `FileEvent(cgroup_id: int, dev: str, inode: int, generation: int, basename: bytes, name_len: int, reads: int, writes: int, bytes_r: int, bytes_w: int)`.
- Produces: `CgroupFileStats(cgroup_id: int, hook_hits: HookHits, overflow_files: int, overflow_bytes_r: int, overflow_bytes_w: int, skipped_nonfile: int, skipped_unsupported_fs: int)`; cgroup ID `0` is reserved for loader-reported unknown-owner overflow and may not be treated as a real process cgroup.
- Produces: `FileDrain(receipt: Receipt, events: tuple[FileEvent, ...], stats: tuple[CgroupFileStats, ...], complete: bool, diagnostic: str | None)` and `FileSourceError(errno: int | None, verifier: str | None, detail: str)`.
- Produces: `BpfFileSource.start() -> FileAvailability`, `BpfFileSource.drain(receipt: Receipt) -> FileDrain`, and `BpfFileSource.close() -> None`; `FileAvailability` exposes `file_io`, `bpf_loaded`, and a bounded administrator-facing diagnostic.
- Produces: an injected `LoaderProtocol` and a deterministic `VectorLoader` test shim; production code never imports a test shim and never calls a privileged operation while decoding a vector.

- [ ] **Step 1: Write the failing tests**

  Add tests for: successful HELLO requiring all six attach bits; one old-slot event and one post-flip event being returned on separate drains with no duplicate; exact conversion of major/minor to canonical decimal `dev`; rejection of leading-zero device strings, booleans, negative counters, generation values above the durable limit, malformed names, truncated payloads, duplicate cgroup stats, and a nonzero reserved-key collision; monotonic receipt enforcement; and bounded propagation of errno/verifier text through `FileSourceError`.

  Add a loss test using `VectorLoader`: queue a vector in slot A, flip to B, queue a vector in B, make the first drain report all A records and clear A, then assert the second drain reports B. Make a second shim fail while waiting for in-flight quiescence and assert the adapter returns an exception rather than an empty `FileDrain`. Add an overflow vector with one real cgroup and cgroup `0`, and assert their counters remain separate. Add hook-hit and skipped-counter assertions even when `events == ()`.

  Falsifiers: omitting the second slot, accepting a duplicate receipt, dropping a trailing event, treating reserved key `0` as an app cgroup, converting a failed drain to `complete=True`, accepting malformed widths, or truncating a verifier diagnostic without retaining its errno must fail a named test. A test that passes only because the production loader is unavailable is invalid; all tests use the injected shim.

- [ ] **Step 2: Run the tests and confirm they fail**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_bpf_drain.py
  ```

  Expected result: FAIL because `appio.bpf_fileio` and its typed drain/loader seam do not exist.

- [ ] **Step 3: Write the minimal implementation**

  Parse the fixed protocol with `struct.Struct` formats that exactly match `fileio_protocol.h`; use `read_exact` semantics for every frame and reject trailing bytes. Validate every integer with the existing SQLite-range conventions before constructing Python records, but retain raw unsigned values in the exception detail when they exceed the durable range. Decode device major/minor into the canonical `major:minor` string and retain basename bytes plus `name_len`; do not Unicode-normalize or silently replace bytes in this layer.

  `BpfFileSource.start()` must fail closed unless the ABI matches, map capacity is 262144, all six hooks report attached, and the loader is ready. `drain()` must pass the exact `Receipt.source/epoch/seq`, require the response to echo it, require strictly increasing receipts for the source's current epoch, merge no records, and return all records and stats exactly once. A protocol error, quiescence timeout, clear failure, counter overflow, or diagnostic-size violation raises `FileSourceError` and sets the source unavailable until the daemon explicitly starts a new source. `close()` must be idempotent and must not turn a failed close into a successful drain.

  Keep `VectorLoader` in the module as a small protocol-compatible in-memory implementation used by unit tests and daemon injection. It must model slot flip, old-slot clearing, receipt echo, and a selectable quiescence failure so tests exercise the adapter mechanism rather than only its dataclasses.

- [ ] **Step 4: Run the tests and confirm they pass**

  Run the focused command from Step 2. Expected result: all vector, validation, receipt, diagnostic, and lossless-drain tests pass without root, BPF, `/proc`, or `/sys` access.

- [ ] **Step 5: Commit**

  ```bash
  git add src/appio/bpf_fileio.py tests/p4/test_bpf_drain.py
  git commit -m "feat(files): add validated lossless BPF drain adapter"
  ```

**Scheduling:** `depends_on: [1]; lane: drain; sequence: 1`.

## Task 3: Attribute file events and preserve hourly identity state

**Implementer tier:** Capable

**Files:**

- Modify: `src/appio/files.py:1-253`
- Modify: `src/appio/live.py:240-590`
- Modify: `tests/test_files.py:1-211`
- Modify: `tests/test_live.py:1-999`
- Create: `tests/p4/test_file_attribution.py`

**Interfaces:**

- Consumes: `FileDrain`, `FileEvent`, and `CgroupFileStats` from Task 2; `StableProcRecord`, `CgroupObservation`, `AggregatedFrame`, `TickInput`, and `resolve_app` from the shipped process/sampler seams.
- Produces: `FileFrameContext(boot_id: str, epoch: int, seq: int, observed_mono_ns: int, app_keys: tuple[str, ...], owners_by_cgroup: tuple[tuple[int, tuple[str, ...]], ...])`, published by `LiveCollector.file_context` only after its corresponding T/D tick commits.
- Produces: `FileIdentityResolver(fs_uuid_for_dev: Callable[[str], str | None])`; a missing or unproved UUID returns `fs_uuid=""` and `identity_weak=True`, never a fabricated strong UUID.
- Produces: `FileFrameContribution(apps: tuple[tuple[str, tuple[FileObservation, ...], FileCoverageInput], ...], unknown_owner: UnknownOwnerInput, captured_names: tuple[CapturedName, ...])` and `attribute_file_drain(drain: FileDrain, context: FileFrameContext, resolver: FileIdentityResolver) -> FileFrameContribution`; each app tuple is UTF-8-byte sorted, and the unknown-owner object is never folded into an app.
- Preserves: `HourlyAccumulator`'s all-candidate state, receipt-level coverage-once rule, byte/access/identity tie order, alias collection, checked integer arithmetic, and top-N-at-hour-close behavior. The task may extend these existing types but may not select top-N per drain.

- [ ] **Step 1: Write the failing tests**

  Add attribution tests with synthetic `StableProcRecord`/`CgroupObservation` tuples: one cgroup with one resolved AppKey; one cgroup shared by two AppKeys; an event with no owner; reserved cgroup `0`; an app with no events; and a cgroup whose stats contain hook hits but no file record. Assert that only a uniquely owned cgroup produces app rows, ambiguous/unresolved events become unknown-owner bytes/files, and every app in the committed frame receives a zero-event coverage contribution when F is complete.

  Add identity tests for a valid UTF-8 full basename (`resolved`), a 40-byte boundary, a name longer than 40 bytes (`truncated`), empty/invalid UTF-8 (`unresolved`), generation preservation, weak UUID fallback, and a provider-supplied strong UUID. Assert no NFC/case folding, no path reconstruction, and no accidental merging of two generations or two devices.

  Add tests that put two aliases for the same `(fs_uuid, dev, inode, generation, identity_weak)` through different drains and assert the accumulator chooses the canonical UTF-8-byte basename, keeps the other alias, uses the latest observed timestamp for the aggregate row, and leaves candidate losers pending until finalization. Add a boot-change context test that proves the new context carries a new boot ID rather than comparing monotonic values across boots.

  Falsifiers: assigning a shared cgroup to either app, dropping unknown-owner bytes, counting global hook hits once per file row, treating no events as missing coverage, using basename as identity, normalizing names, treating an absent UUID as strong, or selecting top-100 before the hour closes must fail the corresponding test. A `LiveCollector` context visible before `record_tick` commits must fail the commit-barrier test.

- [ ] **Step 2: Run the tests and confirm they fail**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_file_attribution.py tests/test_files.py tests/test_live.py
  ```

  Expected result: FAIL because the frame context, attribution conversion, identity-provider seam, and live publication hook do not exist.

- [ ] **Step 3: Write the minimal implementation**

  In `files.py`, add immutable context/contribution records and a pure attribution function. Build `owners_by_cgroup` from the current stable process records by calling the already authoritative `resolve_app(record.app_key_inputs.cgroup_path, record.app_key_inputs.exe, record.app_key_inputs.desktop_id)` path; deduplicate and UTF-8-sort AppKeys. Use `frame.coverage` app keys to include zero-event apps. Do not infer an owner from a device row, PID number, basename, or a prior frame.

  Attribute each file event and each cgroup stat as follows: one current owner sends event counters and that cgroup's hook/skipped/overflow counters to that app; zero or multiple owners sends event bytes/files and overflow counters to `UnknownOwnerInput`; cgroup `0` is always unknown. Keep unknown-owner evidence separate from `uncounted_overflow_*` and do not mark a complete drain as a gap merely because ownership was ambiguous. A source/protocol failure is handled by Task 5 and is the condition that sets `complete=False`/`gap_intervals`.

  Decode basename bytes only in this layer. Preserve valid UTF-8 exactly, mark a length beyond 40 as `truncated`, mark empty or invalid data `unresolved` with an explicit nonempty display marker, and pass `name_len` into the state decision. Use the injected UUID provider; validate a returned UUID through the existing B5 rules, and force weak identity for `None`/empty/unavailable values. Construct `FileObservation` with `i_generation` and the current frame monotonic timestamp. Produce one `CapturedName` per identity/basename with first and last timestamps for the existing store.

  In `LiveCollector.tick`, construct the context from the exact records/cgroups used for the accepted frame, retain it beside `_last_frame`, and publish it only after `store.record_tick(frame, next_state)` returns successfully. Clear it on close and on a rejected/stale tick. Do not add a live filesystem lookup to `FileHourStore`; all lookup/provider behavior remains an injected Python dependency.

- [ ] **Step 4: Run the tests and confirm they pass**

  Run the focused command from Step 2. Expected result: all existing accumulator/live tests and the new attribution tests pass, including unchanged T/D numerical results and the commit-publication barrier. No test may read the host process or cgroup filesystem.

- [ ] **Step 5: Commit**

  ```bash
  git add src/appio/files.py src/appio/live.py tests/test_files.py tests/test_live.py tests/p4/test_file_attribution.py
  git commit -m "feat(files): attribute drained events and preserve path identity"
  ```

**Scheduling:** `depends_on: [2]; lane: attribution; sequence: 1`.

## Task 4: Materialise file hours and enforce independent retention

**Implementer tier:** Capable

**Files:**

- Modify: `src/appio/file_store.py:31-354`
- Modify: `src/appio/quota.py:1-478`
- Modify: `tests/test_file_store.py:1-312`
- Modify: `tests/test_quota.py:1-419`
- Create: `tests/p4/test_file_retention.py`

**Interfaces:**

- Consumes: Task 3 `FileFrameContribution`, `HourlyFileSnapshot`, `CapturedName`, `FileHourState`, and `Receipt`; the shipped `FileHourStore.apply_pending()`/`finalize_hour()` and schema-v1 path/file tables.
- Produces: an idempotent file-hour persistence path that writes cumulative pending state under `file_hour_state`, finalizes one `(boot_id, AppKey, hour)` snapshot in chronological order, applies top 100 only at close, and writes zero-F app coverage plus `file_overflow_unknown` even when no retained file row exists.
- Preserves: `FileHourStore._atomic` savepoint/outer-transaction behavior, A21 overflow diagnostics, B5 weak identity, `path`/`path_alias` identity keys, persisted `boot_order` endpoint ordering, and file coverage JSON shape.
- Produces: quota units `file` and `file_unknown` with independent 14-day expiry, exact A13 prerequisite/materialisation checks, complete-primary-key ordering under wall-clock collisions, and no deletion of a path still referenced by retained file samples or boot totals.

- [ ] **Step 1: Write the failing tests**

  Add a zero-hour test that creates an authoritative app with no file events, applies a complete pending snapshot, finalizes the hour, and asserts an `app_boot_file_coverage` row with zero observed counters and a complete file coverage object, while the file table remains distinguishable from unavailable.

  Add an A20 endpoint test that records the same identity and alias in `boot-A` and `boot-B` with monotonic values that would sort incorrectly by wall time. Assert `path.first_boot_id/last_boot_id` and `path_alias.first_boot_id/last_boot_id` follow `boot.boot_order` and within-boot monotonic time. Add a chronology-failure test for missing/conflicting boot order that leaves the transaction unchanged.

  Add retention tests that place process raw rows at the strict 24-hour boundary and file rows/unknown-owner units at the strict 14-day boundary, assert raw rows expire independently, and assert file/file-unknown rows remain until their own boundary. Add quota tests for the existing two rollup primary keys whose mapped wall timestamps collide, proving the full primary-key suffix selects the first unit, and tests proving file totals/materialisation are committed before file observations are deleted.

  Add a stop-without-guessing test for missing `clock_map` evidence and missing file-total materialisation; assert the pruner returns an incomplete result, deletes nothing from the affected unit, and preserves unrelated writers. Add a path-protection test for samples and `app_boot_file_total` references.

  Falsifiers: dropping zero-F coverage, using wall time to order endpoints, sharing the 24-hour raw cutoff with files, ordering by mapped wall time alone, deleting before totals, guessing a missing clock map, merging unknown-owner overflow into an app, or deleting a referenced path must fail a named test.

- [ ] **Step 2: Run the tests and confirm they fail**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_file_retention.py tests/test_file_store.py tests/test_quota.py
  ```

  Expected result: FAIL on the new zero-F, chronology, retention, and ordering assertions before implementation changes are made.

- [ ] **Step 3: Write the minimal implementation**

  Keep all candidate state in `HourlyAccumulator` until the hour is closed. Make the persistence boundary accept a cumulative snapshot plus captured-name endpoints, call `apply_pending` on each successful F receipt, and call `finalize_hour` only for complete half-open hours behind the current frame. Include every authoritative app from `FileFrameContext.app_keys`, including apps with no event, and retain a separate unknown-owner accumulator/coverage object. Reuse the existing extension checks so replayed or regressing state cannot erase candidates, aliases, coverage, or receipts.

  Ensure the path materialiser creates/updates the canonical path and every alias with first/last `(boot_id, mono_ns)` endpoints and uses `_endpoint_key` backed by `boot.boot_order`. Reject a new path or alias without a captured timestamp; reject missing or conflicting boot chronology before mutating the transaction. Preserve `generation` and `identity_weak` in every lookup and keep empty UUID rows weak.

  Audit and adjust `QuotaPruner` only within its existing unit abstraction: `file` and `file_unknown` each map to 14-day retention; file units are ordered after core and before gap/rollup according to the existing A13 order; each deletion unit is the exact app-hour or unknown-owner hour primary key; full primary keys, including bucket start monotonic time, break mapped-wall ties. Before deleting a file unit, verify committed file totals/coverage prerequisites and protect referenced `path_id`s. If clock evidence or materialisation is absent, stop the pass without deleting or inventing a wall timestamp.

- [ ] **Step 4: Run the tests and confirm they pass**

  Run the focused command from Step 2, then run the full persistence/query contract subset:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/test_contracts.py tests/test_schema.py tests/test_files.py tests/test_file_store.py tests/test_quota.py tests/test_query.py tests/test_query_properties.py
  ```

  Expected result: all zero-F, A20, A13, retention, path-protection, existing accumulator, schema, and query tests pass. No test uses a live collector or privileged filesystem.

- [ ] **Step 5: Commit**

  ```bash
  git add src/appio/file_store.py src/appio/quota.py tests/test_file_store.py tests/test_quota.py tests/p4/test_file_retention.py
  git commit -m "feat(files): materialize hourly observations with independent retention"
  ```

**Scheduling:** `depends_on: [3]; lane: persistence; sequence: 1`.

## Task 5: Integrate F with the daemon and history routing

**Implementer tier:** Frontier

**Files:**

- Create: `src/appio/file_runtime.py`
- Modify: `src/appio/iod.py:220-275`
- Modify: `src/appio/iod.py:620-750`
- Modify: `src/appio/history_router.py:92-670`
- Modify: `src/appio/iodclient.py:240-300`
- Modify: `src/appio/iodclient.py:1140-1210`
- Create: `tests/p4/test_file_runtime.py`
- Create: `tests/p4/test_file_daemon.py`
- Create: `tests/p4/test_file_history_router.py`

**Interfaces:**

- Consumes: Task 2 `BpfFileSource`/`FileSourceError`; Task 3 `FileFrameContext`/`attribute_file_drain`; Task 4 `FileHourStore`; existing `LiveCollector.last_frame`, `last_result`, `file_context`, `QueryService.file_table()`, `file_coverage()`, and `unknown_owner_file_coverage()`.
- Produces: `FileAvailability(available: bool, bpf_loaded: bool, reason: str | None, diagnostic: str | None)` and `FileRuntime.start()`, `consume(frame: AggregatedFrame, context: FileFrameContext)`, `close()`, and read-only `availability`/`diagnostic` properties.
- Produces: one per-daemon-hour accumulator keyed by `(boot_id, hour_start_mono_ns)`; each accepted `iod` frame is drained with `Receipt("iod", frame.epoch, frame.seq)`, persisted as pending state, and finalized only after the hour closes. A source error records an incomplete F interval/diagnostic and disables only F.
- Produces: `FileViewResult(table: FileTable, coverage: FileCoverage)` and `HistoryRouter.request_file_view(app_id: int, window: Window, sort: FileSort, direction: Direction, search: str | None) -> Future[FileViewResult]`, where local requests use the router's bounded executor and remote requests use the existing `IodClient.query(QueryRequest(kind="files"/...))` plus the existing coverage request and HMAC cursor/snapshot checks.
- Produces: `HistoryRouter.file_source_status() -> FileAvailability` and client state field `file_diagnostic: str | None`; remote diagnostics are read from the validated plain `snapshot.coverage["file_source"]` object and are bounded before UI exposure.
- Preserves: `Capabilities`' exact four booleans, A22 disabled-window precedence, cursor generation/epoch/sequence rules, remote snapshot binding, `query_busy`, T/D `last_error`, recovery backoff, and existing file-source-unavailable behavior when no authoritative F evidence exists.

- [ ] **Step 1: Write the failing tests**

  Add `FileRuntime` tests with `VectorLoader`/scripted sources: a successful frame with one unique owner and one zero-event app; a crossing from one hour to the next; duplicate receipt rejection; an F source failure after a committed T/D frame; a counter/protocol failure; an incomplete final hour; and a boot change. Assert `file_hour_state`, finalized rows, zero-F coverage, receipt source/epoch/seq, unknown-owner rows, and 14-day-compatible units.

  Add daemon tests that inject a fake file-source factory into `Daemon`: start failure with errno/verifier text; successful six-hook availability; drain failure; and recovery to a new F source. Assert T/D frames continue at the existing cadence, process/device snapshot data and `last_error` remain healthy, `Capabilities.file_io`/`bpf_loaded` are false on F failure and true only after attach verification, and the snapshot coverage contains a bounded `file_source` diagnostic. Assert the daemon never invokes a privileged loader in the default test.

  Add router tests for local file-view futures and remote `IodClient` futures. Cover every file sort (`bytes_total`, `bytes_r`, `bytes_w`, `reads`, `writes`, `basename`, `dev`, `last_seen`) in both directions, NFC-casefold search delegation, cursor/snapshot mismatch, stale remote reply after `invalidate()`, `file_source_unavailable`, `not_retained`, and a valid complete zero-row/zero-F result. Assert unavailable values are not converted into zero rows or zero counters.

  Falsifiers: starting F in the same transaction path as T/D in a way that blocks the next T/D tick, setting `last_error` for an F-only failure, advertising `file_io=True` before all six hooks attach, using a different receipt source/epoch/seq, dropping a late old-hour drain, accepting a stale router future, bypassing the existing HMAC cursor, or treating a missing capability as an empty table must fail a named test.

- [ ] **Step 2: Run the tests and confirm they fail**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_file_runtime.py tests/p4/test_file_daemon.py tests/p4/test_file_history_router.py
  ```

  Expected result: FAIL because `FileRuntime`, daemon injection/status integration, client diagnostic capture, and `HistoryRouter.request_file_view` do not exist.

- [ ] **Step 3: Write the minimal implementation**

  Implement `FileRuntime` as an owner-loop object using the existing `LiveCollector.store.connection`; it must not open a second writer connection or let a history worker mutate SQLite. On `start`, construct the injected/default source and retain its availability. On each committed `LiveCollector` frame, call `drain(Receipt("iod", frame.epoch, frame.seq))`, attribute the complete vector using `collector.file_context`, add one coverage contribution for every current app, update the `(boot, hour)` accumulator, call `FileHourStore.apply_pending`, and finalize older hours in monotonic order. On a boot change, close the prior boot's partial current hour with explicit incomplete/gap evidence before beginning the new boot's hour; never compare cross-boot monotonic values. A file transaction failure must roll back only its F transaction before the daemon catches the exception; it must not leave the shared connection in a transaction that can block or corrupt the next T/D tick.

  Catch only F-specific exceptions at the daemon integration boundary. Persist an incomplete F coverage/diagnostic contribution when a current receipt is available, mark the source unavailable, log and expose bounded `errno` plus verifier/detail text, and continue publishing T/D. Do not set `Daemon.last_error`, do not clear the T/D replay ring, and do not alter `_next_tick`, `_next_sample`, recovery, handover, or shutdown budgets. On the existing recovery path, a fresh source may be started; capabilities change only in the next committed snapshot after the six-hook HELLO succeeds. Extend `_snapshot`'s plain coverage object with `file_source: {available, bpf_loaded, reason, diagnostic}` and replace the two hardcoded false capability arguments with the runtime properties. Keep `files=[]` as a live snapshot compatibility placeholder; historical file rows come from the bounded query RPC, so no unbounded file payload is added to the stream.

  Add `ClientState.file_diagnostic` with a default of `None`. When a validated snapshot arrives, copy only the bounded string diagnostic from `coverage.file_source`; clear it when a later snapshot reports no diagnostic. Do not add keys to the exact capabilities object or alter status/request validation.

  Extend `HistoryRouter` with generation-bound file requests. Local requests run `QueryService.file_table` and `file_coverage` against the already selected read-only view; remote requests use the existing `IodClient.query` decoder for `files` and `coverage` and require the response snapshot to equal the prepared remote snapshot. A canceled/invalidate generation, stale cursor, remote error, mixed retention tier, or unavailable source settles the future with the existing explicit reason; it never returns a fabricated empty table. Preserve the router's local ownership exclusions and do not merge database-local `path_id` values as if they were globally stable. When a file view cannot be represented across unproven/mixed sources, return `file_source_unavailable` with incomplete coverage rather than summing rows by basename.

- [ ] **Step 4: Run the tests and confirm they pass**

  Run the focused command from Step 2, then run the existing daemon/history/client coverage:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_file_runtime.py tests/p4/test_file_daemon.py tests/p4/test_file_history_router.py tests/p3/test_iod.py tests/p3/test_iod_history.py tests/p3/test_iodclient.py
  ```

  Expected result: F-only failure tests show answering T/D and explicit F diagnostics; capability tests use booleans; all file sorts/searches/cursors are validated; and existing P3 daemon/client/history behavior remains green.

- [ ] **Step 5: Commit**

  ```bash
  git add src/appio/file_runtime.py src/appio/iod.py src/appio/history_router.py src/appio/iodclient.py tests/p4/test_file_runtime.py tests/p4/test_file_daemon.py tests/p4/test_file_history_router.py
  git commit -m "feat(iod): run independent file collection and routed queries"
  ```

**Scheduling:** `depends_on: [2, 3, 4]; lane: integration; sequence: 1`.

## Task 6: Replace the unavailable page with the Files surface

**Implementer tier:** Advanced

**Files:**

- Create: `src/appio/ui/files_view.py`
- Modify: `src/appio/ui/collector_window.py:398-542`
- Modify: `src/appio/ui/collector_window.py:640-690`
- Modify: `tests/p3/test_collector_window.py:190-240`
- Create: `tests/p4/test_files_view.py`

**Interfaces:**

- Consumes: Task 5 `HistoryRouter.request_file_view(...) -> Future[FileViewResult]`, `HistoryRouter.file_source_status()`, and existing `HistoryRouter.ranking(window, "total_bytes") -> list[AppTotal]` for the app selector; `Window`, `FileTable`, `FileCoverage`, `FileSort`, and `Direction`.
- Produces: `FilesView(QWidget)` with a `QTableView` model, explicit loading/unavailable/incomplete/ready states, app selector, NFC-casefold search field, file sort/direction controls, and coverage footer labels. The table columns are exactly `FILE`, `DISK`, `READS`, `WRITES`, `BYTES R`, `BYTES W`, `LAST`; basename is rendered with truncated/unresolved state markers and no full path is invented.
- Produces: `FilesView.set_window(window: Window)`, `set_app_id(app_id: int)`, `request_refresh()`, and `state`/`last_result` test seams. The view never imports `appio.iod`, `appio.bpf_fileio`, `appio.file_runtime`, or setup/privilege modules.
- Modifies: `CollectorWindow.files_page` to host the real Files view; `open_files_unavailable()` remains a compatibility navigation slot but now selects the Files page and requests the current view. The existing P3 Settings switch remains the only privileged control.

- [ ] **Step 1: Write the failing tests**

  Add offscreen `FilesView` tests with an immediate fake `Future` and a synthetic `FileViewResult`: assert exact headers/order, basename-only rendering, `last_seen` display, sort/direction/search request fields, app selection, loading transition, ready rows, incomplete coverage footer, counted omissions, uncounted overflow, unknown-owner overflow, skipped non-files/unsupported filesystems, hook hits, and weak identity count. Assert the view renders `unavailable`/reason text and no numeric zeroes when the future fails or capability is absent.

  Update the P3 collector-window navigation test to assert that opening Files changes only the visible page and query request state, not coordinator/setup/privileged state. Add a collector test that injects a ready fake history router, opens Files, and verifies the real table appears; add an F-unavailable test that keeps the explicit `FILES_UNAVAILABLE`/diagnostic state. Keep the existing assertions for one system-services switch, no tray, no menu, no privileged action, and `AppIoWindow` untouched.

  Falsifiers: using `QTableWidget` instead of the model-backed table, rendering an unavailable result as zeroes, hiding counted vs uncounted vs unknown-owner facts, sorting `last_seen` by display text rather than the stored value, re-querying ranking for every file row, opening a root/setup action from Files, or importing a forbidden module into `AppIoWindow` must fail a named test.

- [ ] **Step 2: Run the tests and confirm they fail**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_files_view.py tests/p3/test_collector_window.py
  ```

  Expected result: FAIL because the Files view is currently the P3 unavailable message and no file-query model exists.

- [ ] **Step 3: Write the minimal implementation**

  Implement `FilesView` with a `QAbstractTableModel` over `FileTable.rows`, stable column roles, and fixed header/row sizing so late text cannot resize the layout. Use existing Qt widgets and icons available in the repository; use familiar icon-only direction/refresh controls only where the action is unambiguous, with tooltips for unfamiliar controls. Keep the table unframed inside the existing page and use the existing application palette rather than introducing a second design system.

  Drive all data through one `request_file_view` future per control change, cancel/ignore a prior generation when the window/app/sort/search changes, and update the model only on the Qt thread. Render the `FileTable.coverage` and `FileCoverage` fields separately: observed rows/bytes, `counted_omitted_*`, `uncounted_overflow_*`, unknown-owner overflow, skipped non-files, skipped unsupported filesystems, weak identity rows, hook-hit totals, completeness, and any unavailable reason. Use `unavailable` for `None`/missing facts and never substitute zero. Keep text within its parent at narrow widths with elision/wrapping, and show the row state marker without fabricating a path.

  Replace only the `CollectorWindow.files_page` placeholder content and its navigation callback. When the prepared window becomes ready, create/update `FilesView` with the same exact `Window` used by `AppIoWindow`; when history is unavailable or the daemon reports F unavailable, keep the page visible with the explicit message and Settings deep link. Do not add a Files tab, text, or import to `AppIoWindow`; the P1/P2 acceptance guard must continue to see the Overview window as P1/P2-only.

- [ ] **Step 4: Run the tests and confirm they pass**

  Run the focused command from Step 2, then run the UI and acceptance tests:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_files_view.py tests/p3/test_collector_window.py tests/test_ui.py tests/acceptance/test_no_later_scope.py tests/acceptance/test_p1_p2.py
  ```

  Expected result: the Files surface is usable under offscreen Qt, unavailable values remain honest, the P3 switch/navigation invariants remain green, and `test_no_later_scope.py` is unchanged and passes.

- [ ] **Step 5: Commit**

  ```bash
  git add src/appio/ui/files_view.py src/appio/ui/collector_window.py tests/p3/test_collector_window.py tests/p4/test_files_view.py
  git commit -m "feat(ui): add honest per-file Files view"
  ```

**Scheduling:** `depends_on: [5]; lane: ui; sequence: 1`.

## Task 7: Run the single opt-in privileged workload acceptance

**Implementer tier:** Advanced

**Files:**

- Create: `tests/p4/test_bpf_acceptance.py`

**Interfaces:**

- Consumes: the Task 1 loader executable/ABI, Task 2 drain decoder, Task 3 attribution fixtures, and the Task 5 daemon/runtime capability seam.
- Produces: one opt-in acceptance test module that is skipped unless `APPIO_P4_BPF_ACCEPTANCE=1`, effective UID is zero, clang/libbpf/BTF prerequisites are present, and the host exposes all six target symbols. The default suite remains rootless and offline.
- Verifies: all six hooks attach or fail with a bounded errno/verifier diagnostic; a controlled regular-file workload produces positive read/write/readv/writev activity; failed/nonpositive operations do not add bytes; the drained vector reconciles known inode/generation/device/name counters; overflow/unknown-owner accounting remains separate; and T/D remains unaffected when the F loader is deliberately made unavailable.

- [ ] **Step 1: Write the failing acceptance test**

  Add a test that performs explicit prerequisite checks and skips with the reason, not a silent pass, when the opt-in variable is absent, the process is not root, BTF is unreadable, or the six BTF symbols are unavailable. Under opt-in, build the loader into a temporary directory, create an isolated temporary block-backed file and an isolated cgroup/workload context permitted by the host, launch a small controlled Python/C workload that performs positive `read`, `write`, `readv`, and `writev` operations plus a failing/nonpositive operation, and ask the loader for a bounded drain.

  Assert the HELLO attach bitmask includes all six hooks, the expected `(dev, inode, i_generation)` record is present, positive return counters are nonzero and have the expected byte totals, the failed/nonpositive call contributes nothing, the drain completes and clears the old slot, and the per-hook counters are observable. Run a second subcase with a fake unavailable source through the daemon test seam and assert the process/device stream remains answering with `file_io=False` and `bpf_loaded=False`. Always unload/detach, remove the temporary cgroup/file, and close the loader in `finally` blocks.

  Falsifiers: accepting only five hooks, counting a failed operation, losing the generation/name/device identity, returning before the old slot is clear, treating a skipped/overflow counter as a file row, running the acceptance by default, or making T/D unavailable when F is unavailable must fail the acceptance module or its default skip-policy assertion.

- [ ] **Step 2: Run the test and confirm the default path is non-privileged**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_bpf_acceptance.py
  ```

  Expected result: the module is skipped with an explicit opt-in/root/prerequisite reason and performs no load, cgroup mutation, or workload when `APPIO_P4_BPF_ACCEPTANCE` is not `1`.

- [ ] **Step 3: Write the minimal acceptance implementation**

  Implement the guarded workload and cleanup using only standard-library/process APIs already available on the host. Keep all generated object, loader, data, and logs under a temporary directory. Pass the workload's cgroup ID and file identity into the assertion rather than discovering app ownership from live `/proc`. Bound command timeouts and diagnostic reads; a timeout is a failure with the loader's stderr/verifier evidence.

  Make the test tolerate a host kernel that attaches all six hooks but does not route every synthetic operation through every hook: attachment is the six-hook requirement, while byte reconciliation is asserted for hooks the workload actually exercises and the observed hit counters are required to be nonnegative and internally consistent. Do not weaken the positive-return, regular-file, block-backed, generation, overflow, or slot-clear assertions.

- [ ] **Step 4: Run the acceptance and then the complete rootless suite**

  For the opt-in host gate, run:

  ```bash
  APPIO_P4_BPF_ACCEPTANCE=1 PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q tests/p4/test_bpf_acceptance.py
  ```

  Then run the complete non-privileged suite:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest -q
  ```

  Expected result: the opt-in test either proves the real host workload or fails with actionable kernel/verifier evidence; the complete suite passes without root and without a live BPF load. Record an opt-in skip only when the guarded prerequisites are genuinely absent, never as a way to hide a loader failure after the gate is enabled.

- [ ] **Step 5: Commit**

  ```bash
  git add tests/p4/test_bpf_acceptance.py
  git commit -m "test(bpf): add opt-in privileged file workload acceptance"
  ```

**Scheduling:** `depends_on: [1, 2, 3, 4, 5, 6]; lane: acceptance; sequence: 1`.
