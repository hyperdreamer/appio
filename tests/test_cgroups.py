"""Cgroup device-accounting tests pinned to specification sections 5.2 and 10.2.

Every test states what would falsify it, so a green run is evidence about the
contract rather than evidence that the module imports. The fixtures are static
trees under ``tests/fixtures/cgroup`` and no test reads a live ``/sys/fs/cgroup``
or ``/proc``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from appio.cgroups import (
    CgroupObservation,
    DevStat,
    DeviceDelta,
    SharedAttribution,
    attribute_shared_cgroup,
    device_deltas,
    friendly_device_name,
    parse_diskstats,
    parse_io_stat,
    read_cgroup_fixture,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cgroup"

CHROMIUM = (
    "user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-chromium@fixture.service"
)
CHROMIUM_PATH = f"/{CHROMIUM}"
PIDREUSE = "user.slice/user-1000.slice/user@1000.service/app.slice/app-pidreuse.scope"
NOIO = (
    "user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-noio@fixture.service"
)
SHARED = "user.slice/user-1000.slice/user@1000.service/app.slice/app-shared.scope"

BOOT_A = "11111111-2222-4333-8444-555555555555"
BOOT_B = "99999999-8888-4777-8666-555555555555"


def read_normal(tick: str, cgroup_id: int = 101) -> CgroupObservation:
    return read_cgroup_fixture(FIXTURES / tick, CHROMIUM, BOOT_A, cgroup_id)


def diskstats() -> dict[str, str]:
    return parse_diskstats((FIXTURES / "proc" / "diskstats").read_text(encoding="utf-8"))


# --- parse_io_stat -----------------------------------------------------------


def test_parse_io_stat_extracts_only_byte_counters() -> None:
    """Falsified if rios/wios (or dbytes/dios) leak into DevStat or change bytes."""

    text = (
        "259:0 rbytes=1000 wbytes=200 rios=10 wios=3 dbytes=7 dios=1\n"
        "259:1 rbytes=300 wbytes=500 rios=4 wios=5\n"
    )

    stats = parse_io_stat(text, 7)

    assert stats == (
        DevStat(cgroup_id=7, dev="259:0", rbytes=1000, wbytes=200),
        DevStat(cgroup_id=7, dev="259:1", rbytes=300, wbytes=500),
    )
    assert not hasattr(stats[0], "rios")
    assert not hasattr(stats[0], "dbytes")


def test_parse_io_stat_sorts_by_canonical_device_token() -> None:
    """Falsified if input order is preserved or numeric major order is used."""

    text = (
        "259:1 rbytes=1 wbytes=2 rios=0 wios=0\n"
        "8:0 rbytes=3 wbytes=4 rios=0 wios=0\n"
        "259:0 rbytes=5 wbytes=6 rios=0 wios=0\n"
    )

    stats = parse_io_stat(text, 1)

    assert tuple(stat.dev for stat in stats) == ("259:0", "259:1", "8:0")


def test_parse_io_stat_preserves_decimal_major_minor_and_cgroup_id() -> None:
    """Falsified if the token is normalized, reordered, or loses the cgroup id."""

    stats = parse_io_stat("8:16 rbytes=9 wbytes=10 rios=0 wios=0\n", 42)

    assert stats == (DevStat(cgroup_id=42, dev="8:16", rbytes=9, wbytes=10),)
    assert stats[0].cgroup_id == 42


def test_parse_io_stat_empty_text_has_no_devices() -> None:
    """Falsified if an empty io.stat synthesizes a zero-valued row."""

    assert parse_io_stat("", 1) == ()
    assert parse_io_stat("\n", 1) == ()


@pytest.mark.parametrize(
    "token",
    ["259", "259:", ":0", "259:0:1", "0x103:0", "0259:0", "+259:0", "259:-1", "nvme0n1"],
)
def test_parse_io_stat_rejects_malformed_device_token(token: str) -> None:
    """Falsified if a non-canonical token is accepted as a device identity."""

    with pytest.raises(ValueError, match="device"):
        parse_io_stat(f"{token} rbytes=1 wbytes=2 rios=0 wios=0\n", 1)


def test_parse_io_stat_rejects_duplicate_device_rows() -> None:
    """Falsified if two rows for one boot/cgroup/device could both be parsed."""

    text = (
        "259:0 rbytes=1 wbytes=2 rios=0 wios=0\n"
        "259:0 rbytes=3 wbytes=4 rios=0 wios=0\n"
    )

    with pytest.raises(ValueError, match="duplicate"):
        parse_io_stat(text, 1)


@pytest.mark.parametrize("line", ["259:0 rios=1 wios=1", "259:0 rbytes=1", "259:0 wbytes=1"])
def test_parse_io_stat_requires_both_byte_counters(line: str) -> None:
    """Falsified if a row without rbytes and wbytes is silently completed with zero."""

    with pytest.raises(ValueError):
        parse_io_stat(f"{line}\n", 1)


@pytest.mark.parametrize(
    "field",
    ["rbytes=-1", "wbytes=-5", "rbytes=9223372036854775808", "rbytes=1.5", "rbytes=abc"],
)
def test_parse_io_stat_rejects_negative_out_of_range_or_noninteger_values(field: str) -> None:
    """Falsified if a negative, floating, or out-of-SQLite-range value is stored."""

    with pytest.raises(ValueError):
        parse_io_stat(f"259:0 {field} wbytes=2\n", 1)


def test_parse_io_stat_accepts_the_sqlite_maximum() -> None:
    """Falsified if the upper bound is applied one short or coerced to floating."""

    stats = parse_io_stat("259:0 rbytes=9223372036854775807 wbytes=0\n", 1)

    assert stats[0].rbytes == 9223372036854775807
    assert isinstance(stats[0].rbytes, int)


@pytest.mark.parametrize("line", ["259:0 rbytes = 1 wbytes=2", "259:0 rbytes=1x wbytes=2"])
def test_parse_io_stat_rejects_malformed_fields(line: str) -> None:
    """Falsified if a non ``key=value`` token is ignored instead of rejected."""

    with pytest.raises(ValueError):
        parse_io_stat(f"{line}\n", 1)


def test_parse_io_stat_rejects_duplicate_field_keys() -> None:
    """Falsified if a repeated counter silently takes the last value."""

    with pytest.raises(ValueError, match="duplicate"):
        parse_io_stat("259:0 rbytes=1 wbytes=2 rbytes=3\n", 1)


# --- parse_diskstats and friendly names --------------------------------------


def test_parse_diskstats_maps_major_minor_to_display_name_only() -> None:
    """Falsified if names become keys, tokens are lost, or the map is inverted."""

    lookup = diskstats()

    assert lookup["259:0"] == "nvme0n1"
    assert lookup["259:1"] == "nvme0n1p1"
    assert lookup["8:0"] == "sda"
    assert lookup["8:1"] == "sda1"
    assert "nvme0n1" not in lookup
    assert "sda" not in lookup


def test_parse_diskstats_rejects_duplicate_tokens_and_malformed_lines() -> None:
    """Falsified if a duplicate token silently overwrites or a short line parses."""

    with pytest.raises(ValueError, match="duplicate"):
        parse_diskstats("259 0 nvme0n1 1 2 3\n259 0 nvme1n1 4 5 6\n")
    with pytest.raises(ValueError):
        parse_diskstats("259 0\n")
    with pytest.raises(ValueError):
        parse_diskstats("259 x nvme0n1 1 2 3\n")


def test_friendly_device_name_is_display_metadata_not_identity() -> None:
    """Falsified if the friendly name replaces, or becomes, the stored token key."""

    observation = read_normal("normal-t0")
    lookup = diskstats()

    assert friendly_device_name("259:0", lookup) == "nvme0n1"
    assert friendly_device_name("8:16", lookup) == "8:16"
    assert {stat.dev for stat in observation.devices} == {"259:0", "259:1"}
    assert "nvme0n1" not in {stat.dev for stat in observation.devices}


# --- read_cgroup_fixture -----------------------------------------------------


def test_read_cgroup_fixture_normal_tree_parses_all_three_ticks() -> None:
    """Falsified if a tick's bytes, device order, or controller walk is wrong."""

    t0 = read_normal("normal-t0")
    t1 = read_normal("normal-t1")
    t2 = read_normal("normal-t2")

    assert t0.boot_id == BOOT_A
    assert t0.cgroup_id == 101
    assert t0.cgroup_path == CHROMIUM_PATH
    assert t0.device_state == "available"
    assert t0.devices == (
        DevStat(101, "259:0", 1000, 200),
        DevStat(101, "259:1", 300, 500),
    )
    # t0 and t1 hold the same cumulative io.stat, so their observations are equal.
    assert t1.devices == t0.devices
    assert t2.devices == (
        DevStat(101, "259:0", 1120, 270),
        DevStat(101, "259:1", 350, 550),
    )
    # cgroup.controllers lives at the hierarchy root, not on the leaf: the reader
    # must find the nearest ancestor file, as cgroupfs does.
    assert not (FIXTURES / "normal-t0" / CHROMIUM / "cgroup.controllers").exists()
    assert (FIXTURES / "normal-t0" / "cgroup.controllers").is_file()


