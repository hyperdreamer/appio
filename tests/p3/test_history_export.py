"""Rootless export tests: WAL history, hostile paths and atomic publication."""
from __future__ import annotations

from dataclasses import replace
import errno
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

import appio.history_export as exports
from appio.durable import HandoverState, canonical_json
from appio.history_export import ExportError, export_history
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.sampler import TickInput, aggregate_tick
from appio.setup_boundary import CommandResult, SetupDeps, SetupIdentity
from appio.store import AppIoStore

BOOT = "11111111-2222-4333-8444-555555555555"
STAMP = 1_700_000_000_000_000_000


def host(env, path):
    return env.root / Path(path).relative_to("/")


def tables(connection):
    names = connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name")
    return {name: sorted(map(tuple, connection.execute(f'SELECT * FROM "{name}"')), key=repr) for (name,) in names}


@pytest.fixture
def export_env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = root / "home/alice"
    home.mkdir(parents=True, mode=0o700)
    (root / "var/lib/appio").mkdir(parents=True, mode=0o711)
    identity = SetupIdentity(os.getuid(), os.getgid(), "alice", Path("/home/alice"))
    overrides = {}
    events = []
    ownership = []

    def metadata(fd):
        real = os.fstat(fd)
        key = real.st_dev, real.st_ino
        path = os.readlink(f"/proc/self/fd/{fd}")
        default = identity.uid if path == str(home) or path.startswith(str(home) + "/") else 0
        uid, gid, mode = overrides.get(key, (default, identity.gid, real.st_mode))
        return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode, st_dev=real.st_dev,
                               st_ino=real.st_ino, st_nlink=real.st_nlink, st_size=real.st_size)

    def chown(fd, uid, gid):
        real = os.fstat(fd)
        overrides[real.st_dev, real.st_ino] = uid, gid, real.st_mode
        ownership.append((uid, gid))

    record = lambda uid: SimpleNamespace(pw_uid=uid, pw_gid=identity.gid, pw_name="alice", pw_dir=str(identity.home))
    deps = SetupDeps(root, SimpleNamespace(run=lambda *a, **k: CommandResult(0, "", "")),
                     lambda: 0, record, metadata, chown, lambda: 123456789, events.append)
    monkeypatch.setattr(exports.time, "time_ns", lambda: STAMP)
    return SimpleNamespace(root=root, home=home, identity=identity, deps=deps,
                           overrides=overrides, events=events, ownership=ownership)


@pytest.fixture
def history(export_env):
    env = export_env
    path = host(env, "/var/lib/appio/iod.db")
    store = AppIoStore(path)
    db = store.connection
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    store.start_boot(BOOT, 1, 0, 1000.0, "iod")
    epoch = store.start_collector("iod", BOOT, 100).epoch
    state = None
    seq = 0

    def advance(amount=100):
        nonlocal state, seq
        cgroup = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-fixture.service"
        process = StableProcRecord(ProcessIdentity(BOOT, 410, 90), 1, env.identity.uid,
            "/usr/bin/fixture", cgroup, AppKeyInputs(cgroup, "/usr/bin/fixture"),
            IoReadOutcome("readable", (amount * 10, amount * 5, amount, amount * 2), True, None))
        tick = TickInput(BOOT, "iod", epoch, seq, 100 + seq * 100,
                         1_700_000_000_000_000_000 + seq * 100, (process,), ())
        frame, state = aggregate_tick(state, tick)
        store.record_tick(frame, state)
        seq += 1

    advance(100)
    advance(140)
    os.chmod(path, 0o600)
    value = SimpleNamespace(env=env, path=path, store=store, db=db, advance=advance)
    yield value
    store.connection.close()


def test_online_backup_includes_wal_rows_all_tables_and_valid_checkpoint(history):
    """A main-file copy loses committed WAL-only totals and checkpoint evidence."""
    h = history
    assert Path(str(h.path) + "-wal").stat().st_size > 0
    before = tables(h.db)
    result = export_history(h.env.identity, deps=h.env.deps)
    path = host(h.env, result.path)
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as exported:
        assert tables(exported) == before
        assert exported.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert exported.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (result.boot_id, result.iod_epoch, result.seq, result.cutoff_mono_ns) == (BOOT, 1, 1, 200)
    assert result.path.parent == Path("/home/alice/.local/share/appio")
    assert path.name == f"iod-export-{STAMP}.db"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == os.getuid()  # independently readable without root
    assert h.env.ownership[-1] == (h.env.identity.uid, h.env.identity.gid)
    assert not list(path.parent.glob(".*tmp*"))
    assert not list(host(h.env, "/var/lib/appio/staging").iterdir())


