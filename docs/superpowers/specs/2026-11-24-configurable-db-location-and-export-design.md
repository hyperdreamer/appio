# Configurable database location and history export — design

- **Topic:** `configurable-db-location-and-export`
- **Run:** `pm-run-20261124-053737-08935c25`
- **Date:** 2026-11-24
- **Status:** revision 31 — retention mappings are durable transaction state with recovery precedence and system collision handling; independent review pending
- **Approved intent:** both databases get a location setting; export produces one
  complete database file; changing a location moves the data and leaves nothing behind.

## 1. Background

appio has two independent databases and neither location can be chosen by the person
who owns the data. Neither can be exported from the window.

| Fact | Where |
| --- | --- |
| Console database default: `$XDG_DATA_HOME/appio/history.db`, else `~/.local/share/appio/history.db` | `src/appio/__main__.py:12-17` |
| `--db` overrides it for one launch only; nothing persists a choice | `src/appio/__main__.py:21`, `:58` |
| The console opens that file twice: a bootstrap `AppIoStore`, and the managed source `local`, whose URI the history router reads and whose actor owns its own SQLite connection | `src/appio/ui/collector_window.py:2183`, `:2189-2190`; `src/appio/history_source.py:530-561` |
| The console's local collector writes the same file through `LiveCollector(database_path=...)`; the factory closes over the startup path | `src/appio/ui/collector_window.py:2243-2251`; `src/appio/live.py:261-291` |
| Managed sources can be retired and re-registered at runtime | `src/appio/history_source.py:783`, `:847` |
| Daemon database default `/var/lib/appio/iod.db`; the daemon refuses to run unless root and never reads a database path from the environment | `src/appio/iod.py:131-141`, `:1022-1031` |
| The shipped unit runs with `ProtectSystem=strict` and `ReadWritePaths=/var/lib/appio /run/appio` | `data/systemd/appio-iod.service:22-26` |
| The daemon requires its database parent to be root-owned, mode exactly `0711`, and the database root-owned, mode `0600`, single link | `src/appio/iod.py:350-371`, `:471-477` |
| The daemon reads `setup-state.json` from the database's own directory | `src/appio/iod.py:590` |
| The root export helper has a hard-coded source `/var/lib/appio/iod.db` and a 15 s backup deadline | `src/appio/history_export.py:28`, `:29`, `:348` |
| The GUI waits 120 s for any helper invocation | `src/appio/ui/collector_window.py:135`, `:2133` |
| `export-history` exists as a helper verb, but no window control runs it for its own sake; the window only imports the export produced by **disable** | `src/appio/setup_boundary.py:88-100`; `src/appio/ui/collector_window.py:1943-1961` |
| The helper's result decoder rejects any field outside its exact key set | `src/appio/setup_client.py:43-56`, `:94-114` |
| A privileged, persisted setting already exists end to end: `set-sample-period` writes a canonical drop-in, reloads, conditionally restarts, and verifies | `docs/superpowers/specs/2026-11-17-sample-period-settings-design.md` |
| Nothing in the window opens a file chooser or writes a settings file today | no `QFileDialog`/`QSettings` in `src/appio` |

## 2. Goal

Two independent, remembered choices — one per database — plus an export that produces
one complete database file.

Success criteria, each observable:

1. **Defaults unchanged.** With no settings file and no drop-in, both databases resolve
   exactly where they do today, and a fresh install behaves identically to 0.1.10.
2. **The console setting persists.** After choosing a new path, the window says a restart
   completes the move; on the next start the history is moved, the console reads and
   writes the new file, the old path is gone, and the window's history still shows the
   moved records.
3. **The system setting persists.** After a consented change, the collector reports the
   new database path, the old path is gone, and the service runs exactly when it ran
   before the change.
4. **Moving keeps the data.** The moved file opens, passes `integrity_check`, passes the
   store's schema validation, and has the same per-table row counts as the source.
5. **Export produces a usable database.** The exported file passes `integrity_check` and
   schema validation, is mode `0600`, never overwrites an existing file, and can itself
   be used as either database.
6. **Failure changes nothing.** A destination that already exists, an unwritable
   directory, a corrupt source, a refusing service, or an indeterminate configuration
   leaves the old file and the old setting untouched and reports one exact reason. A
   cancelled consent reports `cancelled` and changes nothing.
7. **No writer is lost and no second writer is created.** At every completed change the
   console's local collector and the local managed source are on the new file or
   stopped, and the daemon holds either its old or its new file, never both.

## 3. Non-goals

- CSV, JSON, or spreadsheet output; any export format other than one database file.
- Importing a database produced elsewhere, merging two databases, or "open this file".
- Per-user system databases, or running the root collector without root.
- Changing retention, quota, rollup granularity, or schema.
- Migrating the delegation drop-ins, the socket, the staging directory, or
  `setup-state.json`; only the history database moves.
- Making `--db` persist. It stays a one-launch override.

## 4. Decisions

**D1 — Two settings, two stores, no shared configuration file.**

- The console's choice is user data: `$XDG_CONFIG_HOME/appio/settings.json`, defaulting
  to `~/.config/appio/settings.json`. Canonical bytes, one trailing LF, mode `0600`,
  written by the console's own atomic replace. The document also carries
  `"generation"`, a monotonically increasing integer the console uses to notice that
  another window changed the setting (D16).
- The system choice is machine configuration: the drop-in described in D6, written only
  by the root helper.
- Neither file is a cache: deleting either returns that database to its default.

**D2 — The console settings file is exact and refuses foreign content.**

```json
{"database_path":"/absolute/path/history.db","generation":3,"schema":1}
```

Canonical serialization is `json.dumps(..., sort_keys=True, separators=(",", ":"))` plus
one LF. `parse_settings(data) -> Settings | None` returns a value only when the bytes have
exactly these three keys: `schema` is the integer `1`, `database_path` is an absolute string
with no `..` component and no control character, and `generation` is a positive integer.
Any other non-empty content is *not ours*:

- at startup with no pending transaction, a foreign or unparseable file is ignored with one
  `settings_invalid path=<path>` log line and the default is used. With a pending transaction,
  `settings_foreign` preserves settings bytes, record and both databases and refuses migration;
  SQLite is never opened on a missing source pathname through the fallback.
- at change time, it is **refused** (`settings_foreign`) rather than overwritten, because
  canonical bytes cannot prove authorship and silently replacing a file the console did not
  write is how a user loses a hand edit.

An absent file is the default state and is created by the first successful change. **A
pending move is not a settings key**: it lives in its own record (D5), so the settings shape
never changes when the feature is used (review R5-1).

**D3 — Console resolution order is `--db`, then the setting, then the default.**

`--db` keeps its meaning and is never written to the settings file. While `--db` is in
effect the Settings row shows the override and `Change…` is disabled with the reason
("this launch was started with `--db`"), because a change made there would not take effect
on the next launch. A launch with `--db` also **skips any pending move**: the record stays
for the next normal launch, and the override path is what that launch uses (review R5-6).

**D4 — One file procedure: `src/appio/database.py`.**

Three functions own every byte of this feature. Both the console and the root helper call
the same module, so the rules cannot drift apart. It imports `sqlite3`, `os`, `pathlib`,
`json`, and `migrations.validate_schema`; no Qt, no setup module, no privilege assumption
(`migrations` imports only `contracts` and `schema`).

```text
guard_source(existing_path) -> Guard            # rw connection holding BEGIN IMMEDIATE
copy_verified(source, destination, *, record, guard) -> CopyReceipt
publish_verified(receipt) -> Published             # link + inode check, never rename
retire_source(record, guard) -> Deferred | Retired # holder scan + manifest-driven rename
delete_retired(record) -> Deleted | Retained(reason)
export_verified(source, destination, *, record) -> CopyReceipt
```

The **guard is a first-class argument with an explicit lifetime** (R12-2): `guard_source`
accepts the existing authoritative pathname chosen by recovery, returns the open read-write
connection holding `BEGIN IMMEDIATE`, `copy_verified` and `retire_source` require it, and it is
closed after source retirement and deletion complete when the guard protects the original
source. Recovery classifies the durable record and directory entries before opening SQLite.
Branches 1/3 that copy and retire a source keep that source's guard through retirement.
Branches 2/2b that have already retired or deleted the source validate destination identity and
settings durability, then close any destination guard **before** starting the system daemon.
They preserve the record and retired entries through startup/status verification. Branch 2b
requires no source guard because no source retirement remains. An absent source path is never
opened with SQLite's create behavior. `copy_verified` and `retire_source` always take the same
live original-source guard when they perform those operations (R25-1).

`record` is the caller's durable move record (D5); `database.py` only reads and updates the
fields it owns (`temp_name`, `temp_identity`, `published_identity`, `stage`).

