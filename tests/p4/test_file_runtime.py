"""Committed T/D receipts drive independent, loss-honest F persistence.

Falsifiers are named in the tests: no drain receipt rewriting, duplicate drain,
missed hour close, early top-N, partial-hour promotion, or leaked F transaction.
All process/cgroup and loader records are synthetic; no host lookups or BPF load.
"""
from dataclasses import replace
import json
import sqlite3

import pytest

from appio.bpf_fileio import BpfFileSource, FileEvent, FileSourceError, VectorLoader
from appio.contracts import FILE_HOUR_NS as H, SQLITE_INT_MAX, Window
from appio.durable import Receipt, decode_file_hour_state
from appio.file_runtime import FileRuntime
from appio.files import FileFrameContext, FileIdentityResolver
from appio.handover import baseline_seed
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.query import QueryService
from appio.sampler import TickInput, aggregate_tick
from appio.store import AppIoStore

APP, ZERO = "exe:/demo", "exe:/zero"


def event(cgroup=1, inode=7, amount=700):
    return FileEvent(cgroup, "8:0", inode, 3, b"Cafe\xcc\x81", 6, 1, 2, amount, 11)


class Frames:
    def __init__(self, path):
        self.store = AppIoStore(path)
        self.boot("boot-A", 1)

    @property
    def db(self):
        return self.store.connection

    def boot(self, boot, order):
        self.boot_id = boot
        self.store.start_boot(boot, order, 0, 1000.0, "iod")
        self.epoch = self.store.start_collector("iod", boot, 0).epoch
        self.seq, self.state = -1, None

    def tick(self, at, *, amount=100):
        self.seq += 1
        records = tuple(StableProcRecord(
            ProcessIdentity(self.boot_id, pid, 100), 1, 1000, name, "/fixture",
            AppKeyInputs("/fixture", name), IoReadOutcome("readable", (amount,) * 4, None, None),
        ) for pid, name in ((410, "/demo"), (411, "/zero")))
        tick = TickInput(self.boot_id, "iod", self.epoch, self.seq, at, 1000 * 10**9 + at, records, ())
        frame, self.state = baseline_seed(tick) if self.state is None else aggregate_tick(self.state, tick)
        self.store.record_tick(frame, self.state)
        context = FileFrameContext(self.boot_id, self.epoch, self.seq, at, (APP, ZERO), ((1, (APP,)), (2, (ZERO,))))
        return frame, context


@pytest.fixture
def frames(tmp_path):
    value = Frames(tmp_path / "files.db")
    yield value
    value.db.close()


def runtime(frames, loader=None):
    loader = VectorLoader() if loader is None else loader
    rt = FileRuntime(frames.db, source_factory=lambda: BpfFileSource(loader),
                     identity_resolver=FileIdentityResolver(lambda dev: "fs-uuid"))
    assert rt.start().available
    return rt, loader


def pending(frames, boot="boot-A", hour=0):
    row = frames.db.execute("SELECT state_json FROM file_hour_state WHERE boot_id=? AND ts_mono_ns=?", (boot, hour)).fetchone()
    return None if row is None else decode_file_hour_state(row[0], boot, hour)


def test_file_store_rejects_caller_transaction_without_savepoint_or_mutation():
    from appio.file_store import _atomic

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE marker(value INTEGER)")
    db.execute("BEGIN")
    db.execute("INSERT INTO marker VALUES(1)")
    try:
        with pytest.raises(ValueError, match="F persistence requires its own transaction"):
            with _atomic(db):
                db.execute("INSERT INTO marker VALUES(2)")
        assert db.in_transaction
        assert db.execute("SELECT value FROM marker ORDER BY value").fetchall() == [(1,)]
    finally:
        db.rollback()
        db.close()


