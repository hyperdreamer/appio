# appio — Per-application disk I/O monitor (design, revision 2)

Date: 2026-10-21
Status: revised after design review attempt-3 (FAIL, 15 blockers) — awaiting re-review
Target: Arch Linux, KDE Plasma 6 / Wayland, Python 3.14 + PyQt6

Revision 2 changes the *measurement model*, not the product. Every blocker from the
review is answered in "Findings ledger" at the end, with the section that fixes it.

## Background

Nothing on a stock Arch desktop answers "which application is wearing out my NVMe
drives, and by how much". `iotop` needs root, is per-process and transient, and
keeps no history. `iostat` shows devices but no applications. This machine has two
1.8 TB NVMe devices (`nvme0n1` carrying `/` and `/data`, `nvme1n1` carrying `/vm`),
so both "which app" and "which disk" are real questions.

Verified facts that shape the design:

| Fact | Observed |
| --- | --- |
| `/proc/<pid>/io` for **root-owned** PIDs | denied, mode `0400` |
| `/proc/<pid>/io` for **own** PIDs | readable **only while the process is dumpable**; own-UID `systemd --user`, `kwin_wayland` and the polkit agent returned `EACCES` |
| One sampling pass | ~20 ms early in the session, ~53 ms under review conditions at ~730 PIDs — treated as a dated observation, re-measured at build time |
| User-session systemd units | 130 `app-*` units, a mix of `@<uuid>.service`, `@autostart.service` and `app-<Name>-<pid>.scope` |
| cgroup v2 `io` controller | enabled at root/`system.slice`/`user.slice`; **absent** below `user.slice` (`DelegateControllers=cpu memory pids`) |
| Per-service `io.stat` | present, mode `0444` (`avahi 259:3 rbytes=569344 wbytes=0`) |
| `cgroup_id` ↔ kernfs inode | equal on this kernel (verified ids match) |
| eBPF prerequisites | clang 23.1.3 BPF backend, libbpf 1.7, `/usr/include/bpf`, readable `/sys/kernel/btf/vmlinux`; **no** `bpftrace`/`bpftool`; `unprivileged_bpf_disabled=2` |
| Session manager | `loginctl show-user 1000` → **`Linger=yes`**, four sessions, `user@1000.service` up since 2026-10-16 |
| polkit | `/usr/bin/pkexec` plus a running `polkit-kde-authentication-agent-1` |

The `Linger=yes` and the `EACCES` rows both invalidate claims made in revision 1 and
drive sections 3.3 and 5 below.

## Goals

1. Record each application's total **task-accounted disk I/O**, reads and writes
   separately, accumulated since boot.
2. Rank applications by that total, sortable.
3. Open one application and see its historical I/O as an infographic.
4. Show which physical disk the I/O landed on, with the reconciliation residual
   disclosed and arithmetic visible.
5. Per-app **file activity** list: which files were read/written, per-file bytes and
   access frequency, sortable — measured, and labelled as logical file activity.
6. Tray behaviour matching `ai-manager`; `appio-iod` enabled **and** disabled from
   the GUI with privilege elevation.

## Non-goals (v1)

Per-access timestamps per file, network I/O, per-process detail below app
granularity, alerting, remote collection, Windows/macOS, retroactive accounting for
units started before setup, and any claim of byte-exact completeness for processes
that live shorter than one sampling interval.

## Decisions taken with the user

| Decision | Choice |
| --- | --- |
| Monitoring scope | **C1**: unprivileged GUI plus `appio-iod` root daemon, shipped but disabled |
| Default metric | **Disk I/O** (`read_bytes`/`write_bytes`) as the headline; all four kernel counters stored |
| Disk attribution | **cgroup `io.stat`** per app unit, with signed residual |
| Accumulation window | **Since boot** default; launch and lifetime also stored and selectable |
| Detail view | Combined timeline + composition, with per-disk lanes and a dual-sided TOTAL lane |
| Per-file source | **eBPF inside `appio-iod`** (fexit-based, see 4.1) |
| Per-file scope | ships in **v1** |
| Switch placement | **Settings owns it**; banner and `Files` empty state deep-link; no tray copy |
| Root enablement | consent dialog + `pkexec appio-setup <verb>`; enable **and** disable |

