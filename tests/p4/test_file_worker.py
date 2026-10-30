"""Real SQLite, real AF_UNIX and a synthetic pipe peer; never BPF or host /proc.

Falsifiers: synchronous F in _sample, unbounded/no-loss handoff, pre-COMMIT
submission, shared SQLite writer, or ignoring shutdown cancellation.
"""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import replace
import json
from pathlib import Path
import queue
import shutil
import socket
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from appio.bpf_fileio import (ACK_RESPONSE, COMMAND_HEADER, DRAIN_RESPONSE,
                              HELLO_RESPONSE, HOOK_MASK_ALL, MAX_MAP_ENTRIES, BpfFileSource)
from appio.file_runtime import _ProcessLoader
from appio.iod import Daemon, DaemonPaths
from appio.ipc_protocol import FrameDecoder, encode_frame
from appio.ipc_security import Peer
from tests.p3.test_iod import Clock, DeferredWorker, FakeFilesystem, write_io

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    pytest.fail("timed out waiting for worker evidence")


@pytest.fixture
def worker_daemon(tmp_path):
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    shutil.copytree(FIXTURES / "proc", proc, symlinks=True)
    shutil.rmtree(proc / "500")
    shutil.copytree(FIXTURES / "cgroup/normal-t0", cg)
    shutil.copyfile(FIXTURES / "cgroup/proc/diskstats", proc / "diskstats")
    write_io(proc, 100)
    state, run = tmp_path / "state", tmp_path / "run"
    state.mkdir(mode=0o711)
    run.mkdir(mode=0o750)
    blocked, release, log = (tmp_path / name for name in ("blocked", "release", "drains"))
    helper, obj = tmp_path / "synthetic-peer", tmp_path / "object"
    obj.touch()
    helper.write_text(
        f"#!{sys.executable}\n"
        "import os,struct,time\nfrom pathlib import Path\n"
        "def read_exact(n):\n"
        "    out=b''\n"
        "    while len(out)<n:\n"
        "        part=os.read(0,n-len(out))\n"
        "        if not part: raise SystemExit\n"
        "        out+=part\n"
        "    return out\n"
        "slot=0\nwhile True:\n"
        f"    abi,op,n=struct.unpack({COMMAND_HEADER.format!r},read_exact(8))\n"
        "    payload=read_exact(n)\n"
        "    if op==1:\n"
        f"        os.write(1,{HELLO_RESPONSE.pack(1, 0, HOOK_MASK_ALL, MAX_MAP_ENTRIES, 0, 0)!r})\n"
        "    elif op==2:\n"
        "        epoch,seq=struct.unpack('<QQ',payload)\n"
        f"        with open({str(log)!r},'a') as out: out.write(str(seq)+'\\n')\n"
        "        if seq==1:\n"
        f"            Path({str(blocked)!r}).touch()\n"
        f"            while not Path({str(release)!r}).exists(): time.sleep(0.005)\n"
        f"        os.write(1,struct.pack({DRAIN_RESPONSE.format!r},1,0,{HOOK_MASK_ALL},{MAX_MAP_ENTRIES},0,slot,epoch,seq,0,0,1,0))\n"
        "        slot=1-slot\n"
        "    else:\n"
        f"        os.write(1,{ACK_RESPONSE.pack(1, 0, 0, 0)!r})\n"
        "        break\n"
    )
    helper.chmod(0o700)
    commands, stopping, started = queue.Queue(), threading.Event(), Future()
    loaders, threads, errors = [], [], []
    clock = Clock()

    def factory():
        threads.append(threading.get_ident())
        loader = _ProcessLoader(str(helper), str(obj))
        loaders.append(loader)
        return BpfFileSource(loader)

    daemon = Daemon(DaemonPaths(state / "iod.db", run / "iod.sock", proc, cg),
                    clock=clock, filesystem=FakeFilesystem(),
                    peer_reader=lambda sock: Peer(7, 0, 0),
                    query_worker=DeferredWorker(), file_source_factory=factory)

    def owner():
        try:
            daemon.start()
            started.set_result(None)
            while not stopping.is_set():
                try:
                    function, future = commands.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        future.set_result(function())
                    except BaseException as exc:
                        future.set_exception(exc)
                if daemon._running:
                    daemon.step()
        except BaseException as exc:
            errors.append(exc)
            if not started.done():
                started.set_exception(exc)
        finally:
            daemon.stop()
            daemon.selector.close()

    thread = threading.Thread(target=owner, name="synthetic-iod-owner")
    thread.start()
    started.result(timeout=3)

    def call(function, timeout=0.35):
        future = Future()
        commands.put((function, future))
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            pytest.fail("iod owner loop was blocked by F")

    def schedule_sample():
        def advance():
            clock.advance(10)
            return daemon.step()
        future = Future()
        commands.put((advance, future))
        return future

    def sample():
        try:
            return schedule_sample().result(timeout=0.35)
        except FutureTimeout:
            pytest.fail("iod step did not return while F was draining")

    def states():
        with sqlite3.connect(state / "iod.db", timeout=0.05) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT state_json FROM file_hour_state")]

    h = SimpleNamespace(daemon=daemon, thread=thread, call=call, sample=sample,
                        blocked=blocked, release=release, log=log, states=states,
                        loaders=loaders, factory_threads=threads, clock=clock,
                        schedule_sample=schedule_sample)
    try:
        eventually(lambda: log.exists() and log.read_text().splitlines() == ["0"])
        yield h
    finally:
        # Independent emergency cleanup also makes the synchronous mutant safe.
        release.touch()
        stopping.set()
        thread.join(5)
        for loader in loaders:
            if loader.process.poll() is None:
                loader.process.kill()
            loader.process.wait(timeout=1)
        assert not thread.is_alive()
        assert not errors, errors



