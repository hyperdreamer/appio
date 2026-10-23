"""Adversarial tests for the helper's root-owned file layer (P3 task 8).

The threat this file pins is:

    a privileged helper must not be turned into a root-owned arbitrary-write
    primitive -- by a symlink, a hardlink, a hostile pre-existing file, a
    traversable ancestor, a path outside the fixed allowlist, or a crash in
    the middle of a replace -- and the state that gates activation must be
    durable and bounded rather than best-effort.

Every section names the concrete route by which that threat would be realised
and fails if the route reopens. Nothing here needs root: the filesystem root,
trusted-file metadata, the effective UID, the ownership callback and the
operation/failure callback are injected through :class:`SetupDeps`, and no test
writes outside its temporary tree.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from appio.setup_boundary import SetupDeps
from appio.setup_files import (
    LOCK_FILE,
    LOCK_ROOT,
    PROBE_ROOT,
    SERVICE_DROPIN,
    STAGING_ROOT,
    STATE_FILE,
    STATE_ROOT,
    USER_MANAGER_DROPIN,
    SafeRootFS,
    SafeRootFSError,
    manager_dropin_payload,
    parse_manager_dropin,
    parse_service_dropin,
    service_dropin_payload,
)

SERVICE_PAYLOAD = b"[Service]\nDelegate=cpu io memory pids\nIOAccounting=yes\n"
MANAGER_PAYLOAD = b"[Manager]\nDefaultIOAccounting=yes\n"
MAX_UID = 2**32 - 2


# --------------------------------------------------------------------------- #
# Test doubles: the seams are dependency objects, never argv or environment.
# --------------------------------------------------------------------------- #


class InjectedFault(RuntimeError):
    pass


class FakeMetadata:
    """``os.fstat`` replacement that can lie about ownership without root.

    The real file type, inode and link count are kept so directory/regular/
    hardlink checks stay real; only owner and permission bits are injected.
    """

    def __init__(self, default_uid: int = 0):
        self.default_uid = default_uid
        self.overrides: dict[tuple[int, int], tuple[int | None, int | None]] = {}

    @staticmethod
    def inode(path: Path | str) -> tuple[int, int]:
        info = os.stat(path, follow_symlinks=False)
        return (info.st_dev, info.st_ino)

    def override(self, path: Path | str, *, uid: int | None = None, mode: int | None = None) -> None:
        self.overrides[self.inode(path)] = (uid, mode)

    def __call__(self, fd: int) -> SimpleNamespace:
        real = os.fstat(fd)
        uid, permissions = self.overrides.get((real.st_dev, real.st_ino), (self.default_uid, None))
        mode = real.st_mode if permissions is None else (real.st_mode & ~0o777) | permissions
        return SimpleNamespace(
            st_uid=uid,
            st_gid=0,
            st_mode=mode,
            st_ino=real.st_ino,
            st_dev=real.st_dev,
            st_nlink=real.st_nlink,
        )


class EventLog:
    """Records boundary operations, runs a hook, and injects one failure."""

    def __init__(self, failures: tuple[str, ...] = (), hook=None):
        self.names: list[str] = []
        self.failures = set(failures)
        self.hook = hook

    def __call__(self, name: str) -> None:
        self.names.append(name)
        if self.hook is not None:
            self.hook(name)
        if name in self.failures:
            raise InjectedFault(name)

    def index(self, name: str) -> int:
        return self.names.index(name)

    def publish(self) -> list[str]:
        wanted = {"mkstemp", "write", "chown", "fsync", "rename", "fsync-dir"}
        return [name for name in self.names if name in wanted]


def make_deps(
    root: Path,
    *,
    metadata: FakeMetadata | None = None,
    events: EventLog | None = None,
    effective_uid=None,
    chown=None,
) -> SetupDeps:
    return SetupDeps(
        root=Path(root),
        runner=None,  # no command is expected by the file layer
        effective_uid=effective_uid or (lambda: 0),
        getpwuid=lambda uid: None,
        metadata=metadata or FakeMetadata(),
        chown=chown or (lambda fd, uid, gid: None),
        clock_ns=lambda: 123456789,
        event=events or EventLog(),
    )


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    (base / SERVICE_DROPIN.parent.relative_to("/")).mkdir(parents=True)
    (base / USER_MANAGER_DROPIN.parent.relative_to("/")).mkdir(parents=True)
    return base


def rebase(root: Path, target: Path) -> Path:
    return root / target.relative_to("/")


def write_via_fs(
    root: Path,
    target: Path,
    payload: bytes,
    *,
    mode: int = 0o644,
    owner: tuple[int, int] | None = None,
) -> None:
    SafeRootFS(make_deps(root)).atomic_write(str(target), payload, mode=mode, owner=owner)


# --------------------------------------------------------------------------- #
# Threat: a planted symlink redirects a root write onto a victim file.
# --------------------------------------------------------------------------- #


def test_config_symlink_cannot_overwrite_a_victim(tmp_path):
    target = tmp_path / "etc/systemd/system/user@.service.d/10-appio-io.conf"
    target.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    target.symlink_to(victim)

    def metadata(fd):
        row = os.fstat(fd)
        return SimpleNamespace(st_uid=0, st_gid=0, st_mode=row.st_mode,
                               st_ino=row.st_ino, st_dev=row.st_dev, st_nlink=row.st_nlink)

    deps = SetupDeps(tmp_path, None, lambda: 0, lambda uid: None, metadata,
                     lambda path, uid, gid: None, lambda: 1, lambda name: None)
    with pytest.raises(PermissionError):
        SafeRootFS(deps).atomic_write(
            "/etc/systemd/system/user@.service.d/10-appio-io.conf", b"new")
    assert victim.read_bytes() == b"preserve"


def test_symlinked_ancestor_directory_is_refused(root: Path, tmp_path: Path):
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (root / "etc/systemd/system").rename(root / "etc/systemd/real-system")
    (root / "etc/systemd/system").symlink_to(attacker)
    events = EventLog()
    deps = make_deps(root, events=events)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(deps).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert "symlink" in caught.value.reason
    assert list(attacker.iterdir()) == []
    assert events.names == []


def test_existing_target_symlink_is_refused_and_victim_survives(root: Path):
    victim = root.parent / "victim"
    victim.write_bytes(b"preserve")
    target = rebase(root, SERVICE_DROPIN)
    target.symlink_to(victim)
    events = EventLog()
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root, events=events)).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert "symlink" in caught.value.reason
    assert victim.read_bytes() == b"preserve"
    assert "mkstemp" not in events.names


# --------------------------------------------------------------------------- #
# Threat: an ancestor the attacker controls makes the fixed path a lie.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uid, mode, expected",
    [
        (1000, 0o755, "not_root_owned"),
        (0, 0o775, "writable"),
        (0, 0o757, "writable"),
        (0, 0o777, "writable"),
    ],
)
def test_attacker_owned_or_writable_ancestor_is_refused(root: Path, uid: int, mode: int, expected: str):
    metadata = FakeMetadata()
    metadata.override(root / "etc/systemd/system", uid=uid, mode=mode)
    events = EventLog()
    deps = make_deps(root, metadata=metadata, events=events)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(deps).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert expected in caught.value.reason
    assert events.names == []
    assert not rebase(root, SERVICE_DROPIN).exists()


def test_non_canonical_injected_root_is_refused(tmp_path: Path):
    events = EventLog()
    deps = make_deps(tmp_path, events=events)
    deps = SetupDeps(
        root=tmp_path / "root" / "..",
        runner=None,
        effective_uid=lambda: 0,
        getpwuid=lambda uid: None,
        metadata=FakeMetadata(),
        chown=lambda fd, uid, gid: None,
        clock_ns=lambda: 1,
        event=events,
    )
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(deps).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert "not_normalized" in caught.value.reason
    assert events.names == []


# --------------------------------------------------------------------------- #
# Threat: a hardlink or non-regular file at the fixed name is trusted.
# --------------------------------------------------------------------------- #


def test_existing_target_hardlink_is_refused(root: Path):
    victim = root.parent / "victim"
    victim.write_bytes(SERVICE_PAYLOAD)
    target = rebase(root, SERVICE_DROPIN)
    os.link(victim, target)
    assert os.stat(target).st_nlink == 2
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root)).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert "hardlinked" in caught.value.reason
    assert victim.read_bytes() == SERVICE_PAYLOAD


def test_existing_target_non_regular_file_is_refused(root: Path):
    target = rebase(root, SERVICE_DROPIN)
    os.mkfifo(target)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root)).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert "not_a_regular_file" in caught.value.reason
    assert stat.S_ISFIFO(os.stat(target).st_mode)


# --------------------------------------------------------------------------- #
# Threat: an unrelated administrator file is silently replaced.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "target, payload",
    [
        (SERVICE_DROPIN, b"[Unit]\nDescription=admin owned\n"),
        (SERVICE_DROPIN, b"[Service]\nDelegate=io\nIOAccounting=maybe\n"),
        (SERVICE_DROPIN, b"[Service]\nDelegate=io io\nIOAccounting=yes\n"),
        (USER_MANAGER_DROPIN, b"[Manager]\nDefaultTasksMax=infinity\n"),
    ],
)
def test_existing_unexpected_content_is_refused_and_preserved(root: Path, target: Path, payload: bytes):
    path = rebase(root, target)
    path.write_bytes(payload)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root)).atomic_write(str(target), SERVICE_PAYLOAD)
    assert "unexpected_content" in caught.value.reason
    assert path.read_bytes() == payload


def test_recognizers_accept_only_the_canonical_fixed_formats():
    assert parse_service_dropin(service_dropin_payload({"cpu", "io", "memory", "pids"})) == frozenset(
        {"cpu", "io", "memory", "pids"}
    )
    assert parse_service_dropin(b"[Service]\nDelegate=cpu io memory pids\nIOAccounting=yes") is None
    assert parse_service_dropin(b"[Service]\nDelegate=io memory\nIOAccounting=yes\n") == frozenset(
        {"io", "memory"}
    )
    assert parse_service_dropin(b"[Service]\nDelegate=Io\nIOAccounting=yes\n") is None
    assert parse_service_dropin(b"[Service]\nDelegate=io;rm -rf /\nIOAccounting=yes\n") is None
    assert parse_service_dropin(b"") is None
    assert parse_manager_dropin(MANAGER_PAYLOAD)
    assert not parse_manager_dropin(b"[Manager]\nDefaultTasksMax=infinity\n")


# --------------------------------------------------------------------------- #
# Threat: a crash mid-replace leaves a partial file or loses the old one.
# --------------------------------------------------------------------------- #


def test_atomic_write_publishes_and_leaves_no_temporary(root: Path):
    path = rebase(root, SERVICE_DROPIN)
    SafeRootFS(make_deps(root)).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert path.read_bytes() == SERVICE_PAYLOAD
    assert [child.name for child in path.parent.iterdir()] == [SERVICE_DROPIN.name]


def test_write_order_file_fsync_precedes_rename_and_directory_fsync_follows(root: Path, monkeypatch):
    events = EventLog()
    chown_calls: list[tuple[int, int, int]] = []
    deps = make_deps(root, events=events, chown=lambda fd, uid, gid: chown_calls.append((fd, uid, gid)))
    # Record the descriptors that are actually fsynced, so deleting the parent
    # fsync cannot be hidden behind an event that no longer has a syscall.
    fsynced: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsynced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    SafeRootFS(deps).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert fsynced == ["file", "dir"]  # file bytes, then the parent directory entry
    assert events.publish() == ["mkstemp", "write", "chown", "fsync", "rename", "fsync-dir"]
    assert events.index("write") > events.index("mkstemp")
    assert events.index("chown") > events.index("write")
    assert events.index("fsync") > events.index("chown")
    assert events.index("rename") > events.index("fsync")
    assert events.index("fsync-dir") > events.index("rename")
    assert [(uid, gid) for _, uid, gid in chown_calls] == [(0, 0)]


@pytest.mark.parametrize("failure", ["write", "fsync", "rename"])
def test_failure_before_publication_leaves_the_previous_file_intact(root: Path, failure: str):
    path = rebase(root, SERVICE_DROPIN)
    previous = b"[Service]\nDelegate=cpu io memory pids\nIOAccounting=yes\n"
    path.write_bytes(previous)
    events = EventLog(failures=(failure,))
    deps = make_deps(root, events=events)
    with pytest.raises(InjectedFault):
        SafeRootFS(deps).atomic_write(
            str(SERVICE_DROPIN), b"[Service]\nDelegate=cpu cpuset io memory pids\nIOAccounting=yes\n"
        )
    assert path.read_bytes() == previous
    assert [child.name for child in path.parent.iterdir()] == [SERVICE_DROPIN.name]


def test_parent_fsync_failure_is_reported_after_publication_and_leaves_no_temporary(root: Path):
    # The rename is the publication point: once it has happened the previous
    # bytes cannot be restored without a second, also-unsafe write. What must
    # hold is that the failure is reported (never silent success) and that no
    # temporary is left behind, so the caller's idempotent retry is safe.
    path = rebase(root, SERVICE_DROPIN)
    path.write_bytes(b"[Service]\nDelegate=cpu io memory pids\nIOAccounting=yes\n")
    events = EventLog(failures=("fsync-dir",))
    with pytest.raises(InjectedFault):
        SafeRootFS(make_deps(root, events=events)).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert path.read_bytes() == SERVICE_PAYLOAD
    assert [child.name for child in path.parent.iterdir()] == [SERVICE_DROPIN.name]
    assert events.index("rename") < events.index("fsync-dir")


def test_live_target_is_never_truncated_while_the_temporary_is_written(root: Path):
    path = rebase(root, SERVICE_DROPIN)
    previous = b"[Service]\nDelegate=cpu io memory pids\nIOAccounting=yes\n"
    path.write_bytes(previous)
    inode_before = os.stat(path).st_ino
    seen: list[tuple[bytes, int]] = []

    def hook(name: str) -> None:
        if name == "write":
            seen.append((path.read_bytes(), os.stat(path).st_ino))

    events = EventLog(hook=hook)
    SafeRootFS(make_deps(root, events=events)).atomic_write(
        str(SERVICE_DROPIN), b"[Service]\nDelegate=cpu cpuset io memory pids\nIOAccounting=yes\n"
    )
    assert seen == [(previous, inode_before)]


def test_parent_swapped_during_the_write_is_refused(root: Path):
    parent = rebase(root, SERVICE_DROPIN).parent
    moved = root / "moved-aside"

    def hook(name: str) -> None:
        if name == "mkstemp":
            os.rename(parent, moved)
            parent.mkdir()

    events = EventLog(hook=hook)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root, events=events)).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert "changed_during_write" in caught.value.reason
    assert not (parent / SERVICE_DROPIN.name).exists()
    assert not (moved / SERVICE_DROPIN.name).exists()
    assert list(moved.iterdir()) == []


def test_mode_is_fixed_even_under_an_untrusted_umask(root: Path):
    previous_umask = os.umask(0o077)
    try:
        write_via_fs(root, SERVICE_DROPIN, SERVICE_PAYLOAD)
        write_via_fs(root, STATE_FILE, b"{}", mode=0o600)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(os.stat(rebase(root, SERVICE_DROPIN)).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(rebase(root, STATE_FILE)).st_mode) == 0o600


def test_repeated_write_is_idempotent_and_keeps_the_fixed_mode(root: Path):
    write_via_fs(root, SERVICE_DROPIN, SERVICE_PAYLOAD)
    first = os.stat(rebase(root, SERVICE_DROPIN))
    write_via_fs(root, SERVICE_DROPIN, SERVICE_PAYLOAD)
    second = os.stat(rebase(root, SERVICE_DROPIN))
    assert rebase(root, SERVICE_DROPIN).read_bytes() == SERVICE_PAYLOAD
    assert stat.S_IMODE(second.st_mode) == 0o644
    assert first.st_ino != second.st_ino  # replace, never in-place truncation


# --------------------------------------------------------------------------- #
# Threat: the allowlist is a convention callers can step around.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",
        "/etc/systemd/system/99-other.service",
        "/etc/systemd/user.conf.d/../user.conf.d/10-appio.conf",
        "etc/systemd/user.conf.d/10-appio.conf",
        "/var/lib/appio",
        "/var/lib/appio/../etc/passwd",
        "/var/lib/appio/.hidden",
        "/var/lib/appio/probes/../setup-state.json",
        "/var/lib/appio/probes/../../etc/passwd",
        "/run/appio-setup/../setup-state.json",
        "/var/lib/appio/setup-state.json\n",
        "/" + "a" * 5000,
    ],
)
def test_paths_outside_the_declared_fixed_roots_are_refused(root: Path, candidate: str):
    events = EventLog()
    with pytest.raises(SafeRootFSError):
        SafeRootFS(make_deps(root, events=events)).atomic_write(candidate, b"payload")
    assert events.names == []


def test_state_probe_and_staging_paths_are_addressable(root: Path):
    write_via_fs(root, STATE_FILE, b"{}", mode=0o600)
    write_via_fs(root, PROBE_ROOT / "1000" / "probe.bin", b"x", mode=0o600, owner=(1000, 1000))
    write_via_fs(root, STAGING_ROOT / "snapshot" / "history.db", b"x", mode=0o600)
    assert rebase(root, STATE_FILE).exists()
    assert (rebase(root, PROBE_ROOT / "1000" / "probe.bin")).exists()
    assert (rebase(root, STAGING_ROOT / "snapshot" / "history.db")).exists()


def test_a_caller_owned_file_is_only_allowed_under_the_probe_root(root: Path):
    with pytest.raises(SafeRootFSError) as caught:
        write_via_fs(root, STATE_FILE, b"{}", mode=0o600, owner=(1000, 1000))
    assert "unsafe_owner" in caught.value.reason
    with pytest.raises(SafeRootFSError):
        write_via_fs(root, SERVICE_DROPIN, SERVICE_PAYLOAD, mode=0o644, owner=(1000, 1000))


def test_config_targets_refuse_any_mode_other_than_the_fixed_one(root: Path):
    # The drop-in's mode is part of the installed contract: a caller must not be
    # able to publish a group- or world-writable root configuration file.
    for bad_mode in (0o600, 0o666, 0o664, 0o777, 0o4644):
        with pytest.raises(SafeRootFSError) as caught:
            write_via_fs(root, SERVICE_DROPIN, SERVICE_PAYLOAD, mode=bad_mode)
        assert "unsafe_mode" in caught.value.reason
    assert not rebase(root, SERVICE_DROPIN).exists()


def test_directory_modes_are_established_by_the_file_layer(root: Path):
    write_via_fs(root, STATE_FILE, b"{}", mode=0o600)
    write_via_fs(root, PROBE_ROOT / "1000" / "probe.bin", b"x", mode=0o600, owner=(1000, 1000))
    write_via_fs(root, STAGING_ROOT / "snapshot" / "history.db", b"x", mode=0o600)
    # /var/lib/appio and the probe chain are execute-only (0711): the invoking
    # user must be able to reach its own pre-created probe file by path, but
    # must not be able to list or write the state directory.
    assert stat.S_IMODE(os.stat(rebase(root, STATE_ROOT)).st_mode) == 0o711
    assert stat.S_IMODE(os.stat(rebase(root, PROBE_ROOT)).st_mode) == 0o711
    assert stat.S_IMODE(os.stat(rebase(root, PROBE_ROOT / "1000")).st_mode) == 0o711
    # Private staging is root-only (0700).
    assert stat.S_IMODE(os.stat(rebase(root, STAGING_ROOT)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(rebase(root, STAGING_ROOT / "snapshot")).st_mode) == 0o700


def test_write_requires_the_privileged_effective_uid(root: Path):
    events = EventLog()
    deps = make_deps(root, events=events, effective_uid=lambda: 1000)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(deps).atomic_write(str(SERVICE_DROPIN), SERVICE_PAYLOAD)
    assert "root_required" in caught.value.reason
    assert events.names == []
    assert not rebase(root, SERVICE_DROPIN).exists()


def test_the_file_layer_never_writes_outside_the_injected_root(root: Path, tmp_path: Path):
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    write_via_fs(root, SERVICE_DROPIN, SERVICE_PAYLOAD)
    write_via_fs(root, USER_MANAGER_DROPIN, MANAGER_PAYLOAD)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    added = set(after) - set(before)
    assert added == {
        "root/etc/systemd/system/user@.service.d/10-appio-io.conf",
        "root/etc/systemd/user.conf.d/10-appio.conf",
    }


# --------------------------------------------------------------------------- #
# Threat: a bounded reader is used to parse unbounded or substituted content.
# --------------------------------------------------------------------------- #


def test_read_bounded_returns_exact_bytes(root: Path):
    write_via_fs(root, STATE_FILE, b"exact", mode=0o600)
    assert SafeRootFS(make_deps(root)).read_bounded(str(STATE_FILE), max_bytes=16) == b"exact"


def test_read_bounded_refuses_oversize_content(root: Path):
    write_via_fs(root, STATE_FILE, b"x" * 17, mode=0o600)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root)).read_bounded(str(STATE_FILE), max_bytes=16)
    assert "too_large" in caught.value.reason


def test_read_bounded_refuses_a_symlink(root: Path):
    victim = root.parent / "victim"
    victim.write_bytes(b"secret")
    rebase(root, STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    rebase(root, STATE_FILE).symlink_to(victim)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root)).read_bounded(str(STATE_FILE), max_bytes=64)
    assert "symlink" in caught.value.reason


def test_read_bounded_refuses_untrusted_metadata_and_hardlinks(root: Path):
    write_via_fs(root, STATE_FILE, b"data", mode=0o600)
    metadata = FakeMetadata()
    metadata.override(rebase(root, STATE_FILE), uid=1000)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root, metadata=metadata)).read_bounded(str(STATE_FILE), max_bytes=64)
    assert "not_root_owned" in caught.value.reason

    victim = root.parent / "victim"
    victim.write_bytes(b"data")
    hardlinked = rebase(root, USER_MANAGER_DROPIN)
    os.link(victim, hardlinked)
    with pytest.raises(SafeRootFSError) as caught:
        SafeRootFS(make_deps(root)).read_bounded(str(USER_MANAGER_DROPIN), max_bytes=64)
    assert "hardlinked" in caught.value.reason


@pytest.mark.parametrize("max_bytes", [0, -1, True, "64", None])
def test_read_bounded_validates_its_bound(root: Path, max_bytes):
    write_via_fs(root, STATE_FILE, b"data", mode=0o600)
    with pytest.raises((TypeError, ValueError, SafeRootFSError)):
        SafeRootFS(make_deps(root)).read_bounded(str(STATE_FILE), max_bytes=max_bytes)


# --------------------------------------------------------------------------- #
# Threat: concurrent helpers interleave two read-modify-write sequences.
# --------------------------------------------------------------------------- #


def test_the_helper_lock_is_exclusive_across_instances(root: Path):
    first = SafeRootFS(make_deps(root))
    second = SafeRootFS(make_deps(root))
    entered = threading.Event()
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with first.locked():
            entered.set()
            release.wait(10)

    def contender() -> None:
        # A separate SafeRootFS instance is a separate helper process for lock
        # purposes: flock is per open file description, not per instance.
        entered.wait(10)
        with second.locked():
            acquired.set()

    holding = threading.Thread(target=holder, daemon=True)
    waiting = threading.Thread(target=contender, daemon=True)
    holding.start()
    waiting.start()
    try:
        assert entered.wait(10)
        assert not acquired.wait(0.2)
        release.set()
        waiting.join(10)
        assert acquired.is_set()
    finally:
        release.set()
        holding.join(10)


def test_nested_locks_in_one_helper_do_not_deadlock(root: Path):
    fs = SafeRootFS(make_deps(root))
    with fs.locked():
        with fs.locked():
            pass


def test_a_failure_while_acquiring_the_lock_does_not_leave_a_stale_hold(root: Path):
    # Found in self-review: recording the re-entrancy depth before the failure
    # seam means a later nested call silently skips flock.
    events = EventLog(failures=("lock",))
    fs = SafeRootFS(make_deps(root, events=events))
    with pytest.raises(InjectedFault):
        with fs.locked():
            pass
    assert getattr(fs._hold, "depth", 0) == 0
    events.failures.clear()
    with fs.locked():
        pass
    assert events.names.count("lock") == 2


def test_lock_file_is_root_owned_0600_and_a_symlink_is_refused(root: Path):
    chown_calls: list[tuple[int, int]] = []
    events = EventLog()
    deps = make_deps(root, events=events, chown=lambda fd, uid, gid: chown_calls.append((uid, gid)))
    fs = SafeRootFS(deps)
    with fs.locked():
        pass
    lock_path = rebase(root, LOCK_FILE)
    assert stat.S_IMODE(os.stat(lock_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(rebase(root, LOCK_ROOT)).st_mode) == 0o755
    assert chown_calls[-1] == (0, 0)

    os.unlink(lock_path)
    victim = root.parent / "victim"
    victim.write_bytes(b"preserve")
    lock_path.symlink_to(victim)
    with pytest.raises(SafeRootFSError) as caught:
        with fs.locked():
            pass
    assert "symlink" in caught.value.reason
    assert victim.read_bytes() == b"preserve"
