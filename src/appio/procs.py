"""Read ``/proc``-shaped fixture records into stable process identities.

A fixture root is a directory laid out like ``/proc``: numeric PID directories
containing ``stat``, ``io``, ``status``, ``cgroup``, and an ``exe`` symlink, plus
a ``boot_id`` file for the boot identity. Tests copy such a tree into a temporary
directory; nothing here opens the host ``/proc``.

``read_stable`` reads one logical record in the normative order: stat-before, the
IO outcome plus cgroup/exe/status metadata, then stat-after. A start-time change
between the two stat reads returns ``None``: no partial sample is emitted, and
the sampler layer keeps the previously open identity. A vanished process
(``ESRCH``/``ENOENT``) is distinguishable from a permission failure
(``EACCES``): the former uses ``status="esrch"`` and the latter
``status="eacces"`` with ``errno_name="EACCES"``. Dumpability is separate
harness metadata and never synthesizes a permission fact.

A PID that disappears before the first ``stat`` read has no start time and
therefore no identity; ``read_stable`` raises :class:`ProcessVanished` for it so
the sampler can close an already-open identity and count the ESRCH reset.
Returning ``None`` is reserved for an identity that is merely unstable, where the
sampler must preserve its prior state.
"""

from __future__ import annotations

import errno as errno_module
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .contracts import CounterVector, validate_sqlite_integer

__all__ = [
    "AppKeyInputs",
    "IoReadOutcome",
    "ProcReader",
    "ProcessIdentity",
    "ProcessStat",
    "ProcessVanished",
    "StableProcRecord",
    "parse_io",
    "parse_stat",
    "parse_status_uid",
]

IoStatus = Literal["readable", "eacces", "esrch", "error"]
_STATUSES = ("readable", "eacces", "esrch", "error")

_KERNEL_IO_NAMES = (
    "rchar",
    "wchar",
    "syscr",
    "syscw",
    "read_bytes",
    "write_bytes",
    "cancelled_write_bytes",
)
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_PID_DIRECTORY = re.compile(r"[1-9][0-9]*\Z")


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL")
    return value


def _decimal(text: str, field: str) -> int:
    """Parse one canonical non-negative decimal token inside SQLite's range."""

    if not isinstance(text, str) or _DECIMAL.fullmatch(text) is None:
        raise ValueError(f"{field} must be a non-negative decimal integer, got {text!r}")
    return validate_sqlite_integer(int(text), field)


def _positive_decimal(text: str, field: str) -> int:
    value = _decimal(text, field)
    if value == 0:
        raise ValueError(f"{field} must be positive, got 0")
    return value


@dataclass(frozen=True)
class ProcessIdentity:
    """Stable process identity: a boot plus a PID plus its start time."""

    boot_id: str
    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        _require_text(self.boot_id, "identity.boot_id")
        value = validate_sqlite_integer(self.pid, "identity.pid")
        if value == 0:
            raise ValueError("identity.pid must be positive, got 0")
        validate_sqlite_integer(self.start_ticks, "identity.start_ticks")


@dataclass(frozen=True)
class ProcessStat:
    """The subset of ``/proc/<pid>/stat`` the sampler consumes."""

    pid: int
    comm: str
    state: str
    ppid: int
    start_ticks: int

    def __post_init__(self) -> None:
        value = validate_sqlite_integer(self.pid, "stat.pid")
        if value == 0:
            raise ValueError("stat.pid must be positive, got 0")
        if not isinstance(self.comm, str):
            raise ValueError("stat.comm must be a string")
        _require_text(self.state, "stat.state")
        validate_sqlite_integer(self.ppid, "stat.ppid")
        validate_sqlite_integer(self.start_ticks, "stat.start_ticks")


@dataclass(frozen=True)
class IoReadOutcome:
    """One IO read's outcome, with permission facts separate from metadata."""

    status: IoStatus
    counters: CounterVector | None
    dumpable: bool | None
    errno_name: str | None

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"io.status must be one of {', '.join(_STATUSES)}")
        if self.status == "readable":
            if not isinstance(self.counters, tuple) or len(self.counters) != 4:
                raise ValueError("a readable io outcome requires a four-counter tuple")
            for index, value in enumerate(self.counters):
                validate_sqlite_integer(value, f"io.counters[{index}]")
        elif self.counters is not None:
            raise ValueError(f"an {self.status} io outcome must not carry counters")
        if self.dumpable is not None and not isinstance(self.dumpable, bool):
            raise ValueError("io.dumpable must be a boolean or None")
        if self.errno_name is not None:
            _require_text(self.errno_name, "io.errno_name")


