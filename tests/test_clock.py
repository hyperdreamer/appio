"""Contract tests for boottime buckets, clock segments, and suspend attribution.

The acceptance sequence is specification section 10.5: ``bucket_ns`` is one
minute, wall labels come only from persisted ``(segment_start, offset)`` rows, and
the ``[179s, 661s]`` observation straddles a measured ``[180s, 660s]`` suspend.
"""

from __future__ import annotations

import pytest

from appio.clock import (
    ClockMapState,
    ClockSegment,
    IntervalEvidence,
    SuspensionSet,
    attribute_interval,
    bucket_start_mono_ns,
)
from appio.reconcile import rate_bps

SECOND = 1_000_000_000
MINUTE = 60 * SECOND


def test_bucket_start_uses_integer_division_at_boundaries() -> None:
    assert bucket_start_mono_ns(0, MINUTE) == 0
    assert bucket_start_mono_ns(MINUTE, MINUTE) == MINUTE
    assert bucket_start_mono_ns(MINUTE - 1, MINUTE) == 0
    assert bucket_start_mono_ns(MINUTE + 1, MINUTE) == MINUTE
    assert bucket_start_mono_ns(3 * MINUTE - 1, MINUTE) == 2 * MINUTE


@pytest.mark.parametrize("bucket_ns", [0, -1, -MINUTE])
def test_bucket_start_requires_a_positive_bucket_width(bucket_ns: int) -> None:
    with pytest.raises(ValueError):
        bucket_start_mono_ns(0, bucket_ns)


def test_bucket_start_rejects_negative_boottime_and_non_integers() -> None:
    with pytest.raises(ValueError):
        bucket_start_mono_ns(-1, MINUTE)
    with pytest.raises(ValueError):
        bucket_start_mono_ns(True, MINUTE)
    with pytest.raises(ValueError):
        bucket_start_mono_ns(1.0, MINUTE)  # type: ignore[arg-type]


