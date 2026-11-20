#!/usr/bin/env python3
"""Opt-in P3 host acceptance. The default path only prints a plan.

Run from an expendable non-root login after following docs/acceptance/appio-p3-root.md.
The helper receives a fixed verb and, for set-sample-period, one seconds argument.
All flags here are script flags.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
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
import tempfile
import time

HELPER = ("/usr/bin/pkexec", "/usr/bin/appio-setup")
SERVICE = "appio-iod.service"
DROPINS = (Path("/etc/systemd/system/user@.service.d/10-appio-io.conf"),
           Path("/etc/systemd/user.conf.d/10-appio.conf"))
PHASES = ("enable", "verify", "period-runtime", "period-capacity", "disable", "restart-user-manager")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def plan(phase):
    print("DRY RUN: no commands, imports of appio, file writes, elevation or session changes.")
    print("Prerequisite: install the reviewed runtime; service initially disabled (see runbook).")
    print("Sequence: enable -> manual reboot/fresh session -> verify -> period-runtime -> disable.")
    print("period-runtime measures both periods and restores 10 s; period-capacity uses a disposable SQLite fixture.")
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


def helper(verb, *command_args, observe=None):
    require(verb in ("install-delegation", "enable", "status", "export-history", "disable", "restart-user-manager", "set-sample-period"), "unknown helper verb")
    if verb == "set-sample-period":
        require(len(command_args) == 1 and isinstance(command_args[0], str) and command_args[0] in tuple(str(n) for n in range(2, 31)), "invalid sample period")
    else:
        require(not command_args, "unexpected helper arguments")
    argv = HELPER + (verb,) + command_args
    print("Authorizing: " + shlex.join(argv), flush=True)
    if observe is None:
        output = command(argv)
    else:
        # Bound every wait; observe the short-lived probe while the fixed helper
        # runs. Missing evidence fails instead of treating controller presence as proof.
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
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
    output = command(("/usr/bin/systemctl", "show", name, "--property=UnitFileState,ActiveState,MainPID,InvocationID"))
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
    evidence["status"] = helper("status", observe=observe)
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


NS = 1_000_000_000
PERIOD_DROPIN = Path("/etc/systemd/system/appio-iod.service.d/20-sample-period.conf")


def period_export():
    result = helper("export-history")
    path, data = exported(result)
    return {"helper": result, "tables": dict(data), "path": str(path)}


def period_status(seconds):
    status = exchange()[-1]
    require(status.get("answering") is True and status.get("sample_ns") == seconds * NS,
            f"daemon does not report {seconds} seconds")
    return status


def frame_endpoints(export, boot, epoch, start=0, end=None):
    endpoints = {}
    for row in export["tables"]["sample"]:
        row_boot, source, row_epoch, seq, _, _, _, observed = row[:8]
        if row_boot == boot and source == "iod" and row_epoch == epoch and observed >= start and (end is None or observed <= end):
            key = (row_epoch, seq)
            if key in endpoints:
                require(endpoints[key] == observed, "inconsistent frame endpoints")
            endpoints[key] = observed
    return [endpoints[key] for key in sorted(endpoints)]


def test_two_second_and_thirty_second_runs_match_status_and_intervals(export, status, seconds):
    cursor = status["cursor"]
    points = frame_endpoints(export, cursor["boot_id"], cursor["iod_epoch"])
    require(len(points) >= 3, f"insufficient {seconds}-second committed frames")
    period = seconds * NS
    tolerance = max(500_000_000, period // 10)
    deltas = [b - a for a, b in zip(points[-3:], points[-2:])]
    require(all(abs(delta - period) <= tolerance for delta in deltas), "sample cadence outside tolerance")
    return {"sample_ns": status["sample_ns"], "endpoints": points[-3:], "deltas": deltas}


def test_apply_two_and_thirty_seconds_restarts_only_active_service(seconds):
    before = unit()
    require(before["ActiveState"] == "active" and before["InvocationID"], "service must be active before period change")
    boundary = time.monotonic_ns()
    result = helper("set-sample-period", str(seconds))
    after = unit()
    status = period_status(seconds)
    deadline = time.monotonic() + seconds * 2 + 10
    while status.get("cursor") is None and time.monotonic() < deadline:
        time.sleep(1)
        status = period_status(seconds)
    require(status.get("cursor") is not None, "collector did not publish a cursor")
    require(result.get("sample_period") == seconds and after["ActiveState"] == "active" and after["InvocationID"] and after["InvocationID"] != before["InvocationID"], "active apply failed to restart service")
    return {"before": before, "after": after, "result": result, "status": status, "restart_boundary_ns": boundary}


def test_active_period_apply_records_restart_gap(before, after, apply):
    boot = apply["status"]["cursor"]["boot_id"]
    previous_max = max((row[0] for row in before["tables"]["gap"]), default=0)
    gaps = [row for row in after["tables"]["gap"] if row[0] > previous_max and row[1] == boot and row[2] == "iod" and row[7] == "collector_gap"]
    if gaps:
        return {"baseline_max_gap_id": previous_max, "new_gaps": gaps}
    epoch = apply["status"]["cursor"]["iod_epoch"]
    epochs = [row for row in after["tables"]["collector_epoch"] if row[0] == "iod" and row[1] == boot and row[2] == epoch]
    if epochs:
        epoch_start = epochs[0][4]
        partial = [row for row in after["tables"]["sample"] if row[0] == boot and row[1] == "iod" and row[2] == epoch and row[8] is not None and row[9] is not None and row[8] < apply["restart_boundary_ns"] and row[7] == epoch_start and row[9] == epoch_start - row[8]]
        if partial:
            return {"baseline_max_gap_id": previous_max, "new_epoch_start_ns": epoch_start, "partial_interval": partial[0]}
    raise RuntimeError("no new collector_gap or truncated partial interval in exported history after active apply")


def test_identical_period_reapply_is_unchanged():
    before = PERIOD_DROPIN.stat()
    invocation = unit()["InvocationID"]
    result = helper("set-sample-period", "30")
    after = PERIOD_DROPIN.stat()
    require(result.get("sample_period_state") == "unchanged" and
            (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns) and
            unit()["InvocationID"] == invocation, "identical period reapply changed service or drop-in")
    return {"result": result, "inode": before.st_ino, "mtime_ns": before.st_mtime_ns, "invocation_id": invocation}


def test_thirty_second_ten_minute_run_has_no_overdue_paths():
    from appio.iod import flush_ns_for
    baseline = period_export()
    before = period_status(30)
    invocation = unit()["InvocationID"]
    start = time.monotonic_ns()
    while time.monotonic_ns() - start < 600 * NS:
        time.sleep(min(5, (600 * NS - (time.monotonic_ns() - start)) / NS))
    end = time.monotonic_ns()
    final = period_export()
    after = period_status(30)
    old = baseline["helper"]["export_cutoff"]
    new = final["helper"]["export_cutoff"]
    require((old["boot_id"], old["iod_epoch"]) == (new["boot_id"], new["iod_epoch"]) == (before["cursor"]["boot_id"], before["cursor"]["iod_epoch"]) == (after["cursor"]["boot_id"], after["cursor"]["iod_epoch"]) and unit()["InvocationID"] == invocation, "collector identity changed during measurement")
    points = frame_endpoints(final, old["boot_id"], old["iod_epoch"], start, end)
    tolerance = max(500_000_000, 30 * NS // 10)
    require(len(points) >= 18 and points[0] - start <= 30 * NS + tolerance and end - points[-1] <= 30 * NS + tolerance, "measurement endpoint coverage insufficient")
    deltas = [b - a for a, b in zip(points, points[1:])]
    flush = flush_ns_for(30 * NS)
    require(flush == 90 * NS and all(abs(delta - 30 * NS) <= tolerance and delta < flush for delta in deltas), "overdue or irregular sample interval")
    return {"baseline": baseline["helper"], "final": final["helper"], "start_ns": start, "end_ns": end, "invocation_id": invocation, "endpoints": points, "deltas": deltas, "flush_ns": flush}


def period_runtime(evidence):
    require(evidence.get("host_checks", {}).get("verify", "").startswith("passed"), "run verify before period-runtime")
    record = evidence["period_runtime"] = {"applies": []}
    try:
        for seconds in (2, 30):
            before = period_export()
            apply = test_apply_two_and_thirty_seconds_restarts_only_active_service(seconds)
            item = {"before_export": before["helper"], "apply": apply}
            record["applies"].append(item)
            time.sleep(seconds * 3 + 1)
            after = period_export()
            item["after_export"] = after["helper"]
            item["restart_gap"] = test_active_period_apply_records_restart_gap(before, after, apply)
            item["cadence"] = test_two_second_and_thirty_second_runs_match_status_and_intervals(after, period_status(seconds), seconds)
        record["identical"] = test_identical_period_reapply_is_unchanged()
        record["ten_minute"] = test_thirty_second_ten_minute_run_has_no_overdue_paths()
    finally:
        try:
            result = helper("set-sample-period", "10")
            status = period_status(10)
            record["cleanup"] = {"result": result, "status": status, "restored": True}
        except Exception as error:
            record["cleanup"] = {"restored": False, "error": str(error)}
            raise


@contextmanager
def capacity_fixture(path, app_count=226, initial_count=57_968):
    from appio.durable import canonical_json
    from appio.schema import SCHEMA_SQL
    from appio.store import _empty_coverage

    coverage = canonical_json(_empty_coverage())
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA_SQL)
        db.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) VALUES('capacity',1,0,0,'iod')")
        db.execute("INSERT INTO clock_map VALUES('capacity',0,0)")
        db.executemany("INSERT INTO app VALUES(?,?,?,NULL,0,0,'system')", ((app, f'app-{app}', f'app-{app}') for app in range(1, app_count + 1)))
        db.executemany("INSERT INTO app_boot_total VALUES('capacity',?,0,0,0,0,NULL,NULL,NULL,?)", ((app, coverage) for app in range(1, app_count + 1)))
        db.executemany("INSERT INTO app_lifetime_total VALUES(?,0,0,0,0,?,'capacity',0)", ((app, coverage) for app in range(1, app_count + 1)))
        db.executemany("INSERT INTO rollup_app VALUES('capacity',?,0,?,0,0,0,0,NULL,NULL,NULL,?)", ((app, secs, coverage) for app in range(1, app_count + 1) for secs in (60, 3600)))
        db.executemany("INSERT INTO sample_coverage VALUES('capacity','iod',1,?,?,0,?)", ((seq // app_count, seq % app_count + 1, coverage) for seq in range(initial_count)))
        db.commit()
        yield db
    finally:
        db.close()


def capacity_generate(db, app_count, seq, mono_ns):
    from appio.durable import canonical_json
    from appio.store import _empty_coverage

    coverage = canonical_json(_empty_coverage())
    db.executemany("INSERT OR IGNORE INTO rollup_app VALUES('capacity',?,?,?,0,0,0,0,NULL,NULL,NULL,?)", ((app, mono_ns // (secs * NS) * (secs * NS), secs, coverage) for app in range(1, app_count + 1) for secs in (60, 3600)))
    db.executemany("INSERT INTO sample_coverage VALUES('capacity','iod',1,?,?,?,?)", ((seq, app, mono_ns, coverage) for app in range(1, app_count + 1)))
    db.commit()


def period_capacity(evidence):
    from appio.iod import maintenance_ns_for
    from appio.quota import DEFAULT_BATCH_SIZE, QuotaPruner

    require(maintenance_ns_for(2 * NS) == 15 * NS and DEFAULT_BATCH_SIZE == 2048, "unexpected pruning cadence or batch")
    record = evidence["period_capacity"] = {"app_count": 226, "initial_backlog": 57_968, "generated": 0, "retired": 0, "batches": []}
    temp_root = Path(tempfile.gettempdir()).resolve()
    require(not temp_root.is_relative_to(Path("/var/lib/appio")), "capacity fixture must be outside the installed state directory")
    with tempfile.TemporaryDirectory(prefix="appio-period-capacity-", dir=temp_root) as directory:
        path = Path(directory) / "capacity.sqlite"
        with capacity_fixture(path) as db:
            count = db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0]
            require(count == record["initial_backlog"], "initial fixture backlog mismatch")
            pruner = QuotaPruner(db, [path])
            start = time.monotonic_ns()
            next_sample = 2 * NS
            next_prune = 15 * NS
            sequence = 100_000
            while next_prune <= 1800 * NS:
                elapsed = time.monotonic_ns() - start
                if elapsed < min(next_sample, next_prune):
                    time.sleep((min(next_sample, next_prune) - elapsed) / NS)
                    continue
                if next_sample <= next_prune:
                    require(elapsed - next_sample < 2 * NS, "capacity generation missed a sample deadline")
                    capacity_generate(db, 226, sequence, next_sample)
                    sequence += 1
                    record["generated"] += 226
                    next_sample += 2 * NS
                else:
                    before = db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0]
                    require(before >= 2048, "insufficient eligible backlog")
                    result = pruner.expire("capacity", 2 * 86_400 * NS + next_prune)
                    after = db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0]
                    batch = {"deadline_ns": next_prune, "pre_prune": before, "retired": len(result.deleted), "skipped": len(result.skipped), "complete": result.complete, "sampling_paused": result.sampling_paused, "physical_bytes": result.physical_bytes, "reason": result.reason, "post_prune": after}
                    record["batches"].append(batch)
                    require(len(result.deleted) == 2048 and not result.skipped and before - after == 2048 and all(unit.kind == "core" for unit in result.deleted), "pruning batch was not saturated")
                    record["retired"] += len(result.deleted)
                    next_prune += 15 * NS
            record["elapsed_ns"] = time.monotonic_ns() - start
            record["final_backlog"] = db.execute("SELECT count(*) FROM sample_coverage").fetchone()[0]
            record["minimum_pre_prune"] = min(batch["pre_prune"] for batch in record["batches"])
            require(record["elapsed_ns"] >= 1800 * NS and record["retired"] * NS > 113 * record["elapsed_ns"] and record["final_backlog"] <= record["initial_backlog"], "capacity measurement failed")


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
            {"enable": enable, "verify": verify, "period-runtime": period_runtime, "period-capacity": period_capacity, "disable": disable}[args.phase](evidence)
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
