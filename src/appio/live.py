"""Live process and cgroup collection for the AppIO application.

The existing process, cgroup, sampler, and store modules deliberately stop at
fixture-shaped inputs.  This module is the small adapter that supplies those
inputs from configurable roots while retaining their pure and transactional
boundaries.  Tests can therefore point it at a temporary tree and a deterministic
clock without opening the host process table or clock.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
import time
from typing import Literal

from .cgroups import CgroupObservation, read_cgroup_fixture
from .handover import (
    HandoverOffer,
    IncompleteHandoverError,
    baseline_seed,
    capture_handover,
    seed_handover,
)
from .procs import ProcReader, ProcessVanished, StableProcRecord
from .sampler import (
    AggregatedFrame,
    FrameCoverage,
    SamplerState,
    TickInput,
    aggregate_tick,
)
from .store import AppIoStore

__all__ = [
    "CoverageSummary",
    "LiveCollector",
    "SystemClock",
    "TickResult",
]

_NS = 1_000_000_000
_SOURCE = "gui"
_SOURCES = ("gui", "iod")
_ROW_TABLES = ("sample", "sample_dev", "sample_coverage")


class SystemClock:
    """The production clock used by the live collector."""

    def __call__(self) -> tuple[int, int]:
        return (
            time.clock_gettime_ns(time.CLOCK_BOOTTIME),
            time.time_ns(),
        )


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Immutable counts and device state for one committed tick."""

    inaccessible_pids: int = 0
    resets: int = 0
    reap_corrections: int = 0
    partial_intervals: int = 0
    gap_intervals: int = 0
    overlap_uncertain_intervals: int = 0
    spans_suspend_intervals: int = 0
    device_state: Literal["available", "not_delegated", "incomplete"] = "incomplete"

    @property
    def complete(self) -> bool:
        """Whether this tick has no process or device coverage caveat."""

        return (
            self.inaccessible_pids == 0
            and self.gap_intervals == 0
            and self.overlap_uncertain_intervals == 0
            and self.device_state in ("available", "not_delegated")
        )


@dataclass(frozen=True, slots=True)
class TickResult:
    """The small immutable public result returned after one committed tick."""

    apps_observed: int
    rows_written: int
    coverage: CoverageSummary

    @property
    def coverage_summary(self) -> CoverageSummary:
        """Descriptive alias for callers that prefer the full field name."""

        return self.coverage


def _require_nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "" or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string without NUL")
    return value


def _read_clock(clock: object) -> tuple[int, int]:
    """Accept a callable clock plus the common small test-clock protocols."""

    if callable(clock):
        value = clock()
    elif hasattr(clock, "now") and callable(clock.now):
        value = clock.now()
    elif hasattr(clock, "boottime_ns") and hasattr(clock, "wall_ns"):
        boottime = clock.boottime_ns
        wall = clock.wall_ns
        value = (boottime() if callable(boottime) else boottime,
                 wall() if callable(wall) else wall)
    elif hasattr(clock, "monotonic_ns") and hasattr(clock, "time_ns"):
        monotonic = clock.monotonic_ns
        wall_clock = clock.time_ns
        value = (monotonic() if callable(monotonic) else monotonic,
                 wall_clock() if callable(wall_clock) else wall_clock)
    else:
        raise ValueError(
            "clock must be callable or provide now(), boottime_ns/wall_ns, "
            "or monotonic_ns/time_ns"
        )
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("clock must return (boottime_ns, wall_ns)")
    boottime_ns, wall_ns = value
    if (
        isinstance(boottime_ns, bool)
        or not isinstance(boottime_ns, int)
        or boottime_ns < 0
        or isinstance(wall_ns, bool)
        or not isinstance(wall_ns, int)
        or wall_ns < 0
    ):
        raise ValueError("clock values must be non-negative integers")
    return boottime_ns, wall_ns


def _boot_id_from_proc_root(root: Path) -> str:
    """Read fixture and Linux procfs boot-id layouts without guessing a boot."""

    candidates = (
        root / "boot_id",
        root / "sys" / "kernel" / "random" / "boot_id",
    )
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        except OSError:
            continue
        if value:
            return _require_nonempty_text(value, "boot_id")
    raise ValueError(f"no boot_id under proc root {root}")


