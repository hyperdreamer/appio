"""Length-delimited JSON framing for the AppIO local socket (design §4.2, B10/B11).

One frame is a 4-byte unsigned network-order length prefix followed by one UTF-8
JSON **object**. There is no newline delimiter and no second framing layer, so a
receiver must be able to stop reading before a body exists.

The three properties this module exists to guarantee:

* an oversized or malformed **header** is rejected from the header alone, so a
  peer cannot make the receiver allocate a body it announced and never sent;
* the JSON parser refuses duplicate object keys (which a dict would silently
  collapse), non-finite numbers (``NaN``/``Infinity``/``1e999``), unbounded
  nesting and unbounded container membership;
* a large concatenated ``feed()`` is decoded frame by frame in place instead of
  being copied into a growing internal buffer once per frame.

A :class:`ProtocolError` poisons the decoder on purpose. Framing damage means the
stream can no longer be trusted to be aligned, so the transport closes the
connection rather than guessing where the next frame begins.
"""

from __future__ import annotations

import json
import math
import struct

from typing import Any

__all__ = [
    "FrameDecoder",
    "HEADER",
    "MAX_CONTAINER_MEMBERS",
    "MAX_INBOUND",
    "MAX_JSON_DEPTH",
    "MAX_OUTBOUND",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "encode_frame",
]

HEADER = struct.Struct("!I")
"""The four-byte network-order length prefix."""

PROTOCOL_VERSION = 1
"""The only wire protocol version this module speaks."""

MAX_INBOUND = 1 << 20
"""Largest accepted inbound payload: 1 MiB (design §4.2)."""

MAX_OUTBOUND = 256 << 10
"""Largest emitted payload: 256 KiB (design §4.2, D4)."""

MAX_JSON_DEPTH = 32
"""Deepest accepted JSON nesting, counting the top-level object as depth 1."""

MAX_CONTAINER_MEMBERS = 16_384
"""Largest aggregate number of object entries plus array items in one frame."""


class ProtocolError(ValueError):
    """A framing or message-shape rejection with a stable machine ``reason``.

    ``reason`` is the token a transport maps to an error reply or a disconnect
    decision. ``str(error)`` is the bounded human detail and always contains the
    token, so ``pytest.raises(..., match=...)`` and logs agree.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        if not isinstance(reason, str) or reason == "":
            raise ValueError("reason must be a non-empty string")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("detail must be a string or None")
        text = reason if detail is None else f"{reason}: {detail}"
        super().__init__(text)
        self.reason = reason
        self.detail = detail


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object, refusing a repeated key at that nesting level."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_key", f"object repeats key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    """Refuse the JSON extensions ``NaN``, ``Infinity`` and ``-Infinity``."""

    raise ProtocolError("nonfinite_number", f"non-finite JSON constant {name}")


def _check_number(value: float) -> None:
    """Reject a non-finite float, including an overflowed literal such as ``1e999``."""

    if not math.isfinite(value):
        raise ProtocolError("nonfinite_number", "JSON number is not finite")


def _check_value_bounds(value: Any) -> None:
    """Reject excessive nesting and aggregate container membership.

    Iterative on purpose: the point of the bound is to keep a hostile frame from
    driving the checker itself into ``RecursionError``.
    """

    stack: list[tuple[Any, int]] = [(value, 1)]
    members = 0
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ProtocolError(
                "nesting_too_deep", f"JSON nesting exceeds {MAX_JSON_DEPTH} levels"
            )
        if isinstance(node, dict):
            members += len(node)
            if members > MAX_CONTAINER_MEMBERS:
                raise ProtocolError(
                    "too_many_members", f"JSON has more than {MAX_CONTAINER_MEMBERS} members"
                )
            for item in node.values():
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            members += len(node)
            if members > MAX_CONTAINER_MEMBERS:
                raise ProtocolError(
                    "too_many_members", f"JSON has more than {MAX_CONTAINER_MEMBERS} members"
                )
            for item in node:
                stack.append((item, depth + 1))
        elif isinstance(node, float):
            _check_number(node)


def _check_outbound(value: Any) -> None:
    """Reject a message that would serialize to something the decoder refuses.

    A dict with a non-string key would coerce to a string and could then collide
    with a genuine string key, so outbound objects are held to the same
    no-duplicate-key rule by construction.
    """

    # ``(node, depth, entering)``: an exit marker lets a shared sub-object appear
    # twice in one message while a genuine cycle is still detected.
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 1, True)]
    members = 0
    while stack:
        node, depth, entering = stack.pop()
        if not entering:
            active.discard(id(node))
            continue
        if depth > MAX_JSON_DEPTH:
            raise ProtocolError(
                "nesting_too_deep", f"JSON nesting exceeds {MAX_JSON_DEPTH} levels"
            )
        if isinstance(node, dict):
            if id(node) in active:
                raise ProtocolError("invalid_message", "message contains a circular reference")
            active.add(id(node))
            members += len(node)
            if members > MAX_CONTAINER_MEMBERS:
                raise ProtocolError(
                    "too_many_members", f"JSON has more than {MAX_CONTAINER_MEMBERS} members"
                )
            stack.append((node, depth, False))
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ProtocolError(
                        "invalid_message", "JSON object keys must be strings"
                    )
                stack.append((item, depth + 1, True))
        elif isinstance(node, list):
            if id(node) in active:
                raise ProtocolError("invalid_message", "message contains a circular reference")
            active.add(id(node))
            members += len(node)
            if members > MAX_CONTAINER_MEMBERS:
                raise ProtocolError(
                    "too_many_members", f"JSON has more than {MAX_CONTAINER_MEMBERS} members"
                )
            stack.append((node, depth, False))
            for item in node:
                stack.append((item, depth + 1, True))
        elif isinstance(node, float):
            _check_number(node)
        elif node is None or isinstance(node, (str, bool, int)):
            continue
        else:
            raise ProtocolError(
                "invalid_message", f"{type(node).__name__} is not a JSON value"
            )


def encode_frame(message: dict[str, Any], *, max_bytes: int = MAX_OUTBOUND) -> bytes:
    """Serialize one message as ``<length>|<utf-8 json object>``.

    ``max_bytes`` bounds the payload only, matching the design's per-frame egress
    cap. A message that cannot be represented without changing meaning is
    rejected rather than coerced.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", "a frame payload must be a JSON object")
    _check_outbound(message)
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - pre-checked above
        raise ProtocolError("invalid_message", f"payload is not JSON-encodable: {exc}") from exc
    if not payload or len(payload) > max_bytes:
        raise ProtocolError(
            "frame_too_large",
            f"payload of {len(payload)} bytes exceeds the {max_bytes}-byte bound",
        )
    return HEADER.pack(len(payload)) + payload