def test_same_timestamp_collisions_never_overwrite_or_follow_a_final_symlink(history, tmp_path):
    h = history
    destination = h.env.home / ".local/share/appio"
    destination.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"do not replace")
    (destination / f"iod-export-{STAMP}.db").symlink_to(victim)
    (destination / f"iod-export-{STAMP}-1.db").write_bytes(b"existing export")
    first = export_history(h.env.identity, deps=h.env.deps)
    second = export_history(h.env.identity, deps=h.env.deps)
    assert first.path.name == f"iod-export-{STAMP}-2.db"
    assert second.path.name == f"iod-export-{STAMP}-3.db"
    assert victim.read_bytes() == b"do not replace"
    assert (destination / f"iod-export-{STAMP}.db").is_symlink()
    assert (destination / f"iod-export-{STAMP}-1.db").read_bytes() == b"existing export"


@pytest.mark.parametrize("component", ["home", ".local", "share", "appio"])
def test_symlinked_destination_or_ancestor_is_refused_without_writing_elsewhere(history, tmp_path, component):
    h = history
    outside = tmp_path / "outside"
    outside.mkdir()
    target = h.env.home if component == "home" else h.env.home / ".local"
    if component == "share":
        target = target / "share"
    if component == "appio":
        target = target / "share/appio"
    if target == h.env.home:
        target.rmdir()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    before = tables(h.db)
    with pytest.raises(ExportError, match="destination"):
        export_history(h.env.identity, deps=h.env.deps)
    assert list(outside.iterdir()) == []
    assert tables(h.db) == before


@pytest.mark.parametrize("uid,mode", [(4242, 0o700), (0, 0o700), (1000, 0o777)])
def test_foreign_owned_or_writable_home_is_rejected(history, uid, mode):
    h = history
    info = h.env.home.stat()
    h.env.overrides[info.st_dev, info.st_ino] = uid, 0, stat.S_IFDIR | mode
    with pytest.raises(ExportError, match="destination"):
        export_history(h.env.identity, deps=h.env.deps)
    assert not (h.env.home / ".local").exists()


def test_missing_source_never_creates_a_database_or_export(export_env):
    env = export_env
    with pytest.raises(ExportError, match="source.*missing"):
        export_history(env.identity, deps=env.deps)
    assert not host(env, "/var/lib/appio/iod.db").exists()
    assert not (env.home / ".local").exists()


@pytest.mark.parametrize("point", ["export-backup", "export-copy", "export-chown", "export-fsync", "export-link", "export-fsync-dir"])
def test_failure_never_claims_a_partial_export_and_preserves_root_history(history, point):
    h = history
    before = tables(h.db)
    observed = []

    def fail(name):
        observed.append(name)
        if name == point:
            raise OSError(errno.ENOSPC, point)

    deps = replace(h.env.deps, event=fail)
    with pytest.raises(ExportError):
        export_history(h.env.identity, deps=deps)
    assert point in observed
    assert tables(h.db) == before
    assert h.path.exists()
    directory = h.env.home / ".local/share/appio"
    assert not directory.exists() or list(directory.iterdir()) == []
    stage = host(h.env, "/var/lib/appio/staging")
    assert not stage.exists() or list(stage.iterdir()) == []


def test_actual_ownership_failure_is_not_success(history):
    h = history

    def deny(fd, uid, gid):
        if uid == h.env.identity.uid:
            raise PermissionError("chown denied")
        h.env.deps.chown(fd, uid, gid)

    with pytest.raises(ExportError, match="ownership"):
        export_history(h.env.identity, deps=replace(h.env.deps, chown=deny))
    assert h.path.exists()


def test_missing_safe_anonymous_publication_is_reported(history, monkeypatch):
    h = history
    original = os.open

    def unsupported(path, flags, *args, **kwargs):
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError(errno.EOPNOTSUPP, "O_TMPFILE unsupported")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(exports.os, "open", unsupported)
    with pytest.raises(ExportError, match="anonymous_publication_unsupported"):
        export_history(h.env.identity, deps=h.env.deps)
    assert h.path.exists()


@pytest.mark.parametrize("damage", ["schema", "checkpoint"])
def test_invalid_trusted_backup_is_not_published(history, damage):
    h = history
    if damage == "schema":
        h.db.execute("CREATE TABLE extra_unexpected(value TEXT)")
    else:
        h.db.execute("UPDATE sampler_checkpoint SET state_json='{}'")
    h.db.commit()
    with pytest.raises(ExportError, match="validation"):
        export_history(h.env.identity, deps=h.env.deps)
    directory = h.env.home / ".local/share/appio"
    assert not directory.exists() or list(directory.iterdir()) == []


def test_empty_valid_history_exports_without_fabricated_cutoff(export_env):
    env = export_env
    store = AppIoStore(host(env, "/var/lib/appio/iod.db"))
    store.connection.close()
    result = export_history(env.identity, deps=env.deps)
    assert (result.boot_id, result.iod_epoch, result.seq, result.cutoff_mono_ns) == (None, None, None, None)


