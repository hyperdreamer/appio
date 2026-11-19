# Adjustable sample period implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal**

Implement the approved D1–D13 sample-period setting for the system `appio-iod` service, with canonical `APPIO_SAMPLE_SECONDS` values from 2 through 30 seconds, durable privileged updates, truthful IPC and GUI observations, and executable acceptance coverage.

**Architecture**

The production daemon resolves the environment setting once at its module-level entry point and carries the resulting nanosecond cadence through sampling, flush, maintenance, recovery, shutdown, and status reporting. The helper remains a fixed-argv, root-owned operation. Its period handler uses a separate systemd configuration query, safe fixed filesystem primitives, and a transaction journal to distinguish a fresh apply, repair, unchanged configuration, and incomplete lifecycle transitions. The GUI treats the spin box as the requested value and `ServiceFacts.sample_ns` as the observed value, preserving the existing collector ownership and activation rules.

**Tech Stack**

Python 3.14, PyQt6, pytest, pytest-qt, SQLite-backed history, systemd command seams, injected filesystem and runner dependencies, and the existing `appio` IPC protocol.

## Global Constraints

- Use `docs/superpowers/specs/2026-11-17-sample-period-settings-spec.md` as the implementation contract. Its SHA-256 is `ebf3d082c002b59a0d369856c961b498fc4a70f46d8cec8c6b4c2c41e4bf3751`. Preserve the approved design at commit `323eb8d612b45b5efa9dbca925cf6a5844693f8c`, whose recorded blob SHA-256 is `93b0cf6c31355725fe0d8ebbd071cd833e7d74cb5e256078abba6f7f1b6208c8`.
- Preserve all D1–D13 decisions. Implement one system-service integer setting only. Do not add a second setting, change read-only IPC request vocabulary, change SQLite schema, change retention or rollup formats, or make the helper start an inactive service.
- Keep the six legacy helper verbs in their existing two-element form. The seventh verb, `set-sample-period`, is the only form that accepts a third argv element, and that element is a canonical decimal period from 2 through 30.
- Keep all privileged operations under the existing stdlib-only shim trust order, `PKEXEC_UID` identity source, fixed target allowlists, component-wise `O_NOFOLLOW` traversal, root ownership and write-mode checks, common helper lock, bounded runner, atomic replacement, file and directory fsyncs, and bounded result encoding.
- Preserve legacy inputs and behavior. Existing `Daemon` embedders retain deterministic constructor defaults. Existing status replies without `sample_ns` remain accepted. Existing lifecycle verbs leave the sample drop-in untouched. GUI-owned local collection, `tick_local`, and `appio --interval` remain unchanged.
- Keep the lifecycle `SHOW_ARGV`, the period configuration `SAMPLE_SHOW_ARGV`, and the GUI `UNIT_SHOW_ARGV` as separate fixed property contracts. A parser must reject fields outside its own contract and must never consume another parser’s output contract.
- Use existing injected clocks, runners, launchers, status readers, filesystem roots, metadata seams, and temporary databases for rootless tests. Do not read or mutate host `/etc`, `/var`, systemd state, installed databases, or the system journal from unit tests.
- Keep task file ownership disjoint. Follow the dependency order in the task inventory before editing a consumer of a produced interface. If the execution controller integrates tasks in parallel, it must respect these dependencies and must not merge overlapping edits without review.
- Implementers commit each verified task on its isolated lane branch. The PM controller owns candidate integration and delivery. This plan authorizes implementation changes only in the files listed by each task. Do not modify the design, specification, unrelated source, unrelated tests, lockfiles, package metadata, or the systemd unit content.
- `data/systemd/appio-iod.service` remains byte-equivalent. In particular, do not add `APPIO_SAMPLE_SECONDS` to the unit. The fixed `/etc/systemd/system/appio-iod.service.d/20-sample-period.conf` drop-in owns the deployment setting.
- Implement and dry-run-test `period-runtime` and `period-capacity`. Run `period-capacity` against its disposable materialized SQLite fixture once implemented. Run `period-runtime` on the disposable systemd login specified by the acceptance contract. Record host evidence accurately, keeping `Current host evidence: UNRUN.` until evidence is collected. Never use sudo or a host transition command.
- Every task must leave its targeted test file and the normal entry point consistent. Do not claim a task passes until its exact commands have been run and their output recorded by the implementer.

## Task 1: Resolve configurable cadence and status protocol

**Implementer tier:** Advanced

**Lane/category**

- Lane `runtime-cadence` and `status-contract`.
- This is the first implementation lane and has no task dependency.

**Files**

- Modify `src/appio/iod.py`.
- Modify `src/appio/ipc_models.py`.
- Modify `tests/p3/test_iod_cadence_pins.py`.
- Inspect and preserve `data/systemd/appio-iod.service` without editing it.

**Interfaces**

- Consumes no new task interface.
- Produces `PERIOD_TEXT: re.Pattern[str] = re.compile(r"(?:[2-9]|[12][0-9]|30)", re.ASCII)`.
- Produces `parse_sample_seconds(raw: object) -> int | None`.
- Produces `configured_sample_ns(environ: Mapping[str, str] | None = None) -> int`.
- Produces `flush_ns_for(sample_ns: int) -> int`.
- Produces `maintenance_ns_for(sample_ns: int) -> int`.
- Produces the `Daemon` keyword-only constructor tail `sample_ns: int = SAMPLE_NS, flush_ns: int = MAX_FLUSH_NS, maintenance_ns: int = MAINTENANCE_NS, file_source_factory: Callable | None = None`.
- Produces `Daemon._status(...)` with the existing eight status values plus `sample_ns` equal to the constructor value.
- Produces an IPC status validator that accepts the eight required keys plus optional `sample_ns`, normalizes an absent field to no period, and raises `ProtocolError("unknown_key", ...)` or `ProtocolError("invalid_field", ...)` according to the status contract.

### Step 1: Add falsifiable cadence and validator tests

- [ ] Keep `test_raw_persistence_follows_the_documented_ten_second_cadence`'s 15 two-second ticks and existing sequence and total assertions. Extend `tests/p3/test_iod_cadence_pins.py` with the exact named tests from the specification. Add `test_parse_sample_seconds_acceptance_table`, `test_parse_sample_seconds_refusal_table`, `test_configured_sample_ns_invalid_values_warn_once_and_start`, `test_flush_ns_for_uses_three_periods_at_two_ten_eleven_and_thirty`, `test_maintenance_ns_for_clamps_all_supported_periods`, `test_status_reports_constructor_sample_ns`, `test_production_main_passes_valid_period_and_derived_windows`, `test_production_main_uses_ten_second_default_when_environment_is_absent`, `test_main_empty_environment_starts_with_default_period`, and `test_status_validator_accepts_period_and_older_status`.
- [ ] Make the status cases distinguish an absent `sample_ns` key from explicit JSON `null`. Cover valid integer bounds, bool, float, string, zero, negative, above-30-second, and unknown-key failures with the exact protocol reasons.
- [ ] Add controlled-clock tests named `test_sampling_overdue_gate_uses_derived_flush_window`, `test_step_overdue_gate_uses_derived_flush_window`, and `test_shutdown_recovery_gate_uses_derived_flush_window`.

