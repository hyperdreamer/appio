"""Delegation install and the durable "activation pending" state.

The design's consent flow installs the cgroup-delegation drop-ins **and** then
enables the daemon, because enabling alone cannot produce per-app device data.
The drop-ins only take effect for a user manager started after them, and this
host's user manager survives logout (``Linger=yes``), so the honest product
statement is "delegation installed, activation pending" until a probe proves
working accounting. This module owns exactly that half:

* the two fixed drop-ins are written through :class:`~appio.setup_files.
  SafeRootFS` with canonical, idempotent content;
* an existing delegated-controller set is a stable sorted union with the
  required controllers, so installing never silently removes a delegation an
  administrator added;
* ``usermod -aG`` appends the supplementary group instead of replacing it, and
  the beneficiary is the NSS-validated identity rather than any claimed name;
* ``pending_activation`` is persisted at ``/var/lib/appio/setup-state.json``
  *before* either file is installed, in exact canonical bytes, under the helper
  lock, and is only ever cleared by a successful activation probe (Task 9).

No function here reports ``answering`` or "accounting active": installing
configuration is not evidence that device accounting works.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

from .setup_boundary import SetupDeps, SetupIdentity
from .setup_files import (
    MAX_TARGET_BYTES,
    SERVICE_DROPIN,
    STATE_FILE,
    USER_MANAGER_DROPIN,
    SafeRootFS,
    SafeRootFSError,
    manager_dropin_payload,
    parse_service_dropin,
    service_dropin_payload,
)

__all__ = [
    "DAEMON_RELOAD_ARGV",
    "DelegationError",
    "GETENT_GROUP_ARGV",
    "MAX_DIAGNOSTICS",
    "MAX_DIAGNOSTIC_CHARS",
    "MAX_STATE_BYTES",
    "REQUIRED_DELEGATE_CONTROLLERS",
    "SHOW_DELEGATES_ARGV",
    "STATE_VERSION",
    "SetupState",
    "SetupStateError",
    "install_delegation",
    "load_setup_state",
    "set_pending",
    "usermod_argv",
]

STATE_VERSION = 1
MAX_STATE_BYTES = 64 * 1024
MAX_DIAGNOSTICS = 16
MAX_DIAGNOSTIC_CHARS = 256

REQUIRED_DELEGATE_CONTROLLERS: frozenset[str] = frozenset({"cpu", "io", "memory", "pids"})

GETENT_GROUP_ARGV: tuple[str, ...] = ("/usr/bin/getent", "group", "appio")
SHOW_DELEGATES_ARGV: tuple[str, ...] = (
    "/usr/bin/systemctl",
    "show",
    "user@.service",
    "--property=Delegate",
    "--value",
)
DAEMON_RELOAD_ARGV: tuple[str, ...] = ("/usr/bin/systemctl", "daemon-reload")

_CANONICAL_UID = re.compile(r"(?:0|[1-9][0-9]{0,9})", re.ASCII)
_SAFE_USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", re.ASCII)
_CONTROLLER = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]", re.ASCII)
_UID_MAX = 2**32 - 2


class SetupStateError(ValueError):
    """The shared pending state exists but is not the exact version-1 shape."""


class DelegationError(RuntimeError):
    """The internal failure signal for one fixed installation operation.

    ``install_delegation`` catches it and returns ``result`` as the verb's
    bounded JSON, so callers never need exception plumbing. ``operation`` always
    names one fixed step, never a caller-supplied string, so "which operation
    failed" is answerable without free-form parsing. A lower-level caller (for
    example a future actor that composes the steps itself) may catch it instead.
    """

    def __init__(self, operation: str, reason: str, *, result: Mapping[str, object]) -> None:
        super().__init__(f"{operation}: {reason}")
        self.operation = operation
        self.reason = reason
        self.result = dict(result)


@dataclasses.dataclass(frozen=True)
class SetupState:
    """The shared on-disk pending state, versioned and read-only to callers."""

    version: int
    pending_by_uid: Mapping[int, bool]


# --------------------------------------------------------------------------- #
# Pending state
# --------------------------------------------------------------------------- #


def load_setup_state(fs: SafeRootFS, *, create_parents: bool = True) -> SetupState:
    """Read and validate the shared state; a missing file is legitimately empty.

    A present-but-invalid file is *not* treated as empty. It is evidence that
    something else is wrong, and reporting "unknown pending" is safer than
    letting a later write erase it.
    """

    try:
        if create_parents:
            raw = fs.read_bounded(str(STATE_FILE), max_bytes=MAX_STATE_BYTES)
        else:
            raw = fs.read_bounded(
                str(STATE_FILE), max_bytes=MAX_STATE_BYTES, create_parents=False
            )
    except FileNotFoundError:
        return SetupState(version=STATE_VERSION, pending_by_uid=MappingProxyType({}))
    except PermissionError as exc:
        raise SetupStateError(f"setup state is not readable: {getattr(exc, 'reason', exc)}") from exc
    return decode_setup_state(raw)


def decode_setup_state(raw: bytes) -> SetupState:
    if not isinstance(raw, bytes):
        raise SetupStateError("setup state must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SetupStateError("setup state is not UTF-8") from exc
    try:
        document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise SetupStateError("setup state is not canonical JSON") from exc
    if not isinstance(document, dict) or set(document) != {"version", "pending_by_uid"}:
        raise SetupStateError("setup state must have exactly version and pending_by_uid")
    version = document["version"]
    # ``type(...) is int`` rather than ``isinstance``: JSON 1.0 and 1e0 parse to
    # a float that compares equal to 1, and a non-canonical spelling is exactly
    # what this reader exists to refuse.
    if type(version) is not int or version != STATE_VERSION:
        raise SetupStateError(f"setup state version must be {STATE_VERSION}")
    pending = document["pending_by_uid"]
    if not isinstance(pending, dict):
        raise SetupStateError("pending_by_uid must be an object")
    parsed: dict[int, bool] = {}
    for key, value in pending.items():
        uid = _parse_state_uid(key)
        if not isinstance(value, bool):
            raise SetupStateError(f"pending_by_uid[{key!r}] must be a boolean")
        parsed[uid] = value
    return SetupState(version=STATE_VERSION, pending_by_uid=MappingProxyType(dict(sorted(parsed.items()))))


def encode_setup_state(pending_by_uid: Mapping[int, bool]) -> bytes:
    """The exact canonical bytes: ``{"version":1,"pending_by_uid":{...}}``."""

    ordered = {str(uid): pending_by_uid[uid] for uid in sorted(pending_by_uid)}
    body = json.dumps(ordered, separators=(",", ":"), allow_nan=False)
    return f'{{"version":{STATE_VERSION},"pending_by_uid":{body}}}'.encode("utf-8")


def set_pending(fs: SafeRootFS, uid: int, pending: bool) -> None:
    """Persist one UID's pending flag under the helper lock."""

    _require_uid(uid)
    _require_bool(pending, "pending")
    with fs.locked():
        current = load_setup_state(fs)
        merged = dict(current.pending_by_uid)
        merged[uid] = pending
        fs.atomic_write(str(STATE_FILE), encode_setup_state(merged), mode=0o600)


