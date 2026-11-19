"""Fixed six-verb appio-setup orchestration; no path or command selectors.

The shim authenticates via polkit before importing this module. Main validates
syntax before constructing adapters, rechecks the trusted runtime, and only then
enters the common helper lock. A root peer speaking protocol 1, rather than a
successful systemctl exit, is the evidence that the daemon is answering.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import uuid
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
    SAMPLE_PERIOD_COMPLETE, SAMPLE_PERIOD_DROPIN, SAMPLE_PERIOD_PENDING,
    SERVICE_DROPIN, USER_MANAGER_DROPIN, PeriodJournalRecord, SafeRootFS,
    decode_period_journal, parse_manager_dropin, parse_sample_period_dropin,
    parse_service_dropin, publish_period_complete, publish_period_pending,
    sample_period_payload,
)

ENABLE_ARGV = ("/usr/bin/systemctl", "enable", "--now", "appio-iod.service")
DISABLE_ARGV = ("/usr/bin/systemctl", "disable", "--now", "appio-iod.service")
DAEMON_RELOAD_ARGV = ("/usr/bin/systemctl", "daemon-reload")
TRY_RESTART_ARGV = ("/usr/bin/systemctl", "try-restart", "appio-iod.service")
SHOW_ARGV = ("/usr/bin/systemctl", "show", "appio-iod.service",
    "--property=UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus", "--no-pager")
SAMPLE_SHOW_ARGV = ("/usr/bin/systemctl", "show", "appio-iod.service",
    "--property=Environment,EnvironmentFiles,UnsetEnvironment,DropInPaths,UnitFileState,ActiveState", "--no-pager")
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
                export_cutoff=None, error=None, sample_period=None,
                sample_period_state=None)


def _pending(identity, fs, *, create_parents=True):
    try:
        return load_setup_state(fs, create_parents=create_parents).pending_by_uid.get(identity.uid, True)
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


def _period_unit_state(deps):
    unit = _unit_state(deps)
    active_states = {
        "active",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "maintenance",
        "reloading",
        "refreshing",
    }
    if unit["ActiveState"] not in active_states or not re.fullmatch(
        r"[a-z][a-z0-9-]*", unit["Result"], flags=re.ASCII
    ):
        raise ServiceError("inspect-service:invalid_response")
    return unit


def _observe(result, *, deps, fs, identity, strict=False, create_parents=True):
    result["pending_activation"] = _pending(identity, fs, create_parents=create_parents)
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


def _parse_sample_show(stdout: object) -> dict[str, object]:
    if not isinstance(stdout, str) or len(stdout.encode("utf-8", "surrogatepass")) > 64 * 1024:
        raise ValueError("bounded output")
    rows: dict[str, object] = {"EnvironmentFiles": []}
    single = {"Environment", "UnsetEnvironment", "DropInPaths", "UnitFileState", "ActiveState"}
    allowed = single | {"EnvironmentFiles"}
    seen: set[str] = set()
    for line in stdout.splitlines():
        if not line:
            raise ValueError("blank line")
        key, separator, value = line.partition("=")
        if not separator or key not in allowed:
            raise ValueError("unknown property")
        if key in single and key in seen:
            raise ValueError("duplicate property")
        seen.add(key)
        if key == "EnvironmentFiles":
            rows["EnvironmentFiles"].append(value)
        else:
            rows[key] = value
    required = {"Environment", "DropInPaths", "UnitFileState", "ActiveState"}
    if seen & required != required:
        raise ValueError("missing property")
    if "UnsetEnvironment" not in seen:
        rows["UnsetEnvironment"] = ""
    return rows


_PERIOD_ENV = re.compile(r"APPIO_SAMPLE_SECONDS=(?:0|[1-9][0-9]*)\Z", re.ASCII)


def _configured_period(environment: str) -> str | None:
    if not isinstance(environment, str):
        return None
    tokens = [token for token in environment.split() if token.startswith("APPIO_SAMPLE_SECONDS=")]
    if len(tokens) != 1 or _PERIOD_ENV.fullmatch(tokens[0]) is None:
        return None
    return tokens[0].split("=", 1)[1]


def _sample_seconds(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("sample_ns")
    if type(value) is not int or value <= 0 or value % 1_000_000_000:
        return None
    seconds = value // 1_000_000_000
    return seconds if 1 <= seconds <= 30 else None


def _period_view(deps) -> dict[str, object]:
    command = deps.runner.run(SAMPLE_SHOW_ARGV, timeout_seconds=STATUS_TIMEOUT_SECONDS)
    if command.returncode != 0:
        raise _command_error("sample_period_show", command)
    try:
        return _parse_sample_show(command.stdout)
    except (TypeError, UnicodeError, ValueError) as error:
        raise ServiceError("sample_period_show_invalid", stderr="sample_period_show_invalid") from error


def _read_optional(fs: SafeRootFS, path: Path, *, max_bytes: int) -> bytes | None:
    try:
        return fs.read_bounded(str(path), max_bytes=max_bytes, create_parents=False)
    except FileNotFoundError:
        return None


def _read_period_record(fs: SafeRootFS, path: Path) -> tuple[PeriodJournalRecord | None, bool]:
    raw = _read_optional(fs, path, max_bytes=1024)
    if raw is None:
        return None, False
    try:
        return decode_period_journal(raw), False
    except ValueError:
        return None, True


def _period_record_matches(record: PeriodJournalRecord | None, digest: str, seconds: int) -> bool:
    return record is not None and record.target == str(SAMPLE_PERIOD_DROPIN) \
        and record.sample_seconds == seconds and record.target_sha256 == digest


def _period_operation_id() -> str:
    return uuid.uuid4().hex


def _period_result_unit(result: dict[str, object], unit: dict[str, str], *, answering: bool | None = None) -> None:
    enabled = unit.get("UnitFileState")
    result["enabled"] = (
        True if enabled in {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias"}
        else False if enabled in {"disabled", "masked", "masked-runtime", "static", "indirect"}
        else None
    )
    if answering is not None:
        result["answering"] = answering
    elif unit.get("ActiveState") != "active":
        result["answering"] = False
    _shape(result)


def _period_diagnostic_active(view: dict[str, object], observed: object) -> str:
    value = observed if isinstance(observed, (int, str)) else "unknown"
    paths = view.get("DropInPaths") or "(none)"
    return f"sample_period_not_in_effect observed={value} DropInPaths={paths}"


def _period_diagnostic_inactive(view: dict[str, object], observed: object) -> str:
    value = observed if isinstance(observed, (int, str)) else "unknown"
    files = view.get("EnvironmentFiles") or []
    file_text = " | ".join(str(item) for item in files) if files else "(none)"
    unset = view.get("UnsetEnvironment") or "(none)"
    paths = view.get("DropInPaths") or "(none)"
    return (f"sample_period_unverifiable observed={value} EnvironmentFiles={file_text} "
            f"UnsetEnvironment={unset} DropInPaths={paths}")


def _period_active_observation(view: dict[str, object], observed: object, seconds: int) -> object:
    configured = _configured_period(view.get("Environment", ""))
    return configured if configured != str(seconds) else observed


def _period_final_lifecycle(deps):
    """Read lifecycle twice, with status between the reads when active."""
    observation_failure = None
    try:
        preceding = _period_unit_state(deps)
    except BoundaryError as error:
        preceding = None
        observation_failure = ServiceError(error.reason, stderr=getattr(error, "stderr", ""))

    status = None
    if preceding is not None and preceding.get("ActiveState") == "active":
        status = probe_daemon_status_payload(deps=deps, timeout_seconds=STATUS_TIMEOUT_SECONDS)

    try:
        final = _period_unit_state(deps)
    except BoundaryError as error:
        final = None
        if observation_failure is None:
            observation_failure = ServiceError(error.reason, stderr=getattr(error, "stderr", ""))
    return preceding, final, status, observation_failure


def _period_failure(
    reason: str,
    *,
    stderr: str = "",
    result: dict[str, object],
    exit_code: int = 1,
) -> ServiceError:
    return ServiceError(reason, exit_code=exit_code, stderr=stderr, result=result)


def _set_sample_period(identity, deps, fs, result):
    seconds = result["sample_period"]
    if type(seconds) is not int:
        raise _period_failure("invalid_verb_or_argument_count", result=result)
    canonical = sample_period_payload(seconds)
    digest = hashlib.sha256(canonical).hexdigest()

    before = _period_unit_state(deps)
    active_before = before.get("ActiveState") == "active"
    _period_result_unit(result, before)

    existing = _read_optional(fs, SAMPLE_PERIOD_DROPIN, max_bytes=64 * 1024)
    if existing and parse_sample_period_dropin(existing) is None:
        raise _period_failure(
            "foreign_drop_in",
            stderr="foreign_drop_in path=/etc/systemd/system/appio-iod.service.d/20-sample-period.conf",
            result=result,
            exit_code=2,
        )
    existing_for_compare = existing
    needs_write = existing_for_compare != canonical

    try:
        pending, pending_malformed = _read_period_record(fs, SAMPLE_PERIOD_PENDING)
        complete, complete_malformed = _read_period_record(fs, SAMPLE_PERIOD_COMPLETE)
    except (OSError, PermissionError) as error:
        raise _period_failure("sample_period_state_unsafe", result=result) from error
    reconciliation = pending_malformed or complete_malformed
    completed_matches = _period_record_matches(complete, digest, seconds)
    pending_matches = _period_record_matches(pending, digest, seconds)
    recovery_pair = (
        pending is not None and complete is not None
        and pending == complete and _period_record_matches(pending, digest, seconds)
    )

    try:
        initial_view = _period_view(deps)
    except ServiceError:
        _, final_unit, final_status, final_failure = _period_final_lifecycle(deps)
        if final_unit is not None:
            answering = bool(
                final_unit.get("ActiveState") == "active"
                and final_status is not None
                and final_status.get("answering") is True
            )
            _period_result_unit(result, final_unit, answering=answering)
            if active_before and final_unit.get("ActiveState") != "active":
                result["sample_period_state"] = "service_not_running"
                raise _period_failure("service_not_running", result=result)
        if final_failure is not None:
            raise final_failure
        raise
    configured = _configured_period(initial_view["Environment"])
    initial_status = None
    running = None
    if active_before:
        initial_status = probe_daemon_status_payload(deps=deps, timeout_seconds=STATUS_TIMEOUT_SECONDS)
        running = _sample_seconds(initial_status)
    configured_matches = configured == str(seconds)
    running_matches = active_before and running == seconds
    incomplete_pending = pending is not None
    manager_required = (
        needs_write or not configured_matches
        or (active_before and not running_matches)
        or not completed_matches or incomplete_pending or reconciliation
    )

    # A matching pair is a durable recovery candidate only after all fresh
    # observations above match the requested transaction.
    recovered = recovery_pair and not needs_write and configured_matches \
        and (not active_before or running_matches) and not reconciliation
    if recovered:
        manager_required = False

    operation_failure: ServiceError | None = None
    try:
        fs.prepare_period_directories()
        if needs_write is False and manager_required:
            fs.sync_period_parent(SAMPLE_PERIOD_DROPIN)
    except (OSError, PermissionError):
        operation_failure = _period_failure("sample_period_state_write_failed", result=result)

    operation_id = None
    if manager_required and operation_failure is None:
        if pending_matches and not recovery_pair and not reconciliation:
            operation_id = pending.operation_id
        else:
            operation_id = _period_operation_id()
        record = PeriodJournalRecord(operation_id, str(SAMPLE_PERIOD_DROPIN), seconds, digest)
        try:
            publish_period_pending(fs, record)
        except (OSError, PermissionError, ValueError):
            operation_failure = _period_failure("sample_period_state_write_failed", result=result)
        if operation_failure is None:
            incomplete_pending = True
            if needs_write:
                try:
                    fs.atomic_write(SAMPLE_PERIOD_DROPIN, canonical, mode=0o644)
                except (OSError, PermissionError, ValueError):
                    operation_failure = _period_failure("sample_period_write_failed", result=result)
            else:
                try:
                    fs.sync_period_parent(SAMPLE_PERIOD_DROPIN)
                except (OSError, PermissionError):
                    operation_failure = _period_failure("sample_period_state_write_failed", result=result)

    if manager_required and not recovered and operation_failure is None:
        try:
            _run(deps, DAEMON_RELOAD_ARGV, "daemon_reload")
        except ServiceError as error:
            operation_failure = _period_failure("daemon_reload_failed", stderr=error.stderr, result=result)
        if operation_failure is None and active_before:
            try:
                _run(deps, TRY_RESTART_ARGV, "service_restart")
            except ServiceError as error:
                operation_failure = _period_failure("service_restart_failed", stderr=error.stderr, result=result)

    verification_view = None
    verification_failure: ServiceError | None = None
    if operation_failure is None or operation_failure.reason == "service_restart_failed":
        try:
            verification_view = _period_view(deps)
            configured_after = _configured_period(verification_view["Environment"])
            active_after_view = verification_view.get("ActiveState") == "active"
            if active_after_view:
                status_after_view = probe_daemon_status_payload(
                    deps=deps, timeout_seconds=STATUS_TIMEOUT_SECONDS
                )
                observed_after = _sample_seconds(status_after_view)
                if configured_after != str(seconds) or observed_after != seconds:
                    verification_failure = _period_failure(
                        "sample_period_overridden",
                        stderr=_period_diagnostic_active(
                            verification_view,
                            _period_active_observation(verification_view, observed_after, seconds),
                        ),
                        result=result,
                    )
            elif (
                configured_after != str(seconds)
                or verification_view.get("EnvironmentFiles")
                or _sample_unset(verification_view.get("UnsetEnvironment", ""))
            ):
                verification_failure = _period_failure(
                    "sample_period_unverifiable",
                    stderr=_period_diagnostic_inactive(verification_view, configured_after),
                    result=result,
                )
        except ServiceError as error:
            verification_failure = error

    after_verification, final_unit, final_status, final_failure = _period_final_lifecycle(deps)
    final_observed = _sample_seconds(final_status)
    if final_unit is not None:
        answering = bool(
            final_unit.get("ActiveState") == "active"
            and final_status is not None
            and final_status.get("answering") is True
        )
        _period_result_unit(result, final_unit, answering=answering)
    active_after = final_unit is not None and final_unit.get("ActiveState") == "active"

    if final_unit is not None and active_before and not active_after:
        result["sample_period_state"] = "service_not_running"
        raise _period_failure("service_not_running", result=result)
    if operation_failure is not None and operation_failure.reason == "service_restart_failed" and active_after:
        raise operation_failure
    if final_failure is not None:
        raise final_failure
    if operation_failure is not None:
        raise operation_failure
    if verification_failure is not None:
        raise verification_failure
    if final_unit is None or "ActiveState" not in final_unit or "Result" not in final_unit:
        raise _period_failure("sample_period_final_observation_failed", result=result)
    if active_after and final_unit.get("Result") != "success":
        raise _period_failure("service_lifecycle_failed", result=result)
    preceding_active = after_verification is not None and after_verification.get("ActiveState") == "active"
    if (active_after or preceding_active) and (not preceding_active or final_observed != seconds):
        diagnostic = _period_diagnostic_active(
            verification_view or initial_view,
            _period_active_observation(verification_view or initial_view, final_observed, seconds),
        )
        raise _period_failure("sample_period_overridden", stderr=diagnostic, result=result)

    # For an inactive unit, a historical Result does not block a verified apply.
    if operation_id is None:
        if recovered:
            operation_id = pending.operation_id
        elif completed_matches and not pending and not manager_required:
            result["sample_period_state"] = "unchanged"
            return
        else:
            operation_id = _period_operation_id()
            record = PeriodJournalRecord(operation_id, str(SAMPLE_PERIOD_DROPIN), seconds, digest)
            try:
                publish_period_pending(fs, record)
            except (OSError, PermissionError, ValueError) as error:
                raise _period_failure("sample_period_state_write_failed", result=result) from error
    record = PeriodJournalRecord(operation_id, str(SAMPLE_PERIOD_DROPIN), seconds, digest)
    if not recovered:
        try:
            publish_period_complete(fs, record)
        except (OSError, PermissionError, ValueError) as error:
            raise _period_failure("sample_period_state_commit_failed", result=result) from error
    try:
        fs.remove_period_pending()
    except FileNotFoundError:
        pass
    except (OSError, PermissionError):
        # Completion is already durable. Keep the verified success, and let the
        # next invocation reconcile visible pending evidence.
        result["sample_period_state"] = "repair" if recovered or not needs_write else (
            "applied_not_running" if not active_after else "applied"
        )
        return

    if recovered:
        result["sample_period_state"] = "repair"
    elif needs_write:
        result["sample_period_state"] = "applied_not_running" if not active_after else "applied"
    else:
        result["sample_period_state"] = "repair"


def _sample_unset(value: object) -> bool:
    return isinstance(value, str) and any(
        token.partition("=")[0] == "APPIO_SAMPLE_SECONDS" for token in value.split()
    )


# One literal route for every accepted verb, with no default or shell dispatcher.
VERB_HANDLERS = MappingProxyType({
    "install-delegation": _installed,
    "enable": _enabled,
    "disable": _disabled,
    "status": _status,
    "restart-user-manager": _restart,
    "export-history": _exported,
    "set-sample-period": _set_sample_period,
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
    if invocation.verb == "set-sample-period":
        if type(invocation.period_seconds) is not int:
            raise InvocationError(2, "invalid_verb_or_argument_count")
        verified_argv = ("/usr/bin/appio-setup", invocation.verb, str(invocation.period_seconds))
    else:
        if invocation.period_seconds is not None:
            raise InvocationError(2, "invalid_verb_or_argument_count")
        verified_argv = ("/usr/bin/appio-setup", invocation.verb)
    verified = parse_invocation(
        verified_argv,
        environ={"PKEXEC_UID": str(invocation.identity.uid)}, euid=deps.effective_uid(), getpwuid=deps.getpwuid)
    if verified.identity != invocation.identity:
        raise InvocationError(1, "stale_caller_identity")
    result = _empty(invocation.verb, invocation.identity.uid)
    if invocation.verb == "set-sample-period":
        result["sample_period"] = invocation.period_seconds
    reason = _environment_reason(deps)
    if reason:
        result["error"] = reason
        raise ServiceError(reason, result=result)
    observer = _ObservedRunner(deps.runner)
    deps = replace(deps, runner=observer)
    fs = SafeRootFS(deps)
    try:
        with fs.locked():
            try:
                VERB_HANDLERS[invocation.verb](invocation.identity, deps, fs, result)
            except Exception as error:
                # Probe while still holding the common lock: a different admin's
                # operation cannot leak its cutoff or enabled state into this result.
                try:
                    _observe(
                        result,
                        deps=deps,
                        fs=fs,
                        identity=invocation.identity,
                        create_parents=invocation.verb != "set-sample-period",
                    )
                except Exception:
                    pass
                reason = getattr(error, "reason", None) or str(error) or type(error).__name__
                result["error"] = reason[:1024]
                if isinstance(error, ServiceError):
                    error.result = result
                    raise
                raise ServiceError(result["error"], result=result) from error
    except Exception as error:
        if isinstance(error, ServiceError):
            error.result = result
            raise
        reason = getattr(error, "reason", None) or str(error) or type(error).__name__
        raise ServiceError(reason[:1024], result=result) from error
    return result


def _valid_syntax(argv):
    # parse_invocation checks the entire syntax before it looks at euid, NSS or
    # files. A deliberately nonroot euid exercises that pure prefix only.
    try:
        parse_invocation(argv, environ={}, euid=1, getpwuid=lambda _uid: None)
    except InvocationError as error:
        if error.exit_code == 2:
            return error.reason
        return None
    return None


def main(argv: list[str] | None = None, *, deps: SetupDeps | None = None) -> int:
    arguments = sys.argv if argv is None else argv
    syntax_reason = _valid_syntax(arguments)
    if syntax_reason is not None and syntax_reason != "sample_period_out_of_range":
        print(f"appio-setup: {syntax_reason}", file=sys.stderr)
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


def probe_daemon_status_payload(*, deps: SetupDeps, timeout_seconds: float = STATUS_TIMEOUT_SECONDS) -> dict[str, object] | None:
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
                            return None
                        return value
        except (PermissionError, ProtocolError, ValueError):
            return None
        except BoundaryError:
            # The runtime directory is created by the unit's RuntimeDirectory= the
            # first time the service starts, so on a fresh install it legitimately
            # does not exist yet. That is "not answering *yet*", not a failed verb and
            # not a reason to stop observing: `enable` starts the unit and then asks,
            # so the absence has to consume the budget instead of ending the poll. It
            # previously returned instantly, which made enable exit 1 with
            # enable:daemon_not_answering while the daemon answered a second later.
            pass
        except OSError:
            pass  # Startup may not have published the socket yet.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
    return None


def probe_daemon_status(*, deps: SetupDeps, timeout_seconds: float = STATUS_TIMEOUT_SECONDS) -> bool:
    payload = probe_daemon_status_payload(deps=deps, timeout_seconds=timeout_seconds)
    return bool(payload is not None and payload.get("answering") is True)