### Step 2: Run the red cadence vector

- [ ] Run `python -m pytest -q tests/p3/test_iod_cadence_pins.py`.
- [ ] Confirm the new assertions fail for the missing parser, derived windows, constructor parameter, status field, and validator behavior. Keep failures attributable to the requested mechanism rather than changing assertions to match current behavior.

### Step 3: Implement the parser and derived windows

- [ ] Implement `parse_sample_seconds` with `type(raw) is str` and `PERIOD_TEXT.fullmatch`, without coercion or logging.
- [ ] Implement `configured_sample_ns` so only `APPIO_SAMPLE_SECONDS` is read, absent and empty values use `SAMPLE_NS`, canonical values multiply by `NS`, and all other present values use the default while logging exactly one single-line warning `sample_period_invalid value=<escaped>` with `repr(raw)[:64]`. Invalid input must never raise or block startup.
- [ ] Implement the positive-integer, bool-excluding validation shared by `flush_ns_for` and `maintenance_ns_for`. Return `max(30 * NS, 3 * sample_ns)` for flush and `min(60 * NS, max(15 * NS, 3 * sample_ns))` for maintenance. Preserve the exact `ValueError("sample_ns must be a positive integer")` message.

### Step 4: Thread cadence through the daemon

- [ ] Add and validate `maintenance_ns`, raising `ValueError("maintenance_ns must be a positive integer")` for invalid values. Preserve the existing `sample_ns` and `flush_ns` checks and constructor defaults.
- [ ] Replace every production use of the module constant for maintenance deadlines in `start`, `step`, sampling recovery, and `stop` with `self._maintenance_ns`. Keep all three flush gates on the instance `_flush_ns` value.
- [ ] In `main()`, resolve `sample_ns`, `flush_ns`, and `maintenance_ns` before constructing the production daemon. Ensure only `main()` calls `configured_sample_ns`, and preserve deterministic injected-constructor behavior.
- [ ] Add `sample_ns` to the daemon status envelope without changing its other eight fields or values. The value must be the constructor value and must not perform a fresh environment lookup.

### Step 5: Implement and verify optional status validation

- [ ] Extend `_status` in `src/appio/ipc_models.py` so the required-key set remains exactly `protocol`, `type`, `request_id`, `enabled`, `answering`, `pending_activation`, `ownership_start_mono_ns`, and `cursor`, while the allowed set adds only `sample_ns`.
- [ ] Accept a missing field as no period. Treat explicit `null` as a present invalid value. Accept only an integer excluding bool, at least 1, no greater than `30 * NS`, and inside the existing SQLite integer bound.
- [ ] Preserve the existing normalized envelope and error taxonomy for all other fields.

### Step 6: Run the task verification vector

- [ ] Run `python -m pytest -q tests/p3/test_iod_cadence_pins.py tests/p3/test_iod.py tests/p3/test_ipc_protocol.py`.
- [ ] Confirm the systemd unit has no sample environment assignment and that the task diff changes only the three owned files.

### Step 7: Commit

- [ ] Run `git add src/appio/iod.py src/appio/ipc_models.py tests/p3/test_iod_cadence_pins.py`.
- [ ] Run `git commit -m "feat: configure daemon sampling cadence"` and report the commit SHA.

## Task 2: Implement the sample period setup transaction

**Implementer tier:** Frontier

**Lane/category**

- Lane `setup-transaction`.
- Depends on Task 1. Boundary, safe-filesystem, client, and service changes are one task because `setup_service` asserts that its literal dispatch table equals `VERBS` at import time. Commit the combined result only after all of these components and their tests pass.

**Files**

- Modify `src/appio/setup_boundary.py`.
- Modify `data/libexec/appio-setup`.
- Modify `src/appio/setup_client.py`.
- Modify `tests/p3/test_setup_boundary.py`.
- Modify `tests/p3/test_setup_client.py`.
- Modify `src/appio/setup_files.py`.
- Modify `src/appio/setup_delegation.py`.
- Modify `tests/p3/test_setup_files.py`.
- Modify `tests/p3/test_setup_delegation.py`.
- Modify `src/appio/setup_service.py`.
- Modify `tests/p3/test_setup_service.py`.

**Interfaces**

