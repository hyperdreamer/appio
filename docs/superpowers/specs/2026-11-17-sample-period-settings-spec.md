# Adjustable sample period implementation specification

- **Design:** `docs/superpowers/specs/2026-11-17-sample-period-settings-design.md`
- **Design commit:** `323eb8d612b45b5efa9dbca925cf6a5844693f8c`
- **Design blob:** `05b7193b...` (SHA-256 `93b0cf6c31355725fe0d8ebbd071cd833e7d74cb5e256078abba6f7f1b6208c8`)
- **Status:** implementation contract for the approved D1-D13 design

## 1. Scope and invariants

Implement one integer setting for the **system service**. The setting is
`APPIO_SAMPLE_SECONDS`, accepts canonical decimal seconds from 2 through 30, and
defaults to 10 seconds. The collector converts seconds to nanoseconds once in the
module-level production entry point. The GUI-owned local collector and
`appio --interval` keep their current behavior.

D1-D13 are closed design decisions. The implementation must not add a second
setting, change the read-only IPC request vocabulary, change retention or rollup
formats, or make the helper start an inactive service. All privileged operations
remain fixed argv operations under the existing trust, lock, timeout, and atomic
write boundaries.

## 2. Change inventory

| File | Symbols and exact responsibility |
|---|---|
| `src/appio/iod.py` | Add `PERIOD_TEXT`, `parse_sample_seconds`, `configured_sample_ns`, `flush_ns_for`, and `maintenance_ns_for`; add `maintenance_ns` to `Daemon.__init__`; use the instance maintenance interval in `start`, `step`, sampling recovery, and `stop`; add `sample_ns` to `_status`; resolve the three values in `main`. |
| `src/appio/ipc_models.py` | Extend `_status` with optional `sample_ns`; retain the eight existing required status keys and reject invalid `sample_ns` types, signs, or values above 30 seconds. |
| `src/appio/setup_boundary.py` | Add `set-sample-period` to `VERBS`; add `period_seconds` to `Invocation`; extend `parse_invocation` and its range/shape errors; update prior exact-verb and accepted-verb assertions for seven verbs, keeping the six legacy two-element forms separate from the period three-element payload form; add the sample-period `FixedTarget` and the two fixed helper journal targets. |
| `src/appio/setup_files.py` | Add `SAMPLE_PERIOD_DROPIN`, its payload/parser pair, target membership, expected-content branch, and parent-directory durability after directory creation and on retries when the directory already exists; add a non-creating traversal option to `SafeRootFS.read_bounded` for period preflight reads; add strict pending/completed journal codecs and durable publish/remove operations, including a repeatable parent-directory durability check after pending unlink, separate from `FIXED_CONFIG_TARGETS`. |
| `src/appio/setup_service.py` | Add the two result fields, retain the existing lifecycle `SHOW_ARGV`, add the sample configuration show path and set-period handler, preserve payloads during dispatch revalidation, preserve syntax reasons, and implement the D7 state machine with non-creating target/journal preflight reads, bounded multi-entry `EnvironmentFiles` aggregation, deferred journal publication until the initial configuration view is valid, non-creating setup-state observation after period-handler failures, retryable ancestor-directory durability confirmation, a fresh post-lifecycle runtime probe for active outcomes, transaction-scoped retry repair despite an older inactive systemd `Result`, journal recovery, and completion commit. |
| `src/appio/setup_delegation.py` | Add an optional non-creating mode to `load_setup_state` and pass it to `SafeRootFS.read_bounded`; preserve the creating default for existing callers, while period-handler exception observation uses the non-creating mode. |
| `src/appio/setup_client.py` | Add the period argument to `SetupClient.invoke`, validate verb/payload pairing, append a third argv element only for the period verb, update existing launch-vector tests so six legacy verbs retain their vectors and the period form is explicit, and validate the two new result fields in `decode_body`. |
| `src/appio/collector_owner.py` | Add and validate `ServiceFacts.sample_ns`. Existing positional constructors remain valid because the field is appended with a `None` default. |
| `src/appio/ui/collector_window.py` | Add the sampling-period Settings row, consent text, pending/effective state handling, object names, status strings, status-to-`ServiceFacts` pass-through, and saved-state handling for successful inactive `applied_not_running` and `repair` results. Keep `UNIT_SHOW_ARGV` and `parse_unit_facts` as the GUI service-state parser. |
| `data/libexec/appio-setup` | Add the new verb and the anchored three-argument shape check while retaining the stdlib-only trust order and usage prefix. |
| `data/systemd/appio-iod.service` | Preserve the unit file and its existing `APPIO_QUOTA_BYTES` deployment override. Do not add a unit-level sample environment assignment; the fixed `/etc` drop-in owns the setting. |
| `tools/acceptance/appio-p3-root.py` | Add the executable `period-runtime` phase for the named 2 s/30 s status and committed-interval checks, restart-gap exports, and identical-value reapply; check the ten-minute overdue boundary and active-unit restart; restore 10 s in unconditional cleanup. Define `helper(verb, *command_args, observe=None)` so positional command arguments construct the fixed argv while the keyword-only callback observes a running helper; allow one canonical seconds argument only for `set-sample-period`, and no command arguments for the other six verbs. Retain the isolated 30-minute `period-capacity` phase; both phases have side-effect-free dry runs. |
| `docs/acceptance/appio-p3-root.md` | Keep `Current host evidence: UNRUN.` truthful; document the `period-runtime` command, its place after `verify` and before `disable`, prerequisites, expected thirteen-minute duration, restart-gap and identical-reapply assertions, evidence, export-copy-only inspection, and unconditional restoration of 10 s with reported-value verification; document `period-capacity`, its fixture, 30-minute duration and reserve; update the helper verb count to seven and explain the period verb's seconds argument; preserve dry-run purity. |
| `tests/p3/test_iod_cadence_pins.py` | Add parser, environment, derived-window, status, validator, and production-entry-point tests; add the two-period observed-interval, ten-minute overdue, and five-value `maintenance_ns_for` assertions; preserve the existing default cadence pins. |
| `tests/p3/test_setup_boundary.py` | Update `test_verb_set_is_exactly_seven_design_verbs`; update accepted-verb coverage to test six legacy two-element forms separately from the period form; add period three-element shape, canonical range, payload transfer, and account-lookup ordering tests. |
| `tests/p3/test_setup_files.py` | Add canonical payload/parser, target allowlist, directory-fsync, foreign-content, empty-file, and byte-preservation tests; prove a bounded preflight read does not create missing parent directories; assert ancestor-directory fsync is repeated for existing period directories; add exact pending/completed journal codec, atomic replacement, parent-fsync, and pending-removal recovery tests, including proof that journal paths are fixed state targets but not config targets. |
| `tests/p3/test_setup_service.py` | Update the dispatch-table coverage for seven handlers; add D7 operation, failure, repair, inactive, precedence, multi-entry environment, activation-race, cleanup-recovery, missing-completion, exception-observation, and directory-durability regressions. |
| `tests/p3/test_setup_client.py` | Update existing every-verb vector coverage so six legacy verbs keep their vectors and the period verb carries canonical seconds; add payload-pairing refusals and result-field decode tests. |
| `tests/p3/test_collector_window.py` | Add row construction, object names, focus, range, edit preservation, exact status, apply lifecycle, saved-state assertions for both inactive success outcomes, consent, mismatch, and no-start assertions. |
| `tests/p3/test_p3_acceptance.py` | Update period dry-run coverage and add a helper argv/callback regression; prove dry runs remain side-effect-free. |

## 3. Collector cadence contract

In `src/appio/iod.py`, use these exact signatures:

```python
PERIOD_TEXT: re.Pattern[str] = re.compile(r"(?:[2-9]|[12][0-9]|30)", re.ASCII)
def parse_sample_seconds(raw: object) -> int | None: ...
def configured_sample_ns(environ: Mapping[str, str] | None = None) -> int: ...
def flush_ns_for(sample_ns: int) -> int: ...
def maintenance_ns_for(sample_ns: int) -> int: ...
```

`parse_sample_seconds` returns an integer only when `type(raw) is str` and
`PERIOD_TEXT.fullmatch(raw)` succeeds. It returns `None` for every other object.
It performs no logging and no coercion.

`configured_sample_ns` reads only `APPIO_SAMPLE_SECONDS` from `environ`, or from
`os.environ` when `environ is None`. An absent key and an empty string return
`SAMPLE_NS`. A canonical value returns `parse_sample_seconds(raw) * NS`. Every
other present value returns `SAMPLE_NS` and emits exactly one warning whose message
is the single line `sample_period_invalid value=<escaped>`, with
`<escaped> = repr(raw)[:64]`. The truncation is applied after `repr`; no raw value
is written into the log message. Invalid input never raises and never prevents
startup.

`flush_ns_for` validates `sample_ns` as a positive `int` excluding `bool`, raising
`ValueError("sample_ns must be a positive integer")` otherwise, and returns
`max(30 * NS, 3 * sample_ns)`. `maintenance_ns_for` uses the same validation and
returns `min(60 * NS, max(15 * NS, 3 * sample_ns))`.

The constructor keeps deterministic defaults for embedders and existing cadence
fixtures. Its keyword-only tail is:

```python
sample_ns: int = SAMPLE_NS,
flush_ns: int = MAX_FLUSH_NS,
maintenance_ns: int = MAINTENANCE_NS,
file_source_factory: Callable | None = None,
```

The existing `sample_ns` and `flush_ns` checks remain, including the exact errors
`sample_ns must be a positive integer` and `flush_ns must exceed sample_ns`.
`maintenance_ns` must be a positive integer excluding `bool`, with the exact error
`maintenance_ns must be a positive integer`.

`main()` performs the following before constructing the production daemon:

```python
sample_ns = configured_sample_ns()
flush_ns = flush_ns_for(sample_ns)
maintenance_ns = maintenance_ns_for(sample_ns)
daemon = Daemon(sample_ns=sample_ns, flush_ns=flush_ns,
                maintenance_ns=maintenance_ns)
```

Only `main()` calls `configured_sample_ns`. The default `Daemon` constructor path
therefore remains 10 seconds, 30 seconds, and 60 seconds for tests that inject no
cadence arguments. Every `_next_maintenance` assignment and every maintenance or
recovery retry currently using `MAINTENANCE_NS` uses `self._maintenance_ns` after
this change. The three `_flush_ns` gates continue to use the instance value:
the sampling duration check, the pre-sample recovery check, and shutdown
recovery.

`Daemon._status()` adds `"sample_ns": self._sample_ns` and otherwise preserves its
current eight fields and values. The status value is the constructor value, never a
fresh environment lookup.

## 4. IPC and helper result contracts

The daemon status envelope has these required keys:

```text
protocol, type, request_id, enabled, answering, pending_activation,
ownership_start_mono_ns, cursor
```

`sample_ns` is an optional ninth key for protocol compatibility. When present it
must be an integer excluding `bool`, at least `1`, no greater than `30 * NS`, and
valid under the existing SQLite-integer bound. A missing key is accepted and
normalizes to no period; an explicit JSON `null` is a present value and is
rejected. An unknown key raises `ProtocolError("unknown_key", ...)`; an explicit
null, boolean, float, negative value, zero, or value above `30 * NS` raises
`ProtocolError("invalid_field", ...)`. The daemon always sends the field.

The setup result document keeps all existing keys and adds these exact keys to
`_empty` and every encoded result:

```text
version: int == 1
verb: str
uid: int | None
enabled: bool | None
answering: bool | None
pending_activation: bool | None
state: str
export_path: str | None
export_cutoff: object | None
error: str | None
sample_period: int | None
sample_period_state: str | None
```

The pre-identity syntax/range-error path emits `uid: null`; after identity is resolved, `uid` is the beneficiary's exact integer. The helper never guesses a caller UID from argv or environment.

For `set-sample-period`, `sample_period` is the requested seconds and
`sample_period_state` is `None` on failure or one of
`applied`, `applied_not_running`, `repair`, `unchanged`, or
`service_not_running` on the corresponding D7 outcome. For every other verb both
new fields remain `None`. `state` retains its existing service-state meaning.
`sample_period` is restricted to the integer range 2 through 30; the state field
is restricted to that closed set or `None`. A malformed field makes
`decode_body` return `None` for the whole document.

`sample_ns` is not added to the result document. It is a status-envelope field and
flows through `production_probe` into `ServiceFacts.sample_ns`.

## 5. Drop-in bytes, parser, and fixed paths

Define this exact target and canonical payload:

```python
SAMPLE_PERIOD_DROPIN = Path(
    "/etc/systemd/system/appio-iod.service.d/20-sample-period.conf"
)
def sample_period_payload(seconds: int) -> bytes: ...
def parse_sample_period_dropin(data: bytes) -> int | None: ...
```

For a valid `seconds`, `sample_period_payload(seconds)` returns exactly these UTF-8
bytes, including the final single LF and no preceding or trailing blank line:

```text
[Service]
Environment=APPIO_SAMPLE_SECONDS=<seconds>
```

`seconds` must be an integer excluding `bool` in 2 through 30. Otherwise the
function raises `ValueError("sample period must be an integer from 2 through 30")`.
`parse_sample_period_dropin` returns the integer for exactly the canonical bytes
for 2 through 30. It returns `None` for non-bytes, empty bytes, missing or extra
LF, non-UTF-8 data, a different section, a different key, a noncanonical number,
a number outside 2 through 30, extra lines, or trailing spaces.

Add a `FixedTarget` with key `sample-period-dropin` and the path above to
`setup_boundary.FIXED_TARGETS`. Add the same path to
`setup_files.FIXED_CONFIG_TARGETS` and to `_require_expected_content`. A missing
entry in `FIXED_CONFIG_TARGETS` raises `target:not_fixed_path`; an entry in
`FIXED_CONFIG_TARGETS` without a parser branch raises
`unexpected_content:not_a_config_target`. These are distinct exit-2 refusals.

The sample target uses the existing fixed-target policy: root ownership, mode
0644, one-link regular file, the 64 KiB bound, `O_NOFOLLOW` component traversal,
`mkstemp`, complete write, `fchown`, file `fsync`, rename, and parent-directory
`fsync`. A missing `/etc` parent uses fixed mode `0o755`. Every period-path
`mkdir` runs through the safe pinned traversal and immediately fsyncs its
containing directory; step 6 is the first point allowed to create missing target
or journal parent directories, after the initial configuration view validates.
For every period invocation eligible to select a successful outcome, after the
initial `SAMPLE_SHOW_ARGV` response validates, fsync
`/etc/systemd/system` (the containing directory for the drop-in parent entry)
and `/var/lib` (the containing directory for the `/var/lib/appio`
state-root entry), even when those child directories already exist. Before
relying on existing target or journal entries for `unchanged` or recovered
completion, also fsync the leaf directories
`/etc/systemd/system/appio-iod.service.d` and `/var/lib/appio`. After file
publication or removal, fsync the affected leaf directory again as its file-entry
durability boundary. Repeat the containing-directory fsyncs on every retry, not
only in the `mkdir` path; any required sync failure blocks every successful
outcome. The operation must leave existing target bytes unchanged when validation
or a pre-rename write fails.

The target is ours when it is empty or exactly canonical for one valid period. A
non-empty noncanonical target is foreign and is never replaced.

`SafeRootFS.read_bounded(path, *, max_bytes, create_parents=True)` supports a
non-creating traversal mode. With `create_parents=False`, it opens each existing
parent through the same pinned `O_NOFOLLOW` descriptors and root/mode/ownership
checks, never calls `mkdir`, and propagates `FileNotFoundError` for a missing
parent or leaf. Existing callers retain the creating default. The period handler
uses `create_parents=False` for the target read in step 2 and both journal reads in
step 4; a missing file or parent is treated as absent, while every other trust or
I/O error remains fail-closed. The drop-in and `/var/lib/appio` parents may first
be created by step 6, after step 5 has returned a valid zero-exit configuration
view and before any target write, reload, or restart. When `dispatch` runs its
exception observer for a `set-sample-period` failure, `_observe` must propagate
`create_parents=False` through `_pending` to
`setup_delegation.load_setup_state(fs, *, create_parents=True)`, which passes the
flag to `read_bounded`; a missing setup-state file or parent remains non-creating.
Other helper verbs retain the default creating behavior.

## 6. Invocation and argv validation

`setup_boundary.Invocation` becomes:

```python
@dataclasses.dataclass(frozen=True)
class Invocation:
    verb: str
    identity: SetupIdentity
    period_seconds: int | None = None
```

The six existing verbs remain valid. `set-sample-period` is the seventh verb. The
installed shim, `parse_invocation`, `SetupClient`, `setup_service._valid_syntax`,
and `dispatch` must enforce the same two argv forms:

```text
("/usr/bin/appio-setup", <one of the six existing verbs>)
("/usr/bin/appio-setup", "set-sample-period", <canonical decimal shape>)
```

The third element is accepted only with `set-sample-period`. The shim's shape
pattern is exactly `(?:0|[1-9][0-9]{0,7})`, with `re.ASCII`, full-match semantics.
It checks arity and shape only. Thus `1`, `31`, and `0` pass the shim and reach
the boundary; `abc`, `02`, ` 5`, an empty string, a sign, a decimal point, and a
nine-digit value fail at the shim with exit 2 and the usage message.

`parse_invocation` validates the executable shape before account lookup. For a
three-element sample-period form, a canonical shape is converted to `int`; values
outside 2 through 30 raise `InvocationError(2, "sample_period_out_of_range")`.
A wrong arity, wrong verb/arity pairing, noncanonical payload, or invalid
executable raises `InvocationError(2, "invalid_verb_or_argument_count")`.
The accepted invocation contains the parsed integer in `period_seconds`. Existing
euid, `PKEXEC_UID`, NSS, runtime-trust, and identity errors retain their existing
exit codes and reason strings.

The shim's exact usage text is:

```text
usage: appio-setup <install-delegation|enable|disable|status|restart-user-manager|export-history|set-sample-period <seconds>>
```

The shim prefixes it with `appio-setup: ` and a final LF. The shim retains its
five-step order: syntax, root euid, `PKEXEC_UID`, verified import closure, then
import and dispatch. It remains stdlib-only.

`SetupClient.invoke` becomes:

```python
def invoke(self, verb: str, *, period_seconds: int | None = None) -> Future: ...
```

