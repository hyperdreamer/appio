# appio — Per-application disk I/O monitor (design, revision 5)

Date: 2026-10-21
Status: revision 5 — aligned with the reviewed P1 specification (spec r3); awaiting operator sign-off
Target: Arch Linux, KDE Plasma 6 / Wayland, Python 3.14 + PyQt6

Revision history: r1 initial design; r2 rewrote the measurement model after review
attempt-3 (15 blockers); r3 answered review attempt-4 (11 partial closures, 7 new
blockers) and adopted the A′ delivery order; r4 fixes the six internal contradictions
found while drafting the P1 specification and records the operator's two product
rulings (hourly file observations; unanswerable windows are disabled, not faked); r5
aligns this document with the reviewed specification: evidence-only unseen-reap
classification, nullable peak validity with a persisted combined peak, an
attributed-timestamp sample key, a durable sampler checkpoint, canonical durable JSON
schemas, file identity versus alias separation, and null semantics for unavailable
results.

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
   parent, so a corrected detector cannot rely on "parent delta exceeds child". The
   algorithm is credit-based instead: when identity `C` disappears, its last
   cumulative totals are recorded as a **credit** owed against its parent identity
   `P`. Every subsequent delta emitted for `P` is reduced by `min(credit, delta)`, and
   the remainder of the credit carries forward until exhausted. If `P` also
   disappears first, the unapplied credit is counted in `R_reap_unapplied` rather
   than being folded anywhere. If a parent shows a jump and no child identity is
   known (the child was never sampled), the jump is **counted normally and disclosed**: r5
   supersedes the earlier `reap_unobserved` attribution because magnitude alone cannot
   distinguish a reaped child from ordinary parent I/O. The provisional rule is
   evidence-only — without a disappeared child identity there is no evidence, so the
   increment is not reclassified — and the audit counter records that an unclassified
   increment occurred (`unclassified_parent_increment`) rather than claiming it was a reap.
   Identity is read atomically: `/proc/<pid>/stat` (for `starttime`) and
   `/proc/<pid>/io` are read as one logical record, then `starttime` is re-read; a
   mismatch discards the sample rather than mixing two processes.
4. **Exit** never reduces a window total, because window totals are sums of emitted
   deltas, never sums of live snapshots.
5. **Inaccessible processes (own-UID `EACCES`).** Recorded per app as
   `inaccessible_pids`; the app's window total is rendered `≥ X` with the count
   attached. A boot total is never silently presented as complete.
6. **Processes that start and exit inside one tick are unknowable, not countable
   (B4).** Polling has no event source for them, so there is no honest counter for
   their number. What is stored instead is `first_seen_partial`: when an identity is
   observed for the first time with non-zero counters, its pre-observation bytes are
   unknown, so the row is flagged and the app's window carries
   `partial_intervals + 1`. The UI says "some processes were never observed" — it
   never invents a count of them.
7. **Time (B6, B3-of-round-2 / N3).** Buckets are **fixed `CLOCK_BOOTTIME`
   arithmetic**: `bucket_start_mono_ns = ts_mono_ns // bucket_ns * bucket_ns`. There is
   no per-insert wall offset — that was algebraically identical to using realtime and
   is removed. Wall time is converted for display only, through persisted
   `clock_map(boot_id, segment_start_mono_ns, wall_offset_ns)` segments written whenever the
   offset changes by more than 1 s; a historical hour-of-day chart after pruned raw
   data re-derives wall labels from those segments, so an NTP step cannot move rows
   or repeat labels. A delta that spans a suspend is assigned to the last bucket
   observed before suspend and flagged `spans_suspend = 1`; rates divide by *observed*
   time, excluding gap time, so a suspend cannot produce a fake trough or a fake spike.

## 3. Collection ownership and durability (B4)

Two collectors exist; exactly one is authoritative for a given interval.

| Condition | Collector of record | Durability |
| --- | --- | --- |
| `appio-iod` active | daemon | daemon persists to `/var/lib/appio/iod.db` (root, `0600`), flushing at least every 5 s, so closing the GUI loses nothing |
| daemon absent | GUI | GUI persists to `~/.local/share/appio/history.db` |

