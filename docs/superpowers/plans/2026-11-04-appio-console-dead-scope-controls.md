# Appio Console Dead Scope Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking. Leave them unticked because
> the controller pins this document's digest.

**Goal:** Remove the never-actionable Launch offer, show Range only for a supported displayed range, and clear both detail surfaces on direct refusal without a query or stored-result change.

**Architecture:** Add the synchronous `FilesView.refuse(self, reason: str) -> None` presentation boundary and call it unconditionally from the existing direct-refusal branch. Keep the existing scope handlers, formatter predicate, unavailable rendering and standalone/embedded ownership split; the Files owner remains the only caption writer.

**Tech Stack:** Python >=3.14, PyQt6>=6.6, pytest>=8, pytest-qt>=4.4, existing contract fakes and real acceptance fixtures; no dependency changes.

**Execution:** One lane, strictly Task 1 → Task 2 → Task 3 → Task 4 → Task 5. Each of Tasks 1–4 exclusively owns one different repository file. Task 5 owns none: it runs disposable mutation checks and final gates, reports outside the checkout, and makes an empty verification commit. Temporary probes let each production-file owner complete a red/green cycle without owning the permanent test files.

### File structure and responsibility

| Task | Exclusive repository ownership | Responsibility |
| --- | --- | --- |
| 1 | `src/appio/ui/files_view.py` | Ordered refusal cleanup; initial collection evidence. |
| 2 | `src/appio/ui/main_window.py` | Launch removal, Range lifecycle, refusal/caption wiring. |
| 3 | `tests/test_ui.py` | Two renames, one extension, exactly seven new named tests and local helpers. |
| 4 | `tests/acceptance/test_p1_p2.py` | A22 acceptance rename and two control-state rows. |
| 5 | None | Mutation evidence, collection comparison, targeted and full-suite gates. |

All source ranges below describe the readable planning baseline `233ca62b78570cd46be578dbfd7eada41996d26c`. Locate later edits by their quoted method/test names after insertions shift line numbers. Authority: `docs/superpowers/specs/2026-11-04-appio-console-dead-scope-controls-spec.md`; approved design: `docs/superpowers/specs/2026-11-04-appio-console-dead-scope-controls-design.md`.

## Global Constraints

- The signed-off specification `docs/superpowers/specs/2026-11-04-appio-console-dead-scope-controls-spec.md` is authoritative. Keep it and `docs/superpowers/specs/2026-11-04-appio-console-dead-scope-controls-design.md` unmodified. Earlier A22/charts/design amendments are already recorded there; edit no earlier specification or approved design.
- Run tests with `python -m pytest` in the repository environment; set `QT_QPA_PLATFORM=offscreen` and `PYTEST_QT_API=pyqt6` for offscreen Qt runs. Preserve Python >=3.14, PyQt6>=6.6, pytest>=8 and pytest-qt>=4.4; add no dependencies.
- Never widen a refused window: retain the exact requested `Window` in `_active_window`; do not synthesize, query, select or label a supported replacement scope. `FilesView.refuse(self, reason: str) -> None` adopts no window and dispatches no query.
- Keep every A22 store outcome byte-identical: `WindowUnavailableError(reason="launch_attribution", message="Launch window disabled: device and file activity are not stored per launch.")` for every well-formed launch; `WindowUnavailableError(reason="unsupported_window", message="Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.")` for an over-long range. Preserve disabled-window precedence before identity/ID lookup in `ranking`, `series`, `disk_share`, `file_table`, `file_coverage`, and `unknown_owner_file_coverage`.
- Preserve exact strings: `Launch window disabled: device and file activity are not stored per launch.`; `Ranges longer than 365 days are disabled: retained rollups cannot represent arbitrary ranges.`; `Historical boot prefix unavailable: totals cover the full collected boot.`; `unavailable`. Keep `A22_LAUNCH_REASON`, its export, the formatter refusal branch and contracts constants; remove only its unused import from `main_window.py`.
- `MAX_RANGE_NS` remains exactly `365 * 86400 * 1_000_000_000` / `31536000000000000` ns. Equality is supported; a greater duration is refused. Do not broaden `window_disabled_reason()` to historical-prefix or operational failures.
- Do not touch daemon, wire, schema, storage, query, tier or retention code, A06 launch bookkeeping, frames, totals, source ownership, recovery/stall policy, or the separate `appio setup failed (exit 127): Error accessing /usr/bin/appio-setup` banner. Add no picker, lifetime control, Launch explanation link, new error state/label/log line, unrelated refactor or production range-entry path.
- Preserve exact widget object names in contracts/tests: `scope_since_boot`, `scope_launch`, `scope_range`, `launch_reason`, `range_reason`, `files_message`, `detail_state`. Construct no `scope_launch` or `launch_reason`; retain exactly the two scope buttons `scope_since_boot` and `scope_range`, reusing the same Range widget across transitions.
- Since boot remains visible, disabled and unchecked until a since-boot window is supplied; range/lifetime/refusals neither invent nor forget that cached window. Range starts explicitly hidden, disabled and unchecked. Keep `_scope_control()`, `_on_since_boot_clicked()` and `_on_range_clicked()` bodies and exact-window re-query/empty-console behavior unchanged.
- Every direct refusal selects Overview and calls `files_view.refuse(reason)` for either owner, even while hidden, transient or not ready. Cleanup order: retire deliveries; clear refresh demand; forget adopted/deferred scopes; clear subject and selector with saved/restored signal blocking; `_set_unavailable(None, reason)`. Add no cancellation, prepare, scope adoption, choice read, refresh or flush to `refuse()`.
- Keep one caption writer per Files owner. Standalone sets/shows the exact reason and clears/hides it before supported refresh; embedded captions remain shell-owned. Preserve `file history unavailable: <reason>`, the shell's `file activity is unavailable: <reason>` under its existing ready/displayed conditions, the static footer note, pre-frame Files hiding and shell `prepare()` refusal behavior.
- Exclusive repository ownership: Task 1 alone changes `src/appio/ui/files_view.py`; Task 2 alone changes `src/appio/ui/main_window.py`; Task 3 alone changes `tests/test_ui.py`; Task 4 alone changes `tests/acceptance/test_p1_p2.py`; Task 5 changes none. All other files, including `tests/p3/test_p3_acceptance.py`, remain byte-identical. Temporary probes/mutations belong only under their task-specific `/tmp/appio-dead-scope-task-<N>-*` roots, never in the active checkout.
- Retain the names and expectations of `test_a22_exact_messages_in_client_router_and_window`, `test_detail_tabs_are_exactly_overview_then_files`, `test_attribution_panel_keeps_rows_and_boot_qualifies_lifetime_labels`, and every other retained test. Tasks 1–2 cannot rewrite the superseded chip assertions owned by Tasks 3–4.
- Record actual full-suite collection with `python -m pytest --collect-only -q` before Task 1's production edit and again in Task 5. Task 1's assigned report must include `implementation_base_sha: str` and the observed count/output; the controller supplies this report to Task 5. Never infer passes or collected counts from the inventory; report missing evidence as not run.
- Use only the controller-assigned report path outside the checkout for evidence. Add no evidence document/probe/mutation script to the repository. Stage explicit owned paths, make each task's listed commit, and leave this plan unchanged during execution.

## Task 1: Add the ordered FilesView refusal boundary

**Implementer tier:** Standard

**Files:**

- Modify (exclusive repository ownership): `src/appio/ui/files_view.py:590-625`; insert after `retire_deliveries()` and before `refresh_wanted()`. Change no existing method body/import.
- Create (disposable probe only): `/tmp/appio-dead-scope-task-1-*/test_refuse.py`.
- Read/test only: `tests/test_ui.py`, `tests/p4/test_files_view.py`, `tests/p4/test_files_tab_scope_text.py`.
- No other repository file may be created or modified.

**Interfaces:**