def test_read_cgroup_fixture_accepts_an_absolute_cgroup_path() -> None:
    """Falsified if passing the real absolute cgroup path cannot find the fixture."""

    relative = read_cgroup_fixture(FIXTURES / "normal-t2", CHROMIUM, BOOT_A, 101)
    absolute = read_cgroup_fixture(FIXTURES / "normal-t2", CHROMIUM_PATH, BOOT_A, 101)

    assert absolute == relative
    assert absolute.cgroup_path == CHROMIUM_PATH


def test_read_cgroup_fixture_absent_io_controller_is_not_delegated() -> None:
    """Falsified if a missing io controller is reported as available/incomplete,
    or if zero-valued device rows are synthesized for it."""

    observation = read_cgroup_fixture(FIXTURES / "controller-absent", NOIO, BOOT_A, 55)

    assert observation.device_state == "not_delegated"
    assert observation.devices == ()
    assert not (FIXTURES / "controller-absent" / NOIO / "io.stat").exists()


def test_read_cgroup_fixture_missing_io_stat_with_controller_is_incomplete(
    tmp_path: Path,
) -> None:
    """Falsified if an absent io.stat under a delegated controller claims available
    or raises instead of marking device attribution incomplete (section 4.3)."""

    (tmp_path / "cgroup.controllers").write_text("cpu io memory pids\n", encoding="utf-8")
    leaf = tmp_path / "app.slice" / "app-x.scope"
    leaf.mkdir(parents=True)
    (leaf / "cgroup.procs").write_text("1\n", encoding="utf-8")

    observation = read_cgroup_fixture(tmp_path, "app.slice/app-x.scope", BOOT_A, 3)

    assert observation.device_state == "incomplete"
    assert observation.devices == ()


