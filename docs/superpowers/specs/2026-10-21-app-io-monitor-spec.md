# appio: P1 Contract Specification (revision 3)

Date: 2026-10-21  
Status: revision 3 — review blockers B1–B10 resolved against design revision 4 and the operator's unchanged A11/A22 rulings.
Scope: measurement, aggregation, identity, SQLite storage, query semantics, and P1 error/degradation contracts. This document does not specify the daemon, socket protocol, BPF implementation, Qt UI, tray, packaging, or polkit, except for values those components must preserve.

## 1. Conformance and Scope

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. All 22 appendix decisions A01–A22 are **ratified**. Their tags identify the decision's origin; they do not make its requirements provisional. A11 and A22 follow the operator's explicit rulings; the other 20 adopt revision 1's recommendations, including their limits on exactness.

P1 MUST provide interchangeable sampler/store components. P1 MUST NOT implement the daemon, socket protocol, BPF program, UI, tray, package, or privilege setup. P1 MUST reserve the stream fields `source`, `boot_id`, `epoch`, and `seq`, and file-coverage fields needed by later plans.

The authoritative source is `2026-10-21-app-io-monitor-design.md`, revision 4, whose header now includes revision history. The operator's explicit A11/A22 rulings take precedence over conflicting source wording; section 13 records the remaining A22 wording conflict without changing the design.

The concrete encodings, storage columns, algorithms, and acceptance fixtures below are binding P1 contracts. Given the same database state, ordered input observations, supplied metadata, and ratified policies, implementations MUST produce equal row values; this does not require identical SQLite file bytes or page layout.

Revision history: r1 specified P1 and recorded 22 decisions plus six source contradictions; r2 ratified those decisions, adopted r4's corrected arithmetic, overflow attribution, boot scope and stored raw coverage, and disabled unrepresentable windows; r3 fixes the ten reproduced review blockers with durable sampler checkpoints, per-attribution rows, exact JSON schemas, deterministic identities/orderings, diagnostic/query overlays, and persisted peak validity/combined PEAK.

## 2. Measurement Model

### 2.1 T: task-accounted disk I/O

`T` is the sum of deltas of `/proc/<pid>/io` fields `read_bytes` and `write_bytes`, after the child-reap correction in section 5. It is per process identity, then attributed to an `AppKey` and launch. `rchar` and `wchar` are also stored as separate kernel counters but MUST NOT be added to `T` and MUST NOT be labelled disk I/O.

`T` is not SSD wear. It includes task accounting such as writes later cancelled or not flushed. A collector MUST NOT infer that each byte in `T` reached a physical device.

### 2.2 D: cgroup device bytes

`D` is the nonnegative delta of `rbytes` and `wbytes` in cgroup-v2 `io.stat`, grouped by app and device. It is block-layer accounting, not process accounting. It can include writeback charged to kernel workers and can exclude I/O after a task migrates out of a cgroup. A device row MUST never be replicated once per process in the cgroup.

The canonical stored device token is the decimal `major:minor` token from `io.stat` (for example `259:0`). Friendly device names are display metadata and MUST NOT be used as primary identity. `D` remains boot-scoped.

### 2.3 F: logical file activity

`F` is logical VFS read/write activity. For a file, store access counts and returned byte counts independently for reads and writes. Page-cache activity counts in `F`. `F` MUST NOT be added to or subtracted from `T` or `D`, and MUST NOT be labelled physical disk traffic.

Only explicitly counted omissions and overflow bytes may complete the file-activity accounting equation. `skipped_nonfile` and `skipped_unsupported_fs` describe excluded objects and MUST NOT be added to `F`. Per-app overflow MUST originate in a per-cgroup-keyed producer counter and an unambiguous cgroup-to-AppKey mapping. Loss under the reserved `overflow_owner_unknown` key is a separate, first-class unknown-owner quantity; it MUST NOT be distributed among apps or hidden in an invented app/path row.

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

For a whole-window file total, `F_all_read = sum(app F_read) + overflow_owner_unknown.bytes_r` and `F_all_write = sum(app F_write) + overflow_owner_unknown.bytes_w`. Each term MUST cover the same window and be counted once. The unknown-owner remainder does not belong to any app's equation. A consumer MUST display it separately even when a selected app has zero attributed overflow.

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

`AppKey` is a nonempty UTF-8 string without NUL. Under the ratified A07 encoding, each payload is nonempty and preserves the source UTF-8 bytes: ASCII letters, digits, and `/._:@+-` pass through; every other byte is `%HH` with uppercase hexadecimal. Literal `%` is therefore `%25`; safe bytes MUST NOT be unnecessarily escaped. A decoded executable/cgroup payload MUST start with `/`; a decoded unit/desktop payload MUST NOT contain `/`. The all-inputs-unavailable fallback MUST be `scope:/session/unknown`. No case folding or Unicode normalization is applied to identity. `app_id` is a positive, database-local surrogate and MUST NOT be sent across stores or treated as identity. Stores correlate applications only by exact `AppKey` equality plus boot identity where required.

`resolve_app(cgroup_path, exe, desktop_id)` MUST use this precedence:

1. Deepest matching `app-*` systemd unit in the cgroup path.
2. Nearest ancestor `app.slice` unit.
3. Desktop ID, if supplied and valid.
4. Executable path, if absolute and available.
5. Session-scope identity.

Within the same precedence class, the candidate is the deepest path component; equal-depth candidates are ordered by bytewise UTF-8 unit name. The fallback must always return an `AppKey`; unavailable cgroup, desktop, and executable inputs resolve to a session-scope key.

For precedence step 5, the session-scope payload MUST be derived only from the supplied `cgroup_path`: among its slash-delimited components, select the deepest component matching `session-[^/]+\.scope` and use the complete absolute path prefix through that component as the payload. Do not use the process leaf below that ancestor, an unrelated `.scope`, or a separately inferred session identifier. If the path is absent, non-absolute, or contains no matching ancestor, return `scope:/session/unknown`. Thus `/user.slice/session-7.scope/worker` resolves to `scope:/user.slice/session-7.scope`; `/user.slice/user-1000.slice/user@1000.service/app.slice/worker`, null, and relative `session-7.scope/worker` each resolve to `scope:/session/unknown` when desktop and executable inputs are also unavailable.

**A07 — ratified.** Resolution MUST preserve systemd unit spelling and use a supplied desktop file ID verbatim. The executable fallback MUST use the absolute `/proc/<pid>/exe` target without resolving another symlink layer. The percent encoding, precedence, fallback, and tie ordering above MUST be used; identity MUST NOT depend on locale, case folding, or display-name normalization.

### 3.3 Launch identity

A launch row is scoped by `(app_id, boot_id, launch_id)`. `launch_id` is a positive local surrogate. Raw task samples MUST include a non-null `launch_id`; `0` is reserved for “launch not resolved” and MUST NOT appear in `launch`. Device/file rows are app-level and need not be split by launch.

**A06 — ratified.** A collector MUST open one launch per app-root process identity and group its known descendants only while they resolve to the same AppKey. An app root is a validated identity with no known same-AppKey parent; concurrent roots create distinct launches. A launch MUST close only after all known members are confirmed absent, not when its root alone exits. The stored `(boot_id, root_pid, root_start_ticks)` identifies the root across collector restarts; an unresolved membership MUST use `launch_id=0` rather than guess. Launch records support aggregation bookkeeping; A22 disables every public launch-window query.

### 3.4 Collector stream identity

A stored observation is namespaced by `(boot_id, source, epoch, seq)`. Reserved P1 source values are `gui` and `iod`; a future source value MUST be a new string, not an alias. `epoch` is a positive integer allocated per source and boot; `seq` is a nonnegative integer strictly increasing within `(boot_id, source, epoch)`. `collector_epoch.last_seq` is the committed high-water mark for the active epoch. The writer MUST serialize frames in sequence order; before any baseline or database mutation it MUST reject a frame whose source/boot/epoch is not active or whose `seq <= last_seq`, as duplicate/stale. The high-water update and all rows/totals for the frame commit atomically. Rows are deduplicated only by their full declared primary key; stream replay is rejected by the high-water mark, never by wall time.

Each collector-owned interval is half-open `[from_mono_ns, to_mono_ns)`. Handover uses a single `cutoff_mono_ns`; the outgoing interval ends there and the incoming interval starts there. Baselines captured at the cutoff MUST prevent the incoming collector from re-emitting pre-cutoff counters. An uncovered interval MUST be represented by a `gap` row. If a handover record is missing and two ownership intervals overlap, both byte contributions in the overlap MUST be excluded and the overlap MUST be marked uncertain.

**A08 — ratified.** Every collector process start MUST transactionally allocate a new epoch (`previous + 1` in the same boot, otherwise `1`) and set `last_seq=NULL`; sequence numbering starts at zero. A frame MUST commit its sequence receipt, rows, totals, and `sampler_checkpoint` atomically before publication. On an ordinary same-boot restart, allocate the new epoch but restore the last committed checkpoint's open identities, readable baselines, app/parent/root metadata, device baselines, and pending credits before accepting a frame; the checkpoint may have the preceding epoch and is not a replay receipt for the new epoch. Preserve cumulative counters across that restart, emit only the difference from the restored baseline, and mark the interval after the last durable observation as a gap. If the boot differs, reject the old checkpoint for restoration and start fresh. A crashed interval MUST be closed at the first durable evidence of the next collector's start, with time after its last durable observation recorded as a gap rather than claimed coverage. Handover baseline JSON MUST have exactly `version`, `boot_id`, `cutoff_mono_ns`, `processes`, and `devices`; `version` is integer `1`; the arrays may be empty only when their corresponding state is empty. Process entries MUST have exactly `pid`, `start_ticks`, `ppid`, `parent`, `app_key`, `launch_root`, `launch_id`, `baseline`, and `credits`; their nested schemas, validation, ordering, and canonical encoding are specified in §5.4. Device entries MUST have exactly `(cgroup_id,dev,rbytes,wbytes)` with nonnegative integer identifiers/counters and nonempty device text. Null explicitly denotes an unknown parent/root or absent baseline. An incompatible or incomplete object MUST be rejected as a handover, recording the resulting gap/uncertainty. This is a store contract, not a socket protocol.

### 3.5 Path identity and boot chronology

**A20 — ratified.** Every monotonic endpoint MUST retain its boot identity: `path`/`path_alias` use first/last boot-ID pairs, lifetime totals use `updated_boot_id`, and clock segments use `(boot_id,segment_start_mono_ns)`. Cross-boot first/last ordering MUST use persisted `boot.boot_order`, then the within-boot monotonic value; implementations MUST NOT compare boottimes from different boots. A new locally observed boot gets the next positive boot order. Imports MUST supply consistent relative boot chronology and remap local orders transactionally; conflicting or missing chronology MUST be rejected rather than guessed from current wall time. Boot order is local ordering metadata, not external identity.

A strong file identity MUST use the supplied filesystem UUID, device, inode, and generation; basename is deliberately excluded from this hourly aggregation identity. The hourly aggregation key is `(app_key,fs_uuid,dev,inode,generation,identity_weak)`. The distinct path-row uniqueness key is `(fs_uuid,dev,inode,generation,basename,identity_weak)`, so each observed basename has its own reusable `path` row. ext4 supports strong identity; btrfs/overlayfs and an unverified filesystem MUST be weak, and missing UUID MUST use the empty string with `identity_weak=1`. Weak and strong rows MUST remain separate. tmpfs is excluded and counted under the supported-filesystem policy; excluded objects MUST NOT enter F. Every new captured basename MUST create/reuse its own path row and record its first/last alias observations without rewriting earlier captured names. Within an app-hour aggregation identity, select the bytewise least observed basename as the one canonical `file_sample.path_id`; preserve every other observed basename as an alias of that canonical path row. Counters/accesses merge across those names and across cgroups, and the row's LAST is the greatest contributing observation time. This canonicalization selects among observed basenames; it MUST NOT reconstruct a full path or hide truncated/unresolved states. Alias first/last pairs MUST use persisted boot chronology. LAST for a file query MUST come from its selected observations/totals, not a global alias timestamp.

## 4. Time, Buckets, Windows, and Coverage

### 4.1 Clocks and bucket arithmetic

All ordering and bucket keys use `CLOCK_BOOTTIME` nanoseconds. For bucket width `bucket_ns > 0` and an assigned observation timestamp `ts_mono_ns`:

