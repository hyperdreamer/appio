"""Protocol-1 framing and typed read-only message contract (plan task 1).

Every vector here is adversarial input a local client or a compromised peer can
send. The framing must reject an oversized header *before* reading its body, the
JSON parser must refuse duplicate keys, non-finite constants, and unbounded
nesting, and the request model must expose exactly ``status``, ``subscribe`` and
``query`` — never a privileged verb.
"""

from __future__ import annotations

import struct

import pytest

from appio.contracts import (
    HISTORICAL_BOOT_PREFIX_MESSAGE,
    LAUNCH_WINDOW_MESSAGE,
    RANGE_TOO_LONG_MESSAGE,
    WindowUnavailableError,
)
from appio.ipc_models import (
    Capabilities,
    QueryRequest,
    Request,
    StreamCursor,
    validate_request,
    validate_server_message,
)
from appio.ipc_protocol import FrameDecoder, ProtocolError, encode_frame

RANGE_WINDOW = {
    "kind": "range",
    "boot_id": "boot-1",
    "start_mono_ns": 1_000,
    "end_mono_ns": 2_000,
}


def status_message(**overrides):
    message = {"protocol": 1, "type": "status", "request_id": "r1"}
    message.update(overrides)
    return message


def subscribe_message(**overrides):
    message = {
        "protocol": 1,
        "type": "subscribe",
        "request_id": "s1",
        "since": {"boot_id": "boot-1", "iod_epoch": 3, "seq": 9},
    }
    message.update(overrides)
    return message


def query_message(**query_overrides):
    query = {"kind": "ranking", "window": dict(RANGE_WINDOW)}
    query.update(query_overrides)
    return {"protocol": 1, "type": "query", "request_id": "q1", "query": query}


def raw_frame(payload: bytes, length: int | None = None) -> bytes:
    return struct.pack("!I", len(payload) if length is None else length) + payload


def snapshot_message(**overrides):
    message = {
        "protocol": 1,
        "type": "snapshot",
        "boot_id": "boot-1",
        "iod_epoch": 2,
        "seq": 11,
        "ts_mono_ns": 1_761_000_000_123_456,
        "ts_wall": 1_761_000_000.1,
        "source": "iod",
        "capabilities": {
            "proc_all": True,
            "dev_stat": True,
            "file_io": False,
            "bpf_loaded": False,
        },
        "procs": [{"pid": 7}],
        "devs": [],
        "files": [],
        "coverage": {"inaccessible_pids": 0},
    }
    message.update(overrides)
    return message


def protocol_error_of(callable_):
    with pytest.raises(ProtocolError) as captured:
        callable_()
    return captured.value


# ---------------------------------------------------------------------------
# The plan's three decisive tests
# ---------------------------------------------------------------------------


def test_fragmented_frames_are_not_newline_delimited():
    first = {"protocol": 1, "type": "status", "request_id": "a"}
    second = {"protocol": 1, "type": "status", "request_id": "b"}
    decoder = FrameDecoder()
    wire = encode_frame(first) + encode_frame(second)
    assert decoder.feed(wire[:3]) == ()
    assert decoder.feed(wire[3:]) == (first, second)
    decoder.eof()


def test_oversize_is_rejected_from_header_without_waiting_for_body():
    decoder = FrameDecoder()
    with pytest.raises(ProtocolError, match="frame_too_large"):
        decoder.feed(struct.pack("!I", 1048577))


@pytest.mark.parametrize("verb", ["enable", "disable", "exec", "handover"])
def test_root_mutation_verbs_never_enter_dispatch(verb):
    with pytest.raises(ProtocolError, match="unsupported_type"):
        validate_request({"protocol": 1, "type": verb, "request_id": "a"})


# ---------------------------------------------------------------------------
# Framing: fragmentation, coalescing, UTF-8 splits, EOF, zero length
# ---------------------------------------------------------------------------


