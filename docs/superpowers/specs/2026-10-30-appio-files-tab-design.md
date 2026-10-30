# appio Files tab — honest window and honest label

Status: revision 6 — addresses design reviews 1-3. The label clause is now a
preservation pin, not a repair: review 3 showed the behaviour I called a defect is
required by tests and produced by unchanged code.
Date: 2026-10-30
Supersedes: revision 1 of this document. Builds on the merged P4 work at `2874841`.

## Background

The Files tab has never rendered on this host, and the header has misreported the daemon.
Both were investigated live; the measurements below come from the real database and the
running daemon.

**Why the tab is empty.** File rows are hourly. Successive fixes made the query refuser,
then suffix-only, then largest-contiguous-interval — each necessary, none sufficient:

```
file data is hourly                     → the window must be hour-aligned
~710 legacy gaps in the store           → every hour-aligned window contains one
gap-free stretch before the watermark   → 614 s, below hourly granularity
router provenance over whole window     → refusal
router provenance over served interval  → still refused while a gap is present
```

The unmerged `dd8b042` serves an hour-aligned interval across known, countable gaps when
ownership is unambiguous, no overlap uncertainty falls inside the served interval, every
unproved segment is fully covered by known non-overlap gaps, and those segments are
hour-aligned.

**About the label.** The header was observed reading `enabled, not answering` while the table
was populated. Two reviews then established that the refresh already promotes a verified,
answering live connection over a silent probe, that the activation probe override is deliberate,
and that unchanged code produces every health transition the earlier drafts called missing. The
label is therefore treated here as **behaviour to preserve**, with two uncovered transitions
pinned. A repair would need a concrete failing observation, and none has been exhibited.

**Why naming matters.** The served interval is carried in
`coverage.unknown_owner_overflow.window`. That field does mean "the interval served", but
its name says otherwise, and that confusion cost hours during diagnosis.

## Goals

1. For a requested window, the Files tab renders the rows of the interval it served, and
   discloses that interval and the gaps inside it.
2. The header's existing truthful behaviour is preserved and its two uncovered transitions are
   pinned; no precedence or ownership change is made.
3. The served interval is exposed under an honest name without changing the wire contract.
4. No honesty rule weakens.

## Non-goals

No schema change, no migration, no new table, no wire-protocol or ABI change, no Overview
redesign, no collection-cadence change, no new user-facing copy.

## The contract

> The window must not misreport the daemon, and must not misreport its data.

**Rendering outcomes.** Five, evaluated **in order**, so exactly one applies to any result:

| # | Predicate (first match wins) | Disclosure |
| --- | --- | --- |
| 1 | no servable interval, or any refusal fires | `unavailable`, with the reason |
| 2 | rows > 0 | `rows` — bounds, gaps, and the filter in effect when one is present |
| 3 | a search filter is active and rows = 0 | `no_matches` — bounds, gaps, and the filter in effect |
| 4 | no filter, rows = 0, coverage complete | `empty_proven` — bounds; "no retained file activity for this application in this window" |
| 5 | no filter, rows = 0, coverage incomplete | `empty_partial` — bounds; "no retained file activity in the served interval"; coverage incomplete with both counts |

Ordering is what makes the table exclusive: a filtered zero-row result matches rule 3 and can
never also match 5, because 5 requires the absence of a filter. Revision 3 listed these as
unordered predicates and consequently let one result satisfy two of them.

`empty_proven` deliberately speaks about **this application's file result**, not about whether
applications were observed. `HourlyAccumulator` supports a monitored app with no supported file
observations, so complete coverage with zero rows is a statement about that app's retained file
activity while other apps may have been busy. The phrase "no applications observed" belongs only
to the separate empty-application-selector case, which this spec does not change.

`no_matches` claims only what is knowable: **no retained file matches this filter**. It does not
claim that a retained file population existed and was filtered away — `file_coverage` reports
aggregate bytes, not an unfiltered retained-row count, so that stronger claim is not available.
The observed byte totals are shown separately, exactly as `_observed_text` already does.

The zero-row partial case is not hypothetical: `HourlyAccumulator` supports monitored apps with
no supported file observations (`files.py:309`), producing coverage with rows=0, `complete=False`,
no refusal, and a served interval equal to the request. `empty_partial` is its terminal outcome.

**Per-route disclosure.** The Files tab is fed either by the local store or by the daemon over
the history route. The served-gap count needs the gap rows, and the remote consumer receives
only `FileTable` + `FileCoverage` — two different gap layouts encode identically. Therefore the
design states what each route can prove, rather than assuming both can prove the same thing:

