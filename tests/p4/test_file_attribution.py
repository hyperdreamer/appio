"""Pure P4 attribution probes: synthetic drains, process records and cgroups only.

The docstrings name a wrong production mechanism that must make each probe fail.
No test needs root, a BPF load, or host process/filesystem identity information.
"""
from dataclasses import replace

import pytest

from appio.bpf_fileio import CgroupFileStats, FileDrain, FileEvent
from appio.contracts import CounterOverflowError, FILE_HOUR_NS, HookHits, SQLITE_INT_MAX
from appio.durable import Receipt, decode_file_hour_state
import appio.files as files
from appio.cgroups import CgroupObservation
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.sampler import FrameCoverage, TickInput, aggregate_tick

APP = "unit:app-demo.service"
BOOT = "boot-A"
R = Receipt("iod", 1, 0)
HITS = HookHits(0, 0, 0, 0, 0, 0)


def record(pid=1, path="/app-demo.service", *, exe="/bin/demo", desktop_id=None,
           status="readable", boot=BOOT, inputs=None):
    return StableProcRecord(
        ProcessIdentity(boot, pid, 100), 0, 1000, exe, path,
        inputs or AppKeyInputs(path, exe, desktop_id),
        IoReadOutcome(status, (0, 0, 0, 0) if status == "readable" else None,
                      None, None),
    )


def group(cgroup_id=10, path="/app-demo.service", boot=BOOT):
    return CgroupObservation(boot, cgroup_id, path, "not_delegated", ())


def event(cgroup_id=10, inode=42, *, name=b"report", **changes):
    # This is the shipped producer/adapter contract: length and bytes are capped.
    captured = name[:40]
    values = dict(cgroup_id=cgroup_id, dev="259:0", inode=inode, generation=7,
                  basename=captured, name_len=len(captured), reads=1, writes=2,
                  bytes_r=40, bytes_w=10)
    values.update(changes)
    return FileEvent(**values)


def stats(cgroup_id=10, **changes):
    values = dict(cgroup_id=cgroup_id, hook_hits=HITS, overflow_files=0,
                  overflow_bytes_r=0, overflow_bytes_w=0, skipped_nonfile=0,
                  skipped_unsupported_fs=0)
    values.update(changes)
    return CgroupFileStats(**values)


def context(*, mono=100, seq=0, boot=BOOT, owners=((10, (APP,)),), apps=(APP,)):
    return files.FileFrameContext(boot, 1, seq, mono, apps, owners)


def attribute(events=(), counters=(), *, ctx=None, provider=lambda dev: "1111-aaaa",
              receipt=R, complete=True):
    return files.attribute_file_drain(
        FileDrain(receipt, tuple(events), tuple(counters), complete),
        context() if ctx is None else ctx, files.FileIdentityResolver(provider),
    )


def add(acc, contribution, receipt=R):
    for key, rows, coverage in contribution.apps:
        for row in rows or (None,):
            acc.add_observation(receipt, key, row, coverage)
    acc.add_unknown_owner(receipt, contribution.unknown_owner)


def test_context_uses_authoritative_resolver_and_current_eligible_records():
    """Falsifier: guess owners from device rows, stale state, PID or record.exe."""
    records = (
        record(1, "/join", inputs=AppKeyInputs("/app-demo.service", "/wrong")),
        record(2, "/join", inputs=AppKeyInputs("/app-demo.service", "/other")),
        record(3, "/shared", exe="/bin/left"),
        record(4, "/shared", exe="/bin/right", status="eacces"),
        record(5, "/join", exe="/bin/gone", status="esrch"),
        record(6, "/join", exe="/bin/error", status="error"),
        record(7, "/desktop", desktop_id="demo.desktop"),
    )
    cgroups = (group(30, "/desktop"), group(20, "/shared"), group(10, "/join"),
               group(99, "/empty"))
    tick = TickInput(BOOT, "iod", 1, 0, 100, 1000, records, cgroups)
    frame, _ = aggregate_tick(None, tick)
    # Coverage, not current records alone, defines the monitored app set.
    frame = replace(frame, coverage=frame.coverage + (FrameCoverage("unit:app-prior.service", 100),))
    ctx = files.FileFrameContext.from_frame(frame, records, cgroups)
    assert (ctx.boot_id, ctx.epoch, ctx.seq, ctx.observed_mono_ns) == (BOOT, 1, 0, 100)
    assert ctx.owners_by_cgroup == (
        (10, (APP,)), (20, ("exe:/bin/left", "exe:/bin/right")),
        (30, ("desktop:demo.desktop",)), (99, ()),
    )
    assert ctx.app_keys == ("desktop:demo.desktop", "exe:/bin/left", "exe:/bin/right",
                            APP, "unit:app-prior.service")