def test_byte_by_byte_fragmentation_yields_every_frame_once():
    messages = [
        {"protocol": 1, "type": "status", "request_id": "a"},
        {"protocol": 1, "type": "status", "request_id": "b"},
        {"protocol": 1, "type": "status", "request_id": "c"},
    ]
    wire = b"".join(encode_frame(message) for message in messages)
    decoder = FrameDecoder()
    decoded = []
    for index in range(len(wire)):
        decoded.extend(decoder.feed(wire[index : index + 1]))
    assert tuple(decoded) == tuple(messages)
    decoder.eof()


def test_many_frames_in_one_feed_are_all_returned_in_order():
    messages = [
        {"protocol": 1, "type": "status", "request_id": f"r{index}"} for index in range(200)
    ]
    decoder = FrameDecoder()
    assert decoder.feed(b"".join(encode_frame(message) for message in messages)) == tuple(messages)


def test_utf8_sequence_split_across_feeds_is_decoded_once():
    message = {"protocol": 1, "type": "status", "request_id": "caf\u00e9 \u2713"}
    payload = encode_frame(message)
    text = payload[4:].decode("utf-8")
    # Split the payload bytes inside the two-byte ``é`` sequence.
    split = payload.index("\u00e9".encode("utf-8")) + 1
    assert text  # the payload really is UTF-8 text
    decoder = FrameDecoder()
    assert decoder.feed(payload[:split]) == ()
    assert decoder.feed(payload[split:]) == (message,)


def test_zero_length_header_is_rejected():
    decoder = FrameDecoder()
    error = protocol_error_of(lambda: decoder.feed(struct.pack("!I", 0)))
    assert error.reason == "empty_frame"


def test_invalid_utf8_body_is_rejected():
    decoder = FrameDecoder()
    error = protocol_error_of(lambda: decoder.feed(raw_frame(b'{"a":"\xff"}')))
    assert error.reason == "invalid_utf8"


def test_eof_in_header_is_a_truncated_frame():
    decoder = FrameDecoder()
    decoder.feed(b"\x00\x00")
    error = protocol_error_of(decoder.eof)
    assert error.reason == "truncated_frame"


def test_eof_in_body_is_a_truncated_frame():
    decoder = FrameDecoder()
    decoder.feed(raw_frame(b'{"protocol":1}')[:-1])
    error = protocol_error_of(decoder.eof)
    assert error.reason == "truncated_frame"


def test_eof_after_complete_frames_is_clean():
    decoder = FrameDecoder()
    decoder.feed(encode_frame({"protocol": 1, "type": "status", "request_id": "a"}))
    assert decoder.eof() is None


def test_top_level_array_is_rejected():
    decoder = FrameDecoder()
    error = protocol_error_of(lambda: decoder.feed(raw_frame(b"[1,2,3]")))
    assert error.reason == "invalid_message"


def test_top_level_scalar_is_rejected():
    decoder = FrameDecoder()
    error = protocol_error_of(lambda: decoder.feed(raw_frame(b"7")))
    assert error.reason == "invalid_message"


# ---------------------------------------------------------------------------
# JSON hardening: duplicate keys, non-finite numbers, nesting, member budget
# ---------------------------------------------------------------------------


def test_duplicate_keys_at_top_level_are_rejected():
    decoder = FrameDecoder()
    payload = b'{"protocol":1,"type":"status","request_id":"a","request_id":"b"}'
    error = protocol_error_of(lambda: decoder.feed(raw_frame(payload)))
    assert error.reason == "duplicate_key"


def test_duplicate_keys_at_every_nesting_level_are_rejected():
    decoder = FrameDecoder()
    payload = (
        b'{"protocol":1,"type":"query","request_id":"a","query":'
        b'{"kind":"series","window":{"kind":"range","boot_id":"b","boot_id":"c",'
        b'"start_mono_ns":1,"end_mono_ns":2},"app_key":"exe:/bin/x"}}'
    )
    error = protocol_error_of(lambda: decoder.feed(raw_frame(payload)))
    assert error.reason == "duplicate_key"


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity", b"1e999"])
def test_nonfinite_json_constants_are_rejected(literal):
    decoder = FrameDecoder()
    payload = b'{"protocol":1,"type":"status","request_id":"a","n":' + literal + b"}"
    error = protocol_error_of(lambda: decoder.feed(raw_frame(payload)))
    assert error.reason == "nonfinite_number"


