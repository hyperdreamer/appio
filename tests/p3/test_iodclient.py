"""Read-through daemon client: verified authority, dedupe, gaps, honest absence.

Threat model.  A client that draws daemon data into the GUI must never let any of
the following become *current* data:

* a socket that is well permissioned but answered by a non-root process;
* a delayed frame from an earlier boot, epoch or connection generation;
* a re-delivered snapshot, which would double-count the same interval;
* a transport loss silently rendered as a period of measured zero;
* a missing or silent daemon rendered as an "empty but complete" dataset;
* a gap whose bounds are unknown rendered as a concrete interval.

Every test is rootless and offline.  The transport is a real AF_UNIX
``socketpair`` (no daemon, no listener, no root peer) with the connector and the
``SO_PEERCRED`` reader injected; frames are encoded with the shared
:mod:`appio.ipc_protocol` helpers so the client is exercised through the real
wire format rather than a private one.  A virtual clock drives reconnect and
timeout logic, so no test sleeps.
"""

from __future__ import annotations

import json
import select
import socket
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from appio.contracts import LAUNCH_WINDOW_MESSAGE, Window
from appio.iodclient import (
    BACKOFF_SECONDS,
    CACHE_BYTES,
    MAX_PENDING_REQUESTS,
    MAX_RESPONSE_BYTES,
    NO_ANSWER_NS,
    REQUEST_LIFETIME_NS,
    AppRegistry,
    ClientState,
    CursorTracker,
    GapNotice,
    IodClient,
    IodClientError,
    RemoteError,
    RequestRejected,
)
from appio.ipc_models import Capabilities, QueryRequest, StreamCursor
from appio.ipc_protocol import FrameDecoder, encode_frame
from appio.ipc_security import Peer, PeerRejected

NS = 1_000_000_000
BOOT_A = "11111111-2222-4333-8444-555555555555"
BOOT_B = "99999999-8888-4777-8666-555555555555"
CAPS = {"proc_all": True, "dev_stat": True, "file_io": True, "bpf_loaded": True}


# --------------------------------------------------------------------------- #
# harness: a virtual clock, a real socketpair and a minimal protocol-1 server
# --------------------------------------------------------------------------- #


class Clock:
    """Virtual ``CLOCK_BOOTTIME``; nothing in this file sleeps."""

    def __init__(self) -> None:
        self.ns = 10 * NS

    def __call__(self) -> int:
        return self.ns

    def advance(self, seconds: float) -> None:
        self.ns += int(seconds * NS)


def status_frame(*, answering=True, pending_activation=False, cursor=None, request_id="st-0"):
    """A status envelope exactly as validated by ``validate_server_message``."""

    return {
        "protocol": 1,
        "type": "status",
        "request_id": request_id,
        "enabled": True,
        "answering": answering,
        "pending_activation": pending_activation,
        "ownership_start_mono_ns": None,
        "cursor": None if cursor is None else asdict(cursor),
    }


def snapshot_frame(seq, *, boot_id=BOOT_A, epoch=7, ts=100, caps=None, files=()):
    return {
        "protocol": 1,
        "type": "snapshot",
        "boot_id": boot_id,
        "iod_epoch": epoch,
        "seq": seq,
        "ts_mono_ns": ts,
        "ts_wall": 1_700_000_000.5 + seq,
        "source": "iod",
        "capabilities": dict(CAPS if caps is None else caps),
        "procs": [],
        "devs": [],
        "files": list(files),
        "coverage": {},
    }


def gap_frame(*, previous, current, reason="cursor_not_retained", start=None, end=None):
    return {
        "protocol": 1,
        "type": "gap",
        "boot_id": current.boot_id,
        "iod_epoch": current.iod_epoch,
        "from_mono_ns": start,
        "to_mono_ns": end,
        "previous": None if previous is None else asdict(previous),
        "current": asdict(current),
        "reason": reason,
    }


def result_frame(request_id, *, kind="ranking", snapshot="snap-1", rows=()):
    payload = [{"rows": list(rows), "next_cursor": None, "snapshot": snapshot}]
    return {
        "protocol": 1,
        "type": "result",
        "request_id": request_id,
        "kind": kind,
        "payload": payload,
    }


def coverage_result(request_id, *, value=None, snapshot="snap-1"):
    """A coverage reply whose payload is a plain object, so size is controllable."""

    return {
        "protocol": 1,
        "type": "result",
        "request_id": request_id,
        "kind": "coverage",
        "payload": {"value": {} if value is None else value, "snapshot": snapshot},
    }


def error_frame(request_id, reason, message):
    return {
        "protocol": 1,
        "type": "error",
        "request_id": request_id,
        "reason": reason,
        "message": message,
    }


