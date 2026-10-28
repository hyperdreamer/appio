from __future__ import annotations

import re
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


def test_bpf_program_counts_failed_updates_as_per_cgroup_overflow(bpf_source: str) -> None:
    update = re.search(
        r"if\s*\(\s*(?:update_rc|update_result|rc)\s*!=\s*0\s*\)\s*\{(?P<body>.*?)\}",
        bpf_source,
        re.DOTALL,
    )
    assert update is not None
    body = update.group("body")
    assert "overflow_files" in body
    assert "overflow_bytes" in body
    assert "fileio_cgroup_stats" in bpf_source


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


def test_loader_uses_quiescence_and_clear_before_drain_ack(loader_source: str) -> None:
    for token in ("in_flight", "quiesc", "clear", "DRAIN", "complete"):
        assert token.lower() in loader_source.lower()
    assert re.search(r"acknowledge|response", loader_source, re.IGNORECASE)
