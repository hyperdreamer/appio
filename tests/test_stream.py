"""Collector epoch allocation and the high-water frame receipt.

Specification section 3.4 and A08: an epoch is allocated transactionally per
source and boot (``previous + 1`` in the same boot, otherwise ``1``) with
``last_seq=NULL``, and a frame whose source/boot/epoch is not active or whose
sequence is at or below the committed high-water mark is rejected *before* any
baseline or database mutation.
"""

from __future__ import annotations

import sqlite3

import pytest

from appio.contracts import SQLITE_INT_MAX
from appio.durable import (
    SamplerCheckpointRow,
    canonical_json,
    decode_sampler_checkpoint,
)
from appio.migrations import migrate_empty
from appio.stream import (
    CollectorEpoch,
    FrameIdentity,
    accept_frame,
    allocate_epoch,
)

BOOT_A = "11111111-2222-4333-8444-555555555555"
BOOT_B = "99999999-8888-4777-8666-555555555555"
UNSEEN_BOOT = "77777777-6666-4555-8444-333333333333"
TS = 120_000_000_000


@pytest.fixture()
def connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    try:
        migrate_empty(connection)
        for boot_id, order in ((BOOT_A, 1), (BOOT_B, 2)):
            connection.execute(
                "INSERT INTO boot (boot_id, boot_order, started_mono_ns, started_wall, source) "
                "VALUES (?, ?, 0, 0.0, 'gui')",
                (boot_id, order),
            )
        connection.commit()
        yield connection
    finally:
        connection.close()


def _epoch_row(connection: sqlite3.Connection, source: str = "gui"):
    return connection.execute(
        "SELECT source, boot_id, epoch, last_seq, allocated_mono_ns "
        "FROM collector_epoch WHERE source = ?",
        (source,),
    ).fetchone()


def _dump(connection: sqlite3.Connection) -> list[str]:
    connection.commit()
    return list(connection.iterdump())


# --------------------------------------------------------------------------
# Epoch allocation
# --------------------------------------------------------------------------


def test_first_allocation_starts_at_epoch_one_with_a_null_high_water(
    connection: sqlite3.Connection,
) -> None:
    epoch = allocate_epoch(connection, "gui", BOOT_A, TS)

    assert epoch == CollectorEpoch(
        source="gui", boot_id=BOOT_A, epoch=1, last_seq=None, allocated_mono_ns=TS
    )
    assert _epoch_row(connection) == ("gui", BOOT_A, 1, None, TS)


def test_same_boot_restart_increments_the_epoch_and_resets_the_high_water(
    connection: sqlite3.Connection,
) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is True
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 1)) is True
    assert _epoch_row(connection) == ("gui", BOOT_A, 1, 1, TS)

    restarted = allocate_epoch(connection, "gui", BOOT_A, TS + 10)

    assert restarted == CollectorEpoch(
        source="gui", boot_id=BOOT_A, epoch=2, last_seq=None, allocated_mono_ns=TS + 10
    )
    assert _epoch_row(connection) == ("gui", BOOT_A, 2, None, TS + 10)
    # Sequence numbering restarts at zero for the new epoch.
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 2, 0)) is True
    assert _epoch_row(connection) == ("gui", BOOT_A, 2, 0, TS + 10)


def test_a_new_boot_restarts_at_epoch_one(connection: sqlite3.Connection) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    allocate_epoch(connection, "gui", BOOT_A, TS)

    epoch = allocate_epoch(connection, "gui", BOOT_B, TS)

    assert epoch.epoch == 1
    assert epoch.last_seq is None
    assert _epoch_row(connection) == ("gui", BOOT_B, 1, None, TS)


def test_sources_allocate_their_own_epochs(connection: sqlite3.Connection) -> None:
    assert allocate_epoch(connection, "gui", BOOT_A, TS).epoch == 1
    assert allocate_epoch(connection, "iod", BOOT_A, TS).epoch == 1
    assert allocate_epoch(connection, "gui", BOOT_A, TS).epoch == 2
    assert _epoch_row(connection, "iod") == ("iod", BOOT_A, 1, None, TS)
    assert _epoch_row(connection, "gui") == ("gui", BOOT_A, 2, None, TS)


