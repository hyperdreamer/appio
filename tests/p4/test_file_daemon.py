"""Rootless daemon integration; distinguish F bytes, T bytes and D bytes.

Falsifiers: fold F into T/D, set T/D last_error on attach/drain/SQLite error,
retry every 2s or bypass storage backoff, miss a closed hour, or leak a loader.
"""
from dataclasses import asdict
import errno
import logging
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import appio.iod as iod

import pytest

from appio.bpf_fileio import BpfFileSource, VectorLoader
from appio.contracts import FILE_HOUR_NS as H
from appio.iod import Daemon, DaemonPaths
from appio.ipc_protocol import FrameDecoder
from tests.p3.test_iod import Clock, DeferredWorker, FakeFilesystem, PollSelector, write_io
from tests.p3.test_iodclient import Harness, snapshot_frame
from tests.p4.test_file_runtime import event

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NS = 10**9


@pytest.fixture(autouse=True)
def forbid_default_privileged_loader(monkeypatch):
    def forbidden():
        pytest.fail("daemon test attempted the installed privileged loader")
    monkeypatch.setattr("appio.file_runtime.default_file_source", forbidden)


@pytest.fixture
def make_daemon(tmp_path):
    made = []

    def make(loader=None, factory=None):
        root = tmp_path / str(len(made))
        root.mkdir()
        proc, cgroup = root / "proc", root / "cgroup"
        shutil.copytree(FIXTURES / "proc", proc, symlinks=True)
        shutil.copytree(FIXTURES / "cgroup/normal-t0", cgroup)
        shutil.rmtree(proc / "500")
        shutil.copyfile(FIXTURES / "cgroup/proc/diskstats", proc / "diskstats")
        write_io(proc, 100)
        state, run = root / "state", root / "run"
        state.mkdir(mode=0o711)
        run.mkdir(mode=0o750)
        clock = Clock()
        loader = VectorLoader() if loader is None else loader
        daemon = Daemon(DaemonPaths(state / "iod.db", run / "iod.sock", proc, cgroup),
                        clock=clock, filesystem=FakeFilesystem(), selector=PollSelector(),
                        query_worker=DeferredWorker(), file_source_factory=factory or (lambda: BpfFileSource(loader)))
        made.append(daemon)
        daemon.start()
        wait_for_F(daemon)
        return SimpleNamespace(daemon=daemon, clock=clock, loader=loader, proc=proc)

    yield make
    for daemon in made:
        daemon.stop()
        daemon.selector.close()


def wait_for_F(d):
    # Tests may await persistence; the production owner must never do so.
    frame = d.collector.last_frame
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        worker, runtime = d.file_runtime, d.file_runtime._runtime
        if runtime is not None and worker.availability is runtime.availability:
            if runtime._last.get(frame.boot_id) == (frame.epoch, frame.seq):
                return
            if worker.diagnostic and "F failure" in worker.diagnostic:
                return
        time.sleep(0.001)
    pytest.fail("worker did not complete the committed F endpoint")


def snapshot(d):
    return FrameDecoder().feed(d._current.frame)[0]


def step(h, seconds=10, amount=200):
    write_io(h.proc, amount)
    h.clock.advance(seconds)
    h.daemon.step()
    wait_for_F(h.daemon)


def total(d):
    return d.collector.store.connection.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone()[0]


def test_verified_six_hooks_only_then_F_snapshot_and_same_10_second_cadence(make_daemon):
    h = make_daemon()
    first = h.daemon.committed_cursor
    h.loader.queue(1, events=[event(amount=999999)])
    step(h, seconds=8)
    assert h.daemon.committed_cursor == first
    assert h.loader.flip_requests == [(first.iod_epoch, first.seq)]
    step(h, seconds=2)
    message = snapshot(h.daemon)
    assert message["capabilities"] == dict(proc_all=True, dev_stat=True, file_io=True, bpf_loaded=True)
    assert message["files"] == []
    assert message["coverage"]["file_source"] == dict(available=True, bpf_loaded=True, reason=None, diagnostic=None)
    assert h.loader.flip_requests[-1] == (message["iod_epoch"], message["seq"])
    # Four synthetic processes: each moves 300 T bytes; F's 999999 cannot enter T.
    assert total(h.daemon) == 1200
    assert all(row.get("read_bytes") != 999999 for row in message["procs"])
    assert h.daemon.last_error is None


