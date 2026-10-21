# appio: P1 Contract Specification

Date: 2026-10-21  
Status: P1 contract draft derived from the approved revision-3 decisions in the supplied design body.  
Scope: measurement, aggregation, identity, SQLite storage, query semantics, and P1 error/degradation contracts. This document does not specify the daemon, socket protocol, BPF implementation, Qt UI, tray, packaging, or polkit, except for values those components must preserve.

## 1. Conformance and Scope

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. A requirement marked `TODO(design)` records a decision that the supplied design does not determine. The recommendation is not binding until the design owner resolves it. Implementations MUST NOT silently choose a different answer for an unresolved item.

P1 MUST provide interchangeable sampler/store components. P1 MUST NOT implement the daemon, socket protocol, BPF program, UI, tray, package, or privilege setup. P1 MUST reserve the stream fields `source`, `boot_id`, `epoch`, and `seq`, and file-coverage fields needed by later plans.

This specification treats the approved design body and its explicit decisions as authoritative. Its title says “revision 2” and “awaiting re-review,” while its section 13 describes revision-3 resolutions and section 14 records an agreed delivery decomposition. That source metadata inconsistency is recorded in section 13; it does not reopen the closed review history.

The concrete encodings, added storage columns, and algorithms explicitly linked to an appendix item are proposed contracts for review, not resolutions of that item. Fixtures depending on them MUST be marked provisional. Given the same database state, ordered input observations, supplied metadata, and ratified policies, implementations MUST produce equal row values; this does not require identical SQLite file bytes or page layout.

## 2. Measurement Model

### 2.1 T: task-accounted disk I/O

`T` is the sum of deltas of `/proc/<pid>/io` fields `read_bytes` and `write_bytes`, after the child-reap correction in section 5. It is per process identity, then attributed to an `AppKey` and launch. `rchar` and `wchar` are also stored as separate kernel counters but MUST NOT be added to `T` and MUST NOT be labelled disk I/O.

`T` is not SSD wear. It includes task accounting such as writes later cancelled or not flushed. A collector MUST NOT infer that each byte in `T` reached a physical device.

### 2.2 D: cgroup device bytes

`D` is the nonnegative delta of `rbytes` and `wbytes` in cgroup-v2 `io.stat`, grouped by app and device. It is block-layer accounting, not process accounting. It can include writeback charged to kernel workers and can exclude I/O after a task migrates out of a cgroup. A device row MUST never be replicated once per process in the cgroup.

The canonical stored device token is the decimal `major:minor` token from `io.stat` (for example `259:0`). Friendly device names are display metadata and MUST NOT be used as primary identity. `D` remains boot-scoped.

### 2.3 F: logical file activity

`F` is logical VFS read/write activity. For a file, store access counts and returned byte counts independently for reads and writes. Page-cache activity counts in `F`. `F` MUST NOT be added to or subtracted from `T` or `D`, and MUST NOT be labelled physical disk traffic.

Only explicitly counted omissions and overflow bytes may complete the file-activity accounting equation. `skipped_nonfile` and `skipped_unsupported_fs` describe excluded objects and MUST NOT be added to `F`.

### 2.4 Reconciliation equations

For any app and the same covered half-open window, define totals with signed, unbounded intermediate arithmetic:

```text
R       = T.read + T.write - sum(D.read + D.write)
R_read  = T.read - sum(D.read)
R_write = T.write - sum(D.write)
F_read  = sum(file_row.bytes_r) + counted_omitted_bytes_r + uncounted_overflow_bytes_r
F_write = sum(file_row.bytes_w) + counted_omitted_bytes_w + uncounted_overflow_bytes_w
F       = F_read + F_write
```

Therefore `sum(D) + R == T` and the corresponding read and write identities hold exactly. `R` is signed; a negative value means attributed device bytes exceed task-accounted bytes. It means “difference between accounting points,” not bytes lost to a named cause. `R` is derived at query time and is not persisted as an independent counter. Python results MUST use signed integers; SQLite unsigned-like counters are limited to `[0, 2^63-1]`.

If device accounting is unavailable for any part of a window, the query MUST return device attribution as unavailable/incomplete and MUST NOT substitute zero D or present a complete residual. Task totals remain available independently. Mixed device coverage is represented by `device_state="incomplete"` under section 4.3 and null aggregate device/residual values under section 8.2.

## 3. Identity Contracts

### 3.1 Process identity

A process identity is `(boot_id, pid, start_ticks)`, where `start_ticks` is field 22 (`starttime`) of `/proc/<pid>/stat`. `pid` is a positive integer and `start_ticks` is a nonnegative integer. Every accepted sample MUST read stat, io, and cgroup as one logical record and reread stat; a changed start time discards the entire sample. A PID reused with a different start time is a new identity and MUST never inherit the prior identity's baseline or credit.

### 3.2 AppKey grammar and resolution

The stable external key grammar is:

```text
AppKey = "unit:" UnitName
       | "desktop:" DesktopId
       | "exe:" AbsoluteExecutablePath
       | "scope:" CgroupPath
```

`AppKey` is a nonempty UTF-8 string without NUL. Under the proposed A07 encoding, each payload is nonempty and preserves the source UTF-8 bytes: ASCII letters, digits, and `/._:@+-` pass through; every other byte is `%HH` with uppercase hexadecimal. Literal `%` is therefore `%25`; safe bytes MUST NOT be unnecessarily escaped. A decoded executable/cgroup payload MUST start with `/`; a decoded unit/desktop payload MUST NOT contain `/`. The all-inputs-unavailable fallback is proposed as `scope:/session/unknown`. No case folding or Unicode normalization is applied to identity. `app_id` is a positive, database-local surrogate and MUST NOT be sent across stores or treated as identity. Stores correlate applications only by exact `AppKey` equality plus boot identity where required.

`resolve_app(cgroup_path, exe, desktop_id)` MUST use this precedence:

1. Deepest matching `app-*` systemd unit in the cgroup path.
2. Nearest ancestor `app.slice` unit.
3. Desktop ID, if supplied and valid.
4. Executable path, if absolute and available.
5. Session-scope identity.

Within the same precedence class, the candidate is the deepest path component; equal-depth candidates are ordered by bytewise UTF-8 unit name. The fallback must always return an `AppKey`; unavailable cgroup, desktop, and executable inputs resolve to a session-scope key.

`TODO(design) A07` records that the design gives examples and precedence but does not specify systemd escaping, desktop-ID canonicalization, executable symlink normalization, or the exact encoding of arbitrary cgroup components. The grammar and tie-break above are recommendations, not a silent claim that the design chose those encodings.

### 3.3 Launch identity

A launch row is scoped by `(app_id, boot_id, launch_id)`. `launch_id` is a positive local surrogate. Raw task samples MUST include a non-null `launch_id`; `0` is reserved for “launch not resolved” and MUST NOT appear in `launch`. Device/file rows are app-level and need not be split by launch.

`TODO(design) A06`: the design does not define which process starts or events open and close a launch, how multiple concurrent instances of one app are grouped, or how a launch is recovered after collector restart. Recommendation: define one launch per app-root process identity and descendants that resolve to the same AppKey; close only after all known member identities disappear. P2 MUST NOT claim launch/lifetime accuracy until this is ratified.

### 3.4 Collector stream identity

A stored observation is namespaced by `(boot_id, source, epoch, seq)`. Reserved P1 source values are `gui` and `iod`; a future source value MUST be a new string, not an alias. `epoch` is a positive integer allocated per source and boot; `seq` is a nonnegative integer strictly increasing within `(boot_id, source, epoch)`. `collector_epoch.last_seq` is the committed high-water mark for the active epoch. The writer MUST serialize frames in sequence order; before any baseline or database mutation it MUST reject a frame whose source/boot/epoch is not active or whose `seq <= last_seq`, as duplicate/stale. The high-water update and all rows/totals for the frame commit atomically. Rows are deduplicated only by their full declared primary key; stream replay is rejected by the high-water mark, never by wall time.

Each collector-owned interval is half-open `[from_mono_ns, to_mono_ns)`. Handover uses a single `cutoff_mono_ns`; the outgoing interval ends there and the incoming interval starts there. Baselines captured at the cutoff MUST prevent the incoming collector from re-emitting pre-cutoff counters. An uncovered interval MUST be represented by a `gap` row. If a handover record is missing and two ownership intervals overlap, both byte contributions in the overlap MUST be excluded and the overlap MUST be marked uncertain.

`TODO(design) A08`: the design names persisted epochs and sequence identity but does not specify atomic epoch allocation, sequence persistence, restart behavior, or the exact interval when a collector dies before closing its interval. Recommendation: allocate a new epoch transactionally on every process start, persist sequence before publishing each row, and close a crashed interval at the first durable evidence of the next collector's start.

## 4. Time, Buckets, Windows, and Coverage

### 4.1 Clocks and bucket arithmetic

All ordering and bucket keys use `CLOCK_BOOTTIME` nanoseconds. For bucket width `bucket_ns > 0` and an assigned observation timestamp `ts_mono_ns`:

```text
bucket_start_mono_ns = (ts_mono_ns // bucket_ns) * bucket_ns
bucket interval = [bucket_start_mono_ns, bucket_start_mono_ns + bucket_ns)
```

Bucket boundaries MUST be computed with integer division and MUST NOT use wall time. Wall labels use a persisted mapping `wall_ns = mono_ns + wall_offset_ns`; each offset is sampled from realtime and boottime at the same observation. A new `clock_map` segment starts at the current boottime timestamp when the absolute change from the previous offset is strictly greater than `1_000_000_000` ns. Historical labels use the segment with the greatest start timestamp not greater than the sample timestamp. An NTP step MUST NOT alter a bucket key or row order.

A new boot's first accepted observation MUST create its initial clock segment at `segment_start_mono_ns=0`, using that observation's offset; this extends the initial label mapping back to boot zero without asserting collection coverage there. Subsequent segments follow the threshold rule above. Raw durable observations have a nominal 10-second cadence; the 2-second live cache is not a second durable stream. The proposed raw series bucket width is `10_000_000_000` ns. Exact attribution timestamps for suspend-spanning observations and persisted observed-time denominators remain part of `TODO(design) A09`; they MUST NOT be inferred from a long delay alone.

A delta known to span suspend is assigned to the bucket of the last observation before suspend, and its sample row has `spans_suspend=1`. The rate denominator excludes the suspended interval. `spans_suspend` is a boolean integer, `0` or `1`; aggregation across rows uses logical OR, not addition.

`TODO(design) A09`: the design does not specify how suspend is detected, whether the suspend boundary is exact, how observed time is computed for a partially observed interval, or how multiple gaps and suspends interact. Recommendation: consume an explicit suspend/resume boundary source; subtract only measured suspended nanoseconds from the sample interval; never infer a suspend from a large sampling delay alone.

### 4.2 Windows and tier selection

Every query window is half-open. A bounded range is `[start_mono_ns, end_mono_ns)` with `start < end` and one `boot_id`. `since_boot` is from the boot's first boottime instant to the requested end. `launch` is bounded by that launch's stored start/end. `lifetime` spans all boots and is answered from lifetime totals only.

For a bounded window of duration `d = end - start`, tier selection is disjoint and MUST use these inclusive upper bounds:

| Condition | App/device source | Stated resolution |
|---|---|---|
| `d <= 24 hours` | raw `sample` and `sample_dev` | raw observations |
| `24 hours < d <= 30 days` | 60-second `rollup_app` and `rollup_dev` | 60 s |
| `30 days < d <= 365 days` | 3600-second rollups | 3600 s |
| `d > 365 days` or lifetime | totals tables | totals only; no time series or peak |

