"""Pure file attribution and deterministic, boot-scoped file-hour accumulation.

A committed frame supplies cgroup owners; an injected UUID lookup supplies only
proved filesystem identities. No path reconstruction or host lookup occurs here.
Contributions must already have passed stream receipt/ownership selection and
arrive in accepted frame order. Coverage is a receipt-level quantity: repeating
it beside each file in a frame counts it once for that app. All candidates remain
in canonical pending state; top-N is applied only when the hour closes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING

from appio.contracts import (
    CounterOverflowError, FILE_HOUR_NS, FileCoverageInput, FileObservation,
    HookHits, HourlyFileSnapshot, SQLITE_INT_MAX, UnknownOwnerInput,
    validate_file_uuid, validate_sqlite_integer,
)
from appio.durable import (
    FileEntry, FileHourApp, FileHourCoverage, FileHourState, Receipt,
    UnknownOwnerCoverage, UnknownOwnerState, canonical_json, decode_file_hour_state,
)
from appio.identity import resolve_app, validate_app_key

if TYPE_CHECKING:
    from appio.bpf_fileio import FileDrain, FileEvent
    from appio.cgroups import CgroupObservation
    from appio.procs import StableProcRecord
    from appio.sampler import AggregatedFrame

HOOKS = tuple(f.name for f in fields(HookHits))
FILE_COUNTERS = tuple(f.name for f in fields(FileCoverageInput)
                      if f.name not in ("hook_hits", "complete"))
ZERO_COVERAGE = FileCoverageInput(**dict.fromkeys(FILE_COUNTERS, 0),
                                hook_hits=HookHits(0, 0, 0, 0, 0, 0), complete=True)
ZERO_UNKNOWN = UnknownOwnerInput(0, 0, 0, 0, 0, True)


def checked_sum(*values: int, field: str) -> int:
    """Add in Python; refuse an unrepresentable result before binding to SQLite."""
    value = sum(values)
    if not 0 <= value <= SQLITE_INT_MAX:
        raise CounterOverflowError(field)
    return value


def merge_coverage(a: FileCoverageInput, b: FileCoverageInput) -> FileCoverageInput:
    return FileCoverageInput(
        **{name: checked_sum(getattr(a, name), getattr(b, name), field=name)
           for name in FILE_COUNTERS},
        hook_hits=HookHits(**{name: checked_sum(getattr(a.hook_hits, name),
                                             getattr(b.hook_hits, name), field=name)
                              for name in HOOKS}),
        complete=a.complete and b.complete,
    )


def top_key(entry: FileEntry, path_id: int = 0) -> tuple:
    """Total order on distinct hourly identities, with the local uniqueness guard."""
    return (-(entry.bytes_r + entry.bytes_w), -(entry.reads + entry.writes),
            entry.dev.encode("utf-8"), entry.inode, entry.generation, entry.identity_weak,
            entry.fs_uuid.encode("utf-8"), entry.basename.encode("utf-8"), path_id)


@dataclass(frozen=True)
class FileFrameContext:
    """Immutable owner evidence for exactly one successfully committed T/D frame.

    Owners come from current stable readable/EACCES process records joined by
    their exact cgroup path, never device deltas or restored process baselines.
    Empty and shared owner sets remain explicit. Key 0 may occur in synthetic
    evidence but attribution always reserves it for unknown-owner overflow.
    """
    boot_id: str
    epoch: int
    seq: int
    observed_mono_ns: int
    app_keys: tuple[str, ...]
    owners_by_cgroup: tuple[tuple[int, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.boot_id, str) or not self.boot_id or "\x00" in self.boot_id:
            raise ValueError("file context boot_id must be nonempty text without NUL")
        Receipt("iod", self.epoch, self.seq)
        validate_sqlite_integer(self.observed_mono_ns, "file_context.observed_mono_ns")
        if not isinstance(self.app_keys, tuple):
            raise ValueError("file context app_keys must be an immutable tuple")
        for key in self.app_keys:
            validate_app_key(key)
        if self.app_keys != tuple(sorted(set(self.app_keys), key=lambda key: key.encode("utf-8"))):
            raise ValueError("file context app_keys must be bytewise sorted and unique")
        if not isinstance(self.owners_by_cgroup, tuple):
            raise ValueError("file context owners must be an immutable tuple")
        previous = -1
        for item in self.owners_by_cgroup:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("file context owner entry must be a pair")
            cgroup_id, owners = item
            validate_sqlite_integer(cgroup_id, "file_context.cgroup_id")
            if cgroup_id <= previous:
                raise ValueError("file context cgroup IDs must be sorted and unique")
            previous = cgroup_id
            if not isinstance(owners, tuple) or any(key not in self.app_keys for key in owners):
                raise ValueError("file context owners must be an immutable tuple of monitored apps")
            if owners != tuple(sorted(set(owners), key=lambda key: key.encode("utf-8"))):
                raise ValueError("file context owners must be bytewise sorted and unique")

    @classmethod
    def from_frame(cls, frame: AggregatedFrame, records: tuple[StableProcRecord, ...],
                   cgroups: tuple[CgroupObservation, ...]) -> FileFrameContext:
        """Build staged evidence; the collector publishes it only after commit."""
        by_path: dict[str, set[str]] = {}
        for record in records:
            if record.identity.boot_id != frame.boot_id:
                raise ValueError("file context process belongs to a different boot")
            if record.io_outcome.status not in ("readable", "eacces"):
                continue
            inputs = record.app_key_inputs
            key = resolve_app(inputs.cgroup_path, inputs.exe, inputs.desktop_id)
            if record.cgroup_path is not None:
                by_path.setdefault(record.cgroup_path, set()).add(key)
        by_id: dict[int, set[str]] = {}
        for cgroup in cgroups:
            if cgroup.boot_id != frame.boot_id:
                raise ValueError("file context cgroup belongs to a different boot")
            by_id.setdefault(cgroup.cgroup_id, set()).update(by_path.get(cgroup.cgroup_path, ()))
        return cls(frame.boot_id, frame.epoch, frame.seq, frame.observed_mono_ns,
                   tuple(sorted({c.app_key for c in frame.coverage}, key=lambda key: key.encode("utf-8"))),
                   tuple((cg, tuple(sorted(owners, key=lambda key: key.encode("utf-8"))))
                         for cg, owners in sorted(by_id.items())))


@dataclass(frozen=True)
class FileIdentityResolver:
    """Inject a proved UUID lookup, returning None/empty or OSError if unavailable.

    The caller owns the proof that a UUID belongs to this supported filesystem.
    dev_t and inode are never substituted for that UUID. No result is cached
    across drains: a later proof creates a separate strong identity, not an
    in-place promotion of earlier weak observations.
    """
    fs_uuid_for_dev: Callable[[str], str | None]

    def resolve(self, dev: str) -> tuple[str, bool]:
        try:
            uuid = self.fs_uuid_for_dev(dev)
        except OSError:
            uuid = None
        uuid = "" if uuid is None else uuid
        weak = uuid == ""
        return validate_file_uuid(uuid, weak, "file_identity"), weak


@dataclass(frozen=True)
class CapturedName:
    fs_uuid: str
    dev: str
    inode: int
    generation: int
    identity_weak: int
    basename: str
    state: str
    first_mono_ns: int
    last_mono_ns: int

    @property
    def path_key(self) -> tuple:
        return (self.fs_uuid, self.dev, self.inode, self.generation,
                self.basename, self.identity_weak)


@dataclass(frozen=True)
class FileFrameContribution:
    """Per-frame inputs for HourlyAccumulator, including explicit zero-F apps."""
    apps: tuple[tuple[str, tuple[FileObservation, ...], FileCoverageInput], ...]
    unknown_owner: UnknownOwnerInput
    captured_names: tuple[CapturedName, ...]


def _captured_basename(event: FileEvent, name_cap: int) -> tuple[str, str]:
    try:
        name = event.basename.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        name = ""
    # Empty/invalid bytes cannot honestly be a filesystem name. Never use a
    # replacement-decoded string or a full path as the captured basename.
    if not name or "/" in name:
        return "[unresolved]", "unresolved"
    # The shipped wire length is clamped, so even exactly-cap-length captures
    # cannot be proved full. A split multibyte character was handled above.
    return name, "truncated" if event.name_len >= name_cap else "resolved"


def attribute_file_drain(drain: FileDrain, context: FileFrameContext,
                         resolver: FileIdentityResolver) -> FileFrameContribution:
    """Attribute one synthetic/live drain without reading proc, sysfs or paths.

    Only one distinct current owner is authoritative. Each unowned event adds
    one unknown file (not its access count); unowned stats add only their
    overflow counters. Hooks/skips never manufacture F candidates. Weak-row
    counts are derived from distinct hourly identities at finalization, not
    summed per drain. No ranking or top-100 truncation occurs at this boundary.
    """
    # Lazy import preserves the T/D-only application's no-BPF import boundary.
    from .bpf_fileio import NAME_CAP, RESERVED_CGROUP_ID

    if (drain.receipt.epoch, drain.receipt.seq) != (context.epoch, context.seq):
        raise ValueError("file drain receipt does not match the committed frame")
    owners = dict(context.owners_by_cgroup)

    def owner(cgroup_id: int) -> str | None:
        candidates = owners.get(cgroup_id, ())
        if cgroup_id == RESERVED_CGROUP_ID or len(candidates) != 1:
            return None
        return candidates[0]

    base = replace(ZERO_COVERAGE, complete=drain.complete,
                   gap_intervals=int(not drain.complete))
    coverages = dict.fromkeys(context.app_keys, base)
    rows: dict[str, list[FileObservation]] = {key: [] for key in context.app_keys}
    identities: dict[str, tuple[str, bool]] = {}
    names: dict[tuple, CapturedName] = {}
    unknown = replace(ZERO_UNKNOWN, complete=drain.complete,
                      gap_intervals=int(not drain.complete))

    def add_unknown(count: int, read: int, written: int) -> None:
        nonlocal unknown
        unknown = replace(unknown,
            overflow_files=checked_sum(unknown.overflow_files, count, field="overflow_files"),
            overflow_bytes_r=checked_sum(unknown.overflow_bytes_r, read, field="overflow_bytes_r"),
            overflow_bytes_w=checked_sum(unknown.overflow_bytes_w, written, field="overflow_bytes_w"))

    for event in drain.events:
        key = owner(event.cgroup_id)
        if key is None:
            add_unknown(1, event.bytes_r, event.bytes_w)
            continue
        if event.dev not in identities:
            identities[event.dev] = resolver.resolve(event.dev)
        uuid, weak = identities[event.dev]
        basename, state = _captured_basename(event, NAME_CAP)
        row = FileObservation(key, uuid, event.dev, event.inode, event.generation,
                              basename, state, weak, event.reads, event.writes,
                              event.bytes_r, event.bytes_w, context.observed_mono_ns)
        rows[key].append(row)
        name = CapturedName(uuid, event.dev, event.inode, event.generation,
                            int(weak), basename, state,
                            context.observed_mono_ns, context.observed_mono_ns)
        prior = names.get(name.path_key)
        if prior is not None:
            name = replace(name, state=max((prior.state, name.state),
                           key=("resolved", "truncated", "unresolved").index))
        names[name.path_key] = name
    for item in drain.stats:
        key = owner(item.cgroup_id)
        if key is None:
            add_unknown(item.overflow_files, item.overflow_bytes_r, item.overflow_bytes_w)
        else:
            cov = replace(ZERO_COVERAGE,
                uncounted_overflow_files=item.overflow_files,
                uncounted_overflow_bytes_r=item.overflow_bytes_r,
                uncounted_overflow_bytes_w=item.overflow_bytes_w,
                skipped_nonfile=item.skipped_nonfile,
                skipped_unsupported_fs=item.skipped_unsupported_fs,
                hook_hits=item.hook_hits)
            coverages[key] = merge_coverage(coverages[key], cov)
    return FileFrameContribution(
        tuple((key, tuple(rows[key]), coverages[key]) for key in context.app_keys), unknown,
        tuple(names[key] for key in sorted(names, key=lambda key: (
            key[0].encode("utf-8"), key[1].encode("utf-8"), key[2], key[3], key[4].encode("utf-8"), key[5]))))


@dataclass(frozen=True)
class _HourlySnapshot(HourlyFileSnapshot):
    pending_state: FileHourState
    captured_names: tuple[CapturedName, ...]
    top_n: int


@dataclass
class _App:
    files: dict[tuple, FileEntry]
    coverage: FileCoverageInput
    last_receipt: Receipt
    receipt_coverage: dict[Receipt, FileCoverageInput | None]


def _input_coverage(coverage: FileHourCoverage) -> FileCoverageInput:
    return FileCoverageInput(**{name: getattr(coverage, name) for name in FILE_COUNTERS},
                             hook_hits=coverage.hook_hits, complete=bool(coverage.complete))


class HourlyAccumulator:
    """All authoritative contributions in one boot-scoped, half-open hour."""
    def __init__(self, boot_id: str, hour_start_mono_ns: int):
        # Reuse the strict canonical validator for boot, alignment and range rules.
        FileHourState(boot_id, hour_start_mono_ns, (),
                      UnknownOwnerState(0, 0, 0, UnknownOwnerCoverage(0, 0, 1), None))
        self.boot_id = boot_id
        self.hour_start_mono_ns = hour_start_mono_ns
        self._apps: dict[str, _App] = {}
        self._unknown = ZERO_UNKNOWN
        self._unknown_receipt: Receipt | None = None
        self._unknown_seen: set[Receipt] = set()
        self._names: dict[tuple, CapturedName] = {}

    def add_observation(self, receipt: Receipt, app_key: str,
                        observation: FileObservation | FileEntry | None,
                        coverage: FileCoverageInput) -> None:
        """Stage one candidate, or an explicit zero-F monitored app receipt.

        FileEntry also accepts canonical durable observations. None records
        coverage for a monitored app with no supported file observations.
        """
        if not isinstance(receipt, Receipt) or not isinstance(coverage, FileCoverageInput):
            raise ValueError("a validated Receipt and FileCoverageInput are required")
        validate_app_key(app_key)
        entry = None
        if observation is not None:
            if isinstance(observation, FileObservation):
                if observation.app_key != app_key:
                    raise ValueError("observation.app_key does not match app_key")
                entry = FileEntry(observation.fs_uuid, observation.dev, observation.inode,
                                  observation.generation, int(observation.identity_weak),
                                  observation.basename, (), observation.state,
                                  observation.reads, observation.writes, observation.bytes_r,
                                  observation.bytes_w, observation.last_seen_mono_ns)
            elif isinstance(observation, FileEntry):
                entry = observation
            else:
                raise ValueError("observation must be FileObservation, FileEntry or None")
            if not self.hour_start_mono_ns <= entry.last_seen_mono_ns < self.hour_start_mono_ns + FILE_HOUR_NS:
                raise ValueError("last_seen_mono_ns must be inside the half-open hour")
        previous = self._apps.get(app_key)
        app = _App(dict(previous.files), previous.coverage, previous.last_receipt,
                   dict(previous.receipt_coverage)) if previous else _App({}, ZERO_COVERAGE, receipt, {})
        names = dict(self._names)
        if receipt not in app.receipt_coverage:
            app.coverage = merge_coverage(app.coverage, coverage)
            app.receipt_coverage[receipt] = coverage
            # LAST describes the file; provenance describes the latest accepted
            # contributing frame, including coverage-only and handover frames.
            app.last_receipt = receipt
        elif app.receipt_coverage[receipt] not in (None, coverage) and coverage != ZERO_COVERAGE:
            raise ValueError("conflicting coverage for the same app receipt")
        if entry is not None:
            old = app.files.get(entry.identity)
            if old:
                all_names = sorted(set((old.basename, *old.aliases, entry.basename, *entry.aliases)), key=lambda name: name.encode("utf-8"))
                # Preserve the least informative captured state; never silently resolve a truncated name.
                state = max((old.state, entry.state), key=("resolved", "truncated", "unresolved").index)
                combined = {name: checked_sum(getattr(old, name), getattr(entry, name), field=name)
                            for name in ("reads", "writes", "bytes_r", "bytes_w")}
                merged = replace(entry, basename=all_names[0], aliases=tuple(all_names[1:]),
                                 state=state, last_seen_mono_ns=max(old.last_seen_mono_ns, entry.last_seen_mono_ns), **combined)
            else:
                merged = entry
            app.files[entry.identity] = merged
            for basename in (entry.basename, *entry.aliases):
                name = CapturedName(entry.fs_uuid, entry.dev, entry.inode, entry.generation,
                                    entry.identity_weak, basename, entry.state,
                                    entry.last_seen_mono_ns, entry.last_seen_mono_ns)
                prior_name = names.get(name.path_key)
                if prior_name:
                    name = replace(name, first_mono_ns=min(prior_name.first_mono_ns, name.first_mono_ns),
                                   last_mono_ns=max(prior_name.last_mono_ns, name.last_mono_ns),
                                   state=max((name.state, prior_name.state), key=("resolved", "truncated", "unresolved").index))
                names[name.path_key] = name
        # Publish only after every counter and identity has validated.
        self._apps[app_key], self._names = app, names

    def add_unknown_owner(self, receipt: Receipt, unknown: UnknownOwnerInput) -> None:
        if not isinstance(receipt, Receipt) or not isinstance(unknown, UnknownOwnerInput):
            raise ValueError("a validated Receipt and UnknownOwnerInput are required")
        if receipt in self._unknown_seen:
            return
        values = {f.name: checked_sum(getattr(self._unknown, f.name), getattr(unknown, f.name), field=f.name)
                  for f in fields(UnknownOwnerInput) if f.name != "complete"}
        updated = UnknownOwnerInput(**values, complete=self._unknown.complete and unknown.complete)
        self._unknown = updated
        self._unknown_seen.add(receipt)
        self._unknown_receipt = receipt

    def state(self) -> FileHourState:
        apps = tuple(FileHourApp(key, tuple(sorted(app.files.values(), key=lambda entry: entry.identity)),
                                FileHourCoverage(**{name: getattr(app.coverage, name) for name in FILE_COUNTERS},
                                                 hook_hits=app.coverage.hook_hits, complete=int(app.coverage.complete)),
                                app.last_receipt)
                     for key, app in sorted(self._apps.items(), key=lambda pair: pair[0].encode("utf-8")))
        u = self._unknown
        return FileHourState(self.boot_id, self.hour_start_mono_ns, apps,
                             UnknownOwnerState(u.overflow_files, u.overflow_bytes_r, u.overflow_bytes_w,
                                               UnknownOwnerCoverage(u.gap_intervals, u.overlap_uncertain_intervals, int(u.complete)),
                                               self._unknown_receipt))

    def state_json(self) -> str:
        return canonical_json(self.state())

    @classmethod
    def from_state(cls, state: FileHourState) -> HourlyAccumulator:
        if not isinstance(state, FileHourState):
            raise ValueError("state must be a validated FileHourState")
        # Round-trip at the trust boundary rather than relying on SQL json_valid.
        state = decode_file_hour_state(canonical_json(state), state.boot_id, state.hour_start_mono_ns)
        result = cls(state.boot_id, state.hour_start_mono_ns)
        for app in state.apps:
            result._apps[app.app_key] = _App({e.identity: e for e in app.files}, _input_coverage(app.coverage),
                                           app.last_receipt, {app.last_receipt: None})
        u = state.unknown_owner
        result._unknown = UnknownOwnerInput(u.overflow_files, u.overflow_bytes_r, u.overflow_bytes_w,
                                           u.coverage.gap_intervals, u.coverage.overlap_uncertain_intervals, bool(u.coverage.complete))
        result._unknown_receipt = u.last_receipt
        if u.last_receipt is not None:
            result._unknown_seen.add(u.last_receipt)
        return result

    def finalize(self, top_n: int = 100) -> HourlyFileSnapshot:
        validate_sqlite_integer(top_n, "top_n")
        if top_n > 100:
            raise ValueError("top_n must be at most 100")
        apps = []
        for key, app in sorted(self._apps.items(), key=lambda pair: pair[0].encode("utf-8")):
            ranked = sorted(app.files.values(), key=top_key)
            kept, omitted = ranked[:top_n], ranked[top_n:]
            cov = app.coverage
            cov = replace(cov,
                counted_omitted_files=checked_sum(cov.counted_omitted_files, len(omitted), field="counted_omitted_files"),
                counted_omitted_bytes_r=checked_sum(cov.counted_omitted_bytes_r, *(e.bytes_r for e in omitted), field="counted_omitted_bytes_r"),
                counted_omitted_bytes_w=checked_sum(cov.counted_omitted_bytes_w, *(e.bytes_w for e in omitted), field="counted_omitted_bytes_w"),
                weak_identity_rows=max(cov.weak_identity_rows, sum(e.identity_weak for e in ranked)))
            observations = tuple(FileObservation(key, e.fs_uuid, e.dev, e.inode, e.generation,
                                                      e.basename, e.state, bool(e.identity_weak),
                                                      e.reads, e.writes, e.bytes_r, e.bytes_w, e.last_seen_mono_ns)
                                 for e in kept)
            apps.append((key, observations, cov))
        return _HourlySnapshot(self.boot_id, self.hour_start_mono_ns, tuple(apps), self._unknown,
                               self.state(), tuple(self._names[key] for key in sorted(self._names,
                               key=lambda key: (key[0].encode(), key[1].encode(), key[2], key[3], key[4].encode(), key[5]))), top_n)


def hourly_file_budget(*, apps: int, top_n: int = 100, days: int = 14,
                       payload_bytes: int = 88) -> dict[str, int]:
    """Exact planning payload arithmetic, separate from measured physical size."""
    for name, value in (("apps", apps), ("top_n", top_n), ("days", days), ("payload_bytes", payload_bytes)):
        validate_sqlite_integer(value, name)
    snapshot = apps * top_n
    daily = snapshot * 24
    retained = daily * days
    return dict(rows_per_snapshot=snapshot, bytes_per_snapshot=snapshot * payload_bytes,
                rows_per_day=daily, bytes_per_day=daily * payload_bytes,
                retained_rows=retained, retained_payload_bytes=retained * payload_bytes)