- Consumes Task 1 status validation and `sample_ns` observations, together with the existing `SetupDeps`, `FixedTarget`, `InvocationError`, `CommandResult`, `SetupIdentity`, and rootless dependency seams.
- Produces `Invocation(verb: str, identity: SetupIdentity, period_seconds: int | None = None)`.
- Produces a seven-element `VERBS` set with the six legacy verbs and `set-sample-period`.
- Produces `SetupClient.invoke(self, verb: str, *, period_seconds: int | None = None) -> Future`.
- Produces `decode_body(text: str, verb: str, expected_uid: int) -> dict | None` that validates `sample_period` and `sample_period_state` as part of the whole result document. For each of the six legacy verbs, require both new fields to be `None`. For a trusted `set-sample-period` result, require an exact integer period from 2 through 30 and require the invoke completion path to match it to the requested seconds. Require `sample_period_state=None` when the result has a non-`None` error, and otherwise accept only the closed success-state set or `None` where the result contract permits it.
- Produces fixed target declarations in `setup_boundary.FIXED_TARGETS` for the sample drop-in and `/var/lib/appio/sample-period.pending.json` and `/var/lib/appio/sample-period.complete.json`. Journal targets remain fixed state targets and are separate from configuration target membership.
- Produces `SAMPLE_PERIOD_DROPIN: Path = Path("/etc/systemd/system/appio-iod.service.d/20-sample-period.conf")`.
- Produces `sample_period_payload(seconds: int) -> bytes` and `parse_sample_period_dropin(data: bytes) -> int | None` with the exact canonical-byte contract.
- Produces `SafeRootFS.read_bounded(path: str, *, max_bytes: int, create_parents: bool = True) -> bytes` with the legacy creating default and a pinned, non-creating mode.
- Produces `load_setup_state(fs: SafeRootFS, *, create_parents: bool = True) -> SetupState`, preserving the creating default while forwarding the flag to `read_bounded`.
- Produces a strict journal record type `PeriodJournalRecord(operation_id: str, target: str, sample_seconds: int, target_sha256: str)` and exact codec and durability functions `encode_period_journal(record: PeriodJournalRecord) -> bytes`, `decode_period_journal(data: bytes) -> PeriodJournalRecord`, `publish_period_pending(fs: SafeRootFS, record: PeriodJournalRecord) -> None`, `publish_period_complete(fs: SafeRootFS, record: PeriodJournalRecord) -> None`, and `remove_period_pending(fs: SafeRootFS) -> None`.
- Produces fixed constants `SAMPLE_PERIOD_PENDING` and `SAMPLE_PERIOD_COMPLETE` for the two `/var/lib/appio` journal paths. Journal records are root-owned, single-link regular files, mode `0600`, at most 1 KiB, canonical ASCII JSON plus one LF, and use exactly ordered keys `version`, `operation_id`, `target`, `sample_seconds`, and `target_sha256`.
- Produces `SAMPLE_SHOW_ARGV = ("/usr/bin/systemctl", "show", "appio-iod.service", "--property=Environment,EnvironmentFiles,UnsetEnvironment,DropInPaths,UnitFileState,ActiveState", "--no-pager")`.
- Preserves `SHOW_ARGV = ("/usr/bin/systemctl", "show", "appio-iod.service", "--property=UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus", "--no-pager")` for lifecycle operations.
- Produces a seven-entry literal `VERB_HANDLERS` and `dispatch(invocation: Invocation, *, deps: SetupDeps) -> dict` with complete period-payload revalidation before the helper lock or any operation.
- Preserves `main(argv: list[str] | None = None, *, deps: SetupDeps | None = None) -> int`.
- Produces `_valid_syntax(argv: list[str]) -> str | None`, where `None` means valid syntax and a returned string is the exact exit-2 reason.
- Produces `_pending(identity: SetupIdentity, fs: SafeRootFS, *, create_parents: bool = True) -> bool | None` and `_observe(result: dict, *, deps: SetupDeps, fs: SafeRootFS, identity: SetupIdentity, strict: bool = False, create_parents: bool = True) -> dict[str, str] | None`, preserving creating observation for legacy verbs and using `create_parents=False` for period-handler exception observation.
- Produces the complete result document with keys `version`, `verb`, `uid`, `enabled`, `answering`, `pending_activation`, `state`, `export_path`, `export_cutoff`, `error`, `sample_period`, and `sample_period_state` in every encoded result.
- Produces period outcomes only from `None`, `applied`, `applied_not_running`, `repair`, `unchanged`, and `service_not_running`, while retaining the existing service-state meaning in `state`.

### Step 1: Add boundary, shim, and client contract tests

- [ ] Update the existing six-verb and fixed-target equality assertions for the seventh verb, the sample drop-in, and separately identified fixed journal state targets. Update `tests/p3/test_setup_boundary.py::test_verb_set_is_exactly_seven_design_verbs` and `test_every_design_verb_is_accepted`. Keep the six legacy two-element vectors separate from the period three-element vector.
- [ ] Add `test_period_argv_forms_are_checked_before_account_lookup`, `test_period_payload_range_table`, `test_dispatch_preserves_period_payload`, and `test_forged_period_invocation_without_payload_is_rejected`.
- [ ] Verify the shim table with canonical shape `(?:0|[1-9][0-9]{0,7})` using ASCII full-match semantics. Cover `2` and `30`, out-of-range `0`, `1`, `31`, and `12345678`, and shim-only rejection of `abc`, `02`, ` 5`, empty, signed, decimal, and nine-digit values.
- [ ] Update `tests/p3/test_setup_client.py::test_existing_verbs_keep_legacy_vectors_and_period_verb_carries_payload` and add `test_period_payload_pairing_refuses_before_launch` and `test_decode_body_validates_period_result_fields`.

### Step 2: Run the red boundary vector

- [ ] Run `python -m pytest -q tests/p3/test_setup_boundary.py tests/p3/test_setup_client.py`.
- [ ] Confirm failures show the absent seventh verb, period payload, exact argv split, and result-field validation. Ensure invalid period payload tests prove the launcher is not called.

### Step 3: Implement the boundary and fixed paths

- [ ] Add `set-sample-period` to `VERBS`, add the `period_seconds` field to `Invocation`, and add a `FixedTarget` with key `sample-period-dropin` for `/etc/systemd/system/appio-iod.service.d/20-sample-period.conf`.
- [ ] Add fixed journal targets for `/var/lib/appio/sample-period.pending.json` and `/var/lib/appio/sample-period.complete.json` to the boundary allowlist used by safe filesystem operations, while retaining the separate configuration target collection and exact delegation state file shape.
- [ ] Make `parse_invocation` validate executable and argv shape before euid, `PKEXEC_UID`, NSS, or filesystem lookup. Accept only the six old two-element forms or the period three-element form. Convert the canonical period to an integer and raise `InvocationError(2, "sample_period_out_of_range")` for values outside 2 through 30. Use `InvocationError(2, "invalid_verb_or_argument_count")` for wrong arity, wrong pairing, noncanonical payload, or invalid executable.
- [ ] Preserve all existing identity, runtime trust, and error codes. Keep the shim stdlib-only and in the order syntax, root euid, `PKEXEC_UID`, trusted import closure, import, and dispatch. Update its exact usage text to `usage: appio-setup <install-delegation|enable|disable|status|restart-user-manager|export-history|set-sample-period <seconds>>`.

### Step 4: Implement the client payload pairing

- [ ] Change `SetupClient.invoke` to the exact keyword-only payload signature. Reject a payload on any non-period verb, reject a missing payload on `set-sample-period`, and reject every non-exact integer or value outside 2 through 30 before launch.
- [ ] Preserve `HELPER_ARGV = ("/usr/bin/pkexec", "/usr/bin/appio-setup")`. Build exactly `HELPER_ARGV + (verb,)` for six legacy verbs and exactly `HELPER_ARGV + ("set-sample-period", str(period_seconds))` for the period verb.
- [ ] Extend `decode_body` to require the existing result fields and validate `sample_period` as `None` or an exact integer in 2 through 30, and `sample_period_state` as `None` or one of `applied`, `applied_not_running`, `repair`, `unchanged`, and `service_not_running`. A malformed field invalidates the entire body and retains exit status and bounded stderr.


### Step 5: Add canonical payload, traversal, durability, and journal tests

