"""File materialization tests; real STRICT SQLite databases, synthetic inputs only."""
from dataclasses import replace
import json

import pytest

from appio.contracts import FILE_HOUR_NS, SQLITE_INT_MAX, CounterOverflowError, UnknownOwnerInput
from appio.durable import Receipt, decode_file_hour_state
from appio.files import HourlyAccumulator
from appio.file_store import FileHourStore
from appio.migrations import open_database
from test_files import APP, R, ZERO, observation


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path / "appio.sqlite")
    connection.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) VALUES('boot-A',1,0,1000.0,'gui')")
    connection.execute("INSERT INTO clock_map VALUES('boot-A',0,1000000000000)")
    connection.execute("INSERT INTO app VALUES(1,?, 'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    connection.commit()
    yield connection
    connection.close()


def test_pending_reopen_finalization_idempotence_and_no_reopening_after_pruning(db):
    # Falsifier: pending only retains winners, repeats additive totals, or relies on raw PKs for dedupe.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(1, bytes_r=60, bytes_w=40, reads=2), ZERO)
    acc.add_observation(R, APP, observation(2, bytes_r=150, reads=6), ZERO)
    store = FileHourStore(db)
    store.apply_pending(acc.finalize(1))
    path = db.execute("PRAGMA database_list").fetchone()[2]
    other = open_database(path)
    try:
        pending = other.execute("SELECT state_json FROM file_hour_state").fetchone()[0]
        restored = HourlyAccumulator.from_state(decode_file_hour_state(pending, "boot-A", 0))
        restored.add_observation(Receipt("iod", 1, 1), APP, observation(1, bytes_r=40, bytes_w=60, reads=3, last_seen_mono_ns=200_000_000_000), ZERO)
        snapshot = restored.finalize(1)
        assert FileHourStore(other).finalize_hour(snapshot) is True
        assert FileHourStore(other).finalize_hour(snapshot) is False
        assert other.execute("SELECT bytes_r,bytes_w,reads FROM file_sample").fetchall() == [(100, 100, 5)]
        assert other.execute("SELECT counted_omitted_files,counted_omitted_bytes_r FROM file_coverage").fetchall() == [(1, 150)]
        assert other.execute("SELECT count(*) FROM file_hour_state").fetchone()[0] == 0
        assert other.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone()[0] == FILE_HOUR_NS
        totals = other.execute("SELECT * FROM app_boot_file_total").fetchall()
        other.execute("DELETE FROM file_sample")
        other.execute("DELETE FROM file_coverage")
        other.execute("DELETE FROM file_overflow_unknown")
        other.commit()
        assert FileHourStore(other).finalize_hour(snapshot) is False
        assert FileHourStore(other).apply_pending(snapshot) is False
        assert other.execute("SELECT * FROM app_boot_file_total").fetchall() == totals
        assert other.execute("SELECT count(*) FROM file_hour_state").fetchone()[0] == 0
    finally:
        other.close()


def test_rename_paths_alias_times_and_later_last_survive_pending_reopen(db):
    # Falsifier: canonical path gets last global name time, or aliases inherit identity-wide first time.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(42, basename="b.txt", reads=1, writes=2, bytes_r=0, bytes_w=40, last_seen_mono_ns=200_000_000_000), ZERO)
    acc.add_observation(R, APP, observation(42, basename="a.txt", reads=2, bytes_r=60, last_seen_mono_ns=100_000_000_000), ZERO)
    store = FileHourStore(db)
    store.apply_pending(acc.finalize())
    pending = db.execute("SELECT state_json FROM file_hour_state").fetchone()[0]
    restored = HourlyAccumulator.from_state(decode_file_hour_state(pending, "boot-A", 0))
    store.finalize_hour(restored.finalize())
    assert db.execute("SELECT basename,first_mono_ns,last_mono_ns FROM path ORDER BY basename").fetchall() == [("a.txt", 100_000_000_000, 100_000_000_000), ("b.txt", 200_000_000_000, 200_000_000_000)]
    assert db.execute("SELECT p.basename,f.reads,f.writes,f.bytes_r,f.bytes_w,f.last_seen_mono_ns FROM file_sample f JOIN path p USING(path_id)").fetchall() == [("a.txt", 3, 2, 60, 40, 200_000_000_000)]
    assert db.execute("SELECT basename,first_boot_id,first_mono_ns,last_boot_id,last_mono_ns FROM path_alias").fetchall() == [("b.txt", "boot-A", 200_000_000_000, "boot-A", 200_000_000_000)]


def test_zero_hour_and_unknown_owner_have_independent_canonical_coverage(db):
    # Falsifier: skip a zero-F app, copy unknown bytes into app JSON, or disagree with structured columns.
    acc = HourlyAccumulator("boot-A", 0)
    cov = replace(ZERO, skipped_nonfile=3, skipped_unsupported_fs=1)
    acc.add_observation(R, APP, None, cov)
    acc.add_unknown_owner(R, UnknownOwnerInput(2, 11, 13, 0, 0, True))
    store = FileHourStore(db)
    store.finalize_hour(acc.finalize())
    assert db.execute("SELECT count(*) FROM file_sample").fetchone()[0] == 0
    coverage_json, skipped, unsupported = db.execute("SELECT coverage_json,skipped_nonfile,skipped_unsupported_fs FROM file_coverage").fetchone()
    coverage = json.loads(coverage_json)
    assert (skipped, unsupported) == (3, 1)
    assert coverage["file"]["uncounted_overflow_files"] == 0
    assert coverage["file"]["skipped_nonfile"] == 3
    assert coverage["file"]["skipped_unsupported_fs"] == 1
    assert set(coverage) == {"inaccessible_pids", "resets", "reap_corrections", "reap_unapplied", "reap_unobserved", "partial_intervals", "gap_intervals", "overlap_uncertain_intervals", "spans_suspend_intervals", "reap_incomplete", "counter_overflow", "device_state", "file"}
    assert db.execute("SELECT overflow_files,overflow_bytes_r,overflow_bytes_w FROM boot_file_overflow_unknown").fetchone() == (2, 11, 13)
    unknown = store.unknown_owner_for_hour("boot-A", 0)
    assert (unknown.overflow_files, unknown.overflow_bytes_r, unknown.overflow_bytes_w) == (2, 11, 13)
    assert store.unknown_owner_for_hour("boot-A", FILE_HOUR_NS) is None
    second = HourlyAccumulator("boot-A", FILE_HOUR_NS)
    second.add_observation(Receipt("gui", 1, 1), APP, None, ZERO)
    store.finalize_hour(second.finalize())
    assert store.unknown_owner_for_hour("boot-A", FILE_HOUR_NS) == UnknownOwnerInput(0, 0, 0, 0, 0, True)
    assert db.execute("SELECT count(*) FROM file_coverage").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM file_overflow_unknown").fetchone()[0] == 2
    assert db.execute("SELECT ts_mono_ns FROM file_overflow_unknown ORDER BY ts_mono_ns").fetchall() == [(0,), (FILE_HOUR_NS,)]
    from appio.contracts import Window
    from appio.query import QueryService
    app_boot_text, = db.execute("SELECT coverage_json FROM app_boot_file_coverage WHERE boot_id='boot-A' AND app_id=1").fetchone()
    assert json.loads(app_boot_text)["file"]["skipped_nonfile"] == 3
    empty_hour = QueryService(db).file_table(1, Window("range", "boot-A", FILE_HOUR_NS, 2 * FILE_HOUR_NS), "bytes_total", "desc", None)
    assert empty_hour.rows == ()
    assert empty_hour.coverage.complete


def test_file_coverage_columns_equal_json_and_boot_totals_keep_omissions_separate(db):
    # Falsifier: add omitted or overflow bytes to retained-observation boot totals.
    acc = HourlyAccumulator("boot-A", 0)
    cov = replace(ZERO, uncounted_overflow_files=2, uncounted_overflow_bytes_r=5)
    acc.add_observation(R, APP, observation(1, bytes_r=40), cov)
    acc.add_observation(R, APP, observation(2, bytes_r=20), cov)
    store = FileHourStore(db)
    store.finalize_hour(acc.finalize(1))
    assert db.execute("SELECT bytes_r FROM app_boot_file_total").fetchall() == [(40,)]
    row = db.execute("SELECT * FROM file_coverage")
    names = [column[0] for column in row.description]
    values = dict(zip(names, row.fetchone()))
    f = json.loads(values["coverage_json"])["file"]
    for name, value in f.items():
        if name == "hook_hits":
            for hook, hits in value.items():
                assert values["hook_" + hook] == hits
        else:
            assert values[name] == value
    boot = json.loads(db.execute("SELECT coverage_json FROM app_boot_file_coverage").fetchone()[0])
    assert (boot["file"]["counted_omitted_files"], boot["file"]["counted_omitted_bytes_r"], boot["file"]["uncounted_overflow_files"], boot["file"]["uncounted_overflow_bytes_r"]) == (1, 20, 2, 5)


def test_boot_chronology_controls_path_alias_endpoints_and_file_last(db):
    # Falsifier: MAX(mono_ns) chooses an older boot as LAST.
    db.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) VALUES('boot-B',2,0,2000.0,'gui')")
    db.execute("INSERT INTO clock_map VALUES('boot-B',0,2000000000000)")
    db.commit()
    for boot, t in (("boot-B", 10_000_000_000), ("boot-A", 900_000_000_000)):
        acc = HourlyAccumulator(boot, 0)
        acc.add_observation(R, APP, observation(42, basename="a", last_seen_mono_ns=t), ZERO)
        acc.add_observation(R, APP, observation(42, basename="b", last_seen_mono_ns=t), ZERO)
        FileHourStore(db).finalize_hour(acc.finalize())
    assert db.execute("SELECT first_boot_id,first_mono_ns,last_boot_id,last_mono_ns FROM path WHERE basename='a'").fetchone() == ("boot-A", 900_000_000_000, "boot-B", 10_000_000_000)
    assert db.execute("SELECT first_boot_id,last_boot_id FROM path_alias").fetchone() == ("boot-A", "boot-B")
    assert db.execute("SELECT boot_id,last_seen_mono_ns FROM app_boot_file_total ORDER BY boot_id").fetchall() == [("boot-A", 900_000_000_000), ("boot-B", 10_000_000_000)]


