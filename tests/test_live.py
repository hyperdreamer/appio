"""Tests for the live collector and the runnable application entry point."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

# pytest-qt can discover the PySide6 backend first on this host.  appio's
# production UI is PyQt6, so pin the backend before importing Qt widgets.
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtWidgets import QListWidget

from appio.query import QueryService
from appio.ui.main_window import AppIoWindow
from appio.live import LiveCollector

PROC_FIXTURE = Path(__file__).parent / "fixtures" / "proc"
CGROUP_FIXTURE = Path(__file__).parent / "fixtures" / "cgroup" / "normal-t0"
BOOT_ID = "11111111-2222-4333-8444-555555555555"
APP_KEY = "unit:app-chromium@fixture.service"


class SequenceClock:
    def __init__(self, *pairs: tuple[int, int]) -> None:
        self._pairs = iter(pairs)

    def __call__(self) -> tuple[int, int]:
        return next(self._pairs)


def make_input_tree(tmp_path: Path) -> tuple[Path, Path]:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    shutil.copytree(PROC_FIXTURE, proc_root, symlinks=True)
    shutil.copytree(CGROUP_FIXTURE, cgroup_root, symlinks=True)
    # Keep all observations under one app so the expected totals are direct.
    shutil.rmtree(proc_root / "500")
    return proc_root, cgroup_root


def write_io(root: Path, pid: int, values: tuple[int, int, int, int]) -> None:
    rchar, wchar, read_bytes, write_bytes = values
    (root / str(pid) / "io").write_text(
        "rchar: {rchar}\n"
        "wchar: {wchar}\n"
        "syscr: 0\n"
        "syscw: 0\n"
        "read_bytes: {read_bytes}\n"
        "write_bytes: {write_bytes}\n"
        "cancelled_write_bytes: 0\n".format(
            rchar=rchar,
            wchar=wchar,
            read_bytes=read_bytes,
            write_bytes=write_bytes,
        ),
        encoding="utf-8",
    )


def app_total(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
    row = connection.execute(
        "SELECT t.rchar,t.wchar,t.read_bytes,t.write_bytes "
        "FROM app_boot_total AS t JOIN app AS a USING(app_id) WHERE a.key=?",
        (APP_KEY,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_tick_against_fake_proc_tree_persists_expected_per_app_totals(tmp_path: Path) -> None:
    proc_root, cgroup_root = make_input_tree(tmp_path)
    for pid, values in {
        410: (100, 50, 10, 7),
        411: (20, 30, 3, 4),
        412: (5, 8, 2, 1),
        413: (1, 2, 0, 1),
    }.items():
        write_io(proc_root, pid, values)

    collector = LiveCollector(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        database_path=tmp_path / "history.sqlite",
        clock=SequenceClock((1_000_000_000, 1_700_000_000_000_000_000),
                            (2_000_000_000, 1_700_000_001_000_000_000)),
    )

    result = collector.tick()

    assert result.apps_observed == 1
    assert result.rows_written >= 1
    assert app_total(collector.store.connection) == (126, 90, 15, 13)
    assert result.coverage.inaccessible_pids == 0


def test_two_ticks_store_counter_deltas_once_without_cumulative_double_counting(
    tmp_path: Path,
) -> None:
    proc_root, cgroup_root = make_input_tree(tmp_path)
    initial = (100, 50, 10, 7)
    for pid in (410, 411, 412, 413):
        write_io(proc_root, pid, initial)

    collector = LiveCollector(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        database_path=tmp_path / "history.sqlite",
        clock=SequenceClock((1_000_000_000, 1_700_000_000_000_000_000),
                            (2_000_000_000, 1_700_000_001_000_000_000),
                            (3_000_000_000, 1_700_000_002_000_000_000)),
    )
    collector.tick()

    for pid in (410, 411, 412, 413):
        write_io(proc_root, pid, (initial[0] + 10, initial[1] + 4, initial[2] + 3, initial[3] + 2))
    collector.tick()

    # Four identical process deltas are added once; the first frame's baseline
    # remains part of the boot total and is not read again as a second delta.
    assert app_total(collector.store.connection) == (440, 216, 52, 36)
    sample_totals = dict(collector.store.connection.execute(
        "SELECT seq,sum(read_bytes) FROM sample GROUP BY seq"
    ).fetchall())
    assert sample_totals == {0: 40, 1: 12}


def test_pid_disappearance_does_not_reduce_boot_total(tmp_path: Path) -> None:
    proc_root, cgroup_root = make_input_tree(tmp_path)
    for pid in (410, 411, 412, 413):
        write_io(proc_root, pid, (10, 20, 30, 40))

    collector = LiveCollector(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        database_path=tmp_path / "history.sqlite",
        clock=SequenceClock((1_000_000_000, 1_700_000_000_000_000_000),
                            (2_000_000_000, 1_700_000_001_000_000_000),
                            (3_000_000_000, 1_700_000_002_000_000_000)),
    )
    collector.tick()
    before = app_total(collector.store.connection)
    shutil.rmtree(proc_root / "411")

    collector.tick()

    assert app_total(collector.store.connection) == before
    assert collector.store.connection.execute(
        "SELECT count(*) FROM sample_coverage"
    ).fetchone()[0] == 2


def test_eacces_is_recorded_as_inaccessible_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc_root, cgroup_root = make_input_tree(tmp_path)
    for pid in (410, 411, 412, 413):
        write_io(proc_root, pid, (10, 20, 30, 40))

    denied = proc_root / "410" / "io"
    original_read_text = Path.read_text

    def deny_one(path: Path, *args: object, **kwargs: object) -> str:
        if path == denied:
            raise PermissionError(13, "Permission denied", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_one)
    collector = LiveCollector(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        database_path=tmp_path / "history.sqlite",
        clock=SequenceClock((1_000_000_000, 1_700_000_000_000_000_000),
                            (2_000_000_000, 1_700_000_001_000_000_000)),
    )

    result = collector.tick()

    assert result.coverage.inaccessible_pids == 1
    row = collector.store.connection.execute(
        "SELECT coverage_json FROM sample_coverage ORDER BY app_id LIMIT 1"
    ).fetchone()
    assert row is not None
    assert '"inaccessible_pids":1' in row[0]
    assert app_total(collector.store.connection) == (30, 60, 90, 120)


def test_identical_input_and_clock_produce_identical_database_dump(tmp_path: Path) -> None:
    proc_root, cgroup_root = make_input_tree(tmp_path)
    for pid in (410, 411, 412, 413):
        write_io(proc_root, pid, (10, 20, 30, 40))

    def build(path: Path) -> LiveCollector:
        return LiveCollector(
            proc_root=proc_root,
            cgroup_root=cgroup_root,
            database_path=path,
            clock=SequenceClock((1_000_000_000, 1_700_000_000_000_000_000),
                                (2_000_000_000, 1_700_000_001_000_000_000),
                                (3_000_000_000, 1_700_000_002_000_000_000)),
        )

    first = build(tmp_path / "first.sqlite")
    second = build(tmp_path / "second.sqlite")
    for collector in (first, second):
        collector.tick()
        collector.tick()

    assert list(first.store.connection.iterdump()) == list(second.store.connection.iterdump())


def test_module_once_opens_database_and_writes_rows(tmp_path: Path) -> None:
    proc_root, cgroup_root = make_input_tree(tmp_path)
    database = tmp_path / "cli.sqlite"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "appio",
            "--once",
            "--db",
            str(database),
            "--proc-root",
            str(proc_root),
            "--cgroup-root",
            str(cgroup_root),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM boot").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM collector_epoch").fetchone()[0] == 1


def test_offscreen_window_against_real_query_service_renders_an_app_row(
    tmp_path: Path, qtbot
) -> None:
    proc_root, cgroup_root = make_input_tree(tmp_path)
    for pid in (410, 411, 412, 413):
        write_io(proc_root, pid, (10, 20, 30, 40))
    collector = LiveCollector(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        database_path=tmp_path / "history.sqlite",
        clock=SequenceClock((1_000_000_000, 1_700_000_000_000_000_000),
                            (2_000_000_000, 1_700_000_001_000_000_000)),
    )
    collector.tick()

    window = AppIoWindow(
        QueryService(collector.store), collector.boot_id, 10_000_000_000
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(
        lambda: window.findChild(QListWidget, "app_list").count() >= 1,
        timeout=1000,
    )
    assert window.findChild(QListWidget, "app_list").count() >= 1


def synthetic_file_collector(tmp_path, monkeypatch):
    """Only synthetic stable records; even enumeration of the fake tree is bypassed."""
    from appio.cgroups import CgroupObservation
    from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "boot_id").write_text(BOOT_ID)
    path = "/app-chromium@fixture.service"
    records = (StableProcRecord(ProcessIdentity(BOOT_ID, 1, 10), 0, 1000,
               "/bin/chromium", path, AppKeyInputs(path, "/bin/chromium", None),
               IoReadOutcome("readable", (0, 0, 0, 0), None, None)),)
    cgroups = (CgroupObservation(BOOT_ID, 77, path, "not_delegated", ()),)
    monkeypatch.setattr(LiveCollector, "_read_processes", lambda self, reader: (records, (), ()))
    monkeypatch.setattr(LiveCollector, "_read_cgroups", lambda self, records: cgroups)
    collector = LiveCollector(proc, tmp_path / "not-read", tmp_path / "db.sqlite",
                              SequenceClock(*((t, 1_000_000_000 + t) for t in (1, 100, 200, 300))),
                              source="iod")
    return collector


def test_file_context_publishes_only_after_commit_and_remains_immutable(tmp_path, monkeypatch):
    """Falsifier: publish staged context, guess from dev deltas, or mutate old context."""
    from dataclasses import FrozenInstanceError
    collector = synthetic_file_collector(tmp_path, monkeypatch)
    try:
        assert collector.file_context is None
        record_tick = collector.store.record_tick
        def guarded_commit(frame, state):
            assert collector.file_context is None
            record_tick(frame, state)
            assert collector.file_context is None
        monkeypatch.setattr(collector.store, "record_tick", guarded_commit)
        collector.tick()
        first = collector.file_context
        assert first is not None
        assert (first.boot_id, first.epoch, first.seq, first.observed_mono_ns) == (BOOT_ID, 1, 0, 100)
        assert first.app_keys == (APP_KEY,)
        assert first.owners_by_cgroup == ((77, (APP_KEY,)),)
        assert collector.last_frame.device_deltas == ()
        with pytest.raises(FrozenInstanceError):
            first.seq = 99
        with pytest.raises(AttributeError):
            collector.file_context = None
        collector.tick()
        assert collector.file_context is not first
        assert (collector.file_context.seq, collector.file_context.observed_mono_ns) == (1, 200)
        assert (first.seq, first.observed_mono_ns) == (0, 100)
    finally:
        collector.close()
    assert collector.file_context is None


def test_file_context_clears_on_failed_commit_and_reappears_on_retry(tmp_path, monkeypatch):
    """Falsifier: expose prior/current owners after a frame fails durable acceptance."""
    collector = synthetic_file_collector(tmp_path, monkeypatch)
    try:
        collector.tick()
        previous = collector.last_frame
        checkpoint = collector.store.connection.execute("SELECT * FROM sampler_checkpoint").fetchall()
        commit = collector.store.record_tick
        def reject(frame, state):
            assert collector.file_context is None
            raise sqlite3.OperationalError("synthetic commit rejection")
        monkeypatch.setattr(collector.store, "record_tick", reject)
        with pytest.raises(sqlite3.OperationalError, match="rejection"):
            collector.tick()
        assert collector.file_context is None
        assert collector.last_frame is previous and collector.next_seq == 1
        assert collector.store.connection.execute("SELECT * FROM sampler_checkpoint").fetchall() == checkpoint
        monkeypatch.setattr(collector.store, "record_tick", commit)
        collector.tick()
        assert (collector.file_context.seq, collector.file_context.observed_mono_ns) == (1, 300)
    finally:
        collector.close()


def test_file_context_clears_on_stale_frame_before_store_and_on_closed_tick(tmp_path, monkeypatch):
    """Falsifier: leave the last owner map usable when aggregate_tick rejects seq."""
    from appio.sampler import StaleFrameError
    collector = synthetic_file_collector(tmp_path, monkeypatch)
    try:
        collector.tick()
        assert collector.file_context is not None
        collector._next_seq = 0
        with pytest.raises(StaleFrameError):
            collector.tick()
        assert collector.file_context is None
    finally:
        collector.close()
    with pytest.raises(RuntimeError, match="closed"):
        collector.tick()
    assert collector.file_context is None


def test_restored_sampler_checkpoint_does_not_invent_a_file_context(tmp_path, monkeypatch):
    """Falsifier: reconstruct current cgroup owners from a durable sampler seed."""
    collector = synthetic_file_collector(tmp_path, monkeypatch)
    collector.tick()
    collector.close()
    reopened = LiveCollector(tmp_path / "proc", tmp_path / "not-read", tmp_path / "db.sqlite",
                             SequenceClock((400, 1_000_000_400), (500, 1_000_000_500)), source="iod")
    try:
        assert reopened.sampler_state is not None
        assert reopened.file_context is None
        reopened.tick()
        assert (reopened.file_context.epoch, reopened.file_context.seq) == (2, 0)
    finally:
        reopened.close()