def test_f_transaction_survives_later_caller_rollback_and_own_failure():
    from appio.file_store import _atomic

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE marker(value INTEGER)")
    try:
        with _atomic(db):
            db.execute("INSERT INTO marker VALUES(2)")
        db.execute("BEGIN")
        db.execute("INSERT INTO marker VALUES(3)")
        db.rollback()
        assert db.execute("SELECT value FROM marker").fetchall() == [(2,)]
        with pytest.raises(RuntimeError, match="F write failed"):
            with _atomic(db):
                db.execute("INSERT INTO marker VALUES(4)")
                raise RuntimeError("F write failed")
        assert db.execute("SELECT value FROM marker").fetchall() == [(2,)]
        assert not db.in_transaction
    finally:
        db.close()


def test_receipt_zero_app_and_unknown_owner_persist_without_touching_T_D(frames):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event(), event(cgroup=99, amount=31)])
    frame, context = frames.tick(0)
    assert rt.consume(frame, context)
    state = pending(frames)
    assert [app.app_key for app in state.apps] == [APP, ZERO]
    assert all(app.last_receipt == Receipt("iod", frame.epoch, frame.seq) for app in state.apps)
    assert state.apps[1].files == () and state.apps[1].coverage.complete == 1
    assert state.unknown_owner.overflow_bytes_r == 31
    assert state.unknown_owner.last_receipt == Receipt("iod", frame.epoch, frame.seq)
    assert loader.flip_requests == [(frame.epoch, frame.seq)]
    assert frames.db.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone()[0] == 0
    assert frames.db.execute("SELECT count(*) FROM app_boot_dev_total").fetchone()[0] == 0
    assert not frames.db.in_transaction


def test_duplicate_and_stale_receipts_are_rejected_before_drain_even_after_restart(frames):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event()])
    frame, context = frames.tick(0)
    rt.consume(frame, context)
    before = pending(frames)
    assert rt.consume(frame, context) is False
    assert loader.flip_requests == [(1, 0)]
    rt.close()
    other, new_loader = runtime(frames)
    assert other.consume(frame, context) is False
    assert new_loader.flip_requests == []
    assert pending(frames).apps[0].files == before.apps[0].files


def test_closed_hour_includes_last_old_hour_drain_and_finalizes_once(frames):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event(amount=20)])
    rt.consume(*frames.tick(0))
    loader.queue(1, events=[event(amount=30)])
    rt.consume(*frames.tick(H - 1))
    assert frames.db.execute("SELECT count(*) FROM file_sample").fetchone() == (0,)
    loader.queue(0, events=[event(amount=999)])
    rt.consume(*frames.tick(H))
    assert frames.db.execute("SELECT source,epoch,seq,ts_mono_ns,bytes_r FROM file_sample").fetchall() == [("iod", 1, 1, 0, 50)]
    assert frames.db.execute("SELECT bytes_r FROM app_boot_file_total").fetchone() == (50,)
    assert frames.db.execute("SELECT file_finalized_through_mono_ns FROM boot").fetchone() == (H,)
    assert pending(frames) is None
    assert pending(frames, hour=H).apps[0].files[0].bytes_r == 999
    assert frames.db.execute("SELECT count(*) FROM file_coverage WHERE ts_mono_ns=0").fetchone() == (2,)
    assert frames.db.execute("SELECT count(*) FROM file_overflow_unknown WHERE ts_mono_ns=0").fetchone() == (1,)
    app_id = frames.db.execute("SELECT app_id FROM app WHERE key=?", (ZERO,)).fetchone()[0]
    table = QueryService(frames.db).file_table(app_id, Window("since_boot", "boot-A", end_mono_ns=H), "bytes_total", "desc", None)
    assert table.rows == () and table.coverage.complete


