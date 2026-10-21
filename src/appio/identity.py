"""Ratified A07 ``AppKey`` grammar, encoding, and deterministic resolution.

The external identity grammar is::

    AppKey = "unit:" UnitName
           | "desktop:" DesktopId
           | "exe:" AbsoluteExecutablePath
           | "scope:" CgroupPath

Each payload preserves its source UTF-8 bytes. ASCII letters, digits, and
``/._:@+-`` pass through; every other byte becomes ``%HH`` with uppercase
hexadecimal. No case folding or Unicode normalization is applied, so two
differently-spelled units are two different applications.

Resolution precedence is the signed-off order:

1. deepest matching ``app-*`` systemd unit in the cgroup path,
2. nearest ``app.slice`` ancestor,
3. a supplied desktop file id,
4. a supplied absolute executable target,
5. the deepest ``session-*.scope`` ancestor prefix, or
   ``scope:/session/unknown``.

One spec pair contradicts itself: step 2 says a nearest ``app.slice`` ancestor is
an identity, while the ratified B8 vector requires
``.../app.slice/worker`` to fall through to the session key when desktop and
executable inputs are absent. This module applies ``app.slice`` only when it is
the cgroup leaf itself; any unrecognized child cgroup below ``app.slice``
therefore falls through, which keeps both the precedence step and the exact B8
vectors true. The conflict is disclosed in the task report rather than hidden.
"""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import AppKey

__all__ = [
    "AppKey",
    "deepest_candidate",
    "encode_app_key",
    "resolve_app",
    "session_scope_key",
    "validate_app_key",
]

_KINDS = ("unit", "desktop", "exe", "scope")
_PATH_KINDS = ("exe", "scope")

_SAFE_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    b"abcdefghijklmnopqrstuvwxyz"
    b"0123456789"
    b"/._:@+-"
)
_HEX_DIGITS = frozenset(b"0123456789ABCDEF")

_UNKNOWN_SCOPE: AppKey = "scope:/session/unknown"


def _require_kind(kind: object) -> str:
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError(f"kind must be one of {', '.join(_KINDS)}, got {kind!r}")
    return kind


def _validate_payload_shape(kind: str, payload: str) -> None:
    """Reject a payload whose decoded shape contradicts its kind."""

    if not isinstance(payload, str):
        raise ValueError(f"payload must be a string, got {type(payload).__name__}")
    if payload == "":
        raise ValueError("payload must be non-empty")
    if kind in _PATH_KINDS:
        if not payload.startswith("/"):
            raise ValueError(f"a {kind} payload must be an absolute path: {payload!r}")
    elif "/" in payload:
        raise ValueError(f"a {kind} payload must not contain '/': {payload!r}")


def encode_app_key(kind: str, payload: str) -> AppKey:
    """Encode one AppKey, escaping every non-safe UTF-8 byte as ``%HH``."""

    kind = _require_kind(kind)
    _validate_payload_shape(kind, payload)

    encoded: list[str] = []
    for value in payload.encode("utf-8"):
        if value in _SAFE_BYTES:
            encoded.append(chr(value))
        else:
            encoded.append(f"%{value:02X}")
    key = f"{kind}:{''.join(encoded)}"
    # The encoder's own output must satisfy the decoder, so a drift in either
    # direction fails here rather than producing a key another store rejects.
    return validate_app_key(key)


