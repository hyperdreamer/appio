"""Real-store regression pins for Files provenance over its served interval.

All receipts and hourly observations are synthetic; no collector, BPF, /proc,
network, or privileged fixture is used.
"""

import pytest

from appio.contracts import FILE_HOUR_NS as H, Window, WindowUnavailableError
from appio.durable import Receipt
from appio.file_store import FileHourStore
from appio.files import HourlyAccumulator
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry
from appio.migrations import open_database
from appio.query import QueryService
from appio.ui.files_view import FilesView
from test_files import APP, ZERO, observation
from tests.p4.test_file_history_router import Client


BOOT = "boot-A"
SERVED = Window("range", BOOT, 2 * H, 4 * H)
REQUESTED = Window("since_boot", BOOT, end_mono_ns=6 * H)


@pytest.fixture
def histories(tmp_path):
    connections, routers = [], []

    def make(name="local", *, owner="gui", hours=(0, 2, 3, 5), gaps=True,
             start=0, end=6 * H):
        db = open_database(tmp_path / f"{name}.sqlite")
        connections.append(db)
        db.execute(
            "INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) "
            "VALUES(?,1,0,1000.0,?)", (BOOT, owner),
        )
        db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
        db.execute("INSERT INTO collector_interval VALUES(?,?,1,?,?)", (owner, BOOT, start, end))
        db.commit()
        for hour in hours:
            accumulator = HourlyAccumulator(BOOT, hour * H)
            accumulator.add_observation(
                Receipt(owner, 1, hour), APP,
                observation(hour + 1, basename=f"hour-{hour}.txt", bytes_r=(hour + 1) * 10,
                            last_seen_mono_ns=hour * H + 100_000_000_000),
                ZERO,
            )
            FileHourStore(db).finalize_hour(accumulator.finalize())
        if gaps:
            for index, hour in enumerate((1, 4)):
                db.execute("INSERT INTO gap VALUES(?,?,?,?,?,?,?,'collector_gap')",
                           (index + 1, BOOT, owner, 1, index, hour * H, (hour + 1) * H))
        db.commit()
        return db

    def route(db, window=REQUESTED, other=None):
        router = HistoryRouter(QueryService(db), Client(), AppRegistry())
        routers.append(router)
        if other is not None:
            router.add_source(QueryService(other), collector_id="other", owner="iod")
        router.prepare(window)
        router._local_future.result(timeout=10)
        assert router.ready(window)
        return router

    yield make, route
    for router in routers:
        router.close()
    for db in connections:
        db.close()


def rendered(qtbot, router, window):
    view = FilesView(router)
    qtbot.addWidget(view)
    view.set_window(window)
    qtbot.waitUntil(lambda: view.state != "loading", timeout=10_000)
    return view


def refused(router, window):
    with pytest.raises(WindowUnavailableError) as caught:
        router.request_file_view(router.registry.id_for(APP), window,
                                 "bytes_total", "desc", None).result(timeout=10)
    assert caught.value.reason in {"file_source_unavailable", "not_retained"}
    assert not router.file_source_status().available


@pytest.mark.parametrize("window", [REQUESTED, Window("range", BOOT, 0, 6 * H)],
                         ids=["since-boot", "six-hour-range"])
def test_gapped_window_renders_proven_served_interval_rows(histories, qtbot, window):
    make, route = histories
    db = make()
    direct = QueryService(db).file_table(1, window, "bytes_total", "desc", None)
    assert direct.unknown_owner_overflow.window == SERVED
    assert direct.coverage.unavailable_reason is None
    assert not direct.coverage.complete
    assert {row.basename for row in direct.rows} == {"hour-2.txt", "hour-3.txt"}
    router = route(db, window)

    view = rendered(qtbot, router, window)

    assert view.state == "incomplete", view.state_label.text()
    assert view.model.rowCount() == 2
    assert view.last_result.table.rows == direct.rows
    assert view.last_result.coverage.observed_bytes_r == 70
    assert view.last_result.coverage.unknown_owner_overflow.window == SERVED
    assert router.file_source_status().available