class Harness:
    """Drive :class:`IodClient` against a scripted protocol-1 peer."""

    def __init__(self, *, clock=None, peer_uid=0, verifier=None, **client_options):
        self.clock = Clock() if clock is None else clock
        self.gaps: list[GapNotice] = []
        self.changes = 0
        self.connects: list[int] = []
        self.verifications: list[Path] = []
        self.failures: list[BaseException] = []
        self.peer_uid = peer_uid
        self.servers: list[socket.socket] = []
        self.decoders: list[FrameDecoder] = []
        self.served: list[list[dict]] = []
        self.last_request_id: str | None = None
        self.socket_path = Path("/run/appio/iod.sock")
        self._verifier = verifier
        self.client = IodClient(
            self.socket_path,
            peer_reader=self._peer,
            path_verifier=self._verify,
            clock_ns=self.clock,
            on_gap=self.gaps.append,
            on_change=self._changed,
            connector=self._connect,
            **client_options,
        )

    # -- injected seams ---------------------------------------------------- #

    def _changed(self) -> None:
        self.changes += 1

    def _verify(self, path) -> None:
        self.verifications.append(Path(path))
        if self._verifier is not None:
            self._verifier(path)

    def _peer(self, sock) -> Peer:
        return Peer(pid=4321, uid=self.peer_uid, gid=self.peer_uid)

    def _connect(self, path):
        self.connects.append(self.clock.ns)
        if self.failures:
            raise self.failures.pop(0)
        client_end, server_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client_end.setblocking(False)
        server_end.settimeout(2.0)
        self.servers.append(server_end)
        self.decoders.append(FrameDecoder())
        self.served.append([])
        return client_end

    # -- scripted server --------------------------------------------------- #

    @property
    def server(self) -> socket.socket:
        return self.servers[-1]

    def requests(self, minimum=1, deadline=0.5) -> list[dict]:
        """Every request frame the client has sent on the current connection."""

        frames: list[dict] = []
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            ready, _, _ = select.select([self.server], [], [], 0.01)
            if not ready:
                if frames:
                    break
                continue
            data = self.server.recv(65536)
            if not data:  # pragma: no cover - only on a closed peer
                break
            frames.extend(self.decoders[-1].feed(data))
        self.served[-1].extend(frames)
        if frames:
            self.last_request_id = frames[-1].get("request_id")
        return frames

    def one_request(self, kind=None) -> dict:
        frames = self.requests()
        assert frames, "the client sent no request"
        if kind is not None:
            assert [frame["type"] for frame in frames] == [kind], frames
            return frames[0]
        return frames[-1]

    def send(self, message) -> None:
        self.server.sendall(encode_frame(message))

    def status(self, *, answering=True, pending=False, cursor=None) -> None:
        """Reply to this connection's own status request, as the daemon does."""

        self.send(
            status_frame(
                answering=answering,
                pending_activation=pending,
                cursor=cursor,
                request_id=self.last_request_id,
            )
        )

    def send_raw(self, data: bytes) -> None:
        self.server.sendall(data)

    def eof(self) -> None:
        self.server.close()

    def handshake(self, *, cursor=None, answering=True, pending=False, seq=0):
        """Complete status and subscribe; returns ``(status_request, subscribe)``."""

        self.client.connect()
        status = self.one_request("status")
        self.status(answering=answering, pending=pending, cursor=cursor)
        self.client.pump()
        subscribe = self.one_request("subscribe")
        return status, subscribe


def handshaken(*, cursor=None, answering=True, pending=False, **client_options) -> Harness:
    """A harness already past status+subscribe, with nothing accepted yet."""

    harness = Harness(**client_options)
    harness.handshake(cursor=cursor, answering=answering, pending=pending)
    return harness


def ranking_request(**overrides) -> QueryRequest:
    window = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=5 * NS)
    values = {"kind": "ranking", "window": window}
    values.update(overrides)
    return QueryRequest(**values)


def coverage_request(*, end_seconds=5, **overrides) -> QueryRequest:
    window = Window(kind="since_boot", boot_id=BOOT_A, end_mono_ns=end_seconds * NS)
    values = {"kind": "coverage", "window": window}
    values.update(overrides)
    return QueryRequest(**values)


def reason_of(future) -> str:
    assert future.done(), "the future must settle without blocking the caller"
    with pytest.raises(IodClientError) as caught:  # noqa: PT011 - reason is asserted below
        future.result()
    return caught.value.reason


# --------------------------------------------------------------------------- #
# the projection's required vector
# --------------------------------------------------------------------------- #