def test_finalization_overflow_rolls_back_all_rows_watermark_and_pending(db):
    # Falsifier: SQL adds into REAL, or finalization commits paths/coverage before checking totals.
    store = FileHourStore(db)
    first = HourlyAccumulator("boot-A", 0)
    first.add_observation(R, APP, observation(bytes_r=SQLITE_INT_MAX), ZERO)
    store.finalize_hour(first.finalize())
    second = HourlyAccumulator("boot-A", FILE_HOUR_NS)
    second.add_observation(Receipt("gui", 1, 1), APP, observation(bytes_r=1, last_seen_mono_ns=FILE_HOUR_NS+10), ZERO)
    store.apply_pending(second.finalize())
    before = tuple(db.execute(f"SELECT * FROM {table}").fetchall() for table in ("path", "file_sample", "file_coverage", "file_hour_state", "app_boot_file_total", "boot"))
    with pytest.raises(CounterOverflowError):
        store.finalize_hour(second.finalize())
    after = tuple(db.execute(f"SELECT * FROM {table}").fetchall() for table in ("path", "file_sample", "file_coverage", "file_hour_state", "app_boot_file_total", "boot"))
    assert before == after
    assert db.execute("SELECT code,field FROM store_diagnostic").fetchone()[0] == "counter_overflow"