A non-period verb with a non-`None` payload, or the period verb with a `None`
payload, raises `ValueError` before launch. The payload must be an exact integer
in 2 through 30. The launch vectors are exactly `HELPER_ARGV + (verb,)` for the
six old verbs and `HELPER_ARGV + ("set-sample-period", str(period_seconds))` for
the new one. `HELPER_ARGV` remains
`("/usr/bin/pkexec", "/usr/bin/appio-setup")`.

`setup_service.dispatch` reconstructs the complete two- or three-element vector
for its internal revalidation. A forged `Invocation` with the period verb and
`period_seconds is None`, or a forged payload on another verb, is rejected as
`invalid_verb_or_argument_count` before the lock or any operation. `_valid_syntax`
returns the actual exit-2 reason; `main` prints that reason rather than replacing
`sample_period_out_of_range` with the generic reason.

## 7. D7 helper state machine

The new handler runs under the existing common helper lock and uses the existing
bounded runner. The lifecycle `SHOW_ARGV` remains exactly the six-property query
for existing service operations:

```text
UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus
```

The period handler uses a separate fixed query, named `SAMPLE_SHOW_ARGV`, for the
configuration view:

```text
Environment,EnvironmentFiles,UnsetEnvironment,DropInPaths,UnitFileState,ActiveState
```

Both use `/usr/bin/systemctl`, `appio-iod.service`, `--no-pager`, and the existing
bounded timeouts. The GUI has its own `UNIT_SHOW_ARGV`, whose property set remains
`UnitFileState,ActiveState,SubState,Result,NRestarts`; its `parse_unit_facts`
requires `UnitFileState` and `ActiveState` and permits `SubState`, `Result`, and
`NRestarts` only. No parser may consume the other parser's property contract.

The period helper keeps per-target completion evidence in two new fixed records,
`/var/lib/appio/sample-period.pending.json` and
`/var/lib/appio/sample-period.complete.json`. These are root-owned, single-link
regular files with mode `0600`, bounded to 1 KiB, under the existing
`/var/lib/appio` state directory. Each record is canonical ASCII JSON followed by
one LF, with exactly these ordered keys and value constraints:

```text
{"version":1,"operation_id":"<32 lowercase hex>","target":"/etc/systemd/system/appio-iod.service.d/20-sample-period.conf","sample_seconds":<2..30>,"target_sha256":"<64 lowercase hex>"}\n
```

`operation_id` is a fresh `uuid4().hex` for a new transaction. The target digest
is SHA-256 over the exact canonical drop-in bytes. The pending and completed
records have the same schema and represent different transaction states; the
completed record is success evidence only when its operation ID, target, period,
and digest match the requested drop-in. The journal uses dedicated fixed-state
path and content validation, separate from `FIXED_CONFIG_TARGETS` and the exact
`{version, pending_by_uid}` delegation state in `setup-state.json`. It is never
returned to the GUI or included in the setup result.

Before any required drop-in write, daemon reload, or active-unit restart, publish
a pending record using the safe atomic-write path and fsync `/var/lib/appio`.
Also publish pending when the requested target has no matching completed record.
Under step 5, that condition sets `manager_operation_required`, so the incomplete
transaction must run `daemon-reload` and, when active, `try-restart` before
completion. Reuse a matching incomplete pending operation ID only after fsyncing
its parent; for a different target value, replace it with a new ID and make that
replacement durable before proceeding. A pending record without a completed record
carrying the same ID is incomplete even when the current bytes and observations
happen to match. It forces recovery: fsync the drop-in parent, daemon-reload, and
`try-restart` only if the unit is active; for an inactive unit, reload only and
never start it.

After the final lifecycle read and all configuration/runtime checks pass, atomically
publish the matching completed record and fsync its parent. A write or sync error
leaves the pending record and cannot produce a success result. If the completed
record became visible but its directory fsync failed, a retry fsyncs that directory
and treats matching pending/completed IDs plus matching fresh observations as a
recovered `repair`; if those observations differ, the helper creates a new pending
ID and performs recovery again. Only after the completed record is durable may the
helper unlink the pending record and fsync the directory. A pending unlink or its
parent-fsync failure does not revoke already durable completion; any pending record
seen on a later invocation must be reconciled as `repair` before `unchanged` is
eligible. Malformed record contents are never completion evidence; an unsafe path,
owner, mode, or link count fails closed. A no-op is eligible for `unchanged` only
when no pending record exists and the completed record matches the requested
path, period, and digest.

The handler executes these steps in order:

1. Run the existing lifecycle show query before reading or writing the target;
   retain its `ActiveState` and `Result` values as `active_state_before` and
   `result_before`, and set `active_before = (active_state_before == "active")`.
2. Read the sample target through
   `SafeRootFS.read_bounded(SAMPLE_PERIOD_DROPIN, max_bytes=64*1024,
   create_parents=False)`; treat a missing leaf or parent as an absent target and
   represent it as `None`. This preflight read must not create any directory.
3. If existing bytes are non-empty and `parse_sample_period_dropin` returns
   `None`, fail with exit 2 and reason `foreign_drop_in`. The bounded diagnostic is
   `foreign_drop_in path=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf`.
   No target bytes may change.
4. Compute `needs_write = (existing_bytes != canonical_bytes)` and
   `target_sha256 = SHA256(canonical_bytes)`. Read and validate both journal
   records through `SafeRootFS.read_bounded(..., create_parents=False)`; a missing
   leaf or parent is absence, and neither read may create a directory. Set
   `completed_matches_request` only when the completed record matches the
   requested target, seconds, and digest. Set `completed_recovery_candidate` only
   when pending and completed records have identical operation ID, target,
   seconds, and digest and those fields also match this request. A valid pending
   record without that exact pair is incomplete; retain its ID only when its
   target, seconds, and digest match this request, otherwise allocate a fresh ID
   before operation. A matching pair still requires step 6's current-invocation
   durability checks for `/var/lib`, `/var/lib/appio`, `/etc/systemd/system`, and
   the drop-in parent, including containing-directory fsyncs when the child
   directories already existed, and matching target/configuration/status/lifecycle
   observations before it can establish recovered completion. Malformed content cannot
   establish completion and sets `journal_reconciliation_required`; do not publish
   or replace a journal record during this step. After step 5 returns a valid
   zero-exit configuration view, step 6 publishes a fresh pending record for any
   malformed record before a drop-in write, reload, or restart. Unsafe record
   metadata fails closed.
5. Read the manager configuration using `SAMPLE_SHOW_ARGV`. Parse the line-oriented
   output without shell expansion: each non-empty line is a requested
   `Property=value` field split at its first `=`, with no blank or unrecognized
   lines. The total stdout is bounded by the runner to `MAX_OUTPUT_BYTES` (64 KiB);
   truncation makes the response invalid. `Environment`, `DropInPaths`,
   `UnitFileState`, and `ActiveState` must each appear exactly once; duplicate
   fields are invalid. `EnvironmentFiles` may be omitted (empty list) or appear on
   multiple lines, one entry per line; retain each bounded value in output order,
   including repeated equal entries. `UnsetEnvironment` may be omitted (empty
   string) or appear exactly once; its duplicate line is invalid. All other
   duplicate fields are invalid. A zero-exit response violating these rules fails
   with exit 1, reason `sample_period_show_invalid`, and exact one-line diagnostic
   `sample_period_show_invalid`. In inactive-verification diagnostics, format an
   empty EnvironmentFiles list as `(none)` or join entries in output order with
   ` | `. If this initial read is invalid, retain the failure, skip steps 6–9,
   and continue to step 10 for the required final lifecycle read; do not publish a
   journal or create the drop-in or journal parent directories, or perform a
   drop-in write, reload, or restart. After the handler has performed step 10,
   `dispatch` still runs its exception observer; for this verb it must use the
   non-creating setup-state observation from §5, so it cannot create `/var/lib/appio`.
   The `Environment` value is a canonical token `APPIO_SAMPLE_SECONDS=<n>`; a
   missing, duplicate, malformed, or different token is a different configured
   value. `EnvironmentFiles` entries are the ordered list described above, and
   `UnsetEnvironment` is retained verbatim. Read the daemon status reply as a
   separate bounded status observation when the unit is active; its `sample_ns`
   divided by `NS` is the running value only when it is a positive exact multiple
   of `NS`. Record whether the configured value equals `n` and whether an active
   daemon reports `n * NS`. Set `manager_operation_required` when the target needs
   writing, merged configuration differs, the active daemon value differs or is
   unknown, no matching completion evidence exists, or
   `journal_reconciliation_required` is true. A failed prior period operation is
   represented by its matching incomplete pending record, not inferred from the
   unit-wide lifecycle `Result`. A completed-recovery
   candidate is usable only when the current target bytes, configuration, active
   daemon value when active, and initial lifecycle view match this request;
   otherwise allocate a fresh pending ID and replay the required operations.
