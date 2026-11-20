"""Single-threaded collector authority and a pinned, per-user writer lease.

Service absence, not a broken socket, permits GUI collection. The caller must
reconcile an unknown/transition state BEFORE invoking a service-enable helper.
The zero-argument factory constructs a baseline-only collector, or consumes
``coordinator.resume_handover`` via LiveCollector's existing constructor seam.
No GUI checkpoint is uploaded to the daemon.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Callable, Literal

from .handover import HandoverOffer, IncompleteHandoverError, capture_handover
from .history_export import ExportResult
from .history_router import HistoryRouter
from .iodclient import IodClient
from .live import LiveCollector
from .migrations import validate_schema
from .query import QueryService
from .store import AppIoStore


OwnerKind = Literal["gui", "iod", "unknown"]
HistoryKey = tuple[OwnerKind, bool, bool, bool]


@dataclass(frozen=True)
class ServiceFacts:
    enabled: bool | None
    active: Literal["absent", "inactive", "active", "unknown"]
    daemon_verified: bool
    answering: bool
    pending_activation: bool | None
    sub_state: str | None = None
    result: str | None = None
    restarts: int | None = None
    sample_ns: int | None = None

    def __post_init__(self):
        if self.active not in ("absent", "inactive", "active", "unknown"):
            raise ValueError("invalid service active state")
        for name in ("enabled", "pending_activation"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean or None")
        if type(self.daemon_verified) is not bool or type(self.answering) is not bool:
            raise ValueError("daemon facts must be boolean")
        for name in ("sub_state", "result"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a nonempty string or None")
        if self.restarts is not None and (
            type(self.restarts) is not int or self.restarts < 0
        ):
            raise ValueError("restarts must be a nonnegative integer or None")
        if self.sample_ns is not None and (
            type(self.sample_ns) is not int or self.sample_ns <= 0
        ):
            raise ValueError("sample_ns must be a positive integer or None")


@dataclass(frozen=True)
class OwnerDecision:
    owner: OwnerKind
    may_collect_local: bool
    state: str


def decide_owner(facts: ServiceFacts) -> OwnerDecision:
    if facts.daemon_verified and facts.answering:
        return OwnerDecision("iod", False,
            "on, needs activation" if facts.pending_activation else "on, answering")
    if facts.active in ("absent", "inactive"):
        return OwnerDecision("gui", True, "off")
    return OwnerDecision("unknown", False,
        "enabled, not answering" if facts.enabled else "unavailable")


class GuiLease:
    """An advisory writer lock; never unlink the inode on release.

    The runtime directory must already exist, belong to this UID and not be
    group/world-writable. Every path component is opened without symlink
    traversal. The directory descriptor remains pinned while the lease is held.
    """
    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        self._parent = self._fd = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        parent = descriptor = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            parent = os.open("/", flags)
            for component in self.path.parent.parts[1:]:
                child = os.open(component, flags, dir_fd=parent)
                os.close(parent)
                parent = child
            info = os.fstat(parent)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return False
            descriptor = os.open(self.path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600, dir_fd=parent)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o077):
                return False
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            visible = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (info.st_dev, info.st_ino):
                return False
            self._parent, self._fd = parent, descriptor
            parent = descriptor = None
            return True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM, errno.EAGAIN, errno.ENOENT,
                               errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
                return False
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent is not None:
                os.close(parent)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._parent is not None:
            os.close(self._parent)
            self._parent = None


class CollectorCoordinator:
    def __init__(self, *, local_factory: Callable[[], LiveCollector], client: IodClient,
                 history: HistoryRouter, lease: GuiLease,
                 clock: Callable[[], tuple[int, int]]):
        self.local_factory, self.client, self.history = local_factory, client, history
        self.lease, self.clock = lease, clock
        self._thread = threading.get_ident()
        self._busy = self._closed = False
        self._local = None
        self._decision = OwnerDecision("unknown", False, "unavailable")
        self._facts = None
        self._history_key: HistoryKey | None = None
        self._exports = []
        self.resume_handover: HandoverOffer | None = None
        self.outgoing_handover: HandoverOffer | None = None
        self.boundary_issues: list[str] = []

    def _check(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError("collector transitions must run on the owner thread")
        if self._busy:
            raise RuntimeError("collector transition/tick is already in progress")
        if self._closed:
            raise RuntimeError("collector coordinator is closed")

    def _stop_local(self):
        local, self._local = self._local, None
        try:
            if local is not None:
                self.outgoing_handover = None
                try:
                    self.outgoing_handover = local.quiesce()
                    if self.outgoing_handover is None:
                        self.boundary_issues.append("no committed outgoing GUI interval")
                except IncompleteHandoverError as error:
                    self.boundary_issues.append(str(error))
                finally:
                    local.close()
        finally:
            self.lease.release()

    def reconcile(self, facts: ServiceFacts) -> OwnerDecision:
        self._check()
        self._busy = True
        try:
            decision = decide_owner(facts)
            self._facts = facts
            # Revoke permission before touching a writer: even a failed quiesce
            # must not admit another local frame on the next timer callback.
            self._decision = OwnerDecision("unknown", False, "transitioning")
            if not decision.may_collect_local:
                self.resume_handover = None
                self._stop_local()
            elif self._local is None:
                if not self.lease.acquire():
                    decision = OwnerDecision("gui", False, "off, viewer (collector lease held)")
                else:
                    try:
                        local = self.local_factory()
                        fallback = getattr(local, "handover_fallback_reason", None)
                        if (getattr(local, "source", "gui") != "gui" or
                                not local.baseline_only and local.last_frame is None and not fallback):
                            local.close()
                            raise ValueError("local factory requires GUI baseline_only or a validated resume_handover")
                        if fallback:
                            self.boundary_issues.append(f"unclean incoming handover: {fallback}")
                        self._local = local
                        self.resume_handover = None
                    except BaseException:
                        self.lease.release()
                        raise
            self._decision = decision
            key: HistoryKey = (
                decision.owner,
                decision.may_collect_local,
                facts.daemon_verified,
                facts.answering,
            )
            if key != self._history_key:
                self.history.invalidate()
            self._history_key = key
            return decision
        finally:
            self._busy = False

    def tick_local(self) -> bool:
        self._check()
        if not self._decision.may_collect_local or self._local is None:
            return False
        self._busy = True
        try:
            self._local.tick()
            return True
        finally:
            self._busy = False

    def accept_export(self, result: ExportResult) -> None:
        """Retain a helper export read-only, never replace the user's database.

        A matching export is only a candidate offer. The factory's LiveCollector
        install still validates its *local* complete predecessor chain; missing
        cross-store evidence correctly degrades through the producer's seam.
        After accepting an inactive-service export, reconcile once more before
        ticking: the zero-argument factory then consumes ``resume_handover``.
        A GUI that has already committed retains the export as history only.
        """
        self._check()
        self._busy = True
        try:
            self._accept_export(result)
        finally:
            self._busy = False

    def _accept_export(self, result):
        if not isinstance(result, ExportResult):
            raise ValueError("expected typed helper export")
        connection = sqlite3.connect(Path(result.path).absolute().as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            validate_schema(connection)
            store = AppIoStore(connection)
            state = store.restore_checkpoint("iod", result.boot_id) if result.boot_id else None
            if state is None or (state.boot_id, state.epoch, state.last_seq, state.checkpoint_mono_ns) != (
                    result.boot_id, result.iod_epoch, result.seq, result.cutoff_mono_ns):
                raise ValueError("helper export cutoff does not match its durable checkpoint")
            interval = connection.execute("SELECT from_mono_ns FROM collector_interval "
                "WHERE source='iod' AND boot_id=? AND epoch=?", (state.boot_id, state.epoch)).fetchone()
            offer = None
            if interval is not None:
                offer = HandoverOffer("iod", "gui", state.epoch, state.last_seq,
                                      interval[0], capture_handover(state))
                try:
                    store._validate_handover_offer(offer)
                except (ValueError, IncompleteHandoverError) as error:
                    self.boundary_issues.append(f"export has no complete ownership chain: {error}")
                    offer = None
            self.history.add_source(QueryService(connection),
                collector_id=f"export:{state.boot_id}:{state.epoch}:{state.last_seq}", owner="iod")
            self._exports.append(connection)
            connection = None
            if self._facts is not None and self._facts.active in ("absent", "inactive"):
                if self._local is not None and self._local.last_frame is not None:
                    self.resume_handover = None
                    self.boundary_issues.append("GUI already committed; export is history only")
                else:
                    # An inactive fact lazily constructs a fresh collector. Retire
                    # that still-unsampled instance so the next reconcile can
                    # consume this offer before its first baseline/tick.
                    self._decision = OwnerDecision("gui", False, "off, awaiting handover")
                    self._stop_local()
                    self.resume_handover = offer
            else:
                self.resume_handover = None
                self.boundary_issues.append("export is not proof of an orderly stopped service")
            if offer is None:
                self.boundary_issues.append("export boundary unavailable; GUI must baseline fresh")
        finally:
            if connection is not None:
                connection.close()

    def close(self) -> None:
        if self._closed:
            return
        self._check()
        self._decision = OwnerDecision("unknown", False, "closed")
        try:
            self._stop_local()
        finally:
            try:
                self.client.close()
            finally:
                for connection in self._exports:
                    connection.close()
                self._closed = True