def test_unique_shared_absent_and_reserved_owners_keep_independent_evidence():
    """Falsifier: split shared/unknown bytes, leak key 0, or repeat hooks per row."""
    hooks_app = "unit:app-hooks.service"
    zero_app = "unit:app-zero.service"
    apps = ("exe:/bin/left", "exe:/bin/right", APP, hooks_app, zero_app)
    ctx = context(apps=apps, owners=((0, (APP,)), (10, (APP,)), (11, (APP,)),
                  (20, (zero_app,)), (30, (hooks_app,)),
                  (40, ("exe:/bin/left", "exe:/bin/right"))))
    result = attribute(
        (event(), event(11), event(40, 3, reads=9, bytes_r=11, bytes_w=13),
         event(99, 4, bytes_r=17, bytes_w=19)),
        (stats(10, hook_hits=HookHits(1, 2, 3, 4, 5, 6), overflow_files=2,
               overflow_bytes_r=5, overflow_bytes_w=7, skipped_nonfile=3,
               skipped_unsupported_fs=1),
         stats(11, hook_hits=HookHits(7, 8, 9, 10, 11, 12), overflow_files=1,
               overflow_bytes_r=2, overflow_bytes_w=3, skipped_nonfile=2,
               skipped_unsupported_fs=3),
         stats(30, hook_hits=HookHits(3, 0, 0, 0, 0, 0)),
         stats(40, overflow_files=3, overflow_bytes_r=23, overflow_bytes_w=29),
         stats(99, overflow_files=5, overflow_bytes_r=31, overflow_bytes_w=37),
         stats(0, hook_hits=HookHits(99, 0, 0, 0, 0, 0), overflow_files=7,
               overflow_bytes_r=41, overflow_bytes_w=43)), ctx=ctx,
    )
    assert tuple(key for key, _, _ in result.apps) == apps
    by_app = {key: (rows, cov) for key, rows, cov in result.apps}
    rows, cov = by_app[APP]
    assert [(r.inode, r.reads, r.writes, r.bytes_r, r.bytes_w) for r in rows] == [
        (42, 1, 2, 40, 10), (42, 1, 2, 40, 10)]
    assert (cov.uncounted_overflow_files, cov.uncounted_overflow_bytes_r,
            cov.uncounted_overflow_bytes_w) == (3, 7, 10)
    assert (cov.skipped_nonfile, cov.skipped_unsupported_fs) == (5, 4)
    assert cov.hook_hits == HookHits(8, 10, 12, 14, 16, 18)
    for key in apps:
        if key != APP:
            assert by_app[key][0] == ()
        assert by_app[key][1].complete
        assert by_app[key][1].gap_intervals == 0
    assert by_app[hooks_app][1].hook_hits == HookHits(3, 0, 0, 0, 0, 0)
    assert by_app[zero_app][1] == files.ZERO_COVERAGE
    u = result.unknown_owner
    assert (u.overflow_files, u.overflow_bytes_r, u.overflow_bytes_w) == (17, 123, 141)
    assert u.complete and u.gap_intervals == 0
    # Two cgroups captured the very same identity/name; metadata is not duplicated.
    assert len(result.captured_names) == 1
    name, = result.captured_names
    assert name.path_key == ("1111-aaaa", "259:0", 42, 7, "report", 0)
    assert (name.first_mono_ns, name.last_mono_ns) == (100, 100)
    acc = files.HourlyAccumulator(BOOT, 0)
    add(acc, result)
    row, = acc.finalize().apps[2][1]
    assert (row.bytes_r, row.bytes_w, row.reads, row.writes) == (80, 20, 2, 4)
    assert acc.finalize().apps[2][2].hook_hits == HookHits(8, 10, 12, 14, 16, 18)


def test_unowned_events_do_not_lookup_file_identity_or_invent_an_app():
    """Falsifier: reuse a prior owner or assign an event using basename/dev/PID."""
    def forbidden_lookup(dev):
        raise AssertionError("unowned event must not need an identity lookup")
    result = attribute((event(99),), ctx=context(apps=(), owners=()), provider=forbidden_lookup)
    assert result.apps == () and result.captured_names == ()
    assert (result.unknown_owner.overflow_files, result.unknown_owner.overflow_bytes_r,
            result.unknown_owner.overflow_bytes_w) == (1, 40, 10)


