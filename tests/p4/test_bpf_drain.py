"""Rootless contract tests for the P4 per-file drain adapter.

Every case drives the adapter through an injected shim: no root, no BPF load, no
``/proc`` and no ``/sys`` read. Each test docstring names the falsifier — the
mutation that must make it fail — so a green run is evidence rather than a
formality.
"""

from __future__ import annotations

import ast
import errno
import io
import re
import struct
from pathlib import Path

import pytest

from appio.bpf_fileio import (
    ABI_VERSION,
    ACK_RESPONSE,
    CGROUP_STATS,
    COMMAND_HEADER,
    DIAGNOSTIC_CAP,
    DRAIN_COMPLETE,
    DRAIN_RESPONSE,
    FILE_RECORD,
    FLIP_REQUEST,
    HELLO_RESPONSE,
    HOOK_MASK_ALL,
    MAX_MAP_ENTRIES,
    NAME_CAP,
    OP_CLOSE,
    OP_FLIP_DRAIN,
    OP_HELLO,
    RESERVED_CGROUP_ID,
    STATUS_ERROR,
    STATUS_OK,
    BpfFileSource,
    CgroupFileStats,
    FileAvailability,
    FileDrain,
    FileEvent,
    FileSourceError,
    LoaderFrameError,
    StreamLoader,
    VectorLoader,
    pack_cgroup_stats,
    pack_file_record,
)
from appio.contracts import HookHits
from appio.durable import Receipt

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_HEADER = ROOT / "appio_iod" / "bpf" / "fileio_protocol.h"
MODULE_PATH = ROOT / "src" / "appio" / "bpf_fileio.py"


# ---------------------------------------------------------------------------
# Fixtures and wire helpers
# ---------------------------------------------------------------------------


def make_event(**overrides: object) -> FileEvent:
    values: dict[str, object] = {
        "cgroup_id": 1001,
        "dev": "259:0",
        "inode": 42,
        "generation": 7,
        "basename": b"a.txt",
        "name_len": 5,
        "reads": 1,
        "writes": 0,
        "bytes_r": 40,
        "bytes_w": 0,
    }
    values.update(overrides)
    return FileEvent(**values)  # type: ignore[arg-type]


def make_stats(**overrides: object) -> CgroupFileStats:
    values: dict[str, object] = {
        "cgroup_id": 1001,
        "hook_hits": HookHits(1, 0, 0, 0, 0, 0),
        "overflow_files": 0,
        "overflow_bytes_r": 0,
        "overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
    }
    values.update(overrides)
    return CgroupFileStats(**values)  # type: ignore[arg-type]


def raw_record(**overrides: object) -> bytes:
    """One fixed-width file record, packed straight onto the wire."""
    values: dict[str, object] = {
        "cgroup_id": 1001,
        "major": 259,
        "minor": 0,
        "inode": 42,
        "generation": 7,
        "reads": 1,
        "writes": 0,
        "bytes_r": 4096,
        "bytes_w": 0,
        "name_len": 5,
        "name": b"a.txt".ljust(NAME_CAP, b"\0"),
    }
    values.update(overrides)
    ordered = [
        values["cgroup_id"],
        values["major"],
        values["minor"],
        values["inode"],
        values["generation"],
        values["reads"],
        values["writes"],
        values["bytes_r"],
        values["bytes_w"],
        values["name_len"],
        values["name"],
    ]
    return FILE_RECORD.pack(*ordered)  # type: ignore[arg-type]


def raw_stats(**overrides: object) -> bytes:
    values = [
        overrides.get("cgroup_id", 1001),
        *overrides.get("hook_hits", (1, 0, 0, 0, 0, 0)),
        overrides.get("overflow_files", 0),
        overrides.get("overflow_bytes_r", 0),
        overrides.get("overflow_bytes_w", 0),
        overrides.get("skipped_nonfile", 0),
        overrides.get("skipped_unsupported_fs", 0),
    ]
    return CGROUP_STATS.pack(*values)


def started(loader: VectorLoader) -> BpfFileSource:
    source = BpfFileSource(loader)
    assert source.start() == FileAvailability(file_io=True, bpf_loaded=True, diagnostic=None)
    return source


def _format_macro(header: str, name: str) -> str:
    match = re.search(rf'#define\s+{name}\s+"([^"]+)"', header)
    assert match is not None, f"missing wire format macro {name}"
    return match.group(1)


# ---------------------------------------------------------------------------
# Wire layout pins
# ---------------------------------------------------------------------------