def test_read_cgroup_fixture_missing_leaf_raises() -> None:
    """Falsified if a mistyped cgroup path silently yields a fabricated observation."""

    with pytest.raises(FileNotFoundError):
        read_cgroup_fixture(FIXTURES / "normal-t0", "user.slice/app-typo.scope", BOOT_A, 1)


def test_read_cgroup_fixture_rejects_an_escaping_relative_path() -> None:
    """Falsified if ``..`` can read a sibling fixture outside the named root."""

    with pytest.raises(ValueError):
        read_cgroup_fixture(FIXTURES / "normal-t0", "../normal-t2/" + CHROMIUM, BOOT_A, 1)


# --- device_deltas -----------------------------------------------------------


def test_first_observation_emits_zero_delta_and_seeds_the_baseline() -> None:
    """Falsified if the first sighting emits cumulative bytes, or seeds nothing."""

    baselines: dict[tuple[str, int, str], tuple[int, int]] = {}

    deltas = device_deltas(BOOT_A, (read_normal("normal-t0"),), baselines)

    assert deltas == (
        DeviceDelta(101, "259:0", 0, 0, reset=False),
        DeviceDelta(101, "259:1", 0, 0, reset=False),
    )
    assert baselines == {
        (BOOT_A, 101, "259:0"): (1000, 200),
        (BOOT_A, 101, "259:1"): (300, 500),
    }


def test_three_tick_deltas_match_the_acceptance_vectors() -> None:
    """Falsified if t0..t2 D is not (170,120) or bytes are counted twice."""

    baselines: dict[tuple[str, int, str], tuple[int, int]] = {}
    emitted: list[DeviceDelta] = []
    for tick in ("normal-t0", "normal-t1", "normal-t2"):
        emitted.extend(device_deltas(BOOT_A, (read_normal(tick),), baselines))

    by_device: dict[str, list[DeviceDelta]] = {}
    for delta in emitted:
        by_device.setdefault(delta.dev, []).append(delta)

    assert [(d.rbytes, d.wbytes) for d in by_device["259:0"]] == [(0, 0), (0, 0), (120, 70)]
    assert [(d.rbytes, d.wbytes) for d in by_device["259:1"]] == [(0, 0), (0, 0), (50, 50)]
    assert sum(d.rbytes for d in emitted) == 170
    assert sum(d.wbytes for d in emitted) == 120


