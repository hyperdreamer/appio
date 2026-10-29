"""Owner-loop F collection, independent of the committed T/D transaction.

No second writer, timers, host identity lookup, or sampling loop lives here.
F drains only committed iod endpoints. Its transactions finish (or roll back)
before returning to the daemon; a broken F source is sticky until recovery.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import logging
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import time

from .bpf_fileio import BpfFileSource, FileSourceError, StreamLoader
from .contracts import CounterOverflowError, FILE_HOUR_NS
from .durable import Receipt, decode_file_hour_state
from .file_store import FileHourStore
from .files import (
    FileFrameContext, FileIdentityResolver, HourlyAccumulator, ZERO_COVERAGE,
    ZERO_UNKNOWN, attribute_file_drain, checked_sum,
)
from .sampler import AggregatedFrame

DIAGNOSTIC_CAP = 512
_LOG = logging.getLogger(__name__)
_F_ERRORS = (FileSourceError, OSError, sqlite3.Error, ValueError, CounterOverflowError)


def bounded_diagnostic(value: object) -> str | None:
    """Only plain bounded text may cross the administrator/UI boundary."""
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= DIAGNOSTIC_CAP else value[:DIAGNOSTIC_CAP - 1] + "…"


@dataclass(frozen=True)
class FileAvailability:
    available: bool
    bpf_loaded: bool
    reason: str | None = None
    diagnostic: str | None = None

    def __post_init__(self):
        if type(self.available) is not bool or type(self.bpf_loaded) is not bool:
            raise ValueError("file availability requires booleans")
        if self.bpf_loaded and not self.available:
            raise ValueError("an unavailable F source cannot advertise loaded BPF")
        if self.reason not in (None, "file_source_unavailable", "not_retained"):
            raise ValueError("invalid file source reason")
        object.__setattr__(self, "diagnostic", bounded_diagnostic(self.diagnostic))


class _Pipe:
    """Nonblocking binary pipe with one *command* deadline, not per-read retries."""
    def __init__(self, stream, owner, writing=False):
        self.fd, self.owner, self.writing = stream.fileno(), owner, writing
        os.set_blocking(self.fd, False)

    def _wait(self):
        left = self.owner.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("file loader command deadline expired")
        ready = select.select([] if self.writing else [self.fd],
                              [self.fd] if self.writing else [], [], left)
        if not (ready[0] or ready[1]):
            raise TimeoutError("file loader command deadline expired")

    def read(self, size):
        while True:
            self._wait()
            try:
                return os.read(self.fd, size)
            except BlockingIOError:
                continue

    def write(self, data):
        view = memoryview(data)
        while view:
            self._wait()
            try:
                sent = os.write(self.fd, view)
                if sent == 0:
                    raise BrokenPipeError("file loader pipe closed")
                view = view[sent:]
            except BlockingIOError:
                continue

    def flush(self):
        pass


class _ProcessLoader:
    """Absolute, installed helper only. No shell, builds, or unbounded pipe I/O.

    The daemon supplies ownership and privilege; StreamLoader supplies the ABI.
    HELLO/drain have a 250ms wall deadline. CLOSE/kill/reap fit within the existing
    shutdown budget (100ms command + at most 100ms kill wait), including failure.
    """
    def __init__(self, executable="/usr/lib/appio/fileio_loader", obj="/usr/lib/appio/fileio.bpf.o"):
        if not Path(executable).is_absolute() or not Path(obj).is_absolute():
            raise FileSourceError(None, None, "fileio loader/object paths must be absolute")
        if not Path(executable).is_file() or not Path(obj).is_file():
            raise FileSourceError(None, None, "installed fileio loader/object unavailable")
        self.process = subprocess.Popen([executable, obj], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.deadline = 0.0
        self._reap_attempted = False
        self.stream = StreamLoader(_Pipe(self.process.stdout, self), _Pipe(self.process.stdin, self, True))

    def _command(self, method, *args, timeout=0.25):
        self.deadline = time.monotonic() + timeout
        try:
            return method(*args)
        except OSError:
            self._reap()
            raise

    def hello(self):
        return self._command(self.stream.hello)

    def flip_drain(self, epoch, seq):
        return self._command(self.stream.flip_drain, epoch, seq)

    def _reap(self):
        if self._reap_attempted:
            return
        self._reap_attempted = True
        if self.process.poll() is None:
            self.process.kill()
        self.process.stdin.close()
        self.process.stdout.close()
        try:
            self.process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            # Killed but not yet reapable: never wait unboundedly on the owner loop.
            _LOG.warning("file_loader_reap_pending")

    def close(self):
        try:
            return self._command(self.stream.close, timeout=0.1)
        finally:
            self._reap()


def default_file_source():
    if os.geteuid() != 0:
        raise FileSourceError(-errno.EPERM, None, "file collection requires the privileged daemon")
    return BpfFileSource(_ProcessLoader())


class FileRuntime:
    def __init__(self, connection: sqlite3.Connection, *, source_factory=None, identity_resolver=None):
        self.connection = connection
        self.source_factory = default_file_source if source_factory is None else source_factory
        # No path reconstruction or unsafe dev_t-to-UUID inference. A deployment
        # may inject a proved UUID resolver; absence is explicitly weak identity.
        self.identity_resolver = identity_resolver or FileIdentityResolver(lambda dev: None)
        self.store = FileHourStore(connection)
        self._source = None
        self._source_boot = None
        self._hours = {}
        self._last = {}
        self._boot = None
        self._closed = False
        self._availability = FileAvailability(False, False, "file_source_unavailable")

    @property
    def availability(self):
        return self._availability

    @property
    def diagnostic(self):
        return self._availability.diagnostic

    def _disable(self, failure):
        diagnostic = bounded_diagnostic(str(failure))
        self._availability = FileAvailability(False, False, "file_source_unavailable", diagnostic)
        source, self._source = self._source, None
        if source is not None:
            try:
                source.close()
            except _F_ERRORS:
                pass  # Keep the original failure, not a secondary CLOSE error.

    @staticmethod
    def _incomplete(acc):
        state = acc.state()
        def gap(coverage):
            return replace(coverage, complete=0,
                           gap_intervals=checked_sum(coverage.gap_intervals, 1, field="gap_intervals"))
        return HourlyAccumulator.from_state(replace(state,
            apps=tuple(replace(app, coverage=gap(app.coverage)) for app in state.apps),
            unknown_owner=replace(state.unknown_owner, coverage=gap(state.unknown_owner.coverage))))

    def _persist(self, hours, finalize=()):
        if self.connection.in_transaction:
            raise ValueError("F requires a committed T/D frame, not a caller transaction")
        # A separate F transaction must not inherit the T/D writer's 5s lock
        # wait. Apply one wall deadline to all statements, restore connection
        # policy on every exit, and keep the rollback outside the progress hook.
        busy = self.connection.execute("PRAGMA busy_timeout").fetchone()[0]
        deadline = time.monotonic() + 0.25
        self.connection.execute(f"PRAGMA busy_timeout={min(busy, 10)}")
        self.connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for key, acc in hours.items():
                self.store.apply_pending(acc.finalize())
                if time.monotonic() >= deadline:
                    raise TimeoutError("file persistence deadline expired")
            for key in finalize:
                self.store.finalize_hour(hours[key].finalize())
                if time.monotonic() >= deadline:
                    raise TimeoutError("file persistence deadline expired")
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.set_progress_handler(None, 0)
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            # In particular, do NOT emit FileHourStore.record_overflow here:
            # generic iod diagnostics would poison the next T/D receipt.
            raise
        finally:
            self.connection.set_progress_handler(None, 0)
            self.connection.execute(f"PRAGMA busy_timeout={busy}")
        self._hours.update(hours)
        for key in finalize:
            self._hours.pop(key, None)

    def _attach(self):
        self._source = self.source_factory()
        self._source_boot = None
        status = self._source.start()
        if not (status.file_io and status.bpf_loaded):
            raise FileSourceError(None, None, status.diagnostic or "all six file hooks unavailable")
        self._availability = FileAvailability(True, True)

    def start(self):
        if self._source is not None:
            return self.availability
        self._closed = False
        try:
            # Crash/recovery tails cannot silently become complete hours. Path
            # endpoints already persisted by apply_pending survive this restore.
            restored = {}
            for boot, hour, text in self.connection.execute(
                    "SELECT f.boot_id,f.ts_mono_ns,f.state_json FROM file_hour_state f "
                    "JOIN boot b USING(boot_id) ORDER BY b.boot_order,f.ts_mono_ns"):
                acc = HourlyAccumulator.from_state(decode_file_hour_state(text, boot, hour))
                restored[(boot, hour)] = self._incomplete(acc)
                receipts = [app.last_receipt for app in acc.state().apps]
                if acc.state().unknown_owner.last_receipt:
                    receipts.append(acc.state().unknown_owner.last_receipt)
                if receipts:
                    self._last[boot] = max(self._last.get(boot, (0, -1)),
                                           max((r.epoch, r.seq) for r in receipts))
                self._boot = boot
            if restored:
                self._persist(restored)
            self._attach()
        except _F_ERRORS as failure:
            self._disable(failure)
        return self.availability

    def _validate(self, frame, context):
        if not isinstance(frame, AggregatedFrame) or not isinstance(context, FileFrameContext):
            raise ValueError("F requires a committed frame and its immutable context")
        if frame.source != "iod" or (frame.boot_id, frame.epoch, frame.seq, frame.observed_mono_ns) != (
                context.boot_id, context.epoch, context.seq, context.observed_mono_ns):
            raise ValueError("F context does not match its iod frame")
        if (frame.epoch, frame.seq) <= self._last.get(frame.boot_id, (0, -1)):
            return False
        if self.connection.in_transaction:
            raise ValueError("T/D frame is not committed")
        committed = self.connection.execute(
            "SELECT boot_id,epoch,seq,checkpoint_mono_ns FROM sampler_checkpoint WHERE source='iod'").fetchone()
        if committed != (frame.boot_id, frame.epoch, frame.seq, frame.observed_mono_ns):
            raise ValueError("F may only drain the committed iod frontier")
        if self._boot is not None and self._boot != frame.boot_id:
            orders = dict(self.connection.execute("SELECT boot_id,boot_order FROM boot"))
            if orders[frame.boot_id] <= orders[self._boot]:
                raise ValueError("F boot chronology must advance before draining")
        return True

    def _advance(self, frame, context, contribution=None):
        boot, at = frame.boot_id, frame.observed_mono_ns
        hour = at // FILE_HOUR_NS * FILE_HOUR_NS
        key = (boot, hour)
        updates, final = {}, []
        if self._boot is not None and boot != self._boot:
            orders = dict(self.connection.execute("SELECT boot_id,boot_order FROM boot"))
            if orders[boot] <= orders[self._boot]:
                raise ValueError("F boot chronology must advance")
            for old in sorted(k for k in self._hours if k[0] == self._boot):
                updates[old] = self._incomplete(self._hours[old])
                final.append(old)
        previous = self._hours.get(key)
        acc = HourlyAccumulator.from_state(previous.state()) if previous else HourlyAccumulator(boot, hour)
        receipt = Receipt("iod", frame.epoch, frame.seq)
        if contribution is None:
            keys = set(context.app_keys) | {app.app_key for app in acc.state().apps}
            for app in sorted(keys):
                acc.add_observation(receipt, app, None, replace(ZERO_COVERAGE, complete=False, gap_intervals=1))
            acc.add_unknown_owner(receipt, replace(ZERO_UNKNOWN, complete=False, gap_intervals=1))
        else:
            partial = previous is None and not any(k[0] == boot for k in self._hours) and at % FILE_HOUR_NS
            for app, rows, coverage in contribution.apps:
                if partial:
                    coverage = replace(coverage, complete=False,
                        gap_intervals=checked_sum(coverage.gap_intervals, 1, field="gap_intervals"))
                for row in rows or (None,):
                    acc.add_observation(receipt, app, row, coverage)
            unknown = contribution.unknown_owner
            if partial:
                unknown = replace(unknown, complete=False,
                    gap_intervals=checked_sum(unknown.gap_intervals, 1, field="gap_intervals"))
            acc.add_unknown_owner(receipt, unknown)
        updates[key] = acc
        for old in sorted(k for k in self._hours if k[0] == boot and k[1] < hour):
            updates[old] = self._hours[old]
            final.append(old)
        self._persist(updates, final)
        self._boot = boot
        self._last[boot] = (frame.epoch, frame.seq)

    def consume(self, frame: AggregatedFrame, context: FileFrameContext):
        validated = False
        try:
            if not self._validate(frame, context):
                return False
            validated = True
            hour = frame.observed_mono_ns // FILE_HOUR_NS * FILE_HOUR_NS
            watermark = self.connection.execute(
                "SELECT file_finalized_through_mono_ns FROM boot WHERE boot_id=?", (frame.boot_id,)).fetchone()[0]
            if hour < watermark:
                return False  # Reject replay BEFORE flipping/clearing a map.
            if self._source is not None and self._source_boot not in (None, frame.boot_id):
                # Receipt's wire tuple is boot-local. A new boot needs fresh
                # maps and an independent BpfFileSource receipt guard.
                self._source.close()
                self._source = None
                self._attach()
            if self._closed or not self.availability.available:
                raise FileSourceError(None, None, self.diagnostic or "file source unavailable")
            self._source_boot = frame.boot_id
            receipt = Receipt("iod", frame.epoch, frame.seq)
            drain = self._source.drain(receipt)
            if drain.receipt != receipt:
                raise ValueError("file drain rewrote its source/epoch/seq")
            contribution = attribute_file_drain(drain, context, self.identity_resolver)
            self._advance(frame, context, contribution)
            return True
        except _F_ERRORS as failure:
            self._disable(failure)
            if validated:
                try:
                    self._advance(frame, context)
                except _F_ERRORS:
                    # Even failure to persist F gap evidence cannot leave the
                    # shared writer in a transaction or claim measured zero.
                    _LOG.warning("file_gap_persistence_failed")
            raise FileSourceError(getattr(failure, "errno", None), None, self.diagnostic or "file source unavailable") from failure

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._hours:
                self._persist({key: self._incomplete(acc) for key, acc in self._hours.items()})
        except _F_ERRORS as failure:
            self._disable(failure)
        source, self._source = self._source, None
        if source is not None:
            try:
                source.close()
            except _F_ERRORS as failure:
                self._disable(failure)
        self._availability = FileAvailability(False, False, "file_source_unavailable", self.diagnostic)
