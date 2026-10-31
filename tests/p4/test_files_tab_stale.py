"""Prove a retired Files reply cannot publish rows or the facts that describe them.

A ``FileViewResult`` is one committed state: the rows, the compatibility scope on
``coverage``, the requested-window coverage scalar and the served-hour reducer.
If a retired generation could publish any member of that result on its own, the
view would show rows from one query under another query's scope, or keep the
current rows while retiring the facts that describe them -- a state that no
snapshot ever produced.

Each probe defers two requests, drains both deliveries behind a signal barrier
(the stale callback must actually run, not merely be queued) and then asserts
that the published result, the rendered rows and every footer line still belong
to the current request.

Falsifiers (each observed red at the named assertion after the delivery barrier):

* ``M_publish_retired_view_generation`` -- drop the view's generation guard, so a
  retired reply publishes its own rows, scope and reducers.
* ``M_stale_facts_only`` -- keep the generation guard for the rows but render the
  retired result's coverage: rows and ``last_result`` stay current while the
  footer labels describe the retired scope.
* ``M_publish_retired_rows_only`` -- the mirror image: publish the retired rows
  and ``last_result`` but leave the current footer facts in place.
* ``M_settle_retired_error`` -- gate only ``_publish`` on the generation, so a
  retired *failure* still settles as unavailable and clears the current rows and
  facts instead of being discarded.

The production code already retires whole results (``FilesView._on_delivered``
checks the generation before it even extracts the future's result, and
``HistoryRouter._settle_file`` settles a retired future with
``WindowUnavailableError``), so no honest pre-fix red exists for these pins; they
are falsified by the mutants above.
"""

from __future__ import annotations

import pytest

from appio.contracts import Window, WindowUnavailableError
from tests.p4.files_tab_support import BOOT, H, R, S
from tests.p4.files_tab_view_cases import result
from tests.p4.test_files_view import APPS, FakeRouter, build, file_row, texts

#: One hour-aligned window that is distinguishable from ``R`` (0h..6h).
NEXT = Window("range", BOOT, 0, 8 * H)

LOADING = "loading"


def footer_lines(view) -> list[str]:
    """Every populated coverage line, in the order the view ships them."""

    return [
        label.text()
        for label in (
            view.footer_state,
            view.footer_observed,
            view.footer_counted,
            view.footer_uncounted,
            view.footer_unknown_owner,
            view.footer_skipped,
            view.footer_weak,
            view.footer_hooks,
        )
    ]