def test_pending_path_id_overflow_rolls_back_and_persists_separate_diagnostic(db):
    # Falsifier: pending-path allocation bypasses the observation's overflow boundary.
    db.execute("INSERT INTO path VALUES(?,'x','259:0',20,0,'occupied','resolved',0,'boot-A',0,'boot-A',0)", (SQLITE_INT_MAX,))
    db.commit()
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(), ZERO)
    with pytest.raises(CounterOverflowError, match="path_id"):
        FileHourStore(db).apply_pending(acc.finalize())
    assert db.execute("SELECT count(*) FROM path").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM file_hour_state").fetchone()[0] == 0
    assert db.execute("SELECT code,field FROM store_diagnostic").fetchone() == ("counter_overflow", "path_id")


def test_missing_uuid_materializes_as_weak_without_losing_retained_bytes(db):
    # Falsifier: transport validation drops missing-UUID F rows on the store boundary.
    from appio.durable import FileEntry
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, FileEntry("", "259:0", 99, 0, 1, "weak", (), "unresolved", 1, 0, 8, 0, 10), ZERO)
    FileHourStore(db).finalize_hour(acc.finalize())
    assert db.execute("SELECT fs_uuid,identity_weak FROM path").fetchone() == ("", 1)
    assert db.execute("SELECT bytes_r FROM file_sample").fetchone()[0] == 8
    assert db.execute("SELECT weak_identity_rows FROM file_coverage").fetchone()[0] == 1


