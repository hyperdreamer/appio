"""Adversarial tests for the accounting-activation probe and session choice.

The threat this file pins is:

    *controller presence is not accounting*. A delegated ``io`` controller, a
    successful ``dd`` exit, a leftover probe file, or a stale ``io.stat`` under a
    unit name we did not create must never be reported as working device
    accounting. Only the transient unit's own ``io.stat`` moving by the expected
    bytes (within 5%, in integer arithmetic) may clear ``pending_activation``.

    *activation is a user choice, not a side effect*. Installing delegation must
    never end a session on its own: ``Activate after reboot`` only persists
    ``pending_activation`` and never reboots, and ``Restart my user manager now``
    persists pending *before* running the one fixed ``systemctl restart
    user@<uid>.service`` vector.

    *Linger=yes makes logout advice false*, so no activation path may tell the
    user to simply log out.

Nothing here needs root, a real user manager, or systemd: the filesystem root,
effective UID, metadata, ownership callback, clock, command runner, cgroup tree
and mount table are all injected. The fake runner materialises the transient
unit's cgroup under the injected root, which is the same tree shape the P1
``appio.cgroups`` parser reads in production.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from appio.activation import (
    ACTIVATION_CHOICES,
    CHOICE_REBOOT_LABEL,
    CHOICE_RESTART_LABEL,
    EXPECTED_PROBE_BYTES,
    RESTART_CHOICE_WARNING,
    STATE_ANSWERING,
    STATE_INACTIVE,
    STATE_NEEDS_ACTIVATION,
    STATE_NOT_DELEGATED,
    STATE_UNKNOWN,
    ActivationError,
    ActivationResult,
    ProbeEvidence,
    activation_passes,
    choose_activation,
    probe_file_path,
    probe_accounting,
)
from appio.setup_boundary import CommandResult, SetupDeps, SetupIdentity
from appio.setup_delegation import load_setup_state, set_pending
from appio.setup_files import PROBE_ROOT, STATE_FILE, SafeRootFS, SafeRootFSError

CGROUP_ROOT = Path("/sys/fs/cgroup")
USER_SLICE = CGROUP_ROOT / "user.slice" / "user-1000.slice"
USER_MANAGER = USER_SLICE / "user@1000.service"
APP_SLICE = USER_MANAGER / "app.slice"
MOUNTINFO = Path("/proc/self/mountinfo")
RUN_USER_BUS = Path("/run/user/1000/bus")

SYSTEMD_RUN = "/usr/bin/systemd-run"
DD = "/usr/bin/dd"
SYSTEMCTL = "/usr/bin/systemctl"

PROBE = probe_file_path(1000)
NONCE = 987654321_000_000_000
EXPECTED = EXPECTED_PROBE_BYTES

UNIT = f"appio-probe-{NONCE}"
USER_BUS_ENV = {
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
}


class InjectedFault(RuntimeError):
    pass


class FakeMetadata:
    """Every path looks root-owned unless a test overrides that inode."""

    def __init__(self, default_uid: int = 0):
        self.default_uid = default_uid
        self.overrides: dict[tuple[int, int], tuple[int | None, int | None]] = {}

    @staticmethod
    def inode(path: Path | str) -> tuple[int, int]:
        info = os.stat(path, follow_symlinks=False)
        return (info.st_dev, info.st_ino)

    def override(self, path: Path | str, *, uid: int | None = None, mode: int | None = None) -> None:
        self.overrides[self.inode(path)] = (uid, mode)

    def __call__(self, fd: int) -> SimpleNamespace:
        real = os.fstat(fd)
        uid, permissions = self.overrides.get((real.st_dev, real.st_ino), (self.default_uid, None))
        mode = real.st_mode if permissions is None else (real.st_mode & ~0o777) | permissions
        return SimpleNamespace(
            st_uid=uid,
            st_gid=0,
            st_mode=mode,
            st_ino=real.st_ino,
            st_dev=real.st_dev,
            st_nlink=real.st_nlink,
        )


def _executable(path: Path) -> None:
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o755)


def device_token(path: Path) -> str:
    info = os.stat(path)
    return f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"


def mount_line(mountpoint: str, fstype: str, device: str, index: int = 36) -> str:
    return f"{index} 35 {device} / {mountpoint} rw,relatime - {fstype} /dev/root rw\n"


def flag(argv: tuple[str, ...], prefix: str) -> str:
    for token in argv:
        if token.startswith(prefix):
            return token[len(prefix) :]
    raise AssertionError(f"no {prefix} in {argv}")


class ProbeRunner:
    """A fake runner that materialises the transient unit's cgroup.

    ``systemd-run`` creates ``<app.slice>/<unit>.service/io.stat`` for the unit
    named on its own command line, exactly as the kernel would for a unit that
    wrote ``write_bytes`` through the block device of the probe file.
    """

    def __init__(
        self,
        *,
        write_bytes: int | None = EXPECTED,
        dd_exit: int = 0,
        dd_stderr: str = "",
        timed_out: bool = False,
        create_unit: bool = True,
        device_override: str | None = None,
        stop_exit: int = 0,
        reset_exit: int = 0,
        stop_raises: bool = False,
    ):
        self.write_bytes = write_bytes
        self.dd_exit = dd_exit
        self.dd_stderr = dd_stderr
        self.timed_out = timed_out
        self.create_unit = create_unit
        self.device_override = device_override
        self.stop_exit = stop_exit
        self.reset_exit = reset_exit
        self.stop_raises = stop_raises
        self.calls: list[tuple[tuple[str, ...], int, int, dict[str, str], float]] = []
        self.units: list[str] = []

    # -- observation helpers -------------------------------------------------

    def runs(self, executable: str) -> list[tuple[str, ...]]:
        return [argv for argv, *_rest in self.calls if argv[0] == executable]

    def unit_dir(self, unit: str) -> Path:
        return self._root / APP_SLICE.relative_to("/") / f"{unit}.service"

    # -- fake command execution ---------------------------------------------

    def run(self, argv, *, uid=0, gid=0, env=None, timeout_seconds=5.0):
        call = tuple(argv)
        self.calls.append((call, uid, gid, dict(env or {}), timeout_seconds))
        if call[0] == SYSTEMD_RUN:
            unit = flag(call, "--unit=")
            self.units.append(unit)
            if self.create_unit:
                leaf = self.unit_dir(unit)
                leaf.mkdir(parents=True, exist_ok=True)
                if self.write_bytes is not None:
                    (leaf / "io.stat").write_text(
                        self._io_stat(flag(call, "of=")), encoding="utf-8"
                    )
            if self.timed_out:
                return CommandResult(-9, "", "appio-setup: command timed out after 5s")
            return CommandResult(self.dd_exit, "", self.dd_stderr)
        if call[0] == SYSTEMCTL and call[1] == "--user" and call[2] == "stop":
            if self.stop_raises:
                raise InjectedFault("stop failed")
            if self.stop_exit == 0:
                leaf = self.unit_dir(call[3])
                if leaf.exists():
                    for child in leaf.iterdir():
                        child.unlink()
                    leaf.rmdir()
            return CommandResult(self.stop_exit, "", "" if self.stop_exit == 0 else "stop failed")
        if call[0] == SYSTEMCTL and call[1] == "--user" and call[2] == "reset-failed":
            return CommandResult(self.reset_exit, "", "" if self.reset_exit == 0 else "no such unit")
        return CommandResult(0, "", "")

    def _io_stat(self, probe: str) -> str:
        rebased = self._root / Path(probe).relative_to("/")
        device = self.device_override or device_token(rebased)
        return f"{device} rbytes=0 wbytes={self.write_bytes} rios=0 wios=0\n"

    _root: Path = Path("/")


def make_deps(root: Path, runner: ProbeRunner, *, effective_uid=None, chown=None, clock_ns=None):
    return SetupDeps(
        root=Path(root),
        runner=runner,
        effective_uid=effective_uid or (lambda: 0),
        getpwuid=lambda uid: SimpleNamespace(
            pw_uid=uid, pw_gid=1000, pw_name="alice", pw_dir="/home/alice"
        ),
        metadata=FakeMetadata(),
        chown=chown or (lambda fd, uid, gid: None),
        clock_ns=clock_ns or (lambda: NONCE),
        event=lambda name: None,
    )


def identity(**overrides) -> SetupIdentity:
    fields = dict(uid=1000, gid=1000, username="alice", home=Path("/home/alice"))
    fields.update(overrides)
    return SetupIdentity(**fields)


class Harness:
    """One injected root: cgroup tree, mount table, user bus, probe file."""

    def __init__(
        self,
        root: Path,
        *,
        delegate: str = "cpu io memory pids",
        bus: bool = True,
        systemd_run: bool = True,
        mountpoints: list[tuple[str, str, str]] | None = None,
        precreate_probe: bool = True,
    ):
        self.root = root
        self.runner = ProbeRunner()
        self.runner._root = root
        self._probe = root / PROBE.relative_to("/")

        manager = root / USER_MANAGER.relative_to("/")
        manager.mkdir(parents=True, exist_ok=True)
        (manager / "cgroup.controllers").write_text(delegate + "\n", encoding="utf-8")
        (root / APP_SLICE.relative_to("/")).mkdir(parents=True, exist_ok=True)

        (root / "usr/bin").mkdir(parents=True, exist_ok=True)
        if systemd_run:
            _executable(root / "usr/bin/systemd-run")
        _executable(root / "usr/bin/dd")

        if bus:
            bus_path = root / RUN_USER_BUS.relative_to("/")
            bus_path.parent.mkdir(parents=True, exist_ok=True)
            bus_path.write_text("", encoding="utf-8")

        if precreate_probe:
            self._probe.parent.mkdir(parents=True, exist_ok=True)
            self._probe.write_bytes(b"")
            os.chmod(self._probe.parent, 0o711)
        self.device = device_token(self._probe) if self._probe.exists() else None
        self.mountpoints = mountpoints

    @property
    def probe(self) -> Path:
        return self._probe

    def write_mountinfo(self, entries: list[tuple[str, str, str]] | None = None) -> None:
        entries = entries or [(str(self.root), "ext4", device_token(self._probe))]
        text = "".join(
            mount_line(mountpoint, fstype, device, index=index + 30)
            for index, (mountpoint, fstype, device) in enumerate(entries)
        )
        target = self.root / MOUNTINFO.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def deps(self, **kwargs) -> SetupDeps:
        return make_deps(self.root, self.runner, **kwargs)

    def probe_now(self, deps: SetupDeps | None = None) -> ActivationResult:
        deps = deps or self.deps()
        return probe_accounting(identity(), deps=deps, fs=SafeRootFS(deps))

    # -- assertions shared by every failure vector ---------------------------

    def assert_unit_cleaned(self, unit: str = UNIT) -> None:
        assert not self.runner.unit_dir(unit).exists(), f"unit {unit} was not removed"
        argv_stop = (SYSTEMCTL, "--user", "stop", unit)
        argv_reset = (SYSTEMCTL, "--user", "reset-failed", unit)
        assert argv_stop in self.runner.runs(SYSTEMCTL)
        assert argv_reset in self.runner.runs(SYSTEMCTL)

    def assert_probe_file_removed(self) -> None:
        assert not self._probe.exists(), "the caller-owned probe file was left behind"


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    built = Harness(tmp_path / "root")
    built.write_mountinfo()
    return built


def pending(fs_root: Harness) -> dict[int, bool]:
    deps = fs_root.deps()
    return dict(load_setup_state(SafeRootFS(deps)).pending_by_uid)


def seed_pending(built: Harness) -> None:
    deps = built.deps()
    set_pending(SafeRootFS(deps), 1000, True)


# --------------------------------------------------------------------------- #
# The two contracts stated verbatim by the plan.
# --------------------------------------------------------------------------- #


def test_controller_without_device_bytes_never_passes():
    evidence = ProbeEvidence(True, True, 16 * 1024 * 1024, 0, "259:0", 0, True)
    assert not activation_passes(evidence)
    assert activation_passes(dataclasses.replace(evidence, observed_bytes=16 * 1024 * 1024))
    assert not activation_passes(dataclasses.replace(evidence, filesystem_supported=False))
    assert not activation_passes(dataclasses.replace(evidence, cleanup_complete=False))


def test_five_percent_comparison_is_exact_integer_arithmetic():
    evidence = ProbeEvidence(True, True, 100, 95, "259:0", 0, True)
    assert activation_passes(evidence)
    assert activation_passes(dataclasses.replace(evidence, observed_bytes=105))
    assert not activation_passes(dataclasses.replace(evidence, observed_bytes=94))
    assert not activation_passes(dataclasses.replace(evidence, observed_bytes=106))


# --------------------------------------------------------------------------- #
# Threat: controller presence is reported as working accounting.
# --------------------------------------------------------------------------- #


def test_absent_controller_reports_not_delegated_without_running_anything(harness: Harness):
    harness.root.joinpath(USER_MANAGER.relative_to("/"), "cgroup.controllers").write_text(
        "cpu memory pids\n", encoding="utf-8"
    )
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_NOT_DELEGATED
    assert result.pending_activation is True
    assert pending(harness) == {1000: True}
    assert harness.runner.calls == []
    assert result.evidence is not None and result.evidence.delegation_present is False
    assert result.evidence.observed_bytes is None


def test_delegated_controller_without_bytes_reports_accounting_inactive(harness: Harness):
    harness.runner.write_bytes = 0
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.pending_activation is True
    assert pending(harness) == {1000: True}
    assert result.evidence.delegation_present is True
    assert result.evidence.filesystem_supported is True
    assert result.evidence.command_exit == 0
    assert result.evidence.observed_bytes == 0
    assert result.evidence.expected_bytes == EXPECTED
    assert result.evidence.device == harness.device
    harness.assert_unit_cleaned()
    harness.assert_probe_file_removed()


def test_wrong_device_row_never_passes(harness: Harness):
    harness.runner.device_override = "253:99"
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert pending(harness) == {1000: True}
    assert result.evidence.device == harness.device
    assert result.evidence.observed_bytes is None
    assert result.evidence.observed_bytes != EXPECTED
    harness.assert_unit_cleaned()


def test_missing_io_stat_is_unmeasured_not_a_zero(harness: Harness):
    harness.runner.write_bytes = None
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert pending(harness) == {1000: True}
    assert result.evidence.observed_bytes is None
    harness.assert_unit_cleaned()


def test_correct_bytes_clear_pending_and_report_answering(harness: Harness):
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_ANSWERING
    assert result.pending_activation is False
    assert pending(harness) == {1000: False}
    assert result.evidence.observed_bytes == EXPECTED
    assert result.evidence.command_exit == 0
    harness.assert_unit_cleaned()
    harness.assert_probe_file_removed()


def test_measured_bytes_within_tolerance_still_pass(harness: Harness):
    harness.runner.write_bytes = EXPECTED + EXPECTED // 20
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_ANSWERING
    assert pending(harness) == {1000: False}


def test_bytes_one_percent_over_tolerance_never_pass(harness: Harness):
    harness.runner.write_bytes = EXPECTED + EXPECTED // 20 + 1
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert pending(harness) == {1000: True}


# --------------------------------------------------------------------------- #
# Threat: a command exit status is mistaken for evidence.
# --------------------------------------------------------------------------- #


def test_dd_error_never_passes_even_when_bytes_match(harness: Harness):
    harness.runner.dd_exit = 1
    harness.runner.dd_stderr = "dd: failed to open: Operation not permitted"
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.command_exit == 1
    assert result.evidence.observed_bytes == EXPECTED
    assert pending(harness) == {1000: True}
    harness.assert_unit_cleaned()


def test_timeout_never_passes(harness: Harness):
    harness.runner.timed_out = True
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.command_exit == -9
    assert pending(harness) == {1000: True}
    harness.assert_unit_cleaned()


# --------------------------------------------------------------------------- #
# Threat: an unprovable target or a cannot-run host is reported as success.
# --------------------------------------------------------------------------- #


def test_tmpfs_target_is_unsupported_and_never_runs_the_probe(harness: Harness):
    harness.write_mountinfo(
        [
            (str(harness.root), "ext4", device_token(harness.probe)),
            (str(harness.root / "var/lib"), "tmpfs", "0:44"),
        ]
    )
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.filesystem_supported is False
    assert result.evidence.observed_bytes is None
    assert pending(harness) == {1000: True}
    assert harness.runner.runs(SYSTEMD_RUN) == []
    harness.assert_probe_file_removed()


def test_btrfs_target_is_unprovable_rather_than_assumed(harness: Harness):
    harness.write_mountinfo([(str(harness.root), "btrfs", device_token(harness.probe))])
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.filesystem_supported is False
    assert harness.runner.runs(SYSTEMD_RUN) == []


def test_missing_mount_table_is_unprovable_rather_than_assumed(harness: Harness):
    (harness.root / MOUNTINFO.relative_to("/")).unlink()
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.filesystem_supported is False
    assert harness.runner.runs(SYSTEMD_RUN) == []


def test_missing_user_bus_is_unknown_not_success(harness: Harness):
    (harness.root / RUN_USER_BUS.relative_to("/")).unlink()
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_UNKNOWN
    assert result.pending_activation is True
    assert pending(harness) == {1000: True}
    assert harness.runner.calls == []
    assert result.evidence is None


def test_missing_systemd_run_is_unknown_not_success(harness: Harness):
    (harness.root / "usr/bin/systemd-run").unlink()
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_UNKNOWN
    assert pending(harness) == {1000: True}
    assert harness.runner.calls == []


def test_missing_user_manager_cgroup_is_unknown_not_success(harness: Harness):
    shutil.rmtree(harness.root / USER_MANAGER.relative_to("/"))
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_UNKNOWN
    assert pending(harness) == {1000: True}
    assert harness.runner.calls == []


def test_unknown_state_never_clears_pending_even_without_a_state_file(harness: Harness):
    (harness.root / RUN_USER_BUS.relative_to("/")).unlink()

    result = harness.probe_now()

    assert result.state == STATE_UNKNOWN
    assert result.pending_activation is True
    assert pending(harness) == {}


# --------------------------------------------------------------------------- #
# Threat: a stale io.stat is read as this probe's evidence.
# --------------------------------------------------------------------------- #


def test_stale_io_stat_from_a_preexisting_unit_is_never_counted(harness: Harness):
    stale = harness.runner.unit_dir(UNIT)
    stale.mkdir(parents=True)
    stale_text = f"{harness.device} rbytes=0 wbytes={EXPECTED} rios=0 wios=0\n"
    (stale / "io.stat").write_text(stale_text, encoding="utf-8")
    harness.runner.write_bytes = 0
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.observed_bytes == 0
    assert harness.runner.units == [f"appio-probe-{NONCE + 1}"]
    assert (stale / "io.stat").read_text(encoding="utf-8") == stale_text


def test_every_preexisting_nonce_candidate_is_refused_without_running_anything(harness: Harness):
    for offset in range(8):
        leaf = harness.runner.unit_dir(f"appio-probe-{NONCE + offset}")
        leaf.mkdir(parents=True)
        (leaf / "io.stat").write_text(
            f"{harness.device} rbytes=0 wbytes={EXPECTED} rios=0 wios=0\n",
            encoding="utf-8",
        )
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.observed_bytes is None
    assert harness.runner.calls == []
    assert pending(harness) == {1000: True}


# --------------------------------------------------------------------------- #
# Threat: cleanup is skipped on a failure path, leaving a unit or a file behind.
# --------------------------------------------------------------------------- #


def test_cleanup_failure_keeps_pending_and_never_reports_success(harness: Harness):
    harness.runner.stop_exit = 1
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.observed_bytes == EXPECTED
    assert result.evidence.cleanup_complete is False
    assert result.pending_activation is True
    assert pending(harness) == {1000: True}
    harness.assert_probe_file_removed()


def test_cleanup_exception_still_unlinks_the_probe_file(harness: Harness):
    harness.runner.stop_raises = True
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.cleanup_complete is False
    assert pending(harness) == {1000: True}
    harness.assert_probe_file_removed()


def test_dd_error_still_cleans_up_the_unit(harness: Harness):
    harness.runner.dd_exit = 1
    seed_pending(harness)

    harness.probe_now()

    harness.assert_unit_cleaned()


def test_symlinked_probe_file_is_refused_and_reports_accounting_inactive(harness: Harness):
    seeds = harness.root / "seeds"
    seeds.mkdir()
    target = seeds / "other.bin"
    target.write_bytes(b"do not touch")
    harness.probe.unlink()
    harness.probe.symlink_to(target)
    seed_pending(harness)

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.evidence.cleanup_complete is False
    assert pending(harness) == {1000: True}
    assert target.read_bytes() == b"do not touch"


# --------------------------------------------------------------------------- #
# Regression: F-1 -- cleanup is judged by the artifact, not by an exit status.
# --------------------------------------------------------------------------- #


def test_reset_failed_nonzero_on_a_removed_unit_still_counts_as_clean(harness: Harness):
    harness.runner.reset_exit = 1
    seed_pending(harness)

    result = harness.probe_now()

    assert result.evidence.observed_bytes == EXPECTED
    assert result.evidence.cleanup_complete is True
    assert result.state == STATE_ANSWERING
    assert pending(harness) == {1000: False}
    assert not harness.runner.unit_dir(UNIT).exists()
    assert (SYSTEMCTL, "--user", "reset-failed", UNIT) in harness.runner.runs(SYSTEMCTL)


# --------------------------------------------------------------------------- #
# Regression: F-2 -- a definite non-pass re-arms pending_activation.
# --------------------------------------------------------------------------- #


def test_a_failing_probe_re_arms_pending_after_an_earlier_pass(harness: Harness):
    deps = harness.deps()
    set_pending(SafeRootFS(deps), 1000, False)
    assert pending(harness) == {1000: False}
    harness.runner.write_bytes = 0

    result = harness.probe_now()

    assert result.state == STATE_INACTIVE
    assert result.pending_activation is True
    assert pending(harness) == {1000: True}


def test_a_not_delegated_verdict_also_re_arms_pending(harness: Harness):
    deps = harness.deps()
    set_pending(SafeRootFS(deps), 1000, False)
    harness.root.joinpath(USER_MANAGER.relative_to("/"), "cgroup.controllers").write_text(
        "cpu memory pids\n", encoding="utf-8"
    )

    result = harness.probe_now()

    assert result.state == STATE_NOT_DELEGATED
    assert result.pending_activation is True
    assert pending(harness) == {1000: True}


def test_unknown_never_writes_the_pending_flag(harness: Harness):
    (harness.root / RUN_USER_BUS.relative_to("/")).unlink()

    harness.probe_now()

    assert pending(harness) == {}


def test_a_plain_oserror_on_the_re_arm_still_returns_the_measured_verdict(harness: Harness):
    harness.runner.write_bytes = 0
    events: list[str] = []
    deps = dataclasses.replace(harness.deps(), event=events.append)

    class OSErroringStateFS:
        def __init__(self, inner: SafeRootFS):
            self.inner = inner

        def atomic_write(self, path, content, *, mode=0o644, owner=None):
            if path == str(STATE_FILE):
                raise OSError(5, "Input/output error")
            return self.inner.atomic_write(path, content, mode=mode, owner=owner)

        def read_bounded(self, path, *, max_bytes):
            return self.inner.read_bounded(path, max_bytes=max_bytes)

        def locked(self):
            return self.inner.locked()

    result = probe_accounting(
        identity(), deps=deps, fs=OSErroringStateFS(SafeRootFS(deps))
    )

    assert result.state == STATE_INACTIVE
    assert result.pending_activation is True
    assert result.evidence.observed_bytes == 0
    assert result.evidence.cleanup_complete is True
    assert events[-1] == "persist-pending-failed"


def test_a_failure_to_persist_the_re_armed_flag_still_reports_pending(harness: Harness):
    harness.runner.write_bytes = 0
    events: list[str] = []
    deps = dataclasses.replace(harness.deps(), event=events.append)

    class BrokenStateFileFS:
        def __init__(self, inner: SafeRootFS):
            self.inner = inner

        def atomic_write(self, path, content, *, mode=0o644, owner=None):
            if path == str(STATE_FILE):
                raise SafeRootFSError("state_unavailable")
            return self.inner.atomic_write(path, content, mode=mode, owner=owner)

        def read_bounded(self, path, *, max_bytes):
            return self.inner.read_bounded(path, max_bytes=max_bytes)

        def locked(self):
            return self.inner.locked()

    result = probe_accounting(
        identity(), deps=deps, fs=BrokenStateFileFS(SafeRootFS(deps))
    )

    assert result.state == STATE_INACTIVE
    assert result.pending_activation is True
    # SafeRootFS traces its own file steps on the same seam.
    assert events[-1] == "persist-pending-failed"


# --------------------------------------------------------------------------- #
# Threat: the probe runs with the wrong identity, bus or argument vector.
# --------------------------------------------------------------------------- #


def test_probe_runs_as_the_validated_caller_with_a_fresh_user_bus(harness: Harness):
    harness.probe_now()

    assert harness.runner.units == [UNIT]
    assert harness.runner.runs(SYSTEMD_RUN) == [
        (
            SYSTEMD_RUN,
            "--user",
            f"--unit={UNIT}",
            "--property=Type=oneshot",
            "--property=RemainAfterExit=yes",
            "--no-block",
            DD,
            "if=/dev/zero",
            f"of={PROBE}",
            "bs=1M",
            "count=16",
            "oflag=direct",
        )
    ]
    for argv, uid, gid, env, timeout in harness.runner.calls:
        assert uid == 1000
        assert gid == 1000
        assert env == USER_BUS_ENV
        assert timeout <= 5.0
        assert argv[0].startswith("/usr/")
    assert harness.runner.calls[0][4] == 5.0


def test_probe_refuses_to_run_as_a_non_root_effective_uid(harness: Harness):
    deps = harness.deps(effective_uid=lambda: 1000)

    with pytest.raises(ActivationError):
        probe_accounting(identity(), deps=deps, fs=SafeRootFS(deps))

    assert harness.runner.calls == []
    assert harness.probe.exists()


def test_probe_file_is_pre_created_caller_owned_and_not_world_reachable(harness: Harness):
    shutil.rmtree(harness.root / "var/lib/appio/probes")
    deps = harness.deps()
    chowned: list[tuple[int, int]] = []
    deps = dataclasses.replace(deps, chown=lambda fd, uid, gid: chowned.append((uid, gid)))

    result = probe_accounting(identity(), deps=deps, fs=SafeRootFS(deps))

    assert result.state == STATE_ANSWERING
    assert chowned[0] == (1000, 1000)
    assert (1000, 1000) in chowned
    assert stat.S_IMODE(os.stat(harness.root / PROBE_ROOT.relative_to("/")).st_mode) == 0o711
    assert stat.S_IMODE(os.stat(harness.probe.parent).st_mode) == 0o711


# --------------------------------------------------------------------------- #
# Threat: the activation choice ends a session or reboots without consent.
# --------------------------------------------------------------------------- #


def test_reboot_choice_persists_pending_and_issues_no_restart(harness: Harness):
    deps = harness.deps()

    result = choose_activation("reboot", identity(), deps=deps, fs=SafeRootFS(deps))

    assert result == ActivationResult(
        pending_activation=True, state=STATE_NEEDS_ACTIVATION, evidence=None
    )
    assert pending(harness) == {1000: True}
    assert harness.runner.calls == []


def test_restart_choice_uses_the_exact_root_systemctl_unit(harness: Harness):
    deps = harness.deps()

    result = choose_activation("restart", identity(), deps=deps, fs=SafeRootFS(deps))

    assert result.state == STATE_NEEDS_ACTIVATION
    assert result.pending_activation is True
    assert [argv for argv, *_rest in harness.runner.calls] == [
        (SYSTEMCTL, "restart", "user@1000.service")
    ]
    _argv, uid, gid, _env, _timeout = harness.runner.calls[0]
    assert (uid, gid) == (0, 0)
    assert pending(harness) == {1000: True}


def test_restart_choice_persists_pending_before_the_disruptive_command(harness: Harness):
    seen: list[dict[int, bool]] = []
    deps = harness.deps()
    fs = SafeRootFS(deps)

    class ObservingRunner(ProbeRunner):
        def run(self, argv, **kwargs):
            seen.append(dict(load_setup_state(fs).pending_by_uid))
            return super().run(argv, **kwargs)

    observer = ObservingRunner()
    observer._root = harness.root
    deps = dataclasses.replace(deps, runner=observer)

    choose_activation("restart", identity(), deps=deps, fs=SafeRootFS(deps))

    assert seen == [{1000: True}]


def test_restart_failure_is_reported_and_never_clears_pending(harness: Harness):
    runner = FailingRestartRunner()
    runner._root = harness.root
    deps = dataclasses.replace(harness.deps(), runner=runner)

    with pytest.raises(ActivationError) as caught:
        choose_activation("restart", identity(), deps=deps, fs=SafeRootFS(deps))

    assert caught.value.pending_activation is True
    assert "restart-user-manager" in str(caught.value)
    assert caught.value.reason == "exit_1"
    assert pending(harness) == {1000: True}


class FailingRestartRunner(ProbeRunner):
    def run(self, argv, **kwargs):
        call = tuple(argv)
        if call[:2] == (SYSTEMCTL, "restart"):
            self.calls.append((call, kwargs.get("uid", 0), kwargs.get("gid", 0), {}, 5.0))
            return CommandResult(1, "", "Failed to restart")
        return super().run(argv, **kwargs)


def test_a_durable_state_failure_while_recording_the_choice_is_an_error(harness: Harness):
    deps = harness.deps()

    class BrokenStateFS:
        def __init__(self, inner: SafeRootFS):
            self.inner = inner

        def atomic_write(self, path, content, *, mode=0o644, owner=None):
            raise SafeRootFSError("state_unavailable")

        def read_bounded(self, path, *, max_bytes):
            return self.inner.read_bounded(path, max_bytes=max_bytes)

        def locked(self):
            return self.inner.locked()

    with pytest.raises(ActivationError) as caught:
        choose_activation("reboot", identity(), deps=deps, fs=BrokenStateFS(SafeRootFS(deps)))

    assert caught.value.operation == "persist-pending"
    assert caught.value.pending_activation is True
    assert harness.runner.calls == []


def test_unknown_choice_is_refused_without_mutating_state(harness: Harness):
    deps = harness.deps()

    with pytest.raises(ValueError):
        choose_activation("logout", identity(), deps=deps, fs=SafeRootFS(deps))

    assert harness.runner.calls == []
    assert pending(harness) == {}


# --------------------------------------------------------------------------- #
# Threat: the UI tells the user to log out, which Linger=yes makes false.
# --------------------------------------------------------------------------- #


def test_activation_wording_never_tells_the_user_to_log_out():
    source = Path(__file__).resolve().parents[2] / "src/appio/activation.py"
    text = source.read_text(encoding="utf-8").lower()
    for phrase in ("log out", "logout", "log-out", "sign out"):
        assert phrase not in text, f"activation.py advises {phrase!r}, which Linger=yes makes false"


def test_the_two_choices_name_the_session_ending_warning():
    labels = {choice: label for choice, label, _warning in ACTIVATION_CHOICES}
    assert labels == {"restart": CHOICE_RESTART_LABEL, "reboot": CHOICE_REBOOT_LABEL}
    assert CHOICE_RESTART_LABEL == "Restart my user manager now"
    assert CHOICE_REBOOT_LABEL == "Activate after reboot"
    warnings = {choice: warning for choice, _label, warning in ACTIVATION_CHOICES}
    assert warnings["restart"] == RESTART_CHOICE_WARNING
    assert warnings["reboot"] is None
    assert "session" in RESTART_CHOICE_WARNING.lower()
    assert "logout" not in RESTART_CHOICE_WARNING.lower()


def test_io_in_controllers_alone_never_clears_pending(harness: Harness):
    seed_pending(harness)
    harness.runner.write_bytes = 0

    first = harness.probe_now()
    second = harness.probe_now()

    assert first.state == STATE_INACTIVE == second.state
    assert pending(harness) == {1000: True}
    assert harness.root.joinpath(
        USER_MANAGER.relative_to("/"), "cgroup.controllers"
    ).read_text(encoding="utf-8").split().count("io") == 1


def test_writes_accounted_against_the_whole_disk_still_pass(harness: Harness):
    # Host acceptance finding H6: on this machine / is the partition 259:5 but cgroup
    # io.stat reports the probe's writes against the whole disk 259:1, so the exact
    # device comparison found no row and activation reported "accounting inactive"
    # for ever while the accounting was byte-exact (16777216 bytes, 0% deviation).
    # sysfs is the authority for the partition -> whole-disk relation.
    seed_pending(harness)
    partition = harness.device
    major = partition.split(":")[0]
    whole = f"{major}:9001"
    disk = harness.root / "sys/devices/fakedisk0"
    (disk / "fakedisk0p1").mkdir(parents=True, exist_ok=True)
    (disk / "dev").write_text(whole + "\n", encoding="ascii")
    entry = harness.root / "sys/dev/block" / partition
    entry.parent.mkdir(parents=True, exist_ok=True)
    if not entry.exists():
        entry.symlink_to(disk / "fakedisk0p1")
    harness.runner.device_override = whole

    result = harness.probe_now()

    assert result.evidence.device == partition
    assert result.evidence.observed_bytes == EXPECTED, result.evidence
    assert result.state == STATE_ANSWERING
    assert pending(harness) == {1000: False}
    harness.assert_unit_cleaned()
