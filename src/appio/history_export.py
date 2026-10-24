"""Consistent daemon history exports into a pinned, NSS-selected user directory.

SQLite only opens the root-owned source and private staging files. User files
are never SQLite inputs: a completed, validated backup is copied into an
anonymous inode and linked without replacement after ownership and fsync.
The seams are exclusively SetupDeps; neither argv nor environment selects paths.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from typing import Iterator
from urllib.parse import quote

from .durable import decode_handover, decode_sampler_checkpoint
from .migrations import validate_schema
from .setup_boundary import SetupDeps, SetupIdentity, open_trusted_directory, parse_invocation
from .setup_files import STAGING_ROOT, SafeRootFS

SOURCE = Path("/var/lib/appio/iod.db")
EXPORT_TIMEOUT_SECONDS = 15.0
MAX_NAME_ATTEMPTS = 128
_MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ExportError(RuntimeError):
    """No complete, durable export can be asserted; the source is preserved."""


@dataclass(frozen=True)
class ExportResult:
    path: Path
    boot_id: str | None
    iod_epoch: int | None
    seq: int | None
    cutoff_mono_ns: int | None


def _require_identity(identity: SetupIdentity, deps: SetupDeps) -> None:
    try:
        if not isinstance(identity, SetupIdentity):
            raise ValueError("invalid identity")
        parsed = parse_invocation(("/usr/bin/appio-setup", "export-history"),
            environ={"PKEXEC_UID": str(identity.uid)}, euid=deps.effective_uid(),
            getpwuid=deps.getpwuid)
        if parsed.identity != identity:
            raise ValueError("stale account identity")
    except (ValueError, TypeError, AttributeError, RuntimeError) as error:
        raise ExportError("identity:invalid_or_stale_account") from error
    except Exception as error:
        raise ExportError("identity:account_unavailable") from error


def _fixed_path(path: Path, deps: SetupDeps) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ExportError("source:invalid_path")
    return Path(deps.root) / path.relative_to("/")


def _trusted_regular(fd: int, deps: SetupDeps) -> None:
    real, info = os.fstat(fd), deps.metadata(fd)
    if not stat.S_ISREG(real.st_mode) or real.st_nlink != 1:
        raise ExportError("source:not_a_regular_single_link_file")
    if getattr(info, "st_uid", None) != 0 or getattr(info, "st_mode", 0) & 0o022:
        raise ExportError("source:untrusted_owner_or_mode")


@contextmanager
def _source(source: Path, deps: SetupDeps) -> Iterator[tuple[int, str]]:
    """Pin the root-owned directory for SQLite's main file and WAL sidecars."""
    parent = descriptor = None
    try:
        path = _fixed_path(source, deps)
        parent = open_trusted_directory(path.parent, base=Path(deps.root), metadata=deps.metadata)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        _trusted_regular(descriptor, deps)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                sidecar = os.open(path.name + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                continue
            try:
                _trusted_regular(sidecar, deps)
            finally:
                os.close(sidecar)
        yield parent, path.name
    except ExportError:
        raise
    except FileNotFoundError as error:
        raise ExportError("source:missing") from error
    except Exception as error:
        raise ExportError("source:untrusted_or_unreadable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def _resolved_home_parts(deps: SetupDeps, identity: SetupIdentity) -> tuple[str, ...]:
    """Split the account's home for the walk, resolving one administrator relocation.

    An account's home may be a symlink - this machine maps ``/home/henry`` to
    ``/data/home/henry-arch`` - and the walk opens every component ``O_NOFOLLOW``, so
    an unresolved link made the export fail with ``destination:unsafe_or_unavailable``
    and ``disable`` could not preserve history. Resolving the home once, under the
    injected root, keeps every defence that matters: symlinks *below* the home are
    still refused, and each resolved component is still checked for ownership and mode
    by ``_check_directory``. Only the relocation itself is accepted, because an
    administrator created it deliberately and the account cannot rewrite it.
    """
    declared = identity.home
    candidate = Path(deps.root) / declared.relative_to("/")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return declared.relative_to("/").parts
    try:
        inside = resolved.relative_to(Path(deps.root))
    except ValueError:  # a link that leaves the injected root is not trusted
        return declared.relative_to("/").parts
    return inside.parts


def _destination(identity: SetupIdentity, deps: SetupDeps, *, create: bool) -> int:
    """Walk from the injected root; retain the final directory descriptor.

    A user may rename their own directories after this walk. Every subsequent
    write still addresses this descriptor, and publication rechecks the visible
    chain so the returned pathname names the directory that was actually used.
    """
    descriptor = None
    try:
        descriptor = os.open(deps.root, _DIR_FLAGS)
        _check_directory(descriptor, identity, deps, home=False)
        parts = _resolved_home_parts(deps, identity)
        for offset, part in enumerate((*parts, ".local", "share", "appio")):
            user_tree = offset >= len(parts)
            created = False
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create or not user_tree:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            try:
                if created:
                    os.fchmod(child, 0o700)
                    try:
                        deps.chown(child, identity.uid, identity.gid)
                    except Exception as error:
                        raise ExportError("destination:ownership_failed") from error
                    os.fsync(child)
                    os.fsync(descriptor)
                _check_directory(child, identity, deps, home=offset == len(parts) - 1)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        result, descriptor = descriptor, None
        return result
    except ExportError:
        raise
    except Exception as error:
        raise ExportError("destination:unsafe_or_unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _check_directory(fd, identity, deps, *, home):
    real, info = os.fstat(fd), deps.metadata(fd)
    uid, mode = getattr(info, "st_uid", None), getattr(info, "st_mode", None)
    if not stat.S_ISDIR(real.st_mode) or type(uid) is not int or type(mode) is not int:
        raise ExportError("destination:invalid_directory_metadata")
    if uid not in (0, identity.uid) or mode & 0o022:
        raise ExportError("destination:foreign_owned_or_writable")
    if home and uid != identity.uid:
        raise ExportError("destination:home_not_caller_owned")
    if not (mode & (0o100 if uid == identity.uid else 0o001)):
        raise ExportError("destination:caller_cannot_traverse")


def _anonymous(parent: int) -> int:
    try:
        return os.open(".", os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=parent)
    except (OSError, AttributeError) as error:
        raise ExportError("destination:anonymous_publication_unsupported") from error


def _link_anonymous(descriptor: int, parent: int, name: str) -> None:
    """linkat(AT_EMPTY_PATH) links the pinned inode; EEXIST never replaces."""
    try:
        library = ctypes.CDLL(None, use_errno=True)
        linkat = library.linkat
    except (AttributeError, OSError) as error:
        raise ExportError("destination:anonymous_publication_unsupported") from error
    linkat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    linkat.restype = ctypes.c_int
    if linkat(descriptor, b"", parent, name.encode("ascii"), 0x1000) != 0:
        number = ctypes.get_errno()
        if number in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL, errno.EPERM}:
            raise ExportError("destination:anonymous_publication_unsupported")
        raise OSError(number, os.strerror(number))


def _same_inode(first: int, second: int) -> bool:
    a, b = os.fstat(first), os.fstat(second)
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


@dataclass
class PreparedExport:
    """A preflighted anonymous output held across graceful service shutdown."""
    identity: SetupIdentity
    deps: SetupDeps
    parent: int
    descriptor: int

    def _check_location(self):
        current = _destination(self.identity, self.deps, create=False)
        try:
            if not _same_inode(current, self.parent):
                raise ExportError("destination:changed_during_export")
        finally:
            os.close(current)

    def export(self, *, source: Path = SOURCE) -> ExportResult:
        linked = None
        try:
            with _staged_backup(source, self.deps) as (stage, evidence):
                self.deps.event("export-copy")
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                os.ftruncate(self.descriptor, 0)
                while data := os.read(stage, 256 * 1024):
                    evidence["deadline"]()
                    view = memoryview(data)
                    while view:
                        count = os.write(self.descriptor, view)
                        if count <= 0:
                            raise ExportError("publication:short_write")
                        view = view[count:]
                self.deps.event("export-chown")
                os.fchmod(self.descriptor, 0o600)
                try:
                    self.deps.chown(self.descriptor, self.identity.uid, self.identity.gid)
                except Exception as error:
                    raise ExportError("publication:ownership_failed") from error
                self.deps.event("export-fsync")
                os.fsync(self.descriptor)
                self._check_location()
                stamp = time.time_ns()
                if type(stamp) is not int or stamp < 0:
                    raise ExportError("publication:invalid_utc_timestamp")
                for suffix in range(MAX_NAME_ATTEMPTS):
                    name = f"iod-export-{stamp}{'-' + str(suffix) if suffix else ''}.db"
                    try:
                        self.deps.event("export-link")
                        _link_anonymous(self.descriptor, self.parent, name)
                    except FileExistsError:
                        continue
                    linked = name
                    break
                else:
                    raise ExportError("publication:name_collisions_exhausted")
                self.deps.event("export-fsync-dir")
                os.fsync(self.parent)
                self._check_location()
                visible = os.stat(linked, dir_fd=self.parent, follow_symlinks=False)
                opened = os.fstat(self.descriptor)
                if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ExportError("publication:changed_during_export")
                self.deps.event("export-complete")
                return ExportResult(self.identity.home / ".local/share/appio" / linked,
                    evidence["boot_id"], evidence["iod_epoch"], evidence["seq"], evidence["cutoff_mono_ns"])
        except Exception as error:
            if linked is not None:
                _remove_own_link(self.descriptor, self.parent, linked)
            if isinstance(error, ExportError):
                raise
            raise ExportError(f"export:{type(error).__name__}") from error


def _remove_own_link(descriptor, parent, name):
    try:
        info, opened = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (opened.st_dev, opened.st_ino):
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
    except OSError:
        pass  # A failure is still a failure; never remove a caller's replacement.


@contextmanager
def prepare_export(identity: SetupIdentity, *, deps: SetupDeps, source: Path = SOURCE) -> Iterator[PreparedExport]:
    """Check source, ownership and safe publication before stopping collection."""
    _require_identity(identity, deps)
    parent = descriptor = None
    try:
        try:
            with _source(source, deps):
                pass
            parent = _destination(identity, deps, create=True)
            descriptor = _anonymous(parent)
            try:
                deps.chown(descriptor, identity.uid, identity.gid)
            except Exception as error:
                raise ExportError("destination:ownership_failed") from error
            # Use a separate probe: an unlinked inode cannot be linked again.
            probe = _anonymous(parent)
            probe_name = f".appio-export-probe-{secrets.token_hex(12)}"
            try:
                _link_anonymous(probe, parent, probe_name)
            finally:
                _remove_own_link(probe, parent, probe_name)
                os.close(probe)
            available = os.fstatvfs(descriptor)
            if available.f_bavail * available.f_frsize < _fixed_path(source, deps).stat().st_size:
                raise ExportError("destination:insufficient_space")
        except ExportError:
            raise
        except Exception as error:
            raise ExportError(f"preflight:{type(error).__name__}") from error
        # Exceptions from the caller's stop command keep their exact exit code.
        yield PreparedExport(identity, deps, parent, descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def export_history(identity: SetupIdentity, *, deps: SetupDeps, source: Path = SOURCE) -> ExportResult:
    """Export a consistent online snapshot without controlling the daemon."""
    with prepare_export(identity, deps=deps, source=source) as target:
        return target.export(source=source)


def _connect(parent: int, name: str, mode: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:/proc/self/fd/{parent}/{quote(name, safe='')}?mode={mode}", uri=True, timeout=0.1)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    if mode == "ro":
        connection.execute("PRAGMA query_only=ON")
    return connection


@contextmanager
def _staged_backup(source: Path, deps: SetupDeps) -> Iterator[tuple[int, dict]]:
    parent = descriptor = None
    name = f"export-{secrets.token_hex(12)}.db"
    end = time.monotonic() + EXPORT_TIMEOUT_SECONDS

    def check_deadline(*_args):
        if time.monotonic() >= end:
            raise ExportError("backup:deadline_exceeded")

    def sql_progress():
        return int(time.monotonic() >= end)

    try:
        # SafeRootFS owns creation of the root-only staging directory and file.
        SafeRootFS(deps).atomic_write(str(STAGING_ROOT / name), b"", mode=0o600)
        directory = _fixed_path(STAGING_ROOT, deps)
        parent = open_trusted_directory(directory, base=Path(deps.root), metadata=deps.metadata)
        with _source(source, deps) as (source_parent, source_name):
            with closing(_connect(source_parent, source_name, "ro")) as original, closing(_connect(parent, name, "rw")) as stage:
                deps.event("export-backup")
                original.backup(stage, pages=256, progress=check_deadline, sleep=0.01)
                check_deadline()
                stage.set_progress_handler(sql_progress, 1000)
                # A WAL-mode source must leave a self-contained main file.
                stage.execute("PRAGMA journal_mode=DELETE")
                try:
                    evidence = _validate_backup(stage)
                except (ValueError, sqlite3.Error) as error:
                    raise ExportError("backup:validation_failed") from error
                check_deadline()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        _trusted_regular(descriptor, deps)
        evidence["deadline"] = check_deadline
        yield descriptor, evidence
    except ExportError:
        raise
    except Exception as error:
        raise ExportError(f"backup:{type(error).__name__}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.unlink(name + suffix, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.close(parent)


def _validate_backup(connection: sqlite3.Connection) -> dict:
    validate_schema(connection)
    if connection.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
        raise ValueError("integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("foreign-key check failed")
    evidence = dict(boot_id=None, iod_epoch=None, seq=None, cutoff_mono_ns=None)
    columns = ("source", "boot_id", "epoch", "seq", "checkpoint_mono_ns")
    for row in connection.execute("SELECT source,boot_id,epoch,seq,checkpoint_mono_ns,state_json FROM sampler_checkpoint ORDER BY source"):
        if len(row[5].encode("utf-8")) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint too large")
        checkpoint = decode_sampler_checkpoint(row[5], dict(zip(columns, row[:5], strict=True)))
        if checkpoint.source == "iod":
            evidence.update(boot_id=checkpoint.boot_id, iod_epoch=checkpoint.epoch,
                            seq=checkpoint.last_seq, cutoff_mono_ns=checkpoint.checkpoint_mono_ns)
    for boot_id, cutoff, text in connection.execute("SELECT boot_id,cutoff_mono_ns,baselines_json FROM handover ORDER BY boot_id,cutoff_mono_ns"):
        if len(text.encode("utf-8")) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("handover checkpoint too large")
        decode_handover(text, boot_id, cutoff)
    return evidence
