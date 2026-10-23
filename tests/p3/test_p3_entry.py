"""The interactive entry is lazy; ``--once`` stays on the P1/P2 import boundary.

A fresh interpreter is used for the import guard so modules imported by other
tests cannot hide a regression. The legacy path must never import a P3 module,
and the interactive path must not construct a local collector before the
coordinator's factory decides who owns collection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from appio.collector_owner import ServiceFacts
from appio.iodclient import ClientState

SRC = Path(__file__).parents[2] / "src"

FORBIDDEN_ONCE = (
    "daemon", "iod", "socket", "bpf", "polkit", "privilege", "setup", "tray", "packaging",
)


def _run(script: str, *argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, "-c", script, *argv], env=env, text=True,
                          capture_output=True, timeout=timeout)


def test_once_never_imports_a_p3_module_in_a_fresh_interpreter(tmp_path):
    database = tmp_path / "once.db"
    script = r'''
import json, sys
from appio.__main__ import main
code = main(["--once", "--db", sys.argv[1], "--proc-root", sys.argv[2],
             "--cgroup-root", sys.argv[3]])
loaded = sorted(name for name in sys.modules if name.startswith("appio."))
print(json.dumps({"code": code, "loaded": loaded}))
'''
    proc_root = Path(__file__).parents[1] / "fixtures" / "proc"
    cgroup_root = Path(__file__).parents[1] / "fixtures" / "cgroup" / "normal-t0"
    completed = _run(script, str(database), str(proc_root), str(cgroup_root))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout.splitlines()[-1])
    assert report["code"] == 0
    loaded = report["loaded"]
    assert "appio.live" in loaded and "appio.store" in loaded
    for name in loaded:
        parts = name.split(".")[1:]
        assert not any(part.startswith(FORBIDDEN_ONCE) for part in parts), name
    assert database.exists(), "the legacy once path still records one sample"


def test_once_returns_the_legacy_exit_code_and_closes_the_collector(tmp_path, monkeypatch):
    from appio import __main__ as entry

    class Collector:
        instances: list["Collector"] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.events: list[str] = []
            Collector.instances.append(self)

        def tick(self):
            self.events.append("tick")

        def close(self):
            self.events.append("close")

    monkeypatch.setattr(entry, "LiveCollector", Collector)
    args = entry._parser().parse_args(["--once", "--db", str(tmp_path / "x.db")])
    assert entry.run_legacy_once(args) == 0
    assert [collector.events for collector in Collector.instances] == [["tick", "close"]]
    assert Collector.instances[0].kwargs["database_path"] == tmp_path / "x.db"


def test_run_legacy_once_does_not_import_the_p3_modules():
    from appio import __main__ as entry

    assert not hasattr(entry, "run_interactive")
    source = Path(entry.__file__).read_text(encoding="utf-8")
    # The P3 window is imported inside the interactive branch only.
    assert "from appio.ui.collector_window import run_interactive" in source
    assert source.index("if args.once:") < source.index(
        "from appio.ui.collector_window import run_interactive")


def test_main_branches_before_constructing_any_collector(tmp_path, monkeypatch):
    from appio import __main__ as entry

    calls: list[str] = []

    def fake_once(args):
        calls.append("once")
        return 7

    def fake_interactive(args):
        calls.append("interactive")
        return 9

    monkeypatch.setattr(entry, "run_legacy_once", fake_once)
    monkeypatch.setitem(sys.modules, "appio.ui.collector_window",
                        type(sys)("appio.ui.collector_window"))
    sys.modules["appio.ui.collector_window"].run_interactive = fake_interactive
    try:
        assert entry.main(["--once", "--db", str(tmp_path / "x.db")]) == 7
        assert entry.main(["--db", str(tmp_path / "x.db")]) == 9
    finally:
        sys.modules.pop("appio.ui.collector_window", None)
    assert calls == ["once", "interactive"]


def test_a_negative_interval_is_still_rejected_before_any_branch():
    from appio import __main__ as entry

    with pytest.raises(SystemExit) as raised:
        entry.main(["--once", "--interval", "-1"])
    assert raised.value.code == 2


def test_offscreen_is_applied_before_the_interactive_branch(tmp_path, monkeypatch):
    from appio import __main__ as entry

    seen: list[str | None] = []

    def fake_interactive(args):
        seen.append(os.environ.get("QT_QPA_PLATFORM"))
        return 0

    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setitem(sys.modules, "appio.ui.collector_window",
                        type(sys)("appio.ui.collector_window"))
    sys.modules["appio.ui.collector_window"].run_interactive = fake_interactive
    try:
        assert entry.main(["--offscreen", "--db", str(tmp_path / "x.db")]) == 0
    finally:
        sys.modules.pop("appio.ui.collector_window", None)
    assert seen == ["offscreen"]


def test_interactive_composition_forwards_a_pending_handover_to_the_factory(tmp_path, monkeypatch, qapp):
    """The shipped entry must use the coordinator's incoming handover seam."""
    from PyQt6.QtWidgets import QApplication
    from appio.ui import collector_window

    class Signal:
        def emit(self):
            pass

        def connect(self, callback):
            pass

    class Client:
        def __init__(self, *args, **kwargs):
            self.state = ClientState()

        def close(self):
            pass

        def pump(self):
            pass

    class Collector:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.source = kwargs["source"]
            self.baseline_only = kwargs["baseline_only"]
            self.last_frame = object()
            self.handover_fallback_reason = None

        def quiesce(self):
            return None

        def close(self):
            pass

    calls = []
    holder = {}
    offer = object()

    class Window:
        def __init__(self, coordinator, history, setup, **kwargs):
            holder["coordinator"] = coordinator
            holder["history"] = history
            coordinator.resume_handover = offer
            coordinator.reconcile(ServiceFacts(False, "inactive", False, False, False))

        def show(self):
            pass

        def refresh(self):
            pass

        def shutdown(self):
            holder["coordinator"].close()
            holder["history"].close()

    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    monkeypatch.setattr("appio.iodclient.IodClient", Client)
    monkeypatch.setattr(collector_window, "LiveCollector", Collector)
    monkeypatch.setattr(collector_window, "CollectorWindow", Window)
    monkeypatch.setattr(collector_window, "_wake_object", lambda: SimpleNamespace(woke=Signal()))
    monkeypatch.setattr(collector_window, "_gui_lease_path", lambda: tmp_path / "runtime" / "collector.lock")
    (tmp_path / "runtime").mkdir()

    assert collector_window.run_interactive(SimpleNamespace(
        db=tmp_path / "gui.db", proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cgroup",
        interval=5.0)) == 0
    assert len(calls) == 1
    assert calls[0]["baseline_only"] is False
    assert calls[0]["resume_handover"] is offer


def test_interactive_composition_construction_is_deferred_to_the_factory(tmp_path):
    """`run_interactive` imports lazily and owns its Qt objects only when called."""
    from appio.ui import collector_window

    assert callable(collector_window.run_interactive)
    source = Path(collector_window.__file__).read_text(encoding="utf-8")
    body = source[source.index("def run_interactive"):]
    assert "LiveCollector(" in body
    # The collector is built by a callable the coordinator invokes, not eagerly.
    assert "local_factory" in body