| Route | Bounds | Gaps inside served interval | Gaps inside requested window |
| --- | --- | --- | --- |
| local store | yes | yes — `file_serve_facts` computes it from the gap rows | yes — existing scalar |
| daemon / remote | yes | **no** — stated as unavailable for that route | yes — existing scalar |

No wire change is made to enable the remote column. The disclosure names the scope it can
prove and says plainly when a scope is unavailable on that route, which is consistent with the
rest of this product: no number is invented and none is silently omitted.

**Label clause — preservation, not repair.** Review 3 established that what revision 1 and 2
called the label defect is in fact required behaviour, already implemented and already tested:

- `CollectorWindow.refresh` promotes a verified, answering live connection over a silent probe;
- `display_state` deliberately lets the activation probe override `decide_owner` whenever
  `facts.enabled is not False`, so `on, needs activation`, `off` for an enabled-but-inactive
  service, and `unavailable` for unknown facts all remain reachable;
- `coordinator.reconcile` keeps using `decide_owner`, so display and ownership are **not** split;
- `test_current_link_status_requires_verification_and_preserves_activation` and
  `test_pending_activation_stays_visible_across_a_restart` pin the activation behaviour.

This design therefore **does not change** witness handling, display precedence, or ownership
reconciliation. It adds only these pins, on the two transitions the existing tests do not cover
(`test_verified_status_round_trip_clears_silent_header_while_window_open` starts from a silent
probe and stops at EOF):

**Which probe.** "Probe" below means the **unprivileged status-socket observation**
(`production_probe` / `SetupClient.refresh()`), which is what can produce
`daemon_verified=True, answering=True` (`collector_window.py:1006-1058`,
`setup_client.py:116-172`). It is **not** the root activation probe: `_apply_body` deliberately
discards that probe's answering claim (`collector_window.py:869-880`), so a pin written against it
could never observe the expected answer.

| Case | Sequence | Expected |
| --- | --- | --- |
| probe-only truth | verified connection → EOF on the live connection, status probe answered on the next three refreshes | answering throughout, because the status probe witnesses a healthy daemon |
| recovery | connected → EOF **and** status probe silent on one refresh (label becomes `enabled, not answering`) → status probe answers again | returns to answering on the following refresh, with no restart |

The recovery row is what makes the pins falsifiable: without it, freezing the refresh or retaining
stale answering facts produces an identical trace, since the label would read answering at every
stage. The sequence must therefore include a refresh where the label observably changes, and the
assertion must cover the transition back, not only the steady state.

Additionally the test asserts the *connection* becomes verified after reconnection, not merely that
the label reads answering, so a probe-only recovery cannot masquerade as a stream recovery.

If the implementer can exhibit a concrete state where the current precedence misreports the
daemon, that observation becomes its own finding and its own change; absent such an observation,
this clause is a preservation pin and the delivery summary must say so. No repair is claimed
without an observed failure.

## Component changes

| Unit | Responsibility | Change |
| --- | --- | --- |
| `QueryService.file_serve_facts` | report what was served | **new local method** returning `FileServeFacts(served_interval, gaps_in_served_interval, gaps_in_requested_window)`; not transported |
| `QueryService.file_coverage` | coverage as today | unchanged shape; `served_interval` exposed as a **computed property** derived from existing fields |
| `QueryService.file_applications`, `file_table` | select using served bounds | unchanged behaviour, conversion noted |
| `_largest_contiguous_file_interval` | compute the interval | unchanged |
| `HistoryRouter` | provenance | reads `served_interval`; keeps every refusal; keeps `_gap_ranges` scoped to the requested window for process coverage |
| `HistoryRouter.FileViewResult` | carry the facts to the view | **local dataclass** gains `served_gap_count: int \| None`, where `None` means "not provable on this route"; it is never transported |
| `FilesView` | render | the five ordered outcomes above; disclosure reads `FileServeFacts` plus `FileViewResult.served_gap_count` |
| `CollectorWindow` | status | two-witness rule; bounded refresh |

**Two mechanisms, deliberately.** `served_interval` is derivable from fields already on
`FileCoverage`, so it is a computed property. `gaps_in_served_interval` is **not** derivable
from those fields — it needs the gap rows and a connection — so it is produced once per query
by `file_serve_facts` and passed alongside the coverage. Neither is added to the transported
record: `FileCoverage` is encoded by dataclass field and decoded with an exact field-set
match, so a new field would break the wire contract this design keeps.

**Reader conversion table.** Every current reader of the nested window must be accounted for:

| Reader | Treatment |
| --- | --- |
| `QueryService.file_applications` (`query.py:952`) | uses served bounds; convert |
| `QueryService.file_table` (`query.py:987`) | uses served bounds; convert |
| `QueryService.file_coverage` (`query.py:1039`) | produces the interval; convert |
| `HistoryRouter._read_local_files` (`history_router.py:215`) | provenance over `served_interval` |
| `FilesView._render_coverage` (`files_view.py:707`) | disclosure |
| `FilesView._observed_text` (`files_view.py:788`) | must state whether its totals are whole-window or served-interval; convert |
| tests (`test_query.py`, `test_file_served_interval.py`) | converted with the fixtures in Testing |

A reader left on the nested window is a defect, not a compatibility choice.

## Data flow

Daemon: sample tick → T/D commit → F drain → hourly rows with coverage. UI: the client
stream and the one-shot probe feed the label; the `HistoryRouter` over the local store feeds
the table. The query layer computes the served interval and both gap counts once per result;
the view never recomputes them from the requested window.

## Honesty and error handling

- No coverage for the app in the window → `unavailable`.
- Ownership ambiguous, or the app belongs to another store → refuse.
- Served interval unproven and not fully covered by known non-overlap gaps → refuse.
- Overlap uncertainty inside the served interval → refuse.
- Partial coverage → `complete=False`, bounds and **both** gap counts stated.
- Complete coverage with zero rows → `empty_proven`, never `unavailable`.

Two gap counts exist because they answer different questions, and the previous scalar
answered only the requested-window question:

| Count | Meaning |
| --- | --- |
| `gaps_in_served_interval` | gaps between T0 and T1 — what the numbers actually omit |
| `gaps_in_requested_window` | gaps anywhere in the request, including regions outside the served interval |

The view names the scope it displays. Reusing the requested-window scalar as the served count
is precisely the mislabel review finding 2 identified.

## Adoption rule for `dd8b042`

Adoption is gated on a read-only audit whose scope is **the served-interval mechanism and the
disclosures it produces** — not this whole spec, which the candidate predates. The findings
choose the response; the design does not pre-commit to reimplementation:

- defect local and correctable → adopt after the repair, with the repair pinned;
- mechanism unsound → implement fresh in this run, recording why.

Two conditions are non-negotiable before adoption:

1. `_gap_ranges` must keep its requested-window scoping. `dd8b042` deletes that assignment,
   and those ranges feed **process-history** coverage (`history_router.py:650,735`), so gaps
   outside the request could mark covered process history incomplete. That is outside the
   Files contract and must be restored with a pin.
2. No claim of correctness rests on the suite being green. Three "green" changes in the
   preceding run were wrong on the live host.

## Testing strategy

1. **Audit gate** (read-only) with the scope above; explicit pass/repair/replace verdict.
2. **`served_interval`**: largest contiguous, hour-aligned interval inside the request;
   `None` when nothing serves; equals the request when complete.
3. **Gap counts distinguished**: request `[0h,6h)` while serving `[2h,4h)` with gaps at
   `[1h,2h)`, `[4h,5h)` and `[3h,4h)`. Assert served count `1` and requested count `3`. A
   fixture where request equals served cannot fail a reused requested count and is therefore
   not acceptable evidence.
4. **Router refusals**, each pinned independently so one predicate cannot satisfy several.
5. **Wire contract**: encoded field set and exact-match decode of `FileCoverage` unchanged.
6. **View outcomes**: all five, each reached by its own predicate in the stated order,
   including a filtered zero-row result that must match rule 3 and not rule 5; disclosure
   states bounds, both counts with named scopes, and `served_gap_count=None` on the remote
   route.
7. **Label (preservation)**: the two uncovered transitions above — EOF with a healthy probe,
   and reconnection on the same window — plus the existing activation tests staying green.
   Mutations freeze the refresh and retain stale facts to prove the pins bite.
8. **Real-database acceptance**: offscreen render against a snapshot shows rows with bounds
   and both counts; the label reads answering while the stream is verified.

Every pin is falsified: revert the mechanism, observe the named test fail, restore
byte-identically, observe it pass.

## Risks

- **Hour-aligned eligibility narrows what serves.** Selection serves whole-hour intervals and
  accepts unproved segments only when they are hour-aligned and fully covered by known
  non-overlap gaps. So the served interval is hour-granular, not minute-granular; a store
  whose recent history is a single sub-hour gap serves nothing until an hour boundary lands.
- **Property vs field.** Keeping `served_interval` computed protects the wire contract at the
  cost of deriving it repeatedly; the derivation must be cheap and pure.
- **Label authority.** The two-witness rule resolves both directions, but it makes "answering"
  a statement about the daemon, not about the GUI's stream. Copy that implies the latter must
  not be introduced later without revisiting this rule.
