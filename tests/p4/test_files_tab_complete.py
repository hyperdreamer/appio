"""Retained calendars prove zero; incompleteness does not change scope."""
import pytest

from appio.contracts import Window
from appio.query import FileServeFacts, QueryService
from tests.p4.files_tab_support import BOOT, H, read_files, tab_store


@pytest.mark.parametrize("window", [Window("range", BOOT, 0, 2*H),
                                    Window("since_boot", BOOT, end_mono_ns=2*H)])
def test_complete_retained_hours_have_measured_zero_gap_and_equal_scope(tab_store, window):
    # Kills measured-zero -> None and no unknown overflow -> partial.
    db = tab_store(hours=(0, 1), app_gaps={}, gaps=(), end=2*H)
    service = QueryService(db)
    for aid in (1, 2):
        facts = service.file_serve_facts(aid, window)
        assert facts == FileServeFacts(window, 0, 0)
        assert type(facts.gaps_in_served_interval) is int
        assert service.file_coverage(aid, window).coverage.complete is True
    value = read_files((("local", db, "gui"),), window=window)
    assert value.coverage.served_interval == window
    assert value.served_gap_count == 0
    assert type(value.served_gap_count) is int
    assert value.coverage.coverage.complete is True
    assert value.coverage.unknown_owner_overflow.overflow_files == 0


@pytest.mark.parametrize("window", [Window("range", BOOT, 0, 2*H),
                                    Window("since_boot", BOOT, end_mono_ns=2*H)])
def test_full_scope_incomplete_is_not_automatically_shortened(tab_store, window):
    # Kills incompleteness -> shortened effective scope.
    db = tab_store(hours=(0, 1), app_gaps={}, gaps=(), end=2*H)
    db.execute("UPDATE file_coverage SET uncounted_overflow_files=1, "
               "coverage_json=json_set(coverage_json, '$.file.uncounted_overflow_files', 1) "
               "WHERE app_id=1")
    db.commit()
    assert QueryService(db).file_coverage(1, window).served_interval == window
    value = read_files((("local", db, "gui"),), window=window)
    assert value.coverage.coverage.complete is False
    assert value.coverage.served_interval == window
    assert value.served_gap_count == 0
    assert value.coverage.coverage.gap_intervals == 0
    assert QueryService(db).file_serve_facts(1, window) == FileServeFacts(window, 0, 0)
