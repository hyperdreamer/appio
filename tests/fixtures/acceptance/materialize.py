"""Materialize only this directory's static spec §10.1–10.2 fixture trees.

Run explicitly with Python; tests only copy these checked-in files. No appio
imports, host process reads, permissions changes, or privileged setup are used.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent
BOOT = "11111111-2222-4333-8444-555555555555\n"
BASE = "/user.slice/user-1000.slice/user@1000.service/app.slice/"
CHROMIUM = BASE + "app-chromium@fixture.service"
REUSE = BASE + "app-pidreuse.scope"
SHARED = BASE + "app-shared.scope"
STATUS = "Name: fixture\nUid: 1000 1000 1000 1000\nGid: 1000 1000 1000 1000\nTracerPid: 0\n"
ZERO = (0, 0, 0, 0)
NORMAL_IO = {
    410: (ZERO, (1000, 500, 100, 70), (1600, 650, 170, 90)),
    411: (ZERO, (500, 100, 50, 10), None),
    412: (ZERO, (300, 200, 30, 10), (300, 200, 30, 10)),
    413: (ZERO, ZERO, ZERO),  # Read failure is metadata, not a magic IO string.
    500: (ZERO, (0, 0, 25, 0), ZERO),
}


def put(path, content):
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def process(root, pid, comm, ppid, start, cgroup, exe, counters):
    leaf = root / str(pid)
    put(leaf / "stat", f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {start} 0\n")
    put(leaf / "status", STATUS)
    put(leaf / "cgroup", f"0::{cgroup}\n")
    rc, wc, rb, wb = counters
    put(leaf / "io", f"rchar: {rc}\nwchar: {wc}\nsyscr: 0\nsyscw: 0\nread_bytes: {rb}\nwrite_bytes: {wb}\ncancelled_write_bytes: 0\n")
    link = ROOT / leaf / "exe"
    if link.is_symlink():
        assert str(link.readlink()) == exe
    else:
        link.symlink_to(exe)


def materialize():
    for index in range(3):
        proc = Path("proc") / f"t{index}"
        put(proc / "sys/kernel/random/boot_id", BOOT)
        for pid in (410, 411, 412, 413, 500):
            counters = NORMAL_IO[pid][index]
            if counters is None:
                continue
            comm = {410: "app", 411: "child", 412: "worker", 413: "blocked", 500: "reused"}[pid]
            start = {410: 100, 411: 101, 412: 102, 413: 103, 500: 900 if index == 2 else 700}[pid]
            exe = ("/usr/bin/fixture-new" if index == 2 else "/usr/bin/fixture-old") if pid == 500 else "/usr/lib/chromium/chromium"
            process(proc, pid, comm, 410 if pid == 411 else 1, start,
                    REUSE if pid == 500 else CHROMIUM, exe, counters)
        for variant in ("normal", "not-delegated"):
            root = Path("cgroup")
            if variant != "normal":
                root /= variant
            root /= f"t{index}"
            delegated = variant == "normal"
            put(root / "cgroup.controllers", "cpu io memory pids\n" if delegated else "cpu memory pids\n")
            chrome = root / CHROMIUM.lstrip("/")
            put(chrome / "cgroup.procs", "410\n411\n412\n413\n" if index < 2 else "410\n412\n413\n")
            reuse = root / REUSE.lstrip("/")
            put(reuse / "cgroup.procs", "500\n")
            if delegated:
                put(chrome / "io.stat", (
                    "259:0 rbytes=1000 wbytes=200 rios=10 wios=3\n259:1 rbytes=300 wbytes=500 rios=4 wios=5\n"
                    if index < 2 else
                    "259:0 rbytes=1120 wbytes=270 rios=14 wios=7\n259:1 rbytes=350 wbytes=550 rios=8 wios=7\n"))
                put(reuse / "io.stat", "259:0 rbytes=0 wbytes=0 rios=0 wios=0\n")
            else:
                assert not (ROOT / chrome / "io.stat").exists()
                assert not (ROOT / reuse / "io.stat").exists()

    for variant, path in (("shared-unit", SHARED), ("shared-distinct", "/fixture-shared")):
        for index in range(2):
            proc = Path("proc") / variant / f"t{index}"
            put(proc / "sys/kernel/random/boot_id", BOOT)
            for pid in (420, 421):
                # The spec leaves shared-case T unspecified; zero T isolates A19.
                exe = "/usr/bin/shared" if variant == "shared-unit" else f"/usr/bin/tool-{'a' if pid == 420 else 'b'}"
                process(proc, pid, "shared", 1, pid, path, exe, ZERO)
            root = Path("cgroup") / variant / f"t{index}"
            put(root / "cgroup.controllers", "cpu io memory pids\n")
            leaf = root / path.lstrip("/")
            put(leaf / "cgroup.procs", "420\n421\n")
            put(leaf / "io.stat", "259:0 rbytes=0 wbytes=0 rios=0 wios=0\n" if index == 0
                else "259:0 rbytes=40 wbytes=5 rios=1 wios=1\n")

    metadata = {
        "boot_id": BOOT.strip(), "source": "gui", "epoch": 1, "sequences": [0, 1, 2],
        "observed_mono_ns": [100_000_000_000, 110_000_000_000, 120_000_000_000],
        "wall_offset_ns": 1_000_000_000_000, "clk_tck": 100,
        "dumpability": {"413": False},
        "eacces_io": [f"proc/t{index}/413/io" for index in range(3)],
        "app_metadata": {"name": "fixture", "icon_path": None, "scope": "user"},
        "cgroup_ids": {CHROMIUM: 101, REUSE: 102, SHARED: 103, "/fixture-shared": 104},
    }
    put(Path("metadata.json"), json.dumps(metadata, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    materialize()
