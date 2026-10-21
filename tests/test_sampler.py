from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

import pytest

from appio.cgroups import CgroupObservation, DevStat, DeviceDelta
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.sampler import SamplerState, StaleFrameError, TickInput, aggregate_tick

BOOT = "11111111-2222-4333-8444-555555555555"
CHROMIUM_PATH = (
    "/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-chromium@fixture.service"
)
PIDREUSE_PATH = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-pidreuse.scope"
T0, T1, T2 = 100_000_000_000, 110_000_000_000, 120_000_000_000


def proc(
    pid: int,
    ppid: int,
    start: int,
    counters: tuple[int, int, int, int] | None = (0, 0, 0, 0),
    *,
    path: str = CHROMIUM_PATH,
    status: str = "readable",
    exe: str | None = None,
) -> StableProcRecord:
    exe_value = exe or ("/usr/lib/chromium/chromium" if path == CHROMIUM_PATH else "/usr/bin/fixture")
    return StableProcRecord(
        identity=ProcessIdentity(BOOT, pid, start),
        ppid=ppid,
        uid=1000,
        exe=exe_value,
        cgroup_path=path,
        app_key_inputs=AppKeyInputs(path, exe_value),
        io_outcome=IoReadOutcome(
            status=status,  # type: ignore[arg-type]
            counters=counters if status == "readable" else None,
            dumpable=False if status == "eacces" else None,
            errno_name="EACCES" if status == "eacces" else None,
        ),
    )


def tick(
    seq: int,
    at: int,
    processes: Iterable[StableProcRecord],
    *,
    epoch: int = 1,
    cgroups: tuple[CgroupObservation, ...] = (),
    suspend: tuple[tuple[int, int], ...] = (),
) -> TickInput:
    return TickInput(
        boot_id=BOOT,
        source="gui",
        epoch=epoch,
        seq=seq,
        observed_mono_ns=at,
        wall_ns=at + 1_000_000_000_000,
        processes=tuple(processes),
        cgroups=cgroups,
        suspend_boundaries=suspend,
    )


def by_app(values: Iterable[object]) -> dict[str, object]:
    return {getattr(value, "app_key"): value for value in values}


def chromium_cgroup(at: int, r0: int, w0: int, r1: int = 0, w1: int = 0) -> CgroupObservation:
    return CgroupObservation(
        boot_id=BOOT,
        cgroup_id=101,
        cgroup_path=CHROMIUM_PATH,
        device_state="available",
        devices=(DevStat(101, "259:0", r0, w0), DevStat(101, "259:1", r1, w1)),
    )