- [ ] Add `test_sample_period_payload_and_parser_round_trip` for 2, 10, and 30, including exact bytes and a single final LF.
- [ ] Add `test_sample_period_target_is_in_both_allowlists`, `test_missing_config_target_and_missing_parser_have_distinct_refusals`, `test_foreign_sample_dropin_preserves_bytes`, and `test_empty_sample_dropin_is_replaced`. Cover missing or doubled LF, noncanonical numbers, wrong sections or keys, extra lines, trailing spaces, non-UTF-8 bytes, symlink, hard link, ownership, mode, and size refusals.
- [ ] Add `test_read_bounded_without_creating_missing_parents` and `test_period_ancestor_directory_sync_repeats_for_existing_parent`. The latter must record actual containing-directory descriptors or equivalent injected events when period directories already exist and on retry.
- [ ] Add `test_period_journal_targets_are_fixed_state_only`, `test_period_journal_pending_and_complete_codec_is_exact`, `test_period_journal_atomic_publish_syncs_parent`, and `test_period_journal_remove_syncs_parent`.
- [ ] Update setup-state tests to cover `load_setup_state(..., create_parents=False)` and prove missing state parents remain absent. Preserve all existing creating behavior for delegation installation.

### Step 6: Run the red filesystem vector

- [ ] Run `python -m pytest -q tests/p3/test_setup_files.py tests/p3/test_setup_delegation.py`.
- [ ] Confirm the new tests fail on the absent period parser, missing non-creating flag, missing journal codecs, and missing repeatable directory sync behavior.

### Step 7: Implement canonical period content and target policy

- [ ] Implement `sample_period_payload` with exact validation `ValueError("sample period must be an integer from 2 through 30")` for bool, non-integer, and out-of-range values. Emit exactly `[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=<seconds>\n` as UTF-8.
- [ ] Make `parse_sample_period_dropin` accept only those canonical bytes for 2 through 30. Return `None` for every other byte sequence, including empty data, non-UTF-8 data, missing or multiple final LF, noncanonical period text, altered section, altered key, extra line, or trailing space.
- [ ] Add the sample target to `FIXED_CONFIG_TARGETS` and `_require_expected_content`. Preserve the distinction between `target:not_fixed_path` and `unexpected_content:not_a_config_target`. Empty target bytes remain replaceable. Every other non-empty noncanonical sequence is foreign and must remain byte-identical.

### Step 8: Implement non-creating safe traversal and directory durability

- [ ] Add `create_parents` to `SafeRootFS.read_bounded` with default `True`. When false, walk only existing root-owned, non-writable directories through `O_NOFOLLOW` descriptors, never call `mkdir`, and propagate a missing parent or leaf as `FileNotFoundError`. Preserve all trust and bounded-read refusals.
- [ ] Ensure period directory creation uses the existing pinned traversal, fixed modes, and immediate containing-directory fsync. After the initial valid sample configuration view, support creation of the target and journal parents only at the state-machine’s directory-preparation step.
- [ ] Add repeatable ancestor and leaf directory durability operations. Every eligible period invocation must fsync `/etc/systemd/system` and `/var/lib`, then the leaf directories `/etc/systemd/system/appio-iod.service.d` and `/var/lib/appio`, even when they already existed. Publication and removal must fsync the affected leaf again. Retries repeat the containing-directory fsyncs instead of assuming a visible child proves durability.
- [ ] Ensure a failure before rename leaves existing target bytes unchanged and never truncates a fixed target.

### Step 9: Implement strict transaction journal operations

- [ ] Validate journal records as version 1, exact lowercase 32-hex operation IDs, the one canonical target path, integer seconds 2 through 30, and lowercase 64-hex SHA-256. Reject extra or missing keys, duplicate JSON keys, non-ASCII or noncanonical JSON, wrong LF shape, oversized bytes, unsafe metadata, and link counts other than one.
- [ ] Make pending and completed publication use the same safe atomic write and parent-directory durability boundary, with mode `0600` and root ownership. Keep completion evidence separate from the exact `{version, pending_by_uid}` delegation state.
- [ ] Publish completion before pending removal. A failed completion write or fsync leaves pending evidence and cannot be reported as success. Pending removal or its fsync failure cannot revoke already durable completion, but later invocations must recheck pending absence and classify visible pending recovery correctly.

### Step 10: Preserve the delegation exception-observation contract

- [ ] Change `load_setup_state` to accept the keyword-only `create_parents` flag and forward it to `SafeRootFS.read_bounded` while preserving its default.
- [ ] Ensure the period handler can observe setup state after a failure without creating `/var/lib/appio` or its parents. Keep all non-period helper verbs on their existing creating path.


### Step 11: Add exact service and dispatch tests

- [ ] Update `tests/p3/test_setup_service.py::test_literal_dispatch_table_has_exactly_seven_verbs`.
- [ ] Add `test_set_sample_period_inactive_writes_and_never_starts`, `test_set_sample_period_active_uses_try_restart`, and `test_main_preserves_sample_period_out_of_range_reason_and_json`.
- [ ] Add `test_active_identical_successful_apply_is_unchanged`, `test_inactive_identical_successful_apply_is_unchanged`, and `test_inactive_canonical_retry_with_only_reload_is_repair`.
- [ ] Add the recovery and durability tests `test_identical_apply_recovers_after_target_directory_sync_failure`, `test_identical_apply_recovers_after_reload_failure`, `test_identical_apply_recovers_after_restart_failure`, `test_identical_apply_recovers_after_completed_record_directory_sync_failure`, `test_apply_after_ancestor_directory_sync_failure_reports_applied`, `test_identical_apply_recovers_after_ancestor_directory_sync_failure`, `test_failed_restart_inactive_retry_repairs_without_start`, `test_inactive_unrelated_failed_result_does_not_block_apply`, and `test_inactive_failed_result_does_not_mask_period_directory_sync_failure`.
- [ ] Add the observation and precedence tests `test_final_lifecycle_activation_race_requires_fresh_sample_probe`, `test_pending_unlink_sync_failure_rechecks_absence_before_unchanged`, `test_missing_completion_evidence_replays_manager_operations`, `test_active_before_inactive_after_has_service_not_running_precedence`, `test_final_lifecycle_read_failure_cannot_return_success`, and `test_sample_show_malformed_fields_fail_closed`.
- [ ] Add `test_inactive_environment_sources_are_unverifiable` for the inactive environment-file and unset-variable branches.
- [ ] Add `test_malformed_initial_show_does_not_create_missing_preflight_directories`, `test_sample_show_environment_files_multiple_entries_active_and_inactive`, `test_sample_show_omitted_empty_environment_files_is_empty`, `test_sample_show_omitted_empty_unset_environment_is_empty`, `test_active_override_reports_value_and_dropin_paths`, and `test_inactive_environment_override_is_unverifiable_with_value_and_dropin_paths`. Cover the inactive file and unset branches as specified.

