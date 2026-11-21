"""Qt-free fixtures for the live Overview lane.

Only the standard library and the frozen P1 contracts appear here, so importing
this module never pulls in Qt, a client, a router or a query service. The lane's
later harness imports these factories to build committed-value fixtures whose
fields stay exactly the production contract fields.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
import threading

from appio.contracts import (
    AppTotal,
    Coverage,
    DeviceSeriesPoint,
    DeviceShareRow,
    DiskShare,
    HookHits,
    ResidualPoint,
    Series,
    SeriesPoint,
    Tier,
    Window,
)

__all__ = [
    "Clock",
    "ExactOnlyQuery",
    "IdentityExactOnlyQuery",
    "LiveOverviewHarness",
    "LocalPositionStores",
    "coverage",
    "series",
    "share",
    "total",
]

DEFAULT_KEY = "unit:alpha.service"

#: One device split used by :func:`share`; 9 + 5 == 14, the §12.5 device total.
_DEVICE_READ = 9
_DEVICE_WRITE = 5

#: Task bytes for the normative raw series fixture (7 + 3 == 10 per bucket).
_TASK_READ = 7
_TASK_WRITE = 3

#: Per-device bytes (2 + 1 == 3, two devices per bucket == 6 of the task 10).
_DEVICE_POINT_READ = 2
_DEVICE_POINT_WRITE = 1


@dataclass
class Clock:
    """Injected monotonic nanosecond clock: the lane's only age time source."""

    ns: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.ns, bool) or not isinstance(self.ns, int) or self.ns < 0:
            raise ValueError("clock.ns must be a nonnegative integer")

    def __call__(self) -> int:
        return self.ns

    def advance_ns(self, delta: int) -> None:
        if isinstance(delta, bool) or not isinstance(delta, int) or delta < 0:
            raise ValueError("clock.advance_ns requires a nonnegative integer delta")
        self.ns += delta