def test_three_tick_acceptance_fixture_and_pid_reuse() -> None:
    """Falsified by wrong t1 totals, double-counted reap credit, or reused baselines."""
    t0_processes = (
        proc(410, 1, 100),
        proc(411, 410, 101),
        proc(412, 1, 102),
        proc(413, 1, 103, None, status="eacces"),
        proc(500, 1, 700, path=PIDREUSE_PATH),
    )
    t1_processes = (
        proc(410, 1, 100, (1000, 500, 100, 70)),
        proc(411, 410, 101, (500, 100, 50, 10)),
        proc(412, 1, 102, (300, 200, 30, 10)),
        proc(413, 1, 103, None, status="eacces"),
        proc(500, 1, 700, (0, 0, 25, 0), path=PIDREUSE_PATH),
    )
    t2_processes = (
        proc(410, 1, 100, (1600, 650, 170, 90)),
        proc(412, 1, 102, (300, 200, 30, 10)),
        proc(413, 1, 103, None, status="eacces"),
        proc(500, 1, 900, (0, 0, 0, 0), path=PIDREUSE_PATH),
    )

    frame0, state0 = aggregate_tick(None, tick(0, T0, t0_processes))
    frame1, state1 = aggregate_tick(state0, tick(1, T1, t1_processes))
    frame2, state2 = aggregate_tick(
        state1,
        tick(
            2,
            T2,
            t2_processes,
            cgroups=(chromium_cgroup(T2, 1120, 270, 350, 550),),
        ),
    )

    chromium_key = "unit:app-chromium@fixture.service"
    chromium_t1 = [d for d in frame1.deltas if d.app_key == chromium_key]
    assert sum(d.counters[i] for d in chromium_t1 for i in (0,)) == 1800
    assert sum(d.counters[1] for d in chromium_t1) == 800
    assert sum(d.counters[2] for d in chromium_t1) == 180
    assert sum(d.counters[3] for d in chromium_t1) == 90
    assert all(d.delta_kind == "normal" for d in chromium_t1)

    chromium_t2 = [d for d in frame2.deltas if d.app_key == chromium_key]
    assert sum(d.counters[0] for d in chromium_t2) == 100
    assert sum(d.counters[1] for d in chromium_t2) == 50
    assert sum(d.counters[2] for d in chromium_t2) == 20
    assert sum(d.counters[3] for d in chromium_t2) == 10
    coverage0, coverage1, coverage2 = (
        by_app(frame.coverage)[chromium_key] for frame in (frame0, frame1, frame2)
    )
    assert [c.inaccessible_pids for c in (coverage0, coverage1, coverage2)] == [1, 1, 1]
    assert [c.resets for c in (coverage0, coverage1, coverage2)] == [0, 0, 0]
    assert coverage2.reap_corrections == 1
    assert coverage2.reap_unapplied == (0, 0, 0, 0)
    assert coverage2.reap_unobserved == (0, 0, 0, 0)
    cumulative = tuple(
        sum(d.counters[i] for frame in (frame0, frame1, frame2) for d in frame.deltas if d.app_key == chromium_key)
        for i in range(4)
    )
    assert cumulative == (1900, 850, 200, 100)

    reuse_key = "unit:app-pidreuse.scope"
    reuse_read = sum(
        d.counters[2]
        for frame in (frame0, frame1, frame2)
        for d in frame.deltas
        if d.app_key == reuse_key
    )
    assert reuse_read == 25
    assert not any(min(d.counters) < 0 for d in frame2.deltas)
    assert ProcessIdentity(BOOT, 500, 700) not in {p.identity for p in state2.processes}
    assert ProcessIdentity(BOOT, 500, 900) in {p.identity for p in state2.processes}


def test_pid_reuse_does_not_transfer_nonzero_baseline_or_credit() -> None:
    """Falsified if the new start_ticks identity inherits the old counters."""
    first, state = aggregate_tick(None, tick(0, T0, [proc(500, 1, 700, (0, 0, 25, 0), path=PIDREUSE_PATH)]))
    second, next_state = aggregate_tick(
        state,
        tick(1, T1, [proc(500, 1, 900, (0, 0, 0, 0), path=PIDREUSE_PATH)]),
    )
    assert sum(d.counters[2] for d in first.deltas) == 25
    assert sum(d.counters[2] for d in second.deltas) == 0
    assert by_app(second.coverage)["unit:app-pidreuse.scope"].resets == 0
    assert next_state.processes[0].baseline == (0, 0, 0, 0)


def test_first_seen_partial_and_zero_baseline_are_distinct() -> None:
    """Falsified if first nonzero counters are omitted, repeated, or not marked partial."""
    partial_frame, partial_state = aggregate_tick(
        None, tick(0, T0, [proc(20, 1, 1, (70, 30, 7, 3))])
    )
    delta = partial_frame.deltas[0]
    coverage = partial_frame.coverage[0]
    assert delta.counters == (70, 30, 7, 3)
    assert delta.first_seen_partial is True
    assert delta.delta_kind == "baseline"
    assert coverage.partial_intervals == 1

    next_frame, _ = aggregate_tick(
        partial_state, tick(1, T1, [proc(20, 1, 1, (70, 30, 7, 3))])
    )
    assert sum(next_delta.counters[2] for next_delta in next_frame.deltas) == 0

    zero_frame, _ = aggregate_tick(None, tick(0, T0, [proc(21, 1, 2, (0, 0, 0, 0))]))
    assert zero_frame.deltas[0].counters == (0, 0, 0, 0)
    assert zero_frame.deltas[0].first_seen_partial is False
    assert zero_frame.deltas[0].delta_kind == "baseline"


def test_each_decreased_counter_resets_only_its_own_baseline() -> None:
    """Falsified if one decrease zeros other components or the next delta uses stale values."""
    _, state0 = aggregate_tick(None, tick(0, T0, [proc(30, 1, 1, (100, 20, 10, 5))]))
    reset_frame, state1 = aggregate_tick(
        state0, tick(1, T1, [proc(30, 1, 1, (90, 25, 8, 9))])
    )
    assert reset_frame.deltas[0].counters == (0, 5, 0, 4)
    assert reset_frame.deltas[0].delta_kind == "reset"
    assert reset_frame.coverage[0].resets == 1
    assert state1.processes[0].baseline == (90, 25, 8, 9)
    next_frame, _ = aggregate_tick(
        state1, tick(2, T2, [proc(30, 1, 1, (95, 30, 9, 10))])
    )
    assert next_frame.deltas[0].counters == (5, 5, 1, 1)
    assert next_frame.coverage[0].resets == 0