def test_duplicate_old_epoch_and_old_boot_cannot_reenter_current_stream():
    tracker = CursorTracker()
    tracker.accept_status({"protocol": 1, "type": "status", "boot_id": "boot-a",
                           "iod_epoch": 7, "seq": 0, "source": "iod"})
    assert tracker.accept(StreamCursor("boot-a", 7, 1))
    assert not tracker.accept(StreamCursor("boot-a", 7, 1))
    assert not tracker.accept(StreamCursor("boot-a", 6, 999))
    tracker.begin_connection()
    tracker.accept_status({"protocol": 1, "type": "status", "boot_id": "boot-b",
                           "iod_epoch": 1, "seq": 0, "source": "iod"})
    assert tracker.accept(StreamCursor("boot-b", 1, 1))
    assert not tracker.accept(StreamCursor("boot-a", 8, 999))


# --------------------------------------------------------------------------- #
# authority: what may become current, and when
# --------------------------------------------------------------------------- #


def test_new_connection_must_reestablish_authority_before_samples_are_accepted():
    """A reconnect may not reuse trust earned by the previous connection."""

    tracker = CursorTracker()
    tracker.accept_status({"boot_id": "boot-a", "iod_epoch": 7, "seq": 4})
    assert tracker.accept(StreamCursor("boot-a", 7, 5))
    tracker.begin_connection()
    assert tracker.current == StreamCursor("boot-a", 7, 5)  # resume position survives
    assert not tracker.accept(StreamCursor("boot-a", 7, 6))  # ... authority does not
    tracker.accept_status({"boot_id": "boot-a", "iod_epoch": 7, "seq": 5})
    assert tracker.accept(StreamCursor("boot-a", 7, 6))


def test_snapshot_before_status_cannot_bootstrap_authority():
    """The first frame of a connection must be the status the client asked for."""

    harness = Harness()
    harness.client.connect()
    harness.one_request("status")
    harness.send(snapshot_frame(9))
    harness.client.pump()
    assert harness.client.state.cursor is None
    assert harness.client.state.capabilities is None
    assert harness.client.state.verified is False
    harness.status(cursor=None)
    harness.client.pump()
    harness.one_request("subscribe")
    harness.send(snapshot_frame(9))
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 9)


def test_retired_generation_status_cannot_reclaim_authority():
    """An old connection may never switch authority back to its own boot/epoch."""

    tracker = CursorTracker()
    tracker.accept_status({"boot_id": "boot-a", "iod_epoch": 7, "seq": 3})
    tracker.begin_connection()
    tracker.accept_status({"boot_id": "boot-b", "iod_epoch": 1, "seq": 0})
    tracker.accept_status({"boot_id": "boot-a", "iod_epoch": 7, "seq": 99})
    assert tracker.authority == ("boot-b", 1)
    assert not tracker.accept(StreamCursor("boot-a", 7, 100))
    # A retired generation stays retired even across a later connection, where no
    # current authority exists to compare against.
    tracker.begin_connection()
    tracker.accept_status({"boot_id": "boot-a", "iod_epoch": 7, "seq": 99})
    assert tracker.authority is None
    assert not tracker.accept(StreamCursor("boot-a", 7, 100))
    assert not tracker.accept(StreamCursor("boot-a", 8, 100))


def test_status_without_cursor_lets_the_first_snapshot_establish_the_generation():
    """A fresh daemon advertises no committed cursor; the stream still starts."""

    harness = Harness()
    _, subscribe = harness.handshake(cursor=None)
    assert subscribe["since"] is None
    assert harness.client.state.verified is True
    assert harness.client.state.cursor is None
    harness.send(snapshot_frame(0))
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 0)
    assert harness.client.state.capabilities == Capabilities(True, True, True, True)


def test_reboot_accepts_the_replayed_committed_snapshot_exactly_once():
    """A new boot resets epoch/seq; the replayed snapshot is new data, not a duplicate."""

    harness = Harness()
    harness.handshake(cursor=StreamCursor(BOOT_A, 7, 42))
    harness.send(snapshot_frame(42))
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 42)
    # The daemon restarts on a new boot and replays its committed snapshot at seq 0.
    harness.eof()
    harness.client.pump()
    harness.client.connect()
    harness.one_request("status")
    harness.status(cursor=StreamCursor(BOOT_B, 1, 0))
    harness.client.pump()
    subscribe = harness.one_request("subscribe")
    assert subscribe["since"] is None, "a cursor from another boot is not a resume point"
    harness.send(snapshot_frame(0, boot_id=BOOT_B, epoch=1))
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_B, 1, 0)
    changes = harness.changes
    harness.send(snapshot_frame(0, boot_id=BOOT_B, epoch=1))  # duplicate replay
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_B, 1, 0)
    assert harness.client.duplicate_snapshots == 1, "the repeat must be refused, not re-applied"
    assert harness.changes == changes, "a duplicate must not be applied or announced"


