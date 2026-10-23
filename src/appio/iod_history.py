"""Bounded daemon adapters for the existing snapshot-consistent P1 queries.

Accounting, coverage, ownership and tier selection remain in ``appio.query``.
This module preflights materialization, maps external identities and limits the
complete reply (including coverage); it never resamples or widens a window.

Protocol 1 owns the outer result envelope. Its array payloads contain one page:
ranking uses ``{rows, next_cursor, snapshot}``; files use
``{value, next_cursor, snapshot}``; series uses ``{value, snapshot}``.
Totals/coverage use that latter object directly.
``from_wire`` accepts these payloads as well as direct ``to_wire`` output.
A ranking decoder returns the P1 list; pagination metadata stays in the payload.
Cursors are stateless MACs, scoped to this RPC instance and committed snapshot.
The protocol producer must allow ranking's cursor selector for wire continuation.

File rows have no transferable path_id in P1. The codec omits that surrogate and
uses the non-identifying placeholder 0 on decode. P3 has no file producer; file
availability and unknown-owner context are delegated to the P1 contracts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace

from appio.contracts import (
    AppTotal, Coverage, DeviceSeriesPoint, DeviceShareRow, DiskShare, FileCoverage,
    FileRow, FileTable, HookHits, RAW_BUCKET_NS, ResidualPoint, Series, SeriesPoint,
    UnknownOwnerOverflow, Window, WindowUnavailableError, validate_enum, validate_search,
    validate_sqlite_integer, validate_window,
)
from appio.identity import validate_app_key
from appio.ipc_models import QueryRequest
from appio.ipc_protocol import MAX_OUTBOUND, ProtocolError, encode_frame
from appio.query import QueryService

__all__ = ["QueryLimits", "HistoryRpc", "to_wire", "from_wire"]

_RECORDS = {cls.__name__: cls for cls in (
    Window, AppTotal, Coverage, DeviceSeriesPoint, DeviceShareRow, DiskShare,
    FileCoverage, FileRow, FileTable, HookHits, ResidualPoint, Series, SeriesPoint,
    UnknownOwnerOverflow,
)}
_TUPLES = {
    Coverage: ("reap_unapplied", "reap_unobserved"),
    DiskShare: ("devices",), Series: ("points", "device_points", "residual_points"),
    FileTable: ("rows",),
}
_METRICS = ("total_bytes", "read_bytes", "write_bytes", "rchar", "wchar")
_SORTS = ("bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen")
_KINDS = ("ranking", "series", "files", "coverage", "totals")


def to_wire(value: object, *, app_keys: Mapping[int, str]) -> object:
    """Encode only the explicitly supported P1 records and JSON containers."""
    cls = type(value)
    if cls in _RECORDS.values():
        if isinstance(value, Window) and value.kind == "launch":
            QueryService._precheck(value)
        result = {"record": cls.__name__}
        for field in fields(value):
            if field.name in ("app_id", "path_id", "launch_id"):
                continue
            result[field.name] = to_wire(getattr(value, field.name), app_keys=app_keys)
        if hasattr(value, "app_id"):
            key = validate_app_key(app_keys[value.app_id])
            if isinstance(value, AppTotal) and key != value.app_key:
                raise ValueError("inconsistent application identity")
            result["app_key"] = key
        return result
    if isinstance(value, (tuple, list)):
        return [to_wire(item, app_keys=app_keys) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) or key in ("app_id", "path_id", "launch_id") for key in value):
            raise ValueError("wire objects require external identities")
        return {key: to_wire(item, app_keys=app_keys) for key, item in value.items()}
    if value is None or cls in (str, bool, int):
        return value
    if cls is float and math.isfinite(value):
        return value
    raise ValueError("unsupported history value")


def from_wire(value: object, *, app_ids: Callable[[str], int], result_kind: str) -> object:
    """Reconstruct P1 records using the consumer's presentation AppKey registry."""
    validate_enum(result_kind, _KINDS, "result_kind")
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        if result_kind == "ranking" and set(value[0]) == {"rows", "next_cursor", "snapshot"}:
            value = value[0]["rows"]
        elif result_kind in ("series", "files") and set(value[0]) in ({"value", "snapshot"}, {"value", "snapshot", "next_cursor"}):
            value = value[0]["value"]
    if isinstance(value, dict) and set(value) == {"value", "snapshot"}:
        value = value["value"]

    def decode(item):
        if isinstance(item, list):
            return [decode(child) for child in item]
        if not isinstance(item, dict):
            if item is None or type(item) in (str, bool, int):
                return item
            if type(item) is float and math.isfinite(item):
                return item
            raise ValueError("invalid history value")
        if "record" not in item:
            if any(key in ("app_id", "path_id", "launch_id") for key in item):
                raise ValueError("database surrogate on wire")
            return {key: decode(child) for key, child in item.items()}
        cls = _RECORDS.get(item["record"])
        if cls is None:
            raise ValueError("unknown history record")
        names = {field.name for field in fields(cls)} - {"app_id", "path_id", "launch_id"}
        has_app = "app_id" in cls.__dataclass_fields__
        if has_app:
            names.add("app_key")
        if set(item) != names | {"record"}:
            raise ValueError("invalid history record fields")
        values = {name: decode(item[name]) for name in names}
        if has_app:
            key = validate_app_key(values.pop("app_key"))
            values["app_id"] = app_ids(key)
            if cls is AppTotal:
                values["app_key"] = key
        if cls is FileRow:
            values["path_id"] = 0
        for name in _TUPLES.get(cls, ()):
            values[name] = tuple(values[name])
        return cls(**values)

    result = decode(value)
    expected = {
        "ranking": list, "series": Series, "files": FileTable, "totals": DiskShare,
        "coverage": (Coverage, FileCoverage, UnknownOwnerOverflow, dict),
    }[result_kind]
    if not isinstance(result, expected) or (result_kind == "ranking" and any(not isinstance(row, AppTotal) for row in result)):
        raise ValueError("history result does not match selector")
    return result


