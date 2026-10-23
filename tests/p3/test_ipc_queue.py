"""Bounded per-client egress queue for the AppIO daemon socket.

Threat model for this file. An *authorized* client that stops reading must not:

* stall the collector -- the queue's nonblocking send path may never block the
  sampling/commit loop, so a full queue has to become a disconnect, not a wait;
* grow root memory without bound -- the queued-byte budget is absolute and covers
  status, error and query-result frames as well as live snapshots, so an error
  path cannot bypass it;
* leave the daemon believing a snapshot was delivered -- a snapshot's pending
  count is cleared only when its *complete* frame has drained, so a partially
  written frame cannot reset the slow-client accounting.

Two consecutive logical snapshots still pending transmission are the design's
explicit slow-client signal (design §4.2, D4): the second one disconnects that
client and clears only that client's queue. The tests below pin the byte
accounting against actual (partial) send counts, the snapshot counter against
partial writes, and isolation between a slow client and a healthy one.
"""

from __future__ import annotations

import pytest

from appio.ipc_models import StreamCursor
from appio.ipc_protocol import encode_frame
from appio.ipc_queue import ClientQueue, SlowClientError

CURSOR_0 = StreamCursor("boot", 1, 0)
CURSOR_1 = StreamCursor("boot", 1, 1)
CURSOR_2 = StreamCursor("boot", 1, 2)


def status_frame(marker: str = "x") -> bytes:
    """A real protocol frame, so the four-byte length prefix is in the accounting."""

    return encode_frame({"protocol": 1, "type": "status", "marker": marker})


# --------------------------------------------------------------------------- #
# the plan's two required vectors
# --------------------------------------------------------------------------- #


def test_partial_send_does_not_reset_unread_snapshot_count():
    """Threat: a partially transmitted snapshot must still count as pending.

    If ``consume`` cleared the counter on the first byte, a client that reads one
    byte per snapshot would look healthy forever while falling arbitrarily far
    behind, and the daemon would buffer snapshots without bound.
    """

    queue = ClientQueue(max_bytes=524288)
    queue.push(b"abcdef", snapshot=StreamCursor("boot", 1, 0))
    queue.consume(1)
    with pytest.raises(SlowClientError):
        queue.push(b"next", snapshot=StreamCursor("boot", 1, 1))
    assert queue.closed
    assert queue.queued_bytes == 0


# --------------------------------------------------------------------------- #
# byte budget
# --------------------------------------------------------------------------- #


def test_exact_byte_budget_includes_the_four_byte_length_prefix():
    """Threat: accounting the payload but not the prefix understates egress by 4
    bytes per frame, so a flooding client exceeds the real budget."""

    frame = status_frame()
    queue = ClientQueue(max_bytes=len(frame))
    queue.push(frame)
    assert queue.queued_bytes == len(frame)
    assert bytes(queue.peek(len(frame) + 100)) == frame
    # One byte over the budget is a disconnect, not a partial enqueue.
    with pytest.raises(SlowClientError) as caught:
        queue.push(b"!")
    assert caught.value.reason == "queue_budget"
    assert queue.closed
    assert queue.queued_bytes == 0


def test_budget_counts_the_prefix_not_just_the_payload():
    """Threat: if only the JSON payload were budgeted, a client could hold
    ``max_bytes / (payload + 4)`` frames in slightly more memory than the cap, and
    the real socket cost of each frame would be unaccounted. This uses
    ``max_bytes == len(payload)``: a payload-only budget would accept the frame."""

    frame = status_frame()
    payload = frame[4:]
    queue = ClientQueue(max_bytes=len(payload))
    with pytest.raises(SlowClientError) as caught:
        queue.push(frame)
    assert caught.value.reason == "queue_budget"
    assert queue.queued_bytes == 0


def test_one_byte_below_the_budget_is_refused_at_push_time():
    """Threat: a frame that can never fit must be refused immediately, not queued
    and then reported as "sent" by a caller that only checks ``queued_bytes``."""

    frame = status_frame()
    queue = ClientQueue(max_bytes=len(frame) - 1)
    with pytest.raises(SlowClientError):
        queue.push(frame)
    assert queue.queued_bytes == 0


