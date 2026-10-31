from dataclasses import FrozenInstanceError, fields

import pytest

import appio.query as query_module
from appio.contracts import Window
from appio.query import QueryService
from tests.p4.files_tab_support import A, B, BOOT, H, R, S, tab_store


def _persisted_gap_count(db, window):
    return db.execute(
        "SELECT count(*) FROM gap WHERE boot_id=? AND from_mono_ns<? AND to_mono_ns>?",
        (window.boot_id, window.end_mono_ns, window.start_mono_ns),
    ).fetchone()[0]


def test_facts_distinguish_app_hour_reducers_from_persisted_rows(tab_store):
    db = tab_store()
    service = QueryService(db)
    with service._snapshot():
        assert _persisted_gap_count(db, S) == 1
        assert _persisted_gap_count(db, R) == 3

        applications = service.file_applications(R)
        unknown_owner = service.unknown_owner_file_coverage(R)
        assert unknown_owner.window == S
        facts_a = service.file_serve_facts(1, R)
        facts_b = service.file_serve_facts(2, R)
        fact_type = query_module.FileServeFacts
        assert isinstance(facts_a, fact_type)
        assert isinstance(facts_b, fact_type)
        assert facts_a == fact_type(S, 1, 4)
        assert facts_b == fact_type(S, 2, 5)
        assert facts_b.gaps_in_served_interval != _persisted_gap_count(db, S)
        assert {key: total for key, _, total in applications} == {A: 70, B: 140}

        for app_id, facts, requested_gap_count, expected_served_gap_count in (
            (1, facts_a, 4, 1),
            (2, facts_b, 5, 2),
        ):
            table = service.file_table(app_id, R, "bytes_total", "desc", None)
            coverage = service.file_coverage(app_id, R)
            assert table.app_id == coverage.app_id == app_id
            assert table.window == coverage.window == R
            assert (
                table.unknown_owner_overflow.window
                == coverage.served_interval
                == facts.served_interval
                == S
            )
            assert (
                coverage.coverage.gap_intervals
                == facts.gaps_in_requested_window
                == requested_gap_count
            )
            assert facts.gaps_in_served_interval == expected_served_gap_count
            assert type(facts.gaps_in_served_interval) is int
            assert type(facts.gaps_in_requested_window) is int
        assert db.in_transaction

    assert [
        field.name for field in fields(query_module.FileServeFacts)
    ] == ["served_interval", "gaps_in_served_interval", "gaps_in_requested_window"]
    with pytest.raises(FrozenInstanceError):
        facts_a.gaps_in_served_interval = 99


@pytest.mark.parametrize(
    ("app_id", "served_gap_count", "requested_gap_count"),
    [(1, 1, 4), (2, 2, 5)],
    ids=["app-a", "app-b"],
)
def test_requested_and_served_facts_are_pinned_per_application(
    tab_store, app_id, served_gap_count, requested_gap_count
):
    facts = QueryService(tab_store()).file_serve_facts(app_id, R)
    assert facts.served_interval == S
    assert facts.gaps_in_served_interval == served_gap_count
    assert facts.gaps_in_requested_window == requested_gap_count


def test_no_servable_interval_facts_are_unknown_not_zero(tab_store):
    db = tab_store(hours=(), app_gaps={}, gaps=())
    facts = QueryService(db).file_serve_facts(1, R)
    assert facts.served_interval is None
    assert facts.gaps_in_served_interval is None
    assert type(facts.gaps_in_requested_window) is int


def test_malformed_served_hour_coverage_is_not_measured_as_zero(tab_store):
    db = tab_store()
    db.execute(
        "UPDATE file_coverage SET coverage_json='{}' WHERE app_id=2 AND ts_mono_ns=?",
        (2 * H,),
    )
    db.commit()

    service = QueryService(db)
    with service._snapshot():
        coverage = service.file_coverage(2, R)
        facts = service.file_serve_facts(2, R)
        assert coverage.served_interval == S
        assert not coverage.coverage.complete
        assert facts.served_interval == S
        assert facts.gaps_in_served_interval is None


@pytest.mark.parametrize("app_id", [True, 0, -1], ids=["bool", "zero", "negative"])
def test_facts_validate_positive_local_app_id(tab_store, app_id):
    service = QueryService(tab_store())
    with pytest.raises(ValueError):
        service.file_serve_facts(app_id, R)


def test_facts_use_existing_missing_app_behavior(tab_store):
    service = QueryService(tab_store())
    with pytest.raises(KeyError, match="999"):
        service.file_serve_facts(999, R)


@pytest.mark.parametrize(
    "window",
    [
        Window("since_boot", BOOT, end_mono_ns=2 * H),
        Window("lifetime"),
    ],
    ids=["pruned-since-boot", "pruned-lifetime"],
)
def test_pruned_totals_only_facts_keep_scope_but_not_hourly_zero(tab_store, window):
    db = tab_store(hours=(0, 1), app_gaps={}, gaps=(), end=2 * H)
    for table in ("file_sample", "file_coverage", "file_overflow_unknown"):
        db.execute(f"DELETE FROM {table}")
    db.commit()

    service = QueryService(db)
    with service._snapshot():
        coverage = service.file_coverage(1, window)
        facts = service.file_serve_facts(1, window)
        assert coverage.served_interval == window
        assert facts.served_interval == window
        assert facts.gaps_in_served_interval is None
        assert facts.gaps_in_requested_window == coverage.coverage.gap_intervals
        assert type(facts.gaps_in_requested_window) is int
        assert db.in_transaction