def test_esrch_counts_only_for_open_identity_and_eacces_preserves_baseline() -> None:
    """Falsified by counting unknown ESRCH, emitting an EACCES delta, or losing its baseline."""
    _, state = aggregate_tick(None, tick(0, T0, [proc(40, 1, 1, (0, 0, 0, 0)), proc(41, 1, 2, None, status="eacces")]))
    vanished_frame, vanished_state = aggregate_tick(
        state,
        tick(1, T1, [proc(40, 1, 1, None, status="esrch"), proc(99, 1, 7, None, status="esrch")]),
    )
    app = by_app(vanished_frame.coverage)["unit:app-chromium@fixture.service"]
    assert app.resets == 1
    assert app.inaccessible_pids == 0
    assert not vanished_frame.deltas
    assert all(p.identity.pid != 40 for p in vanished_state.processes)

    _, with_baseline = aggregate_tick(None, tick(0, T0, [proc(42, 1, 3, (1, 2, 3, 4))]))
    denied, denied_state = aggregate_tick(
        with_baseline, tick(1, T1, [proc(42, 1, 3, None, status="eacces")])
    )
    assert not denied.deltas
    assert denied.coverage[0].inaccessible_pids == 1
    assert denied_state.processes[0].baseline == (1, 2, 3, 4)


def test_error_record_preserves_launch_membership_until_stable_read() -> None:
    """Falsified if an unreadable record closes a launch that remains open."""
    root = proc(150, 1, 1, (0, 0, 0, 0))
    child = proc(151, 150, 2, (0, 0, 0, 0))
    _, state0 = aggregate_tick(None, tick(0, T0, [root, child]))
    launch = state0.processes[1].launch_identity

    _, state1 = aggregate_tick(
        state0,
        tick(1, T1, [proc(150, 1, 1, (0, 0, 0, 0)), proc(151, 150, 2, None, status="error")]),
    )
    assert state1.processes[1].launch_identity == launch
    assert state1.launches[0].members == (root.identity, child.identity)

    resumed, state2 = aggregate_tick(
        state1,
        tick(2, T2, [proc(151, 150, 2, (0, 0, 1, 0))]),
    )
    assert resumed.deltas[0].launch_key == launch
    assert state2.launches[0].members == (child.identity,)


def test_unresolved_identity_is_preserved_without_emitting_a_delta() -> None:
    """Falsified if an unstable stat read is treated as a confirmed disappearance."""
    _, state0 = aggregate_tick(None, tick(0, T0, [proc(45, 1, 1, (0, 0, 10, 4))]))
    unresolved_tick = TickInput(
        boot_id=BOOT,
        source="gui",
        epoch=1,
        seq=1,
        observed_mono_ns=T1,
        wall_ns=T1 + 1_000_000_000_000,
        processes=(),
        unresolved_pids=(45,),
    )
    frame1, state1 = aggregate_tick(state0, unresolved_tick)
    assert frame1.deltas == ()
    assert state1.processes == state0.processes
    assert state1.launches == state0.launches

    frame2, state2 = aggregate_tick(state1, tick(2, T2, [proc(45, 1, 1, (0, 0, 16, 7))]))
    assert frame2.deltas[0].counters == (0, 0, 6, 3)
    assert state2.processes[0].baseline == (0, 0, 16, 7)


def test_pre_identity_vanish_closes_open_pid_once() -> None:
    """Falsified if ProcessVanished loses the PID before sampler reset accounting."""
    _, state0 = aggregate_tick(None, tick(0, T0, [proc(46, 1, 1, (0, 0, 10, 4))]))
    vanished_tick = TickInput(
        boot_id=BOOT,
        source="gui",
        epoch=1,
        seq=1,
        observed_mono_ns=T1,
        wall_ns=T1 + 1_000_000_000_000,
        processes=(),
        vanished_pids=(46,),
    )
    frame, state = aggregate_tick(state0, vanished_tick)
    assert not state.processes
    assert by_app(frame.coverage)["unit:app-chromium@fixture.service"].resets == 1