@pytest.mark.parametrize("unproved", ["gap", "missing-owner"])
def test_unproven_served_interval_still_refuses(histories, qtbot, unproved):
    make, route = histories
    db = make()
    if unproved == "gap":
        db.execute("INSERT INTO gap VALUES(3,?,'gui',1,3,?,?,'collector_gap')",
                   (BOOT, 2 * H + H // 2, 2 * H + H // 2 + 10))
    else:
        db.execute("UPDATE collector_interval SET to_mono_ns=?", (3 * H,))
    db.commit()
    direct = QueryService(db).file_table(1, REQUESTED, "bytes_total", "desc", None)
    assert direct.unknown_owner_overflow.window == SERVED
    assert len(direct.rows) == 2
    assert direct.coverage.unavailable_reason is None
    router = route(db)

    refused(router, REQUESTED)
    view = rendered(qtbot, router, REQUESTED)
    assert view.state == "unavailable"
    assert view.model.rowCount() == 0


@pytest.mark.parametrize("ownership", ["overlap", "split", "disjoint"])
def test_ambiguous_served_interval_ownership_still_refuses(histories, qtbot, ownership):
    make, route = histories
    db = make()
    if ownership in {"split", "disjoint"}:
        db.execute("UPDATE collector_interval SET to_mono_ns=?",
                   ((3 if ownership == "split" else 4) * H,))
        db.commit()
    other = make("other", owner="iod", hours=((5,) if ownership == "disjoint" else ()),
                 gaps=False, start={"overlap": 2, "split": 3, "disjoint": 5}[ownership] * H,
                 end=(6 if ownership == "disjoint" else 4) * H)
    assert QueryService(db).file_table(1, REQUESTED, "bytes_total", "desc", None).rows
    router = route(db, other=other)

    refused(router, REQUESTED)
    view = rendered(qtbot, router, REQUESTED)
    assert view.state == "unavailable"
    assert view.model.rowCount() == 0


def test_other_owner_outside_served_interval_does_not_hide_rows(histories, qtbot):
    make, route = histories
    db = make()
    other = make("other", owner="iod", hours=(), gaps=False, start=5 * H)
    router = route(db, other=other)

    view = rendered(qtbot, router, REQUESTED)

    assert view.state == "incomplete"
    assert sum(row.bytes_r for row in view.last_result.table.rows) == 70
    assert view.last_result.coverage.unknown_owner_overflow.window == SERVED


def test_mirror_cannot_replace_the_authoritative_served_interval(histories, qtbot):
    make, route = histories
    db = make()
    mirror = make("mirror", hours=(2, 3), gaps=False)
    mirror.execute("UPDATE file_sample SET bytes_r=999")
    mirror.commit()
    router = route(db, other=mirror)

    view = rendered(qtbot, router, REQUESTED)

    assert view.state == "incomplete"
    assert view.last_result.coverage.observed_bytes_r == 70
    assert sum(row.bytes_r for row in view.last_result.table.rows) == 70


def test_unproved_served_interval_chronology_still_refuses(histories):
    make, route = histories
    db = make()
    # Both snapshots initially agree on boot order so normal process history
    # prepares. The next file request must inspect its own fresh snapshots.
    other = make("other", owner="iod", hours=(), gaps=False, start=5 * H)
    for connection in (db, other):
        connection.execute(
            "INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) "
            "VALUES('boot-B',2,0,2000.0,'gui')"
        )
        connection.commit()
    router = route(db, other=other)
    other.execute("UPDATE boot SET boot_order=3 WHERE boot_id=?", (BOOT,))
    other.execute("UPDATE boot SET boot_order=1 WHERE boot_id='boot-B'")
    other.execute("UPDATE boot SET boot_order=2 WHERE boot_id=?", (BOOT,))
    other.commit()

    refused(router, REQUESTED)


@pytest.mark.parametrize("missing", ["ownership", "file-calendar"])
def test_no_proven_file_interval_renders_unavailable(histories, qtbot, missing):
    make, route = histories
    db = make(gaps=False)
    window = Window("range", BOOT, 0, 6 * H)
    if missing == "ownership":
        db.execute("DELETE FROM collector_interval")
    else:
        for table in ("file_sample", "file_coverage", "file_overflow_unknown"):
            db.execute(f"DELETE FROM {table}")
    db.commit()
    router = route(db, window)

    refused(router, window)
    view = rendered(qtbot, router, window)
    assert view.state == "unavailable"
    assert view.model.rowCount() == 0
    assert view.last_result is None


def test_real_router_disclosure_names_the_served_interval(histories, qtbot):
    make, route = histories
    router = route(make())

    view = rendered(qtbot, router, REQUESTED)

    labels = (view.footer_state, view.footer_observed, view.footer_counted,
              view.footer_uncounted, view.footer_unknown_owner, view.footer_skipped,
              view.footer_weak, view.footer_hooks)
    for label in labels:
        assert "served interval only" in label.text()
        assert "T0=2h 0m 0s through T1=4h 0m 0s after boot" in label.text()
    assert "collection has gaps in the requested window" in view.footer_state.text()
    assert view.state == "incomplete"
    assert view.last_result.table.window == REQUESTED
    assert view.last_result.coverage.window == REQUESTED