## 1. Measurement model — three counters, never merged

Revision 1 mixed these; that was blocker B2. They stay separate in the schema, the
contracts, and the UI.

| Symbol | Name | Source | What it is | What it is **not** |
| --- | --- | --- | --- | --- |
| `T` | task-accounted disk I/O | `/proc/<pid>/io` `read_bytes`/`write_bytes` | bytes the task caused to reach the storage layer, plus waited-for children's charges | not SSD wear; includes cancelled/never-flushed writes |
| `D` | cgroup device bytes | `/sys/fs/cgroup/**/io.stat` | block-layer bytes charged to the cgroup, per device | not per-process; excludes migrated-out I/O, includes kworker writeback |
| `F` | logical file activity | eBPF `fexit` on VFS read/write | bytes/files a process moved through the VFS, per file | not physical I/O: 1 GB of page-cache reads is 1 GB of `F` and 0 bytes of `T` |

Identities that are defined **and tested**:

```
Σ(device rows D) + R      = T_window        R is a signed residual
Σ(device reads  D) + R_r  = T_window.read
Σ(device writes D) + R_w  = T_window.write
Σ(file rows F) + F_unknown = F_window       F_unknown is counted, never inferred
```

`R` is signed and its UI text is `attributed exceeds task accounting by X GB` when
negative. `R` is **not** described as "bytes lost to mmap" — its cause is a mix of
accounting points, `mmap`, `io_uring`, eviction and writeback ownership, so the UI
says "difference between accounting points" and shows the number. `F` never enters
any `T` or `D` computation and is never subtracted from them.

`T`'s UI label is **"task-accounted disk I/O"**, not "disk wear".

## 2. Aggregation algorithm (executable, not hand-waved)

Per sample tick, for every readable PID:

1. **Identity** = `(boot_id, pid, start_ticks)` from `/proc/<pid>/stat`
   (`starttime`), validated on *every* read. A mismatch means PID reuse → close the
   old identity, open a new one; never emit a negative delta.
2. **Delta** = `cur_iocounter − last_iocounter` for each of the four counters,
   clamped to `≥ 0`. A decrease resets that identity's baseline and increments
   `resets` (a per-window audit counter), never a negative byte.
3. **Reaped children (B3).** `/proc/<pid>/io` adds a reaped child's totals into its
   parent. When a tracked child identity disappears in the same tick a parent's
   delta exceeds the child's last cumulative total, subtract the child's last
   cumulative total from the parent delta and add the difference to `R_reap`. The
   remainder is recorded as the parent's own delta.
4. **Exit** never reduces a window total, because window totals are sums of emitted
   deltas, never sums of live snapshots.
5. **Inaccessible processes (own-UID `EACCES`).** Recorded per app as
   `inaccessible_pids`; the app's window total is rendered `≥ X` with the count
   attached. A boot total is never silently presented as complete.
6. **Short-lived processes (B4).** A process may start and exit between ticks. Each
   app records `unobserved_starts`; when a first observation shows non-zero
   counters, those bytes are attributed to the interval but flagged
   `interval_partial = 1`. Coverage is therefore a first-class stored quantity:
   `coverage = {inaccessible_pids, unobserved_starts, resets, reap_corrections}`.
7. **Time (B6).** Record identity is `(source, epoch, seq)`; ordering is
   `CLOCK_BOOTTIME` nanoseconds, which is monotonic within a boot and survives
   suspend. Wall-clock time is stored beside it for display only. Bucket assignment
   uses the monotonic value with a per-insert `monotonic→wall` offset captured at
   that instant, so an NTP step moves no rows and repeats nothing. Suspend gaps are
   recorded as gap rows, not interpolated.

## 3. Collection ownership and durability (B4)

Two collectors exist; exactly one is authoritative for a given interval.

