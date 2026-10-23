"""Rootless lifecycle/dispatch tests. No real manager, polkit or root is used."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest

import appio.setup_service as service
from appio.activation import ActivationResult, STATE_UNKNOWN
from appio.ipc_protocol import FrameDecoder, encode_frame
from appio.ipc_security import Peer
from appio.setup_boundary import CommandResult, Invocation, VERBS
from appio.setup_files import SERVICE_DROPIN, USER_MANAGER_DROPIN
from test_history_export import BOOT, export_env, history, host, tables

ENABLE = ("/usr/bin/systemctl", "enable", "--now", "appio-iod.service")
DISABLE = ("/usr/bin/systemctl", "disable", "--now", "appio-iod.service")
SHOW = ("/usr/bin/systemctl", "show", "appio-iod.service",
        "--property=UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus", "--no-pager")
RESULT_KEYS = {"version", "verb", "uid", "enabled", "answering", "pending_activation", "state", "export_path", "export_cutoff", "error"}


class Runner:
    def __init__(self):
        self.calls = []
        self.enabled = True
        self.active = True
        self.failed = {}
        self.on_disable = lambda: None
        self.result = "success"
        self.exec_status = 0

    def run(self, argv, **kwargs):
        argv = tuple(argv)
        self.calls.append((argv, kwargs))
        if argv in self.failed:
            return self.failed[argv]
        if argv == ENABLE:
            self.enabled = self.active = True
        elif argv == DISABLE:
            self.on_disable()
            self.enabled = self.active = False
        elif argv == SHOW:
            return CommandResult(0, f"UnitFileState={'enabled' if self.enabled else 'disabled'}\nActiveState={'active' if self.active else 'inactive'}\nSubState={'running' if self.active else 'dead'}\nResult={self.result}\nExecMainCode=1\nExecMainStatus={self.exec_status}\n", "")
        elif argv == ("/usr/bin/getent", "group", "appio"):
            return CommandResult(0, "appio:x:987:alice\n", "")
        elif argv[1:3] == ("show", "user@.service"):
            return CommandResult(0, "cpu io memory pids\n", "")
        return CommandResult(0, "", "")


@pytest.fixture
def lifecycle(export_env, monkeypatch):
    env = export_env
    (env.root / "run/systemd/system").mkdir(parents=True)
    (env.root / "sys/fs/cgroup").mkdir(parents=True)
    (env.root / "sys/fs/cgroup/cgroup.controllers").write_text("cpu io memory pids\n")
    for path, payload in ((SERVICE_DROPIN, b"[Service]\nDelegate=cpu io memory pids\nIOAccounting=yes\n"),
                          (USER_MANAGER_DROPIN, b"[Manager]\nDefaultIOAccounting=yes\n")):
        target = host(env, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    runner = Runner()
    env.deps = replace(env.deps, runner=runner)
    env.runner = runner
    monkeypatch.setenv("PKEXEC_UID", str(env.identity.uid))
    monkeypatch.setattr(service, "validate_trusted_runtime", lambda *a, **kw: env.events.append("runtime-trust"))
    monkeypatch.setattr(service, "probe_daemon_status", lambda *, deps, **kw: runner.active)
    monkeypatch.setattr(service, "probe_accounting", lambda *a, **kw: ActivationResult(True, STATE_UNKNOWN, None))
    return env


def invoke(env, verb, capsys):
    code = service.main(["/usr/bin/appio-setup", verb], deps=env.deps)
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert set(document) == RESULT_KEYS
    assert document["version"] == 1 and document["verb"] == verb
    assert document["uid"] == env.identity.uid
    return code, document, captured.err


def test_invalid_invocation_does_not_construct_privileged_adapters(capsys):
    touched = []
    deps = SimpleNamespace(effective_uid=lambda: touched.append("euid"),
        getpwuid=lambda uid: touched.append("lookup"),
        runner=SimpleNamespace(run=lambda *args, **kwargs: touched.append("command")),
        event=lambda name: touched.append(name))
    assert service.main(["/usr/bin/appio-setup", "disable", "--anything"], deps=deps) == 2
    assert touched == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv", [["/usr/bin/appio-setup", word] for word in
    ["start", "stop", "restart", "enable;id", "export-history /root/db", "STATUS", "", "--root"]]
    + [["appio-setup", "enable"], ["/usr/bin/appio-setup"], [], ["/usr/bin/appio-setup", "status", "/tmp"]])
def test_unknown_verb_and_all_extra_arguments_touch_nothing(argv, monkeypatch, capsys):
    monkeypatch.setattr(service, "production_deps", lambda: pytest.fail("constructed privileged adapters"))
    assert service.main(argv) == 2
    assert capsys.readouterr().out == ""


def test_literal_dispatch_table_has_exactly_the_six_design_verbs():
    assert set(service.VERB_HANDLERS) == set(VERBS) == {
        "status", "enable", "disable", "install-delegation", "restart-user-manager", "export-history"}
    assert len(set(service.VERB_HANDLERS.values())) == 6


def test_enable_fixed_command_requires_delegation_and_retains_pending(lifecycle, capsys):
    env = lifecycle
    code, result, _ = invoke(env, "enable", capsys)
    assert code == 0 and result["error"] is None
    assert [argv for argv, _ in env.runner.calls if argv == ENABLE] == [ENABLE]
    assert all(Path(argv[0]).is_absolute() for argv, _ in env.runner.calls)
    assert result["enabled"] is True and result["answering"] is True
    assert result["pending_activation"] is True
    assert result["state"] == "on, needs activation"
    host(env, SERVICE_DROPIN).unlink()
    env.runner.calls.clear()
    code, result, _ = invoke(env, "enable", capsys)
    assert code != 0 and "delegation" in result["error"]
    assert ENABLE not in [argv for argv, _ in env.runner.calls]


def test_enable_exit_zero_does_not_prove_daemon_answering(lifecycle, monkeypatch, capsys):
    env = lifecycle
    monkeypatch.setattr(service, "probe_daemon_status", lambda **kw: False)
    code, result, _ = invoke(env, "enable", capsys)
    assert code != 0 and result["answering"] is False
    assert result["enabled"] is True
    assert result["state"] == "enabled, not answering"
    assert "answering" in result["error"]


@pytest.mark.parametrize("verb,argv", [("enable", ENABLE), ("disable", DISABLE)])
def test_lifecycle_command_preserves_exit_stderr_and_probes_actual_state(lifecycle, history, capsys, verb, argv):
    env = lifecycle
    env.runner.failed[argv] = CommandResult(17, "", "systemctl: rejected exact request\n")
    before = tables(history.db)
    code, result, diagnostic = invoke(env, verb, capsys)
    assert code == 17
    assert "systemctl: rejected exact request\n" in diagnostic
    assert result["error"] is not None and result["enabled"] is True and result["answering"] is True
    assert result["export_path"] is None
    assert tables(history.db) == before


def test_disable_preserves_both_delegation_dropins_group_and_final_flush_history(lifecycle, history, capsys):
    env = lifecycle
    delegation = {path: host(env, path).read_bytes() for path in (SERVICE_DROPIN, USER_MANAGER_DROPIN)}
    env.runner.on_disable = lambda: history.advance(200)
    code, result, _ = invoke(env, "disable", capsys)
    assert code == 0 and result["error"] is None
    assert result["enabled"] is False and result["answering"] is False and result["state"] == "off"
    assert [argv for argv, _ in env.runner.calls if argv == DISABLE] == [DISABLE]
    assert not any(argv[0] == "/usr/bin/usermod" for argv, _ in env.runner.calls)
    assert {path: host(env, path).read_bytes() for path in delegation} == delegation
    assert result["export_cutoff"] == {"boot_id": BOOT, "iod_epoch": 1, "seq": 2, "cutoff_mono_ns": 300}
    import sqlite3
    with sqlite3.connect(host(env, result["export_path"])) as exported:
        assert tables(exported) == tables(history.db)
    assert history.path.exists()


def test_disable_preflight_failure_does_not_stop_or_disable(lifecycle, history, capsys, tmp_path):
    env = lifecycle
    outside = tmp_path / "outside"
    outside.mkdir()
    (env.home / ".local").symlink_to(outside)
    code, result, _ = invoke(env, "disable", capsys)
    assert code != 0 and "destination" in result["error"]
    assert DISABLE not in [argv for argv, _ in env.runner.calls]
    assert result["enabled"] is True and result["answering"] is True
    assert list(outside.iterdir()) == []


def test_export_failure_after_disable_preserves_root_db_and_reports_actual_off(lifecycle, history, capsys):
    env = lifecycle

    def fault(name):
        if name == "export-backup":
            raise OSError("disk full")

    env.deps = replace(env.deps, event=fault)
    before = tables(history.db)
    code, result, _ = invoke(env, "disable", capsys)
    assert code != 0 and result["export_path"] is None and result["error"]
    assert result["enabled"] is False and result["answering"] is False
    assert history.path.exists() and tables(history.db) == before


def test_forced_stop_cannot_be_reported_as_clean_disable(lifecycle, history, capsys):
    env = lifecycle
    env.runner.result = "timeout"
    env.runner.exec_status = 9
    code, result, _ = invoke(env, "disable", capsys)
    assert code != 0 and "shutdown" in result["error"]
    assert result["export_path"] is None and history.path.exists()
    assert result["enabled"] is False


def test_online_export_never_controls_service(lifecycle, history, capsys):
    env = lifecycle
    code, result, _ = invoke(env, "export-history", capsys)
    assert code == 0 and result["export_path"] is not None
    assert all(argv not in (DISABLE, ENABLE) for argv, _ in env.runner.calls)
    assert result["enabled"] is True and result["answering"] is True


def test_status_calls_explicit_accounting_check(lifecycle, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(service, "probe_accounting", lambda identity, **kw: (seen.append(identity.uid) or ActivationResult(True, STATE_UNKNOWN, None)))
    code, result, _ = invoke(lifecycle, "status", capsys)
    assert code == 0 and seen == [lifecycle.identity.uid]
    assert result["pending_activation"] is True


def test_restart_is_one_absolute_consented_command_for_nss_uid(lifecycle, capsys):
    code, result, _ = invoke(lifecycle, "restart-user-manager", capsys)
    restarts = [argv for argv, _ in lifecycle.runner.calls if "restart" in argv]
    assert code == 0 and restarts == [("/usr/bin/systemctl", "restart", f"user@{lifecycle.identity.uid}.service")]
    assert result["pending_activation"] is True


def test_install_dispatches_to_append_only_delegation_installer(lifecycle, capsys):
    code, result, _ = invoke(lifecycle, "install-delegation", capsys)
    assert code == 0
    assert ("/usr/bin/usermod", "-aG", "appio", "alice") in [argv for argv, _ in lifecycle.runner.calls]
    assert result["pending_activation"] is True


@pytest.mark.parametrize("marker,reason", [("run/systemd/container", "container"), (".dockerenv", "container"), ("missing-systemd", "systemd"), ("missing-cgroup", "cgroup")])
def test_unsupported_environment_is_detected_before_mutations(lifecycle, marker, reason, capsys):
    env = lifecycle
    if marker == "missing-systemd":
        (env.root / "run/systemd/system").rmdir()
    elif marker == "missing-cgroup":
        (env.root / "sys/fs/cgroup/cgroup.controllers").unlink()
    else:
        (env.root / marker).touch()
    code, result, _ = invoke(env, "enable", capsys)
    assert code != 0 and reason in result["error"]
    assert not env.runner.calls
    assert not host(env, "/run/appio-setup").exists()


def test_unprivileged_or_missing_polkit_identity_has_no_state_effect(lifecycle, monkeypatch, capsys):
    """pkexec rejects before exec; even a direct unprivileged call cannot mutate."""
    env = lifecycle
    before = {str(path.relative_to(env.root)): path.read_bytes() for path in env.root.rglob("*") if path.is_file()}
    env.deps = replace(env.deps, effective_uid=lambda: env.identity.uid)
    code = service.main(["/usr/bin/appio-setup", "disable"], deps=env.deps)
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code != 0 and "root" in result["error"] and result["uid"] is None
    env.deps = replace(env.deps, effective_uid=lambda: 0)
    monkeypatch.delenv("PKEXEC_UID")
    assert service.main(["/usr/bin/appio-setup", "disable"], deps=env.deps) != 0
    capsys.readouterr()
    after = {str(path.relative_to(env.root)): path.read_bytes() for path in env.root.rglob("*") if path.is_file()}
    assert before == after and env.runner.calls == []
    assert not host(env, "/run/appio-setup").exists()


def test_two_admin_helpers_serialize_complete_export_and_cutoff(lifecycle, history, monkeypatch):
    import appio.setup_files as root_files
    env = lifecycle
    active = 0
    maximum = 0
    gate = threading.Lock()
    barrier = threading.Barrier(2)
    second_lock_attempt = threading.Event()
    lock_attempts = 0
    real_flock = root_files.fcntl.flock
    results = []
    failures = []

    def contended_flock(fd, operation):
        nonlocal lock_attempts
        if operation == root_files.fcntl.LOCK_EX:
            with gate:
                lock_attempts += 1
                if lock_attempts == 2:
                    second_lock_attempt.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(root_files.fcntl, "flock", contended_flock)

    def event(name):
        nonlocal active, maximum
        if name == "export-backup":
            with gate:
                active += 1
                maximum = max(maximum, active)
            assert second_lock_attempt.wait(timeout=2), "second helper did not contend on the common lock"
        if name == "export-complete":
            with gate:
                active -= 1

    deps = replace(env.deps, event=event)

    def run():
        try:
            barrier.wait(timeout=2)
            results.append(service.dispatch(Invocation("export-history", env.identity), deps=deps))
        except BaseException as error:
            failures.append(error)

    workers = [threading.Thread(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert not any(worker.is_alive() for worker in workers)
    assert failures == [] and len(results) == 2
    assert maximum == 1 and active == 0
    assert results[0]["export_path"] != results[1]["export_path"]
    assert results[0]["export_cutoff"] == results[1]["export_cutoff"]


def test_production_protocol_probe_authenticates_peer_before_decoding(export_env, monkeypatch):
    env = export_env
    server, client = socket.socketpair()
    received = []

    class Connected:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            client.close()
        def connect(self, path):
            received.append(("connect", path))
        def __getattr__(self, name):
            return getattr(client, name)

    def answer():
        decoder = FrameDecoder()
        request = []
        while not request:
            request = decoder.feed(server.recv(4096))
        server.sendall(encode_frame({"protocol": 1, "type": "status", "request_id": request[0]["request_id"],
            "enabled": True, "answering": True, "pending_activation": True,
            "ownership_start_mono_ns": 100, "cursor": None}))
        server.close()

    monkeypatch.setattr(service.socket, "socket", lambda *a, **kw: Connected())
    monkeypatch.setattr(service, "_verify_socket_paths", lambda deps: None)
    monkeypatch.setattr(service, "read_peer", lambda sock: (received.append(("peer", None)) or Peer(7, 0, 0)))
    worker = threading.Thread(target=answer)
    worker.start()
    assert service.probe_daemon_status(deps=env.deps, timeout_seconds=0.5) is True
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert received[1][0] == "peer"


def test_protocol_probe_rejects_nonroot_peer_without_send_or_decode(export_env, monkeypatch):
    class Hostile:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connect(self, path): pass
        def settimeout(self, value): pass
        def sendall(self, value): pytest.fail("sent request to nonroot peer")
        def recv(self, value): pytest.fail("decoded nonroot peer")
    monkeypatch.setattr(service.socket, "socket", lambda *a, **kw: Hostile())
    monkeypatch.setattr(service, "_verify_socket_paths", lambda deps: None)
    monkeypatch.setattr(service, "read_peer", lambda sock: Peer(7, 4242, 4242))
    assert service.probe_daemon_status(deps=export_env.deps, timeout_seconds=0.05) is False


def test_untrusted_runtime_refuses_before_lock_or_manager(lifecycle, monkeypatch, capsys):
    from appio.setup_boundary import RuntimeTrustError

    def reject(*args, **kwargs):
        raise RuntimeTrustError(1, "untrusted_runtime:caller_owned")

    monkeypatch.setattr(service, "validate_trusted_runtime", reject)
    code, result, _ = invoke(lifecycle, "disable", capsys)
    assert code != 0 and result["error"] == "untrusted_runtime:caller_owned"
    assert lifecycle.runner.calls == [] and lifecycle.events == []
    assert not host(lifecycle, "/run/appio-setup").exists()


@pytest.mark.parametrize("case", ["wrong-version", "wrong-id", "slow-drip"])
def test_protocol_probe_rejects_invalid_frames_and_bounds_fragment_deadline(export_env, monkeypatch, case):
    server, client = socket.socketpair()
    finished = threading.Event()

    class Connected:
        def __enter__(self): return self
        def __exit__(self, *args): client.close()
        def connect(self, path): pass
        def __getattr__(self, name): return getattr(client, name)

    def answer():
        try:
            server.recv(4096)
            if case == "slow-drip":
                server.sendall(b"\x00")
                finished.wait(timeout=0.5)
            else:
                value = {"protocol": 1, "type": "status", "request_id": "wrong" if case == "wrong-id" else "setup-status",
                    "enabled": True, "answering": True, "pending_activation": True,
                    "ownership_start_mono_ns": 100, "cursor": None}
                payload = encode_frame(value)
                if case == "wrong-version":
                    payload = payload.replace(b'"protocol":1', b'"protocol":2')
                server.sendall(payload)
        finally:
            server.close()

    monkeypatch.setattr(service.socket, "socket", lambda *a, **kw: Connected())
    monkeypatch.setattr(service, "_verify_socket_paths", lambda deps: None)
    monkeypatch.setattr(service, "read_peer", lambda sock: Peer(7, 0, 0))
    worker = threading.Thread(target=answer)
    worker.start()
    start = service.time.monotonic()
    try:
        assert service.probe_daemon_status(deps=export_env.deps, timeout_seconds=0.08) is False
    finally:
        finished.set()
        worker.join(timeout=1)
    assert service.time.monotonic() - start < 0.5
    assert not worker.is_alive()
