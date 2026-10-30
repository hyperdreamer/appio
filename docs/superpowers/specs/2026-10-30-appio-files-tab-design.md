# appio Files tab — honest window and honest label

Status: draft for review
Date: 2026-10-30
Supersedes: nothing. Builds on the merged P4 work at `2874841`.

## Background

The Files tab has never rendered on this host, and the window's header has twice
misreported a healthy daemon. Both were investigated live; the measurements below are
from the real database and the running daemon, not from fixtures.

**Why the tab is empty.** File collection writes hourly rows. The query layer originally
refused any window whose coverage was incomplete, then served the contiguous suffix, then
the largest contiguous interval. Each step was necessary and none was sufficient, because
the *view* never received the rows:

```
file data is hourly                  → the window must be hour-aligned
the store carries ~710 legacy gaps   → every hour-aligned window contains one
gap-free stretch before the watermark: 614 s → below hourly granularity
a 614 s range                        → unsupported_window, 0 rows
router required provenance across the whole requested window → refusal
router now checks the served interval → still refused while any gap is present
```

One unmerged commit from the previous run (`dd8b042`, 2214 tests green, 4/4 mutations
killed) closes the last of these: it serves an hour-aligned interval across known,
countable gaps when ownership is unambiguous, no overlap uncertainty falls inside the
served interval, and every unproved segment is fully covered by known gaps.

**Why the label lies.** The header read `enabled, not answering` while the daemon
answered the GUI's own status round-trip 10/10 at a 4 ms median and the table was
populated from the same daemon. The status refresh discarded the verified state of the
live connection in favour of a separate one-shot probe result.

**Why the field names matter.** The served interval is currently carried in
`coverage.unknown_owner_overflow.window`. That field genuinely means "the interval that
was served", but its name says "unknown owner overflow", and reading the code suggests
otherwise. This confusion cost hours during diagnosis.

## Goals

1. The Files tab renders retained per-file rows for a requested window whose coverage is
   partial, disclosing exactly what it served.
2. The header reports the daemon state that the live connection actually observes.
3. The served interval is a first-class, honestly named part of the coverage model.
4. No honesty rule weakens: partial is never presented as complete, unavailable is never
   an empty table, ownership is never ambiguous silently.

## Non-goals

- No schema change, no migration, no new table.
- No change to the wire protocol or the HELLO/FLIP_DRAIN/CLOSE ABI.
- No redesign of the Overview, the daemon, or the collection cadence.
- No new user-facing copy beyond the disclosure sentence and label states that already
  exist.

## The contract

> The window must not misreport the daemon, and must not misreport its data.

Clause 1 — data. For a requested window the Files tab either renders the rows of the
interval it actually served, with that interval and its gap count disclosed, or renders
unavailable. There is no third state.

Clause 2 — daemon. The header derives "answering" from the live connection's verified
state. The one-shot probe remains only to distinguish enabled/disabled; it may not
overrule a live verified connection.

## Component changes

| Unit | Responsibility | Change |
| --- | --- | --- |
| `QueryService.file_coverage` | report what was served | adds `served_interval: (start_ns, end_ns) \| None` to the coverage model |
| `_largest_contiguous_file_interval` | compute the interval | unchanged |
| `HistoryRouter` | provenance | reads `coverage.served_interval`; keeps every existing refusal |
| `FilesView` | render | disclosure reads `served_interval` plus a gap count inside it |
| `CollectorWindow` | status | "answering" from live client state; refrozen on each refresh |

`served_interval` is `None` when no interval could be served. It equals the requested
window when coverage is complete, so a complete window keeps its existing behaviour.

## Data flow

Daemon: sample tick → T/D commit → F drain → hourly rows with coverage. UI: the client
stream drives the label; the `HistoryRouter` over the local store drives the table. The
disclosure is computed once per result, from the coverage the query returned, never from
the requested window. The label is recomputed on every refresh, so a transient failure
cannot latch.

## Honesty and error handling

- No coverage for the app in the window → unavailable, never an empty table.
- Ownership ambiguous, or the app belongs to another store → refuse.
- Served interval unproven and not fully covered by known non-overlap gaps → refuse.
- Partial coverage → `complete=False`, disclosure names T0, T1 and the number of gaps
  inside the served interval.
- Overlap uncertainty inside the served interval → refuse; overlapping evidence is not
  servable.
- Label never claims answering without a verified stream, and never claims silence while
  one exists.

## Adoption rule for `dd8b042`

Adoption is gated on a read-only audit of that single commit: contract fidelity against
this spec, its four claimed refusal pins, and mutation probes on the mechanisms it claims
to protect. On pass, it is adopted into the integration branch before the remaining tasks.
On failure, the served-interval behaviour is implemented fresh in this run and the audit
findings are recorded. Adoption is never granted on the strength of a green suite alone:
three separate "green" changes in the preceding run were wrong on the live host.

## Testing strategy

1. **Audit gate** (read-only): `dd8b042` against this spec; explicit pass/fail.
2. **Unit**: `served_interval` is the largest contiguous, hour-aligned interval inside the
   requested window; `None` when nothing is servable; equals the window when complete.
3. **Router**: provenance over `served_interval`; each refusal pinned by name.
4. **View**: rows render with rows, bounds and gap count; unavailable when nothing serves.
5. **Label**: a verified live connection yields "answering" and cannot be overridden by a
   silent probe; a lost connection clears it.
6. **Acceptance on the real database**: an offscreen render against a snapshot of the live
   store shows rows for an hour-aligned window with the served bounds and gap count, and
   the label reads answering while the stream is verified.

Every pin is falsified by mutation before it is trusted: revert the mechanism, observe the
named test fail, restore byte-identically, observe it pass.

## Risks

- **Gap-heavy stores serve short intervals.** A store whose history is mostly gaps may
  serve only minutes of data. That is honest but not useful; the mitigation is disclosure,
  not wider claims.
- **Field promotion touches call sites.** `unknown_owner_overflow.window` is read in more
  than one place; the promotion must keep those semantics or convert them deliberately.
- **Label source of truth.** If the live client is disconnected while the daemon is up, the
  label must distinguish "not answering" from "not connected" without inventing a state.