def test_top_100_is_chosen_only_after_all_drains(frames):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event(inode=i, amount=i + 1) for i in range(101)])
    rt.consume(*frames.tick(0))
    assert len(pending(frames).apps[0].files) == 101
    loader.queue(1, events=[event(inode=0, amount=1000)])
    rt.consume(*frames.tick(H - 1))
    rt.consume(*frames.tick(H))
    assert frames.db.execute("SELECT count(*) FROM file_sample").fetchone() == (100,)
    assert frames.db.execute("SELECT bytes_r FROM file_sample s JOIN path p USING(path_id) WHERE p.inode=0").fetchone() == (1001,)
    assert frames.db.execute("SELECT counted_omitted_bytes_r FROM file_coverage c JOIN app a USING(app_id) WHERE a.key=? AND ts_mono_ns=0", (APP,)).fetchone() == (2,)


@pytest.mark.parametrize("fault", ["clear", "counter", "protocol", "identity", "sqlite"])
def test_F_failure_rolls_back_only_F_records_gap_and_next_T_D_receipt_commits(frames, fault):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event(amount=17)])
    rt.consume(*frames.tick(0))
    if fault == "clear":
        loader.clear_failure = -5
    elif fault == "counter":
        loader.queue(1, events=[event(amount=SQLITE_INT_MAX)])
    elif fault == "protocol":
        loader.echo_seq = 999
    elif fault == "identity":
        rt.identity_resolver = FileIdentityResolver(lambda dev: 123)
        loader.queue(1, events=[event()])
    else:
        frames.db.execute("CREATE TEMP TRIGGER fail_F BEFORE INSERT ON file_hour_state BEGIN SELECT RAISE(FAIL,'F write failed'); END")
        frames.db.commit()
    with pytest.raises(FileSourceError):
        rt.consume(*frames.tick(10 * 10**9, amount=200))
    assert not rt.availability.available and rt.diagnostic
    assert not frames.db.in_transaction
    if fault != "sqlite":
        assert pending(frames).apps[0].files[0].bytes_r == 17
        assert pending(frames).apps[0].coverage.complete == 0
    frames.tick(20 * 10**9, amount=300)
    assert frames.db.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone() == (400,)
    # A generic iod overflow diagnostic would poison record_tick's next epoch.
    assert frames.db.execute("SELECT count(*) FROM store_diagnostic").fetchone() == (0,)


def test_unavailable_attach_does_not_erase_retained_file_totals(frames):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event(amount=55)])
    rt.consume(*frames.tick(0))
    rt.consume(*frames.tick(H))
    rt.close()
    failed = FileRuntime(frames.db, source_factory=lambda: BpfFileSource(VectorLoader(hello_status=1, hello_error=-1, verifier=b"verifier refused")))
    assert not failed.start().available
    with pytest.raises(FileSourceError):
        failed.consume(*frames.tick(H + 10**9))
    assert frames.db.execute("SELECT bytes_r FROM app_boot_file_total").fetchone() == (55,)
    assert pending(frames, hour=H).apps[0].coverage.complete == 0
    assert "errno -1" in failed.diagnostic and "verifier refused" in failed.diagnostic


def test_partial_shutdown_is_pending_and_incomplete_then_new_source_extends_it(frames):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event(amount=55)])
    rt.consume(*frames.tick(12))
    rt.close()
    assert loader.close_requests == 1
    assert pending(frames).apps[0].coverage.complete == 0
    assert frames.db.execute("SELECT count(*) FROM file_sample").fetchone() == (0,)
    other, fresh = runtime(frames)
    fresh.queue(0, events=[event(amount=9)])
    other.consume(*frames.tick(15))
    other.consume(*frames.tick(H))
    assert frames.db.execute("SELECT bytes_r FROM app_boot_file_total").fetchone() == (64,)
    cov = frames.db.execute("SELECT coverage_json FROM file_coverage LIMIT 1").fetchone()[0]
    assert json.loads(cov)["gap_intervals"] > 0


