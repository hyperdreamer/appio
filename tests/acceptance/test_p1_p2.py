"""The spec §10.1–10.2 trees through the real P1 modules and P2 window.

Only the OS read of metadata-declared EACCES files is fault injected. Readers,
identity resolution, aggregation, SQLite storage, queries and widgets are real.
The since-boot gate deliberately retains the projection's exact expectations;
unsupported expectations must fail rather than be hidden with xfail or a stub.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import io
import json
import os
from pathlib import Path
import shutil

import pytest
from pytestqt.qt_compat import qt_api

# The host may have both bindings; the runtime contract requires PyQt6.
if getattr(qt_api, "pytest_qt_api", None) != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QListWidget, QPushButton

from appio.cgroups import read_cgroup_fixture
from appio.contracts import MAX_RANGE_NS, Window, WindowUnavailableError
from appio.identity import resolve_app
from appio.procs import ProcReader
from appio.sampler import StaleFrameError, TickInput, aggregate_tick
from appio.store import AppIoStore
from appio.query import QueryService
from appio.ui.main_window import AppIoWindow

BOOT = "11111111-2222-4333-8444-555555555555"
CHROMIUM = "unit:app-chromium@fixture.service"
REUSED = "unit:app-pidreuse.scope"
NS = 1_000_000_000
T0, T1, T2 = (100 * NS, 110 * NS, 120 * NS)
# Include the entire bucket containing t2. Ending at t2 excludes t2 by A10.
SINCE_BOOT = Window("since_boot", BOOT, end_mono_ns=130 * NS)
LAUNCH_REASON = "Launch window disabled: device and file activity are not stored per launch."
RANGE_REASON = (
    "Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges."
)


@dataclass
class FixtureIO:
    root: Path
    metadata: dict
    reads: list[Path]
    denied: list[Path]

    def tick(self, index, *, variant="normal", epoch=1, seq=None, known_uncovered=()):
        proc = self.root / "proc"
        if variant.startswith("shared-"):
            proc /= variant
        proc /= f"t{index}"
        boot_file = proc / "sys/kernel/random/boot_id"
        assert boot_file.read_bytes() == (BOOT + "\n").encode()
        reader = ProcReader(
            proc,
            dumpability=lambda pid: self.metadata["dumpability"].get(str(pid), True),
            boot_id=boot_file.read_text().strip(),
        )
        records = tuple(reader.read_stable(pid) for pid in reader.enumerate_pids())
        assert records and all(record is not None for record in records)
        if variant in ("normal", "not-delegated"):
            blocked = next(record for record in records if record.identity.pid == 413)
            assert (blocked.uid, blocked.io_outcome.status, blocked.io_outcome.counters,
                    blocked.io_outcome.dumpable, blocked.io_outcome.errno_name) == (
                        1000, "eacces", None, False, "EACCES")
        cgroup_root = self.root / "cgroup"
        if variant != "normal":
            cgroup_root /= variant
        cgroup_root /= f"t{index}"
        groups = []
        for path in sorted({record.cgroup_path for record in records}):
            leaf = cgroup_root / path.lstrip("/")
            members = tuple(map(int, (leaf / "cgroup.procs").read_text().split()))
            assert members == tuple(record.identity.pid for record in records
                                    if record.cgroup_path == path)
            groups.append(read_cgroup_fixture(
                cgroup_root, path, BOOT, self.metadata["cgroup_ids"][path]))
        timestamp = self.metadata["observed_mono_ns"][index]
        return TickInput(
            BOOT, "gui", epoch, index if seq is None else seq, timestamp,
            timestamp + self.metadata["wall_offset_ns"], records, tuple(groups),
            known_uncovered=known_uncovered,
        )


@pytest.fixture
def fixture_io(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / "fixtures/acceptance"
    assert (source / "metadata.json").is_file(), "materialize the spec acceptance trees first"
    root = tmp_path / "fixture"
    shutil.copytree(source, root, symlinks=True)
    metadata = json.loads((root / "metadata.json").read_text())
    assert metadata["clk_tck"] == 100
    assert metadata["observed_mono_ns"] == [T0, T1, T2]
    assert metadata["wall_offset_ns"] == 1000 * NS
    reads, denied = [], []
    denied_paths = {root / path for path in metadata["eacces_io"]}

    def guard(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            value = Path(os.fsdecode(path)).absolute()
            assert not (value == Path("/proc") or Path("/proc") in value.parents
                        or value == Path("/sys/fs/cgroup")
                        or Path("/sys/fs/cgroup") in value.parents), value
            reads.append(value)
            return value
        return None  # file descriptors are not process/cgroup paths

    def guarded_open(original):
        def open_file(file, *args, **kwargs):
            path = guard(file)
            if path in denied_paths:
                denied.append(path)
                raise PermissionError(errno.EACCES, "fixture EACCES metadata", str(path))
            return original(file, *args, **kwargs)
        return open_file

    real_scandir, real_readlink = os.scandir, os.readlink

    def scandir(path="."):
        guard(path)
        return real_scandir(path)

    def readlink(path, *args, **kwargs):
        guard(path)
        return real_readlink(path, *args, **kwargs)

    # Inject an OS failure, not a fabricated ProcReader/IoReadOutcome result.
    monkeypatch.setattr(io, "open", guarded_open(io.open))
    monkeypatch.setattr(builtins, "open", guarded_open(builtins.open))
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "readlink", readlink)
    return FixtureIO(root, metadata, reads, denied)


@dataclass
class Collected:
    store: AppIoStore
    ticks: list
    frames: list

    @property
    def service(self):
        return QueryService(self.store)

    def app_id(self, key=CHROMIUM):
        return self.store.connection.execute("SELECT app_id FROM app WHERE key=?", (key,)).fetchone()[0]


@contextmanager
def collect_fixture(fixture_io, database, *, variant="normal"):
    store = AppIoStore(database)
    try:
        store.start_boot(BOOT, 1, 0, 1000.0, "gui")
        allocation = store.start_collector("gui", BOOT, T0)
        assert allocation.epoch == 1
        ticks, frames = [], []
        for index in range(2 if variant.startswith("shared-") else 3):
            tick = fixture_io.tick(index, variant=variant)
            frame, state = aggregate_tick(store.sampler_state, tick)
            store.record_tick(frame, state)
            ticks.append(tick)
            frames.append(frame)
        yield Collected(store, ticks, frames)
    finally:
        store.connection.close()


@pytest.fixture
def collected(fixture_io, tmp_path):
    with collect_fixture(fixture_io, tmp_path / "acceptance.sqlite") as result:
        yield result


def snapshot(db):
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name")]
    return {table: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')), key=repr)
            for table in tables}


def label(window, name):
    result = window.findChild(QLabel, name)
    assert result is not None, name
    return result.text()


def show_chromium(qtbot, result):
    window = AppIoWindow(result.service, BOOT, SINCE_BOOT.end_mono_ns)
    qtbot.addWidget(window)
    window.show()
    listing = window.findChild(QListWidget, "app_list")
    assert listing is not None
    for index in range(listing.count()):
        if listing.item(index).data(Qt.ItemDataRole.UserRole) == result.app_id():
            listing.setCurrentRow(index)
            break
    assert window.selected_app_id() == result.app_id()
    return window


def test_static_fixture_drives_exact_storage_and_coverage(collected, fixture_io):
    """Hand oracle: 180+20 reads, 90+10 writes; 411's credit is spent once."""
    db, aid = collected.store.connection, collected.app_id()
    assert [(tick.epoch, tick.seq) for tick in collected.ticks] == [(1, 0), (1, 1), (1, 2)]
    for tick in collected.ticks:
        assert all(resolve_app(record.app_key_inputs.cgroup_path,
                               record.app_key_inputs.exe, record.app_key_inputs.desktop_id) == CHROMIUM
                   for record in tick.processes if record.identity.pid in (410, 411, 412, 413))
    for table in ("app_boot_total", "app_lifetime_total"):
        assert tuple(db.execute(
            f"SELECT rchar,wchar,read_bytes,write_bytes FROM {table} WHERE app_id=?",
            (aid,)).fetchone()) == (1900, 850, 200, 100)
    assert tuple(db.execute(
        "SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM app_boot_total WHERE app_id=?",
        (aid,)).fetchone()) == (18, 9, 27)
    assert [tuple(row) for row in db.execute(
        "SELECT seq,sum(rchar),sum(wchar),sum(read_bytes),sum(write_bytes) FROM sample "
        "WHERE app_id=? GROUP BY seq ORDER BY seq", (aid,))] == [
            (0, 0, 0, 0, 0), (1, 1800, 800, 180, 90), (2, 100, 50, 20, 10)]
    assert [tuple(row) for row in db.execute(
        "SELECT dev,rbytes,wbytes FROM app_boot_dev_total WHERE app_id=? ORDER BY dev", (aid,))] == [
            ("259:0", 120, 70), ("259:1", 50, 50)]
    coverages = [json.loads(row[0]) for row in db.execute(
        "SELECT coverage_json FROM sample_coverage WHERE app_id=? ORDER BY seq", (aid,))]
    assert [row["inaccessible_pids"] for row in coverages] == [1, 1, 1]
    assert [row["reap_corrections"] for row in coverages] == [0, 0, 1]
    assert [row["resets"] for row in coverages] == [0, 0, 0]
    assert all(row["partial_intervals"] == 0 and row["device_state"] == "available"
               and not any(row["reap_unapplied"].values()) for row in coverages)
    assert tuple(db.execute("SELECT read_bytes,write_bytes FROM app_boot_total WHERE app_id=?",
                            (collected.app_id(REUSED),)).fetchone()) == (25, 0)
    assert all(all(value >= 0 for value in delta.counters)
               for frame in collected.frames for delta in frame.deltas)
    reused = next(p for p in collected.store.sampler_state.processes if p.identity.pid == 500)
    assert (reused.identity.start_ticks, reused.baseline) == (900, (0, 0, 0, 0))
    assert not any(row[0] for row in db.execute("SELECT first_seen_partial FROM sample"))
    for width in (60, 3600):
        assert tuple(db.execute("SELECT sum(read_bytes),sum(write_bytes) FROM rollup_app "
                                "WHERE app_id=? AND bucket_secs=?", (aid, width)).fetchone()) == (200, 100)
        assert tuple(db.execute("SELECT sum(rbytes),sum(wbytes) FROM rollup_dev "
                                "WHERE app_id=? AND bucket_secs=?", (aid, width)).fetchone()) == (170, 120)
    assert [tuple(row) for row in db.execute("SELECT segment_start_mono_ns,wall_offset_ns FROM clock_map")] == [(0, 1000 * NS)]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(fixture_io.denied) == 3
    assert any(fixture_io.root / "proc" in path.parents for path in fixture_io.reads)
    assert any(fixture_io.root / "cgroup" in path.parents for path in fixture_io.reads)