def test_bucket_partition_is_half_open_and_touches_without_gaps() -> None:
    for ts in (0, 1, MINUTE - 1, MINUTE, MINUTE + 1, 7 * MINUTE + 123):
        start = bucket_start_mono_ns(ts, MINUTE)
        assert start <= ts < start + MINUTE
        assert start == (ts // MINUTE) * MINUTE
    assert bucket_start_mono_ns(5 * MINUTE, MINUTE) + MINUTE == bucket_start_mono_ns(
        6 * MINUTE, MINUTE
    )


def test_first_observation_inserts_the_initial_segment_at_zero() -> None:
    clock = ClockMapState()
    segment = clock.observe("boot-A", 0, 1_000 * SECOND)
    assert segment == ClockSegment("boot-A", 0, 1_000 * SECOND)
    assert clock.segments("boot-A") == (ClockSegment("boot-A", 0, 1_000 * SECOND),)
    assert segment.boot_id == "boot-A"
    assert segment.segment_start_mono_ns == 0
    assert segment.wall_offset_ns == 1_000 * SECOND


def test_new_segment_only_when_the_offset_change_exceeds_one_second() -> None:
    clock = ClockMapState()
    clock.observe("boot-A", 0, 1_000 * SECOND)  # offset 1_000 s

    # Exactly one second of change is not "strictly greater".
    assert clock.observe("boot-A", 10 * SECOND, 1_011 * SECOND) is None
    assert clock.observe("boot-A", 20 * SECOND, 1_021 * SECOND + 1) == ClockSegment(
        "boot-A", 20 * SECOND, 1_001 * SECOND + 1
    )

    # The threshold is absolute, so a large negative step also inserts a segment.
    assert clock.observe("boot-A", 30 * SECOND, 1_000 * SECOND) == ClockSegment(
        "boot-A", 30 * SECOND, 970 * SECOND
    )

    assert [segment.segment_start_mono_ns for segment in clock.segments("boot-A")] == [
        0,
        20 * SECOND,
        30 * SECOND,
    ]


def test_clock_fixture_sequence_and_historical_labels() -> None:
    clock = ClockMapState()
    assert clock.observe("boot-A", 0, 1_000 * SECOND) == ClockSegment(
        "boot-A", 0, 1_000 * SECOND
    )
    assert clock.observe("boot-A", 119 * SECOND, 1_119 * SECOND) is None
    assert clock.observe("boot-A", 121 * SECOND, 1_121 * SECOND) is None
    assert clock.observe("boot-A", 125 * SECOND, 625 * SECOND) == ClockSegment(
        "boot-A", 125 * SECOND, 500 * SECOND
    )
    assert clock.observe("boot-A", 179 * SECOND, 679 * SECOND) is None
    assert clock.observe("boot-A", 661 * SECOND, 1_161 * SECOND) is None

    assert clock.segments("boot-A") == (
        ClockSegment("boot-A", 0, 1_000 * SECOND),
        ClockSegment("boot-A", 125 * SECOND, 500 * SECOND),
    )

    # Historical labels use the greatest start not greater than the sample.
    assert clock.wall_ns("boot-A", 0) == 1_000 * SECOND
    assert clock.wall_ns("boot-A", 119 * SECOND) == 1_119 * SECOND
    assert clock.wall_ns("boot-A", 125 * SECOND - 1) == (125 * SECOND - 1) + 1_000 * SECOND
    assert clock.wall_ns("boot-A", 125 * SECOND) == 625 * SECOND
    assert clock.wall_ns("boot-A", 179 * SECOND) == 679 * SECOND
    assert clock.wall_ns("boot-A", 660 * SECOND) == 1_160 * SECOND
    assert clock.wall_ns("boot-A", 661 * SECOND) == 1_161 * SECOND


def test_ntp_step_leaves_existing_bucket_keys_unchanged() -> None:
    clock = ClockMapState()
    clock.observe("boot-A", 0, 1_000 * SECOND)
    timestamps = (119 * SECOND, 121 * SECOND, 125 * SECOND, 179 * SECOND, 661 * SECOND)
    before = {ts: bucket_start_mono_ns(ts, MINUTE) for ts in timestamps}

    clock.observe("boot-A", 119 * SECOND, 1_119 * SECOND)
    clock.observe("boot-A", 121 * SECOND, 1_121 * SECOND)
    clock.observe("boot-A", 125 * SECOND, 625 * SECOND)
    clock.observe("boot-A", 179 * SECOND, 679 * SECOND)
    clock.observe("boot-A", 661 * SECOND, 1_161 * SECOND)
    after = {ts: bucket_start_mono_ns(ts, MINUTE) for ts in timestamps}

    assert after == before
    assert before == {
        119 * SECOND: 60 * SECOND,
        121 * SECOND: 120 * SECOND,
        125 * SECOND: 120 * SECOND,
        179 * SECOND: 120 * SECOND,
        661 * SECOND: 660 * SECOND,
    }


def test_boots_keep_independent_segments_starting_at_zero() -> None:
    clock = ClockMapState()
    assert clock.observe("boot-A", 0, 1_000 * SECOND) == ClockSegment(
        "boot-A", 0, 1_000 * SECOND
    )
    assert clock.observe("boot-B", 0, 2_000 * SECOND) == ClockSegment(
        "boot-B", 0, 2_000 * SECOND
    )
    assert clock.wall_ns("boot-A", 10 * SECOND) == 1_010 * SECOND
    assert clock.wall_ns("boot-B", 10 * SECOND) == 2_010 * SECOND
    assert clock.wall_ns("boot-C", 0) is None
    assert clock.segments("boot-C") == ()


def test_first_segment_covers_samples_before_the_first_observed_timestamp() -> None:
    # The initial segment starts at boot zero, so a later sample is still labelled
    # from the persisted mapping instead of by wall time.
    clock = ClockMapState()
    assert clock.observe("boot-A", 10 * SECOND, 1_010 * SECOND) == ClockSegment(
        "boot-A", 0, 1_000 * SECOND
    )
    assert clock.wall_ns("boot-A", 0) == 1_000 * SECOND
    assert clock.wall_ns("boot-A", 9 * SECOND) == 1_009 * SECOND


def test_suspend_union_of_overlaps_and_repeats_counts_once() -> None:
    susp = SuspensionSet()
    susp.add(180 * SECOND, 660 * SECOND)
    susp.add(300 * SECOND, 400 * SECOND)  # fully inside the first interval
    susp.add(180 * SECOND, 660 * SECOND)  # exact repeat
    susp.add(200 * SECOND, 250 * SECOND)  # another contained interval
    susp.add(661 * SECOND, 700 * SECOND)  # disjoint from the observation

    assert susp.measured_active_ns(179 * SECOND, 661 * SECOND) == 2 * SECOND
    # 1000 s elapsed minus 480 s suspend minus the uncovered [660 s, 661 s) gap.
    assert susp.measured_active_ns(0, 1_000 * SECOND) == 481 * SECOND
    assert susp.measured_active_ns(100 * SECOND, 200 * SECOND) == 80 * SECOND
    assert susp.intervals() == (
        (180 * SECOND, 660 * SECOND),
        (661 * SECOND, 700 * SECOND),
    )


def test_reversed_or_empty_intervals_are_handled_explicitly() -> None:
    susp = SuspensionSet()
    assert susp.measured_active_ns(10 * SECOND, 5 * SECOND) is None
    assert susp.measured_active_ns(5 * SECOND, 5 * SECOND) == 0
    assert susp.measured_active_ns(0, 10 * SECOND) == 10 * SECOND
    with pytest.raises(ValueError):
        susp.add(10 * SECOND, 5 * SECOND)


def test_spec_acceptance_suspend_spanning_interval() -> None:
    susp = SuspensionSet()
    susp.add(180 * SECOND, 660 * SECOND)
    susp.add(300 * SECOND, 400 * SECOND)  # repeated/overlapping boundary
    susp.add(180 * SECOND, 660 * SECOND)

    evidence = attribute_interval(179 * SECOND, 661 * SECOND, susp)

    assert evidence == IntervalEvidence(
        interval_start_mono_ns=179 * SECOND,
        observed_ns=2 * SECOND,
        attributed_ts_mono_ns=179 * SECOND,
        spans_suspend=True,
    )
    assert rate_bps(40, evidence.observed_ns) == 20
    assert rate_bps(20, evidence.observed_ns) == 10
    assert rate_bps(60, evidence.observed_ns) == 30


def test_ordinary_interval_attributes_to_the_observation_timestamp() -> None:
    susp = SuspensionSet()
    susp.add(180 * SECOND, 660 * SECOND)

    evidence = attribute_interval(119 * SECOND, 121 * SECOND, susp)

    assert evidence == IntervalEvidence(
        interval_start_mono_ns=119 * SECOND,
        observed_ns=2 * SECOND,
        attributed_ts_mono_ns=121 * SECOND,
        spans_suspend=False,
    )
    assert bucket_start_mono_ns(evidence.attributed_ts_mono_ns, MINUTE) == 120 * SECOND


def test_intervals_touching_the_suspend_boundary_do_not_span() -> None:
    susp = SuspensionSet()
    susp.add(180 * SECOND, 660 * SECOND)

    before = attribute_interval(170 * SECOND, 180 * SECOND, susp)
    assert before.observed_ns == 10 * SECOND
    assert before.spans_suspend is False
    assert before.attributed_ts_mono_ns == 180 * SECOND

    after = attribute_interval(660 * SECOND, 661 * SECOND, susp)
    assert after.observed_ns == 1 * SECOND
    assert after.spans_suspend is False
    assert after.attributed_ts_mono_ns == 661 * SECOND


def test_baseline_has_null_interval_and_rate_evidence() -> None:
    evidence = attribute_interval(None, 119 * SECOND, SuspensionSet())

    assert evidence == IntervalEvidence(
        interval_start_mono_ns=None,
        observed_ns=None,
        attributed_ts_mono_ns=119 * SECOND,
        spans_suspend=False,
    )
    assert rate_bps(40, evidence.observed_ns) is None


def test_unknown_suspend_boundaries_produce_null_observed_ns() -> None:
    evidence = attribute_interval(179 * SECOND, 661 * SECOND, None)

    assert evidence.interval_start_mono_ns == 179 * SECOND
    assert evidence.observed_ns is None
    assert evidence.spans_suspend is False
    assert rate_bps(40, evidence.observed_ns) is None


def test_known_uncovered_time_overlapping_suspend_is_excluded_once() -> None:
    susp = SuspensionSet()
    susp.add(180 * SECOND, 660 * SECOND)

    evidence = attribute_interval(
        179 * SECOND, 661 * SECOND, susp, known_uncovered=[(300 * SECOND, 400 * SECOND)]
    )

    assert evidence.observed_ns == 2 * SECOND
    assert evidence.spans_suspend is True


def test_known_uncovered_time_disjoint_from_suspend_is_excluded() -> None:
    susp = SuspensionSet()
    susp.add(180 * SECOND, 660 * SECOND)
    uncovered = SuspensionSet()
    uncovered.add(670 * SECOND, 700 * SECOND)

    evidence = attribute_interval(179 * SECOND, 700 * SECOND, susp, known_uncovered=uncovered)

    assert evidence.observed_ns == 11 * SECOND
    assert evidence.spans_suspend is True


def test_long_delay_without_explicit_boundaries_is_not_suspend() -> None:
    evidence = attribute_interval(0, 600 * SECOND, SuspensionSet())

    assert evidence.observed_ns == 600 * SECOND
    assert evidence.spans_suspend is False
    assert evidence.attributed_ts_mono_ns == 600 * SECOND


def test_reversed_observation_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        attribute_interval(10 * SECOND, 5 * SECOND, SuspensionSet())