def test_boot_change_finalizes_old_partial_with_gap_without_comparing_monotonic_clocks(frames):
    rt, loader = runtime(frames)
    loader.queue(0, events=[event(amount=55)])
    rt.consume(*frames.tick(2 * H + 20))
    frames.boot("boot-B", 2)
    rt.consume(*frames.tick(5))
    assert frames.db.execute("SELECT boot_id,ts_mono_ns,bytes_r FROM file_sample").fetchall() == [("boot-A", 2 * H, 55)]
    assert json.loads(frames.db.execute("SELECT coverage_json FROM file_coverage LIMIT 1").fetchone()[0])["gap_intervals"] > 0
    assert pending(frames, boot="boot-B") is not None
    assert loader.close_requests == 1 and rt.availability.available


def test_mismatched_context_and_uncommitted_frame_never_drain(frames):
    rt, loader = runtime(frames)
    frame, context = frames.tick(0)
    with pytest.raises(FileSourceError):
        rt.consume(replace(frame, seq=5), replace(context, seq=5))
    assert loader.flip_requests == []
    assert pending(frames) is None


def test_old_committed_frame_is_rejected_before_drain_without_disabling_current_source(frames):
    rt, loader = runtime(frames)
    old = frames.tick(0)
    rt.consume(*old)
    rt.consume(*frames.tick(10**10))
    assert rt.consume(*old) is False
    assert loader.flip_requests == [(1, 0), (1, 1)]
    assert rt.availability.available and rt.diagnostic is None


@pytest.mark.parametrize("mode", ["hello", "drain", "close"])
def test_installed_helper_stalls_are_bounded_and_reaped_without_root_or_live_BPF(tmp_path, mode):
    import sys
    import time
    from appio.bpf_fileio import HOOK_MASK_ALL, HELLO_RESPONSE, ABI_VERSION
    from appio.file_runtime import (
        ATTACH_BUDGET_S,
        CLOSE_BUDGET_S,
        DRAIN_BUDGET_S,
        _ProcessLoader,
    )

    helper = tmp_path / "fake-fileio"
    obj = tmp_path / "fake-object"
    obj.touch()
    # Synthetic protocol peer, not a BPF loader. Each test deliberately stalls.
    response = HELLO_RESPONSE.pack(ABI_VERSION, 0, HOOK_MASK_ALL, 4096, 0, 0)
    helper.write_text(f"#!{sys.executable}\nimport os,time\n"
                      "os.read(0,8)\n"
                      + ("" if mode == "hello" else f"os.write(1,{response!r})\n")
                      + "time.sleep(60)\n")
    helper.chmod(0o700)
    loader = _ProcessLoader(str(helper), str(obj))
    try:
        if mode != "hello":
            loader.hello()
        at = time.monotonic()
        with pytest.raises((OSError, FileSourceError)):
            if mode == "hello":
                loader.hello()
            elif mode == "drain":
                loader.flip_drain(1, 1)
            else:
                loader.close()
        budget = {
            "hello": ATTACH_BUDGET_S,
            "drain": DRAIN_BUDGET_S,
            "close": CLOSE_BUDGET_S,
        }[mode]
        assert time.monotonic() - at < budget + 1.0
        assert loader.process.poll() is not None
    finally:
        try:
            loader._reap()
        finally:
            # Emergency fixture cleanup is deliberately AFTER the assertions,
            # independent of the mechanism under test (including mutants).
            if loader.process.poll() is None:
                loader.process.kill()
            loader.process.wait(timeout=1)
            loader.process.stdin.close()
            loader.process.stdout.close()


