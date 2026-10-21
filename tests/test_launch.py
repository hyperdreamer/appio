from __future__ import annotations

from appio.launch import LaunchIdentity, assign_launches
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord

BOOT = "boot-A"
APP_PATH = "/user.slice/app.slice/app-demo.service"
OTHER_PATH = "/user.slice/app.slice/app-other.service"


def proc(pid: int, ppid: int, start: int, path: str = APP_PATH) -> StableProcRecord:
    return StableProcRecord(
        identity=ProcessIdentity(BOOT, pid, start),
        ppid=ppid,
        uid=1000,
        exe="/usr/bin/demo",
        cgroup_path=path,
        app_key_inputs=AppKeyInputs(path, "/usr/bin/demo"),
        io_outcome=IoReadOutcome("readable", (0, 0, 0, 0), None, None),
    )


def test_root_and_same_app_descendants_share_one_launch() -> None:
    """Falsified if a same-AppKey child becomes a second launch."""
    records = [proc(10, 1, 1), proc(11, 10, 2), proc(12, 11, 3)]
    assignments, launches = assign_launches(records)
    root = assignments[records[0].identity]
    assert assignments[records[1].identity] == root
    assert assignments[records[2].identity] == root
    assert root == LaunchIdentity(records[0].identity, "unit:app-demo.service")
    assert len(launches) == 1
    assert launches[0].members == tuple(record.identity for record in records)


def test_concurrent_roots_and_app_key_changes_open_separate_launches() -> None:
    """Falsified if roots are merged by app or a changed AppKey inherits a launch."""
    records = [
        proc(20, 1, 1),
        proc(21, 1, 2),
        proc(22, 20, 3, OTHER_PATH),
        proc(23, 20, 4),
    ]
    assignments, launches = assign_launches(records)
    assert assignments[records[0].identity] != assignments[records[1].identity]
    assert assignments[records[2].identity].root == records[2].identity
    assert assignments[records[3].identity] == assignments[records[0].identity]
    assert len(launches) == 3


def test_unknown_parent_membership_uses_launch_zero() -> None:
    """Falsified if an unobserved parent is guessed into a local launch."""
    record = proc(30, 999, 1)
    assignments, launches = assign_launches([record])
    assert assignments[record.identity] == LaunchIdentity(None, "unit:app-demo.service")
    assert assignments[record.identity].launch_id == 0
    assert launches == ()


def test_launch_remains_open_until_last_known_member_disappears() -> None:
    """Falsified if root exit closes a launch while a known descendant remains."""
    root, child = proc(40, 1, 1), proc(41, 40, 2)
    assignments, launches = assign_launches([root, child])
    identity = assignments[root.identity]
    after_root, launches_after_root = assign_launches([child], previous_launches=launches)
    assert after_root[child.identity] == identity
    assert len(launches_after_root) == 1
    assert launches_after_root[0].members == (child.identity,)

    _, after_last = assign_launches([], previous_launches=launches_after_root)
    assert after_last == ()


def test_launch_identity_includes_process_start_ticks() -> None:
    """Falsified if launch identity is based on a PID without its start_ticks."""
    old = LaunchIdentity(ProcessIdentity(BOOT, 50, 7), "unit:app-demo.service")
    reused = LaunchIdentity(ProcessIdentity(BOOT, 50, 8), "unit:app-demo.service")
    assert old != reused
