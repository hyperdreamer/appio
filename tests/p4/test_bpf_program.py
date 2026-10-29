from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BPF_DIR = ROOT / "appio_iod" / "bpf"
BPF_SOURCE = BPF_DIR / "fileio.bpf.c"
LOADER_SOURCE = BPF_DIR / "fileio_loader.c"


@pytest.fixture
def bpf_source() -> str:
    assert BPF_SOURCE.is_file(), f"missing CO-RE producer: {BPF_SOURCE}"
    return BPF_SOURCE.read_text(encoding="utf-8")


@pytest.fixture
def loader_source() -> str:
    assert LOADER_SOURCE.is_file(), f"missing loader: {LOADER_SOURCE}"
    return LOADER_SOURCE.read_text(encoding="utf-8")


def _function_body(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unterminated function body for {marker}")


def test_bpf_program_declares_exactly_six_vfs_fexit_hooks(bpf_source: str) -> None:
    expected = {
        "vfs_read",
        "vfs_write",
        "vfs_readv",
        "vfs_writev",
        "vfs_iter_read",
        "vfs_iter_write",
    }
    sections = re.findall(r'SEC\("fexit/([^"]+)"\)', bpf_source)
    assert len(sections) == 6
    assert set(sections) == expected


def test_bpf_program_guards_positive_returns_before_counting(bpf_source: str) -> None:
    helper = _function_body(bpf_source, "fileio_account")
    guard = re.search(r"if\s*\(\s*ret\s*<=\s*0\s*\)", helper)
    assert guard is not None
    first_count = min(
        (index for index in (helper.find("hook_hits"), helper.find("bpf_map_update_elem")) if index >= 0),
        default=-1,
    )
    assert first_count >= 0
    assert guard.start() < first_count


def test_bpf_program_filters_regular_block_backed_files(bpf_source: str) -> None:
    assert re.search(r"S_ISREG\s*\(", bpf_source)
    assert re.search(r"s_bdev[\s\S]{0,80}NULL|NULL[\s\S]{0,80}s_bdev", bpf_source)
    assert "skipped_nonfile" in bpf_source
    assert "skipped_unsupported" in bpf_source


def test_bpf_program_uses_current_cgroup_and_four_part_generation_key(bpf_source: str) -> None:
    assert "bpf_get_current_cgroup_id" in bpf_source
    key_match = re.search(r"struct\s+fileio_key\s*\{(?P<body>.*?)\};", bpf_source, re.DOTALL)
    assert key_match is not None
    key_body = key_match.group("body")
    for field in ("cgroup_id", "device", "inode", "i_generation"):
        assert re.search(rf"\b{field}\b", key_body)
    assert key_body.count(";") >= 4


def test_bpf_program_has_bounded_basename_and_length(bpf_source: str) -> None:
    value_match = re.search(r"struct\s+fileio_value\s*\{(?P<body>.*?)\};", bpf_source, re.DOTALL)
    assert value_match is not None
    value_body = value_match.group("body")
    assert re.search(r"name\s*\[\s*40\s*\]", value_body)
    assert re.search(r"\bname_len\b", value_body)


def test_bpf_program_has_two_hash_slots_behind_array_of_maps(bpf_source: str) -> None:
    assert bpf_source.count("BPF_MAP_TYPE_HASH") >= 2
    assert "__uint(max_entries, 262144)" in bpf_source
    assert "struct fileio_event_map" in bpf_source
    assert "fileio_slot_0" in bpf_source
    assert "fileio_slot_1" in bpf_source
    assert "BPF_MAP_TYPE_ARRAY_OF_MAPS" in bpf_source
    assert "BPF_MAP_TYPE_LRU_HASH" not in bpf_source
    assert re.search(r"fileio_slots|event_slots|hash_slots", bpf_source)


def test_bpf_program_counts_failed_updates_as_per_cgroup_overflow(native_producer_probe) -> None:
    # Map capacity failures must preserve the actual positive return counts.
    result = native_producer_probe("overflow-sequence")
    assert (result["overflow_files"], result["overflow_bytes_r"],
            result["overflow_bytes_w"]) == (3, 100, 11)
    assert result["event_present"] == 0
    assert (result["hook_hits"], result["hook_hits_w"], result["in_flight"]) == (2, 1, 0)


def test_readv_and_writev_bodies_do_not_call_iter_hooks(bpf_source: str) -> None:
    readv_body = _function_body(bpf_source, "fileio_vfs_readv")
    writev_body = _function_body(bpf_source, "fileio_vfs_writev")
    assert "vfs_iter_read" not in readv_body
    assert "vfs_iter_write" not in writev_body


def test_loader_declares_protocol_and_six_hook_abi(loader_source: str) -> None:
    for opcode in ("FILEIO_OP_HELLO", "FILEIO_OP_FLIP_DRAIN", "FILEIO_OP_CLOSE"):
        assert opcode in loader_source
    assert "FILEIO_HOOK_MASK_ALL" in loader_source
    assert "FILEIO_MAX_MAP_ENTRIES" in loader_source
    assert "verifier" in loader_source.lower()
    assert "fileio_protocol.h" in loader_source


def test_loader_uses_quiescence_and_clear_before_drain_ack(native_loader_probe) -> None:
    # F=flip, B=busy lookup, S=sleep, Q=zero in-flight, E/T=first
    # event/stats clear, A=ack. Removing the production wait gives FETA.
    for result in native_loader_probe():
        assert result["order"] == "FBSBSQETA"
        assert result["cleared_while_busy"] == 0
        assert (result["wait_reads"], result["sleep_calls"]) == (3, 2)
        assert (result["status"], result["error"], result["complete"]) == (0, 0, 1)


@pytest.mark.parametrize("scenario,expected", [
    ("read-sequence", (100, 2, 11, 1, 2, 1)),
    ("read-wide", (4294967313, 1, 0, 0, 1, 0)),
])
def test_native_producer_successful_read_byte_arithmetic(
    native_producer_probe, scenario, expected,
) -> None:
    # First insert and existing-entry accumulation use successful returned bytes;
    # zero/negative returns do not contribute, and 64-bit returns are not truncated.
    result = native_producer_probe(scenario)
    assert (result["bytes_r"], result["reads"], result["bytes_w"], result["writes"],
            result["hook_hits"], result["hook_hits_w"]) == expected
    assert (result["overflow_files"], result["in_flight"]) == (0, 0)


@pytest.mark.parametrize("mode,error,order", [
    ("timeout", -110, "FBA"),
    ("lookup-error", -5, "FXA"),
])
def test_native_loader_failed_quiescence_preserves_retired_maps(
    native_loader_probe, mode, error, order,
) -> None:
    result, = native_loader_probe(mode=mode)
    assert result["order"] == order
    assert (result["status"], result["error"], result["complete"]) == (1, error, 0)
    assert result["event_counts"] == [55, 7]
    assert result["stats_counts"] == [1, 1]
    assert (result["events"], result["stats"], result["wait_reads"]) == (0, 0, 1)


@pytest.fixture(scope="module")
def native_producer_probe(tmp_path_factory):
    """Compile production helpers against deterministic map and atomic stubs."""
    compiler = shutil.which("cc")
    assert compiler is not None, "native producer probe requires cc"
    out = tmp_path_factory.mktemp("native-producer-probe")
    source = BPF_SOURCE.read_text(encoding="utf-8")
    # Preserve all map declarations and helper bodies verbatim. The hook wrappers
    # require BPF register calling conventions and are checked by the BPF build.
    helpers = source[:source.index('SEC("fexit/vfs_read")')]
    helpers = "\n".join(line for line in helpers.splitlines()
                        if not line.startswith("#include <bpf/"))
    (out / "fileio_host_helpers.h").write_text(helpers, encoding="utf-8")
    binary = out / "producer"
    built = subprocess.run(
        [compiler, "-O2", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
         "-I", str(out), "-I", str(BPF_DIR),
         str(Path(__file__).with_name("fileio_producer_host.c")), "-o", str(binary)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    def run(scenario, slot=0, cgroup=42):
        result = subprocess.run([str(binary), scenario, str(slot), str(cgroup)],
                                capture_output=True, text=True, check=False, timeout=5)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    return run


@pytest.mark.parametrize("scenario", ["race-event", "race-stats"])
@pytest.mark.parametrize("slot,cgroup", [(0, 42), (1, 0)])
def test_native_producer_first_touch_preserves_both_cpus(
    native_producer_probe, scenario, slot, cgroup,
) -> None:
    # Breaks on BPF_ANY at either first-touch site, including the reserved key.
    result = native_producer_probe(scenario, slot, cgroup)
    assert (result["bytes_r"], result["reads"], result["hook_hits"],
            result["overflow_files"], result["overflow_bytes_r"]) == (100, 2, 2, 0, 0)
    assert result["stats_present"] == result["event_present"] == 1
    assert result["cgroup_id"] == cgroup
    assert result["in_flight"] == 0


def test_native_producer_stats_race_preserves_skip_deltas(native_producer_probe) -> None:
    result = native_producer_probe("race-skip")
    assert (result["skipped_nonfile"], result["skipped_unsupported"]) == (1, 1)


def test_native_producer_genuine_insert_failures(native_producer_probe) -> None:
    for scenario in ("event-full", "event-disappears"):
        full = native_producer_probe(scenario)
        assert (full["event_present"], full["overflow_files"], full["overflow_bytes_r"],
                full["hook_hits"], full["in_flight"]) == (0, 1, 30, 1, 0)
    for scenario in ("stats-full", "stats-disappears"):
        no_stats = native_producer_probe(scenario)
        assert (no_stats["stats_present"], no_stats["event_present"], no_stats["bytes_r"],
                no_stats["in_flight"]) == (0, 1, 30, 0)


@pytest.fixture(scope="module")
def native_loader_probe(tmp_path_factory):
    """Run production C against fake maps; never open/load a BPF object."""
    compiler = shutil.which("cc")
    assert compiler is not None, "native loader probe requires cc and libbpf development files"
    out = tmp_path_factory.mktemp("native-loader-probe")
    source = Path(__file__).with_name("fileio_loader_host.c")
    binary = out / "probe"
    built = subprocess.run(
        [compiler, "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(BPF_DIR), str(source), "-o", str(binary), "-lbpf", "-lelf", "-lz"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    def run(kernel_dev=0x10300005, mode="success"):
        result = subprocess.run([str(binary), hex(kernel_dev), mode], capture_output=True,
                                text=True, check=False, timeout=5)
        assert result.returncode == 0, result.stderr
        return [json.loads(line) for line in result.stdout.splitlines()]

    return run


@pytest.mark.parametrize("kernel_dev, expected", [
    (0x10300005, [259, 5]),
    (0x00800100, [8, 256]),
    (0xFFFFFFFF, [4095, 1048575]),
    (0, [0, 0]),
])
def test_native_loader_serializes_kernel_bd_dev(native_loader_probe, kernel_dev, expected) -> None:
    # Linux dev_t: (major << 20) | minor. libc's st_dev encoding is a different ABI.
    assert native_loader_probe(kernel_dev)[0]["device"] == expected


def test_native_loader_clears_both_retired_inner_maps(native_loader_probe) -> None:
    # Characterization control: this already passes at the lane's baseline.
    # Removing clear_map or selecting the new slot must make it fail.
    first, second, revisit = native_loader_probe()
    expected_first = {
        "slot": 0, "events": 55, "stats": 1, "status": 0, "active": 1,
        "event_counts": [0, 7], "stats_counts": [0, 1], "device": first["device"],
    }
    assert {key: first[key] for key in expected_first} == expected_first
    assert second["slot"] == 1 and second["events"] == 7 and second["stats"] == 1
    assert second["event_counts"] == [0, 0] and second["stats_counts"] == [0, 0]
    assert revisit["slot"] == 0 and revisit["events"] == 0 and revisit["stats"] == 0