- Consumes: existing `FilesView.retire_deliveries(self) -> None`, `set_scope_ready(self, ready: bool) -> None`, `set_window(self, window: Window) -> None`, `request_refresh(self) -> None`, `refresh_wanted(self) -> bool`, `_set_unavailable(self, reason: str | None, message: str) -> None`.
- Consumes: test seams `FakeFilesHistory(*, ranking_rows: tuple[AppTotal, ...] = (), local_applications: tuple[tuple[str, str, int], ...] = (), registry_ids: dict[str, int] | None = None)` and `retained_file_result(window: Window) -> FileViewResult` in `tests/test_ui.py`; pending entries are real `concurrent.futures.Future` objects.
- Produces: `FilesView.refuse(self, reason: str) -> None`, synchronous on the UI thread, performing the four ordered duties without changing admission or querying.
- Produces: initial implementation SHA and actual collection output/count in this task's assigned report for Task 5.

- [ ] **Step 1: Record collection and create the isolated failing probe**

Before editing production, run and record:

```bash
git rev-parse HEAD
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest --collect-only -q
```

Create the following temporary direct-`FilesView` probe. Its name is the specification's `test_refused_window_clears_files_and_shows_the_reason`; Task 3 supplies the complete permanent standalone-window test with that name, including Overview/caption assertions.

```bash
probe_root=$(mktemp -d /tmp/appio-dead-scope-task-1-XXXXXX)
export APPIO_TASK1_PROBE_ROOT="$probe_root"
python - <<'PY'
import os
from pathlib import Path
Path(os.environ["APPIO_TASK1_PROBE_ROOT"], "test_refuse.py").write_text('''\
import pytest
from appio.ui.files_view import FilesView
from appio.ui.formatting import A22_LAUNCH_REASON
from test_ui import FakeFilesHistory, SINCE_BOOT, app_total, retained_file_result

@pytest.mark.parametrize("signals_blocked", [False, True])
def test_refused_window_clears_files_and_shows_the_reason(qtbot, signals_blocked):
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 200)
    key = "unit:retained-only.service"
    history = FakeFilesHistory(ranking_rows=(alpha,),
        local_applications=((key, "Retained only", 40),), registry_ids={key: 99})
    view = FilesView(history)
    qtbot.addWidget(view)
    view.show()
    view.set_window(SINCE_BOOT)
    history.pending[-1].set_result(retained_file_result(SINCE_BOOT))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert view.app_selector.count() == 2
    assert all(item.text() != "unavailable" for item in view._footer_labels())
    view.request_refresh()
    assert view._outstanding
    view.set_scope_ready(False)
    view.set_window(SINCE_BOOT)
    assert view._deferred_window is SINCE_BOOT
    queries = (len(history.ranking_calls), len(history.choice_calls), len(history.requests))
    selections, subjects = [], []
    view.app_selector.currentIndexChanged.connect(selections.append)
    view.subject_changed.connect(subjects.append)
    view.app_selector.blockSignals(signals_blocked)
    generation = view._generation

    view.refuse(A22_LAUNCH_REASON)

    assert view._generation == generation + 1
    assert view._outstanding is False
    assert view.refresh_wanted() is False
    assert view._window is None
    assert view._deferred_window is None
    assert view._app_id is None
    assert view._choices_loaded is False
    assert view.app_selector.count() == 0
    assert view.app_selector.currentIndex() == -1
    assert view.app_selector.isHidden()
    assert view.app_selector.signalsBlocked() is signals_blocked
    assert selections == subjects == []
    assert view.last_result is None and view.model.rowCount() == 0
    assert [item.text() for item in view._footer_labels()] == ["unavailable"] * 8
    assert view.state == "unavailable"
    assert view.unavailable_text() == A22_LAUNCH_REASON
    assert view.state_label.text() == f"file history unavailable: {A22_LAUNCH_REASON}"
    assert view.state_label.isVisible()
    assert view._scope_ready is False
    assert queries == (len(history.ranking_calls), len(history.choice_calls), len(history.requests))
''')
PY
```

- [ ] **Step 2: Run the probe and observe the missing API**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 PYTHONPATH="$PWD/src:$PWD/tests" python -m pytest -c "$PWD/pyproject.toml" "$probe_root/test_refuse.py" -q
```

Expected red: `view.refuse(...)` raises `AttributeError` because the method is absent. An import/collection failure is a probe defect, not the expected red.

- [ ] **Step 3: Insert the exact public method**

```python
    def refuse(self, reason: str) -> None:
        """Clear a refused scope without adopting it or issuing a query."""

        self.retire_deliveries()
        self._refresh_wanted = False
        self._window = None
        self._deferred_window = None
        blocked = self.app_selector.blockSignals(True)
        try:
            self._app_id = None
            self._choices_loaded = False
            self.app_selector.clear()
            self.app_selector.setVisible(False)
        finally:
            self.app_selector.blockSignals(blocked)
        self._set_unavailable(None, reason)
```

Add no coercion, validation, closed/transient/readiness guard or manual subject notification. Retirement precedes clearing refresh demand because it can re-arm that flag. `_set_unavailable(None, reason)` remains last, retaining normal state notification, footer clearing and prefixed state text.

- [ ] **Step 4: Verify the new boundary and existing Files contracts**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 PYTHONPATH="$PWD/src:$PWD/tests" python -m pytest -c "$PWD/pyproject.toml" "$probe_root/test_refuse.py" -q
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/p4/test_files_view.py tests/p4/test_files_tab_scope_text.py
```

Expected: both pass with no failures; record actual counts. Delete only the task-specific probe directory after recording results in the assigned report.

- [ ] **Step 5: Commit**

```bash
git add src/appio/ui/files_view.py
git commit -m "feat(ui): clear Files presentation on direct scope refusal"
```

## Task 2: Remove Launch and wire the scope-control lifecycle

**Implementer tier:** Standard

**Files:**

- Modify (exclusive repository ownership): `src/appio/ui/main_window.py:44-52`, `src/appio/ui/main_window.py:397-417`, `src/appio/ui/main_window.py:593-627`.
- Create (disposable probe only): `/tmp/appio-dead-scope-task-2-*/test_scope_controls.py`.
- Read/test only: `tests/test_ui.py`, `tests/p3/test_p3_acceptance.py`, `src/appio/ui/files_view.py`, `src/appio/ui/collector_window.py`.
- No other repository file may be created or modified.

**Interfaces:**

- Consumes: `FilesView.refuse(self, reason: str) -> None` from Task 1, which retires publication, forgets both scopes/subject, clears/hides the selector and renders the reason without querying.
- Consumes: existing `window_disabled_reason(window: Window) -> str | None`, `AppIoWindow._render_unavailable(self, reason: str) -> None`, `refresh(self) -> None`, `_sync_owned_files(self) -> None`, and `FilesView.flush_refresh(self) -> None`.
- Consumes: `FakeQueryService(totals: list[AppTotal], shares: dict[int, DiskShare], *, series: dict[int, Series] | None = None)`, `build(qtbot, totals: list[AppTotal], shares: dict[int, DiskShare] | None = None, *, series: dict[int, Series] | None = None, files_view: FilesView | None = None) -> tuple[AppIoWindow, FakeQueryService]`, `app_total(app_id: int, key: str, name: str, total_bytes: int, *, peak_bps: int | None = None) -> AppTotal`, `label(window: AppIoWindow, name: str) -> QLabel`, `listing(window: AppIoWindow) -> QListWidget`, and literal `A22_LAUNCH_TEXT` from `tests/test_ui.py`.
- Produces: unchanged `AppIoWindow.set_window(self, window: Window) -> None` signature with the six-state scope row, unconditional direct-refusal cleanup, Overview selection and owner-only caption set/clear. Both click-handler bodies and `_scope_control()` stay unchanged.

- [ ] **Step 1: Create the scope-control probe before editing the window**