def _relative_cgroup_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ValueError(f"invalid absolute cgroup path {path!r}")
    parts = tuple(part for part in path.split("/") if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise ValueError(f"cgroup path escapes its root: {path!r}")
    return "/".join(parts)


def _fallback_cgroup_id(path: str) -> int:
    """Provide a stable id if a cgroup disappears before stat(2) can finish."""

    digest = hashlib.blake2b(path.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _cgroup_id(root: Path, cgroup_path: str) -> int:
    try:
        leaf = root.joinpath(*_relative_cgroup_path(cgroup_path).split("/"))
        return leaf.stat().st_ino
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return _fallback_cgroup_id(cgroup_path)


def _device_state(states: list[str]) -> Literal["available", "not_delegated", "incomplete"]:
    if not states:
        return "incomplete"
    if any(state == "incomplete" for state in states):
        return "incomplete"
    if all(state == "available" for state in states):
        return "available"
    if all(state == "not_delegated" for state in states):
        return "not_delegated"
    return "incomplete"


def _coverage_summary(coverage: tuple[FrameCoverage, ...]) -> CoverageSummary:
    if not coverage:
        return CoverageSummary()
    return CoverageSummary(
        inaccessible_pids=sum(item.inaccessible_pids for item in coverage),
        resets=sum(item.resets for item in coverage),
        reap_corrections=sum(item.reap_corrections for item in coverage),
        partial_intervals=sum(item.partial_intervals for item in coverage),
        gap_intervals=sum(item.gap_intervals for item in coverage),
        overlap_uncertain_intervals=sum(item.overlap_uncertain_intervals for item in coverage),
        spans_suspend_intervals=sum(item.spans_suspend_intervals for item in coverage),
        device_state=_device_state([item.device_state for item in coverage]),
    )


def _other_source(source: Literal["gui", "iod"]) -> Literal["gui", "iod"]:
    """The single destination a source change can hand over to."""

    return "iod" if source == "gui" else "gui"


def _mark_unmeasured(frame: AggregatedFrame) -> AggregatedFrame:
    """Flag every app in a frame whose window no collector measured.

    The stage keeps the computed bookkeeping; only the coverage caveat changes,
    so a query cannot read this frame as complete coverage.
    """

    return replace(
        frame,
        coverage=tuple(
            replace(
                item,
                partial_intervals=max(item.partial_intervals, 1),
                reap_incomplete=True,
                gap_intervals=max(item.gap_intervals, 1),
            )
            for item in frame.coverage
        ),
    )


class LiveCollector:
    """Own one live process stream and commit one complete frame per tick.

    ``proc_root``, ``cgroup_root``, and ``database_path`` are all explicit so a
    caller can use the same object with a fixture tree.  ``clock`` returns a
    boottime and wall-clock pair; a callable is the simplest injectable form.

    ``source`` selects the stream this collector owns.  ``baseline_only`` starts
    a fresh authority: the epoch is allocated at the first sampled baseline and
    that sample emits no bytes, so counters that accumulated before this
    collector started never become measured io.  ``resume_handover`` installs a
    validated outgoing cutoff and continues the incoming stream from sequence
    zero; an offer whose ownership chain cannot be proven degrades to the same
    conservative baseline instead of installing a misleading handover row.
    """

    def __init__(
        self,
        proc_root: str | Path = "/proc",
        cgroup_root: str | Path = "/sys/fs/cgroup",
        database_path: str | Path = "history.db",
        clock: object | None = None,
        *,
        source: Literal["gui", "iod"] = _SOURCE,
        baseline_only: bool = False,
        resume_handover: HandoverOffer | None = None,
    ) -> None:
        if source not in _SOURCES:
            raise ValueError("LiveCollector accepts only the gui and iod source tokens")
        if not isinstance(baseline_only, bool):
            raise ValueError("baseline_only must be a boolean")
        if resume_handover is not None:
            if not isinstance(resume_handover, HandoverOffer):
                raise ValueError("resume_handover must be a HandoverOffer or None")
            if resume_handover.to_source != source:
                raise ValueError(
                    "resume_handover must name this collector's source as its destination"
                )
            if baseline_only:
                raise ValueError("resume_handover already establishes the incoming baseline")
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock: object = SystemClock() if clock is None else clock
        self.source = source
        self.baseline_only = baseline_only
        self.boot_id = _boot_id_from_proc_root(self.proc_root)
        initial_mono_ns, initial_wall_ns = _read_clock(self.clock)

        self.store = AppIoStore(self.database_path)
        boot = self.store.connection.execute(
            "SELECT boot_order,started_mono_ns,started_wall,source FROM boot WHERE boot_id=?",
            (self.boot_id,),
        ).fetchone()
        if boot is None:
            boot_order = self.store.connection.execute(
                "SELECT coalesce(max(boot_order),0)+1 FROM boot"
            ).fetchone()[0]
            self.store.start_boot(
                self.boot_id,
                boot_order,
                initial_mono_ns,
                initial_wall_ns / _NS,
                self.source,
            )
        # A boot row records the source that created it and stays immutable, so
        # an imported root-origin export keeps ``boot.source`` exactly as it is.
        # Stream ownership lives in collector_epoch, collector_interval and the
        # sampler checkpoint, each keyed by this collector's own source.

        self._state: SamplerState | None = None
        self._last_frame: AggregatedFrame | None = None
        self._last_result: TickResult | None = None
        self._last_observed_mono_ns: int | None = None
        self._next_seq = 0
        self._epoch: int | None = None
        self._deferred_baseline = False
        self._pending_gap_from: int | None = None
        self._offer: HandoverOffer | None = None
        self._handover_fallback_reason: str | None = None
        self._quiesced = False
        self._closed = False

        if resume_handover is not None:
            self._install_incoming(resume_handover, initial_wall_ns)
        elif baseline_only:
            # Allocation waits for the sampled baseline so the epoch, the
            # baseline and the first interval all share one observation.
            self._deferred_baseline = True
        else:
            epoch = self.store.start_collector(self.source, self.boot_id, initial_mono_ns)
            self._epoch = epoch.epoch
            self._next_seq = 0
            self._state = self.store.restore_checkpoint(self.source, self.boot_id)

    def _install_incoming(self, offer: HandoverOffer, wall_ns: int) -> None:
        """Install a proven cutoff, or degrade to a conservative fresh baseline.

        A store rejection means the offer contradicts durable evidence: the
        cutoff receipt is gone, the ownership chain has a hole, another owner
        overlaps the interval, or the cutoff is behind evidence this source
        already committed. Every one of those is an unclean transfer, so the
        collector takes a fresh baseline and records the unmeasured absence.
        The reason stays readable rather than silent.
        """

        try:
            epoch = self.store.install_handover(offer, wall_ns=wall_ns)
        except (IncompleteHandoverError, ValueError) as error:
            self._handover_fallback_reason = f"{type(error).__name__}: {error}"
            self._deferred_baseline = True
            self._pending_gap_from = offer.state.cutoff_mono_ns
            return
        self._epoch = epoch.epoch
        self._state = self.store.restore_checkpoint(self.source, self.boot_id)
        self._next_seq = 0 if epoch.last_seq is None else epoch.last_seq + 1
        # Re-derive the seed from the durable checkpoint. The cutoff, baselines,
        # deltas and coverage are the committed ones, and the wall-clock offset
        # is the persisted mapping for that observation, so a repeated
        # installation yields exactly the frame the store committed.
        cutoff = offer.state.cutoff_mono_ns
        self._last_frame = seed_handover(
            offer,
            incoming_epoch=epoch.epoch,
            wall_ns=self._committed_wall_ns(cutoff, wall_ns),
        )[0]
        self._last_observed_mono_ns = self._last_frame.observed_mono_ns
        self._pending_gap_from = cutoff

    def _committed_wall_ns(self, observed_mono_ns: int, fallback_wall_ns: int) -> int:
        """The wall reading this observation maps to in the persisted clock map."""

        mapping = self.store.connection.execute(
            "SELECT wall_offset_ns FROM clock_map WHERE boot_id=? AND segment_start_mono_ns<=? "
            "ORDER BY segment_start_mono_ns DESC LIMIT 1",
            (self.boot_id, observed_mono_ns),
        ).fetchone()
        return fallback_wall_ns if mapping is None else observed_mono_ns + mapping[0]

    @property
    def sampler_state(self) -> SamplerState | None:
        return self._state

    @property
    def handover_fallback_reason(self) -> str | None:
        """Why an incoming offer degraded to a fresh baseline, if it did."""

        return self._handover_fallback_reason

    @property
    def last_frame(self) -> AggregatedFrame | None:
        """The last frame this collector's stream committed, including a seed."""

        return self._last_frame

    @property
    def last_result(self) -> TickResult | None:
        return self._last_result

    @property
    def last_observed_mono_ns(self) -> int | None:
        return self._last_observed_mono_ns

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def epoch(self) -> int | None:
        """The active epoch, or ``None`` until a deferred baseline is sampled."""

        return self._epoch

    def _read_processes(
        self, reader: ProcReader
    ) -> tuple[tuple[StableProcRecord, ...], tuple[int, ...], tuple[int, ...]]:
        pids = reader.enumerate_pids()
        records: list[StableProcRecord] = []
        unresolved: list[int] = []
        vanished: list[int] = []
        for pid in pids:
            try:
                record = reader.read_stable(pid)
            except ProcessVanished as error:
                vanished.append(error.pid)
                continue
            except (OSError, ValueError):
                unresolved.append(pid)
                continue
            if record is None:
                unresolved.append(pid)
            else:
                records.append(record)
        return (
            tuple(sorted(records, key=lambda item: item.identity.pid)),
            tuple(sorted(set(unresolved))),
            tuple(sorted(set(vanished))),
        )

    def _read_cgroups(self, records: tuple[StableProcRecord, ...]) -> tuple[CgroupObservation, ...]:
        paths = sorted(
            {record.cgroup_path for record in records if record.cgroup_path is not None},
            key=lambda value: value.encode("utf-8"),
        )
        observations: list[CgroupObservation] = []
        for path in paths:
            assert path is not None
            cgroup_id = _cgroup_id(self.cgroup_root, path)
            try:
                observation = read_cgroup_fixture(
                    self.cgroup_root,
                    _relative_cgroup_path(path),
                    self.boot_id,
                    cgroup_id,
                )
            except (FileNotFoundError, NotADirectoryError, PermissionError, OSError, ValueError):
                observation = CgroupObservation(
                    self.boot_id,
                    cgroup_id,
                    path,
                    "incomplete",
                    (),
                )
            observations.append(observation)
        return tuple(observations)

    def _row_counts(self) -> tuple[int, ...]:
        return tuple(
            self.store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in _ROW_TABLES
        )

    def _gap_floor(self) -> int:
        """The latest boundary this source already accounts for durably."""

        row = self.store.connection.execute(
            "SELECT max(value) FROM ("
            " SELECT checkpoint_mono_ns AS value FROM sampler_checkpoint"
            "  WHERE source=? AND boot_id=?"
            " UNION ALL SELECT to_mono_ns FROM gap WHERE source=? AND boot_id=?)",
            (self.source, self.boot_id, self.source, self.boot_id),
        ).fetchone()
        return 0 if row is None or row[0] is None else row[0]

    def _record_handover_gap(self, epoch: int, seq: int, observed_mono_ns: int) -> None:
        """Record the absence between an installed cutoff and the first sample.

        ``start_collector`` already closes an interval inherited from this
        source, so only the part no durable evidence covers is added here. That
        keeps a repeated installation from recording the same absence twice.
        The gap commits before the frame, so no frame receipt can claim coverage
        that the gap contradicts.
        """

        start = self._pending_gap_from
        if start is None:
            return
        floor = max(start, self._gap_floor())
        if floor < observed_mono_ns:
            self.store.record_gap(
                self.boot_id, self.source, epoch, seq, floor, observed_mono_ns, "collector_gap"
            )

    def _gap_intersects(self, start_mono_ns: int, end_mono_ns: int) -> bool:
        return self.store.connection.execute(
            "SELECT 1 FROM gap WHERE boot_id=? AND source=? "
            "AND from_mono_ns<? AND to_mono_ns>? LIMIT 1",
            (self.boot_id, self.source, end_mono_ns, start_mono_ns),
        ).fetchone() is not None

    def _cross_gap_frame(self, tick: TickInput) -> tuple[AggregatedFrame, SamplerState]:
        """Advance bookkeeping across an installed cutoff without emitting bytes.

        The restored baseline sits at the cutoff, and a cumulative delta cannot
        be split across the absence that follows it without inventing a rate and
        a byte count. The bookkeeping still advances, so the next sample is
        measured from this one.
        """

        frame, next_state = aggregate_tick(self._state, tick)
        return replace(frame, deltas=(), device_deltas=()), next_state

    def tick(self) -> TickResult:
        """Read the host once, aggregate it, and commit one sampler frame."""

        if self._closed:
            raise RuntimeError("LiveCollector is closed")
        if self._quiesced:
            raise RuntimeError("LiveCollector is quiesced; its cutoff offer is final")
        observed_mono_ns, wall_ns = _read_clock(self.clock)
        epoch = self._epoch
        previous: SamplerState | None = None
        if self._deferred_baseline:
            # One timestamp pair serves the allocation, the baseline and the
            # frame. Same-boot evidence is restored for its credits only.
            previous = self.store.restore_checkpoint(self.source, self.boot_id)
            epoch = self.store.start_collector(self.source, self.boot_id, observed_mono_ns).epoch
        assert epoch is not None
        reader = ProcReader(self.proc_root, boot_id=self.boot_id)
        records, unresolved, vanished = self._read_processes(reader)
        cgroups = self._read_cgroups(records)
        tick = TickInput(
            boot_id=self.boot_id,
            source=self.source,
            epoch=epoch,
            seq=self._next_seq,
            observed_mono_ns=observed_mono_ns,
            wall_ns=wall_ns,
            processes=records,
            cgroups=cgroups,
            unresolved_pids=unresolved,
            vanished_pids=vanished,
        )
        # An installed cutoff is only a cross-gap start when the sample comes
        # after it; a sample at the cutoff itself is an ordinary first frame.
        cross_gap = (
            self._pending_gap_from is not None and self._pending_gap_from < observed_mono_ns
        )
        if self._deferred_baseline:
            frame, next_state = baseline_seed(tick, previous=previous)
        elif cross_gap:
            frame, next_state = self._cross_gap_frame(tick)
        else:
            frame, next_state = aggregate_tick(self._state, tick)
        if cross_gap:
            frame = _mark_unmeasured(frame)
            self._record_handover_gap(epoch, frame.seq, observed_mono_ns)
        elif self._deferred_baseline and previous is not None and self._gap_intersects(
            previous.checkpoint_mono_ns, observed_mono_ns
        ):
            frame = _mark_unmeasured(frame)
        before = self._row_counts()
        self.store.record_tick(frame, next_state)
        after = self._row_counts()
        # Publication order: the durable commit comes first, then the state,
        # frame and sequence a reader may act on.
        self._state = next_state
        self._last_frame = frame
        self._next_seq = frame.seq + 1
        self._epoch = frame.epoch
        self._deferred_baseline = False
        self._pending_gap_from = None
        self._last_observed_mono_ns = observed_mono_ns
        result = TickResult(
            apps_observed=len(frame.coverage),
            rows_written=sum(after[index] - before[index] for index in range(len(before))),
            coverage=_coverage_summary(frame.coverage),
        )
        self._last_result = result
        return result

    def run_forever(
        self, interval_seconds: float, max_ticks: int | None = None
    ) -> tuple[TickResult, ...]:
        """Run ticks at a fixed interval, with a finite mode useful in tests.

        The tick body has no event-loop dependency, so a Qt ``QTimer`` can call
        :meth:`tick` directly.  This method is the corresponding blocking
        convenience for a console caller and stops before sleeping after its
        final finite tick.
        """

        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
            raise ValueError("interval_seconds must be a finite non-negative number")
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("interval_seconds must be a finite non-negative number")
        if max_ticks is not None:
            if isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 0:
                raise ValueError("max_ticks must be a non-negative integer or None")
        results: list[TickResult] = []
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            results.append(self.tick())
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if interval_seconds:
                time.sleep(interval_seconds)
        return tuple(results)

    def quiesce(self) -> HandoverOffer:
        """Freeze this collector and capture its last committed interval.

        Call this from the collector's single owner thread: there is no lock, and
        a concurrent :meth:`tick` could commit a frame after the offered cutoff.

        The offer is durable evidence, so it is built only from committed rows:
        an epoch, a sequence, a checkpoint and the collector interval that ends
        at that checkpoint.  A stream without a measured interval cannot offer a
        cutoff, and a quiesced collector never ticks again.
        """

        if self._closed:
            raise RuntimeError("LiveCollector is closed")
        if self._offer is not None:
            self._quiesced = True
            return self._offer
        state = self._state
        if state is None or self._last_frame is None:
            raise IncompleteHandoverError(
                "a collector that has not committed a frame cannot offer a cutoff"
            )
        interval = self.store.connection.execute(
            "SELECT from_mono_ns,to_mono_ns FROM collector_interval "
            "WHERE source=? AND boot_id=? AND epoch=?",
            (self.source, self.boot_id, state.epoch),
        ).fetchone()
        if interval is None or interval[1] != state.checkpoint_mono_ns:
            raise IncompleteHandoverError(
                "no measured collector interval ends at the committed cutoff"
            )
        self._offer = HandoverOffer(
            self.source,
            _other_source(self.source),
            state.epoch,
            state.last_seq,
            interval[0],
            capture_handover(state),
        )
        self._quiesced = True
        return self._offer

    def close(self) -> None:
        if not self._closed:
            self.store.connection.close()
            self._closed = True

    def __enter__(self) -> "LiveCollector":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
