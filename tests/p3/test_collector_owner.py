"""Owner changes require service evidence, a lease and a stopped local writer."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from appio.collector_owner import (
    CollectorCoordinator, GuiLease, ServiceFacts, decide_owner,
)

OFF = ServiceFacts(False, "inactive", False, False, False)
ON = ServiceFacts(True, "active", True, True, False)
SILENT = ServiceFacts(True, "unknown", False, False, None)


def test_silent_enabled_service_is_not_permission_to_collect_locally():
    decision = decide_owner(SILENT)
    assert decision.owner == "unknown"
    assert not decision.may_collect_local
    assert decision.state == "enabled, not answering"


def test_only_proven_absence_allows_gui_fallback():
    assert decide_owner(OFF).may_collect_local
    assert decide_owner(ON).owner == "iod"
    assert not decide_owner(ON).may_collect_local
    assert not decide_owner(replace(OFF, active="active")).may_collect_local


class Local:
    def __init__(self, events):
        self.events = events
        self.baseline_only = True
        self.sampler_state = None
        self.last_frame = None
        self.boot_id = "boot"
        self.epoch = None
        self.next_seq = 0
        self.last_observed_mono_ns = None

    def tick(self):
        self.events.append("tick")

    def quiesce(self):
        self.events.append("quiesce")
        return None

    def close(self):
        self.events.append("close")


class History:
    def __init__(self):
        self.gaps = []
        self.invalidations = 0
        self.view_state = "loading"

    def invalidate(self):
        self.invalidations += 1
        self.view_state = f"unavailable:{self.invalidations}"

    def record_transport_gap(self, notice):
        self.gaps.append(notice)


def coordinator(tmp_path, events, **kwargs):
    def factory():
        events.append("open")
        return Local(events)
    client = SimpleNamespace(close=lambda: events.append("client-close"), state=None)
    return CollectorCoordinator(local_factory=kwargs.get("factory", factory), client=client,
        history=History(), lease=GuiLease(tmp_path / "collector.lock"),
        clock=lambda: (100, 1000))


def test_daemon_startup_never_opens_local_and_disconnect_is_not_absence(tmp_path):
    events = []
    owner = coordinator(tmp_path, events)
    assert owner.reconcile(ON).owner == "iod"
    owner.tick_local()
    assert owner.reconcile(SILENT).owner == "unknown"
    owner.tick_local()
    assert events == []
    owner.close()


def test_absence_opens_once_and_takeover_quiesces_before_further_ticks(tmp_path):
    events = []
    owner = coordinator(tmp_path, events)
    assert owner.reconcile(OFF).may_collect_local
    owner.reconcile(OFF)
    owner.tick_local()
    assert events == ["open", "tick"]
    owner.reconcile(ON)
    owner.tick_local()
    assert events == ["open", "tick", "quiesce", "close"]
    owner.reconcile(OFF)
    owner.tick_local()
    assert events[-2:] == ["open", "tick"]
    owner.close()


def test_second_gui_is_a_viewer_until_first_releases_lease(tmp_path):
    first_events, second_events = [], []
    first, second = coordinator(tmp_path, first_events), coordinator(tmp_path, second_events)
    first.reconcile(OFF)
    decision = second.reconcile(OFF)
    assert not decision.may_collect_local
    assert second_events == []
    first.close()
    assert second.reconcile(OFF).may_collect_local
    assert second_events == ["open"]
    second.close()


@pytest.mark.parametrize("trap", ["symlink", "directory", "hardlink", "writable"])
def test_lease_rejects_unsafe_files_without_deleting_them(tmp_path, trap):
    path = tmp_path / "lock"
    target = tmp_path / "target"
    target.write_text("keep")
    if trap == "symlink":
        path.symlink_to(target)
    elif trap == "directory":
        path.mkdir()
    elif trap == "hardlink":
        path.hardlink_to(target)
    else:
        path.write_text("keep")
        path.chmod(0o666)
    lease = GuiLease(path)
    assert lease.acquire() is False
    assert target.read_text() == "keep"
    assert path.exists()


def test_factory_failure_releases_lease(tmp_path):
    def failing():
        raise RuntimeError("construction failed")
    owner = coordinator(tmp_path, [], factory=failing)
    with pytest.raises(RuntimeError, match="construction failed"):
        owner.reconcile(OFF)
    lease = GuiLease(tmp_path / "collector.lock")
    assert lease.acquire()
    lease.release()
    owner.close()


def test_quiesce_failure_never_leaves_permission_for_another_tick(tmp_path):
    events = []
    local = Local(events)
    def fail():
        events.append("quiesce-failed")
        raise RuntimeError("crash")
    local.quiesce = fail
    owner = coordinator(tmp_path, events, factory=lambda: local)
    owner.reconcile(OFF)
    with pytest.raises(RuntimeError, match="crash"):
        owner.reconcile(ON)
    owner.tick_local()
    assert "tick" not in events
    owner.close()


def test_real_collector_quiesces_and_never_commits_a_gui_frame_under_daemon_ownership(tmp_path):
    from test_live_sources import make_tree, write_all, SequenceClock, WALL
    from appio.live import LiveCollector
    proc, group = make_tree(tmp_path)
    clock = SequenceClock(*((at*10**9, WALL+at*10**9) for at in (100, 101, 110)))
    opened = []
    def factory():
        local = LiveCollector(proc, group, tmp_path / "local.db", clock,
                              source="gui", baseline_only=True)
        opened.append(local)
        return local
    owner = coordinator(tmp_path, [], factory=factory)
    owner.reconcile(OFF)
    owner.tick_local()
    write_all(proc, (1000,)*4)
    owner.tick_local()
    owner.reconcile(ON)
    owner.tick_local()
    assert opened[0]._quiesced and opened[0]._closed
    with pytest.raises(RuntimeError, match="closed"):
        opened[0].tick()
    assert owner.outgoing_handover.to_source == "iod"
    assert owner.outgoing_handover.state.cutoff_mono_ns == 110*10**9
    owner.close()


def test_owner_thread_and_reentrant_callbacks_cannot_race_the_writer(tmp_path):
    import threading
    events = []
    owner = coordinator(tmp_path, events)
    owner.reconcile(OFF)
    local = owner._local
    def tick():
        with pytest.raises(RuntimeError, match="in progress"):
            owner.reconcile(ON)
    local.tick = tick
    owner.tick_local()
    errors = []
    def other_thread():
        try:
            owner.tick_local()
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert errors and "owner thread" in errors[0]
    owner.close()


def test_export_is_opened_read_only_and_must_match_its_checkpoint(tmp_path):
    from test_history_router import source, router, BOOT, NS
    from appio.history_export import ExportResult
    local, _ = source(tmp_path / "local.db")
    root, state = source(tmp_path / "export.db", "iod", start=110, end=130)
    history = router(local)
    owner = coordinator(tmp_path, [])
    owner.history = history
    owner.reconcile(OFF)
    export = ExportResult(tmp_path / "export.db", BOOT, state.epoch, state.last_seq, 130*NS)
    before = (tmp_path / "export.db").read_bytes()
    owner.accept_export(export)
    assert owner.resume_handover is not None
    exported = next(service for name, (service, _) in history._sources.items() if name != "local")
    assert exported.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(Exception, match="readonly"):
        exported.connection.execute("DELETE FROM sample")
    assert (tmp_path / "export.db").read_bytes() == before
    with pytest.raises(ValueError, match="checkpoint"):
        owner.accept_export(replace(export, cutoff_mono_ns=129*NS))
    owner.close()


def test_export_from_a_running_daemon_is_not_a_resume_permit(tmp_path):
    from test_history_router import source, router, BOOT, NS
    from appio.history_export import ExportResult
    local, _ = source(tmp_path / "local.db")
    root, state = source(tmp_path / "export.db", "iod", start=110, end=130)
    owner = coordinator(tmp_path, [])
    owner.history = router(local)
    owner.reconcile(ON)
    owner.accept_export(ExportResult(tmp_path / "export.db", BOOT, state.epoch, state.last_seq, 130*NS))
    assert owner.resume_handover is None
    assert any("orderly stopped" in issue for issue in owner.boundary_issues)
    owner.close()


def test_inactive_export_retires_unsampled_gui_and_installs_before_the_first_tick(tmp_path):
    import shutil
    import sqlite3
    from test_live_sources import make_tree, write_all, write_io, PIDS, SequenceClock, WALL
    from test_history_router import router, NS
    from appio.handover import capture_handover
    from appio.history_export import ExportResult
    from appio.live import LiveCollector
    from appio.store import AppIoStore
    proc, group = make_tree(tmp_path)
    clock = SequenceClock(*((at*NS, WALL+at*NS) for at in (100, 101, 110, 111, 120, 121, 130, 140)))
    database = tmp_path / "local.db"
    daemon = LiveCollector(proc, group, database, clock, source="iod", baseline_only=True)
    daemon.tick()
    write_all(proc, (1000,)*4)
    daemon.tick()
    shutil.rmtree(proc / "411")
    daemon.tick()
    cutoff = daemon.quiesce()
    assert any(process.credits for process in cutoff.state.processes)
    exported = tmp_path / "export.db"
    with sqlite3.connect(exported) as copy:
        daemon.store.connection.backup(copy)
    daemon.close()
    local = AppIoStore(database)
    history = router(local)
    opened = []
    def factory():
        offer = owner.resume_handover
        collector = LiveCollector(proc, group, database, clock, source="gui",
                                  baseline_only=offer is None, resume_handover=offer)
        opened.append(collector)
        return collector
    owner = coordinator(tmp_path, [], factory=factory)
    owner.history = history
    owner.reconcile(OFF)
    owner.accept_export(ExportResult(exported, cutoff.state.boot_id, cutoff.outgoing_epoch,
                                     cutoff.outgoing_seq, cutoff.state.cutoff_mono_ns))
    owner.tick_local()
    assert len(opened) == 1  # awaiting reconcile, never an accidental fresh tick
    owner.reconcile(OFF)
    assert len(opened) == 2
    assert opened[1].last_frame is not None
    assert opened[1].handover_fallback_reason is None
    assert capture_handover(opened[1].sampler_state).processes == cutoff.state.processes
    assert owner.resume_handover is None  # consumed exactly once
    before = local.connection.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone()[0]
    for pid in set(PIDS) - {411}:
        write_io(proc, pid, (2000,)*4)
    owner.tick_local()
    assert local.connection.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone()[0] == before
    assert local.connection.execute("SELECT from_mono_ns,to_mono_ns FROM gap WHERE source='gui'").fetchone() == (111*NS, 130*NS)
    assert all(not process.credits for process in opened[1].sampler_state.processes)
    for pid in set(PIDS) - {411}:
        write_io(proc, pid, (2010,)*4)
    owner.tick_local()
    assert local.connection.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone()[0] == before + 30
    owner.close()
    history.close()


def test_reconcile_same_history_key_does_not_invalidate(tmp_path):
    owner = coordinator(tmp_path, [])
    owner.reconcile(SILENT)
    assert owner.history.invalidations == 1
    state = owner.history.view_state

    owner.reconcile(replace(SILENT, active="active"))

    assert owner.history.invalidations == 1
    assert owner.history.view_state == state
    owner.close()


def test_reconcile_changed_history_key_invalidates_once_per_change(tmp_path):
    owner = coordinator(tmp_path, [])

    assert owner.reconcile(SILENT).state == "enabled, not answering"
    assert owner.reconcile(OFF).state == "off"
    assert owner.reconcile(ON).state == "on, answering"

    assert owner.history.invalidations == 3
    owner.close()


def test_reconcile_publishes_history_key_only_after_invalidation(tmp_path):
    owner = coordinator(tmp_path, [])
    owner.reconcile(SILENT)
    previous_key = owner._history_key
    original_invalidate = owner.history.invalidate
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("invalidation failed")
        original_invalidate()

    owner.history.invalidate = fail_once
    changed = replace(SILENT, daemon_verified=True)
    with pytest.raises(RuntimeError, match="invalidation failed"):
        owner.reconcile(changed)
    assert owner._history_key == previous_key

    owner.reconcile(changed)
    assert attempts == 2
    assert owner.history.invalidations == 2
    assert owner._history_key == ("unknown", False, True, False)
    owner.close()


def test_tick_local_returns_committed_advance_without_invalidation(tmp_path):
    owner = coordinator(tmp_path, [])
    owner.reconcile(OFF)
    invalidations = owner.history.invalidations

    assert owner.tick_local() is True
    assert owner.history.invalidations == invalidations
    assert "tick" in owner._local.events
    owner.close()

    no_local = coordinator(tmp_path, [])
    no_local.reconcile(SILENT)
    invalidations = no_local.history.invalidations
    assert no_local.tick_local() is False
    assert no_local.history.invalidations == invalidations
    no_local.close()

    failing = coordinator(tmp_path, [])
    failing.reconcile(OFF)
    error = RuntimeError("tick failed")
    failing._local.tick = lambda: (_ for _ in ()).throw(error)
    with pytest.raises(RuntimeError) as raised:
        failing.tick_local()
    assert raised.value is error
    failing.close()


def test_parent_symlink_does_not_redirect_the_lease(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(safe, target_is_directory=True)
    assert not GuiLease(alias / "lock").acquire()
    assert not (safe / "lock").exists()