6. Use `manager_operation_required` from step 5. After the valid initial view,
   create any missing fixed target or journal parent directories through the safe
   pinned traversal; each `mkdir` immediately fsyncs its containing directory.
   Before any branch that can select success, including `unchanged` and recovered
   completion, perform the current-invocation containing-directory fsyncs for
   `/etc/systemd/system` and `/var/lib`, and the leaf-directory fsyncs for
   `/etc/systemd/system/appio-iod.service.d` and `/var/lib/appio`, even when the
   corresponding directories already exist. A failure is
   `sample_period_state_write_failed`, retains any durable pending
   record, and cannot select success. If there is no pending record,
   `completed_matches_request` is true, `journal_reconciliation_required` is
   false, and `manager_operation_required` is false, leave the journal untouched
   for the `unchanged` path. If
   `completed_recovery_candidate` is true and the target/configuration/status and
   initial lifecycle observations match, fsync `/var/lib/appio`, retain the
   existing operation ID, and do not replay manager operations; step 10 still
   supplies the mandatory final lifecycle check. In every other case, publish a
   matching pending record and fsync `/var/lib/appio` before continuing. This
   cannot be a no-command completion: absent matching completion evidence makes
   `manager_operation_required` true in step 5, so the incomplete transaction
   requires `daemon-reload` and, if active, `try-restart` before it can complete.
   Reuse an incomplete pending ID only for the same target, seconds, and digest;
   otherwise allocate a fresh ID and replace the old pending record. Set
   `incomplete_pending=true` for this transaction so required repair commands
   below cannot be skipped. If `needs_write`, atomically write the canonical
   drop-in bytes only after pending is durable. If target bytes are already
   canonical but the transaction is incomplete, fsync its parent directory before
   relying on them. A matching prior completed record proves the target-directory
   sync only when it matches this request and no new operation is needed; identical
   bytes alone never prove that an earlier operation completed.

7. Run `/usr/bin/systemctl daemon-reload` when `needs_write` is true, the configured
   value is not exactly `n`, or the transaction is incomplete. On a nonzero result,
   retain `daemon_reload_failed`, skip the restart and verification steps, and
   continue to the final lifecycle read in step 10. The pending record remains
   durable for a later repair, and this invocation cannot select a successful
   outcome.
8. Run `/usr/bin/systemctl try-restart appio-iod.service` only when
   `active_before` is true and at least one of `needs_write`, configured value not
   equal to `n`, running value different from `n`, running value unknown, or
   incomplete transaction is true. On a nonzero result, retain the
   `service_restart_failed` operation failure and continue through steps 9–10;
   outcome selection waits for the final lifecycle state. If that read proves the
   unit inactive, precedence reports `service_not_running`; if it proves the unit
   remains active, the result is `service_restart_failed`. No successful outcome
   is possible after a nonzero restart. `try-restart` is never used for an inactive
   unit and therefore cannot start or enable collection.
9. Verify the resulting state. Re-read the configuration view and require the
   merged `Environment` value to equal `n`. If this zero-exit `SAMPLE_SHOW_ARGV`
   response is malformed under step 5's field rules, retain
   `sample_period_show_invalid`, continue to step 10, and select no successful
   outcome. For an active unit, a configuration mismatch, a missing/invalid status
   reply, or `sample_ns != n * NS` is an active verification failure. For an
   inactive unit, a merged `Environment` mismatch, a non-empty `EnvironmentFiles`
   list, or `UnsetEnvironment` naming `APPIO_SAMPLE_SECONDS` is an unverifiable
   configuration failure. Inactive verification does not claim a runtime value.
   Record verification failures and continue to step 10 so lifecycle precedence is
   evaluated before returning.
10. Read lifecycle after operation/configuration verification. If the observed unit is
    active, obtain a fresh bounded daemon-status reply after that lifecycle read and
    require `sample_ns == n * NS`; missing reply or mismatch records active
    verification failure and cannot select success. Read lifecycle once more to
    establish `active_state_after`, `result_after`, and `active_after`. If that final
    view is active, require a matching status observation obtained after the
    preceding active lifecycle read; otherwise fail closed. The final runtime probe
    is required for every active final outcome: `applied`, `repair`, `unchanged`,
    and the active-final path after `try-restart`. `Result` is unit-wide and may
    retain an earlier failure; it does not independently block an inactive apply
    or retry because the pending/completed journal is transaction-specific
    evidence. Apply `service_not_running` only when this call began active and the
    final lifecycle read shows it inactive. A non-success `result_after` while the
    unit remains active is handled by outcome row 3. If a required lifecycle read
    is unavailable or invalid, return its bounded failure with
    `sample_period_state=None`; no success is eligible without the final
    `ActiveState` and `Result`. Command or verification failures still perform the
    required lifecycle read(s) whenever the runner can obtain them.

11. For a matching pending/completed recovery pair, step 6 makes the visible
    completion durable; do not rewrite it. After final lifecycle and configuration
    checks pass, unlink pending and fsync its parent, then return `repair`. For a
    clean no-op with no pending record and a matching completed record, no journal
    write is needed; return `unchanged`. Otherwise atomically publish the completed
    record for the pending operation and fsync `/var/lib/appio` before selecting a
    successful outcome. A failed completed write or sync returns
    `sample_period_state_commit_failed` (or the originating bounded error), leaves
    pending evidence, and selects no success. After durable completion, unlink
    pending and fsync its parent. If unlink succeeds but the parent fsync fails,
    the current operation may still return its verified success because completion
    is already durable. On a later invocation, repeat the directory durability
    checks before selecting any result: reconcile a visible pending record as
    `repair`; if pending is absent, `unchanged` is eligible only after fresh
    directory fsyncs confirm its absence and the completed record and fresh
    observations still match.

An active verification failure uses exit-1 reason `sample_period_overridden` and
exact diagnostic `sample_period_not_in_effect observed=<value> DropInPaths=<paths>`.
For an active `Environment` mismatch, `<value>` is the canonical integer seconds
from the merged `Environment` token, or `unknown` if it is missing, duplicate,
malformed, or noncanonical. For an active status mismatch or unavailable status,
`<value>` is the daemon-reported seconds only when `sample_ns` is a positive exact
multiple of `NS`; otherwise it is `unknown`. `<paths>` is the raw `DropInPaths`
field or `(none)`. This covers a winning other drop-in, `EnvironmentFile=`,
`UnsetEnvironment=`, or daemon value.

An inactive verification failure uses exit-1 reason `sample_period_unverifiable`
and exact diagnostic
`sample_period_unverifiable observed=<value> EnvironmentFiles=<files> UnsetEnvironment=<unset> DropInPaths=<paths>`.
Here `<value>` is parsed from the merged `Environment` token using the same rule
above, or `unknown`. `<files>` is `(none)` when empty or the retained
EnvironmentFiles entries joined in output order with ` | `; `<unset>` and
`<paths>` are the raw fields or `(none)`. All diagnostics are single lines.

Outcome selection is ordered and exclusive:

1. If `active_before` is true and the final lifecycle read shows the unit inactive
   (`active_after` is false), return `service_not_running`; it suppresses later
   successes and lower-precedence failures for this current-call active-to-inactive
   transition. An inactive unit's persisted earlier `Result`/`ActiveState` does not
   select this outcome.
2. Otherwise, a nonzero `try-restart` with a valid final lifecycle view showing the
   unit active returns `service_restart_failed`.
3. Otherwise, any write, reload, observation, verification, journal, or required
   status-probe failure returns its bounded reason; if the unit remains active with
   a non-success `Result` and no higher-precedence row applies, use
   `service_lifecycle_failed`.
4. Otherwise, select exactly one successful result: `applied` requires a target write
   and an active final state; `applied_not_running` requires a target write and both
   the before and final lifecycle states inactive; `repair` requires no target write
   but a reload, restart, incomplete-journal replay, or completion-evidence
   recovery/publication; `unchanged` requires no write, reload, or restart, no
   pending record, a matching durable completion, no required manager operation,
   and matching fresh observations plus directory checks. The final verification
   and durable-completion requirements below apply to every success. Any active
   success also requires a post-lifecycle runtime-status match. The selected result
   is carried in `sample_period_state`; the existing `state` field keeps its
   service-state meaning.


Every successful outcome requires a durable completed record for the exact target,
period, and canonical bytes, a valid final lifecycle view containing `ActiveState`
and `Result`, and no failure from this invocation. If the unit remains active,
`result_after` must be `success`; while inactive, a non-success `Result` may be
retained from an earlier invocation and does not independently reject a verified
period operation. A clean identical apply can be `unchanged` whether the unit is
active or inactive; an inactive canonical retry that needs only a reload is
`repair`. A pending record left by a failed write sync, reload, restart, or
completion commit prevents `unchanged` even if later systemd and daemon observations
happen to match. The retry must either confirm a matching completed transaction
with fresh directory durability checks and classify it as `repair`, or replay the
required operations and commit a new completed record. A failed restart that
leaves the unit inactive is `service_not_running` on that first invocation because
it began active and ended inactive. On a later apply, the matching incomplete
pending record requires repair; the inactive helper reloads and verifies without
starting the unit, then can complete the transaction. The existing Settings switch
remains the only start control.

The result carries `sample_period=n` for a `set-sample-period` invocation that
reaches dispatch, including operation failures. A pre-identity boundary error such
as `sample_period_out_of_range` carries `sample_period=null`. Existing lifecycle
verbs leave both period result fields null.

## 8. GUI contract

`SettingsDialog.ROWS` is exactly `("system_services", "sampling_period")`.
The second peer row has the visible form
`Sampling period (system service)  [ 10 ] s  (2-30)  [Apply]`, with its status
line beneath it and the hint `Shorter periods increase raw history writes and CPU use.`
Use a `QSpinBox` with `setRange(2, 30)`, `setSuffix(" s")`, and
`setKeyboardTracking(False)`. The object names are exactly:

```text
sample_period_row
sample_period_spin
sample_period_apply
sample_period_status
```

`focus_row("sampling_period")` focuses `sample_period_spin`. Any other unknown
row still raises `ValueError("unknown settings row")`.

The spin box initially contains the last valid reported
`sample_ns // NS` when `sample_ns` is a positive exact multiple of `NS` in the
range 2 through 30; otherwise it contains 10. A refresh updates the spin box
only when there is no edited-but-unapplied value and no pending operation. The
status line is rendered from the observed collector value independently of the
spin box. The Apply button is disabled while any helper operation is pending and
remains enabled when the daemon is not answering.

`ServiceFacts` becomes:

```python
sample_ns: int | None = None
```

The field is appended after `restarts`. It accepts `None` or a positive integer
excluding `bool`; invalid values raise
`ValueError("sample_ns must be a positive integer or None")`.

`production_probe` passes a validated status `sample_ns` through when the status
reply says `answering is True`. A disconnected, invalid, non-answering, restarted,
or generation-mismatched observation carries `sample_ns=None`. The existing
`ProbePayload` retains the value with the other facts; stale payloads are dropped
by the existing ticket/generation checks.

Apply stores `pending_period_seconds` as seconds, calls
`SetupClient.invoke("set-sample-period", period_seconds=n)` only after the existing
consent dialog is accepted, and never sends a request on cancellation. The new
consent caption is `Apply sampling period`. Its text includes this exact command
line:

```text
  /usr/bin/pkexec /usr/bin/appio-setup set-sample-period N
```

It also states that an active service is restarted and an inactive service is not
started. The helper's bounded first stderr line remains the source of the failure
message when launch or authentication fails.

On each fresh probe, compare nanoseconds exactly:
`reported_sample_ns == pending_period_seconds * NS`. An answering probe that reports a
match clears the pending request and shows the verified status. An answering probe
that reports another period leaves the pending request visible and shows the mismatch
string. If the service remains inactive after a successful helper result with
`sample_period_state` equal to `applied_not_running` or `repair`, the helper's verified
`sample_period` is shown only with the saved status line and the request remains
pending until a later answering probe reports the same nanosecond value. Do not label
that state `Sampling every N s.` or treat it as collector-reported. A failed helper
result never establishes saved status. A helper result with
`sample_period_state="applied_not_running"` or inactive `repair` therefore shows the
saved string until a later probe verifies a running value.

The exact status lines are:

| Situation | Status line |
|---|---|
| verified match | `Sampling every N s.` |
| collector not answering | `Sampling period not known while the collector is not answering.` |
| answering but `sample_ns` absent | `Sampling period not reported by this collector.` |
| applied while inactive, including a successful inactive `repair` | `Saved: sampling every N s once the system service starts.` |
| helper failed before changing anything | the helper's bounded first stderr line |
| apply completed but the value is not in effect | `Requested N s, collector reports M s.` |

A superseding console or operation is therefore a mismatch, never a success. The
existing single-switch assertions remain: the period Apply control can restart
an active collector, but no navigation or period operation can enable or start an
inactive collector. `appio --interval` and local `tick_local` remain untouched.

## 9. Literal acceptance and refusal tables

### 9.1 Environment parser

| Raw value | Result | Warning |
|---|---|---|
| absent | `10 * NS` | none |
| `""` | `10 * NS` | none |
| `"2"`, `"9"`, `"10"`, `"29"`, `"30"` | corresponding seconds times `NS` | none |
| `"0"`, `"1"`, `"31"`, `"02"`, `"+5"`, `"5.0"`, `" 5"`, `"abc"` | `10 * NS` | exactly one `sample_period_invalid` warning |
| one control character | `10 * NS` | exactly one escaped single-line warning |
| a 4,000-character string | `10 * NS` | exactly one warning containing `repr(raw)[:64]` |
| a non-string present value | `10 * NS` | exactly one warning |

### 9.2 Shim and boundary

| argv payload | Shim | Boundary |
|---|---|---|
| old verb in two-element form | accepts shape | accepts after existing identity checks |
| `set-sample-period 2` or `30` | accepts shape | accepts and stores the integer |
| `set-sample-period 1`, `31`, or `0` | accepts shape | exit 2 `sample_period_out_of_range` |
| `set-sample-period 12345678` | accepts shape | exit 2 `sample_period_out_of_range` |
| `set-sample-period abc`, `02`, ` 5`, empty, signed, decimal, or nine digits | exit 2 with usage | never reaches account lookup |
| old verb with a third element | exit 2 with usage | never reaches account lookup |
| `set-sample-period` with no third element | exit 2 with usage | two-element boundary form is invalid |
| unknown verb or wrong executable | exit 2 with usage | exit 2 `invalid_verb_or_argument_count` |

### 9.3 Drop-in parser and write boundary

| Existing target bytes | Parser | Helper behavior |
|---|---|---|
| exact canonical bytes for 2, 10, or 30 | corresponding integer | ours; identical bytes may be repaired or unchanged |
| empty bytes | `None` | replace with canonical bytes |
| missing final LF or two final LFs | `None` | exit 2 `foreign_drop_in`; preserve bytes |
| `Environment=APPIO_SAMPLE_SECONDS=02` | `None` | exit 2 `foreign_drop_in`; preserve bytes |
| different section, key, value, extra line, or trailing space | `None` | exit 2 `foreign_drop_in`; preserve bytes |
| non-UTF-8 bytes | `None` | exit 2 `foreign_drop_in`; preserve bytes |
| path absent from `FIXED_CONFIG_TARGETS` | not reached | exit 2 `target:not_fixed_path` |
| fixed path with no expected-content parser branch | not reached | exit 2 `unexpected_content:not_a_config_target` |
| symlink, hard link, wrong owner, writable target, oversized target | not reached | existing trust refusal; preserve bytes |

### 9.4 Status and facts

| Value | Status validator | `ServiceFacts.sample_ns` |
|---|---|---|
| field absent | accepted; normalize to no period | accepted as `None` |
| explicit JSON `null` | rejected with `invalid_field` | accepted as `None` |
| exact integer from `1` through `30 * NS` | accepted | accepted as a positive integer |
| bool, float, string, zero, negative, or above `30 * NS` | rejected with `invalid_field` | rejected for bool, non-integer, and non-positive values; any positive integer remains valid |
| unknown status key | rejected with `unknown_key` | not applicable |

### 9.5 Manager configuration view

| Property | Accepted output shape | Active verification | Inactive verification |
|---|---|---|---|
| `Environment`, `DropInPaths`, `UnitFileState`, `ActiveState` | Exactly one line each; duplicates are invalid. | Verify merged sample token and running period. | Verify merged sample token. |
| `EnvironmentFiles` | Omitted means empty; multiple lines are allowed and retained in order, including repeats. | Post-lifecycle runtime status is authoritative for running period; `DropInPaths` appears in override diagnostics. | Any entries are unverifiable; list entries in order, joined with ` | `, or `(none)`. |
| `UnsetEnvironment` | Omitted means empty; otherwise exactly one line. | A winning unset that leaves the daemon mismatched fails active verification. | Naming `APPIO_SAMPLE_SECONDS` is unverifiable; retain raw value. |

### 9.6 Period journal

The current-invocation durability checks in §5 apply to every row that can select
success: fsync `/etc/systemd/system` and `/var/lib`, then the leaf directories
`/etc/systemd/system/appio-iod.service.d` and `/var/lib/appio`, even when they
already existed. A visible target or completed record cannot replace these checks
after an earlier ancestor-directory fsync failure.

For incomplete transactions, pending is durable before manager operations. Missing
matching completion evidence sets `manager_operation_required`; therefore a new
pending transaction must run `daemon-reload` and, when active, `try-restart` before
completion. There is no no-manager-operation completion path for absent, stale, or
mismatched completion evidence.

| Pending record | Completed record | Fresh observations | Required behavior |
|---|---|---|---|
| Absent | Absent, stale, or mismatched | Any | Publish a fresh durable pending record, run `daemon-reload`, `try-restart` only if active, verify, and publish completion. No no-command completion is allowed. |
| Valid pending does not match the request | Any | Any | Allocate a fresh pending ID, publish it durably, run `daemon-reload`, `try-restart` only if active, verify, and publish completion. |
| Valid pending matches the request | Absent or different operation ID | Even if current values match | Treat as incomplete; fsync the target parent, reload, restart only if active, verify, and publish completion. |
| Valid pending and completed record form a matching pair | Same operation ID, target, seconds, digest | All match | Repeat state/target containing- and leaf-directory fsyncs; if active, require the post-lifecycle runtime-status match; classify recovered completion as `repair`, then unlink pending durably. |
| Valid pending and completed record form a matching pair | Same operation ID, target, seconds, digest | Any differ | Allocate a fresh pending ID before replay; do not report `unchanged`. |
| Absent | Matching record for this request | A target, merged configuration, or active-runtime observation requires an operation | Publish a fresh pending ID, run the required manager operations, verify, and publish completion. |
| Pending unlink parent-fsync failed on prior call | Matching completed record | Pending may be present or absent | Repeat directory fsyncs. If pending is present, reconcile as `repair`; if absent, allow `unchanged` only after pending absence and matching fresh observations are durably confirmed. |
| Malformed journal at safe fixed path | Any | Any | Defer replacement until valid initial `SAMPLE_SHOW_ARGV`, then publish fresh pending before manager operations. Invalid initial show leaves target/state parents uncreated, including during exception observation. |
| Unsafe journal path or metadata | Any | Any | Fail `sample_period_state_unsafe` before drop-in mutation. |
| No pending record | Matching completed record | Request and observations match; current directory fsyncs succeed | `unchanged` is eligible, subject to final lifecycle and active runtime-status checks. |

