"""Schema-v1 contracts, with the signed DDL as an independent executable oracle."""

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from appio import migrations
from appio.migrations import migrate_empty, open_database, validate_schema
from appio.schema import SCHEMA_SQL, SCHEMA_VERSION


TABLE_KEYS = {
    "boot": ("boot_id",),
    "app": ("app_id",),
    "launch": ("launch_id",),
    "sample": ("boot_id", "source", "epoch", "seq", "app_id", "launch_id", "ts_mono_ns"),
    "sample_coverage": ("boot_id", "source", "epoch", "seq", "app_id"),
    "sample_dev": ("boot_id", "source", "epoch", "seq", "app_id", "dev"),
    "path": ("path_id",),
    "path_alias": ("path_id", "basename"),
    "file_sample": ("boot_id", "app_id", "ts_mono_ns", "path_id"),
    "file_coverage": ("boot_id", "app_id", "ts_mono_ns"),
    "file_hour_state": ("boot_id", "ts_mono_ns"),
    "sampler_checkpoint": ("source",),
    "file_overflow_unknown": ("boot_id", "ts_mono_ns"),
    "boot_file_overflow_unknown": ("boot_id",),
    "collector_epoch": ("source",),
    "collector_interval": ("source", "boot_id", "epoch"),
    "handover": ("boot_id", "cutoff_mono_ns"),
    "clock_map": ("boot_id", "segment_start_mono_ns"),
    "rollup_app": ("boot_id", "app_id", "bucket_start_mono_ns", "bucket_secs"),
    "rollup_dev": ("boot_id", "app_id", "bucket_start_mono_ns", "bucket_secs", "dev"),
    "app_boot_total": ("boot_id", "app_id"),
    "app_boot_dev_total": ("boot_id", "app_id", "dev"),
    "app_boot_file_total": ("boot_id", "app_id", "path_id"),
    "app_boot_file_coverage": ("boot_id", "app_id"),
    "app_lifetime_total": ("app_id",),
    "gap": ("gap_id",),
    "store_diagnostic": ("diagnostic_id",),
}
ROWID_TABLES = {
    "boot", "app", "launch", "path", "sampler_checkpoint",
    "boot_file_overflow_unknown", "collector_epoch", "app_lifetime_total",
    "gap", "store_diagnostic",
}
INDEX_NAMES = {
    "app_name_idx", "launch_app_time_idx", "sample_app_time_idx",
    "sample_coverage_app_time_idx", "sample_dev_app_time_idx", "path_basename_idx",
    "path_alias_basename_idx", "file_sample_app_time_idx", "file_sample_last_seen_idx",
    "file_coverage_app_time_idx", "store_diagnostic_time_idx",
    "collector_interval_time_idx", "rollup_app_time_idx", "rollup_dev_time_idx",
    "gap_time_idx",
}
JSON_COLUMNS = [
    ("launch", "coverage_json"), ("sample_coverage", "coverage_json"),
    ("file_coverage", "coverage_json"), ("file_hour_state", "state_json"),
    ("sampler_checkpoint", "state_json"), ("file_overflow_unknown", "coverage_json"),
    ("boot_file_overflow_unknown", "coverage_json"), ("handover", "baselines_json"),
    ("rollup_app", "coverage_json"), ("app_boot_total", "coverage_json"),
    ("app_boot_file_coverage", "coverage_json"), ("app_lifetime_total", "coverage_json"),
]


def signed_ddl():
    specification = (Path(__file__).resolve().parents[1] / "docs/superpowers/specs/2026-10-21-app-io-monitor-spec.md").read_text()
    return specification.split("### 6.2 DDL", 1)[1].split("```sql", 1)[1].split("```", 1)[0].strip()


def schema_objects(connection):
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
    ).fetchall()


@pytest.fixture
def database():
    with closing(sqlite3.connect(":memory:")) as connection:
        migrate_empty(connection)
        yield connection


