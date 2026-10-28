"""Prove that cgroup-v2 device accounting works before claiming activation.

The design's activation flow (section 3.3, blocker B13) exists because a
delegated ``io`` controller is **not** evidence that device bytes are accounted:
the drop-ins only take effect for a user manager started after them, and this
host runs ``Linger=yes``, so the user manager survives the end of every session.
Telling the user to end and start a session would therefore change nothing, and
this module never says it.

What it does instead:

* ``probe_accounting`` starts one transient *user* unit named
  ``appio-probe-<numeric nonce>`` as the validated caller, runs a fixed
  ``dd if=/dev/zero of=<fixed probe file> bs=1M count=16 oflag=direct`` argument
  vector inside it, and reads that unit's **own** ``io.stat`` for the block
  device that backs the probe file. Only a write-byte delta within 5% of the
  expected 16 MiB, measured in integer arithmetic, clears ``pending_activation``.
  A delegated controller with no accounted bytes reports exactly
  ``delegation present, accounting inactive``; no controller reports
  ``not delegated``; a probe that cannot run at all (no user manager, no
  ``systemd-run``, no user bus) reports ``unknown`` and never success.
* ``choose_activation`` records the user's explicit choice. ``Activate after
  reboot`` only persists ``pending_activation`` and never reboots.
  ``Restart my user manager now`` persists ``pending_activation`` **before**
  running the single fixed ``/usr/bin/systemctl restart user@<uid>.service``
  vector, because that command ends the running session. The success of that
  command is not evidence of accounting either, so ``pending_activation`` stays
  set until a new probe passes.

Deviation from design section 3.3, approved as plan decision D2: the probe writes
to a fixed, verified block-backed file under ``/var/lib/appio/probes/<uid>/``
rather than to tmpfs. A tmpfs or otherwise unprovable target cannot produce
block-device accounting, so it yields ``delegation present, accounting inactive``
instead of a false pass. The probe root and file shape are owned by
:mod:`appio.setup_files`; this module only names the one basename beneath them.

Everything ambient is injected: the filesystem root, effective UID, metadata,
ownership callback, clock (which also seeds the unit nonce), and the command
runner all come from :class:`~appio.setup_boundary.SetupDeps`. The cgroup tree,
mount table, user bus and executables are read beneath that injected root, so an
ordinary test needs no root, no user manager, and no real ``systemd-run``.
"""

from __future__ import annotations

import dataclasses
import os
import re
import stat
import time
from pathlib import Path
from typing import Literal

from .cgroups import parse_io_stat
from .setup_boundary import SetupDeps, SetupIdentity
from .setup_delegation import SetupStateError, load_setup_state, set_pending
from .setup_files import PROBE_ROOT, SafeRootFS, SafeRootFSError

__all__ = [
    "ACTIVATION_CHOICES",
    "ACTIVATION_STATES",
    "ActivationError",
    "ActivationResult",
    "CHOICE_REBOOT_LABEL",
    "CHOICE_RESTART_LABEL",
    "EXPECTED_PROBE_BYTES",
    "MAX_NONCE_ATTEMPTS",
    "PROBE_FILE_NAME",
    "PROBE_TIMEOUT_SECONDS",
    "ProbeEvidence",
    "RESTART_TIMEOUT_SECONDS",
    "RESTART_CHOICE_WARNING",
    "STATE_ANSWERING",
    "STATE_INACTIVE",
    "STATE_NEEDS_ACTIVATION",
    "STATE_NOT_DELEGATED",
    "STATE_UNKNOWN",
    "SUPPORTED_PROBE_FILESYSTEMS",
    "activation_passes",
    "choose_activation",
    "probe_accounting",
    "probe_file_path",
    "restart_user_manager_argv",
]

# --------------------------------------------------------------------------- #
# Fixed vocabulary
# --------------------------------------------------------------------------- #

#: The one basename this module may create beneath ``/var/lib/appio/probes/<uid>``.
PROBE_FILE_NAME = "probe.bin"

#: ``dd bs=1M count=16``: large enough that page-cache noise cannot hide it.
EXPECTED_PROBE_BYTES = 16 * 1024 * 1024
PROBE_MIB = 16

#: A finite deadline, so a stuck transient unit cannot hang the helper.
PROBE_TIMEOUT_SECONDS = 5.0

#: ``systemctl restart`` may legitimately take longer than the write deadline.
RESTART_TIMEOUT_SECONDS = 30.0

#: Bounded retries proving the chosen unit name did not exist before creation.
MAX_NONCE_ATTEMPTS = 8