def test_cgroup_recreation_starts_a_fresh_baseline_without_transfer() -> None:
    """Falsified if a baseline keyed by cgroup/dev is reused for another cgroup id."""

    baselines: dict[tuple[str, int, str], tuple[int, int]] = {}
    device_deltas(BOOT_A, (read_normal("normal-t2", cgroup_id=101),), baselines)

    recreated = read_normal("normal-t2", cgroup_id=202)
    deltas = device_deltas(BOOT_A, (recreated,), baselines)

    assert deltas == (
        DeviceDelta(202, "259:0", 0, 0, reset=False),
        DeviceDelta(202, "259:1", 0, 0, reset=False),
    )
    assert baselines[(BOOT_A, 101, "259:0")] == (1120, 270)
    assert baselines[(BOOT_A, 202, "259:0")] == (1120, 270)


def test_decreasing_field_emits_zero_and_resets_only_that_field() -> None:
    """Falsified if a decrease emits negative bytes, resets both fields, or
    measures the next tick from the pre-reset baseline."""

    baselines = {(BOOT_A, 101, "259:0"): (1120, 270)}
    decreasing = CgroupObservation(
        boot_id=BOOT_A,
        cgroup_id=101,
        cgroup_path="/user.slice/app-x.scope",
        device_state="available",
        devices=(DevStat(101, "259:0", 1000, 300),),
    )

    first = device_deltas(BOOT_A, (decreasing,), baselines)

    assert first == (DeviceDelta(101, "259:0", 0, 30, reset=True),)
    assert baselines[(BOOT_A, 101, "259:0")] == (1000, 300)

    next_tick = CgroupObservation(
        boot_id=BOOT_A,
        cgroup_id=101,
        cgroup_path="/user.slice/app-x.scope",
        device_state="available",
        devices=(DevStat(101, "259:0", 1050, 320),),
    )
    second = device_deltas(BOOT_A, (next_tick,), baselines)

    assert second == (DeviceDelta(101, "259:0", 50, 20, reset=False),)
    assert baselines[(BOOT_A, 101, "259:0")] == (1050, 320)


def test_baseline_never_transfers_across_boot_ids() -> None:
    """Falsified if the key omits the boot id and a new boot inherits old bytes."""

    baselines = {(BOOT_A, 101, "259:0"): (1120, 270)}
    new_boot = CgroupObservation(
        boot_id=BOOT_B,
        cgroup_id=101,
        cgroup_path="/user.slice/app-x.scope",
        device_state="available",
        devices=(DevStat(101, "259:0", 40, 5),),
    )

    deltas = device_deltas(BOOT_B, (new_boot,), baselines)

    assert deltas == (DeviceDelta(101, "259:0", 0, 0, reset=False),)
    assert baselines[(BOOT_B, 101, "259:0")] == (40, 5)
    assert baselines[(BOOT_A, 101, "259:0")] == (1120, 270)


def test_device_deltas_rejects_observations_from_another_boot() -> None:
    """Falsified if a mistyped frame boot is silently keyed under the wrong boot."""

    baselines: dict[tuple[str, int, str], tuple[int, int]] = {}

    with pytest.raises(ValueError, match="boot"):
        device_deltas(BOOT_B, (read_normal("normal-t0"),), baselines)

    assert baselines == {}


def test_not_delegated_observation_yields_no_delta_and_no_baseline() -> None:
    """Falsified if not_delegated is treated as a zero-byte device row."""

    baselines: dict[tuple[str, int, str], tuple[int, int]] = {}
    observation = read_cgroup_fixture(FIXTURES / "controller-absent", NOIO, BOOT_A, 55)

    assert device_deltas(BOOT_A, (observation,), baselines) == ()
    assert baselines == {}


# --- shared-owner attribution (A19) -----------------------------------------


def test_same_app_shared_cgroup_is_attributed_exactly_once() -> None:
    """Falsified if D is duplicated per PID, divided, or emitted for no owner."""

    observation = read_cgroup_fixture(FIXTURES / "shared-same-app", SHARED, BOOT_A, 7)
    owners = {7: ("unit:app-shared.scope", "unit:app-shared.scope")}

    result = attribute_shared_cgroup(observation, owners)

    assert result == SharedAttribution(
        contributions=(
            ("unit:app-shared.scope", DeviceDelta(7, "259:0", 40, 5, reset=False)),
        ),
        incomplete_app_keys=(),
    )
    assert len(result.contributions) == 1