@pytest.mark.parametrize("raw,want,state", [
    ("E\u0301-É.Txt".encode(), "E\u0301-É.Txt", "resolved"),
    (b"x" * 39, "x" * 39, "resolved"),
    (b"x" * 40, "x" * 40, "truncated"),
    (b"long" * 12, "long" * 10, "truncated"),
    (b"", None, "unresolved"),
    (b"\xffbad", None, "unresolved"),
    (b"x" * 39 + "é".encode(), None, "unresolved"),
])
def test_captured_basename_keeps_exact_utf8_and_honest_wire_cap_state(raw, want, state):
    """Falsifier: normalize/casefold, replace invalid UTF-8, or call a capped name full."""
    result = attribute((event(name=raw),))
    row, = result.apps[0][1]
    assert row.state == state
    if want is None:
        assert row.basename and "unresolved" in row.basename.lower()
    else:
        assert row.basename == want
    assert "/" not in row.basename
    assert (row.generation, row.last_seen_mono_ns) == (7, 100)
    assert result.captured_names[0].basename == row.basename
    assert result.captured_names[0].state == state


@pytest.mark.parametrize("uuid,weak", [(None, True), ("", True), ("1111-aAaa", False)])
def test_uuid_provider_is_the_only_source_of_strong_identity(uuid, weak):
    """Falsifier: fabricate a UUID from dev/inode or promote missing UUID to strong."""
    result = attribute((event(),), provider=lambda dev: uuid)
    row, = result.apps[0][1]
    assert (row.fs_uuid, row.identity_weak, row.dev, row.generation) == (
        uuid or "", weak, "259:0", 7)
    assert result.captured_names[0].identity_weak == int(weak)


def test_uuid_lookup_unavailable_is_weak_and_a_device_is_resolved_once_per_drain():
    """Falsifier: a failing lookup discards F, or relookup splits one drain's identity."""
    lookups = []
    def unavailable(dev):
        lookups.append(dev)
        if len(lookups) > 1:
            return "must-not-promote"
        raise OSError("fixture has no proved filesystem UUID")
    result = attribute((event(), event(inode=43)), provider=unavailable)
    assert [(r.fs_uuid, r.identity_weak) for r in result.apps[0][1]] == [("", True), ("", True)]
    assert lookups == ["259:0"]
    assert result.apps[0][2].complete


@pytest.mark.parametrize("uuid", [True, 17, b"uuid"])
def test_invalid_uuid_provider_result_is_rejected_not_stringified(uuid):
    """Falsifier: str(provider_result) manufactures a strong filesystem UUID."""
    with pytest.raises(ValueError, match="fs_uuid"):
        attribute((event(),), provider=lambda dev: uuid)


def test_generation_device_and_weak_to_strong_changes_survive_hour_state_roundtrip():
    """Falsifier: drop generation/dev/UUID-strength from the hourly identity."""
    acc = files.HourlyAccumulator(BOOT, 0)
    add(acc, attribute((event(), event(generation=8), event(dev="259:1")),
                       provider=lambda dev: None))
    r1 = Receipt("iod", 1, 1)
    add(acc, attribute((event(),), ctx=context(seq=1, mono=200), receipt=r1), r1)
    r2 = Receipt("iod", 1, 2)
    add(acc, attribute((event(),), ctx=context(seq=2, mono=300), receipt=r2,
                       provider=lambda dev: None), r2)
    restored = files.HourlyAccumulator.from_state(
        decode_file_hour_state(acc.state_json(), BOOT, 0))
    assert [(e.fs_uuid, e.dev, e.generation, e.identity_weak, e.bytes_r)
            for e in restored.state().apps[0].files] == [
        ("", "259:0", 7, 1, 80), ("", "259:0", 8, 1, 40),
        ("", "259:1", 7, 1, 40), ("1111-aaaa", "259:0", 7, 0, 40),
    ]
    assert restored.finalize().apps[0][2].weak_identity_rows == 3


def test_excluded_objects_are_coverage_only_not_file_observations():
    """Falsifier: synthesize file candidates from hook or skipped counters."""
    result = attribute(counters=(stats(hook_hits=HookHits(9, 8, 7, 6, 5, 4),
                       skipped_nonfile=23, skipped_unsupported_fs=17),))
    key, rows, cov = result.apps[0]
    assert key == APP and rows == () and result.captured_names == ()
    assert (cov.skipped_nonfile, cov.skipped_unsupported_fs) == (23, 17)
    assert cov.hook_hits == HookHits(9, 8, 7, 6, 5, 4) and cov.complete
    assert cov.counted_omitted_files == cov.uncounted_overflow_files == 0


