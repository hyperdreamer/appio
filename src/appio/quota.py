"""Mapped-wall retention, indivisible deletion units, and a physical hard quota.

Rollups/totals are prerequisites, never rebuilt with additive UPSERTs by pruning.
Missing clock evidence stops a pass without guessing. Unmaterialized units are
reported and skipped so independent ready units can still be deleted. Results
account separately for measured rows, counted omissions and failed insert attempts.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from heapq import nsmallest
import heapq
import os
from pathlib import Path
import sqlite3

from appio.contracts import FILE_HOUR_NS, SQLITE_INT_MAX, validate_sqlite_integer
from appio.durable import canonical_json, decode_file_hour_state
from appio.file_store import _decode_coverage
from appio.files import checked_sum
from appio.ownership import ownership_settled

DAY_NS = 86_400_000_000_000
DEFAULT_QUOTA_BYTES = 1_500_000_000
#: Units retired per maintenance pass. Two constraints set this: the pass blocks
#: the owner loop, so it must stay under FRAME_TIMEOUT_NS (2 s), and it must
#: retire more than the collector produces or the retained tail grows forever.
#: A 10 s sample of ~226 app rows produces ~1350 units/minute. Measured on the
#: host with the heap-merged selection: 256 -> 0.14 s, 2048 -> 0.82 s,
#: 4096 -> 4.2 s (superlinear), so 2048 is the largest pass that keeps headroom
#: under the 2 s deadline and still outruns production 1.5x.
DEFAULT_BATCH_SIZE = 2048
#: Units retired per transaction: the whole pass in one transaction. Measured on a
#: real copy of the 1.45 GB store, deleting 2048 units cost 25.9 KiB/unit one
#: transaction per 128 with a checkpoint after each, 9.9 KiB/unit at 512, and
#: 5.6 KiB/unit as a single transaction with one checkpoint. Each checkpoint
#: writes back every page the preceding chunk dirtied, so a per-chunk checkpoint
#: rewrote the same pages sixteen times. A bigger dirty set is exactly what the
#: page cache is for; nothing needs to reach disk until the pass ends.
DELETE_UNITS_PER_TRANSACTION = 2048
#: Headroom a rewrite must leave under the ceiling. Compacting to the last free
#: page puts the store straight back over the quota, so it rewrites itself again
#: on the next pass: a store parked at its ceiling then spends its life copying
#: 1.4 GB to bank 200 KB (on the author's host: 4.6 TB of writes in 37 hours).
COMPACT_RESERVE_BYTES = 32 * 1024 * 1024
COMPACT_RESERVE_FRACTION = (1, 20)  # 5 % of the database file
#: Deployment override for the ratified ceiling; only a host that keeps more
#: history than 1.5 GB can hold has any reason to set it.
QUOTA_ENV = "APPIO_QUOTA_BYTES"
RETENTION_NS = {"core": DAY_NS, "gap": DAY_NS, "file": 14 * DAY_NS,
                "file_unknown": 14 * DAY_NS, 60: 30 * DAY_NS, 3600: 365 * DAY_NS}
#: (kind, bucket, table, index, time column, index order) per deletion tier. The
#: order is a prefix of the A13 key inside one app, so a per-app keyset scan feeds
#: a heap merge directly.
_TIERS = (
    ("core", 0, "sample_coverage", "sample_coverage_app_time_idx", "ts_mono_ns",
     "ts_mono_ns,source,epoch,seq"),
    ("file", 0, "file_coverage", "file_coverage_app_time_idx", "ts_mono_ns", "ts_mono_ns"),
    ("file_unknown", 0, "file_overflow_unknown", None, "ts_mono_ns", "ts_mono_ns"),
    ("rollup", 60, "rollup_app", "rollup_app_time_idx", "bucket_start_mono_ns",
     "bucket_start_mono_ns"),
    ("rollup", 3600, "rollup_app", "rollup_app_time_idx", "bucket_start_mono_ns",
     "bucket_start_mono_ns"),
)


def _paths(paths) -> tuple[Path, ...]:
    if isinstance(paths, (str, Path)):
        paths = (paths,)
    return tuple(dict.fromkeys(Path(path) for path in paths))


def configured_quota(environ=None) -> int:
    """The ratified ceiling, unless this deployment overrides it explicitly.

    A malformed override must not take the collector down: the default is the
    documented contract, and a typo in a unit file is not a reason to stop
    measuring. Callers that want strictness pass an explicit mapping.
    """
    raw = (os.environ if environ is None else environ).get(QUOTA_ENV)
    if raw is None or raw == "":
        return DEFAULT_QUOTA_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_QUOTA_BYTES
    return value if value > 0 else DEFAULT_QUOTA_BYTES


def physical_store_bytes(database_paths) -> int:
    """Sum actual st_size for databases and sidecars, counting each path once."""
    paths = set()
    for main in _paths(database_paths):
        paths.update((main, Path(str(main) + "-wal"), Path(str(main) + "-shm")))
    total = 0
    for path in sorted(paths):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


@dataclass(frozen=True)
class DroppedUnit:
    kind: str
    primary_key: tuple
    retained_rows: int = 0
    bytes_r: int = 0
    bytes_w: int = 0
    counted_omitted_files: int = 0
    counted_omitted_bytes_r: int = 0
    counted_omitted_bytes_w: int = 0
    uncounted_overflow_files: int = 0
    uncounted_overflow_bytes_r: int = 0
    uncounted_overflow_bytes_w: int = 0
    unknown_overflow_files: int = 0
    unknown_overflow_bytes_r: int = 0
    unknown_overflow_bytes_w: int = 0


@dataclass(frozen=True)
class SkippedUnit:
    kind: str
    primary_key: tuple
    reason: str = "missing_materialization"


@dataclass(frozen=True)
class PruneResult:
    deleted: tuple[DroppedUnit, ...]
    complete: bool
    sampling_paused: bool
    physical_bytes: int
    reason: str | None = None
    skipped: tuple[SkippedUnit, ...] = ()


@dataclass(frozen=True)
class _Unit:
    kind: str
    primary_key: tuple
    boot: str
    mono: int
    source: str = ""
    epoch: int = 0
    seq: int = 0
    app_id: int = 0
    bucket_secs: int = 0
    gap_id: int = 0
    wall: int = 0

    @property
    def tier(self):
        return {0: 0, 60: 1, 3600: 2}[self.bucket_secs]

    @property
    def key(self):
        typed_pk = tuple(value.encode("utf-8") if isinstance(value, str) else value
                         for value in self.primary_key)
        return (self.tier, self.wall, self.boot.encode("utf-8"), self.source.encode("utf-8"),
                self.epoch, self.seq, self.app_id,
                ("core", "file", "file_unknown", "gap", "rollup").index(self.kind),
                self.bucket_secs, self.gap_id, typed_pk)


@dataclass
class _Cursor:
    """One ordered (tier, app, boot, clock segment) scan for the candidate merge.

    Within a cursor, rows arrive in ``_Unit.key`` order, so a heap merge over the
    cursors reproduces the A13 order one row at a time. The alternative -- asking
    every app for ``batch_size`` rows per tier and merging in Python -- reads
    O(apps x batch) rows to keep ``batch`` of them and cost ~2.7 ms per candidate
    on a 500-app host, which is why retention could never keep up.
    """
    kind: str
    secs: int
    app: int
    boot: str
    low: int
    high: int
    offset: int
    table: str
    index: str | None
    time: str
    order: str
    seek_sql: str = ""
    seek_params: tuple = ()


class QuotaPruner:
    def __init__(self, connection: sqlite3.Connection, database_paths,
                 quota_bytes: int = DEFAULT_QUOTA_BYTES, *,
                 batch_size: int = DEFAULT_BATCH_SIZE):
        validate_sqlite_integer(quota_bytes, "quota_bytes")
        validate_sqlite_integer(batch_size, "batch_size")
        if batch_size == 0:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.connection = connection
        # The connected database is always included even if callers omit its name.
        connected = [row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main" and row[2]]
        self.database_paths = _paths((*_paths(database_paths), *connected))
        self.quota_bytes = quota_bytes
        self.sampling_paused = False
        self.coverage_complete = True
        self.last_reason: str | None = None
        self._expiry_after = None
        self._quota_after = None
        self._gap_scans = {}
        self._gc_after = 0

    def _require_idle(self):
        if self.connection.in_transaction:
            raise ValueError("pruning requires a connection without an active transaction")

    def _map(self, boot, mono):
        row = self.connection.execute("SELECT wall_offset_ns FROM clock_map WHERE boot_id=? AND segment_start_mono_ns<=? ORDER BY segment_start_mono_ns DESC LIMIT 1", (boot, mono)).fetchone()
        return None if row is None else mono + row[0]

    def _clock_ranges(self):
        """Small metadata, not observation history. Offsets may step backwards."""
        ranges = {}
        for (boot,) in self.connection.execute("SELECT boot_id FROM boot ORDER BY boot_id"):
            segments = self.connection.execute(
                "SELECT segment_start_mono_ns,wall_offset_ns FROM clock_map "
                "WHERE boot_id=? ORDER BY segment_start_mono_ns", (boot,),
            ).fetchall()
            ranges[boot] = [(start, segments[i + 1][0] - 1 if i + 1 < len(segments)
                             else SQLITE_INT_MAX, offset)
                            for i, (start, offset) in enumerate(segments)]
        return ranges

    def _unmapped(self, ranges, apps):
        # Preserve the global missing-clock fail-closed contract using index
        # existence probes, including retained units. Gaps are checked in their
        # bounded interval scan below: their index leads with *start*, not end.
        for boot, segments in ranges.items():
            first = segments[0][0] if segments else SQLITE_INT_MAX + 1
            if first == 0:
                continue
            ceiling = min(first - 1, SQLITE_INT_MAX)
            for app in apps:
                for table, index, time, extra in (
                    ("sample_coverage", "sample_coverage_app_time_idx", "ts_mono_ns", ""),
                    ("file_coverage", "file_coverage_app_time_idx", "ts_mono_ns", ""),
                    ("rollup_app", "rollup_app_time_idx", "bucket_start_mono_ns", " AND bucket_secs=?"),
                ):
                    for suffix in ((60,), (3600,)) if extra else ((),):
                        if self.connection.execute(
                            f"SELECT 1 FROM {table} INDEXED BY {index} WHERE app_id=? AND boot_id=?"
                            f"{extra} AND {time}<=? LIMIT 1", (app, boot, *suffix, ceiling),
                        ).fetchone():
                            return True
            if self.connection.execute(
                "SELECT 1 FROM file_overflow_unknown WHERE boot_id=? AND ts_mono_ns<=? LIMIT 1",
                (boot, ceiling),
            ).fetchone():
                return True
        return False

    def _gap_candidates(self, ranges, now_wall, after):
        """Bounded keyset scan where the existing gap index cannot order by end.

        from < to is a conservative indexed eligibility bound. Keep only the
        oldest batch (+ one lookahead), and publish it once the interval scan is
        complete. Until then no unit is deleted: guessing the earliest end could
        violate A13. Large gap histories advance over several ticks, never one
        unbounded SQL sort. No observation-sized cache or schema change.
        """
        mode = now_wall is None
        scan = self._gap_scans.get(mode)
        if scan is None or scan["after"] != after:
            bounds = {}
            for boot, segments in ranges.items():
                bounds[boot] = max((min(end, now_wall - RETENTION_NS["gap"] - offset - 1)
                                    if now_wall is not None else end
                                    for _, end, offset in segments), default=SQLITE_INT_MAX)
                # An unmapped endpoint must be found even outside retention.
                if segments:
                    bounds[boot] = max(bounds[boot], segments[0][0] - 1)
            scan = {"after": after, "wall": now_wall, "bounds": bounds,
                    "cursors": {}, "done": set(), "units": []}
            self._gap_scans[mode] = scan
        self._selection_wall = (min(now_wall, scan["wall"])
                                if now_wall is not None else None)
        for boot, ceiling in scan["bounds"].items():
            if boot in scan["done"]:
                continue
            params = [boot, max(-1, min(SQLITE_INT_MAX, ceiling))]
            seek = ""
            if boot in scan["cursors"]:
                seek = " AND (from_mono_ns,to_mono_ns,source,epoch,gap_id)>(?,?,?,?,?)"
                params.extend(scan["cursors"][boot])
            rows = self.connection.execute(
                "SELECT from_mono_ns,to_mono_ns,source,epoch,gap_id,seq FROM gap "
                "INDEXED BY gap_time_idx WHERE boot_id=? AND from_mono_ns<=?" + seek +
                " ORDER BY from_mono_ns,to_mono_ns,source,epoch,gap_id LIMIT ?",
                (*params, self.batch_size + 1),
            ).fetchall()
            for beginning, mono, source, epoch, gap_id, seq in rows[:self.batch_size]:
                wall = self._map(boot, mono)
                if wall is None:
                    self._gap_scans.pop(mode, None)
                    return None
                unit = _Unit("gap", (gap_id,), boot, mono, source, epoch, seq,
                             gap_id=gap_id, wall=wall)
                if ((after is None or unit.key > after) and
                    (self._selection_wall is None or wall < self._selection_wall - RETENTION_NS["gap"])):
                    scan["units"].append(unit)
            scan["units"] = nsmallest(self.batch_size + 1, scan["units"], key=lambda u: u.key)
            if len(rows) > self.batch_size:
                scan["cursors"][boot] = rows[self.batch_size - 1][:5]
            else:
                scan["done"].add(boot)
        self._scan_pending = len(scan["done"]) != len(scan["bounds"])
        if self._scan_pending:
            return []
        self._gap_scans.pop(mode, None)
        return [u for u in scan["units"] if self._selection_wall is None or
                u.wall < self._selection_wall - RETENTION_NS["gap"]]

    def _candidates(self, now_wall=None, after=None) -> list[_Unit] | None:
        """Merge ordered index scans, one row per cursor, restricted per tier.

        Every tier is scanned through the covering index that already orders rows
        by ``(time, source, epoch, seq)`` inside one app, which is a prefix of the
        A13 key, so a heap over per-app cursors yields the global key order while
        touching only ``batch_size`` rows in total. The final order and the
        resume key are therefore unchanged; only the row volume is.
        """
        self._scan_pending = False
        self._candidate_more = False
        ranges = self._clock_ranges()
        apps = [row[0] for row in self.connection.execute("SELECT app_id FROM app ORDER BY app_id")]
        if self._unmapped(ranges, apps):
            return None
        gaps = self._gap_candidates(ranges, now_wall, after)
        if gaps is None or self._scan_pending:
            return gaps
        now_wall = self._selection_wall
        return self._merge(self._cursors(ranges, now_wall, after, apps), gaps, after)

    def _cursors(self, ranges, now_wall, after, apps):
        """One cursor per boot, clock segment, tier and app, already bounded."""
        cursors = []
        for boot, segments in ranges.items():
            for start, end, offset in segments:
                for kind, secs, table, index, time, order in _TIERS:
                    tier = {0: 0, 60: 1, 3600: 2}[secs]
                    if after is not None and tier < after[0]:
                        continue
                    low, high = start, end
                    if now_wall is not None:
                        high = min(high, now_wall - RETENTION_NS[secs or kind] - offset - 1)
                    same_tier = after is not None and tier == after[0]
                    if same_tier:
                        low = max(low, after[1] - offset)
                    if low > high:
                        continue
                    pivot = after[1] - offset if same_tier else None
                    for app in (apps if kind != "file_unknown" else (0,)):
                        seek_sql, seek_params = "", ()
                        if kind == "core" and pivot is not None and pivot >= start:
                            # Seek past tied ticks in the index itself instead of
                            # rereading an arbitrarily large equal-time prefix. A
                            # later app_id at the same tick is still eligible, so
                            # the comparison is inclusive for those.
                            if boot.encode() == after[2]:
                                op = ">=" if app > after[6] else ">"
                                seek_sql = f"({time},source,epoch,seq){op}(?,?,?,?)"
                                seek_params = (pivot, after[3].decode(), after[4], after[5])
                            elif boot.encode() < after[2]:
                                seek_sql = f"{time}>?"
                                seek_params = (pivot,)
                        cursors.append(_Cursor(kind, secs, app, boot, low, high, offset,
                                               table, index, time, order, seek_sql, seek_params))
        return cursors

    def _cursor_units(self, cursor, limit):
        # A row-value seek says exactly where to start, and SQLite will not use it
        # as a seek while a separate range constraint leads the same index column;
        # with both, the tuple became a per-row filter and each advance rescanned
        # the equal-time prefix (~1400 VM steps per unit instead of a seek).
        tuple_seek = cursor.seek_sql.startswith("(")
        params = [cursor.boot]
        condition = "boot_id=?"
        if cursor.kind != "file_unknown":
            condition += " AND app_id=?"
            params.append(cursor.app)
        if cursor.secs:
            condition += " AND bucket_secs=?"
            params.append(cursor.secs)
        if not tuple_seek:
            condition += f" AND {cursor.time}>=?"
            params.append(cursor.low)
        condition += f" AND {cursor.time}<=?"
        params.append(cursor.high)
        if cursor.seek_sql:
            condition += " AND " + cursor.seek_sql
            params.extend(cursor.seek_params)
        columns = (f"{cursor.time},source,epoch,seq" if not cursor.secs
                   else f"{cursor.time},'',0,0")
        indexed = f" INDEXED BY {cursor.index}" if cursor.index else ""
        units = []
        for mono, source, epoch, seq in self.connection.execute(
            f"SELECT {columns} FROM {cursor.table}{indexed} WHERE {condition} "
            f"ORDER BY {cursor.order} LIMIT ?", (*params, limit),
        ):
            pk = ((cursor.boot, source, epoch, seq, cursor.app) if cursor.kind == "core" else
                  (cursor.boot, cursor.app, mono) if cursor.kind == "file" else
                  (cursor.boot, mono) if cursor.kind == "file_unknown" else
                  (cursor.boot, cursor.app, cursor.secs, mono))
            units.append(_Unit(cursor.kind, pk, cursor.boot, mono, source, epoch, seq, cursor.app,
                               bucket_secs=cursor.secs, wall=mono + cursor.offset))
        return units

    @staticmethod
    def _advance_cursor(cursor, unit):
        """Position a cursor strictly past the unit just merged from it."""
        if cursor.secs or cursor.kind == "file_unknown":
            cursor.seek_sql, cursor.seek_params = f"{cursor.time}>?", (unit.mono,)
        else:
            cursor.seek_sql = f"({cursor.time},source,epoch,seq)>(?,?,?,?)"
            cursor.seek_params = (unit.mono, unit.source, unit.epoch, unit.seq)

    def _merge(self, cursors, gaps, after):
        """Heap-merge the cursors with the gap tier into the next A13 batch."""
        frontier = [(unit.key, len(cursors), unit) for unit in gaps]
        heapq.heapify(frontier)
        for index, cursor in enumerate(cursors):
            first = self._cursor_units(cursor, 1)
            if first:
                heapq.heappush(frontier, (first[0].key, index, first[0]))
        merged = []
        while frontier and len(merged) <= self.batch_size:
            _, index, unit = heapq.heappop(frontier)
            if after is None or unit.key > after:
                merged.append(unit)
            if index < len(cursors):
                cursor = cursors[index]
                self._advance_cursor(cursor, unit)
                following = self._cursor_units(cursor, 1)
                if following:
                    heapq.heappush(frontier, (following[0].key, index, following[0]))
        self._candidate_more = len(merged) > self.batch_size
        return merged[:self.batch_size]

    @staticmethod
    def _dominates(total, contribution) -> bool:
        if total is None or contribution is None:
            return False
        return all(value is None or (stored is not None and stored >= value)
                   for stored, value in zip(total, contribution))

    @staticmethod
    def _coverage_dominates(total_json, contribution_json) -> bool:
        """Malformed or weaker persisted evidence can never justify deletion."""
        try:
            total = _decode_coverage(total_json)
            contribution = _decode_coverage(contribution_json)
        except (ValueError, TypeError):
            return False
        def contains(stored, incoming):
            if isinstance(incoming, dict):
                return all(contains(stored[key], value) for key, value in incoming.items())
            if isinstance(incoming, str):
                return stored == incoming or stored == "incomplete"
            return stored >= incoming
        return contains(total, contribution)

    def _process_totals(self, boot, app_id, values, coverage_json) -> bool:
        c = self.connection
        boot_total = c.execute("SELECT read_bytes,write_bytes,rchar,wchar,peak_read_bps,peak_write_bps,peak_total_bps,coverage_json FROM app_boot_total WHERE boot_id=? AND app_id=?", (boot, app_id)).fetchone()
        lifetime = c.execute("SELECT read_bytes,write_bytes,rchar,wchar,coverage_json FROM app_lifetime_total WHERE app_id=?", (app_id,)).fetchone()
        return (boot_total is not None and lifetime is not None
                and self._dominates(boot_total[:7], values)
                and self._dominates(lifetime[:4], values[:4])
                and self._coverage_dominates(boot_total[7], coverage_json)
                and self._coverage_dominates(lifetime[4], coverage_json))

    def _device_total(self, boot, app, dev, values):
        total = self.connection.execute("SELECT rbytes,wbytes FROM app_boot_dev_total WHERE boot_id=? AND app_id=? AND dev=?", (boot, app, dev)).fetchone()
        return self._dominates(total, values)

    def _materialized(self, unit: _Unit) -> bool:
        c = self.connection
        if unit.kind == "file":
            coverage = c.execute("SELECT coverage_json FROM file_coverage WHERE boot_id=? AND app_id=? AND ts_mono_ns=?", unit.primary_key).fetchone()
            total_cov = c.execute("SELECT coverage_json FROM app_boot_file_coverage WHERE boot_id=? AND app_id=?", (unit.boot, unit.app_id)).fetchone()
            if total_cov is None or coverage is None or not self._coverage_dominates(total_cov[0], coverage[0]):
                return False
            for path_id, br, bw, reads, writes in c.execute("SELECT path_id,bytes_r,bytes_w,reads,writes FROM file_sample WHERE boot_id=? AND app_id=? AND ts_mono_ns=?", unit.primary_key):
                total = c.execute("SELECT bytes_r,bytes_w,reads,writes FROM app_boot_file_total WHERE boot_id=? AND app_id=? AND path_id=?", (unit.boot, unit.app_id, path_id)).fetchone()
                if not self._dominates(total, (br, bw, reads, writes)):
                    return False
            return True
        if unit.kind == "file_unknown":
            raw = c.execute("SELECT overflow_files,overflow_bytes_r,overflow_bytes_w,coverage_json FROM file_overflow_unknown WHERE boot_id=? AND ts_mono_ns=?", unit.primary_key).fetchone()
            total = c.execute("SELECT overflow_files,overflow_bytes_r,overflow_bytes_w,coverage_json FROM boot_file_overflow_unknown WHERE boot_id=?", (unit.boot,)).fetchone()
            return (raw is not None and total is not None and self._dominates(total[:3], raw[:3])
                    and self._coverage_dominates(total[3], raw[3]))
        if unit.kind == "core":
            coverage = c.execute("SELECT coverage_json FROM sample_coverage WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=?", unit.primary_key).fetchone()
            if coverage is None:
                return False
            has_contributions = any(c.execute(
                f"SELECT 1 FROM {table} WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=? LIMIT 1",
                unit.primary_key,
            ).fetchone() for table in ("sample", "sample_dev"))
            if has_contributions and not ownership_settled(c, unit.boot, unit.primary_key[1], unit.mono):
                return False
            buckets = {(60, unit.mono // 60_000_000_000 * 60_000_000_000): [0, 0, 0, 0],
                       (3600, unit.mono // 3_600_000_000_000 * 3_600_000_000_000): [0, 0, 0, 0]}
            counters = [0, 0, 0, 0]
            for row in c.execute("SELECT ts_mono_ns,read_bytes,write_bytes,rchar,wchar FROM sample WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=?", unit.primary_key):
                for index, value in enumerate(row[1:]):
                    counters[index] += value
                for secs in (60, 3600):
                    start = row[0] // (secs * 1_000_000_000) * (secs * 1_000_000_000)
                    values = buckets.setdefault((secs, start), [0, 0, 0, 0])
                    for index, value in enumerate(row[1:]):
                        values[index] += value
            if not self._process_totals(unit.boot, unit.app_id, (*counters, None, None, None), coverage[0]):
                return False
            for (secs, start), values in buckets.items():
                total = c.execute("SELECT read_bytes,write_bytes,rchar,wchar,coverage_json FROM rollup_app WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=?", (unit.boot, unit.app_id, secs, start)).fetchone()
                if total is None or not self._dominates(total[:4], values):
                    return False
                if start == unit.mono // (secs * 1_000_000_000) * (secs * 1_000_000_000) and not self._coverage_dominates(total[4], coverage[0]):
                    return False
            for dev, mono, rb, wb in c.execute("SELECT dev,ts_mono_ns,rbytes,wbytes FROM sample_dev WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=?", unit.primary_key):
                if not self._device_total(unit.boot, unit.app_id, dev, (rb, wb)):
                    return False
                for secs in (60, 3600):
                    start = mono // (secs * 1_000_000_000) * (secs * 1_000_000_000)
                    row = c.execute("SELECT rbytes,wbytes FROM rollup_dev WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=? AND dev=?", (unit.boot, unit.app_id, secs, start, dev)).fetchone()
                    if not self._dominates(row, (rb, wb)):
                        return False
            return True
        if unit.kind == "gap":
            beginning, end, reason = c.execute("SELECT from_mono_ns,to_mono_ns,reason FROM gap WHERE gap_id=?", unit.primary_key).fetchone()
            field = "overlap_uncertain_intervals" if reason == "overlap_uncertain" else "gap_intervals"
            apps = c.execute("SELECT app_id,coverage_json FROM app_boot_total WHERE boot_id=?", (unit.boot,)).fetchall()
            if not apps:
                return False
            for app_id, text in apps:
                try:
                    if _decode_coverage(text)[field] == 0 and reason != "overlap_uncertain":
                        return False
                    life = c.execute("SELECT coverage_json FROM app_lifetime_total WHERE app_id=?", (app_id,)).fetchone()
                    if life is None or (_decode_coverage(life[0])[field] == 0 and reason != "overlap_uncertain"):
                        return False
                    for secs in (60, 3600):
                        width = secs * 1_000_000_000
                        first = beginning // width * width
                        last = max(beginning, end - 1) // width * width
                        rows = c.execute("SELECT bucket_start_mono_ns,coverage_json FROM rollup_app WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns BETWEEN ? AND ? ORDER BY bucket_start_mono_ns", (unit.boot, app_id, secs, first, last))
                        expected = first
                        for start, text in rows:
                            if start != expected or (_decode_coverage(text)[field] == 0 and reason != "overlap_uncertain"):
                                return False
                            expected += width
                        if expected != last + width:
                            return False
                except (ValueError, TypeError):
                    return False
            return self._file_gap_materializable(unit.boot, beginning, end)
        row = c.execute("SELECT read_bytes,write_bytes,rchar,wchar,peak_read_bps,peak_write_bps,peak_total_bps,coverage_json FROM rollup_app WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=?", unit.primary_key).fetchone()
        if row is None or not self._process_totals(unit.boot, unit.app_id, row[:7], row[7]):
            return False
        return all(self._device_total(unit.boot, unit.app_id, dev, (rb, wb))
                   for dev, rb, wb in c.execute("SELECT dev,rbytes,wbytes FROM rollup_dev WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=?", unit.primary_key))

    def _preserve_overlap_gap(self, boot, beginning, end):
        """Replace the disappearing live process overlay in retained tiers."""
        for table, keys, condition, params in (
            ("app_boot_total", "boot_id,app_id", "boot_id=?", (boot,)),
            ("app_lifetime_total", "app_id", "app_id IN (SELECT app_id FROM app_boot_total WHERE boot_id=?)", (boot,)),
            ("rollup_app", "boot_id,app_id,bucket_start_mono_ns,bucket_secs",
             "boot_id=? AND bucket_start_mono_ns<? AND bucket_start_mono_ns+bucket_secs*1000000000>?", (boot, end, beginning)),
        ):
            rows = self.connection.execute(f"SELECT {keys},coverage_json FROM {table} WHERE {condition}", params).fetchall()
            for *values, text in rows:
                obj = _decode_coverage(text)
                for field in ("gap_intervals", "overlap_uncertain_intervals"):
                    obj[field] = checked_sum(obj[field], 1, field=field)
                where = " AND ".join(f"{key}=?" for key in keys.split(","))
                self.connection.execute(f"UPDATE {table} SET coverage_json=? WHERE {where}", (canonical_json(obj), *values))

    def _file_gap_rows(self, boot, beginning, end):
        """Every retained hourly source and never-pruned file coverage source."""
        first = beginning // FILE_HOUR_NS * FILE_HOUR_NS
        last = (end - 1) // FILE_HOUR_NS * FILE_HOUR_NS
        for table, keys, bounds, params in (
            ("file_coverage", "boot_id,app_id,ts_mono_ns", " AND ts_mono_ns BETWEEN ? AND ?", (boot, first, last)),
            ("file_overflow_unknown", "boot_id,ts_mono_ns", " AND ts_mono_ns BETWEEN ? AND ?", (boot, first, last)),
            ("app_boot_file_coverage", "boot_id,app_id", "", (boot,)),
            ("boot_file_overflow_unknown", "boot_id", "", (boot,)),
        ):
            for row in self.connection.execute(f"SELECT {keys},coverage_json FROM {table} WHERE boot_id=?{bounds}", params).fetchall():
                yield table, keys.split(","), row[:-1], row[-1]

    def _file_gap_materializable(self, boot, beginning, end):
        try:
            for _, _, _, text in self._file_gap_rows(boot, beginning, end):
                _decode_coverage(text)
            for hour, text in self.connection.execute(
                "SELECT ts_mono_ns,state_json FROM file_hour_state WHERE boot_id=? AND ts_mono_ns<? AND ts_mono_ns>?",
                (boot, end, beginning - FILE_HOUR_NS),
            ):
                decode_file_hour_state(text, boot, hour)
        except (ValueError, TypeError):
            return False
        return True

    def _preserve_file_gap(self, boot, beginning, end, reason):
        """Transfer the live overlay exactly once, in the gap deletion transaction.

        A rollback restores both the gap and these updates; a committed retry
        cannot find the deleted gap. File counters and hook columns stay exact.
        """
        fields = ("gap_intervals", "overlap_uncertain_intervals") if reason == "overlap_uncertain" else ("gap_intervals",)
        for table, keys, values, text in self._file_gap_rows(boot, beginning, end):
            obj = _decode_coverage(text)
            for field in fields:
                obj[field] = checked_sum(obj[field], 1, field=field)
            where = " AND ".join(f"{key}=?" for key in keys)
            self.connection.execute(f"UPDATE {table} SET coverage_json=? WHERE {where}", (canonical_json(obj), *values))
        pending = self.connection.execute(
            "SELECT ts_mono_ns,state_json FROM file_hour_state WHERE boot_id=? AND ts_mono_ns<? AND ts_mono_ns>?",
            (boot, end, beginning - FILE_HOUR_NS),
        ).fetchall()
        def coverage(obj):
            return replace(obj, complete=0, **{field: checked_sum(getattr(obj, field), 1, field=field) for field in fields})
        for hour, text in pending:
            state = decode_file_hour_state(text, boot, hour)
            state = replace(state, apps=tuple(replace(app, coverage=coverage(app.coverage)) for app in state.apps),
                            unknown_owner=replace(state.unknown_owner, coverage=coverage(state.unknown_owner.coverage)))
            self.connection.execute("UPDATE file_hour_state SET state_json=? WHERE boot_id=? AND ts_mono_ns=?",
                                    (canonical_json(state), boot, hour))

    def _checkpoint(self, *, truncate: bool = True) -> bool:
        """Checkpoint the WAL; ask for TRUNCATE only where file size is the point.

        The retention delete loop must not request TRUNCATE. A history query holds
        one WAL snapshot for its whole duration, and a live reader makes TRUNCATE
        report busy, so a successful deletion would be surfaced as a maintenance
        failure -- the daemon then reports ``answering: false`` while it is in fact
        collecting and deleting. PASSIVE never waits on a reader and never reports
        busy, which is what a per-deletion checkpoint needs. The quota boundary
        still truncates, because there the physical size is the measurement.
        """

        statement = ("PRAGMA wal_checkpoint(TRUNCATE)" if truncate
                     else "PRAGMA wal_checkpoint(PASSIVE)")
        row = self.connection.execute(statement).fetchone()
        return row is not None and row[0] == 0

    def _garbage_collect_paths(self):
        # Path references have no inverse path_id index in the frozen schema.
        # Defer optional GC when proving reachability would scan a large file
        # history. Retaining an orphan is safe; deleting a referenced identity
        # is not. Small histories still collect orphans incrementally.
        limit = self.batch_size + 1
        pending = self.connection.execute(
            "SELECT boot_id,ts_mono_ns,state_json FROM file_hour_state LIMIT ?", (limit,),
        ).fetchall()
        references = [self.connection.execute(
            f"SELECT path_id FROM {table} LIMIT ?", (limit,),
        ).fetchall() for table in ("file_sample", "app_boot_file_total")]
        if any(len(rows) == limit for rows in (pending, *references)):
            return
        protected = {row[0] for rows in references for row in rows}
        for boot, hour, text in pending:
            state = decode_file_hour_state(text, boot, hour)
            for app in state.apps:
                for entry in app.files:
                    for name in (entry.basename, *entry.aliases):
                        row = self.connection.execute("SELECT path_id FROM path WHERE fs_uuid=? AND dev=? AND inode=? AND generation=? AND basename=? AND identity_weak=?", (entry.fs_uuid, entry.dev, entry.inode, entry.generation, name, entry.identity_weak)).fetchone()
                        if row:
                            protected.add(row[0])
        paths = self.connection.execute(
            "SELECT path_id FROM path WHERE path_id>? ORDER BY path_id LIMIT ?",
            (self._gc_after, self.batch_size),
        ).fetchall()
        for (path_id,) in paths:
            if path_id not in protected:
                self.connection.execute("DELETE FROM path_alias WHERE path_id=?", (path_id,))
                self.connection.execute("DELETE FROM path WHERE path_id=?", (path_id,))
        self._gc_after = paths[-1][0] if len(paths) == self.batch_size else 0

    def _delete(self, unit: _Unit) -> DroppedUnit:
        c, pk = self.connection, unit.primary_key
        stats = DroppedUnit(unit.kind, pk)
        if unit.kind == "core":
            rows = c.execute("SELECT read_bytes,write_bytes FROM sample WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=?", pk).fetchall()
            stats = replace(stats, retained_rows=len(rows), bytes_r=sum(r[0] for r in rows), bytes_w=sum(r[1] for r in rows))
            for table in ("sample", "sample_dev", "sample_coverage"):
                c.execute(f"DELETE FROM {table} WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=?", pk)
        elif unit.kind == "file":
            rows = c.execute("SELECT bytes_r,bytes_w FROM file_sample WHERE boot_id=? AND app_id=? AND ts_mono_ns=?", pk).fetchall()
            coverage = c.execute("SELECT counted_omitted_files,counted_omitted_bytes_r,counted_omitted_bytes_w,uncounted_overflow_files,uncounted_overflow_bytes_r,uncounted_overflow_bytes_w FROM file_coverage WHERE boot_id=? AND app_id=? AND ts_mono_ns=?", pk).fetchone()
            stats = replace(stats, retained_rows=len(rows), bytes_r=sum(r[0] for r in rows), bytes_w=sum(r[1] for r in rows), **dict(zip(("counted_omitted_files", "counted_omitted_bytes_r", "counted_omitted_bytes_w", "uncounted_overflow_files", "uncounted_overflow_bytes_r", "uncounted_overflow_bytes_w"), coverage)))
            c.execute("DELETE FROM file_sample WHERE boot_id=? AND app_id=? AND ts_mono_ns=?", pk)
            c.execute("DELETE FROM file_coverage WHERE boot_id=? AND app_id=? AND ts_mono_ns=?", pk)
        elif unit.kind == "file_unknown":
            values = c.execute("SELECT overflow_files,overflow_bytes_r,overflow_bytes_w FROM file_overflow_unknown WHERE boot_id=? AND ts_mono_ns=?", pk).fetchone()
            stats = replace(stats, unknown_overflow_files=values[0], unknown_overflow_bytes_r=values[1], unknown_overflow_bytes_w=values[2])
            c.execute("DELETE FROM file_overflow_unknown WHERE boot_id=? AND ts_mono_ns=?", pk)
        elif unit.kind == "gap":
            beginning, end, reason = c.execute("SELECT from_mono_ns,to_mono_ns,reason FROM gap WHERE gap_id=?", pk).fetchone()
            self._preserve_file_gap(unit.boot, beginning, end, reason)
            if reason == "overlap_uncertain":
                self._preserve_overlap_gap(unit.boot, beginning, end)
            c.execute("DELETE FROM gap WHERE gap_id=?", pk)
        else:
            boot, app, secs, start = pk
            values = c.execute("SELECT read_bytes,write_bytes FROM rollup_app WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=?", pk).fetchone()
            stats = replace(stats, retained_rows=1, bytes_r=values[0], bytes_w=values[1])
            c.execute("DELETE FROM rollup_dev WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=?", (boot, app, secs, start))
            c.execute("DELETE FROM rollup_app WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=?", pk)
        return stats

    def _result(self, deleted, reason=None, *, paused=None, skipped=(), coverage_loss=True):
        if reason and coverage_loss:
            self.coverage_complete = False
        self.last_reason = reason
        if paused is not None:
            self.sampling_paused = paused
        return PruneResult(tuple(deleted), reason is None, self.sampling_paused,
                           physical_store_bytes(self.database_paths), reason, tuple(skipped))

    def _commit_delete(self, unit):
        results = self._commit_deletes((unit,))
        return results[0]

    def _commit_deletes(self, units):
        """Delete units in one transaction; ``None`` marks an unmaterialized unit.

        One transaction per unit cost ~4 ms dominated by the durability fsync, so
        a pass capped at ``DEFAULT_BATCH_SIZE`` retired ~220 units a minute while
        the collector produced ~1350: the retained tail grew without bound and the
        store climbed back to its ceiling. A batch keeps every unit indivisible
        (a unit's rows all go, or the batch rolls back) and pays one fsync for it.
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            results = []
            for unit in units:
                results.append(self._delete(unit) if self._materialized(unit) else None)
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return results

    def expire(self, now_boot_id: str, now_mono_ns: int) -> PruneResult:
        self._require_idle()
        validate_sqlite_integer(now_mono_ns, "now_mono_ns")
        if not isinstance(now_boot_id, str) or not now_boot_id:
            raise ValueError("now_boot_id must be nonempty")
        now_wall = self._map(now_boot_id, now_mono_ns)
        if now_wall is None:
            return self._result([], "missing_clock_mapping")
        units = self._candidates(now_wall, self._expiry_after)
        if units is None:
            return self._result([], "missing_clock_mapping")
        if self._scan_pending:
            return self._result([], "batch_limit")
        deleted, skipped = [], []
        start = 0
        chunk_size = DELETE_UNITS_PER_TRANSACTION
        recovering_full = False
        retried_single = False
        while start < len(units):
            chunk = units[start:start + chunk_size]
            previous_after = self._expiry_after
            # Advance before committing so a crash mid-batch cannot pin the sweep
            # on the same oldest units forever.
            self._expiry_after = chunk[-1].key
            try:
                stats_by_unit = self._commit_deletes(chunk)
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", None)
                if code is None or (code & 0xff) != sqlite3.SQLITE_FULL:
                    raise
                # The transaction has rolled back. Retry its oldest units, not
                # the cursor beyond the failed chunk; checkpointing also lets
                # SQLite reuse WAL space before each smaller transaction.
                self._expiry_after = previous_after
                if len(chunk) == 1 and retried_single:
                    raise
                if not self._checkpoint(truncate=False):
                    return self._result(deleted, "checkpoint_busy", paused=True, skipped=skipped)
                # Even one unit may fit after reclaiming committed WAL frames.
                # Retry it once; a second failure is a real, bounded error.
                retried_single = len(chunk) == 1
                chunk_size = max(1, len(chunk) // 2)
                recovering_full = True
                continue
            retried_single = False
            for unit, stats in zip(chunk, stats_by_unit):
                if stats is None:
                    skipped.append(SkippedUnit(unit.kind, unit.primary_key))
                else:
                    deleted.append(stats)
            start += len(chunk)
            if recovering_full and not self._checkpoint(truncate=False):
                return self._result(deleted, "checkpoint_busy", paused=True, skipped=skipped)
        # One checkpoint per pass on the healthy path: unit deletion has no
        # remeasurement to serve, and a checkpoint between chunks publishes pages
        # the next chunk dirties again.
        if deleted and not self._checkpoint(truncate=False):
            return self._result(deleted, "checkpoint_busy", paused=True, skipped=skipped)
        # Advancing past skipped units prevents a permanently bad oldest batch
        # from starving later ready units. Wrap after a sweep so repaired units
        # are retried, and late/imported observations are eventually revisited.
        self._expiry_after = units[-1].key if units and self._candidate_more else None
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._garbage_collect_paths()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        if not self._checkpoint(truncate=False):
            return self._result(deleted, "checkpoint_busy", paused=True, skipped=skipped)
        reason = "missing_materialization" if skipped else "batch_limit" if self._candidate_more else None
        return self._result(deleted, reason, skipped=skipped)

    def main_bytes(self) -> int:
        """Size of the database file itself: the part a rewrite can shrink."""
        path = next((row[2] for row in self.connection.execute("PRAGMA database_list") if row[1] == "main"), "")
        if not path:
            return 0
        try:
            return Path(path).stat().st_size
        except FileNotFoundError:
            return 0

    def reclaimable_bytes(self) -> int:
        """Bytes a rewrite could hand back to the filesystem right now."""
        page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        free = self.connection.execute("PRAGMA freelist_count").fetchone()[0]
        return free * page_size

    def compaction_reserve(self) -> int:
        """Free space a rewrite must leave under the ceiling to be worth doing."""
        numerator, denominator = COMPACT_RESERVE_FRACTION
        return max(COMPACT_RESERVE_BYTES, self.main_bytes() * numerator // denominator)

    def _compact(self) -> bool:
        """Rewrite the file only when the result is meaningfully under the ceiling.

        ``False`` means no rewrite ran, so the caller owes no checkpoint. The
        rewrite costs one full copy of the database, so it must return at least
        ``compaction_reserve()`` bytes of headroom -- otherwise the store lands
        back over quota almost immediately and rewrites again. When the live rows
        alone exceed the quota no rewrite can help, and the caller must prune.
        """
        reclaimable = self.reclaimable_bytes()
        if reclaimable <= 0:
            return False
        if physical_store_bytes(self.database_paths) - reclaimable + self.compaction_reserve() > self.quota_bytes:
            return False
        self.connection.execute("VACUUM")
        return True

    def shrink_if_needed(self) -> PruneResult:
        self._require_idle()
        validate_sqlite_integer(self.quota_bytes, "quota_bytes")
        deleted, skipped = [], []
        if not self._checkpoint():
            return self._result(deleted, "checkpoint_busy", paused=True)
        physical = physical_store_bytes(self.database_paths)
        if physical > self.quota_bytes:
            reclaimable = self.reclaimable_bytes()
            # Reachable by a rewrite: the live rows plus the pages already freed
            # fit under the ceiling.
            reachable = reclaimable > 0 and physical - reclaimable <= self.quota_bytes
            if self._compact():
                if not self._checkpoint():
                    return self._result(deleted, "checkpoint_busy", paused=True)
            elif reachable:
                # The file is over the ceiling only by pages this store already
                # freed. Deleting history cannot shrink the file, and a rewrite is
                # not yet worth it, so hold the free pages rather than spend
                # gigabytes to bank them.
                return self._result(deleted, "compaction_deferred", paused=False,
                                    coverage_loss=False)
            if physical_store_bytes(self.database_paths) > self.quota_bytes:
                units = self._candidates(after=self._quota_after)
                if units is None:
                    return self._result(deleted, "missing_clock_mapping", paused=True)
                if self._scan_pending:
                    return self._result(deleted, "batch_limit", paused=True)
                for unit in units:
                    self._quota_after = unit.key
                    stats = self._commit_delete(unit)
                    if stats is None:
                        skipped.append(SkippedUnit(unit.kind, unit.primary_key))
                        continue
                    deleted.append(stats)
                    if not self._checkpoint():
                        return self._result(deleted, "checkpoint_busy", paused=True, skipped=skipped)
                    if physical_store_bytes(self.database_paths) <= self.quota_bytes:
                        break
                else:
                    if not self._candidate_more:
                        self._quota_after = None
                if deleted and physical_store_bytes(self.database_paths) > self.quota_bytes:
                    # Compact once per batch, never once per deleted unit.
                    if self._compact() and not self._checkpoint():
                        return self._result(deleted, "checkpoint_busy", paused=True, skipped=skipped)
                if physical_store_bytes(self.database_paths) > self.quota_bytes:
                    reason = ("missing_materialization" if skipped else
                              "batch_limit" if self._candidate_more else "quota_exhausted")
                    return self._result(deleted, reason, paused=True, skipped=skipped)
        # Resume is based only on a new physical measurement after checkpoint.
        size = physical_store_bytes(self.database_paths)
        if size > self.quota_bytes:
            return self._result(deleted, "quota_exhausted", paused=True, skipped=skipped)
        self._quota_after = None
        return self._result(deleted, "missing_materialization" if skipped else None,
                            paused=False, skipped=skipped)