def receive_one(sock):
    decoder = FrameDecoder()
    while True:
        data = sock.recv(65536)
        assert data, "daemon closed a valid unix client"
        messages = decoder.feed(data)
        if messages:
            return messages[0]


def test_real_unix_handshake_and_request_finish_while_F_drain_is_slow(worker_daemon):
    h = worker_daemon
    h.schedule_sample()
    try:
        eventually(h.blocked.exists)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.30)
            client.connect(str(h.daemon.paths.socket))
            started = time.monotonic()
            try:
                # The protocol's status exchange is the client's handshake.
                client.sendall(encode_frame(dict(protocol=1, type="status", request_id="hello")))
                hello = receive_one(client)
                client.sendall(encode_frame(dict(protocol=1, type="status", request_id="request")))
                reply = receive_one(client)
            except TimeoutError:
                pytest.fail("unix handshake/request starved while F drain was pending")
            assert hello["answering"] and reply["answering"]
            assert reply["request_id"] == "request"
            assert time.monotonic() - started < 0.30
            assert not h.release.exists()
    finally:
        h.release.touch()


def test_step_returns_promptly_while_F_worker_is_mid_drain(worker_daemon):
    h = worker_daemon
    h.sample()
    eventually(h.blocked.exists)
    before = time.monotonic()
    h.sample()
    assert time.monotonic() - before < 0.20
    assert h.daemon.committed_cursor.seq == 2
    assert h.daemon.last_error is None
    assert not h.release.exists()


def test_busy_worker_replaces_pending_with_newest_and_persists_F_gap(worker_daemon):
    h = worker_daemon
    h.sample()
    eventually(h.blocked.exists)
    h.sample()  # pending 2
    h.sample()  # replaces 2 with 3
    h.sample()  # replaces 3 with 4; queue is still bounded
    h.release.touch()
    eventually(lambda: "4" in h.log.read_text().splitlines())
    states = eventually(lambda: h.states())
    state = eventually(lambda: next((s for s in h.states()
                                    if s["unknown_owner"]["last_receipt"]["seq"] == 4), None))
    assert h.log.read_text().splitlines() == ["0", "1", "4"]
    assert state["unknown_owner"]["coverage"]["gap_intervals"] >= 2
    assert state["unknown_owner"]["coverage"]["complete"] == 0
    assert states


def test_F_rejects_uncommitted_handoff_before_loader_touches_it(worker_daemon):
    h = worker_daemon
    def submit_uncommitted():
        collector = h.daemon.collector
        db = collector.store.connection
        frame = replace(collector.last_frame, seq=1,
                        observed_mono_ns=collector.last_frame.observed_mono_ns + 10**10)
        context = replace(collector.file_context, seq=1,
                          observed_mono_ns=frame.observed_mono_ns)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("UPDATE sampler_checkpoint SET seq=?,checkpoint_mono_ns=? WHERE source='iod'",
                       (1, frame.observed_mono_ns))
            with pytest.raises(ValueError, match="COMMIT"):
                h.daemon.file_runtime.submit_committed(db, frame, context)
        finally:
            db.rollback()
    h.call(submit_uncommitted)
    assert h.log.read_text().splitlines() == ["0"]
    assert not h.blocked.exists()


