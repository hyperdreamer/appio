from dataclasses import MISSING, fields, replace
import json

import pytest

from appio.contracts import Window, WindowUnavailableError
from appio.durable import canonical_json
from appio.history_router import FileViewResult, HistoryRouter
from appio.query import QueryService
from tests.p4.files_tab_support import A, B, BOOT, H, R, S, read_files, tab_store
from tests.p4.test_file_history_router import Remote, id_for, retained, WINDOW as REMOTE_WINDOW


@pytest.mark.parametrize(
    ("key", "local_id", "presentation_id", "served_count", "requested_count"),
    [(A, 1, 101, 1, 4), (B, 2, 202, 2, 5)],
    ids=["app-a", "app-b"],
)
def test_local_result_uses_explicit_store_app_and_one_snapshot(
    tab_store, monkeypatch, key, local_id, presentation_id, served_count, requested_count
):
    db = tab_store()
    calls = []
    original = QueryService.file_serve_facts

    def facts(service, app_id, window):
        assert service.connection.in_transaction
        calls.append((app_id, window, id(service.connection)))
        return original(service, app_id, window)

    monkeypatch.setattr(QueryService, "file_serve_facts", facts)
    result = read_files((("local", db, "gui"),), key=key, app_id=presentation_id)

    assert result.table.app_id == result.coverage.app_id == presentation_id
    assert result.table.window == result.coverage.window == R
    assert result.coverage.served_interval == S
    assert result.served_gap_count == served_count
    assert result.coverage.coverage.gap_intervals == requested_count
    assert len(calls) == 1
    assert calls[0][0:2] == (local_id, R)


def test_file_view_result_has_one_explicit_frozen_served_gap_field():
    result_fields = fields(FileViewResult)
    assert [field.name for field in result_fields] == ["table", "coverage", "served_gap_count"]
    assert result_fields[-1].default is MISSING
    assert FileViewResult.__dataclass_params__.frozen


def test_local_table_coverage_and_facts_share_the_pinned_snapshot(tab_store, monkeypatch):
    db = tab_store()
    calls = []
    source_changed = []
    original_table = QueryService.file_table
    original_coverage = QueryService.file_coverage
    original_facts = QueryService.file_serve_facts

    def note(name, service, app_id, window):
        calls.append((name, app_id, id(service.connection), service.connection.in_transaction))

    def file_table(service, app_id, window, sort, direction, search):
        note("table", service, app_id, window)
        value = original_table(service, app_id, window, sort, direction, search)
        if not source_changed:
            row = db.execute(
                "SELECT coverage_json FROM file_coverage WHERE boot_id=? AND app_id=1 AND ts_mono_ns=?",
                (BOOT, 2 * H),
            ).fetchone()
            changed = json.loads(row[0])
            changed["gap_intervals"] = 99
            db.execute(
                "UPDATE file_coverage SET coverage_json=? WHERE boot_id=? AND app_id=1 AND ts_mono_ns=?",
                (canonical_json(changed), BOOT, 2 * H),
            )
            db.commit()
            source_changed.append(True)
        return value

    def file_coverage(service, app_id, window):
        note("coverage", service, app_id, window)
        return original_coverage(service, app_id, window)

    def file_serve_facts(service, app_id, window):
        note("facts", service, app_id, window)
        return original_facts(service, app_id, window)

    monkeypatch.setattr(QueryService, "file_table", file_table)
    monkeypatch.setattr(QueryService, "file_coverage", file_coverage)
    monkeypatch.setattr(QueryService, "file_serve_facts", file_serve_facts)
    result = read_files((("local", db, "gui"),))

    assert [call[0] for call in calls] == ["table", "coverage", "facts", "coverage"]
    assert all(call[1] == 1 for call in calls)
    assert len({call[2] for call in calls}) == 1
    assert all(call[3] for call in calls)
    assert source_changed == [True]
    changed_source = json.loads(db.execute(
        "SELECT coverage_json FROM file_coverage WHERE boot_id=? AND app_id=1 AND ts_mono_ns=?",
        (BOOT, 2 * H),
    ).fetchone()[0])
    assert changed_source["gap_intervals"] == 99
    assert result.served_gap_count == 1
    assert result.coverage.coverage.gap_intervals == 4


