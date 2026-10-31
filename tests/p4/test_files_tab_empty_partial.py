"""Incomplete empty results preserve measured zero and explicit unknown."""
from dataclasses import replace

import pytest

from appio.contracts import Window
from tests.p4.files_tab_support import R, S
from tests.p4.files_tab_view_cases import SCOPE_A, result, show

LIFETIME = Window("lifetime")


@pytest.mark.parametrize("route,gap,requested,served,tier,scope", [
    ("local", 0, R, S, "raw", SCOPE_A.replace("in served interval: 1;", "in served interval: 0;")),
    ("pruned", None, LIFETIME, LIFETIME, "totals_only",
     "served interval: lifetime; requested window: lifetime; this application's served-hour gap evidence in served interval: unavailable; this application's requested-window coverage scalar: 4"),
    ("remote", None, R, S, "raw", SCOPE_A.replace("in served interval: 1;", "in served interval: unavailable;")),
    ("full_non_gap_uncertainty", 0, R, R, "raw",
     "served interval: T0=0s through T1=6h 0m 0s after boot; requested window: T0=0s through T1=6h 0m 0s after boot; this application's served-hour gap evidence in served interval: 0; this application's requested-window coverage scalar: 4"),
])
def test_partial_empty_keeps_both_scopes_and_zero_unknown_distinction(
        qtbot, route, gap, requested, served, tier, scope):
    # Kills zero rows -> complete; missing requested scalar; None -> 0;
    # and using incompleteness instead of scope inequality for 'only'.
    value = result(requested=requested, served=served, gap=gap, rows=(),
                   complete=False, tier=tier, observed=(0, 0))
    if route == "full_non_gap_uncertainty":
        inner = replace(value.coverage.coverage, uncounted_overflow_files=1)
        unknown = replace(value.coverage.unknown_owner_overflow, coverage=inner)
        value = replace(value, table=replace(value.table, coverage=inner),
                        coverage=replace(value.coverage, coverage=inner,
                                         unknown_owner_overflow=unknown))
    view = show(qtbot, value)
    assert view.footer_state.text() == (
        "no retained file activity in the served interval; coverage: incomplete; " + scope)
    assert view.state == "incomplete"
    assert view.model.rowCount() == 0
    assert view.last_result is value
    assert "coverage: complete" not in view.footer_state.text()
    assert "no applications observed" not in view.footer_state.text()
    assert view.state_label.text() == ""
    assert all(label.text() != "unavailable" for label in view._footer_labels())
    if requested == served:
        assert "served interval only" not in view.footer_state.text()