def test_wire_layouts_match_the_protocol_header() -> None:
    """Falsifier: any hand-typed struct format, size, or constant drifting from
    ``fileio_protocol.h`` (the C producer would then decode our frames wrongly)."""
    header = PROTOCOL_HEADER.read_text(encoding="utf-8")
    assert f"#define FILEIO_ABI_VERSION {ABI_VERSION}" in header
    assert f"#define FILEIO_NAME_CAP {NAME_CAP}" in header
    assert f"#define FILEIO_MAX_MAP_ENTRIES {MAX_MAP_ENTRIES}" in header
    assert f"#define FILEIO_HOOK_MASK_ALL 0x{HOOK_MASK_ALL:x}" in header
    assert f"#define FILEIO_DIAGNOSTIC_CAP {DIAGNOSTIC_CAP}" in header
    assert re.search(rf"#define FILEIO_CGROUP_UNKNOWN\s+{RESERVED_CGROUP_ID}\b", header)
    assert re.search(rf"#define FILEIO_DRAIN_COMPLETE\s+{DRAIN_COMPLETE}\b", header)

    assert FILE_RECORD.format == _format_macro(header, "FILEIO_FILE_RECORD_FORMAT")
    assert CGROUP_STATS.format == _format_macro(header, "FILEIO_CGROUP_STATS_FORMAT")
    assert (FILE_RECORD.size, CGROUP_STATS.size) == (104, 96)
    assert (
        COMMAND_HEADER.size,
        FLIP_REQUEST.size,
        HELLO_RESPONSE.size,
        ACK_RESPONSE.size,
        DRAIN_RESPONSE.size,
    ) == (8, 16, 20, 12, 52)


def test_pack_helpers_round_trip_through_the_decoder() -> None:
    """Falsifier: a pack helper that drops a counter or a name byte, which the
    decoder could not then recover on a real drain."""
    event = make_event(basename=b"report", name_len=6)
    stats = make_stats(overflow_files=3, overflow_bytes_r=5, overflow_bytes_w=7,
                       skipped_nonfile=11, skipped_unsupported_fs=13)
    loader = VectorLoader()
    source = started(loader)
    loader.queue(0, events=[pack_file_record(event)], stats=[pack_cgroup_stats(stats)])
    drain = source.drain(Receipt("iod", 1, 1))
    assert drain.events == (event,)
    assert drain.stats == (stats,)


# ---------------------------------------------------------------------------
# HELLO
# ---------------------------------------------------------------------------


def test_start_returns_available_when_the_loader_is_ready() -> None:
    """Falsifier: a start that reports F available without a HELLO, or that
    leaves a diagnostic on a healthy attach."""
    loader = VectorLoader()
    source = BpfFileSource(loader)
    availability = source.start()
    assert availability == FileAvailability(file_io=True, bpf_loaded=True, diagnostic=None)
    assert source.available is True
    assert source.error is None
    assert loader.hello_requests == 1


def test_start_requires_all_six_hook_bits() -> None:
    """Falsifier: dropping the attach-mask check, which would advertise F from a
    loader that attached five hooks."""
    loader = VectorLoader(hook_mask=HOOK_MASK_ALL & ~(1 << 4))
    source = BpfFileSource(loader)
    availability = source.start()
    assert availability.file_io is False
    assert availability.bpf_loaded is False
    assert "vfs_iter_read" in availability.diagnostic
    assert source.available is False
    assert source.error is not None and source.error.errno is None


def test_start_rejects_a_map_capacity_other_than_262144() -> None:
    """Falsifier: accepting a smaller map, which silently changes F's loss
    bound and its documented overflow accounting."""
    source = BpfFileSource(VectorLoader(map_capacity=MAX_MAP_ENTRIES - 1))
    availability = source.start()
    assert availability.file_io is False
    assert "map capacity" in availability.diagnostic


def test_start_rejects_an_abi_mismatch() -> None:
    """Falsifier: decoding a frame whose ABI we do not know, so every field
    offset after the header is a guess."""
    source = BpfFileSource(VectorLoader(hello_abi_version=ABI_VERSION + 1))
    availability = source.start()
    assert availability.file_io is False
    assert "ABI" in availability.diagnostic
    assert source.error is not None and source.error.errno == -errno.EPROTO