def test_fixture_to_since_boot_query_and_screen(collected, qtbot):
    """T-012 gate: the three frames must agree in storage, query and actual UI."""
    total = next(row for row in collected.service.ranking(SINCE_BOOT, "total_bytes")
                 if row.app_key == CHROMIUM)
    share = collected.service.disk_share(collected.app_id(), SINCE_BOOT)
    window = show_chromium(qtbot, collected)
    db, aid = collected.store.connection, collected.app_id()
    observed = {
        "stored_task": tuple(db.execute(
            "SELECT read_bytes,write_bytes,read_bytes+write_bytes FROM app_boot_total WHERE app_id=?",
            (aid,)).fetchone()),
        "stored_device": tuple(db.execute(
            "SELECT sum(rbytes),sum(wbytes),sum(rbytes+wbytes) FROM app_boot_dev_total WHERE app_id=?",
            (aid,)).fetchone()),
        "task": (total.read_bytes, total.write_bytes, total.total_bytes),
        "device": (share.device_read_bytes, share.device_write_bytes, share.device_total_bytes),
        "residual": (share.residual_read, share.residual_write, share.residual_total),
        "coverage_counts": (total.coverage.inaccessible_pids, total.coverage.reap_corrections),
        "screen_total": label(window, "total_value"),
        "screen_read": label(window, "read_value"),
        "screen_write": label(window, "write_value"),
        "screen_device": label(window, "device_value"),
        "screen_residual": label(window, "residual_value"),
    }
    # Ratified policy (PM ruling, 2026-10-21): this fixture's since_boot window is [0, 130 s)
    # while the collector only owns time from T0 = 100 s, so 100 s of the requested window was
    # never observed. Per spec 4.2 (since_boot starts at boot zero), 8.2 "a required missing
    # interval makes Coverage.complete false" and 8.2 "an incomplete/unavailable D result sets
    # device totals and all residuals to None", the correct result is UNAVAILABLE device data -
    # NOT the stored 290. The store is still required to hold the truth (stored_device below),
    # and the UI is still required to say so honestly rather than print 0.
    assert observed == {
        "stored_task": (200, 100, 300), "stored_device": (170, 120, 290),
        "task": (200, 100, 300), "device": (None, None, None), "residual": (None, None, None),
        "coverage_counts": (3, 1), "screen_total": "300 B", "screen_read": "200 B",
        "screen_write": "100 B", "screen_device": "unavailable",
        "screen_residual": "Residual: unavailable - difference between accounting points",
    }
    # The incompleteness must be disclosed, not merely implied by a missing number.
    assert total.coverage.complete is False
    assert share.coverage.complete is False


