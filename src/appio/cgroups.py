"""Cgroup-v2 device accounting for P1.

``D`` is the nonnegative delta of the ``rbytes``/``wbytes`` counters in cgroup-v2
``io.stat``, grouped by app and device (specification section 2.2). Three rules
shape this module, all ratified in section 5.2:

* the canonical device identity is the decimal ``major:minor`` token from
  ``io.stat``; a friendly ``/proc/diskstats`` name is display metadata and is
  never a key or an identity;
* device baselines are keyed only by ``(boot_id, cgroup_id, dev)``. A first
  observation or a recreated cgroup establishes a fresh baseline and emits zero,
  and a decrease emits zero for that field while resetting only that field
  (A05);
* distinct ``AppKey`` values sharing one cgroup have no approved unique owner, so
  their device bytes are neither duplicated nor divided; no per-app row is
  emitted and every affected app is marked incomplete (A19).

The module is fixture-driven and pure: no reader touches a live ``/sys/fs/cgroup``
or ``/proc``. Baselines are caller-owned state, so :func:`device_deltas` mutates
the mapping it is given rather than hiding persistence inside this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .contracts import (
    AppKey,
    DeviceState,
    validate_enum,
    validate_sqlite_integer,
)

__all__ = [
    "CgroupObservation",
    "DevStat",
    "DeviceBaseline",
    "DeviceBaselineKey",
    "DeviceDelta",
    "SharedAttribution",
    "attribute_shared_cgroup",
    "device_deltas",
    "friendly_device_name",
    "parse_diskstats",
    "parse_io_stat",
    "read_cgroup_fixture",
]

_DEVICE_STATES: tuple[str, ...] = ("available", "not_delegated", "incomplete")

#: Kernel ``io.stat`` prints decimal ``major:minor`` without leading zeros; the
#: canonical form is therefore the only accepted device token.
_CANONICAL_DEVICE = re.compile(r"^(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)$")
_IO_STAT_FIELD = re.compile(r"^(?P<key>[a-z][a-z0-9_]*)=(?P<value>-?[0-9]+)$")

#: ``(boot_id, cgroup_id, dev)`` -- the A05 baseline key. The boot id is part of
#: the key so a baseline can never transfer across boots.
DeviceBaselineKey = tuple[str, int, str]

#: Stored cumulative ``(rbytes, wbytes)`` for one :data:`DeviceBaselineKey`.
DeviceBaseline = tuple[int, int]


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL")
    return value


def _require_device_token(value: object, field: str) -> str:
    if not isinstance(value, str) or _CANONICAL_DEVICE.fullmatch(value) is None:
        raise ValueError(
            f"{field} must be a canonical decimal major:minor token, got {value!r}"
        )
    return value


def _require_absolute_cgroup_path(value: object, field: str) -> str:
    text = _require_text(value, field)
    if not text.startswith("/"):
        raise ValueError(f"{field} must be an absolute cgroup path, got {text!r}")
    return text


def _require_app_key(value: object, field: str) -> AppKey:
    return _require_text(value, field)


@dataclass(frozen=True)
class DevStat:
    """One device's cumulative ``io.stat`` counters for one cgroup."""

    cgroup_id: int
    dev: str
    rbytes: int
    wbytes: int

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.cgroup_id, "dev_stat.cgroup_id")
        _require_device_token(self.dev, "dev_stat.dev")
        validate_sqlite_integer(self.rbytes, "dev_stat.rbytes")
        validate_sqlite_integer(self.wbytes, "dev_stat.wbytes")