def test_start_surfaces_a_bounded_verifier_diagnostic() -> None:
    """Falsifier: keeping a load failure silent, or losing the errno while
    truncating the verifier text to the administrator-facing bound."""
    verifier = b"BPF verifier: invalid bpf_context access at insn 12. " * 6
    assert 100 < len(verifier) <= DIAGNOSTIC_CAP
    source = BpfFileSource(
        VectorLoader(hello_status=STATUS_ERROR, hello_error=-errno.EACCES, verifier=verifier)
    )
    availability = source.start()
    assert availability.file_io is False and availability.bpf_loaded is False
    assert "Permission denied" in availability.diagnostic
    assert "BPF verifier" in availability.diagnostic
    assert len(availability.diagnostic) <= DIAGNOSTIC_CAP
    error = source.error
    assert isinstance(error, FileSourceError)
    assert error.errno == -errno.EACCES
    assert error.verifier == verifier.decode("utf-8")
    assert "Permission denied" in str(error)


def test_start_rejects_an_oversized_verifier_payload() -> None:
    """Falsifier: reading a declared verifier length without bounding it, which
    lets the loader choose our allocation size."""
    source = BpfFileSource(VectorLoader(verifier=b"v" * (DIAGNOSTIC_CAP + 1)))
    availability = source.start()
    assert availability.file_io is False
    assert str(DIAGNOSTIC_CAP) in availability.diagnostic
    assert "exceeds" in availability.diagnostic


def test_start_after_a_failure_recovers_the_source() -> None:
    """Falsifier: a source that stays unavailable forever, or one that claims
    availability without a new HELLO."""
    loader = VectorLoader(hook_mask=0)
    source = BpfFileSource(loader)
    assert source.start().file_io is False
    loader.hook_mask = HOOK_MASK_ALL

    availability = source.start()
    assert availability == FileAvailability(file_io=True, bpf_loaded=True, diagnostic=None)
    assert source.available is True
    assert source.error is None
    assert loader.hello_requests == 2


# ---------------------------------------------------------------------------
# Losslessness across the slot flip
# ---------------------------------------------------------------------------


def test_drain_returns_old_slot_records_once_across_a_flip() -> None:
    """Falsifier: draining the new slot, forgetting the second slot, or
    returning an already-drained record again (a duplicate receipt)."""
    old = make_event(basename=b"old.txt", name_len=7)
    new = make_event(inode=99, basename=b"new.txt", name_len=7)
    loader = VectorLoader()
    loader.queue(0, events=[old])
    source = started(loader)

    first = source.drain(Receipt("iod", 1, 1))
    assert first.events == (old,)
    assert first.complete is True

    loader.queue(1, events=[new])
    second = source.drain(Receipt("iod", 1, 2))
    assert second.events == (new,)

    third = source.drain(Receipt("iod", 1, 3))
    assert third.events == ()
    assert loader.flip_requests == [(1, 1), (1, 2), (1, 3)]
    assert len({event.inode for event in first.events + second.events}) == 2


def test_drain_returns_every_record_in_a_slot_exactly_once() -> None:
    """Falsifier: dropping the trailing record of a slot, or re-reporting a
    slot that was already enumerated and cleared."""
    events = [make_event(inode=index + 1, basename=b"f", name_len=1) for index in range(3)]
    loader = VectorLoader()
    loader.queue(0, events=events)
    source = started(loader)

    first = source.drain(Receipt("iod", 1, 1))
    assert first.events == tuple(events)
    second = source.drain(Receipt("iod", 1, 2))
    assert second.events == ()


def test_drain_converts_major_minor_to_canonical_dev() -> None:
    """Falsifier: stringifying the raw device number, zero-padding a part, or
    formatting major/minor in the wrong order."""
    cases = [((8, 1), "8:1"), ((259, 0), "259:0"), ((0, 0), "0:0"),
             ((2**32 - 1, 2**32 - 1), "4294967295:4294967295")]
    for (major, minor), expected in cases:
        loader = VectorLoader()
        loader.queue(0, events=[raw_record(major=major, minor=minor)])
        source = started(loader)
        drain = source.drain(Receipt("iod", 1, 1))
        assert [event.dev for event in drain.events] == [expected]


# ---------------------------------------------------------------------------
# Record validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dev",
    ["0259:0", "259:00", "0:00", "259", "259:", ":0", "259:0:", " 259:0",
     "259 :0", "-1:0", "+1:0", "２５９:0", "259:0\n", "4294967296:0"],
)
def test_file_event_rejects_noncanonical_dev_strings(dev: str) -> None:
    """Falsifier: accepting a dev string a consumer could not compare or sort
    bytewise against the canonical ``major:minor`` spelling."""
    with pytest.raises(ValueError):
        make_event(dev=dev)


@pytest.mark.parametrize("dev", ["0:0", "259:0", "8:1", "4294967295:4294967295"])
def test_file_event_accepts_canonical_dev_strings(dev: str) -> None:
    """Falsifier: an over-strict dev validator that rejects the kernel's own
    canonical spelling."""
    assert make_event(dev=dev).dev == dev