@pytest.mark.parametrize("component,task,device,residual", [
    ("total", 300, 290, 10), ("read", 200, 170, 30), ("write", 100, 120, -20),
])
def test_reconciliation_identities_in_exact_stored_scope(collected, component, task, device, residual):
    """The explicit lifetime scope reduces these same committed frames, without a fabricated tail."""
    share = collected.service.disk_share(collected.app_id(), Window("lifetime"))
    expected_task = getattr(share, f"task_{component}_bytes")
    expected_device = getattr(share, f"device_{component}_bytes")
    expected_residual = getattr(share, f"residual_{component}")
    assert (expected_task, expected_device, expected_residual) == (task, device, residual)
    field = {"total": "total_bytes", "read": "rbytes", "write": "wbytes"}[component]
    assert sum(getattr(row, field) for row in share.devices) + expected_residual == expected_task
    assert share.coverage.inaccessible_pids == 3
    assert share.coverage.reap_corrections == 1
    assert not share.coverage.complete


def test_duplicate_replay_changes_no_rows_baselines_or_screen(collected, qtbot):
    window = show_chromium(qtbot, collected)
    before = snapshot(collected.store.connection)
    state = collected.store.sampler_state
    screen = [label(window, name) for name in ("total_value", "device_value", "residual_value", "coverage_label")]
    with pytest.raises(StaleFrameError):
        aggregate_tick(state, collected.ticks[-1])
    with pytest.raises(StaleFrameError):
        collected.store.record_tick(collected.frames[-1], state)
    assert snapshot(collected.store.connection) == before
    assert collected.store.sampler_state is state
    window.refresh()
    assert [label(window, name) for name in ("total_value", "device_value", "residual_value", "coverage_label")] == screen