- Handover is a **protocol, not a convention (N1)**. Each collector holds a
  persisted `collector_epoch` and writes `collector_interval` rows for every interval
  it owned. A handover records `cutoff_mono_ns` plus the counter **baselines** of all
  live identities at that instant. Rules: the outgoing collector stops at `cutoff`,
  the incoming one starts at `cutoff`; baselines mean the incoming collector emits no
  bytes for work done before it started (no retroactive accounting); any interval not
  covered by a `collector_interval` row becomes a `gap` row and is excluded from
  totals with `coverage.gap_intervals` incremented. If a `handover` row is missing
  because a process died mid-transfer, both intervals are kept, the overlap is
  flagged `overlap_uncertain`, and its bytes are excluded rather than double counted.
- The GUI never writes rows for a window the daemon owns: it reads them over the
  socket and keeps only a read-through cache.
- 3.3 **Activation with `Linger=yes` (B13).** On this host the user manager is
  immune to logout, so `install-delegation` ends with an explicit choice:
  `Restart my user manager now` (destructive: ends the session) or `Activate after
  reboot`, with a persisted `pending_activation` flag and a banner until activation
  passes. Success is **proved by working accounting, not by controller presence**:
  `appio-setup status` starts a transient user unit
  (`systemd-run --user --unit=appio-probe-<ts> dd if=/dev/zero of=<tmpfs>/p bs=1M
  count=16 oflag=direct`), reads that unit's `io.stat`, and requires the expected
  device bytes within 5%; the unit is then stopped and removed. A controller that is
  delegated but not accounting fails the probe and the UI says
  `delegation present, accounting inactive` rather than claiming success.

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
SEC("fexit/vfs_readv")       ssize_t -> bytes if ret > 0   (syscall path)
SEC("fexit/vfs_writev")      ssize_t -> bytes if ret > 0   (syscall path)
SEC("fexit/vfs_iter_read")   ssize_t -> bytes if ret > 0   (kernel-internal iterators)
SEC("fexit/vfs_iter_write")  ssize_t -> bytes if ret > 0
```

**Why all six and not four (B1, CONTRADICTED by disassembly):** `vfs_readv`/
`vfs_writev` call `do_iter_readv_writev` directly and do **not** route through
`vfs_iter_read`/`vfs_iter_write`, so a four-hook set silently misses every
`readv`/`writev`. All six symbols are present in this kernel's BTF (verified) and
in `kallsyms`. Each hook increments its own `hook_hits_<name>` counter, and the
Files footer exposes those counters, so a hook that never fires is visible instead
of being assumed to work.

- **Eligible objects only:** `S_ISREG(inode->i_mode)` **and** the superblock has a
  block device (`sb->s_bdev != NULL`). Pipes, sockets, `/proc`, `/sys`, `tmpfs`,
  `anon_inode` are excluded and counted in `skipped_nonfile` so the exclusion is
  visible, not invisible.
- **Key:** `(cgroup_id, dev, inode, i_generation)`. `i_generation` defeats inode
  reuse within a bucket (B9).
- **Value:** `{reads, writes, bytes_r, bytes_w, name_len, name[40]}` — a truncated
  *basename* captured while the dentry is alive, never a reconstructed path.
- **Never evict silently, and attribute the loss (B8, corrected in r4):**
  `BPF_MAP_TYPE_HASH`, `max_entries = 262144`, no LRU. A failed insert increments
  `overflow_files` **and** `overflow_bytes_r/w` in a **per-cgroup-keyed** overflow map
  (`BPF_MAP_TYPE_HASH`, key `cgroup_id`, value three counters), not a single global
  counter. r3 specified both a global counter and per-app overflow accounting, which
  cannot both be true; per-cgroup keying is what makes `file_coverage`'s per-app
  fields producible. A cgroup that cannot be keyed (map full) rolls into a reserved
  `overflow_owner_unknown` key, which the UI reports as an unknown-owner remainder.
  Measured payload: 262144 × (key 32 B + value 88 B) ≈ 31 MB plus hash overhead, and
  that figure is stated in the settings UI.
- **Lossless drain by double buffering (B8, replaces lookup-and-delete):** the data
  map is an `ARRAY_OF_MAPS` holding two inner hash maps plus a one-entry
  `active_index` map. The BPF program reads `active_index` and writes into that
  inner map. Userspace flips `active_index`, then drains the now-inactive map with
  `lookup_and_delete_elem` at leisure. No program can hold a pointer into a map that
  is being deleted from, which is the loss window a plain lookup-and-delete leaves
  open. Drain is therefore lossless by construction rather than by claim; the only
  remaining loss is a full inner map, which increments `overflow_*`.
- **Failure of load** (no BTF, verifier error) disables only `F`; `T` and `D`
  continue, and the Files tab states the errno and verifier line.
- **Registration race (B8):** after attach, userspace waits until every
  `hook_hits_*` counter has increased at least once before reporting `F` as active;
  a hook that never fires is reported as `inactive`, not as "no I/O".

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

Client → server messages: `{"protocol":1,"type":"status"}`,
`{"protocol":1,"type":"subscribe","since":{"iod_epoch":7,"seq":10412}}`, and
`{"type":"query","kind":"ranking|series|files|coverage|totals","window":{…},"sort":…}`.
Deduplication is by `(boot_id, iod_epoch, seq)`; a reconnect with a stale `since`
receives `{"type":"gap","from":…,"to":…}` and the client records a gap row instead of
inventing continuity.

**Historical queries are part of the protocol, not an afterthought (N2).** The daemon
answers `query` from its own durable store with hard server-side bounds: ≤ 4 000
points per `series`, ≤ 500 rows per `files` page, ≤ 256 KiB per response frame, and
`totals`/`coverage` always answerable for any retained window. A GUI that starts
long after the daemon has been running can therefore render history it never
observed, instead of living with gaps. The daemon rejects a frame over 1 MiB and
disconnects a client that leaves two consecutive snapshots unread; total egress is
capped by a per-client queue budget, not just per-frame.

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
  (not `keep`). Privilege rules stated exactly:
  - the invoking user comes from **`PKEXEC_UID`** (the calling `getuid()` is root
    inside the helper), validated with `getpwuid`, never from `$USER` or argv;
  - `argc == 2` and `argv[1]` matched against the six verbs; anything else exits 2
    having touched nothing;
  - every external command is an absolute path (`/usr/bin/systemctl`,
    `/usr/bin/usermod -aG appio <user>`, `/usr/bin/dd`) with fixed argv and no shell;
  - the file targets are fixed constants (`/etc/systemd/system/user@.service.d/10-appio-io.conf`,
    `/etc/systemd/user.conf.d/10-appio.conf`), opened `O_NOFOLLOW`, written to a
    `mkstemp` sibling, `fsync`-ed, renamed, then the **parent directory is
    `fsync`-ed** too;
  - it refuses if any target or its parent directory is a symlink or not root-owned;
  - interpreter, imported modules and the BPF object are root-owned and not writable
    by the invoking user, so the helper cannot be turned into a loader for user code.

## 6. Data model (SQLite)

```sql
-- one row per machine boot, both collectors write it
boot(boot_id TEXT PRIMARY KEY, started_mono_ns INTEGER, started_wall REAL, source TEXT);
app(app_id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, icon_path TEXT,
    first_seen REAL, last_seen REAL, scope TEXT);