@pytest.mark.parametrize(
    "overrides",
    [
        {"reads": True},
        {"writes": False},
        {"bytes_r": -1},
        {"bytes_w": -1},
        {"inode": -1},
        {"name_len": True},
        {"bytes_r": 2**63},
        {"cgroup_id": -5},
    ],
)
def test_file_event_rejects_booleans_negative_and_oversized_counters(
    overrides: dict[str, object],
) -> None:
    """Falsifier: letting a bool through ``isinstance(value, int)``, or binding a
    negative/oversized counter that SQLite would reject later."""
    with pytest.raises(ValueError):
        make_event(**overrides)


def test_file_event_rejects_generation_above_the_durable_limit() -> None:
    """Falsifier: accepting a generation the durable ``file.generation`` range
    (0..2**32-1) cannot store, which would fail at persistence time instead."""
    assert make_event(generation=2**32 - 1).generation == 2**32 - 1
    with pytest.raises(ValueError):
        make_event(generation=2**32)


@pytest.mark.parametrize(
    "overrides",
    [
        {"basename": b"a.txt", "name_len": 6},
        {"basename": b"a" * (NAME_CAP + 1), "name_len": NAME_CAP + 1},
        {"basename": b"a\x00b", "name_len": 3},
        {"basename": "a.txt", "name_len": 5},
        {"basename": bytearray(b"a.txt"), "name_len": 5},
    ],
)
def test_file_event_rejects_malformed_names(overrides: dict[str, object]) -> None:
    """Falsifier: silently re-slicing or normalizing a name whose declared
    length disagrees with its bytes, which invents a basename."""
    with pytest.raises(ValueError):
        make_event(**overrides)


def test_file_event_keeps_raw_name_bytes_for_a_dentry_miss() -> None:
    """Falsifier: rejecting or unicode-decoding the loader's dentry-unavailable
    record; this layer records bytes and lets attribution decide weak identity."""
    event = make_event(basename=b"\xff\xfe", name_len=2)
    assert event.basename == b"\xff\xfe"
    assert event.name_len == 2
    empty = make_event(basename=b"", name_len=0)
    assert empty.basename == b""


def test_file_event_accepts_a_truncated_name_at_the_cap() -> None:
    """Falsifier: treating a capped 40-byte name as a length violation."""
    event = make_event(basename=b"n" * NAME_CAP, name_len=NAME_CAP)
    assert event.name_len == NAME_CAP


def test_cgroup_file_stats_rejects_boolean_counters() -> None:
    """Falsifier: accepting ``True`` as the number one in any stats counter."""
    with pytest.raises(ValueError):
        make_stats(overflow_files=True)
    with pytest.raises(ValueError):
        make_stats(hook_hits=HookHits(1, 0, 0, 0, 0, True))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Reserved unknown-owner key
# ---------------------------------------------------------------------------


def test_drain_rejects_a_file_record_under_the_reserved_key() -> None:
    """Falsifier: turning an unassignable event into an app row by accepting
    cgroup 0 — the reserved ``overflow_owner_unknown`` key — as a process cgroup."""
    loader = VectorLoader()
    loader.queue(0, events=[raw_record(cgroup_id=RESERVED_CGROUP_ID)])
    source = started(loader)
    with pytest.raises(FileSourceError, match="reserved"):
        source.drain(Receipt("iod", 1, 1))
    assert source.available is False


def test_drain_keeps_reserved_key_zero_separate_from_real_cgroups() -> None:
    """Falsifier: folding the reserved row into a real cgroup, or dropping it,
    either of which changes the unknown-owner remainder."""
    real = make_stats(cgroup_id=42, overflow_files=1, overflow_bytes_r=5, overflow_bytes_w=7)
    reserved = make_stats(cgroup_id=RESERVED_CGROUP_ID, overflow_files=2,
                          overflow_bytes_r=11, overflow_bytes_w=13)
    loader = VectorLoader()
    loader.queue(0, stats=[real, reserved])
    source = started(loader)

    drain = source.drain(Receipt("iod", 1, 1))
    assert drain.stats == (real, reserved)
    assert drain.cgroup_stats == (real,)
    assert drain.unknown_owner_stats == (reserved,)
    assert [(item.cgroup_id, item.overflow_files, item.overflow_bytes_r,
             item.overflow_bytes_w) for item in drain.stats] == [
        (42, 1, 5, 7), (RESERVED_CGROUP_ID, 2, 11, 13)]
    assert real.is_unknown_owner is False
    assert reserved.is_unknown_owner is True