@pytest.fixture
def populated(database):
    """Synthetic valid rows cover every FK parent, never a host observation."""
    rows = [
        ("boot", ("b", 1, 0, 0, 0.0, "gui")),
        ("app", (1, "example.app", "Example", None, 0.0, 0.0, "user")),
        ("launch", (1, 1, "b", 2, 0, 0, None, 1, "{}")),
        ("sample", ("b", "gui", 1, 0, 1, 1, 0, 10, None, None, 0.0, 0, 0, 0, 0, "baseline", 0, 0)),
        ("sample_coverage", ("b", "gui", 1, 0, 1, 0, "{}")),
        ("sample_dev", ("b", "gui", 1, 0, 1, 0, "8:0", 0, 0)),
        ("path", (1, "uuid", "8:0", 1, 0, "example", "resolved", 0, "b", 0, "b", 0)),
        ("path_alias", (1, "example", "b", 0, "b", 0)),
        ("file_sample", ("b", "gui", 1, 0, 1, 0, "8:0", 1, 0, 0, 0, 0, 0)),
        ("file_coverage", ("b", "gui", 1, 0, 1, 0, "{}", *([0] * 15))),
        ("file_hour_state", ("b", 0, "{}")),
        ("sampler_checkpoint", ("gui", "b", 1, 0, 0, "{}")),
        ("file_overflow_unknown", ("b", "gui", 1, 0, 0, "overflow_owner_unknown", 0, 0, 0, "{}")),
        ("boot_file_overflow_unknown", ("b", "overflow_owner_unknown", 0, 0, 0, "{}")),
        ("collector_epoch", ("gui", "b", 1, None, 0)),
        ("collector_interval", ("gui", "b", 1, 0, 10)),
        ("handover", ("b", "gui", "iod", 0, "{}")),
        ("clock_map", ("b", 0, -1)),
        ("rollup_app", ("b", 1, 0, 60, 0, 0, 0, 0, None, None, None, "{}")),
        ("rollup_dev", ("b", 1, 0, 60, "8:0", 0, 0)),
        ("app_boot_total", ("b", 1, 0, 0, 0, 0, None, None, None, "{}")),
        ("app_boot_dev_total", ("b", 1, "8:0", 0, 0)),
        ("app_boot_file_total", ("b", 1, 1, 0, 0, 0, 0, 0)),
        ("app_boot_file_coverage", ("b", 1, "{}")),
        ("app_lifetime_total", (1, 0, 0, 0, 0, "{}", "b", 0)),
        ("gap", (1, "b", "gui", 1, 0, 0, 10, "collector_gap")),
        ("store_diagnostic", (1, "b", "gui", 1, 0, None, 0, "counter_overflow", "rchar")),
    ]
    for table, values in rows:
        placeholders = ",".join("?" for _ in values)
        database.execute(f"INSERT INTO {table} VALUES ({placeholders})", values)
    database.commit()
    return database


def test_fresh_migration_has_exact_strict_tables_keys_and_indexes(database):
    # Falsified by a missing/extra table, wrong storage mode/key/index or version.
    tables = {row[1]: row for row in database.execute("PRAGMA table_list") if row[1] not in {"sqlite_schema", "sqlite_temp_schema"}}
    assert set(tables) == set(TABLE_KEYS)
    assert len(tables) == 27
    for name, expected_key in TABLE_KEYS.items():
        assert tables[name][5] == 1, name  # STRICT
        assert tables[name][4] == (name not in ROWID_TABLES), name
        info = database.execute(f"PRAGMA table_info({name})").fetchall()
        assert tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]) == expected_key
    indexes = {row[0] for row in database.execute("SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL")}
    assert indexes == INDEX_NAMES
    assert database.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1


def test_entire_manifest_matches_signed_ddl(database):
    # Independent executable oracle detects any changed column, CHECK, FK or index.
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(signed_ddl())
        assert schema_objects(database) == schema_objects(expected)


def test_all_tables_accept_synthetic_rows(populated):
    for name in TABLE_KEYS:
        assert populated.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 1