def test_reap_credit_is_disclosed_when_parent_closed_in_an_earlier_frame() -> None:
    """Falsified if a credit for an already-closed parent silently disappears."""
    _, state0 = aggregate_tick(
        None,
        tick(0, T0, [proc(52, 1, 1), proc(53, 52, 2)]),
    )
    _, state1 = aggregate_tick(
        state0,
        tick(1, T1, [proc(53, 52, 2, (0, 0, 8, 0))]),
    )

    frame, next_state = aggregate_tick(state1, tick(2, T2, []))

    assert not next_state.processes
    coverage = by_app(frame.coverage)["unit:app-chromium@fixture.service"]
    assert coverage.reap_unapplied == (0, 0, 8, 0)


def test_fifo_credit_carry_forward_and_unapplied_disclosure() -> None:
    """Falsified if credit makes a delta negative, vanishes before use, or is counted twice."""
    _, state0 = aggregate_tick(
        None,
        tick(0, T0, [proc(50, 1, 1), proc(51, 50, 2)]),
    )
    _, state1 = aggregate_tick(
        state0,
        tick(1, T1, [proc(50, 1, 1), proc(51, 50, 2, (0, 0, 8, 0))]),
    )
    partial, state2 = aggregate_tick(state1, tick(2, T2, [proc(50, 1, 1, (0, 0, 3, 0))]))
    assert partial.deltas[0].counters[2] == 0
    assert partial.coverage[0].reap_corrections == 1
    assert partial.coverage[0].reap_unobserved == (0, 0, 0, 0)
    assert state2.processes[0].credits[0].remaining == (0, 0, 5, 0)

    second_parent_delta, state3 = aggregate_tick(
        state2, tick(3, T2 + 10_000_000_000, [proc(50, 1, 1, (0, 0, 7, 0))])
    )
    assert second_parent_delta.deltas[0].counters[2] == 0
    assert second_parent_delta.coverage[0].reap_corrections == 0
    assert state3.processes[0].credits[0].remaining == (0, 0, 1, 0)

    closed, final_state = aggregate_tick(
        state3, tick(4, T2 + 20_000_000_000, [])
    )
    assert not final_state.processes
    assert by_app(closed.coverage)["unit:app-chromium@fixture.service"].reap_unapplied == (0, 0, 1, 0)


def test_nested_reap_credit_marks_intermediate_parent_incomplete() -> None:
    """Falsified if descendant reap credit is presented as provably exact T."""
    _, state0 = aggregate_tick(
        None,
        tick(
            0,
            T0,
            [
                proc(130, 1, 1, (0, 0, 0, 0)),
                proc(131, 130, 2, (0, 0, 0, 0)),
                proc(132, 131, 3, (0, 0, 10, 0)),
            ],
        ),
    )
    nested_reap, state1 = aggregate_tick(
        state0,
        tick(1, T1, [proc(130, 1, 1), proc(131, 130, 2)]),
    )
    assert nested_reap.coverage[0].reap_incomplete is True
    assert state1.processes[1].credits[0].remaining == (0, 0, 10, 0)

    applied, state2 = aggregate_tick(
        state1,
        tick(2, T2, [proc(130, 1, 1), proc(131, 130, 2, (0, 0, 10, 0))]),
    )
    assert sum(delta.counters[2] for delta in applied.deltas) == 0
    assert applied.coverage[0].reap_incomplete is True
    assert applied.coverage[0].reap_corrections == 1
    assert state2.processes[1].credits == ()


def test_counter_reset_with_pending_credit_stays_incomplete_through_use() -> None:
    """Falsified if reset-tainted credit is later reported as exact T."""
    _, state0 = aggregate_tick(
        None,
        tick(
            0,
            T0,
            [proc(140, 1, 1, (0, 0, 100, 0)), proc(141, 140, 2, (0, 0, 20, 0))],
        ),
    )
    _, state1 = aggregate_tick(state0, tick(1, T1, [proc(140, 1, 1, (0, 0, 100, 0))]))
    reset, state2 = aggregate_tick(
        state1, tick(2, T2, [proc(140, 1, 1, (0, 0, 90, 0))])
    )
    assert reset.coverage[0].resets == 1
    assert reset.coverage[0].reap_incomplete is True

    consumed, _ = aggregate_tick(
        state2, tick(3, T2 + 10_000_000_000, [proc(140, 1, 1, (0, 0, 110, 0))])
    )
    assert consumed.deltas[0].counters[2] == 0
    assert consumed.coverage[0].reap_corrections == 1
    assert consumed.coverage[0].reap_incomplete is True

    checkpoint = state2.to_checkpoint()
    checkpoint_process = checkpoint["processes"][0]
    assert set(checkpoint_process).isdisjoint({"had_reset", "reap_incomplete"})
    restored_processes = tuple(
        replace(process, had_reset=False, reap_incomplete=False)
        for process in state2.processes
    )
    restored_state = replace(state2, processes=restored_processes)
    after_restart, _ = aggregate_tick(
        restored_state,
        tick(0, T2 + 20_000_000_000, [proc(140, 1, 1, (0, 0, 110, 0))], epoch=2),
    )
    assert after_restart.deltas[0].counters[2] == 0
    assert after_restart.coverage[0].reap_incomplete is True


