"""Adversarial tests for delegation install and pending activation state.

The threat this file pins is:

    installing the IOAccounting delegation must never (a) persist state after
    the fact, so a crash leaves a helper claiming activation it never staged,
    (b) remove a supplementary group membership or target a claimed username,
    (c) write a drop-in anywhere other than the two fixed paths with content
    other than the canonical form, or (d) let a failed operation be reported
    as installed.

The pending flag is the only thing that gates the UI's activation banner, so
it is tested as durable evidence: exact bytes, canonical UID keys, bounded
reads, and refusal to overwrite a corrupt file. Nothing here needs root: the
filesystem root, metadata, effective UID, ownership callback, operation
callback and command runner are all injected.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from appio.setup_boundary import CommandResult, SetupDeps, SetupIdentity
from appio.setup_delegation import (
    SetupState,
    SetupStateError,
    install_delegation,
    load_setup_state,
    set_pending,
)
from appio.setup_files import (
    LOCK_ROOT,
    SERVICE_DROPIN,
    STATE_FILE,
    STATE_ROOT,
    USER_MANAGER_DROPIN,
    SafeRootFS,
)

GETENT = ("/usr/bin/getent", "group", "appio")
SHOW = ("/usr/bin/systemctl", "show", "user@.service", "--property=Delegate", "--value")
USERMOD = ("/usr/bin/usermod", "-aG", "appio", "alice")
RELOAD = ("/usr/bin/systemctl", "daemon-reload")

SERVICE_PAYLOAD = b"[Service]\nDelegate=cpu io memory pids\nIOAccounting=yes\n"
MANAGER_PAYLOAD = b"[Manager]\nDefaultIOAccounting=yes\n"
CANONICAL_PENDING = b'{"version":1,"pending_by_uid":{"1000":true}}'
MAX_UID = 2**32 - 2


class InjectedFault(RuntimeError):
    pass


class FakeMetadata:
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


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult] | None = None, timeline=None):
        self.calls: list[tuple[str, ...]] = []
        self.results = dict(results or {})
        self.timeline = timeline

    def run(self, argv, *, uid=0, gid=0, env=None, timeout_seconds=5.0):
        call = tuple(argv)
        self.calls.append(call)
        if self.timeline is not None:
            self.timeline.append(("run", call))
        return self.results.get(call, CommandResult(0, "", ""))


class RecordingFS:
    """A SafeRootFS facade that records every write and can fail one path."""

    def __init__(self, inner: SafeRootFS, timeline=None):
        self.inner = inner
        self.writes: list[tuple[str, bytes, int, tuple[int, int] | None]] = []
        self.lock_entries = 0
        self.fail_on: dict[str, Exception] = {}
        self.timeline = timeline

    def atomic_write(self, path: str, content: bytes, *, mode: int = 0o644, owner=None) -> None:
        if path in self.fail_on:
            raise self.fail_on[path]
        self.writes.append((path, content, mode, owner))
        if self.timeline is not None:
            self.timeline.append(("write", path))
        self.inner.atomic_write(path, content, mode=mode, owner=owner)

    def read_bounded(self, path: str, *, max_bytes: int, create_parents: bool = True) -> bytes:
        return self.inner.read_bounded(path, max_bytes=max_bytes, create_parents=create_parents)

    @contextlib.contextmanager
    def locked(self):
        self.lock_entries += 1
        with self.inner.locked():
            yield

    # Convenience for assertions.
    def written_paths(self) -> list[str]:
        return [path for path, _content, _mode, _owner in self.writes]


def test_load_setup_state_noncreating_mode_and_default(root: Path):
    fs = RecordingFS(SafeRootFS(make_deps(root)))
    assert not rebase(root, STATE_ROOT).exists()
    assert dict(load_setup_state(fs, create_parents=False).pending_by_uid) == {}
    assert not rebase(root, STATE_ROOT).exists()
    assert dict(load_setup_state(fs).pending_by_uid) == {}
    assert rebase(root, STATE_ROOT).is_dir()
    assert not rebase(root, STATE_FILE).exists()


def results(delegate: str = "cpu io memory pids", *, getent=0, usermod=0, reload=0):
    return {
        GETENT: CommandResult(getent, "appio:x:990:\n" if getent == 0 else "", "" if getent == 0 else "no group\n"),
        SHOW: CommandResult(0, delegate + "\n", ""),
        USERMOD: CommandResult(usermod, "", "" if usermod == 0 else "usermod: group busy\n"),
        RELOAD: CommandResult(reload, "", "" if reload == 0 else "Failed to reload daemon\n"),
    }


def make_deps(
    root: Path,
    *,
    runner: FakeRunner | None = None,
    metadata: FakeMetadata | None = None,
    effective_uid=None,
    chown=None,
    events=None,
    getpwuid=None,
) -> SetupDeps:
    return SetupDeps(
        root=Path(root),
        runner=runner or FakeRunner(results()),
        effective_uid=effective_uid or (lambda: 0),
        getpwuid=getpwuid
        or (lambda uid: SimpleNamespace(pw_uid=uid, pw_gid=1000, pw_name="alice", pw_dir="/home/alice")),
        metadata=metadata or FakeMetadata(),
        chown=chown or (lambda fd, uid, gid: None),
        clock_ns=lambda: 123456789,
        event=events or (lambda name: None),
    )


def identity(**overrides) -> SetupIdentity:
    fields = dict(uid=1000, gid=1000, username="alice", home=Path("/home/alice"))
    fields.update(overrides)
    return SetupIdentity(**fields)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    (base / SERVICE_DROPIN.parent.relative_to("/")).mkdir(parents=True)
    (base / USER_MANAGER_DROPIN.parent.relative_to("/")).mkdir(parents=True)
    return base


def rebase(root: Path, target: Path) -> Path:
    return root / target.relative_to("/")


def harness(root: Path, *, delegate: str = "cpu io memory pids", **kwargs):
    timeline: list[tuple] = []
    runner = kwargs.pop("runner", None) or FakeRunner(results(delegate), timeline=timeline)
    runner.timeline = timeline
    deps = make_deps(root, runner=runner, **kwargs)
    fs = RecordingFS(SafeRootFS(deps), timeline=timeline)
    return deps, fs, runner, timeline


# --------------------------------------------------------------------------- #
# Threat: the installed configuration is not byte-identical to the design.
# --------------------------------------------------------------------------- #


def test_install_writes_both_dropins_exactly(root: Path):
    deps, fs, runner, timeline = harness(root)
    chowned: list[tuple[int, int]] = []
    deps = make_deps(root, runner=runner, chown=lambda fd, uid, gid: chowned.append((uid, gid)))
    fs = RecordingFS(SafeRootFS(deps), timeline=timeline)

    result = install_delegation(identity(), deps=deps, fs=fs)

    assert fs.written_paths() == [str(STATE_FILE), str(SERVICE_DROPIN), str(USER_MANAGER_DROPIN)]
    assert fs.writes[0][1] == CANONICAL_PENDING
    assert fs.writes[0][2] == 0o600
    assert fs.writes[1][1] == SERVICE_PAYLOAD
    assert fs.writes[2][1] == MANAGER_PAYLOAD
    assert fs.writes[1][2] == 0o644
    assert fs.writes[2][2] == 0o644
    assert rebase(root, SERVICE_DROPIN).read_bytes() == SERVICE_PAYLOAD
    assert rebase(root, USER_MANAGER_DROPIN).read_bytes() == MANAGER_PAYLOAD
    # The lock inode and the three published files are all root-owned.
    assert chowned and set(chowned) == {(0, 0)} and len(chowned) >= 3
    assert result == {
        "version": 1,
        "uid": 1000,
        "delegation_installed": True,
        "pending_activation": True,
        "diagnostics": result["diagnostics"],
    }
    assert "answering" not in result
    assert all("answer" not in key and "active" not in key for key in result)
    assert isinstance(result["diagnostics"], list) and all(
        isinstance(item, str) and len(item) <= 256 for item in result["diagnostics"]
    )


def test_pending_state_is_persisted_before_either_dropin(root: Path):
    events = []
    deps, fs, runner, timeline = harness(root)
    original = fs.atomic_write

    def observing_write(path, content, *, mode=0o644, owner=None):
        events.append((path, "pending" if b"pending_by_uid" in content else "config"))
        original(path, content, mode=mode, owner=owner)

    fs.atomic_write = observing_write
    install_delegation(identity(), deps=deps, fs=fs)

    assert [kind for _path, kind in events] == ["pending", "config", "config"]
    assert events[0][0] == str(STATE_FILE)
    assert load_setup_state(fs).pending_by_uid == {1000: True}


def test_existing_delegated_controllers_are_preserved_in_a_sorted_union(root: Path):
    deps, fs, runner, timeline = harness(root, delegate="cpu cpuset pids")
    install_delegation(identity(), deps=deps, fs=fs)
    assert fs.writes[1][1] == b"[Service]\nDelegate=cpu cpuset io memory pids\nIOAccounting=yes\n"
    assert fs.writes[1][1].splitlines()[1] == b"Delegate=cpu cpuset io memory pids"


def test_a_controller_only_in_the_existing_file_is_never_removed(root: Path):
    existing = b"[Service]\nDelegate=cpu cpuset io memory pids\nIOAccounting=yes\n"
    rebase(root, SERVICE_DROPIN).write_bytes(existing)
    deps, fs, runner, timeline = harness(root, delegate="cpu io memory pids")
    install_delegation(identity(), deps=deps, fs=fs)
    assert fs.writes[1][1] == existing
    assert b"cpuset" in rebase(root, SERVICE_DROPIN).read_bytes()


def test_an_uninspectable_delegate_property_does_not_block_installation(root: Path):
    # Host acceptance finding (systemd 262): `systemctl show user@.service
    # --property=Delegate --value` exits 1 with "Unit name user@.service is neither
    # a valid invocation ID nor unit name", because a template needs an instance.
    # The observation is advisory - the required controller set is unioned in
    # regardless - so an uninspectable unit must not abort the install on a fresh
    # host. Before the fix this returned inspect-delegates:exit_1 and wrote nothing.
    observed_failure = CommandResult(
        1, "", "Failed to get properties: Unit name user@.service is neither a valid "
               "invocation ID nor unit name."
    )
    table = results()
    table[SHOW] = observed_failure
    deps = make_deps(root, runner=FakeRunner(table))
    fs = RecordingFS(SafeRootFS(deps))
    result = install_delegation(identity(), deps=deps, fs=fs)
    assert result["delegation_installed"] is True, result
    assert result.get("error") is None
    # pending state first, then both fixed drop-ins - and nothing else.
    assert fs.written_paths() == [
        str(STATE_FILE), str(SERVICE_DROPIN), str(USER_MANAGER_DROPIN)
    ]
    written = rebase(root, SERVICE_DROPIN).read_bytes()
    delegate = next(
        line.split(b"=", 1)[1].split() for line in written.splitlines()
        if line.startswith(b"Delegate=")
    )
    # io must be delegated; order is whatever the existing file and observation imply.
    assert {b"cpu", b"io", b"memory", b"pids"} <= set(delegate), delegate


def test_install_refuses_a_non_canonical_existing_service_dropin(root: Path):
    # F-1: the install path must stop at a hostile pre-existing service drop-in
    # before writing either file, and must name that fixed operation so Task 10
    # can report the exact failure.
    seeded = b"[Unit]\nDescription=admin owned\n"
    rebase(root, SERVICE_DROPIN).write_bytes(seeded)
    deps, fs, runner, timeline = harness(root)
    result = install_delegation(identity(), deps=deps, fs=fs)
    assert result["error"] == "inspect-existing-service-dropin:unexpected_content"
    assert result["delegation_installed"] is False
    assert result["pending_activation"] is True
    assert runner.calls == [GETENT, SHOW]
    assert fs.written_paths() == [str(STATE_FILE)]
    assert rebase(root, SERVICE_DROPIN).read_bytes() == seeded
    assert not rebase(root, USER_MANAGER_DROPIN).exists()


def test_install_refuses_a_non_canonical_existing_manager_dropin(root: Path):
    # The manager drop-in is checked by SafeRootFS when it is replaced; the
    # failure must be attributed to that write and must preserve the file.
    seeded = b"[Manager]\nDefaultTasksMax=infinity\n"
    rebase(root, USER_MANAGER_DROPIN).write_bytes(seeded)
    deps, fs, runner, timeline = harness(root)
    result = install_delegation(identity(), deps=deps, fs=fs)
    assert result["error"] == "write-user-manager-dropin:write_failed"
    assert result["delegation_installed"] is False
    assert result["pending_activation"] is True
    assert runner.calls == [GETENT, SHOW]
    assert rebase(root, USER_MANAGER_DROPIN).read_bytes() == seeded
    assert rebase(root, SERVICE_DROPIN).read_bytes() == SERVICE_PAYLOAD


# --------------------------------------------------------------------------- #
# Threat: group installation widens membership or targets a claimed username.
# --------------------------------------------------------------------------- #


def test_group_membership_is_appended_and_never_replaced(root: Path):
    deps, fs, runner, timeline = harness(root)
    install_delegation(identity(), deps=deps, fs=fs)
    assert runner.calls == [GETENT, SHOW, USERMOD, RELOAD]
    usermod = [call for call in runner.calls if call[0] == "/usr/bin/usermod"]
    assert usermod == [("/usr/bin/usermod", "-aG", "appio", "alice")]
    assert all("-G" not in call for call in runner.calls)


@pytest.mark.parametrize(
    "username",
    [
        "alice -G wheel",
        "alice\nroot",
        "",
        "root:x",
        "alice;id",
        " alice",
        "-alice",
        "a" * 40,
        "alice/../root",
        "alice\x00root",
    ],
)
def test_a_claimed_username_never_reaches_argv(root: Path, username: str):
    deps, fs, runner, timeline = harness(root)
    result = install_delegation(identity(username=username), deps=deps, fs=fs)
    assert result["error"] == "identity:invalid_identity"
    assert result["delegation_installed"] is False
    assert result["pending_activation"] is False
    assert runner.calls == []
    assert fs.writes == []
    assert fs.lock_entries == 0


def test_install_refuses_a_non_root_effective_uid_before_any_effect(root: Path):
    deps, fs, runner, timeline = harness(root, effective_uid=lambda: 1000)
    result = install_delegation(identity(), deps=deps, fs=fs)
    assert result["error"] == "root:root_required"
    assert result["delegation_installed"] is False
    assert result["pending_activation"] is False
    assert runner.calls == []
    assert fs.writes == []


@pytest.mark.parametrize("getpwuid", [lambda uid: None, lambda uid: SimpleNamespace(pw_uid=uid, pw_gid=1000, pw_name="mallory", pw_dir="/home/mallory")])
def test_the_username_is_re_resolved_from_nss_before_usermod(root: Path, getpwuid):
    # A hand-built identity must not be able to add a different account to the
    # appio group: the name handed to usermod is re-derived from the account
    # database, not taken on trust from the identity object.
    deps, fs, runner, timeline = harness(root, getpwuid=getpwuid)
    result = install_delegation(identity(username="root"), deps=deps, fs=fs)
    assert result["error"] in {"identity:unknown_uid", "identity:account_mismatch"}
    assert result["delegation_installed"] is False
    assert result["pending_activation"] is False
    assert runner.calls == []
    assert fs.writes == []
    assert fs.lock_entries == 0


# --------------------------------------------------------------------------- #
# Threat: a missing prerequisite is not noticed and the install half-succeeds.
# --------------------------------------------------------------------------- #


def test_group_absence_fails_before_any_dropin_and_keeps_pending(root: Path):
    deps, fs, runner, timeline = harness(root)
    runner.results = results(getent=2)
    error = install_delegation(identity(), deps=deps, fs=fs)
    assert error["error"] == "group-prerequisite:exit_2"
    assert error["delegation_installed"] is False
    assert error["pending_activation"] is True
    assert "answering" not in error
    assert runner.calls == [GETENT]
    assert fs.written_paths() == [str(STATE_FILE)]
    assert not rebase(root, SERVICE_DROPIN).exists()
    assert not rebase(root, USER_MANAGER_DROPIN).exists()
    assert rebase(root, STATE_FILE).read_bytes() == CANONICAL_PENDING


def test_failed_delegate_inspection_writes_no_dropin(root: Path):
    deps, fs, runner, timeline = harness(root)
    runner.results[SHOW] = CommandResult(1, "", "Failed to connect to bus\n")
    error = install_delegation(identity(), deps=deps, fs=fs)
    assert error["error"] == "inspect-delegates:exit_1"
    assert error["delegation_installed"] is False
    assert error["pending_activation"] is True
    assert fs.written_paths() == [str(STATE_FILE)]
    assert "answering" not in error


# --------------------------------------------------------------------------- #
# Threat: a failed operation is reported as installed.
# --------------------------------------------------------------------------- #


def test_failed_usermod_reports_the_operation_and_stays_pending(root: Path):
    deps, fs, runner, timeline = harness(root)
    runner.results = results(usermod=1)
    error = install_delegation(identity(), deps=deps, fs=fs)
    assert error["error"] == "usermod:exit_1"
    assert error["delegation_installed"] is False
    assert error["pending_activation"] is True
    assert "group busy" in error["diagnostics"][-1]
    assert runner.calls == [GETENT, SHOW, USERMOD]
    assert rebase(root, SERVICE_DROPIN).read_bytes() == SERVICE_PAYLOAD
    assert rebase(root, USER_MANAGER_DROPIN).read_bytes() == MANAGER_PAYLOAD
    assert load_setup_state(fs).pending_by_uid == {1000: True}

    runner.results = results()
    retried = install_delegation(identity(), deps=fs_instance_deps(root, runner), fs=fs)
    assert retried["delegation_installed"] is True
    assert retried["pending_activation"] is True


def fs_instance_deps(root: Path, runner: FakeRunner) -> SetupDeps:
    """Deps that reuse an existing runner across a retry."""
    return make_deps(root, runner=runner)


def test_a_failed_second_dropin_leaves_pending_and_names_the_operation(root: Path):
    deps, fs, runner, timeline = harness(root)
    fs.fail_on[str(USER_MANAGER_DROPIN)] = InjectedFault("disk full")
    error = install_delegation(identity(), deps=deps, fs=fs)
    assert error["error"] == "write-user-manager-dropin:write_failed"
    assert error["delegation_installed"] is False
    assert error["pending_activation"] is True
    assert rebase(root, SERVICE_DROPIN).read_bytes() == SERVICE_PAYLOAD
    assert not rebase(root, USER_MANAGER_DROPIN).exists()
    assert runner.calls == [GETENT, SHOW]
    assert load_setup_state(fs).pending_by_uid == {1000: True}

    del fs.fail_on[str(USER_MANAGER_DROPIN)]
    retried = install_delegation(identity(), deps=deps, fs=fs)
    assert retried["delegation_installed"] is True
    assert rebase(root, USER_MANAGER_DROPIN).read_bytes() == MANAGER_PAYLOAD


def test_failed_daemon_reload_does_not_claim_installed(root: Path):
    deps, fs, runner, timeline = harness(root)
    runner.results = results(reload=1)
    error = install_delegation(identity(), deps=deps, fs=fs)
    assert error["error"] == "daemon-reload:exit_1"
    assert error["delegation_installed"] is False
    assert error["pending_activation"] is True
    assert runner.calls == [GETENT, SHOW, USERMOD, RELOAD]
    assert load_setup_state(fs).pending_by_uid == {1000: True}

    runner.results = results()
    retried = install_delegation(identity(), deps=fs_instance_deps(root, runner), fs=fs)
    assert retried["delegation_installed"] is True


def test_corrupt_pending_state_fails_before_any_install_and_claims_nothing(root: Path):
    target = rebase(root, STATE_FILE)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not json")
    deps, fs, runner, timeline = harness(root)
    result = install_delegation(identity(), deps=deps, fs=fs)
    assert result["error"] == "persist-pending:state_unavailable"
    assert result["delegation_installed"] is False
    assert result["pending_activation"] is False
    assert runner.calls == []
    assert fs.written_paths() == []
    assert target.read_bytes() == b"not json"


def test_a_planted_lock_symlink_is_a_bounded_failure(root: Path):
    lock_dir = rebase(root, LOCK_ROOT)
    lock_dir.mkdir(parents=True)
    victim = root.parent / "victim"
    victim.write_bytes(b"preserve")
    (lock_dir / "helper.lock").symlink_to(victim)
    deps, fs, runner, timeline = harness(root)
    result = install_delegation(identity(), deps=deps, fs=fs)
    assert result["error"].startswith("helper-lock:untrusted_lock:symlink")
    assert result["delegation_installed"] is False
    assert result["pending_activation"] is False
    assert fs.writes == []
    assert runner.calls == []
    assert victim.read_bytes() == b"preserve"


# --------------------------------------------------------------------------- #
# Threat: the pending flag is not durable, readable, or bounded.
# --------------------------------------------------------------------------- #


def test_pending_state_has_exact_canonical_bytes(root: Path):
    fs = SafeRootFS(make_deps(root))
    set_pending(fs, 1000, True)
    assert rebase(root, STATE_FILE).read_bytes() == b'{"version":1,"pending_by_uid":{"1000":true}}'
    set_pending(fs, 7, True)
    assert (
        rebase(root, STATE_FILE).read_bytes()
        == b'{"version":1,"pending_by_uid":{"7":true,"1000":true}}'
    )
    set_pending(fs, 7, False)
    assert (
        rebase(root, STATE_FILE).read_bytes()
        == b'{"version":1,"pending_by_uid":{"7":false,"1000":true}}'
    )
    state = load_setup_state(fs)
    assert isinstance(state, SetupState)
    assert state.version == 1
    assert dict(state.pending_by_uid) == {7: False, 1000: True}
    with pytest.raises(TypeError):
        state.pending_by_uid[5] = True  # type: ignore[index]


def test_missing_state_reads_as_empty_and_pending_is_serialized_under_the_lock(root: Path):
    deps, fs, runner, timeline = harness(root)
    assert dict(load_setup_state(fs).pending_by_uid) == {}
    assert fs.lock_entries == 0
    set_pending(fs, 1000, True)
    assert fs.lock_entries == 1
    assert rebase(root, STATE_FILE).read_bytes() == CANONICAL_PENDING


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"{}",
        b'{"version":2,"pending_by_uid":{}}',
        b'{"version":true,"pending_by_uid":{}}',
        b'{"version":1.0,"pending_by_uid":{}}',
        b'{"version":1e0,"pending_by_uid":{}}',
        b'{"version":1,"pending_by_uid":[]}',
        b'{"version":1,"pending_by_uid":{"01000":true}}',
        b'{"version":1,"pending_by_uid":{"1000":"true"}}',
        b'{"version":1,"pending_by_uid":{"1000":1}}',
        b'{"version":1,"pending_by_uid":{"1000":true},"extra":0}',
        b'{"version":1,"pending_by_uid":{"0":true}}',
        b'{"version":1,"pending_by_uid":{"1000":true},"pending_by_uid":{"1000":false}}',
        b'{"version":1,"pending_by_uid":{}} ' + b" " * (64 * 1024),
    ],
)
def test_invalid_state_is_refused_and_never_overwritten(root: Path, payload: bytes):
    target = rebase(root, STATE_FILE)
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    fs = SafeRootFS(make_deps(root))
    with pytest.raises((SetupStateError, ValueError)):
        load_setup_state(fs)
    with pytest.raises((SetupStateError, ValueError)):
        set_pending(fs, 1000, True)
    assert target.read_bytes() == payload


@pytest.mark.parametrize("uid", [0, -1, 2**32 - 1, True, False, "1000", 1000.0, None])
def test_set_pending_validates_the_uid(root: Path, uid):
    fs = SafeRootFS(make_deps(root))
    with pytest.raises((TypeError, ValueError)):
        set_pending(fs, uid, True)
    assert not rebase(root, STATE_FILE).exists()


def test_state_writes_are_capped_at_64_kib(root: Path):
    # The design caps the shared file at 64 KiB on read *and* write: an
    # unbounded UID map must fail loudly rather than publish a truncated file
    # or grow without limit.
    from appio.setup_delegation import encode_setup_state

    huge = {uid: True for uid in range(1000, 11000)}
    payload = encode_setup_state(huge)
    assert len(payload) > 64 * 1024
    fs = SafeRootFS(make_deps(root))
    with pytest.raises(PermissionError):
        fs.atomic_write(str(STATE_FILE), payload, mode=0o600)
    assert not rebase(root, STATE_FILE).exists()


@pytest.mark.parametrize("pending", [1, 0, "true", None, [True]])
def test_set_pending_validates_the_value(root: Path, pending):
    fs = SafeRootFS(make_deps(root))
    with pytest.raises((TypeError, ValueError)):
        set_pending(fs, 1000, pending)
    assert not rebase(root, STATE_FILE).exists()


def test_set_pending_upper_bound_is_a_real_uid(root: Path):
    fs = SafeRootFS(make_deps(root))
    set_pending(fs, MAX_UID, True)
    assert load_setup_state(fs).pending_by_uid == {MAX_UID: True}


# --------------------------------------------------------------------------- #
# Threat: the helper's effects escape the fixed target set.
# --------------------------------------------------------------------------- #


def test_install_creates_the_state_directory_0711_with_a_0600_state_file(root: Path):
    deps, fs, runner, timeline = harness(root)
    install_delegation(identity(), deps=deps, fs=fs)
    assert stat.S_IMODE(os.stat(rebase(root, STATE_ROOT)).st_mode) == 0o711
    assert stat.S_IMODE(os.stat(rebase(root, STATE_FILE)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(rebase(root, SERVICE_DROPIN)).st_mode) == 0o644


def test_install_touches_nothing_outside_the_declared_targets(root: Path, tmp_path: Path):
    before = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}
    deps, fs, runner, timeline = harness(root)
    install_delegation(identity(), deps=deps, fs=fs)
    after = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}
    assert after - before == {
        "root/var",
        "root/var/lib",
        "root/var/lib/appio",
        "root/var/lib/appio/setup-state.json",
        "root/etc/systemd/system/user@.service.d/10-appio-io.conf",
        "root/etc/systemd/user.conf.d/10-appio.conf",
        # The fixed helper lock is itself a declared target under /run; it is
        # not a side effect outside the allowlist.
        "root/run",
        "root/run/appio-setup",
        "root/run/appio-setup/helper.lock",
    }


def test_repeated_install_is_idempotent(root: Path):
    deps, fs, runner, timeline = harness(root)
    first = install_delegation(identity(), deps=deps, fs=fs)
    service = rebase(root, SERVICE_DROPIN).read_bytes()
    manager = rebase(root, USER_MANAGER_DROPIN).read_bytes()
    second = install_delegation(identity(), deps=deps, fs=fs)
    assert first["delegation_installed"] and second["delegation_installed"]
    assert rebase(root, SERVICE_DROPIN).read_bytes() == service
    assert rebase(root, USER_MANAGER_DROPIN).read_bytes() == manager
    assert rebase(root, STATE_FILE).read_bytes() == CANONICAL_PENDING
    assert [call for call in runner.calls if call[0] == "/usr/bin/usermod"] == [USERMOD, USERMOD]
    assert fs.written_paths() == [
        str(STATE_FILE),
        str(SERVICE_DROPIN),
        str(USER_MANAGER_DROPIN),
        str(STATE_FILE),
        str(SERVICE_DROPIN),
        str(USER_MANAGER_DROPIN),
    ]


def test_daemon_reload_runs_after_both_files_and_the_membership_change(root: Path):
    deps, fs, runner, timeline = harness(root)
    install_delegation(identity(), deps=deps, fs=fs)
    assert timeline == [
        ("write", str(STATE_FILE)),
        ("run", GETENT),
        ("run", SHOW),
        ("write", str(SERVICE_DROPIN)),
        ("write", str(USER_MANAGER_DROPIN)),
        ("run", USERMOD),
        ("run", RELOAD),
    ]
