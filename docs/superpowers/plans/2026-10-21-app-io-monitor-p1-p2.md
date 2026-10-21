# appio P1/P2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Build the appio P1 measurement, identity, storage, aggregation, and query contracts, then prove them through a P2 PyQt6 window that lists since-boot application totals and the signed T/D residual.

**Architecture:** Pure fixture-driven modules parse process and cgroup observations, resolve stable AppKeys, perform boottime arithmetic, and produce deterministic aggregation frames. A STRICT SQLite v1 store commits stream receipts, raw rows, rollups, totals, coverage, diagnostics, and sampler checkpoints atomically; a query service selects one supported tier and derives signed reconciliation values with `None` for unavailable device data. A small PyQt6 window consumes only the query contracts and renders the application list, since-boot totals, and residual; daemon, socket, BPF, tray, and Files UI work stay outside this plan.

**Tech Stack:** Python 3.14, standard-library `sqlite3`, `pathlib`, `json`, and `dataclasses`, PyQt6, pytest, pytest-qt, and a `src/` layout configured by `pyproject.toml`.

## Global Constraints

- Python 3.14 is the runtime floor; use PyQt6 for the window and pytest plus pytest-qt for tests.
- The repository uses a `src/` layout and `pyproject.toml`; do not add a runtime dependency beyond PyQt6 and the Python standard library.
- SQLite MUST be at least 3.37.0 with JSON functions available; every one of the 27 specified tables is `STRICT`, and the migration runner MUST create the schema from an empty database without an outer transaction.
- SQLite-range nonnegative counters and monotonic timestamps are in `[0, 9223372036854775807]`; Python arithmetic is signed and arbitrary precision; overflow MUST roll back the complete observation and MUST NOT wrap, saturate, or coerce to REAL.
- All windows are half-open; all bucket keys use `CLOCK_BOOTTIME` nanoseconds; raw process samples use `10_000_000_000` ns buckets and file hours use `3_600_000_000_000` ns buckets.
- Stream identity is `(boot_id, source, epoch, seq)`; P1 source values are exactly `gui` and `iod`, epochs are positive, sequence numbers start at zero, and stale or duplicate frames are rejected before any baseline or database mutation.
- Every query result carries a populated `Coverage`; unavailable device attribution uses `None` for aggregate device and residual values while task totals remain numeric.
- Tests MUST use temporary fixture trees, synthetic observations, and an offscreen Qt application. No task may require root, a real `/proc`, a live `/sys/fs/cgroup`, a live session, BPF, a daemon, a socket, polkit, a tray, or a privileged integration test.
- This plan covers only the agreed A-prime P1 contracts and P2 thin vertical slice. It MUST NOT implement the daemon, socket protocol, BPF program, tray, package, privilege setup, or Files tab; file-hour storage and query contracts may accept synthetic future-producer data so the P1 schema remains complete.
- The signed-off specification is authoritative for behavior, including A01–A22, the exact A22 messages, the canonical JSON schemas, and the complete DDL in its section 6. The design revision 5 is authoritative for architecture and delivery order; the approved assets are layout references only.

## Task 1: Establish the Python package and shared contract types

**Implementer tier:** Standard

**Lane:** P1-foundation

**Sequence:** P1.1

**depends_on:** none

**Ownership:** `pyproject.toml`, `src/appio/__init__.py`, `src/appio/contracts.py`, `tests/test_contracts.py`

**Files:**

- Create: `pyproject.toml`
- Create: `src/appio/__init__.py`
- Create: `src/appio/contracts.py`
- Test: `tests/test_contracts.py`

**Interfaces:**

- Consumes: nothing; this task defines the shared vocabulary used by every later task.
- Produces `CounterVector = tuple[int, int, int, int]` in the fixed order `(rchar, wchar, read_bytes, write_bytes)`; `AppKey = str`; `CounterName = Literal["rchar", "wchar", "read_bytes", "write_bytes"]`; `Metric = Literal["total_bytes", "read_bytes", "write_bytes", "rchar", "wchar"]`; `Tier = Literal["raw", "rollup_60s", "rollup_3600s", "totals_only"]`; `FileSort = Literal["bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen"]`; and `Direction = Literal["asc", "desc"]`.
- Produces frozen dataclasses `Window`, `HookHits`, `Coverage`, `AppTotal`, `SeriesPoint`, `DeviceSeriesPoint`, `ResidualPoint`, `Series`, `DeviceShareRow`, `DiskShare`, `FileRow`, `UnknownOwnerOverflow`, `FileTable`, and `FileCoverage` with the exact field names and optionality in specification section 8.1. `Coverage` MUST include `tier`, `complete`, `unavailable_reason`, all four coverage counters/tuples, all file counters, `device_state`, `reap_incomplete`, `counter_overflow`, `spans_suspend`, and `HookHits`.
- Produces `WindowUnavailableError(reason, message)`, `CounterOverflowError(field)`, `validate_window(window)`, `validate_enum(value, allowed, field)`, and `validate_sqlite_integer(value, field, nonnegative=True)`. The validators reject `bool` wherever an integer is required, reject NUL in search strings later consumed by queries, and preserve the exact A22 message constants for launch and over-365-day range errors.
- Produces `FileObservation(app_key, fs_uuid, dev, inode, generation, basename, state, identity_weak, reads, writes, bytes_r, bytes_w, last_seen_mono_ns)`, `FileCoverageInput(counted_omitted_files, counted_omitted_bytes_r, counted_omitted_bytes_w, uncounted_overflow_files, uncounted_overflow_bytes_r, uncounted_overflow_bytes_w, skipped_nonfile, skipped_unsupported_fs, weak_identity_rows, hook_hits, gap_intervals, overlap_uncertain_intervals, complete)`, `UnknownOwnerInput(overflow_files, overflow_bytes_r, overflow_bytes_w, gap_intervals, overlap_uncertain_intervals, complete)`, and `HourlyFileSnapshot(boot_id, hour_start_mono_ns, apps, unknown_owner)`. These transport dataclasses contain only validated synthetic future-producer data and do not load BPF.

### Tests first

- [ ] Write `tests/test_contracts.py` with tests that construct each valid `Window` form and assert that malformed combinations raise `ValueError`: a range without `boot_id`, a nonpositive or reversed range, a since-boot window with a start, a launch without a positive launch ID, a lifetime window with any boot or endpoint, an enum outside its literal set, and a boolean passed as an integer.
- [ ] Add a test that `WindowUnavailableError("launch_attribution", "Launch window disabled: device and file activity are not stored per launch.")` exposes both `.reason` and the exact `str(error)`, and that `CounterOverflowError("read_bytes")` exposes `.field`.
- [ ] Add a test that `Coverage`, `AppTotal`, `Series`, `DiskShare`, `FileTable`, and `FileCoverage` can be constructed with the declared fields and retain `None` for unavailable device/residual values; this fails if a later implementation silently substitutes zero.
- [ ] Run `python -m pytest -q tests/test_contracts.py`; it MUST fail before implementation because the package and contract module do not exist.

### Implementation

- [ ] Create `pyproject.toml` with a build-independent `src` package layout, Python `>=3.14`, the `appio` package discovery path, pytest configuration, and `pytest-qt` test support without introducing a new runtime library.
- [ ] Implement the frozen dataclasses and validators. Keep `CounterVector` order fixed everywhere, compute `AppTotal.total_bytes` and `FileRow.bytes_total` only from integer fields, and do not put database-local IDs into external identity types.
- [ ] Encode the A22 exact messages as constants: `Launch window disabled: device and file activity are not stored per launch.` and `Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.`; also expose `Historical boot prefix unavailable: totals cover the full collected boot.` for the query task.
- [ ] Run `python -m pytest -q tests/test_contracts.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add pyproject.toml src/appio/__init__.py src/appio/contracts.py tests/test_contracts.py && git commit -m "feat(appio): define shared P1 contracts"`.