launch(launch_id INTEGER PRIMARY KEY, app_id, boot_id, started_mono_ns, ended_mono_ns,
       proc_count, coverage_json);
-- raw samples: identity is (source, epoch, seq); monotonic ordering; wall time for display
-- identity is per collector stream: boot + source + epoch + seq (+ app, launch)
sample(boot_id, source, epoch, seq, app_id, launch_id, ts_mono_ns INTEGER,
       ts_wall REAL,
       rchar INTEGER, wchar INTEGER, read_bytes INTEGER, write_bytes INTEGER,
       delta_kind TEXT, first_seen_partial INTEGER, spans_suspend INTEGER,
       PRIMARY KEY (boot_id, source, epoch, seq, app_id, launch_id, ts_mono_ns)) WITHOUT ROWID;
-- r5: one frame may attribute contributions to several buckets (a suspend-spanning
-- parent delta and a newly seen child land in different buckets), so the attributed
-- timestamp is part of the key and coverage is stored once per frame, not per row.
sampler_checkpoint(source TEXT PRIMARY KEY, boot_id TEXT, epoch INTEGER, seq INTEGER,
                   identities_json TEXT NOT NULL, credits_json TEXT NOT NULL,
                   committed_mono_ns INTEGER NOT NULL);
sample_dev(boot_id, source, epoch, seq, app_id, ts_mono_ns INTEGER,
           dev TEXT, rbytes INTEGER, wbytes INTEGER,
           PRIMARY KEY (boot_id, source, epoch, seq, app_id, dev)) WITHOUT ROWID;
