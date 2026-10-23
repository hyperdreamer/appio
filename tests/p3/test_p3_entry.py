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

import pytest

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


def test_interactive_composition_construction_is_deferred_to_the_factory(tmp_path):
    """`run_interactive` imports lazily and owns its Qt objects only when called."""
    from appio.ui import collector_window

    assert callable(collector_window.run_interactive)
    source = Path(collector_window.__file__).read_text(encoding="utf-8")
    body = source[source.index("def run_interactive"):]
    assert "LiveCollector(" in body
    # The collector is built by a callable the coordinator invokes, not eagerly.
    assert "local_factory" in body