## Task 2: Parse fixture `/proc` records and resolve stable process identity

**Implementer tier:** Advanced

**Lane:** P1-proc-identity

**Sequence:** P1.2

**depends_on:** Task 1

**Ownership:** `src/appio/procs.py`, `src/appio/identity.py`, `tests/test_procs.py`, `tests/test_identity.py`, `tests/fixtures/proc/**`

**Files:**

- Create: `src/appio/procs.py`
- Create: `src/appio/identity.py`
- Create: `tests/test_procs.py`
- Create: `tests/test_identity.py`
- Create: `tests/fixtures/proc/**`

**Interfaces:**

- Consumes: `CounterVector` and `AppKey` from Task 1; it defines the process identity type used by later sampler tasks.
- Produces `ProcessIdentity(boot_id: str, pid: int, start_ticks: int)`, `ProcessStat(pid, comm, state, ppid, start_ticks)`, `IoReadOutcome(status: Literal["readable", "eacces", "esrch", "error"], counters: CounterVector | None, dumpable: bool | None, errno_name: str | None)`, and `StableProcRecord(identity, ppid, uid, exe, cgroup_path, app_key_inputs, io_outcome)`.
- Produces `ProcReader(root: Path, dumpability: Callable[[int], bool | None] | None = None)` with `enumerate_pids() -> tuple[int, ...]`, `read_stat(pid)`, `read_io(pid)`, and `read_stable(pid)`. `read_stable` reads stat-before, io plus cgroup/exe/status metadata, then stat-after; a changed start time returns an unstable result with no accepted sample, and `ESRCH`/`ENOENT` is distinguishable from `EACCES`.
- Produces `parse_stat(text)`, `parse_io(text)`, `parse_status_uid(text)`, `encode_app_key(kind, payload)`, `validate_app_key(key)`, `resolve_app(cgroup_path, exe, desktop_id)`, and `session_scope_key(cgroup_path)`. AppKey encoding passes ASCII letters, digits, and `/._:@+-` through, percent-encodes every other UTF-8 byte as uppercase `%HH`, preserves unit spelling, requires absolute executable/cgroup payloads, and uses `scope:/session/unknown` when no valid higher-precedence input exists.

### Tests first

- [ ] Create fixture files for the specification’s PID table, including `stat` with field 22 start ticks, `io` with all seven kernel lines, `status`, `cgroup`, and `exe` symlinks. Keep the fixture under `tests/fixtures/proc/`; never open the host `/proc`.
- [ ] Test `enumerate_pids()` returns only numeric process directories in ascending order, `parse_stat()` extracts field 22 and the parent PID, and `parse_io()` returns `(rchar,wchar,read_bytes,write_bytes)` while rejecting missing, duplicate, negative, boolean-like, or malformed counters.
- [ ] Test a harness metadata provider marks own-UID non-dumpable `EACCES` as `status="eacces"`, `dumpable=False`, and no counter vector; assert that permission facts are distinct from dumpability metadata. Test an `ENOENT` during any logical read as `status="esrch"`.
- [ ] Test stat-before/stat-after start-time mismatch returns no accepted `StableProcRecord`, while a stable pair produces `ProcessIdentity(boot_id, pid, start_ticks)` and preserves the supplied executable target without resolving another symlink layer.
- [ ] Test A07 resolution precedence with a deepest `app-*` unit, nearest `app.slice`, desktop fallback, executable fallback, and session fallback. Include the exact B8 session cases: `/user.slice/session-7.scope/worker` maps to `scope:/user.slice/session-7.scope`; `/user.slice/user-1000.slice/user@1000.service/app.slice/worker`, `None`, and relative `session-7.scope/worker` map to `scope:/session/unknown` when higher inputs are absent.
- [ ] Test percent encoding of `%`, Unicode UTF-8, safe bytes, invalid unit slash, invalid relative executable, and exact bytewise tie ordering. A test fails if safe characters are unnecessarily escaped or unit/executable identity is normalized.
- [ ] Run `python -m pytest -q tests/test_procs.py tests/test_identity.py`; it MUST fail before implementation because these modules are absent.

### Implementation

- [ ] Implement stat parsing by locating the final `)` that terminates `comm`, then indexing the remaining fields so field 22 is `start_ticks`; enforce positive PIDs, nonnegative start ticks, and nonnegative counters before creating a record.
- [ ] Implement `ProcReader.read_stable()` as one logical record: stat-before, all metadata and IO outcome, stat-after. Preserve the old open identity only at the sampler layer when stat is unstable; do not emit a partial process sample here.
- [ ] Implement the exact A07 AppKey grammar and precedence. For systemd paths select the deepest matching `app-*` component, then the deepest `app.slice` ancestor; use bytewise unit tie ordering; then desktop, absolute executable, and the session-scope prefix rule.
- [ ] Run `python -m pytest -q tests/test_procs.py tests/test_identity.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/procs.py src/appio/identity.py tests/test_procs.py tests/test_identity.py tests/fixtures/proc && git commit -m "feat(appio): parse proc fixtures and resolve AppKeys"`.

## Task 3: Parse cgroup device accounting and controller state

**Implementer tier:** Advanced

**Lane:** P1-cgroups

**Sequence:** P1.2

**depends_on:** Task 1

**Ownership:** `src/appio/cgroups.py`, `tests/test_cgroups.py`, `tests/fixtures/cgroup/**`

**Files:**

- Create: `src/appio/cgroups.py`
- Create: `tests/test_cgroups.py`
- Create: `tests/fixtures/cgroup/**`

**Interfaces:**

- Consumes: `AppKey` and integer validation from Task 1.
- Produces `DevStat(cgroup_id: int, dev: str, rbytes: int, wbytes: int)`, `CgroupObservation(boot_id, cgroup_id, cgroup_path, device_state: Literal["available", "not_delegated", "incomplete"], devices: tuple[DevStat, ...])`, and `DeviceDelta(cgroup_id, dev, rbytes, wbytes, reset: bool)`.
- Produces `parse_io_stat(text, cgroup_id) -> tuple[DevStat, ...]`, `parse_diskstats(text) -> dict[str, str]`, `friendly_device_name(dev, diskstats) -> str`, `read_cgroup_fixture(root, relative_path, boot_id, cgroup_id)`, `device_deltas(boot_id, observations, baselines)`, and `attribute_shared_cgroup(observation, owners_by_cgroup)`. The latter emits one device contribution for exactly one AppKey, emits no per-app row for distinct owners, and returns all affected AppKeys as incomplete.
- Device baselines are keyed only by `(boot_id, cgroup_id, dev)`; `major:minor` remains the canonical identity and friendly names are display metadata.

### Tests first

