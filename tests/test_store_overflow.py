from __future__ import annotations

from copy import copy
from dataclasses import replace
import sqlite3

import pytest

from appio.cgroups import DeviceDelta
from appio.contracts import CounterOverflowError, RAW_BUCKET_NS, SQLITE_INT_MAX
from appio.sampler import AggregatedDeviceDelta, aggregate_tick
from appio.store import AppIoStore
from test_store import APP, BOOT, NS, T0, T1, initialized, process, record, snapshot, tick


def unchanged_except_diagnostic(store, before, *, field=None, app_id=1, at=T1):
    after = snapshot(store.connection)
    diagnostic = after.pop("store_diagnostic")
    before = dict(before)
    before.pop("store_diagnostic")
    assert after == before, "overflow must not leave a partial row, receipt, total or checkpoint"
    assert len(diagnostic) == 1
    row = store.connection.execute("SELECT app_id,code,field,typeof(seq),typeof(ts_mono_ns) FROM store_diagnostic").fetchone()
    assert row[0] == app_id
    assert row[1] == "counter_overflow"
    if field:
        assert field in row[2]
    assert tuple(row[3:]) == ("integer", "integer")
    assert not store.connection.in_transaction
    # This is the durable input to the query lane's A21 overlay, including no raw row at the rejected time.
    start = at // RAW_BUCKET_NS * RAW_BUCKET_NS
    assert store.connection.execute("SELECT exists(SELECT 1 FROM store_diagnostic WHERE boot_id=? AND (app_id=? OR app_id IS NULL) AND ts_mono_ns>=? AND ts_mono_ns<?)", (BOOT, app_id, start, start + RAW_BUCKET_NS)).fetchone()[0] == 1


def test_out_of_range_input_counter_rolls_back_and_stores_only_representable_diagnostic():
    """Falsified if 2**63 reaches SQL as REAL/wrapped/saturated data or advances a baseline."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    frame, staged = aggregate_tick(state, tick(1, T1, [process(counters=(0, 0, 1, 0))]))
    # The producer normally validates AppDelta at construction. Deliberately bypass
    # that guard to prove the persistence boundary independently rejects bad input.
    invalid_delta = copy(frame.deltas[0])
    object.__setattr__(invalid_delta, "counters", (0, 0, SQLITE_INT_MAX + 1, 0))
    frame = replace(frame, deltas=(invalid_delta,))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError) as raised:
        store.record_tick(frame, staged)
    assert "read_bytes" in raised.value.field
    unchanged_except_diagnostic(store, before, field="read_bytes")
    assert store.sampler_state is state
    assert store.restore_checkpoint("gui", BOOT).processes[0].baseline == (0, 0, 0, 0)


def test_new_app_rollback_uses_stream_wide_diagnostic_not_a_dangling_local_id():
    """Falsified if the diagnostic resurrects rolled-back app metadata or violates a foreign key."""
    store = initialized()
    frame, staged = aggregate_tick(None, tick(0, T0, [process()]))
    invalid = copy(frame.deltas[0])
    object.__setattr__(invalid, "counters", (0, 0, SQLITE_INT_MAX + 1, 0))
    frame = replace(frame, deltas=(invalid,))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError):
        store.record_tick(frame, staged)
    unchanged_except_diagnostic(store, before, field="read_bytes", app_id=None, at=T0)
    assert store.connection.execute("SELECT count(*) FROM app").fetchone()[0] == 0


@pytest.mark.parametrize("second_at,target", [(110 * NS, "rollup_app"), (200 * NS, "rollup_app"), (4000 * NS, "app_boot_total")])
def test_python_integer_addition_detects_rollup_and_boot_sum_overflow(second_at, target):
    """Falsified by SQL addition coercing MAX+1 to REAL, including the independent hourly width."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process(counters=(0, 0, SQLITE_INT_MAX, 0))]))
    frame, staged = aggregate_tick(state, tick(1, second_at, [process(counters=(0, 0, SQLITE_INT_MAX, 0)), process(420, 1, 200, (0, 0, 1, 0))]))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError) as raised:
        store.record_tick(frame, staged)
    assert target in raised.value.field
    after = snapshot(store.connection)
    diagnostics = after.pop("store_diagnostic")
    old = dict(before)
    old.pop("store_diagnostic")
    assert after == old
    assert len(diagnostics) == 1
    assert store.sampler_state is state
    assert all(row[0] == "integer" for row in store.connection.execute("SELECT typeof(read_bytes) FROM rollup_app UNION ALL SELECT typeof(read_bytes) FROM app_boot_total"))