def test_query_only_flooding_disconnects_without_touching_a_healthy_client():
    """Threat: a client that issues queries and never reads must not exhaust root
    memory, and must not disturb another client's queue.

    This is the "one slow client beside one healthy client" vector: the slow
    client is closed at its budget while the healthy client's bytes and state are
    untouched.
    """

    slow = ClientQueue(max_bytes=256)
    healthy = ClientQueue(max_bytes=256)
    payload = status_frame("result")
    healthy.push(payload)
    before = healthy.queued_bytes

    with pytest.raises(SlowClientError) as caught:
        for _ in range(100):
            slow.push(payload)
    assert caught.value.reason == "queue_budget"

    assert slow.closed and slow.queued_bytes == 0
    assert not healthy.closed
    assert healthy.queued_bytes == before
    assert bytes(healthy.peek(1000)) == payload
    healthy.push(payload)
    assert healthy.queued_bytes == before + len(payload)


def test_error_frames_cannot_bypass_the_budget():
    """Threat: the error-reporting path is the obvious place to "just queue one
    more message"; an unbounded error queue is still unbounded memory."""

    queue = ClientQueue(max_bytes=64)
    with pytest.raises(SlowClientError) as caught:
        queue.push(encode_frame({"protocol": 1, "type": "error", "reason": "y" * 200}))
    assert caught.value.reason == "queue_budget"
    assert queue.closed


# --------------------------------------------------------------------------- #
# snapshot accounting
# --------------------------------------------------------------------------- #


def test_complete_drain_resets_the_pending_snapshot_count():
    """Threat: if the counter is never decremented, a healthy client is
    disconnected after one snapshot; if it is decremented too early, a slow client
    is never disconnected."""

    queue = ClientQueue(max_bytes=1024)
    frame = b"snapshot-frame"
    queue.push(frame, snapshot=CURSOR_0)
    assert queue.pending_snapshots == 1
    assert queue.pending_cursors == (CURSOR_0,)
    queue.consume(len(frame) - 1)
    assert queue.pending_snapshots == 1  # still one unread byte
    queue.consume(1)
    assert queue.pending_snapshots == 0
    assert queue.pending_cursors == ()
    queue.push(frame, snapshot=CURSOR_1)  # a drained queue accepts the next snapshot
    assert queue.pending_snapshots == 1


def test_second_pending_snapshot_disconnects_and_clears_that_queue():
    """The design's explicit slow-client rule: two consecutive logical snapshots
    still pending transmission disconnect the client."""

    queue = ClientQueue(max_bytes=1024)
    queue.push(b"first", snapshot=CURSOR_0)
    with pytest.raises(SlowClientError) as caught:
        queue.push(b"second", snapshot=CURSOR_1)
    assert caught.value.reason == "slow_client"
    assert queue.closed
    assert queue.queued_bytes == 0
    assert queue.pending_snapshots == 0
    assert bytes(queue.peek(16)) == b""


def test_snapshot_limit_is_configurable_and_still_bounded():
    """Threat: a caller that configures a larger allowance must still get a
    disconnect at the configured bound rather than an unbounded list."""

    queue = ClientQueue(max_bytes=1024, max_pending_snapshots=3)
    queue.push(b"a", snapshot=CURSOR_0)
    queue.push(b"b", snapshot=CURSOR_1)
    assert queue.pending_snapshots == 2
    with pytest.raises(SlowClientError) as caught:
        queue.push(b"c", snapshot=CURSOR_2)
    assert caught.value.reason == "slow_client"
    assert queue.closed


def test_non_snapshot_frames_are_not_counted_as_pending_snapshots():
    """Threat: status/error/result frames must not consume the snapshot budget,
    or a client that reads them will be disconnected as "slow"."""

    queue = ClientQueue(max_bytes=1024)
    for _ in range(5):
        queue.push(b"result")
        queue.consume(6)
    assert queue.pending_snapshots == 0
    queue.push(b"s", snapshot=CURSOR_0)
    assert queue.pending_snapshots == 1


# --------------------------------------------------------------------------- #
# peek/consume accounting
# --------------------------------------------------------------------------- #


