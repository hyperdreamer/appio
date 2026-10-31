"""Contract-shaped Files results and literal canonical scope oracle."""
from dataclasses import replace

from tests.p4.files_tab_support import R, S
from tests.p4.test_files_view import APPS, FakeRouter, build, coverage, file_view, unknown_owner

SCOPE_A = ("served interval only: T0=2h 0m 0s through T1=4h 0m 0s after boot; "
           "requested window: T0=0s through T1=6h 0m 0s after boot; "
           "this application's served-hour gap evidence in served interval: 1; "
           "this application's requested-window coverage scalar: 4")


def result(*, requested=R, served=S, gap=1, requested_gaps=4,
           complete=False, rows=(), search=None, observed=(7, 0), tier="raw"):
    inner = coverage(complete=complete, gap_intervals=requested_gaps, tier=tier)
    unknown = replace(unknown_owner(cov=inner), window=served)
    return file_view(rows, window=requested, search=search, cov=inner,
                     unknown_owner_overflow=unknown, served_gap_count=gap,
                     observed_bytes_r=observed[0], observed_bytes_w=observed[1])


def show(qtbot, value):
    view = build(qtbot, FakeRouter(result=value, ranking_rows=APPS))
    view.set_window(value.table.window)
    qtbot.waitUntil(lambda: view.state != "loading", timeout=1000)
    return view
