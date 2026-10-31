from types import SimpleNamespace

from appio.contracts import Window
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry
from appio.query import QueryService
from tests.p4.test_files_view import coverage
from tests.p4.files_tab_support import tab_store, BOOT, H, S


def test_requested_process_gaps_are_not_file_admission_ranges(tab_store):
    R2 = Window("range", BOOT, 2*H, 6*H)
    db = tab_store(hours=(2,3), app_gaps={},
        gaps=((4*H,5*H,"collector_gap"),(0,H,"collector_gap")))
    router = HistoryRouter(QueryService(db), SimpleNamespace(state=None), AppRegistry())
    try:
        router._window = R2
        router._plan(R2)
        assert router._gap_ranges == [(BOOT,4*H,5*H)]
        process = router._combine_coverage([coverage()])
        assert process.gap_intervals == 1
        assert not process.complete
        facts = QueryService(db).file_serve_facts(1, R2)
        assert facts.served_interval == S
        assert facts.gaps_in_served_interval == 0
    finally:
        router.close()


def test_outside_requested_gap_keeps_process_complete(tab_store):
    R2 = Window("range", BOOT, 2*H, 6*H)
    db = tab_store(hours=(2,3), app_gaps={}, gaps=((0,H,"collector_gap"),))
    router = HistoryRouter(QueryService(db), SimpleNamespace(state=None), AppRegistry())
    try:
        router._plan(R2)
        assert router._gap_ranges == []
        process = router._combine_coverage([coverage()])
        assert process.gap_intervals == 0
        assert process.complete
    finally:
        router.close()
