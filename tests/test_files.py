"""Synthetic file-hour tests. Each case names the implementation break it catches."""
from dataclasses import replace
from itertools import permutations
import json
import random

import pytest

from appio.contracts import (
    CounterOverflowError, FILE_HOUR_NS, FileCoverageInput, FileObservation,
    HookHits, HourlyFileSnapshot, SQLITE_INT_MAX, UnknownOwnerInput,
)
from appio.durable import FileEntry, Receipt, canonical_json, decode_file_hour_state
from appio.files import HourlyAccumulator, hourly_file_budget

APP = "unit:app-demo.service"
ZERO = FileCoverageInput(0, 0, 0, 0, 0, 0, 0, 0, 0, HookHits(0, 0, 0, 0, 0, 0), 0, 0, True)
R = Receipt("gui", 1, 0)


def observation(inode=1, **changes):
    values = dict(app_key=APP, fs_uuid="1111-aaaa", dev="259:0", inode=inode,
                  generation=7, basename=f"file-{inode}", state="resolved", identity_weak=False,
                  reads=1, writes=0, bytes_r=10, bytes_w=0, last_seen_mono_ns=120_000_000_000)
    values.update(changes)
    return FileObservation(**values)


def test_synthetic_vector_preserves_measured_omissions_and_failed_attempts_separately():
    # Falsifier: fold overflow attempts into counted omissions or allocate unknown bytes to APP.
    acc = HourlyAccumulator("boot-A", 0)
    coverage = replace(ZERO, counted_omitted_files=1, counted_omitted_bytes_r=20,
                       counted_omitted_bytes_w=30, uncounted_overflow_files=1,
                       uncounted_overflow_bytes_r=5, uncounted_overflow_bytes_w=7,
                       skipped_nonfile=3, skipped_unsupported_fs=1)
    acc.add_observation(R, APP, observation(1001, reads=3, writes=1, bytes_r=90, bytes_w=40), coverage)
    acc.add_observation(R, APP, observation(1001, generation=8, reads=1, writes=2, bytes_r=10, bytes_w=20), coverage)
    acc.add_observation(R, APP, observation(1002, generation=3, reads=1, writes=1, bytes_r=0, bytes_w=60), coverage)
    acc.add_unknown_owner(R, UnknownOwnerInput(2, 11, 13, 0, 0, True))
    snap = acc.finalize()
    assert isinstance(snap, HourlyFileSnapshot)
    key, rows, cov = snap.apps[0]
    assert key == APP
    assert tuple(sum(getattr(row, field) for row in rows) for field in ("bytes_r", "bytes_w", "reads", "writes")) == (100, 120, 5, 4)
    assert (cov.counted_omitted_files, cov.counted_omitted_bytes_r, cov.counted_omitted_bytes_w) == (1, 20, 30)
    assert (cov.uncounted_overflow_files, cov.uncounted_overflow_bytes_r, cov.uncounted_overflow_bytes_w) == (1, 5, 7)
    assert (snap.unknown_owner.overflow_files, snap.unknown_owner.overflow_bytes_r, snap.unknown_owner.overflow_bytes_w) == (2, 11, 13)
    assert (cov.skipped_nonfile, cov.skipped_unsupported_fs) == (3, 1)


def test_two_collectors_share_one_candidate_budget_and_pending_keeps_losers():
    # Falsifier: select top-N per drain before merging A's contributions.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(1, reads=2, bytes_r=60, bytes_w=40), ZERO)
    acc.add_observation(Receipt("iod", 1, 1), APP, observation(1, reads=3, bytes_r=40, bytes_w=60, last_seen_mono_ns=200_000_000_000), ZERO)
    acc.add_observation(R, APP, observation(2, reads=6, bytes_r=150), ZERO)
    snap = acc.finalize(top_n=1)
    assert [(r.inode, r.bytes_r, r.bytes_w, r.reads) for r in snap.apps[0][1]] == [(1, 100, 100, 5)]
    assert (snap.apps[0][2].counted_omitted_files, snap.apps[0][2].counted_omitted_bytes_r) == (1, 150)
    state = decode_file_hour_state(acc.state_json(), "boot-A", 0)
    assert len(state.apps[0].files) == 2
    assert state.apps[0].last_receipt == Receipt("iod", 1, 1)
    assert HourlyAccumulator.from_state(state).finalize(1).apps == snap.apps


def test_top_two_and_weak_strong_cutoff_are_invariant_under_all_permutations():
    # Falsifier: omit identity_weak/fs_uuid, or use accesses before bytes.
    candidates = [observation(200, bytes_r=50, bytes_w=50, reads=5),
                  observation(100, dev="259:1", bytes_r=70, bytes_w=30, reads=5),
                  observation(1, bytes_r=50, bytes_w=49, reads=9)]
    for ordered in permutations(candidates):
        acc = HourlyAccumulator("boot-A", 0)
        for row in ordered:
            acc.add_observation(R, APP, row, ZERO)
        snap = acc.finalize(2)
        assert [(r.dev, r.inode) for r in snap.apps[0][1]] == [("259:0", 200), ("259:1", 100)]
        assert snap.apps[0][2].counted_omitted_bytes_r == 50
    a = observation(9, fs_uuid="aaaa", generation=4, bytes_r=80, bytes_w=20, reads=3, writes=2)
    b = replace(a, fs_uuid="bbbb", identity_weak=True)
    for ordered in permutations((a, b)):
        acc = HourlyAccumulator("boot-A", 0)
        for row in ordered:
            acc.add_observation(R, APP, row, ZERO)
        assert [(r.fs_uuid, r.identity_weak) for r in acc.finalize(1).apps[0][1]] == [("aaaa", False)]


