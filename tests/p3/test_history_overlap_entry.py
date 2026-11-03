"""Ownership exclusion must survive a live daemon reply in the shipped GUI."""
from functools import partial
import json
import shutil
import sqlite3
import stat
import threading
import time
from types import SimpleNamespace

from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtWidgets import QApplication, QLabel

from appio import iodclient
from appio.history_export import ExportResult
from appio.iod import Daemon, DaemonPaths
from appio.iod_history import HistoryRpc
from appio.ipc_models import QueryRequest
from appio.ipc_security import Peer
from appio.setup_boundary import CommandResult
from appio.ui import collector_window as ui
from test_history_router import source
from test_iod import APP, BOOT, NS, FIXTURES, Clock, FakeFilesystem, PollSelector, write_io


def test_live_incomplete_reply_cannot_resurrect_export_overlap_in_production_entry(
    tmp_path, monkeypatch, qapp,
):
    """Catch replacing ownership-filtered zeros/coverage with private daemon bytes."""
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    shutil.copytree(FIXTURES / "proc", proc, symlinks=True)
    shutil.copytree(FIXTURES / "cgroup/normal-t0", cgroup)
    shutil.rmtree(proc / "500")
    shutil.copyfile(FIXTURES / "cgroup/proc/diskstats", proc / "diskstats")
    for name, mode in (("state", 0o711), ("run", 0o750), ("gui-run", 0o700)):
        (tmp_path / name).mkdir(mode=mode)
    clock = Clock()
    paths = DaemonPaths(tmp_path / "state/iod.db", tmp_path / "run/iod.sock", proc, cgroup)
    write_io(proc, 100)
    # Freeze only the RPC wall budget, not its real SQLite query or socket path.
    monkeypatch.setattr("appio.iod.HistoryRpc", partial(HistoryRpc, clock_ns=lambda: 0))
    daemon = Daemon(paths, clock=clock, filesystem=FakeFilesystem(), selector=PollSelector(),
                    peer_reader=lambda sock: Peer(77, 1000, 987),
                    group_lookup=lambda uid: frozenset({987}) if uid == 1000 else frozenset())
    raw_client, raw_status = iodclient.IodClient, ui._read_daemon_status
    holder, local_opens = {}, []
    owner_threads: list[int] = []
    probe_threads: list[int] = []
    try:
        daemon.start()
        for step in range(1, 6):
            write_io(proc, 100 + step)
            clock.advance()
            daemon.step()
        # The daemon owns 10..20s (60 read + 40 write); the GUI owns 10..22s.
        # No handover proves either owner. Both whole deltas must be excluded.
        local, _ = source(tmp_path / "gui.db", boot=BOOT, start=10, end=22)
        local.connection.execute("UPDATE app SET key=?, name=?", (APP, "fixture"))
        local.connection.commit()
        local.connection.close()
        export_path = tmp_path / "verified-export.db"
        with sqlite3.connect(export_path) as copy:
            daemon.collector.store.connection.backup(copy)
        checkpoint = daemon.collector.sampler_state
        exported = ExportResult(export_path, BOOT, checkpoint.epoch,
                                checkpoint.last_seq, checkpoint.checkpoint_mono_ns)

        def status_reader():
            # The shell's probe worker owns the blocking read. It performs no
            # daemon ownership work: every daemon step stays on the test owner
            # thread that also drives the live client and the shell.
            probe_threads.append(threading.get_ident())
            return raw_status()

        def command(argv):
            assert argv == ui.UNIT_SHOW_ARGV
            return CommandResult(0, "UnitFileState=enabled\nActiveState=active\n"
                                 "SubState=running\nResult=success\nExecMainCode=0\n"
                                 "ExecMainStatus=0", "")

        def client_factory(socket_path, **kwargs):
            assert socket_path == paths.socket

            def verify(path):
                assert path == paths.socket
                info = path.lstat()
                assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o660

            client = raw_client(socket_path, **kwargs, peer_reader=lambda sock: Peer(88, 0, 987),
                                path_verifier=verify, clock_ns=lambda: clock.ns)
            holder["client"] = client
            return client

        def local_factory(**kwargs):
            local_opens.append(kwargs)
            raise AssertionError("an answering daemon must not start a GUI collector")

        def run_event_loop(application):
            window = next(widget for widget in application.topLevelWidgets()
                          if type(widget) is ui.CollectorWindow and not widget._closed)
            window._timer.stop()

            def step_daemon():
                """One owner-thread daemon mutation plus the live client pump."""
                owner_threads.append(threading.get_ident())
                daemon.step()
                holder["client"].pump()

            def drive():
                step_daemon()
                application.processEvents()

            def settle():
                after = window.facts_applied
                deadline = time.monotonic() + 10

                def current():
                    return (window.facts_applied > after
                            and window._pending is None
                            and window._prepare_retry is None
                            and window._displayed is not None
                            and window._displayed == window._target
                            and window.history.ready(window._displayed))

                while not current():
                    if window._retry_after is not None:
                        clock_ns = window._clock_ns
                        floor = window._retry_after
                        if clock_ns() < floor:
                            # Advance only the GUI's frozen virtual retry clock.
                            window._clock_ns = lambda clock_ns=clock_ns, floor=floor: max(
                                clock_ns(), floor
                            )
                    drive()
                    window.refresh()
                    assert time.monotonic() < deadline, (
                        window.history_state.text(), window.error_label.text())
                window._poll_timer.stop()

            def finish_probe():
                # Let the worker callable finish so nothing outlives the owner
                # thread, but deliver nothing: a late fact would reconcile and
                # retire the frame this test just asserted.
                deadline = time.monotonic() + 10
                while True:
                    outstanding = [future for future in
                                   (window._probe_inflight, window._probe_successor)
                                   if future is not None]
                    if all(future.done() or future.cancelled() for future in outstanding):
                        return
                    step_daemon()
                    time.sleep(0.001)
                    assert time.monotonic() < deadline, "probe did not finish"

            settle()
            # Use the production export validator/coordinator, not add_source.
            window.coordinator.accept_export(exported)
            window.refresh()
            settle()
            finish_probe()
            history, scope = window.history, window._prepared
            assert holder["client"].state.verified and holder["client"].state.answering
            assert not local_opens
            assert scope.boot_id == BOOT and scope.kind == "since_boot"
            assert len(history._sources) == 2
            assert history._overlap_ranges == [(BOOT, 10 * NS, 20 * NS)]
            assert history.local.connection.execute("SELECT count(*) FROM handover").fetchone()[0] == 0
            assert history.local.connection.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 20
            filtered, = history._ranking
            assert (filtered.total_bytes, filtered.read_bytes, filtered.write_bytes,
                    filtered.coverage.overlap_uncertain_intervals) == (0, 0, 0, 1)
            # Prove the live socket still offers the conflicting, honestly
            # incomplete 100 B answer, not an unanswered-client fallback.
            reply = holder["client"].query(QueryRequest("ranking", scope))
            deadline = time.monotonic() + 5
            while not reply.done():
                assert time.monotonic() < deadline, "live ranking did not answer"
                step_daemon()
                time.sleep(0.001)
            response = reply.result()
            remote_row, = response["result"]
            assert (remote_row.total_bytes, remote_row.read_bytes, remote_row.write_bytes,
                    remote_row.coverage.overlap_uncertain_intervals) == (100, 60, 40, 0)
            assert not remote_row.coverage.complete
            assert response["snapshot"] is not None and response.get("next_cursor") is None
            row, = history.ranking(scope, "total_bytes")
            share = history.disk_share(row.app_id, scope)
            series = history.series(row.app_id, scope)
            labels = {name: window.view.findChild(QLabel, name).text()
                      for name in ("total_value", "read_value", "write_value", "coverage_label")}
            observed = (row.total_bytes, row.read_bytes, row.write_bytes,
                        row.coverage.overlap_uncertain_intervals)
            print(json.dumps({"total_read_write_overlap": observed, "labels": labels,
                              "coverage": {"complete": row.coverage.complete,
                                           "overlap_uncertain_intervals": observed[3]}}, sort_keys=True))
            assert observed == (0, 0, 0, 1), observed
            assert (share.task_total_bytes, share.task_read_bytes, share.task_write_bytes,
                    share.coverage.overlap_uncertain_intervals) == (0, 0, 0, 1)
            assert sum(point.read_bytes + point.write_bytes for point in series.points) == 0
            assert series.coverage.overlap_uncertain_intervals == 1
            assert not row.coverage.complete and not share.coverage.complete
            assert labels["total_value"] == labels["read_value"] == labels["write_value"] == "0 B"
            assert "coverage: incomplete" in labels["coverage_label"]
            # One owner thread stepped the daemon; the blocking status read ran
            # on the shell's probe worker, never on that owner thread.
            assert owner_threads and set(owner_threads) == {threading.get_ident()}
            assert probe_threads
            assert set(probe_threads).isdisjoint({threading.get_ident()})
            return 0

        monkeypatch.setattr(ui, "SOCKET_PATH", paths.socket)
        monkeypatch.setattr(ui, "_run_systemctl_show", command)
        monkeypatch.setattr(ui, "_read_daemon_status", status_reader)
        monkeypatch.setattr(ui, "read_peer", lambda sock: Peer(88, 0, 987))
        monkeypatch.setattr(iodclient, "IodClient", client_factory)
        monkeypatch.setattr(ui, "_gui_lease_path", lambda: tmp_path / "gui-run/collector.lock")
        monkeypatch.setattr(ui, "LiveCollector", local_factory)
        monkeypatch.setattr(ui.time, "clock_gettime_ns", lambda which: clock.ns)
        monkeypatch.setattr(QApplication, "exec", run_event_loop)
        assert ui.run_interactive(SimpleNamespace(db=tmp_path / "gui.db", proc_root=proc,
                                                 cgroup_root=cgroup, interval=1.0)) == 0
    finally:
        daemon.stop()
        daemon.selector.close()
