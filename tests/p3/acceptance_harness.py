"""Test-only P3 composition; privileges and commands are data, accounting is real.

All Python mutation opens are checked against ``root``. SQLite receives only
constructed paths beneath it (or production's pinned /proc/self/fd aliases).
No host process/cgroup sampling, manager, user bus, pkexec or network is used.
"""
from __future__ import annotations

import builtins
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import dataclass, replace
import errno
from functools import partial
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import sqlite3
import stat
import time
from types import SimpleNamespace
from unittest.mock import patch

from appio.activation import choose_activation, probe_accounting, probe_file_path
from appio.collector_owner import CollectorCoordinator, GuiLease, ServiceFacts
from appio.contracts import Window
from appio.history_export import ExportResult
from appio.history_router import HistoryRouter
from appio.iod import Daemon, DaemonPaths, RuntimeFilesystem
from appio.iod_history import HistoryRpc
from appio.iodclient import AppRegistry, IodClient
from appio.ipc_models import QueryRequest, validate_server_message
from appio.ipc_protocol import FrameDecoder, encode_frame
from appio.ipc_security import Peer
from appio.live import LiveCollector
from appio.query import QueryService
from appio.setup_boundary import CommandResult, Invocation, SetupDeps, SetupIdentity
from appio.setup_client import HELPER_ARGV, SetupClient
from appio.setup_delegation import DAEMON_RELOAD_ARGV, GETENT_GROUP_ARGV, SHOW_DELEGATES_ARGV, load_setup_state
from appio.setup_files import SafeRootFS
from appio.setup_service import DISABLE_ARGV, ENABLE_ARGV, SHOW_ARGV, ServiceError, dispatch
from appio.store import AppIoStore

NS = 1_000_000_000
BOOT = "11111111-2222-4333-8444-555555555555"
APP = "unit:app-chromium@fixture.service"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
STAMP = 1_700_000_000_000_000_000


def canonical_rows(connection):
    """Every schema-v1 table, column and row, in declared primary-key order.

    Return one entry per row so growth is observable, retaining empty tables too.
    The independent comparison includes receipts, checkpoint JSON and coverage.
    """
    result = []
    for (name,) in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name").fetchall():
        columns = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        order = ",".join(f'"{column}"' for column in primary or [row[1] for row in columns])
        rows = connection.execute(f'SELECT * FROM "{name}" ORDER BY {order}').fetchall()
        result.extend((name, tuple(row[1] for row in columns), tuple(row)) for row in rows)
        if not rows:
            result.append((name, tuple(row[1] for row in columns), None))
    return tuple(result)


class Clock:
    ns = 10 * NS

    def __call__(self):
        return self.ns, STAMP + self.ns


class PollSelector(selectors.DefaultSelector):
    def select(self, timeout=None):
        return super().select(0)


class FixtureFilesystem(RuntimeFilesystem):
    effective_uid = 0
    appio_gid = 987

    def __init__(self, harness):
        self.h = harness

    def lstat(self, path):
        return self.h.stat_metadata(os.lstat(path))

    def fstat(self, fd):
        return self.h.metadata(fd)


@dataclass
class Gui:
    client: IodClient
    local: AppIoStore
    history: HistoryRouter
    coordinator: CollectorCoordinator
    setup: SetupClient
    window: object
    gaps: list


