"""Who is allowed to talk, and who is allowed to be listened to (design §5, B11).

The AppIO local socket is protected by filesystem permissions, but a permission
bit is not an identity. This module supplies the two independent checks that make
"connected" different from "trusted":

* **Server side** — :func:`read_peer` reads ``SO_PEERCRED`` once, from the kernel,
  for the *connected* socket. Connectedness is a precondition: a listening or
  unconnected socket has no peer, and the kernel echoes the caller's own
  credentials there, which in a root daemon would read as uid 0.
  :func:`authorize_client` then allows uid 0 or a member of the ``appio`` group.
  A supplementary group set is resolved for that peer's *own* uid; the server's
  own group list is never consulted, because the daemon is in ``appio`` by
  construction and using it would authorize everybody.
* **Client side** — :func:`verify_runtime_paths` checks the fixed runtime
  directory/socket ownership and modes without following symlinks, and
  :func:`require_root_server` checks that the process at the other end is uid 0.
  The client performs both before it decodes a status or historical payload,
  because a correctly permissioned socket path does not prove who is listening.

Failure policy is uniform and deliberate: *deny*. No path here returns a default,
falls back to the local user, or accepts an identity claimed in JSON. Group
membership changes take effect for new authenticated connections only, which is
why credentials are read once per connection rather than per message.

Test seams (alternate ``metadata``, injected ``supplementary_groups``) exist only
as call parameters; production argv and environment never enable them.
"""

from __future__ import annotations

import os
import socket
import stat
import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MAX_REFUSALS",
    "PEERCRED",
    "PRODUCTION_METADATA",
    "RUNTIME_DIRECTORY_MODE",
    "ROOT_UID",
    "SOCKET_MODE",
    "Peer",
    "PeerRejected",
    "Refusal",
    "RefusalLog",
    "authorize_client",
    "read_peer",
    "require_root_server",
    "verify_runtime_paths",
]

PEERCRED = struct.Struct("iII")
"""``struct ucred``: ``pid_t`` (signed), ``uid_t`` and ``gid_t`` (unsigned).

The unsigned fields matter: decoding a uid of ``4294967295`` as ``-1`` would make
it compare unequal to every real group and could be mistaken for "unset".
"""

ROOT_UID = 0
"""The only uid a client may accept as a daemon (design §5)."""

RUNTIME_DIRECTORY_MODE = 0o750
"""``/run/appio`` — ``root:appio 0750`` (design §5)."""

SOCKET_MODE = 0o660
"""The socket itself — ``root:appio 0660`` (design §5)."""

MAX_REFUSALS = 32
"""How many refusals :class:`RefusalLog` retains before overwriting the oldest."""

_MAX_CREDENTIAL = 2**32 - 1
_MAX_PID = 2**31 - 1


class PeerRejected(PermissionError):
    """A refused endpoint, with a stable machine ``reason``.

    ``PeerRejected`` covers both directions of endpoint trust (a client that is
    not permitted, and a server that is not root) because both are the same
    decision for a caller: *stop, do not decode anything from this peer*. It is a
    :class:`PermissionError` so a caller that already handles permission failures
    refuses these by default instead of crashing.

    ``reason`` is the token a transport logs or maps to a refusal message.
    ``str(error)`` is bounded human detail and always contains the token.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        if not isinstance(reason, str) or reason == "":
            raise ValueError("reason must be a non-empty string")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("detail must be a string or None")
        text = reason if detail is None else f"{reason}: {detail}"
        super().__init__(text)
        self.reason = reason
        self.detail = detail


def _credential(value: object, field: str, maximum: int) -> int:
    """Validate one kernel credential field; booleans are not integers here."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"peer.{field} must be an integer")
    if value < 0 or value > maximum:
        raise ValueError(f"peer.{field} must be in [0, {maximum}]")
    return value


@dataclass(frozen=True, slots=True)
class Peer:
    """The credentials the kernel reported for one connected peer.

    ``pid`` is diagnostic only. It can already be recycled by the time it is read,
    so authorization must depend on ``uid``/``gid`` alone.
    """

    pid: int
    uid: int
    gid: int

    def __post_init__(self) -> None:
        _credential(self.pid, "pid", _MAX_PID)
        _credential(self.uid, "uid", _MAX_CREDENTIAL)
        _credential(self.gid, "gid", _MAX_CREDENTIAL)


@dataclass(frozen=True, slots=True)
class Refusal:
    """One bounded refusal record; no payload text is ever retained."""

    reason: str
    pid: int
    uid: int


