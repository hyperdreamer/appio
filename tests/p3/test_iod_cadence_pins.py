"""Regression pins for the shipped storage cadence and persistent-failure recovery.

Use the rootless runtime's real collector, SQLite store and socket loop. These
pins deliberately use production Daemon defaults, unlike fast behavioural vectors.
"""
import logging
import signal
import sqlite3

import pytest

import appio.iod as iod
import appio.iodclient as iodclient
from appio.ipc_models import _require_keys, validate_server_message
from appio.ipc_protocol import ProtocolError
from appio.ipc_security import Peer
from appio.quota import PruneResult, QuotaPruner, SkippedUnit
from test_iod import NS, PollSelector, pump, receive, request, runtime, total, write_io


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_raw_persistence_follows_the_documented_ten_second_cadence(runtime):
    r = runtime
    r.daemon.start()
    baseline = r.daemon.committed_cursor
    db = r.daemon.collector.store.connection
    before = db.execute("SELECT count(DISTINCT seq) FROM sample").fetchone()[0]
    client = r.connect()[0]
    # 15 responsiveness ticks span exactly three budgeted raw observations.
    for tick in range(1, 16):
        write_io(r.proc, 100 + tick)
        r.clock.advance(2)
        request(client, request_id=str(tick))
        pump(r)
        assert any(row.get("request_id") == str(tick) for row in receive(client))
        count = db.execute("SELECT count(DISTINCT seq) FROM sample").fetchone()[0]
        assert count - before == tick // 5
        assert r.daemon.committed_cursor.iod_epoch == baseline.iod_epoch
        assert r.daemon.committed_cursor.seq == tick // 5
    assert total(r.paths.database) == (600, 300, 180, 120)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_sample_deadline_is_independent_of_responsiveness_tick(runtime):
    r = runtime
    r.daemon.start()
    r.clock.advance(9)
    pump(r)
    assert r.daemon.committed_cursor.seq == 0
    write_io(r.proc, 101)
    r.clock.advance(1)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1
    assert total(r.paths.database) == (40, 20, 12, 8)
    r.clock.advance(1)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_missed_sample_boundaries_do_not_create_catch_up_frames(runtime):
    r = runtime
    r.daemon.start()
    write_io(r.proc, 106)
    r.clock.advance(26)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1
    r.clock.advance(2)
    pump(r)
    assert r.daemon.committed_cursor.seq == 1
    write_io(r.proc, 107)
    r.clock.advance(2)
    pump(r)
    assert r.daemon.committed_cursor.seq == 2
    db = r.daemon.collector.store.connection
    assert db.execute("SELECT DISTINCT seq,observed_mono_ns FROM sample ORDER BY seq").fetchall() == [
        (1, 36 * NS), (2, 40 * NS)]
    assert total(r.paths.database) == (280, 140, 84, 56)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_shipped_stall_guard_records_a_gap_after_thirty_seconds(runtime):
    r = runtime
    r.daemon.start()
    write_io(r.proc, 105)
    r.clock.advance(31)
    pump(r)
    assert (r.daemon.committed_cursor.iod_epoch, r.daemon.committed_cursor.seq) == (2, 0)
    assert total(r.paths.database) == (0, 0, 0, 0)
    with sqlite3.connect(r.paths.database) as db:
        assert db.execute("SELECT from_mono_ns,to_mono_ns FROM gap WHERE reason='collector_gap'").fetchall() == [
            (10 * NS, 41 * NS)]


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_recovery_window_clears_only_when_committed_mono_advances(runtime, monkeypatch):
    r = runtime
    r.daemon.start()
    committed = r.daemon.committed_cursor
    before = r.daemon._committed_mono
    monkeypatch.setattr(r.daemon._pruner, "expire", lambda *args:
                        PruneResult((), False, False, 4096, "retention_incomplete"))
    r.daemon._next_maintenance = r.clock()[0]
    pump(r)
    tick = r.daemon.collector.tick
    # A non-raising return alone cannot prove that the committed frame moved.
    monkeypatch.setattr(r.daemon.collector, "tick", lambda: r.daemon.collector.last_result)
    r.clock.advance(10)
    pump(r)
    assert r.daemon._committed_mono == before
    assert r.daemon.committed_cursor == committed
    assert r.daemon._next_recovery > r.clock()[0]
    monkeypatch.setattr(r.daemon.collector, "tick", tick)
    write_io(r.proc, 101)
    r.clock.advance(10)
    pump(r)
    assert r.daemon._committed_mono > before
    assert r.daemon._next_recovery == 0
    assert total(r.paths.database) == (40, 20, 12, 8)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_nonstorage_collection_failures_retry_at_each_sample_boundary(runtime, monkeypatch):
    r = runtime
    r.daemon.start()
    committed = r.daemon.committed_cursor
    tick = r.daemon.collector.tick.__func__
    calls = []

    def transient(collector):
        calls.append(r.clock()[0])
        if len(calls) <= 3:
            raise OSError("temporary process observation failure")
        return tick(collector)

    monkeypatch.setattr(type(r.daemon.collector), "tick", transient)
    for attempt in range(1, 4):
        r.clock.advance(10)
        pump(r)
        assert len(calls) == attempt
        assert r.daemon.committed_cursor == committed
        assert r.daemon.last_error == "collection_failed"
        assert r.daemon._next_recovery == 0
    r.clock.advance(10)
    pump(r)
    assert len(calls) == 4
    assert r.daemon.last_error is None
    assert r.daemon._committed_mono == r.clock()[0]
    assert r.daemon.committed_cursor.iod_epoch == committed.iod_epoch + 1
    assert total(r.paths.database) == (0, 0, 0, 0)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
