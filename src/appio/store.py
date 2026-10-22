"""Atomic process-stream persistence (specification sections 5.1, 5.4 and 6).

The store owns the outer BEGIN IMMEDIATE. Receipt savepoints join that transaction;
all arithmetic happens in Python before binding INTEGER values. The only write
allowed after a rejected observation is its separate, representable A21 diagnostic.
Callers publish the returned pure sampler state only after record_tick succeeds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3

from .clock import SUSPEND_OFFSET_STEP_NS, bucket_start_mono_ns
from .contracts import CounterOverflowError, SQLITE_INT_MAX, validate_sqlite_integer as _contract_integer
from .durable import (
    SamplerCheckpointRow,
    SamplerCheckpointState,
    canonical_json,
    decode_sampler_checkpoint,
)
from .launch import LaunchIdentity, LaunchState, ProcessState, ReapCredit
from .handover import HandoverOffer, IncompleteHandoverError
from .migrations import migrate_empty, open_database
from .ownership import (
    device_interval_start, intersects, ownership_exclusions, subtract_intervals,
    uncertain_intervals,
)
from .procs import ProcessIdentity
from .reconcile import rate_bps
from .sampler import AggregatedFrame, AppDelta, FrameCoverage, SamplerState, StaleFrameError
from .stream import CollectorEpoch, FrameIdentity, accept_frame, allocate_epoch

__all__ = ["AppIoStore"]

_NS = 1_000_000_000
_COUNTERS = ("rchar", "wchar", "read_bytes", "write_bytes")
_PEAKS = ("peak_read_bps", "peak_write_bps", "peak_total_bps")
_COUNTS = (
    "inaccessible_pids", "resets", "reap_corrections", "partial_intervals",
    "gap_intervals", "overlap_uncertain_intervals", "spans_suspend_intervals",
)
_VECTORS = ("reap_unapplied", "reap_unobserved")
_FLAGS = ("reap_incomplete", "counter_overflow")
_FILE_COUNTS = (
    "counted_omitted_files", "counted_omitted_bytes_r", "counted_omitted_bytes_w",
    "uncounted_overflow_files", "uncounted_overflow_bytes_r", "uncounted_overflow_bytes_w",
    "skipped_nonfile", "skipped_unsupported_fs", "weak_identity_rows",
)
_HOOKS = ("vfs_read", "vfs_write", "vfs_readv", "vfs_writev", "vfs_iter_read", "vfs_iter_write")


def _integer(value: object, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > SQLITE_INT_MAX:
        raise CounterOverflowError(field)
    return _contract_integer(value, field)


@contextmanager
def _app_context(app_key: str | None) -> Iterator[None]:
    """Keep an external identity on the exception, never a rolled-back local ID."""
    try:
        yield
    except CounterOverflowError as error:
        if app_key is not None and getattr(error, "app_key", None) is None:
            error.app_key = app_key
        raise


def _check_tree(value: object, field: str) -> None:
    """JSON integers need the same range checks as SQL INTEGER bindings."""
    if isinstance(value, int):
        _integer(value, field)
    elif isinstance(value, dict):
        with _app_context(value.get("app_key")):
            for key, item in value.items():
                _check_tree(item, f"{field}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _check_tree(item, f"{field}[{index}]")


def _empty_coverage() -> dict:
    return {
        **dict.fromkeys(_COUNTS, 0),
        **{name: dict.fromkeys(_COUNTERS, 0) for name in _VECTORS},
        **dict.fromkeys(_FLAGS, 0),
        "device_state": "incomplete",
        "file": {**dict.fromkeys(_FILE_COUNTS, 0), "hook_hits": dict.fromkeys(_HOOKS, 0)},
    }


def _exact_keys(value: object, keys: tuple[str, ...], field: str) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{field} must have exactly {', '.join(keys)}")


def _validate_coverage(value: dict) -> dict:
    _exact_keys(value, _COUNTS + _VECTORS + _FLAGS + ("device_state", "file"), "coverage")
    for name in _COUNTS + _FLAGS:
        number = _integer(value[name], f"coverage.{name}")
        if name in _FLAGS and number not in (0, 1):
            raise ValueError(f"coverage.{name} must be 0 or 1")
    for name in _VECTORS:
        _exact_keys(value[name], _COUNTERS, f"coverage.{name}")
        _check_tree(value[name], f"coverage.{name}")
    if value["device_state"] not in ("available", "not_delegated", "incomplete"):
        raise ValueError("invalid coverage.device_state")
    _exact_keys(value["file"], _FILE_COUNTS + ("hook_hits",), "coverage.file")
    _exact_keys(value["file"]["hook_hits"], _HOOKS, "coverage.file.hook_hits")
    _check_tree(value["file"], "coverage.file")
    if value["file"] != _empty_coverage()["file"]:
        raise ValueError("process coverage must retain the zero file placeholder")
    return value


def _frame_coverage(item: FrameCoverage) -> dict:
    result = _empty_coverage()
    for name in _COUNTS:
        result[name] = getattr(item, name)
    for name in _VECTORS:
        result[name] = dict(zip(_COUNTERS, getattr(item, name), strict=True))
    for name in _FLAGS:
        flag = getattr(item, name)
        if not isinstance(flag, bool):
            raise ValueError(f"frame coverage {name} must be a bool")
        result[name] = int(flag)
    result["device_state"] = item.device_state
    return _validate_coverage(result)


def _merge_coverage(left: dict | None, right: dict) -> dict:
    _validate_coverage(right)
    if left is None:
        return right
    _validate_coverage(left)
    result = _empty_coverage()
    for name in _COUNTS:
        result[name] = _integer(left[name] + right[name], f"coverage.{name}")
    for name in _VECTORS:
        for counter in _COUNTERS:
            result[name][counter] = _integer(
                left[name][counter] + right[name][counter], f"coverage.{name}.{counter}"
            )
    for name in _FLAGS:
        result[name] = left[name] | right[name]
    result["device_state"] = left["device_state"] if left["device_state"] == right["device_state"] else "incomplete"
    return result


def _nullable_max(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    return left if right is None else max(left, right)


def _interval_peaks(deltas: list[AppDelta]) -> dict[int, dict[str, int | None]]:
    """Sum common app intervals across launches before flooring each of three rates."""
    by_timestamp: dict[int, list[AppDelta]] = defaultdict(list)
    for delta in deltas:
        by_timestamp[delta.attributed_ts_mono_ns].append(delta)
    result = {}
    for timestamp, items in by_timestamp.items():
        intervals = {(item.interval_start_mono_ns, item.observed_ns, item.observed_mono_ns) for item in items}
        first = items[0]
        peaks = dict.fromkeys(_PEAKS)
        if len(intervals) == 1 and first.interval_start_mono_ns is not None and all(item.delta_kind != "baseline" for item in items):
            read = sum(item.counters[2] for item in items)
            write = sum(item.counters[3] for item in items)
            for field, amount in zip(_PEAKS, (read, write, read + write), strict=True):
                rate = rate_bps(amount, first.observed_ns)
                peaks[field] = None if rate is None else _integer(rate, field)
        result[timestamp] = peaks
    return result


class AppIoStore:
    """A single migrated SQLite connection and its last published sampler state.

    A supplied connection remains caller-owned and must have no outer transaction.
    ``connection`` is also the connection query/file-hour components must share.
    ``sampler_state`` changes only after a successful commit or explicit restore.
    """

    def __init__(self, connection_or_path: sqlite3.Connection | str | Path) -> None:
        if isinstance(connection_or_path, sqlite3.Connection):
            migrate_empty(connection_or_path)
            self.connection = connection_or_path
        else:
            self.connection = open_database(Path(connection_or_path))
        self.sampler_state: SamplerState | None = None
        self._owns_transaction = False

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.connection.in_transaction:
            raise ValueError("store operation requires a connection without an active transaction")
        self.connection.execute("BEGIN IMMEDIATE")
        self._owns_transaction = True
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        finally:
            self._owns_transaction = False

    def start_boot(self, boot_id: str, boot_order: int, started_mono_ns: int, started_wall: float, source: str) -> None:
        FrameIdentity(boot_id, source, 1, 0)
        _integer(boot_order, "boot.boot_order")
        _integer(started_mono_ns, "boot.started_mono_ns")
        if boot_order == 0:
            raise ValueError("boot_order must be positive")
        if isinstance(started_wall, bool) or not isinstance(started_wall, (int, float)) or not math.isfinite(started_wall) or started_wall < 0:
            raise ValueError("started_wall must be finite nonnegative wall seconds")
        values = (boot_id, boot_order, started_mono_ns, started_wall, source)
        with self._transaction():
            old = self.connection.execute(
                "SELECT boot_id,boot_order,started_mono_ns,started_wall,source FROM boot WHERE boot_id=?", (boot_id,)
            ).fetchone()
            if old is not None:
                if tuple(old) != values:
                    raise ValueError("boot chronology is immutable")
                return
            self.connection.execute(
                "INSERT INTO boot (boot_id,boot_order,started_mono_ns,started_wall,source) VALUES (?,?,?,?,?)", values
            )

    def start_collector(self, source: str, boot_id: str, allocated_mono_ns: int) -> CollectorEpoch:
        FrameIdentity(boot_id, source, 1, 0)
        _integer(allocated_mono_ns, "collector.allocated_mono_ns")
        with self._transaction():
            previous = self.connection.execute(
                "SELECT boot_id,epoch,allocated_mono_ns FROM collector_epoch WHERE source=?", (source,)
            ).fetchone()
            checkpoint = self.connection.execute(
                "SELECT boot_id,checkpoint_mono_ns FROM sampler_checkpoint WHERE source=?", (source,)
            ).fetchone()
            same_previous = previous is not None and previous[0] == boot_id
            same_checkpoint = checkpoint is not None and checkpoint[0] == boot_id
            if (same_previous and allocated_mono_ns < previous[2]) or (same_checkpoint and allocated_mono_ns < checkpoint[1]):
                raise ValueError("collector start precedes its durable evidence")
            last_diagnostic = self.connection.execute(
                "SELECT max(ts_mono_ns) FROM store_diagnostic WHERE boot_id=? AND source=?", (boot_id, source)
            ).fetchone()[0]
            if last_diagnostic is not None and allocated_mono_ns < last_diagnostic:
                raise ValueError("overflow recovery start precedes the rejected observation")
            epoch = allocate_epoch(self.connection, source, boot_id, allocated_mono_ns)
            if same_previous and allocated_mono_ns > previous[2]:
                self._collector_interval(source, boot_id, previous[1], previous[2], allocated_mono_ns)
            gap_start = checkpoint[1] if same_checkpoint else None
            if same_previous:
                if gap_start is not None:
                    gap_start = max(gap_start, previous[2])
                elif last_diagnostic is not None:
                    gap_start = previous[2]
            if gap_start is not None and allocated_mono_ns > gap_start:
                self._insert_gap(boot_id, source, epoch.epoch, 0, gap_start, allocated_mono_ns, "collector_gap")
        return epoch

    def _collector_interval(self, source: str, boot_id: str, epoch: int, start: int, end: int) -> None:
        previous = self.connection.execute(
            "SELECT to_mono_ns FROM collector_interval WHERE source=? AND boot_id=? AND epoch=?",
            (source, boot_id, epoch),
        ).fetchone()
        beginning = previous[0] if previous else start
        self.connection.execute(
            "INSERT INTO collector_interval (source,boot_id,epoch,from_mono_ns,to_mono_ns) VALUES (?,?,?,?,?) "
            "ON CONFLICT(source,boot_id,epoch) DO UPDATE SET to_mono_ns=excluded.to_mono_ns",
            (source, boot_id, epoch, start, end),
        )
        exclusions = ownership_exclusions(self.connection, boot_id)
        for left, right in exclusions[source]:
            if max(beginning, left) < min(end, right):
                self._exclude_contributions(boot_id, max(beginning, left), min(end, right), source=source)
        others = self.connection.execute(
            "SELECT source,from_mono_ns,to_mono_ns FROM collector_interval WHERE boot_id=? "
            "AND NOT (source=? AND epoch=?) AND from_mono_ns<? AND to_mono_ns>?",
            (boot_id, source, epoch, end, beginning),
        ).fetchall()
        active = self.connection.execute("SELECT last_seq FROM collector_epoch WHERE source=?", (source,)).fetchone()
        seq = active[0] if active and active[0] is not None else 0
        for other_source, left, right in others:
            for overlap_start, overlap_end in subtract_intervals(
                max(beginning, left), min(end, right), exclusions[source] + exclusions[other_source],
            ):
                for new_start, new_end in subtract_intervals(overlap_start, overlap_end, uncertain_intervals(self.connection, boot_id)):
                    self._insert_gap(boot_id, source, epoch, seq, new_start, new_end, "overlap_uncertain")
        # An explicit earlier gap must also exclude a newly arriving contributor.
        for left, right in uncertain_intervals(self.connection, boot_id):
            if left < end and right > beginning:
                self._exclude_contributions(boot_id, left, right)

    def _insert_gap(self, boot_id: str, source: str, epoch: int, seq: int, start: int, end: int, reason: str) -> None:
        if reason == "overlap_uncertain" and self.connection.execute(
            "SELECT 1 FROM gap WHERE boot_id=? AND reason=? AND from_mono_ns=? AND to_mono_ns=?",
            (boot_id, reason, start, end),
        ).fetchone():
            return
        self.connection.execute(
            "INSERT INTO gap (boot_id,source,epoch,seq,from_mono_ns,to_mono_ns,reason) VALUES (?,?,?,?,?,?,?)",
            (boot_id, source, epoch, seq, start, end, reason),
        )
        if reason == "overlap_uncertain":
            self._exclude_contributions(boot_id, start, end)
            # Coverage-only buckets keep the overlap representable after raw expiry.
            for (app_id,) in self.connection.execute("SELECT app_id FROM app_boot_total WHERE boot_id=?", (boot_id,)).fetchall():
                for width in (60, 3600):
                    first = bucket_start_mono_ns(start, width * _NS)
                    last = bucket_start_mono_ns(end - 1, width * _NS)
                    for bucket in range(first, last + 1, width * _NS):
                        self._accumulate("rollup_app", {"boot_id": boot_id, "app_id": app_id,
                                         "bucket_start_mono_ns": bucket, "bucket_secs": width},
                                         dict.fromkeys(_COUNTERS, 0), peaks=dict.fromkeys(_PEAKS))

    def _subtract(self, table, keys, amounts):
        fields = tuple(amounts)
        where = " AND ".join(f"{key}=?" for key in keys)
        row = self.connection.execute(f"SELECT {','.join(fields)} FROM {table} WHERE {where}", tuple(keys.values())).fetchone()
        if row is None:
            raise ValueError(f"ownership exclusion requires materialized {table}")
        values = tuple(_integer(old - amounts[field], f"{table}.{field}") for old, field in zip(row, fields, strict=True))
        self.connection.execute(f"UPDATE {table} SET " + ",".join(f"{field}=?" for field in fields) + f" WHERE {where}",
                                (*values, *keys.values()))

    def _remaining_peaks(self, boot_id, app_id, bucket, width):
        groups = defaultdict(list)
        for source, epoch, seq, ts, observed, start, duration, kind, read, write in self.connection.execute(
            "SELECT source,epoch,seq,ts_mono_ns,observed_mono_ns,interval_start_mono_ns,observed_ns,delta_kind,read_bytes,write_bytes "
            "FROM sample WHERE boot_id=? AND app_id=? AND ts_mono_ns>=? AND ts_mono_ns<?",
            (boot_id, app_id, bucket, bucket + width * _NS),
        ):
            groups[(source, epoch, seq, ts, observed)].append((start, duration, kind, read, write))
        peaks = dict.fromkeys(_PEAKS)
        for entries in groups.values():
            if entries[0][0] is None or len({(row[0], row[1]) for row in entries}) != 1 or any(row[2] == "baseline" for row in entries):
                continue
            read, write = sum(row[3] for row in entries), sum(row[4] for row in entries)
            for field, amount in zip(_PEAKS, (read, write, read + write), strict=True):
                peaks[field] = _nullable_max(peaks[field], rate_bps(amount, entries[0][1]))
        return peaks

    def _exclude_contributions(self, boot_id, beginning, end, *, source=None):
        """Retract both owners' indivisible deltas atomically, without replaying totals.

        Excluded source rows are removed while their coverage receipts remain.
        Subtraction preserves history that was already pruned from raw storage.
        Repeating this operation cannot subtract the same contribution twice.
        """
        by_app, buckets = {}, {}
        source_sql = " AND source=?" if source is not None else ""
        params = (boot_id, *((source,) if source is not None else ()))
        rows = self.connection.execute(
            "SELECT source,epoch,seq,app_id,launch_id,ts_mono_ns,observed_mono_ns,interval_start_mono_ns,rchar,wchar,read_bytes,write_bytes "
            "FROM sample WHERE boot_id=?" + source_sql, params,
        ).fetchall()
        receipts = set()
        for owner, epoch, seq, app, launch, ts, observed, start, *amounts in rows:
            if source is None and start is None:
                # A restored delta has no rate duration in schema v1. For
                # uncertainty, the epoch start is only a conservative lower
                # bound: exclude the whole delta, never invent a rate or split it.
                interval = self.connection.execute(
                    "SELECT from_mono_ns FROM collector_interval WHERE boot_id=? AND source=? AND epoch=?",
                    (boot_id, owner, epoch),
                ).fetchone()
                if interval is not None and interval[0] <= observed:
                    start = interval[0]
            if not intersects(start, observed, ts, [(beginning, end)]):
                continue
            receipts.add((owner, epoch, seq, app))
            target = by_app.setdefault(app, dict.fromkeys(_COUNTERS, 0))
            for field, amount in zip(_COUNTERS, amounts, strict=True):
                target[field] += amount
            for width in (60, 3600):
                key = (app, width, bucket_start_mono_ns(ts, width * _NS))
                target = buckets.setdefault(key, dict.fromkeys(_COUNTERS, 0))
                for field, amount in zip(_COUNTERS, amounts, strict=True):
                    target[field] += amount
            self.connection.execute("DELETE FROM sample WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=? AND launch_id=? AND ts_mono_ns=?",
                                    (boot_id, owner, epoch, seq, app, launch, ts))
        for app, amounts in by_app.items():
            self._subtract("app_boot_total", {"boot_id": boot_id, "app_id": app}, amounts)
            self._subtract("app_lifetime_total", {"app_id": app}, amounts)
        for (app, width, bucket), amounts in buckets.items():
            keys = {"boot_id": boot_id, "app_id": app, "bucket_secs": width, "bucket_start_mono_ns": bucket}
            self._subtract("rollup_app", keys, amounts)
            peaks = self._remaining_peaks(boot_id, app, bucket, width)
            self.connection.execute("UPDATE rollup_app SET peak_read_bps=?,peak_write_bps=?,peak_total_bps=? "
                                    "WHERE boot_id=? AND app_id=? AND bucket_secs=? AND bucket_start_mono_ns=?",
                                    (*peaks.values(), *keys.values()))
        for app in by_app:
            peaks = self.connection.execute("SELECT max(peak_read_bps),max(peak_write_bps),max(peak_total_bps) "
                                            "FROM rollup_app WHERE boot_id=? AND app_id=? AND bucket_secs=3600", (boot_id, app)).fetchone()
            self.connection.execute("UPDATE app_boot_total SET peak_read_bps=?,peak_write_bps=?,peak_total_bps=? WHERE boot_id=? AND app_id=?",
                                    (*peaks, boot_id, app))
        devices = self.connection.execute("SELECT source,epoch,seq,app_id,ts_mono_ns,dev,rbytes,wbytes FROM sample_dev WHERE boot_id=?" + source_sql, params).fetchall()
        for owner, epoch, seq, app, ts, dev, read, write in devices:
            start = device_interval_start(self.connection, boot_id, owner, epoch, ts)
            if (owner, epoch, seq, app) not in receipts and not intersects(start, ts, ts, [(beginning, end)]):
                continue
            amounts = {"rbytes": read, "wbytes": write}
            self._subtract("app_boot_dev_total", {"boot_id": boot_id, "app_id": app, "dev": dev}, amounts)
            for width in (60, 3600):
                self._subtract("rollup_dev", {"boot_id": boot_id, "app_id": app, "bucket_secs": width,
                               "bucket_start_mono_ns": bucket_start_mono_ns(ts, width * _NS), "dev": dev}, amounts)
            self.connection.execute("DELETE FROM sample_dev WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND app_id=? AND dev=?",
                                    (boot_id, owner, epoch, seq, app, dev))

    def record_gap(self, boot_id: str, source: str, epoch: int, seq: int, from_mono_ns: int, to_mono_ns: int, reason: str = "collector_gap") -> None:
        FrameIdentity(boot_id, source, epoch, seq)
        _integer(from_mono_ns, "gap.from_mono_ns")
        _integer(to_mono_ns, "gap.to_mono_ns")
        if to_mono_ns <= from_mono_ns:
            raise ValueError("a gap must be a nonempty half-open interval")
        if reason not in ("collector_gap", "overlap_uncertain", "gui_absent", "quota_exhausted"):
            raise ValueError("invalid gap reason")
        with self._transaction():
            self._insert_gap(boot_id, source, epoch, seq, from_mono_ns, to_mono_ns, reason)

    def _record_tick_in_transaction(self, frame: AggregatedFrame, next_state: SamplerState) -> None:
        """Write one receipt and frame inside the store's own outer transaction."""
        if not self._owns_transaction or not self.connection.in_transaction:
            raise ValueError("tick writer requires a store-owned transaction")
        if not isinstance(frame, AggregatedFrame) or not isinstance(next_state, SamplerState):
            raise TypeError("record_tick requires an AggregatedFrame and SamplerState")
        identity = FrameIdentity(frame.boot_id, frame.source, frame.epoch, frame.seq)
        active = self.connection.execute(
            "SELECT boot_id,epoch,last_seq,allocated_mono_ns FROM collector_epoch WHERE source=?", (frame.source,)
        ).fetchone()
        if active is None or tuple(active[:2]) != (frame.boot_id, frame.epoch) or (active[2] is not None and frame.seq <= active[2]):
            raise StaleFrameError("duplicate or stale frame identity")
        if active[2] is None and frame.seq != 0:
            raise StaleFrameError("the first frame in an epoch must have seq=0")
        if not accept_frame(self.connection, identity):
            raise StaleFrameError("duplicate or stale frame receipt")
        if self.connection.execute(
            "SELECT 1 FROM store_diagnostic WHERE boot_id=? AND source=? AND epoch=? LIMIT 1",
            (frame.boot_id, frame.source, frame.epoch),
        ).fetchone() is not None:
            raise ValueError("counter_overflow recovery requires a new collector epoch and explicit gap")
        previous = self.connection.execute(
            "SELECT checkpoint_mono_ns FROM sampler_checkpoint WHERE source=? AND boot_id=?", (frame.source, frame.boot_id)
        ).fetchone()
        if frame.observed_mono_ns < active[3] or (previous is not None and frame.observed_mono_ns < previous[0]):
            raise ValueError("frame observation precedes its durable evidence")
        checkpoint_text = self._checkpoint_text(frame, next_state)
        self._clock_observation(frame)
        ids = self._app_ids(frame, next_state)
        launches = self._launch_ids(frame, next_state, ids)
        coverages: dict[str, FrameCoverage] = {}
        deltas: dict[str, list[AppDelta]] = defaultdict(list)
        devices: dict[str, list] = defaultdict(list)
        for item in frame.coverage:
            if item.app_key in coverages or item.observed_mono_ns != frame.observed_mono_ns:
                raise ValueError("coverage must occur once per app at the observed frame timestamp")
            coverages[item.app_key] = item
        for delta in frame.deltas:
            deltas[delta.app_key].append(delta)
        for item in frame.device_deltas:
            devices[item.app_key].append(item.delta)
        required = set(deltas) | set(devices) | {process.app_key for process in next_state.processes}
        if not required <= coverages.keys():
            raise ValueError("each observed app requires one frame coverage receipt")
        for key in sorted(coverages, key=lambda value: value.encode("utf-8")):
            with _app_context(key):
                self._record_app(frame, ids[key], deltas[key], devices[key], coverages[key], launches)
        self.connection.execute(
            "INSERT INTO sampler_checkpoint (source,boot_id,epoch,seq,checkpoint_mono_ns,state_json) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET boot_id=excluded.boot_id,epoch=excluded.epoch,seq=excluded.seq,"
            "checkpoint_mono_ns=excluded.checkpoint_mono_ns,state_json=excluded.state_json",
            (frame.source, frame.boot_id, frame.epoch, frame.seq, frame.observed_mono_ns, checkpoint_text),
        )
        if frame.observed_mono_ns > active[3]:
            self._collector_interval(frame.source, frame.boot_id, frame.epoch, active[3], frame.observed_mono_ns)

    def record_tick(self, frame: AggregatedFrame, next_state: SamplerState) -> None:
        """Commit a complete frame or raise without publishing its staged state."""
        try:
            with self._transaction():
                self._record_tick_in_transaction(frame, next_state)
        except CounterOverflowError as error:
            self._diagnose_overflow(frame, error)
            raise
        self.sampler_state = next_state

    def _validate_handover_offer(self, offer: HandoverOffer) -> None:
        """Validate the outgoing durable frontier before any handover write."""
        from .durable import HandoverState, decode_handover

        if not isinstance(offer, HandoverOffer):
            raise TypeError("install_handover requires a HandoverOffer")
        state = decode_handover(canonical_json(offer.state), offer.state.boot_id, offer.state.cutoff_mono_ns)
        boot = self.connection.execute("SELECT boot_id FROM boot WHERE boot_id=?", (state.boot_id,)).fetchone()
        if boot is None:
            raise ValueError("handover requires an initialized boot row")
        active = self.connection.execute(
            "SELECT boot_id,epoch,last_seq,allocated_mono_ns FROM collector_epoch WHERE source=?",
            (offer.from_source,),
        ).fetchone()
        if active is None or tuple(active[:3]) != (state.boot_id, offer.outgoing_epoch, offer.outgoing_seq):
            raise ValueError("handover outgoing epoch or sequence is not the active durable frontier")
        if offer.interval_start_mono_ns < active[3] or offer.state.cutoff_mono_ns <= offer.interval_start_mono_ns:
            raise ValueError("handover interval is outside the outgoing epoch")
        checkpoint = self.connection.execute(
            "SELECT source,boot_id,epoch,seq,checkpoint_mono_ns,state_json FROM sampler_checkpoint WHERE source=?",
            (offer.from_source,),
        ).fetchone()
        if checkpoint is None:
            raise IncompleteHandoverError("handover outgoing checkpoint is missing")
        row = SamplerCheckpointRow(*checkpoint[:5])
        checkpoint_state = decode_sampler_checkpoint(checkpoint[5], row)
        if (checkpoint_state.boot_id, checkpoint_state.epoch, checkpoint_state.last_seq,
                checkpoint_state.checkpoint_mono_ns) != (state.boot_id, offer.outgoing_epoch,
                offer.outgoing_seq, state.cutoff_mono_ns):
            raise IncompleteHandoverError("handover cutoff is not the outgoing checkpoint frontier")
        checkpoint_handover = HandoverState(
            checkpoint_state.boot_id, checkpoint_state.checkpoint_mono_ns,
            checkpoint_state.processes, checkpoint_state.devices,
        )
        if canonical_json(checkpoint_handover) != canonical_json(state):
            raise ValueError("handover baselines do not match the outgoing checkpoint")
        cutoff_receipt = self.connection.execute(
            "SELECT 1 FROM sample_coverage WHERE boot_id=? AND source=? AND epoch=? AND seq=? "
            "AND ts_mono_ns=? LIMIT 1",
            (state.boot_id, offer.from_source, offer.outgoing_epoch, offer.outgoing_seq, state.cutoff_mono_ns),
        ).fetchone()
        if cutoff_receipt is None:
            raise IncompleteHandoverError("handover cutoff interval evidence is missing or pruned")
        # Schema v1 stores a rate-unknown delta as a point. At this exact
        # cutoff the unchanged raw query would select the incoming owner,
        # while materialized totals would retain the outgoing bytes.
        if self.connection.execute(
            "SELECT 1 FROM sample WHERE boot_id=? AND source=? AND epoch=? AND ts_mono_ns>=? "
            "AND interval_start_mono_ns IS NULL "
            "AND (rchar>0 OR wchar>0 OR read_bytes>0 OR write_bytes>0) LIMIT 1",
            (state.boot_id, offer.from_source, offer.outgoing_epoch, state.cutoff_mono_ns),
        ).fetchone() is not None:
            raise IncompleteHandoverError("handover cutoff lacks whole-delta interval evidence")
        interval = self.connection.execute(
            "SELECT from_mono_ns,to_mono_ns FROM collector_interval "
            "WHERE source=? AND boot_id=? AND epoch=?",
            (offer.from_source, state.boot_id, offer.outgoing_epoch),
        ).fetchone()
        if interval is None or tuple(interval) != (offer.interval_start_mono_ns, state.cutoff_mono_ns):
            raise IncompleteHandoverError("handover outgoing interval evidence is incomplete")
        if self.connection.execute(
            "SELECT 1 FROM store_diagnostic WHERE boot_id=? AND source=? AND epoch=? LIMIT 1",
            (state.boot_id, offer.from_source, offer.outgoing_epoch),
        ).fetchone() is not None:
            raise IncompleteHandoverError("handover outgoing epoch contains an overflow diagnostic")

        prior = self.connection.execute(
            "SELECT from_source,to_source,cutoff_mono_ns FROM handover WHERE boot_id=? "
            "ORDER BY cutoff_mono_ns DESC LIMIT 1", (state.boot_id,),
        ).fetchone()
        if prior is not None:
            if prior[1] != offer.from_source or prior[2] != offer.interval_start_mono_ns:
                raise IncompleteHandoverError("handover ownership chain has a missing predecessor")
        else:
            # A first clean handover cannot retroactively settle a concurrent
            # contributor or a gap whose bytes have already left raw storage.
            other = self.connection.execute(
                "SELECT source,from_mono_ns,to_mono_ns FROM collector_interval "
                "WHERE boot_id=? AND source<>? AND from_mono_ns<? LIMIT 1",
                (state.boot_id, offer.from_source, state.cutoff_mono_ns),
            ).fetchone()
            if other is not None:
                raise IncompleteHandoverError("handover has an unrecorded overlapping owner")
        if self.connection.execute(
            "SELECT 1 FROM gap WHERE boot_id=? AND from_mono_ns<? AND to_mono_ns>? "
            "AND reason IN ('collector_gap','quota_exhausted') LIMIT 1",
            (state.boot_id, state.cutoff_mono_ns, offer.interval_start_mono_ns),
        ).fetchone() is not None:
            raise IncompleteHandoverError("handover interval contains an unclosed gap")
        incoming = self.connection.execute(
            "SELECT boot_id,allocated_mono_ns FROM collector_epoch WHERE source=?", (offer.to_source,),
        ).fetchone()
        checkpoint = self.connection.execute(
            "SELECT boot_id,checkpoint_mono_ns FROM sampler_checkpoint WHERE source=?", (offer.to_source,),
        ).fetchone()
        if any(row is not None and row[0] == state.boot_id and row[1] > state.cutoff_mono_ns
               for row in (incoming, checkpoint)):
            raise ValueError("handover cutoff precedes incoming durable evidence")
        diagnostic = self.connection.execute(
            "SELECT max(ts_mono_ns) FROM store_diagnostic WHERE boot_id=? AND source=?",
            (state.boot_id, offer.to_source),
        ).fetchone()[0]
        if diagnostic is not None and diagnostic > state.cutoff_mono_ns:
            raise ValueError("handover cutoff precedes incoming overflow evidence")

    def install_handover(self, offer: HandoverOffer, *, wall_ns: int) -> CollectorEpoch:
        """Atomically install a validated cutoff and an incoming zero-byte receipt."""
        from dataclasses import replace
        from .handover import seed_handover

        if not isinstance(offer, HandoverOffer):
            raise TypeError("install_handover requires a HandoverOffer")
        _contract_integer(wall_ns, "handover.wall_ns", nonnegative=False)
        if self.connection.in_transaction:
            raise ValueError("store operation requires a connection without an active transaction")
        canonical = canonical_json(offer.state)
        with self._transaction():
            existing = self.connection.execute(
                "SELECT from_source,to_source,cutoff_mono_ns,baselines_json FROM handover "
                "WHERE boot_id=? AND cutoff_mono_ns=?",
                (offer.state.boot_id, offer.state.cutoff_mono_ns),
            ).fetchone()
            if existing is not None:
                if tuple(existing[:3]) != (offer.from_source, offer.to_source, offer.state.cutoff_mono_ns) or existing[3] != canonical:
                    raise ValueError("conflicting handover already exists at this cutoff")
                current = self.connection.execute(
                    "SELECT source,boot_id,epoch,last_seq,allocated_mono_ns FROM collector_epoch WHERE source=?",
                    (offer.to_source,),
                ).fetchone()
                if current is None or current[1] != offer.state.boot_id or current[4] < offer.state.cutoff_mono_ns:
                    raise IncompleteHandoverError("existing handover has no durable incoming epoch")
                return CollectorEpoch(*current)

            self._validate_handover_offer(offer)
            self.connection.execute(
                "INSERT INTO handover (boot_id,from_source,to_source,cutoff_mono_ns,baselines_json) VALUES (?,?,?,?,?)",
                (offer.state.boot_id, offer.from_source, offer.to_source, offer.state.cutoff_mono_ns, canonical),
            )
            # Reassert the outgoing interval in this transaction so the cutoff
            # evidence and ownership row share one commit boundary.
            self._collector_interval(
                offer.from_source, offer.state.boot_id, offer.outgoing_epoch,
                offer.interval_start_mono_ns, offer.state.cutoff_mono_ns,
            )
            incoming = allocate_epoch(
                self.connection, offer.to_source, offer.state.boot_id, offer.state.cutoff_mono_ns,
            )
            seed_frame, seeded_state = seed_handover(
                offer, incoming_epoch=incoming.epoch, wall_ns=wall_ns,
            )
            self._record_tick_in_transaction(seed_frame, seeded_state)
            result = replace(incoming, last_seq=0)
        self.sampler_state = seeded_state
        return result

    def _checkpoint_text(self, frame: AggregatedFrame, state: SamplerState) -> str:
        value = state.to_checkpoint()
        _check_tree(value, "checkpoint")
        _check_tree(frame.checkpoint_state, "frame.checkpoint")
        text = canonical_json(value)
        if text != canonical_json(frame.checkpoint_state):
            raise ValueError("frame and next_state checkpoint disagree")
        decode_sampler_checkpoint(text, SamplerCheckpointRow(frame.source, frame.boot_id, frame.epoch, frame.seq, frame.observed_mono_ns))
        for process in state.processes:
            identities = (process.identity, process.ppid_identity, process.launch_identity.root)
            if any(identity is not None and identity.boot_id != frame.boot_id for identity in identities):
                raise ValueError("checkpoint process identities belong to another boot")
            if process.launch_identity.app_key != process.app_key:
                raise ValueError("checkpoint launch and process app identities disagree")
        if any(key[0] != frame.boot_id for key, _ in state.device_baselines):
            raise ValueError("checkpoint device baseline belongs to another boot")
        return text

    def _clock_observation(self, frame: AggregatedFrame) -> None:
        latest = self.connection.execute(
            "SELECT segment_start_mono_ns,wall_offset_ns FROM clock_map WHERE boot_id=? ORDER BY segment_start_mono_ns DESC LIMIT 1", (frame.boot_id,)
        ).fetchone()
        if latest is not None and frame.observed_mono_ns < latest[0]:
            raise ValueError("clock observations must be non-decreasing within a boot")
        if latest is None or abs(frame.clock_offset_ns - latest[1]) > SUSPEND_OFFSET_STEP_NS:
            self.connection.execute(
                "INSERT INTO clock_map (boot_id,segment_start_mono_ns,wall_offset_ns) VALUES (?,?,?) "
                "ON CONFLICT(boot_id,segment_start_mono_ns) DO UPDATE SET wall_offset_ns=excluded.wall_offset_ns",
                (frame.boot_id, 0 if latest is None else frame.observed_mono_ns, frame.clock_offset_ns),
            )

    def _wall_seconds(self, boot_id: str, timestamp: int) -> float:
        mapping = self.connection.execute(
            "SELECT wall_offset_ns FROM clock_map WHERE boot_id=? AND segment_start_mono_ns<=? ORDER BY segment_start_mono_ns DESC LIMIT 1",
            (boot_id, timestamp),
        ).fetchone()
        if mapping is None:
            raise ValueError("observation has no persisted clock mapping")
        wall_ns = timestamp + mapping[0]
        if wall_ns < 0:
            raise ValueError("observation maps to negative wall seconds")
        return wall_ns / _NS

    def _app_ids(self, frame: AggregatedFrame, state: SamplerState) -> dict[str, int]:
        keys = ({item.app_key for item in frame.coverage} | {item.app_key for item in frame.deltas}
                | {item.app_key for item in frame.device_deltas} | {item.app_key for item in state.processes}
                | {item.identity.app_key for item in state.launches})
        result = {}
        wall = self._wall_seconds(frame.boot_id, frame.observed_mono_ns)
        next_id = self.connection.execute("SELECT coalesce(max(app_id),0) FROM app").fetchone()[0]
        for key in sorted(keys, key=lambda value: value.encode("utf-8")):
            old = self.connection.execute("SELECT app_id,first_seen,last_seen FROM app WHERE key=?", (key,)).fetchone()
            if old is None:
                next_id = _integer(next_id + 1, "app.app_id")
                self.connection.execute(
                    "INSERT INTO app (app_id,key,name,icon_path,first_seen,last_seen,scope) VALUES (?,?,?,NULL,?,?,?)",
                    (next_id, key, key.partition(":")[2] or key, wall, wall, "user"),
                )
                result[key] = next_id
            else:
                result[key] = old[0]
                self.connection.execute("UPDATE app SET first_seen=?,last_seen=? WHERE app_id=?", (min(old[1], wall), max(old[2], wall), old[0]))
        return result

    def _launch_ids(self, frame: AggregatedFrame, state: SamplerState, apps: dict[str, int]) -> dict[LaunchIdentity, int]:
        identities = {delta.launch_key for delta in frame.deltas} | {launch.identity for launch in state.launches}
        started = {identity: min((delta.attributed_ts_mono_ns for delta in frame.deltas if delta.launch_key == identity), default=frame.observed_mono_ns) for identity in identities}
        result = {}
        next_id = self.connection.execute("SELECT coalesce(max(launch_id),0) FROM launch").fetchone()[0]
        ordered = sorted((identity for identity in identities if identity.root is not None), key=lambda identity: (apps[identity.app_key], started[identity], identity.root.pid, identity.root.start_ticks))
        for identity in ordered:
            root = identity.root
            if root.boot_id != frame.boot_id:
                raise ValueError("launch root belongs to another boot")
            key = (frame.boot_id, apps[identity.app_key], root.pid, root.start_ticks)
            old = self.connection.execute("SELECT launch_id FROM launch WHERE boot_id=? AND app_id=? AND root_pid=? AND root_start_ticks=?", key).fetchone()
            if old is None:
                next_id = _integer(next_id + 1, "launch.launch_id")
                self.connection.execute(
                    "INSERT INTO launch (launch_id,boot_id,app_id,root_pid,root_start_ticks,started_mono_ns,ended_mono_ns,proc_count,coverage_json) VALUES (?,?,?,?,?,?,NULL,0,?)",
                    (next_id, *key, started[identity], canonical_json(_empty_coverage())),
                )
                result[identity] = next_id
            else:
                result[identity] = old[0]
        live = {result[launch.identity]: len(launch.members) for launch in state.launches if launch.identity.root is not None and launch.open}
        for app_id in apps.values():
            for row in self.connection.execute("SELECT launch_id FROM launch WHERE boot_id=? AND app_id=? AND ended_mono_ns IS NULL", (frame.boot_id, app_id)).fetchall():
                count = _integer(live.get(row[0], 0), "launch.proc_count")
                self.connection.execute("UPDATE launch SET proc_count=?,ended_mono_ns=? WHERE launch_id=?", (count, None if count else frame.observed_mono_ns, row[0]))
        return result

    def _accumulate(self, table: str, keys: Mapping, increments: Mapping, *, peaks: Mapping | None = None, coverage: dict | None = None, extra: Mapping | None = None) -> None:
        """An internal typed reducer: no SQL arithmetic can promote an INTEGER to REAL."""
        peak_fields = tuple(peaks) if peaks is not None else ()
        fields = tuple(increments) + peak_fields + (("coverage_json",) if table != "rollup_dev" and table != "app_boot_dev_total" else ())
        old_row = self.connection.execute(f"SELECT {','.join(fields)} FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in keys), tuple(keys.values())).fetchone()
        old = dict(zip(fields, old_row, strict=True)) if old_row is not None else {}
        values = dict(keys)
        for field, amount in increments.items():
            _integer(amount, f"{table}.{field}")
            values[field] = _integer(old.get(field, 0) + amount, f"{table}.{field}")
        for field in peak_fields:
            value = _nullable_max(old.get(field), peaks[field])
            values[field] = None if value is None else _integer(value, f"{table}.{field}")
        if "coverage_json" in fields:
            previous = None if old_row is None else _validate_coverage(json.loads(old["coverage_json"]))
            merged = previous if coverage is None else _merge_coverage(previous, coverage)
            values["coverage_json"] = canonical_json(_empty_coverage() if merged is None else merged)
        if extra is not None:
            values.update(extra)
        fields_sql = ",".join(values)
        updates = ",".join(f"{field}=excluded.{field}" for field in values if field not in keys)
        self.connection.execute(
            f"INSERT INTO {table} ({fields_sql}) VALUES ({','.join('?' for _ in values)}) ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}", tuple(values.values())
        )

    def _record_app(self, frame: AggregatedFrame, app_id: int, deltas: list[AppDelta], devices: list, receipt: FrameCoverage, launches: dict[LaunchIdentity, int]) -> None:
        prefix = (frame.boot_id, frame.source, frame.epoch, frame.seq, app_id)
        totals = dict.fromkeys(_COUNTERS, 0)
        by_bucket: dict[tuple[int, int], dict[str, int]] = {}
        peaks = _interval_peaks(deltas)
        boot_peaks = dict.fromkeys(_PEAKS)
        bucket_peaks: dict[tuple[int, int], dict[str, int | None]] = {}
        frame_coverage = _frame_coverage(receipt)
        for delta in sorted(deltas, key=lambda item: (launches.get(item.launch_key, 0), item.attributed_ts_mono_ns)):
            if delta.launch_key.app_key != delta.app_key or delta.observed_mono_ns != frame.observed_mono_ns:
                raise ValueError("sample app or observed frame identity disagrees")
            for field, amount in zip(_COUNTERS, delta.counters, strict=True):
                _integer(amount, f"sample.{field}")
                totals[field] += amount
            self.connection.execute(
                "INSERT INTO sample (boot_id,source,epoch,seq,app_id,launch_id,ts_mono_ns,observed_mono_ns,interval_start_mono_ns,observed_ns,ts_wall,rchar,wchar,read_bytes,write_bytes,delta_kind,first_seen_partial,spans_suspend) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*prefix, launches.get(delta.launch_key, 0), delta.attributed_ts_mono_ns, delta.observed_mono_ns,
                 delta.interval_start_mono_ns if delta.observed_ns is not None else None,
                 delta.observed_ns, self._wall_seconds(frame.boot_id, delta.attributed_ts_mono_ns),
                 *delta.counters, delta.delta_kind, int(delta.first_seen_partial), int(delta.spans_suspend)),
            )
            for width in (60, 3600):
                key = (width, bucket_start_mono_ns(delta.attributed_ts_mono_ns, width * _NS))
                contributions = by_bucket.setdefault(key, dict.fromkeys(_COUNTERS, 0))
                for field, amount in zip(_COUNTERS, delta.counters, strict=True):
                    contributions[field] += amount
        for timestamp, rates in peaks.items():
            for field in _PEAKS:
                boot_peaks[field] = _nullable_max(boot_peaks[field], rates[field])
            for width in (60, 3600):
                key = (width, bucket_start_mono_ns(timestamp, width * _NS))
                target = bucket_peaks.setdefault(key, dict.fromkeys(_PEAKS))
                for field in _PEAKS:
                    target[field] = _nullable_max(target[field], rates[field])
        device_totals: dict[str, dict[str, int]] = {}
        for device in devices:
            target = device_totals.setdefault(device.dev, {"rbytes": 0, "wbytes": 0})
            target["rbytes"] += _integer(device.rbytes, "sample_dev.rbytes")
            target["wbytes"] += _integer(device.wbytes, "sample_dev.wbytes")
        for dev, amounts in sorted(device_totals.items()):
            for field, value in amounts.items():
                _integer(value, f"sample_dev.{field}")
            self.connection.execute("INSERT INTO sample_dev (boot_id,source,epoch,seq,app_id,ts_mono_ns,dev,rbytes,wbytes) VALUES (?,?,?,?,?,?,?,?,?)", (*prefix, frame.observed_mono_ns, dev, amounts["rbytes"], amounts["wbytes"]))
        self.connection.execute("INSERT INTO sample_coverage (boot_id,source,epoch,seq,app_id,ts_mono_ns,coverage_json) VALUES (?,?,?,?,?,?,?)", (*prefix, frame.observed_mono_ns, canonical_json(frame_coverage)))
        for width in (60, 3600):
            observed_bucket = bucket_start_mono_ns(frame.observed_mono_ns, width * _NS)
            by_bucket.setdefault((width, observed_bucket), dict.fromkeys(_COUNTERS, 0))
        for (width, start), amounts in sorted(by_bucket.items()):
            keys = {"boot_id": frame.boot_id, "app_id": app_id, "bucket_start_mono_ns": start, "bucket_secs": width}
            observed_bucket = bucket_start_mono_ns(frame.observed_mono_ns, width * _NS)
            self._accumulate("rollup_app", keys, amounts, peaks=bucket_peaks.get((width, start), dict.fromkeys(_PEAKS)), coverage=frame_coverage if start == observed_bucket else None)
        for width in (60, 3600):
            keys = {"boot_id": frame.boot_id, "app_id": app_id, "bucket_start_mono_ns": bucket_start_mono_ns(frame.observed_mono_ns, width * _NS), "bucket_secs": width}
            for dev, amounts in sorted(device_totals.items()):
                self._accumulate("rollup_dev", {**keys, "dev": dev}, amounts)
        self._accumulate("app_boot_total", {"boot_id": frame.boot_id, "app_id": app_id}, totals, peaks=boot_peaks, coverage=frame_coverage)
        for dev, amounts in sorted(device_totals.items()):
            self._accumulate("app_boot_dev_total", {"boot_id": frame.boot_id, "app_id": app_id, "dev": dev}, amounts)
        last = self.connection.execute(
            "SELECT t.updated_boot_id,t.updated_mono_ns,b.boot_order FROM app_lifetime_total t JOIN boot b ON b.boot_id=t.updated_boot_id WHERE t.app_id=?", (app_id,)
        ).fetchone()
        boot_order = self.connection.execute("SELECT boot_order FROM boot WHERE boot_id=?", (frame.boot_id,)).fetchone()[0]
        endpoint = (frame.boot_id, frame.observed_mono_ns)
        if last is not None and (last[2], last[1]) > (boot_order, frame.observed_mono_ns):
            endpoint = (last[0], last[1])
        self._accumulate("app_lifetime_total", {"app_id": app_id}, totals, coverage=frame_coverage, extra={"updated_boot_id": endpoint[0], "updated_mono_ns": endpoint[1]})

    def _diagnose_overflow(self, frame: AggregatedFrame, error: CounterOverflowError) -> None:
        with self._transaction():
            app = self.connection.execute("SELECT app_id FROM app WHERE key=?", (getattr(error, "app_key", None),)).fetchone()
            self.connection.execute(
                "INSERT INTO store_diagnostic (boot_id,source,epoch,seq,app_id,ts_mono_ns,code,field) VALUES (?,?,?,?,?,?,'counter_overflow',?) "
                "ON CONFLICT(boot_id,source,epoch,seq,field) DO NOTHING",
                (frame.boot_id, frame.source, frame.epoch, frame.seq, None if app is None else app[0], frame.observed_mono_ns, error.field),
            )

    def restore_checkpoint(self, source: str, boot_id: str) -> SamplerState | None:
        FrameIdentity(boot_id, source, 1, 0)
        row = self.connection.execute("SELECT source,boot_id,epoch,seq,checkpoint_mono_ns,state_json FROM sampler_checkpoint WHERE source=?", (source,)).fetchone()
        if row is None or row[1] != boot_id:
            self.sampler_state = None
            return None
        data = decode_sampler_checkpoint(row[5], SamplerCheckpointRow(*row[:5]))
        state = self._restore_state(data)
        self.sampler_state = state
        return state

    @staticmethod
    def _restore_state(data: SamplerCheckpointState) -> SamplerState:
        def identity(ref):
            return None if ref is None else ProcessIdentity(data.boot_id, ref.pid, ref.start_ticks)

        processes = tuple(ProcessState(
            identity=ProcessIdentity(data.boot_id, item.pid, item.start_ticks),
            ppid_identity=identity(item.parent), app_key=item.app_key,
            launch_identity=LaunchIdentity(identity(item.launch_root), item.app_key), baseline=item.baseline,
            credits=tuple(ReapCredit(identity(credit.child), credit.disappearance_epoch, credit.disappearance_seq, credit.remaining, bool(credit.correction_counted)) for credit in item.credits),
            last_observed_mono_ns=data.checkpoint_mono_ns if item.baseline is not None else None,
            # v1 has no per-process observation/reset provenance. Never invent a
            # measured interval or exact reap conservation from that missing data.
            rate_unknown=True, reap_incomplete=True,
        ) for item in data.processes)
        groups: dict[LaunchIdentity, list[ProcessIdentity]] = defaultdict(list)
        for process in processes:
            if process.launch_identity.root is not None:
                groups[process.launch_identity].append(process.identity)
        launches = tuple(LaunchState(key, tuple(members)) for key, members in sorted(groups.items(), key=lambda item: (item[0].app_key.encode("utf-8"), item[0].root.pid, item[0].root.start_ticks)))
        return SamplerState(
            data.boot_id, data.source, data.epoch, data.last_seq, data.checkpoint_mono_ns,
            processes, launches,
            tuple(((data.boot_id, item.cgroup_id, item.dev), (item.rbytes, item.wbytes)) for item in data.devices),
            data.checkpoint_mono_ns,
        )

    def physical_store_bytes(self) -> int:
        path = next((row[2] for row in self.connection.execute("PRAGMA database_list") if row[1] == "main"), "")
        if not path:
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(path + suffix).stat().st_size
            except FileNotFoundError:
                pass
        return total