- [ ] Create fixture trees for the normal cgroup, the controller-absent cgroup, the same-AppKey shared cgroup, and the distinct-AppKey shared cgroup from specification section 10.2. Include `cgroup.controllers`, `cgroup.procs`, `io.stat`, and a `/proc/diskstats` fixture.
- [ ] Test `parse_io_stat()` extracts only `rbytes` and `wbytes`, ignores `rios`/`wios` for D, preserves decimal `major:minor`, rejects malformed device tokens, duplicate device rows, negative values, and out-of-range integers, and sorts by canonical device token.
- [ ] Test `read_cgroup_fixture()` returns `device_state="not_delegated"` and an empty device tuple when `io` is absent from `cgroup.controllers`; it MUST NOT synthesize zero-valued rows.
- [ ] Test device-name mapping maps a `259:0` token to the fixture’s friendly name while retaining `259:0` as the stored key.
- [ ] Test first observation and cgroup recreation emit zero deltas, a decreasing field emits zero and resets only that field’s baseline, and a cgroup baseline never transfers between boot IDs or cgroup IDs.
- [ ] Test the shared-cgroup vectors: same AppKey yields exactly one `(259:0,40,5)` contribution; distinct AppKeys yield no per-app D rows and mark both owners incomplete.
- [ ] Run `python -m pytest -q tests/test_cgroups.py`; it MUST fail before implementation because the module and fixtures are absent.

### Implementation

- [ ] Parse each `io.stat` line into one `DevStat`, require the canonical `major:minor` token and nonnegative SQLite-range `rbytes`/`wbytes`, and make the absence of the `io` controller an explicit state.
- [ ] Read diskstats into a major/minor lookup only for display labels; never use a friendly name as a primary key or merge equal names across boots.
- [ ] Implement component-wise baseline deltas with the A05 key and reset semantics. Return reset metadata so the sampler can add coverage without emitting a negative D value.
- [ ] Implement shared-owner attribution exactly as A19: one unique owner is attributed once; multiple distinct owners receive no D rows and each affected app carries incomplete device coverage.
- [ ] Run `python -m pytest -q tests/test_cgroups.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/cgroups.py tests/test_cgroups.py tests/fixtures/cgroup && git commit -m "feat(appio): parse cgroup device accounting"`.

## Task 4: Implement boottime buckets, clock segments, suspend attribution, and signed reconciliation

**Implementer tier:** Advanced

**Lane:** P1-time-math

**Sequence:** P1.2

**depends_on:** Task 1

**Ownership:** `src/appio/clock.py`, `src/appio/reconcile.py`, `tests/test_clock.py`, `tests/test_reconcile.py`

**Files:**

- Create: `src/appio/clock.py`
- Create: `src/appio/reconcile.py`
- Create: `tests/test_clock.py`
- Create: `tests/test_reconcile.py`

**Interfaces:**

- Consumes: `CounterVector`, `Coverage`, `ResidualPoint`, `DeviceShareRow`, and integer validation from Task 1.
- Produces `bucket_start_mono_ns(ts_mono_ns: int, bucket_ns: int) -> int`, `ClockSegment(boot_id, segment_start_mono_ns, wall_offset_ns)`, `ClockMapState.observe(boot_id, ts_mono_ns, wall_ns) -> ClockSegment | None`, `ClockMapState.wall_ns(boot_id, ts_mono_ns) -> int | None`, and `SuspensionSet` with `add(start_ns, end_ns)` and `measured_active_ns(start_ns, end_ns) -> int | None`.
- Produces `IntervalEvidence(interval_start_mono_ns, observed_ns, attributed_ts_mono_ns, spans_suspend)` and `attribute_interval(last_observed_ns, observed_ns, suspend_set, known_uncovered=None)`. Explicit boundaries are unioned and intersected once; a spanning delta is attributed to the last pre-suspend timestamp and unknown boundaries produce `observed_ns=None`.
- Produces `rate_bps(delta_bytes, observed_ns) -> int | None`, using `floor(delta_bytes * 1_000_000_000 / observed_ns)`, and `Reconciliation(task_read, task_write, task_total, device_read, device_write, device_total, residual_read, residual_write, residual_total, device_state)` from signed arbitrary-precision arithmetic. If device state is not `available`, every aggregate D and residual field is `None` while T remains numeric.

### Tests first

- [ ] Test integer bucket arithmetic for zero, exact boundaries, timestamps just below/above a boundary, and a positive `bucket_ns`; assert no wall offset changes any bucket key.
- [ ] Test the clock fixture sequence: initial segment at zero, a new segment only when `abs(offset_delta) > 1_000_000_000`, historical lookup by greatest start not greater than the sample, and an NTP step that leaves existing boottime buckets unchanged.
- [ ] Test explicit suspend union for overlapping and repeated boundaries. For the specification’s `[179s,661s]` observation with measured suspend `[180s,660s]`, assert attributed timestamp `179s`, `observed_ns=2_000_000_000`, `spans_suspend=True`, and rates `(20,10,30)` for `(read,write,total)=(40,20,60)`.
- [ ] Test `rate_bps()` returns `None` for a baseline, zero duration, unknown duration, or negative input; distinguish an integer zero for a defined floored-zero rate from `None` for an undefined rate.
- [ ] Test positive and negative residuals with `T=(200,100)` and `D=(170,120)`, yielding `(R_read,R_write,R)=(30,-20,10)` and the exact identities. Test `not_delegated` and `incomplete` return `None` device/residual fields without attempting arithmetic with `None`.
- [ ] Run `python -m pytest -q tests/test_clock.py tests/test_reconcile.py`; it MUST fail before implementation because these modules are absent.

### Implementation

- [ ] Implement `bucket_start_mono_ns` with integer division and require `bucket_ns > 0`; use nonnegative boottime values only.
- [ ] Implement `ClockMapState` so the first accepted observation for a boot inserts `(boot_id,0,offset)`, a later segment starts at the current timestamp only for a strictly greater than one-second absolute offset change, and historical labels use the greatest applicable segment.
- [ ] Implement `SuspensionSet` by sorting and merging intervals before subtracting them from an observation interval. Do not infer suspend from a long delay. Preserve actual observation time separately from the attributed bucket timestamp.
- [ ] Implement reconciliation with signed Python integers and the exact read/write/combined equations; return `None` for aggregate D and residuals whenever coverage is unavailable or mixed.
- [ ] Run `python -m pytest -q tests/test_clock.py tests/test_reconcile.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/clock.py src/appio/reconcile.py tests/test_clock.py tests/test_reconcile.py && git commit -m "feat(appio): add boottime arithmetic and reconciliation"`.

## Task 5: Create and validate the exact STRICT SQLite schema-v1 migration

**Implementer tier:** Capable

**Lane:** P1-schema

**Sequence:** P1.2

**depends_on:** Task 1

**Ownership:** `src/appio/schema.py`, `src/appio/migrations.py`, `tests/test_schema.py`

**Files:**

- Create: `src/appio/schema.py`
- Create: `src/appio/migrations.py`
- Create: `tests/test_schema.py`

**Interfaces:**

