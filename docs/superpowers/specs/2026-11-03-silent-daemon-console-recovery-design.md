# appio console: honest silence, and a console that recovers

Date: 2026-11-03
Status: draft for review
Supersedes one decision of `2026-11-01-appio-console-charts-design.md` (§5.5's
acceptance of one replacement preparation per sampled frame); see §9.

## 1. Background

A user screenshot showed the banner at `enabled, not answering` above a console
stuck at `updating (loading)` on top of a complete frame, with the footer reading
`coverage: incomplete; device attribution: incomplete; …; reason: not_retained` and
the device strip reading `device attribution unavailable: not_retained`.

Three separate faults produce that one picture.

### 1.1 The console never stops loading

| Cause | Evidence | Effect |
| --- | --- | --- |
| Every probe delta invalidates stored history | `CollectorCoordinator.reconcile` calls `history.invalidate()` unconditionally (`src/appio/collector_owner.py:196`), whatever the owner decision | A prepared read is retired by a fact change that changes nothing a reader can see |
| A crash-looping unit changes facts every second | `_ACTIVE_STATES` has no `activating` entry (`src/appio/ui/collector_window.py:139-145`), so `active` and `activating` both map to `"unknown"`; `decide_owner` returns the same decision for both (`src/appio/collector_owner.py:56-63`) | The owner is `unknown → unknown` while the facts differ every refresh, so the invalidation never stops |
| Invalidation discards the frame under preparation | `invalidate()` bumps the generation, cancels file requests and sets `view_state = "unavailable"` (`src/appio/history_router.py:413-421`); `_tick_target` drops `_pending` when the router reports retirement (`src/appio/ui/collector_window.py:690-691`) and immediately prepares again (`:735-748`) | `_show_pending_status` prints `updating (loading)` (`:772-780`) for as long as the invalidation continues |
| Nothing bounds the wait | `_pending` has no timestamp and no deadline; `apply_ready_frame` only ever retires on router retirement (`:784-800`) | `updating (loading)` can last forever with no honest label |

The only existing guard is `keep_pending = facts == self._facts` (`:1004`), which
covers *identical* facts only. The 2026-11-01 design knew about this and chose to
leave it: §1.1 lists "Every invalidation re-prepares the same scope" and §5.5 states
"With local collection active, each sampled frame invalidates, so one replacement…".
Its remedy (AC-1, retiring queued deliveries when a frame is applied) removes
duplicate *deliveries*; it cannot stop the next probe delta from retiring a read that
is still being prepared.

### 1.2 The reason is fabricated

`_not_retained` is set whenever a remote query fails or is retired
(`src/appio/history_router.py:641`, `:646`, `:899`), and is then stamped into a
contract data field: `Coverage.unavailable_reason = "not_retained"`
(`:916`, `:945`, `:997`). `not_retained` is a *data* reason — retained history was
pruned (`src/appio/contracts.py:323-359`, `2026-10-21-app-io-monitor-spec.md` §4) —
so a dead daemon is reported as expired history. Because the same token feeds the
coverage footer (`src/appio/ui/render_model.py:359-372`) and the device strip
(`src/appio/ui/main_window.py:154-163`), one panel can say
`device attribution: incomplete` while the other says `unavailable: not_retained`.

### 1.3 The banner cannot say why

`display_state` produces one of the four ratified strings
(`src/appio/ui/collector_window.py:322`, `2026-10-21-app-io-monitor-spec.md` §12:
"Daemon enabled but silent | Preserve `enabled, not answering`"). The window already
holds the client's own refusal reason (`ClientState.last_error`) and
`IodClient`'s connect deadline (`src/appio/iodclient.py:110`), but the probe asks
systemd for two properties only (`UNIT_SHOW_ARGV`, `:115-121`; `parse_unit_facts`
rejects any other key set, `:1252-1269`). So "not answering" cannot distinguish a
crash loop, a permission refusal, and a daemon that never started.

## 2. Goals

1. A probe delta that changes nothing a reader can observe never retires a prepared
   read; a crash-looping unit therefore cannot keep the console loading.
2. A history read that does not finish is bounded: it is retired after a budget, the
   displayed frame stays, the label says so, and the retry is rate-limited.
