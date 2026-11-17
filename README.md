# appio

A block-I/O monitor for Linux desktops. It answers "which application is actually
touching my disk, and how much" from kernel accounting rather than estimation:
per-process counters from `/proc/<pid>/io`, aggregated by stable process identity
and cgroup, with per-file attribution from a BPF probe on the privileged path.

The console ranks applications over a time window, shows one subject's read/write
series and disk share, and lists the files the collector can prove. Every value it
cannot prove is rendered as `unavailable` — a missing residual or device total is
never shown as `0`.

**Status: 0.1.3, pre-release.** The query, storage, IPC and UI layers are exercised
by a large rootless test suite; root collection, polkit consent and BPF are
exercised only through the documented acceptance lane. See
[Known limitations](#known-limitations).

## Two collection modes

| Mode | Who writes | Database | Privileges |
| --- | --- | --- | --- |
| Per-user fallback | the GUI process itself | `~/.local/share/appio/history.db` | none |
| System-wide daemon | `appio-iod.service` | `/var/lib/appio/iod.db` | root, opt-in |

The daemon is the authority when it is active; the GUI then reads through its
socket and never becomes a second writer. When no daemon is answering, the GUI
falls back to its own collector under a single per-UID lease. Turning the system
service on or off is a consent operation, not a checkbox: exactly one control in
Settings can request it, and that request goes through polkit.

## Requirements

- Linux with systemd as PID 1 and cgroup v2 exposing the `io` controller
- Python >= 3.14 with SQLite >= 3.37
- PyQt6 >= 6.6 for the console
- For the system daemon: `libbpf`, a kernel with BTF/CO-RE, and `clang` to build
  the BPF object
- A block filesystem that supports the probe contract (ext2/3/4, xfs, f2fs)

## Install

Arch packaging is the supported path:

```sh
./packaging/arch/build-package.sh --check    # verify the recorded source digest
./packaging/arch/build-package.sh            # build appio-<version>-x86_64.pkg.tar.zst
./packaging/arch/build-package.sh --install  # build, then pacman -U
```

The script exports the tracked tree into a deterministic tarball, refuses to run
with untracked work that would be silently dropped, and builds outside the
checkout so `makepkg` never collides with `src/`.

A manual staging procedure for an expendable host, with every destination, owner
and mode, is in [`docs/acceptance/appio-p3-root.md`](docs/acceptance/appio-p3-root.md).

Installed layout:

| Path | Content |
| --- | --- |
| `/usr/bin/appio` | console launcher (`#!/usr/bin/python -I`) |
| `/usr/bin/appio-iod` | daemon launcher |
| `/usr/bin/appio-setup` | polkit helper, the only root entry point |
| `/usr/lib/appio/fileio.bpf.o`, `fileio_loader` | BPF probe and its userspace loader |
| `/usr/lib/systemd/system/appio-iod.service` | collector unit |
| `/usr/share/polkit-1/actions/io.github.appio.setup.policy` | the single polkit action |
| `/usr/lib/sysusers.d/appio.conf` | the `appio` system group |

## Usage

```sh
appio                          # console, own history in ~/.local/share/appio/history.db
appio --db /tmp/check.db       # a specific database
appio --interval 2             # live sampling period, seconds
appio --once                   # collect one sample and exit, no window
appio --offscreen              # Qt offscreen platform (headless checks)
```

The console header reports the daemon state: `off`, `on, answering`,
`enabled, not answering`, or `on, needs activation`. Rankings cover *since boot*
or a selected window; the subject pane has an `Overview` tab (totals, read vs
write series, attributed share by disk, signed residual) and a `Files` tab
(captured basenames with coverage).

The daemon is started only after consent. Its helper verbs are fixed:

```sh
/usr/bin/pkexec /usr/bin/appio-setup install-delegation
/usr/bin/pkexec /usr/bin/appio-setup enable
/usr/bin/pkexec /usr/bin/appio-setup disable
/usr/bin/pkexec /usr/bin/appio-setup status
/usr/bin/pkexec /usr/bin/appio-setup restart-user-manager
/usr/bin/pkexec /usr/bin/appio-setup export-history
```

`install-delegation` writes the two drop-ins that give the user manager the `io`
controller, `export-history` writes a caller-owned `0600` SQLite copy, and
`status` runs a bounded block probe rather than trusting a mocked number.
Enablement can be pending until a new user manager starts; the GUI reports that
as `on, needs activation` instead of pretending collection is live.

## How it works

| Module | Responsibility |
| --- | --- |
| `contracts` | normative P1 vocabulary; rejects floats/bools where bytes are typed |
| `schema`, `migrations`, `store`, `durable` | atomic stores, in-place v1→v2 upgrade, canonical durable JSON |
| `stream` | collector epochs and `(boot, source, epoch, seq)` frame receipts |
| `procs`, `cgroups`, `sampler`, `live` | `/proc`-shaped inputs, reap-credit aggregation, coverage |
| `ownership`, `reconcile`, `tier` | half-open ownership selection, signed residual, window→source tier |
| `query`, `history_router` | snapshot-consistent tiered queries and the per-panel request planner |
| `ipc_protocol`, `ipc_security`, `ipc_queue`, `iodclient`, `iod` | length-delimited JSON framing, peer credential checks, bounded queues, the daemon |
| `file_runtime`, `bpf_fileio`, `files`, `file_store`, `quota` | BPF probe lifecycle, file-hour accumulation, retention and quota |
| `ui/*` | `collector_window` (shell and the one privileged switch), `main_window` (console), `files_view`, `charts`, `formatting`, `theme` |
| `setup_boundary`, `setup_client`, `setup_delegation`, `setup_service`, `setup_files` | the polkit boundary and its six verbs |
| `handover`, `activation`, `launch`, `collector_owner` | daemon/GUI handover evidence, activation checks, the per-UID fallback lease |

Framing is a 4-byte big-endian length prefix plus one UTF-8 JSON object. Limits:
1 MiB inbound, 256 KiB outbound, 512 KiB per client queue, 64 clients, 500-row
pages, 4000 series points, 200 ms cooperative query deadline, 2 s partial-frame
deadline.

Retention defaults (`quota`): core and gap observations 1 day, file attribution
14 days, 1-minute rollups 30 days, hourly rollups 365 days, under a 1.5 GB quota.

## Security model

- The helper runs as root, so its contract is fixed: argv is exactly one verb from
  a closed set, the beneficiary comes from `PKEXEC_UID` (never `$USER`, `$HOME` or
  argv), and file targets are constant paths.
- One polkit action, `io.github.appio.setup`, `auth_admin` in every session state,
  bound by `exec.path` to `/usr/bin/appio-setup`. No cached authorization.
- Before importing privileged code, `appio-setup` verifies that the interpreter,
  every appio module file and every ancestor directory is root-owned and not
  group- or other-writable. The launchers use `-I` so neither root component
  inherits `PYTHONPATH` or user site-packages.
- The daemon socket is `/run/appio/iod.sock`, `root:appio 0660`, inside a
  `0750` directory; the client verifies path metadata and reads real kernel
  `SO_PEERCRED` before decoding any reply. A socket mode is not treated as proof
  of identity.
- The unit runs `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `NoNewPrivileges=yes`, `UMask=0077`, with writes limited to `/var/lib/appio`
  and `/run/appio`.
- Members of the `appio` group can read machine-wide history; that is a
  deliberate decision, not an oversight.

## Tests

```sh
python -m pytest            # 2738 rootless tests: fixture /proc and cgroup trees,
                            # virtual time, injected credentials
```

The suite never runs polkit, a real manager, BPF, or a host sampler. The host
acceptance lane is separate and dry-run by default:

```sh
/usr/bin/python -I tools/acceptance/appio-p3-root.py            # prints planned operations only
/usr/bin/python -I tools/acceptance/appio-p3-root.py --run --phase enable|verify|disable|...
```

## Known limitations

- **File-attribution lane is degraded.** The daemon can log
  `file_gap_persistence_failed` and `file_source_unavailable diagnostic=file
  persistence deadline expired`, after which the console reports file gaps and an
  unavailable attributed share. The defect is in the producing lane, not the
  console.
- **Ranking continuation is incomplete.** A ranking large enough to page (for
  example 502 apps → 250 rows plus a continuation cursor) produces a cursor that
  `IodClient` rejects as `invalid_selector`; the wire path cannot fetch the
  remaining rows. `test_wire_ranking_continuation_preserves_every_row_in_order`
  fails rather than skipping.
- **Host accounting evidence is unrun.** No claim is made here that this machine
  accounts block I/O beyond what the rootless fixtures and the acceptance dry run
  exercise.

## Layout

```
src/appio/          modules listed above
appio_iod/bpf/      fileio.bpf.c, fileio_loader.c, Makefile
data/               launchers, systemd unit, polkit policy, sysusers entry
packaging/arch/     PKGBUILD and the deterministic export/build script
tools/acceptance/   opt-in host acceptance lane
tests/              rootless suite
docs/acceptance/    manual staging and host acceptance procedure
```
