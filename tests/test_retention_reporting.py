"""A retention sweep that is merely unfinished must not report a dead daemon.

``answering`` is ``last_error is None`` (``iod.py``), and the console blanks the
whole window when a status frame says ``answering: false``. A backlog batch and a
checkpoint that is busy behind a live read snapshot are both normal, so mapping
them to ``retention_incomplete`` made the console go dark every time it asked a
question -- while samples kept committing on the ten-second cadence.
"""

import pytest

from appio.iod import storage_error_for
from appio.quota import PruneResult, SkippedUnit


def result(complete, reason=None, *, paused=False, skipped=()):
    return PruneResult((), complete, paused, 4096, reason, tuple(skipped))


@pytest.mark.parametrize(
    "retention,expected",
    [
        (result(True), None),
        (result(False, "batch_limit"), None),
        (result(False, "checkpoint_busy"), None),
        (
            result(
                False,
                "missing_materialization",
                skipped=[SkippedUnit("core", ("boot-A", "gui", 1, 0, 1))],
            ),
            "retention_incomplete",
        ),
        (result(False, "missing_clock_mapping"), "retention_incomplete"),
    ],
)
def test_retention_verdict_maps_to_one_storage_error(retention, expected):
    assert storage_error_for(quota=result(True), retention=retention) == expected


def test_a_skipped_unit_is_never_deferred_even_with_a_batch_reason():
    skipped = [SkippedUnit("core", ("boot-A", "gui", 1, 0, 1))]
    retention = result(False, "batch_limit", skipped=skipped)
    assert storage_error_for(quota=result(True), retention=retention) == (
        "retention_incomplete"
    )


def test_quota_pause_outranks_a_deferred_retention_sweep():
    assert (
        storage_error_for(
            quota=result(False, "quota_exhausted", paused=True),
            retention=result(False, "checkpoint_busy"),
        )
        == "quota_exhausted"
    )