def test_same_generation_resumes_from_the_last_matching_cursor():
    harness = Harness()
    harness.handshake(cursor=StreamCursor(BOOT_A, 7, 40))
    harness.send(snapshot_frame(41))
    harness.client.pump()
    harness.eof()
    harness.client.pump()
    harness.client.connect()
    harness.one_request("status")
    harness.status(cursor=StreamCursor(BOOT_A, 7, 41))
    harness.client.pump()
    subscribe = harness.one_request("subscribe")
    assert subscribe["since"] == asdict(StreamCursor(BOOT_A, 7, 41))
    harness.send(snapshot_frame(41))  # the daemon re-delivers the cursor itself
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 41)
    # We already accepted 41 on the previous connection; it must not count twice.
    assert harness.client.duplicate_snapshots == 1
    harness.send(snapshot_frame(42))
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 42)


# --------------------------------------------------------------------------- #
# transport authority: uid 0, path metadata, absence, silence
# --------------------------------------------------------------------------- #


def test_non_root_server_is_refused_before_any_callback_request_or_cache():
    """A correctly permissioned socket does not prove who is listening."""

    harness = Harness(peer_uid=1000)
    harness.client.connect()
    harness.client.pump()
    state = harness.client.state
    assert state.verified is False
    assert state.answering is False
    assert state.last_error == "server_uid_nonzero"
    assert state.capabilities is None and state.cursor is None
    assert harness.gaps == [], "a refused endpoint is not an observed data gap"
    assert harness.servers[-1].recv(1) == b"", "no request may be written to an unverified peer"
    assert harness.client.pending_requests == 0
    assert reason_of(harness.client.query(ranking_request())) == "daemon_unavailable"


def test_path_verification_failure_refuses_before_connecting():
    def refuse(path):
        raise PeerRejected("socket_mode", "the socket mode is not 0660")

    harness = Harness(verifier=refuse)
    harness.client.connect()
    assert harness.verifications == [harness.socket_path]
    assert harness.connects == [], "no connector may run after a metadata refusal"
    assert harness.client.state.last_error == "socket_mode"


def test_absent_daemon_is_unavailable_not_an_empty_complete_dataset():
    harness = Harness()
    harness.failures.append(FileNotFoundError(2, "No such file or directory"))
    harness.client.connect()
    harness.client.pump()
    state = harness.client.state
    assert (state.verified, state.answering) == (False, False)
    assert state.cursor is None and state.capabilities is None
    assert state.pending_activation is None, "unknown activation is not 'not pending'"
    assert state.last_error == "daemon_absent"
    assert harness.gaps == []


def test_silent_daemon_becomes_not_answering_after_five_seconds():
    harness = Harness()
    harness.client.connect()
    harness.one_request("status")
    harness.client.pump()
    assert harness.client.state.verified is False
    harness.clock.advance(4.99)
    harness.client.pump()
    assert harness.client.state.last_error is None, "silence is only a fault after five seconds"
    assert len(harness.connects) == 1
    harness.clock.advance(0.01)
    harness.client.pump()
    state = harness.client.state
    assert state.verified is False and state.answering is False
    assert state.last_error == "daemon_not_answering"
    assert state.capabilities is None
    # The silent connection is dropped and the next attempt is already scheduled.
    assert len(harness.connects) == 1
    harness.client.pump()
    assert len(harness.connects) == 1
    harness.clock.advance(BACKOFF_SECONDS[0])
    harness.client.pump()
    assert len(harness.connects) == 2


def test_eof_reconnect_backoff_is_bounded_and_driven_by_the_clock():
    harness = Harness()
    harness.handshake()
    harness.eof()
    harness.failures.extend(ConnectionRefusedError(111, "Connection refused") for _ in range(8))
    harness.client.pump()  # observes EOF and schedules the first retry
    delays = []
    for index in range(7):
        rung = BACKOFF_SECONDS[min(index, len(BACKOFF_SECONDS) - 1)]
        before = len(harness.connects)
        harness.clock.advance(rung - 0.01)
        harness.client.pump()
        assert len(harness.connects) == before, f"retry {index} fired before its {rung}s delay"
        harness.clock.advance(0.01)
        harness.client.pump()
        assert len(harness.connects) == before + 1, f"retry {index} never fired at {rung}s"
        delays.append(round((harness.connects[-1] - harness.connects[-2]) / NS, 3))
    assert delays == [0.25, 0.5, 1.0, 2.0, 5.0, 5.0, 5.0]


