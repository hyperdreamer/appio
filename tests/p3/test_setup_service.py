"""Rootless lifecycle/dispatch tests. No real manager, polkit or root is used."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest

import appio.setup_service as service
import appio.setup_files as setup_files
from appio.activation import ActivationResult, STATE_UNKNOWN
from appio.ipc_protocol import FrameDecoder, encode_frame
from appio.ipc_security import Peer
from appio.setup_boundary import CommandResult, Invocation, InvocationError, VERBS
from appio.setup_files import (
    SAMPLE_PERIOD_COMPLETE,
    SAMPLE_PERIOD_DROPIN,
    SAMPLE_PERIOD_PENDING,
    SERVICE_DROPIN,
    USER_MANAGER_DROPIN,
)
from test_history_export import BOOT, export_env, history, host, tables

ENABLE = ("/usr/bin/systemctl", "enable", "--now", "appio-iod.service")
DISABLE = ("/usr/bin/systemctl", "disable", "--now", "appio-iod.service")
DAEMON_RELOAD = ("/usr/bin/systemctl", "daemon-reload")
TRY_RESTART = ("/usr/bin/systemctl", "try-restart", "appio-iod.service")
SAMPLE_SHOW = ("/usr/bin/systemctl", "show", "appio-iod.service",
               "--property=Environment,EnvironmentFiles,UnsetEnvironment,DropInPaths,UnitFileState,ActiveState", "--no-pager")
RESULT_KEYS = {"version", "verb", "uid", "enabled", "answering", "pending_activation", "state", "export_path", "export_cutoff", "error", "sample_period", "sample_period_state"}


class Runner:
    def __init__(self):
        self.calls = []
        self.enabled = True
        self.active = True
        self.failed = {}
        self.on_disable = lambda: None
        self.result = "success"
        self.exec_status = 0
        self.sample_seconds = 10
        self.environment_files = []
        self.unset_environment = ""
        self.drop_in_paths = "/etc/systemd/system/appio-iod.service.d/20-sample-period.conf"

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
        elif argv == DAEMON_RELOAD:
            pass
        elif argv == TRY_RESTART:
            self.active = True
        elif argv == service.SHOW_ARGV:
            return CommandResult(0, f"UnitFileState={'enabled' if self.enabled else 'disabled'}\nActiveState={'active' if self.active else 'inactive'}\nSubState={'running' if self.active else 'dead'}\nResult={self.result}\nExecMainCode=1\nExecMainStatus={self.exec_status}\n", "")
        elif argv == SAMPLE_SHOW:
            lines = [
                f"Environment=APPIO_SAMPLE_SECONDS={self.sample_seconds}",
                *(f"EnvironmentFiles={entry}" for entry in self.environment_files),
                f"UnsetEnvironment={self.unset_environment}",
                f"DropInPaths={self.drop_in_paths}",
                f"UnitFileState={'enabled' if self.enabled else 'disabled'}",
                f"ActiveState={'active' if self.active else 'inactive'}",
            ]
            return CommandResult(0, "\n".join(lines) + "\n", "")
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


def invoke(env, verb, capsys, period=None):
    argv = ["/usr/bin/appio-setup", verb]
    if period is not None:
        argv.append(str(period))
    code = service.main(argv, deps=env.deps)
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


def test_literal_dispatch_table_has_exactly_seven_verbs():
    assert set(service.VERB_HANDLERS) == set(VERBS) == {
        "status", "enable", "disable", "install-delegation", "restart-user-manager", "export-history", "set-sample-period"}
    assert len(set(service.VERB_HANDLERS.values())) == 7


def test_set_sample_period_active_uses_try_restart(lifecycle, monkeypatch, capsys):
    env = lifecycle
    monkeypatch.setattr(
        service,
        "probe_daemon_status_payload",
        lambda *, deps, timeout_seconds=service.STATUS_TIMEOUT_SECONDS: {"sample_ns": env.runner.sample_seconds * 1_000_000_000},
        raising=False,
    )
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period"] == 10
    assert result["sample_period_state"] == "applied"
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART in [argv for argv, _ in env.runner.calls]
    assert host(env, SAMPLE_PERIOD_DROPIN).read_bytes() == b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n"
    assert host(env, SAMPLE_PERIOD_COMPLETE).exists()
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()


def test_set_sample_period_inactive_writes_and_never_starts(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.active = False
    env.runner.result = "failed"
    monkeypatch.setattr(
        service,
        "probe_daemon_status_payload",
        lambda **kwargs: {"sample_ns": 10 * 1_000_000_000},
        raising=False,
    )
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "applied_not_running"
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]
    assert ENABLE not in [argv for argv, _ in env.runner.calls]


def test_active_identical_successful_apply_is_unchanged(lifecycle, monkeypatch, capsys):
    env = lifecycle
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    monkeypatch.setattr(
        service,
        "probe_daemon_status_payload",
        lambda **kwargs: {"sample_ns": 10 * 1_000_000_000},
        raising=False,
    )
    # The first invocation establishes durable completion evidence.
    assert invoke(env, "set-sample-period", capsys, period=10)[0] == 0
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "unchanged"
    assert DAEMON_RELOAD not in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]


def test_inactive_identical_successful_apply_is_unchanged(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.active = False
    env.runner.result = "failed"
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    _write_period_record(env, SAMPLE_PERIOD_COMPLETE)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "unchanged"
    assert DAEMON_RELOAD not in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]


def test_inactive_canonical_retry_with_only_reload_is_repair(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.active = False
    env.runner.result = "failed"
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    _write_period_record(env, SAMPLE_PERIOD_PENDING)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]
    assert host(env, SAMPLE_PERIOD_COMPLETE).exists()
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()


def test_dispatch_preserves_period_payload(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.sample_seconds = 30
    monkeypatch.setattr(
        service,
        "probe_daemon_status_payload",
        lambda **kwargs: {"sample_ns": 30 * 1_000_000_000},
        raising=False,
    )
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=30)
    assert code == 0, diagnostic
    assert result["sample_period"] == 30


def test_forged_period_invocation_without_payload_is_rejected(lifecycle):
    env = lifecycle
    with pytest.raises(InvocationError) as caught:
        service.dispatch(Invocation("set-sample-period", env.identity), deps=env.deps)
    assert caught.value.reason == "invalid_verb_or_argument_count"


def test_main_preserves_sample_period_out_of_range_reason_and_json(lifecycle, capsys):
    env = lifecycle
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("PKEXEC_UID", str(env.identity.uid))
        monkeypatch.setattr(service, "validate_trusted_runtime", lambda *a, **kw: None)
        code = service.main(
            ["/usr/bin/appio-setup", "set-sample-period", "31"],
            deps=env.deps,
        )
    finally:
        monkeypatch.undo()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == 2
    assert "sample_period_out_of_range" in captured.err
    assert set(result) == RESULT_KEYS
    assert result["uid"] is None
    assert result["sample_period"] is None
    assert result["sample_period_state"] is None
def _write_period_record(env, path, *, operation_id="a" * 32, seconds=10):
    payload = b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n"
    record = setup_files.PeriodJournalRecord(
        operation_id,
        str(SAMPLE_PERIOD_DROPIN),
        seconds,
        hashlib.sha256(payload).hexdigest(),
    )
    target = host(env, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(setup_files.encode_period_journal(record))
    target.chmod(0o600)


def _patch_period_probe(monkeypatch):
    monkeypatch.setattr(
        service,
        "probe_daemon_status_payload",
        lambda **kwargs: {"sample_ns": 10 * 1_000_000_000},
        raising=False,
    )


@pytest.mark.parametrize(
    "foreign",
    [
        b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10",
        b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n\n",
        b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=02\n",
        b"[Unit]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n",
        b"[Service]\nEnvironmentX=APPIO_SAMPLE_SECONDS=10\n",
        b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=99\n",
        b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\nExtra=yes\n",
        b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10 \n",
        b"\xff\n",
    ],
)
def test_foreign_drop_in_variants_are_exit_two_and_preserve_bytes(lifecycle, capsys, foreign):
    env = lifecycle
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(foreign)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 2
    assert result["error"] == "foreign_drop_in"
    assert diagnostic == "foreign_drop_in path=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf\n"
    assert target.read_bytes() == foreign
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()
    assert not [argv for argv, _ in env.runner.calls if argv in (DAEMON_RELOAD, TRY_RESTART)]


def _replace_sample_show(env, monkeypatch, output):
    original = env.runner.run

    def run(argv, **kwargs):
        if tuple(argv) == SAMPLE_SHOW:
            return CommandResult(0, output, "")
        return original(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", run)


@pytest.mark.parametrize(
    "output",
    [
        "Environment=APPIO_SAMPLE_SECONDS=10\nEnvironment=APPIO_SAMPLE_SECONDS=10\nDropInPaths=/etc/x\nUnitFileState=enabled\nActiveState=active\n",
        "Environment=APPIO_SAMPLE_SECONDS=10\nUnknown=value\nDropInPaths=/etc/x\nUnitFileState=enabled\nActiveState=active\n",
        "Environment=APPIO_SAMPLE_SECONDS=10\n\nDropInPaths=/etc/x\nUnitFileState=enabled\nActiveState=active\n",
        "Environment=APPIO_SAMPLE_SECONDS=10\nDropInPaths=/etc/x\nUnitFileState=enabled\n",
    ],
)
def test_sample_show_malformed_fields_fail_closed(lifecycle, monkeypatch, capsys, output):
    env = lifecycle
    _replace_sample_show(env, monkeypatch, output)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_show_invalid"
    assert not host(env, SAMPLE_PERIOD_DROPIN).exists()
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()
    assert DAEMON_RELOAD not in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]


def test_malformed_initial_show_does_not_create_missing_preflight_directories(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _replace_sample_show(
        env,
        monkeypatch,
        "Environment=APPIO_SAMPLE_SECONDS=10\nDropInPaths=/etc/x\nUnitFileState=enabled\n",
    )
    monkeypatch.setattr(
        service.SafeRootFS,
        "prepare_period_directories",
        lambda *args, **kwargs: pytest.fail("period directories were prepared after invalid initial show"),
    )
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_show_invalid"


@pytest.mark.parametrize("active,expected_code", [(True, 0), (False, 1)])
def test_sample_show_environment_files_multiple_entries_active_and_inactive(
    lifecycle, monkeypatch, capsys, active, expected_code,
):
    env = lifecycle
    env.runner.active = active
    env.runner.result = "success"
    env.runner.environment_files = ["/etc/appio-a.conf", "/etc/appio-b.conf", "/etc/appio-a.conf"]
    _patch_period_probe(monkeypatch)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == expected_code, diagnostic
    if active:
        assert result["sample_period_state"] == "applied"
    else:
        assert result["error"] == "sample_period_unverifiable"
        assert "EnvironmentFiles=/etc/appio-a.conf | /etc/appio-b.conf | /etc/appio-a.conf" in diagnostic


def test_sample_show_omitted_empty_environment_files_is_empty(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.environment_files = []
    _patch_period_probe(monkeypatch)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "applied"


def test_sample_show_omitted_empty_unset_environment_is_empty(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _replace_sample_show(
        env,
        monkeypatch,
        "Environment=APPIO_SAMPLE_SECONDS=10\nDropInPaths=/etc/x\nUnitFileState=enabled\nActiveState=active\n",
    )
    _patch_period_probe(monkeypatch)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "applied"


def test_active_override_reports_value_and_dropin_paths(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.sample_seconds = 11
    _patch_period_probe(monkeypatch)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_overridden"
    assert diagnostic == "sample_period_not_in_effect observed=11 DropInPaths=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf\n"


def test_inactive_environment_override_is_unverifiable_with_value_and_dropin_paths(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.active = False
    env.runner.sample_seconds = 11
    env.runner.environment_files = []
    env.runner.unset_environment = ""
    env.runner.drop_in_paths = (
        "/etc/systemd/system/appio-iod.service.d/20-sample-period.conf "
        "/etc/systemd/system/appio-iod.service.d/30-conflict.conf"
    )
    _patch_period_probe(monkeypatch)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_unverifiable"
    assert diagnostic == (
        "sample_period_unverifiable observed=11 EnvironmentFiles=(none) "
        "UnsetEnvironment=(none) DropInPaths=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf "
        "/etc/systemd/system/appio-iod.service.d/30-conflict.conf\n"
    )


def test_inactive_environment_sources_are_unverifiable(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.active = False
    env.runner.environment_files = ["/etc/one.conf", "/etc/two.conf"]
    _patch_period_probe(monkeypatch)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_unverifiable"
    assert diagnostic == (
        "sample_period_unverifiable observed=10 EnvironmentFiles=/etc/one.conf | /etc/two.conf "
        "UnsetEnvironment=(none) DropInPaths=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf\n"
    )
def test_period_write_failure_leaves_pending_and_skips_manager_commands(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    original = service.SafeRootFS.atomic_write

    def fail_drop_in(fs, path, payload, **kwargs):
        if Path(path) == SAMPLE_PERIOD_DROPIN:
            raise OSError("drop-in write failed")
        return original(fs, path, payload, **kwargs)

    monkeypatch.setattr(service.SafeRootFS, "atomic_write", fail_drop_in)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_write_failed"
    assert host(env, SAMPLE_PERIOD_PENDING).exists()
    assert not host(env, SAMPLE_PERIOD_DROPIN).exists()
    assert DAEMON_RELOAD not in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]


def test_reload_failure_keeps_pending_and_does_not_restart(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    env.runner.failed[DAEMON_RELOAD] = CommandResult(17, "", "reload failed\n")
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "daemon_reload_failed"
    assert host(env, SAMPLE_PERIOD_PENDING).exists()
    assert not host(env, SAMPLE_PERIOD_COMPLETE).exists()
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]


def test_restart_failure_keeps_pending_when_service_stays_active(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    env.runner.failed[TRY_RESTART] = CommandResult(17, "", "restart failed\n")
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "service_restart_failed"
    assert result["sample_period_state"] is None
    assert host(env, SAMPLE_PERIOD_PENDING).exists()
    assert not host(env, SAMPLE_PERIOD_COMPLETE).exists()


def test_restart_failure_that_leaves_service_inactive_reports_not_running(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    original = env.runner.run

    def fail_restart(argv, **kwargs):
        if tuple(argv) == TRY_RESTART:
            env.runner.active = False
            return CommandResult(17, "", "restart failed\n")
        return original(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", fail_restart)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "service_not_running"
    assert host(env, SAMPLE_PERIOD_PENDING).exists()
    assert not host(env, SAMPLE_PERIOD_COMPLETE).exists()


def test_malformed_journal_is_reconciled_by_replaying_transaction(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    pending = host(env, SAMPLE_PERIOD_PENDING)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_bytes(b"not-json\n")
    pending.chmod(0o600)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0
    assert result["sample_period_state"] == "applied"
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()
    assert host(env, SAMPLE_PERIOD_COMPLETE).exists()
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART in [argv for argv, _ in env.runner.calls]


def test_completed_record_failure_leaves_pending_for_retry(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)

    def fail_complete(fs, record):
        raise OSError("complete commit failed")

    monkeypatch.setattr(service, "publish_period_complete", fail_complete)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_state_commit_failed"
    assert host(env, SAMPLE_PERIOD_PENDING).exists()
    assert not host(env, SAMPLE_PERIOD_COMPLETE).exists()


def test_matching_pending_and_complete_pair_is_repair_without_replaying_manager(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    _write_period_record(env, SAMPLE_PERIOD_PENDING)
    _write_period_record(env, SAMPLE_PERIOD_COMPLETE)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert DAEMON_RELOAD not in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()
    assert host(env, SAMPLE_PERIOD_COMPLETE).exists()


def test_recovery_cleanup_failure_keeps_verified_success(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    _write_period_record(env, SAMPLE_PERIOD_PENDING)
    _write_period_record(env, SAMPLE_PERIOD_COMPLETE)
    monkeypatch.setattr(
        service.SafeRootFS,
        "remove_period_pending",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("pending cleanup failed")),
    )
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert host(env, SAMPLE_PERIOD_PENDING).exists()
    assert host(env, SAMPLE_PERIOD_COMPLETE).exists()


def test_identical_apply_recovers_after_target_directory_sync_failure(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    original = service.SafeRootFS.sync_period_parent
    calls = 0

    def fail_once(fs, path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("target parent sync failed")
        return original(fs, path)

    monkeypatch.setattr(service.SafeRootFS, "sync_period_parent", fail_once)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_state_write_failed"
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART in [argv for argv, _ in env.runner.calls]


def test_identical_apply_recovers_after_reload_failure(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    env.runner.failed[DAEMON_RELOAD] = CommandResult(17, "", "reload failed\n")
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "daemon_reload_failed"
    env.runner.failed.clear()
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART in [argv for argv, _ in env.runner.calls]


def test_identical_apply_recovers_after_restart_failure(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    env.runner.failed[TRY_RESTART] = CommandResult(17, "", "restart failed\n")
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "service_restart_failed"
    env.runner.failed.clear()
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART in [argv for argv, _ in env.runner.calls]


def test_identical_apply_recovers_after_completed_record_directory_sync_failure(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    original = service.publish_period_complete

    def publish_then_fail(fs, record):
        original(fs, record)
        raise OSError("completion directory sync failed")

    monkeypatch.setattr(service, "publish_period_complete", publish_then_fail)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_state_commit_failed"
    assert host(env, SAMPLE_PERIOD_PENDING).exists()
    assert host(env, SAMPLE_PERIOD_COMPLETE).exists()
    monkeypatch.setattr(service, "publish_period_complete", original)
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert DAEMON_RELOAD not in [argv for argv, _ in env.runner.calls]
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]


def test_apply_after_ancestor_directory_sync_failure_reports_applied(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    original = service.SafeRootFS.prepare_period_directories
    calls = 0

    def prepare_then_fail(fs):
        nonlocal calls
        calls += 1
        original(fs)
        if calls == 1:
            raise OSError("ancestor sync failed")

    monkeypatch.setattr(service.SafeRootFS, "prepare_period_directories", prepare_then_fail)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_state_write_failed"
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "applied"
    assert host(env, SAMPLE_PERIOD_DROPIN).read_bytes() == b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n"


def test_identical_apply_recovers_after_ancestor_directory_sync_failure(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n")
    original_prepare = service.SafeRootFS.prepare_period_directories
    prepare_calls = 0
    writes = []

    def prepare_then_fail(fs):
        nonlocal prepare_calls
        prepare_calls += 1
        original_prepare(fs)
        if prepare_calls == 1:
            raise OSError("ancestor sync failed")

    original_write = service.SafeRootFS.atomic_write

    def record_write(fs, path, payload, **kwargs):
        if Path(path) == SAMPLE_PERIOD_DROPIN:
            writes.append(payload)
        return original_write(fs, path, payload, **kwargs)

    monkeypatch.setattr(service.SafeRootFS, "prepare_period_directories", prepare_then_fail)
    monkeypatch.setattr(service.SafeRootFS, "atomic_write", record_write)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_state_write_failed"
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert writes == []


def test_failed_restart_inactive_retry_repairs_without_start(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    original = env.runner.run

    def fail_restart(argv, **kwargs):
        if tuple(argv) == TRY_RESTART:
            env.runner.active = False
            return CommandResult(17, "", "restart failed\n")
        return original(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", fail_restart)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "service_not_running"
    env.runner.calls.clear()
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]
    assert DAEMON_RELOAD in [argv for argv, _ in env.runner.calls]


def test_inactive_unrelated_failed_result_does_not_block_apply(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.active = False
    env.runner.result = "failed"
    _patch_period_probe(monkeypatch)
    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "applied_not_running"
    assert TRY_RESTART not in [argv for argv, _ in env.runner.calls]


def test_inactive_failed_result_does_not_mask_period_directory_sync_failure(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.active = False
    env.runner.result = "failed"
    _patch_period_probe(monkeypatch)
    monkeypatch.setattr(
        service.SafeRootFS,
        "prepare_period_directories",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("directory sync failed")),
    )
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["sample_period_state"] is None
    assert result["error"] == "sample_period_state_write_failed"


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


def test_a_fresh_host_without_a_runtime_directory_reports_not_answering(tmp_path):
    # Host acceptance finding: /run/appio exists only after the unit first starts, so
    # on a fresh install every verb that reports `answering` died with
    # "untrusted_parent:missing" instead of answering false. install-delegation had
    # already written both drop-ins and appended the group by then, so the failure
    # appeared to condemn a step that had actually succeeded.
    from appio.setup_boundary import CommandResult, SetupDeps, SetupIdentity

    root = tmp_path / "root"
    (root / "var/lib/appio").mkdir(parents=True, mode=0o711)
    identity = SetupIdentity(os.getuid(), os.getgid(), "alice", Path("/home/alice"))

    def metadata(fd):
        real = os.fstat(fd)
        return SimpleNamespace(st_uid=0, st_gid=identity.gid, st_mode=real.st_mode,
                               st_dev=real.st_dev, st_ino=real.st_ino,
                               st_nlink=real.st_nlink, st_size=real.st_size)

    record = lambda uid: SimpleNamespace(pw_uid=uid, pw_gid=identity.gid, pw_name="alice",
                                        pw_dir=str(identity.home))
    deps = SetupDeps(root, SimpleNamespace(run=lambda *a, **k: CommandResult(0, "", "")),
                     lambda: 0, record, metadata, lambda fd, uid, gid: None,
                     lambda: 123456789, lambda event: None)
    # A positive budget is required: with 0.0 the deadline is already spent and the
    # loop never runs, which would make this test pass without exercising anything.
    assert service.probe_daemon_status(deps=deps, timeout_seconds=0.05) is False


def test_the_probe_waits_for_a_runtime_directory_that_appears_mid_budget(tmp_path, monkeypatch):
    # Host acceptance finding H3: `enable` failed with enable:daemon_not_answering
    # while the daemon answered seconds later, because the probe returned False on the
    # first missing /run/appio instead of continuing to poll within its deadline.
    # "Not there yet" must consume budget, not end the observation.
    import appio.setup_service as service
    from appio.setup_boundary import CommandResult, SetupDeps, SetupIdentity

    root = tmp_path / "root"
    (root / "var/lib/appio").mkdir(parents=True, mode=0o711)
    identity = SetupIdentity(os.getuid(), os.getgid(), "alice", Path("/home/alice"))

    def metadata(fd):
        real = os.fstat(fd)
        return SimpleNamespace(st_uid=0, st_gid=identity.gid, st_mode=real.st_mode,
                               st_dev=real.st_dev, st_ino=real.st_ino,
                               st_nlink=real.st_nlink, st_size=real.st_size)

    record = lambda uid: SimpleNamespace(pw_uid=uid, pw_gid=identity.gid, pw_name="alice",
                                        pw_dir=str(identity.home))
    deps = SetupDeps(root, SimpleNamespace(run=lambda *a, **k: CommandResult(0, "", "")),
                     lambda: 0, record, metadata, lambda fd, uid, gid: None,
                     lambda: 123456789, lambda event: None)

    seen: list[float] = []

    def verify(_deps):
        # First poll: no runtime directory yet. Later polls: same failure, but the
        # point is that the loop kept going and used its budget.
        seen.append(1.0)
        raise __import__("appio.setup_boundary", fromlist=["TargetError"]).TargetError(1, "untrusted_parent:missing")

    monkeypatch.setattr(service, "_verify_socket_paths", verify)
    monkeypatch.setattr(service.time, "sleep", lambda _s: None)
    assert service.probe_daemon_status(deps=deps, timeout_seconds=0.06) is False
    assert len(seen) > 1, f"the probe gave up after {len(seen)} poll(s) instead of retrying"


def test_final_lifecycle_activation_race_requires_fresh_sample_probe(lifecycle, monkeypatch, capsys):
    env = lifecycle
    observations = []

    def status_probe(**_kwargs):
        observations.append(True)
        seconds = 10 if len(observations) < 4 else 11
        return {"sample_ns": seconds * 1_000_000_000}

    monkeypatch.setattr(service, "probe_daemon_status_payload", status_probe)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "sample_period_overridden"
    assert result["sample_period_state"] is None
    assert len(observations) >= 4


def test_pending_unlink_sync_failure_rechecks_absence_before_unchanged(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    original_prepare = service.SafeRootFS.prepare_period_directories
    prepared = []

    def record_prepare(fs):
        prepared.append(True)
        return original_prepare(fs)

    monkeypatch.setattr(service.SafeRootFS, "prepare_period_directories", record_prepare)
    original_publish = service.publish_period_complete
    completion_published = []

    def publish_then_mark(fs, record):
        result = original_publish(fs, record)
        completion_published.append(True)
        return result

    monkeypatch.setattr(service, "publish_period_complete", publish_then_mark)
    real_fsync = setup_files.os.fsync
    fsync_failed = []
    state_directory = host(env, "/var/lib/appio")

    def fail_pending_unlink_sync(fd):
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if completion_published and not fsync_failed and descriptor_path == state_directory:
            assert not host(env, SAMPLE_PERIOD_PENDING).exists()
            fsync_failed.append(True)
            raise OSError("pending unlink directory sync failed")
        return real_fsync(fd)

    monkeypatch.setattr(setup_files.os, "fsync", fail_pending_unlink_sync)
    code, first, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert first["sample_period_state"] == "applied"
    assert fsync_failed == [True]
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()

    call_count = len(env.runner.calls)
    code, retry, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert retry["sample_period_state"] == "unchanged"
    assert len(prepared) == 2
    retry_manager_calls = [argv for argv, _ in env.runner.calls[call_count:]]
    assert DAEMON_RELOAD not in retry_manager_calls
    assert TRY_RESTART not in retry_manager_calls


def test_missing_completion_evidence_replays_manager_operations(lifecycle, monkeypatch, capsys):
    env = lifecycle
    payload = b"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS=10\n"
    target = host(env, SAMPLE_PERIOD_DROPIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    _write_period_record(env, SAMPLE_PERIOD_PENDING)
    _patch_period_probe(monkeypatch)

    code, result, diagnostic = invoke(env, "set-sample-period", capsys, period=10)
    assert code == 0, diagnostic
    assert result["sample_period_state"] == "repair"
    assert target.read_bytes() == payload
    calls = [argv for argv, _ in env.runner.calls]
    assert calls.count(DAEMON_RELOAD) == 1
    assert calls.count(TRY_RESTART) == 1
    assert host(env, SAMPLE_PERIOD_COMPLETE).exists()
    assert not host(env, SAMPLE_PERIOD_PENDING).exists()


def test_active_before_inactive_after_has_service_not_running_precedence(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.sample_seconds = 11
    _patch_period_probe(monkeypatch)
    original_run = env.runner.run

    def stop_during_restart(argv, **kwargs):
        if tuple(argv) == TRY_RESTART:
            env.runner.calls.append((tuple(argv), kwargs))
            env.runner.active = False
            return CommandResult(0, "", "")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", stop_during_restart)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "service_not_running"
    assert result["sample_period_state"] == "service_not_running"
    assert TRY_RESTART in [argv for argv, _ in env.runner.calls]


def test_final_lifecycle_read_failure_after_stop_keeps_read_error(lifecycle, monkeypatch, capsys):
    env = lifecycle
    env.runner.sample_seconds = 11
    _patch_period_probe(monkeypatch)
    original_run = env.runner.run
    lifecycle_reads = []

    def stop_and_fail_final_read(argv, **kwargs):
        if tuple(argv) == TRY_RESTART:
            env.runner.calls.append((tuple(argv), kwargs))
            env.runner.active = False
            return CommandResult(0, "", "")
        if tuple(argv) == service.SHOW_ARGV:
            lifecycle_reads.append(True)
            if len(lifecycle_reads) == 3:
                env.runner.calls.append((tuple(argv), kwargs))
                return CommandResult(17, "", "final lifecycle unavailable\\n")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", stop_and_fail_final_read)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "inspect-service:exit_17"
    assert result["sample_period_state"] is None
    assert len(lifecycle_reads) >= 3


def test_final_lifecycle_read_failure_cannot_return_success(lifecycle, monkeypatch, capsys):
    env = lifecycle
    _patch_period_probe(monkeypatch)
    original_run = env.runner.run
    lifecycle_reads = []

    def fail_final_lifecycle_read(argv, **kwargs):
        if tuple(argv) == service.SHOW_ARGV:
            lifecycle_reads.append(True)
            if len(lifecycle_reads) == 3:
                env.runner.calls.append((tuple(argv), kwargs))
                return CommandResult(17, "", "final lifecycle unavailable\\n")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", fail_final_lifecycle_read)
    code, result, _ = invoke(env, "set-sample-period", capsys, period=10)
    assert code != 0
    assert result["error"] == "inspect-service:exit_17"
    assert result["sample_period_state"] is None
    assert len(lifecycle_reads) >= 3
