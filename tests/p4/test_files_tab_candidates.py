import pytest

from appio.contracts import Window, WindowUnavailableError
from appio.history_router import HistoryRouter
from appio.query import QueryService
from tests.p4.files_tab_support import tab_store, read_files, BOOT, H, R, S


@pytest.mark.parametrize("reverse", [False, True], ids=["A-then-B", "B-then-A"])
def test_two_valid_stores_with_different_served_intervals_are_ambiguous(
        tab_store, monkeypatch, reverse):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), start=2*H, end=4*H)
    b = tab_store("b", hours=(4,5), app_gaps={}, gaps=(), owner="iod", start=4*H)
    watermark_a = a.execute(
        "SELECT file_finalized_through_mono_ns FROM boot WHERE boot_id=?", (BOOT,)
    ).fetchone()[0]
    watermark_b = b.execute(
        "SELECT file_finalized_through_mono_ns FROM boot WHERE boot_id=?", (BOOT,)
    ).fetchone()[0]
    assert (watermark_a, watermark_b) == (4*H, 6*H)

    service_a, service_b = QueryService(a), QueryService(b)
    table_a = service_a.file_table(1, R, "bytes_total", "desc", None)
    table_b = service_b.file_table(1, R, "bytes_total", "desc", None)
    coverage_a, coverage_b = service_a.file_coverage(1, R), service_b.file_coverage(1, R)
    served_b = Window("range", BOOT, 4*H, 6*H)
    assert table_a.window == table_b.window == R
    assert table_a.coverage.unavailable_reason is None
    assert table_b.coverage.unavailable_reason is None
    assert coverage_a.window == coverage_b.window == R
    assert coverage_a.served_interval == S
    assert coverage_b.served_interval == served_b
    assert coverage_a.served_interval.end_mono_ns == watermark_a
    assert coverage_b.served_interval.end_mono_ns == watermark_b

    observed = []
    check = HistoryRouter._check_file_provenance

    def record_candidate(worker, served, supplying_name):
        observed.append((supplying_name, served))
        return check(worker, served, supplying_name)

    monkeypatch.setattr(
        HistoryRouter, "_check_file_provenance", staticmethod(record_candidate)
    )
    stores = (("A", a, "gui"), ("B", b, "iod"))
    ordered = tuple(reversed(stores)) if reverse else stores
    with pytest.raises(WindowUnavailableError) as caught:
        read_files(ordered)
    assert (caught.value.reason, str(caught.value)) == (
        "file_source_unavailable", "File ownership cannot be represented by one store.")
    assert observed == [(name, S if name == "A" else served_b)
                        for name, _, _ in ordered]


def test_outside_candidate_owner_and_gap_do_not_fail_the_only_survivor(tab_store):
    a = tab_store("a", hours=(2,3), app_gaps={}, gaps=(), start=2*H, end=4*H)
    b = tab_store("b", hours=(), app_gaps={}, gaps=((5*H,6*H,"collector_gap"),),
                  owner="iod", start=5*H)

    result = read_files((("A",a,"gui"),("B",b,"iod")))

    assert result.coverage.served_interval == S
    assert result.coverage.observed_bytes_r == 70


@pytest.mark.parametrize("hours,owner_end", [
    ((0,1,3,4), 2*H),
    ((0,3,4), H),
], ids=["equal-runs-latest-end", "shorter-earlier-run"])
def test_largest_calendar_run_and_latest_end_tie_break_are_not_provenance_choices(
        tab_store, hours, owner_end):
    db = tab_store(hours=hours, app_gaps={}, gaps=())
    service = QueryService(db)
    watermark = db.execute(
        "SELECT file_finalized_through_mono_ns FROM boot WHERE boot_id=?", (BOOT,)
    ).fetchone()[0]
    selected = Window("range", BOOT, 3*H, 5*H)
    table = service.file_table(1, R, "bytes_total", "desc", None)
    coverage = service.file_coverage(1, R)
    assert watermark == 5*H
    assert table.window == R
    assert table.coverage.unavailable_reason is None
    assert coverage.window == R
    served_before_provenance = coverage.served_interval
    assert served_before_provenance == selected
    assert served_before_provenance.end_mono_ns == watermark

    db.execute("UPDATE collector_interval SET to_mono_ns=? WHERE source='gui' AND boot_id=?",
               (owner_end, BOOT))
    db.commit()
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("local",db,"gui"),))
    assert (caught.value.reason, str(caught.value)) == (
        "file_source_unavailable", "File ownership cannot be represented by one store.")