## 10. Exit codes and exact diagnostics

| Condition | Exit | Exact reason or diagnostic |
|---|---:|---|
| invalid arity, verb, executable, or payload shape | 2 | `invalid_verb_or_argument_count` at the boundary; shim usage for shim-only rejection |
| canonical period outside 2-30 | 2 | `sample_period_out_of_range`; before caller identity is resolved, JSON has `uid: null`, `sample_period: null`, and `sample_period_state: null` |
| non-empty foreign period drop-in | 2 | reason `foreign_drop_in`; diagnostic `foreign_drop_in path=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf` |
| target omitted from fixed allowlist | 2 | `target:not_fixed_path` |
| fixed target omitted from expected-content parser | 2 | `unexpected_content:not_a_config_target` |
| active before and inactive after | 1 | `service_not_running`; evaluated first and suppresses later failures for this current-call transition |
| try-restart command fails and the valid final lifecycle view shows the unit active | 1 | `service_restart_failed`; `sample_period_state=null` |
| daemon reload command fails after the first lifecycle-precedence row does not apply | 1 | `daemon_reload_failed`; `sample_period_state=null` |
| active verification finds a winning alternate value/source after the first row does not apply | 1 | reason `sample_period_overridden`; diagnostic `sample_period_not_in_effect observed=<value> DropInPaths=<paths>` |
| inactive verification finds a merged `Environment` mismatch, one or more `EnvironmentFiles` entries, or the sample variable in `UnsetEnvironment`, after the first row does not apply | 1 | reason `sample_period_unverifiable`; diagnostic `sample_period_unverifiable observed=<value> EnvironmentFiles=<ordered entries or (none)> UnsetEnvironment=<unset> DropInPaths=<paths>` |
| zero-exit `SAMPLE_SHOW_ARGV` response has a blank/unrecognized line, duplicate single-valued field, missing required field, or exceeds the bounded output | 1 | reason `sample_period_show_invalid`; exact diagnostic `sample_period_show_invalid` when lifecycle precedence does not select another failure; repeated `EnvironmentFiles` lines are valid and retained in output order; initial-read failure still skips mutations, performs final lifecycle observation and non-creating dispatch exception observation |
| final lifecycle view is unavailable or invalid | 1 | bounded final-observation failure; `sample_period_state=null` and no successful outcome |
| active final lifecycle view is active but `result_after != "success"`, after the first two precedence rows do not apply | 1 | `service_lifecycle_failed`; `sample_period_state=null`; no successful outcome |
| unsafe journal path, owner, mode, or link count when the current-transition precedence row does not apply | 1 | `sample_period_state_unsafe`; no drop-in operation or successful outcome |
| required step-6 directory fsync or pending journal write fails before final verification, after current-transition precedence does not apply | 1 | `sample_period_state_write_failed`; `sample_period_state=null`; retain pending when present; if this precedes the first target write, perform no drop-in mutation, reload, or restart |
| completed journal write or its parent-directory fsync fails after final verification, when current-transition precedence does not apply | 1 | `sample_period_state_commit_failed`; `sample_period_state=null`; pending evidence remains and no success is selected |
| pending cleanup unlink succeeds but its parent fsync fails after durable completion, when current-transition precedence does not apply | current success remains valid | A later invocation repeats directory durability checks; `repair` if pending is visible, `unchanged` only after durable pending absence and matching fresh observations |
| drop-in write, rename, or another bounded command fails after the first row does not apply | 1 | preserve the originating bounded error; `sample_period_state=null` |
| missing helper or cancelled authentication | launcher outcome | existing bounded first stderr line; no fabricated helper result |

The shim always writes `appio-setup: ` followed by its message and one LF. The
service entry point prints the preserved reason or bounded stderr diagnostic and
always emits JSON using the existing compact, ASCII-safe encoder. Oversized JSON
still returns `{"error":"result:too_large"}` with exit 1.

## 11. Tests mapped to the eight success criteria

Each named test below must assert the mechanism stated in its row and use the
listed fixture. Root acceptance tests may use the existing privileged acceptance
harness; unit tests must use the existing injected filesystem, runner, clock,
status-reader, and launcher seams.

The executable acceptance phases are deliberately split by isolation. The
rootless pytest nodes use injected clocks/runners and temporary databases. The
host `period-runtime` phase runs on the disposable systemd login after the
existing `verify` phase and before `disable`. `tools/acceptance/appio-p3-root.py`
implements these host assertions. Run it with
`tools/acceptance/appio-p3-root.py --run --phase period-runtime`; the phase runs
the host assertions mapped to criteria 2, 3, 5, and 6 in the table below.
The phase takes about thirteen minutes and records results in the existing 0600
evidence file. It applies 2 seconds then 30 seconds with
`helper("set-sample-period", str(seconds))`, verifies active state and a changed
systemd `InvocationID` after each active apply, checks the reported `sample_ns`,
and checks committed sample intervals from user-owned `export-history` copies.
For each active apply, capture a baseline and final export and inspect only these
user-owned copies. Require either (a) a newly committed `gap` row whose `gap_id`
is greater than the baseline maximum, with matching `boot_id`, `source='iod'`, and
`reason='collector_gap'`, or (b) a documented partial final interval for the new
`iod_epoch` with `interval_start_mono_ns` before the restart boundary and
`observed_ns` truncated at the new epoch start. This assertion proves the active
restart is represented in exported history; a continuous-looking wall-clock frame
sequence alone is insufficient. After the successful 30-second apply, the phase records the
drop-in `(st_ino, st_mtime_ns)` and service `InvocationID`, reapplies 30 seconds,
requires exit 0 with `sample_period_state == "unchanged"`, then proves both
recorded values are unchanged. It reads target metadata only, never the installed
database or system journal. The ten-minute check uses a monotonic clock for at
least 600 seconds. It captures baseline and final user-owned export copies and
requires their `boot_id` and `iod_epoch`, plus the service `InvocationID`, to
remain unchanged as evidence that no collector recovery or service restart
occurred. It derives distinct committed
`source='iod'` frame endpoints ordered by `(epoch, seq)` and computes `delta` as
the difference between successive `observed_mono_ns` values. It requires at least
18 endpoints in the measured window, with the first and last no farther than
`30*NS + max(500_000_000, 30*NS//10)` from their respective boundaries. Cadence must remain within
`max(500 ms, 10% of 30 seconds)` of the requested 30-second period; as a
separate check with no cadence tolerance, every elapsed interval must be strictly
less than `flush_ns_for(30*NS)` (90 seconds), so an interval at or above the
overdue boundary fails. The phase has an unconditional cleanup path. Even when a runtime
assertion fails, it applies 10 seconds with `helper("set-sample-period", "10")` and probes status
again; it records cleanup evidence and cannot report phase completion unless the
helper succeeds and the collector reports `sample_ns == 10*NS`. A failed restore
fails the phase and is recorded in the evidence file. The phase never opens
`/var/lib/appio` or depends on permission to read the system journal. It is
separate from `period-capacity`, which uses only its disposable materialized
SQLite fixture. A dry run of either phase remains non-mutating: it imports no
appio module, reads no host state, runs no command, and writes no evidence file.
Every helper call passes exactly one of the seven accepted verbs. The host harness
uses `helper(verb, *command_args, observe=None)`: positional `command_args` are
appended after the verb in the fixed pkexec argv, while `observe` is keyword-only
and cannot be mistaken for a payload. Only `set-sample-period` accepts exactly
one canonical seconds argument in 2..30; the other six verbs reject all extra
command arguments. Existing status polling calls `helper("status", observe=observe)`;
period apply and cleanup use `helper("set-sample-period", str(seconds))`. A
regression test asserts the exact argv and observer separation. The `period-runtime`
phase does not use `--allow-session-termination`.