```bash
probe_root=$(mktemp -d /tmp/appio-dead-scope-task-2-XXXXXX)
export APPIO_TASK2_PROBE_ROOT="$probe_root"
python - <<'PY'
import os
from pathlib import Path
Path(os.environ["APPIO_TASK2_PROBE_ROOT"], "test_scope_controls.py").write_text('''\
from PyQt6.QtWidgets import QAbstractButton, QLabel
from appio.contracts import Window
from appio.ui.main_window import AppIoWindow
from test_ui import A22_LAUNCH_TEXT, BOOT, FakeQueryService, app_total, build, label, listing


def test_default_console_offers_only_since_boot(qtbot):
    service = FakeQueryService([], {})
    window = AppIoWindow(service)
    qtbot.addWidget(window)
    window.show()
    boot = window.findChild(QAbstractButton, "scope_since_boot")
    control = window.findChild(QAbstractButton, "scope_range")
    assert boot is not None and boot.isVisible()
    assert not boot.isEnabled() and not boot.isChecked()
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert control is not None and control.isHidden()
    assert not control.isEnabled() and not control.isChecked()
    window._on_since_boot_clicked()
    window._on_range_clicked()
    assert window._active_window is window._since_boot is None
    assert window.has_frame is False
    assert service.ranking_calls == service.series_calls == service.disk_share_calls == []


def test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text(qtbot):
    window, service = build(qtbot, [app_total(1, "unit:/alpha.service", "Alpha", 100)])
    calls = (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    window.detail_tabs.setCurrentIndex(1)
    requested = Window(kind="launch", boot_id=BOOT, launch_id=1)
    window.set_window(requested)
    assert window._active_window is requested
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert window.detail_tabs.currentIndex() == 0
    assert label(window, "detail_state").isVisible()
    assert label(window, "detail_state").text() == A22_LAUNCH_TEXT
    assert listing(window).count() == 0
    for name in ("total_value", "read_value", "write_value", "peak_value", "device_value"):
        assert label(window, name).text() == "unavailable"
    assert window.files_view.state == "unavailable"
    assert window.files_view.unavailable_text() == A22_LAUNCH_TEXT
    assert window.files_message.text() == A22_LAUNCH_TEXT
    assert not window.files_message.isHidden()
    assert calls == (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
''')
PY
```

- [ ] **Step 2: Run the probe and observe the old offering behavior**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 PYTHONPATH="$PWD/src:$PWD/tests" python -m pytest -c "$PWD/pyproject.toml" "$probe_root/test_scope_controls.py" -q
```

Expected red: Launch absence/empty-console Range state assertions fail against the old controls. An import/collection error is not this red.

- [ ] **Step 3: Apply exact construction edits and replace `set_window()`**

Remove only `A22_LAUNCH_REASON` from the formatting import. Replace scope construction from the first `_since_boot_control` assignment through the insertion loop with:

```python
        self._since_boot_control = self._scope_control("Since boot", "scope_since_boot")
        self._range_control = self._scope_control("Range", "scope_range")
        self._since_boot_control.setEnabled(False)
        self._range_control.setVisible(False)
        self._range_control.setEnabled(False)
        self._range_control.setChecked(False)
        for control in (self._since_boot_control, self._range_control):
            scope_row.addWidget(control)
```

Keep the following stretch/layout insertion. Delete the entire `_launch_reason` label construction/object-name/word-wrap/layout-insertion block. No `_launch_control` or `_launch_reason` member remains. Keep the existing `range_reason` label and signal connections.

Replace the complete `set_window()` method with:

```python
    def set_window(self, window: Window) -> None:
        """Render a requested window without widening or silently replacing it."""

        reason = window_disabled_reason(window)
        if window.kind == "since_boot":
            self._since_boot = window
            self._since_boot_control.setEnabled(True)
        self._active_window = window
        self._disabled_reason = reason
        self._since_boot_control.setChecked(
            reason is None and window.kind == "since_boot"
        )
        supported_range = reason is None and window.kind == "range"
        over_long_range = reason == A22_RANGE_REASON
        self._range_control.setVisible(supported_range)
        self._range_control.setEnabled(supported_range)
        self._range_control.setChecked(supported_range)
        self._range_reason.setText(A22_RANGE_REASON if over_long_range else "")
        self._range_reason.setVisible(over_long_range)
        if over_long_range:
            self._range_control.setAccessibleDescription(A22_RANGE_REASON)
            self._range_control.setToolTip(A22_RANGE_REASON)
        else:
            self._range_control.setAccessibleDescription("")
            self._range_control.setToolTip("")
        if reason is not None:
            self._render_unavailable(reason)
            self.detail_tabs.setCurrentIndex(0)
            self.files_view.refuse(reason)
            if self._owns_files_scope:
                self.files_message.setText(reason)
                self.files_message.setVisible(True)
            return
        if self._owns_files_scope:
            self.files_message.setText("")
            self.files_message.setVisible(False)
        self.refresh()
        if self._owns_files_scope:
            self._sync_owned_files()
```

Supported continuation adds no tab selection. Do not edit `_sync_owned_files()`, either click handler, unavailable renderers or `collector_window.py`.

- [ ] **Step 4: Verify the change and compatible retained controls**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 PYTHONPATH="$PWD/src:$PWD/tests" python -m pytest -c "$PWD/pyproject.toml" "$probe_root/test_scope_controls.py" -q
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/test_ui.py -k 'not test_disabled_launch_control_uses_exact_a22_reason and not test_over_365_day_range_control_uses_exact_a22_reason'
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/p3/test_p3_acceptance.py::test_a22_exact_messages_in_client_router_and_window
```

Expected: pass with no failures. The two exclusions are precisely the obsolete chip tests owned by Task 3; exclude no other test and do not claim the whole unrun UI/acceptance suites passed. Remove the task-specific probe directory after recording results.

- [ ] **Step 5: Commit**

```bash
git add src/appio/ui/main_window.py
git commit -m "fix(ui): offer only actionable console scopes"
```

## Task 3: Pin all scope states and refusal cleanup in UI tests

**Implementer tier:** Advanced

**Files:**

- Modify (exclusive repository ownership): `tests/test_ui.py:9-85` for imports; `tests/test_ui.py:409-497` for three existing tests; insert helpers/seven tests after `retained_file_result()` at `tests/test_ui.py:1507-1532`.
- Read/test only: `src/appio/ui/main_window.py`, `src/appio/ui/files_view.py`, `src/appio/ui/collector_window.py`, `tests/p3/test_p3_acceptance.py`.
- No other repository file may be created or modified. Report production defects to the controller without editing another task's file.

**Interfaces:**

- Consumes: `AppIoWindow(query_service: QueryService, boot_id: str | None = None, end_mono_ns: int | None = None, *, files_view: FilesView | None = None)`; `set_window(self, window: Window) -> None`, `selected_app_id(self) -> int | None`, `has_frame: bool`, `detail_tabs: QTabWidget`, `files_message: QLabel`, `_on_since_boot_clicked(self) -> None`, `_on_range_clicked(self) -> None` from Task 2.
- Consumes: `FilesView.refuse(self, reason: str) -> None` from Task 1; existing `set_window(self, window: Window) -> None`, `set_scope_ready(self, ready: bool) -> None`, `request_refresh(self) -> None`, `refresh_wanted(self) -> bool`, `_on_delivered(self, payload) -> None`, `_footer_labels(self) -> tuple[QLabel, ...]`, `state: str`, `unavailable_text(self) -> str`, `app_selector: QComboBox`, `model: FileTableModel`, `_bridge.delivered: pyqtSignal(object)`.
- Consumes: production `CollectorWindow(coordinator: CollectorCoordinator, history: HistoryRouter, setup: SetupClient, *, clock_ns: Callable[[], int] | None = None, pump: Callable[[], None] | None = None, refresh_interval_ms: int = 1000)`; `view: AppIoWindow | None`, `_files_status_text(self) -> str | None`, `shutdown(self) -> None`. Stop both timers before event processing; construction alone installs no shell frame and runs no collaborator call.
- Consumes: existing `FakeQueryService(totals: list[AppTotal], shares: dict[int, DiskShare], *, series: dict[int, Series] | None = None)` and `FakeFilesHistory(*, ranking_rows: tuple[AppTotal, ...] = (), local_applications: tuple[tuple[str, str, int], ...] = (), registry_ids: dict[str, int] | None = None)` in this file; the latter exposes pending real Futures in `pending: list[Future]` and request tuples in `requests: list[tuple[object, ...]]`.
- Consumes: existing `build(qtbot, totals: list[AppTotal], shares: dict[int, DiskShare] | None = None, *, series: dict[int, Series] | None = None, files_view: FilesView | None = None) -> tuple[AppIoWindow, FakeQueryService]`, `coverage(**overrides: object) -> Coverage`, `app_total(app_id: int, key: str, name: str, total_bytes: int, *, peak_bps: int | None = None) -> AppTotal`, and `series_points(start: int = 1_000_000_000, count: int = 4, *, bucket_secs: int = 10, dev: str = "8:0", suspend_at: int | None = None) -> tuple[tuple[SeriesPoint, ...], tuple[DeviceSeriesPoint, ...]]`.
- Consumes: existing `retained_file_result(window: Window) -> FileViewResult`, `kpi_texts(window: AppIoWindow) -> list[str]`, `label(window: AppIoWindow, name: str) -> QLabel`, `listing(window: AppIoWindow) -> QListWidget`, `KPI_NAMES`, `SINCE_BOOT`, `BOOT`, `END`, `A22_LAUNCH_TEXT`, `A22_RANGE_TEXT`. The result factory's existing declaration is unannotated; its returned object is a real `FileViewResult`. Preserve all these seams' semantics.
- Produces: the R25 migrations and exactly seven R26 top-level test names below. Helpers have no `test_` prefix; queued tests use real production widgets and Futures.