path(path_id INTEGER PRIMARY KEY, fs_uuid TEXT, dev TEXT, inode INTEGER, generation INTEGER,
     basename TEXT, state TEXT, identity_weak INTEGER, first_mono_ns, last_mono_ns,
     UNIQUE(fs_uuid, dev, inode, generation, basename));
path_alias(path_id, basename TEXT, first_mono_ns, last_mono_ns, PRIMARY KEY(path_id, basename));
file_sample(boot_id, source, epoch, seq, app_id, ts_mono_ns INTEGER,
            dev TEXT, path_id INTEGER, reads INTEGER, writes INTEGER,
            bytes_r INTEGER, bytes_w INTEGER, last_seen_mono_ns INTEGER,
            PRIMARY KEY (boot_id, source, epoch, seq, app_id, path_id)) WITHOUT ROWID;
file_coverage(boot_id, source, epoch, seq, app_id,
              counted_omitted_files INTEGER, counted_omitted_bytes_r INTEGER,
              counted_omitted_bytes_w INTEGER,
              uncounted_overflow_files INTEGER, uncounted_overflow_bytes_r INTEGER,
              uncounted_overflow_bytes_w INTEGER,
              skipped_nonfile INTEGER, skipped_unsupported_fs INTEGER,
              PRIMARY KEY (boot_id, source, epoch, seq, app_id)) WITHOUT ROWID;
-- collector streams and handover (N1)
collector_epoch(source TEXT PRIMARY KEY, boot_id TEXT, epoch INTEGER, allocated_mono_ns);
collector_interval(source TEXT, boot_id TEXT, epoch INTEGER, from_mono_ns INTEGER, to_mono_ns INTEGER,
                   PRIMARY KEY (source, boot_id, epoch));
handover(boot_id, from_source, to_source, cutoff_mono_ns INTEGER,
         baselines_json TEXT, PRIMARY KEY (boot_id, cutoff_mono_ns));
clock_map(boot_id TEXT, segment_start_mono_ns INTEGER, wall_offset_ns INTEGER,
          PRIMARY KEY (boot_id, segment_start_mono_ns));
-- r4: monotonic values restart every boot, so every table that stores one also stores
-- its boot identity (clock_map, path, path_alias, app_lifetime_total.updated_boot_id).
-- Raw-window coverage is stored per tick, because it cannot be reconstructed later.
sample_coverage(boot_id, source, epoch, seq, app_id,
                inaccessible_pids INTEGER, resets INTEGER, reap_corrections INTEGER,
                reap_unapplied INTEGER, reap_unobserved INTEGER,
                first_seen_partial INTEGER, spans_suspend INTEGER,
                PRIMARY KEY (boot_id, source, epoch, seq, app_id)) WITHOUT ROWID;
-- rollups: carry boot identity AND device, so the promised windows stay answerable (B5)
rollup_app(boot_id, app_id, bucket_start_mono_ns, bucket_secs, read_bytes, write_bytes,
           rchar, wchar, peak_read_bps, peak_write_bps, peak_total_bps, coverage_json,
           PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs));
rollup_dev(boot_id, app_id, bucket_start_mono_ns, bucket_secs, dev, rbytes, wbytes,
           PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs, dev));