The source tier MUST NOT be blended with an adjacent tier for the same quantity. Results MUST disclose the selected tier and coverage. Selection by duration does not guarantee that the selected history is still retained: a short old window whose raw observations have expired remains unavailable rather than silently switching tiers. Exact partial-bucket inclusion at either window edge is unresolved; see `TODO(design) A10`. Recommendation: include only complete buckets wholly contained in the requested window, expose omitted boundary duration as incomplete coverage, and do not prorate bytes. A duration above 365 days selects the totals-only presentation, but arbitrary bounded and launch windows cannot necessarily be answered from the existing totals; see `TODO(design) A22`. A full-boot/lifetime total MUST NOT be substituted for a different requested range.

File data has its own 14-day `file_sample` retention and `app_boot_file_total` totals. The design does not define exact file-table behavior for 14-365 day bounded windows, nor whether `file_sample` is per drain or pre-aggregated to hourly top-100. See `TODO(design) A11`. Recommendation: answer only from retained file observations or exact full-boot totals; return explicit unavailable/incomplete coverage for an unrepresentable bounded window, never relabel a full-boot total as a bounded-window result.

### 4.3 Stored coverage quantities

All coverage counts are nonnegative integers no greater than `2^63-1`. They are event/identity counts, not byte counts, unless the name ends in `_bytes` or the value is one of the four explicit reap-counter components.

| Quantity | Type and unit | Required interpretation |
|---|---|---|
| `inaccessible_pids` | integer count | `/proc/<pid>/io` reads returning `EACCES`, deduplicated by process identity within one collector tick and summed across ticks in the query window |
| `resets` | integer count | identity records with one or more decreasing counters, plus `ESRCH` outcomes for identities already open; each such record/outcome contributes once |
| `reap_corrections` | integer count | child identities whose carried credits reduced a parent delta |
| `reap_unapplied` | object of four nonnegative byte counters | credit left when its parent identity disappears |
| `reap_unobserved` | object of four nonnegative byte counters | parent increments explicitly classified as an unseen reaped-child contribution |
| `first_seen_partial` | integer boolean on an app/launch sample row | `1` iff at least one contributing process identity was first observed with nonzero counters |
| `partial_intervals` | integer count | intervals containing at least one first-seen-partial identity |
| `gap_intervals` | integer count | uncovered intervals in the queried range |
| `overlap_uncertain_intervals` | integer count | ownership-overlap intervals excluded from totals |
| `spans_suspend_intervals` | integer count | intervals whose accepted deltas span suspend |
| `spans_suspend` | integer boolean on an app/launch sample row | `1` iff at least one contributing process interval spans suspend |
| `counted_omitted_files` | integer count | distinct file rows omitted by deterministic top-N/quota selection in the represented interval |
| `counted_omitted_bytes_r/w` | nonnegative integer bytes | exact bytes of those counted omitted rows |
| `uncounted_overflow_files` | integer count | failed map insert attempts; not a distinct-file estimate |
| `uncounted_overflow_bytes_r/w` | nonnegative integer bytes | exact bytes charged to overflow counters |
| `skipped_nonfile` | integer count | excluded non-file objects |
| `skipped_unsupported_fs` | integer count | excluded filesystem objects |
| `weak_identity_rows` | integer count | observed file identities whose `path.identity_weak=1`, including retained and omitted rows |
| `hook_hits` | object of six nonnegative integer counters | hits for `vfs_read`, `vfs_write`, `vfs_readv`, `vfs_writev`, `vfs_iter_read`, `vfs_iter_write`; zero means no observed hit, not proof of inactivity |
| `identity_weak` | integer boolean on `path` | `1` means the path key is weak and MUST NOT merge with a strong identity |

`identity_weak`, `first_seen_partial`, and `spans_suspend` are constrained to `{0,1}` in SQLite. Coverage over a union of source rows is computed after stream deduplication and interval selection: sum integer counts/bytes and the four reap components; sum all six `hook_hits`; OR boolean coverage flags. `device_state` is `incomplete` if any selected interval is incomplete or if `available` and `not_delegated` are mixed, `not_delegated` if all selected intervals are not delegated, and `available` if all are available. With no source rows it is `incomplete`. `hook_hits` are stored in the file subobject of `coverage_json` and in the six `hook_*` columns of `file_coverage`, so they survive the independent 14-day file retention; `weak_identity_rows` is stored in `file_coverage`. A result's `Coverage` MUST report the exact aggregation tier and whether the requested window is complete.

`TODO(design) A12`: the design does not define counter units for `inaccessible_pids`, `resets`, `reap_corrections`, `partial_intervals`, or `skipped_*`, nor a stable JSON schema for `coverage_json`. The table above is the recommendation used by this draft and requires ratification. In addition, the original raw `sample` schema has nowhere to persist most process-coverage counters; this draft adds `sample_coverage` rather than silently dropping them.

## 5. Aggregation Algorithm

### 5.1 Normative tick ordering

An implementation MUST process each tick in this order. All intermediate sums use arbitrary-precision integers; before SQLite insertion each stored counter MUST fit `[0, 2^63-1]`. It MUST NOT wrap, saturate, or coerce an out-of-range value to floating point.

```text
aggregate_tick(boot_id, source, epoch, seq, ts_mono_ns, wall_ns):
    BEGIN IMMEDIATE
    active = collector_epoch row for source
    if active is absent or (active.boot_id, active.epoch) != (boot_id, epoch)
       or (active.last_seq is not NULL and seq <= active.last_seq):
        ROLLBACK; return duplicate_or_stale without changing any baseline
    set active.last_seq = seq in this transaction
    persist/update clock_map using offset = wall_ns - ts_mono_ns
    stage baseline/credit changes in a private copy; publish it only after COMMIT
    snapshots = enumerate candidate PIDs and read each logical record:
        read stat_before, then io outcome and cgroup/exe/session metadata, then stat_after
        if an enumerated PID vanishes during any read (ESRCH or ENOENT):
            record one ESRCH outcome
            if its previously open identity is known: increment that app's resets once
            mark the old identity confirmed absent; continue
        if either stat read has another failure or the two start_ticks differ:
            mark this PID unresolved; preserve its prior state until a stable scan; continue
        identity = (boot_id, pid, stat_before.start_ticks)
        app_key = resolve_app(cgroup, exe, desktop_id/session metadata)
        accept (identity, app_key, ppid, io counters or EACCES, cgroup)

    for each validated identity in ascending numeric pid order:
        identity = (boot_id, pid, start_ticks)
        if an open identity with same pid but different start_ticks exists:
            close old identity; if it had a readable baseline, enqueue its last four cumulative counters as credit
                against its last known parent identity, if known
            discard all old baselines for that pid
        open/retain this exact identity and its app/parent metadata even if it has no readable baseline
        if io outcome is EACCES:
            add identity to this tick's inaccessible set for app_key
            preserve any same-identity readable baseline; do not emit a T delta; continue
        if identity has no baseline:
            if any current counter is nonzero:
                set first_seen_partial = 1
                emit current cumulative counters as the proposed initial delta
            else:
                set first_seen_partial = 0
                emit four zero deltas
            save current counters and parent identity as baseline
        else:
            for each of rchar, wchar, read_bytes, write_bytes independently:
                if current < previous:
                    delta[counter] = 0
                    baseline[counter] = current
                    mark this identity record reset
                else:
                    delta[counter] = current - previous
                    baseline[counter] = current
            increment resets once if any of the four counters decreased

    for each still-open identity whose PID was absent from the complete enumeration
        or was confirmed absent during the scan:
        close it once
        if it had a readable baseline and known parent, enqueue its last cumulative counters as credit
            against the last observed parent identity
        an identity seen only through EACCES has no counter credit to enqueue
    for each readable parent identity in ascending stable identity order:
        for counter in the fixed order (rchar, wchar, read_bytes, write_bytes):
            consume queued credits FIFO:
                applied = min(credit[counter], parent_delta[counter])
                parent_delta[counter] -= applied
                credit[counter] -= applied
                retain positive remainder for a later parent delta
        aggregate adjusted deltas by (AppKey, launch_id)
        attribute reap_corrections to the parent's AppKey; count each child identity once
    if a parent closes with remaining credit:
        add that remainder to reap_unapplied; attribute it to no app total

    aggregate cgroup io.stat deltas per (app, dev), never per process
    aggregate file events and file-coverage counters independently from T and D
    materialize one sample row per (app, launch) with at least one readable identity:
        sum each adjusted counter; first_seen_partial is OR across contributing identities
        spans_suspend is OR across contributing identity intervals
        delta_kind = 'reset' if any counter decreased,
                     else 'baseline' if any identity had no prior baseline,
                     else 'normal'
    materialize sample_dev, sample_coverage, file_sample, file_coverage
        including coverage rows with zero T/F rows; store per-tick inaccessible set counts
    update rollup and lifetime/boot totals once; commit all state with collector_epoch.last_seq
```

`first_seen_partial` on the app/launch sample row is `1` iff at least one contributing process identity was first observed with nonzero counters; `spans_suspend` is `1` iff at least one contributing interval spans suspend. `partial_intervals` increments once per app/frame if any contributing identity is first-seen partial, and `spans_suspend_intervals` increments once per app/frame if any contributing interval spans suspend; multiple launches in that frame do not multiply these counts. For aggregated `delta_kind`, `reset` takes precedence over `baseline`, and `baseline` over `normal`; `baseline` applies when any contributing identity had no prior readable baseline, including a zero-valued first read.

Credits are component-wise. Credit values are cumulative child counters because the parent receives the child's cumulative totals on reap. A credit is applied to a delta computed on or after the scan that detects disappearance and never makes an emitted delta negative; this includes the current parent delta in section 10's reap fixture. No parent delta means the credit carries unchanged. Increment `reap_corrections` only on the first positive application of a child's credit, even if later ticks consume more components. Queue order is disappearance sequence ascending, then `(child_pid, child_start_ticks)` ascending for simultaneous disappearances. If the parent vanishes first, remaining credit is recorded in its app's `reap_unapplied` and not folded into T. `reap_unobserved` MUST NOT be guessed from a large parent delta alone. Parent capture, nested reaps, and counter-reset interactions still require the A02/A04 policies; this ordering does not resolve those source ambiguities.

`TODO(design) A03`: polling alone does not identify which portion of a parent jump is a never-sampled child's reap charge or provide a threshold for a “jump.” Recommendation: until an explicit event/threshold contract exists, set `reap_unobserved` to zero and account the parent delta normally; do not label ordinary parent activity as an unseen reap.

`TODO(design) A04`: the design does not determine parent-identity capture across reparenting, a child disappearing between scans, or credit ordering when multiple children are reaped into one parent. Recommendation: preserve the last observed `(boot_id,pid,start_ticks)` parent identity and consume credits FIFO by disappearance sequence, then `(child_pid, child_start_ticks)` for simultaneous disappearances.

### 5.2 Cgroup and file deltas

For each cgroup/device pair, D is the current cumulative `io.stat` value minus the last value for the same boot and cgroup identity. A cgroup with no `io.stat` contributes no D row and is not equivalent to a zero-byte row. Counter-decrease and cgroup-recreation handling for D is unresolved (`TODO(design) A05`); recommendation: key baselines by `(boot_id,cgroup_id,dev)`, clamp a decrease to zero, and establish a fresh baseline.

