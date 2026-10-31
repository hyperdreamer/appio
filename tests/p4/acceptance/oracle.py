#!/usr/bin/env python3
"""Independent expectations for the Files-tab acceptance, computed from raw rows only.

Deliberately stdlib-only and deliberately dumb: it scans ``file_coverage`` timestamps and the
``gap`` table directly, with no import from ``appio``, so its numbers cannot be a restatement of the
code under test. ``oracle-out.json`` beside this file is its committed output for the frozen capture;
the acceptance test compares the served interval against those numbers.

    python tests/p4/acceptance/oracle.py tests/p4/acceptance/capture.json /path/to/iod.sqlite
"""

from __future__ import annotations

import json
import sqlite3
import sys

HOUR = 3_600_000_000_000


def main(argv: list[str]) -> int:
    manifest_path, snapshot = argv[1], argv[2]
    manifest = json.loads(open(manifest_path).read())
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    boot = manifest["boot_id"]
    watermark = manifest["file_finalized_through_mono_ns"]

    hours = sorted(
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT ts_mono_ns FROM file_coverage WHERE boot_id=? AND ts_mono_ns<?",
            (boot, watermark),
        )
    )
    best = (0, 0)
    run_start = run_end = None
    for hour in hours:
        if run_end is not None and hour == run_end:
            run_end = hour + HOUR
        else:
            run_start, run_end = hour, hour + HOUR
        if run_end - run_start > best[1] - best[0]:
            best = (run_start, run_end)

    gaps_inside = connection.execute(
        "SELECT COUNT(*) FROM gap WHERE boot_id=? AND to_mono_ns>? AND from_mono_ns<?",
        (boot, best[0], best[1]),
    ).fetchone()[0]
    apps = connection.execute(
        "SELECT COUNT(DISTINCT app_id) FROM file_sample WHERE boot_id=? AND ts_mono_ns>=? AND ts_mono_ns<?",
        (boot, best[0], best[1]),
    ).fetchone()[0]
    rows = connection.execute(
        "SELECT COUNT(*) FROM file_sample WHERE boot_id=? AND ts_mono_ns>=? AND ts_mono_ns<?",
        (boot, best[0], best[1]),
    ).fetchone()[0]
    release = {
        "capture_sha256": manifest["snapshot"]["sha256"],
        "boot_id": boot,
        "file_finalized_through_mono_ns": watermark,
        "file_bearing_hours": [h // HOUR for h in hours],
        "expected_served_interval_ns": list(best),
        "expected_served_interval_hours": [best[0] // HOUR, best[1] // HOUR],
        "gaps_inside_expected_served_interval": gaps_inside,
        "apps_with_rows_in_served_interval": apps,
        "rows_in_served_interval": rows,
    }
    print(json.dumps(release, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