### Step 12: Run the red service vector

- [ ] Run `python -m pytest -q tests/p3/test_setup_service.py`.
- [ ] Confirm failures identify the missing result fields, seventh handler, period query, payload revalidation, state transitions, journal recovery, final lifecycle observation, and exact diagnostics.

### Step 13: Extend result construction and syntax preservation

- [ ] Add `sample_period=None` and `sample_period_state=None` to `_empty` and every result path. Update legacy result fixtures and assertions to expect both null fields while preserving the existing six verbs. Update `VERB_HANDLERS` and `VERBS` together so `setup_service` remains importable at the task commit. For a period invocation that reaches dispatch, retain the requested integer even when the operation later fails. For a boundary range error before identity, emit `uid: null`, `sample_period: null`, and `sample_period_state: null`.
- [ ] Make dispatch reconstruct the exact two- or three-element argv before calling `parse_invocation`. Reject a forged period `Invocation` without a payload and a forged payload on another verb with `InvocationError(2, "invalid_verb_or_argument_count")`, before the common lock or any operation.
- [ ] Change syntax validation so `_valid_syntax` preserves the actual exit-2 reason. `main` must print `sample_period_out_of_range` for a canonical out-of-range period rather than replacing it with the generic reason, while preserving the existing shim usage behavior for shape failures.
- [ ] Keep the compact ASCII-safe result encoder and bounded `result:too_large` fallback. Preserve the beneficiary UID only after identity resolution and never infer it from argv or an arbitrary environment variable.

### Step 14: Keep the three show-property contracts independent

- [ ] Retain the lifecycle `SHOW_ARGV` and its exact six fields for existing enable, disable, status, restart, export, and exception observation.
- [ ] Add the exact `SAMPLE_SHOW_ARGV` and a bounded line parser for `Environment`, repeated ordered `EnvironmentFiles`, `UnsetEnvironment`, `DropInPaths`, `UnitFileState`, and `ActiveState`. Require exactly one line for `Environment`, `DropInPaths`, `UnitFileState`, and `ActiveState`. Permit omitted `EnvironmentFiles` and `UnsetEnvironment` as empty values, permit repeated `EnvironmentFiles` lines in output order, and reject blank, unknown, duplicate single-valued, missing, or over-limit output with exit 1 and exact reason `sample_period_show_invalid`.
- [ ] Keep `parse_unit_facts` and the GUI’s `UNIT_SHOW_ARGV` contract in Task 3. Do not reuse either parser for the period configuration response.

### Step 15: Implement D7 steps 1 through 6

- [ ] Under the common helper lock and bounded runner, read the lifecycle view first and retain `active_state_before`, `result_before`, and `active_before`.
- [ ] Read the target and both journal files with `create_parents=False`. Treat only missing leaf or parent as absent. Preserve every other trust or I/O error. Reject non-empty noncanonical target bytes as exit 2 with `foreign_drop_in` and exact one-line diagnostic `foreign_drop_in path=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf`.
- [ ] Compute canonical bytes, `needs_write`, and the exact SHA-256 digest. Read and validate pending and completed records without publishing or replacing either during preflight. Set `completed_matches_request` only when the completed record matches the requested target, seconds, and digest. Set `completed_recovery_candidate` only when pending and completed records have identical operation ID, target, seconds, and digest and those values match this request. A pending record without a completed record carrying the same operation ID is incomplete even when the target bytes and observations match. Any valid pending/completed mismatch or malformed journal sets `journal_reconciliation_required`. Reuse an incomplete pending operation ID only for the same target, seconds, and digest. A completed-recovery candidate is usable only when target bytes, merged configuration, active daemon value when active, and the initial lifecycle view match this request; otherwise allocate a fresh pending ID and replay the required operations.
- [ ] Read the initial configuration view before creating missing target or journal parents. Parse the `Environment` property without shell expansion and consider the sample setting configured only when exactly one canonical `APPIO_SAMPLE_SECONDS=<n>` token is present. Treat an absent, duplicate, malformed, noncanonical, or different sample token as a different configured value. Retain ordered `EnvironmentFiles`, raw `UnsetEnvironment`, `DropInPaths`, `UnitFileState`, and `ActiveState`; read daemon status separately only while active and accept its running period only when `sample_ns` is a positive exact multiple of `NS`. Set `manager_operation_required` when the target needs writing, merged configuration differs, the active daemon value differs or is unknown, no matching completed record exists, journal reconciliation is required, or a completed-recovery candidate fails its fresh-observation checks. A completed record alone permits the clean no-op candidate only when pending is absent.
- [ ] A malformed initial configuration view records `sample_period_show_invalid`, skips directory creation, journal publication, target write, reload, restart, and verification, and proceeds to the required final lifecycle observation. The subsequent dispatch exception observer uses the non-creating setup-state read path.
- [ ] After a valid initial view, create missing parents through safe traversal, run the current-invocation ancestor and leaf directory fsyncs, and fail with `sample_period_state_write_failed` if any required sync fails. If there is no pending record, a matching completed record exists, no manager operation is required, and no journal reconciliation is needed, leave both records untouched for the `unchanged` path. If a matching pending/completed recovery candidate and all initial observations match, fsync the state parent, retain its operation ID, and skip manager operations so final checks can classify it as `repair`. In every other case, publish a matching pending record and fsync `/var/lib/appio` before any target write, reload, or restart. Mark the transaction incomplete whenever matching completion evidence is absent or journal reconciliation is required, so manager operations cannot be skipped.

### Step 16: Implement D7 manager operations and verification

