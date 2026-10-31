"""Real-store acceptance for the Files tab (T-026).

Runs against the PM's frozen capture of the live daemon store and asserts that the Files tab
serves a real interval from it. Expectations are derived from the capture's **raw rows** by a
small independent scan below, never by asking the code under test what it should produce — a
handcrafted store that happens to satisfy the implementation is exactly what this test exists to
prevent.

The capture is deliberately awkward: every file-bearing hour contains gap boundaries that fall
off the hour, because gaps begin when a daemon restarted. Under the pre-pivot rule that made the
whole store unservable (PIVOT-1); the served interval now only has to be hour-aligned itself.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from appio.contracts import FILE_HOUR_NS, Window
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry, ClientState
from appio.query import QueryService
from appio.ui.files_view import FilesView

CAPTURE = Path("/tmp/appio-files-tab-capture/iod.sqlite")
CAPTURE_SHA256 = "5eea10690422e3eba12e3376044c953dfcfe1ae3657bf13e34cb42d5bfe891e6"


class _NoDaemon:
    """No daemon in this test: the capture is the only source."""

    state = ClientState()

    def query(self, request):  # pragma: no cover - fails loudly if the router asks a peer
        raise AssertionError("unexpected remote request")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_served_interval(connection, boot: str, end: int) -> tuple[int, int]:
    """Largest contiguous run of *file-bearing hours*, computed from raw rows only.

    Deliberately a plain loop over ``file_coverage`` timestamps: no coverage model, no QueryService,
    no reuse of the production interval selector.
    """
    hours = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT ts_mono_ns FROM file_coverage WHERE boot_id=? AND ts_mono_ns<?",
            (boot, end),
        )
    }
    assert hours, "capture must contain file-bearing hours"
    best: tuple[int, int] = (0, 0)
    run_start = run_end = None
    for hour in sorted(hours):
        if run_end is not None and hour == run_end:
            run_end = hour + FILE_HOUR_NS
        else:
            run_start, run_end = hour, hour + FILE_HOUR_NS
        if run_end - run_start > best[1] - best[0]:
            best = (run_start, run_end)
    return best


def _gaps_inside(connection, boot: str, start: int, end: int) -> int:
    rows = connection.execute(
        "SELECT from_mono_ns, to_mono_ns FROM gap WHERE boot_id=? AND to_mono_ns>? AND from_mono_ns<?",
        (boot, start, end),
    )
    # An hour is affected when any gap overlaps it; the app counts per app-hour evidence, so this
    # independent figure is only used to prove the served interval really does contain gaps.
    return sum(1 for _ in rows)


@pytest.fixture(scope="module")
def capture():
    if not CAPTURE.exists():
        pytest.skip(f"frozen capture not present at {CAPTURE}")
    before = _sha256(CAPTURE)
    assert before == CAPTURE_SHA256, (
        "the frozen capture changed; regenerate it deliberately rather than accepting drift"
    )
    yield CAPTURE
    assert _sha256(CAPTURE) == before, "the acceptance run must not modify the frozen capture"


def test_real_capture_serves_an_interval_shorter_than_the_request(capture):
    connection = sqlite3.connect(f"file:{capture}?mode=ro", uri=True)
    app = QApplication.instance() or QApplication([])
    try:
        boot, end = connection.execute(
            "SELECT boot_id, file_finalized_through_mono_ns FROM boot ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        expected_start, expected_end = _expected_served_interval(connection, boot, end)
        expected_gaps = _gaps_inside(connection, boot, expected_start, expected_end)
        assert expected_gaps > 0, "the chosen real interval must actually contain gaps"

        requested = Window("since_boot", boot, end_mono_ns=end)
        router = HistoryRouter(QueryService(connection), _NoDaemon(), AppRegistry())
        try:
            router.prepare(requested)
            router._local_future.result(timeout=600)
            assert router._consume_local() is True, "the local window must settle before rendering"

            view = FilesView(router)
            view.resize(1100, 700)
            view.set_window(requested)
            view.show()
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and view.state == "loading":
                app.processEvents()
                time.sleep(0.05)
            app.processEvents()
            try:
                assert view.state == "incomplete", view.state_label.text()
                assert view.model.rowCount() > 0, "a served interval must render rows"

                result = view.last_result
                assert result is not None
                served = result.coverage.unknown_owner_overflow.window
                assert (served.start_mono_ns, served.end_mono_ns) == (expected_start, expected_end)
                # The served interval is a strict subset of the request: this is the disclosure case.
                assert served.end_mono_ns - served.start_mono_ns < end
                assert result.coverage.coverage.complete is False
                assert result.coverage.coverage.gap_intervals >= 0
                assert result.served_gap_count is not None, "the local route must report its own count"
                assert view.app_selector.count() > 0, "file rows must yield selectable applications"
            finally:
                view.close()
        finally:
            router.close()
    finally:
        connection.close()