@dataclass(frozen=True)
class AppKeyInputs:
    """The three inputs :func:`appio.identity.resolve_app` accepts."""

    cgroup_path: str | None
    exe: str | None
    desktop_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("cgroup_path", "exe", "desktop_id"):
            value = getattr(self, field)
            if value is not None:
                _require_text(value, f"app_key_inputs.{field}")


@dataclass(frozen=True)
class StableProcRecord:
    """One accepted logical record for a stable process identity."""

    identity: ProcessIdentity
    ppid: int
    uid: int | None
    exe: str | None
    cgroup_path: str | None
    app_key_inputs: AppKeyInputs
    io_outcome: IoReadOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProcessIdentity):
            raise ValueError("record.identity must be a ProcessIdentity")
        validate_sqlite_integer(self.ppid, "record.ppid")
        if self.uid is not None:
            validate_sqlite_integer(self.uid, "record.uid")
        for field in ("exe", "cgroup_path"):
            value = getattr(self, field)
            if value is not None:
                _require_text(value, f"record.{field}")
        if not isinstance(self.app_key_inputs, AppKeyInputs):
            raise ValueError("record.app_key_inputs must be AppKeyInputs")
        if not isinstance(self.io_outcome, IoReadOutcome):
            raise ValueError("record.io_outcome must be an IoReadOutcome")


def parse_stat(text: str) -> ProcessStat:
    """Parse ``/proc/<pid>/stat`` using the final ``)`` that ends ``comm``.

    ``comm`` may itself contain ``)`` and spaces, so the first ``)`` is not a
    delimiter. Field 22 (``starttime``) is tail index 19 once the tail starts at
    field 3 (``state``).
    """

    if not isinstance(text, str):
        raise ValueError("stat text must be a string")
    opening = text.find("(")
    closing = text.rfind(")")
    if opening <= 0 or closing <= opening:
        raise ValueError(f"stat text has no comm-delimiting parentheses: {text!r}")

    pid = _positive_decimal(text[:opening].strip(), "stat.pid")
    comm = text[opening + 1 : closing]
    tail = text[closing + 1 :].split()
    if len(tail) < 20:
        raise ValueError(f"stat text is too short to hold field 22: {text!r}")

    return ProcessStat(
        pid=pid,
        comm=comm,
        state=tail[0],
        ppid=_decimal(tail[1], "stat.ppid"),
        start_ticks=_decimal(tail[19], "stat.start_ticks"),
    )


def parse_io(text: str) -> CounterVector:
    """Return ``(rchar, wchar, read_bytes, write_bytes)`` from ``io`` text."""

    if not isinstance(text, str):
        raise ValueError("io text must be a string")

    values: dict[str, int] = {}
    for line in text.splitlines():
        if line.strip() == "":
            continue
        name, separator, raw = line.partition(":")
        if separator == "":
            raise ValueError(f"malformed io line: {line!r}")
        name = name.strip()
        if name not in _KERNEL_IO_NAMES:
            # A future kernel counter is not a malformed known counter; the four
            # accounted counters and their validation are unaffected.
            continue
        if name in values:
            raise ValueError(f"duplicate io counter: {name!r}")
        values[name] = _decimal(raw.strip(), f"io.{name}")

    missing = [name for name in _KERNEL_IO_NAMES if name not in values]
    if missing:
        raise ValueError(f"io text is missing counter(s): {', '.join(missing)}")

    return (
        values["rchar"],
        values["wchar"],
        values["read_bytes"],
        values["write_bytes"],
    )


def parse_status_uid(text: str) -> int:
    """Return the real UID from a ``/proc/<pid>/status`` fixture."""

    if not isinstance(text, str):
        raise ValueError("status text must be a string")
    uid_lines = [line for line in text.splitlines() if line.startswith("Uid:")]
    if len(uid_lines) != 1:
        raise ValueError("status text must contain exactly one Uid line")
    fields = uid_lines[0][len("Uid:") :].split()
    if len(fields) != 4:
        raise ValueError("status Uid line must hold four fields")
    parsed = [_decimal(field, "status.uid") for field in fields]
    return parsed[0]