@pytest.mark.parametrize("table,column,value", [
    ("boot", "file_finalized_through_mono_ns", 1),
    ("sample", "rchar", -1), ("sample", "wchar", -1),
    ("sample", "read_bytes", -1), ("sample", "write_bytes", -1),
    ("sample", "rchar", 1.5), ("sample", "delta_kind", "invalid"),
    ("sample", "observed_mono_ns", -1),
    ("sample", "interval_start_mono_ns", 0), ("sample", "observed_ns", 1),
    ("sample", "first_seen_partial", 2), ("sample", "spans_suspend", 2),
    ("file_sample", "ts_mono_ns", 1),
    ("file_sample", "last_seen_mono_ns", 3_600_000_000_000),
    ("file_coverage", "ts_mono_ns", 1), ("file_coverage", "hook_vfs_read", -1),
    ("file_hour_state", "ts_mono_ns", 1),
    ("file_overflow_unknown", "ts_mono_ns", 1),
    ("file_overflow_unknown", "owner_key", "attributed"),
    ("rollup_app", "bucket_secs", 1), ("rollup_dev", "bucket_secs", 1),
    ("rollup_app", "peak_total_bps", -1),
    ("store_diagnostic", "code", "other"),
    ("collector_interval", "to_mono_ns", 0),
    ("handover", "to_source", "gui"),
    ("path", "last_mono_ns", -1), ("app", "first_seen", float("inf")),
])
def test_representative_constraints_reject_invalid_rows(populated, table, column, value):
    # Removing the named column's CHECK (or STRICT storage) allows this update.
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute(f"UPDATE {table} SET {column}=?", (value,))


@pytest.mark.parametrize("table,column", JSON_COLUMNS)
def test_json_valid_checks(populated, table, column):
    # Removing json_valid permits malformed coverage/recovery JSON on that table.
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute(f"UPDATE {table} SET {column}=?", ("not-json",))


def test_sample_accepts_distinct_attributed_times_for_one_frame(populated):
    populated.execute("INSERT INTO sample SELECT boot_id,source,epoch,seq,app_id,launch_id,1,observed_mono_ns,interval_start_mono_ns,observed_ns,ts_wall,rchar,wchar,read_bytes,write_bytes,delta_kind,first_seen_partial,spans_suspend FROM sample")
    assert populated.execute("SELECT ts_mono_ns FROM sample ORDER BY ts_mono_ns").fetchall() == [(0,), (1,)]
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute("INSERT INTO sample SELECT * FROM sample WHERE ts_mono_ns=0")


def test_sample_coverage_is_one_receipt_per_app_not_per_timestamp(populated):
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute("INSERT INTO sample_coverage SELECT boot_id,source,epoch,seq,app_id,1,coverage_json FROM sample_coverage")


@pytest.mark.parametrize("table", ["file_sample", "file_coverage"])
def test_file_hours_are_unique_across_collectors(populated, table):
    columns = [row[1] for row in populated.execute(f"PRAGMA table_info({table})")]
    values = ["'iod'" if column == "source" else "2" if column == "epoch" else column for column in columns]
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute(f"INSERT INTO {table} SELECT {','.join(values)} FROM {table}")


@pytest.mark.parametrize("table,column", [("sample", "boot_id"), ("sample", "app_id"), ("launch", "app_id"), ("file_sample", "path_id"), ("path", "last_boot_id"), ("app_lifetime_total", "updated_boot_id")])
def test_foreign_keys_enforced(populated, table, column):
    assert populated.execute("PRAGMA foreign_keys").fetchone() == (1,)
    missing = "missing" if column.endswith("boot_id") else 999
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute(f"UPDATE {table} SET {column}=?", (missing,))


def test_launch_and_diagnostic_uniqueness_and_alias_cascade(populated):
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute("INSERT INTO launch SELECT 2,app_id,boot_id,root_pid,root_start_ticks,started_mono_ns,ended_mono_ns,proc_count,coverage_json FROM launch")
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute("INSERT INTO store_diagnostic SELECT 2,boot_id,source,epoch,seq,app_id,1,code,field FROM store_diagnostic")
    populated.execute("DELETE FROM file_sample")
    populated.execute("DELETE FROM app_boot_file_total")
    populated.execute("DELETE FROM path")
    assert populated.execute("SELECT count(*) FROM path_alias").fetchone() == (0,)