class FrameDecoder:
    """Incremental decoder for a stream of length-prefixed JSON frames."""

    __slots__ = ("_buffer", "_max_bytes")

    def __init__(self, *, max_bytes: int = MAX_INBOUND) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._max_bytes = max_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[dict[str, Any], ...]:
        """Absorb ``data`` and return every newly completed frame, in order.

        Pending bytes are held in one buffer that is compacted once per call, and
        a header is inspected before any body is retained, so the retained size
        is bounded by one frame plus the current call's input rather than by the
        number of frames it contained.
        """

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("feed requires a bytes-like object")
        buffer = self._buffer
        buffer.extend(data)
        frames: list[dict[str, Any]] = []
        offset = 0
        total = len(buffer)
        while total - offset >= HEADER.size:
            (length,) = HEADER.unpack_from(buffer, offset)
            if length == 0:
                raise ProtocolError("empty_frame", "frame length prefix is zero")
            if length > self._max_bytes:
                raise ProtocolError(
                    "frame_too_large",
                    f"header announces {length} bytes, above the {self._max_bytes}-byte bound",
                )
            end = offset + HEADER.size + length
            if end > total:
                break
            frames.append(_decode_payload(bytes(buffer[offset + HEADER.size : end])))
            offset = end
        if offset:
            del buffer[:offset]
        return tuple(frames)

    def eof(self) -> None:
        """Finish the stream, rejecting a header or body that stopped mid-frame."""

        if self._buffer:
            raise ProtocolError(
                "truncated_frame",
                f"stream ended with {len(self._buffer)} unread byte(s)",
            )
        return None


def _decode_payload(payload: bytes) -> dict[str, Any]:
    """Decode one body into a JSON object, enforcing every parser bound."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(
            "invalid_utf8", f"payload is not valid UTF-8 at byte {exc.start}"
        ) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ProtocolError:
        raise
    except RecursionError as exc:
        raise ProtocolError("nesting_too_deep", "JSON nesting is too deep to parse") from exc
    except ValueError as exc:
        raise ProtocolError("invalid_json", f"payload is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("invalid_message", "a frame payload must be a JSON object")
    _check_value_bounds(value)
    return value