3. The console never reports a data-retention reason for a client-side failure; the
   coverage footer and the device strip agree.
4. `enabled, not answering` carries one honest cause line, from the client's own
   observation plus systemd's restart/failure evidence.
5. No new data contract, no daemon or wire change, no new runtime dependency.

## 3. Non-goals

- Launch and Range stay disabled with their existing A22 reasons
  (`src/appio/ui/formatting.py:12-13`, `:78-90`); the launch window is genuinely not stored.
- Retention, cadence, sampling and the `not_retained` data semantics are unchanged.
- The four state strings and every other ratified vocabulary stay byte-identical.
- No new `UnavailableReason` token: the contract literal keeps its four members.
- Recovering the daemon itself (restart policy, socket ownership) is out of scope.
  The window reports the evidence; it does not repair the service.

## 4. Decisions taken with the user

| # | Decision | Choice |
| --- | --- | --- |
| D1 | Scope | All three faults: permanent spinner, fabricated reason, and the boxed banner |
| D2 | Banner content | The ratified state string stays; a cause line is added from the socket observation **and** systemd restart/failure evidence |
| D3 | Banner layout | Option A: the cause is a second, muted line under the state inside the banner strip |
| D4 | Slow read | Bounded wait (30 s), honest stalled label, rate-limited retry that heals itself |

Mockup of record for D3: `assets/2026-11-03-silent-daemon-cause.png`.

## 5. Architecture

### 5.1 Staleness is one key, not every fact delta

`CollectorCoordinator` keeps the key its current stored history was prepared under:

```python
HistoryKey = tuple[OwnerKind, bool, bool, bool]
# (decision.owner, decision.may_collect_local, facts.daemon_verified, facts.answering)
```

`reconcile` computes `decide_owner(facts)` first, derives the key, and calls
`history.invalidate()` only when the key differs from the stored one (or when no key
is stored yet). The key deliberately excludes the display string and the
`enabled`/`active` facts: `enabled, not answering` and `unavailable` read differently
but select the same data source (local only, no daemon contribution).

Guarantee: a fact delta that maps to the same key must not change
`history.view_state` and must not bump the router generation. A key change is the
only probe-driven invalidation.

The key is the whole staleness question; no second trigger is scattered through the
class. `accept_export` keeps its existing invalidation through `add_source`.

### 5.2 A local tick that wrote nothing is not an advance

`tick_local` reads the collector's committed `TickResult` and invalidates only when
it wrote rows:

```python
result = self._local.tick()
if result.rows_written:
    self.history.invalidate()
```

`TickResult.rows_written` (`src/appio/live.py:87-99`) counts rows committed to
`sample`, `sample_dev` and `sample_coverage` (`_ROW_TABLES`, `:48`). A tick that
observed no application writes none of them (`store._record_tick_in_transaction`,
`src/appio/store.py:461-482`), so it cannot change a query answer and must not retire
a read. The spec pins the exact table set the router queries, so an idle tick is
provably a no-op for readers.

### 5.3 A bounded wait with a retry floor

New window fields: `_pending_since: int | None`, `_retry_after: int | None`, and
`PENDING_BUDGET_NS = 30 * 10**9`.

- `_pending_since` is set when a preparation is accepted and cleared whenever
  `_pending` is cleared.
- In `_tick_target`, before preparing: when `_pending` is not None and
  `now - _pending_since >= PENDING_BUDGET_NS`, retire that preparation
  (`_pending = None`, keep `_displayed`), set `_retry_after = now + PENDING_BUDGET_NS`,
  and render the stalled label.
- While `_retry_after` is in the future, no new preparation is issued.
- The pause and the label are cleared by an applied frame, by a real invalidation
  (`_history_retired()`), and by an explicit refresh (`files_view`/Recheck path).
- The stalled label is
  `history read has not completed (<view_state>); retrying in <n> s`, rendered in the
  existing `history_state` label, which today prints `updating (loading)`.

The deadline is a clock deadline, not scope identity: `_current_window()` advances its
end to the next `RAW_BUCKET_NS` boundary, so a pause keyed on the scope would never
fire. The engine does not claim the read was cancelled — a running future cannot be
interrupted; only the waiting ends.

### 5.4 A client failure is not a retention reason