def test_backoff_resets_after_a_verified_status():
    harness = Harness()
    harness.failures.extend(OSError(2, "No such file or directory") for _ in range(3))
    harness.client.connect()
    for _ in range(2):
        harness.clock.advance(BACKOFF_SECONDS[-1])
        harness.client.pump()
    assert len(harness.connects) == 3
    harness.clock.advance(BACKOFF_SECONDS[-1])
    harness.client.pump()
    assert len(harness.connects) == 4
    harness.one_request("status")
    harness.status(cursor=None)
    harness.client.pump()
    harness.eof()
    harness.client.pump()
    harness.clock.advance(BACKOFF_SECONDS[0])  # the ladder restarted at rung 0
    harness.client.pump()
    assert len(harness.connects) == 5


def test_malformed_capabilities_are_never_trusted():
    harness = handshaken()
    broken = snapshot_frame(1)
    broken["capabilities"] = {"proc_all": True, "dev_stat": True, "file_io": "yes", "bpf_loaded": True}
    harness.send(broken)
    harness.client.pump()
    state = harness.client.state
    assert state.capabilities is None
    assert state.verified is False
    assert state.last_error == "invalid_field"


def test_missing_file_capability_is_reported_absent_not_zero_files():
    harness = handshaken()
    harness.send(
        snapshot_frame(
            1,
            caps={"proc_all": True, "dev_stat": True, "file_io": False, "bpf_loaded": False},
            files=(),
        )
    )
    harness.client.pump()
    capabilities = harness.client.state.capabilities
    assert capabilities == Capabilities(proc_all=True, dev_stat=True, file_io=False, bpf_loaded=False)
    assert capabilities.file_io is False, "an unavailable file source is not an empty file table"


# --------------------------------------------------------------------------- #
# gaps: recorded, bounded, never invented
# --------------------------------------------------------------------------- #


def test_stale_since_emits_one_gap_then_recovers_at_the_committed_snapshot():
    harness = Harness()
    harness.handshake(cursor=StreamCursor(BOOT_A, 7, 10))
    harness.send(snapshot_frame(11, ts=900))  # the last position this client truly saw
    harness.client.pump()
    harness.eof()
    harness.client.pump()
    harness.client.connect()
    harness.one_request("status")
    harness.status(cursor=StreamCursor(BOOT_A, 7, 30))
    harness.client.pump()
    subscribe = harness.one_request("subscribe")
    assert subscribe["since"] == asdict(StreamCursor(BOOT_A, 7, 11))
    harness.send(
        gap_frame(
            previous=StreamCursor(BOOT_A, 7, 10),
            current=StreamCursor(BOOT_A, 7, 30),
            reason="cursor_not_retained",
            start=1_000,
            end=2_000,
        )
    )
    harness.send(snapshot_frame(30, ts=2_000))
    harness.client.pump()
    assert harness.gaps == [
        # The EOF is real provenance: the client stopped observing at seq 11.
        GapNotice(boot_id=BOOT_A, iod_epoch=7, seq=11, from_mono_ns=900,
                  to_mono_ns=None, reason="transport_lost"),
        # The daemon's own gap is the accounting-relevant one, with its own bounds.
        GapNotice(boot_id=BOOT_A, iod_epoch=7, seq=30, from_mono_ns=1_000,
                  to_mono_ns=2_000, reason="cursor_not_retained"),
    ]
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 30)
    harness.send(snapshot_frame(30, ts=2_000))  # re-delivery of the same snapshot
    harness.client.pump()
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 30)
    assert harness.client.duplicate_snapshots == 1
    assert len(harness.gaps) == 2, "the duplicate snapshot adds no gap and no cursor"


def test_unknown_gap_bounds_are_not_invented_as_a_concrete_interval():
    harness = Harness()
    harness.handshake(cursor=StreamCursor(BOOT_A, 7, 10))
    harness.send(
        gap_frame(
            previous=StreamCursor(BOOT_A, 7, 10),
            current=StreamCursor(BOOT_A, 7, 12),
            reason="cursor_boot_unknown",
            start=None,
            end=None,
        )
    )
    harness.client.pump()
    assert harness.gaps == [
        GapNotice(boot_id=BOOT_A, iod_epoch=7, seq=12, from_mono_ns=None,
                  to_mono_ns=None, reason="cursor_boot_unknown")
    ]


def test_transport_loss_yields_one_bounded_gap_per_outage():
    """A lost connection is provenance, not a period of measured zero."""

    harness = handshaken()
    harness.send(snapshot_frame(5, ts=5_000))
    harness.client.pump()
    harness.eof()
    harness.client.pump()
    assert harness.gaps == [
        GapNotice(boot_id=BOOT_A, iod_epoch=7, seq=5, from_mono_ns=5_000,
                  to_mono_ns=None, reason="transport_lost")
    ]
    harness.failures.extend(OSError(111, "Connection refused") for _ in range(3))
    for _ in range(3):
        harness.clock.advance(BACKOFF_SECONDS[-1])
        harness.client.pump()
    assert len(harness.gaps) == 1, "one outage is one gap, not one per retry"
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 5)
    assert harness.client.state.verified is False