@pytest.mark.parametrize("cgroup_id", [1001, RESERVED_CGROUP_ID])
def test_drain_rejects_duplicate_cgroup_stats(cgroup_id: int) -> None:
    """Falsifier: accepting two stats rows for one key, including the reserved
    key, which would double-count hook hits and overflow."""
    loader = VectorLoader()
    loader.queue(0, stats=[raw_stats(cgroup_id=cgroup_id), raw_stats(cgroup_id=cgroup_id)])
    source = started(loader)
    with pytest.raises(FileSourceError, match="duplicate"):
        source.drain(Receipt("iod", 1, 1))
    assert source.available is False


def test_file_drain_rejects_duplicate_stats_without_the_wire() -> None:
    """Falsifier: a dataclass that only the wire decoder protects; a duplicate
    built directly must be refused too."""
    stats = make_stats()
    with pytest.raises(ValueError):
        FileDrain(Receipt("iod", 1, 1), (), (stats, stats), True, None)


# ---------------------------------------------------------------------------
# Frame widths, truncation and trailing bytes
# ---------------------------------------------------------------------------


def test_drain_rejects_a_truncated_record_payload() -> None:
    """Falsifier: accepting a frame holding fewer record bytes than the header
    declares, which would drop a trailing event silently."""
    loader = VectorLoader(event_count_report=2)
    loader.queue(0, events=[raw_record()])
    source = started(loader)
    with pytest.raises(FileSourceError, match="drain payload"):
        source.drain(Receipt("iod", 1, 1))
    assert source.available is False


def test_drain_rejects_trailing_bytes_in_a_drain_payload() -> None:
    """Falsifier: accepting a frame with more payload than the header declares,
    which leaves undecoded bytes in the command stream."""
    loader = VectorLoader(event_count_report=1)
    loader.queue(0, events=[raw_record(), raw_record(inode=43)])
    source = started(loader)
    with pytest.raises(FileSourceError, match="drain payload"):
        source.drain(Receipt("iod", 1, 1))


def test_drain_rejects_a_declared_count_above_the_map_capacity() -> None:
    """Falsifier: trusting an unbounded count from the loader before reading it."""
    loader = VectorLoader(event_count_report=MAX_MAP_ENTRIES + 1)
    source = started(loader)
    with pytest.raises(FileSourceError, match="map capacity"):
        source.drain(Receipt("iod", 1, 1))


def test_drain_rejects_a_record_name_len_wider_than_the_field() -> None:
    """Falsifier: slicing the fixed 40-byte name to a declared length above the
    cap, which silently truncates instead of reporting a malformed width."""
    loader = VectorLoader()
    loader.queue(0, events=[raw_record(name_len=NAME_CAP + 1)])
    source = started(loader)
    with pytest.raises(FileSourceError, match="name_len"):
        source.drain(Receipt("iod", 1, 1))


@pytest.mark.parametrize(
    ("overrides", "raw"),
    [
        ({"inode": 2**63}, "9223372036854775808"),
        ({"cgroup_id": 2**63}, "9223372036854775808"),
        ({"bytes_r": 2**63 + 5}, str(2**63 + 5)),
    ],
)
def test_drain_rejects_an_out_of_range_unsigned_value_and_names_it(
    overrides: dict[str, object], raw: str
) -> None:
    """Falsifier: overflow-wrapping or silently clamping a raw unsigned value
    instead of naming it in the failure detail."""
    loader = VectorLoader()
    loader.queue(0, events=[raw_record(**overrides)])
    source = started(loader)
    with pytest.raises(FileSourceError) as caught:
        source.drain(Receipt("iod", 1, 1))
    assert raw in str(caught.value)


def test_drain_rejects_a_drained_slot_outside_the_two_slots() -> None:
    """Falsifier: accepting a slot index the double buffer cannot have, which
    would mean the acknowledgement is not about our flip."""
    source = started(VectorLoader(drained_slot_report=2))
    with pytest.raises(FileSourceError, match="drained slot"):
        source.drain(Receipt("iod", 1, 1))


# ---------------------------------------------------------------------------
# Acknowledgement, quiescence and clear failures
# ---------------------------------------------------------------------------


def test_drain_rejects_an_incomplete_acknowledgement() -> None:
    """Falsifier: mapping ``complete != 1`` to ``FileDrain(complete=True)`` —
    reporting a slot that was not fully cleared as a complete measurement."""
    source = started(VectorLoader(complete=0))
    with pytest.raises(FileSourceError, match="not acknowledged"):
        source.drain(Receipt("iod", 1, 1))
    assert source.available is False