| Condition | Collector of record | Durability |
| --- | --- | --- |
| `appio-iod` active | daemon | daemon persists to `/var/lib/appio/iod.db` (root, `0600`), flushing at least every 5 s, so closing the GUI loses nothing |
| daemon absent | GUI | GUI persists to `~/.local/share/appio/history.db` |

- On daemon start/stop, a **handover epoch** is written by both sides. Intervals are
  attributed to exactly one collector; overlapping ticks are resolved in favour of
  the daemon, and the discarded interval is counted in `handover_discarded` rather
  than silently merged.
- The GUI never writes rows for a window the daemon owns: it reads them over the
  socket and keeps only a read-through cache.
- 3.3 **Activation with `Linger=yes` (B13).** On this host the user manager is
  immune to logout. `install-delegation` therefore ends with an explicit choice:
  `Restart my user manager now` (destructive: ends the session, saves nothing) or
  `Activate after reboot`, with a persisted `pending_activation` flag and a banner
  until the probe passes. Success is verified by reading
  `user@1000.service/cgroup.controllers` for `io` — not assumed from an exit code.

## 4. Components

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `appio.procs` | enumerate PIDs; read `io`, `stat`, `cgroup`; tolerate `ESRCH`/`EACCES`; expose `dumpable` failures separately from permission facts | `/proc` |
| `appio.cgroups` | parse `io.stat`; map `major:minor` → device name; detect controller absence | `/sys/fs/cgroup`, `/proc/diskstats` |
| `appio.identity` | resolve `app_key` from a unit (`@<uuid>.service`, `@autostart.service`, `app-<Name>-<pid>.scope`, escaped ids) or from executable | `procs` |
| `appio.sampler` | one tick; identity validation; deltas; reap correction; coverage counters | `procs`, `cgroups`, `identity` |
| `appio.store` | schema, migrations, insert/rollup/retention, query API | sqlite3 |
| `appio.iodclient` | socket client: framing, peer verification, capabilities, reconnect with epoch/seq dedupe | socket |
| `appio.iod` | daemon: all-PID sampling, device stats, BPF drain, persistence, serving | `sampler`, BPF |
| `appio_iod/bpf/fileio.bpf.c` | fexit accounting, see 4.1 | libbpf, BTF |
| `appio.files` | file-row ingestion, path states, coverage/loss accounting | `store` |
| `appio.ui.*` | list, Overview, Files, tray, settings, consent flow | `store`, `appio.setup_client` |
| `appio.setup_client` | consent dialog, `pkexec` invocation, exit-code mapping, state probe | polkit |

### 4.1 BPF program contract (B1, B8, B9)

`fexit` — not `fentry` — on all four VFS entry points, because the byte count is
the **return value**, not the argument:

```
SEC("fexit/vfs_read")        ssize_t -> bytes if ret > 0
SEC("fexit/vfs_write")       ssize_t -> bytes if ret > 0
SEC("fexit/vfs_iter_read")   ssize_t -> bytes if ret > 0   (vectored path)
SEC("fexit/vfs_iter_write")  ssize_t -> bytes if ret > 0
```

- **Eligible objects only:** `S_ISREG(inode->i_mode)` **and** the superblock has a
  block device (`sb->s_bdev != NULL`). Pipes, sockets, `/proc`, `/sys`, `tmpfs`,
  `anon_inode` are excluded and counted in `skipped_nonfile` so the exclusion is
  visible, not invisible.
- **Key:** `(cgroup_id, dev, inode, i_generation)`. `i_generation` defeats inode
  reuse within a bucket (B9).
- **Value:** `{reads, writes, bytes_r, bytes_w, name_len, name[40]}` — a truncated
  *basename* captured while the dentry is alive, never a reconstructed path.
- **Never evict silently (B8):** `BPF_MAP_TYPE_HASH`, `max_entries = 262144`, no LRU.
  A failed insert increments `overflow_files`; overflowed bytes are added to
  `overflow_bytes_r/w` in a separate global-counter map, so loss is **counted**.
  Measured payload: 262144 × (key 32 B + value 88 B) ≈ 31 MB plus hash overhead, and
  that figure is stated in the settings UI.
