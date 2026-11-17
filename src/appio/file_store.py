"""Atomic hourly file materialization and retained-observation boot totals.

The snapshot returned by HourlyAccumulator carries the canonical all-candidate
state, receipts and captured-name endpoints needed to persist it. The public
``apps`` field retains the E-1 (app_key, observations, coverage) triple shape.
No collector, filesystem lookup, or path reconstruction happens here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import copy
import json
import sqlite3

from appio.contracts import (
    CounterOverflowError, FILE_HOUR_NS, FileCoverageInput, HourlyFileSnapshot,
    UnknownOwnerInput, validate_sqlite_integer,
)
from appio.durable import (
    FileHourState, Receipt, canonical_json, decode_file_hour_state,
)
from appio.files import (
    CapturedName, FILE_COUNTERS, HOOKS, HourlyAccumulator, ZERO_COVERAGE,
    checked_sum,
)

FILE_COLUMNS = tuple(name for name in FILE_COUNTERS if name not in ("gap_intervals", "overlap_uncertain_intervals"))

#: Bounded memo of validated coverage JSON: one frame's text is shared by every
#: app in it, and a retention pass re-examines the same hours many times.
_DECODED_COVERAGE: dict[str, dict] = {}
_DECODED_COVERAGE_MAX = 512


@contextmanager
def _atomic(connection: sqlite3.Connection):
    """Own the transaction, or join a frame without publishing its caller's work."""
    nested = connection.in_transaction
    connection.execute("SAVEPOINT appio_file_hour" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("RELEASE appio_file_hour" if nested else "COMMIT")
    except BaseException:
        if nested:
            connection.execute("ROLLBACK TO appio_file_hour")
            connection.execute("RELEASE appio_file_hour")
        elif connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def coverage_object(coverage: FileCoverageInput = ZERO_COVERAGE) -> dict:
    """The exact persisted section-6.1 shape, with independent file evidence."""
    zero_vector = dict(rchar=0, wchar=0, read_bytes=0, write_bytes=0)
    return dict(inaccessible_pids=0, resets=0, reap_corrections=0,
                reap_unapplied=dict(zero_vector), reap_unobserved=dict(zero_vector),
                partial_intervals=0,
                gap_intervals=max(coverage.gap_intervals, int(not coverage.complete)),
                overlap_uncertain_intervals=coverage.overlap_uncertain_intervals,
                spans_suspend_intervals=0, reap_incomplete=0, counter_overflow=0,
                device_state="incomplete",
                file={**{name: getattr(coverage, name) for name in FILE_COLUMNS},
                      "hook_hits": asdict(coverage.hook_hits)})


def _decode_coverage(text: str) -> dict:
    """Validate every exact key and integer before reusing persisted coverage.

    The same JSON is duplicated across every app in a frame and re-read for every
    unit a retention pass touches, so validating it again dominates the pass: the
    walk plus the canonical re-encode cost ~900 us against ~10 us to copy the
    result. Cached decodes are handed back as independent copies because callers
    (``_preserve_file_gap``) mutate what they decode.
    """
    cached = _DECODED_COVERAGE.get(text)
    if cached is not None:
        return copy.deepcopy(cached)

    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate coverage key")
            obj[key] = value
        return obj
    obj = json.loads(text, object_pairs_hook=unique)
    model = coverage_object()
    if not isinstance(obj, dict) or obj.keys() != model.keys():
        raise ValueError("coverage_json requires exactly the section-6.1 keys")
    def check(value, shape, path):
        if isinstance(shape, dict):
            if not isinstance(value, dict) or value.keys() != shape.keys():
                raise ValueError(f"invalid coverage object: {path}")
            for key in shape:
                check(value[key], shape[key], path + "." + key)
        elif isinstance(shape, int):
            validate_sqlite_integer(value, path)
            if path in ("coverage.reap_incomplete", "coverage.counter_overflow") and value not in (0, 1):
                raise ValueError(f"invalid flag: {path}")
        elif value not in ("available", "not_delegated", "incomplete"):
            raise ValueError("invalid device_state")
    check(obj, model, "coverage")
    if canonical_json(obj) != text:
        raise ValueError("coverage_json is not canonical")
    if len(_DECODED_COVERAGE) >= _DECODED_COVERAGE_MAX:
        _DECODED_COVERAGE.clear()
    _DECODED_COVERAGE[text] = obj
    return copy.deepcopy(obj)


def _merge_coverage_json(old_text: str | None, incoming: dict) -> str:
    if old_text is None:
        return canonical_json(incoming)
    old = _decode_coverage(old_text)
    def merge(a, b, name):
        if isinstance(a, dict):
            return {key: merge(a[key], b[key], key) for key in a}
        if isinstance(a, str):
            return a if a == b else "incomplete"
        if name in ("reap_incomplete", "counter_overflow"):
            return int(bool(a or b))
        return checked_sum(a, b, field=name)
    return canonical_json(merge(old, incoming, "coverage"))


class PendingStateRegressionError(ValueError):
    """A cumulative write would discard already committed pending evidence."""


class FileHourStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def _watermark(self, boot_id: str) -> int:
        row = self.connection.execute("SELECT file_finalized_through_mono_ns FROM boot WHERE boot_id=?", (boot_id,)).fetchone()
        if row is None:
            raise ValueError("unknown boot_id")
        return row[0]

    def _existing(self, boot: str, hour: int) -> FileHourState | None:
        row = self.connection.execute("SELECT state_json FROM file_hour_state WHERE boot_id=? AND ts_mono_ns=?", (boot, hour)).fetchone()
        return None if row is None else decode_file_hour_state(row[0], boot, hour)

    @staticmethod
    def _require_extension(previous: FileHourState, incoming: FileHourState) -> None:
        """A cumulative checkpoint can extend committed evidence, never replace it."""
        def nondecreasing(old, new, counters):
            if any(getattr(new, name) < getattr(old, name) for name in counters):
                raise PendingStateRegressionError("pending state must not regress counters")

        def receipt(old, new):
            if old is not None and (new is None or
                    (old.source == new.source and (new.epoch, new.seq) < (old.epoch, old.seq))):
                raise PendingStateRegressionError("pending state must not regress receipts")

        def coverage(old, new):
            nondecreasing(old, new, FILE_COUNTERS)
            nondecreasing(old.hook_hits, new.hook_hits, HOOKS)
            if old.complete < new.complete:
                raise PendingStateRegressionError("pending state must not regress incompleteness evidence")

        apps = {app.app_key: app for app in incoming.apps}
        for old in previous.apps:
            if old.app_key not in apps:
                raise PendingStateRegressionError("pending state must not regress app candidates")
            new = apps[old.app_key]
            receipt(old.last_receipt, new.last_receipt)
            coverage(old.coverage, new.coverage)
            entries = {entry.identity: entry for entry in new.files}
            for entry in old.files:
                candidate = entries.get(entry.identity)
                if candidate is None or not {entry.basename, *entry.aliases} <= {candidate.basename, *candidate.aliases}:
                    raise PendingStateRegressionError("pending state must not regress file candidates or names")
                nondecreasing(entry, candidate, ("reads", "writes", "bytes_r", "bytes_w", "last_seen_mono_ns"))
        old, new = previous.unknown_owner, incoming.unknown_owner
        receipt(old.last_receipt, new.last_receipt)
        nondecreasing(old, new, ("overflow_files", "overflow_bytes_r", "overflow_bytes_w"))
        nondecreasing(old.coverage, new.coverage, ("gap_intervals", "overlap_uncertain_intervals"))
        if old.coverage.complete < new.coverage.complete:
            raise PendingStateRegressionError("pending state must not regress unknown-owner evidence")

    def _snapshot(self, snapshot: HourlyFileSnapshot) -> tuple[FileHourState, tuple[CapturedName, ...], int]:
        if not isinstance(snapshot, HourlyFileSnapshot):
            raise ValueError("snapshot must be an HourlyFileSnapshot")
        previous = self._existing(snapshot.boot_id, snapshot.hour_start_mono_ns)
        state = getattr(snapshot, "pending_state", previous)
        if state is None:
            raise ValueError("snapshot needs authoritative receipts and all-candidate pending_state")
        state = decode_file_hour_state(canonical_json(state), snapshot.boot_id, snapshot.hour_start_mono_ns)
        if previous is not None:
            self._require_extension(previous, state)
        top_n = getattr(snapshot, "top_n", 100)
        expected = HourlyAccumulator.from_state(state).finalize(top_n)
        def values(apps):
            return tuple((key, tuple(asdict(row) for row in rows), cov) for key, rows, cov in apps)
        if values(snapshot.apps) != values(expected.apps) or snapshot.unknown_owner != expected.unknown_owner:
            raise ValueError("snapshot does not match its canonical all-candidate state")
        return state, getattr(snapshot, "captured_names", ()), top_n

    def _app_id(self, key: str) -> int:
        row = self.connection.execute("SELECT app_id FROM app WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"file app must be registered before materialization: {key}")
        return row[0]

    def _endpoint_key(self, boot: str, mono: int) -> tuple[int, int]:
        row = self.connection.execute("SELECT boot_order FROM boot WHERE boot_id=?", (boot,)).fetchone()
        if row is None:
            raise ValueError("endpoint refers to unknown boot")
        return row[0], mono

    def _endpoints(self, previous, boot, first, last):
        if previous is None:
            return boot, first, boot, last
        fb, ft, lb, lt = previous
        if self._endpoint_key(boot, first) < self._endpoint_key(fb, ft):
            fb, ft = boot, first
        if self._endpoint_key(boot, last) > self._endpoint_key(lb, lt):
            lb, lt = boot, last
        return fb, ft, lb, lt

    def _paths(self, state: FileHourState, captured: tuple[CapturedName, ...]) -> dict[tuple, int]:
        metadata = {name.path_key: name for name in captured}
        expected = {}
        for app in state.apps:
            for entry in app.files:
                for basename in (entry.basename, *entry.aliases):
                    key = (entry.fs_uuid, entry.dev, entry.inode, entry.generation, basename, entry.identity_weak)
                    expected[key] = entry
        ids = {}
        def sort_key(key):
            return (key[0].encode("utf-8"), key[1].encode("utf-8"), key[2], key[3], key[4].encode("utf-8"), key[5])
        for key in sorted(expected, key=sort_key):
            row = self.connection.execute("SELECT path_id,first_boot_id,first_mono_ns,last_boot_id,last_mono_ns,state FROM path WHERE fs_uuid=? AND dev=? AND inode=? AND generation=? AND basename=? AND identity_weak=?", key).fetchone()
            name = metadata.get(key)
            if row is None and name is None:
                raise ValueError("captured-name timestamps required for a new pending path")
            if row is None:
                path_id = checked_sum(self.connection.execute("SELECT COALESCE(MAX(path_id),0) FROM path").fetchone()[0], 1, field="path_id")
                self.connection.execute("INSERT INTO path VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (path_id, *key[:5], name.state, key[5], state.boot_id, name.first_mono_ns, state.boot_id, name.last_mono_ns))
            else:
                path_id = row[0]
                if name is not None:
                    endpoints = self._endpoints(row[1:5], state.boot_id, name.first_mono_ns, name.last_mono_ns)
                    path_state = max((row[5], name.state), key=("resolved", "truncated", "unresolved").index)
                    self.connection.execute("UPDATE path SET first_boot_id=?,first_mono_ns=?,last_boot_id=?,last_mono_ns=?,state=? WHERE path_id=?", (*endpoints, path_state, path_id))
            ids[key] = path_id
        for app in state.apps:
            for entry in app.files:
                canonical = (entry.fs_uuid, entry.dev, entry.inode, entry.generation, entry.basename, entry.identity_weak)
                path_id = ids[canonical]
                self.connection.execute("UPDATE path SET state=? WHERE path_id=? AND CASE state WHEN 'unresolved' THEN 2 WHEN 'truncated' THEN 1 ELSE 0 END < ?", (entry.state, path_id, ("resolved", "truncated", "unresolved").index(entry.state)))
                for alias in entry.aliases:
                    alias_key = (*canonical[:4], alias, entry.identity_weak)
                    name = metadata.get(alias_key)
                    prior = self.connection.execute("SELECT first_boot_id,first_mono_ns,last_boot_id,last_mono_ns FROM path_alias WHERE path_id=? AND basename=?", (path_id, alias)).fetchone()
                    if name is not None:
                        endpoints = self._endpoints(prior, state.boot_id, name.first_mono_ns, name.last_mono_ns)
                    elif prior:
                        endpoints = tuple(prior)
                    else:
                        endpoints = tuple(self.connection.execute("SELECT first_boot_id,first_mono_ns,last_boot_id,last_mono_ns FROM path WHERE path_id=?", (ids[alias_key],)).fetchone())
                    self.connection.execute("INSERT INTO path_alias VALUES(?,?,?,?,?,?) ON CONFLICT(path_id,basename) DO UPDATE SET first_boot_id=excluded.first_boot_id,first_mono_ns=excluded.first_mono_ns,last_boot_id=excluded.last_boot_id,last_mono_ns=excluded.last_mono_ns", (path_id, alias, *endpoints))
        return ids

    @contextmanager
    def _overflow_boundary(self, snapshot):
        try:
            yield
        except CounterOverflowError as error:
            if not self.connection.in_transaction:
                self.record_overflow(snapshot, error)
            raise

    def record_overflow(self, snapshot: HourlyFileSnapshot, error: CounterOverflowError) -> None:
        """Persist A21 evidence after the owner rolls back its complete frame.

        Standalone operations call this automatically. A caller joining pending
        state to a wider frame must roll that frame back, then call this method.
        """
        if self.connection.in_transaction:
            raise ValueError("overflow diagnostics require the frame transaction to be rolled back")
        if not isinstance(error, CounterOverflowError):
            raise ValueError("a CounterOverflowError is required")
        self._diagnostic(snapshot, error.field)

    def apply_pending(self, snapshot: HourlyFileSnapshot) -> bool:
        """Replace the whole cumulative pending state inside the caller's frame."""
        with self._overflow_boundary(snapshot), _atomic(self.connection):
            if snapshot.hour_start_mono_ns < self._watermark(snapshot.boot_id):
                return False
            state, names, _ = self._snapshot(snapshot)
            self._paths(state, names)
            self.connection.execute("INSERT INTO file_hour_state VALUES(?,?,?) ON CONFLICT(boot_id,ts_mono_ns) DO UPDATE SET state_json=excluded.state_json", (state.boot_id, state.hour_start_mono_ns, canonical_json(state)))
        return True

    def quarantine_pending(self, boot_id: str, hour: int) -> None:
        """Atomically trade one incompatible restored checkpoint for its gap.

        This is recovery of durable state, not permission for live cumulative
        writes to regress. Keep receipts for provenance, but retain none of the
        unmergeable counters and do not advance the finalization watermark.
        """
        with _atomic(self.connection):
            state = self._existing(boot_id, hour)
            if state is None:
                raise ValueError("quarantine requires an existing pending hour")
            receipts = [app.last_receipt for app in state.apps]
            if state.unknown_owner.last_receipt is not None:
                receipts.append(state.unknown_owner.last_receipt)
            receipt = max(receipts, key=lambda r: (r.epoch, r.seq, r.source))
            end = checked_sum(hour, FILE_HOUR_NS, field="quarantined_hour_end")
            self.connection.execute(
                "INSERT INTO gap(boot_id,source,epoch,seq,from_mono_ns,to_mono_ns,reason) "
                "VALUES(?,?,?,?,?,?,?)",
                (boot_id, receipt.source, receipt.epoch, receipt.seq,
                 hour, end, "collector_gap"))
            self.connection.execute(
                "DELETE FROM file_hour_state WHERE boot_id=? AND ts_mono_ns=?",
                (boot_id, hour))

    def finalize_hour(self, snapshot: HourlyFileSnapshot) -> bool:
        """Finalize once; atomic totals/coverage/state removal/watermark update."""
        with self._overflow_boundary(snapshot):
            with _atomic(self.connection):
                boot, hour = snapshot.boot_id, snapshot.hour_start_mono_ns
                if hour < self._watermark(boot):
                    return False
                state, names, _ = self._snapshot(snapshot)
                earlier = self.connection.execute("SELECT 1 FROM file_hour_state WHERE boot_id=? AND ts_mono_ns<? LIMIT 1", (boot, hour)).fetchone()
                if earlier:
                    raise ValueError("file hours must finalize in chronological order")
                checked_sum(hour, FILE_HOUR_NS, field="file_finalized_through_mono_ns")
                ids = self._paths(state, names)
                receipts = {app.app_key: app.last_receipt for app in state.apps}
                for app_key, observations, coverage in snapshot.apps:
                    app_id = self._app_id(app_key)
                    receipt = receipts[app_key]
                    source_key = (boot, receipt.source, receipt.epoch, receipt.seq, app_id, hour)
                    obj = coverage_object(coverage)
                    columns = tuple(getattr(coverage, name) for name in FILE_COLUMNS)
                    hooks = tuple(getattr(coverage.hook_hits, name) for name in HOOKS)
                    values = (*source_key, canonical_json(obj), *columns, *hooks)
                    self.connection.execute("INSERT INTO file_coverage VALUES(" + ",".join("?" for _ in values) + ")", values)
                    previous_cov = self.connection.execute("SELECT coverage_json FROM app_boot_file_coverage WHERE boot_id=? AND app_id=?", (boot, app_id)).fetchone()
                    merged_json = _merge_coverage_json(previous_cov[0] if previous_cov else None, obj)
                    self.connection.execute("INSERT INTO app_boot_file_coverage VALUES(?,?,?) ON CONFLICT(boot_id,app_id) DO UPDATE SET coverage_json=excluded.coverage_json", (boot, app_id, merged_json))
                    for row in observations:
                        path_id = ids[(row.fs_uuid, row.dev, row.inode, row.generation, row.basename, int(row.identity_weak))]
                        self.connection.execute("INSERT INTO file_sample VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*source_key, row.dev, path_id, row.reads, row.writes, row.bytes_r, row.bytes_w, row.last_seen_mono_ns))
                        previous = self.connection.execute("SELECT bytes_r,bytes_w,reads,writes,last_seen_mono_ns FROM app_boot_file_total WHERE boot_id=? AND app_id=? AND path_id=?", (boot, app_id, path_id)).fetchone()
                        counters = (row.bytes_r, row.bytes_w, row.reads, row.writes)
                        if previous:
                            counters = tuple(checked_sum(old, new, field=name) for old, new, name in zip(previous[:4], counters, ("bytes_r", "bytes_w", "reads", "writes")))
                        last_seen = max(previous[4], row.last_seen_mono_ns) if previous else row.last_seen_mono_ns
                        self.connection.execute("INSERT INTO app_boot_file_total VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(boot_id,app_id,path_id) DO UPDATE SET bytes_r=excluded.bytes_r,bytes_w=excluded.bytes_w,reads=excluded.reads,writes=excluded.writes,last_seen_mono_ns=excluded.last_seen_mono_ns", (boot, app_id, path_id, *counters, last_seen))
                unknown = snapshot.unknown_owner
                receipt = state.unknown_owner.last_receipt
                if receipt is None:
                    receipt = max(receipts.values(), key=lambda r: (r.epoch, r.seq, r.source.encode()), default=None)
                if receipt is None:
                    raise ValueError("a monitored hour requires at least one authoritative receipt")
                unknown_cov = coverage_object(replace(ZERO_COVERAGE, gap_intervals=unknown.gap_intervals, overlap_uncertain_intervals=unknown.overlap_uncertain_intervals, complete=unknown.complete))
                u = (unknown.overflow_files, unknown.overflow_bytes_r, unknown.overflow_bytes_w)
                self.connection.execute("INSERT INTO file_overflow_unknown VALUES(?,?,?,?,?,?,?,?,?,?)", (boot, receipt.source, receipt.epoch, receipt.seq, hour, "overflow_owner_unknown", *u, canonical_json(unknown_cov)))
                old = self.connection.execute("SELECT overflow_files,overflow_bytes_r,overflow_bytes_w,coverage_json FROM boot_file_overflow_unknown WHERE boot_id=?", (boot,)).fetchone()
                totals = tuple(checked_sum(a, b, field=name) for a, b, name in zip(old[:3], u, ("overflow_files", "overflow_bytes_r", "overflow_bytes_w"))) if old else u
                merged_unknown = _merge_coverage_json(old[3] if old else None, unknown_cov)
                self.connection.execute("INSERT INTO boot_file_overflow_unknown VALUES(?,?,?,?,?,?) ON CONFLICT(boot_id) DO UPDATE SET overflow_files=excluded.overflow_files,overflow_bytes_r=excluded.overflow_bytes_r,overflow_bytes_w=excluded.overflow_bytes_w,coverage_json=excluded.coverage_json", (boot, "overflow_owner_unknown", *totals, merged_unknown))
                self.connection.execute("DELETE FROM file_hour_state WHERE boot_id=? AND ts_mono_ns=?", (boot, hour))
                self.connection.execute("UPDATE boot SET file_finalized_through_mono_ns=? WHERE boot_id=?", (hour + FILE_HOUR_NS, boot))
        return True

    def _diagnostic(self, snapshot, field):
        state = getattr(snapshot, "pending_state", None)
        if state is None:
            state = self._existing(snapshot.boot_id, snapshot.hour_start_mono_ns)
        if state is None:
            raise ValueError("overflow diagnostic requires authoritative file-hour receipts")
        candidates = [(app.last_receipt, app.app_key) for app in state.apps]
        if not candidates and state.unknown_owner.last_receipt:
            candidates.append((state.unknown_owner.last_receipt, None))
        with _atomic(self.connection):
            for receipt, key in candidates:
                row = self.connection.execute("SELECT app_id FROM app WHERE key=?", (key,)).fetchone()
                app_id = row[0] if row else None
                exists = self.connection.execute("SELECT 1 FROM store_diagnostic WHERE boot_id=? AND source=? AND epoch=? AND seq=? AND field=?", (snapshot.boot_id, receipt.source, receipt.epoch, receipt.seq, field)).fetchone()
                if exists:
                    continue
                diagnostic_id = checked_sum(self.connection.execute("SELECT COALESCE(MAX(diagnostic_id),0) FROM store_diagnostic").fetchone()[0], 1, field="diagnostic_id")
                self.connection.execute("INSERT INTO store_diagnostic VALUES(?,?,?,?,?,?,?,?,?)", (diagnostic_id, snapshot.boot_id, receipt.source, receipt.epoch, receipt.seq, app_id, snapshot.hour_start_mono_ns, "counter_overflow", field))

    def unknown_owner_for_hour(self, boot_id: str, hour_start: int) -> UnknownOwnerInput | None:
        validate_sqlite_integer(hour_start, "hour_start")
        if hour_start % FILE_HOUR_NS:
            raise ValueError("hour_start must be hour-aligned")
        row = self.connection.execute("SELECT overflow_files,overflow_bytes_r,overflow_bytes_w,coverage_json FROM file_overflow_unknown WHERE boot_id=? AND ts_mono_ns=?", (boot_id, hour_start)).fetchone()
        if row is None:
            return None
        cov = _decode_coverage(row[3])
        return UnknownOwnerInput(*row[:3], cov["gap_intervals"], cov["overlap_uncertain_intervals"],
                                 not (cov["gap_intervals"] or cov["overlap_uncertain_intervals"] or cov["counter_overflow"]))