def test_nesting_beyond_32_levels_is_rejected():
    decoder = FrameDecoder()
    payload = b'{"a":' + b'{"a":' * 33 + b"1" + b"}" * 33 + b"}"
    error = protocol_error_of(lambda: decoder.feed(raw_frame(payload)))
    assert error.reason == "nesting_too_deep"


def test_aggregate_container_members_are_bounded():
    decoder = FrameDecoder()
    payload = b'{"a":[' + b",".join([b"0"] * 16_385) + b"]}"
    error = protocol_error_of(lambda: decoder.feed(raw_frame(payload)))
    assert error.reason == "too_many_members"


def test_encode_frame_rejects_nonfinite_float():
    error = protocol_error_of(lambda: encode_frame({"protocol": 1, "x": float("nan")}))
    assert error.reason == "nonfinite_number"


def test_encode_frame_rejects_non_object_message():
    error = protocol_error_of(lambda: encode_frame(["status"]))
    assert error.reason == "invalid_message"


def test_encode_frame_rejects_payload_over_the_bound():
    error = protocol_error_of(lambda: encode_frame({"blob": "x" * 300_000}))
    assert error.reason == "frame_too_large"


def test_encode_frame_round_trips_and_is_length_prefixed():
    message = {"protocol": 1, "type": "status", "request_id": "r1"}
    wire = encode_frame(message)
    assert struct.unpack("!I", wire[:4])[0] == len(wire) - 4
    assert FrameDecoder().feed(wire) == (message,)


# ---------------------------------------------------------------------------
# request_id, protocol, key sets, integer typing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_id",
    ["", "a\x00b", "x" * 65, 7, None, True],
)
def test_invalid_request_id_is_rejected(request_id):
    error = protocol_error_of(lambda: validate_request(status_message(request_id=request_id)))
    assert error.reason == "invalid_request_id"


def test_request_id_at_64_bytes_is_accepted():
    assert validate_request(status_message(request_id="x" * 64)).request_id == "x" * 64


@pytest.mark.parametrize(
    "protocol",
    [2, 0, "1", 1.0, True, None],
)
def test_protocol_must_be_the_integer_one(protocol):
    error = protocol_error_of(lambda: validate_request(status_message(protocol=protocol)))
    assert error.reason == "protocol_mismatch"


def test_missing_protocol_is_rejected():
    message = status_message()
    del message["protocol"]
    error = protocol_error_of(lambda: validate_request(message))
    assert error.reason == "missing_key"


def test_unknown_key_is_rejected():
    error = protocol_error_of(lambda: validate_request(status_message(verb="enable")))
    assert error.reason == "unknown_key"


def test_non_object_request_is_rejected():
    error = protocol_error_of(lambda: validate_request(["status"]))
    assert error.reason == "invalid_message"


def test_bool_is_not_an_integer_field():
    error = protocol_error_of(
        lambda: validate_request(subscribe_message(since={"boot_id": "b", "iod_epoch": True, "seq": 1}))
    )
    assert error.reason == "invalid_field"


@pytest.mark.parametrize("field", ["iod_epoch", "seq"])
def test_negative_cursor_component_is_rejected(field):
    cursor = {"boot_id": "boot-1", "iod_epoch": 3, "seq": 9}
    cursor[field] = -1
    error = protocol_error_of(lambda: validate_request(subscribe_message(since=cursor)))
    assert error.reason == "invalid_field"


# ---------------------------------------------------------------------------
# Read-only request types
# ---------------------------------------------------------------------------


def test_status_request_has_no_cursor_or_query():
    request = validate_request(status_message())
    assert isinstance(request, Request)
    assert (request.type, request.since, request.query) == ("status", None, None)


def test_subscribe_request_carries_boot_scoped_cursor():
    request = validate_request(subscribe_message())
    assert request.type == "subscribe"
    assert request.since == StreamCursor(boot_id="boot-1", iod_epoch=3, seq=9)