def test_b5_public_missing_uuid_survives_pending_round_trip_and_finalization(db):
    # Falsifier: a private FileEntry workaround hides public transport rejection.
    row = observation(99, fs_uuid="", identity_weak=True, bytes_r=8, last_seen_mono_ns=10)
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, row, ZERO)
    store = FileHourStore(db)
    store.apply_pending(acc.finalize())
    text = db.execute("SELECT state_json FROM file_hour_state").fetchone()[0]
    restored = HourlyAccumulator.from_state(decode_file_hour_state(text, "boot-A", 0))
    result, = restored.finalize().apps[0][1]
    assert (result.fs_uuid, result.identity_weak, result.bytes_r) == ("", True, 8)
    assert store.finalize_hour(restored.finalize()) is True
    assert store.finalize_hour(restored.finalize()) is False
    assert db.execute("SELECT fs_uuid,identity_weak FROM path").fetchone() == ("", 1)
    assert db.execute("SELECT bytes_r FROM file_sample").fetchone()[0] == 8
    assert db.execute("SELECT weak_identity_rows FROM file_coverage").fetchone()[0] == 1
    assert db.execute("SELECT bytes_r FROM app_boot_file_total").fetchone()[0] == 8


def test_plain_snapshot_overflow_keeps_counter_error_and_persists_diagnostic(db):
    # Falsifier: diagnostic handling masks CounterOverflowError for public transport snapshots.
    from appio.contracts import HourlyFileSnapshot
    store = FileHourStore(db)
    first = HourlyAccumulator("boot-A", 0)
    first.add_observation(R, APP, observation(bytes_r=SQLITE_INT_MAX), ZERO)
    store.finalize_hour(first.finalize())
    second = HourlyAccumulator("boot-A", FILE_HOUR_NS)
    row = observation(bytes_r=1, last_seen_mono_ns=FILE_HOUR_NS+10)
    second.add_observation(Receipt("gui", 1, 1), APP, row, ZERO)
    store.apply_pending(second.finalize())
    plain = HourlyFileSnapshot("boot-A", FILE_HOUR_NS, ((APP, (row,), ZERO),), second.finalize().unknown_owner)
    with pytest.raises(CounterOverflowError):
        store.finalize_hour(plain)
    assert db.execute("SELECT source,seq,code FROM store_diagnostic").fetchone() == ("gui", 1, "counter_overflow")
    assert db.execute("SELECT count(*) FROM file_hour_state").fetchone()[0] == 1


def test_noncanonical_pending_json_is_rejected_before_mutation(db):
    # Falsifier: trust SQLite json_valid or overwrite corrupt state with new valid data.
    db.execute("INSERT INTO file_hour_state VALUES('boot-A',0,'{}')")
    db.commit()
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(), ZERO)
    with pytest.raises(ValueError):
        FileHourStore(db).apply_pending(acc.finalize())
    assert db.execute("SELECT count(*) FROM path").fetchone()[0] == 0
    assert db.execute("SELECT state_json FROM file_hour_state").fetchone()[0] == "{}"