@dataclass(frozen=True)
class CgroupObservation:
    """One cgroup's device-accounting state at one tick.

    ``device_state`` is explicit rather than inferred from an empty tuple: a
    controller-absent cgroup (``not_delegated``) has no D at all, while an absent
    ``io.stat`` under a delegated controller is ``incomplete`` because bytes may
    exist but were not observed. A ``not_delegated`` observation may not carry
    device rows: synthesizing a zero-valued row would claim an exact zero D that
    was never measured.
    """

    boot_id: str
    cgroup_id: int
    cgroup_path: str
    device_state: DeviceState
    devices: tuple[DevStat, ...]

    def __post_init__(self) -> None:
        _require_text(self.boot_id, "cgroup_observation.boot_id")
        validate_sqlite_integer(self.cgroup_id, "cgroup_observation.cgroup_id")
        _require_absolute_cgroup_path(self.cgroup_path, "cgroup_observation.cgroup_path")
        validate_enum(self.device_state, _DEVICE_STATES, "cgroup_observation.device_state")
        if not isinstance(self.devices, tuple):
            raise ValueError("cgroup_observation.devices must be a tuple")
        seen: set[str] = set()
        for index, stat in enumerate(self.devices):
            if not isinstance(stat, DevStat):
                raise ValueError(
                    f"cgroup_observation.devices[{index}] must be a DevStat"
                )
            if stat.cgroup_id != self.cgroup_id:
                raise ValueError(
                    "cgroup_observation.devices"
                    f"[{index}].cgroup_id {stat.cgroup_id} does not match "
                    f"cgroup_observation.cgroup_id {self.cgroup_id}"
                )
            if stat.dev in seen:
                raise ValueError(
                    f"cgroup_observation.devices has a duplicate device {stat.dev}; "
                    "one cgroup/device pair may appear once"
                )
            seen.add(stat.dev)
        if self.device_state == "not_delegated" and self.devices:
            raise ValueError(
                "a not_delegated cgroup_observation must not synthesize device rows"
            )


@dataclass(frozen=True)
class DeviceDelta:
    """One nonnegative D contribution for ``(cgroup_id, dev)``.

    ``reset`` is metadata, not a byte value: it is true when at least one field
    decreased, which lets the sampler count a reset without emitting a negative
    D. A first observation is a baseline, not a reset, so it reports ``False``.
    """

    cgroup_id: int
    dev: str
    rbytes: int
    wbytes: int
    reset: bool

    def __post_init__(self) -> None:
        validate_sqlite_integer(self.cgroup_id, "device_delta.cgroup_id")
        _require_device_token(self.dev, "device_delta.dev")
        validate_sqlite_integer(self.rbytes, "device_delta.rbytes")
        validate_sqlite_integer(self.wbytes, "device_delta.wbytes")
        if not isinstance(self.reset, bool):
            raise ValueError("device_delta.reset must be a boolean")


@dataclass(frozen=True)
class SharedAttribution:
    """The A19 outcome for one shared cgroup.

    ``contributions`` pairs exactly one unique owner with each device row when the
    cgroup has a unique owner. ``incomplete_app_keys`` lists every distinct owner
    when the cgroup has more than one, in which case no bytes are attributed.
    """

    contributions: tuple[tuple[AppKey, DeviceDelta], ...]
    incomplete_app_keys: tuple[AppKey, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.contributions, tuple):
            raise ValueError("shared_attribution.contributions must be a tuple")
        if not isinstance(self.incomplete_app_keys, tuple):
            raise ValueError("shared_attribution.incomplete_app_keys must be a tuple")
        attributed: set[str] = set()
        seen: set[tuple[str, str]] = set()
        for index, entry in enumerate(self.contributions):
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError(
                    "shared_attribution.contributions"
                    f"[{index}] must be (app_key, DeviceDelta)"
                )
            app_key, delta = entry
            _require_app_key(app_key, f"shared_attribution.contributions[{index}].app_key")
            if not isinstance(delta, DeviceDelta):
                raise ValueError(
                    "shared_attribution.contributions"
                    f"[{index}][1] must be a DeviceDelta"
                )
            key = (app_key, delta.dev)
            if key in seen:
                raise ValueError(
                    f"shared_attribution.contributions repeats ({app_key}, {delta.dev})"
                )
            seen.add(key)
            attributed.add(app_key)
        for index, app_key in enumerate(self.incomplete_app_keys):
            _require_app_key(app_key, f"shared_attribution.incomplete_app_keys[{index}]")
            if app_key in attributed:
                raise ValueError(
                    f"{app_key} cannot be both attributed and incomplete"
                )


