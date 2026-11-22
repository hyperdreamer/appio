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
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from concurrent.futures import CancelledError, Future, InvalidStateError, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import tempfile
import time
from typing import Literal, cast

from .contracts import (
    AppTotal, Direction, DiskShare, FileCoverage, FileSort, FileTable, HistoryPending, Series, Window,
    FILE_HOUR_NS, LAUNCH_WINDOW_MESSAGE, WindowUnavailableError, SQLITE_INT_MAX,
    MAX_RANGE_NS, validate_enum, validate_search, validate_sqlite_integer, validate_window,
)
from .history_types import (
    AcceptedIdentity,
    AnswerProvenance,
    CapturedSourceSet,
    FrameProvenance,
    LocalBracket,
    LocalPosition,
    LocalPositionVector,
    ManagedSourceSpec,
    PartKind,
    PartPhase,
    PartState,
    RemotePosition,
    RetainedRecord,
    RetentionScope,
    SnapshotProvenance,
    SourcePosition,
    _boot_text,
    applied_endpoint,
    local_answer_digest,
    retention_scope,
    validate_scope,
)
from .history_validation import (
    _canonical_app_key,
    _normalize_request,
    _revalidate_tree,
    _validate_local_ranking,
    _validate_local_series,
    _validate_local_totals,
    _validate_served_bounds,
    validate_query_result,
)
from .history_source import (
    GapWriteResult, ManagedSourceRegistry, SourceAdmissionError, _snapshot_digest,
)
from .migrations import validate_schema
from .durable import decode_handover
from .file_runtime import FileAvailability
from .iodclient import (
    NO_ANSWER_NS,
    REQUEST_LIFETIME_NS,
    AppRegistry,
    ClientUnavailable,
    ConnectionLost,
    GapNotice,
    IodClient,
    IodClientError,
    RemoteError,
    RequestRejected,
)
from .ipc_models import QueryRequest
from .ownership import ownership_exclusions, subtract_intervals
from .query import QueryService, _model_coverage, _reduce_coverage
from .store import AppIoStore, _empty_coverage
from .tier import select_tier

_COUNTERS = ("rchar", "wchar", "read_bytes", "write_bytes")

#: Remote requests kept in flight while one frame is assembled. Protocol v1 answers
#: one query per client at a time and refuses a second with ``query_busy``, so the
#: router keeps exactly one outstanding; the frame is assembled by issuing the next
#: request the moment the previous one settles (the client wakes its owner on every
#: settled reply). A 211-app frame is 400+ round trips and must stay inside the
#: daemon's 10 s commit cadence for one snapshot to bind it.
MAX_INFLIGHT = 1
_PEAKS = ("peak_read_bps", "peak_write_bps", "peak_total_bps")
_METRICS = ("total_bytes", "read_bytes", "write_bytes", "rchar", "wchar")
_FILE_SORT_NAMES = (
    "bytes_total", "bytes_r", "bytes_w", "reads", "writes", "basename", "dev", "last_seen",
)

#: Charged local lanes (§3.3). A frame owns at most one Future on its own
#: executor; the background executor carries at most two detail Futures and
#: eight Files Futures, ten combined. A job stays charged from submission until
#: the owner drains its terminal completion, retired jobs included.
_DETAIL_CHARGE = 2
_FILES_CHARGE = 8
_BACKGROUND_CHARGE = 10

_TOKEN_ORIGINS = ("remote", "local")
_TOKEN_OWNERS = ("frame", "detail", "files")
_POSITION_OWNERS = ("gui", "iod")
_DETAIL_KINDS: tuple[PartKind, ...] = ("totals", "series")

#: Scheduler ranks (§5.1). Lower runs first; admission serial breaks ties and
#: no issued request is ever preempted.
_PRIORITY_FRAME_RANKING = 0
_PRIORITY_DETAIL = 1
_PRIORITY_FILES = 2
_PRIORITY_OTHER = 3


@dataclass(frozen=True)
class DaemonAnswerError:
    kind: Literal["remote_error", "unavailable", "rejected", "transport", "internal"]
    reason: str

    def __post_init__(self):
        if self.kind not in ("remote_error", "unavailable", "rejected", "transport", "internal"):
            raise ValueError("invalid daemon answer error kind")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("daemon answer error reason must be a non-empty string")


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


def _private_int(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}, got {value}")
    return value


def _private_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _private_enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _private_name(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} is not valid UTF-8 text") from error
    return value


def _private_interval(value: object, field: str) -> tuple[str, int, int]:
    """One nonempty half-open ``(boot, start, end)`` evidence interval."""

    if not isinstance(value, tuple) or len(value) != 3:
        raise ValueError(f"{field} must be a (boot, start, end) tuple")
    boot = _boot_text(value[0], f"{field}.boot_id")
    start = _private_int(value[1], f"{field}.start_mono_ns")
    end = _private_int(value[2], f"{field}.end_mono_ns")
    if start >= end:
        raise ValueError(f"{field} must be a nonempty half-open interval")
    return (boot, start, end)


def _private_registrations(value: object, field: str) -> tuple[tuple[str, str, int], ...]:
    """The ordered local registration vector used as one authority."""

    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple of registrations")
    names: list[bytes] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, tuple) or len(entry) != 3:
            raise ValueError(f"{field}[{index}] must be a (name, owner, registration_epoch) tuple")
        name = _private_name(entry[0], f"{field}[{index}].name")
        _private_enum(entry[1], _POSITION_OWNERS, f"{field}[{index}].owner")
        _private_int(entry[2], f"{field}[{index}].registration_epoch", minimum=1)
        names.append(name.encode("utf-8"))
    if len(set(names)) != len(names):
        raise ValueError(f"{field} names must be unique")
    if names != sorted(names):
        raise ValueError(f"{field} must be ordered by name UTF-8 bytes")
    return value


def _private_authority(value: object) -> tuple:
    """§3.2's tagged authority: a remote connection or a local registration list."""

    if not isinstance(value, tuple) or not value:
        raise ValueError("request_token.authority must be a tagged tuple")
    if value[0] == "remote":
        if len(value) != 4:
            raise ValueError("a remote authority is (remote, generation, boot, epoch)")
        _private_int(value[1], "request_token.authority.generation")
        _boot_text(value[2], "request_token.authority.boot_id")
        _private_int(value[3], "request_token.authority.iod_epoch", minimum=1)
        return value
    if value[0] == "local":
        if len(value) != 2:
            raise ValueError("a local authority is (local, registrations)")
        _private_registrations(value[1], "request_token.authority.registrations")
        return value
    raise ValueError("request_token.authority must be tagged remote or local")


def _validate_worker_snapshot(snapshot: object) -> SnapshotProvenance:
    if not isinstance(snapshot, SnapshotProvenance):
        raise ValueError("a local worker requires snapshot provenance")
    _revalidate_tree(snapshot)
    return snapshot


def _capture_provenance(capture: CapturedSourceSet) -> SnapshotProvenance:
    if not isinstance(capture, CapturedSourceSet):
        raise ValueError("a local worker requires an immutable captured source set")
    CapturedSourceSet.__post_init__(capture)
    if capture.snapshot_digest != _snapshot_digest(capture.images):
        raise ValueError("captured source set digest does not match its images")
    opening = tuple((source.name, source.owner, source.registration_epoch)
                    for source in capture.opening.sources)
    closing = tuple((source.name, source.owner, source.registration_epoch)
                    for source in capture.closing.sources)
    if opening != closing:
        raise ValueError("capture registration identities changed")
    return SnapshotProvenance(
        capture.capture_id, capture.snapshot_digest,
        LocalBracket(capture.opening, capture.closing))


@contextmanager
def _captured_sources(capture: CapturedSourceSet):
    """Open verified images as worker-private SQLite files and remove them."""

    snapshot = _capture_provenance(capture)
    sources = []
    owners = {source.name: source.owner for source in capture.opening.sources}
    with tempfile.TemporaryDirectory(prefix="appio-history-") as directory:
        try:
            for image in capture.images:
                descriptor, path = tempfile.mkstemp(dir=directory, suffix=".sqlite")
                with os.fdopen(descriptor, "wb") as image_file:
                    image_file.write(image.image)
                with open(path, "rb") as image_file:
                    digest = hashlib.file_digest(image_file, "sha256").hexdigest()
                if digest != image.image_digest:
                    raise ValueError("private source image digest mismatch")
                connection = sqlite3.connect(path, isolation_level=None, timeout=0.2)
                try:
                    validate_schema(connection)
                    service = QueryService(connection)
                except BaseException:
                    connection.close()
                    raise
                sources.append((image.name, service, owners[image.name]))
            yield sources, snapshot
        finally:
            for _, service, _ in sources:
                service.connection.close()


def _private_source(value: object, field: str) -> SourcePosition:
    if not isinstance(value, (RemotePosition, LocalPositionVector)):
        raise ValueError(f"{field} must be a RemotePosition or LocalPositionVector")
    _revalidate_tree(value)
    return value


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


# ---------------------------------------------------------------------------
# Private typed worker records (§3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestToken:
    """One admission: its normalized request, owner, authority and source."""

    token_id: int
    request: QueryRequest
    origin: Literal["remote", "local"]
    scope: RetentionScope | None
    owner: Literal["frame", "detail", "files"]
    owner_id: int
    slot_id: int | None
    authority: tuple
    requested_source: SourcePosition

    def __post_init__(self) -> None:
        _private_int(self.token_id, "request_token.token_id", minimum=1)
        request = _normalize_request(self.request)
        origin = _private_enum(self.origin, _TOKEN_ORIGINS, "request_token.origin")
        owner = _private_enum(self.owner, _TOKEN_OWNERS, "request_token.owner")
        _private_int(self.owner_id, "request_token.owner_id", minimum=1)
        if owner == "detail":
            if self.scope is None:
                raise ValueError("a detail token owns its retention scope")
            scope = validate_scope(self.scope)
            if request.app_key is None:
                raise ValueError("a detail token requires its external subject")
            if scope != retention_scope(request.app_key, request.window):
                raise ValueError("a detail token scope must match its subject and window")
        elif self.scope is not None:
            raise ValueError("only a detail token carries a retention scope")
        slot = self.slot_id
        if origin == "remote":
            _private_int(slot, "request_token.slot_id", minimum=1)
        elif slot is not None:
            raise ValueError("a local token carries no wire slot")
        _private_authority(self.authority)
        source = _private_source(self.requested_source, "request_token.requested_source")
        if origin == "remote" and not isinstance(source, RemotePosition):
            raise ValueError("a remote token requests a remote position")
        if origin == "local" and not isinstance(source, LocalPositionVector):
            raise ValueError("a local token requests a local position")


@dataclass(frozen=True)
class LocalFrameOutcome:
    """Everything one frame copy proves, with no eager detail or handle."""

    worker_id: int
    request: QueryRequest
    ranking: tuple[AppTotal, ...]
    segments: tuple[HistorySegment, ...]
    boundaries: tuple[tuple[str, int, str, str], ...]
    gaps: tuple[tuple[str, int, int], ...]
    known_gaps: tuple[tuple[str, int, int], ...]
    overlaps: tuple[tuple[str, int, int], ...]
    has_local_history: bool
    not_retained: bool
    chronology_unproved: bool
    file_available: bool
    snapshot: SnapshotProvenance

    def __post_init__(self) -> None:
        _private_int(self.worker_id, "local_frame.worker_id", minimum=1)
        request = _normalize_request(self.request)
        if request.kind != "ranking" or request.app_key is not None:
            raise ValueError("a local frame answers exactly one ranking request")
        if not isinstance(self.ranking, tuple):
            raise ValueError("local_frame.ranking must be an immutable tuple")
        _validate_local_ranking(request, self.ranking)

        if not isinstance(self.segments, tuple):
            raise ValueError("local_frame.segments must be a tuple")
        previous: dict[str, HistorySegment] = {}
        for segment in self.segments:
            if not isinstance(segment, HistorySegment):
                raise ValueError("local_frame.segments contains only HistorySegment records")
            _revalidate_tree(segment)
            # The pre-existing segment validator checks identity truthiness, not
            # text type/encoding, and the recursive walk visits primitives only.
            # A frame outcome promises real UTF-8 boot identities, so validate
            # every nested segment here rather than trusting re-entry alone.
            _boot_text(segment.boot_id, "local_frame.segments.boot_id")
            last = previous.get(segment.boot_id)
            if last is not None and (segment.start_ns <= last.start_ns
                                     or segment.start_ns < last.end_ns):
                raise ValueError("local_frame.segments must keep planner chronology order")
            previous[segment.boot_id] = segment

        if not isinstance(self.boundaries, tuple):
            raise ValueError("local_frame.boundaries must be a tuple")
        boot_rank: dict[str, int] = {}
        for segment in self.segments:
            boot_rank.setdefault(segment.boot_id, len(boot_rank))
        seen_transitions: set[tuple[str, int, str, str]] = set()
        last_boot_rank = -1
        last_at: dict[str, int] = {}
        for boundary in self.boundaries:
            if not isinstance(boundary, tuple) or len(boundary) != 4:
                raise ValueError("a boundary is (boot, at_ns, from_owner, to_owner)")
            boot = _boot_text(boundary[0], "local_frame.boundaries.boot_id")
            at_ns = _private_int(boundary[1], "local_frame.boundaries.at_ns")
            outgoing = _private_enum(boundary[2], _POSITION_OWNERS, "local_frame.boundaries.from_owner")
            incoming = _private_enum(boundary[3], _POSITION_OWNERS, "local_frame.boundaries.to_owner")
            if outgoing == incoming:
                raise ValueError("a boundary joins two different proven owners")
            # Boundaries are a chronology, not an unordered evidence set: keep
            # planner boot order, strictly increasing in-boot instants and one
            # record per real transition. Never sort malformed order into valid.
            if boot not in boot_rank:
                raise ValueError("a boundary must belong to a copied boot")
            rank = boot_rank[boot]
            if rank < last_boot_rank:
                raise ValueError("local_frame.boundaries must keep boot chronology order")
            last_boot_rank = rank
            transition = (boot, at_ns, outgoing, incoming)
            if transition in seen_transitions:
                raise ValueError("local_frame.boundaries must not repeat a transition")
            seen_transitions.add(transition)
            previous_at = last_at.get(boot)
            if previous_at is not None and at_ns <= previous_at:
                raise ValueError("local_frame.boundaries must strictly increase within a boot")
            last_at[boot] = at_ns
            if not any(s.proven and s.boot_id == boot and s.owner == outgoing and s.end_ns == at_ns
                       for s in self.segments):
                raise ValueError("a boundary must follow a proven outgoing segment")
            if not any(s.proven and s.boot_id == boot and s.owner == incoming and s.start_ns == at_ns
                       for s in self.segments):
                raise ValueError("a boundary must precede a proven incoming segment")

        for name in ("gaps", "known_gaps", "overlaps"):
            evidence = getattr(self, name)
            if not isinstance(evidence, tuple):
                raise ValueError(f"local_frame.{name} must be a tuple")
            for interval in evidence:
                _private_interval(interval, f"local_frame.{name}[]")
        for name in ("has_local_history", "not_retained", "chronology_unproved", "file_available"):
            _private_bool(getattr(self, name), f"local_frame.{name}")
        _validate_worker_snapshot(self.snapshot)


@dataclass(frozen=True)
class LocalPartOutcome:
    """One lazily computed subject part and its canonical local digest."""

    worker_id: int
    request: QueryRequest
    app_key: str
    value: DiskShare | Series
    data_digest: str
    snapshot: SnapshotProvenance

    def __post_init__(self) -> None:
        _private_int(self.worker_id, "local_part.worker_id", minimum=1)
        request = _normalize_request(self.request)
        if request.kind not in ("totals", "series"):
            raise ValueError("a local part answers totals or series only")
        app_key = _canonical_app_key(self.app_key)
        if app_key != request.app_key:
            raise ValueError("a local part key must equal its request subject")
        if request.kind == "totals":
            if not isinstance(self.value, DiskShare):
                raise ValueError("a totals part carries a DiskShare")
            _validate_local_totals(request, app_key, self.value)
        else:
            if not isinstance(self.value, Series):
                raise ValueError("a series part carries a Series")
            _validate_local_series(request, app_key, self.value)
        if not isinstance(self.data_digest, str) or len(self.data_digest) != 64 \
                or set(self.data_digest) - set("0123456789abcdef"):
            raise ValueError("a local part digest is 64 lowercase hexadecimal characters")
        if self.data_digest != local_answer_digest(request.kind, app_key, request.window, self.value):
            raise ValueError("a local part digest must match its accepted payload")
        _validate_worker_snapshot(self.snapshot)


