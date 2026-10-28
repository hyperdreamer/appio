from __future__ import annotations

import re
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BPF_DIR = ROOT / "appio_iod" / "bpf"
PROTOCOL_HEADER = BPF_DIR / "fileio_protocol.h"


@pytest.fixture
def protocol_header() -> str:
    assert PROTOCOL_HEADER.is_file(), f"missing protocol header: {PROTOCOL_HEADER}"
    return PROTOCOL_HEADER.read_text(encoding="utf-8")


def _format_macro(header: str, name: str) -> str:
    match = re.search(rf"#define\s+{name}\s+\"([^\"]+)\"", header)
    assert match is not None, f"missing wire format macro {name}"
    return match.group(1)


def test_protocol_round_trips_file_event_and_reserved_cgroup_stats(protocol_header: str) -> None:
    assert "FILEIO_ABI_VERSION 1" in protocol_header
    assert "FILEIO_NAME_CAP 40" in protocol_header
    assert "FILEIO_MAX_MAP_ENTRIES 262144" in protocol_header
    assert re.search(r"FILEIO_CGROUP_UNKNOWN\s+0", protocol_header)
    assert "__attribute__((packed))" in protocol_header

    event_struct = struct.Struct(_format_macro(protocol_header, "FILEIO_FILE_RECORD_FORMAT"))
    stats_struct = struct.Struct(_format_macro(protocol_header, "FILEIO_CGROUP_STATS_FORMAT"))
    name = b"report"
    padded_name = name + b"\0" * (40 - len(name))
    event_values = (17, 8, 9, 123456, 22, 3, 4, 4096, 8192, len(name), padded_name)
    event = event_struct.pack(*event_values)
    assert len(event) == 104
    assert event_struct.unpack(event) == event_values

    stats_values = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
    stats = stats_struct.pack(*stats_values)
    assert len(stats) == 96
    assert stats_struct.unpack(stats) == stats_values
    assert stats_struct.unpack(stats)[0] == 0


def test_protocol_declares_wire_field_order_and_fixed_sizes(protocol_header: str) -> None:
    file_record = re.search(
        r"struct\s+fileio_file_record\s*\{(?P<body>.*?)\}\s*__attribute__\s*\(\(packed\)\)",
        protocol_header,
        re.DOTALL,
    )
    assert file_record is not None
    body = file_record.group("body")
    field_order = [
        "cgroup_id",
        "major",
        "minor",
        "inode",
        "generation",
        "reads",
        "writes",
        "bytes_r",
        "bytes_w",
        "name_len",
        "name",
    ]
    positions = [body.index(field) for field in field_order]
    assert positions == sorted(positions)
    assert re.search(r"name\s*\[\s*FILEIO_NAME_CAP\s*\]", body)


def test_make_check_builds_object_and_loader_without_root() -> None:
    with tempfile.TemporaryDirectory(prefix="appio-bpf-build-") as directory:
        result = subprocess.run(
            ["make", "-C", str(BPF_DIR), "check", f"OUT_DIR={directory}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(
                "make check failed with exit "
                f"{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        assert (Path(directory) / "fileio.bpf.o").is_file()
        assert (Path(directory) / "fileio_loader").is_file()