```text
bucket_start_mono_ns = (ts_mono_ns // bucket_ns) * bucket_ns
bucket interval = [bucket_start_mono_ns, bucket_start_mono_ns + bucket_ns)
```

Bucket boundaries MUST be computed with integer division and MUST NOT use wall time. Wall labels use a persisted mapping `wall_ns = mono_ns + wall_offset_ns`; each offset is sampled from realtime and boottime at the same observation. A new `clock_map` segment starts at the current boottime timestamp when the absolute change from the previous offset is strictly greater than `1_000_000_000` ns. Historical labels use the segment with the greatest start timestamp not greater than the sample timestamp. An NTP step MUST NOT alter a bucket key or row order.

A new boot's first accepted observation MUST create its initial clock segment at `segment_start_mono_ns=0`, using that observation's offset; this extends the initial label mapping back to boot zero without asserting collection coverage there. Subsequent segments follow the threshold rule above. Raw durable observations have a nominal 10-second cadence; the 2-second live cache is not a second durable stream. The raw series bucket width MUST be `10_000_000_000` ns. Each raw sample MUST persist the actual `observed_mono_ns`, the attributed `ts_mono_ns`, and the nullable `interval_start_mono_ns`/`observed_ns` rate evidence. All these timestamps belong to that row's `boot_id`.

A delta known to span suspend is assigned to the bucket of the last observation before suspend, and its sample row has `spans_suspend=1`. The rate denominator excludes the suspended interval. `spans_suspend` is a boolean integer, `0` or `1`; aggregation across rows uses logical OR, not addition.

**A09 — ratified.** The collector MUST consume explicit suspend/resume boundaries and MUST NOT infer suspend from a long sampling delay. It MUST subtract the union of measured suspended intervals from the observed interval, intersecting boundaries with that interval and counting overlaps only once. Known uncovered time is likewise excluded once. `observed_ns` MUST persist this denominator; unknown boundaries or duration MUST produce NULL, not a guessed rate. A first baseline has NULL interval/rate evidence. If contributors to an app/launch row do not have a common measured interval, its rate MUST be unavailable; their bytes still accumulate. A suspend-spanning sample MUST retain its actual observation timestamp while `ts_mono_ns` identifies the last pre-suspend observation for bucket assignment.

### 4.2 Windows and tier selection

Every supported query window is half-open. A bounded `range` is `[start_mono_ns, end_mono_ns)` with `start < end` and one `boot_id`. `since_boot` is from boot zero to the requested end. `lifetime` spans all boots and is answered from lifetime totals only. `launch` remains a syntactically valid reserved Window kind but MUST be rejected as disabled, even when raw launch rows survive (A22 — ratified).

For a bounded window of duration `d = end - start`, tier selection is disjoint and MUST use these inclusive upper bounds:

| Condition | App/device source | Stated resolution |
|---|---|---|
| `d <= 24 hours` | raw `sample` and `sample_dev` | raw observations |
| `24 hours < d <= 30 days` | 60-second `rollup_app` and `rollup_dev` | 60 s |
| `30 days < d <= 365 days` | 3600-second rollups | 3600 s |
| bounded `range` with `d > 365 days` | none | disabled; `WindowUnavailableError(reason="unsupported_window")` |
| exact full `since_boot` with `d > 365 days`, or `lifetime` | totals tables | totals only; no time series or peak |
| `launch`, at any duration/age | none | disabled; `WindowUnavailableError(reason="launch_attribution")` |

The source tier MUST NOT be blended with an adjacent tier for the same quantity. Disabled-window checks MUST precede tier selection. Results MUST disclose the selected tier and coverage. Selection by duration does not guarantee retention: a short old window whose selected observations expired MUST remain unavailable/incomplete rather than switch tiers. **A10 — ratified:** only complete buckets wholly contained in the request MUST contribute; omitted boundary time MUST make coverage incomplete, and bytes MUST NOT be prorated. An exact full-boot total means a `since_boot` request ending at that boot's durable collection cutoff, not an arbitrary historical prefix. A full-boot/lifetime total MUST NOT substitute for a different requested range.

**A11 — ratified, operator ruling.** File observations MUST be HOURLY: one finalized snapshot per `(boot_id,AppKey,hour)` after aggregation across all authoritative contributions, with at most the top 100 files per app-hour. File observations and their hourly coverage MUST have independent 14-day retention. File queries MUST use retained hourly observations, or exact full-boot/lifetime retained-observation totals; other unrepresentable allowed windows MUST return explicit unavailable/incomplete file results. Section 8.3's disabled-window error takes precedence for all launch and over-365-day range queries.

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
| `uncounted_overflow_bytes_r/w` | nonnegative integer bytes | exact bytes from the app's attributable per-cgroup overflow counters |
| `overflow_owner_unknown` | separate owner key with attempt/read-byte/write-byte counters | unassignable overflow, stored and queried independently; never allocated to an app |
| `reap_incomplete` | integer boolean | nested/rebased reap credits lack proven conservation; no exactness claim |
| `counter_overflow` | integer boolean | a rejected out-of-range transaction affects the window; durable diagnostic, never a wrapped byte count |
| `skipped_nonfile` | integer count | excluded non-file objects |
| `skipped_unsupported_fs` | integer count | excluded filesystem objects |
| `weak_identity_rows` | integer count | observed file identities whose `path.identity_weak=1`, including retained and omitted rows |
| `hook_hits` | object of six nonnegative integer counters | hits for `vfs_read`, `vfs_write`, `vfs_readv`, `vfs_writev`, `vfs_iter_read`, `vfs_iter_write`; zero means no observed hit, not proof of inactivity |
| `identity_weak` | integer boolean on `path` | `1` means the path key is weak and MUST NOT merge with a strong identity |

`identity_weak`, `first_seen_partial`, `spans_suspend`, `reap_incomplete`, and `counter_overflow` use `{0,1}` when persisted. After stream deduplication and interval selection, implementations MUST sum integer counts/bytes and each reap component, sum all six hook counters, and OR boolean flags. `device_state` is `incomplete` if any selected interval is incomplete or if `available` and `not_delegated` are mixed, `not_delegated` if all are not delegated, and `available` if all are available; no source rows means `incomplete`. Hook counters and `weak_identity_rows` MUST survive independently in hourly `file_coverage` and file totals. A result's `Coverage` MUST report its source tier and window completeness. Unknown-owner overflow MUST have its own window result and MUST NOT be summed once per app.

**A12 — ratified.** Section 4.3 defines the required units and operators. `sample_coverage` MUST persist one per-app receipt per accepted process tick, including zero-T ticks. Its JSON is a superset of r4's abbreviated raw-coverage columns and preserves all four components of reap quantities. Raw coverage MUST be read from these rows, not inferred from missing byte rows or borrowed from full-boot coverage.

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
    if same-boot sampler_checkpoint exists on process start:
        restore its identities, baselines, credits and device baselines before this tick
        record a gap after its checkpoint_mono_ns; do not reset cumulative baselines
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
                emit current cumulative counters once as the initial partial delta (A01 — ratified)
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
        retain the adjusted delta's attributed timestamp and aggregate by
            (AppKey, launch_id, attributed_ts_mono_ns)
        attribute reap_corrections to the parent's AppKey; count each child identity once
    if a parent closes with remaining credit:
        add that remainder to reap_unapplied; attribute it to no app total

    aggregate cgroup io.stat deltas per (app, dev), never per process
    accumulate file candidates and coverage independently in their boottime app-hour
    finalize a file snapshot only when its app-hour closes; apply top-100 after aggregation
    route keyed overflow to its unambiguous app; keep overflow_owner_unknown separate
    materialize one sample row per (app, launch, attributed_ts_mono_ns) with at least
        one readable identity; distinct attributed timestamps remain distinct rows:
        sum each adjusted counter; first_seen_partial is OR across contributing identities
        spans_suspend is OR across contributing identity intervals
        delta_kind = 'reset' if any counter decreased,
                     else 'baseline' if any identity had no prior baseline,
                     else 'normal'
    materialize sample_dev and exactly one per-tick sample_coverage row per app,
        including zero-T coverage; frame-level counters are attributed once to the
        observed frame timestamp, never copied to each sample timestamp
    persist staged open file-hour state; for finalized hours materialize file_sample,
        file_coverage and file_overflow_unknown, including zero-F coverage
    update each quantity's rollups and lifetime/boot totals once
    serialize the staged sampler state into sampler_checkpoint in this transaction
    commit rows, pending-hour state, checkpoint and last_seq atomically
