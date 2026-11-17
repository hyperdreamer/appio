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
| `src/appio/setup_boundary.py` | Add `set-sample-period` to `VERBS`; add `period_seconds` to `Invocation`; extend `parse_invocation` and its range/shape errors; add the sample-period `FixedTarget`. |
| `src/appio/setup_files.py` | Add `SAMPLE_PERIOD_DROPIN`, its payload/parser pair, target membership, expected-content branch, and parent-directory durability after directory creation. |
| `src/appio/setup_service.py` | Add the two result fields, retain the existing lifecycle `SHOW_ARGV`, add the sample configuration show path and set-period handler, preserve payloads during dispatch revalidation, preserve syntax reasons, and implement the D7 state machine. |
| `src/appio/setup_client.py` | Add the period argument to `SetupClient.invoke`, validate its verb/payload pairing, append the third argv element only for the new verb, and validate `sample_period` and `sample_period_state` in `decode_body`. |
| `src/appio/collector_owner.py` | Add and validate `ServiceFacts.sample_ns`. Existing positional constructors remain valid because the field is appended with a `None` default. |
| `src/appio/ui/collector_window.py` | Add the sampling-period Settings row, consent text, pending/effective state handling, object names, status strings, and status-to-`ServiceFacts` pass-through. Keep `UNIT_SHOW_ARGV` and `parse_unit_facts` as the GUI service-state parser. |
| `data/libexec/appio-setup` | Add the new verb and the anchored three-argument shape check while retaining the stdlib-only trust order and usage prefix. |
| `data/systemd/appio-iod.service` | Preserve the unit file and its existing `APPIO_QUOTA_BYTES` deployment override. Do not add a unit-level sample environment assignment; the fixed `/etc` drop-in owns the setting. |
| `tests/p3/test_iod_cadence_pins.py` | Add parser, environment, derived-window, status, validator, and production-entry-point tests; preserve the existing default cadence pins. |
| `tests/p3/test_setup_boundary.py` | Add the six-verb-plus-period argv table, period range table, invocation payload assertions, and account-lookup ordering tests. |
| `tests/p3/test_setup_files.py` | Add canonical payload/parser, target allowlist, directory-fsync, foreign-content, empty-file, and byte-preservation tests. |
| `tests/p3/test_setup_service.py` | Add result fields and all D7 operation, failure, repair, inactive, precedence, and systemd-view tests. |
| `tests/p3/test_setup_client.py` | Add two-element/three-element launch vectors, payload pairing refusals, and new result-field decode tests. |
| `tests/p3/test_collector_window.py` | Add row construction, object names, focus, range, edit preservation, exact status, apply lifecycle, consent, mismatch, and no-start assertions. |

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
normalizes to no period. Any unknown key, boolean, float, negative value, zero, or
value above `30 * NS` is rejected through the existing `ProtocolError("invalid_field", ...)`
path. The daemon always sends the field.

The setup result document keeps all existing keys and adds these exact keys to
`_empty` and every encoded result:

