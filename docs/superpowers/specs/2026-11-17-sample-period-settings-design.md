# Adjustable sample period — design

- **Topic:** `sample-period-settings`
- **Run:** `pm-run-20261117-153438-045b6e15`
- **Date:** 2026-11-17
- **Status:** approved by the operator (design proposal and UI mockup choice A)

## 1. Background

The collector measures application I/O in fixed windows and commits each window to
SQLite. Today that period is a module constant:

| Fact | Where |
| --- | --- |
| `SAMPLE_NS = 10 * NS` — the measurement period | `src/appio/iod.py:42` |
| `MAX_FLUSH_NS = 30 * NS` — the commit ("overdue") window | `src/appio/iod.py:44` |
| `Daemon(..., sample_ns=SAMPLE_NS, flush_ns=MAX_FLUSH_NS)` | `src/appio/iod.py:263-271` |
| `flush_ns` must be strictly greater than `sample_ns`, enforced in the constructor | `src/appio/iod.py:267-270` |
| `_flush_ns` drives the `collection_overdue` self-alarm | `src/appio/iod.py:824`, `:853`, `:967` |
| The shipped cadence is pinned by regression tests on production defaults | `tests/p3/test_iod_cadence_pins.py:16-33` |
| A deployment tunable already lives in the unit as `Environment=` | `data/systemd/appio-iod.service` (`APPIO_QUOTA_BYTES`) |
| A malformed environment override falls back to the default rather than failing | `src/appio/quota.py:73-91` |
| The Settings dialog has exactly one row (`ROWS = ("system_services",)`), one privileged switch, a probed status label, and "Check activation" | `src/appio/ui/collector_window.py:408-455` |
| Privileged changes go through `/usr/bin/pkexec /usr/bin/appio-setup <verb>` with a fixed six-verb set and no argument-carrying verb | `src/appio/setup_boundary.py:88-98`, `:284-319` |
| Two fixed configuration targets exist, both written atomically under an `O_NOFOLLOW` component walk | `src/appio/setup_boundary.py:206-227`, `src/appio/setup_files.py:64-67` |
| The collector's request vocabulary is read-only (`status`, `subscribe`, `query`) | `src/appio/ipc_models.py:77` |
| The status reply is validated against an exact key set | `src/appio/ipc_models.py:603-630`, `src/appio/iod.py:531-536` |
| Raw history is kept 1 day, 60 s rollups 30 days, hourly rollups 365 days; the store is capped (1.5 GB code default, 8 GB on the packaged unit) | `src/appio/quota.py:24-49` |
| Note: `appio --interval` is the legacy console's live *refresh* interval, unrelated to this setting | `src/appio/__main__.py:33-37` |

So the measurement period is neither adjustable nor reported: the console cannot see
it, and an operator who wants finer or coarser measurement has to patch a constant.
A 1-day raw window and a size-capped store make the period a real product trade-off,
not an internal detail: it decides burst resolution against how far back the raw
window actually reaches.

## 2. Goal

One machine-wide setting, changed from the application's Settings dialog, that sets
how often the collector measures application I/O.

Success criteria, each observable:

1. With no setting, the collector commits at 10 s exactly as today.
2. Setting 2 s produces committed intervals of about 2 s; setting 30 s produces about
   30 s; the collector's own status reply reports the value in use.
3. Over ten minutes at 30 s, the collector never reports `collection_overdue` from a
   normal cadence.
4. `1`, `31`, `abc`, `02`, and `" 5"` leave the collector at 10 s and log one line;
   the collector never refuses to start because of this setting.
5. Applying a value while collection is off writes the configuration and does **not**
   start or enable collection.
6. Applying the same value twice writes nothing the second time and does not restart
   the collector.
7. A drop-in an administrator wrote is refused with a distinct message and left
   byte-identical.

## 3. Non-goals

- Changing the period without a restart.
- Per-user or per-window periods.
- Retention, quota, rollup-granularity, or schema changes.
- Any change to the read-only request vocabulary (`status`, `subscribe`, `query`).
- Changing the legacy console's `--interval` refresh behaviour.

## 4. Decisions

**D1 — The setting is an integer number of seconds, 2-30, in `APPIO_SAMPLE_SECONDS`.**
Seconds because the helper, the drop-in, the dialog, and the log line all speak
seconds; the collector converts once at startup. Integer because the dialog's control
is an integer spin box and a fractional period buys nothing an integer does not.

