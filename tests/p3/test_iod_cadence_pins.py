"""Regression pins for the shipped storage cadence and persistent-failure recovery.

Use the rootless runtime's real collector, SQLite store and socket loop. These
pins deliberately use production Daemon defaults, unlike fast behavioural vectors.
"""
import sqlite3

import pytest

from appio.quota import PruneResult, QuotaPruner
from test_iod import NS, pump, receive, request, runtime, total, write_io


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_raw_persistence_follows_the_documented_ten_second_cadence(runtime):
    r = runtime
    r.daemon.start()
    baseline = r.daemon.committed_cursor
    db = r.daemon.collector.store.connection
    before = db.execute("SELECT count(DISTINCT seq) FROM sample").fetchone()[0]
    client = r.connect()[0]
    # 15 responsiveness ticks span exactly three budgeted raw observations.
    for tick in range(1, 16):
        write_io(r.proc, 100 + tick)
        r.clock.advance(2)
        request(client, request_id=str(tick))
        pump(r)
        assert any(row.get("request_id") == str(tick) for row in receive(client))
        count = db.execute("SELECT count(DISTINCT seq) FROM sample").fetchone()[0]
        assert count - before == tick // 5
        assert r.daemon.committed_cursor.iod_epoch == baseline.iod_epoch
        assert r.daemon.committed_cursor.seq == tick // 5
    assert total(r.paths.database) == (600, 300, 180, 120)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_sample_deadline_is_independent_of_responsiveness_tick(runtime):
    r = runtime
    r.daemon.start()
    r.clock.advance(9)
    pump(r)
    assert r.daemon.committed_cursor.seq == 0
    write_io(r.proc, 101)
    r.clock.advance(1)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1
    assert total(r.paths.database) == (40, 20, 12, 8)
    r.clock.advance(1)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_missed_sample_boundaries_do_not_create_catch_up_frames(runtime):
    r = runtime
    r.daemon.start()
    write_io(r.proc, 106)
    r.clock.advance(26)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1
    r.clock.advance(2)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1
    write_io(r.proc, 107)
    r.clock.advance(2)
    pump(r)
    assert r.daemon.committed_cursor.seq == 2
    db = r.daemon.collector.store.connection
    assert db.execute("SELECT DISTINCT seq,observed_mono_ns FROM sample ORDER BY seq").fetchall() == [
        (1, 36 * NS), (2, 40 * NS)]
    assert total(r.paths.database) == (280, 140, 84, 56)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_shipped_stall_guard_records_a_gap_after_thirty_seconds(runtime):
    r = runtime
    r.daemon.start()
    write_io(r.proc, 105)
    r.clock.advance(31)
    pump(r)
    assert (r.daemon.committed_cursor.iod_epoch, r.daemon.committed_cursor.seq) == (2, 0)
    assert total(r.paths.database) == (0, 0, 0, 0)
    with sqlite3.connect(r.paths.database) as db:
        assert db.execute("SELECT from_mono_ns,to_mono_ns FROM gap WHERE reason='collector_gap'").fetchall() == [
            (10 * NS, 41 * NS)]


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_recovery_window_clears_only_when_committed_mono_advances(runtime, monkeypatch):
    r = runtime
    r.daemon.start()
    committed = r.daemon.committed_cursor
    before = r.daemon._committed_mono
    monkeypatch.setattr(r.daemon._pruner, "expire", lambda *args:
                        PruneResult((), False, False, 4096, "retention_incomplete"))
    r.daemon._next_maintenance = r.clock()[0]
    pump(r)
    tick = r.daemon.collector.tick
    # A non-raising return alone cannot prove that the committed frame moved.
    monkeypatch.setattr(r.daemon.collector, "tick", lambda: r.daemon.collector.last_result)
    r.clock.advance(10)
    pump(r)
    assert r.daemon._committed_mono == before
    assert r.daemon.committed_cursor == committed
    assert r.daemon._next_recovery > r.clock()[0]
    monkeypatch.setattr(r.daemon.collector, "tick", tick)
    write_io(r.proc, 101)
    r.clock.advance(10)
    pump(r)
    assert r.daemon._committed_mono > before
    assert r.daemon._next_recovery == 0
    assert total(r.paths.database) == (40, 20, 12, 8)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_nonstorage_collection_failures_retry_at_each_sample_boundary(runtime, monkeypatch):
    r = runtime
    r.daemon.start()
    committed = r.daemon.committed_cursor
    tick = r.daemon.collector.tick.__func__
    calls = []

    def transient(collector):
        calls.append(r.clock()[0])
        if len(calls) <= 3:
            raise OSError("temporary process observation failure")
        return tick(collector)

    monkeypatch.setattr(type(r.daemon.collector), "tick", transient)
    for attempt in range(1, 4):
        r.clock.advance(10)
        pump(r)
        assert len(calls) == attempt
        assert r.daemon.committed_cursor == committed
        assert r.daemon.last_error == "collection_failed"
        assert r.daemon._next_recovery == 0
    r.clock.advance(10)
    pump(r)
    assert len(calls) == 4
    assert r.daemon.last_error is None
    assert r.daemon._committed_mono == r.clock()[0]
    assert r.daemon.committed_cursor.iod_epoch == committed.iod_epoch + 1
    assert total(r.paths.database) == (0, 0, 0, 0)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