def parse_io_stat(text: str, cgroup_id: int) -> tuple[DevStat, ...]:
    """Parse cgroup-v2 ``io.stat`` into device rows sorted by device token.

    Only ``rbytes`` and ``wbytes`` are extracted; ``rios``/``wios`` and any future
    kernel counters are ignored. Both byte counters are required on every line so
    a partial row can never be completed with an invented zero. Rows are ordered
    bytewise by the canonical token, so input order cannot leak into storage.
    """

    validate_sqlite_integer(cgroup_id, "parse_io_stat.cgroup_id")
    if not isinstance(text, str):
        raise ValueError("io.stat text must be a string")

    stats: list[DevStat] = []
    seen: set[str] = set()
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line == "":
            continue
        tokens = line.split()
        dev = _require_device_token(tokens[0], f"io.stat line {number} device")
        if dev in seen:
            raise ValueError(
                f"io.stat line {number} has a duplicate device {dev}; one "
                "boot/cgroup/device pair may appear once"
            )
        seen.add(dev)

        fields: dict[str, int] = {}
        for token in tokens[1:]:
            match = _IO_STAT_FIELD.fullmatch(token)
            if match is None:
                raise ValueError(
                    f"io.stat line {number} has a malformed field {token!r}; "
                    "expected key=integer"
                )
            key = match["key"]
            if key in fields:
                raise ValueError(
                    f"io.stat line {number} has a duplicate field {key!r} for device {dev}"
                )
            fields[key] = int(match["value"])

        for required in ("rbytes", "wbytes"):
            if required not in fields:
                raise ValueError(
                    f"io.stat line {number} for device {dev} is missing {required}"
                )

        stats.append(
            DevStat(
                cgroup_id=cgroup_id,
                dev=dev,
                rbytes=validate_sqlite_integer(
                    fields["rbytes"], f"io.stat line {number} rbytes"
                ),
                wbytes=validate_sqlite_integer(
                    fields["wbytes"], f"io.stat line {number} wbytes"
                ),
            )
        )

    return tuple(sorted(stats, key=lambda stat: stat.dev))


def parse_diskstats(text: str) -> dict[str, str]:
    """Map ``/proc/diskstats`` ``major:minor`` tokens to display names only.

    The returned mapping is display metadata. It is never a device identity: equal
    names for different tokens stay separate entries, and nothing here merges
    devices across boots.
    """

    if not isinstance(text, str):
        raise ValueError("diskstats text must be a string")

    lookup: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line == "":
            continue
        fields = line.split()
        if len(fields) < 3:
            raise ValueError(
                f"diskstats line {number} has {len(fields)} field(s); "
                "expected at least major minor name"
            )
        token = _require_device_token(
            f"{fields[0]}:{fields[1]}", f"diskstats line {number} device"
        )
        if token in lookup:
            raise ValueError(f"diskstats line {number} has a duplicate device {token}")
        lookup[token] = fields[2]
    return lookup


def friendly_device_name(dev: str, diskstats: Mapping[str, str]) -> str:
    """Return the display name for ``dev``, or the token itself when unknown.

    The fallback is the canonical token rather than an empty string or a guessed
    name, so a display surface always has a truthful label.
    """

    token = _require_device_token(dev, "friendly_device_name.dev")
    if not isinstance(diskstats, Mapping):
        raise ValueError("diskstats must be a mapping of major:minor to name")
    if token not in diskstats:
        return token
    name = diskstats[token]
    if not isinstance(name, str) or name == "":
        raise ValueError(f"diskstats[{token!r}] must be a non-empty display name")
    return name