@pytest.mark.parametrize("condition", ["quota_exhausted", "retention_incomplete", "maintenance_failed"])
def test_recovery_does_not_allocate_an_epoch_on_every_tick(runtime, monkeypatch, condition):
    r = runtime
    r.daemon.start()
    before = r.daemon.committed_cursor
    client = r.connect()[0]
    attempts = []
    maintaining = False
    tick = r.daemon.collector.tick.__func__

    def blocked_tick(collector):
        # Keep real epoch allocation and collector_gap side effects before the
        # failing durable observation, as LiveCollector does in production.
        db = collector.store.connection
        db.execute("CREATE TEMP TRIGGER reject_frame BEFORE INSERT ON sample_coverage "
                   "BEGIN SELECT RAISE(ABORT, 'persistent storage failure'); END")
        try:
            return tick(collector)
        finally:
            db.execute("DROP TRIGGER reject_frame")

    def expire(pruner, boot, now):
        nonlocal maintaining
        attempts.append(now)
        if condition == "maintenance_failed":
            raise sqlite3.OperationalError("persistent maintenance failure")
        maintaining = True
        return PruneResult((), condition != "retention_incomplete", False, 4096,
                           condition if condition == "retention_incomplete" else None)

    def shrink(pruner):
        nonlocal maintaining
        paused = maintaining and condition == "quota_exhausted"
        maintaining = False
        return PruneResult((), not paused, paused, 4096, "quota_exhausted" if paused else None)

    # Paused-turn pruning sees temporary headroom, but the next durable commit
    # still fails. Maintenance reasserts the persistent condition every cycle.
    # Patch the class so collector replacement retains the same storage fault.
    monkeypatch.setattr(type(r.daemon.collector), "tick", blocked_tick)
    monkeypatch.setattr(QuotaPruner, "expire", expire)
    monkeypatch.setattr(QuotaPruner, "shrink_if_needed", shrink)
    for _ in range(20):
        # Exercise the actual maintenance branch while paused. Quiet socket
        # turns between retries must not clear a recovery window without commit.
        r.daemon._quota_paused = True
        r.daemon._next_maintenance = r.clock()[0]
        r.daemon.step()
        assert r.daemon.last_error == condition
        write_io(r.proc, 105)
        r.clock.advance(4)
        request(client)
        pump(r)
        assert any(row["type"] == "status" and not row["answering"] for row in receive(client))
    with sqlite3.connect(r.paths.database) as db:
        epochs = db.execute("SELECT max(epoch) FROM collector_epoch WHERE source='iod'").fetchone()[0] - before.iod_epoch
        gaps = db.execute("SELECT count(*) FROM gap WHERE reason='collector_gap'").fetchone()[0]
    assert len(attempts) == 20
    assert 1 <= epochs <= 2, f"20 maintenance cycles allocated {epochs} epochs and {gaps} gaps"
    assert gaps <= 2
    assert r.daemon.committed_cursor == before
    assert total(r.paths.database) == (0, 0, 0, 0)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
