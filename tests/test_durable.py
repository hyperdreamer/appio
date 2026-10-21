"""Canonical durable JSON: exact fixtures, malformed rejection, round-trips.

The three fixtures below are copied byte for byte from the signed
specification section 5.4 (fenced canonical examples) and the version-1
sampler checkpoint derived from the handover fixture. A separate test
re-derives their canonical form with ``json.dumps`` so a typo in a literal
cannot hide behind the implementation it is meant to pin.
"""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3

import pytest

from appio.durable import (
    Credit,
    DeviceBaseline,
    FileEntry,
    FileHourApp,
    FileHourCoverage,
    FileHourState,
    HandoverState,
    ProcessCheckpoint,
    ProcessRef,
    Receipt,
    SamplerCheckpointRow,
    SamplerCheckpointState,
    UnknownOwnerCoverage,
    UnknownOwnerState,
    canonical_json,
    decode_file_hour_state,
    decode_handover,
    decode_sampler_checkpoint,
)
from appio.contracts import SQLITE_INT_MAX, HookHits
from appio.migrations import migrate_empty


BOOT_ID = "11111111-2222-4333-8444-555555555555"
CUTOFF = 120_000_000_000
HOUR = 3_600_000_000_000

# Specification section 5.4, fenced "canonical non-empty handover fixture".
HANDOVER_FIXTURE = (
    '{"boot_id":"11111111-2222-4333-8444-555555555555","cutoff_mono_ns":120000000000,'
    '"devices":[{"cgroup_id":42,"dev":"259:0","rbytes":500,"wbytes":20}],'
    '"processes":[{"app_key":"unit:app-demo.service","baseline":{"rchar":120,'
    '"read_bytes":10,"wchar":30,"write_bytes":5},"credits":[],"launch_id":3,'
    '"launch_root":{"pid":10,"start_ticks":7},"parent":null,"pid":10,"ppid":1,'
    '"start_ticks":7}],"version":1}'
)

# Version-1 sampler checkpoint derived from the handover fixture: the same
# live process and device baseline, one FIFO credit, and the row columns the
# root must agree with (source gui, epoch 2, seq/last_seq 9, cutoff time).
CHECKPOINT_FIXTURE = (
    '{"boot_id":"11111111-2222-4333-8444-555555555555",'
    '"checkpoint_mono_ns":120000000000,'
    '"devices":[{"cgroup_id":42,"dev":"259:0","rbytes":500,"wbytes":20}],'
    '"epoch":2,"last_seq":9,'
    '"processes":[{"app_key":"unit:app-demo.service","baseline":{"rchar":120,'
    '"read_bytes":10,"wchar":30,"write_bytes":5},"credits":[{"child":{"pid":11,'
    '"start_ticks":9},"correction_counted":1,"disappearance_epoch":1,'
    '"disappearance_seq":4,"remaining":{"rchar":7,"read_bytes":1,"wchar":2,'
    '"write_bytes":1}}],"launch_id":3,"launch_root":{"pid":10,"start_ticks":7},'
    '"parent":null,"pid":10,"ppid":1,"start_ticks":7}],"source":"gui","version":1}'
)

# Specification section 5.4, fenced "canonical non-empty pending-hour fixture".
FILE_HOUR_FIXTURE = (
    '{"apps":[{"app_key":"unit:app-demo.service","coverage":{"complete":1,'
    '"counted_omitted_bytes_r":0,"counted_omitted_bytes_w":0,'
    '"counted_omitted_files":0,"gap_intervals":0,"hook_hits":{"vfs_iter_read":1,'
    '"vfs_iter_write":0,"vfs_read":2,"vfs_readv":0,"vfs_write":0,"vfs_writev":0},'
    '"overlap_uncertain_intervals":0,"skipped_nonfile":0,"skipped_unsupported_fs":0,'
    '"uncounted_overflow_bytes_r":0,"uncounted_overflow_bytes_w":0,'
    '"uncounted_overflow_files":0,"weak_identity_rows":0},"files":[{"aliases":'
    '["b.txt"],"basename":"a.txt","bytes_r":40,"bytes_w":10,"dev":"259:0",'
    '"fs_uuid":"1111-aaaa","generation":7,"identity_weak":0,"inode":42,'
    '"last_seen_mono_ns":120000000000,"reads":2,"state":"resolved","writes":1}],'
    '"last_receipt":{"epoch":2,"seq":9,"source":"gui"}}],'
    '"boot_id":"11111111-2222-4333-8444-555555555555","hour_start_mono_ns":0,'
    '"unknown_owner":{"coverage":{"complete":1,"gap_intervals":0,'
    '"overlap_uncertain_intervals":0},"last_receipt":null,"overflow_bytes_r":0,'
    '"overflow_bytes_w":0,"overflow_files":0},"version":1}'
)

CHECKPOINT_ROW = SamplerCheckpointRow(
    source="gui",
    boot_id=BOOT_ID,
    epoch=2,
    seq=9,
    checkpoint_mono_ns=CUTOFF,
)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _handover(**changes: object) -> str:
    parsed = json.loads(HANDOVER_FIXTURE)
    parsed.update(changes)
    return _canonical(parsed)