SYSTEMD_RUN = "/usr/bin/systemd-run"
DD = "/usr/bin/dd"
SYSTEMCTL = "/usr/bin/systemctl"

#: Filesystems where the probe file's ``st_dev`` names the block device that
#: ``io.stat`` accounts. Anything else -- tmpfs, ramfs, overlay, btrfs (whose
#: per-file device is anonymous), fuse, an unknown type -- is unprovable and is
#: reported inactive rather than assumed.
SUPPORTED_PROBE_FILESYSTEMS: frozenset[str] = frozenset(
    {"ext2", "ext3", "ext4", "xfs", "f2fs"}
)

#: The exact states the UI may render. Nothing else is produced.
STATE_ANSWERING = "on, answering"
STATE_INACTIVE = "delegation present, accounting inactive"
STATE_NOT_DELEGATED = "not delegated"
STATE_NEEDS_ACTIVATION = "on, needs activation"
STATE_UNKNOWN = "unknown"

ACTIVATION_STATES: frozenset[str] = frozenset(
    {
        STATE_ANSWERING,
        STATE_INACTIVE,
        STATE_NOT_DELEGATED,
        STATE_NEEDS_ACTIVATION,
        STATE_UNKNOWN,
    }
)

#: The two consent choices, in the order a dialog should offer them. A choice
#: that ends the session carries its warning next to the label, never buried in
#: prose elsewhere: ``Linger=yes`` means only a manager restart or a reboot can
#: pick up the drop-ins, and restarting ends the session immediately.
CHOICE_RESTART_LABEL = "Restart my user manager now"
CHOICE_REBOOT_LABEL = "Activate after reboot"
RESTART_CHOICE_WARNING = (
    "This ends your session immediately and closes every program running in it."
)
ACTIVATION_CHOICES: tuple[tuple[str, str, str | None], ...] = (
    ("restart", CHOICE_RESTART_LABEL, RESTART_CHOICE_WARNING),
    ("reboot", CHOICE_REBOOT_LABEL, None),
)

CGROUP_ROOT = Path("/sys/fs/cgroup")
MOUNTINFO = Path("/proc/self/mountinfo")

_DEVICE = re.compile(r"^(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)$", re.ASCII)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]", re.ASCII)
_UID_MAX = 2**32 - 2
_MAX_READ_BYTES = 64 * 1024
_MAX_MOUNTINFO_BYTES = 1024 * 1024
_SQLITE_MAX = 2**63 - 1

