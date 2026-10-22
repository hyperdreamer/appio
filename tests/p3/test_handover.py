"""Handover evidence, whole-delta exclusion and failure atomicity on schema v1."""
from dataclasses import replace
import sqlite3

import pytest

from appio.cgroups import CgroupObservation, DevStat
from appio.contracts import Window
from appio.durable import (
    Credit, DeviceBaseline, HandoverState, ProcessCheckpoint, ProcessRef,
    canonical_json, decode_handover,
)
from appio.handover import (
    HandoverOffer, IncompleteHandoverError, baseline_seed, capture_handover,
    seed_handover,
)
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.query import QueryService
from appio.quota import QuotaPruner
from appio.sampler import StaleFrameError, TickInput, aggregate_tick
from appio.store import AppIoStore

BOOT, APP, CGROUP = "boot", "exe:/usr/bin/fixture", "/fixture"
NS = 1_000_000_000
T0, T1, T2, T3 = (i * NS for i in (100, 110, 120, 130))


def process(counters=(0, 0, 0, 0), *, pid=410, start=100, ppid=1, denied=False):
    return StableProcRecord(
        ProcessIdentity(BOOT, pid, start), ppid, 1000, "/usr/bin/fixture", CGROUP,
        AppKeyInputs(CGROUP, "/usr/bin/fixture"),
        IoReadOutcome("eacces" if denied else "readable", None if denied else counters,
                      None, "EACCES" if denied else None),
    )


def group(read=100, write=20):
    return CgroupObservation(BOOT, 101, CGROUP, "available", (DevStat(101, "259:0", read, write),))


def tick(seq, at, counters=(0, 0, 0, 0), *, source="gui", epoch=1, records=None, devices=()):
    return TickInput(BOOT, source, epoch, seq, at, at + 1000 * NS,
                     (process(counters),) if records is None else tuple(records), tuple(devices))