def test_restart_mid_fixture_restores_baselines_credits_devices_and_records_gap(fixture_io, tmp_path):
    database = tmp_path / "restart.sqlite"
    store = AppIoStore(database)
    try:
        store.start_boot(BOOT, 1, 0, 1000.0, "gui")
        store.start_collector("gui", BOOT, T0)
        for index in (0, 1):
            frame, state = aggregate_tick(store.sampler_state, fixture_io.tick(index))
            store.record_tick(frame, state)
        checkpoint = store.sampler_state.to_checkpoint()
    finally:
        store.connection.close()
    # New connection and store: no old in-memory state is fed to the sampler.
    restarted = AppIoStore(database)
    try:
        epoch = restarted.start_collector("gui", BOOT, 115 * NS)
        assert epoch.epoch == 2
        restored = restarted.restore_checkpoint("gui", BOOT)
        assert restored is not None and restored.to_checkpoint() == checkpoint
        frame, state = aggregate_tick(restored, fixture_io.tick(
            2, epoch=epoch.epoch, seq=0, known_uncovered=((T1, 115 * NS),)))
        assert sum(d.counters[2] for d in frame.deltas if d.app_key == CHROMIUM) == 20
        assert sum(d.counters[3] for d in frame.deltas if d.app_key == CHROMIUM) == 10
        restarted.record_tick(frame, state)
        db = restarted.connection
        aid = db.execute("SELECT app_id FROM app WHERE key=?", (CHROMIUM,)).fetchone()[0]
        assert tuple(db.execute("SELECT read_bytes,write_bytes FROM app_boot_total WHERE app_id=?", (aid,)).fetchone()) == (200, 100)
        assert tuple(db.execute("SELECT sum(rbytes),sum(wbytes) FROM app_boot_dev_total WHERE app_id=?", (aid,)).fetchone()) == (170, 120)
        assert [tuple(row) for row in db.execute(
            "SELECT from_mono_ns,to_mono_ns,reason FROM gap")] == [(T1, 115 * NS, "collector_gap")]
        share = QueryService(restarted).disk_share(aid, SINCE_BOOT)
        assert share.task_total_bytes == 300
        assert share.coverage.gap_intervals > 0 and not share.coverage.complete
        assert share.device_total_bytes is None and share.residual_total is None
        before = snapshot(db)
        with pytest.raises(StaleFrameError):
            restarted.record_tick(frame, state)
        assert snapshot(db) == before
    finally:
        restarted.connection.close()