def test_lifetime_overflow_rolls_back_the_entire_new_boot_observation():
    """Falsified if a safe boot total commits before an overflowing lifetime total."""
    store = initialized()
    record(store, None, tick(0, T0, [process(counters=(0, 0, SQLITE_INT_MAX, 0))]))
    store.start_boot("boot-B", 2, 0, 2000.0, "gui")
    store.start_collector("gui", "boot-B", 0)
    frame, staged = aggregate_tick(None, tick(0, 10 * NS, [process(counters=(0, 0, 1, 0), boot="boot-B")], boot="boot-B"))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError, match="app_lifetime_total.read_bytes"):
        store.record_tick(frame, staged)
    after = snapshot(store.connection)
    assert len(after["store_diagnostic"]) == 1
    after["store_diagnostic"] = []
    assert after == before


@pytest.mark.parametrize("field", ["inaccessible_pids", "reap_unapplied"])
def test_coverage_components_overflow_instead_of_entering_json_as_big_integers(field):
    """Falsified by validating SQLite columns while allowing out-of-range JSON coverage sums."""
    store = initialized()
    first, state = aggregate_tick(None, tick(0, T0, [process()]))
    change = SQLITE_INT_MAX if field == "inaccessible_pids" else (0, 0, SQLITE_INT_MAX, 0)
    first = replace(first, coverage=(replace(first.coverage[0], **{field: change}),))
    store.record_tick(first, state)
    frame, staged = aggregate_tick(state, tick(1, T1, [process()]))
    change = 1 if field == "inaccessible_pids" else (0, 0, 1, 0)
    frame = replace(frame, coverage=(replace(frame.coverage[0], **{field: change}),))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError):
        store.record_tick(frame, staged)
    unchanged_except_diagnostic(store, before, field=field)


def test_checkpoint_baseline_overflow_does_not_escape_via_json():
    """Falsified by an unchecked baseline in a canonical-looking checkpoint string."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    frame, staged = aggregate_tick(state, tick(1, T1, [process()]))
    bad_process = replace(staged.processes[0], baseline=(0, 0, SQLITE_INT_MAX + 1, 0))
    staged = replace(staged, processes=(bad_process,))
    frame = replace(frame, checkpoint_state=staged.to_checkpoint())
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError):
        store.record_tick(frame, staged)
    unchanged_except_diagnostic(store, before, field="baseline.read_bytes")


def test_peak_overflow_rejects_even_when_byte_columns_fit():
    """Falsified by storing an exact byte value while rounding/saturating its 1-nanosecond rate."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    frame, staged = aggregate_tick(state, tick(1, T0 + 1, [process(counters=(0, 0, SQLITE_INT_MAX, 0))]))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError, match="peak"):
        store.record_tick(frame, staged)
    unchanged_except_diagnostic(store, before, field="peak", at=T0 + 1)


