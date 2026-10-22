"""Internal handover evidence and pure zero-byte baseline receipts.

An offer is evidence, never authentication. The public daemon protocol does not
accept it. Version 1 omits rate/reset provenance, so restoration is conservative;
local launch surrogates are discarded in favour of stable roots and AppKeys.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

from .contracts import validate_sqlite_integer
from .durable import (
    HandoverState, SamplerCheckpointRow, canonical_json, decode_handover,
    decode_sampler_checkpoint,
)
from .launch import LaunchIdentity, LaunchState, ProcessState, ReapCredit
from .procs import ProcessIdentity
from .sampler import AggregatedFrame, FrameCoverage, SamplerState, TickInput, aggregate_tick
from .stream import FrameIdentity

__all__ = [
    "HandoverOffer", "IncompleteHandoverError", "capture_handover",
    "seed_handover", "baseline_seed",
]


class IncompleteHandoverError(ValueError):
    """Exact transfer cannot be proven; the caller must record an unclean gap."""


@dataclass(frozen=True)
class HandoverOffer:
    from_source: str
    to_source: str
    outgoing_epoch: int
    outgoing_seq: int
    interval_start_mono_ns: int
    state: HandoverState

    def __post_init__(self) -> None:
        if not isinstance(self.state, HandoverState):
            raise ValueError("handover requires a HandoverState")
        FrameIdentity(self.state.boot_id, self.from_source, self.outgoing_epoch, self.outgoing_seq)
        FrameIdentity(self.state.boot_id, self.to_source, 1, 0)
        if self.from_source == self.to_source:
            raise ValueError("handover must change source")
        validate_sqlite_integer(self.interval_start_mono_ns, "handover.interval_start_mono_ns")
        if self.interval_start_mono_ns >= self.state.cutoff_mono_ns:
            raise ValueError("handover requires a nonempty outgoing interval")
        decode_handover(canonical_json(self.state), self.state.boot_id, self.state.cutoff_mono_ns)


def capture_handover(state: SamplerState) -> HandoverState:
    """Validate a canonical checkpoint before extracting its two typed arrays."""
    if not isinstance(state, SamplerState):
        raise ValueError("capture_handover requires a SamplerState")
    for process in state.processes:
        identities = (process.identity, process.ppid_identity, process.launch_identity.root,
                      *(credit.child for credit in process.credits))
        if any(identity is not None and identity.boot_id != state.boot_id for identity in identities):
            raise ValueError("handover process identities belong to another boot")
        if process.app_key != process.launch_identity.app_key:
            raise ValueError("handover launch and process app identities disagree")
    if any(key[0] != state.boot_id for key, _ in state.device_baselines):
        raise ValueError("handover device baseline belongs to another boot")
    data = decode_sampler_checkpoint(canonical_json(state.to_checkpoint()),
        SamplerCheckpointRow(state.source, state.boot_id, state.epoch, state.last_seq, state.checkpoint_mono_ns))
    result = HandoverState(data.boot_id, data.checkpoint_mono_ns, data.processes, data.devices)
    return decode_handover(canonical_json(result), state.boot_id, state.checkpoint_mono_ns)


def seed_handover(offer: HandoverOffer, *, incoming_epoch: int, wall_ns: int) -> tuple[AggregatedFrame, SamplerState]:
    """Restore a distinct stream at seq=0, emitting no historical task/device bytes."""
    if not isinstance(offer, HandoverOffer):
        raise ValueError("seed_handover requires a HandoverOffer")
    FrameIdentity(offer.state.boot_id, offer.to_source, incoming_epoch, 0)
    validate_sqlite_integer(wall_ns, "handover.wall_ns", nonnegative=False)
    data = decode_handover(canonical_json(offer.state), offer.state.boot_id, offer.state.cutoff_mono_ns)

    def identity(ref):
        return None if ref is None else ProcessIdentity(data.boot_id, ref.pid, ref.start_ticks)

    processes = tuple(ProcessState(
        identity=ProcessIdentity(data.boot_id, item.pid, item.start_ticks),
        ppid_identity=identity(item.parent), app_key=item.app_key,
        launch_identity=LaunchIdentity(identity(item.launch_root), item.app_key),
        baseline=item.baseline,
        credits=tuple(ReapCredit(identity(credit.child), credit.disappearance_epoch,
            credit.disappearance_seq, credit.remaining, bool(credit.correction_counted)) for credit in item.credits),
        last_observed_mono_ns=data.cutoff_mono_ns if item.baseline is not None else None,
        rate_unknown=True, reap_incomplete=True,
    ) for item in data.processes)
    groups = defaultdict(list)
    for process in processes:
        if process.launch_identity.root is not None:
            groups[process.launch_identity].append(process.identity)
    launches = tuple(LaunchState(key, tuple(members)) for key, members in sorted(groups.items(),
        key=lambda item: (item[0].app_key.encode("utf-8"), item[0].root.pid, item[0].root.start_ticks)))
    state = SamplerState(data.boot_id, offer.to_source, incoming_epoch, 0, data.cutoff_mono_ns,
        processes, launches,
        tuple(((data.boot_id, item.cgroup_id, item.dev), (item.rbytes, item.wbytes)) for item in data.devices),
        data.cutoff_mono_ns)
    coverage = tuple(FrameCoverage(key, data.cutoff_mono_ns,
        inaccessible_pids=sum(item.baseline is None for item in processes if item.app_key == key),
        partial_intervals=1, reap_incomplete=True) for key in sorted({item.app_key for item in processes},
        key=lambda key: key.encode("utf-8")))
    frame = AggregatedFrame(state.boot_id, state.source, state.epoch, 0, state.checkpoint_mono_ns,
        wall_ns - state.checkpoint_mono_ns, (), coverage, (), state.to_checkpoint())
    return frame, state


def baseline_seed(tick: TickInput, *, previous: SamplerState | None = None) -> tuple[AggregatedFrame, SamplerState]:
    """Baseline a fresh authority without turning unobserved lifetime work into bytes.

    Same-authority, stable identities may retain verified credits without consuming
    them at this observation. Cross-authority credits cannot establish conservation.
    The caller records the actual unclean gap bounds separately via record_gap.
    """
    if not isinstance(tick, TickInput) or tick.seq != 0:
        raise ValueError("baseline_seed requires a seq=0 TickInput")
    if previous is not None and not isinstance(previous, SamplerState):
        raise ValueError("previous must be SamplerState or None")
    verified = {}
    if previous is not None and (previous.boot_id, previous.source) == (tick.boot_id, tick.source):
        capture_handover(previous)
        if tick.epoch <= previous.epoch or tick.observed_mono_ns < previous.checkpoint_mono_ns:
            raise ValueError("baseline seed must advance the same authority's epoch and chronology")
        verified = {item.identity: item for item in previous.processes}
    frame, state = aggregate_tick(None, tick)
    processes = []
    for item in state.processes:
        old = verified.get(item.identity)
        same = old is not None and old.app_key == item.app_key and old.launch_identity == item.launch_identity
        processes.append(replace(item, credits=old.credits if same else (),
            reap_incomplete=True, rate_unknown=tick.known_uncovered is None))
    state = replace(state, processes=tuple(processes))
    frame = replace(frame, deltas=(), device_deltas=(), checkpoint_state=state.to_checkpoint(),
        coverage=tuple(replace(item, partial_intervals=1, reap_incomplete=True) for item in frame.coverage))
    return frame, state