def read_cgroup_fixture(
    root: str | Path,
    relative_path: str | PurePosixPath,
    boot_id: str,
    cgroup_id: int,
) -> CgroupObservation:
    """Read one cgroup from a fixture tree rooted at ``root``.

    ``cgroup.controllers`` is looked up on the leaf and then on each ancestor up
    to ``root``, mirroring cgroupfs where the file lives at the hierarchy root.
    ``io`` absent from that file is an explicit ``not_delegated`` state with no
    device rows. With ``io`` delegated but ``io.stat`` absent, the state is
    ``incomplete``: bytes may exist but were not observed, and section 4.3 forbids
    claiming coverage from "no source rows". A malformed ``io.stat`` raises.
    """

    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"cgroup fixture root does not exist: {root_path}")

    relative = _normalize_relative_path(relative_path)
    leaf = root_path.joinpath(*PurePosixPath(relative).parts) if relative else root_path
    resolved_root = root_path.resolve()
    if resolved_root not in (leaf.resolve(), *leaf.resolve().parents):
        raise ValueError(f"cgroup path escapes the fixture root: {relative_path!r}")
    if not leaf.is_dir():
        raise FileNotFoundError(f"no cgroup fixture directory at {leaf}")

    controllers = _find_controllers_file(leaf, root_path)
    if controllers is None:
        raise FileNotFoundError(
            f"no cgroup.controllers at or above {leaf} within {root_path}"
        )
    controller_names = controllers.read_text(encoding="utf-8").split()

    cgroup_path = f"/{relative}" if relative else "/"
    if "io" not in controller_names:
        return CgroupObservation(
            boot_id=boot_id,
            cgroup_id=cgroup_id,
            cgroup_path=cgroup_path,
            device_state="not_delegated",
            devices=(),
        )

    io_stat_path = leaf / "io.stat"
    if not io_stat_path.is_file():
        return CgroupObservation(
            boot_id=boot_id,
            cgroup_id=cgroup_id,
            cgroup_path=cgroup_path,
            device_state="incomplete",
            devices=(),
        )

    return CgroupObservation(
        boot_id=boot_id,
        cgroup_id=cgroup_id,
        cgroup_path=cgroup_path,
        device_state="available",
        devices=parse_io_stat(io_stat_path.read_text(encoding="utf-8"), cgroup_id),
    )


def _normalize_relative_path(value: str | PurePosixPath) -> str:
    """Normalize a fixture-relative cgroup path, rejecting escapes and NUL."""

    if not isinstance(value, (str, PurePosixPath)):
        raise ValueError(
            "relative_path must be a string or PurePosixPath, "
            f"got {type(value).__name__}"
        )
    text = str(value)
    if "\x00" in text:
        raise ValueError("relative_path must not contain NUL")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"relative_path must not contain '..': {text!r}")
    return "/".join(parts)


def _find_controllers_file(leaf: Path, root: Path) -> Path | None:
    """Return the nearest ``cgroup.controllers`` between ``leaf`` and ``root``."""

    current = leaf
    while True:
        candidate = current / "cgroup.controllers"
        if candidate.is_file():
            return candidate
        if current == root:
            return None
        current = current.parent


def _baseline_value(value: object, key: DeviceBaselineKey) -> DeviceBaseline:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError(f"baseline for {key} must be an (rbytes, wbytes) tuple")
    rbytes = validate_sqlite_integer(value[0], f"baseline {key} rbytes")
    wbytes = validate_sqlite_integer(value[1], f"baseline {key} wbytes")
    return (rbytes, wbytes)


