"""Mapped-wall retention, indivisible deletion units, and a physical hard quota.

Rollups/totals are prerequisites, never rebuilt with additive UPSERTs by pruning.
Missing clock evidence or materialization stops a pass without guessing. Results
account separately for measured rows, counted omissions and failed insert attempts.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3

from appio.contracts import FILE_HOUR_NS, validate_sqlite_integer
from appio.durable import canonical_json, decode_file_hour_state
from appio.file_store import _decode_coverage
from appio.files import checked_sum
from appio.ownership import ownership_settled

DAY_NS = 86_400_000_000_000
DEFAULT_QUOTA_BYTES = 1_500_000_000
RETENTION_NS = {"core": DAY_NS, "gap": DAY_NS, "file": 14 * DAY_NS,
                "file_unknown": 14 * DAY_NS, 60: 30 * DAY_NS, 3600: 365 * DAY_NS}


def _paths(paths) -> tuple[Path, ...]:
    if isinstance(paths, (str, Path)):
        paths = (paths,)
    return tuple(dict.fromkeys(Path(path) for path in paths))


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
class PruneResult:
    deleted: tuple[DroppedUnit, ...]
    complete: bool
    sampling_paused: bool
    physical_bytes: int
    reason: str | None = None


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


class QuotaPruner:
    def __init__(self, connection: sqlite3.Connection, database_paths,
                 quota_bytes: int = DEFAULT_QUOTA_BYTES):
        validate_sqlite_integer(quota_bytes, "quota_bytes")
        self.connection = connection
        # The connected database is always included even if callers omit its name.
        connected = [row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main" and row[2]]
        self.database_paths = _paths((*_paths(database_paths), *connected))
        self.quota_bytes = quota_bytes
        self.sampling_paused = False
        self.coverage_complete = True
        self.last_reason: str | None = None

    def _require_idle(self):
        if self.connection.in_transaction:
            raise ValueError("pruning requires a connection without an active transaction")

    def _map(self, boot, mono):
        row = self.connection.execute("SELECT wall_offset_ns FROM clock_map WHERE boot_id=? AND segment_start_mono_ns<=? ORDER BY segment_start_mono_ns DESC LIMIT 1", (boot, mono)).fetchone()
        return None if row is None else mono + row[0]

    def _candidates(self) -> list[_Unit] | None:
        units = []
        for row in self.connection.execute("SELECT boot_id,source,epoch,seq,app_id,ts_mono_ns FROM sample_coverage"):
            boot, source, epoch, seq, app, mono = row
            units.append(_Unit("core", (boot, source, epoch, seq, app), boot, mono, source, epoch, seq, app))
        for row in self.connection.execute("SELECT boot_id,app_id,ts_mono_ns,source,epoch,seq FROM file_coverage"):
            boot, app, mono, source, epoch, seq = row
            units.append(_Unit("file", (boot, app, mono), boot, mono, source, epoch, seq, app))
        for row in self.connection.execute("SELECT boot_id,ts_mono_ns,source,epoch,seq FROM file_overflow_unknown"):
            boot, mono, source, epoch, seq = row
            units.append(_Unit("file_unknown", (boot, mono), boot, mono, source, epoch, seq))
        for row in self.connection.execute("SELECT gap_id,boot_id,to_mono_ns,source,epoch,seq FROM gap"):
            gap, boot, mono, source, epoch, seq = row
            units.append(_Unit("gap", (gap,), boot, mono, source, epoch, seq, gap_id=gap))
        for row in self.connection.execute("SELECT boot_id,app_id,bucket_secs,bucket_start_mono_ns FROM rollup_app"):
            boot, app, secs, mono = row
            units.append(_Unit("rollup", (boot, app, secs, mono), boot, mono, app_id=app, bucket_secs=secs))
        mapped = []
        for unit in units:
            wall = self._map(unit.boot, unit.mono)
            if wall is None:
                return None
            mapped.append(replace(unit, wall=wall))
        return sorted(mapped, key=lambda unit: unit.key)

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

    def _checkpoint(self) -> bool:
        row = self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return row is not None and row[0] == 0

    def _garbage_collect_paths(self):
        protected = set()
        for boot, hour, text in self.connection.execute("SELECT boot_id,ts_mono_ns,state_json FROM file_hour_state"):
            state = decode_file_hour_state(text, boot, hour)
            for app in state.apps:
                for entry in app.files:
                    for name in (entry.basename, *entry.aliases):
                        row = self.connection.execute("SELECT path_id FROM path WHERE fs_uuid=? AND dev=? AND inode=? AND generation=? AND basename=? AND identity_weak=?", (entry.fs_uuid, entry.dev, entry.inode, entry.generation, name, entry.identity_weak)).fetchone()
                        if row:
                            protected.add(row[0])
        protected.update(row[0] for row in self.connection.execute("SELECT path_id FROM file_sample UNION SELECT path_id FROM app_boot_file_total"))
        orphan_ids = [row[0] for row in self.connection.execute("SELECT path_id FROM path") if row[0] not in protected]
        for path_id in sorted(orphan_ids):
            self.connection.execute("DELETE FROM path_alias WHERE path_id=?", (path_id,))
            self.connection.execute("DELETE FROM path WHERE path_id=?", (path_id,))

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
        self._garbage_collect_paths()
        return stats

    def _result(self, deleted, reason=None, *, paused=None):
        if reason:
            self.coverage_complete = False
        self.last_reason = reason
        if paused is not None:
            self.sampling_paused = paused
        return PruneResult(tuple(deleted), reason is None, self.sampling_paused,
                           physical_store_bytes(self.database_paths), reason)

    def _commit_delete(self, unit):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if not self._materialized(unit):
                self.connection.execute("ROLLBACK")
                return None
            stats = self._delete(unit)
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return stats

    def expire(self, now_boot_id: str, now_mono_ns: int) -> PruneResult:
        self._require_idle()
        validate_sqlite_integer(now_mono_ns, "now_mono_ns")
        if not isinstance(now_boot_id, str) or not now_boot_id:
            raise ValueError("now_boot_id must be nonempty")
        now_wall = self._map(now_boot_id, now_mono_ns)
        units = self._candidates()
        if now_wall is None or units is None:
            return self._result([], "missing_clock_mapping")
        eligible = [u for u in units if u.wall < now_wall - RETENTION_NS[u.bucket_secs if u.kind == "rollup" else u.kind]]
        # Inspect all prerequisites before deleting even the first eligible row.
        if any(not self._materialized(u) for u in eligible):
            return self._result([], "missing_materialization")
        deleted = []
        for unit in eligible:
            stats = self._commit_delete(unit)
            if stats is None:
                return self._result(deleted, "missing_materialization")
            deleted.append(stats)
            if not self._checkpoint():
                return self._result(deleted, "checkpoint_busy", paused=True)
        if not eligible:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._garbage_collect_paths()
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            if not self._checkpoint():
                return self._result(deleted, "checkpoint_busy", paused=True)
        return self._result(deleted)

    def shrink_if_needed(self) -> PruneResult:
        self._require_idle()
        validate_sqlite_integer(self.quota_bytes, "quota_bytes")
        deleted = []
        if not self._checkpoint():
            return self._result(deleted, "checkpoint_busy", paused=True)
        while physical_store_bytes(self.database_paths) > self.quota_bytes:
            if self.connection.execute("PRAGMA freelist_count").fetchone()[0] > 0:
                self.connection.execute("VACUUM")
                if not self._checkpoint():
                    return self._result(deleted, "checkpoint_busy", paused=True)
                if physical_store_bytes(self.database_paths) <= self.quota_bytes:
                    break
            units = self._candidates()
            if units is None:
                return self._result(deleted, "missing_clock_mapping", paused=True)
            if not units:
                return self._result(deleted, "quota_exhausted", paused=True)
            unit = units[0]
            stats = self._commit_delete(unit)
            if stats is None:
                return self._result(deleted, "missing_materialization", paused=True)
            deleted.append(stats)
            if not self._checkpoint():
                return self._result(deleted, "checkpoint_busy", paused=True)
        # Resume is based only on a new physical measurement after checkpoint.
        size = physical_store_bytes(self.database_paths)
        if size > self.quota_bytes:
            return self._result(deleted, "quota_exhausted", paused=True)
        return self._result(deleted, paused=False)