def deferred_pair(qtbot):
    """Two in-flight queries: ``R`` (retired) and ``NEXT`` (current), in order."""

    router = FakeRouter(defer=True, ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(R)
    first = router.pending[-1]
    view.set_window(NEXT)
    second = router.pending[-1]
    assert first is not second
    assert len(router.pending) == 2
    return view, first, second


def drain_barrier(view) -> list[int]:
    """Record the generation of every delivery so assertions wait for the drain."""

    drained: list[int] = []

    def record(payload) -> None:
        drained.append(payload[0])

    view._bridge.delivered.connect(record)
    return drained


@pytest.mark.parametrize(
    "retired_first",
    [False, True],
    ids=["retired_after_current", "retired_while_pending"],
)
def test_retired_reply_cannot_replace_rows_or_serve_facts(qtbot, retired_first):
    view, first, second = deferred_pair(qtbot)
    current = result(
        requested=NEXT,
        served=S,
        gap=2,
        requested_gaps=5,
        rows=(file_row(basename="current", bytes_r=20, bytes_w=0),),
        observed=(20, 0),
    )
    retired = result(
        rows=(file_row(basename="retired", bytes_r=99, bytes_w=0),),
        gap=1,
        requested_gaps=4,
        observed=(99, 0),
    )
    drained = drain_barrier(view)

    if retired_first:
        first.set_result(retired)
        qtbot.waitUntil(lambda: len(drained) >= 1, timeout=1000)
        # The retired reply drained while the current request is still pending:
        # it must leave the view loading and publish neither rows nor facts.
        assert view.state == "loading"
        assert view.last_result is None
        assert view.model.rowCount() == 0
        assert footer_lines(view) == [LOADING] * 8

    second.set_result(current)
    qtbot.waitUntil(lambda: view.last_result is current, timeout=1000)
    before = footer_lines(view)

    if not retired_first:
        first.set_result(retired)
        qtbot.waitUntil(lambda: len(drained) >= 2, timeout=1000)

    assert len(drained) == 2, "both replies must have drained"
    assert drained[0] != drained[1], "the two replies are different generations"

    # Rows: the model renders exactly the current result's rows, in its order.
    assert view.last_result is current
    assert view.model.rowCount() == len(current.table.rows)
    assert [cells[0] for cells in texts(view)] == [
        row.basename for row in current.table.rows
    ]

    # Facts: the compatibility scope, the requested scalar and the served
    # reducer are the current result's, never the retired one's.
    state_text = view.footer_state.text()
    assert "served-hour gap evidence in served interval: 2" in state_text
    assert "requested-window coverage scalar: 5" in state_text
    assert "T1=8h 0m 0s" in state_text
    observed_text = view.footer_observed.text()
    assert "20 B read" in observed_text
    assert "99 B" not in observed_text
    assert footer_lines(view) == before


def test_retired_reply_cannot_retire_only_the_facts_of_the_current_result(qtbot):
    """Same rows, different facts: unchanged rows cannot hide a stale retirement."""

    view, first, second = deferred_pair(qtbot)
    rows = (file_row(basename="current", bytes_r=20, bytes_w=0),)
    current = result(
        requested=NEXT, served=S, gap=2, requested_gaps=5, rows=rows, observed=(20, 0)
    )
    retired = result(
        requested=R, served=S, gap=1, requested_gaps=4, rows=rows, observed=(20, 0)
    )
    drained = drain_barrier(view)

    second.set_result(current)
    qtbot.waitUntil(lambda: view.last_result is current, timeout=1000)
    before = footer_lines(view)

    first.set_result(retired)
    qtbot.waitUntil(lambda: len(drained) >= 2, timeout=1000)
    assert drained[0] != drained[1]

    assert view.last_result is current
    assert [cells[0] for cells in texts(view)] == [
        row.basename for row in current.table.rows
    ]
    state_text = view.footer_state.text()
    assert "served-hour gap evidence in served interval: 2" in state_text
    assert "served-hour gap evidence in served interval: 1" not in state_text
    assert "requested-window coverage scalar: 5" in state_text
    assert "requested-window coverage scalar: 4" not in state_text
    assert "T1=8h 0m 0s" in state_text
    assert "T1=6h 0m 0s" not in state_text
    assert footer_lines(view) == before


def test_retired_failure_cannot_clear_the_current_rows_or_facts(qtbot):
    """The generation check must run before the future's failure is settled."""

    view, first, second = deferred_pair(qtbot)
    current = result(
        requested=NEXT,
        served=S,
        gap=2,
        requested_gaps=5,
        rows=(file_row(basename="current", bytes_r=20, bytes_w=0),),
        observed=(20, 0),
    )
    drained = drain_barrier(view)

    second.set_result(current)
    qtbot.waitUntil(lambda: view.last_result is current, timeout=1000)
    before = footer_lines(view)

    first.set_exception(
        WindowUnavailableError("file_source_unavailable", "File history generation retired.")
    )
    qtbot.waitUntil(lambda: len(drained) >= 2, timeout=1000)
    assert drained[0] != drained[1]

    assert view.last_result is current
    assert view.state != "unavailable"
    assert view.model.rowCount() == len(current.table.rows)
    assert [cells[0] for cells in texts(view)] == [
        row.basename for row in current.table.rows
    ]
    assert footer_lines(view) == before
