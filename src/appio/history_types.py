"""Immutable, Qt-free records for the live Overview lane.

This module owns only neutral value types and pure helpers: no I/O, registry,
query service, client, router, Future, mutable global cache or Qt object. It
imports only the standard library and :mod:`appio.contracts` (§1.2).

The records here are observations, not handles. Every one is
``@dataclass(frozen=True)`` with exactly the fields this module lists and a
validating ``__post_init__``; invalid direct or nested construction raises
``ValueError``, never an assertion. Integer fields exclude ``bool`` and carry no
SQLite ceiling unless a contract value supplies its own bound. Nested frozen
objects are revalidated rather than trusted, so a forged object with changed
fields is not proof of validity.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields
from typing import Literal, TypeVar, cast

from .contracts import (
    AppTotal,
    Coverage,
    DiskShare,
    Series,
    Tier,
    Window,
    validate_window,
)

__all__ = [
    "AcceptedIdentity",
    "AnswerKind",
    "AnswerProvenance",
    "CapturedSourceImage",
    "CapturedSourceSet",
    "FrameProvenance",
    "LocalBracket",
    "LocalPosition",
    "LocalPositionVector",
    "ManagedSourceSpec",
    "PartKind",
    "PartPhase",
    "PartState",
    "RemotePosition",
    "RetentionScope",
    "RetainedRecord",
    "SeriesMetadata",
    "SnapshotProvenance",
    "SourcePosition",
    "applied_endpoint",
    "local_answer_digest",
    "retention_scope",
    "validate_scope",
]

PartKind = Literal["totals", "series"]
AnswerKind = Literal["ranking", "totals", "series"]
PartPhase = Literal["absent", "pending", "ok", "failed"]

RetentionScope = (
    tuple[str, Literal["range"], str, int, int]
    | tuple[str, Literal["since_boot"], str]
    | tuple[str, Literal["lifetime"]]
    | tuple[str, Literal["launch"]]
)

_PART_KINDS: tuple[str, ...] = ("totals", "series")
_ANSWER_KINDS: tuple[str, ...] = ("ranking", "totals", "series")
_PART_PHASES: tuple[str, ...] = ("absent", "pending", "ok", "failed")
_SCOPE_TAGS: tuple[str, ...] = ("range", "since_boot", "lifetime", "launch")
_OWNERS: tuple[str, ...] = ("gui", "iod")
_TIERS: tuple[str, ...] = ("raw", "rollup_60s", "rollup_3600s", "totals_only")

#: The StreamCursor boot-text bound (ipc_models, 256 UTF-8 bytes). Booting text
#: is mirrored here rather than imported so the neutral module stays free of
#: ``appio.ipc_models``.
_MAX_BOOT_ID_BYTES = 256

_HEX_DIGITS = frozenset("0123456789abcdef")

_LOCAL_DIGEST_TAG = "appio-local-answer-v1"


# ---------------------------------------------------------------------------
# Primitive validation
# ---------------------------------------------------------------------------


def _text(
    value: object, field: str, *, max_bytes: int | None = None, allow_empty: bool = False
) -> str:
    if not isinstance(value, str) or (value == "" and not allow_empty):
        requirement = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{field} must be {requirement}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} is not valid UTF-8 text") from error
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return value


def _boot_text(value: object, field: str) -> str:
    """Nonempty boot identity obeying the existing StreamCursor text rules."""

    text = _text(value, field, max_bytes=_MAX_BOOT_ID_BYTES)
    if "\x00" in text:
        raise ValueError(f"{field} must not contain NUL")
    return text


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}, got {value}")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _window(value: object) -> Window:
    if not isinstance(value, Window):
        raise ValueError(f"a Window is required, got {type(value).__name__}")
    return validate_window(value)


def _revalidate(record: object) -> object:
    """Re-run one frozen record's own validation instead of trusting its fields."""

    type(record).__post_init__(record)  # type: ignore[attr-defined]
    return record