def test_legacy_epoch_only_cursor_asks_for_resync_instead_of_cross_boot_replay():
    request = validate_request(subscribe_message(since={"iod_epoch": 3, "seq": 9}))
    assert request.type == "subscribe"
    assert request.since is None
    assert request.resync_reason == "cursor_boot_unknown"


def test_subscribe_without_since_starts_fresh():
    request = validate_request(subscribe_message(since=None))
    assert request.since is None
    assert request.resync_reason is None


def test_subscribe_since_must_be_an_object():
    error = protocol_error_of(lambda: validate_request(subscribe_message(since=[1, 2])))
    assert error.reason == "invalid_field"


def test_subscribe_since_rejects_unknown_keys():
    error = protocol_error_of(
        lambda: validate_request(
            subscribe_message(since={"boot_id": "b", "iod_epoch": 1, "seq": 1, "after": 4})
        )
    )
    assert error.reason == "unknown_key"


# ---------------------------------------------------------------------------
# Query selectors: allowlist, per-kind keys, bounded strings and limits
# ---------------------------------------------------------------------------


def test_ranking_query_parses_into_typed_request():
    request = validate_request(query_message())
    assert request.type == "query"
    query = request.query
    assert isinstance(query, QueryRequest)
    assert query.kind == "ranking"
    assert query.window.kind == "range"
    assert (query.metric, query.sort, query.direction) == ("total_bytes", "bytes_total", "desc")
    assert (query.search, query.cursor, query.limit) == (None, None, 500)
    assert query.coverage_kind == "process"
    assert query.app_key is None


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("ranking", {}),
        ("series", {"app_key": "exe:/usr/bin/x"}),
        ("files", {"app_key": "exe:/usr/bin/x"}),
        ("coverage", {}),
        ("totals", {"app_key": "exe:/usr/bin/x"}),
    ],
)
def test_every_allowed_query_kind_round_trips(kind, extra):
    request = validate_request(query_message(kind=kind, **extra))
    assert request.query.kind == kind


@pytest.mark.parametrize("kind", ["sql", "exec", "raw", "", "status", True, None])
def test_unknown_query_kind_is_rejected(kind):
    error = protocol_error_of(lambda: validate_request(query_message(kind=kind)))
    assert error.reason == "unsupported_query_kind"


def test_selector_not_valid_for_the_kind_is_rejected():
    # ``app_key`` is a series/files/totals selector, never a ranking selector.
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="ranking", app_key="exe:/usr/bin/x"))
    )
    assert error.reason == "invalid_selector"


def test_coverage_kind_is_default_only_outside_the_coverage_query():
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="ranking", coverage_kind="file"))
    )
    assert error.reason == "invalid_selector"
    assert (
        validate_request(query_message(kind="ranking", coverage_kind="process"))
        .query.coverage_kind
        == "process"
    )


@pytest.mark.parametrize("coverage_kind", ["process", "file", "unknown_owner"])
def test_coverage_query_accepts_each_coverage_kind(coverage_kind):
    request = validate_request(query_message(kind="coverage", coverage_kind=coverage_kind))
    assert request.query.coverage_kind == coverage_kind


def test_coverage_query_rejects_unknown_coverage_kind():
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="coverage", coverage_kind="device"))
    )
    assert error.reason == "invalid_field"


@pytest.mark.parametrize("kind", ["series", "files", "totals"])
def test_app_scoped_query_kinds_require_app_key(kind):
    error = protocol_error_of(lambda: validate_request(query_message(kind=kind)))
    assert error.reason == "missing_key"


@pytest.mark.parametrize(
    "app_key",
    ["", "bogus:x", "unit:", "unit:a/b", "exe:relative", "unit:space here", 7],
)
def test_invalid_app_key_is_rejected(app_key):
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="totals", app_key=app_key))
    )
    assert error.reason == "invalid_app_key"


def test_canonical_app_key_is_accepted():
    request = validate_request(query_message(kind="totals", app_key="unit:app-x@1.service"))
    assert request.query.app_key == "unit:app-x@1.service"