- **Race-safe drain:** `bpf_map_lookup_and_delete_elem`, one call per element, so a
  concurrent increment is either taken in this drain or lands in the next one.
- **Failure of load** (no BTF, verifier error) disables only `F`; `T` and `D`
  continue, and the Files tab states the errno and verifier line.

### 4.2 Wire protocol (B10, B11)

One length-delimited JSON object per frame, **both directions**, `protocol: 1`.

```json
{"protocol":1,"type":"snapshot","boot_id":"…","iod_epoch":7,"seq":10413,
 "ts_mono_ns":1761000000123456,"ts_wall":1761000000.1,"source":"iod",
 "capabilities":{"proc_all":true,"dev_stat":true,"file_io":true,"bpf_loaded":true},
 "procs":[…],"devs":[…],"files":[…],
 "coverage":{"inaccessible_pids":3,"unobserved_starts":11,"resets":0,"reap_corrections":2,
             "skipped_nonfile":90210,"overflow_files":0,"overflow_bytes_r":0,"overflow_bytes_w":0}}
```

Client → server messages: `{"protocol":1,"type":"status"}` and
`{"protocol":1,"type":"subscribe","since":{"iod_epoch":7,"seq":10412}}`. Deduplication
is by `(iod_epoch, seq)`; a reconnect with a stale `since` receives
`{"type":"gap","from":…,"to":…}` and the client records a gap row instead of
inventing continuity. The daemon rejects a frame over 1 MiB, and disconnects any
client that leaves two consecutive snapshots unread.

Precedence rule: for a given `(app, window)` the daemon's rows win; the GUI's own
rows are used only when `iod_epoch` is absent for that interval.

## 5. Privilege surface (B11, B12)

- `/run/appio/` is `root:appio 0750`; the socket is `root:appio 0660`. The package
  creates group `appio`; `install-delegation` adds the invoking user to it. This
  costs no extra login, because delegation already requires activation (3.3).
- Server-side: `SO_PEERCRED`; accept only `uid 0` or a member of `appio`; log refusals.
- Client-side: verify the **server** uid is `0` before trusting data.
- No command surface: the only client messages are `status` and `subscribe`.
- `appio-setup`: `/usr/bin/appio-setup`, polkit action `io.github.appio.setup`, bound
  with `org.freedesktop.policykit.exec.path=/usr/bin/appio-setup`, `auth_admin`
  (not `keep`). It validates `argc == 2`, matches `argv[1]` against the four verbs,
  uses absolute `/usr/bin/systemctl`, writes config by `tmp → fsync → rename`, and
  refuses if the target path is a symlink or not root-owned. Interpreter, imported
  modules and the BPF object are root-owned and not writable by the invoking user.

## 6. Data model (SQLite)

