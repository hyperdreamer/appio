"""P3 acceptance under malformed peers, lifecycle failures and durable cutoffs.

Only fixture data is mutated. Retained file rows exercise the P3 read contract;
this suite does not create a Files view or load BPF.
"""
from concurrent.futures import Future
from dataclasses import asdict, replace
import errno
import json
import shutil
import socket
import sqlite3
import stat
import struct
from types import SimpleNamespace

import pytest

from appio.contracts import Window
from appio.ipc_models import QueryRequest
from appio.ipc_protocol import FrameDecoder, MAX_INBOUND, MAX_OUTBOUND, ProtocolError, encode_frame
from appio.ipc_security import Peer
from appio.iod import FRAME_TIMEOUT_NS, IO_BUDGET, REQUEST_BUDGET
from appio.iod_history import HistoryRpc, QueryLimits, from_wire
from appio.iodclient import IodClient
from appio.setup_boundary import CommandResult
from appio.setup_client import HELPER_ARGV, SetupClient
from appio.setup_files import STAGING_ROOT
from appio.setup_service import SHOW_ARGV, ServiceError

from acceptance_harness import APP, BOOT, NS, P3Harness, canonical_rows


def request(kind="status", request_id="r", **fields):
    return {"protocol": 1, "type": kind, "request_id": request_id, **fields}


def receive(peer):
    decoder, frames = FrameDecoder(), []
    while True:
        try:
            chunk = peer.recv(2 * 1024 * 1024)
        except BlockingIOError:
            break
        if not chunk:
            break
        frames.extend(decoder.feed(chunk))
    return frames


class ObservedSocket:
    def __init__(self, sock, *, stalled=False):
        self.sock, self.stalled = sock, stalled
        self.read_bytes = self.sent_bytes = 0

    def __getattr__(self, key):
        return getattr(self.sock, key)

    def recv(self, count):
        data = self.sock.recv(count)
        self.read_bytes += len(data)
        return data

    def send(self, data):
        if self.stalled:
            raise BlockingIOError()
        count = self.sock.send(data)
        self.sent_bytes += count
        return count


def raw_peer(h, *, stalled=False):
    server, peer = socket.socketpair()
    peer.setblocking(False)
    observed = ObservedSocket(server, stalled=stalled)
    assert h.daemon._accept_client(observed)
    return peer, observed, h.daemon.clients[server.fileno()]


@pytest.mark.parametrize("bad", [
    struct.pack("!I", MAX_INBOUND + 1),
    struct.pack("!I", 0),
    struct.pack("!I", 1) + b"\xff",
    struct.pack("!I", 1) + b"[",
])
def test_bad_frames_drop_only_the_peer_and_collection_continues(tmp_path, bad):
    with P3Harness(tmp_path) as h:
        h.enable()
        peer, observed, _ = raw_peer(h)
        with peer:
            peer.sendall(bad)
            h.pump()
            assert observed.fileno() == -1
        h.advance(6)
        assert h.totals() == (120, 60, 36, 24)
        assert h.wire(request(request_id="healthy"), response_type="status")["request_id"] == "healthy"


def test_partial_frame_expires_at_boottime_deadline(tmp_path):
    with P3Harness(tmp_path) as h:
        h.enable()
        peer, observed, _ = raw_peer(h)
        with peer:
            peer.sendall(b"\x00\x00")
            h.pump()
            assert observed.fileno() >= 0
            h.clock.ns += FRAME_TIMEOUT_NS
            h.pump()
            assert observed.fileno() == -1
        assert [r.reason for r in h.daemon.refusals.records] == ["frame_timeout"]
        assert h.wire(request(), response_type="status")["answering"]


def test_flood_has_per_turn_bounds_and_slow_snapshot_peer_cannot_stop_ticks(tmp_path):
    with P3Harness(tmp_path) as h:
        h.enable()
        flood, observed, _ = raw_peer(h)
        slow, stalled, _ = raw_peer(h, stalled=True)
        with flood, slow:
            flood.sendall(encode_frame(request()) * 80)
            h.daemon.step()
            assert observed.read_bytes <= IO_BUDGET
            assert len(receive(flood)) <= REQUEST_BUDGET
            slow.sendall(encode_frame(request("subscribe", since=None)))
            h.pump()
            assert stalled.fileno() >= 0
            h.advance(8)
            assert stalled.fileno() == -1
        assert "slow_client" in [r.reason for r in h.daemon.refusals.records]
        assert h.totals() == (160, 80, 48, 32)
        assert h.wire(request(), response_type="status")["answering"]


