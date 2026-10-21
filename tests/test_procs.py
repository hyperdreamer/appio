"""Tests for fixture ``/proc`` parsing and stable process identity.

Every test copies the checked-in fixture tree into a temporary directory. No test
opens the host ``/proc``, requires root, or depends on a live session.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from appio.procs import (
    AppKeyInputs,
    IoReadOutcome,
    ProcReader,
    ProcessIdentity,
    ProcessStat,
    StableProcRecord,
    parse_io,
    parse_stat,
    parse_status_uid,
)

FIXTURE = Path(__file__).parent / "fixtures" / "proc"
BOOT_ID = "11111111-2222-4333-8444-555555555555"
CGROUP_CHROMIUM = (
    "/user.slice/user-1000.slice/user@1000.service/app.slice/app-chromium@fixture.service"
)

IO_LINES = (
    "rchar: {rchar}\n"
    "wchar: {wchar}\n"
    "syscr: 7\n"
    "syscw: 8\n"
    "read_bytes: {read_bytes}\n"
    "write_bytes: {write_bytes}\n"
    "cancelled_write_bytes: 0\n"
)


def make_tree(tmp_path: Path) -> Path:
    """Copy the fixture tree so permission and mutation tests cannot leak."""

    root = tmp_path / "proc"
    shutil.copytree(FIXTURE, root, symlinks=True)
    return root


# --- enumeration -----------------------------------------------------------


def test_enumerate_pids_returns_only_positive_numeric_directories_ascending(
    tmp_path: Path,
) -> None:
    reader = ProcReader(make_tree(tmp_path))

    assert reader.enumerate_pids() == (410, 411, 412, 413, 500)


# --- stat ------------------------------------------------------------------


def test_parse_stat_extracts_field_22_and_the_parent_pid() -> None:
    text = "77 (a) (b) R 42 77 77 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 555 0\n"

    stat = parse_stat(text)

    assert stat == ProcessStat(pid=77, comm="a) (b", state="R", ppid=42, start_ticks=555)


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing at all
        "410 app S 1 410 410 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100 0\n",  # no comm parens
        "0 (app) S 1 0 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100 0\n",  # pid 0
        "-1 (app) S 1 -1 -1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100 0\n",  # negative pid
        "410 (app) S -1 410 410 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100 0\n",  # ppid
        "410 (app) S 1 410 410 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 -5 0\n",  # start
        "410 (app) S 1 410 410 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 nope 0\n",  # start
        "410 (app) S 1 410 410 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0\n",  # truncated
    ],
)
def test_parse_stat_rejects_malformed_or_out_of_range_fields(text: str) -> None:
    with pytest.raises(ValueError):
        parse_stat(text)


# --- io --------------------------------------------------------------------


def test_parse_io_returns_rchar_wchar_read_and_write_bytes_in_order() -> None:
    text = IO_LINES.format(rchar=1000, wchar=500, read_bytes=100, write_bytes=70)

    assert parse_io(text) == (1000, 500, 100, 70)


def _io_text(**overrides: str) -> str:
    values = {"rchar": "10", "wchar": "20", "read_bytes": "3", "write_bytes": "4"}
    values.update(overrides)
    return IO_LINES.format(**values)


@pytest.mark.parametrize(
    "text",
    [
        _io_text().replace("write_bytes: 4\n", ""),  # missing counter
        _io_text() + "rchar: 11\n",  # duplicate counter
        _io_text(read_bytes="-3"),  # negative
        _io_text(read_bytes="true"),  # boolean-like
        _io_text(read_bytes="False"),  # boolean-like
        _io_text(rchar="1x"),  # malformed
        _io_text(rchar="+10"),  # malformed sign
        _io_text(rchar=""),  # empty value
        _io_text() + "device: 1\n",  # unknown kernel line
    ],
)
def test_parse_io_rejects_missing_duplicate_negative_boolean_and_malformed(
    text: str,
) -> None:
    with pytest.raises(ValueError):
        parse_io(text)


# --- status ----------------------------------------------------------------


def test_parse_status_uid_returns_the_real_uid() -> None:
    text = "Name: fixture\nUid: 1000 1001 1002 1003\nGid: 1000 1000 1000 1000\n"

    assert parse_status_uid(text) == 1000


@pytest.mark.parametrize(
    "text",
    [
        "Name: fixture\n",  # no Uid
        "Uid: 1000 1000 1000 1000\nUid: 0 0 0 0\n",  # duplicate
        "Uid: 1000 1000 nope 1000\n",  # malformed field
        "Uid: true 1000 1000 1000\n",  # boolean-like
        "Uid: -1 1000 1000 1000\n",  # negative
        "Uid:\n",  # empty
    ],
)
def test_parse_status_uid_rejects_missing_duplicate_and_malformed(text: str) -> None:
    with pytest.raises(ValueError):
        parse_status_uid(text)


# --- io outcomes and dumpability metadata ----------------------------------


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses file permission bits, so EACCES cannot be provoked",
)
def test_read_io_eacces_is_reported_as_a_permission_fact_and_no_counters(
    tmp_path: Path,
) -> None:
    root = make_tree(tmp_path)
    (root / "413" / "io").chmod(0o000)
    reader = ProcReader(root, dumpability=lambda pid: False)

    outcome = reader.read_io(413)

    assert outcome == IoReadOutcome(
        status="eacces",
        counters=None,
        dumpable=False,
        errno_name="EACCES",
    )
    # The permission fact is a status plus an errno name; dumpability is a
    # separate metadata field and never substitutes for them.
    assert outcome.status == "eacces"
    assert outcome.errno_name == "EACCES"


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses file permission bits, so EACCES cannot be provoked",
)
@pytest.mark.parametrize("dumpable", [None, True])
def test_eacces_stays_a_permission_fact_when_dumpability_is_unknown_or_true(
    tmp_path: Path, dumpable: bool | None
) -> None:
    root = make_tree(tmp_path)
    (root / "413" / "io").chmod(0o000)
    reader = ProcReader(root, dumpability=lambda pid: dumpable)

    outcome = reader.read_io(413)

    assert outcome.status == "eacces"
    assert outcome.counters is None
    assert outcome.dumpable is dumpable
    assert outcome.errno_name == "EACCES"


def test_read_io_classifies_permission_error_even_without_file_mode_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin EACCES classification independently of the process's file mode."""

    root = make_tree(tmp_path)
    denied = root / "413" / "io"
    real_open = Path.open

    def denying_open(self: Path, *args: object, **kwargs: object):
        if self == denied:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denying_open)
    reader = ProcReader(root, dumpability=lambda pid: False)

    outcome = reader.read_io(413)

    assert outcome.status == "eacces"
    assert outcome.counters is None
    assert outcome.dumpable is False
    assert outcome.errno_name == "EACCES"


