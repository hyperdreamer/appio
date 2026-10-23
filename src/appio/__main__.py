"""Command-line entry point for the runnable AppIO monitor."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .live import LiveCollector


def _default_database() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "appio" / "history.db"
    return Path.home() / ".local" / "share" / "appio" / "history.db"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="appio", description="measure application IO activity")
    parser.add_argument("--db", type=Path, default=None, help="SQLite history database")
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    parser.add_argument(
        "--cgroup-root",
        type=Path,
        default=Path("/sys/fs/cgroup"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between live samples (default: 5)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="collect one sample and exit without opening a window",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="use Qt's offscreen platform plugin",
    )
    return parser


def run_legacy_once(args: argparse.Namespace) -> int:
    """Collect one sample on the P1/P2 boundary and exit.

    This is the only collector branch the legacy entry point may construct: no P3
    composition, no daemon client and no privilege module is imported here.
    """

    collector = LiveCollector(
        proc_root=args.proc_root,
        cgroup_root=args.cgroup_root,
        database_path=args.db,
    )
    try:
        collector.tick()
    finally:
        collector.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.interval < 0:
        _parser().error("--interval must be non-negative")

    if args.offscreen:
        # This must happen before QApplication is imported or constructed.
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    args.db = args.db if args.db is not None else _default_database()

    if args.once:
        return run_legacy_once(args)

    # The P3 composition (client, coordinator, router and window) is imported only
    # on the interactive path, and it is what decides whether to collect locally.
    from appio.ui.collector_window import run_interactive

    return run_interactive(args)


if __name__ == "__main__":
    raise SystemExit(main())
