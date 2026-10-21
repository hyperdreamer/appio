# appio — Per-application disk I/O monitor (design)

Date: 2026-10-21
Status: draft for approval
Target: Arch Linux, KDE Plasma 6 / Wayland, Python 3.14 + PyQt6

## Background

Nothing on a stock Arch desktop answers "which application is wearing out my
NVMe drives, and by how much". `iotop` needs root, is per-process and transient,
and shows no history. `iostat` shows devices but no applications. This machine has
two 1.8 TB NVMe devices (`nvme0n1` holding `/` and `/data`, `nvme1n1` holding
`/vm`), so both "which app" and "which disk" are real questions.

Measured facts that shape the design (verified on the target machine):

| Fact | Observed value |
| --- | --- |
| `/proc/<pid>/io` access | readable for own UID; `PERMISSION DENIED` for root/system PIDs (mode `0400`) |
| Full sampling pass (351 processes) | 19 ms |
| Distinguishing user processes / app names | ~726 PIDs / 107 names / 343 user PIDs |
| User-session systemd app units | 130 |
| cgroup v2 `io` controller | enabled at root, `system.slice`, `user.slice`; **not delegated** below `user.slice` |
| Per-service `io.stat` | present and world-readable for system services (`avahi 259:3 rbytes=569344 …`) |
| Per-app `io.stat` in the user session | absent — `user-1000.slice` exposes only `cpu memory pids` |
| eBPF tooling / unprivileged BPF | none installed / `unprivileged_bpf_disabled=2` |
| polkit | `pkexec` present, `polkit-kde-authentication-agent-1` running |

## Goals

1. Record every application's total disk I/O — reads and writes separately —
   accumulated since the current boot.
2. Rank applications by that total, with read/write/total sortable.
3. Open one application and see its historical I/O as an infographic: per-disk
   lanes over time, read/write composition, disk share, hour-of-day intensity.
4. Show explicitly which disk the I/O landed on, with the measurement residual
   disclosed rather than hidden.
5. Match the tray behaviour of `ai-manager`, which the user already runs daily.
6. For any application, list the **files it accessed**, with per-file read/write
   byte counts and access frequency, sortable by any column.

## Non-goals (v1)

Network I/O, per-process detail below app granularity, alerting/notifications on
thresholds, remote or multi-machine collection, Windows/macOS, retroactive
accounting for units started before setup, and per-access *timestamps* per file
(only the aggregates listed here are retained).

## Decisions taken with the user

| Decision | Choice | Why |
| --- | --- | --- |
| Monitoring scope | **C1**: unprivileged GUI *plus* `appio-iod` root daemon shipped in v1, disabled by default | machine-wide reach exists on day one, enabled with consent |
| Default metric | **Disk I/O** (`read_bytes`/`write_bytes`); all four kernel counters stored | the number that costs SSD wear; logical I/O (`rchar`/`wchar`) available as a toggle |
| Disk attribution | **systemd cgroup `io.stat`** | same block layer as the headline metric, so the split reconciles; no eBPF dependency |
| Accumulation window | **Since boot** for the list default; `since launch` and `lifetime` also stored and selectable | every app measured over an identical window; survives browser restarts |
| Detail view | **Combined A+B**: KPI strip, per-disk timeline lanes (plus a dual-sided TOTAL lane), read/write donut, share-by-disk, hour-of-day heatmap, reconciliation strip | keeps the time axis *and* the composition summary |
| Tray | mirror `ai-manager` | familiar, already part of the user's workflow |
| Root enablement | in-app consent dialog listing exact commands, then `pkexec appio-setup <verb>`; the same switch turns the daemon **on and off**, and the UI always shows the probed state | no surprise privilege escalation, and no one-way door: the user can always undo it from the app |
| Per-file data source | **F1**: eBPF `vfs_read`/`vfs_write` accounting inside `appio-iod` | the only source that yields measured per-file bytes *and* a real access count; clang 23.1.3 + libbpf 1.7 + `/usr/include/bpf` + kernel BTF are already present, so no new package |
| Per-file scope | ships in **v1** | the requirement is part of the app's purpose, not an add-on |