```sql
-- one row per machine boot, both collectors write it
boot(boot_id TEXT PRIMARY KEY, started_mono_ns INTEGER, started_wall REAL, source TEXT);
app(app_id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, icon_path TEXT,
    first_seen REAL, last_seen REAL, scope TEXT);
launch(launch_id INTEGER PRIMARY KEY, app_id, boot_id, started_mono_ns, ended_mono_ns,
       proc_count, coverage_json);
-- raw samples: identity is (source, epoch, seq); monotonic ordering; wall time for display
sample(source TEXT, epoch INTEGER, seq INTEGER, app_id, launch_id,
       ts_mono_ns INTEGER, ts_wall REAL,
       rchar INTEGER, wchar INTEGER, read_bytes INTEGER, write_bytes INTEGER,
       delta_kind TEXT, interval_partial INTEGER,
       PRIMARY KEY (source, epoch, seq, app_id)) WITHOUT ROWID;
sample_dev(source TEXT, epoch INTEGER, seq INTEGER, app_id, ts_mono_ns INTEGER,
           dev TEXT, rbytes INTEGER, wbytes INTEGER,
           PRIMARY KEY (source, epoch, seq, app_id, dev)) WITHOUT ROWID;
path(path_id INTEGER PRIMARY KEY, dev TEXT, inode INTEGER, generation INTEGER,
     basename TEXT, state TEXT, UNIQUE(dev, inode, generation));
file_sample(source TEXT, epoch INTEGER, seq INTEGER, app_id, ts_mono_ns INTEGER,
            dev TEXT, path_id INTEGER, reads INTEGER, writes INTEGER,
            bytes_r INTEGER, bytes_w INTEGER,
            PRIMARY KEY (source, epoch, seq, app_id, path_id)) WITHOUT ROWID;
file_coverage(source TEXT, epoch INTEGER, seq INTEGER, app_id,
              overflow_files INTEGER, overflow_bytes_r INTEGER, overflow_bytes_w INTEGER,
              skipped_nonfile INTEGER, PRIMARY KEY (source, epoch, seq, app_id)) WITHOUT ROWID;
-- rollups: carry boot identity AND device, so the promised windows stay answerable (B5)
rollup_app(boot_id, app_id, bucket_start_mono_ns, bucket_secs, read_bytes, write_bytes,
           rchar, wchar, PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs));
rollup_dev(boot_id, app_id, bucket_start_mono_ns, bucket_secs, dev, rbytes, wbytes,
           PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs, dev));
-- never pruned: the windows the UI promises
app_boot_total(boot_id, app_id, read_bytes, write_bytes, rchar, wchar, coverage_json,
               PRIMARY KEY (boot_id, app_id));
app_lifetime_total(app_id PRIMARY KEY, read_bytes, write_bytes, rchar, wchar, updated_mono_ns);
gap(source, epoch, seq, from_mono_ns, to_mono_ns, reason);
```

**Tier selection is disjoint, never blended (B5):** window ≤ 24 h → `sample`;
≤ 30 d → `rollup_*` at 60 s; ≤ 365 d → `rollup_*` at 3600 s; longer → totals tables
only, and the UI says "totals only beyond 365 days".

**Storage budget, with arithmetic (B7):** per-app per-minute top-N files, `N = 200`
(configurable), gives ≤ 200 × 1440 = 288 000 rows/app/day; the **global** cap is
`2 000 000` file rows/day across all apps, enforced by quota, not hope; `path` rows
are garbage-collected when no `file_sample` references them; the DB budget is
≤ 400 MB for 30 days with `VACUUM` thresholds documented. Selection is deterministic:
rank by `bytes_r + bytes_w`, tie-break by `reads + writes`, then by `inode`
ascending; the omitted remainder is stored in `file_coverage` and displayed.

## 7. Contracts

```python
@dataclass(frozen=True)
class ProcSample:
    pid: int; start_ticks: int; uid: int; dumpable: bool; exe: str | None
    io: dict[str, int]            # rchar, wchar, read_bytes, write_bytes
    cgroup: str | None

@dataclass(frozen=True)
class DevStat: dev: str; rbytes: int; wbytes: int

@dataclass(frozen=True)
class FileStat:
    dev: str; inode: int; generation: int; basename: str
    reads: int; writes: int; bytes_r: int; bytes_w: int

# appio.store — every window query takes an explicit window and returns coverage
def ranking(window: Window, metric: Metric) -> list[AppTotal]        # each carries coverage
def series(app_id: int, window: Window) -> Series                    # tier chosen per 6
def disk_share(app_id: int, window: Window) -> DiskShare             # dev rows + signed residual
def file_table(app_id: int, window: Window, sort: FileSort,
               direction: Literal["asc","desc"], search: str | None) -> FileTable
def file_coverage(app_id: int, window: Window) -> FileCoverage       # counted loss, not inferred
```

`FileSort ∈ {bytes_total, bytes_r, bytes_w, reads, writes, basename, dev}` and the
default is `bytes_total` descending, matching the mockup's "sorted by traffic".

## 8. UI

**Precedence (B15):** this text is the behavioural authority; the mockups in
`assets/` are the **layout** authority. Where they disagree, layout follows the
mockup and behaviour follows this text, and the disagreement is listed here:

- `assets/2026-10-21-scope-and-daemon.png` shows an `Include system services`
  toggle inside the app window. **Superseded** by the Settings switch (§9).