def test_pending_cumulative_state_cannot_regress_or_lose_a_candidate(db):
    # Falsifier: an old cumulative snapshot overwrites a later receipt and silently loses bytes.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(bytes_r=10), ZERO)
    old = acc.finalize()
    store = FileHourStore(db)
    store.apply_pending(old)
    acc.add_observation(Receipt("gui", 1, 1), APP, observation(2, bytes_r=20), ZERO)
    newer = acc.finalize()
    store.apply_pending(newer)
    before = db.execute("SELECT state_json FROM file_hour_state").fetchone()[0]
    for operation in (store.apply_pending, store.finalize_hour):
        with pytest.raises(ValueError, match="regress"):
            operation(old)
        assert db.execute("SELECT state_json FROM file_hour_state").fetchone()[0] == before
        assert db.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone()[0] == 0
    store.finalize_hour(newer)
    assert db.execute("SELECT SUM(bytes_r) FROM file_sample").fetchone()[0] == 30


@pytest.mark.parametrize("operation", ["apply_pending", "finalize_hour"])
@pytest.mark.parametrize("counter", ["bytes_r", "skipped_nonfile"])
def test_live_pending_counter_regression_still_raises(db, operation, counter):
    # Falsifier: remove the guard, or allow quarantine on ordinary live writes.
    store = FileHourStore(db)
    original = HourlyAccumulator("boot-A", 0)
    original.add_observation(R, APP, observation(bytes_r=10),
                             replace(ZERO, skipped_nonfile=4))
    store.apply_pending(original.finalize())
    regressed = HourlyAccumulator("boot-A", 0)
    regressed.add_observation(R, APP,
        observation(bytes_r=9 if counter == "bytes_r" else 10),
        replace(ZERO, skipped_nonfile=3 if counter == "skipped_nonfile" else 4))
    tables = ("file_hour_state", "file_sample", "file_coverage", "gap", "boot")
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    with pytest.raises(ValueError, match="pending state must not regress counters"):
        getattr(store, operation)(regressed.finalize())
    assert {table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables} == before
    assert not db.in_transaction


def test_quarantine_pending_gap_and_state_deletion_are_atomic(db):
    # Falsifier: persist the gap separately so a failed delete can duplicate loss.
    import sqlite3

    store = FileHourStore(db)
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(bytes_r=17), ZERO)
    store.apply_pending(acc.finalize())
    before = db.execute("SELECT * FROM file_hour_state").fetchall()
    db.execute("CREATE TEMP TRIGGER fail_quarantine BEFORE DELETE ON main.file_hour_state "
               "BEGIN SELECT RAISE(ABORT,'quarantine delete fault'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="quarantine delete fault"):
        store.quarantine_pending("boot-A", 0)
    assert db.execute("SELECT * FROM file_hour_state").fetchall() == before
    assert db.execute("SELECT * FROM gap").fetchall() == []
    assert db.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone() == (0,)
    assert not db.in_transaction
    db.execute("DROP TRIGGER fail_quarantine")
    db.commit()
    store.quarantine_pending("boot-A", 0)
    assert db.execute("SELECT * FROM file_hour_state").fetchall() == []
    assert db.execute("SELECT from_mono_ns,to_mono_ns FROM gap").fetchall() == [(0, FILE_HOUR_NS)]
    assert not db.in_transaction


def test_public_snapshot_with_pending_receipts_consumes_base_observation_type(db):
    # Falsifier: private dataclass subclass equality rejects the public transport interface.
    from appio.contracts import HourlyFileSnapshot
    acc = HourlyAccumulator("boot-A", 0)
    row = observation(bytes_r=10)
    acc.add_observation(R, APP, row, ZERO)
    store = FileHourStore(db)
    store.apply_pending(acc.finalize())
    plain = HourlyFileSnapshot("boot-A", 0, ((APP, (row,), ZERO),), acc.finalize().unknown_owner)
    assert store.finalize_hour(plain)
    assert db.execute("SELECT bytes_r FROM file_sample").fetchall() == [(10,)]


def test_frame_transaction_retains_control_of_successful_pending_and_finalization(db):
    # Falsifier: internal COMMIT publishes a receipt/hour independently of its surrounding frame.
    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(), ZERO)
    db.execute("BEGIN IMMEDIATE")
    FileHourStore(db).apply_pending(acc.finalize())
    FileHourStore(db).finalize_hour(acc.finalize())
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT count(*) FROM path").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM file_sample").fetchone()[0] == 0
    assert db.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone()[0] == 0