def _revalidate_tree(value: object) -> None:
    """Recursively re-enter every nested frozen record's ``__post_init__``.

    Contract records only check the *types* of their nested children, so a
    forged grandchild (a ``DeviceShareRow`` with a bool counter, a ``HookHits``
    with a negative hit) survives the parent's own validation. A retained or
    metadata record is an observation, so the whole value tree is validated
    here, never trusted because an outer ``isinstance`` passed.
    """

    if isinstance(value, tuple):
        for item in value:
            _revalidate_tree(item)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("history values must be finite numbers")
        return
    if value is None or isinstance(value, (bool, int, str, bytes)):
        return
    if isinstance(value, (list, dict, set, frozenset)):
        raise ValueError("history records carry tuples, not mutable collections")
    if isinstance(value, type) or not hasattr(value, "__dataclass_fields__"):
        raise ValueError(f"unsupported history value {type(value).__name__}")
    _revalidate(value)
    for field in fields(value):  # type: ignore[arg-type]
        _revalidate_tree(getattr(value, field.name))


_T = TypeVar("_T")


def _record(record: object, expected: type[_T], field: str) -> _T:
    if not isinstance(record, expected):
        raise ValueError(f"{field} must be a {expected.__name__}")
    _revalidate_tree(record)
    return cast(_T, record)


def _source_position(value: object, field: str) -> "RemotePosition | LocalPositionVector":
    if isinstance(value, RemotePosition):
        _revalidate_tree(value)
        return value  # type: ignore[return-value]
    if isinstance(value, LocalPositionVector):
        _revalidate_tree(value)
        return value  # type: ignore[return-value]
    raise ValueError(f"{field} must be a RemotePosition or LocalPositionVector")


def _digest_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError("a data digest is exactly 64 lowercase hexadecimal characters")
    return value


# ---------------------------------------------------------------------------
# Retention scope and applied endpoint
# ---------------------------------------------------------------------------


def validate_scope(scope: object) -> RetentionScope:
    """Return the canonical retention-scope tuple, or raise ``ValueError``."""

    if not isinstance(scope, tuple):
        raise ValueError("a retention scope must be a tuple")
    if len(scope) < 2:
        raise ValueError("a retention scope carries a subject and a tag")
    app_key = _text(scope[0], "scope.app_key")
    tag = _enum(scope[1], _SCOPE_TAGS, "scope.tag")
    if tag == "range":
        if len(scope) != 5:
            raise ValueError("a range scope is (app_key, 'range', boot, start, end)")
        boot_id = _boot_text(scope[2], "scope.boot_id")
        start = _integer(scope[3], "scope.start_mono_ns")
        end = _integer(scope[4], "scope.end_mono_ns")
        if start >= end:
            raise ValueError("range scope bounds must be strictly increasing and half-open")
        return (app_key, "range", boot_id, start, end)
    if tag == "since_boot":
        if len(scope) != 3:
            raise ValueError("a since_boot scope is (app_key, 'since_boot', boot)")
        boot_id = _boot_text(scope[2], "scope.boot_id")
        return (app_key, "since_boot", boot_id)
    if len(scope) != 2:
        raise ValueError(f"a {tag} scope carries only its subject")
    if tag == "lifetime":
        return (app_key, "lifetime")
    return (app_key, "launch")


def retention_scope(app_key: str, window: Window) -> RetentionScope:
    """The exact retention key for one subject and its requested Window."""

    key = _text(app_key, "scope.app_key")
    window = _window(window)
    if window.kind == "range":
        return (
            key,
            "range",
            _boot_text(window.boot_id, "window.boot_id"),
            _integer(window.start_mono_ns, "window.start_mono_ns"),
            _integer(window.end_mono_ns, "window.end_mono_ns"),
        )
    if window.kind == "since_boot":
        return (key, "since_boot", _boot_text(window.boot_id, "window.boot_id"))
    if window.kind == "lifetime":
        # A lifetime window never acquires a boot or endpoint.
        return (key, "lifetime")
    # The launch key supplies no launch semantics: A22 refuses launch earlier.
    return (key, "launch")


def applied_endpoint(window: Window) -> int | None:
    """The applied endpoint for range/since-boot, or ``None`` for the others."""

    window = _window(window)
    if window.kind in ("range", "since_boot"):
        return window.end_mono_ns
    return None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptedIdentity:
    """The complete logical identity of one accepted answer."""

    kind: AnswerKind
    app_key: str
    window: Window
    data_digest: str

    def __post_init__(self) -> None:
        _enum(self.kind, _ANSWER_KINDS, "accepted_identity.kind")
        _text(self.app_key, "accepted_identity.app_key")
        _window(self.window)
        _digest_text(self.data_digest)


