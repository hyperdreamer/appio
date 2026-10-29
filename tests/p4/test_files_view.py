"""Offscreen proof that the Files surface renders only what the contract says.

Every test drives :class:`appio.ui.files_view.FilesView` through a fake router
that implements the shipped Files query contract
(``ranking(window, "total_bytes")``, ``file_source_status()`` and
``request_file_view(app_id, window, sort, direction, search)``). No test opens a
database, loads BPF, reads ``/proc`` or touches ``/sys``.

Falsifiers (one named test each):

* a row renders a reconstructed path instead of the captured basename;
* the footer adds counted omissions to uncounted overflow as if both were
  measured;
* an unavailable source renders numeric zeroes;
* sorting a column drops or reorders rows locally instead of using the query;
* ``last_seen`` is not sortable, or is sorted by its display text.
"""

from __future__ import annotations

from concurrent.futures import Future
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

# Pin PyQt6 before pytest-qt builds its qapp fixture (see tests/test_ui.py).
from pytestqt.qt_compat import qt_api

if qt_api.pytest_qt_api != "pyqt6":
    qt_api.set_qt_api("pyqt6")

from PyQt6.QtCore import QAbstractTableModel, Qt
from PyQt6.QtWidgets import QHeaderView, QLineEdit, QTableView, QTableWidget

from appio.contracts import (
    AppTotal,
    Coverage,
    FileCoverage,
    FileRow,
    FileTable,
    HookHits,
    UnknownOwnerOverflow,
    Window,
    WindowUnavailableError,
)
from appio.file_runtime import FileAvailability
from appio.history_router import FileViewResult
from appio.ui.formatting import UNAVAILABLE, format_bytes

BOOT = "11111111-2222-4333-8444-555555555555"
END = 7_200_000_000_000
WINDOW = Window(kind="since_boot", boot_id=BOOT, end_mono_ns=END)
NEXT_WINDOW = Window(kind="since_boot", boot_id=BOOT, end_mono_ns=2 * END)

HOOKS = (
    "vfs_read",
    "vfs_write",
    "vfs_readv",
    "vfs_writev",
    "vfs_iter_read",
    "vfs_iter_write",
)

HEADERS = ("FILE", "DISK", "READS", "WRITES", "BYTES R", "BYTES W", "LAST")
COLUMN_SORTS = (
    "basename",
    "dev",
    "reads",
    "writes",
    "bytes_r",
    "bytes_w",
    "last_seen",
)


# --------------------------------------------------------------------------- #
# Contract-shaped fixtures
# --------------------------------------------------------------------------- #


def coverage(**overrides: object) -> Coverage:
    values: dict[str, object] = {
        "tier": "raw",
        "complete": True,
        "unavailable_reason": None,
        "inaccessible_pids": 0,
        "resets": 0,
        "reap_corrections": 0,
        "reap_unapplied": (0, 0, 0, 0),
        "reap_unobserved": (0, 0, 0, 0),
        "partial_intervals": 0,
        "gap_intervals": 0,
        "overlap_uncertain_intervals": 0,
        "spans_suspend_intervals": 0,
        "spans_suspend": False,
        "reap_incomplete": False,
        "counter_overflow": False,
        "device_state": "available",
        "counted_omitted_files": 0,
        "counted_omitted_bytes_r": 0,
        "counted_omitted_bytes_w": 0,
        "uncounted_overflow_files": 0,
        "uncounted_overflow_bytes_r": 0,
        "uncounted_overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
        "weak_identity_rows": 0,
        "hook_hits": HookHits(0, 0, 0, 0, 0, 0),
    }
    values.update(overrides)
    return Coverage(**values)  # type: ignore[arg-type]


def hooks(**overrides: int) -> HookHits:
    values = dict.fromkeys(HOOKS, 0)
    values.update(overrides)
    return HookHits(**values)  # type: ignore[arg-type]


def unknown_owner(
    files: int = 0, bytes_r: int = 0, bytes_w: int = 0, cov: Coverage | None = None
) -> UnknownOwnerOverflow:
    return UnknownOwnerOverflow(
        WINDOW, "overflow_owner_unknown", files, bytes_r, bytes_w, cov or coverage()
    )


