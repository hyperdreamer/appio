"""Privilege boundary of the ``appio-setup`` polkit helper.

The helper runs as root because polkit authenticated the invoking user. That
makes *every* value the caller supplies a potential privilege selector, and the
only defensible contract is a fixed one:

* argv is exactly one verb out of :data:`VERBS`; anything else exits 2 before a
  file is opened or a command is started;
* the beneficiary is ``PKEXEC_UID`` (``getuid()`` inside pkexec is root),
  resolved through ``getpwuid``; ``$USER``, ``$HOME``, ``$PATH``, the environment
  and argv never select it;
* file targets are the two fixed configuration drop-ins in :data:`FIXED_TARGETS`.
  They and every parent directory are opened component by component with
  ``O_NOFOLLOW``, must be root-owned, and must not be writable by group or other;
* a write is ``mkstemp`` + ``write`` + ``fsync`` + ``fchown`` + ``rename`` +
  parent-directory ``fsync``, so a crash cannot leave a partial drop-in and a
  planted symlink cannot redirect the write;
* external commands are absolute argv with ``shell=False``, a minimal
  environment, and a finite timeout whose process group is killed and reaped;
* the interpreter and the whole imported ``appio`` module closure must be
  root-owned and non-writable, so the prompt cannot be turned into a loader for
  the invoking user's Python.

Nothing here is configured by argv or by an environment variable. The seams a
test needs -- an alternate filesystem root, a fake effective UID, trusted-file
metadata, the command runner and an operation callback -- exist only as fields
of :class:`SetupDeps`, which production code builds once through
:func:`production_deps`.
"""

from __future__ import annotations

import dataclasses
import errno
import math
import os
import posixpath
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Protocol, runtime_checkable

__all__ = [
    "BASE_ENV",
    "BoundaryError",
    "CommandResult",
    "CommandRunner",
    "FIXED_TARGETS",
    "FIXED_TARGET_PATHS",
    "FixedTarget",
    "Invocation",
    "InvocationError",
    "MAX_ARGV_ENTRIES",
    "MAX_ARG_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_PATH_BYTES",
    "MAX_TARGET_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "RunnerError",
    "RuntimeTrustError",
    "SetupDeps",
    "SetupIdentity",
    "SubprocessCommandRunner",
    "TIMEOUT_RETURNCODE",
    "TargetError",
    "UID_MAX",
    "UID_MIN",
    "VERBS",
    "accept_invocation",
    "atomic_write",
    "open_trusted_directory",
    "parse_invocation",
    "production_deps",
    "production_runtime_paths",
    "require_fixed_target",
    "resolve_target",
    "validate_trusted_runtime",
]

VERBS: frozenset[str] = frozenset(
    {
        "install-delegation",
        "enable",
        "disable",
        "status",
        "restart-user-manager",
        "export-history",
    }
)
"""The complete verb set. Any other word is an invalid invocation, not a fallback."""

UID_MIN = 1
UID_MAX = 2**32 - 2
"""``uid_t`` is 32-bit and ``(uid_t) -1`` is the error value, so 2**32-2 is the top."""

MAX_PATH_BYTES = 4096
MAX_TARGET_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ARG_BYTES = 4096
MAX_ARGV_ENTRIES = 64
MAX_TIMEOUT_SECONDS = 300.0
TIMEOUT_RETURNCODE = -int(signal.SIGKILL)

_TARGET_MODE = 0o644
_WRITABLE_BITS = 0o022
_MAX_COMPONENTS = 64

_CANONICAL_UID = re.compile(r"(?:0|[1-9][0-9]{0,9})", re.ASCII)
_SAFE_USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", re.ASCII)
_SAFE_ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]{0,63}", re.ASCII)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