@pytest.mark.parametrize("reason", ["batch_limit", "checkpoint_busy"])
def test_a_deferred_retention_sweep_never_reports_not_answering(runtime, monkeypatch, reason):
    """Deferred retention work is progress, not a dead collector.

    Falsifier: every incomplete sweep becomes ``retention_incomplete``, which is
    ``last_error``, which the console renders as 'daemon status reports not
    answering' and a blank window -- while samples keep committing on cadence.
    """

    r = runtime
    r.daemon.start()
    client = r.connect()[0]
    monkeypatch.setattr(
        QuotaPruner, "expire", lambda *args: PruneResult((), False, False, 4096, reason)
    )
    monkeypatch.setattr(
        QuotaPruner, "shrink_if_needed", lambda *args: PruneResult((), True, False, 4096, None)
    )
    r.daemon._next_maintenance = r.clock()[0]
    pump(r)
    assert r.daemon.last_error is None
    assert r.daemon._storage_error is None
    request(client)
    pump(r)
    assert any(row["type"] == "status" and row["answering"] for row in receive(client))


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_a_skipped_unit_still_reports_retention_incomplete(runtime, monkeypatch):
    """The deferred rule must not swallow a sweep that cannot prove its deletions."""

    r = runtime
    r.daemon.start()
    skipped = (SkippedUnit("core", ("boot-A", "gui", 1, 0, 1)),)
    monkeypatch.setattr(
        QuotaPruner,
        "expire",
        lambda *args: PruneResult((), False, False, 4096, "missing_materialization", skipped),
    )
    monkeypatch.setattr(
        QuotaPruner, "shrink_if_needed", lambda *args: PruneResult((), True, False, 4096, None)
    )
    r.daemon._next_maintenance = r.clock()[0]
    pump(r)
    assert r.daemon.last_error == "retention_incomplete"


@pytest.mark.parametrize("raw,seconds", [("2", 2), ("9", 9), ("10", 10), ("29", 29), ("30", 30)])
def test_parse_sample_seconds_acceptance_table(raw, seconds, caplog):
    with caplog.at_level(logging.WARNING, logger="appio.iod"):
        assert iod.parse_sample_seconds(raw) == seconds
        assert iod.configured_sample_ns({"APPIO_SAMPLE_SECONDS": raw}) == seconds * NS
    assert not caplog.records


@pytest.mark.parametrize(
    "raw",
    [None, 5, True, "", "0", "1", "31", "02", "+5", "5.0", " 5", "abc", "\n", "2\n", "٢", "9" * 4000],
)
def test_parse_sample_seconds_refusal_table(raw):
    assert iod.parse_sample_seconds(raw) is None


def test_parse_sample_seconds_rejects_string_subclasses():
    class StringSubclass(str):
        pass

    assert iod.parse_sample_seconds(StringSubclass("2")) is None


@pytest.mark.parametrize("raw", [5, None, False])
def test_configured_sample_ns_absent_empty_and_non_string_values(caplog, raw):
    with caplog.at_level(logging.WARNING, logger="appio.iod"):
        assert iod.configured_sample_ns({}) == 10 * NS
        assert iod.configured_sample_ns({"APPIO_SAMPLE_SECONDS": ""}) == 10 * NS
        assert not caplog.records
        assert iod.configured_sample_ns({"APPIO_SAMPLE_SECONDS": raw}) == 10 * NS
    assert [record.getMessage() for record in caplog.records] == [f"sample_period_invalid value={repr(raw)[:64]}"]