| Criterion | File, test name, assertion, and fixture |
|---:|---|
| 1 | `tests/p3/test_iod_cadence_pins.py::test_raw_persistence_follows_the_documented_ten_second_cadence` keeps its 15 two-second ticks and existing sequence/total assertions; `tests/p3/test_iod_cadence_pins.py::test_production_main_uses_ten_second_default_when_environment_is_absent` stubs the daemon entry point, removes the setting, and asserts `sample_ns=10*NS`, `flush_ns=30*NS`, and `maintenance_ns=30*NS`; `tests/p3/test_iod_cadence_pins.py::test_main_empty_environment_starts_with_default_period` runs the production startup seam and asserts no invalid warning and successful start. |
| 2 | `tests/p3/test_iod_cadence_pins.py::test_production_main_passes_valid_period_and_derived_windows` parameterizes 2 and 30 and asserts the three constructor values; `test_maintenance_ns_for_clamps_all_supported_periods` asserts `(2,15)`, `(5,15)`, `(10,30)`, `(20,60)`, and `(30,60)` seconds; `test_status_reports_constructor_sample_ns` constructs `Daemon(sample_ns=2*NS)` and asserts the status field; `test_flush_ns_for_uses_three_periods_at_two_ten_eleven_and_thirty` asserts 30, 30, 33, and 90 seconds. The host assertion `tools/acceptance/appio-p3-root.py::test_two_second_and_thirty_second_runs_match_status_and_intervals` is executed by `tools/acceptance/appio-p3-root.py --run --phase period-runtime`; it applies 2 s and 30 s to the disposable installed service, checks reported `sample_ns`, and checks successive committed `sample.observed_mono_ns` frame intervals within `max(500 ms, 10% of the requested period)`. It reads only a user-owned `export-history` copy with `rows()`, never the installed database path. |
| 3 | `tests/p3/test_iod_cadence_pins.py::test_sampling_overdue_gate_uses_derived_flush_window` drives the sampling-duration gate below and above `flush_ns_for(30*NS)` with a controlled clock; `tests/p3/test_iod_cadence_pins.py::test_step_overdue_gate_uses_derived_flush_window` drives the pre-sample gate with the same clock; `tests/p3/test_iod_cadence_pins.py::test_shutdown_recovery_gate_uses_derived_flush_window` drives `stop()` below and above the threshold and asserts recovery/epoch effects. The host assertion `tools/acceptance/appio-p3-root.py::test_thirty_second_ten_minute_run_has_no_overdue_paths` runs in `tools/acceptance/appio-p3-root.py --run --phase period-runtime` for at least 600 monotonic seconds, verifies `sample_ns == 30*NS`, captures baseline and final user-owned export copies and requires unchanged `boot_id`, `iod_epoch`, and service `InvocationID`, proving no collector recovery or service restart during the measured window; derives distinct committed `source='iod'` frame endpoints ordered by `(epoch, seq)` and computes `delta` from successive `observed_mono_ns` values; requires at least 18 endpoints in the measured window with first and last no farther than `30*NS + max(500_000_000, 30*NS//10)` from their respective boundaries; requires each cadence interval to satisfy `abs(delta - 30*NS) <= max(500_000_000, 30*NS//10)` and, independently with no cadence tolerance, every interval to be `< flush_ns_for(30*NS)` (90 seconds), so any interval at or above 90 seconds fails; it records the result without reading the system journal. |
| 4 | `tests/p3/test_iod_cadence_pins.py::test_parse_sample_seconds_acceptance_table` uses the literal accepted values in section 9.1 and asserts integers; `tests/p3/test_iod_cadence_pins.py::test_parse_sample_seconds_refusal_table` uses all named invalid values and asserts `None`; `tests/p3/test_iod_cadence_pins.py::test_configured_sample_ns_invalid_values_warn_once_and_start` parametrizes `1`, `31`, `abc`, `02`, ` 5`, a control character, and a 4,000-character value, asserts 10 seconds, exactly one log record, and completed startup; `tests/p3/test_iod_cadence_pins.py::test_status_validator_accepts_period_and_older_status` covers a present valid `sample_ns`, an absent key, explicit null rejection, and the exact-integer bounds in section 9.4. |
| 5 | `tests/p3/test_setup_service.py::test_set_sample_period_inactive_writes_and_never_starts` uses a fake inactive unit and records no `enable`, `start`, or `try-restart`; `tests/p3/test_setup_service.py::test_set_sample_period_active_uses_try_restart` uses an active unit and asserts the exact reload/restart argv; `tests/p3/test_collector_window.py::test_sampling_apply_preserves_single_switch_activation_invariant` uses a fake setup client and asserts an inactive service is never started. The host assertion `tools/acceptance/appio-p3-root.py::test_apply_two_and_thirty_seconds_restarts_only_active_service` runs in `tools/acceptance/appio-p3-root.py --run --phase period-runtime`: before each active apply it records the service `InvocationID`, then requires a successful result, a changed `InvocationID`, active state, and the requested status value for both 2 s and 30 s; unconditional phase cleanup then restores 10 s and verifies `sample_ns == 10*NS` before evidence is complete. The inactive no-start case remains pinned by the fake-runner unit test. The host assertion `tools/acceptance/appio-p3-root.py::test_active_period_apply_records_restart_gap` verifies a newly committed `collector_gap` after each active apply using before/after user-owned exports. |
| 6 | `tests/p3/test_setup_service.py::test_active_identical_successful_apply_is_unchanged` and `test_inactive_identical_successful_apply_is_unchanged` preload canonical bytes, matching configuration/status, and a matching completed record; they assert no write, reload, or restart and `unchanged` for active and inactive final states. `test_inactive_canonical_retry_with_only_reload_is_repair` asserts `daemon-reload` plus a durable completion record returns `repair` with no start/restart. `test_identical_apply_recovers_after_target_directory_sync_failure`, `test_identical_apply_recovers_after_reload_failure`, and `test_identical_apply_recovers_after_restart_failure` make the first invocation fail, then make post-failure bytes/configuration/status/lifecycle appear to match; each retry must replay the required operations and return `repair`, never `unchanged`. `test_identical_apply_recovers_after_completed_record_directory_sync_failure` makes the new completion record visible while its directory fsync fails; the retry fsyncs the state directory, verifies matching IDs and fresh observations, and returns `repair`. `tests/p3/test_setup_files.py::test_period_ancestor_directory_sync_repeats_for_existing_parent` records the actual containing-directory descriptors fsynced when the period directories already exist. `tests/p3/test_setup_service.py::test_apply_after_ancestor_directory_sync_failure_reports_applied` uses an active service with no target file; the first call creates the drop-in parent then fails an `/etc/systemd/system` or `/var/lib` ancestor fsync before pending publication or target write, and the retry repeats the fsync with the child directory present, writes canonical bytes, and returns `applied` only after success. `tests/p3/test_setup_service.py::test_identical_apply_recovers_after_ancestor_directory_sync_failure` preloads canonical target bytes and no journal, injects each containing-directory fsync failure, then verifies retry repeats sync with existing children, performs no target write, and returns `repair`, never `unchanged`. `tests/p3/test_setup_service.py::test_failed_restart_inactive_retry_repairs_without_start` makes the first active `try-restart` fail and leave the unit inactive; it asserts that invocation returns `service_not_running`, then the next apply uses the matching pending transaction to reload and verify without start or `try-restart`, returns `repair`, and durably completes the record. `tests/p3/test_setup_service.py::test_inactive_unrelated_failed_result_does_not_block_apply` starts with an inactive unit carrying a prior non-success `Result`, applies a new value, and asserts `applied_not_running` with no start. `tests/p3/test_setup_service.py::test_inactive_failed_result_does_not_mask_period_directory_sync_failure` injects a required step-6 directory-fsync failure while the inactive unit retains a prior failed `Result`; it asserts `sample_period_state=null` and `sample_period_state_write_failed`, not `service_not_running`. The host assertion `tools/acceptance/appio-p3-root.py::test_identical_period_reapply_is_unchanged` runs after the successful 30-second apply in `period-runtime`, repeats 30 seconds, and asserts helper `sample_period_state == "unchanged"`, unchanged target `(st_ino, st_mtime_ns)`, and unchanged service `InvocationID`, proving no drop-in rewrite or restart. `tests/p3/test_setup_service.py::test_final_lifecycle_activation_race_requires_fresh_sample_probe` proves an inactive step-9 observation followed by active lifecycle state requires a fresh matching runtime-status probe before active success. `tests/p3/test_setup_service.py::test_pending_unlink_sync_failure_rechecks_absence_before_unchanged` fails parent fsync after unlink and requires a later directory-fsync confirmation of pending absence before `unchanged`. `tests/p3/test_setup_service.py::test_missing_completion_evidence_replays_manager_operations` asserts absent or mismatched completion evidence forces reload and conditional restart. `test_active_before_inactive_after_has_service_not_running_precedence` forces a stop race; `test_final_lifecycle_read_failure_cannot_return_success` asserts failure with `sample_period_state=None`; `tests/p3/test_collector_window.py::test_sampling_apply_success_waits_for_matching_nanosecond_probe` asserts success only on exact `N*NS` equality. |
| 7 | `tests/p3/test_setup_files.py::test_sample_period_payload_and_parser_round_trip` covers 2, 10, and 30 exact bytes; `test_sample_period_target_is_in_both_allowlists` asserts both target tables; `test_missing_config_target_and_missing_parser_have_distinct_refusals` asserts `target:not_fixed_path` and `unexpected_content:not_a_config_target`; `test_foreign_sample_dropin_preserves_bytes` covers every refusal form; `test_empty_sample_dropin_is_replaced` covers the empty exception; `test_read_bounded_without_creating_missing_parents` proves preflight reads do not create absent parents; `test_period_journal_targets_are_fixed_state_only` proves the journal targets are separate from configuration targets; `test_period_journal_pending_and_complete_codec_is_exact` pins both record codecs; `test_period_journal_atomic_publish_syncs_parent` and `test_period_journal_remove_syncs_parent` assert durable publication and removal. `tests/p3/test_setup_service.py::test_sample_show_malformed_fields_fail_closed` supplies blank or unrecognized lines, a missing required field, and duplicate single-valued configuration fields in both the initial and verification reads; with a successful final lifecycle view it asserts exit 1, reason and exact diagnostic `sample_period_show_invalid`, no journal/drop-in write/reload/restart after an invalid initial read, and retained pending evidence with no successful result after an invalid verification read. `tests/p3/test_setup_service.py::test_malformed_initial_show_does_not_create_missing_preflight_directories` invokes public `dispatch` with absent drop-in and `/var/lib/appio` parents, supplies a malformed zero-exit initial configuration view and a successful final lifecycle view, and asserts the exact failure after `dispatch`'s exception observer runs; it proves both parents remain absent and that the observer's `_pending` to `load_setup_state(..., create_parents=False)` read does not create the state parent. `tests/p3/test_setup_service.py::test_sample_show_environment_files_multiple_entries_active_and_inactive` supplies two repeated `EnvironmentFiles` lines, verifies ordered aggregation in the active and inactive verification paths, checks inactive diagnostic formatting, and keeps duplicate rejection for single-valued fields. `tests/p3/test_setup_service.py::test_sample_show_omitted_empty_environment_files_is_empty` omits the `EnvironmentFiles` line and asserts successful empty-value parsing; `test_sample_show_omitted_empty_unset_environment_is_empty` does the same for `UnsetEnvironment`; `test_active_override_reports_value_and_dropin_paths` asserts the active reason and exact diagnostic; `test_inactive_environment_override_is_unverifiable_with_value_and_dropin_paths` supplies merged `Environment=APPIO_SAMPLE_SECONDS=7`, empty `EnvironmentFiles`/`UnsetEnvironment`, and `DropInPaths=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf /etc/systemd/system/appio-iod.service.d/30-conflict.conf`, then asserts exit 1, `sample_period_state=None`, reason `sample_period_unverifiable`, and exact diagnostic `sample_period_unverifiable observed=7 EnvironmentFiles=(none) UnsetEnvironment=(none) DropInPaths=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf /etc/systemd/system/appio-iod.service.d/30-conflict.conf`; `test_inactive_environment_sources_are_unverifiable` covers the inactive file and unset branches. |
| 8 | `tools/acceptance/appio-p3-root.py --run --phase period-capacity` is an executable host phase. It builds a disposable, materialized SQLite fixture outside `/var/lib/appio` with 226 app IDs and at least 57,968 aged, eligible core units (retention age at least two days). At a 2-second sample period, `maintenance_ns_for(2*NS)` is 15 seconds and each pruning pass is limited to 2,048 candidate units. The reserve is `2048 + ceil(1800/15) * (2048 - 226 * floor(15/2)) = 57,968`: it conservatively allows only seven generated units per app during each 15-second maintenance interval, so the initial backlog can keep every batch saturated for the full 1,800-second monotonic measurement window. Generate one eligible core unit per app every 2 seconds and run the production pruning path at each maintenance deadline. Before every prune call, assert and record that at least 2,048 eligible units remain; each call must retire exactly 2,048 materialized units with no skipped units. Also record the minimum pre-prune count, app count, initial eligible count, generated and retired units, elapsed wall time, final eligible count, and each prune result. The phase passes only when every measured batch was saturated, retired units per second exceed the fixture production rate, elapsed time is at least 1,800 seconds, and final eligible backlog is no greater than the initial backlog. A fixture that misses these conditions or lacks materialized aged rows is invalid; no comparison with `APPIO_QUOTA_BYTES` or physical database size is required. The phase invokes no helper or systemctl command and never opens the installed service database. `docs/acceptance/appio-p3-root.md` documents setup, invocation, the 30-minute minimum duration, reserve calculation, and evidence fields; `tests/p3/test_p3_acceptance.py::test_period_runtime_phase_is_available_in_side_effect_free_dry_run` and `test_period_capacity_phase_is_available_in_side_effect_free_dry_run` pin both CLI choices without running the thirteen- or thirty-minute phases. |