`HistoryRouter` keeps the contract field honest and carries its own diagnostic:

- `_not_retained` is set only from row-level coverage (`:883-886`), i.e. from what the
  data says about itself.
- New display-only field `daemon_answer_error: str | None`, set on the three
  failure/retirement paths (`:641`, `:646`, `:899`) from the exception's stable
  reason, and cleared by `prepare()` (`:742-769`).
- The three stampings at `:916`, `:945`, `:997` stop writing
  `unavailable_reason="not_retained"` for a client failure. The coverage object keeps
  every genuinely data-derived reason, including a real `not_retained` from pruned
  rows.

Rendering: `build_overview` gains one optional keyword
`coverage_diagnostic: str | None = None` (`src/appio/ui/render_model.py:420-430`),
stored on `OverviewModel` and consumed by `_coverage_text` and
`main_window._device_unavailable_text`. When it is set, the sentence is
`… ; reason: daemon answer unusable (<detail>)`; when it is not, the existing
`reason: <contract reason>` stands unchanged.

### 5.5 One honest cause line under the state

Probe: `UNIT_SHOW_ARGV` additionally requests `SubState`, `Result` and `NRestarts` on
the same `systemctl show` invocation. `parse_unit_facts` keeps failing closed on a
malformed line or a duplicated key, requires `UnitFileState` and `ActiveState`, and
treats the three new properties as optional evidence.

`ServiceFacts` gains three defaulted fields (`sub_state: str | None = None`,
`result: str | None = None`, `restarts: int | None = None`) so every existing
positional construction keeps its meaning.

New pure function in `src/appio/ui/collector_window.py`:

```python
def silence_cause(facts: ServiceFacts, client_state: ClientState | None) -> str | None
```

It returns `None` unless the state is `enabled, not answering`; otherwise it joins
one socket clause and one systemd clause when available:

| Socket observation (`ClientState.last_error`) | Clause |
| --- | --- |
| `socket_not_connected`, missing path | `socket: not connected` |
| `ECONNREFUSED` | `socket: connection refused` |
| five-second silence (`CONNECT_DEADLINE_NS`) | `socket: no answer within 5 s` |
| permission/identity refusal (`PeerRejected.reason`) | `socket: <reason>` |
| no client observation | socket clause omitted |

| systemd evidence | Clause |
| --- | --- |
| `NRestarts > 0` | `<n> restarts` |
| `Result` not `success`/`none` | `failed: <result>` |
| `SubState == "auto-restart"` | `restarting` |

Rendering: `banner_detail` is a second `QLabel` under `banner_status` inside the banner
strip (`src/appio/ui/collector_window.py:527-533`), hidden when `silence_cause`
returns `None`, and updated in `_render` beside `banner_status.setText(state)`
(`:884-892`). The state strings themselves are untouched, so `settings.status_label`
and every existing assertion keep working.

### 5.6 Data flow after the change

```text
probe facts ──► decide_owner ──► HistoryKey ──┬─ same ──► no invalidate (frame survives)
                                              └─ differs ──► invalidate ──► reprepare
local tick ──► TickResult.rows_written ──┬─ 0 ──► no invalidate
                                         └─ >0 ──► invalidate
pending read ──► 30 s budget ──► retire wait, stalled label, retry floor
remote failure ──► daemon_answer_error ──► "reason: daemon answer unusable (…)"
```

### 5.7 State fields

| Field | Owner | Meaning |
| --- | --- | --- |
| `_history_key` | `CollectorCoordinator` | key the current stored history was prepared under |
| `_pending_since` | `CollectorWindow` | clock of the accepted preparation |
| `_retry_after` | `CollectorWindow` | earliest next preparation after a stall |
| `daemon_answer_error` | `HistoryRouter` | display-only client diagnostic, never a data reason |
| `coverage_diagnostic` | `OverviewModel` | the same string on its way to the footer and device strip |
| `banner_detail` | `CollectorWindow` | the cause line widget |

## 6. Error handling

