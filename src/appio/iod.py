"""Opt-in root collector: one durable writer, bounded nonblocking clients.

No GUI input controls collection or daemon lifetime. History workers own separate
read-only connections; only the owner loop touches LiveCollector and its store.
Filesystem/clock/credential seams are Python dependencies, never CLI options.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
import errno
import fcntl
import grp
import logging
import os
from pathlib import Path
import pwd
import queue
import selectors
import signal
import socket
import sqlite3
import stat
import threading
import time

from .cgroups import friendly_device_name, parse_diskstats
from .contracts import WindowUnavailableError
from .file_runtime import FileAvailability, FileRuntime
from .bpf_fileio import FileSourceError
from .iod_history import HistoryRpc, to_wire
from .ipc_models import Capabilities, StreamCursor, validate_request, validate_server_message
from .ipc_protocol import HEADER, FrameDecoder, ProtocolError, encode_frame
from .ipc_queue import ClientQueue, SlowClientError
from .ipc_security import PeerRejected, RefusalLog, authorize_client, read_peer, verify_runtime_paths
from .live import LiveCollector, SystemClock
from .quota import QuotaPruner

NS = 1_000_000_000
SAMPLE_NS = 10 * NS
TICK_NS = 2 * NS
MAX_FLUSH_NS = 30 * NS
MAINTENANCE_NS = 60 * NS
_STORAGE_ERRORS = frozenset({"quota_exhausted", "retention_incomplete", "maintenance_failed"})
FRAME_TIMEOUT_NS = 2 * NS
IO_BUDGET = 65536
REQUEST_BUDGET = 8
MAX_CLIENTS = 64
REPLAY_COUNT = 64
REPLAY_BYTES = 4 * 1024 * 1024
_COUNTERS = ("rchar", "wchar", "read_bytes", "write_bytes")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DaemonPaths:
    database: Path = Path("/var/lib/appio/iod.db")
    socket: Path = Path("/run/appio/iod.sock")
    proc_root: Path = Path("/proc")
    cgroup_root: Path = Path("/sys/fs/cgroup")

    def __post_init__(self):
        for name in ("database", "socket", "proc_root", "cgroup_root"):
            object.__setattr__(self, name, Path(getattr(self, name)))
            if not getattr(self, name).is_absolute():
                raise ValueError(f"{name} must be absolute")


class RuntimeFilesystem:
    """Trusted metadata seam. Production never substitutes ownership or paths."""
    lstat = staticmethod(os.lstat)
    fstat = staticmethod(os.fstat)

    @property
    def effective_uid(self):
        return os.geteuid()

    @property
    def appio_gid(self):
        return grp.getgrnam("appio").gr_gid


def _groups(uid):
    account = pwd.getpwuid(uid)
    return frozenset(os.getgrouplist(account.pw_name, account.pw_gid))


def _error(reason, request_id=None, message=None):
    reply = {"protocol": 1, "type": "error", "reason": reason,
             "message": message or "Request could not be completed."}
    if request_id is not None:
        reply["request_id"] = request_id
    return reply


class HistoryWorker:
    """Two daemon threads, 64 waiting jobs, interruptible finite shutdown.

    Opaque continuation tokens stay on their issuing RPC/connection. The routing
    cache is bounded; an evicted token gets an explicit stale_cursor, not a page
    from a different snapshot. Connections are opened/closed by their own thread.
    """
    def __init__(self, database):
        self.database = Path(database)
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._queues = [queue.Queue(), queue.Queue()]
        self._waiting = 0
        self._next = 0
        self._routes = OrderedDict()
        self._connections = {}
        self._threads = [threading.Thread(target=self._run, args=(index,), daemon=True,
                                         name=f"appio-history-{index}") for index in range(2)]
        for thread in self._threads:
            thread.start()

    def submit(self, request, *, request_id):
        with self._lock:
            if self._stopping.is_set() or self._waiting >= 64:
                raise ProtocolError("query_busy", "History worker queue is full.")
            future = Future()
            if request.cursor is not None and request.cursor not in self._routes:
                future.set_result(_error("stale_cursor", request_id))
                return future
            index = self._routes.get(request.cursor, self._next)
            self._next = (self._next + 1) % 2
            self._waiting += 1
            self._queues[index].put((future, request, request_id))
            return future

    def _run(self, index):
        connection = None
        try:
            connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=0.01)
            connection.execute("PRAGMA query_only=ON")
            rpc = HistoryRpc(connection)
            with self._lock:
                self._connections[index] = connection
            while not self._stopping.is_set():
                try:
                    job = self._queues[index].get(timeout=0.05)
                except queue.Empty:
                    continue
                if job is None:
                    break
                future, request, request_id = job
                with self._lock:
                    self._waiting -= 1
                if not future.set_running_or_notify_cancel():
                    continue
                if self._stopping.is_set():
                    future.set_result(_error("history_unavailable", request_id))
                    continue
                try:
                    reply = rpc.execute(request, request_id=request_id)
                    payload = reply.get("payload")
                    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
                        cursor = payload[0].get("next_cursor")
                        if cursor is not None:
                            with self._lock:
                                self._routes[cursor] = index
                                self._routes.move_to_end(cursor)
                                while len(self._routes) > 128:
                                    self._routes.popitem(last=False)
                    future.set_result(reply)
                except Exception:
                    # Worker/provider bugs are isolated and sanitized like storage failures.
                    future.set_result(_error("history_unavailable", request_id))
        except sqlite3.Error:
            self._stopping.set()
        finally:
            with self._lock:
                self._connections.pop(index, None)
            if connection is not None:
                connection.close()
            self._cancel_pending(index)

    def _cancel_pending(self, index):
        while True:
            try:
                job = self._queues[index].get_nowait()
            except queue.Empty:
                return
            if job is not None:
                with self._lock:
                    self._waiting -= 1
                job[0].cancel()

    def shutdown(self, timeout=0.5):
        self._stopping.set()
        with self._lock:
            for connection in self._connections.values():
                connection.interrupt()
        for index in range(2):
            self._cancel_pending(index)
            self._queues[index].put(None)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)


@dataclass
class _Client:
    sock: object
    peer: object
    decoder: FrameDecoder = field(default_factory=FrameDecoder)
    output: ClientQueue = field(default_factory=ClientQueue)
    requests: deque = field(default_factory=deque)
    unfed: bytearray = field(default_factory=bytearray)
    unfed_since: int | None = None
    header: bytearray = field(default_factory=bytearray)
    body_remaining: int = 0
    replay: deque = field(default_factory=deque)
    partial_since: int | None = None
    subscribed: bool = False
    job: Future | None = None
    job_request_id: str | None = None
    requests_processed: int = 0


@dataclass(frozen=True)
class _Snapshot:
    cursor: StreamCursor
    mono_ns: int
    frame: bytes


class Daemon:
    def __init__(self, paths: DaemonPaths = DaemonPaths(), *, clock=None, peer_reader=read_peer,
                 group_lookup=_groups, filesystem=None, selector=None, query_worker=None,
                 sample_ns=SAMPLE_NS, flush_ns=MAX_FLUSH_NS, file_source_factory=None):
        if type(sample_ns) is not int or sample_ns <= 0:
            raise ValueError("sample_ns must be a positive integer")
        if type(flush_ns) is not int or flush_ns <= sample_ns:
            raise ValueError("flush_ns must exceed sample_ns")
        self._sample_ns, self._flush_ns = sample_ns, flush_ns
        self.paths = paths
        self.clock = SystemClock() if clock is None else clock
        self.peer_reader, self.group_lookup = peer_reader, group_lookup
        self.filesystem = RuntimeFilesystem() if filesystem is None else filesystem
        self.selector = selectors.DefaultSelector() if selector is None else selector
        self._injected_worker = query_worker
        self.query_worker = query_worker
        self.clients = {}
        self.refusals = RefusalLog()
        self.collector = None
        self.file_runtime = None
        self._file_source_factory = file_source_factory
        self._file_diagnostic = None
        self.committed_cursor = None
        self.last_error = None
        self._lock_fd = None
        self._listener = None
        self._socket_identity = None
        self._ring = deque()
        self._ring_bytes = 0
        self._current = None
        self._running = False
        self._recover = False
        self._quota_paused = False
        self._storage_error = None
        self._announced_error = None
        self._next_tick = 0
        self._next_sample = 0
        self._next_recovery = 0
        self._next_maintenance = 0
        self._ownership_start = None

    def _metadata(self, path, *, mode, directory=False, group=False):
        found = self.filesystem.lstat(path)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(found.st_mode) or found.st_uid != 0 or
                stat.S_IMODE(found.st_mode) != mode or
                (not directory and found.st_nlink != 1) or
                (group and found.st_gid != self.filesystem.appio_gid)):
            raise PermissionError("untrusted runtime metadata")
        return found

    def _private_file(self, path):
        """No following links, truncation or permission repair of preexisting files."""
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        try:
            found = self._metadata(path, mode=0o600)
            opened = self.filesystem.fstat(fd)
            if (found.st_dev, found.st_ino) != (opened.st_dev, opened.st_ino):
                raise PermissionError("runtime file replaced during open")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _sidecars(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.paths.database) + suffix)
            try:
                self._metadata(path, mode=0o600)
            except FileNotFoundError:
                if not suffix:
                    raise

    def _bind(self):
        path = self.paths.socket
        try:
            old = self.filesystem.lstat(path)
        except FileNotFoundError:
            old = None
        if old is not None:
            verify_runtime_paths(path, metadata=self.filesystem.lstat, appio_gid=self.filesystem.appio_gid)
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.setblocking(False)
                result = probe.connect_ex(str(path))
                if result not in (errno.ECONNREFUSED, errno.ENOENT):
                    raise PermissionError("socket is not stale")
            finally:
                probe.close()
            latest = self.filesystem.lstat(path)
            if (old.st_dev, old.st_ino) != (latest.st_dev, latest.st_ino):
                raise PermissionError("socket replaced during stale check")
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener = listener
        listener.setblocking(False)
        listener.bind(str(path))
        found = self.filesystem.lstat(path)
        self._socket_identity = (found.st_dev, found.st_ino)
        os.chmod(path, 0o660, follow_symlinks=False)
        verify_runtime_paths(path, metadata=self.filesystem.lstat, appio_gid=self.filesystem.appio_gid)
        listener.listen(MAX_CLIENTS)
        self.selector.register(listener, selectors.EVENT_READ, None)

    def _new_collector(self):
        if self.file_runtime is not None:
            self.file_runtime.close()
        if self.collector is not None:
            self.collector.close()
        self.collector = LiveCollector(self.paths.proc_root, self.paths.cgroup_root, self.paths.database,
                                       self.clock, source="iod", baseline_only=True)
        connection = self.collector.store.connection
        connection.execute("PRAGMA busy_timeout=10")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        self._sidecars()
        self._pruner = QuotaPruner(connection, self.paths.database)
        self.file_runtime = FileRuntime(connection, source_factory=self._file_source_factory)
        self.file_runtime.start()

    def start(self):
        if self._running:
            return
        if self.filesystem.effective_uid != 0:
            raise PermissionError("appio-iod requires root")
        # Parent trust is established before any file creation. Never mkdir a
        # potentially redirected state path on behalf of an untrusted caller.
        self._metadata(self.paths.database.parent, mode=0o711, directory=True)
        self._metadata(self.paths.socket.parent, mode=0o750, directory=True, group=True)
        mask = os.umask(0o077)
        try:
            self._lock_fd = self._private_file(self.paths.database.with_name("iod.lock"))
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = self._private_file(self.paths.database)
            os.close(fd)
            self._sidecars()
            self._new_collector()
            self._bind()
            self._ring.clear()
            self._ring_bytes = 0
            self._current = None
            self.committed_cursor = None
            self.last_error = None
            self._recover = False
            self._quota_paused = False
            self._storage_error = None
            self._announced_error = None
            self._next_recovery = 0
            self._sample()
            if self.last_error:
                raise RuntimeError("initial collection failed")
            if self._injected_worker is None:
                self.query_worker = HistoryWorker(self.paths.database)
            now = self.clock()[0]
            self._next_tick = now + TICK_NS
            self._next_sample = self._committed_mono + self._sample_ns
            self._next_maintenance = now + MAINTENANCE_NS
            self._ownership_start = self._committed_mono
            self._running = True
        except BaseException:
            self._release_resources()
            raise
        finally:
            os.umask(mask)

    def _accept_client(self, sock):
        peer = None
        try:
            peer = self.peer_reader(sock)
            if not authorize_client(peer, appio_gid=self.filesystem.appio_gid,
                                    supplementary_groups=self.group_lookup, refusals=self.refusals):
                _LOG.warning("client_refused reason=%s uid=%d", self.refusals.records[-1].reason, peer.uid)
                sock.close()
                return False
            if len(self.clients) >= MAX_CLIENTS:
                self.refusals.record("client_limit", peer)
                _LOG.warning("client_refused reason=client_limit uid=%d", peer.uid)
                sock.close()
                return False
            sock.setblocking(False)
            client = _Client(sock, peer)
            self.selector.register(sock, selectors.EVENT_READ, client)
            self.clients[sock.fileno()] = client
            return True
        except (OSError, PeerRejected, ValueError):
            _LOG.warning("client_refused reason=peer_rejected")
            if peer is not None:
                self.refusals.record("peer_rejected", peer)
            sock.close()
            return False

    def _close_client(self, client, reason=None, *, operation=None, error=None):
        if reason is not None:
            self.refusals.record(reason, client.peer)
            if isinstance(error, OSError):
                # Keep syscall evidence, never peer data or exception text. A
                # peer timeout/reset is not evidence of a decode refusal.
                _LOG.warning(
                    "client_refused reason=%s uid=%d operation=%s errno=%s",
                    reason[:128], client.peer.uid, operation, error.errno,
                )
            else:
                _LOG.warning("client_refused reason=%s uid=%d", reason[:128], client.peer.uid)
        self.clients.pop(client.sock.fileno(), None)
        try:
            self.selector.unregister(client.sock)
        except (KeyError, ValueError):
            pass
        if client.job is not None:
            client.job.cancel()
        client.sock.close()

    def _interest(self, client):
        if client.sock.fileno() in self.clients:
            events = selectors.EVENT_READ
            if client.output.queued_bytes or client.replay:
                events |= selectors.EVENT_WRITE
            self.selector.modify(client.sock, events, client)

    def _enqueue(self, client, message, *, snapshot=None):
        try:
            if not isinstance(message, bytes):
                validate_server_message(message)
            data = message if isinstance(message, bytes) else encode_frame(message)
            client.output.push(data, snapshot=snapshot)
            self._interest(client)
            return True
        except (SlowClientError, ProtocolError) as error:
            self._close_client(client, error.reason)
            return False

    def _status(self, client, request_id):
        return {"protocol": 1, "type": "status", "request_id": request_id,
                "enabled": True, "answering": self.last_error is None,
                "pending_activation": self._pending(client.peer.uid),
                "ownership_start_mono_ns": self._ownership_start,
                "cursor": None if self.committed_cursor is None else asdict(self.committed_cursor)}

    def _pending(self, uid):
        # Protocol 1 requires a bool. Unknown fails closed as pending, never as
        # successful activation. Neither peer payload nor daemon UID selects it.
        path = self.paths.database.with_name("setup-state.json")
        fd = None
        try:
            found = self._metadata(path, mode=0o600)
            if found.st_size > 65536:
                return True
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            opened = self.filesystem.fstat(fd)
            if (found.st_dev, found.st_ino) != (opened.st_dev, opened.st_ino):
                return True
            data = os.read(fd, 65537)
            if len(data) > 65536:
                return True
            # Reuse the same duplicate-key/number/depth bounds as socket JSON.
            value = FrameDecoder().feed(HEADER.pack(len(data)) + data)[0]
            if set(value) != {"version", "pending_by_uid"} or type(value["version"]) is not int or value["version"] != 1:
                return True
            pending = value["pending_by_uid"]
            if not isinstance(pending, dict) or any(
                not key.isascii() or not key.isdecimal() or str(int(key)) != key or
                int(key) > 2**32 - 1 or type(flag) is not bool for key, flag in pending.items()
            ):
                return True
            return pending.get(str(uid), True)
        except (OSError, ValueError, IndexError):
            return True
        finally:
            if fd is not None:
                os.close(fd)

    def _gap(self, previous, reason, request_id=None, start=None):
        current = self.committed_cursor
        reply = {"protocol": 1, "type": "gap", "boot_id": current.boot_id, "iod_epoch": current.iod_epoch,
                 "from_mono_ns": start, "to_mono_ns": self._committed_mono if start is not None and start < self._committed_mono else None,
                 "previous": None if previous is None else asdict(previous), "current": asdict(current), "reason": reason}
        if request_id is not None:
            reply["request_id"] = request_id
        return reply

    def _subscribe(self, client, request):
        client.subscribed = True
        client.replay.clear()
        since = request.since
        if self.last_error is not None:
            self._enqueue(client, self._gap(since, self.last_error, request.request_id))
            self._enqueue(client, self._status(client, request.request_id))
            return
        if since is not None and since == self.committed_cursor and self._current is not None and not request.resync_reason:
            return
        retained = next((item for item in self._ring if item.cursor == since), None)
        if retained is not None and since.boot_id == self.committed_cursor.boot_id and since.iod_epoch == self.committed_cursor.iod_epoch:
            client.replay.extend(item.cursor for item in self._ring if item.cursor.seq > since.seq)
        else:
            if since is not None or request.resync_reason:
                self._enqueue(client, self._gap(since, request.resync_reason or "cursor_not_retained", request.request_id))
            if self._current is not None:
                client.replay.append(self._current.cursor)
            else:
                self._enqueue(client, self._gap(since, "snapshot_too_large", request.request_id))
                self._enqueue(client, self._status(client, request.request_id))
        self._interest(client)

    def _dispatch(self, client, message):
        try:
            request = validate_request(message)
        except WindowUnavailableError as error:
            # validate_request rejects A22 before any database/identity lookup.
            self._enqueue(client, _error(error.reason, self._request_id(message), str(error)))
            return
        except ProtocolError as error:
            self._close_client(client, error.reason)
            return
        if request.type == "status":
            self._enqueue(client, self._status(client, request.request_id))
        elif request.type == "subscribe":
            self._subscribe(client, request)
        elif client.job is not None:
            self._enqueue(client, _error("query_busy", request.request_id))
        else:
            try:
                client.job = self.query_worker.submit(request.query, request_id=request.request_id)
                client.job_request_id = request.request_id
            except ProtocolError as error:
                self._enqueue(client, _error(error.reason, request.request_id))

    @staticmethod
    def _request_id(message):
        value = message.get("request_id")
        if isinstance(value, str) and value and len(value.encode("utf-8", errors="replace")) <= 64 and "\x00" not in value:
            return value
        return None

    def _read(self, client, now):
        try:
            if not client.unfed:
                data = client.sock.recv(IO_BUDGET)
                if not data:
                    self._close_client(client)
                    return
                client.unfed.extend(data)
                client.unfed_since = now
            # Feed one frame at a time so FrameDecoder cannot JSON-decode an
            # entire flood before the eight-request limit takes effect. Header
            # validity/length bounds and all JSON parsing still belong to it.
            while client.unfed and len(client.requests) < REQUEST_BUDGET:
                if not client.header:
                    client.partial_since = client.unfed_since
                if len(client.header) < HEADER.size:
                    count = min(HEADER.size - len(client.header), len(client.unfed))
                    chunk = bytes(client.unfed[:count])
                    del client.unfed[:count]
                    client.header.extend(chunk)
                    client.decoder.feed(chunk)
                    if len(client.header) < HEADER.size:
                        break
                    client.body_remaining = HEADER.unpack(client.header)[0]
                count = min(client.body_remaining, len(client.unfed))
                chunk = bytes(client.unfed[:count])
                del client.unfed[:count]
                client.requests.extend(client.decoder.feed(chunk))
                client.body_remaining -= count
                if not client.body_remaining:
                    client.header.clear()
                    client.partial_since = None
            if not client.unfed:
                client.unfed_since = None
            if client.partial_since is not None and now - client.partial_since >= FRAME_TIMEOUT_NS:
                self._close_client(client, "frame_timeout")
        except BlockingIOError:
            pass
        except (OSError, ProtocolError) as error:
            self._close_client(
                client, getattr(error, "reason", "transport_error"),
                operation="recv", error=error,
            )

    def _send(self, client):
        budget = IO_BUDGET
        try:
            while budget and client.sock.fileno() in self.clients:
                if not client.output.queued_bytes and client.replay:
                    cursor = client.replay.popleft()
                    item = next((item for item in self._ring if item.cursor == cursor), None)
                    if item is None:
                        # Keep only tiny cursors/client, never pin an evicted
                        # 4 MiB ring or bypass ClientQueue's 512 KiB byte cap.
                        client.replay.clear()
                        self._enqueue(client, self._gap(cursor, "cursor_not_retained"))
                        if self._current is not None:
                            client.replay.append(self._current.cursor)
                        else:
                            self._enqueue(client, self._status(client, "resync"))
                        continue
                    client.output.push(item.frame, snapshot=item.cursor)
                view = client.output.peek(budget)
                if not view:
                    break
                sent = client.sock.send(view)
                if sent == 0:
                    raise ConnectionError("peer closed")
                client.output.consume(sent)
                budget -= sent
        except BlockingIOError:
            pass
        except (OSError, SlowClientError) as error:
            self._close_client(
                client, getattr(error, "reason", "transport_error"),
                operation="send", error=error,
            )
        self._interest(client)

    def _snapshot(self, frame, result):
        try:
            names = parse_diskstats((self.paths.proc_root / "diskstats").read_text())
        except (OSError, ValueError):
            names = {}
        procs = []
        for delta in frame.deltas:
            row = {name: getattr(delta, name) for name in
                   ("app_key", "attributed_ts_mono_ns", "observed_mono_ns", "interval_start_mono_ns", "delta_kind", "first_seen_partial", "spans_suspend", "observed_ns")}
            row.update(zip(_COUNTERS, delta.counters, strict=True))
            procs.append(row)
        present = {row["app_key"] for row in procs}
        for coverage in frame.coverage:
            if coverage.app_key not in present:
                procs.append({"app_key": coverage.app_key, **dict.fromkeys(_COUNTERS)})
        devs = [{"app_key": item.app_key, **asdict(item.delta),
                 "name": friendly_device_name(item.delta.dev, names)} for item in frame.device_deltas]
        summary = asdict(result.coverage)
        files = (self.file_runtime.availability if self.file_runtime is not None else
                 FileAvailability(False, False, "file_source_unavailable"))
        summary["file_source"] = asdict(files)
        summary["apps"] = {item.app_key: asdict(item) for item in frame.coverage}
        if self._storage_error is not None:
            summary["storage_error"] = self._storage_error
        return {"protocol": 1, "type": "snapshot", "boot_id": frame.boot_id,
                "iod_epoch": frame.epoch, "seq": frame.seq, "ts_mono_ns": frame.observed_mono_ns,
                "ts_wall": (frame.observed_mono_ns + frame.clock_offset_ns) / NS, "source": "iod",
                "capabilities": asdict(Capabilities(True, result.coverage.device_state == "available", files.available, files.bpf_loaded)),
                "procs": procs, "devs": devs, "files": [], "coverage": to_wire(summary, app_keys={})}

    def _publish(self):
        # LiveCollector.tick returned only after record_tick's outer COMMIT.
        frame = self.collector.last_frame
        previous = self.committed_cursor
        previous_mono = getattr(self, "_committed_mono", None)
        self.committed_cursor = StreamCursor(frame.boot_id, frame.epoch, frame.seq)
        self._committed_mono = frame.observed_mono_ns
        if previous is None or (previous.boot_id, previous.iod_epoch) != (frame.boot_id, frame.epoch):
            self._ownership_start = frame.observed_mono_ns
        if previous is not None and (previous.boot_id, previous.iod_epoch) != (frame.boot_id, frame.epoch):
            self._ring.clear()
            self._ring_bytes = 0
            for client in tuple(self.clients.values()):
                if client.subscribed:
                    client.replay.clear()
                    self._enqueue(client, self._gap(previous, "collector_gap", start=previous_mono if previous.boot_id == frame.boot_id else None))
        try:
            data = encode_frame(self._snapshot(frame, self.collector.last_result))
        except (ProtocolError, UnicodeError):
            self._current = None
            self._ring.clear()
            self._ring_bytes = 0
            for client in tuple(self.clients.values()):
                if client.subscribed:
                    self._enqueue(client, self._gap(previous, "snapshot_too_large"))
                    self._enqueue(client, self._status(client, "resync"))
            return
        item = _Snapshot(self.committed_cursor, frame.observed_mono_ns, data)
        self._current = item
        self._ring.append(item)
        self._ring_bytes += len(data)
        while len(self._ring) > REPLAY_COUNT or self._ring_bytes > REPLAY_BYTES:
            self._ring_bytes -= len(self._ring.popleft().frame)
        for client in tuple(self.clients.values()):
            if client.subscribed:
                self._enqueue(client, data, snapshot=item.cursor)

    def _failure(self, reason):
        self.last_error = reason
        if self.committed_cursor is None or self._announced_error == reason:
            return
        self._announced_error = reason
        for client in tuple(self.clients.values()):
            if client.subscribed:
                client.replay.clear()
                self._enqueue(client, self._gap(self.committed_cursor, reason, start=self._committed_mono))
                self._enqueue(client, self._status(client, "collection"))

    def _sample(self):
        started = self.clock()[0]
        previous_mono = getattr(self, "_committed_mono", None)
        # A recovery window belongs only to a persistent storage failure. Keep
        # it through quiet ticks and successful pruning that commits no frame.
        if self._storage_error in _STORAGE_ERRORS or self._next_recovery:
            self._next_recovery = started + MAINTENANCE_NS
        try:
            if self._recover:
                self._new_collector()
            self.collector.tick()
            self._sidecars()
        except (OSError, sqlite3.Error, ValueError):
            self._failure("collection_failed")
            self._recover = True
            _LOG.warning("collection_failed")
            return
        collection_elapsed = self.clock()[0] - started
        self._recover = False
        self.last_error = self._storage_error
        if self.last_error is None:
            self._announced_error = None
        # F consumes the same committed endpoint, in its own short transaction.
        # It cannot change the T/D error, replay, cadence, or recovery state.
        try:
            self.file_runtime.consume(self.collector.last_frame, self.collector.file_context)
        except FileSourceError:
            if self._file_diagnostic != self.file_runtime.diagnostic:
                _LOG.warning("file_source_unavailable diagnostic=%s", self.file_runtime.diagnostic)
        self._file_diagnostic = self.file_runtime.diagnostic
        self._publish()
        if previous_mono is None or self._committed_mono > previous_mono:
            self._next_recovery = 0
        if collection_elapsed > self._flush_ns:
            self._failure("collection_overdue")
            self._recover = True
            _LOG.warning("collection_overdue")

    def step(self):
        if not self._running:
            raise RuntimeError("daemon is not started")
        now = self.clock()[0]
        if now >= self._next_tick:
            self._next_tick = now + TICK_NS
        # Persistence has its own deadline; a responsiveness tick just before
        # the boundary must not postpone a scheduled observation by another 2 s.
        recovery_due = now >= self._next_recovery
        if now >= self._next_sample and (recovery_due or not (self._quota_paused or self._recover)):
            if self._quota_paused:
                if self._storage_error in _STORAGE_ERRORS:
                    self._next_recovery = now + MAINTENANCE_NS
                try:
                    quota = self._pruner.shrink_if_needed()
                    self._quota_paused = quota.sampling_paused
                    if not self._quota_paused:
                        self._storage_error = None
                except (sqlite3.Error, OSError, ValueError):
                    self._storage_error = "maintenance_failed"
                    self._failure("maintenance_failed")
                    self._recover = True
                    self._next_recovery = now + MAINTENANCE_NS
            if not self._quota_paused:
                if now - self._committed_mono > self._flush_ns:
                    self.last_error = "collection_overdue"
                    self._recover = True
                self._sample()
            # Skip missed boundaries rather than fabricating catch-up frames.
            elapsed = max(0, self.clock()[0] - self._next_sample)
            self._next_sample += (elapsed // self._sample_ns + 1) * self._sample_ns
        if now >= self._next_maintenance:
            try:
                retention = self._pruner.expire(self.collector.boot_id, now)
                quota = self._pruner.shrink_if_needed()
                self._quota_paused = quota.sampling_paused
                self._storage_error = ("quota_exhausted" if quota.sampling_paused else
                                       "retention_incomplete" if not retention.complete else None)
                if self._storage_error:
                    self._failure(self._storage_error)
                if self._quota_paused:
                    self._recover = True
            except (sqlite3.Error, OSError, ValueError):
                self._storage_error = "maintenance_failed"
                self._failure("maintenance_failed")
                self._quota_paused = True
                self._recover = True
            self._next_maintenance = self.clock()[0] + MAINTENANCE_NS
        buffered = any(client.unfed or client.requests or (client.job and client.job.done()) for client in self.clients.values())
        # A past sample boundary in a storage recovery window must not make the
        # selector spin; the retry deadline is the earliest useful wakeup there.
        sample_deadline = (max(self._next_sample, self._next_recovery)
                           if self._quota_paused or self._recover else self._next_sample)
        deadlines = [self._next_tick, sample_deadline, *(client.partial_since + FRAME_TIMEOUT_NS for client in self.clients.values() if client.partial_since is not None)]
        timeout = 0 if buffered else min(0.05, max(0, min(deadlines) - self.clock()[0]) / NS)
        events = self.selector.select(timeout)
        ready = {key.fd: mask for key, mask in events if key.data is not None}
        if any(key.data is None for key, _ in events):
            for _ in range(REQUEST_BUDGET):
                try:
                    sock, _ = self._listener.accept()
                except BlockingIOError:
                    break
                self._accept_client(sock)
        for fd, client in tuple(self.clients.items()):
            now = self.clock()[0]
            if client.partial_since is not None and now - client.partial_since >= FRAME_TIMEOUT_NS:
                self._close_client(client, "frame_timeout")
                continue
            if client.unfed or ready.get(fd, 0) & selectors.EVENT_READ:
                self._read(client, now)
            for _ in range(REQUEST_BUDGET):
                if fd not in self.clients or not client.requests:
                    break
                client.requests_processed += 1
                self._dispatch(client, client.requests.popleft())
            if fd not in self.clients:
                continue
            if client.job is not None and client.job.done():
                try:
                    reply = client.job.result()
                except Exception:
                    reply = _error("history_unavailable", client.job_request_id)
                client.job = None
                self._enqueue(client, reply)
            if fd in self.clients:
                self._send(client)

    def _release_resources(self):
        self._running = False
        for client in tuple(self.clients.values()):
            self._close_client(client)
        if self.query_worker is not None:
            if not self.query_worker.shutdown(timeout=0.5):
                self.last_error = "shutdown_timeout"
            if self._injected_worker is None:
                self.query_worker = None
        if self._listener is not None:
            try:
                self.selector.unregister(self._listener)
            except (KeyError, ValueError):
                pass
            self._listener.close()
            self._listener = None
        if self._socket_identity is not None:
            try:
                found = self.filesystem.lstat(self.paths.socket)
                if stat.S_ISSOCK(found.st_mode) and (found.st_dev, found.st_ino) == self._socket_identity:
                    self.paths.socket.unlink()
            except FileNotFoundError:
                pass
            self._socket_identity = None
        if self.file_runtime is not None:
            self.file_runtime.close()
        if self.collector is not None:
            self.collector.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def stop(self):
        if self._running and self.collector is not None and not self._quota_paused:
            # SIGTERM's handler only sets a flag. Final accounting runs here on
            # the owner thread, never reentrantly inside SQLite or a tick.
            if self.clock()[0] > self._committed_mono and self.clock()[0] >= self._next_recovery:
                if self.clock()[0] - self._committed_mono > self._flush_ns:
                    self._recover = True
                self._sample()
        self._release_resources()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Optional root application I/O collector")
    parser.parse_args(argv)
    if os.geteuid() != 0:
        _LOG.error("appio-iod requires root")
        return 1
    daemon = Daemon()
    stopping = False

    def stop_signal(signum, frame):
        nonlocal stopping
        stopping = True

    previous = {signum: signal.signal(signum, stop_signal) for signum in (signal.SIGTERM, signal.SIGINT)}
    result = 0
    try:
        daemon.start()
        while not stopping:
            daemon.step()
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError):
        _LOG.error("daemon_failed", exc_info=True)
        result = 1
    finally:
        try:
            daemon.stop()
            if daemon.last_error is not None:
                _LOG.error("daemon_shutdown_failed")
                result = 1
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            _LOG.error("daemon_shutdown_failed")
            result = 1
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            daemon.selector.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