@dataclass(frozen=True)
class LocalFilesOutcome:
    """One ownership-proved Files page plus its local served evidence."""

    worker_id: int
    request: QueryRequest
    app_key: str
    table: FileTable
    coverage: FileCoverage
    served_gap_count: int | None
    snapshot: SnapshotProvenance

    def __post_init__(self) -> None:
        _private_int(self.worker_id, "local_files.worker_id", minimum=1)
        request = _normalize_request(self.request)
        if request.kind != "files":
            raise ValueError("a local Files outcome answers a files request")
        if request.cursor is not None:
            raise ValueError("a local Files outcome is a first page, never a continuation")
        app_key = _canonical_app_key(self.app_key)
        if app_key != request.app_key:
            raise ValueError("a local Files key must equal its request subject")
        if not isinstance(self.table, FileTable):
            raise ValueError("local_files.table must be a FileTable")
        _revalidate_tree(self.table)
        if not isinstance(self.coverage, FileCoverage):
            raise ValueError("local_files.coverage must be a FileCoverage")
        _revalidate_tree(self.coverage)
        if self.table.window != request.window or self.coverage.window != request.window:
            raise ValueError("local Files evidence must carry the exact requested window")
        if (self.table.sort, self.table.direction, self.table.search) != (
                request.sort, request.direction, request.search):
            raise ValueError("local Files evidence must echo the requested selector")
        if len(self.table.rows) > request.limit:
            raise ValueError("a local Files page exceeds the requested page limit")
        if self.table.app_id != self.coverage.app_id:
            raise ValueError("local Files coverage must match the table snapshot")
        if self.table.coverage != self.coverage.coverage:
            raise ValueError("local Files coverage must match the table coverage")
        if self.table.unknown_owner_overflow != self.coverage.unknown_owner_overflow:
            raise ValueError("local Files coverage must match the table unknown-owner evidence")
        for evidence in (self.table.coverage, self.coverage.coverage):
            if evidence.unavailable_reason is not None:
                raise ValueError("a published local Files outcome is available")
        served = self.coverage.served_interval
        if served is None:
            raise ValueError("local Files evidence requires a served interval")
        _validate_served_bounds(served, request)
        if self.served_gap_count is not None:
            _private_int(self.served_gap_count, "local_files.served_gap_count")
        _validate_worker_snapshot(self.snapshot)


@dataclass(frozen=True)
class RemoteSettlement:
    """One terminal remote outcome, bound to the Future that supplied it."""

    future: Future[dict]
    response: Mapping[str, object] | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.future, Future):
            raise ValueError("a remote settlement requires its bound Future")
        if self.response is not None and not isinstance(self.response, Mapping):
            raise ValueError("a remote settlement response must be a mapping")
        if self.error is not None and not isinstance(self.error, Exception):
            raise ValueError("a remote settlement error must be an Exception")
        if (self.response is None) == (self.error is None):
            raise ValueError("a remote settlement carries exactly one result or error")


@dataclass(frozen=True)
class LocalSettlement:
    """One terminal local worker outcome for its exact admitted worker."""

    worker_id: int
    capture: CapturedSourceSet | None = None
    outcome: LocalFrameOutcome | LocalPartOutcome | LocalFilesOutcome | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        _private_int(self.worker_id, "local_settlement.worker_id", minimum=1)
        capture = self.capture
        if capture is not None:
            if not isinstance(capture, CapturedSourceSet):
                raise ValueError("a local settlement carries a CapturedSourceSet")
            CapturedSourceSet.__post_init__(capture)
        outcome = self.outcome
        if (outcome is None) == (self.error is None):
            raise ValueError("a local settlement carries exactly one result or error")
        if outcome is not None:
            if not isinstance(outcome, (LocalFrameOutcome, LocalPartOutcome, LocalFilesOutcome)):
                raise ValueError("a local settlement carries a typed local outcome")
            # Re-enter the outcome's own validation. QueryRequest is a
            # dataclass without __post_init__, so the shared recursive walk
            # would incorrectly descend into it.
            validator = cast(Callable[[LocalOutcome], None], type(outcome).__post_init__)
            validator(outcome)
            if outcome.worker_id != self.worker_id:
                raise ValueError("a local settlement must carry its admitted worker id")
        if self.error is not None and not isinstance(self.error, Exception):
            raise ValueError("a local settlement error must be an Exception")


SettlementInput = RemoteSettlement | LocalSettlement
LocalOutcome = LocalFrameOutcome | LocalPartOutcome | LocalFilesOutcome


@dataclass(frozen=True)
class _SourceStamp:
    """The immutable identity of one managed registration."""

    name: str
    owner: str
    registration_epoch: int

    def __post_init__(self) -> None:
        _private_name(self.name, "registration_stamp.name")
        _private_enum(self.owner, _POSITION_OWNERS, "registration_stamp.owner")
        _private_int(self.registration_epoch, "registration_stamp.registration_epoch", minimum=1)


@dataclass(frozen=True)
class _LocalCopyBinding:
    """Owner-side request, registration and immutable snapshot association.

    Legacy path slots stay owner-side during the staged constructor cutover.
    Workers receive the captured bytes and the exact request and worker ID.
    """

    worker_id: int
    token_id: int | None
    request: QueryRequest
    registrations: tuple[_SourceStamp, ...]
    paths: tuple[tuple[str, str, str], ...]
    opening: LocalPositionVector
    snapshot: SnapshotProvenance | None = None

    def __post_init__(self) -> None:
        _private_int(self.worker_id, "local_binding.worker_id", minimum=1)
        if self.token_id is not None:
            _private_int(self.token_id, "local_binding.token_id", minimum=1)
        _normalize_request(self.request)
        if not isinstance(self.registrations, tuple) or not self.registrations:
            raise ValueError("a local binding carries its registered sources")
        for stamp in self.registrations:
            if not isinstance(stamp, _SourceStamp):
                raise ValueError("a local binding carries only registration stamps")
            _SourceStamp.__post_init__(stamp)
        if not isinstance(self.paths, tuple) or len(self.paths) != len(self.registrations):
            raise ValueError("a local binding carries one path per registration")
        for stamp, entry in zip(self.registrations, self.paths, strict=True):
            if (not isinstance(entry, tuple) or len(entry) != 3
                    or entry[0] != stamp.name or entry[2] != stamp.owner
                    or not isinstance(entry[1], str)):
                raise ValueError("a local binding path must match its registration")
        if not isinstance(self.opening, LocalPositionVector):
            raise ValueError("a local binding carries its opening vector")
        if len(self.opening.sources) != len(self.registrations):
            raise ValueError("a local binding opening vector must match its registrations")
        for stamp, position in zip(self.registrations, self.opening.sources, strict=True):
            if ((position.name, position.owner, position.registration_epoch)
                    != (stamp.name, stamp.owner, stamp.registration_epoch)):
                raise ValueError(
                    "a local binding opening vector must match its registrations")
        if self.snapshot is not None:
            _validate_worker_snapshot(self.snapshot)
            if self.snapshot.bracket.opening != self.opening:
                raise ValueError("a local binding snapshot must match its opening vector")


@dataclass(frozen=True)
class _FilesBinding:
    """The request and managed snapshot association for one local Files job.

    The captured bracket stays in snapshot provenance. Files keeps its existing
    private-image ownership and served-interval acceptance predicates.
    """

    worker_id: int
    token_id: int
    request: QueryRequest
    registrations: tuple[_SourceStamp, ...]
    paths: tuple[tuple[str, str, str], ...]
    snapshot: SnapshotProvenance | None = None

    def __post_init__(self) -> None:
        _private_int(self.worker_id, "files_binding.worker_id", minimum=1)
        _private_int(self.token_id, "files_binding.token_id", minimum=1)
        _normalize_request(self.request)
        if self.request.kind != "files":
            raise ValueError("a Files binding answers a files request")
        if not isinstance(self.registrations, tuple) or not self.registrations:
            raise ValueError("a Files binding carries its registered sources")
        for stamp in self.registrations:
            if not isinstance(stamp, _SourceStamp):
                raise ValueError("a Files binding carries only registration stamps")
            _SourceStamp.__post_init__(stamp)
        if not isinstance(self.paths, tuple) or len(self.paths) != len(self.registrations):
            raise ValueError("a Files binding carries one path per registration")
        for stamp, entry in zip(self.registrations, self.paths, strict=True):
            if (not isinstance(entry, tuple) or len(entry) != 3
                    or entry[0] != stamp.name or entry[2] != stamp.owner
                    or not isinstance(entry[1], str)):
                raise ValueError("a Files binding path must match its registration")
        if self.snapshot is not None:
            _validate_worker_snapshot(self.snapshot)
            positions = self.snapshot.bracket.opening.sources
            identities = tuple((stamp.name, stamp.owner, stamp.registration_epoch)
                               for stamp in self.registrations)
            if tuple((position.name, position.owner, position.registration_epoch)
                     for position in positions) != identities:
                raise ValueError("a Files binding snapshot must match its registrations")


class _FrameCopyPaths(tuple):
    """One frame's registered copy paths, carrying their admission binding.

    The frame worker keeps its two-argument ``(paths, window)`` shape so
    fixtures that substitute ``_read_local`` stay valid, while the copy itself
    receives the exact admitted worker id and normalized request. The copy can
    then return an outcome under its own binding and the owner never relabels a
    foreign result with its own identity (§3.4, §4.3).
    """

    binding: _LocalCopyBinding

    def __new__(cls, binding: _LocalCopyBinding) -> _FrameCopyPaths:
        self = super().__new__(cls, binding.paths)
        self.binding = binding
        return self


@dataclass(frozen=True)
class _QueueEntry:
    """One admitted historical request waiting for the single wire slot."""

    token: RequestToken
    priority: int
    serial: int

    def __post_init__(self) -> None:
        if not isinstance(self.token, RequestToken):
            raise ValueError("a queue entry carries its admitted request token")
        _private_int(self.priority, "queue_entry.priority")
        _private_int(self.serial, "queue_entry.serial", minimum=1)


@dataclass
class _PartSlot:
    """One exact-scope detail slot inside one selected lifetime.

    Mutable owner-thread state only: no token, record or observation here ever
    crosses an owner boundary. ``stamps`` keeps the identity-to-first-seen
    lookup for this exact scope so a repeated accepted identity is not
    restamped merely because another value was accepted in between (§2.4).
    """

    kind: PartKind
    scope: RetentionScope
    phase: PartPhase = "absent"
    token: RequestToken | None = None
    record: RetainedRecord | None = None
    reason: str | None = None
    error: Exception | None = None
    stamps: dict[AcceptedIdentity, int] = field(default_factory=dict)


@dataclass
class _DetailLifetime:
    """One selected subject/scope lifetime; resets allocate a new id.

    The selected ranking proof survives preparation. ``local_tokens`` binds its
    local ownership decision to pending remote requests until they terminate.
    """

    app_key: str
    family: tuple
    lifetime_id: int
    endpoint: int | None
    slots: dict[tuple[str, RetentionScope], _PartSlot] = field(default_factory=dict)
    live: dict[str, RequestToken] = field(default_factory=dict)
    successors: dict[str, RequestToken] = field(default_factory=dict)
    ranking_provenance: AnswerProvenance | None = None
    local_tokens: set[int] = field(default_factory=set)


@dataclass
class _FilesOwner:
    """One outward Files view and the private page sequence it assembles.

    Owner-thread mutable state only. Every page and the final file-coverage
    answer is a separate immutable :class:`RequestToken` with its own scheduler
    slot; this record never crosses into a worker and publishes no partial
    table. ``snapshot``/``connection``/``cursor`` are the ranking snapshot and
    connection/cursor authority captured when the view was admitted, and
    ``cursors`` remembers every opaque continuation already visited so a page
    can neither repeat nor skip.
    """

    owner_id: int
    generation: int
    outward: Future
    origin: str
    app_id: int
    app_key: str
    window: Window
    sort: str
    direction: str
    search: str | None
    authority: tuple
    requested_source: SourcePosition
    snapshot: str | None = None
    connection: int | None = None
    cursor: object = None
    pages: list = field(default_factory=list)
    cursors: set[str] = field(default_factory=set)
    rows: int = 0
    retired: bool = False
    current: RequestToken | None = None

    def __post_init__(self) -> None:
        _private_int(self.owner_id, "files_owner.owner_id", minimum=1)
        _private_int(self.generation, "files_owner.generation")
        if not isinstance(self.outward, Future):
            raise ValueError("a Files owner carries its outward Future")
        _private_enum(self.origin, _TOKEN_ORIGINS, "files_owner.origin")
        _private_int(self.app_id, "files_owner.app_id", minimum=1)
        _private_name(self.app_key, "files_owner.app_key")
        if not isinstance(self.window, Window):
            raise ValueError("a Files owner carries its exact requested Window")
        validate_window(self.window)
        _private_enum(self.sort, _FILE_SORT_NAMES, "files_owner.sort")
        _private_enum(self.direction, ("asc", "desc"), "files_owner.direction")
        if self.search is not None:
            _private_name(self.search, "files_owner.search")
        _private_authority(self.authority)
        _private_source(self.requested_source, "files_owner.requested_source")
        if self.cursor is not None and not isinstance(
                getattr(self.cursor, "boot_id", None), str):
            raise ValueError("a Files owner carries its exact issue cursor")
        if not isinstance(self.pages, list):
            raise ValueError("a Files owner accumulates an owner-side page list")
        if not isinstance(self.cursors, set):
            raise ValueError("a Files owner remembers visited continuations")
        _private_int(self.rows, "files_owner.rows")
        _private_bool(self.retired, "files_owner.retired")


@dataclass
class _FrameOwner:
    """One admitted frame generation's mutable owner-side context."""

    owner_id: int
    window: Window
    requested_source: SourcePosition | None
    authority: tuple
    local_token: RequestToken
    remote_token: RequestToken | None
    local_binding: _LocalCopyBinding
    local_settled: bool = False
    remote_settled: bool = False
    remote_candidate: dict | None = None
    remote_applied: bool = False
    retired: bool = False
    error: Exception | None = None
    certificate: FrameProvenance | None = None
    ranking_provenance: dict[str, AnswerProvenance] = field(default_factory=dict)
    local_bracket: LocalBracket | None = None
    answered_source: SourcePosition | None = None
    remote_future: Future | None = None
    remote_answered: SourcePosition | None = None


@dataclass(eq=False)
class _GapSubmission:
    notice: GapNotice
    registration_epoch: int | None
    future: Future[GapWriteResult] | None
    error: Exception | None = None


@dataclass(frozen=True)
class _Preparation:
    owner_id: int
    window: Window
    authority: tuple
    requested_source: SourcePosition | None
    fence: tuple[_GapSubmission, ...]


@dataclass(frozen=True)
class _LocalFileChoices:
    window: Window | None = None
    applications: tuple[tuple[str, str, int], ...] = ()
    error: Exception | None = None

    def file_applications(self, window: Window) -> tuple[tuple[str, str, int], ...]:
        if self.window != window:
            raise HistoryPending("file application choices are still loading")
        if self.error is not None:
            raise self.error
        return self.applications


@dataclass(frozen=True)
class _CaptureMetadata:
    snapshot: SnapshotProvenance
    boot_id: str | None
    choices: _LocalFileChoices


