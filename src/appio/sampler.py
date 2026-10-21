"""Pure process, reap-credit, coverage, and device aggregation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from .cgroups import CgroupObservation, DeviceDelta, device_deltas
from .clock import IntervalEvidence, SuspensionSet, attribute_interval
from .contracts import CounterVector, RAW_BUCKET_NS, validate_sqlite_integer
from .identity import resolve_app, validate_app_key
from .launch import LaunchIdentity, LaunchState, ProcessState, ReapCredit, assign_launches
from .procs import ProcessIdentity, StableProcRecord

__all__ = [
    "AggregatedDeviceDelta",
    "AggregatedFrame",
    "AppDelta",
    "FrameCoverage",
    "SamplerState",
    "StaleFrameError",
    "TickInput",
    "aggregate_tick",
]

_COUNTER_NAMES = ("rchar", "wchar", "read_bytes", "write_bytes")
_VALID_SOURCES = ("gui", "iod")


def _vector(value: object, field: str = "counters") -> CounterVector:
    if not isinstance(value, tuple) or len(value) != 4:
        raise ValueError(f"{field} must be a four-counter tuple")
    return tuple(validate_sqlite_integer(item, f"{field}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


def _signed_ns(value: object, field: str) -> int:
    return validate_sqlite_integer(value, field, nonnegative=False)


def _boot(value: object, field: str = "boot_id") -> str:
    if not isinstance(value, str) or value == "" or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string without NUL")
    return value


def _source(value: object) -> str:
    if not isinstance(value, str) or value not in _VALID_SOURCES:
        raise ValueError("source must be 'gui' or 'iod'")
    return value


def _or_delta_kind(left: str, right: str) -> str:
    order = {"normal": 0, "baseline": 1, "reset": 2}
    if left not in order or right not in order:
        raise ValueError("delta_kind must be normal, baseline, or reset")
    return left if order[left] >= order[right] else right


def _sum_vectors(left: CounterVector, right: CounterVector) -> CounterVector:
    return tuple(left[index] + right[index] for index in range(4))  # type: ignore[return-value]


def _zero() -> CounterVector:
    return (0, 0, 0, 0)


class StaleFrameError(ValueError):
    """The stream receipt is duplicate or older than the committed state."""


@dataclass(frozen=True)
class TickInput:
    """One already-read collector frame; no host process access happens here.

    ``known_uncovered`` carries measured gap bounds; None means unknown bounds.
    An epoch change without measured bounds also leaves rate time unknown.
    """

    boot_id: str
    source: Literal["gui", "iod"]
    epoch: int
    seq: int
    observed_mono_ns: int
    wall_ns: int
    processes: tuple[StableProcRecord, ...]
    cgroups: tuple[CgroupObservation, ...] = ()
    suspend_boundaries: tuple[tuple[int, int], ...] | None = ()
    unresolved_pids: tuple[int, ...] = ()
    vanished_pids: tuple[int, ...] = ()
    known_uncovered: tuple[tuple[int, int], ...] | None = ()

    def __post_init__(self) -> None:
        _boot(self.boot_id, "tick.boot_id")
        _source(self.source)
        epoch = validate_sqlite_integer(self.epoch, "tick.epoch")
        if epoch == 0:
            raise ValueError("tick.epoch must be positive")
        validate_sqlite_integer(self.seq, "tick.seq")
        validate_sqlite_integer(self.observed_mono_ns, "tick.observed_mono_ns")
        _signed_ns(self.wall_ns, "tick.wall_ns")
        if not isinstance(self.processes, tuple):
            raise ValueError("tick.processes must be a tuple")
        if any(not isinstance(record, StableProcRecord) for record in self.processes):
            raise ValueError("tick.processes must contain StableProcRecord values")
        for record in self.processes:
            if record.identity.boot_id != self.boot_id:
                raise ValueError("every process identity must use tick.boot_id")
        if not isinstance(self.cgroups, tuple):
            raise ValueError("tick.cgroups must be a tuple")
        if any(not isinstance(observation, CgroupObservation) for observation in self.cgroups):
            raise ValueError("tick.cgroups must contain CgroupObservation values")
        for observation in self.cgroups:
            if observation.boot_id != self.boot_id:
                raise ValueError("every cgroup observation must use tick.boot_id")
        if self.suspend_boundaries is not None:
            if not isinstance(self.suspend_boundaries, tuple):
                raise ValueError("tick.suspend_boundaries must be a tuple or None")
            for start, end in self.suspend_boundaries:
                validate_sqlite_integer(start, "tick.suspend_boundaries.start")
                validate_sqlite_integer(end, "tick.suspend_boundaries.end")
                if end < start:
                    raise ValueError("a suspend boundary must end at or after its start")
        if self.known_uncovered is not None:
            if not isinstance(self.known_uncovered, tuple):
                raise ValueError("tick.known_uncovered must be a tuple or None")
            for start, end in self.known_uncovered:
                validate_sqlite_integer(start, "tick.known_uncovered.start")
                validate_sqlite_integer(end, "tick.known_uncovered.end")
                if end < start:
                    raise ValueError("an uncovered interval must end at or after its start")
        if not isinstance(self.unresolved_pids, tuple):
            raise ValueError("tick.unresolved_pids must be a tuple")
        unresolved: set[int] = set()
        represented_pids = {record.identity.pid for record in self.processes}
        for pid in self.unresolved_pids:
            value = validate_sqlite_integer(pid, "tick.unresolved_pids.pid")
            if value == 0 or value in unresolved:
                raise ValueError("tick.unresolved_pids must contain unique positive PIDs")
            unresolved.add(value)
            if value in represented_pids:
                raise ValueError("a PID cannot be both unresolved and represented")
        if not isinstance(self.vanished_pids, tuple):
            raise ValueError("tick.vanished_pids must be a tuple")
        vanished: set[int] = set()
        for pid in self.vanished_pids:
            value = validate_sqlite_integer(pid, "tick.vanished_pids.pid")
            if value == 0 or value in vanished:
                raise ValueError("tick.vanished_pids must contain unique positive PIDs")
            vanished.add(value)
            if value in represented_pids or value in unresolved:
                raise ValueError("a PID cannot be represented, unresolved, and vanished")


@dataclass(frozen=True)
class AppDelta:
    """One app/launch contribution at one attributed boottime timestamp."""

    app_key: str
    launch_key: LaunchIdentity
    counters: CounterVector
    attributed_ts_mono_ns: int
    observed_mono_ns: int
    interval_start_mono_ns: int | None
    delta_kind: Literal["normal", "baseline", "reset"]
    first_seen_partial: bool
    spans_suspend: bool
    observed_ns: int | None = None

    def __post_init__(self) -> None:
        validate_app_key(self.app_key)
        if not isinstance(self.launch_key, LaunchIdentity):
            raise ValueError("app_delta.launch_key must be a LaunchIdentity")
        _vector(self.counters, "app_delta.counters")
        validate_sqlite_integer(self.attributed_ts_mono_ns, "app_delta.attributed_ts_mono_ns")
        validate_sqlite_integer(self.observed_mono_ns, "app_delta.observed_mono_ns")
        if self.interval_start_mono_ns is not None:
            validate_sqlite_integer(self.interval_start_mono_ns, "app_delta.interval_start_mono_ns")
        if self.observed_ns is not None:
            validate_sqlite_integer(self.observed_ns, "app_delta.observed_ns")
        if self.delta_kind not in ("normal", "baseline", "reset"):
            raise ValueError("app_delta.delta_kind is invalid")
        if not isinstance(self.first_seen_partial, bool) or not isinstance(self.spans_suspend, bool):
            raise ValueError("app_delta flags must be bools")


@dataclass(frozen=True)
class FrameCoverage:
    """One process-coverage receipt for an app and observed frame timestamp."""

    app_key: str
    observed_mono_ns: int
    inaccessible_pids: int = 0
    resets: int = 0
    reap_corrections: int = 0
    reap_unapplied: CounterVector = (0, 0, 0, 0)
    reap_unobserved: CounterVector = (0, 0, 0, 0)
    partial_intervals: int = 0
    gap_intervals: int = 0
    overlap_uncertain_intervals: int = 0
    spans_suspend_intervals: int = 0
    spans_suspend: bool = False
    reap_incomplete: bool = False
    counter_overflow: bool = False
    device_state: Literal["available", "not_delegated", "incomplete"] = "incomplete"

    def __post_init__(self) -> None:
        validate_app_key(self.app_key)
        validate_sqlite_integer(self.observed_mono_ns, "frame_coverage.observed_mono_ns")
        for name in (
            "inaccessible_pids",
            "resets",
            "reap_corrections",
            "partial_intervals",
            "gap_intervals",
            "overlap_uncertain_intervals",
            "spans_suspend_intervals",
        ):
            validate_sqlite_integer(getattr(self, name), f"frame_coverage.{name}")
        _vector(self.reap_unapplied, "frame_coverage.reap_unapplied")
        _vector(self.reap_unobserved, "frame_coverage.reap_unobserved")
        if not isinstance(self.spans_suspend, bool) or not isinstance(self.reap_incomplete, bool):
            raise ValueError("frame coverage flags must be bools")
        if self.device_state not in ("available", "not_delegated", "incomplete"):
            raise ValueError("frame_coverage.device_state is invalid")


@dataclass(frozen=True)
class AggregatedDeviceDelta:
    """One cgroup/device delta attributed to one uniquely owned AppKey."""

    app_key: str
    delta: DeviceDelta

    def __post_init__(self) -> None:
        validate_app_key(self.app_key)
        if not isinstance(self.delta, DeviceDelta):
            raise ValueError("aggregated_device_delta.delta must be DeviceDelta")


@dataclass(frozen=True)
class AggregatedFrame:
    """The pure result of one accepted frame and its staged next state."""

    boot_id: str
    source: Literal["gui", "iod"]
    epoch: int
    seq: int
    observed_mono_ns: int
    clock_offset_ns: int
    deltas: tuple[AppDelta, ...]
    coverage: tuple[FrameCoverage, ...]
    device_deltas: tuple[AggregatedDeviceDelta, ...]
    checkpoint_state: dict[str, object]

    def __post_init__(self) -> None:
        _boot(self.boot_id, "frame.boot_id")
        _source(self.source)
        validate_sqlite_integer(self.epoch, "frame.epoch")
        validate_sqlite_integer(self.seq, "frame.seq")
        validate_sqlite_integer(self.observed_mono_ns, "frame.observed_mono_ns")
        _signed_ns(self.clock_offset_ns, "frame.clock_offset_ns")
        if not isinstance(self.deltas, tuple) or not isinstance(self.coverage, tuple):
            raise ValueError("frame deltas and coverage must be tuples")
        if not isinstance(self.device_deltas, tuple):
            raise ValueError("frame.device_deltas must be a tuple")
        if not isinstance(self.checkpoint_state, dict):
            raise ValueError("frame.checkpoint_state must be a dict")


@dataclass(frozen=True)
class SamplerState:
    """Committed sampler state; all nested collections are immutable tuples."""

    boot_id: str
    source: Literal["gui", "iod"]
    epoch: int
    last_seq: int
    checkpoint_mono_ns: int
    processes: tuple[ProcessState, ...] = ()
    launches: tuple[LaunchState, ...] = ()
    device_baselines: tuple[tuple[tuple[str, int, str], tuple[int, int]], ...] = ()
    last_observed_mono_ns: int | None = None

    def __post_init__(self) -> None:
        _boot(self.boot_id, "state.boot_id")
        _source(self.source)
        validate_sqlite_integer(self.epoch, "state.epoch")
        if self.epoch == 0:
            raise ValueError("state.epoch must be positive")
        validate_sqlite_integer(self.last_seq, "state.last_seq")
        validate_sqlite_integer(self.checkpoint_mono_ns, "state.checkpoint_mono_ns")
        if self.last_observed_mono_ns is not None:
            validate_sqlite_integer(self.last_observed_mono_ns, "state.last_observed_mono_ns")
        if any(not isinstance(process, ProcessState) for process in self.processes):
            raise ValueError("state.processes must contain ProcessState values")
        if any(not isinstance(launch, LaunchState) for launch in self.launches):
            raise ValueError("state.launches must contain LaunchState values")

    def to_checkpoint(self) -> dict[str, object]:
        """Return the exact v1 sampler-checkpoint-shaped Python object."""

        processes = []
        for process in sorted(self.processes, key=lambda item: (item.identity.pid, item.identity.start_ticks)):
            processes.append(
                {
                    "pid": process.identity.pid,
                    "start_ticks": process.identity.start_ticks,
                    "ppid": process.ppid_identity.pid if process.ppid_identity is not None else 0,
                    "parent": _identity_json(process.ppid_identity),
                    "app_key": process.app_key,
                    "launch_root": _identity_json(process.launch_identity.root),
                    "launch_id": process.launch_identity.launch_id,
                    "baseline": _baseline_json(process.baseline),
                    "credits": [
                        {
                            "child": _identity_json(credit.child),
                            "disappearance_epoch": credit.disappearance_epoch,
                            "disappearance_seq": credit.disappearance_seq,
                            "remaining": _baseline_json(credit.remaining),
                            "correction_counted": int(credit.correction_counted),
                        }
                        for credit in sorted(process.credits, key=lambda item: item.sort_key)
                    ],
                }
            )
        devices = [
            {"cgroup_id": key[1], "dev": key[2], "rbytes": value[0], "wbytes": value[1]}
            for key, value in sorted(self.device_baselines, key=lambda item: (item[0][1], item[0][2].encode("utf-8")))
        ]
        return {
            "version": 1,
            "boot_id": self.boot_id,
            "source": self.source,
            "epoch": self.epoch,
            "last_seq": self.last_seq,
            "checkpoint_mono_ns": self.checkpoint_mono_ns,
            "processes": processes,
            "devices": devices,
        }


def _identity_json(identity: ProcessIdentity | None) -> dict[str, int] | None:
    if identity is None:
        return None
    return {"pid": identity.pid, "start_ticks": identity.start_ticks}


def _baseline_json(value: CounterVector | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {name: value[index] for index, name in enumerate(_COUNTER_NAMES)}


def _state_process_map(state: SamplerState | None) -> dict[ProcessIdentity, ProcessState]:
    if state is None:
        return {}
    result: dict[ProcessIdentity, ProcessState] = {}
    for process in state.processes:
        if process.identity in result:
            raise ValueError(f"duplicate state identity {process.identity}")
        result[process.identity] = process
    return result


def _process_by_pid(processes: Mapping[ProcessIdentity, ProcessState]) -> dict[int, ProcessState]:
    result: dict[int, ProcessState] = {}
    for process in processes.values():
        prior = result.get(process.identity.pid)
        if prior is not None and prior.identity != process.identity:
            raise ValueError(f"state has two identities for PID {process.identity.pid}")
        result[process.identity.pid] = process
    return result


def _record_app(record: StableProcRecord) -> str:
    return resolve_app(
        record.app_key_inputs.cgroup_path,
        record.app_key_inputs.exe,
        record.app_key_inputs.desktop_id,
    )


def _make_suspension_set(boundaries: tuple[tuple[int, int], ...] | None) -> SuspensionSet | None:
    if boundaries is None:
        return None
    result = SuspensionSet()
    for start, end in boundaries:
        result.add(start, end)
    return result


def _parent_identity(
    record: StableProcRecord,
    current_by_pid: Mapping[int, StableProcRecord],
    old_by_pid: Mapping[int, ProcessState],
) -> ProcessIdentity | None:
    if record.ppid in (0, 1):
        return None
    current_parent = current_by_pid.get(record.ppid)
    if current_parent is not None and current_parent.io_outcome.status in ("readable", "eacces"):
        return current_parent.identity
    old_parent = old_by_pid.get(record.ppid)
    if old_parent is not None:
        return old_parent.identity
    return None


def _add_credit(
    pending: dict[ProcessIdentity, list[ReapCredit]],
    target: ProcessIdentity | None,
    child: ProcessState,
    tick: TickInput,
) -> None:
    if target is None or child.baseline is None:
        return
    pending.setdefault(target, []).append(
        ReapCredit(
            child=child.identity,
            disappearance_epoch=tick.epoch,
            disappearance_seq=tick.seq,
            remaining=child.baseline,
        )
    )


def _merge_credit_lists(
    old: Iterable[ReapCredit], new: Iterable[ReapCredit]
) -> tuple[ReapCredit, ...]:
    combined = list(old) + list(new)
    combined.sort(key=lambda item: item.sort_key)
    seen: set[tuple[ProcessIdentity, int, int]] = set()
    for credit in combined:
        key = (credit.child, credit.disappearance_epoch, credit.disappearance_seq)
        if key in seen:
            raise ValueError(f"duplicate reap credit {key}")
        seen.add(key)
    return tuple(combined)


def _reduce_delta(
    app_key: str,
    launch_key: LaunchIdentity,
    counters: CounterVector,
    evidence: IntervalEvidence,
    observed_mono_ns: int,
    delta_kind: Literal["normal", "baseline", "reset"],
    first_seen_partial: bool,
) -> AppDelta:
    return AppDelta(
        app_key=app_key,
        launch_key=launch_key,
        counters=counters,
        attributed_ts_mono_ns=evidence.attributed_ts_mono_ns,
        observed_mono_ns=observed_mono_ns,
        interval_start_mono_ns=evidence.interval_start_mono_ns,
        observed_ns=evidence.observed_ns,
        delta_kind=delta_kind,
        first_seen_partial=first_seen_partial,
        spans_suspend=evidence.spans_suspend,
    )


def _device_state_for_app(
    app_key: str,
    observations: tuple[CgroupObservation, ...],
    owners_by_cgroup: Mapping[int, tuple[str, ...]],
) -> str:
    states = [
        observation.device_state
        for observation in observations
        if app_key in owners_by_cgroup.get(observation.cgroup_id, ())
    ]
    if not states:
        return "incomplete"
    if any(state == "incomplete" for state in states):
        return "incomplete"
    if all(state == "not_delegated" for state in states):
        return "not_delegated"
    if all(state == "available" for state in states):
        return "available"
    return "incomplete"


def aggregate_tick(
    previous_state: SamplerState | None,
    tick: TickInput,
) -> tuple[AggregatedFrame, SamplerState]:
    """Compute one frame from a private copy of ``previous_state``.

    The function does not mutate ``previous_state``. A caller publishes the
    returned state only after its durable frame transaction commits.
    """

    if not isinstance(tick, TickInput):
        raise ValueError("aggregate_tick requires a TickInput")
    if previous_state is not None and not isinstance(previous_state, SamplerState):
        raise ValueError("previous_state must be SamplerState or None")

    epoch_changed = False
    if previous_state is None:
        if tick.seq != 0:
            raise StaleFrameError("the first frame in an epoch must have seq=0")
    else:
        if tick.boot_id != previous_state.boot_id or tick.source != previous_state.source:
            raise ValueError("tick stream identity differs from sampler state")
        if tick.epoch < previous_state.epoch:
            raise StaleFrameError("frame epoch is older than the committed epoch")
        if tick.epoch > previous_state.epoch and tick.seq != 0:
            raise StaleFrameError("the first frame in an epoch must have seq=0")
        if tick.epoch == previous_state.epoch and tick.seq <= previous_state.last_seq:
            raise StaleFrameError("duplicate or stale frame sequence")
        epoch_changed = tick.epoch > previous_state.epoch

    old_processes = _state_process_map(previous_state)
    rate_unknown = tick.known_uncovered is None or (epoch_changed and not tick.known_uncovered)
    if tick.known_uncovered or rate_unknown:
        # Retain gap evidence across EACCES/unstable reads until the next readable
        # baseline. A restart with no measured bounds cannot define a rate.
        old_processes = {
            identity: replace(
                process,
                rate_uncovered_intervals=process.rate_uncovered_intervals + (tick.known_uncovered or ()),
                rate_unknown=process.rate_unknown or rate_unknown,
            )
            for identity, process in old_processes.items()
        }
    if epoch_changed:
        # The exact v1 checkpoint has no reset/conservation provenance. Every
        # restored identity can create a future credit, including one that closes
        # in this frame, so retain uncertainty before processing disappearances.
        old_processes = {
            identity: replace(process, reap_incomplete=True)
            for identity, process in old_processes.items()
        }
    old_by_pid = _process_by_pid(old_processes)
    current_records: dict[int, StableProcRecord] = {}
    for record in tick.processes:
        pid = record.identity.pid
        if pid in current_records:
            raise ValueError(f"tick enumerates PID {pid} more than once")
        current_records[pid] = record

    eligible_records = tuple(
        record
        for record in tick.processes
        if record.io_outcome.status in ("readable", "eacces")
    )
    launch_unresolved_pids = set(tick.unresolved_pids)
    launch_unresolved_pids.update(
        record.identity.pid
        for record in tick.processes
        if record.io_outcome.status == "error" and record.identity in old_processes
    )
    assignments, launch_states = assign_launches(
        eligible_records,
        previous_state.launches if previous_state is not None else (),
        unresolved_pids=tuple(sorted(launch_unresolved_pids)),
    )

    # A staged map contains only identities that remain open after the frame.
    staged: dict[ProcessIdentity, ProcessState] = {}
    pending_credits: dict[ProcessIdentity, list[ReapCredit]] = defaultdict(list)
    unapplied_by_app: dict[str, CounterVector] = defaultdict(_zero)
    reset_by_app: dict[str, int] = defaultdict(int)
    inaccessible_by_app: dict[str, int] = defaultdict(int)
    partial_by_app: set[str] = set()
    spans_by_app: set[str] = set()
    reap_corrections_by_app: dict[str, int] = defaultdict(int)
    incomplete_by_app: set[str] = set()
    incomplete_identities: set[ProcessIdentity] = set()

    for process in old_processes.values():
        if process.identity.pid in tick.unresolved_pids:
            staged[process.identity] = process
    for record in tick.processes:
        if record.io_outcome.status == "error":
            previous = old_processes.get(record.identity)
            if previous is not None:
                staged[record.identity] = previous

    closed_identities: set[ProcessIdentity] = set()

    def close_identity(old: ProcessState, *, esrch: bool) -> None:
        if old.identity in closed_identities:
            return
        closed_identities.add(old.identity)
        if esrch:
            reset_by_app[old.app_key] += 1
        target = old.ppid_identity
        target_record = next(
            (record for record in eligible_records if record.identity == target), None
        )
        target_state = old_processes.get(target) if target is not None else None
        target_launch = assignments.get(target) if target is not None else None
        if target_launch is None and target_state is not None:
            target_launch = target_state.launch_identity
        target_has_parent = False
        if target_state is not None:
            target_has_parent = (
                target_state.ppid_identity is not None
                or target_state.launch_identity.root is None
            )
        elif target_record is not None:
            target_has_parent = (
                _parent_identity(target_record, current_records, old_by_pid) is not None
                or (target_launch is not None and target_launch.root is None)
            )
        nested_reap = old.baseline is not None and target is not None and target_has_parent
        if old.credits:
            incomplete_by_app.add(old.app_key)
        if old.credits or old.had_reset or old.reap_incomplete or nested_reap:
            if target is not None:
                incomplete_identities.add(target)
            incomplete_by_app.add(
                _record_app(target_record)
                if target_record is not None
                else target_state.app_key
                if target_state is not None
                else old.app_key
            )
        _add_credit(pending_credits, target, old, tick)

    for record in sorted(tick.processes, key=lambda item: item.identity.pid):
        old = old_by_pid.get(record.identity.pid)
        if old is not None and old.identity != record.identity:
            close_identity(old, esrch=False)
    for record in tick.processes:
        if record.io_outcome.status == "esrch":
            old = old_processes.get(record.identity)
            if old is not None:
                close_identity(old, esrch=True)
    for pid in tick.vanished_pids:
        old = old_by_pid.get(pid)
        if old is not None:
            close_identity(old, esrch=True)

    # Complete enumeration closes identities that did not appear. An explicit
    # unresolved read is retained and does not count as a disappearance.
    for identity, old in old_processes.items():
        if identity in closed_identities or identity in staged:
            continue
        if identity.pid in tick.unresolved_pids:
            continue
        record = current_records.get(identity.pid)
        if record is not None and record.identity == identity:
            if record.io_outcome.status == "error":
                continue
            if record.io_outcome.status in ("readable", "eacces"):
                continue
        close_identity(old, esrch=False)

    # Resolve current membership and calculate raw deltas. Device/state metadata
    # is retained for EACCES, while only readable records advance baselines.
    suspension_set = _make_suspension_set(tick.suspend_boundaries)
    raw_deltas: dict[ProcessIdentity, tuple[CounterVector, IntervalEvidence, str, bool]] = {}
    for record in sorted(eligible_records, key=lambda item: item.identity.pid):
        identity = record.identity
        app_key = _record_app(record)
        launch_key = assignments[identity]
        previous = old_processes.get(identity)
        if previous is not None and previous.reap_incomplete:
            incomplete_by_app.add(app_key)
        parent_identity = _parent_identity(record, current_records, old_by_pid)
        credits = list(previous.credits if previous is not None else ())
        credits.extend(pending_credits.pop(identity, ()))
        merged_credits = _merge_credit_lists((), credits)
        if merged_credits and (parent_identity is not None or launch_key.root is None):
            incomplete_identities.add(identity)
            incomplete_by_app.add(app_key)

        if record.io_outcome.status == "eacces":
            inaccessible_by_app[app_key] += 1
            if previous is not None:
                staged[identity] = ProcessState(
                    identity=identity,
                    ppid_identity=parent_identity or previous.ppid_identity,
                    app_key=app_key,
                    launch_identity=launch_key,
                    baseline=previous.baseline,
                    credits=merged_credits,
                    last_observed_mono_ns=previous.last_observed_mono_ns,
                    had_reset=previous.had_reset,
                    reap_incomplete=previous.reap_incomplete or identity in incomplete_identities,
                    rate_uncovered_intervals=previous.rate_uncovered_intervals,
                    rate_unknown=previous.rate_unknown,
                )
            else:
                staged[identity] = ProcessState(
                    identity=identity,
                    ppid_identity=parent_identity,
                    app_key=app_key,
                    launch_identity=launch_key,
                    baseline=None,
                    credits=merged_credits,
                )
            continue
        if record.io_outcome.status != "readable":
            continue
        counters = _vector(record.io_outcome.counters, "io_outcome.counters")
        previous_baseline = previous.baseline if previous is not None else None
        if previous_baseline is None:
            delta = counters
            reset = False
            delta_kind: Literal["normal", "baseline", "reset"] = "baseline"
            first_partial = any(counters)
        else:
            changed = [counters[index] < previous_baseline[index] for index in range(4)]
            delta = tuple(
                0 if changed[index] else counters[index] - previous_baseline[index]
                for index in range(4)
            )  # type: ignore[assignment]
            reset = any(changed)
            delta_kind = "reset" if reset else "normal"
            first_partial = False
            if reset:
                reset_by_app[app_key] += 1
                if merged_credits:
                    incomplete_identities.add(identity)
                    incomplete_by_app.add(app_key)
        evidence = attribute_interval(
            previous.last_observed_mono_ns if previous is not None else None,
            tick.observed_mono_ns,
            suspension_set,
            known_uncovered=previous.rate_uncovered_intervals if previous is not None else (),
        )
        if previous is not None and previous.rate_unknown:
            evidence = replace(evidence, observed_ns=None)
        staged[identity] = ProcessState(
            identity=identity,
            ppid_identity=parent_identity,
            app_key=app_key,
            launch_identity=launch_key,
            baseline=counters,
            credits=merged_credits,
            last_observed_mono_ns=tick.observed_mono_ns,
            had_reset=(previous.had_reset if previous is not None else False) or reset,
            reap_incomplete=(previous.reap_incomplete if previous is not None else False)
            or identity in incomplete_identities,
        )
        raw_deltas[identity] = (delta, evidence, delta_kind, first_partial)
        if first_partial:
            partial_by_app.add(app_key)
        if evidence.spans_suspend:
            spans_by_app.add(app_key)

    # A current parent consumes its queued credits after its raw delta is known.
    adjusted: dict[ProcessIdentity, tuple[CounterVector, IntervalEvidence, str, bool]] = {}
    for identity, (delta, evidence, kind, first_partial) in raw_deltas.items():
        process = staged[identity]
        remaining_delta = list(delta)
        credits = list(process.credits)
        updated_credits: list[ReapCredit] = []
        remaining_credit = [list(credit.remaining) for credit in sorted(credits, key=lambda item: item.sort_key)]
        ordered_credits = sorted(credits, key=lambda item: item.sort_key)
        applied_by_credit = [False] * len(ordered_credits)
        for index in range(4):
            for credit_index in range(len(ordered_credits)):
                applied = min(remaining_credit[credit_index][index], remaining_delta[index])
                if applied:
                    remaining_delta[index] -= applied
                    remaining_credit[credit_index][index] -= applied
                    applied_by_credit[credit_index] = True
        updated_credits: list[ReapCredit] = []
        for credit_index, credit in enumerate(ordered_credits):
            counted = credit.correction_counted
            if applied_by_credit[credit_index] and not counted:
                counted = True
                reap_corrections_by_app[process.app_key] += 1
            if any(remaining_credit[credit_index]):
                updated_credits.append(
                    ReapCredit(
                        child=credit.child,
                        disappearance_epoch=credit.disappearance_epoch,
                        disappearance_seq=credit.disappearance_seq,
                        remaining=tuple(remaining_credit[credit_index]),  # type: ignore[arg-type]
                        correction_counted=counted,
                    )
                )
        staged[identity] = ProcessState(
            identity=process.identity,
            ppid_identity=process.ppid_identity,
            app_key=process.app_key,
            launch_identity=process.launch_identity,
            baseline=process.baseline,
            credits=tuple(updated_credits),
            last_observed_mono_ns=process.last_observed_mono_ns,
            had_reset=process.had_reset,
            reap_incomplete=process.reap_incomplete or identity in incomplete_identities,
        )
        adjusted[identity] = (tuple(remaining_delta), evidence, kind, first_partial)  # type: ignore[arg-type]

    # Credits that cannot target a current readable parent are unapplied. A
    # parent with EACCES remains open, so its credit stays on its ProcessState.
    for target, credits in pending_credits.items():
        target_process = staged.get(target)
        if target_process is not None:
            merged = _merge_credit_lists(target_process.credits, credits)
            staged[target] = ProcessState(
                identity=target_process.identity,
                ppid_identity=target_process.ppid_identity,
                app_key=target_process.app_key,
                launch_identity=target_process.launch_identity,
                baseline=target_process.baseline,
                credits=merged,
                last_observed_mono_ns=target_process.last_observed_mono_ns,
                had_reset=target_process.had_reset,
                reap_incomplete=target_process.reap_incomplete,
                rate_uncovered_intervals=target_process.rate_uncovered_intervals,
                rate_unknown=target_process.rate_unknown,
            )
        else:
            # Prefer the exact parent's app while its closed state is available;
            # otherwise the child is the only surviving attribution evidence.
            old_target = old_processes.get(target)
            for credit in credits:
                attribution = old_target
                if attribution is None:
                    attribution = old_processes.get(credit.child)
                if attribution is None:
                    raise ValueError(f"cannot attribute unapplied reap credit {credit.child}")
                unapplied_by_app[attribution.app_key] = _sum_vectors(
                    unapplied_by_app[attribution.app_key], credit.remaining
                )  # type: ignore[assignment]

    # Any credits remaining on a closed parent are disclosed under that parent's
    # app and are never folded into an app byte delta.
    current_identities = set(staged)
    for identity, old in old_processes.items():
        if identity in current_identities:
            continue
        for credit in old.credits:
            unapplied_by_app[old.app_key] = _sum_vectors(
                unapplied_by_app[old.app_key], credit.remaining
            )  # type: ignore[assignment]

    for identity in incomplete_identities:
        process = staged.get(identity)
        if process is not None and not process.reap_incomplete:
            staged[identity] = replace(process, reap_incomplete=True)

    # Materialize distinct attributed timestamps without duplicating frame-level
    # coverage. Contributions with differing interval evidence conservatively lose
    # rate evidence but retain their byte deltas.
    grouped: dict[tuple[str, LaunchIdentity, int], AppDelta] = {}
    for identity, (delta, evidence, kind, first_partial) in adjusted.items():
        process = staged[identity]
        if delta == _zero() and kind == "normal" and not evidence.spans_suspend:
            # Zero normal rows are retained: a readable identity is still a
            # process observation, and the store may need its attributed timestamp.
            pass
        item = _reduce_delta(
            process.app_key,
            process.launch_identity,
            delta,
            evidence,
            tick.observed_mono_ns,
            kind,
            first_partial,
        )
        key = (item.app_key, item.launch_key, item.attributed_ts_mono_ns)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = item
        else:
            same_interval = (
                existing.interval_start_mono_ns == item.interval_start_mono_ns
                and existing.observed_ns == item.observed_ns
            )
            grouped[key] = AppDelta(
                app_key=existing.app_key,
                launch_key=existing.launch_key,
                counters=_sum_vectors(existing.counters, item.counters),
                attributed_ts_mono_ns=existing.attributed_ts_mono_ns,
                observed_mono_ns=tick.observed_mono_ns,
                interval_start_mono_ns=existing.interval_start_mono_ns if same_interval else None,
                observed_ns=existing.observed_ns if same_interval else None,
                delta_kind=_or_delta_kind(existing.delta_kind, item.delta_kind),
                first_seen_partial=existing.first_seen_partial or item.first_seen_partial,
                spans_suspend=existing.spans_suspend or item.spans_suspend,
            )

    # Build app coverage from all current identities plus apps that need to
    # disclose unapplied credit. Cgroup state is computed independently of T.
    apps = (
        set(process.app_key for process in old_processes.values())
        | set(process.app_key for process in staged.values())
        | set(unapplied_by_app)
        | set(reset_by_app)
        | set(incomplete_by_app)
        | partial_by_app
        | spans_by_app
    )
    owners_by_cgroup: dict[int, tuple[str, ...]] = {}
    for observation in tick.cgroups:
        owners = sorted(
            {
                _record_app(record)
                for record in eligible_records
                if record.cgroup_path == observation.cgroup_path
            },
            key=lambda value: value.encode("utf-8"),
        )
        owners_by_cgroup[observation.cgroup_id] = tuple(owners)
        apps.update(owners)

    coverage_by_app: dict[str, FrameCoverage] = {}
    for app_key in sorted(apps, key=lambda value: value.encode("utf-8")):
        device_state = _device_state_for_app(app_key, tick.cgroups, owners_by_cgroup)
        coverage_by_app[app_key] = FrameCoverage(
            app_key=app_key,
            observed_mono_ns=tick.observed_mono_ns,
            inaccessible_pids=inaccessible_by_app[app_key],
            resets=reset_by_app[app_key],
            reap_corrections=reap_corrections_by_app[app_key],
            reap_unapplied=unapplied_by_app[app_key],
            partial_intervals=int(app_key in partial_by_app),
            gap_intervals=int(epoch_changed or tick.known_uncovered is None or bool(tick.known_uncovered)),
            spans_suspend_intervals=int(app_key in spans_by_app),
            spans_suspend=app_key in spans_by_app,
            reap_incomplete=app_key in incomplete_by_app,
            device_state=device_state,
        )

    # Device baselines are consumed once per cgroup/device, then assigned only
    # to a unique app owner. Shared distinct owners receive incomplete coverage.
    baseline_map = dict(previous_state.device_baselines if previous_state is not None else ())
    deltas = device_deltas(tick.boot_id, tick.cgroups, baseline_map)
    device_results: list[AggregatedDeviceDelta] = []
    for observation in tick.cgroups:
        owners = owners_by_cgroup.get(observation.cgroup_id, ())
        if len(owners) > 1:
            for owner in owners:
                existing = coverage_by_app[owner]
                coverage_by_app[owner] = FrameCoverage(
                    **{**existing.__dict__, "device_state": "incomplete"}
                )
            continue
        if len(owners) != 1:
            continue
        owner = owners[0]
        for delta in deltas:
            if delta.cgroup_id == observation.cgroup_id:
                device_results.append(AggregatedDeviceDelta(owner, delta))

    next_state = SamplerState(
        boot_id=tick.boot_id,
        source=tick.source,
        epoch=tick.epoch,
        last_seq=tick.seq,
        checkpoint_mono_ns=tick.observed_mono_ns,
        processes=tuple(sorted(staged.values(), key=lambda item: (item.identity.pid, item.identity.start_ticks))),
        launches=launch_states,
        device_baselines=tuple(sorted(baseline_map.items(), key=lambda item: (item[0][1], item[0][2].encode("utf-8")))),
        last_observed_mono_ns=tick.observed_mono_ns,
    )
    frame = AggregatedFrame(
        boot_id=tick.boot_id,
        source=tick.source,
        epoch=tick.epoch,
        seq=tick.seq,
        observed_mono_ns=tick.observed_mono_ns,
        clock_offset_ns=tick.wall_ns - tick.observed_mono_ns,
        deltas=tuple(
            sorted(
                grouped.values(),
                key=lambda item: (
                    item.app_key.encode("utf-8"),
                    item.launch_key.root is None,
                    item.launch_key.root.pid if item.launch_key.root is not None else 0,
                    item.launch_key.root.start_ticks if item.launch_key.root is not None else 0,
                    item.attributed_ts_mono_ns,
                ),
            )
        ),
        coverage=tuple(coverage_by_app[key] for key in sorted(coverage_by_app, key=lambda value: value.encode("utf-8"))),
        device_deltas=tuple(
            sorted(
                device_results,
                key=lambda item: (
                    item.app_key.encode("utf-8"),
                    item.delta.cgroup_id,
                    item.delta.dev.encode("utf-8"),
                ),
            )
        ),
        checkpoint_state=next_state.to_checkpoint(),
    )
    return frame, next_state
