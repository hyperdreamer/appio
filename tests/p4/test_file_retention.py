"""Focused persistence acceptance tests for per-app file-hour quota units."""
import pytest

from appio.contracts import (
    FILE_HOUR_NS, FileCoverageInput, FileObservation, HookHits, UnknownOwnerInput,
)
from appio.durable import Receipt
from appio.file_store import FileHourStore
from appio.files import HourlyAccumulator
from appio.migrations import open_database
from appio.quota import QuotaPruner

APP = "unit:app-demo.service"
R = Receipt("gui", 1, 0)
ZERO = FileCoverageInput(0, 0, 0, 0, 0, 0, 0, 0, 0, HookHits(0, 0, 0, 0, 0, 0), 0, 0, True)
DAY = 86_400_000_000_000


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path / "appio.sqlite")
    connection.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) VALUES('boot-A',1,0,1000.0,'gui')")
    connection.execute("INSERT INTO clock_map VALUES('boot-A',0,1000000000000)")
    connection.execute("INSERT INTO app VALUES(1,?, 'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    connection.commit()
    yield connection
    connection.close()


def _observation(inode, hour):
    return FileObservation(
        APP, "1111-aaaa", "259:0", inode, 7, f"file-{inode}", "resolved", False,
        1, 0, inode, 0, hour + 1,
    )


def _finalize_file_hour(connection, hour, inode):
    acc = HourlyAccumulator("boot-A", hour)
    acc.add_observation(R, APP, _observation(inode, hour), ZERO)
    acc.add_unknown_owner(R, UnknownOwnerInput(inode, inode + 1, inode + 2, 0, 0, True))
    assert FileHourStore(connection).finalize_hour(acc.finalize())


def test_file_quota_units_expire_one_app_hour_without_cross_hour_deletion(db):
    # Two hours of one app have independent file and unknown-owner A13 units.
    _finalize_file_hour(db, 0, 5)
    _finalize_file_hour(db, FILE_HOUR_NS, 7)
    db.commit()

    # Hour zero is older than 14 days; the next hour is exactly at the strict
    # retention boundary and must remain. Totals/path identities survive both.
    now = 14 * DAY + FILE_HOUR_NS
    result = QuotaPruner(db, []).expire("boot-A", now)

    assert result.complete
    assert {(unit.kind, unit.primary_key) for unit in result.deleted} == {
        ("file", ("boot-A", 1, 0)),
        ("file_unknown", ("boot-A", 0)),
    }
    assert db.execute("SELECT ts_mono_ns FROM file_sample").fetchall() == [(FILE_HOUR_NS,)]
    assert db.execute("SELECT ts_mono_ns FROM file_coverage").fetchall() == [(FILE_HOUR_NS,)]
    assert db.execute("SELECT ts_mono_ns FROM file_overflow_unknown").fetchall() == [(FILE_HOUR_NS,)]
    assert db.execute("SELECT bytes_r FROM app_boot_file_total ORDER BY bytes_r").fetchall() == [(5,), (7,)]
    assert db.execute("SELECT count(*) FROM path").fetchone() == (2,)
    assert db.execute("SELECT overflow_files FROM boot_file_overflow_unknown").fetchone() == (12,)


def test_file_quota_wall_collision_uses_full_file_hour_key(db, tmp_path):
    # Falsifier: tied mapped walls select whichever hour the unordered scan returns first.
    from appio.quota import physical_store_bytes

    _finalize_file_hour(db, 0, 5)
    _finalize_file_hour(db, FILE_HOUR_NS, 7)
    db.execute("DELETE FROM file_overflow_unknown")
    db.execute("INSERT INTO clock_map VALUES('boot-A',?,?)", (FILE_HOUR_NS, 1_000_000_000_000 - FILE_HOUR_NS))
    db.commit()

    database = tmp_path / "appio.sqlite"
    marker = tmp_path / "quota-marker"
    marker.write_bytes(b"x" * 500_000)
    db_size = physical_store_bytes(database)
    db.execute("PRAGMA reverse_unordered_selects=ON")
    pruner = QuotaPruner(db, [database, marker], quota_bytes=db_size + 200_000)
    db.set_trace_callback(lambda sql: marker.unlink(missing_ok=True)
                           if sql.upper().startswith("DELETE FROM FILE_SAMPLE") else None)

    result = pruner.shrink_if_needed()
    db.set_trace_callback(None)

    assert [unit.primary_key for unit in result.deleted] == [("boot-A", 1, 0)]
    assert db.execute("SELECT ts_mono_ns FROM file_sample").fetchall() == [(FILE_HOUR_NS,)]


def test_missing_file_hour_total_keeps_that_unit_and_allows_ready_unknown_owner(db):
    _finalize_file_hour(db, 0, 5)
    db.execute("DELETE FROM app_boot_file_total")
    db.commit()

    result = QuotaPruner(db, []).expire("boot-A", 15 * DAY)

    assert not result.complete
    assert result.reason == "missing_materialization"
    assert [u.kind for u in result.deleted] == ["file_unknown"]
    assert [(u.kind, u.primary_key) for u in result.skipped] == [("file", ("boot-A", 1, 0))]
    assert db.execute("SELECT count(*) FROM file_sample").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM file_coverage").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM file_overflow_unknown").fetchone() == (0,)