def test_drain_rejects_an_error_status_with_a_complete_flag() -> None:
    """Falsifier: treating ``complete == 1`` as sufficient, so a loader that
    says ERROR but forgot to clear the completion flag is believed."""
    source = started(VectorLoader(status_report=STATUS_ERROR))
    with pytest.raises(FileSourceError, match="not acknowledged"):
        source.drain(Receipt("iod", 1, 1))
    assert source.available is False


def test_drain_rejects_a_nonzero_error_beside_a_ready_status() -> None:
    """Falsifier: ignoring the errno field when status and completion both look
    healthy, which would discard the loader's own failure report."""
    source = started(VectorLoader(error_report=-errno.EIO))
    with pytest.raises(FileSourceError) as caught:
        source.drain(Receipt("iod", 1, 1))
    assert caught.value.errno == -errno.EIO
    assert "Input/output error" in str(caught.value)


def test_drain_rejects_a_quiescence_failure_instead_of_an_empty_drain() -> None:
    """Falsifier: reporting a timeout as ``events == ()``, which renders
    unmeasured file activity as zero."""
    loader = VectorLoader(quiescence_failure=-errno.ETIMEDOUT,
                          diagnostic=b"wait_for_quiescence expired")
    source = started(loader)

    with pytest.raises(FileSourceError) as caught:
        source.drain(Receipt("iod", 1, 1))
    assert caught.value.errno == -errno.ETIMEDOUT
    assert "Connection timed out" in str(caught.value)
    assert caught.value.verifier == "wait_for_quiescence expired"
    assert source.available is False
    assert source.availability.file_io is False
    assert "Connection timed out" in source.diagnostic

    with pytest.raises(FileSourceError):
        source.drain(Receipt("iod", 1, 2))
    assert loader.flip_requests == [(1, 1)]


def test_drain_rejects_a_clear_failure() -> None:
    """Falsifier: returning the records the loader managed to enumerate before
    its clear failed, which presents partial data as a complete drain."""
    loader = VectorLoader(clear_failure=-errno.EIO, diagnostic=b"map delete failed")
    loader.queue(0, events=[raw_record()])
    source = started(loader)

    with pytest.raises(FileSourceError) as caught:
        source.drain(Receipt("iod", 1, 1))
    assert caught.value.errno == -errno.EIO
    assert caught.value.verifier == "map delete failed"
    assert source.available is False
    assert loader.slots[0][0] == []


def test_drain_reports_hook_hits_and_skipped_counters_with_no_events() -> None:
    """Falsifier: dropping the stats payload when a slot holds no file event,
    which would lose hook-hit and skipped evidence."""
    stats = make_stats(cgroup_id=77, hook_hits=HookHits(1, 2, 3, 4, 5, 6),
                       skipped_nonfile=90210, skipped_unsupported_fs=7)
    loader = VectorLoader()
    loader.queue(0, stats=[stats])
    source = started(loader)

    drain = source.drain(Receipt("iod", 1, 1))
    assert drain.events == ()
    assert drain.stats == (stats,)
    assert drain.stats[0].hook_hits.vfs_iter_write == 6
    assert drain.stats[0].skipped_nonfile == 90210
    assert drain.stats[0].skipped_unsupported_fs == 7


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def test_drain_passes_the_exact_receipt_to_the_loader() -> None:
    """Falsifier: dropping the caller's source/epoch/seq instead of echoing it
    onto the command and into the returned drain."""
    receipt = Receipt("iod", 7, 42)
    loader = VectorLoader()
    source = started(loader)
    drain = source.drain(receipt)
    assert loader.flip_requests == [(7, 42)]
    assert drain.receipt == receipt
    assert drain.receipt.source == "iod"


def test_drain_requires_the_response_to_echo_the_receipt() -> None:
    """Falsifier: accepting an acknowledgement for another (epoch, seq), which
    would attach someone else's records to this receipt."""
    source = started(VectorLoader(echo_epoch=8))
    with pytest.raises(FileSourceError, match="echo"):
        source.drain(Receipt("iod", 7, 42))

    source = started(VectorLoader(echo_seq=41))
    with pytest.raises(FileSourceError, match="echo"):
        source.drain(Receipt("iod", 7, 42))


def test_drain_requires_strictly_increasing_receipts() -> None:
    """Falsifier: accepting a duplicate or a rewound receipt, which would replay
    a drain into an hour that already counted it."""
    loader = VectorLoader()
    source = started(loader)
    source.drain(Receipt("iod", 2, 5))

    for stale in (Receipt("iod", 2, 5), Receipt("iod", 2, 4), Receipt("iod", 1, 9)):
        with pytest.raises(FileSourceError, match="advance"):
            source.drain(stale)

    assert source.available is True
    assert loader.flip_requests == [(2, 5)]

    source.drain(Receipt("iod", 3, 0))
    assert loader.flip_requests == [(2, 5), (3, 0)]