def test_sqlite_integer_extremes_and_nullable_peak_evidence(populated):
    populated.execute("UPDATE sample SET rchar=9223372036854775807,interval_start_mono_ns=0,observed_ns=10")
    populated.execute("UPDATE clock_map SET wall_offset_ns=-9223372036854775808")
    for table in ("rollup_app", "app_boot_total"):
        assert populated.execute(f"SELECT peak_read_bps,peak_write_bps,peak_total_bps FROM {table}").fetchone() == (None, None, None)
        populated.execute(f"UPDATE {table} SET peak_total_bps=9223372036854775807")
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute("UPDATE sample SET rchar=9223372036854775808")


def test_migration_enables_foreign_keys_and_has_one_transaction():
    with closing(sqlite3.connect(":memory:")) as connection:
        trace = []
        connection.set_trace_callback(trace.append)
        migrate_empty(connection)
        statements = [sql.strip().upper() for sql in trace]
        begin = statements.index("BEGIN IMMEDIATE;")
        assert statements.count("BEGIN IMMEDIATE;") == 1
        assert statements.count("COMMIT;") == 1
        assert any(sql.startswith("PRAGMA FOREIGN_KEYS") for sql in statements[:begin])
        first_create = next(i for i, sql in enumerate(statements) if sql.startswith("CREATE TABLE"))
        assert any("USER_VERSION" in sql for sql in statements[begin + 1:first_create])
        assert any("SQLITE_SCHEMA" in sql for sql in statements[begin + 1:first_create])
        assert not connection.in_transaction


def test_open_database_creates_a_valid_file(tmp_path):
    path = tmp_path / "fresh.sqlite"
    with closing(open_database(path)) as connection:
        validate_schema(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    with closing(sqlite3.connect(path)) as connection:
        assert len([row for row in connection.execute("PRAGMA table_list") if row[5]]) == 27


@pytest.mark.parametrize("state", ["nonempty-zero", "view-only-zero", "higher-version", "negative-version"])
def test_rejected_open_preserves_existing_database_byte_for_byte(tmp_path, state):
    path = tmp_path / "existing.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        if state == "nonempty-zero":
            connection.execute("CREATE TABLE original (payload TEXT)")
            connection.execute("INSERT INTO original VALUES ('preserve me')")
        elif state == "view-only-zero":
            connection.execute("CREATE VIEW original AS SELECT 1")
        else:
            connection.execute(f"PRAGMA user_version={'2' if state == 'higher-version' else '-1'}")
        connection.commit()
    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        open_database(path)
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == before


def test_version_one_is_validated_without_recreation_or_file_writes(tmp_path):
    path = tmp_path / "valid.sqlite"
    with closing(open_database(path)) as connection:
        connection.execute("INSERT INTO boot VALUES ('b',1,0,0,0.0,'gui')")
        connection.commit()
    before = path.read_bytes()
    with closing(open_database(path)) as connection:
        trace = []
        connection.set_trace_callback(trace.append)
        migrate_empty(connection)
        assert connection.execute("SELECT boot_id FROM boot").fetchall() == [("b",)]
        assert not any(sql.lstrip().upper().startswith(("CREATE", "BEGIN", "COMMIT", "INSERT", "UPDATE", "DELETE")) for sql in trace)
    assert path.read_bytes() == before


@pytest.mark.parametrize("original,replacement", [
    ("started_mono_ns INTEGER NOT NULL CHECK (started_mono_ns >= 0)", "started_mono_ns INTEGER NOT NULL"),
    ("UNIQUE (launch_id, app_id, boot_id),", ""),
    ("PRIMARY KEY (boot_id, source, epoch, seq, app_id, launch_id, ts_mono_ns)", "PRIMARY KEY (boot_id, source, epoch, seq, app_id, launch_id)"),
    ("REFERENCES app(app_id)", ""),
    ("WITHOUT ROWID, STRICT", "STRICT"),
    (") STRICT;", ");"),
    ("CREATE INDEX app_name_idx ON app(name COLLATE NOCASE, key);", ""),
    ("last_seen_mono_ns DESC, path_id", "last_seen_mono_ns, path_id"),
    ("observed_mono_ns INTEGER NOT NULL", "observed_mono_ns TEXT NOT NULL"),
])
def test_version_one_schema_drift_is_rejected_without_mutation(tmp_path, original, replacement):
    path = tmp_path / "drift.sqlite"
    ddl = signed_ddl()
    assert original in ddl
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(ddl.replace(original, replacement))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        open_database(path)
    assert path.read_bytes() == before


def test_validate_schema_rejects_wrong_version_and_extra_objects(database):
    database.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError):
        validate_schema(database)
    database.execute("PRAGMA user_version=1")
    database.execute("CREATE TABLE unexpected (id INTEGER) STRICT")
    with pytest.raises(ValueError):
        validate_schema(database)


