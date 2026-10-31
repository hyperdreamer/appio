"""Filtered zero precedes either unfiltered empty outcome."""
import pytest

from tests.p4.files_tab_view_cases import APPS, FakeRouter, SCOPE_A, build, result, show


@pytest.mark.parametrize("complete", [False, True])
def test_filtered_zero_is_no_matches_with_independent_observed_bytes(qtbot, complete):
    # Complete and incomplete both reach no_matches, never no-activity copy.
    value = result(search="absent", rows=(), observed=(7, 0), complete=complete)
    view = show(qtbot, value)
    status = "complete" if complete else "incomplete"
    assert view.model.rowCount() == 0
    assert view.footer_state.text() == f"no retained file matches 'absent'; {SCOPE_A}; coverage: {status}"
    assert view.footer_observed.text() == (
        f"observed (served interval only): 7 B read, 0 B written; 0 retained file row(s); {SCOPE_A}; "
        "table filtered to 0 row(s) matching 'absent'")
    assert "no retained file activity" not in view.footer_state.text()
    # Accepted query text, not subsequently edited input, owns the disclosure.
    view.search_field.blockSignals(True)
    view.search_field.setText("changed")
    view._publish(value)
    assert "matches 'absent';" in view.footer_state.text()


def test_whitespace_only_search_is_not_a_filter(qtbot):
    # Kills whitespace treated as active; existing normalizer already complies.
    router = FakeRouter(result=result(), ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(router.result.table.window)
    view.search_field.setText(" \t ")
    assert view.search() is None
    assert router.requests[-1][4] is None
    assert "matches" not in view.footer_state.text()
    view.search_field.setText("cafe\u0301")
    assert view.search() == "café"
    assert router.requests[-1][4] == "café"
