"""Atomic empty-database migration and read-only schema-v1 validation."""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
import sqlite3

from appio.contracts import validate_sqlite_integer
from appio.schema import SCHEMA_SQL, SCHEMA_VERSION


def _statements(script: str) -> Iterator[str]:
    """Split complete SQLite statements without breaking quoted semicolons."""
    buffer = []
    for character in script:
        buffer.append(character)
        if character == ";" and sqlite3.complete_statement("".join(buffer)):
            yield "".join(buffer).strip()
            buffer.clear()
    if "".join(buffer).strip():
        raise ValueError("incomplete schema statement")


def _check_runtime(connection: sqlite3.Connection) -> None:
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise ValueError("SQLite 3.37.0 or newer is required for STRICT tables")
    try:
        available = connection.execute("SELECT json_valid('{}')").fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise ValueError("SQLite JSON functions are required") from error
    if available != 1:
        raise ValueError("SQLite JSON functions are required")


def _version(connection: sqlite3.Connection) -> int:
    return validate_sqlite_integer(
        connection.execute("PRAGMA user_version").fetchone()[0], "user_version"
    )


def _require_empty(connection: sqlite3.Connection) -> None:
    if _version(connection) != 0:
        raise ValueError("empty migration requires user_version=0")
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
    ).fetchone() is not None:
        raise ValueError("refusing to migrate a nonempty version-0 database")


def _schema_signature(connection: sqlite3.Connection) -> tuple:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
    ).fetchall()
    return tuple(
        (row[0], row[1], row[2], " ".join(row[3].split())) for row in rows
    )


def validate_schema(connection: sqlite3.Connection) -> None:
    """Reject missing or altered v1 columns, constraints, storage modes or indexes.

    SQLite's stored CREATE statements retain CHECK, UNIQUE, foreign-key and key
    definitions. Compare them with a separately compiled in-memory reference;
    this never issues DDL or changes the user version on the caller's database.
    """
    _check_runtime(connection)
    if _version(connection) != SCHEMA_VERSION:
        raise ValueError(f"schema validation requires user_version={SCHEMA_VERSION}")
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in _statements(SCHEMA_SQL):
            reference.execute(statement)
        if _schema_signature(connection) != _schema_signature(reference):
            raise ValueError("database schema does not match schema version 1")


def migrate_empty(connection: sqlite3.Connection) -> None:
    """Create v1 once, or validate v1; reject unsupported/nonempty state unchanged.

    The signed DDL owns its single transaction. Preconditions are checked both
    before locking (fail closed without writes) and immediately after BEGIN
    IMMEDIATE (exclude another initializer changing the database in between).
    No executescript or connection context manager may implicitly commit a
    caller's transaction. Explicit SQL rollback also works in autocommit mode.
    """
    if connection.in_transaction:
        raise ValueError("migration requires a connection without an active transaction")
    _check_runtime(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("SQLite foreign-key enforcement is required")
    version = _version(connection)
    if version == SCHEMA_VERSION:
        validate_schema(connection)
        return
    if version != 0:
        raise ValueError(f"unsupported database schema version: {version}")
    _require_empty(connection)
    try:
        for statement in _statements(SCHEMA_SQL):
            connection.execute(statement)
            if statement.upper() == "BEGIN IMMEDIATE;":
                _require_empty(connection)
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def open_database(path: Path) -> sqlite3.Connection:
    """Open and migrate/validate a store, closing the connection on any failure."""
    connection = sqlite3.connect(path)
    try:
        migrate_empty(connection)
    except BaseException:
        connection.close()
        raise
    return connection