**Trust model, stated once.** For the console the destination directory is the user's own
chosen directory, where that user already controls their own data, so no privilege boundary
is crossed. For the root helper it is a root-owned `0711` directory that no other account
can write (D14). Inside that boundary the procedure pins the directory with one
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC` open (the helper uses `setup_boundary`'s
component walk) and does every create, link, and unlink through that descriptor. It does
not claim to defeat an attacker who already has the database owner's privilege; it claims
that no operation follows a symlink or acts on an unpinned name, and that a substituted
entry is detected before it can be published.

`copy_verified` rule sequence:

1. Both paths absolute; source and destination are not the same file by `(st_dev, st_ino)`.
2. **Guard the source with a real lock.** Open the source read-write and take
   `BEGIN IMMEDIATE` with `busy_timeout=0`: `IMMEDIATE` takes SQLite's `RESERVED` lock, so
   no other connection can commit while it is held, and a database that already has a
   writer refuses `database_in_use` immediately. (Round 4 showed that `BEGIN EXCLUSIVE` on a
   *read-only* connection proves nothing.) The guard connection stays open, holding the
   lock, until the source has been renamed and the retired file deleted — not merely until the
   copy ends.
3. **Copy from a separate reader.** The backup source is a **second, read-only**
   connection, because Python's `backup()` on the connection that holds `BEGIN IMMEDIATE`
   stalls on a WAL database (round-5 probe). The reader sees the pre-write snapshot, which
   is exactly what the guard fixes in place.
4. **Temporary, recorded in two steps.** The temporary name
   `.appio-move-<operation_id>.tmp` is written into the record with
   `stage: "CREATING"` and no inode **before** the file exists. The file is then created
   with `O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0600`, and the record is updated
   with `temp_identity` and `stage: "COPIED"`. A crash between the two leaves a `CREATING`
   record naming a file whose name contains this operation's unique id; recovery stats that
   name, and adopts it as this operation's temporary when it is a regular, root-owned (or
   caller-owned, for the console) file matching the recorded name — then re-records its
   inode and continues. A file at that name that is not a regular file, or is owned by
   someone else, refuses `temporary_conflict` (review R6-1).
5. **Copy** with `backup(..., pages=256)` into the temporary file, held at
   `PRAGMA journal_mode=MEMORY` so a WAL source cannot turn it into a WAL database
   mid-copy; afterwards switch it to `journal_mode=DELETE` and remove any
   `-wal`/`-shm`/`-journal` sidecar the switch leaves, through the pinned directory.
6. **Verify the copy**: `integrity_check(1) == ok`, `validate_schema(connection)`, and
   per-table row counts recorded in the receipt. The counts are not compared with the live
   source: the guard in rule 2 already fixed the snapshot, and a comparison would be the
   only racy part (F19, R2-8).
7. `fstat(tempfd)` must equal `lstat(tempname)` in `(st_dev, st_ino)`; otherwise
   `verification_failed` and nothing is published.
8. `fsync(tempfd)`, close it, then **publish without replacement**:
   `os.link(tempname, destination, src_dir_fd=dirfd, dst_dir_fd=dirfd)` (default
   `follow_symlinks=True`, so a swapped symlink would link its target), then
   `lstat(destination)` must equal the recorded temporary inode with `st_nlink == 2`. A
   mismatch unlinks the destination this call just created and refuses
   `verification_failed`. `os.rename` is never used, because it replaces an existing
   destination (R4-2; round-5 probe confirms `os.link` raises `EEXIST` and leaves the
   existing file intact).
9. Record `stage: "PUBLISHED"` and `published_identity = lstat(destination)` in the record,
   then `fsync` the record, then unlink the temporary name (leaving one link) and `fsync`
   the directory descriptor. The stage write happens **after** the link, so a crash in this
   window is reconciled by the next rule rather than guessed.
10. Return `CopyReceipt{source, destination, bytes, schema_version, tables, verified_at,
    source_identity, published_identity, operation_id}`.

**Retirement is a complete manifest written before the first rename (R12-1).** The record
carries `retired: [{original, retired, dev, ino}]`, one entry for **every** file retirement will
move — the main database and every sidecar present — and the *whole* array is written and
`fsync`ed before the first rename, and the record's **parent directory is `fsync`ed after that
write** so the manifest itself survives a power loss that the renames survive. (Round 13
injected a crash at each of 29 boundaries: a per-entry append left nine of them with an
unrecorded sidecar at its old name, while recording the complete inventory first recovered
every case; round 14 showed the same loss is possible if the record's directory entry is not
flushed before a rename lands.) The renames then happen in a fixed order: **the main file first** (so the old path
disappears atomically and no process can open an incomplete pair), then each sidecar. Recovery
treats `retired` as the authoritative list: a file whose entry exists and whose name still sits
at `original` is renamed forward; a file at `retired` is deleted (or restored, in branch 4) in
reverse order; an entry whose `retired` name is absent is already finished. "No retired file"
therefore means *every* recorded entry is absent, which is what branch 2b tests.

**Manifest moves preserve occupied targets.** Every source-retirement move, sidecar move,
reverse-order restore, and retired-to-retained relocation uses Linux `renameat2(..., RENAME_NOREPLACE)` relative to pinned directory
descriptors, with no checked-then-rename fallback. A target matching its own recorded entry is
an idempotently completed move. An existing target with any other identity returns
`recovery_target_exists`, preserves both target and retired/original entry and the full record,
and keeps the affected service stopped. Unsupported atomic no-replace facilities refuse before
retirement begins. The operation never invokes ordinary replacing rename for a manifest entry.
This rule applies to D4 branch 4, every forward-recovery rename, and D5/D11 rollback before any
caller reopens the source path (R28-1). Ordinary atomic replacement remains limited to configuration
files and the record-bound verified-destination replacement transaction.

**The source is retired by rename, never compared by metadata (R10-1, R10-2).** A main-file
`(size, mtime_ns)` fingerprint cannot see a committed WAL-only change — round 10 committed a
row into the WAL, left the main file byte-identical, and the fingerprint called an outdated copy
fresh. Rather than trying to fingerprint committed state, the design removes the window in which
a comparison would be needed: the guard is held across the copy, the publication, and a
**rename of the source to `.<source name>.retired-<operation_id>`** in its own directory, and the
settings commit happens only after that rename. The completed move selects the copied history at
the destination and removes the old database and recorded sidecars. Current-version appio entry
points participate in the application lock and re-read configuration after acquiring it. A
separately launched legacy binary or external SQLite client may subsequently create a new file
at a removed pathname. Such newly created history belongs to that independent process and is
outside this feature's migration operation. Existing legacy holders discovered before retirement
still defer the operation, preserving their source history.

**Recovery, in this order** (the record names the `retired` manifest once written):

0. **Retention manifest present** → reconcile its complete retired/retained inode pairs before
   evaluating any numbered source-retirement branch. For each entry, a matching inode at the
   retained name is a completed relocation, a matching inode only at the retired name is moved
   forward with RENAME_NOREPLACE, and a different inode at either expected target is
   `recovery_target_exists` with all files and the record preserved. Both names absent while the
   entry is uncompleted is `recovery_missing_entry`, which preserves the record. Matching hard
   links are reconciled by recorded identity. After every entry is at its retained name, fsync
   the source directory, durably report all retained paths, and remove the record. Branch 2b may
   remove a record only when no retention manifest exists or this retention finalization has
   already completed and been fsynced (R30-1).

1. **Destination published and the source exists at its own name** → first verify the source
   `(st_dev, st_ino)` equals `record.source_identity`. A mismatch refuses `source_changed`,
   retaining destination, source and record; no cleanup path may remove the published copy
   (R18-1/R18-3). Next scan for foreign holders; any holder returns `source_in_use` with both
   copies and the record retained. With matching identity and no holder, acquire a fresh guard
   and recopy to a fresh temporary. Persist
   `replacement:{stage:"PREPARED",old_identity,new_temp_identity,backup_name}` and fsync its
   parent before touching the current destination. Rename the old published destination to its
   unique backup and fsync the directory, then link the new temp to the final name, verify its
   inode, and fsync the destination directory **before** recording `replacement.stage:"PUBLISHED"`
   or removing the backup (R19-1). Recovery is explicit for each state:

   - `PREPARED`, old destination at final, new temp present → move old to backup, fsync, link
     new, fsync, record PUBLISHED.
   - `PREPARED`, backup present, final absent, new temp present → link new, fsync, record PUBLISHED.
   - `PREPARED`, final matches new temp identity → link landed; fsync destination directory,
     record PUBLISHED.
   - `PUBLISHED`, final matches new identity, backup present → retain both until settings are
     DURABLE, then remove backup.
   - final and backup absent, temp present → source still matches its recorded inode, so recopy
     under a new operation id.
   - Any unrecognized inode combination → refuse and retain every file and record.

   The old verified destination remains until the new final link and PUBLISHED record are
   directory-durable. Source retirement begins only after that point (R18-4).
2. **Destination published and any recorded retired entry exists** → complete the manifest:
   rename forward any entry still at its original name, then **confirm settings durability**
   (R12-3): a directory `fsync` must succeed before anything is deleted, even when the settings
   already name the destination. On an `UNPROVEN` outcome the retired files and the record are
   kept and the start reports `settings_durability_unknown`. Only a `DURABLE` outcome deletes
   every retired entry and removes the record.
2b. **Destination published, source absent, all recorded retired entries absent** → the retirement
   finished; the settings are confirmed durable exactly as branch 2 requires, and only then is the
   record removed. An `UNPROVEN` outcome keeps it.
3. **No destination, source at its own name** → copy again from the source.
4. **No destination, no source, a retired entry present** → restore the manifest in reverse order
   (sidecars first, main last), `fsync`, and copy again.
5. **No destination, no source, no retired entry** → refuse `source_missing`; the record is left
   untouched.
6. **A destination with any other inode** → refuse `destination_exists`.


Every branch is a function of three existence facts and one holder scan, so the state machine is
total. A crash before the rename leaves the source authoritative and the settings untouched (the
only price is a repeated copy). A crash after the rename leaves the settings naming a path that
no longer exists plus a record naming the retired file, which branch 2 completes; that window is
unreachable by any other process because the exclusive application lock is held through it.

`delete_retired(record)` re-scans `/proc/<pid>/fd` for the **retired** inode. When a foreign
holder still exists — a process that opened the file between the pre-rename scan and the rename
— the files are retained under `<source name>.retained-<timestamp>-<operation_id>`. Before
any retention rename, atomically write the **complete** mapping into the same durable
transaction record as `retention:{state:"PREPARED",entries:[{retired,retained,dev,ino}]}`,
fsync the record file and its parent directory, then perform relocations. Every main/sidecar
entry is included before the first move. Recovery applies retention branch 0 first, so partial
retention cannot be mistaken for completed deletion. Each move uses
`renameat2(..., RENAME_NOREPLACE)` through the pinned directory. A target matching its recorded
inode completes idempotently. An occupied target with another inode returns
`recovery_target_exists`, preserves target, retired source, every manifest entry and record, and
keeps the affected service stopped. The record is removed only after **all** retention moves
and source-directory fsync succeed. Then `retired_in_use` reports the retained path. These
relocations preserve any previous retained database and the raced opener's current file
(R29-1). With no holder, the retired files are removed through the
pinned parent, refusing a replaced inode and tolerating absent sidecars. A stale `.retired-*`
file with no record is removed by the next start only when it is owned by the caller and its
name contains an operation id no record references.


`export_verified` is rules 4–10 only: **no writer guard, no source deletion**. An export runs
against the live database and must not block collection; a consistent online backup does not
need the guard, and round 8 measured a real write failing after five seconds while the guard
was held (R8-5). It returns the receipt and the caller removes its record when the export
completes.

**D5 — A console change is recorded now and performed at the next start.**

A running console has a bootstrap store, a managed-source actor, a local collector, and
possibly a second console process with its own connection; moving the file underneath all of
that cannot be made safe without stopping writers the console does not own (round 4
demonstrated a gap commit landing during a copy). So the window never moves a file while the
application runs. It records the intent, and the move happens at start-up, before any
window, actor, or collector exists.

The pending-move record and all operation coordination use
`<passwd home>/.local/state/appio/pending-move.json` and
`<passwd home>/.local/state/appio/settings.json.lock`, derived from `pwd.getpwuid(os.getuid())`.
The startup log is `<passwd home>/.local/state/appio/startup-move.log`. These coordination
paths ignore HOME, XDG_STATE_HOME and XDG_CONFIG_HOME. The settings file retains D1's selected
XDG_CONFIG_HOME semantics, and the default database retains the existing XDG_DATA_HOME semantics.
The single pending record carries the absolute selected `settings_path`, captured
`resolved_source`, and `settings_generation`, plus `settings_was_absent`. Processes with a
different selected settings path or default source detect that binding under the same canonical
lock, report `changed_elsewhere` and preserve the pending operation. A second environment cannot
create a second pending record or cancel an operation from another settings profile. Thus
per-launch XDG defaults are supported while all pending migration ownership is account-wide
(R26-1). The pending record is mode `0600`, canonical JSON plus one LF:

```json
{"schema":1,"operation_id":"<32 hex>","source":"/abs/old.db","destination":"/abs/new.db",
 "settings_path":"/abs/config/appio/settings.json","resolved_source":"/abs/old.db",
 "settings_was_absent":false,"settings_generation":3,
 "stage":"CREATING|COPIED|PUBLISHED","temp_name":".appio-move-<id>.tmp",
 "temp_identity":{"dev":1,"ino":2},"published_identity":{"dev":1,"ino":2},
 "source_identity":{"dev":1,"ino":3},"retired":[],
 "retention":{"state":"PREPARED|COMPLETE","entries":[{"retired":"...","retained":"...","dev":1,"ino":3}]}|null,
 "replacement":{"stage":"PREPARED|PUBLISHED","old_identity":{},"new_temp_identity":{},"backup_name":"..."}|null,
 "created_at":"<ISO>"}