def _process(parsed: dict) -> dict:
    return parsed["processes"][0]


def _device(parsed: dict) -> dict:
    return parsed["devices"][0]


def _credit(parsed: dict) -> dict:
    return _process(parsed)["credits"][0]


def _file_hour(**changes: object) -> str:
    parsed = json.loads(FILE_HOUR_FIXTURE)
    parsed.update(changes)
    return _canonical(parsed)


def _app(parsed: dict) -> dict:
    return parsed["apps"][0]


def _file(parsed: dict) -> dict:
    return _app(parsed)["files"][0]


def _coverage(parsed: dict) -> dict:
    return _app(parsed)["coverage"]


# --------------------------------------------------------------------------
# The fixtures are canonical independently of the implementation under test.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture", [HANDOVER_FIXTURE, CHECKPOINT_FIXTURE, FILE_HOUR_FIXTURE]
)
def test_spec_fixtures_are_canonical_by_an_independent_encoder(fixture: str) -> None:
    assert _canonical(json.loads(fixture)) == fixture
    assert not fixture.endswith("\n")


def test_canonical_json_sorts_keys_and_uses_compact_separators() -> None:
    assert canonical_json({"z": 1, "a": {"y": 2, "b": 3}}) == '{"a":{"b":3,"y":2},"z":1}'


def test_canonical_json_preserves_utf8_and_adds_no_newline() -> None:
    text = canonical_json({"name": "café.txt"})
    assert text == '{"name":"café.txt"}'
    assert b"caf\xc3\xa9" in text.encode("utf-8")
    assert not text.endswith("\n")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1.0, 1e9])
def test_canonical_json_rejects_floats_and_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})


def test_canonical_json_rejects_non_json_values() -> None:
    with pytest.raises(TypeError):
        canonical_json({"value": object()})


# --------------------------------------------------------------------------
# Handover: typed round-trip and the specification fixture.
# --------------------------------------------------------------------------


def test_decode_handover_returns_the_typed_canonical_state() -> None:
    state = decode_handover(HANDOVER_FIXTURE, BOOT_ID, CUTOFF)

    assert isinstance(state, HandoverState)
    assert (state.version, state.boot_id, state.cutoff_mono_ns) == (1, BOOT_ID, CUTOFF)
    (process,) = state.processes
    assert isinstance(process, ProcessCheckpoint)
    assert (process.pid, process.start_ticks, process.ppid) == (10, 7, 1)
    assert process.parent is None
    assert process.launch_root == ProcessRef(pid=10, start_ticks=7)
    assert process.app_key == "unit:app-demo.service"
    assert process.launch_id == 3
    assert process.baseline == (120, 30, 10, 5)
    assert process.credits == ()
    (device,) = state.devices
    assert device == DeviceBaseline(cgroup_id=42, dev="259:0", rbytes=500, wbytes=20)
    assert canonical_json(state) == HANDOVER_FIXTURE


def test_handover_credit_vector_is_ordered_by_the_declared_fifo_key() -> None:
    parsed = json.loads(HANDOVER_FIXTURE)
    later = {
        "child": {"pid": 12, "start_ticks": 3},
        "correction_counted": 0,
        "disappearance_epoch": 2,
        "disappearance_seq": 0,
        "remaining": {"rchar": 0, "read_bytes": 0, "wchar": 0, "write_bytes": 0},
    }
    earlier = dict(later)
    earlier["disappearance_epoch"] = 1
    _process(parsed)["credits"] = [earlier, later]
    text = _canonical(parsed)

    state = decode_handover(text, BOOT_ID, CUTOFF)
    assert [credit.disappearance_epoch for credit in state.processes[0].credits] == [1, 2]
    assert state.processes[0].credits[0].remaining == (0, 0, 0, 0)
    assert state.processes[0].credits[0].correction_counted == 0
    assert isinstance(state.processes[0].credits[0], Credit)


def test_handover_credit_order_uses_the_full_fifo_tie_break_key() -> None:
    parsed = json.loads(HANDOVER_FIXTURE)

    def credit(child_pid: int, start_ticks: int, epoch: int, seq: int) -> dict:
        return {
            "child": {"pid": child_pid, "start_ticks": start_ticks},
            "correction_counted": 0,
            "disappearance_epoch": epoch,
            "disappearance_seq": seq,
            "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
        }

    _process(parsed)["credits"] = [
        credit(11, 1, 1, 0),
        credit(12, 1, 1, 0),
        credit(12, 2, 1, 0),
        credit(12, 2, 1, 1),
    ]
    text = _canonical(parsed)
    state = decode_handover(text, BOOT_ID, CUTOFF)
    assert [credit_.order_key for credit_ in state.processes[0].credits] == [
        (1, 0, 11, 1),
        (1, 0, 12, 1),
        (1, 0, 12, 2),
        (1, 1, 12, 2),
    ]
    assert canonical_json(state) == text