def test_allocation_participates_in_a_caller_transaction(
    connection: sqlite3.Connection,
) -> None:
    connection.execute("BEGIN")
    try:
        allocate_epoch(connection, "gui", BOOT_A, TS)
        assert connection.in_transaction is True
        assert _epoch_row(connection) is not None
    finally:
        connection.rollback()

    assert _epoch_row(connection) is None


def test_allocation_rolls_back_when_the_boot_is_unknown(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        allocate_epoch(connection, "gui", UNSEEN_BOOT, TS)
    assert _epoch_row(connection) is None


def test_allocation_does_not_wrap_at_the_integer_maximum(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        "INSERT INTO collector_epoch (source, boot_id, epoch, last_seq, allocated_mono_ns) "
        "VALUES ('gui', ?, ?, NULL, ?)",
        (BOOT_A, SQLITE_INT_MAX, TS),
    )
    connection.commit()
    before = _dump(connection)

    with pytest.raises(ValueError):
        allocate_epoch(connection, "gui", BOOT_A, TS + 1)

    assert _epoch_row(connection) == ("gui", BOOT_A, SQLITE_INT_MAX, None, TS)
    assert _dump(connection) == before


@pytest.mark.parametrize(
    "source, boot_id, allocated_mono_ns",
    [
        ("bpf", BOOT_A, TS),
        ("", BOOT_A, TS),
        ("gui", "", TS),
        ("gui", BOOT_A, -1),
        ("gui", BOOT_A, True),
        ("gui", BOOT_A, SQLITE_INT_MAX + 1),
        ("gui", BOOT_A, "now"),
    ],
    ids=[
        "unknown-source",
        "empty-source",
        "empty-boot",
        "negative-time",
        "boolean-time",
        "out-of-range-time",
        "text-time",
    ],
)
def test_allocation_rejects_invalid_identity_arguments(
    connection: sqlite3.Connection,
    source: object,
    boot_id: object,
    allocated_mono_ns: object,
) -> None:
    with pytest.raises(ValueError):
        allocate_epoch(connection, source, boot_id, allocated_mono_ns)
    assert _epoch_row(connection) is None


# --------------------------------------------------------------------------
# High-water acceptance and rejection
# --------------------------------------------------------------------------


def test_the_first_frame_starts_at_sequence_zero(connection: sqlite3.Connection) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)

    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is True
    assert _epoch_row(connection) == ("gui", BOOT_A, 1, 0, TS)


def test_duplicate_and_stale_frames_are_rejected_without_mutation(
    connection: sqlite3.Connection,
) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is True
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 2)) is True
    before = _dump(connection)

    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is False
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 1)) is False
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 2)) is False

    assert _epoch_row(connection) == ("gui", BOOT_A, 1, 2, TS)
    assert _dump(connection) == before
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 3)) is True


def test_a_higher_sequence_advances_the_high_water(connection: sqlite3.Connection) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is True
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 2)) is True
    assert _epoch_row(connection) == ("gui", BOOT_A, 1, 2, TS)
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 1)) is False


def test_inactive_foreign_and_superseded_frames_are_rejected(
    connection: sqlite3.Connection,
) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    before = _dump(connection)

    # No epoch has been allocated for this source.
    assert accept_frame(connection, FrameIdentity(BOOT_A, "iod", 1, 0)) is False
    # A different boot is not the active boot.
    assert accept_frame(connection, FrameIdentity(BOOT_B, "gui", 1, 0)) is False
    # A superseded epoch is stale even though its boot is active.
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 2, 0)) is False

    assert _dump(connection) == before

    # After a same-boot restart the preceding epoch's frames are superseded.
    allocate_epoch(connection, "gui", BOOT_A, TS + 1)
    restarted = _dump(connection)
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is False
    assert _dump(connection) == restarted


def test_sources_keep_independent_high_water_marks(
    connection: sqlite3.Connection,
) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    allocate_epoch(connection, "iod", BOOT_A, TS)

    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is True
    assert accept_frame(connection, FrameIdentity(BOOT_A, "iod", 1, 0)) is True
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is False
    assert accept_frame(connection, FrameIdentity(BOOT_A, "iod", 1, 0)) is False
    assert _epoch_row(connection, "gui") == ("gui", BOOT_A, 1, 0, TS)
    assert _epoch_row(connection, "iod") == ("iod", BOOT_A, 1, 0, TS)