def test_slow_F_persistence_does_not_hold_T_D_writer_or_step(worker_daemon, monkeypatch):
    from appio.file_store import FileHourStore
    h = worker_daemon
    entered, release = threading.Event(), threading.Event()
    apply_pending = FileHourStore.apply_pending
    def slow(store, snapshot):
        entered.set()
        assert release.wait(2)
        return apply_pending(store, snapshot)
    monkeypatch.setattr(FileHourStore, "apply_pending", slow)
    h.release.touch()
    try:
        h.sample()
        assert entered.wait(1)
        before = time.monotonic()
        h.sample()
        assert time.monotonic() - before < 0.20
        assert h.daemon.committed_cursor.seq == 2
        assert h.daemon.last_error is None
        assert h.daemon.collector.store.connection is not h.daemon.file_runtime._runtime.connection
    finally:
        release.set()


def test_stop_cancels_SQLite_work_on_F_connection(worker_daemon, monkeypatch):
    from appio.file_store import FileHourStore
    h = worker_daemon
    entered = threading.Event()
    def long_query(store, snapshot):
        entered.set()
        store.connection.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL "
                                 "SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n").fetchone()
    monkeypatch.setattr(FileHourStore, "apply_pending", long_query)
    h.release.touch()
    h.sample()
    assert entered.wait(1)
    before = time.monotonic()
    h.call(h.daemon.stop, timeout=0.8)
    assert time.monotonic() - before < 0.75
    assert not any(t.is_alive() and t.name == "appio-file-worker" for t in threading.enumerate())


def test_F_never_consumes_before_T_D_commit(worker_daemon):
    h = worker_daemon
    observed = []
    def fail_commit():
        db = h.daemon.collector.store.connection
        def authorizer(action, operation, *_):
            if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
                observed.append(h.log.read_text().splitlines())
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        h.clock.advance(10)
        h.daemon.step()
        db.set_authorizer(None)
    h.call(fail_commit)
    assert observed and all(rows == ["0"] for rows in observed)
    assert h.daemon.committed_cursor.seq == 0
    assert h.log.read_text().splitlines() == ["0"]
    assert h.daemon.last_error == "collection_failed"


def test_stop_is_bounded_mid_drain_and_leaves_no_F_thread(worker_daemon):
    h = worker_daemon
    h.schedule_sample()
    eventually(h.blocked.exists)
    started = time.monotonic()
    h.call(h.daemon.stop, timeout=0.8)
    assert time.monotonic() - started < 0.75
    assert not h.release.exists()
    assert all(loader.process.poll() is not None for loader in h.loaders)
    assert not any(t.is_alive() and t.name == "appio-file-worker" for t in threading.enumerate())


def test_shutdown_records_abandoned_pending_frames_as_F_gaps(worker_daemon):
    h = worker_daemon
    h.sample()
    eventually(h.blocked.exists)
    h.sample()
    h.sample()
    worker = h.daemon.file_runtime
    h.call(h.daemon.stop, timeout=0.8)
    state = h.states()[0]
    assert state["unknown_owner"]["last_receipt"]["seq"] == 3
    assert state["unknown_owner"]["coverage"]["gap_intervals"] >= 2
    assert state["unknown_owner"]["coverage"]["complete"] == 0
    assert h.log.read_text().splitlines() == ["0", "1"]
    assert worker.submit_committed(None, None, None) is False


def test_T_D_recovery_does_not_join_or_replace_busy_F_worker(worker_daemon):
    h = worker_daemon
    h.sample()
    eventually(h.blocked.exists)
    worker = h.daemon.file_runtime
    def recover():
        h.daemon._recover = True
        h.clock.advance(10)
        h.daemon.step()
    h.call(recover)
    assert h.daemon.file_runtime is worker, "T/D recovery joined/replaced the F worker"
    assert h.daemon.last_error is None
    assert not h.release.exists()


def test_loader_and_F_connection_are_owned_by_worker(worker_daemon):
    h = worker_daemon
    assert h.factory_threads and h.factory_threads[0] != h.thread.ident
    # Connection remains SQLite thread-affine: an owner-thread access is refused.
    def wrong_thread():
        with pytest.raises(sqlite3.ProgrammingError, match="same thread"):
            h.daemon.file_runtime._runtime.connection.execute("SELECT 1")
    h.call(wrong_thread)