-- never pruned: the windows the UI promises, including device and file totals
app_boot_total(boot_id, app_id, read_bytes, write_bytes, rchar, wchar, peak_read_bps, peak_write_bps, peak_total_bps,
               coverage_json, PRIMARY KEY (boot_id, app_id));
app_boot_dev_total(boot_id, app_id, dev, rbytes, wbytes, PRIMARY KEY (boot_id, app_id, dev));
app_boot_file_total(boot_id, app_id, path_id, bytes_r, bytes_w, reads, writes,
                    PRIMARY KEY (boot_id, app_id, path_id));
app_lifetime_total(app_id PRIMARY KEY, read_bytes, write_bytes, rchar, wchar, updated_mono_ns);
gap(boot_id, source, epoch, seq, from_mono_ns, to_mono_ns, reason);
```

**Tier selection is disjoint, never blended (B5, narrowed in r4):** window ≤ 24 h →
`sample`; ≤ 30 d → `rollup_*` at 60 s; ≤ 365 d → `rollup_*` at 3600 s; longer → totals
tables only. **Ratified limit on what the UI may promise:** because launch identity is
not carried by rollups or totals, the `launch` window is **disabled** and rendered as a
chip with a stated reason (`launch_attribution`), and any range beyond 365 days is
disabled with its own reason (`unsupported_window`). Both were ratified by the operator:
a consumer MUST NOT silently select or label a wider replacement window. Peaks are
stored as NULL when no rate was ever defined and as `0` when a defined rate rounded to
zero, so "no data" and "measured zero" are distinguishable after pruning; the combined
peak is persisted as `peak_total_bps` because per-component maxima cannot recover
`(read+write)/secs` (the intervals `(100,0),(0,100)` and `(100,100),(0,0)` have identical
component maxima but different combined peaks). Device and residual values are `None`,
never `0`, whenever the underlying accounting is unavailable or incomplete.
Each tier's coverage is stored with it (`sample_coverage` for raw, `coverage_json` for
rollups and totals), so a partial window reports its own gaps instead of borrowing the
whole-boot coverage.

**Storage budget, with arithmetic that actually holds (B7).** Row sizes used below are
measured SQLite payloads including the record header, not estimates:

| Tier | Rows/day | Bytes/row | Per day | Retention | Steady state |
| --- | --- | --- | --- | --- | --- |
| raw `sample` (10 s persistence, 2 s live cache) | 107 apps × 8 640 | 72 | 66 MB | 24 h | 66 MB |
| raw `sample_dev` | 107 × 2 devs × 8 640 | 40 | 74 MB | 24 h | 74 MB |
| raw `file_sample` (top-100 per app per **hour**) | 107 × 24 × 100 = 256 800 | 88 | 22.6 MB | 14 d | 316 MB |
| `rollup_app` 60 s | 107 × 1 440 | 64 | 10 MB | 30 d | 296 MB |
| `rollup_dev` 60 s | 107 × 1 440 × 2 | 56 | 17 MB | 30 d | 518 MB |
| `rollup_*` 3 600 s | 107 × 24 × 3 | 64 | 0.5 MB | 365 d | 181 MB |
| totals tables | fixed | — | — | forever | < 1 MB |

**Hard budget: 1.5 GB**, enforced by a byte-quota check every 5 minutes that prunes
oldest buckets first (raw → 60 s → 3 600 s), never totals. If pruning cannot keep the
DB under quota, sampling pauses and the UI says so — a database that silently eats the
disk would be a worse bug than a gap. `path` rows are garbage-collected when unreferenced.
Selection is deterministic: rank by `bytes_r + bytes_w`, tie-break by `reads + writes`,
then by `(dev, inode, generation)` as a total order. The omitted remainder is stored per
tier in `file_coverage` and displayed, and the Files tab labels its ranking as
"retained observations" because a per-minute top-N can hide a window-leading file.

## 7. Contracts

```python
@dataclass(frozen=True)
class ProcSample:
    pid: int; start_ticks: int; uid: int; dumpable: bool; exe: str | None
    io: dict[str, int]            # rchar, wchar, read_bytes, write_bytes
    cgroup: str | None

@dataclass(frozen=True)
class DevStat:
    cgroup_id: int; dev: str; rbytes: int; wbytes: int