## Architecture

```
┌──────────────────────────── appio (unprivileged, PyQt6) ───────────────────────────┐
│  sampler ──► store (SQLite) ──► model ──► main window (list │ Overview │ Files) ──► tray
│     │                                    ▲                                          │
│     └── reads /proc/<pid>/io (own UID) ──┘                                          │
│     └── reads /sys/fs/cgroup/**/io.stat  (world-readable)                           │
│     └── reads /run/appio/iod.sock  ──────────┐  (optional, for other-UID apps)      │
└──────────────────────────────────────────────┼──────────────────────────────────────┘
                                               │  newline-delimited JSON, read-only
┌──────────────────────────── appio-iod (root, systemd) ──────────────────────────────┐
│  samples /proc/<pid>/io for every PID; reads per-unit io.stat; serves snapshots     │
│  loads appio_fileio.bpf.o (fentry on vfs_read/vfs_write) → LRU map drained ≥1/s     │
└─────────────────────────────────────────────────────────────────────────────────────┘

setup path (once, with consent):  pkexec appio-setup  ──►  drop-in + daemon-reload
                                                        ──►  enable --now appio-iod
```

### Why two processes

`/proc/<pid>/io` is mode `0400`, so other-UID totals require root. Device splits
are world-readable *once the cgroup controller is delegated*, so per-disk data for
the user's own apps needs no daemon. `appio-iod` exists for exactly the gap:
other-UID application totals. It never writes to `/proc`, never accepts commands
over its socket, and answers one query type.

### Enabling and disabling the root daemon (consented, reversible)

The daemon is **switched on and off from the app**, never by asking the user to
type systemctl commands. `appio-setup` is a root-owned helper installed by the
package; it accepts exactly four fixed verbs and nothing else:

| Verb | Effect |
| --- | --- |
| `pkexec appio-setup install-delegation` | writes `/etc/systemd/system/user@.service.d/10-appio-io.conf` (`Delegate=pids memory cpu io`), writes `/etc/systemd/user.conf.d/10-appio.conf` (`DefaultIOAccounting=yes`), runs `systemctl daemon-reload` |
| `pkexec appio-setup enable` | `systemctl enable --now appio-iod.service` |
| `pkexec appio-setup disable` | `systemctl disable --now appio-iod.service` (leaves the delegation drop-ins in place) |
| `pkexec appio-setup status` | prints `is-enabled`/`is-active`/socket presence as JSON; read-only |

Hard rules for the helper:

- `argv[1]` must match one of those four verbs exactly; anything else exits 2
  without side effects. No shell, no user-supplied arguments, no globbing.
- The helper file is root:root mode 0755 and is shipped by the package together
  with a polkit action, so `pkexec` shows a named prompt rather than a generic one.
- `disable` never removes the drop-ins; removing them is a separate explicit
  action (`uninstall-delegation`, out of scope for v1). Turning the service off
  must not silently undo a system configuration the user approved earlier.

UI contract for the toggle (`Settings → System services (root)`):

1. On open, and after every toggle, the app reads state **unprivileged** via
   `systemctl is-enabled appio-iod` + `systemctl is-active appio-iod` + a socket
   probe, so the switch always reflects reality rather than the last click.
2. Turning it **on** opens a consent dialog that lists the exact commands above,
   then runs `pkexec appio-setup enable`. On success the toggle stays on and the
   status line reports the verification result; on polkit dismissal (exit 126) or
   any failure the toggle **reverts** and the status line carries the exit code
   and the first stderr line verbatim.
3. Turning it **off** opens the same dialog with the `disable` command, then
   re-probes. If the daemon is still active, the toggle reverts to on and says so.
4. Success is verified, not assumed: `enable` counts as successful only when the
   socket answers a `status` request within 5 s. A started-but-unreachable daemon
   is reported as `enabled, not answering` rather than shown as working.