| Situation | Behaviour |
| --- | --- |
| `systemctl show` fails or returns an unexpected key | `enabled`/`active` become `unknown` as today; no cause clause is invented |
| `NRestarts`/`Result` malformed | property ignored; the socket clause still renders |
| Client has never connected (no `last_error`) | cause line shows systemd evidence only, or stays hidden |
| Read exceeds the budget repeatedly | every stall is labelled, the retry floor prevents a busy loop, no numeric zero is invented |
| Router retirement during a stall pause | pause cleared, normal prepare resumes on the next tick |
| Remote query fails after a local answer arrives | local rows stay; the footer says `daemon answer unusable (…)`, never `not_retained` |

## 7. Test strategy

### 7.1 Coordinator (`tests/p3/test_collector_owner.py`)

1. `reconcile` with `SILENT` facts, then `replace(SILENT, active="active")` (the
   crash-loop delta): generation and `view_state` unchanged, exactly one
   invalidate for the first call and none for the second.
2. A genuine key change (`SILENT` → `OFF` → `ON`) invalidates exactly twice and the
   owner transitions as today.
3. `tick_local` with `rows_written == 0` does not invalidate; with rows it does.

### 7.2 Window (`tests/p3/test_collector_window.py`)

4. A pending preparation survives ten owner-equivalent probe deltas.
5. A pending preparation older than `PENDING_BUDGET_NS` is retired, the displayed
   frame stays, `history_state` reads the stalled sentence, and the next tick issues
   no preparation. After the floor, exactly one preparation is issued.
6. An applied frame clears the stalled label and the floor.
7. A real invalidation during a stall pause resumes immediately.

### 7.3 Router and rendering (`tests/p3/test_file_history_router.py`, `tests/test_ui.py`)

8. A failed remote query sets `daemon_answer_error` and leaves
   `coverage.unavailable_reason` unset; a pruned-row query still yields
   `not_retained`.
9. `_coverage_text` with a diagnostic renders `reason: daemon answer unusable (…)`
   and never two reasons; the device strip uses the same string.

### 7.4 Banner (`tests/p3/test_collector_window.py`)

10. `parse_unit_facts` accepts the five-property output, still fails closed on a
    duplicated key or malformed line.
11. `silence_cause` for each socket reason and for restart/failure evidence; `None`
    for every other state.
12. `banner_detail` is visible only in `enabled, not answering`, and
    `banner_status`/`settings.status_label` still read the ratified strings.

### 7.5 Existing tests that must change

- `test_changed_ownership_replaces_retained_preparation` drives retirement through a
  fake knob (`coordinator.invalidate_on = "reconcile"`). It keeps its ownership
  assertions; the invalidation assertion moves to the genuine key-change test.
- The fake coordinator's `tick_local` invalidates unconditionally; it gains the
  `rows_written` rule so window tests exercise the real trigger.

### 7.6 Gates

Full suite, run in the pinned environment, compared against the recorded baseline
`2652 passed, 1 failed (missing clang in tests/p4/test_bpf_build.py), 2 skipped`: no
new failures. Plus a manual live check on a host with the daemon: with the unit
stopped, the console must settle into the stalled label instead of a permanent
spinner, and the banner must name the socket cause.

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Over-eager no-invalidate leaves stale data on screen | The key contains every input that selects a source; a genuine source change still invalidates |
| A 30 s budget masks a permanently broken read | The label is explicit and rate-limited; the data shown is still labelled `incomplete` |
| `rows_written` misses a reader-visible table | The spec enumerates the tables the router queries and a test pins the idle-tick case |
| A longer banner strip crowds a narrow window | The cause line wraps inside the existing strip; the mockup was checked at the shipped width |
| Removing the fabricated reason hides a real gap | The contract reason still comes from rows; only the client-failure case changes wording |

## 9. Relationship to earlier design and the mockup record

`2026-11-01-appio-console-charts-design.md` §5.5 accepted one replacement preparation
per sampled frame and treated unconditional `reconcile()` invalidation as given. This
document replaces that single decision: a sampled frame invalidates only when it wrote
rows, and a probe delta invalidates only when the staleness key changes. Everything
else in that design — target/displayed/pending discipline, transient scopes, chart
rendering, A22 launch reasons — stands unchanged.

Layout authority for §5.5 is `assets/2026-11-03-silent-daemon-cause.png`: three
candidate layouts (A stacked detail line, B single line with separators, C chips on
the right); **A** is chosen (D3). Behaviour follows this text where the mockup is
silent.
