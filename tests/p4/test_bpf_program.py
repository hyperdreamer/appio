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


@pytest.fixture(scope="module")
def native_loader_probe(tmp_path_factory):
    """Run production C against fake maps; never open/load a BPF object."""
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native loader probe requires cc and libbpf development files")
    out = tmp_path_factory.mktemp("native-loader-probe")
    source = out / "probe.c"
    source.write_text(
        r'''
#define _POSIX_C_SOURCE 200809L
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static int fake_next(int fd, const void *key, void *next);
static int fake_lookup(int fd, const void *key, void *value);
static int fake_delete(int fd, const void *key);
static int fake_update(int fd, const void *key, const void *value, __u64 flags);
static ssize_t fake_write(int fd, const void *data, size_t size);
static __attribute__((unused)) FILE *fake_fopen(const char *path, const char *mode);
#define fopen fake_fopen
#define bpf_map_get_next_key fake_next
#define bpf_map_lookup_elem fake_lookup
#define bpf_map_delete_elem fake_delete
#define bpf_map_update_elem fake_update
#define write fake_write
#define main fileio_loader_main
#include "fileio_loader.c"
#undef main
#undef write
#undef fopen

/* Suppress the baseline's temporary device debug file during the probe. */
static FILE *fake_fopen(const char *path, const char *mode)
{
    (void)path; (void)mode;
    return NULL;
}

static unsigned int active_slot;
static __u64 raw_device = 0x10300005;
/* event slots 0/1 at fd 20/21; stats slots 0/1 at fd 22/23. */
static unsigned char live[4][55];
static unsigned char output[32768];
static size_t output_size;

static int absent(void) { errno = ENOENT; return -ENOENT; }
static int index_for(int fd) { if (fd < 20 || fd > 23) abort(); return fd - 20; }
static __attribute__((noinline)) unsigned int key_index(int fd, const void *key)
{
    return fd < 22 ? (unsigned int)((const struct fileio_key *)key)->inode - 1
                   : (unsigned int)*(const __u64 *)key - 1;
}
static int fake_next(int fd, const void *key, void *next)
{
    int map = index_for(fd);
    unsigned int start = key ? key_index(fd, key) + 1 : 0;
    for (unsigned int i = start; i < 55; i++) {
        if (!live[map][i]) continue;
        if (fd < 22) {
            struct fileio_key event = { .cgroup_id = 42, .device = raw_device,
                                       .inode = i + 1, .i_generation = 7 };
            memcpy(next, &event, sizeof(event));
        } else {
            __u64 cgroup = i + 1;
            memcpy(next, &cgroup, sizeof(cgroup));
        }
        return 0;
    }
    return absent();
}
static int fake_lookup(int fd, const void *key, void *value)
{
    if (fd == 10) { memcpy(value, &active_slot, sizeof(active_slot)); return 0; }
    if (fd == 11) { memset(value, 0, sizeof(__u64)); return 0; }
    unsigned int i = key_index(fd, key);
    if (i >= 55 || !live[index_for(fd)][i]) return absent();
    if (fd < 22) {
        struct fileio_value event = { .reads = 1, .bytes_r = 17,
                                     .name_len = 3, .name = "log" };
        memcpy(value, &event, sizeof(event));
    } else {
        struct fileio_cgroup_stats stats = {0};
        memcpy(value, &stats, sizeof(stats));
    }
    return 0;
}
static int fake_delete(int fd, const void *key)
{
    unsigned int i = key_index(fd, key);
    if (i >= 55 || !live[index_for(fd)][i]) return absent();
    live[index_for(fd)][i] = 0;
    return 0;
}
static int fake_update(int fd, const void *key, const void *value, __u64 flags)
{
    if (fd != 10 || *(__u32 *)key != 0 || flags != BPF_ANY) abort();
    memcpy(&active_slot, value, sizeof(active_slot));
    return 0;
}
static ssize_t fake_write(int fd, const void *data, size_t size)
{
    if (fd != STDOUT_FILENO || output_size + size > sizeof(output)) abort();
    memcpy(output + output_size, data, size);
    output_size += size;
    return (ssize_t)size;
}
static unsigned int count(int map)
{
    unsigned int result = 0;
    for (unsigned int i = 0; i < 55; i++) result += live[map][i] != 0;
    return result;
}
int main(int argc, char **argv)
{
    if (argc == 2) raw_device = strtoull(argv[1], NULL, 0);
    struct loader_state state = { .active_slot_fd = 10, .in_flight_fd = 11,
        .event_fd = {20, 21}, .stats_fd = {22, 23}, .hook_mask = 63, .ready = true };
    struct fileio_flip_request request = { .epoch = 1, .seq = 1 };
    memset(live[0], 1, 55);
    memset(live[1], 1, 7);
    live[2][0] = live[3][0] = 1;
    for (int pass = 0; pass < 3; pass++) {
        output_size = 0;
        if (drain(&state, &request) != 0) return 1;
        struct fileio_drain_response response;
        memcpy(&response, output, sizeof(response));
        struct fileio_file_record event = {0};
        if (response.event_count)
            memcpy(&event, output + sizeof(response), sizeof(event));
        printf("{\"slot\":%u,\"events\":%u,\"stats\":%u,\"status\":%u,"
               "\"active\":%u,\"event_counts\":[%u,%u],\"stats_counts\":[%u,%u],"
               "\"device\":[%u,%u]}\n", response.drained_slot,
               response.event_count, response.stats_count, response.status, active_slot,
               count(0), count(1), count(2), count(3), event.major, event.minor);
        request.seq++;
    }
    return 0;
}
''',
        encoding="utf-8",
    )
    binary = out / "probe"
    built = subprocess.run(
        [compiler, "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(BPF_DIR), str(source), "-o", str(binary), "-lbpf", "-lelf", "-lz"],
        capture_output=True, text=True, check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    def run(kernel_dev=0x10300005):
        result = subprocess.run([str(binary), hex(kernel_dev)], capture_output=True,
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
    assert first == {
        "slot": 0, "events": 55, "stats": 1, "status": 0, "active": 1,
        "event_counts": [0, 7], "stats_counts": [0, 1], "device": first["device"],
    }
    assert second["slot"] == 1 and second["events"] == 7 and second["stats"] == 1
    assert second["event_counts"] == [0, 0] and second["stats_counts"] == [0, 0]
    assert revisit["slot"] == 0 and revisit["events"] == 0 and revisit["stats"] == 0