def test_production_top_100_is_a_total_order_for_128_candidates():
    # Falsifier: stable input ordering replaces any part of the full tie key at the cutoff.
    candidates = [observation(i // 4, fs_uuid=("é", "z")[i % 2], identity_weak=bool(i % 4 // 2),
                              bytes_r=10, reads=1, basename=f"name-{i // 4}") for i in range(128)]
    # Eight high-byte/high-access vectors deliberately reverse identity preference.
    candidates += [observation(1000 + i, bytes_r=11, reads=0) for i in range(4)]
    candidates += [observation(2000 + i, bytes_r=10, reads=2) for i in range(4)]
    expected = [(1000+i, False, "1111-aaaa") for i in range(4)] + [(2000+i, False, "1111-aaaa") for i in range(4)]
    expected += [(inode, weak, uuid) for inode in range(23) for weak in (False, True) for uuid in ("z", "é")]
    baseline = None
    for seed in range(10):
        ordered = list(candidates)
        random.Random(seed).shuffle(ordered)
        acc = HourlyAccumulator("boot-A", 0)
        for row in ordered:
            acc.add_observation(R, APP, row, ZERO)
        snap = acc.finalize()
        assert [(r.inode, r.identity_weak, r.fs_uuid) for r in snap.apps[0][1]] == expected
        assert snap.apps[0][2].counted_omitted_files == 36
        if baseline is not None:
            assert snap.apps == baseline
        baseline = snap.apps


def test_names_aliases_receipts_and_e1_app_triples_have_canonical_order():
    # Falsifier: basename becomes an aggregation key, or E-1 drops the app_key slot.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, "unit:z.service", None, ZERO)
    for row in (observation(42, basename="b.txt", bytes_r=0, bytes_w=40, reads=1, writes=2, last_seen_mono_ns=200_000_000_000),
                observation(42, basename="a.txt", bytes_r=60, reads=2, last_seen_mono_ns=100_000_000_000)):
        acc.add_observation(R, APP, row, ZERO)
    snap = acc.finalize()
    assert [entry[0] for entry in snap.apps] == [APP, "unit:z.service"]
    assert all(len(entry) == 3 for entry in snap.apps)
    row, = snap.apps[0][1]
    assert (row.basename, row.reads, row.writes, row.bytes_r, row.bytes_w, row.last_seen_mono_ns) == ("a.txt", 3, 2, 60, 40, 200_000_000_000)
    state = json.loads(acc.state_json())
    assert set(state) == {"version", "boot_id", "hour_start_mono_ns", "apps", "unknown_owner"}
    assert state["apps"][0]["files"][0]["aliases"] == ["b.txt"]
    assert state["unknown_owner"]["last_receipt"] is None
    assert acc.state_json() == canonical_json(state)


def test_missing_uuid_uses_weak_durable_identity_without_excluding_bytes():
    # FileObservation's upstream constructor currently rejects ''. Durable FileEntry permits it.
    acc = HourlyAccumulator("boot-A", 0)
    entry = FileEntry("", "259:0", 9, 0, 1, "weak", (), "unresolved", 1, 0, 8, 0, 10)
    acc.add_observation(R, APP, entry, ZERO)
    row, = acc.finalize().apps[0][1]
    assert (row.fs_uuid, row.identity_weak, row.bytes_r) == ("", True, 8)
    assert acc.finalize().apps[0][2].weak_identity_rows == 1


def test_counter_overflow_and_bad_time_leave_accumulator_unchanged():
    # Falsifier: mutate the counters/coverage/receipts before validating their sums.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(bytes_r=SQLITE_INT_MAX), ZERO)
    before = acc.state_json()
    with pytest.raises(CounterOverflowError):
        acc.add_observation(Receipt("gui", 1, 1), APP, observation(bytes_r=1), replace(ZERO, skipped_nonfile=1))
    assert acc.state_json() == before
    with pytest.raises(ValueError):
        acc.add_observation(R, APP, observation(last_seen_mono_ns=FILE_HOUR_NS), ZERO)
    assert acc.state_json() == before