F events are keyed by `(cgroup_id,dev,inode,generation)` in the later BPF plan and are translated to app ownership before persistence. F aggregation MUST preserve reads, writes, bytes_r, and bytes_w separately. Insertion overflow bytes are counted as unknown; they MUST NOT be made up from retained file rows.

### 5.3 Quota and prune job

The quota job runs every 300 seconds and after a bulk import. Before expiring or quota-deleting a source unit, the transaction MUST commit the exact 60-second/hour rollups and boot/lifetime totals promised for that unit; file totals MUST be committed before deleting file observations. Expiry uses mapped wall nanoseconds: for `(boot_id, mono_ns)`, select `wall_offset_ns` from the `clock_map` row in that boot with greatest `segment_start_mono_ns <= mono_ns`, then compute `mono_ns + wall_offset_ns`. The current comparison time is mapped by the same rule. If either mapping is absent, pruning MUST stop before deleting a row and mark coverage incomplete rather than guess. A row is expired only when its mapped time is strictly less than `now_wall_ns - retention_ns`.

Retention and deletion units are independent: `sample`, `sample_dev`, and `sample_coverage` expire after 24 hours; `file_sample` and `file_coverage` expire after 14 days; 60-second rollups expire after 30 days; 3600-second rollups expire after 365 days; totals and `path` identities referenced by totals are never expired. `sample_coverage` and `file_coverage` MUST have one row per app/stream observation even when the corresponding T or F row set is empty. This preserves per-app time and lets 14-day file observations outlive 24-hour process samples. `gap` rows expire after 24 hours; rollup coverage retains older gap effects.

Under quota pressure, select candidates in tier order: raw units, 60-second app buckets, then 3600-second app buckets. Within each tier select the smallest deterministic key. A core raw unit is `(boot_id, source, epoch, seq, app_id)` and deletes all `sample` launches plus matching `sample_dev` and `sample_coverage` rows. A file raw unit is the same key and deletes only matching `file_sample` and `file_coverage` rows. A gap is its own unit keyed by `gap_id`. A rollup unit is `(boot_id, app_id, bucket_secs, bucket_start_mono_ns)` and includes the corresponding `rollup_app` row and all `rollup_dev` rows. Within a tier, the primary sort value is mapped wall time computed from `sample_coverage.ts_mono_ns` for core units, `file_coverage.ts_mono_ns` for file units, `gap.to_mono_ns` for gaps, and `bucket_start_mono_ns` for rollups. Remaining keys sort by `boot_id` UTF-8 bytes, `source` UTF-8 bytes (empty for rollups), `epoch` (zero for rollups), `seq` (zero for rollups), `app_id` (zero for gaps), unit kind (`core`, `file`, `gap`, then `rollup`), `bucket_secs` (zero for raw/gap units), and `gap_id` for gaps or zero otherwise. No child row may outlive a deleted core raw unit; file observations are independent units by design.

```text
quota_prune(boot_id, now_mono_ns):
    BEGIN IMMEDIATE
    ensure exact due rollups and boot/lifetime totals already exist; do not re-add them
    map the current boottime to now_wall_ns
    delete expired core raw units using 24h cutoff
    delete expired file raw units using 14d cutoff
    delete expired gap units using 24h cutoff
    delete expired 60-second rollup units using 30d cutoff
    delete expired 3600-second rollup units using 365d cutoff
    garbage-collect path_alias rows not referenced by retained observations or totals
    garbage-collect path rows not referenced by retained observations or totals
    COMMIT
    checkpoint WAL with TRUNCATE
    while physical_store_bytes() > 1_500_000_000:
        if freelist_count() > 0:
            VACUUM
            if physical_store_bytes() <= 1_500_000_000: break
        choose and delete the first eligible raw unit by the deterministic key above
        if a raw unit was deleted: checkpoint; continue
        choose and delete the first eligible 60-second app bucket by the key above
        if a 60-second bucket was deleted: checkpoint; continue
        choose and delete the first eligible 3600-second app bucket by the key above
        if a 3600-second bucket was deleted: checkpoint; continue
        pause sampling before accepting another durable sample
        expose quota_exhausted state and stop pruning
        return
    resume sampling only after a fresh physical_store_bytes() check is <= 1_500_000_000
```

`physical_store_bytes()` is the sum of `st_size` for the main database, `-wal`, and `-shm` files after a successful checkpoint; a missing sidecar contributes zero. The loop commits each deletion unit and checkpoints before measuring again. For raw-unit key sorting, the final row key is `gap_id` for a gap and zero for core, file, and rollup units. `TODO(design) A13`: the design specifies neither decimal versus binary GB, physical versus logical byte accounting, cross-boot age, nor tie ordering. This draft recommends a decimal 1,500,000,000-byte hard limit, mapped-wall age across boots, file lengths as measured above, and the exact ordering stated here. These choices require ratification. Totals are never deleted; if all eligible history has been pruned and the store remains over quota, sampling pauses rather than silently exceeding the limit.

### 5.4 Deterministic materialization

For each newly accepted frame, app/launch sample counters are component-wise sums of adjusted process deltas. Each cgroup/device baseline is consumed once, then its D delta is summed into the app/device row; the proposed first cgroup observation establishes a baseline and emits zero. F candidates with the same producer identity are summed before top-N selection. Counted omissions and overflow are never re-added to retained rows.

For each app/frame, coverage is reduced once across its identities, independently of the number of launches. Insert its coverage once into each selected 60-second and 3600-second bucket, boot total, and lifetime total. Both rollup widths receive the frame directly; an hourly row MUST NOT also add a minute row containing the same frame. All additive UPSERTs execute only behind the committed sequence guard. Retention/pruning jobs validate or rebuild from a complete source snapshot; they MUST NOT replay additive UPSERTs into existing totals.

Within a boot, ordinary deltas go to `floor(ts_mono_ns / width_ns) * width_ns`. Suspend attribution and observation duration use A09, not wall labels. Proposed A16 peak semantics are the maximum defined app-interval rate in the represented bucket/window: `floor(delta_bytes * 1_000_000_000 / observed_ns)` for positive observed duration. A baseline without an observed interval has no rate; persisted peak fields use zero until a defined rate exists, while incomplete coverage prevents that placeholder from implying a measured peak. The same frame's rate feeds both rollup widths and its boot peak. Totals-only query peaks remain `None`. This numeric and temporal peak policy requires A16 ratification.

Surrogate IDs are allocated as one greater than the current maximum positive ID (or `1` for an empty table), with new apps sorted by AppKey UTF-8 bytes, launches by `(app_id, started_mono_ns, root_pid, root_start_ticks)`, and paths by their declared uniqueness tuple. Existing identities reuse their IDs. Text metadata is supplied input, not locale-derived. Canonical JSON uses sorted keys, UTF-8, `ensure_ascii=False`, separators `(',', ':')`, decimal integer tokens, and no trailing LF or nonfinite values. These rules specify equal row values from equal prior state without promising equal database files.

## 6. SQLite Schema

### 6.1 Types and constraints

SQLite uses signed 64-bit `INTEGER`. Every table below is `STRICT`; the minimum supported SQLite version is 3.37.0 with the JSON functions available. Nonnegative counters and timestamps have range `[0, 9223372036854775807]`; explicitly signed integers use `[-9223372036854775808, 9223372036854775807]`. `pid` and local surrogate IDs are positive integers; `start_ticks` is nonnegative. `ts_wall`, `started_wall`, and app `first_seen`/`last_seen` are Unix epoch seconds as finite nonnegative binary64 SQLite `REAL`; conversion from integer wall nanoseconds divides by `1_000_000_000` with round-to-nearest, ties-to-even. Booleans are integer `0` or `1`. `STRICT` enforces storage classes after SQLite affinity conversion; the Python boundary MUST reject bools as integers and MUST validate types/ranges before binding, rather than rely on coercion.

`coverage_json` is UTF-8 JSON object text. Its top-level object has exactly `inaccessible_pids`, `resets`, `reap_corrections`, `reap_unapplied`, `reap_unobserved`, `partial_intervals`, `gap_intervals`, `overlap_uncertain_intervals`, `spans_suspend_intervals`, `device_state`, and `file`. The integer counters and each of the four reap-counter components have range `[0, 2^63-1]`; `device_state` is `available`, `not_delegated`, or `incomplete`. The `file` object has exactly the nine numeric quantities in section 4.3 plus `hook_hits`, whose object has exactly the six named counters, each in `[0, 2^63-1]`. Booleans are not accepted in place of integers. The implementation MUST validate the exact object shape, component names, and value ranges in Python in addition to SQLite `json_valid`; `reap_unapplied` and `reap_unobserved` each have exactly `rchar`, `wchar`, `read_bytes`, and `write_bytes`. All coverage rows are inserted in the same transaction as their matching stream receipt and observations. The numeric file fields in `sample_coverage.coverage_json.file` MUST equal the structured columns in `file_coverage` for the same `(boot_id, source, epoch, seq, app_id)`; the six `hook_*` columns mirror the corresponding `hook_hits` fields. Both rows are present with zero file values when the app has an observation but no F row.

`TODO(design) A14`: precise JSON keys and aggregation operators are not provided by the design. These keys are a recommended compatibility contract and must be ratified before two stores exchange coverage.

### 6.2 DDL

`file_coverage` adds `ts_mono_ns`, `weak_identity_rows`, and six `hook_*` counters so bounded-window file coverage survives independently of 24-hour process retention. This DDL also adds `sample_coverage`, `collector_epoch.last_seq` (nullable only before the first committed frame), `clock_map.boot_id`, `gap.gap_id`, boot IDs alongside the first/last monotonic path and alias timestamps, `identity_weak` to the path uniqueness key, and `coverage_json` plus `updated_boot_id` to lifetime totals. Each addition makes an existing query, identity, or recovery contract representable; these additions and their effect on the design's measured-byte table are not present in its abbreviated DDL. `path` and `path_alias` boot ordering remains unresolved in `TODO(design) A20`.