5. The toggle is disabled (with an explanation) when `appio-setup` is missing,
   when the process is not on a systemd host, or inside a container.
6. **One owner, deep links.** Exactly one surface can change privilege: the
   `Settings → System services (root)` switch. Everywhere else that reports the
   degraded state — the main-window banner and the `Files` tab empty state —
   carries an `Open settings` button that only **navigates** to that row and
   focuses it. The tray menu carries no copy of the control at all: a quick-action
   menu is the wrong place to host a slow, root-level, reversible decision. No
   surface requires a terminal, and no surface duplicates the switch's state or
   its consent dialog.

Steps 1–3 of `install-delegation` only take effect for **new** user units, so the
UI states plainly: "log out and back in to activate per-app disk attribution",
and then verifies by probing for `io.stat` under `app.slice/<unit>/`. Until
verified, the app renders `nvme*` lanes for system services only and labels user
apps' device split `unavailable until re-login`.

## Components

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `appio.procs` | enumerate PIDs, read `/proc/<pid>/io`, `/proc/<pid>/stat`, `/proc/<pid>/cgroup`; tolerate `ESRCH`/`EACCES` | `/proc` |
| `appio.cgroups` | parse `io.stat` for a unit path; map `major:minor` → device name via `/proc/diskstats` + `/sys/block` | `/sys/fs/cgroup` |
| `appio.identity` | resolve an app key from a cgroup unit (`app-<desktopid>@<uuid>.service`) or executable path; merge that app's processes across PIDs | `appio.procs` |
| `appio.sampler` | take one snapshot per tick; compute per-launch deltas; append rows; never block the UI thread | `procs`, `cgroups`, `identity`, `store` |
| `appio.store` | SQLite schema, migrations, insert/rollup/retention, query API for the UI | sqlite3 |
| `appio.iodclient` | connect to `/run/appio/iod.sock`, parse snapshots, degrade to own-UID when absent | socket |
| `appio.iod` (daemon) | same snapshot logic for all PIDs, served read-only as NDJSON; loads and drains the per-file BPF map | `procs`, `cgroups`, BPF |
| `appio_iod/bpf/fileio.bpf.c` | fentry `vfs_read`/`vfs_write` → `bpf_get_current_cgroup_id()`; LRU hash keyed `(cgroup_id, dev, inode)`; value `{reads, writes, bytes_r, bytes_w, name[64]}`; captures the dentry name on first insert | libbpf, kernel BTF |
| `appio.files` | maps `cgroup_id` → unit path via kernfs inode = cgroup id; resolves inodes to paths; merges map dumps into `file_sample`; enforces top-N caps | `store` |
| `appio.ui.listview` | sorted application list (proxy model over the query API) | store |
| `appio.ui.detailview` | approved infographic pane | store |
| `appio.ui.tray` | `QSystemTrayIcon` mirroring ai-manager | Qt |
| `appio.ui.settings` | cadence, retention, tray options, system-service toggle | store, `appio.setup_client` |
| `appio.setup_client` | consent dialog + `pkexec appio-setup <action>` + verification probes | polkit |

## Data model (SQLite, `~/.local/share/appio/history.db`)