class P3Harness:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.proc = self.root / "proc"
        self.cgroup = self.root / "sys/fs/cgroup"
        self.paths = DaemonPaths(self.root / "var/lib/appio/iod.db", self.root / "run/appio/iod.sock", self.proc, self.cgroup)
        self.clock = Clock()
        self.actual_uid = os.getuid()
        self.identity = SetupIdentity(1000, 1000, "alice", Path("/home/alice"))
        self.commands = []
        self.events = []
        self.failure_point = None
        self.current_event = None
        self.mutation_paths = []
        self.owners = {}
        self.fd_paths = {}
        self.enabled = self.running = False
        self.probe_bytes = 16 * 1024 * 1024
        self.probe_fs = "ext4"
        self.helper_exit = None
        self.peer_uid = 0
        self.amount = 100
        self.guis = []
        self.imports = []
        self.gui = None
        self.daemon = None
        self.last_reply = None
        self.stack = ExitStack()
        self.deps = SetupDeps(self.root, SimpleNamespace(run=self._run), lambda: 0,
            lambda uid: SimpleNamespace(pw_uid=uid, pw_gid=1000, pw_name="alice", pw_dir="/home/alice"),
            self.metadata, self.chown, lambda: self.clock.ns, self._event)

    def host(self, fixed):
        return self.root / Path(fixed).relative_to("/")

    def _check_write(self, path, dir_fd=None):
        if isinstance(path, int):
            candidate = self.fd_paths.get(path)
            assert candidate is not None, "untracked writable descriptor"
        else:
            candidate = Path(os.fsdecode(path))
            if not candidate.is_absolute():
                parent = self.fd_paths.get(dir_fd) if dir_fd is not None else Path.cwd()
                assert parent is not None, "untracked mutation directory descriptor"
                candidate = parent / candidate
        candidate = candidate.resolve()
        assert candidate.is_relative_to(self.root), f"mutation outside temporary root: {candidate}"
        self.mutation_paths.append(candidate)
        return candidate

    def _guard_opens(self):
        raw_os_open, raw_open, raw_io_open = os.open, builtins.open, io.open

        def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            candidate = self._check_write(path, dir_fd) if writing else None
            fd = raw_os_open(path, flags, mode, dir_fd=dir_fd)
            if candidate is None:
                candidate = Path(os.fsdecode(path))
                if not candidate.is_absolute():
                    parent = self.fd_paths.get(dir_fd) if dir_fd is not None else Path.cwd()
                    assert parent is not None
                    candidate = parent / candidate
                candidate = candidate.absolute()
            self.fd_paths[fd] = candidate
            return fd

        def guard(original):
            def opened(path, mode="r", *args, **kwargs):
                if any(flag in mode for flag in "wax+"):
                    self._check_write(path)
                return original(path, mode, *args, **kwargs)
            return opened

        self.stack.enter_context(patch.object(os, "open", guarded_os_open))
        self.stack.enter_context(patch.object(builtins, "open", guard(raw_open)))
        self.stack.enter_context(patch.object(io, "open", guard(raw_io_open)))

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o755)
        self._guard_opens()
        try:
            shutil.copytree(FIXTURES / "proc", self.proc, symlinks=True)
            shutil.copytree(FIXTURES / "cgroup/normal-t0", self.cgroup)
            shutil.rmtree(self.proc / "500")
            shutil.copyfile(FIXTURES / "cgroup/proc/diskstats", self.proc / "diskstats")
            for path, mode in ((self.paths.database.parent, 0o711), (self.paths.socket.parent, 0o750),
                               (self.root / "gui-run", 0o700), (self.host(self.identity.home), 0o700)):
                path.mkdir(parents=True, mode=mode, exist_ok=True)
                path.chmod(mode)
            home = self.host(self.identity.home).stat()
            self.owners[home.st_dev, home.st_ino] = (1000, 1000)
            (self.root / "run/systemd/system").mkdir(parents=True)
            self._write_counters()
            # Use the real query executor's clock dependency. Accounting uses
            # virtual boot time; a frozen execution clock keeps host CPU load
            # out of pagination assertions. Deadline expiry has separate tests.
            self.stack.enter_context(patch("appio.iod.HistoryRpc", partial(HistoryRpc, clock_ns=lambda: 0)))
            # Group 3 — 2 s acceptance vectors retain their accounting cutoffs;
            # the production-default cadence is pinned in test_iod_cadence_pins.
            self.daemon = Daemon(self.paths, clock=self.clock, filesystem=FixtureFilesystem(self),
                peer_reader=lambda sock: Peer(7, 1000, 987), group_lookup=lambda uid: frozenset({987}),
                selector=PollSelector(), sample_ns=2 * NS, flush_ns=5 * NS)
            # Drive the synchronous helper status read on this daemon's owner
            # thread. Only kernel credentials/transport scheduling are injected.
            self._status_transport()
            self.stack.enter_context(patch("appio.history_export.time.time_ns", lambda: STAMP))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *exc):
        try:
            for gui in list(self.guis):
                self.close_gui(gui=gui)
            for connection in self.imports:
                connection.close()
            if self.daemon is not None:
                self.daemon.stop()
                self.daemon.selector.close()
        finally:
            self.stack.close()

    def stat_metadata(self, real):
        uid, gid = self.owners.get((real.st_dev, real.st_ino), (0, 987))
        return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=real.st_mode, st_dev=real.st_dev,
            st_ino=real.st_ino, st_nlink=real.st_nlink, st_size=real.st_size)

    def metadata(self, fd):
        return self.stat_metadata(os.fstat(fd))

    def chown(self, fd, uid, gid):
        info = os.fstat(fd)
        self.owners[info.st_dev, info.st_ino] = uid, gid

    def _event(self, point):
        self.events.append(point)
        self.current_event = point
        if point == self.failure_point:
            raise OSError(errno.EIO, f"injected {point}")

    def _status_transport(self):
        real_socket = socket.socket
        harness = self

        class PumpingSocket:
            def __init__(self, *args):
                self.sock = real_socket(*args)

            def __getattr__(self, name):
                return getattr(self.sock, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.sock.close()

            def recv(self, count):
                harness.pump(turns=4)
                return self.sock.recv(count)

        self.stack.enter_context(patch("appio.setup_service.socket", SimpleNamespace(
            socket=PumpingSocket, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM)))
        self.stack.enter_context(patch("appio.setup_service.STATUS_TIMEOUT_SECONDS", 0.05))
        self.stack.enter_context(patch("appio.setup_service.read_peer", lambda sock: Peer(1, self.peer_uid, 987)))

    def _write_counters(self):
        for pid in (410, 411, 412, 413):
            if not (self.proc / str(pid)).exists():
                continue  # a fixture child may have exited between samples
            amount = self.amount
            (self.proc / str(pid) / "io").write_text(
                f"rchar: {amount*10}\nwchar: {amount*5}\nsyscr: 0\nsyscw: 0\n"
                f"read_bytes: {amount*3}\nwrite_bytes: {amount*2}\ncancelled_write_bytes: 0\n")
        step = self.amount - 100
        leaf = next(path for path in self.cgroup.rglob("io.stat") if "chromium" in str(path))
        leaf.write_text(f"259:0 rbytes={1000+13*step} wbytes={200+5*step}\n"
                        f"259:1 rbytes={300+7*step} wbytes={500+9*step}\n")

    def _run(self, argv, *, uid=0, gid=0, env=None, timeout_seconds=5.0):
        argv = tuple(argv)
        self.commands.append(argv)
        if argv == GETENT_GROUP_ARGV:
            return CommandResult(0, "appio:x:987:alice\n", "")
        if argv == SHOW_DELEGATES_ARGV:
            return CommandResult(0, "cpu memory pids\n", "")
        if argv in (DAEMON_RELOAD_ARGV, ("/usr/bin/usermod", "-aG", "appio", "alice")):
            return CommandResult(0, "", "")
        if argv == ENABLE_ARGV:
            self.enabled = True
            self.daemon.start()
            self.running = True
            return CommandResult(0, "", "")
        if argv == DISABLE_ARGV:
            self.daemon.stop()
            self.running = self.enabled = False
            return CommandResult(0, "", "")
        if argv[0] == "/usr/bin/systemctl" and argv[1:3] == ("--user", "show") and "ExecMainStatus" in argv:
            return CommandResult(0, "0\n", "")
        if argv == SHOW_ARGV:
            enabled, active = ("enabled" if self.enabled else "disabled"), ("active" if self.running else "inactive")
            substate = "running" if self.running else "dead"
            return CommandResult(0, f"UnitFileState={enabled}\nActiveState={active}\nSubState={substate}\nResult=success\nExecMainCode=0\nExecMainStatus=0", "")
        unit = f"appio-probe-{self.clock.ns}"
        probe = probe_file_path(1000)
        expected = ("/usr/bin/systemd-run", "--user", f"--unit={unit}", "--property=Type=oneshot",
            "--property=RemainAfterExit=yes", "--no-block", "/usr/bin/dd", "if=/dev/zero", f"of={probe}", "bs=1M", "count=16", "oflag=direct")
        leaf = self.cgroup / f"user.slice/user-1000.slice/user@1000.service/app.slice/{unit}.service"
        if argv == expected:
            assert (uid, gid) == (1000, 1000)
            assert env == {"XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
            leaf.mkdir(parents=True)
            info = self.host(probe).stat()
            dev = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
            (leaf / "io.stat").write_text(f"{dev} rbytes=0 wbytes={self.probe_bytes}\n")
            return CommandResult(0, "", "")
        if argv in (("/usr/bin/systemctl", "--user", "stop", unit), ("/usr/bin/systemctl", "--user", "reset-failed", unit)):
            if leaf.exists():
                shutil.rmtree(leaf)
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command outside temporary root: {argv!r}")

    def invoke(self, verb):
        self.last_reply = dispatch(Invocation(verb, self.identity), deps=self.deps)
        return self.last_reply

    def enable(self):
        for gui in self.guis:
            gui.coordinator.reconcile(ServiceFacts(None, "unknown", False, False, None))
        self.invoke("install-delegation")
        self.choose_reboot()
        self.invoke("enable")
        for gui in self.guis:
            gui.client.connect()
        self.pump()

    def choose_reboot(self):
        return choose_activation("reboot", self.identity, deps=self.deps, fs=SafeRootFS(self.deps))

    def activate(self):
        # Fixture a fresh user-manager environment and a verified block mount.
        for fixed in ("/usr/bin/systemd-run", "/usr/bin/dd", "/usr/bin/systemctl", "/run/user/1000/bus"):
            path = self.host(fixed)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            path.chmod(0o755)
        manager = self.cgroup / "user.slice/user-1000.slice/user@1000.service"
        (manager / "cgroup.controllers").write_text("cpu io memory pids\n")
        info = self.root.stat()
        device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
        (self.proc / "self").mkdir(exist_ok=True)
        (self.proc / "self/mountinfo").write_text(f"36 35 {device} / {self.root} rw - {self.probe_fs} /dev/fixture rw\n")
        result = probe_accounting(self.identity, deps=self.deps, fs=SafeRootFS(self.deps))
        # The subscription's status is a connection observation, not a live
        # activation watch. Reconnect to obtain the newly persisted root verdict.
        for connection in list(self.daemon.clients.values()):
            connection.sock.shutdown(socket.SHUT_RDWR)
        self.pump()
        for gui in self.guis:
            gui.client.connect()
        self.pump()
        return result

    def pump(self, turns=8):
        for _ in range(turns):
            if self.running:
                self.daemon.step()
            for gui in self.guis:
                gui.client.pump()

    def until(self, condition, *, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition():
            assert time.monotonic() < deadline, "acceptance transport/history did not settle"
            self.pump(2)
            time.sleep(0.001)  # yield to the real read-only history worker

    def facts(self, client):
        state = client.state
        return ServiceFacts(self.enabled, "active" if self.running else "unknown" if self.enabled else "inactive",
            state.verified, state.answering, state.pending_activation)

    def open_gui(self):
        from appio.ui.collector_window import CollectorWindow
        local_path = self.root / "gui.db"
        local = AppIoStore(local_path)
        # A daemon-owned GUI starts with a genuinely empty local history DB. Its
        # boot identity must come from the verified daemon status, not from a
        # synthetic local boot row (which would conceal the production bug).
        gaps, holder = [], {}

        def on_gap(notice):
            gaps.append(notice)
            if "history" in holder:
                holder["history"].record_transport_gap(notice)

        def verify(path):
            assert path == self.paths.socket
            if self.running:
                info = path.lstat()
                assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o660

        client = IodClient(self.paths.socket, path_verifier=verify,
            peer_reader=lambda sock: Peer(1, self.peer_uid, 987), clock_ns=lambda: self.clock.ns,
            on_gap=on_gap, on_change=lambda: None)
        history = HistoryRouter(QueryService(local), client, AppRegistry())
        holder["history"] = history
        coordinator = None

        def local_factory():
            offer = coordinator.resume_handover
            return LiveCollector(self.proc, self.cgroup, local_path, self.clock,
                source="gui", baseline_only=offer is None, resume_handover=offer)

        coordinator = CollectorCoordinator(local_factory=local_factory, client=client, history=history,
            lease=GuiLease(self.root / "gui-run/collector.lock"), clock=self.clock)

        def launch(argv):
            assert len(argv) == 3 and argv[:2] == HELPER_ARGV
            future = Future()
            if self.helper_exit is not None:
                future.set_result(CommandResult(self.helper_exit, "", "authentication cancelled\nignored"))
            else:
                try:
                    body = self.invoke(argv[2])
                    future.set_result(CommandResult(0, json.dumps(body), ""))
                except ServiceError as error:
                    future.set_result(CommandResult(error.exit_code, json.dumps(error.result), error.stderr))
            return future

        setup = SetupClient(launch=launch, probe=lambda: self.facts(client), expected_uid=1000)
        window = CollectorWindow(coordinator, history, setup, clock_ns=lambda: self.clock.ns,
            pump=client.pump, refresh_interval_ms=1_000_000)
        window._timer.stop()
        gui = Gui(client, local, history, coordinator, setup, window, gaps)
        self.guis.append(gui)
        self.gui = gui
        client.connect()
        self.pump()
        window.show()
        window.refresh()
        window._poll_timer.stop()
        self.settle(gui)

    def settle(self, gui=None):
        gui = gui or self.gui
        scope = gui.history._window
        if scope is not None and gui.history._local_generation != -1:
            self.until(lambda: gui.history.ready(scope))

    def import_history(self, *, gui=None):
        """Use the helper's real export as schema-v1 ownership evidence.

        Fresh-baseline daemon coverage is deliberately incomplete. Protocol v1
        cannot prove that ownership, whereas the exported collector intervals can.
        """
        gui = gui or self.gui
        self.settle(gui)
        body = self.invoke("export-history")
        connection = sqlite3.connect(self.host(body["export_path"]).as_uri()+"?mode=ro", uri=True)
        self.imports.append(connection)
        gui.history.add_source(QueryService(connection), collector_id=f"export-{len(self.imports)}", owner="iod")

    def close_gui(self, *, gui=None):
        gui = gui or self.gui
        if gui is None:
            return
        gui.window.shutdown()
        future = gui.history._local_future
        if future is not None and not future.cancelled():
            future.result(timeout=5)
        gui.history._executor.shutdown(wait=True, cancel_futures=True)
        gui.window.close()
        gui.local.connection.close()
        self.guis.remove(gui)
        self.gui = self.guis[-1] if self.guis else None
        self.pump()

    def advance(self, seconds: int):
        assert type(seconds) is int and seconds >= 0 and seconds % 2 == 0
        for _ in range(seconds // 2):
            self.amount += 1
            self._write_counters()
            self.clock.ns += 2 * NS
            self.pump()
            for gui in self.guis:
                gui.window.refresh()
                gui.window._poll_timer.stop()
            self.pump()
            for gui in self.guis:
                self.settle(gui)

    def crash(self):
        # Abrupt resource teardown: no final tick, like a process death.
        self.daemon._release_resources()
        self.running = False
        self.pump()

    def restart(self):
        self.daemon.start()
        self.running = True
        for gui in self.guis:
            gui.client.connect()
        self.pump()

    def disable_and_export(self):
        body = self.invoke("disable")
        cutoff = body["export_cutoff"]
        result = ExportResult(self.host(body["export_path"]), cutoff["boot_id"], cutoff["iod_epoch"], cutoff["seq"], cutoff["cutoff_mono_ns"])
        for gui in self.guis:
            self.pump()
            gui.coordinator.reconcile(self.facts(gui.client))
            gui.coordinator.accept_export(result)
            gui.coordinator.reconcile(self.facts(gui.client))
        return result

    def sql(self, sql, params=()):
        with sqlite3.connect(self.paths.database) as connection:
            return connection.execute(sql, params).fetchall()

    def totals(self):
        return self.sql("SELECT rchar,wchar,read_bytes,write_bytes FROM app_boot_total")[0]

    def daemon_rows(self):
        with sqlite3.connect(self.paths.database) as connection:
            return canonical_rows(connection)

    def export_rows(self, path: Path):
        with sqlite3.connect(Path(path).as_uri()+"?mode=ro", uri=True) as connection:
            return canonical_rows(connection)

    def rpc(self, request):
        if not self.guis:
            raise AssertionError("open a GUI before using its verified RPC client")
        self.settle()
        future = self.gui.client.query(request)
        self.until(future.done)
        return future.result()

    def query(self, window: Window, *, gui=None):
        gui = gui or self.gui
        gui.history.prepare(window)
        self.until(lambda: gui.history.ready(window))
        return gui.history.ranking(window, "total_bytes")

    def render(self, window, *, gui=None):
        gui = gui or self.gui
        self.query(window, gui=gui)
        # The shell constructs the unchanged P2 view with a since-boot selector.
        # Make that exact selector ready, then exercise its public set_window.
        since = Window("since_boot", window.boot_id, end_mono_ns=window.end_mono_ns)
        self.query(since, gui=gui)
        gui.window._show_ready(since)
        self.query(window, gui=gui)
        gui.window.view.set_window(window)

    def wire(self, message, *, response_type=None):
        """One actual AF_UNIX exchange, including invalid/adversarial requests."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.paths.socket))
            client.setblocking(False)
            client.sendall(encode_frame(message))
            decoder, replies = FrameDecoder(), []
            def received():
                try:
                    data = client.recv(65536)
                except BlockingIOError:
                    return False
                assert data, "server closed before reply"
                replies.extend(decoder.feed(data))
                return any(response_type is None or reply["type"] == response_type for reply in replies)
            self.until(received)
            for reply in replies:
                validate_server_message(reply)
            return next(reply for reply in replies if response_type is None or reply["type"] == response_type)