```

`first_seen_partial` on an app/launch/attributed-timestamp sample row is `1` iff at least one identity contributing to that row was first observed with nonzero counters; `spans_suspend` is `1` iff at least one contributing interval for that row spans suspend. `partial_intervals` increments once per app/frame if any contributing identity is first-seen partial, and `spans_suspend_intervals` increments once per app/frame if any contributing interval spans suspend; multiple launches or attributed timestamps in that frame do not multiply these counts. The sole `sample_coverage` row uses the frame's observed timestamp, not an attributed timestamp. For rollups, sample bytes and per-interval peaks go to their attributed buckets; frame-level coverage counters go once to the bucket containing that `sample_coverage.ts_mono_ns`. For aggregated `delta_kind`, `reset` takes precedence over `baseline`, and `baseline` over `normal`; `baseline` applies when any contributing identity had no prior readable baseline, including a zero-valued first read.

Credits are component-wise. Credit values are cumulative child counters because the parent receives the child's cumulative totals on reap. A credit is applied to a delta computed on or after the scan that detects disappearance and never makes an emitted delta negative; this includes the current parent delta in section 10's reap fixture. No parent delta means the credit carries unchanged. Increment `reap_corrections` only on the first positive application of a child's credit, even if later ticks consume more components. Queue order is `(disappearance_epoch, disappearance_seq, child_pid, child_start_ticks)` ascending. If the parent vanishes first, remaining credit is recorded in its app's `reap_unapplied` and not folded into T. `reap_unobserved` MUST NOT be guessed from a large parent delta alone. **A02 — ratified:** each decreasing field MUST reset only its own baseline; an identity record with any decrease contributes exactly one reset. An already-open identity's ESRCH outcome contributes one reset independently; an unknown identity contributes none.

**A01 — ratified.** A first readable identity without an incoming handover baseline MUST emit its current counters exactly once and set `first_seen_partial=1` if any is nonzero. A handover baseline MUST take precedence and prevent re-emission of pre-cutoff bytes.

**A03 — ratified.** Without independent explicit evidence, P1 MUST keep all four `reap_unobserved` components zero and account a parent increment normally; polling magnitude alone MUST NOT infer an unseen child.

**A04 — ratified.** Parent credits MUST target the last observed `(boot_id,pid,start_ticks)` parent, never a reused PID or guessed reparenting target. FIFO MUST use disappearance sequence then `(child_pid,child_start_ticks)`; a child correction MUST count only at first positive use. Nested reaps or counter resets that make credit conservation unprovable MUST set persisted `reap_incomplete=1` and force T coverage incomplete. The component-wise nonnegative algorithm still applies, but an implementation MUST NOT claim byte-exactness for those intervals without an independently proved conservation rule. This is the ratified conservative contract, not a pending product decision.

### 5.2 Cgroup and file deltas

For each cgroup/device pair, D is the current cumulative `io.stat` value minus the last value for the same boot and cgroup identity. A cgroup with no `io.stat` contributes no D row and is not equivalent to a zero-byte row. **A05 — ratified:** baselines MUST be keyed by `(boot_id,cgroup_id,dev)`. A first observation or observed recreation MUST establish a fresh baseline and emit zero; a decrease MUST emit zero for that field and reset its baseline. Task migration MUST NOT copy a cgroup baseline to another cgroup or consume it once per process.

**A19 — ratified.** If distinct AppKeys share one cgroup and no unique approved owner exists, P1 MUST NOT duplicate or divide its D bytes. It MUST emit no per-app D for that cgroup and mark all affected apps' device attribution incomplete. This specification approves no synthetic cgroup owner; ordinary single-AppKey cgroups retain exact once-only attribution.

F events use `(cgroup_id,dev,inode,generation)` producer keys and MUST preserve reads, writes, bytes_r, and bytes_w separately. Before app-hour reduction, enrich each candidate with its supplied filesystem UUID and `identity_weak`; group by the hourly identity in §3.5, excluding basename. The later producer MUST supply per-cgroup overflow attempts and exact read/write bytes. A uniquely resolved cgroup MUST contribute to that app's `uncounted_overflow_*`; unkeyable loss MUST contribute under `overflow_owner_unknown`. A keyed cgroup without a unique app owner MUST also remain in that unknown-owner remainder, never receive an arbitrary split. Producer and store MUST preserve both quantities without deriving overflow from retained rows.

### 5.3 Quota and prune job

The quota job runs every 300 seconds and after a bulk import. Before expiring or quota-deleting a source unit, the transaction MUST commit the exact 60-second/hour rollups and boot/lifetime totals promised for that unit; file totals MUST be committed before deleting file observations. Expiry uses mapped wall nanoseconds: for `(boot_id, mono_ns)`, select `wall_offset_ns` from the `clock_map` row in that boot with greatest `segment_start_mono_ns <= mono_ns`, then compute `mono_ns + wall_offset_ns`. The current comparison time is mapped by the same rule. If either mapping is absent, pruning MUST stop before deleting a row and mark coverage incomplete rather than guess. A row is expired only when its mapped time is strictly less than `now_wall_ns - retention_ns`.

Retention and deletion units are independent: `sample`, `sample_dev`, and `sample_coverage` expire after 24 hours; hourly `file_sample`, `file_coverage`, and `file_overflow_unknown` expire after 14 days; 60-second rollups expire after 30 days; 3600-second rollups expire after 365 days. Totals, diagnostics, boot chronology, and path identities referenced by totals MUST NOT expire. `sample_coverage` MUST exist once per app/process tick, even with zero T. `file_coverage` MUST exist once per monitored app-hour, even with zero F, and MUST persist hour completeness independently of process retention. The unknown-owner hourly coverage MUST likewise exist for every monitored file hour, including a zero remainder. A pending hour is durable accumulator state, not an additional observation; it MUST be removed atomically when that hour is finalized. `gap` rows expire after 24 hours; persisted tier coverage retains older gap effects.

Under quota pressure, select candidates in tier order: raw units, 60-second app buckets, then 3600-second app buckets. Within each tier select the smallest deterministic key. A core raw unit is `(boot_id, source, epoch, seq, app_id)` and deletes all `sample` attributed-timestamp rows plus matching `sample_dev` and `sample_coverage` rows. A file raw unit is `(boot_id,app_id,ts_mono_ns)` for one hour and deletes its `file_sample` plus `file_coverage`; source/epoch/seq are provenance, not extra hourly budgets. Unknown-owner file units use `(boot_id,ts_mono_ns)` and delete `file_overflow_unknown` only. A gap is its own unit keyed by `gap_id`. A rollup unit is `(boot_id, app_id, bucket_secs, bucket_start_mono_ns)` and includes the corresponding `rollup_app` row and all `rollup_dev` rows. Within a tier, the primary sort value is mapped wall time computed from `sample_coverage.ts_mono_ns` for core units, `file_coverage.ts_mono_ns` for app-file units, `file_overflow_unknown.ts_mono_ns` for unknown-owner units, `gap.to_mono_ns` for gaps, and `bucket_start_mono_ns` for rollups. Remaining keys sort by `boot_id` UTF-8 bytes, `source` UTF-8 bytes (empty for rollups), `epoch` (zero for rollups), `seq` (zero for rollups), `app_id` (zero for gaps), unit kind (`core`, `file`, `file_unknown`, `gap`, then `rollup`), `bucket_secs` (zero for raw/gap units), and `gap_id` for gaps or zero otherwise. The final comparison component MUST be the complete primary key of the selected deletion unit, compared lexicographically by its typed fields in the key's declared order: core `(boot_id,source,epoch,seq,app_id)`; app-file `(boot_id,app_id,ts_mono_ns)`; unknown-owner file `(boot_id,ts_mono_ns)`; gap `(gap_id)`; rollup `(boot_id,app_id,bucket_secs,bucket_start_mono_ns)`. This final component is mandatory even where earlier components usually distinguish units. No child row may outlive a deleted core raw unit; file observations are independent units by design.

```text
quota_prune(boot_id, now_mono_ns):
    BEGIN IMMEDIATE
    ensure exact due rollups and boot/lifetime totals already exist; do not re-add them
    map the current boottime to now_wall_ns
    delete expired core raw units using 24h cutoff
    delete expired app-file and unknown-owner hourly units using 14d cutoff
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

**A13 — ratified.** `physical_store_bytes()` MUST sum `st_size` for the main database, `-wal`, and `-shm` after a successful checkpoint; a missing sidecar contributes zero. The quota MUST be decimal `1_500_000_000` bytes. Each deletion unit MUST commit and checkpoint before remeasurement; the final sort key is `gap_id` for gaps and zero otherwise. Unknown-owner units use `app_id=0` for sorting only, never a synthetic app row. The mapped-wall age and exact deterministic ordering above MUST be used across boots. Totals MUST never be deleted. Sampling MUST pause when eligible pruning/shrink cannot meet quota and MUST resume only after a fresh successful size check satisfies the limit.

### 5.4 Deterministic materialization

For each accepted frame, app/launch samples MUST sum adjusted process deltas component-wise per attributed timestamp, as specified in §5.1. Each cgroup/device baseline MUST be consumed once, with its first observation emitting zero. F candidates MUST accumulate for a fixed boottime hour before top-100 selection, merging the same hourly aggregation identity across basenames and cgroups. A single `file_hour_state` record per `(boot_id,hour)` MUST checkpoint the complete accumulator atomically with stream receipts.

The version-1 `file_hour_state.state_json` object MUST have exactly these top-level fields and types: `version` (integer 1), `boot_id` (nonempty string equal to the row key), `hour_start_mono_ns` (nonnegative hour-aligned integer equal to the row key), `apps` (array), and `unknown_owner` (object). `apps` MAY be empty only when no app contributed in that hour; otherwise each app object has exactly `app_key`, `files`, `coverage`, and `last_receipt`. AppKeys are unique and sorted by UTF-8 bytes. Each `files` array has exactly one object per `(fs_uuid,dev,inode,generation,identity_weak)` and is sorted lexicographically by those typed fields, with text compared as UTF-8 bytes. A file object has exactly `fs_uuid` (string, empty permitted), `dev` (nonempty string), `inode` (integer `[0,2^63-1]`), `generation` (integer `[0,2^32-1]`), `identity_weak` (integer 0 or 1), `basename` (nonempty UTF-8 string), `aliases` (array of every other captured basename, unique and UTF-8-byte sorted), `state` (`resolved`, `truncated`, or `unresolved`), `reads`, `writes`, `bytes_r`, `bytes_w` (nonnegative SQLite-range integers), and `last_seen_mono_ns` (integer in `[hour_start,hour_start+3600000000000)`). `basename` is the bytewise least name observed for that aggregation identity in the hour. The app `coverage` object has exactly `counted_omitted_files`, `counted_omitted_bytes_r`, `counted_omitted_bytes_w`, `uncounted_overflow_files`, `uncounted_overflow_bytes_r`, `uncounted_overflow_bytes_w`, `skipped_nonfile`, `skipped_unsupported_fs`, `weak_identity_rows`, `hook_hits`, `gap_intervals`, `overlap_uncertain_intervals`, and `complete`; its 11 scalar counters are nonnegative SQLite-range integers, `hook_hits` has exactly the six §4.3 names with nonnegative integer values, and `complete` is integer 0 or 1. `last_receipt` has exactly `source` (`gui` or `iod`), `epoch` (positive integer), and `seq` (nonnegative integer). The `unknown_owner` object has exactly `overflow_files`, `overflow_bytes_r`, `overflow_bytes_w`, `coverage`, and `last_receipt`; counters are nonnegative SQLite-range integers, coverage has exactly `gap_intervals`, `overlap_uncertain_intervals`, and `complete` with the same integer rules, and `last_receipt` is either null (no unknown-owner contribution) or the same receipt object. It MUST exist even when all counters are zero.

The version-1 `handover.baselines_json` object MUST have exactly `version` (integer 1), `boot_id` (nonempty string equal to the row key), `cutoff_mono_ns` (nonnegative SQLite-range integer equal to the row key), `processes` (array), and `devices` (array). Each process entry has exactly `pid`, `start_ticks`, `ppid`, `parent`, `app_key`, `launch_root`, `launch_id`, `baseline`, and `credits`: `pid` is positive; `start_ticks`/`ppid` are nonnegative; parent and launch root are null or exactly `{pid,start_ticks}`; `app_key` is a valid external AppKey; `launch_id` is a nonnegative SQLite-range integer (0 means unresolved); baseline is null or an exact four-key CounterName object of nonnegative SQLite-range integers; credits is an array ordered by `(disappearance_epoch,disappearance_seq,child.pid,child.start_ticks)`, each with exactly `child`, `disappearance_epoch`, `disappearance_seq`, `remaining`, and `correction_counted`, where child has exactly `{pid,start_ticks}`, remaining has exactly the four CounterName keys, and correction_counted is 0 or 1. Processes are ordered by `(pid,start_ticks)`. Device entries have exactly `cgroup_id`, `dev`, `rbytes`, and `wbytes`, with nonnegative SQLite-range integers except nonempty UTF-8 `dev`, and are ordered by `(cgroup_id,dev UTF-8 bytes)`. Duplicate process/device/credit identities, unknown or missing keys, wrong JSON types (including booleans where integers are required), out-of-range values, invalid AppKeys, or a mismatched boot/cutoff MUST reject the entire object.

The `sampler_checkpoint.state_json` root has exactly `version` (integer 1), `boot_id` (nonempty string), `source` (`gui` or `iod`), `epoch` (positive integer), `last_seq` (nonnegative integer), `checkpoint_mono_ns` (nonnegative SQLite-range integer), `processes` (array), and `devices` (array). Its process/device entries have exactly the handover schemas above. The root `source`, `epoch`, and `last_seq` MUST equal the row's `source`, `epoch`, and `seq` column respectively; `checkpoint_mono_ns` MUST equal that column, and the checkpoint boot MUST equal its `boot_id` column. Restore only a valid checkpoint for the current boot; restore all open identities including entries with null baseline, every component-wise credit and its correction-counted bit, and every cgroup/device baseline. A frame transaction MUST update sample rows, receipts, totals, rollups, file-hour pending state, `collector_epoch.last_seq`, and `sampler_checkpoint` before COMMIT; stage in-memory state privately and publish it only after that COMMIT. A crash before COMMIT restores the previous checkpoint and applies none of that frame; a crash after COMMIT restores its checkpoint and receipt, so replay is rejected and the following cumulative sample subtracts the committed baseline. Handover serializes the same process/device entries at the cutoff, and an incompatible/incomplete object MUST be rejected with a gap/uncertainty rather than partially restored.

All these JSON values MUST use the section's canonical formatting: UTF-8, sorted object keys, `ensure_ascii=False`, separators `(',', ':')`, decimal integer tokens, no trailing LF, no duplicate keys, and no nonfinite values. Array orders are the explicit typed orders above, independent of object-key serialization. Database-local IDs MUST NOT be used as interchange identity. Pending state MUST survive restart/handover. Finalization MUST write one snapshot (at most 100 rows) and coverage row per app-hour, but exactly one unknown-owner row per boot-hour, then delete pending state atomically. It MUST advance `boot.file_finalized_through_mono_ns` to that hour's end in chronological order. An hour below that durable watermark MUST NOT reopen after replay or even after its observations have expired. Counted omissions and overflow MUST NOT be re-added to retained rows.

Canonical non-empty pending-hour fixture (compact canonical JSON; app `unit:app-demo.service` has one file and the reserved owner has an explicit zero remainder):

