"""Remote Files results disclose the served bounds but never served-hour evidence.

Protocol 1 transports ``FileTable`` and ``FileCoverage`` only. The hourly
``file_coverage`` rows that define ``gaps_in_served_interval`` never cross the
route, so a remote result must carry ``served_gap_count = None`` even when the
response looks complete, even when the requested-window scalar is nonzero, and
even when the local store measured a genuine ``0`` in the same snapshot. None is
never converted to zero, never copied from the requested scalar, and never added
to the request or reply wire shape.
"""
from dataclasses import fields, replace

from appio.contracts import FILE_HOUR_NS as H, Window
from appio.history_router import FileViewResult
from appio.ipc_models import QueryRequest
from appio.query import QueryService
from tests.p4.test_file_history_router import (
    Remote, WINDOW, id_for, inject_requested_gap_evidence, retained,
)

SERVED_KEYS = ("served_interval", "served_gap_count", "gaps_in_served_interval")
# Only hour 0 is finalized, so this range request is shortened to the served hour.
SHORTENED_REQUEST = Window("range", "boot-A", 0, 2 * H)
SHORTENED_SERVED = Window("range", "boot-A", 0, H)


def file_view(remote, *, window=WINDOW, search="CAFÉ"):
    """Request one Files view and return it with the requests it actually sent."""
    opened = len(remote.requests)
    future = remote.router.request_file_view(
        id_for(remote.router), window, "bytes_total", "desc", search)
    return remote.finish(future), remote.requests[opened:]


def test_remote_real_codec_pages_never_invent_served_gap_evidence(retained, tmp_path):
    remote = Remote(tmp_path, retained)
    try:
        result, sent = file_view(remote)

        # A complete-looking answer with a zero requested scalar must not become 0.
        assert result.coverage.coverage.gap_intervals == 0
        assert type(result.coverage.coverage.gap_intervals) is int
        assert result.coverage.coverage.complete is True
        assert result.served_gap_count is None
        assert type(result.served_gap_count) is not int

        # Bounds are disclosed through the existing compatibility carrier, while
        # the requested window stays on both transported records.
        assert result.coverage.served_interval == result.table.unknown_owner_overflow.window
        assert result.table.window == result.coverage.window == WINDOW

        # Real HMAC pagination: both pages, the opaque continuation, one coverage stage.
        assert [request.kind for request in sent] == ["files", "files", "coverage"]
        pages = [request for request in sent if request.kind == "files"]
        assert pages[0].cursor is None and pages[1].cursor
        assert all((request.sort, request.direction, request.search) ==
                   ("bytes_total", "desc", "CAFÉ") for request in pages)
        assert len(result.table.rows) == 2

        # Unknown never collides with a zero measurement in comparison.
        assert result.served_gap_count != result.coverage.coverage.gap_intervals
        assert replace(result, served_gap_count=0) != result
    finally:
        remote.close()


def test_remote_requested_scalar_never_becomes_served_hour_evidence(retained, tmp_path):
    inject_requested_gap_evidence(retained.db)
    remote = Remote(tmp_path, retained)
    try:
        result, sent = file_view(remote)

        # The requested-window coverage scalar remains available and nonzero.
        assert result.coverage.coverage.gap_intervals == 1
        assert type(result.coverage.coverage.gap_intervals) is int
        assert result.coverage.coverage.complete is False
        # It must not fill the served slot: protocol 1 has no hourly app rows.
        assert result.served_gap_count is None
        assert result.served_gap_count != result.coverage.coverage.gap_intervals
        assert [request.kind for request in sent] == ["files", "files", "coverage"]
        assert len(result.table.rows) == 2
    finally:
        remote.close()


def test_remote_shortened_served_scope_is_disclosed_without_hourly_gap_evidence(
        retained, tmp_path):
    remote = Remote(tmp_path, retained, window=SHORTENED_REQUEST)
    try:
        result, sent = file_view(remote, window=SHORTENED_REQUEST)

        # The request was [0h,2h) but only hour 0 is finalized; the honest answer
        # keeps the request on the table/coverage and discloses the served hour.
        assert result.table.window == result.coverage.window == SHORTENED_REQUEST
        assert result.coverage.served_interval == SHORTENED_SERVED
        assert result.coverage.served_interval != result.coverage.window
        assert result.coverage.coverage.gap_intervals == 0
        assert type(result.coverage.coverage.gap_intervals) is int
        assert result.served_gap_count is None
        assert [request.kind for request in sent] == ["files", "files", "coverage"]
        assert len(result.table.rows) == 2
    finally:
        remote.close()


def test_served_hour_evidence_never_enters_the_remote_wire_shape(retained, tmp_path):
    remote = Remote(tmp_path, retained)
    try:
        result, _ = file_view(remote)

        # The field exists only on the in-process result type.
        assert "served_gap_count" in {field.name for field in fields(FileViewResult)}
        assert result.served_gap_count is None
        assert not set(SERVED_KEYS) & {field.name for field in fields(QueryRequest)}

        files = [reply for reply in remote.replies if reply.get("kind") == "files"]
        coverage = [reply for reply in remote.replies if reply.get("kind") == "coverage"]
        assert len(files) == 2 and len(coverage) == 1
        for reply in files:
            assert set(reply["payload"][0]) == {"value", "snapshot", "next_cursor"}
            assert not any(key in repr(reply["payload"][0]) for key in SERVED_KEYS)
        for reply in coverage:
            assert set(reply["payload"]) == {"value", "snapshot"}
            assert not any(key in repr(reply["payload"]) for key in SERVED_KEYS)
    finally:
        remote.close()


def test_remote_unknown_is_not_a_local_measured_zero(retained, tmp_path):
    measured = QueryService(retained.db).file_serve_facts(1, WINDOW)
    assert measured.gaps_in_served_interval == 0
    assert type(measured.gaps_in_served_interval) is int

    remote = Remote(tmp_path, retained)
    try:
        result, _ = file_view(remote)

        # The same snapshot and window prove a local zero; the remote route still
        # cannot prove the reducer, so its answer stays explicitly unknown.
        assert result.served_gap_count is None
        assert result.served_gap_count != measured.gaps_in_served_interval
        assert (result.served_gap_count is None) != (measured.gaps_in_served_interval is None)
        assert replace(result, served_gap_count=measured.gaps_in_served_interval) != result
    finally:
        remote.close()