```

It is created when the user chooses a path, and its existence *is* the pending state; no
settings key changes (D2). It is the single discovery point for a later process, so a crash
after the settings commit is still found on the next start (R5-4). The record is data and is
replaced atomically. Every settings write, pending Change, pending Cancel, and startup move takes
an exclusive flock on the canonical stable `<passwd home>/.local/state/appio/settings.json.lock`
file with a five-second acquisition bound. This lock is never replaced or removed. Under it the caller re-reads settings
and the pending record before writing. A Change compares the displayed settings generation and
displayed pending `operation_id` (including absent) with current values. A Cancel first compares
its selected absolute `settings_path` and resolved source with the pending record under the same
canonical lock. Either binding mismatch returns `changed_elsewhere` and preserves pending-record
bytes, settings bytes and database identities, even if the displayed operation ID is current.
Only after both bindings pass does Cancel compare its displayed `operation_id` with the current
pending record. Any mismatch returns
`changed_elsewhere`, refreshes the row, and preserves the newer request. Cancellation is
permitted only in the initial `CREATING` stage before any owned database temporary has been
created. Later stages retain their record and report `move_in_progress`. The application lock
prevents live windows from coexisting with a startup move. The stable settings lock serializes
writes between live windows; callers acquire instance lock before settings lock in both paths.

**The application lock is what makes "no other writer" true.** Every console process —
window, `--once`, or start-up sequence — holds a **shared** `flock` on
`<passwd home>/.local/state/appio/instance.lock`, derived from
`pwd.getpwuid(os.getuid()).pw_dir` and never from `$HOME`, `$XDG_RUNTIME_DIR`, or any other
environment variable, so two processes of the same account always open the same inode
(R7-1). The directory is created once with mode `0700`; the file is created once with mode
`0600` and never replaced or deleted.

**The lock is never upgraded.** On Linux a failed `LOCK_SH → LOCK_EX|LOCK_NB` conversion
drops the shared lock (round 8), which would leave the process unprotected exactly when it
then starts a writer. The sequence therefore takes the exclusive lock first and downgrades
by closing and reopening, never the other way round:

1. **Try `LOCK_EX|LOCK_NB` first.**
   - **Acquired** — no other appio process is running. Read the settings and the record, and
     if the record exists perform the move (steps 5–9 below). Then close the exclusive
     descriptor, take `LOCK_SH` (bounded), and **re-read the settings** before opening the
     database.
   - **Not acquired** — another instance holds a shared or exclusive lock. Take `LOCK_SH`
     with a 180 s bound, and **read the settings only after it is held**. On timeout, exit
     with `another_instance` and the message that another appio process is using or moving
     the history; nothing is opened, so nothing can recreate a stale database (review R8-2).
2. In both branches the database path is resolved **after** the lock is held, never before.

**In the running window.** `Change…` validates the selected absolute path and existing writable
parent, then takes the canonical settings lock and compares the selected settings profile and
expected operation ID. Choosing the same canonical path or existing inode returns `unchanged`
before creating a pending record or incrementing a generation. The system helper performs the
same check before creating a root transaction or stopping the service (R26-3). For a different
path Change writes the record with
`stage: "CREATING"`, a fresh `operation_id`, and the destination; it then shows
`Will move to <path> when appio restarts.` Change and Cancel acquire `settings.json.lock`
and compare the displayed operation identity under that lock as defined above. A stale Cancel
returns `changed_elsewhere` and refreshes the row without deleting a newer request. A current
operation ID from another settings profile or resolved source also returns `changed_elsewhere`.
The row displays that account-wide pending request with Cancel disabled. The mutation API enforces
the same binding checks independently of button state (R27-1). An active window holds shared
instance admission, so a startup move waits until it exits.

**The move sequence** (executed only while holding the exclusive lock, before Qt exists):

1. Take the canonical account-wide `settings.json.lock` and keep it through step 5. Before
   opening SQLite or changing any database, validate the record's `settings_path`, source and
   generation against current settings under that lock (R26-2). Absent settings are generation
   0 and match only a record with `settings_was_absent:true` and the identical captured default
   source. Foreign/unparseable settings return `settings_foreign` and preserve exact bytes,
   both databases and record. A profile, generation or source mismatch returns
   `changed_elsewhere` with the same preservation. The one admitted post-commit state is a
   canonical setting at `record.settings_generation + 1` selecting the recorded destination,
   accompanied by the matching published/retired manifest. Recovery skips rewriting that
   generation. A changed setting with any other combination is never overwritten.
2. Classify the record and source/destination/retired entries without opening SQLite. Choose
   the existing authoritative path for this branch, then `guard = guard_source(path)`; pass that
   same guard to `copy_verified`, `retire_source`, and deletion until the branch completes. A
   branch with no existing database refuses without opening a path (R18-3).
3. `retire_source(record, guard)` (D4), holding the same guard: scan for a foreign holder of the
   source inode, and either defer with `source_in_use` (settings untouched, record kept) or
   rename the source to the recorded retired name and `fsync` the directory. The guard stays
   held across this step and is closed only after `delete_retired`.
4. Write `database_path = record.destination` with `generation + 1` and confirm settings
   durability. Keep the settings lock held through step 5 or the corresponding recovery-branch
   exit. D4 branch 2 skips a duplicate settings write when they already name the destination.
5. `delete_retired(record)` (D4), remove the record, then release the settings lock. Every
   failure path releases the settings lock in `finally` after preserving its recovery state.
6. Close the exclusive lock, take the shared lock, re-read the settings, and open the
   database at the resolved path.

Failure outcomes, each with exactly one result:

- **Ordinary failure before retirement** (lock, copy, validation): settings and source remain
  unchanged; remove only this attempt's incomplete temporary. A verified PUBLISHED copy or
  PREPARED replacement belongs to branch recovery and is retained with the record, never removed
  by generic cleanup (R18-1/R18-4). Close the guard and start on the old path only if the old
  configured source path still exists and passes identity validation.
- **`source_changed` or `source_in_use`**: retain the published destination, source and record
  without cleanup (R18-1); for a system move, set `ABORTED_OLD_VALIDATED` and restart the
  previously active daemon only after the old configuration/path identity is verified (R18-2). Log the reason to
  `<passwd home>/.local/state/appio/startup-move.log` and show a banner with `Retry`.
- **Failure after any retirement rename but before a DURABLE settings commit** (retirement,
  drop-in write, reload, effective-path verification, or settings commit): stop/keep stopped the
  service, restore **every** retired manifest entry in reverse order (sidecars first, main last),
  `fsync` the source directory, verify the original main inode and sidecar identities, then
  restore the prior drop-in and directory mode where applicable, reload, and only then restart
  the service if it was active. Remove the published copy and record only after the source path
  and old configuration are verified. If any restore or verification fails, retain the record
  and both databases, leave the service stopped, and report `rollback_failed`; never let SQLite
  create an empty replacement at the configured source path.
- **UNPROVEN settings durability**: do not roll back and do not delete. Preserve the destination,
  retired manifest, and record, start on the visible destination setting if available, and report
  `settings_durability_unknown`. A later start repeats the settings-directory `fsync`; only
  DURABLE proceeds to delete retired files and remove the record.
- **Failure after DURABLE commit, during retired-file deletion**: the new setting and database
  are authoritative. Retain the manifest and report `moved_old_file_retained` with the exact
  retired paths. A later start retries deletion only after the holder scan. If a holder remains,
  retain and report `retired_in_use` without deleting or silently discarding its writes.

**D5a — The collector factory reads the resolved path.**

`run_interactive`'s `local_factory` closes over `database_path`. It instead reads
`window.database_path`, the value start-up resolution produced; the bootstrap store and the
primary `ManagedSourceSpec` derive from that same attribute. A moved database is therefore
opened by the collector and the source from one value (F3). No runtime mutation is offered.

**D6 — The system setting is a fourth fixed drop-in, with two mandatory lines.**

`/etc/systemd/system/appio-iod.service.d/30-database.conf`, canonical bytes:

```
[Service]
Environment=APPIO_DATABASE_PATH=<absolute path>
ReadWritePaths=<the database's parent directory>
```

One trailing LF. The second line is not decoration: the shipped unit sets
`ProtectSystem=strict` with only `/var/lib/appio` and `/run/appio` writable, so a database
elsewhere is read-only to the collector without it. `ReadWritePaths=` is additive across
drop-ins, so this grants exactly one directory and changes nothing else.

**Pending-move startup gate.** The unit also gains
`ExecStartPre=/usr/lib/appio/database-startup-check`. This root-owned executable refuses daemon
startup when `/var/lib/appio/database-change.json` exists in any nonterminal stage. It does not
open SQLite. Thus a reboot between source retirement and configuration switching cannot start
the daemon on the now-missing old pathname (R17-2). The helper alone advances or restores that
record, then permits the service to start after the selected database path is verified.

**Path character policy (F12).** Both values accept only `[A-Za-z0-9._/+-]` and must not
end in `/`. systemd's `Environment=` and `ReadWritePaths=` both have quoting rules, and the
smallest correct answer is to refuse a path those directives cannot carry literally:
whitespace, `"`, `'`, `\`, `%`, `$`, `:`, `,`, control characters, and non-ASCII are
refused with `database_path_unsupported_characters`. The helper never escapes and never
rewrites the user's path.