```json
{"apps":[{"app_key":"unit:app-demo.service","coverage":{"complete":1,"counted_omitted_bytes_r":0,"counted_omitted_bytes_w":0,"counted_omitted_files":0,"gap_intervals":0,"hook_hits":{"vfs_iter_read":1,"vfs_iter_write":0,"vfs_read":2,"vfs_readv":0,"vfs_write":0,"vfs_writev":0},"overlap_uncertain_intervals":0,"skipped_nonfile":0,"skipped_unsupported_fs":0,"uncounted_overflow_bytes_r":0,"uncounted_overflow_bytes_w":0,"uncounted_overflow_files":0,"weak_identity_rows":0},"files":[{"aliases":["b.txt"],"basename":"a.txt","bytes_r":40,"bytes_w":10,"dev":"259:0","fs_uuid":"1111-aaaa","generation":7,"identity_weak":0,"inode":42,"last_seen_mono_ns":120000000000,"reads":2,"state":"resolved","writes":1}],"last_receipt":{"epoch":2,"seq":9,"source":"gui"}}],"boot_id":"11111111-2222-4333-8444-555555555555","hour_start_mono_ns":0,"unknown_owner":{"coverage":{"complete":1,"gap_intervals":0,"overlap_uncertain_intervals":0},"last_receipt":null,"overflow_bytes_r":0,"overflow_bytes_w":0,"overflow_files":0},"version":1}
```

Canonical non-empty handover fixture (one live process and one device baseline):

```json
{"boot_id":"11111111-2222-4333-8444-555555555555","cutoff_mono_ns":120000000000,"devices":[{"cgroup_id":42,"dev":"259:0","rbytes":500,"wbytes":20}],"processes":[{"app_key":"unit:app-demo.service","baseline":{"rchar":120,"read_bytes":10,"wchar":30,"write_bytes":5},"credits":[],"launch_id":3,"launch_root":{"pid":10,"start_ticks":7},"parent":null,"pid":10,"ppid":1,"start_ticks":7}],"version":1}
```

`file_hour_state` and handover/checkpoint validators MUST parse JSON structurally, require these exact keys/types/order invariants, reject duplicate object keys, and then require byte-for-byte equality with canonical re-encoding. Merely accepting `json_valid`, or accepting `{}`/an object in place of an array, is nonconforming.

For each app/process frame, process coverage MUST be reduced once across identities, independently of launches, and inserted once into both 60-second and 3600-second app buckets and boot/lifetime process totals. Both widths receive the frame directly; an hour MUST NOT also add its minute rows. File coverage instead follows its hourly tier and `app_boot_file_coverage`; lifetime file coverage is the sum of those boot rows. Process-tier JSON retains a zero-valued file placeholder and MUST NOT masquerade as a finer file observation. File queries MUST obtain file fields from their own hourly/total source. All additive updates and hour finalization MUST be guarded by the committed receipt/finalized-hour identity. Retention jobs MUST NOT replay additive UPSERTs into existing totals.

**A16 — ratified.** For a positive measured app interval, each rate MUST be `floor(delta_bytes * 1_000_000_000 / observed_ns)`. The combined rate MUST be `floor((delta_read_bytes + delta_write_bytes) * 1_000_000_000 / observed_ns)` from that same interval. Raw queries and both rollup widths MUST take maxima of those same defined interval read, write, and combined rates, never averages fabricated from a wider bucket. A baseline, zero-duration, or unknown/common-interval failure has no defined rate. Persist each peak as nullable: SQL NULL means no defined contributing interval; integer zero means at least one defined interval whose floored rate is zero. Rollup reduction is `MAX` over defined values, ignoring NULL; if every contributing value is NULL, the reduced peak remains NULL. The same frame rates feed boot peaks. Totals-only query peaks MUST remain `None`. Ordinary bucket assignment uses the integer rule in section 4.1; suspend timestamps/durations MUST follow A09.

Surrogate IDs are allocated as one greater than the current maximum positive ID (or `1` for an empty table), with new apps sorted by AppKey UTF-8 bytes, launches by `(app_id, started_mono_ns, root_pid, root_start_ticks)`, and paths by their declared uniqueness tuple. Existing identities reuse their IDs. Text metadata is supplied input, not locale-derived. Canonical JSON uses sorted keys, UTF-8, `ensure_ascii=False`, separators `(',', ':')`, decimal integer tokens, and no trailing LF or nonfinite values. These rules specify equal row values from equal prior state without promising equal database files.

**A21 — ratified.** A counter, aggregate, or peak outside its SQLite range MUST reject and roll back the entire observation transaction, including sequence, baseline and total changes. The caller MUST receive `CounterOverflowError`. In a separate representable transaction, the store MUST persist a `store_diagnostic` with `code="counter_overflow"`, stream identity, affected already-persisted app (NULL means the whole stream, including a new app whose insert rolled back), the observation time and offending field name; no out-of-range numeric payload is stored. Queries intersecting the affected observation bucket MUST OR `counter_overflow=1` into coverage, including boot/lifetime queries, and MUST report incomplete coverage. Diagnostics MUST survive pruning. If diagnostic persistence fails, collection MUST stop with that failure rather than acknowledge success. Subsequent observations MUST NOT silently bridge the rejected interval; recovery MUST establish an explicit baseline/gap. Wrapping, saturation and REAL coercion are forbidden.

## 6. SQLite Schema

### 6.1 Types and constraints

SQLite uses signed 64-bit `INTEGER`. Every table below is `STRICT`; the minimum supported SQLite version is 3.37.0 with the JSON functions available. Nonnegative counters and timestamps have range `[0, 9223372036854775807]`; explicitly signed integers use `[-9223372036854775808, 9223372036854775807]`. `pid` and local surrogate IDs are positive integers; `start_ticks` is nonnegative. `ts_wall`, `started_wall`, and app `first_seen`/`last_seen` are Unix epoch seconds as finite nonnegative binary64 SQLite `REAL`; conversion from integer wall nanoseconds divides by `1_000_000_000` with round-to-nearest, ties-to-even. Booleans are integer `0` or `1`. `STRICT` enforces storage classes after SQLite affinity conversion; the Python boundary MUST reject bools as integers and MUST validate types/ranges before binding, rather than rely on coercion.

`coverage_json` MUST be UTF-8 JSON object text. Its top-level keys MUST be exactly `inaccessible_pids`, `resets`, `reap_corrections`, `reap_unapplied`, `reap_unobserved`, `partial_intervals`, `gap_intervals`, `overlap_uncertain_intervals`, `spans_suspend_intervals`, `reap_incomplete`, `counter_overflow`, `device_state`, and `file`. Counts and each reap component use `[0,2^63-1]`; the two added flags use integer `{0,1}`. `device_state` is `available`, `not_delegated`, or `incomplete`. The `file` object MUST have exactly `counted_omitted_files`, `counted_omitted_bytes_r`, `counted_omitted_bytes_w`, `uncounted_overflow_files`, `uncounted_overflow_bytes_r`, `uncounted_overflow_bytes_w`, `skipped_nonfile`, `skipped_unsupported_fs`, `weak_identity_rows`, and `hook_hits`. `hook_hits` MUST have exactly the six named counters. Python MUST reject booleans in place of these integers and validate exact keys, components and ranges in addition to SQLite `json_valid`; each reap object has exactly the four CounterName keys.

**A14 — ratified.** Counts/bytes/reap components/hook hits MUST add component-wise, and flags MUST combine by OR. Process coverage MUST commit with its tick; hourly file coverage MUST commit with hour finalization. `file_coverage.coverage_json.file` MUST equal its nine structured numeric columns and six hook columns for that same app-hour. A zero-F monitored hour still has that row. The file subobject in process-only `sample_coverage`, process rollups and process totals MUST be the zero placeholder; file evidence MUST instead survive in hourly `file_coverage` and never-pruned `app_boot_file_coverage`. This period separation supersedes revision 1's per-tick mirror, which cannot represent the ratified hourly file observations. Unknown-owner quantities MUST remain separate from every app JSON.

### 6.2 DDL

The DDL is an executable, typed superset of r4's abbreviated schema. It persists raw coverage, sequence receipts, boot chronology and every monotonic value's boot identity. It also persists observed-time rate evidence, launch roots, hourly file completeness, pending-hour recovery state, per-boot file LAST/coverage, separate unknown-owner hourly/boot totals, and representable counter-overflow diagnostics. Hourly file primary keys enforce a single app-hour budget across collectors. Every table is STRICT; these additions require the schema-specific size measurement in A15.

```sql
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE boot (
    boot_id TEXT PRIMARY KEY NOT NULL CHECK (length(boot_id) > 0),
    boot_order INTEGER NOT NULL UNIQUE CHECK (boot_order > 0),
    file_finalized_through_mono_ns INTEGER NOT NULL DEFAULT 0
        CHECK (file_finalized_through_mono_ns >= 0 AND file_finalized_through_mono_ns % 3600000000000 = 0),
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
    root_pid INTEGER NOT NULL CHECK (root_pid > 0),
    root_start_ticks INTEGER NOT NULL CHECK (root_start_ticks >= 0),
    started_mono_ns INTEGER NOT NULL CHECK (started_mono_ns >= 0),
    ended_mono_ns INTEGER CHECK (ended_mono_ns IS NULL OR ended_mono_ns >= started_mono_ns),
    proc_count INTEGER NOT NULL CHECK (proc_count >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    UNIQUE (launch_id, app_id, boot_id),
    UNIQUE (boot_id, app_id, root_pid, root_start_ticks)
) STRICT;

CREATE TABLE sample (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    launch_id INTEGER NOT NULL CHECK (launch_id >= 0),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    observed_mono_ns INTEGER NOT NULL CHECK (observed_mono_ns >= ts_mono_ns),
    interval_start_mono_ns INTEGER,
    observed_ns INTEGER,
    ts_wall REAL NOT NULL CHECK (ts_wall BETWEEN 0.0 AND 1.7976931348623157e308),
    rchar INTEGER NOT NULL CHECK (rchar BETWEEN 0 AND 9223372036854775807),
    wchar INTEGER NOT NULL CHECK (wchar BETWEEN 0 AND 9223372036854775807),
    read_bytes INTEGER NOT NULL CHECK (read_bytes BETWEEN 0 AND 9223372036854775807),
    write_bytes INTEGER NOT NULL CHECK (write_bytes BETWEEN 0 AND 9223372036854775807),
    delta_kind TEXT NOT NULL CHECK (delta_kind IN ('normal', 'baseline', 'reset')),
    first_seen_partial INTEGER NOT NULL CHECK (first_seen_partial IN (0, 1)),
    spans_suspend INTEGER NOT NULL CHECK (spans_suspend IN (0, 1)),
    CHECK ((interval_start_mono_ns IS NULL AND observed_ns IS NULL) OR
           (interval_start_mono_ns IS NOT NULL AND observed_ns IS NOT NULL AND
            interval_start_mono_ns BETWEEN 0 AND observed_mono_ns AND
            observed_ns BETWEEN 0 AND observed_mono_ns - interval_start_mono_ns)),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id, launch_id, ts_mono_ns)
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
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    path_id INTEGER NOT NULL REFERENCES path(path_id),
    reads INTEGER NOT NULL CHECK (reads BETWEEN 0 AND 9223372036854775807),
    writes INTEGER NOT NULL CHECK (writes BETWEEN 0 AND 9223372036854775807),
    bytes_r INTEGER NOT NULL CHECK (bytes_r BETWEEN 0 AND 9223372036854775807),
    bytes_w INTEGER NOT NULL CHECK (bytes_w BETWEEN 0 AND 9223372036854775807),
    last_seen_mono_ns INTEGER NOT NULL CHECK (last_seen_mono_ns >= 0),
    CHECK (last_seen_mono_ns >= ts_mono_ns AND last_seen_mono_ns - ts_mono_ns < 3600000000000),
    PRIMARY KEY (boot_id, app_id, ts_mono_ns, path_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE file_coverage (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
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
    PRIMARY KEY (boot_id, app_id, ts_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE file_hour_state (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    PRIMARY KEY (boot_id, ts_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE sampler_checkpoint (
    source TEXT PRIMARY KEY NOT NULL CHECK (source IN ('gui', 'iod')),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    checkpoint_mono_ns INTEGER NOT NULL CHECK (checkpoint_mono_ns >= 0),
    state_json TEXT NOT NULL CHECK (json_valid(state_json))
) STRICT;

CREATE TABLE file_overflow_unknown (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    owner_key TEXT NOT NULL CHECK (owner_key = 'overflow_owner_unknown'),
    overflow_files INTEGER NOT NULL CHECK (overflow_files >= 0),
    overflow_bytes_r INTEGER NOT NULL CHECK (overflow_bytes_r >= 0),
    overflow_bytes_w INTEGER NOT NULL CHECK (overflow_bytes_w >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, ts_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE boot_file_overflow_unknown (
    boot_id TEXT PRIMARY KEY NOT NULL REFERENCES boot(boot_id),
    owner_key TEXT NOT NULL CHECK (owner_key = 'overflow_owner_unknown'),
    overflow_files INTEGER NOT NULL CHECK (overflow_files >= 0),
    overflow_bytes_r INTEGER NOT NULL CHECK (overflow_bytes_r >= 0),
    overflow_bytes_w INTEGER NOT NULL CHECK (overflow_bytes_w >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json))
) STRICT;

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
    peak_read_bps INTEGER CHECK (peak_read_bps IS NULL OR peak_read_bps >= 0),
    peak_write_bps INTEGER CHECK (peak_write_bps IS NULL OR peak_write_bps >= 0),
    peak_total_bps INTEGER CHECK (peak_total_bps IS NULL OR peak_total_bps >= 0),
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
    peak_read_bps INTEGER CHECK (peak_read_bps IS NULL OR peak_read_bps >= 0),
    peak_write_bps INTEGER CHECK (peak_write_bps IS NULL OR peak_write_bps >= 0),
    peak_total_bps INTEGER CHECK (peak_total_bps IS NULL OR peak_total_bps >= 0),
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
    last_seen_mono_ns INTEGER NOT NULL CHECK (last_seen_mono_ns >= 0),
    PRIMARY KEY (boot_id, app_id, path_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_file_coverage (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, app_id)
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

CREATE TABLE store_diagnostic (
    diagnostic_id INTEGER PRIMARY KEY NOT NULL CHECK (diagnostic_id > 0),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    code TEXT NOT NULL CHECK (code = 'counter_overflow'),
    field TEXT NOT NULL CHECK (length(field) > 0),
    UNIQUE (boot_id, source, epoch, seq, field)
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
CREATE INDEX store_diagnostic_time_idx ON store_diagnostic(boot_id, ts_mono_ns, app_id);
CREATE INDEX collector_interval_time_idx ON collector_interval(boot_id, from_mono_ns, to_mono_ns);
CREATE INDEX rollup_app_time_idx ON rollup_app(app_id, boot_id, bucket_secs, bucket_start_mono_ns);
CREATE INDEX rollup_dev_time_idx ON rollup_dev(app_id, boot_id, bucket_secs, bucket_start_mono_ns, dev);
CREATE INDEX gap_time_idx ON gap(boot_id, from_mono_ns, to_mono_ns, source, epoch);

PRAGMA user_version = 1;
COMMIT;
```