def test_unseen_child_jump_is_normal_parent_io_not_a_guessed_reap() -> None:
    """Falsified if a large parent delta is relabelled as an unseen-child reap."""
    _, state = aggregate_tick(None, tick(0, T0, [proc(60, 1, 1)]))
    frame, _ = aggregate_tick(state, tick(1, T1, [proc(60, 1, 1, (0, 0, 40, 12))]))
    assert frame.deltas[0].counters == (0, 0, 40, 12)
    assert frame.coverage[0].reap_unobserved == (0, 0, 0, 0)
    assert frame.coverage[0].reap_corrections == 0


def test_multiple_attributed_timestamps_keep_one_observed_frame_coverage() -> None:
    """Falsified if suspend and baseline contributors collapse to one timestamp or duplicate coverage."""
    _, state = aggregate_tick(
        None,
        tick(0, 110_000_000_000, [proc(70, 1, 1, (0, 0, 0, 0))]),
    )
    frame, _ = aggregate_tick(
        state,
        tick(
            1,
            T2,
            [proc(70, 1, 1, (0, 0, 40, 20)), proc(71, 70, 2, (0, 0, 7, 3))],
            suspend=((115_000_000_000, 119_000_000_000),),
        ),
    )
    assert [(d.attributed_ts_mono_ns, d.counters[2:]) for d in frame.deltas] == [
        (110_000_000_000, (40, 20)),
        (T2, (7, 3)),
    ]
    assert all(d.observed_mono_ns == T2 for d in frame.deltas)
    assert len(frame.coverage) == 1
    assert frame.coverage[0].observed_mono_ns == T2
    assert frame.coverage[0].partial_intervals == 1
    assert frame.coverage[0].spans_suspend_intervals == 1


def test_frame_sequence_is_rejected_without_mutating_checkpoint_state() -> None:
    """Falsified if duplicate/stale sequence handling advances a baseline."""
    _, state = aggregate_tick(None, tick(0, T0, [proc(80, 1, 1, (0, 0, 10, 0))]))
    with pytest.raises(StaleFrameError):
        aggregate_tick(state, tick(0, T1, [proc(80, 1, 1, (0, 0, 99, 0))]))
    with pytest.raises(StaleFrameError):
        aggregate_tick(
            state,
            tick(1, T1, [proc(80, 1, 1, (0, 0, 99, 0))], epoch=2),
        )
    assert state.processes[0].baseline == (0, 0, 10, 0)


def test_cgroup_device_baselines_are_consumed_once_per_cgroup_not_process() -> None:
    """Falsified if shared process membership multiplies D or first observation is nonzero."""
    owners = [proc(90, 1, 1), proc(91, 90, 2)]
    cgroup0 = CgroupObservation(BOOT, 101, CHROMIUM_PATH, "available", (DevStat(101, "259:0", 100, 20),))
    cgroup1 = CgroupObservation(BOOT, 101, CHROMIUM_PATH, "available", (DevStat(101, "259:0", 140, 25),))
    first, state = aggregate_tick(None, tick(0, T0, owners, cgroups=(cgroup0,)))
    second, _ = aggregate_tick(state, tick(1, T1, owners, cgroups=(cgroup1,)))
    assert first.device_deltas[0].app_key == "unit:app-chromium@fixture.service"
    assert first.device_deltas[0].delta == DeviceDelta(101, "259:0", 0, 0, False)
    assert second.device_deltas[0].delta == DeviceDelta(101, "259:0", 40, 5, False)