def test_handover_accepts_correctly_ordered_processes_and_devices() -> None:
    parsed = json.loads(HANDOVER_FIXTURE)
    second_process = json.loads(json.dumps(_process(parsed)))
    second_process.update(pid=12, start_ticks=8, ppid=10)
    parsed["processes"].append(second_process)
    parsed["devices"].insert(0, {"cgroup_id": 7, "dev": "8:0", "rbytes": 0, "wbytes": 0})
    text = _canonical(parsed)

    state = decode_handover(text, BOOT_ID, CUTOFF)
    assert [process.identity for process in state.processes] == [(10, 7), (12, 8)]
    assert [device.order_key for device in state.devices] == [(7, b"8:0"), (42, b"259:0")]
    assert canonical_json(state) == text


def test_handover_and_checkpoint_accept_a_null_baseline_and_launch_root() -> None:
    parsed = json.loads(HANDOVER_FIXTURE)
    _process(parsed)["baseline"] = None
    _process(parsed)["launch_root"] = None
    text = _canonical(parsed)
    state = decode_handover(text, BOOT_ID, CUTOFF)
    assert state.processes[0].baseline is None
    assert state.processes[0].launch_root is None
    assert canonical_json(state) == text

    checkpoint = json.loads(CHECKPOINT_FIXTURE)
    _process(checkpoint)["baseline"] = None
    _process(checkpoint)["launch_root"] = None
    checkpoint_text = _canonical(checkpoint)
    restored = decode_sampler_checkpoint(checkpoint_text, CHECKPOINT_ROW)
    assert restored.processes[0].baseline is None
    assert restored.processes[0].launch_root is None


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_handover_rejects_nonfinite_number_literals(token: str) -> None:
    text = HANDOVER_FIXTURE.replace("120000000000", token, 1)
    with pytest.raises(ValueError):
        decode_handover(text, BOOT_ID, CUTOFF)


def test_handover_rejects_a_mismatched_boot_even_when_the_object_is_valid() -> None:
    with pytest.raises(ValueError):
        decode_handover(HANDOVER_FIXTURE, "99999999-8888-4777-8666-555555555555", CUTOFF)


def test_handover_rejects_a_mismatched_cutoff_even_when_the_object_is_valid() -> None:
    with pytest.raises(ValueError):
        decode_handover(HANDOVER_FIXTURE, BOOT_ID, CUTOFF + 1)


def test_handover_requires_noncanonical_input_to_be_rejected() -> None:
    pretty = json.dumps(json.loads(HANDOVER_FIXTURE), indent=2)
    with pytest.raises(ValueError):
        decode_handover(pretty, BOOT_ID, CUTOFF)
    with pytest.raises(ValueError):
        decode_handover(HANDOVER_FIXTURE + "\n", BOOT_ID, CUTOFF)