def test_distinct_apps_sharing_a_cgroup_emit_no_row_and_are_incomplete() -> None:
    """Falsified if either app receives (part of) the (40,5) delta, or an owner is
    left out of the incomplete set."""

    observation = read_cgroup_fixture(
        FIXTURES / "shared-distinct-app", "fixture-shared", BOOT_A, 9
    )
    owners = {
        9: ("exe:/usr/bin/tool-a", "exe:/usr/bin/tool-b"),
    }

    result = attribute_shared_cgroup(observation, owners)

    assert result.contributions == ()
    assert result.incomplete_app_keys == (
        "exe:/usr/bin/tool-a",
        "exe:/usr/bin/tool-b",
    )


def test_unowned_or_not_delegated_cgroup_attributes_nothing() -> None:
    """Falsified if bytes are attributed without an owner, or a controller-absent
    cgroup marks its owners incomplete despite having no D to assign."""

    with_devices = read_cgroup_fixture(FIXTURES / "shared-same-app", SHARED, BOOT_A, 7)
    not_delegated = read_cgroup_fixture(FIXTURES / "controller-absent", NOIO, BOOT_A, 55)

    assert attribute_shared_cgroup(with_devices, {}) == SharedAttribution((), ())
    assert attribute_shared_cgroup(
        not_delegated, {55: ("unit:app-noio@fixture.service", "exe:/usr/bin/tool")}
    ) == SharedAttribution((), ())


# --- value-object invariants -------------------------------------------------


def test_cgroup_observation_rejects_device_rows_under_not_delegated() -> None:
    """Falsified if a zero-valued row can be represented as not_delegated, which
    section 10.2 forbids synthesizing."""

    with pytest.raises(ValueError, match="not_delegated"):
        CgroupObservation(
            boot_id=BOOT_A,
            cgroup_id=1,
            cgroup_path="/user.slice/app-x.scope",
            device_state="not_delegated",
            devices=(DevStat(1, "259:0", 0, 0),),
        )


def test_cgroup_observation_rejects_foreign_or_duplicate_devices() -> None:
    """Falsified if an observation can mix cgroup identities or repeat a device."""

    with pytest.raises(ValueError, match="cgroup_id"):
        CgroupObservation(
            boot_id=BOOT_A,
            cgroup_id=1,
            cgroup_path="/user.slice/app-x.scope",
            device_state="available",
            devices=(DevStat(2, "259:0", 0, 0),),
        )
    with pytest.raises(ValueError, match="duplicate"):
        CgroupObservation(
            boot_id=BOOT_A,
            cgroup_id=1,
            cgroup_path="/user.slice/app-x.scope",
            device_state="available",
            devices=(DevStat(1, "259:0", 1, 2), DevStat(1, "259:0", 3, 4)),
        )


def test_cgroup_observation_rejects_a_relative_cgroup_path() -> None:
    """Falsified if a relative path can stand in for cgroup identity."""

    with pytest.raises(ValueError):
        CgroupObservation(
            boot_id=BOOT_A,
            cgroup_id=1,
            cgroup_path="user.slice/app-x.scope",
            device_state="available",
            devices=(),
        )


def test_device_delta_rejects_a_non_boolean_reset_flag() -> None:
    """Falsified if an integer or string can masquerade as reset metadata."""

    with pytest.raises(ValueError):
        DeviceDelta(1, "259:0", 0, 0, reset=1)  # type: ignore[arg-type]


# --- fixture fidelity --------------------------------------------------------


def test_fixture_membership_matches_specification_section_10_2() -> None:
    """Falsified if a later fixture edit silently drifts from the pinned vectors."""

    assert (FIXTURES / "normal-t0" / CHROMIUM / "cgroup.procs").read_bytes() == (
        b"410\n411\n412\n413\n"
    )
    assert (FIXTURES / "normal-t2" / CHROMIUM / "cgroup.procs").read_bytes() == (
        b"410\n412\n413\n"
    )
    assert (FIXTURES / "normal-t0" / PIDREUSE / "cgroup.procs").read_bytes() == b"500\n"
    assert (
        FIXTURES / "shared-same-app" / SHARED / "cgroup.procs"
    ).read_bytes() == b"420\n421\n"
    assert (
        FIXTURES / "shared-distinct-app" / "fixture-shared" / "cgroup.procs"
    ).read_bytes() == b"420\n421\n"
    assert (
        FIXTURES / "controller-absent" / NOIO / "cgroup.procs"
    ).read_bytes() == b"600\n"