class RefusalLog:
    """A bounded ring of refusals the daemon can log without growing memory.

    The design asks for refusal logging; an unbounded list would itself be a
    memory-growth vector for an attacker who connects repeatedly and is refused.
    """

    __slots__ = ("_capacity", "_records")

    def __init__(self, capacity: int = MAX_REFUSALS) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._records: deque[Refusal] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def records(self) -> tuple[Refusal, ...]:
        """Retained refusals, oldest first."""

        return tuple(self._records)

    def record(self, reason: str, peer: Peer) -> None:
        if not isinstance(reason, str) or reason == "":
            raise ValueError("refusal reason must be a non-empty string")
        if not isinstance(peer, Peer):
            raise TypeError("refusals are recorded for a Peer")
        self._records.append(Refusal(reason=reason, pid=peer.pid, uid=peer.uid))

    def __len__(self) -> int:
        return len(self._records)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"RefusalLog(capacity={self._capacity}, records={len(self._records)})"


def read_peer(sock: socket.socket) -> Peer:
    """Read ``SO_PEERCRED`` exactly once for a *connected* ``sock``.

    Uses ``getsockopt`` rather than ``/proc/<pid>`` so a recycled pid or a
    non-dumpable peer cannot change the answer: the kernel snapshots the
    credentials when ``connect()``/``accept()`` happens.

    Connectedness is checked first because peer credentials are only meaningful
    for a socket that has a peer. On Linux, ``getsockopt(SO_PEERCRED)`` on a
    listening or unconnected socket echoes the **calling** process's credentials;
    in a root daemon that reads as ``uid 0``, which would authorize a socket that
    nobody is on. ``getpeername`` raises ``ENOTCONN`` there, so this refuses
    before reading anything, with reason ``socket_not_connected``.
    """

    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:  # pragma: no cover - non-Linux hosts
        raise PeerRejected(
            "peercred_unsupported", "SO_PEERCRED is not available on this platform"
        )

    try:
        sock.getpeername()
    except AttributeError as exc:
        raise PeerRejected("socket_not_connected", "the object is not a socket") from exc
    except OSError as exc:
        raise PeerRejected(
            "socket_not_connected",
            f"the socket has no peer: {exc.strerror or exc}",
        ) from exc

    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, PEERCRED.size)
    except OSError as exc:
        raise PeerRejected(
            "peercred_unavailable", f"getsockopt(SO_PEERCRED) failed: {exc.strerror or exc}"
        ) from exc

    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise PeerRejected(
            "invalid_peercred", f"expected a {PEERCRED.size}-byte buffer"
        )
    data = bytes(raw)
    if len(data) != PEERCRED.size:
        # Never slice or pad: a shorter buffer would silently drop the gid, and a
        # longer one means this is not the struct we think it is.
        raise PeerRejected(
            "invalid_peercred", f"expected {PEERCRED.size} bytes, got {len(data)}"
        )

    pid, uid, gid = PEERCRED.unpack(data)
    try:
        return Peer(pid=pid, uid=uid, gid=gid)
    except ValueError as exc:
        raise PeerRejected("invalid_peercred", str(exc)) from exc


def _check_appio_gid(appio_gid: object) -> int:
    if isinstance(appio_gid, bool) or not isinstance(appio_gid, int) or appio_gid < 1:
        raise ValueError("appio_gid must be a positive integer")
    if appio_gid > _MAX_CREDENTIAL:
        raise ValueError("appio_gid is outside the uid/gid range")
    return appio_gid


def authorize_client(
    peer: Peer,
    *,
    appio_gid: int,
    supplementary_groups: Callable[[int], frozenset[int]],
    refusals: RefusalLog | None = None,
) -> bool:
    """Decide whether ``peer`` may talk to a daemon socket.

    Root is allowed. Otherwise the peer's **primary** group or a supplementary
    group of that peer's own account must be ``appio_gid``. Every failure denies;
    none of them raises, so one broken account lookup cannot stop the accept loop.

    ``refusals`` is optional and receives one bounded record per denial so the
    daemon can log the decision without an unbounded list of its own.
    """

    appio_gid = _check_appio_gid(appio_gid)
    if not isinstance(peer, Peer):
        raise TypeError("peer must be a Peer read from SO_PEERCRED")
    if not callable(supplementary_groups):
        raise TypeError("supplementary_groups must be callable")
    if refusals is not None and not isinstance(refusals, RefusalLog):
        raise TypeError("refusals must be a RefusalLog or None")

    if peer.uid == ROOT_UID:
        return True
    if peer.gid == appio_gid:
        # Primary-group membership needs no account database. Requiring a lookup
        # here would deny the operator who was just added to the group whenever
        # NSS is slow or the account is temporarily unavailable.
        return True

    try:
        member = appio_gid in supplementary_groups(peer.uid)
    except Exception:  # noqa: BLE001 - any lookup failure is a denial, never a crash
        # pwd.getpwuid raises KeyError for a vanished account, getgrouplist raises
        # OSError when NSS is unavailable, and a broken provider can raise anything
        # else. All of them mean "cannot prove membership", which is not membership.
        _refuse(refusals, "group_lookup_failed", peer)
        return False

    if not member:
        _refuse(refusals, "not_member", peer)
        return False
    return True