@pytest.mark.parametrize("metric", ["total_bytes", "read_bytes", "write_bytes", "rchar", "wchar"])
def test_ranking_metrics_are_typed(metric):
    assert validate_request(query_message(metric=metric)).query.metric == metric


@pytest.mark.parametrize("metric", ["bytes_total", "sql", "", 7, True])
def test_unknown_metric_is_rejected(metric):
    error = protocol_error_of(lambda: validate_request(query_message(metric=metric)))
    assert error.reason == "invalid_field"


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_directions_are_typed(direction):
    assert validate_request(query_message(direction=direction)).query.direction == direction


@pytest.mark.parametrize("direction", ["ASC", "up", "", 1])
def test_unknown_direction_is_rejected(direction):
    error = protocol_error_of(lambda: validate_request(query_message(direction=direction)))
    assert error.reason == "invalid_field"


def test_file_sort_accepts_the_file_sort_literal_only():
    request = validate_request(
        query_message(kind="files", app_key="exe:/usr/bin/x", sort="last_seen")
    )
    assert request.query.sort == "last_seen"
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="files", app_key="exe:/usr/bin/x", sort="rchar"))
    )
    assert error.reason == "invalid_field"


def test_search_at_256_bytes_is_accepted_and_longer_is_rejected():
    accepted = validate_request(query_message(kind="files", app_key="exe:/x", search="s" * 256))
    assert accepted.query.search == "s" * 256
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="files", app_key="exe:/x", search="s" * 257))
    )
    assert error.reason == "invalid_field"


def test_search_with_nul_is_rejected():
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="files", app_key="exe:/x", search="a\x00b"))
    )
    assert error.reason == "invalid_field"


def test_cursor_at_512_bytes_is_accepted_and_longer_is_rejected():
    accepted = validate_request(
        query_message(kind="files", app_key="exe:/x", cursor="c" * 512)
    )
    assert accepted.query.cursor == "c" * 512
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="files", app_key="exe:/x", cursor="c" * 513))
    )
    assert error.reason == "invalid_field"


def test_search_is_not_a_series_selector():
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="series", app_key="exe:/x", search="a"))
    )
    assert error.reason == "invalid_selector"


@pytest.mark.parametrize("limit", [0, -1, True, "500", 501])
def test_files_limit_is_bounded(limit):
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="files", app_key="exe:/x", limit=limit))
    )
    assert error.reason == "invalid_field"


def test_files_limit_at_500_is_accepted():
    assert validate_request(
        query_message(kind="files", app_key="exe:/x", limit=500)
    ).query.limit == 500


@pytest.mark.parametrize("limit", [4001, 0, True])
def test_series_limit_is_bounded_at_the_design_point_budget(limit):
    error = protocol_error_of(
        lambda: validate_request(query_message(kind="series", app_key="exe:/x", limit=limit))
    )
    assert error.reason == "invalid_field"


def test_query_must_be_an_object():
    message = query_message()
    message["query"] = "ranking"
    error = protocol_error_of(lambda: validate_request(message))
    assert error.reason == "invalid_field"


def test_query_requires_a_window():
    message = query_message()
    del message["query"]["window"]
    error = protocol_error_of(lambda: validate_request(message))
    assert error.reason == "missing_key"


# ---------------------------------------------------------------------------
# Windows, including the disabled launch scope (A22 before any ID lookup)
# ---------------------------------------------------------------------------


def test_since_boot_lifetime_and_range_windows_parse():
    since_boot = validate_request(
        query_message(window={"kind": "since_boot", "boot_id": "boot-1", "end_mono_ns": 10})
    ).query.window
    assert (since_boot.kind, since_boot.boot_id, since_boot.end_mono_ns) == (
        "since_boot",
        "boot-1",
        10,
    )
    lifetime = validate_request(query_message(window={"kind": "lifetime"})).query.window
    assert lifetime.kind == "lifetime"


