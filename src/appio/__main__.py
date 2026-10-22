"""Command-line entry point for the runnable AppIO monitor."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .contracts import RAW_BUCKET_NS, Window
from .live import LiveCollector
from .query import QueryService


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


def _display_end(observed_mono_ns: int) -> int:
    """Expose the current raw bucket through the half-open query window."""

    return (observed_mono_ns // RAW_BUCKET_NS + 1) * RAW_BUCKET_NS


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.interval < 0:
        _parser().error("--interval must be non-negative")

    if args.offscreen:
        # This must happen before QApplication is imported or constructed.
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    database_path = args.db if args.db is not None else _default_database()
    collector = LiveCollector(
        proc_root=args.proc_root,
        cgroup_root=args.cgroup_root,
        database_path=database_path,
    )
    if args.once:
        try:
            collector.tick()
        finally:
            collector.close()
        return 0

    try:
        collector.tick()
    except BaseException:
        collector.close()
        raise

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from appio.ui.main_window import AppIoWindow

    app = QApplication([sys.argv[0]])
    app.setDesktopFileName("appio")
    query_service = QueryService(collector.store)
    end_mono_ns = collector.last_observed_mono_ns
    if end_mono_ns is None:
        raise RuntimeError("the initial live tick did not produce an observation timestamp")
    window = AppIoWindow(query_service, collector.boot_id, _display_end(end_mono_ns))
    window.show()

    timer = QTimer(window)
    timer.setInterval(max(0, round(args.interval * 1000)))

    def collect_and_refresh() -> None:
        try:
            collector.tick()
            observed = collector.last_observed_mono_ns
            if observed is not None:
                window.set_window(
                    Window(kind="since_boot", boot_id=collector.boot_id, end_mono_ns=_display_end(observed))
                )
        except BaseException as error:
            print(f"appio: live collection stopped: {error}", file=sys.stderr)
            timer.stop()
            app.exit(1)

    timer.timeout.connect(collect_and_refresh)
    timer.start()
    try:
        return app.exec()
    finally:
        collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