@pytest.mark.parametrize("variant,owners,expected", [
    ("shared-unit", ("unit:app-shared.scope",), [("259:0", 40, 5)]),
    ("shared-distinct", ("exe:/usr/bin/tool-a", "exe:/usr/bin/tool-b"), []),
])
def test_shared_cgroup_is_attributed_once_or_marked_ambiguous(fixture_io, tmp_path, qtbot, variant, owners, expected):
    with collect_fixture(fixture_io, tmp_path / "shared.sqlite", variant=variant) as result:
        db = result.store.connection
        assert [tuple(row) for row in db.execute(
            "SELECT dev,rbytes,wbytes FROM sample_dev WHERE seq=1 ORDER BY dev")] == expected
        assert [tuple(row) for row in db.execute(
            "SELECT dev,rbytes,wbytes FROM app_boot_dev_total WHERE rbytes!=0 OR wbytes!=0 ORDER BY dev")] == expected
        for owner in owners:
            share = result.service.disk_share(result.app_id(owner), Window("lifetime"))
            if variant == "shared-unit":
                assert (share.device_read_bytes, share.device_write_bytes, share.device_total_bytes) == (40, 5, 45)
                assert share.residual_total == -45
                window = AppIoWindow(result.service, BOOT, 120 * NS)
                qtbot.addWidget(window)
                window.set_window(Window("lifetime"))
                window.show()
                assert label(window, "residual_value") == (
                    "Residual: attributed exceeds task accounting by 45 B - difference between accounting points")
            else:
                assert share.coverage.device_state == "incomplete"
                assert share.devices == ()
                assert share.device_total_bytes is None and share.residual_total is None


def test_unavailable_device_coverage_remains_none_and_renders_unavailable(fixture_io, tmp_path, qtbot):
    with collect_fixture(fixture_io, tmp_path / "not-delegated.sqlite", variant="not-delegated") as result:
        assert result.store.connection.execute("SELECT count(*) FROM sample_dev").fetchone()[0] == 0
        assert result.store.connection.execute("SELECT count(*) FROM app_boot_dev_total").fetchone()[0] == 0
        assert all(coverage.device_state == "not_delegated" for frame in result.frames for coverage in frame.coverage)
        for scope in (SINCE_BOOT, Window("lifetime")):
            share = result.service.disk_share(result.app_id(), scope)
            assert (share.task_read_bytes, share.task_write_bytes, share.task_total_bytes) == (200, 100, 300)
            assert (share.device_read_bytes, share.device_write_bytes, share.device_total_bytes,
                    share.residual_read, share.residual_write, share.residual_total) == (None,) * 6
            if scope.kind == "lifetime":
                assert share.coverage.device_state == "not_delegated"
        window = show_chromium(qtbot, result)
        assert label(window, "total_value") == "300 B"
        assert label(window, "device_value") == "unavailable"
        assert label(window, "residual_value") == "Residual: unavailable - difference between accounting points"


@pytest.mark.parametrize("kind,reason,message,control_name", [
    ("launch", "launch_attribution", LAUNCH_REASON, "scope_launch"),
    ("range", "unsupported_window", RANGE_REASON, "scope_range"),
])
def test_disabled_windows_raise_exact_a22_errors_and_disable_controls(collected, qtbot, kind, reason, message, control_name):
    aid = collected.app_id()
    launch_id = collected.store.connection.execute("SELECT launch_id FROM launch WHERE app_id=?", (aid,)).fetchone()[0]
    scope = (Window("launch", BOOT, launch_id=launch_id) if kind == "launch"
             else Window("range", BOOT, 0, MAX_RANGE_NS + 1))
    service = collected.service
    queries = (
        lambda: service.ranking(scope, "total_bytes"), lambda: service.series(aid, scope),
        lambda: service.disk_share(aid, scope),
        lambda: service.file_table(aid, scope, "bytes_total", "desc", None),
        lambda: service.file_coverage(aid, scope), lambda: service.unknown_owner_file_coverage(scope),
    )
    before = snapshot(collected.store.connection)
    for query in queries:
        with pytest.raises(WindowUnavailableError) as error:
            query()
        assert (error.value.reason, str(error.value)) == (reason, message)
    window = show_chromium(qtbot, collected)
    window.set_window(scope)
    control = window.findChild(QPushButton, control_name)
    assert control is not None and not control.isEnabled()
    assert control.toolTip() == message and control.accessibleDescription() == message
    assert label(window, "detail_state") == message
    assert label(window, "total_value") == "unavailable"
    assert snapshot(collected.store.connection) == before