```sql
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE boot (
    boot_id TEXT PRIMARY KEY NOT NULL CHECK (length(boot_id) > 0),
    started_mono_ns INTEGER NOT NULL CHECK (started_mono_ns >= 0),
    started_wall REAL NOT NULL CHECK (started_wall BETWEEN 0.0 AND 1.7976931348623157e308),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod'))
) STRICT;

CREATE TABLE app (
    app_id INTEGER PRIMARY KEY NOT NULL CHECK (app_id > 0),
    key TEXT NOT NULL UNIQUE CHECK (length(key) > 0),
    name TEXT NOT NULL CHECK (length(name) > 0),
    icon_path TEXT,
    first_seen REAL NOT NULL CHECK (first_seen BETWEEN 0.0 AND 1.7976931348623157e308),
    last_seen REAL NOT NULL CHECK (last_seen BETWEEN first_seen AND 1.7976931348623157e308),
    scope TEXT NOT NULL CHECK (scope IN ('user', 'system'))
) STRICT;

CREATE TABLE launch (
    launch_id INTEGER PRIMARY KEY NOT NULL CHECK (launch_id > 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    started_mono_ns INTEGER NOT NULL CHECK (started_mono_ns >= 0),
    ended_mono_ns INTEGER CHECK (ended_mono_ns IS NULL OR ended_mono_ns >= started_mono_ns),
    proc_count INTEGER NOT NULL CHECK (proc_count >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    UNIQUE (launch_id, app_id, boot_id)
) STRICT;

CREATE TABLE sample (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    launch_id INTEGER NOT NULL CHECK (launch_id >= 0),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    ts_wall REAL NOT NULL CHECK (ts_wall BETWEEN 0.0 AND 1.7976931348623157e308),
    rchar INTEGER NOT NULL CHECK (rchar BETWEEN 0 AND 9223372036854775807),
    wchar INTEGER NOT NULL CHECK (wchar BETWEEN 0 AND 9223372036854775807),
    read_bytes INTEGER NOT NULL CHECK (read_bytes BETWEEN 0 AND 9223372036854775807),
    write_bytes INTEGER NOT NULL CHECK (write_bytes BETWEEN 0 AND 9223372036854775807),
    delta_kind TEXT NOT NULL CHECK (delta_kind IN ('normal', 'baseline', 'reset')),
    first_seen_partial INTEGER NOT NULL CHECK (first_seen_partial IN (0, 1)),
    spans_suspend INTEGER NOT NULL CHECK (spans_suspend IN (0, 1)),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id, launch_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE sample_coverage (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE sample_dev (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    rbytes INTEGER NOT NULL CHECK (rbytes BETWEEN 0 AND 9223372036854775807),
    wbytes INTEGER NOT NULL CHECK (wbytes BETWEEN 0 AND 9223372036854775807),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id, dev)
) WITHOUT ROWID, STRICT;

CREATE TABLE path (
    path_id INTEGER PRIMARY KEY NOT NULL CHECK (path_id > 0),
    fs_uuid TEXT NOT NULL,
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    inode INTEGER NOT NULL CHECK (inode BETWEEN 0 AND 9223372036854775807),
    generation INTEGER NOT NULL CHECK (generation BETWEEN 0 AND 4294967295),
    basename TEXT NOT NULL CHECK (length(basename) > 0),
    state TEXT NOT NULL CHECK (state IN ('resolved', 'truncated', 'unresolved')),
    identity_weak INTEGER NOT NULL CHECK (identity_weak IN (0, 1)),
    first_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    first_mono_ns INTEGER NOT NULL CHECK (first_mono_ns >= 0),
    last_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    last_mono_ns INTEGER NOT NULL CHECK (last_mono_ns >= 0),
    CHECK (first_boot_id <> last_boot_id OR last_mono_ns >= first_mono_ns),
    UNIQUE (fs_uuid, dev, inode, generation, basename, identity_weak)
) STRICT;

CREATE TABLE path_alias (
    path_id INTEGER NOT NULL REFERENCES path(path_id) ON DELETE CASCADE,
    basename TEXT NOT NULL CHECK (length(basename) > 0),
    first_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    first_mono_ns INTEGER NOT NULL CHECK (first_mono_ns >= 0),
    last_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    last_mono_ns INTEGER NOT NULL CHECK (last_mono_ns >= 0),
    CHECK (first_boot_id <> last_boot_id OR last_mono_ns >= first_mono_ns),
    PRIMARY KEY (path_id, basename)
) WITHOUT ROWID, STRICT;

CREATE TABLE file_sample (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    path_id INTEGER NOT NULL REFERENCES path(path_id),
    reads INTEGER NOT NULL CHECK (reads BETWEEN 0 AND 9223372036854775807),
    writes INTEGER NOT NULL CHECK (writes BETWEEN 0 AND 9223372036854775807),
    bytes_r INTEGER NOT NULL CHECK (bytes_r BETWEEN 0 AND 9223372036854775807),
    bytes_w INTEGER NOT NULL CHECK (bytes_w BETWEEN 0 AND 9223372036854775807),
    last_seen_mono_ns INTEGER NOT NULL CHECK (last_seen_mono_ns >= 0),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id, path_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE file_coverage (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    counted_omitted_files INTEGER NOT NULL CHECK (counted_omitted_files >= 0),
    counted_omitted_bytes_r INTEGER NOT NULL CHECK (counted_omitted_bytes_r >= 0),
    counted_omitted_bytes_w INTEGER NOT NULL CHECK (counted_omitted_bytes_w >= 0),
    uncounted_overflow_files INTEGER NOT NULL CHECK (uncounted_overflow_files >= 0),
    uncounted_overflow_bytes_r INTEGER NOT NULL CHECK (uncounted_overflow_bytes_r >= 0),
    uncounted_overflow_bytes_w INTEGER NOT NULL CHECK (uncounted_overflow_bytes_w >= 0),
    skipped_nonfile INTEGER NOT NULL CHECK (skipped_nonfile >= 0),
    skipped_unsupported_fs INTEGER NOT NULL CHECK (skipped_unsupported_fs >= 0),
    weak_identity_rows INTEGER NOT NULL CHECK (weak_identity_rows >= 0),
    hook_vfs_read INTEGER NOT NULL CHECK (hook_vfs_read >= 0),
    hook_vfs_write INTEGER NOT NULL CHECK (hook_vfs_write >= 0),
    hook_vfs_readv INTEGER NOT NULL CHECK (hook_vfs_readv >= 0),
    hook_vfs_writev INTEGER NOT NULL CHECK (hook_vfs_writev >= 0),
    hook_vfs_iter_read INTEGER NOT NULL CHECK (hook_vfs_iter_read >= 0),
    hook_vfs_iter_write INTEGER NOT NULL CHECK (hook_vfs_iter_write >= 0),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE collector_epoch (
    source TEXT PRIMARY KEY NOT NULL CHECK (source IN ('gui', 'iod')),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    last_seq INTEGER CHECK (last_seq IS NULL OR last_seq >= 0),
    allocated_mono_ns INTEGER NOT NULL CHECK (allocated_mono_ns >= 0)
) STRICT;

CREATE TABLE collector_interval (
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    from_mono_ns INTEGER NOT NULL CHECK (from_mono_ns >= 0),
    to_mono_ns INTEGER NOT NULL CHECK (to_mono_ns > from_mono_ns),
    PRIMARY KEY (source, boot_id, epoch)
) WITHOUT ROWID, STRICT;

CREATE TABLE handover (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    from_source TEXT NOT NULL CHECK (from_source IN ('gui', 'iod')),
    to_source TEXT NOT NULL CHECK (to_source IN ('gui', 'iod') AND to_source <> from_source),
    cutoff_mono_ns INTEGER NOT NULL CHECK (cutoff_mono_ns >= 0),
    baselines_json TEXT NOT NULL CHECK (json_valid(baselines_json)),
    PRIMARY KEY (boot_id, cutoff_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE clock_map (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    segment_start_mono_ns INTEGER NOT NULL CHECK (segment_start_mono_ns >= 0),
    wall_offset_ns INTEGER NOT NULL,
    PRIMARY KEY (boot_id, segment_start_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE rollup_app (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    bucket_start_mono_ns INTEGER NOT NULL CHECK (bucket_start_mono_ns >= 0),
    bucket_secs INTEGER NOT NULL CHECK (bucket_secs IN (60, 3600)),
    read_bytes INTEGER NOT NULL CHECK (read_bytes >= 0),
    write_bytes INTEGER NOT NULL CHECK (write_bytes >= 0),
    rchar INTEGER NOT NULL CHECK (rchar >= 0),
    wchar INTEGER NOT NULL CHECK (wchar >= 0),
    peak_read_bps INTEGER NOT NULL CHECK (peak_read_bps >= 0),
    peak_write_bps INTEGER NOT NULL CHECK (peak_write_bps >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs)
) WITHOUT ROWID, STRICT;

CREATE TABLE rollup_dev (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    bucket_start_mono_ns INTEGER NOT NULL CHECK (bucket_start_mono_ns >= 0),
    bucket_secs INTEGER NOT NULL CHECK (bucket_secs IN (60, 3600)),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    rbytes INTEGER NOT NULL CHECK (rbytes >= 0),
    wbytes INTEGER NOT NULL CHECK (wbytes >= 0),
    PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs, dev)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_total (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    read_bytes INTEGER NOT NULL CHECK (read_bytes >= 0),
    write_bytes INTEGER NOT NULL CHECK (write_bytes >= 0),
    rchar INTEGER NOT NULL CHECK (rchar >= 0),
    wchar INTEGER NOT NULL CHECK (wchar >= 0),
    peak_read_bps INTEGER NOT NULL CHECK (peak_read_bps >= 0),
    peak_write_bps INTEGER NOT NULL CHECK (peak_write_bps >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, app_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_dev_total (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    rbytes INTEGER NOT NULL CHECK (rbytes >= 0),
    wbytes INTEGER NOT NULL CHECK (wbytes >= 0),
    PRIMARY KEY (boot_id, app_id, dev)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_file_total (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    path_id INTEGER NOT NULL REFERENCES path(path_id),
    bytes_r INTEGER NOT NULL CHECK (bytes_r >= 0),
    bytes_w INTEGER NOT NULL CHECK (bytes_w >= 0),
    reads INTEGER NOT NULL CHECK (reads >= 0),
    writes INTEGER NOT NULL CHECK (writes >= 0),
    PRIMARY KEY (boot_id, app_id, path_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_lifetime_total (
    app_id INTEGER PRIMARY KEY NOT NULL REFERENCES app(app_id) CHECK (app_id > 0),
    read_bytes INTEGER NOT NULL CHECK (read_bytes >= 0),
    write_bytes INTEGER NOT NULL CHECK (write_bytes >= 0),
    rchar INTEGER NOT NULL CHECK (rchar >= 0),
    wchar INTEGER NOT NULL CHECK (wchar >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    updated_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    updated_mono_ns INTEGER NOT NULL CHECK (updated_mono_ns >= 0)
) STRICT;

CREATE TABLE gap (
    gap_id INTEGER PRIMARY KEY NOT NULL CHECK (gap_id > 0),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    from_mono_ns INTEGER NOT NULL CHECK (from_mono_ns >= 0),
    to_mono_ns INTEGER NOT NULL CHECK (to_mono_ns > from_mono_ns),
    reason TEXT NOT NULL CHECK (reason IN ('collector_gap', 'overlap_uncertain', 'gui_absent', 'quota_exhausted'))
) STRICT;

CREATE INDEX app_name_idx ON app(name COLLATE NOCASE, key);
CREATE INDEX launch_app_time_idx ON launch(app_id, boot_id, started_mono_ns);
CREATE INDEX sample_app_time_idx ON sample(app_id, boot_id, ts_mono_ns, source, epoch, seq);
CREATE INDEX sample_coverage_app_time_idx ON sample_coverage(app_id, boot_id, ts_mono_ns, source, epoch, seq);
CREATE INDEX sample_dev_app_time_idx ON sample_dev(app_id, boot_id, ts_mono_ns, dev);
CREATE INDEX path_basename_idx ON path(basename COLLATE NOCASE, path_id);
CREATE INDEX path_alias_basename_idx ON path_alias(basename COLLATE NOCASE, path_id);
CREATE INDEX file_sample_app_time_idx ON file_sample(app_id, boot_id, ts_mono_ns, path_id);
CREATE INDEX file_sample_last_seen_idx ON file_sample(app_id, boot_id, last_seen_mono_ns DESC, path_id);
CREATE INDEX file_coverage_app_time_idx ON file_coverage(app_id, boot_id, ts_mono_ns, source, epoch, seq);
CREATE INDEX collector_interval_time_idx ON collector_interval(boot_id, from_mono_ns, to_mono_ns);
CREATE INDEX rollup_app_time_idx ON rollup_app(app_id, boot_id, bucket_secs, bucket_start_mono_ns);
CREATE INDEX rollup_dev_time_idx ON rollup_dev(app_id, boot_id, bucket_secs, bucket_start_mono_ns, dev);
CREATE INDEX gap_time_idx ON gap(boot_id, from_mono_ns, to_mono_ns, source, epoch);

PRAGMA user_version = 1;
COMMIT;
```