def file_row(**overrides: object) -> FileRow:
    values: dict[str, object] = {
        "path_id": 1,
        "basename": "report.txt",
        "state": "resolved",
        "identity_weak": False,
        "dev": "259:0",
        "inode": 42,
        "generation": 7,
        "reads": 3,
        "writes": 2,
        "bytes_r": 100,
        "bytes_w": 50,
        "bytes_total": 150,
        "last_seen_boot_id": BOOT,
        "last_seen_mono_ns": 3_600_000_000_000,
    }
    values.update(overrides)
    if "bytes_total" not in overrides:
        values["bytes_total"] = values["bytes_r"] + values["bytes_w"]  # type: ignore[operator]
    return FileRow(**values)  # type: ignore[arg-type]


def file_coverage(rows: tuple[FileRow, ...] = (), **overrides: object) -> FileCoverage:
    values: dict[str, object] = {
        "app_id": 1,
        "window": WINDOW,
        "counted_omitted_files": 0,
        "counted_omitted_bytes_r": 0,
        "counted_omitted_bytes_w": 0,
        "uncounted_overflow_files": 0,
        "uncounted_overflow_bytes_r": 0,
        "uncounted_overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
        "weak_identity_rows": 0,
        "hook_hits": hooks(),
        "observed_bytes_r": sum(row.bytes_r for row in rows),
        "observed_bytes_w": sum(row.bytes_w for row in rows),
        "unknown_owner_overflow": unknown_owner(),
        "coverage": coverage(),
    }
    values.update(overrides)
    return FileCoverage(**values)  # type: ignore[arg-type]


def file_view(
    rows: tuple[FileRow, ...] = (),
    *,
    window: Window = WINDOW,
    sort: str = "bytes_total",
    direction: str = "desc",
    search: str | None = None,
    cov: Coverage | None = None,
    **overrides: object,
) -> FileViewResult:
    table_coverage = cov or coverage()
    values: dict[str, object] = {
        "counted_omitted_files": 0,
        "counted_omitted_bytes_r": 0,
        "counted_omitted_bytes_w": 0,
        "uncounted_overflow_files": 0,
        "uncounted_overflow_bytes_r": 0,
        "uncounted_overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
        "weak_identity_rows": 0,
        "hook_hits": table_coverage.hook_hits,
        "observed_bytes_r": sum(row.bytes_r for row in rows),
        "observed_bytes_w": sum(row.bytes_w for row in rows),
        "unknown_owner_overflow": unknown_owner(cov=table_coverage),
        "coverage": table_coverage,
    }
    values.update(overrides)
    result_coverage = FileCoverage(
        app_id=1, window=window, **values  # type: ignore[arg-type]
    )
    table = FileTable(
        app_id=1,
        window=window,
        rows=rows,
        sort=sort,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        search=search,
        unknown_owner_overflow=result_coverage.unknown_owner_overflow,
        coverage=table_coverage,
    )
    return FileViewResult(table, result_coverage)


def app_total(app_id: int, name: str, total: int) -> AppTotal:
    read = total // 2
    write = total - read
    return AppTotal(app_id, f"unit:{name}.service", name, read, write, total, read,
                    write, None, None, None, coverage())


class FakeRouter:
    """The shipped Files query surface, with request bookkeeping."""

    def __init__(
        self,
        *,
        result: FileViewResult | None = None,
        error: BaseException | None = None,
        make=None,
        status: FileAvailability | None = None,
        ranking_rows: tuple[AppTotal, ...] = (),
        ranking_error: BaseException | None = None,
        defer: bool = False,
    ) -> None:
        self.result = result
        self.error = error
        self.make = make
        self.status = status if status is not None else FileAvailability(True, True)
        self.ranking_rows = ranking_rows
        self.ranking_error = ranking_error
        self.defer = defer
        self.ranking_calls: list[tuple[Window, str]] = []
        self.requests: list[tuple[object, ...]] = []
        self.pending: list[Future] = []

    def ranking(self, window: Window, metric: str):
        self.ranking_calls.append((window, metric))
        if self.ranking_error is not None:
            raise self.ranking_error
        return list(self.ranking_rows)

    def file_source_status(self) -> FileAvailability:
        return self.status

    def file_source_diagnostic(self) -> str | None:
        return self.status.diagnostic

    def request_file_view(self, app_id: int, window: Window, sort: str,
                          direction: str, search: str | None) -> Future:
        self.requests.append((app_id, window, sort, direction, search))
        future: Future = Future()
        self.pending.append(future)
        if not self.defer:
            self.complete(future, app_id, window, sort, direction, search)
        return future

    def complete(self, future: Future, app_id: int, window: Window, sort: str,
                 direction: str, search: str | None) -> None:
        if self.error is not None:
            future.set_exception(self.error)
            return
        if callable(self.make):
            future.set_result(self.make(app_id, window, sort, direction, search))
            return
        future.set_result(self.result if self.result is not None else file_view())