- Consumes: `validate_sqlite_integer` and shared type names from Task 1.
- Produces `SCHEMA_VERSION = 1`, `SCHEMA_SQL`, `migrate_empty(connection) -> None`, `open_database(path: Path) -> sqlite3.Connection`, and `validate_schema(connection) -> None`.
- `migrate_empty` enables foreign keys outside the transaction, verifies `PRAGMA user_version=0` and no user tables, executes one `BEGIN IMMEDIATE` containing the DDL and `PRAGMA user_version=1`, and commits once. It rejects a nonempty `user_version=0` database without mutation, validates version 1 without recreation, and rejects higher versions without mutation.
- The schema manifest is load-bearing: `boot(boot_id,boot_order,file_finalized_through_mono_ns,started_mono_ns,started_wall,source)`; `app(app_id,key,name,icon_path,first_seen,last_seen,scope)`; `launch(launch_id,app_id,boot_id,root_pid,root_start_ticks,started_mono_ns,ended_mono_ns,proc_count,coverage_json)` with the two specified unique constraints; `sample(boot_id,source,epoch,seq,app_id,launch_id,ts_mono_ns,observed_mono_ns,interval_start_mono_ns,observed_ns,ts_wall,rchar,wchar,read_bytes,write_bytes,delta_kind,first_seen_partial,spans_suspend)`; `sample_coverage(boot_id,source,epoch,seq,app_id,ts_mono_ns,coverage_json)`; and `sample_dev(boot_id,source,epoch,seq,app_id,ts_mono_ns,dev,rbytes,wbytes)`. `path` and `path_alias` carry boot-scoped first/last endpoints and the declared strong/weak identity keys. `file_sample` is keyed by `(boot_id,app_id,ts_mono_ns,path_id)` and `file_coverage` by `(boot_id,app_id,ts_mono_ns)` with all 9 numeric file fields plus the six hook fields. `file_hour_state`, `sampler_checkpoint`, `file_overflow_unknown`, `boot_file_overflow_unknown`, `collector_epoch`, `collector_interval`, `handover`, and `clock_map` use the exact columns and keys in specification section 6.2. `rollup_app`/`rollup_dev` carry `bucket_secs` 60 or 3600 and all three nullable peaks; `app_boot_total`, `app_boot_dev_total`, `app_boot_file_total`, `app_boot_file_coverage`, `app_lifetime_total`, `gap`, and `store_diagnostic` preserve the exact columns, checks, foreign keys, and keys specified there.

### Tests first

- [ ] Write `tests/test_schema.py` against an in-memory SQLite connection and a `tmp_path` file. Assert that a fresh migration creates exactly the 27 named tables, all are marked `STRICT`, the specified composite tables are `WITHOUT ROWID`, `PRAGMA user_version` is 1, and all 15 index names exist: `app_name_idx`, `launch_app_time_idx`, `sample_app_time_idx`, `sample_coverage_app_time_idx`, `sample_dev_app_time_idx`, `path_basename_idx`, `path_alias_basename_idx`, `file_sample_app_time_idx`, `file_sample_last_seen_idx`, `file_coverage_app_time_idx`, `store_diagnostic_time_idx`, `collector_interval_time_idx`, `rollup_app_time_idx`, `rollup_dev_time_idx`, and `gap_time_idx`.
- [ ] Assert representative constraints: `boot.file_finalized_through_mono_ns` is hour-aligned, `sample` rejects negative counters and invalid `delta_kind`, `sample` accepts distinct attributed timestamps for one frame, `sample_coverage` has one row per app receipt, `file_sample.ts_mono_ns` is hour-aligned, `rollup_app.bucket_secs` is 60 or 3600, and `store_diagnostic.code` is `counter_overflow`.
- [ ] Test foreign-key enforcement, canonical primary keys, and `json_valid` checks for coverage/state columns. Test that a nonempty version-0 database and a higher-version database remain byte-for-byte unchanged after a failed open; test version 1 is idempotently validated without duplicate DDL.
- [ ] Test that failed DDL rolls back the transaction and leaves no partial user table set. The test uses a temporary copy of the DDL with a deliberate failing statement and never touches a host database.
- [ ] Run `python -m pytest -q tests/test_schema.py`; it MUST fail before implementation because the migration modules are absent.

### Implementation

- [ ] Transcribe the specification section 6.2 DDL into `SCHEMA_SQL` without adding tables or indexes. Every table declaration ends with `STRICT`; use `WITHOUT ROWID` exactly on the composite-key tables specified by the contract. Preserve all checks, foreign keys, primary keys, unique constraints, and the 15 index definitions named above.
- [ ] The required table definitions must include these load-bearing columns and keys: `sample` includes `observed_mono_ns`, nullable `interval_start_mono_ns`/`observed_ns`, `first_seen_partial`, `spans_suspend`, and primary key `(boot_id,source,epoch,seq,app_id,launch_id,ts_mono_ns)`; `sample_coverage` is keyed by `(boot_id,source,epoch,seq,app_id)`; `sampler_checkpoint` stores `source,boot_id,epoch,seq,checkpoint_mono_ns,state_json`; file tables include hourly keys and all omission/overflow/hook columns; rollups and totals include nullable `peak_read_bps`, `peak_write_bps`, and `peak_total_bps`; `app_lifetime_total` stores `updated_boot_id` and `updated_mono_ns`; diagnostics store stream identity, optional app, time, field, and unique `(boot_id,source,epoch,seq,field)`.
- [ ] Implement migration preconditions so `PRAGMA foreign_keys=ON` is set before `BEGIN IMMEDIATE`, the migration runner never wraps `SCHEMA_SQL` in another transaction, and unsupported state fails closed without rewriting the file.
- [ ] Run `python -m pytest -q tests/test_schema.py`; it MUST pass with no failures and report all schema/index checks successful.
- [ ] Ensure the FINAL commit message is `git add src/appio/schema.py src/appio/migrations.py tests/test_schema.py && git commit -m "feat(appio): add STRICT schema v1 migration"`.

## Task 6: Add canonical durable JSON and collector stream state

**Implementer tier:** Advanced

**Lane:** P1-durable-stream

**Sequence:** P1.3

**depends_on:** Task 1 and Task 5

**Ownership:** `src/appio/durable.py`, `src/appio/stream.py`, `tests/test_durable.py`, `tests/test_stream.py`

**Files:**

- Create: `src/appio/durable.py`
- Create: `src/appio/stream.py`
- Create: `tests/test_durable.py`
- Create: `tests/test_stream.py`

**Interfaces:**

- Consumes: `CounterVector`, AppKey validation, `HourlyFileSnapshot`, `validate_sqlite_integer`, `sqlite3.Connection`, and the schema-v1 tables from Tasks 1 and 5.
- Produces `canonical_json(value) -> str`, `decode_handover(text, expected_boot_id, expected_cutoff)`, `decode_sampler_checkpoint(text, expected_row)`, and `decode_file_hour_state(text, expected_boot_id, expected_hour_start)`. Canonical bytes use UTF-8, sorted object keys, `ensure_ascii=False`, separators `(',', ':')`, decimal integer tokens, no trailing LF, no duplicate keys, and no nonfinite values.
- Produces `CollectorEpoch(source, boot_id, epoch, last_seq, allocated_mono_ns)`, `FrameIdentity(boot_id, source, epoch, seq)`, `allocate_epoch(connection, source, boot_id, allocated_mono_ns)`, and `accept_frame(connection, frame) -> bool`. Epoch allocation is `previous + 1` for the same boot or `1` for a new boot and starts with `last_seq=NULL`; `accept_frame` rejects inactive, stale, and duplicate frames without mutating baselines.
- Durable JSON validators require exact key sets and typed array ordering for version-1 handover, checkpoint, and file-hour state. They reject `{}`, `apps:{}`, duplicate identities/keys, booleans in integer fields, invalid AppKeys, mismatched boot/cutoff/receipt columns, and noncanonical re-encodings.

### Tests first

- [ ] Add tests using the exact canonical handover fixture, sampler checkpoint fixture derived from it, and compact file-hour fixture in specification section 5.4. Assert byte-for-byte canonical output, no final newline, UTF-8 preservation, and sorted object keys.
- [ ] Add malformed JSON tests for missing/extra keys, duplicate keys, arrays replaced by objects, booleans in integer positions, negative/out-of-range counters, invalid AppKeys, duplicate processes/devices/credits, wrong boot/cutoff, and a valid object serialized with noncanonical whitespace. Every malformed input MUST be rejected before database mutation.
- [ ] Test epoch allocation for first boot, same-boot restart, and new boot; assert sequence starts at zero and the old checkpoint can be restored only for the same boot. Test stale/duplicate `FrameIdentity` rejection leaves `collector_epoch.last_seq` and a sentinel baseline unchanged.
- [ ] Run `python -m pytest -q tests/test_durable.py tests/test_stream.py`; it MUST fail before implementation because the modules are absent.

