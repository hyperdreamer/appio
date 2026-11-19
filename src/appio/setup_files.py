"""Safe, root-owned file operations for the ``appio-setup`` helper.

The helper runs as root, so a file operation here is a privilege operation even
when it looks like ``open``/``write``. Three properties are therefore structural
rather than advisory:

* **Fixed targets only.** :data:`FIXED_CONFIG_TARGETS` are the three design
  drop-ins; :data:`STATE_ROOT` and :data:`LOCK_ROOT` are the helper's own
  private roots. Nothing else is addressable, so argv can never become a path.
* **Pinned traversal.** Every path is walked component by component from the
  injected :attr:`SetupDeps.root` with ``O_NOFOLLOW``/``O_DIRECTORY``, and each
  component must be root-owned with no group or other write bit. Symlinked,
  attacker-owned or writable ancestors are refused before a descriptor is used
  for anything else.
* **Durable replace, never truncate.** A write creates an exclusive temporary in
  the already-verified parent, writes it completely, sets its fixed mode and
  ownership, ``fsync``s it, renames it *by descriptor* within that same parent,
  and finally ``fsync``s the parent directory. A failure before the rename
  leaves the previous file byte-identical.

The same layer owns the shared ``setup-state.json`` gate and the helper lock, so
"delegation installed but activation pending" is durable evidence rather than a
value the caller can invent.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

from .setup_boundary import (
    FIXED_STATE_TARGETS,
    FIXED_TARGETS,
    MAX_TARGET_BYTES,
    SAMPLE_PERIOD_COMPLETE,
    SAMPLE_PERIOD_PENDING,
    SetupDeps,
)

__all__ = [
    "FIXED_CONFIG_TARGETS",
    "FIXED_STATE_TARGETS",
    "SAMPLE_PERIOD_DROPIN",
    "SAMPLE_PERIOD_PENDING",
    "SAMPLE_PERIOD_COMPLETE",
    "MAX_PERIOD_JOURNAL_BYTES",
    "PeriodJournalRecord",
    "encode_period_journal",
    "decode_period_journal",
    "publish_period_pending",
    "publish_period_complete",
    "remove_period_pending",
    "sample_period_payload",
    "parse_sample_period_dropin",
    "LOCK_FILE",
    "LOCK_ROOT",
    "MAX_COMPONENTS",
    "PROBE_ROOT",
    "SafeRootFS",
    "SafeRootFSError",
    "SERVICE_DROPIN",
    "STAGING_ROOT",
    "STATE_FILE",
    "STATE_ROOT",
    "USER_MANAGER_DROPIN",
    "manager_dropin_payload",
    "parse_manager_dropin",
    "parse_service_dropin",
    "service_dropin_payload",
]

# The boundary is the single declaration of the fixed configuration targets.
_TARGETS_BY_KEY = {target.key: target.path for target in FIXED_TARGETS}
SERVICE_DROPIN: Path = _TARGETS_BY_KEY["delegation-service-dropin"]
USER_MANAGER_DROPIN: Path = _TARGETS_BY_KEY["user-manager-dropin"]
SAMPLE_PERIOD_DROPIN: Path = _TARGETS_BY_KEY["sample-period-dropin"]
FIXED_CONFIG_TARGETS: frozenset[Path] = frozenset(
    (SERVICE_DROPIN, USER_MANAGER_DROPIN, SAMPLE_PERIOD_DROPIN)
)
MAX_PERIOD_JOURNAL_BYTES = 1024

STATE_ROOT = Path("/var/lib/appio")
STATE_FILE = STATE_ROOT / "setup-state.json"
PROBE_ROOT = STATE_ROOT / "probes"
STAGING_ROOT = STATE_ROOT / "staging"
LOCK_ROOT = Path("/run/appio-setup")
LOCK_FILE = LOCK_ROOT / "helper.lock"

ADDRESSABLE_ROOTS: tuple[Path, ...] = (STATE_ROOT, LOCK_ROOT)

MAX_COMPONENTS = 8
"""Deepest fixed relative path (``probes/<uid>/<name>`` is three)."""

MAX_TEMP_ATTEMPTS = 32
_FIXED_CONFIG_MODE = 0o644
_WRITABLE_BITS = 0o022
_MAX_PATH_BYTES = 4096
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

_CONTROLLER = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]", re.ASCII)


class SafeRootFSError(PermissionError):
    """A fail-closed refusal. Subclasses ``PermissionError`` on purpose.

    Task 8's contract test asserts ``PermissionError`` for a symlinked target,
    and every refusal here is the same class of event: the caller asked for a
    write the fixed policy does not permit.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class SafeRootFS:
    """The helper's only filesystem primitive, bound to injected dependencies."""

    deps: SetupDeps
    _hold: threading.local = dataclasses.field(
        default_factory=threading.local, compare=False, repr=False
    )

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    def atomic_write(
        self,
        path: str,
        content: bytes,
        *,
        mode: int = _FIXED_CONFIG_MODE,
        owner: tuple[int, int] | None = None,
    ) -> None:
        """Durably replace one fixed file; never truncate the live target.

        ``owner`` defaults to root. A non-root owner is permitted only beneath
        :data:`PROBE_ROOT`, where Task 9 pre-creates the caller-owned probe file;
        a caller-owned root configuration file would be a privilege regression.
        """

        target_fixed = _require_fixed_path(path, "target")
        payload = _require_payload(content)
        file_mode = _require_mode(mode)
        uid, gid = _require_owner(owner, target_fixed)
        if target_fixed in FIXED_CONFIG_TARGETS and file_mode != _FIXED_CONFIG_MODE:
            raise SafeRootFSError("unsafe_mode:config_target_requires_0644")
        if target_fixed in FIXED_STATE_TARGETS:
            if file_mode != 0o600:
                raise SafeRootFSError("unsafe_mode:period_journal_requires_0600")
            decode_period_journal(payload)
        if self.deps.effective_uid() != 0:
            raise SafeRootFSError("root_required")

        destination = self._resolve(target_fixed)
        events = self.deps.event
        parent = self._open_parent(
            destination.parent,
            sync_created=(target_fixed in FIXED_STATE_TARGETS or target_fixed == SAMPLE_PERIOD_DROPIN),
        )
        temporary: str | None = None
        descriptor: int | None = None
        try:
            events("parent-opened")
            existing = self._read_existing(parent, destination.name, target_fixed)
            events("target-verified")
            if existing is not None and target_fixed in FIXED_CONFIG_TARGETS:
                _require_expected_content(target_fixed, existing)
            descriptor, temporary = self._create_temporary(parent, destination.name)
            events("mkstemp")
            _require_same_directory(parent, destination.parent)
            try:
                events("write")
                _write_all(descriptor, payload)
                events("chown")
                os.fchmod(descriptor, file_mode)
                self.deps.chown(descriptor, uid, gid)
                events("fsync")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
                descriptor = None
            # The event precedes the syscall so an injected failure models
            # "the rename did not happen", not "it happened and then reported
            # failure". Publication must be the success boundary.
            events("rename")
            os.rename(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            temporary = None
            events("fsync-dir")
            os.fsync(parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                else:
                    events("unlink-temp")
            os.close(parent)

    def read_bounded(
        self, path: str | Path, *, max_bytes: int, create_parents: bool = True
    ) -> bytes:
        """Read a fixed file through pinned, trusted descriptors."""

        target_fixed = _require_fixed_path(path, "target")
        limit = _require_bound(max_bytes)
        if target_fixed in FIXED_STATE_TARGETS:
            limit = min(limit, MAX_PERIOD_JOURNAL_BYTES)
        destination = self._resolve(target_fixed)
        parent = self._open_parent(
            destination.parent,
            create_parents=create_parents,
            sync_created=create_parents and (
                target_fixed in FIXED_STATE_TARGETS or target_fixed == SAMPLE_PERIOD_DROPIN
            ),
        )
        try:
            descriptor = _open_readonly(parent, destination.name)
            try:
                _require_regular_single_link(descriptor, "untrusted_target")
                self._require_trusted_file(descriptor, "untrusted_target", target_fixed)
                return _read_all(descriptor, limit)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def prepare_period_directories(self) -> None:
        """Prepare and durably sync the fixed period target and state directories."""
        if self.deps.effective_uid() != 0:
            raise SafeRootFSError("root_required")
        directories = (
            SAMPLE_PERIOD_DROPIN.parent.parent,
            STATE_ROOT.parent,
            SAMPLE_PERIOD_DROPIN.parent,
            STATE_ROOT,
        )
        for directory in directories:
            resolved = self._resolve(directory)
            descriptor = self._open_parent(resolved, sync_created=True)
            try:
                _require_same_directory(descriptor, resolved)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def sync_period_parent(self, path: str | Path) -> None:
        """Repeat a period file's parent-directory durability boundary."""
        target = _canonical_absolute(path, "target")
        if target not in FIXED_STATE_TARGETS and target != SAMPLE_PERIOD_DROPIN:
            raise SafeRootFSError("target:not_fixed_path")
        if self.deps.effective_uid() != 0:
            raise SafeRootFSError("root_required")
        parent_path = self._resolve(target.parent)
        parent = self._open_parent(parent_path, create_parents=False)
        try:
            _require_same_directory(parent, parent_path)
            os.fsync(parent)
        finally:
            os.close(parent)

    def remove_period_pending(self) -> None:
        """Remove pending evidence and durably record its absence."""
        if self.deps.effective_uid() != 0:
            raise SafeRootFSError("root_required")
        destination = self._resolve(SAMPLE_PERIOD_PENDING)
        parent = self._open_parent(destination.parent, create_parents=False)
        try:
            existing = self._read_existing(parent, destination.name, SAMPLE_PERIOD_PENDING)
            _require_same_directory(parent, destination.parent)
            if existing is not None:
                os.unlink(destination.name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize helper operations under the fixed root-owned lock.

        Reentrant within one helper instance on one thread, so ``set_pending``
        can be composed inside ``install_delegation`` without self-deadlocking a
        per-open-file-description ``flock``. A second helper instance always
        contends, which is the property that matters.
        """

        depth = getattr(self._hold, "depth", 0)
        if depth:
            self._hold.depth = depth + 1
            try:
                yield
            finally:
                self._hold.depth -= 1
            return

        lock_path = _require_fixed_path(str(LOCK_FILE), "lock")
        if self.deps.effective_uid() != 0:
            raise SafeRootFSError("root_required")
        destination = self._resolve(lock_path)
        parent = self._open_parent(destination.parent)
        try:
            descriptor = self._open_lock_file(parent, destination.name)
        finally:
            os.close(parent)

        acquired = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            acquired = True
            self.deps.event("lock")
            # Claim re-entrancy only after the lock is genuinely held and no
            # earlier step has raised: a stale depth would silently skip the
            # flock on the next nested call.
            self._hold.depth = 1
            try:
                yield
            finally:
                self._hold.depth = 0
        finally:
            if acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)

    # ------------------------------------------------------------------ #
    # Traversal
    # ------------------------------------------------------------------ #

    def _resolve(self, fixed: Path) -> Path:
        root = _canonical_absolute(self.deps.root, "root")
        return root / fixed.relative_to("/")

    def _open_parent(
        self, parent: Path, *, create_parents: bool = True, sync_created: bool = False
    ) -> int:
        root = _canonical_absolute(self.deps.root, "root")
        try:
            relative = parent.relative_to(root)
        except ValueError as exc:
            raise SafeRootFSError("outside_root") from exc
        if len(relative.parts) > MAX_COMPONENTS:
            raise SafeRootFSError("too_deep")

        descriptor = self._open_anchor(root)
        fixed_current = Path("/")
        try:
            for part in relative.parts:
                # Directory modes belong to the production path, not the
                # injected-root spelling: /var/lib/appio is 0711 whether the
                # seam points at "/" or at a temporary tree.
                fixed_current = fixed_current / part
                descriptor = self._open_or_create_child(
                    descriptor, part, fixed_current,
                    create=create_parents, sync_created=sync_created,
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_anchor(self, root: Path) -> int:
        try:
            descriptor = os.open(root, _DIR_FLAGS)
        except OSError as exc:
            raise SafeRootFSError(f"root:{_open_word(exc)}") from exc
        try:
            self._verify_directory(descriptor, "root")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_or_create_child(
        self, parent: int, part: str, full: Path, *, create: bool, sync_created: bool
    ) -> int:
        flags = _DIR_FLAGS
        created = False
        raced = False
        fixed_mode = _fixed_directory_mode(full)
        try:
            child = os.open(part, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(part, fixed_mode, dir_fd=parent)
                created = True
            except FileExistsError:
                raced = True
            except OSError as exc:
                raise SafeRootFSError(f"create_parent:{_open_word(exc)}") from exc
            if created and sync_created:
                os.fsync(parent)
            try:
                child = os.open(part, flags, dir_fd=parent)
            except OSError as exc:
                raise SafeRootFSError(f"untrusted_parent:{_open_word(exc)}") from exc
        except OSError as exc:
            raise SafeRootFSError(
                f"untrusted_parent:{_open_word(exc, parent=parent, name=part)}"
            ) from exc
        try:
            self._verify_directory(
                child,
                "untrusted_parent",
                expected_mode=fixed_mode if raced else None,
            )
            if not raced:
                # Normalize only after the directory has passed the trust checks.
                os.fchmod(child, fixed_mode)
                self._verify_directory(child, "untrusted_parent", expected_mode=fixed_mode)
            # A failed mkdir fsync may have left an existing directory behind.
            # Re-sync its parent on the next period traversal as well.
            if sync_created and not created:
                os.fsync(parent)
        except BaseException:
            os.close(child)
            raise
        os.close(parent)
        return child

    def _verify_directory(
        self, descriptor: int, prefix: str, *, expected_mode: int | None = None
    ) -> None:
        real = os.fstat(descriptor)
        if not stat.S_ISDIR(real.st_mode):
            raise SafeRootFSError(f"{prefix}:not_a_directory")
        info = self.deps.metadata(descriptor)
        uid = getattr(info, "st_uid", None)
        mode = getattr(info, "st_mode", None)
        if (
            not isinstance(uid, int)
            or isinstance(uid, bool)
            or not isinstance(mode, int)
            or isinstance(mode, bool)
        ):
            raise SafeRootFSError(f"{prefix}:unreadable_metadata")
        if uid != 0:
            raise SafeRootFSError(f"{prefix}:not_root_owned")
        if stat.S_IMODE(mode) & _WRITABLE_BITS:
            raise SafeRootFSError(f"{prefix}:writable")
        if expected_mode is not None and stat.S_IMODE(mode) != expected_mode:
            raise SafeRootFSError(f"{prefix}:mode_mismatch")

    # ------------------------------------------------------------------ #
    # Publication details
    # ------------------------------------------------------------------ #

    def _create_temporary(self, parent: int, destination_name: str) -> tuple[int, str]:
        for _attempt in range(MAX_TEMP_ATTEMPTS):
            name = f".{destination_name}.{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise SafeRootFSError(f"mkstemp:{_open_word(exc)}") from exc
            return descriptor, name
        raise SafeRootFSError("mkstemp:exhausted")

    def _read_existing(self, parent: int, name: str, target: Path) -> bytes | None:
        try:
            descriptor = _open_readonly(parent, name)
        except FileNotFoundError:
            return None
        try:
            _require_regular_single_link(descriptor, "untrusted_target")
            self._require_trusted_file(descriptor, "untrusted_target", target)
            limit = MAX_PERIOD_JOURNAL_BYTES if target in FIXED_STATE_TARGETS else MAX_TARGET_BYTES
            return _read_all(descriptor, limit)
        finally:
            os.close(descriptor)

    def _require_trusted_file(
        self, descriptor: int, prefix: str, target: Path | None = None
    ) -> None:
        info = self.deps.metadata(descriptor)
        uid = getattr(info, "st_uid", None)
        mode = getattr(info, "st_mode", None)
        if (
            not isinstance(uid, int)
            or isinstance(uid, bool)
            or not isinstance(mode, int)
            or isinstance(mode, bool)
        ):
            raise SafeRootFSError(f"{prefix}:unreadable_metadata")
        if uid != 0:
            raise SafeRootFSError(f"{prefix}:not_root_owned")
        if stat.S_IMODE(mode) & _WRITABLE_BITS:
            raise SafeRootFSError(f"{prefix}:writable")
        if target in FIXED_STATE_TARGETS and stat.S_IMODE(mode) != 0o600:
            raise SafeRootFSError(f"{prefix}:period_journal_mode")
        if target == SAMPLE_PERIOD_DROPIN and stat.S_IMODE(mode) != 0o644:
            raise SafeRootFSError(f"{prefix}:sample_dropin_mode")

    def _open_lock_file(self, parent: int, name: str) -> int:
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
        except OSError as exc:
            raise SafeRootFSError(f"untrusted_lock:{_open_word(exc)}") from exc
        try:
            _require_regular_single_link(descriptor, "untrusted_lock")
            self._require_trusted_file(descriptor, "untrusted_lock")
            os.fchmod(descriptor, 0o600)
            self.deps.chown(descriptor, 0, 0)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


# --------------------------------------------------------------------------- #
# Fixed path policy
# --------------------------------------------------------------------------- #


def _require_fixed_path(candidate: object, prefix: str) -> Path:
    path = _canonical_absolute(candidate, prefix)
    if path in FIXED_CONFIG_TARGETS:
        return path
    for root in ADDRESSABLE_ROOTS:
        if path == root:
            # A directory is not a file target; callers must name the basename
            # from their own module, never a caller-supplied one.
            raise SafeRootFSError(f"{prefix}:not_fixed_path")
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) > MAX_COMPONENTS:
            raise SafeRootFSError(f"{prefix}:too_deep")
        for part in parts:
            if not _is_safe_component(part):
                raise SafeRootFSError(f"{prefix}:not_fixed_path")
        return path
    raise SafeRootFSError(f"{prefix}:not_fixed_path")


def _is_safe_component(part: str) -> bool:
    if not isinstance(part, str) or not part:
        return False
    if part in {".", ".."}:
        return False
    if len(part.encode("utf-8")) > 64:
        return False
    return _SAFE_COMPONENT.fullmatch(part) is not None


def _canonical_absolute(candidate: object, prefix: str) -> Path:
    if isinstance(candidate, (str, os.PathLike)):
        raw = os.fspath(candidate)
    else:
        raise SafeRootFSError(f"{prefix}:invalid_path")
    if isinstance(raw, bytes):
        raise SafeRootFSError(f"{prefix}:invalid_path")
    text = str(raw)
    if not text or _has_control(text):
        raise SafeRootFSError(f"{prefix}:invalid_path")
    if len(text.encode("utf-8")) > _MAX_PATH_BYTES:
        raise SafeRootFSError(f"{prefix}:path_too_long")
    if not text.startswith("/") or text.startswith("//"):
        raise SafeRootFSError(f"{prefix}:not_absolute")
    if ".." in Path(text).parts:
        raise SafeRootFSError(f"{prefix}:not_normalized")
    if str(Path(text)) != text:
        # One canonical spelling only: "x/", "x//y" and "x/./y" are aliases.
        raise SafeRootFSError(f"{prefix}:not_normalized")
    return Path(text)


def _has_control(text: str) -> bool:
    return _CONTROL_CHARS.search(text) is not None


def _fixed_directory_mode(full: Path) -> int:
    if full == STATE_ROOT:
        # Traversal-only: the per-UID probe path must be reachable by the
        # invoking user without exposing the state file.
        return 0o711
    if full == LOCK_ROOT:
        return 0o755
    if full == PROBE_ROOT or _is_beneath(full, PROBE_ROOT):
        # The caller's ``dd`` runs as the invoking UID and must open the
        # pre-created probe file by path, so the per-UID parent has to be
        # traversable. 0711 is execute-only: not writable, not listable.
        return 0o711
    if _is_beneath(full, STATE_ROOT):
        # Private staging: root-only. Nothing outside the helper reads here.
        return 0o700
    return 0o755


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Field validation
# --------------------------------------------------------------------------- #


def _require_payload(content: object) -> bytes:
    if not isinstance(content, bytes):
        raise SafeRootFSError("payload_not_bytes")
    if len(content) > MAX_TARGET_BYTES:
        raise SafeRootFSError("payload_too_large")
    return content


def _require_mode(mode: object) -> int:
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise SafeRootFSError("unsafe_mode:invalid")
    if mode < 0 or mode > 0o777:
        raise SafeRootFSError("unsafe_mode:invalid")
    return mode


def _require_owner(owner: object, path: Path) -> tuple[int, int]:
    if owner is None:
        return (0, 0)
    if not isinstance(owner, tuple) or len(owner) != 2:
        raise SafeRootFSError("invalid_owner")
    uid, gid = owner
    for value in (uid, gid):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SafeRootFSError("invalid_owner")
        if value < 0 or value > 2**32 - 2:
            raise SafeRootFSError("invalid_owner")
    if (uid, gid) != (0, 0):
        try:
            path.relative_to(PROBE_ROOT)
        except ValueError as exc:
            raise SafeRootFSError("unsafe_owner:caller_owned_outside_probe_root") from exc
    return (int(uid), int(gid))


def _require_bound(max_bytes: object) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an int")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if max_bytes > MAX_TARGET_BYTES:
        raise ValueError(f"max_bytes must not exceed {MAX_TARGET_BYTES}")
    return max_bytes


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #


def _open_readonly(parent: int, name: str) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SafeRootFSError(f"untrusted_target:{_open_word(exc)}") from exc


def _require_regular_single_link(descriptor: int, prefix: str) -> None:
    real = os.fstat(descriptor)
    if not stat.S_ISREG(real.st_mode):
        raise SafeRootFSError(f"{prefix}:not_a_regular_file")
    if real.st_nlink != 1:
        raise SafeRootFSError(f"{prefix}:hardlinked")


def _require_same_directory(descriptor: int, path: Path) -> None:
    opened = os.fstat(descriptor)
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise SafeRootFSError(f"untrusted_parent:{_open_word(exc)}") from exc
    if stat.S_ISLNK(current.st_mode):
        raise SafeRootFSError("untrusted_parent:changed_during_write")
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise SafeRootFSError("untrusted_parent:changed_during_write")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise SafeRootFSError("short_write")
        view = view[written:]


def _read_all(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise SafeRootFSError(f"too_large:limit_{limit}")
    return data


def _open_word(exc: OSError, *, parent: int | None = None, name: str | None = None) -> str:
    if exc.errno == errno.ENOTDIR and parent is not None and name is not None:
        # O_NOFOLLOW|O_DIRECTORY reports a symlinked component as ENOTDIR on
        # Linux; report what was actually there so the refusal is diagnosable.
        try:
            if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                return "symlink"
        except OSError:
            pass
    return {
        errno.ELOOP: "symlink",
        errno.ENOTDIR: "not_a_directory",
        errno.ENOENT: "missing",
        errno.EACCES: "permission_denied",
        errno.EPERM: "permission_denied",
    }.get(exc.errno or 0, f"errno_{exc.errno}")


# --------------------------------------------------------------------------- #
# The three fixed drop-in formats
# --------------------------------------------------------------------------- #

MANAGER_PAYLOAD: bytes = b"[Manager]\nDefaultIOAccounting=yes\n"


def service_dropin_payload(controllers) -> bytes:
    """Render the canonical service drop-in for a controller set.

    Controllers are sorted and de-duplicated so two helpers observing the same
    effective set produce the same bytes: idempotent install depends on it.
    """

    names = frozenset(controllers)
    if not names:
        raise ValueError("at least one controller is required")
    for name in names:
        _require_controller_name(name)
    ordered = " ".join(sorted(names))
    return f"[Service]\nDelegate={ordered}\nIOAccounting=yes\n".encode("utf-8")


def manager_dropin_payload() -> bytes:
    return MANAGER_PAYLOAD


def sample_period_payload(seconds: int) -> bytes:
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("sample period must be an integer from 2 through 30")
    value = int(seconds)
    if not 2 <= value <= 30:
        raise ValueError("sample period must be an integer from 2 through 30")
    return f"[Service]\nEnvironment=APPIO_SAMPLE_SECONDS={value}\n".encode("ascii")


def parse_sample_period_dropin(data: bytes) -> int | None:
    if not isinstance(data, bytes):
        return None
    match = re.fullmatch(rb"\[Service\]\nEnvironment=APPIO_SAMPLE_SECONDS=([2-9]|[12][0-9]|30)\n", data)
    return int(match.group(1)) if match else None


def parse_service_dropin(data: bytes) -> frozenset[str] | None:
    """Recognise the canonical service drop-in; ``None`` means "not ours".

    Only the exact three-line shape is accepted. Anything else -- extra keys,
    trailing whitespace, a different key order, an unsorted or duplicated
    delegate list -- is an administrator's unrelated file and is refused rather
    than replaced.
    """

    if not isinstance(data, bytes):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if len(lines) != 4 or lines[3] != "":
        return None
    if lines[0] != "[Service]" or lines[2] != "IOAccounting=yes":
        return None
    prefix = "Delegate="
    if not lines[1].startswith(prefix):
        return None
    value = lines[1][len(prefix) :]
    if not value:
        return None
    parts = value.split(" ")
    if any(_CONTROLLER.fullmatch(part) is None for part in parts):
        return None
    if list(parts) != sorted(set(parts)):
        return None
    return frozenset(parts)


def parse_manager_dropin(data: bytes) -> bool:
    return data == MANAGER_PAYLOAD


def _require_expected_content(target: Path, existing: bytes) -> None:
    if existing == b"":
        # An empty file loses nothing by being replaced.
        return
    if target == USER_MANAGER_DROPIN:
        if not parse_manager_dropin(existing):
            raise SafeRootFSError("unexpected_content:user_manager_dropin")
        return
    if target == SERVICE_DROPIN:
        if parse_service_dropin(existing) is None:
            raise SafeRootFSError("unexpected_content:service_dropin")
        return
    if target == SAMPLE_PERIOD_DROPIN:
        if parse_sample_period_dropin(existing) is None:
            raise SafeRootFSError("unexpected_content:sample_period_dropin")
        return
    raise SafeRootFSError("unexpected_content:not_a_config_target")


def _require_controller_name(name: object) -> None:
    if not isinstance(name, str) or _CONTROLLER.fullmatch(name) is None:
        raise ValueError(f"invalid controller name: {name!r}")


@dataclasses.dataclass(frozen=True)
class PeriodJournalRecord:
    operation_id: str
    target: str
    sample_seconds: int
    target_sha256: str


def encode_period_journal(record: PeriodJournalRecord) -> bytes:
    if not isinstance(record, PeriodJournalRecord):
        raise ValueError("invalid period journal record")
    if not isinstance(record.operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", record.operation_id) is None:
        raise ValueError("invalid period journal operation_id")
    if record.target != str(SAMPLE_PERIOD_DROPIN):
        raise ValueError("invalid period journal target")
    if type(record.sample_seconds) is not int:
        raise ValueError("invalid period journal sample_seconds")
    sample_period_payload(record.sample_seconds)
    if not isinstance(record.target_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", record.target_sha256) is None:
        raise ValueError("invalid period journal target_sha256")
    document = {
        "version": 1,
        "operation_id": record.operation_id,
        "target": record.target,
        "sample_seconds": record.sample_seconds,
        "target_sha256": record.target_sha256,
    }
    return (json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def decode_period_journal(data: bytes) -> PeriodJournalRecord:
    if not isinstance(data, bytes) or len(data) > MAX_PERIOD_JOURNAL_BYTES:
        raise ValueError("invalid period journal bytes")
    try:
        document = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid period journal JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "version", "operation_id", "target", "sample_seconds", "target_sha256"
    }:
        raise ValueError("invalid period journal fields")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("invalid period journal version")
    record = PeriodJournalRecord(
        document["operation_id"], document["target"], document["sample_seconds"], document["target_sha256"]
    )
    if encode_period_journal(record) != data:
        raise ValueError("noncanonical period journal")
    return record


def publish_period_pending(fs: SafeRootFS, record: PeriodJournalRecord) -> None:
    fs.atomic_write(SAMPLE_PERIOD_PENDING, encode_period_journal(record), mode=0o600)


def publish_period_complete(fs: SafeRootFS, record: PeriodJournalRecord) -> None:
    fs.atomic_write(SAMPLE_PERIOD_COMPLETE, encode_period_journal(record), mode=0o600)


def remove_period_pending(fs: SafeRootFS) -> None:
    fs.remove_period_pending()