### 6.3 WITHOUT ROWID and index rationale

Composite-key observation tables use `WITHOUT ROWID` because their full composite keys address the records directly, avoiding a second rowid-to-key index. `path`, `app`, `launch`, and `app_lifetime_total` retain rowids so their integer primary keys alias the rowid without a separate primary-key B-tree. Single-key metadata tables `boot` and `collector_epoch` retain conventional rowid storage; its extra text-key index cost is included in schema measurements. `gap` retains a rowid-backed surrogate because gap intervals may share stream coordinates after recovery and require independently addressable records.

The primary keys on `handover` and `clock_map` already provide their boot/cutoff and boot/segment lookup order, so no duplicate secondary indexes are created. The secondary indexes cover app/time filtering and the supported path basename / last-seen sort. Additional indexes MUST be justified by an observed query plan and included in quota measurements; no index may change the result order specified in section 8.

### 6.4 Empty-database migration

For a new empty DB, a migration runner MUST enable foreign keys outside the transaction, execute the single `BEGIN IMMEDIATE` in section 6.2, and then confirm no user tables exist and `PRAGMA user_version = 0` before executing that block's first `CREATE TABLE`. It executes the remaining statements and sets `user_version=1` before the block's single commit. The runner MUST NOT wrap the block in another transaction or use an executor that implicitly commits between statements. Failed preconditions or DDL MUST roll back. If a nonempty database has `user_version=0`, startup MUST fail closed and preserve the file; it MUST NOT guess that the file is empty or rewrite it. If `user_version=1`, startup MUST validate required columns, constraints, tables, and indexes without recreating them. Any unsupported higher version MUST be rejected without mutation.

The database schema is created independently in each store. Integer `app_id`, `launch_id`, `path_id`, and `gap_id` values are local and MUST NOT cross store boundaries. External identity uses `AppKey`, `boot_id`, and stream identity.

## 7. Storage Budget

### 7.1 Measured-bytes table

The design reports measured SQLite payload sizes (record header included) and a 1.5 GB hard quota. Decimal MB is used below (`1 MB = 1,000,000 bytes`); rows/day use 107 apps and two devices. Exact multiplication is shown separately from the rounded MB figures.

| Tier | Rows/day formula | Bytes/row | Exact bytes/day | Retention | Exact retained payload |
|---|---:|---:|---:|---:|---:|
| raw `sample`, 10 s | `107 * 8640 = 924480` | 72 | 66,562,560 | 1 day | 66,562,560 |
| raw `sample_dev`, 10 s | `107 * 2 * 8640 = 1848960` | 40 | 73,958,400 | 1 day | 73,958,400 |
| `file_sample`, top-100/app/hour | `107 * 24 * 100 = 256800` | 88 | 22,598,400 | 14 days | 316,377,600 |
| `rollup_app`, 60 s | `107 * 1440 = 154080` | 64 | 9,861,120 | 30 days | 295,833,600 |
| `rollup_dev`, 60 s | `107 * 1440 * 2 = 308160` | 56 | 17,256,960 | 30 days | 517,708,800 |
| `rollup_*`, 3600 s | `107 * 24 * 3 = 7704` | 64 | 493,056 | 365 days | 179,965,440 |
| totals | fixed | n/a | n/a | forever | `< 1,000,000` stated |

The payload subtotal excluding totals is exactly 1,450,406,400 bytes, the sum of the exact retained-payload cells above. The design's rounded steady-state entries (`66 + 74 + 322 + 296 + 518 + 181 MB`) sum to 1,457 MB before totals. The `file_sample` exact product is 316,377,600 bytes, not 322,000,000 bytes. These budget arithmetic discrepancies and the difference between measured payload sizes and this expanded DDL are recorded as design contradictions in section 13. The quota check, not this planning estimate, is the enforceable limit.

`TODO(design) A15`: the design does not state whether its 1.5 GB limit is decimal or binary, or how its measured payload bytes map to a database containing indexes, coverage rows, free pages, and WAL. Recommendation: hard-limit allocated file sizes at 1,500,000,000 bytes and treat the table as planning data until measurements are repeated on this exact schema.

### 7.2 Retention and selection

Default retention is raw app/device samples 24 hours, file observations 14 days, 60-second app/device rollups 30 days, 3600-second rollups 365 days, and boot/lifetime totals forever. Quota pressure can prune oldest eligible buckets before these maxima; totals are never deleted.

File top-N ordering is descending `bytes_r + bytes_w`, then descending `reads + writes`, then ascending `(dev, inode, generation)` as a deterministic total order. Basename is not a tiebreaker. The omitted remainder is stored in `file_coverage` as counted omission; full-map insertion failures are stored separately as uncounted overflow. The Files result is labelled “retained observations” because a per-hour top-N can hide a file that led a larger window.

The design states `top-100 per app per hour`; it does not determine whether selection is by interval, by hour, or after aggregation, nor how separate collectors' budgets combine. These choices are included in `TODO(design) A11`.

## 8. Query Contracts

### 8.1 Python types

```python
from dataclasses import dataclass
from typing import Literal

CounterName = Literal["rchar", "wchar", "read_bytes", "write_bytes"]
Metric = Literal["total_bytes", "read_bytes", "write_bytes", "rchar", "wchar"]
FileSort = Literal["bytes_total", "bytes_r", "bytes_w", "reads", "writes",
                   "basename", "dev", "last_seen"]
Direction = Literal["asc", "desc"]
Tier = Literal["raw", "rollup_60s", "rollup_3600s", "totals_only"]
UnavailableReason = Literal["not_retained", "unsupported_window",
                            "launch_attribution", "file_source_unavailable"]

class WindowUnavailableError(ValueError):
    reason: UnavailableReason

@dataclass(frozen=True)
class Window:
    kind: Literal["range", "since_boot", "launch", "lifetime"]
    boot_id: str | None = None
    start_mono_ns: int | None = None
    end_mono_ns: int | None = None
    launch_id: int | None = None

@dataclass(frozen=True)
class HookHits:
    vfs_read: int
    vfs_write: int
    vfs_readv: int
    vfs_writev: int
    vfs_iter_read: int
    vfs_iter_write: int

@dataclass(frozen=True)
class Coverage:
    tier: Tier
    complete: bool
    unavailable_reason: UnavailableReason | None  # query-derived, not a persisted counter
    inaccessible_pids: int
    resets: int
    reap_corrections: int
    reap_unapplied: tuple[int, int, int, int]  # rchar, wchar, read_bytes, write_bytes
    reap_unobserved: tuple[int, int, int, int]
    partial_intervals: int
    gap_intervals: int
    overlap_uncertain_intervals: int
    spans_suspend_intervals: int
    spans_suspend: bool
    device_state: Literal["available", "not_delegated", "incomplete"]
    counted_omitted_files: int
    counted_omitted_bytes_r: int
    counted_omitted_bytes_w: int
    uncounted_overflow_files: int
    uncounted_overflow_bytes_r: int
    uncounted_overflow_bytes_w: int
    skipped_nonfile: int
    skipped_unsupported_fs: int
    weak_identity_rows: int
    hook_hits: HookHits

@dataclass(frozen=True)
class AppTotal:
    app_id: int
    app_key: str
    name: str
    read_bytes: int
    write_bytes: int
    total_bytes: int
    rchar: int
    wchar: int
    peak_read_bps: int | None
    peak_write_bps: int | None
    coverage: Coverage

@dataclass(frozen=True)
class SeriesPoint:
    bucket_start_mono_ns: int
    bucket_secs: int
    wall_start_seconds: float | None
    read_bytes: int
    write_bytes: int
    peak_read_bps: int | None
    peak_write_bps: int | None
    spans_suspend: bool

@dataclass(frozen=True)
class DeviceSeriesPoint:
    bucket_start_mono_ns: int
    bucket_secs: int
    dev: str
    rbytes: int
    wbytes: int

@dataclass(frozen=True)
class ResidualPoint:
    bucket_start_mono_ns: int
    bucket_secs: int
    total: int | None
    read: int | None
    write: int | None

@dataclass(frozen=True)
class Series:
    app_id: int
    window: Window
    tier: Tier
    points: tuple[SeriesPoint, ...]
    device_points: tuple[DeviceSeriesPoint, ...]
    residual_points: tuple[ResidualPoint, ...]
    coverage: Coverage

@dataclass(frozen=True)
class DeviceShareRow:
    boot_id: str  # major:minor is meaningful only within this boot
    dev: str
    rbytes: int
    wbytes: int
    total_bytes: int

@dataclass(frozen=True)
class DiskShare:
    app_id: int
    window: Window
    task_read_bytes: int
    task_write_bytes: int
    task_total_bytes: int
    devices: tuple[DeviceShareRow, ...]
    device_read_bytes: int | None
    device_write_bytes: int | None
    device_total_bytes: int | None
    residual_read: int | None
    residual_write: int | None
    residual_total: int | None
    coverage: Coverage

@dataclass(frozen=True)
class FileRow:
    path_id: int
    basename: str
    state: Literal["resolved", "truncated", "unresolved"]
    identity_weak: bool
    dev: str
    inode: int
    generation: int
    reads: int
    writes: int
    bytes_r: int
    bytes_w: int
    bytes_total: int
    last_seen_boot_id: str
    last_seen_mono_ns: int

@dataclass(frozen=True)
class FileTable:
    app_id: int
    window: Window
    rows: tuple[FileRow, ...]
    sort: FileSort
    direction: Direction
    search: str | None
    coverage: Coverage

@dataclass(frozen=True)
class FileCoverage:
    app_id: int
    window: Window
    counted_omitted_files: int
    counted_omitted_bytes_r: int
    counted_omitted_bytes_w: int
    uncounted_overflow_files: int
    uncounted_overflow_bytes_r: int
    uncounted_overflow_bytes_w: int
    skipped_nonfile: int
    skipped_unsupported_fs: int
    weak_identity_rows: int
    hook_hits: HookHits
    observed_bytes_r: int
    observed_bytes_w: int
    coverage: Coverage
```

`TODO(design) A16`: the design does not specify the numeric representation or temporal grain of peak rates. The proposed contract uses the maximum defined app-interval integer rate, calculated by exact floor division as in section 5.4. UI formatting SHOULD round to the nearest displayed unit, ties upward. This requires ratification: truncating each rate changes it by less than 1 B/s, and choosing interval peaks rather than whole-bucket averages can change it by more.

### 8.2 Exact function signatures and ordering

```python
def ranking(window: Window, metric: Metric) -> list[AppTotal]: ...
def series(app_id: int, window: Window) -> Series: ...
def disk_share(app_id: int, window: Window) -> DiskShare: ...
def file_table(
    app_id: int,
    window: Window,
    sort: FileSort,
    direction: Direction,
    search: str | None,
) -> FileTable: ...
def file_coverage(app_id: int, window: Window) -> FileCoverage: ...
```

`Window` validation MUST reject malformed combinations with `ValueError`: `range` requires boot/start/end, `0 <= start < end <= 2^63-1`, and no launch; `since_boot` requires boot and positive end and has no explicit start/launch; `launch` requires boot and positive launch ID and no range endpoints; `lifetime` has no boot, launch, or monotonic bounds. Query enum values outside the declared Literals, nonpositive app IDs, or NUL in search are `ValueError`; a valid but unknown app/launch ID is `KeyError`. An open launch uses one query-start boottime read as its end. Each query reads one consistent database snapshot. All result containers carry populated `Coverage`; individual points/rows inherit it from their container.

