"""``CLOCK_BOOTTIME`` bucket arithmetic, clock-map segments, and suspend attribution.

Everything here operates on boot-scoped monotonic nanoseconds. Wall time is a
display label derived from persisted ``(boot_id, segment_start_mono_ns,
wall_offset_ns)`` rows, so an NTP step can never move a bucket key or a row.

The two suspend rules from A09 are enforced rather than inferred:

* only explicit suspend boundaries are subtracted, never a long sampling delay;
* a delta that spans suspend keeps its actual observation timestamp while its
  attributed bucket timestamp is the last observation before suspend.

Intervals are half-open ``[start, end)`` throughout.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from appio.contracts import validate_sqlite_integer

__all__ = [
    "ClockMapState",
    "ClockSegment",
    "IntervalEvidence",
    "SUSPEND_OFFSET_STEP_NS",
    "SuspensionSet",
    "attribute_interval",
    "bucket_start_mono_ns",
]

SUSPEND_OFFSET_STEP_NS: Final = 1_000_000_000
"""Offset change strictly greater than this inserts a new ``clock_map`` segment."""


def _require_boot_id(value: object, field: str = "boot_id") -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL")
    return value


def _require_mono(value: object, field: str) -> int:
    """Validate a nonnegative ``CLOCK_BOOTTIME`` nanosecond value."""

    return validate_sqlite_integer(value, field)


def _require_signed_ns(value: object, field: str) -> int:
    return validate_sqlite_integer(value, field, nonnegative=False)


def bucket_start_mono_ns(ts_mono_ns: int, bucket_ns: int) -> int:
    """Return ``(ts_mono_ns // bucket_ns) * bucket_ns`` with integer division."""

    timestamp = _require_mono(ts_mono_ns, "ts_mono_ns")
    width = _require_mono(bucket_ns, "bucket_ns")
    if width == 0:
        raise ValueError("bucket_ns must be positive, got 0")
    return (timestamp // width) * width


@dataclass(frozen=True)
class ClockSegment:
    """One persisted ``clock_map`` row: ``wall_ns = mono_ns + wall_offset_ns``."""

    boot_id: str
    segment_start_mono_ns: int
    wall_offset_ns: int

    def __post_init__(self) -> None:
        _require_boot_id(self.boot_id, "clock_segment.boot_id")
        _require_mono(self.segment_start_mono_ns, "clock_segment.segment_start_mono_ns")
        _require_signed_ns(self.wall_offset_ns, "clock_segment.wall_offset_ns")


class ClockMapState:
    """In-memory form of the persisted ``clock_map`` rows, per boot."""

    def __init__(self) -> None:
        self._segments: dict[str, list[ClockSegment]] = {}

    def segments(self, boot_id: str) -> tuple[ClockSegment, ...]:
        """Return this boot's segments in ascending start order."""

        boot = _require_boot_id(boot_id)
        return tuple(self._segments.get(boot, ()))

    def observe(self, boot_id: str, ts_mono_ns: int, wall_ns: int) -> ClockSegment | None:
        """Record one simultaneous realtime/boottime sample.

        The first accepted observation for a boot inserts its segment at boot zero
        so the label mapping reaches back to boot zero. A later segment is inserted
        only when the absolute offset change is strictly greater than one second;
        otherwise the existing mapping already covers this sample.
        """

        boot = _require_boot_id(boot_id)
        timestamp = _require_mono(ts_mono_ns, "ts_mono_ns")
        wall = _require_signed_ns(wall_ns, "wall_ns")
        offset = _require_signed_ns(wall - timestamp, "wall_offset_ns")

        current = self._segments.get(boot)
        if not current:
            segment = ClockSegment(boot, 0, offset)
            self._segments[boot] = [segment]
            return segment

        latest = current[-1]
        if timestamp < latest.segment_start_mono_ns:
            raise ValueError(
                "clock observations must be non-decreasing in boottime: "
                f"got {timestamp} after {latest.segment_start_mono_ns}"
            )
        if abs(offset - latest.wall_offset_ns) <= SUSPEND_OFFSET_STEP_NS:
            return None

        segment = ClockSegment(boot, timestamp, offset)
        if timestamp == latest.segment_start_mono_ns:
            current[-1] = segment
        else:
            current.append(segment)
        return segment

    def wall_ns(self, boot_id: str, ts_mono_ns: int) -> int | None:
        """Map a boottime sample to wall nanoseconds, or ``None`` when unmapped."""

        boot = _require_boot_id(boot_id)
        timestamp = _require_mono(ts_mono_ns, "ts_mono_ns")
        current = self._segments.get(boot)
        if not current:
            return None
        starts = [segment.segment_start_mono_ns for segment in current]
        index = bisect_right(starts, timestamp) - 1
        if index < 0:
            return None
        return timestamp + current[index].wall_offset_ns


def _merge(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and union half-open intervals, counting overlap only once."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _covered_ns(intervals: list[tuple[int, int]], start: int, end: int) -> int:
    """Return how much of ``[start, end)`` the sorted, merged intervals cover."""

    covered = 0
    for interval_start, interval_end in intervals:
        if interval_end <= start:
            continue
        if interval_start >= end:
            break
        covered += min(interval_end, end) - max(interval_start, start)
    return covered


def _normalize_uncovered(source: object) -> list[tuple[int, int]]:
    if isinstance(source, SuspensionSet):
        return list(source.intervals())
    if isinstance(source, (str, bytes)) or not isinstance(source, Iterable):
        raise ValueError("known_uncovered must be a SuspensionSet or an iterable of pairs")
    intervals: list[tuple[int, int]] = []
    for entry in source:
        try:
            start, end = entry
        except (TypeError, ValueError) as error:
            raise ValueError(
                "known_uncovered intervals must be (start_ns, end_ns) pairs"
            ) from error
        start = _require_mono(start, "known_uncovered.start_ns")
        end = _require_mono(end, "known_uncovered.end_ns")
        if end < start:
            raise ValueError(
                f"known_uncovered interval end {end} precedes start {start}"
            )
        if end > start:
            intervals.append((start, end))
    return _merge(intervals)


class SuspensionSet:
    """The union of explicit, measured suspend intervals for one boot."""

    def __init__(self) -> None:
        self._intervals: list[tuple[int, int]] = []

    def add(self, start_ns: int, end_ns: int) -> None:
        """Union one measured suspend interval into the set."""

        start = _require_mono(start_ns, "suspend.start_ns")
        end = _require_mono(end_ns, "suspend.end_ns")
        if end < start:
            raise ValueError(f"suspend interval end {end} precedes start {start}")
        if end == start:
            return
        self._intervals = _merge([*self._intervals, (start, end)])

    def intervals(self) -> tuple[tuple[int, int], ...]:
        """Return the sorted, merged union."""

        return tuple(self._intervals)

    def measured_active_ns(self, start_ns: int, end_ns: int) -> int | None:
        """Return the measured active duration, or ``None`` for an unknown duration."""

        start = _require_mono(start_ns, "start_ns")
        end = _require_mono(end_ns, "end_ns")
        if end < start:
            return None
        return (end - start) - _covered_ns(self._intervals, start, end)


@dataclass(frozen=True)
class IntervalEvidence:
    """Rate evidence for one observed delta.

    ``observed_ns`` is the measured active denominator (``None`` when undefined);
    ``attributed_ts_mono_ns`` is the bucket-assignment timestamp, which is the last
    pre-suspend observation for a suspend-spanning delta.
    """

    interval_start_mono_ns: int | None
    observed_ns: int | None
    attributed_ts_mono_ns: int
    spans_suspend: bool

    def __post_init__(self) -> None:
        if self.interval_start_mono_ns is not None:
            _require_mono(self.interval_start_mono_ns, "interval_evidence.interval_start_mono_ns")
        if self.observed_ns is not None:
            _require_mono(self.observed_ns, "interval_evidence.observed_ns")
        elif self.interval_start_mono_ns is None and self.spans_suspend:
            raise ValueError(
                "a baseline interval cannot be marked as spanning suspend"
            )
        _require_mono(self.attributed_ts_mono_ns, "interval_evidence.attributed_ts_mono_ns")
        if not isinstance(self.spans_suspend, bool):
            raise ValueError("interval_evidence.spans_suspend must be a bool")


def attribute_interval(
    last_observed_ns: int | None,
    observed_ns: int,
    suspend_set: SuspensionSet | None,
    known_uncovered: SuspensionSet | Iterable[tuple[int, int]] | None = None,
) -> IntervalEvidence:
    """Attribute one delta observed at ``observed_ns`` back to a bucket timestamp.

    A first baseline (``last_observed_ns is None``) has NULL interval/rate evidence.
    ``suspend_set is None`` means the suspend boundaries are unknown, which yields a
    NULL denominator rather than a guessed rate. Explicit suspend and known-uncovered
    intervals are unioned before being subtracted, so overlapping time counts once.
    """

    observed = _require_mono(observed_ns, "observed_ns")
    if last_observed_ns is None:
        return IntervalEvidence(None, None, observed, False)

    last = _require_mono(last_observed_ns, "last_observed_ns")
    if observed < last:
        raise ValueError(
            f"observation at {observed} precedes the previous observation at {last}"
        )
    if suspend_set is None:
        return IntervalEvidence(last, None, observed, False)
    if not isinstance(suspend_set, SuspensionSet):
        raise ValueError("suspend_set must be a SuspensionSet or None")

    suspend_intervals = list(suspend_set.intervals())
    excluded = list(suspend_intervals)
    if known_uncovered is not None:
        excluded.extend(_normalize_uncovered(known_uncovered))

    covered = _covered_ns(_merge(excluded), last, observed)
    spans_suspend = _covered_ns(suspend_intervals, last, observed) > 0
    attributed = last if spans_suspend else observed
    return IntervalEvidence(last, (observed - last) - covered, attributed, spans_suspend)
