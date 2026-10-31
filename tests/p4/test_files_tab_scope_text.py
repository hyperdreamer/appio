"""Literal bounds and reducer labels, independent of mutable UI state."""
from dataclasses import replace

import pytest

from appio.contracts import Window, WindowUnavailableError
from appio.query import QueryService
from appio.ui import files_view
from tests.p4.files_tab_support import BOOT, H, R, S, tab_store
from tests.p4.files_tab_view_cases import SCOPE_A, result, show

SB = Window("since_boot", BOOT, end_mono_ns=6*H)
LIFETIME = Window("lifetime")


def test_canonical_partial_range_scope_is_exact():
    # Kills requested bounds substituted for served bounds / swapped reducers.
    value = result()
    assert files_view._scope_text(value.coverage, value.served_gap_count) == SCOPE_A


@pytest.mark.parametrize("requested,served,tier,bounds", [
    (R, R, "raw", "served interval: T0=0s through T1=6h 0m 0s after boot; requested window: T0=0s through T1=6h 0m 0s after boot"),
    (SB, SB, "raw", "served interval: since_boot through T1=6h 0m 0s after boot; requested window: since_boot through T1=6h 0m 0s after boot"),
    (SB, S, "raw", "served interval only: T0=2h 0m 0s through T1=4h 0m 0s after boot; requested window: since_boot through T1=6h 0m 0s after boot"),
    (SB, SB, "totals_only", "served interval: since_boot through T1=6h 0m 0s after boot; requested window: since_boot through T1=6h 0m 0s after boot"),
    (LIFETIME, LIFETIME, "totals_only", "served interval: lifetime; requested window: lifetime"),
])
def test_scope_formats_since_boot_end_lifetime_and_unknown_gap(requested, served, tier, bounds):
    # Kills dropped since_boot end, internal boot label, invented lifetime bounds,
    # and None -> 0; expected text never calls the production formatter.
    value = result(requested=requested, served=served, gap=None, tier=tier)
    assert files_view._scope_text(value.coverage, None) == (
        bounds + "; this application's served-hour gap evidence in served interval: unavailable; "
        "this application's requested-window coverage scalar: 4")
    assert files_view._scope_bounds(SB) == "since_boot through T1=6h 0m 0s after boot"
    assert files_view._scope_bounds(LIFETIME) == "lifetime"


@pytest.mark.parametrize("complete,served,only", [(False, R, False), (True, S, True)])
def test_only_depends_on_scope_inequality_not_completeness_or_rows(complete, served, only):
    # Adversarial controls: incomplete/full and complete/shortened.
    value = result(served=served, complete=complete, gap=0)
    text = files_view._scope_text(value.coverage, 0)
    assert ("served interval only:" in text) is only
    assert "in served interval: 0;" in text


def test_formatter_uses_accepted_result_after_current_request_changes(qtbot):
    # Kills use of self._window when constructing the accepted disclosure.
    value = result()
    view = show(qtbot, value)
    view._window = Window("range", BOOT, 0, H)
    view._publish(value)
    assert SCOPE_A in view.footer_state.text()


def test_unavailable_scope_is_rejected():
    value = result()
    cov = replace(value.coverage, coverage=replace(value.coverage.coverage,
                                                  unavailable_reason="not_retained"))
    with pytest.raises(ValueError, match="no scope disclosure"):
        files_view._scope_text(cov, None)


def test_launch_query_keeps_existing_a22_refusal(tab_store):
    # Kills bypass of the query precheck (already supported, no product red).
    db = tab_store()
    with pytest.raises(WindowUnavailableError) as caught:
        QueryService(db).file_serve_facts(1, Window("launch", BOOT, launch_id=1))
    assert (caught.value.reason, str(caught.value)) == (
        "launch_attribution", "Launch window disabled: device and file activity are not stored per launch.")