@pytest.mark.parametrize("unsafe_repr", ["raises", "raises_base_exception", "non_string", "controls"])
def test_invalid_sample_values_with_unsafe_repr_warn_once_and_fall_back(caplog, unsafe_repr):
    class RaisingRepr(str):
        def __repr__(self):
            raise ValueError("cannot represent value")

    class RaisingBaseExceptionRepr(str):
        def __repr__(self):
            raise BaseException("cannot represent value")

    class NonStringRepr(str):
        def __repr__(self):
            return None

    class ControlRepr(str):
        def __repr__(self):
            return "bad\nline\r\x1b\x85\u2028" + "x" * 100

    cases = {
        "raises": (RaisingRepr("invalid"), "<unrepresentable>"),
        "raises_base_exception": (RaisingBaseExceptionRepr("invalid"), "<unrepresentable>"),
        "non_string": (NonStringRepr("invalid"), "<unrepresentable>"),
        "controls": (ControlRepr("invalid"), (r"bad\nline\r\x1b\x85\u2028" + "x" * 100)[:64]),
    }
    raw, expected = cases[unsafe_repr]
    with caplog.at_level(logging.WARNING, logger="appio.iod"):
        assert iod.configured_sample_ns({"APPIO_SAMPLE_SECONDS": raw}) == 10 * NS
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert message == f"sample_period_invalid value={expected}"
    assert len(expected) <= 64
    assert message.isprintable()