def test_finalize_state_deletion_fault_rolls_back_the_entire_hour(db):
    # Falsifier: publish coverage, totals or the watermark if pending-state removal fails.
    import sqlite3

    acc = HourlyAccumulator("boot-A", 0)
    acc.add_observation(R, APP, observation(bytes_r=17), ZERO)
    store = FileHourStore(db)
    store.apply_pending(acc.finalize())
    tables = ("path", "file_sample", "file_coverage", "file_hour_state",
              "app_boot_file_total", "app_boot_file_coverage", "file_overflow_unknown",
              "boot_file_overflow_unknown", "boot")
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db.execute("CREATE TEMP TRIGGER fail_file_state_delete BEFORE DELETE ON main.file_hour_state "
               "BEGIN SELECT RAISE(ABORT,'pending delete fault'); END")
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="pending delete fault"):
        store.finalize_hour(acc.finalize())

    assert not db.in_transaction
    assert {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before
    db.execute("DROP TRIGGER fail_file_state_delete")
    db.commit()
    assert store.finalize_hour(acc.finalize())
    assert db.execute("SELECT count(*) FROM file_hour_state").fetchone() == (0,)


def test_enclosing_frame_rolls_back_receipt_before_separate_overflow_diagnostic(db):
    # Falsifier: either publish a failed frame's receipt or lose A21 evidence after rollback.
    store = FileHourStore(db)
    first = HourlyAccumulator("boot-A", 0)
    first.add_observation(R, APP, observation(bytes_r=SQLITE_INT_MAX), ZERO)
    store.finalize_hour(first.finalize())
    second = HourlyAccumulator("boot-A", FILE_HOUR_NS)
    second.add_observation(Receipt("gui", 1, 1), APP, observation(bytes_r=1, last_seen_mono_ns=FILE_HOUR_NS+10), ZERO)
    snapshot = second.finalize()
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE app SET name='failed-frame'")
    store.apply_pending(snapshot)
    with pytest.raises(CounterOverflowError) as caught:
        store.finalize_hour(snapshot)
    assert db.in_transaction
    assert db.execute("SELECT count(*) FROM store_diagnostic").fetchone()[0] == 0
    with pytest.raises(ValueError, match="rolled back"):
        store.record_overflow(snapshot, caught.value)
    db.rollback()
    store.record_overflow(snapshot, caught.value)
    assert db.execute("SELECT name FROM app").fetchone()[0] == "Demo"
    assert db.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone()[0] == FILE_HOUR_NS
    assert db.execute("SELECT count(*) FROM file_hour_state").fetchone()[0] == 0
    assert db.execute("SELECT code,field FROM store_diagnostic").fetchone() == ("counter_overflow", "bytes_r")


def test_finalization_cannot_skip_an_earlier_pending_hour(db):
    # Falsifier: advance watermark past a pending app-hour and strand its observations.
    store = FileHourStore(db)
    first = HourlyAccumulator("boot-A", 0)
    first.add_observation(R, APP, observation(), ZERO)
    store.apply_pending(first.finalize())
    second = HourlyAccumulator("boot-A", FILE_HOUR_NS)
    second.add_observation(Receipt("gui", 1, 1), APP, None, ZERO)
    with pytest.raises(ValueError, match="chronological"):
        store.finalize_hour(second.finalize())
    assert db.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone()[0] == 0