def test_stale_or_forged_nss_home_never_selects_an_export_destination(history):
    h = history
    with pytest.raises(ExportError, match="identity"):
        export_history(replace(h.env.identity, home=Path("/root")), deps=h.env.deps)
    assert not host(h.env, "/root").exists()


def test_root_owned_untraversable_ancestor_cannot_produce_unreadable_export(history):
    h = history
    ancestor = h.env.home.parent
    info = ancestor.stat()
    h.env.overrides[info.st_dev, info.st_ino] = 0, 0, stat.S_IFDIR | 0o700
    with pytest.raises(ExportError, match="destination"):
        export_history(h.env.identity, deps=h.env.deps)
    assert not (h.env.home / ".local").exists()


@pytest.mark.parametrize("phase", ["export-fsync", "export-fsync-dir"])
def test_actual_publication_fsync_error_never_returns_success(history, monkeypatch, phase):
    h = history
    current = [None]
    original = os.fsync

    def sync(fd):
        if current[0] == phase:
            current[0] = None  # permit cleanup's directory sync
            raise OSError(errno.EIO, "real fsync fault")
        return original(fd)

    deps = replace(h.env.deps, event=lambda name: current.__setitem__(0, name))
    monkeypatch.setattr(exports.os, "fsync", sync)
    with pytest.raises(ExportError):
        export_history(h.env.identity, deps=deps)
    assert list((h.env.home / ".local/share/appio").iterdir()) == []
    assert h.path.exists()


def test_busy_backup_has_one_wall_clock_deadline_and_preserves_source(history, monkeypatch):
    h = history
    h.db.execute("PRAGMA journal_mode=DELETE")
    h.db.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(exports, "EXPORT_TIMEOUT_SECONDS", 0.08)
    start = exports.time.monotonic()
    try:
        with pytest.raises(ExportError, match="deadline"):
            export_history(h.env.identity, deps=h.env.deps)
    finally:
        h.db.rollback()
    assert exports.time.monotonic() - start < 1
    assert h.path.exists()


@pytest.mark.parametrize("phase", ["export-copy", "export-fsync-dir"])
def test_destination_rename_during_copy_never_returns_a_stale_export_path(history, phase):
    h = history
    pinned = h.env.home / ".local/share/appio"
    moved = h.env.home / ".local/share/old-appio"

    def swap(name):
        if name == phase:
            pinned.rename(moved)
            pinned.mkdir()

    with pytest.raises(ExportError, match="destination:changed"):
        export_history(h.env.identity, deps=replace(h.env.deps, event=swap))
    assert list(pinned.iterdir()) == [] and list(moved.iterdir()) == []


def test_missing_linkat_capability_is_a_precise_export_error(history, monkeypatch):
    h = history
    linkat = lambda *args: -1
    monkeypatch.setattr(exports.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(linkat=linkat))
    monkeypatch.setattr(exports.ctypes, "get_errno", lambda: errno.EOPNOTSUPP)
    with pytest.raises(ExportError, match="anonymous_publication_unsupported"):
        export_history(h.env.identity, deps=h.env.deps)
    assert list((h.env.home / ".local/share/appio").iterdir()) == []
    assert h.path.exists()


@pytest.mark.parametrize("case", ["main-symlink", "sidecar-symlink", "foreign-owner"])
def test_untrusted_root_source_and_sidecars_are_never_opened_as_sqlite(history, tmp_path, case):
    h = history
    outside = tmp_path / "private-victim"
    outside.write_bytes(b"untouched")
    if case == "main-symlink":
        h.path.rename(h.path.with_suffix(".saved"))
        h.path.symlink_to(outside)
    elif case == "sidecar-symlink":
        Path(str(h.path) + "-journal").symlink_to(outside)
    else:
        info = h.path.stat()
        h.env.overrides[info.st_dev, info.st_ino] = 4242, 4242, stat.S_IFREG | 0o600
    with pytest.raises(ExportError, match="source"):
        export_history(h.env.identity, deps=h.env.deps)
    assert outside.read_bytes() == b"untouched"
    assert not (h.env.home / ".local").exists()


def test_handover_checkpoints_preserve_canonical_evidence_and_reject_invalid_rows(history):
    h = history
    text = canonical_json(HandoverState(BOOT, 100, (), ()))
    h.db.execute("INSERT INTO handover VALUES (?, 'gui', 'iod', 100, ?)", (BOOT, text))
    h.db.commit()
    result = export_history(h.env.identity, deps=h.env.deps)
    with sqlite3.connect(host(h.env, result.path)) as exported:
        assert exported.execute("SELECT baselines_json FROM handover").fetchone()[0] == text
    h.db.execute("UPDATE handover SET baselines_json='{}'")
    h.db.commit()
    with pytest.raises(ExportError, match="validation"):
        export_history(h.env.identity, deps=h.env.deps)