Additional named tests update existing assumptions for the seventh verb: `tests/p3/test_setup_boundary.py::test_verb_set_is_exactly_seven_design_verbs` and `tests/p3/test_setup_boundary.py::test_every_design_verb_is_accepted` cover all verbs while preserving legacy two-element vectors; `tests/p3/test_setup_service.py::test_literal_dispatch_table_has_exactly_seven_verbs`; `tests/p3/test_setup_client.py::test_existing_verbs_keep_legacy_vectors_and_period_verb_carries_payload`; and `tests/p3/test_p3_acceptance.py::test_helper_period_argument_is_not_observation_callback`. Period-specific boundary tests include `tests/p3/test_setup_boundary.py::test_period_argv_forms_are_checked_before_account_lookup`, `test_period_payload_range_table`, `test_dispatch_preserves_period_payload`, and `test_forged_period_invocation_without_payload_is_rejected`; `tests/p3/test_setup_client.py::test_period_payload_pairing_refuses_before_launch`, and `test_decode_body_validates_period_result_fields`; `tests/p3/test_setup_service.py::test_main_preserves_sample_period_out_of_range_reason_and_json` (exit 2, stderr preserves `sample_period_out_of_range`, and the decoded JSON has the exact result key set with `uid=None`, `sample_period=None`, and `sample_period_state=None`); and `tests/p3/test_collector_window.py::test_sampling_row_has_required_object_names`, `test_sampling_row_focus_and_spin_contract`, `test_sampling_edit_survives_refresh`, `test_sampling_status_strings_are_exact`, `test_sampling_apply_button_is_disabled_while_pending`, `test_sampling_mismatch_and_superseded_period_are_not_success`, `test_sampling_saved_status_is_shown_when_inactive` covers both a successful `applied_not_running` reply and a successful inactive `repair` reply with matching `sample_period`; in each case it asserts the saved string, retains the pending request until a later answering probe reports `sample_ns == pending_period_seconds * NS`, and never shows the numeric value as collector-reported before that probe; `tests/p3/test_collector_window.py::test_sampling_consent_discloses_exact_command`, `test_sampling_consent_cancellation_dispatches_nothing`, and `test_sampling_missing_helper_shows_bounded_stderr`.

## 12. Compatibility and migration

The drop-in is under `/etc` while the packaged unit remains under `/usr/lib`; a
package upgrade therefore overwrites neither. This is a file-layout guarantee,
not a precedence guarantee. The D7 merged-value verification reports an
administrator's conflicting drop-in, environment file, or unset directive.

The SQLite schema and persisted data format do not change. Rows retain measured
intervals through `interval_start_mono_ns` and `observed_ns`, so histories mixed
across periods remain representable. Rollups remain 60 seconds and 3600 seconds;
30 seconds remains the ceiling because it supplies two samples per 60-second
bucket.

A console built before this change rejects the new status key under its old exact
status validator and shows its existing unavailable state until the console is
restarted. It never displays a guessed period. A console built with this change
accepts both old status replies without `sample_ns` and new replies with it.

Existing lifecycle verbs leave the sample drop-in untouched. There is no uninstall
verb. Removing the package leaves the `/etc` drop-in inert with the unit removed.

## 13. Resolved implementation choices

- The active foreign-source result uses the design's explicit exit reason
  `sample_period_overridden`; its one-line operator diagnostic uses the design's
  `sample_period_not_in_effect` wording and includes the observed value and
  `DropInPaths`. Inactive verification failures, including a merged `Environment`
  mismatch, use `sample_period_unverifiable` and include the observed value,
  `EnvironmentFiles`, `UnsetEnvironment`, and `DropInPaths`.
- The helper keeps the old lifecycle `SHOW_ARGV`, adds a separate configuration
  query for D7, and leaves the GUI `UNIT_SHOW_ARGV` parser unchanged. This prevents
  a strict lifecycle parser from consuming environment properties or a GUI parser
  from silently accepting helper-only fields.
- The spin box is the requested value, while `ServiceFacts.sample_ns` and the
  status label are the effective value. Pending seconds are compared with
  nanoseconds only after multiplying by `NS`; refreshes cannot erase an edit.
- Canonical bytes are the sole authorship test. Empty files are replaceable,
  exact canonical files are ours, and every other non-empty byte sequence is
  foreign and remains byte-identical.
- `service_not_running` wins when this call began active and its final lifecycle
  read shows the unit inactive. A prior failed unit-wide `Result` alone is not
  transaction-specific evidence and does not block an inactive apply or retry;
  the pending/completed period journal determines whether this operation needs
  repair. The helper never starts the unit, and the existing switch remains the
  only start control.
- The optional status field distinguishes absence from an explicit JSON `null`: old replies without `sample_ns` remain compatible, while a present null is an invalid status value. Unknown status keys retain the `unknown_key` protocol reason; field-value faults retain `invalid_field`.
- Criterion 8 is executable through the isolated `period-capacity` host phase and
  documents the 226-app, 57,968-unit reserve needed to keep every 2,048-unit batch
  saturated for 30 minutes; the fixture does not require an above-quota database.
- The entry-point test pins that `sample_period_out_of_range` survives
  `setup_service.main` as both its exit-2 diagnostic and encoded result; it asserts
  the exact result key set and the pre-identity values `uid: null`,
  `sample_period: null`, and `sample_period_state: null`.
- A canonical target with no successful prior completion still runs the parent
  directory durability check. Matching bytes do not establish that a preceding
  rename and directory sync completed.
- The unit file gets no sample environment line. The default is produced by the
  collector when the process environment has no setting, and the helper owns the
  fixed `/etc` drop-in.