def test_replay_continuation_and_evicted_cursor_emit_an_explicit_gap(tmp_path):
    from appio.iod import REPLAY_COUNT

    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(6)
        cursor = asdict(h.daemon.committed_cursor)
        assert cursor == {"boot_id": BOOT, "iod_epoch": 1, "seq": 3}
        h.advance(2)
        resumed = h.wire(request("subscribe", since=cursor), response_type="snapshot")
        assert (resumed["boot_id"], resumed["iod_epoch"], resumed["seq"]) == (BOOT, 1, 4)
        cursor["seq"] = 4
        h.advance(2 * (REPLAY_COUNT + 2))
        gap = h.wire(request("subscribe", since=cursor), response_type="gap")
        assert gap["reason"] == "cursor_not_retained"
        assert gap["previous"] == cursor
        assert gap["current"] == {"boot_id": BOOT, "iod_epoch": 1, "seq": 70}
        assert gap["from_mono_ns"] is None and gap["to_mono_ns"] is None
        assert h.totals() == (2800, 1400, 840, 560)


def test_client_rejects_nonroot_socket_peer_before_trusting_status(tmp_path):
    with P3Harness(tmp_path) as h:
        h.enable()
        client = IodClient(h.paths.socket, path_verifier=lambda _path: None,
                           peer_reader=lambda _sock: Peer(7, 1000, 987),
                           clock_ns=lambda: h.clock.ns, on_gap=lambda _gap: None, on_change=lambda: None)
        try:
            client.connect()
            for _ in range(6):
                h.pump(1)
                client.pump()
            assert not client.state.verified
            assert not client.state.answering
            assert client.state.last_error == "server_uid_nonzero"
        finally:
            client.close()


def seed_large_ranking(h):
    db = h.daemon.collector.store.connection
    for aid in range(100, 601):
        key = f"unit:acceptance-{aid:04}.service"
        db.execute("INSERT INTO app VALUES(?,?,?,NULL,0.0,0.0,'user')", (aid, key, key))
        db.execute("INSERT INTO app_lifetime_total SELECT ?,read_bytes,write_bytes,rchar,wchar,coverage_json,updated_boot_id,updated_mono_ns "
                   "FROM app_lifetime_total WHERE app_id=1", (aid,))
    db.commit()
    return db


@pytest.mark.parametrize("transport", ["client", "wire"])
def test_wire_ranking_continuation_preserves_every_row_in_order(tmp_path, qapp, transport):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(6)
        h.open_gui()
        seed_large_ranking(h)
        rank = QueryRequest("ranking", Window("lifetime"), limit=500)
        first = h.rpc(rank)
        assert first["snapshot"] is not None
        # A smaller exact page is permitted when 500 rows exceed 256 KiB.
        assert 0 < len(first["result"]) <= 500
        assert first["next_cursor"]
        seen = [row.app_key for row in first["result"]]
        page = first
        while page["next_cursor"]:
            if transport == "client":
                page = h.rpc(replace(rank, cursor=page["next_cursor"]))
            else:
                reply = h.wire(request("query", query={"kind": "ranking", "window": {"kind": "lifetime"},
                                                     "limit": 500, "cursor": page["next_cursor"]}))
                assert reply["type"] == "result", reply
                page = {"result": from_wire(reply["payload"], app_ids=lambda _key: 1, result_kind="ranking"),
                        "next_cursor": reply["payload"][0]["next_cursor"]}
            seen.extend(row.app_key for row in page["result"])
            assert len(seen) <= 502
        assert seen == [f"unit:acceptance-{aid:04}.service" for aid in range(100, 601)] + [APP]