@pytest.mark.parametrize(
    "window",
    [
        {"kind": "range", "boot_id": "b", "start_mono_ns": 5, "end_mono_ns": 5},
        {"kind": "range", "boot_id": "b", "start_mono_ns": 6, "end_mono_ns": 5},
        {"kind": "range", "boot_id": "", "start_mono_ns": 0, "end_mono_ns": 5},
        {"kind": "range", "boot_id": "b", "start_mono_ns": 0},
        {"kind": "since_boot", "boot_id": "b", "end_mono_ns": 0},
        {"kind": "since_boot", "boot_id": "b"},
        {"kind": "lifetime", "boot_id": "b"},
        {"kind": "range", "boot_id": "b", "start_mono_ns": True, "end_mono_ns": 5},
        {"kind": "calendar"},
        {},
    ],
)
def test_malformed_windows_are_rejected_with_invalid_window(window):
    error = protocol_error_of(lambda: validate_request(query_message(window=window)))
    assert error.reason == "invalid_window"


def test_window_rejects_unknown_wire_keys():
    error = protocol_error_of(
        lambda: validate_request(
            query_message(window={**RANGE_WINDOW, "app_id": 7})
        )
    )
    assert error.reason == "unknown_key"


@pytest.mark.parametrize(
    "wire_window",
    [
        {"kind": "launch", "boot_id": "boot-1"},
        {"kind": "launch", "boot_id": "boot-1", "launch_id": 42},
        {"kind": "launch"},
    ],
)
def test_launch_window_raises_the_exact_a22_error_without_an_id_lookup(wire_window):
    with pytest.raises(WindowUnavailableError) as captured:
        validate_request(query_message(window=wire_window))
    assert captured.value.reason == "launch_attribution"
    assert str(captured.value) == LAUNCH_WINDOW_MESSAGE


def test_disabled_scope_messages_are_the_ratified_constants():
    assert LAUNCH_WINDOW_MESSAGE.startswith("Launch window disabled")
    assert RANGE_TOO_LONG_MESSAGE.startswith("Ranges longer than 365 days")
    assert HISTORICAL_BOOT_PREFIX_MESSAGE.startswith("Historical boot prefix unavailable")


# ---------------------------------------------------------------------------
# Server message envelopes
# ---------------------------------------------------------------------------


def test_status_envelope_is_validated_and_normalized():
    message = {
        "protocol": 1,
        "type": "status",
        "request_id": "r1",
        "enabled": True,
        "answering": False,
        "pending_activation": False,
        "ownership_start_mono_ns": None,
        "cursor": {"boot_id": "boot-1", "iod_epoch": 3, "seq": 12},
    }
    validated = validate_server_message(message)
    assert validated["type"] == "status"
    assert validated["cursor"] == StreamCursor(boot_id="boot-1", iod_epoch=3, seq=12)


def test_status_requires_booleans_and_a_cursor():
    message = {
        "protocol": 1,
        "type": "status",
        "request_id": "r1",
        "enabled": 1,
        "answering": False,
        "pending_activation": False,
        "ownership_start_mono_ns": None,
        "cursor": None,
    }
    assert protocol_error_of(lambda: validate_server_message(message)).reason == "invalid_field"


def test_snapshot_envelope_pins_the_iod_identity():
    validated = validate_server_message(snapshot_message())
    assert validated["source"] == "iod"
    assert (validated["boot_id"], validated["iod_epoch"], validated["seq"]) == ("boot-1", 2, 11)
    assert validated["capabilities"] == Capabilities(True, True, False, False)


@pytest.mark.parametrize("source", ["gui", "iod2", "", None])
def test_snapshot_source_must_be_iod(source):
    error = protocol_error_of(lambda: validate_server_message(snapshot_message(source=source)))
    assert error.reason == "invalid_field"


@pytest.mark.parametrize("field,value", [("boot_id", ""), ("iod_epoch", 0), ("seq", -1), ("seq", True)])
def test_snapshot_identity_fields_are_typed(field, value):
    error = protocol_error_of(lambda: validate_server_message(snapshot_message(**{field: value})))
    assert error.reason == "invalid_field"