@pytest.mark.parametrize(
    ("key", "store_app_id", "presentation_id"),
    [(A, 1, 101), (B, 2, 202)],
    ids=["app-a", "app-b"],
)
@pytest.mark.parametrize("mismatch", ["served-scope", "requested-count", "other-app-facts"])
def test_local_result_rejects_facts_from_a_different_scope_or_app(
    tab_store, monkeypatch, key, store_app_id, presentation_id, mismatch
):
    db = tab_store()
    original = QueryService.file_serve_facts

    def facts(service, app_id, window):
        assert app_id == store_app_id
        if mismatch == "other-app-facts":
            other_app_id = 2 if app_id == 1 else 1
            return original(service, other_app_id, window)
        value = original(service, app_id, window)
        if mismatch == "served-scope":
            return replace(value, served_interval=R)
        return replace(value, gaps_in_requested_window=value.gaps_in_requested_window + 1)

    monkeypatch.setattr(QueryService, "file_serve_facts", facts)
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("local", db, "gui"),), key=key, app_id=presentation_id)
    assert (caught.value.reason, str(caught.value)) == (
        "file_source_unavailable",
        "File coverage does not match the table snapshot.",
    )


@pytest.mark.parametrize(
    ("key", "store_app_id", "presentation_id"),
    [(A, 1, 101), (B, 2, 202)],
    ids=["app-a", "app-b"],
)
@pytest.mark.parametrize("component", ["table", "coverage"])
def test_local_result_rejects_records_bound_to_another_store_app(
    tab_store, monkeypatch, key, store_app_id, presentation_id, component
):
    db = tab_store()
    original_table = QueryService.file_table
    original_coverage = QueryService.file_coverage

    def file_table(service, app_id, window, sort, direction, search):
        assert app_id == store_app_id
        value = original_table(service, app_id, window, sort, direction, search)
        return replace(value, app_id=app_id + 100) if component == "table" else value

    def file_coverage(service, app_id, window):
        assert app_id == store_app_id
        value = original_coverage(service, app_id, window)
        return replace(value, app_id=app_id + 100) if component == "coverage" else value

    monkeypatch.setattr(QueryService, "file_table", file_table)
    monkeypatch.setattr(QueryService, "file_coverage", file_coverage)
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("local", db, "gui"),), key=key, app_id=presentation_id)
    assert (caught.value.reason, str(caught.value)) == (
        "file_source_unavailable",
        "File coverage does not match the table snapshot.",
    )


def test_remote_compatible_result_checker_defaults_served_gap_count_to_unknown(tab_store):
    db = tab_store()
    local = read_files((("local", db, "gui"),))

    checked = HistoryRouter._check_file_result(local.table, local.coverage)

    assert checked.served_gap_count is None


def test_remote_route_marks_served_gap_evidence_unavailable(retained, tmp_path):
    remote = Remote(tmp_path, retained)
    try:
        result = remote.finish(remote.router.request_file_view(
            id_for(remote.router), REMOTE_WINDOW, "bytes_total", "desc", None))
        assert result.served_gap_count is None
    finally:
        remote.close()


@pytest.mark.parametrize(
    "mismatch",
    ["table-app", "coverage-app", "table-window", "coverage-window", "tier", "coverage", "carrier"],
)
def test_result_checker_rejects_mixed_identity_window_tier_and_coverage(
    tab_store, mismatch
):
    db = tab_store()
    local = read_files((("local", db, "gui"),))
    table, coverage = local.table, local.coverage
    other_window = Window("range", BOOT, H, 5 * H)
    candidate_table, candidate_coverage = table, coverage
    if mismatch == "table-app":
        candidate_table = replace(table, app_id=999)
    elif mismatch == "coverage-app":
        candidate_coverage = replace(coverage, app_id=999)
    elif mismatch == "table-window":
        candidate_table = replace(table, window=other_window)
    elif mismatch == "coverage-window":
        candidate_coverage = replace(coverage, window=other_window)
    elif mismatch == "tier":
        tier = "totals_only" if table.coverage.tier != "totals_only" else "raw"
        candidate_table = replace(table, coverage=replace(table.coverage, tier=tier))
    elif mismatch == "coverage":
        candidate_table = replace(table, coverage=replace(table.coverage, gap_intervals=7))
    else:
        candidate_table = replace(
            table,
            unknown_owner_overflow=replace(
                table.unknown_owner_overflow,
                overflow_bytes_r=table.unknown_owner_overflow.overflow_bytes_r + 1,
            ),
        )

    with pytest.raises(WindowUnavailableError):
        HistoryRouter._check_file_result(candidate_table, candidate_coverage, 1)
