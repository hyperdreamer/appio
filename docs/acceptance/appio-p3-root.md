# P3 opt-in host acceptance

This procedure is for an expendable Linux login on a systemd/cgroup-v2 host.
The ordinary pytest suite uses fixture proc/cgroup trees, virtual time and
injected credentials. It never runs this procedure, polkit, a real manager,
BPF or a host sampler. P5 packaging/installers remain deferred; the manual
staging below exists only to exercise reviewed P3 runtime artifacts.

**Current host evidence: UNRUN.** Only the rootless tests and the script's
non-mutating dry run have been exercised in the acceptance lane. A simulated
16 MiB probe is not evidence that this machine accounts block I/O.

## Prerequisites and installation

Use a disposable account with an expendable desktop session, a working polkit
agent, systemd as PID 1, cgroup v2 with `io` available, and Python >=3.14 with
SQLite >=3.37 and JSON support. The GUI needs PyQt6 >=6.6. Install the matching
Python/PyQt6 through the host's normal administration procedure first. The
probe target under `/var/lib/appio/probes/<uid>/probe.bin` must be backed by a
supported block filesystem (currently ext2/ext3/ext4/xfs/f2fs). tmpfs, overlay
and btrfs cannot close D2 under the implemented probe contract.

Review the source before copying it into a trusted runtime. Do not point a root
launcher at a checkout, home-owned venv, editable install, `PYTHONPATH`, or nvm
interpreter. `/usr/bin/appio-setup` starts `/usr/bin/python -I` and verifies the
interpreter, appio module closure and every ancestor are root-owned and not
writable by group/other before importing privileged code. The installed module
tree must contain regular files; do not use symlinks into the checkout.

From a reviewed checkout, these are the installation destinations and modes:

| Source | Destination | Owner / mode |
| --- | --- | --- |
| `src/appio/` | `/usr/lib/python3.14/site-packages/appio/` on a matching Arch runtime; otherwise the system interpreter's `purelib` below | root:root, directories 0755, files 0644 |
| daemon launcher below | `/usr/bin/appio-iod` | root:root 0755 |
| GUI launcher below | `/usr/bin/appio` | root:root 0755 |
| `data/libexec/appio-setup` | `/usr/bin/appio-setup` | root:root 0755 |
| `data/systemd/appio-iod.service` | `/etc/systemd/system/appio-iod.service` | root:root 0644 |
| `data/polkit/io.github.appio.setup.policy` | `/usr/share/polkit-1/actions/io.github.appio.setup.policy` | root:root 0644 |

The commands below are manual administrator operations, outside the default
script and pytest. Use them only on the selected test host, with no pre-existing
appio installation to overwrite. Resolve `purelib` with the actual trusted
system interpreter; it must not resolve into a home directory.

```sh
/usr/bin/python -I -c 'import sqlite3, sys, sysconfig; print(sys.version); print(sqlite3.sqlite_version); print(sysconfig.get_path("purelib"))'
appio_site=$(/usr/bin/python -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
sudo /usr/bin/groupadd --system appio  # omit only if getent group appio already succeeds
sudo /usr/bin/install -d -o root -g root -m 0755 "$appio_site/appio"
sudo /usr/bin/cp -R src/appio/. "$appio_site/appio/"
sudo /usr/bin/chown -R root:root "$appio_site/appio"
sudo /usr/bin/find "$appio_site/appio" -type d -exec chmod 0755 '{}' +
sudo /usr/bin/find "$appio_site/appio" -type f -exec chmod 0644 '{}' +
sudo /usr/bin/install -o root -g root -m 0755 data/libexec/appio-setup /usr/bin/appio-setup
sudo /usr/bin/install -o root -g root -m 0644 data/systemd/appio-iod.service /etc/systemd/system/appio-iod.service
sudo /usr/bin/install -o root -g root -m 0644 data/polkit/io.github.appio.setup.policy /usr/share/polkit-1/actions/io.github.appio.setup.policy
```

Create the two fixed launchers in a local staging directory, then install them:

```sh
appio_stage=$(mktemp -d)
cat > "$appio_stage/appio-iod" <<'PY'
#!/usr/bin/python -I
from appio.iod import main
raise SystemExit(main())
PY
cat > "$appio_stage/appio" <<'PY'
#!/usr/bin/python -I
from appio.__main__ import main
raise SystemExit(main())
PY
sudo /usr/bin/install -o root -g root -m 0755 "$appio_stage/appio-iod" /usr/bin/appio-iod
sudo /usr/bin/install -o root -g root -m 0755 "$appio_stage/appio" /usr/bin/appio
sudo /usr/bin/systemctl daemon-reload
sudo /usr/bin/systemctl disable --now appio-iod.service
/usr/bin/systemctl show appio-iod.service --property=UnitFileState,ActiveState
```

