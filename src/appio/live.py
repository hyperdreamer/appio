"""Live process and cgroup collection for the AppIO application.

The existing process, cgroup, sampler, and store modules deliberately stop at
fixture-shaped inputs.  This module is the small adapter that supplies those
inputs from configurable roots while retaining their pure and transactional
boundaries.  Tests can therefore point it at a temporary tree and a deterministic
clock without opening the host process table or clock.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import time
from typing import Literal

from .cgroups import CgroupObservation, read_cgroup_fixture
from .procs import ProcReader, ProcessVanished, StableProcRecord
from .sampler import FrameCoverage, SamplerState, TickInput, aggregate_tick
from .store import AppIoStore

__all__ = [
    "CoverageSummary",
    "LiveCollector",
    "SystemClock",
    "TickResult",
]

_NS = 1_000_000_000
_SOURCE = "gui"
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


class LiveCollector:
    """Own one live process stream and commit one complete frame per tick.

    ``proc_root``, ``cgroup_root``, and ``database_path`` are all explicit so a
    caller can use the same object with a fixture tree.  ``clock`` returns a
    boottime and wall-clock pair; a callable is the simplest injectable form.
    """

    def __init__(
        self,
        proc_root: str | Path = "/proc",
        cgroup_root: str | Path = "/sys/fs/cgroup",
        database_path: str | Path = "history.db",
        clock: object | None = None,
        *,
        source: Literal["gui", "iod"] = _SOURCE,
    ) -> None:
        if source != _SOURCE:
            raise ValueError("LiveCollector currently owns the gui collector stream")
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock: object = SystemClock() if clock is None else clock
        self.source = source
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
        elif tuple(boot[3:]) != (self.source,):
            raise ValueError(
                f"boot {self.boot_id!r} belongs to source {boot[3]!r}, not {self.source!r}"
            )

        epoch = self.store.start_collector(self.source, self.boot_id, initial_mono_ns)
        self.epoch = epoch.epoch
        self._state: SamplerState | None = self.store.restore_checkpoint(
            self.source, self.boot_id
        )
        self._next_seq = 0
        self._last_result: TickResult | None = None
        self._last_observed_mono_ns: int | None = None
        self._closed = False

    @property
    def sampler_state(self) -> SamplerState | None:
        return self._state

    @property
    def last_result(self) -> TickResult | None:
        return self._last_result

    @property
    def last_observed_mono_ns(self) -> int | None:
        return self._last_observed_mono_ns

    @property
    def next_seq(self) -> int:
        return self._next_seq

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

    def tick(self) -> TickResult:
        """Read the host once, aggregate it, and commit one sampler frame."""

        if self._closed:
            raise RuntimeError("LiveCollector is closed")
        observed_mono_ns, wall_ns = _read_clock(self.clock)
        reader = ProcReader(self.proc_root, boot_id=self.boot_id)
        records, unresolved, vanished = self._read_processes(reader)
        cgroups = self._read_cgroups(records)
        tick = TickInput(
            boot_id=self.boot_id,
            source=self.source,
            epoch=self.epoch,
            seq=self._next_seq,
            observed_mono_ns=observed_mono_ns,
            wall_ns=wall_ns,
            processes=records,
            cgroups=cgroups,
            unresolved_pids=unresolved,
            vanished_pids=vanished,
        )
        frame, next_state = aggregate_tick(self._state, tick)
        before = self._row_counts()
        self.store.record_tick(frame, next_state)
        after = self._row_counts()
        self._state = next_state
        self._next_seq += 1
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

    def close(self) -> None:
        if not self._closed:
            self.store.connection.close()
            self._closed = True

    def __enter__(self) -> "LiveCollector":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