def test_transport_loss_before_any_snapshot_carries_no_invented_identity():
    harness = handshaken()
    harness.eof()
    harness.client.pump()
    assert harness.gaps == [
        GapNotice(boot_id="", iod_epoch=0, seq=0, from_mono_ns=None,
                  to_mono_ns=None, reason="transport_lost")
    ]
    assert harness.client.state.verified is False
    assert harness.client.state.cursor is None


# --------------------------------------------------------------------------- #
# historical queries: correlation, exact errors, bounded cache
# --------------------------------------------------------------------------- #


def test_query_before_a_verified_status_fails_closed():
    harness = Harness()
    future = harness.client.query(ranking_request())
    assert reason_of(future) == "daemon_unavailable"
    assert harness.connects == [], "no request may be written before a verified peer"


def test_historical_result_uses_the_shared_decoder_and_does_not_advance_the_cursor():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 12))
    harness.send(snapshot_frame(13))
    harness.client.pump()
    future = harness.client.query(ranking_request())
    request = harness.one_request("query")
    harness.send(result_frame(request["request_id"]))
    harness.client.pump()
    value = future.result(timeout=0)
    assert value["kind"] == "ranking"
    assert value["result"] == []
    assert value["snapshot"] == "snap-1"
    assert value["next_cursor"] is None
    assert harness.client.state.cursor == StreamCursor(BOOT_A, 7, 13)


def test_remote_a22_error_is_preserved_exactly_in_the_future():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    future = harness.client.query(ranking_request())
    request = harness.one_request("query")
    harness.send(error_frame(request["request_id"], "launch_attribution", LAUNCH_WINDOW_MESSAGE))
    harness.client.pump()
    with pytest.raises(RemoteError) as caught:
        future.result(timeout=0)
    assert caught.value.reason == "launch_attribution"
    assert str(caught.value) == LAUNCH_WINDOW_MESSAGE
    assert caught.value.message == LAUNCH_WINDOW_MESSAGE


def test_local_a22_rejection_never_reaches_the_wire():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    launch = QueryRequest(kind="ranking", window=Window(kind="launch", boot_id=BOOT_A, launch_id=1))
    future = harness.client.query(launch)
    with pytest.raises(RequestRejected) as caught:
        future.result(timeout=0)
    assert caught.value.reason == "launch_attribution"
    assert str(caught.value) == LAUNCH_WINDOW_MESSAGE
    assert harness.requests(minimum=1, deadline=0.05) == []


def test_out_of_order_result_from_an_old_connection_is_ignored():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    future = harness.client.query(ranking_request())
    stale = harness.one_request("query")["request_id"]
    harness.eof()
    harness.client.pump()
    assert reason_of(future) == "transport_lost"
    harness.client.connect()
    harness.one_request("status")
    harness.status(cursor=StreamCursor(BOOT_A, 7, 1))
    harness.client.pump()
    harness.one_request("subscribe")
    harness.client.pump()
    cache_before = harness.client.cached_responses
    harness.send(result_frame(stale))
    harness.client.pump()
    assert harness.client.cached_responses == cache_before
    assert future.done() and reason_of(future) == "transport_lost"
    assert harness.client.state.verified is True


def test_response_size_cap_fails_the_future_and_skips_the_cache():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1), max_response_bytes=512)
    future = harness.client.query(coverage_request())
    request = harness.one_request("query")
    harness.send(coverage_result(request["request_id"], value={"pad": "x" * 2048}))
    harness.client.pump()
    assert reason_of(future) == "response_too_large"
    assert harness.client.cached_responses == 0


def test_pending_request_cap_and_five_second_lifetime():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    futures = [harness.client.query(ranking_request()) for _ in range(MAX_PENDING_REQUESTS)]
    assert all(not future.done() for future in futures)
    assert harness.client.pending_requests == MAX_PENDING_REQUESTS
    assert reason_of(harness.client.query(ranking_request())) == "too_many_requests"
    assert len(harness.requests(minimum=MAX_PENDING_REQUESTS)) == MAX_PENDING_REQUESTS
    harness.clock.advance(REQUEST_LIFETIME_NS / NS)
    harness.client.pump()
    assert harness.client.pending_requests == 0
    assert all(reason_of(future) == "query_timeout" for future in futures)


