"""Adversarial tests for the polkit helper entry boundary (P3 task 7).

The threat this file pins is not "the helper has six verbs". It is:

    a local unprivileged user must not be able to make root run anything other
    than the fixed set of configuration operations -- no arbitrary executable,
    no arbitrary file target, no arbitrary user, and no user-controlled Python
    loaded into the root process.

Every section below names the concrete route by which that threat would be
realised, and fails if the route reopens. The tests run without root: the
filesystem root, the effective UID, trusted-file metadata and the command
runner are injected, and no test writes outside its temporary tree.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from appio import setup_boundary as boundary
from appio.setup_boundary import (
    MAX_OUTPUT_BYTES,
    MAX_TARGET_BYTES,
    TIMEOUT_RETURNCODE,
    VERBS,
    CommandResult,
    InvocationError,
    RunnerError,
    RuntimeTrustError,
    SetupDeps,
    SubprocessCommandRunner,
    TargetError,
    accept_invocation,
    atomic_write,
    parse_invocation,
    production_deps,
    production_runtime_paths,
    resolve_target,
    validate_trusted_runtime,
)

WORKTREE = Path(__file__).resolve().parents[2]
SHIM = WORKTREE / "data" / "libexec" / "appio-setup"
POLICY = WORKTREE / "data" / "polkit" / "io.github.appio.setup.policy"
MODULE = WORKTREE / "src" / "appio" / "setup_boundary.py"

POLICY_ACTION = "io.github.appio.setup"
POLICY_EXEC_PATH = "/usr/bin/appio-setup"
DELEGATION_TARGET = Path("/etc/systemd/system/user@.service.d/10-appio-io.conf")
MANAGER_TARGET = Path("/etc/systemd/user.conf.d/10-appio.conf")


# --------------------------------------------------------------------------- #
# Test doubles: the seams are dependency objects, never argv or environment.
# --------------------------------------------------------------------------- #


class FakeMetadata:
    """os.fstat replacement that can lie about ownership without touching the host.

    The return value keeps the real file type (so directory/regular checks are
    real) and fakes only the permission bits and owner, which are exactly the
    two facts the boundary must not trust the filesystem for during tests.
    """

    def __init__(self, default_uid: int = 0, overrides: dict[tuple[int, int], tuple[int, int]] | None = None):
        self.default_uid = default_uid
        self.overrides = dict(overrides or {})
        self.calls: list[int] = []

    @staticmethod
    def inode(path: Path | str) -> tuple[int, int]:
        info = os.stat(path, follow_symlinks=False)
        return (info.st_dev, info.st_ino)

    def override(self, path: Path | str, *, uid: int, mode: int) -> None:
        self.overrides[self.inode(path)] = (uid, mode)

    def __call__(self, fd: int) -> SimpleNamespace:
        self.calls.append(fd)
        real = os.fstat(fd)
        uid, permissions = self.overrides.get(
            (real.st_dev, real.st_ino),
            (self.default_uid, 0o755 if stat.S_ISDIR(real.st_mode) else 0o644),
        )
        return SimpleNamespace(st_uid=uid, st_mode=(real.st_mode & ~0o777) | permissions)


class EventLog:
    """Records boundary operations and injects a failure at a named one."""

    def __init__(self, failures: tuple[str, ...] = ()):
        self.names: list[str] = []
        self.failures = set(failures)

    def __call__(self, name: str) -> None:
        self.names.append(name)
        if name in self.failures:
            raise InjectedFault(name)

    def index(self, name: str) -> int:
        return self.names.index(name)


class InjectedFault(RuntimeError):
    pass


class FakeRunner:
    def __init__(self, result: CommandResult | None = None):
        self.calls: list[tuple[tuple[str, ...], int, int, dict[str, str], float]] = []
        self.result = result or CommandResult(0, "", "")

    def run(self, argv, *, uid=0, gid=0, env=None, timeout_seconds=5.0):
        self.calls.append((tuple(argv), uid, gid, dict(env or {}), timeout_seconds))
        return self.result


def account(uid: int = 1000, gid: int = 1000, name: object = "alice", home: object = "/home/alice", **overrides):
    fields = dict(pw_uid=uid, pw_gid=gid, pw_name=name, pw_dir=home)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_deps(
    root: Path,
    *,
    effective_uid=None,
    metadata: FakeMetadata | None = None,
    events: EventLog | None = None,
    runner: FakeRunner | None = None,
    getpwuid=None,
    chown=None,
) -> SetupDeps:
    return SetupDeps(
        root=Path(root),
        runner=runner or FakeRunner(),
        effective_uid=effective_uid or (lambda: 0),
        getpwuid=getpwuid or (lambda uid: account(uid=uid)),
        metadata=metadata or FakeMetadata(),
        chown=chown or (lambda fd, uid, gid: None),
        clock_ns=lambda: 123456789,
        event=events or EventLog(),
    )


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    (base / DELEGATION_TARGET.parent.relative_to("/")).mkdir(parents=True)
    (base / MANAGER_TARGET.parent.relative_to("/")).mkdir(parents=True)
    return base


def rebase(root: Path, target: Path) -> Path:
    return root / target.relative_to("/")


# --------------------------------------------------------------------------- #
# Threat: a root prompt must not become an arbitrary verb / argument selector.
# --------------------------------------------------------------------------- #


def test_verb_set_is_exactly_the_six_design_verbs():
    assert VERBS == frozenset(
        {
            "install-delegation",
            "enable",
            "disable",
            "status",
            "restart-user-manager",
            "export-history",
        }
    )
    with pytest.raises(AttributeError):
        VERBS.add("uninstall-delegation")  # type: ignore[attr-defined]


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_every_design_verb_is_accepted(verb: str):
    call = parse_invocation(
        ["/usr/bin/appio-setup", verb],
        environ={"PKEXEC_UID": "1000"},
        euid=0,
        getpwuid=lambda uid: account() if uid == 1000 else None,
    )
    assert call.verb == verb
    assert call.identity.uid == 1000


@pytest.mark.parametrize(
    "verb",
    [
        "uninstall-delegation",  # deliberately out of v1 scope
        "install",
        "root",
        "",
        "ENABLE",
        "enable ",  # trailing space is not the verb
        " enable",
        "--enable",
        "-c",
        "$(id)",
        ";id;",
        "&&",
        "../../bin/sh",
        "enable\n",
    ],
)
def test_unknown_or_injected_verbs_exit_two_before_account_lookup(verb: str):
    def forbidden_lookup(uid: int) -> object:
        raise AssertionError("argv rejection must precede account lookup")

    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", verb],
            environ={"PKEXEC_UID": "1000"},
            euid=0,
            getpwuid=forbidden_lookup,
        )
    assert caught.value.exit_code == 2


def test_extra_argument_is_rejected_before_account_lookup():
    def forbidden_lookup(uid):
        raise AssertionError("argv rejection must precede account lookup")

    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "enable", "/tmp/evil"],
            environ={},
            euid=0,
            getpwuid=forbidden_lookup,
        )
    assert caught.value.exit_code == 2


def test_missing_verb_is_rejected_before_account_lookup():
    def forbidden_lookup(uid):
        raise AssertionError("argv rejection must precede account lookup")

    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup"], environ={"PKEXEC_UID": "1000"}, euid=0, getpwuid=forbidden_lookup
        )
    assert caught.value.exit_code == 2


@pytest.mark.parametrize("argv", [None, "enable", b"enable", {}, 7])
def test_non_sequence_argv_is_rejected_without_crashing(argv):
    with pytest.raises(InvocationError) as caught:
        parse_invocation(argv, environ={"PKEXEC_UID": "1000"}, euid=0, getpwuid=lambda uid: account())
    assert caught.value.exit_code == 2


@pytest.mark.parametrize(
    "executable",
    [
        "appio-setup",
        "./appio-setup",
        "../bin/appio-setup",
        "/usr/bin/appio-setup\x00",
        "",
        42,
    ],
)
def test_executable_must_be_a_fixed_absolute_path(executable):
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            [executable, "enable"], environ={"PKEXEC_UID": "1000"}, euid=0, getpwuid=lambda uid: account()
        )
    assert caught.value.exit_code == 2


def test_non_root_helper_invocation_fails_closed():
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "enable"],
            environ={"PKEXEC_UID": "1000"},
            euid=1000,
            getpwuid=lambda uid: account() if uid == 1000 else None,
        )
    assert caught.value.exit_code == 1
    assert caught.value.reason == "root_required"


def test_invalid_invocation_touches_no_file_and_runs_no_command(root: Path):
    # The threat: exit code 2 must mean "nothing happened", not "argv was
    # rejected after the privileged operation started".
    metadata = FakeMetadata()
    events = EventLog()
    runner = FakeRunner()
    deps = make_deps(root, metadata=metadata, events=events, runner=runner)

    with pytest.raises(InvocationError) as caught:
        accept_invocation(
            ["/usr/bin/appio-setup", "enable", "alice"],
            deps=deps,
            environ={"PKEXEC_UID": "1000"},
        )

    assert caught.value.exit_code == 2
    assert events.names == []
    assert metadata.calls == []
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# Threat: the beneficiary must come from PKEXEC_UID, never argv/$USER/getuid().
# --------------------------------------------------------------------------- #


def test_pkexec_identity_wins_over_user_and_effective_uid():
    account_record = SimpleNamespace(
        pw_uid=1000, pw_gid=1000, pw_name="alice", pw_dir="/home/alice"
    )
    call = parse_invocation(
        ["/usr/bin/appio-setup", "enable"],
        environ={"PKEXEC_UID": "1000", "USER": "root", "HOME": "/root"},
        euid=0,
        getpwuid=lambda uid: account_record if uid == 1000 else None,
    )
    assert call.identity.uid == 1000
    assert call.identity.username == "alice"
    assert str(call.identity.home) == "/home/alice"


@pytest.mark.parametrize(
    "environ",
    [
        {"USER": "1000", "HOME": "/home/alice"},
        {"USER": "alice", "LOGNAME": "alice", "SUDO_UID": "1000"},
        {"PKEXEC_UID_ALIAS": "1000", "USER": "alice"},
        {},
    ],
)
def test_user_home_and_other_environment_variables_never_select_the_identity(environ):
    # The threat: $USER=0 or $SUDO_UID is attacker-controlled by the process that
    # called pkexec, so treating it as the beneficiary would let a caller pick
    # whose session root modifies.
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "install-delegation"],
            environ=environ,
            euid=0,
            getpwuid=lambda uid: account(),
        )
    assert caught.value.exit_code == 1
    assert caught.value.reason == "missing_pkexec_uid"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " 1000",
        "1000 ",
        "\t1000",
        "1000\n",
        "-1",
        "+1000",
        "0x3e8",
        "1e3",
        "1000.0",
        "true",
        "False",
        "002000",
        "١٠٠٠",  # non-ASCII digits must not parse as decimal
        "4294967295",  # outside the platform UID range
        "99999999999999999999",
        "9" * 5000,
        "1000\x00",
        "1000;id",
        "uid=1000",
    ],
)
def test_malformed_pkexec_uid_is_refused(raw: str):
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "status"], environ={"PKEXEC_UID": raw}, euid=0, getpwuid=lambda uid: account()
        )
    assert caught.value.exit_code == 1
    assert "pkexec_uid" in caught.value.reason


def test_root_caller_is_refused():
    # PKEXEC_UID=0 is not an escalation, but root has no delegated user session and
    # no home under which to write an export; accepting it would widen the fixed
    # operation set for no legitimate purpose.
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "enable"],
            environ={"PKEXEC_UID": "0"},
            euid=0,
            getpwuid=lambda uid: account(uid=0, gid=0, name="root", home="/root"),
        )
    assert caught.value.exit_code == 1
    assert caught.value.reason == "root_caller_rejected"


@pytest.mark.parametrize("lookup", [lambda uid: None, lambda uid: (_ for _ in ()).throw(KeyError(uid))])
def test_unknown_caller_uid_is_refused(lookup):
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "disable"], environ={"PKEXEC_UID": "4242"}, euid=0, getpwuid=lookup
        )
    assert caught.value.exit_code == 1
    assert caught.value.reason == "unknown_caller_uid"


@pytest.mark.parametrize(
    "pw_uid",
    [1001, "1000", True, None, -1],
)
def test_nss_answer_must_agree_with_the_requested_uid(pw_uid):
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "disable"],
            environ={"PKEXEC_UID": "1000"},
            euid=0,
            getpwuid=lambda uid: account(uid=pw_uid),
        )
    assert caught.value.exit_code == 1
    assert caught.value.reason.startswith("unsafe_account") or caught.value.reason == "unknown_caller_uid"


@pytest.mark.parametrize(
    "fields",
    [
        {"pw_name": ""},
        {"pw_name": "al ice"},
        {"pw_name": "-rf"},
        {"pw_name": "alice:root"},
        {"pw_name": "ali/ce"},
        {"pw_name": "ali\nce"},
        {"pw_name": "ali\x00ce"},
        {"pw_name": "a" * 33},
        {"pw_name": "Álice"},
        {"pw_name": None},
        {"pw_dir": ""},
        {"pw_dir": "home/alice"},
        {"pw_dir": "/home/alice/"},
        {"pw_dir": "/home/../root"},
        {"pw_dir": "/"},
        {"pw_dir": "/home/alice\x00"},
        {"pw_dir": "/home/\n"},
        {"pw_dir": "/" + "a" * 5000},
        {"pw_dir": None},
        {"pw_gid": -1},
        {"pw_gid": 2**32},
        {"pw_gid": "1000"},
        {"pw_gid": True},
        {"pw_gid": None},
    ],
)
def test_unsafe_nss_account_fields_are_refused(fields):
    # The threat: a crafted or broken NSS entry must not smuggle a newline, an
    # option, a relative home or a NUL into a fixed root argv or file path.
    record = account(**fields)
    with pytest.raises(InvocationError) as caught:
        parse_invocation(
            ["/usr/bin/appio-setup", "install-delegation"],
            environ={"PKEXEC_UID": "1000"},
            euid=0,
            getpwuid=lambda uid: record,
        )
    assert caught.value.exit_code == 1


def test_identity_fields_are_derived_only_from_the_nss_record():
    record = account(uid=1500, gid=1600, name="bob", home="/home/bob")
    call = parse_invocation(
        ["/usr/bin/appio-setup", "export-history"],
        environ={"PKEXEC_UID": "1500", "HOME": "/tmp", "USER": "root"},
        euid=0,
        getpwuid=lambda uid: record if uid == 1500 else None,
    )
    assert call.identity.uid == 1500
    assert call.identity.gid == 1600
    assert call.identity.username == "bob"
    assert call.identity.home == Path("/home/bob")


# --------------------------------------------------------------------------- #
# Threat: root must not load the invoking user's Python (loader attack).
# --------------------------------------------------------------------------- #


def test_trusted_runtime_accepts_a_root_owned_non_writable_closure(tmp_path: Path):
    directory = tmp_path / "lib" / "appio"
    directory.mkdir(parents=True)
    module = directory / "setup_boundary.py"
    module.write_text("# fixture\n", encoding="utf-8")

    assert (
        validate_trusted_runtime(
            (module, directory), metadata=FakeMetadata(default_uid=0), caller_uid=1000
        )
        is None
    )


def test_invoking_user_owned_module_is_refused(tmp_path: Path):
    module = tmp_path / "owned_by_caller.py"
    module.write_text("# fixture\n", encoding="utf-8")
    metadata = FakeMetadata(default_uid=0)
    metadata.override(module, uid=1000, mode=0o644)

    with pytest.raises(RuntimeTrustError) as caught:
        validate_trusted_runtime((module,), metadata=metadata, caller_uid=1000)
    assert caught.value.exit_code == 1
    assert "untrusted_runtime" in caught.value.reason


def test_writable_module_is_refused_even_when_root_owned(tmp_path: Path):
    module = tmp_path / "writable.py"
    module.write_text("# fixture\n", encoding="utf-8")
    metadata = FakeMetadata(default_uid=0)
    metadata.override(module, uid=0, mode=0o664)

    with pytest.raises(RuntimeTrustError) as caught:
        validate_trusted_runtime((module,), metadata=metadata, caller_uid=1000)
    assert "writable" in caught.value.reason


@pytest.mark.parametrize("mode", [0o775, 0o777, 0o757])
def test_writable_ancestor_directory_is_refused(tmp_path: Path, mode: int):
    # The threat: the module itself is root-owned and read-only, but a
    # group/world-writable directory above it lets the caller replace it.
    directory = tmp_path / "shared"
    directory.mkdir()
    module = directory / "module.py"
    module.write_text("# fixture\n", encoding="utf-8")
    metadata = FakeMetadata(default_uid=0)
    metadata.override(directory, uid=0, mode=mode)

    with pytest.raises(RuntimeTrustError) as caught:
        validate_trusted_runtime((module,), metadata=metadata, caller_uid=1000)
    assert "writable" in caught.value.reason


def test_symlinked_runtime_module_is_refused(tmp_path: Path):
    real = tmp_path / "real.py"
    real.write_text("# fixture\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)

    with pytest.raises(RuntimeTrustError) as caught:
        validate_trusted_runtime((link,), metadata=FakeMetadata(default_uid=0), caller_uid=1000)
    assert caught.value.exit_code == 1


def test_symlinked_runtime_ancestor_is_refused(tmp_path: Path):
    real_dir = tmp_path / "actual"
    real_dir.mkdir()
    (real_dir / "module.py").write_text("# fixture\n", encoding="utf-8")
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir)

    with pytest.raises(RuntimeTrustError):
        validate_trusted_runtime(
            (linked_dir / "module.py",), metadata=FakeMetadata(default_uid=0), caller_uid=1000
        )


def test_missing_runtime_path_is_refused(tmp_path: Path):
    with pytest.raises(RuntimeTrustError) as caught:
        validate_trusted_runtime(
            (tmp_path / "absent.py",), metadata=FakeMetadata(default_uid=0), caller_uid=1000
        )
    assert "missing" in caught.value.reason


@pytest.mark.parametrize("path", ["relative/module.py", "./module.py", "module.py"])
def test_relative_runtime_path_is_refused(path: str):
    with pytest.raises(RuntimeTrustError):
        validate_trusted_runtime((path,), metadata=FakeMetadata(default_uid=0), caller_uid=1000)


@pytest.mark.parametrize("caller_uid", [0, -1, 2**32, True, None])
def test_invalid_caller_uid_cannot_validate_a_runtime(caller_uid):
    with pytest.raises(RuntimeTrustError):
        validate_trusted_runtime((MODULE,), metadata=FakeMetadata(default_uid=0), caller_uid=caller_uid)


def test_empty_runtime_path_set_is_refused():
    with pytest.raises(RuntimeTrustError):
        validate_trusted_runtime((), metadata=FakeMetadata(default_uid=0), caller_uid=1000)


def test_accept_invocation_parses_identity_then_validates_the_runtime(tmp_path: Path):
    module = tmp_path / "appio" / "setup_boundary.py"
    module.parent.mkdir()
    module.write_text("# fixture\n", encoding="utf-8")
    deps = make_deps(tmp_path, metadata=FakeMetadata(default_uid=0))

    call = accept_invocation(
        ["/usr/bin/appio-setup", "enable"],
        deps=deps,
        environ={"PKEXEC_UID": "1000"},
        runtime_paths=(module,),
    )
    assert call.verb == "enable"
    assert call.identity.uid == 1000
    assert deps.metadata.calls, "the runtime closure must actually be inspected"


def test_accept_invocation_refuses_a_caller_owned_runtime(tmp_path: Path):
    # The threat: the verb and the beneficiary are valid, but the Python that
    # would run the fixed operation belongs to the invoking user.
    module = tmp_path / "appio" / "setup_boundary.py"
    module.parent.mkdir()
    module.write_text("# fixture\n", encoding="utf-8")
    metadata = FakeMetadata(default_uid=0)
    metadata.override(module, uid=1000, mode=0o644)
    deps = make_deps(tmp_path, metadata=metadata)

    with pytest.raises(RuntimeTrustError) as caught:
        accept_invocation(
            ["/usr/bin/appio-setup", "enable"],
            deps=deps,
            environ={"PKEXEC_UID": "1000"},
            runtime_paths=(module,),
        )
    assert caught.value.exit_code == 1
    assert "untrusted_runtime" in caught.value.reason


def test_accept_invocation_production_paths_include_interpreter_and_package():
    paths = production_runtime_paths()
    assert Path(os.path.realpath(sys.executable)) in paths
    assert MODULE in paths
    assert all(path.is_absolute() for path in paths)


# --------------------------------------------------------------------------- #
# Threat: fixed file targets only, opened O_NOFOLLOW, verified root-owned.
# --------------------------------------------------------------------------- #


def test_fixed_targets_are_the_two_design_dropins():
    assert {target.key for target in boundary.FIXED_TARGETS} == {
        "delegation-service-dropin",
        "user-manager-dropin",
    }
    assert boundary.FIXED_TARGET_PATHS == frozenset({DELEGATION_TARGET, MANAGER_TARGET})
    for path in boundary.FIXED_TARGET_PATHS:
        assert path.is_absolute()
        assert ".." not in path.parts


@pytest.mark.parametrize(
    "candidate",
    [
        Path("/etc/passwd"),
        Path("/etc/systemd/system/appio-evil.service"),
        Path("/etc/systemd/system/user@.service.d/../../evil.conf"),
        Path("/tmp/10-appio-io.conf"),
        Path("etc/systemd/user.conf.d/10-appio.conf"),
        "/etc/sudoers.d/appio",
        "/usr/bin/appio-setup",
        None,
        42,
    ],
)
def test_arbitrary_target_path_is_refused(root: Path, candidate):
    events = EventLog()
    deps = make_deps(root, events=events)
    with pytest.raises(TargetError) as caught:
        atomic_write(candidate, b"data\n", deps=deps)
    assert caught.value.exit_code == 2
    assert caught.value.reason == "target_not_fixed"
    assert events.names == []


def test_require_fixed_target_maps_values_onto_the_allowlist(root: Path):
    # The threat: a caller-supplied path that merely *looks* like the drop-in
    # (a prefix, a trailing slash, a relative spelling) must not be accepted by
    # an equality check that normalizes on the way in.
    assert boundary.require_fixed_target(str(DELEGATION_TARGET)).path == DELEGATION_TARGET
    assert boundary.require_fixed_target(MANAGER_TARGET).key == "user-manager-dropin"
    for target in boundary.FIXED_TARGETS:
        assert boundary.require_fixed_target(target) is target
    for near_miss in (
        str(DELEGATION_TARGET) + "/",
        str(DELEGATION_TARGET) + ".bak",
        str(DELEGATION_TARGET.parent),
        str(DELEGATION_TARGET).replace("/systemd/", "/systemd/./"),
        str(DELEGATION_TARGET) + "\x00",
        boundary.FixedTarget(key="delegation-service-dropin", path=Path("/tmp/x"), description=""),
    ):
        with pytest.raises(TargetError) as caught:
            boundary.require_fixed_target(near_miss)
        assert caught.value.exit_code == 2


def test_open_trusted_directory_verifies_each_component_below_the_injected_root(root: Path):
    # The threat: verifying only the leaf directory lets a writable ancestor be
    # swapped between the check and the write.
    metadata = FakeMetadata(default_uid=0)
    deps = make_deps(root, metadata=metadata)
    parent = rebase(root, DELEGATION_TARGET.parent)

    descriptor = boundary.open_trusted_directory(
        parent, base=Path(deps.root), metadata=metadata
    )
    try:
        assert os.fstat(descriptor).st_ino == os.stat(parent).st_ino
    finally:
        os.close(descriptor)
    assert len(metadata.calls) >= 3, "every component from the root down must be inspected"

    metadata.override(rebase(root, Path("/etc")), uid=1000, mode=0o755)
    with pytest.raises(TargetError) as caught:
        boundary.open_trusted_directory(parent, base=Path(deps.root), metadata=metadata)
    assert caught.value.exit_code == 1

    with pytest.raises(TargetError):
        boundary.open_trusted_directory(
            Path("/etc/passwd"), base=Path(deps.root), metadata=metadata
        )


def test_atomic_write_uses_mkstemp_fsync_rename_and_parent_directory_fsync(root: Path):
    # The design names the exact durable-write mechanism; a plain open/truncate
    # would expose a partial config file after a crash and could follow a
    # planted symlink.
    events = EventLog()
    written: list[tuple[int, int, int]] = []
    deps = make_deps(root, events=events, chown=lambda fd, uid, gid: written.append((fd, uid, gid)))
    path = rebase(root, DELEGATION_TARGET)

    result = atomic_write(DELEGATION_TARGET, b"[Service]\nIOAccounting=yes\n", deps=deps)

    assert result == path
    assert path.read_bytes() == b"[Service]\nIOAccounting=yes\n"
    assert [name for name in events.names if name in {"mkstemp", "write", "fsync", "chown", "rename", "fsync-dir"}] == [
        "mkstemp",
        "write",
        "chown",
        "fsync",
        "rename",
        "fsync-dir",
    ]
    assert events.index("write") > events.index("mkstemp")
    assert events.index("chown") > events.index("write")
    assert events.index("fsync") > events.index("chown")
    assert events.index("rename") > events.index("fsync")
    assert events.index("fsync-dir") > events.index("rename")
    assert written and written[0][1:] == (0, 0), "the new file must be root-owned before rename"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


def test_atomic_write_replaces_existing_content(root: Path):
    path = rebase(root, MANAGER_TARGET)
    path.write_bytes(b"old-content\n")

    atomic_write(MANAGER_TARGET, b"new-content\n", deps=make_deps(root))

    assert path.read_bytes() == b"new-content\n"
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


def test_symlinked_target_is_refused_and_victim_is_unchanged(root: Path):
    # The threat: plant a symlink at the fixed target so root's atomic write
    # overwrites an arbitrary file (/etc/shadow, another service unit).
    path = rebase(root, DELEGATION_TARGET)
    victim = root / "etc" / "victim.conf"
    victim.write_bytes(b"victim-original\n")
    path.symlink_to(victim)

    with pytest.raises(TargetError) as caught:
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=make_deps(root))
    assert caught.value.exit_code == 1
    assert "symlink" in caught.value.reason or "untrusted_target" in caught.value.reason
    assert victim.read_bytes() == b"victim-original\n"
    assert path.is_symlink()


def test_symlinked_target_parent_is_refused(root: Path):
    # The threat: replace the drop-in directory itself with a symlink to a
    # user-controlled directory so the write lands somewhere else.
    parent = rebase(root, DELEGATION_TARGET.parent)
    evil = root / "evil"
    evil.mkdir()
    shutil.rmtree(parent)
    parent.symlink_to(evil)

    with pytest.raises(TargetError) as caught:
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=make_deps(root))
    assert caught.value.exit_code == 1
    assert list(evil.iterdir()) == []


@pytest.mark.parametrize(
    "inject",
    ["target_uid", "target_mode", "parent_uid", "parent_mode", "ancestor_mode"],
)
def test_non_root_owned_or_writable_target_chain_is_refused(root: Path, inject: str):
    # The threat: root is tricked into replacing a file it does not own, or one
    # that a non-root account can rewrite between verification and rename.
    path = rebase(root, DELEGATION_TARGET)
    path.write_bytes(b"original\n")
    metadata = FakeMetadata(default_uid=0)
    if inject == "target_uid":
        metadata.override(path, uid=1000, mode=0o644)
    elif inject == "target_mode":
        metadata.override(path, uid=0, mode=0o666)
    elif inject == "parent_uid":
        metadata.override(path.parent, uid=1000, mode=0o755)
    elif inject == "parent_mode":
        metadata.override(path.parent, uid=0, mode=0o777)
    else:
        metadata.override(rebase(root, Path("/etc/systemd")), uid=0, mode=0o775)
    deps = make_deps(root, metadata=metadata)

    with pytest.raises(TargetError) as caught:
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=deps)

    assert caught.value.exit_code == 1
    assert path.read_bytes() == b"original\n"
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


def test_non_root_effective_uid_cannot_write(root: Path):
    events = EventLog()
    deps = make_deps(root, effective_uid=lambda: 1000, events=events)

    with pytest.raises(TargetError) as caught:
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=deps)
    assert caught.value.exit_code == 1
    assert caught.value.reason == "root_required"
    assert events.names == []
    assert list(rebase(root, DELEGATION_TARGET.parent).iterdir()) == []


def test_oversized_payload_is_refused_before_any_write(root: Path):
    events = EventLog()
    deps = make_deps(root, events=events)
    with pytest.raises(TargetError) as caught:
        atomic_write(DELEGATION_TARGET, b"x" * (MAX_TARGET_BYTES + 1), deps=deps)
    assert caught.value.exit_code == 2
    assert events.names == []


@pytest.mark.parametrize("payload", ["text not bytes", 7, bytearray(b"mutable")])
def test_payload_must_be_bounded_bytes(root: Path, payload):
    with pytest.raises(TargetError) as caught:
        atomic_write(DELEGATION_TARGET, payload, deps=make_deps(root))
    assert caught.value.exit_code == 2


@pytest.mark.parametrize("failure", ["write", "fsync", "chown"])
def test_failure_before_rename_preserves_original_and_removes_the_temp_file(root: Path, failure: str):
    path = rebase(root, DELEGATION_TARGET)
    path.write_bytes(b"original\n")
    events = EventLog(failures=(failure,))

    with pytest.raises(InjectedFault):
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=make_deps(root, events=events))

    assert path.read_bytes() == b"original\n"
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]
    assert "rename" not in events.names


def test_failure_after_rename_reports_a_durability_gap(root: Path):
    # rename() already replaced the target, so the only honest outcome is an
    # error: the content is new but the directory entry may not be durable.
    path = rebase(root, DELEGATION_TARGET)
    path.write_bytes(b"original\n")
    events = EventLog(failures=("fsync-dir",))

    with pytest.raises(InjectedFault):
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=make_deps(root, events=events))

    assert path.read_bytes() == b"payload\n"
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]
    assert events.index("rename") < events.index("fsync-dir")


def test_non_canonical_injected_root_is_refused(root: Path):
    # The threat: a root seam that accepts ".." spellings lets the destination
    # drift outside the tree the test (or a later caller) believes it pinned.
    canonical = make_deps(root)
    noncanonical = dataclasses.replace(canonical, root=root / "a" / ".." / "b")

    with pytest.raises(TargetError) as caught:
        resolve_target(DELEGATION_TARGET, deps=noncanonical)
    assert caught.value.exit_code == 1
    assert "root" in caught.value.reason

    events = EventLog()
    with pytest.raises(TargetError):
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=dataclasses.replace(noncanonical, event=events))
    assert events.names == []
    assert (root / "a").exists() is False


def test_atomic_write_refuses_a_parent_swapped_during_the_write(root: Path, monkeypatch):
    # The threat: the verified directory is renamed away and replaced between the
    # O_NOFOLLOW walk and mkstemp, so the temporary file -- and a later rename --
    # land in a directory nobody verified.
    path = rebase(root, DELEGATION_TARGET)
    path.write_bytes(b"original\n")
    parent = path.parent
    moved = parent.with_name(parent.name + ".moved")
    real_mkstemp = boundary.tempfile.mkstemp

    def swapping_mkstemp(*, prefix, suffix, dir):
        parent.rename(moved)
        parent.mkdir()
        return real_mkstemp(prefix=prefix, suffix=suffix, dir=str(parent))

    monkeypatch.setattr(boundary.tempfile, "mkstemp", swapping_mkstemp)

    with pytest.raises(TargetError) as caught:
        atomic_write(DELEGATION_TARGET, b"payload\n", deps=make_deps(root))

    assert "changed_during_write" in caught.value.reason
    assert (moved / path.name).read_bytes() == b"original\n"
    assert list(parent.iterdir()) == [], "the stray temporary file must be removed"


def test_atomic_write_confines_itself_to_the_injected_root(root: Path):
    # The threat: a "temporary root" seam that silently falls back to the host
    # path would let an ordinary test (or a caller) write real system config.
    host_path = DELEGATION_TARGET
    before = (host_path.exists(), host_path.parent.exists())
    result = atomic_write(DELEGATION_TARGET, b"payload\n", deps=make_deps(root))
    after = (host_path.exists(), host_path.parent.exists())

    assert result.is_relative_to(root)
    assert resolve_target(DELEGATION_TARGET, deps=make_deps(root)) == result
    assert after == before


# --------------------------------------------------------------------------- #
# Threat: no shell, no inherited environment, no unbounded child at root.
# --------------------------------------------------------------------------- #


def test_command_runner_requires_an_absolute_argv():
    runner = SubprocessCommandRunner()
    for argv in [("true",), ("/bin/true\x00",), (), ("/bin/true", "arg\x00")]:
        with pytest.raises(RunnerError) as caught:
            runner.run(tuple(argv), uid=os.getuid(), gid=os.getegid())
        assert caught.value.exit_code == 2


@pytest.mark.skipif(not Path("/bin/echo").exists(), reason="/bin/echo is unavailable")
def test_command_runner_executes_argv_without_a_shell(tmp_path: Path):
    # The threat: an argument that reaches a shell becomes an arbitrary root
    # command. argv must be passed literally, with no expansion at all.
    marker = tmp_path / "shell-ran"
    literal = f"$(touch {marker}) ; `touch {marker}` ; id"
    runner = SubprocessCommandRunner()

    result = runner.run(
        ("/bin/echo", literal),
        uid=os.getuid(),
        gid=os.getegid(),
        env={"LC_ALL": "C.UTF-8"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == literal
    assert not marker.exists()


def test_command_runner_module_never_asks_for_a_shell():
    source = MODULE.read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "os.popen" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert not (isinstance(node.func.value, ast.Name) and node.func.value.id == "os" and node.func.attr in {"system", "popen"})


def test_command_runner_builds_a_minimal_environment(monkeypatch):
    monkeypatch.setenv("APPIO_INHERITED_SECRET", "leaked")
    runner = SubprocessCommandRunner()

    result = runner.run(
        ("/usr/bin/env",), uid=os.getuid(), gid=os.getegid(), env={"APPIO_EXPLICIT": "1"}
    )

    assert result.returncode == 0
    assert "APPIO_INHERITED_SECRET" not in result.stdout
    assert "APPIO_EXPLICIT=1" in result.stdout
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in result.stdout
    assert "LANG=C.UTF-8" in result.stdout


@pytest.mark.parametrize(
    "env",
    [
        {"LD_PRELOAD": "/tmp/evil.so"},
        {"PYTHONPATH": "/tmp/evil"},
        {"BASH_ENV": "/tmp/evil.sh"},
        {"IFS": " "},
        {"lower_case": "x"},
        {"WITH NUL": "x"},
        {"PATH": "/tmp"},
        {"OK": "bad\nvalue"},
    ],
)
def test_command_runner_rejects_environment_injection(env):
    with pytest.raises(RunnerError) as caught:
        SubprocessCommandRunner().run(
            ("/usr/bin/env",), uid=os.getuid(), gid=os.getegid(), env=env
        )
    assert caught.value.exit_code == 2


def test_command_runner_times_out_and_reaps_the_child():
    # The threat: a hung root command leaves pkexec waiting forever (or leaves a
    # grandchild holding the terminal). A timeout must kill the process group and
    # return a distinct failure.
    runner = SubprocessCommandRunner()
    result = runner.run(
        (sys.executable, "-c", "import time; time.sleep(10)"),
        uid=os.getuid(),
        gid=os.getegid(),
        timeout_seconds=0.3,
    )
    assert result.returncode == TIMEOUT_RETURNCODE
    assert "timed out" in result.stderr


@pytest.mark.parametrize("timeout", [0, -1, 0.0, float("inf"), float("nan"), "5", True])
def test_command_runner_rejects_a_non_finite_or_nonpositive_timeout(timeout):
    with pytest.raises(RunnerError) as caught:
        SubprocessCommandRunner().run(
            ("/bin/true",), uid=os.getuid(), gid=os.getegid(), timeout_seconds=timeout
        )
    assert caught.value.exit_code == 2


def test_command_runner_bounds_captured_output():
    runner = SubprocessCommandRunner()
    result = runner.run(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"),
        uid=os.getuid(),
        gid=os.getegid(),
        timeout_seconds=10.0,
    )
    assert result.returncode == 0
    assert len(result.stdout) <= MAX_OUTPUT_BYTES + 64


@pytest.mark.skipif(os.geteuid() == 0, reason="identity drop succeeds for root")
def test_command_runner_identity_drop_requires_root():
    # A non-root invocation asking to run as uid 0 would fail in subprocess with a
    # bare PermissionError; the boundary must fail closed with its own error.
    with pytest.raises(RunnerError) as caught:
        SubprocessCommandRunner().run(("/bin/true",))
    assert caught.value.exit_code == 1
    assert caught.value.reason == "cannot_change_identity"


def test_command_result_is_a_plain_immutable_record():
    result = CommandResult(0, "out", "err")
    assert (result.returncode, result.stdout, result.stderr) == (0, "out", "err")
    assert result.ok is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.returncode = 1  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Threat: the polkit binding and the installed entry point must match the design.
# --------------------------------------------------------------------------- #


def test_shim_uses_the_isolated_system_interpreter():
    first_line = SHIM.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/python -I"


def test_shim_is_executable_and_not_group_or_world_writable():
    mode = stat.S_IMODE(SHIM.stat().st_mode)
    assert mode & 0o111
    assert not mode & 0o022


def run_shim(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SHIM), *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
        check=False,
    )


@pytest.mark.parametrize("args", [(), ("enable", "/tmp/evil"), ("enable", "alice", "root")])
def test_shim_rejects_wrong_argument_count_with_exit_two(args):
    result = run_shim(*args)
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize("verb", ["uninstall-delegation", "ENABLE", "enable ", "$(id)", "--root=/tmp"])
def test_shim_rejects_unknown_verbs_with_exit_two(verb):
    result = run_shim(verb)
    assert result.returncode == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="the helper is expected to be root here")
def test_shim_fails_closed_and_imports_nothing_without_root(tmp_path: Path):
    marker = tmp_path / "user-code-ran"
    fake_package = tmp_path / "appio"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('executed')\n", encoding="utf-8"
    )

    result = run_shim("enable", cwd=tmp_path)

    assert result.returncode == 1
    assert not marker.exists(), "the helper must not import user-controlled appio code"
    assert "root" in result.stderr.lower()


def test_shim_import_root_filter_drops_untrusted_entries(tmp_path: Path):
    # The threat: a caller-writable sys.path entry shadows a stdlib module that
    # appio.setup_service imports after the appio closure check.
    shim = load_shim_module()
    stdlib = os.path.dirname(os.__file__)
    entries = [
        "",
        ".",
        "relative/root",
        str(tmp_path),
        "/nonexistent-appio-import-root",
        stdlib,
    ]

    filtered = shim._trusted_import_roots(entries, os.geteuid())

    assert filtered == [stdlib], "only the root-owned, non-writable stdlib root may survive"
    assert shim._is_trusted_import_root(str(tmp_path), os.geteuid()) is False


def test_shim_import_root_filter_is_stderr_silent(tmp_path: Path, capsys):
    # A dropped import root is the normal case on an operator's machine, not an
    # error: writing to stderr makes a successful helper look failed and pollutes
    # the first-line error the polkit client displays.
    shim = load_shim_module()
    filtered = shim._trusted_import_roots(
        [str(tmp_path), str(tmp_path / "missing"), "", ".", "relative"], os.geteuid()
    )
    assert filtered == []
    assert capsys.readouterr().err == ""


def test_shim_import_root_filter_refuses_a_shadowing_plant(tmp_path: Path):
    shim = load_shim_module()
    plant = tmp_path / "plant"
    plant.mkdir()
    (plant / "subprocess.py").write_text("raise SystemExit('shadowed')\n")
    assert shim._is_trusted_import_root(str(plant), os.geteuid()) is False
    assert shim._trusted_import_roots([str(plant)], os.geteuid()) == []


def test_shim_drops_unsafe_import_roots_before_resolving_appio():
    source = SHIM.read_text(encoding="utf-8")
    assert source.index("sys.path[:] =") < source.index('find_spec("appio")')
    assert source.index('find_spec("appio")') < source.index("from appio.setup_service import")


def load_shim_module():
    """Import ``data/libexec/appio-setup`` (no .py suffix) as a module object."""

    loader = importlib.machinery.SourceFileLoader("appio_setup_shim", str(SHIM))
    spec = importlib.util.spec_from_loader("appio_setup_shim", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_shim_refuses_a_caller_owned_module(tmp_path: Path):
    shim = load_shim_module()
    module = tmp_path / "module.py"
    module.write_text("# fixture\n", encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        shim._verify_runtime_path(str(module), os.geteuid())

    assert caught.value.code == 1


def test_shim_opens_every_component_no_follow(tmp_path: Path, monkeypatch):
    # The threat: a symlink planted at a module path (or an ancestor) redirects
    # the verification to one inode while a later import loads another. With the
    # ownership policy stubbed out, O_NOFOLLOW is the only remaining defence, so
    # this fails if it is dropped.
    shim = load_shim_module()
    monkeypatch.setattr(shim, "_require_trusted", lambda *args, **kwargs: None)
    real = tmp_path / "real.py"
    real.write_text("# fixture\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)

    shim._verify_runtime_path(str(real), os.geteuid())

    with pytest.raises(SystemExit) as caught:
        shim._verify_runtime_path(str(link), os.geteuid())
    assert caught.value.code == 1


def test_shim_refuses_writable_or_non_root_components(tmp_path: Path):
    shim = load_shim_module()
    module = tmp_path / "module.py"
    module.write_text("# fixture\n", encoding="utf-8")
    descriptor = os.open(module, os.O_RDONLY)
    try:
        with pytest.raises(SystemExit) as caught:
            shim._require_trusted(descriptor, str(module), os.geteuid())
    finally:
        os.close(descriptor)
    assert caught.value.code == 1


def test_shim_rejects_non_canonical_pkexec_uid(monkeypatch):
    shim = load_shim_module()
    for raw in ["", " 1000", "1000 ", "0", "-1", "0x1", "1000.0", "١٠٠٠", "9" * 40]:
        with pytest.raises(SystemExit) as caught:
            shim._parse_caller_uid({"PKEXEC_UID": raw})
        assert caught.value.code == 1
    assert shim._parse_caller_uid({"PKEXEC_UID": "1000"}) == 1000


def test_shim_has_no_top_level_appio_import():
    tree = ast.parse(SHIM.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("appio") for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("appio")


def test_policy_binds_the_exact_action_and_helper_path():
    document = ET.parse(POLICY).getroot()
    actions = document.findall("action")
    assert len(actions) == 1
    action = actions[0]
    assert action.get("id") == POLICY_ACTION
    exec_paths = [
        element.text
        for element in action.findall("annotate")
        if element.get("key") == "org.freedesktop.policykit.exec.path"
    ]
    assert exec_paths == [POLICY_EXEC_PATH]
    assert SHIM.name == Path(POLICY_EXEC_PATH).name


def test_policy_requires_auth_admin_for_every_session_state():
    actions = ET.parse(POLICY).getroot().findall("action")
    defaults = actions[0].find("defaults")
    assert defaults is not None
    for field in ("allow_any", "allow_inactive", "allow_active"):
        element = defaults.find(field)
        assert element is not None and element.text == "auth_admin", field
    text = POLICY.read_text(encoding="utf-8")
    assert "auth_admin_keep" not in text
    assert "<action id=\"*\"" not in text
    assert ">yes<" not in text


def test_no_second_helper_entry_point_is_generated():
    # The helper must be the single fixed shim, not a setuptools console script
    # whose name or arguments a package metadata change could widen.
    project = tomllib.loads((WORKTREE / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    # The threat is that the PRIVILEGED HELPER becomes a console script, whose name or
    # arguments a metadata change could widen. Unprivileged entry points are allowed and
    # must stay inside the appio package; the helper stays the single fixed shim executed
    # via policykit exec.path (asserted above).
    assert "appio-setup" not in scripts
    assert "appio-helper" not in scripts
    assert "appio-setup" not in (WORKTREE / "pyproject.toml").read_text(encoding="utf-8")
    assert set(scripts) <= {"appio", "appio-iod"}, scripts
    for name, target in scripts.items():
        assert target.startswith("appio."), (name, target)


def test_no_test_seam_environment_variable_or_argv_option_exists():
    source = MODULE.read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv" not in source
    assert "--root" not in source
    assert "APPIO_TEST" not in source
    assert "APPIO_ROOT" not in source


def test_production_deps_pins_root_only_in_the_factory():
    deps = production_deps()
    assert deps.root == Path("/")
    assert deps.effective_uid is os.geteuid
    assert deps.metadata is os.fstat
    assert deps.chown is os.fchown
    assert isinstance(deps.runner, SubprocessCommandRunner)
    assert deps.clock_ns() > 0
    with pytest.raises(TypeError):
        SetupDeps(  # type: ignore[call-arg]
            runner=deps.runner,
            effective_uid=deps.effective_uid,
            getpwuid=deps.getpwuid,
            metadata=deps.metadata,
            chown=deps.chown,
            clock_ns=deps.clock_ns,
            event=deps.event,
        )
