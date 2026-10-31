"""Real-store acceptance for the Files tab (T-024/T-026).

The Files tab is only correct if it serves a real interval from a store that existed *before* the
code under test. That cannot be synthesised, so this test runs against the frozen capture in
``tests/p4/acceptance/`` (tool, manifest, oracle output and report all committed beside it).

Two modes, and the difference is the point:

* default suite — if the capture is absent this test **skips**, and a skip is visibly not a pass;
* acceptance run — set ``APPIO_ACCEPTANCE_REQUIRED=1`` and an absent or drifted capture is a
  **failure**. A missing input can never be mistaken for a green acceptance.

Expectations come from ``oracle-out.json``, produced by ``oracle.py`` scanning the capture's raw
rows with the standard library only. Nothing here asks ``appio`` what its own answer should be.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from appio.contracts import Window
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry, ClientState
from appio.query import QueryService
from appio.ui.files_view import FilesView

HERE = Path(__file__).with_name("acceptance")
CAPTURE_DIR = Path(os.environ.get("APPIO_ACCEPTANCE_CAPTURE", "/tmp/appio-files-tab-capture"))
REQUIRED = os.environ.get("APPIO_ACCEPTANCE_REQUIRED") == "1"
ORACLE = json.loads((HERE / "oracle-out.json").read_text())


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


def _require_capture() -> Path:
    snapshot = CAPTURE_DIR / "iod.sqlite"
    if not snapshot.exists():
        message = (
            f"frozen capture missing at {snapshot}; reproduce it with "
            f"`python tests/p4/acceptance/capture_store.py capture <live store> {CAPTURE_DIR}`"
        )
        if REQUIRED:
            pytest.fail(message)  # acceptance run: absent input is a failure, never a skip
        pytest.skip(message + " (set APPIO_ACCEPTANCE_REQUIRED=1 to make this a failure)")
    actual = _sha256(snapshot)
    if actual != ORACLE["capture_sha256"]:
        pytest.fail(f"capture digest drift: {actual} != {ORACLE['capture_sha256']}")
    return snapshot


def test_real_capture_serves_the_interval_the_raw_rows_imply():
    snapshot = _require_capture()
    before = _sha256(snapshot)
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    app = QApplication.instance() or QApplication([])
    try:
        boot = ORACLE["boot_id"]
        watermark = ORACLE["file_finalized_through_mono_ns"]
        expected = tuple(ORACLE["expected_served_interval_ns"])
        requested = Window("since_boot", boot, end_mono_ns=watermark)

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
                # The interval comes from the raw rows, not from the implementation.
                assert (served.start_mono_ns, served.end_mono_ns) == expected
                # Served is strictly narrower than requested: this is the disclosure case.
                assert served.end_mono_ns - served.start_mono_ns < (watermark or 0)
                assert result.coverage.coverage.complete is False
                assert result.served_gap_count is not None, "the local route must report its own count"
                assert ORACLE["gaps_inside_expected_served_interval"] > 0
                assert view.app_selector.count() == ORACLE["apps_with_rows_in_served_interval"]
            finally:
                view.close()
        finally:
            router.close()
    finally:
        connection.close()
        assert _sha256(snapshot) == before, "the acceptance run must not modify the frozen capture"