- [ ] **Step 1: Add imports and replace the three existing tests**

Add `import threading` among standard-library imports; replace `from PyQt6.QtCore import QRect, Qt` with `from PyQt6.QtCore import QObject, QRect, Qt, pyqtSlot`; add `from appio.ui.collector_window import CollectorWindow` beside UI imports. Keep literal A22 strings unchanged.

Replace `test_empty_supported_range_labels_the_active_window`; rename/replace `test_disabled_launch_control_uses_exact_a22_reason` and `test_over_365_day_range_control_uses_exact_a22_reason` with these bodies. Update only their section comment to describe absent/hidden controls.

```python
def test_empty_supported_range_labels_the_active_window(qtbot) -> None:
    window, _ = build(qtbot, [])
    assert "window: since_boot" in label(window, "detail_state").text()
    control = window.findChild(QAbstractButton, "scope_range")
    assert control is not None and control.isHidden()
    assert not control.isEnabled() and not control.isChecked()
    requested = Window("range", BOOT, 0, MAX_RANGE_NS)
    window.set_window(requested)
    detail = label(window, "detail_state").text()
    assert detail.startswith("window: range")
    assert "since boot" not in detail
    assert window._active_window is requested
    assert window.findChild(QAbstractButton, "scope_range") is control
    assert control.isVisible() and control.isEnabled() and control.isChecked()
    assert label(window, "range_reason").text() == ""
    assert label(window, "range_reason").isHidden()
    assert control.toolTip() == control.accessibleDescription() == ""


def test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text(qtbot) -> None:
    total = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    window, service = build(qtbot, [total], series={1: series})
    assert listing(window).count() == 1
    assert window._total_chart.segments()
    assert window._donut.shares() is not None
    assert window._share_bars.fractions()
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert A22_LAUNCH_REASON == A22_LAUNCH_TEXT
    before = (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    requested = Window("launch", BOOT, launch_id=1)
    window.set_window(requested)
    assert window._active_window is requested
    assert before == (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert label(window, "range_reason").text() == ""
    assert label(window, "range_reason").isHidden()


def test_over_365_day_range_shows_the_reason_and_hides_the_control(qtbot) -> None:
    total = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)
    task, devices = series_points()
    series = Series(1, SINCE_BOOT, "raw", task, devices, (), coverage())
    window, service = build(qtbot, [total], series={1: series})
    assert listing(window).count() == 1
    assert window._total_chart.segments()
    assert window._donut.shares() is not None
    assert window._share_bars.fractions()
    before = (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    requested = Window("range", BOOT, 0, MAX_RANGE_NS + 1)
    window.set_window(requested)
    control = window.findChild(QAbstractButton, "scope_range")
    assert control is not None
    assert control.isHidden() and not control.isVisible()
    assert not control.isEnabled() and not control.isChecked()
    assert A22_RANGE_REASON == A22_RANGE_TEXT
    assert control.toolTip() == A22_RANGE_TEXT
    assert control.accessibleDescription() == A22_RANGE_TEXT
    assert label(window, "range_reason").text() == A22_RANGE_TEXT
    assert label(window, "range_reason").isVisible()
    assert window._active_window is requested
    assert before == (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
    _assert_refused_overview(window, A22_RANGE_TEXT)
```

- [ ] **Step 2: Insert local fixtures and the queued-delivery witness**

Insert after `retained_file_result()` and before `retained_pending_files_view()`. The witness connects after the production receiver; observing the same payload on that queued connection proves the preceding production delivery ran. Joining the settling worker processes no Qt events. Do not replace cleanup or publication with fake methods.

```python
def _assert_refused_overview(window: AppIoWindow, reason: str) -> None:
    assert window.detail_tabs.currentIndex() == 0
    assert label(window, "detail_state").isVisible()
    assert label(window, "detail_state").text() == reason
    assert listing(window).count() == 0
    assert window.selected_app_id() is None
    assert window._totals == {}
    assert kpi_texts(window) == ["unavailable"] * 5
    assert window._total_chart.is_available() is False
    assert window._total_chart.columns() == ()
    assert window._total_chart.segments() == ()
    assert all(lane.segments() == () for lane in window._device_lanes)
    assert all(not lane.isVisible() for lane in window._device_lanes)
    assert window._donut.shares() is None
    assert window._share_bars.fractions() == ()
    assert label(window, "residual_value").text() == UNAVAILABLE_RESIDUAL


def _assert_refused_files(view: FilesView, reason: str) -> None:
    assert view.state == "unavailable"
    assert view.last_result is None and view.model.rowCount() == 0
    assert [item.text() for item in view._footer_labels()] == ["unavailable"] * 8
    assert [item.objectName() for item in view._footer_labels()] == [
        "files_footer_state", "files_footer_observed", "files_footer_counted",
        "files_footer_uncounted", "files_footer_unknown_owner", "files_footer_skipped",
        "files_footer_weak", "files_footer_hooks",
    ]
    assert view._window is None
    assert view._deferred_window is None
    assert view._app_id is None
    assert view._choices_loaded is False
    assert view.app_selector.count() == 0
    assert view.app_selector.currentIndex() == -1
    assert view.app_selector.currentData() is None
    assert view.app_selector.isHidden()
    assert view.refresh_wanted() is False
    assert view._outstanding is False
    assert view._unavailable_reason is None
    assert view.unavailable_text() == reason
    assert view.state_label.text() == f"file history unavailable: {reason}"
    assert not view.state_label.isHidden()


def _refusal_query_counts(service: FakeQueryService, history: FakeFilesHistory):
    return (
        len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls),
        len(history.ranking_calls), len(history.choice_calls), len(history.requests),
    )


def _populated_refusal_console(qtbot):
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 200, peak_bps=100)
    key = "unit:retained-only.service"
    history = FakeFilesHistory(ranking_rows=(alpha,),
        local_applications=((key, "Retained only", 40),), registry_ids={key: 99})
    window, service = build(qtbot, [alpha], files_view=FilesView(history))
    view = window.files_view
    assert len(history.requests) == 1
    history.pending[-1].set_result(retained_file_result(SINCE_BOOT))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert view.last_result is not None and view.app_selector.count() == 2
    assert view.app_selector.findData(99) >= 0
    assert "40 B" in view.app_selector.itemText(view.app_selector.findData(99))
    assert not view.app_selector.isHidden()
    assert all(item.text() != "unavailable" for item in view._footer_labels())
    view.request_refresh()
    assert len(history.requests) == 2
    assert not history.pending[-1].done() and view._outstanding is True
    return window, service, history


class _QueuedFileDeliveryWitness(QObject):
    def __init__(self, view: FilesView) -> None:
        super().__init__(view)
        self.deliveries: list[object] = []
        view._bridge.delivered.connect(self.observe, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object)
    def observe(self, payload: object) -> None:
        self.deliveries.append(payload)


def _queue_success_without_pumping(view: FilesView, pending: Future, result):
    witness = _QueuedFileDeliveryWitness(view)
    worker = threading.Thread(target=pending.set_result, args=(result,), daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and pending.done()
    assert witness.deliveries == []
    assert view._outstanding is True
    return witness
```

- [ ] **Step 3: Add exactly the seven required named tests**

The signal-block parameterization tests both restoration states without adding an eighth top-level name. Every Range destination gets its own starting console so the no-supplied-boot rows cannot inherit a boot window from another row.

