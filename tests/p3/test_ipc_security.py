"""Peer-credential and endpoint-metadata authorization for the AppIO socket.

Threat model for this file. The socket lives in a root-owned directory and is
``root:appio``, but file permissions are only a first gate: any process that can
reach the socket can still claim whatever it likes in JSON. Two independent
forgeries therefore have to fail here:

* a **client** that is not root and not in the ``appio`` group must be refused
  even though it connected successfully -- its claimed identity in a message is
  never authority channel for identity;
* a **server** that answers on a perfectly permissioned path but is not owned by
  uid 0 must be refused by the client before any status or historical payload is
  decoded, because a socket file's mode does not prove who is on the other end.

Every vector below is either (a) a credential the kernel could actually report and
whose handling has a security consequence, or (b) hostile metadata for the runtime
path. Kernel credentials are injected, never obtained from a real root peer, and
the one genuine ``AF_UNIX`` socketpair asserts only the invoking process's own
identity. Each test names the threat in its docstring.
"""

from __future__ import annotations

import errno
import os
import socket
import stat
import struct
import tempfile

import pytest
from pathlib import Path

from appio.ipc_security import (
    Peer,
    PeerRejected,
    RefusalLog,
    authorize_client,
    read_peer,
    require_root_server,
    verify_runtime_paths,
)