def _reject_duplicate_keys(pairs):
    seen: set[str] = set()
    rebuilt: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
        rebuilt[key] = value
    return rebuilt


def _parse_state_uid(key: object) -> int:
    if not isinstance(key, str) or _CANONICAL_UID.fullmatch(key) is None:
        raise SetupStateError(f"pending_by_uid key {key!r} is not a canonical decimal uid")
    uid = int(key)
    if uid < 1 or uid > _UID_MAX:
        raise SetupStateError(f"pending_by_uid key {key!r} is outside the uid range")
    return uid


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #


def usermod_argv(username: str) -> tuple[str, ...]:
    """The fixed append-only membership command for an NSS-validated name."""

    _require_username(username)
    return ("/usr/bin/usermod", "-aG", "appio", username)


def install_delegation(
    identity: SetupIdentity, *, deps: SetupDeps, fs: SafeRootFS
) -> dict[str, object]:
    """Install both drop-ins, append the group membership, and persist pending.

    Always returns the versioned result JSON -- ``delegation_installed`` is the
    success flag, and a failure carries ``error`` plus ``pending_activation`` so
    the verb dispatcher can report it without exception plumbing. Order is the
    contract: validate the identity and the effective UID, persist
    ``pending_activation=True``, prove the group exists, inspect the effective
    delegation, then write, then append membership, then ``daemon-reload``. A
    failure keeps ``pending_activation`` true (or false when pending itself could
    not be persisted) and names the failed fixed operation, which is what makes a
    retry safe and honest.
    """

    try:
        return _install_delegation(identity, deps=deps, fs=fs)
    except DelegationError as exc:
        # The bounded result is the report; raising would force every caller to
        # duplicate the same error JSON.
        return exc.result