`database_payload(path) -> bytes` and `parse_database_dropin(data) -> str | None` live
beside the existing payload/parse pairs. The target must be added to `FIXED_TARGETS`,
`FIXED_CONFIG_TARGETS`, and `_require_expected_content` in `setup_files.py`.

**D7 — The daemon reads the path from the environment, and a bad value never stops it.**

`configured_database(environ=None) -> Path` in `iod.py`, called only from module-level
`main()` and never as a `DaemonPaths` default argument:

- absent or empty → `DaemonPaths.database` default (`/var/lib/appio/iod.db`);
- a canonical absolute path with no `..`, no control character, and the D6 character
  policy → that path;
- anything else → the default, plus exactly one warning
  `database_path_invalid value=<repr truncated to 64 characters>`.

**D8 — `setup-state.json` is pinned; the lock follows the database.**

`iod.py:590` resolves `setup-state.json` next to the database, which is correct only while
the database cannot move. The activation gate belongs to the service, so `_pending` uses
`setup_files.STATE_ROOT / "setup-state.json"` (`/var/lib/appio/setup-state.json`)
unconditionally. `STATE_FILE` is defined in `setup_files.py:93` and imported by
`setup_delegation.py`; that module remains its single definition. `iod.lock` stays beside
the database (`with_name("iod.lock")`), because it guards that database's single writer.

**D9 — The status reply reports the database path.**

`status` gains an optional `database_path` (absolute string, at most 4096 bytes, no control
character), validated on both sides exactly as `sample_ns` is. Compatibility is honest: a
console built before this change holds the old exact-key validator, treats one unknown key
as a protocol error, and shows the collector as unavailable until the console is restarted.
It never shows a wrong path.

`ServiceFacts` gains `database_path: str | None`, populated by `production_probe` from the
status reply. That is the **reported** path: evidence that the daemon is using a file, and
`None` while the collector is not answering. The **configured** path is resolved
independently from the merged unit configuration (D11 step 2) and is what the Settings row
shows, marked `configured, not confirmed` when the collector is silent. The two are never
conflated.

**D10 — A new argument-carrying helper verb, plumbed end to end.**

`set-database <absolute path>` follows `set-sample-period` through every layer:

1. **Installed shim** (`data/libexec/appio-setup`, stdlib only): the verb joins its verb
   set; the syntax rule accepts exactly three elements and requires the third to match a
   shape pattern (`^/[^\x00\n]{1,4095}$`). Every policy decision belongs to the boundary.
2. **Boundary** (`setup_boundary`): `Invocation.database_path: str | None = None`;
   `parse_invocation` accepts the three-element form only for this verb, refuses
   `invalid_verb_or_argument_count` for any other arity or shape, and
   `database_path_invalid` for a relative path, a `..` component, `/` itself, a length over
   4096 bytes, a missing or non-directory parent, a parent that is not root-owned or is
   group- or world-writable, a directory that fails D14, and any character outside D6's
   policy. The parent check uses the existing component-by-component `O_NOFOLLOW` walk.
3. **Dispatch, entry point, client, decode**: the same additions as the period verb,
   including the internal re-validation that synthesizes the same three-element argv.
   `SetupClient.invoke(verb, *, database_path=None)`; `decode_body` accepts the optional
   `database_path` string **and** the optional `database_state` token from D11's outcome
   list, validating both and keeping its exact-key rejection for everything else.
4. **Consent text** names the exact command and path and states that the service is
   stopped for the duration of the move.

**D11 — A system change: copy, retire, switch, verify, start.**

A root-owned `/var/lib/appio/database-change.json` record is created and fsynced before any
mutation. The unit's `ExecStartPre` gate prevents automatic daemon startup while this transaction
is incomplete.

1. Capture `ActiveState` and resolve the effective path from `systemctl show -p Environment -p
   EnvironmentFiles -p UnsetEnvironment -p DropInPaths -p ActiveState appio-iod.service`. A
   non-empty `EnvironmentFiles` or an `UnsetEnvironment` naming `APPIO_DATABASE_PATH` refuses
   `database_path_indeterminate`.
2. Stop the service if active. A failed stop is `service_stop_failed`, with no copy/config
   change. The startup gate's record is already durable, so reboot cannot start it mid-run.
3. Classify the durable record and the effective source, destination, and retired entries
   before opening SQLite. Choose an existing authoritative inode for the current recovery branch,
   then acquire `guard = guard_source(existing_path)`; pass it through `copy_verified`,
   `retire_source`, and deletion, closing only after the branch completes. If a branch has no
   existing database path, refuse without SQLite `O_CREAT` (R18-3). Enforce destination file
   mode/owner and D14 directory rules.
4. Scan for foreign holders. If any exist, mark the record `ABORTED_OLD_VALIDATED` only after
   verifying that the old effective configuration still names the source and its inode matches
   the record; fsync the record, release the guard, and restart the service if it was active.
   `ExecStartPre` permits this state only when the old path identity and old effective config
   match; it **does not clear the record**. Settings/config stay unchanged, source remains
   authoritative, service resumes the old database, and the requested target remains in the
   durable record for a later retry (R18-2). The next helper run takes the exclusive app lock,
   stops the old service, reclassifies the record and retries the holder check. Otherwise write and fsync the
   complete retirement manifest, rename
   the main file then sidecars, and fsync the source directory. Keep the guard open.
5. Write the canonical drop-in (D6), run `systemctl daemon-reload`, and verify the effective
   path unconditionally. The service remains stopped and the startup gate remains closed.