### Implementation

- [ ] Implement canonical JSON parsing with an `object_pairs_hook` that rejects duplicate keys, explicit Python type checks that reject bool-as-int, and a canonical re-encode comparison after structural validation.
- [ ] Implement exact nested schemas. Handover and checkpoint process entries contain exactly `pid,start_ticks,ppid,parent,app_key,launch_root,launch_id,baseline,credits`; credits are FIFO ordered by `(disappearance_epoch,disappearance_seq,child.pid,child.start_ticks)` and contain `child,disappearance_epoch,disappearance_seq,remaining,correction_counted`. Device entries contain exactly `cgroup_id,dev,rbytes,wbytes`.
- [ ] Implement file-hour state validation with top-level `version,boot_id,hour_start_mono_ns,apps,unknown_owner`; app arrays are AppKey-byte sorted, file arrays use the declared typed identity order, aliases are unique byte-sorted, coverage has exactly the specified scalar and six hook keys, and an explicit zero `unknown_owner` object is required.
- [ ] Implement transactional epoch allocation and high-water acceptance against `collector_epoch`, with no baseline mutation in the rejection path.
- [ ] Run `python -m pytest -q tests/test_durable.py tests/test_stream.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/durable.py src/appio/stream.py tests/test_durable.py tests/test_stream.py && git commit -m "feat(appio): validate durable state and stream receipts"`.

## Task 7: Implement the pure identity, delta, credit, and coverage aggregation tick

**Implementer tier:** Capable

**Lane:** P1-sampler

**Sequence:** P1.3

**depends_on:** Task 1, Task 2, Task 3, and Task 4

**Ownership:** `src/appio/launch.py`, `src/appio/sampler.py`, `tests/test_sampler.py`, `tests/test_launch.py`

**Files:**

- Create: `src/appio/launch.py`
- Create: `src/appio/sampler.py`
- Create: `tests/test_sampler.py`
- Create: `tests/test_launch.py`

**Interfaces:**

- Consumes: `StableProcRecord`/`ProcessIdentity` from Task 2, `CgroupObservation`/`DeviceDelta` from Task 3, `IntervalEvidence`/`bucket_start_mono_ns` from Task 4, and `CounterVector`/coverage types from Task 1.
- Produces `SamplerState` containing open process identities, readable baselines, app/parent/root metadata, component-wise pending credits, launch state, and cgroup/device baselines; it is the object serialized by Task 6 and committed by Task 8.
- Produces `LaunchIdentity(root: ProcessIdentity, app_key: AppKey)`, `LaunchState`, `ProcessState(identity, ppid_identity, app_key, launch_identity, baseline, credits, open)`, `TickInput(boot_id, source, epoch, seq, observed_mono_ns, wall_ns, processes, cgroups, suspend_boundaries)`, `AppDelta(app_key, launch_key, counters, attributed_ts_mono_ns, observed_mono_ns, interval_start_mono_ns, delta_kind, first_seen_partial, spans_suspend)`, `FrameCoverage`, and `AggregatedFrame`.
- Produces `aggregate_tick(previous_state: SamplerState | None, tick: TickInput) -> tuple[AggregatedFrame, SamplerState]`. `AggregatedFrame` contains distinct app/launch/attributed-timestamp deltas, one per-app frame coverage receipt even for zero T, device deltas, clock offset, and a serializable checkpoint state.
- Produces `assign_launches()` implementing A06: an app-root identity with no known same-AppKey parent opens a launch; same-AppKey descendants remain in it; concurrent roots remain separate; unknown membership uses launch ID 0 until the store resolves a local surrogate; a launch closes only after all known members disappear.

### Tests first

- [ ] Write the three-tick fixture test from specification section 10.1 using synthetic `StableProcRecord` objects, not host files. Assert Chromium t1 deltas `(rchar=1800,wchar=800,read=180,write=90)` and t2 reap-adjusted parent delta `(100,50,20,10)` after child credit `(500,100,50,10)`.
- [ ] Test PID reuse: `(boot,pid=500,start=700)` followed by `(boot,pid=500,start=900)` closes the old identity, emits no negative delta, does not transfer its baseline or credits, and emits zero for the new zero-counter identity.
- [ ] Test first-seen nonzero counters emit once with `first_seen_partial=1`, `partial_intervals=1`, and `delta_kind="baseline"`; a first-seen zero identity emits zero with `first_seen_partial=0` but still stores a baseline.
- [ ] Test per-field reset with prior `(100,20,10,5)` and current `(90,25,8,9)` emits `(0,5,0,4)`, counts one reset, resets only decreased fields, and measures the next `(95,30,9,10)` from `(90,25,8,9)`.
- [ ] Test open-identity ESRCH closes once and increments resets once, unknown-identity ESRCH increments zero, and an EACCES identity produces no T delta or credit while adding one deduplicated inaccessible PID to its app coverage per tick.
- [ ] Test FIFO component-wise credit consumption, carry-forward, parent disappearance into `reap_unapplied`, no negative adjusted delta, first positive correction counting once, and all-zero `reap_unobserved` without an explicit evidence input.
- [ ] Test the multiple-attributed-timestamp frame: a parent delta attributed to 110 seconds and a new child delta attributed to 120 seconds yield two sample contributions, while one frame-level coverage receipt at 120 seconds has `partial_intervals=1` and `spans_suspend_intervals=1` only once.
- [ ] Test launch grouping for one root plus same-AppKey descendants, concurrent roots, AppKey changes, and closure only after the last known member disappears.
- [ ] Run `python -m pytest -q tests/test_sampler.py tests/test_launch.py`; it MUST fail before implementation because the sampler modules are absent.

### Implementation

- [ ] Keep sampler state private until the caller commits it. Process validated identities in ascending numeric PID order; preserve an open identity on an unstable stat read, preserve a readable baseline across EACCES, and discard old state on PID reuse.
- [ ] For each readable identity, emit current counters once when no baseline exists, otherwise subtract each component independently and clamp only decreased components to zero while replacing only those baselines. Set `delta_kind` precedence `reset > baseline > normal`.
- [ ] When a child disappears with a readable baseline and known last parent, enqueue its cumulative vector with disappearance epoch/sequence/PID ordering. Consume FIFO credits component-wise from each parent delta, never below zero; count a correction on first positive use and put remaining credit into `reap_unapplied` if the parent closes. Never classify a parent jump as `reap_unobserved` from magnitude.
- [ ] Use `attribute_interval()` from Task 4 for each contribution so suspend-spanning deltas retain actual observation time but use the last pre-suspend attributed timestamp and measured active denominator. Aggregate app rows by `(AppKey, launch identity, attributed timestamp)` while reducing frame coverage once per app at the observed timestamp.
- [ ] Aggregate cgroup/device observations once per `(AppKey,dev)`, apply the shared-cgroup degradation from Task 3, and preserve `device_state="not_delegated"` without zero-valued D rows.
- [ ] Run `python -m pytest -q tests/test_sampler.py tests/test_launch.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/launch.py src/appio/sampler.py tests/test_sampler.py tests/test_launch.py && git commit -m "feat(appio): implement identity and credit-based sampling"`.

## Task 8: Build the atomic store writer, rollups, totals, and checkpoint persistence

**Implementer tier:** Capable

**Lane:** P1-store-write

**Sequence:** P1.4

