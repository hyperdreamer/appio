"""Tiered, snapshot-consistent SQLite queries for AppIO."""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import unicodedata
from collections import defaultdict
from typing import Iterator

from appio.contracts import (
    AppTotal,
    Coverage,
    Direction,
    DeviceSeriesPoint,
    DeviceShareRow,
    DiskShare,
    FILE_HOUR_NS,
    FileCoverage,
    FileRow,
    FileSort,
    FileTable,
    HookHits,
    MAX_RANGE_NS,
    Metric,
    RAW_BUCKET_NS,
    SQLITE_INT_MAX,
    ResidualPoint,
    Series,
    SeriesPoint,
    Tier,
    UnknownOwnerOverflow,
    Window,
    validate_enum,
    validate_search,
    validate_sqlite_integer,
    validate_window,
)
from appio.ownership import device_interval_start, intersects, ownership_exclusions, uncertain_intervals
from appio.reconcile import rate_bps
from appio.tier import select_tier

_COUNTERS = ("rchar", "wchar", "read_bytes", "write_bytes")
_COUNTS = (
    "inaccessible_pids", "resets", "reap_corrections", "partial_intervals",
    "gap_intervals", "overlap_uncertain_intervals", "spans_suspend_intervals",
)
_FLAGS = ("reap_incomplete", "counter_overflow")
_FILE_COUNTS = (
    "counted_omitted_files", "counted_omitted_bytes_r", "counted_omitted_bytes_w",
    "uncounted_overflow_files", "uncounted_overflow_bytes_r", "uncounted_overflow_bytes_w",
    "skipped_nonfile", "skipped_unsupported_fs", "weak_identity_rows",
)
_HOOKS = (
    "vfs_read", "vfs_write", "vfs_readv", "vfs_writev", "vfs_iter_read", "vfs_iter_write",
)
_METRICS = ("total_bytes", "read_bytes", "write_bytes", "rchar", "wchar")
_FILE_SORTS = ("bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen")
_DIRECTIONS = ("asc", "desc")


def _empty_coverage() -> dict:
    zero = dict.fromkeys(_COUNTERS, 0)
    return {
        **dict.fromkeys(_COUNTS, 0),
        "reap_unapplied": dict(zero),
        "reap_unobserved": dict(zero),
        **dict.fromkeys(_FLAGS, 0),
        "device_state": "incomplete",
        "invalid": False,
        "file": {**dict.fromkeys(_FILE_COUNTS, 0), "hook_hits": dict.fromkeys(_HOOKS, 0)},
    }


