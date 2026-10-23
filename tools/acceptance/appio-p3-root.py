#!/usr/bin/env python3
"""Opt-in P3 host acceptance. The default path only prints a plan.

Run from an expendable non-root login after following docs/acceptance/appio-p3-root.md.
The helper always receives exactly one verb; all flags here are script flags.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import shlex
import sqlite3
import stat
import subprocess
import sys
import time

HELPER = ("/usr/bin/pkexec", "/usr/bin/appio-setup")
SERVICE = "appio-iod.service"
DROPINS = (Path("/etc/systemd/system/user@.service.d/10-appio-io.conf"),
           Path("/etc/systemd/user.conf.d/10-appio.conf"))
PHASES = ("enable", "verify", "disable", "restart-user-manager")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def plan(phase):
    print("DRY RUN: no commands, imports of appio, file writes, elevation or session changes.")
    print("Prerequisite: install the reviewed runtime; service initially disabled (see runbook).")
    print("Sequence: enable -> manual reboot/fresh session -> verify -> disable.")
    for verb in ("install-delegation", "enable", "status", "export-history", "disable"):
        print("  " + shlex.join(HELPER + (verb,)))
    print("Activation choices: restart user manager now (ENDS THE SESSION), or activate after reboot.")
    print("Default choice: reboot later; this script never reboots and enable never restarts a manager.")
    print("Verify: fresh manager, actual 16 MiB block-device evidence within 5%, probe cleanup,")
    print("root socket 0750/0660, only iod authority, GUI closed >5 s, replay/history and final WAL export.")
    print("Disable retains delegation and root history; requires its own polkit authorization.")
    print("Destructive operation is separate and needs BOTH --run --allow-session-termination:")
    print("  " + shlex.join(HELPER + ("restart-user-manager",)))
    print(f"Selected phase: {phase}. Host checks: UNRUN.")


def command(argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    require(done.returncode == 0, f"{shlex.join(argv)}: exit {done.returncode}: {done.stderr[:4096]}")
    return done.stdout.strip()


def helper(verb, observe=None):
    require(verb in ("install-delegation", "enable", "status", "export-history", "disable", "restart-user-manager"), "unknown helper verb")
    print("Authorizing: " + shlex.join(HELPER + (verb,)), flush=True)
    if observe is None:
        output = command(HELPER + (verb,))
    else:
        # Bound every wait; observe the short-lived probe while the fixed helper
        # runs. Missing evidence fails instead of treating controller presence as proof.
        with subprocess.Popen(HELPER + (verb,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            deadline = time.monotonic() + 90
            while child.poll() is None:
                observe()
                if time.monotonic() >= deadline:
                    child.kill()
                    child.communicate()
                    raise RuntimeError("helper deadline expired; inspect host state before retrying")
                time.sleep(0.001)
            output, error = child.communicate()
            require(child.returncode == 0, f"helper exit {child.returncode}: {error[:4096]}")
    body = json.loads(output)
    require(body.get("version") == 1 and body.get("verb") == verb and body.get("uid") == os.getuid(), "untrusted helper result")
    require(not body.get("error"), f"helper error: {body.get('error')}")
    return body


def unit(name=SERVICE):
    output = command(("/usr/bin/systemctl", "show", name, "--property=UnitFileState,ActiveState,MainPID"))
    return dict(line.split("=", 1) for line in output.splitlines())


def manager():
    pid = int(unit(f"user@{os.getuid()}.service")["MainPID"])
    require(pid > 0, "no live user manager")
    # stat comm may contain spaces and parentheses; field 22 follows the final ')'.
    started = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    return {"boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "pid": pid, "start": started}


def confirm(text):
    require(input(text + "\nType observed to record this manual check: ").strip() == "observed", "manual check not confirmed")
    return {"observation": text, "confirmed": True}


def exchange(kind="status", **fields):
    import socket
    from appio.ipc_models import validate_server_message
    from appio.ipc_protocol import FrameDecoder, MAX_OUTBOUND, encode_frame
    from appio.ipc_security import read_peer, require_root_server, verify_runtime_paths

    path = Path("/run/appio/iod.sock")
    verify_runtime_paths(path, appio_gid=grp.getgrnam("appio").gr_gid)
    frames, decoder = [], FrameDecoder(max_bytes=MAX_OUTBOUND)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        require_root_server(read_peer(client))
        client.sendall(encode_frame({"protocol": 1, "type": kind, "request_id": "host-acceptance", **fields}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chunk = client.recv(65536)
            require(chunk, "daemon closed before response")
            for message in decoder.feed(chunk):
                normalized = validate_server_message(message)
                # Preserve JSON cursor records in evidence, not dataclass internals.
                frames.append(message)
                if normalized["type"] == "error":
                    raise RuntimeError(f"daemon rejected {kind}: {message}")
                if (kind == "subscribe" and normalized["type"] == "snapshot") or (kind != "subscribe" and normalized["type"] in {"status", "result"}):
                    return frames
    raise RuntimeError("daemon response deadline expired")


def rows(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        require(db.execute("PRAGMA integrity_check").fetchone() == ("ok",), "export integrity check failed")
        result = []
        for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name"):
            columns = db.execute(f'PRAGMA table_info("{name}")').fetchall()
            keys = [column[1] for column in sorted(columns, key=lambda column: column[5]) if column[5]]
            order = ",".join(f'"{key}"' for key in keys or [column[1] for column in columns])
            result.append((name, tuple(db.execute(f'SELECT * FROM "{name}" ORDER BY {order}'))))
        return tuple(result)


def exported(body):
    path = Path(body["export_path"])
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    require(path.is_relative_to(account_home), "export is outside the account home")
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, "export must be caller-owned regular 0600")
    data = rows(path)
    require({name for name, _ in data} >= {"sample", "sample_dev", "sample_coverage", "collector_epoch", "sampler_checkpoint", "handover", "rollup_app"}, "incomplete export schema")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        cp = db.execute("SELECT boot_id,epoch,seq,checkpoint_mono_ns FROM sampler_checkpoint WHERE source='iod'").fetchone()
    cutoff = body["export_cutoff"]
    require(cp == tuple(cutoff[key] for key in ("boot_id", "iod_epoch", "seq", "cutoff_mono_ns")), "export checkpoint/cutoff mismatch")
    return path, data


def enable(evidence):
    initial = unit()
    require(initial["UnitFileState"] == "disabled" and initial["ActiveState"] == "inactive", "initial service must be disabled and inactive")
    evidence["initial"] = initial
    evidence["fallback"] = confirm("Open two GUIs on the same local database: banner is off/absent; one writer, second viewer. Close both.")
    before = manager()
    old_groups = set(command(("/usr/bin/id", "-G", pwd.getpwuid(os.getuid()).pw_name)).split())
    evidence["install"] = helper("install-delegation")
    new_groups = set(command(("/usr/bin/id", "-G", pwd.getpwuid(os.getuid()).pw_name)).split())
    require(old_groups <= new_groups and str(grp.getgrnam("appio").gr_gid) in new_groups, "group append lost membership or appio absent")
    evidence["groups_before"], evidence["groups_after"] = sorted(old_groups), sorted(new_groups)
    evidence["enable"] = helper("enable")
    require(evidence["enable"]["enabled"] is True and evidence["enable"]["answering"] is True, "daemon did not enable/answer")
    evidence["activation_choice"] = "activate after reboot (no reboot command executed)"
    require(manager() == before, "user manager changed during the non-destructive enable")
    evidence["manager_before_activation"] = before
    evidence["dropins"] = {str(path): path.read_text() for path in DROPINS}
    print("Reboot later yourself, start a fresh login, then run --run --phase verify. Pending is not accounting proof.")


def verify(evidence):
    require("manager_before_activation" in evidence, "run enable first")
    current = manager()
    require(current != evidence["manager_before_activation"], "activation requires a fresh user manager, not a logout into a lingering manager")
    require(grp.getgrnam("appio").gr_gid in {os.getgid(), *os.getgroups()}, "current session lacks appio; use a fresh login")
    evidence["manager_after_activation"] = current
    uid = os.getuid()
    target = Path(f"/var/lib/appio/probes/{uid}/probe.bin")
    leaf_root = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")
    observed, units, devices = {}, set(), set()
    def probe_paths():
        return [*leaf_root.glob("appio-probe-*.service/io.stat"),
                *leaf_root.glob("*/appio-probe-*.service/io.stat")]
    prior_units = {path.parent for path in probe_paths()}
    # The helper status envelope does not carry ProbeEvidence. Observe the real
    # transient unit directly and save it. Never fabricate evidence if missed.
    def observe():
        try:
            info = target.stat()
            devices.add(f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}")
        except FileNotFoundError:
            pass
        for path in probe_paths():
            if path.parent in prior_units:
                continue
            units.add(path.parent)
            try:
                for line in path.read_text().splitlines():
                    dev, *fields = line.split()
                    values = dict(item.split("=", 1) for item in fields)
                    observed[dev] = max(observed.get(dev, 0), int(values.get("wbytes", "0")))
            except FileNotFoundError:
                pass
    evidence["status"] = helper("status", observe)
    expected = 16 * 1024 * 1024
    evidence["probe"] = {"target": str(target), "expected_bytes": expected, "observed_wbytes": observed, "devices": sorted(devices), "units": sorted(map(str, units))}
    require(len(devices) == 1 and units, "probe observation missed; rerun verify, do not claim host accounting")
    dev = next(iter(devices))
    require(dev in observed and abs(observed[dev] - expected) * 100 <= expected * 5, "actual device write is not within 5% of 16 MiB")
    require(not target.exists() and not any(path.exists() for path in units), "probe file/unit cleanup incomplete")
    evidence["probe"]["filesystem"] = command(("/usr/bin/findmnt", "-n", "-o", "FSTYPE,SOURCE,MAJ:MIN", "-T", str(target.parent)))
    from appio.activation import SUPPORTED_PROBE_FILESYSTEMS
    mount = evidence["probe"]["filesystem"].split()
    require(len(mount) == 3 and mount[0] in SUPPORTED_PROBE_FILESYSTEMS and mount[2] == dev, "D2 target is not a verified supported block device")
    require(evidence["status"]["pending_activation"] is False, "probe did not clear pending")
    require(exchange()[-1]["answering"] is True, "verified socket is not answering")
    evidence["authority"] = confirm("Open GUI: on, answering; only iod advances while authoritative. Enable is a D3 fresh baseline with visible partial coverage, not a clean upload. Close ALL GUIs now.")
    first = exchange("subscribe", since=None)[-1]
    require(first["source"] == "iod", "wrong collector source")
    time.sleep(6.2)
    since = {key: first[key] for key in ("boot_id", "iod_epoch", "seq")}
    replay = exchange("subscribe", since=since)
    last = replay[-1]
    require(last["source"] == "iod" and last["ts_mono_ns"] > first["ts_mono_ns"], "daemon did not advance independently of GUI")
    query = {"kind": "ranking", "window": {"kind": "range", "boot_id": first["boot_id"], "start_mono_ns": first["ts_mono_ns"], "end_mono_ns": last["ts_mono_ns"] + 1}}
    history = [exchange("query", query=query)[-1]]
    deadline = time.monotonic() + 30
    while history[-1]["payload"][0].get("next_cursor"):
        require(time.monotonic() < deadline, "historical pagination did not terminate")
        query["cursor"] = history[-1]["payload"][0]["next_cursor"]
        history.append(exchange("query", query=query)[-1])
    evidence["closed_gui_history"] = {"first": first, "replay_or_gap": replay, "history": history}
    evidence["reopened"] = confirm("Reopen GUI and query that closed interval: historical data remains available; gaps are visible and unavailable is not zero. Close GUI before disable.")


def disable(evidence):
    require("closed_gui_history" in evidence, "run verify before disable")
    before = exchange()[-1]["cursor"]
    body = helper("disable")
    evidence["disable"] = body
    state = unit()
    require(state["UnitFileState"] == "disabled" and state["ActiveState"] == "inactive", "daemon is not disabled/inactive")
    path, data = exported(body)
    cutoff = body["export_cutoff"]
    require((cutoff["boot_id"], cutoff["iod_epoch"]) == (before["boot_id"], before["iod_epoch"]) and cutoff["seq"] >= before["seq"], "final WAL state predates last observed commit")
    again = helper("export-history")
    second, second_data = exported(again)
    require(path != second and data == second_data, "retained root history differs from final complete export")
    require({str(path): path.read_text() for path in DROPINS} == evidence["dropins"], "disable changed delegation")
    require(str(grp.getgrnam("appio").gr_gid) in command(("/usr/bin/id", "-G", pwd.getpwuid(os.getuid()).pw_name)).split(), "disable removed appio membership")
    evidence["retained_export"] = again
    evidence["fallback_after_disable"] = confirm("Reopen GUI after inactive proof: one lease holder resumes local collection; actual transition gap/partial coverage remains visible. Root history and both user exports remain preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="perform the selected host phase")
    parser.add_argument("--phase", choices=PHASES, default="enable")
    parser.add_argument("--allow-session-termination", action="store_true", help="explicit consent for the separate restart phase; ENDS THIS SESSION")
    args = parser.parse_args()
    if args.phase == "restart-user-manager" and args.run and not args.allow_session_termination:
        parser.error("restart-user-manager ENDS THE SESSION; requires --allow-session-termination")
    if args.allow_session_termination and args.phase != "restart-user-manager":
        parser.error("--allow-session-termination applies only to the separate restart-user-manager phase")
    if not args.run:
        plan(args.phase)
        return 0
    require(os.getuid() == os.geteuid() != 0, "run as the non-root beneficiary, never with sudo")
    require(sys.version_info >= (3, 14), "installed runtime requires Python >=3.14")
    account = pwd.getpwuid(os.getuid())
    location = Path(account.pw_dir) / ".local/state/appio/p3-acceptance.json"
    evidence = json.loads(location.read_text()) if location.exists() and args.phase != "enable" else {"uid": os.getuid(), "host_checks": {}}
    require(evidence["uid"] == os.getuid(), "evidence belongs to another UID")
    location.parent.mkdir(parents=True, exist_ok=True)
    def save():
        fd = os.open(location, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(evidence, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    if args.phase == "restart-user-manager":
        require("enable" in evidence, "enable first; destructive restart is an explicit activation choice")
        evidence["host_checks"][args.phase] = "requested: session will end; verify after fresh login"
        save()
        print("THIS ENDS YOUR SESSION. Explicit destructive flag received.", flush=True)
        helper("restart-user-manager")
    else:
        try:
            {"enable": enable, "verify": verify, "disable": disable}[args.phase](evidence)
            evidence["host_checks"][args.phase] = "passed (manual observations separately recorded)"
        except Exception as error:
            evidence["host_checks"][args.phase] = f"incomplete: {error}"
            raise
        finally:
            save()
    print(f"Host evidence: {location}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Acceptance incomplete: {error}", file=sys.stderr)
        raise SystemExit(1)