APPIO_GID = 987


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class FakeSocket:
    """A socket-shaped object that records the exact calls made to it."""

    def __init__(
        self,
        payload: bytes | None = None,
        error: OSError | None = None,
        *,
        peer_name: str = "",
        peer_name_error: OSError | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.peer_name = peer_name
        self.peer_name_error = peer_name_error
        self.calls: list[tuple[int, int, int]] = []
        self.peer_name_calls = 0

    def getpeername(self) -> str:
        self.peer_name_calls += 1
        if self.peer_name_error is not None:
            raise self.peer_name_error
        return self.peer_name

    def getsockopt(self, level: int, option: int, buflen: int) -> bytes:
        self.calls.append((level, option, buflen))
        if self.error is not None:
            raise self.error
        assert self.payload is not None
        return self.payload


class FakeStat:
    """The three ``stat`` fields peer verification is allowed to read."""

    def __init__(self, mode: int, uid: int = 0, gid: int = APPIO_GID) -> None:
        self.st_mode = mode
        self.st_uid = uid
        self.st_gid = gid


class FakeMetadata:
    """A ``metadata`` injection: path -> stat, with a call log and no filesystem."""

    def __init__(self, entries: dict[os.PathLike[str], FakeStat]) -> None:
        self.entries = {Path(key): value for key, value in entries.items()}
        self.calls: list[Path] = []

    def __call__(self, path: os.PathLike[str]) -> FakeStat:
        self.calls.append(path)
        try:
            return self.entries[path]
        except KeyError:
            raise FileNotFoundError(errno.ENOENT, "no such path", str(path)) from None


def groups(*gids: int):
    """A supplementary-group provider that records which uid it was asked about."""

    asked: list[int] = []

    def lookup(uid: int) -> frozenset[int]:
        asked.append(uid)
        return frozenset(gids)

    lookup.asked = asked  # type: ignore[attr-defined]
    return lookup


def refusing(exc: BaseException):
    """A provider that fails the way a vanished/locked account lookup fails."""

    def lookup(uid: int) -> frozenset[int]:
        raise exc

    return lookup


# --------------------------------------------------------------------------- #
# authorization: supplementary membership and the root fast path
# --------------------------------------------------------------------------- #


def test_supplementary_membership_and_root_server_are_independent():
    """Threat: a peer's group claim and the server's uid are different facts.

    Authorizing a client must not imply that the *server* is root, and refusing a
    non-root server must not depend on the client's groups.
    """

    peer = Peer(pid=73, uid=1000, gid=1000)
    assert authorize_client(
        peer, appio_gid=987, supplementary_groups=lambda uid: frozenset({987})
    )
    assert not authorize_client(
        peer, appio_gid=987, supplementary_groups=lambda uid: frozenset()
    )
    with pytest.raises(PeerRejected):
        require_root_server(peer)
    require_root_server(Peer(pid=74, uid=0, gid=0))


def test_root_client_is_authorized_without_asking_any_account_database():
    """Threat: a broken account lookup must not lock the root daemon's own owner out.

    Root is explicitly allowed by design, so the authorization decision must not
    depend on a successful ``getpwuid``/``getgrouplist`` at all.
    """

    lookup = refusing(KeyError("no such account"))
    assert authorize_client(Peer(pid=1, uid=0, gid=0), appio_gid=APPIO_GID, supplementary_groups=lookup)
    # A primary-group-only root peer is still root.
    assert authorize_client(Peer(pid=1, uid=0, gid=4242), appio_gid=APPIO_GID, supplementary_groups=lookup)


def test_primary_appio_group_is_authorized_without_a_supplementary_lookup():
    """Threat: an account whose primary group is appio must not need a lookup.

    Otherwise an unreadable or temporarily missing account would deny the very
    user the operator just added to the group.
    """

    asked: list[int] = []

    def lookup(uid: int) -> frozenset[int]:
        asked.append(uid)
        raise AssertionError("primary-group membership must not require the account DB")

    assert authorize_client(Peer(pid=7, uid=1000, gid=APPIO_GID), appio_gid=APPIO_GID, supplementary_groups=lookup)
    assert asked == []


def test_missing_account_and_lookup_failure_are_denied_and_logged():
    """Threat: fail-open on a vanished account authorizes any numeric uid.

    ``pwd.getpwuid`` raises ``KeyError`` and ``os.getgrouplist`` can raise
    ``OSError``; both must deny, and the refusal must be visible in a bounded log
    rather than silently swallowed.
    """

    log = RefusalLog()
    peer = Peer(pid=11, uid=4242, gid=4242)
    assert not authorize_client(
        peer, appio_gid=APPIO_GID, supplementary_groups=refusing(KeyError(4242)), refusals=log
    )
    assert not authorize_client(
        peer,
        appio_gid=APPIO_GID,
        supplementary_groups=refusing(OSError(errno.EIO, "nss failed")),
        refusals=log,
    )
    assert len(log) == 2
    assert {entry.reason for entry in log.records} == {"group_lookup_failed"}
    assert {entry.uid for entry in log.records} == {4242}


def test_supplementary_group_provider_failure_fails_closed():
    """Threat: an unexpected provider failure must deny, not crash the accept loop."""

    log = RefusalLog()
    assert not authorize_client(
        Peer(pid=12, uid=4243, gid=4243),
        appio_gid=APPIO_GID,
        supplementary_groups=refusing(RuntimeError("broken nss module")),
        refusals=log,
    )
    assert [entry.reason for entry in log.records] == ["group_lookup_failed"]


def test_non_member_is_denied_and_logged_with_a_bounded_log():
    """Threat: an authorized-looking connection from a non-member must be refused.

    The refusal log is bounded, so a connection flood cannot grow root memory.
    """

    log = RefusalLog(capacity=4)
    lookup = lambda uid: frozenset({5, 6})  # noqa: E731 - a deliberately dumb provider
    for _ in range(50):
        assert not authorize_client(
            Peer(pid=13, uid=1000, gid=1000), appio_gid=APPIO_GID, supplementary_groups=lookup, refusals=log
        )
    assert len(log) == 4
    assert {entry.reason for entry in log.records} == {"not_member"}


def test_authorization_uses_the_peer_uid_and_never_the_server_group_list(monkeypatch):
    """Threat: authorizing with ``os.getgroups()`` would accept the daemon's groups.

    The server process is in the ``appio`` group by construction. If it consulted
    its own group list instead of the peer's account, every client would be
    authorized. ``os.getgroups`` must not be called, and the provider must receive
    the peer's uid, not the daemon's.
    """

    def forbidden() -> list[int]:
        raise AssertionError("the server's own group list is not the peer's group list")

    monkeypatch.setattr(os, "getgroups", forbidden)
    monkeypatch.setattr(os, "getgrouplist", forbidden)
    lookup = groups(APPIO_GID)
    assert authorize_client(
        Peer(pid=21, uid=4321, gid=4321), appio_gid=APPIO_GID, supplementary_groups=lookup
    )
    assert lookup.asked == [4321]


def test_pid_is_never_consulted_so_a_recycled_or_dead_pid_cannot_change_the_verdict():
    """Threat: PID reuse. Credentials are read once from the kernel; the pid is a
    diagnostic, so a recycled or already-exited pid must not alter a decision."""

    allowed = lambda uid: frozenset({APPIO_GID})  # noqa: E731
    for pid in (0, 1, 999_999):
        assert authorize_client(
            Peer(pid=pid, uid=1000, gid=1000), appio_gid=APPIO_GID, supplementary_groups=allowed
        )
        assert not authorize_client(
            Peer(pid=pid, uid=1000, gid=1000), appio_gid=APPIO_GID, supplementary_groups=groups()
        )


def test_peer_rejects_impossible_credentials():
    """Threat: a negative or boolean uid silently comparing equal to root/group 0.

    ``True == 1`` and ``-1`` are the classic ways a partially validated credential
    slips past an ``==`` comparison.
    """

    for kwargs in (
        {"pid": -1, "uid": 1000, "gid": 1000},
        {"pid": 1, "uid": -1, "gid": 1000},
        {"pid": 1, "uid": 1000, "gid": -2},
        {"pid": 1, "uid": True, "gid": 1000},
        {"pid": True, "uid": 1000, "gid": 1000},
        {"pid": 1, "uid": 2**32, "gid": 1000},
    ):
        with pytest.raises(ValueError):
            Peer(**kwargs)


def test_require_root_server_rejects_a_group_zero_but_non_root_peer():
    """Threat: gid 0 is not uid 0. A forged server running as a group-0 normal user
    must be refused before its payload is trusted."""

    with pytest.raises(PeerRejected) as caught:
        require_root_server(Peer(pid=31, uid=1000, gid=0))
    assert isinstance(caught.value, PermissionError)
    assert caught.value.reason == "server_uid_nonzero"
    for uid in (1, 65534):
        with pytest.raises(PeerRejected):
            require_root_server(Peer(pid=32, uid=uid, gid=0))


# --------------------------------------------------------------------------- #
# read_peer: one kernel credential read, exact size, unsigned uids
# --------------------------------------------------------------------------- #


def test_read_peer_reads_the_kernel_credentials_exactly_once():
    """Threat: re-reading credentials per message lets a pid/uid change mid-
    connection, and a guessed size can truncate the struct."""

    payload = struct.pack("iII", 1234, 1000, 1000)
    sock = FakeSocket(payload=payload)
    peer = read_peer(sock)  # type: ignore[arg-type]
    assert peer == Peer(pid=1234, uid=1000, gid=1000)
    assert sock.calls == [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)]