6. Commit the operation's configuration setting using temp-file fsync, atomic replacement, and
   parent-directory fsync. On confirmed directory-fsync success, record
   `stage:"DURABLE"` with `stage_write_boot_id=<current boot id>`, fsync the record and its
   parent, then write and fsync `/run/appio-setup/database-change-durable-<operation_id>` and
   fsync `/run/appio-setup` before permitting `ExecStartPre` (R19-2, R25-2). Provision this
   root-owned directory through the existing helper `LOCK_ROOT` no-follow, root-owned/mode
   checks. This witness belongs to the helper and lasts until reboot or successful move cleanup.
   Stopping/restarting `appio-iod.service` removes `/run/appio`, which cannot affect the witness.
   Remove the operation's witness only after retired entries and the transaction record are
   durably removed. On a record-stage fsync error, do not create that volatile
   witness; retain both databases and the record. The same-boot gate requires the matching
   witness. After reboot, a DURABLE record whose `stage_write_boot_id` differs from the current
   boot is accepted because those bytes demonstrably survived the reboot. This gives an
   explicit ambiguity rule for the record's own durability. A settings `UNPROVEN` result remains
   pre-DURABLE; recovery retries its directory fsync before setting the stage. A FAILED commit
   restores the source/config as below.
7. On `DURABLE`, complete all required identity/durability checks and release any destination
   writer guard before daemon startup. Retain an original-source guard only when it protects a
   distinct retired original inode through source cleanup. In branch 2b the source is already
   gone, so no write guard remains when startup is attempted. Start only if the service was
   active before. `ExecStartPre` requires the recorded DURABLE state, the destination effective
   path and the specified witness. Verify the daemon's reported `database_path`. Keep the record
   and retired entries on any startup/status failure, preserving destination commits (R25-1).
8. After successful verification, finalize retired deletion or retention through D4's manifest
   recovery. On `recovery_target_exists` during cleanup, including a collision **after daemon
   startup**, stop the daemon, preserve the DURABLE record, witness, destination and every
   retired/retained entry, and return that exact decodable state. Never roll back committed
   destination rows. Resume retention branch 0 under the helper lock on retry. On success,
   fsync the source directory, durably report retained paths when applicable, remove the record
   and witness, then close any original-source guard. An initially inactive service stays stopped.
9. Outcomes are `applied`, `applied_not_running`, `unchanged`, `repair`, `source_in_use`,
   `settings_durability_unknown`, `moved_old_file_retained`, `retired_in_use`,
   `recovery_target_exists`, `recovery_missing_entry`, `database_not_in_effect`,
   `rollback_failed`, and `service_start_failed`. `decode_body` accepts every token in this list.

**Rollback and post-start failure.** Before a DURABLE switch, any failure restores the manifest
in reverse order, fsyncs the source directory, verifies original inodes, restores the previous
drop-in and directory mode, reloads, and only then restarts the service if it was active. If any
restore/verification fails, retain record and both databases, keep service stopped, and report
`rollback_failed`. After DURABLE switch, the destination is authoritative. If startup/status
verification fails, do not roll back or delete it. Stop the service if necessary, retain
record/retired files and destination, and report `database_not_in_effect` or
`service_start_failed` for retry. Rows committed after start remain authoritative (R17-1).

On every boot, `ExecStartPre` reads the record. `PREPARED` and every pre-DURABLE stage block
startup until helper recovery. `ABORTED_OLD_VALIDATED` permits startup on the old path only when
both the old effective config and recorded source identity match; it leaves the pending target
record intact for retry. `DURABLE` permits startup only on the destination and only with a
same-boot volatile witness or a record observed from a later boot. A DURABLE record selecting the destination permits startup, while the record
remains until daemon verification and retired-file cleanup finish. This closes the reboot window
between retirement and drop-in switching (R17-2).

**D12 — The existing root export follows the configured path.**

The export source is the daemon-reported `database_path` when the service answers (D9).
When it does not answer, the source is resolved from the merged configuration as in D11
step 2; when that is indeterminate the export refuses `source:database_path_unresolved`
rather than exporting the wrong file. The destination policy is unchanged: a generated,
non-replacing name in the caller's home, validated against the caller's identity. No new
root command accepts an arbitrary destination.

**D12a — Export timeouts follow the data (F15).**

`EXPORT_TIMEOUT_SECONDS` becomes a parameter with default `max(15, physical_size / 32 MiB
per second)` seconds, capped at 15 minutes, and the interactive window passes that
parameter. The deadline still aborts a stalled backup; it no longer aborts a large healthy
one.

**D13 — The window gets two Settings rows.**

| Row key | Caption | Controls | Object names |
| --- | --- | --- | --- |
| `history_database` | `History database (this app)` | current path, pending path, `Change…`/`Cancel`, `Export…`, status line | `history_database_row`, `history_database_path`, `history_database_change`, `history_database_export`, `history_database_status` |
| `system_history` | `System services history` | path label, `Change…`, `Export…`, status line | `system_history_row`, `system_history_path`, `system_history_change`, `system_history_export`, `system_history_status` |

Rules:

- The console row shows the current path; when a pending-move record exists it shows
  `Will move to <path> when appio restarts.` with `Cancel` in place of `Change…`. Cancel uses
  the displayed operation identity under the stable settings lock (D5). `Export…` always
  exports the current file and is unaffected by a pending change.
- **Enablement (F13, R2-10, single rule).** Both system controls are driven by the
  *configured* path from D9. `Change…` is enabled when that path is determinate, even while
  the service is stopped. `Export…` is enabled when that path is determinate **and** the
  source file is readable. When the configured path cannot be determined, both are disabled
  with the reason, and the row shows no path. When the path is known but the collector is
  silent, the row shows it with `configured, not confirmed`. There is no state in which a
  control is enabled and the reason text says it cannot work.
