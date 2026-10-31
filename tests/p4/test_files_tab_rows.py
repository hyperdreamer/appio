"""Rows precede filtered/empty outcomes and preserve the query's result."""
from dataclasses import replace

import pytest

from tests.p4.test_files_view import file_row, texts
from tests.p4.files_tab_support import R
from tests.p4.files_tab_view_cases import SCOPE_A, result, show


@pytest.mark.parametrize("search", [None, "a"])
@pytest.mark.parametrize("complete", [False, True])
def test_rows_outcome_keeps_query_order_and_served_byte_scope(qtbot, search, complete):
    # Kills basename sorting, whole-window bytes, and requiring complete for rows.
    rows = (file_row(basename="zeta", bytes_r=10, bytes_w=0),
            file_row(path_id=2, inode=43, basename="alpha", bytes_r=5, bytes_w=0))
    args = dict(rows=rows, search=search, observed=(15, 0), complete=complete)
    scope = SCOPE_A
    if complete:
        args.update(served=R, gap=0, requested_gaps=0)
        scope = ("served interval: T0=0s through T1=6h 0m 0s after boot; "
                 "requested window: T0=0s through T1=6h 0m 0s after boot; "
                 "this application's served-hour gap evidence in served interval: 0; "
                 "this application's requested-window coverage scalar: 0")
    value = result(**args)
    assert (value.table.sort, value.table.direction) == ("bytes_total", "desc")
    view = show(qtbot, value)
    assert [row[0] for row in texts(view)] == ["zeta", "alpha"]
    matching = "" if search is None else "; matching 'a'"
    status = "complete" if complete else "incomplete"
    assert view.footer_state.text() == f"rows retained{matching}; {scope}; coverage: {status}"
    only = "" if complete else " only"
    observed = f"observed (served interval{only}): 15 B read, 0 B written; 2 retained file row(s); {scope}"
    if search is not None:
        observed += "; table filtered to 2 row(s) matching 'a'"
    assert view.footer_observed.text() == observed
    assert "whole window" not in observed


def test_rows_are_not_filtered_again_by_view(qtbot):
    # An independently supplied query result must not be re-filtered locally.
    value = result(rows=(file_row(basename="server-selected"),), search="opaque-query-token")
    view = show(qtbot, value)
    assert [row[0] for row in texts(view)] == ["server-selected"]
    assert view.footer_state.text().startswith("rows retained; matching 'opaque-query-token'; ")


@pytest.mark.parametrize("placeholder", [False, True])
def test_unavailable_guard_precedes_rows_and_search(qtbot, monkeypatch, placeholder):
    # Both row and search predicates match: only the earlier unavailable guard wins.
    value = result(rows=(file_row(),), search="report", complete=True)
    view = show(qtbot, value)
    if placeholder:
        unknown = replace(value.coverage.unknown_owner_overflow,
                          coverage=replace(value.coverage.coverage, unavailable_reason="not_retained"))
        cov = replace(value.coverage, unknown_owner_overflow=unknown)
    else:
        cov = replace(value.coverage,
                      coverage=replace(value.coverage.coverage, unavailable_reason="not_retained"))
    # Isolate admission from the scope formatter's independent validation.
    monkeypatch.setattr(view, "_render_coverage", lambda accepted, window: None)
    view._publish(replace(value, coverage=cov))
    assert view.state == "unavailable"
    assert view.model.rowCount() == 0
    assert view.last_result is None
    assert view.state_label.text().startswith("file history unavailable: not_retained:")
    assert all(label.text() == "unavailable" for label in view._footer_labels())