```text
version: int == 1
verb: str
uid: int
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
`fsync`. A missing parent directory is created with the existing fixed directory
mode (`0o755` for this `/etc` path). Every directory created by this operation is
`fsync`ed through its parent immediately after creation. The operation must leave
existing target bytes unchanged when validation or a pre-rename write fails.

The target is ours when it is empty or exactly canonical for one valid period. A
non-empty noncanonical target is foreign and is never replaced.

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

The handler executes these steps in order:

1. Run the existing lifecycle show query before reading or writing the target and
   set `active_before = (ActiveState == "active")`.
2. Read the sample target through `SafeRootFS`; represent an absent target as
   `None`.
3. If existing bytes are non-empty and `parse_sample_period_dropin` returns
   `None`, fail with exit 2 and reason `foreign_drop_in`. The bounded diagnostic is
   `foreign_drop_in path=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf`.
   No target bytes may change.
4. Compute `needs_write = (existing_bytes != canonical_bytes)`.
5. Read the manager configuration using `SAMPLE_SHOW_ARGV`. Parse the one-line
   fields without shell expansion. `Environment` contains the configured value
   only when it has exactly one canonical token
   `APPIO_SAMPLE_SECONDS=<n>`; a missing, duplicate, malformed, or different
   token is a different configured value. `EnvironmentFiles` and
   `UnsetEnvironment` are retained verbatim for the verification diagnostic.
   Read the daemon status reply as a separate bounded status observation when the
   unit is active; its `sample_ns` divided by `NS` is the running value only when
   it is a positive exact multiple of `NS`.
6. If `needs_write`, atomically write the canonical bytes. If the target bytes are
   already canonical, `fsync` its parent directory and every ancestor directory
   created by this helper before continuing. This makes a prior rename followed
   by a directory-sync failure repairable and never treats the identical bytes as
   proof that the earlier operation completed.
7. Run `/usr/bin/systemctl daemon-reload` when `needs_write` is true or the
   configured value is not exactly `n`. A nonzero result is exit 1 with reason
   `daemon_reload_failed`; the already-written canonical file remains valid for a
   later repair.
8. Run `/usr/bin/systemctl try-restart appio-iod.service` only when
   `active_before` is true and at least one of `needs_write`, configured value not
   equal to `n`, running value different from `n`, or running value unknown is
   true. A nonzero result is exit 1 with reason `service_restart_failed`.
   `try-restart` is never used for an inactive unit and therefore cannot start or
   enable collection.
9. Verify the resulting state. Re-read the configuration view and require the
   merged `Environment` value to equal `n`. If the unit is active, require a
   validated daemon status with `answering is True` and
   `sample_ns == n * NS`. If the unit is inactive, require an empty
   `EnvironmentFiles` field and require `UnsetEnvironment` not to name
   `APPIO_SAMPLE_SECONDS`; an inactive unit cannot prove its runtime value.
10. Re-read `ActiveState` after all operation and verification steps as
    `active_after`, then select the outcome by the precedence below. A command or
    verification failure still performs this final read whenever the runner can
    obtain it.

For an active mismatch, the exit-1 result reason is
`sample_period_overridden`. Its exact bounded diagnostic is
`sample_period_not_in_effect observed=<value> DropInPaths=<paths>`, where
`<value>` is the observed integer seconds or `unknown`, and `<paths>` is the raw
`DropInPaths` field or `(none)`. This covers a winning other drop-in,
`EnvironmentFile=`, `UnsetEnvironment=`, or daemon value. For an inactive
`EnvironmentFiles` or `UnsetEnvironment` violation, the reason is
`sample_period_unverifiable` and the exact diagnostic is
`sample_period_unverifiable EnvironmentFiles=<files> UnsetEnvironment=<unset> DropInPaths=<paths>`.
A failed status observation while active uses the active reason with
`observed=unknown`. These diagnostics are single lines.

Outcome selection is ordered and exclusive:

| Precedence | Condition | Exit/result |
|---:|---|---|
| 1 | `active_before` is true and `active_after` is not active | Exit 1; `sample_period_state="service_not_running"`; reason `service_not_running`. This suppresses every other outcome, including a passing inactive verification. |
| 2 | Verification fails after the first row does not apply | Exit 1; `sample_period_state=None`; use the exact reason from steps 7 or 9. |
| 3 | Canonical bytes were written and `active_after` is active | Exit 0; `sample_period_state="applied"`. |
| 4 | Canonical bytes were written and the unit was inactive both before and after | Exit 0; `sample_period_state="applied_not_running"`. |
| 5 | No write occurred, a reload or restart occurred, and the unit is active | Exit 0; `sample_period_state="repair"`. |
| 6 | No write, reload, or restart occurred and verification passed | Exit 0; `sample_period_state="unchanged"`. |

A no-write repair that leaves the unit inactive cannot claim one of the five
successful meanings; it returns `sample_period_unverifiable` after the final
state read. This preserves the explicit meaning of `applied_not_running` and
`repair` and prevents an inactive operation from being reported as merely saved
when its configuration view was repaired without a file publication.

The result always carries `sample_period=n` for this verb. Existing lifecycle
verbs leave both period result fields null. A write, reload, or restart failure
is retriable; a retry evaluates the same state machine and can produce `repair`.
A successful identical re-apply produces `unchanged` only when no earlier
operation was left incomplete and no reload or restart is needed.

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
`reported_sample_ns == pending_period_seconds * NS`. A match clears the pending
request and shows the verified status. Any other reported period leaves the
pending request visible and shows the mismatch string. A helper result with
`sample_period_state="applied_not_running"` shows the saved string until a later
probe verifies a running value.

The exact status lines are:

| Situation | Status line |
|---|---|
| verified match | `Sampling every N s.` |
| collector not answering | `Sampling period not known while the collector is not answering.` |
| answering but `sample_ns` absent | `Sampling period not reported by this collector.` |
| applied while inactive | `Saved: sampling every N s once the system service starts.` |
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
| field absent / `None` | accepted as absent / null | accepted as `None` |
| `2 * NS` through `30 * NS` | accepted | accepted when represented as the exact positive integer |
| `True`, `False`, float, string, zero, negative, or above `30 * NS` | rejected | rejected for bool, zero, negative; positive integers remain valid facts |
| unknown status key | rejected | not applicable |

## 10. Exit codes and exact diagnostics

| Condition | Exit | Exact reason or diagnostic |
|---|---:|---|
| invalid arity, verb, executable, or payload shape | 2 | `invalid_verb_or_argument_count` at the boundary; shim usage for shim-only rejection |
| canonical period outside 2-30 | 2 | `sample_period_out_of_range` |
| non-empty foreign period drop-in | 2 | reason `foreign_drop_in`; diagnostic `foreign_drop_in path=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf` |
| target omitted from fixed allowlist | 2 | `target:not_fixed_path` |
| fixed target omitted from expected-content parser | 2 | `unexpected_content:not_a_config_target` |
| daemon reload command fails | 1 | `daemon_reload_failed` |
| try-restart command fails | 1 | `service_restart_failed` |
| active before, inactive after | 1 | `service_not_running` |
| active verification has a winning alternate value/source | 1 | reason `sample_period_overridden`; diagnostic begins `sample_period_not_in_effect observed=` |
| inactive verification has `EnvironmentFiles` or the sample variable in `UnsetEnvironment` | 1 | `sample_period_unverifiable` with the exact fields named in section 7 |
| write, rename, directory-sync, or underlying bounded command failure | 1 | existing underlying error is retained; the result is never a successful period state |
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

| Criterion | File, test name, assertion, and fixture |
|---:|---|
| 1 | `tests/p3/test_iod_cadence_pins.py::test_raw_persistence_follows_the_documented_ten_second_cadence` keeps its 15 two-second ticks and existing sequence/total assertions; `tests/p3/test_iod_cadence_pins.py::test_production_main_uses_ten_second_default_when_environment_is_absent` stubs the daemon entry point, removes the setting, and asserts `sample_ns=10*NS`, `flush_ns=30*NS`, and `maintenance_ns=30*NS`; `tests/p3/test_iod_cadence_pins.py::test_main_empty_environment_starts_with_default_period` runs the production startup seam and asserts no invalid warning and successful start. |
| 2 | `tests/p3/test_iod_cadence_pins.py::test_production_main_passes_valid_period_and_derived_windows` parameterizes 2 and 30 and asserts the three constructor values; `tests/p3/test_iod_cadence_pins.py::test_status_reports_constructor_sample_ns` constructs `Daemon(sample_ns=2*NS)` and asserts the status field; `tests/p3/test_iod_cadence_pins.py::test_flush_ns_for_uses_three_periods_at_two_ten_eleven_and_thirty` asserts 30, 30, 33, and 90 seconds; root acceptance `test_two_second_and_thirty_second_runs_match_status_and_intervals` uses committed rows and the status reply to assert approximately requested spacing. |
| 3 | `tests/p3/test_iod_cadence_pins.py::test_sampling_overdue_gate_uses_derived_flush_window` drives the sampling-duration gate below and above `flush_ns_for(30*NS)` with a controlled clock; `tests/p3/test_iod_cadence_pins.py::test_step_overdue_gate_uses_derived_flush_window` drives the pre-sample gate with the same clock; `tests/p3/test_iod_cadence_pins.py::test_shutdown_recovery_gate_uses_derived_flush_window` drives `stop()` below and above the threshold and asserts recovery/epoch effects; root acceptance `test_thirty_second_ten_minute_run_has_no_overdue_paths` runs ten minutes and asserts none of the three overdue effects. |
| 4 | `tests/p3/test_iod_cadence_pins.py::test_parse_sample_seconds_acceptance_table` uses the literal accepted values in section 9.1 and asserts integers; `tests/p3/test_iod_cadence_pins.py::test_parse_sample_seconds_refusal_table` uses all named invalid values and asserts `None`; `tests/p3/test_iod_cadence_pins.py::test_configured_sample_ns_invalid_values_warn_once_and_start` parametrizes `1`, `31`, `abc`, `02`, ` 5`, a control character, and a 4,000-character value, asserts 10 seconds, exactly one log record, and completed startup; `tests/p3/test_iod_cadence_pins.py::test_status_validator_accepts_period_and_older_status` covers present and absent `sample_ns`. |
| 5 | `tests/p3/test_setup_service.py::test_set_sample_period_inactive_writes_and_never_starts` uses a fake inactive unit and records no `enable`, `start`, or `try-restart`; `tests/p3/test_setup_service.py::test_set_sample_period_active_uses_try_restart` uses an active unit and asserts the exact reload/restart argv; `tests/p3/test_collector_window.py::test_sampling_apply_preserves_single_switch_activation_invariant` uses a fake setup client and asserts an inactive service is never started; root acceptance `test_apply_two_and_thirty_seconds_restarts_only_active_service` checks the service state and interval after each apply. |
| 6 | `tests/p3/test_setup_service.py::test_identical_successful_apply_is_unchanged` preloads canonical bytes and matching configuration/status and asserts no write, reload, or restart plus `unchanged`; `test_identical_apply_repairs_failed_reload`, `test_identical_apply_repairs_failed_restart`, and `test_identical_apply_repairs_failed_directory_sync` inject each failure, re-arm the directory-sync fault on every attempt, retry, and assert `repair`; `test_active_before_inactive_after_has_service_not_running_precedence` forces the final state down and asserts exit 1 and `service_not_running`; `tests/p3/test_collector_window.py::test_sampling_apply_success_waits_for_matching_nanosecond_probe` uses a real `N*NS` value and asserts success only on equality. |
| 7 | `tests/p3/test_setup_files.py::test_sample_period_payload_and_parser_round_trip` covers 2, 10, and 30 exact bytes; `test_sample_period_target_is_in_both_allowlists` asserts both target tables; `test_missing_config_target_and_missing_parser_have_distinct_refusals` asserts `target:not_fixed_path` and `unexpected_content:not_a_config_target`; `test_foreign_sample_dropin_preserves_bytes` covers every refusal form; `test_empty_sample_dropin_is_replaced` covers the empty exception; `tests/p3/test_setup_service.py::test_active_override_reports_value_and_dropin_paths` and `test_inactive_environment_sources_are_unverifiable` cover both verification branches. |
| 8 | root acceptance `test_two_second_capacity_run_retires_aged_units_ahead_of_production` pre-populates at least two days of expired raw rows for 200 or more applications, runs at least 30 minutes of wall clock, counts eligible backlog before and after, computes `retired / measured_elapsed`, asserts retirement throughput exceeds production throughput, and asserts the eligible backlog does not grow. The fixture must contain materialized aged rows; an empty or below-quota store is invalid. |

Additional named seam tests are required for `tests/p3/test_setup_boundary.py::test_period_argv_forms_are_checked_before_account_lookup`, `test_period_payload_range_table`, `test_dispatch_preserves_period_payload`, and `test_forged_period_invocation_without_payload_is_rejected`; `tests/p3/test_setup_client.py::test_old_and_period_launch_vectors`, `test_period_payload_pairing_refuses_before_launch`, and `test_decode_body_validates_period_result_fields`; and `tests/p3/test_collector_window.py::test_sampling_row_has_required_object_names`, `test_sampling_row_focus_and_spin_contract`, `test_sampling_edit_survives_refresh`, `test_sampling_status_strings_are_exact`, `test_sampling_apply_button_is_disabled_while_pending`, `test_sampling_mismatch_and_superseded_period_are_not_success`, `test_sampling_saved_status_is_shown_when_inactive`, `test_sampling_consent_discloses_exact_command`, `test_sampling_consent_cancellation_dispatches_nothing`, and `test_sampling_missing_helper_shows_bounded_stderr`.

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
  `DropInPaths`. Inactive source ambiguity uses `sample_period_unverifiable`.
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
- `service_not_running` is selected before verification and all four successful
  states. A restart race therefore cannot be reported as `repair`, `applied`, or
  a passing inactive result.
- A canonical target with no successful prior completion still runs the parent
  directory durability check. Matching bytes do not establish that a preceding
  rename and directory sync completed.
- The unit file gets no sample environment line. The default is produced by the
  collector when the process environment has no setting, and the helper owns the
  fixed `/etc` drop-in.