@dataclass(frozen=True)
class RemotePosition:
    """One verified daemon cursor: the boot, epoch and sequence triple."""

    boot_id: str
    iod_epoch: int
    seq: int

    def __post_init__(self) -> None:
        _boot_text(self.boot_id, "remote_position.boot_id")
        _integer(self.iod_epoch, "remote_position.iod_epoch", minimum=1)
        _integer(self.seq, "remote_position.seq")


@dataclass(frozen=True)
class ManagedSourceSpec:
    """Immutable connection configuration for one managed history source."""

    name: str
    owner: Literal["gui", "iod"]
    database_uri: str
    connect_options: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _text(self.name, "managed_source_spec.name")
        _enum(self.owner, _OWNERS, "managed_source_spec.owner")
        uri = _text(self.database_uri, "managed_source_spec.database_uri")
        if "\x00" in uri:
            raise ValueError("managed_source_spec.database_uri must not contain NUL")
        if not isinstance(self.connect_options, tuple):
            raise ValueError("managed_source_spec.connect_options must be a tuple")
        for option in self.connect_options:
            if not isinstance(option, tuple) or len(option) != 2:
                raise ValueError("each connect option must be an immutable text pair")
            _text(option[0], "managed_source_spec.connect_options[].key")
            _text(option[1], "managed_source_spec.connect_options[].value")


@dataclass(frozen=True)
class LocalPosition:
    """One registered source's O(1) change token, never a table hash."""

    name: str
    owner: Literal["gui", "iod"]
    registration_epoch: int
    data_version: int
    total_changes: int

    def __post_init__(self) -> None:
        _text(self.name, "local_position.name")
        _enum(self.owner, _OWNERS, "local_position.owner")
        _integer(self.registration_epoch, "local_position.registration_epoch", minimum=1)
        _integer(self.data_version, "local_position.data_version", minimum=1)
        _integer(self.total_changes, "local_position.total_changes")


@dataclass(frozen=True)
class LocalPositionVector:
    """Every registered source, unique and ordered by UTF-8 name bytes."""

    sources: tuple[LocalPosition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple):
            raise ValueError("local_position_vector.sources must be a tuple")
        if not self.sources:
            raise ValueError("an open-router vector always contains its primary source")
        encoded: list[bytes] = []
        for source in self.sources:
            _record(source, LocalPosition, "local_position_vector.sources[]")
            encoded.append(source.name.encode("utf-8"))
        if len(set(encoded)) != len(encoded):
            raise ValueError("local_position_vector.sources names must be unique")
        if encoded != sorted(encoded):
            raise ValueError(
                "local_position_vector.sources must be sorted by name UTF-8 bytes"
            )


@dataclass(frozen=True)
class LocalBracket:
    """Opening and closing observations; unequal is valid race evidence."""

    opening: LocalPositionVector
    closing: LocalPositionVector

    def __post_init__(self) -> None:
        _record(self.opening, LocalPositionVector, "local_bracket.opening")
        _record(self.closing, LocalPositionVector, "local_bracket.closing")


@dataclass(frozen=True)
class CapturedSourceImage:
    """Serialized bytes and digest for one registered source incarnation."""

    name: str
    registration_epoch: int
    image: bytes
    image_digest: str

    def __post_init__(self) -> None:
        _text(self.name, "captured_source_image.name")
        _integer(
            self.registration_epoch, "captured_source_image.registration_epoch", minimum=1
        )
        if not isinstance(self.image, bytes):
            raise ValueError("captured_source_image.image must be immutable bytes")
        _digest_text(self.image_digest)
        if self.image_digest != hashlib.sha256(self.image).hexdigest():
            raise ValueError("captured_source_image.image_digest must match image bytes")


@dataclass(frozen=True)
class CapturedSourceSet:
    """One complete, ordered image set with its opening and closing vectors."""

    capture_id: int
    opening: LocalPositionVector
    closing: LocalPositionVector
    images: tuple[CapturedSourceImage, ...]
    snapshot_digest: str

    def __post_init__(self) -> None:
        _integer(self.capture_id, "captured_source_set.capture_id", minimum=1)
        _record(self.opening, LocalPositionVector, "captured_source_set.opening")
        _record(self.closing, LocalPositionVector, "captured_source_set.closing")
        _digest_text(self.snapshot_digest)
        if not isinstance(self.images, tuple):
            raise ValueError("captured_source_set.images must be a tuple")
        encoded: list[bytes] = []
        for image in self.images:
            _record(image, CapturedSourceImage, "captured_source_set.images[]")
            encoded.append(image.name.encode("utf-8"))
        if len(set(encoded)) != len(encoded):
            raise ValueError("captured_source_set.images names must be unique")
        if encoded != sorted(encoded):
            raise ValueError("captured_source_set.images must be sorted by name UTF-8 bytes")
        members = tuple((image.name, image.registration_epoch) for image in self.images)
        for vector in (self.opening, self.closing):
            sources = tuple(
                (source.name, source.registration_epoch) for source in vector.sources
            )
            if members != sources:
                raise ValueError(
                    "captured images must match every source incarnation in both vectors"
                )