**D2 — An invalid value is ignored, never clamped.** Absent or empty means 10 s.
Non-canonical decimal (`02`, `+5`, `" 5"`, `5.0`) or an out-of-range value (`1`, `31`)
means 10 s plus one `sample_period_invalid value=<raw>` warning at startup. The
collector never fails to start over this setting, matching `configured_quota`
(`src/appio/quota.py:73-91`): a typo in a unit file must not stop measurement.
Clamping is rejected because it would run at a period the operator did not choose and
cannot see. The environment is read **only** in `Daemon.main()`, never in
`Daemon.__init__`'s default arguments: embedders, the fast test runtimes, and
`tests/p3/test_iod_cadence_pins.py` keep deterministic 10 s behaviour no matter what
the process environment contains.

**D3 — The commit window becomes `max(30 s, 3 x period)`.** `flush_ns` must exceed
`sample_ns` (`src/appio/iod.py:269-270`) and drives the `collection_overdue`
self-alarm. A fixed 30 s window would make a legitimate 30 s cadence look overdue on
the first scheduling jitter. Three periods keeps the alarm meaningful (it fires only
after three missed cadences) and leaves 2-10 s periods exactly as forgiving as today.

**D4 — The collector reports the period it is using.** The status reply gains
`sample_ns` (positive integer, nanosecond period). The reply's key set is exact, so
the field is added as *optional* in `ipc_models._status`: required keys unchanged,
allowed keys plus `sample_ns`. An already-running older console therefore keeps
working against a restarted newer daemon, and the new console treats the field's
absence as "not reported", never as 10 s.

**D5 — Persistence is a third fixed helper target:**
`/etc/systemd/system/appio-iod.service.d/20-sample-period.conf`, canonical bytes:

```
[Service]
Environment=APPIO_SAMPLE_SECONDS=<n>
```

`<n>` is canonical decimal (no leading zeros) and the file ends with exactly one LF.
It is a *drop-in next to the packaged unit*, not an edit of it, so a package upgrade
never conflicts with it, and `APPIO_QUOTA_BYTES` in the packaged unit is unaffected
(`Environment=` entries merge across drop-ins).

**D6 — One new helper verb, `set-sample-period <seconds>`.** This is the first
argument-carrying verb in a boundary whose documented rule is "argv is exactly one
verb". That rule is extended *only* as far as a single validated integer:

- `len(argv)` must be 3 for `set-sample-period` and 2 for every other verb; anything
  else exits 2 `invalid_verb_or_argument_count`;
- the payload must match `^(?:[2-9]|[12][0-9]|30)$`; a canonical integer outside
  2-30 exits 2 `sample_period_out_of_range`; any other shape exits 2
  `invalid_verb_or_argument_count`.

The payload can never name a path, a command, a unit, or an environment key, so the
boundary's actual prohibition ("no path or command selectors") is preserved. No
value file, no stdin, no shell.

