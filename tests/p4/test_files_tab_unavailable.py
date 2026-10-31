"""Unavailable file sources must refuse, never publish numeric emptiness.

Contract pins for spec sections 3.2 (outcome 1), 3.3 and 4.3:

* a request with no authoritative candidate is a typed ``WindowUnavailableError``
  refusal, never a zero-row/numeric-empty success;
* the view preserves the refused reason and clears every numeric footer;
* an untyped failure must not fabricate a ``: {reason}`` segment;
* a coverage whose ``served_interval`` is the unavailable placeholder is never
  published as an accepted served scope;
* the empty-application selector state must not outrank an unavailable source.

Falsifiers (one applied mutation each, see the lane report):

* ``M_unavailable_returns_empty_success`` — no candidate becomes an empty success;
* ``M_unavailable_numeric_zero`` — the refused view renders numbers again;
* ``M_untyped_reason_fabricated`` — an untyped failure gains a reason segment;
* ``M_placeholder_is_served`` — a placeholder coverage is published as served;
* ``M_unavailable_hidden_by_no_apps`` — the no-app selector copy hides the refusal.
"""

from __future__ import annotations

import pytest

# Pin PyQt6 before pytest-qt builds its qapp fixture (see tests/test_ui.py).
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from appio.contracts import WindowUnavailableError
from appio.file_runtime import FileAvailability
from tests.p4.files_tab_support import read_files, tab_store
from tests.p4.test_files_view import (
    APPS,
    WINDOW,
    FakeRouter,
    build,
    coverage,
    file_row,
    file_view,
    footer_text,
    unknown_owner,
)

# --------------------------------------------------------------------------- #
# No authoritative source: refusal, not an empty success
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["receipt", "calendar"])
def test_no_authoritative_candidate_is_typed_refusal_not_numeric_empty(
    tab_store, missing
):
    db = tab_store(
        apps=(2,) if missing == "receipt" else (1, 2),
        hours=() if missing == "calendar" else (2, 3),
        app_gaps={},
        gaps=(),
    )
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("local", db, "gui"),))
    if missing == "receipt":
        assert (caught.value.reason, str(caught.value)) == (
            "file_source_unavailable",
            "No authoritative file app receipt.",
        )
    else:
        assert caught.value.reason in {
            "not_retained",
            "file_source_unavailable",
            "unsupported_window",
        }


# --------------------------------------------------------------------------- #
# Outcome 1: reason preserved, every numeric footer cleared
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reason",
    [
        "not_retained",
        "unsupported_window",
        "launch_attribution",
        "file_source_unavailable",
    ],
)
def test_unavailable_view_preserves_reason_and_clears_numeric_results(qtbot, reason):
    router = FakeRouter(
        error=WindowUnavailableError(reason, "synthetic refusal"),
        ranking_rows=APPS,
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state_label.text() == f"file history unavailable: {reason}: synthetic refusal"
    assert view.model.rowCount() == 0
    assert view.last_result is None
    for label in view._footer_labels():
        assert "0 read" not in label.text()
        assert "served interval" not in label.text()


def test_untyped_failure_has_no_fabricated_reason_segment(qtbot):
    router = FakeRouter(error=RuntimeError("synthetic failure"), ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state_label.text() == "file history unavailable: synthetic failure"
    assert view.model.rowCount() == 0
    assert view.last_result is None


def test_placeholder_coverage_with_no_served_scope_never_publishes(qtbot):
    # A successful-looking outer coverage whose nested unknown-owner coverage
    # carries the unavailable reason: ``served_interval`` is the placeholder
    # ``None``, so the view must refuse instead of rendering its rows/zeroes.
    result = file_view(
        (file_row(),),
        unknown_owner_overflow=unknown_owner(
            files=3,
            bytes_r=30,
            bytes_w=10,
            cov=coverage(unavailable_reason="not_retained"),
        ),
    )
    assert result.coverage.served_interval is None
    router = FakeRouter(ranking_rows=APPS, result=result)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state == "unavailable"
    assert "not_retained" in view.state_label.text()
    assert view.last_result is None
    assert view.model.rowCount() == 0
    for label in view._footer_labels():
        assert "0 read" not in label.text()
        assert "served interval" not in label.text()
        assert "retained file" not in label.text()
    assert not any(character.isdigit() for character in footer_text(view)), footer_text(view)


# --------------------------------------------------------------------------- #
# Selector state versus source capability
# --------------------------------------------------------------------------- #


def test_empty_application_selector_remains_separate(qtbot):
    # An available source with no observed applications keeps the selector copy
    # and never issues a per-app query: it is not one of the five outcomes.
    available = FakeRouter(ranking_rows=())
    view = build(qtbot, available)
    view.set_window(WINDOW)
    assert available.requests == []
    assert view.state == "ready"
    assert (
        view.footer_state.text()
        == "coverage: not applicable; no applications observed in this window"
    )
    assert view.model.rowCount() == 0

    # An unavailable capability outranks that selector state: the selector copy
    # is a claim about apps, not about whether the file source could be read.
    unreadable = FakeRouter(
        ranking_rows=(),
        status=FileAvailability(
            False, False, "file_source_unavailable", "bpf load failed: EPERM"
        ),
    )
    refused = build(qtbot, unreadable)
    refused.set_window(WINDOW)
    assert unreadable.requests == []
    assert refused.state == "unavailable"
    assert "bpf load failed: EPERM" in refused.state_label.text()
    assert "no applications observed" not in refused.footer_state.text()
    assert not any(
        character.isdigit() for character in footer_text(refused)
    ), footer_text(refused)