```python
def test_default_console_offers_only_since_boot(qtbot) -> None:
    service = FakeQueryService([], {})
    window = AppIoWindow(service)
    qtbot.addWidget(window)
    window.show()
    boot = window.findChild(QAbstractButton, "scope_since_boot")
    control = window.findChild(QAbstractButton, "scope_range")
    assert boot is not None and boot.isVisible()
    assert not boot.isEnabled() and not boot.isChecked()
    assert window.findChild(QAbstractButton, "scope_launch") is None
    assert window.findChild(QLabel, "launch_reason") is None
    assert control is not None and control.isHidden()
    assert not control.isEnabled() and not control.isChecked()
    assert sorted(button.objectName() for button in window.findChildren(QAbstractButton)
                  if button.objectName().startswith("scope_")) == [
        "scope_range", "scope_since_boot",
    ]
    assert window._active_window is window._since_boot is None
    assert label(window, "range_reason").text() == ""
    assert label(window, "range_reason").isHidden()
    window._on_since_boot_clicked()
    window._on_range_clicked()
    assert service.ranking_calls == service.series_calls == service.disk_share_calls == []
    assert window._active_window is window._since_boot is None
    assert window.has_frame is False


def test_leaving_a_range_hides_the_control(qtbot) -> None:
    supported = Window("range", BOOT, 0, MAX_RANGE_NS)
    destinations = (SINCE_BOOT, Window("lifetime"),
                    Window("range", BOOT, 0, MAX_RANGE_NS + 1),
                    Window("launch", BOOT, launch_id=1))
    for supplied_boot in (False, True):
        for destination in destinations:
            service = FakeQueryService([], {})
            window = (AppIoWindow(service, BOOT, END) if supplied_boot
                      else AppIoWindow(service))
            qtbot.addWidget(window)
            window.show()
            boot = window.findChild(QAbstractButton, "scope_since_boot")
            control = window.findChild(QAbstractButton, "scope_range")
            assert boot is not None and control is not None
            remembered = window._since_boot
            window.set_window(supported)
            assert control.isVisible() and control.isEnabled() and control.isChecked()
            assert boot.isEnabled() is supplied_boot and not boot.isChecked()
            assert window._since_boot is remembered
            before = len(service.ranking_calls)
            window._on_range_clicked()
            assert len(service.ranking_calls) == before + 1
            assert service.ranking_calls[-1][0] is supported

            window.set_window(destination)

            assert window._active_window is destination
            assert window.findChild(QAbstractButton, "scope_range") is control
            assert control.isHidden() and not control.isEnabled() and not control.isChecked()
            over_long = destination.kind == "range"
            reason = label(window, "range_reason")
            assert reason.text() == (A22_RANGE_TEXT if over_long else "")
            assert reason.isVisible() is over_long
            assert control.toolTip() == (A22_RANGE_TEXT if over_long else "")
            assert control.accessibleDescription() == (A22_RANGE_TEXT if over_long else "")
            expected_boot = destination if destination.kind == "since_boot" else remembered
            assert window._since_boot is expected_boot
            assert boot.isEnabled() is (expected_boot is not None)
            assert boot.isChecked() is (destination.kind == "since_boot")
            calls = (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))
            window._on_range_clicked()
            assert calls == (len(service.ranking_calls), len(service.series_calls), len(service.disk_share_calls))

            window.set_window(supported)
            assert window.findChild(QAbstractButton, "scope_range") is control
            assert control.isVisible() and control.isEnabled() and control.isChecked()
            assert reason.text() == "" and reason.isHidden()
            assert control.toolTip() == control.accessibleDescription() == ""
            assert window._since_boot is expected_boot
            assert boot.isEnabled() is (expected_boot is not None)
            before = len(service.ranking_calls)
            window._on_since_boot_clicked()
            if expected_boot is None:
                assert len(service.ranking_calls) == before
                assert window._active_window is supported
            else:
                assert len(service.ranking_calls) == before + 1
                assert service.ranking_calls[-1][0] is expected_boot
                assert window._active_window is expected_boot
                assert control.isHidden() and not control.isEnabled() and not control.isChecked()


@pytest.mark.parametrize("signals_blocked", [False, True])
def test_refused_window_clears_files_and_shows_the_reason(qtbot, signals_blocked) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    view = window.files_view
    window.detail_tabs.setCurrentIndex(1)
    assert window.detail_tabs.currentIndex() == 1 and view.model.rowCount() == 1
    assert view._outstanding
    view.set_scope_ready(False)
    assert view._outstanding
    selections, subjects, states = [], [], []
    view.app_selector.currentIndexChanged.connect(selections.append)
    view.subject_changed.connect(subjects.append)
    view.state_changed.connect(states.append)
    view.app_selector.blockSignals(signals_blocked)
    before = _refusal_query_counts(service, history)
    generation = view._generation
    policy = (view._scope_ready, view._transient, view._sort, view._direction,
              view.search(), view._updating_scope, view._stalled_caption, view.footer_note.text())

    window.set_window(Window("launch", BOOT, launch_id=1))

    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    assert view._generation == generation + 1
    assert view.app_selector.signalsBlocked() is signals_blocked
    assert selections == subjects == []
    assert states == ["unavailable"]
    assert policy == (view._scope_ready, view._transient, view._sort, view._direction,
                      view.search(), view._updating_scope, view._stalled_caption, view.footer_note.text())
    assert _refusal_query_counts(service, history) == before
    assert window.files_message.objectName() == "files_message"
    assert window.files_message.text() == A22_LAUNCH_TEXT
    assert not window.files_message.isHidden()
    window.detail_tabs.setCurrentIndex(1)
    assert window.files_message.isVisible()


def test_late_delivery_after_refusal_never_republishes(qtbot) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    view = window.files_view
    replacement = history.pending[-1]
    witness = _queue_success_without_pumping(view, replacement, retained_file_result(SINCE_BOOT))
    before = _refusal_query_counts(service, history)

    window.set_window(Window("launch", BOOT, launch_id=1))

    assert witness.deliveries == []
    assert view.model.rowCount() == 0 and view.state == "unavailable"
    qtbot.waitUntil(lambda: len(witness.deliveries) == 1)
    assert witness.deliveries[0][1] is replacement
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert _refusal_query_counts(service, history) == before


def test_returning_to_the_refused_window_reloads_once(qtbot) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    view = window.files_view
    supported = window._active_window
    assert isinstance(supported, Window)
    view.set_scope_ready(False)
    view.set_window(supported)
    assert view._window is supported and view._deferred_window is supported
    assert view.refresh_wanted()
    window.set_window(Window("launch", BOOT, launch_id=1))
    assert view._window is None
    assert view._deferred_window is None
    assert view._app_id is None
    assert view._choices_loaded is False
    assert view.refresh_wanted() is False
    requests = len(history.requests)
    view.set_scope_ready(True)
    assert len(history.requests) == requests

    window.set_window(supported)

    assert window._active_window is supported and view._window is supported
    assert len(history.requests) == requests + 1
    assert history.requests[-1][1] is supported
    history.pending[-1].set_result(retained_file_result(supported))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert view.state == "ready" and view.app_selector.count() == 2
    assert view.refresh_wanted() is False
    assert all(item.text() != "unavailable" for item in view._footer_labels())


def test_embedded_direct_refusal_still_clears_files(qtbot) -> None:
    alpha = app_total(1, "unit:/alpha.service", "Alpha", 200)
    key = "unit:retained-only.service"
    history = FakeFilesHistory(ranking_rows=(alpha,),
        local_applications=((key, "Retained only", 40),), registry_ids={key: 99})
    history.close = lambda: None
    history.series = lambda *args: pytest.fail("refusal queried series")
    history.disk_share = lambda *args: pytest.fail("refusal queried disk share")
    shell = CollectorWindow(SimpleNamespace(close=lambda: None), history, SimpleNamespace())
    shell._timer.stop()
    shell._poll_timer.stop()
    qtbot.addWidget(shell)
    qtbot.addWidget(shell.settings)
    shell.show()
    window = shell.view
    assert window is not None
    assert window._active_window is window._since_boot is None
    assert window._owns_files_scope is False and shell._displayed is None
    view = window.files_view
    view.set_window(SINCE_BOOT)
    history.pending[-1].set_result(retained_file_result(SINCE_BOOT))
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert view.app_selector.count() == 2
    assert "40 B" in view.app_selector.itemText(view.app_selector.findData(99))
    assert all(item.text() != "unavailable" for item in view._footer_labels())
    view.request_refresh()
    assert view._outstanding
    view.set_scope_ready(False)
    window.detail_tabs.setCurrentIndex(1)
    witness = _queue_success_without_pumping(view, history.pending[-1], retained_file_result(SINCE_BOOT))
    queries = (len(history.ranking_calls), len(history.choice_calls), len(history.requests))
    shell_fields = (shell._target, shell._displayed, shell._pending)

    window.set_window(Window("launch", BOOT, launch_id=1))

    assert witness.deliveries == []
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert view.isVisible() is False and shell._displayed is None
    assert shell.files_message.text() == shell._files_status_text() == "waiting for the first sample"
    assert shell.files_message.text() != A22_LAUNCH_TEXT
    qtbot.waitUntil(lambda: len(witness.deliveries) == 1)
    _assert_refused_files(view, A22_LAUNCH_TEXT)
    _assert_refused_overview(window, A22_LAUNCH_TEXT)
    assert queries == (len(history.ranking_calls), len(history.choice_calls), len(history.requests))
    assert shell_fields == (shell._target, shell._displayed, shell._pending)
    shell.shutdown()


def test_supported_adoption_clears_the_refusal_message(qtbot) -> None:
    window, service, history = _populated_refusal_console(qtbot)
    supported = window._active_window
    assert isinstance(supported, Window)
    window.set_window(Window("launch", BOOT, launch_id=1))
    assert window.files_message.text() == A22_LAUNCH_TEXT
    assert not window.files_message.isHidden()
    window.detail_tabs.setCurrentIndex(1)
    before = _refusal_query_counts(service, history)

    window.set_window(supported)

    assert window.files_message.text() == ""
    assert window.files_message.isHidden()
    assert window.detail_tabs.currentIndex() == 1
    assert window._active_window is supported
    assert _refusal_query_counts(service, history) == tuple(count + 1 for count in before)
    history.pending[-1].set_result(retained_file_result(supported))
    qtbot.waitUntil(lambda: window.files_view.model.rowCount() == 1)
    assert window.files_view.state == "ready"
```