def test_wire_limits_and_oversized_text_do_not_disable_totals(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(6)
        h.open_gui()
        db = seed_large_ranking(h)
        for kind in ("ranking", "files"):
            query = {"kind": kind, "window": {"kind": "lifetime"}, "limit": 501}
            if kind == "files":
                query["app_key"] = APP
            message = request("query", query=query)
            peer, observed, state = raw_peer(h)
            with peer:
                peer.sendall(encode_frame(message))
                h.pump()
                assert observed.fileno() == -1
                assert state.job is None
            assert h.daemon.refusals.records[-1].reason == "invalid_field"
        db.execute("UPDATE app SET name=? WHERE app_id=1", ("é" * 140000,))
        db.commit()
        oversized = h.wire(request("query", query={"kind": "ranking", "window": {"kind": "lifetime"}}), response_type="error")
        assert oversized["reason"] == "query_too_large"
        assert len(encode_frame(oversized)) < 512
        total = h.rpc(QueryRequest("totals", Window("lifetime"), APP))["result"]
        assert (total.task_read_bytes, total.task_write_bytes) == (36, 24)


def test_exact_outbound_byte_limit():
    message = {"value": ""}
    empty_size = len(encode_frame(message)) - 4
    message["value"] = "x" * (MAX_OUTBOUND - empty_size)
    assert len(encode_frame(message)) == 4 + 256 * 1024
    message["value"] += "x"
    with pytest.raises(ProtocolError, match="frame_too_large"):
        encode_frame(message)


def test_retained_file_and_series_limits_are_explicit_not_silent_truncation(tmp_path):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(6)
        backup = sqlite3.connect(tmp_path / "retained.db")
        h.daemon.collector.store.connection.backup(backup)
        try:
            aid = backup.execute("SELECT app_id FROM app WHERE key=?", (APP,)).fetchone()[0]
            cov = backup.execute("SELECT coverage_json FROM app_boot_total WHERE app_id=?", (aid,)).fetchone()[0]
            backup.execute("UPDATE boot SET file_finalized_through_mono_ns=?", (3600 * NS,))
            backup.execute("INSERT INTO app_boot_file_coverage VALUES(?,?,?)", (BOOT, aid, cov))
            for ino in range(1, 502):
                backup.execute("INSERT INTO path VALUES(?,'fs','259:0',?,0,?,'resolved',0,?,0,?,1)",
                               (ino, ino, f"fixture-{ino}", BOOT, BOOT))
                backup.execute("INSERT INTO app_boot_file_total VALUES(?,?,?,10,5,2,3,1)", (BOOT, aid, ino))
            backup.commit()
            rpc = HistoryRpc(backup, limits=QueryLimits(timeout_ns=10**10))
            query = QueryRequest("files", Window("lifetime"), APP, limit=500)
            first = rpc.execute(query, request_id="files-500")
            assert first["type"] == "result", first
            assert len(from_wire(first["payload"], app_ids=lambda _key: aid, result_kind="files").rows) == 500
            continuation = first["payload"][0]["next_cursor"]
            second = rpc.execute(replace(query, cursor=continuation), request_id="files-last")
            assert len(from_wire(second["payload"], app_ids=lambda _key: aid, result_kind="files").rows) == 1
            assert second["payload"][0]["next_cursor"] is None
            backup.execute("UPDATE path SET basename='renamed during paging' WHERE path_id=1")
            backup.commit()
            stale = rpc.execute(replace(query, cursor=continuation), request_id="files-stale")
            assert (stale["type"], stale["reason"]) == ("error", "stale_cursor")
            assert "restart" in stale["message"]
            scope = Window("range", BOOT, 10 * NS, 20 * NS)
            series = QueryRequest("series", scope, APP, limit=4000)
            assert rpc.execute(series, request_id="series-4000")["type"] == "result"
            assert rpc.execute(replace(series, limit=4001), request_id="series-4001")["reason"] == "invalid_request"
            for index in range(4001):
                start = 1000 * NS + index * 10 * NS
                backup.execute("INSERT INTO sample VALUES(?,'iod',9,?,?,0,?,?,?, ?,0.0,0,0,1,0,'normal',0,0)",
                               (BOOT, index, aid, start, start + NS, start, NS))
            backup.commit()
            large = replace(series, window=Window("range", BOOT, 1000 * NS, 41010 * NS))
            assert rpc.execute(large, request_id="too-many-points")["reason"] == "query_too_large"
            assert not backup.in_transaction
        finally:
            backup.close()


def test_delegation_parent_directory_fsync_failure_reaches_caller(tmp_path, monkeypatch):
    import os

    with P3Harness(tmp_path) as h:
        original = os.fsync
        visited = []
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                visited.append(fd)
                raise OSError(errno.EIO, "acceptance parent fsync failure")
            return original(fd)
        with monkeypatch.context() as scope:
            scope.setattr(os, "fsync", fail_directory)
            with pytest.raises(ServiceError):
                h.invoke("install-delegation")
        assert visited
        assert not h.enabled and not any("enable" in argv for argv in h.commands)


@pytest.mark.parametrize("point", ["write", "chown", "fsync", "rename", "fsync-dir"])
def test_delegation_publication_failure_does_not_enable_service(tmp_path, point):
    with P3Harness(tmp_path) as h:
        h.failure_point = point
        with pytest.raises(ServiceError):
            h.invoke("install-delegation")
        assert not h.running
        assert not any("enable" in argv for argv in h.commands)
        assert not list(h.root.rglob("*.tmp"))
        h.failure_point = None
        assert h.invoke("install-delegation")["pending_activation"]


@pytest.mark.parametrize("point", [
    "export-backup", "export-copy", "export-chown", "export-fsync", "export-link", "export-fsync-dir", "export-complete",
])
def test_failed_export_is_never_published_or_accepted_as_ownership(tmp_path, point, qapp):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(6)
        h.open_gui()
        h.failure_point = point
        with pytest.raises(ServiceError):
            h.invoke("export-history")
        assert h.gui.coordinator.resume_handover is None
        assert h.gui.coordinator._exports == []
        assert not list(h.host(h.identity.home).rglob("iod-export-*.db"))
        assert not list(h.host(STAGING_ROOT).glob("*.db"))
        h.failure_point = None
        assert h.invoke("export-history")["export_path"]
        assert h.totals() == (120, 60, 36, 24)


def test_export_collision_and_disable_capture_final_wal_state(tmp_path):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(2)
        original = h.invoke("export-history")
        first_path = h.host(original["export_path"])
        saved = first_path.read_bytes()
        db = h.daemon.collector.store.connection
        db.execute("PRAGMA wal_autocheckpoint=0")
        # No scheduled tick yet: graceful stop must commit these final counters.
        h.amount += 1
        h._write_counters()
        h.clock.ns += NS
        assert h.paths.database.with_name("iod.db-wal").stat().st_size > 0
        exported = h.disable_and_export()
        assert (exported.iod_epoch, exported.seq, exported.cutoff_mono_ns) == (1, 2, 13 * NS)
        assert h.export_rows(exported.path) == h.daemon_rows()
        assert h.totals() == (80, 40, 24, 16)
        assert first_path.read_bytes() == saved
        assert exported.path != first_path and exported.path.stem.endswith("-1")
        assert {name for name, _, _ in h.export_rows(exported.path)} >= {"sample_dev", "rollup_app", "clock_map", "sampler_checkpoint"}


def test_failed_stop_cannot_publish_a_successful_disable_export(tmp_path):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(6)
        original = h.deps.runner.run
        def run(argv, **kwargs):
            if tuple(argv) == SHOW_ARGV:
                return CommandResult(0, "UnitFileState=disabled\nActiveState=failed\nSubState=failed\n"
                                     "Result=exit-code\nExecMainCode=1\nExecMainStatus=9\n", "")
            return original(argv, **kwargs)
        h.deps = replace(h.deps, runner=SimpleNamespace(run=run))
        with pytest.raises(ServiceError, match="shutdown_not_clean"):
            h.invoke("disable")
        assert not list(h.host(h.identity.home).rglob("iod-export-*.db"))


@pytest.mark.parametrize("status", [126, 127])
def test_helper_denial_and_absence_preserve_gui_ownership(tmp_path, qapp, qtbot, status):
    with P3Harness(tmp_path) as h:
        h.open_gui()
        gui, calls = h.gui, []
        def launch(argv):
            calls.append(tuple(argv))
            result = Future()
            result.set_result(CommandResult(status, "", "denied or unavailable"))
            return result
        gui.window.setup = SetupClient(launch=launch, probe=lambda: h.facts(gui.client), expected_uid=1000)
        gui.window._begin("enable")
        qtbot.waitUntil(lambda: not gui.window.busy)
        assert calls == [HELPER_ARGV + ("install-delegation",)]
        assert not h.enabled
        assert gui.coordinator._local is not None
        assert gui.window.banner_status.text() == "off"
        assert f"exit {status}" in gui.window.error_label.text()
        assert "denied or unavailable" in gui.window.error_label.text()
        if status == 127:
            assert gui.window._unsupported_reason == "appio-setup is not available"


def test_helper_cancellation_and_window_close_do_not_start_a_daemon(tmp_path, qapp, qtbot):
    with P3Harness(tmp_path) as h:
        h.open_gui()
        gui, pending = h.gui, Future()
        gui.window.setup = SetupClient(launch=lambda _argv: pending, probe=lambda: h.facts(gui.client), expected_uid=1000)
        gui.window._begin("enable")
        assert gui.window.busy
        pending.cancel()
        qtbot.waitUntil(lambda: not gui.window.busy)
        assert gui.window.banner_status.text() == "off"
        assert not h.running
        h.close_gui(gui=gui)
        assert h.commands == []


def test_exact_cutoff_handover_preserves_credit_and_rolls_back_before_commit(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(2)
        shutil.rmtree(h.proc / "411")
        h.advance(4)
        outgoing = h.daemon.collector
        parent = next(process for process in outgoing.sampler_state.processes if process.identity.pid == 410)
        assert [(credit.child.pid, credit.remaining) for credit in parent.credits] == [(411, (990, 495, 297, 198))]
        offer = outgoing.quiesce()
        assert offer.state.cutoff_mono_ns == 16 * NS
        h.open_gui()
        store = h.gui.local
        # A clean handover needs the entire durable predecessor chain, not only
        # externally supplied baselines. Copy real committed evidence into it.
        outgoing.store.connection.backup(store.connection)
        h.crash()
        before = canonical_rows(store.connection)
        memory = store.sampler_state
        store.connection.execute("CREATE TEMP TRIGGER abort_handover BEFORE INSERT ON sampler_checkpoint "
                                 "BEGIN SELECT RAISE(ABORT, 'acceptance injected commit failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="acceptance injected"):
            store.install_handover(offer, wall_ns=h.clock.ns)
        assert canonical_rows(store.connection) == before
        assert store.sampler_state is memory
        assert not store.connection.in_transaction
        store.connection.execute("DROP TRIGGER abort_handover")
        store.install_handover(offer, wall_ns=h.clock.ns)
        assert tuple(store.connection.execute("SELECT from_source,to_source,cutoff_mono_ns FROM handover").fetchone()) == ("iod", "gui", 16 * NS)
        seeded = next(process for process in store.sampler_state.processes if process.identity.pid == 410)
        assert seeded.credits == parent.credits
        assert tuple(store.connection.execute("SELECT rchar,wchar,read_bytes,write_bytes FROM app_boot_total").fetchone()) == (80, 40, 24, 16)
        assert store.connection.execute("SELECT count(*) FROM sample WHERE source='gui'").fetchone() == (0,)
        assert store.connection.execute("SELECT count(*) FROM sample_dev WHERE source='gui'").fetchone() == (0,)
        coverage = json.loads(store.connection.execute(
            "SELECT coverage_json FROM sample_coverage WHERE source='gui' AND seq=0").fetchone()[0])
        assert (coverage["partial_intervals"], coverage["reap_incomplete"], coverage["gap_intervals"],
                coverage["overlap_uncertain_intervals"]) == (1, 1, 0, 0)
        assert all(process.rate_unknown and process.reap_incomplete for process in store.sampler_state.processes)
        assert json.loads(store.connection.execute("SELECT state_json FROM sampler_checkpoint WHERE source='gui'").fetchone()[0])["version"] == 1
