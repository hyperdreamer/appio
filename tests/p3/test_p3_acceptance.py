"""P3 acceptance: fixture counters travel through the real daemon and Qt shell."""
import os
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QLabel

from appio.contracts import MAX_RANGE_NS, Window, WindowUnavailableError
from appio.ipc_models import QueryRequest
from appio.setup_files import SERVICE_DROPIN, USER_MANAGER_DROPIN
from acceptance_harness import APP, BOOT, NS, P3Harness


def test_daemon_survives_gui_close_and_disable_exports_final_commits(tmp_path, qapp):
    with P3Harness(tmp_path) as harness:
        harness.enable()
        harness.open_gui()
        harness.advance(2)
        harness.close_gui()
        before = harness.daemon_rows()
        harness.advance(6)
        after = harness.daemon_rows()
        assert len(after) > len(before)
        result = harness.disable_and_export()
        assert harness.export_rows(result.path) == harness.daemon_rows()
        assert result.cutoff_mono_ns is not None
        assert ("/usr/bin/systemctl", "disable", "--now", "appio-iod.service") in harness.commands
        assert not any("restart" in item for item in harness.commands)
        assert harness.totals() == (160, 80, 48, 32)
        assert (result.boot_id, result.iod_epoch, result.seq, result.cutoff_mono_ns) == (BOOT, 1, 4, 18 * NS)
        assert {name for name, _, _ in harness.daemon_rows()} >= {
            "sample", "sample_dev", "sample_coverage", "collector_epoch", "sampler_checkpoint"
        }


