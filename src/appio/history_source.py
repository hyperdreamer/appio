"""Managed, actor-owned SQLite sources for local history observations.

Each source actor owns one long-lived observer connection and performs every
SQLite operation, including backup and close, on its own thread. The primary
GUI actor also owns its paired gap writer. Registry results contain immutable
records only. The source URI and connections stay on their owning actor.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
import hashlib
import math
import os
import queue
import secrets
import sqlite3
import tempfile
import threading
import time
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from typing import Callable, Literal, TypeVar, cast

from .contracts import SQLITE_INT_MAX, validate_enum, validate_sqlite_integer
from .iodclient import GapNotice
from .history_types import (
    CapturedSourceImage,
    CapturedSourceSet,
    LocalPosition,
    LocalPositionVector,
    ManagedSourceSpec,
)
from .migrations import validate_schema

__all__ = ["GapWriteResult", "ManagedSourceRegistry", "SourceAdmissionError"]


class SourceAdmissionError(RuntimeError):
    """A managed source could not be safely admitted or observed."""


@dataclass(frozen=True)
class GapWriteResult:
    registration_epoch: int
    disposition: Literal["inserted", "duplicate", "unknown_boot"]
    boot_id: str
    from_mono_ns: int
    to_mono_ns: int

    def __post_init__(self) -> None:
        if (isinstance(self.registration_epoch, bool)
                or not isinstance(self.registration_epoch, int)
                or self.registration_epoch <= 0):
            raise ValueError("gap registration_epoch must be a positive integer")
        validate_enum(self.disposition, ("inserted", "duplicate", "unknown_boot"),
                      "gap disposition")
        if not isinstance(self.boot_id, str) or not self.boot_id:
            raise ValueError("gap boot_id must be nonempty text")
        validate_sqlite_integer(self.from_mono_ns, "gap.from_mono_ns")
        validate_sqlite_integer(self.to_mono_ns, "gap.to_mono_ns")
        if self.from_mono_ns >= self.to_mono_ns:
            raise ValueError("gap bounds must be strictly increasing")


@dataclass(frozen=True)
class _GapWrite:
    notice: GapNotice


def _validate_gap_notice(notice: GapNotice) -> None:
    if not isinstance(notice, GapNotice):
        raise ValueError("gap writer requires a GapNotice")
    GapNotice.__post_init__(notice)
    if not notice.boot_id:
        raise ValueError("gap writer requires a boot identity")
    validate_sqlite_integer(notice.from_mono_ns, "gap.from_mono_ns")
    validate_sqlite_integer(notice.to_mono_ns, "gap.to_mono_ns")


@dataclass(frozen=True)
class _RegistrationReceipt:
    name: str
    owner: str
    registration_epoch: int
    status: str


@dataclass(frozen=True)
class _Registration:
    spec: ManagedSourceSpec
    registration_epoch: int
    actor: _SourceActor
    receipt: _RegistrationReceipt


@dataclass(frozen=True)
class _PendingRegistration:
    spec: ManagedSourceSpec
    registration_epoch: int
    actor: _SourceActor
    future: Future


_STOP = object()
_EPOCH_LOCK = threading.Lock()
_NEXT_REGISTRATION_EPOCH = 1
_READ_ONLY_PRAGMAS = frozenset((
    "data_version", "database_list", "user_version", "query_only", "read_uncommitted"))
_GAP_RETRY_NS = 1_000_000_000
_GAP_ATTEMPTS = 10
_GAP_RETRY_SECONDS = 0.01
_GAP_PREDICATE = (
    "SELECT 1 FROM gap WHERE boot_id=? AND source='gui' "
    "AND from_mono_ns=? AND to_mono_ns=? AND reason='gui_absent'")
_WRITE_ACTION_NAMES = (
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DELETE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_VTABLE",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_VTABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_ALTER_TABLE",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
    "SQLITE_TRANSACTION",
    "SQLITE_SAVEPOINT",
)
_WRITE_ACTIONS = frozenset(
    getattr(sqlite3, name) for name in _WRITE_ACTION_NAMES if hasattr(sqlite3, name)
)


def _allocate_registration_epoch() -> int:
    global _NEXT_REGISTRATION_EPOCH
    with _EPOCH_LOCK:
        epoch = _NEXT_REGISTRATION_EPOCH
        _NEXT_REGISTRATION_EPOCH += 1
        return epoch


_T = TypeVar("_T")


def _failed_future(error: BaseException) -> Future[_T]:
    future: Future[_T] = Future()
    future.set_exception(error)
    return future


def _validate_file_uri(database_uri: str) -> None:
    """Accept only absolute, file-backed SQLite URIs with non-creating modes."""

    try:
        parsed = urlsplit(database_uri)
        parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        path = unquote(parsed.path)
    except (ValueError, UnicodeError) as error:
        raise SourceAdmissionError("managed source URI is invalid") from None
    if (parsed.scheme != "file" or parsed.netloc not in ("", "localhost")
            or not path.startswith("/") or "\x00" in path or parsed.fragment):
        raise SourceAdmissionError("managed source must use an absolute file URI")
    if any(key not in ("mode", "cache") for key, _ in parameters):
        raise SourceAdmissionError("managed source URI has unsupported parameters")
    modes = [value for key, value in parameters if key == "mode"]
    caches = [value for key, value in parameters if key == "cache"]
    if len(modes) != 1 or modes[0] not in ("ro", "rw"):
        raise SourceAdmissionError("managed source URI must use mode=ro or mode=rw")
    if len(caches) > 1 or (caches and caches[0] not in ("private", "shared")):
        raise SourceAdmissionError("managed source URI has an invalid cache mode")


def _connection_options(spec: ManagedSourceSpec) -> dict[str, float | int | str]:
    options: dict[str, float | int | str] = {}
    for key, value in spec.connect_options:
        if key in options:
            raise SourceAdmissionError("managed source has duplicate connect options")
        if key == "timeout":
            try:
                timeout = float(value)
            except ValueError:
                raise SourceAdmissionError("managed source timeout is invalid") from None
            if not math.isfinite(timeout) or timeout < 0:
                raise SourceAdmissionError("managed source timeout is invalid")
            options[key] = timeout
        elif key == "cached_statements":
            try:
                cached_statements = int(value)
            except ValueError:
                raise SourceAdmissionError(
                    "managed source cached_statements is invalid") from None
            if cached_statements < 0 or str(cached_statements) != value:
                raise SourceAdmissionError(
                    "managed source cached_statements is invalid")
            options[key] = cached_statements
        elif key == "isolation_level":
            if value not in ("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
                raise SourceAdmissionError(
                    "managed source isolation_level is invalid")
            options[key] = value
        else:
            raise SourceAdmissionError("managed source has an unsupported connect option")
    return options


def _query(connection: sqlite3.Connection, statement: str, parameters: tuple = ()) -> list:
    """Finish every SQLite cursor before the actor changes transaction state."""

    cursor = connection.execute(statement, parameters)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _open_read_only_connection(
        spec: ManagedSourceSpec, *, authorizer: Callable | None = None,
) -> sqlite3.Connection:
    _validate_file_uri(spec.database_uri)
    options = _connection_options(spec)
    isolation_level = options.pop("isolation_level", None)
    connection = cast(Callable[..., sqlite3.Connection], sqlite3.connect)(
        spec.database_uri,
        uri=True,
        isolation_level=cast(
            Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None,
            isolation_level,
        ),
        check_same_thread=True,
        **options,
    )
    try:
        validate_schema(connection)

        connection.execute("PRAGMA busy_timeout=0").close()
        connection.execute("PRAGMA read_uncommitted=OFF").close()
        connection.execute("PRAGMA query_only=ON").close()
        cursor = connection.execute("PRAGMA query_only")
        try:
            query_only = cursor.fetchone()
        finally:
            cursor.close()
        if query_only is None or query_only[0] != 1:
            raise SourceAdmissionError("managed source query-only mode is unavailable")
        connection.set_authorizer(authorizer or _read_only_authorizer)
        return connection
    except BaseException:
        connection.close()
        raise


def _read_only_authorizer(action: int, first: str | None, second: str | None,
                          _database: str | None, _source: str | None) -> int:
    if action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        pragma = "" if first is None else first.lower()
        if second is None and pragma in _READ_ONLY_PRAGMAS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and second is not None:
        if second.lower() == "load_extension":
            return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                  sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _position(connection: sqlite3.Connection, spec: ManagedSourceSpec,
              registration_epoch: int) -> LocalPosition:
    if connection.in_transaction:
        raise SourceAdmissionError("managed source has an uncommitted transaction")
    cursor = connection.execute("PRAGMA main.data_version")
    try:
        row = cursor.fetchone()
        cursor.fetchall()
    finally:
        cursor.close()
    if connection.in_transaction:
        raise SourceAdmissionError("managed source observation opened a transaction")
    if row is None:
        raise SourceAdmissionError("managed source data_version is unavailable")
    return LocalPosition(
        spec.name, spec.owner, registration_epoch, row[0], connection.total_changes)


def _snapshot_digest(images: tuple[CapturedSourceImage, ...]) -> str:
    """Hash the ordered registration identities and image digests."""

    digest = hashlib.sha256()
    digest.update(b"appio-captured-source-set-v1\0")
    for image in images:
        name = image.name.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        epoch = str(image.registration_epoch).encode("ascii")
        digest.update(len(epoch).to_bytes(4, "big"))
        digest.update(epoch)
        digest.update(bytes.fromhex(image.image_digest))
    return digest.hexdigest()


def _backup_image(connection: sqlite3.Connection, *, clock_ns: Callable[[], int],
                  deadline_ns: int) -> bytes:
    if connection.in_transaction:
        raise SourceAdmissionError("managed source has an uncommitted transaction")
    descriptor, path = tempfile.mkstemp(prefix="appio-source-", suffix=".sqlite")
    os.close(descriptor)
    destination = None
    try:
        destination = sqlite3.connect(path, isolation_level=None)

        def progress(status: int, _remaining: int, _total: int) -> None:
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if clock_ns() >= deadline_ns:
                    raise SourceAdmissionError("managed source backup timed out")

        connection.backup(destination, pages=128, progress=progress, sleep=0.005)
        if connection.in_transaction or destination.in_transaction:
            raise SourceAdmissionError("managed source backup left a transaction open")
        validate_schema(destination)
        destination.close()
        destination = None
        with open(path, "rb") as image_file:
            image = image_file.read()
        if not image:
            raise SourceAdmissionError("managed source backup produced an empty image")
        return image
    finally:
        if destination is not None:
            destination.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


class _CaptureLease:
    """Keep one actor available until an admitted capture has finished."""

    def __init__(self, actor: _SourceActor) -> None:
        self.actor = actor
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.actor._release_lease(self)


class _SourceActor:
    """Serialize all access to one observer connection on its owner thread."""

    def __init__(self, spec: ManagedSourceSpec, registration_epoch: int,
                 wake: Callable[[], None], clock_ns: Callable[[], int],
                 busy_timeout_ns: int, retry_wait: Callable[[float], None]) -> None:
        self.spec = spec
        self.registration_epoch = registration_epoch
        self._wake = wake
        self._clock_ns = clock_ns
        self._busy_timeout_ns = busy_timeout_ns
        self._retry_wait = retry_wait
        self._pairing = False
        self._writer: sqlite3.Connection | None = None
        self._commands: queue.Queue[
            tuple[object, Future | None, _CaptureLease | None]
        ] = queue.Queue()
        self._deferred: list[tuple[object, Future]] = []
        self._leases: set[_CaptureLease] = set()
        self._serializing: set[_CaptureLease] = set()
        self._stop_queued = False
        self.ready: Future = Future()
        self.closed: Future = Future()
        self._status_lock = threading.Lock()
        self._status = "starting"
        self._connection: sqlite3.Connection | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"appio-source-{registration_epoch}",
            daemon=True,
        )

    @property
    def status(self) -> str:
        with self._status_lock:
            return self._status

    def start(self) -> None:
        self._thread.start()

    def submit(self, operation: str | _GapWrite, lease: _CaptureLease | None = None) -> Future:
        future: Future = Future()
        with self._status_lock:
            if lease is None:
                if self._status != "active":
                    future.set_exception(SourceAdmissionError(
                        f"managed source {self.spec.name!r} is not active"))
                    return future
                if self._serializing:
                    self._deferred.append((operation, future))
                else:
                    self._commands.put((operation, future, None))
            elif (lease.actor is not self or lease not in self._leases
                  or self._status not in ("active", "retiring", "draining")):
                future.set_exception(SourceAdmissionError(
                    f"managed source {self.spec.name!r} is not active"))
            else:
                self._commands.put((operation, future, lease))
        return future

    def acquire_lease(self) -> _CaptureLease:
        with self._status_lock:
            if self._status != "active":
                raise SourceAdmissionError(
                    f"managed source {self.spec.name!r} is not active")
            lease = _CaptureLease(self)
            self._leases.add(lease)
            self._serializing.add(lease)
            return lease

    def finish_capture(self, lease: _CaptureLease) -> None:
        """End capture serialization while retaining the actor lease."""

        with self._status_lock:
            self._serializing.discard(lease)
            if not self._serializing:
                for command in self._deferred:
                    self._commands.put((*command, None))
                self._deferred.clear()

    def close(self, *, drain_gaps: bool = False) -> Future:
        with self._status_lock:
            if self._status not in ("retiring", "draining", "closed", "failed"):
                self._status = "draining" if drain_gaps else "retiring"
            if self._status in ("retiring", "draining") and not self._leases:
                self._queue_stop_locked()
            elif self._status == "failed" and not self.closed.done():
                # The opening path always completes ``closed`` after cleanup.
                pass
        return self.closed

    def _queue_stop_locked(self) -> None:
        if not self._stop_queued:
            self._stop_queued = True
            self._commands.put((_STOP, None, None))

    def _release_lease(self, lease: _CaptureLease) -> None:
        with self._status_lock:
            if lease not in self._leases:
                return
            self._leases.remove(lease)
            self._serializing.discard(lease)
            if not self._serializing:
                for command in self._deferred:
                    self._commands.put((*command, None))
                self._deferred.clear()
            if self._status in ("retiring", "draining") and not self._leases:
                self._queue_stop_locked()

    def _set_status(self, status: str) -> None:
        with self._status_lock:
            self._status = status

    def _observe(self) -> LocalPosition:
        connection = self._connection
        if connection is None:
            raise SourceAdmissionError("managed source observer is not active")
        deadline_ns = self._clock_ns() + self._busy_timeout_ns
        while True:
            try:
                return _position(connection, self.spec, self.registration_epoch)
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                if self._clock_ns() >= deadline_ns:
                    raise
                time.sleep(0.005)

    def _observer_authorizer(self, action, first, second, database, source) -> int:
        if (self._pairing and action == sqlite3.SQLITE_PRAGMA
                and first is not None and first.lower() == "read_uncommitted"):
            return sqlite3.SQLITE_OK
        return _read_only_authorizer(action, first, second, database, source)

    def _retry_writer(self, operation: Callable[[], _T]) -> _T:
        writer = self._writer
        if writer is None:
            raise SourceAdmissionError("managed source has no primary gap writer")
        deadline = self._clock_ns() + _GAP_RETRY_NS
        for attempt in range(_GAP_ATTEMPTS):
            try:
                return operation()
            except BaseException as error:
                if writer.in_transaction:
                    _query(writer, "ROLLBACK")
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                if (code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        or attempt + 1 == _GAP_ATTEMPTS
                        or self._clock_ns() >= deadline):
                    raise
                remaining = (deadline - self._clock_ns()) / 1_000_000_000
                if remaining <= 0:
                    raise
                self._retry_wait(min(_GAP_RETRY_SECONDS, remaining))
                if self._clock_ns() >= deadline:
                    raise
        raise SourceAdmissionError("managed source writer retry budget exhausted")

    def _open_primary(self) -> None:
        """Create and challenge the only writer/observer pair for this epoch."""

        try:
            _validate_file_uri(self.spec.database_uri)
            parsed = urlsplit(self.spec.database_uri)
            parameters = dict(parse_qsl(parsed.query))
            if parameters != {"mode": "rw", "cache": "shared"}:
                raise SourceAdmissionError("primary URI requires mode=rw&cache=shared")
            options = _connection_options(self.spec)
            if ("isolation_level" in options
                    or options.get("cached_statements", 0) != 0):
                raise SourceAdmissionError("primary connection options conflict with pairing")
            self._writer = sqlite3.connect(
                self.spec.database_uri, uri=True, isolation_level=None,
                check_same_thread=True, timeout=0, cached_statements=0)
            writer = self._writer
            if writer is None:
                raise SourceAdmissionError("primary writer is not active")
            _query(writer, "PRAGMA busy_timeout=0")
            _query(writer, "PRAGMA foreign_keys=ON")
            _query(writer, "PRAGMA synchronous=FULL")
            if (_query(writer, "PRAGMA foreign_keys") != [(1,)]
                    or _query(writer, "PRAGMA synchronous") != [(2,)]
                    or writer.in_transaction):
                raise SourceAdmissionError("primary writer controls are unavailable")
            validate_schema(writer)
            observer_uri = urlunsplit(parsed._replace(
                query=urlencode({"mode": "ro", "cache": "shared"})))
            observer_spec = replace(self.spec, database_uri=observer_uri,
                                    connect_options=(("cached_statements", "0"), ("timeout", "0")))
            self._connection = _open_read_only_connection(
                observer_spec, authorizer=self._observer_authorizer)
            observer = self._connection
            if observer is None:
                raise SourceAdmissionError("primary observer is not active")
            self._retry_writer(self._challenge_pair)
            if _query(observer, "PRAGMA query_only") != [(1,)]:
                raise SourceAdmissionError("primary observer is not query-only")
            for statement in ("PRAGMA query_only=OFF", "PRAGMA read_uncommitted=ON",
                              "UPDATE boot SET source=source WHERE 0"):
                try:
                    _query(observer, statement)
                except sqlite3.DatabaseError as error:
                    if getattr(error, "sqlite_errorcode", 0) != sqlite3.SQLITE_AUTH:
                        raise
                else:
                    raise SourceAdmissionError("primary observer authorizer is unavailable")
        except BaseException as error:
            raise SourceAdmissionError("gap writer binding is unprovable") from error

    def _challenge_pair(self) -> None:
        writer = self._writer
        observer = self._connection
        if writer is None or observer is None:
            raise SourceAdmissionError("managed source pair is not active")
        if writer.in_transaction or observer.in_transaction:
            raise SourceAdmissionError("pairing requires committed connections")
        _query(writer, "BEGIN IMMEDIATE")
        try:
            for _ in range(10):
                nonce = secrets.token_hex(32)
                order = secrets.randbelow(SQLITE_INT_MAX) + 1
                if not _query(writer, "SELECT 1 FROM boot WHERE boot_id=? OR boot_order=?",
                              (nonce, order)):
                    break
            else:
                raise SourceAdmissionError("pairing challenge identities collided")
            _query(writer, "INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) "
                   "VALUES(?,?,0,0,'gui')", (nonce, order))
            self._pairing = True
            _query(observer, "PRAGMA read_uncommitted=ON")
            if _query(observer, "SELECT boot_id,boot_order FROM boot WHERE boot_id=? AND boot_order=?",
                      (nonce, order)) != [(nonce, order)]:
                raise SourceAdmissionError("pairing challenge is invisible")
            _query(writer, "ROLLBACK")
            for connection in (writer, observer):
                if _query(connection, "SELECT 1 FROM boot WHERE boot_id=?", (nonce,)):
                    raise SourceAdmissionError("pairing challenge survived rollback")
        finally:
            try:
                if writer.in_transaction:
                    _query(writer, "ROLLBACK")
            finally:
                self._pairing = True
                try:
                    _query(observer, "PRAGMA read_uncommitted=OFF")
                    if _query(observer, "PRAGMA read_uncommitted") != [(0,)]:
                        raise SourceAdmissionError("pairing mode could not be restored")
                finally:
                    self._pairing = False

    def _write_gap(self, notice: GapNotice) -> GapWriteResult:
        _validate_gap_notice(notice)
        from_mono_ns = notice.from_mono_ns
        to_mono_ns = notice.to_mono_ns
        if (isinstance(from_mono_ns, bool) or not isinstance(from_mono_ns, int)
                or isinstance(to_mono_ns, bool) or not isinstance(to_mono_ns, int)):
            raise SourceAdmissionError("gap writer requires integer bounds")

        def transaction() -> GapWriteResult:
            writer = self._writer
            observer = self._connection
            if writer is None or observer is None:
                raise SourceAdmissionError("managed source pair is not active")
            if writer.in_transaction or observer.in_transaction:
                raise SourceAdmissionError("gap write requires committed connections")
            bounds: tuple[str, int, int] = (notice.boot_id, from_mono_ns, to_mono_ns)
            _query(writer, "BEGIN IMMEDIATE")
            disposition: Literal["inserted", "duplicate", "unknown_boot"]
            if not _query(writer, "SELECT 1 FROM boot WHERE boot_id=?", (notice.boot_id,)):
                disposition = "unknown_boot"
                _query(writer, "ROLLBACK")
            elif _query(writer, _GAP_PREDICATE, bounds):
                disposition = "duplicate"
                _query(writer, "ROLLBACK")
            else:
                epoch = _query(writer, "SELECT epoch FROM collector_epoch WHERE source='gui'")
                _query(writer, "INSERT INTO gap(boot_id,source,epoch,seq,from_mono_ns,to_mono_ns,reason) "
                       "VALUES(?,'gui',?,0,?,?,'gui_absent')",
                       (notice.boot_id, epoch[0][0] if epoch else 1,
                        from_mono_ns, to_mono_ns))
                _query(writer, "COMMIT")
                disposition = "inserted"
            if writer.in_transaction or observer.in_transaction:
                raise SourceAdmissionError("gap write left a transaction open")
            boot_visible = bool(_query(observer, "SELECT 1 FROM boot WHERE boot_id=?", (notice.boot_id,)))
            gap_visible = bool(_query(observer, _GAP_PREDICATE, bounds))
            if ((disposition == "unknown_boot" and boot_visible)
                    or (disposition != "unknown_boot" and not (boot_visible and gap_visible))):
                raise SourceAdmissionError("gap writer result is not visible to managed source")
            return GapWriteResult(self.registration_epoch, disposition, *bounds)

        return self._retry_writer(transaction)

    def _run(self) -> None:
        try:
            if self.spec.name == "local" and self.spec.owner == "gui":
                self._open_primary()
            else:
                self._connection = _open_read_only_connection(self.spec)
            initial = self._observe()
        except BaseException as error:
            self._set_status("failed")
            self._finish_close()
            if not self.ready.done():
                self.ready.set_exception(error)
            return

        self._set_status("active")
        self.ready.set_result(initial)
        self._notify()
        try:
            while True:
                operation, future, lease = self._commands.get()
                if operation is _STOP:
                    break
                assert future is not None
                with self._status_lock:
                    admitted = (self._status == "active"
                                or (self._status == "draining" and isinstance(operation, _GapWrite))
                                or (lease is not None and lease in self._leases
                                    and self._status in ("retiring", "draining")))
                if not future.set_running_or_notify_cancel():
                    self._notify()
                    continue
                if not admitted:
                    future.set_exception(SourceAdmissionError(
                        f"managed source {self.spec.name!r} is retired"))
                    self._notify()
                    continue
                try:
                    if operation == "observe":
                        result: object = self._observe()
                    elif operation == "capture":
                        connection = self._connection
                        if connection is None:
                            raise SourceAdmissionError("managed source observer is not active")
                        result = _backup_image(
                            connection, clock_ns=self._clock_ns,
                            deadline_ns=self._clock_ns() + self._busy_timeout_ns)
                    elif isinstance(operation, _GapWrite):
                        result = self._write_gap(operation.notice)
                    else:
                        raise SourceAdmissionError("managed source operation is invalid")
                except BaseException as error:
                    future.set_exception(error)
                else:
                    future.set_result(result)
                self._notify()
        finally:
            self._set_status("closed")
            self._finish_close()

    def _finish_close(self) -> None:
        connection, self._connection = self._connection, None
        writer, self._writer = self._writer, None
        for owned in (writer, connection):
            if owned is not None:
                try:
                    owned.close()
                except sqlite3.Error:
                    pass
        self._set_status("closed")
        if not self.closed.done():
            self.closed.set_result(None)
        self._notify()

    def _notify(self) -> None:
        try:
            self._wake()
        except BaseException:
            # Notification is best-effort and must not terminate the actor loop.
            pass


class ManagedSourceRegistry:
    """Own named source actors and return immutable observations and captures."""

    def __init__(self, *, wake: Callable[[], None],
                 clock_ns: Callable[[], int] = time.monotonic_ns,
                 busy_timeout_ns: int = 2_000_000_000,
                 retry_wait: Callable[[float], None] = time.sleep) -> None:
        if not callable(wake):
            raise ValueError("wake must be callable")
        if not callable(clock_ns):
            raise ValueError("clock_ns must be callable")
        if not callable(retry_wait):
            raise ValueError("retry_wait must be callable")
        if (isinstance(busy_timeout_ns, bool) or not isinstance(busy_timeout_ns, int)
                or busy_timeout_ns <= 0):
            raise ValueError("busy_timeout_ns must be a positive integer")
        self._wake = wake
        self._clock_ns = clock_ns
        self._busy_timeout_ns = busy_timeout_ns
        self._retry_wait = retry_wait
        self._lock = threading.RLock()
        self._active: dict[str, _Registration] = {}
        self._pending: dict[str, _PendingRegistration] = {}
        self._reserved: dict[str, Future] = {}
        self._retiring: dict[str, Future] = {}
        self._next_capture_id = 1
        self._capture_lock = threading.Lock()
        self._capture_count = 0
        self._capture_leases: dict[int, tuple[tuple[_CaptureLease, ...], int]] = {}
        self._claimed_captures: set[int] = set()
        self._positions: dict[int, LocalPosition] = {}
        self._observation: tuple[tuple[_Registration, ...], Future] | None = None
        self._deferred_admissions: list[Callable[[], None]] = []
        self._capture_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="appio-source-capture")
        self._closed = False

    def register(self, spec: ManagedSourceSpec) -> Future:
        """Admit a file-backed source after its actor's first observation."""

        if not isinstance(spec, ManagedSourceSpec):
            return _failed_future(SourceAdmissionError(
                "managed source registration requires a ManagedSourceSpec"))
        future: Future = Future()
        with self._lock:
            if self._closed:
                return _failed_future(SourceAdmissionError("managed source registry is closed"))
            name = spec.name
            if (name in self._active or name in self._pending or name in self._reserved):
                return _failed_future(SourceAdmissionError(
                    f"managed source {name!r} is already registered"))
            self._reserved[name] = future
            retiring = self._retiring.get(name)
        if retiring is None:
            self._start_registration(spec, future)
        else:
            retiring.add_done_callback(
                lambda completed: self._start_after_retirement(
                    spec, future, completed))
        return future

    def replace(self, name: str, spec: ManagedSourceSpec) -> Future:
        """Retire a name before beginning its replacement admission."""

        if not isinstance(spec, ManagedSourceSpec) or spec.name != name:
            return _failed_future(SourceAdmissionError(
                "replacement name must match its ManagedSourceSpec"))
        future: Future = Future()
        with self._lock:
            if self._closed:
                return _failed_future(SourceAdmissionError("managed source registry is closed"))
            if name in self._reserved:
                return _failed_future(SourceAdmissionError(
                    f"managed source {name!r} already has a pending change"))
            retiring = self._retire_locked(name, "replaced")
            self._reserved[name] = future
        if retiring is None:
            self._start_registration(spec, future)
        else:
            retiring.add_done_callback(
                lambda completed: self._start_after_retirement(
                    spec, future, completed))
        return future

    def record_transport_gap(self, notice: GapNotice) -> Future[GapWriteResult]:
        """Admit a bounded notice to the active primary pair without waiting."""

        with self._lock:
            if self._closed:
                return _failed_future(SourceAdmissionError("managed source registry is closed"))
            try:
                _validate_gap_notice(notice)
            except Exception as error:
                return _failed_future(error)
            primary = self._active.get("local")
            if primary is None or primary.spec.owner != "gui":
                return _failed_future(SourceAdmissionError("primary GUI source is not active"))
            future = primary.actor.submit(_GapWrite(notice))
        self._notify()
        return future

    def retire(self, name: str, reason: str) -> None:
        """Remove a source from authority immediately and close it on its actor."""

        if not isinstance(name, str) or not name:
            raise ValueError("source name must be non-empty text")
        if not isinstance(reason, str):
            raise ValueError("retirement reason must be text")
        with self._lock:
            if self._closed:
                return
            self._retire_locked(name, reason)

    def registration_vector(self) -> tuple[tuple[str, str, int], ...]:
        """Return active incarnation identities without scheduling SQLite work."""

        with self._lock:
            return tuple((item.spec.name, item.spec.owner, item.registration_epoch)
                         for item in self._ordered_registrations(self._active))

    def observe_async(self) -> Future:
        """Request a fresh actor observation, coalescing a pending vector read."""

        with self._lock:
            registrations = self._active_snapshot()
            pending = self._observation
            if pending is not None and pending[0] == registrations and not pending[1].done():
                return pending[1]
            future = self._capture_executor.submit(self._observe_sources, registrations)
            self._observation = (registrations, future)
            return future

    def _observe_sources(self, registrations: tuple[_Registration, ...]) -> LocalPositionVector:
        positions = self._collect(registrations, "observe")
        with self._lock:
            self._verify_current(registrations)
            self._positions.update((position.registration_epoch, position) for position in positions)
        return LocalPositionVector(positions)

    def observe(self) -> LocalPositionVector:
        """Poll completed observations and request a refresh without waiting.

        During actor work the last completed same-connection positions remain
        available. A later poll consumes the refreshed vector or its exact error.
        """

        with self._lock:
            registrations = self._active_snapshot()
            pending = self._observation
            if pending is not None and pending[0] == registrations and pending[1].done():
                self._observation = None
                return pending[1].result()
            if pending is None or pending[0] != registrations:
                self.observe_async()
            return LocalPositionVector(tuple(
                self._positions[item.registration_epoch] for item in registrations))

    def capture(self) -> Future:
        """Back up all active sources between opening and closing observations."""

        with self._lock:
            if self._closed:
                return _failed_future(SourceAdmissionError(
                    "managed source registry is closed"))
            registrations = self._ordered_registrations(self._active)
            if not registrations:
                return _failed_future(SourceAdmissionError(
                    "managed source registry has no active sources"))
            capture_id = self._next_capture_id
            self._next_capture_id += 1
            leases: list[_CaptureLease] = []
            try:
                for registration in registrations:
                    leases.append(registration.actor.acquire_lease())
            except BaseException as error:
                for lease in leases:
                    lease.release()
                return _failed_future(error)
            lease_tuple = tuple(leases)
            self._capture_count += 1
        try:
            future = self._capture_executor.submit(
                self._capture, capture_id, registrations, lease_tuple)
        except RuntimeError:
            self._finish_capture(capture_id, lease_tuple, retain=False)
            return _failed_future(SourceAdmissionError(
                "managed source registry is closed"))
        future.add_done_callback(
            lambda completed: self._finish_capture(
                capture_id, lease_tuple, retain=not completed.cancelled()
                and completed.exception() is None))
        return future

    def retain_capture(self, capture_id: int) -> None:
        """Keep a completed capture alive for one additional dependent worker."""

        with self._lock:
            if capture_id not in self._capture_leases:
                raise SourceAdmissionError("managed capture is no longer retained")
            if self._closed:
                raise SourceAdmissionError("managed source registry is closed")
            leases, count = self._capture_leases[capture_id]
            self._capture_leases[capture_id] = (leases, count + 1)
            self._claimed_captures.add(capture_id)

    def release_capture(self, capture_id: int) -> None:
        """Release one drained dependent without granting publication authority."""

        with self._lock:
            if capture_id not in self._capture_leases:
                raise SourceAdmissionError("managed capture is no longer retained")
            leases, count = self._capture_leases[capture_id]
            if count > 1:
                self._capture_leases[capture_id] = (leases, count - 1)
                return
            del self._capture_leases[capture_id]
            self._claimed_captures.discard(capture_id)
        self._release_capture_leases(leases)

    def close(self) -> None:
        """Reject new work and queue connection closes on all owning actors."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            for capture_id, (leases, _count) in tuple(self._capture_leases.items()):
                if capture_id not in self._claimed_captures:
                    del self._capture_leases[capture_id]
                    self._release_capture_leases(leases)
            actors: dict[int, _SourceActor] = {}
            for registration in self._active.values():
                actors[id(registration.actor)] = registration.actor
            for pending in self._pending.values():
                actors[id(pending.actor)] = pending.actor
                if not pending.future.done():
                    pending.future.set_exception(SourceAdmissionError(
                        "managed source registry is closed"))
            for future in self._reserved.values():
                if not future.done():
                    future.set_exception(SourceAdmissionError(
                        "managed source registry is closed"))
            self._active.clear()
            self._pending.clear()
            self._reserved.clear()
            self._retiring.clear()
            self._deferred_admissions.clear()
            for actor in actors.values():
                actor.close(drain_gaps=True)
        self._capture_executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _release_capture_leases(leases: tuple[_CaptureLease, ...]) -> None:
        for lease in leases:
            lease.release()

    def _finish_capture(self, capture_id: int, leases: tuple[_CaptureLease, ...], *,
                        retain: bool) -> None:
        for lease in leases:
            lease.actor.finish_capture(lease)
        with self._lock:
            retained = retain and capture_id in self._capture_leases
            if not retained:
                self._release_capture_leases(leases)
            self._capture_count -= 1
            if self._capture_count or self._closed:
                deferred: tuple[Callable[[], None], ...] = ()
            else:
                deferred = tuple(self._deferred_admissions)
                self._deferred_admissions.clear()
        for admission in deferred:
            admission()
        self._notify()

    def _start_after_retirement(self, spec: ManagedSourceSpec, future: Future,
                                retired: Future) -> None:
        with self._lock:
            if self._capture_count:
                self._deferred_admissions.append(
                    lambda: self._start_after_retirement(spec, future, retired))
                return
        try:
            retired.result()
        except BaseException:
            # Actor close futures are terminal-success signals. Continue after a
            # close error because the old registration has already been retired.
            pass
        self._start_registration(spec, future)

    def _start_registration(self, spec: ManagedSourceSpec, future: Future) -> None:
        with self._lock:
            if self._capture_count:
                self._deferred_admissions.append(
                    lambda: self._start_registration(spec, future))
                return
            if future.done():
                if self._reserved.get(spec.name) is future:
                    self._reserved.pop(spec.name, None)
                return
            if self._closed:
                self._reserved.pop(spec.name, None)
                future.set_exception(SourceAdmissionError(
                    "managed source registry is closed"))
                return
            if self._reserved.get(spec.name) is not future:
                future.set_exception(SourceAdmissionError(
                    "managed source registration was retired"))
                return
            if spec.name in self._active or spec.name in self._pending:
                self._reserved.pop(spec.name, None)
                future.set_exception(SourceAdmissionError(
                    f"managed source {spec.name!r} is already registered"))
                return
            epoch = _allocate_registration_epoch()
            actor = _SourceActor(spec, epoch, self._wake, self._clock_ns,
                                 self._busy_timeout_ns, self._retry_wait)
            pending = _PendingRegistration(spec, epoch, actor, future)
            self._pending[spec.name] = pending
            self._reserved.pop(spec.name, None)
        actor.ready.add_done_callback(
            lambda ready: self._registration_ready(pending, ready))
        try:
            actor.start()
        except BaseException as error:
            with self._lock:
                if self._pending.get(spec.name) is pending:
                    self._pending.pop(spec.name, None)
            future.set_exception(error)
            actor.close()

    def _registration_ready(self, pending: _PendingRegistration,
                            ready: Future) -> None:
        spec = pending.spec
        try:
            ready.result()
        except BaseException as error:
            with self._lock:
                if self._pending.get(spec.name) is pending:
                    self._pending.pop(spec.name, None)
            if not pending.future.done():
                pending.future.set_exception(error)
            self._notify()
            return

        receipt = _RegistrationReceipt(
            spec.name, spec.owner, pending.registration_epoch, "active")
        with self._lock:
            if self._capture_count:
                self._deferred_admissions.append(
                    lambda: self._registration_ready(pending, ready))
                return
            if (self._closed or self._pending.get(spec.name) is not pending
                    or pending.actor.status != "active"):
                if self._pending.get(spec.name) is pending:
                    self._pending.pop(spec.name, None)
                if not pending.future.done():
                    pending.future.set_exception(SourceAdmissionError(
                        "managed source registration was retired before publication"))
                pending.actor.close()
                self._notify()
                return
            self._pending.pop(spec.name, None)
            self._active[spec.name] = _Registration(
                spec, pending.registration_epoch, pending.actor, receipt)
            self._positions[pending.registration_epoch] = ready.result()
        if not pending.future.done():
            pending.future.set_result(receipt)
        self._notify()

    def _retire_locked(self, name: str, reason: str) -> Future | None:
        del reason
        registration = self._active.pop(name, None)
        pending = self._pending.pop(name, None)
        reservation = self._reserved.pop(name, None)
        actors = []
        if registration is not None:
            actors.append(registration.actor)
        if pending is not None:
            actors.append(pending.actor)
        close_future = self._retiring.get(name)
        if actors:
            close_futures = [actor.close() for actor in actors]
            close_future = close_futures[0]
            self._retiring[name] = close_future
            close_future.add_done_callback(
                lambda completed: self._clear_retiring(name, completed))
        if reservation is not None and not reservation.done():
            reservation.set_exception(SourceAdmissionError(
                "managed source registration was retired"))
        if pending is not None and not pending.future.done():
            pending.future.set_exception(SourceAdmissionError(
                "managed source registration was retired"))
        return close_future

    def _clear_retiring(self, name: str, completed: Future) -> None:
        with self._lock:
            if self._retiring.get(name) is completed:
                self._retiring.pop(name, None)

    def _fail_reservation(self, name: str, future: Future,
                          error: BaseException) -> None:
        with self._lock:
            if self._reserved.get(name) is future:
                self._reserved.pop(name, None)
            if not future.done():
                future.set_exception(error)
        self._notify()

    def _active_snapshot(self) -> tuple[_Registration, ...]:
        with self._lock:
            if self._closed:
                raise SourceAdmissionError("managed source registry is closed")
            registrations = self._ordered_registrations(self._active)
            if not registrations:
                raise SourceAdmissionError(
                    "managed source registry has no active sources")
            return registrations

    @staticmethod
    def _ordered_registrations(
        registrations: dict[str, _Registration],
    ) -> tuple[_Registration, ...]:
        names = sorted(registrations, key=lambda name: name.encode("utf-8"))
        return tuple(registrations[name] for name in names)

    def _collect(
            self,
            registrations: tuple[_Registration, ...],
            operation: str,
            leases: tuple[_CaptureLease, ...] | None = None,
    ) -> tuple[LocalPosition, ...]:
        if leases is None:
            futures = [registration.actor.submit(operation)
                       for registration in registrations]
        else:
            futures = [registration.actor.submit(operation, lease)
                       for registration, lease in zip(
                           registrations, leases, strict=True)]
        positions = []
        for future in futures:
            result = future.result()
            if not isinstance(result, LocalPosition):
                raise SourceAdmissionError("managed source returned an invalid position")
            positions.append(result)
        return tuple(positions)

    def _verify_current(self, registrations: tuple[_Registration, ...]) -> None:
        with self._lock:
            if self._closed:
                raise SourceAdmissionError("managed source registry is closed")
            current = self._ordered_registrations(self._active)
            if current != registrations:
                raise SourceAdmissionError("managed source registrations changed")

    def _capture(self, capture_id: int,
                 registrations: tuple[_Registration, ...],
                 leases: tuple[_CaptureLease, ...]) -> CapturedSourceSet:
        with self._capture_lock:
            opening_positions = self._collect(registrations, "observe", leases)
            opening = LocalPositionVector(opening_positions)

            images = []
            for registration, lease in zip(registrations, leases, strict=True):
                future = registration.actor.submit("capture", lease)
                image = future.result()
                if not isinstance(image, bytes):
                    raise SourceAdmissionError("managed source returned an invalid image")
                images.append(CapturedSourceImage(
                    registration.spec.name,
                    registration.registration_epoch,
                    image,
                    hashlib.sha256(image).hexdigest(),
                ))

            closing_positions = self._collect(registrations, "observe", leases)
            closing = LocalPositionVector(closing_positions)
            image_tuple = tuple(images)
            capture = CapturedSourceSet(
                capture_id,
                opening,
                closing,
                image_tuple,
                _snapshot_digest(image_tuple),
            )
            with self._lock:
                if not self._closed:
                    self._capture_leases[capture_id] = (leases, 1)
                    self._positions.update(
                        (position.registration_epoch, position) for position in closing_positions)
            return capture

    def _notify(self) -> None:
        try:
            self._wake()
        except BaseException:
            pass