def test_slow_attach_response_after_600ms_succeeds(tmp_path):
    import sys
    from appio.bpf_fileio import ABI_VERSION, HOOK_MASK_ALL, HELLO_RESPONSE
    from appio.file_runtime import _ProcessLoader

    helper = tmp_path / "slow-attach-fileio"
    obj = tmp_path / "fake-object"
    obj.touch()
    response = HELLO_RESPONSE.pack(ABI_VERSION, 0, HOOK_MASK_ALL, 4096, 0, 0)
    helper.write_text(
        f"#!{sys.executable}\nimport os,time\n"
        "os.read(0,8)\n"
        "time.sleep(0.6)\n"
        f"os.write(1,{response!r})\n"
        "time.sleep(60)\n"
    )
    helper.chmod(0o700)
    loader = _ProcessLoader(str(helper), str(obj))
    try:
        assert loader.hello() == response
    finally:
        loader._reap()
        if loader.process.poll() is None:
            loader.process.kill()
        loader.process.wait(timeout=1)
        loader.process.stdin.close()
        loader.process.stdout.close()


def test_slow_drain_response_after_600ms_succeeds(tmp_path):
    import sys
    from appio.bpf_fileio import (
        ABI_VERSION,
        DRAIN_RESPONSE,
        HOOK_MASK_ALL,
        HELLO_RESPONSE,
    )
    from appio.file_runtime import _ProcessLoader

    helper = tmp_path / "slow-drain-fileio"
    obj = tmp_path / "fake-object"
    obj.touch()
    hello = HELLO_RESPONSE.pack(ABI_VERSION, 0, HOOK_MASK_ALL, 4096, 0, 0)
    drain = DRAIN_RESPONSE.pack(ABI_VERSION, 0, HOOK_MASK_ALL, 4096, 0, 0, 1, 1, 0, 0, 1, 0)
    helper.write_text(
        f"#!{sys.executable}\nimport os,time\n"
        "os.read(0,8)\n"
        f"os.write(1,{hello!r})\n"
        "os.read(0,24)\n"
        "time.sleep(0.6)\n"
        f"os.write(1,{drain!r})\n"
        "time.sleep(60)\n"
    )
    helper.chmod(0o700)
    loader = _ProcessLoader(str(helper), str(obj))
    try:
        loader.hello()
        assert loader.flip_drain(1, 1) == drain
    finally:
        loader._reap()
        if loader.process.poll() is None:
            loader.process.kill()
        loader.process.wait(timeout=1)
        loader.process.stdin.close()
        loader.process.stdout.close()


def test_stalled_drain_waits_to_and_then_aborts_at_drain_budget(tmp_path):
    import sys
    import time
    from appio.bpf_fileio import HOOK_MASK_ALL, HELLO_RESPONSE
    from appio.file_runtime import DRAIN_BUDGET_S, _ProcessLoader

    helper = tmp_path / "stalled-drain-fileio"
    obj = tmp_path / "fake-object"
    obj.touch()
    hello = HELLO_RESPONSE.pack(1, 0, HOOK_MASK_ALL, 4096, 0, 0)
    helper.write_text(
        f"#!{sys.executable}\nimport os,time\n"
        "os.read(0,8)\n"
        f"os.write(1,{hello!r})\n"
        "os.read(0,24)\n"
        "time.sleep(60)\n"
    )
    helper.chmod(0o700)
    loader = _ProcessLoader(str(helper), str(obj))
    try:
        loader.hello()
        started = time.monotonic()
        with pytest.raises(OSError):
            loader.flip_drain(1, 1)
        elapsed = time.monotonic() - started
        assert DRAIN_BUDGET_S - 0.5 < elapsed < DRAIN_BUDGET_S + 1.0, elapsed
        assert loader.process.poll() is not None
    finally:
        loader._reap()
        if loader.process.poll() is None:
            loader.process.kill()
        loader.process.wait(timeout=1)
        loader.process.stdin.close()
        loader.process.stdout.close()