@pytest.mark.parametrize("condition", ["quota_exhausted", "retention_incomplete", "maintenance_failed"])
def test_recovery_does_not_allocate_an_epoch_on_every_tick(runtime, monkeypatch, condition):
    r = runtime
    r.daemon.start()
    before = r.daemon.committed_cursor
    client = r.connect()[0]
    attempts = []
    maintaining = False
    tick = r.daemon.collector.tick.__func__

    def blocked_tick(collector):
        # Keep real epoch allocation and collector_gap side effects before the
        # failing durable observation, as LiveCollector does in production.
        db = collector.store.connection
        db.execute("CREATE TEMP TRIGGER reject_frame BEFORE INSERT ON sample_coverage "
                   "BEGIN SELECT RAISE(ABORT, 'persistent storage failure'); END")
        try:
            return tick(collector)
        finally:
            db.execute("DROP TRIGGER reject_frame")

    def expire(pruner, boot, now):
        nonlocal maintaining
        attempts.append(now)
        if condition == "maintenance_failed":
            raise sqlite3.OperationalError("persistent maintenance failure")
        maintaining = True
        return PruneResult((), condition != "retention_incomplete", False, 4096,
                           condition if condition == "retention_incomplete" else None)

    def shrink(pruner):
        nonlocal maintaining
        paused = maintaining and condition == "quota_exhausted"
        maintaining = False
        return PruneResult((), not paused, paused, 4096, "quota_exhausted" if paused else None)

    # Paused-turn pruning sees temporary headroom, but the next durable commit
    # still fails. Maintenance reasserts the persistent condition every cycle.
    # Patch the class so collector replacement retains the same storage fault.
    monkeypatch.setattr(type(r.daemon.collector), "tick", blocked_tick)
    monkeypatch.setattr(QuotaPruner, "expire", expire)
    monkeypatch.setattr(QuotaPruner, "shrink_if_needed", shrink)
    for _ in range(20):
        # Exercise the actual maintenance branch while paused. Quiet socket
        # turns between retries must not clear a recovery window without commit.
        r.daemon._quota_paused = True
        r.daemon._next_maintenance = r.clock()[0]
        r.daemon.step()
        assert r.daemon.last_error == condition
        write_io(r.proc, 105)
        r.clock.advance(4)
        request(client)
        pump(r)
        assert any(row["type"] == "status" and not row["answering"] for row in receive(client))
    with sqlite3.connect(r.paths.database) as db:
        epochs = db.execute("SELECT max(epoch) FROM collector_epoch WHERE source='iod'").fetchone()[0] - before.iod_epoch
        gaps = db.execute("SELECT count(*) FROM gap WHERE reason='collector_gap'").fetchone()[0]
    assert len(attempts) == 20
    assert 1 <= epochs <= 2, f"20 maintenance cycles allocated {epochs} epochs and {gaps} gaps"
    assert gaps <= 2
    assert r.daemon.committed_cursor == before
    assert total(r.paths.database) == (0, 0, 0, 0)