For every query, `Coverage.complete` is false if any required source interval is missing, uncertain, clipped by the selected tier's bucket edges, pruned, or marked incomplete; for T it is also false when `inaccessible_pids`, `resets`, or `partial_intervals` is nonzero. For D it is false unless `device_state="available"` throughout and ownership is exact. For F accounting it is false when any bytes are uncounted or the requested window cannot be reconstructed; counted omissions remain complete because their bytes are represented exactly, while the returned path rows are explicitly only retained/top-N rows. `weak_identity_rows` reports identity quality but does not by itself make byte accounting incomplete. A metric unaffected by another metric's unavailable source may remain complete (for example, not-delegated D does not make T incomplete).

`ranking` sorts by requested metric descending, then `app_key` ascending bytewise. `total_bytes = read_bytes + write_bytes`. Every result's app total is the sum of accepted task deltas for the selected tier/window and stream ownership rules. An inaccessible process makes that app total a lower bound; the result carries its count and `complete=False`. `ranking` completeness concerns T; `series` and `disk_share` completeness concern both requested T and D; file-query completeness concerns F. An empty ranking is `[]`, not an invented zero-valued app.

`series` returns no points for `totals_only`; `peak_*_bps` is `None` there. Other points sort by `(bucket_start_mono_ns, bucket_secs)`; device points append `dev` bytewise to that key, and residual points align with task points. `device_points` use raw major:minor tokens. Residuals are computed per bucket from task and device bucket totals and are `None` where D is unavailable. `disk_share` returns device rows in ascending `(boot_id, dev)` UTF-8 order, preserving distinct boot identities. When D is unavailable or mixed, aggregate device bytes and all residuals are `None`; available device rows may still be returned as partial observations with `complete=False`. Its signed total/read/write residuals obey section 2.4 whenever device coverage is complete.

File sort keys are exact: `bytes_total = bytes_r + bytes_w`; numeric keys sort numerically; `last_seen` sorts by `last_seen_mono_ns` within one boot (cross-boot ordering remains A20); `basename` sorts by Unicode codepoint order after Unicode NFC normalization; `dev` sorts bytewise. Every sort appends the deterministic tie key `(dev, inode, generation, basename, path_id)` ascending. `direction` reverses only the selected primary sort, never the tie key. Under proposed A17 search, compare `NFC(search).casefold()` as a substring of `NFC(basename).casefold()` before sorting; `None` and the empty string match all rows. The returned basename retains its original spelling. `FileCoverage.observed_bytes_r/w` is the sum of retained rows before search; it excludes omissions and overflow, which remain separate fields.

`TODO(design) A17`: the design does not define search case/normalization semantics, `direction` defaults, or how unavailable file windows are represented. Recommendation: use NFC-normalized, case-insensitive substring search and an empty row tuple with `coverage.complete=False` plus explicit file-window unavailability for an unrepresentable window.

### 8.3 Tier behavior

`ranking`, `series`, and `disk_share` use the app/device tier table in section 4.2. For exact full-boot/lifetime requests, totals-only ranking uses `app_boot_total`/`app_lifetime_total`; disk share uses that T source plus `app_boot_dev_total` grouped by `(boot_id, dev)`. There is no separate lifetime device table, and identical major:minor strings from different boots MUST NOT merge into one device identity. Totals-only series has no time points and no peak. Results MUST state `tier="totals_only"` and `complete` according to coverage.

`TODO(design) A22`: the tables cannot reconstruct a general bounded window above 365 days, an old launch from app-level rollups/totals, or a concurrent launch's D/F split from app-level rows. The proposed app-query contract raises `WindowUnavailableError` with `reason="unsupported_window"` or `reason="launch_attribution"` instead of returning a larger scope as though exact. For a selected historical tier already pruned, `series` returns its remaining observed points and `unavailable_reason="not_retained"`; missing time is incomplete, not a zero interval. These representability rules require ratification alongside the design contradiction in section 13.

`file_table` and `file_coverage` MUST never fabricate a bounded window from a full-boot total. `file_sample` supports only retained observations. `app_boot_file_total` supports full-boot byte/access counts, but contains no per-boot last-seen timestamp; a global path timestamp MUST NOT masquerade as that boot's last observation. Until `TODO(design) A11/A20/A22` is resolved, an unrepresentable file query returns no guessed rows, zero retained-observation bytes, `coverage.complete=False`, and the applicable `unavailable_reason`; zero retained-observation bytes is not a claim of zero F. `file_source_unavailable` covers an absent/failed F producer; `unsupported_window` covers an unrepresentable bounded range; `launch_attribution` covers a launch split; `not_retained` covers expired raw file history. Available file coverage reads the independent `file_coverage` hook counters, so 24-hour process expiry does not erase 14-day file evidence.

## 9. P1 Error and Edge Contracts

“Stored state” below names only P1 records/counters. User-visible wording is the required semantic outcome; the Qt rendering itself is P5.

| Condition | Exact P1 stored state | Required user-visible consequence |
|---|---|---|
| `/proc` read returns `ESRCH` before identity was open | No sample row and no reset count | No bytes are invented. |
| `/proc` read returns `ESRCH` for an open identity | Close the identity once; increment `resets` once as required by design section 10; if it had a readable baseline and known parent, enqueue its last cumulative counters as reap credit; do not write a negative delta | Existing window totals do not decrease. |
| Own-UID `/proc/<pid>/io` returns `EACCES` | No T delta for that tick; increment per-app `inaccessible_pids` once per process identity in that tick | App total is rendered as a lower bound (`>= X`) with the inaccessible-observation count; a window ending before a later readable sample remains incomplete. |
| cgroup `io.stat` is absent because controller is not delegated | No `sample_dev` zero row; coverage `device_state="not_delegated"`; T is stored normally | Device split says “not delegated”; T is unaffected. |
| Collector source is absent for an interval | Persist `gap` with exact half-open bounds and reason `collector_gap` or `gui_absent`; increment `gap_intervals`; exclude uncovered time | Totals disclose the gap and remain incomplete/lower-bound where applicable. |
| Collector intervals overlap without a handover | Persist `gap` reason `overlap_uncertain` for overlap; increment `overlap_uncertain_intervals`; exclude both contributions in overlap | No double-counted total is presented; coverage marks the uncertain overlap. |
| Child identity disappears and its parent remains | Store component-wise credit; subtract at most each subsequent parent delta; increment `reap_corrections` when any credit is applied | No negative delta and no double attribution of the child's already-emitted totals. |
| Parent disappears with remaining child credit | Add remainder to `reap_unapplied`; attribute it to no app's T | Coverage discloses unapplied reap bytes; totals do not silently absorb them. |
| Clock offset changes by more than 1 second | Add boot-scoped `clock_map` segment; retain all monotonic keys | Historical row order and bucket assignment do not move or repeat because of NTP. |
| Delta spans suspend | Assign it to last pre-suspend observation bucket; set `spans_suspend=1`; exclude suspended time from rate denominator | No suspend trough/spike is inferred; coverage indicates suspend-spanning data. |
| BPF/file-map insert failure (later P4 producer) | Increment `uncounted_overflow_files` per failed insert and add exact bytes to overflow read/write counters; do not create a file row | File coverage states how many bytes are unknown; F remains separate from T. |
| BPF drain/reconnect produces an explicit gap | Preserve `(boot_id, source, epoch, seq)`; insert `gap`; never dedupe on wall time | The affected window is visibly incomplete; no continuity is invented. |
| Database integrity check reports corruption | Preserve the damaged DB under `history.db.corrupt-<ts>` and create a new schema-v1 DB only after quarantine succeeds | Notify that prior history is quarantined and unavailable in the new DB. Exact timestamp/collision rule is `TODO(design) A18`. |
| GUI closes while daemon is active | P1 store does not change ownership; daemon-owned durable rows remain in its store | No history is lost solely because the GUI closes. Daemon mechanics are P3. |
| GUI closes while daemon is off | Persist a `gui_absent` gap when collection resumes; do not synthesize process deltas across the absence | Totals are marked incomplete/lower-bound. |
| Daemon absent | Active collector source is `gui`; P1 stream/gap rules apply | P3/P5 show the absent-daemon banner with `Open settings`; GUI owns collection for its recorded intervals. |
| Daemon enabled but silent | Preserve `enabled, not answering`; P1 MUST NOT manufacture daemon samples | P3/P5 show that exact status rather than pretend data. |
| BPF load failure | Reserve the failure's integer `errno` and verifier-line text; do not fabricate F rows | P4/P5 show errno and verifier line in Files; T and D continue. |
| `pkexec` dismissed/failed | Reserve exact exit code and stderr text; no new collector ownership is claimed | P3/P5 revert the switch and show code/stderr. |
| Delegation installed, activation pending | Reserve `pending_activation`; no successful accounting state is inferred | P3/P5 show the pending banner and re-probe `cgroup.controllers`. |
| Setup helper missing or container host | Reserve the reason text; no setup or collection success is inferred | P3/P5 disable the switch with that reason. |

`TODO(design) A18` also covers the corruption timestamp format and collision handling. Recommendation: UTC epoch nanoseconds plus an atomic no-replace rename; if the destination exists, append a monotonically increasing suffix.

## 10. Deterministic Acceptance Fixtures and Vectors

### 10.1 `/proc` fixture

Boot ID file `/proc/sys/kernel/random/boot_id` contains exactly:

```text
11111111-2222-4333-8444-555555555555\n
```

Notation below is an exact fixture generator. `IO(rc,wc,rb,wb)` expands to the seven lines shown, with LF endings and final LF:

```text
rchar: rc
wchar: wc
syscr: 0
syscw: 0
read_bytes: rb
write_bytes: wb
cancelled_write_bytes: 0
```

`STAT(pid,comm,state,ppid,start)` expands to:

```text
pid (comm) state ppid pid pid 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 start 0
```

Here `start` is field 22. All fixture status files contain `Name: fixture`, `Uid: 1000 1000 1000 1000`, `Gid: 1000 1000 1000 1000`, `TracerPid: 0`, each followed by LF. Each process `cgroup` file contains one `0::<path>\n` line. `exe` is a symlink to the target stated below. Dumpability/read outcomes are harness metadata because proc status does not expose a portable `Dumpable` field.

App process tree for ticks `t0`, `t1`, `t2`: use boottimes `100_000_000_000`, `110_000_000_000`, and `120_000_000_000` ns; wall offset is exactly `1_000_000_000_000` ns, `CLK_TCK=100`, source `gui`, epoch `1`, and sequences `0`, `1`, `2`. Acceptance totals below reduce these three frames inclusively, rather than imply coverage of a larger unobserved interval. The fixture enumerates PID 411 only at t0/t1; its absence at t2 is not an ESRCH mid-scan outcome. All unspecified numeric coverage inputs are zero and supplied text metadata is `name="fixture"`, `icon_path=None`, `scope="user"`.