def test_not_delegated_cgroup_keeps_state_without_zero_device_rows() -> None:
    """Falsified if absent controller delegation becomes available or emits D=0."""
    process = proc(61, 1, 1)
    observation = CgroupObservation(
        boot_id=BOOT,
        cgroup_id=161,
        cgroup_path=CHROMIUM_PATH,
        device_state="not_delegated",
        devices=(),
    )

    frame, _ = aggregate_tick(None, tick(0, T0, [process], cgroups=(observation,)))

    coverage = by_app(frame.coverage)["unit:app-chromium@fixture.service"]
    assert coverage.device_state == "not_delegated"
    assert frame.device_deltas == ()


def test_distinct_app_owners_of_one_cgroup_emit_no_device_bytes_and_mark_incomplete() -> None:
    """Falsified if a shared cgroup is split, duplicated, or assigned arbitrarily."""
    path = "/fixture-shared"
    records = (
        proc(100, 1, 1, (0, 0, 0, 0), path=path, exe="/usr/bin/app-a"),
        proc(101, 1, 2, (0, 0, 0, 0), path=path, exe="/usr/bin/app-b"),
    )
    observation = CgroupObservation(BOOT, 7, path, "available", (DevStat(7, "259:0", 40, 5),))
    frame, _ = aggregate_tick(None, tick(0, T0, records, cgroups=(observation,)))
    assert frame.device_deltas == ()
    assert {c.app_key for c in frame.coverage if c.device_state == "incomplete"} == {
        "exe:/usr/bin/app-a",
        "exe:/usr/bin/app-b",
    }


def test_new_epoch_restores_baselines_and_only_emits_the_new_difference() -> None:
    """Falsified by re-emitting the old cumulative baseline after a same-boot restart."""
    _, state = aggregate_tick(None, tick(0, T0, [proc(110, 1, 1, (100, 0, 20, 0))]))
    frame, next_state = aggregate_tick(
        state,
        tick(0, T1, [proc(110, 1, 1, (120, 0, 25, 0))], epoch=2),
    )
    assert frame.deltas[0].counters == (20, 0, 5, 0)
    assert next_state.processes[0].baseline == (120, 0, 25, 0)
    assert frame.coverage[0].gap_intervals == 1


def test_fifo_credits_are_componentwise_and_each_child_counts_once() -> None:
    """Falsified by wrong queue order, vector-wide min, or repeated correction counts."""
    _, state0 = aggregate_tick(
        None,
        tick(0, T0, [proc(120, 1, 1), proc(121, 120, 2), proc(122, 120, 3)]),
    )
    _, state1 = aggregate_tick(
        state0,
        tick(1, T1, [proc(120, 1, 1), proc(121, 120, 2, (0, 0, 5, 8)), proc(122, 120, 3, (0, 0, 7, 2))]),
    )
    frame2, state2 = aggregate_tick(
        state1, tick(2, T2, [proc(120, 1, 1, (0, 0, 10, 5))])
    )
    assert frame2.deltas[0].counters == (0, 0, 0, 0)
    assert frame2.coverage[0].reap_corrections == 2
    credits = state2.processes[0].credits
    assert [(credit.child.pid, credit.remaining) for credit in credits] == [
        (121, (0, 0, 0, 3)),
        (122, (0, 0, 2, 2)),
    ]
    frame3, state3 = aggregate_tick(
        state2, tick(3, T2 + 10_000_000_000, [proc(120, 1, 1, (0, 0, 12, 5))])
    )
    assert frame3.deltas[0].counters[2] == 0
    assert frame3.coverage[0].reap_corrections == 0
    assert [(credit.child.pid, credit.remaining) for credit in state3.processes[0].credits] == [
        (121, (0, 0, 0, 3)),
        (122, (0, 0, 0, 2)),
    ]


def test_checkpoint_state_has_schema_fields_and_deterministic_process_order() -> None:
    """Falsified if the frame cannot hand its staged sampler state to the store."""
    frame, state = aggregate_tick(
        None,
        tick(0, T0, [proc(202, 1, 2), proc(201, 1, 1)]),
    )
    checkpoint = frame.checkpoint_state
    assert checkpoint == state.to_checkpoint()
    assert tuple(p["pid"] for p in checkpoint["processes"]) == (201, 202)
    assert set(checkpoint) == {
        "version", "boot_id", "source", "epoch", "last_seq", "checkpoint_mono_ns", "processes", "devices"
    }