def test_readable_io_keeps_dumpability_metadata_independent(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    reader = ProcReader(root, dumpability=lambda pid: False)

    outcome = reader.read_io(410)

    assert outcome.status == "readable"
    assert outcome.counters == (0, 0, 0, 0)
    assert outcome.dumpable is False
    assert outcome.errno_name is None


# --- stable logical record -------------------------------------------------


def test_read_stable_returns_identity_parent_uid_exe_cgroup_and_counters(
    tmp_path: Path,
) -> None:
    root = make_tree(tmp_path)
    reader = ProcReader(root, dumpability=lambda pid: False)

    record = reader.read_stable(410)

    assert isinstance(record, StableProcRecord)
    assert record.identity == ProcessIdentity(boot_id=BOOT_ID, pid=410, start_ticks=100)
    assert record.ppid == 1
    assert record.uid == 1000
    assert record.exe == "/usr/lib/chromium/chromium"
    assert record.cgroup_path == CGROUP_CHROMIUM
    assert record.app_key_inputs == AppKeyInputs(
        cgroup_path=CGROUP_CHROMIUM,
        exe="/usr/lib/chromium/chromium",
        desktop_id=None,
    )
    assert record.io_outcome.status == "readable"
    assert record.io_outcome.counters == (0, 0, 0, 0)
    assert record.io_outcome.dumpable is False


def test_boot_id_can_be_supplied_instead_of_the_fixture_file(tmp_path: Path) -> None:
    reader = ProcReader(make_tree(tmp_path), boot_id="supplied-boot")

    record = reader.read_stable(410)

    assert record is not None
    assert record.identity.boot_id == "supplied-boot"


def test_read_stable_preserves_the_exe_target_without_resolving_another_layer(
    tmp_path: Path,
) -> None:
    root = make_tree(tmp_path)
    link2 = root / "second-layer"
    link2.symlink_to("/usr/bin/real-target")
    exe = root / "410" / "exe"
    exe.unlink()
    exe.symlink_to(link2)
    reader = ProcReader(root)

    record = reader.read_stable(410)

    assert record is not None
    assert record.exe == str(link2)
    assert record.exe != "/usr/bin/real-target"


@pytest.mark.parametrize("relative", ["io", "status", "cgroup", "exe"])
def test_enoent_during_any_logical_read_is_an_esrch_outcome(
    tmp_path: Path, relative: str
) -> None:
    root = make_tree(tmp_path)
    (root / "410" / relative).unlink()
    reader = ProcReader(root)

    record = reader.read_stable(410)

    assert record is not None
    assert record.identity == ProcessIdentity(boot_id=BOOT_ID, pid=410, start_ticks=100)
    assert record.io_outcome.status == "esrsh"
    assert record.io_outcome.counters is None
    assert record.io_outcome.errno_name == "ENOENT"


def test_start_time_change_between_stat_reads_returns_no_accepted_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = ProcReader(make_tree(tmp_path))
    real_read_stat = reader.read_stat
    calls = [0]

    def changing_stat(pid: int) -> ProcessStat:
        calls[0] += 1
        stat = real_read_stat(pid)
        if calls[0] == 1:
            return stat
        return ProcessStat(
            pid=stat.pid,
            comm=stat.comm,
            state=stat.state,
            ppid=stat.ppid,
            start_ticks=stat.start_ticks + 1,
        )

    monkeypatch.setattr(reader, "read_stat", changing_stat)

    assert reader.read_stable(410) is None
    assert calls[0] == 2


def test_disappearance_before_the_first_stat_read_has_no_identity(
    tmp_path: Path,
) -> None:
    reader = ProcReader(make_tree(tmp_path))

    assert reader.read_stable(9999) is None


def test_disappearance_between_stat_reads_is_esrch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_tree(tmp_path)
    reader = ProcReader(root)
    real_read_stat = reader.read_stat
    calls = [0]

    def vanishing_stat(pid: int) -> ProcessStat:
        calls[0] += 1
        if calls[0] == 1:
            return real_read_stat(pid)
        raise FileNotFoundError(2, "vanished", str(root / "410" / "stat"))

    monkeypatch.setattr(reader, "read_stat", vanishing_stat)

    record = reader.read_stable(410)

    assert record is not None
    assert record.identity == ProcessIdentity(boot_id=BOOT_ID, pid=410, start_ticks=100)
    assert record.io_outcome.status == "esrsh"
    assert calls[0] == 2


def test_malformed_io_content_is_an_error_outcome(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "410" / "io").write_text("rchar: 1\n", encoding="utf-8")
    reader = ProcReader(root)

    record = reader.read_stable(410)

    assert record is not None
    assert record.io_outcome.status == "error"
    assert record.io_outcome.counters is None


def test_malformed_status_content_is_an_error_outcome(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "410" / "status").write_text("Name: fixture\n", encoding="utf-8")
    reader = ProcReader(root)

    record = reader.read_stable(410)

    assert record is not None
    assert record.io_outcome.status == "error"
    assert record.uid is None


def test_undecodable_io_bytes_are_an_error_outcome(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "410" / "io").write_bytes(b"\xff\xfe\x00")
    reader = ProcReader(root)

    record = reader.read_stable(410)

    assert record is not None
    assert record.io_outcome.status == "error"
    assert record.io_outcome.counters is None


def test_unreadable_status_metadata_is_an_error_not_esrch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_tree(tmp_path)
    denied = root / "410" / "status"
    real_open = Path.open

    def denying_open(self: Path, *args: object, **kwargs: object):
        if self == denied:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denying_open)
    reader = ProcReader(root)

    record = reader.read_stable(410)

    assert record is not None
    assert record.io_outcome.status == "error"
    assert record.io_outcome.counters is None
    assert record.io_outcome.errno_name == "EACCES"
    assert record.uid is None