@dataclass(frozen=True)
class SnapshotProvenance:
    """The capture identity and local bracket supporting an observed answer."""

    capture_id: int
    snapshot_digest: str
    bracket: LocalBracket

    def __post_init__(self) -> None:
        _integer(self.capture_id, "snapshot_provenance.capture_id", minimum=1)
        _digest_text(self.snapshot_digest)
        _record(self.bracket, LocalBracket, "snapshot_provenance.bracket")


@dataclass(frozen=True)
class AnswerProvenance:
    """The measured Window, identity, stamp and proved source of one answer."""

    kind: AnswerKind
    app_key: str
    scope: RetentionScope
    measured_window: Window
    accepted_identity: AcceptedIdentity | None
    first_seen_at_ns: int | None
    answered_source: "SourcePosition | None"
    local_bracket: LocalBracket | None
    snapshot: SnapshotProvenance | None = None

    def __post_init__(self) -> None:
        _enum(self.kind, _ANSWER_KINDS, "answer_provenance.kind")
        _text(self.app_key, "answer_provenance.app_key")
        window = _window(self.measured_window)
        if validate_scope(self.scope) != retention_scope(self.app_key, window):
            raise ValueError(
                "answer_provenance.scope must match its subject and measured window"
            )

        identity = self.accepted_identity
        stamp = self.first_seen_at_ns
        if identity is None and stamp is None:
            pass
        elif identity is None or stamp is None:
            raise ValueError(
                "answer_provenance identity and first-seen stamp are both present or both None"
            )
        else:
            _record(identity, AcceptedIdentity, "answer_provenance.accepted_identity")
            if identity.kind != self.kind:
                raise ValueError("accepted identity kind must match the provenance kind")
            if identity.app_key != self.app_key:
                raise ValueError("accepted identity subject must match the provenance subject")
            if identity.window != window:
                raise ValueError("accepted identity window must be the measured window")
            _integer(stamp, "answer_provenance.first_seen_at_ns")

        answered = self.answered_source
        bracket = self.local_bracket
        if answered is not None:
            _source_position(answered, "answer_provenance.answered_source")
        if bracket is not None:
            _record(bracket, LocalBracket, "answer_provenance.local_bracket")
            if bracket.opening == bracket.closing:
                if answered != bracket.opening:
                    raise ValueError(
                        "an equal local bracket is the answered source"
                    )
            elif answered is not None:
                raise ValueError(
                    "an unequal local bracket proves no answered source"
                )
        elif isinstance(answered, LocalPositionVector):
            raise ValueError("a local answered source requires its local bracket")
        if isinstance(answered, RemotePosition) and bracket is not None:
            raise ValueError("a remote answered source cannot carry a local bracket")
        snapshot = self.snapshot
        if snapshot is not None:
            _record(snapshot, SnapshotProvenance, "answer_provenance.snapshot")
            if bracket is None or snapshot.bracket != bracket:
                raise ValueError("answer snapshot evidence must match its local bracket")
        if identity is not None and bracket is not None and snapshot is None:
            raise ValueError("a local accepted identity requires snapshot evidence")


@dataclass(frozen=True)
class RetainedRecord:
    """One retained detail value with its provenance; never a ranking page."""

    value: DiskShare | Series
    provenance: AnswerProvenance

    def __post_init__(self) -> None:
        provenance = _record(self.provenance, AnswerProvenance, "retained_record.provenance")
        if provenance.kind == "totals":  # type: ignore[attr-defined]
            if not isinstance(self.value, DiskShare):
                raise ValueError("a totals record retains a DiskShare")
            _revalidate_tree(self.value)
        elif provenance.kind == "series":  # type: ignore[attr-defined]
            if not isinstance(self.value, Series):
                raise ValueError("a series record retains a Series")
            _revalidate_tree(self.value)
        else:
            raise ValueError("a ranking cannot be stored as a retained detail record")
        if self.value.app_id <= 0:
            raise ValueError("a retained value needs a positive presentation subject ID")
        if self.value.window != provenance.measured_window:  # type: ignore[attr-defined]
            raise ValueError("a retained value window must be its measured window")