```sql
boot(boot_id TEXT PRIMARY KEY, started_at REAL NOT NULL);
app(app_id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    icon_path TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL);
launch(launch_id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES app(app_id),
       boot_id TEXT NOT NULL REFERENCES boot(boot_id), started_at REAL NOT NULL,
       ended_at REAL, proc_count INTEGER NOT NULL DEFAULT 0);
-- cumulative snapshot of a launch, so deltas are trivial and restarts are visible
sample(launch_id INTEGER NOT NULL REFERENCES launch(launch_id), ts REAL NOT NULL,
       rchar INTEGER, wchar INTEGER, read_bytes INTEGER, write_bytes INTEGER,
       PRIMARY KEY (launch_id, ts)) WITHOUT ROWID;
-- per-device cumulative counters from the app's cgroup, when available
sample_dev(launch_id INTEGER NOT NULL, ts REAL NOT NULL, dev TEXT NOT NULL,
           rbytes INTEGER, wbytes INTEGER, PRIMARY KEY (launch_id, ts, dev)) WITHOUT ROWID;
rollup(app_id INTEGER NOT NULL, bucket_start REAL NOT NULL, bucket_secs INTEGER NOT NULL,
       read_bytes INTEGER, write_bytes INTEGER, rchar INTEGER, wchar INTEGER,
       PRIMARY KEY (app_id, bucket_start, bucket_secs)) WITHOUT ROWID;
-- per-file access, filled only while the daemon runs (F1)
path(path_id INTEGER PRIMARY KEY, text TEXT UNIQUE NOT NULL);
file_sample(app_id INTEGER NOT NULL REFERENCES app(app_id), bucket_start REAL NOT NULL,
            dev TEXT NOT NULL, inode INTEGER NOT NULL, path_id INTEGER NOT NULL REFERENCES path(path_id),
            reads INTEGER NOT NULL, writes INTEGER NOT NULL,
            bytes_r INTEGER NOT NULL, bytes_w INTEGER NOT NULL,
            PRIMARY KEY (app_id, bucket_start, dev, inode)) WITHOUT ROWID;
```

Per-file sizing and retention: the BPF LRU map holds at most 65 536 entries
(≈6 MB), evicting least-recently-used files first; the daemon drains at least
once per second; the store keeps 1-minute `file_sample` buckets for 24 h and only
the **top 500 files per app** per bucket, so a pathological app cannot fill the
disk. Evicted or capped files are reported as part of the
`not attributable to a file` row rather than dropped silently.

- **Since boot** = Σ deltas whose `boot.boot_id` = the current `/proc/sys/kernel/random/boot_id`.
- **Since launch** = Σ deltas for the app's current `launch` row.
- **Lifetime** = Σ over all rows for the `app`.
- **Residual** = launch total − Σ `sample_dev` bytes for that launch, reported for
  the window; never silently merged into the disk split.
- Retention: raw `sample*` 24 h at the configured cadence; `rollup` 1-minute
  buckets for 30 days and 1-hour buckets for 365 days; a nightly job prunes in
  one transaction.
- Timestamps are wall clock for display; deltas are derived from consecutive
  snapshots so a clock jump cannot create negative or phantom bytes.

## Contracts

```python
# appio.procs
@dataclass(frozen=True)
class ProcSample:
    pid: int; start_ticks: int; uid: int
    io: dict[str, int]            # rchar, wchar, read_bytes, write_bytes
    cgroup: str | None            # e.g. app.slice/app-chromium@…​.service

def sample_procs(*, uids: set[int] | None = None) -> Iterator[ProcSample]: ...

# appio.cgroups
@dataclass(frozen=True)
class DevStat:
    dev: str                      # "nvme0n1"; "unattributed" is a caller concept
    rbytes: int; wbytes: int

def unit_io_stat(cgroup_path: str) -> list[DevStat]: ...   # [] when not delegated
def dev_name(major_minor: str) -> str: ...                 # "259:3" -> "nvme0n1"

# appio.store
def record(snapshots: Sequence[LaunchDelta], dev: Mapping[str, Sequence[DevStat]], ts: float) -> None: ...
def ranking(window: Window, metric: Metric) -> list[AppTotal]: ...
def series(app_id: int, window: Window, bucket_secs: int) -> list[Bucket]: ...
def disk_share(app_id: int, window: Window) -> DiskShare: ...   # includes residual

# appio.files
def record_files(app_id: int, ts: float, stats: Sequence[FileStat]) -> None: ...
def file_table(app_id: int, window: Window,
               sort: Literal["reads","writes","bytes_r","bytes_w","path"]) -> list[FileRow]: ...
def file_residual(app_id: int, window: Window) -> int: ...   # app total - sum(file rows)
```

