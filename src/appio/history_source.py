"""Managed, actor-owned SQLite sources for local history observations.

Each source actor owns one long-lived observer connection and performs every
SQLite operation, including backup and close, on its own thread. Registry
results contain immutable records only. The source URI and connection never
leave the actor.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import math
import queue
import sqlite3
import threading
from urllib.parse import parse_qsl, unquote, urlsplit
from typing import Callable

from .history_types import (
    CapturedSourceImage,
    CapturedSourceSet,
    LocalPosition,
    LocalPositionVector,
    ManagedSourceSpec,
)
from .migrations import validate_schema

__all__ = ["ManagedSourceRegistry", "SourceAdmissionError"]


class SourceAdmissionError(RuntimeError):
    """A managed source could not be safely admitted or observed."""


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
_READ_ONLY_PRAGMAS = frozenset(("data_version", "database_list", "user_version"))
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


def _failed_future(error: BaseException) -> Future:
    future = Future()
    future.set_exception(error)
    return future


def _safe_error(spec: ManagedSourceSpec, error: BaseException, phase: str) -> SourceAdmissionError:
    if isinstance(error, SourceAdmissionError):
        return error
    return SourceAdmissionError(
        f"managed source {spec.name!r} {phase} failed")


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


def _connection_options(spec: ManagedSourceSpec) -> dict[str, object]:
    options: dict[str, object] = {}
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


def _open_read_only_connection(spec: ManagedSourceSpec) -> sqlite3.Connection:
    _validate_file_uri(spec.database_uri)
    options = _connection_options(spec)
    isolation_level = options.pop("isolation_level", None)
    connection = sqlite3.connect(
        spec.database_uri,
        uri=True,
        isolation_level=isolation_level,
        check_same_thread=True,
        **options,
    )
    try:
        validate_schema(connection)

        connection.execute("PRAGMA query_only=ON").close()
        cursor = connection.execute("PRAGMA query_only")
        try:
            query_only = cursor.fetchone()
        finally:
            cursor.close()
        if query_only is None or query_only[0] != 1:
            raise SourceAdmissionError("managed source query-only mode is unavailable")
        connection.set_authorizer(_read_only_authorizer)
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
        if second is not None or pragma not in _READ_ONLY_PRAGMAS:
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and second is not None:
        if second.lower() == "load_extension":
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _position(connection: sqlite3.Connection, spec: ManagedSourceSpec,
              registration_epoch: int) -> LocalPosition:
    if connection.in_transaction:
        raise SourceAdmissionError("managed source has an uncommitted transaction")
    cursor = connection.execute("PRAGMA main.data_version")
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    if connection.in_transaction:
        raise SourceAdmissionError("managed source observation opened a transaction")
    if row is None:
        raise SourceAdmissionError("managed source data_version is unavailable")
    return LocalPosition(
        spec.name, spec.owner, registration_epoch, row[0], connection.total_changes)


def _backup_image(connection: sqlite3.Connection) -> bytes:
    if connection.in_transaction:
        raise SourceAdmissionError("managed source has an uncommitted transaction")
    destination = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.backup(destination, pages=128)
        if connection.in_transaction or destination.in_transaction:
            raise SourceAdmissionError("managed source backup left a transaction open")
        return destination.serialize(name="main")
    finally:
        destination.close()


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
                 wake: Callable[[], None]) -> None:
        self.spec = spec
        self.registration_epoch = registration_epoch
        self._wake = wake
        self._commands: queue.Queue[
            tuple[object, Future | None, _CaptureLease | None]
        ] = queue.Queue()
        self._deferred: list[tuple[object, Future]] = []
        self._leases: set[_CaptureLease] = set()
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

    def submit(self, operation: str, lease: _CaptureLease | None = None) -> Future:
        future: Future = Future()
        with self._status_lock:
            if lease is None:
                if self._status != "active":
                    future.set_exception(SourceAdmissionError(
                        f"managed source {self.spec.name!r} is not active"))
                    return future
                if self._leases:
                    self._deferred.append((operation, future))
                else:
                    self._commands.put((operation, future, None))
            elif (lease.actor is not self or lease not in self._leases
                  or self._status not in ("active", "retiring")):
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
            return lease

    def close(self) -> Future:
        with self._status_lock:
            if self._status not in ("retiring", "closed", "failed"):
                self._status = "retiring"
            if self._status == "retiring" and not self._leases:
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
            if self._leases:
                return
            for command in self._deferred:
                self._commands.put((*command, None))
            self._deferred.clear()
            if self._status == "retiring":
                self._queue_stop_locked()

    def _set_status(self, status: str) -> None:
        with self._status_lock:
            self._status = status

    def _run(self) -> None:
        try:
            self._connection = _open_read_only_connection(self.spec)
            initial = _position(self._connection, self.spec, self.registration_epoch)
        except BaseException as error:
            self._set_status("failed")
            self._finish_close()
            if not self.ready.done():
                self.ready.set_exception(_safe_error(self.spec, error, "admission"))
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
                    admitted = (self._status == "active" or (
                        lease is not None and lease in self._leases
                        and self._status == "retiring"))
                if not admitted:
                    future.set_exception(SourceAdmissionError(
                        f"managed source {self.spec.name!r} is retired"))
                    self._notify()
                    continue
                try:
                    if operation == "observe":
                        result = _position(
                            self._connection, self.spec, self.registration_epoch)
                    elif operation == "capture":
                        result = _backup_image(self._connection)
                    else:
                        raise SourceAdmissionError("managed source operation is invalid")
                except BaseException as error:
                    future.set_exception(_safe_error(self.spec, error, "operation"))
                else:
                    future.set_result(result)
                self._notify()
        finally:
            self._set_status("closed")
            self._finish_close()

    def _finish_close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
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

    def __init__(self, *, wake: Callable[[], None]) -> None:
        if not callable(wake):
            raise ValueError("wake must be callable")
        self._wake = wake
        self._lock = threading.RLock()
        self._active: dict[str, _Registration] = {}
        self._pending: dict[str, _PendingRegistration] = {}
        self._reserved: dict[str, Future] = {}
        self._retiring: dict[str, Future] = {}
        self._next_capture_id = 1
        self._capture_lock = threading.Lock()
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
            if spec.owner == "gui":
                return _failed_future(SourceAdmissionError(
                    "GUI source registration requires writer pairing admission"))
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
        if spec.owner == "gui":
            self._fail_reservation(name, future, SourceAdmissionError(
                "GUI source registration requires writer pairing admission"))
        elif retiring is None:
            self._start_registration(spec, future)
        else:
            retiring.add_done_callback(
                lambda completed: self._start_after_retirement(
                    spec, future, completed))
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

    def observe(self) -> LocalPositionVector:
        """Read every active actor in UTF-8 name order."""

        registrations = self._active_snapshot()
        positions = self._collect(registrations, "observe")
        self._verify_current(registrations)
        return LocalPositionVector(positions)

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
                return _failed_future(_safe_error(
                    registrations[0].spec, error, "capture admission"))
        try:
            future = self._capture_executor.submit(
                self._capture, capture_id, registrations, tuple(leases))
        except RuntimeError:
            for lease in leases:
                lease.release()
            return _failed_future(SourceAdmissionError(
                "managed source registry is closed"))
        future.add_done_callback(
            lambda _completed: self._release_capture_leases(leases))
        future.add_done_callback(lambda _completed: self._notify())
        return future

    def close(self) -> None:
        """Reject new work and queue connection closes on all owning actors."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
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
            for actor in actors.values():
                actor.close()
        self._capture_executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _release_capture_leases(leases: tuple[_CaptureLease, ...]) -> None:
        for lease in leases:
            lease.release()

    def _start_after_retirement(self, spec: ManagedSourceSpec, future: Future,
                                retired: Future) -> None:
        try:
            retired.result()
        except BaseException:
            # Actor close futures are terminal-success signals. Continue after a
            # close error because the old registration has already been retired.
            pass
        self._start_registration(spec, future)

    def _start_registration(self, spec: ManagedSourceSpec, future: Future) -> None:
        with self._lock:
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
            actor = _SourceActor(spec, epoch, self._wake)
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
            future.set_exception(_safe_error(spec, error, "actor startup"))
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
                pending.future.set_exception(_safe_error(spec, error, "admission"))
            self._notify()
            return

        receipt = _RegistrationReceipt(
            spec.name, spec.owner, pending.registration_epoch, "active")
        with self._lock:
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
            try:
                result = future.result()
            except BaseException as error:
                raise _safe_error(registrations[0].spec, error, operation) from None
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
                try:
                    image = future.result()
                except BaseException as error:
                    raise _safe_error(registration.spec, error, "capture") from None
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
            return CapturedSourceSet(
                capture_id,
                opening,
                closing,
                tuple(images),
                digest.hexdigest(),
            )

    def _notify(self) -> None:
        try:
            self._wake()
        except BaseException:
            pass
