"""Every terminal footer independently owns the same canonical disclosure."""
from dataclasses import replace

import pytest

from appio.contracts import HookHits
from appio.ui import files_view
from tests.p4.files_tab_support import S
from tests.p4.files_tab_view_cases import SCOPE_A, result, show
from tests.p4.test_files_view import file_row

FOOTERS = ("footer_state", "footer_observed", "footer_counted", "footer_uncounted",
           "footer_unknown_owner", "footer_skipped", "footer_weak", "footer_hooks")


@pytest.mark.parametrize("footer", FOOTERS)
@pytest.mark.parametrize("gap", [1, 0, None])
@pytest.mark.parametrize("outcome", ["rows", "no_matches", "empty_partial"])
def test_each_coverage_footer_contains_canonical_scope_and_reducer_labels(qtbot, footer, gap, outcome):
    value = result(gap=gap, rows=(file_row(),) if outcome == "rows" else (),
                   search="absent" if outcome == "no_matches" else None)
    view = show(qtbot, value)
    scope = SCOPE_A.replace("in served interval: 1;", "in served interval: " +
                            ("unavailable" if gap is None else str(gap)) + ";")
    text = getattr(view, footer).text()
    assert scope in text, (footer, text)
    assert text.count(scope) == 1, (footer, text)
    assert "this application's served-hour gap evidence in served interval:" in text
    assert "this application's requested-window coverage scalar: 4" in text
    assert "whole window" not in text
    assert "collection has gaps in the requested window" not in text
    assert "same gaps" not in text
    prefix = {"rows": "rows retained; ", "no_matches": "no retained file matches 'absent'; ",
              "empty_partial": "no retained file activity in the served interval; coverage: incomplete; "}[outcome]
    assert view.footer_state.text().startswith(prefix)
    assert view.state_label.text() == ""


@pytest.mark.parametrize("footer", FOOTERS)
@pytest.mark.parametrize("gap", [0, None])
def test_complete_empty_footer_has_full_scope_and_keeps_unknown_explicit(qtbot, footer, gap):
    value = result(requested=S, served=S, complete=True, requested_gaps=0,
                   gap=gap, observed=(0, 0))
    view = show(qtbot, value)
    expected_gap = "unavailable" if gap is None else "0"
    scope = ("served interval: T0=2h 0m 0s through T1=4h 0m 0s after boot; "
             "requested window: T0=2h 0m 0s through T1=4h 0m 0s after boot; "
             f"this application's served-hour gap evidence in served interval: {expected_gap}; "
             "this application's requested-window coverage scalar: 0")
    text = getattr(view, footer).text()
    assert scope in text, (footer, text)
    assert text.count(scope) == 1
    assert "served interval only" not in text
    assert view.footer_state.text() == (
        f"no retained file activity for this application in the served interval; {scope}; coverage: complete")
    assert view.state_label.text() == ""


@pytest.mark.parametrize("kind", ["typed_refusal", "untyped_refusal", "placeholder"])
def test_refusal_replaces_every_old_row_and_numeric_footer(qtbot, kind):
    value = result(rows=(file_row(),), observed=(123, 456))
    view = show(qtbot, value)
    assert view.model.rowCount() == 1
    if kind == "placeholder":
        unknown = replace(value.coverage.unknown_owner_overflow,
                          coverage=replace(value.coverage.coverage, unavailable_reason="not_retained"))
        view._publish(replace(value, coverage=replace(value.coverage, unknown_owner_overflow=unknown)))
        expected = "file history unavailable: not_retained: file history is not available for this window"
    else:
        reason = "not_retained" if kind == "typed_refusal" else None
        view._set_unavailable(reason, "retained source gone")
        expected = ("file history unavailable: not_retained: retained source gone" if reason
                    else "file history unavailable: retained source gone")
    assert view.state_label.text() == expected
    assert view.state == "unavailable"
    assert view.model.rowCount() == 0
    assert view.last_result is None
    for name in FOOTERS:
        assert getattr(view, name).text() == "unavailable", name


def test_one_scope_is_computed_once_from_accepted_result(qtbot, monkeypatch):
    value = result(rows=(file_row(),))
    calls = []
    original = files_view._scope_text

    def tracked(coverage, count):
        calls.append((coverage, count))
        return original(coverage, count)

    monkeypatch.setattr(files_view, "_scope_text", tracked)
    view = show(qtbot, value)
    assert len(calls) == 1
    assert calls[0][0] is value.coverage
    assert calls[0][1] == 1
    assert all(SCOPE_A in getattr(view, name).text() for name in FOOTERS)


@pytest.mark.parametrize("footer,prefix", [
    ("footer_counted", "counted omissions (top-100/quota): 3 files, 4 KiB read, 512 B written"),
    ("footer_uncounted", "uncounted overflow (BPF map full, not measured): 5 files, 60 KiB read, 64 KiB written"),
    ("footer_unknown_owner", "unknown owner overflow (unassigned, never added to an app): 2 files, 8 KiB read, 4 KiB written"),
    ("footer_skipped", "skipped objects (excluded, never counted as file I/O): 11 non-file, 2 unsupported filesystem"),
    ("footer_weak", "weak identity rows (dev+inode only, no filesystem UUID): 13"),
    ("footer_hooks", "hook hits: vfs_read 7 · vfs_write 5 · vfs_readv 0 (never fired) · vfs_writev 0 (never fired) · vfs_iter_read 1 · vfs_iter_write 0 (never fired)"),
])
def test_scope_does_not_merge_or_replace_counter_values(qtbot, footer, prefix):
    value = result(rows=(file_row(),))
    unknown = replace(value.coverage.unknown_owner_overflow,
                      overflow_files=2, overflow_bytes_r=8192, overflow_bytes_w=4096)
    cov = replace(value.coverage, counted_omitted_files=3,
                  counted_omitted_bytes_r=4096, counted_omitted_bytes_w=512,
                  uncounted_overflow_files=5, uncounted_overflow_bytes_r=61440,
                  uncounted_overflow_bytes_w=65536, unknown_owner_overflow=unknown,
                  skipped_nonfile=11, skipped_unsupported_fs=2, weak_identity_rows=13,
                  hook_hits=HookHits(7, 5, 0, 0, 1, 0))
    view = show(qtbot, replace(value, coverage=cov))
    assert getattr(view, footer).text() == f"{prefix}; {SCOPE_A}"