Daemon wire format, one JSON object per line, server → client only:

```json
{"ts": 1761000000.0, "pids": 726,
 "procs": [{"pid": 1234, "start_ticks": 88213, "uid": 0,
            "rchar": 10, "wchar": 20, "read_bytes": 30, "write_bytes": 40,
            "cgroup": "system.slice/sshd.service"}],
 "devs":  [{"cgroup": "system.slice/sshd.service", "dev": "nvme0n1",
            "rbytes": 4096, "wbytes": 0}],
 "files": [{"cgroup": "app.slice/app-chromium@x.service", "dev": "nvme0n1",
            "inode": 412345, "name": "data_1",
            "reads": 12481, "writes": 3, "bytes_r": 191230771, "bytes_w": 1258291}],
 "files_dropped": 0}
```

## UI

The approved mockups are the specification; reproduce them as drawn.

- Detail view (approved): `docs/superpowers/specs/assets/2026-10-21-detail-view.png`
- Scope and daemon split (approved): `docs/superpowers/specs/assets/2026-10-21-scope-and-daemon.png`


- **Main window**: left application list (name, "since boot" total, inline bar,
  search field), right detail pane; window title `appio`. Sorted by total disk
  bytes descending by default; sortable by read, write, total; a metric switch
  toggles disk ↔ logical.
- **Detail pane**, top to bottom: KPI strip (`TOTAL` with lifetime sub-line,
  `READ`, `WRITE`, `PEAK`); timeline with three lanes — `TOTAL` dual-sided
  (read up, write down) above `nvme0n1` and `nvme1n1` stacked lanes — with
  range chips in the title bar (`1h 6h 24h boot 30d`); read/write donut;
  share-by-disk bars; hour-of-day intensity strip; reconciliation strip
  (`reconciled … within 1.8%`, `residual …`).
- **Files tab** (beside `Overview`, see `assets/2026-10-21-per-file.png`): search
  field, a sort indicator, and a table of `FILE · DISK · READS · WRITES ·
  BYTES R · BYTES W`, sortable by clicking any column (path sorts
  lexicographically, counters numerically). `READS`/`WRITES` are `read()`/
  `write()` syscall counts. A footer row states the part of the app total that
  is **not attributable to a file** (`mmap`, `io_uring`, files evicted from the
  BPF map), so the table never pretends to sum to the KPI total.
- **Tray** (mirrors `ai-manager/ui/system_tray.py`): dynamic `Show`/`Hide`
  item, single-click (`Trigger`) toggles the window, separator-separated
  domain actions — `Pause sampling`, `Open data folder` — then `⚙ Settings…`,
  `About appio`, `Quit appio`; `close_to_tray` defaults to true;
  `start_minimized` supported with `--minimized` / `--tray`; icon is
  palette-tinted and re-tinted on `paletteChanged`;
  `app.setQuitOnLastWindowClosed(False)`, `app.setDesktopFileName("appio")`.
- **Settings**: sampling cadence (1–10 s, default 2), retention, tray options,
  autostart `.desktop` generation, and the `System services (root)` switch that
  turns `appio-iod` on or off with a consent dialog and `pkexec` (see above).
  A status line under the switch always shows the probed reality:
  `off` · `on, answering` · `enabled, not answering` · `on, delegation needs a re-login`.

## Error handling and degradation