- [ ] **Step 4: Run complete UI tests and retained controls**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/test_ui.py
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/p3/test_p3_acceptance.py::test_a22_exact_messages_in_client_router_and_window
```

Expected: pass with no failures. Check exact inventory with:

```bash
python - <<'PY'
import ast
from pathlib import Path
names = [item.name for item in ast.parse(Path("tests/test_ui.py").read_text()).body
         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
required = (
    "test_empty_supported_range_labels_the_active_window",
    "test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text",
    "test_over_365_day_range_shows_the_reason_and_hides_the_control",
    "test_default_console_offers_only_since_boot",
    "test_leaving_a_range_hides_the_control",
    "test_refused_window_clears_files_and_shows_the_reason",
    "test_late_delivery_after_refusal_never_republishes",
    "test_returning_to_the_refused_window_reloads_once",
    "test_embedded_direct_refusal_still_clears_files",
    "test_supported_adoption_clears_the_refusal_message",
)
assert all(names.count(name) == 1 for name in required)
assert "test_disabled_launch_control_uses_exact_a22_reason" not in names
assert "test_over_365_day_range_control_uses_exact_a22_reason" not in names
print("UI specification inventory: exact names present once")
PY
```

Production is already integrated, so do not manufacture an active-tree red or weaken assertions to claim one. Task 5 breaks each specified mechanism in a disposable copy against passing controls. Leave `test_detail_tabs_are_exactly_overview_then_files` and `test_attribution_panel_keeps_rows_and_boot_qualifies_lifetime_labels` unchanged.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ui.py
git commit -m "test(ui): cover scope visibility and refusal retirement"
```

## Task 4: Migrate the real-query A22 acceptance controls

**Implementer tier:** Fast

**Files:**

- Modify (exclusive repository ownership): `tests/acceptance/test_p1_p2.py:425-453`, only the A22 decorator/test.
- Read/test only: `tests/test_ui.py`, `tests/p3/test_p3_acceptance.py`.
- No other repository file may be created or modified.

**Interfaces:**

- Consumes: unchanged real `QueryService.ranking(window, metric)`, `series(app_id, window)`, `disk_share(app_id, window)`, `file_table(app_id, window, sort, direction, search)`, `file_coverage(app_id, window)`, `unknown_owner_file_coverage(window)` exception behavior, `WindowUnavailableError.reason: str`, and exact `str(error): str`.
- Consumes: existing `collected`/`qtbot` fixtures, `snapshot(db) -> dict`, `show_chromium(qtbot, result) -> AppIoWindow`, `label(window, name) -> str`, `LAUNCH_REASON`, `RANGE_REASON`, `BOOT`, `MAX_RANGE_NS` in this file; `AppIoWindow.set_window(self, window: Window) -> None` from Task 2.
- Produces: `test_disabled_windows_raise_exact_a22_errors_and_offer_no_control(collected, qtbot, kind, reason, message, control_state)` with `control_state="absent"` / `control_state="hidden"`. Preserve all six query checks, exact tokens/messages and the complete store snapshot assertion.

- [ ] **Step 1: Observe the obsolete common-control assertion**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/acceptance/test_p1_p2.py::test_disabled_windows_raise_exact_a22_errors_and_disable_controls
```

Expected: the launch row fails because `scope_launch` is absent. Record the failed assertion; query behavior must remain unchanged. The Range row's old metadata expectations remain valid, and both tooltip/accessibility checks have permanent UI coverage in Task 3.

- [ ] **Step 2: Replace exactly the A22 decorator and function**

```python
@pytest.mark.parametrize("kind,reason,message,control_state", [
    ("launch", "launch_attribution", LAUNCH_REASON, "absent"),
    ("range", "unsupported_window", RANGE_REASON, "hidden"),
])
def test_disabled_windows_raise_exact_a22_errors_and_offer_no_control(collected, qtbot, kind, reason, message, control_state):
    aid = collected.app_id()
    launch_id = collected.store.connection.execute("SELECT launch_id FROM launch WHERE app_id=?", (aid,)).fetchone()[0]
    scope = (Window("launch", BOOT, launch_id=launch_id) if kind == "launch"
             else Window("range", BOOT, 0, MAX_RANGE_NS + 1))
    service = collected.service
    queries = (
        lambda: service.ranking(scope, "total_bytes"), lambda: service.series(aid, scope),
        lambda: service.disk_share(aid, scope),
        lambda: service.file_table(aid, scope, "bytes_total", "desc", None),
        lambda: service.file_coverage(aid, scope), lambda: service.unknown_owner_file_coverage(scope),
    )
    before = snapshot(collected.store.connection)
    for query in queries:
        with pytest.raises(WindowUnavailableError) as error:
            query()
        assert (error.value.reason, str(error.value)) == (reason, message)
    window = show_chromium(qtbot, collected)
    window.set_window(scope)
    if control_state == "absent":
        assert window.findChild(QPushButton, "scope_launch") is None
    else:
        assert control_state == "hidden"
        control = window.findChild(QPushButton, "scope_range")
        assert control is not None
        assert control.isHidden()
        assert not control.isEnabled()
        assert not control.isChecked()
    assert label(window, "detail_state") == message
    assert label(window, "total_value") == "unavailable"
    assert snapshot(collected.store.connection) == before
```

- [ ] **Step 3: Run the required targeted gate and retained P3 control**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/test_ui.py tests/acceptance/test_p1_p2.py
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/p3/test_p3_acceptance.py::test_a22_exact_messages_in_client_router_and_window
```

Expected: pass with no failures, including both acceptance rows. Inspect the diff: retain both literal A22 messages, every query call and exact error assertion, detail/KPI checks, and unchanged snapshot equality.

- [ ] **Step 4: Commit**

```bash
git add tests/acceptance/test_p1_p2.py
git commit -m "test(acceptance): assert unavailable scopes offer no control"
```

## Task 5: Prove the mutation inventory and run the final implementation gates

**Implementer tier:** Standard

**Files:**

- Repository ownership: none. Create/modify no repository file; stage no path. The final commit is empty.
- Create/modify (disposable copies only): `/tmp/appio-dead-scope-task-5-*/checkout/src/appio/ui/main_window.py`, `/tmp/appio-dead-scope-task-5-*/checkout/src/appio/ui/files_view.py`, `/tmp/appio-dead-scope-task-5-*/checkout/src/appio/ui/formatting.py` for the listed mutations; copy source/tests/fixtures/configuration unchanged under that checkout. Temporary pytest logs/XML live only below that temporary root.
- Read/test only in the active checkout: `tests/test_ui.py`, `tests/acceptance/test_p1_p2.py`, `tests/p3/test_p3_acceptance.py`, and the existing full suite.
- Evidence goes only to the assigned report path outside the checkout. Add no new permanent test; this task falsifies the exact named tests from the specification.

**Interfaces:**

- Consumes: the controller-supplied Task 1 report containing `implementation_base_sha: str` and observed initial collection count/output, plus Tasks 1–4's passing checkpoint reports. Missing evidence is not run, never inferred.
- Consumes: `FilesView.refuse(self, reason: str) -> None`, `AppIoWindow.set_window(self, window: Window) -> None`, the exact R25/R26 test names below, `test_disabled_windows_raise_exact_a22_errors_and_offer_no_control`, and unchanged `test_a22_exact_messages_in_client_router_and_window`.
- Produces: actual collection comparison, targeted/full-suite results, per-mutation passing baseline → named assertion failure → restored passing result, and protected-byte evidence. No production/permanent-test edit is authorized.

- [ ] **Step 1: Establish a targeted baseline and observe collection**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest tests/test_ui.py tests/acceptance/test_p1_p2.py
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest --collect-only -q
```

Record the final count beside Task 1's actual initial count. Two UI renames, one extension, seven new UI functions (one parameterized), and one acceptance rename describe inventory, not collected-count evidence.

- [ ] **Step 2: Run each exact mutation in a disposable copy**

Run this driver. Record a compact ledger entry per case in the assigned report: mutation ID, exact named test, actual failing source/assertion line, and restored pass; omit complete tracebacks from that bounded report. Each selected test must pass in the same copy before mutation. Restore source in `finally`, then require the test to pass again. Import/collection errors, non-assertion exceptions, skips, timeouts and already-red controls are not mutation kills. Qt delivery mutations must fail after the witness actually drains the old success.

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python - <<'PY'
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

source = Path.cwd().resolve()
main = 'src/appio/ui/main_window.py'
files = 'src/appio/ui/files_view.py'
fmt = 'src/appio/ui/formatting.py'
ui = 'tests/test_ui.py::'
launch = 'test_launch_scope_is_not_offered_and_detail_fallback_keeps_a22_text'
long_range = 'test_over_365_day_range_shows_the_reason_and_hides_the_control'
empty = 'test_default_console_offers_only_since_boot'
entry = 'test_empty_supported_range_labels_the_active_window'
exit_range = 'test_leaving_a_range_hides_the_control'
cleanup = 'test_refused_window_clears_files_and_shows_the_reason'
late = 'test_late_delivery_after_refusal_never_republishes'
returns = 'test_returning_to_the_refused_window_reloads_once'
embedded = 'test_embedded_direct_refusal_still_clears_files'
caption = 'test_supported_adoption_clears_the_refusal_message'

# id, file, class (None for a module function), method, exact old/new, killer
cases = [
    ('launch-button', main, 'AppIoWindow', '_build_ui',
     '        self._range_control = self._scope_control("Range", "scope_range")',
     '        self._range_control = self._scope_control("Range", "scope_range")\n'
     '        revived = self._scope_control("Launch", "scope_launch")\n'
     '        revived.setParent(self)\n        revived.setEnabled(False)', launch),
    ('launch-label', main, 'AppIoWindow', '_build_ui',
     '        self._range_reason = QLabel("")',
     '        revived = QLabel("Launch window disabled: device and file activity are not stored per launch.", self)\n'
     '        revived.setObjectName("launch_reason")\n        self._range_reason = QLabel("")', launch),
    ('detail-text', main, 'AppIoWindow', '_render_unavailable',
     '        self._detail_state.setText(reason)',
     '        self._detail_state.setText("unavailable")', launch),
    ('detail-hidden', main, 'AppIoWindow', '_render_unavailable',
     '        self._detail_state.setText(reason)',
     '        self._detail_state.setText(reason)\n        self._detail_state.hide()', launch),
    ('range-always-visible', main, 'AppIoWindow', 'set_window',
     '        self._range_control.setVisible(supported_range)',
     '        self._range_control.setVisible(True)', long_range),
    ('empty-range-not-hidden', main, 'AppIoWindow', '_build_ui',
     '        self._range_control.setVisible(False)\n', '', empty),
    ('empty-range-enabled', main, 'AppIoWindow', '_build_ui',
     '        self._range_control.setEnabled(False)\n', '', empty),
    ('range-never-visible', main, 'AppIoWindow', 'set_window',
     '        self._range_control.setVisible(supported_range)',
     '        self._range_control.setVisible(False)', entry),
    ('range-never-enabled', main, 'AppIoWindow', 'set_window',
     '        self._range_control.setEnabled(supported_range)',
     '        self._range_control.setEnabled(False)', entry),
    ('range-never-checked', main, 'AppIoWindow', 'set_window',
     '        self._range_control.setChecked(supported_range)',
     '        self._range_control.setChecked(False)', entry),
    ('range-boundary-exclusive', fmt, None, 'window_disabled_reason',
     '> MAX_RANGE_NS', '>= MAX_RANGE_NS', entry),
    ('range-retains-visibility', main, 'AppIoWindow', 'set_window',
     '        self._range_control.setVisible(supported_range)',
     '        if supported_range:\n            self._range_control.setVisible(True)', exit_range),
    ('range-retains-enabling', main, 'AppIoWindow', 'set_window',
     '        self._range_control.setEnabled(supported_range)',
     '        if supported_range:\n            self._range_control.setEnabled(True)', exit_range),
    ('range-retains-check', main, 'AppIoWindow', 'set_window',
     '        self._range_control.setChecked(supported_range)',
     '        if supported_range:\n            self._range_control.setChecked(True)', exit_range),
    ('missing-files-refusal', main, 'AppIoWindow', 'set_window',
     '            self.files_view.refuse(reason)\n', '', cleanup),
    ('transient-failure-boundary', files, 'FilesView', 'refuse',
     '        self._set_unavailable(None, reason)',
     '        self._handle_failure(None, reason)', cleanup),
    ('selector-items-retained', files, 'FilesView', 'refuse',
     '            self.app_selector.clear()\n', '', cleanup),
    ('selector-not-hidden', files, 'FilesView', 'refuse',
     '            self.app_selector.setVisible(False)\n', '', cleanup),
    ('subject-retained', files, 'FilesView', 'refuse',
     '            self._app_id = None\n', '', cleanup),
    ('choice-state-retained', files, 'FilesView', 'refuse',
     '            self._choices_loaded = False\n', '', cleanup),
    ('selector-not-blocked', files, 'FilesView', 'refuse',
     '        blocked = self.app_selector.blockSignals(True)',
     '        blocked = self.app_selector.blockSignals(False)', cleanup),
    ('selector-block-state-lost', files, 'FilesView', 'refuse',
     '            self.app_selector.blockSignals(blocked)',
     '            self.app_selector.blockSignals(False)', cleanup),
    ('refresh-rearmed-by-retirement', files, 'FilesView', 'refuse',
     '        self.retire_deliveries()\n        self._refresh_wanted = False',
     '        self._refresh_wanted = False\n        self.retire_deliveries()', cleanup),
    ('delivery-not-retired', files, 'FilesView', 'refuse',
     '        self.retire_deliveries()\n', '', late),
    ('stale-generation-publishes', files, 'FilesView', '_on_delivered',
     '        if self._closed or generation != self._generation:',
     '        if self._closed:', late),
    ('adopted-window-retained', files, 'FilesView', 'refuse',
     '        self._window = None\n', '', returns),
    ('deferred-window-retained', files, 'FilesView', 'refuse',
     '        self._deferred_window = None\n', '', returns),
    ('embedded-refusal-omitted', main, 'AppIoWindow', 'set_window',
     '            self.files_view.refuse(reason)',
     '            if self._owns_files_scope:\n                self.files_view.refuse(reason)', embedded),
    ('overview-not-selected', main, 'AppIoWindow', 'set_window',
     '            self.detail_tabs.setCurrentIndex(0)\n', '', cleanup),
    ('embedded-overview-not-selected', main, 'AppIoWindow', 'set_window',
     '            self.detail_tabs.setCurrentIndex(0)\n', '', embedded),
    ('standalone-reason-not-set', main, 'AppIoWindow', 'set_window',
     '                self.files_message.setText(reason)\n', '', cleanup),
    ('standalone-reason-not-shown', main, 'AppIoWindow', 'set_window',
     '                self.files_message.setVisible(True)\n', '', cleanup),
    ('supported-caption-not-cleared', main, 'AppIoWindow', 'set_window',
     '            self.files_message.setText("")\n', '', caption),
    ('supported-caption-not-hidden', main, 'AppIoWindow', 'set_window',
     '            self.files_message.setVisible(False)\n', '', caption),
]


def changed_method(text, class_name, method_name, old, new):
    tree = ast.parse(text)
    owner = tree if class_name is None else next(
        item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name)
    method = next(item for item in owner.body
                  if isinstance(item, ast.FunctionDef) and item.name == method_name)
    lines = text.splitlines(keepends=True)
    block = ''.join(lines[method.lineno - 1:method.end_lineno])
    assert block.count(old) == 1, (class_name, method_name, old)
    replacement = block.replace(old, new, 1)
    mutated = ''.join(lines[:method.lineno - 1]) + replacement + ''.join(lines[method.end_lineno:])
    ast.parse(mutated)
    return mutated


with tempfile.TemporaryDirectory(prefix='appio-dead-scope-task-5-') as temporary:
    root = Path(temporary)
    checkout = root / 'checkout'
    shutil.copytree(source, checkout, symlinks=True,
        ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__', '.pytest_cache', '.codegraph'))
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(checkout / 'src')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['QT_QPA_PLATFORM'] = 'offscreen'
    environment['PYTEST_QT_API'] = 'pyqt6'
    subprocess.run([sys.executable, '-c',
        'from pathlib import Path; import appio.ui.main_window as m; '
        'assert Path(m.__file__).resolve().is_relative_to(Path.cwd())'],
        cwd=checkout, env=environment, check=True)
    baseline = {name: (checkout / name).read_text() for name in (main, files, fmt)}

    def run(test_name, label):
        xml = root / f'{label}.xml'
        result = subprocess.run([sys.executable, '-m', 'pytest', ui + test_name, '-q',
            '--tb=short', '-p', 'no:cacheprovider', '--junitxml', str(xml)],
            cwd=checkout, env=environment, text=True, capture_output=True, timeout=60)
        (root / f'{label}.log').write_text(result.stdout + result.stderr)
        assert xml.exists(), (label, result.stdout, result.stderr)
        document = ET.parse(xml)
        assert not document.findall('.//error'), (label, result.stdout, result.stderr)
        assert not document.findall('.//skipped'), (label, 'unexpected skip')
        return result, document

    for index, test_name in enumerate(sorted({case[-1] for case in cases})):
        result, _ = run(test_name, f'baseline-{index}')
        assert result.returncode == 0, (test_name, result.stdout, result.stderr)
    print('All named mutation controls pass in the disposable checkout', flush=True)

    for mutation_id, path, class_name, method, old, new, test_name in cases:
        target = checkout / path
        try:
            target.write_text(changed_method(baseline[path], class_name, method, old, new))
            result, document = run(test_name, mutation_id)
            failures = document.findall('.//testcase/failure')
            assert result.returncode == 1 and failures, (mutation_id, result.stdout, result.stderr)
            assert all('AssertionError' in (failure.text or '')
                       or (failure.get('message') or '').startswith('assert ')
                       for failure in failures), (mutation_id, result.stdout, result.stderr)
            assert all(case.get('name', '').startswith(test_name)
                       for case in document.findall('.//testcase') if case.find('failure') is not None)
            print(f'KILLED {mutation_id}: {test_name}', flush=True)
            for failure in failures:
                print((failure.text or failure.get('message') or '').strip(), flush=True)
        finally:
            target.write_text(baseline[path])
        restored, _ = run(test_name, mutation_id + '-restored')
        assert restored.returncode == 0, (mutation_id, restored.stdout, restored.stderr)
        assert all((checkout / name).read_text() == original for name, original in baseline.items())
        print(f'RESTORED PASS {mutation_id}', flush=True)
PY
```

There are 34 independently restored cases. The adopted/deferred-window mutations must fail the direct seeded-state assertions even if request count still happens to be one. The two signal-block mutations distinguish clearing under an unblocked selector from restoring an originally blocked selector. Keep an actual failing assertion for every case, including cases sharing a killer test.

- [ ] **Step 3: Verify exclusive paths and protected bytes**

Set `APPIO_IMPLEMENTATION_BASE_SHA` to the literal observed SHA in the Task 1 report, then run this read-only check and `git diff --check`. The controller must supply that report; do not guess a newer baseline. No protected path is editable during this task.

```bash
python - "$APPIO_IMPLEMENTATION_BASE_SHA" <<'PY'
import ast
import os
from pathlib import Path
import subprocess
import sys
base = sys.argv[1]
assert len(base) == 40 and all(char in '0123456789abcdef' for char in base)
allowed = {'src/appio/ui/files_view.py', 'src/appio/ui/main_window.py',
           'tests/test_ui.py', 'tests/acceptance/test_p1_p2.py'}
entries = subprocess.check_output(['git', 'ls-tree', '-r', '-z', base]).split(b'\0')
for entry in filter(None, entries):
    metadata, encoded_name = entry.split(b'\t', 1)
    mode, kind, object_id = metadata.split()
    name = os.fsdecode(encoded_name)
    if name not in allowed:
        assert kind == b'blob', name
        before = subprocess.check_output(['git', 'cat-file', 'blob', object_id.decode()])
        actual = os.fsencode(os.readlink(name)) if mode == b'120000' else Path(name).read_bytes()
        assert actual == before, name
changed = set(subprocess.check_output(['git', 'diff', '--name-only', base, 'HEAD'], text=True).splitlines())
assert changed <= allowed, changed - allowed

def method_bytes(data, method_name):
    text = data.decode()
    owner = next(item for item in ast.parse(text).body
                 if isinstance(item, ast.ClassDef) and item.name == 'AppIoWindow')
    node = next(item for item in owner.body
                if isinstance(item, ast.FunctionDef) and item.name == method_name)
    start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
    return ''.join(text.splitlines(keepends=True)[start - 1:node.end_lineno]).encode()
path = 'src/appio/ui/main_window.py'
before = subprocess.check_output(['git', 'show', f'{base}:{path}'])
after = Path(path).read_bytes()
for name in ('_scope_control', '_on_since_boot_clicked', '_on_range_clicked',
             '_render_unavailable', '_render_empty_charts', '_sync_owned_files'):
    assert method_bytes(before, name) == method_bytes(after, name), name
print('Protected tracked bytes and retained window methods are unchanged')
PY
git diff --check
```

This includes both signed-off documents, P3 acceptance, formatting/contracts and every other protected tracked source/test. Report any defect to its owning task through the controller; do not make a repair in this verification-only task.

- [ ] **Step 4: Run the required full suite**

```bash
QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 python -m pytest
```

Expected: pass with no failures, including unchanged P3 acceptance. Record actual pass/skip/failure and collection counts; mark missing evidence not run. The assigned report must map R01–R24/R29 to preserved/implemented behavior and R25–R28/R30 to observed test/mutation evidence. Add no environment or privileged integration prerequisite.

- [ ] **Step 5: Commit**

After reporting the observed results, require an empty index and make this verification-only commit, which changes no file:

```bash
git diff --cached --quiet && git commit --allow-empty -m "test(ui): verify dead scope control acceptance gates"
```