@pytest.mark.parametrize("autocommit", [sqlite3.LEGACY_TRANSACTION_CONTROL, True, False])
def test_caller_transaction_is_never_committed_or_rolled_back(autocommit):
    with closing(sqlite3.connect(":memory:", autocommit=autocommit)) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        connection.execute("CREATE TABLE caller_owned (id INTEGER)")
        with pytest.raises(ValueError):
            migrate_empty(connection)
        assert connection.in_transaction
        assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == [("caller_owned",)]


@pytest.mark.parametrize("autocommit", [sqlite3.LEGACY_TRANSACTION_CONTROL, True])
def test_failed_ddl_rolls_back_every_table_and_version(tmp_path, monkeypatch, autocommit):
    path = tmp_path / "failed.sqlite"
    broken = SCHEMA_SQL.replace("PRAGMA user_version = 1;", "PRAGMA user_version = 1;\nCREATE TABLE deliberate_partial (id INTEGER) STRICT;\nSELECT nonexistent_migration_function();")
    assert broken != SCHEMA_SQL
    monkeypatch.setattr(migrations, "SCHEMA_SQL", broken)
    with closing(sqlite3.connect(path, autocommit=autocommit)) as connection:
        with pytest.raises(sqlite3.OperationalError, match="nonexistent_migration_function"):
            migrate_empty(connection)
        assert not connection.in_transaction
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert schema_objects(connection) == []
    with closing(sqlite3.connect(path)) as connection:
        assert schema_objects(connection) == []


def test_explicit_autocommit_connection_is_supported():
    with closing(sqlite3.connect(":memory:", autocommit=True)) as connection:
        migrate_empty(connection)
        validate_schema(connection)
        assert not connection.in_transaction


@pytest.mark.parametrize("concurrent_change", ["table", "version"])
def test_empty_preconditions_are_rechecked_after_acquiring_write_lock(tmp_path, concurrent_change):
    # A second real connection changes the file after the unlocked precheck.
    # Omitting the check after BEGIN would either create a mixed schema or
    # overwrite the competing writer's version instead of failing closed.
    path = tmp_path / "racing.sqlite"
    with closing(sqlite3.connect(path)) as connection, closing(sqlite3.connect(path)) as writer:
        def competing_write(statement):
            if statement.strip().upper() == "BEGIN IMMEDIATE;":
                if concurrent_change == "table":
                    writer.execute("CREATE TABLE competing_writer (id INTEGER)")
                else:
                    writer.execute("PRAGMA user_version=2")
                writer.commit()

        connection.set_trace_callback(competing_write)
        with pytest.raises(ValueError):
            migrate_empty(connection)
        assert not connection.in_transaction
        objects = connection.execute("SELECT name FROM sqlite_schema").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        expected = ([("competing_writer",)], 0) if concurrent_change == "table" else ([], 2)
        assert (objects, version) == expected


def test_runtime_without_json_functions_is_rejected_without_tables():
    # A real SQLite connection with its JSON function disabled, not a mock DB.
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("json_valid", 1, None)
        with pytest.raises(ValueError, match="JSON"):
            migrate_empty(connection)
        assert schema_objects(connection) == []
        assert not connection.in_transaction


def test_old_sqlite_runtime_is_rejected_before_ddl(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(ValueError, match="3.37.0"):
            migrate_empty(connection)
        assert schema_objects(connection) == []
        assert not connection.in_transaction
