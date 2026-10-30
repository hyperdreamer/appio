# appio Files tab — honest window and honest label

Status: revision 2 — addresses design review 1 (4 blockers, 4 majors, 1 minor)
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

**Why the label misreports.** The header read `enabled, not answering` while the table was
populated. Review established that the refresh **already** promotes a verified, answering
live connection over a silent one-shot probe. The actual asymmetry is the other direction:
an unverified or disconnected live client does **not** clear facts supplied by a successful
probe, and the disconnect case was never resolved.

**Why naming matters.** The served interval is carried in
`coverage.unknown_owner_overflow.window`. That field does mean "the interval served", but
its name says otherwise, and that confusion cost hours during diagnosis.

## Goals

1. For a requested window, the Files tab renders the rows of the interval it served, and
   discloses that interval and the gaps inside it.
2. The header reports daemon health truthfully in **both** directions.
3. The served interval is exposed under an honest name without changing the wire contract.
4. No honesty rule weakens.

## Non-goals

No schema change, no migration, no new table, no wire-protocol or ABI change, no Overview
redesign, no collection-cadence change, no new user-facing copy.

## The contract

> The window must not misreport the daemon, and must not misreport its data.

**Rendering outcomes.** Four, each with a predicate that is exhaustive because the
zero-row partial case is its own outcome:

| Outcome | Predicate | Disclosure |
| --- | --- | --- |
| `rows` | a served interval exists and holds ≥1 row | bounds T0/T1, gaps inside the served interval (where provable), gaps inside the requested window |
| `empty_proven` | served interval equals the requested window, coverage is `complete`, **zero rows, and no active search filter** | bounds; "no applications observed in this window" |
| `no_matches` | zero rows while a search filter is active | bounds; "no retained files match <filter>" — proven absence of matches, never a claim that the app had no activity |
| `empty_partial` | a served interval exists, coverage is incomplete, **zero rows** | bounds; "no retained file activity in the served interval"; coverage incomplete with both counts |
| `unavailable` | no servable interval, or any refusal fires | reason |

The zero-row partial case is not hypothetical: `HourlyAccumulator` supports monitored apps
with no supported file observations (`files.py:309`), producing coverage with rows=0,
`complete=False`, no refusal, and a served interval equal to the request. That result matched
none of revision 2's three predicates; `empty_partial` is its terminal outcome and is pinned
independently of missing-coverage.

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

**Label rule — witness merging, then existing precedence.** "answering" describes the daemon
and has two independent witnesses: the live client's verified stream and the one-shot status
probe. Witness merging and display state are separate steps, and the existing owner logic keeps
authority over the final label:

1. **Merge witnesses** into one `answering` fact: positive if either witness is positive,
   negative only when both are. The live verified state, when present, supplies
   `pending_activation`; a disconnected GUI against a healthy daemon therefore still yields a
   positive `answering` fact.
2. **Derive display state from the existing precedence**, unchanged: `off` for an absent or
   inactive service; `on, needs activation` when a verified answering daemon has a pending
   activation; `on, answering` otherwise when the fact is positive; `enabled, not answering`
   only when the fact is negative. `unavailable` remains reachable for unknown facts.
3. **The merged facts reach `coordinator.reconcile` unchanged**, so collector ownership keeps
   its current behaviour; the label change may not alter ownership.

The label is recomputed on every refresh so no value latches.

**This clause is preservation unless a failing case is demonstrated.** A controlled replay of
`CollectorWindow.refresh` on unchanged code yields all four health transitions already (live
positive/silent probe → answering; EOF with a healthy probe → answering; both negative → not
answering; reconnection → answering), and
`test_verified_status_round_trip_clears_silent_header_while_window_open` already covers live
promotion and EOF. Revision 2 misdescribed required behaviour as the defect. The implementer
must therefore either exhibit a concrete failing observation (a state the current precedence
produces that misreports the daemon) or treat this clause as a preservation pin, and the design
summary must say which. No repair is claimed without an observed failure.

## Component changes

| Unit | Responsibility | Change |
| --- | --- | --- |
| `QueryService.file_serve_facts` | report what was served | **new local method** returning `FileServeFacts(served_interval, gaps_in_served_interval, gaps_in_requested_window)`; not transported |
| `QueryService.file_coverage` | coverage as today | unchanged shape; `served_interval` exposed as a **computed property** derived from existing fields |
| `QueryService.file_applications`, `file_table` | select using served bounds | unchanged behaviour, conversion noted |
| `_largest_contiguous_file_interval` | compute the interval | unchanged |
| `HistoryRouter` | provenance | reads `served_interval`; keeps every refusal; keeps `_gap_ranges` scoped to the requested window for process coverage |
| `FilesView` | render | three outcomes above; disclosure reads `FileServeFacts` |
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
6. **View outcomes**: `rows`, `empty_proven`, `unavailable`, each by its own predicate;
   disclosure states bounds and both counts with named scopes.
7. **Label**: verified stream + silent probe → answering; EOF on the stream while the probe
   still answers → answering; both negative → not answering; reconnection on the same open
   window restores answering. Mutations separately freeze the refresh and retain stale facts.
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