| Condition | Behaviour |
| --- | --- |
| `ESRCH` / process exits mid-scan | skip silently, never a gap error |
| `EACCES` on `/proc/<pid>/io` | mark app `scope=system, unattributed` when the daemon is off |
| `io.stat` missing for a unit | device split shows `not delegated`; totals unaffected |
| `appio-iod` absent or socket refused | main-window banner "system apps are not measured" with an `Open settings` button that focuses the Settings switch; own-UID sampling continues |
| SQLite write failure | retry once, then pause sampling, keep UI responsive, surface a non-modal banner |
| DB corruption | move to `history.db.corrupt-<ts>`, recreate schema, notify |
| `pkexec` dismissed/failed | toggle reverts to the probed state, status line carries the exact exit code (126 = dismissed) and first stderr line |
| `enable` returns 0 but the socket never answers | status line reads `enabled, not answering`; the switch stays on, the banner keeps saying system apps are unmeasured |
| daemon enabled outside the app (pacman hook, admin) | next probe reflects it; the UI follows reality and never fights the admin |
| `disable` while the GUI is sampling | the GUI drops to own-UID mode within one tick, keeps its history, and the `Files` tab explains why per-file rows stopped |
| `appio-setup` missing or container host | switch is rendered disabled with the reason, instead of failing after a polkit prompt |
| Delegation installed but not yet re-logged-in | detail view states `unavailable until re-login`, with a probe re-check button |
| Clock jump / suspend | deltas derived from consecutive snapshots; a gap becomes one bucket, never negative bytes |
| BPF program fails to load (no BTF, verifier error, old kernel) | `Files` tab shows `unavailable: <errno and verifier line>`; sampling and the Overview tab are unaffected |
| BPF map full | LRU eviction; evicted files roll into the `not attributable to a file` row, count surfaced in the UI |
| daemon restarted mid-window | map drains are cumulative-only in the store; a restart starts a fresh drain sequence, never a negative delta |
| unprivileged user, daemon disabled | `Files` tab explains that per-file data requires the daemon and offers the same `Open settings` button, so the empty state leads somewhere instead of dead-ending |

## Testing strategy

- Unit tests with a **fixture `/proc` and `/sys/fs/cgroup` tree** on disk; no test
  requires root, real processes, or a live session.
- `appio.procs`: parsing, `ESRCH` races, uid filtering, PID reuse detection via
  `start_ticks`.
- `appio.cgroups`: `io.stat` parsing including multi-device lines, `major:minor`
  → `nvme0n1` mapping, missing-file returns `[]`.
- `appio.identity`: `app-chromium@<uuid>.service` → desktop id; exe-path fallback;
  stable keys across restarts.
- `appio.store`: since-boot/launch/lifetime windows, residual arithmetic
  (property test: `dev_sum + residual == launch_total`), retention pruning,
  migration idempotency.
- `appio.sampler`: cadence drift, first-tick baseline, launch rollover on restart.
- `appio.iodclient`: NDJSON framing, truncated line, daemon disappearance.
- UI with `pytest-qt` and `QT_QPA_PLATFORM=offscreen`: list sorting, metric
  toggle, detail view series plumbing, tray menu text toggling
  (`Show`/`Hide`), consent dialog text.
- Golden test for the reconciliation strip so "1.8%" cannot silently become a lie.
- `appio.files`: synthetic BPF map dumps → `file_sample` rows; top-N capping;
  `cgroup_id` → unit-path resolution from a fixture kernfs tree; residual
  arithmetic (`sum(file bytes) + file_residual == launch total`).
- BPF program: aggregator logic compiled with a userspace shim and asserted on
  synthetic key/value vectors; an integration test that actually loads the object
  is skipped unless `os.geteuid() == 0` and BTF is present, so CI never fakes a
  pass.
- `Files` tab: sort by every column, search filtering, residual footer text.
- `appio.setup_client`: verb allowlist (`argv[1]` only), exit-code mapping
  (0/126/127/other), toggle reverts on dismissal, status probing with a fake
  `systemctl` on `PATH`, and the enabled-but-unreachable case reported as such.
- The two deep links (main-window banner, `Files` tab empty state) are driven in a
  `pytest-qt` test that asserts each one lands on the Settings row with the switch
  focused and **changes no state** — the failure this guards against is a "helpful"
  button that quietly starts a privileged operation.
- Only one widget may own the switch: a test asserts the tray menu contains no
  `System services` action, so a future edit cannot reintroduce a second surface.
- `appio-setup`: invoked with a wrong verb or extra arguments, it must exit 2 and
  touch nothing (asserted against a tmp root).
