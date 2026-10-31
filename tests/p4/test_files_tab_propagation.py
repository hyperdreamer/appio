from dataclasses import replace

import pytest

from appio.query import QueryService
from tests.p4.files_tab_support import A, B, R, S, tab_store


def _assert_served_app(service, aid, expected):
    table = service.file_table(aid, R, "bytes_total", "desc", None)
    cov = service.file_coverage(aid, R)
    assert table.app_id == cov.app_id == aid
    assert table.window == cov.window == R
    assert cov.served_interval == S
    assert table.unknown_owner_overflow.window == S
    assert {row.basename for row in table.rows} == {f"app-{aid}-hour-2.txt", f"app-{aid}-hour-3.txt"}
    assert sum(row.bytes_r for row in table.rows) == cov.observed_bytes_r == expected
    assert cov.observed_bytes_w == 0


def test_served_propagation_excludes_outside_rows_for_both_apps(tab_store):
    db = tab_store()
    service = QueryService(db)
    with service._snapshot():
        assert dict((key, total) for key, name, total in service.file_applications(R)) == {A: 70, B: 140}
        for aid, expected in ((1, 70), (2, 140)):
            _assert_served_app(service, aid, expected)


@pytest.mark.parametrize(("aid", "expected"), [(1, 70), (2, 140)], ids=["app-a", "app-b"])
def test_each_app_reads_only_the_served_interval(tab_store, aid, expected):
    service = QueryService(tab_store())
    with service._snapshot():
        _assert_served_app(service, aid, expected)


@pytest.mark.parametrize("unavailable_component", ["app", "unknown_owner"])
@pytest.mark.parametrize("aid", [1, 2], ids=["app-a", "app-b"])
def test_unavailable_coverage_has_no_served_interval(tab_store, unavailable_component, aid):
    cov = QueryService(tab_store()).file_coverage(aid, R)
    placeholder = replace(cov.unknown_owner_overflow, window=R)
    if unavailable_component == "app":
        result = replace(
            cov,
            coverage=replace(cov.coverage, unavailable_reason="not_retained"),
            unknown_owner_overflow=placeholder,
        )
    else:
        result = replace(
            cov,
            unknown_owner_overflow=replace(
                placeholder,
                coverage=replace(placeholder.coverage, unavailable_reason="not_retained"),
            ),
        )
    assert result.served_interval is None


@pytest.mark.parametrize("aid", [1, 2], ids=["app-a", "app-b"])
def test_incomplete_full_scope_still_has_a_served_interval(tab_store, aid):
    db = tab_store(
        hours=tuple(range(6)), app_gaps={(1, 0): 1, (2, 0): 1}, gaps=(),
    )
    cov = QueryService(db).file_coverage(aid, R)

    assert cov.coverage.complete is False
    assert cov.served_interval == R