@dataclass(frozen=True)
class QueryLimits:
    max_points: int = 4000
    max_file_rows: int = 500
    max_payload_bytes: int = 262144
    max_scan_rows: int = 50000
    timeout_ns: int = 200000000

    def __post_init__(self):
        for field in fields(self):
            if validate_sqlite_integer(getattr(self, field.name), field.name) == 0:
                raise ValueError("query limits must be positive")
        # Even the bounded error envelope must be representable.
        if self.max_payload_bytes < 256:
            raise ValueError("max_payload_bytes must be at least 256")


class _Rejected(Exception):
    def __init__(self, reason, message):
        self.reason, self.message = reason, message


def _too_large():
    raise _Rejected("query_too_large", "History query exceeds the server resource limits.")


def _text(value, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("invalid text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError("text too long")
    if "\x00" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid text")
    return value


class _Budget:
    def __init__(self, connection, limits, clock):
        self.connection, self.limits, self.clock = connection, limits, clock
        self.deadline = clock() + limits.timeout_ns
        self.remaining = limits.max_scan_rows
        self.expired = False

    def progress(self):
        self.expired = self.clock() >= self.deadline
        return int(self.expired)

    def check(self):
        if self.progress():
            raise _Rejected("query_timeout", "History query exceeded its execution deadline.")

    def count(self, sql, params=(), *, maximum=None, consume=True):
        """Read only bounded scalar rows, before P1 can fetchall or build lists."""
        self.check()
        cap = self.remaining if maximum is None else maximum
        cursor = self.connection.execute(sql + " LIMIT ?", (*params, cap + 1))
        count = 0
        while True:
            self.check()
            batch = cursor.fetchmany(min(256, cap + 1 - count))
            if not batch:
                break
            count += len(batch)
            if count > cap:
                _too_large()
        if consume:
            self.remaining -= count
        return count

    def table(self, table, where="1", params=(), *, text=None):
        # Table/column/where fragments are internal literals, never selectors.
        self.count(f"SELECT 1 FROM {table} WHERE {where}", params)
        if text and self.connection.execute(
            f"SELECT 1 FROM {table} WHERE ({where}) AND length(CAST({text} AS BLOB))>? LIMIT 1",
            (*params, min(self.limits.max_payload_bytes, MAX_OUTBOUND)),
        ).fetchone():
            _too_large()


class HistoryRpc:
    """Use a dedicated caller-owned connection; never retain a page transaction.

    The deadline is cooperative: SQLite is interruptible, Python results are
    checked and discarded when late. The daemon runs this on its query worker.
    """
    def __init__(self, connection: sqlite3.Connection, *, limits: QueryLimits = QueryLimits(), clock_ns: Callable[[], int] = time.monotonic_ns):
        self.connection = connection
        self.service = QueryService(connection)
        self.limits, self.clock_ns = limits, clock_ns
        self._secret = secrets.token_bytes(32)
        self._active = threading.Lock()

    def _validate(self, request, request_id):
        _text(request_id, 64)
        if not isinstance(request, QueryRequest):
            raise ValueError("expected typed query")
        validate_window(request.window)
        self.service._precheck(request.window)
        validate_enum(request.kind, _KINDS, "kind")
        validate_enum(request.metric, _METRICS, "metric")
        sorts = _SORTS if request.kind == "files" else ("bytes_total",)
        if request.kind == "ranking":
            sorts += _METRICS
        validate_enum(request.sort, sorts, "sort")
        validate_enum(request.direction, ("asc", "desc"), "direction")
        if request.kind != "ranking" and request.metric != "total_bytes":
            raise ValueError("inapplicable metric")
        if request.kind not in ("ranking", "files") and request.direction != "desc":
            raise ValueError("inapplicable direction")
        if request.kind != "files" and request.search is not None:
            raise ValueError("inapplicable search")
        validate_enum(request.coverage_kind, ("process", "file", "unknown_owner"), "coverage_kind")
        search = validate_search(request.search)
        if search is not None and len(search.encode("utf-8")) > 256:
            raise ValueError("search too long")
        limit = validate_sqlite_integer(request.limit, "limit")
        cap = min(500, self.limits.max_file_rows) if request.kind != "series" else 4000
        if not 1 <= limit <= cap:
            raise ValueError("invalid page limit")
        if request.app_key is not None:
            validate_app_key(request.app_key)
        if request.kind in ("series", "totals", "files") and request.app_key is None:
            raise ValueError("app-scoped request requires AppKey")
        if request.kind == "ranking" and (request.app_key is not None or request.search is not None or request.sort not in ("bytes_total", request.metric)):
            raise ValueError("invalid ranking selector")
        if request.kind != "coverage" and request.coverage_kind != "process":
            raise ValueError("invalid coverage selector")
        if request.kind == "coverage" and ((request.coverage_kind == "file" and request.app_key is None) or (request.coverage_kind == "unknown_owner" and request.app_key is not None)):
            raise ValueError("invalid coverage identity")
        if request.cursor is not None:
            _text(request.cursor, 512)
            if request.kind not in ("ranking", "files"):
                raise ValueError("this result does not paginate")

    def _error(self, request_id, reason, message):
        result = {"protocol": 1, "type": "error", "reason": reason, "message": message}
        try:
            result["request_id"] = _text(request_id, 64)
        except (ValueError, UnicodeError):
            pass
        # Error IDs are optional in protocol 1. At tiny configured caps, omit
        # that optional field rather than trimming an exact A22 reason/message.
        maximum = min(self.limits.max_payload_bytes, MAX_OUTBOUND)
        try:
            encode_frame(result, max_bytes=maximum)
        except ProtocolError:
            result.pop("request_id", None)
            encode_frame(result, max_bytes=maximum)
        return result

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _identity(self):
        checkpoints = list(self.connection.execute(
            "SELECT source,boot_id,epoch,seq,checkpoint_mono_ns FROM sampler_checkpoint ORDER BY source LIMIT 3"))
        return self._digest((self.connection.execute("PRAGMA data_version").fetchone()[0], self.connection.total_changes, checkpoints))

    def _scope_identity(self, request):
        return self._digest([request.kind, to_wire(request.window, app_keys={}), request.app_key,
                             request.metric, request.sort, request.direction, request.search,
                             request.coverage_kind, request.limit])

    def _cursor(self, scope, snapshot, offset):
        body = json.dumps([scope, snapshot, offset], separators=(",", ":")).encode()
        signature = hmac.digest(self._secret, body, "sha256")
        return base64.urlsafe_b64encode(signature + body).decode().rstrip("=")

    def _offset(self, token, scope, snapshot):
        if token is None:
            return 0
        try:
            packed = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            signature, body = packed[:32], packed[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._secret, body, "sha256")):
                raise ValueError("invalid signature")
            prior_scope, prior_snapshot, offset = json.loads(body)
            if prior_scope != scope or prior_snapshot != snapshot or type(offset) is not int or not 0 < offset <= self.limits.max_scan_rows:
                raise ValueError("changed scope or snapshot")
            return offset
        except (ValueError, TypeError, UnicodeError):
            raise _Rejected("stale_cursor", "History cursor is stale or invalid; restart the query.") from None

    def _preflight_apps(self, sql, params, budget):
        where = "app_id IN (" + sql + ")"
        budget.table("app", where, params, text="key")
        if self.connection.execute(
            f"SELECT 1 FROM app WHERE {where} AND length(CAST(name AS BLOB))>? LIMIT 1",
            (*params, min(self.limits.max_payload_bytes, MAX_OUTBOUND)),
        ).fetchone():
            _too_large()

    def _preflight(self, request, tier, cutoff, aid, budget):
        window = request.window
        boot = window.boot_id
        start = window.start_mono_ns if window.kind == "range" else 0
        end = window.end_mono_ns
        is_file = request.kind == "files" or (request.kind == "coverage" and request.coverage_kind != "process")
        width = RAW_BUCKET_NS if tier == "raw" else (60 if tier == "rollup_60s" else 3600) * 10**9
        if boot is None:
            budget.table("gap")
            budget.table("store_diagnostic")
            budget.table("boot")
        else:
            budget.table("gap", "boot_id=?", (boot,))
            budget.table("collector_interval", "boot_id=?", (boot,))
            budget.table("handover", "boot_id=?", (boot,))
            budget.table("store_diagnostic", "boot_id=?", (boot,))
        if is_file:
            scope, boot, start, end = self.service._file_scope(window, cutoff)
            if scope == "lifetime":
                where, params = "1", ()
                budget.table("file_hour_state")
                unknown_table = "boot_file_overflow_unknown"
            elif scope == "boot":
                where, params = "boot_id=?", (boot,)
                unknown_table = "boot_file_overflow_unknown"
            else:
                first = ((start + 3600*10**9-1)//(3600*10**9))*(3600*10**9)
                last = (end//(3600*10**9))*(3600*10**9)
                where, params = "boot_id=? AND ts_mono_ns>=? AND ts_mono_ns<?", (boot, first, last)
                unknown_table = "file_overflow_unknown"
            budget.table(unknown_table, where, params, text="coverage_json")
            if aid is not None:
                app_where, app_params = where + " AND app_id=?", (*params, aid)
                budget.table("file_coverage" if scope == "hourly" else "app_boot_file_coverage", app_where, app_params, text="coverage_json")
                file_table = "file_sample" if scope == "hourly" else "app_boot_file_total"
                budget.table(file_table, app_where, app_params)
                budget.table(f"{file_table} t JOIN path p USING(path_id)", app_where, app_params, text="p.basename")
            return
        need_names = request.kind == "ranking" or aid is None
        if tier == "totals_only":
            # P1's totals reducer reads all app summaries, even for one app.
            if window.kind == "lifetime":
                budget.table("app_lifetime_total", text="coverage_json")
                budget.table("app_boot_dev_total")
                app_sql, app_params = "SELECT app_id FROM app_lifetime_total UNION SELECT app_id FROM app_boot_dev_total", ()
            else:
                budget.table("app_boot_total", "boot_id=?", (boot,), text="coverage_json")
                budget.table("app_boot_dev_total", "boot_id=?", (boot,))
                app_sql, app_params = "SELECT app_id FROM app_boot_total WHERE boot_id=? UNION SELECT app_id FROM app_boot_dev_total WHERE boot_id=?", (boot, boot)
            if need_names:
                self._preflight_apps(app_sql, app_params, budget)
            return
        first, last = ((start + width-1)//width)*width, (end//width)*width
        column = "ts_mono_ns" if tier == "raw" else "bucket_start_mono_ns"
        where = f"boot_id=? AND {column}>=? AND {column}<?"
        params = (boot, first, last)
        if tier != "raw":
            where += " AND bucket_secs=?"
            params += (width//10**9,)
        if aid is not None:
            where += " AND app_id=?"
            params += (aid,)
        app_table, dev_table = ("sample", "sample_dev") if tier == "raw" else ("rollup_app", "rollup_dev")
        budget.table(app_table, where, params, text=None if tier == "raw" else "coverage_json")
        budget.table(dev_table, where, params)
        cov_where, cov_params = where, params
        if tier == "raw":
            # Coverage receipts may be observed outside their sample's bucket.
            cov_where = (
                "c.boot_id=? AND ((c.ts_mono_ns>=? AND c.ts_mono_ns<?) OR EXISTS ("
                "SELECT 1 FROM sample s WHERE s.boot_id=c.boot_id AND s.source=c.source "
                "AND s.epoch=c.epoch AND s.seq=c.seq AND s.app_id=c.app_id "
                "AND s.ts_mono_ns>=? AND s.ts_mono_ns<?))"
            )
            cov_params = (boot, first, last, first, last)
            if aid is not None:
                cov_where += " AND c.app_id=?"
                cov_params += (aid,)
            budget.table("sample_coverage c", cov_where, cov_params, text="coverage_json")
        if need_names:
            app_sql = f"SELECT app_id FROM {app_table} WHERE {where} UNION SELECT app_id FROM {dev_table} WHERE {where}"
            app_params = (*params, *params)
            if tier == "raw":
                app_sql += f" UNION SELECT c.app_id FROM sample_coverage c WHERE {cov_where}"
                app_params += cov_params
            self._preflight_apps(app_sql, app_params, budget)
        if request.kind == "series":
            budget.table("clock_map", "boot_id=?", (boot,))
            point_limit = min(self.limits.max_points, request.limit)
            union = f"SELECT {column}/? AS bucket FROM {app_table} WHERE {where} UNION SELECT {column}/? FROM {dev_table} WHERE {where}"
            args = (width, *params, width, *params)
            if tier == "raw":
                union += f" UNION SELECT ts_mono_ns/? FROM sample_coverage WHERE {where}"
                args += (width, *params)
            budget.count("SELECT 1 FROM (" + union + ")", args, maximum=point_limit, consume=False)
            budget.count(f"SELECT DISTINCT {column}/?,dev FROM {dev_table} WHERE {where}", (width, *params), maximum=point_limit, consume=False)

    def _serialize(self, reply, budget):
        budget.check()
        maximum = min(self.limits.max_payload_bytes, MAX_OUTBOUND)
        size = 0
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(reply):
            budget.check()
            size += len(chunk.encode("utf-8"))
            if size > maximum:
                _too_large()
        try:
            encode_frame(reply, max_bytes=maximum)
        except ProtocolError as exc:
            if exc.reason in ("frame_too_large", "too_many_members", "nesting_too_deep"):
                _too_large()
            raise
        budget.check()
        return reply

    def _reply(self, request, request_id, aid, snapshot, offset, budget):
        app_keys = {}
        def key_for(app_id):
            if app_id not in app_keys:
                row = self.connection.execute("SELECT key FROM app WHERE app_id=?", (app_id,)).fetchone()
                if row is None:
                    raise ValueError("missing stored app")
                app_keys[app_id] = row[0]
            return app_keys[app_id]
        def domain(method, *args):
            budget.check()
            value = method(*args)
            budget.check()
            return value
        def envelope(payload):
            return {"protocol": 1, "type": "result", "request_id": request_id, "kind": request.kind, "payload": payload}
        if request.kind == "ranking":
            rows = domain(self.service.ranking, request.window, request.metric)
            if request.direction == "asc":
                rows.sort(key=lambda row: getattr(row, request.metric))
            if offset > len(rows):
                raise _Rejected("stale_cursor", "History cursor is stale; restart the query.")
            scope = self._scope_identity(request)
            def page(count):
                selected = rows[offset:offset+count]
                for row in selected:
                    key_for(row.app_id)
                continuation = self._cursor(scope, snapshot, offset+count) if offset+count < len(rows) else None
                return envelope([{"rows": to_wire(selected, app_keys=app_keys), "next_cursor": continuation, "snapshot": snapshot}])
            count = min(request.limit, len(rows)-offset)
            # Byte/member pressure reduces a page only with an exact continuation.
            while True:
                try:
                    return self._serialize(page(count), budget)
                except _Rejected as exc:
                    if exc.reason != "query_too_large" or count <= 1:
                        raise
                    count //= 2
        if aid is not None:
            key_for(aid)
        if request.kind == "series":
            value = domain(self.service.series, aid, request.window)
            if any(len(getattr(value, field)) > min(self.limits.max_points, request.limit) for field in ("points", "device_points", "residual_points")):
                _too_large()
        elif request.kind == "totals":
            value = domain(self.service.disk_share, aid, request.window)
        elif request.kind == "files":
            value = domain(self.service.file_table, aid, request.window, request.sort, request.direction, request.search)
            if offset > len(value.rows):
                raise _Rejected("stale_cursor", "History cursor is stale; restart the query.")
            count = min(request.limit, len(value.rows)-offset)
            scope = self._scope_identity(request)
            while True:
                page = replace(value, rows=value.rows[offset:offset+count])
                continuation = self._cursor(scope, snapshot, offset+count) if offset+count < len(value.rows) else None
                payload = {"value": to_wire(page, app_keys=app_keys), "snapshot": snapshot, "next_cursor": continuation}
                try:
                    return self._serialize(envelope([payload]), budget)
                except _Rejected as exc:
                    if exc.reason != "query_too_large" or count <= 1:
                        raise
                    count //= 2
        elif request.coverage_kind == "unknown_owner":
            value = domain(self.service.unknown_owner_file_coverage, request.window)
        elif request.coverage_kind == "file":
            value = domain(self.service.file_coverage, aid, request.window)
        elif aid is not None:
            value = domain(self.service.disk_share, aid, request.window).coverage
        else:
            rows = domain(self.service.ranking, request.window, request.metric)
            value = {key_for(row.app_id): row.coverage for row in rows}
        payload = {"value": to_wire(value, app_keys=app_keys), "snapshot": snapshot}
        return self._serialize(envelope([payload] if request.kind in ("series", "files") else payload), budget)

    def execute(self, request: QueryRequest, *, request_id: str) -> dict:
        """Return a complete bounded result or a sanitized explicit rejection."""
        locked = False
        owned = False
        busy_timeout = None
        budget = None
        try:
            self._validate(request, request_id)
            locked = self._active.acquire(blocking=False)
            if not locked or self.connection.in_transaction:
                raise _Rejected("history_unavailable", "History connection is busy.")
            budget = _Budget(self.connection, self.limits, self.clock_ns)
            budget.check()
            busy_timeout = self.connection.execute("PRAGMA busy_timeout").fetchone()[0]
            wait_ms = max(0, min(10, (budget.deadline - self.clock_ns())//1000000))
            self.connection.execute(f"PRAGMA busy_timeout={wait_ms}")
            self.connection.set_progress_handler(budget.progress, 1000)
            self.connection.execute("BEGIN")
            owned = True
            # Pin a read snapshot before scope, identities, preflight and P1 calls.
            self.connection.execute("SELECT 1 FROM app LIMIT 1").fetchone()
            snapshot = self._identity()
            offset = self._offset(request.cursor, self._scope_identity(request), snapshot)
            tier, cutoff = self.service._scope(request.window)
            budget.check()
            aid = None
            if request.app_key is not None:
                row = self.connection.execute("SELECT app_id FROM app WHERE key=?", (request.app_key,)).fetchone()
                if row is None:
                    raise _Rejected("unknown_app", "Application is not present in retained history.")
                aid = row[0]
            self._preflight(request, tier, cutoff, aid, budget)
            return self._reply(request, request_id, aid, snapshot, offset, budget)
        except WindowUnavailableError as exc:
            return self._error(request_id, exc.reason, str(exc))
        except _Rejected as exc:
            return self._error(request_id, exc.reason, exc.message)
        except (ValueError, UnicodeError, TypeError, ProtocolError):
            if owned:
                return self._error(request_id, "history_unavailable", "History is temporarily unavailable.")
            return self._error(request_id, "invalid_request", "Invalid history request.")
        except (sqlite3.Error, KeyError, OverflowError):
            if budget is not None and (budget.expired or self.clock_ns() >= budget.deadline):
                return self._error(request_id, "query_timeout", "History query exceeded its execution deadline.")
            return self._error(request_id, "history_unavailable", "History is temporarily unavailable.")
        finally:
            if locked:
                try:
                    if budget is not None:
                        self.connection.set_progress_handler(None, 0)
                    if owned and self.connection.in_transaction:
                        self.connection.execute("ROLLBACK")
                    if busy_timeout is not None:
                        self.connection.execute(f"PRAGMA busy_timeout={busy_timeout}")
                finally:
                    self._active.release()