def coverage(**changes: object) -> Coverage:
    """A complete, available raw-tier Coverage, with explicit overrides."""

    base = Coverage(
        tier="raw",
        complete=True,
        unavailable_reason=None,
        inaccessible_pids=0,
        resets=0,
        reap_corrections=0,
        reap_unapplied=(0, 0, 0, 0),
        reap_unobserved=(0, 0, 0, 0),
        partial_intervals=0,
        gap_intervals=0,
        overlap_uncertain_intervals=0,
        spans_suspend_intervals=0,
        spans_suspend=False,
        reap_incomplete=False,
        counter_overflow=False,
        device_state="available",
        counted_omitted_files=0,
        counted_omitted_bytes_r=0,
        counted_omitted_bytes_w=0,
        uncounted_overflow_files=0,
        uncounted_overflow_bytes_r=0,
        uncounted_overflow_bytes_w=0,
        skipped_nonfile=0,
        skipped_unsupported_fs=0,
        weak_identity_rows=0,
        hook_hits=HookHits(0, 0, 0, 0, 0, 0),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def total(
    app_id: int,
    key: str,
    *,
    read: int = 17,
    write: int = 5,
    peak: int | None = None,
) -> AppTotal:
    """One ranking row whose subject matches ``key`` exactly."""

    return AppTotal(
        app_id=app_id,
        app_key=key,
        name=key.partition(":")[2] or key,
        read_bytes=read,
        write_bytes=write,
        total_bytes=read + write,
        rchar=read,
        wchar=write,
        peak_read_bps=peak,
        peak_write_bps=peak,
        peak_total_bps=peak,
        coverage=coverage(),
    )


def share(
    app_id: int,
    window: Window,
    *,
    read: int = 17,
    write: int = 5,
    device: bool = True,
) -> DiskShare:
    """A task/device split with valid matching device rows and residuals."""

    if not isinstance(window, Window):
        raise ValueError("share requires a Window")
    boot = window.boot_id if window.boot_id is not None else "boot-a"
    if device:
        devices = (
            DeviceShareRow(
                boot_id=boot,
                dev="8:0",
                rbytes=_DEVICE_READ,
                wbytes=_DEVICE_WRITE,
                total_bytes=_DEVICE_READ + _DEVICE_WRITE,
            ),
        )
        device_read: int | None = _DEVICE_READ
        device_write: int | None = _DEVICE_WRITE
        device_total: int | None = _DEVICE_READ + _DEVICE_WRITE
        residual_read: int | None = read - _DEVICE_READ
        residual_write: int | None = write - _DEVICE_WRITE
        residual_total: int | None = residual_read + residual_write
        state = "available"
    else:
        devices = ()
        device_read = device_write = device_total = None
        residual_read = residual_write = residual_total = None
        state = "not_delegated"
    return DiskShare(
        app_id=app_id,
        window=window,
        task_read_bytes=read,
        task_write_bytes=write,
        task_total_bytes=read + write,
        devices=devices,
        device_read_bytes=device_read,
        device_write_bytes=device_write,
        device_total_bytes=device_total,
        residual_read=residual_read,
        residual_write=residual_write,
        residual_total=residual_total,
        coverage=coverage(device_state=state),
    )


def series(app_id: int, window: Window, *, tier: Tier = "raw") -> Series:
    """The §5.4 normative raw fixture: two buckets, two simultaneous devices.

    Device order is ``(0, 8:0), (0, 8:16), (10s, 8:0), (10s, 8:16)``: two
    different devices share a bucket while every single device sequence keeps
    its own strict bucket order.
    """

    if not isinstance(window, Window):
        raise ValueError("series requires a Window")
    if tier == "totals_only":
        return Series(
            app_id=app_id,
            window=window,
            tier=tier,
            points=(),
            device_points=(),
            residual_points=(),
            coverage=coverage(tier=tier),
        )
    if tier not in ("raw", "rollup_60s", "rollup_3600s"):
        raise ValueError(f"unsupported series tier {tier!r}")
    secs = {"raw": 10, "rollup_60s": 60, "rollup_3600s": 3600}[tier]
    starts = (0, secs * 10**9)
    points = tuple(
        SeriesPoint(
            bucket_start_mono_ns=start,
            bucket_secs=secs,
            wall_start_seconds=1_700_000_000.0 + start / 1_000_000_000,
            read_bytes=_TASK_READ,
            write_bytes=_TASK_WRITE,
            peak_read_bps=None,
            peak_write_bps=None,
            peak_total_bps=None,
            spans_suspend=False,
        )
        for start in starts
    )
    device_points = tuple(
        DeviceSeriesPoint(
            bucket_start_mono_ns=start,
            bucket_secs=secs,
            dev=dev,
            rbytes=_DEVICE_POINT_READ,
            wbytes=_DEVICE_POINT_WRITE,
        )
        for start in starts
        for dev in ("8:0", "8:16")
    )
    device_total = 2 * (_DEVICE_POINT_READ + _DEVICE_POINT_WRITE)
    device_read = 2 * _DEVICE_POINT_READ
    device_write = 2 * _DEVICE_POINT_WRITE
    residual_points = tuple(
        ResidualPoint(
            bucket_start_mono_ns=start,
            bucket_secs=secs,
            total=(_TASK_READ + _TASK_WRITE) - device_total,
            read=_TASK_READ - device_read,
            write=_TASK_WRITE - device_write,
        )
        for start in starts
    )
    return Series(
        app_id=app_id,
        window=window,
        tier=tier,
        points=points,
        device_points=device_points,
        residual_points=residual_points,
        coverage=coverage(tier=tier),
    )


class ExactOnlyQuery:
    """A structural exact service; the real UI supplies its retention adapter.

    Each (kind, app id, window) has an independent result or exception.  No
    retained capability is implemented here, including on the identity variant.
    """

    def __init__(self, rows: list[AppTotal] | None = None) -> None:
        self.rows = list(rows) if rows is not None else [total(1, DEFAULT_KEY)]
        self.results: dict[tuple[str, int, Window], object] = {}
        self.ranking_error: BaseException | None = None
        self.calls = {name: 0 for name in ("ranking", "series", "totals", "identity")}
        self.events: list[tuple] = []

    def set_share(self, window: Window, value: DiskShare) -> None:
        self.results[("totals", value.app_id, window)] = value

    def set_series(self, window: Window, value: Series) -> None:
        self.results[("series", value.app_id, window)] = value

    def set_pending(self, kind: str, app_id: int, window: Window) -> None:
        from appio.contracts import HistoryPending

        self.results[(kind, app_id, window)] = HistoryPending("still loading")

    def set_error(self, kind: str, app_id: int, window: Window, error: BaseException) -> None:
        self.results[(kind, app_id, window)] = error

    def ranking(self, window: Window, metric: str) -> list[AppTotal]:
        self.calls["ranking"] += 1
        self.events.append(("ranking", window, metric))
        if self.ranking_error is not None:
            raise self.ranking_error
        return list(self.rows)

    def _exact(self, kind: str, app_id: int, window: Window):
        self.calls[kind] += 1
        self.events.append(("get", kind, app_id, window))
        value = self.results.get((kind, app_id, window))
        if isinstance(value, BaseException):
            raise value
        if value is None:
            return series(app_id, window) if kind == "series" else share(app_id, window)
        return value

    def disk_share(self, app_id: int, window: Window) -> DiskShare:
        return self._exact("totals", app_id, window)

    def series(self, app_id: int, window: Window) -> Series:
        return self._exact("series", app_id, window)


class IdentityExactOnlyQuery(ExactOnlyQuery):
    """The optional identity capability, with independently controlled values."""

    def __init__(self, rows: list[AppTotal] | None = None) -> None:
        super().__init__(rows)
        self.identities: dict[tuple[str, str, Window], object] = {}

    def exact_answer_identity(self, kind: str, app_key: str, window: Window) -> object:
        self.calls["identity"] += 1
        self.events.append(("identity", kind, app_key, window))
        value = self.identities.get((kind, app_key, window))
        if isinstance(value, BaseException):
            raise value
        return value


class _Barrier:
    """A one-shot worker barrier whose release is guaranteed on teardown."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()

    def wait(self, timeout: float = 5.0) -> None:
        if not self.entered.wait(timeout):
            raise TimeoutError("worker did not reach the fixture barrier")

    def release(self) -> None:
        self.released.set()

    def _pause(self) -> None:
        self.entered.set()
        if not self.released.wait(15):
            raise TimeoutError("fixture barrier was not released")


class LocalPositionStores:
    """Two actual file-backed sources and independent, committed writers.

    Managed observers are persistent source-actor connections and writers are
    independent committed SQLite connections. Worker barriers never replace an
    observer or manufacture a source position.
    """

    def __init__(self, directory=None, *, journal_modes=None) -> None:
        import os
        import sqlite3
        import tempfile
        from pathlib import Path
        from appio.query import QueryService
        from appio.store import AppIoStore

        self._temporary = tempfile.TemporaryDirectory(prefix="overview-stores-", dir=directory or os.getcwd())
        self.root = Path(self._temporary.name)
        self.paths = {name: self.root / f"{name}.db" for name in ("local", "export")}
        self.stores = {name: AppIoStore(path) for name, path in self.paths.items()}
        modes = journal_modes or {"local": "WAL", "export": "WAL"}
        for name, store in self.stores.items():
            mode = modes.get(name, "WAL")
            if mode not in ("DELETE", "WAL"):
                raise ValueError(f"unsupported fixture journal mode {mode!r}")
            store.connection.execute(f"PRAGMA journal_mode={mode}")
            store.start_boot("boot-a", 1, 0, 1000.0, "gui")
            store.connection.commit()
        self._seed_local()
        self.services = {name: QueryService(store) for name, store in self.stores.items()}
        self.writers = {
            name: sqlite3.connect(path, timeout=2, check_same_thread=False)
            for name, path in self.paths.items()
        }
        self.router = None
        self.managed_registry = None
        self._extra_connections: list[object] = []
        self._barriers: list[_Barrier] = []
        self._closed = False

    def _seed_local(self) -> None:
        from appio.handover import baseline_seed
        from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
        from appio.sampler import TickInput, aggregate_tick

        store = self.stores["local"]
        keys = (DEFAULT_KEY, "unit:beta.service")
        for index, key in enumerate(keys, 1):
            store.connection.execute(
                "INSERT INTO app VALUES (?,?,?,NULL,0,0,'user')",
                (index, key, key.partition(":")[2]),
            )
        store.connection.commit()
        store.start_collector("gui", "boot-a", 10 * 10**9)
        state = None
        for seq, second in ((0, 10), (1, 20)):
            records = tuple(
                StableProcRecord(
                    ProcessIdentity("boot-a", 410 + index, 100), 1, 1000, key,
                    "/fixture", AppKeyInputs("/fixture", key.partition(":")[2]),
                    IoReadOutcome("readable", (100 + seq * (20 - index),) * 4, None, None),
                )
                for index, key in enumerate(keys)
            )
            tick = TickInput("boot-a", "gui", 1, seq, second * 10**9,
                             (1000 + second) * 10**9, records, ())
            frame, state = baseline_seed(tick) if seq == 0 else aggregate_tick(state, tick)
            store.record_tick(frame, state)

    def managed_spec(self, name: str, *, owner: str = "iod"):
        """Describe one fixture file without sharing its SQLite connection."""
        from appio.history_types import ManagedSourceSpec

        if name not in self.paths:
            raise KeyError(name)
        return ManagedSourceSpec(
            name, owner, self.paths[name].as_uri() + "?mode=ro", ())

    def new_managed_registry(self, *, wake=None):
        """Create one registry whose actors own persistent fixture observers."""
        from appio.history_source import ManagedSourceRegistry

        if self.managed_registry is not None:
            raise RuntimeError("a managed registry is already attached")
        self.managed_registry = ManagedSourceRegistry(
            wake=(lambda: None) if wake is None else wake)
        return self.managed_registry

    def attach(self, router) -> None:
        if self.router is not None:
            raise RuntimeError("stores are already attached")
        self.router = router
        router.add_source(self.services["export"], collector_id="export", owner="gui")

    def commit(self, name: str, sql: str, parameters: tuple = ()) -> int:
        """Perform a real external update or deletion and commit it."""
        connection = self.writers[name]
        cursor = connection.execute(sql, parameters)
        connection.commit()
        return cursor.rowcount

    def replace(self, name: str = "export", path=None, *, owner: str = "gui"):
        """Install a new persistent observer through the production registry."""
        import sqlite3
        from appio.query import QueryService

        if self.router is None or name == "local":
            raise ValueError("replacement requires an attached imported source")
        connection = sqlite3.connect(path or self.paths[name])
        service = QueryService(connection)
        try:
            self.router.add_source(service, collector_id=name, owner=owner)
        except BaseException:
            connection.close()
            raise
        self._extra_connections.append(connection)
        self.services[name] = service
        return service

    @contextmanager
    def backup_complete(self, *, kind: str = "ranking"):
        """Hold a real copied outcome before enqueue and the owner closing read."""
        if self.router is None:
            raise RuntimeError("attach the stores first")
        router = self.router
        name = "_read_local" if kind == "ranking" else "_read_local_part"
        original = getattr(router, name)
        barrier = _Barrier()
        self._barriers.append(barrier)

        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            if kind == "ranking" or args[1].kind == kind:
                barrier._pause()
            return result

        setattr(router, name, wrapped)
        try:
            yield barrier
        finally:
            barrier.release()
            setattr(router, name, original)
            self._barriers.remove(barrier)

    @contextmanager
    def closing_read(self):
        """Hold the owner immediately after its real closing vector read."""
        if self.router is None:
            raise RuntimeError("attach the stores first")
        router = self.router
        original = router._closing_vector
        barrier = _Barrier()
        self._barriers.append(barrier)

        def wrapped(binding):
            result = original(binding)
            barrier._pause()
            return result

        router._closing_vector = wrapped
        try:
            yield barrier
        finally:
            barrier.release()
            router._closing_vector = original
            self._barriers.remove(barrier)

    @contextmanager
    def backup_in_progress(self):
        """Pause SQLite's real paged backup from its progress callback.

        The module-local proxy wraps only read-only worker source connections;
        registered observers and their identity are untouched.
        """
        import appio.history_router as router_module
        original = router_module.sqlite3
        barrier = _Barrier()
        self._barriers.append(barrier)

        class Source:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def backup(self, target, **kwargs):
                def progress(status, remaining, total_pages):
                    if not barrier.entered.is_set():
                        barrier._pause()
                return self.connection.backup(target, pages=1, progress=progress, **kwargs)

        class SqliteProxy:
            def __getattr__(self, name):
                return getattr(original, name)

            def connect(self, database, *args, **kwargs):
                connection = original.connect(database, *args, **kwargs)
                if isinstance(database, str) and database.endswith("?mode=ro"):
                    return Source(connection)
                return connection

        router_module.sqlite3 = SqliteProxy()
        try:
            yield barrier
        finally:
            barrier.release()
            router_module.sqlite3 = original
            self._barriers.remove(barrier)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for barrier in self._barriers:
            barrier.release()
        if self.managed_registry is not None:
            self.managed_registry.close()
        for connection in (*self.writers.values(), *self._extra_connections):
            connection.close()
        for store in self.stores.values():
            store.connection.close()
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def make_managed_router(local, client, registry, **options):
    """Inject real observer-only actors during the staged constructor cutover."""

    from pathlib import Path
    from appio.history_router import HistoryRouter
    from appio.history_source import ManagedSourceRegistry
    from appio.history_types import ManagedSourceSpec

    sources = ManagedSourceRegistry(wake=lambda: None)

    def register(name, service):
        path = service.connection.execute("PRAGMA database_list").fetchone()[2]
        spec = ManagedSourceSpec(name, "iod", Path(path).as_uri() + "?mode=ro", ())
        sources.register(spec).result(timeout=10)

    register("local", local)
    router = HistoryRouter(local, client, registry, _capture_provider=sources, **options)
    add_source = router.add_source
    close = router.close

    def add_managed_source(service, *, collector_id, owner):
        register(collector_id, service)
        add_source(service, collector_id=collector_id, owner=owner)

    def close_managed_router():
        close()
        sources.close()

    router.add_source = add_managed_source
    router.close = close_managed_router
    return router


def inject_managed_router_sources(monkeypatch):
    """Give legacy Files fixtures real source actors through the private seam."""

    from pathlib import Path
    from appio.history_router import HistoryRouter
    from appio.history_source import ManagedSourceRegistry
    from appio.history_types import ManagedSourceSpec

    initialize = HistoryRouter.__init__
    add_source = HistoryRouter.add_source
    registries = []

    def register(sources, name, service):
        path = service.connection.execute("PRAGMA database_list").fetchone()[2]
        sources.register(ManagedSourceSpec(
            name, "iod", Path(path).as_uri() + "?mode=ro", ())).result(timeout=10)

    def managed_init(router, local, client, registry, **options):
        sources = ManagedSourceRegistry(wake=lambda: None)
        registries.append(sources)
        register(sources, "local", local)
        initialize(router, local, client, registry, _capture_provider=sources, **options)

    def managed_add(router, service, *, collector_id, owner):
        register(router._capture_provider, collector_id, service)
        add_source(router, service, collector_id=collector_id, owner=owner)

    monkeypatch.setattr(HistoryRouter, "__init__", managed_init)
    monkeypatch.setattr(HistoryRouter, "add_source", managed_add)
    return registries


def wait_for_capture_admission(router, *, timeout=15.0):
    """Drive the owner until completed captures have reached real workers."""

    import time

    deadline = time.monotonic() + timeout
    while router._capture_jobs:
        router.drain()
        assert time.monotonic() < deadline, "capture admission did not finish"
        time.sleep(0.001)


def wait_for_local_frame(router, *, timeout=15.0):
    """Drain capture admission and return the actual terminal frame outcome."""

    import time

    deadline = time.monotonic() + timeout
    while True:
        router.drain()
        future = router._local_future
        captures = getattr(router, "_capture_jobs", {})
        if future is not None and future.done() and router._local_worker_id not in captures:
            return future.result(timeout=0)
        assert time.monotonic() < deadline, "local frame did not finish"
        time.sleep(0.001)


class _DeliveryQueue:
    """Hold actual queued local completions without blocking their workers."""

    def __init__(self, target) -> None:
        self.target = target
        self.held: set[str] = set()
        self.all_local = False
        self.buffer: list[tuple] = []
        self.lock = threading.Lock()
        self.entered = threading.Event()

    def put(self, item) -> None:
        token, _ = item
        with self.lock:
            if token.origin == "local" and (self.all_local or token.request.kind in self.held):
                self.buffer.append(item)
                self.entered.set()
                return
        self.target.put(item)

    def get_nowait(self):
        return self.target.get_nowait()

    def empty(self):
        with self.lock:
            return not self.buffer and self.target.empty()

    def wait(self, timeout: float = 5.0) -> None:
        if not self.entered.wait(timeout):
            raise TimeoutError("local delivery did not reach the fixture barrier")

    def release(self, kind: str | None = None) -> None:
        with self.lock:
            if kind is None:
                self.all_local = False
            else:
                self.held.discard(kind)
            ready, retained = [], []
            for item in self.buffer:
                token = item[0]
                if self.all_local or token.request.kind in self.held:
                    retained.append(item)
                else:
                    ready.append(item)
            self.buffer = retained
            for item in ready:
                self.target.put(item)


class LiveOverviewHarness:
    """Drive the real offscreen shell, router, client and SQLite workers.

    All publication uses production admission, wire decoding, owner draining and
    readiness gates. Peer responses and worker ordering are test-controlled.
    """

    _KINDS = frozenset(("ranking", "series", "totals", "files", "coverage"))

    def __init__(self, directory=None, *, qtbot=None, journal_modes=None) -> None:
        from PyQt6.QtWidgets import QApplication
        from appio.history_router import HistoryRouter
        from appio.iodclient import AppRegistry
        from appio.ui.collector_window import CollectorWindow
        from appio.collector_owner import ServiceFacts, decide_owner
        from live_overview_transport import QueryTransportHarness

        class FixtureCoordinator:
            def reconcile(self, facts):
                return decide_owner(facts)

            def tick_local(self):
                return False

            def close(self):
                pass

        class FixtureSetup:
            def refresh(self):
                return ServiceFacts(False, "inactive", False, False, False)

            def invoke(self, verb):
                from concurrent.futures import Future
                return Future()

        self.app = QApplication.instance() or QApplication([])
        self.age_clock = Clock(ns=100 * 10**9)
        self.recovery_clock = Clock(ns=100 * 10**9)
        self.stores = LocalPositionStores(directory, journal_modes=journal_modes)
        self.transport = QueryTransportHarness(clock=self.age_clock)
        self.registry = AppRegistry()
        for key in (DEFAULT_KEY, "unit:beta.service"):
            app_id = self.registry.id_for(key)
            self.transport.register_subject(app_id, key)
        self.router = make_managed_router(
            self.stores.services["local"], self.transport.client, self.registry,
            clock_ns=self.age_clock)
        self.stores.attach(self.router)
        self.events: list[tuple] = []
        self.worker_charges: list[tuple] = []
        self._futures: dict[object, object] = {}
        self._wire_query_index = len(self.transport.query_frames)
        self._held: set[str] = set()
        self.responses: dict[str, object] = {}
        self._barriers: list[_Barrier] = []
        self._closed = False
        self._delivery = _DeliveryQueue(self.router._completed)
        self.router._completed = self._delivery
        collect_frame = self.router._collect_local_frame

        def collect_when_deliverable():
            future = self.router._local_future
            with self._delivery.lock:
                held = self._delivery.all_local or "ranking" in self._delivery.held
            if held and future is not None and future.done():
                self._delivery.entered.set()
                return False
            changed = collect_frame()
            shell = getattr(self, "shell", None)
            if shell is not None and shell._pending_source is None and shell._pending is not None:
                shell._pending_source = self.router.preparation_source(shell._pending)
            return changed

        self.router._collect_local_frame = collect_when_deliverable
        self._install_spies()
        try:
            self.shell = CollectorWindow(FixtureCoordinator(), self.router, FixtureSetup(),
                                         clock_ns=self.recovery_clock, age_clock_ns=self.age_clock,
                                         pump=self._service_peer)
            self.shell._timer.stop()
            self.shell._poll_timer.stop()
            self.view = self.shell._ensure_view()
            if qtbot is not None:
                qtbot.addWidget(self.shell)
            self._spy(self.shell, "_apply_frame", "apply")
            self._spy(self.view, "refresh_measurements", "notify")
        except BaseException:
            self.close()
            raise

    def _spy(self, obj, method: str, name: str) -> None:
        original = getattr(obj, method)

        def wrapped(*args, **kwargs):
            self.events.append((name, args, dict(kwargs)))
            try:
                return original(*args, **kwargs)
            finally:
                if name in ("reserve", "reap", "submit"):
                    charges = tuple((worker_id, owner, future is not None,
                                     future.done() if future is not None else False)
                                    for worker_id, (owner, future)
                                    in self.router._worker_jobs.items())
                    self.worker_charges.append((name, charges))

        setattr(obj, method, wrapped)

    def _install_spies(self) -> None:
        for method, name in (("prepare", "prepare"), ("request_detail", "request_detail"),
                             ("ranking", "ranking"), ("series", "series"),
                             ("disk_share", "totals"), ("_settle", "settle"),
                             ("_notify", "router_notify"), ("_retire_frame_owner", "retire"),
                             ("_submit_background_worker", "submit"),
                             ("_reserve_background_worker", "reserve"),
                             ("_reap_background_worker", "reap")):
            self._spy(self.router, method, name)
        drain_peer = self.transport._drain_peer

        def observe_peer(server):
            try:
                return drain_peer(server)
            finally:
                self._record_wire_query_frames()

        self.transport._drain_peer = observe_peer
        original = self.transport.client.query

        def query(request):
            self.events.append(("query", (request,), {}))
            future = original(request)
            self._futures[future] = request
            return future

        self.transport.client.query = query

    def _record_wire_query_frames(self) -> None:
        # Only complete frames decoded from real socket bytes count as sends;
        # buffered partial writes are observed when a later peer drain completes.
        frames = self.transport.query_frames
        while self._wire_query_index < len(frames):
            frame = frames[self._wire_query_index]
            self._wire_query_index += 1
            self.events.append(("send", (frame,), {}))

    def _answer(self, request):
        from appio.contracts import FileCoverage, FileTable, UnknownOwnerOverflow

        configured = self.responses.get(request.kind)
        if configured is not None:
            return configured(request) if callable(configured) else configured
        app_id = self.registry.id_for(request.app_key) if request.app_key else 1
        if request.kind == "ranking":
            return [total(1, DEFAULT_KEY), total(2, "unit:beta.service")]
        if request.kind == "series":
            return series(app_id, request.window)
        if request.kind == "totals":
            return share(app_id, request.window)
        cov = coverage()
        if request.kind == "coverage" and request.coverage_kind == "process":
            return cov
        overflow = UnknownOwnerOverflow(request.window, "overflow_owner_unknown", 0, 0, 0, cov)
        if request.kind == "files":
            return FileTable(app_id, request.window, (), request.sort, request.direction,
                             request.search, overflow, cov)
        if request.kind == "coverage":
            return FileCoverage(app_id, request.window, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                                cov.hook_hits, 0, 0, overflow, cov)
        raise ValueError(f"unsupported fixture response: {request.kind}")

    def _service_peer(self) -> None:
        from live_overview_transport import SNAPSHOT

        self.transport.pump()
        for future, request in tuple(self._futures.items()):
            if future.done() or request.kind in self._held:
                continue
            state = self.transport.client.query_transport(future)
            if state.request_id is None:
                continue
            self.transport._futures[state.request_id] = future
            if self.transport._job_server(state.request_id) is not self.transport.server:
                continue
            value = self._answer(request)
            if isinstance(value, BaseException):
                self.transport.error(future, "unavailable", str(value))
            else:
                self.transport.reply(future, value, snapshot=SNAPSHOT)

    def _stop_timers(self) -> None:
        self.shell._timer.stop()
        self.shell._poll_timer.stop()

    def apply(self, window: Window) -> None:
        if self.shell._pending != window:
            raise ValueError("request the window before applying its ready frame")
        before = sum(event[0] == "apply" for event in self.events)
        try:
            applied = self.shell.apply_ready_frame(
                now=self.recovery_clock(), context="ordinary")
        finally:
            self._stop_timers()
        after = sum(event[0] == "apply" for event in self.events)
        if not applied or self.shell._displayed != window or after != before + 1:
            raise RuntimeError(
                "production readiness gate did not apply the requested frame "
                f"{window!r} (returned {applied!r}, displayed={self.shell._displayed!r}, "
                f"apply events={after - before})")

    def ordinary_refresh(self) -> None:
        self.shell.refresh()
        self._stop_timers()

    def poll(self) -> None:
        self.shell._poll_history()
        self._stop_timers()

    def notify(self) -> None:
        self.view.refresh_measurements()
        self._stop_timers()

    def request(self, window: Window) -> None:
        self.shell.show_window(window)
        self._stop_timers()

    def advance_ns(self, delta: int, *, domain: str = "both") -> None:
        if domain not in ("age", "recovery", "both"):
            raise ValueError("clock domain must be age, recovery or both")
        if domain in ("age", "both"):
            self.age_clock.advance_ns(delta)
        if domain in ("recovery", "both"):
            self.recovery_clock.advance_ns(delta)

    def hold(self, kind: str) -> None:
        if kind not in self._KINDS:
            raise ValueError(f"unknown held kind {kind!r}")
        self._held.add(kind)
        with self._delivery.lock:
            self._delivery.entered.clear()
            self._delivery.held.add(kind)

    def release(self, kind: str) -> None:
        if kind not in self._KINDS:
            raise ValueError(f"unknown held kind {kind!r}")
        self._held.discard(kind)
        self._delivery.release(kind)
        self._service_peer()

    @contextmanager
    def block_detail_computation(self):
        """Pause inside the real reducer after its worker snapshots are opened."""
        from appio.history_router import HistoryRouter
        original = HistoryRouter._open_local_worker
        barrier = _Barrier()
        self._barriers.append(barrier)

        def wrapped(paths, window):
            worker, sources = original(paths, window)
            try:
                barrier._pause()
            except BaseException:
                HistoryRouter._close_local_worker(worker, sources)
                raise
            return worker, sources

        HistoryRouter._open_local_worker = staticmethod(wrapped)
        try:
            yield barrier
        finally:
            barrier.release()
            HistoryRouter._open_local_worker = staticmethod(original)
            self._barriers.remove(barrier)

    @contextmanager
    def block_local_delivery(self):
        """Hold queued local outcomes after worker computation has completed."""
        with self._delivery.lock:
            self._delivery.all_local = True
            self._delivery.entered.clear()
        try:
            yield self._delivery
        finally:
            self._delivery.release()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for barrier in (*self._barriers, *self.stores._barriers):
            barrier.release()
        self._held.clear()
        with self._delivery.lock:
            self._delivery.held.clear()
        self._delivery.release()
        shell = getattr(self, "shell", None)
        if shell is not None:
            shell.shutdown()
            shell.close()
            shell.deleteLater()
        else:
            self.router.close()
        self.router._frame_executor.shutdown(wait=True, cancel_futures=True)
        self.router._background_executor.shutdown(wait=True, cancel_futures=True)
        self.transport.client.close()
        for server in self.transport.servers:
            server.close()
        self.stores.close()
        self.app.processEvents()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