### 6.3 WITHOUT ROWID and index rationale

Composite-key observation tables use `WITHOUT ROWID` because their full composite keys address the records directly, avoiding a second rowid-to-key index. `path`, `app`, `launch`, and `app_lifetime_total` retain rowids so their integer primary keys alias the rowid without a separate primary-key B-tree. Single-key metadata tables `boot`, `collector_epoch`, `sampler_checkpoint`, and `boot_file_overflow_unknown` retain conventional rowid storage; their primary-key indexes MUST be included in schema measurements. `gap` and `store_diagnostic` retain rowid-backed surrogates for independently addressable recovery records.

The primary keys on `handover` and `clock_map` already provide their boot/cutoff and boot/segment lookup order, so no duplicate secondary indexes are created. The secondary indexes cover app/time filtering and the supported path basename / last-seen sort. Additional indexes MUST be justified by an observed query plan and included in quota measurements; no index may change the result order specified in section 8.

### 6.4 Empty-database migration

For a new empty DB, a migration runner MUST enable foreign keys outside the transaction, execute the single `BEGIN IMMEDIATE` in section 6.2, and then confirm no user tables exist and `PRAGMA user_version = 0` before executing that block's first `CREATE TABLE`. It executes the remaining statements and sets `user_version=1` before the block's single commit. The runner MUST NOT wrap the block in another transaction or use an executor that implicitly commits between statements. Failed preconditions or DDL MUST roll back. If a nonempty database has `user_version=0`, startup MUST fail closed and preserve the file; it MUST NOT guess that the file is empty or rewrite it. If `user_version=1`, startup MUST validate required columns, constraints, tables, and indexes without recreating them. Any unsupported higher version MUST be rejected without mutation.

The database schema is created independently in each store. Integer `app_id`, `launch_id`, `path_id`, `gap_id`, `diagnostic_id`, and `boot_order` values are local and MUST NOT cross store boundaries as identity. External identity uses `AppKey`, `boot_id`, and stream identity.

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

The required hourly file arithmetic is: `107 apps * 100 files = 10700 rows/snapshot`; `10700 * 88 = 941600 bytes`, about 1 MB per snapshot. `24 snapshots/day * 10700 = 256800 rows/day`; `256800 * 88 = 22598400 bytes/day`, about 22.6 MB/day. `256800 * 14 = 3595200 rows`; `3595200 * 88 = 316377600 bytes`, about 316 MB of retained payload. These are hourly snapshots after aggregation, not another sampling cadence.

The exact payload subtotal excluding totals is `66562560 + 73958400 + 316377600 + 295833600 + 517708800 + 179965440 = 1450406400` bytes. R4's coarsely rounded planning entries (`66 + 74 + 316 + 296 + 518 + 181 MB`) sum to 1,451 MB; the exact products above govern arithmetic. These payloads omit indexes, extra coverage/identity columns, pending-hour state, diagnostics, free pages and sidecars. They MUST NOT be presented as a measured total for this DDL.

**A15 — ratified.** Before shipping, the exact schema MUST be remeasured with its indexes and representative data, including all added tables/columns and DB/WAL/SHM. Until then the table is planning data. The enforceable physical limit MUST remain decimal `1_500_000_000` bytes under A13; a planning estimate MUST NOT waive quota enforcement.

### 7.2 Retention and selection

Default retention is raw app/device samples 24 hours, file observations 14 days, 60-second app/device rollups 30 days, 3600-second rollups 365 days, and boot/lifetime totals forever. Quota pressure can prune oldest eligible buckets before these maxima; totals are never deleted.

File top-N ordering is descending `bytes_r + bytes_w`, then descending `reads + writes`, then ascending `(dev, inode, generation, identity_weak, fs_uuid UTF-8 bytes, basename UTF-8 bytes, path_id)`. The final fields distinguish weak from strong identities and every distinct retained path-row identity; `path_id` is the final local uniqueness guard. Basename is not an aggregation key but is part of path-row identity. The omitted remainder is stored in `file_coverage` as counted omission; full-map insertion failures are stored separately as uncounted overflow. The Files result is labelled “retained observations” because a per-hour top-N can hide a file that led a larger window.