def _decode_coverage(text: str) -> dict:
    """Decode the exact stored v1 coverage shape; malformed evidence is incomplete."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate coverage key")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_object)
        expected = set(_COUNTS) | set(_FLAGS) | {
            "reap_unapplied", "reap_unobserved", "device_state", "file",
        }
        if not isinstance(value, dict) or value.keys() != expected:
            raise ValueError("invalid top-level coverage keys")
        result = _empty_coverage()
        for key in _COUNTS:
            result[key] = _nonnegative(value[key])
        for vector_name in ("reap_unapplied", "reap_unobserved"):
            vector = value[vector_name]
            if not isinstance(vector, dict) or vector.keys() != set(_COUNTERS):
                raise ValueError("invalid reap vector")
            for name in _COUNTERS:
                result[vector_name][name] = _nonnegative(vector[name])
        for key in _FLAGS:
            flag = _nonnegative(value[key])
            if flag not in (0, 1):
                raise ValueError("invalid coverage flag")
            result[key] = flag
        state = value["device_state"]
        if state not in ("available", "not_delegated", "incomplete"):
            raise ValueError("invalid device state")
        result["device_state"] = state
        file_value = value["file"]
        expected_file = set(_FILE_COUNTS) | {"hook_hits"}
        if not isinstance(file_value, dict) or file_value.keys() != expected_file:
            raise ValueError("invalid file coverage keys")
        for key in _FILE_COUNTS:
            result["file"][key] = _nonnegative(file_value[key])
        hooks = file_value["hook_hits"]
        if not isinstance(hooks, dict) or hooks.keys() != set(_HOOKS):
            raise ValueError("invalid hook coverage keys")
        for key in _HOOKS:
            result["file"]["hook_hits"][key] = _nonnegative(hooks[key])
        return result
    except (TypeError, ValueError, json.JSONDecodeError):
        invalid = _empty_coverage()
        invalid["invalid"] = True
        return invalid


def _nonnegative(value: object) -> int:
    return validate_sqlite_integer(value, "stored coverage")


def _reduce_coverage(target: dict, text: str) -> None:
    row = _decode_coverage(text)
    for key in _COUNTS:
        target[key] += row[key]
    for vector_name in ("reap_unapplied", "reap_unobserved"):
        for name in _COUNTERS:
            target[vector_name][name] += row[vector_name][name]
    for key in _FLAGS:
        target[key] = int(bool(target[key] or row[key]))
    target["invalid"] = bool(target.get("invalid", False) or row.get("invalid", False))
    target["device_states"].append(row["device_state"])
    for key in _FILE_COUNTS:
        target["file"][key] += row["file"][key]
    for key in _HOOKS:
        target["file"]["hook_hits"][key] += row["file"]["hook_hits"][key]


def _coverage_state(states: list[str]) -> str:
    if not states or "incomplete" in states or len(set(states)) != 1:
        return "incomplete"
    return states[0]


def _model_coverage(
    reduced: dict,
    tier: Tier,
    *,
    base_complete: bool,
    unavailable_reason: str | None,
    require_device: bool = False,
    require_file: bool = False,
    unknown_owner_files: int = 0,
) -> Coverage:
    state = _coverage_state(reduced["device_states"])
    file_values = reduced["file"]
    hook_hits = HookHits(**file_values["hook_hits"])
    task_incomplete = any(
        reduced[field]
        for field in ("inaccessible_pids", "resets", "partial_intervals", "gap_intervals",
                      "overlap_uncertain_intervals")
    ) or bool(reduced["reap_incomplete"] or reduced["counter_overflow"] or reduced.get("invalid"))
    device_incomplete = state != "available"
    file_incomplete = bool(
        file_values["uncounted_overflow_files"]
        or file_values["uncounted_overflow_bytes_r"]
        or file_values["uncounted_overflow_bytes_w"]
        or unknown_owner_files
    )
    complete = base_complete and not task_incomplete
    if require_device:
        complete = complete and not device_incomplete
    if require_file:
        complete = complete and not file_incomplete
    return Coverage(
        tier=tier,
        complete=bool(complete),
        unavailable_reason=unavailable_reason,
        inaccessible_pids=reduced["inaccessible_pids"],
        resets=reduced["resets"],
        reap_corrections=reduced["reap_corrections"],
        reap_unapplied=tuple(reduced["reap_unapplied"][name] for name in _COUNTERS),
        reap_unobserved=tuple(reduced["reap_unobserved"][name] for name in _COUNTERS),
        partial_intervals=reduced["partial_intervals"],
        gap_intervals=reduced["gap_intervals"],
        overlap_uncertain_intervals=reduced["overlap_uncertain_intervals"],
        spans_suspend_intervals=reduced["spans_suspend_intervals"],
        spans_suspend=bool(reduced.get("spans_suspend", False)),
        reap_incomplete=bool(reduced["reap_incomplete"]),
        counter_overflow=bool(reduced["counter_overflow"]),
        device_state=state,
        counted_omitted_files=file_values["counted_omitted_files"],
        counted_omitted_bytes_r=file_values["counted_omitted_bytes_r"],
        counted_omitted_bytes_w=file_values["counted_omitted_bytes_w"],
        uncounted_overflow_files=file_values["uncounted_overflow_files"],
        uncounted_overflow_bytes_r=file_values["uncounted_overflow_bytes_r"],
        uncounted_overflow_bytes_w=file_values["uncounted_overflow_bytes_w"],
        skipped_nonfile=file_values["skipped_nonfile"],
        skipped_unsupported_fs=file_values["skipped_unsupported_fs"],
        weak_identity_rows=file_values["weak_identity_rows"],
        hook_hits=hook_hits,
    )


def _ceil_bucket(value: int, width: int) -> int:
    return ((value + width - 1) // width) * width


def _missing_file_prefix_start(
    start: int, end: int, watermark: int, present_hours: set[int],
    collection_start: int | None,
) -> int | None:
    first = _ceil_bucket(start, FILE_HOUR_NS)
    last = (end // FILE_HOUR_NS) * FILE_HOUR_NS
    retained_end = max(first, min(last, watermark))
    expected = (retained_end - first) // FILE_HOUR_NS
    present = sorted(hour for hour in present_hours if first <= hour < retained_end)
    if len(present) >= expected or not present or present[0] <= first:
        return None
    covered_start = present[0]
    if collection_start is None or collection_start <= first or covered_start != collection_start:
        return None
    suffix_hours = (retained_end - covered_start) // FILE_HOUR_NS
    if len(present) == suffix_hours and all(
        hour == covered_start + index * FILE_HOUR_NS
        for index, hour in enumerate(present)
    ):
        return covered_start
    return None


def _unavailable_reason(*reasons: str | None) -> str | None:
    if "not_retained" in reasons:
        return "not_retained"
    return next((reason for reason in reasons if reason is not None), None)


def _bounds(window: Window) -> tuple[str | None, int, int | None]:
    if window.kind == "range":
        return window.boot_id, window.start_mono_ns, window.end_mono_ns
    if window.kind == "since_boot":
        return window.boot_id, 0, window.end_mono_ns
    return None, 0, None


class QueryService:
    """Run each public query against one SQLite read snapshot.

    Accepts either an ``AppIoStore`` or its SQLite connection. The connection
    remains caller-owned and is never mutated by a query.
    """

    def __init__(self, store_or_connection: object) -> None:
        connection = getattr(store_or_connection, "connection", store_or_connection)
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("QueryService requires an AppIoStore or sqlite3.Connection")
        self.connection = connection

    @contextmanager
    def _snapshot(self) -> Iterator[None]:
        owned = not self.connection.in_transaction
        if owned:
            self.connection.execute("BEGIN")
        try:
            yield
        finally:
            if owned and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")

    @staticmethod
    def _precheck(window: Window) -> None:
        validate_window(window)
        if window.kind == "launch" or (
            window.kind == "range" and window.end_mono_ns - window.start_mono_ns > MAX_RANGE_NS
        ):
            select_tier(window, None)

    def _available_cutoff(self, boot_id: str) -> int | None:
        values = []
        for sql, params in (
            ("SELECT max(checkpoint_mono_ns) FROM sampler_checkpoint WHERE boot_id=?", (boot_id,)),
            ("SELECT max(to_mono_ns) FROM collector_interval WHERE boot_id=?", (boot_id,)),
            ("SELECT max(ts_mono_ns) FROM sample_coverage WHERE boot_id=?", (boot_id,)),
            ("SELECT max(ts_mono_ns) FROM store_diagnostic WHERE boot_id=?", (boot_id,)),
        ):
            value = self.connection.execute(sql, params).fetchone()[0]
            if value is not None:
                values.append(value)
        return max(values) if values else None

    def _scope(self, window: Window) -> tuple[Tier, int | None]:
        cutoff = self._available_cutoff(window.boot_id) if window.boot_id is not None else None
        return select_tier(window, cutoff), cutoff

    def _require_app(self, app_id: int) -> None:
        app_id = validate_sqlite_integer(app_id, "app_id")
        if app_id <= 0:
            raise ValueError("app_id must be positive")
        if self.connection.execute("SELECT 1 FROM app WHERE app_id=?", (app_id,)).fetchone() is None:
            raise KeyError(app_id)

    def _diagnostics(self, boot_id: str | None, start: int, end: int | None, app_id: int | None, width: int) -> int:
        """A21 follows observation buckets, including clipped boundary buckets."""
        if boot_id is None:
            if app_id is None:
                row = self.connection.execute("SELECT count(*) FROM store_diagnostic WHERE app_id IS NULL").fetchone()
            else:
                row = self.connection.execute(
                    "SELECT count(*) FROM store_diagnostic WHERE app_id=? OR app_id IS NULL", (app_id,)
                ).fetchone()
            return row[0]
        if end is None:
            return 0
        first = (start // width) * width
        final = min(SQLITE_INT_MAX, _ceil_bucket(end, width) - 1)
        if app_id is None:
            return self.connection.execute(
                "SELECT count(*) FROM store_diagnostic WHERE boot_id=? AND app_id IS NULL "
                "AND ts_mono_ns>=? AND ts_mono_ns<=?",
                (boot_id, first, final),
            ).fetchone()[0]
        return self.connection.execute(
            "SELECT count(*) FROM store_diagnostic WHERE boot_id=? AND (app_id=? OR app_id IS NULL) "
            "AND ts_mono_ns>=? AND ts_mono_ns<=?",
            (boot_id, app_id, first, final),
        ).fetchone()[0]

    def _intervals_cover(self, boot_id: str, start: int, end: int) -> bool:
        cursor = start
        intervals = self.connection.execute(
            "SELECT from_mono_ns,to_mono_ns FROM collector_interval "
            "WHERE boot_id=? AND from_mono_ns<? AND to_mono_ns>? ORDER BY from_mono_ns,to_mono_ns",
            (boot_id, end, start),
        )
        for interval_start, interval_end in intervals:
            if interval_start > cursor:
                return False
            cursor = max(cursor, interval_end)
            if cursor >= end:
                return True
        return cursor >= end

    def _gap_overlay(self, reduced: dict, boot_id: str | None, start: int | None, end: int | None) -> bool:
        if boot_id is None:
            gaps = self.connection.execute("SELECT reason FROM gap").fetchall()
        else:
            gaps = self.connection.execute(
                "SELECT reason FROM gap WHERE boot_id=? AND from_mono_ns<? AND to_mono_ns>?",
                (boot_id, end, start),
            ).fetchall()
        for (reason,) in gaps:
            reduced["gap_intervals"] += 1
            if reason == "overlap_uncertain":
                reduced["overlap_uncertain_intervals"] += 1
        return not gaps

    def _overlays(self, reduced: dict, window: Window, app_id: int | None, width: int, totals_only: bool = False) -> tuple[bool, str | None]:
        boot_id, start, end = _bounds(window)
        base_complete = True
        reason = None
        if window.kind != "lifetime":
            if not totals_only and (start % width or end % width):
                base_complete = False
            if not self._intervals_cover(boot_id, start, end):
                base_complete = False
        gaps_complete = self._gap_overlay(reduced, boot_id, start, end)
        if not gaps_complete:
            base_complete = False
        if self._diagnostics(boot_id, start or 0, end, app_id, width):
            reduced["counter_overflow"] = 1
            base_complete = False
        if reduced["gap_intervals"] or reduced["overlap_uncertain_intervals"]:
            base_complete = False
        if reduced.get("missing_evidence"):
            base_complete = False
            reason = reduced.get("unavailable_reason", "not_retained")
        return base_complete, reason

    def _process_data(self, app_id: int | None, window: Window, tier: Tier):
        """Return per-app task/device buckets and exact persisted coverage rows."""
        boot_id, start, end = _bounds(window)
        data: dict[int, dict] = defaultdict(lambda: {
            "read": 0, "write": 0, "rchar": 0, "wchar": 0,
            "peaks": [None, None, None], "buckets": {}, "devices": defaultdict(lambda: [0, 0]),
            "coverage": _empty_coverage() | {"device_states": [], "spans_suspend": False},
            "evidence": 0, "sample_receipts": set(), "coverage_receipts": set(), "evidence_buckets": set(),
        })
        if tier == "totals_only":
            if window.kind == "lifetime":
                sql = "SELECT app_id,read_bytes,write_bytes,rchar,wchar,coverage_json FROM app_lifetime_total"
                params = ()
                dev_sql = "SELECT d.app_id,d.boot_id,d.dev,d.rbytes,d.wbytes FROM app_boot_dev_total d JOIN boot b USING(boot_id) ORDER BY b.boot_order,d.dev"
            else:
                sql = "SELECT app_id,read_bytes,write_bytes,rchar,wchar,peak_read_bps,peak_write_bps,peak_total_bps,coverage_json FROM app_boot_total WHERE boot_id=?"
                params = (boot_id,)
                dev_sql = "SELECT app_id,boot_id,dev,rbytes,wbytes FROM app_boot_dev_total WHERE boot_id=? ORDER BY dev"
            for row in self.connection.execute(sql, params):
                aid, read_bytes, write_bytes, rchar, wchar = row[:5]
                peaks = (None, None, None)
                cov = row[-1]
                item = data[aid]
                item["read"], item["write"], item["rchar"], item["wchar"] = read_bytes, write_bytes, rchar, wchar
                item["peaks"] = list(peaks)
                _reduce_coverage(item["coverage"], cov)
                item["coverage"]["spans_suspend"] = bool(item["coverage"]["spans_suspend_intervals"])
                item["evidence"] += 1
            dev_params = () if window.kind == "lifetime" else (boot_id,)
            for aid, boot, dev, read_bytes, write_bytes in self.connection.execute(dev_sql, dev_params):
                item = data[aid]
                item["devices"][(boot, dev)] = [read_bytes, write_bytes]
            return data

        if tier == "raw":
            uncertain = uncertain_intervals(self.connection, boot_id)
            excluded = {source: uncertain + intervals for source, intervals in ownership_exclusions(self.connection, boot_id).items()}
            excluded_receipts = set()
            width = RAW_BUCKET_NS
            first, last = _ceil_bucket(start, width), (end // width) * width
            coverage_rows = []
            if first < last:
                rows = self.connection.execute(
                    "SELECT app_id,ts_mono_ns,rchar,wchar,read_bytes,write_bytes,interval_start_mono_ns,observed_ns,observed_mono_ns,delta_kind,spans_suspend,source,epoch,seq "
                    "FROM sample WHERE boot_id=? AND ts_mono_ns>=? AND ts_mono_ns<? "
                    + ("AND app_id=? " if app_id is not None else "") + "ORDER BY app_id,ts_mono_ns,source,epoch,seq,launch_id",
                    (boot_id, first, last, *((app_id,) if app_id is not None else ())),
                ).fetchall()
                accepted_rows = []
                for row in rows:
                    aid, ts, rchar, wchar, read_bytes, write_bytes, interval_start, observed_ns, observed_mono, kind, spans, source, epoch, seq = row
                    item = data[aid]
                    receipt = (source, epoch, seq, aid)
                    key = (ts // width) * width
                    bucket = item["buckets"].setdefault(key, {"read": 0, "write": 0, "peaks": [None, None, None], "spans": False})
                    item["evidence"] += 1
                    item["sample_receipts"].add(receipt)
                    item["evidence_buckets"].add(key)
                    if intersects(interval_start, observed_mono, ts, excluded[source]):
                        excluded_receipts.add(receipt)
                        continue
                    accepted_rows.append(row)
                    item["read"] += read_bytes
                    item["write"] += write_bytes
                    item["rchar"] += rchar
                    item["wchar"] += wchar
                    bucket["read"] += read_bytes
                    bucket["write"] += write_bytes
                    bucket["spans"] = bucket["spans"] or bool(spans)
                    item["coverage"]["spans_suspend"] = item["coverage"]["spans_suspend"] or bool(spans)
                groups = defaultdict(list)
                for row in accepted_rows:
                    aid, ts, *tail = row
                    interval_start, observed_ns, observed_mono, kind = tail[4:8]
                    receipt = (tail[-3], tail[-2], tail[-1])
                    groups[(aid, *receipt, ts, observed_mono)].append((interval_start, observed_ns, kind, tail[0:4]))
                for (aid, source, epoch, seq, ts, observed_mono), entries in groups.items():
                    intervals = {(entry[0], entry[1], observed_mono) for entry in entries}
                    if len(intervals) != 1 or entries[0][0] is None or any(entry[2] == "baseline" for entry in entries):
                        continue
                    interval_start, observed_ns, _ = next(iter(intervals))
                    deltas = [sum(entry[3][index] for entry in entries) for index in (2, 3)]
                    rates = (
                        rate_bps(deltas[0], observed_ns),
                        rate_bps(deltas[1], observed_ns),
                        rate_bps(deltas[0] + deltas[1], observed_ns),
                    )
                    key = (ts // width) * width
                    bucket = data[aid]["buckets"].setdefault(key, {"read": 0, "write": 0, "peaks": [None, None, None], "spans": False})
                    for index, rate in enumerate(rates):
                        if rate is not None:
                            old = bucket["peaks"][index]
                            bucket["peaks"][index] = rate if old is None else max(old, rate)
                coverage_sql = (
                    "SELECT c.source,c.epoch,c.seq,c.app_id,c.ts_mono_ns,c.coverage_json "
                    "FROM sample_coverage c WHERE c.boot_id=? AND "
                    "((c.ts_mono_ns>=? AND c.ts_mono_ns<?) OR EXISTS ("
                    "SELECT 1 FROM sample s WHERE s.boot_id=c.boot_id AND s.source=c.source "
                    "AND s.epoch=c.epoch AND s.seq=c.seq AND s.app_id=c.app_id "
                    "AND s.ts_mono_ns>=? AND s.ts_mono_ns<?)) "
                    + ("AND c.app_id=? " if app_id is not None else "")
                    + "ORDER BY c.app_id,c.ts_mono_ns,c.source,c.epoch,c.seq"
                )
                coverage_params = (boot_id, first, last, first, last, *((app_id,) if app_id is not None else ()))
                coverage_rows = self.connection.execute(coverage_sql, coverage_params).fetchall() if first < last else []
            for source, epoch, seq, aid, ts, cov in coverage_rows:
                item = data[aid]
                _reduce_coverage(item["coverage"], cov)
                item["coverage_receipts"].add((source, epoch, seq, aid))
                item["evidence"] += 1
                if first <= ts < last:
                    key = (ts // width) * width
                    item["evidence_buckets"].add(key)
                    item["buckets"].setdefault(key, {"read": 0, "write": 0, "peaks": [None, None, None], "spans": False})
            if first < last:
                dev_rows = self.connection.execute(
                    "SELECT app_id,ts_mono_ns,dev,rbytes,wbytes,source,epoch,seq FROM sample_dev WHERE boot_id=? AND ts_mono_ns>=? AND ts_mono_ns<? "
                    + ("AND app_id=? " if app_id is not None else "") + "ORDER BY app_id,ts_mono_ns,dev",
                    (boot_id, first, last, *((app_id,) if app_id is not None else ())),
                ).fetchall()
                for aid, ts, dev, rb, wb, source, epoch, seq in dev_rows:
                    interval_start = device_interval_start(self.connection, boot_id, source, epoch, ts)
                    if (source, epoch, seq, aid) in excluded_receipts or intersects(interval_start, ts, ts, excluded[source]):
                        continue
                    item = data[aid]
                    key = (ts // width) * width
                    bucket = item["buckets"].setdefault(key, {"read": 0, "write": 0, "peaks": [None, None, None], "spans": False})
                    item["devices"][(boot_id, dev)][0] += rb
                    item["devices"][(boot_id, dev)][1] += wb
                    bucket.setdefault("device", defaultdict(lambda: [0, 0]))[dev][0] += rb
                    bucket["device"][dev][1] += wb
        else:
            width = 60_000_000_000 if tier == "rollup_60s" else 3_600_000_000_000
            first, last = _ceil_bucket(start, width), (end // width) * width
            if first < last:
                sql = (
                    "SELECT app_id,bucket_start_mono_ns,bucket_secs,read_bytes,write_bytes,rchar,wchar,peak_read_bps,peak_write_bps,peak_total_bps,coverage_json "
                    "FROM rollup_app WHERE boot_id=? AND bucket_secs=? AND bucket_start_mono_ns>=? AND bucket_start_mono_ns<? "
                    + ("AND app_id=? " if app_id is not None else "") + "ORDER BY app_id,bucket_start_mono_ns"
                )
                args = (boot_id, width // 1_000_000_000, first, last, *((app_id,) if app_id is not None else ()))
                for aid, bucket_start, bucket_secs, rb, wb, rc, wc, pr, pw, pt, cov in self.connection.execute(sql, args):
                    item = data[aid]
                    item["read"] += rb
                    item["write"] += wb
                    item["rchar"] += rc
                    item["wchar"] += wc
                    bucket = item["buckets"].setdefault(bucket_start, {"read": 0, "write": 0, "peaks": [None, None, None], "spans": False})
                    bucket["read"] += rb
                    bucket["write"] += wb
                    for index, value in enumerate((pr, pw, pt)):
                        if value is not None:
                            old = bucket["peaks"][index]
                            bucket["peaks"][index] = value if old is None else max(old, value)
                    _reduce_coverage(item["coverage"], cov)
                    bucket["spans"] = bucket["spans"] or bool(_decode_coverage(cov)["spans_suspend_intervals"])
                    item["coverage"]["spans_suspend"] = item["coverage"]["spans_suspend"] or bucket["spans"]
                    item["evidence_buckets"].add(bucket_start)
                    item["evidence"] += 1
                dev_sql = (
                    "SELECT app_id,bucket_start_mono_ns,dev,rbytes,wbytes FROM rollup_dev "
                    "WHERE boot_id=? AND bucket_secs=? AND bucket_start_mono_ns>=? AND bucket_start_mono_ns<? "
                    + ("AND app_id=? " if app_id is not None else "") + "ORDER BY app_id,bucket_start_mono_ns,dev"
                )
                for aid, bucket_start, dev, rb, wb in self.connection.execute(dev_sql, args[:4] + ((app_id,) if app_id is not None else ())):
                    data[aid]["devices"][(boot_id, dev)][0] += rb
                    data[aid]["devices"][(boot_id, dev)][1] += wb
                    bucket = data[aid]["buckets"].setdefault(bucket_start, {"read": 0, "write": 0, "peaks": [None, None, None], "spans": False})
                    bucket.setdefault("device", defaultdict(lambda: [0, 0]))[dev][0] += rb
                    bucket["device"][dev][1] += wb
        # Retained receipts, including zero-task ticks, prove bucket availability.
        expected_buckets = max(0, (last - first) // width)
        for item in data.values():
            if (
                not item["evidence"]
                or not item["sample_receipts"].issubset(item["coverage_receipts"])
                or len(item["evidence_buckets"]) < expected_buckets
            ):
                item["coverage"]["missing_evidence"] = True
        return data

    def _rank_rows(self, window: Window, tier: Tier, metric: str) -> list[AppTotal]:
        data = self._process_data(None, window, tier)
        rows = []
        boot_id, start, end = _bounds(window)
        width = RAW_BUCKET_NS if tier == "raw" else 60_000_000_000 if tier == "rollup_60s" else 3_600_000_000_000
        if tier == "rollup_60s":
            width = 60_000_000_000
        elif tier == "rollup_3600s":
            width = 3_600_000_000_000
        for aid, item in data.items():
            raw_cov = item["coverage"]
            base_complete, reason = self._overlays(raw_cov, window, aid, width, tier == "totals_only")
            if not item["evidence"]:
                raw_cov["missing_evidence"] = True
                base_complete, reason = False, "not_retained"
            coverage = _model_coverage(raw_cov, tier, base_complete=base_complete, unavailable_reason=reason)
            peaks = list(item["peaks"])
            for bucket in item["buckets"].values():
                for index, value in enumerate(bucket["peaks"]):
                    if value is not None:
                        peaks[index] = value if peaks[index] is None else max(peaks[index], value)
            read_bytes, write_bytes = item["read"], item["write"]
            total = read_bytes + write_bytes
            app = self.connection.execute("SELECT key,name FROM app WHERE app_id=?", (aid,)).fetchone()
            value = {"total_bytes": total, "read_bytes": read_bytes, "write_bytes": write_bytes,
                     "rchar": item["rchar"], "wchar": item["wchar"]}[metric]
            rows.append((value, app[0].encode("utf-8"), AppTotal(
                aid, app[0], app[1], read_bytes, write_bytes, total, item["rchar"], item["wchar"],
                *peaks, coverage,
            )))
        rows.sort(key=lambda record: (-record[0], record[1]))
        return [record[2] for record in rows]

    def ranking(self, window: Window, metric: Metric) -> list[AppTotal]:
        self._precheck(window)
        metric = validate_enum(metric, _METRICS, "metric")
        with self._snapshot():
            tier, _ = self._scope(window)
            return self._rank_rows(window, tier, metric)

    def _query_app_data(self, app_id: int, window: Window, tier: Tier, require_device: bool):
        self._require_app(app_id)
        data = self._process_data(app_id, window, tier).get(app_id)
        if data is None:
            data = {
                "read": 0, "write": 0, "rchar": 0, "wchar": 0,
                "peaks": [None, None, None], "buckets": {}, "devices": defaultdict(lambda: [0, 0]),
                "coverage": _empty_coverage() | {"device_states": [], "spans_suspend": False}, "evidence": 0,
            }
        width = RAW_BUCKET_NS if tier == "raw" else 60_000_000_000 if tier == "rollup_60s" else 3_600_000_000_000
        base, reason = self._overlays(data["coverage"], window, app_id, width, tier == "totals_only")
        if not data["evidence"]:
            base, reason = False, "not_retained"
        if require_device and not base:
            data["coverage"]["device_states"].append("incomplete")
        data["coverage"]["device_states"] = data["coverage"].get("device_states", [])
        coverage = _model_coverage(data["coverage"], tier, base_complete=base,
                                   unavailable_reason=reason, require_device=require_device)
        return data, coverage

    def series(self, app_id: int, window: Window) -> Series:
        self._precheck(window)
        with self._snapshot():
            tier, _ = self._scope(window)
            app_id = validate_sqlite_integer(app_id, "app_id")
            if app_id <= 0:
                raise ValueError("app_id must be positive")
            data, coverage = self._query_app_data(app_id, window, tier, True)
            points = []
            device_points = []
            residuals = []
            if tier != "totals_only":
                width = RAW_BUCKET_NS if tier == "raw" else 60_000_000_000 if tier == "rollup_60s" else 3_600_000_000_000
                boot_id = window.boot_id
                for start, value in sorted(data["buckets"].items()):
                    wall = None
                    if boot_id is not None:
                        mapping = self.connection.execute(
                            "SELECT wall_offset_ns FROM clock_map WHERE boot_id=? AND segment_start_mono_ns<=? ORDER BY segment_start_mono_ns DESC LIMIT 1",
                            (boot_id, start),
                        ).fetchone()
                        if mapping is not None:
                            wall = (start + mapping[0]) / 1_000_000_000
                    points.append(SeriesPoint(start, width // 1_000_000_000, wall, value["read"], value["write"],
                                              *value["peaks"], bool(value["spans"])))
                    bucket_devices = value.get("device", {})
                    for dev, amounts in sorted(bucket_devices.items(), key=lambda entry: entry[0].encode("utf-8")):
                        device_points.append(DeviceSeriesPoint(start, width // 1_000_000_000, dev, *amounts))
                    if coverage.device_state == "available":
                        d_read = sum(amounts[0] for amounts in bucket_devices.values())
                        d_write = sum(amounts[1] for amounts in bucket_devices.values())
                        residuals.append(ResidualPoint(start, width // 1_000_000_000,
                                                        value["read"] + value["write"] - d_read - d_write,
                                                        value["read"] - d_read, value["write"] - d_write))
                    else:
                        residuals.append(ResidualPoint(start, width // 1_000_000_000, None, None, None))
            return Series(app_id, window, tier, tuple(points), tuple(device_points), tuple(residuals), coverage)

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        self._precheck(window)
        with self._snapshot():
            tier, _ = self._scope(window)
            app_id = validate_sqlite_integer(app_id, "app_id")
            if app_id <= 0:
                raise ValueError("app_id must be positive")
            data, coverage = self._query_app_data(app_id, window, tier, True)
            device_rows = tuple(
                DeviceShareRow(boot, dev, values[0], values[1], values[0] + values[1])
                for (boot, dev), values in sorted(data["devices"].items(), key=lambda item: (item[0][0].encode("utf-8"), item[0][1].encode("utf-8")))
            )
            task_read, task_write = data["read"], data["write"]
            if coverage.device_state == "available":
                device_read = sum(row.rbytes for row in device_rows)
                device_write = sum(row.wbytes for row in device_rows)
                residual_read, residual_write = task_read - device_read, task_write - device_write
                device_total = device_read + device_write
                residual_total = residual_read + residual_write
            else:
                device_read = device_write = device_total = None
                residual_read = residual_write = residual_total = None
            return DiskShare(
                app_id, window, task_read, task_write, task_read + task_write, device_rows,
                device_read, device_write, device_total, residual_read, residual_write,
                residual_total, coverage,
            )

    def _file_scope(self, window: Window, cutoff: int | None) -> tuple[str, str | None, int | None, int | None]:
        if window.kind == "lifetime":
            return "lifetime", None, None, None
        boot_id = window.boot_id
        exact_boot = window.kind == "since_boot" and cutoff is not None and window.end_mono_ns == cutoff
        if exact_boot:
            first_file_hour = self.connection.execute(
                "SELECT MIN(ts_mono_ns) FROM file_overflow_unknown WHERE boot_id=?", (boot_id,)
            ).fetchone()[0]
            if first_file_hour is not None and first_file_hour > 0:
                return "hourly", boot_id, 0, window.end_mono_ns
            return "boot", boot_id, 0, window.end_mono_ns
        _, start, end = _bounds(window)
        return "hourly", boot_id, start, end

    def _file_coverage_rows(self, scope: str, boot_id: str | None, start: int | None, end: int | None, app_id: int):
        if scope == "lifetime":
            return self.connection.execute("SELECT boot_id,coverage_json FROM app_boot_file_coverage WHERE app_id=?", (app_id,)).fetchall()
        if scope == "boot":
            return self.connection.execute("SELECT coverage_json FROM app_boot_file_coverage WHERE boot_id=? AND app_id=?", (boot_id, app_id)).fetchall()
        first, last = _ceil_bucket(start, FILE_HOUR_NS), (end // FILE_HOUR_NS) * FILE_HOUR_NS
        if first >= last:
            return []
        return self.connection.execute(
            "SELECT ts_mono_ns,coverage_json FROM file_coverage WHERE boot_id=? AND app_id=? AND ts_mono_ns>=? AND ts_mono_ns<? ORDER BY ts_mono_ns",
            (boot_id, app_id, first, last),
        ).fetchall()

    def _file_reduced(self, rows) -> dict:
        reduced = _empty_coverage() | {"device_states": [], "spans_suspend": False}
        for row in rows:
            _reduce_coverage(reduced, row[-1])
        return reduced

    def _unknown_owner(self, window: Window, scope: str, boot_id: str | None, start: int | None, end: int | None) -> UnknownOwnerOverflow:
        if scope == "lifetime":
            rows = self.connection.execute(
                "SELECT b.boot_order,u.boot_id,NULL,u.overflow_files,u.overflow_bytes_r,u.overflow_bytes_w,u.coverage_json "
                "FROM boot_file_overflow_unknown u JOIN boot b USING(boot_id) ORDER BY b.boot_order"
            ).fetchall()
        elif scope == "boot":
            rows = self.connection.execute(
                "SELECT b.boot_order,u.boot_id,NULL,u.overflow_files,u.overflow_bytes_r,u.overflow_bytes_w,u.coverage_json "
                "FROM boot_file_overflow_unknown u JOIN boot b USING(boot_id) WHERE u.boot_id=?", (boot_id,)
            ).fetchall()
        else:
            first, last = _ceil_bucket(start, FILE_HOUR_NS), (end // FILE_HOUR_NS) * FILE_HOUR_NS
            rows = [] if first >= last else self.connection.execute(
                "SELECT b.boot_order,u.boot_id,u.ts_mono_ns,u.overflow_files,u.overflow_bytes_r,u.overflow_bytes_w,u.coverage_json "
                "FROM file_overflow_unknown u JOIN boot b USING(boot_id) WHERE u.boot_id=? AND u.ts_mono_ns>=? AND u.ts_mono_ns<? ORDER BY u.ts_mono_ns",
                (boot_id, first, last),
            ).fetchall()
        reduced = _empty_coverage() | {"device_states": [], "spans_suspend": False}
        files = rb = wb = 0
        for _, _, _, count, read_bytes, write_bytes, cov in rows:
            files += count
            rb += read_bytes
            wb += write_bytes
            _reduce_coverage(reduced, cov)
        present_hours = {row[2] for row in rows} if scope == "hourly" else set()
        base_complete, reason = self._file_availability(
            scope, boot_id, start, end, present_hours, bool(rows)
        )
        if scope == "lifetime":
            required_boots = {row[0] for row in self.connection.execute(
                "SELECT boot_id FROM boot WHERE file_finalized_through_mono_ns>0"
            )}
            if not required_boots.issubset({row[1] for row in rows}):
                base_complete, reason = False, "file_source_unavailable"
        overlays_complete = self._file_overlays(reduced, scope, boot_id, start, end, None)
        base_complete = base_complete and overlays_complete
        if reason == "not_retained":
            files = rb = wb = 0
        tier: Tier = "raw" if scope == "hourly" else "totals_only"
        coverage = _model_coverage(reduced, tier, base_complete=base_complete, unavailable_reason=reason)
        covered_window = window
        if scope == "hourly" and reason is None:
            first = _ceil_bucket(start, FILE_HOUR_NS)
            last = (end // FILE_HOUR_NS) * FILE_HOUR_NS
            watermark = self.connection.execute(
                "SELECT file_finalized_through_mono_ns FROM boot WHERE boot_id=?", (boot_id,)
            ).fetchone()[0]
            retained_end = max(first, min(last, watermark))
            prefix_start = _missing_file_prefix_start(
                start, end, watermark, present_hours,
                self.connection.execute(
                    "SELECT MIN(ts_mono_ns) FROM file_overflow_unknown WHERE boot_id=?",
                    (boot_id,),
                ).fetchone()[0],
            )
            if prefix_start is not None:
                covered_window = Window(
                    "range", boot_id, start_mono_ns=prefix_start, end_mono_ns=retained_end
                )
        return UnknownOwnerOverflow(covered_window, "overflow_owner_unknown", files, rb, wb, coverage)

    def _file_rows(self, app_id: int, scope: str, boot_id: str | None, start: int | None, end: int | None) -> list[dict]:
        if scope == "lifetime":
            sql = (
                "SELECT t.path_id,p.basename,p.state,p.identity_weak,p.dev,p.inode,p.generation,t.reads,t.writes,t.bytes_r,t.bytes_w,"
                "t.last_seen_mono_ns,t.boot_id,b.boot_order FROM app_boot_file_total t JOIN path p USING(path_id) JOIN boot b USING(boot_id) "
                "WHERE t.app_id=? ORDER BY p.path_id,b.boot_order"
            )
            params = (app_id,)
        elif scope == "boot":
            sql = (
                "SELECT t.path_id,p.basename,p.state,p.identity_weak,p.dev,p.inode,p.generation,t.reads,t.writes,t.bytes_r,t.bytes_w,"
                "t.last_seen_mono_ns,t.boot_id,b.boot_order FROM app_boot_file_total t JOIN path p USING(path_id) JOIN boot b USING(boot_id) "
                "WHERE t.boot_id=? AND t.app_id=? ORDER BY p.path_id"
            )
            params = (boot_id, app_id)
        else:
            first, last = _ceil_bucket(start, FILE_HOUR_NS), (end // FILE_HOUR_NS) * FILE_HOUR_NS
            sql = (
                "SELECT f.path_id,p.basename,p.state,p.identity_weak,f.dev,p.inode,p.generation,f.reads,f.writes,f.bytes_r,f.bytes_w,"
                "f.last_seen_mono_ns,f.boot_id,b.boot_order FROM file_sample f JOIN path p USING(path_id) JOIN boot b USING(boot_id) "
                "WHERE f.boot_id=? AND f.app_id=? AND f.ts_mono_ns>=? AND f.ts_mono_ns<? ORDER BY f.path_id,f.ts_mono_ns"
            )
            params = (boot_id, app_id, first, last)
        grouped = {}
        for row in self.connection.execute(sql, params):
            (path_id, basename, state, weak, dev, inode, generation, reads, writes,
             bytes_r, bytes_w, last_seen, row_boot, boot_order) = row
            item = grouped.setdefault(path_id, {
                "path_id": path_id, "basename": basename, "state": state, "identity_weak": bool(weak),
                "dev": dev, "inode": inode, "generation": generation, "reads": 0, "writes": 0,
                "bytes_r": 0, "bytes_w": 0, "last_seen_boot_id": row_boot,
                "last_seen_mono_ns": last_seen, "boot_order": boot_order,
            })
            item["reads"] += reads
            item["writes"] += writes
            item["bytes_r"] += bytes_r
            item["bytes_w"] += bytes_w
            if (boot_order, last_seen) > (item["boot_order"], item["last_seen_mono_ns"]):
                item["last_seen_boot_id"] = row_boot
                item["last_seen_mono_ns"] = last_seen
                item["boot_order"] = boot_order
        return list(grouped.values())

    def _file_overlays(self, reduced: dict, scope: str, boot_id: str | None, start: int | None,
                       end: int | None, app_id: int | None) -> bool:
        complete = self._gap_overlay(reduced, boot_id, start, end)
        if self._diagnostics(boot_id, start or 0, end, app_id, FILE_HOUR_NS):
            reduced["counter_overflow"] = 1
            complete = False
        if reduced.get("invalid"):
            complete = False
        return complete

    def _pending_lifetime_file_tail(self) -> bool:
        if self.connection.execute("SELECT 1 FROM file_hour_state LIMIT 1").fetchone() is not None:
            return True
        boots = self.connection.execute(
            "SELECT boot_id,file_finalized_through_mono_ns FROM boot ORDER BY boot_order"
        ).fetchall()
        return any(
            (cutoff := self._available_cutoff(boot_id)) is not None and cutoff > watermark
            for boot_id, watermark in boots
        )

    def _file_availability(self, scope: str, boot_id: str | None, start: int | None,
                           end: int | None, present_hours: set[int], has_rows: bool,
                           *, allow_missing_prefix: bool = True):
        if scope == "lifetime":
            pending = self._pending_lifetime_file_tail()
            reason = "file_source_unavailable" if pending or not has_rows else None
            return has_rows and not pending, reason
        row = self.connection.execute(
            "SELECT file_finalized_through_mono_ns FROM boot WHERE boot_id=?", (boot_id,)
        ).fetchone()
        if row is None:
            return False, "file_source_unavailable"
        watermark = row[0]
        pending = end > watermark
        if scope == "boot":
            reason = "file_source_unavailable" if pending or not has_rows else None
            return has_rows and not pending, reason
        first = _ceil_bucket(start, FILE_HOUR_NS)
        last = (end // FILE_HOUR_NS) * FILE_HOUR_NS
        retained_end = max(first, min(last, watermark))
        expected_finalized = (retained_end - first) // FILE_HOUR_NS
        present_finalized = sum(first <= hour < retained_end for hour in present_hours)
        collection_start = self.connection.execute(
            "SELECT MIN(ts_mono_ns) FROM file_overflow_unknown WHERE boot_id=?", (boot_id,)
        ).fetchone()[0]
        missing_prefix = (
            _missing_file_prefix_start(
                start, end, watermark, present_hours, collection_start
            )
            if allow_missing_prefix and present_finalized < expected_finalized else None
        )
        if present_finalized < expected_finalized:
            if missing_prefix is None:
                return False, "not_retained"
        if pending or not has_rows:
            reason = "unsupported_window" if not pending and first >= last else "file_source_unavailable"
            return False, reason
        clipped = bool(start % FILE_HOUR_NS or end % FILE_HOUR_NS)
        return not clipped and missing_prefix is None, None

    def _file_app_coverage(self, app_id: int, window: Window, scope: str, boot_id: str | None, start: int | None, end: int | None):
        rows = self._file_coverage_rows(scope, boot_id, start, end, app_id)
        reduced = self._file_reduced(rows)
        base, reason = self._file_availability(
            scope, boot_id, start, end,
            {row[0] for row in rows} if scope == "hourly" else set(), bool(rows),
            allow_missing_prefix=False,
        )
        if scope == "lifetime":
            required_boots = {row[0] for row in self.connection.execute(
                "SELECT DISTINCT boot_id FROM app_boot_file_total WHERE app_id=?", (app_id,)
            )}
            if not required_boots.issubset({row[0] for row in rows}):
                base, reason = False, "file_source_unavailable"
        overlays_complete = self._file_overlays(reduced, scope, boot_id, start, end, app_id)
        return reduced, base and overlays_complete, reason

    def _file_query_context(self, window: Window):
        self._precheck(window)
        _, cutoff = self._scope(window)
        scope, boot_id, start, end = self._file_scope(window, cutoff)
        tier: Tier = "raw" if scope == "hourly" else "totals_only"
        return tier, scope, boot_id, start, end

    def file_applications(self, window: Window) -> tuple[tuple[str, str, int], ...]:
        """Return apps with retained file observations in the requested window."""

        self._precheck(window)
        with self._snapshot():
            _, scope, boot_id, start, end = self._file_query_context(window)
            if scope == "lifetime":
                rows = self.connection.execute(
                    "SELECT a.key,a.name,t.bytes_r,t.bytes_w "
                    "FROM app_boot_file_total t JOIN app a USING(app_id) "
                    "ORDER BY a.key"
                ).fetchall()
            elif scope == "boot":
                rows = self.connection.execute(
                    "SELECT a.key,a.name,t.bytes_r,t.bytes_w "
                    "FROM app_boot_file_total t JOIN app a USING(app_id) "
                    "WHERE t.boot_id=? ORDER BY a.key",
                    (boot_id,),
                ).fetchall()
            else:
                unknown = self._unknown_owner(window, scope, boot_id, start, end)
                query_start = max(start, unknown.window.start_mono_ns) \
                    if unknown.window != window and unknown.window.kind == "range" else start
                first = _ceil_bucket(query_start, FILE_HOUR_NS)
                last = (end // FILE_HOUR_NS) * FILE_HOUR_NS
                if first >= last:
                    return ()
                rows = self.connection.execute(
                    "SELECT a.key,a.name,f.bytes_r,f.bytes_w "
                    "FROM file_sample f JOIN app a USING(app_id) "
                    "WHERE f.boot_id=? AND f.ts_mono_ns>=? AND f.ts_mono_ns<? "
                    "ORDER BY a.key",
                    (boot_id, first, last),
                ).fetchall()
            totals = {}
            for key, name, bytes_r, bytes_w in rows:
                aggregate = totals.setdefault(key, [name, 0])
                aggregate[1] += bytes_r + bytes_w
            return tuple(
                (key, name, total) for key, (name, total) in sorted(totals.items())
            )

    def file_table(self, app_id: int, window: Window, sort: FileSort, direction: Direction, search: str | None) -> FileTable:
        self._precheck(window)
        sort = validate_enum(sort, _FILE_SORTS, "sort")
        direction = validate_enum(direction, _DIRECTIONS, "direction")
        search = validate_search(search)
        with self._snapshot():
            tier, scope, boot_id, start, end = self._file_query_context(window)
            app_id = validate_sqlite_integer(app_id, "app_id")
            if app_id <= 0:
                raise ValueError("app_id must be positive")
            self._require_app(app_id)
            unknown = self._unknown_owner(window, scope, boot_id, start, end)
            query_start = max(start, unknown.window.start_mono_ns) \
                if scope == "hourly" and unknown.window != window and unknown.window.kind == "range" else start
            reduced, base, reason = self._file_app_coverage(
                app_id, window, scope, boot_id, query_start, end
            )
            coverage_reason = _unavailable_reason(reason, unknown.coverage.unavailable_reason)
            base = base and unknown.coverage.complete
            coverage = _model_coverage(
                reduced, tier, base_complete=base, unavailable_reason=coverage_reason,
                require_file=True,
                unknown_owner_files=(unknown.overflow_files or unknown.overflow_bytes_r or unknown.overflow_bytes_w),
            )
            raw_rows = (
                self._file_rows(app_id, scope, boot_id, query_start, end)
                if reason != "not_retained" and reduced["device_states"] else []
            )
            normalized_search = unicodedata.normalize("NFC", search).casefold() if search else None
            if normalized_search:
                raw_rows = [row for row in raw_rows if normalized_search in unicodedata.normalize("NFC", row["basename"]).casefold()]
            def primary(row):
                values = {
                    "bytes_total": row["bytes_r"] + row["bytes_w"],
                    "bytes_r": row["bytes_r"], "bytes_w": row["bytes_w"],
                    "reads": row["reads"], "writes": row["writes"],
                    "basename": unicodedata.normalize("NFC", row["basename"]),
                    "dev": row["dev"].encode("utf-8"),
                    "last_seen": (row["boot_order"], row["last_seen_mono_ns"]),
                }
                value = values[sort]
                return value if direction == "asc" else _Descending(value)
            raw_rows.sort(key=lambda row: (
                primary(row), row["dev"].encode("utf-8"), row["inode"], row["generation"],
                row["basename"].encode("utf-8"), row["path_id"],
            ))
            rows = tuple(FileRow(
                row["path_id"], row["basename"], row["state"], row["identity_weak"], row["dev"],
                row["inode"], row["generation"], row["reads"], row["writes"], row["bytes_r"],
                row["bytes_w"], row["bytes_r"] + row["bytes_w"], row["last_seen_boot_id"], row["last_seen_mono_ns"],
            ) for row in raw_rows)
            return FileTable(app_id, window, rows, sort, direction, search, unknown, coverage)

    def file_coverage(self, app_id: int, window: Window) -> FileCoverage:
        self._precheck(window)
        with self._snapshot():
            tier, scope, boot_id, start, end = self._file_query_context(window)
            app_id = validate_sqlite_integer(app_id, "app_id")
            if app_id <= 0:
                raise ValueError("app_id must be positive")
            self._require_app(app_id)
            unknown = self._unknown_owner(window, scope, boot_id, start, end)
            query_start = max(start, unknown.window.start_mono_ns) \
                if scope == "hourly" and unknown.window != window and unknown.window.kind == "range" else start
            reduced, base, reason = self._file_app_coverage(
                app_id, window, scope, boot_id, query_start, end
            )
            coverage_reason = _unavailable_reason(reason, unknown.coverage.unavailable_reason)
            coverage = _model_coverage(
                reduced, tier, base_complete=base and unknown.coverage.complete,
                unavailable_reason=coverage_reason, require_file=True,
                unknown_owner_files=(unknown.overflow_files or unknown.overflow_bytes_r or unknown.overflow_bytes_w),
            )
            rows = (
                self._file_rows(app_id, scope, boot_id, query_start, end)
                if reason != "not_retained" and reduced["device_states"] else []
            )
            file_values = reduced["file"]
            return FileCoverage(
                app_id, window,
                file_values["counted_omitted_files"], file_values["counted_omitted_bytes_r"],
                file_values["counted_omitted_bytes_w"], file_values["uncounted_overflow_files"],
                file_values["uncounted_overflow_bytes_r"], file_values["uncounted_overflow_bytes_w"],
                file_values["skipped_nonfile"], file_values["skipped_unsupported_fs"],
                file_values["weak_identity_rows"], HookHits(**file_values["hook_hits"]),
                sum(row["bytes_r"] for row in rows), sum(row["bytes_w"] for row in rows), unknown, coverage,
            )

    def unknown_owner_file_coverage(self, window: Window) -> UnknownOwnerOverflow:
        self._precheck(window)
        with self._snapshot():
            _, scope, boot_id, start, end = self._file_query_context(window)
            return self._unknown_owner(window, scope, boot_id, start, end)


class _Descending:
    """Reverse only the primary sort value while retaining ascending tie keys."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        if not isinstance(other, _Descending):
            return NotImplemented
        return self.value == other.value

    def __lt__(self, other):
        return self.value > other.value


__all__ = ["QueryService", "select_tier"]