def test_failed_diagnostic_transaction_surfaces_its_failure_and_never_acknowledges_tick():
    """Falsified if a failed diagnostic is swallowed or the rejected observation survives."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    frame, staged = aggregate_tick(state, tick(1, T0 + 1, [process(counters=(0, 0, SQLITE_INT_MAX, 0))]))
    store.connection.execute("CREATE TEMP TRIGGER reject_diagnostic BEFORE INSERT ON store_diagnostic BEGIN SELECT RAISE(ABORT,'diagnostic persistence failed'); END")
    before = snapshot(store.connection)
    with pytest.raises(sqlite3.IntegrityError, match="diagnostic persistence failed"):
        store.record_tick(frame, staged)
    assert snapshot(store.connection) == before
    assert store.sampler_state is state


def test_overflow_recovery_is_durable_and_requires_epoch_restart_with_explicit_gap(tmp_path):
    """Falsified if the next cumulative observation silently bridges a rejected interval after reopen."""
    path = tmp_path / "overflow.sqlite"
    store = initialized(path)
    _, state = record(store, None, tick(0, T0, [process()]))
    frame, staged = aggregate_tick(state, tick(1, T0 + 1, [process(counters=(0, 0, SQLITE_INT_MAX, 0))]))
    with pytest.raises(CounterOverflowError):
        store.record_tick(frame, staged)
    store.connection.close()
    reopened = AppIoStore(path)
    later, staged = aggregate_tick(state, tick(2, T1, [process(counters=(0, 0, 20, 0))]))
    before = snapshot(reopened.connection)
    with pytest.raises(ValueError, match="recovery"):
        reopened.record_tick(later, staged)
    assert snapshot(reopened.connection) == before
    reopened.start_collector("gui", BOOT, T1)
    restored = reopened.restore_checkpoint("gui", BOOT)
    record(reopened, restored, tick(0, T1 + NS, [process(counters=(0, 0, 30, 0))], epoch=2))
    assert [tuple(row) for row in reopened.connection.execute("SELECT from_mono_ns,to_mono_ns FROM gap")] == [(T0, T1)]
    assert reopened.connection.execute("SELECT read_bytes FROM app_boot_total").fetchone()[0] == 30
    assert reopened.connection.execute("SELECT count(*) FROM store_diagnostic").fetchone()[0] == 1


@pytest.mark.parametrize("bad_counter", [True, -1])
def test_device_inputs_are_validated_before_cgroup_reduction(bad_counter):
    """Falsified if summing coerces a bool to 1 or conceals a negative constituent."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    frame, staged = aggregate_tick(state, tick(1, T1, [process()]))
    invalid = DeviceDelta(101, "259:0", 0, 0, False)
    object.__setattr__(invalid, "rbytes", bad_counter)
    frame = replace(frame, device_deltas=(AggregatedDeviceDelta(APP, invalid), AggregatedDeviceDelta(APP, DeviceDelta(102, "259:0", 1, 0, False))))
    before = snapshot(store.connection)
    with pytest.raises(ValueError):
        store.record_tick(frame, staged)
    assert snapshot(store.connection) == before
    assert store.sampler_state is state


def test_device_sum_overflow_cannot_be_hidden_in_an_app_device_row():
    """Falsified by saturating or coercing two individually representable cgroup deltas."""
    store = initialized()
    _, state = record(store, None, tick(0, T0, [process()]))
    frame, staged = aggregate_tick(state, tick(1, T1, [process()]))
    frame = replace(frame, device_deltas=tuple(AggregatedDeviceDelta(APP, DeviceDelta(group, "259:0", amount, 0, False)) for group, amount in ((101, SQLITE_INT_MAX), (102, 1))))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError, match="sample_dev.rbytes"):
        store.record_tick(frame, staged)
    unchanged_except_diagnostic(store, before, field="sample_dev.rbytes")


@pytest.mark.parametrize("second_at,target", [(110 * NS, "rollup_dev"), (200 * NS, "rollup_dev"), (4000 * NS, "app_boot_dev_total")])
def test_device_rollup_and_total_overflow_roll_back_the_whole_frame(second_at, target):
    """Falsified by checking only process counters while a device accumulator overflows."""
    store = initialized()
    first, state = aggregate_tick(None, tick(0, T0, [process()]))
    # Synthetic producer frames isolate device reduction from process byte totals.
    first = replace(first, device_deltas=(AggregatedDeviceDelta(APP, DeviceDelta(101, "259:0", SQLITE_INT_MAX, 0, False)),))
    store.record_tick(first, state)
    frame, staged = aggregate_tick(state, tick(1, second_at, [process()]))
    frame = replace(frame, device_deltas=(AggregatedDeviceDelta(APP, DeviceDelta(101, "259:0", 1, 0, False)),))
    before = snapshot(store.connection)
    with pytest.raises(CounterOverflowError, match=f"{target}.rbytes"):
        store.record_tick(frame, staged)
    unchanged_except_diagnostic(store, before, field=f"{target}.rbytes", at=second_at)
