"""Stable launch grouping for validated process observations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .identity import resolve_app
from .procs import ProcessIdentity, StableProcRecord

__all__ = ["LaunchIdentity", "LaunchState", "ProcessState", "ReapCredit", "assign_launches"]


@dataclass(frozen=True)
class LaunchIdentity:
    """A launch is rooted in one stable process identity and one exact AppKey."""

    root: ProcessIdentity | None
    app_key: str

    @property
    def launch_id(self) -> int:
        """Local launch IDs belong to the store; zero means unresolved here."""

        return 0


@dataclass(frozen=True)
class ReapCredit:
    """Cumulative child counters owed against the last observed parent identity."""

    child: ProcessIdentity
    disappearance_epoch: int
    disappearance_seq: int
    remaining: tuple[int, int, int, int]
    correction_counted: bool = False

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        return (
            self.disappearance_epoch,
            self.disappearance_seq,
            self.child.pid,
            self.child.start_ticks,
        )


@dataclass(frozen=True)
class ProcessState:
    """Open sampler state for one stable identity."""

    identity: ProcessIdentity
    ppid_identity: ProcessIdentity | None
    app_key: str
    launch_identity: LaunchIdentity
    baseline: tuple[int, int, int, int] | None
    credits: tuple[ReapCredit, ...]
    open: bool = True
    last_observed_mono_ns: int | None = None
    had_reset: bool = False
    reap_incomplete: bool = False


@dataclass(frozen=True)
class LaunchState:
    """One open launch and the known live identities currently in its tree."""

    identity: LaunchIdentity
    members: tuple[ProcessIdentity, ...]
    open: bool = True


def _previous_launches(
    previous_launches: Iterable[LaunchState],
) -> tuple[LaunchState, ...]:
    states = tuple(previous_launches)
    if any(not isinstance(state, LaunchState) for state in states):
        raise ValueError("previous_launches must contain LaunchState values")
    return states


def assign_launches(
    processes: Iterable[StableProcRecord],
    previous_launches: Iterable[LaunchState] = (),
    *,
    unresolved_pids: Iterable[int] = (),
) -> tuple[dict[ProcessIdentity, LaunchIdentity], tuple[LaunchState, ...]]:
    """Assign current stable records to app-root launches.

    Same-AppKey children inherit a currently observed parent's launch. A process
    with an unobserved, non-init parent is unresolved unless it was already a
    known member of an open launch. Current records and unresolved prior members
    keep a launch open; an empty member set closes it by omission from the result.
    """

    records = tuple(processes)
    if any(not isinstance(record, StableProcRecord) for record in records):
        raise ValueError("processes must contain StableProcRecord values")
    eligible = tuple(
        record
        for record in records
        if record.io_outcome.status in ("readable", "eacces")
    )
    by_pid: dict[int, StableProcRecord] = {}
    for record in eligible:
        pid = record.identity.pid
        if pid in by_pid:
            raise ValueError(f"assign_launches received PID {pid} more than once")
        by_pid[pid] = record

    prior = _previous_launches(previous_launches)
    prior_member_launch: dict[ProcessIdentity, LaunchIdentity] = {}
    for launch in prior:
        if not launch.open:
            continue
        for member in launch.members:
            if member in prior_member_launch:
                raise ValueError(f"identity {member} belongs to more than one open launch")
            prior_member_launch[member] = launch.identity

    unresolved = set(unresolved_pids)
    if any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in unresolved):
        raise ValueError("unresolved_pids must contain positive integer PIDs")

    assignments: dict[ProcessIdentity, LaunchIdentity] = {}
    visiting: set[ProcessIdentity] = set()

    def assign(record: StableProcRecord) -> LaunchIdentity:
        identity = record.identity
        existing = assignments.get(identity)
        if existing is not None:
            return existing
        if identity in visiting:
            raise ValueError("process parent graph contains a cycle")
        visiting.add(identity)
        app_key = resolve_app(
            record.app_key_inputs.cgroup_path,
            record.app_key_inputs.exe,
            record.app_key_inputs.desktop_id,
        )
        parent = by_pid.get(record.ppid)
        if parent is not None:
            parent_key = resolve_app(
                parent.app_key_inputs.cgroup_path,
                parent.app_key_inputs.exe,
                parent.app_key_inputs.desktop_id,
            )
            if parent_key == app_key:
                launch = assign(parent)
            else:
                launch = LaunchIdentity(identity, app_key)
        elif record.ppid in (0, 1):
            launch = LaunchIdentity(identity, app_key)
        else:
            prior_launch = prior_member_launch.get(identity)
            if prior_launch is not None and prior_launch.app_key == app_key:
                launch = prior_launch
            else:
                launch = LaunchIdentity(None, app_key)
        visiting.remove(identity)
        assignments[identity] = launch
        return launch

    for record in sorted(eligible, key=lambda item: item.identity.pid):
        assign(record)

    members_by_launch: dict[LaunchIdentity, set[ProcessIdentity]] = {}
    for identity, launch in assignments.items():
        if launch.root is not None:
            members_by_launch.setdefault(launch, set()).add(identity)

    current = set(assignments)
    for launch in prior:
        if not launch.open:
            continue
        retained = {
            member
            for member in launch.members
            if member not in current and member.pid in unresolved
        }
        if retained:
            members_by_launch.setdefault(launch.identity, set()).update(retained)

    states = tuple(
        LaunchState(identity=identity, members=tuple(sorted(members, key=lambda item: (item.pid, item.start_ticks))))
        for identity, members in sorted(
            members_by_launch.items(),
            key=lambda item: (
                item[0].app_key.encode("utf-8"),
                item[0].root is None,
                item[0].root.pid if item[0].root is not None else 0,
                item[0].root.start_ticks if item[0].root is not None else 0,
            ),
        )
        if members
    )
    return assignments, states