def _refuse(log: RefusalLog | None, reason: str, peer: Peer) -> None:
    if log is not None:
        log.record(reason, peer)


def require_root_server(peer: Peer) -> None:
    """Refuse a server endpoint that is not uid 0 (design §5).

    Raises :class:`PeerRejected` (a :class:`PermissionError`) with reason
    ``server_uid_nonzero``. A group-0 or otherwise privileged non-root server is
    still refused: the client trusts data because of uid 0, not because of mode
    bits on the socket file.
    """

    if not isinstance(peer, Peer):
        raise TypeError("peer must be a Peer read from SO_PEERCRED")
    if peer.uid != ROOT_UID:
        raise PeerRejected(
            "server_uid_nonzero", f"server uid {peer.uid} is not root"
        )


PRODUCTION_METADATA: Callable[[Path], os.stat_result] = os.lstat
"""Production metadata reader: ``lstat``, which never follows a symlink."""


def verify_runtime_paths(
    path: Path,
    *,
    metadata: Callable[[Path], object] = PRODUCTION_METADATA,
    appio_gid: int,
) -> None:
    """Verify the installed runtime directory and socket before trusting either.

    Requires the socket's parent directory to be ``root:appio 0750`` and the
    socket itself to be ``root:appio 0660``, both read with ``lstat`` so a symlink
    is a refusal rather than a redirect. Raises :class:`PeerRejected` with a
    reason naming the failing fact.

    ``metadata`` is a test seam only: production passes :data:`PRODUCTION_METADATA`
    and there is no argv or environment switch that changes it.
    """

    appio_gid = _check_appio_gid(appio_gid)
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("path must be a filesystem path")
    socket_path = Path(path)
    if not socket_path.is_absolute():
        # A relative path would make the "trusted parent directory" check describe
        # whatever the daemon's current working directory happens to be.
        raise ValueError("path must be absolute")
    directory = socket_path.parent

    directory_stat = _lookup(metadata, directory, "directory")
    _require_not_symlink(directory_stat, directory)
    _require_type(directory_stat, directory, stat.S_ISDIR, "directory_not_directory")
    _require_owner(directory_stat, directory, appio_gid, "directory_owner", "directory_group")
    _require_mode(directory_stat, directory, RUNTIME_DIRECTORY_MODE, "directory_mode")

    socket_stat = _lookup(metadata, socket_path, "socket")
    _require_not_symlink(socket_stat, socket_path)
    _require_type(socket_stat, socket_path, stat.S_ISSOCK, "socket_not_socket")
    _require_owner(socket_stat, socket_path, appio_gid, "socket_owner", "socket_group")
    _require_mode(socket_stat, socket_path, SOCKET_MODE, "socket_mode")


def _lookup(
    metadata: Callable[[Path], object], path: Path, field: str
) -> os.stat_result:
    try:
        found = metadata(path)
    except FileNotFoundError as exc:
        raise PeerRejected(f"{field}_missing", f"{path} does not exist") from exc
    except OSError as exc:
        raise PeerRejected(
            f"{field}_missing", f"{path} cannot be inspected: {exc.strerror or exc}"
        ) from exc
    for attribute in ("st_mode", "st_uid", "st_gid"):
        if not hasattr(found, attribute):
            raise PeerRejected(f"{field}_metadata_invalid", f"{path} has no {attribute}")
    return found  # type: ignore[return-value]


def _require_not_symlink(metadata_result: object, path: Path) -> None:
    if stat.S_ISLNK(metadata_result.st_mode):  # type: ignore[attr-defined]
        # Checked before the type: a symlink's own S_IFMT is S_IFLNK, and every
        # later check would otherwise describe the link instead of its target.
        raise PeerRejected("path_symlink", f"{path} is a symbolic link")


def _require_type(metadata_result: object, path: Path, predicate, reason: str) -> None:
    if not predicate(metadata_result.st_mode):  # type: ignore[attr-defined]
        raise PeerRejected(reason, f"{path} has the wrong file type")


def _require_owner(
    metadata_result: object,
    path: Path,
    appio_gid: int,
    owner_reason: str,
    group_reason: str,
) -> None:
    uid = metadata_result.st_uid  # type: ignore[attr-defined]
    gid = metadata_result.st_gid  # type: ignore[attr-defined]
    if uid != ROOT_UID:
        raise PeerRejected(owner_reason, f"{path} is owned by uid {uid}, not root")
    if gid != appio_gid:
        raise PeerRejected(group_reason, f"{path} is owned by gid {gid}, not {appio_gid}")


def _require_mode(metadata_result: object, path: Path, expected: int, reason: str) -> None:
    actual = stat.S_IMODE(metadata_result.st_mode)  # type: ignore[attr-defined]
    if actual != expected:
        raise PeerRejected(
            reason, f"{path} has mode {actual:04o}, expected {expected:04o}"
        )