@dataclass(frozen=True)
class PartState:
    """One immutable slot observation; see the exhaustive four-state table."""

    kind: PartKind
    app_key: str
    scope: RetentionScope
    state: PartPhase
    request_id: int | None
    requested_window: Window | None
    record: RetainedRecord | None
    reason: str | None

    def __post_init__(self) -> None:
        _enum(self.kind, _PART_KINDS, "part_state.kind")
        _text(self.app_key, "part_state.app_key")
        scope = validate_scope(self.scope)
        if scope[0] != self.app_key:
            raise ValueError("part_state.scope must name its subject")
        phase = _enum(self.state, _PART_PHASES, "part_state.state")

        request_id = self.request_id
        window = self.requested_window
        if window is not None:
            window = _window(window)
            if retention_scope(self.app_key, window) != scope:
                raise ValueError("part_state.requested window must match the slot scope")
        if request_id is None:
            if window is not None:
                raise ValueError("a requested window requires its request token")
        else:
            _integer(request_id, "part_state.request_id", minimum=1)
            if window is None:
                raise ValueError("a request token requires its requested window")

        record = self.record
        reason = self.reason
        if phase == "absent":
            if request_id is not None:
                raise ValueError("an absent part carries no request token")
            if record is not None or reason is not None:
                raise ValueError("an absent part carries no record and no reason")
            return
        if request_id is None:  # pragma: no cover - guarded above
            raise ValueError(f"a {phase} part requires its request token")
        if reason is not None and phase != "failed":
            raise ValueError(f"a {phase} part carries no reason")
        if record is not None and phase != "ok":
            raise ValueError(f"a {phase} part carries no record")
        if phase == "failed":
            _text(reason, "part_state.reason")
            return
        if phase == "pending":
            if reason is not None or record is not None:
                raise ValueError("a pending part carries no record and no reason")
            return
        # phase == "ok"
        if record is None:
            raise ValueError("an ok part carries its retained record")
        record = _record(record, RetainedRecord, "part_state.record")
        provenance = record.provenance  # type: ignore[attr-defined]
        if provenance.kind != self.kind:
            raise ValueError("the retained record kind must match the slot kind")
        if provenance.app_key != self.app_key:
            raise ValueError("the retained record subject must match the slot subject")
        if provenance.scope != scope:
            raise ValueError("the retained record scope must match the slot scope")
        if provenance.measured_window != window:
            raise ValueError("the retained record window must match the requested window")


@dataclass(frozen=True, init=False)
class FrameProvenance:
    """Prepared window, requested/answered source and exact certification."""

    generation: int
    window: Window
    requested_source: "SourcePosition"
    answered_source: "SourcePosition | None"
    local_bracket: LocalBracket | None
    snapshot: SnapshotProvenance | None
    certified: bool

    def __init__(
        self,
        generation: int,
        window: Window,
        requested_source: SourcePosition,
        answered_source: SourcePosition | None,
        local_bracket: LocalBracket | None,
        *args: object,
        snapshot: SnapshotProvenance | None = None,
        certified: bool | None = None,
    ) -> None:
        if len(args) > 2:
            raise TypeError("FrameProvenance accepts at most seven positional arguments")
        if len(args) == 2:
            if snapshot is not None or certified is not None:
                raise TypeError("snapshot or certified supplied twice")
            snapshot = cast(SnapshotProvenance | None, args[0])
            certified = cast(bool | None, args[1])
        elif args:
            if isinstance(args[0], bool) and certified is None:
                certified = args[0]
            else:
                if snapshot is not None:
                    raise TypeError("snapshot supplied twice")
                snapshot = cast(SnapshotProvenance | None, args[0])
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "window", window)
        object.__setattr__(self, "requested_source", requested_source)
        object.__setattr__(self, "answered_source", answered_source)
        object.__setattr__(self, "local_bracket", local_bracket)
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "certified", certified)
        self.__post_init__()

    def __post_init__(self) -> None:
        _integer(self.generation, "frame_provenance.generation")
        _window(self.window)
        requested = _source_position(
            self.requested_source, "frame_provenance.requested_source"
        )
        answered = self.answered_source
        if answered is not None:
            _source_position(answered, "frame_provenance.answered_source")
        bracket = self.local_bracket
        if bracket is not None:
            bracket = _record(bracket, LocalBracket, "frame_provenance.local_bracket")  # type: ignore[assignment]
        certified = _boolean(self.certified, "frame_provenance.certified")

        if isinstance(requested, LocalPositionVector) and bracket is None:
            raise ValueError("a local requested source requires its local bracket")
        if isinstance(answered, LocalPositionVector) and bracket is None:
            # A remote failure can fall back to local data; that local answer
            # still needs its opening/closing bracket, not just a remote request.
            raise ValueError("a local answered source requires its local bracket")
        if isinstance(answered, RemotePosition) and bracket is not None:
            raise ValueError("a remote answered source cannot carry a local bracket")
        if bracket is not None:
            if bracket.opening == bracket.closing:
                if answered != bracket.opening:
                    raise ValueError("an equal local bracket is the answered source")
            elif answered is not None:
                raise ValueError("an unequal local bracket proves no answered source")
        snapshot = self.snapshot
        if snapshot is not None:
            _record(snapshot, SnapshotProvenance, "frame_provenance.snapshot")
            if bracket is None or snapshot.bracket != bracket:
                raise ValueError("frame snapshot evidence must match its local bracket")
        if certified and (answered is None or answered != requested):
            raise ValueError(
                "certification requires an answered source equal to the requested source"
            )
        # Ranking success and source-incarnation checks are supplied by the
        # producer. This neutral record validates their provenance evidence.


