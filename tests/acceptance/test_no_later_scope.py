"""Guard the P1/P2 acceptance path against dependencies on later-plan features."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from PyQt6.QtWidgets import QAbstractButton, QMenu, QSystemTrayIcon, QTabWidget, QWidget

from test_p1_p2 import collected, fixture_io, show_chromium


def test_window_has_no_later_plan_widgets_actions_or_flows(collected, qtbot):
    window = show_chromium(qtbot, collected)
    assert window.findChildren(QSystemTrayIcon) == []
    # The approved one detail host: two tabs, Overview and Files.
    tabs = window.findChildren(QTabWidget)
    assert len(tabs) == 1
    assert tabs[0].objectName() == "detail_tabs"
    assert tabs[0].count() == 2
    assert [tabs[0].tabText(index) for index in range(tabs[0].count())] == [
        "Overview", "Files"
    ]
    # Exactly one real Files surface, and it lives in the Files tab.
    surfaces = [widget for widget in window.findChildren(QWidget)
                if widget.objectName() == "files_view"]
    assert len(surfaces) == 1
    files_tab = tabs[0].widget(1)
    assert files_tab is surfaces[0] or files_tab.isAncestorOf(surfaces[0])
    assert window.findChildren(QMenu) == []
    forbidden = ("daemon", "socket", "bpf", "polkit", "privilege", "setup", "tray")
    for widget in window.findChildren(QWidget):
        assert not any(word in widget.objectName().lower() for word in forbidden), widget.objectName()
    for control in window.findChildren(QAbstractButton):
        assert not any(word in control.text().lower() for word in forbidden), control.text()
    assert [action.text() for action in window.actions()] == []


def test_fresh_process_imports_and_executes_only_p1_p2_modules(tmp_path):
    """A fresh interpreter makes the import guard effective even if other tests imported more modules."""
    script = r'''
import builtins
import importlib.abc
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, sys.argv[1])
forbidden = ("daemon", "iod", "socket", "bpf", "polkit", "privilege", "setup", "tray", "packaging")
seen = set()
def check(name):
    if name.startswith("appio."):
        assert not any(part.startswith(forbidden) for part in name.split(".")[1:]), name
        seen.add(name)
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        check(fullname)
sys.meta_path.insert(0, Guard())
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    check(name)
    caller = (globals or {}).get("__name__", "")
    if caller == "appio" or caller.startswith("appio."):
        assert name.split(".")[0] not in {"socket", "bcc", "bpf", "polkit", "dbus", "pydbus"}, (caller, name)
    if name == "appio":
        for child in fromlist or ():
            check(name + "." + child)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import

from test_p1_p2 import fixture_io, collect_fixture, CHROMIUM, BOOT, SINCE_BOOT
from appio.ui.main_window import AppIoWindow
from PyQt6.QtWidgets import QApplication
app = QApplication([])
patch = pytest.MonkeyPatch()
try:
    root = Path(sys.argv[2])
    fixture = fixture_io.__wrapped__(root, patch)
    with collect_fixture(fixture, root / "isolated.sqlite") as result:
        assert result.service.disk_share(result.app_id(CHROMIUM), SINCE_BOOT).task_total_bytes == 300
        window = AppIoWindow(result.service, BOOT, SINCE_BOOT.end_mono_ns)
        window.show()
        app.processEvents()
        window.close()
finally:
    patch.undo()
loaded = sorted(name for name in sys.modules if name.startswith("appio."))
for name in loaded:
    check(name)
print(json.dumps({"loaded": loaded, "attempted": sorted(seen)}))
'''
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(Path(__file__).parents[2] / "src"))
    completed = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).parent), str(tmp_path)],
        env=env, text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    loaded = json.loads(completed.stdout.splitlines()[-1])["loaded"]
    assert {"appio.procs", "appio.cgroups", "appio.sampler", "appio.store", "appio.query",
            "appio.ui.main_window"} <= set(loaded)