**A11 — ratified.** Hours MUST be fixed `CLOCK_BOOTTIME` buckets of `3_600_000_000_000` ns. All contributions owned by either collector in that app-hour MUST aggregate into the same candidate set before applying one top-100 budget. Stored `ts_mono_ns` is the hour start; source/epoch/seq identify the last authoritative contributing receipt, not another snapshot. No per-drain snapshot or extra per-collector top-100 budget is permitted. The pending accumulator MUST retain all candidates needed for exact hour ranking; quota failure MUST pause collection and disclose a gap. Hourly coverage records the full counted omitted remainder, overflow, hooks, gaps and completeness, even if no file row survives selection. Boot file totals MUST sum retained hourly rows, with omissions/overflow preserved separately; they retain the “retained observations” meaning and MUST store per-boot LAST.

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
    def __init__(self, reason: UnavailableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason

class CounterOverflowError(OverflowError):
    def __init__(self, field: str) -> None:
        super().__init__(f"Counter outside SQLite INTEGER range: {field}")
        self.field = field

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
    reap_incomplete: bool
    counter_overflow: bool
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
    peak_total_bps: int | None
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
    peak_total_bps: int | None
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
class UnknownOwnerOverflow:
    window: Window
    owner_key: Literal["overflow_owner_unknown"]
    overflow_files: int
    overflow_bytes_r: int
    overflow_bytes_w: int
    coverage: Coverage

@dataclass(frozen=True)
class FileTable:
    app_id: int
    window: Window
    rows: tuple[FileRow, ...]
    sort: FileSort
    direction: Direction
    search: str | None
    unknown_owner_overflow: UnknownOwnerOverflow
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
    unknown_owner_overflow: UnknownOwnerOverflow
    coverage: Coverage
```

**A16 — ratified.** Peaks MUST use the exact integer interval-rate rule in section 5.4, preserving original byte counts. UI formatting SHOULD round to the nearest displayed unit with ties upward; display rounding MUST NOT change stored rates or reconciliation arithmetic. A wider rollup MUST retain the maxima of the same underlying intervals.

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
def unknown_owner_file_coverage(window: Window) -> UnknownOwnerOverflow: ...
```

`Window` validation MUST reject malformed combinations with `ValueError`: `range` requires boot/start/end, `0 <= start < end <= 2^63-1`, and no launch; `since_boot` requires boot and positive end and has no explicit start/launch; `launch` requires boot and positive launch ID and no endpoints; `lifetime` has no boot, launch, or monotonic bounds. Enum values outside the Literals, nonpositive app IDs, and NUL in search MUST raise `ValueError`. Next, every function in section 8.2 MUST enforce A22's disabled-window errors before any ID lookup or tier selection; a well-formed launch with an unknown ID still receives the disabled-window error. For supported windows, a valid but unknown app ID MUST raise `KeyError`. Each query MUST use one consistent database snapshot. All result containers carry populated Coverage; individual points/rows inherit it.

For every query, `Coverage.complete` MUST be false if a required interval is missing, uncertain, clipped by bucket edges, pruned, or marked incomplete. For T it MUST also be false for nonzero `inaccessible_pids`, `resets`, `partial_intervals`, `reap_incomplete`, or an affecting counter-overflow diagnostic. For D it MUST be false unless device coverage and ownership are exact. For F it MUST be false when overflow loses path attribution, a nonzero unknown-owner remainder could belong to the app, unknown-owner evidence is missing, an affecting diagnostic exists, or the window cannot be reconstructed. Counted omissions remain complete for byte accounting because their bytes are represented exactly; returned rows remain retained/top-N observations. `weak_identity_rows` reports identity quality but does not by itself make byte accounting incomplete. A metric unaffected by another metric's unavailable source may remain complete (for example, not-delegated D does not make T incomplete).

Coverage equality has two stages. First reduce and compare every persisted coverage field from the exact deduplicated source rows selected by the query, using the section 4.3 sum/component-wise/OR/device-state operators; `spans_suspend` is OR-reduced from selected sample rows. Then apply overlays that are not in those source rows: intersecting `store_diagnostic` rows OR `counter_overflow=True`; query-derived tier, `complete`, and `unavailable_reason` are computed from window validation, retained interval/bucket coverage, required source availability, clipping, pending-hour tails and the preceding diagnostics. `complete` and `unavailable_reason` MUST NOT be asserted equal to a reduction that omits these overlays. An unavailable/incomplete D result has `device_read_bytes`, `device_write_bytes`, `device_total_bytes`, and all residuals set to `None`; a property test MUST guard before arithmetic and MUST NOT evaluate `0 + None`. T totals remain numeric. A21 diagnostic overlays MUST apply even when the rejected observation has no surviving raw row.

`ranking` sorts by requested metric descending, then `app_key` ascending bytewise. `total_bytes = read_bytes + write_bytes`. Every result's app total is the sum of accepted task deltas for the selected tier/window and stream ownership rules. An inaccessible process makes that app total a lower bound; the result carries its count and `complete=False`. `ranking` completeness concerns T; `series` and `disk_share` completeness concern both requested T and D; file-query completeness concerns F. An empty ranking is `[]`, not an invented zero-valued app.

`series` returns no points for `totals_only`; `peak_*_bps` is `None` there. Other points sort by `(bucket_start_mono_ns, bucket_secs)`; device points append `dev` bytewise to that key, and residual points align with task points. `device_points` use raw major:minor tokens. Residuals are computed per bucket from task and device bucket totals and are `None` where D is unavailable. `disk_share` returns device rows in ascending `(boot_id, dev)` UTF-8 order, preserving distinct boot identities. When D is unavailable or mixed, aggregate device bytes and all residuals are `None`; available device rows may still be returned as partial observations with `complete=False`. Its signed total/read/write residuals obey section 2.4 whenever device coverage is complete.

File sort keys MUST be exact: `bytes_total=bytes_r+bytes_w`; numeric keys sort numerically; `last_seen` uses `(boot.boot_order,last_seen_mono_ns)`, never cross-boot boottime alone; `basename` uses NFC Unicode codepoint order; `dev` uses UTF-8 bytes. Every sort MUST append `(dev,inode,generation,basename,path_id)` ascending; `direction` reverses only the primary key. LAST MUST use `file_sample.last_seen_mono_ns` or that boot's `app_boot_file_total.last_seen_mono_ns`. Lifetime rows MUST choose their last boot by persisted chronology.

**A17 — ratified.** Search MUST compare `NFC(search).casefold()` as a substring of `NFC(basename).casefold()` before sorting; None/empty search matches all. Returned names retain their spelling. The API requires an explicit direction; the consumer default MUST be `sort="bytes_total", direction="desc"`. For a supported but unrepresentable file window, the result MUST have an empty row tuple, zero retained-observation bytes, incomplete coverage and the applicable unavailability reason; zero observed bytes MUST NOT claim zero F. A22's disabled-window exceptions take precedence. `FileCoverage.observed_bytes_r/w` MUST sum retained rows before search and MUST exclude omissions and both kinds of overflow.

### 8.3 Tier behavior

`ranking`, `series`, and `disk_share` MUST use section 4.2's app/device tiers after validating scope. Exact full-boot/lifetime totals-only ranking MUST use `app_boot_total`/`app_lifetime_total`; disk share MUST use that T source and `app_boot_dev_total` grouped by `(boot_id,dev)`. Equal major:minor strings from different boots MUST NOT merge. Totals-only series MUST have empty point tuples and no peak; coverage MUST state `tier="totals_only"`. An explicit since-boot or lifetime request may use its exact totals even above 365 days; this does not enable a bounded range of that duration.

**A22 — ratified, operator ruling.** Every public store query, including file and unknown-owner coverage queries, MUST apply this table. Error `str(error)` MUST equal the reason text shown; `error.reason` is the listed token. A consumer MUST render a disabled, unselected chip/control with that text visible as its reason (also exposed to accessibility), MUST NOT dispatch it as an enabled choice, and MUST NOT silently select or label a wider replacement window. If an old persisted selection or direct API call reaches this error, the detail view MUST render unavailable data with the same text; no stale numeric series/totals may appear under the disabled label.

| Requested scope | Exact store outcome | Exact required consumer state |
|---|---|---|
| Any well-formed `kind="launch"`, regardless of raw retention | Raise `WindowUnavailableError(reason="launch_attribution", message="Launch window disabled: device and file activity are not stored per launch.")` | `Launch` control disabled and unselected; reason text exactly as the error message; queried detail state unavailable |
| `kind="range"` with `end-start > 365*86400*1000000000` | Raise `WindowUnavailableError(reason="unsupported_window", message="Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.")` | That range control disabled and unselected; reason text exactly as the error message; queried detail state unavailable |

For a supported selected historical tier already pruned, `series` MUST return remaining observed points with `coverage.complete=False` and `unavailable_reason="not_retained"`; missing time MUST NOT become zero-valued points or trigger a tier substitution. A historical since-boot prefix above 365 days that is not the exact full-boot cutoff MUST raise `WindowUnavailableError(reason="unsupported_window", message="Historical boot prefix unavailable: totals cover the full collected boot.")`; it MUST render unavailable with that reason, not the full-boot number.

File queries MUST use retained, fully contained hourly observations with `tier="raw"` (hourly file resolution, not 10-second process resolution). `app_boot_file_total` and `app_boot_file_coverage` MUST support exact full-boot retained-observation totals, access counts, per-boot LAST and coverage after hourly expiry. Lifetime file totals MUST aggregate these boot rows, choosing LAST by boot chronology; they MUST NOT fabricate an arbitrary bounded range. An unfinished hour remains pending, so a query including its tail MUST be incomplete. A supported bounded file request whose necessary hourly history expired MUST return the A17 empty/incomplete result with `unavailable_reason="not_retained"`; absent/failed F uses `file_source_unavailable`, and another unrepresentable allowed scope uses `unsupported_window`. File coverage MUST read its independent hourly/boot sources, so process expiry cannot erase file hooks or gap evidence.

`unknown_owner_file_coverage` MUST return `owner_key="overflow_owner_unknown"`, exact attempts/read/write bytes and coverage for the same window, reading `file_overflow_unknown` or exact `boot_file_overflow_unknown` totals. A monitored zero remainder MUST be returned explicitly as zero; missing evidence MUST be unavailable/incomplete rather than inferred zero. `FileTable` and `FileCoverage` MUST attach this same global window quantity as context, never add it to the selected app's F or app overflow fields, and never multiply it by the number of apps. The Files consumer MUST display `Unknown owner overflow: <R> read bytes, <W> write bytes (<N> failed inserts)` once for the window, alongside per-app overflow. Unknown-owner byte loss MUST remain visible even when per-app counters are zero.

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
| BPF/file-map insert failure (later P4 producer), cgroup uniquely owned | Increment that app's `uncounted_overflow_*` from its per-cgroup counters; do not create a file row | File coverage states the app's unknown-path bytes; F remains separate from T. |
| Overflow cgroup cannot be keyed or uniquely owned | Persist under `overflow_owner_unknown` in hourly and boot unknown-owner tables; do not assign an app ID | Files shows the separate unknown-owner remainder, including when the selected app's overflow is zero. |
| Integer exceeds the declared SQLite range | Roll back the whole observation, persist separate `store_diagnostic.code="counter_overflow"`, raise `CounterOverflowError` | Affected coverage is incomplete with `counter_overflow=True`; no wrapped/saturated value is shown. |
| Disabled launch or over-365-day bounded range | No query-side mutation; raise the exact A22 exception | The requested control is disabled with the exact reason; detail data is unavailable and the scope is never widened. |
| BPF drain/reconnect produces an explicit gap | Preserve `(boot_id, source, epoch, seq)`; insert `gap`; never dedupe on wall time | The affected window is visibly incomplete; no continuity is invented. |
| Database integrity check reports corruption | Quarantine as `history.db.corrupt-<UTC-epoch-ns>[-<suffix>]` by the A18 no-replace procedure; create schema-v1 DB only after quarantine succeeds | Notify that prior history is quarantined and unavailable in the new DB; quarantine failure leaves collection stopped and the source preserved. |
| GUI closes while daemon is active | P1 store does not change ownership; daemon-owned durable rows remain in its store | No history is lost solely because the GUI closes. Daemon mechanics are P3. |
| GUI closes while daemon is off | Persist a `gui_absent` gap when collection resumes; do not synthesize process deltas across the absence | Totals are marked incomplete/lower-bound. |
| Daemon absent | Active collector source is `gui`; P1 stream/gap rules apply | P3/P5 show the absent-daemon banner with `Open settings`; GUI owns collection for its recorded intervals. |
| Daemon enabled but silent | Preserve `enabled, not answering`; P1 MUST NOT manufacture daemon samples | P3/P5 show that exact status rather than pretend data. |
| BPF load failure | Reserve the failure's integer `errno` and verifier-line text; do not fabricate F rows | P4/P5 show errno and verifier line in Files; T and D continue. |
| `pkexec` dismissed/failed | Reserve exact exit code and stderr text; no new collector ownership is claimed | P3/P5 revert the switch and show code/stderr. |
| Delegation installed, activation pending | Reserve `pending_activation`; no successful accounting state is inferred | P3/P5 show the pending banner and re-probe `cgroup.controllers`. |
| Setup helper missing or container host | Reserve the reason text; no setup or collection success is inferred | P3/P5 disable the switch with that reason. |

**A18 — ratified.** Corruption quarantine MUST use the UTC Unix epoch timestamp in integer decimal nanoseconds, with an atomic no-replace rename. A collision MUST try suffixes `-1`, `-2`, … in increasing order for that timestamp; no prior quarantine may be overwritten. Relevant WAL/SHM evidence MUST be preserved with the quarantined DB before creating a replacement. If any required quarantine step fails, startup MUST preserve the source/evidence, report that failure and stop; it MUST NOT recreate over the damaged file.

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

The shared-cgroup case has `cgroup.procs` exactly `420\n421\n`, both PIDs in `app-shared.scope`, and one `io.stat` delta for `259:0` of `rbytes=40`, `wbytes=5`. Both PIDs resolve to the same app. D MUST be stored once as `(259:0,40,5)`, not once per PID. A second shared-cgroup fixture uses cgroup path `/fixture-shared` (no recognized app unit), with PID 420's executable `/usr/bin/tool-a` and PID 421's executable `/usr/bin/tool-b`; these resolve to distinct executable AppKeys. Under A19 — ratified, this second fixture MUST emit no per-app D row, MUST mark both apps' device state incomplete, and MUST NOT duplicate or divide the single `(40,5)` delta.

### 10.3 Aggregator-shim BPF vectors

The shim receives these entries for app cgroup id `42`, device `259:0`, over one completed boottime hour `[0,3600s)`. Drains MUST aggregate before producing its single hourly snapshot (`ts_mono_ns=0`, all supplied LAST values `120_000_000_000`); they are not independent observations:

| Key `(cgroup_id,dev,inode,generation)` | Value `(reads,writes,bytes_r,bytes_w,basename)` |
|---|---|
| `(42,"259:0",1001,7)` | `(3,1,90,40,"data_1")` |
| `(42,"259:0",1001,8)` | `(1,2,10,20,"data_1")` |
| `(42,"259:0",1002,3)` | `(1,1,0,60,"big.iso")` |

Generation 7 and 8 are distinct path identities even though inode and basename match. `fs_uuid` is supplied by the fixture as `1111-aaaa` for all three entries. Additional coverage inputs for the same app/interval are: one counted omitted file `(20 read bytes,30 write bytes)`, one failed insertion `(5 read bytes,7 write bytes)`, `skipped_nonfile=3`, and `skipped_unsupported_fs=1`.

For a synthetic map capacity of three, insert the three entries above, then submit the single failed entry `(42,"259:0",2000,1)` with value `(1,0,5,7,"lost")` while full. It fails insertion once and contributes overflow bytes `(5,7)` to cgroup 42's keyed counter; this is the same failed insertion counted above, not another failure. Independently, the reserved `overflow_owner_unknown` counter reports two failures with read bytes `6+5=11` and write bytes `8+5=13`. These MUST remain separate from cgroup 42's app counters. The app's file equation is unchanged; the all-owner equation adds this remainder once.

A separate hourly aggregation vector uses a synthetic top-1 test limit: in two disjoint authoritative collector intervals of the same app-hour, file A contributes `(bytes_r=60,bytes_w=40,accesses=2)` then `(40,60,3)`, while file B contributes `(150,0,6)` across the hour. After aggregation A is `(100,100,5)` and ranks first at 200 bytes; B's 150 read bytes and six accesses are counted omitted. Exactly one app-hour snapshot and one coverage row MUST result across both collectors. Production uses top-100; only the test capacity is one.

For deterministic top-2 selection, candidates are:

| Candidate identity `(dev,inode,generation)` | bytes_r | bytes_w | reads+writes |
|---|---:|---:|---:|
| `("259:0",200,1)` | 50 | 50 | 5 |
| `("259:1",100,2)` | 70 | 30 | 5 |
| `("259:0",1,1)` | 50 | 49 | 9 |

The top-2 are `("259:0",200,1)` then `("259:1",100,2)`: byte totals tie, access totals tie, then the identity tuple sorts ascending. The third candidate is counted omitted.

For a top-1 cutoff identity tie, use two candidates with the same `(dev="259:0", inode=9, generation=4)`, both `(bytes_r=80,bytes_w=20,reads=3,writes=2)`, but candidate A `(identity_weak=0,fs_uuid="aaaa")` and candidate B `(identity_weak=1,fs_uuid="bbbb")`. A MUST rank first by the full tie key; B is counted omitted. Reversing input order MUST NOT change that result.

For the quota deletion-key collision, use rollup buckets with full keys `("boot-A",app_id=4,bucket_secs=60,bucket_start_mono_ns=0)` and `("boot-A",4,60,60_000_000_000)`. Clock offsets at those starts are respectively `1_000_000_000_000` and `940_000_000_000` ns, so both map to `1_000_000_000_000` ns. All earlier sort components tie; the final full-primary-key suffix chooses `bucket_start_mono_ns=0` first. Swapping insertion order does not change the deletion choice.

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
| Per-app F equality | `F_read=100+20+5=125`; `F_write=120+30+7=157`; `F=282`; retained `220` + counted `50` + app overflow `12` = `282` |
| Unknown-owner overflow | Reserved owner key, `attempts=2`, `bytes_r=6+5=11`, `bytes_w=8+5=13`, total `24`; never included in the selected app's overflow `(1,5,7)` |
| All-owner F equality | `F_all_read=125+11=136`; `F_all_write=157+13=170`; `F_all=136+170=306=282+24`; unknown-owner bytes enter once |
| Hourly snapshot/top-N | Two collectors' disjoint contributions yield one hour: A `(100,100,5)` retained, B `(150,0,6)` omitted under synthetic top-1; `200+150=350` bytes accounted |
| BPF top-2 | first `("259:0",200,1)`, second `("259:1",100,2)`; omitted candidate total `99`, accesses `9` |
| Top-N tie at cutoff | Equal bytes/access/dev/inode/generation candidates strong `("aaaa",identity_weak=0)` and weak `("bbbb",identity_weak=1)` select strong first; weak is omitted regardless of insertion order |
| Quota sort collision | Bucket 0 maps to `0+1_000_000_000_000`; bucket 60s maps to `60_000_000_000+940_000_000_000`; equal wall key is resolved by full rollup PK, deleting bucket 0 first |
| Controller absent | no device rows; `device_state="not_delegated"`; T values unchanged |
| First-seen nonzero identity | With no baseline and input `(70,30,7,3)`, output MUST be that delta once, headline `7+3=10`, `first_seen_partial=1`, `partial_intervals=1`, `delta_kind="baseline"`; A01 — ratified |
| Per-field reset | Prior `(100,20,10,5)`, current `(90,25,8,9)` emits `(0,5,0,4)` with `resets=1`; next `(95,30,9,10)` emits `(5,5,1,1)` with no new reset; A02 — ratified |
| ESRCH mid-scan | One enumerated, already-open identity whose read vanishes increments `resets` exactly once, closes once, and emits no bytes; an unknown identity gives zero resets |
| Multiple attributed timestamps in one frame (B1) | For one `(boot,source,epoch,seq,app,launch)`, a suspend-spanning parent delta `(read=40,write=20)` attributed to `110s` and a newly observed child delta `(read=7,write=3)` attributed to `120s` persist as two rows with distinct `ts_mono_ns`; both retain `observed_mono_ns=120s`. One frame/app `sample_coverage` receipt at `120s` carries `partial_intervals=1`, `spans_suspend_intervals=1` once, and its counters are added only to the 120s coverage bucket, not duplicated into the 110s bucket. Byte totals by attributed bucket are `(40,20)` and `(7,3)` |
| Crash/restart checkpoint (B2) | Commit cumulative baseline/output `100 B`; crash; allocate next epoch and restore baseline `100 B`; next same-process cumulative value `120 B` emits `120-100=20 B`; durable total `100+20=120 B`, never `100+120=220 B`; post-checkpoint time is a gap |
| Peak validity after pruning (B3) | Two otherwise identical app/buckets each total `10 B` with identical coverage: one has a defined interval `10 B / 60 s`, so `floor(1/6)=0` and persisted peak is integer `0`; the other has an unknown denominator, so its persisted peak and query result are NULL/`None`. After raw pruning, `MAX` preserves zero versus NULL, ignores NULL beside a defined value, and remains NULL when every input is undefined |
| Rename/hard-link hourly identity (B5) | Same `(app,fs_uuid="1111-aaaa",dev="259:0",inode=42,generation=7,identity_weak=0)` observed as `a.txt` at `100s` `(reads=2,bytes_r=60)` and `b.txt` at `200s` `(reads=1,writes=2,bytes_w=40)`: two basename-specific path rows are observed; one hourly `file_sample` row points to canonical `a.txt`, totals `(reads=3,writes=2,bytes_r=60,bytes_w=40,total=100)`, LAST `200s`; `path_alias(a_path,"b.txt")` has first/last `200s`. Reversing names/input order retains canonical `a.txt`, one aggregate row, and the same totals/LAST |
| Session-scope identity (B8) | With desktop/exe absent, `/user.slice/session-7.scope/worker` resolves to `scope:/user.slice/session-7.scope`; `/user.slice/user-1000.slice/user@1000.service/app.slice/worker`, null, and relative `session-7.scope/worker` resolve to `scope:/session/unknown` |
| Unobserved reap departure (B10/A03) | An unclassified parent increment of `(0,0,40,12)` with no observed child or explicit reap evidence is emitted normally; `reap_unobserved=(0,0,0,0)` and no delta-size heuristic runs |
| Component and combined peaks (B10/A16) | At `observed_ns=10s`, intervals `(read,write)=(100,0),(0,100)` have component maxima `(10,10)` and combined max `10`; intervals `(100,100),(0,0)` have the same component maxima `(10,10)` but combined max `20`. Persisted `peak_total_bps` distinguishes them |
| Ordinary interval peaks | Persisted 10-second observed intervals give Chromium t1 `floor(180/10)=18` read B/s, `floor(90/10)=9` write B/s and `floor(270/10)=27` total B/s; t2 gives `(2,1,3)`; interval peaks MUST be `(18,9,27)` under ratified A16 |
| SQLite counter overflow | Input `2^63=9223372036854775808` MUST reject/roll back without sequence or baseline changes; diagnostic code `counter_overflow` persists; affected coverage MUST be incomplete |
| Disabled launch | Even a retained known launch MUST raise A22's `launch_attribution` error; its UI control MUST be disabled with the exact reason |
| Tier upper boundary | `365*86400*1000000000=31536000000000000` ns uses 3600s rollups for a range; `31536000000000001` ns MUST raise A22's `unsupported_window` error, never return totals |
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

The clock fixture starts with an initial observation at boottime zero and wall seconds `1000`, with zero counters, before the observations shown. Expected clock map rows are `(segment_start=0,offset=1_000_000_000_000)` and `(segment_start=125_000_000_000,offset=500_000_000_000)`. The bucket sequence is `[60s,120s)`, `[120s,180s)`, `[660s,720s)` for ordinary observations, while the spanning delta is stored in the last pre-suspend observed bucket `[120s,180s)` with `spans_suspend=1`. No row is assigned by wall time. Under A09 — ratified, the spanning interval MUST store `observed_mono_ns=661_000_000_000`, `interval_start_mono_ns=179_000_000_000`, and attributed `ts_mono_ns=179_000_000_000`. Its elapsed time is `661-179=482` s; measured suspend is `660-180=480` s; persisted `observed_ns=(482-480)*1000000000=2000000000`. A delta `(read=40,write=20)` therefore has exact rates `(20,10)` B/s, not `(floor(40/482),floor(20/482))=(0,0)`. The actual observation falls in `[660s,720s)` but its delta is assigned only to `[120s,180s)` for the 60s bucket test. Repeated/overlapping suspend boundaries MUST be unioned, not subtracted twice.

A cross-boot vector uses boot A order 1 with LAST `900_000_000_000` and boot B order 2 with LAST `10_000_000_000`. B MUST sort later despite its smaller boottime. Both boots may have a `clock_map` segment at zero without collision. A path/alias first endpoint MUST remain `(A,900_000_000_000)`, its last endpoint MUST become `(B,10_000_000_000)`, and lifetime `updated_boot_id` MUST accompany B's timestamp.

## 11. Property-Based Tests

All property tests use deterministic seeds recorded with failures. Generate 0–128 observations, counters/counts in `[0, 2^63-1]`, canonical AppKeys under the chosen A07 policy, devices `f"{major}:{minor}"` with each component in `[0, 2^20-1]`, inode values in `[0, 2^63-1]`, generations in `[0, 2^32-1]`, and nonnegative boottimes no greater than `2^63-1`. Coverage generators use the exact section 6.1 shape. Mathematical intermediate sums may use signed 128-bit test arithmetic; sums outside the storage range MUST exercise the overflow path, not a successful insert. The production oracle uses arbitrary-precision integers. Shrink by deleting unrelated apps/devices/files first, then timestamps, then reducing to the minimum two samples/two identities needed to preserve the failing condition.

1. **Device reconciliation:** For generated windows with complete D, assert `sum(dev.total_bytes) + residual_total == task_total_bytes`; independently, `sum(dev.rbytes) + residual_read == task_read_bytes` and `sum(dev.wbytes) + residual_write == task_write_bytes`. Include positive, zero, and negative residuals. For incomplete/unavailable D, assert all aggregate D and residual fields are `None`, do not perform arithmetic, and retain numeric T. Generate this branch as well as the complete branch.
2. **File byte equality:** Per app and read/write independently, `sum(file rows)+counted omissions+app overflow==app F`. Across owners, `sum(app F)+overflow_owner_unknown==all-owner F`. Unknown-owner loss MUST enter once, never be apportioned to apps; skipped objects enter neither equation.
3. **Coverage equality and overlays:** First assert each persisted field equals the exact deduplicated source-row reduction (sum components/counts, OR booleans, exact device-state reduction). Then independently apply intersecting A21 diagnostic overlays (`counter_overflow=True`, incomplete even if the rejected frame has no source row) and query-derived `tier`, `complete`, and `unavailable_reason` from clipping, pruning, gaps, pending tails and source availability. Generate complete and incomplete D, missing and retained buckets, boundary clipping, an affecting diagnostic with no raw row, and allowed-but-unrepresentable file windows. Never compare query-derived fields to the persisted-row reduction alone. Counted omission and overflow bytes remain independent through rollup and totals.
4. **Monotonic bucket partition:** Generate nonnegative integer `ts_mono_ns` values and positive `bucket_ns`; for every timestamp, exactly one bucket index is `floor(ts_mono_ns / bucket_ns)`, and the mathematical half-open intervals for consecutive indices touch without gaps or overlap. Observed samples may skip bucket indices, which does not imply coverage. Generate positive and negative NTP offset steps and suspend intervals crossing zero, one, or multiple bucket boundaries. Clock steps MUST NOT change bucket keys. Suspend-spanning deltas are placed in the last pre-suspend observed bucket and marked once; no byte delta is duplicated or omitted by partitioning.
5. **Counter reset:** A decreasing counter emits no negative delta, resets the selected baseline, and the next nondecreasing value is measured from the reset baseline under ratified A02's per-counter rule.
6. **PID reuse:** Replacing `(boot,pid,start_ticks)` with a new start time never transfers the old baseline or reap credit.
7. **Credit conservation:** For each counter, emitted parent delta plus the applied credit equals the raw parent delta; leftover credit equals the stored reap-unapplied amount if the parent closes. No adjusted delta is negative.
8. **Hourly top-N determinism:** Permuting BPF entries or partitioning the same authoritative app-hour across drains/collectors MUST leave one identical snapshot, selected rows and omissions. Aggregate by `(app_key,fs_uuid,dev,inode,generation,identity_weak)` before selecting top-100 by bytes/accesses and ascending full identity tie key `(dev,inode,generation,identity_weak,fs_uuid,basename,path_id)`; no source gains another hourly budget. Generate strong/weak candidates tied on the old key at the cutoff. Each hour's unknown-owner remainder MUST also remain invariant.
9. **Tier boundary and disabled scope:** An allowed range of exactly 24h uses raw; one nanosecond over uses 60s; exactly 30d uses 60s; one nanosecond over uses 3600s; exactly 365d uses 3600s. A range one nanosecond over 365d MUST raise the exact A22 error, and every launch request MUST raise its exact A22 error even with retained raw rows. Only explicit exact full-boot/lifetime scopes may use totals-only. No query blends tiers or broadens a window.
10. **Stream dedupe/ownership:** Duplicate or stale `(boot_id,source,epoch,seq)` frames are rejected before baseline changes and have no effect on any rows/totals; a new frame is applied once. Disjoint half-open collector intervals have no overlap; uncertain overlap contributes zero bytes from both owners in the overlap and increments coverage once per interval. Generate integer interval endpoints with `start < end`, collector ownership labels, and duplicate/stale sequence keys; shrink to the smallest two overlapping intervals or duplicate frames that preserve the failure.
11. **ESRCH accounting:** Generate an open or unknown `(boot,pid,start_ticks)` identity and one `ESRCH` scan outcome. An already-open identity increments `resets` by exactly one and is closed once; an unknown identity increments it by zero. Neither case emits a negative delta or adds a reap credit without a readable baseline. Shrink by removing other identities and reducing the scan to this single outcome.
12. **Retention independence and overflow:** Generate ages on both sides of 24 hours and 14 days, file/core units with the same stream key, and nonzero hook counters. Core expiry MUST leave eligible file rows and hook counters byte-for-byte equal until file expiry. Separately generate a counter or sum in `[2^63, 2^64-1]`; it MUST NOT commit as a wrapped integer, REAL, or saturated counter. Shrink age toward each boundary and overflow toward `2^63`; the entire observation MUST roll back and a representable A21 diagnostic MUST survive with incomplete coverage.
13. **Multi-timestamp frame:** Generate one frame/app/launch with contributors assigned to at least two attributed timestamps. Distinct sample rows MUST coexist under the extended PK, while exactly one frame-level coverage receipt/counter set is assigned to the observed timestamp. Shrink to two identities and two timestamps.
14. **Crash checkpoint:** Commit a cumulative baseline and its output, simulate a process crash before the next frame, allocate a new epoch, restore same-boot identity/baseline/credit/device state, and feed the next cumulative value. Assert only the difference is emitted, replay is rejected by the new epoch receipt rules, and checkpoint/receipt/output commit atomically. Include commit-before-crash and crash-before-commit branches.
15. **Peak validity/reduction:** Generate all-undefined intervals, defined zero-rate intervals, and mixed defined/undefined intervals. Persist NULL for no defined rate and integer 0 for a defined floored-zero rate; reduce by nullable MAX, preserve the distinction after pruning, and compute combined maxima independently of component maxima.
16. **Complete deletion-key ordering:** Generate distinct deletion units that tie in mapped wall time and every preceding sort field. Assert the final comparison is their full typed primary key; include two rollups at 0s/60s with offsets 1000s/940s and the same app/tier.
17. **Session-scope selection:** Generate cgroup paths with multiple session-scope ancestors, unrelated `.scope` leaves, absent paths, and malformed relative paths. The deepest matching `session-*.scope` ancestor prefix wins; otherwise use exactly `scope:/session/unknown` when higher-precedence identity inputs are absent.
18. **Canonical durable JSON:** Generate valid and malformed handover/checkpoint/hour-state objects. Require exact keys/types, no duplicate identities/keys, ordered arrays, and canonical bytes. In particular `{}` and `apps:{}` MUST fail; a valid pending state has array `apps` and an explicit object `unknown_owner` even at zero.

## 12. Ambiguous in the Design — Ratified Decisions

All 22 revision-1 items are ratified. This ledger preserves their IDs and the binding outcome; A11/A22 use the operator's rulings, and the other 20 preserve the recommendations' substance. Conservative unavailability/incompleteness is an explicit outcome, not an unresolved decision.

| ID/status | Normative decision and contract |
|---|---|
| A01 — ratified | First readable counters MUST emit once with `first_seen_partial` for nonzero values; an incoming baseline MUST prevent pre-cutoff re-emission (§5.1). |
| A02 — ratified | Only decreasing fields MUST reset; any decrease counts once per identity record, and an open-identity ESRCH counts once (§5.1). |
| A03 — ratified | P1 MUST NOT infer unseen reaps from delta size; unsupported `reap_unobserved` components MUST remain zero (§5.1). |
| A04 — ratified | Credits MUST retain the last observed parent identity and FIFO disappearance/child order, counting a correction once. Unproved nested/reset conservation MUST persist `reap_incomplete` and MUST NOT claim exactness (§5.1). |
| A05 — ratified | D baselines MUST use boot/cgroup/device, establish zero-delta initial/recreation baselines, and clamp/reset decreases (§5.2). |
| A06 — ratified | One launch MUST group an app root and same-AppKey descendants, closing only after all known members disappear; concurrent roots MUST remain separate (§3.3). |
| A07 — ratified | AppKeys MUST preserve unit/desktop/executable spelling and use the specified UTF-8 percent encoding and deterministic precedence (§3.2). |
| A08 — ratified | Each process start MUST allocate an epoch/reset high water transactionally; receipts MUST commit before publication; crashes MUST leave explicit gaps. Baseline interchange MUST be versioned/validated (§3.4). |
| A09 — ratified | Suspend detection MUST use explicit boundaries; actual/attributed timestamps and measured observed durations MUST persist, excluding only measured suspended/uncovered time (§4.1). |
| A10 — ratified | Queries MUST include only fully contained buckets, MUST mark clipped edges incomplete, and MUST NOT prorate bytes (§4.2). |
| A11 — ratified, operator | File observations MUST be HOURLY after app-hour aggregation: one snapshot, top 100 files, independent 14-day retention. `107*100=10700` rows/snapshot (~1 MB), `*24=256800` rows/day (~22.6 MB/day), `*14=3595200` rows (~316 MB payload). Other bounded file windows MUST NOT borrow boot totals (§4.2, §7). |
| A12 — ratified | Coverage units/operators MUST follow §4.3, and raw-window evidence MUST persist in per-tick `sample_coverage`, including zero-byte ticks. |
| A13 — ratified | The physical quota MUST be decimal 1,500,000,000 bytes across DB/WAL/SHM; deterministic mapped-age pruning/shrink/pause/resume MUST follow §5.3. |
| A14 — ratified | Coverage JSON MUST use the exact §6.1 shape, component-wise sums and OR flags, with independent hourly file coverage and file totals rather than a finer fictional file tier. |
| A15 — ratified | The exact expanded schema and indexes MUST be remeasured before shipping; planning payload estimates MUST NOT override the hard quota (§7.1). |
| A16 — ratified | Defined interval rates MUST use integer floor B/s and bucket/window maxima, preserving exact bytes; display rounding SHOULD use nearest unit, ties upward (§5.4, §8.1). |
| A17 — ratified | File search MUST use NFC/casefold substring matching, the caller MUST pass direction, and default presentation MUST be traffic descending; unsupported allowed file windows MUST return empty/incomplete (§8.2–8.3). |
| A18 — ratified | Corrupt DBs MUST use atomic UTC-nanosecond no-replace quarantine with increasing collision suffixes; failure MUST preserve evidence and stop rather than overwrite (§9). |
| A19 — ratified | Distinct AppKeys sharing a cgroup MUST NOT duplicate/divide D. With no approved unique owner, affected device attribution MUST be incomplete (§5.2). |
| A20 — ratified | Every monotonic value MUST retain boot identity; persisted boot chronology MUST order endpoints/LAST, observed aliases MUST remain faithful, and weak/strong identities MUST stay separate (§3.5, §6.2). |
| A21 — ratified | An out-of-range counter/aggregate MUST roll back, MUST persist a representable separate overflow diagnostic, and MUST mark coverage incomplete without wrapping, saturation or REAL coercion (§5.4). |
| A22 — ratified, operator | Every launch window and every bounded range above 365 days MUST be disabled with §8.3's exact store exception and consumer state. No wider window may substitute. Exact explicit boot/lifetime totals remain distinct supported scopes. |

## 13. Design Contradictions and Specification Deltas — Resolution Against r4

| Revision-1 contradiction | How design r4 resolved it | What specification revision 3 now requires |
|---|---|---|
| Revision metadata disagreed with the approved body | Header explicitly says revision 4 and supplies r1–r4 revision history | §1 names r4 as authoritative and records spec r1–r3 history; no stale revision-2/3 source-status claim remains. |
| File-sample storage arithmetic | §6 gives `107*24*100=256800` rows/day, 22.6 MB/day and 316 MB/14 days | §7 shows exact hourly/snapshot/day/14-day products: 3,595,200 retained rows and 316,377,600 payload bytes; exact-schema remeasurement and physical quota remain mandatory. |
| Global overflow could not produce per-app loss | §4.1 now keys the overflow map by cgroup and reserves `overflow_owner_unknown` when keying fails | §2/§5 separate attributable app overflow from unknown-owner loss; STRICT hourly/boot remainder tables, a first-class query result and UI disclosure preserve both without apportioning loss. |
| Monotonic values lacked boot scope | §2.7/§6 key clock segments by `(boot_id,segment_start_mono_ns)` and require boot identity beside every monotonic value, including path/alias/lifetime endpoints | DDL pairs path/alias endpoints with their boots and lifetime update time with `updated_boot_id`; §3.5 uses persisted boot chronology for cross-boot ordering. |
| Raw-window coverage had no durable source | §6 adds `sample_coverage` and says it cannot be reconstructed later | §4.3/§6 adopt a typed superset using exact coverage JSON and per-app tick timestamps, preserving zero-T intervals, full reap components and flags; file hours retain independent coverage. |
| Promised launch/long-range scopes exceeded stored dimensions | §6 narrows promises and requires a disabled chip with a reason; its residual raw-launch/totals wording still conflicts with unconditional disablement (below) | Operator A22 controls §4.2/§8.3: every launch and every bounded range above 365 days raises an exact typed error and renders disabled/unavailable, never a substitute scope. File totals now store per-boot LAST. |

**Remaining source contradiction: 1 (window promises).** R4's decision table still says launch is selectable, and §6 says launch is “offered only while raw rows … are retained” and `>365 d` “returns totals only,” while the same paragraph says both are disabled. The operator's A22 ruling is unambiguous and overrides those sentences for this specification: launch is always disabled, and a bounded range above 365 days errors. Only a separately requested exact boot/lifetime scope can return totals. This source-text conflict remains recorded rather than claiming r4 is internally consistent; no new product decision is needed and the design file is unchanged.

All six original contradictions have concrete specification outcomes. The five other source contradictions are resolved by r4; the remaining source conflict above has an explicit operator-precedence resolution in the spec. No unratified design decisions remain.

**Specification departures from design r4 (design unchanged):**

| Decision | Design r4 behavior | This specification's binding behavior | Reason and persisted consequence |
|---|---|---|---|
| A03 — unseen reap classification | A parent counter jump without a sampled child is classified as `reap_unobserved` and counted as unknown. | P1 keeps all four `reap_unobserved` components zero unless independent explicit evidence classifies the increment; a large delta alone is emitted as ordinary parent I/O (§5.1). | Polling only exposes cumulative counters, so jump magnitude cannot distinguish an unseen reaped child from legitimate parent I/O. Guessing would subtract or relabel valid bytes. The field remains in typed coverage for compatibility, but P1 writes zero under this conservative contract; later producers need an explicit evidence source before nonzero values are meaningful. |
| A16 — PEAK definition and validity | PEAK is described as the maximum `(read_bytes + write_bytes) / bucket_secs` over retained buckets, with separate read/write bucket peaks persisted. | P1 computes read, write, and combined rates from each same positive measured interval, floors each using its actual `observed_ns`, and takes maxima over those interval rates (§5.4/§8). It persists `peak_total_bps` as well as nullable component peaks. | Separate component maxima cannot reconstruct combined PEAK when read and write maxima occur in different intervals; dividing totals by a wider bucket can also fabricate a rate. The explicit combined field preserves the requested total metric across pruning. Nullable fields distinguish “no defined interval” from a measured rate that floors to zero. DDL consumers/migrations and schema-size measurement must include `peak_total_bps` and nullable peak columns; totals-only PEAK remains unavailable. |

The B1–B10 repairs also add the extended attributed-timestamp sample key, durable `sampler_checkpoint`, exact version-1 checkpoint/handover/hour-state JSON contracts, and deterministic file aggregation/tie/deletion keys. These are normative schema and implementation deltas, not edits to design r4. In particular, the combined nullable peak fields, checkpoint table/state, and larger file-hour accumulator affect migration compatibility, validation, and the A15 physical-size remeasurement. The design document remains byte-for-byte untouched.

## 14. P2 Can Start Immediately

P2 can implement the pure `/proc` parser, identity validator, boottime buckets, signed reconciliation, reviewed schema-v1 creation, indexes, stream deduplication and controller-absent path against these ratified contracts. First-seen/reset/launch bookkeeping, shared-cgroup degradation, clipped edges, file-hour semantics and disabled-window errors MUST follow the rules above; no provisional policy seam or further ratification is required. P2 MUST preserve the stored coverage and typed failures while building the since-boot vertical slice. Daemon/BPF/UI mechanics remain in their agreed later plans; their consumers MUST preserve the values and disabled states specified here.

Unratified items remaining: **0**. Remaining design-source contradictions: **1**, recorded in section 13 and governed by the explicit A22 operator ruling.
