from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from appio.cgroups import CgroupObservation, DevStat
from appio.contracts import CounterOverflowError
from appio.durable import canonical_json, decode_sampler_checkpoint
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.reconcile import Reconciliation
from appio.sampler import SamplerState, StaleFrameError, TickInput, aggregate_tick
from appio.store import AppIoStore

BOOT = "11111111-2222-4333-8444-555555555555"
APP = "unit:app-chromium@fixture.service"
CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-chromium@fixture.service"
NS = 1_000_000_000
T0, T1, T2 = 100 * NS, 110 * NS, 120 * NS


def process(pid=410, ppid=1, start=100, counters=(0, 0, 0, 0), *, path=CGROUP, boot=BOOT, denied=False):
    return StableProcRecord(
        ProcessIdentity(boot, pid, start), ppid, 1000, "/usr/bin/fixture", path,
        AppKeyInputs(path, "/usr/bin/fixture"),
        IoReadOutcome("eacces" if denied else "readable", None if denied else counters,
                      dumpable=False if denied else None, errno_name="EACCES" if denied else None),
    )


def tick(seq, at, processes=(), *, epoch=1, boot=BOOT, source="gui", cgroups=(), suspend=(), offset=1000 * NS):
    return TickInput(boot, source, epoch, seq, at, at + offset, tuple(processes), tuple(cgroups), suspend)


def group(r0, w0, r1=0, w1=0):
    return CgroupObservation(BOOT, 101, CGROUP, "available",
                             (DevStat(101, "259:0", r0, w0), DevStat(101, "259:1", r1, w1)))


def initialized(path=":memory:"):
    store = AppIoStore(path)
    store.start_boot(BOOT, 1, 0, 1000.0, "gui")
    store.start_collector("gui", BOOT, T0)
    return store


def record(store, prior, observation):
    frame, staged = aggregate_tick(prior, observation)
    store.record_tick(frame, staged)
    return frame, staged