def test_read_peer_rejects_a_short_or_long_credential_buffer():
    """Threat: ``struct.unpack`` on a truncated buffer either raises an untyped
    error or, if the code slices, silently invents a uid. Both are refusals."""

    for payload in (b"", b"\x00" * 8, b"\x00" * 16):
        with pytest.raises(PeerRejected) as caught:
            read_peer(FakeSocket(payload=payload))  # type: ignore[arg-type]
        assert caught.value.reason == "invalid_peercred"
        assert isinstance(caught.value, PermissionError)


def test_read_peer_fails_closed_when_the_kernel_call_fails():
    """Threat: an unconnected or reset socket must deny, never default to uid 0."""

    with pytest.raises(PeerRejected) as caught:
        read_peer(FakeSocket(error=OSError(errno.ENOTCONN, "not connected")))  # type: ignore[arg-type]
    assert caught.value.reason == "peercred_unavailable"


def test_read_peer_decodes_unsigned_uid_and_gid():
    """Threat: signed decoding turns uid 4294967295 into -1, which no longer
    compares equal to any real group and would deny the wrong peer, or worse, be
    treated as "unset" and authorized by a fallback."""

    payload = struct.pack("iII", 4321, 0xFFFFFFFF, 0xFFFFFFFE)
    peer = read_peer(FakeSocket(payload=payload))  # type: ignore[arg-type]
    assert (peer.pid, peer.uid, peer.gid) == (4321, 4294967295, 4294967294)


def test_read_peer_refuses_a_listening_socket_with_no_peer():
    """Threat: a listening socket has no peer, and Linux echoes the *calling*
    process's credentials from SO_PEERCRED. In a root daemon that is uid 0, so a
    caller that reads credentials from the listener instead of the accepted socket
    would authorize a connection that does not exist as root. The refusal must be
    typed and must not depend on the caller noticing."""

    directory = tempfile.mkdtemp()
    path = Path(directory) / "iod.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
        # The kernel would answer with our own credentials here; assert the guard
        # refuses before any credential is read.
        with pytest.raises(PeerRejected) as caught:
            read_peer(listener)
        assert caught.value.reason == "socket_not_connected"
    finally:
        listener.close()
        os.unlink(path)
        os.rmdir(directory)


