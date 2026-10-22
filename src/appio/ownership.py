"""Half-open ownership selection shared by the writer and raw queries.

A counter delta cannot be apportioned in time. A delta straddling an excluded
interval is therefore excluded as a whole; prorating would invent byte counts.
"""
from __future__ import annotations

from appio.contracts import SQLITE_INT_MAX


def intersects(start, observed, timestamp, intervals):
    if start is None or start == observed:
        return any(left <= timestamp < right for left, right in intervals)
    return any(start < right and observed > left for left, right in intervals)


def subtract_intervals(start, end, excluded):
    cursor = start
    for left, right in sorted(excluded):
        if right <= cursor:
            continue
        if left >= end:
            break
        if cursor < left:
            yield cursor, left
        cursor = max(cursor, right)
    if cursor < end:
        yield cursor, end


def uncertain_intervals(connection, boot_id):
    return connection.execute(
        "SELECT from_mono_ns,to_mono_ns FROM gap WHERE boot_id=? AND reason='overlap_uncertain' "
        "ORDER BY from_mono_ns,to_mono_ns", (boot_id,),
    ).fetchall()


def ownership_exclusions(connection, boot_id):
    """A handover cutoff selects one source on each side, without a time split."""
    handovers = connection.execute(
        "SELECT from_source,to_source,cutoff_mono_ns FROM handover WHERE boot_id=? ORDER BY cutoff_mono_ns",
        (boot_id,),
    ).fetchall()
    excluded = {"gui": [], "iod": []}
    if handovers:
        start, owner = 0, handovers[0][0]
        for _, incoming, cutoff in [*handovers, (None, None, SQLITE_INT_MAX)]:
            for source in excluded:
                if source != owner and start < cutoff:
                    excluded[source].append((start, cutoff))
            start, owner = cutoff, incoming
    return excluded


def ownership_settled(connection, boot_id, source, observed):
    """Keep raw correction evidence while another active owner can reveal overlap."""
    if connection.execute("SELECT 1 FROM handover WHERE boot_id=? LIMIT 1", (boot_id,)).fetchone():
        return True
    frontiers = connection.execute(
        "SELECT c.checkpoint_mono_ns FROM collector_epoch e LEFT JOIN sampler_checkpoint c "
        "ON c.boot_id=e.boot_id AND c.source=e.source AND c.epoch=e.epoch "
        "WHERE e.boot_id=? AND e.source<>? AND e.allocated_mono_ns<=?",
        (boot_id, source, observed),
    )
    return all(frontier is not None and frontier >= observed for (frontier,) in frontiers)


def device_interval_start(connection, boot_id, source, epoch, timestamp):
    """Device deltas share their collector's preceding observation boundary."""
    previous = connection.execute(
        "SELECT max(ts_mono_ns) FROM sample_coverage WHERE boot_id=? AND source=? AND epoch=? AND ts_mono_ns<?",
        (boot_id, source, epoch, timestamp),
    ).fetchone()[0]
    if previous is not None:
        return previous
    interval = connection.execute(
        "SELECT from_mono_ns FROM collector_interval WHERE boot_id=? AND source=? AND epoch=?",
        (boot_id, source, epoch),
    ).fetchone()
    return interval[0] if interval else None