def test_fixture_tick_sqlite_socket_client_router_and_window_match(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(10)
        assert h.totals() == (200, 100, 60, 40)
        h.open_gui()
        # The GUI database is genuinely empty. Its initial scope must come from
        # the verified daemon status rather than a synthetic local boot row.
        assert h.gui.window._prepared is not None
        assert h.gui.window._prepared.boot_id == BOOT
        scope = Window("range", BOOT, 10 * NS, 20 * NS)
        # A successful daemon query is authoritative even when its measured
        # coverage is honestly incomplete; no privileged export is required.
        rows = h.query(scope)
        row, = rows
        assert (row.app_key, row.rchar, row.wchar, row.read_bytes, row.write_bytes) == (APP, 160, 80, 48, 32)
        assert not row.coverage.complete and row.coverage.reap_incomplete
        assert h.gui.client.state.verified and h.gui.client.state.answering
        assert h.gui.client.state.cursor == h.daemon.committed_cursor
        remote = h.rpc(QueryRequest("totals", scope, APP))["result"]
        assert (remote.task_read_bytes, remote.task_write_bytes, remote.task_total_bytes) == (48, 32, 80)
        assert [(d.dev, d.rbytes, d.wbytes) for d in remote.devices] == [
            ("259:0", 52, 20), ("259:1", 28, 36)
        ]
        snapshot = h.wire({"protocol": 1, "type": "subscribe", "request_id": "devices", "since": None}, response_type="snapshot")
        assert [(d["dev"], d["name"]) for d in snapshot["devs"]] == [
            ("259:0", "nvme0n1"), ("259:1", "nvme0n1p1")
        ]
        assert (remote.device_total_bytes, remote.residual_total) == (136, -56)
        assert sum(d.rbytes + d.wbytes for d in remote.devices) + remote.residual_total == 80
        # Render the same prepared result through the actual embedded P2 window.
        h.render(scope)
        view = h.gui.window.view
        assert view is not None
        assert view.findChild(QLabel, "total_value").text() == "80 B"
        assert view.findChild(QLabel, "read_value").text() == "48 B"
        assert view.findChild(QLabel, "write_value").text() == "32 B"
        assert view.findChild(QLabel, "device_value").text() == "136 B"
        assert "attributed exceeds task accounting by 56 B" in view.findChild(QLabel, "residual_value").text()
        assert "difference between accounting points" in view.findChild(QLabel, "residual_value").text()
        # Device identity comes from the fixture diskstats, never a host disk.
        assert h.gui.local.connection.execute("SELECT count(*) FROM sample WHERE source='gui'").fetchone()[0] == 0


def test_daemon_crash_restart_skips_unobserved_work_and_never_doubles(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.open_gui()
        h.advance(4)
        old = h.gui.client.state.cursor
        h.crash()
        h.advance(4)
        assert not h.gui.client.state.answering
        h.restart()
        h.advance(2)
        assert h.totals() == (120, 60, 36, 24)
        assert h.gui.client.state.cursor.iod_epoch == old.iod_epoch + 1
        assert h.gui.client.state.cursor.seq == 1
        assert h.sql("SELECT from_mono_ns,to_mono_ns,reason FROM gap") == [(14 * NS, 18 * NS, "collector_gap")]
        assert h.gui.gaps
        scope = Window("range", BOOT, 10 * NS, 30 * NS)
        result = h.rpc(QueryRequest("totals", scope, APP))["result"]
        assert result.task_total_bytes == 60
        assert not result.coverage.complete and result.coverage.gap_intervals > 0


def test_absent_daemon_is_unavailable_and_two_guis_have_one_fallback_writer(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        # Controller presence is absent, so device bytes must stay nullable.
        (h.cgroup / "cgroup.controllers").write_text("cpu memory pids\n")
        h.open_gui()
        first = h.gui
        h.open_gui()
        second = h.gui
        h.advance(10)
        assert first.coordinator._decision.may_collect_local
        assert not second.coordinator._decision.may_collect_local
        assert "viewer" in second.coordinator._decision.state
        assert not first.client.state.verified and not second.client.state.answering
        assert first.window.banner_status.text() == "off"
        scope = Window("range", BOOT, 10 * NS, 20 * NS)
        row, = h.query(scope, gui=first)
        assert (row.read_bytes, row.write_bytes) == (48, 32)
        share = h.disk_share(row.app_id, scope, gui=first)
        assert share.device_total_bytes is None and share.residual_total is None
        h.render(scope, gui=first)
        assert first.window.view.findChild(QLabel, "device_value").text() == "unavailable"
        assert "unavailable" in first.window.view.findChild(QLabel, "residual_value").text()
        first.window.open_files_unavailable()
        assert "file activity is unavailable" in first.window.files_message.text()
        h.close_gui(gui=first)
        h.advance(2)
        assert second.coordinator._decision.may_collect_local
        assert h.commands == []


@pytest.mark.parametrize("scope,message", [
    (Window("launch", BOOT, launch_id=99), "Launch window disabled: device and file activity are not stored per launch."),
    (Window("range", BOOT, 0, MAX_RANGE_NS + 1), "Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges."),
])
def test_a22_exact_messages_in_client_router_and_window(tmp_path, qapp, scope, message):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(4)
        h.open_gui()
        h.import_history()
        h.render(Window("range", BOOT, 10 * NS, 20 * NS))
        with pytest.raises(WindowUnavailableError) as rejected:
            h.gui.history.prepare(scope)
        assert str(rejected.value) == message
        rejected = h.gui.client.query(QueryRequest("totals", scope, APP))
        h.until(rejected.done)
        assert str(rejected.exception()) == message
        h.gui.window.view.set_window(scope)
        assert message in " ".join(label.text() for label in h.gui.window.view.findChildren(QLabel))
        assert h.gui.window.view.findChild(QLabel, "total_value").text() == "unavailable"


def test_enable_reboot_choice_probe_success_disable_and_fallback(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        h.open_gui()
        h.advance(2)
        h.enable()
        assert h.last_reply["pending_activation"] is True
        before = list(h.commands)
        chosen = h.choose_reboot()
        assert chosen.pending_activation and h.commands == before
        h.advance(2)
        assert h.gui.window.banner_status.text() == "on, needs activation"
        evidence = h.activate()
        assert evidence.evidence.expected_bytes == evidence.evidence.observed_bytes == 16 * 1024 * 1024
        assert evidence.evidence.cleanup_complete
        assert evidence.pending_activation is False
        h.advance(2)
        assert h.gui.window.banner_status.text() == "on, answering"
        dropins = [h.host(p).read_bytes() for p in (SERVICE_DROPIN, USER_MANAGER_DROPIN)]
        exported = h.disable_and_export()
        assert h.export_rows(exported.path) == h.daemon_rows()
        assert [h.host(p).read_bytes() for p in (SERVICE_DROPIN, USER_MANAGER_DROPIN)] == dropins
        h.advance(2)
        assert h.gui.coordinator._decision.may_collect_local
        assert not any(argv[1:2] == ("restart",) for argv in h.commands)
        assert ("/usr/bin/usermod", "-aG", "appio", "alice") in h.commands


def test_d3_live_enable_is_fresh_baseline_with_explicit_partial_coverage(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        h.open_gui()
        h.advance(2)
        h.enable()
        assert h.totals() == (0, 0, 0, 0)
        assert h.sql("SELECT count(*) FROM handover") == [(0,)]
        assert h.sql("SELECT source,epoch,last_seq FROM collector_epoch") == [("iod", 1, 0)]
        coverage = h.rpc(QueryRequest("coverage", Window("lifetime"), APP))["result"]
        assert not coverage.complete
        assert (coverage.partial_intervals, coverage.reap_incomplete) == (1, True)
        h.advance(2)
        assert h.totals() == (40, 20, 12, 8)


def test_raw_rollup_boundary_uses_one_tier_through_socket_router_and_window(tmp_path, qapp):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(60)
        h.open_gui()
        h.import_history()
        day = 86400 * NS
        for end, tier in ((day, "raw"), (day + 1, "rollup_60s")):
            scope = Window("range", BOOT, 0, end)
            remote = h.rpc(QueryRequest("totals", scope, APP))["result"]
            assert (remote.task_read_bytes, remote.task_write_bytes) == (360, 240)
            assert remote.coverage.tier == tier and not remote.coverage.complete
            row, = h.query(scope)
            assert (row.rchar, row.wchar, row.read_bytes, row.write_bytes) == (1200, 600, 360, 240)
            assert row.coverage.tier == tier
            h.render(scope)
            assert h.gui.window.view.findChild(QLabel, "total_value").text() == "600 B"
        assert h.totals() == (1200, 600, 360, 240)


def test_all_mutation_opens_are_confined_and_unknown_commands_fail(tmp_path):
    with P3Harness(tmp_path) as h:
        h.enable()
        h.advance(2)
        h.disable_and_export()
        assert h.mutation_paths
        assert all(path.is_relative_to(h.root) for path in h.mutation_paths)
        with pytest.raises(AssertionError, match="outside temporary root"):
            h.deps.runner.run(("/usr/bin/touch", "/etc/appio-unexpected"))
        assert h.actual_uid == os.getuid() != 0


def _host_acceptance():
    path = Path(__file__).resolve().parents[2] / "tools/acceptance/appio-p3-root.py"
    spec = importlib.util.spec_from_file_location("p3_host_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("phase", ["period-runtime", "period-capacity"])
def test_period_phase_dry_run_is_pure(monkeypatch, capsys, tmp_path, phase):
    module = _host_acceptance()
    import builtins
    from types import SimpleNamespace
    original_import = builtins.__import__
    original_os = module.os
    evidence_path = tmp_path / ".local" / "state" / "appio" / "p3-acceptance.json"

    def checked_import(name, *args, **kwargs):
        assert not name.startswith("appio")
        return original_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run accessed host state")

    class GuardedOS:
        def __getattr__(self, name):
            if name in {
                "open", "stat", "lstat", "access", "listdir", "scandir", "walk",
                "readlink", "mkdir", "makedirs", "unlink", "remove", "rmdir", "rename",
                "replace", "chmod", "fchmod", "fsync", "write", "truncate",
            }:
                return forbidden
            return getattr(original_os, name)

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "__import__", checked_import)
        guarded.setattr(builtins, "open", forbidden)
        guarded.setattr(module, "os", GuardedOS())
        for name in (
            "open", "read_text", "read_bytes", "write_text", "write_bytes", "exists",
            "is_file", "is_dir", "stat", "lstat", "mkdir", "unlink", "rename", "replace",
            "glob", "iterdir",
        ):
            guarded.setattr(Path, name, forbidden)
        guarded.setattr(module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
        guarded.setattr(module.subprocess, "run", forbidden)
        guarded.setattr(module.subprocess, "Popen", forbidden)
        guarded.setattr(sys, "argv", ["acceptance", "--phase", phase])
        result = module.main()
    assert result == 0
    assert f"Selected phase: {phase}" in capsys.readouterr().out
    assert not evidence_path.exists()


def test_period_runtime_phase_is_available_in_side_effect_free_dry_run(monkeypatch, capsys, tmp_path):
    test_period_phase_dry_run_is_pure(monkeypatch, capsys, tmp_path, "period-runtime")


def test_period_capacity_phase_is_available_in_side_effect_free_dry_run(monkeypatch, capsys, tmp_path):
    test_period_phase_dry_run_is_pure(monkeypatch, capsys, tmp_path, "period-capacity")


def test_helper_period_argument_is_not_observation_callback(monkeypatch):
    module = _host_acceptance()
    calls = []
    def command(argv):
        calls.append(argv)
        return json.dumps({"version": 1, "verb": argv[2], "uid": os.getuid()})
    monkeypatch.setattr(module, "command", command)
    def unexpected_popen(*args, **kwargs):
        raise AssertionError("invalid helper arguments reached subprocess")
    monkeypatch.setattr(module.subprocess, "Popen", unexpected_popen)
    for verb in ("install-delegation", "enable", "disable", "status", "restart-user-manager", "export-history"):
        module.helper(verb)
        assert calls[-1] == module.HELPER + (verb,)
        with pytest.raises((RuntimeError, TypeError)):
            module.helper(verb, "2")
    module.helper("set-sample-period", "2")
    assert calls[-1] == module.HELPER + ("set-sample-period", "2")
    for args in ((), ("1",), ("31",), ("02",), ("2", "3"), (2,)):
        with pytest.raises((RuntimeError, TypeError)):
            module.helper("set-sample-period", *args)

    class Child:
        returncode = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def poll(self):
            self.polls = getattr(self, "polls", 0) + 1
            return None if self.polls == 1 else 0
        def communicate(self):
            return json.dumps({"version": 1, "verb": "status", "uid": os.getuid()}), ""
    def popen(argv, **kwargs):
        calls.append(argv)
        return Child()
    observed = []
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    module.helper("status", observe=lambda: observed.append(True))
    assert calls[-1] == module.HELPER + ("status",)
    assert observed == [True]


def test_capacity_fixture_materializes_and_prunes_real_core_units(tmp_path):
    module = _host_acceptance()
    from appio.quota import QuotaPruner
    database = tmp_path / "capacity.sqlite"
    with module.capacity_fixture(database, app_count=3, initial_count=12) as db:
        pruner = QuotaPruner(db, [database], batch_size=4)
        result = pruner.expire("capacity", 2 * 86_400 * NS)
        assert len(result.deleted) == 4
        assert not result.skipped
        assert db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 8
        module.capacity_generate(db, 3, 12, 2 * NS)
        assert db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 11


def test_capacity_fixture_full_reserve_supports_saturated_production_prune(tmp_path):
    module = _host_acceptance()
    from appio.quota import QuotaPruner
    database = tmp_path / "capacity.sqlite"
    with module.capacity_fixture(database) as db:
        assert db.execute("SELECT count(*) FROM app").fetchone()[0] == 226
        assert db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 57_968
        result = QuotaPruner(db, [database]).expire("capacity", 2 * 86_400 * NS + 15 * NS)
        assert len(result.deleted) == 2048
        assert not result.skipped
        assert all(unit.kind == "core" for unit in result.deleted)
        assert db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0] == 57_968 - 2048


def test_restart_gap_accepts_new_gap_and_rejects_old():
    module = _host_acceptance()
    before = {"tables": {"gap": [(9, "boot", "iod", 1, 1, 1, 2, "collector_gap")]}}
    apply = {"status": {"cursor": {"boot_id": "boot", "iod_epoch": 2}}, "restart_boundary_ns": 10}
    after = {"tables": {"gap": before["tables"]["gap"] + [(10, "boot", "iod", 2, 0, 3, 4, "collector_gap")], "sample": [], "collector_epoch": []}}
    assert module.test_active_period_apply_records_restart_gap(before, after, apply)["new_gaps"]
    after["tables"]["gap"] = before["tables"]["gap"]
    with pytest.raises(RuntimeError):
        module.test_active_period_apply_records_restart_gap(before, after, apply)

    after["tables"]["collector_epoch"] = [("iod", "boot", 2, 1, 15)]
    after["tables"]["sample"] = [("boot", "iod", 2, 1, 1, 0, 15, 15, 5, 10)]
    assert module.test_active_period_apply_records_restart_gap(before, after, apply)["new_epoch_start_ns"] == 15
    after["tables"]["sample"][0] = ("boot", "iod", 2, 1, 1, 0, 15, 15, 5, 9)
    with pytest.raises(RuntimeError):
        module.test_active_period_apply_records_restart_gap(before, after, apply)


def test_ten_minute_measurement_checks_identity_endpoints_and_cadence(monkeypatch):
    from types import SimpleNamespace
    module = _host_acceptance()
    clock = [100 * NS]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic_ns=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + int(seconds * NS))))
    cutoff = {"boot_id": "boot", "iod_epoch": 3}
    frames = [("boot", "iod", 3, seq, 1, 0, clock[0] + seq * 30 * NS, clock[0] + seq * 30 * NS) for seq in range(1, 21)]
    export = {"helper": {"export_cutoff": cutoff}, "tables": {"sample": frames}}
    monkeypatch.setattr(module, "period_export", lambda: export)
    monkeypatch.setattr(module, "period_status", lambda seconds: {"sample_ns": seconds * NS, "cursor": cutoff})
    monkeypatch.setattr(module, "unit", lambda: {"InvocationID": "instance"})
    result = module.test_thirty_second_ten_minute_run_has_no_overdue_paths()
    assert len(result["endpoints"]) == 20
    assert result["end_ns"] - result["start_ns"] == 600 * NS
    clock[0] = 100 * NS
    broken = list(frames[10])
    broken[7] += 10 * NS
    frames[10] = tuple(broken)
    with pytest.raises(RuntimeError, match="overdue or irregular"):
        module.test_thirty_second_ten_minute_run_has_no_overdue_paths()


def test_runtime_always_restores_after_failed_apply(monkeypatch):
    module = _host_acceptance()
    monkeypatch.setattr(module, "period_export", lambda: {"helper": {}, "tables": {}})
    def failed_apply(seconds):
        raise RuntimeError("apply failed")
    monkeypatch.setattr(module, "test_apply_two_and_thirty_seconds_restarts_only_active_service", failed_apply)
    calls = []
    monkeypatch.setattr(module, "helper", lambda *args: calls.append(args) or {"sample_period": 10})
    monkeypatch.setattr(module, "period_status", lambda seconds: {"sample_ns": seconds * NS})
    evidence = {"host_checks": {"verify": "passed"}}
    with pytest.raises(RuntimeError, match="apply failed"):
        module.period_runtime(evidence)
    assert calls == [("set-sample-period", "10")]
    assert evidence["period_runtime"]["cleanup"]["restored"] is True


def test_runtime_restoration_failure_is_recorded(monkeypatch):
    module = _host_acceptance()
    monkeypatch.setattr(module, "period_export", lambda: {"helper": {}, "tables": {}})
    monkeypatch.setattr(module, "test_apply_two_and_thirty_seconds_restarts_only_active_service", lambda seconds: (_ for _ in ()).throw(RuntimeError("apply failed")))
    monkeypatch.setattr(module, "helper", lambda *args: (_ for _ in ()).throw(RuntimeError("restore failed")))
    evidence = {"host_checks": {"verify": "passed"}}
    with pytest.raises(RuntimeError, match="restore failed"):
        module.period_runtime(evidence)
    assert evidence["period_runtime"]["cleanup"] == {"restored": False, "error": "restore failed"}