@pytest.mark.parametrize("changes", [
    {"dev": "259:1"},
    {"inode": 2},
    {"generation": 8},
    {"identity_weak": True},
    {"fs_uuid": "é"},
])
def test_each_full_identity_component_decides_a_production_cutoff(changes):
    # Falsifier: leaving any identity component out admits input order at row 100.
    base = observation(1, fs_uuid="z", basename="same", reads=1, bytes_r=10)
    other = replace(base, **changes)
    winners = [observation(10_000+i, bytes_r=20) for i in range(99)]
    for tie in permutations((other, base)):
        acc = HourlyAccumulator("boot-A", 0)
        for row in (*winners, *tie):
            acc.add_observation(R, APP, row, ZERO)
        selected = acc.finalize().apps[0][1][-1]
        assert (selected.dev, selected.inode, selected.generation, selected.identity_weak, selected.fs_uuid) == (base.dev, base.inode, base.generation, base.identity_weak, base.fs_uuid)


def test_latest_accepted_receipt_advances_on_coverage_only_and_collector_handover():
    # Falsifier: file LAST or unrelated stream sequence magnitudes select provenance.
    acc = HourlyAccumulator("boot-A", 0)
    early = Receipt("gui", 9, 100)
    later = Receipt("gui", 9, 101)
    handed_over = Receipt("iod", 1, 0)
    acc.add_observation(early, APP, observation(last_seen_mono_ns=1000), ZERO)
    acc.add_observation(later, APP, None, replace(ZERO, skipped_nonfile=1))
    assert acc.state().apps[0].last_receipt == later
    acc.add_observation(handed_over, APP, observation(last_seen_mono_ns=1001), ZERO)
    acc.add_unknown_owner(early, UnknownOwnerInput(1, 2, 3, 0, 0, True))
    acc.add_unknown_owner(handed_over, UnknownOwnerInput(1, 4, 5, 0, 0, True))
    assert acc.state().apps[0].last_receipt == handed_over
    assert acc.state().unknown_owner.last_receipt == handed_over
    restored = HourlyAccumulator.from_state(acc.state())
    assert restored.state() == acc.state()


def test_receipt_coverage_hooks_sum_once_per_frame_and_incompleteness_survives_restore():
    # Falsifier: copy frame coverage per candidate, omit hook counters, or lose a false completeness bit.
    acc = HourlyAccumulator("boot-A", 0)
    cov = replace(ZERO, hook_hits=HookHits(1, 2, 3, 4, 5, 6), gap_intervals=1, complete=False)
    acc.add_observation(R, APP, observation(1), cov)
    acc.add_observation(R, APP, observation(2), cov)
    acc.add_observation(Receipt("iod", 1, 1), APP, None, cov)
    restored = HourlyAccumulator.from_state(decode_file_hour_state(acc.state_json(), "boot-A", 0))
    result = restored.finalize().apps[0][2]
    assert result.hook_hits == HookHits(2, 4, 6, 8, 10, 12)
    assert result.gap_intervals == 2
    assert not result.complete


def test_hourly_14_day_budget_matches_exact_specification_products():
    # Falsifier: use a drain cadence, binary MB, or count only one snapshot per day.
    assert hourly_file_budget(apps=107, top_n=100, days=14, payload_bytes=88) == {
        "rows_per_snapshot": 10700, "bytes_per_snapshot": 941600,
        "rows_per_day": 256800, "bytes_per_day": 22598400,
        "retained_rows": 3595200, "retained_payload_bytes": 316377600,
    }


def test_weak_and_strong_equal_uuid_identities_and_names_never_merge():
    """Falsifier: remove identity_weak from either hourly or captured-name key."""
    acc = HourlyAccumulator("boot-A", 0)
    for weak, amount in ((True, 11), (False, 17)):
        acc.add_observation(R, APP, observation(42, fs_uuid="same-uuid", identity_weak=weak,
                            basename="same-name", bytes_r=amount, bytes_w=0), ZERO)
    pending = acc.state().apps[0].files
    assert [(e.identity_weak, e.bytes_r) for e in pending] == [(0, 17), (1, 11)]
    assert len(acc.finalize().captured_names) == 2
    restored = HourlyAccumulator.from_state(decode_file_hour_state(acc.state_json(), "boot-A", 0))
    assert [(r.identity_weak, r.bytes_r) for r in restored.finalize().apps[0][1]] == [
        (False, 17), (True, 11)]


def test_canonical_name_is_bytewise_least_not_first_last_or_casefolded():
    """Falsifier: key by name, choose last/first/max, normalize or sort casefolded."""
    acc = HourlyAccumulator("boot-A", 0)
    for seq, (name, time) in enumerate((("z.txt", 30), ("a.txt", 20),
                                      ("Z.txt", 10), ("é.txt", 40),
                                      ("e\u0301.txt", 50))):
        acc.add_observation(Receipt("iod", 1, seq), APP,
                            observation(42, basename=name, bytes_r=1, bytes_w=0,
                                        last_seen_mono_ns=time), ZERO)
    row, = acc.state().apps[0].files
    assert row.basename == "Z.txt"
    assert row.aliases == ("a.txt", "e\u0301.txt", "z.txt", "é.txt")
    assert (row.bytes_r, row.last_seen_mono_ns) == (5, 50)
    restored = HourlyAccumulator.from_state(decode_file_hour_state(acc.state_json(), "boot-A", 0))
    assert restored.state().apps[0].files == (row,)