- [ ] Run `daemon-reload` when the target needs writing, the merged configured value differs, or the transaction is incomplete. On a nonzero result, retain `daemon_reload_failed`, skip `try-restart` and the step-9 configuration verification, then continue to the final lifecycle observation without success.
- [ ] Run `try-restart appio-iod.service` only when `active_before` is true and the requested target, merged value, running value, or incomplete transaction requires it. Never use it for an inactive unit. Retain `service_restart_failed` on a nonzero result and continue through final observation.
- [ ] Re-read and validate the configuration. For active verification, require the merged value and a fresh daemon status sample to equal `n * NS`. Return exit 1 with reason `sample_period_overridden` and exact diagnostic `sample_period_not_in_effect observed=<value> DropInPaths=<paths>`. Format `<value>` as canonical merged seconds or observed daemon seconds when available, and `unknown` when absent, duplicate, malformed, noncanonical, non-integral in nanoseconds, or unavailable. Format empty `DropInPaths` as `(none)`.
- [ ] For inactive verification, reject merged mismatches, any environment-file entries, or `UnsetEnvironment` naming `APPIO_SAMPLE_SECONDS`. Return exit 1 with reason `sample_period_unverifiable` and exact diagnostic `sample_period_unverifiable observed=<value> EnvironmentFiles=<files> UnsetEnvironment=<unset> DropInPaths=<paths>`. Format the merged value as canonical seconds or `unknown`, join retained environment-file entries in order with ` | ` or use `(none)`, and format empty unset and drop-in fields as `(none)`. Keep diagnostics to one line.
- [ ] For a zero-exit but malformed post-operation `SAMPLE_SHOW_ARGV` response, retain `sample_period_show_invalid`, skip success, and continue to the final lifecycle observation. Do not perform a second period show after a failed daemon-reload.
- [ ] Perform lifecycle observations in the specified order. After operation and configuration verification, read lifecycle. If active, obtain a fresh bounded status reply after that lifecycle read and require `sample_ns == n * NS`. Then read lifecycle once more to establish final `ActiveState` and `Result`. If that final read is active, require a matching status observation obtained after the preceding active lifecycle read. Do not reuse a pre-operation or step-9 probe to satisfy this final check. This fresh post-lifecycle runtime probe is required for every active `applied`, `repair`, and `unchanged` outcome, including the active-final outcome after `try-restart`.
- [ ] Require valid final `ActiveState` and `Result` for every success. A non-success `Result` while active yields `service_lifecycle_failed` after higher-precedence failures. A prior inactive failed `Result` alone never blocks an inactive apply or repair. Continue required lifecycle reads after command or verification failures whenever the runner can obtain them.

### Step 17: Implement exclusive outcome precedence and journal completion

- [ ] Return exit 1 `service_not_running` for an active-before to inactive-final transition. A nonzero `try-restart` with a valid final active lifecycle view returns exit 1 `service_restart_failed`; a failed `daemon-reload` after the first precedence row does not apply returns exit 1 `daemon_reload_failed`. Preserve `sample_period_state=None` for each failure. Apply the remaining exact failure and success precedence from §7, including `service_lifecycle_failed` for an active non-success `Result` when no higher row applies.
- [ ] Return `applied` only for a target write with an active final state, `applied_not_running` only for a target write with inactive before and after states, `repair` for reload, restart, incomplete-journal replay, or completion-evidence recovery without a target rewrite, and `unchanged` only when no pending record, no operation is required, matching durable completion, fresh matching observations, and current directory checks all hold.
- [ ] For a successful transaction, publish matching completed evidence atomically and fsync `/var/lib/appio` before returning success. Preserve pending on commit failure with `sample_period_state_commit_failed`. Remove pending only after completion durability, fsync its parent, and treat a pending-unlink fsync failure as non-revoking of already durable completion. On later invocations, repeat directory checks, reconcile visible pending as `repair`, and permit `unchanged` only after durable pending absence is confirmed.
- [ ] Keep `test_final_lifecycle_activation_race_requires_fresh_sample_probe`, `test_pending_unlink_sync_failure_rechecks_absence_before_unchanged`, `test_missing_completion_evidence_replays_manager_operations`, `test_active_before_inactive_after_has_service_not_running_precedence`, and `test_final_lifecycle_read_failure_cannot_return_success` owned by this task. The GUI test `test_sampling_apply_success_waits_for_matching_nanosecond_probe` belongs to Task 3 and is consumed only as the downstream observation contract.
- [ ] Ensure every successful active result has the fresh post-lifecycle status match and every successful inactive result remains truthful about not observing a running period. Ensure no handler path calls `enable`, `start`, or `try-restart` for an inactive unit.

### Step 18: Run the task verification vector

- [ ] Run `python -m pytest -q tests/p3/test_setup_service.py tests/p3/test_setup_files.py tests/p3/test_setup_boundary.py tests/p3/test_setup_delegation.py tests/p3/test_setup_client.py`.
- [ ] Confirm all exact error strings, exit precedence, journal states, command vectors, result fields, and no-start assertions pass through public `main` and `dispatch` entry points.

### Step 19: Commit

- [ ] Run `git add src/appio/setup_boundary.py data/libexec/appio-setup src/appio/setup_client.py tests/p3/test_setup_boundary.py tests/p3/test_setup_client.py src/appio/setup_files.py src/appio/setup_delegation.py tests/p3/test_setup_files.py tests/p3/test_setup_delegation.py src/appio/setup_service.py tests/p3/test_setup_service.py`.
- [ ] Run `git commit -m "feat: implement durable sample period setup"` and report the commit SHA.
## Task 3: Add observed period state to ownership and GUI settings

**Implementer tier:** Capable

**Lane/category**

- Lane `gui-ownership`.
- Depends on Tasks 1 and 2 for the status field, `SetupClient.invoke` payload, and helper result states. It does not change local collector cadence or the single-switch ownership protocol.

**Files**

- Modify `src/appio/collector_owner.py`.
- Modify `src/appio/ui/collector_window.py`.
- Modify `tests/p3/test_collector_window.py`.

**Interfaces**

- Consumes `validate_server_message` status normalization with optional `sample_ns`, `SetupClient.invoke("set-sample-period", period_seconds=n)`, `SetupReply.body`, and existing ticket/generation probe delivery.
- Produces `ServiceFacts.sample_ns: int | None = None` appended after `restarts`. Its invalid-value message is exactly `ValueError("sample_ns must be a positive integer or None")`.
- Produces `SettingsDialog.ROWS == ("system_services", "sampling_period")` and the object names `sample_period_row`, `sample_period_spin`, `sample_period_apply`, and `sample_period_status`.
- Produces `production_probe` behavior that carries a validated `sample_ns` only from an answering status reply and carries `None` for disconnected, invalid, non-answering, or restarted observations. Ticket/generation-mismatched payloads are discarded before they can replace current facts or update the period label.
- Produces `pending_period_seconds` as the GUI requested-period state. A matching answering observation is established only when `reported_sample_ns == pending_period_seconds * NS`.

### Step 1: Add exact ownership and GUI tests