def test_peek_is_bounded_by_its_argument_and_does_not_consume():
    """Threat: a peek that returned more than the caller's socket budget, or that
    advanced the queue, would double-count or over-read bytes."""

    queue = ClientQueue(max_bytes=1024)
    queue.push(b"abcdefgh")
    assert bytes(queue.peek(3)) == b"abc"
    assert bytes(queue.peek(3)) == b"abc"
    assert queue.queued_bytes == 8
    assert bytes(queue.peek(64)) == b"abcdefgh"
    assert bytes(queue.peek(0)) == b""


def test_peek_returns_a_memoryview_over_the_immutable_frame():
    """The send path hands the view straight to a nonblocking ``send``; a copy per
    turn would make the daemon's cost proportional to queue churn."""

    queue = ClientQueue(max_bytes=1024)
    queue.push(b"abcdefgh")
    view = queue.peek(4)
    assert isinstance(view, memoryview)
    assert bytes(view) == b"abcd"
    assert view.readonly


def test_consume_journals_bytes_across_frames_using_the_actual_send_count():
    """Threat: decrementing by the requested size instead of the bytes actually
    written desynchronises the budget from the socket, so a short send silently
    drops bytes while the accounting says they were delivered."""

    frames = [b"aaaa", b"bbbbbb", b"cc"]
    queue = ClientQueue(max_bytes=1024)
    for frame in frames:
        queue.push(frame)
    sent_total = 0
    while queue.queued_bytes:
        view = queue.peek(5)
        chunk = len(view)  # what a nonblocking send() accepted this turn
        assert bytes(view) == b"".join(frames)[sent_total : sent_total + chunk]
        sent_total += chunk
        queue.consume(chunk)
    assert sent_total == len(b"".join(frames))
    assert queue.queued_bytes == 0
    assert not queue.closed


def test_consume_rejects_impossible_byte_counts():
    """Threat: a negative or oversized consume must not mint or erase bytes."""

    queue = ClientQueue(max_bytes=1024)
    queue.push(b"abcdef")
    for bad in (-1, 7, 1000, "2", 2.0, True):
        with pytest.raises(ValueError):
            queue.consume(bad)  # type: ignore[arg-type]
        assert queue.queued_bytes == 6
    queue.consume(0)
    assert queue.queued_bytes == 6


def test_push_rejects_things_that_are_not_frames():
    """Threat: a str or an empty frame would be sent as a zero/odd-length frame,
    desynchronising the length-prefixed stream for that client."""

    queue = ClientQueue(max_bytes=1024)
    for bad in ("frame", b"", 0):
        with pytest.raises((TypeError, ValueError)):
            queue.push(bad)  # type: ignore[arg-type]
        assert queue.queued_bytes == 0


def test_a_closed_queue_stays_inert():
    """Threat: after a disconnect the daemon's error path may still push a final
    message; that must not silently reopen or refill the queue."""

    queue = ClientQueue(max_bytes=64)
    with pytest.raises(SlowClientError):
        queue.push(b"x" * 65)
    assert queue.closed
    with pytest.raises(SlowClientError) as caught:
        queue.push(b"late")
    assert caught.value.reason == "queue_closed"
    assert queue.queued_bytes == 0
    queue.consume(0)
    assert bytes(queue.peek(8)) == b""


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": -1},
        {"max_bytes": True},
        {"max_bytes": 512.0},
        {"max_bytes": 1024, "max_pending_snapshots": 1},
        {"max_bytes": 1024, "max_pending_snapshots": 0},
        {"max_bytes": 1024, "max_pending_snapshots": True},
    ],
)
def test_constructor_rejects_a_budget_that_cannot_bound_anything(kwargs):
    """Threat: a zero/boolean budget makes the cap either infinite (falsy fast
    paths) or a de-facto disconnect-everything; both are silent misconfigurations."""

    with pytest.raises(ValueError):
        ClientQueue(**kwargs)


def test_default_budget_matches_the_plan():
    """D4 pins 512 KiB per client and two pending snapshots; a drift here changes
    the daemon's memory bound without anyone re-reading the design."""

    queue = ClientQueue()
    assert queue.max_bytes == 512 * 1024
    assert queue.max_pending_snapshots == 2
