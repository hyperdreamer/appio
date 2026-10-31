"""Real-store acceptance for the Files tab.

Two independent proofs, both required:

1. **Reproducible from HEAD** — ``tests/p4/acceptance/files_tab_pivot_case.sqlite.gz`` is a committed
   slice derived from the frozen capture (see ``derive_fixture.py`` and ``fixture.json``). It cannot be
   skipped: if the fixture is missing or drifted, this test fails.
2. **Ground truth** — when the frozen capture is present (or ``APPIO_ACCEPTANCE_REQUIRED=1``), the same
   assertions run against the real store. In required mode an absent or drifted capture is a failure,
   never a skip.

The package itself is checked too: ``capture.json`` and ``oracle.py`` must exist, the manifest digest
must agree with the oracle output, and re-running ``oracle.py`` must reproduce ``oracle-out.json``
byte-for-byte. Deleting a package file therefore breaks the acceptance instead of silently passing it.

Expectations come from the oracle, which scans raw rows with the standard library only and never
imports ``appio`` — nothing here asks the implementation what its own answer should be.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication, QLabel

from appio.contracts import Window
from appio.history_router import HistoryRouter
from appio.iodclient import AppRegistry, ClientState
from appio.query import QueryService
from appio.ui.files_view import FilesView

HERE = Path(__file__).with_name("acceptance")
FIXTURE = HERE / "files_tab_pivot_case.sqlite.gz"
FIXTURE_META = json.loads((HERE / "fixture.json").read_text())
ORACLE_OUT = json.loads((HERE / "oracle-out.json").read_text())
CAPTURE_DIR = Path(os.environ.get("APPIO_ACCEPTANCE_CAPTURE", "/tmp/appio-files-tab-capture"))
REQUIRED = os.environ.get("APPIO_ACCEPTANCE_REQUIRED") == "1"


class _NoDaemon:
    state = ClientState()

    def query(self, request):  # pragma: no cover
        raise AssertionError("unexpected remote request")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _materialise_fixture() -> Path:
    """Decompress the committed fixture, failing loudly if it is absent or drifted."""
    if not FIXTURE.exists():
        pytest.fail(
            f"committed fixture missing at {FIXTURE}; acceptance must be reproducible from HEAD, so this "
            "is a failure and not a skip"
        )
    archive = FIXTURE.read_bytes()
    # Pin the compressed artifact too, not just its payload: a corrupted recorded digest, or an
    # archive whose gzip header changed while the payload stayed identical, must not pass.
    archive_digest = hashlib.sha256(archive).hexdigest()
    if archive_digest != FIXTURE_META["gz_sha256"]:
        pytest.fail(f"fixture archive digest drift: {archive_digest} != {FIXTURE_META['gz_sha256']}")
    payload = gzip.decompress(archive)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != FIXTURE_META["uncompressed_sha256"]:
        pytest.fail(f"fixture digest drift: {actual} != {FIXTURE_META['uncompressed_sha256']}")
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    handle.write(payload)
    handle.close()
    return Path(handle.name)


def _capture_path() -> Path | None:
    snapshot = CAPTURE_DIR / "iod.sqlite"
    if not snapshot.exists():
        if REQUIRED:
            pytest.fail(
                f"frozen capture missing at {snapshot}; reproduce it with "
                f"`python tests/p4/acceptance/capture_store.py capture <live store> {CAPTURE_DIR}`"
            )
        return None
    if _sha256(snapshot) != ORACLE_OUT["capture_sha256"]:
        pytest.fail("frozen capture digest drift; regenerate it deliberately rather than accepting drift")
    return snapshot


def _assert_served(snapshot: Path, expected: dict, *, expected_apps: int, check_rows: bool) -> None:
    before = _sha256(snapshot)
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    app = QApplication.instance() or QApplication([])
    try:
        requested = Window("since_boot", expected["boot_id"], end_mono_ns=expected["file_finalized_through_mono_ns"])
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
                result = view.last_result
                assert result is not None
                served = result.coverage.unknown_owner_overflow.window
                assert (served.start_mono_ns, served.end_mono_ns) == tuple(expected["expected_served_interval_ns"])
                assert result.coverage.coverage.complete is False
                assert expected["gaps_inside_expected_served_interval"] > 0
                assert result.served_gap_count is not None, "the local route must report its own count"
                assert result.served_gap_count > 0, "the served interval contains known gaps"
                if check_rows:
                    assert view.model.rowCount() > 0
                    assert view.app_selector.count() == expected_apps
                # The disclosure must name both scopes, or the reader cannot tell what was served.
                text = " ".join(label.text() for label in view.findChildren(QLabel) if label.text().strip())
                assert "served interval only" in text
                assert "requested window" in text
            finally:
                view.close()
        finally:
            router.close()
    finally:
        connection.close()
        assert _sha256(snapshot) == before, "acceptance must not modify its input"


def test_committed_fixture_is_a_complete_acceptance_package():
    """The package must be self-consistent, or acceptance cannot be re-checked from HEAD."""
    for name in ("capture_store.py", "capture.json", "oracle.py", "oracle-out.json", "derive_fixture.py", "REPORT.md"):
        assert (HERE / name).exists(), f"acceptance package file missing: {name}"
    manifest = json.loads((HERE / "capture.json").read_text())
    assert manifest["snapshot"]["sha256"] == ORACLE_OUT["capture_sha256"], (
        "the manifest and the oracle must describe the same capture"
    )
    assert FIXTURE_META["derived_from_capture_sha256"] == ORACLE_OUT["capture_sha256"]
    # Re-running the oracle must reproduce its committed output byte for byte. The live capture is
    # optional here: this test checks what is committed, not what this host happens to have, so it
    # must not inherit the required-capture failure semantics.
    snapshot = (CAPTURE_DIR / "iod.sqlite") if (CAPTURE_DIR / "iod.sqlite").exists() else None
    if snapshot is not None:
        produced = subprocess.run(
            [sys.executable, str(HERE / "oracle.py"), str(HERE / "capture.json"), str(snapshot)],
            capture_output=True, text=True, check=True,
        ).stdout
        assert json.loads(produced) == ORACLE_OUT, "oracle output drifted from the committed evidence"


def test_committed_fixture_serves_the_interval_the_raw_rows_imply():
    """Reproducible from HEAD: no capture, no environment, no skip."""
    snapshot = _materialise_fixture()
    expected = {
        "boot_id": ORACLE_OUT["boot_id"],
        "file_finalized_through_mono_ns": ORACLE_OUT["file_finalized_through_mono_ns"],
        "expected_served_interval_ns": ORACLE_OUT["expected_served_interval_ns"],
        "gaps_inside_expected_served_interval": ORACLE_OUT["gaps_inside_expected_served_interval"],
    }
    try:
        _assert_served(snapshot, expected, expected_apps=FIXTURE_META["expected"]["apps_in_selector"], check_rows=True)
    finally:
        snapshot.unlink(missing_ok=True)


def test_real_capture_serves_the_same_interval_when_available():
    """Ground truth: the same assertions against the frozen live store."""
    snapshot = _capture_path()
    if snapshot is None:
        pytest.skip("frozen capture not present on this host (set APPIO_ACCEPTANCE_REQUIRED=1 to require it)")
    _assert_served(
        snapshot, ORACLE_OUT,
        expected_apps=ORACLE_OUT["apps_with_rows_in_served_interval"], check_rows=True,
    )