def test_restart_does_not_reset_receipt_monotonicity() -> None:
    """Falsifier: clearing the acknowledged receipt on start(), which lets a
    restarted source replay an old drain."""
    loader = VectorLoader()
    source = started(loader)
    source.drain(Receipt("iod", 4, 9))
    loader.hook_mask = 0
    assert source.start().file_io is False
    loader.hook_mask = HOOK_MASK_ALL
    assert source.start().file_io is True

    with pytest.raises(FileSourceError, match="advance"):
        source.drain(Receipt("iod", 4, 9))


# ---------------------------------------------------------------------------
# Diagnostics and ABI
# ---------------------------------------------------------------------------


def test_drain_propagates_errno_and_verifier_text_bounded() -> None:
    """Falsifier: keeping only the message (bounded, therefore truncated) and
    losing the errno or the full verifier text the administrator needs."""
    text = b"E" * DIAGNOSTIC_CAP
    loader = VectorLoader(quiescence_failure=-errno.EACCES, diagnostic=text)
    source = started(loader)
    with pytest.raises(FileSourceError) as caught:
        source.drain(Receipt("iod", 1, 1))
    assert caught.value.errno == -errno.EACCES
    assert "Permission denied" in str(caught.value)
    assert caught.value.verifier == text.decode("utf-8")
    assert len(str(caught.value)) <= DIAGNOSTIC_CAP


def test_drain_rejects_an_oversized_drain_diagnostic() -> None:
    """Falsifier: trusting a declared diagnostic length, which lets the loader
    choose our allocation and the administrator-facing text length."""
    loader = VectorLoader(quiescence_failure=-errno.ETIMEDOUT,
                          diagnostic=b"d" * (DIAGNOSTIC_CAP + 1))
    source = started(loader)
    with pytest.raises(FileSourceError) as caught:
        source.drain(Receipt("iod", 1, 1))
    assert caught.value.errno == -errno.ETIMEDOUT
    assert "exceeds" in str(caught.value)


def test_drain_rejects_a_bad_abi_on_the_drain_response() -> None:
    """Falsifier: decoding a drain response from an ABI we do not implement."""
    source = started(VectorLoader(drain_abi_version=ABI_VERSION + 1))
    with pytest.raises(FileSourceError) as caught:
        source.drain(Receipt("iod", 1, 1))
    assert caught.value.errno == -errno.EPROTO
    assert "ABI" in str(caught.value)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_drain_before_start_and_after_close_raises() -> None:
    """Falsifier: an unstarted or closed source answering with an empty drain."""
    source = BpfFileSource(VectorLoader())
    with pytest.raises(FileSourceError, match="not been started"):
        source.drain(Receipt("iod", 1, 1))

    assert source.start().file_io is True
    assert source.close() is None
    with pytest.raises(FileSourceError, match="closed"):
        source.drain(Receipt("iod", 1, 1))


def test_close_is_idempotent_and_reports_a_failed_close() -> None:
    """Falsifier: a close that swallows a refusal (the loader still attached), or
    one that can be called twice and send two CLOSE commands."""
    loader = VectorLoader(close_failure=-errno.EBUSY, diagnostic=b"drain in progress")
    source = started(loader)
    with pytest.raises(FileSourceError) as caught:
        source.close()
    assert caught.value.errno == -errno.EBUSY
    assert caught.value.verifier == "drain in progress"

    assert source.close() is None
    assert loader.close_requests == 1
    with pytest.raises(FileSourceError):
        source.drain(Receipt("iod", 1, 1))


def test_close_before_start_sends_nothing() -> None:
    """Falsifier: a close that talks to a loader this source never attached,
    consuming a response that belongs to a different phase."""
    loader = VectorLoader()
    source = BpfFileSource(loader)
    assert source.close() is None
    assert loader.close_requests == 0
    assert source.close() is None


def test_close_after_a_successful_drain_sends_one_close() -> None:
    """Falsifier: a close that never reaches the loader, leaving it attached."""
    loader = VectorLoader()
    source = started(loader)
    loader.queue(0, events=[raw_record()])
    source.drain(Receipt("iod", 1, 1))

    assert source.close() is None
    assert source.close() is None
    assert loader.close_requests == 1
    assert loader.closed is True


# ---------------------------------------------------------------------------
# The real byte transport
# ---------------------------------------------------------------------------


