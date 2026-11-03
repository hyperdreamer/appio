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
| A crash-looping unit changes facts every second | `_ACTIVE_STATES` maps `active` to `active` but has no `activating` entry, which falls back to `unknown` (`src/appio/ui/collector_window.py:139-145`); `decide_owner` nevertheless returns the same decision for both (`src/appio/collector_owner.py:56-63`) | The owner is `unknown → unknown` while the facts differ every refresh, so the invalidation never stops |
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

One boolean carries two unrelated meanings. Local retention loss sets
`_not_retained` when a persisted interval cannot be corrected or a legacy aggregate
carries no ownership receipt (`src/appio/history_router.py:641`, `:646`), and a *failed
remote query* sets the same flag (`:899`). Both are then stamped into a contract data
field, `Coverage.unavailable_reason = "not_retained"` (`:916`, `:945`, `:997`), so a
dead daemon is reported as expired history. `not_retained` is a *data* reason — retained history was
pruned (`src/appio/contracts.py:323-359`, `2026-10-21-app-io-monitor-spec.md` §4) —
so a dead daemon is reported as expired history. Because the same token feeds the
coverage footer (`src/appio/ui/render_model.py:359-372`) and the device strip
(`src/appio/ui/main_window.py:154-163`), one panel can say
`device attribution: incomplete` while the other says `unavailable: not_retained`.

### 1.3 The banner cannot say why

`display_state` returns either the `decide_owner` state — one of the five owner strings
(`src/appio/ui/collector_window.py:322`, `2026-10-21-app-io-monitor-spec.md` §12:
"Daemon enabled but silent | Preserve `enabled, not answering`") — or, once a root probe
has run, one of the five activation display states defined in
`src/appio/activation.py:119-134`. The window already
holds the client's own refusal reason (`ClientState.last_error`) and
the client's silence budget (`NO_ANSWER_NS`, 5 s,
`src/appio/iodclient.py:109-110`), but the probe asks
systemd for two properties only (`UNIT_SHOW_ARGV`, `:115-121`; `parse_unit_facts`
rejects any other key set, `:1252-1269`). So "not answering" cannot distinguish a
crash loop, a permission refusal, and a daemon that never started.

## 2. Goals

1. A probe delta that changes nothing a reader can observe never retires a prepared
   read; a crash-looping unit therefore cannot keep the console loading.
2. A read that does not finish stops claiming to be updating: after a budget the
   label states what is true, the displayed frame stays, and no duplicate read is ever
   queued behind it. A retired read is retried no more than once per budget.
3. The console never reports a data-retention reason for a client-side failure; the
   coverage footer and the device strip agree.
4. `enabled, not answering` carries one honest cause line, from the client's own
   observation plus systemd's restart/failure evidence.
5. No new data contract, no daemon or wire change, no new runtime dependency.

## 3. Non-goals

- Launch and Range stay disabled with their existing A22 reasons
  (`src/appio/ui/formatting.py:12-13`, `:78-90`); the launch window is genuinely not stored.
- Retention, cadence, sampling and the `not_retained` data semantics are unchanged.
- The five fixed state strings (`off`, `on, answering`, `enabled, not answering`,
  `on, needs activation`, `unavailable`) and every other ratified vocabulary stay
  byte-identical.
- No new `UnavailableReason` token: the contract literal keeps its four members.
- Recovering the daemon itself (restart policy, socket ownership) is out of scope.
  The window reports the evidence; it does not repair the service.
- Reclaiming a worker thread that never returns. Python cannot interrupt a running
  future; the console reports that truthfully instead of claiming to fix it.

## 4. Decisions taken with the user

| # | Decision | Choice |
| --- | --- | --- |
| D1 | Scope | All three faults: permanent spinner, fabricated reason, and the boxed banner |
| D2 | Banner content | The ratified state string stays; a cause line is added from the socket observation **and** systemd restart/failure evidence |
| D3 | Banner layout | Option A: the cause is a second, muted line under the state inside the banner strip |
| D4 | Slow read | Bounded wait (30 s), honest stalled label, retry rate-limited to one per budget |

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