**depends_on:** Task 4, Task 5, Task 6, and Task 7

**Ownership:** `src/appio/store.py`, `tests/test_store.py`, `tests/test_store_overflow.py`

**Files:**

- Create: `src/appio/store.py`
- Create: `tests/test_store.py`
- Create: `tests/test_store_overflow.py`

**Interfaces:**

- Consumes: `AggregatedFrame`/`SamplerState` from Task 7, schema/migration functions from Task 5, canonical checkpoint/receipt functions from Task 6, and clock/reconciliation primitives from Task 4.
- Produces `AppIoStore(connection_or_path)`, `start_boot(boot_id, boot_order, started_mono_ns, started_wall, source)`, `start_collector(source, boot_id, allocated_mono_ns) -> CollectorEpoch`, `record_tick(frame: AggregatedFrame, next_state: SamplerState) -> None`, `restore_checkpoint(source, boot_id) -> SamplerState | None`, `record_gap(...)`, and `physical_store_bytes() -> int`.
- `record_tick` commits stream receipt, clock map, app/launch identity rows, all sample rows, exactly one per-app `sample_coverage` row including zero-T apps, sample devices, both 60-second and 3600-second rollups, boot/lifetime totals, and the sampler checkpoint in one `BEGIN IMMEDIATE` transaction. It publishes the in-memory state only after commit.
- Produces deterministic local IDs: new apps sorted by AppKey UTF-8 bytes, launches by `(app_id, started_mono_ns, root_pid, root_start_ticks)`, and paths by their declared uniqueness tuple. `app_id`, `launch_id`, and other local surrogates never leave the store as external identity.
- On a representable overflow, rolls back sequence, rows, baselines, totals, and checkpoint, then persists a separate `store_diagnostic(code="counter_overflow")` in a new representable transaction and raises `CounterOverflowError`. No out-of-range numeric payload is stored.

### Tests first

- [ ] Write a store integration test that migrates a temporary empty database, starts boot/source/epoch, records the three process/cgroup fixture frames, and asserts the exact Chromium values from specification section 10.4: T `(read=200,write=100,total=300)`, D `259:0=(120,70)`, `259:1=(50,50)`, residual `(30,-20,10)`, `inaccessible_pids=3`, and `reap_corrections=1`.
- [ ] Assert one `sample_coverage` receipt per accepted app/frame even when T is zero, distinct attributed timestamps under one frame, `observed_mono_ns` and interval evidence, both rollup widths receiving the frame once, nullable peak values preserving defined zero versus undefined `NULL`, and boot/lifetime totals receiving one additive update.
- [ ] Test duplicate and stale frames by replaying `(boot,gui,1,2)` and an older sequence; assert every row, total, coverage value, baseline, checkpoint, and high-water mark is unchanged.
- [ ] Test crash semantics with a committed baseline/output of 100 bytes, a simulated crash before the next commit, epoch allocation to 2, checkpoint restore, and cumulative value 120; assert the next output is 20 and durable total is 120, with a gap after the checkpoint.
- [ ] Test a same-boot restart restores open identities, credits, app/parent/root metadata, and device baselines; test a new boot rejects the old checkpoint and starts fresh.
- [ ] Test an input counter of `9223372036854775808` raises `CounterOverflowError`, leaves sequence/baseline/totals unchanged, persists one diagnostic with the offending field, and makes the affected query coverage incomplete.
- [ ] Test mapped clock offsets and `physical_store_bytes()` sum the main DB, `-wal`, and `-shm`, treating a missing sidecar as zero; no test opens a privileged path.
- [ ] Run `python -m pytest -q tests/test_store.py tests/test_store_overflow.py`; it MUST fail before implementation because the store module is absent.

### Implementation

- [ ] Make `AppIoStore` open/migrate a caller-provided path or connection and use one consistent connection with `PRAGMA foreign_keys=ON`; never create an implicit second store for a query.
- [ ] In `record_tick`, begin immediate, validate active `(boot_id,source,epoch)` and sequence before touching state, update `clock_map`, allocate/reuse local app and launch IDs deterministically, materialize every distinct attributed timestamp, write sample/dev/coverage rows, update both rollup widths directly from frame contributions, update totals and nullable interval-rate peaks, serialize the exact checkpoint, and commit once.
- [ ] Keep file coverage in process-only rows as the exact zero placeholder; do not fabricate a file observation from a process tick. Preserve reserved stream fields and future file-hour columns for Task 9.
- [ ] Implement diagnostic persistence after a rolled-back overflow in a separate transaction; if diagnostic persistence fails, surface that failure instead of acknowledging the observation.
- [ ] Implement checkpoint restore and gap creation so post-checkpoint time is explicitly uncovered and the restored cumulative baseline is subtracted from the next sample.
- [ ] Run `python -m pytest -q tests/test_store.py tests/test_store_overflow.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/store.py tests/test_store.py tests/test_store_overflow.py && git commit -m "feat(appio): persist atomic samples, rollups, totals, and checkpoints"`.

## Task 9: Implement synthetic hourly file contracts and deterministic retention primitives

**Implementer tier:** Capable

**Lane:** P1-files-retention

**Sequence:** P1.4

**depends_on:** Task 1, Task 4, Task 5, and Task 6

**Ownership:** `src/appio/files.py`, `src/appio/file_store.py`, `src/appio/quota.py`, `tests/test_files.py`, `tests/test_file_store.py`, `tests/test_quota.py`

**Files:**

- Create: `src/appio/files.py`
- Create: `src/appio/file_store.py`
- Create: `src/appio/quota.py`
- Create: `tests/test_files.py`
- Create: `tests/test_file_store.py`
- Create: `tests/test_quota.py`

**Interfaces:**

- Consumes: `FileObservation`, `FileCoverageInput`, `UnknownOwnerInput`, `HourlyFileSnapshot`, canonical JSON validators, clock-map lookup, and schema-v1 connection from Tasks 1, 4, 5, and 6.
- Produces `HourlyAccumulator(boot_id, hour_start_mono_ns)`, `add_observation(receipt, app_key, observation, coverage)`, `add_unknown_owner(receipt, unknown)`, and `finalize(top_n=100) -> HourlyFileSnapshot`. The accumulator merges all authoritative contributions in one app-hour by `(app_key,fs_uuid,dev,inode,generation,identity_weak)`, keeps every candidate until finalization, chooses the bytewise least observed basename, preserves other names as aliases, and applies the deterministic top-N key `(bytes_r+bytes_w desc, reads+writes desc, dev,inode,generation,identity_weak,fs_uuid,basename,path_id asc)`.
- Produces `FileHourStore(connection)` with `apply_pending(snapshot)`, `finalize_hour(snapshot)`, and `unknown_owner_for_hour(boot_id,hour_start)`. It validates canonical `file_hour_state`, writes one `file_sample` and one `file_coverage` per selected app-hour, exactly one `file_overflow_unknown` row per boot-hour including explicit zero remainder, updates `app_boot_file_total`/`app_boot_file_coverage`, and advances `boot.file_finalized_through_mono_ns` without reopening a finalized hour.
- Produces `QuotaPruner(connection, database_paths, quota_bytes=1_500_000_000)` with `expire(now_boot_id, now_mono_ns)` and `shrink_if_needed()`. It maps monotonic times through `clock_map`, applies independent 24-hour raw, 14-day hourly-file, 30-day 60-second, and 365-day 3600-second retention, uses the complete deterministic deletion key, checkpoints each deletion, and pauses sampling if the fresh physical size remains above quota.

### Tests first