class HistoryRouter:
    def __init__(self, sources: ManagedSourceRegistry, client: IodClient,
                 registry: AppRegistry, *,
                 clock_ns: Callable[[], int] = time.monotonic_ns):
        if not isinstance(sources, ManagedSourceRegistry):
            raise SourceAdmissionError("history source is not managed")
        if not callable(clock_ns):
            raise ValueError("clock_ns must be callable")
        self.sources, self.client, self.registry = sources, client, registry
        self._clock_ns = clock_ns
        self._capture_provider = sources
        self._capture_jobs: dict[int, tuple[RequestToken, Future]] = {}
        self._worker_captures: dict[int, int] = {}
        self._worker_capture_sets: dict[int, CapturedSourceSet] = {}
        self._frame_jobs: dict[int, Future] = {}
        self._frame_metadata: dict[int, Future] = {}
        self.local = _LocalFileChoices()
        self._boot_id: str | None = None
        self._boot_registrations = sources.registration_vector()
        self._boot_capture: Future | None = None
        self._boot_future: Future | None = None
        self._boot_error: Exception | None = None
        self._registrations_seen = sources.registration_vector()
        self.segments: tuple[HistorySegment, ...] = ()
        self.boundaries: tuple[tuple[str, int, str, str], ...] = ()
        self.view_state = "unavailable"
        self._window = None
        self._generation = 0
        self._ready = False
        self._views: list[tuple[str, QueryService]] = []
        self._ranking: list[AppTotal] = []
        self._remote: list[AppTotal] | None = None
        self._remote_answered: SourcePosition | None = None
        self._remote_snapshot: str | None = None
        #: Scope-owned detail (§3.1-3.2): one selected lifetime, never a
        #: cross-subject cache, plus the view's last adopted applied window.
        self._detail: _DetailLifetime | None = None
        self._next_lifetime_id = 1
        self._applied_window: Window | None = None
        #: Immutable row proofs from the last observed applied ranking. A newly
        #: selected subject needs its ownership decision during preparation too.
        self._applied_ranking_provenance: dict[str, AnswerProvenance] = {}
        #: One wire occupant and its priority-stable queue (§5.1).
        self._queue: list[_QueueEntry] = []
        self._occupant: tuple[RequestToken, Future] | None = None
        #: The Future and actual cursor provenance bound when a queued token is
        #: issued. Admission source remains separate for frame certification.
        self._remote_futures: dict[int, Future] = {}
        self._remote_issue_sources: dict[int, RemotePosition | None] = {}
        self._admission_serial = 0
        #: Settlement guards: a token settles exactly once, and a publication
        #: retired token can never publish even if its Future completes.
        self._settled_tokens: set[int] = set()
        self._retired_tokens: set[int] = set()
        #: Admitted local detail tokens waiting for background capacity.
        self._unscheduled: dict[int, RequestToken] = {}
        #: Outward Files views and their private page sequences (§3.4, §5.4).
        #: Each delivery owns at most one live token; pages and coverage are
        #: separately admitted scheduler slots, never recursive direct sends.
        self._file_owners: dict[int, _FilesOwner] = {}
        self._next_files_id = 1
        #: The current frame owner and its readiness/authority context (§3.4).
        self._frame: _FrameOwner | None = None
        self._frame_error: Exception | None = None
        self._authority: tuple | None = None
        self._ranking_stamps: dict[AcceptedIdentity, int] = {}
        self._deferred_admissions: list[PartKind] = []
        self._gap_ranges: list[tuple[str, int, int]] = []
        self._known_gap_ranges: list[tuple[str, int, int]] = []
        self._overlap_ranges: list[tuple[str, int, int]] = []
        self._has_local_history = False
        self._not_retained = False
        self._chronology_unproved = False
        self.transport_issues: list[GapNotice] = []
        self._gap_writes: list[_GapSubmission] = []
        self._preparation: _Preparation | None = None
        self._all_segments = ()
        # Two independent execution lanes (§3.3): a frame copy may never queue
        # behind lazy detail or Files work, and vice versa. `_executor` stays a
        # compatibility alias for the frame lane used by existing fixtures.
        self._frame_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="appio-frame")
        self._background_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="appio-detail-files")
        self._executor = self._frame_executor
        #: Immutable token-bound outcomes; only an owner drain may read this.
        self._completed: queue.SimpleQueue = queue.SimpleQueue()
        #: Charged background submissions, keyed by their admitted worker id.
        self._worker_jobs: dict[int, tuple[str, Future | None]] = {}
        #: A settlement has been removed from the owner queue, but its worker
        #: Future may still be running while queue publication returns. The
        #: charge remains in ``_worker_jobs`` with a None Future until this map
        #: reaches terminal completion.
        self._drained_futures: dict[int, Future] = {}
        #: Legacy Files submissions carry no request token or queued settlement,
        #: so only these terminal charges may be reaped at a later admission.
        #: A helper token's binding stays until its settlement is drained.
        self._legacy_files_workers: set[int] = set()
        self._next_token_id = 1
        self._local_paths: tuple[tuple[str, str, str], ...] | None = None
        self._local_future: Future | None = None
        #: The frame owns one entry; detail copies own one until their settlement.
        self._worker_bindings: dict[int, _LocalCopyBinding] = {}
        #: Local Files worker associations (no opening vector, no bracket).
        self._files_bindings: dict[int, _FilesBinding] = {}
        self._local_worker_id: int | None = None
        self._local_settlement: LocalSettlement | None = None
        self._daemon_answer_error: DaemonAnswerError | None = None
        self._local_generation: int | None = None
        self._closed = False
        self._file_jobs: set[Future] = set()
        self._file_status = FileAvailability(True, False)

    def _allocate_token_id(self) -> int:
        """A router-wide, never-reused positive token/worker id (§3.4)."""

        token_id = self._next_token_id
        # An explicitly admitted helper id can already occupy a number that the
        # sequence has not reached yet. A generated id never takes a live
        # binding, and the counter only moves forward.
        while token_id in self._worker_jobs:
            token_id += 1
        self._next_token_id = token_id + 1
        return token_id

    @staticmethod
    def _precheck(window: Window) -> None:
        validate_window(window)
        if window.kind == "launch" or (
                window.kind == "range"
                and window.end_mono_ns - window.start_mono_ns > MAX_RANGE_NS):
            select_tier(window, None)

    @staticmethod
    def _read_capture_metadata(capture: CapturedSourceSet, window: Window | None) -> _CaptureMetadata:
        with _captured_sources(capture) as (sources, snapshot):
            boot_id = None
            applications: dict[str, tuple[str, int]] = {}
            choice_error = None
            successful_sources = 0
            for name, service, _owner in sources:
                if name == "local":
                    row = service.connection.execute(
                        "SELECT boot_id FROM boot ORDER BY boot_order DESC LIMIT 1").fetchone()
                    if row is not None:
                        boot_id = _boot_text(row[0], "local boot")
                if window is not None:
                    try:
                        rows = service.file_applications(window)
                    except Exception as error:
                        choice_error = error
                        continue
                    successful_sources += 1
                    for key, label, total in rows:
                        previous = applications.get(key)
                        if previous is None or total > previous[1]:
                            applications[key] = (label, total)
            choices = _LocalFileChoices(window, tuple(
                (key, label, total) for key, (label, total) in sorted(applications.items())),
                choice_error if not successful_sources else None)
            return _CaptureMetadata(snapshot, boot_id, choices)

    def _local_boot(self) -> str | None:
        """Select the primary boot from completed actor-captured evidence."""

        if self._closed:
            return None
        identities = self.sources.registration_vector()
        if (tuple(item for item in identities if item[0] == "local")
                != tuple(item for item in self._boot_registrations if item[0] == "local")):
            self._boot_id = None
        self._collect_boot()
        if self._boot_error is not None:
            error, self._boot_error = self._boot_error, None
            raise error
        if self._boot_id is None and self._boot_capture is None and self._boot_future is None:
            self._boot_capture = self.sources.capture()
        return self._boot_id

    def _collect_boot(self) -> None:
        capture_future = self._boot_capture
        if capture_future is not None and capture_future.done():
            self._boot_capture = None
            capture = None
            try:
                capture = capture_future.result()
                self.sources.retain_capture(capture.capture_id)
                self.sources.release_capture(capture.capture_id)
                self._boot_future = self._frame_executor.submit(
                    self._read_capture_metadata, capture, None)
                sources = self.sources
                def release_boot_capture(_completed: Future,
                                         capture_id: int = capture.capture_id) -> None:
                    sources.release_capture(capture_id)
                self._boot_future.add_done_callback(release_boot_capture)
            except Exception as error:
                self._boot_error = error
                if capture is not None:
                    self._discard_capture(self.sources, capture_future)
        future = self._boot_future
        if future is not None and future.done():
            self._boot_future = None
            try:
                metadata = future.result()
                identities = tuple((item.name, item.owner, item.registration_epoch)
                                   for item in metadata.snapshot.bracket.opening.sources)
                if identities == self.sources.registration_vector():
                    self._boot_id = metadata.boot_id
                    self._boot_registrations = identities
            except Exception as error:
                self._boot_error = error

    def local_position(self, source_name: str) -> LocalPosition:
        """Return the last completed actor observation for one source."""

        positions = self.sources.observe().sources
        for position in positions:
            if position.name == source_name:
                return position
        raise KeyError(source_name)

    def _local_vector(self) -> LocalPositionVector:
        return self.sources.observe()

    def source_position(self) -> SourcePosition:
        """The current source authority: a remote cursor or the local vector."""

        state = self.client.state
        if state is not None and state.verified and state.answering:
            cursor = state.cursor
            if cursor is not None:
                return RemotePosition(cursor.boot_id, cursor.iod_epoch, cursor.seq)
        return self._local_vector()

    def retire_managed_source(self, name: str, reason: str) -> None:
        self.sources.retire(name, reason)
        self.invalidate()

    def replace_managed_source(self, name: str, spec: ManagedSourceSpec) -> None:
        if not isinstance(spec, ManagedSourceSpec):
            raise SourceAdmissionError("history source is not managed")
        future = self.sources.replace(name, spec)
        future.add_done_callback(lambda _completed: self._notify())
        self.invalidate()

    def _managed_sources(self) -> ManagedSourceRegistry:
        return self.sources

    def _capture_local_binding(self, *, worker_id: int, token_id: int | None = None,
                               request: QueryRequest,
                               capture: CapturedSourceSet | None = None) -> _LocalCopyBinding:
        """Bind a request to completed actor observations or its exact capture."""

        sources = self._managed_sources()
        opening = sources.observe() if capture is None else capture.opening
        stamps = tuple(_SourceStamp(position.name, position.owner,
                                    position.registration_epoch)
                       for position in opening.sources)
        paths = tuple((position.name, "", position.owner) for position in opening.sources)
        snapshot = None if capture is None else _capture_provenance(capture)
        return _LocalCopyBinding(worker_id, token_id, request, stamps, paths, opening, snapshot)

    def _capture_files_binding(self, *, worker_id: int, token_id: int,
                               request: QueryRequest,
                               capture: CapturedSourceSet | None = None) -> _FilesBinding:
        binding = self._capture_local_binding(
            worker_id=worker_id, token_id=token_id, request=request, capture=capture)
        return _FilesBinding(worker_id, token_id, request, binding.registrations,
                             binding.paths, binding.snapshot)

    def _request_local_capture(self, token: RequestToken) -> Future:
        future = self._managed_sources().capture()
        self._capture_jobs[token.token_id] = (token, future)
        if token.owner != "frame":
            self._bind_background_worker(token.token_id, token.owner, future)
        return future

    def _release_worker_capture(self, worker_id: int) -> None:
        capture_id = self._worker_captures.pop(worker_id, None)
        self._worker_capture_sets.pop(worker_id, None)
        if capture_id is not None:
            self._managed_sources().release_capture(capture_id)

    @staticmethod
    def _discard_capture(sources: ManagedSourceRegistry, future: Future) -> None:
        if future.cancelled() or future.exception() is not None:
            return
        try:
            sources.release_capture(future.result().capture_id)
        except SourceAdmissionError:
            # Registry close already reaps an unclaimed late capture.
            pass

    def _collect_captures(self) -> bool:
        """Admit immutable captures to workers after their actor Future completes."""

        changed = False
        for worker_id, (token, future) in tuple(self._capture_jobs.items()):
            if not future.done():
                continue
            live = (not self._closed and worker_id not in self._retired_tokens
                    and token.authority == self._current_authority())
            if token.owner == "frame":
                frame = self._frame
                live = live and frame is not None and not frame.retired
                live = live and frame is not None and frame.owner_id == token.owner_id
            elif token.owner == "detail":
                live = live and self._detail_token_live(token)
            else:
                owner = self._file_owners.get(token.owner_id)
                live = live and owner is not None and not owner.outward.done()
            if not live:
                self._discard_capture(self._managed_sources(), future)
                self._capture_jobs.pop(worker_id)
                self._worker_bindings.pop(worker_id, None)
                self._files_bindings.pop(worker_id, None)
                self._reap_background_worker(worker_id)
                continue
            try:
                capture = future.result()
                if not isinstance(capture, CapturedSourceSet):
                    raise SourceAdmissionError("managed source returned an invalid capture")
                local_binding = self._capture_local_binding(
                    worker_id=worker_id, token_id=worker_id,
                    request=token.request, capture=capture)
                identities = tuple((item.name, item.owner, item.registration_epoch)
                                   for item in capture.opening.sources)
                sources = self._managed_sources()
                if identities != sources.registration_vector():
                    raise SourceAdmissionError("managed source registrations changed")
                sources.retain_capture(capture.capture_id)
                sources.release_capture(capture.capture_id)
                self._worker_captures[worker_id] = capture.capture_id
                self._worker_capture_sets[worker_id] = capture
                if token.owner == "frame":
                    frame = self._frame
                    if frame is None or frame.retired:
                        raise SourceAdmissionError("local frame owner is no longer active")
                    token = replace(token, requested_source=capture.opening)
                    frame.local_token = token
                    frame.local_binding = local_binding
                    if frame.remote_token is None:
                        frame.requested_source = capture.opening
                    self._worker_bindings[worker_id] = local_binding
                    frame_reader: Callable[..., LocalFrameOutcome] = self._read_local
                    metadata_future: Future[_CaptureMetadata] = Future()
                    self._frame_metadata[worker_id] = metadata_future

                    def read_frame(
                        capture: CapturedSourceSet = capture,
                        token: RequestToken = token,
                        reader: Callable[..., LocalFrameOutcome] = frame_reader,
                        metadata_future: Future[_CaptureMetadata] = metadata_future,
                    ) -> LocalFrameOutcome:
                        try:
                            metadata = HistoryRouter._read_capture_metadata(
                                capture, token.request.window)
                            metadata_future.set_result(metadata)
                            return reader(
                                capture, token.request.window,
                                worker_id=token.token_id, request=token.request)
                        except BaseException as error:
                            if not metadata_future.done():
                                metadata_future.set_exception(error)
                            raise

                    self._local_future = self._frame_executor.submit(read_frame)
                    self._frame_jobs[worker_id] = self._local_future
                    self._file_status = FileAvailability(True, False)
                elif token.owner == "files":
                    owner = self._file_owners.get(token.owner_id)
                    if owner is None:
                        raise SourceAdmissionError("Files owner is no longer active")
                    file_binding = _FilesBinding(
                        worker_id, worker_id, token.request,
                        local_binding.registrations, local_binding.paths,
                        local_binding.snapshot)
                    app_id = owner.app_id

                    files_reader: Callable[..., LocalFilesOutcome] = self._read_local_files

                    def read_files(
                        capture: CapturedSourceSet = capture,
                        token: RequestToken = token,
                        app_id: int = app_id,
                        reader: Callable[..., LocalFilesOutcome] = files_reader,
                    ) -> LocalFilesOutcome:
                        request = token.request
                        return reader(
                            capture, request.window, request.app_key, app_id,
                            request.sort, request.direction, request.search,
                            worker_id=token.token_id, request=request)

                    self._worker_jobs[worker_id] = (token.owner, None)
                    self._submit_background_worker(
                        token, read_files, binding=file_binding,
                        capture=capture, reserved=True)
                else:
                    part_reader: Callable[..., LocalPartOutcome] = self._read_local_part

                    def read_part(
                        capture: CapturedSourceSet = capture,
                        token: RequestToken = token,
                        reader: Callable[..., LocalPartOutcome] = part_reader,
                    ) -> LocalPartOutcome:
                        return reader(
                            capture, token.request, worker_id=token.token_id,
                            app_key=token.request.app_key)

                    self._worker_jobs[worker_id] = (token.owner, None)
                    self._submit_background_worker(
                        token, read_part, binding=local_binding,
                        capture=capture, reserved=True)
            except Exception as error:  # noqa: BLE001 - exact current capture/submission failure
                changed |= self._settle(token, LocalSettlement(worker_id, error=error))
                if worker_id in self._worker_captures:
                    self._release_worker_capture(worker_id)
                else:
                    self._discard_capture(self._managed_sources(), future)
                self._reap_background_worker(worker_id)
            finally:
                self._capture_jobs.pop(worker_id, None)
        return changed

    def _reap_terminal_frame_workers(self) -> None:
        for worker_id, future in tuple(self._frame_jobs.items()):
            if future.done():
                self._frame_jobs.pop(worker_id)
                self._frame_metadata.pop(worker_id, None)
                self._worker_bindings.pop(worker_id, None)
                self._release_worker_capture(worker_id)

    def _closing_vector(self, binding: _LocalCopyBinding) -> LocalPositionVector:
        """Return the registry's owner-side closing observation."""

        closing = self.sources.observe()
        identities = tuple((position.name, position.owner, position.registration_epoch)
                           for position in closing.sources)
        expected = tuple((stamp.name, stamp.owner, stamp.registration_epoch)
                         for stamp in binding.registrations)
        if identities != expected:
            raise ValueError("history source registrations changed during the copy")
        return closing

    def _close_local_settlement(self, settlement: LocalSettlement, *,
                                binding: _LocalCopyBinding | None = None
                                ) -> LocalSettlement:
        """Validate the capture carried by one admitted local settlement."""

        if not isinstance(settlement, LocalSettlement):
            raise ValueError("a local settlement requires a local settlement record")
        if binding is None:
            binding = self._worker_bindings.get(settlement.worker_id)
        if binding is None:
            raise ValueError("a local settlement has no captured copy binding")
        capture = settlement.capture
        if settlement.outcome is None or capture is None or binding.snapshot is None:
            raise ValueError("a local settlement requires its admitted capture")
        snapshot = _validate_worker_snapshot(settlement.outcome.snapshot)
        capture_snapshot = _capture_provenance(capture)
        if (snapshot.capture_id != binding.snapshot.capture_id
                or capture_snapshot.capture_id != binding.snapshot.capture_id):
            raise ValueError("a local settlement answered another capture")
        if snapshot != binding.snapshot or capture_snapshot != snapshot:
            raise ValueError("a local settlement answered another snapshot")
        return settlement

    def _background_charge(self, owner: str) -> int:
        return sum(1 for charged_owner, _ in self._worker_jobs.values() if charged_owner == owner)

    def _background_capacity(self, owner: str) -> bool:
        """Whether one more charged background job of ``owner`` is admissible."""

        if owner not in ("detail", "files"):
            raise ValueError("the background lane carries detail and files workers only")
        if self._closed or len(self._worker_jobs) >= _BACKGROUND_CHARGE:
            return False
        limit = _DETAIL_CHARGE if owner == "detail" else _FILES_CHARGE
        return self._background_charge(owner) < limit

    def _submit_background_worker(
        self, token: RequestToken, work: Callable[[], LocalOutcome],
        *, binding: _LocalCopyBinding | _FilesBinding | None = None,
        capture: CapturedSourceSet | None = None,
        reserved: bool = False,
    ) -> Future | None:
        """Charge one background slot and submit an immutable-outcome worker.

        The submission is charged until the owner drains its terminal
        completion, never released by a done callback. The worker callback only
        wraps the returned immutable outcome/error and enqueues it; it reads no
        registered connection and mutates no router state.
        """

        if not isinstance(token, RequestToken):
            raise ValueError("a background worker needs its admitted request token")
        if token.origin != "local" or token.owner not in ("detail", "files"):
            raise ValueError("only a local detail or Files token owns a background worker")
        worker_id: int | None
        if reserved:
            if self._worker_jobs.get(token.token_id) != (token.owner, None):
                raise ValueError("a capture requires its reserved background slot")
            worker_id = token.token_id
        else:
            if token.token_id in self._worker_jobs:
                raise ValueError("a request token admits one worker only")
            worker_id = self._reserve_background_worker(token.owner, worker_id=token.token_id)
        if worker_id is None:
            return None
        if binding is None:
            # Capture the admission binding immediately before submission; a
            # refused capture releases the reservation and publishes nothing.
            # Only frame/detail copies need an opening vector; a Files binding
            # is the worker association, never a bracket (§3.4).
            try:
                if token.owner == "files":
                    self._files_bindings[worker_id] = self._capture_files_binding(
                        worker_id=worker_id, token_id=token.token_id,
                        request=token.request)
                else:
                    self._worker_bindings[worker_id] = self._capture_local_binding(
                        worker_id=worker_id, token_id=token.token_id,
                        request=token.request)
            except Exception:
                self._reap_background_worker(worker_id)
                raise
        elif token.owner == "files":
            if not isinstance(binding, _FilesBinding):
                raise ValueError("a Files worker requires its Files binding")
            self._files_bindings[worker_id] = binding
        else:
            if not isinstance(binding, _LocalCopyBinding):
                raise ValueError("a detail worker requires its local binding")
            self._worker_bindings[worker_id] = binding

        def run() -> None:
            try:
                settlement: SettlementInput = LocalSettlement(
                    worker_id, capture=capture, outcome=work())
            except Exception as error:  # noqa: BLE001 - the owner reports it once
                settlement = LocalSettlement(worker_id, capture=capture, error=error)
            self._completed.put((token, settlement))

        try:
            future = self._background_executor.submit(run)
        except RuntimeError:
            if not reserved:
                self._reap_background_worker(worker_id)
            raise
        self._bind_background_worker(worker_id, token.owner, future)
        return future

    def _bind_background_worker(self, worker_id: int, owner: str,
                                future: Future) -> None:
        """Attach a submitted Future to the one reservation that charged it.

        A live helper binding is never overwritten: the id authority above and
        this binding check keep every submitted Future under its own charge.
        """

        if self._worker_jobs.get(worker_id) != (owner, None):
            raise RuntimeError("a charged background slot was rebound")
        self._worker_jobs[worker_id] = (owner, future)

    def _reserve_background_worker(self, owner: str, *,
                                   worker_id: int | None = None) -> int | None:
        """Reserve one charged background slot before any executor submission.

        Every Future submitted to the shared background executor -- lazy detail,
        helper Files work and the legacy local Files path -- reserves here first,
        so one authoritative ledger enforces the two/eight/ten bounds. A binding
        stays charged until the owner-thread path reaps it; a done callback never
        releases capacity. ``worker_id`` is the admitted token id for helper work
        and an allocated id for the legacy Files path that has no token yet.
        """

        self._reap_terminal_legacy_files()
        if not self._background_capacity(owner):
            return None
        if worker_id is None:
            worker_id = self._allocate_token_id()
        elif worker_id in self._worker_jobs:
            return None
        else:
            # Explicit helper ids join the same never-reused sequence, so a
            # later implicit allocation may never hand this number out again.
            self._next_token_id = max(self._next_token_id, worker_id + 1)
        self._worker_jobs[worker_id] = (owner, None)
        return worker_id

    def _reap_terminal_background_workers(self) -> None:
        """Owner-thread reaping of every terminal-but-undrained charge.

        A running Future is a live resource, never a terminal charge, so close()
        preserves it here; only cancellation or completion leaves the ledger.
        """

        for worker_id, future in tuple(self._drained_futures.items()):
            if future.done():
                self._reap_background_worker(worker_id)
        if self._closed:
            for worker_id, (_owner, job_future) in tuple(self._worker_jobs.items()):
                if job_future is not None and job_future.done():
                    self._reap_background_worker(worker_id)

    def _reap_terminal_legacy_files(self) -> None:
        """Reap completed legacy Files charges, which have no queued settlement.

        A helper token's terminal job must keep its binding until its enqueued
        settlement is drained, so admission-time reaping stays narrow.
        """

        for worker_id in tuple(self._legacy_files_workers):
            entry = self._worker_jobs.get(worker_id)
            if entry is not None and entry[1] is not None and entry[1].done():
                self._reap_background_worker(worker_id)

    def _discard_completed(self, worker_id: int) -> None:
        """Drop queued settlements for one reaped worker without publishing."""

        kept: list[tuple[RequestToken, SettlementInput]] = []
        while True:
            try:
                item = self._completed.get_nowait()
            except queue.Empty:
                break
            if item[0].token_id != worker_id:
                kept.append(item)
        for item in kept:
            self._completed.put(item)

    def _reap_background_worker(self, worker_id: int) -> None:
        """Release a charged background job after its terminal completion.

        Publication retirement never erases a resource: a closed router still
        reaps through this same owner path, and any queued outcome for that
        worker is dropped instead of reaching a widget or caller.
        """

        if self._worker_jobs.pop(worker_id, None) is None:
            return
        self._drained_futures.pop(worker_id, None)
        self._release_worker_capture(worker_id)
        self._worker_bindings.pop(worker_id, None)
        self._files_bindings.pop(worker_id, None)
        self._legacy_files_workers.discard(worker_id)
        if self._closed:
            self._discard_completed(worker_id)

    @property
    def daemon_answer_error(self) -> DaemonAnswerError | None:
        return self._daemon_answer_error

    @property
    def local_read_in_flight(self) -> bool:
        return self._local_future is not None and not self._local_future.done()

    @property
    def pending_work(self) -> bool:
        """True while any frame, detail, Files or transport work needs draining.

        A queued, running, completed-but-undrained or retired-held request keeps
        the console's poll armed; it never waits for a target calculation.
        """

        if (self._boot_capture is not None or self._boot_future is not None
                or self._preparation is not None
                or any(item.future is not None for item in self._gap_writes)
                or self._occupant is not None or self._queue or self._worker_jobs
                or self._capture_jobs or self._frame_jobs
                or self._unscheduled or self._file_owners
                or not self._completed.empty()):
            return True
        frame = self._frame
        if (frame is None or frame.retired or frame.local_settled
                or self._local_future is None):
            return False
        # A frame whose generation was retired (invalidate/prepare) is not work
        # any drain can collect; only the live generation's copy still is.
        return self._local_generation == self._generation

    @property
    def _pending(self):
        """The oldest unsettled wire work, or None when nothing is outstanding."""

        if self._occupant is not None:
            token, future = self._occupant
            return (self._generation, token.request, future)
        if self._queue:
            entry = self._queue[0]
            return (self._generation, entry.token.request, None)
        return None

    def _cancel_file_requests(self):
        """Retire every live Files delivery through its administrative arm.

        Publication retirement only: a running local worker keeps its charge and
        a sent transport occupant stays held until its genuine terminal outcome
        is drained. No outward Future is completed from a second path here.
        """

        for owner in tuple(self._file_owners.values()):
            token = owner.current
            if token is None:
                self._file_owners.pop(owner.owner_id, None)
                owner.retired = True
                self._settle_file(owner.outward, owner.generation, error=WindowUnavailableError(
                    "file_source_unavailable", "File history generation retired."))
            else:
                self._settle(token, retiring=True)

    def _settle_file(self, future, generation, value=None, error=None):
        if future.done():
            return
        if generation != self._generation or self._closed:
            error = WindowUnavailableError("file_source_unavailable", "File history generation retired.")
        try:
            if error is not None:
                if not isinstance(error, (WindowUnavailableError, IodClientError)):
                    reason = str(error) or type(error).__name__
                    original = error
                    error = WindowUnavailableError("file_source_unavailable", reason)
                    error.__cause__ = original
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
    def _read_local_files(capture: CapturedSourceSet, window, key, app_id, sort, direction, search, *,
                          worker_id: int | None = None,
                          request: QueryRequest | None = None) -> LocalFilesOutcome:
        """Read one ownership-proved store on the background executor, never sum IDs.

        Process retraction cannot retract top-100 hourly F candidates. Prove
        ownership over each query's reported served interval, retaining its
        original window and coverage disclosure. Never use _materialize's
        unfiltered F tables or make process retention a prerequisite for F.

        The returned immutable outcome includes the capture provenance.
        """
        if request is None:
            request = QueryRequest("files", window, key, sort=sort, direction=direction,
                                   search=search)
        if worker_id is None:
            worker_id = 1
        with _captured_sources(capture) as (sources, snapshot):
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
                worker = HistoryRouter._snapshot_planner(sources, served)
                try:
                    worker._plan(served)
                    if not HistoryRouter._check_file_provenance(worker, served, name):
                        continue  # A discarded mirror cannot supply the rows.
                    results.append(result)
                except WindowUnavailableError as error:
                    if provenance_error is None:
                        provenance_error = error
                finally:
                    worker._close_views()
            if provenance_error is not None:
                raise provenance_error
            if not results:
                raise unavailable
            if len(results) != 1:
                raise WindowUnavailableError("file_source_unavailable", "File ownership cannot be represented by one store.")
            result = results[0]
            return LocalFilesOutcome(worker_id, request, key, result.table, result.coverage,
                                     result.served_gap_count, snapshot)

    def request_file_view(self, app_id: int, window: Window, sort: FileSort,
                          direction: Direction, search: str | None) -> Future[FileViewResult]:
        """Admit one outward Files view; every page is a scheduler slot.

        Admission validates the selector, captures the ranking snapshot and
        connection/cursor authority, and enqueues only the first page (or the
        one local worker). Continuations and the final file coverage are
        separately admitted by :meth:`_settle`, never sent from a callback and
        never accumulated in a partial outward table.
        """

        # A22 must win even over a bad app/sort, just as on the RPC surface.
        self._precheck(window)
        validate_enum(sort, _FILE_SORT_NAMES, "sort")
        validate_enum(direction, ("asc", "desc"), "direction")
        validate_search(search)
        future: Future[FileViewResult] = Future()
        generation = self._generation
        if len(self._file_jobs) >= 8:
            future.set_exception(RequestRejected("query_busy", "File history queue is full."))
            return future
        self._file_jobs.add(future)
        future.add_done_callback(self._file_jobs.discard)
        owner = None
        try:
            self._require(window)
            key = self.registry.key_for(app_id)
            remote = self._remote is not None
            owner = _FilesOwner(
                self._next_files_id, generation, future,
                "remote" if remote else "local", app_id, key,
                window, sort, direction, search, self._current_authority(),
                self.source_position(),
            )
            self._next_files_id += 1
            self._file_owners[owner.owner_id] = owner
            if remote:
                self._admit_remote_files(owner)
            else:
                self._admit_local_files(owner)
        except Exception as error:
            if owner is not None:
                self._file_owners.pop(owner.owner_id, None)
            self._settle_file(future, generation, error=error)
        return future

    def _admit_remote_files(self, owner: _FilesOwner) -> None:
        """Capture the ranking snapshot/cursor and enqueue the first page."""

        snapshot = self._remote_snapshot
        state = self.client.state
        if not self.file_source_status().available or snapshot is None:
            raise WindowUnavailableError(
                "file_source_unavailable", "Daemon file source unavailable.")
        owner.snapshot = snapshot
        owner.connection = getattr(self.client, "generation", None)
        owner.cursor = state.cursor
        self._enqueue_files_token(owner, QueryRequest(
            "files", owner.window, owner.app_key, sort=owner.sort,
            direction=owner.direction, search=owner.search))

    def _admit_local_files(self, owner: _FilesOwner) -> None:
        """Reserve a bounded slot and request its actor-owned snapshot."""

        request = QueryRequest("files", owner.window, owner.app_key, sort=owner.sort,
                               direction=owner.direction, search=owner.search)
        worker_id = self._allocate_token_id()
        token = RequestToken(
            worker_id, request, "local", None, "files", owner.owner_id, None,
            owner.authority, owner.requested_source)
        if self._reserve_background_worker("files", worker_id=worker_id) is None:
            raise RequestRejected("query_busy", "File history queue is full.")
        owner.current = token
        try:
            self._files_bindings[worker_id] = self._capture_files_binding(
                worker_id=worker_id, token_id=worker_id, request=token.request)
            self._request_local_capture(token)
        except Exception:
            self._reap_background_worker(worker_id)
            owner.current = None
            raise

    def _enqueue_files_token(self, owner: _FilesOwner, request: QueryRequest) -> RequestToken:
        """Admit one immutable Files page/coverage token as a priority-2 slot."""

        token = RequestToken(
            self._allocate_token_id(), request, "remote", None, "files",
            owner.owner_id, self._allocate_token_id(), owner.authority,
            owner.requested_source)
        owner.current = token
        self._queue.append(_QueueEntry(token, _PRIORITY_FILES, self._next_serial()))
        self._dispatch()
        return token

    def _files_source_current(self, owner: _FilesOwner) -> bool:
        """Whether the Files owner's exact snapshot/cursor authority still holds."""

        state = self.client.state
        return (
            state is not None and state.verified and state.answering
            and state.cursor == owner.cursor
            and getattr(self.client, "generation", None) == owner.connection
        )

    def _accept_files_remote(self, token: RequestToken, response: Mapping[str, object],
                             future: Future) -> bool:
        """Owner predicates and private page assembly for one accepted answer."""

        owner = self._file_owners.get(token.owner_id)
        if owner is None or owner.retired:
            return False
        if owner.generation != self._generation or self._closed:
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "File history generation retired."),
                future=future)
        if not self._files_source_current(owner):
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "Stale file history response."),
                future=future)
        if response.get("snapshot") != owner.snapshot:
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "Remote file scope or snapshot changed."),
                future=future)
        if token.request.kind == "files":
            return self._accept_files_page(owner, token, response, future)
        return self._accept_files_coverage(owner, token, response, future)

    def _accept_files_page(self, owner: _FilesOwner, token: RequestToken,
                           response: Mapping[str, object], future: Future) -> bool:
        table = response["result"]
        if not isinstance(table, FileTable):
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "Invalid remote file page."),
                future=future)
        if owner.pages and replace(table, rows=owner.pages[0].rows) != owner.pages[0]:
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "Remote file page metadata changed."),
                future=future)
        owner.pages.append(table)
        owner.rows += len(table.rows)
        if owner.rows > 50_000 or len(owner.pages) > 128:
            return self._files_failure(token, RequestRejected(
                "query_too_large", "File history exceeds the bounded view budget."),
                future=future)
        continuation = response.get("next_cursor")
        if continuation is None:
            self._settled_tokens.add(token.token_id)
            # The final coverage page is a newly admitted priority-2 slot.
            self._enqueue_files_token(owner, QueryRequest(
                "coverage", owner.window, owner.app_key, coverage_kind="file"))
            return False
        if not isinstance(continuation, str) or not continuation or not table.rows \
                or continuation in owner.cursors:
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "Invalid file continuation."),
                future=future)
        # Opaque server HMAC, forwarded unchanged. Never decode, recompute,
        # skip a page, repeat a cursor, or fabricate a local continuation.
        owner.cursors.add(continuation)
        self._settled_tokens.add(token.token_id)
        self._enqueue_files_token(owner, replace(token.request, cursor=continuation))
        return False

    def _accept_files_coverage(self, owner: _FilesOwner, token: RequestToken,
                               response: Mapping[str, object], future: Future) -> bool:
        coverage = response["result"]
        if not isinstance(coverage, FileCoverage) or not owner.pages:
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "Invalid remote file coverage."),
                future=future)
        table = replace(
            owner.pages[0],
            rows=tuple(row for page in owner.pages for row in page.rows),
            app_id=owner.app_id,
        )
        try:
            value = self._check_file_result(
                table, replace(coverage, app_id=owner.app_id), None)
        except Exception as error:  # noqa: BLE001 - acceptance failure of this owner
            return self._files_failure(token, error, future=future)
        self._settled_tokens.add(token.token_id)
        return self._files_complete(token, value)

    def _accept_files_local(self, token: RequestToken, outcome: LocalFilesOutcome,
                            settlement: LocalSettlement) -> bool:
        """Revalidate and publish one local Files worker's immutable outcome."""

        owner = self._file_owners.get(token.owner_id)
        if owner is None or owner.retired:
            return False
        if owner.generation != self._generation or self._closed:
            return self._files_failure(token, WindowUnavailableError(
                "file_source_unavailable", "File history generation retired."))
        if settlement.capture is None:
            return self._files_failure(token, RequestRejected("invalid_result", "invalid_result"))
        try:
            LocalFilesOutcome.__post_init__(outcome)
        except Exception:  # noqa: BLE001 - a forged outcome never publishes
            return self._files_failure(token, RequestRejected("invalid_result", "invalid_result"))
        if (outcome.request != token.request or outcome.app_key != token.request.app_key
                or outcome.table.app_id != owner.app_id
                or outcome.coverage.app_id != owner.app_id):
            return self._files_failure(token, RequestRejected("invalid_result", "invalid_result"))
        try:
            value = self._check_file_result(
                outcome.table, outcome.coverage, outcome.served_gap_count)
        except Exception as error:  # noqa: BLE001 - acceptance failure of this owner
            return self._files_failure(token, error)
        self._settled_tokens.add(token.token_id)
        return self._files_complete(token, value)

    def _files_complete(self, token: RequestToken, value: FileViewResult) -> bool:
        owner = self._file_owners.pop(token.owner_id, None)
        if owner is None:
            return False
        self._settle_file(owner.outward, owner.generation, value=value)
        return True

    def _files_failure(self, token: RequestToken, error: Exception, *,
                       future: Future | None = None) -> bool:
        self._settled_tokens.add(token.token_id)
        if future is not None:
            # Rejection evicts exactly the cache entry this bound Future
            # produced, before the outward failure and never a successor.
            try:
                self.client.reject_query_result(future)
            except Exception:  # noqa: BLE001 - an evicted/absent entry is a no-op
                pass
        owner = self._file_owners.pop(token.owner_id, None)
        if owner is None:
            return False
        self._settle_file(owner.outward, owner.generation, error=error)
        return True

    def invalidate(self, *, _before_preparation: bool = False) -> None:
        # A fenced preparation has allocated an owner but has issued no work.
        # Earlier writes retire the prior delivery before activating that owner.
        if not _before_preparation:
            self._generation += 1
            self._preparation = None
        self._cancel_file_requests()
        self._file_status = FileAvailability(True, False)
        self._local_generation = -1
        if self._local_future is not None:
            self._local_future.cancel()
        self._retire_frame_publication()
        self._ready = False
        self.view_state = "unavailable"

    def _retire_frame_publication(self) -> None:
        """Retire the admitted frame's publication rights without canceling it.

        The running local Future and any sent transport occupant stay real
        resources; their terminal completions are drained and ignored. This is
        the generation seam's own retirement, distinct from authority change.
        """

        frame = self._frame
        if frame is None or frame.retired:
            return
        frame.retired = True
        self._retired_tokens.add(frame.local_token.token_id)
        if frame.remote_token is not None:
            self._retired_tokens.add(frame.remote_token.token_id)
        self._queue = [entry for entry in self._queue
                       if not (entry.token.owner == "frame"
                               and entry.token.owner_id == frame.owner_id)]
        frame.remote_candidate = None
        frame.certificate = None
        frame.ranking_provenance = {}
        self._remote = None
        self._remote_answered = None
        self._remote_snapshot = None
        self._ready = False

    def add_managed_source(self, spec: ManagedSourceSpec) -> Future:
        if not isinstance(spec, ManagedSourceSpec):
            raise SourceAdmissionError("history source is not managed")
        future = self.sources.register(spec)
        future.add_done_callback(lambda _completed: self._notify())
        return future

    def add_source(self, spec: ManagedSourceSpec) -> Future:
        """Compatibility entry point for immutable managed source specs only."""

        return self.add_managed_source(spec)

    def _primary_registration_epoch(self) -> int | None:
        for name, owner, epoch in self._managed_sources().registration_vector():
            if name == "local" and owner == "gui":
                return epoch
        return None

    def record_transport_gap(self, notice: GapNotice) -> None:
        """Append the callback immediately and enqueue qualifying actor work."""

        if self._closed:
            return
        self.transport_issues.append(notice)
        if not isinstance(notice, GapNotice):
            raise ValueError("transport gap requires a GapNotice")
        GapNotice.__post_init__(notice)
        if (not notice.boot_id or notice.from_mono_ns is None or notice.to_mono_ns is None
                or notice.from_mono_ns < 0):
            return
        epoch = self._primary_registration_epoch()
        future = self._managed_sources().record_transport_gap(notice)
        self._gap_writes.append(_GapSubmission(notice, epoch, future))

    @property
    def gap_persistence_error(self) -> Exception | None:
        return next((item.error for item in self._gap_writes if item.error is not None), None)

    def retry_transport_gaps(self) -> None:
        """Retry each failed notice once on its original active primary epoch."""

        if self._closed:
            return
        if self._drain_transport_gaps():
            self._notify()
        epoch = self._primary_registration_epoch()
        for item in self._gap_writes:
            if (item.error is not None and item.future is None
                    and epoch is not None and item.registration_epoch == epoch):
                item.future = self._managed_sources().record_transport_gap(item.notice)

    def _drain_transport_gaps(self) -> bool:
        changed = False
        epoch = None if self._closed else self._primary_registration_epoch()
        for item in tuple(self._gap_writes):
            future = item.future
            if future is None or not future.done():
                continue
            item.future = None
            try:
                result = future.result()
                if not isinstance(result, GapWriteResult):
                    raise ValueError("gap writer returned an invalid result")
                GapWriteResult.__post_init__(result)
                if (result.registration_epoch != item.registration_epoch
                        or (result.boot_id, result.from_mono_ns, result.to_mono_ns)
                        != (item.notice.boot_id, item.notice.from_mono_ns, item.notice.to_mono_ns)):
                    raise ValueError("gap writer answered another notice or registration")
            except Exception as error:
                item.error = error
                changed = True
                continue
            item.error = None
            self._gap_writes.remove(item)
            if result.registration_epoch != epoch:
                continue
            if result.disposition in ("inserted", "duplicate"):
                preparation = self._preparation
                fenced = preparation is not None and item in preparation.fence
                self.invalidate(_before_preparation=fenced)
                if fenced:
                    self.view_state = "loading"
                changed = True
        return changed

    def _close_views(self):
        for _, service in self._views:
            service.connection.close()
        self._views = []

    def close(self):
        self.invalidate()
        self._applied_ranking_provenance = {}
        self._close_views()
        self._queue = []
        self._closed = True
        # Publication retirement: no queued or late worker outcome may reach a
        # widget or a caller after close. Capacity is a resource, not an outward
        # publication, so it is never erased here: a running worker keeps its
        # charge until it terminates and its owner path reaps it. Cancelled
        # queued jobs are terminal and are reaped through that same path below.
        while True:
            try:
                self._completed.get_nowait()
            except queue.Empty:
                break
        self._frame_executor.shutdown(wait=False, cancel_futures=True)
        self._background_executor.shutdown(wait=False, cancel_futures=True)
        self._reap_terminal_frame_workers()
        self._reap_terminal_background_workers()
        # Transfer cleanup of running work to terminal callbacks. These callbacks
        # release only registry resources and never publish or mutate router maps.
        sources = self._capture_provider
        for _token, future in self._capture_jobs.values():
            future.add_done_callback(
                lambda completed, sources=sources: self._discard_capture(sources, completed))
        self._capture_jobs.clear()
        for worker_id, capture_id in tuple(self._worker_captures.items()):
            if worker_id in self._frame_jobs:
                future = self._frame_jobs[worker_id]
            else:
                future = self._drained_futures.get(worker_id)
                if future is None:
                    entry = self._worker_jobs.get(worker_id)
                    if entry is None or entry[1] is None:
                        raise RuntimeError("a captured worker has no terminal Future")
                    future = entry[1]
            future.add_done_callback(
                lambda completed, sources=sources, capture_id=capture_id:
                    sources.release_capture(capture_id))
            self._worker_capture_sets.pop(worker_id, None)
            self._worker_captures.pop(worker_id)
        if self._boot_capture is not None:
            self._boot_capture.add_done_callback(
                lambda completed: self._discard_capture(sources, completed))
            self._boot_capture = None
        self.sources.close()

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
    def _snapshot_planner(sources, window):
        """Initialize a private planner over worker-owned snapshot connections."""

        worker = object.__new__(HistoryRouter)
        worker._sources = {name: (service, owner) for name, service, owner in sources}
        worker.registry = AppRegistry()
        worker._window = window
        worker._views = []
        worker._ranking = []
        worker._all_segments = ()
        worker.segments = ()
        worker.boundaries = ()
        worker._gap_ranges = []
        worker._known_gap_ranges = []
        worker._overlap_ranges = []
        worker._has_local_history = False
        worker._not_retained = False
        worker._chronology_unproved = False
        return worker

    @staticmethod
    def _read_local(capture: CapturedSourceSet, window, *, worker_id: int,
                    request: QueryRequest) -> LocalFrameOutcome:
        """Plan and rank a verified immutable capture on the worker thread."""

        if not isinstance(request, QueryRequest) or request.kind != "ranking" or request.window != window:
            raise ValueError("a local frame request must match its exact window")
        request = _normalize_request(request)
        with _captured_sources(capture) as (sources, snapshot):
            worker = HistoryRouter._snapshot_planner(sources, window)
            try:
                worker._plan(window)
                worker._materialize()
                worker.boundaries = tuple(boundary for boundary in worker.boundaries if all(
                    any(s.boot_id == boundary[0] and s.proven and s.owner == owner and
                        (s.end_ns == boundary[1] if index == 2 else s.start_ns == boundary[1])
                        for s in worker.segments)
                    for index, owner in enumerate(boundary[2:], 2)))
                worker._build_ranking(window)
                file_available = False
                for _, service, _ in sources:
                    try:
                        if service.connection.execute(
                                "SELECT 1 FROM boot WHERE file_finalized_through_mono_ns > 0 LIMIT 1"
                        ).fetchone() is not None:
                            file_available = True
                    except Exception:
                        continue
                return LocalFrameOutcome(
                    worker_id=worker_id,
                    request=request,
                    ranking=tuple(worker._ranking),
                    segments=tuple(worker.segments),
                    boundaries=tuple(worker.boundaries),
                    gaps=tuple(worker._gap_ranges),
                    known_gaps=tuple(worker._known_gap_ranges),
                    overlaps=tuple(worker._overlap_ranges),
                    has_local_history=bool(worker._has_local_history),
                    not_retained=bool(worker._not_retained),
                    chronology_unproved=bool(worker._chronology_unproved),
                    file_available=bool(file_available),
                    snapshot=snapshot,
                )
            finally:
                worker._close_views()

    @staticmethod
    def _read_local_part(capture: CapturedSourceSet, request: QueryRequest, *,
                         worker_id: int, app_key: str) -> LocalPartOutcome:
        """Compute exactly one requested kind for one external subject."""

        normalized = _normalize_request(request)
        if normalized.kind not in ("totals", "series"):
            raise ValueError("a local part request is totals or series")
        if normalized.app_key != app_key:
            raise ValueError("a local part request must name its external subject")
        with _captured_sources(capture) as (sources, snapshot):
            worker = HistoryRouter._snapshot_planner(sources, normalized.window)
            try:
                worker._plan(normalized.window)
                worker._materialize()
                parts = []
                for _name, service in worker._views:
                    row = service.connection.execute(
                        "SELECT app_id FROM app WHERE key=?", (app_key,)).fetchone()
                    if row is None:
                        continue
                    if normalized.kind == "totals":
                        parts.append(service.disk_share(row[0], normalized.window))
                    else:
                        parts.append(service.series(row[0], normalized.window))
                if not parts:
                    raise KeyError(app_key)
                coverage = worker._part_coverage(parts)
                if normalized.kind == "totals":
                    value = HistoryRouter._merge_disk_shares(
                        parts, parts[0].app_id, normalized.window, coverage)
                else:
                    value = HistoryRouter._merge_series(
                        parts, parts[0].app_id, normalized.window, coverage)
                digest = local_answer_digest(normalized.kind, app_key, normalized.window, value)
                return LocalPartOutcome(worker_id, normalized, app_key, value, digest, snapshot)
            finally:
                worker._close_views()

    # -- authority and source identity (§3.2) ------------------------------ #

    def _local_authority(self) -> tuple:
        """The current local authority: every registered identity, name-ordered."""

        return ("local", self._capture_provider.registration_vector())

    def _remote_authority(self) -> tuple | None:
        """The verified remote connection identity, or None for local mode.

        The cursor sequence is deliberately absent: advancing it is an ordinary
        source change, never a connection-authority change.
        """

        state = self.client.state
        if (state is None or not state.verified or not state.answering
                or state.cursor is None):
            return None
        return ("remote", getattr(self.client, "generation", 0),
                state.cursor.boot_id, state.cursor.iod_epoch)

    def _current_authority(self) -> tuple:
        remote = self._remote_authority()
        return remote if remote is not None else self._local_authority()

    def _remote_mode(self) -> bool:
        return self._remote_authority() is not None

    def _reconcile_authority(self) -> bool:
        """Retire obsolete owners before collecting or dispatching (§3.2)."""

        current = self._current_authority()
        registrations = self.sources.registration_vector()
        registrations_changed = registrations != self._registrations_seen
        self._registrations_seen = registrations
        if self._authority is None:
            self._authority = current
            return False
        if current == self._authority and not registrations_changed:
            return False
        self._retire_authority(current)
        return True

    def _retire_authority(self, current: tuple) -> bool:
        """One authority transition: clear detail and retire its admitted frame."""

        self._authority = current
        self._applied_ranking_provenance = {}
        changed = self._retire_detail_lifetime()
        frame = self._frame
        if frame is not None and not frame.retired:
            frame.error = None
            self._settle(frame.local_token, retiring=True)
            if frame.remote_token is not None:
                self._settle(frame.remote_token, retiring=True)
            changed = True
        # Files deliveries are owner-level too: they retire through their
        # administrative settlement arm before any obsolete answer is read.
        if self._cancel_file_requests():
            changed = True
        self._ranking_stamps.clear()
        return changed

    # -- scope-owned detail lifecycle (§§2.4, 3.1-3.2) --------------------- #

    @staticmethod
    def _scope_family(scope: RetentionScope) -> tuple:
        if len(scope) == 5:
            return (scope[0], "range", scope[2], scope[3], scope[4])
        if len(scope) == 3:
            return (scope[0], "since_boot", scope[2])
        return (scope[0], scope[1])

    @staticmethod
    def _window_family(app_key: str, window: Window) -> tuple:
        if window.kind == "range":
            return (app_key, "range", window.boot_id,
                    window.start_mono_ns, window.end_mono_ns)
        if window.kind == "since_boot":
            return (app_key, "since_boot", window.boot_id)
        return (app_key, window.kind)

    @staticmethod
    def _lifetime_tokens(lifetime: _DetailLifetime) -> tuple[RequestToken, ...]:
        """Every token a lifetime owns, pending, settled or superseded, once."""

        owned: dict[int, RequestToken] = {}
        for token in (*lifetime.live.values(), *lifetime.successors.values()):
            owned[token.token_id] = token
        for slot in lifetime.slots.values():
            if slot.token is not None:
                owned[slot.token.token_id] = slot.token
        return tuple(owned.values())

    def _retire_detail_lifetime(self) -> bool:
        """Retire the selected lifetime through the sole settlement writer.

        The lifetime is an owner, so one of its admitted tokens carries the
        administrative retirement into ``_settle``; the arm clears every
        pending/successor owner, accepted record and stamp lookup of that
        lifetime and resets the selection pointer. Publication retirement never
        releases a running worker or an issued transport slot; their eventual
        completions are drained and ignored. A lifetime that never admitted a
        token owns no record, stamp or live owner, so only its empty selection
        pointer is reset here.
        """

        lifetime = self._detail
        if lifetime is None:
            self._applied_window = None
            return False
        for token in self._lifetime_tokens(lifetime):
            if token.token_id not in self._retired_tokens:
                return self._settle(token, retiring=True)
        self._detail = None
        self._applied_window = None
        self._ranking_stamps.clear()
        return False

    def select_detail(self, app_key: str | None, window: Window | None) -> None:
        """Adopt the view's selected applied subject and scope, or retire it.

        Same subject and scope with a nondecreasing endpoint preserves tokens,
        records and stamps; a changed subject, tag, boot, range start or a
        backward endpoint retires them synchronously. This sends no query.
        """

        if app_key is None and window is None:
            self._retire_detail_lifetime()
            self._ranking_stamps.clear()
            return
        if app_key is None or window is None:
            raise ValueError("select_detail needs both the subject and the applied window")
        key = _canonical_app_key(app_key)
        if not isinstance(window, Window):
            raise ValueError("select_detail requires an applied Window")
        if window.kind == "launch":
            raise WindowUnavailableError("launch_attribution", LAUNCH_WINDOW_MESSAGE)
        validate_window(window)
        family = self._window_family(key, window)
        endpoint = applied_endpoint(window)
        lifetime = self._detail
        ranking_provenance = self._applied_ranking_provenance.get(key)
        if ranking_provenance is None or ranking_provenance.measured_window != window:
            ranking_provenance = self.ranking_provenance(key, window)
        if (lifetime is not None and lifetime.app_key == key
                and lifetime.family == family):
            if ranking_provenance is not None:
                lifetime.ranking_provenance = ranking_provenance
            if endpoint is None:
                self._applied_window = window
                return
            if lifetime.endpoint is not None and endpoint < lifetime.endpoint:
                self._retire_detail_lifetime()
            else:
                lifetime.endpoint = endpoint
                self._applied_window = window
                return
        self._retire_detail_lifetime()
        self._detail = _DetailLifetime(
            key, family, self._next_lifetime_id, endpoint,
            ranking_provenance=ranking_provenance)
        self._next_lifetime_id += 1
        self._applied_window = window

    def _detail_token_live(self, token: RequestToken) -> bool:
        lifetime = self._detail
        if lifetime is None or lifetime.lifetime_id != token.owner_id:
            return False
        if token.scope is None or lifetime.app_key != token.scope[0]:
            return False
        kind = token.request.kind
        live = lifetime.live.get(kind)
        if live is not None and live.token_id == token.token_id:
            return True
        successor = lifetime.successors.get(kind)
        return successor is not None and successor.token_id == token.token_id

    def _admit_detail(self, lifetime: _DetailLifetime, kind: PartKind, key: str,
                      window: Window, scope: RetentionScope) -> RequestToken:
        token_id = self._allocate_token_id()
        origin: Literal["remote", "local"] = "remote" if self._remote_mode() else "local"
        authority = self._current_authority()
        requested_source = self.source_position() if origin == "remote" else self._local_vector()
        slot_id = self._allocate_token_id() if origin == "remote" else None
        ranking_provenance = lifetime.ranking_provenance
        local_ownership = (
            ranking_provenance is not None
            and ranking_provenance.measured_window == window
            and ranking_provenance.local_bracket is not None)
        token = RequestToken(
            token_id, QueryRequest(kind, window, key), origin, scope, "detail",
            lifetime.lifetime_id, slot_id, authority, requested_source,
        )
        if origin == "remote" and local_ownership:
            lifetime.local_tokens.add(token_id)
        slot = lifetime.slots.get((kind, scope))
        if slot is None:
            slot = _PartSlot(kind, scope)
            lifetime.slots[(kind, scope)] = slot
        slot.phase = "pending"
        slot.token = token
        slot.reason = None
        slot.error = None
        lifetime.live[kind] = token
        if origin == "remote":
            self._queue.append(_QueueEntry(token, _PRIORITY_DETAIL, self._next_serial()))
            self._dispatch()
        else:
            self._start_local_detail(token)
        return token

    def request_detail(self, kind: PartKind, app_id: int, window: Window) -> PartState:
        """Admit at most one live token for one selected kind/scope.

        An older in-flight exact request survives and is returned as pending;
        only one latest eligible desired applied Window is remembered as its
        successor. This never replaces a live owner on every forward frame.
        """

        _private_enum(kind, _DETAIL_KINDS, "detail.kind")
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise ValueError("request_detail requires a positive presentation subject id")
        key = self.registry.key_for(app_id)
        if not isinstance(window, Window):
            raise ValueError("request_detail requires an exact Window")
        self._precheck(window)
        validate_window(window)
        lifetime = self._detail
        if lifetime is None or lifetime.app_key != key:
            raise ValueError("request_detail requires the selected applied subject")
        if self._window_family(key, window) != lifetime.family:
            raise ValueError("request_detail window is outside the selected scope")
        endpoint = applied_endpoint(window)
        if endpoint is not None:
            if lifetime.endpoint is None or endpoint > lifetime.endpoint:
                raise ValueError("request_detail window is beyond the applied frame")
        scope = retention_scope(key, window)
        live = lifetime.live.get(kind)
        if live is not None:
            if live.request.window == window:
                return self.part_state(kind, key, scope)
            live_endpoint = applied_endpoint(live.request.window)
            if endpoint is not None and live_endpoint is not None and endpoint >= live_endpoint:
                successor = lifetime.successors.get(kind)
                successor_endpoint = (None if successor is None
                                      else applied_endpoint(successor.request.window))
                if successor is None or successor_endpoint is None or endpoint >= successor_endpoint:
                    if successor is not None:
                        self._retired_tokens.add(successor.token_id)
                        self._unscheduled.pop(successor.token_id, None)
                        lifetime.local_tokens.discard(successor.token_id)
                    token = self._admit_detail(lifetime, kind, key, window, scope)
                    lifetime.successors[kind] = token
                    lifetime.live[kind] = live  # the older live owner keeps the wire
                    return self.part_state(kind, key, scope)
            return self.part_state(kind, key, scope)
        self._admit_detail(lifetime, kind, key, window, scope)
        return self.part_state(kind, key, scope)

    def _part_state_from_slot(self, key: str, kind: PartKind,
                              scope: RetentionScope) -> PartState:
        lifetime = self._detail
        slot = None if lifetime is None else lifetime.slots.get((kind, scope))
        if slot is None or slot.token is None:
            return PartState(kind, key, scope, "absent", None, None, None, None)
        token = slot.token
        record = slot.record if slot.phase == "ok" else None
        reason = slot.reason if slot.phase == "failed" else None
        return PartState(kind, key, scope, slot.phase, token.token_id,
                         token.request.window, record, reason)

    def part_state(self, kind: PartKind, app_key: str,
                   scope: RetentionScope) -> PartState:
        """A pure observation of one exact slot, or a valid absent state."""

        _private_enum(kind, _DETAIL_KINDS, "part_state.kind")
        key = _canonical_app_key(app_key)
        scope = validate_scope(scope)
        if scope[0] != key:
            raise ValueError("part_state scope must name its subject")
        lifetime = self._detail
        if (lifetime is None or lifetime.app_key != key
                or self._scope_family(scope) != lifetime.family):
            return PartState(kind, key, scope, "absent", None, None, None, None)
        return self._part_state_from_slot(key, kind, scope)

    def retained(self, kind: PartKind, app_key: str, scope: RetentionScope,
                 applied_endpoint_at: int | None) -> RetainedRecord | None:
        """The newest eligible accepted record; pure, never a send or a stamp."""

        _private_enum(kind, _DETAIL_KINDS, "retained.kind")
        key = _canonical_app_key(app_key)
        scope = validate_scope(scope)
        if scope[0] != key:
            raise ValueError("retained scope must name its subject")
        tag = scope[1]
        if tag in ("lifetime", "launch"):
            if applied_endpoint_at is not None:
                raise ValueError("a lifetime or launch scope takes no applied endpoint")
        else:
            _private_int(applied_endpoint_at, "retained.applied_endpoint")
        lifetime = self._detail
        if lifetime is None or lifetime.app_key != key:
            return None
        if self._scope_family(scope) != lifetime.family:
            # A different live scope (tag, boot, range start) is not this
            # caller's slot; a retired or foreign slot supplies no record.
            return None
        exact = lifetime.slots.get((kind, scope))
        if exact is not None and exact.phase == "failed":
            return None
        best = None
        for (slot_kind, _slot_scope), slot in lifetime.slots.items():
            if slot_kind != kind or slot.record is None or slot.phase == "failed":
                continue
            measured = slot.record.provenance.measured_window
            if tag in ("range", "since_boot"):
                if measured.end_mono_ns is None or measured.end_mono_ns > applied_endpoint_at:
                    continue
            if best is None:
                best = slot.record
                continue
            best_end = best.provenance.measured_window.end_mono_ns
            candidate_end = measured.end_mono_ns
            if best_end is None:
                best = slot.record
            elif candidate_end is not None and candidate_end >= best_end:
                best = slot.record
        return best

    def _exact_part(self, app_id: int, window: Window, kind: PartKind):
        """The exact-window detail getter: its own Window or HistoryPending."""

        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise ValueError("a detail getter requires a positive presentation id")
        key = self.registry.key_for(app_id)
        if not isinstance(window, Window):
            raise ValueError("a detail getter requires an exact Window")
        lifetime = self._detail
        if (lifetime is None or lifetime.app_key != key
                or self._window_family(key, window) != lifetime.family):
            # A legacy exact caller names its subject and applied Window itself.
            # Adopting that scope here is the same synchronous reset the view's
            # select_detail performs; it is never a cross-subject detail cache.
            self.select_detail(key, window)
            lifetime = self._detail
        if lifetime is None:
            raise HistoryPending("detail lifetime is still loading")
        scope = retention_scope(key, window)
        slot = lifetime.slots.get((kind, scope))
        if (slot is not None and slot.phase == "failed" and slot.token is not None
                and slot.token.request.window == window):
            if slot.error is not None:
                raise slot.error
        if (slot is not None and slot.phase == "ok" and slot.record is not None
                and slot.record.provenance.measured_window == window):
            return slot.record.value
        endpoint = applied_endpoint(window)
        if endpoint is not None and (lifetime.endpoint is None or endpoint > lifetime.endpoint):
            raise HistoryPending(
                f"this application's {kind} is not measured at this applied endpoint")
        self.request_detail(kind, app_id, window)
        raise HistoryPending(
            f"this application's {kind} is still loading from the collector")

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        return self._exact_part(app_id, window, "totals")

    def series(self, app_id: int, window: Window) -> Series:
        return self._exact_part(app_id, window, "series")

    # -- frame preparation, readiness and lazy local detail (§3.3) --------- #

    def _next_serial(self) -> int:
        self._admission_serial += 1
        return self._admission_serial

    def prepare(self, window: Window) -> None:
        # A22 precedes any source observation, exactly as before.
        self._file_status = FileAvailability(False, False, "file_source_unavailable")
        self._precheck(window)
        if self._closed:
            raise RuntimeError("history router is closed")
        self._managed_sources()
        if self.local_read_in_flight or self._preparation is not None:
            raise HistoryPending("local frame is still running")
        if self._drain_transport_gaps():
            self._notify()
        self._reap_terminal_frame_workers()
        # Reconcile before admitting a new owner so this frame belongs to the
        # authority observed now rather than retiring itself on its first drain.
        self._reconcile_authority()
        remote_mode = self._remote_mode()
        requested_source = self.source_position() if remote_mode else None
        previous = self._frame
        if previous is not None:
            self._retired_tokens.add(previous.local_token.token_id)
            if previous.remote_token is not None:
                self._retired_tokens.add(previous.remote_token.token_id)
            self._queue = [entry for entry in self._queue
                           if not (entry.token.owner == "frame"
                                   and entry.token.owner_id == previous.owner_id)]
        self._frame = None
        self._collect_captures()
        if self._local_future is not None:
            self._local_future.cancel()
        self._local_future = None
        if self._local_worker_id is not None:
            self._worker_bindings.pop(self._local_worker_id, None)
            self._local_worker_id = None
        self._generation += 1
        self._cancel_file_requests()
        self._window, self._ready = window, False
        self.segments, self.boundaries = (), ()
        self._gap_ranges, self._known_gap_ranges, self._overlap_ranges = [], [], []
        self._has_local_history = False
        self._ranking = []
        self._remote = None
        self._remote_answered = None
        self._remote_snapshot = None
        self._not_retained = self._chronology_unproved = False
        self._daemon_answer_error = None
        self._frame_error = None
        if self._detail is None:
            self._ranking_stamps.clear()
        self.view_state = "loading"
        epoch = self._primary_registration_epoch()
        fence = tuple(item for item in self._gap_writes
                      if epoch is not None and item.registration_epoch == epoch)
        preparation = _Preparation(self._generation, window, self._current_authority(),
                                   requested_source, fence)
        self._preparation = preparation
        if fence:
            self._advance_preparation()
        else:
            self._start_preparation(preparation)

    def _advance_preparation(self) -> bool:
        preparation = self._preparation
        if preparation is None:
            return False
        if any(item.future is not None for item in preparation.fence):
            return False
        error = next((item.error for item in preparation.fence if item.error is not None), None)
        try:
            if error is not None:
                raise error
            if (preparation.owner_id != self._generation
                    or preparation.authority != self._current_authority()):
                raise SourceAdmissionError("managed source registrations changed")
            self._start_preparation(preparation)
        except Exception as caught:
            self._preparation = None
            self._frame_error = caught
            self.view_state = "unavailable: local history evidence"
        return True

    def _start_preparation(self, preparation: _Preparation) -> None:
        """Activate the already allocated frame after all earlier gap drains."""

        window = preparation.window
        requested_source = preparation.requested_source
        self._preparation = None
        worker_id = self._allocate_token_id()
        request = QueryRequest("ranking", window)
        binding = self._capture_local_binding(
            worker_id=worker_id, token_id=worker_id, request=request)
        self._worker_bindings[worker_id] = binding
        self._local_generation = self._generation
        self._local_worker_id = worker_id
        self._local_paths = None
        authority = preparation.authority
        local_token = RequestToken(
            worker_id, request, "local", None, "frame", self._generation, None,
            authority, binding.opening)
        remote_token = None
        if isinstance(requested_source, RemotePosition):
            remote_token = RequestToken(
                self._allocate_token_id(), request, "remote", None, "frame",
                self._generation, self._allocate_token_id(), authority,
                requested_source)
            self._queue.append(_QueueEntry(
                remote_token, _PRIORITY_FRAME_RANKING, self._next_serial()))
        self._frame = _FrameOwner(
            self._generation, window, requested_source, authority, local_token,
            remote_token, binding)
        try:
            self._local_future = self._request_local_capture(local_token)
        except Exception:
            self._worker_bindings.pop(worker_id, None)
            self._local_worker_id = None
            self._frame = None
            raise
        self._dispatch()

    def preparation_source(self, window: Window) -> SourcePosition | None:
        preparation = self._preparation
        if preparation is not None and preparation.window == window:
            return preparation.requested_source
        frame = self._frame
        if frame is None or frame.window != window or frame.retired:
            return None
        return frame.requested_source

    def frame_provenance(self, window: Window) -> FrameProvenance | None:
        frame = self._frame
        if frame is None or frame.window != window:
            return None
        return frame.certificate

    def ranking_provenance(self, app_key: str, window: Window) -> AnswerProvenance | None:
        key = _canonical_app_key(app_key)
        applied = self._applied_ranking_provenance.get(key)
        lifetime = self._detail
        if (applied is not None and lifetime is not None
                and lifetime.app_key == key
                and self._applied_window == window
                and applied.measured_window == window):
            return applied
        frame = self._frame
        if frame is not None and frame.window == window and frame.certificate is not None:
            return frame.ranking_provenance.get(key)
        return None

    # -- the sole owner-side settlement writer (§3.4) ---------------------- #

    def _notify(self) -> None:
        """Owner-thread notification hook; view wiring arrives in Tasks 9/12."""

    def _settle(self, token: RequestToken, outcome: SettlementInput | None = None,
                *, retiring: bool = False) -> bool:
        """Settle one admitted request token exactly once.

        Guard order: closed/settled/retired, current token, live owner,
        authority and scope; then the bound input association; then selector
        and owner predicates. No ignored completion mutates another owner.
        """

        if not isinstance(token, RequestToken):
            raise ValueError("a settlement requires its admitted request token")
        if self._closed:
            return False
        if retiring:
            if outcome is not None:
                raise ValueError("an administrative retirement carries no outcome")
            if token.owner == "frame":
                # A frame owner retires once even after its local copy settled:
                # the owner, not the settled token, is what authority retires.
                if token.token_id in self._retired_tokens:
                    return False
                return self._retire_frame_owner(token)
            if token.owner == "detail":
                # A detail owner is its lifetime, so the retirement arm is
                # owner-level: one call retires the lifetime exactly once,
                # whether this representative token was settled or pending.
                return self._retire_detail_token(token)
            if token.owner == "files":
                return self._retire_files_owner(token)
            return False
        if token.token_id in self._settled_tokens or token.token_id in self._retired_tokens:
            return False
        if token.owner == "frame":
            frame = self._frame
            if frame is None or frame.owner_id != token.owner_id or frame.retired:
                return False
            if token.authority != frame.authority:
                return False
            if token.request.window != self._window:
                return False
        elif token.owner == "detail":
            if not self._detail_token_live(token):
                return False
            if token.authority != self._current_authority():
                return False
        else:
            owner = self._file_owners.get(token.owner_id)
            if (owner is None or owner.retired
                    or owner.generation != self._generation
                    or token.authority != owner.authority
                    or owner.current is None
                    or owner.current.token_id != token.token_id):
                return False
        if outcome is None:
            return self._reject_token(
                token, None, RequestRejected("invalid_result", "invalid_result"))
        if isinstance(outcome, RemoteSettlement):
            if token.origin != "remote":
                return self._reject_token(
                    token, outcome, RequestRejected("invalid_result", "invalid_result"))
            return self._settle_remote(token, outcome)
        if isinstance(outcome, LocalSettlement):
            if token.origin != "local":
                return self._reject_token(
                    token, outcome, RequestRejected("invalid_result", "invalid_result"))
            return self._settle_local(token, outcome)
        return self._reject_token(
            token, None, RequestRejected("invalid_result", "invalid_result"))

    def _retire_frame_owner(self, token: RequestToken) -> bool:
        frame = self._frame
        if frame is None or frame.owner_id != token.owner_id or frame.retired:
            return False
        frame.retired = True
        for side in (frame.local_token, frame.remote_token):
            if side is not None:
                self._retired_tokens.add(side.token_id)
        self._queue = [entry for entry in self._queue
                       if not (entry.token.owner == "frame"
                               and entry.token.owner_id == frame.owner_id)]
        frame.remote_candidate = None
        frame.certificate = None
        frame.ranking_provenance = {}
        self._remote = None
        self._remote_answered = None
        self._remote_snapshot = None
        self._ready = False
        self._local_generation = -1
        # Advance the generation once for this frame owner, exactly as the
        # existing invalidation seam does. Its running Future and sent occupant
        # stay real resources; they can never publish.
        self._generation += 1
        self.view_state = "unavailable: authority changed"
        return True

    def _retire_files_owner(self, token: RequestToken) -> bool:
        """Retire one outward Files delivery; publication only, once.

        The matching outward Future receives the existing generation-retired
        refusal. A running worker keeps its charge and an issued transport
        occupant keeps its slot; both are drained later and can publish nothing.
        """

        owner = self._file_owners.get(token.owner_id)
        if owner is None or owner.retired:
            return False
        owner.retired = True
        if owner.current is not None:
            self._retired_tokens.add(owner.current.token_id)
        self._file_owners.pop(owner.owner_id, None)
        self._settle_file(owner.outward, owner.generation, error=WindowUnavailableError(
            "file_source_unavailable", "File history generation retired."))
        return True

    def _retire_detail_token(self, token: RequestToken) -> bool:
        """Retire the selected detail lifetime that admitted ``token``, once.

        Administrative detail retirement belongs to the lifetime, not to each
        local/remote token: one call retires every pending and successor owner
        of that lifetime, clears every accepted record and stamp lookup of it
        (including already-settled slot records) and resets the selected
        lifetime/applied-window bookkeeping. A running worker or sent transport
        occupant stays a real resource; its completion is drained and ignored.
        """

        lifetime = self._detail
        if lifetime is None or lifetime.lifetime_id != token.owner_id:
            return False
        owned = self._lifetime_tokens(lifetime)
        if not any(item.token_id == token.token_id for item in owned):
            return False
        for item in owned:
            self._retired_tokens.add(item.token_id)
            self._unscheduled.pop(item.token_id, None)
        for slot in lifetime.slots.values():
            slot.record = None
            slot.stamps.clear()
            slot.phase = "absent"
            slot.token = None
            slot.reason = None
            slot.error = None
        lifetime.live.clear()
        lifetime.successors.clear()
        self._detail = None
        self._applied_window = None
        self._ranking_stamps.clear()
        return True

    def _reject_token(self, token: RequestToken, outcome: SettlementInput | None,
                      error: Exception) -> bool:
        if isinstance(outcome, RemoteSettlement):
            try:
                self.client.reject_query_result(outcome.future)
            except Exception:
                pass
        return self._settle_failure(token, error)

    def _settle_failure(self, token: RequestToken, error: Exception) -> bool:
        self._settled_tokens.add(token.token_id)
        if token.origin == "local":
            # A failed copy is terminal: it keeps its diagnostics and its binding
            # is retired; only a closing-observer failure stays retryable.
            self._worker_bindings.pop(token.token_id, None)
            self._files_bindings.pop(token.token_id, None)
        if token.owner == "detail":
            return self._current_detail_failure(token, error)
        if token.owner == "files":
            # A Files failure owns only its outward view: it can never alter the
            # frame's not_retained flag, ranking diagnostic or applicability.
            return self._files_failure(token, error)
        frame = self._frame
        if frame is None:
            return False
        if token.origin == "local":
            # The existing availability seam owns a local copy failure; it is
            # never a successful empty measurement or a remote diagnostic.
            frame.error = error
            self._local_generation = None
            self._ready = False
            self._file_status = FileAvailability(
                False, False, "file_source_unavailable")
            self.view_state = "unavailable: local history evidence"
            self._ranking = []
            return True
        if token.request.kind == "ranking":
            frame.remote_settled = True
            frame.remote_candidate = None
            self._fail_remote(error)
            return True
        return False

    def _current_detail_failure(self, token: RequestToken, error: Exception) -> bool:
        lifetime = self._detail
        if lifetime is None or lifetime.lifetime_id != token.owner_id:
            return False
        if token.scope is None:
            return False
        kind = cast(PartKind, token.request.kind)
        scope = token.scope
        slot = lifetime.slots.get((kind, scope))
        if slot is None:
            slot = _PartSlot(kind, scope)
            lifetime.slots[(kind, scope)] = slot
        # A current failure clears the record before any notification; a pending
        # retry cannot restore it, and the original exception is retained.
        slot.record = None
        slot.phase = "failed"
        slot.token = token
        slot.reason = str(error) if str(error) else type(error).__name__
        slot.error = error
        live = lifetime.live.get(kind)
        if live is not None and live.token_id == token.token_id:
            del lifetime.live[kind]
        lifetime.local_tokens.discard(token.token_id)
        successor = lifetime.successors.pop(kind, None)
        if successor is not None:
            self._retired_tokens.add(successor.token_id)
            self._unscheduled.pop(successor.token_id, None)
            lifetime.local_tokens.discard(successor.token_id)
        return True

    def _settle_remote(self, token: RequestToken, settlement: RemoteSettlement) -> bool:
        future = settlement.future
        bound_future = self._remote_futures.get(token.token_id)
        if bound_future is not future:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        try:
            state = self.client.query_transport(future)
        except Exception:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        if state.request != token.request:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        if state.connection_generation != token.authority[1]:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        cache_hit = state.phase == "terminal" and state.request_id is None
        issue_source = self._remote_issue_sources.get(token.token_id)
        if cache_hit:
            issue_source = state.answered_source
        elif issue_source != state.issued_source:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        if state.phase not in ("terminal", "expired", "lost"):
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        if not future.done():
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        try:
            actual = future.result()
        except Exception as error:  # noqa: BLE001 - the bound terminal exception
            # A cancelled Future re-raises a fresh CancelledError on every result
            # read, so cancellation is the one bound outcome compared by class.
            cancelled = future.cancelled() and isinstance(settlement.error, CancelledError)
            if settlement.error is not error and not cancelled:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            if settlement.error is None:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            return self._settle_failure(token, settlement.error)
        if settlement.response is None or settlement.response != actual:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        try:
            validate_query_result(token.request, actual)
        except Exception:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        if token.owner == "detail":
            kind = cast(PartKind, token.request.kind)
            value = actual["result"]
            if kind == "totals" and not isinstance(value, DiskShare):
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            if kind == "series" and not isinstance(value, Series):
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            if value.window != token.request.window:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            answered = state.answered_source
            if answered is None or answered != issue_source:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            lifetime = self._detail
            if lifetime is None or token.scope is None:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            if token.token_id in lifetime.local_tokens:
                # The admitted ranking's source decision stays with this exact
                # detail across preparation and later applied-frame adoption.
                # Recompute through the same managed-source reducers.
                lifetime.local_tokens.discard(token.token_id)
                self.client.reject_query_result(future)
                self._settled_tokens.add(token.token_id)
                local_token = replace(
                    token, token_id=self._allocate_token_id(), origin="local",
                    slot_id=None, requested_source=self._local_vector())
                if lifetime.live.get(kind) == token:
                    lifetime.live[kind] = local_token
                else:
                    lifetime.successors[kind] = local_token
                slot = lifetime.slots[(kind, token.scope)]
                if slot.token == token:
                    slot.token = local_token
                self._start_local_detail(local_token)
                return True
            digest = actual.get("snapshot")
            if not isinstance(digest, str):
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            presentation = self.registry.id_for(token.request.app_key)
            return self._publish_detail(
                token, replace(value, app_id=presentation), digest,
                answered_source=answered, bracket=None)
        if token.owner == "files":
            return self._accept_files_remote(token, actual, settlement.future)
        if token.request.kind != "ranking":
            # An already-supported other selector is validated and discarded;
            # it publishes no owner record and does not affect the frame.
            self._settled_tokens.add(token.token_id)
            return False
        frame = self._frame
        if frame is None:
            return False
        if not frame.local_settled:
            # The remote completion waits privately for this same frame's local
            # ownership evidence and cannot become a readiness success early.
            frame.remote_candidate = dict(actual)
            frame.remote_future = future
            frame.remote_answered = state.answered_source
            frame.remote_settled = True
            return False
        try:
            self._apply_remote_ranking(frame, actual, state.answered_source)
        except Exception as error:  # noqa: BLE001 - rejection becomes the frame seam
            frame.remote_settled = True
            frame.remote_candidate = None
            self._settled_tokens.add(token.token_id)
            self._fail_remote(error)
            try:
                self.client.reject_query_result(future)
            except Exception:
                pass
            return True
        frame.remote_settled = True
        frame.remote_applied = True
        self._settled_tokens.add(token.token_id)
        return True

    def _settle_local(self, token: RequestToken, settlement: LocalSettlement) -> bool:
        if settlement.worker_id != token.token_id:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        binding = (self._files_bindings.get(token.token_id) if token.owner == "files"
                   else self._worker_bindings.get(token.token_id))
        if (binding is None or binding.token_id != token.token_id
                or binding.request != token.request):
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        if settlement.error is not None:
            return self._settle_failure(token, settlement.error)
        outcome = settlement.outcome
        if outcome is None or settlement.capture is None:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        capture = settlement.capture
        if token.owner == "files":
            file_binding = cast(_FilesBinding, binding)
            binding_snapshot = file_binding.snapshot
        else:
            local_binding = cast(_LocalCopyBinding, binding)
            binding_snapshot = local_binding.snapshot
        try:
            if outcome.worker_id != binding.worker_id:
                raise ValueError("a local outcome answered another worker")
            snapshot = _validate_worker_snapshot(outcome.snapshot)
            capture_snapshot = _capture_provenance(capture)
            if (binding_snapshot is None
                    or capture_snapshot.capture_id != binding_snapshot.capture_id
                    or snapshot.capture_id != binding_snapshot.capture_id):
                raise ValueError("a local outcome answered another capture")
            if snapshot != binding_snapshot or capture_snapshot != snapshot:
                raise ValueError("a local outcome answered another snapshot")
            sources = getattr(self, "_capture_provider", None)
            if sources is not None:
                identities = tuple((item.name, item.owner, item.registration_epoch)
                                   for item in snapshot.bracket.opening.sources)
                if identities != sources.registration_vector():
                    raise ValueError("a local outcome answered retired registrations")
        except Exception:  # noqa: BLE001 - malformed or foreign worker result
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        if token.owner == "detail":
            if not isinstance(outcome, LocalPartOutcome):
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            try:
                # A worker result is not proof of its own validity: re-enter the
                # record's validation so a forged key/kind/Window/digest never
                # publishes a record or a stamp.
                LocalPartOutcome.__post_init__(outcome)
            except Exception:  # noqa: BLE001 - a malformed current input
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            if outcome.request != token.request or outcome.app_key != token.request.app_key:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            local_binding = cast(_LocalCopyBinding, binding)
            bracket = _capture_provenance(capture).bracket
            if bracket.opening != local_binding.opening:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            answered = bracket.opening if bracket.opening == bracket.closing else None
            presentation = self.registry.id_for(outcome.app_key)
            return self._publish_detail(
                token, replace(outcome.value, app_id=presentation), outcome.data_digest,
                answered_source=answered, bracket=bracket, snapshot=snapshot)
        if token.owner == "files":
            if not isinstance(outcome, LocalFilesOutcome):
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            return self._accept_files_local(token, outcome, settlement)
        if not isinstance(outcome, LocalFrameOutcome):
            return self._settle_failure(
                token, ValueError("a local frame worker must return a typed frame outcome"))
        frame = self._frame
        if frame is None:
            return False
        try:
            outcome = self._admitted_frame_outcome(
                outcome, cast(_LocalCopyBinding, binding))
        except Exception:  # noqa: BLE001 - malformed frame evidence
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        bracket = _capture_provenance(capture).bracket
        if bracket.opening != cast(_LocalCopyBinding, binding).opening:
            return self._reject_token(
                token, settlement, RequestRejected("invalid_result", "invalid_result"))
        metadata_future = self._frame_metadata.get(token.token_id)
        if metadata_future is not None:
            try:
                metadata = metadata_future.result(timeout=0)
                if metadata.snapshot != outcome.snapshot or metadata.choices.window != token.request.window:
                    raise ValueError("capture metadata answered another snapshot")
            except Exception:
                return self._reject_token(
                    token, settlement, RequestRejected("invalid_result", "invalid_result"))
            self.local = metadata.choices
            self._boot_id = metadata.boot_id
            self._boot_registrations = self.sources.registration_vector()
        self.segments, self.boundaries = outcome.segments, outcome.boundaries
        self._gap_ranges = list(outcome.gaps)
        self._known_gap_ranges = list(outcome.known_gaps)
        self._overlap_ranges = list(outcome.overlaps)
        self._has_local_history = outcome.has_local_history
        self._not_retained = self._not_retained or outcome.not_retained
        self._chronology_unproved = outcome.chronology_unproved
        self._file_status = FileAvailability(
            outcome.file_available, False,
            None if outcome.file_available else "file_source_unavailable",
        )
        self._ranking = [replace(row, app_id=self.registry.id_for(row.app_key))
                         for row in outcome.ranking]
        self._local_generation = None
        self._local_settlement = settlement
        self._worker_bindings.pop(token.token_id, None)
        self._settled_tokens.add(token.token_id)
        frame.local_settled = True
        frame.local_bracket = bracket
        if frame.remote_candidate is not None and not frame.remote_applied:
            try:
                self._apply_remote_ranking(
                    frame, frame.remote_candidate, frame.remote_answered)
            except Exception as error:  # noqa: BLE001 - rejection follows the seam
                self._fail_remote(error)
                if frame.remote_future is not None:
                    try:
                        self.client.reject_query_result(frame.remote_future)
                    except Exception:
                        pass
            finally:
                frame.remote_candidate = None
        return True

    def _publish_detail(self, token: RequestToken, value: DiskShare | Series,
                        digest: str, *, answered_source: SourcePosition | None,
                        bracket: LocalBracket | None,
                        snapshot: SnapshotProvenance | None = None) -> bool:
        lifetime = self._detail
        if lifetime is None or lifetime.lifetime_id != token.owner_id:
            return False
        kind = cast(PartKind, token.request.kind)
        app_key = token.request.app_key
        if app_key is None or token.scope is None:
            return False
        scope = token.scope
        window = token.request.window
        identity = AcceptedIdentity(kind, app_key, window, digest)
        slot = lifetime.slots.get((kind, scope))
        if slot is None:
            slot = _PartSlot(kind, scope)
            lifetime.slots[(kind, scope)] = slot
        if identity not in slot.stamps:
            slot.stamps[identity] = self._clock_ns()
        first_seen = slot.stamps[identity]
        provenance = AnswerProvenance(
            kind, app_key, scope, window, identity, first_seen, answered_source,
            bracket, snapshot)
        record = RetainedRecord(value, provenance)
        slot.record = record
        slot.phase = "ok"
        slot.token = token
        slot.reason = None
        slot.error = None
        live = lifetime.live.get(kind)
        if live is not None and live.token_id == token.token_id:
            del lifetime.live[kind]
        self._settled_tokens.add(token.token_id)
        if kind not in self._deferred_admissions:
            self._deferred_admissions.append(kind)
        return True

    def _apply_remote_ranking(self, frame: _FrameOwner, response: Mapping[str, object],
                              answered_source: SourcePosition | None) -> None:
        rows = response["result"]
        window = frame.window
        if (window.kind == "lifetime" or self._has_local_history or self._overlap_ranges
                or any(row.total_bytes or row.rchar or row.wchar for row in self._ranking)):
            raise ValueError("remote query cannot prove a mixed-store cutoff")
        snapshot = response.get("snapshot")
        if snapshot is None or not isinstance(snapshot, str):
            raise ValueError("remote history snapshot identity required")
        if response.get("next_cursor") is not None:
            raise ValueError("paginated remote ownership is not proved atomically")
        if not isinstance(rows, (list, tuple)):
            raise ValueError("remote ranking result must be a sequence")
        if not rows:
            raise ValueError("remote ownership evidence required")
        self._remote_snapshot = snapshot
        self._remote_answered = answered_source
        self._remote = [replace(row, app_id=self.registry.id_for(row.app_key))
                        for row in rows]
        self._not_retained = self._not_retained or any(
            row.coverage.unavailable_reason == "not_retained" for row in rows)
        frame.remote_applied = True

    def _fail_remote(self, error: Exception) -> None:
        """Drop remote evidence for this frame, keeping locally proved parts."""

        frame = self._frame
        self._remote = None
        self._remote_answered = None
        if frame is not None:
            self._queue = [entry for entry in self._queue
                           if not (entry.token.owner == "frame"
                                   and entry.token.owner_id == frame.owner_id)]
        if isinstance(error, (WindowUnavailableError, CancelledError)):
            return
        kind: Literal["remote_error", "unavailable", "rejected", "transport", "internal"]
        if isinstance(error, RemoteError):
            kind = "remote_error"
        elif isinstance(error, ClientUnavailable):
            kind = "unavailable"
        elif isinstance(error, RequestRejected):
            kind = "rejected"
        elif isinstance(error, ConnectionLost):
            kind = "transport"
        else:
            kind = "internal"
        reason = (error.reason if isinstance(error, IodClientError)
                  else str(error).strip() or type(error).__name__)
        self._daemon_answer_error = DaemonAnswerError(kind, reason)

    def _finish_frame_if_terminal(self) -> bool:
        frame = self._frame
        if frame is None or frame.retired or self._ready:
            return False
        if not frame.local_settled:
            return False
        if frame.remote_token is not None and not frame.remote_settled:
            return False
        if self._remote is not None:
            answered: SourcePosition | None = self._remote_answered
            bracket = None
        else:
            bracket = frame.local_bracket
            answered = (bracket.opening
                        if bracket is not None and bracket.opening == bracket.closing
                        else None)
        if frame.requested_source is None:
            raise ValueError("a frame requires its requested source")
        certified = answered is not None and answered == frame.requested_source
        snapshot = None
        if bracket is not None:
            if self._local_settlement is None or self._local_settlement.outcome is None:
                raise ValueError("a local frame requires its captured snapshot")
            snapshot = self._local_settlement.outcome.snapshot
        certificate = FrameProvenance(
            frame.owner_id, frame.window, frame.requested_source, answered,
            bracket, snapshot=snapshot, certified=certified)
        self._finish()
        frame.certificate = certificate
        frame.answered_source = answered
        self._build_ranking_provenance(frame, answered, bracket, snapshot)
        return True

    def _build_ranking_provenance(self, frame: _FrameOwner,
                                  answered: SourcePosition | None,
                                  bracket: LocalBracket | None,
                                  snapshot: SnapshotProvenance | None) -> None:
        remote = self._remote is not None
        digest = self._remote_snapshot if remote else None
        rows = self._remote if remote else self._ranking
        if rows is None:
            return
        provenance: dict[str, AnswerProvenance] = {}
        for row in rows:
            key = row.app_key
            if remote:
                row_digest = digest
                if not isinstance(row_digest, str):
                    raise ValueError("remote ranking snapshot identity is invalid")
                row_answered: SourcePosition | None = answered
                row_bracket = None
            else:
                row_digest = local_answer_digest("ranking", key, frame.window, row)
                row_answered = answered
                row_bracket = bracket
            identity = AcceptedIdentity("ranking", key, frame.window, row_digest)
            stamps = self._ranking_stamps
            if identity not in stamps:
                stamps[identity] = self._clock_ns()
            provenance[key] = AnswerProvenance(
                "ranking", key, retention_scope(key, frame.window), frame.window,
                identity, stamps[identity], row_answered, row_bracket,
                None if remote else snapshot)
        frame.ranking_provenance = provenance

    # -- scheduler: one wire occupant, priority and stable serial (§5.1) --- #

    def _entry_is_live(self, token: RequestToken) -> bool:
        if token.token_id in self._retired_tokens:
            return False
        if token.owner == "frame":
            frame = self._frame
            return (frame is not None and not frame.retired
                    and frame.owner_id == token.owner_id
                    and not frame.remote_settled)
        if token.owner == "detail":
            return self._detail_token_live(token)
        owner = self._file_owners.get(token.owner_id)
        return (owner is not None and not owner.retired
                and owner.generation == self._generation
                and owner.current is not None
                and owner.current.token_id == token.token_id)

    def _dispatch(self) -> None:
        """Issue the highest-priority live queued request while the slot is free."""

        if self._closed or self._occupant is not None:
            return
        while self._queue:
            entry = min(self._queue, key=lambda item: (item.priority, item.serial))
            self._queue.remove(entry)
            if not self._entry_is_live(entry.token):
                continue
            try:
                future = self.client.query(entry.token.request)
            except Exception:
                self._settle(entry.token, None)
                continue
            self._occupant = (entry.token, future)
            self._remote_futures[entry.token.token_id] = future
            try:
                state = self.client.query_transport(future)
            except Exception:
                issue_source = None
            else:
                issue_source = state.issued_source
                if state.phase == "terminal" and state.request_id is None:
                    issue_source = state.answered_source
            self._remote_issue_sources[entry.token.token_id] = issue_source
            return

    def _collect_remote(self) -> bool:
        if self._occupant is None:
            return False
        token, future = self._occupant
        if not future.done():
            return False
        try:
            value = future.result()
        except Exception as error:  # noqa: BLE001 - the bound terminal exception
            settlement: SettlementInput = RemoteSettlement(future, error=error)
        else:
            settlement = RemoteSettlement(future, response=value)
        changed = self._settle(token, settlement)
        try:
            state = self.client.query_transport(future)
        except Exception:
            state = None
        if state is None:
            release = True
        else:
            release = state.phase in ("terminal", "not_sent", "lost")
            issued_at_ns = state.issued_at_ns
            if not release and state.phase == "expired" and issued_at_ns is not None:
                deadline = issued_at_ns + REQUEST_LIFETIME_NS + NO_ANSWER_NS
                if self._clock_ns() >= deadline:
                    try:
                        retired = self.client.retire_query_connection(future)
                    except Exception:
                        retired = False
                    if retired:
                        try:
                            state = self.client.query_transport(future)
                        except Exception:
                            state = None
                        release = (state is None
                                   or state.phase in ("terminal", "not_sent", "lost"))
        if release:
            self._occupant = None
            self._remote_futures.pop(token.token_id, None)
            self._remote_issue_sources.pop(token.token_id, None)
        return changed

    def _collect_local_frame(self) -> bool:
        frame = self._frame
        if frame is None or frame.local_settled or self._local_future is None:
            return False
        if frame.local_token.token_id in self._settled_tokens:
            return False
        if self._local_generation != self._generation:
            return False
        if self._local_worker_id in self._capture_jobs:
            return False
        if not self._local_future.done():
            return False
        worker_id = self._local_worker_id
        if worker_id is None:
            self._frame_error = ValueError("a local frame has no worker binding")
            self.view_state = "unavailable: local history evidence"
            return False
        binding = self._worker_bindings.get(worker_id)
        capture = self._worker_capture_sets.get(worker_id)
        try:
            outcome = self._local_future.result()
        except Exception as error:  # noqa: BLE001 - the existing readiness seam
            settlement: LocalSettlement = LocalSettlement(
                worker_id, capture=capture, error=error)
        else:
            try:
                settlement = LocalSettlement(
                    worker_id, capture=capture, outcome=outcome)
            except Exception as error:  # noqa: BLE001 - a foreign/typed failure
                settlement = LocalSettlement(worker_id, capture=capture, error=error)
        if binding is None:
            self._frame_error = ValueError("a local frame has no captured copy binding")
            self.view_state = "unavailable: local history evidence"
            return False
        if settlement.error is None:
            try:
                outcome = self._admitted_frame_outcome(outcome, binding)
            except Exception as caught:  # noqa: BLE001 - reported after the capture
                settlement = LocalSettlement(worker_id, capture=capture, error=caught)
            else:
                try:
                    settlement = self._close_local_settlement(
                        LocalSettlement(worker_id, capture=capture, outcome=outcome),
                        binding=binding)
                except Exception as caught:  # noqa: BLE001 - retryable observation
                    self._frame_error = caught
                    self.view_state = "unavailable: local history evidence"
                    return False
        self._local_settlement = settlement
        self._frame_error = None
        self._settle(frame.local_token, settlement)
        return True

    def _admitted_frame_outcome(self, outcome: object,
                                binding: _LocalCopyBinding) -> LocalFrameOutcome:
        """Validate a frame outcome against the owner's admitted binding.

        The submitted copy receives the admitted worker id and request, so a
        result under any other identity is foreign. It is rejected before the
        owner publishes any frame fact, and a worker result is never relabelled
        as this admission (§3.4, §4.3).
        """

        if not isinstance(outcome, LocalFrameOutcome):
            raise ValueError("a local frame worker must return a typed frame outcome")
        # Re-enter the record's own validation: a worker/forged object is not
        # proof that every routing field is still well formed.
        LocalFrameOutcome.__post_init__(outcome)
        if outcome.worker_id != binding.worker_id:
            raise ValueError("a local frame worker answered another worker binding")
        if outcome.request != binding.request:
            raise ValueError("a local frame worker answered another request")
        if binding.snapshot is None or outcome.snapshot != binding.snapshot:
            raise ValueError("a local frame worker answered another snapshot")
        return outcome

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

    def _start_local_detail(self, token: RequestToken) -> None:
        if self._reserve_background_worker("detail", worker_id=token.token_id) is None:
            self._unscheduled[token.token_id] = token
            return
        try:
            self._worker_bindings[token.token_id] = self._capture_local_binding(
                worker_id=token.token_id, token_id=token.token_id, request=token.request)
            self._request_local_capture(token)
        except Exception as error:  # noqa: BLE001 - exact dependency failure
            self._current_detail_failure(token, error)
            self._reap_background_worker(token.token_id)

    def _schedule_unscheduled(self) -> None:
        for token_id, token in tuple(self._unscheduled.items()):
            if not self._detail_token_live(token) or token_id in self._retired_tokens:
                self._unscheduled.pop(token_id, None)
                continue
            if not self._background_capacity("detail"):
                continue
            self._unscheduled.pop(token_id)
            self._start_local_detail(token)

    def _collect_local_workers(self) -> bool:
        changed = False
        while True:
            try:
                token, settlement = self._completed.get_nowait()
            except queue.Empty:
                break
            if self._closed:
                continue
            if settlement.worker_id != token.token_id:
                self._reap_background_worker(token.token_id)
                continue
            changed |= self._settle(token, settlement)
            entry = self._worker_jobs.get(token.token_id)
            if entry is None:
                continue
            future = entry[1]
            if future is None or future.done():
                self._reap_background_worker(token.token_id)
            else:
                self._drained_futures[token.token_id] = future
                self._worker_jobs[token.token_id] = (entry[0], None)
        return changed

    def _admit_successors(self) -> None:
        pending = list(self._deferred_admissions)
        self._deferred_admissions.clear()
        for kind in pending:
            lifetime = self._detail
            if lifetime is None:
                continue
            token = lifetime.successors.get(kind)
            if token is None or token.token_id in self._retired_tokens:
                lifetime.successors.pop(kind, None)
                continue
            if lifetime.live.get(kind) is not None:
                continue  # a newer live owner still owns the wire
            lifetime.successors.pop(kind, None)
            lifetime.live[kind] = token
            if token.scope is None:
                self._retired_tokens.add(token.token_id)
                continue
            slot = lifetime.slots.get((kind, token.scope))
            if slot is None:
                slot = _PartSlot(kind, token.scope)
                lifetime.slots[(kind, token.scope)] = slot
            slot.phase = "pending"
            slot.token = token
            slot.reason = None
            slot.error = None
            if token.origin == "remote":
                self._queue.append(
                    _QueueEntry(token, _PRIORITY_DETAIL, self._next_serial()))
            else:
                self._start_local_detail(token)

    def drain(self) -> bool:
        """Collect, settle, reap and dispatch once; never wait or mutate widgets."""

        if self._closed:
            if self._gap_writes:
                self._drain_transport_gaps()
            self._reap_terminal_frame_workers()
            self._reap_terminal_background_workers()
            return False
        self._collect_boot()
        changed = self._drain_transport_gaps()
        changed |= self._reconcile_authority()
        changed |= self._advance_preparation()
        changed |= self._collect_captures()
        changed |= self._collect_local_frame()
        self._reap_terminal_frame_workers()
        changed |= self._collect_local_workers()
        self._reap_terminal_background_workers()
        changed |= self._collect_remote()
        changed |= self._finish_frame_if_terminal()
        self._schedule_unscheduled()
        self._dispatch()
        if changed:
            self._notify()
            self._admit_successors()
            self._dispatch()
        return bool(changed)

    def _finish(self) -> None:
        self._ready = True
        self.view_state = "incomplete: not_retained" if self._not_retained else (
            "incomplete: ownership gap" if self._gap_ranges else (
                "incomplete: boot chronology unproved" if self._chronology_unproved
                else "ready"))

    def _take_preparation_error(self, window: Window) -> Exception | None:
        if window != self._window:
            return None
        if self._frame_error is not None:
            error, self._frame_error = self._frame_error, None
            return error
        frame = self._frame
        if frame is not None and not frame.retired and frame.error is not None:
            error, frame.error = frame.error, None
            return error
        return None

    def ready(self, window: Window) -> bool:
        if window != self._window:
            return False
        self.drain()
        error = self._take_preparation_error(window)
        if error is not None:
            raise error
        frame = self._frame
        if frame is None or frame.retired:
            return False
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
                                                   unavailable_reason="not_retained"))
                    for row in rows]
        assert self._frame is not None
        self._applied_ranking_provenance = dict(self._frame.ranking_provenance)
        return sorted(rows, key=lambda row: (-getattr(row, "total_bytes" if metric == "total_bytes" else metric),
                                             row.app_key.encode("utf-8")))

    def _consume_local(self):
        """Compatibility seam: settle the local frame copy once, or raise its error."""

        settled = self._collect_local_frame()
        if self._frame_error is not None:
            error, self._frame_error = self._frame_error, None
            raise error
        frame = self._frame
        if frame is not None and frame.error is not None:
            error, frame.error = frame.error, None
            raise error
        return settled

    def _part_coverage(self, parts):
        coverage = self._combine_coverage([part.coverage for part in parts])
        if self._not_retained:
            coverage = replace(coverage, complete=False, unavailable_reason="not_retained")
        return coverage

    @staticmethod
    def _merge_disk_shares(parts, app_id, window, coverage):
        """Preserve the existing device merge and signed residual reducers."""

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

    @staticmethod
    def _merge_series(parts, app_id, window, coverage):
        """Preserve the existing point/peak/residual merge reducers."""

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
            coverage=coverage)