def test_canonical_aliases_and_all_101_candidates_wait_for_hour_close():
    """Falsifier: take top-100 per drain, key by name, reverse name order, or lose LAST."""
    acc = files.HourlyAccumulator(BOOT, 0)
    events = tuple(event(inode=i, bytes_r=50, bytes_w=0) for i in range(100))
    add(acc, attribute((*events, event(inode=999, name=b"z-name", bytes_r=40, bytes_w=0))))
    assert len(acc.state().apps[0].files) == 101
    receipt = Receipt("iod", 1, 1)
    add(acc, attribute((event(inode=999, name=b"A-name", bytes_r=20, bytes_w=0),),
                       ctx=context(seq=1, mono=200), receipt=receipt), receipt)
    pending = acc.state().apps[0].files
    assert len(pending) == 101
    assert next(e for e in pending if e.inode == 999).aliases == ("z-name",)
    snap = acc.finalize()
    _, rows, cov = snap.apps[0]
    assert len(rows) == 100 and rows[0].inode == 999
    assert (rows[0].basename, rows[0].last_seen_mono_ns, rows[0].bytes_r) == ("A-name", 200, 60)
    assert rows[-1].inode == 98
    assert (cov.counted_omitted_files, cov.counted_omitted_bytes_r) == (1, 50)
    names = [n for n in snap.captured_names if n.inode == 999]
    assert [(n.basename, n.first_mono_ns, n.last_mono_ns) for n in names] == [
        ("A-name", 200, 200), ("z-name", 100, 100)]


@pytest.mark.parametrize("receipt", [Receipt("iod", 2, 0), Receipt("iod", 1, 1)])
def test_attribution_refuses_a_drain_from_a_different_committed_frame(receipt):
    """Falsifier: attribute an old/new drain with the current cgroup-owner mapping."""
    with pytest.raises(ValueError, match="receipt"):
        attribute((event(),), receipt=receipt)


def test_cgroup_stats_checked_sum_cannot_wrap_or_coerce_to_real():
    """Falsifier: sum counters unchecked when two uniquely owned cgroups merge."""
    with pytest.raises(CounterOverflowError):
        attribute(counters=(stats(10, overflow_bytes_r=SQLITE_INT_MAX),
                            stats(11, overflow_bytes_r=1)),
                  ctx=context(owners=((10, (APP,)), (11, (APP,)))))


def test_hour_boundary_keeps_old_and_new_boot_scoped_hours_separate():
    """Falsifier: include the right endpoint in the old hour or compare boots' mono."""
    old = files.HourlyAccumulator(BOOT, 0)
    first = attribute((event(),), ctx=context(mono=FILE_HOUR_NS - 1))
    add(old, first)
    at_boundary = attribute((event(),), ctx=context(mono=FILE_HOUR_NS))
    before = old.state_json()
    with pytest.raises(ValueError, match="half-open"):
        add(old, at_boundary)
    assert old.state_json() == before
    new_hour = files.HourlyAccumulator(BOOT, FILE_HOUR_NS)
    add(new_hour, at_boundary)
    new_boot = files.HourlyAccumulator("boot-B", 0)
    add(new_boot, attribute((event(),), ctx=context(boot="boot-B", mono=1)))
    assert [(a.state().boot_id, a.state().hour_start_mono_ns,
             a.finalize().apps[0][1][0].last_seen_mono_ns) for a in (old, new_hour, new_boot)] == [
        (BOOT, 0, FILE_HOUR_NS - 1), (BOOT, FILE_HOUR_NS, FILE_HOUR_NS), ("boot-B", 0, 1)]


def test_duplicate_display_marker_metadata_never_promotes_unresolved_to_resolved():
    """Falsifier: last capture overwrites a less informative same-name state."""
    result = attribute((event(name=b"\xff"), event(name=b"[unresolved]")))
    name, = result.captured_names
    assert name.basename == "[unresolved]" and name.state == "unresolved"


def test_incomplete_drain_never_becomes_authoritative_zero_coverage():
    """Falsifier: normalize incomplete input to complete zero-event evidence."""
    result = attribute(counters=(stats(),), complete=False)
    assert result.apps[0][1] == ()
    assert not result.apps[0][2].complete and result.apps[0][2].gap_intervals == 1
    assert not result.unknown_owner.complete and result.unknown_owner.gap_intervals == 1


def file_database(path):
    from appio.migrations import open_database
    db = open_database(path)
    db.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) "
               "VALUES('boot-A',1,0,1000.0,'iod')")
    db.execute("INSERT INTO clock_map VALUES('boot-A',0,1000000000000)")
    db.execute("INSERT INTO app VALUES(1,?,'Demo',NULL,1000.0,1000.0,'user')", (APP,))
    db.commit()
    return db