def snapshot(connection):
    """All durable rows, including receipts, checkpoints, metadata and diagnostics."""
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name")]
    return {table: sorted((tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')), key=repr)
            for table in tables}


def coverage(connection, table, where="", values=()):
    return [json.loads(row[0]) for row in connection.execute(f"SELECT coverage_json FROM {table} {where}", values)]


def chromium_frames(store):
    observations = (
        tick(0, T0, [process(), process(411, 410, 101), process(412, 1, 102), process(413, 1, 103, denied=True)],
             cgroups=(group(1000, 200, 300, 500),)),
        tick(1, T1, [process(counters=(1000, 500, 100, 70)), process(411, 410, 101, (500, 100, 50, 10)),
                      process(412, 1, 102, (300, 200, 30, 10)), process(413, 1, 103, denied=True)],
             cgroups=(group(1100, 250, 330, 520),)),
        tick(2, T2, [process(counters=(1600, 650, 170, 90)), process(412, 1, 102, (300, 200, 30, 10)),
                      process(413, 1, 103, denied=True)], cgroups=(group(1120, 270, 350, 550),)),
    )
    prior = None
    frames = []
    for observation in observations:
        frame, prior = record(store, prior, observation)
        frames.append(frame)
    return frames, prior


def test_three_tick_store_acceptance_and_every_rollup_matches_raw(tmp_path):
    """Falsified by reap/device duplication, launch-local peaks, or an hour adding minute totals."""
    store = initialized(tmp_path / "samples.sqlite")
    frames, state = chromium_frames(store)
    db = store.connection
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert len(db.execute("PRAGMA table_list").fetchall()) >= 27
    assert tuple(db.execute("SELECT rchar,wchar,read_bytes,write_bytes FROM app_boot_total").fetchone()) == (1900, 850, 200, 100)
    assert tuple(db.execute("SELECT rchar,wchar,read_bytes,write_bytes FROM app_lifetime_total").fetchone()) == (1900, 850, 200, 100)
    devices = [tuple(row) for row in db.execute("SELECT dev,rbytes,wbytes FROM app_boot_dev_total ORDER BY dev")]
    assert devices == [("259:0", 120, 70), ("259:1", 50, 50)]
    reconciliation = Reconciliation(200, 100, 300, 170, 120, 290)
    assert (reconciliation.residual_read, reconciliation.residual_write, reconciliation.residual_total) == (30, -20, 10)
    receipts = coverage(db, "sample_coverage", "ORDER BY seq")
    assert [item["inaccessible_pids"] for item in receipts] == [1, 1, 1]
    assert [item["reap_corrections"] for item in receipts] == [0, 0, 1]
    total = coverage(db, "app_boot_total")[0]
    assert (total["inaccessible_pids"], total["reap_corrections"], total["device_state"]) == (3, 1, "available")
    assert coverage(db, "app_lifetime_total") == [total]
    assert tuple(db.execute("SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM app_boot_total").fetchone()) == (18, 9, 27)
    for width in (60, 3600):
        for row in db.execute("SELECT bucket_start_mono_ns,rchar,wchar,read_bytes,write_bytes,coverage_json FROM rollup_app WHERE bucket_secs=?", (width,)):
            start, *rest = tuple(row)
            raw = db.execute("SELECT coalesce(sum(rchar),0),coalesce(sum(wchar),0),coalesce(sum(read_bytes),0),coalesce(sum(write_bytes),0) FROM sample WHERE ts_mono_ns>=? AND ts_mono_ns<?", (start, start + width * NS)).fetchone()
            assert tuple(rest[:4]) == tuple(raw)
            source = coverage(db, "sample_coverage", "WHERE ts_mono_ns>=? AND ts_mono_ns<?", (start, start + width * NS))
            rolled = json.loads(rest[4])
            for field in ("inaccessible_pids", "reap_corrections", "partial_intervals"):
                assert rolled[field] == sum(item[field] for item in source)
        assert tuple(db.execute("SELECT sum(read_bytes),sum(write_bytes) FROM rollup_app WHERE bucket_secs=?", (width,)).fetchone()) == (200, 100)
        assert tuple(db.execute("SELECT sum(rbytes),sum(wbytes) FROM rollup_dev WHERE bucket_secs=?", (width,)).fetchone()) == (170, 120)
    checkpoint = db.execute("SELECT source,boot_id,epoch,seq,checkpoint_mono_ns,state_json FROM sampler_checkpoint").fetchone()
    decoded = decode_sampler_checkpoint(checkpoint[5], dict(zip(("source", "boot_id", "epoch", "seq", "checkpoint_mono_ns"), checkpoint[:5])))
    assert canonical_json(decoded) == canonical_json(frames[-1].checkpoint_state)
    assert store.sampler_state is state
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_multiple_attributed_timestamps_and_zero_task_receipts():
    """Falsified by collapsed timestamps or copying coverage counts into the suspend bucket."""
    store = initialized()
    _, state = record(store, None, tick(0, 110 * NS, [process()]))
    frame, state = record(store, state, tick(1, 120 * NS, [process(counters=(0, 0, 40, 20)), process(411, 410, 101, (0, 0, 7, 3))], suspend=((115 * NS, 119 * NS),)))
    rows = [tuple(row) for row in store.connection.execute("SELECT ts_mono_ns,observed_mono_ns,interval_start_mono_ns,observed_ns,read_bytes,write_bytes,spans_suspend FROM sample WHERE seq=1 ORDER BY ts_mono_ns")]
    assert rows == [(110 * NS, 120 * NS, 110 * NS, 6 * NS, 40, 20, 1), (120 * NS, 120 * NS, None, None, 7, 3, 0)]
    assert store.connection.execute("SELECT count(*) FROM sample_coverage WHERE seq=1").fetchone()[0] == 1
    minute = {row[0]: (row[1], row[2], json.loads(row[3])) for row in store.connection.execute("SELECT bucket_start_mono_ns,read_bytes,write_bytes,coverage_json FROM rollup_app WHERE bucket_secs=60")}
    assert minute[60 * NS][:2] == (40, 20)
    assert minute[120 * NS][:2] == (7, 3)
    assert minute[60 * NS][2]["partial_intervals"] == 0
    assert minute[60 * NS][2]["spans_suspend_intervals"] == 0
    assert minute[120 * NS][2]["partial_intervals"] == 1
    assert minute[120 * NS][2]["spans_suspend_intervals"] == 1
    record(store, state, tick(2, 130 * NS, []))
    assert store.connection.execute("SELECT count(*) FROM sample WHERE seq=2").fetchone()[0] == 0
    assert store.connection.execute("SELECT count(*) FROM sample_coverage WHERE seq=2").fetchone()[0] == 1
    assert all(not any(item["file"][key] for key in item["file"] if key != "hook_hits") and not any(item["file"]["hook_hits"].values()) for item in coverage(store.connection, "sample_coverage"))
    assert store.connection.execute("SELECT count(*) FROM file_coverage").fetchone()[0] == 0


@pytest.mark.parametrize("candidate", ["duplicate", "older_seq", "stale_epoch", "wrong_boot", "unknown_source_epoch"])
def test_rejected_frame_precedes_any_row_baseline_or_checkpoint_mutation(candidate):
    """Falsified if a rejected frame touches a row, checkpoint, clock segment, or published baseline."""
    store = initialized()
    frames, state = chromium_frames(store)
    frame = frames[-1]
    if candidate == "older_seq":
        frame = replace(frame, seq=1)
    elif candidate == "stale_epoch":
        store.start_collector("gui", BOOT, 130 * NS)
    elif candidate == "wrong_boot":
        frame = replace(frame, boot_id="another-boot")
    elif candidate == "unknown_source_epoch":
        frame = replace(frame, source="iod")
    # A bad payload must not be examined before duplicate/stale identity rejection.
    frame = replace(frame, checkpoint_state={"corrupt": True}, clock_offset_ns=999 * NS)
    before = snapshot(store.connection)
    published = store.sampler_state
    writes = []
    store.connection.set_trace_callback(writes.append)
    with pytest.raises(StaleFrameError):
        store.record_tick(frame, state)
    store.connection.set_trace_callback(None)
    assert snapshot(store.connection) == before
    assert store.sampler_state is published
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in writes)


class FaultConnection(sqlite3.Connection):
    failure = None
    seen_failure = False
    raised = RuntimeError

    def execute(self, sql, parameters=(), /):
        if self.failure and self.failure in sql and not self.seen_failure:
            self.seen_failure = True
            raise self.raised("injected store failure")
        return super().execute(sql, parameters)


@pytest.mark.parametrize("failure", ["UPDATE collector_epoch", "INSERT INTO clock_map", "INSERT INTO app ", "INSERT INTO launch ", "INSERT INTO sample ", "INSERT INTO sample_dev ", "INSERT INTO sample_coverage ", "INSERT INTO rollup_app ", "INSERT INTO rollup_dev ", "INSERT INTO app_boot_total ", "INSERT INTO app_boot_dev_total ", "INSERT INTO app_lifetime_total ", "INSERT INTO sampler_checkpoint ", "INSERT INTO collector_interval ", "COMMIT"])
def test_mid_transaction_fault_rolls_back_every_durable_row_and_publication(tmp_path, failure):
    """Falsified by any partial prefix, advanced receipt/checkpoint, or unretryable sequence."""
    path = tmp_path / "fault.sqlite"
    db = sqlite3.connect(path, factory=FaultConnection)
    store = initialized(db)
    _, state = record(store, None, tick(0, T0, [process()], cgroups=(group(100, 10),)))
    frame, staged = aggregate_tick(state, tick(1, T1, [process(counters=(100, 20, 10, 5)), process(510, 1, 201, path="/app-new.scope")], cgroups=(group(120, 20),), offset=500 * NS))
    before = snapshot(db)
    db.failure = failure
    with pytest.raises(RuntimeError, match="injected store failure"):
        store.record_tick(frame, staged)
    assert db.seen_failure, "fixture must actually hit its fault point"
    assert not db.in_transaction
    assert snapshot(db) == before
    assert store.sampler_state is state
    with sqlite3.connect(path) as reader:
        assert snapshot(reader) == before
    db.failure = None
    store.record_tick(frame, staged)
    assert store.sampler_state is staged
    assert db.execute("SELECT last_seq FROM collector_epoch").fetchone()[0] == 1
    assert db.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 10


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_crash_before_or_after_commit_then_restart_resumes_without_double_count(tmp_path, crash_after_commit):
    """Falsified by partial writes, a lost baseline, replay acceptance, or a final total above 120."""
    path = tmp_path / "crash.sqlite"
    db = sqlite3.connect(path, factory=FaultConnection)
    store = initialized(db)
    _, committed = record(store, None, tick(0, T0, [process(counters=(0, 0, 100, 0))]))
    interrupted_frame, staged = aggregate_tick(committed, tick(1, T1, [process(counters=(0, 0, 110, 0))]))
    if crash_after_commit:
        store.record_tick(interrupted_frame, staged)
    else:
        db.failure = "INSERT INTO sampler_checkpoint "
        db.raised = KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            store.record_tick(interrupted_frame, staged)
    db.close()
    reopened = AppIoStore(path)
    epoch = reopened.start_collector("gui", BOOT, 115 * NS)
    assert epoch.epoch == 2 and epoch.last_seq is None
    restored = reopened.restore_checkpoint("gui", BOOT)
    baseline = 110 if crash_after_commit else 100
    assert restored.processes[0].baseline == (0, 0, baseline, 0)
    before = snapshot(reopened.connection)
    with pytest.raises(StaleFrameError):
        reopened.record_tick(interrupted_frame, staged)
    assert snapshot(reopened.connection) == before
    resumed, state = record(reopened, restored, tick(0, T2, [process(counters=(0, 0, 120, 0))], epoch=2))
    assert resumed.deltas[0].counters[2] == 120 - baseline
    assert tuple(reopened.connection.execute("SELECT interval_start_mono_ns,observed_ns FROM sample WHERE epoch=2").fetchone()) == (None, None)
    assert reopened.connection.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 120
    assert [tuple(row) for row in reopened.connection.execute("SELECT from_mono_ns,to_mono_ns,reason FROM gap")] == [(T1 if crash_after_commit else T0, 115 * NS, "collector_gap")]
    assert coverage(reopened.connection, "app_boot_total")[0]["gap_intervals"] == 1
    assert reopened.restore_checkpoint("gui", "new-boot") is None
    with pytest.raises(StaleFrameError):
        reopened.record_tick(resumed, state)


def test_checkpoint_round_trip_preserves_parent_launch_credit_and_device_state(tmp_path):
    """Falsified by restoring only cumulative counters while discarding FIFO credit or cgroup identity."""
    path = tmp_path / "checkpoint.sqlite"
    store = initialized(path)
    unreadable = process(412, 999, 102, denied=True)
    _, state = record(store, None, tick(0, T0, [process(), process(411, 410, 101, (50, 30, 10, 5)), unreadable], cgroups=(group(100, 20),)))
    _, state = record(store, state, tick(1, T1, [process(counters=(20, 10, 4, 2)), unreadable], cgroups=(group(120, 30),)))
    assert state.processes[0].credits[0].remaining == (30, 20, 6, 3)
    saved = canonical_json(state.to_checkpoint())
    store.connection.close()
    reopened = AppIoStore(path)
    reopened.start_collector("gui", BOOT, 115 * NS)
    restored = reopened.restore_checkpoint("gui", BOOT)
    assert canonical_json(restored.to_checkpoint()) == saved
    assert restored.launches == state.launches
    assert restored.processes[0].credits == state.processes[0].credits
    assert restored.device_baselines == state.device_baselines
    assert restored.processes[1].baseline is None
    assert restored.processes[1].ppid_identity is None
    assert restored.processes[1].launch_identity.root is None
    next_frame, _ = record(reopened, restored, tick(0, T2, [process(counters=(50, 30, 10, 5)), unreadable], epoch=2, cgroups=(group(140, 40),)))
    assert next_frame.deltas[0].counters == (0, 0, 0, 0)
    assert next_frame.coverage[0].reap_corrections == 0
    assert next_frame.device_deltas[0].delta.rbytes == 20


def test_defined_zero_and_undefined_peaks_survive_raw_deletion():
    """Falsified by converting NULL to zero, or computing a bucket average as a peak."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    record(store, state, tick(1, T0 + 60 * NS, [process(counters=(0, 0, 10, 0))]))
    db = store.connection
    assert tuple(db.execute("SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM rollup_app WHERE bucket_secs=60 AND bucket_start_mono_ns=?", (60 * NS,)).fetchone()) == (None, None, None)
    assert tuple(db.execute("SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM app_boot_total").fetchone()) == (0, 0, 0)
    db.execute("DELETE FROM sample")
    db.commit()
    assert tuple(db.execute("SELECT max(peak_read_bps),max(peak_total_bps) FROM rollup_app").fetchone()) == (0, 0)
    assert db.execute("SELECT peak_read_bps FROM rollup_app WHERE bucket_secs=60 AND bucket_start_mono_ns=?", (60 * NS,)).fetchone()[0] is None


@pytest.mark.parametrize("intervals,expected", [([(100, 0), (0, 100)], (10, 10, 10)), ([(100, 100), (0, 0)], (10, 10, 20))])
def test_combined_peak_is_maximum_of_same_interval_sums(intervals, expected):
    """Falsified by adding component maxima collected in different intervals."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    read = write = 0
    for seq, (dr, dw) in enumerate(intervals, 1):
        read += dr
        write += dw
        _, state = record(store, state, tick(seq, T0 + seq * 10 * NS, [process(counters=(0, 0, read, write))]))
    assert tuple(store.connection.execute("SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM app_boot_total").fetchone()) == expected


def test_different_launch_intervals_do_not_fabricate_an_app_peak():
    """Falsified if rates from incompatible launch intervals are summed or maximized as an app rate."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    _, state = record(store, state, tick(1, T1, [process(counters=(0, 0, 100, 0)), process(411, 1, 101, (0, 0, 50, 0))]))
    assert store.connection.execute("SELECT peak_read_bps FROM app_boot_total").fetchone()[0] is None
    record(store, state, tick(2, T2, [process(counters=(0, 0, 200, 0)), process(411, 1, 101, (0, 0, 100, 0))]))
    assert store.connection.execute("SELECT peak_read_bps FROM app_boot_total").fetchone()[0] == 15


def test_local_app_and_launch_ids_are_deterministic_and_launch_lifetime_is_exact():
    """Falsified by input-order allocation or closing a launch when only its root disappears."""
    rows = []
    for reverse in (False, True):
        store = initialized()
        processes = [process(410, 1, 100, path="/app-z.scope"), process(420, 1, 200, path="/app-a.scope"), process(421, 420, 201, path="/app-a.scope")]
        _, state = record(store, None, tick(0, T0, reversed(processes) if reverse else processes))
        rows.append(([tuple(row) for row in store.connection.execute("SELECT app_id,key FROM app ORDER BY app_id")], [tuple(row) for row in store.connection.execute("SELECT launch_id,app_id,root_pid,root_start_ticks FROM launch ORDER BY launch_id")]))
        _, state = record(store, state, tick(1, T1, [process(421, 420, 201, path="/app-a.scope")]))
        assert tuple(store.connection.execute("SELECT ended_mono_ns,proc_count FROM launch WHERE root_pid=420").fetchone()) == (None, 1)
        record(store, state, tick(2, T2, []))
        assert tuple(store.connection.execute("SELECT ended_mono_ns,proc_count FROM launch WHERE root_pid=420").fetchone()) == (T2, 0)
    assert rows[0] == rows[1]
    assert rows[0][0] == [(1, "unit:app-a.scope"), (2, "unit:app-z.scope")]


def test_clock_segments_wall_labels_and_physical_main_wal_shm_bytes(tmp_path):
    """Falsified by wall-time bucket assignment, a missing initial segment, or counting only the main DB."""
    path = tmp_path / "clock.sqlite"
    store = initialized(path)
    store.connection.execute("PRAGMA journal_mode=WAL")
    state = None
    for seq, (at, offset) in enumerate([(119 * NS, 1000 * NS), (121 * NS, 1000 * NS), (125 * NS, 500 * NS), (126 * NS, 501 * NS)]):
        _, state = record(store, state, tick(seq, at, [process()], offset=offset))
    assert [tuple(row) for row in store.connection.execute("SELECT segment_start_mono_ns,wall_offset_ns FROM clock_map ORDER BY segment_start_mono_ns")] == [(0, 1000 * NS), (125 * NS, 500 * NS)]
    assert [row[0] for row in store.connection.execute("SELECT ts_wall FROM sample ORDER BY seq")] == [1119.0, 1121.0, 625.0, 626.0]
    assert [row[0] for row in store.connection.execute("SELECT bucket_start_mono_ns FROM rollup_app WHERE bucket_secs=60 ORDER BY bucket_start_mono_ns")] == [60 * NS, 120 * NS]
    actual = sum(candidate.stat().st_size for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")) if candidate.exists())
    assert actual > path.stat().st_size
    assert store.physical_store_bytes() == actual
    store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.connection.close()
    reopened = AppIoStore(path)
    assert reopened.physical_store_bytes() == sum(candidate.stat().st_size for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")) if candidate.exists())
    assert initialized().physical_store_bytes() == 0


def test_new_boot_starts_fresh_but_lifetime_last_keeps_boot_chronology():
    """Falsified by reusing a prior boot baseline or comparing boottimes across boots."""
    store = initialized()
    record(store, None, tick(0, 900 * NS, [process(counters=(0, 0, 100, 0))]))
    store.start_boot("boot-B", 2, 0, 2000.0, "gui")
    epoch = store.start_collector("gui", "boot-B", 0)
    assert epoch.epoch == 1
    assert store.restore_checkpoint("gui", "boot-B") is None
    record(store, None, tick(0, 10 * NS, [process(counters=(0, 0, 20, 0), boot="boot-B")], boot="boot-B", offset=2000 * NS))
    assert tuple(store.connection.execute("SELECT read_bytes,updated_boot_id,updated_mono_ns FROM app_lifetime_total").fetchone()) == (120, "boot-B", 10 * NS)


def test_frame_and_checkpoint_disagreement_is_atomic():
    """Falsified if the writer commits bytes with a different baseline or stream header."""
    store = initialized()
    frame, staged = aggregate_tick(None, tick(0, T0, [process()]))
    frame.checkpoint_state["processes"][0]["baseline"]["read_bytes"] = 99
    before = snapshot(store.connection)
    with pytest.raises(ValueError, match="checkpoint"):
        store.record_tick(frame, staged)
    assert snapshot(store.connection) == before
    assert store.sampler_state is None


def test_constructor_and_writer_never_commit_a_callers_outer_transaction():
    """Falsified by an implicit commit of unrelated caller work."""
    db = sqlite3.connect(":memory:")
    store = initialized(db)
    assert store.connection is db
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE boot SET started_wall=999")
    with pytest.raises(ValueError, match="transaction"):
        AppIoStore(db)
    frame, state = aggregate_tick(None, tick(0, T0, [process()]))
    with pytest.raises(ValueError, match="transaction"):
        store.record_tick(frame, state)
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT started_wall FROM boot").fetchone()[0] == 1000.0


def test_shared_cgroup_never_materializes_an_arbitrary_device_owner():
    """Falsified by duplicating, splitting, or fabricating zero device rows for ambiguous ownership."""
    store = initialized()
    first = process(path="/shared", counters=(0, 0, 10, 0))
    second = replace(process(420, 1, 200, (0, 0, 20, 0), path="/shared"), exe="/usr/bin/other", app_key_inputs=AppKeyInputs("/shared", "/usr/bin/other"))
    observation = CgroupObservation(BOOT, 101, "/shared", "available", (DevStat(101, "259:0", 100, 20),))
    _, state = record(store, None, tick(0, T0, [first, second], cgroups=(observation,)))
    observation = replace(observation, devices=(DevStat(101, "259:0", 160, 40),))
    record(store, state, tick(1, T1, [first, second], cgroups=(observation,)))
    db = store.connection
    assert db.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone()[0] == 30
    for table in ("sample_dev", "rollup_dev", "app_boot_dev_total"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert len(coverage(db, "sample_coverage")) == 4
    assert all(item["device_state"] == "incomplete" for item in coverage(db, "app_boot_total"))


def test_b2_overlap_retracts_devices_and_peaks_but_keeps_adjacent_valid_deltas():
    # Falsifier: retracting one owner, an entire bucket, or bytes without its peak/device contribution.
    from appio.contracts import Window
    from appio.query import QueryService

    store = initialized()
    db = store.connection
    try:
        _, gui = record(store, None, tick(0, T0, [process()], cgroups=(group(100, 10),)))
        _, gui = record(store, gui, tick(1, T1, [process(counters=(50, 20, 30, 20))], cgroups=(group(130, 30),)))
        store.start_collector("iod", BOOT, T1)
        _, iod = record(store, None, tick(0, T1, [process()], source="iod", cgroups=(group(100, 10),)))
        _, gui = record(store, gui, tick(2, T2, [process(counters=(250, 220, 230, 220))], cgroups=(group(330, 230),)))
        record(store, iod, tick(1, T2, [process(counters=(200, 200, 200, 200))], source="iod", cgroups=(group(300, 210),)))
        record(store, gui, tick(3, T2 + 10 * NS, [process(counters=(260, 225, 240, 225))], cgroups=(group(340, 235),)))
        for window in (Window("range", BOOT, 0, 140 * NS), Window("range", BOOT, 0, 2 * 86400 * NS),
                       Window("range", BOOT, 0, 31 * 86400 * NS), Window("lifetime")):
            result, = QueryService(db).ranking(window, "total_bytes")
            assert (result.rchar, result.wchar, result.read_bytes, result.write_bytes) == (60, 25, 40, 25)
            assert not result.coverage.complete
            assert result.coverage.overlap_uncertain_intervals == 1
        assert db.execute("SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM app_boot_total").fetchone() == (3, 2, 5)
        assert db.execute("SELECT rbytes,wbytes FROM app_boot_dev_total WHERE dev='259:0'").fetchone() == (40, 25)
        for secs in (60, 3600):
            assert db.execute("SELECT sum(rbytes),sum(wbytes) FROM rollup_dev WHERE bucket_secs=? AND dev='259:0'", (secs,)).fetchone() == (40, 25)
        before = snapshot(db)
        store.record_gap(BOOT, "gui", 1, 2, T1, T2, "overlap_uncertain")
        assert snapshot(db) == before
    finally:
        db.close()


def test_b2_handover_cutoff_selects_the_authoritative_source_on_each_side():
    # Falsifier: treating a ratified handover as an uncertain overlap drops valid bytes.
    from appio.durable import HandoverState

    store = initialized()
    db = store.connection
    try:
        db.execute("INSERT INTO handover VALUES(?,?,?,?,?)", (BOOT, "gui", "iod", T1, canonical_json(HandoverState(BOOT, T1, (), ()))))
        db.commit()
        _, gui = record(store, None, tick(0, T0, [process()]))
        store.start_collector("iod", BOOT, T0)
        _, iod = record(store, None, tick(0, T0, [process()], source="iod"))
        _, gui = record(store, gui, tick(1, T1, [process(counters=(10, 0, 10, 0))]))
        _, iod = record(store, iod, tick(1, T1, [process(counters=(10, 0, 10, 0))], source="iod"))
        record(store, gui, tick(2, T2, [process(counters=(20, 0, 20, 0))]))
        record(store, iod, tick(2, T2, [process(counters=(30, 0, 30, 0))], source="iod"))
        assert db.execute("SELECT rchar,read_bytes FROM app_boot_total").fetchone() == (30, 30)
        assert db.execute("SELECT count(*) FROM gap WHERE reason='overlap_uncertain'").fetchone()[0] == 0
        assert db.execute("SELECT source,ts_mono_ns,read_bytes FROM sample WHERE read_bytes>0 ORDER BY ts_mono_ns").fetchall() == [("gui", T1, 10), ("iod", T2, 20)]
    finally:
        db.close()


def test_b2_overlap_retraction_keeps_totals_from_already_pruned_history():
    # Falsifier: rebuilding corrected totals from retained raw rows erases valid older bytes.
    from appio.quota import QuotaPruner

    store = initialized()
    db = store.connection
    try:
        _, gui = record(store, None, tick(0, T0, [process()]))
        _, gui = record(store, gui, tick(1, T1, [process(counters=(7, 0, 7, 0))]))
        assert QuotaPruner(db, []).expire(BOOT, 2 * 86400 * NS).complete
        assert db.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
        now = 2 * 86400 * NS
        _, gui = record(store, gui, tick(2, now, [process(counters=(7, 0, 7, 0))]))
        store.start_collector("iod", BOOT, now)
        _, iod = record(store, None, tick(0, now, [process()], source="iod"))
        record(store, gui, tick(3, now + 20 * NS, [process(counters=(17, 0, 17, 0))]))
        record(store, iod, tick(1, now + 20 * NS, [process(counters=(10, 0, 10, 0))], source="iod"))
        assert db.execute("SELECT rchar,read_bytes FROM app_boot_total").fetchone() == (7, 7)
        assert db.execute("SELECT rchar,read_bytes FROM app_lifetime_total").fetchone() == (7, 7)
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["DELETE FROM sample WHERE", "UPDATE app_lifetime_total SET"])
def test_b2_retraction_failure_rolls_back_ownership_totals_and_receipt(tmp_path, failure):
    # Falsifier: a correction commits one owner's deletion or advances the rejected receipt.
    db = sqlite3.connect(tmp_path / "ownership-fault.sqlite", factory=FaultConnection)
    store = initialized(db)
    try:
        store.start_collector("iod", BOOT, T0)
        _, gui = record(store, None, tick(0, T0, [process()]))
        _, iod = record(store, None, tick(0, T0, [process()], source="iod"))
        record(store, gui, tick(1, T1, [process(counters=(10, 0, 10, 0))]))
        frame, staged = aggregate_tick(iod, tick(1, T1, [process(counters=(10, 0, 10, 0))], source="iod"))
        before, published = snapshot(db), store.sampler_state
        db.failure = failure
        with pytest.raises(RuntimeError, match="injected store failure"):
            store.record_tick(frame, staged)
        assert db.seen_failure
        assert not db.in_transaction
        assert snapshot(db) == before
        assert store.sampler_state is published
        db.failure = None
        store.record_tick(frame, staged)
        assert db.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 0
        assert db.execute("SELECT last_seq FROM collector_epoch WHERE source='iod'").fetchone()[0] == 1
    finally:
        db.close()


def test_record_gap_is_half_open_and_preserves_the_declared_stream():
    """Falsified by changing endpoints/source or accepting an empty/inverted gap."""
    store = initialized()
    store.record_gap(BOOT, "gui", 1, 0, T0, T1, "quota_exhausted")
    assert [tuple(row) for row in store.connection.execute("SELECT boot_id,source,epoch,seq,from_mono_ns,to_mono_ns,reason FROM gap")] == [(BOOT, "gui", 1, 0, T0, T1, "quota_exhausted")]
    before = snapshot(store.connection)
    for end in (T0, T0 - 1):
        with pytest.raises(ValueError):
            store.record_gap(BOOT, "gui", 1, 0, T0, end)
        assert snapshot(store.connection) == before