- [ ] Update existing positional `ServiceFacts` fixtures so the appended default preserves all old constructors. Assert `None` and positive integers including values above `30 * NS` are accepted. Add bool, zero, negative, and non-integer rejection cases with the exact message.
- [ ] Add `test_sampling_row_has_required_object_names`, `test_sampling_row_focus_and_spin_contract`, `test_sampling_edit_survives_refresh`, `test_sampling_status_strings_are_exact`, and `test_sampling_apply_button_is_disabled_while_pending`.
- [ ] Add `test_sampling_mismatch_and_superseded_period_are_not_success`, `test_sampling_saved_status_is_shown_when_inactive`, `test_sampling_apply_preserves_single_switch_activation_invariant`, and `test_sampling_apply_success_waits_for_matching_nanosecond_probe`. The last test asserts success only after exact `N * NS` equality.
- [ ] Add `test_sampling_consent_discloses_exact_command`, `test_sampling_consent_cancellation_dispatches_nothing`, and `test_sampling_missing_helper_shows_bounded_stderr`.
- [ ] Extend production-probe tests so answering status values pass `sample_ns`, while missing, invalid, non-answering, disconnected, and restarted observations produce `sample_ns=None`. Assert that stale ticket/generation payloads are discarded without changing the accepted facts or period label.

### Step 2: Run the red GUI vector

- [ ] Run `python -m pytest -q tests/p3/test_collector_window.py tests/p3/test_collector_owner.py`.
- [ ] Confirm failures identify the missing facts field, row, object names, exact focus behavior, pending state, consent path, and effective-value status rendering.

### Step 3: Extend ServiceFacts and the production probe

- [ ] Append `sample_ns` after `restarts` with a `None` default so existing positional constructors remain valid. Accept `None` or a positive integer excluding bool and raise the exact specified `ValueError` otherwise.
- [ ] In `production_probe`, pass the status `sample_ns` through only when the status reply says `answering is True` and validation has accepted the field. Return `None` when status is unavailable, invalid, non-answering, or the probe was restarted. Keep `UNIT_SHOW_ARGV`, `parse_unit_facts`, and systemd fact mapping unchanged. At the existing ticket/generation acceptance point, discard mismatched payloads before they can replace current facts or update the period label.
- [ ] Preserve `ProbePayload` as the immutable carrier of the complete `ServiceFacts` object. Keep the existing ticket and setup-generation checks as the only acceptance gate for asynchronous facts, dropping generation-mismatched payloads before they can update the period status.

### Step 4: Build the sampling-period row

- [ ] Change `SettingsDialog.ROWS` exactly to `("system_services", "sampling_period")` and preserve the existing switch row.
- [ ] Add the peer row with the exact visible form `Sampling period (system service)  [ 10 ] s  (2-30)  [Apply]`, a `QSpinBox`, and the status line beneath it. Include the exact hint `Shorter periods increase raw history writes and CPU use.`
- [ ] Set the exact object names. Configure `setRange(2, 30)`, `setSuffix(" s")`, and `setKeyboardTracking(False)`. Make `focus_row("sampling_period")` focus `sample_period_spin` and preserve `ValueError("unknown settings row")` for unknown keys.
- [ ] Initialize the spin box to the last valid observed `sample_ns // NS` only for an exact positive multiple in 2 through 30, otherwise 10. Refresh the spin box only when there is no edited-but-unapplied value and no pending operation. Render the status line only from observed facts or verified helper state, independently of the requested spin value.

### Step 5: Add consent, apply, and pending lifecycle behavior

- [ ] Add the `Apply sampling period` consent caption. Include the exact command line `  /usr/bin/pkexec /usr/bin/appio-setup set-sample-period N`, state that active service application restarts the service, and state that an inactive service is not started.
- [ ] Store `pending_period_seconds` in seconds only after consent is accepted. Call `SetupClient.invoke("set-sample-period", period_seconds=n)` only from the accepted path. Cancellation must dispatch nothing. Keep the helper’s bounded first stderr line as the launch or authentication failure message.
- [ ] Disable Apply while any helper operation is pending, while leaving it enabled when the daemon is not answering. Do not let the period operation alter the existing switch’s activation controls or start an inactive collector.
- [ ] On each fresh answering probe, compare nanoseconds exactly. A match clears the pending request and displays `Sampling every N s.`. A missing period displays `Sampling period not reported by this collector.`. A non-answering collector displays `Sampling period not known while the collector is not answering.`. A mismatch displays `Requested N s, collector reports M s.` and remains pending. Every observation other than an exact answering match leaves the request pending.
- [ ] For successful inactive `applied_not_running` and inactive `repair`, display `Saved: sampling every N s once the system service starts.` and retain the pending request until a later answering probe reports the exact requested nanoseconds. Never label the saved value as collector-reported. A failed helper result never establishes saved status.
- [ ] Preserve existing switch rendering, quiescence, local lease transitions, history refresh, and `tick_local` behavior.

### Step 6: Run the task verification vector

- [ ] Run `python -m pytest -q tests/p3/test_collector_window.py tests/p3/test_collector_owner.py tests/p3/test_setup_client.py`.
- [ ] Exercise the normal settings navigation entry point, accepted consent path, cancelled consent path, stale probe path, inactive saved path, and active mismatch path through the Qt test seams.

### Step 7: Commit

- [ ] Run `git add src/appio/collector_owner.py src/appio/ui/collector_window.py tests/p3/test_collector_window.py`.
- [ ] Run `git commit -m "feat: expose sample period in settings"` and report the commit SHA.

## Task 4: Add isolated host acceptance phases and documentation

**Implementer tier:** Capable

**Lane/category**

- Lane `acceptance-and-documentation`.
- Depends on Tasks 1 through 3. This task updates only the acceptance harness, its dry-run tests, and the acceptance runbook.

**Files**

- Modify `tools/acceptance/appio-p3-root.py`.
- Modify `docs/acceptance/appio-p3-root.md`.
- Modify `tests/p3/test_p3_acceptance.py`.

**Interfaces**

- Consumes the seven helper verbs, the result fields `sample_period` and `sample_period_state`, the daemon status `sample_ns`, and the existing `export-history` user-owned SQLite copies.
- Produces `helper(verb, *command_args, observe=None)` with keyword-only `observe`. Positional arguments append after the verb in the fixed pkexec argv. Only `set-sample-period` accepts exactly one canonical seconds argument from 2 through 30. The other six verbs reject every command argument.
- Produces the CLI phases `period-runtime` and `period-capacity` in `PHASES` with side-effect-free dry runs.
- Produces evidence under the existing 0600 user-owned acceptance evidence file. Report each phase's actual execution status. `period-runtime` uses a disposable systemd login and `period-capacity` uses a disposable SQLite fixture.

### Step 1: Add dry-run and helper-vector tests