def test_stream_loader_speaks_the_fixed_command_stream() -> None:
    """Falsifier: any command header, opcode, payload length, or byte order that
    differs from what ``fileio_loader.c`` reads."""
    writer = io.BytesIO()
    loader = StreamLoader(io.BytesIO(b""), writer)
    with pytest.raises(LoaderFrameError):
        loader.hello()
    assert writer.getvalue() == COMMAND_HEADER.pack(ABI_VERSION, OP_HELLO, 0)

    writer = io.BytesIO()
    loader = StreamLoader(io.BytesIO(b""), writer)
    with pytest.raises(LoaderFrameError):
        loader.flip_drain(7, 42)
    assert writer.getvalue() == (
        COMMAND_HEADER.pack(ABI_VERSION, OP_FLIP_DRAIN, FLIP_REQUEST.size)
        + FLIP_REQUEST.pack(7, 42)
    )

    writer = io.BytesIO()
    loader = StreamLoader(io.BytesIO(b""), writer)
    with pytest.raises(LoaderFrameError):
        loader.close()
    assert writer.getvalue() == COMMAND_HEADER.pack(ABI_VERSION, OP_CLOSE, 0)


def test_stream_loader_reports_premature_end_of_stream() -> None:
    """Falsifier: a read that treats a short read as a whole frame, so a killed
    loader looks like a well-formed empty response."""
    loader = StreamLoader(io.BytesIO(b"\x01\x00\x00"), io.BytesIO())
    with pytest.raises(LoaderFrameError, match="ended"):
        loader.hello()


def test_stream_loader_refuses_an_out_of_bound_declared_payload() -> None:
    """Falsifier: sizing a read from an unvalidated loader count."""
    header = DRAIN_RESPONSE.pack(
        ABI_VERSION, STATUS_OK, HOOK_MASK_ALL, MAX_MAP_ENTRIES, 0, 0, 1, 1,
        MAX_MAP_ENTRIES + 1, 0, DRAIN_COMPLETE, 0,
    )
    loader = StreamLoader(io.BytesIO(header), io.BytesIO())
    with pytest.raises(LoaderFrameError, match="above the"):
        loader.flip_drain(1, 1)


def test_stream_loader_drives_the_adapter_over_real_bytes() -> None:
    """Falsifier: an adapter that only works against the in-memory shim, whose
    framing differs from what the loader writes to a pipe."""
    shim = VectorLoader()
    shim.queue(0, events=[raw_record()], stats=[raw_stats()])
    frames = b"".join([shim.hello(), shim.flip_drain(7, 42), shim.close()])

    writer = io.BytesIO()
    source = BpfFileSource(StreamLoader(io.BytesIO(frames), writer))
    assert source.start() == FileAvailability(file_io=True, bpf_loaded=True, diagnostic=None)

    drain = source.drain(Receipt("iod", 7, 42))
    assert [event.dev for event in drain.events] == ["259:0"]
    assert [item.cgroup_id for item in drain.stats] == [1001]
    assert drain.complete is True

    assert source.close() is None
    assert writer.getvalue() == b"".join([
        COMMAND_HEADER.pack(ABI_VERSION, OP_HELLO, 0),
        COMMAND_HEADER.pack(ABI_VERSION, OP_FLIP_DRAIN, FLIP_REQUEST.size)
        + FLIP_REQUEST.pack(7, 42),
        COMMAND_HEADER.pack(ABI_VERSION, OP_CLOSE, 0),
    ])


# ---------------------------------------------------------------------------
# Rootless, dependency and provenance guards
# ---------------------------------------------------------------------------


def test_module_imports_only_rootless_dependencies() -> None:
    """Falsifier: the adapter reaching for a privilege, process-spawn, or
    test-shim module, which would make the rootless tests describe something
    other than the code that runs."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            imported.add(node.module.split(".")[0])
            if node.level:
                raise AssertionError("bpf_fileio must use absolute imports")
    allowed = {"__future__", "errno", "os", "re", "struct", "dataclasses",
               "typing", "appio", "tests"}
    assert imported <= allowed, f"unexpected imports: {sorted(imported - allowed)}"
    assert "tests" not in imported


def test_vector_loader_is_defined_in_the_production_module() -> None:
    """Falsifier: production code importing its test shim instead of shipping the
    protocol-compatible in-memory implementation the daemon may inject."""
    assert VectorLoader.__module__ == "appio.bpf_fileio"
    assert StreamLoader.__module__ == "appio.bpf_fileio"
    assert FileSourceError.__module__ == "appio.bpf_fileio"