def snapshot(db):
    return {name: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')), key=repr)
            for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")}


def initialized(path=":memory:", source="gui"):
    store = AppIoStore(path)
    store.start_boot(BOOT, 1, 0, 1000.0, source)
    store.start_collector(source, BOOT, T0)
    return store


def record(store, state, observation, *, seed=False):
    frame, staged = baseline_seed(observation) if seed else aggregate_tick(state, observation)
    store.record_tick(frame, staged)
    return frame, staged


def offer_for(state, incoming, start=T0):
    return HandoverOffer(state.source, incoming, state.epoch, state.last_seq, start, capture_handover(state))


def outgoing(store, source="gui"):
    _, state = record(store, None, tick(0, T0, (100, 20, 80, 10), source=source,
                                      devices=(group(1000, 200),)), seed=True)
    _, state = record(store, state, tick(1, T1, (110, 24, 88, 12), source=source,
                                       devices=(group(1010, 204),)))
    return state


def assert_totals(store, expected):
    db = store.connection
    for table in ("sample", "app_boot_total", "app_lifetime_total"):
        assert tuple(db.execute(f"SELECT coalesce(sum(rchar),0),coalesce(sum(wchar),0),coalesce(sum(read_bytes),0),coalesce(sum(write_bytes),0) FROM {table}").fetchone()) == expected
    for width in (60, 3600):
        assert tuple(db.execute("SELECT coalesce(sum(rchar),0),coalesce(sum(wchar),0),coalesce(sum(read_bytes),0),coalesce(sum(write_bytes),0) FROM rollup_app WHERE bucket_secs=?", (width,)).fetchone()) == expected
    for window in (Window("range", BOOT, 0, 140 * NS),
                   Window("range", BOOT, 0, 2 * 86400 * NS),
                   Window("range", BOOT, 0, 31 * 86400 * NS), Window("lifetime")):
        result, = QueryService(db).ranking(window, "total_bytes")
        assert (result.rchar, result.wchar, result.read_bytes, result.write_bytes) == expected


def test_seed_carries_credit_but_emits_no_pre_cutoff_bytes():
    # Falsifier: discarding a pending credit, its correction bit, or baseline.
    parent = ProcessCheckpoint(
        pid=410, start_ticks=100, ppid=1, parent=None,
        app_key=APP, launch_root=ProcessRef(410, 100), launch_id=72,
        baseline=(100, 20, 80, 10),
        credits=(Credit(ProcessRef(411, 101), 3, 9, (4, 2, 3, 1), 1),))
    state = HandoverState(BOOT, T1, (parent,), (DeviceBaseline(101, "259:0", 100, 20),))
    offer = HandoverOffer("gui", "iod", 3, 10, T0, state)
    frame, seeded = seed_handover(offer, incoming_epoch=7, wall_ns=T1 + 1000 * NS)
    assert (frame.source, frame.epoch, frame.seq) == ("iod", 7, 0)
    assert frame.deltas == frame.device_deltas == ()
    assert seeded.processes[0].baseline == (100, 20, 80, 10)
    credit = seeded.processes[0].credits[0]
    assert credit.remaining == (4, 2, 3, 1)
    assert credit.correction_counted is True
    assert seeded.processes[0].reap_incomplete is True
    assert seeded.processes[0].rate_unknown is True
    assert seeded.processes[0].launch_identity.launch_id == 0
    assert seeded.device_baselines == (((BOOT, 101, "259:0"), (100, 20)),)
    assert seeded.to_checkpoint() == frame.checkpoint_state
    assert frame.coverage[0].partial_intervals == 1
    assert frame.coverage[0].reap_incomplete is True
    assert decode_handover(canonical_json(capture_handover(seeded)), BOOT, T1).devices == state.devices


@pytest.mark.parametrize("source,incoming", [("gui", "iod"), ("iod", "gui")])
def test_cutoff_and_restored_baselines_choose_one_owner(source, incoming, tmp_path):
    # Falsifier: counting both sides, using lifetime counters, or rewinding retry.
    path = tmp_path / "handover.db"
    store = initialized(path, source)
    state = outgoing(store, source)
    offer = offer_for(state, incoming)
    epoch = store.install_handover(offer, wall_ns=T1 + 1000 * NS)
    assert (epoch.source, epoch.epoch, epoch.last_seq, epoch.allocated_mono_ns) == (incoming, 1, 0, T1)
    assert_totals(store, (10, 4, 8, 2))
    assert store.connection.execute("SELECT baselines_json FROM handover").fetchone()[0] == canonical_json(offer.state)
    before, published = snapshot(store.connection), store.sampler_state
    assert store.install_handover(offer, wall_ns=T1 + 1000 * NS) == epoch
    assert snapshot(store.connection) == before and store.sampler_state is published
    store.connection.close()
    store = AppIoStore(path)
    incoming_state = store.restore_checkpoint(incoming, BOOT)
    assert incoming_state.processes[0].baseline == (110, 24, 88, 12)
    assert incoming_state.last_seq == 0
    _, incoming_state = record(store, incoming_state, tick(1, T2, (130, 30, 100, 15),
        source=incoming, devices=(group(1030, 210),)))
    # A late outgoing frame is accepted as evidence, but its entire delta is excluded.
    record(store, state, tick(2, T2, (130, 30, 100, 15), source=source,
                              devices=(group(1030, 210),)))
    assert_totals(store, (30, 10, 20, 5))
    assert store.connection.execute("SELECT count(*) FROM gap WHERE reason='overlap_uncertain'").fetchone()[0] == 0
    assert [tuple(row) for row in store.connection.execute("SELECT source,read_bytes FROM sample WHERE read_bytes>0 ORDER BY ts_mono_ns")] == [(source, 8), (incoming, 12)]
    assert tuple(store.connection.execute("SELECT rbytes,wbytes FROM app_boot_dev_total").fetchone()) == (30, 10)
    before, published = snapshot(store.connection), store.sampler_state
    advanced = store.install_handover(offer, wall_ns=T3 + 1000 * NS)
    assert advanced.epoch == 1 and advanced.last_seq == 1
    assert snapshot(store.connection) == before and store.sampler_state is published


class CrashConnection(sqlite3.Connection):
    crash_after = None
    hit = False

    def execute(self, sql, parameters=(), /):
        if self.crash_after == "COMMIT" and sql == "COMMIT":
            self.hit = True
            raise KeyboardInterrupt("crash before commit")
        result = super().execute(sql, parameters)
        if self.crash_after and self.crash_after != "COMMIT" and self.crash_after in sql:
            self.hit = True
            raise KeyboardInterrupt("crash after write")
        return result


@pytest.mark.parametrize("crash_after", ["INSERT INTO handover", "INSERT INTO collector_interval",
    "INSERT INTO collector_epoch", "UPDATE collector_epoch", "INSERT INTO sample_coverage",
    "INSERT INTO sampler_checkpoint", "COMMIT"])
def test_every_handover_write_rolls_back_with_publication(tmp_path, crash_after):
    # Falsifier: committing the cutoff, epoch, receipt or seed independently.
    path = tmp_path / "crash.db"
    db = sqlite3.connect(path, factory=CrashConnection)
    store = initialized(db)
    state = outgoing(store)
    offer = offer_for(state, "iod")
    before, published = snapshot(db), store.sampler_state
    db.crash_after = crash_after
    with pytest.raises(KeyboardInterrupt, match="crash"):
        store.install_handover(offer, wall_ns=T1 + 1000 * NS)
    assert db.hit and not db.in_transaction
    assert snapshot(db) == before and store.sampler_state is published
    with sqlite3.connect(path) as observer:
        assert snapshot(observer) == before
    db.crash_after = None
    assert store.install_handover(offer, wall_ns=T1 + 1000 * NS).epoch == 1


def test_crash_between_collectors_excludes_both_whole_deltas(tmp_path):
    # Falsifier: resolving an absent cutoff by preferring or adding either source.
    path = tmp_path / "unclean.db"
    store = initialized(path)
    gui = outgoing(store)
    store.connection.close()  # Outgoing dies without installing its offer.
    store = AppIoStore(path)
    store.start_collector("iod", BOOT, T0)
    _, iod = record(store, None, tick(0, T0, (100, 20, 80, 10), source="iod",
                                     devices=(group(1000, 200),)), seed=True)
    _, iod = record(store, iod, tick(1, T1, (110, 24, 88, 12), source="iod",
                                    devices=(group(1010, 204),)))
    assert_totals(store, (0, 0, 0, 0))
    assert store.connection.execute("SELECT count(*) FROM handover").fetchone()[0] == 0
    assert [tuple(row) for row in store.connection.execute("SELECT from_mono_ns,to_mono_ns,reason FROM gap")] == [(T0, T1, "overlap_uncertain")]
    for window in (Window("range", BOOT, 0, 140 * NS), Window("lifetime")):
        result, = QueryService(store.connection).ranking(window, "total_bytes")
        assert not result.coverage.complete and result.coverage.overlap_uncertain_intervals == 1
    assert tuple(store.connection.execute("SELECT rbytes,wbytes FROM app_boot_dev_total").fetchone()) == (0, 0)
    # Ordinary quota expiry retains the uncertainty after removing raw evidence.
    assert QuotaPruner(store.connection, []).expire(BOOT, 2 * 86400 * NS).complete
    result, = QueryService(store.connection).ranking(Window("lifetime"), "total_bytes")
    assert result.total_bytes == 0 and result.coverage.overlap_uncertain_intervals > 0


def test_uncertain_overlap_excludes_rate_unknown_deltas_ending_at_its_boundary():
    # Falsifier: treating a restored interval as a point at the half-open gap's end.
    store = initialized()
    record(store, None, tick(0, T0, (100, 20, 80, 10), devices=(group(1000, 200),)), seed=True)
    gui = store.restore_checkpoint("gui", BOOT)
    record(store, gui, tick(1, T1, (110, 24, 88, 12), devices=(group(1010, 204),)))
    store.start_collector("iod", BOOT, T0)
    record(store, None, tick(0, T0, (100, 20, 80, 10), source="iod", devices=(group(1000, 200),)), seed=True)
    iod = store.restore_checkpoint("iod", BOOT)
    record(store, iod, tick(1, T1, (110, 24, 88, 12), source="iod", devices=(group(1010, 204),)))
    assert_totals(store, (0, 0, 0, 0))
    assert tuple(store.connection.execute("SELECT rbytes,wbytes FROM app_boot_dev_total").fetchone()) == (0, 0)
    result, = QueryService(store.connection).ranking(Window("lifetime"), "total_bytes")
    assert not result.coverage.complete and result.coverage.overlap_uncertain_intervals == 1


def test_unclean_gap_baselines_skip_unobserved_work():
    # Falsifier: treating the counters accumulated while no owner ran as observed bytes.
    store = initialized()
    outgoing(store)
    store.start_collector("iod", BOOT, T2)
    store.record_gap(BOOT, "iod", 1, 0, T1, T2)
    _, iod = record(store, None, tick(0, T2, (900, 400, 800, 200), source="iod"), seed=True)
    record(store, iod, tick(1, T3, (907, 403, 805, 201), source="iod"))
    assert_totals(store, (17, 7, 13, 3))
    result, = QueryService(store.connection).ranking(Window("lifetime"), "total_bytes")
    assert not result.coverage.complete and result.coverage.gap_intervals > 0
    assert [tuple(row) for row in store.connection.execute("SELECT from_mono_ns,to_mono_ns FROM collector_interval ORDER BY from_mono_ns")] == [(T0, T1), (T2, T3)]


@pytest.mark.parametrize("change", ["epoch", "seq", "boot", "cutoff", "baseline", "start", "source"])
def test_conflicting_or_forged_offer_mutates_nothing(change):
    # Falsifier: trusting offer chronology without matching committed evidence.
    store = initialized()
    state = outgoing(store)
    offer = offer_for(state, "iod")
    if change == "epoch":
        offer = replace(offer, outgoing_epoch=2)
    elif change == "seq":
        offer = replace(offer, outgoing_seq=2)
    elif change == "boot":
        offer = replace(offer, state=replace(offer.state, boot_id="other"))
    elif change == "cutoff":
        offer = replace(offer, state=replace(offer.state, cutoff_mono_ns=T2))
    elif change == "baseline":
        offer = replace(offer, state=replace(offer.state, processes=(replace(offer.state.processes[0], baseline=(999, 0, 0, 0)),)))
    elif change == "start":
        offer = replace(offer, interval_start_mono_ns=T0 - 1)
    else:
        offer = replace(offer, from_source="iod", to_source="gui")
    before, published = snapshot(store.connection), store.sampler_state
    with pytest.raises(ValueError):
        store.install_handover(offer, wall_ns=T2 + 1000 * NS)
    assert snapshot(store.connection) == before and store.sampler_state is published


def test_conflicting_same_cutoff_json_and_outer_transaction_are_rejected():
    # Falsifier: overwrite-on-conflict or joining/committing unrelated caller work.
    store = initialized()
    offer = offer_for(outgoing(store), "iod")
    store.install_handover(offer, wall_ns=T1 + 1000 * NS)
    conflict = replace(offer, state=replace(offer.state, devices=()))
    before = snapshot(store.connection)
    with pytest.raises(ValueError):
        store.install_handover(conflict, wall_ns=T1 + 1000 * NS)
    assert snapshot(store.connection) == before
    store.connection.execute("BEGIN IMMEDIATE")
    store.connection.execute("UPDATE boot SET started_wall=5")
    with pytest.raises(ValueError, match="transaction"):
        store.install_handover(offer, wall_ns=T1 + 1000 * NS)
    assert store.connection.in_transaction
    store.connection.rollback()
    assert snapshot(store.connection) == before


def test_incomplete_preceding_chain_and_pruned_corrections_are_refused():
    # Falsifier: the first cutoff retroactively claims that all earlier history was GUI-owned.
    store = initialized()
    gui = outgoing(store)
    store.start_collector("iod", BOOT, T0)
    _, iod = record(store, None, tick(0, T0, source="iod"), seed=True)
    record(store, iod, tick(1, T1, (4, 2, 3, 1), source="iod"))
    QuotaPruner(store.connection, []).expire(BOOT, 2 * 86400 * NS)
    before = snapshot(store.connection)
    with pytest.raises(IncompleteHandoverError):
        store.install_handover(offer_for(gui, "iod"), wall_ns=T1 + 1000 * NS)
    assert snapshot(store.connection) == before


def test_adjacent_transfers_remap_local_launch_ids_and_exclude_straddling_devices():
    # Falsifier: moving local launch IDs across stores or splitting a cumulative delta at a cutoff.
    store = initialized()
    gui = outgoing(store)
    first = store.install_handover(offer_for(gui, "iod"), wall_ns=T1 + 1000 * NS)
    iod = store.sampler_state
    _, iod = record(store, iod, tick(1, T2, (115, 26, 92, 13), source="iod", devices=(group(1015, 206),)))
    # The first post-transfer observation re-establishes process rate provenance.
    assert iod.processes[0].rate_unknown is False
    _, iod = record(store, iod, tick(2, T3, (120, 28, 96, 14), source="iod", devices=(group(1020, 208),)))
    second = store.install_handover(offer_for(iod, "gui", T1), wall_ns=T3 + 1000 * NS)
    assert (first.epoch, second.epoch) == (1, 2)
    record(store, store.sampler_state, tick(1, T3 + 5 * NS, (130, 32, 104, 16), epoch=2, devices=(group(1030, 212),)))
    # An old outgoing observation covering [T3,T3+5s) crosses its cutoff at T3.
    record(store, iod, tick(3, T3 + 5 * NS, (150, 40, 120, 20), source="iod", devices=(group(1050, 220),)))
    assert_totals(store, (30, 12, 24, 6))
    assert tuple(store.connection.execute("SELECT rbytes,wbytes FROM app_boot_dev_total").fetchone()) == (30, 12)
    assert store.connection.execute("SELECT count(*) FROM launch").fetchone()[0] == 1
    assert store.connection.execute("SELECT count(*) FROM gap WHERE reason='overlap_uncertain'").fetchone()[0] == 0


def test_first_handover_cannot_reassign_nonoverlapping_earlier_history():
    # Falsifier: checking only overlap in the outgoing epoch, while losing older bytes in raw queries.
    store = initialized(source="iod")
    outgoing(store, "iod")
    store.start_collector("gui", BOOT, T2)
    _, gui = record(store, None, tick(0, T2, (100, 20, 80, 10)), seed=True)
    _, gui = record(store, gui, tick(1, T3, (110, 24, 88, 12)))
    before, published = snapshot(store.connection), store.sampler_state
    with pytest.raises(IncompleteHandoverError):
        store.install_handover(offer_for(gui, "iod", T2), wall_ns=T3 + 1000 * NS)
    assert snapshot(store.connection) == before and store.sampler_state is published
    assert_totals(store, (20, 8, 16, 4))


def test_install_cannot_rewind_an_incoming_epoch_without_a_checkpoint():
    # Falsifier: allocating the incoming epoch at a cutoff older than its previous start.
    store = initialized()
    gui = outgoing(store)
    store.start_collector("iod", BOOT, T2)
    before, published = snapshot(store.connection), store.sampler_state
    with pytest.raises(ValueError):
        store.install_handover(offer_for(gui, "iod"), wall_ns=T1 + 1000 * NS)
    assert snapshot(store.connection) == before and store.sampler_state is published


def test_pruned_cutoff_evidence_requires_an_unclean_transition():
    # Falsifier: accepting a cutoff after its original interval provenance has expired.
    store = initialized()
    gui = outgoing(store)
    assert QuotaPruner(store.connection, []).expire(BOOT, 2 * 86400 * NS).complete
    assert store.connection.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 0
    before, published = snapshot(store.connection), store.sampler_state
    with pytest.raises(IncompleteHandoverError):
        store.install_handover(offer_for(gui, "iod"), wall_ns=T1 + 1000 * NS)
    assert snapshot(store.connection) == before and store.sampler_state is published


def test_rate_unknown_cutoff_is_refused_without_changing_any_tier():
    # Falsifier: committing a cutoff that filters raw bytes but keeps their totals.
    store = initialized()
    store.install_handover(offer_for(outgoing(store), "iod"), wall_ns=T1 + 1000 * NS)
    _, iod = record(store, store.sampler_state, tick(1, T2, (120, 28, 96, 14), source="iod"))
    assert tuple(store.connection.execute(
        "SELECT interval_start_mono_ns,observed_ns FROM sample WHERE source='iod'"
    ).fetchone()) == (None, None)
    before, published = snapshot(store.connection), store.sampler_state
    with pytest.raises(IncompleteHandoverError, match="interval evidence"):
        store.install_handover(offer_for(iod, "gui", T1), wall_ns=T2 + 1000 * NS)
    assert snapshot(store.connection) == before and store.sampler_state is published
    assert_totals(store, (20, 8, 16, 4))


@pytest.mark.parametrize("source,incoming", [("gui", "iod"), ("iod", "gui")])
def test_credit_survives_two_parent_increments_eacces_disappearance_and_pid_reuse(source, incoming):
    # Falsifier: clearing the FIFO on transfer/EACCES or subtracting it from a reused PID.
    parent = ProcessCheckpoint(410, 100, 1, None, APP, ProcessRef(410, 100), 72,
                               (100, 20, 80, 10), (Credit(ProcessRef(411, 101), 3, 9, (4, 2, 3, 1), 1),))
    another = ProcessCheckpoint(420, 200, 1, None, APP, ProcessRef(420, 200), 72, None, ())
    offer = HandoverOffer(source, incoming, 3, 10, T0, HandoverState(BOOT, T1, (parent, another), ()))
    _, state = seed_handover(offer, incoming_epoch=7, wall_ns=T1 + 1000 * NS)
    assert state.processes[1].baseline is None
    assert len(state.launches) == 2
    frame, denied = aggregate_tick(state, tick(1, T2, source=incoming, epoch=7, records=[process(denied=True)]))
    assert denied.processes[0].credits == state.processes[0].credits
    assert frame.coverage[0].inaccessible_pids == 1
    frame, after = aggregate_tick(denied, tick(2, T3, (102, 21, 81, 10), source=incoming, epoch=7))
    assert frame.deltas[0].counters == (0, 0, 0, 0)
    assert after.processes[0].credits[0].remaining == (2, 1, 2, 1)
    frame, after = aggregate_tick(after, tick(3, T3 + 10 * NS, (105, 23, 84, 12), source=incoming, epoch=7))
    assert frame.deltas[0].counters == (1, 1, 1, 1)
    assert frame.coverage[0].reap_corrections == 0
    assert after.processes[0].credits == ()
    frame, _ = aggregate_tick(denied, tick(2, T3, source=incoming, epoch=7, records=[]))
    assert frame.coverage[0].reap_unapplied == (4, 2, 3, 1)
    frame, _ = aggregate_tick(denied, tick(2, T3, source=incoming, epoch=7, records=[process((1, 1, 1, 1), start=999)]))
    assert frame.deltas[0].counters == (1, 1, 1, 1)
    assert frame.coverage[0].reap_unapplied == (4, 2, 3, 1)


def test_baseline_seed_preserves_only_same_authority_credits():
    # Falsifier: importing an unverified collector's credits into the fresh authority.
    store = initialized()
    _, state = record(store, None, tick(0, T0, records=[process(), process((9, 5, 7, 3), pid=411, start=101, ppid=410)]))
    _, state = record(store, state, tick(1, T1, (1, 1, 1, 1)))
    assert state.processes[0].credits[0].remaining == (8, 4, 6, 2)
    frame, same = baseline_seed(tick(0, T2, (20, 20, 20, 20), epoch=2), previous=state)
    assert frame.deltas == frame.device_deltas == ()
    assert same.processes[0].credits[0].remaining == (8, 4, 6, 2)
    frame, other = baseline_seed(tick(0, T2, (20, 20, 20, 20), source="iod"), previous=state)
    assert frame.deltas == frame.device_deltas == () and other.processes[0].credits == ()
    assert other.processes[0].baseline == (20, 20, 20, 20)
    with pytest.raises(ValueError):
        baseline_seed(tick(1, T2))


def test_capture_rejects_boot_aliases_before_losing_identity_fields():
    # Falsifier: serializing only pid/start and silently changing an identity's boot.
    store = initialized()
    state = outgoing(store)
    broken = replace(state, processes=(replace(state.processes[0], identity=ProcessIdentity("another-boot", 410, 100)),))
    with pytest.raises(ValueError):
        capture_handover(broken)
