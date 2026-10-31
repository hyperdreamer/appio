"""Totals-only/pruned Files scope keeps the requested Window and reports an unknown served count.

Spec §§2.2-2.3 and §8 (totals-only/pruned semantics): after the hourly rows
are pruned, a valid ``since_boot`` or ``lifetime`` aggregate still has the
requested ``Window`` as its served scope and still carries the existing
requested-window coverage scalar.  The served-hour reducer is unmeasurable
from absent hourly rows, so it is ``None`` -- never ``0``.  ``0`` would claim a
measured absence.
"""

import pytest

from appio.contracts import Window
from appio.query import QueryService
from tests.p4.files_tab_support import H, tab_store

BOOT = "boot-A"
SINCE_BOOT = Window("since_boot", BOOT, end_mono_ns=2 * H)
LIFETIME = Window("lifetime")

PRUNED_HOURLY_TABLES = ("file_sample", "file_coverage", "file_overflow_unknown")
REQUESTED_OVERLAY_GAPS = ((H, 2 * H, "collector_gap"),)


def _prune_hourly(db):
    for table in PRUNED_HOURLY_TABLES:
        db.execute(f"DELETE FROM {table}")
    db.commit()
    assert not db.in_transaction


@pytest.mark.parametrize(
    "window", [SINCE_BOOT, LIFETIME], ids=["since_boot", "lifetime"]
)
def test_pruned_totals_keep_requested_scope_and_unknown_served_hour_count(
    tab_store, window
):
    db = tab_store(hours=(0, 1), app_gaps={}, gaps=(), end=2 * H)
    _prune_hourly(db)
    service = QueryService(db)
    with service._snapshot():
        for app_id, expected_bytes in ((1, 30), (2, 60)):
            table = service.file_table(app_id, window, "bytes_total", "desc", None)
            coverage = service.file_coverage(app_id, window)
            facts = service.file_serve_facts(app_id, window)
            assert table.window == coverage.window == window
            assert facts.served_interval == coverage.served_interval == window
            assert facts.served_interval.kind == window.kind
            assert coverage.coverage.tier == "totals_only"
            assert coverage.observed_bytes_r == expected_bytes
            assert facts.gaps_in_served_interval is None
            assert facts.gaps_in_requested_window == coverage.coverage.gap_intervals == 0
        assert db.in_transaction


@pytest.mark.parametrize(
    ("window", "tier_before_pruning"),
    [(SINCE_BOOT, "raw"), (LIFETIME, "totals_only")],
    ids=["since_boot", "lifetime"],
)
def test_pruned_totals_preserve_retained_requested_scalar(
    tab_store, window, tier_before_pruning
):
    db = tab_store(
        hours=(0, 1), app_gaps={(1, 0): 4, (1, 1): 2}, gaps=REQUESTED_OVERLAY_GAPS,
        end=2 * H,
    )
    service = QueryService(db)
    retained = service.file_coverage(1, window)
    retained_facts = service.file_serve_facts(1, window)
    assert retained.coverage.tier == tier_before_pruning
    # Six retained app-hour gaps plus the one persisted requested-window overlay.
    assert retained.coverage.gap_intervals == retained_facts.gaps_in_requested_window == 7
    assert retained_facts.gaps_in_served_interval == (
        None if window.kind == "lifetime" else 6
    )

    _prune_hourly(db)
    pruned = service.file_coverage(1, window)
    facts = service.file_serve_facts(1, window)
    assert pruned.coverage.tier == "totals_only"
    assert facts.served_interval == pruned.served_interval == window
    assert facts.gaps_in_served_interval is None
    assert facts.gaps_in_requested_window == pruned.coverage.gap_intervals == 7