@pytest.mark.parametrize("loader", [VectorLoader(hook_mask=0x1f), VectorLoader(hello_status=1, hello_error=-errno.EPERM, verifier=b"verifier denied")])
def test_attach_failure_advertises_unavailable_never_zero_and_T_D_keep_running(make_daemon, loader):
    h = make_daemon(loader)
    first = h.daemon.committed_cursor
    step(h)
    message = snapshot(h.daemon)
    assert h.daemon.committed_cursor.seq > first.seq and total(h.daemon) == 1200
    assert h.daemon.last_error is None and message["procs"] and message["devs"]
    assert not message["capabilities"]["file_io"] and not message["capabilities"]["bpf_loaded"]
    status = message["coverage"]["file_source"]
    assert status["reason"] == "file_source_unavailable" and status["diagnostic"]
    assert len(status["diagnostic"]) <= 512
    assert h.daemon.collector.store.connection.execute("SELECT state_json FROM file_hour_state").fetchone() is not None


def test_drain_failure_does_not_stall_T_D_publish_or_clear_replay(make_daemon):
    h = make_daemon()
    h.loader.clear_failure = -errno.EIO
    step(h)
    failed = snapshot(h.daemon)
    assert failed["seq"] == 1 and total(h.daemon) == 1200
    assert h.daemon.last_error is None and len(h.daemon._ring) == 2
    assert not h.daemon.file_runtime.availability.available
    step(h, amount=300)
    assert snapshot(h.daemon)["seq"] == 2 and total(h.daemon) == 2400
    assert not snapshot(h.daemon)["capabilities"]["file_io"]
    assert "errno -5" in snapshot(h.daemon)["coverage"]["file_source"]["diagnostic"]
    assert h.daemon.last_error is None and not h.daemon._recover




def test_F_failure_does_not_change_T_D_cadence_or_last_error(make_daemon):
    h = make_daemon()
    h.loader.clear_failure = -errno.EIO
    first = h.daemon.committed_cursor

    step(h)
    failed = snapshot(h.daemon)
    assert failed["seq"] == first.seq + 1
    assert total(h.daemon) == 1200
    assert h.daemon.last_error is None

    h.clock.advance(9)
    h.daemon.step()
    assert snapshot(h.daemon)["seq"] == failed["seq"]
    write_io(h.proc, 300)
    h.clock.advance(1)
    h.daemon.step()
    assert snapshot(h.daemon)["seq"] == failed["seq"] + 1
    assert total(h.daemon) == 2400
    assert h.daemon.last_error is None


def test_file_sqlite_failure_leaves_T_D_connection_ready_for_next_commit(make_daemon):
    h = make_daemon()
    db = h.daemon.collector.store.connection
    db.execute("CREATE TRIGGER fail_F BEFORE INSERT ON file_hour_state BEGIN SELECT RAISE(FAIL,'F failure'); END")
    db.commit()
    step(h)
    assert not db.in_transaction and h.daemon.last_error is None
    assert not h.daemon.file_runtime.availability.available
    step(h, amount=300)
    assert not snapshot(h.daemon)["capabilities"]["file_io"]
    assert total(h.daemon) == 2400 and snapshot(h.daemon)["seq"] == 2


def test_closed_hour_finalized_through_real_daemon_path(make_daemon):
    h = make_daemon()
    # Native cgroup IDs are fixture-directory inode IDs, not live kernel reads.
    owners = h.daemon.collector.file_context.owners_by_cgroup
    cg = next(cg for cg, apps in owners if len(apps) == 1)
    h.loader.queue(1, events=[event(cgroup=cg, amount=57)])
    step(h)
    # Move to the last scheduled frame before the hour; suppress overdue T/D
    # recovery only by taking normal cadence steps, never editing a deadline.
    while h.clock.ns < H:
        step(h, amount=200)
    db = h.daemon.collector.store.connection
    assert db.execute("SELECT bytes_r FROM file_sample WHERE ts_mono_ns=0").fetchone() == (57,)
    assert db.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone() == (H,)