def test_stalled_attach_is_bounded_by_attach_budget(tmp_path):
    import sys
    import time
    from appio.file_runtime import ATTACH_BUDGET_S, _ProcessLoader

    helper = tmp_path / "stalled-attach-fileio"
    obj = tmp_path / "fake-object"
    obj.touch()
    helper.write_text(
        f"#!{sys.executable}\nimport os,time\n"
        "os.read(0,8)\n"
        "time.sleep(60)\n"
    )
    helper.chmod(0o700)
    loader = _ProcessLoader(str(helper), str(obj))
    try:
        started = time.monotonic()
        with pytest.raises(OSError):
            loader.hello()
        elapsed = time.monotonic() - started
        assert ATTACH_BUDGET_S - 0.5 < elapsed < ATTACH_BUDGET_S + 1.0, elapsed
        assert loader.process.poll() is not None
    finally:
        loader._reap()
        if loader.process.poll() is None:
            loader.process.kill()
        loader.process.wait(timeout=1)
        loader.process.stdin.close()
        loader.process.stdout.close()


def test_close_remains_tight_when_the_peer_never_acknowledges(tmp_path):
    import sys
    import time
    from appio.bpf_fileio import HOOK_MASK_ALL, HELLO_RESPONSE
    from appio.file_runtime import CLOSE_BUDGET_S, _ProcessLoader

    helper = tmp_path / "stalled-close-fileio"
    obj = tmp_path / "fake-object"
    obj.touch()
    hello = HELLO_RESPONSE.pack(1, 0, HOOK_MASK_ALL, 4096, 0, 0)
    helper.write_text(
        f"#!{sys.executable}\nimport os,time\n"
        "os.read(0,8)\n"
        f"os.write(1,{hello!r})\n"
        "os.read(0,8)\n"
        "time.sleep(60)\n"
    )
    helper.chmod(0o700)
    loader = _ProcessLoader(str(helper), str(obj))
    try:
        loader.hello()
        started = time.monotonic()
        with pytest.raises(OSError):
            loader.close()
        assert time.monotonic() - started < CLOSE_BUDGET_S + 0.5
        assert loader.process.poll() is not None
    finally:
        loader._reap()
        if loader.process.poll() is None:
            loader.process.kill()
        loader.process.wait(timeout=1)
        loader.process.stdin.close()
        loader.process.stdout.close()


@pytest.mark.parametrize("relative", ["helper", "object"])
def test_helper_paths_must_be_absolute_even_when_relative_files_exist(tmp_path, monkeypatch, relative):
    import appio.file_runtime as runtime_module
    monkeypatch.chdir(tmp_path)
    (tmp_path / "helper").touch()
    (tmp_path / "object").touch()
    monkeypatch.setattr(runtime_module.subprocess, "Popen", lambda *a, **k: pytest.fail("relative helper/object was spawned"))
    executable = "helper" if relative == "helper" else str(tmp_path / "helper")
    obj = "object" if relative == "object" else str(tmp_path / "object")
    with pytest.raises(FileSourceError, match="absolute"):
        runtime_module._ProcessLoader(executable, obj)


def test_default_source_refuses_unprivileged_load_before_spawning(monkeypatch):
    import appio.file_runtime as runtime_module
    monkeypatch.setattr(runtime_module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(runtime_module, "_ProcessLoader", lambda: pytest.fail("unprivileged live BPF invocation"))
    with pytest.raises(FileSourceError) as caught:
        runtime_module.default_file_source()
    assert caught.value.errno in (1, -1)


def test_F_writer_lock_wait_is_bounded_restores_busy_timeout_and_next_T_D_tick_commits(frames):
    import time
    rt, loader = runtime(frames)
    committed = frames.tick(0)
    original = frames.db.execute("PRAGMA busy_timeout").fetchone()[0]
    path = frames.db.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    try:
        at = time.monotonic()
        with pytest.raises(FileSourceError):
            rt.consume(*committed)
        assert time.monotonic() - at < 0.5
        assert not frames.db.in_transaction
        assert frames.db.execute("PRAGMA busy_timeout").fetchone()[0] == original
    finally:
        other.rollback()
        other.close()
    frames.tick(10**10, amount=200)
    assert frames.db.execute("SELECT sum(read_bytes) FROM app_boot_total").fetchone() == (200,)