def _errno_name(error: OSError) -> str | None:
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "ENOENT"
    if isinstance(error, PermissionError):
        return "EACCES"
    if isinstance(error.errno, int):
        return errno_module.errorcode.get(error.errno, f"E{error.errno}")
    return None


def _esrch_outcome() -> IoReadOutcome:
    return IoReadOutcome(status="esrch", counters=None, dumpable=None, errno_name="ENOENT")


class ProcessVanished(Exception):
    """An enumerated PID disappeared before any identity could be established.

    This is deliberately distinguishable from ``read_stable`` returning ``None``:
    ``None`` means the identity is unstable (a changed start time or a malformed
    stat record) and the sampler should preserve any previously open identity,
    while this exception proves the PID is gone and its open identity -- if one
    exists in the sampler's own state -- must be closed and counted as ESRCH.
    """

    def __init__(self, pid: int, errno_name: str = "ENOENT") -> None:
        super().__init__(f"process {pid} vanished during a logical read ({errno_name})")
        self.pid = pid
        self.errno_name = errno_name


class ProcReader:
    """Read one ``/proc``-shaped tree without touching the host process table."""

    def __init__(
        self,
        root: Path,
        dumpability: Callable[[int], bool | None] | None = None,
        *,
        boot_id: str | None = None,
    ) -> None:
        self._root = Path(root)
        self._dumpability = dumpability
        if boot_id is not None:
            _require_text(boot_id, "boot_id")
        self._boot_id_override = boot_id

    @property
    def root(self) -> Path:
        return self._root

    def _boot_id(self) -> str:
        if self._boot_id_override is not None:
            return self._boot_id_override
        try:
            text = (self._root / "boot_id").read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(f"no boot_id fixture under {self._root}") from error
        return _require_text(text, "boot_id")

    def _pid_dir(self, pid: int) -> Path:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"pid must be a positive integer, got {pid!r}")
        return self._root / str(pid)

    def enumerate_pids(self) -> tuple[int, ...]:
        """Return only canonical positive numeric process directories, ascending."""

        try:
            entries = list(self._root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return ()
        pids = [
            int(entry.name)
            for entry in entries
            if entry.is_dir() and _PID_DIRECTORY.fullmatch(entry.name) is not None
        ]
        return tuple(sorted(pids))

    def read_stat(self, pid: int) -> ProcessStat:
        text = (self._pid_dir(pid) / "stat").read_text(encoding="utf-8")
        return parse_stat(text)

    def _dumpable(self, pid: int) -> bool | None:
        if self._dumpability is None:
            return None
        value = self._dumpability(pid)
        if value is not None and not isinstance(value, bool):
            raise ValueError("dumpability provider must return bool or None")
        return value

    def read_io(self, pid: int) -> IoReadOutcome:
        """Read ``io``, mapping EACCES, ENOENT, and malformed content distinctly."""

        path = self._pid_dir(pid) / "io"
        try:
            text = path.read_text(encoding="utf-8")
        except PermissionError as error:
            return IoReadOutcome(
                status="eacces",
                counters=None,
                dumpable=self._dumpable(pid),
                errno_name=_errno_name(error),
            )
        except (FileNotFoundError, NotADirectoryError) as error:
            return IoReadOutcome(
                status="esrch",
                counters=None,
                dumpable=None,
                errno_name=_errno_name(error),
            )
        except OSError as error:
            return IoReadOutcome(
                status="error",
                counters=None,
                dumpable=self._dumpable(pid),
                errno_name=_errno_name(error),
            )
        except ValueError:
            # Undecodable bytes are a malformed record, not an OS-level error.
            return IoReadOutcome(
                status="error",
                counters=None,
                dumpable=self._dumpable(pid),
                errno_name=None,
            )

        try:
            counters = parse_io(text)
        except ValueError:
            return IoReadOutcome(
                status="error",
                counters=None,
                dumpable=self._dumpable(pid),
                errno_name=None,
            )
        return IoReadOutcome(
            status="readable",
            counters=counters,
            dumpable=self._dumpable(pid),
            errno_name=None,
        )

    def read_stable(self, pid: int) -> StableProcRecord | None:
        """Read one logical record, or ``None`` when identity is not stable."""

        pid_dir = self._pid_dir(pid)
        try:
            stat_before = self.read_stat(pid)
        except (FileNotFoundError, NotADirectoryError) as error:
            # The PID is gone rather than merely unstable. The sampler owns the
            # previously open identity and must be able to close it and count the
            # ESRCH reset, so this case is signalled, never folded into None.
            raise ProcessVanished(pid, _errno_name(error) or "ENOENT") from error
        except (OSError, ValueError):
            # No identity can be established and the PID was not proven absent.
            return None

        io_outcome = self.read_io(pid)

        uid: int | None = None
        exe: str | None = None
        cgroup_path: str | None = None
        missing = io_outcome.status == "esrch"
        metadata_error: str | None = None

        try:
            uid = parse_status_uid((pid_dir / "status").read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError):
            missing = True
        except PermissionError as error:
            metadata_error = _errno_name(error)
        except OSError as error:
            metadata_error = _errno_name(error)
        except ValueError:
            metadata_error = "metadata"

        try:
            cgroup_path = self._parse_cgroup(
                (pid_dir / "cgroup").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, NotADirectoryError):
            missing = True
        except PermissionError as error:
            metadata_error = _errno_name(error)
        except OSError as error:
            metadata_error = _errno_name(error)
        except ValueError:
            metadata_error = "metadata"

        try:
            exe = os.readlink(pid_dir / "exe")
        except (FileNotFoundError, NotADirectoryError):
            missing = True
        except OSError as error:
            metadata_error = _errno_name(error)

        try:
            stat_after = self.read_stat(pid)
        except (FileNotFoundError, NotADirectoryError):
            return self._record(
                stat_before, uid, exe, cgroup_path, _esrch_outcome()
            )
        except PermissionError as error:
            return self._record(
                stat_before,
                uid,
                exe,
                cgroup_path,
                IoReadOutcome("error", None, io_outcome.dumpable, _errno_name(error)),
            )
        except OSError as error:
            return self._record(
                stat_before,
                uid,
                exe,
                cgroup_path,
                IoReadOutcome("error", None, io_outcome.dumpable, _errno_name(error)),
            )
        except ValueError:
            return self._record(
                stat_before,
                uid,
                exe,
                cgroup_path,
                IoReadOutcome("error", None, io_outcome.dumpable, None),
            )

        if stat_after.start_ticks != stat_before.start_ticks:
            return None

        if missing or io_outcome.status == "esrch":
            outcome = _esrch_outcome()
        elif metadata_error is not None:
            # A record whose identity metadata cannot be read is not accepted as
            # a sample: counters without a trustworthy app/parent attribution
            # would be credited to a guess. ENOENT above is ESRCH; every other
            # metadata failure stays distinguishable through errno_name.
            outcome = IoReadOutcome(
                status="error",
                counters=None,
                dumpable=io_outcome.dumpable,
                errno_name=None if metadata_error == "metadata" else metadata_error,
            )
        else:
            outcome = io_outcome

        return self._record(stat_before, uid, exe, cgroup_path, outcome)

    @staticmethod
    def _parse_cgroup(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("0::"):
                path = line[len("0::") :]
                if path.startswith("/"):
                    return path
                raise ValueError(f"cgroup path is not absolute: {path!r}")
        raise ValueError("cgroup text has no 0:: unified-hierarchy line")

    def _record(
        self,
        stat: ProcessStat,
        uid: int | None,
        exe: str | None,
        cgroup_path: str | None,
        io_outcome: IoReadOutcome,
    ) -> StableProcRecord:
        return StableProcRecord(
            identity=ProcessIdentity(
                boot_id=self._boot_id(),
                pid=stat.pid,
                start_ticks=stat.start_ticks,
            ),
            ppid=stat.ppid,
            uid=uid,
            exe=exe,
            cgroup_path=cgroup_path,
            app_key_inputs=AppKeyInputs(
                cgroup_path=cgroup_path,
                exe=exe,
                desktop_id=None,
            ),
            io_outcome=io_outcome,
        )
