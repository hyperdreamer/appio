"""Read-only, ownership-proved history views over independent schema-v1 stores.

Private SQLite snapshots retract excluded *whole* raw deltas through the existing
store accounting machinery before QueryService runs. We never subtract an
estimated overlap from precomputed totals. Pruned mixed histories are unavailable
rather than guessed. Exports remain separate stores; AppKey joins the results.

Protocol v1 does not carry collector_interval/handover rows. A remote whole-window
answer is authoritative when the daemon returns a non-empty, snapshot-bound
result; its coverage may honestly be incomplete because process observation can
lose identities or intervals. The GUI preserves those numeric retained bytes
and their incomplete coverage instead of requiring a privileged export. Empty
answers and paginated answers still cannot prove that no history was retained,
so they remain unavailable without an independent ownership source. A remote
answer cannot supersede local ownership receipts or known overlap exclusions,
even when excluding uncertain whole deltas has reduced every counter to zero.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from .contracts import (
    AppTotal, Direction, DiskShare, FileCoverage, FileSort, FileTable, Series, Window, FILE_HOUR_NS,
    WindowUnavailableError, SQLITE_INT_MAX, validate_enum, validate_search, validate_sqlite_integer,
)
from .durable import decode_handover
from .file_runtime import FileAvailability
from .iodclient import AppRegistry, GapNotice, IodClient, IodClientError, RequestRejected
from .ipc_models import QueryRequest
from .ownership import ownership_exclusions, subtract_intervals
from .query import QueryService, _model_coverage, _reduce_coverage
from .store import AppIoStore, _empty_coverage

_COUNTERS = ("rchar", "wchar", "read_bytes", "write_bytes")
_PEAKS = ("peak_read_bps", "peak_write_bps", "peak_total_bps")
_METRICS = ("total_bytes", "read_bytes", "write_bytes", "rchar", "wchar")


@dataclass(frozen=True)
class HistorySegment:
    collector_id: str
    owner: str
    boot_id: str
    start_ns: int
    end_ns: int
    proven: bool

    def __post_init__(self):
        if not self.collector_id or not self.boot_id:
            raise ValueError("segment requires collector and boot identities")
        validate_enum(self.owner, ("gui", "iod", "unknown"), "segment.owner")
        validate_sqlite_integer(self.start_ns, "segment.start_ns")
        validate_sqlite_integer(self.end_ns, "segment.end_ns")
        if self.end_ns <= self.start_ns or type(self.proven) is not bool:
            raise ValueError("segment must be a nonempty half-open interval")


def _peak(values):
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _coverage(rows, *, gaps=0, overlaps=0, unavailable=False):
    # Use the established component-wise coverage reducer (sum counts/vectors,
    # OR flags, and retain unavailable device state), not numeric averaging.
    reduced = _empty_coverage() | {"device_states": [], "invalid": False}
    for row in rows:
        value = _empty_coverage()
        for key in value:
            if key == "file":
                continue  # P3 process-only coverage has the canonical zero file placeholder
            field = getattr(row, key)
            value[key] = (dict(zip(_COUNTERS, field, strict=True))
                          if key in ("reap_unapplied", "reap_unobserved")
                          else int(field) if type(field) is bool else field)
        _reduce_coverage(reduced, json.dumps(value))
    reduced["gap_intervals"] += gaps
    reduced["overlap_uncertain_intervals"] += overlaps
    tier = rows[0].tier if rows else "raw"
    reason = "not_retained" if unavailable else next(
        (row.unavailable_reason for row in rows if row.unavailable_reason), None)
    return _model_coverage(reduced, tier, base_complete=bool(rows) and all(row.complete for row in rows)
                           and not gaps and not overlaps and not reason,
                           unavailable_reason=reason)


@dataclass(frozen=True)
class FileViewResult:
    table: FileTable
    coverage: FileCoverage
    served_gap_count: int | None


class HistoryRouter:
    def __init__(self, local: QueryService, client: IodClient, registry: AppRegistry):
        self.local, self.client, self.registry = local, client, registry
        self._sources = {"local": (local, "gui")}
        # Capture identity only when a source is registered, never in a Files
        # request on the UI thread. Worker connections stay read-only.
        self._source_paths = {"local": local.connection.execute("PRAGMA database_list").fetchone()[2]}
        self.segments: tuple[HistorySegment, ...] = ()
        self.boundaries: tuple[tuple[str, int, str, str], ...] = ()
        self.view_state = "unavailable"
        self._window = None
        self._generation = 0
        self._ready = False
        self._views = []
        self._ranking = []
        self._remote = None
        self._pending = None
        self._queue = []
        self._remote_parts = {}
        self._gap_ranges = []
        self._known_gap_ranges = []
        self._overlap_ranges = []
        self._has_local_history = False
        self._not_retained = False
        self._chronology_unproved = False
        self.transport_issues: list[GapNotice] = []
        self._all_segments = ()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="appio-history")
        self._local_future = None
        self._local_generation = None
        self._cached_parts = {}
        self._remote_snapshot = None
        self._closed = False
        self._file_jobs = set()
        self._file_workers = set()
        self._file_status = FileAvailability(True, False)

    def _cancel_file_requests(self):
        for future in tuple(self._file_jobs):
            self._settle_file(future, self._generation, error=WindowUnavailableError(
                "file_source_unavailable", "File history generation retired."))

    def _settle_file(self, future, generation, value=None, error=None):
        if future.done():
            return
        if generation != self._generation or self._closed:
            error = WindowUnavailableError("file_source_unavailable", "File history generation retired.")
        try:
            if error is not None:
                if not isinstance(error, (WindowUnavailableError, IodClientError)):
                    error = WindowUnavailableError("file_source_unavailable", "File history could not be read.")
                self._file_status = FileAvailability(False, False,
                    "not_retained" if getattr(error, "reason", None) == "not_retained" else "file_source_unavailable")
                future.set_exception(error)
            else:
                self._file_status = FileAvailability(True, False)
                future.set_result(value)
        except InvalidStateError:
            pass  # Caller cancellation racing worker completion is not a UI fault.

    def file_source_status(self) -> FileAvailability:
        state = self.client.state
        if state is not None and state.verified and state.answering:
            caps = state.capabilities
            ready = bool(caps is not None and caps.file_io and caps.bpf_loaded)
            return FileAvailability(ready, ready, None if ready else "file_source_unavailable",
                                    getattr(state, "file_diagnostic", None))
        return self._file_status

    @staticmethod
    def _check_file_result(table, coverage, served_gap_count: int | None = None):
        if table.app_id != coverage.app_id:
            raise WindowUnavailableError("file_source_unavailable", "File coverage does not match the table snapshot.")
        if table.window != coverage.window or table.coverage.tier != coverage.coverage.tier:
            raise WindowUnavailableError("file_source_unavailable", "Mixed file scope or retention tiers.")
        for evidence in (table.coverage, coverage.coverage):
            if evidence.unavailable_reason:
                raise WindowUnavailableError(evidence.unavailable_reason, "File history is not available for this window.")
        if table.coverage != coverage.coverage or table.unknown_owner_overflow != coverage.unknown_owner_overflow:
            raise WindowUnavailableError("file_source_unavailable", "File coverage does not match the table snapshot.")
        if coverage.served_interval is None:
            raise WindowUnavailableError(
                "file_source_unavailable", "File ownership cannot be represented by one store.")
        return FileViewResult(table, coverage, served_gap_count)

    @staticmethod
    def _check_file_provenance(worker, served: Window, supplying_name: str) -> bool:
        start = served.start_mono_ns or 0
        end = served.end_mono_ns or SQLITE_INT_MAX
        segments = []
        for segment in worker._all_segments:
            if served.boot_id is not None and segment.boot_id != served.boot_id:
                continue
            left, right = max(segment.start_ns, start), min(segment.end_ns, end)
            if left < right:
                segments.append(replace(segment, start_ns=left, end_ns=right))

        known = []
        for boot, left, right in worker._known_gap_ranges:
            if served.boot_id is not None and boot != served.boot_id:
                continue
            clipped_left, clipped_right = max(left, start), min(right, end)
            if clipped_left < clipped_right:
                known.append((boot, clipped_left, clipped_right))

        def fully_covered(segment):
            # PIVOT-1: the *served interval* must be hour-aligned (rows are hourly), but a
            # segment inside it may start or end anywhere: real gaps begin and end at the
            # instant a daemon restarted, so demanding hour-aligned segments made every
            # real store unservable. Coverage must still be fully explained by known gaps.
            cursor = segment.start_ns
            for boot, left, right in sorted(known):
                if boot != segment.boot_id or right <= cursor:
                    continue
                if left > cursor:
                    return False
                cursor = max(cursor, right)
                if cursor >= segment.end_ns:
                    return True
            return False

        proven_names = {segment.collector_id for segment in segments if segment.proven}
        unproved = [segment for segment in segments if not segment.proven]
        overlap = any(
            (served.boot_id is None or boot == served.boot_id)
            and left < end and right > start
            for boot, left, right in worker._overlap_ranges
        )
        if (not segments or worker._chronology_unproved or overlap
                or len(proven_names) != 1
                or any(not fully_covered(segment) for segment in unproved)):
            raise WindowUnavailableError(
                "file_source_unavailable", "File ownership cannot be represented by one store.")
        return proven_names == {supplying_name}

    @staticmethod
    def _read_local_files(paths, window, key, app_id, sort, direction, search):
        """Read one ownership-proved store on the bounded executor, never sum IDs.

        Process retraction cannot retract top-100 hourly F candidates. Prove
        ownership over each query's reported served interval, retaining its
        original window and coverage disclosure. Never use _materialize's
        unfiltered F tables or make process retention a prerequisite for F.
        """
        sources = []
        try:
            for name, path, owner in paths:
                source = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=0.2)
                copy = sqlite3.connect(":memory:")
                try:
                    source.backup(copy)
                finally:
                    source.close()
                sources.append((name, QueryService(copy), owner))
            results = []
            unavailable = WindowUnavailableError("file_source_unavailable", "No authoritative file app receipt.")
            provenance_error = None
            for name, service, _ in sources:
                try:
                    with service._snapshot():
                        row = service.connection.execute("SELECT app_id FROM app WHERE key=?", (key,)).fetchone()
                        if row is None:
                            continue
                        local_app_id = row[0]
                        table = service.file_table(local_app_id, window, sort, direction, search)
                        coverage = service.file_coverage(local_app_id, window)
                        facts = service.file_serve_facts(local_app_id, window)
                        if (table.app_id != local_app_id or coverage.app_id != local_app_id
                                or table.window != window or coverage.window != window
                                or facts.served_interval != coverage.served_interval
                                or facts.gaps_in_requested_window != coverage.coverage.gap_intervals):
                            raise WindowUnavailableError(
                                "file_source_unavailable", "File coverage does not match the table snapshot.")
                        result = HistoryRouter._check_file_result(
                            replace(table, app_id=app_id), replace(coverage, app_id=app_id),
                            facts.gaps_in_served_interval)
                except WindowUnavailableError as error:
                    unavailable = error
                    continue
                served = result.coverage.served_interval
                if served is None:
                    unavailable = WindowUnavailableError(
                        "file_source_unavailable", "File ownership cannot be represented by one store.")
                    continue
                worker = HistoryRouter(service, SimpleNamespace(state=None), AppRegistry())
                try:
                    worker._sources = {source: (query, owner) for source, query, owner in sources}
                    worker._window = served
                    worker._plan(served)
                    if not HistoryRouter._check_file_provenance(worker, served, name):
                        continue  # A discarded mirror cannot supply the rows.
                    results.append(result)
                except WindowUnavailableError as error:
                    if provenance_error is None:
                        provenance_error = error
                finally:
                    worker.close()
            if provenance_error is not None:
                raise provenance_error
            if not results:
                raise unavailable
            if len(results) != 1:
                raise WindowUnavailableError("file_source_unavailable", "File ownership cannot be represented by one store.")
            return results[0]
        finally:
            for _, service, _ in sources:
                service.connection.close()

    def request_file_view(self, app_id: int, window: Window, sort: FileSort,
                          direction: Direction, search: str | None) -> Future[FileViewResult]:
        # A22 must win even over a bad app/sort, just as on the RPC surface.
        self.local._precheck(window)
        validate_enum(sort, ("bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen"), "sort")
        validate_enum(direction, ("asc", "desc"), "direction")
        validate_search(search)
        future, generation = Future(), self._generation
        if len(self._file_jobs) >= 8 or len(self._file_workers) >= 8:
            future.set_exception(RequestRejected("query_busy", "File history queue is full."))
            return future
        self._file_jobs.add(future)
        future.add_done_callback(self._file_jobs.discard)
        try:
            self._require(window)
            key = self.registry.key_for(app_id)
            if self._remote is not None:
                self._request_remote_files(future, generation, app_id, key, window, sort, direction, search)
            else:
                paths = []
                for name, (service, owner) in self._sources.items():
                    path = self._source_paths[name]
                    if not path or service.connection.in_transaction:
                        raise WindowUnavailableError("file_source_unavailable", "File history needs a committed store.")
                    paths.append((name, path, owner))
                def read():
                    if not future.done():
                        return self._read_local_files(paths, window, key, app_id, sort, direction, search)
                job = self._executor.submit(read)
                # Cancellation retires a UI response, not a running worker.
                # Keep its admission slot until the executor really retires it.
                self._file_workers.add(job)
                job.add_done_callback(self._file_workers.discard)
                def completed(job):
                    try:
                        self._settle_file(future, generation, value=job.result())
                    except Exception as error:
                        self._settle_file(future, generation, error=error)
                job.add_done_callback(completed)
        except Exception as error:
            self._settle_file(future, generation, error=error)
        return future

    def _request_remote_files(self, out, generation, app_id, key, window, sort, direction, search):
        snapshot = self._remote_snapshot
        state = self.client.state
        if not self.file_source_status().available or snapshot is None:
            raise WindowUnavailableError("file_source_unavailable", "Daemon file source unavailable.")
        cursor, connection = state.cursor, getattr(self.client, "generation", None)
        pages, tokens = [], set()
        request = QueryRequest("files", window, key, sort=sort, direction=direction, search=search)

        def active():
            now = self.client.state
            if (out.done() or generation != self._generation or self._closed or now is None
                    or not now.verified or not now.answering or now.cursor != cursor
                    or getattr(self.client, "generation", None) != connection):
                raise WindowUnavailableError("file_source_unavailable", "Stale file history response.")

        def decoded(job, kind):
            active()
            response = job.result()
            if (response.get("snapshot") != snapshot or response.get("kind") != kind
                    or response.get("app_key") != key):
                raise WindowUnavailableError("file_source_unavailable", "Remote file scope or snapshot changed.")
            return response

        def covered(job):
            try:
                response = decoded(job, "coverage")
                coverage = response["result"]
                if not isinstance(coverage, FileCoverage) or response.get("next_cursor") is not None:
                    raise WindowUnavailableError("file_source_unavailable", "Invalid remote file coverage.")
                table = replace(pages[0], rows=tuple(row for page in pages for row in page.rows), app_id=app_id)
                value = self._check_file_result(table, replace(coverage, app_id=app_id), None)
                self._settle_file(out, generation, value=value)
            except Exception as error:
                self._settle_file(out, generation, error=error)

        def received(job):
            try:
                response = decoded(job, "files")
                table = response["result"]
                if (not isinstance(table, FileTable) or table.window != window or
                        (table.sort, table.direction, table.search) != (sort, direction, search)):
                    raise WindowUnavailableError("file_source_unavailable", "Remote file scope changed.")
                if pages and replace(table, rows=pages[0].rows) != pages[0]:
                    raise WindowUnavailableError("file_source_unavailable", "Remote file page metadata changed.")
                pages.append(table)
                if sum(len(page.rows) for page in pages) > 50_000 or len(pages) > 128:
                    raise RequestRejected("query_too_large", "File history exceeds the bounded view budget.")
                token = response.get("next_cursor")
                if token is not None:
                    if not isinstance(token, str) or not token or token in tokens or not table.rows:
                        raise WindowUnavailableError("file_source_unavailable", "Invalid file continuation.")
                    tokens.add(token)
                    # Opaque server HMAC, forwarded unchanged. Never decode,
                    # recompute, skip a page, or fabricate a local continuation.
                    self.client.query(replace(request, cursor=token)).add_done_callback(received)
                else:
                    self.client.query(QueryRequest("coverage", window, key, coverage_kind="file")).add_done_callback(covered)
            except Exception as error:
                self._settle_file(out, generation, error=error)
        self.client.query(request).add_done_callback(received)

    def invalidate(self) -> None:
        self._generation += 1
        self._cancel_file_requests()
        self._file_status = FileAvailability(True, False)
        self._local_generation = -1
        if self._local_future is not None:
            self._local_future.cancel()
        self._ready = False
        self.view_state = "unavailable"

    def add_source(self, service: QueryService, *, collector_id: str, owner: str) -> None:
        if not collector_id or collector_id == "local":
            raise ValueError("an imported collector requires its own identity")
        validate_enum(owner, ("gui", "iod"), "owner")
        self._sources[collector_id] = (service, owner)
        self._source_paths[collector_id] = service.connection.execute("PRAGMA database_list").fetchone()[2]
        self.invalidate()

    def record_transport_gap(self, notice: GapNotice) -> None:
        """Persist bounded visibility loss locally, without creating root gaps."""
        self.transport_issues.append(notice)
        if (not notice.boot_id or notice.from_mono_ns is None or notice.to_mono_ns is None
                or notice.from_mono_ns < 0):
            return
        connection = self.local.connection
        if connection.execute("SELECT 1 FROM boot WHERE boot_id=?", (notice.boot_id,)).fetchone() is None:
            return
        exists = connection.execute("SELECT 1 FROM gap WHERE boot_id=? AND source='gui' "
            "AND from_mono_ns=? AND to_mono_ns=? AND reason='gui_absent'",
            (notice.boot_id, notice.from_mono_ns, notice.to_mono_ns)).fetchone()
        if not exists:
            epoch = connection.execute("SELECT epoch FROM collector_epoch WHERE source='gui'").fetchone()
            AppIoStore(connection).record_gap(notice.boot_id, "gui", epoch[0] if epoch else 1,
                0, notice.from_mono_ns, notice.to_mono_ns, "gui_absent")
        self.invalidate()

    def _close_views(self):
        for _, service in self._views:
            service.connection.close()
        self._views = []

    def close(self):
        self.invalidate()
        self._close_views()
        self._queue = []
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _plan(self, window):
        evidence, cuts, gaps = [], defaultdict(dict), set()
        retained = {}
        predecessors = defaultdict(set)
        boots = {window.boot_id: (0, 0)} if window.boot_id is not None else {}
        for name, (service, _) in self._sources.items():
            db = service.connection
            chronology = db.execute("SELECT boot_id,boot_order FROM boot ORDER BY boot_order").fetchall()
            for boot, order in chronology:
                boots.setdefault(boot, (len(boots), order))
            for (earlier, _), (later, _) in zip(chronology, chronology[1:]):
                predecessors[later].add(earlier)
            for owner, boot, epoch, start, end in db.execute(
                    "SELECT source,boot_id,epoch,from_mono_ns,to_mono_ns FROM collector_interval"):
                if window.boot_id is None or boot == window.boot_id:
                    evidence.append((name, owner, boot, epoch, start, end))
                    if (name, boot) not in retained:
                        retained[(name, boot)] = self._raw_complete(db, boot)
            for boot, outgoing, incoming, at, text in db.execute("SELECT * FROM handover"):
                decode_handover(text, boot, at)
                item = (outgoing, incoming, text)
                if at in cuts[boot] and cuts[boot][at] != item:
                    raise ValueError("conflicting handover evidence")
                cuts[boot][at] = item
            gaps.update(tuple(row) for row in db.execute(
                "SELECT boot_id,source,from_mono_ns,to_mono_ns,reason FROM gap"))
        # Preserve ownership evidence before materialization retracts whole
        # deltas. Zero filtered counters do not prove that local history was
        # absent, and the daemon cannot see another store's conflicting owner.
        self._has_local_history = any(
            name == "local" and start < (window.end_mono_ns or SQLITE_INT_MAX)
            and end > (window.start_mono_ns or 0)
            for name, _, _, _, start, end in evidence)
        # Each store's boot_order is authoritative only inside that store.
        # Shared boot IDs join these partial orders, never CLOCK_BOOTTIME or
        # wall-clock values from unrelated boots. Disconnected orders stay explicit.
        chronology, remaining = [], set(boots)
        active_boots = {row[2] for row in evidence}
        while remaining:
            available = [boot for boot in remaining if not predecessors[boot] & remaining]
            if not available:
                raise ValueError("conflicting boot chronology evidence")
            if (window.boot_id is None and len(available) > 1 and
                    len(active_boots & remaining) > 1):
                self._chronology_unproved = True
            chosen = min(available, key=boots.get)
            chronology.append(chosen)
            remaining.remove(chosen)
        selected = []
        for boot in chronology:
            entries = [row for row in evidence if row[2] == boot]
            beginning = min((row[4] for row in entries), default=0)
            ending = max((row[5] for row in entries), default=0)
            if boot == window.boot_id:
                beginning = min(beginning, window.start_mono_ns or 0)
                ending = max(ending, window.end_mono_ns or 0)
            if beginning >= ending:
                continue
            points = {beginning, ending}
            for row in entries:
                points.update(at for at in row[4:] if beginning < at < ending)
            chain = sorted(cuts[boot].items())
            exclusions = {"gui": [], "iod": []}
            if chain:
                # The producer owns cutoff semantics. Require a complete chain
                # in one durable source, rather than stitch unrelated offers.
                for service, _ in self._sources.values():
                    count = service.connection.execute("SELECT count(*) FROM handover WHERE boot_id=?", (boot,)).fetchone()[0]
                    if count == len(chain):
                        exclusions = ownership_exclusions(service.connection, boot)
                        break
                else:
                    raise ValueError("no complete preceding ownership chain")
            for index, (at, (outgoing, incoming, _)) in enumerate(chain):
                if index and chain[index-1][1][1] != outgoing:
                    raise ValueError("handover chain has a missing predecessor")
                if beginning < at < ending:
                    points.add(at)
            for gap_boot, _, left, right, _ in gaps:
                if gap_boot == boot:
                    points.update(at for at in (left, right) if beginning < at < ending)
            ordered = sorted(points)
            for left, right in zip(ordered, ordered[1:]):
                candidates = [row for row in entries if row[4] <= left and row[5] >= right]
                candidates = [row for row in candidates if not any(
                    a < right and b > left for a, b in exclusions[row[1]])]
                ambiguous = len({(row[1], row[3]) for row in candidates}) > 1
                persisted = [row for row in gaps if row[0] == boot and row[2] < right and row[3] > left]
                uncertain = ambiguous or any(row[4] == "overlap_uncertain" for row in persisted)
                if uncertain:
                    self._overlap_ranges.append((boot, left, right))
                # GUI visibility loss cannot override authoritative root evidence.
                owners = {candidate[1] for candidate in candidates}
                missing = any(row[1] in owners and
                              (row[4] != "gui_absent" or "iod" not in owners)
                              for row in persisted)
                if not candidates or uncertain or missing:
                    segment = HistorySegment("unproved", "unknown", boot, left, right, False)
                    self._gap_ranges.append((boot, left, right))
                else:
                    # Prefer a single freshest copy of a collector epoch. Multiple
                    # exports of the same receipt are mirrors, not additive data.
                    chosen = max(candidates, key=lambda row: (retained[(row[0], boot)],
                                                              row[5], row[0] == "local", row[0]))
                    segment = HistorySegment(chosen[0], chosen[1], boot, left, right, True)
                if (selected and selected[-1].collector_id == segment.collector_id
                        and selected[-1].owner == segment.owner and selected[-1].boot_id == boot
                        and selected[-1].end_ns == left and selected[-1].proven == segment.proven):
                    selected[-1] = replace(selected[-1], end_ns=right)
                else:
                    selected.append(segment)
        self._all_segments = tuple(selected)
        scoped = []
        for segment in selected:
            if window.boot_id is not None and segment.boot_id != window.boot_id:
                continue
            left = max(segment.start_ns, window.start_mono_ns or 0)
            right = min(segment.end_ns, window.end_mono_ns or SQLITE_INT_MAX)
            if left < right:
                scoped.append(replace(segment, start_ns=left, end_ns=right))
        self.segments = tuple(scoped)
        self._gap_ranges = [(s.boot_id, s.start_ns, s.end_ns) for s in scoped if not s.proven]
        self._known_gap_ranges = [
            (boot, max(left, window.start_mono_ns or 0),
             min(right, window.end_mono_ns or SQLITE_INT_MAX))
            for boot, _, left, right, reason in gaps
            if reason != "overlap_uncertain"
            and (window.boot_id is None or boot == window.boot_id)
            and left < (window.end_mono_ns or SQLITE_INT_MAX)
            and right > (window.start_mono_ns or 0)
            and max(left, window.start_mono_ns or 0) < min(right, window.end_mono_ns or SQLITE_INT_MAX)
        ]
        self._overlap_ranges = [(boot, max(left, window.start_mono_ns or 0),
                                 min(right, window.end_mono_ns or SQLITE_INT_MAX))
            for boot, left, right in self._overlap_ranges
            if (window.boot_id is None or boot == window.boot_id)
            and left < (window.end_mono_ns or SQLITE_INT_MAX) and right > (window.start_mono_ns or 0)]
        self.boundaries = tuple((a.boot_id, a.end_ns, a.owner, b.owner)
            for a, b in zip(scoped, scoped[1:]) if a.boot_id == b.boot_id and a.end_ns == b.start_ns
            and a.proven and b.proven and a.owner != b.owner)

    @staticmethod
    def _raw_complete(db, boot):
        for field in _COUNTERS:
            if db.execute(f"SELECT 1 FROM app_boot_total t WHERE boot_id=? AND {field}<>"
                          f"(SELECT coalesce(sum(s.{field}),0) FROM sample s WHERE s.boot_id=t.boot_id "
                          "AND s.app_id=t.app_id) LIMIT 1", (boot,)).fetchone():
                return False
        for field, raw in (("rbytes", "rbytes"), ("wbytes", "wbytes")):
            if db.execute(f"SELECT 1 FROM app_boot_dev_total t WHERE boot_id=? AND {field}<>"
                          f"(SELECT coalesce(sum(s.{raw}),0) FROM sample_dev s WHERE s.boot_id=t.boot_id "
                          "AND s.app_id=t.app_id AND s.dev=t.dev) LIMIT 1", (boot,)).fetchone():
                return False
        return True

    def _materialize(self):
        for name, (service, _) in self._sources.items():
            if not any(s.collector_id == name and s.proven for s in self._all_segments):
                # A discarded mirror is not missing evidence for the chosen one.
                if self._all_segments and all(s.proven for s in self._all_segments):
                    continue
                if any(s.proven for s in self._all_segments) and name != "local":
                    continue
            copy = sqlite3.connect(":memory:")
            try:
                if service.connection.in_transaction:
                    raise ValueError("history source has an uncommitted transaction")
                service.connection.backup(copy)
                store = AppIoStore(copy)
                for (boot,) in copy.execute("SELECT boot_id FROM boot").fetchall():
                    if self._window is not None and self._window.boot_id is not None and boot != self._window.boot_id:
                        continue
                    allowed = [(s.start_ns, s.end_ns) for s in self._all_segments
                               if s.collector_id == name and s.boot_id == boot and s.proven]
                    intervals = copy.execute("SELECT from_mono_ns,to_mono_ns FROM collector_interval "
                                             "WHERE boot_id=?", (boot,)).fetchall()
                    if not intervals and copy.execute(
                            "SELECT 1 FROM app_boot_total WHERE boot_id=? LIMIT 1", (boot,)).fetchone():
                        # A legacy aggregate is not an ownership receipt. With
                        # no interval horizon, lifetime cannot even bound a gap.
                        self._not_retained = True
                    # Correct only when a whole persisted interval is not selected.
                    needs_correction = (not intervals or any(
                        list(subtract_intervals(start, end, allowed)) for start, end in intervals))
                    if needs_correction and not self._raw_complete(copy, boot):
                        self._not_retained = True
                        self.segments = tuple(replace(s, proven=False) if s.collector_id == name
                                              and s.boot_id == boot else s for s in self.segments)
                        allowed = []
                        # Omit only this uncorrectable boot, preserving other
                        # boots' exact lifetime contributions and their peaks.
                        for app, *amounts in copy.execute(
                            "SELECT app_id,rchar,wchar,read_bytes,write_bytes FROM app_boot_total WHERE boot_id=?", (boot,)).fetchall():
                            store._subtract("app_lifetime_total", {"app_id": app},
                                            dict(zip(_COUNTERS, amounts, strict=True)))
                        for table in ("sample", "sample_dev", "sample_coverage", "rollup_app", "rollup_dev",
                                      "app_boot_total", "app_boot_dev_total"):
                            copy.execute(f"DELETE FROM {table} WHERE boot_id=?", (boot,))
                        copy.commit()
                        continue
                    horizon = max([end for _, end in intervals] + [0,
                        copy.execute("SELECT coalesce(max(observed_mono_ns),0) FROM sample WHERE boot_id=?",
                                     (boot,)).fetchone()[0]])
                    # The existing retraction preserves integer conservation and
                    # peaks. Work only on the private snapshot, never an export.
                    with store._transaction():
                        for left, right in subtract_intervals(0, min(horizon+1, SQLITE_INT_MAX), allowed):
                            store._exclude_contributions(boot, left, right)
                        for source, epoch, seq, at in copy.execute(
                            "SELECT DISTINCT source,epoch,seq,ts_mono_ns FROM sample_coverage WHERE boot_id=?",
                            (boot,)).fetchall():
                            if not any(left <= at <= right for left, right in allowed):
                                copy.execute("DELETE FROM sample_coverage WHERE boot_id=? AND source=? "
                                    "AND epoch=? AND seq=?", (boot, source, epoch, seq))
                # Gap provenance is deduplicated and overlaid by the routing plan,
                # rather than counted once for every copy of a daemon export.
                copy.execute("DELETE FROM gap")
                copy.commit()
                self._views.append((name, QueryService(copy)))
                copy = None
            finally:
                if copy is not None:
                    copy.close()

    @staticmethod
    def _read_local(paths, window):
        """Run SQLite and cached detail construction entirely off the UI thread."""
        sources = []
        worker = None
        try:
            for name, path, owner in paths:
                source = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=0.2)
                copy = sqlite3.connect(":memory:")
                try:
                    source.backup(copy)
                finally:
                    source.close()
                sources.append((name, QueryService(copy), owner))
            worker = HistoryRouter(sources[0][1], SimpleNamespace(state=None), AppRegistry())
            worker._sources = {name: (service, owner) for name, service, owner in sources}
            worker._window = window
            worker._plan(window)
            worker._materialize()
            worker.boundaries = tuple(boundary for boundary in worker.boundaries if all(
                any(s.boot_id == boundary[0] and s.proven and s.owner == owner and
                    (s.end_ns == boundary[1] if index == 2 else s.start_ns == boundary[1])
                    for s in worker.segments)
                for index, owner in enumerate(boundary[2:], 2)))
            worker._build_ranking(window)
            worker._ready = True
            details = {}
            for row in worker._ranking:
                for kind, method in (("totals", worker.disk_share), ("series", worker.series)):
                    try:
                        details[(kind, row.app_key)] = method(row.app_id, window)
                    except (ValueError, RuntimeError) as error:
                        # An A22-disabled series is not a zero-filled timeline.
                        details[(kind, row.app_key)] = error
            file_available = False
            for _, service, _ in sources:
                try:
                    if service.connection.execute(
                            "SELECT 1 FROM boot WHERE file_finalized_through_mono_ns > 0 LIMIT 1"
                    ).fetchone() is not None:
                        file_available = True
                except Exception:
                    # Optional file metadata must not suppress process history
                    # or prevent another source from proving file availability.
                    continue
            return {"ranking": worker._ranking, "parts": details, "segments": worker.segments,
                "boundaries": worker.boundaries, "gaps": worker._gap_ranges,
                "overlaps": worker._overlap_ranges, "local_history": worker._has_local_history,
                "not_retained": worker._not_retained,
                "chronology_unproved": worker._chronology_unproved,
                "file_available": file_available}
        finally:
            if worker is not None:
                worker.close()
            for _, service, _ in sources:
                service.connection.close()

    def prepare(self, window: Window) -> None:
        # Preparation may fail before a future exists to report unavailability.
        self._file_status = FileAvailability(False, False, "file_source_unavailable")
        self.local._precheck(window)
        if self._closed:
            raise RuntimeError("history router is closed")
        self._generation += 1
        self._cancel_file_requests()
        self._window, self._ready = window, False
        self.segments, self.boundaries = (), ()
        self._gap_ranges, self._known_gap_ranges, self._overlap_ranges = [], [], []
        self._has_local_history = False
        self._ranking, self._remote_parts, self._queue, self._cached_parts = [], {}, [], {}
        self._remote = self._remote_snapshot = None
        self._not_retained = self._chronology_unproved = False
        self.view_state = "loading"
        if self._local_future is not None:
            self._local_future.cancel()
        paths = []
        for name, (service, owner) in self._sources.items():
            if service.connection.in_transaction:
                raise ValueError("history source has an uncommitted transaction")
            path = service.connection.execute("PRAGMA database_list").fetchone()[2]
            if not path:
                raise ValueError("asynchronous history requires a file-backed SQLite source")
            paths.append((name, path, owner))
        self._local_generation = self._generation
        self._local_future = self._executor.submit(self._read_local, paths, window)
        state = self.client.state
        if state is not None and state.verified and state.answering:
            self._queue.append(QueryRequest("ranking", window))
            self._dispatch()
        self._file_status = FileAvailability(True, False)

    def _consume_local(self):
        if self._local_generation is None:
            return self._local_future is not None
        if self._local_generation != self._generation:
            return False
        if self._local_future is None or not self._local_future.done():
            return False
        try:
            data = self._local_future.result()
        except Exception:
            self._file_status = FileAvailability(False, False, "file_source_unavailable")
            raise
        self.segments, self.boundaries = data["segments"], data["boundaries"]
        self._gap_ranges, self._overlap_ranges = data["gaps"], data["overlaps"]
        self._has_local_history = data["local_history"]
        self._not_retained = self._not_retained or data["not_retained"]
        self._chronology_unproved = data["chronology_unproved"]
        self._file_status = FileAvailability(
            bool(data["file_available"]), False,
            None if data["file_available"] else "file_source_unavailable",
        )
        self._ranking = [replace(row, app_id=self.registry.id_for(row.app_key)) for row in data["ranking"]]
        self._cached_parts = data["parts"]
        self._local_generation = None
        return True

    def _build_ranking(self, window):
        grouped = defaultdict(list)
        for name, service in self._views:
            for row in service.ranking(window, "total_bytes"):
                grouped[row.app_key].append(row)
        self._ranking = []
        for key, rows in grouped.items():
            amounts = {field: sum(getattr(row, field) for row in rows) for field in _COUNTERS}
            self._ranking.append(replace(rows[0], app_id=self.registry.id_for(key), **amounts,
                total_bytes=amounts["read_bytes"]+amounts["write_bytes"],
                **{field: _peak(getattr(row, field) for row in rows) for field in _PEAKS},
                coverage=self._combine_coverage([row.coverage for row in rows])))

    def _combine_coverage(self, rows):
        coverage = _coverage(rows, gaps=len(self._merged_ranges(self._gap_ranges)),
            overlaps=len(self._merged_ranges(self._overlap_ranges)), unavailable=self._not_retained)
        return replace(coverage, complete=False) if self._chronology_unproved else coverage

    @staticmethod
    def _merged_ranges(ranges):
        merged = []
        for boot, left, right in sorted(ranges):
            if merged and merged[-1][0] == boot and merged[-1][2] >= left:
                merged[-1] = (boot, merged[-1][1], max(merged[-1][2], right))
            else:
                merged.append((boot, left, right))
        return merged

    def _dispatch(self):
        if self._pending is None and self._queue:
            request = self._queue.pop(0)
            self._pending = (self._generation, request, self.client.query(request))

    def _finish(self):
        self._ready = True
        self.view_state = "incomplete: not_retained" if self._not_retained else (
            "incomplete: ownership gap" if self._gap_ranges else (
                "incomplete: boot chronology unproved" if self._chronology_unproved else "ready"))

    def ready(self, window: Window) -> bool:
        if window != self._window:
            return False
        if self._ready:
            return True
        while self._pending is not None and self._pending[0] != self._generation and self._pending[2].done():
            # Discard an obsolete response before waiting on this scope's disk work.
            self._pending = None
            self._dispatch()
        try:
            local_ready = self._consume_local()
        except Exception:
            self.view_state = "unavailable: local history evidence"
            raise
        if not local_ready:
            return False
        while self._pending is not None and self._pending[2].done():
            generation, request, future = self._pending
            self._pending = None
            if generation == self._generation:
                try:
                    response = future.result()
                    if request.kind == "ranking":
                        rows = response["result"]
                        if (window.kind == "lifetime" or self._has_local_history or self._overlap_ranges
                                or any(row.total_bytes or row.rchar or row.wchar for row in self._ranking)):
                            raise ValueError("remote query cannot prove a mixed-store cutoff")
                        self._remote_snapshot = response.get("snapshot")
                        if self._remote_snapshot is None:
                            raise ValueError("remote history snapshot identity required")
                        if response.get("next_cursor") is not None:
                            raise ValueError("paginated remote ownership is not proved atomically")
                        if not rows:
                            raise ValueError("remote ownership evidence required")
                        # Incomplete coverage is an honest property of the
                        # daemon's retained observations (reaps, gaps, or
                        # inaccessible identities), not proof that the answer
                        # is unusable. Keep the rows and let their contracts
                        # disclose the uncertainty. Only an empty answer lacks
                        # enough evidence to distinguish "no apps" from a
                        # missing/unretained history window.
                        self._remote = [replace(row, app_id=self.registry.id_for(row.app_key)) for row in rows]
                        self._not_retained = self._not_retained or any(
                            row.coverage.unavailable_reason == "not_retained" for row in rows
                        )
                        for row in self._remote:
                            self._queue.extend([QueryRequest("totals", window, row.app_key),
                                                QueryRequest("series", window, row.app_key)])
                    else:
                        value = response["result"]
                        if value.window != window or response.get("snapshot") != self._remote_snapshot:
                            raise ValueError("remote history answered a different scope or snapshot")
                        self._remote_parts[(request.kind, request.app_key)] = value
                except Exception:
                    # Keep only locally proved/exported contributions. No stale
                    # remote result may populate a new window or widen its scope.
                    self._remote = None
                    self._queue = []
                    self._not_retained = True
            self._dispatch()
        self._dispatch()
        if self._pending is None and not self._queue:
            self._finish()
        return self._ready

    def _require(self, window):
        if not self.ready(window):
            raise RuntimeError("history is not ready for this window")

    def ranking(self, window: Window, metric: str) -> list[AppTotal]:
        validate_enum(metric, _METRICS, "metric")
        self._require(window)
        rows = self._remote if self._remote is not None else self._ranking
        if self._not_retained:
            rows = [replace(row, coverage=replace(row.coverage, complete=False,
                                                   unavailable_reason="not_retained")) for row in rows]
        return sorted(rows, key=lambda row: (-getattr(row, "total_bytes" if metric == "total_bytes" else metric),
                                             row.app_key.encode("utf-8")))

    def _app_parts(self, app_id, window, method):
        self._require(window)
        key = self.registry.key_for(app_id)
        kind = "totals" if method == "disk_share" else "series"
        if self._remote is not None:
            return [self._remote_parts[(kind, key)]]
        if self._local_future is not None:
            value = self._cached_parts[(kind, key)]
            if isinstance(value, Exception):
                raise value
            return [value]
        parts = []
        for _, service in self._views:
            row = service.connection.execute("SELECT app_id FROM app WHERE key=?", (key,)).fetchone()
            if row is not None:
                parts.append(getattr(service, method)(row[0], window))
        if not parts:
            raise KeyError(app_id)
        return parts

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        parts = self._app_parts(app_id, window, "disk_share")
        coverage = (self._combine_coverage([part.coverage for part in parts])
                    if self._remote is None and self._local_future is None else parts[0].coverage)
        if self._not_retained:
            coverage = replace(coverage, complete=False, unavailable_reason="not_retained")
        devices = {}
        for part in parts:
            for row in part.devices:
                key = (row.boot_id, row.dev)
                if key in devices:
                    old = devices[key]
                    row = replace(row, rbytes=row.rbytes+old.rbytes, wbytes=row.wbytes+old.wbytes,
                                  total_bytes=row.total_bytes+old.total_bytes)
                devices[key] = row
        available = all(part.device_read_bytes is not None for part in parts)
        task_read, task_write = (sum(getattr(part, field) for part in parts)
                                 for field in ("task_read_bytes", "task_write_bytes"))
        read, write = ((sum(row.rbytes for row in devices.values()), sum(row.wbytes for row in devices.values()))
                       if available else (None, None))
        return replace(parts[0], app_id=app_id, window=window, task_read_bytes=task_read,
            task_write_bytes=task_write, task_total_bytes=task_read+task_write, devices=tuple(devices.values()),
            device_read_bytes=read, device_write_bytes=write, device_total_bytes=None if read is None else read+write,
            residual_read=None if read is None else task_read-read,
            residual_write=None if write is None else task_write-write,
            residual_total=None if read is None else task_read+task_write-read-write, coverage=coverage)

    def series(self, app_id: int, window: Window) -> Series:
        parts = self._app_parts(app_id, window, "series")
        if len({part.tier for part in parts}) != 1:
            raise RuntimeError("mixed retention tiers cannot represent one series")
        points, devices, residuals = {}, {}, {}
        for part in parts:
            for row in part.points:
                key = (row.bucket_start_mono_ns, row.bucket_secs)
                old = points.get(key)
                points[key] = row if old is None else replace(row,
                    read_bytes=row.read_bytes+old.read_bytes, write_bytes=row.write_bytes+old.write_bytes,
                    **{field: _peak((getattr(row, field), getattr(old, field))) for field in _PEAKS},
                    spans_suspend=row.spans_suspend or old.spans_suspend)
            for row in part.device_points:
                key = (row.bucket_start_mono_ns, row.bucket_secs, row.dev)
                old = devices.get(key)
                devices[key] = row if old is None else replace(row, rbytes=row.rbytes+old.rbytes,
                                                               wbytes=row.wbytes+old.wbytes)
            for row in part.residual_points:
                key = (row.bucket_start_mono_ns, row.bucket_secs)
                old = residuals.get(key)
                residuals[key] = row if old is None else replace(row, **{
                    field: None if getattr(row, field) is None or getattr(old, field) is None
                    else getattr(row, field)+getattr(old, field) for field in ("read", "write", "total")})
        return replace(parts[0], app_id=app_id, window=window,
            points=tuple(points[key] for key in sorted(points)),
            device_points=tuple(devices[key] for key in sorted(devices)),
            residual_points=tuple(residuals[key] for key in sorted(residuals)),
            coverage=(self._combine_coverage([part.coverage for part in parts])
                      if self._remote is None and self._local_future is None else
                      replace(parts[0].coverage, complete=False, unavailable_reason="not_retained")
                      if self._not_retained else parts[0].coverage))