def _install_delegation(
    identity: SetupIdentity, *, deps: SetupDeps, fs: SafeRootFS
) -> dict[str, object]:
    _require_identity(identity)
    _require_nss_account(identity, deps)
    if deps.effective_uid() != 0:
        raise _error("root", "root_required", identity, pending=False, diagnostics=[])

    diagnostics: list[str] = []
    try:
        lock = fs.locked()
        with lock:
            return _install_locked(identity, deps=deps, fs=fs, diagnostics=diagnostics)
    except DelegationError:
        raise
    except SafeRootFSError as exc:
        raise _error(
            "helper-lock",
            getattr(exc, "reason", "lock_failed"),
            identity,
            pending=False,
            diagnostics=[_describe(exc)],
        ) from exc


def _install_locked(
    identity: SetupIdentity,
    *,
    deps: SetupDeps,
    fs: SafeRootFS,
    diagnostics: list[str],
) -> dict[str, object]:
    # Before either drop-in exists: a crash now still surfaces as pending.
    try:
        set_pending(fs, identity.uid, True)
    except DelegationError:
        raise
    except Exception as exc:
        raise _error(
            "persist-pending",
            "state_unavailable",
            identity,
            pending=False,
            diagnostics=[_describe(exc)],
        ) from exc
    diagnostics.append(f"pending:uid={identity.uid}:true")

    _require_group(deps, identity, diagnostics)
    observed = _observed_delegated_controllers(deps, identity, diagnostics)
    existing = _existing_delegated_controllers(fs, identity)
    effective = frozenset(observed) | frozenset(existing) | REQUIRED_DELEGATE_CONTROLLERS
    diagnostics.append(f"delegates:{';'.join(sorted(effective))}")

    try:
        fs.atomic_write(str(SERVICE_DROPIN), service_dropin_payload(effective), mode=0o644)
    except Exception as exc:
        raise _error(
            "write-service-dropin", "write_failed", identity, pending=True,
            diagnostics=diagnostics + [_describe(exc)],
        ) from exc
    try:
        fs.atomic_write(str(USER_MANAGER_DROPIN), manager_dropin_payload(), mode=0o644)
    except Exception as exc:
        raise _error(
            "write-user-manager-dropin", "write_failed", identity, pending=True,
            diagnostics=diagnostics + [_describe(exc)],
        ) from exc

    _run(deps, usermod_argv(identity.username), "usermod", identity, diagnostics)
    _run(deps, DAEMON_RELOAD_ARGV, "daemon-reload", identity, diagnostics)

    return {
        "version": STATE_VERSION,
        "uid": identity.uid,
        "delegation_installed": True,
        "pending_activation": True,
        "diagnostics": _bounded(diagnostics),
    }


def _require_group(deps: SetupDeps, identity: SetupIdentity, diagnostics: list[str]) -> None:
    _run(deps, GETENT_GROUP_ARGV, "group-prerequisite", identity, diagnostics)
    diagnostics.append("group:appio=present")