- [ ] Add `test_period_runtime_phase_is_available_in_side_effect_free_dry_run` and `test_period_capacity_phase_is_available_in_side_effect_free_dry_run`.
- [ ] Add `test_helper_period_argument_is_not_observation_callback`. Assert exact argv for legacy calls, `helper("set-sample-period", "2")`, and keyword observer calls. Assert period payload and observer callback cannot be confused.
- [ ] Prove dry runs import no `appio` module, read no host state, run no command, and write no evidence file. Keep the restart phase’s separate `--allow-session-termination` gate unchanged.

### Step 2: Run the red acceptance vector

- [ ] Run `python -m pytest -q tests/p3/test_p3_acceptance.py`.
- [ ] Run `python tools/acceptance/appio-p3-root.py --phase period-runtime` and `python tools/acceptance/appio-p3-root.py --phase period-capacity` without `--run`. Confirm both print a non-mutating dry-run plan and do not write evidence.

### Step 3: Extend the helper and CLI phases

- [ ] Update the helper to the exact `helper(verb, *command_args, observe=None)` shape. Validate all seven verbs. Append only validated positional command arguments after the fixed helper vector. Preserve bounded waits, observer polling, deadline cleanup, and the existing JSON identity and error checks.
- [ ] Add `period-runtime` after `verify` and before `disable`. Name its executable assertions `test_two_second_and_thirty_second_runs_match_status_and_intervals`, `test_thirty_second_ten_minute_run_has_no_overdue_paths`, `test_apply_two_and_thirty_seconds_restarts_only_active_service`, `test_active_period_apply_records_restart_gap`, and `test_identical_period_reapply_is_unchanged`. The phase must run on the disposable systemd login, apply 2 seconds then 30 seconds, verify active state, changed `InvocationID`, returned `sample_ns`, and committed interval behavior from user-owned `export-history` copies.
- [ ] For each active apply, capture baseline and final user-owned exports. Require either a new `gap` row with `gap_id` greater than the baseline maximum and matching `boot_id`, `source='iod'`, and `reason='collector_gap'`, or a documented partial final interval for the new `iod_epoch` whose interval starts before the restart boundary and whose observed duration is truncated at the new epoch start.
- [ ] After successful 30-second apply, record target `(st_ino, st_mtime_ns)` and service `InvocationID`, reapply 30 seconds, require exit 0 with `sample_period_state == "unchanged"`, and require both values unchanged.
- [ ] Keep the ten-minute monotonic run at 30 seconds. Require at least 18 distinct committed `source='iod'` endpoints ordered by `(epoch, seq)`, boundary proximity, cadence within `max(500 ms, 10% of 30 seconds)`, and every elapsed interval strictly below `flush_ns_for(30 * NS)` equal to 90 seconds. Require unchanged `boot_id`, `iod_epoch`, and service `InvocationID` during this measurement.
- [ ] Add unconditional restoration of 10 seconds. Even after a runtime assertion fails, call `helper("set-sample-period", "10")`, verify `sample_ns == 10 * NS`, record cleanup evidence, and fail the phase if restoration fails. Do not read `/var/lib/appio`, the installed database, or the system journal.
- [ ] Add `period-capacity` using a disposable materialized SQLite fixture outside `/var/lib/appio`. Assert `maintenance_ns_for(2 * NS) == 15 * NS`. Use 226 application IDs, at least 57,968 aged eligible core units (retention age at least two days), a 2-second period, 2,048-unit prune batches, and this exact 30-minute saturation reserve: `2048 + ceil(1800 / 15) * (2048 - 226 * floor(15 / 2)) = 57,968`. Generate one eligible core unit per app every 2 seconds and invoke the production pruning path at each 15-second maintenance deadline. Before every prune call, assert and record at least 2,048 eligible units, and require each batch to retire exactly 2,048 units. Measure at least 1,800 seconds monotonically, recording app count, initial eligible count, generated/retired units, minimum pre-prune count, elapsed time, final eligible count, and every prune result. Require every batch saturated, retirement rate above the fixture production rate of 113 units/second, and no final backlog greater than the initial backlog. This is a disposable capacity fixture and does not compare physical database size or `APPIO_QUOTA_BYTES`.

### Step 4: Update the acceptance documentation

- [ ] Preserve `Current host evidence: UNRUN.` while neither host phase has produced evidence, and update the report truthfully if a phase is subsequently run.
- [ ] Document the phase order `enable -> verify -> period-runtime -> disable`, the roughly thirteen-minute runtime phase, active restart and restart-gap evidence, identical reapply, 10-second cleanup, user-owned export-copy inspection, and no system-journal or installed-database access.
- [ ] Document `period-capacity`, its disposable fixture, 30-minute minimum, 226-app and 57,968-unit reserve, 2,048-unit batches, evidence fields, and dry-run purity.
- [ ] Update all helper-verb references to seven and document the one canonical seconds argument for `set-sample-period`. Preserve the warning that the restart-user-manager phase is separate and terminates the session only with both explicit flags.

### Step 5: Run the task verification vector

- [ ] Run `python -m pytest -q tests/p3/test_p3_acceptance.py`.
- [ ] Run both dry-run commands again and inspect the evidence path mtime and contents to confirm no evidence mutation.

### Step 6: Run the integrated rootless verification vector

- [ ] Run `python -m pytest -q tests/p3/test_iod_cadence_pins.py tests/p3/test_setup_boundary.py tests/p3/test_setup_files.py tests/p3/test_setup_delegation.py tests/p3/test_setup_service.py tests/p3/test_setup_client.py tests/p3/test_collector_owner.py tests/p3/test_collector_window.py tests/p3/test_p3_acceptance.py`.
- [ ] Run the repository’s required full test command from `pyproject.toml` as `python -m pytest -q`.
- [ ] Confirm `data/systemd/appio-iod.service` remains unchanged, and report the actual status of each host acceptance phase and any `reboot-required.json` marker.

### Step 7: Run applicable acceptance phases

- [ ] Run `python tools/acceptance/appio-p3-root.py --run --phase period-capacity` with its disposable materialized SQLite fixture. Observe the full 30-minute measurement, retain its evidence, and report the actual result.
- [ ] Run `period-runtime` on the disposable systemd login specified by the acceptance contract when that environment is available. Record the actual result and preserve `UNRUN` for any phase that cannot be executed.

### Step 8: Commit

- [ ] Run `git add tools/acceptance/appio-p3-root.py docs/acceptance/appio-p3-root.md tests/p3/test_p3_acceptance.py`.
- [ ] Run `git commit -m "test: add sample period acceptance phases"` and report the commit SHA.