Require `UnitFileState=disabled` and `ActiveState=inactive` before continuing.
Starting the unit is not part of installation. Do not pre-install delegation
or add the account to appio; acceptance exercises those consented operations.
The service uses root:appio, UMask 0077, state directory `/var/lib/appio` 0711,
root-owned history, runtime directory `/run/appio` root:appio 0750 and socket
`/run/appio/iod.sock` root:appio 0660. The state database and sidecars are
root-owned 0600. The daemon owns collection; no GUI owns the system service.

The helper alone writes the root-owned 0644 fixed drop-ins:

- `/etc/systemd/system/user@.service.d/10-appio-io.conf`
- `/etc/systemd/user.conf.d/10-appio.conf`

Its lock is `/run/appio-setup/helper.lock`, pending activation state is
`/var/lib/appio/setup-state.json`, and probe parents are root-owned,
non-writable, traversable directories. The probe file is pre-created for the
beneficiary as 0600. Exports are caller-owned 0600 files in the account's home.
The policy grants the single `io.github.appio.setup` action with `auth_admin`
and binds it to `/usr/bin/appio-setup`.

## Dry run and evidence

```sh
/usr/bin/python -I tools/acceptance/appio-p3-root.py
```

This prints fixed proposed operations and exits without importing appio,
reading host state, creating evidence files or running a command. All host
checks remain UNRUN. `--run` authorizes one named phase; the helper still
receives exactly one of its six verbs. Script options are never helper options.
Do not run this script with sudo. `pkexec` must derive the beneficiary from the
real non-root login. Real-phase results, helper JSON and separate manual
observations go to the NSS home at
`~/.local/state/appio/p3-acceptance.json` (0600). A failed or unobserved check is
recorded as incomplete; it never becomes a pass by inference.

## Host sequence

1. **Disabled service and fallback.** Run
   `/usr/bin/python -I tools/acceptance/appio-p3-root.py --run --phase enable`.
   When prompted, open two `/usr/bin/appio --db <same-local-db>` windows. Check
   the off/absent banner, one fallback writer and a second viewer. Close both.
   The script records the disabled/inactive state, original account groups and
   user-manager identity. It invokes exactly:
   `/usr/bin/pkexec /usr/bin/appio-setup install-delegation` and then
   `/usr/bin/pkexec /usr/bin/appio-setup enable`. Check the actual polkit consent;
   a cached authorization can suppress the dialog and should be recorded as
   such rather than claimed as a new prompt. Existing groups must survive
   appending appio. The GUI's equivalent switch exposes consent, then both
   “Restart my user manager now” and “Activate after reboot” choices.
2. **Non-destructive default choice.** The script selects activate-after-reboot
   and compares the manager PID/start time/boot before and after enable. It
   runs no manager restart and never reboots. Record pending activation as
   pending. Linger means logout/login alone may leave the same manager alive;
   merely seeing `io` in `cgroup.controllers` is insufficient.
3. **Fresh manager and measured D2 probe.** Reboot manually at a convenient
   point and enter a fresh appio-group login. Then run
   `/usr/bin/python -I tools/acceptance/appio-p3-root.py --run --phase verify`.
   This requires a different manager identity and current process group
   membership, then authorizes the fixed helper `status` verb. While the real
   helper runs, the script observes its new transient unit's `io.stat`, probe
   device and target filesystem. Require 16,777,216 expected write bytes,
   actual writes within 5% by integer arithmetic, `pending_activation=false`,
   and removal of the unit and probe file. The helper envelope omits detailed
   `ProbeEvidence`; therefore the script must observe the unit itself. If the
   short-lived probe is missed or unreadable, it records incomplete: rerun
   verify or retain external observations. Never claim controller presence or
   a mocked result as measured bytes.
4. **Verified daemon authority.** The script checks root:appio 0750/0660 paths
   and real kernel `SO_PEERCRED` before decoding any status/history. In the
   GUI check `on, answering`, the single System services switch, only iod
   frames while authoritative and visible pending/probe failures. Enabling
   while a GUI collector existed uses D3 fresh baselines; partial coverage is
   expected. It does not upload GUI counters. A separately tested exact
   in-process/export-backed handover is a different case.
5. **Collection without a GUI.** Close every GUI at the script prompt. The
   script records a committed snapshot, waits 6.2 seconds, reconnects with
   its boot/epoch/seq cursor and requires a later iod observation. It queries
   the historical interval while no GUI was subscribed, following every
   continuation cursor. Reopen the GUI and inspect that interval. Record an
   explicit replay/gap and unavailable values when evidence is absent. Do not
   substitute zero for a missed interval or claim wall time as observed I/O.
