"""Daemon (``iod``) and handover starts for the existing live collector.

The collector already owns one GUI stream.  A daemon start must extend that
collector rather than fork a second sampler, so every test here drives the real
``LiveCollector`` against the fixture proc/cgroup trees and asserts what the
database durably contains.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

import appio.live as live_module
from appio.handover import IncompleteHandoverError
from appio.live import LiveCollector
from appio.store import AppIoStore

FIXTURES = Path(__file__).parents[1] / "fixtures"
BOOT_ID = "11111111-2222-4333-8444-555555555555"
OTHER_BOOT_ID = "99999999-8888-4777-8666-555555555555"
APP_KEY = "unit:app-chromium@fixture.service"
PIDS = (410, 411, 412, 413)
NS = 1_000_000_000
WALL = 1_700_000_000_000_000_000


class SequenceClock:
    """Deterministic boots- and wall-clock pairs, one pair per observable read."""

    def __init__(self, *pairs: tuple[int, int]) -> None:
        self._pairs = iter(pairs)

    def __call__(self) -> tuple[int, int]:
        return next(self._pairs)


def make_tree(tmp_path: Path) -> tuple[Path, Path]:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    shutil.copytree(FIXTURES / "proc", proc_root, symlinks=True)
    shutil.copytree(FIXTURES / "cgroup" / "normal-t0", cgroup_root, symlinks=True)
    # Keep one app so an expected total is the direct sum of its four processes.
    shutil.rmtree(proc_root / "500")
    return proc_root, cgroup_root


def write_io(root: Path, pid: int, values: tuple[int, int, int, int]) -> None:
    rchar, wchar, read_bytes, write_bytes = values
    (root / str(pid) / "io").write_text(
        "rchar: {rchar}\n"
        "wchar: {wchar}\n"
        "syscr: 0\n"
        "syscw: 0\n"
        "read_bytes: {read_bytes}\n"
        "write_bytes: {write_bytes}\n"
        "cancelled_write_bytes: 0\n".format(
            rchar=rchar,
            wchar=wchar,
            read_bytes=read_bytes,
            write_bytes=write_bytes,
        ),
        encoding="utf-8",
    )


def write_all(root: Path, values: tuple[int, int, int, int]) -> None:
    for pid in PIDS:
        write_io(root, pid, values)


def app_total(connection: sqlite3.Connection, boot_id: str = BOOT_ID) -> tuple[int, int, int, int]:
    row = connection.execute(
        "SELECT t.rchar,t.wchar,t.read_bytes,t.write_bytes "
        "FROM app_boot_total AS t JOIN app AS a USING(app_id) "
        "WHERE a.key=? AND t.boot_id=?",
        (APP_KEY, boot_id),
    ).fetchone()
    assert row is not None
    return tuple(row)


def gaps(connection: sqlite3.Connection) -> list[tuple[int, int, str]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT from_mono_ns,to_mono_ns,reason FROM gap ORDER BY from_mono_ns"
        )
    ]


def test_iod_baseline_does_not_import_pre_start_cumulative_bytes(tmp_path):
    fixtures = Path(__file__).parents[1] / "fixtures"
    proc = tmp_path / "proc"
    group = tmp_path / "cgroup"
    shutil.copytree(fixtures / "proc", proc, symlinks=True)
    shutil.copytree(fixtures / "cgroup" / "normal-t0", group, symlinks=True)
    clock = iter([(10_000_000_000, 1_700_000_000_000_000_000),
                  (11_000_000_000, 1_700_000_001_000_000_000)])
    with LiveCollector(proc, group, tmp_path / "iod.db", lambda: next(clock),
                       source="iod", baseline_only=True) as collector:
        collector.tick()
        assert collector.last_frame.source == "iod"
        assert collector.last_frame.deltas == ()
        assert collector.next_seq == 1
        assert collector.store.connection.execute(
            "SELECT sum(read_bytes+write_bytes) FROM app_boot_total").fetchone()[0] in (None, 0)


def test_iod_baseline_only_emits_only_post_baseline_deltas(tmp_path):
    # Falsifier: routing a daemon start through the A01 first-seen emission turns
    # the lifetime counters that accumulated before the daemon started into bytes.
    proc, group = make_tree(tmp_path)
    write_all(proc, (100, 50, 10, 7))
    collector = LiveCollector(
        proc, group, tmp_path / "iod.db",
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
        source="iod", baseline_only=True,
    )
    assert collector.epoch is None  # the epoch is allocated at the first sample
    collector.tick()
    assert (collector.epoch, collector.next_seq) == (1, 1)
    assert collector.last_frame.deltas == ()
    assert app_total(collector.store.connection) == (0, 0, 0, 0)
    assert collector.store.connection.execute(
        "SELECT epoch,last_seq FROM collector_epoch WHERE source='iod'"
    ).fetchone() == (1, 0)

    write_all(proc, (110, 54, 13, 9))
    collector.tick()
    assert (collector.epoch, collector.next_seq) == (1, 2)
    assert collector.last_frame.seq == 1
    assert app_total(collector.store.connection) == (40, 16, 12, 8)
    assert collector.store.connection.execute(
        "SELECT count(*) FROM sample WHERE epoch=1 AND seq=0"
    ).fetchone()[0] == 0  # the baseline receipt commits without byte rows
    assert [
        tuple(row)
        for row in collector.store.connection.execute(
            "SELECT source,boot_id,epoch,seq FROM sample_coverage ORDER BY epoch,seq"
        )
    ] == [("iod", BOOT_ID, 1, 0), ("iod", BOOT_ID, 1, 1)]
    collector.close()


def test_same_boot_daemon_restart_allocates_the_next_epoch_and_keeps_credit(tmp_path):
    # Falsifier: reusing the epoch or sequence, or dropping the pending reap
    # credit that the restored checkpoint still owns.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "iod.db"
    write_all(proc, (100, 50, 10, 7))
    clock = SequenceClock(
        (10 * NS, WALL),
        (11 * NS, WALL + NS),
        (12 * NS, WALL + 2 * NS),
        (13 * NS, WALL + 3 * NS),
        (14 * NS, WALL + 4 * NS),
        (15 * NS, WALL + 5 * NS),
        (16 * NS, WALL + 6 * NS),
    )
    collector = LiveCollector(proc, group, database, clock, source="iod", baseline_only=True)
    collector.tick()
    write_all(proc, (110, 54, 13, 9))
    collector.tick()
    assert app_total(collector.store.connection) == (40, 16, 12, 8)

    shutil.rmtree(proc / "411")  # the child is reaped; its bytes shift to 410
    collector.tick()
    parent = next(
        process for process in collector.sampler_state.processes if process.identity.pid == 410
    )
    assert [(credit.child.pid, credit.remaining) for credit in parent.credits] == [
        (411, (110, 54, 13, 9))
    ]
    collector.close()

    restarted = LiveCollector(proc, group, database, clock, source="iod", baseline_only=True)
    assert restarted.epoch is None
    restarted.tick()  # a fresh epoch baseline that emits nothing
    assert (restarted.epoch, restarted.next_seq) == (2, 1)
    assert app_total(restarted.store.connection) == (40, 16, 12, 8)
    parent = next(
        process for process in restarted.sampler_state.processes if process.identity.pid == 410
    )
    assert [(credit.child.pid, credit.remaining) for credit in parent.credits] == [
        (411, (110, 54, 13, 9))
    ]
    assert gaps(restarted.store.connection) == [(13 * NS, 15 * NS, "collector_gap")]  # allocation waits for the sample

    # The restored credit is consumed against the parent's next real delta.
    write_io(proc, 410, (310, 154, 33, 23))
    result = restarted.tick()
    assert result.coverage.reap_corrections == 1
    assert app_total(restarted.store.connection) == (130, 62, 19, 13)
    parent = next(
        process for process in restarted.sampler_state.processes if process.identity.pid == 410
    )
    assert parent.credits == ()
    assert restarted.store.connection.execute(
        "SELECT epoch,last_seq FROM collector_epoch WHERE source='iod'"
    ).fetchone() == (2, 1)
    # Every frame owns one receipt, so an epoch or sequence is never reused.
    assert [
        tuple(row)
        for row in restarted.store.connection.execute(
            "SELECT epoch,seq FROM sample_coverage GROUP BY epoch,seq ORDER BY epoch,seq"
        )
    ] == [(1, 0), (1, 1), (1, 2), (2, 0), (2, 1)]
    assert restarted.store.connection.execute(
        "SELECT count(*) FROM sample WHERE epoch=1 AND seq=0"
    ).fetchone()[0] == 0
    restarted.close()


def test_new_boot_resets_the_epoch_and_rejects_the_old_checkpoint(tmp_path):
    # Falsifier: restoring a checkpoint from another boot, or continuing an epoch.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "iod.db"
    write_all(proc, (100, 50, 10, 7))
    clock = SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS))
    old = LiveCollector(proc, group, database, clock, source="iod", baseline_only=True)
    old.tick()
    write_all(proc, (110, 54, 13, 9))
    old.tick()
    assert app_total(old.store.connection) == (40, 16, 12, 8)
    old.close()

    (proc / "boot_id").write_text(OTHER_BOOT_ID + "\n", encoding="utf-8")
    write_all(proc, (900, 400, 800, 200))
    new = LiveCollector(
        proc, group, database,
        SequenceClock((20 * NS, WALL + 10 * NS), (21 * NS, WALL + 11 * NS)),
        source="iod", baseline_only=True,
    )
    assert new.epoch is None
    new.tick()
    assert (new.epoch, new.next_seq) == (1, 1)
    assert new.sampler_state.boot_id == OTHER_BOOT_ID
    assert new.sampler_state.processes[0].baseline == (900, 400, 800, 200)
    assert all(process.credits == () for process in new.sampler_state.processes)
    assert app_total(new.store.connection, OTHER_BOOT_ID) == (0, 0, 0, 0)
    assert app_total(new.store.connection, BOOT_ID) == (40, 16, 12, 8)
    assert [
        tuple(row)
        for row in new.store.connection.execute(
            "SELECT boot_id,boot_order FROM boot ORDER BY boot_order"
        )
    ] == [(BOOT_ID, 1), (OTHER_BOOT_ID, 2)]
    assert [
        tuple(row)
        for row in new.store.connection.execute(
            "SELECT boot_id,epoch FROM collector_epoch WHERE source='iod'"
        )
    ] == [(OTHER_BOOT_ID, 1)]
    new.close()


def test_installed_handover_starts_at_seq_zero_and_advances_through_its_gap(tmp_path):
    # Falsifier: emitting the cross-gap delta that spans the interval in which no
    # collector was running, or continuing the outgoing sequence.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "handover.db"
    write_all(proc, (100, 50, 10, 7))
    gui = LiveCollector(
        proc, group, database,
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
    )
    gui.tick()
    write_all(proc, (110, 54, 13, 9))
    gui.tick()
    assert app_total(gui.store.connection) == (440, 216, 52, 36)
    offer = gui.quiesce()
    assert (
        offer.from_source,
        offer.to_source,
        offer.outgoing_epoch,
        offer.outgoing_seq,
        offer.interval_start_mono_ns,
        offer.state.cutoff_mono_ns,
    ) == ("gui", "iod", 1, 1, 10 * NS, 12 * NS)
    gui.close()

    write_all(proc, (120, 58, 16, 11))
    iod = LiveCollector(
        proc, group, database,
        SequenceClock((20 * NS, WALL + 8 * NS), (21 * NS, WALL + 9 * NS), (22 * NS, WALL + 10 * NS)),
        source="iod", resume_handover=offer,
    )
    assert (iod.epoch, iod.next_seq) == (1, 1)
    assert (iod.last_frame.source, iod.last_frame.seq, iod.last_frame.deltas) == ("iod", 0, ())
    assert [process.baseline for process in iod.sampler_state.processes] == [
        (110, 54, 13, 9)
    ] * 4

    # The cutoff precedes this sample, so its whole delta spans unmeasured time.
    result = iod.tick()
    assert result.rows_written >= 1
    assert (iod.epoch, iod.next_seq, iod.last_frame.seq) == (1, 2, 1)
    assert iod.last_frame.deltas == ()
    assert iod.last_frame.device_deltas == ()
    assert app_total(iod.store.connection) == (440, 216, 52, 36)
    assert gaps(iod.store.connection) == [(12 * NS, 21 * NS, "collector_gap")]
    assert iod.last_result.coverage.partial_intervals == 1
    assert iod.last_result.coverage.gap_intervals == 1

    # Only a delta that starts after the measured baseline is accounted.
    write_all(proc, (130, 62, 19, 13))
    iod.tick()
    assert (iod.epoch, iod.next_seq, iod.last_frame.seq) == (1, 3, 2)
    assert app_total(iod.store.connection) == (480, 232, 64, 44)
    assert gaps(iod.store.connection) == [(12 * NS, 21 * NS, "collector_gap")]
    assert iod.store.connection.execute(
        "SELECT count(*) FROM handover WHERE boot_id=? AND cutoff_mono_ns=?",
        (BOOT_ID, 12 * NS),
    ).fetchone()[0] == 1
    iod.close()


def test_quiesce_freezes_ticks_and_refuses_a_collection_that_never_started(tmp_path):
    # Falsifier: offering a cutoff for a stream with no durable interval, or
    # letting a quiesced collector keep sampling.
    proc, group = make_tree(tmp_path)
    write_all(proc, (100, 50, 10, 7))
    collector = LiveCollector(
        proc, group, tmp_path / "iod.db",
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
        source="iod", baseline_only=True,
    )
    with pytest.raises(IncompleteHandoverError):
        collector.quiesce()
    collector.tick()
    with pytest.raises(IncompleteHandoverError):
        collector.quiesce()  # a lone baseline has no measured interval to offer

    write_all(proc, (110, 54, 13, 9))
    collector.tick()
    offer = collector.quiesce()
    assert (offer.from_source, offer.to_source) == ("iod", "gui")
    assert offer.outgoing_epoch == collector.sampler_state.epoch
    assert offer.outgoing_seq == collector.sampler_state.last_seq == 1
    assert offer.interval_start_mono_ns == 11 * NS
    assert offer.state.cutoff_mono_ns == 12 * NS
    # The first offer already freezes the stream: no later tick may move the
    # cutoff it published.
    with pytest.raises(RuntimeError, match="quiesced"):
        collector.tick()
    with pytest.raises(RuntimeError, match="quiesced"):
        collector.run_forever(0, max_ticks=1)
    assert collector.quiesce() == offer  # stable when asked again
    assert collector.next_seq == 2  # nothing advanced after the freeze
    collector.close()


class CrashOnCommitConnection(sqlite3.Connection):
    """Fail the outer COMMIT so the frame never becomes durable."""

    crash = False

    def execute(self, sql, parameters=(), /):
        if self.crash and sql == "COMMIT":
            raise KeyboardInterrupt("crash before commit")
        return super().execute(sql, parameters)


def test_commit_failure_leaves_last_frame_state_and_next_seq_unchanged(tmp_path, monkeypatch):
    # Falsifier: publishing state, the frame or the next sequence before commit.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "crash.db"
    connection = sqlite3.connect(database, factory=CrashOnCommitConnection)
    monkeypatch.setattr(live_module, "AppIoStore", lambda path: AppIoStore(connection))
    write_all(proc, (100, 50, 10, 7))
    collector = LiveCollector(
        proc, group, database,
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
        source="iod", baseline_only=True,
    )
    collector.tick()
    state, frame, seq = collector.sampler_state, collector.last_frame, collector.next_seq
    samples = connection.execute("SELECT count(*) FROM sample").fetchone()[0]
    assert (state, frame, seq) == (collector.sampler_state, collector.last_frame, 1)

    write_all(proc, (110, 54, 13, 9))
    connection.crash = True
    with pytest.raises(KeyboardInterrupt, match="crash before commit"):
        collector.tick()
    connection.crash = False
    assert collector.sampler_state is state
    assert collector.last_frame is frame
    assert collector.next_seq == seq
    assert connection.execute("SELECT count(*) FROM sample").fetchone()[0] == samples
    assert connection.execute("SELECT last_seq FROM collector_epoch WHERE source='iod'").fetchone()[0] == 0
    collector.close()


def test_exported_root_origin_boot_reopens_for_gui_without_rewriting_source(tmp_path):
    # Falsifier: refusing (or rewriting) a boot row whose creating source differs.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "export.db"
    write_all(proc, (100, 50, 10, 7))
    daemon = LiveCollector(
        proc, group, database,
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS)),
        source="iod", baseline_only=True,
    )
    daemon.tick()
    daemon.close()
    assert database.exists()

    gui = LiveCollector(
        proc, group, database,
        SequenceClock((20 * NS, WALL + 8 * NS), (21 * NS, WALL + 9 * NS)),
        source="gui", baseline_only=True,
    )
    gui.tick()
    assert gui.epoch == 1
    assert gui.store.connection.execute(
        "SELECT source FROM boot WHERE boot_id=?", (BOOT_ID,)
    ).fetchone() == ("iod",)
    assert [
        tuple(row)
        for row in gui.store.connection.execute(
            "SELECT source,epoch FROM collector_epoch ORDER BY source"
        )
    ] == [("gui", 1), ("iod", 1)]
    assert [
        tuple(row)
        for row in gui.store.connection.execute(
            "SELECT source FROM collector_interval ORDER BY source"
        )
    ] == []
    gui.close()


def test_gui_cold_start_keeps_its_first_seen_emission(tmp_path):
    # Falsifier: applying the daemon baseline rule to the unchanged GUI default.
    proc, group = make_tree(tmp_path)
    write_all(proc, (100, 50, 10, 7))
    collector = LiveCollector(
        proc, group, tmp_path / "gui.db",
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
    )
    result = collector.tick()
    assert collector.epoch == 1
    assert app_total(collector.store.connection) == (400, 200, 40, 28)
    assert result.coverage.partial_intervals == 1
    assert collector.store.connection.execute(
        "SELECT delta_kind,first_seen_partial,sum(read_bytes) FROM sample"
        " GROUP BY delta_kind,first_seen_partial"
    ).fetchall() == [("baseline", 1, 40)]

    write_all(proc, (110, 54, 13, 9))
    collector.tick()
    assert app_total(collector.store.connection) == (440, 216, 52, 36)
    collector.close()


def test_offer_without_an_ownership_chain_falls_back_to_a_fresh_baseline(tmp_path):
    # Falsifier: installing a handover row whose cutoff evidence is gone, or
    # importing the offer's baselines without the chain that proves them.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "pruned.db"
    write_all(proc, (100, 50, 10, 7))
    gui = LiveCollector(
        proc, group, database,
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
    )
    gui.tick()
    write_all(proc, (110, 54, 13, 9))
    gui.tick()
    offer = gui.quiesce()
    gui.close()

    # A pruned/exported database can lose the cutoff receipt that proves the chain.
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM sample_coverage WHERE ts_mono_ns=?", (12 * NS,))

    write_all(proc, (900, 400, 800, 200))
    iod = LiveCollector(
        proc, group, database,
        SequenceClock((20 * NS, WALL + 8 * NS), (21 * NS, WALL + 9 * NS)),
        source="iod", resume_handover=offer,
    )
    assert iod.epoch is None
    iod.tick()
    assert (iod.epoch, iod.next_seq) == (1, 1)
    assert iod.last_frame.deltas == ()
    assert iod.store.connection.execute("SELECT count(*) FROM handover").fetchone()[0] == 0
    assert [process.baseline for process in iod.sampler_state.processes] == [
        (900, 400, 800, 200)
    ] * 4
    assert app_total(iod.store.connection) == (440, 216, 52, 36)
    assert gaps(iod.store.connection)[-1] == (12 * NS, 21 * NS, "collector_gap")
    iod.close()


def test_a_cutoff_equal_to_the_first_sample_is_not_a_cross_gap(tmp_path):
    # Falsifier: treating a sample taken at the cutoff itself as unmeasured time.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "equal.db"
    write_all(proc, (100, 50, 10, 7))
    gui = LiveCollector(
        proc, group, database,
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
    )
    gui.tick()
    write_all(proc, (110, 54, 13, 9))
    gui.tick()
    offer = gui.quiesce()
    gui.close()

    write_all(proc, (120, 58, 16, 11))
    iod = LiveCollector(
        proc, group, database,
        SequenceClock((12 * NS, WALL + 3 * NS), (12 * NS, WALL + 3 * NS),
                      (13 * NS, WALL + 4 * NS)),
        source="iod", resume_handover=offer,
    )
    result = iod.tick()
    assert iod.last_frame.seq == 1
    # The sample sits at the cutoff, so its delta is measured, not dropped.
    assert app_total(iod.store.connection) == (480, 232, 64, 44)
    assert result.coverage.gap_intervals == 0
    assert gaps(iod.store.connection) == []

    write_all(proc, (130, 62, 19, 13))
    iod.tick()
    assert app_total(iod.store.connection) == (520, 248, 76, 52)
    iod.close()


def test_a_stale_offer_degrades_visibly_instead_of_replaying_backwards(tmp_path):
    # Falsifier: installing a cutoff that is behind the incoming source's own
    # durable evidence, or degrading without saying why.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "stale.db"
    write_all(proc, (100, 50, 10, 7))
    gui = LiveCollector(
        proc, group, database,
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
    )
    gui.tick()
    write_all(proc, (110, 54, 13, 9))
    gui.tick()
    offer = gui.quiesce()
    gui.close()

    daemon = LiveCollector(
        proc, group, database,
        SequenceClock((13 * NS, WALL + 3 * NS), (14 * NS, WALL + 4 * NS)),
        source="iod", baseline_only=True,
    )
    daemon.tick()
    assert daemon.epoch == 1
    daemon.close()

    stale = LiveCollector(
        proc, group, database,
        SequenceClock((15 * NS, WALL + 5 * NS), (16 * NS, WALL + 6 * NS)),
        source="iod", resume_handover=offer,
    )
    assert stale.epoch is None
    assert stale.handover_fallback_reason is not None
    assert stale.handover_fallback_reason.startswith("ValueError")
    stale.tick()
    assert stale.epoch == 2
    assert stale.last_frame.deltas == ()
    assert stale.store.connection.execute("SELECT count(*) FROM handover").fetchone()[0] == 0
    assert app_total(stale.store.connection) == (440, 216, 52, 36)
    stale.close()


def test_a_repeated_installation_restores_the_exact_committed_seed(tmp_path):
    # Falsifier: rebuilding the seed with this installation's clock read, which
    # leaves last_frame.clock_offset_ns disagreeing with the persisted mapping.
    proc, group = make_tree(tmp_path)
    database = tmp_path / "repeat.db"
    write_all(proc, (100, 50, 10, 7))
    gui = LiveCollector(
        proc, group, database,
        SequenceClock((10 * NS, WALL), (11 * NS, WALL + NS), (12 * NS, WALL + 2 * NS)),
    )
    gui.tick()
    write_all(proc, (110, 54, 13, 9))
    gui.tick()
    offer = gui.quiesce()
    gui.close()

    first = LiveCollector(
        proc, group, database,
        SequenceClock((20 * NS, WALL + 8 * NS), (21 * NS, WALL + 9 * NS)),
        source="iod", resume_handover=offer,
    )
    committed = first.last_frame
    assert committed.clock_offset_ns == WALL + 8 * NS - 12 * NS
    first.close()

    later = LiveCollector(
        proc, group, database,
        SequenceClock((30 * NS, WALL + 20 * NS), (31 * NS, WALL + 21 * NS)),
        source="iod", resume_handover=offer,
    )
    assert later.epoch == first.epoch == 1
    assert later.last_frame == committed
    later.tick()
    assert later.last_frame.seq == 1
    later.close()