| PID | `stat` at t0/t1/t2 | `cgroup` path | `exe` target | `io` at t0 | `io` at t1 | `io` at t2 |
|---:|---|---|---|---|---|---|
| 410 | `STAT(410,app,S,1,100)` at all ticks | `/user.slice/user-1000.slice/user@1000.service/app.slice/app-chromium@fixture.service` | `/usr/lib/chromium/chromium` | `IO(0,0,0,0)` | `IO(1000,500,100,70)` | `IO(1600,650,170,90)` |
| 411 | `STAT(411,child,S,410,101)` at t0/t1; absent at t2 | same as 410 | `/usr/lib/chromium/chromium` | `IO(0,0,0,0)` | `IO(500,100,50,10)` | `ENOENT` |
| 412 | `STAT(412,worker,S,1,102)` at all ticks | same as 410 | `/usr/lib/chromium/chromium` | `IO(0,0,0,0)` | `IO(300,200,30,10)` | unchanged from t1 |
| 500 | `STAT(500,reused,S,1,700)` at t0/t1; `STAT(500,reused,S,1,900)` at t2 | `/user.slice/user-1000.slice/user@1000.service/app.slice/app-pidreuse.scope` | `/usr/bin/fixture-old` at t0/t1; `/usr/bin/fixture-new` at t2 | `IO(0,0,0,0)` | `IO(0,0,25,0)` | `IO(0,0,0,0)` |
| 413 | `STAT(413,blocked,S,1,103)` | `/user.slice/user-1000.slice/user@1000.service/app.slice/app-chromium@fixture.service` | `/usr/lib/chromium/chromium` | `EACCES` on io read at all ticks | same | same |

PIDs 410/411/412 resolve to `unit:app-chromium@fixture.service`. PID 500 resolves to the `app-pidreuse.scope` identity. At `t0`, all readable identities establish zero baselines. At `t1`, child 411 is observed. At `t2`, it has been reaped and its cumulative counters are already included in PID 410's cumulative counters. PID 500's new start time proves reuse; its new identity has zero counters. The blocked PID is own UID and non-dumpable in harness metadata.

### 10.2 `/sys/fs/cgroup` fixture

The normal tree contains the following t0/t1 membership; at t2 its Chromium `cgroup.procs` is exactly `410\n412\n413\n`:

```text
/sys/fs/cgroup/cgroup.controllers:
cpu io memory pids\n

/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/app-chromium@fixture.service/cgroup.procs:
410\n411\n412\n413\n
```

At both `t0` and `t1`, the Chromium leaf's `io.stat` is exactly:

```text
259:0 rbytes=1000 wbytes=200 rios=10 wios=3
259:1 rbytes=300 wbytes=500 rios=4 wios=5
```

At `t2`, its `io.stat` is exactly:

```text
259:0 rbytes=1120 wbytes=270 rios=14 wios=7
259:1 rbytes=350 wbytes=550 rios=8 wios=7
```

The sibling PID-reuse leaf has `cgroup.procs` exactly `500\n` and, at all three ticks, `io.stat` exactly `259:0 rbytes=0 wbytes=0 rios=0 wios=0\n`. A separate controller-absent tree has `cgroup.controllers` exactly `cpu memory pids\n` and no `io.stat` in its app leaf. Expected device state for that tree is `not_delegated`, with no zero-valued D rows.

The shared-cgroup case has `cgroup.procs` exactly `420\n421\n`, both PIDs in `app-shared.scope`, and one `io.stat` delta for `259:0` of `rbytes=40`, `wbytes=5`. Both PIDs resolve to the same app. D MUST be stored once as `(259:0,40,5)`, not once per PID. A second shared-cgroup fixture uses cgroup path `/fixture-shared` (no recognized app unit), with PID 420's executable `/usr/bin/tool-a` and PID 421's executable `/usr/bin/tool-b`; these resolve to distinct executable AppKeys. Per-app D ownership is unresolved by the design and has no normative expected split (`TODO(design) A19`).

### 10.3 Aggregator-shim BPF vectors

The shim receives one drain for app cgroup id `42`, device `259:0`, with these exact entries:

| Key `(cgroup_id,dev,inode,generation)` | Value `(reads,writes,bytes_r,bytes_w,basename)` |
|---|---|
| `(42,"259:0",1001,7)` | `(3,1,90,40,"data_1")` |
| `(42,"259:0",1001,8)` | `(1,2,10,20,"data_1")` |
| `(42,"259:0",1002,3)` | `(1,1,0,60,"big.iso")` |

Generation 7 and 8 are distinct path identities even though inode and basename match. `fs_uuid` is supplied by the fixture as `1111-aaaa` for all three entries. Additional coverage inputs for the same app/interval are: one counted omitted file `(20 read bytes,30 write bytes)`, one failed insertion `(5 read bytes,7 write bytes)`, `skipped_nonfile=3`, and `skipped_unsupported_fs=1`.

For a synthetic map capacity of three, insert the three entries above, then submit the single failed entry `(42,"259:0",2000,1)` with value `(1,0,5,7,"lost")` while full. It fails insertion once and contributes overflow bytes `(5,7)`; this is the same failed insertion counted in the coverage inputs above, not an additional failure.

For deterministic top-2 selection, candidates are:

| Candidate identity `(dev,inode,generation)` | bytes_r | bytes_w | reads+writes |
|---|---:|---:|---:|
| `("259:0",200,1)` | 50 | 50 | 5 |
| `("259:1",100,2)` | 70 | 30 | 5 |
| `("259:0",1,1)` | 50 | 49 | 9 |

The top-2 are `("259:0",200,1)` then `("259:1",100,2)`: byte totals tie, access totals tie, then the identity tuple sorts ascending. The third candidate is counted omitted.

### 10.4 Exact acceptance results

| Fixture/query | Expected exact result |
|---|---|
| Chromium per-tick coverage | `inaccessible_pids=1` at each of `t0`, `t1`, and `t2`; `resets=0`; the three-row window sums `inaccessible_pids=3` |
| Chromium T at t1 | `rchar=1800`, `wchar=800`, `read_bytes=180`, `write_bytes=90`; total headline `270` bytes |
| Chromium reap-adjusted T at t2 | PID 410 raw deltas `(rchar=600,wchar=150,read=70,write=20)` less child credit `(500,100,50,10)` gives `(100,50,20,10)`; PID 411 emits no second delta; PID 412 emits zero |
| Chromium cumulative T after t2 | `rchar=1900`, `wchar=850`, `read_bytes=200`, `write_bytes=100`, headline total `300`; `first_seen_partial=0`, `resets=0`, `reap_corrections=1`, `reap_unapplied=(0,0,0,0)` |
| PID reuse app | Old identity contributes exactly `read_bytes=25`; new `(pid=500,start_ticks=900)` contributes zero; cumulative app read `25`; no negative delta and `resets=0` |
| Chromium D over t0..t2 | `259:0=(r=120,w=70)`, `259:1=(r=50,w=50)`; aggregate D `(r=170,w=120,total=290)` |
| Chromium reconciliation | T `(r=200,w=100,total=300)`; `R_read=30`, `R_write=-20`, `R=10`; `170+120+10=300`, `170+30=200`, `120-20=100` |
| Shared-cgroup D | Exactly one row `(dev="259:0",rbytes=40,wbytes=5)` for both PIDs |
| F retained rows | `(bytes_r=100,bytes_w=120,reads=5,writes=4,total=220)` across three identities |
| F coverage | counted omitted `(files=1,bytes_r=20,bytes_w=30)`; overflow `(attempts=1,bytes_r=5,bytes_w=7)`; skipped `(nonfile=3,unsupported_fs=1)` |
| F equality | `F_read=100+20+5=125`; `F_write=120+30+7=157`; `F=282`; retained rows `220` + counted `50` + uncounted `12` = `282` |
| BPF top-2 | first `("259:0",200,1)`, second `("259:1",100,2)`; omitted candidate total `99`, accesses `9` |
| Controller absent | no device rows; `device_state="not_delegated"`; T values unchanged |
| First-seen nonzero identity | With no baseline and input `(rchar=70,wchar=30,read_bytes=7,write_bytes=3)`, proposed output is that same delta, headline `10`, `first_seen_partial=1`, `partial_intervals=1`, and `delta_kind="baseline"`; provisional under `TODO(design) A01` |
| Per-field reset | Prior `(100,20,10,5)`, current `(90,25,8,9)` emits `(0,5,0,4)` with `resets=1`; next `(95,30,9,10)` emits `(5,5,1,1)` with no new reset; provisional under A02 |
| ESRCH mid-scan | One enumerated, already-open identity whose read vanishes increments `resets` exactly once, closes once, and emits no bytes; an unknown identity gives zero resets |
| Ordinary interval peak | With exact 10-second observed intervals, Chromium t1 rates are `18` read B/s and `9` write B/s, t2 rates `2` and `1`; proposed interval peaks are `(18,9)` under A16 |
| Duplicate frame | Re-delivering `(boot_id,gui,1,2)` after t2 leaves every row, baseline, rollup, total, and coverage counter unchanged |

### 10.5 Clock/suspend acceptance sequence

Use `bucket_ns=60_000_000_000`. Observations are in one boot:

| `ts_mono_ns` | Wall seconds | Offset ns | Event |
|---:|---:|---:|---|
| 119,000,000,000 | 1119 | 1,000,000,000,000 | ordinary sample; bucket starts 60,000,000,000 |
| 121,000,000,000 | 1121 | 1,000,000,000,000 | ordinary sample; bucket starts 120,000,000,000 |
| 125,000,000,000 | 625 | 500,000,000,000 | NTP step; new clock_map segment at 125,000,000,000 |
| 179,000,000,000 | 679 | 500,000,000,000 | last sample before suspend; bucket starts 120,000,000,000 |
| 661,000,000,000 | 1161 | 500,000,000,000 | first sample after suspend; known suspend boundary `[180s,660s]`; spanning delta assigned to pre-suspend bucket and flagged |

The clock fixture starts with an initial observation at boottime zero and wall seconds `1000`, with zero counters, before the observations shown. Expected clock map rows are `(segment_start=0,offset=1_000_000_000_000)` and `(segment_start=125_000_000_000,offset=500_000_000_000)`. The bucket sequence is `[60s,120s)`, `[120s,180s)`, `[660s,720s)` for ordinary observations, while the spanning delta is stored in the last pre-suspend observed bucket `[120s,180s)` with `spans_suspend=1`. No row is assigned by wall time. This sequence fixes bucket partition and labeling only; rate denominator details remain `TODO(design) A09`.

## 11. Property-Based Tests

All property tests use deterministic seeds recorded with failures. Generate 0–128 observations, counters/counts in `[0, 2^63-1]`, canonical AppKeys under the chosen A07 policy, devices `f"{major}:{minor}"` with each component in `[0, 2^20-1]`, inode values in `[0, 2^63-1]`, generations in `[0, 2^32-1]`, and nonnegative boottimes no greater than `2^63-1`. Coverage generators use the exact section 6.1 shape. Mathematical intermediate sums may use signed 128-bit test arithmetic; sums outside the storage range MUST exercise the overflow path, not a successful insert. The production oracle uses arbitrary-precision integers. Shrink by deleting unrelated apps/devices/files first, then timestamps, then reducing to the minimum two samples/two identities needed to preserve the failing condition.