BASE_ENV: Mapping[str, str] = MappingProxyType(
    {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
)
"""The complete inherited environment for a privileged child. Nothing is copied in."""

DENIED_ENV: frozenset[str] = frozenset(
    {
        "BASH_ENV",
        "BASHOPTS",
        "CDPATH",
        "ENV",
        "GLOBIGNORE",
        "IFS",
        "LD_AUDIT",
        "LD_DEBUG",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PATH",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "SHELLOPTS",
    }
)
"""Variables that would let a caller inject code or lookup paths into a root child."""


class BoundaryError(Exception):
    """Base of every fail-closed outcome; ``exit_code`` is the helper's exit status."""

    def __init__(self, exit_code: int, reason: str) -> None:
        super().__init__(reason)
        self.exit_code = exit_code
        self.reason = reason


class InvocationError(BoundaryError):
    """argv, effective UID or ``PKEXEC_UID`` did not satisfy the fixed contract."""


class RuntimeTrustError(BoundaryError):
    """The interpreter or an imported module is not root-owned and non-writable."""


class TargetError(BoundaryError):
    """A file target is not one of the fixed drop-ins, or is not safely owned."""


class RunnerError(BoundaryError):
    """A privileged command could not be validated or started."""


@dataclasses.dataclass(frozen=True)
class SetupIdentity:
    """The beneficiary of one invocation, derived only from ``PKEXEC_UID`` + NSS."""

    uid: int
    gid: int
    username: str
    home: Path


@dataclasses.dataclass(frozen=True)
class Invocation:
    verb: str
    identity: SetupIdentity


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclasses.dataclass(frozen=True)
class FixedTarget:
    """One configuration file the helper is allowed to write."""

    key: str
    path: Path
    description: str


FIXED_TARGETS: tuple[FixedTarget, ...] = (
    FixedTarget(
        key="delegation-service-dropin",
        path=Path("/etc/systemd/system/user@.service.d/10-appio-io.conf"),
        description="user-manager IOAccounting/Delegate drop-in",
    ),
    FixedTarget(
        key="user-manager-dropin",
        path=Path("/etc/systemd/user.conf.d/10-appio.conf"),
        description="user-manager DefaultIOAccounting/DefaultTasksMax drop-in",
    ),
    FixedTarget(
        key="sample-period-dropin",
        path=Path("/etc/systemd/system/appio-iod.service.d/20-sample-period.conf"),
        description="system service sample-period drop-in",
    ),
)

SAMPLE_PERIOD_PENDING = Path("/var/lib/appio/sample-period.pending.json")
SAMPLE_PERIOD_COMPLETE = Path("/var/lib/appio/sample-period.complete.json")
FIXED_STATE_TARGETS = frozenset((SAMPLE_PERIOD_PENDING, SAMPLE_PERIOD_COMPLETE))

FIXED_TARGET_PATHS: frozenset[Path] = frozenset(target.path for target in FIXED_TARGETS)


@runtime_checkable
class CommandRunner(Protocol):
    """One privileged external command, argv-only, never through a shell."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        uid: int = 0,
        gid: int = 0,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> CommandResult: ...


@dataclasses.dataclass(frozen=True)
class SetupDeps:
    """Every ambient fact the boundary needs, injected rather than read globally.

    ``root`` replaces ``/`` for file targets and exists only in tests;
    ``effective_uid``, ``metadata``, ``chown``, ``clock_ns`` and ``event`` are the
    other seams. None of them is reachable from argv or the environment.
    """

    root: Path
    runner: CommandRunner
    effective_uid: Callable[[], int]
    getpwuid: Callable[[int], object]
    metadata: Callable[[int], object]
    chown: Callable[[int, int, int], None]
    clock_ns: Callable[[], int]
    event: Callable[[str], None]


def production_deps() -> SetupDeps:
    """The only place where ``/``, real UID checks and real commands are bound."""

    return SetupDeps(
        root=Path("/"),
        runner=SubprocessCommandRunner(),
        effective_uid=os.geteuid,
        getpwuid=pwd.getpwuid,
        metadata=os.fstat,
        chown=os.fchown,
        clock_ns=time.monotonic_ns,
        event=lambda _name: None,
    )


# --------------------------------------------------------------------------- #
# Invocation parsing
# --------------------------------------------------------------------------- #


def parse_invocation(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    euid: int,
    getpwuid: Callable[[int], object],
) -> Invocation:
    """Validate the fixed argv and derive the beneficiary from ``PKEXEC_UID``.

    Order is the contract: argc, verb and the executable shape are rejected
    before the account database is consulted, and the account database is the
    only source of identity. The caller's environment is used for exactly one
    key, ``PKEXEC_UID``.
    """

    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise InvocationError(2, "invalid_verb_or_argument_count")
    if len(argv) != 2:
        raise InvocationError(2, "invalid_verb_or_argument_count")
    executable, verb = argv
    _require_executable(executable)
    if not isinstance(verb, str) or verb not in VERBS:
        raise InvocationError(2, "invalid_verb_or_argument_count")
    if euid != 0:
        raise InvocationError(1, "root_required")

    uid = _parse_pkexec_uid(environ)
    try:
        record = getpwuid(uid)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise InvocationError(1, "unknown_caller_uid") from exc
    if record is None:
        raise InvocationError(1, "unknown_caller_uid")

    identity = _identity_from_record(record, uid)
    return Invocation(verb=verb, identity=identity)


def accept_invocation(
    argv: Sequence[str],
    *,
    deps: SetupDeps,
    environ: Mapping[str, str],
    runtime_paths: Sequence[Path] | None = None,
) -> Invocation:
    """Parse the invocation, then prove the running code is not caller-controlled.

    Parsing comes first so an invalid argv never reaches the filesystem. The
    runtime check is repeated here (the shim performs its own) because the
    module closure that will dispatch privileged operations must be the one that
    was verified, not one some later import replaced.
    """

    invocation = parse_invocation(
        argv, environ=environ, euid=deps.effective_uid(), getpwuid=deps.getpwuid
    )
    paths = production_runtime_paths() if runtime_paths is None else tuple(runtime_paths)
    validate_trusted_runtime(
        paths, metadata=deps.metadata, caller_uid=invocation.identity.uid
    )
    return invocation


def _require_executable(executable: object) -> None:
    if not isinstance(executable, str):
        raise InvocationError(2, "invalid_verb_or_argument_count")
    if _CONTROL_CHARS.search(executable):
        raise InvocationError(2, "invalid_verb_or_argument_count")
    if len(executable.encode("utf-8")) > MAX_PATH_BYTES:
        raise InvocationError(2, "invalid_verb_or_argument_count")
    if not executable.startswith("/") or executable.startswith("//"):
        raise InvocationError(2, "invalid_verb_or_argument_count")
    if ".." in Path(executable).parts:
        raise InvocationError(2, "invalid_verb_or_argument_count")


def _parse_pkexec_uid(environ: Mapping[str, str]) -> int:
    raw = environ.get("PKEXEC_UID") if isinstance(environ, Mapping) else None
    if raw is None:
        raise InvocationError(1, "missing_pkexec_uid")
    if not isinstance(raw, str) or not _CANONICAL_UID.fullmatch(raw):
        raise InvocationError(1, "invalid_pkexec_uid")
    uid = int(raw)
    if uid == 0:
        # root has no delegated session and no export target under a home; the
        # fixed operation set exists for a non-root caller, so refuse instead of
        # widening it.
        raise InvocationError(1, "root_caller_rejected")
    if uid < UID_MIN or uid > UID_MAX:
        raise InvocationError(1, "invalid_pkexec_uid")
    return uid


def _identity_from_record(record: object, uid: int) -> SetupIdentity:
    pw_uid = getattr(record, "pw_uid", None)
    if not isinstance(pw_uid, int) or isinstance(pw_uid, bool):
        raise InvocationError(1, "unsafe_account_uid")
    if pw_uid != uid:
        raise InvocationError(1, "unsafe_account_uid_mismatch")

    name = getattr(record, "pw_name", None)
    if not isinstance(name, str) or not _SAFE_USERNAME.fullmatch(name):
        raise InvocationError(1, "unsafe_account_name")

    home = _safe_home(getattr(record, "pw_dir", None))

    gid = getattr(record, "pw_gid", None)
    if (
        not isinstance(gid, int)
        or isinstance(gid, bool)
        or gid < 0
        or gid > UID_MAX
    ):
        raise InvocationError(1, "unsafe_account_gid")

    return SetupIdentity(uid=uid, gid=gid, username=name, home=home)


def _safe_home(raw: object) -> Path:
    if not isinstance(raw, str) or raw == "" or raw == "/":
        raise InvocationError(1, "unsafe_account_home")
    if _CONTROL_CHARS.search(raw):
        raise InvocationError(1, "unsafe_account_home")
    if len(raw.encode("utf-8")) > MAX_PATH_BYTES:
        raise InvocationError(1, "unsafe_account_home")
    if not raw.startswith("/") or raw.startswith("//"):
        raise InvocationError(1, "unsafe_account_home")
    if posixpath.normpath(raw) != raw:
        # One canonical spelling only: ``/home/alice/`` and ``/home/../x`` are
        # rejected rather than silently normalised into a different target.
        raise InvocationError(1, "unsafe_account_home")
    return Path(raw)


# --------------------------------------------------------------------------- #
# Trusted runtime closure (loader defence)
# --------------------------------------------------------------------------- #


def production_runtime_paths() -> tuple[Path, ...]:
    """The interpreter and every module file of the imported ``appio`` package.

    The interpreter is realpath-resolved because the platform is allowed to
    package ``/usr/bin/python`` as a root-owned symlink; the resolved binary and
    its ancestry are what get verified. Module paths are *not* resolved, so a
    symlinked module is visible to the ``O_NOFOLLOW`` walk and refused.
    """

    module = Path(os.path.abspath(__file__))
    package = module.parent
    paths: list[Path] = [Path(os.path.realpath(sys.executable)), module, package]
    for pattern in ("*.py", "*.pyc"):
        for candidate in sorted(package.rglob(pattern)):
            if candidate not in paths:
                paths.append(candidate)
    return tuple(paths)


def validate_trusted_runtime(
    paths: Sequence[Path],
    *,
    metadata: Callable[[int], object],
    caller_uid: int,
) -> None:
    """Refuse to run privileged code from a caller-owned or caller-writable tree.

    Every path is opened component by component from ``/`` with ``O_NOFOLLOW``,
    and every component plus the final file must be root-owned with no group or
    other write bit. ``metadata`` receives the pinned descriptor, so tests can
    assert the policy without owning a root filesystem.
    """

    if (
        not isinstance(caller_uid, int)
        or isinstance(caller_uid, bool)
        or caller_uid < UID_MIN
        or caller_uid > UID_MAX
    ):
        raise RuntimeTrustError(1, "untrusted_runtime:invalid_caller_uid")
    entries = tuple(paths)
    if not entries:
        raise RuntimeTrustError(1, "untrusted_runtime:no_paths")

    for entry in entries:
        path = _absolute_path(entry, RuntimeTrustError, "untrusted_runtime")
        if path == Path("/"):
            descriptor = _open_component(
                Path("/"), Path("/"), metadata, caller_uid, RuntimeTrustError, "untrusted_runtime",
                directory=True,
            )
            os.close(descriptor)
            continue
        descriptor = _open_directory_chain(
            path.parent,
            base=Path("/"),
            metadata=metadata,
            caller_uid=caller_uid,
            error_type=RuntimeTrustError,
            prefix="untrusted_runtime",
        )
        try:
            try:
                handle = os.open(
                    path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise RuntimeTrustError(1, f"untrusted_runtime:{_open_word(exc)}") from exc
            try:
                real = os.fstat(handle)
                if not (stat.S_ISREG(real.st_mode) or stat.S_ISDIR(real.st_mode)):
                    raise RuntimeTrustError(1, "untrusted_runtime:not_a_regular_file")
                _require_trusted(
                    metadata(handle),
                    error_type=RuntimeTrustError,
                    prefix="untrusted_runtime",
                    caller_uid=caller_uid,
                )
            finally:
                os.close(handle)
        finally:
            os.close(descriptor)


# --------------------------------------------------------------------------- #
# Fixed targets and durable writes
# --------------------------------------------------------------------------- #


def require_fixed_target(candidate: object) -> FixedTarget:
    """Map a caller value onto one of the two fixed drop-ins or refuse it."""

    if isinstance(candidate, FixedTarget):
        for target in FIXED_TARGETS:
            if target.key == candidate.key and target.path == candidate.path:
                return target
        raise TargetError(2, "target_not_fixed")
    try:
        path = _absolute_path(candidate, TargetError, "target")
    except TargetError as exc:
        raise TargetError(2, "target_not_fixed") from exc
    for target in FIXED_TARGETS:
        if target.path == path:
            return target
    raise TargetError(2, "target_not_fixed")


def resolve_target(candidate: object, *, deps: SetupDeps) -> Path:
    """Resolve a fixed target beneath ``deps.root`` (``/`` in production)."""

    fixed = require_fixed_target(candidate)
    # ``deps.root`` is a seam, not a caller input, but it is still held to the
    # same one-canonical-spelling rule as every other path: a root containing
    # ".." or a relative root must not silently retarget the write.
    root = _absolute_path(deps.root, TargetError, "root")
    return root / fixed.path.relative_to("/")


def open_trusted_directory(
    path: object,
    *,
    base: Path,
    metadata: Callable[[int], object],
    caller_uid: int | None = None,
) -> int:
    """Open ``path`` component by component below ``base``, verifying ownership.

    Returns an owned descriptor the caller must close. ``base`` is the injected
    filesystem root, so production passes ``/`` and tests pass their temporary
    tree; every component *below* ``base`` is opened with ``O_NOFOLLOW``.
    """

    return _open_directory_chain(
        _absolute_path(path, TargetError, "untrusted_parent"),
        base=_absolute_path(base, TargetError, "untrusted_parent"),
        metadata=metadata,
        caller_uid=caller_uid,
        error_type=TargetError,
        prefix="untrusted_parent",
    )


def atomic_write(candidate: object, data: bytes, *, deps: SetupDeps) -> Path:
    """Durably replace one fixed drop-in; no other path is writable.

    The mechanism is load-bearing: ``mkstemp`` in the verified parent, write,
    ``fsync``, ``fchown(0, 0)``, ``rename`` within that same directory, then
    ``fsync`` of the parent directory.
    """

    fixed = require_fixed_target(candidate)
    payload = _require_payload(data)
    if deps.effective_uid() != 0:
        raise TargetError(1, "root_required")

    destination = resolve_target(fixed, deps=deps)
    events = deps.event

    parent = _open_directory_chain(
        destination.parent,
        base=Path(deps.root),
        metadata=deps.metadata,
        caller_uid=None,
        error_type=TargetError,
        prefix="untrusted_parent",
    )
    try:
        events("write-start")
        events("parent-opened")
        _verify_existing_target(parent, destination.name, deps.metadata)
        events("target-verified")

        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        try:
            events("mkstemp")
            _require_same_directory(parent, destination.parent)
            try:
                _write_all(descriptor, payload)
                events("write")
                # Ownership and mode are inode metadata: set them before the
                # fsync so the single flush that precedes rename also commits
                # them, rather than relying on a later implicit writeback.
                os.fchmod(descriptor, _TARGET_MODE)
                deps.chown(descriptor, 0, 0)
                events("chown")
                os.fsync(descriptor)
                events("fsync")
            finally:
                os.close(descriptor)
            # Rename within the already-verified directory, by descriptor, so the
            # destination pathname is never re-resolved through a fresh lookup.
            os.rename(
                os.path.basename(temporary),
                destination.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            events("rename")
            os.fsync(parent)
            events("fsync-dir")
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
                events("unlink-temp")
    finally:
        os.close(parent)
    return destination


def _require_payload(data: object) -> bytes:
    if not isinstance(data, bytes):
        raise TargetError(2, "payload_not_bytes")
    if len(data) > MAX_TARGET_BYTES:
        raise TargetError(2, "payload_too_large")
    return data


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise TargetError(1, "short_write")
        view = view[written:]


def _verify_existing_target(parent: int, name: str, metadata: Callable[[int], object]) -> None:
    try:
        handle = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TargetError(1, f"untrusted_target:{_open_word(exc)}") from exc
    try:
        real = os.fstat(handle)
        if not stat.S_ISREG(real.st_mode):
            raise TargetError(1, "untrusted_target:not_a_regular_file")
        _require_trusted(
            metadata(handle), error_type=TargetError, prefix="untrusted_target", caller_uid=None
        )
    finally:
        os.close(handle)


def _require_same_directory(descriptor: int, path: Path) -> None:
    opened = os.fstat(descriptor)
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise TargetError(1, f"untrusted_parent:{_open_word(exc)}") from exc
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise TargetError(1, "untrusted_parent:changed_during_write")


# --------------------------------------------------------------------------- #
# Verified filesystem walk
# --------------------------------------------------------------------------- #


def _open_directory_chain(
    path: Path,
    *,
    base: Path,
    metadata: Callable[[int], object],
    caller_uid: int | None,
    error_type: type[BoundaryError],
    prefix: str,
) -> int:
    if path == base:
        relative = Path()
    else:
        try:
            relative = path.relative_to(base)
        except ValueError as exc:
            raise error_type(1, f"{prefix}:outside_root") from exc
    if len(relative.parts) > _MAX_COMPONENTS:
        raise error_type(1, f"{prefix}:too_deep")
    descriptor = _open_component(
        base, base, metadata, caller_uid, error_type, prefix, directory=True
    )
    try:
        for part in relative.parts:
            child = _open_component(
                Path(part), base, metadata, caller_uid, error_type, prefix,
                directory=True, dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_component(
    path: Path,
    base: Path,
    metadata: Callable[[int], object],
    caller_uid: int | None,
    error_type: type[BoundaryError],
    prefix: str,
    *,
    directory: bool,
    dir_fd: int | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    try:
        if dir_fd is None:
            descriptor = os.open(path, flags)
        else:
            descriptor = os.open(path.name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise error_type(1, f"{prefix}:{_open_word(exc)}") from exc
    try:
        real = os.fstat(descriptor)
        if directory and not stat.S_ISDIR(real.st_mode):
            raise error_type(1, f"{prefix}:not_a_directory")
        _require_trusted(
            metadata(descriptor),
            error_type=error_type,
            prefix=prefix,
            caller_uid=caller_uid,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_trusted(
    info: object,
    *,
    error_type: type[BoundaryError],
    prefix: str,
    caller_uid: int | None,
) -> None:
    uid = getattr(info, "st_uid", None)
    mode = getattr(info, "st_mode", None)
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or not isinstance(mode, int)
        or isinstance(mode, bool)
    ):
        raise error_type(1, f"{prefix}:unreadable_metadata")
    if uid != 0:
        raise error_type(1, f"{prefix}:not_root_owned")
    if caller_uid is not None and uid == caller_uid:
        raise error_type(1, f"{prefix}:caller_owned")
    if stat.S_IMODE(mode) & _WRITABLE_BITS:
        raise error_type(1, f"{prefix}:writable")


def _open_word(exc: OSError) -> str:
    return {
        errno.ELOOP: "symlink",
        errno.ENOTDIR: "not_a_directory",
        errno.ENOENT: "missing",
        errno.EACCES: "permission_denied",
        errno.EPERM: "permission_denied",
    }.get(exc.errno or 0, f"errno_{exc.errno}")


def _absolute_path(
    candidate: object, error_type: type[BoundaryError], prefix: str
) -> Path:
    if isinstance(candidate, FixedTarget):
        candidate = candidate.path
    if not isinstance(candidate, (str, os.PathLike)):
        raise error_type(1, f"{prefix}:invalid_path")
    raw = os.fspath(candidate)
    if isinstance(raw, bytes):
        raise error_type(1, f"{prefix}:invalid_path")
    text = str(raw)
    if not text or _CONTROL_CHARS.search(text):
        raise error_type(1, f"{prefix}:invalid_path")
    if len(text.encode("utf-8")) > MAX_PATH_BYTES:
        raise error_type(1, f"{prefix}:path_too_long")
    if not text.startswith("/") or text.startswith("//"):
        raise error_type(1, f"{prefix}:not_absolute")
    if ".." in Path(text).parts:
        raise error_type(1, f"{prefix}:not_normalized")
    if str(Path(text)) != text:
        # One canonical spelling only: ``x/``, ``x//y`` and ``x/./y`` must not be
        # accepted as aliases of an allowlisted path.
        raise error_type(1, f"{prefix}:not_normalized")
    return Path(text)


# --------------------------------------------------------------------------- #
# Privileged command execution
# --------------------------------------------------------------------------- #


class SubprocessCommandRunner:
    """The production runner: argv-only, no shell, bounded output and time."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        uid: int = 0,
        gid: int = 0,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> CommandResult:
        arguments = _require_argv(argv)
        environment = _sanitize_environment(env)
        seconds = _require_timeout(timeout_seconds)
        user_id, group_id = _require_ids(uid, gid)

        options: dict[str, object] = {}
        if (user_id, group_id) != (os.geteuid(), os.getegid()):
            if os.geteuid() != 0:
                raise RunnerError(1, "cannot_change_identity")
            options = {"user": user_id, "group": group_id, "extra_groups": []}

        with tempfile.TemporaryFile() as captured_out, tempfile.TemporaryFile() as captured_err:
            try:
                process = subprocess.Popen(
                    list(arguments),
                    stdin=subprocess.DEVNULL,
                    stdout=captured_out,
                    stderr=captured_err,
                    env=dict(environment),
                    shell=False,
                    close_fds=True,
                    start_new_session=True,
                    **options,
                )
            except OSError as exc:
                raise RunnerError(1, f"spawn_failed:{_open_word(exc)}") from exc

            timed_out = False
            try:
                process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate(process)

            captured_out.seek(0)
            captured_err.seek(0)
            stdout = _read_bounded(captured_out)
            stderr = _read_bounded(captured_err)

        if timed_out:
            stderr = (
                f"{stderr}\nappio-setup: command timed out after {seconds:g}s".strip()
            )
            return CommandResult(TIMEOUT_RETURNCODE, stdout, stderr)
        return CommandResult(int(process.returncode), stdout, stderr)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    # start_new_session=True makes the child a process-group leader, so killing
    # the group also kills anything it started; waiting reaps the direct child.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _require_argv(argv: object) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise RunnerError(2, "argv_must_be_a_sequence")
    items = tuple(argv)
    if not items or len(items) > MAX_ARGV_ENTRIES:
        raise RunnerError(2, "invalid_argv_length")
    for item in items:
        if not isinstance(item, str) or item == "":
            raise RunnerError(2, "invalid_argv_entry")
        if _CONTROL_CHARS.search(item):
            raise RunnerError(2, "invalid_argv_entry")
        if len(item.encode("utf-8")) > MAX_ARG_BYTES:
            raise RunnerError(2, "argv_entry_too_long")
    executable = items[0]
    if not executable.startswith("/") or executable.startswith("//"):
        raise RunnerError(2, "argv0_not_absolute")
    if ".." in Path(executable).parts:
        raise RunnerError(2, "argv0_not_normalized")
    return items


def _sanitize_environment(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is None:
        return dict(BASE_ENV)
    if not isinstance(env, Mapping):
        raise RunnerError(2, "invalid_environment")
    merged = dict(BASE_ENV)
    for key, value in env.items():
        if not isinstance(key, str) or not _SAFE_ENV_KEY.fullmatch(key):
            raise RunnerError(2, "invalid_environment_key")
        if key in DENIED_ENV:
            raise RunnerError(2, "denied_environment_key")
        if not isinstance(value, str):
            raise RunnerError(2, "invalid_environment_value")
        if _CONTROL_CHARS.search(value) or len(value.encode("utf-8")) > MAX_ARG_BYTES:
            raise RunnerError(2, "invalid_environment_value")
        merged[key] = value
    return merged


def _require_timeout(timeout_seconds: object) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > MAX_TIMEOUT_SECONDS
    ):
        raise RunnerError(2, "invalid_timeout")
    return float(timeout_seconds)


def _require_ids(uid: object, gid: object) -> tuple[int, int]:
    for value in (uid, gid):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > UID_MAX
        ):
            raise RunnerError(2, "invalid_identity")
    return int(uid), int(gid)


def _read_bounded(handle: BinaryIO) -> str:
    raw = handle.read(MAX_OUTPUT_BYTES + 1)
    text = raw.decode("utf-8", "replace")
    if len(raw) > MAX_OUTPUT_BYTES:
        text = f"{text[:MAX_OUTPUT_BYTES]}\n[output truncated]"
    return text