### 5.2 A local sample never discards the read in flight

A tick always commits a new sampler checkpoint, and the query layer reads that
checkpoint and the collector interval to compute a window's available cutoff
(`QueryService._available_cutoff`, `src/appio/query.py:300-310`). A tick that
observed no application therefore still moves a query-visible value, so "wrote no
rows" is not evidence that history is unchanged; the row count is a diagnostic, not a
staleness test.

`CollectorCoordinator.tick_local` stops invalidating from inside the tick and returns a
committed-advance flag. The window owns the timing, because only it knows whether a
read is pending:

- nothing pending → `history.invalidate()` immediately (today's behaviour);
- a preparation pending → set `_refresh_after_apply = True` and leave the generation
  alone; when that frame is applied, invalidate once and prepare the next scope.

Guarantee: with local collection active at any cadence, a pending read is retired at
most once per applied frame — never once per sample. Today it is retired per sample;
that is the "one replacement per second" the 2026-11-01 design accepted (§1.1) and
this document replaces. The equivalent rule for a probe delta is §5.1: only a
staleness-key change retires a pending read.

### 5.3 A slow read stops claiming to be updating

New window fields: `_pending_since: int | None`, `_stalled: bool`, `_retry_after:
int | None`, `_refresh_after_apply: bool`, and `PENDING_BUDGET_NS = 30 * 10**9`.

- Accepting a preparation sets `_pending_since = now` and `_stalled = False`.
- In `_tick_target`, when `_pending` is not None and
  `now - _pending_since >= PENDING_BUDGET_NS`, set `_stalled = True`. The pending read
  is **not** discarded: a submitted future cannot be interrupted, so abandoning a read
  that is still working only guarantees the work is wasted.
- Labels, both in the existing `history_state` label:
  - stalled with the read still in flight:
    `history read has not completed (<view_state>); the reader is still busy`
  - after the router retires the generation (`_history_retired()`): the pending read is
    cleared, `_retry_after = now + PENDING_BUDGET_NS`, and the label reads
    `history read has not completed (<view_state>); retrying in <n> s`
- A retirement that leaves the worker busy records the wanted target in
  `_deferred_target` instead of forgetting it: `_tick_target` submits exactly one
  preparation when, and only when, `local_read_in_flight` is false, the retry floor has
  elapsed, and `_deferred_target` is set. The busy label stays until a replacement is
  actually accepted, so it never claims a retry that has not happened.
- No preparation is submitted while `local_read_in_flight` is true (new read-only
  router property over `_local_future`, `src/appio/history_router.py:127`, `:759`,
  `:769`), on every path — tick, refresh, retirement, and explicit request. A wedged
  read therefore cannot accumulate duplicates behind it, and an explicit refresh cannot
  discard the deferred target.
- `_stalled`, the stalled label, and `_retry_after` are cleared by an applied frame, by
  an invalidation followed by a fresh preparation, and by an explicit refresh.
- The Files pane has its own caption (`files_updating`, today
  `updating to <kind>`, `src/appio/ui/files_view.py:759-769`). It gains a real stall
  input — `FilesView.set_stalled(stalled: bool, text: str)` — driven by the same
  `_stalled`/`_retry_after` transitions, so a retained table is never merely labelled
  "updating" while the console has already admitted the read is slow.
- Accepted residual: a read that never returns leaves the console saying
  `the reader is still busy`. That is the truth, and it is a bounded *claim*, not a
  bounded read. Reclaiming a wedged worker thread is explicitly out of scope (§3);
  the window never reports a repair it cannot perform.

The deadline is a clock deadline, not scope identity: `_current_window()` advances its
end to the next `RAW_BUCKET_NS` boundary (`:899-916`), so a pause keyed on the scope
would never fire.

### 5.4 A client failure is not a retention reason

Two different facts share one boolean today.

- `_not_retained` means *the data says it was not retained*. It is set from row
  coverage (`src/appio/history_router.py:883-886`) and from local materialization when
  a persisted interval cannot be corrected or a legacy aggregate carries no ownership
  receipt (`:641`, `:646`). Both stay exactly as they are.
- `daemon_answer_error` is new and display-only, a typed record
  `{kind, reason}` where `kind` is one of `remote_error`, `unavailable`, `rejected`,
  `transport`, `internal`. It is set only in `ready()` where a *remote query* settles
  (`:886-901`), classified by the client's own exception types
  (`src/appio/iodclient.py:168-190`):

| Exception from the remote query | `kind` |
| --- | --- |
| `RemoteError` (daemon answered with an error envelope) | `remote_error` |
| `ClientUnavailable` (no verified, answering connection) | `unavailable` |
| `RequestRejected` (pending cap, oversized reply, refused selector, expired deadline) | `rejected` |
| `ConnectionLost` | `transport` |
| anything else | `internal` |

  It is cleared by `prepare()` (`:742-769`). Retirement is not a daemon failure:
  `invalidate()` (`:413-421`), `WindowUnavailableError` and a cancelled query set
  neither flag, so navigating away can never be reported as a dead daemon.

The three stampings that write `unavailable_reason="not_retained"` for a client
failure (`:916`, `:945`, `:997`) stop doing so. A genuine `not_retained` still arrives
through `_not_retained` alone, so pruned history keeps its contract reason and its
typed value.

Rendering: `build_overview` gains one optional keyword `coverage_diagnostic: str | None
= None` and `OverviewModel` gains the matching field (`src/appio/ui/render_model.py:420-430`;
interface amendment ratified in §9). `main_window._device_unavailable_text` reads the
same string, so the coverage footer and the device strip say the same thing.

Wording is chosen by kind, so that a local refusal is never blamed on the daemon:

| `kind` | Rendered clause |
| --- | --- |
| `remote_error` | `daemon reported an error (<reason>)` |
| `unavailable` | `no answering daemon (<reason>)` |
| `rejected` | `history query failed (<reason>)` |
| `transport` | `connection lost before the answer (<reason>)` |
| `internal` | `history query failed (<reason>)` |

`_coverage_text` **composes** the two independent causes instead of letting one hide
the other: the contract reason first when the data has one, then this clause —
`reason: not_retained; daemon report: remote_error (… )`. When only one cause exists,
only that one is printed.

### 5.5 One honest cause line under the state

Probe: `UNIT_SHOW_ARGV` additionally requests `SubState`, `Result` and `NRestarts` on
the same `systemctl show` invocation. `parse_unit_facts` keeps failing closed on a
malformed line or a duplicated key and still requires `UnitFileState` and
`ActiveState`; the three new properties are optional, and a malformed optional value
is ignored rather than poisoning the required facts.

`ServiceFacts` gains three defaulted fields (`sub_state: str | None = None`,
`result: str | None = None`, `restarts: int | None = None`), so every existing
positional construction keeps its meaning.

New pure function in `src/appio/ui/collector_window.py`:

```python
def silence_cause(facts: ServiceFacts, client_state: ClientState | None) -> str | None
```

It returns `None` unless one of two situations holds: the state is
`enabled, not answering`, or the state is `off` while `facts.enabled is True` **and**
systemd reports failure evidence (`SubState == "failed"` or a `Result` other than
`success`/`none`). The second case is the daemon that was enabled, crashed, and was
given up on: `_ACTIVE_STATES` maps `failed` to `inactive`
(`src/appio/ui/collector_window.py:139-145`) and `decide_owner` calls that `off`
(`src/appio/collector_owner.py:56-63`), so without this case the crash-after-give-up
daemon is reported as a normal stopped service. For that case the systemd clause leads
(`enabled but not running: failed: <result>`); otherwise the function joins one socket
clause and one systemd clause when each is available. The clauses are built
only from observations the client actually publishes (`ClientState.last_error`,
`iodclient.py:910-979`, `_publish` `:1325-1333`):

| Observation | Clause |
| --- | --- |
| `verified` and not `answering` (status answered, `last_error is None`) | `daemon status reports not answering` |
| `last_error == "daemon_absent"` | `socket: nothing listening at /run/appio/iod.sock` |
| `last_error == "connect_failed"` | `socket: connect failed` |
| `last_error == "path_unverifiable"` | `socket: socket path could not be verified` |
| `last_error == "peer_unreadable"` | `socket: peer credentials unreadable` |
| `last_error == "daemon_not_answering"` (connected, silent for `NO_ANSWER_NS`) | `socket: connected, no answer within 5 s` |
| `last_error == "connection_lost"` (socket error mid-read) | `socket: connection lost` |
| `last_error == "transport_lost"` (peer closed the stream) | `socket: transport closed by the daemon` |
| any `PeerRejected.reason` token (e.g. `socket_not_connected`) | `socket: <token>` |
| no client observation at all | clause omitted |

The client's silence deadline is `NO_ANSWER_NS` (5 s, `src/appio/iodclient.py:109-110`);
`CONNECT_DEADLINE_NS` (2 s, `:112-113`) bounds an incomplete connect and is not a
silence budget. The design maps the tokens the client publishes and does not invent a
"timed out after 5 s" clause for a state the client never names.

| systemd evidence | Clause |
| --- | --- |
| `NRestarts > 0` | `<n> restarts` |
| `Result` not `success`/`none` | `failed: <result>` |
| `SubState == "auto-restart"` | `restarting` |

Rendering: the banner strip's left side becomes a vertical column
(`QHBoxLayout(status_column, settings_button)` where `status_column` is
`QVBoxLayout(banner_status, banner_detail)`, `src/appio/ui/collector_window.py:524-533`).
`banner_detail` is hidden when `silence_cause` returns `None`, wraps rather than
elides, and is updated in `_render` beside `banner_status.setText(state)`
(`:884-892`). The five state tokens are untouched, so `settings.status_label` and
every existing assertion keep working.

### 5.6 Data flow after the change

```text
probe facts ──► decide_owner ──► HistoryKey ──┬─ same ──► no invalidate (frame survives)
                                              └─ differs ──► invalidate ──► reprepare
local tick ──► committed advance ──┬─ nothing pending ──► invalidate
                                    └─ read pending ──► _refresh_after_apply ──► one invalidate on apply
pending read ──► 30 s budget ──┬─ still running ──► "the reader is still busy" (kept, not discarded)
                               └─ router retired it ──► "retrying in <n> s" ──► one reprepare
remote failure ──► daemon_answer_error ──► "reason: <per-kind clause from §5.4>"
```

### 5.7 State fields

| Field | Owner | Meaning |
| --- | --- | --- |
| `_history_key` | `CollectorCoordinator` | key the current stored history was prepared under |
| `_pending_since` | `CollectorWindow` | clock of the accepted preparation |
| `_stalled` | `CollectorWindow` | the pending read passed its budget; the label says so |
| `_retry_after` | `CollectorWindow` | earliest next preparation after a stall |
| `_deferred_target` | `CollectorWindow` | the scope to prepare once the busy worker is free |
| `_refresh_after_apply` | `CollectorWindow` | a sample landed while a read was pending; invalidate once the frame applies |
| `local_read_in_flight` | `HistoryRouter` | read-only: the single worker is still running a local read |
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
| Read still running when the budget expires | label says `the reader is still busy`; the read stays pending and nothing is submitted behind it |
| Read never returns | the same honest label stands; no repair is claimed and no duplicate is queued |
| Router retirement during a stall pause | pause cleared; the label does not claim a daemon failure, because retirement is not one |
| Remote query fails after a local answer arrives | local rows stay; the footer shows the data reason, the per-kind client clause, or both when both are true |

## 7. Test strategy

Every guarantee below is written to fail on today's code; a test that would pass
before the change is not evidence and is not accepted.

### 7.1 Coordinator (`tests/p3/test_collector_owner.py`)

1. `reconcile(SILENT)`, then `replace(SILENT, active="active")` — the crash-loop delta:
   one invalidate for the first call, none for the second, `view_state` unchanged.
2. A genuine key change (`SILENT` → `OFF` → `ON`) invalidates exactly twice and the
   owner transitions as today.
3. `tick_local` reports an advance on every committed tick and does not invalidate by
   itself; the window's coalescing is asserted in §7.2.

### 7.2 Window liveness (`tests/p3/test_collector_window.py`)

Each liveness claim is proved twice: once against the **real**
`CollectorCoordinator` with a counting history double — that arm is red on today's code
and is the regression — and once at the window against a coordinator double that
invalidates exactly where the fixed production coordinator does, which pins the
window's coalescing. Neither arm can stand in for the other.

4a. Real coordinator: ten `reconcile` calls alternating `SILENT` and
    `replace(SILENT, active="active")` produce exactly one invalidate in total (the
    first call), and `view_state` never moves again. Today the same storm invalidates
    ten times.
4b. Window: with that same coordinator shape, a held read survives the storm, the
    generation does not change, and no second prepare is issued.
5a. Real coordinator: `tick_local` on a committed tick returns an advance and performs
    no invalidate. Today it invalidates.
5b. Window: local collection active and one read held; every refresh commits a frame.
    The read is never retired during the storm (`_pending` unchanged, no second
    prepare), `_refresh_after_apply` is set, and exactly one replacement is prepared
    after the frame applies.
6. Budget expiry with the read still in flight: `history_state` reads
   `history read has not completed (loading); the reader is still busy`; `_pending` is
   unchanged and nothing is submitted.
7. Retirement while the worker is busy: the router retires the generation, the busy
   label stays (no retry is claimed), `_deferred_target` is set, and exactly one
   preparation is submitted once the worker is idle and the floor has elapsed.
8. Clearing: an applied frame, and an invalidation followed by a fresh preparation,
   each clear `_stalled`, the stalled label, and `_retry_after`.
9. No duplicates: with `local_read_in_flight` true, five ticks and one explicit refresh
   submit zero preparations and do not discard `_deferred_target`.

### 7.3 Router and rendering (`tests/p4/test_file_history_router.py`, `tests/test_ui.py`)

10. Classification, one case per class: `RemoteError` → `remote_error`,
    `ClientUnavailable` → `unavailable`, `RequestRejected` → `rejected`,
    `ConnectionLost` → `transport`, an unrecognised failure → `internal`. Each leaves
    `coverage.unavailable_reason` exactly as the data says, and a pruned-row query
    still yields `not_retained`.
11. Retirement is not a daemon failure: `invalidate()`, `WindowUnavailableError`, and a
    cancelled query set neither `_not_retained` nor `daemon_answer_error`.
12. Composition: local retention loss **and** a remote failure in the same generation
    print both causes; each cause alone prints only itself.
13. Wording per kind in `_coverage_text` and the device strip, including
    `RequestRejected("too_many_requests")`, whose line must not blame the daemon.

### 7.4 Banner and Files (`tests/p3/test_collector_window.py`, `tests/test_ui.py`)

14. `parse_unit_facts` accepts the five-property output; ignores a malformed or absent
    optional property while still returning the required facts; still fails closed on a
    duplicated key or a malformed line.
15. `silence_cause` for every row of §5.5's socket table, including
    `daemon_not_answering`, built from the client's published state rather than from a
    hand-assembled token list.
16. The enabled-but-failed case: an enabled unit reported `inactive` with `Result` a
    failure produces `off` **with** the `enabled but not running` cause; an enabled unit
    reported `inactive` with no failure evidence produces `off` with no cause line.
17. Layout: `banner_detail` sits below `banner_status` inside the status column, is
    visible only in the states of §5.5, and its text fits without clipping at the
    shipped window width and at the window's minimum width.
18. Files during a stall: on the real `FilesView` with retained rows,
    `set_stalled(True, text)` replaces the generic `updating to <kind>` caption
    (`src/appio/ui/files_view.py:759-769`), and `set_stalled(False, "")` restores it;
    asserted alongside test 6's console label, not on a mock branch.

### 7.5 Existing tests that must change

- `test_pending_history_changed_ownership_reprepares_once`
  (`tests/p3/test_collector_window.py:1208`) drives retirement through a fake knob
  (`coordinator.invalidate_on = "reconcile"`). It keeps its ownership assertions; the
  invalidation assertion moves to test 2.
- `FakeCoordinator.tick_local` returns the advance flag production now returns, and
  `ControlledCoordinator` is replaced by the production-compatible double described in
  §7.2, so no window test can pass by asserting behaviour the fake never performs.
- `tests/p3/test_collector_owner.py`'s `History` double gains an invalidate counter so
  tests 1–3 assert calls, not merely absence of failure.
- The files-view double in `tests/p3/test_collector_window.py` forwards
  `set_stalled` to the real `FilesView` state, so test 18 asserts the production widget
  path rather than a caption a double invented.

### 7.6 Gates

Full suite in the pinned environment, compared with the recorded baseline
`2652 passed, 1 failed (missing `clang` in tests/p4/test_bpf_build.py), 2 skipped`:
no new failures.

Two separate live checks on a host with the daemon installed, because stopping the
unit satisfies neither on its own:

1. **Cause line.** A deliberately crash-looping daemon (a unit whose exec keeps failing
   under `Restart=on-failure`) must render `enabled, not answering` with the socket
   clause, and after the start-limit gives up it must render `off` with
   `enabled but not running: failed: …`.
2. **Stall.** The slow-read label is exercised by holding the reader (a large store or a
   database lock), not by stopping the daemon; stopping the daemon alone leaves the
   local reader healthy. The check asserts that the console leaves `updating (loading)`
   for `the reader is still busy` within the budget while the frame stays on screen.

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Over-eager no-invalidate leaves stale data on screen | The key contains every input that selects a source; a genuine source change still invalidates |
| A 30 s budget masks a permanently broken read | The label is explicit and rate-limited; the data shown is still labelled `incomplete` |
| Treating a "quiet" tick as no advance hides a moved checkpoint | No row-count test is used for staleness; a tick always counts as an advance and the window coalesces instead |
| A longer banner strip crowds a narrow window | The cause line wraps inside the existing strip; the mockup was checked at the shipped width |
| Removing the fabricated reason hides a real gap | The contract reason still comes from rows; only the client-failure case changes wording |

## 9. Relationship to earlier design and the mockup record

### 9.1 Decisions this document replaces

`2026-11-01-appio-console-charts-design.md` §5.5 accepted one replacement preparation
per sampled frame and treated unconditional `reconcile()` invalidation as given. This
document replaces that single decision: a sampled frame never discards a pending read
(it schedules one refresh after the frame applies), and a probe delta invalidates only
when the staleness key changes. Everything else in that design —
target/displayed/pending discipline, transient scopes, chart rendering, A22 launch
reasons — stands unchanged.

### 9.2 Interface amendments this document ratifies

These are deliberate, additive amendments to the ratified 2026-11-01 interface, not
side effects. The technical specification (Step 4 of this run) restates each one with
its exact signature, validation rule and rejection cases.

1. `OverviewModel` gains `coverage_diagnostic: str | None` and `build_overview` gains
   the matching keyword-only argument with default `None`. The field is display text;
   it is validated like `share_unavailable`/`series_unavailable` (string or `None`) and
   never replaces a contract reason.
2. `HistoryRouter` gains the read-only property `local_read_in_flight: bool`. It is an
   internal presentation seam, not a wire or data contract.
3. `ServiceFacts` gains three defaulted evidence fields (`sub_state`, `result`,
   `restarts`). Positional construction keeps its meaning, so no existing call site
   changes.
4. `CollectorCoordinator.tick_local` returns a committed-advance flag instead of
   returning `None`. Every existing caller that ignores the return value keeps working.
5. `parse_unit_facts` accepts three optional properties while keeping its required set
   and its fail-closed rules for malformed lines and duplicated keys.

Layout authority for §5.5 is `assets/2026-11-03-silent-daemon-cause.png`: three
candidate layouts (A stacked detail line, B single line with separators, C chips on
the right); **A** is chosen (D3). Behaviour follows this text where the mockup is
silent.