APPS = (app_total(1, "alpha", 300), app_total(2, "beta", 100))


def build(qtbot, router: FakeRouter):
    from appio.ui.files_view import FilesView

    view = FilesView(router)
    qtbot.addWidget(view)
    view.show()
    return view


def texts(view) -> list[list[str]]:
    model = view.model
    return [
        [model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)
         for column in range(model.columnCount())]
        for row in range(model.rowCount())
    ]


def footer_text(view) -> str:
    return " ".join(
        label.text() for label in (
            view.footer_state,
            view.footer_observed,
            view.footer_counted,
            view.footer_uncounted,
            view.footer_unknown_owner,
            view.footer_skipped,
            view.footer_weak,
            view.footer_hooks,
        )
    )


# --------------------------------------------------------------------------- #
# Headers and columns
# --------------------------------------------------------------------------- #


def test_headers_are_the_seven_ratified_columns_in_order(qtbot):
    view = build(qtbot, FakeRouter(ranking_rows=APPS))
    view.set_window(WINDOW)
    assert view.model.columnCount() == len(HEADERS)
    assert tuple(
        view.model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        for column in range(len(HEADERS))
    ) == HEADERS
    # A table widget would own its items instead of a model; the projection
    # requires the model-backed view.
    assert isinstance(view.table, QTableView)
    assert not isinstance(view.table, QTableWidget)
    assert isinstance(view.model, QAbstractTableModel)
    assert view.findChildren(QTableWidget) == []


# --------------------------------------------------------------------------- #
# Sorting: every column, through the contract, including last_seen
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("column", "sort"), list(enumerate(COLUMN_SORTS)))
def test_every_column_requests_its_contract_sort_key(qtbot, column, sort):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    router.requests.clear()
    view.table.horizontalHeader().sectionClicked.emit(column)
    assert router.requests[-1][2] == sort
    # A fresh column starts descending; last_seen is a time, not a name.
    assert router.requests[-1][3] == "desc"