_Choice = Literal["restart", "reboot"]


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ProbeEvidence:
    """Everything one probe observed, including what it could *not* observe.

    ``observed_bytes`` is ``None`` when no row for the verified device was read.
    It is never ``0`` by default: an unread counter and a measured zero are
    different facts, and only the second may be compared against the expected
    bytes.
    """

    delegation_present: bool
    filesystem_supported: bool
    expected_bytes: int
    observed_bytes: int | None
    device: str | None
    command_exit: int | None
    cleanup_complete: bool

    def __post_init__(self) -> None:
        for name in ("delegation_present", "filesystem_supported", "cleanup_complete"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ValueError(f"probe_evidence.{name} must be a boolean")
        _require_counter(self.expected_bytes, "probe_evidence.expected_bytes")
        if self.observed_bytes is not None:
            _require_counter(self.observed_bytes, "probe_evidence.observed_bytes")
        if self.device is not None:
            if not isinstance(self.device, str) or _DEVICE.fullmatch(self.device) is None:
                raise ValueError("probe_evidence.device must be a canonical major:minor")
        if self.command_exit is not None:
            if isinstance(self.command_exit, bool) or not isinstance(self.command_exit, int):
                raise ValueError("probe_evidence.command_exit must be an integer or None")


@dataclasses.dataclass(frozen=True)
class ActivationResult:
    """The activation verdict the helper returns for one UID.

    ``pending_activation`` answers exactly one question: *has a passing probe
    cleared this UID?* It is ``False`` only when such a probe ran, so an
    unreadable or absent state file can never be reported as activated.
    """

    pending_activation: bool
    state: str
    evidence: ProbeEvidence | None

    def __post_init__(self) -> None:
        if not isinstance(self.pending_activation, bool):
            raise ValueError("activation_result.pending_activation must be a boolean")
        if self.state not in ACTIVATION_STATES:
            raise ValueError(f"activation_result.state {self.state!r} is not a known state")
        if self.evidence is not None and not isinstance(self.evidence, ProbeEvidence):
            raise ValueError("activation_result.evidence must be ProbeEvidence or None")
        if self.state == STATE_ANSWERING:
            if self.evidence is None or not activation_passes(self.evidence):
                raise ValueError(
                    "an answering result requires evidence that actually passes the probe"
                )
            if self.pending_activation:
                raise ValueError("an answering result cannot still be pending activation")
        if self.state == STATE_UNKNOWN and self.evidence is not None:
            raise ValueError("an unknown result must not carry fabricated evidence")
        if self.state == STATE_NOT_DELEGATED:
            if self.evidence is None or self.evidence.delegation_present:
                raise ValueError("a not-delegated result requires evidence with no controller")


class ActivationError(RuntimeError):
    """A failure that must surface as an error rather than as a state.

    ``pending_activation`` is always ``True`` on these paths: whatever went
    wrong, no accounting was proved, so nothing may be reported as activated.
    ``evidence`` carries measurements when the failure happened after the probe
    ran (for example, the flag could not be cleared durably).
    """

    def __init__(
        self,
        operation: str,
        reason: str,
        *,
        pending_activation: bool = True,
        evidence: ProbeEvidence | None = None,
    ) -> None:
        super().__init__(f"{operation}: {reason}")
        self.operation = operation
        self.reason = reason
        self.pending_activation = pending_activation
        self.evidence = evidence


def activation_passes(evidence: ProbeEvidence) -> bool:
    """The single decision point: does this evidence prove working accounting?

    Every conjunct is a fact someone could otherwise over-claim: controller
    presence, a supported block-backed target, a zero command exit, a completed
    cleanup, a verified device, a positive expectation, and a *measured* write
    delta within 5%. The tolerance is exact integer arithmetic -- no float
    rounding can move 94 bytes of 100 into a pass.
    """

    if not isinstance(evidence, ProbeEvidence):
        raise TypeError("activation_passes requires ProbeEvidence")
    return (
        evidence.delegation_present
        and evidence.filesystem_supported
        and evidence.command_exit == 0
        and evidence.cleanup_complete
        and evidence.device is not None
        and evidence.expected_bytes > 0
        and evidence.observed_bytes is not None
        and abs(evidence.observed_bytes - evidence.expected_bytes) * 100
        <= evidence.expected_bytes * 5
    )


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #


def probe_file_path(uid: int) -> Path:
    """The one fixed probe file for one UID: ``<probe root>/<uid>/probe.bin``."""

    _require_uid(uid)
    return PROBE_ROOT / str(uid) / PROBE_FILE_NAME


def probe_accounting(
    identity: SetupIdentity, *, deps: SetupDeps, fs: SafeRootFS
) -> ActivationResult:
    """Prove device accounting for ``identity.uid`` and update its pending flag.

    A passing probe atomically clears only that UID's pending flag. Every other
    outcome leaves the flag alone and reports why, with the measured values for
    diagnosis -- never a claimed zero for ``T`` or ``D``.
    """

    _require_identity(identity)
    _require_deps(deps)
    _require_fs(fs)
    if deps.effective_uid() != 0:
        raise ActivationError(
            "privilege",
            "root_required",
            pending_activation=_pending_now(fs, identity.uid),
        )

    root = Path(deps.root)
    manager = _install_root(root, _user_manager_cgroup(identity.uid))
    controllers_file = manager / "cgroup.controllers"
    if not manager.is_dir() or not controllers_file.is_file():
        # No user manager to ask: this probe cannot distinguish "inactive" from
        # "active and unreadable", so it must not report either.
        return ActivationResult(
            pending_activation=_pending_now(fs, identity.uid),
            state=STATE_UNKNOWN,
            evidence=None,
        )
    if not _probe_environment_present(root, identity.uid):
        # No user bus or no probe executables: the probe cannot run at all.
        return ActivationResult(
            pending_activation=_pending_now(fs, identity.uid),
            state=STATE_UNKNOWN,
            evidence=None,
        )
    controllers = _read_text(controllers_file)
    if controllers is None:
        return ActivationResult(
            pending_activation=_pending_now(fs, identity.uid),
            state=STATE_UNKNOWN,
            evidence=None,
        )

    facts = _Facts(delegation_present="io" in controllers.split())
    probe_path = probe_file_path(identity.uid)
    try:
        try:
            # Pre-created, caller-owned 0600 through pinned descriptors: the
            # caller can write this file but cannot replace it, because every
            # parent is a root-owned, non-writable, execute-only directory.
            fs.atomic_write(
                str(probe_path), b"", mode=0o600, owner=(identity.uid, identity.gid)
            )
        except SafeRootFSError:
            # A target we could not prepare (already a symlink, hostile parent)
            # is neither supported nor cleanly cleaned up, so nothing is proved.
            facts.cleanup_complete = False
        else:
            facts.created = True
            facts.device, facts.filesystem_supported = _probe_target(root, probe_path)
            if facts.filesystem_supported and facts.delegation_present:
                unit, candidates = _select_unit(root, identity.uid, deps)
                if unit is not None:
                    facts.unit = unit
                    # The write is deterministic, but the cgroup is removed the instant
                    # the process exits, so a poll can catch a partial count (observed
                    # 15 MiB of 16 on this host). A partial is not evidence: retry the
                    # whole attempt a bounded number of times and keep the best reading.
                    best: int | None = None
                    launcher_exit = None
                    for attempt in range(_PROBE_ATTEMPTS):
                        attempt_unit = unit if attempt == 0 else f"{unit}-{attempt}"
                        # Baseline per attempt. A discarded attempt still writes bytes,
                        # so a probe-wide delta counts them twice and lands outside the
                        # 5% window even when each attempt was exact (32 MiB observed).
                        before = _slice_written_bytes(root, identity.uid, facts.device)
                        launcher_exit = _run_probe(
                            identity, deps=deps, probe_path=probe_path, unit=attempt_unit
                        )
                        observed = _observe_while_alive(
                            root, _unit_cgroup_candidates(identity.uid, attempt_unit),
                            facts.device, deps=deps, unit=attempt_unit,
                            identity=identity,
                        )
                        # The slice outlives the unit, so its delta is the reliable
                        # reading; the per-unit poll corroborates when it lands exactly.
                        delta = _settled_slice_delta(root, identity.uid, facts.device, before)
                        candidate = delta if delta is not None else observed
                        if candidate is not None:
                            best = candidate if best is None else max(best, candidate)
                        if best is not None and abs(best - EXPECTED_PROBE_BYTES) * 100 <= EXPECTED_PROBE_BYTES * 5:
                            # Accepted: leave the unit alone so its ExecMainStatus can
                            # still be read; cleanup stops it later.
                            break
                        _stop_probe_unit(identity, deps, attempt_unit)
                    facts.observed_bytes = best
                    facts.command_exit = (
                        launcher_exit if launcher_exit not in (0, None)
                        else _probe_unit_status(deps, attempt_unit, identity)
                    )
    finally:
        facts.cleanup_complete = _cleanup(
            identity,
            deps,
            root=root,
            unit=facts.unit,
            probe_path=probe_path if facts.created else None,
        ) and facts.cleanup_complete

    evidence = facts.evidence()
    if activation_passes(evidence):
        try:
            set_pending(fs, identity.uid, False)
        except (SafeRootFSError, SetupStateError) as exc:
            raise ActivationError(
                "clear-pending",
                getattr(exc, "reason", None) or "state_unavailable",
                pending_activation=True,
                evidence=evidence,
            ) from exc
        return ActivationResult(
            pending_activation=False, state=STATE_ANSWERING, evidence=evidence
        )

    # A definite verdict that is not a pass re-arms the flag. Leaving a value an
    # earlier pass cleared would report a broken accounting path as activated,
    # which is the false claim this module exists to prevent in both directions.
    try:
        set_pending(fs, identity.uid, True)
    except (SafeRootFSError, SetupStateError, OSError):
        # `atomic_write` fsyncs and renames directly, so a plain OSError can
        # escape the state layer. The verdict and its measurements still stand,
        # and the returned flag is still pending, so the caller keeps them.
        deps.event("persist-pending-failed")
    state = STATE_INACTIVE if evidence.delegation_present else STATE_NOT_DELEGATED
    return ActivationResult(pending_activation=True, state=state, evidence=evidence)


def choose_activation(
    choice: _Choice, identity: SetupIdentity, *, deps: SetupDeps, fs: SafeRootFS
) -> ActivationResult:
    """Persist the user's explicit activation choice; never invent one.

    Both choices persist ``pending_activation`` first. ``reboot`` stops there and
    never runs a reboot command. ``restart`` then runs the one fixed
    ``systemctl restart user@<uid>.service`` vector as root, which ends the
    session; a zero exit is still not evidence, so the flag remains set.
    """

    _require_choice(choice)
    _require_identity(identity)
    _require_deps(deps)
    _require_fs(fs)
    if deps.effective_uid() != 0:
        raise ActivationError("privilege", "root_required", pending_activation=True)

    # Before anything disruptive: a crash now still surfaces as pending.
    try:
        set_pending(fs, identity.uid, True)
    except (SafeRootFSError, SetupStateError) as exc:
        raise ActivationError(
            "persist-pending", getattr(exc, "reason", None) or "state_unavailable"
        ) from exc
    if choice == "reboot":
        return ActivationResult(
            pending_activation=True, state=STATE_NEEDS_ACTIVATION, evidence=None
        )

    argv = restart_user_manager_argv(identity.uid)
    try:
        result = deps.runner.run(argv, timeout_seconds=RESTART_TIMEOUT_SECONDS)
    except Exception as exc:  # the runner is a fixed-argv boundary; never retry
        raise ActivationError(
            "restart-user-manager", "runner_error", pending_activation=True
        ) from exc
    returncode = getattr(result, "returncode", None)
    if returncode != 0:
        raise ActivationError(
            "restart-user-manager",
            f"exit_{returncode}",
            pending_activation=True,
        )
    return ActivationResult(
        pending_activation=True, state=STATE_NEEDS_ACTIVATION, evidence=None
    )


def restart_user_manager_argv(uid: int) -> tuple[str, ...]:
    """The one fixed, consented restart vector for exactly this UID."""

    _require_uid(uid)
    return (SYSTEMCTL, "restart", f"user@{uid}.service")


# --------------------------------------------------------------------------- #
# Probe internals
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class _Facts:
    """Mutable probe bookkeeping, so cleanup can complete a ``finally``."""

    delegation_present: bool
    filesystem_supported: bool = False
    device: str | None = None
    observed_bytes: int | None = None
    command_exit: int | None = None
    cleanup_complete: bool = True
    created: bool = False
    unit: str | None = None

    def evidence(self) -> ProbeEvidence:
        return ProbeEvidence(
            delegation_present=self.delegation_present,
            filesystem_supported=self.filesystem_supported,
            expected_bytes=EXPECTED_PROBE_BYTES,
            observed_bytes=self.observed_bytes,
            device=self.device,
            command_exit=self.command_exit,
            cleanup_complete=self.cleanup_complete,
        )


def _user_manager_cgroup(uid: int) -> Path:
    return (
        CGROUP_ROOT
        / "user.slice"
        / f"user-{uid}.slice"
        / f"user@{uid}.service"
    )


def _unit_cgroup_candidates(uid: int, unit: str) -> tuple[Path, ...]:
    """Both places a transient user unit can appear, most specific first."""

    manager = _user_manager_cgroup(uid)
    return (manager / "app.slice" / f"{unit}.service", manager / f"{unit}.service")


def _select_unit(
    root: Path, uid: int, deps: SetupDeps
) -> tuple[str | None, tuple[Path, ...]]:
    """First nonce whose unit does not exist *before* creation.

    The transient unit's initial counters may be treated as zero only because
    this proof holds for the chosen name: a leftover unit with plausible-looking
    ``io.stat`` bytes can never be read as this probe's evidence.
    """

    base = int(deps.clock_ns())
    for offset in range(MAX_NONCE_ATTEMPTS):
        unit = f"appio-probe-{base + offset}"
        candidates = _unit_cgroup_candidates(uid, unit)
        existing = [path for path in candidates if _install_root(root, path).exists()]
        if not existing:
            return unit, candidates
    return None, ()


def _locate_leaf(root: Path, candidates: tuple[Path, ...]) -> Path | None:
    found = [path for path in (_install_root(root, c) for c in candidates) if path.is_dir()]
    return found[0] if len(found) == 1 else None


def _run_probe(
    identity: SetupIdentity, *, deps: SetupDeps, probe_path: Path, unit: str
) -> int | None:
    """Start the transient unit as the validated caller; return its exit code."""

    argv = (
        SYSTEMD_RUN,
        "--user",
        f"--unit={unit}",
        "--property=Type=oneshot",
        "--property=RemainAfterExit=yes",
        "--no-block",
        DD,
        "if=/dev/zero",
        f"of={probe_path}",
        "bs=1M",
        f"count={PROBE_MIB}",
        "oflag=direct",
    )
    try:
        result = deps.runner.run(
            argv,
            uid=identity.uid,
            gid=identity.gid,
            env=_caller_environment(identity.uid),
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
        )
    except Exception:  # a runner that cannot start is "no evidence", not a crash
        return None
    returncode = getattr(result, "returncode", None)
    return returncode if isinstance(returncode, int) else None


_CONTROLLER_DEVICE = re.compile(r"[0-9]+:[0-9]+", re.ASCII)


def _device_candidates(root: Path, device: str) -> tuple[str, ...]:
    """The probe device, then its whole-disk ancestor when it is a partition.

    cgroup v2 ``io.stat`` attributes a bio to the block device the request was issued
    against, and kernels differ in whether a partition write is reported against the
    partition (``259:5``) or its whole disk (``259:1``). This host reports the whole
    disk, so an exact-only comparison found no row at all: activation was reported
    "accounting inactive" forever while the accounting was byte-exact. sysfs is the
    authority for the relation, not arithmetic on minor numbers.
    """

    tokens = [device]
    entry = Path(root) / "sys/dev/block" / device
    try:
        resolved = Path(os.path.realpath(entry))
    except OSError:  # pragma: no cover - realpath does not raise for a missing path
        return tuple(tokens)
    if not resolved.is_dir():
        return tuple(tokens)
    try:
        parent_dev = (resolved.parent / "dev").read_text(encoding="ascii").strip()
    except OSError:
        return tuple(tokens)
    if _CONTROLLER_DEVICE.fullmatch(parent_dev) and parent_dev != device:
        tokens.append(parent_dev)
    return tuple(tokens)


#: How long one probe attempt waits for the unit's io.stat to report its write, and how
#: many attempts are made. The write is 16 MiB of direct I/O, so the cgroup is short
#: lived; polling starts immediately after the unit is started.
_PROBE_POLL_SECONDS = 1.0
_PROBE_POLL_INTERVAL = 0.002
_PROBE_ATTEMPTS = 3
#: How long the slice counter is re-sampled after the attempt, and how often.
_SLICE_SETTLE_READS = 100
_SLICE_SETTLE_INTERVAL = 0.005


def _stop_probe_unit(identity: SetupIdentity, deps: SetupDeps, unit: str) -> None:
    """Stop and reset one attempt's unit so a retry cannot inherit its cgroup."""

    for verb in ("stop", "reset-failed"):
        try:
            deps.runner.run(
                (SYSTEMCTL, "--user", verb, f"{unit}.service"),
                uid=identity.uid, gid=identity.gid, env=_caller_environment(identity.uid),
            )
        except Exception:  # a stop that cannot run must not abort the probe
            return


def _probe_unit_status(deps: SetupDeps, unit: str, identity: SetupIdentity) -> int | None:
    """The probe unit's own ``ExecMainStatus``, or ``None`` when unreadable.

    ``_run_probe`` starts the unit with ``--no-block`` so its cgroup survives long
    enough to be observed; that means the launcher's exit status is *not* the write's
    status. Reading the unit's own status is what keeps ``command_exit`` meaning what
    ``activation_passes`` requires it to mean. Unreadable is ``None``, never zero.
    """

    argv = (SYSTEMCTL, "--user", "show", f"{unit}.service", "-p", "ExecMainStatus", "--value")
    try:
        result = deps.runner.run(
            argv, uid=identity.uid, gid=identity.gid, env=_caller_environment(identity.uid)
        )
    except Exception:  # a runner that cannot answer is "no evidence"
        return None
    if getattr(result, "returncode", None) != 0:
        return None
    text = str(getattr(result, "stdout", "") or "").strip()
    return int(text) if text.isdigit() else None


def _settled_slice_delta(
    root: Path, uid: int, device: str | None, before: int | None
) -> int | None:
    """One attempt's slice write delta, sampled until it stops growing."""

    if before is None:
        return None
    best: int | None = None
    for _ in range(_SLICE_SETTLE_READS):
        reading = _slice_written_bytes(root, uid, device)
        if reading is not None:
            delta = reading - before
            if best is None or delta > best:
                best = delta
            if best >= EXPECTED_PROBE_BYTES * 95 // 100:
                break
        time.sleep(_SLICE_SETTLE_INTERVAL)
    return best


def _slice_written_bytes(root: Path, uid: int, device: str | None) -> int | None:
    """Cumulative writes charged to the caller's ``app.slice``, for one device.

    The slice outlives every transient unit inside it, so its counter is still readable
    after the probe's cgroup has been removed. A per-bio counter is only momentarily at
    its final value, which is why polling a dying cgroup returns partials (14, 15, 11.5
    MiB of 16 were observed) and why the slice delta is the reliable reading.
    """

    if device is None:
        return None
    slice_path = _user_manager_cgroup(uid) / "app.slice" / "io.stat"
    text = _read_text(_install_root(Path(root), slice_path))
    if text is None:
        return None
    try:
        stats = parse_io_stat(text, cgroup_id=0)
    except ValueError:
        return None
    for candidate in _device_candidates(Path(root), device):
        for stat in stats:
            if stat.dev == candidate:
                return stat.wbytes
    return None


def _observe_while_alive(
    root: Path,
    candidates: tuple[Path, ...],
    device: str | None,
    *,
    deps: SetupDeps,
    unit: str,
    identity: SetupIdentity,
) -> int | None:
    """Read the unit's own ``io.stat`` while the unit still exists.

    Returns the highest confident reading across attempts, or ``None`` when every
    attempt finished before a readable sample existed. ``None`` stays "unmeasured",
    never zero.
    """

    deadline = time.monotonic() + _PROBE_POLL_SECONDS
    best: int | None = None
    while True:
        leaf = _locate_leaf(root, candidates)
        if leaf is not None:
            # None means "no row for this device"; 0 is a measured zero and must be
            # reported as such rather than treated as missing evidence.
            value = _observed_writes(leaf, device, root)
            if value is not None:
                best = value if best is None else max(best, value)
                if best >= EXPECTED_PROBE_BYTES:
                    return best
        elif best is not None:
            # The unit finished: the cgroup is gone, so the last reading is final.
            return best
        if time.monotonic() >= deadline:
            return best
        time.sleep(_PROBE_POLL_INTERVAL)


def _observed_writes(leaf: Path | None, device: str | None, root: Path | None = None) -> int | None:
    """The unit's own ``wbytes`` for the verified device, or ``None``."""

    if leaf is None or device is None:
        return None
    text = _read_text(leaf / "io.stat")
    if text is None:
        return None
    try:
        stats = parse_io_stat(text, cgroup_id=0)
    except ValueError:
        # Malformed evidence is unmeasured evidence. It must not abort the probe
        # before cleanup, and it must never be repaired into a number.
        return None
    candidates = _device_candidates(Path(root), device) if root is not None else (device,)
    for candidate in candidates:
        for stat in stats:
            if stat.dev == candidate:
                return stat.wbytes
    return None


def _cleanup(
    identity: SetupIdentity,
    deps: SetupDeps,
    *,
    root: Path,
    unit: str | None,
    probe_path: Path | None,
) -> bool:
    """Stop/reset the exact unit and unlink the exact probe file.

    The verdict is the *artifact*, not a command's exit status: a transient unit
    that is already gone makes ``reset-failed`` exit nonzero on some hosts, and
    treating that as a cleanup failure would report working accounting as
    inactive for ever. Both commands are still attempted, so a host that needs
    them gets them; what decides is whether a cgroup is left behind.
    """

    clean = True
    if unit is not None:
        for verb in ("stop", "reset-failed"):
            _stop_command(identity, deps, verb, unit)
        clean = not any(
            _install_root(root, path).exists()
            for path in _unit_cgroup_candidates(identity.uid, unit)
        )
    if probe_path is not None:
        clean = _unlink_probe(deps, probe_path) and clean
    return clean


def _stop_command(identity: SetupIdentity, deps: SetupDeps, verb: str, unit: str) -> None:
    """One best-effort cleanup command; the caller inspects the artifact."""

    try:
        deps.runner.run(
            (SYSTEMCTL, "--user", verb, unit),
            uid=identity.uid,
            gid=identity.gid,
            env=_caller_environment(identity.uid),
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
        )
    except Exception:
        # A cleanup command that cannot run is not proof the artifact survived.
        return


def _unlink_probe(deps: SetupDeps, probe_path: Path) -> bool:
    """Remove the exact probe file.

    The path is built only from a validated UID and fixed constants, and every
    parent was verified root-owned and non-writable when the file was created, so
    there is no directory entry an attacker could redirect this unlink to.
    ``os.unlink`` never follows a final symlink, which is the other half.
    """

    try:
        os.unlink(Path(deps.root) / probe_path.relative_to("/"))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _probe_target(root: Path, probe_path: Path) -> tuple[str | None, bool]:
    """The verified block device of the probe file, and whether it is provable."""

    target = Path(root) / probe_path.relative_to("/")
    try:
        info = os.stat(target)
    except OSError:
        return None, False
    if not stat.S_ISREG(info.st_mode):
        return None, False
    device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
    entries = _read_mountinfo(root)
    if entries is None:
        return None, False
    mount = _backing_mount(entries, str(target))
    if mount is None:
        return None, False
    mountpoint_device, filesystem = mount
    if filesystem not in SUPPORTED_PROBE_FILESYSTEMS:
        return None, False
    if mountpoint_device != device:
        # A stacked or redirected mount: reading one device's bytes would not
        # describe this file's writes, so it is unprovable rather than assumed.
        return None, False
    return device, True


def _read_mountinfo(root: Path) -> tuple[tuple[str, str, str], ...] | None:
    text = _read_text(
        Path(root) / MOUNTINFO.relative_to("/"), max_bytes=_MAX_MOUNTINFO_BYTES
    )
    if text is None:
        return None
    entries: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        fields = left.split()
        tail = right.split()
        if len(fields) < 5 or not tail:
            continue
        entries.append((_unescape(fields[4]), fields[2], tail[0]))
    return tuple(entries)


def _backing_mount(
    entries: tuple[tuple[str, str, str], ...], path: str
) -> tuple[str, str] | None:
    """The longest mount point that is a component prefix of ``path``."""

    best: tuple[str, str] | None = None
    best_length = -1
    for mountpoint, device, filesystem in entries:
        if not _is_component_prefix(mountpoint, path):
            continue
        length = len(mountpoint)
        if length > best_length:
            best, best_length = (device, filesystem), length
    return best


def _is_component_prefix(mountpoint: str, path: str) -> bool:
    if mountpoint == "/":
        return True
    if not path.startswith(mountpoint):
        return False
    return len(path) == len(mountpoint) or path[len(mountpoint)] == "/"


def _unescape(value: str) -> str:
    """Decode the octal escapes mountinfo applies to spaces and newlines."""

    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _probe_environment_present(root: Path, uid: int) -> bool:
    """Can this host run the probe at all?

    Missing here means *unknown*, never success: without a user bus or the probe
    executables there is no way to produce evidence either way.
    """

    for executable in (SYSTEMD_RUN, DD):
        try:
            info = os.stat(Path(root) / executable.lstrip("/"))
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            return False
    return _caller_bus_path(root, uid).exists()


def _caller_environment(uid: int) -> dict[str, str]:
    """The freshly derived user bus for the validated caller, nothing inherited."""

    runtime = f"/run/user/{uid}"
    return {
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }


def _caller_bus_path(root: Path, uid: int) -> Path:
    return Path(root) / Path(f"/run/user/{uid}/bus").relative_to("/")


def _install_root(root: Path, fixed: Path) -> Path:
    return Path(root) / fixed.relative_to("/")


def _read_text(path: Path, *, max_bytes: int = _MAX_READ_BYTES) -> str | None:
    """Read one bounded, non-symlinked kernel or tree file, or report absence."""

    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
    except OSError:
        return None
    try:
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _pending_now(fs: SafeRootFS, uid: int) -> bool:
    """Whether activation is still unproved for this UID.

    ``False`` requires a durable record that a probe passed. An absent, unreadable
    or corrupt state file is therefore *pending*, never activated.
    """

    try:
        state = load_setup_state(fs)
    except (SetupStateError, OSError):
        return True
    return bool(state.pending_by_uid.get(uid, True))


# --------------------------------------------------------------------------- #
# Field validation
# --------------------------------------------------------------------------- #


def _require_counter(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 0 or value > _SQLITE_MAX:
        raise ValueError(f"{field} must be within [0, {_SQLITE_MAX}]")
    return value


def _require_uid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("uid must be an integer")
    if value < 1 or value > _UID_MAX:
        raise ValueError(f"uid must be within [1, {_UID_MAX}]")
    return value


def _require_identity(identity: object) -> SetupIdentity:
    if not isinstance(identity, SetupIdentity):
        raise ValueError("identity must be a SetupIdentity")
    _require_uid(identity.uid)
    if isinstance(identity.gid, bool) or not isinstance(identity.gid, int):
        raise ValueError("identity.gid must be an integer")
    if identity.gid < 0 or identity.gid > _UID_MAX:
        raise ValueError("identity.gid is outside the gid range")
    if not isinstance(identity.username, str) or not identity.username:
        raise ValueError("identity.username must be a non-empty string")
    if _CONTROL_CHARS.search(identity.username):
        raise ValueError("identity.username must not contain control characters")
    if not isinstance(identity.home, Path):
        raise ValueError("identity.home must be a Path")
    return identity


def _require_deps(deps: object) -> SetupDeps:
    if not isinstance(deps, SetupDeps):
        raise ValueError("deps must be a SetupDeps")
    for name in ("effective_uid", "clock_ns", "event"):
        if not callable(getattr(deps, name, None)):
            raise ValueError(f"deps.{name} must be callable")
    if not callable(getattr(deps.runner, "run", None)):
        raise ValueError("deps.runner must provide run()")
    return deps


def _require_fs(fs: object) -> SafeRootFS:
    for name in ("atomic_write", "read_bounded", "locked"):
        if not callable(getattr(fs, name, None)):
            raise ValueError(f"fs must provide {name}()")
    return fs  # type: ignore[return-value]


def _require_choice(choice: object) -> str:
    if choice not in ("restart", "reboot"):
        raise ValueError('activation choice must be "restart" or "reboot"')
    return str(choice)