- [ ] Test the specification’s synthetic hourly vector: three file identities aggregate to retained `(bytes_r=100,bytes_w=120,reads=5,writes=4,total=220)`, counted omission is `(files=1,bytes_r=20,bytes_w=30)`, keyed overflow is `(attempts=1,bytes_r=5,bytes_w=7)`, and unknown-owner overflow is `(attempts=2,bytes_r=11,bytes_w=13)` without app allocation.
- [ ] Test contributions partitioned across two collectors in one app-hour produce one snapshot and one coverage row, never one top-N budget per drain. Test top-2 and strong/weak tie vectors produce the exact selected identities regardless of insertion order.
- [ ] Test rename/hard-link behavior: `a.txt` and `b.txt` create two basename-specific path rows, one canonical `a.txt` file sample, one alias with first/last observations, merged counters, and LAST at the later observation. Test inode generation and weak/strong identities remain separate.
- [ ] Test zero-F monitored hours create explicit app and unknown-owner coverage rows, while skipped nonfiles/unsupported files enter coverage but never F bytes. Test an unknown-owner remainder is never copied into any app row.
- [ ] Test pending hour state survives a reopen, finalization is idempotent, an hour below `file_finalized_through_mono_ns` cannot reopen, and file totals survive raw process expiry.
- [ ] Test quota mapped-wall collision with rollup buckets at 0 and 60 seconds and offsets 1000/940 seconds; assert the complete primary-key suffix chooses bucket 0 first independent of insertion order. Test physical quota size includes DB/WAL/SHM and that a missing mapping stops deletion and marks coverage incomplete.
- [ ] Run `python -m pytest -q tests/test_files.py tests/test_file_store.py tests/test_quota.py`; it MUST fail before implementation because these modules are absent.

### Implementation

- [ ] Implement the all-contribution hourly accumulator and canonical path/alias rules. Treat `fs_uuid=""` and `identity_weak=1` as weak identity, exclude no file from F merely because it is weak, and keep counted omissions and uncounted overflow separate from retained rows.
- [ ] Implement exact state JSON validation and atomic file-hour finalization. The pending object must contain exactly `version,boot_id,hour_start_mono_ns,apps,unknown_owner`; arrays and receipt ordering must be canonical; the unknown-owner object must exist even with zero counters.
- [ ] Implement `FileHourStore` with a single app-hour primary key across collectors, exact coverage columns plus canonical `coverage_json`, boot file totals with persisted last-seen boot identity, and no reopening below the durable finalized watermark.
- [ ] Implement retention’s mapped-wall age and deterministic ordering. `physical_store_bytes()` must sum `st_size` for the main database and sidecars; the decimal quota is exactly `1_500_000_000`; totals, diagnostics, boot chronology, and path identities referenced by totals are never deleted.
- [ ] Run `python -m pytest -q tests/test_files.py tests/test_file_store.py tests/test_quota.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/files.py src/appio/file_store.py src/appio/quota.py tests/test_files.py tests/test_file_store.py tests/test_quota.py && git commit -m "feat(appio): add hourly file contracts and retention"`.

## Task 10: Implement tiered queries, coverage reduction, and signed residual results

**Implementer tier:** Capable

**Lane:** P1-query

**Sequence:** P1.5

**depends_on:** Task 4, Task 8, and Task 9

**Ownership:** `src/appio/tier.py`, `src/appio/query.py`, `tests/test_query.py`, `tests/test_query_properties.py`

**Files:**

- Create: `src/appio/tier.py`
- Create: `src/appio/query.py`
- Create: `tests/test_query.py`
- Create: `tests/test_query_properties.py`

**Interfaces:**

- Consumes: `AppIoStore` from Task 8, `FileHourStore` from Task 9, all result dataclasses and exact A22 messages from Task 1, and reconciliation from Task 4.
- Produces `select_tier(window, available_cutoff) -> Tier` with disjoint inclusive boundaries: `<=24h` raw, `>24h <=30d` rollup_60s, `>30d <=365d` rollup_3600s; a bounded range over 365 days raises A22 `unsupported_window`; launch raises A22 `launch_attribution` before any ID lookup; exact full since-boot/lifetime scopes may use totals-only; historical non-full boot prefixes over 365 days raise the exact historical-prefix message.
- Produces the exact functions `ranking(window: Window, metric: Metric) -> list[AppTotal]`, `series(app_id: int, window: Window) -> Series`, `disk_share(app_id: int, window: Window) -> DiskShare`, `file_table(app_id: int, window: Window, sort: FileSort, direction: Direction, search: str | None) -> FileTable`, `file_coverage(app_id: int, window: Window) -> FileCoverage`, and `unknown_owner_file_coverage(window: Window) -> UnknownOwnerOverflow`.
- Every function uses one SQLite snapshot, validates scope before app lookup, reduces persisted coverage from the exact selected/deduplicated rows by sum/component-wise sum/OR/device-state rules, then applies overlays for clipping, gaps, pruning, pending tails, diagnostics, tier, and unavailability. File queries use independent hourly/boot sources; no process row is treated as file data.

### Tests first

- [ ] Seed a temporary store with raw, 60-second, 3600-second, totals, file-hour, gap, and diagnostic fixtures through the test-only store APIs. Test exact boundaries `24h`, `24h+1ns`, `30d`, `30d+1ns`, `365d`, and `365d+1ns`; assert no tier blending and exact A22 errors/messages.
- [ ] Test `ranking()` ordering by metric descending then AppKey UTF-8 bytes, empty ranking as `[]`, unknown app as `KeyError`, and `AppTotal.total_bytes` as read plus write. Assert every returned total has coverage.
- [ ] Test `series()` point ordering, raw/rollup bucket fields, peak maxima over defined interval rates, `None` for totals-only peaks, `spans_suspend` OR, and no fabricated zero points for missing/pruned buckets.
- [ ] Test `disk_share()` with complete D and signed residuals, negative residuals, distinct equal device tokens across boots, and incomplete/not-delegated D. Assert aggregate D and every residual is `None` in the unavailable branch while numeric T remains present; the test must guard before adding any `None`.
- [ ] Test coverage equality against source rows and independent overlays: deduplicated coverage counts, four reap components, hook hits, weak rows, diagnostic with no surviving raw row, clipped boundary, gap, pending hour, and pruning. Counted omitted bytes remain exact and do not make F byte accounting incomplete by themselves.
- [ ] Test file search with `NFC(search).casefold()` substring matching before sorting, explicit direction, exact sort tie suffixes, basename spelling preservation, last-seen chronology by boot order, retained-observation labeling, empty/incomplete result with `not_retained` for an unrepresentable file window, and one global unknown-owner result not multiplied per app.
- [ ] Add deterministic seeded property-style loops for device reconciliation, file byte equality, bucket partition, per-field resets, PID reuse, credit conservation, tier boundaries, stream dedupe, and nullable peak reduction. Record the seed in an assertion message and exercise both complete and incomplete branches.
- [ ] Run `python -m pytest -q tests/test_query.py tests/test_query_properties.py`; it MUST fail before implementation because the query modules are absent.

### Implementation

- [ ] Implement tier selection and disabled-window checks before ID lookup. Use only the selected source tier; do not replace pruned historical data with a wider tier or totals.
- [ ] Implement SQL reducers for T, D, F, coverage, peaks, boot/lifetime totals, and diagnostics. Keep unknown-owner overflow as a separate result/context quantity and never add it to an app’s F or duplicate it once per app.
- [ ] Implement signed residuals per bucket and aggregate. For complete D, use `R_read=T_read-sum(D_read)`, `R_write=T_write-sum(D_write)`, and `R=R_read+R_write`; for incomplete D return `None` for all D/residual aggregates and preserve T.
- [ ] Implement exact file sorts, Unicode search, hourly completeness, full-boot/lifetime file totals, and the A17 empty/incomplete response for supported but unrepresentable file windows. Preserve `Coverage.unavailable_reason` as query-derived data rather than persisted counter data.
- [ ] Run `python -m pytest -q tests/test_query.py tests/test_query_properties.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/tier.py src/appio/query.py tests/test_query.py tests/test_query_properties.py && git commit -m "feat(appio): implement tiered queries and coverage overlays"`.