def _run_main_on_runtime(monkeypatch, runtime):
    """Run the real startup and shutdown on the rootless temporary runtime."""
    daemon_type = iod.Daemon
    handlers = {}
    constructed = []
    started = []

    def construct(**kwargs):
        daemon = daemon_type(
            runtime.paths, clock=runtime.clock, peer_reader=runtime.daemon.peer_reader,
            group_lookup=runtime.daemon.group_lookup, filesystem=runtime.fs,
            selector=PollSelector(), query_worker=runtime.worker, **kwargs,
        )
        constructed.append((daemon, kwargs))
        return daemon

    def install_handler(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    def finish_step(daemon):
        started.append(daemon._running)
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(iod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(iod.signal, "signal", install_handler)
    monkeypatch.setattr(daemon_type, "step", finish_step)
    monkeypatch.setattr(iod, "Daemon", construct)
    result = iod.main([])
    assert started == [True]
    assert len(constructed) == 1
    return result, constructed[0]


@pytest.mark.parametrize("runtime", [{}], indirect=True)
@pytest.mark.parametrize("raw", ["0", "1", "31", "abc", "02", "+5", "5.0", " 5", "\n", "x" * 4000], ids=[
    "zero", "one", "thirty-one", "letters", "leading-zero", "signed", "decimal", "space", "control", "long",
])
def test_configured_sample_ns_invalid_values_warn_once_and_start(runtime, monkeypatch, caplog, raw):
    monkeypatch.setenv("APPIO_SAMPLE_SECONDS", raw)
    with caplog.at_level(logging.WARNING, logger="appio.iod"):
        result, (daemon, kwargs) = _run_main_on_runtime(monkeypatch, runtime)
    assert result == 0
    assert kwargs == {"sample_ns": 10 * NS, "flush_ns": 30 * NS, "maintenance_ns": 30 * NS}
    assert daemon.committed_cursor is not None
    messages = [record.getMessage() for record in caplog.records if record.getMessage().startswith("sample_period_invalid")]
    assert messages == [f"sample_period_invalid value={repr(raw)[:64]}"]
    assert "\n" not in messages[0]


@pytest.mark.parametrize("seconds,expected", [(2, 30), (10, 30), (11, 33), (30, 90)])
def test_flush_ns_for_uses_three_periods_at_two_ten_eleven_and_thirty(seconds, expected):
    assert iod.flush_ns_for(seconds * NS) == expected * NS


@pytest.mark.parametrize("seconds,expected", [(2, 15), (5, 15), (10, 30), (20, 60), (30, 60)])
def test_maintenance_ns_for_clamps_all_supported_periods(seconds, expected):
    assert iod.maintenance_ns_for(seconds * NS) == expected * NS


@pytest.mark.parametrize("function_name", ["flush_ns_for", "maintenance_ns_for"])
@pytest.mark.parametrize("value", [None, False, True, 0, -1, 1.0, "1"])
def test_derived_windows_reject_non_positive_or_non_integer_values(function_name, value):
    with pytest.raises(ValueError, match="^sample_ns must be a positive integer$"):
        getattr(iod, function_name)(value)


@pytest.mark.parametrize("value", [None, False, True, 0, -1, 1.0, "1"])
def test_maintenance_interval_rejects_non_positive_or_non_integer_values(value):
    with pytest.raises(ValueError, match="^maintenance_ns must be a positive integer$"):
        iod.Daemon(maintenance_ns=value)


@pytest.mark.parametrize("runtime", [{"sample_ns": 2 * NS}], indirect=True)
def test_status_reports_constructor_sample_ns(runtime):
    runtime.daemon.start()
    client = runtime.connect()[0]
    request(client)
    pump(runtime)
    status = next(row for row in receive(client) if row["type"] == "status")
    assert status["sample_ns"] == 2 * NS


@pytest.mark.parametrize(
    "runtime,expected",
    [({"sample_ns": 30 * NS, "flush_ns": 90 * NS}, 30 * NS),
     ({"sample_ns": 30 * NS + 1, "flush_ns": 90 * NS}, None),
     ({"sample_ns": 31 * NS, "flush_ns": 93 * NS}, None)],
    indirect=["runtime"],
)
def test_status_preserves_clients_for_longer_constructor_periods(runtime, expected):
    runtime.daemon.start()
    client = runtime.connect()[0]
    for request_id in ("first", "second"):
        request(client, request_id=request_id)
        pump(runtime)
        replies = receive(client)
        assert len(replies) == 1
        assert replies[0]["request_id"] == request_id
        assert replies[0].get("sample_ns") == expected
        assert ("sample_ns" in replies[0]) is (expected is not None)


def test_older_client_reports_unavailable_until_restarted(runtime, monkeypatch):
    # The base client is unchanged, but its old status validator permitted only
    # these eight keys. Apply that exact key gate to the real client transport.
    legacy_keys = {
        "protocol", "type", "request_id", "enabled", "answering",
        "pending_activation", "ownership_start_mono_ns", "cursor",
    }

    def validate_legacy_response(message):
        if message.get("type") == "status":
            _require_keys(message, legacy_keys, legacy_keys, "status")
        return validate_server_message(message)

    monkeypatch.setattr(iodclient, "validate_server_message", validate_legacy_response)
    runtime.daemon.start()

    def connect_client():
        sock = runtime.connect()[0]
        client = iodclient.IodClient(
            runtime.paths.socket,
            peer_reader=lambda _: Peer(1, 0, 0),
            path_verifier=lambda _: None,
            clock_ns=lambda: runtime.clock()[0],
            on_gap=lambda _: None,
            on_change=lambda: None,
            connector=lambda _: sock,
        )
        client.connect()
        pump(runtime)
        client.pump()
        return client

    original_status = runtime.daemon._status
    with monkeypatch.context() as old_server:
        old_server.setattr(
            runtime.daemon, "_status",
            lambda client, request_id: {
                key: value for key, value in original_status(client, request_id).items()
                if key != "sample_ns"
            },
        )
        client = connect_client()
        assert client.state.verified
        assert client.state.answering
        client.close()

    client = connect_client()
    assert not client.state.verified
    assert not client.state.answering
    assert client.state.last_error == "unknown_key"
    assert client._sock is None
    client.close()

    monkeypatch.setattr(iodclient, "validate_server_message", validate_server_message)
    restarted = connect_client()
    assert restarted.state.verified
    assert restarted.state.answering
    assert restarted.state.last_error is None
    restarted.close()


@pytest.mark.parametrize("runtime", [{}], indirect=True)
@pytest.mark.parametrize("seconds,flush,maintenance", [(2, 30, 15), (30, 90, 60)])
def test_production_main_passes_valid_period_and_derived_windows(runtime, monkeypatch, seconds, flush, maintenance):
    monkeypatch.setenv("APPIO_SAMPLE_SECONDS", str(seconds))
    result, (daemon, kwargs) = _run_main_on_runtime(monkeypatch, runtime)
    assert result == 0
    assert daemon.committed_cursor is not None
    assert kwargs == {"sample_ns": seconds * NS, "flush_ns": flush * NS, "maintenance_ns": maintenance * NS}


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_production_main_uses_ten_second_default_when_environment_is_absent(runtime, monkeypatch, caplog):
    monkeypatch.delenv("APPIO_SAMPLE_SECONDS", raising=False)
    with caplog.at_level(logging.WARNING, logger="appio.iod"):
        result, (_, kwargs) = _run_main_on_runtime(monkeypatch, runtime)
    assert result == 0
    assert kwargs == {"sample_ns": 10 * NS, "flush_ns": 30 * NS, "maintenance_ns": 30 * NS}
    assert not any(record.getMessage().startswith("sample_period_invalid") for record in caplog.records)


@pytest.mark.parametrize("runtime", [{}], indirect=True)
def test_main_empty_environment_starts_with_default_period(runtime, monkeypatch, caplog):
    monkeypatch.setenv("APPIO_SAMPLE_SECONDS", "")
    with caplog.at_level(logging.WARNING, logger="appio.iod"):
        result, (daemon, kwargs) = _run_main_on_runtime(monkeypatch, runtime)
    assert result == 0
    assert daemon.committed_cursor is not None
    assert kwargs == {"sample_ns": 10 * NS, "flush_ns": 30 * NS, "maintenance_ns": 30 * NS}
    assert not any(record.getMessage().startswith("sample_period_invalid") for record in caplog.records)


def test_status_validator_accepts_period_and_older_status():
    class IntegerSubclass(int):
        pass

    status = {
        "protocol": 1, "type": "status", "request_id": "r", "enabled": True,
        "answering": True, "pending_activation": False,
        "ownership_start_mono_ns": None, "cursor": None,
    }
    assert validate_server_message(status).get("sample_ns") is None
    for value in (1, 2 * NS, 30 * NS):
        assert validate_server_message({**status, "sample_ns": value})["sample_ns"] == value
    for value in (None, True, False, 1.0, "2", 0, -1, 30 * NS + 1, 2**63, IntegerSubclass(1)):
        with pytest.raises(ProtocolError) as error:
            validate_server_message({**status, "sample_ns": value})
        assert error.value.reason == "invalid_field"
    with pytest.raises(ProtocolError) as error:
        validate_server_message({**status, "unexpected": 1})
    assert error.value.reason == "unknown_key"


@pytest.mark.parametrize(
    "runtime,seconds",
    [({"sample_ns": 2 * NS, "flush_ns": 30 * NS}, 2),
     ({"sample_ns": 30 * NS, "flush_ns": 90 * NS}, 30)],
    indirect=["runtime"],
)
def test_configured_periods_persist_requested_observed_intervals(runtime, seconds):
    runtime.daemon.start()
    start = runtime.daemon._committed_mono
    for tick in (1, 2):
        write_io(runtime.proc, 100 + tick)
        runtime.clock.advance(seconds)
        pump(runtime)
    rows = runtime.daemon.collector.store.connection.execute(
        "SELECT DISTINCT seq, observed_mono_ns FROM sample ORDER BY seq"
    ).fetchall()
    assert rows == [(1, start + seconds * NS), (2, start + 2 * seconds * NS)]
    assert runtime.daemon.committed_cursor.iod_epoch == 1


@pytest.mark.parametrize("runtime", [{"sample_ns": 30 * NS, "flush_ns": 90 * NS}], indirect=True)
@pytest.mark.parametrize("elapsed,overdue", [(89, False), (90, False), (91, True)])
def test_sampling_overdue_gate_uses_derived_flush_window(runtime, monkeypatch, elapsed, overdue):
    runtime.daemon.start()
    baseline = runtime.daemon.committed_cursor
    tick = runtime.daemon.collector.tick

    def delayed_tick():
        runtime.clock.advance(elapsed)
        return tick()

    monkeypatch.setattr(runtime.daemon.collector, "tick", delayed_tick)
    runtime.daemon._sample()
    assert runtime.daemon.committed_cursor.iod_epoch == baseline.iod_epoch
    assert runtime.daemon.committed_cursor.seq == 1
    assert runtime.daemon._recover is overdue
    assert runtime.daemon.last_error == ("collection_overdue" if overdue else None)
    if overdue:
        runtime.daemon._sample()
        assert runtime.daemon.committed_cursor.iod_epoch == baseline.iod_epoch + 1
        assert runtime.daemon.committed_cursor.seq == 0
        assert runtime.daemon.last_error is None


@pytest.mark.parametrize("runtime", [{"sample_ns": 30 * NS, "flush_ns": 90 * NS}], indirect=True)
@pytest.mark.parametrize("elapsed,overdue", [(89, False), (90, False), (91, True)])
def test_step_overdue_gate_uses_derived_flush_window(runtime, elapsed, overdue):
    runtime.daemon.start()
    baseline = runtime.daemon.committed_cursor
    write_io(runtime.proc, 101)
    runtime.clock.advance(elapsed)
    runtime.daemon.step()
    assert runtime.daemon.committed_cursor.iod_epoch == baseline.iod_epoch + int(overdue)
    assert runtime.daemon.committed_cursor.seq == (0 if overdue else 1)
    assert total(runtime.paths.database) == ((0, 0, 0, 0) if overdue else (40, 20, 12, 8))


@pytest.mark.parametrize("runtime", [{"sample_ns": 30 * NS, "flush_ns": 90 * NS}], indirect=True)
@pytest.mark.parametrize("elapsed,overdue", [(89, False), (90, False), (91, True)])
def test_shutdown_recovery_gate_uses_derived_flush_window(runtime, elapsed, overdue):
    runtime.daemon.start()
    baseline = runtime.daemon.committed_cursor
    write_io(runtime.proc, 101)
    runtime.clock.advance(elapsed)
    runtime.daemon.stop()
    assert runtime.daemon.committed_cursor.iod_epoch == baseline.iod_epoch + int(overdue)
    assert runtime.daemon.committed_cursor.seq == (0 if overdue else 1)
    assert total(runtime.paths.database) == ((0, 0, 0, 0) if overdue else (40, 20, 12, 8))


@pytest.mark.parametrize("runtime", [{"sample_ns": 2 * NS, "flush_ns": 30 * NS, "maintenance_ns": 15 * NS}], indirect=True)
def test_maintenance_deadlines_follow_the_instance_interval(runtime, monkeypatch):
    runtime.daemon.start()
    assert runtime.daemon._next_maintenance == runtime.clock()[0] + 15 * NS
    runtime.clock.advance(15)
    runtime.daemon.step()
    assert runtime.daemon._next_maintenance == runtime.clock()[0] + 15 * NS

    # A failed storage window that does not advance the committed endpoint must
    # defer its next recovery using the same configured maintenance interval.
    runtime.daemon._storage_error = "retention_incomplete"
    monkeypatch.setattr(runtime.daemon.collector, "tick", lambda: runtime.daemon.collector.last_result)
    runtime.daemon._sample()
    assert runtime.daemon._next_recovery == runtime.clock()[0] + 15 * NS


@pytest.mark.parametrize("runtime", [{"sample_ns": 2 * NS, "flush_ns": 30 * NS, "maintenance_ns": 15 * NS}], indirect=True)
@pytest.mark.parametrize("failure", [False, True])
def test_quota_recovery_deadlines_follow_the_instance_interval(runtime, monkeypatch, failure):
    runtime.daemon.start()
    runtime.daemon._quota_paused = True
    runtime.daemon._storage_error = "quota_exhausted"
    runtime.daemon._next_sample = runtime.clock()[0]
    runtime.daemon._next_recovery = 0

    def shrink():
        if failure:
            raise sqlite3.OperationalError("injected failure")
        return PruneResult((), False, True, 4096, "quota_exhausted")

    monkeypatch.setattr(runtime.daemon._pruner, "shrink_if_needed", shrink)
    runtime.daemon.step()
    assert runtime.daemon._next_recovery == runtime.clock()[0] + 15 * NS
    assert runtime.daemon.last_error == ("maintenance_failed" if failure else None)


@pytest.mark.parametrize("runtime", [{"sample_ns": 2 * NS, "flush_ns": 30 * NS, "maintenance_ns": 15 * NS}], indirect=True)
def test_shutdown_storage_recovery_uses_the_instance_interval(runtime, monkeypatch):
    runtime.daemon.start()
    runtime.daemon._storage_error = "retention_incomplete"
    monkeypatch.setattr(runtime.daemon.collector, "tick", lambda: runtime.daemon.collector.last_result)
    runtime.clock.advance(1)
    runtime.daemon.stop()
    assert runtime.daemon._next_recovery == runtime.clock()[0] + 15 * NS