@dataclass(frozen=True)
class SeriesMetadata:
    """The successful effective Series a render model projects its points from."""

    app_id: int
    app_key: str
    measured_window: Window
    tier: Tier
    coverage: Coverage

    def __post_init__(self) -> None:
        _integer(self.app_id, "series_metadata.app_id", minimum=1)
        _text(self.app_key, "series_metadata.app_key")
        _window(self.measured_window)
        tier = _enum(self.tier, _TIERS, "series_metadata.tier")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("series_metadata.coverage must be a Coverage")
        _revalidate_tree(self.coverage)
        if self.coverage.tier != tier:
            raise ValueError("series_metadata.tier must equal coverage.tier")


SourcePosition = RemotePosition | LocalPositionVector


# ---------------------------------------------------------------------------
# Canonical local-answer digest
# ---------------------------------------------------------------------------


def _encode_value(value: object) -> object:
    """Canonical JSON value: dataclass field objects, tuples as arrays, None kept."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("history values must be finite numbers")
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("history text must be UTF-8 encodable") from error
        return value
    if isinstance(value, tuple):
        return [_encode_value(item) for item in value]
    if isinstance(value, list):
        raise ValueError("history collections are tuples, not lists")
    if isinstance(value, dict):
        raise ValueError("history values carry no mapping fields")
    if isinstance(value, type) or not hasattr(value, "__dataclass_fields__"):
        raise ValueError(f"unsupported history value {type(value).__name__}")
    _revalidate(value)
    # Presentation app_id is not an external identity; omit it at every level.
    return {
        field.name: _encode_value(getattr(value, field.name))
        for field in fields(value)  # type: ignore[arg-type]
        if field.name != "app_id"
    }


def local_answer_digest(
    kind: AnswerKind,
    app_key: str,
    window: Window,
    value: AppTotal | DiskShare | Series,
) -> str:
    """SHA-256 of the canonical local payload, with ``app_id`` omitted."""

    answer_kind = _enum(kind, _ANSWER_KINDS, "answer.kind")
    key = _text(app_key, "answer.app_key")
    exact = _window(window)

    if answer_kind == "ranking":
        if not isinstance(value, AppTotal):
            raise ValueError("a ranking digest requires an AppTotal")
        _revalidate(value)
        if value.app_key != key:
            raise ValueError("the ranking value must match its external subject")
    elif answer_kind == "totals":
        if not isinstance(value, DiskShare):
            raise ValueError("a totals digest requires a DiskShare")
        _revalidate(value)
        if value.window != exact:
            raise ValueError("the totals value must match the exact window")
    else:
        if not isinstance(value, Series):
            raise ValueError("a series digest requires a Series")
        _revalidate(value)
        if value.window != exact:
            raise ValueError("the series value must match the exact window")

    payload = [_LOCAL_DIGEST_TAG, answer_kind, key, _encode_value(exact), _encode_value(value)]
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