@dataclass(frozen=True)
class FileStat:
    cgroup_id: int; dev: str; inode: int; generation: int; basename: str
    reads: int; writes: int; bytes_r: int; bytes_w: int

# stable external identity, shared by both collectors (N5)
#   unit:app-chromium@<uuid>.service | desktop:google-chrome.desktop | exe:/usr/lib/firefox/firefox
AppKey = str
#   every stored row is namespaced by (collector_id, boot_id, app_key); local integer
#   app_id is a per-database surrogate and is never exchanged between stores.

def resolve_app(cgroup_path: str | None, exe: str | None, desktop_id: str | None) -> AppKey: ...
# precedence: deepest app-* unit > nearest app.slice ancestor > desktop id > exe > session-scope

# appio.store — every window query takes an explicit window and returns coverage
def ranking(window: Window, metric: Metric) -> list[AppTotal]        # each carries coverage
def series(app_id: int, window: Window) -> Series                    # tier chosen per 6
def disk_share(app_id: int, window: Window) -> DiskShare             # dev rows + signed residual
def file_table(app_id: int, window: Window, sort: FileSort,
               direction: Literal["asc","desc"], search: str | None) -> FileTable
def file_coverage(app_id: int, window: Window) -> FileCoverage       # counted loss, not inferred
```

`FileSort ∈ {bytes_total, bytes_r, bytes_w, reads, writes, basename, dev, last_seen}`
(`last_seen` sorts by `file_sample.last_seen_mono_ns`, which is stored per row; the
mockup's `LAST` column is therefore backed by data, not decoration) and the
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
  BYTES R · BYTES W · LAST`. `FILE` renders the **basename only**, because that is
  what the kernel captured; a truncated or unresolved name renders with an explicit
  marker and `last_seen` is the time of the last observed access. Sortable on every
  column including `LAST`. Footer states four things, all of them stored, none of
  them guessed: file activity is **logical** (page-cache reads count, so it is never
  subtracted from the task-accounted total); `counted omissions` from top-N/quota;
  `uncounted overflow` from a full BPF map; and which hooks actually fired
  (`hook_hits_*`), so a silently dead hook cannot masquerade as "no file I/O".
  Rankings are labelled "retained observations" because a per-hour top-N can hide a
  file that led the full window.
- **PEAK** is defined once, in the store: the maximum of `(read_bytes +
  write_bytes) / bucket_secs` over retained buckets, with per-bucket maxima persisted
  as `peak_read_bps`/`peak_write_bps` so pruning to 60 s or 3 600 s buckets cannot
  fabricate or destroy the value. A window answered from totals-only (beyond 365 d)
  shows `PEAK unavailable` rather than a number.
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

`appio-setup` verbs: `install-delegation`, `enable`, `disable`, `status`,
`restart-user-manager`, `export-history`. First use walks the user through **both**
steps in one consent flow — delegation install, then enable — because enabling alone
cannot produce per-app device data (B13). `disable` stops and disables the unit and
deliberately keeps the delegation drop-ins; removing them is `uninstall-delegation`,
out of v1 scope. `disable` also writes a **user-readable export** of the daemon's
history to `~/.local/share/appio/iod-export-<ts>.db`, owned by the invoking user
taken from `PKEXEC_UID`, so turning the feature off does not confiscate the record
(N2). `restart-user-manager` runs `systemctl restart user@<uid>.service` and its
consent dialog says plainly that it ends the user's session (N/ B13). The switch
shows probed state, never the last click; success requires the socket to answer
within 5 s; polkit dismissal (126) reverts the switch with the exit code and first
stderr line.

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

## 13. Findings ledger — review attempt-4 (round 2)

