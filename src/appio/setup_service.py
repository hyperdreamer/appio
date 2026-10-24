"""Fixed six-verb appio-setup orchestration; no path or command selectors.

The shim authenticates via polkit before importing this module. Main validates
syntax before constructing adapters, rechecks the trusted runtime, and only then
enters the common helper lock. A root peer speaking protocol 1, rather than a
successful systemctl exit, is the evidence that the daemon is answering.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time
from types import MappingProxyType

from .activation import ActivationError, choose_activation, probe_accounting
from .history_export import ExportError, ExportResult, export_history, prepare_export
from .ipc_models import validate_server_message
from .ipc_protocol import FrameDecoder, MAX_OUTBOUND, ProtocolError, encode_frame
from .ipc_security import read_peer, require_root_server
from .setup_boundary import (
    BoundaryError, Invocation, InvocationError, SetupDeps, VERBS, open_trusted_directory,
    parse_invocation, production_deps, production_runtime_paths, validate_trusted_runtime,
)
from .setup_delegation import REQUIRED_DELEGATE_CONTROLLERS, install_delegation, load_setup_state
from .setup_files import (
    SERVICE_DROPIN, USER_MANAGER_DROPIN, SafeRootFS, parse_manager_dropin, parse_service_dropin,
)

ENABLE_ARGV = ("/usr/bin/systemctl", "enable", "--now", "appio-iod.service")
DISABLE_ARGV = ("/usr/bin/systemctl", "disable", "--now", "appio-iod.service")
SHOW_ARGV = ("/usr/bin/systemctl", "show", "appio-iod.service",
    "--property=UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus", "--no-pager")
STATUS_TIMEOUT_SECONDS = 5.0
COMMAND_TIMEOUT_SECONDS = 10.0
MAX_RESULT_BYTES = 16 * 1024


class ServiceError(BoundaryError):
    def __init__(self, reason, *, exit_code=1, stderr="", result=None):
        super().__init__(exit_code, reason)
        self.stderr = stderr
        self.result = result


@dataclass
class _ObservedRunner:
    """Preserve the original failed command result across T-008/T-009 adapters."""
    inner: object
    failed: object = None

    def run(self, argv, **kwargs):
        result = self.inner.run(argv, **kwargs)
        if result.returncode != 0:
            self.failed = result
        return result


def _empty(verb=None, uid=None):
    return dict(version=1, verb=verb, uid=uid, enabled=None, answering=None,
                pending_activation=None, state="unknown", export_path=None,
                export_cutoff=None, error=None)


def _pending(identity, fs):
    try:
        return load_setup_state(fs).pending_by_uid.get(identity.uid, True)
    except (OSError, ValueError):
        return None


def _shape(result):
    if result["enabled"] is False:
        result["state"] = "off"
    elif result["enabled"] is True:
        if result["answering"] is False:
            result["state"] = "enabled, not answering"
        elif result["answering"] is True:
            result["state"] = "on, answering" if result["pending_activation"] is False else "on, needs activation"
    return result


def _unit_state(deps):
    command = deps.runner.run(SHOW_ARGV, timeout_seconds=STATUS_TIMEOUT_SECONDS)
    if command.returncode != 0:
        raise _command_error("inspect-service", command)
    rows = {}
    for line in command.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in rows:
            raise ServiceError("inspect-service:invalid_response")
        rows[key] = value
    names = {"UnitFileState", "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus"}
    if set(rows) != names:
        raise ServiceError("inspect-service:incomplete_response")
    return rows


def _observe(result, *, deps, fs, identity, strict=False):
    result["pending_activation"] = _pending(identity, fs)
    unit = None
    try:
        unit = _unit_state(deps)
        enabled = unit["UnitFileState"]
        result["enabled"] = True if enabled in {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias"} else False if enabled in {"disabled", "masked", "masked-runtime", "static", "indirect"} else None
    except Exception:
        if strict:
            raise
    result["answering"] = probe_daemon_status(deps=deps, timeout_seconds=STATUS_TIMEOUT_SECONDS)
    _shape(result)
    return unit


def _command_error(operation, command):
    code = command.returncode
    return ServiceError(f"{operation}:exit_{code}", exit_code=code if code > 0 else 128 - code,
                        stderr=command.stderr)


def _run(deps, argv, operation):
    command = deps.runner.run(argv, timeout_seconds=COMMAND_TIMEOUT_SECONDS)
    if command.returncode != 0:
        raise _command_error(operation, command)


def _require_delegation(fs):
    try:
        service = parse_service_dropin(fs.read_bounded(str(SERVICE_DROPIN), max_bytes=65536))
        manager = parse_manager_dropin(fs.read_bounded(str(USER_MANAGER_DROPIN), max_bytes=65536))
        if service is None or not REQUIRED_DELEGATE_CONTROLLERS <= service or not manager:
            raise ValueError("incomplete delegation")
    except (OSError, ValueError) as error:
        raise ServiceError("enable:delegation_not_installed") from error


def _enabled(identity, deps, fs, result):
    _require_delegation(fs)
    _run(deps, ENABLE_ARGV, "enable")
    _observe(result, deps=deps, fs=fs, identity=identity, strict=True)
    if result["enabled"] is not True:
        raise ServiceError("enable:unit_not_enabled")
    if result["answering"] is not True:
        raise ServiceError("enable:daemon_not_answering")


def _set_export(result, exported: ExportResult):
    result["export_path"] = str(exported.path)
    result["export_cutoff"] = dict(boot_id=exported.boot_id, iod_epoch=exported.iod_epoch,
        seq=exported.seq, cutoff_mono_ns=exported.cutoff_mono_ns)


def _disabled(identity, deps, fs, result):
    # Hold the same preflighted destination inode across the stop and final commit.
    # Neither drop-in nor account/group state is touched by this verb.
    with prepare_export(identity, deps=deps) as target:
        _run(deps, DISABLE_ARGV, "disable")
        unit = _observe(result, deps=deps, fs=fs, identity=identity, strict=True)
        clean = (result["enabled"] is False and result["answering"] is False
            and unit["ActiveState"] == "inactive" and unit["SubState"] == "dead"
            and unit["Result"] == "success" and unit["ExecMainCode"] in {"0", "1"}
            and unit["ExecMainStatus"] == "0")
        if not clean:
            raise ServiceError("disable:shutdown_not_clean_final_coverage_uncertain")
        _set_export(result, target.export())


def _status(identity, deps, fs, result):
    activation = probe_accounting(identity, deps=deps, fs=fs)
    _observe(result, deps=deps, fs=fs, identity=identity, strict=True)
    result["pending_activation"] = activation.pending_activation
    _shape(result)
    if result["enabled"] is True and activation.state in {"delegation present, accounting inactive", "not delegated"}:
        result["state"] = activation.state


def _installed(identity, deps, fs, result):
    installed = install_delegation(identity, deps=deps, fs=fs)
    if installed.get("error"):
        if deps.runner.failed is not None:
            raise _command_error(installed["error"], deps.runner.failed)
        raise ServiceError(installed["error"])
    _observe(result, deps=deps, fs=fs, identity=identity)
    result["pending_activation"] = installed["pending_activation"]
    _shape(result)


def _restart(identity, deps, fs, result):
    try:
        activation = choose_activation("restart", identity, deps=deps, fs=fs)
    except ActivationError as error:
        if deps.runner.failed is not None:
            raise _command_error("restart-user-manager", deps.runner.failed) from error
        raise
    _observe(result, deps=deps, fs=fs, identity=identity)
    result["pending_activation"] = activation.pending_activation
    _shape(result)


def _exported(identity, deps, fs, result):
    _set_export(result, export_history(identity, deps=deps))
    _observe(result, deps=deps, fs=fs, identity=identity)


# One literal route for every accepted verb, with no default or shell dispatcher.
VERB_HANDLERS = MappingProxyType({
    "install-delegation": _installed,
    "enable": _enabled,
    "disable": _disabled,
    "status": _status,
    "restart-user-manager": _restart,
    "export-history": _exported,
})
assert frozenset(VERB_HANDLERS) == VERBS


def _environment_reason(deps):
    root = Path(deps.root)
    if not sys.platform.startswith("linux"):
        return "unsupported:linux_required"
    for marker in ("run/systemd/container", ".dockerenv", "run/.containerenv"):
        if (root / marker).exists():
            return "unsupported:container_host"
    if not (root / "run/systemd/system").is_dir():
        return "unsupported:systemd_system_manager_unavailable"
    if not (root / "sys/fs/cgroup/cgroup.controllers").is_file():
        return "unsupported:cgroup_v2_unavailable"
    return None


def dispatch(invocation: Invocation, *, deps: SetupDeps) -> dict:
    # Invalid internal calls must obey the same zero-effect syntax contract.
    if not isinstance(invocation, Invocation) or invocation.verb not in VERB_HANDLERS:
        raise InvocationError(2, "invalid_verb_or_argument_count")
    verified = parse_invocation(("/usr/bin/appio-setup", invocation.verb),
        environ={"PKEXEC_UID": str(invocation.identity.uid)}, euid=deps.effective_uid(), getpwuid=deps.getpwuid)
    if verified.identity != invocation.identity:
        raise InvocationError(1, "stale_caller_identity")
    result = _empty(invocation.verb, invocation.identity.uid)
    reason = _environment_reason(deps)
    if reason:
        result["error"] = reason
        raise ServiceError(reason, result=result)
    observer = _ObservedRunner(deps.runner)
    deps = replace(deps, runner=observer)
    fs = SafeRootFS(deps)
    with fs.locked():
        try:
            VERB_HANDLERS[invocation.verb](invocation.identity, deps, fs, result)
        except Exception as error:
            # Probe while still holding the common lock: a different admin's
            # operation cannot leak its cutoff or enabled state into this result.
            try:
                _observe(result, deps=deps, fs=fs, identity=invocation.identity)
            except Exception:
                pass
            reason = getattr(error, "reason", None) or str(error) or type(error).__name__
            result["error"] = reason[:1024]
            if isinstance(error, ServiceError):
                error.result = result
                raise
            raise ServiceError(result["error"], result=result) from error
    return result


def _valid_syntax(argv):
    # parse_invocation checks the entire syntax before it looks at euid, NSS or
    # files. A deliberately nonroot euid exercises that pure prefix only.
    try:
        parse_invocation(argv, environ={}, euid=1, getpwuid=lambda _uid: None)
    except InvocationError as error:
        return error.exit_code != 2
    return False


def main(argv: list[str] | None = None, *, deps: SetupDeps | None = None) -> int:
    arguments = sys.argv if argv is None else argv
    if not _valid_syntax(arguments):
        print("appio-setup: invalid_verb_or_argument_count", file=sys.stderr)
        return 2
    invocation = None
    try:
        deps = production_deps() if deps is None else deps
        invocation = parse_invocation(arguments, environ=os.environ, euid=deps.effective_uid(), getpwuid=deps.getpwuid)
        validate_trusted_runtime(production_runtime_paths(), metadata=deps.metadata, caller_uid=invocation.identity.uid)
        result, code = dispatch(invocation, deps=deps), 0
    except Exception as error:
        code = getattr(error, "exit_code", 1)
        result = getattr(error, "result", None) or _empty(arguments[1], None if invocation is None else invocation.identity.uid)
        result["error"] = str(getattr(error, "reason", None) or error or type(error).__name__)[:1024]
        diagnostic = getattr(error, "stderr", "") or result["error"]
        print(diagnostic[:65536], file=sys.stderr, end="" if diagnostic.endswith("\n") else "\n")
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        result = _empty(arguments[1], None if invocation is None else invocation.identity.uid)
        result["error"] = "result:too_large"
        encoded, code = json.dumps(result, separators=(",", ":")), 1
    print(encoded)
    return code


def _verify_socket_paths(deps):
    directory = Path(deps.root) / "run/appio"
    parent = open_trusted_directory(directory, base=Path(deps.root), metadata=deps.metadata)
    descriptor = None
    try:
        info = deps.metadata(parent)
        if stat.S_IMODE(info.st_mode) != 0o750:
            raise PermissionError("untrusted_socket_directory_mode")
        descriptor = os.open("iod.sock", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        real, endpoint = os.fstat(descriptor), deps.metadata(descriptor)
        if (not stat.S_ISSOCK(real.st_mode) or endpoint.st_uid != 0
            or stat.S_IMODE(endpoint.st_mode) != 0o660 or endpoint.st_gid != info.st_gid):
            raise PermissionError("untrusted_socket")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def probe_daemon_status(*, deps: SetupDeps, timeout_seconds: float = STATUS_TIMEOUT_SECONDS) -> bool:
    """Bound connect, write and every fragmented read by one absolute deadline.

    This is a helper/root observation only. A GUI still has to verify its own
    authenticated connection after NSS membership and session activation change.
    """
    deadline = time.monotonic() + min(STATUS_TIMEOUT_SECONDS, max(0.0, timeout_seconds))
    request_id = "setup-status"
    while time.monotonic() < deadline:
        try:
            _verify_socket_paths(deps)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(0.001, deadline - time.monotonic()))
                client.connect(str(Path(deps.root) / "run/appio/iod.sock"))
                require_root_server(read_peer(client))
                client.settimeout(max(0.001, deadline - time.monotonic()))
                client.sendall(encode_frame({"protocol": 1, "type": "status", "request_id": request_id}))
                decoder = FrameDecoder(max_bytes=MAX_OUTBOUND)
                while time.monotonic() < deadline:
                    client.settimeout(max(0.001, deadline - time.monotonic()))
                    data = client.recv(65536)
                    if not data:
                        break
                    messages = decoder.feed(data)
                    for message in messages:
                        value = validate_server_message(message)
                        if value["type"] != "status" or value.get("request_id") != request_id:
                            return False
                        return value["answering"]
        except (PermissionError, ProtocolError, ValueError):
            return False
        except BoundaryError:
            # The runtime directory is created by the unit's RuntimeDirectory= the
            # first time the service starts, so on a fresh install it legitimately
            # does not exist. That is "not answering", not a failed verb: reporting
            # it as an error made install-delegation look broken after it had already
            # written both drop-ins and appended the group.
            return False
        except OSError:
            pass  # Startup may not have published the socket yet.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
    return False