def _observed_delegated_controllers(
    deps: SetupDeps, identity: SetupIdentity, diagnostics: list[str]
) -> frozenset[str]:
    """Read the currently delegated controllers. Advisory, never fatal.

    The observation exists to prove the installed state to a human, not to gate
    the write: ``install_delegation`` unions ``REQUIRED_DELEGATE_CONTROLLERS``
    into the content it writes, so a host that cannot answer the query still
    reaches the intended end state. A template unit needs an instance, and on
    systemd 262 ``systemctl show user@.service`` exits 1 with "neither a valid
    invocation ID nor unit name"; a host without that unit fails the same way.
    Treating either as fatal blocked installation on a real machine while the
    fixtures - which fake a successful answer - could never show it.
    """
    result = _run(deps, SHOW_DELEGATES_ARGV, "inspect-delegates", identity, diagnostics, allow_failure=True)
    returncode = getattr(result, "returncode", 0)
    if isinstance(returncode, int) and returncode != 0:
        detail = _first_line(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
        if not _is_uninstantiable_unit(detail):
            # Anything else - an unreachable bus, a broken manager - means the host
            # cannot be configured or reloaded, so writing drop-ins would be a claim
            # nobody can verify. Stay fatal.
            raise _error(
                "inspect-delegates",
                f"exit_{returncode}",
                identity,
                pending=True,
                diagnostics=diagnostics + [
                    f"inspect-delegates:{detail}" if detail else "inspect-delegates:no_output"
                ],
            )
        diagnostics.append(f"delegates:unobserved=exit_{returncode}")
        diagnostics.append("inspect-delegates:unit_needs_instance")
        return frozenset()
    text = str(getattr(result, "stdout", "") or "").strip()
    if not text:
        return frozenset()
    tokens = text.split()
    for token in tokens:
        if _CONTROLLER.fullmatch(token) is None:
            raise _error(
                "inspect-delegates",
                "invalid_controller_token",
                identity,
                pending=True,
                diagnostics=diagnostics + [f"delegates:invalid={_bounded_text(token)}"],
            )
    controllers = frozenset(tokens)
    diagnostics.append(f"delegates:observed={';'.join(sorted(controllers))}")
    return controllers


def _existing_delegated_controllers(fs: SafeRootFS, identity: SetupIdentity) -> frozenset[str]:
    try:
        raw = fs.read_bounded(str(SERVICE_DROPIN), max_bytes=MAX_TARGET_BYTES)
    except FileNotFoundError:
        return frozenset()
    except PermissionError as exc:
        raise _error(
            "inspect-existing-service-dropin",
            getattr(exc, "reason", "unreadable"),
            identity,
            pending=True,
            diagnostics=[_describe(exc)],
        ) from exc
    if raw == b"":
        return frozenset()
    parsed = parse_service_dropin(raw)
    if parsed is None:
        raise _error(
            "inspect-existing-service-dropin",
            "unexpected_content",
            identity,
            pending=True,
            diagnostics=["service-dropin:not_canonical_appio_content"],
        )
    return parsed


#: A template unit cannot be inspected without an instance. systemd 262 answers
#: ``systemctl show user@.service`` with "Unit name user@.service is neither a valid
#: invocation ID nor unit name"; other builds report the unit as not found. That is a
#: query-shape problem, not a broken host, so the observation degrades instead of
#: failing the install: the required controller set is unioned into what we write.
_UNINSTANTIABLE_UNIT_MARKERS: tuple[str, ...] = (
    "is neither a valid invocation id nor unit name",
    "not found",
    "not loaded",
)


def _is_uninstantiable_unit(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _UNINSTANTIABLE_UNIT_MARKERS)


def _run(
    deps: SetupDeps,
    argv: tuple[str, ...],
    operation: str,
    identity: SetupIdentity,
    diagnostics: list[str],
    *,
    allow_failure: bool = False,
) -> object:
    try:
        result = deps.runner.run(tuple(argv))
    except Exception as exc:  # the runner is a fixed-argv boundary; never retry
        raise _error(
            operation, "runner_error", identity, pending=True,
            diagnostics=diagnostics + [_describe(exc)],
        ) from exc
    returncode = getattr(result, "returncode", None)
    if returncode != 0 and not allow_failure:
        detail = _first_line(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
        raise _error(
            operation,
            f"exit_{returncode}",
            identity,
            pending=True,
            diagnostics=diagnostics + [f"{operation}:{detail}" if detail else f"{operation}:no_output"],
        )
    return result


# --------------------------------------------------------------------------- #
# Report shaping
# --------------------------------------------------------------------------- #


def _error(
    operation: str,
    reason: str,
    identity: SetupIdentity,
    *,
    pending: bool,
    diagnostics: Iterable[str],
) -> DelegationError:
    report = {
        "version": STATE_VERSION,
        "uid": identity.uid,
        "delegation_installed": False,
        "pending_activation": pending,
        "diagnostics": _bounded(diagnostics),
        "error": f"{operation}:{reason}",
    }
    return DelegationError(operation, reason, result=report)


def _bounded(items: Iterable[str]) -> list[str]:
    bounded: list[str] = []
    for item in items:
        text = _bounded_text(item)
        if text and text not in bounded:
            bounded.append(text)
        if len(bounded) >= MAX_DIAGNOSTICS:
            break
    return bounded


def _bounded_text(value: object) -> str:
    text = _CONTROL_CHARS.sub(" ", str(value)).strip()
    return text[:MAX_DIAGNOSTIC_CHARS]


def _first_line(value: object) -> str:
    text = _bounded_text(value)
    return text.split("\n", 1)[0][:MAX_DIAGNOSTIC_CHARS]


def _describe(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    return _bounded_text(reason if isinstance(reason, str) else type(exc).__name__)


# --------------------------------------------------------------------------- #
# Field validation
# --------------------------------------------------------------------------- #


def _require_identity(identity: object) -> None:
    if not isinstance(identity, SetupIdentity):
        raise DelegationError(
            "identity",
            "invalid_identity",
            result={
                "version": STATE_VERSION,
                "uid": None,
                "delegation_installed": False,
                "pending_activation": False,
                "diagnostics": ["identity:not_setup_identity"],
                "error": "identity:invalid_identity",
            },
        )
    try:
        _require_uid(identity.uid)
        _require_uid(identity.gid, allow_zero=True)
        _require_username(identity.username)
    except (TypeError, ValueError) as exc:
        raise DelegationError(
            "identity",
            "invalid_identity",
            result={
                "version": STATE_VERSION,
                "uid": getattr(identity, "uid", None),
                "delegation_installed": False,
                "pending_activation": False,
                "diagnostics": [_bounded_text(exc)],
                "error": "identity:invalid_identity",
            },
        ) from exc
    home = identity.home
    if not isinstance(home, Path) or not home.is_absolute():
        raise DelegationError(
            "identity",
            "invalid_identity",
            result={
                "version": STATE_VERSION,
                "uid": identity.uid,
                "delegation_installed": False,
                "pending_activation": False,
                "diagnostics": ["identity:home_not_absolute"],
                "error": "identity:invalid_identity",
            },
        )


def _require_nss_account(identity: SetupIdentity, deps: SetupDeps) -> None:
    """Re-resolve the beneficiary from the account database before ``usermod``.

    ``parse_invocation`` already binds ``identity`` to ``PKEXEC_UID``, but this
    layer is what hands the name to a privileged ``usermod``. Re-resolving means a
    hand-built or stale identity cannot install group membership for a *claimed*
    name that does not belong to the UID, which is the difference between adding
    one account to ``appio`` and adding a different account entirely.
    """

    try:
        record = deps.getpwuid(identity.uid)
    except Exception as exc:  # NSS failure is a prerequisite failure, not a retry
        raise _error(
            "identity", "account_lookup_failed", identity, pending=False,
            diagnostics=[_describe(exc)],
        ) from exc
    if record is None:
        raise _error(
            "identity", "unknown_uid", identity, pending=False,
            diagnostics=[f"account:uid={identity.uid}:missing"],
        )
    record_uid = getattr(record, "pw_uid", None)
    record_name = getattr(record, "pw_name", None)
    if record_uid != identity.uid or record_name != identity.username:
        raise _error(
            "identity", "account_mismatch", identity, pending=False,
            diagnostics=[f"account:uid={identity.uid}:name_mismatch"],
        )


def _require_uid(uid: object, *, allow_zero: bool = False) -> None:
    if isinstance(uid, bool) or not isinstance(uid, int):
        raise TypeError("uid must be an int")
    low = 0 if allow_zero else 1
    if uid < low or uid > _UID_MAX:
        raise ValueError(f"uid must be in [{low}, {_UID_MAX}]")


def _require_username(username: object) -> None:
    if not isinstance(username, str):
        raise TypeError("username must be a string")
    if _SAFE_USERNAME.fullmatch(username) is None:
        raise ValueError("username is not a safe local account name")


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")