def _handover_variants() -> list:
    def mutate(change):
        parsed = json.loads(HANDOVER_FIXTURE)
        change(parsed)
        return _canonical(parsed)

    cases = {
        "missing-top-key": mutate(lambda p: p.pop("devices")),
        "extra-top-key": mutate(lambda p: p.update(extra=1)),
        "version-boolean": mutate(lambda p: p.update(version=True)),
        "version-two": mutate(lambda p: p.update(version=2)),
        "version-string": mutate(lambda p: p.update(version="1")),
        "cutoff-negative": mutate(lambda p: p.update(cutoff_mono_ns=-1)),
        "cutoff-boolean": mutate(lambda p: p.update(cutoff_mono_ns=True)),
        "cutoff-out-of-range": mutate(
            lambda p: p.update(cutoff_mono_ns=SQLITE_INT_MAX + 1)
        ),
        "processes-object": mutate(lambda p: p.update(processes={})),
        "devices-object": mutate(lambda p: p.update(devices={})),
        "process-missing-key": mutate(lambda p: _process(p).pop("launch_id")),
        "process-extra-key": mutate(lambda p: _process(p).update(extra=0)),
        "pid-zero": mutate(lambda p: _process(p).update(pid=0)),
        "pid-boolean": mutate(lambda p: _process(p).update(pid=True)),
        "pid-float-token": HANDOVER_FIXTURE.replace('"pid":10', '"pid":10.0', 1),
        "start-ticks-negative": mutate(lambda p: _process(p).update(start_ticks=-1)),
        "start-ticks-boolean": mutate(lambda p: _process(p).update(start_ticks=True)),
        "ppid-negative": mutate(lambda p: _process(p).update(ppid=-5)),
        "parent-missing-key": mutate(lambda p: _process(p).update(parent={"pid": 10})),
        "parent-extra-key": mutate(
            lambda p: _process(p).update(parent={"pid": 10, "start_ticks": 7, "x": 1})
        ),
        "parent-boolean-pid": mutate(
            lambda p: _process(p).update(parent={"pid": True, "start_ticks": 7})
        ),
        "launch-root-object-replaced": mutate(
            lambda p: _process(p).update(launch_root=[10, 7])
        ),
        "launch-id-negative": mutate(lambda p: _process(p).update(launch_id=-1)),
        "launch-id-boolean": mutate(lambda p: _process(p).update(launch_id=False)),
        "baseline-missing-key": mutate(
            lambda p: _process(p).update(baseline={"rchar": 120, "read_bytes": 10, "wchar": 30})
        ),
        "baseline-extra-key": mutate(
            lambda p: _process(p)["baseline"].update(extra=1)
        ),
        "baseline-boolean": mutate(
            lambda p: _process(p)["baseline"].update(rchar=True)
        ),
        "baseline-negative": mutate(
            lambda p: _process(p)["baseline"].update(write_bytes=-1)
        ),
        "baseline-out-of-range": mutate(
            lambda p: _process(p)["baseline"].update(rchar=SQLITE_INT_MAX + 1)
        ),
        "credits-object": mutate(lambda p: _process(p).update(credits={})),
        "credit-missing-key": mutate(
            lambda p: _process(p).update(
                credits=[
                    {
                        "child": {"pid": 11, "start_ticks": 9},
                        "disappearance_epoch": 1,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    }
                ]
            )
        ),
        "credit-remaining-boolean": mutate(
            lambda p: _process(p).update(
                credits=[
                    {
                        "child": {"pid": 11, "start_ticks": 9},
                        "correction_counted": 0,
                        "disappearance_epoch": 1,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": True, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    }
                ]
            )
        ),
        "credit-correction-boolean": mutate(
            lambda p: _process(p).update(
                credits=[
                    {
                        "child": {"pid": 11, "start_ticks": 9},
                        "correction_counted": True,
                        "disappearance_epoch": 1,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    }
                ]
            )
        ),
        "credit-epoch-zero": mutate(
            lambda p: _process(p).update(
                credits=[
                    {
                        "child": {"pid": 11, "start_ticks": 9},
                        "correction_counted": 0,
                        "disappearance_epoch": 0,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    }
                ]
            )
        ),
        "credit-child-missing-key": mutate(
            lambda p: _process(p).update(
                credits=[
                    {
                        "child": {"pid": 11},
                        "correction_counted": 0,
                        "disappearance_epoch": 1,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    }
                ]
            )
        ),
        "device-missing-key": mutate(lambda p: _device(p).pop("wbytes")),
        "device-extra-key": mutate(lambda p: _device(p).update(extra=1)),
        "device-cgroup-negative": mutate(lambda p: _device(p).update(cgroup_id=-1)),
        "device-cgroup-boolean": mutate(lambda p: _device(p).update(cgroup_id=True)),
        "device-dev-empty": mutate(lambda p: _device(p).update(dev="")),
        "device-dev-object": mutate(lambda p: _device(p).update(dev={"name": "259:0"})),
        "device-rbytes-boolean": mutate(lambda p: _device(p).update(rbytes=True)),
        "device-rbytes-out-of-range": mutate(
            lambda p: _device(p).update(rbytes=SQLITE_INT_MAX + 1)
        ),
        "processes-unordered": mutate(
            lambda p: p.update(
                processes=[
                    dict(_process(p), pid=20),
                    dict(_process(p), pid=10),
                ]
            )
        ),
        "process-duplicate-identity": mutate(
            lambda p: p.update(processes=[_process(p), _process(p)])
        ),
        "devices-unordered": mutate(
            lambda p: p.update(
                devices=[
                    dict(_device(p), cgroup_id=43),
                    dict(_device(p), cgroup_id=42),
                ]
            )
        ),
        "devices-duplicate-identity": mutate(
            lambda p: p.update(devices=[_device(p), _device(p)])
        ),
        "duplicate-credit": mutate(
            lambda p: _process(p).update(
                credits=[
                    {
                        "child": {"pid": 11, "start_ticks": 9},
                        "correction_counted": 0,
                        "disappearance_epoch": 1,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    }
                ]
                * 2
            )
        ),
        "credits-unordered": mutate(
            lambda p: _process(p).update(
                credits=[
                    {
                        "child": {"pid": 11, "start_ticks": 9},
                        "correction_counted": 0,
                        "disappearance_epoch": 2,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    },
                    {
                        "child": {"pid": 12, "start_ticks": 9},
                        "correction_counted": 0,
                        "disappearance_epoch": 1,
                        "disappearance_seq": 0,
                        "remaining": {"rchar": 1, "read_bytes": 1, "wchar": 1, "write_bytes": 1},
                    },
                ]
            )
        ),
        "noncanonical-spacing": json.dumps(json.loads(HANDOVER_FIXTURE)),
        "trailing-newline": HANDOVER_FIXTURE + "\n",
        "duplicate-keys": HANDOVER_FIXTURE.replace(
            '{"boot_id"', '{"version":1,"boot_id"', 1
        ),
        "root-array": "[]",
        "root-string": '"handover"',
        "nan-constant": HANDOVER_FIXTURE.replace("120000000000", "NaN", 1),
    }
    for key in ["unit:", "bogus:x", "unit:a/b", "unit:a%2Fb", "exe:relative/path"]:
        cases[f"invalid-app-key-{key}"] = mutate(
            lambda p, key=key: _process(p).update(app_key=key)
        )
    return [pytest.param(text, id=case) for case, text in cases.items()]


@pytest.mark.parametrize("text", _handover_variants())
def test_handover_rejects_malformed_objects(text: str) -> None:
    with pytest.raises(ValueError):
        decode_handover(text, BOOT_ID, CUTOFF)


def test_handover_rejects_duplicate_json_object_keys_before_validation() -> None:
    # The duplicate carries the *same* value, so a last-wins parser would
    # produce a structurally valid object whose canonical re-encoding still
    # differs only by the dropped duplicate. The parser itself must reject the
    # duplicate rather than letting the canonical check carry the case.
    text = HANDOVER_FIXTURE.replace(
        '"boot_id":"11111111-2222-4333-8444-555555555555"',
        '"boot_id":"11111111-2222-4333-8444-555555555555",'
        '"boot_id":"11111111-2222-4333-8444-555555555555"',
        1,
    )
    with pytest.raises(ValueError, match="duplicate"):
        decode_handover(text, BOOT_ID, CUTOFF)


def test_handover_rejects_an_unsorted_but_valid_object() -> None:
    parsed = json.loads(HANDOVER_FIXTURE)
    reversed_root = dict(reversed(list(parsed.items())))
    text = json.dumps(reversed_root, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(ValueError):
        decode_handover(text, BOOT_ID, CUTOFF)


# --------------------------------------------------------------------------
# Sampler checkpoint: exact row columns plus the handover entry schemas.
# --------------------------------------------------------------------------


def test_decode_sampler_checkpoint_matches_its_row_and_round_trips() -> None:
    state = decode_sampler_checkpoint(CHECKPOINT_FIXTURE, CHECKPOINT_ROW)

    assert isinstance(state, SamplerCheckpointState)
    assert (state.version, state.source, state.epoch) == (1, "gui", 2)
    assert (state.last_seq, state.checkpoint_mono_ns) == (9, CUTOFF)
    assert canonical_json(state) == CHECKPOINT_FIXTURE
    (credit,) = state.processes[0].credits
    assert credit.disappearance_epoch == 1
    assert credit.disappearance_seq == 4
    assert credit.child == ProcessRef(pid=11, start_ticks=9)
    assert credit.remaining == (7, 2, 1, 1)
    assert credit.correction_counted == 1


def test_checkpoint_row_may_be_a_mapping_or_sqlite_row() -> None:
    row_dict = {
        "source": "gui",
        "boot_id": BOOT_ID,
        "epoch": 2,
        "seq": 9,
        "checkpoint_mono_ns": CUTOFF,
    }
    assert decode_sampler_checkpoint(CHECKPOINT_FIXTURE, row_dict).last_seq == 9

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT 'gui' AS source, ? AS boot_id, 2 AS epoch, 9 AS seq, "
            "? AS checkpoint_mono_ns",
            (BOOT_ID, CUTOFF),
        )
        sqlite_row = cursor.fetchone()
    assert decode_sampler_checkpoint(CHECKPOINT_FIXTURE, sqlite_row).epoch == 2


@pytest.mark.parametrize(
    "row",
    [
        SamplerCheckpointRow("gui", BOOT_ID, 1, 9, CUTOFF),
        SamplerCheckpointRow("gui", BOOT_ID, 2, 8, CUTOFF),
        SamplerCheckpointRow("gui", BOOT_ID, 2, 9, CUTOFF + 1),
        SamplerCheckpointRow("gui", "99999999-8888-4777-8666-555555555555", 2, 9, CUTOFF),
        SamplerCheckpointRow("iod", BOOT_ID, 2, 9, CUTOFF),
    ],
    ids=["wrong-epoch", "wrong-seq", "wrong-checkpoint-time", "wrong-boot", "wrong-source"],
)
def test_checkpoint_rejects_a_state_that_disagrees_with_its_row(
    row: SamplerCheckpointRow,
) -> None:
    with pytest.raises(ValueError):
        decode_sampler_checkpoint(CHECKPOINT_FIXTURE, row)


@pytest.mark.parametrize(
    "row",
    [
        {"source": "gui", "boot_id": BOOT_ID, "epoch": 2, "seq": 9},
        {
            "source": "gui",
            "boot_id": "",
            "epoch": 2,
            "seq": 9,
            "checkpoint_mono_ns": CUTOFF,
        },
    ],
    ids=["missing-column", "empty-boot"],
)
def test_checkpoint_rejects_an_invalid_expected_row(row: object) -> None:
    with pytest.raises(ValueError):
        decode_sampler_checkpoint(CHECKPOINT_FIXTURE, row)


def _checkpoint_variants() -> list:
    def mutate(change):
        parsed = json.loads(CHECKPOINT_FIXTURE)
        change(parsed)
        return _canonical(parsed)

    cases = {
        "missing-top-key": mutate(lambda p: p.pop("source")),
        "extra-top-key": mutate(lambda p: p.update(extra=1)),
        "source-invalid": mutate(lambda p: p.update(source="bpf")),
        "epoch-zero": mutate(lambda p: p.update(epoch=0)),
        "epoch-boolean": mutate(lambda p: p.update(epoch=True)),
        "last-seq-negative": mutate(lambda p: p.update(last_seq=-1)),
        "last-seq-null": mutate(lambda p: p.update(last_seq=None)),
        "checkpoint-negative": mutate(lambda p: p.update(checkpoint_mono_ns=-1)),
        "process-entry-missing-key": mutate(lambda p: _process(p).pop("app_key")),
        "processes-object": mutate(lambda p: p.update(processes={})),
        "noncanonical-spacing": json.dumps(json.loads(CHECKPOINT_FIXTURE)),
    }
    return [pytest.param(text, id=case) for case, text in cases.items()]


@pytest.mark.parametrize("text", _checkpoint_variants())
def test_checkpoint_rejects_malformed_objects(text: str) -> None:
    with pytest.raises(ValueError):
        decode_sampler_checkpoint(text, CHECKPOINT_ROW)


# --------------------------------------------------------------------------
# File-hour state: typed round-trip, ordering, coverage and unknown owner.
# --------------------------------------------------------------------------


def test_decode_file_hour_state_returns_the_typed_canonical_state() -> None:
    state = decode_file_hour_state(FILE_HOUR_FIXTURE, BOOT_ID, 0)

    assert isinstance(state, FileHourState)
    assert (state.version, state.boot_id, state.hour_start_mono_ns) == (1, BOOT_ID, 0)
    assert canonical_json(state) == FILE_HOUR_FIXTURE

    (application,) = state.apps
    assert isinstance(application, FileHourApp)
    assert application.app_key == "unit:app-demo.service"
    assert application.last_receipt == Receipt(source="gui", epoch=2, seq=9)

    (entry,) = application.files
    assert isinstance(entry, FileEntry)
    assert (entry.fs_uuid, entry.dev, entry.inode, entry.generation) == (
        "1111-aaaa",
        "259:0",
        42,
        7,
    )
    assert entry.identity_weak == 0
    assert (entry.basename, entry.aliases) == ("a.txt", ("b.txt",))
    assert (entry.state, entry.reads, entry.writes) == ("resolved", 2, 1)
    assert (entry.bytes_r, entry.bytes_w, entry.last_seen_mono_ns) == (40, 10, CUTOFF)

    coverage = application.coverage
    assert isinstance(coverage, FileHourCoverage)
    assert coverage.complete == 1
    assert coverage.counted_omitted_files == 0
    assert coverage.hook_hits == HookHits(
        vfs_read=2, vfs_write=0, vfs_readv=0, vfs_writev=0, vfs_iter_read=1, vfs_iter_write=0
    )

    unknown = state.unknown_owner
    assert isinstance(unknown, UnknownOwnerState)
    assert (unknown.overflow_files, unknown.overflow_bytes_r, unknown.overflow_bytes_w) == (
        0,
        0,
        0,
    )
    assert unknown.last_receipt is None
    assert unknown.coverage == UnknownOwnerCoverage(
        gap_intervals=0, overlap_uncertain_intervals=0, complete=1
    )


def test_file_hour_rejects_a_mismatched_boot_or_hour() -> None:
    with pytest.raises(ValueError):
        decode_file_hour_state(FILE_HOUR_FIXTURE, "99999999-8888-4777-8666-555555555555", 0)
    with pytest.raises(ValueError):
        decode_file_hour_state(FILE_HOUR_FIXTURE, BOOT_ID, HOUR)


def test_file_hour_requires_an_hour_aligned_key() -> None:
    parsed = json.loads(FILE_HOUR_FIXTURE)
    parsed["hour_start_mono_ns"] = 1
    text = _canonical(parsed)
    with pytest.raises(ValueError):
        decode_file_hour_state(text, BOOT_ID, 1)


def test_file_hour_accepts_an_empty_app_list() -> None:
    parsed = json.loads(FILE_HOUR_FIXTURE)
    parsed["apps"] = []
    text = _canonical(parsed)
    state = decode_file_hour_state(text, BOOT_ID, 0)
    assert state.apps == ()
    assert canonical_json(state) == text


def test_file_hour_preserves_utf8_basenames_and_rejects_ascii_escapes() -> None:
    text = FILE_HOUR_FIXTURE.replace('"basename":"a.txt"', '"basename":"café.txt"', 1)
    text = text.replace('"aliases":["b.txt"]', '"aliases":["déjà.txt"]', 1)
    state = decode_file_hour_state(text, BOOT_ID, 0)
    assert state.apps[0].files[0].basename == "café.txt"
    assert canonical_json(state) == text
    assert "café.txt" in canonical_json(state)

    escaped = json.dumps(json.loads(text), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    assert "\\u00e9" in escaped
    with pytest.raises(ValueError):
        decode_file_hour_state(escaped, BOOT_ID, 0)


def test_file_hour_accepts_correctly_ordered_apps_and_files() -> None:
    parsed = json.loads(FILE_HOUR_FIXTURE)
    first_app = json.loads(json.dumps(_app(parsed)))
    first_app["app_key"] = "unit:app-demo.service"
    second_app = json.loads(json.dumps(first_app))
    second_app["app_key"] = "unit:z.service"
    parsed["apps"] = [first_app, second_app]

    first_file = json.loads(json.dumps(_file(parsed)))
    second_file = json.loads(json.dumps(first_file))
    second_file.update(dev="259:1", basename="c.txt", aliases=["d.txt"])
    parsed["apps"][0]["files"] = [first_file, second_file]
    text = _canonical(parsed)

    state = decode_file_hour_state(text, BOOT_ID, 0)
    assert [app.app_key for app in state.apps] == ["unit:app-demo.service", "unit:z.service"]
    assert [entry.identity[:2] for entry in state.apps[0].files] == [
        (b"1111-aaaa", b"259:0"),
        (b"1111-aaaa", b"259:1"),
    ]
    assert canonical_json(state) == text


def test_file_hour_accepts_an_unknown_owner_receipt() -> None:
    parsed = json.loads(FILE_HOUR_FIXTURE)
    parsed["unknown_owner"]["last_receipt"] = {"source": "iod", "epoch": 1, "seq": 0}
    text = _canonical(parsed)
    state = decode_file_hour_state(text, BOOT_ID, 0)
    assert state.unknown_owner.last_receipt == Receipt(source="iod", epoch=1, seq=0)


def _file_hour_variants() -> list:
    def mutate(change):
        parsed = json.loads(FILE_HOUR_FIXTURE)
        change(parsed)
        return _canonical(parsed)

    def apps_with_keys(parsed, keys):
        base = json.loads(json.dumps(parsed["apps"][0]))
        entries = []
        for key in keys:
            copy = json.loads(json.dumps(base))
            copy["app_key"] = key
            entries.append(copy)
        parsed["apps"] = entries

    def files_with_devs(parsed, devs):
        base = json.loads(json.dumps(parsed["apps"][0]["files"][0]))
        entries = []
        for dev in devs:
            copy = json.loads(json.dumps(base))
            copy["dev"] = dev
            entries.append(copy)
        parsed["apps"][0]["files"] = entries

    cases = {
        "missing-top-key": mutate(lambda p: p.pop("unknown_owner")),
        "extra-top-key": mutate(lambda p: p.update(extra=1)),
        "apps-object": mutate(lambda p: p.update(apps={})),
        "version-boolean": mutate(lambda p: p.update(version=True)),
        "hour-start-negative": mutate(lambda p: p.update(hour_start_mono_ns=-HOUR)),
        "hour-start-boolean": mutate(lambda p: p.update(hour_start_mono_ns=True)),
        "app-missing-key": mutate(lambda p: _app(p).pop("last_receipt")),
        "app-extra-key": mutate(lambda p: _app(p).update(extra=1)),
        "app-key-invalid": mutate(lambda p: _app(p).update(app_key="unit:")),
        "apps-duplicate-key": mutate(
            lambda p: apps_with_keys(p, ["unit:app-demo.service", "unit:app-demo.service"])
        ),
        "apps-unordered": mutate(
            lambda p: apps_with_keys(p, ["unit:z.service", "unit:app-demo.service"])
        ),
        "files-object": mutate(lambda p: _app(p).update(files={})),
        "file-missing-key": mutate(lambda p: _file(p).pop("aliases")),
        "file-extra-key": mutate(lambda p: _file(p).update(extra=1)),
        "file-duplicate-identity": mutate(
            lambda p: files_with_devs(p, ["259:0", "259:0"])
        ),
        "files-unordered": mutate(lambda p: files_with_devs(p, ["259:1", "259:0"])),
        "aliases-object": mutate(lambda p: _file(p).update(aliases={"b.txt": 1})),
        "aliases-unsorted": mutate(lambda p: _file(p).update(aliases=["c.txt", "b.txt"])),
        "aliases-duplicate": mutate(lambda p: _file(p).update(aliases=["b.txt", "b.txt"])),
        "aliases-contain-basename": mutate(lambda p: _file(p).update(aliases=["a.txt"])),
        "aliases-less-than-basename": mutate(lambda p: _file(p).update(aliases=["0.txt"])),
        "basename-empty": mutate(lambda p: _file(p).update(basename="")),
        "basename-object": mutate(lambda p: _file(p).update(basename={"name": "a.txt"})),
        "fs-uuid-object": mutate(lambda p: _file(p).update(fs_uuid=[])),
        "dev-empty": mutate(lambda p: _file(p).update(dev="")),
        "inode-negative": mutate(lambda p: _file(p).update(inode=-1)),
        "inode-boolean": mutate(lambda p: _file(p).update(inode=True)),
        "generation-out-of-range": mutate(lambda p: _file(p).update(generation=2**32)),
        "identity-weak-two": mutate(lambda p: _file(p).update(identity_weak=2)),
        "identity-weak-boolean": mutate(lambda p: _file(p).update(identity_weak=True)),
        "file-state-invalid": mutate(lambda p: _file(p).update(state="missing")),
        "reads-boolean": mutate(lambda p: _file(p).update(reads=True)),
        "writes-negative": mutate(lambda p: _file(p).update(writes=-1)),
        "bytes-out-of-range": mutate(lambda p: _file(p).update(bytes_r=SQLITE_INT_MAX + 1)),
        "last-seen-negative": mutate(lambda p: _file(p).update(last_seen_mono_ns=-1)),
        "last-seen-before-hour": mutate(lambda p: _file(p).update(last_seen_mono_ns=HOUR)),
        "last-seen-boolean": mutate(lambda p: _file(p).update(last_seen_mono_ns=False)),
        "coverage-missing-key": mutate(lambda p: _coverage(p).pop("complete")),
        "coverage-extra-key": mutate(lambda p: _coverage(p).update(extra=0)),
        "coverage-object-replaced": mutate(lambda p: _app(p).update(coverage=[])),
        "coverage-complete-boolean": mutate(lambda p: _coverage(p).update(complete=True)),
        "coverage-complete-two": mutate(lambda p: _coverage(p).update(complete=2)),
        "coverage-negative": mutate(lambda p: _coverage(p).update(gap_intervals=-1)),
        "hook-hits-missing-key": mutate(lambda p: _coverage(p)["hook_hits"].pop("vfs_read")),
        "hook-hits-extra-key": mutate(lambda p: _coverage(p)["hook_hits"].update(extra=1)),
        "hook-hits-boolean": mutate(lambda p: _coverage(p)["hook_hits"].update(vfs_read=True)),
        "hook-hits-object": mutate(lambda p: _coverage(p).update(hook_hits=[])),
        "last-receipt-missing-key": mutate(lambda p: _app(p)["last_receipt"].pop("seq")),
        "last-receipt-extra-key": mutate(lambda p: _app(p)["last_receipt"].update(extra=1)),
        "last-receipt-source-invalid": mutate(
            lambda p: _app(p)["last_receipt"].update(source="bpf")
        ),
        "last-receipt-epoch-zero": mutate(lambda p: _app(p)["last_receipt"].update(epoch=0)),
        "last-receipt-seq-boolean": mutate(
            lambda p: _app(p)["last_receipt"].update(seq=False)
        ),
        "unknown-owner-null": mutate(lambda p: p.update(unknown_owner=None)),
        "unknown-owner-array": mutate(lambda p: p.update(unknown_owner=[])),
        "unknown-owner-missing-key": mutate(lambda p: p["unknown_owner"].pop("coverage")),
        "unknown-owner-extra-key": mutate(lambda p: p["unknown_owner"].update(extra=1)),
        "unknown-owner-counter-negative": mutate(
            lambda p: p["unknown_owner"].update(overflow_files=-1)
        ),
        "unknown-owner-coverage-missing-key": mutate(
            lambda p: p["unknown_owner"]["coverage"].pop("complete")
        ),
        "unknown-owner-coverage-extra-key": mutate(
            lambda p: p["unknown_owner"]["coverage"].update(extra=1)
        ),
        "unknown-owner-coverage-complete-boolean": mutate(
            lambda p: p["unknown_owner"]["coverage"].update(complete=True)
        ),
        "unknown-owner-receipt-array": mutate(
            lambda p: p["unknown_owner"].update(last_receipt=[])
        ),
        "unknown-owner-receipt-missing-key": mutate(
            lambda p: p["unknown_owner"].update(last_receipt={"source": "gui", "epoch": 1})
        ),
        "noncanonical-spacing": json.dumps(json.loads(FILE_HOUR_FIXTURE)),
        "trailing-newline": FILE_HOUR_FIXTURE + "\n",
        "duplicate-keys": FILE_HOUR_FIXTURE.replace(
            '{"apps"', '{"version":1,"apps"', 1
        ),
        "apps-instead-of-object": '{"apps":[]}',
    }
    return [pytest.param(text, id=case) for case, text in cases.items()]


@pytest.mark.parametrize("text", _file_hour_variants())
def test_file_hour_rejects_malformed_objects(text: str) -> None:
    with pytest.raises(ValueError):
        decode_file_hour_state(text, BOOT_ID, 0)


# --------------------------------------------------------------------------
# Rejection happens without touching a database the caller already holds.
# --------------------------------------------------------------------------


def test_malformed_durable_json_cannot_mutate_an_existing_store() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        migrate_empty(connection)
        connection.execute(
            "INSERT INTO boot (boot_id, boot_order, started_mono_ns, started_wall, source) "
            "VALUES (?, 1, 0, 0.0, 'gui')",
            (BOOT_ID,),
        )
        connection.execute(
            "INSERT INTO collector_epoch (source, boot_id, epoch, last_seq, allocated_mono_ns) "
            "VALUES ('gui', ?, 1, 0, 0)",
            (BOOT_ID,),
        )
        connection.commit()
        before = list(connection.iterdump())

        malformed = [
            HANDOVER_FIXTURE.replace('"version":1', '"version":2', 1),
            FILE_HOUR_FIXTURE.replace('"complete":1', '"complete":true', 1),
            '{"boot_id":"broken"}',
        ]
        for text in malformed:
            with pytest.raises(ValueError):
                decode_handover(text, BOOT_ID, CUTOFF)
            with pytest.raises(ValueError):
                decode_file_hour_state(text, BOOT_ID, 0)
        assert list(connection.iterdump()) == before
    finally:
        connection.close()
