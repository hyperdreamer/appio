"""Real retention transactions under controlled WAL-space pressure."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from appio.quota import QuotaPruner
from test_file_store import db
from test_quota import DAY, coverage_json, seed_core_with_materialization


def _seed_expired(db, count):
    seed_core_with_materialization(db)
    db.executemany(
        "INSERT INTO sample_coverage VALUES('boot-A','gui',1,?,1,?,?)",
        ((i, i, coverage_json()) for i in range(1, count)),
    )
    db.commit()
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0


def _actual_full_error():
    # Obtain SQLite's real error code without filling the test host's filesystem.
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA page_size=1024")
        con.execute("PRAGMA max_page_count=2")
        con.execute("CREATE TABLE filler(data BLOB)")
        with pytest.raises(sqlite3.OperationalError) as error:
            con.execute("INSERT INTO filler VALUES(zeroblob(65536))")
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_FULL
        return error.value
    finally:
        con.close()


def test_expiry_recovers_from_wal_full_using_checkpointed_smaller_transactions(db, monkeypatch):
    # Mutant: remove the SQLITE_FULL retry, or retry small chunks without
    # checkpoints. Either exhausts the simulated WAL budget before progress.
    _seed_expired(db, 16)
    pruner = QuotaPruner(db, [])
    full = _actual_full_error()
    state = {"writes": 0, "transaction_start": 0, "rollbacks": 0}
    original_delete = pruner._delete
    original_checkpoint = pruner._checkpoint

    def trace(sql):
        if sql.upper().startswith("BEGIN IMMEDIATE"):
            state["transaction_start"] = state["writes"]
        elif sql.upper().startswith("ROLLBACK"):
            state["writes"] = state["transaction_start"]
            state["rollbacks"] += 1

    def limited_delete(unit):
        if state["writes"] >= 4:
            raise full
        stats = original_delete(unit)
        state["writes"] += 1
        return stats

    def checkpoint(*, truncate=True):
        result = original_checkpoint(truncate=truncate)
        if result:
            state["writes"] = 0
        return result

    monkeypatch.setattr(pruner, "_delete", limited_delete)
    monkeypatch.setattr(pruner, "_checkpoint", checkpoint)
    db.set_trace_callback(trace)
    try:
        result = pruner.expire("boot-A", 2 * DAY)
    finally:
        db.set_trace_callback(None)
    assert result.complete, result.reason
    assert [item.primary_key[3] for item in result.deleted] == list(range(16))
    assert state["rollbacks"] > 0
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone() == (0,)
    assert not db.in_transaction
    assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_expiry_does_not_hide_full_disk_when_one_unit_cannot_be_deleted(db, monkeypatch):
    _seed_expired(db, 1)
    pruner = QuotaPruner(db, [])
    full = _actual_full_error()
    attempts = []

    def fail_delete(unit):
        attempts.append(unit)
        raise full

    monkeypatch.setattr(pruner, "_delete", fail_delete)
    with pytest.raises(sqlite3.OperationalError) as error:
        pruner.expire("boot-A", 2 * DAY)
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_FULL
    assert len(attempts) == 2  # one retry after a checkpoint, then propagate
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone() == (1,)
    assert not db.in_transaction
    monkeypatch.undo()
    result = pruner.expire("boot-A", 2 * DAY)
    assert result.complete
    assert len(result.deleted) == 1
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone() == (0,)


def test_expiry_batches_many_units_in_one_healthy_transaction(db):
    # Mutant: revert to small transactions and checkpoints on a healthy disk.
    _seed_expired(db, 300)
    pruner = QuotaPruner(db, [])
    events = []
    db.set_trace_callback(events.append)
    try:
        result = pruner.expire("boot-A", 2 * DAY)
    finally:
        db.set_trace_callback(None)
    assert result.complete, result.reason
    assert len(result.deleted) == 300
    assert db.execute("SELECT count(*) FROM sample_coverage").fetchone() == (0,)
    assert sum(sql.upper().startswith("COMMIT") for sql in events) == 2
    assert sum(sql.upper().startswith("PRAGMA WAL_CHECKPOINT") for sql in events) == 2


@pytest.mark.parametrize("count", [1, 2048], ids=["single-unit", "whole-batch"])
def test_expiry_recovers_from_actual_wal_write_enospc(db, tmp_path, count):
    # A real SQLite WAL writer receives ENOSPC. The shim never touches any
    # other file and the host filesystem is not filled.
    compiler = shutil.which("cc")
    if sys.platform != "linux" or compiler is None:
        pytest.skip("WAL ENOSPC probe requires Linux and a C compiler")
    _seed_expired(db, count)
    database = Path(db.execute("PRAGMA database_list").fetchone()[2])
    fixture = Path(__file__).parent / "fixtures" / "wal_write_limit.c"
    shim = tmp_path / "wal_write_limit.so"
    subprocess.run([compiler, "-shared", "-fPIC", str(fixture), "-o", str(shim), "-ldl"],
                   check=True, capture_output=True, text=True, timeout=30)
    script = """
import os
from pathlib import Path
import sys
from appio.migrations import open_database
from appio.quota import QuotaPruner
path = Path(sys.argv[1])
db = open_database(path)
assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA busy_timeout=10')
count = int(sys.argv[2])
if count == 1:
    # Fill the existing WAL with committed, checkpointable pages. The single
    # deletion cannot extend it, but can reuse its space after a checkpoint.
    db.execute('PRAGMA wal_autocheckpoint=0')
    db.execute('CREATE TABLE audit_pad(data BLOB)')
    db.execute('INSERT INTO audit_pad VALUES (zeroblob(131072))')
    db.commit()
    limit = Path(str(path) + '-wal').stat().st_size
else:
    limit = 200000
os.environ['APPIO_TEST_WAL_LIMIT'] = str(limit)
result = QuotaPruner(db, [path]).expire('boot-A', 2 * 86400000000000)
assert result.complete, result.reason
assert len(result.deleted) == count, len(result.deleted)
assert db.execute('SELECT count(*) FROM sample_coverage').fetchone() == (0,)
assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
assert db.execute('PRAGMA foreign_key_check').fetchall() == []
assert not db.in_transaction
"""
    env = os.environ.copy()
    env.pop("APPIO_TEST_WAL_LIMIT", None)
    env["LD_PRELOAD"] = str(shim)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.run([sys.executable, "-c", script, str(database), str(count)],
                             env=env, capture_output=True, text=True, timeout=45)
    assert "test: WAL ENOSPC injected" in process.stderr, process.stderr
    assert process.returncode == 0, process.stderr