- `assets/2026-10-21-detail-view.png` is re-rendered with **three** lanes — a
  dual-sided `TOTAL` lane above `nvme0n1` and `nvme1n1` — matching the approved
  combined view.
- `assets/2026-10-21-per-file.png` gains a footer stating file-activity coverage.

Screens:

- **Main window**: application list (name, since-boot total, inline bar, search),
  detail pane with `Overview` and `Files` tabs.
- **Overview**: KPI strip (`TOTAL` with lifetime sub-line, `READ`, `WRITE`, `PEAK`);
  three timeline lanes (`TOTAL` read-up/write-down, then one per disk); range chips;
  read/write donut; attributed-share-by-disk with `Σ disks + residual = total`
  printed; hour-of-day intensity; reconciliation strip carrying the signed residual.
- **Files**: search, sort indicator, table `FILE · DISK · READS · WRITES ·
  BYTES R · BYTES W`, sortable on every column. Footer states: file activity is
  **logical** (page-cache reads count), and how much is unknown
  (`overflow`, `skipped non-file`, `unobserved`) — never that it is zero.
- **Settings**: cadence, retention, tray options, autostart, and the single
  `System services (root)` switch with a probed status line
  (`off` · `on, answering` · `enabled, not answering` · `on, needs activation`).
- **Tray parity contract (B14), stated exactly:** `Trigger` toggles; hide only when
  the window is visible, not minimized, and **active**, otherwise show + raise +
  activate; `Show`/`Hide` label follows visibility signals; close goes to tray only
  when a tray is currently available, with a one-time first-close message, and
  otherwise quits; the `Quit` action bypasses close-to-tray; `--minimized`/`--tray`
  are runtime-only overrides ORed with the stored preference; autostart `.desktop`
  is created, updated (including `--minimized`) and removed with the preference;
  icon is palette-tinted with the style's standard icon as fallback when null.

## 9. Enabling and disabling the daemon

`appio-setup` verbs: `install-delegation`, `enable`, `disable`, `status`. First use
walks the user through **both** steps in one consent flow — delegation install, then
enable — because enabling alone cannot produce per-app device data (B13). `disable`
stops and disables the unit and deliberately keeps the delegation drop-ins; removing
them is `uninstall-delegation`, out of v1 scope. The switch shows probed state,
never the last click; success requires the socket to answer within 5 s; polkit
dismissal (126) reverts the switch with the exit code and first stderr line.

## 10. Error handling and degradation

| Condition | Behaviour |
| --- | --- |
| `ESRCH` mid-scan | skip, counted in `resets` if the identity was already open |
| own-UID `EACCES` (non-dumpable) | app shows `≥ X`, `inaccessible_pids` in coverage |
| `io.stat` absent for a unit | device split shows `not delegated`; `T` unaffected |
| daemon absent | banner + `Open settings` button; GUI owns collection |
| daemon enabled but silent | `enabled, not answering`; no pretend data |
| BPF load failure | Files tab shows errno + verifier line; `T`/`D` continue |
| map insert failure | `overflow_*` counters rise; Files footer says how much is unknown |
| drain/reconnect | `(epoch, seq)` dedupe; explicit `gap` frames become `gap` rows |
| reap detected | child total subtracted, `R_reap` incremented, never negative |
| clock step / suspend | monotonic ordering; gap rows; no phantom bytes |
| GUI closed while daemon runs | nothing lost (daemon persists) |
| GUI closed with daemon off | `gap` row covering the absence; totals labelled `≥` |
| DB corruption | quarantine `history.db.corrupt-<ts>`, recreate, notify |
| `pkexec` dismissed/failed | switch reverts, exact code and stderr shown |
| delegation installed, activation pending | banner + `pending_activation`; probe re-checks `cgroup.controllers` |
| `appio-setup` missing / container host | switch disabled with the reason |

## 11. Testing strategy

- Fixture `/proc` and `/sys/fs/cgroup` trees; no test needs root, a real session, or
  live processes.