**D7 — Applying means: write, reload, restart only if it was running.** Order:
refuse a foreign drop-in (D9); write canonical bytes atomically through the existing
`safe` target machinery (`mkstemp` + `write` + `fsync` + `fchown` + `rename` +
parent `fsync`); if the bytes were already identical, stop here with exit 0 and
`unchanged`; else run `systemctl daemon-reload` (exit 1 `daemon_reload_failed` on
failure); then, only when the unit is currently active, `systemctl restart
appio-iod.service` (exit 1 `service_restart_failed` on failure, exact stderr in the
helper's output). An inactive or disabled unit is never started or enabled by this
verb: changing a period is not a request to collect.

**D8 — The dialog shows the collector's value, not the file's.** A new Settings row
(`ROWS` gains `"sampling_period"`), reached by deep link like the existing row:

```
Sampling period   [ 10 ] seconds  (2-30)   [Apply]
Currently sampling every 10 s
```

The status line renders the value the collector reports. When the collector is not
answering, or does not report the field, the line says exactly
`Sampling period not known while the collector is not answering.` — never the last
value the console wrote. Apply runs `/usr/bin/pkexec /usr/bin/appio-setup
set-sample-period <n>` through the existing bounded runner, disables the row while it
runs, then re-uses the existing probe: the row ends in one of applied and answering,
applied but not answering, or failed with the helper's exact last line. A console may
not display a success it has not observed.

**D9 — A foreign drop-in is refused, not replaced.** If
`20-sample-period.conf` exists and its bytes are not canonical-for-some-valid-`n`,
the verb exits 2 `foreign_drop_in`, names the path, and leaves the file byte-identical.
This mirrors `parse_service_dropin`'s "an administrator's unrelated file is refused
rather than replaced" rule (`src/appio/setup_files.py:668-680`).

**D10 — Lifecycle.** `disable` and `install-delegation` leave the drop-in untouched:
the period is configuration, not activation, and reinstalling delegation must not
reset an operator's period. There is no uninstall verb today, so nothing else
changes; removing the package removes the unit, and the drop-in becomes inert.

**D11 — The switch stays the only control that changes whether collection runs.** The
existing test `tests/p3/test_collector_window.py::test_the_switch_is_the_only_root_control_and_sits_inside_settings`
asserts a premise this feature narrows. It is rewritten to assert the true rule — the
switch is the only control that starts or stops collection, and it sits inside
Settings — plus a new assertion that the period row cannot start collection.

## 5. Architecture

Three small units, each with one job, joined only by the drop-in file and the status
reply.

### 5.1 Collector (`src/appio/iod.py`, beside the cadence constants)

- `configured_sample_ns(environ=None) -> int`: absent/empty -> `SAMPLE_NS`; canonical
  2-30 -> that many seconds in ns; anything else -> `SAMPLE_NS` and one
  `sample_period_invalid` warning naming the raw value. Pure, no I/O, takes the
  mapping explicitly so tests never touch the process environment.
- `flush_ns_for(sample_ns) -> int`: `max(MAX_FLUSH_NS, 3 * sample_ns)`. Pure.
- `Daemon.main()` builds the pair once and passes them to `Daemon(...)`; the existing
  constructor validation (positive int; `flush_ns > sample_ns`) is unchanged and
  remains the last line of defence.
- `Daemon._status()` adds `sample_ns: int` from the value it was constructed with, so
  the advertised value is the value in use, not a re-read of the environment.
- The legacy CLI path (`appio --interval`) is untouched.

### 5.2 Privileged helper (`src/appio/setup_boundary.py`, `setup_files.py`,
`setup_service.py`)

- `VERBS` gains `set-sample-period`; `Invocation` gains an optional validated payload
  (`period_seconds: int | None`) so handlers never re-parse argv.
- `FIXED_TARGETS` gains a third entry, key `sample-period-dropin`, path as in D5. Its
  description: "packaged collector's sample-period override".
- `sample_period_payload(seconds: int) -> bytes` and
  `parse_sample_period_dropin(data: bytes) -> int | None` ("not ours" -> `None`) live
  beside the existing payload/parse pairs, in the same canonical-bytes style.
- `setup_service` gains the handler: parse the target (D9), compare bytes (D7), write,
  reload, conditional restart, and print a one-line result
  (`sample_period=<n> applied|unchanged|applied-not-running`). Command results use the
  existing bounded runner: absolute argv, minimal `BASE_ENV`, finite timeout.
- Polkit policy is unchanged: one action, one executable; the new verb is another
  argument shape of the same authorized program.

### 5.3 Console (`src/appio/ui/collector_window.py`)

- `SettingsDialog.ROWS` gains `"sampling_period"`; `focus_row` moves focus to the
  period spin box for that key. Object names: `sample_period_row`,
  `sample_period_spin`, `sample_period_apply`, `sample_period_status`.
- The spin box is `QSpinBox` with `setRange(2, 30)`, `setSuffix(" s")`, and
  `setKeyboardTracking(False)` so typing does not fire intermediate applies.
- Apply uses the existing consent/probe machinery (`_launch_helper` and the
  ticket/generation probe payload) rather than a new privileged path. The row is
  disabled while a helper run is in flight, and the button is disabled when the
  helper is unavailable (`appio-setup is not available`, the existing reason string).
- `open_settings(focus="sampling_period")` works like the existing deep links; the
  Files page's existing deep link keeps pointing at `system_services`.

### 5.4 Mockup

Option A — two peer rows, the period row a sibling of the root switch, no grouping:

![Chosen Settings layout](assets/2026-11-17-sample-period-settings-mockup.png)

## 6. Failure and edge cases