- `Change…` opens `QFileDialog.getSaveFileName` with the current directory and
  `history.db`/`iod.db` preselected (a directory is created by that dialog only; the system
  path's directory must already exist). The system row then shows the existing
  `ConsentDialog` with D10's text.
- `Export…` on the console row opens the same chooser and calls `export_verified`, reporting
  the written path. `Export…` on the system row runs `export-history` with D12a's deadline
  and reports the generated path from the reply.
- Every long operation shows the working state, keeps the dialog open, and reports no
  success before its verification step.
- `SettingsDialog.ROWS` and `focus_row` gain both keys; `CollectorWindow.open_settings`
  accepts both.

**D14 — A system database lives in a dedicated directory with the daemon's exact mode.**

The destination's parent must be an existing directory that satisfies **all** of:

1. root-owned, and not group- or world-writable;
2. every entry in it is root-owned and is not a symlink. That is the whole rule, and it is
   why the rule is stated as an ownership invariant rather than a name allowlist: appio's own
   state set is not finite in advance (round 5 found `staging/` and `probes/<uid>/`, round 6
   found `sample-period.complete.json`), so any enumerated list is wrong the next time appio
   adds a file. A directory whose entries are all root-owned contains nothing an unprivileged
   account controls, which is exactly the property that makes rule 3's mode change safe, and
   any non-root-owned entry still refuses the directory (R3-3, R4-4, R6-3);
3. either already at mode exactly `0711`, or **empty**, in which case the helper sets
   `0711` and records the previous mode for rollback (R3-4). A populated directory is never
   widened: round 8 showed that a `0700` directory of root-owned `0644` files becomes
   readable by other accounts once it is `0711`, so a populated directory must already carry
   the exact mode the daemon requires.

Refused `database_directory_shared`: `/`, `/etc`, `/usr`, `/bin`, `/sbin`, `/lib*`,
`/boot`, `/proc`, `/sys`, `/dev`, `/run/systemd`, any `/home/<user>` directory, any
directory owned by a non-root account, and any directory with an entry outside rule 2. The
helper **never creates a directory**. No `ReadWritePaths` entry ever points at anything but
the one dedicated directory.

**D15 — Timeouts and interruption.**

The console waits 15 minutes for `set-database` and 15 minutes for `export-history`,
instead of the 120 s used by the other verbs (F14). A timeout is neither success nor
failure: the row shows `checking`, re-reads the effective configuration and the reported
status once, and reports the completed outcome or `change_pending` naming the exact state it
can see. Interruption is safe by construction: deletion is last, publication is a no-replace
link reconciled by existence facts, and the record survives until the retired file is deleted (D4).

**D16 — Concurrency and convergence.**

- **One application lock at one canonical path.** Current-version console entry points hold a
  shared flock on `<passwd home>/.local/state/appio/instance.lock` for their lifetime. A startup
  move acquires it exclusively first (D5), and reads configuration only after locking. Existing
  legacy holders cause deferral when detected. A later launch of a legacy binary or unrelated
  SQLite client can create a separate database at the former path as described in D4.
- **Two instances starting together** attempt exclusive admission first. Any process that fails
  obtains shared admission and re-reads settings before opening SQLite. No shared-to-exclusive
  upgrade is attempted.
- **Change and Cancel** hold shared instance admission, then take the stable exclusive
  `settings.json.lock` and re-read pending operation identity as defined in D5. Stale displays
  cannot overwrite or cancel a newer operation.
- **The settings file** is replaced atomically under that same lock. A row refreshes after
  a successful write or a `changed_elsewhere` result.
- **The system move** holds the helper's existing exclusive lock for its whole duration.

## 5. Architecture

```
console (user)                                 root helper
┌────────────────────────────────────┐         ┌──────────────────────────────┐
│ __main__: --db > settings > default│         │ shim → boundary → dispatch   │
│   + start-up pending-move sequence │         │   set-database <abs path>    │
│        │                           │         │        │                     │
│        ├─ settings.py (read/write) │         │        ↓                     │
│        ↓                           │         │ server: stop / copy / verify │
│ collector_window                   │         │   drop-in + reload           │
│   ├─ CollectorCoordinator (writer) │         │   verify effective path      │
│   ├─ ManagedSourceRegistry (reader)│         │   delete old file            │
│   └─ Settings rows + QFileDialog   │         │                              │
│        │                           │         │      ↓                       │
│        ↓                           │         │  database.py (shared)        │
│ database.py                        │◀───────▶│                              │
└────────────────────────────────────┘         └──────────────────────────────┘
                    │                                       │
                    └──────────── SQLite file ──────────────┘
```

New modules: `src/appio/database.py` (copy, verify, publish, finish, record recovery; no Qt,
no setup import, no privilege assumption) and `src/appio/settings.py` (parse, serialize,
resolve, atomically write, and lock the settings and pending-record operations using the stable
`settings.json.lock` with operation-ID comparison.

Changed modules: `__main__.py` (resolution and the start-up move), `ui/collector_window.py`
(rows, pending record, export, timeouts), `iod.py`, `ipc_models.py`, `collector_owner.py`,
`setup_boundary.py` / `setup_files.py` / `setup_service.py` / `setup_client.py`,
`history_export.py`, `data/libexec/appio-setup`, `README.md`. `history_source.py` is **not**
changed: a start-up move needs no runtime retirement.

## 6. Data flow

**Console change.** row → chooser → stable settings lock and operation-ID comparison → write
pending record → restart → exclusive instance admission → stable settings lock → read settings →
`copy_verified` (guarded, separate reader) → `retire_source` → settings commit →
`delete_retired` → open the new file.

**Console export.** row → chooser → `export_verified` → verify → report path.

**System change.** row → chooser → consent → helper: capture state → resolve effective path
→ stop (if active) → copy → directory rules → drop-in → reload → verify effective path →
start (if active) → verify reported path → delete old → reply → row shows the reported path.

**System export.** row → helper `export-history` → source from the reported path or the
merged configuration → staged backup, validated, linked into the caller's home → reply → row
reports the path.

## 7. Error handling

| Situation | Behaviour |
| --- | --- |
| Settings file absent | default path; first change creates it |
| Settings file foreign or unparseable | without pending move, default plus one log line; with pending move, `settings_foreign` preserves settings bytes, record and database identities |
| Pending move settings/profile/generation differ | `changed_elsewhere` before SQLite opens or source retirement, pending request retained |
| Selected database is already the current path/inode | `unchanged` before transaction creation or service stop |
| `--db` in effect | row shows the override; `Change…` disabled; a pending record is left for the next normal launch |
| Pending record exists | row shows it; the move runs at the next start |
| Another appio instance is running at start-up | take the shared lock after it; on a 180 s timeout exit with `another_instance`; the record is kept |
| Two processes resolve different lock paths | impossible by construction: the path is `<passwd home>/.local/state/appio/instance.lock`, independent of the environment |
| Change/Cancel in a live window | stable settings lock and expected operation-ID comparison; stale display returns `changed_elsewhere`, advanced transaction returns `move_in_progress`, foreign settings returns `settings_foreign` |
| A writer holds the source database | refused `database_in_use`; setting, source, and destination untouched |
| A published copy is owned by a replacement transaction | finish PREPARED/PUBLISHED by recorded inode state; keep the prior verified copy until the new one verifies |
| Destination exists without a matching record inode | refused `destination_exists` |
| Destination matches the recorded temporary inode | treated as published, adopted, temporary alias removed |
| Destination directory missing, not writable, shared, or wrong mode | refused `destination_unwritable` / `database_directory_shared`; neither side creates a directory |
| Source missing | refused `source_missing` |
| Source is not a database or is corrupt | `source_not_a_database` / `verification_failed` |
| Source identity changed or a holder is detected during recovery | `source_changed` / `source_in_use`; keep source, published destination and record; no generic cleanup |
| Copy interrupted | the next attempt removes exactly the recorded temporary and sidecars and copies again |
| Settings commit fails | the published copy and the record are removed; old file and setting stay; the console starts on the old path |
| Retired files cannot be deleted | console starts on the new path with the retained path in the banner |
| Another process still holds the old database open | the switch is deferred **before** it happens: `source_in_use` with `open_by_pid:<n>`, settings untouched, record kept |
| The old path was recreated by another process before the rename | refused `source_changed`; nothing renamed, settings untouched |
| A process opened the file between the scan and the rename | the retired files are retained as `<name>.retained-<timestamp>` and reported (`retired_in_use`); no write is silently destroyed |
| Settings commit fails after the rename | every retired file is renamed back, the copy and record are removed, the console starts on the old path |
| Service stop fails | `service_stop_failed`; nothing copied, drop-in untouched |
| Effective path indeterminate | refused `database_path_indeterminate` naming `DropInPaths` |
| Drop-in write or reload fails | previous drop-in restored, directory mode restored, service restarted when it was active, old file in place |
| Restore also fails | `rollback_failed`, both paths retained, service stopped, record retained, resumable rerun |
| Daemon status fails after durable switch | keep the destination selected and record; stop service and retry verification/start without rolling back committed destination rows |
| Daemon does not report the new path | `database_not_in_effect` naming both values and the winning drop-in |
| Helper exceeds the window's deadline | `change_pending`, one observation, exact visible state |
| Consent denied / cancelled | `consent_denied` / `cancelled`; nothing changes, no helper runs |
| Export target exists | refused; no overwrite, no truncate |
| A writer is active during an export | not blocked: the export takes a consistent online snapshot with no writer guard |
| Export source unresolvable | `source:database_path_unresolved` |

## 8. Test strategy

All tests are rootless and use temporary roots through the existing seams.

1. **Settings and the pending record** (`tests/test_settings.py`): canonical settings bytes
   with exactly three keys; refusal for `schema: 2`, a relative path, `..`, an extra key
   (including `pending_path`), trailing junk, a BOM, a control character; absent ≠ foreign;
   atomic write leaves no temp and keeps `0600`; the settings lock is exclusive and bounded;
   two windows use barriers to interleave Change(X), Change(Y), then stale Cancel(X), proving Y
   remains recorded and Cancel returns `changed_elsewhere`. A second environment selecting
   another settings profile or default source refreshes and attempts Cancel with Y's *current*
   ID; assert `changed_elsewhere`, byte-identical pending/settings files, and unchanged database
   identities. Test stale Change similarly, and
   test refusal to cancel an operation after its first temporary was created;
   precedence `--db` > setting > default; the pending record round-trips; a corrupt record is
   refused without deleting anything. Vary HOME, XDG_CONFIG_HOME, XDG_STATE_HOME and XDG_DATA_HOME
   across two processes. Assert a single passwd-derived pending record and operation lock,
   preserve per-launch default resolution, and refuse a pending operation whose settings-path or
   resolved-source binding differs. Test startup with generation 0/absent settings, foreign
   replacement settings, a changed canonical generation, and the exact admitted post-commit
   generation. All refusal probes preserve the settings bytes and database identities.
2. **Database procedures** (`tests/test_database.py`): real SQLite files. Copy passes
   `integrity_check`, schema validation, records its own counts; an export completes while a
   live writer commits between pages, and that writer is not blocked; a WAL-only commit made
   after publication is impossible to miss because the guard spans copy and rename (a probe
   commits into the WAL while the guard is held and observes `SQLITE_BUSY`); a holder process
   defers the switch; each of the **seven** recovery branches (1, 2, 2b, 3, 4, 5, 6) is
   exercised from a fixture, and a crash injected at every manifest boundary (before the main
   rename, between sidecar renames, after the main deletion, between sidecar deletions)
   converges to one of them; a settings replacement that becomes visible while its flush
   reports failure is treated as committed by re-reading; a live writer makes
   `BEGIN IMMEDIATE` refuse `database_in_use`, and a writer that starts after the guard is
   taken cannot commit until the guard is released (two processes); the backup is taken from
   a separate read-only connection while the guard is held and completes (the round-5
   stall); a WAL source produces a self-contained destination with no sidecars; recovery is
   by inode for every crash point: before the link, between `os.link` and the stage write,
   between the stage write and the temporary unlink, after the commit, and after the source
   delete; a destination created between the absence check and the link is never replaced
   (`EEXIST`); a substituted temporary symlink is detected and nothing is published;
   runtime startup, and a target recreated between absence check and rollback restore is preserved
   by RENAME_NOREPLACE. Assert its bytes/inode, every retired entry and the full record survive
   with `recovery_target_exists`, and assert the affected daemon remains stopped. Pre-create a
   retained target for both the main file and a sidecar, and inject creation between target
   inspection and retention move. Assert both inode sets and bytes, the full manifest and
   record survive, with exact `recovery_target_exists` and stopped service. Resume every
   retention move after interruption, recognizing a matching target idempotently. Inject loss
   after complete retention-record publication, main retention rename and each sidecar move.
   Verify retention branch 0 runs before branch 2b, all paths remain reportable, and record
   removal follows every retained entry and directory fsync. In the real helper sequence, collide
   after daemon startup, assert it is stopped, exact `recovery_target_exists` decodes, the
   DURABLE witness and all inode sets survive, and the destination keeps post-start rows;
   remove the record only after all retained names are flushed;
   `finish` of cleanup is idempotent, removes the temporary alias by inode, refuses a replaced
   source inode, and removes the record last.
3. **Start-up move** (`tests/p3/test_startup_move.py`): the lock path is identical for two
   processes launched with different `HOME`, `XDG_RUNTIME_DIR`, and `XDG_STATE_HOME` values;
   a failed exclusive attempt never leaves the process holding a shared lock it thinks it has
   (no upgrade path exists); a second instance reads the committed settings only after the
   lock, so it never opens a stale path; a foreign holder defers the switch with
   `source_in_use` and the settings still name the source; a crash after the rename is
   completed by branch 2, and the post-delete state by branch 2b, without a second generation; a pending record moves the database,
   rewrites the setting, deletes the old file, removes the record, and opens the new path;
   a `--db` launch skips the record and leaves it; an exclusive-lock holder (a simulated
   second instance) makes the move defer with `another_instance` and the record survives;
   branch exits and re-entry preserve shared admission and reread settings; same-path Change
   returns `unchanged` without a record, generation bump, filesystem write, or service restart; a
   failed commit removes the copy and starts on the old path; a failed delete starts on the
   new path and reports the retained file; every crash point in D4's reconciliation resumes
   to exactly one outcome, including `CREATING` with no recorded inode and a committed
   settings file; the collector and the source both open the new path.
4. **Daemon path** (`tests/p3/test_iod_database_path.py`): `configured_database` accepts an
   absolute path and rejects relative, `..`, and control characters with exactly one log
   line and the default; `main()` builds `DaemonPaths` from the environment;
   `setup-state.json` is read from `/var/lib/appio` regardless; the lock follows the
   database.
5. **Status protocol** (`tests/p3/test_ipc_status_database.py`): the new key validates on
   both sides; absent stays valid; relative, over-long, newline, and NUL values are refused
   on both sides; `ServiceFacts.database_path` is populated from a reply and is `None`
   without one.
6. **Helper verb, drop-in and stages** (`tests/p3/test_setup_database.py`): canonical
   drop-in bytes and parser round-trip; foreign content refused; unsupported characters
   refused at boundary and payload; fixed config target; boundary argv refusals for
   relative, `..`, `/`, missing parent, non-root parent, group-writable parent, shared system
   directory, over-length, and a directory with a non-root-owned entry; a
   `/var/lib/appio`-shaped directory holding `setup-state.json`, `staging/`,
   `probes/<uid>/probe.bin`, and `sample-period.complete.json` is accepted; the apply sequence against fake runner and filesystem: stop only when
   active, no start when inactive, effective-path verification while inactive, mode recorded
   and restored on rollback, drop-in restored on failed reload, delete last, resumption from
   the record, `unchanged`, `repair`, `moved_old_file_retained`, `rollback_failed`;
   `database_state` decoder includes `service_start_failed` and every D11 outcome, while an
   unknown field or state still refuses.
7. **Export source and deadline** (`tests/p3/test_history_export.py` addition): the reported
   path wins when the service answers; the merged configuration is used when it does not; an
   indeterminate configuration refuses; an injected short deadline aborts a slow backup and
   a larger one completes it.
8. **Window** (`tests/p3/test_collector_window.py` addition): both rows exist with their
   object names and focus; `--db` disables `Change…`; the console row shows and cancels a
   pending record; stale Change/Cancel returns `changed_elsewhere`, and cancellation after a
   temporary was created returns `move_in_progress`; the system controls follow D13's single
   enablement rule including `configured, not confirmed`; a start-up deferral shows its
   banner; `Export…` writes a verified file and reports it; long operations use the new
   deadline.
9. **Startup gate and fault recovery** (`tests/p3/test_database_startup_gate.py`): every
   non-DURABLE stage blocks systemd startup before SQLite opens; `ABORTED_OLD_VALIDATED`
   permits only the verified old path/config; DURABLE permits only the verified destination;
   the record-stage write is fsynced/read back before start admission; a post-start status
   failure preserves destination rows through retry. Exercise branch 2b with a real file-backed
   daemon that performs its initial collection and retains rows written after migration. Assert
   no destination write guard is held during this startup. Model actual removal of `/run/appio`
   on service stop/restart and verify the witness under `/run/appio-setup` survives for a retained
   DURABLE record. Verify helper provisioning and witness cleanup together.
10. **Packaging** (`tests/test_packaging.py` addition): the installed shim accepts the new
   verb with one absolute argument and rejects a relative one; the polkit policy still
   declares exactly one action.

## 9. Risks

- **A large system database makes a visible pause.** The move is a full copy; on an 8 GB
  store this is minutes with the service stopped. Mitigation: the consent text says so, the
  window shows a working state with a 15-minute deadline, and no timeout is imposed inside
  the helper.
- **The console's move happens at start-up.** That is a deliberate trade: it is the only
  moment when this process owns no writer and no reader.
- **A move needs to be the only instance.** When another console is running, the move waits
  for a later launch, and a console that starts while a move holds the exclusive lock waits up
  to 180 s and then exits with `another_instance`. That is the price of never writing to a
  database another process may be committing to, and it is bounded (close the other window and
  restart).
- **The daemon startup gate is part of database-path integrity.** If its fixed helper is missing,
  untrusted, or cannot read the transaction record, systemd refuses daemon startup. This keeps a
  reboot from opening a retired path and creating an empty database.
- **Legacy and external clients.** A pre-feature binary launched after completion can create a
  separate database at the former pathname because it uses its former configuration. The
  migration preserves the source records committed before its guarded retirement. Current
  entry points serialize with the mover and use the saved destination. Existing detected legacy
  holders defer the move. No operation deletes a newly created old-path file whose identity
  differs from the manifest.
- **An export does not block collection.** It reads a consistent online snapshot without the
  writer guard; only a move takes the guard, and a move happens at start-up when no collector
  of this process is writing.
- **A shared directory is the way to damage the host.** D14 bounds a root file operation to
  a root-owned directory whose every entry is appio-owned.
- **A path on an unsupported filesystem** (no directory `fsync`) fails `sync_failed` rather
  than pretending durability.
- **The status key breaks an old console for one restart.** Accepted deliberately.

## 10. Review dispositions

This section retains review history. Current requirements are the decisions and acceptance
criteria in sections 2–9. Superseded mechanisms mentioned in this history have their current
resolution stated in the later disposition.

### Round 1

| Finding | Resolution |
| --- | --- |
| F1 shared-directory chmod | D14 allowlist, every entry root-owned |
| F2 reader actor still open | removed: the console moves only at start-up |
| F3 factory closes over the startup path | D5a one resolved value |
| F4 concurrent consoles | D16 record `flock` + settings lock + read-after-lock |
| F5 rename visible before fsync | no rename; link + directory fsync before the commit |
| F6 failure after the commit | D5 `moved_old_file_retained` + record-driven retry |
| F7 inactive service unverified | D11 step 7 |
| F8 export source from environment only | D12 |
| F9 no path into the window | D9 |
| F10 decoder rejects new fields | D10 step 3 |
| F11 unpinned descriptors | D4 trust model, pinned directory, inode reconciliation, no-replace link |
| F12 systemd quoting | D6 character policy |
| F13 controls disabled while stopped | D13 single rule |
| F14 120 s GUI timeout | D15 |
| F15 15 s export deadline | D12a |
| F16 rollback itself fails | D11 `rollback_failed` + record |
| F17 STATE_FILE owner | D8 |
| F18 line references | §1 |
| F19 row counts | D4 rule 6 |
| F20 cancelled consent | §7 |

### Rounds 2–5

| Finding | Resolution |
| --- | --- |
| R2-1 retire returns no future | removed with the runtime move |
| R2-2 generation read before the lock | D5 step 3/5 read under the lock |
| R2-3, F11 descriptor claims | D4 trust model + link + inode reconciliation |
| R2-4, F16 interrupted run | D4 record + inode reconciliation |
| R2-5 source deletion inside the copy | D4: retirement by rename, deletion only after the commit |
| R2-6 failed settings write leaves a copy | D5 step 7 failure removes copy and record |
| R2-7 directory mode | D14 rule 3 with recorded restore |
| R2-8 racy source counts | D4 rule 6 |
| R2-9 creating a missing parent | D14: never created |
| R2-10 enablement contradiction | D13 single rule |
| R3-1 delete before commit | D4/D5/D11 order |
| R3-2 other processes writing | D16: exclusive application lock + read-after-lock, D4's `IMMEDIATE` guard held through the rename, and a `/proc` holder scan that defers |
| R3-3 `0711` not content-checked | D14 rule 2 |
| R3-4 mode changed without restore | D14 rule 3 + D11 rollback |
| R3-5 `source_busy` cannot restore | removed with the runtime move |
| R3-6 pattern-based temp sweep | the record names exact files |
| R3-7 WAL sidecars after interruption | D4 rule 5 |
| R3-8 deletion failure leaves no restore step | D5 failure outcomes + record retry |
| R3-9 F19 table contradicted the body | corrected |
| R4-1 adoption without provenance | D4 inode reconciliation |
| R4-2 rename replaces | D4 rule 8 uses `os.link`; `os.rename` never used |
| R4-3 marker erased before the commit | the record lives in a fixed path and outlives the commit |
| R4-4 allowlist rejects the default directory | D14 rule 2 includes `setup-state.json`, `staging/`, `probes/` |
| R5-1 pending key breaks the settings shape | D2: three keys; the pending move is its own record |
| R5-2 guarded connection stalls its own backup | D4 rule 3: separate read-only backup reader |
| R5-3 publication crash windows | D4 rule 9 + inode reconciliation covering both windows |
| R5-4 fresh process does not discover the stage | the record is a fixed path discovered on every start |
| R5-5 a concurrent Cancel is overwritten | D5 step 5 holds the settings lock through the commit; `Change…`/`Cancel` take the record lock and report `move_busy` |
| R5-6 `--db` versus a pending move | D3: `--db` skips the pending move for that launch |
| R6-1 temporary created before its inode is recorded | D4 rule 4: `CREATING` stage names the file before it exists; recovery adopts it by name, owner, and type |
| R6-2 ambiguous terminal state after the commit | D4's terminal rule decides by the settings alone: commit only when they do not already name the destination |
| R6-3 allowlist misses `sample-period.complete.json` | D14 rule 2 is an ownership invariant (every entry root-owned, no symlinks), not a name list |
| R3-2 an older instance commits to the unlinked database | D16: the application lock is shared by every instance and taken exclusively for a move, so a move never coexists with another appio process |
| R5-5 flock on a replaced record file | the record is data; mutual exclusion moved to the never-replaced `instance.lock` |
| R7-1 environment-selected lock path splits the lock | D5/D16: one canonical path from `pwd.getpwuid(os.getuid()).pw_dir`, never from the environment |
| R8-1 failed shared→exclusive upgrade drops the lock | D5: no upgrade path; exclusive first, downgrade by close + re-open, shared after |
| R8-2 stale resolved path after waiting | D5: the database path is resolved only after the lock is held |
| R8-3 stale published snapshot adopted | D4 terminal rule compares the source against `source_identity` and re-copies a stale destination |
| R8-4 directory mode widened over populated contents | D14 rule 3: set `0711` only on an empty directory; a populated one must already be `0711` |
| R8-5 export blocks live writers | `export_verified` takes no writer guard |
| R8-3 (round 9) freshness skipped on the temp-identity branch | the recovery order puts freshness first and applies it to every adoption path |
| R3-2 (round 9) a pre-feature process ignores the new lock | `retire_source` scans `/proc/<pid>/fd` and defers **before** the switch when any holder exists |
| R10-1 WAL-only commits defeat a metadata fingerprint | D4 retires the source by rename inside the guard; no freshness comparison exists, so no comparison can be wrong |
| R10-2 post-commit divergence between source and destination | the settings commit happens only after the source name is gone; a holder defers the whole switch before any commit |
| R11-1 WAL sidecars left at the old path | the retirement renames the main file and every present sidecar |
| R11-2 commit failure strands the source under the retired name | the commit-failure path renames every retired file back and removes the copy and record |
| R11-3 no branch for the post-delete, pre-record-removal state | branch 2b completes exactly that state |
| R10-2 a later opener can write the old path or the retired file | identity check before the rename refuses a recreated path; a raced opener's retired files are retained and reported instead of deleted; §9 states the limit |
| R12-1 multi-file retirement lacked a manifest | the record carries `retired[]`, written per entry before its rename, and recovery walks it in a fixed order |
| R12-2 guard lifetime absent from the API | `guard_source` returns the guard; `copy_verified` and `retire_source` take it; it closes after `delete_retired` |
| R12-3 settings commit durability and ambiguous outcome | temp + fsync + rename + directory fsync, with the outcome decided by re-reading the file |
| R12-4 test inventory omitted branch 2b | the test strategy names all seven branches and injects a crash at every manifest boundary |
| R12-1 (round 13) per-entry manifest left unrecorded sidecars | the complete `retired[]` inventory is written and fsynced before the first rename |
| R12-3 (round 13) durability unproven while evidence deleted | the commit returns DURABLE / UNPROVEN / FAILED; nothing is deleted until the directory fsync succeeds |
| R13-1 adoption of a stale copy while the source exists | branch 1 always re-copies under a fresh guard; adoption only happens after the source name is gone |
| R13-2 partial retirement removed the record | one reverse-order restore procedure covers every pre-commit failure |
| R13-3 illustrated calls omitted the guard | D5 and D11 acquire, pass, and close one guard |
| R14-1 a re-copy cannot publish over the existing destination | branch 1 removes the published destination by inode before re-copying |
| R14-2 branches 2/2b skipped the durability retry | both confirm the settings directory fsync and keep everything on `UNPROVEN` |
| R14-3 the manifest could be lost while renames survive | the record's parent directory is fsynced before the first rename |
| R13-3 D11 calls omitted the guard | D11 binds one guard through copy, retirement, and deletion |
| R15-1 D11 switched before retirement, so a started daemon could write rows branch 1 would discard | D11 retires before the drop-in switch, so a crash after the start is branch 2/2b, never branch 1 |
| R16-1 system rollback could restart onto an absent source path | D11 restores the retirement manifest and verifies the original inode/path before restoring config or restarting the service |
| R15-1 system service switched and wrote before source retirement | D11 retires the source before drop-in switch/start; ExecStartPre blocks reboot startup while any pre-DURABLE record exists |
| R17-1 post-start verification failure could discard destination commits | after DURABLE switch, destination remains authoritative; stop and retain record on failure, never roll back |
| R17-2 reboot could create an empty old-path database | the root-owned ExecStartPre gate blocks all non-DURABLE transactions before SQLite opens |
| R17-3 branch 1 removed destination before source identity check | source identity and holder checks now precede any destination unlink; mismatches retain both copies |
| R18-1 branch-1 mismatch/holder deferral contradicted general cleanup | explicit `source_changed`/`source_in_use` outcomes preserve destination, source and record; cleanup is limited to ordinary pre-retirement failure |
| R18-2 active service stayed stopped on holder deferral | `ABORTED_OLD_VALIDATED` permits restart only after old path and inode validation; record remains deferred |
| R18-3 guard opened source before recovery classification | D5/D11 classify filesystem state first, then guard an existing authoritative path; no SQLite open occurs for absent paths |
| R18-4 replacement crash state | replacement record binds old/new inodes and the prior verified destination remains until new copy verified and record updated |
| R18-5 DURABLE gate stage missing | system record stage is written/fsynced only after configuration directory fsync; ExecStartPre blocks through the interval |
| R18-1 branch-owned destination removed by general cleanup | `source_changed` and `source_in_use` are explicit preservation outcomes excluded from cleanup |
| R18-2 service left stopped after source holder deferral | `ABORTED_OLD_VALIDATED` preserves the pending record and permits the old service only after old config and inode validation |
| R18-3 guard opened absent source path before recovery | classify first, guard only an existing authoritative inode, refuse without SQLite open if absent |
| R18-4 recovery replacement lacked crash-state coverage | replacement binds old/new inodes and includes the backup/temp/final transition in recovery; preserved destination is not removed before source identity/holder checks |
| R18-5 DURABLE startup gate state missing | settings parent fsync, then durable record-stage update and parent fsync, then startup admission |
| R19-1 replacement final link could disappear | destination directory fsync precedes PUBLISHED; prior destination backup remains through durable commit |
| R19-2 DURABLE record publication ambiguous | same-boot witness and post-reboot persisted-stage rule |
| R18-1 cleanup erased branch-owned copies | explicit mismatch/holder outcomes preserve destination, source and record; generic cleanup excludes them |
| R18-2 active service stopped on holder deferral | ABORTED_OLD_VALIDATED keeps the target record and permits the validated old path until a later helper retry |
| R18-3 guard opened absent source before recovery classification | D5/D11 classify durable filesystem state before opening a guard on an existing path |
| R18-4 branch-1 replacement crash state was incomplete | replacement substate carries old, new-temp, backup and published identities with recovery for each transition |
| R18-5 DURABLE record stage was not persisted | config-directory fsync precedes record-stage write+fsync; ExecStartPre admits only persisted DURABLE state |
| R19-1 replacement final link could disappear before backup removal | link final, verify inode, fsync destination directory, then record PUBLISHED; preserve backup through commit |
| R19-2 visible DURABLE record stage lacked proof | same-boot volatile fsynced witness, or post-reboot observation of persisted stage bytes, gates ExecStartPre |
| R19-3 cleared abort record lost pending target | ABORTED_OLD_VALIDATED retains the record as a deferred request |
| R25-1 destination guard blocked daemon restart | branches 2/2b release destination guards before startup and retain recovery evidence through verification |
| R25-2 service RuntimeDirectory removed the witness | helper provisions the witness under /run/appio-setup; service stop cannot remove it |
| R25-3 settings lock release points conflicted | hold settings lock through record deletion or recovery-branch exit, finally-release on failure |
| R25-4 stale pending-operation errors in inventories | section 7 and window tests use changed_elsewhere/move_in_progress/settings_foreign as defined in D5 |
| R26-1 environment split pending coordination | one canonical per-UID pending record/operation lock, bound settings profile and source path; per-launch XDG database defaults preserved |
| R26-2 startup could overwrite foreign/stale settings | exact profile/source/generation admission before SQLite or retirement, explicit absent-generation 0 and post-commit recognition |
| R26-3 same path could stop service needlessly | `unchanged` preflight before transaction or stop |
| R26-4 decoder omitted post-start failure state | `service_start_failed` declared and tested |
| R27-1 cross-profile Cancel with a current operation ID | selected settings path and source binding are checked before operation ID in UI/API, with a two-environment regression test |
| R28-1 rollback rename could clobber a recreated old pathname | every manifest forward/restore move uses renameat2 RENAME_NOREPLACE relative to pinned descriptors; target conflicts preserve both files and the record |
| R29-1 retired-to-retained relocation could overwrite a previous retained file | the complete retention manifest uses RENAME_NOREPLACE for main/sidecars, collision preserves both inode sets and record, and final record removal follows all directory fsyncs |
| R30-1 retention recovery lacked durability/precedence and post-start helper handling | complete retention mapping and parent fsynced before moves; retention branch 0 precedes cleanup; post-start collision stops daemon and preserves durable destination, witness and record |