def device_deltas(
    boot_id: str,
    observations: Iterable[CgroupObservation],
    baselines: MutableMapping[DeviceBaselineKey, DeviceBaseline],
) -> tuple[DeviceDelta, ...]:
    """Apply A05 component-wise baseline deltas for one boot.

    ``baselines`` is updated in place under ``(boot_id, cgroup_id, dev)``; the
    caller owns persistence. A missing key is a first observation (or a recreated
    cgroup) and emits ``(0, 0)`` while seeding the fresh baseline. A decrease in
    one field emits zero for that field only, resets that field's baseline, and
    sets ``reset=True``; the other field still emits its ordinary delta. Output is
    ordered by ``(cgroup_id, dev)`` so observation order cannot reorder rows.

    Every observation must belong to ``boot_id``; a mixed-boot batch is rejected
    rather than silently keyed under the wrong boot. ``not_delegated`` and
    ``incomplete`` observations carry no device rows and therefore emit nothing.
    """

    _require_text(boot_id, "device_deltas.boot_id")
    if not isinstance(baselines, MutableMapping):
        raise ValueError("device_deltas requires a mutable baseline mapping")

    deltas: list[DeviceDelta] = []
    seen: set[tuple[int, str]] = set()
    for observation in observations:
        if not isinstance(observation, CgroupObservation):
            raise ValueError("device_deltas observations must be CgroupObservation values")
        if observation.boot_id != boot_id:
            raise ValueError(
                f"observation boot_id {observation.boot_id!r} differs from the "
                f"batch boot_id {boot_id!r}; a baseline never transfers across boots"
            )
        for stat in observation.devices:
            identity = (observation.cgroup_id, stat.dev)
            if identity in seen:
                raise ValueError(
                    f"device_deltas repeats cgroup/device {identity}; D is counted "
                    "once per cgroup and device, never once per process"
                )
            seen.add(identity)

            key: DeviceBaselineKey = (boot_id, observation.cgroup_id, stat.dev)
            if key not in baselines:
                baselines[key] = (stat.rbytes, stat.wbytes)
                deltas.append(
                    DeviceDelta(observation.cgroup_id, stat.dev, 0, 0, reset=False)
                )
                continue

            previous_rbytes, previous_wbytes = _baseline_value(baselines[key], key)
            reset = False
            if stat.rbytes < previous_rbytes:
                delta_rbytes = 0
                previous_rbytes = stat.rbytes
                reset = True
            else:
                delta_rbytes = stat.rbytes - previous_rbytes
                previous_rbytes = stat.rbytes
            if stat.wbytes < previous_wbytes:
                delta_wbytes = 0
                previous_wbytes = stat.wbytes
                reset = True
            else:
                delta_wbytes = stat.wbytes - previous_wbytes
                previous_wbytes = stat.wbytes
            baselines[key] = (previous_rbytes, previous_wbytes)
            deltas.append(
                DeviceDelta(
                    observation.cgroup_id,
                    stat.dev,
                    delta_rbytes,
                    delta_wbytes,
                    reset=reset,
                )
            )

    return tuple(sorted(deltas, key=lambda delta: (delta.cgroup_id, delta.dev)))


def attribute_shared_cgroup(
    observation: CgroupObservation,
    owners_by_cgroup: Mapping[int, Iterable[AppKey]],
) -> SharedAttribution:
    """Apply A19 to one cgroup observation.

    ``observation.devices`` holds the contribution values to attribute: callers
    pass the per-tick D observation produced after :func:`device_deltas` applied the
    baseline once, never a raw cumulative ``io.stat`` row, so A05 and A19 compose
    without double counting. A unique owner receives each device row exactly once,
    so a cgroup with two PIDs of the same app still yields one contribution. Two
    or more distinct owners yield no per-app row at all and list every owner as
    incomplete; the bytes are never divided or duplicated and no synthetic owner
    is invented. A cgroup with no known owner, or one whose observation carries no
    device rows, attributes nothing and marks nobody incomplete.
    """

    if not isinstance(observation, CgroupObservation):
        raise ValueError("attribute_shared_cgroup requires a CgroupObservation")
    if not isinstance(owners_by_cgroup, Mapping):
        raise ValueError("owners_by_cgroup must be a mapping of cgroup_id to AppKeys")
    if observation.devices == ():
        return SharedAttribution(contributions=(), incomplete_app_keys=())

    raw_owners = owners_by_cgroup.get(observation.cgroup_id, ())
    if isinstance(raw_owners, str) or not isinstance(raw_owners, Iterable):
        raise ValueError(
            f"owners_by_cgroup[{observation.cgroup_id}] must be an iterable of AppKeys"
        )
    owners = sorted(
        {
            _require_app_key(owner, f"owners_by_cgroup[{observation.cgroup_id}] entry")
            for owner in raw_owners
        }
    )
    if not owners:
        return SharedAttribution(contributions=(), incomplete_app_keys=())

    if len(owners) > 1:
        return SharedAttribution(
            contributions=(), incomplete_app_keys=tuple(owners)
        )

    owner = owners[0]
    contributions = tuple(
        (owner, DeviceDelta(observation.cgroup_id, stat.dev, stat.rbytes, stat.wbytes, reset=False))
        for stat in observation.devices
    )
    return SharedAttribution(contributions=contributions, incomplete_app_keys=())