## Task 11: Build the P2 thin PyQt6 application window

**Implementer tier:** Advanced

**Lane:** P2-window

**Sequence:** P2.1

**depends_on:** Task 10

**Ownership:** `src/appio/ui/__init__.py`, `src/appio/ui/main_window.py`, `src/appio/ui/formatting.py`, `tests/test_ui.py`

**Files:**

- Create: `src/appio/ui/__init__.py`
- Create: `src/appio/ui/main_window.py`
- Create: `src/appio/ui/formatting.py`
- Test: `tests/test_ui.py`

**Interfaces:**

- Consumes: the query functions and result dataclasses from Task 10 through a `QueryService` protocol with `ranking(window, metric)`, `disk_share(app_id, window)`, and `refresh`-safe methods.
- Produces `AppIoWindow(query_service, boot_id: str, end_mono_ns: int) -> QMainWindow`, `refresh() -> None`, `selected_app_id() -> int | None`, and formatting helpers `format_bytes(value: int) -> str` and `format_residual(value: int | None, complete: bool) -> str`.
- The window renders a top `appio` title, an application list with application name and since-boot total, a selected-application KPI area, and a signed residual label. The residual wording is `difference between accounting points`; a negative residual includes `attributed exceeds task accounting by <formatted value>` and a positive residual includes its signed value. Incomplete/unavailable D shows an explicit unavailable state without arithmetic.
- The window uses the approved detail-view layout as a compact reference: application list on the left, KPI/residual content on the right, and visible coverage state. It deliberately contains no tray action, daemon switch, Files tab, BPF state, socket control, or privilege flow.

### Tests first

- [ ] Add a pytest-qt test with a fake `QueryService` returning two `AppTotal` rows and a `DiskShare` with residual `-20`. Show `AppIoWindow` offscreen and assert the visible list contains both names and since-boot totals, the selected row is displayed, and the residual label contains the signed value and `difference between accounting points`.
- [ ] Add a test with incomplete device coverage and `None` residuals; assert the window shows unavailable device/residual text and never displays `0` as a substitute.
- [ ] Add a test that the window has no `Files` tab, tray menu, daemon control, or privileged action, and that selecting a list row triggers only the query service’s disk-share read.
- [ ] Run `QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_ui.py`; it MUST fail before implementation because the UI modules are absent.

### Implementation

- [ ] Implement the window with Qt widgets available in PyQt6 and no external UI framework. Build the list from `ranking(Window(kind="since_boot", boot_id=boot_id, end_mono_ns=end_mono_ns), "total_bytes")`; display names and formatted totals without changing stored arithmetic.
- [ ] On selection, query `disk_share()` for the selected AppKey and update the residual/coverage labels. Use the `Coverage` state to choose unavailable text; never coerce `None` to zero.
- [ ] Keep the layout small and deterministic for the offscreen test: title/header, list, KPI strip, residual strip, and coverage note. Do not add later-plan tabs, tray objects, daemon controls, BPF/file panels, or setup flows.
- [ ] Run `QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_ui.py`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add src/appio/ui/__init__.py src/appio/ui/main_window.py src/appio/ui/formatting.py tests/test_ui.py && git commit -m "feat(appio): add thin since-boot Qt window"`.

## Task 12: Prove the P1/P2 vertical slice against the complete fixture tree

**Implementer tier:** Capable

**Lane:** P2-acceptance

**Sequence:** P2.2

**depends_on:** Task 2, Task 3, Task 8, Task 10, and Task 11

**Ownership:** `tests/acceptance/test_p1_p2.py`, `tests/acceptance/test_no_later_scope.py`, `tests/fixtures/acceptance/**`

**Files:**

- Create: `tests/acceptance/test_p1_p2.py`
- Create: `tests/acceptance/test_no_later_scope.py`
- Create: `tests/fixtures/acceptance/**`

**Interfaces:**

- Consumes: `ProcReader` and AppKey resolution from Task 2, cgroup fixture readers from Task 3, `AppIoStore` from Task 8, query functions from Task 10, and `AppIoWindow` from Task 11.
- Produces a deterministic end-to-end acceptance fixture and no production interface changes. The fixture’s public assertion is that the same `/proc` and cgroup trees produce the same stored rows, totals, coverage, signed residual, and rendered screen on every run.

### Tests first

- [ ] Build static acceptance fixture trees under `tests/fixtures/acceptance/proc/` and `tests/fixtures/acceptance/cgroup/` for t0/t1/t2, the PID 500 reuse, PID 411 reap, PID 413 EACCES metadata, Chromium cgroup devices, and a controller-absent cgroup. No test reads `/proc`, `/sys/fs/cgroup`, or `/proc/diskstats` outside this fixture directory.
- [ ] Write the end-to-end test that feeds the fixture through `ProcReader`, cgroup reader, sampler, and store, then queries `since_boot`. Assert exact values: Chromium T `(read=200,write=100,total=300)`, D `(170,120,290)`, residual `(30,-20,10)`, per-tick inaccessible count `1` for each of three frames, one reap correction, no PID-reuse negative delta, and no device rows for the controller-absent fixture.
- [ ] In the same test, instantiate the actual offscreen `AppIoWindow`, show it with pytest-qt, select Chromium, and assert the screen exposes the since-boot total and signed residual text. This is the P2 gate that a real screen agrees with `/proc` and `io.stat` by hand.
- [ ] Add `test_no_later_scope.py` asserting the thin window’s object tree contains no Files tab or tray menu and the acceptance path never imports a daemon, socket, BPF, polkit, or setup module. This test must inspect only the P2 window and package modules, not require those later modules to exist.
- [ ] Run `QT_QPA_PLATFORM=offscreen python -m pytest -q tests/acceptance/test_p1_p2.py tests/acceptance/test_no_later_scope.py`; it MUST fail before the fixture and acceptance test are implemented.

### Implementation

- [ ] Materialize the fixture files with the exact line endings and values from specification section 10.1 and 10.2, including the boot ID, `CLK_TCK=100`, wall offset, cgroup controller files, and device `io.stat` rows.
- [ ] Connect the fixture readers to the production sampler/store interfaces without adding test-only branches to production code. Keep frame source `gui`, epoch `1`, sequences `0`, `1`, `2`, and use a temporary SQLite database.
- [ ] Assert the full database/query/UI result in one test and include coverage text for EACCES and signed residual arithmetic. Ensure duplicate frame replay in the acceptance path changes no rows or displayed totals.
- [ ] Run `QT_QPA_PLATFORM=offscreen python -m pytest -q tests/acceptance/test_p1_p2.py tests/acceptance/test_no_later_scope.py`; it MUST pass with no failures.
- [ ] Run the complete suite with `QT_QPA_PLATFORM=offscreen python -m pytest -q`; it MUST pass with no failures.
- [ ] Ensure the FINAL commit message is `git add tests/acceptance/test_p1_p2.py tests/acceptance/test_no_later_scope.py tests/fixtures/acceptance && git commit -m "test(appio): verify P1 contracts through the P2 screen"`.