6. **Disable, final WAL export, fallback.** Close the GUI and run
   `/usr/bin/python -I tools/acceptance/appio-p3-root.py --run --phase disable`.
   Disable is its own fixed helper/polkit operation. Require disabled/inactive
   proof, preserved drop-ins/groups, a caller-owned regular 0600 export, an
   intact schema and a checkpoint matching the returned cutoff at least as
   new as the last observed cursor. A second `export-history` after stop must
   match **all** final schema-v1 tables in primary-key order and use a new
   filename; this also proves root history remains available. The rootless
   test separately leaves rows in WAL and adds final counters before stop,
   then compares every exported row with the daemon database. Reopen the GUI:
   only after inactive proof and acquiring the lease may local collection
   resume. Record actual transition gaps/partial coverage.
7. **Optional session-ending branch.** Only on the expendable login, after
   enable, explicitly select both flags:
   `/usr/bin/python -I tools/acceptance/appio-p3-root.py --run --phase restart-user-manager --allow-session-termination`.
   **This terminates the user manager and ends the session, including the
   script and GUI.** Intent is recorded before the single fixed helper verb.
   Without the destructive flag, argument validation rejects execution before
   any side effect. On a new login, run verify; successful restart alone does
   not clear pending activation. There is no automatic reboot branch.

## Rulings and limits carried into acceptance

| Decision | Implemented contract / remaining limit |
| --- | --- |
| D1 | Read-only `status`, `subscribe`, `query`; typed selectors, no SQL/path/control commands. This follows design §4.2/N2; inconsistent §5 prose remains a design correction to ratify. |
| D2 | Fixed block-backed 16 MiB probe with 5% integer tolerance; unsupported/unobserved targets remain inactive/unknown. Real host evidence is UNRUN here. |
| D3 | Exact handover only with a validated checkpoint and complete preceding ownership chain. Live GUI-to-root enable deliberately starts fresh, with explicit incomplete coverage. No trusted upload/ack protocol was added. |
| D4 | 1 MiB inbound, 256 KiB outbound, 512 KiB/client queue, 64 clients, 500-row pages, 4000 series points, 200 ms cooperative query deadline, 2 s partial-frame deadline. No claim of hard Python preemption. |
| D5 | appio-group readers see machine-wide root history; an authorized administrator may stop the shared daemon. Exports preserve that same global dataset. GUI fallback uses one per-UID lease. |

## Acceptance-lane observations

The rootless fixture round trip renders task 80 B (read 48 B, write 32 B),
device attribution 136 B and signed residual -56 B, satisfying `136 - 56 = 80`.
A daemon crash/restart preserves total counters `(120,60,36,24)` in
`(rchar,wchar,read_bytes,write_bytes)` order and records the explicit 14–18 s
gap. Crossing the 24-hour raw/rollup selection boundary preserves task 600 B.
The final-close lifecycle export matches every schema-v1 table; the separate
WAL/collision case returns cutoff epoch 1, seq 2 at 13 s and preserves the
previous export.

**Open producing-task defect (T-001/T-011):** a 502-app ranking returns 250
rows and a continuation cursor under the byte cap. `IodClient` rejects that
cursor as `invalid_selector`, and the protocol validator also disconnects a
raw ranking request containing it. `HistoryRpc` supports continuation in
isolation, but the full client/wire path cannot fetch the remaining rows.
`test_wire_ranking_continuation_preserves_every_row_in_order[client|wire]`
remains an ordinary failing test, with no skip/xfail. Production corrections
belong to those tasks; rerun this lane after integrating them. An isolated copy
that allows ranking `cursor` in both selector tables and serializes it from
`IodClient` passes both continuation cases; production sources in this branch
remain unchanged. No all-clear P3 acceptance or host-accounting claim follows
from the other passing cases.

| Host acceptance item | Status in this lane |
| --- | --- |
| Initial disabled service / two real GUIs | UNRUN (fixture equivalent exercised) |
| Actual polkit consent / installed groups / reboot choice | UNRUN (command/consent seams exercised) |
| Fresh real manager / actual block probe | UNRUN; D2 evidence remains open |
| Kernel root peer and installed runtime modes | UNRUN on host (injected metadata plus actual AF_UNIX transport exercised) |
| Real GUI closed >5 s / daemon historical query | UNRUN (virtual lifecycle exercised) |
| Actual disable / final WAL export / local fallback | UNRUN (fixture WAL/export/fallback exercised) |
| Session-ending manager restart | UNRUN and opt-in only |