| # | Finding | Resolution in revision 3 |
| --- | --- | --- |
| B1 | vectored I/O bypasses the four hooks | §4.1 six fexit hooks incl. `vfs_readv`/`vfs_writev`; every name verified present in this kernel's BTF; per-hook `hook_hits_*` counters so a dead hook is visible |
| B3 | reap detector under-corrects | §2.3 credit-based correction with carry-forward, `R_reap_unapplied`, `reap_unobserved`, atomic identity read |
| B4 | `unobserved_starts` unknowable | §2.6 replaced with `first_seen_partial`; no invented counts |
| B5 | sample PK, launch identity, missing totals | §6 PK adds `boot_id` + `launch_id`; `app_boot_dev_total`, `app_boot_file_total`; coverage in rollups |
| B6 | epoch allocation, wall offset in buckets | §6 `collector_epoch`/`clock_map`; §2.7 fixed BOOTTIME bucket arithmetic, wall only for display |
| B7 | storage bound arithmetically impossible | §6 measured-bytes table by tier, 1.5 GB hard quota with pruning order and pause-on-quota |
| B8 | lookup-and-delete not lossless | §4.1 double-buffered `ARRAY_OF_MAPS` with epoch flip; §4.1 attach-verification via `hook_hits_*` |
| B9 | rename/link/cgroup-vanish policy missing | §6 `path_alias`, `identity_weak`, `O_NOFOLLOW`-style explicit states; fs policy below |
| B10 | ownership + cross-store identity | §7 `resolve_app` precedence rule, `AppKey` external keys, `cgroup_id` carried on `DevStat`/`FileStat` |
| B12 | helper privilege boundary | §5 `PKEXEC_UID`, six verbs, absolute argv, `O_NOFOLLOW`, parent-directory fsync |
| B13 | probe proves presence, not accounting | §3.3 transient-unit probe with byte tolerance; `delegation present, accounting inactive` state |
| N1 | no handover protocol | §3 `collector_interval` + `handover` with `cutoff_mono_ns` and baselines; overlap excluded, never merged |
| N2 | no historical query; history confiscated on disable | §4.2 bounded `query` messages; §9 `disable` writes a user-owned export DB |
| N3 | per-insert wall offset == realtime | §2.7 fixed BOOTTIME buckets + `clock_map` segments + suspend attribution rules |
| N4 | coverage model cannot support its claims | §6 `file_coverage` splits counted omissions from uncounted overflow, per app; persisted at every tier |
| N5 | two DBs allocate unrelated ids | §7 `AppKey` + `(collector_id, boot_id, app_key)` row namespacing; local ids never exchanged |
| N6 | `i_generation` guarantee overstated | §6 `UNIQUE(fs_uuid, dev, inode, generation, basename)`, `identity_weak` for filesystems without stable generations, weak identities never merged |
| N7 | mockups promise impossible data | §8 basenames only, `LAST` backed by `last_seen_mono_ns` and added to `FileSort`, `PEAK` defined and persisted as per-bucket maxima |

### Supported-filesystem policy (B9, N6)

ext4 on `/`, `/data`, `/vm`: full identity. `tmpfs`: excluded as non-disk, counted in
`skipped_unsupported_fs`. btrfs with snapshots and overlayfs: supported for byte
accounting, but `identity_weak = 1` because inode+generation can repeat within a
superblock; weak identities are stored separately and never merged with a strong
identity. Any future filesystem is treated as weak until a test proves otherwise.

## 14. Delivery decomposition (agreed)

The reviewer's `MUST_DECOMPOSE` is accepted, ordered as **A′ — vertical slice first**:

| Plan | Contents | Gate before the next plan |
| --- | --- | --- |
| P1 contracts | measurement model (§1), aggregation (§2), identity (§7), schema + budget (§6), clock/window semantics | contracts frozen, schema migrations reviewed |
| P2 thin vertical slice | sampler + store + a minimal window showing list, KPI strip and signed residual | a real screen agrees with `/proc` and `io.stat` by hand |
| P3 daemon | privilege surface (§5), activation (§3.3), persistence, IPC (§4.2), handover (§3) | enable/disable round-trip with verified accounting |
| P4 BPF files | six hooks (§4.1), double-buffered drain, coverage accounting | file numbers reconcile against a controlled test workload |
| P5 full UI | Overview + Files + tray + settings + packaging + integrated acceptance | acceptance suite with opt-in privileged tests |

The reason for this order is recorded so it is not relitigated: the first two plans
exist to make contract errors cheap, because every review so far has found contract
errors, not UI errors.
