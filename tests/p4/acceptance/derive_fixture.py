#!/usr/bin/env python3
"""Derive the committed pivot-case fixture from the frozen capture.

The capture is 1.4 GB and cannot live in the repository, but acceptance must be reproducible from
HEAD alone. This script extracts a self-contained slice that preserves the property under test — an
hour-aligned served interval whose unproved segments are covered by gaps that begin and end off the
hour — and records the digest of both the uncompressed fixture and its gzip.

    python tests/p4/acceptance/derive_fixture.py derive  /tmp/appio-files-tab-capture/iod.sqlite
    python tests/p4/acceptance/derive_fixture.py verify  tests/p4/acceptance/files_tab_pivot_case.sqlite.gz

What is kept: every table that describes identity, apps, paths and boot totals in full; `gap` and
`collector_interval` from 40h to 64h so the run boundaries survive; `file_coverage`, `file_sample`
and `file_overflow_unknown` from 46h to 54h. What is dropped: `sample`, `sample_coverage`,
`sample_dev` and the rollups — they are process-side volume, and the fixture's job is the file side.
`PRAGMA user_version` is copied, because appio refuses to migrate a populated version-0 database.
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
import sys
from pathlib import Path

HOUR = 3_600_000_000_000
FULL = ["boot", "clock_map", "collector_epoch", "sampler_checkpoint", "app", "app_boot_total",
        "app_boot_file_total", "app_boot_file_coverage", "app_lifetime_total", "app_boot_dev_total",
        "boot_file_overflow_unknown", "path_alias", "file_hour_state", "path"]
WIDE = {"gap": "to_mono_ns", "collector_interval": "to_mono_ns"}
NARROW = {"file_coverage": "ts_mono_ns", "file_sample": "ts_mono_ns", "file_overflow_unknown": "ts_mono_ns"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def derive(capture: Path, out: Path) -> None:
    if out.exists():
        out.unlink()
    src = sqlite3.connect(f"file:{capture}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    user_version = src.execute("PRAGMA user_version").fetchone()[0]
    application_id = src.execute("PRAGMA application_id").fetchone()[0]
    for (statement,) in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ):
        dst.execute(statement)
    boot = src.execute("SELECT boot_id FROM boot ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    for table in FULL:
        columns = [row[1] for row in src.execute(f"PRAGMA table_info({table})")]
        if "boot_id" in columns:
            rows = src.execute(f"SELECT * FROM {table} WHERE boot_id=?", (boot,)).fetchall()
        else:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            dst.executemany(f"INSERT INTO {table} VALUES({','.join('?' * len(columns))})", rows)
    for table, column in {**WIDE, **NARROW}.items():
        columns = [row[1] for row in src.execute(f"PRAGMA table_info({table})")]
        low, high = (40 * HOUR, 64 * HOUR) if table in WIDE else (46 * HOUR, 54 * HOUR)
        rows = src.execute(
            f"SELECT * FROM {table} WHERE boot_id=? AND {column}>=? AND {column}<?", (boot, low, high)
        ).fetchall()
        if rows:
            dst.executemany(f"INSERT INTO {table} VALUES({','.join('?' * len(columns))})", rows)
    dst.execute(f"PRAGMA user_version={user_version}")
    dst.execute(f"PRAGMA application_id={application_id}")
    dst.commit()
    dst.execute("VACUUM")
    dst.commit()
    dst.close()
    src.close()
    print(f"derived {out} sha256={sha256(out)}")


def verify(archive: Path, expect: str | None = None) -> Path:
    import tempfile
    payload = gzip.decompress(archive.read_bytes())
    actual = hashlib.sha256(payload).hexdigest()
    if expect is not None and actual != expect:
        raise SystemExit(f"fixture digest drift: {actual} != {expect}")
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    handle.write(payload)
    handle.close()
    return Path(handle.name)


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "derive":
        derive(Path(argv[2]), Path(argv[3]))
        return 0
    if len(argv) == 3 and argv[1] == "verify":
        print(verify(Path(argv[2])))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