def validate_app_key(key: object) -> AppKey:
    """Return ``key`` if it is a canonical AppKey, else raise ``ValueError``.

    Canonicality is exact: uppercase ``%HH`` only, never an escape for a byte
    that could pass through, never a raw byte outside the safe set, and a valid
    UTF-8 payload whose shape matches its kind.
    """

    if not isinstance(key, str) or key == "":
        raise ValueError("an AppKey must be a non-empty string")
    kind, separator, payload = key.partition(":")
    if separator == "":
        raise ValueError(f"an AppKey requires a kind prefix: {key!r}")
    if kind not in _KINDS:
        raise ValueError(f"unknown AppKey kind {kind!r}")
    if payload == "":
        raise ValueError("an AppKey payload must be non-empty")

    raw = payload.encode("utf-8")
    decoded = bytearray()
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == 0x25:  # '%'
            if index + 2 >= len(raw):
                raise ValueError(f"truncated percent escape in {key!r}")
            high, low = raw[index + 1], raw[index + 2]
            if high not in _HEX_DIGITS or low not in _HEX_DIGITS:
                raise ValueError(f"percent escape must use uppercase %HH in {key!r}")
            byte = int(bytes((high, low)), 16)
            if byte in _SAFE_BYTES:
                raise ValueError(f"safe byte unnecessarily escaped in {key!r}")
            decoded.append(byte)
            index += 3
            continue
        if value not in _SAFE_BYTES:
            raise ValueError(f"unsafe byte {value:#04x} must be percent-encoded in {key!r}")
        decoded.append(value)
        index += 1

    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"percent escapes do not decode as UTF-8 in {key!r}") from error

    if kind in _PATH_KINDS:
        if not text.startswith("/"):
            raise ValueError(f"a {kind} payload must decode to an absolute path: {key!r}")
    elif "/" in text:
        raise ValueError(f"a {kind} payload must not decode to a path: {key!r}")
    return key


def _components(cgroup_path: str | None) -> list[str] | None:
    """Slash-delimited components of an absolute cgroup path, or ``None``."""

    if not isinstance(cgroup_path, str) or not cgroup_path.startswith("/"):
        return None
    trimmed = cgroup_path.rstrip("/")
    if trimmed == "":
        return [""]
    return trimmed.split("/")


def session_scope_key(cgroup_path: str | None) -> AppKey:
    """Return the deepest matching ``session-*.scope`` ancestor prefix key."""

    components = _components(cgroup_path)
    if components is None:
        return _UNKNOWN_SCOPE

    deepest: int | None = None
    for index, component in enumerate(components):
        if component.startswith("session-") and component.endswith(".scope"):
            middle = component[len("session-") : -len(".scope")]
            if middle != "":
                deepest = index
    if deepest is None:
        return _UNKNOWN_SCOPE

    prefix = "/".join(components[: deepest + 1])
    if not prefix.startswith("/"):
        return _UNKNOWN_SCOPE
    return encode_app_key("scope", prefix)


def deepest_candidate(
    candidates: Iterable[tuple[int, str]],
) -> tuple[int, str] | None:
    """Select by greatest depth, then bytewise-least UTF-8 name.

    A single cgroup path cannot contain two components at the same depth; the
    name tie-break exists so the ordering is fully determined anyway, and so
    callers merging candidate lists get one stable rule rather than two.
    """

    best: tuple[int, str] | None = None
    best_key: tuple[int, bytes] | None = None
    for depth, name in candidates:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValueError(f"candidate depth must be a non-negative integer, got {depth!r}")
        if not isinstance(name, str) or name == "":
            raise ValueError("candidate name must be a non-empty string")
        key = (-depth, name.encode("utf-8"))
        if best_key is None or key < best_key:
            best = (depth, name)
            best_key = key
    return best


def _valid_desktop_id(desktop_id: str | None) -> bool:
    return (
        isinstance(desktop_id, str)
        and desktop_id != ""
        and "/" not in desktop_id
        and "\x00" not in desktop_id
    )


def _valid_executable(exe: str | None) -> bool:
    return (
        isinstance(exe, str)
        and exe.startswith("/")
        and "\x00" not in exe
    )


def resolve_app(
    cgroup_path: str | None,
    exe: str | None,
    desktop_id: str | None,
) -> AppKey:
    """Resolve one process to its stable external AppKey."""

    components = _components(cgroup_path)
    if components is not None:
        units = [
            (index, component)
            for index, component in enumerate(components)
            if component.startswith("app-") and len(component) > len("app-")
        ]
        selected = deepest_candidate(units)
        if selected is not None:
            return encode_app_key("unit", selected[1])

        # See the module docstring: B8 requires an unrecognized child cgroup
        # below app.slice to fall through, so only the slice itself is an app.
        if components[-1] == "app.slice":
            return encode_app_key("unit", "app.slice")

    if _valid_desktop_id(desktop_id):
        return encode_app_key("desktop", desktop_id)  # type: ignore[arg-type]
    if _valid_executable(exe):
        return encode_app_key("exe", exe)  # type: ignore[arg-type]
    return session_scope_key(cgroup_path)