- `procs`: parsing, `ESRCH` race, `EACCES`, PID reuse via `start_ticks`.
- `sampler`: delta clamping, reap correction (`R_reap`), coverage counters,
  short-lived process flagging, monotonic bucketing across an NTP step and a suspend.
- `cgroups`: multi-device `io.stat`, `major:minor` mapping, absent controller.
- `identity`: `@<uuid>.service`, `@autostart.service`, `app-<Name>-<pid>.scope`,
  escaped desktop ids, shared cgroups, executable fallback.
- `store`: signed reconciliation identity for total/read/write including a
  **negative-residual** case; disjoint tier selection; retention pruning; path GC;
  global file quota; migration idempotency; `(source, epoch, seq)` dedupe.
- `files`: top-N determinism with tie-breaks, `file_coverage` arithmetic
  (`Σ rows + unknown = observed`), truncated-name states, inode-reuse separation via
  `i_generation`.
- BPF: aggregator logic compiled against a userspace shim and asserted on synthetic
  key/value vectors; a load test that skips unless `geteuid()==0` and BTF exists, so
  CI never fakes a pass.
- `iodclient`: framing, oversized frame rejection, `gap` handling, peer-uid
  verification, slow-client disconnect.
- `setup_client`: verb allowlist, exit-code mapping, switch revert, probe parsing,
  `pending_activation` surfacing.
- UI (`pytest-qt`, offscreen): sort on every Files column, tier labels, reconciliation
  strip arithmetic parsed from rendered text, deep links change no state, tray menu
  contains no `System services` action, tray transitions per §8.

## 12. Findings ledger — review attempt-3

| # | Blocker | Resolution |
| --- | --- | --- |
| B1 | fentry sees requested bytes | §4.1 `fexit` on all four VFS entry points; `ret > 0`; eligible-object filter; exclusion counters |
| B2 | logical vs physical mixed in `file_residual` | §1 three counters, `F` never subtracted from `T`; `F_unknown` counted separately |
| B3 | reaping double count, snapshot sums, cancelled writes | §2 identity validation, delta clamping, explicit reap correction, `R_reap` |
| B4 | missed short-lived processes; no durable collection | §2.6 `unobserved_starts` + `interval_partial`; §3 daemon persists |
| B5 | rollups lack boot/device; overlapping tiers | §6 boot-scoped `rollup_app`/`rollup_dev`, explicit disjoint tier rule, totals tables |
| B6 | wall-clock PK unsafe | §2.7 + §6 `(source, epoch, seq)` identity, monotonic ordering, wall time display-only |
| B7 | storage bound wrong by orders of magnitude | §6 N=200/minute, global 2 M rows/day quota, path GC, stated 400 MB budget, deterministic selection |
| B8 | LRU loss, drain race, no dedupe | §4.1 non-evicting map + `overflow_*` counters + `lookup_and_delete`; §4.2 `(epoch, seq)` dedupe |
| B9 | truncated names, links, inode reuse, vanished cgroup | §4.1 basename-with-states, `i_generation` key, cgroup cache before removal, rename policy |
| B10 | identity/transport ambiguity | §4.2 protocol v1 with boot/epoch/seq/capabilities/status, precedence rule; §7 `exe` in `ProcSample` |
| B11 | socket security unspecified | §5 directory/socket modes, group, `SO_PEERCRED` both directions, framing caps, slow-client policy |
| B12 | helper privilege boundary | §5 polkit action binding, absolute paths, argc check, atomic config writes, trusted deps |
| B13 | no install-delegation path; `Linger=yes` breaks logout advice | §3.3 + §9 single consent flow, explicit manager-restart/reboot choice, probe verification |
| B14 | tray parity incomplete | §8 tray parity contract, written from the reference implementation's actual transitions |
| B15 | mockups contradict text | §8 precedence + superseded list; detail frame re-rendered with three lanes; F1 frame gains coverage footer |

Machine-fact corrections folded in: own-UID `EACCES` (§Background, §2.5), `Linger=yes`
(§3.3), eBPF tooling present but not `bpftrace` (§4.1), sampling cost restated as a
dated observation (§Background).