1. **Device reconciliation:** For every generated app/window, `sum(dev.total_bytes) + residual_total == task_total_bytes`; independently, `sum(dev.rbytes) + residual_read == task_read_bytes` and `sum(dev.wbytes) + residual_write == task_write_bytes`. Include positive, zero, and negative residuals.
2. **File byte equality:** For read and write independently, `sum(file rows) + counted omissions + uncounted overflow == F`. Total equality is the sum of those independent equations. Skipped objects do not enter either side.
3. **Coverage equality:** For any selected window/tier, result coverage equals the exact, deduplicated aggregation of persisted source coverage rows; it is never recomputed from absence of rows. Counted omission bytes and overflow bytes are independently preserved across rollup and total aggregation.
4. **Monotonic bucket partition:** Generate nonnegative integer `ts_mono_ns` values and positive `bucket_ns`; for every timestamp, exactly one bucket index is `floor(ts_mono_ns / bucket_ns)`, and the mathematical half-open intervals for consecutive indices touch without gaps or overlap. Observed samples may skip bucket indices, which does not imply coverage. Generate positive and negative NTP offset steps and suspend intervals crossing zero, one, or multiple bucket boundaries. Clock steps MUST NOT change bucket keys. Suspend-spanning deltas are placed in the last pre-suspend observed bucket and marked once; no byte delta is duplicated or omitted by partitioning.
5. **Counter reset:** A decreasing counter emits no negative delta, resets the selected baseline, and the next nondecreasing value is measured from the reset baseline under the recommended per-counter rule.
6. **PID reuse:** Replacing `(boot,pid,start_ticks)` with a new start time never transfers the old baseline or reap credit.
7. **Credit conservation:** For each counter, emitted parent delta plus the applied credit equals the raw parent delta; leftover credit equals the stored reap-unapplied amount if the parent closes. No adjusted delta is negative.
8. **Top-N determinism:** Permuting input BPF entries does not change selected rows, omitted counts, or omitted byte totals. Equal byte/access ranks resolve by ascending `(dev,inode,generation)`.
9. **Tier boundary:** Duration exactly 24h uses raw; one nanosecond over uses 60s; exactly 30d uses 60s; one nanosecond over uses 3600s; exactly 365d uses 3600s; one nanosecond over uses totals-only. No query reads two tiers for the same quantity.
10. **Stream dedupe/ownership:** Duplicate or stale `(boot_id,source,epoch,seq)` frames are rejected before baseline changes and have no effect on any rows/totals; a new frame is applied once. Disjoint half-open collector intervals have no overlap; uncertain overlap contributes zero bytes from both owners in the overlap and increments coverage once per interval. Generate integer interval endpoints with `start < end`, collector ownership labels, and duplicate/stale sequence keys; shrink to the smallest two overlapping intervals or duplicate frames that preserve the failure.
11. **ESRCH accounting:** Generate an open or unknown `(boot,pid,start_ticks)` identity and one `ESRCH` scan outcome. An already-open identity increments `resets` by exactly one and is closed once; an unknown identity increments it by zero. Neither case emits a negative delta or adds a reap credit without a readable baseline. Shrink by removing other identities and reducing the scan to this single outcome.
12. **Retention independence and overflow:** Generate ages on both sides of 24 hours and 14 days, file/core units with the same stream key, and nonzero hook counters. Core expiry MUST leave eligible file rows and hook counters byte-for-byte equal until file expiry. Separately generate a counter or sum in `[2^63, 2^64-1]`; it MUST NOT commit as a wrapped integer, REAL, or saturated counter. Shrink age toward each boundary and overflow toward `2^63`; persistence of the overflow diagnostic itself remains A21.

## 12. Ambiguous in the Design

Each item is unresolved by the approved design text. Recommendations below are not binding until ratified. Any unresolved item blocks an exactness claim for behavior that depends on it; unrelated behavior may proceed.

1. `TODO(design) A01` First observation: emit current cumulative counters as a partial delta, establish a zero baseline without emitting, or use a collector-start baseline. Recommendation: emit current values once and set `first_seen_partial=1`, because those counters are known observed values and the missing history is explicitly disclosed.
2. `TODO(design) A02` Counter reset and `ESRCH` accounting: whether decreases reset one field or all four baselines, whether to count one reset per field or identity record, and how the design's explicit rule to count an open-identity `ESRCH` in `resets` composes with counter decreases. Recommendation: reset each decreasing field independently, increment once for an identity record with any decrease, and increment once for an `ESRCH` outcome only when that exact identity was already open.
3. `TODO(design) A03` Never-sampled child reap: no event source/threshold distinguishes it from parent I/O. Recommendation: do not infer `reap_unobserved` from magnitude alone; keep zero until a detector is specified.
4. `TODO(design) A04` Parent identity, nested reaps, and credit ordering across reparenting/multiple reaps. Recommendation: retain last observed parent identity and queue by disappearance sequence, then child `(pid,start_ticks)`; count a correction once on first positive credit use. Nested reaps and resets of cumulative counters require an explicit conservation rule before claiming exactness.
5. `TODO(design) A05` D counter decreases, cgroup recreation, and cgroup migration baseline treatment. Recommendation: key by boot+cgroup id+device, clamp decrease to zero, establish a fresh baseline.
6. `TODO(design) A06` Launch opening/closing and grouping concurrent process trees. Recommendation: one app-root process identity plus same-AppKey descendants, closed when all known members disappear.
7. `TODO(design) A07` Exact AppKey escaping/canonicalization for systemd units, desktop IDs, executable symlinks, and cgroup paths. Recommendation: preserve systemd unit spelling, use desktop file ID verbatim, and use the absolute `/proc/<pid>/exe` target without resolving a second symlink layer; encode reserved characters as UTF-8 percent escapes.
8. `TODO(design) A08` Epoch allocation, sequence persistence/high-water replay rejection, handover baseline JSON shape, and crash interval closure. Recommendation: transactionally allocate an epoch and reset `last_seq` to NULL on collector start; serialize each frame behind the committed high-water guard in section 3.4. Define/version the baseline object before store interchange; P1 reserves it without inventing a daemon protocol.
9. `TODO(design) A09` Suspend detection, persisted attribution timestamp versus actual observation timestamp, and exact observed-time rate denominator. Recommendation: explicit suspend/resume boundaries and persisted observed durations; subtract measured suspended duration only. The abbreviated schema has no dedicated duration field, so complete reconstruction of suspend rates requires ratification.
10. `TODO(design) A10` Partial buckets at query boundaries. Recommendation: include full contained buckets only and mark the clipped interval incomplete rather than prorating bytes.
11. `TODO(design) A11` File observation cadence/top-100 timing, separate 14-day file retention, and bounded file queries beyond retention. Recommendation: define one observation per app-hour after aggregation; exact full-boot totals only beyond observations, and mark other ranges unavailable.
12. `TODO(design) A12` Coverage count units, operators, raw persistence, and stable JSON schema. Recommendation: use section 4.3 definitions and the added `sample_coverage` table.
13. `TODO(design) A13` Quota unit, file-size accounting, pruning tie-break, physical shrink policy, and quota-exhaustion resume. Recommendation: decimal 1,500,000,000 bytes across DB/WAL/SHM; use the algorithm in section 5.3.
14. `TODO(design) A14` Exact persisted `coverage_json` keys and how file coverage is carried in rollups/totals. Recommendation: section 6.1 schema, with component-wise addition for byte/count counters and logical OR for flags.
15. `TODO(design) A15` How measured row payload numbers apply to the expanded schema and indexes. Recommendation: remeasure the exact schema before shipping; hard quota remains authoritative.
16. `TODO(design) A16` Peak numeric representation and temporal grain. Recommendation: integer floor bytes/second for each defined app interval, then maxima over the selected bucket/window as in section 5.4; preserve exact byte counts.
17. `TODO(design) A17` File search normalization/case behavior, sort defaults, and unavailable result encoding. Recommendation: NFC case-insensitive substring, explicit direction, empty/incomplete for unrepresentable ranges.
18. `TODO(design) A18` Corrupt-database timestamp format/collision policy and recovery if quarantine fails. Recommendation: atomic UTC-nanosecond no-replace rename; never overwrite a prior quarantine.
19. `TODO(design) A19` D ownership where different AppKeys share one cgroup. Recommendation: never duplicate D; report it under a deterministic cgroup owner only if the ownership rule is approved, otherwise mark affected device attribution incomplete.
20. `TODO(design) A20` `path.fs_uuid`, device identity, weak identity, rename/hardlink alias updates, and path timestamps across boots. Recommendation: pair every monotonic endpoint with a boot ID as in this DDL, choose first/last boots using persisted boot chronology rather than comparing monotonic values across boots, and keep weak identities separate from strong identities. The chronology source and canonical alias choice still require ratification.
21. `TODO(design) A21` Behavior when a Linux counter or aggregate exceeds SQLite's signed 64-bit maximum. Recommendation: reject that transaction, persist an overflow/incomplete coverage state in a separate representable field, and never wrap or saturate. That diagnostic field is not yet defined by the source design.
22. `TODO(design) A22` Scope representability: general bounded windows above 365 days, launch totals after raw expiry, and launch-specific D/F splits cannot be reconstructed from app/boot totals. Recommendation: use the explicit unavailable/error contracts in section 8.3 until the design chooses additional per-launch/range storage or narrows the promised queries; never substitute a full-boot/lifetime/app total for a narrower scope.

## 13. Design Contradictions

1. **Revision metadata:** the design header identifies revision 2 and says it awaits re-review, while section 13 says its decisions are revision-3 resolutions and section 14 records the agreed decomposition. The user-provided task identifies revision 3 as approved. The behavioral body is usable, but the artifact's revision/status metadata is contradictory.
2. **File storage arithmetic:** the stated `file_sample` formula gives `107 * 24 * 100 * 88 = 22,598,400` bytes/day and `316,377,600` bytes for 14 days, not the table's `23 MB/day * 14 = 322 MB`. This specification preserves the formula and states both values rather than silently changing the approved estimate.
3. **Overflow attribution:** the BPF section describes a separate global overflow counter map, while `file_coverage` and the N4 resolution require overflow bytes per app. A global counter cannot produce exact per-app bytes without an app/cgroup-keyed mechanism. The P1 schema keeps per-app fields but no correct producer can populate them until design chooses a mechanism.
4. **Boot scoping of monotonic values:** `clock_map` uses only `segment_start_mono_ns` as its primary key although monotonic time restarts each boot and the rest of the schema stores multiple boots. `path`/`path_alias` and `app_lifetime_total.updated_mono_ns` likewise retain monotonic timestamps without a boot identity. Such values can collide or be ordered incorrectly across boots. This draft adds boot scope; path-lifetime chronological ordering remains unresolved in TODO(design) A20.
5. **Raw coverage availability:** the design promises window coverage and coverage in results, and carries coverage JSON in rollups/totals, but the raw `sample`/`sample_dev` schema has no raw storage for most process counters (`inaccessible_pids`, resets, reap credits, gaps). Exact raw-window coverage cannot be reconstructed from the listed columns. This draft adds `sample_coverage`; the exact shape remains TODO(design) A12/A14.
6. **Promised windows versus stored dimensions:** the query windows include launches and general ranges, and the tier rule sends ranges above 365 days to totals. App rollups/totals lack launch IDs, app-level D/F rows lack launch attribution, and full-boot/lifetime totals lack arbitrary range endpoints. `app_boot_file_total` also lacks per-boot last-seen data required by the file-row result. These dimensions cannot be recovered exactly from the listed rows. A11/A22 record the choices; this draft does not add guessed splits or silently broaden a request.

## 14. P2 Can Start Immediately

A P2 implementer can build the pure `/proc` parser and identity validator, `CLOCK_BOOTTIME` bucket arithmetic, signed T/D reconciliation, SQLite schema creation and app/time query indexes, stream-key deduplication, the controller-absent path, and the fixture/property tests whose expected values are fixed in section 10. P2 should keep launch grouping, first-seen policy, reset counting, shared-cgroup D ownership, partial rollup edges, and file-window behavior behind explicit seams until their `TODO(design)` decisions are ratified; those unresolved details must not block a narrow since-boot vertical slice that uses one app, one collector stream, complete permissions, and aligned windows.
