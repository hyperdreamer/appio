#!/usr/bin/env python3
"""Produce the frozen acceptance capture from a live appio store.

The acceptance contract needs a store that existed *before* the code under test, so it cannot be
synthesised. This tool reproduces the capture that the acceptance run used, byte-for-byte, on any
host with the daemon installed:

    python tests/p4/acceptance/capture_store.py /var/lib/appio/iod.db /tmp/appio-files-tab-capture

It uses ``sqlite3 .backup`` (a consistent single-transaction copy) and then records the sha256 and
the independent expectations in ``capture.json``. ``verify`` re-checks an existing capture against
the recorded digest, which is what the acceptance test does before and after it runs.

Nothing here needs root if the store is readable; on the target host it is root-owned, which is why
the PM captures it once rather than every lane doing so.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

EXPECTED_SHA256 = "5eea10690422e3eba12e3376044c953dfcfe1ae3657bf13e34cb42d5bfe891e6"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(capture_dir: Path, *, expect: str | None = EXPECTED_SHA256) -> dict:
    snapshot = capture_dir / "iod.sqlite"
    if not snapshot.exists():
        raise SystemExit(f"missing capture: {snapshot}")
    actual = digest(snapshot)
    if expect is not None and actual != expect:
        raise SystemExit(f"capture digest drift: {actual} != {expect}")
    manifest = json.loads((capture_dir / "capture.json").read_text())
    if manifest["snapshot"]["sha256"] != actual:
        raise SystemExit("manifest disagrees with the snapshot digest")
    return manifest


def capture(source: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = out_dir / "iod.sqlite"
    if snapshot.exists():
        snapshot.unlink()
    subprocess.run(["sqlite3", str(source), f".backup {snapshot}"], check=True)
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    boot, watermark = connection.execute(
        "SELECT boot_id, file_finalized_through_mono_ns FROM boot ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("file_sample", "file_coverage", "app_boot_file_total", "app_boot_file_coverage", "gap")
    }
    gaps = connection.execute(
        "SELECT COUNT(*) FROM gap WHERE boot_id=? AND to_mono_ns<=?", (boot, watermark)
    ).fetchone()[0]
    apps = connection.execute(
        "SELECT COUNT(DISTINCT app_id) FROM file_sample WHERE boot_id=?", (boot,)
    ).fetchone()[0]
    connection.close()
    manifest = {
        "captured_at": None,
        "capture_method": "sqlite3 .backup of the live daemon store (consistent, single transaction)",
        "source": {"path": str(source)},
        "snapshot": {"path": str(snapshot), "sha256": digest(snapshot), "bytes": snapshot.stat().st_size},
        "boot_id": boot,
        "file_finalized_through_mono_ns": watermark,
        "row_counts": counts,
        "gaps_in_finalized_window": gaps,
        "apps_with_file_rows": apps,
    }
    (out_dir / "capture.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"captured {snapshot} sha256={manifest['snapshot']['sha256']}")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "verify":
        manifest = verify(Path(argv[2]))
        print(json.dumps({k: manifest[k] for k in ("boot_id", "row_counts", "gaps_in_finalized_window")}, indent=2))
        return 0
    if len(argv) == 4 and argv[1] == "capture":
        capture(Path(argv[2]), Path(argv[3]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