def test_T_D_recovery_keeps_F_worker_and_capabilities_wait_for_committed_snapshot(make_daemon):
    loaders = [VectorLoader(hook_mask=1), VectorLoader()]
    calls = []

    def factory():
        calls.append(len(calls))
        return BpfFileSource(loaders[len(calls) - 1])

    h = make_daemon(factory=factory)
    assert not snapshot(h.daemon)["capabilities"]["file_io"]
    worker = h.daemon.file_runtime
    h.daemon._recover = True
    step(h)
    assert h.daemon.file_runtime is worker and calls == [0]
    # T/D recovery cannot reset F's bounded retry budget. Only the worker's
    # next committed endpoint after the retry deadline permits a fresh attach.
    time.sleep(max(0, worker._runtime._retry_at - time.monotonic()) + 0.02)
    step(h)
    assert calls == [0, 1]
    # Async attachment is advertised by the next committed snapshot.
    step(h)
    assert snapshot(h.daemon)["capabilities"]["file_io"]
    assert snapshot(h.daemon)["coverage"]["file_source"]["diagnostic"] is None
    assert h.daemon.last_error is None


def test_F_never_bypasses_persistent_storage_recovery_backoff(make_daemon):
    h = make_daemon()
    h.daemon._storage_error = "maintenance_failed"
    h.daemon._recover = True
    h.daemon._next_recovery = h.clock.ns + 60 * NS
    before = (h.daemon.committed_cursor, list(h.loader.flip_requests))
    step(h, seconds=10)
    step(h, seconds=10)
    assert (h.daemon.committed_cursor, h.loader.flip_requests) == before


def test_stop_closes_source_without_promoting_partial_hour(make_daemon):
    h = make_daemon()
    step(h)
    d = h.daemon
    connection = d.collector.store.connection
    # Capture before the connection closes via the real stop path.
    captured = []
    close = d.collector.close

    def observe():
        captured.extend(connection.execute("SELECT state_json FROM file_hour_state").fetchall())
        close()

    d.collector.close = observe
    d.stop()
    d.collector.close = close
    assert h.loader.close_requests == 1
    assert captured and '"complete":0' in captured[0][0]




def test_main_logs_daemon_failure_with_traceback(monkeypatch, caplog):
    class FailingDaemon:
        last_error = None
        selector = SimpleNamespace(close=lambda: None)

        def start(self):
            raise RuntimeError("synthetic daemon failure")

        def stop(self):
            return None

    monkeypatch.setattr(iod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(iod, "Daemon", FailingDaemon)
    with caplog.at_level(logging.ERROR, logger="appio.iod"):
        assert iod.main([]) == 1

    failure = next(record for record in caplog.records if record.getMessage() == "daemon_failed")
    assert failure.exc_info is not None
    assert failure.exc_info[0] is RuntimeError
    assert "synthetic daemon failure" in caplog.text


def test_client_diagnostic_bound_clear_and_stale_snapshot_isolation():
    h = Harness()
    try:
        h.handshake()
        first = snapshot_frame(1)
        first["coverage"] = {"file_source": {"diagnostic": "errno 1: " + "x" * 1000}}
        h.send(first)
        h.client.pump()
        assert h.client.state.file_diagnostic.startswith("errno 1")
        assert len(h.client.state.file_diagnostic) <= 512
        stale = snapshot_frame(0)
        stale["coverage"] = {"file_source": {"diagnostic": "STALE"}}
        h.send(stale)
        h.client.pump()
        assert h.client.state.file_diagnostic != "STALE"
        h.send(snapshot_frame(2))
        h.client.pump()
        assert h.client.state.file_diagnostic is None
        assert h.client.state.last_error is None
        assert set(asdict(h.client.state.capabilities)) == {"proc_all", "dev_stat", "file_io", "bpf_loaded"}
    finally:
        h.client.close()
        for server in h.servers:
            server.close()
