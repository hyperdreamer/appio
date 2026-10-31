"""A selected app's measured emptiness is distinct from an empty selector."""
from appio.contracts import Window
from appio.durable import Receipt
from appio.file_store import FileHourStore
from appio.files import HourlyAccumulator
from tests.p4.files_tab_support import A, B, BOOT, H, S, read_files, tab_store
from tests.p4.files_tab_view_cases import result, show
from test_files import ZERO, observation


def test_complete_empty_is_per_application_not_unavailable_or_no_apps(qtbot):
    # Kills unavailable/no-app/partial replacement of this terminal outcome.
    value = result(requested=S, served=S, gap=0, requested_gaps=0,
                   complete=True, rows=(), observed=(0, 0))
    view = show(qtbot, value)
    scope = ("served interval: T0=2h 0m 0s through T1=4h 0m 0s after boot; "
             "requested window: T0=2h 0m 0s through T1=4h 0m 0s after boot; "
             "this application's served-hour gap evidence in served interval: 0; "
             "this application's requested-window coverage scalar: 0")
    assert view.footer_state.text() == (
        f"no retained file activity for this application in the served interval; {scope}; coverage: complete")
    assert view.state == "ready"
    assert view.last_result is value
    assert view.model.rowCount() == 0
    assert view.app_selector.count() == 2
    assert "no applications observed" not in view.footer_state.text()
    assert "0 B read" in view.footer_observed.text()
    assert "counted omissions" in view.footer_counted.text()
    assert all(label.text() != "unavailable" for label in view._footer_labels())


def test_committed_coverage_only_app_hour_is_empty_while_other_app_is_busy(tab_store, qtbot):
    # Existing producer API explicitly permits None for zero-F app evidence.
    db = tab_store(hours=(), gaps=(), end=H)
    acc = HourlyAccumulator(BOOT, 0)
    acc.add_observation(Receipt("gui", 1, 1), A, None, ZERO)
    acc.add_observation(Receipt("gui", 1, 1), B,
                        observation(42, app_key=B, bytes_r=17), ZERO)
    FileHourStore(db).finalize_hour(acc.finalize())
    db.commit()
    assert not db.in_transaction
    window = Window("range", BOOT, 0, H)
    empty = read_files((("gui", db, "gui"),), window, A)
    busy = read_files((("gui", db, "gui"),), window, B, 102)
    assert len(busy.table.rows) == 1
    assert busy.coverage.observed_bytes_r == 17
    assert empty.table.rows == ()
    assert empty.coverage.coverage.complete is True
    assert empty.coverage.served_interval == window
    assert empty.served_gap_count == 0
    assert (empty.coverage.observed_bytes_r, empty.coverage.observed_bytes_w) == (0, 0)
    view = show(qtbot, empty)
    scope = ("served interval: T0=0s through T1=1h 0m 0s after boot; "
             "requested window: T0=0s through T1=1h 0m 0s after boot; "
             "this application's served-hour gap evidence in served interval: 0; "
             "this application's requested-window coverage scalar: 0")
    assert view.footer_state.text() == (
        f"no retained file activity for this application in the served interval; {scope}; coverage: complete")
    assert view.state == "ready"
    assert view.app_selector.count() == 2
    assert view.model.rowCount() == 0
    assert "no applications observed" not in view.footer_state.text()