def test_attributed_hour_checkpoint_and_stream_receipt_commit_or_rollback_together(tmp_path):
    """Falsifier: checkpoint commits its own transaction or cannot survive reopening."""
    from appio.file_store import FileHourStore
    from appio.migrations import open_database
    from appio.stream import FrameIdentity, accept_frame, allocate_epoch
    path = tmp_path / "atomic.sqlite"
    db = file_database(path)
    try:
        allocate_epoch(db, "iod", BOOT, 0)
        acc = files.HourlyAccumulator(BOOT, 0)
        add(acc, attribute((event(name=b"z-name"),)))
        r1 = Receipt("iod", 1, 1)
        add(acc, attribute((event(name=b"a-name"),), ctx=context(seq=1, mono=200), receipt=r1), r1)
        snapshot = acc.finalize()
        for commit in (False, True):
            db.execute("BEGIN IMMEDIATE")
            assert accept_frame(db, FrameIdentity(BOOT, "iod", 1, 1))
            FileHourStore(db).apply_pending(snapshot)
            assert db.in_transaction
            assert db.execute("SELECT last_seq FROM collector_epoch").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM file_hour_state").fetchone() == (1,)
            if commit:
                db.commit()
            else:
                db.rollback()
                assert db.execute("SELECT last_seq FROM collector_epoch").fetchone() == (None,)
                assert db.execute("SELECT count(*) FROM file_hour_state").fetchone() == (0,)
                assert db.execute("SELECT count(*) FROM path").fetchone() == (0,)
    finally:
        db.close()
    reopened = open_database(path)
    try:
        assert reopened.execute("SELECT last_seq FROM collector_epoch").fetchone() == (1,)
        text, = reopened.execute("SELECT state_json FROM file_hour_state").fetchone()
        restored = files.HourlyAccumulator.from_state(decode_file_hour_state(text, BOOT, 0))
        entry, = restored.state().apps[0].files
        assert (entry.basename, entry.aliases, entry.bytes_r, entry.bytes_w,
                entry.last_seen_mono_ns) == ("a-name", ("z-name",), 80, 20, 200)
        assert restored.state().apps[0].last_receipt == r1
        assert restored.state().unknown_owner.last_receipt == r1
        assert FileHourStore(reopened).finalize_hour(restored.finalize())
        assert reopened.execute("SELECT basename,first_mono_ns,last_mono_ns FROM path "
                                "ORDER BY basename").fetchall() == [("a-name", 200, 200), ("z-name", 100, 100)]
        assert reopened.execute("SELECT basename FROM path_alias").fetchall() == [("z-name",)]
    finally:
        reopened.close()


def test_attributed_name_endpoints_use_persisted_boot_chronology_not_monotonic_max(tmp_path):
    """Falsifier: an older boot's greater mono timestamp wins path or alias LAST."""
    from appio.file_store import FileHourStore
    db = file_database(tmp_path / "boots.sqlite")
    try:
        db.execute("INSERT INTO boot(boot_id,boot_order,started_mono_ns,started_wall,source) "
                   "VALUES('boot-B',2,0,1.0,'iod')")  # Wall order deliberately contradicts boot order.
        db.execute("INSERT INTO clock_map VALUES('boot-B',0,1000000000)")
        db.commit()
        for boot, times in (("boot-B", (5, 10)), (BOOT, (800, 900))):
            acc = files.HourlyAccumulator(boot, 0)
            for seq, (name, mono) in enumerate(zip((b"a-name", b"z-name"), times)):
                receipt = Receipt("iod", 1, seq)
                add(acc, attribute((event(name=name),),
                    ctx=context(boot=boot, mono=mono, seq=seq), receipt=receipt), receipt)
            assert FileHourStore(db).finalize_hour(acc.finalize())
        assert db.execute("SELECT basename,first_boot_id,first_mono_ns,last_boot_id,last_mono_ns "
                          "FROM path ORDER BY basename").fetchall() == [
            ("a-name", BOOT, 800, "boot-B", 5), ("z-name", BOOT, 900, "boot-B", 10)]
        assert db.execute("SELECT basename,first_boot_id,first_mono_ns,last_boot_id,last_mono_ns "
                          "FROM path_alias").fetchall() == [("z-name", BOOT, 900, "boot-B", 10)]
        assert db.execute("SELECT boot_id,last_seen_mono_ns FROM file_sample "
                          "ORDER BY boot_id").fetchall() == [(BOOT, 900), ("boot-B", 10)]
    finally:
        db.close()
