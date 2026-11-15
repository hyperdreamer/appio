"""Rootless runtime tests against real accounting, SQLite and AF_UNIX pairs.

The filesystem seam changes credential metadata only, never the accounting path.
Falsifiers are executable mutations of publication, budgets and worker isolation.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import asdict
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import appio.iod as iod
from appio.iod import Daemon, DaemonPaths, RuntimeFilesystem
from appio.ipc_models import QueryRequest, StreamCursor, validate_request, validate_server_message
from appio.contracts import Window
from appio.ipc_protocol import FrameDecoder, ProtocolError, encode_frame
from appio.ipc_security import Peer

NS = 1_000_000_000
BOOT = "11111111-2222-4333-8444-555555555555"
APP = "unit:app-chromium@fixture.service"
FIXTURES = Path(__file__).parents[1] / "fixtures"


class Clock:
    def __init__(self):
        self.ns = 10 * NS

    def __call__(self):
        return self.ns, 1_700_000_000 * NS + self.ns

    def advance(self, seconds=2):
        self.ns += int(seconds * NS)


class PollSelector(selectors.DefaultSelector):
    def select(self, timeout=None):
        return super().select(0)


class FakeFilesystem(RuntimeFilesystem):
    """Do actual tmp-tree I/O; inject root ownership instead of requiring root."""
    effective_uid = 0
    appio_gid = 987

    def __init__(self):
        self.overrides = {}

    def lstat(self, path):
        result = os.lstat(path)
        if Path(path) in self.overrides:
            return self.overrides[Path(path)]
        return self._metadata(result)

    def fstat(self, fd):
        return self._metadata(os.fstat(fd))

    @staticmethod
    def _metadata(result):
        return SimpleNamespace(st_mode=result.st_mode, st_uid=0, st_gid=987,
                               st_dev=result.st_dev, st_ino=result.st_ino,
                               st_nlink=result.st_nlink, st_size=result.st_size)


class DeferredWorker:
    def __init__(self):
        self.jobs = []
        self.stopped = False

    def submit(self, request, *, request_id):
        future = Future()
        self.jobs.append((request, request_id, future))
        return future

    def shutdown(self, timeout=0.5):
        self.stopped = True
        for _, _, future in self.jobs:
            future.cancel()
        return True


class SocketView:
    def __init__(self, sock, *, stalled=False):
        self.sock, self.stalled = sock, stalled
        self.read_bytes = self.sent_bytes = 0

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def recv(self, amount):
        data = self.sock.recv(amount)
        self.read_bytes += len(data)
        return data

    def send(self, data):
        if self.stalled:
            raise BlockingIOError()
        sent = self.sock.send(data)
        self.sent_bytes += sent
        return sent


def write_io(root, amount):
    for pid in (410, 411, 412, 413):
        (root / str(pid) / "io").write_text(
            f"rchar: {amount * 10}\nwchar: {amount * 5}\nsyscr: 0\nsyscw: 0\n"
            f"read_bytes: {amount * 3}\nwrite_bytes: {amount * 2}\ncancelled_write_bytes: 0\n")


@pytest.fixture
def runtime(tmp_path, request):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    shutil.copytree(FIXTURES / "proc", proc, symlinks=True)
    shutil.copytree(FIXTURES / "cgroup/normal-t0", cgroup)
    shutil.rmtree(proc / "500")
    shutil.copyfile(FIXTURES / "cgroup/proc/diskstats", proc / "diskstats")
    write_io(proc, 100)
    state, run = tmp_path / "state", tmp_path / "run"
    state.mkdir(mode=0o711)
    run.mkdir(mode=0o750)
    paths = DaemonPaths(state / "iod.db", run / "iod.sock", proc, cgroup)
    clock, fs, worker = Clock(), FakeFilesystem(), DeferredWorker()
    peers = {}
    # Group 1 — harness advance() defaults: behavioural vectors advance 2 s.
    # Empty indirect parameters select the shipped cadence for regression pins.
    cadence = getattr(request, "param", {"sample_ns": 2 * NS, "flush_ns": 5 * NS})
    daemon = Daemon(paths, clock=clock, peer_reader=lambda sock: peers.get(sock.fileno(), Peer(7, 0, 0)),
                    group_lookup=lambda uid: frozenset({987}) if uid == 1000 else frozenset(),
                    filesystem=fs, selector=PollSelector(), query_worker=worker, **cadence)
    clients = []

    def connect(peer=None, stalled=False):
        server, client = socket.socketpair()
        client.setblocking(False)
        wrapped = SocketView(server, stalled=stalled)
        if peer is not None:
            peers[server.fileno()] = peer
        accepted = daemon._accept_client(wrapped)
        clients.append(client)
        return client, wrapped, accepted

    value = SimpleNamespace(daemon=daemon, paths=paths, clock=clock, fs=fs, worker=worker,
                            connect=connect, proc=proc, cgroup=cgroup, clients=clients)
    yield value
    daemon.stop()
    daemon.selector.close()
    for client in clients:
        client.close()


def request(client, kind="status", request_id="r", **kwargs):
    client.sendall(encode_frame({"protocol": 1, "type": kind, "request_id": request_id, **kwargs}))


def receive(client):
    decoder = FrameDecoder()
    rows = []
    while True:
        try:
            data = client.recv(65536)
        except BlockingIOError:
            break
        if not data:
            break
        rows.extend(decoder.feed(data))
    decoder.eof()
    for row in rows:
        validate_server_message(row)
    return rows


def pump(runtime, count=4):
    for _ in range(count):
        runtime.daemon.step()


def total(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT rchar,wchar,read_bytes,write_bytes FROM app_boot_total").fetchone()


def test_two_peers_see_only_committed_baseline_and_measured_device_bytes(runtime):
    r = runtime
    r.daemon.start()
    assert r.daemon.committed_cursor == StreamCursor(BOOT, 1, 0)
    assert total(r.paths.database) == (0, 0, 0, 0)
    clients = [r.connect()[0] for _ in range(2)]
    for client in clients:
        request(client, "subscribe", since=None)
    pump(r)
    assert [[row["seq"] for row in receive(c) if row["type"] == "snapshot"] for c in clients] == [[0], [0]]
    write_io(r.proc, 102)
    leaf = next(path for path in r.cgroup.rglob("io.stat") if "chromium" in str(path))
    leaf.write_text("259:0 rbytes=1123 wbytes=245\n259:1 rbytes=307 wbytes=509\n")
    r.clock.advance()
    pump(r)
    assert total(r.paths.database) == (80, 40, 24, 16)
    for client in clients:
        snapshots = [row for row in receive(client) if row["type"] == "snapshot"]
        assert [row["seq"] for row in snapshots] == [1]
        snap = snapshots[0]
        assert snap["source"] == "iod" and not snap["capabilities"]["file_io"]
        assert [(d["dev"], d["name"], d["rbytes"], d["wbytes"]) for d in snap["devs"]] == [
            ("259:0", "nvme0n1", 123, 45), ("259:1", "nvme0n1p1", 7, 9)]
        assert all(not any(key in p for key in ("app_id", "launch_id", "path_id")) for p in snap["procs"])
        assert sum(p["read_bytes"] for p in snap["procs"]) == 24


def test_no_gui_is_required_and_stop_final_flush_survives_restart(runtime):
    r = runtime
    r.daemon.start()
    write_io(r.proc, 102)
    for _ in range(3):
        r.clock.advance()
        r.daemon.step()
    assert total(r.paths.database) == (80, 40, 24, 16)
    write_io(r.proc, 103)
    r.clock.advance(1)
    r.daemon.stop()
    assert total(r.paths.database) == (120, 60, 36, 24)
    # Unobserved work at restart must not be re-imported as a lifetime delta.
    write_io(r.proc, 200)
    r.clock.advance(4)
    r.daemon.start()
    assert r.daemon.committed_cursor == StreamCursor(BOOT, 2, 0)
    assert total(r.paths.database) == (120, 60, 36, 24)
    with sqlite3.connect(r.paths.database) as connection:
        assert connection.execute("SELECT from_mono_ns,to_mono_ns FROM gap WHERE reason='collector_gap'").fetchall() == [
            (17 * NS, 21 * NS)]
    write_io(r.proc, 201)
    r.clock.advance()
    r.daemon.step()
    assert total(r.paths.database) == (160, 80, 48, 32)


def test_commit_failure_never_publishes_or_advances_and_recovery_is_a_gap(runtime):
    r = runtime
    r.daemon.start()
    client = r.connect()[0]
    request(client, "subscribe", since=None)
    pump(r)
    receive(client)
    connection = r.daemon.collector.store.connection
    connection.set_authorizer(lambda action, a, b, db, src:
                              sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and a == "COMMIT" else sqlite3.SQLITE_OK)
    before = r.daemon.committed_cursor
    write_io(r.proc, 102)
    r.clock.advance()
    r.daemon.step()
    assert r.daemon.committed_cursor == before
    assert r.daemon.collector.last_frame.seq == 0
    assert total(r.paths.database) == (0, 0, 0, 0)
    failed = receive(client)
    assert [row["type"] for row in failed] == ["gap", "status"]
    assert failed[0]["reason"] == "collection_failed" and failed[0]["current"] == asdict(before)
    assert failed[1]["answering"] is False
    assert r.daemon.last_error == "collection_failed"
    request(client)
    pump(r)
    assert receive(client)[0]["answering"] is False
    connection.set_authorizer(None)
    r.clock.advance()
    pump(r)
    assert r.daemon.committed_cursor == StreamCursor(BOOT, 2, 0)
    assert total(r.paths.database) == (0, 0, 0, 0)
    assert any(row["type"] == "gap" for row in receive(client))
    with sqlite3.connect(r.paths.database) as connection:
        assert connection.execute("SELECT count(*) FROM gap").fetchone()[0] >= 1


def test_slow_peer_and_pending_history_do_not_block_collection(runtime):
    r = runtime
    r.daemon.start()
    slow, _, _ = r.connect(stalled=True)
    fast, _, _ = r.connect()
    request(slow, "subscribe", since=None)
    request(fast, "query", query={"kind": "ranking", "window": {"kind": "lifetime"}})
    pump(r)
    assert len(r.worker.jobs) == 1
    write_io(r.proc, 101)
    r.clock.advance()
    pump(r)
    assert total(r.paths.database) == (40, 20, 12, 8)
    assert len(r.daemon.clients) == 1
    assert any(record.reason == "slow_client" for record in r.daemon.refusals.records)
    request(fast, request_id="still-alive")
    pump(r)
    assert any(row.get("request_id") == "still-alive" for row in receive(fast))
    assert not r.worker.jobs[0][2].done()
    fast.close()
    pump(r)
    assert r.worker.jobs[0][2].cancelled()
    r.clock.advance()
    r.daemon.step()
    assert r.daemon.committed_cursor.seq == 2


def test_flood_read_bytes_and_requests_are_bounded_per_turn(runtime, monkeypatch):
    import appio.ipc_protocol as protocol
    decode = protocol._decode_payload
    decoded = []

    def counted(payload):
        decoded.append(len(payload))
        return decode(payload)

    monkeypatch.setattr(protocol, "_decode_payload", counted)
    r = runtime
    r.daemon.start()
    client, server, _ = r.connect()
    payload = b"".join(encode_frame({"protocol": 1, "type": "status", "request_id": str(i)}) for i in range(2000))
    assert len(payload) > 65536
    client.sendall(payload)
    write_io(r.proc, 101)
    r.clock.advance()
    r.daemon.step()
    assert r.daemon.committed_cursor.seq == 1
    assert 0 < server.read_bytes <= 65536
    state = next(iter(r.daemon.clients.values()))
    assert len(decoded) == state.requests_processed == 8  # Bound JSON parsing, not only dispatch.
    assert server.sent_bytes <= 65536
    # Buffered frames must be serviced even when no more socket data arrives.
    pump(r, 2)
    assert state.requests_processed == 24


@pytest.mark.parametrize("payload", [b"\x00", struct.pack("!I", 100) + b"{"])
def test_incomplete_header_or_body_deadline_is_absolute(runtime, payload):
    r = runtime
    r.daemon.start()
    client, _, _ = r.connect()
    client.send(payload)
    r.daemon.step()
    r.clock.advance(1)
    client.send(b" ")  # trickling must not reset the two-second deadline
    r.daemon.step()
    r.clock.advance(1)
    r.daemon.step()
    assert not r.daemon.clients
    assert r.daemon.refusals.records[-1].reason == "frame_timeout"
    assert r.daemon.committed_cursor.seq == 1


@pytest.mark.parametrize("payload", [struct.pack("!I", 1048577), struct.pack("!I", 0),
                                    encode_frame({"protocol": 1, "type": "exec", "request_id": "bad"})])
def test_malformed_or_privileged_frames_disconnect_only_that_client(runtime, payload):
    r = runtime
    r.daemon.start()
    bad = r.connect()[0]
    good = r.connect()[0]
    bad.sendall(payload)
    request(good)
    pump(r)
    assert len(r.daemon.clients) == 1
    assert receive(good)[0]["type"] == "status"


def test_credentials_are_checked_before_decode_and_65th_client_is_refused(runtime):
    r = runtime
    r.daemon.start()
    refused, server, accepted = r.connect(Peer(8, 2000, 2000))
    assert not accepted and server.read_bytes == 0
    assert r.daemon.refusals.records[-1].reason == "not_member"
    assert r.connect(Peer(9, 1000, 1000))[2]
    assert r.connect(Peer(10, 0, 0))[2]
    for _ in range(62):
        assert r.connect()[2]
    assert not r.connect()[2]
    assert len(r.daemon.clients) == 64
    assert r.daemon.refusals.records[-1].reason == "client_limit"


def test_second_writer_cannot_touch_database_socket_or_lock_inode(runtime):
    r = runtime
    r.daemon.start()
    before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in
              (r.paths.database, r.paths.socket, r.paths.database.with_name("iod.lock"))}
    other = Daemon(r.paths, clock=r.clock, filesystem=r.fs, selector=PollSelector(), query_worker=DeferredWorker())
    with pytest.raises(BlockingIOError):
        other.start()
    after = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in before}
    assert before == after
    other.stop()
    assert r.paths.socket.exists()
    inode = r.paths.database.with_name("iod.lock").stat().st_ino
    r.daemon.stop()
    assert r.paths.database.with_name("iod.lock").stat().st_ino == inode


@pytest.mark.parametrize("kind", ["symlink", "foreign", "regular"])
def test_stale_socket_refuses_untrusted_paths_without_removing_them(runtime, kind):
    r = runtime
    path = r.paths.socket
    if kind == "symlink":
        path.symlink_to(r.paths.database)
    elif kind == "regular":
        path.write_text("not a socket")
    else:
        old = socket.socket(socket.AF_UNIX)
        old.bind(str(path))
        old.close()
        os.chmod(path, 0o660)
        found = r.fs.lstat(path)
        found.st_uid = 1000
        r.fs.overrides[path] = found
    with pytest.raises(PermissionError):
        r.daemon.start()
    assert os.path.lexists(path)


def test_valid_stale_socket_is_replaced_but_stop_does_not_delete_a_replacement(runtime):
    r = runtime
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(r.paths.socket))
    stale.close()
    os.chmod(r.paths.socket, 0o660)
    r.daemon.start()
    r.paths.socket.unlink()
    r.paths.socket.write_text("replacement")
    r.daemon.stop()
    assert r.paths.socket.read_text() == "replacement"


def test_sqlite_durability_and_private_sidecars(runtime):
    r = runtime
    r.daemon.start()
    connection = r.daemon.collector.store.connection
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(r.paths.database) + suffix)
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pending_activation_uses_peer_uid_and_invalid_state_is_not_success(runtime):
    r = runtime
    path = r.paths.database.with_name("setup-state.json")
    path.write_text(json.dumps({"version": 1, "pending_by_uid": {"1000": False, "1001": True}}))
    path.chmod(0o600)
    r.daemon.start()
    member = r.connect(Peer(12, 1000, 987))[0]
    request(member)
    pump(r)
    assert receive(member)[0]["pending_activation"] is False
    path.write_text('{"version":1,"pending_by_uid":{"1000":false,"1000":true}}')
    request(member)
    pump(r)
    assert receive(member)[0]["pending_activation"] is True  # protocol bool: unknown must fail closed
    path.write_text(json.dumps({"version": 1, "pending_by_uid": {"1000": False}}))
    path.chmod(0o666)
    request(member)
    pump(r)
    assert receive(member)[0]["pending_activation"] is True


def test_replay_and_unknown_boot_cursor_have_explicit_gap(runtime):
    r = runtime
    r.daemon.start()
    baseline = r.daemon.committed_cursor
    r.clock.advance()
    pump(r)
    client = r.connect()[0]
    request(client, "subscribe", since=asdict(baseline))
    pump(r)
    rows = receive(client)
    assert [row["seq"] for row in rows if row["type"] == "snapshot"] == [1]
    request(client, "subscribe", since={"boot_id": "different-boot", "iod_epoch": 1, "seq": 0})
    pump(r)
    rows = receive(client)
    assert [row["type"] for row in rows] == ["gap", "snapshot"]
    assert rows[0]["previous"]["boot_id"] == "different-boot"


def test_a_settled_query_job_wakes_the_loop_instead_of_the_idle_poll(runtime, monkeypatch):
    """Falsifier: a completed worker future is not a readable socket, so every
    answer waited out the idle select timeout before it was even enqueued -- one
    poll per query, 400+ queries per console frame.
    """

    r = runtime
    monkeypatch.setattr(iod, "IDLE_SELECT_TIMEOUT_S", 2.0)
    r.daemon.selector = selectors.DefaultSelector()
    r.daemon.start()
    client = r.connect()[0]
    request(client, "query", request_id="wake", query={"kind": "ranking", "window": {"kind": "lifetime"}})
    r.daemon.step()  # readable socket: reads the request and submits the job
    assert len(r.worker.jobs) == 1
    _, _, future = r.worker.jobs[0]

    def settle():
        time.sleep(0.05)
        future.set_result({"protocol": 1, "type": "result", "request_id": "wake", "kind": "ranking",
                           "payload": [{"rows": [], "next_cursor": None, "snapshot": "s"}]})

    thread = threading.Thread(target=settle)
    thread.start()
    started = time.monotonic()
    r.daemon.step()  # blocks in select(2.0) unless the wake byte arrives
    elapsed = time.monotonic() - started
    thread.join(timeout=5)
    assert elapsed < 1.0, f"the loop slept {elapsed:.2f}s on a settled job"
    assert [row["request_id"] for row in receive(client)] == ["wake"]


def test_query_has_one_inflight_per_client_and_exact_a22_errors(runtime):
    r = runtime
    r.daemon.start()
    client = r.connect()[0]
    for index in range(2):
        request(client, "query", request_id=str(index), query={"kind": "ranking", "window": {"kind": "lifetime"}})
    pump(r)
    assert len(r.worker.jobs) == 1
    assert receive(client)[0]["reason"] == "query_busy"
    request(client, "query", query={"kind": "ranking", "window": {"kind": "launch", "boot_id": BOOT}})
    pump(r)
    reply = receive(client)[0]
    from appio.contracts import LAUNCH_WINDOW_MESSAGE
    assert reply["reason"] == "launch_attribution" and reply["message"] == LAUNCH_WINDOW_MESSAGE


def test_main_has_no_fixture_options_and_enforces_root(monkeypatch):
    monkeypatch.setattr(iod.os, "geteuid", lambda: 1000)
    assert iod.main([]) != 0
    with pytest.raises(SystemExit) as result:
        iod.main(["--proc-root=/tmp/spoof"])
    assert result.value.code != 0


@pytest.mark.parametrize("fail_commit", [False, True])
def test_sigterm_equivalent_main_flushes_on_owner_thread_and_restores_handlers(runtime, monkeypatch, fail_commit):
    r = runtime
    handlers = {iod.signal.SIGTERM: iod.signal.SIG_DFL, iod.signal.SIGINT: iod.signal.SIG_DFL}
    original = dict(handlers)

    def signal(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    step = r.daemon.step

    def signal_after_step():
        write_io(r.proc, 101)
        r.clock.advance(1)
        step()
        if fail_commit:
            r.daemon.collector.store.connection.set_authorizer(
                lambda action, a, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and a == "COMMIT" else sqlite3.SQLITE_OK)
        handlers[iod.signal.SIGTERM](iod.signal.SIGTERM, None)

    monkeypatch.setattr(iod, "Daemon", lambda: r.daemon)
    monkeypatch.setattr(iod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(iod.signal, "signal", signal)
    monkeypatch.setattr(r.daemon, "step", signal_after_step)
    assert iod.main([]) == int(fail_commit)
    assert handlers == original
    assert total(r.paths.database) == ((0, 0, 0, 0) if fail_commit else (40, 20, 12, 8))
    assert r.paths.database.with_name("iod.lock").exists()
    assert not r.paths.socket.exists()


def test_real_IodClient_handshakes_and_subscribes_over_bound_unix_socket(runtime, caplog):
    """Catch a daemon/client integration fault invisible to scripted peers.

    The connector, listener, accept loop, framing and both protocol consumers are
    real. Only host credentials/metadata and collection inputs use rootless seams.
    """
    from appio.bpf_fileio import BpfFileSource, VectorLoader
    from appio.iodclient import IodClient
    from appio.ipc_security import read_peer, verify_runtime_paths

    r = runtime
    # Use actual kernel client credentials; only root metadata/server identity
    # need injection to run a privileged daemon contract as an ordinary user.
    r.daemon.peer_reader = read_peer
    r.daemon.group_lookup = lambda uid: frozenset({987}) if uid == os.getuid() else frozenset()
    r.daemon._file_source_factory = lambda: BpfFileSource(VectorLoader())
    r.daemon.start()
    checked = []

    def verify(path):
        verify_runtime_paths(path, metadata=r.fs.lstat, appio_gid=987)
        checked.append(path)

    gaps, changes = [], []
    client = IodClient(
        r.paths.socket, on_gap=gaps.append, on_change=lambda: changes.append(client.state),
        clock_ns=lambda: r.clock.ns, path_verifier=verify,
        peer_reader=lambda sock: Peer(os.getpid(), 0, 987),
    )
    try:
        client.connect()
        for _ in range(12):
            r.daemon.step()
            client.pump()
        evidence = f"daemon_refusals={r.daemon.refusals.records!r}; client_state={client.state!r}; logs={caplog.text}"
        print(evidence)
        assert checked == [r.paths.socket], evidence
        assert client.state.verified and client.state.answering, evidence
        assert client.state.cursor == r.daemon.committed_cursor, evidence
        assert client.state.capabilities is not None, evidence
        assert client.state.capabilities.proc_all, evidence
        assert len(r.daemon.clients) == 1, evidence
        assert next(iter(r.daemon.clients.values())).subscribed, evidence
        assert not r.daemon.refusals.records and not gaps, evidence
        assert changes
    finally:
        client.close()


@pytest.mark.parametrize("operation, error_number", [("send", 32), ("recv", 104)])
def test_real_socket_transport_refusal_names_operation_and_errno(runtime, caplog, operation, error_number):
    """A client timeout/reset can produce the symptom without a wire mismatch."""
    from appio.bpf_fileio import BpfFileSource, VectorLoader
    from appio.iodclient import IodClient, NO_ANSWER_NS
    from appio.ipc_security import read_peer, verify_runtime_paths

    r = runtime
    r.daemon.peer_reader = read_peer
    r.daemon.group_lookup = lambda uid: frozenset({987}) if uid == os.getuid() else frozenset()
    r.daemon._file_source_factory = lambda: BpfFileSource(VectorLoader())
    r.daemon.start()
    client = IodClient(
        r.paths.socket, on_gap=lambda gap: None, on_change=lambda: None,
        clock_ns=lambda: r.clock.ns,
        path_verifier=lambda path: verify_runtime_paths(path, metadata=r.fs.lstat, appio_gid=987),
        peer_reader=lambda sock: Peer(os.getpid(), 0, 987),
    )
    try:
        client.connect()  # Real connector writes the framed status request.
        if operation == "send":
            # Reproduce a deadline while the owner loop has not serviced IPC.
            r.clock.ns += NO_ANSWER_NS
            client.pump()
            assert not client.state.verified and not client.state.answering
            assert client.state.last_error == "daemon_not_answering"
        else:
            # Closing with an unread status response resets this real socket.
            pump(r)
            client.close()
        pump(r)
        evidence = f"daemon_refusals={r.daemon.refusals.records!r}; client_state={client.state!r}; logs={caplog.text}"
        print(evidence)
        assert r.daemon.refusals.records[-1].reason == "transport_error", evidence
        assert f"reason=transport_error uid={os.getuid()} operation={operation} errno={error_number}" in caplog.text, evidence
        assert all(len(record.message) < 256 for record in caplog.records)
    finally:
        client.close()


def test_accept_loop_and_refusal_logs_never_include_raw_payload(runtime, caplog):
    r = runtime
    r.daemon.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    r.clients.append(client)
    client.connect(str(r.paths.socket))
    client.setblocking(False)
    request(client)
    pump(r)
    assert receive(client)[0]["type"] == "status"
    secret = "private-untrusted-argument" * 100
    client.sendall(encode_frame({"protocol": 1, "type": secret, "request_id": "r"}))
    pump(r)
    assert not r.daemon.clients
    assert "unsupported_type" in caplog.text and secret not in caplog.text
    assert all(len(record.message) < 256 for record in caplog.records)


def test_crash_without_stop_preserves_totals_and_releases_lock(runtime):
    r = runtime
    script = '''
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("iod_fixtures", "tests/p3/test_iod.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
state, sock, proc, cgroup = map(Path, sys.argv[1:])
clock = m.Clock()
# Group 2 — embedded subprocess: match m.Clock.advance()'s 2 s test vector.
daemon = m.Daemon(m.DaemonPaths(state, sock, proc, cgroup), clock=clock,
                  filesystem=m.FakeFilesystem(), query_worker=m.DeferredWorker(),
                  sample_ns=2 * m.NS, flush_ns=5 * m.NS)
daemon.start()
m.write_io(proc, 101)
clock.advance()
daemon.step()
os._exit(0)
'''
    result = subprocess.run([sys.executable, "-c", script, str(r.paths.database), str(r.paths.socket),
                             str(r.proc), str(r.cgroup)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert total(r.paths.database) == (40, 20, 12, 8)
    inode = r.paths.database.with_name("iod.lock").stat().st_ino
    r.clock.advance(8)
    write_io(r.proc, 130)
    r.daemon.start()
    assert r.daemon.committed_cursor == StreamCursor(BOOT, 2, 0)
    assert total(r.paths.database) == (40, 20, 12, 8)
    assert r.paths.database.with_name("iod.lock").stat().st_ino == inode
    with sqlite3.connect(r.paths.database) as connection:
        assert connection.execute("SELECT from_mono_ns,to_mono_ns FROM gap WHERE reason='collector_gap'").fetchall() == [(12 * NS, 18 * NS)]
    write_io(r.proc, 131)
    r.clock.advance()
    pump(r)
    assert total(r.paths.database) == (80, 40, 24, 16)


def test_actual_history_worker_uses_its_own_readonly_connection(runtime):
    r = runtime
    r.daemon._injected_worker = None
    r.daemon.start()
    member = r.connect(Peer(12, 1000, 987))[0]
    write_io(r.proc, 102)
    r.clock.advance()
    pump(r)
    request(member, "query", query={"kind": "totals", "app_key": APP, "window": {"kind": "lifetime"}})
    deadline = time.monotonic() + 2
    replies = []
    while time.monotonic() < deadline and not replies:
        pump(r)
        replies.extend(receive(member))
        time.sleep(0.001)
    assert replies and replies[0]["type"] == "result"
    assert replies[0]["payload"]["value"]["task_total_bytes"] == 40
    # Mutating the writer's authorizer after the request does not affect readers.
    r.daemon.collector.store.connection.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
    request(member, "query", query={"kind": "totals", "app_key": APP, "window": {"kind": "lifetime"}})
    replies = []
    while time.monotonic() < deadline and not replies:
        pump(r)
        replies.extend(receive(member))
        time.sleep(0.001)
    r.daemon.collector.store.connection.set_authorizer(None)
    assert replies and replies[0]["type"] == "result"


def test_history_worker_concurrency_queue_and_shutdown_are_bounded(runtime, monkeypatch):
    r = runtime
    r.daemon.start()
    entered = threading.Barrier(3)
    release = threading.Event()
    threads = set()
    readonly = []

    class BlockedRpc:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, request, *, request_id):
            threads.add(threading.get_ident())
            try:
                self.connection.execute("DELETE FROM boot")
            except sqlite3.OperationalError as error:
                readonly.append("readonly" in str(error))
            entered.wait(timeout=2)
            release.wait(timeout=2)
            return {"protocol": 1, "type": "result", "request_id": request_id,
                    "kind": "totals", "payload": {}}

    monkeypatch.setattr(iod, "HistoryRpc", BlockedRpc)
    worker = iod.HistoryWorker(r.paths.database)
    query = QueryRequest("totals", Window("lifetime"))
    try:
        running = [worker.submit(query, request_id=str(index)) for index in range(2)]
        entered.wait(timeout=2)
        waiting = [worker.submit(query, request_id=str(index)) for index in range(64)]
        with pytest.raises(ProtocolError, match="query_busy"):
            worker.submit(query, request_id="overflow")
        started = time.monotonic()
        assert worker.shutdown(timeout=0.05) is False
        assert time.monotonic() - started < 0.2
        assert all(future.cancelled() for future in waiting)
        assert len(threads) == 2 and readonly == [True, True]
    finally:
        release.set()
        assert worker.shutdown(timeout=1)
    assert all(future.done() for future in running)


def test_overdue_collection_becomes_a_gap_not_an_invented_rate(runtime):
    r = runtime
    r.daemon.start()
    client = r.connect()[0]
    request(client, "subscribe", since=None)
    pump(r)
    receive(client)
    write_io(r.proc, 105)
    r.clock.advance(6)
    pump(r)
    assert total(r.paths.database) == (0, 0, 0, 0)
    assert r.daemon.committed_cursor == StreamCursor(BOOT, 2, 0)
    assert [row["type"] for row in receive(client)] == ["gap", "snapshot"]
    with sqlite3.connect(r.paths.database) as connection:
        assert connection.execute("SELECT from_mono_ns,to_mono_ns FROM gap WHERE reason='collector_gap'").fetchall() == [(10 * NS, 16 * NS)]


def test_replay_is_count_and_byte_bounded_and_eviction_resyncs(runtime):
    r = runtime
    r.daemon.start()
    old = r.daemon.committed_cursor
    for _ in range(66):
        r.clock.advance()
        r.daemon.step()
    assert len(r.daemon._ring) == 64
    assert sum(len(item.frame) for item in r.daemon._ring) <= 4 * 1024 * 1024
    client = r.connect()[0]
    request(client, "subscribe", since=asdict(old))
    pump(r)
    assert [row["type"] for row in receive(client)] == ["gap", "snapshot"]


def test_oversized_snapshot_keeps_status_query_and_collection_alive(runtime, monkeypatch):
    r = runtime
    original = r.daemon._snapshot

    def oversized(frame, result):
        reply = original(frame, result)
        reply["procs"] = [{"app_key": "x" * (256 * 1024)}]
        return reply

    monkeypatch.setattr(r.daemon, "_snapshot", oversized)
    r.daemon.start()
    client = r.connect()[0]
    request(client, "subscribe", since=None)
    request(client, "query", query={"kind": "ranking", "window": {"kind": "lifetime"}})
    pump(r)
    rows = receive(client)
    assert [row["type"] for row in rows] == ["gap", "status"]
    assert rows[0]["reason"] == "snapshot_too_large"
    assert rows[1]["cursor"] == asdict(r.daemon.committed_cursor)
    assert len(r.worker.jobs) == 1 and len(r.daemon.clients) == 1
    write_io(r.proc, 101)
    r.clock.advance()
    pump(r)
    assert total(r.paths.database) == (40, 20, 12, 8)
    assert [row["type"] for row in receive(client)] == ["gap", "status"]


def test_quota_pause_does_not_append_more_frames_and_resumes_with_gap(runtime, monkeypatch):
    r = runtime
    r.daemon.start()
    from appio.quota import PruneResult
    paused = True

    def shrink():
        return PruneResult((), not paused, paused, 4096, "quota_exhausted" if paused else None)

    monkeypatch.setattr(r.daemon._pruner, "shrink_if_needed", shrink)
    r.daemon._next_maintenance = r.clock()[0]
    pump(r)
    before = r.daemon.committed_cursor
    write_io(r.proc, 105)
    r.clock.advance()
    pump(r)
    assert r.daemon.committed_cursor == before
    assert r.daemon.last_error == "quota_exhausted"
    assert total(r.paths.database) == (0, 0, 0, 0)
    paused = False
    r.clock.advance()
    pump(r)
    # Group 1 — the 2 s harness must honour the persistent-storage retry window.
    assert r.daemon.committed_cursor == before
    r.clock.advance(58)
    pump(r)
    assert r.daemon.committed_cursor == StreamCursor(BOOT, 2, 0)
    assert total(r.paths.database) == (0, 0, 0, 0)
    # Real baseline commit progress reopens the ordinary 2 s cadence immediately.
    write_io(r.proc, 106)
    r.clock.advance()
    pump(r)
    assert r.daemon.committed_cursor == StreamCursor(BOOT, 2, 1)
    assert total(r.paths.database) == (40, 20, 12, 8)