def test_snapshot_wall_timestamp_may_be_a_finite_float_but_not_nan():
    validated = validate_server_message(snapshot_message(ts_wall=1_761_000_000.25))
    assert validated["ts_wall"] == 1_761_000_000.25
    assert (
        protocol_error_of(
            lambda: validate_server_message(snapshot_message(ts_wall=float("inf")))
        ).reason
        == "invalid_field"
    )


def test_snapshot_rows_must_be_lists_and_capabilities_complete():
    error = protocol_error_of(
        lambda: validate_server_message(snapshot_message(procs={"pid": 1}))
    )
    assert error.reason == "invalid_field"
    missing = snapshot_message()
    del missing["capabilities"]["bpf_loaded"]
    assert protocol_error_of(lambda: validate_server_message(missing)).reason == "missing_key"


def test_gap_envelope_allows_null_bounds_and_rejects_inverted_ones():
    gap = {
        "protocol": 1,
        "type": "gap",
        "boot_id": "boot-1",
        "iod_epoch": 2,
        "from_mono_ns": None,
        "to_mono_ns": None,
        "previous": {"boot_id": "boot-1", "iod_epoch": 2, "seq": 5},
        "current": None,
        "reason": "cursor_boot_unknown",
    }
    validated = validate_server_message(gap)
    assert validated["from_mono_ns"] is None and validated["current"] is None
    inverted = dict(gap, from_mono_ns=20, to_mono_ns=10)
    assert protocol_error_of(lambda: validate_server_message(inverted)).reason == "invalid_field"
    half_open = dict(gap, from_mono_ns=10, to_mono_ns=10)
    assert protocol_error_of(lambda: validate_server_message(half_open)).reason == "invalid_field"


@pytest.mark.parametrize("kind,payload", [("ranking", []), ("coverage", {})])
def test_result_envelope_correlates_and_types_its_payload(kind, payload):
    message = {
        "protocol": 1,
        "type": "result",
        "request_id": "q1",
        "kind": kind,
        "payload": payload,
    }
    assert validate_server_message(message)["kind"] == kind
    message["payload"] = "not-a-payload"
    assert protocol_error_of(lambda: validate_server_message(message)).reason == "invalid_field"


def test_result_requires_request_correlation():
    message = {"protocol": 1, "type": "result", "kind": "ranking", "payload": []}
    assert protocol_error_of(lambda: validate_server_message(message)).reason == "missing_key"


def test_error_envelope_is_bounded_and_carries_a_reason():
    message = {
        "protocol": 1,
        "type": "error",
        "request_id": "q1",
        "reason": "unsupported_query_kind",
        "message": "unsupported_query_kind",
    }
    assert validate_server_message(message)["reason"] == "unsupported_query_kind"
    assert (
        protocol_error_of(
            lambda: validate_server_message(dict(message, message="x" * 513))
        ).reason
        == "invalid_field"
    )
    assert (
        protocol_error_of(
            lambda: validate_server_message(dict(message, message="line\nline"))
        ).reason
        == "invalid_field"
    )


@pytest.mark.parametrize("envelope", ["enable", "handover", "exec", ""])
def test_unknown_server_envelope_type_is_rejected(envelope):
    error = protocol_error_of(
        lambda: validate_server_message({"protocol": 1, "type": envelope})
    )
    assert error.reason == "unsupported_type"


@pytest.mark.parametrize("envelope", ["snapshot", "gap", "result", "error"])
def test_known_server_envelope_without_its_fields_is_missing_keys(envelope):
    error = protocol_error_of(
        lambda: validate_server_message({"protocol": 1, "type": envelope})
    )
    assert error.reason == "missing_key"


def test_server_envelope_unknown_key_and_protocol_are_rejected():
    message = snapshot_message()
    message["verb"] = "enable"
    assert protocol_error_of(lambda: validate_server_message(message)).reason == "unknown_key"
    assert (
        protocol_error_of(
            lambda: validate_server_message(snapshot_message(protocol="1"))
        ).reason
        == "protocol_mismatch"
    )


def test_capabilities_rejects_bool_mismatch():
    with pytest.raises(ValueError):
        Capabilities(True, True, False, 0)