| Situation | Observable behaviour |
| --- | --- |
| Env absent | 10 s, no log line |
| Env empty string | 10 s, no log line (treated as absent) |
| Env non-canonical (`02`, `+5`, `5.0`, `" 5"`, `abc`) | 10 s, one `sample_period_invalid` warning |
| Env canonical but outside 2-30 (`1`, `31`, `0`, `999999`) | 10 s, one `sample_period_invalid` warning |
| Env valid, unit inactive | Collector not running; value applies on next start |
| Helper called with no argument, or two | exit 2 `invalid_verb_or_argument_count` |
| Helper called with `1`, `31`, `abc`, `02` | exit 2 (`sample_period_out_of_range` for canonical out-of-range, otherwise `invalid_verb_or_argument_count`) |
| Drop-in present but not ours | exit 2 `foreign_drop_in`, file untouched |
| Drop-in present, canonical, same value | exit 0, `unchanged`, no write, no reload, no restart |
| Drop-in write succeeds, `daemon-reload` fails | exit 1 `daemon_reload_failed`; file already written; message names the path |
| Restart fails | exit 1 `service_restart_failed`; collector may be down; console shows "applied but not answering" after the probe |
| Restart during collection | Collector commits its last interval on SIGTERM (`iod.py:963-971`); the restart appears as a real gap or partial interval; nothing is backfilled |
| Collector not answering in the console | Status line says the period is not known; Apply stays enabled (it is the repair path) |
| Collector replies without `sample_ns` (older daemon) | Status line says the period is not reported; Apply still available |
| Disk full during drop-in write | exit 1 with the underlying error; no partial file (atomic write) |
| Two consoles applying at once | The helper lock serialises them; the later one sees the earlier bytes and reports `unchanged` or applies its own value |

## 7. Compatibility

- Package upgrade: the drop-in lives under `/etc`, the unit under `/usr/lib`; no
  conflict, and no reinstallation step.
- Console launched before the daemon restart: it receives the extra status field from
  a newer daemon only if the validator allows it, which D4 does; an older console
  bound to an newer daemon is the case the optional field exists for.
- The collector's persisted data format is unchanged: intervals are already measured,
  not assumed, so a mixed history of 10 s and 2 s intervals is already representable
  (`interval_start_mono_ns`, `observed_ns`).
- Rollups bucket at 60 s and 3600 s. A 30 s period yields two samples per 60 s bucket,
  which is why 30 is the ceiling.

## 8. Test strategy

**Unit — collector** (`tests/p3/test_iod.py` or a new focused module):
`configured_sample_ns` for absent, empty, each canonical value at the bounds, and at
least six malformed shapes; `flush_ns_for` at 2, 10, 11, and 30 s; `Daemon` status
reply includes `sample_ns` equal to the constructor value; the status validator
accepts the field when present and when absent. `tests/p3/test_iod_cadence_pins.py`
must pass **unchanged** — it is the proof that the default path still commits exactly
three raw observations per six responsiveness ticks and keeps its (600, 300, 180,
120) totals.

**Unit — helper** (`tests/p3/test_setup_service.py`, `tests/p3/test_setup_boundary.py`):
argv arity for both the new verb and an existing one; the payload regex accept/refuse
table (far more refusals than acceptances); canonical payload bytes; parse round-trip;
foreign-file refusal leaves bytes identical; identical-bytes short circuit performs no
reload and no restart; reload failure and restart failure produce their exact codes;
restart is skipped when the unit is inactive (asserted through the injected command
runner, never by touching the host).

**UI** (`tests/p3/test_collector_window.py`): the row exists with the four object
names; the spin box range and suffix; deep link focuses it; status renders the
collector's value when reported and the exact unknown sentence when not; Apply is
disabled while in flight and when the helper is unavailable; success is not shown
before a probe result; the rewritten switch test from D11.

**Acceptance (root, `tools/acceptance/`):** set 2 s and 30 s through the helper,
observe committed interval spacing close to the requested period, confirm the status
reply reports the same value, confirm the restart gap is recorded as a gap or partial
interval rather than fabricated, and restore 10 s at the end.

## 9. Risks

- **Privileged surface.** Adding an argument-carrying verb is the highest-risk part.
  Mitigated by a single anchored regex, an explicit range, refusal-by-default, and a
  refusal-table test suite far larger than the acceptance table.
- **Short periods shrink the raw window.** At 2 s the row count grows about 5x and the
  size cap deletes the oldest rows first, so "1 day of raw history" is a best case at
  10 s. The dialog's hint and the docs must say this plainly; the setting does not
  promise a fixed raw window.
- **Restart gaps.** Every apply costs one short collection gap. Choosable, visible in
  history, never silent.
- **Administrator drift.** A hand-written drop-in wins the file and the dialog shows
  the collector's actual value, so drift is visible rather than masked. The helper
  refuses to overwrite it.

## 10. Resolved questions

No open design questions remain. The operator approved the design, the 2-30 s range
with a 10 s default, and mockup A.