def test_read_peer_refuses_a_bound_but_unconnected_socket():
    """Threat: the same echo, on a socket that was bound but never connected."""

    directory = tempfile.mkdtemp()
    path = Path(directory) / "iod.sock"
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        bound.bind(str(path))
        with pytest.raises(PeerRejected) as caught:
            read_peer(bound)
        assert caught.value.reason == "socket_not_connected"
    finally:
        bound.close()
        os.unlink(path)
        os.rmdir(directory)


def test_read_peer_refuses_before_reading_credentials_from_a_disconnected_socket():
    """Threat: reading first and judging later means a root daemon has already
    accepted its own echoed uid 0 as a peer. ``getsockopt`` must not be called at
    all when the socket has no peer."""

    sock = FakeSocket(payload=struct.pack("iII", 0, 0, 0), peer_name_error=OSError(errno.ENOTCONN, "not connected"))
    with pytest.raises(PeerRejected) as caught:
        read_peer(sock)  # type: ignore[arg-type]
    assert caught.value.reason == "socket_not_connected"
    assert sock.calls == []
    assert sock.peer_name_calls == 1


def test_read_peer_on_a_real_unix_socketpair_matches_the_calling_process():
    """A genuine kernel read on an AF_UNIX socketpair; no root peer is required."""

    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer = read_peer(left)
    finally:
        left.close()
        right.close()
    assert (peer.pid, peer.uid, peer.gid) == (os.getpid(), os.getuid(), os.getgid())


# --------------------------------------------------------------------------- #
# verify_runtime_paths: root:appio 0750 directory, root:appio 0660 socket
# --------------------------------------------------------------------------- #

SOCKET_PATH = os.path.join(os.sep, "run", "appio", "iod.sock")
RUNTIME_DIR = os.path.dirname(SOCKET_PATH)


def installed_layout(**overrides) -> FakeMetadata:
    entries = {
        RUNTIME_DIR: FakeStat(stat.S_IFDIR | 0o750),
        SOCKET_PATH: FakeStat(stat.S_IFSOCK | 0o660),
    }
    entries.update(overrides)
    return FakeMetadata(entries)


def test_verify_runtime_paths_accepts_the_installed_layout():
    """The positive case the daemon and client both rely on."""

    metadata = installed_layout()
    verify_runtime_paths(SOCKET_PATH, metadata=metadata, appio_gid=APPIO_GID)
    # The parent is checked before the socket, and both are checked exactly once.
    assert metadata.calls == [Path(RUNTIME_DIR), Path(SOCKET_PATH)]


def test_verify_runtime_paths_rejects_a_directory_only_check():
    """Threat: validating the directory but not the socket lets anyone replace the
    socket inode with their own listener inside a correctly permissioned dir."""

    metadata = FakeMetadata({RUNTIME_DIR: FakeStat(stat.S_IFDIR | 0o750)})
    with pytest.raises(PeerRejected) as caught:
        verify_runtime_paths(SOCKET_PATH, metadata=metadata, appio_gid=APPIO_GID)
    assert caught.value.reason == "socket_missing"


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        # A world-writable or group-writable directory lets an unprivileged user
        # unlink and replace the socket.
        ({RUNTIME_DIR: FakeStat(stat.S_IFDIR | 0o777)}, "directory_mode"),
        ({RUNTIME_DIR: FakeStat(stat.S_IFDIR | 0o770)}, "directory_mode"),
        # A user-owned directory can be replaced wholesale.
        ({RUNTIME_DIR: FakeStat(stat.S_IFDIR | 0o750, uid=1000)}, "directory_owner"),
        # Wrong group means the wrong population can connect.
        ({RUNTIME_DIR: FakeStat(stat.S_IFDIR | 0o750, gid=1000)}, "directory_group"),
        # A symlinked directory redirects every later check to attacker-chosen
        # metadata (the classic TOCTOU path substitution).
        ({RUNTIME_DIR: FakeStat(stat.S_IFLNK | 0o777)}, "path_symlink"),
        ({RUNTIME_DIR: FakeStat(stat.S_IFREG | 0o750)}, "directory_not_directory"),
        ({SOCKET_PATH: FakeStat(stat.S_IFSOCK | 0o666)}, "socket_mode"),
        ({SOCKET_PATH: FakeStat(stat.S_IFSOCK | 0o660, uid=1000)}, "socket_owner"),
        ({SOCKET_PATH: FakeStat(stat.S_IFSOCK | 0o660, gid=1000)}, "socket_group"),
        ({SOCKET_PATH: FakeStat(stat.S_IFLNK | 0o777)}, "path_symlink"),
        ({SOCKET_PATH: FakeStat(stat.S_IFREG | 0o660)}, "socket_not_socket"),
        ({SOCKET_PATH: FakeStat(stat.S_IFDIR | 0o660)}, "socket_not_socket"),
    ],
)
def test_verify_runtime_paths_rejects_forged_metadata(override, expected_reason):
    """Threat: a non-root/foreign-owned or symlinked endpoint that still "looks"
    like the installed layout. Each variant must be a typed refusal."""

    metadata = installed_layout(**override)
    with pytest.raises(PeerRejected) as caught:
        verify_runtime_paths(SOCKET_PATH, metadata=metadata, appio_gid=APPIO_GID)
    assert caught.value.reason == expected_reason
    assert isinstance(caught.value, PermissionError)