def test_cache_is_keyed_by_request_and_committed_generation_and_evicted_whole():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    first = harness.client.query(ranking_request())
    request = harness.one_request("query")
    harness.send(result_frame(request["request_id"]))
    harness.client.pump()
    assert first.result(timeout=0)["kind"] == "ranking"
    second = harness.client.query(ranking_request())
    assert second.result(timeout=0) == first.result(timeout=0)
    assert harness.requests(minimum=1, deadline=0.05) == [], "a cache hit costs no round trip"
    # A different request is a different key.
    other = harness.client.query(ranking_request(metric="read_bytes"))
    other_request = harness.one_request("query")
    assert other_request["query"]["metric"] == "read_bytes"
    harness.send(result_frame(other_request["request_id"]))
    harness.client.pump()
    assert other.result(timeout=0)["kind"] == "ranking"
    # A new committed generation makes what was read under the old one
    # unreachable: the same request must go back to the daemon.
    harness.send(gap_frame(previous=StreamCursor(BOOT_A, 7, 1),
                           current=StreamCursor(BOOT_A, 8, 0),
                           reason="epoch_changed"))
    harness.client.pump()
    harness.client.query(ranking_request())
    again = harness.one_request("query")
    assert again["type"] == "query", "an old generation's cached answer is not served"


def test_cache_budget_evicts_whole_entries_and_never_exceeds_its_bound():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1), cache_bytes=600)
    for index in range(4):
        future = harness.client.query(
            coverage_request(end_seconds=5 + index, coverage_kind="unknown_owner")
        )
        request = harness.one_request("query")
        harness.send(
            coverage_result(
                request["request_id"],
                value={"pad": f"{index}-" + "y" * 200},
            )
        )
        harness.client.pump()
        assert future.result(timeout=0)["result"]["pad"] == f"{index}-" + "y" * 200
    assert harness.client.cached_responses <= 2, "each entry is larger than a third of the budget"
    assert harness.client.cached_bytes <= 600


def test_close_fails_pending_queries_and_stops_reconnecting():
    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    future = harness.client.query(ranking_request())
    harness.client.close()
    assert reason_of(future) == "client_closed"
    assert harness.client.state.verified is False and harness.client.state.answering is False
    assert harness.client.state.last_error is None, "a requested close is not a fault"
    attempts = len(harness.connects)
    harness.clock.advance(60)
    harness.client.pump()
    assert len(harness.connects) == attempts
    assert reason_of(harness.client.query(ranking_request())) == "client_closed"


# --------------------------------------------------------------------------- #
# selectors that do not select
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "selector, reason",
    [
        (coverage_request(search="chromium"), "invalid_selector"),
        (coverage_request(cursor="page-2"), "invalid_selector"),
        (coverage_request(sort="bytes_r"), "invalid_selector"),
        (coverage_request(metric="read_bytes"), "invalid_selector"),
        (coverage_request(limit=10), "invalid_selector"),
        (ranking_request(**{"app_key": "unit:app@fixture.service"}), "invalid_selector"),
        (QueryRequest(kind="series", window=Window(kind="since_boot", boot_id=BOOT_A,
                                                   end_mono_ns=5 * NS), limit=5,
                      search="chromium"), "invalid_selector"),
    ],
)
def test_a_selector_the_kind_does_not_use_is_refused_not_silently_dropped(selector, reason):
    """Dropping an explicit selector would answer a different question than asked."""

    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    future = harness.client.query(selector)
    assert reason_of(future) == reason
    assert harness.requests(minimum=1, deadline=0.05) == [], "nothing was sent"


def test_close_clears_a_previous_transport_fault():
    """Once the owner closes the link, the old fault is no longer the reason."""

    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    harness.eof()
    harness.client.pump()
    assert harness.client.state.last_error == "transport_lost"
    harness.client.close()
    state = harness.client.state
    assert (state.verified, state.answering) == (False, False)
    assert state.last_error is None, "a requested close is not a fault"


# --------------------------------------------------------------------------- #
# a caller that gives up
# --------------------------------------------------------------------------- #


def test_cancelled_query_does_not_break_the_pump_and_still_warms_the_cache():
    """A GUI cancels a query when the operator navigates; that must stay safe."""

    harness = handshaken(cursor=StreamCursor(BOOT_A, 7, 1))
    future = harness.client.query(ranking_request())
    request = harness.one_request("query")
    assert future.cancel() is True
    harness.send(result_frame(request["request_id"]))
    harness.client.pump()  # must not raise InvalidStateError
    assert harness.client.pending_requests == 0
    assert future.cancelled() is True
    assert harness.client.cached_responses == 1, "the answer is still worth keeping"
    again = harness.client.query(ranking_request())
    assert again.result(timeout=0)["kind"] == "ranking"


# --------------------------------------------------------------------------- #
# shared wire machinery, and the daemon that answers nothing
# --------------------------------------------------------------------------- #


