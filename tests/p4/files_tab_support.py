from dataclasses import replace
import pytest

from appio.contracts import FILE_HOUR_NS as H, Window
from appio.durable import Receipt
from appio.file_store import FileHourStore
from appio.files import HourlyAccumulator
from appio.history_router import HistoryRouter
from appio.migrations import open_database
from test_files import APP as A, ZERO, observation


BOOT = "boot-A"
B = "unit:app-b.service"
R = Window("range", BOOT, 0, 6 * H)
S = Window("range", BOOT, 2 * H, 4 * H)


@pytest.fixture
def tab_store(tmp_path):
    connections = []

    def make(name="local", *, hours=(0, 2, 3, 5), app_gaps=None,
             gaps=((H, 2*H, "collector_gap"),
                   (3*H, 4*H, "collector_gap"),
                   (4*H, 5*H, "collector_gap")),
             owner="gui", start=0, end=6*H, apps=(1, 2)):
        db = open_database(tmp_path / (name + ".sqlite"))
        connections.append(db)
        db.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) VALUES(?,1,0,1000.0,?)", (BOOT, owner))
        for aid in apps:
            key = A if aid == 1 else B
            db.execute("INSERT INTO app VALUES(?,?,?,NULL,1000.0,1000.0,'user')", (aid, key, "A" if aid == 1 else "B"))
        db.execute("INSERT INTO collector_interval VALUES(?,?,1,?,?)", (owner, BOOT, start, end))
        db.commit()
        counts = {(1, 2): 0, (1, 3): 1, (2, 2): 1, (2, 3): 1} if app_gaps is None else app_gaps
        for hour in hours:
            acc = HourlyAccumulator(BOOT, hour * H)
            for aid in apps:
                key = A if aid == 1 else B
                count = counts.get((aid, hour), 0)
                cov = replace(ZERO, gap_intervals=count, complete=(count == 0))
                acc.add_observation(Receipt(owner, 1, hour), key,
                    observation(100*aid + hour, app_key=key,
                        basename=f"app-{aid}-hour-{hour}.txt",
                        bytes_r=aid*(hour+1)*10,
                        last_seen_mono_ns=hour*H + 100_000_000_000), cov)
            FileHourStore(db).finalize_hour(acc.finalize())
        for index, (left, right, reason) in enumerate(gaps, 1):
            db.execute("INSERT INTO gap VALUES(?,?,?,?,?,?,?,?)", (index, BOOT, owner, 1, index, left, right, reason))
        db.commit()
        assert not db.in_transaction
        return db

    yield make
    for db in connections:
        db.close()


def read_files(stores, window=R, key=A, app_id=101):
    from pathlib import Path
    from appio.history_source import ManagedSourceRegistry
    from appio.history_types import ManagedSourceSpec

    registry = ManagedSourceRegistry(wake=lambda: None)
    captured = None
    try:
        for name, db, owner in stores:
            path = db.execute("PRAGMA database_list").fetchone()[2]
            mode = "?mode=rw&cache=shared" if name == "local" and owner == "gui" else "?mode=ro"
            registry.register(ManagedSourceSpec(
                name, owner, Path(path).as_uri() + mode, ())).result(timeout=10)
        captured = registry.capture().result(timeout=15)
        return HistoryRouter._read_local_files(
            captured, window, key, app_id, "bytes_total", "desc", None)
    finally:
        if captured is not None:
            registry.release_capture(captured.capture_id)
        registry.close()