def test_a_rejected_frame_leaves_a_sentinel_baseline_untouched(
    connection: sqlite3.Connection,
) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is True
    connection.execute(
        "INSERT INTO sampler_checkpoint "
        "(source, boot_id, epoch, seq, checkpoint_mono_ns, state_json) "
        "VALUES ('gui', ?, 1, 0, ?, ?)",
        (BOOT_A, TS, '{"sentinel":"baseline"}'),
    )
    connection.commit()

    rejected = _dump(connection)
    assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is False
    assert _dump(connection) == rejected

    row = connection.execute(
        "SELECT state_json FROM sampler_checkpoint WHERE source = 'gui'"
    ).fetchone()
    assert row == ('{"sentinel":"baseline"}',)


def test_acceptance_participates_in_a_caller_transaction(
    connection: sqlite3.Connection,
) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    connection.execute("BEGIN")
    try:
        assert accept_frame(connection, FrameIdentity(BOOT_A, "gui", 1, 0)) is True
        assert _epoch_row(connection) == ("gui", BOOT_A, 1, 0, TS)
    finally:
        connection.rollback()

    assert _epoch_row(connection) == ("gui", BOOT_A, 1, None, TS)


def test_accept_frame_requires_a_frame_identity(connection: sqlite3.Connection) -> None:
    allocate_epoch(connection, "gui", BOOT_A, TS)
    with pytest.raises(TypeError):
        accept_frame(connection, (BOOT_A, "gui", 1, 0))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The same-boot checkpoint restore boundary the frame path depends on.
# --------------------------------------------------------------------------


def test_a_checkpoint_is_restorable_only_for_its_own_boot(
    connection: sqlite3.Connection,
) -> None:
    text = canonical_json(
        {
            "version": 1,
            "boot_id": BOOT_A,
            "source": "gui",
            "epoch": 1,
            "last_seq": 0,
            "checkpoint_mono_ns": TS,
            "processes": [],
            "devices": [],
        }
    )
    same_boot = SamplerCheckpointRow("gui", BOOT_A, 1, 0, TS)
    assert decode_sampler_checkpoint(text, same_boot).boot_id == BOOT_A

    allocate_epoch(connection, "gui", BOOT_B, TS)
    new_boot_row = _epoch_row(connection)
    assert new_boot_row == ("gui", BOOT_B, 1, None, TS)

    with pytest.raises(ValueError):
        decode_sampler_checkpoint(
            text, SamplerCheckpointRow("gui", BOOT_B, 1, 0, TS)
        )


# --------------------------------------------------------------------------
# Typed identities are validated at construction, not at use.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "identity",
    [
        ("", "gui", 1, 0),
        (BOOT_A, "bpf", 1, 0),
        (BOOT_A, "gui", 0, 0),
        (BOOT_A, "gui", True, 0),
        (BOOT_A, "gui", 1, -1),
        (BOOT_A, "gui", 1, True),
        (BOOT_A, "gui", SQLITE_INT_MAX + 1, 0),
    ],
    ids=[
        "empty-boot",
        "unknown-source",
        "epoch-zero",
        "epoch-boolean",
        "seq-negative",
        "seq-boolean",
        "epoch-out-of-range",
    ],
)
def test_frame_identity_rejects_invalid_fields(identity: tuple) -> None:
    with pytest.raises(ValueError):
        FrameIdentity(*identity)


@pytest.mark.parametrize(
    "fields",
    [
        {"source": "bpf", "boot_id": BOOT_A, "epoch": 1, "last_seq": None, "allocated_mono_ns": 0},
        {"source": "gui", "boot_id": "", "epoch": 1, "last_seq": None, "allocated_mono_ns": 0},
        {"source": "gui", "boot_id": BOOT_A, "epoch": 0, "last_seq": None, "allocated_mono_ns": 0},
        {"source": "gui", "boot_id": BOOT_A, "epoch": 1, "last_seq": -1, "allocated_mono_ns": 0},
        {"source": "gui", "boot_id": BOOT_A, "epoch": 1, "last_seq": True, "allocated_mono_ns": 0},
        {"source": "gui", "boot_id": BOOT_A, "epoch": 1, "last_seq": None, "allocated_mono_ns": -1},
    ],
    ids=[
        "unknown-source",
        "empty-boot",
        "epoch-zero",
        "last-seq-negative",
        "last-seq-boolean",
        "negative-time",
    ],
)
def test_collector_epoch_rejects_invalid_fields(fields: dict) -> None:
    with pytest.raises(ValueError):
        CollectorEpoch(**fields)