def test_shared_framing_authorisation_and_decoding_are_consumed_not_reimplemented():
    """A private copy of framing, credential or result decoding would drift."""

    import appio.iod_history as history
    import appio.iodclient as client
    import appio.ipc_models as models
    import appio.ipc_protocol as protocol
    import appio.ipc_security as security

    assert client.encode_frame is protocol.encode_frame
    assert client.FrameDecoder is protocol.FrameDecoder
    assert client.validate_server_message is models.validate_server_message
    assert client.validate_request is models.validate_request
    assert client.StreamCursor is models.StreamCursor
    assert client.require_root_server is security.require_root_server
    assert client.read_peer is security.read_peer
    assert client.verify_runtime_paths is security.verify_runtime_paths
    assert client.from_wire is history.from_wire
    assert not hasattr(client, "HEADER"), "the length prefix is not re-derived locally"


def test_connected_but_not_answering_daemon_is_reported_honestly():
    """A stalled collector is a distinct state from an absent daemon."""

    harness = Harness()
    _, subscribe = harness.handshake(answering=False, cursor=StreamCursor(BOOT_A, 7, 4))
    assert subscribe["since"] is None
    state = harness.client.state
    assert (state.verified, state.answering) == (True, False)
    assert state.cursor == StreamCursor(BOOT_A, 7, 4)
    # History is still served from the daemon's durable store while live
    # collection is stalled, so the GUI can render the retained past.
    future = harness.client.query(ranking_request())
    request = harness.one_request("query")
    harness.send(result_frame(request["request_id"]))
    harness.client.pump()
    assert future.result(timeout=0)["kind"] == "ranking"
    assert harness.client.state.answering is False


# --------------------------------------------------------------------------- #
# AppKey registry and pinned policy values
# --------------------------------------------------------------------------- #


def test_app_registry_round_trips_stable_positive_ids():
    registry = AppRegistry()
    first = registry.id_for("unit:app-chromium@fixture.service")
    assert first > 0
    assert registry.id_for("unit:app-chromium@fixture.service") == first
    second = registry.id_for("unit:app-firefox@fixture.service")
    assert second > 0 and second != first
    assert registry.key_for(first) == "unit:app-chromium@fixture.service"
    assert registry.key_for(second) == "unit:app-firefox@fixture.service"
    with pytest.raises(KeyError):
        registry.key_for(9999)
    with pytest.raises(ValueError):
        registry.id_for("")


def test_planned_policy_values_are_pinned():
    assert BACKOFF_SECONDS == (0.25, 0.5, 1.0, 2.0, 5.0)
    assert MAX_PENDING_REQUESTS == 64
    assert CACHE_BYTES == 4 * 1024 * 1024
    assert MAX_RESPONSE_BYTES == 256 * 1024
    assert REQUEST_LIFETIME_NS == 5 * NS
    assert NO_ANSWER_NS == 5 * NS
    assert ClientState() == ClientState(
        verified=False,
        answering=False,
        cursor=None,
        capabilities=None,
        pending_activation=None,
        last_error=None,
    )


# --------------------------------------------------------------------------- #
# production connector, exercised against a real AF_UNIX listener (no root)
# --------------------------------------------------------------------------- #


def test_default_connector_reaches_a_real_listener(tmp_path):
    path = tmp_path / "iod.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(2.0)
    clock = Clock()
    gaps: list[GapNotice] = []
    client = IodClient(
        path,
        peer_reader=lambda sock: Peer(pid=1, uid=0, gid=0),
        path_verifier=lambda target: None,
        clock_ns=clock,
        on_gap=gaps.append,
        on_change=lambda: None,
    )
    try:
        client.connect()
        peer, _ = listener.accept()
        peer.settimeout(2.0)
        decoder = FrameDecoder()
        frames: list[dict] = []
        deadline = time.monotonic() + 2.0
        while not frames and time.monotonic() < deadline:
            frames.extend(decoder.feed(peer.recv(65536)))
        assert frames[0]["type"] == "status"
        peer.sendall(
            encode_frame(
                status_frame(
                    cursor=StreamCursor(BOOT_A, 7, 3),
                    request_id=frames[0]["request_id"],
                )
            )
        )
        client.pump()
        frames = []
        deadline = time.monotonic() + 2.0
        while not frames and time.monotonic() < deadline:
            frames.extend(decoder.feed(peer.recv(65536)))
        assert frames[0]["type"] == "subscribe"
        assert frames[0]["since"] is None, "nothing accepted yet is not a resume point"
        peer.sendall(encode_frame(snapshot_frame(4)))
        client.pump()
        assert client.state.verified is True and client.state.answering is True
        assert client.state.cursor == StreamCursor(BOOT_A, 7, 4)
        client.close()
    finally:
        listener.close()
