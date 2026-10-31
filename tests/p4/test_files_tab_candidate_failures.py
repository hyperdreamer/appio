import sqlite3

import pytest

from appio.contracts import Window, WindowUnavailableError
from appio.history_router import HistoryRouter
from appio.query import QueryService
from tests.p4.files_tab_support import BOOT, H, R, S, read_files, tab_store


def _inject_first_result_error(monkeypatch, reason, message):
    original = HistoryRouter._check_file_result
    calls = []

    def check(table, coverage, served_gap_count=None):
        calls.append((table.app_id, coverage.app_id))
        if len(calls) == 1:
            raise WindowUnavailableError(reason, message)
        return original(table, coverage, served_gap_count)

    monkeypatch.setattr(HistoryRouter, "_check_file_result", staticmethod(check))
    return calls


@pytest.mark.parametrize("reverse", [False, True], ids=["A-then-B", "B-then-A"])
def test_provenance_failure_is_fatal_even_when_another_candidate_is_valid(
        tab_store, reverse):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), start=3*H, end=4*H)
    b = tab_store("b", hours=(5,), app_gaps={}, gaps=(), owner="iod", start=5*H, end=6*H)

    for db, served in ((a, S), (b, Window("range", BOOT, 5*H, 6*H))):
        service = QueryService(db)
        table = service.file_table(1, R, "bytes_total", "desc", None)
        coverage = service.file_coverage(1, R)
        assert coverage.served_interval == served
        assert table.coverage.unavailable_reason is None
        assert coverage.coverage.unavailable_reason is None

    assert read_files((("B", b, "iod"),)).coverage.observed_bytes_r == 60
    stores = (("A", a, "gui"), ("B", b, "iod"))
    with pytest.raises(WindowUnavailableError) as caught:
        read_files(tuple(reversed(stores)) if reverse else stores)
    assert (caught.value.reason, str(caught.value)) == (
        "file_source_unavailable", "File ownership cannot be represented by one store.")


@pytest.mark.parametrize("reason", ["not_retained", "file_source_unavailable"])
def test_typed_preprovenance_query_unavailability_skips_to_valid_B(
        tab_store, monkeypatch, reason):
    a = tab_store("a", hours=(), app_gaps={}, gaps=(), start=0, end=H)
    b = tab_store("b", hours=(5,), app_gaps={}, gaps=(), owner="iod", start=5*H, end=6*H)
    calls = _inject_first_result_error(monkeypatch, reason, "synthetic query unavailability")

    result = read_files((("A", a, "gui"), ("B", b, "iod")))

    assert result.coverage.served_interval == Window("range", BOOT, 5*H, 6*H)
    assert result.coverage.observed_bytes_r == 60
    assert len(calls) == 2


def test_missing_app_receipt_skips_without_replacing_last_query_error(
        tab_store, monkeypatch):
    missing = tab_store("missing", apps=(), hours=(), app_gaps={}, gaps=(), start=0, end=H)
    b = tab_store("b", hours=(5,), app_gaps={}, gaps=(), owner="iod", start=5*H, end=6*H)
    result = read_files((("missing", missing, "gui"), ("B", b, "iod")))
    assert result.coverage.observed_bytes_r == 60

    failed = tab_store("failed", hours=(5,), app_gaps={}, gaps=(), owner="gui", start=5*H, end=6*H)
    no_receipt = tab_store("no-receipt", apps=(), hours=(), app_gaps={}, gaps=(), start=0, end=H)
    calls = _inject_first_result_error(monkeypatch, "not_retained", "remember this query error")
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("failed", failed, "gui"), ("no-receipt", no_receipt, "iod")))
    assert (caught.value.reason, str(caught.value)) == (
        "not_retained", "remember this query error")
    assert len(calls) == 1


def test_valid_mirror_is_skipped_only_after_provenance(tab_store, monkeypatch):
    local = tab_store("local", hours=(2,3), app_gaps={}, gaps=(),
                      owner="gui", start=2*H, end=4*H)
    mirror = tab_store("mirror", hours=(2,3), app_gaps={}, gaps=(),
                       owner="gui", start=2*H, end=4*H)
    mirror.execute("UPDATE file_sample SET bytes_r=999, bytes_w=0")
    mirror.commit()
    observed = []
    original = HistoryRouter._check_file_provenance

    def record_provenance(worker, served, supplying_name):
        accepted = original(worker, served, supplying_name)
        observed.append((supplying_name, served, accepted))
        return accepted

    monkeypatch.setattr(HistoryRouter, "_check_file_provenance", staticmethod(record_provenance))
    result = read_files((("local", local, "gui"), ("mirror", mirror, "gui")))

    assert observed == [("local", S, True), ("mirror", S, False)]
    assert result.coverage.served_interval == S
    assert result.coverage.observed_bytes_r == 70


@pytest.mark.parametrize("reverse", [False, True], ids=["A-then-B", "B-then-A"])
def test_mirror_mismatch_cannot_hide_earlier_provenance_failure(tab_store, reverse):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), owner="gui", start=0, end=H)
    b = tab_store("b", hours=(5,), app_gaps={}, gaps=(), owner="iod", start=5*H, end=6*H)
    b.execute("INSERT INTO collector_interval VALUES('iod',?,2,?,?)", (BOOT, 3*H, 4*H))
    b.commit()
    assert QueryService(a).file_coverage(1, R).served_interval == S
    assert QueryService(b).file_coverage(1, R).served_interval == Window("range", BOOT, 5*H, 6*H)

    stores = (("A", a, "gui"), ("B", b, "iod"))
    with pytest.raises(WindowUnavailableError) as caught:
        read_files(tuple(reversed(stores)) if reverse else stores)
    assert (caught.value.reason, str(caught.value)) == (
        "file_source_unavailable", "File ownership cannot be represented by one store.")


@pytest.mark.parametrize("stage", ["read", "planning"], ids=["unexpected-read", "unexpected-planning"])
def test_unexpected_read_or_planning_error_is_fatal_not_a_candidate_skip(
        tab_store, monkeypatch, stage):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), owner="gui", start=2*H, end=4*H)
    b = tab_store("b", hours=(5,), app_gaps={}, gaps=(), owner="iod", start=5*H, end=6*H)
    injected = []

    if stage == "read":
        original = QueryService.file_table

        def fail_first_read(service, *args, **kwargs):
            if not injected:
                injected.append(True)
                raise sqlite3.OperationalError("synthetic SQLite read failure")
            return original(service, *args, **kwargs)

        monkeypatch.setattr(QueryService, "file_table", fail_first_read)
        error_type, message = sqlite3.OperationalError, "synthetic SQLite read failure"
    else:
        original = HistoryRouter._plan

        def fail_first_plan(worker, window):
            if not injected:
                injected.append(True)
                raise ValueError("synthetic planning failure")
            return original(worker, window)

        monkeypatch.setattr(HistoryRouter, "_plan", fail_first_plan)
        error_type, message = ValueError, "synthetic planning failure"

    with pytest.raises(error_type, match=message):
        read_files((("A", a, "gui"), ("B", b, "iod")))
    assert injected == [True]