def test_last_seen_is_sortable_and_clicks_toggle_direction(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    view.table.horizontalHeader().sectionClicked.emit(COLUMN_SORTS.index("last_seen"))
    assert router.requests[-1][2] == "last_seen"
    view.table.horizontalHeader().sectionClicked.emit(COLUMN_SORTS.index("last_seen"))
    assert router.requests[-1][2:4] == ("last_seen", "asc")
    view.direction_button.click()
    assert router.requests[-1][2:4] == ("last_seen", "desc")


def test_every_contract_sort_value_is_reachable_including_bytes_total(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    view.set_sort("bytes_total", "asc")
    assert router.requests[-1][2:4] == ("bytes_total", "asc")
    for sort in COLUMN_SORTS:
        view.set_sort(sort, "desc")
        assert router.requests[-1][2] == sort


def test_sorting_keeps_the_server_row_order_and_never_drops_rows(qtbot):
    # Reads 5, 1, 9 is not any local ordering of the sort key, so a view that
    # sorted its own rows would reorder or filter them here.
    rows = (
        file_row(path_id=1, basename="five.txt", reads=5),
        file_row(path_id=2, basename="one.txt", reads=1),
        file_row(path_id=3, basename="nine.txt", reads=9),
    )
    router = FakeRouter(ranking_rows=APPS, result=file_view(rows, sort="reads", direction="desc"))
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert [row[0] for row in texts(view)] == ["five.txt", "one.txt", "nine.txt"]
    router.requests.clear()
    view.table.horizontalHeader().sectionClicked.emit(COLUMN_SORTS.index("reads"))
    assert router.requests[-1][2] == "reads"
    assert [row[0] for row in texts(view)] == ["five.txt", "one.txt", "nine.txt"]


def test_last_seen_sorts_by_stored_value_not_by_display_text(qtbot):
    # "10s after boot" sorts before "9s after boot" as text but after it as a
    # monotonic value; the contract order must survive untouched.
    rows = (
        file_row(path_id=1, basename="nine.txt", last_seen_mono_ns=9_000_000_000),
        file_row(path_id=2, basename="ten.txt", last_seen_mono_ns=10_000_000_000),
    )
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view(rows, sort="last_seen", direction="asc"),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert [row[0] for row in texts(view)] == ["nine.txt", "ten.txt"]
    assert [row[6] for row in texts(view)] == [
        "9s after boot",
        "10s after boot",
    ]
    router.requests.clear()
    view.table.horizontalHeader().sectionClicked.emit(COLUMN_SORTS.index("last_seen"))
    assert router.requests[-1][2:4] == ("last_seen", "desc")
    assert [row[0] for row in texts(view)] == ["nine.txt", "ten.txt"]


# --------------------------------------------------------------------------- #
# Row rendering: basenames only, explicit name states, exact counters
# --------------------------------------------------------------------------- #


def test_rows_render_the_captured_basename_and_never_a_reconstructed_path(qtbot):
    row = file_row(basename="report.txt", dev="259:0", inode=42, generation=7)
    router = FakeRouter(ranking_rows=APPS, result=file_view((row,)))
    view = build(qtbot, router)
    view.set_window(WINDOW)
    (rendered,) = texts(view)
    assert rendered[0] == "report.txt"
    assert "/" not in rendered[0]
    assert "42" not in rendered[0] and "259:0" not in rendered[0]
    assert rendered[1] == "259:0"


def test_truncated_and_unresolved_names_carry_distinct_explicit_markers(qtbot):
    rows = (
        file_row(path_id=1, basename="long-name.txt", state="truncated"),
        file_row(path_id=2, basename="[unresolved]", state="unresolved"),
    )
    router = FakeRouter(ranking_rows=APPS, result=file_view(rows))
    view = build(qtbot, router)
    view.set_window(WINDOW)
    rendered = texts(view)
    assert "truncated" in rendered[0][0]
    assert "unresolved" in rendered[1][0]
    assert rendered[0][0] != rendered[1][0]
    for cell in (rendered[0][0], rendered[1][0]):
        assert "/" not in cell, "a marker never invents a path"


def test_each_numeric_column_renders_its_own_contract_field(qtbot):
    row = file_row(reads=7, writes=4, bytes_r=1024, bytes_w=1536)
    router = FakeRouter(ranking_rows=APPS, result=file_view((row,)))
    view = build(qtbot, router)
    view.set_window(WINDOW)
    (rendered,) = texts(view)
    assert rendered[2] == "7"
    assert rendered[3] == "4"
    assert rendered[4] == format_bytes(1024)
    assert rendered[5] == format_bytes(1536)
    assert rendered[6] == "1h 0m 0s after boot"


def test_a_search_filtered_table_does_not_tie_its_row_count_to_whole_window_bytes(qtbot):
    # One shown row, three rows' worth of contract-observed bytes: the footer
    # must label the byte total as the whole window, not as the shown row.
    rows = (file_row(basename="app.log"),)
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view(rows, search="log", observed_bytes_r=4096, observed_bytes_w=1024),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    text = view.footer_observed.text()
    assert "whole window" in text
    assert format_bytes(4096) in text and format_bytes(1024) in text
    assert "filtered to 1 row matching 'log'" in text
    assert view.model.rowCount() == 1


def test_weak_identity_rows_are_marked_in_the_row_and_counted_in_the_footer(qtbot):
    row = file_row(identity_weak=True)
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view((row,), cov=coverage(weak_identity_rows=1), weak_identity_rows=1),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    (rendered,) = texts(view)
    assert "weak identity" in rendered[1]
    assert "weak identity rows" in view.footer_weak.text()
    assert "1" in view.footer_weak.text()


def test_a_row_from_another_boot_names_that_boot_instead_of_comparing_clocks(qtbot):
    row = file_row(last_seen_boot_id="boot-previous", last_seen_mono_ns=90_000_000_000)
    router = FakeRouter(ranking_rows=APPS, result=file_view((row,)))
    view = build(qtbot, router)
    view.set_window(WINDOW)
    (rendered,) = texts(view)
    assert "boot-previous" in rendered[6]
    assert "1m 30s" in rendered[6]


# --------------------------------------------------------------------------- #
# App selection, search, and request cadence
# --------------------------------------------------------------------------- #


def test_app_selection_requests_the_selected_app_and_ranks_once_per_window(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert [call[0] for call in router.ranking_calls] == [WINDOW]
    assert router.requests[-1][0] == 1

    view.app_selector.setCurrentIndex(1)
    assert router.requests[-1][0] == 2
    view.set_app_id(1)
    assert router.requests[-1][0] == 1

    # A sort and a search re-query the file table; they never re-rank apps.
    before = len(router.ranking_calls)
    view.set_sort("writes", "asc")
    view.search_field.setText("log")
    assert len(router.ranking_calls) == before
    assert view.model.rowCount() == 0  # never one ranking query per rendered row


def test_search_sends_nfc_normalized_text_and_none_when_blank(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    view.search_field.setText("Cafe\u0301")
    assert router.requests[-1][4] == "Caf\u00e9"
    view.search_field.setText("   ")
    assert router.requests[-1][4] is None
    view.search_field.setText("")
    assert router.requests[-1][4] is None


def test_a_new_window_requeries_and_clears_the_previous_rows(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.model.rowCount() == 0
    assert view.last_result is not None
    router.requests.clear()
    view.set_window(NEXT_WINDOW)
    assert router.requests[-1][1] == NEXT_WINDOW
    assert view.model.rowCount() == 0
    assert view.state == "ready"
    assert view.last_result is not None


# --------------------------------------------------------------------------- #
# Loading and staleness
# --------------------------------------------------------------------------- #


def test_an_in_flight_request_is_explicitly_loading_and_a_stale_reply_is_ignored(qtbot):
    router = FakeRouter(ranking_rows=APPS, defer=True)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state == "loading"
    assert view.model.rowCount() == 0
    first = router.pending[0]
    view.set_sort("reads", "asc")
    second = router.pending[1]
    first.set_result(file_view((file_row(basename="stale.txt"),),
                               sort="bytes_total", direction="desc"))
    qtbot.waitUntil(lambda: view.state == "loading", timeout=1000)
    assert view.model.rowCount() == 0, "a retired generation must not publish"
    second.set_result(file_view((file_row(basename="fresh.txt"),), sort="reads", direction="asc"))
    qtbot.waitUntil(lambda: view.state == "ready", timeout=2000)
    assert [row[0] for row in texts(view)] == ["fresh.txt"]


# --------------------------------------------------------------------------- #
# Unavailable: reasons and no zeros
# --------------------------------------------------------------------------- #


def test_a_failed_future_renders_the_reason_and_no_numeric_zeroes(qtbot):
    router = FakeRouter(
        ranking_rows=APPS,
        error=WindowUnavailableError("file_source_unavailable", "File history could not be read."),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state == "unavailable"
    assert view.model.rowCount() == 0
    assert "file_source_unavailable" in view.state_label.text()
    assert "File history could not be read." in view.state_label.text()
    assert not any(character.isdigit() for character in footer_text(view)), footer_text(view)


def test_an_absent_file_capability_is_unavailable_before_any_request(qtbot):
    router = FakeRouter(
        ranking_rows=APPS,
        status=FileAvailability(False, False, "file_source_unavailable", "bpf load failed: EPERM"),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert router.requests == []
    # The unavailable source outranks "no applications observed": ranking is not
    # even consulted, so its answer cannot replace the source's own verdict.
    assert router.ranking_calls == []
    assert view.state == "unavailable"
    assert "bpf load failed: EPERM" in view.state_label.text()
    assert view.model.rowCount() == 0
    assert not any(character.isdigit() for character in footer_text(view))


def test_a_status_read_that_raises_is_an_explicit_unavailable_state(qtbot):
    class RaisingRouter(FakeRouter):
        def file_source_status(self):
            raise OSError("socket disappeared")

    router = RaisingRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state == "unavailable"
    assert "socket disappeared" in view.state_label.text()
    assert router.requests == []
    assert not any(character.isdigit() for character in footer_text(view))


def test_a_disabled_window_keeps_its_ratified_a22_reason_without_zeroes(qtbot):
    reason = "Launch window disabled: device and file activity are not stored per launch."
    router = FakeRouter(
        ranking_rows=APPS,
        error=WindowUnavailableError("launch_attribution", reason),
    )
    view = build(qtbot, router)
    view.set_window(Window(kind="launch", boot_id=BOOT, launch_id=1))
    assert view.state == "unavailable"
    assert reason in view.state_label.text()
    assert not any(character.isdigit() for character in footer_text(view))


def test_empty_rows_with_complete_zero_coverage_is_ready_not_unavailable(qtbot):
    router = FakeRouter(ranking_rows=APPS, result=file_view((), cov=coverage(complete=True)))
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state == "ready"
    assert view.model.rowCount() == 0
    assert "complete" in view.footer_state.text()
    assert "0 retained file rows" in view.footer_observed.text()


def test_no_observed_applications_is_stated_instead_of_an_empty_zero_table(qtbot):
    router = FakeRouter(ranking_rows=())
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert router.requests == []
    assert view.state == "ready"
    assert view.model.rowCount() == 0
    assert "no applications observed" in view.footer_state.text()


# --------------------------------------------------------------------------- #
# Coverage footer: counted, uncounted, unknown owner, skipped, hooks
# --------------------------------------------------------------------------- #


def test_incomplete_coverage_keeps_the_rows_and_says_incomplete(qtbot):
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view(
            (file_row(),),
            cov=coverage(complete=False, gap_intervals=2),
        ),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.state == "incomplete"
    assert view.model.rowCount() == 1
    assert "incomplete" in view.footer_state.text()
    assert "2" in view.footer_state.text(), "the gap count stays explicit"


def test_counted_omissions_and_uncounted_overflow_stay_separate_never_summed(qtbot):
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view(
            (file_row(),),
            cov=coverage(complete=False),
            counted_omitted_files=3,
            counted_omitted_bytes_r=4096,
            counted_omitted_bytes_w=512,
            uncounted_overflow_files=5,
            uncounted_overflow_bytes_r=61_440,
            uncounted_overflow_bytes_w=65_536,
        ),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    counted = view.footer_counted.text()
    uncounted = view.footer_uncounted.text()
    assert "3 files" in counted
    assert format_bytes(4096) in counted and format_bytes(512) in counted
    assert "5 files" in uncounted
    assert format_bytes(61_440) in uncounted and format_bytes(65_536) in uncounted
    # A footer that added them would have to print 8 files or a merged byte total.
    assert "8 files" not in counted + uncounted
    assert "not measured" in uncounted
    merged_bytes = format_bytes(61_440 + 65_536)
    assert merged_bytes not in footer_text(view)
    assert merged_bytes not in counted + uncounted


def test_unknown_owner_overflow_is_reported_separately_and_never_becomes_a_row(qtbot):
    overflow = unknown_owner(files=2, bytes_r=8192, bytes_w=4096)
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view((file_row(),), unknown_owner_overflow=overflow),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.model.rowCount() == 1
    text = view.footer_unknown_owner.text()
    assert "2 files" in text
    assert format_bytes(8192) in text and format_bytes(4096) in text
    assert "never added to an app" in text
    assert all("[unresolved]" != row[0] for row in texts(view))


def test_skipped_non_files_and_unsupported_filesystems_are_visible_separately(qtbot):
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view(
            (file_row(),),
            cov=coverage(complete=False),
            skipped_nonfile=11,
            skipped_unsupported_fs=2,
        ),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    text = view.footer_skipped.text()
    assert "11 non-file" in text
    assert "2 unsupported filesystem" in text
    assert "excluded" in text


def test_every_hook_counter_is_rendered_so_a_dead_hook_is_visible(qtbot):
    hits = hooks(vfs_read=7, vfs_write=5, vfs_iter_read=1)
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view((file_row(),), cov=coverage(hook_hits=hits), hook_hits=hits),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    text = view.footer_hooks.text()
    for name in HOOKS:
        assert name in text, name
    assert "vfs_read 7" in text
    assert "vfs_write 5" in text
    assert "vfs_iter_read 1" in text
    # A zero counter is labelled as never observed, never silently omitted.
    assert "vfs_readv 0 (never fired)" in text
    assert "vfs_writev 0 (never fired)" in text
    assert "vfs_iter_write 0 (never fired)" in text


def test_all_zero_hook_hits_still_report_that_no_hook_fired(qtbot):
    router = FakeRouter(ranking_rows=APPS, result=file_view((), cov=coverage()))
    view = build(qtbot, router)
    view.set_window(WINDOW)
    text = view.footer_hooks.text()
    assert "no hook has fired" in text
    assert "not proof of no file I/O" in text


def test_the_footer_states_that_file_activity_is_logical_and_never_subtracted(qtbot):
    # The design requires four footer facts; this is the one that keeps logical
    # VFS bytes from being mistaken for a correction to the task total.
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    note = view.footer_note.text()
    assert "basename the kernel captured" in note
    assert "never a reconstructed path" in note
    assert "logical VFS bytes" in note
    assert "never subtracted from the task-accounted total" in note
    assert "retained observations" in note


def test_the_sort_caption_always_names_the_active_contract_sort(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    # The default sort owns no column, so no header indicator can express it.
    assert view.sort_caption.text() == "sorted by traffic ▼ (bytes_total, desc)"
    view.set_sort("last_seen", "asc")
    assert view.sort_caption.text() == "sorted by LAST ▲ (last_seen, asc)"
    view.table.horizontalHeader().sectionClicked.emit(COLUMN_SORTS.index("basename"))
    assert view.sort_caption.text() == "sorted by FILE ▼ (basename, desc)"
    assert view.direction_button.text() == "▼"


# --------------------------------------------------------------------------- #
# Layout and controls
# --------------------------------------------------------------------------- #


def test_long_names_elide_instead_of_resizing_the_layout(qtbot):
    router = FakeRouter(
        ranking_rows=APPS,
        result=file_view((file_row(basename="a" * 300),)),
    )
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.table.textElideMode() == Qt.TextElideMode.ElideRight
    assert view.table.wordWrap() is False
    header = view.table.horizontalHeader()
    for column in range(len(HEADERS)):
        assert header.sectionResizeMode(column) != QHeaderView.ResizeMode.ResizeToContents
    view.resize(320, 240)
    qtbot.wait(10)
    # Fixed row height and a bounded minimum keep late text from growing the page.
    assert view.minimumWidth() <= 320


def test_direction_and_refresh_controls_are_labelled_and_never_silently_ambiguous(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert view.direction_button.toolTip()
    assert view.refresh_button.toolTip()
    assert view.direction_button.accessibleName()
    assert view.refresh_button.accessibleName()
    router.requests.clear()
    view.refresh_button.click()
    assert len(router.requests) == 1


def test_search_field_replaces_the_table_contents_without_touching_the_ranking(qtbot):
    router = FakeRouter(ranking_rows=APPS)
    view = build(qtbot, router)
    view.set_window(WINDOW)
    assert isinstance(view.search_field, QLineEdit)
    ranking_calls = len(router.ranking_calls)
    view.search_field.setText("report")
    assert router.requests[-1][4] == "report"
    assert len(router.ranking_calls) == ranking_calls


# --------------------------------------------------------------------------- #
# Import boundary: the view is a UI module, not a data path
# --------------------------------------------------------------------------- #


def test_files_view_imports_no_production_module_owned_by_another_layer():
    script = r"""
import json
import sys

import appio.ui.files_view  # noqa: F401

print(json.dumps(sorted(name for name in sys.modules if name.startswith("appio."))))
"""
    source = Path(__file__).parents[2] / "src"
    env = dict(
        os.environ,
        PYTHONPATH=str(source),
        QT_QPA_PLATFORM="offscreen",
        PYTHONDONTWRITEBYTECODE="1",
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    loaded = set(json.loads(completed.stdout.splitlines()[-1]))
    assert "appio.ui.files_view" in loaded
    forbidden = (
        "appio.iod",
        "appio.bpf_fileio",
        "appio.file_runtime",
        "appio.iodclient",
        "appio.ipc_models",
        "appio.setup_client",
        "appio.setup_service",
        "appio.setup_boundary",
        "appio.collector_owner",
    )
    assert not (set(forbidden) & loaded), sorted(set(forbidden) & loaded)
    assert UNAVAILABLE == "unavailable"
