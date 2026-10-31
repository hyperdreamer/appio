from types import SimpleNamespace

import pytest

from appio.contracts import WindowUnavailableError
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry
from appio.query import QueryService
from tests.p4.files_tab_support import tab_store, read_files, BOOT, H, S


@pytest.mark.parametrize("resume,gaps,accepted", [
    (3*H, ((2*H,3*H,"collector_gap"),), True),
    (3*H, ((2*H,5*H//2,"collector_gap"),(5*H//2,3*H,"collector_gap")), True),
    (7*H//2, ((2*H,7*H//2,"collector_gap"),), True),  # PIVOT-1: fully gap-covered, mid-hour endpoint
    (3*H, ((2*H,12*H//5,"collector_gap"),(13*H//5,3*H,"collector_gap")), False),
    (3*H, ((2*H,3*H,"overlap_uncertain"),), False),
])
def test_clipped_unproved_segment_requires_complete_known_gap_union(
        tab_store, resume, gaps, accepted):
    db = tab_store(hours=(2,3), app_gaps={}, gaps=gaps)
    db.execute("DELETE FROM collector_interval")
    db.execute("INSERT INTO collector_interval VALUES('gui',?,1,0,?)", (BOOT,3*H//2))
    db.execute("INSERT INTO collector_interval VALUES('gui',?,2,?,?)", (BOOT,resume,6*H))
    db.commit()

    if resume == 3*H and gaps == ((2*H,3*H,"collector_gap"),):
        worker = HistoryRouter(QueryService(db), SimpleNamespace(state=None), AppRegistry())
        try:
            worker._plan(S)
            assert any((s.start_ns, s.end_ns, s.proven) == (3*H//2,3*H,False)
                       for s in worker._all_segments)
            assert any((s.start_ns, s.end_ns, s.proven) == (2*H,3*H,False)
                       for s in worker.segments)
        finally:
            worker.close()
    if gaps[0][2] == "overlap_uncertain":
        worker = HistoryRouter(QueryService(db), SimpleNamespace(state=None), AppRegistry())
        try:
            worker._plan(S)
            assert worker._known_gap_ranges == []
        finally:
            worker.close()

    if accepted:
        result = read_files((("local",db,"gui"),))
        assert result.coverage.served_interval == S
        assert result.coverage.observed_bytes_r == 70
    else:
        with pytest.raises(WindowUnavailableError) as caught:
            read_files((("local",db,"gui"),))
        assert caught.value.reason == "file_source_unavailable"
        assert str(caught.value) == "File ownership cannot be represented by one store."


def test_unproved_segment_wholly_outside_served_interval_is_irrelevant(tab_store):
    db = tab_store(hours=(2,3), app_gaps={},
        gaps=((4*H,5*H,"collector_gap"),))
    worker = HistoryRouter(QueryService(db), SimpleNamespace(state=None), AppRegistry())
    try:
        worker._plan(S)
        assert any((s.start_ns, s.end_ns, s.proven) == (4*H,5*H,False)
                   for s in worker._all_segments)
        assert all(s.proven for s in worker.segments)
    finally:
        worker.close()

    result = read_files((("local",db,"gui"),))
    assert result.coverage.served_interval == S
    assert result.coverage.observed_bytes_r == 70


def test_provenance_refusal_does_not_stop_candidate_scan(tab_store, monkeypatch):
    first = tab_store("first", hours=(2,3), app_gaps={}, gaps=())
    second = tab_store("second", hours=(2,3), app_gaps={}, gaps=())
    checked = []

    def check(worker, served, supplying_name):
        checked.append(supplying_name)
        if supplying_name == "first":
            raise WindowUnavailableError(
                "file_source_unavailable", "File ownership cannot be represented by one store.")
        return True

    monkeypatch.setattr(HistoryRouter, "_check_file_provenance", staticmethod(check))
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("first",first,"gui"),("second",second,"gui")))

    assert checked == ["first", "second"]
    assert caught.value.reason == "file_source_unavailable"
    assert str(caught.value) == "File ownership cannot be represented by one store."