def test_verify_runtime_paths_rejects_a_missing_endpoint():
    """Threat: absence must be a refusal, not a silent success in the caller."""

    with pytest.raises(PeerRejected) as caught:
        verify_runtime_paths(SOCKET_PATH, metadata=FakeMetadata({}), appio_gid=APPIO_GID)
    assert caught.value.reason == "directory_missing"


def test_verify_runtime_paths_refuses_a_relative_path():
    """Threat: a relative socket path validates whatever directory the daemon
    happens to be started in, so an attacker-chosen CWD becomes the "trusted"
    parent. Only the fixed absolute runtime path can be verified."""

    metadata = FakeMetadata(
        {
            Path("."): FakeStat(stat.S_IFDIR | 0o750),
            Path("iod.sock"): FakeStat(stat.S_IFSOCK | 0o660),
        }
    )
    with pytest.raises(ValueError):
        verify_runtime_paths("iod.sock", metadata=metadata, appio_gid=APPIO_GID)
    assert metadata.calls == []


def test_verify_runtime_paths_rejects_an_invalid_group_configuration():
    """Threat: ``appio_gid=0`` would authorize the whole root group by accident."""

    for bad in (0, -1, True, "987", None):
        with pytest.raises(ValueError):
            verify_runtime_paths(SOCKET_PATH, metadata=installed_layout(), appio_gid=bad)


def test_verify_runtime_paths_does_not_follow_symlinks_in_production_metadata():
    """Threat: a follow-mode ``stat`` reports the symlink *target*, so an attacker
    who can plant a link inside a writable directory would be described as the
    trusted layout. The production default must be ``lstat``."""

    from appio import ipc_security

    assert ipc_security.PRODUCTION_METADATA is os.lstat
    assert ipc_security.PRODUCTION_METADATA is not os.stat


# --------------------------------------------------------------------------- #
# authorization contract details
# --------------------------------------------------------------------------- #


def test_authorize_client_rejects_an_invalid_group_configuration():
    """Threat: a missing/zero appio gid must be a configuration error, not a
    wildcard that authorizes nobody or everybody."""

    for bad in (0, -1, True, "987", None):
        with pytest.raises(ValueError):
            authorize_client(
                Peer(pid=1, uid=1000, gid=1000),
                appio_gid=bad,
                supplementary_groups=groups(APPIO_GID),
            )


def test_authorize_client_requires_a_peer_instance():
    """Threat: a dict or a JSON-derived object must not be authorized as a peer."""

    with pytest.raises(TypeError):
        authorize_client(  # type: ignore[arg-type]
            {"pid": 1, "uid": 0, "gid": 0},
            appio_gid=APPIO_GID,
            supplementary_groups=groups(APPIO_GID),
        )


def test_refusal_log_is_bounded_and_reports_oldest_first():
    """The log the daemon prints is a bounded ring, not an unbounded list."""

    log = RefusalLog(capacity=3)
    for uid in (1, 2, 3, 4, 5):
        log.record("not_member", Peer(pid=uid, uid=uid, gid=uid))
    assert [entry.uid for entry in log.records] == [3, 4, 5]
    with pytest.raises(ValueError):
        RefusalLog(capacity=0)
