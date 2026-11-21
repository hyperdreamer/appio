"""Neutral retained-answer and source records: exact fields, phases and digests.

The single required test on this module validates every §2 record field, the
PartState truth table, the canonical local-answer digest, and that importing the
neutral module stays free of the client, router, query service and Qt.
"""

from __future__ import annotations

from concurrent.futures import Future
import copy
from dataclasses import FrozenInstanceError, fields, replace
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from appio.bpf_fileio import BpfFileSource, VectorLoader
from appio.contracts import (
    FILE_HOUR_NS,
    DeviceShareRow,
    FileCoverage,
    FileRow,
    FileTable,
    HistoryPending,
    HookHits,
    UnknownOwnerOverflow,
    Window,
    WindowUnavailableError,
)
from appio.file_runtime import FileRuntime
from appio.files import FileIdentityResolver
from appio.history_router import (
    FileViewResult,
    HistoryRouter,
    HistorySegment,
    LocalFilesOutcome,
    LocalFrameOutcome,
    LocalPartOutcome,
    LocalSettlement,
    RemoteSettlement,
    RequestToken,
)
from appio.history_types import (
    AcceptedIdentity,
    AnswerProvenance,
    FrameProvenance,
    LocalBracket,
    LocalPosition,
    LocalPositionVector,
    PartState,
    RemotePosition,
    RetainedRecord,
    SeriesMetadata,
    applied_endpoint,
    local_answer_digest,
    retention_scope,
)
from appio.handover import baseline_seed
from appio.history_validation import _normalize_request
from appio.iodclient import (
    AppRegistry,
    ClientState,
    ConnectionLost,
    QueryTransportState,
    RemoteError,
    RequestRejected,
)
from appio.ipc_models import Capabilities, QueryRequest, StreamCursor
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.query import QueryService
from appio.sampler import TickInput, aggregate_tick
from appio.store import AppIoStore
from live_overview_support import Clock, coverage, series, share, total
from tests.p4.test_file_runtime import Frames as FileFrames
from tests.p4.test_file_runtime import event as file_event

KEY = "unit:alpha.service"
OTHER_KEY = "unit:beta.service"
DIGEST = "a" * 64
NS = 1_000_000_000
BOOT = "boot-a"
ALPHA = "exe:/usr/bin/alpha"
BETA = "exe:/usr/bin/beta"


def _reject(label: str, call) -> None:
    """Assert ``call`` raises ValueError (not another exception) for ``label``."""

    try:
        call()
    except ValueError:
        return
    except Exception as error:  # noqa: BLE001 - a wrong exception type is a failure
        raise AssertionError(f"{label}: raised {type(error).__name__}, not ValueError") from error
    raise AssertionError(f"{label}: invalid record was accepted")


def _assert_frozen(record: object, name: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(record, name, getattr(record, name))


def test_managed_source_rejects_empty_option_value() -> None:
    from appio.history_types import ManagedSourceSpec

    with pytest.raises(ValueError):
        ManagedSourceSpec("local", "gui", "file:/tmp/a.db", (("x", ""),))


def test_captured_image_rejects_digest_mismatch() -> None:
    from appio.history_types import CapturedSourceImage

    with pytest.raises(ValueError):
        CapturedSourceImage("local", 1, b"abc", "a" * 64)


def test_frame_provenance_accepts_legacy_six_arguments() -> None:
    window = Window("lifetime")
    remote = RemotePosition("boot-a", 1, 0)
    frame = FrameProvenance(3, window, remote, remote, None, True)
    assert frame.snapshot is None
    assert frame.certified is True

    vector = LocalPositionVector((LocalPosition("local", "gui", 1, 1, 0),))
    bracket = LocalBracket(vector, vector)
    local_frame = FrameProvenance(3, window, vector, vector, bracket, True)
    assert local_frame.snapshot is None
    assert local_frame.certified is True
    with pytest.raises(ValueError):
        FrameProvenance(3, window, remote, remote, None, snapshot=True)


def test_live_records_validate_every_field_and_phase_combination() -> None:
    clock = Clock(5)
    assert clock() == 5
    clock.advance_ns(7)
    assert clock.ns == 12
    _reject("clock.negative", lambda: Clock(-1))
    _reject("clock.bool", lambda: Clock(True))
    _reject("clock.advance-negative", lambda: clock.advance_ns(-1))

    since_boot = Window("since_boot", "boot-a", end_mono_ns=20_000_000_000)
    lived = Window("lifetime")
    range_w = Window("range", "boot-a", 1_000_000_000, 20_000_000_000)
    launch = Window("launch", "boot-a", launch_id=7)
    scope = retention_scope(KEY, since_boot)
    assert scope == (KEY, "since_boot", "boot-a")
    assert retention_scope(KEY, lived) == (KEY, "lifetime")
    assert retention_scope(KEY, range_w) == (
        KEY,
        "range",
        "boot-a",
        1_000_000_000,
        20_000_000_000,
    )
    assert retention_scope(KEY, launch) == (KEY, "launch")
    assert applied_endpoint(since_boot) == 20_000_000_000
    assert applied_endpoint(range_w) == 20_000_000_000
    assert applied_endpoint(Window("lifetime")) is None
    assert applied_endpoint(lived) is None
    assert applied_endpoint(launch) is None

    _reject("scope.empty-subject", lambda: retention_scope("", since_boot))
    _reject("scope.bool-subject", lambda: retention_scope(True, since_boot))
    _reject("scope.not-window", lambda: retention_scope(KEY, {"kind": "lifetime"}))
    forged_window = Window("since_boot", "boot-a", end_mono_ns=20_000_000_000)
    object.__setattr__(forged_window, "end_mono_ns", True)
    _reject("scope.forged-window", lambda: retention_scope(KEY, forged_window))
    _reject("endpoint.forged-window", lambda: applied_endpoint(forged_window))

    remote = RemotePosition("boot-a", 1, 0)
    assert remote == RemotePosition("boot-a", 1, 0)
    assert RemotePosition("boot-a", 2**70, 2**70) == RemotePosition("boot-a", 2**70, 2**70)
    assert RemotePosition("é" * 128, 1, 0).boot_id == "é" * 128
    _reject("remote.bool-epoch", lambda: RemotePosition("boot-a", True, 0))
    _reject("remote.zero-epoch", lambda: RemotePosition("boot-a", 0, 0))
    _reject("remote.negative-seq", lambda: RemotePosition("boot-a", 1, -1))

    a = share(7, since_boot)
    assert local_answer_digest("totals", KEY, since_boot, a) == local_answer_digest(
        "totals", KEY, since_boot, replace(a, app_id=91)
    )
    assert local_answer_digest("totals", KEY, since_boot, a) != local_answer_digest(
        "totals", KEY, since_boot, replace(a, task_read_bytes=18, task_total_bytes=23)
    )

    # -- revision-8 managed-source and capture record availability --------- #
    import appio.history_types as history_types

    for name in (
        "ManagedSourceSpec", "CapturedSourceImage", "CapturedSourceSet", "SnapshotProvenance",
    ):
        assert hasattr(history_types, name), f"missing revision-8 record {name}"
        assert name in history_types.__all__
    from appio.history_types import (
        CapturedSourceImage,
        CapturedSourceSet,
        ManagedSourceSpec,
        SnapshotProvenance,
    )

    # -- immutable field inventory ----------------------------------------- #
    inventory = {
        AcceptedIdentity: ("kind", "app_key", "window", "data_digest"),
        RemotePosition: ("boot_id", "iod_epoch", "seq"),
        LocalPosition: ("name", "owner", "registration_epoch", "data_version", "total_changes"),
        LocalPositionVector: ("sources",),
        LocalBracket: ("opening", "closing"),
        ManagedSourceSpec: ("name", "owner", "database_uri", "connect_options"),
        CapturedSourceImage: ("name", "registration_epoch", "image", "image_digest"),
        CapturedSourceSet: (
            "capture_id", "opening", "closing", "images", "snapshot_digest",
        ),
        SnapshotProvenance: ("capture_id", "snapshot_digest", "bracket"),
        AnswerProvenance: (
            "kind",
            "app_key",
            "scope",
            "measured_window",
            "accepted_identity",
            "first_seen_at_ns",
            "answered_source",
            "local_bracket",
            "snapshot",
        ),
        RetainedRecord: ("value", "provenance"),
        PartState: (
            "kind",
            "app_key",
            "scope",
            "state",
            "request_id",
            "requested_window",
            "record",
            "reason",
        ),
        FrameProvenance: (
            "generation",
            "window",
            "requested_source",
            "answered_source",
            "local_bracket",
            "snapshot",
            "certified",
        ),
        SeriesMetadata: ("app_id", "app_key", "measured_window", "tier", "coverage"),
    }
    for record, names in inventory.items():
        assert tuple(field.name for field in fields(record)) == names, record.__name__
        assert record.__dataclass_params__.frozen is True, record.__name__
    _assert_frozen(remote, "seq")
    _assert_frozen(since_boot, "kind")

    # -- AcceptedIdentity --------------------------------------------------- #
    identity = AcceptedIdentity("totals", KEY, since_boot, DIGEST)
    assert identity.window == since_boot
    for label, bad in (
        ("kind", "dashboard"),
        ("kind", True),
        ("kind", 3),
        ("app_key", ""),
        ("app_key", 7),
        ("app_key", "\ud800"),
        ("window", {"kind": "since_boot"}),
        ("window", forged_window),
        ("data_digest", "A" * 64),
        ("data_digest", "a" * 63),
        ("data_digest", "a" * 65),
        ("data_digest", "g" * 64),
        ("data_digest", 5),
    ):
        _reject(
            f"accepted_identity.{label}.{bad!r}",
            lambda label=label, bad=bad: AcceptedIdentity(
                **{**{f.name: getattr(identity, f.name) for f in fields(identity)}, label: bad}
            ),
        )

    # -- RemotePosition / LocalPosition / LocalPositionVector / LocalBracket  #
    for label, bad in (
        ("boot_id.empty", ""),
        ("boot_id.nul", "boot\x00a"),
        ("boot_id.long", "x" * 257),
        ("boot_id.long-utf8", "é" * 129),
        ("boot_id.unencodable", "\ud800"),
        ("boot_id.int", 5),
        ("iod_epoch.zero", 0),
        ("iod_epoch.bool", True),
        ("iod_epoch.float", 1.5),
        ("seq.negative", -1),
        ("seq.bool", True),
        ("seq.float", 1.0),
    ):
        field_name, _ = label.split(".")
        _reject(
            f"remote_position.{label}",
            lambda field_name=field_name, bad=bad: RemotePosition(
                **{**{"boot_id": "boot-a", "iod_epoch": 1, "seq": 0}, field_name: bad}
            ),
        )

    position = LocalPosition("local", "gui", 1, 1, 0)
    assert LocalPosition("local", "gui", 1, 2**70, 2**80) == LocalPosition(
        "local", "gui", 1, 2**70, 2**80
    )
    for label, bad in (
        ("name.empty", ""),
        ("name.int", 5),
        ("name.unencodable", "\ud800"),
        ("owner.enum", "root"),
        ("owner.bool", True),
        ("owner.case", "GUI"),
        ("registration_epoch.zero", 0),
        ("registration_epoch.bool", True),
        ("registration_epoch.negative", -1),
        ("data_version.zero", 0),
        ("data_version.bool", False),
        ("total_changes.negative", -1),
        ("total_changes.bool", True),
    ):
        field_name, _ = label.split(".")
        defaults = {
            "name": "local",
            "owner": "gui",
            "registration_epoch": 1,
            "data_version": 1,
            "total_changes": 0,
        }
        _reject(
            f"local_position.{label}",
            lambda field_name=field_name, bad=bad, defaults=defaults: LocalPosition(
                **{**defaults, field_name: bad}
            ),
        )
    forged_position = LocalPosition("local", "gui", 1, 1, 0)
    object.__setattr__(forged_position, "data_version", 0)
    _reject("local_position.forged", lambda: LocalPositionVector((forged_position,)))

    vector = LocalPositionVector((position,))
    assert vector == LocalPositionVector((position,))
    assert LocalPositionVector((LocalPosition("z", "gui", 1, 1, 0), LocalPosition("é", "iod", 2, 1, 0))).sources[0].name == "z"
    _reject("vector.empty", lambda: LocalPositionVector(()))
    _reject("vector.list", lambda: LocalPositionVector([position]))  # type: ignore[arg-type]
    _reject(
        "vector.duplicate",
        lambda: LocalPositionVector((position, LocalPosition("local", "iod", 2, 1, 0))),
    )
    _reject(
        "vector.unsorted",
        lambda: LocalPositionVector(
            (LocalPosition("b", "gui", 1, 1, 0), LocalPosition("a", "iod", 2, 1, 0))
        ),
    )
    _reject(
        "vector.unsorted-bytes",
        lambda: LocalPositionVector(
            (LocalPosition("é", "iod", 2, 1, 0), LocalPosition("z", "gui", 1, 1, 0))
        ),
    )
    _reject("vector.non-position", lambda: LocalPositionVector((remote,)))  # type: ignore[arg-type]
    _reject("vector.forged", lambda: LocalPositionVector((forged_position,)))

    tolerant = LocalPositionVector((position,))
    changed = LocalPositionVector((LocalPosition("local", "gui", 1, 1, 1),))
    bracket = LocalBracket(tolerant, tolerant)
    assert LocalBracket(tolerant, changed).opening != LocalBracket(tolerant, changed).closing
    _reject("bracket.opening", lambda: LocalBracket(remote, tolerant))  # type: ignore[arg-type]
    _reject("bracket.closing", lambda: LocalBracket(tolerant, [position]))  # type: ignore[arg-type]

    # -- ManagedSourceSpec ------------------------------------------------- #
    spec = ManagedSourceSpec(
        "local", "gui", "file:/tmp/local.db?mode=rw", (("timeout", "5"), ("isolation_level", "DEFERRED"))
    )
    assert spec.database_uri == "file:/tmp/local.db?mode=rw"
    assert spec.connect_options == (("timeout", "5"), ("isolation_level", "DEFERRED"))
    assert replace(spec, owner="iod", connect_options=()).owner == "iod"
    for label, bad in (
        ("name", ""),
        ("name", True),
        ("name", "\ud800"),
        ("owner", "root"),
        ("owner", True),
        ("owner", "GUI"),
        ("database_uri", ""),
        ("database_uri", 5),
        ("database_uri", "file:a\x00b"),
        ("database_uri", "file:\ud800"),
        ("connect_options", []),
        ("connect_options", {"timeout": "5"}),
        ("connect_options", lambda: {}),
        ("connect_options", (["timeout", "5"],)),
        ("connect_options", (("timeout",),)),
        ("connect_options", (("timeout", "5", "extra"),)),
        ("connect_options", ((True, "5"),)),
        ("connect_options", (("timeout", 5),)),
        ("connect_options", (("factory", lambda: {}),)),
        ("connect_options", (("timeout", {}),)),
        ("connect_options", (("\ud800", "5"),)),
        ("connect_options", (("timeout", "\ud800"),)),
    ):
        _reject(
            f"managed_source.{label}.{bad!r}",
            lambda label=label, bad=bad: replace(spec, **{label: bad}),
        )

    # -- CapturedSourceImage / CapturedSourceSet / SnapshotProvenance ------- #
    image = CapturedSourceImage(
        "local", 1, b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    captured = CapturedSourceSet(1, tolerant, tolerant, (image,), DIGEST)
    snapshot = SnapshotProvenance(1, DIGEST, bracket)
    raced_bracket = LocalBracket(tolerant, changed)
    raced_capture = replace(captured, closing=changed)
    raced_snapshot = replace(snapshot, bracket=raced_bracket)
    assert captured.images[0].image == b"abc"
    assert raced_capture.opening != raced_capture.closing
    assert raced_snapshot.bracket == raced_bracket
    assert replace(image, registration_epoch=2**70).registration_epoch == 2**70
    assert replace(captured, capture_id=2**70).capture_id == 2**70
    assert replace(snapshot, capture_id=2**70).capture_id == 2**70
    for label, bad in (
        ("name", ""),
        ("name", 5),
        ("name", "\ud800"),
        ("registration_epoch", 0),
        ("registration_epoch", -1),
        ("registration_epoch", True),
        ("registration_epoch", 1.0),
        ("image", "abc"),
        ("image", bytearray(b"abc")),
        ("image", memoryview(b"abc")),
        ("image", (b"abc",)),
        ("image", True),
        ("image_digest", "A" * 64),
        ("image_digest", "a" * 63),
        ("image_digest", "a" * 65),
        ("image_digest", "g" * 64),
        ("image_digest", 5),
    ):
        _reject(
            f"captured_image.{label}.{bad!r}",
            lambda label=label, bad=bad: replace(image, **{label: bad}),
        )
    for record in (captured, snapshot):
        for capture_id in (0, -1, True, 1.0, "1"):
            _reject(
                f"{type(record).__name__}.capture_id.{capture_id!r}",
                lambda record=record, capture_id=capture_id: replace(record, capture_id=capture_id),
            )
        for digest in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, True):
            _reject(
                f"{type(record).__name__}.snapshot_digest.{digest!r}",
                lambda record=record, digest=digest: replace(record, snapshot_digest=digest),
            )

    second_position = LocalPosition("é", "iod", 2, 1, 0)
    complete_vector = LocalPositionVector((position, second_position))
    second_image = replace(image, name="é", registration_epoch=2)
    complete_capture = replace(
        captured, opening=complete_vector, closing=complete_vector, images=(image, second_image)
    )
    assert tuple(item.name for item in complete_capture.images) == ("local", "é")
    for label, kwargs in (
        ("opening.type", {"opening": remote}),
        ("closing.type", {"closing": (position,)}),
        ("images.list", {"images": [image, second_image]}),
        ("images.type", {"images": (image, second_position)}),
        ("images.empty", {"images": ()}),
        ("images.duplicate", {"images": (image, image, second_image)}),
        ("images.unsorted", {"images": (second_image, image)}),
        ("images.missing", {"images": (image,)}),
        ("images.extra", {"images": (image, replace(image, name="other"), second_image)}),
        ("opening.missing", {"opening": tolerant}),
        ("closing.missing", {"closing": tolerant}),
        ("opening.incarnation", {
            "opening": LocalPositionVector((replace(position, registration_epoch=3), second_position)),
        }),
        ("closing.incarnation", {
            "closing": LocalPositionVector((replace(position, registration_epoch=3), second_position)),
        }),
        ("images.incarnation", {"images": (replace(image, registration_epoch=3), second_image)}),
    ):
        _reject(
            f"captured_set.{label}",
            lambda kwargs=kwargs: replace(complete_capture, **kwargs),
        )
    _reject("snapshot.bracket-type", lambda: replace(snapshot, bracket=(tolerant, tolerant)))
    forged_image = copy.copy(image)
    object.__setattr__(forged_image, "image", bytearray(b"abc"))
    _reject("captured_set.forged-image", lambda: replace(captured, images=(forged_image,)))
    forged_capture_vector = copy.copy(tolerant)
    object.__setattr__(forged_capture_vector, "sources", ())
    _reject("captured_set.forged-opening", lambda: replace(captured, opening=forged_capture_vector))
    _reject("captured_set.forged-closing", lambda: replace(captured, closing=forged_capture_vector))
    forged_bracket = copy.copy(bracket)
    object.__setattr__(forged_bracket, "closing", forged_capture_vector)
    _reject("snapshot.forged-bracket", lambda: replace(snapshot, bracket=forged_bracket))
    for record, name in (
        (spec, "connect_options"), (image, "image"), (captured, "images"), (snapshot, "bracket"),
    ):
        _assert_frozen(record, name)

    # -- AnswerProvenance --------------------------------------------------- #
    provenance = AnswerProvenance("totals", KEY, scope, since_boot, identity, 1_000_000_000, None, None)
    assert provenance.accepted_identity == identity
    local_provenance = AnswerProvenance(
        "totals", KEY, scope, since_boot, identity, 0, tolerant, bracket, snapshot
    )
    assert local_provenance.answered_source == tolerant
    assert local_provenance.snapshot == snapshot
    raced_provenance = AnswerProvenance(
        "totals", KEY, scope, since_boot, identity, 0, None, raced_bracket, raced_snapshot
    )
    assert raced_provenance.answered_source is None
    remote_provenance = replace(provenance, answered_source=remote)
    assert remote_provenance.snapshot is None
    assert replace(local_provenance, accepted_identity=None, first_seen_at_ns=None).snapshot == snapshot
    for label, kwargs in (
        ("snapshot.missing-local", {"snapshot": None}),
        ("snapshot.type", {"snapshot": {"capture_id": 1}}),
        ("snapshot.bracket", {"snapshot": raced_snapshot}),
        ("snapshot.without-bracket", {"answered_source": None, "local_bracket": None}),
    ):
        _reject(
            f"answer_provenance.{label}",
            lambda kwargs=kwargs: replace(local_provenance, **kwargs),
        )
    _reject("answer_provenance.raced-without-snapshot", lambda: replace(raced_provenance, snapshot=None))
    _reject("answer_provenance.remote-snapshot", lambda: replace(remote_provenance, snapshot=snapshot))
    forged_snapshot = copy.copy(snapshot)
    object.__setattr__(forged_snapshot, "capture_id", True)
    _reject("answer_provenance.forged-snapshot", lambda: replace(local_provenance, snapshot=forged_snapshot))
    _assert_frozen(local_provenance, "snapshot")
    assert (
        AnswerProvenance("totals", KEY, scope, since_boot, None, None, None, None).accepted_identity
        is None
    )
    for label, kwargs in (
        ("kind", {"kind": "dashboard"}),
        ("app_key.empty", {"app_key": ""}),
        ("app_key.mismatch", {"app_key": OTHER_KEY}),
        ("scope.tag", {"scope": (KEY, "dashboard", "boot-a")}),
        ("scope.length", {"scope": (KEY, "since_boot", "boot-a", 1)}),
        ("scope.bounds", {"scope": (KEY, "range", "boot-a", 20, 10)}),
        ("scope.subject", {"scope": (OTHER_KEY, "since_boot", "boot-a")}),
        ("scope.not-tuple", {"scope": [KEY, "since_boot", "boot-a"]}),
        ("window.mapping", {"measured_window": {"kind": "since_boot"}}),
        ("window.forged", {"measured_window": forged_window}),
        ("identity.kind", {"accepted_identity": AcceptedIdentity("series", KEY, since_boot, DIGEST)}),
        ("identity.key", {"accepted_identity": AcceptedIdentity("totals", OTHER_KEY, since_boot, DIGEST)}),
        ("identity.window", {"accepted_identity": AcceptedIdentity("totals", KEY, range_w, DIGEST)}),
        ("stamp.negative", {"first_seen_at_ns": -1}),
        ("stamp.bool", {"first_seen_at_ns": True}),
        ("stamp.with-identity-absent", {"accepted_identity": None}),
        ("identity.with-stamp-absent", {"first_seen_at_ns": None}),
        ("source.mapping", {"answered_source": {"boot_id": "boot-a"}}),
        ("source.bool", {"answered_source": True}),
        ("source.local-without-bracket", {"answered_source": tolerant}),
        ("source.remote-with-bracket", {"answered_source": remote, "local_bracket": bracket}),
        ("bracket.equal-without-source", {"answered_source": None, "local_bracket": bracket}),
        ("bracket.unequal-with-source", {"answered_source": changed, "local_bracket": LocalBracket(tolerant, changed)}),
        ("bracket.type", {"local_bracket": [tolerant, tolerant]}),
    ):
        base = {
            "kind": "totals",
            "app_key": KEY,
            "scope": scope,
            "measured_window": since_boot,
            "accepted_identity": identity,
            "first_seen_at_ns": 1_000_000_000,
            "answered_source": None,
            "local_bracket": None,
        }
        _reject(
            f"answer_provenance.{label}",
            lambda kwargs=kwargs, base=base: AnswerProvenance(**{**base, **kwargs}),
        )

    # A forged nested identity/window/source must be revalidated, not trusted.
    forged_identity = AcceptedIdentity("totals", KEY, since_boot, DIGEST)
    object.__setattr__(forged_identity, "data_digest", "A" * 64)
    _reject(
        "answer_provenance.forged-identity",
        lambda: AnswerProvenance(
            "totals", KEY, scope, since_boot, forged_identity, 1_000_000_000, None, None
        ),
    )
    forged_vector = LocalPositionVector((position,))
    object.__setattr__(forged_vector, "sources", ())
    _reject(
        "answer_provenance.forged-bracket",
        lambda: AnswerProvenance(
            "totals", KEY, scope, since_boot, identity, 0, None, LocalBracket(forged_vector, forged_vector)
        ),
    )

    # -- RetainedRecord ----------------------------------------------------- #
    totals_record = RetainedRecord(share(7, since_boot), provenance)
    series_value = series(7, since_boot)
    series_identity = AcceptedIdentity("series", KEY, since_boot, DIGEST)
    series_provenance = AnswerProvenance(
        "series", KEY, scope, since_boot, series_identity, 1_000_000_000, None, None
    )
    series_record = RetainedRecord(series_value, series_provenance)
    assert totals_record.value.app_id == 7
    assert series_record.value.tier == "raw"
    _reject("record.totals-with-series", lambda: RetainedRecord(series_value, provenance))
    _reject("record.series-with-share", lambda: RetainedRecord(a, series_provenance))
    # The ranking negative first builds a *valid* ranking provenance: combining
    # kind ranking with a totals identity fails in AnswerProvenance instead.
    ranking_provenance = AnswerProvenance(
        "ranking",
        KEY,
        scope,
        since_boot,
        AcceptedIdentity("ranking", KEY, since_boot, DIGEST),
        1_000_000_000,
        None,
        None,
    )
    assert ranking_provenance.kind == "ranking"
    _reject("record.ranking", lambda: RetainedRecord(a, ranking_provenance))
    _reject(
        "record.window-mismatch",
        lambda: RetainedRecord(share(7, range_w), provenance),
    )
    _reject(
        "record.provenance-type",
        lambda: RetainedRecord(a, {"kind": "totals"}),  # type: ignore[arg-type]
    )

    # A forged grandchild inside an otherwise valid value is not trusted:
    # contracts validate their children's *types* only. Each control changes
    # exactly one nested field, so only the recursive walk can reject it.
    forged_row = DeviceShareRow("boot-a", "8:0", 9, 5, 14)
    object.__setattr__(forged_row, "rbytes", True)
    _reject(
        "record.forged-device-row",
        lambda: RetainedRecord(replace(a, devices=(forged_row,)), provenance),
    )
    forged_grandchild = coverage()
    object.__setattr__(forged_grandchild.hook_hits, "vfs_read", -1)
    _reject(
        "record.forged-hook-hits",
        lambda: RetainedRecord(replace(a, coverage=forged_grandchild), provenance),
    )

    # -- PartState truth table --------------------------------------------- #
    absent = PartState("totals", KEY, scope, "absent", None, None, None, None)
    pending = PartState("totals", KEY, scope, "pending", 1, since_boot, None, None)
    ok = PartState("totals", KEY, scope, "ok", 1, since_boot, totals_record, None)
    failed = PartState("totals", KEY, scope, "failed", 1, since_boot, None, "query_timeout")
    for state in (absent, pending, ok, failed):
        assert state.scope == scope
    _reject("part.absent-request", lambda: PartState("totals", KEY, scope, "absent", 1, None, None, None))
    _reject("part.absent-window", lambda: PartState("totals", KEY, scope, "absent", None, since_boot, None, None))
    _reject("part.absent-reason", lambda: PartState("totals", KEY, scope, "absent", None, None, None, "why"))
    _reject("part.pending-no-request", lambda: PartState("totals", KEY, scope, "pending", None, since_boot, None, None))
    _reject("part.pending-no-window", lambda: PartState("totals", KEY, scope, "pending", 1, None, None, None))
    _reject("part.pending-record", lambda: PartState("totals", KEY, scope, "pending", 1, since_boot, totals_record, None))
    _reject("part.pending-reason", lambda: PartState("totals", KEY, scope, "pending", 1, since_boot, None, "why"))
    _reject("part.ok-no-record", lambda: PartState("totals", KEY, scope, "ok", 1, since_boot, None, None))
    _reject("part.ok-reason", lambda: PartState("totals", KEY, scope, "ok", 1, since_boot, totals_record, "why"))
    _reject("part.ok-key", lambda: PartState("totals", OTHER_KEY, retention_scope(OTHER_KEY, since_boot), "ok", 1, since_boot, totals_record, None))
    _reject("part.ok-kind", lambda: PartState("series", KEY, scope, "ok", 1, since_boot, totals_record, None))
    # Same scope, different since-boot endpoint: only the exact record/token
    # Window comparison can reject this, because the scope tuple is equal.
    newer_endpoint = Window("since_boot", "boot-a", end_mono_ns=30_000_000_000)
    assert retention_scope(KEY, newer_endpoint) == scope
    _reject(
        "part.ok-endpoint",
        lambda: PartState("totals", KEY, scope, "ok", 1, newer_endpoint, totals_record, None),
    )
    _reject("part.ok-window", lambda: PartState("totals", KEY, scope, "ok", 1, range_w, totals_record, None))
    _reject("part.failed-no-reason", lambda: PartState("totals", KEY, scope, "failed", 1, since_boot, None, None))
    _reject("part.failed-empty-reason", lambda: PartState("totals", KEY, scope, "failed", 1, since_boot, None, ""))
    _reject("part.failed-int-reason", lambda: PartState("totals", KEY, scope, "failed", 1, since_boot, None, 5))
    _reject("part.failed-record", lambda: PartState("totals", KEY, scope, "failed", 1, since_boot, totals_record, "why"))
    _reject("part.request-zero", lambda: PartState("totals", KEY, scope, "pending", 0, since_boot, None, None))
    _reject("part.request-bool", lambda: PartState("totals", KEY, scope, "pending", True, since_boot, None, None))
    _reject("part.request-negative", lambda: PartState("totals", KEY, scope, "pending", -1, since_boot, None, None))
    _reject("part.kind", lambda: PartState("ranking", KEY, scope, "pending", 1, since_boot, None, None))
    _reject("part.state", lambda: PartState("totals", KEY, scope, "done", 1, since_boot, None, None))
    _reject("part.scope", lambda: PartState("totals", KEY, (OTHER_KEY, "since_boot", "boot-a"), "pending", 1, since_boot, None, None))
    _reject("part.window-type", lambda: PartState("totals", KEY, scope, "pending", 1, {"kind": "since_boot"}, None, None))
    _reject("part.window-scope", lambda: PartState("totals", KEY, scope, "pending", 1, range_w, None, None))
    _reject("part.record-type", lambda: PartState("totals", KEY, scope, "ok", 1, since_boot, a, None))

    # -- FrameProvenance ---------------------------------------------------- #
    assert FrameProvenance(3, since_boot, remote, remote, None, None, True).certified is True
    local_frame = FrameProvenance(3, since_boot, tolerant, tolerant, bracket, snapshot, True)
    assert local_frame.certified is True
    fallback = FrameProvenance(3, since_boot, remote, tolerant, bracket, snapshot, False)
    assert fallback.answered_source == tolerant
    uncertified = FrameProvenance(3, since_boot, tolerant, None, raced_bracket, raced_snapshot, False)
    assert uncertified.certified is False
    # Failed local work may have bracket evidence without a captured image.
    assert replace(uncertified, snapshot=None).certified is False
    _assert_frozen(local_frame, "snapshot")
    assert replace(local_frame, snapshot=None).certified is True
    for label, kwargs in (
        ("snapshot.type", {"snapshot": (1, DIGEST, bracket)}),
        ("snapshot.forged", {"snapshot": forged_snapshot}),
        ("snapshot.bracket", {"snapshot": raced_snapshot}),
        ("bracket.equal-without-source", {"answered_source": None, "certified": False}),
        ("bracket.unequal-with-source", {"local_bracket": raced_bracket, "snapshot": raced_snapshot, "certified": False}),
    ):
        _reject(
            f"frame_provenance.{label}",
            lambda kwargs=kwargs: replace(local_frame, **kwargs),
        )
    remote_frame = FrameProvenance(3, since_boot, remote, remote, None, None, False)
    _reject("frame_provenance.remote-snapshot", lambda: replace(remote_frame, snapshot=snapshot))
    _reject(
        "frame_provenance.unanswered-snapshot-without-bracket",
        lambda: replace(remote_frame, answered_source=None, snapshot=snapshot),
    )
    for label, kwargs in (
        ("generation.negative", {"generation": -1}),
        ("generation.bool", {"generation": True}),
        ("window.forged", {"window": forged_window}),
        ("requested.type", {"requested_source": {"boot_id": "boot-a"}}),
        # Isolated from certification: no answered source and certified=False,
        # so only the missing local-request bracket can reject this shape.
        (
            "requested.local-without-bracket",
            {"requested_source": tolerant, "answered_source": None, "certified": False},
        ),
        # Remote-to-local fallback: only the missing local bracket can reject it.
        (
            "answered.local-fallback-without-bracket",
            {"requested_source": remote, "answered_source": tolerant, "certified": False},
        ),
        ("answered.remote-with-bracket", {"answered_source": remote, "local_bracket": bracket}),
        ("certified.not-bool", {"certified": 1}),
        ("certified.empty", {"answered_source": None}),
        ("certified.mismatched", {"answered_source": RemotePosition("boot-a", 1, 1)}),
        ("certified.unequal-bracket", {"answered_source": changed, "local_bracket": LocalBracket(tolerant, changed)}),
    ):
        base = {
            "generation": 3,
            "window": since_boot,
            "requested_source": remote,
            "answered_source": remote,
            "local_bracket": None,
            "snapshot": None,
            "certified": True,
        }
        _reject(
            f"frame_provenance.{label}",
            lambda kwargs=kwargs, base=base: FrameProvenance(**{**base, **kwargs}),
        )

    # -- SeriesMetadata ----------------------------------------------------- #
    metadata = SeriesMetadata(7, KEY, since_boot, "raw", coverage(tier="raw"))
    assert metadata.coverage.tier == metadata.tier
    for label, kwargs in (
        ("app_id.zero", {"app_id": 0}),
        ("app_id.bool", {"app_id": True}),
        ("app_key.empty", {"app_key": ""}),
        ("window.forged", {"measured_window": forged_window}),
        ("tier.enum", {"tier": "minute"}),
        ("coverage.type", {"coverage": {"tier": "raw"}}),
        ("tier.mismatch", {"tier": "rollup_60s"}),
    ):
        base = {
            "app_id": 7,
            "app_key": KEY,
            "measured_window": since_boot,
            "tier": "raw",
            "coverage": coverage(tier="raw"),
        }
        _reject(
            f"series_metadata.{label}",
            lambda kwargs=kwargs, base=base: SeriesMetadata(**{**base, **kwargs}),
        )
    forged_coverage = coverage()
    object.__setattr__(forged_coverage, "complete", 1)
    _reject(
        "series_metadata.forged-coverage",
        lambda: SeriesMetadata(7, KEY, since_boot, "raw", forged_coverage),
    )
    forged_hook_hits = coverage()
    object.__setattr__(forged_hook_hits.hook_hits, "vfs_read", -1)
    _reject(
        "series_metadata.forged-hook-hits",
        lambda: SeriesMetadata(7, KEY, since_boot, "raw", forged_hook_hits),
    )

    # -- canonical local-answer digest ------------------------------------- #
    base_digest = local_answer_digest("totals", KEY, since_boot, a)
    assert len(base_digest) == 64
    assert set(base_digest) <= set("0123456789abcdef")
    # Independently encoded canonical vector: this literal was derived from a
    # hand-built §2.1 payload (domain tag, every Window and contract field,
    # app_id omitted), so it pins the tag and encoding, not just determinism.
    assert base_digest == "70c6320069cb9128073ef4915deefd5056a58ae2ddced05fb718156e7c9fc5af"
    assert base_digest == local_answer_digest("totals", KEY, since_boot, a)
    assert base_digest != local_answer_digest("totals", KEY, range_w, share(7, range_w))
    assert base_digest != local_answer_digest("totals", KEY, since_boot, replace(a, device_read_bytes=None, device_write_bytes=None, device_total_bytes=None, residual_read=None, residual_write=None, residual_total=None))
    assert base_digest != local_answer_digest("totals", KEY, since_boot, replace(a, coverage=coverage(resets=1)))
    assert base_digest != local_answer_digest("totals", KEY, since_boot, replace(a, devices=(DeviceShareRow("boot-a", "8:2", 9, 5, 14),)))
    assert base_digest != local_answer_digest("totals", KEY, since_boot, replace(a, devices=()))
    assert base_digest != local_answer_digest("totals", KEY, since_boot, replace(a, residual_total=0))
    assert base_digest != local_answer_digest("totals", KEY, since_boot, replace(a, residual_total=None))
    assert base_digest != local_answer_digest("totals", KEY, since_boot, replace(a, task_write_bytes=6, task_total_bytes=23))
    _reject("digest.kind-enum", lambda: local_answer_digest("files", KEY, since_boot, a))
    _reject("digest.kind-value", lambda: local_answer_digest("series", KEY, since_boot, a))
    _reject("digest.totals-with-series", lambda: local_answer_digest("totals", KEY, since_boot, series_value))
    _reject("digest.series-with-share", lambda: local_answer_digest("series", KEY, since_boot, a))
    _reject("digest.custom-window", lambda: local_answer_digest("totals", KEY, since_boot, share(7, range_w)))
    _reject("digest.empty-key", lambda: local_answer_digest("totals", "", since_boot, a))

    ranking_value = total(7, KEY)
    ranking_digest = local_answer_digest("ranking", KEY, since_boot, ranking_value)
    assert ranking_digest == local_answer_digest("ranking", KEY, since_boot, replace(ranking_value, app_id=91))
    assert ranking_digest != local_answer_digest("ranking", KEY, since_boot, total(7, KEY, peak=5))
    # Presentation identity is not external: app_id stays omitted, but every
    # Window field (including launch_id) enters the encoded payload.
    launch_a = Window("launch", "boot-a", launch_id=7)
    launch_b = Window("launch", "boot-a", launch_id=8)
    assert local_answer_digest("totals", KEY, launch_a, share(7, launch_a)) != local_answer_digest(
        "totals", KEY, launch_b, share(7, launch_b)
    )
    _reject("digest.ranking-key", lambda: local_answer_digest("ranking", KEY, since_boot, total(7, OTHER_KEY)))
    _reject("digest.ranking-share", lambda: local_answer_digest("ranking", KEY, since_boot, a))

    series_digest = local_answer_digest("series", KEY, since_boot, series_value)
    assert series_digest == local_answer_digest("series", KEY, since_boot, replace(series_value, app_id=91))
    assert series_digest != local_answer_digest("series", KEY, since_boot, replace(series_value, coverage=coverage(tier="raw", resets=1)))
    suspended = replace(
        series_value,
        points=(
            replace(series_value.points[0], spans_suspend=True),
            series_value.points[1],
        ),
    )
    assert series_digest != local_answer_digest("series", KEY, since_boot, suspended)
    reordered = replace(
        series_value,
        points=tuple(reversed(series_value.points)),
        device_points=tuple(reversed(series_value.device_points)),
        residual_points=tuple(reversed(series_value.residual_points)),
    )
    assert series_digest != local_answer_digest("series", KEY, since_boot, reordered)
    _reject("digest.series-window", lambda: local_answer_digest("series", KEY, since_boot, series(7, range_w)))
    nan_wall = replace(
        series_value,
        points=(replace(series_value.points[0], wall_start_seconds=float("nan")), series_value.points[1]),
    )
    _reject("digest.series-nan", lambda: local_answer_digest("series", KEY, since_boot, nan_wall))
    infinite_wall = replace(
        series_value,
        points=(replace(series_value.points[0], wall_start_seconds=float("inf")), series_value.points[1]),
    )
    _reject("digest.series-infinite", lambda: local_answer_digest("series", KEY, since_boot, infinite_wall))
    _reject("digest.forged-coverage", lambda: local_answer_digest("totals", KEY, since_boot, replace(a, coverage=forged_coverage)))

    # -- private router worker records (§3.4) ------------------------------- #
    ranking_request = QueryRequest("ranking", since_boot)
    detail_request = QueryRequest("totals", since_boot, KEY)
    files_request = QueryRequest("files", since_boot, KEY)
    registrations = (("local", "gui", 1),)
    local_authority = ("local", registrations)
    remote_authority = ("remote", 2, "boot-a", 1)
    local_vector = LocalPositionVector((LocalPosition("local", "gui", 1, 1, 0),))
    remote_position = RemotePosition("boot-a", 1, 0)

    private_inventory = {
        RequestToken: (
            "token_id", "request", "origin", "scope", "owner", "owner_id", "slot_id",
            "authority", "requested_source",
        ),
        LocalFrameOutcome: (
            "worker_id", "request", "ranking", "segments", "boundaries", "gaps",
            "known_gaps", "overlaps", "has_local_history", "not_retained",
            "chronology_unproved", "file_available",
        ),
        LocalPartOutcome: ("worker_id", "request", "app_key", "value", "data_digest"),
        LocalFilesOutcome: (
            "worker_id", "request", "app_key", "table", "coverage", "served_gap_count",
        ),
        RemoteSettlement: ("future", "response", "error"),
        LocalSettlement: ("worker_id", "outcome", "error", "local_bracket"),
    }
    for record, names in private_inventory.items():
        assert tuple(field.name for field in fields(record)) == names, record.__name__
        assert record.__dataclass_params__.frozen is True, record.__name__

    detail_token = RequestToken(1, detail_request, "local", scope, "detail", 1, None,
                                local_authority, local_vector)
    assert detail_token.scope == scope
    remote_token = RequestToken(2, ranking_request, "remote", None, "frame", 3, 9,
                                remote_authority, remote_position)
    local_frame_token = RequestToken(3, ranking_request, "local", None, "frame", 4, None,
                                     local_authority, local_vector)
    assert local_frame_token.origin == "local"
    _assert_frozen(remote_token, "slot_id")
    for label, kwargs in (
        ("token_id.zero", {"token_id": 0}),
        ("token_id.bool", {"token_id": True}),
        ("token_id.negative", {"token_id": -1}),
        ("request.not-query", {"request": {"kind": "ranking", "window": since_boot}}),
        ("request.invalid-selector", {"request": QueryRequest("series", since_boot)}),
        ("request.a22", {"request": QueryRequest("ranking", Window("launch", "boot-a", launch_id=1))}),
        ("origin.enum", {"origin": "peer"}),
        ("owner.enum", {"owner": "worker"}),
        ("owner_id.zero", {"owner_id": 0}),
        ("owner_id.bool", {"owner_id": False}),
        ("detail.no-scope", {"request": detail_request, "owner": "detail", "scope": None}),
        ("detail.no-subject", {"request": ranking_request, "owner": "detail", "scope": scope}),
        ("detail.scope-subject", {"scope": retention_scope(OTHER_KEY, since_boot)}),
        ("detail.scope-window", {"scope": retention_scope(KEY, range_w)}),
        ("frame.scope", {"owner": "frame", "scope": scope}),
        ("local.slot", {"slot_id": 1}),
        ("remote.no-slot", {"origin": "remote", "slot_id": None,
                            "requested_source": remote_position, "authority": remote_authority}),
        ("authority.tag", {"authority": ("bogus", 1)}),
        ("authority.remote-length", {"authority": ("remote", 1, "boot-a")}),
        ("authority.remote-bool-generation", {"authority": ("remote", True, "boot-a", 1)}),
        ("authority.remote-epoch", {"authority": ("remote", 1, "boot-a", 0)}),
        ("authority.local-length", {"authority": ("local", registrations, 1)}),
        ("authority.local-duplicate", {"authority": ("local", (("local", "gui", 1), ("local", "iod", 2)))}),
        ("authority.local-unsorted", {"authority": ("local", (("z", "gui", 1), ("a", "iod", 2)))}),
        ("authority.local-empty-name", {"authority": ("local", (("", "gui", 1),))}),
        ("authority.local-epoch", {"authority": ("local", (("local", "gui", 0),))}),
        ("source.remote-with-vector", {"origin": "remote", "slot_id": 1,
                                      "requested_source": local_vector,
                                      "authority": remote_authority}),
        ("source.local-with-remote", {"requested_source": remote_position}),
        ("source.mapping", {"requested_source": {"boot_id": "boot-a"}}),
    ):
        base = {
            "token_id": 2,
            "request": ranking_request,
            "origin": "local",
            "scope": None,
            "owner": "frame",
            "owner_id": 4,
            "slot_id": None,
            "authority": local_authority,
            "requested_source": local_vector,
        }
        _reject(
            f"request_token.{label}",
            lambda kwargs=kwargs, base=base: RequestToken(**{**base, **kwargs}),
        )
    forged_vector = LocalPositionVector((LocalPosition("local", "gui", 1, 1, 0),))
    object.__setattr__(forged_vector, "sources", ())
    _reject(
        "request_token.forged-source",
        lambda: RequestToken(1, ranking_request, "local", None, "frame", 1, None,
                             local_authority, forged_vector),
    )
    # A detail token is already valid before one field is varied. The former
    # cases used a frame base, so they failed the "frame carries no scope" guard
    # instead of the subject/Window scope equality their labels claimed.
    detail_base = {
        "token_id": 1,
        "request": detail_request,
        "origin": "local",
        "scope": scope,
        "owner": "detail",
        "owner_id": 1,
        "slot_id": None,
        "authority": local_authority,
        "requested_source": local_vector,
    }
    assert RequestToken(**detail_base) == detail_token
    for label, kwargs in (
        ("detail.scope-other-subject", {"scope": retention_scope(OTHER_KEY, since_boot)}),
        ("detail.scope-other-window", {"scope": retention_scope(KEY, range_w)}),
    ):
        _reject(
            f"request_token.{label}",
            lambda kwargs=kwargs, base=detail_base: RequestToken(**{**base, **kwargs}),
        )

    # LocalFrameOutcome: all copied routing evidence, no eager detail handle.
    frame_segments = (
        HistorySegment("local", "gui", "boot-a", 0, 4 * NS, True),
        HistorySegment("export", "iod", "boot-a", 4 * NS, 8 * NS, True),
        HistorySegment("unproved", "unknown", "boot-a", 8 * NS, 10 * NS, False),
    )
    frame_boundaries = (("boot-a", 4 * NS, "gui", "iod"),)
    frame_gaps = (("boot-a", 8 * NS, 10 * NS),)
    # Known-gap evidence may overlap across sources; there is no global rule.
    frame_known_gaps = (("boot-a", 8 * NS, 9 * NS + NS // 2), ("boot-a", 9 * NS, 10 * NS))
    frame = LocalFrameOutcome(
        2, ranking_request, (total(7, KEY), total(8, OTHER_KEY)), frame_segments,
        frame_boundaries, frame_gaps, frame_known_gaps, (), False, False, False, True,
    )
    # Boundaries are a chronology: valid ordered evidence is the control, and a
    # permutation or repeat of that same evidence is not another valid plan.
    chrono_segments = (
        HistorySegment("local", "gui", "boot-a", 0, 4 * NS, True),
        HistorySegment("export", "iod", "boot-a", 4 * NS, 8 * NS, True),
        HistorySegment("local", "gui", "boot-a", 8 * NS, 12 * NS, True),
    )
    chrono_boundaries = (("boot-a", 4 * NS, "gui", "iod"), ("boot-a", 8 * NS, "iod", "gui"))
    chrono_frame = LocalFrameOutcome(
        2, ranking_request, (total(7, KEY),), chrono_segments,
        chrono_boundaries, (), (), (), False, False, False, True,
    )
    assert chrono_frame.boundaries == chrono_boundaries
    assert frame.ranking[0].app_key == KEY
    assert frame.known_gaps[0][2] > frame.known_gaps[1][1]
    _assert_frozen(frame, "ranking")
    for label, kwargs in (
        ("worker_id.zero", {"worker_id": 0}),
        ("worker_id.bool", {"worker_id": True}),
        ("request.totals", {"request": QueryRequest("totals", since_boot, KEY)}),
        ("request.subject", {"request": QueryRequest("ranking", since_boot, KEY)}),
        ("ranking.list", {"ranking": [total(7, KEY)]}),
        ("ranking.mutable-row", {"ranking": ([total(7, KEY)],)}),
        ("ranking.duplicate", {"ranking": (total(7, KEY), total(8, KEY))}),
        ("ranking.not-row", {"ranking": ({"app_key": KEY},)}),
        ("segments.list", {"segments": list(frame_segments)}),
        ("segments.not-segment", {"segments": (("local", "gui", "boot-a", 0, NS),)}),
        ("segments.order", {"segments": (frame_segments[1], frame_segments[0])}),
        # Nested segments promise real UTF-8 boot text; the pre-existing segment
        # validator only checks identity truthiness, so these cross this boundary.
        ("segments.boot-int", {
            "segments": (HistorySegment("local", "gui", 123, 0, 4 * NS, True),),
            "boundaries": (),
        }),
        ("segments.boot-tuple", {
            "segments": (HistorySegment("local", "gui", ("x",), 0, 4 * NS, True),),
            "boundaries": (),
        }),
        ("segments.boot-nul", {
            "segments": (HistorySegment("local", "gui", "a\x00b", 0, 4 * NS, True),),
            "boundaries": (),
        }),
        ("segments.boot-encoding", {
            "segments": (HistorySegment("local", "gui", "a\ud800", 0, 4 * NS, True),),
            "boundaries": (),
        }),
        ("boundaries.list", {"boundaries": list(frame_boundaries)}),
        ("boundaries.length", {"boundaries": (("boot-a", 4 * NS, "gui"),)}),
        ("boundaries.boot", {"boundaries": (("", 4 * NS, "gui", "iod"),)}),
        ("boundaries.negative", {"boundaries": (("boot-a", -1, "gui", "iod"),)}),
        ("boundaries.bool", {"boundaries": (("boot-a", True, "gui", "iod"),)}),
        ("boundaries.owner-enum", {"boundaries": (("boot-a", 4 * NS, "gui", "root"),)}),
        ("boundaries.same-owner", {"boundaries": (("boot-a", 4 * NS, "gui", "gui"),)}),
        ("boundaries.unsupported", {"boundaries": (("boot-a", 2 * NS, "gui", "iod"),)}),
        ("boundaries.other-boot", {"boundaries": (("other", 4 * NS, "gui", "iod"),)}),
        ("boundaries.reversed", {
            "segments": chrono_segments,
            "boundaries": tuple(reversed(chrono_boundaries)),
        }),
        ("boundaries.duplicate", {
            "segments": chrono_segments,
            "boundaries": chrono_boundaries + (chrono_boundaries[-1],),
        }),
        ("boundaries.boot-order", {
            "segments": (
                HistorySegment("local", "gui", "boot-a", 0, 4 * NS, True),
                HistorySegment("export", "iod", "boot-a", 4 * NS, 8 * NS, True),
                HistorySegment("local", "gui", "boot-b", 8 * NS, 12 * NS, True),
                HistorySegment("export", "iod", "boot-b", 12 * NS, 16 * NS, True),
            ),
            "boundaries": (("boot-b", 12 * NS, "gui", "iod"),
                           ("boot-a", 4 * NS, "gui", "iod")),
        }),
        ("gaps.list", {"gaps": [("boot-a", 8 * NS, 10 * NS)]}),
        ("gaps.length", {"gaps": (("boot-a", 8 * NS),)}),
        ("gaps.boot", {"gaps": (("", 8 * NS, 10 * NS),)}),
        ("gaps.reversed", {"gaps": (("boot-a", 10 * NS, 8 * NS),)}),
        ("gaps.empty", {"gaps": (("boot-a", 8 * NS, 8 * NS),)}),
        ("gaps.bool", {"gaps": (("boot-a", True, 10 * NS),)}),
        ("known_gaps.list", {"known_gaps": [("boot-a", 8 * NS, 10 * NS)]}),
        ("known_gaps.empty", {"known_gaps": (("boot-a", 8 * NS, 8 * NS),)}),
        ("overlaps.list", {"overlaps": [("boot-a", 8 * NS, 10 * NS)]}),
        ("overlaps.reversed", {"overlaps": (("boot-a", 10 * NS, 8 * NS),)}),
        ("flag.int", {"has_local_history": 1}),
        ("flag.str", {"not_retained": "no"}),
        ("flag.bool-as-int", {"chronology_unproved": 0}),
        ("flag.none", {"file_available": None}),
    ):
        base = {
            "worker_id": 2,
            "request": ranking_request,
            "ranking": (total(7, KEY),),
            "segments": frame_segments,
            "boundaries": frame_boundaries,
            "gaps": frame_gaps,
            "known_gaps": frame_known_gaps,
            "overlaps": (),
            "has_local_history": False,
            "not_retained": False,
            "chronology_unproved": False,
            "file_available": True,
        }
        _reject(
            f"local_frame.{label}",
            lambda kwargs=kwargs, base=base: LocalFrameOutcome(**{**base, **kwargs}),
        )
    forged_segment = HistorySegment("local", "gui", "boot-a", 0, 4 * NS, True)
    object.__setattr__(forged_segment, "end_ns", 0)
    _reject(
        "local_frame.forged-segment",
        lambda: LocalFrameOutcome(2, ranking_request, (total(7, KEY),), (forged_segment,),
                                  (), (), (), (), False, False, False, True),
    )
    # A boundary needs both adjacent *proven* segments. Keep the intended gui/iod
    # owners and vary only `proven`, so the proven-status requirement is the sole
    # reason these values become admissible when it is removed.
    for label, segments in (
        ("boundary-unproven-outgoing", (
            HistorySegment("local", "gui", "boot-a", 0, 4 * NS, False),
            HistorySegment("export", "iod", "boot-a", 4 * NS, 8 * NS, True))),
        ("boundary-unproven-incoming", (
            HistorySegment("local", "gui", "boot-a", 0, 4 * NS, True),
            HistorySegment("unproved", "iod", "boot-a", 4 * NS, 8 * NS, False))),
    ):
        _reject(
            f"local_frame.{label}",
            lambda segments=segments: LocalFrameOutcome(
                2, ranking_request, (total(7, KEY),), segments,
                (("boot-a", 4 * NS, "gui", "iod"),), (), (), (), False, False, False, True),
        )

    # LocalPartOutcome: one kind, one external subject, one canonical digest.
    totals_value = share(7, since_boot)
    part = LocalPartOutcome(
        3, detail_request, KEY, totals_value,
        local_answer_digest("totals", KEY, since_boot, totals_value),
    )
    assert part.value.window == since_boot
    _assert_frozen(part, "data_digest")
    series_value = series(7, since_boot)
    for label, kwargs in (
        ("worker_id.zero", {"worker_id": 0}),
        ("request.ranking", {"request": ranking_request}),
        ("request.files", {"request": files_request}),
        ("request.missing-subject", {"request": QueryRequest("totals", since_boot)}),
        ("key.mismatch", {"app_key": OTHER_KEY}),
        # Recompute the digest for the deliberately mismatched external key, so
        # the request association is the sole fault rather than a digest mismatch.
        ("key.mismatch-valid-digest", {
            "app_key": OTHER_KEY,
            "data_digest": local_answer_digest("totals", OTHER_KEY, since_boot, totals_value),
        }),
        ("key.invalid", {"app_key": "exe:relative/path"}),
        ("value.series", {"value": series_value}),
        ("value.window", {"value": share(7, range_w)}),
        ("digest.uppercase", {"data_digest": "A" * 64}),
        ("digest.length", {"data_digest": "a" * 63}),
        ("digest.nonhex", {"data_digest": "g" * 64}),
        ("digest.wrong", {"data_digest": "b" * 64}),
        ("digest.int", {"data_digest": 5}),
    ):
        base = {
            "worker_id": 3,
            "request": detail_request,
            "app_key": KEY,
            "value": totals_value,
            "data_digest": local_answer_digest("totals", KEY, since_boot, totals_value),
        }
        _reject(
            f"local_part.{label}",
            lambda kwargs=kwargs, base=base: LocalPartOutcome(**{**base, **kwargs}),
        )
    series_request = QueryRequest("series", since_boot, KEY)
    series_part = LocalPartOutcome(
        4, series_request, KEY, series_value,
        local_answer_digest("series", KEY, since_boot, series_value),
    )
    assert series_part.value.tier == "raw"
    _reject(
        "local_part.series-key-mismatch-valid-digest",
        lambda: LocalPartOutcome(
            4, series_request, OTHER_KEY, series_value,
            local_answer_digest("series", OTHER_KEY, since_boot, series_value)),
    )
    forged_value_coverage = coverage()
    object.__setattr__(forged_value_coverage.hook_hits, "vfs_read", -1)
    _reject(
        "local_part.forged-value",
        lambda: LocalPartOutcome(3, detail_request, KEY,
                                 replace(totals_value, coverage=forged_value_coverage),
                                 "a" * 64),
    )

    # LocalFilesOutcome: typed table/coverage/served evidence, first page only.
    overflow = UnknownOwnerOverflow(since_boot, "overflow_owner_unknown", 0, 0, 0, coverage())
    file_table = FileTable(7, since_boot, (), "bytes_total", "desc", None, overflow, coverage())
    file_coverage = FileCoverage(
        7, since_boot, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        HookHits(0, 0, 0, 0, 0, 0), 0, 0, overflow, coverage(),
    )
    files_outcome = LocalFilesOutcome(4, files_request, KEY, file_table, file_coverage, 0)
    assert files_outcome.coverage.served_interval == since_boot
    assert LocalFilesOutcome(4, files_request, KEY, file_table, file_coverage, None).served_gap_count is None
    _assert_frozen(files_outcome, "table")
    unavailable_overflow = UnknownOwnerOverflow(
        since_boot, "overflow_owner_unknown", 0, 0, 0, coverage(unavailable_reason="not_retained"))
    unavailable_table = FileTable(
        7, since_boot, (), "bytes_total", "desc", None, unavailable_overflow,
        coverage(unavailable_reason="not_retained"))
    unavailable_coverage = FileCoverage(
        7, since_boot, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        HookHits(0, 0, 0, 0, 0, 0), 0, 0, unavailable_overflow,
        coverage(unavailable_reason="not_retained"),
    )
    for label, kwargs in (
        ("worker_id.zero", {"worker_id": 0}),
        ("request.totals", {"request": detail_request}),
        ("request.continuation", {"request": QueryRequest("files", since_boot, KEY, cursor="opaque")}),
        ("key.mismatch", {"app_key": OTHER_KEY}),
        ("table.type", {"table": {"app_id": 7}}),
        ("table.window", {"table": replace(file_table, window=range_w)}),
        ("table.sort", {"table": replace(file_table, sort="basename")}),
        ("coverage.type", {"coverage": coverage()}),
        ("coverage.window", {"coverage": replace(file_coverage, window=range_w)}),
        ("coverage.app-id", {"coverage": replace(file_coverage, app_id=8)}),
        ("coverage.tier", {"coverage": replace(file_coverage, coverage=coverage(tier="rollup_60s"))}),
        ("coverage.gap", {"coverage": replace(file_coverage, coverage=coverage(gap_intervals=1))}),
        ("coverage.unavailable", {"table": unavailable_table, "coverage": unavailable_coverage}),
        ("served.negative", {"served_gap_count": -1}),
        ("served.bool", {"served_gap_count": True}),
    ):
        base = {
            "worker_id": 4,
            "request": files_request,
            "app_key": KEY,
            "table": file_table,
            "coverage": file_coverage,
            "served_gap_count": 0,
        }
        _reject(
            f"local_files.{label}",
            lambda kwargs=kwargs, base=base: LocalFilesOutcome(**{**base, **kwargs}),
        )
    # Served evidence must lie inside the exact request. The positive control is
    # an in-bounds subrange; the negative varies only its served end endpoint, so
    # removing the served-bounds check is the sole reason it becomes admissible.
    inside_served = Window("range", BOOT, 1_000_000_000, 15_000_000_000)
    inside_overflow = UnknownOwnerOverflow(
        inside_served, "overflow_owner_unknown", 0, 0, 0, coverage())
    inside_table = FileTable(
        7, since_boot, (), "bytes_total", "desc", None, inside_overflow, coverage())
    inside_coverage = FileCoverage(
        7, since_boot, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        HookHits(0, 0, 0, 0, 0, 0), 0, 0, inside_overflow, coverage(),
    )
    inside = LocalFilesOutcome(4, files_request, KEY, inside_table, inside_coverage, 0)
    assert inside.coverage.served_interval == inside_served

    wide_served = Window("range", BOOT, 1_000_000_000, 30_000_000_000)
    wide_overflow = UnknownOwnerOverflow(
        wide_served, "overflow_owner_unknown", 0, 0, 0, coverage())
    wide_table = FileTable(
        7, since_boot, (), "bytes_total", "desc", None, wide_overflow, coverage())
    wide_coverage = FileCoverage(
        7, since_boot, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        HookHits(0, 0, 0, 0, 0, 0), 0, 0, wide_overflow, coverage(),
    )
    assert wide_coverage.served_interval == wide_served
    _reject(
        "local_files.served-outside-request",
        lambda: LocalFilesOutcome(4, files_request, KEY, wide_table, wide_coverage, 0),
    )

    # Settlement inputs carry exactly one terminal result or error.
    finished = Future()
    finished.set_result({"kind": "totals"})
    remote_settlement = RemoteSettlement(finished, {"kind": "totals"})
    assert remote_settlement.response is not None
    failed_settlement = RemoteSettlement(finished, error=WindowUnavailableError(
        "not_retained", "remote history is not retained"))
    assert failed_settlement.response is None
    _assert_frozen(remote_settlement, "response")
    for label, kwargs in (
        ("future.type", {"future": {"done": True}}),
        ("both", {"response": {}, "error": RuntimeError("x")}),
        ("neither", {"response": None, "error": None}),
        ("response.list", {"response": []}),
        ("error.base", {"response": None, "error": KeyboardInterrupt()}),
    ):
        base = {"future": finished, "response": {"kind": "totals"}, "error": None}
        _reject(
            f"remote_settlement.{label}",
            lambda kwargs=kwargs, base=base: RemoteSettlement(**{**base, **kwargs}),
        )

    bracket = LocalBracket(local_vector, local_vector)
    frame_settlement = LocalSettlement(2, outcome=frame, local_bracket=bracket)
    assert frame_settlement.outcome is frame
    assert LocalSettlement(3, error=RuntimeError("boom")).outcome is None
    _assert_frozen(frame_settlement, "worker_id")
    for label, kwargs in (
        ("worker_id.zero", {"worker_id": 0}),
        ("worker_id.bool", {"worker_id": True}),
        ("both", {"outcome": frame, "error": RuntimeError("x")}),
        ("neither", {"outcome": None, "error": None}),
        ("outcome.type", {"outcome": {"worker_id": 2}}),
        ("outcome.worker", {"outcome": part}),
        ("error.base", {"outcome": None, "error": KeyboardInterrupt()}),
        ("bracket.type", {"local_bracket": [local_vector, local_vector]}),
    ):
        base = {"worker_id": 2, "outcome": frame, "error": None, "local_bracket": None}
        _reject(
            f"local_settlement.{label}",
            lambda kwargs=kwargs, base=base: LocalSettlement(**{**base, **kwargs}),
        )

    # -- the neutral module stays free of client/router/query/Qt ----------- #
    probe = (
        "import sys\n"
        "import appio.history_types\n"
        "banned = ('appio.iodclient', 'appio.history_router', 'appio.query',\n"
        "          'appio.iod_history', 'appio.store', 'appio.ipc_models',\n"
        "          'appio.identity', 'sqlite3', 'PyQt6')\n"
        "loaded = [name for name in banned if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


class _IdleClient:
    """A verified-not-answering stub: a local frame must issue no query."""

    state = ClientState()

    def query(self, request):  # pragma: no cover - a local frame never asks
        raise AssertionError(f"unexpected daemon query: {request.kind}")


def _committed_store(path, apps, *, owner="gui", start=100, end=110):
    """One committed synthetic store with a per-app delta across one tick pair."""

    store = AppIoStore(path)
    store.start_boot(BOOT, 1, 0, 1000.0, owner)
    for key, app_id, _amount in apps:
        store.connection.execute(
            "INSERT INTO app VALUES (?,?,?,NULL,0,0,'user')",
            (app_id, key, key.partition(":")[2]),
        )
    store.connection.commit()
    store.start_collector(owner, BOOT, start * NS)
    state = None
    for seq, at in ((0, start), (1, end)):
        processes = tuple(
            StableProcRecord(
                ProcessIdentity(BOOT, 410 + index, 100), 1, 1000, key, "/fixture",
                AppKeyInputs("/fixture", key.partition(":")[2]),
                IoReadOutcome("readable", (100 + seq * amount,) * 4, None, None),
            )
            for index, (key, _app_id, amount) in enumerate(apps)
        )
        observation = TickInput(BOOT, owner, 1, seq, at * NS, (1000 + at) * NS, processes, ())
        frame, state = baseline_seed(observation) if seq == 0 else aggregate_tick(state, observation)
        store.record_tick(frame, state)
    return store


def _registered_paths(*sources):
    return tuple(
        (name, store.connection.execute("PRAGMA database_list").fetchone()[2], owner)
        for name, store, owner in sources
    )


def test_local_detail_is_lazy_selected_subject_only(tmp_path, monkeypatch):
    """A frame copy proves routing only; selection builds one kind and subject.

    `_read_local` returns no part map and calls neither reducer sequence. A
    selected detail resolves the external AppKey inside each private snapshot,
    so store-local numeric IDs never come from the presentation registry. A real
    computation barrier on the background lane also proves the frame lane keeps
    executing while detail work is held inside its worker.
    """

    alpha_id, beta_id, export_alpha_id = 17, 18, 3
    local = _committed_store(
        tmp_path / "local.db", ((ALPHA, alpha_id, 20), (BETA, beta_id, 7)))
    root = None
    router = None
    release = threading.Event()
    try:
        paths = _registered_paths(("local", local, "gui"))
        scope = Window("range", BOOT, 100 * NS, 140 * NS)

        computations: list[tuple[str, str | None, Window]] = []
        local_ids: list[tuple[str, int]] = []
        originals = {"totals": QueryService.disk_share, "series": QueryService.series}

        def observe(kind, service, app_id, window):
            row = service.connection.execute(
                "SELECT key FROM app WHERE app_id=?", (app_id,)).fetchone()
            computations.append((kind, row[0] if row is not None else None, window))
            local_ids.append((kind, app_id))
            return originals[kind](service, app_id, window)

        monkeypatch.setattr(
            QueryService, "disk_share",
            lambda service, app_id, window: observe("totals", service, app_id, window))
        monkeypatch.setattr(
            QueryService, "series",
            lambda service, app_id, window: observe("series", service, app_id, window))

        frame = HistoryRouter._read_local(
            paths, scope, worker_id=1, request=QueryRequest("ranking", scope))
        assert isinstance(frame, LocalFrameOutcome)
        assert not hasattr(frame, "parts")
        assert computations == []
        assert {row.app_key for row in frame.ranking} == {ALPHA, BETA}
        assert all(row.app_id > 0 for row in frame.ranking)
        assert [segment.collector_id for segment in frame.segments] == ["local", "unproved"]
        assert frame.gaps == ((BOOT, 110 * NS, 140 * NS),)
        assert frame.segments[0].proven is True
        assert frame.segments[1].proven is False
        assert frame.known_gaps == ()
        assert frame.file_available is False

        part = HistoryRouter._read_local_part(
            paths, QueryRequest("totals", scope, ALPHA), worker_id=2, app_key=ALPHA)
        assert isinstance(part, LocalPartOutcome)
        assert part.worker_id == 2 and part.value.window == scope
        assert (part.value.task_read_bytes, part.value.task_write_bytes) == (20, 20)
        assert part.value.coverage.gap_intervals == 1
        assert not part.value.coverage.complete
        assert part.data_digest == local_answer_digest(
            "totals", part.app_key, scope, part.value)
        assert computations == [("totals", ALPHA, scope)]
        assert local_ids == [("totals", alpha_id)]  # the private store's own id

        series_part = HistoryRouter._read_local_part(
            paths, QueryRequest("series", scope, ALPHA), worker_id=3, app_key=ALPHA)
        assert isinstance(series_part, LocalPartOutcome)
        assert series_part.value.tier == "raw"
        assert sum(point.read_bytes for point in series_part.value.points) == 20
        assert [kind for kind, _key, _window in computations] == ["totals", "series"]
        assert all(key == ALPHA for _kind, key, _window in computations)

        # Each private snapshot resolves the same AppKey to its own numeric id:
        # the second store holds alpha as id 3, not the first store's 17.
        root = _committed_store(
            tmp_path / "root.db", ((ALPHA, export_alpha_id, 30),),
            owner="iod", start=110, end=120)
        merged_paths = paths + _registered_paths(("export", root, "iod"))
        computations.clear()
        local_ids.clear()
        merged = HistoryRouter._read_local_part(
            merged_paths, QueryRequest("totals", scope, ALPHA), worker_id=4, app_key=ALPHA)
        assert isinstance(merged, LocalPartOutcome)
        assert merged.value.task_read_bytes == 50
        assert merged.value.coverage.gap_intervals == 1
        assert local_ids == [("totals", alpha_id), ("totals", export_alpha_id)]
        assert computations == [("totals", ALPHA, scope), ("totals", ALPHA, scope)]

        # D03: a held detail computation must not serialize the frame lane.
        computations.clear()
        local_ids.clear()
        router = HistoryRouter(QueryService(local), _IdleClient(), AppRegistry())
        authority = ("local", (("local", "gui", 1),))
        vector = LocalPositionVector((LocalPosition("local", "gui", 1, 1, 0),))
        entered = threading.Event()
        token = RequestToken(
            11, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 1, None, authority, vector,
        )

        def held_detail():
            entered.set()
            # A longer bound than the frame wait: if both lanes shared one
            # worker this hold would outlive the frame assertion below.
            assert release.wait(15), "the detail worker was never released"
            return HistoryRouter._read_local_part(
                paths, QueryRequest("totals", scope, ALPHA),
                worker_id=token.token_id, app_key=ALPHA)

        assert router._frame_executor is not router._background_executor
        assert router._submit_background_worker(token, held_detail) is not None
        assert entered.wait(5)
        router.prepare(scope)
        outcome = router._local_future.result(timeout=10)
        assert isinstance(outcome, LocalFrameOutcome)
        assert release.is_set() is False, "the frame waited for the held detail"
        assert router.ready(scope)
        assert router.pending_work
        assert router._background_charge("detail") == 1

        # Two charged detail Futures is the hard lane bound: with both live, a
        # third admission must refuse rather than submit unbounded stale work.
        second = RequestToken(
            12, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 2, None, authority, vector,
        )

        def held_second():
            assert release.wait(15), "the second detail worker was never released"
            return HistoryRouter._read_local_part(
                paths, QueryRequest("totals", scope, ALPHA), worker_id=12, app_key=ALPHA)

        assert router._submit_background_worker(second, held_second) is not None
        third = RequestToken(
            13, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 3, None, authority, vector,
        )
        assert router._submit_background_worker(third, held_second) is None
        assert router._background_charge("detail") == 2

        release.set()
        settled = [router._completed.get(timeout=10) for _ in range(2)]
        assert {item[0].token_id for item in settled} == {token.token_id, second.token_id}
        for settled_token, settlement in settled:
            assert isinstance(settlement, LocalSettlement)
            assert settlement.worker_id == settled_token.token_id
            assert isinstance(settlement.outcome, LocalPartOutcome)
            assert settlement.outcome.value.window == scope
        # A done callback never releases charged capacity; only an owner drain does.
        assert router._background_charge("detail") == 2
        router._reap_background_worker(token.token_id)
        router._reap_background_worker(second.token_id)
        assert router._background_charge("detail") == 0

        # One authoritative background ledger: the legacy local Files path and
        # helper submissions reserve against the same Files/total charges. Eight
        # Files charges include the helper, so a ninth Files request and another
        # helper Files job must both refuse instead of running a second budget.
        files_release = threading.Event()
        files_started = threading.Event()
        original_files = router._read_local_files

        def held_files(*args, **kwargs):
            files_started.set()
            assert files_release.wait(15), "the Files worker was never released"
            return original_files(*args, **kwargs)

        monkeypatch.setattr(router, "_read_local_files", held_files)
        presentation = router.registry.id_for(ALPHA)
        files_futures = [
            router.request_file_view(presentation, scope, "bytes_total", "desc", None)
            for _ in range(4)
        ]
        assert files_started.wait(5)
        helper_files = RequestToken(
            21, QueryRequest("files", scope, ALPHA), "local", None, "files", 6, None,
            authority, vector,
        )

        def held_helper_files():
            assert files_release.wait(15), "the helper Files worker was never released"
            raise WindowUnavailableError("file_source_unavailable", "held Files worker")

        assert router._submit_background_worker(helper_files, held_helper_files) is not None
        assert router._background_charge("files") == 5
        for _ in range(3):
            files_futures.append(
                router.request_file_view(presentation, scope, "bytes_total", "desc", None))
        assert router._background_charge("files") == 8
        rejected = router.request_file_view(presentation, scope, "bytes_total", "desc", None)
        with pytest.raises(RequestRejected) as refused:
            rejected.result(timeout=5)
        assert refused.value.reason == "query_busy"
        # A fresh explicit helper id stays outside the implicit allocator's
        # current range; the refusal below must come from capacity, not a
        # colliding binding.
        extra_files = RequestToken(
            30, QueryRequest("files", scope, ALPHA), "local", None, "files", 7, None,
            authority, vector,
        )
        assert router._submit_background_worker(extra_files, held_helper_files) is None

        files_release.set()
        # Worker threads only enqueue immutable settlements; the outward Files
        # Futures publish solely on the owner drain. A terminal worker therefore
        # keeps its charge until that owner path reaps it.
        for _owner, job in list(router._worker_jobs.values()):
            if job is not None:
                job.result(timeout=15)
        assert router._background_charge("files") == 8
        assert all(not future.done() for future in files_futures)
        queued = []
        while not router._completed.empty():
            queued.append(router._completed.get_nowait())
        helper_token, helper_settlement = next(
            item for item in queued if item[0] is helper_files)
        assert isinstance(helper_settlement, LocalSettlement)
        assert isinstance(helper_settlement.error, WindowUnavailableError)
        for item in queued:
            router._completed.put(item)
        while not all(future.done() for future in files_futures):
            router.drain()
        for future in files_futures:
            future.exception(timeout=0)
        # The same ledger that admitted every worker releases it on the owner
        # drain; the ignored helper charge is reaped through that path too.
        assert router._background_charge("files") == 0
        assert not router._worker_jobs

        # Close retires publication, never resource accounting. A running worker
        # keeps its binding until it terminates and is reaped through this same
        # owner path; its late outcome is dropped instead of published.
        close_release = threading.Event()
        close_started = threading.Event()

        def held_close(worker_id):
            def work():
                close_started.set()
                assert close_release.wait(15), "the close worker was never released"
                return HistoryRouter._read_local_part(
                    paths, QueryRequest("totals", scope, ALPHA),
                    worker_id=worker_id, app_key=ALPHA)
            return work

        closing = RequestToken(
            31, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 9, None, authority, vector,
        )
        close_job = router._submit_background_worker(closing, held_close(closing.token_id))
        assert close_job is not None
        assert close_started.wait(5)
        router.close()
        assert close_job.running() is True
        assert closing.token_id in router._worker_jobs
        assert router._background_charge("detail") == 1
        assert router.pending_work
        close_release.set()
        assert close_job.result(timeout=15) is None
        assert router._background_charge("detail") == 1
        assert router.pending_work
        router._reap_background_worker(closing.token_id)
        assert router._background_charge("detail") == 0
        assert router._completed.empty()
        assert router.pending_work is False
    finally:
        release.set()
        if router is not None:
            router.close()
        if root is not None:
            root.connection.close()
        local.connection.close()


def test_worker_ids_are_never_reused_across_helper_and_files_admission(tmp_path, monkeypatch):
    """One collision-safe worker-id authority for helper and public Files work.

    The implicit Files allocator must never hand out an id that an admitted
    helper already owns. Each submitted Future then keeps exactly one charge,
    the shared ceiling refuses the eleventh submission instead of silently
    rebinding a running worker, and close() retains that worker's charge.
    """

    local = _committed_store(tmp_path / "local.db", ((ALPHA, 17, 20),))
    router = None
    release = threading.Event()
    entered = threading.Event()
    submitted: list[Future] = []
    try:
        paths = _registered_paths(("local", local, "gui"))
        scope = Window("range", BOOT, 100 * NS, 140 * NS)
        router = HistoryRouter(QueryService(local), _IdleClient(), AppRegistry())
        authority = ("local", (("local", "gui", 1),))
        vector = LocalPositionVector((LocalPosition("local", "gui", 1, 1, 0),))
        router.prepare(scope)
        assert isinstance(router._local_future.result(timeout=10), LocalFrameOutcome)
        assert router.ready(scope)
        assert router.pending_work is False

        actual_submit = router._background_executor.submit

        def counted_submit(work, /, *args, **kwargs):
            future = actual_submit(work, *args, **kwargs)
            submitted.append(future)
            return future

        monkeypatch.setattr(router._background_executor, "submit", counted_submit)

        # The helper takes exactly the id the implicit Files allocator would
        # otherwise return for the first legacy Files request.
        helper_id = router._next_token_id
        helper = RequestToken(
            helper_id, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 1, None, authority, vector,
        )

        def held_detail():
            entered.set()
            assert release.wait(15), "the helper worker was never released"
            return HistoryRouter._read_local_part(
                paths, QueryRequest("totals", scope, ALPHA),
                worker_id=helper_id, app_key=ALPHA)

        helper_job = router._submit_background_worker(helper, held_detail)
        assert helper_job is not None and entered.wait(5)
        assert router._worker_jobs[helper_id][0] == "detail"
        assert router._background_charge("detail") == 1

        presentation = router.registry.id_for(ALPHA)
        router.request_file_view(presentation, scope, "bytes_total", "desc", None)

        def held_helper_worker():
            assert False, "a queued helper worker must never run in this control"

        for worker_id in range(102, 109):
            helper_files = RequestToken(
                worker_id, QueryRequest("files", scope, ALPHA), "local", None,
                "files", worker_id, None, authority, vector,
            )
            assert router._submit_background_worker(
                helper_files, held_helper_worker) is not None

        second = RequestToken(
            100, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 2, None, authority, vector,
        )
        assert router._submit_background_worker(second, held_detail) is not None

        # The eleventh submission attempt must meet the ten-job ceiling. If an
        # implicit id overwrote the first helper's binding, this token is
        # admitted against a ledger that has already lost one real charge.
        third = RequestToken(
            101, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 3, None, authority, vector,
        )
        eleventh = router._submit_background_worker(third, held_detail)

        # Every actual executor submission owns one distinct live charge.
        assert len(submitted) == len(router._worker_jobs) == 10, (
            f"{len(submitted)} submitted Futures against "
            f"{len(router._worker_jobs)} recorded charges")
        assert eleventh is None
        assert router._worker_jobs[helper_id][0] == "detail"
        assert router._background_charge("detail") == 2
        assert router._background_charge("files") == 8

        # Close retires publication, never a running worker's resource. With an
        # overwritten binding this ledger emptied while the worker still ran.
        router.close()
        assert helper_job.running() is True
        assert router._worker_jobs[helper_id][0] == "detail"
        assert len(router._worker_jobs) == 1
        assert router._background_charge("detail") == 1
        assert router.pending_work
        release.set()
        assert helper_job.result(timeout=15) is None
        assert router._background_charge("detail") == 1
        assert router.pending_work
        router._reap_background_worker(helper_id)
        assert router._background_charge("detail") == 0
        assert router._completed.empty()
        assert router.pending_work is False
    finally:
        release.set()
        if router is not None:
            router.close()
        local.connection.close()


# ---------------------------------------------------------------------------
# Local positions and copy brackets (§§2.2, 4.1–4.3)
# ---------------------------------------------------------------------------


class _StateClient:
    """A client whose only observable fact is the state under test."""

    def __init__(self, state):
        self.state = state


def _db_path(store) -> str:
    return store.connection.execute("PRAGMA database_list").fetchone()[2]


def _source_stores(tmp_path):
    """Two file-backed stores with their own connections and separate writers."""

    local = _committed_store(tmp_path / "local.db", ((ALPHA, 17, 20), (BETA, 18, 7)))
    imported = _committed_store(
        tmp_path / "imported.db", ((ALPHA, 3, 30),), owner="iod", start=110, end=120)
    return local, imported


def _source_router(tmp_path):
    local, imported = _source_stores(tmp_path)
    history = HistoryRouter(QueryService(local), _IdleClient(), AppRegistry())
    history.add_source(QueryService(imported), collector_id="imported", owner="iod")
    return history, local, imported


def test_local_vector_observes_each_registered_connection_without_hashing(
        tmp_path, monkeypatch):
    """One constant committed observation per source, ordered, never a scan.

    The position is exactly ``(name, owner, registration_epoch, data_version,
    total_changes)`` read on each registered connection: one PRAGMA plus a
    property, no observer connection, no table/rowid read, and no
    ``QueryService._snapshot`` around the boundary.
    """

    history, local, imported = _source_router(tmp_path)
    try:
        vector = history.source_position()
        assert isinstance(vector, LocalPositionVector)
        assert tuple(position.name for position in vector.sources) == ("imported", "local")
        for position, store, owner in (
                (vector.sources[0], imported, "iod"),
                (vector.sources[1], local, "gui")):
            connection = store.connection
            assert position.owner == owner
            assert position.registration_epoch >= 1
            assert position.data_version == connection.execute(
                "PRAGMA main.data_version").fetchone()[0]
            assert position.total_changes == connection.total_changes
        assert (vector.sources[0].registration_epoch
                != vector.sources[1].registration_epoch)

        traced = {"imported": [], "local": []}
        imported.connection.set_trace_callback(traced["imported"].append)
        local.connection.set_trace_callback(traced["local"].append)
        for _ in range(2):
            observed = history.source_position()
        assert tuple(position.name for position in observed.sources) == ("imported", "local")
        for name, statements in traced.items():
            assert statements == ["PRAGMA main.data_version"] * 2, (name, statements)
            for statement in statements:
                upper = statement.upper()
                assert "ROWID" not in upper
                assert "FROM" not in upper
                assert "SELECT" not in upper
        # One call costs exactly one constant observation per registered source.
        for statements in traced.values():
            statements.clear()
        history.source_position()
        assert all(len(statements) == 1 for statements in traced.values()), traced

        # No observer connection and no QueryService read snapshot is opened.
        def forbidden(*_args, **_kwargs):
            raise AssertionError("source observation opened another connection or snapshot")

        with monkeypatch.context() as guard:
            guard.setattr("appio.history_router.sqlite3.connect", forbidden)
            guard.setattr(QueryService, "_snapshot", forbidden)
            history.source_position()
            history.local_position("local")

        # Only a verified, answering connection that carries a cursor answers a
        # remote position; every lesser state keeps the complete local vector.
        history.client = _StateClient(ClientState(
            verified=True, answering=True, cursor=StreamCursor(BOOT, 3, 9)))
        assert history.source_position() == RemotePosition(BOOT, 3, 9)
        history.client = _StateClient(ClientState(
            verified=True, answering=False, cursor=StreamCursor(BOOT, 3, 9)))
        assert isinstance(history.source_position(), LocalPositionVector)
        # Answering is not enough without verification: the conjunction is the
        # remote authority, and an unverified cursor stays local evidence.
        history.client = _StateClient(ClientState(
            verified=False, answering=True, cursor=StreamCursor(BOOT, 3, 9)))
        assert isinstance(history.source_position(), LocalPositionVector)
        history.client = _StateClient(ClientState(
            verified=True, answering=True, cursor=None))
        assert isinstance(history.source_position(), LocalPositionVector)
    finally:
        history.close()
        local.connection.close()
        imported.connection.close()


def test_local_vector_changes_for_every_existing_commit_path(tmp_path):
    """Every committed store path moves its own source's token, never another's.

    Own-connection commit paths advance ``total_changes``; external commits
    advance ``data_version``. Deletions of older rows (including WITHOUT ROWID
    rollups) and in-place updates count even while row maxima repeat.
    """

    history, local, imported = _source_router(tmp_path)
    external_local = sqlite3.connect(_db_path(local))
    external_imported = sqlite3.connect(_db_path(imported))
    stores = {"local": local, "imported": imported}
    try:
        def maxima(connection):
            return (
                connection.execute(
                    "SELECT coalesce(max(observed_mono_ns), -1) FROM sample").fetchone()[0],
                connection.execute(
                    "SELECT coalesce(max(ts_mono_ns), -1) FROM sample").fetchone()[0],
                connection.execute(
                    "SELECT coalesce(max(bucket_start_mono_ns), -1) FROM rollup_app").fetchone()[0],
            )

        def changed(name, operation, *, external):
            before = {key: history.local_position(key) for key in stores}
            row_maxima = maxima(stores[name].connection)
            operation()
            after = {key: history.local_position(key) for key in stores}
            position = after[name]
            observer = stores[name].connection
            assert position != before[name]
            assert position.registration_epoch == before[name].registration_epoch
            assert position.data_version == observer.execute(
                "PRAGMA main.data_version").fetchone()[0]
            assert position.total_changes == observer.total_changes
            if external:
                assert position.data_version > before[name].data_version
            else:
                assert position.total_changes > before[name].total_changes
            # A deleted old row, a deleted rollup or an in-place update does not
            # move a row maximum; the token still does.
            assert maxima(observer) == row_maxima
            for other, other_position in after.items():
                if other != name:
                    assert other_position == before[other]
            return position

        # 1. Own-connection deletion of the oldest sample row.
        row = local.connection.execute(
            "SELECT boot_id, source, epoch, seq, app_id, launch_id, ts_mono_ns "
            "FROM sample ORDER BY observed_mono_ns, ts_mono_ns LIMIT 1").fetchone()
        assert row is not None

        def delete_old_sample():
            local.connection.execute(
                "DELETE FROM sample WHERE boot_id=? AND source=? AND epoch=? AND seq=? "
                "AND app_id=? AND launch_id=? AND ts_mono_ns=?", row)
            local.connection.commit()

        changed("local", delete_old_sample, external=False)

        # 2. External deletion of an older WITHOUT ROWID rollup row.
        for bucket in (100 * NS, 200 * NS):
            external_imported.execute(
                "INSERT INTO rollup_app(boot_id, app_id, bucket_start_mono_ns, bucket_secs, "
                "read_bytes, write_bytes, rchar, wchar, coverage_json) "
                "VALUES (?, ?, ?, 3600, 0, 0, 0, 0, '{}')", (BOOT, 3, bucket))
        external_imported.commit()

        def delete_old_rollup():
            external_imported.execute(
                "DELETE FROM rollup_app WHERE boot_id=? AND app_id=? AND bucket_start_mono_ns=?",
                (BOOT, 3, 100 * NS))
            external_imported.commit()

        changed("imported", delete_old_rollup, external=True)

        # 3. Own-connection in-place collector_epoch update.
        def bump_collector_epoch():
            cursor = local.connection.execute(
                "UPDATE collector_epoch SET epoch=epoch+1 WHERE source='gui'")
            assert cursor.rowcount == 1
            local.connection.commit()

        changed("local", bump_collector_epoch, external=False)

        # 4. External receipt update of the collector interval.
        def extend_receipt():
            cursor = external_imported.execute(
                "UPDATE collector_interval SET to_mono_ns=to_mono_ns+1 WHERE source='iod'")
            assert cursor.rowcount == 1
            external_imported.commit()

        changed("imported", extend_receipt, external=True)

        # 5. Own-connection handover update.
        external_local.execute(
            "INSERT INTO handover(boot_id, from_source, to_source, cutoff_mono_ns, "
            "baselines_json) VALUES (?, 'gui', 'iod', ?, '{}')", (BOOT, 105 * NS))
        external_local.commit()

        def update_handover():
            cursor = local.connection.execute(
                "UPDATE handover SET baselines_json='{\"marker\": 1}' "
                "WHERE boot_id=? AND cutoff_mono_ns=?", (BOOT, 105 * NS))
            assert cursor.rowcount == 1
            local.connection.commit()

        changed("local", update_handover, external=False)

        # 6. A non-primary-only external write changes only its own source.
        def bump_imported_epoch():
            cursor = external_imported.execute(
                "UPDATE collector_epoch SET epoch=epoch+1 WHERE source='iod'")
            assert cursor.rowcount == 1
            external_imported.commit()

        position = changed("imported", bump_imported_epoch, external=True)
        assert position.data_version > 1
        assert len({history.local_position(key).registration_epoch
                    for key in stores}) == 2
    finally:
        history.close()
        external_local.close()
        external_imported.close()
        local.connection.close()
        imported.connection.close()


def test_local_registration_add_remove_replace_and_reopen_never_repeat_token(tmp_path):
    """Every registration mutation consumes a fresh epoch, counters or not.

    Replacement under the same name/owner can repeat both raw counters, so the
    epoch is the identity. An unlinked connection whose path now denotes another
    file refuses observation until an explicit rebinding replaces it, and a
    connection the router has never registered is only accepted once its own
    database image proves it reads the file now at that path.
    """

    local, imported = _source_stores(tmp_path)
    imported_path = _db_path(imported)
    history = HistoryRouter(QueryService(local), _IdleClient(), AppRegistry())
    imported_service = QueryService(imported)
    extra = []
    try:
        epochs = []
        first = history.local_position("local")
        assert first.registration_epoch == 1
        with pytest.raises(ValueError):
            history.remove_source("local")

        history.add_source(imported_service, collector_id="imported", owner="iod")
        added = history.local_position("imported")
        epochs.append(added.registration_epoch)
        assert tuple(position.name for position in history.source_position().sources) == (
            "imported", "local")

        # Removal changes the vector immediately; re-adding never reuses the
        # retired epoch.
        history.remove_source("imported")
        assert tuple(position.name for position in history.source_position().sources) == (
            "local",)
        with pytest.raises(KeyError):
            history.local_position("imported")
        history.add_source(imported_service, collector_id="imported", owner="iod")
        readded = history.local_position("imported")
        epochs.append(readded.registration_epoch)
        assert readded.registration_epoch != added.registration_epoch

        # Same-name/owner replacement: two fresh connections to the same file
        # report identical raw counters, so only the epoch can distinguish them.
        fresh_a = sqlite3.connect(imported_path)
        extra.append(fresh_a)
        history.add_source(QueryService(fresh_a), collector_id="imported", owner="iod")
        replaced = history.local_position("imported")
        epochs.append(replaced.registration_epoch)
        fresh_b = sqlite3.connect(imported_path)
        extra.append(fresh_b)
        history.add_source(QueryService(fresh_b), collector_id="imported", owner="iod")
        replaced_again = history.local_position("imported")
        epochs.append(replaced_again.registration_epoch)
        assert (replaced.data_version, replaced.total_changes) == (
            replaced_again.data_version, replaced_again.total_changes) == (1, 0)
        assert replaced.registration_epoch != replaced_again.registration_epoch
        assert replaced != replaced_again

        # Reopening the registered service's connection is a registration
        # replacement even without add_source.
        reopened_connection = sqlite3.connect(imported_path)
        extra.append(reopened_connection)
        fresh_b_service = history._sources["imported"][0]
        fresh_b_service.connection = reopened_connection
        reopened = history.local_position("imported")
        epochs.append(reopened.registration_epoch)
        assert reopened.registration_epoch != replaced_again.registration_epoch
        assert (reopened.data_version, reopened.total_changes) == (1, 0)

        # Re-binding the same name/owner to another physical database.
        third = _committed_store(tmp_path / "third.db", ((ALPHA, 5, 40),))
        third_connection = sqlite3.connect(_db_path(third))
        extra.append(third_connection)
        history.add_source(
            QueryService(third_connection), collector_id="imported", owner="iod")
        rebound = history.local_position("imported")
        epochs.append(rebound.registration_epoch)
        assert rebound.registration_epoch > reopened.registration_epoch

        # A path replacement under an unchanged connection: the file at the
        # registered path is now another inode while the observer still holds
        # the old (unlinked) one. Stat alone cannot certify that connection, so
        # observation and preparation refuse until an explicit rebinding.
        replacement = _committed_store(
            tmp_path / "replacement.db", ((ALPHA, 6, 50),), owner="iod", start=110, end=120)
        try:
            third_path = _db_path(third)
            row_total = ("SELECT coalesce(sum(read_bytes), 0) "
                         "FROM app_lifetime_total")
            # A connection opened on the original file before the replacement
            # keeps reading the unlinked original forever. The router has never
            # seen it, so its first registration is where the epoch is chosen.
            preopened = sqlite3.connect(third_path)
            extra.append(preopened)
            assert preopened.execute(row_total).fetchone()[0] == 40
            assert id(preopened) not in history._connection_bindings
            prior_epoch = history._registrations["imported"].registration_epoch
            prior_physical = history._registrations["imported"].physical
            os.replace(tmp_path / "replacement.db", third_path)
            assert preopened.execute(row_total).fetchone()[0] == 40
            assert history.local_position("local").name == "local"
            with pytest.raises(ValueError, match="history source"):
                history.local_position("imported")
            with pytest.raises(ValueError, match="history source"):
                history.source_position()
            with pytest.raises(ValueError, match="history source"):
                history.prepare(Window("range", BOOT, 100 * NS, 140 * NS))
            assert history._local_future is None
            assert history._registrations["imported"].registration_epoch == prior_epoch
            assert history._registrations["imported"].physical == prior_physical

            # A reinstall cannot re-stamp the original connection with the
            # replacement's identity. Both an explicit add_source with the
            # registered service and a fresh QueryService around its existing
            # connection still read the unlinked original, so accepting either
            # would record the replacement inode and let a later equal bracket
            # certify the old observer against a copy of the other file.
            stale_service, stale_owner = history._sources["imported"]
            observer_total = third_connection.execute(row_total).fetchone()[0]
            copied = sqlite3.connect(third_path)
            try:
                copied_total = copied.execute(row_total).fetchone()[0]
            finally:
                copied.close()
            assert (observer_total, copied_total) == (40, 50)
            for proposal in (stale_service, QueryService(stale_service.connection)):
                with pytest.raises(ValueError, match="history source"):
                    history.add_source(
                        proposal, collector_id="imported", owner=stale_owner)
            assert history._sources["imported"][0] is stale_service
            assert history._registrations["imported"].registration_epoch == prior_epoch
            assert history._registrations["imported"].physical == prior_physical
            with pytest.raises(ValueError, match="history source"):
                history.local_position("imported")

            # A connection the router has never registered is no proof either:
            # opened before the replacement it still reads the unlinked original,
            # and first registration would stamp it with the replacement inode.
            # Both the explicit add_source and the automatic service-replacement
            # route must refuse it instead of allocating an epoch for it.
            with pytest.raises(ValueError, match="history source"):
                history.add_source(
                    QueryService(preopened), collector_id="imported", owner="iod")
            history._sources["imported"] = (QueryService(preopened), stale_owner)
            try:
                with pytest.raises(ValueError, match="history source"):
                    history.local_position("imported")
            finally:
                history._sources["imported"] = (stale_service, stale_owner)
            assert id(preopened) not in history._connection_bindings
            assert history._sources["imported"][0] is stale_service
            assert history._registrations["imported"].registration_epoch == prior_epoch
            assert history._registrations["imported"].physical == prior_physical

            # Rebinding to a connection opened on the replacement file restores
            # observation with a fresh, never-reused epoch.
            recovered_connection = sqlite3.connect(third_path)
            extra.append(recovered_connection)
            history.add_source(
                QueryService(recovered_connection), collector_id="imported", owner="iod")
            recovered = history.local_position("imported")
            epochs.append(recovered.registration_epoch)
            assert recovered.registration_epoch > prior_epoch

            # The same proven rebinding through the automatic route, and the
            # certified bracket that follows it: the copied replacement file and
            # the registered observer carry the same ALPHA total.
            automatic_connection = sqlite3.connect(third_path)
            extra.append(automatic_connection)
            history._sources["imported"] = (QueryService(automatic_connection), "iod")
            automatic = history.local_position("imported")
            epochs.append(automatic.registration_epoch)
            assert automatic.registration_epoch > recovered.registration_epoch
            assert (automatic.data_version, automatic.total_changes) == (1, 0)
            scope = Window("range", BOOT, 100 * NS, 140 * NS)
            history.prepare(scope)
            outcome = history._local_future.result(timeout=10)
            assert history.ready(scope)
            bracket = history._local_settlement.local_bracket
            assert bracket is not None
            assert bracket.opening == bracket.closing
            imported_alpha = automatic_connection.execute(row_total).fetchone()[0]
            local_alpha = local.connection.execute(
                row_total + " WHERE app_id=17").fetchone()[0]
            assert imported_alpha == 50
            assert next(row.read_bytes for row in outcome.ranking
                        if row.app_key == ALPHA) == local_alpha + imported_alpha
        finally:
            replacement.connection.close()

        assert len(epochs) == len(set(epochs))
        assert all(later > earlier for earlier, later in zip(epochs, epochs[1:]))

        # A retired source keeps its path bound. An unseen connection opened
        # before that path's file was replaced is refused even under a new name,
        # a refusal spends no epoch, and a connection opened afterwards is
        # proven and accepted.
        other = _committed_store(tmp_path / "other.db", ((ALPHA, 7, 60),))
        other_path = _db_path(other)
        unseen_stale = sqlite3.connect(other_path)
        extra.append(unseen_stale)
        assert unseen_stale.execute(row_total).fetchone()[0] == 60
        history.add_source(
            QueryService(other.connection), collector_id="retired", owner="iod")
        other.connection.close()
        history.remove_source("retired")
        stale_other = _committed_store(tmp_path / "stale.db", ((ALPHA, 8, 70),))
        stale_other.connection.close()
        os.replace(tmp_path / "stale.db", other_path)
        assert unseen_stale.execute(row_total).fetchone()[0] == 60
        next_epoch = history._next_registration_epoch
        with pytest.raises(ValueError, match="history source"):
            history.add_source(
                QueryService(unseen_stale), collector_id="retired2", owner="iod")
        assert history._next_registration_epoch == next_epoch
        assert id(unseen_stale) not in history._connection_bindings
        fresh_other = sqlite3.connect(other_path)
        extra.append(fresh_other)
        history.add_source(
            QueryService(fresh_other), collector_id="retired2", owner="iod")
        assert history.local_position("retired2").registration_epoch == next_epoch
        assert history._next_registration_epoch == next_epoch + 1

        # A source whose journal keeps committed frames outside its main file is
        # not byte-identical to that file, so the database-image comparison keeps
        # a correctly bound rebinding working instead of refusing it.
        wal = _committed_store(tmp_path / "wal.db", ((ALPHA, 9, 80),))
        wal_path = _db_path(wal)
        wal.connection.execute("PRAGMA journal_mode=WAL")
        wal.connection.execute("PRAGMA wal_autocheckpoint=0")
        wal.connection.execute(
            "UPDATE app_lifetime_total SET read_bytes = read_bytes + 1")
        wal.connection.commit()
        with open(wal_path, "rb") as handle:
            wal_bytes = handle.read()
        assert wal.connection.serialize(name="main") != wal_bytes
        history.add_source(QueryService(wal.connection), collector_id="wal", owner="iod")
        wal_rebound = sqlite3.connect(wal_path)
        extra.append(wal_rebound)
        history.add_source(QueryService(wal_rebound), collector_id="wal", owner="iod")
        assert history.local_position("wal").name == "wal"
        wal.connection.close()
    finally:
        history.close()
        for connection in extra:
            connection.close()
        local.connection.close()
        imported.connection.close()


def test_local_observations_require_committed_registered_connections(tmp_path, monkeypatch):
    """Both observers refuse an active transaction; invalid bindings are errors.

    A pinned caller transaction neither hides an external commit nor gets
    committed/rolled back by the router. Closing observation failures reach the
    frame error seam, a registration change after the opening capture retires
    the copy, and a missing/unbound/closed observer is never a sentinel.
    """

    history, local, imported = _source_router(tmp_path)
    external_local = sqlite3.connect(_db_path(local))
    imported_path = _db_path(imported)
    scope = Window("range", BOOT, 100 * NS, 140 * NS)
    try:
        traced = []
        before_dv = local.connection.execute("PRAGMA main.data_version").fetchone()[0]
        before_total = local.connection.total_changes
        local.connection.execute("BEGIN")
        local.connection.set_trace_callback(traced.append)
        for call in (lambda: history.local_position("local"),
                     history.source_position,
                     lambda: history.prepare(scope)):
            with pytest.raises(
                    ValueError, match="history source has an uncommitted transaction"):
                call()
            assert local.connection.in_transaction
            assert history._local_future is None
            assert history._local_worker_id is None
        # The opening observer refuses before it executes any SQL.
        assert traced == []

        # A pinned caller transaction cannot hide an external commit: the
        # observation refuses instead of certifying the pinned snapshot.
        external_local.execute(
            "UPDATE collector_epoch SET epoch=epoch+1 WHERE source='gui'")
        external_local.commit()
        with pytest.raises(
                ValueError, match="history source has an uncommitted transaction"):
            history.local_position("local")
        assert local.connection.in_transaction
        # The router neither commits nor rolls back the caller's transaction.
        assert local.connection.total_changes == before_total
        local.connection.rollback()
        assert not local.connection.in_transaction
        committed = history.local_position("local")
        assert committed.data_version == local.connection.execute(
            "PRAGMA main.data_version").fetchone()[0]
        assert committed.data_version > before_dv
        assert committed.total_changes == before_total

        # The closing observer rejects an active transaction before publication
        # and leaves the frame unconsumed for a later committed retry.
        history.prepare(scope)
        assert isinstance(history._local_future.result(timeout=15), LocalFrameOutcome)
        local.connection.execute("BEGIN")
        with pytest.raises(
                ValueError, match="history source has an uncommitted transaction"):
            history.ready(scope)
        assert local.connection.in_transaction
        assert history.view_state == "unavailable: local history evidence"
        local.connection.rollback()
        assert history.ready(scope) is True
        settlement = history._local_settlement
        assert isinstance(settlement, LocalSettlement)
        assert settlement.local_bracket is not None
        assert settlement.local_bracket.opening == settlement.local_bracket.closing

        # An equal local bracket certifies only that local vector: it never
        # promotes a remote cursor the connection has not verified or cannot
        # answer from. Only the full conjunction is a remote authority.
        cursor = StreamCursor(BOOT, 3, 9)
        for state in (
                ClientState(verified=False, answering=True, cursor=cursor),
                ClientState(verified=True, answering=False, cursor=cursor)):
            history.client = _StateClient(state)
            assert history.source_position() == settlement.local_bracket.closing
        history.client = _StateClient(ClientState(
            verified=True, answering=True, cursor=cursor))
        assert history.source_position() == RemotePosition(BOOT, 3, 9)
        history.client = _IdleClient()

        # A commit after the copy finished but before the closing read is the
        # conservative boundary: the closing token moves while the opening one
        # does not, so the frame is uncertified and no opening counter may be
        # reused as its closing evidence.
        history.prepare(scope)
        copied = history._local_future.result(timeout=15)
        assert isinstance(copied, LocalFrameOutcome)
        assert copied.worker_id == history._local_worker_id
        external_local.execute(
            "UPDATE collector_epoch SET epoch=epoch+1 WHERE source='gui'")
        external_local.commit()
        assert history.ready(scope) is True
        moved = history._local_settlement
        assert moved is not settlement
        moved_bracket = moved.local_bracket
        assert moved_bracket is not None
        assert moved_bracket.opening != moved_bracket.closing
        opening_local = next(
            position for position in moved_bracket.opening.sources
            if position.name == "local")
        closing_local = next(
            position for position in moved_bracket.closing.sources
            if position.name == "local")
        assert closing_local.data_version > opening_local.data_version
        assert closing_local.total_changes == opening_local.total_changes
        assert closing_local == history.local_position("local")
        assert moved_bracket.closing == history.source_position()

        # A commit that lands after the real closing observation never rewrites
        # the just-read boundary: this frame keeps the bracket it observed, and
        # the next current-vector read is what detects the change.
        history.prepare(scope)
        copied = history._local_future.result(timeout=15)
        assert isinstance(copied, LocalFrameOutcome)
        closed_before = history.source_position()
        original_observe = HistoryRouter._observe_registration
        original_closing = history._closing_vector
        phase = {"closing": False}

        def closing(binding):
            phase["closing"] = True
            try:
                return original_closing(binding)
            finally:
                phase["closing"] = False

        def observe_then_commit(registration):
            position = original_observe(registration)
            if phase["closing"] and registration.name == "local":
                # Commit after the closing observation of the last source, but
                # before the bracket leaves the closing read.
                phase["closing"] = False
                external_local.execute(
                    "UPDATE collector_epoch SET epoch=epoch+1 WHERE source='gui'")
                external_local.commit()
            return position

        monkeypatch.setattr(history, "_closing_vector", closing)
        monkeypatch.setattr(
            HistoryRouter, "_observe_registration", staticmethod(observe_then_commit))
        assert history.ready(scope) is True
        closed = history._local_settlement.local_bracket
        assert closed is not None
        assert closed.opening == closed.closing == closed_before
        # The commit was real: the next current-vector read moves.
        assert history.source_position() != closed.closing
        monkeypatch.undo()

        # A registration identity change after the opening capture retires the
        # copy: replacing the service object, its connection or its owner all
        # change the registration set the closing vector was bound to.
        for index, transition in enumerate(
                ("service_only", "same_counter_connection", "owner")):
            binding = history._capture_local_binding(
                worker_id=810 + index, request=QueryRequest("ranking", scope))
            service, owner = history._sources["imported"]
            extra_connection = None
            try:
                if transition == "service_only":
                    history._sources["imported"] = (
                        QueryService(service.connection), owner)
                elif transition == "same_counter_connection":
                    extra_connection = sqlite3.connect(imported_path)
                    history._sources["imported"] = (
                        QueryService(extra_connection), owner)
                else:
                    history._sources["imported"] = (service, "gui")
                with pytest.raises(ValueError, match="registrations changed"):
                    history._closing_vector(binding)
            finally:
                history._sources["imported"] = (service, owner)
                if extra_connection is not None:
                    extra_connection.close()

        # Adding a source changes the registered set itself, even though every
        # captured registration still matches its own name.
        binding = history._capture_local_binding(
            worker_id=820, request=QueryRequest("ranking", scope))
        added = QueryService(sqlite3.connect(imported_path))
        try:
            history.add_source(added, collector_id="another", owner="iod")
            with pytest.raises(ValueError, match="registrations changed"):
                history._closing_vector(binding)
        finally:
            history.remove_source("another")
            added.connection.close()

        # Missing, closed and unbound observers are operational errors.
        with pytest.raises(KeyError):
            history.local_position("missing")
        imported.connection.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            history.local_position("imported")
        os.unlink(imported_path)
        with pytest.raises(ValueError, match="history source"):
            history.local_position("imported")
        with pytest.raises(ValueError, match="history source"):
            history.source_position()
    finally:
        history.close()
        external_local.close()
        local.connection.close()
        imported.connection.close()


def test_local_frame_rejects_a_foreign_worker_binding(tmp_path):
    """A frame result publishes only under its exact admitted worker binding.

    The copy worker receives the admitted worker id and request with its paths,
    so its outcome already carries them; consumption never relabels a foreign
    result. A mismatched worker identity and a mismatched request each fail
    before the owner publishes any routing fact or closing bracket for that
    result, and a later clean frame still publishes normally (§3.4, §4.3).
    """

    history, local, imported = _source_router(tmp_path)
    scope = Window("range", BOOT, 100 * NS, 140 * NS)
    try:
        for corrupt, expected in (("worker", "worker"), ("request", "request")):
            history.prepare(scope)
            admitted = history._local_worker_id
            outcome = history._local_future.result(timeout=15)
            assert isinstance(outcome, LocalFrameOutcome)
            # The submitted worker answers under its own admission; a matching
            # default id would only be a consumer rewrite of a foreign result.
            assert outcome.worker_id == admitted
            assert outcome.request == QueryRequest("ranking", scope)
            forged = replace(
                outcome,
                worker_id=admitted + 900 if corrupt == "worker" else admitted,
                request=(outcome.request if corrupt == "worker"
                         else QueryRequest("ranking", Window("lifetime"))),
            )
            future = Future()
            future.set_result(forged)
            history._local_future = future
            with pytest.raises(ValueError, match=expected):
                history.ready(scope)
            assert history._ranking == []
            assert history.view_state == "unavailable: local history evidence"
            assert isinstance(history._local_settlement.error, ValueError)

        # The rejection does not strand the lane: the next frame publishes under
        # its own admitted binding.
        history.prepare(scope)
        admitted = history._local_worker_id
        assert isinstance(history._local_future.result(timeout=15), LocalFrameOutcome)
        assert history.ready(scope) is True
        assert history._local_settlement.outcome.worker_id == admitted
        assert history.local_position("local").name == "local"
    finally:
        history.close()
        local.connection.close()
        imported.connection.close()


# ---------------------------------------------------------------------------
# Authority retirement, scope-owned detail and the sole settlement writer
# (§§2.4, 3.1–3.4, 5.1, 12.2)
# ---------------------------------------------------------------------------


class _BoundClient:
    """A verified answering client whose Futures carry real transport states.

    Every Future is bound to its normalized request before it can settle, as
    the production client's observation seam does, so a settlement arm fails
    only for the single fault a test names.
    """

    def __init__(self, *, boot=BOOT, epoch=3, seq=7, clock=None):
        self.clock = Clock(50) if clock is None else clock
        self.cursor = StreamCursor(boot, epoch, seq)
        self.state = ClientState(
            verified=True, answering=True, cursor=self.cursor,
            capabilities=Capabilities(proc_all=True, dev_stat=True,
                                      file_io=True, bpf_loaded=True))
        self._generation = 0
        self._serial = 0
        self.requests = []
        self.rejected = []
        self.retired = []
        self._records = {}

    @property
    def generation(self):
        return self._generation

    def source(self):
        return RemotePosition(self.cursor.boot_id, self.cursor.iod_epoch, self.cursor.seq)

    def query(self, request):
        normalized = _normalize_request(request)
        future = Future()
        self._serial += 1
        self._records[future] = {
            "request": normalized,
            "connection_generation": self._generation,
            "request_id": f"q{self._generation}-{self._serial}",
            "phase": "active",
            "issued_at_ns": self.clock(),
            "issued_source": self.source(),
            "answered_source": None,
        }
        self.requests.append((normalized, future))
        return future

    def query_transport(self, future):
        try:
            record = self._records[future]
        except KeyError:
            raise ValueError("this Future is not bound to a client query") from None
        return QueryTransportState(**record)

    def retire_query_connection(self, future):
        record = self._records[future]
        if record["phase"] not in ("active", "expired") or record["request_id"] is None:
            return False
        record["phase"] = "lost"
        self._generation += 1
        self.retired.append(future)
        return True

    def reject_query_result(self, future):
        if future not in self._records:
            raise ValueError("this Future is not bound to a client query")
        self.rejected.append(future)

    def settle(self, future, value):
        record = self._records[future]
        record["phase"] = "terminal"
        record["answered_source"] = record["issued_source"]
        future.set_result(value)

    def fail(self, future, error):
        record = self._records[future]
        record["phase"] = "terminal"
        record["answered_source"] = None
        future.set_exception(error)

    def corrupt(self, future, **changes):
        self._records[future].update(changes)

    def expire(self, future):
        self._records[future]["phase"] = "expired"

    def held(self, kind):
        return [
            future for request, future in self.requests
            if request.kind == kind and not future.done()
        ]

    def only(self, kind):
        futures = self.held(kind)
        assert len(futures) == 1, f"expected exactly one held {kind} query, saw {len(futures)}"
        return futures[0]


def _empty_router(tmp_path, client, *, name):
    store = AppIoStore(tmp_path / name)
    store.connection.commit()
    router = HistoryRouter(
        QueryService(store), client, AppRegistry(), clock_ns=client.clock)
    return router, store


def _remote_frame(router, client, window, *, key=KEY, app_id=17):
    """Prepare one remote-ranking frame and settle its local ownership evidence."""

    router.prepare(window)
    assert router._local_future is not None
    router._local_future.result(timeout=15)
    future = client.only("ranking")
    client.settle(future, {
        "kind": "ranking",
        "result": [total(app_id, key)],
        "snapshot": DIGEST,
        "next_cursor": None,
    })
    assert router.ready(window) is True
    return router.registry.id_for(key)


def _totals_response(key=KEY, *, window, value=None, snapshot=DIGEST, kind="totals"):
    response = {
        "kind": kind,
        "result": share(1, window) if value is None else value,
        "snapshot": snapshot,
        "next_cursor": None,
    }
    if key is not None:
        response["app_key"] = key
    return response


def _file_coverage(window):
    """One complete file-coverage envelope, for a wrong-variant coverage arm."""

    overflow = UnknownOwnerOverflow(window, "overflow_owner_unknown", 0, 0, 0, coverage())
    return FileCoverage(
        1, window,
        0, 0, 0, 0, 0, 0,
        0, 0, 0,
        HookHits(0, 0, 0, 0, 0, 0),
        0, 0,
        overflow, coverage(),
    )


def test_settlement_rejects_each_selector_shape_or_binding_violation(tmp_path):
    """Each single selector/binding fault is a current invalid_result, never absent.

    The malformed process-coverage arms are the two separate mappings from
    §12.3 plus their passing control; every arm settles through its real
    associated Future, so no arm can pass by fabricating a router record.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="settlement.db")
    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    other = Window("since_boot", BOOT, end_mono_ns=150 * NS)
    try:
        presentation = _remote_frame(router, client, window)
        router.select_detail(KEY, window)
        scope = retention_scope(KEY, window)

        def admit(kind="totals"):
            state = router.request_detail(kind, presentation, window)
            assert state.state == "pending"
            return client.only(kind)

        def assert_failed(future, label):
            assert router.part_state("totals", KEY, scope).state == "failed", label
            assert router.part_state("totals", KEY, scope).reason == "invalid_result", label
            assert router.retained("totals", KEY, scope, window.end_mono_ns) is None, label
            assert client.rejected and client.rejected[-1] is future, label

        arms = (
            ("wrong kind", _totals_response(KEY, window=window, kind="series",
                                            value=series(1, window))),
            ("wrong external key", _totals_response(OTHER_KEY, window=window)),
            ("wrong window", _totals_response(KEY, window=window, value=share(1, other))),
            ("coverage for totals", _totals_response(KEY, window=window, value=coverage())),
        )
        for label, response in arms:
            future = admit()
            client.settle(future, response)
            assert router.drain() is True, label
            assert_failed(future, label)

        # The bound transport must carry this exact normalized request.
        future = admit()
        client.corrupt(future, request=_normalize_request(
            QueryRequest("series", window, KEY)))
        client.settle(future, _totals_response(KEY, window=window))
        assert router.drain() is True
        assert_failed(future, "wrong request association")

        # A duplicate ranking identity is a frame-ranking rejection, and the
        # rejected candidate is evicted before any notification.
        router.prepare(window)
        router._local_future.result(timeout=15)
        duplicate = client.only("ranking")
        client.settle(duplicate, {
            "kind": "ranking",
            "result": [total(17, KEY), total(18, KEY)],
            "snapshot": DIGEST,
            "next_cursor": None,
        })
        assert router.ready(window) is True
        assert router._remote is None
        assert router.daemon_answer_error is not None
        assert router.daemon_answer_error.kind == "rejected"
        assert router.daemon_answer_error.reason == "invalid_result"
        assert client.rejected[-1] is duplicate

        # Coverage selectors are validated at the same settlement boundary. A
        # frame owns at most one remote token per owner/generation context.
        frame = router._frame
        serial = [0]

        def coverage_arm(request, result, label, *, rejected):
            serial[0] += 1
            token = RequestToken(
                900 + serial[0], request, "remote", None, "frame",
                frame.owner_id, 500 + serial[0], frame.authority,
                frame.requested_source)
            future = client.query(request)
            client.settle(future, {
                "kind": "coverage", "result": result, "snapshot": DIGEST,
                "next_cursor": None,
            })
            settled = router._settle(token, RemoteSettlement(future, response=future.result()))
            assert settled is False, label
            if rejected:
                assert client.rejected[-1] is future, label
            return future

        process = QueryRequest("coverage", window, None, coverage_kind="process")
        coverage_arm(process, _file_coverage(window), "file coverage in process slot",
                     rejected=True)
        coverage_arm(process, coverage(), "scalar in unscoped process slot", rejected=True)
        control = coverage_arm(
            process, {"unit:alpha.service": coverage()},
            "valid unscoped process mapping", rejected=False)
        assert control not in client.rejected

        unknown = QueryRequest("coverage", window, None, coverage_kind="unknown_owner")
        coverage_arm(unknown, coverage(), "invalid UnknownOwnerOverflow", rejected=True)
    finally:
        router.close()
        store.connection.close()


def test_settlement_superseded_or_duplicate_has_no_publication_effect(tmp_path):
    """Stale or duplicate outcomes never mutate a record, stamp or marker.

    Covers a superseded frame generation, a dead detail lifetime under changed
    connection authority, subject/scope/boot retirement, and a second
    settlement after success and after failure; only the matching terminal wire
    slot is released.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="superseded.db")
    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    scope = retention_scope(KEY, window)
    try:
        presentation = _remote_frame(router, client, window)

        # A superseded frame generation cannot publish its delayed ranking.
        router.prepare(window)
        stale_ranking = client.only("ranking")
        router.prepare(window)
        client.settle(stale_ranking, {
            "kind": "ranking", "result": [total(17, KEY)], "snapshot": DIGEST,
            "next_cursor": None,
        })
        assert router.drain() is False
        assert router._remote is None
        assert router.frame_provenance(window) is None
        # Complete the replacement frame so the wire is free for the detail arm.
        router._local_future.result(timeout=15)
        client.settle(client.only("ranking"), {
            "kind": "ranking", "result": [total(17, KEY)], "snapshot": DIGEST,
            "next_cursor": None,
        })
        assert router.ready(window) is True

        # A dead detail lifetime cannot publish a delayed success.
        router.select_detail(KEY, window)
        state = router.request_detail("totals", presentation, window)
        stale_token = router._detail.live["totals"]
        stale_future = client.only("totals")
        router.select_detail(None, None)
        client.settle(stale_future, _totals_response(KEY, window=window))
        router.drain()
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        assert router.part_state("totals", KEY, scope).state == "absent"
        assert router._occupant is None  # the terminal wire slot was released

        # A connection-authority change retires the lifetime before the old
        # answer can publish.
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        old_future = client.only("totals")
        client._generation += 1
        client.settle(old_future, _totals_response(KEY, window=window))
        router.drain()
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        assert router.part_state("totals", KEY, scope).state == "absent"
        assert router._occupant is None

        # Subject and scope retirement behave the same way.
        for retire in (
            lambda: router.select_detail(OTHER_KEY, window),
            lambda: router.select_detail(KEY, Window("range", BOOT, 0, 140 * NS)),
            lambda: router.select_detail(KEY, Window("since_boot", "boot-b",
                                                     end_mono_ns=140 * NS)),
        ):
            router.select_detail(KEY, window)
            router.request_detail("totals", presentation, window)
            future = client.only("totals")
            retire()
            client.settle(future, _totals_response(KEY, window=window))
            router.drain()
            assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
            assert router._occupant is None

        # Duplicate settlement after success: same record, same stamp, no change.
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        accepted = router._detail.live["totals"]
        future = client.only("totals")
        client.settle(future, _totals_response(KEY, window=window))
        assert router.drain() is True
        record = router.retained("totals", KEY, scope, window.end_mono_ns)
        assert record is not None
        stamp = record.provenance.first_seen_at_ns
        assert router._settle(accepted, RemoteSettlement(future, response=future.result())) is False
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is record
        assert record.provenance.first_seen_at_ns == stamp

        # Duplicate settlement after failure: the failed slot is unchanged.
        router.request_detail("totals", presentation, window)
        failed_token = router._detail.live["totals"]
        failed_future = client.only("totals")
        error = RemoteError("daemon_error", "collector exploded")
        client.fail(failed_future, error)
        assert router.drain() is True
        failed = router.part_state("totals", KEY, scope)
        assert failed.state == "failed" and failed.reason == "collector exploded"
        assert router._settle(
            failed_token, RemoteSettlement(failed_future, error=error)) is False
        assert router.part_state("totals", KEY, scope) == failed
        assert stale_token.token_id != accepted.token_id
        assert router.pending_work is False
    finally:
        router.close()
        store.connection.close()


def test_scope_owned_detail_survives_forward_frame_generations(tmp_path):
    """Detail belongs to its selected scope, not to one preparing frame.

    An ordinary forward preparation neither cancels the pending owner nor
    relabels its delayed answer; the exact A window remains readable through
    the retained accessor while B is applied/pending.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="scope-owned.db")
    window_a = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    window_b = Window("since_boot", BOOT, end_mono_ns=180 * NS)
    scope_a = retention_scope(KEY, window_a)
    scope_b = retention_scope(KEY, window_b)
    try:
        presentation = _remote_frame(router, client, window_a)
        router.select_detail(KEY, window_a)
        state_a = router.request_detail("totals", presentation, window_a)
        assert state_a.state == "pending"
        owner = state_a.request_id
        pending_future = client.only("totals")

        router.prepare(window_b)
        assert router._detail.live["totals"].request.window == window_a
        assert router.part_state("totals", KEY, scope_a).request_id == owner
        assert router.part_state("totals", KEY, scope_a).state == "pending"
        # A since-boot retention scope carries its boot, not a moving endpoint:
        # both applied windows share one slot and one owner.
        assert scope_a == scope_b
        assert router.part_state("totals", KEY, scope_b).requested_window == window_a

        client.settle(pending_future, _totals_response(KEY, window=window_a, value=share(1, window_a, read=91)))
        assert router.drain() is True

        record = router.retained("totals", KEY, scope_b, window_b.end_mono_ns)
        assert record is not None
        assert record.value.window == window_a
        assert record.value.task_read_bytes == 91
        assert record.provenance.measured_window == window_a
        assert record.provenance.accepted_identity.window == window_a
        assert router.part_state("totals", KEY, scope_a).state == "ok"
        assert router.part_state("totals", KEY, scope_a).request_id == owner

        with pytest.raises(HistoryPending):
            router.disk_share(presentation, window_b)
        assert router.retained("totals", KEY, scope_b, window_b.end_mono_ns) == record

        # A record survives an ordinary preparation too; only adoption of a
        # changed scope retires it.
        router.prepare(window_b)
        assert router.retained("totals", KEY, scope_b, window_b.end_mono_ns) == record
        router.select_detail(KEY, window_b)
        assert router.retained("totals", KEY, scope_b, window_b.end_mono_ns) == record
        assert router.part_state("totals", KEY, scope_b).state == "ok"
        with pytest.raises(HistoryPending):
            router.disk_share(presentation, window_b)
        assert router.retained("totals", KEY, scope_b, window_b.end_mono_ns) == record
    finally:
        router.close()
        store.connection.close()


@pytest.mark.parametrize(
    "fault",
    ["remote_error", "window_unavailable", "connection_lost", "query_timeout",
     "invalid_result"],
)
def test_current_detail_failure_clears_record_before_view_notification(tmp_path, fault):
    """A current failure clears the record before the drain batch notifies.

    RemoteError, WindowUnavailableError, an authority-current injected
    ConnectionLost, query_timeout and invalid_result each preserve their own
    exception/reason; a pending retry cannot restore the record.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name=f"failure-{fault}.db")
    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    scope = retention_scope(KEY, window)
    try:
        presentation = _remote_frame(router, client, window)
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        first = client.only("totals")
        client.settle(first, _totals_response(KEY, window=window))
        assert router.drain() is True
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is not None

        observed = []
        original_notify = router._notify

        def notify():
            observed.append((
                router.retained("totals", KEY, scope, window.end_mono_ns),
                router.part_state("totals", KEY, scope),
            ))
            original_notify()

        router._notify = notify
        state = router.request_detail("totals", presentation, window)
        assert state.state == "pending"
        second = client.only("totals")
        if fault == "remote_error":
            error = RemoteError("daemon_error", "collector exploded")
            client.fail(second, error)
        elif fault == "window_unavailable":
            error = WindowUnavailableError("file_source_unavailable", "file history unavailable")
            client.fail(second, error)
        elif fault == "connection_lost":
            error = ConnectionLost("daemon_not_answering", "connection ended")
            client.fail(second, error)
        elif fault == "query_timeout":
            error = RequestRejected("query_timeout", "the daemon did not answer within five seconds")
            client.fail(second, error)
        else:
            error = RequestRejected("invalid_result", "invalid_result")
            client.settle(second, _totals_response(KEY, window=window, value=coverage()))
        assert router.drain() is True

        assert observed, "the view notification observer never ran"
        inside_record, inside_state = observed[-1]
        assert inside_record is None
        assert inside_state.state == "failed"
        assert inside_state.reason == str(error) or inside_state.reason == error.reason
        failed = router.part_state("totals", KEY, scope)
        assert failed.state == "failed"
        assert failed.reason == (str(error) if str(error) else type(error).__name__)
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        with pytest.raises(type(error)) as raised:
            router.disk_share(presentation, window)
        assert str(raised.value) == str(error)

        # A pending retry cannot restore the cleared record.
        retry = router.request_detail("totals", presentation, window)
        assert retry.state == "pending"
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        assert router.drain() is False
        assert router.part_state("totals", KEY, scope).state == "pending"
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
    finally:
        router.close()
        store.connection.close()


def test_exact_getters_reject_older_retained_window_without_hiding_record(tmp_path):
    """An exact getter never repairs an older record into the requested window.

    The applied B scope raises HistoryPending for its own exact answer while
    the accepted A record stays readable and unmodified through retained.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="exact-getters.db")
    window_a = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    window_b = Window("since_boot", BOOT, end_mono_ns=180 * NS)
    scope_b = retention_scope(KEY, window_b)
    try:
        presentation = _remote_frame(router, client, window_a)
        router.select_detail(KEY, window_a)
        router.request_detail("totals", presentation, window_a)
        client.settle(client.only("totals"),
                      _totals_response(KEY, window=window_a, value=share(1, window_a, read=89)))
        assert router.drain() is True
        record = router.retained("totals", KEY, scope_b, window_b.end_mono_ns)
        assert record is not None and record.value.window == window_a

        # Apply a forward frame B, then adopt it; the exact B answer is still
        # missing, so the exact getter must raise rather than relabel A.
        router.prepare(window_b)
        router._local_future.result(timeout=15)
        client.settle(client.only("ranking"), {
            "kind": "ranking", "result": [total(17, KEY)], "snapshot": DIGEST,
            "next_cursor": None,
        })
        assert router.ready(window_b) is True
        router.select_detail(KEY, window_b)
        assert router.retained("totals", KEY, scope_b, window_b.end_mono_ns) == record
        with pytest.raises(HistoryPending):
            router.disk_share(presentation, window_b)
        assert router.retained("totals", KEY, scope_b, window_b.end_mono_ns) == record
        pending = router.part_state("totals", KEY, retention_scope(KEY, window_b))
        assert pending.state == "pending"
        assert pending.requested_window == window_b
        # The admitted exact B request still never rewrites the A measurement.
        assert record.value.window == window_a
        assert record.provenance.measured_window == window_a
        assert record.value.app_id == presentation
        assert router.registry.key_for(record.value.app_id) == KEY

        # A wrong presentation id is a lookup failure, not a repaired subject.
        with pytest.raises((KeyError, TypeError)):
            router.disk_share(presentation + 1000, window_b)
        assert router.retained("totals", KEY, scope_b, window_b.end_mono_ns) == record
    finally:
        router.close()
        store.connection.close()


def test_retained_accessor_uses_applied_endpoint_and_is_pure(tmp_path):
    """Retained eligibility follows the caller's applied endpoint, never prepare.

    The accessor sends, drains, restamps nothing, rejects malformed arguments,
    withholds a future-end record, and keeps that answer through a forward and
    a backward preparation until adoption resets it.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="retained-pure.db")
    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    forward = Window("since_boot", BOOT, end_mono_ns=180 * NS)
    backward = Window("since_boot", BOOT, end_mono_ns=120 * NS)
    scope = retention_scope(KEY, window)
    try:
        presentation = _remote_frame(router, client, window)
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        client.settle(client.only("totals"),
                      _totals_response(KEY, window=window, value=share(1, window, read=73)))
        assert router.drain() is True
        record = router.retained("totals", KEY, scope, window.end_mono_ns)
        assert record is not None
        stamp = record.provenance.first_seen_at_ns
        sends = len(client.requests)
        completed = router._completed.qsize()

        original_drain = router.drain

        def forbidden():
            raise AssertionError("retained must not drain")

        router.drain = forbidden
        try:
            for _ in range(3):
                client.clock.advance_ns(10 * NS)
                assert router.retained("totals", KEY, scope, window.end_mono_ns) is record
                assert record.provenance.first_seen_at_ns == stamp
            # A future measured endpoint is ineligible, not restamped.
            assert router.retained("totals", KEY, scope, window.end_mono_ns - 1) is None
            assert record.provenance.first_seen_at_ns == stamp
            assert len(client.requests) == sends
            assert router._completed.qsize() == completed

            # Prepare cannot change eligibility; adoption owns the reset.
            router.prepare(forward)
            router._local_future.result(timeout=15)
            assert router.retained("totals", KEY, scope, window.end_mono_ns) is record
            router.prepare(backward)
            router._local_future.result(timeout=15)
            assert router.retained("totals", KEY, scope, window.end_mono_ns) is record
        finally:
            router.drain = original_drain

        # Malformed arguments are rejected, never relabelled.
        with pytest.raises(ValueError):
            router.retained("totals", KEY, (OTHER_KEY, "lifetime"), None)
        with pytest.raises(ValueError):
            router.retained("totals", KEY, retention_scope(KEY, Window("lifetime")), 5)
        with pytest.raises(ValueError):
            router.retained("totals", KEY, (KEY, "range", BOOT, 0), 5)
        with pytest.raises(ValueError):
            router.retained("totals", KEY, scope, -1)

        router.select_detail(KEY, backward)
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
    finally:
        router.close()
        store.connection.close()


def test_superseded_failure_does_not_clear_newer_accepted_record(tmp_path):
    """An old token's delayed error cannot clear a newer accepted measurement.

    A newer acceptance replaces the record and its first-seen stamp; the old
    token's error and a later pending retry leave both exactly as accepted.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="superseded-failure.db")
    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    scope = retention_scope(KEY, window)
    try:
        presentation = _remote_frame(router, client, window)
        router.select_detail(KEY, window)

        router.request_detail("totals", presentation, window)
        old_token = router._detail.live["totals"]
        old_future = client.only("totals")
        client.settle(old_future, _totals_response(KEY, window=window))
        assert router.drain() is True
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is not None

        router.request_detail("totals", presentation, window)
        new_token = router._detail.live["totals"]
        new_future = client.only("totals")
        client.settle(new_future, _totals_response(KEY, window=window, value=share(1, window, read=64)))
        assert router.drain() is True
        newer = router.retained("totals", KEY, scope, window.end_mono_ns)
        assert newer is not None and newer.value.task_read_bytes == 64
        stamp = newer.provenance.first_seen_at_ns

        # Deliver the superseded token's error after the newer acceptance.
        error = RemoteError("daemon_error", "late failure")
        assert router._settle(old_token, RemoteSettlement(old_future, error=error)) is False
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is newer
        assert router.retained("totals", KEY, scope, window.end_mono_ns).provenance \
            .first_seen_at_ns == stamp
        assert router.part_state("totals", KEY, scope).state == "ok"
        assert router.part_state("totals", KEY, scope).request_id == new_token.token_id

        # A later pending retry neither restores nor clears the newer record.
        router.request_detail("totals", presentation, window)
        pending = router.part_state("totals", KEY, scope)
        assert pending.state == "pending"
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is newer
        assert newer.provenance.first_seen_at_ns == stamp
        assert old_token.token_id != new_token.token_id
    finally:
        router.close()
        store.connection.close()


def test_retention_resets_before_subject_scope_boot_or_backward_display(tmp_path):
    """Scope transitions clear old detail synchronously, before any render.

    Subject, tag, range bounds, boot and a backward endpoint each retire the
    live lifetime immediately through the same owner settlement seam; a
    backward-then-forward return and a second distinct range never resurrect
    the old record.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="retention-resets.db")
    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    big = Window("since_boot", BOOT, end_mono_ns=180 * NS)
    small = Window("since_boot", BOOT, end_mono_ns=110 * NS)
    range_one = Window("range", BOOT, 0, 140 * NS)
    range_two = Window("range", BOOT, 200 * NS, 240 * NS)
    scope = retention_scope(KEY, window)
    try:
        presentation = _remote_frame(router, client, window)
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        client.settle(client.only("totals"), _totals_response(KEY, window=window))
        assert router.drain() is True
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is not None
        sends = len(client.requests)
        owner = router._detail.lifetime_id

        transitions = (
            ("subject", lambda: router.select_detail(OTHER_KEY, window)),
            ("tag", lambda: router.select_detail(KEY, range_one)),
            ("range bounds", lambda: router.select_detail(KEY, range_two)),
            ("boot", lambda: router.select_detail(KEY, Window("since_boot", "boot-b",
                                                              end_mono_ns=140 * NS))),
        )
        detail_calls = []
        detail_impl = router._retire_detail_token

        def detail_spy(token):
            detail_calls.append(token.token_id)
            return detail_impl(token)

        router._retire_detail_token = detail_spy
        try:
            for label, retire in transitions:
                router.select_detail(KEY, window)
                router.request_detail("totals", presentation, window)
                client.settle(client.only("totals"), _totals_response(KEY, window=window))
                assert router.drain() is True
                assert router.retained("totals", KEY, scope, window.end_mono_ns) is not None, label
                settled_token = router._detail.slots[("totals", scope)].token.token_id
                detail_calls.clear()
                retire()
                # Synchronous: no drain, ready, render or query participates,
                # and the reset enters the one owner retirement seam once.
                assert detail_calls == [settled_token], label
                assert router.retained("totals", KEY, scope, window.end_mono_ns) is None, label
                assert router.part_state("totals", KEY, scope).state == "absent", label
                assert len(client.requests) == sends + 1, label
                sends = len(client.requests)
        finally:
            del router._retire_detail_token

        # A backward endpoint retires; returning forward does not resurrect.
        router.select_detail(KEY, window)
        seen = router._detail.lifetime_id
        router.select_detail(KEY, big)
        assert router._detail.lifetime_id == seen  # forward preserves the lifetime
        router.select_detail(KEY, small)
        assert router._detail.lifetime_id != seen
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        router.select_detail(KEY, big)
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        assert router._detail.lifetime_id != seen
        assert router._detail.lifetime_id != owner

        # Two distinct ranges are distinct lifetimes too.
        router.select_detail(KEY, range_one)
        lifetime_one = router._detail.lifetime_id
        router.select_detail(KEY, range_two)
        assert router._detail.lifetime_id != lifetime_one
        assert router.retained("totals", KEY, retention_scope(KEY, range_one),
                               range_one.end_mono_ns) is None
        assert router.retained("totals", KEY, retention_scope(KEY, range_two),
                               range_two.end_mono_ns) is None
    finally:
        router.close()
        store.connection.close()


def test_authority_change_clears_details_without_conflating_cursor_advance(tmp_path):
    """Only authority identities retire owners; sequence and counters do not.

    Remote connection/boot/epoch and local registration changes clear the
    selected lifetime and its records, while same-authority cursor sequence
    advancement and local counter movement preserve them unchanged. One
    authority transition invokes each owner-level retirement arm exactly once
    through ``_settle``, clearing the accepted record and the pending owner.
    """

    client = _BoundClient()
    router, store = _empty_router(tmp_path, client, name="authority.db")
    window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
    scope = retention_scope(KEY, window)
    try:
        presentation = _remote_frame(router, client, window)
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        client.settle(client.only("totals"),
                      _totals_response(KEY, window=window, value=share(1, window, read=52)))
        assert router.drain() is True
        record = router.retained("totals", KEY, scope, window.end_mono_ns)
        assert record is not None
        stamp = record.provenance.first_seen_at_ns

        # Same authority: a sequence advance is not a connection change.
        client.cursor = StreamCursor(BOOT, 3, 8)
        client.state = ClientState(verified=True, answering=True, cursor=client.cursor)
        client.clock.advance_ns(NS)
        assert router.drain() is False
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is record
        assert record.provenance.first_seen_at_ns == stamp

        # One lifetime holds an accepted record and one pending detail. An
        # authority change retires both through the sole settlement writer,
        # invoking each owner-level retirement arm exactly once.
        router.request_detail("series", presentation, window)
        pending = router._detail.live["series"]
        held_series = client.only("series")
        accepted_slot = router._detail.slots[("totals", scope)]
        assert accepted_slot.record is record
        frame = router._frame
        frame_sides = tuple(side.token_id for side in
                            (frame.local_token, frame.remote_token) if side is not None)
        assert len(frame_sides) == 2, "the frame owns its local and remote tokens"
        frame_calls, detail_calls, retirements_seen = [], [], []
        settle_impl = router._settle
        frame_impl = router._retire_frame_owner
        detail_impl = router._retire_detail_token

        def settle_spy(token, outcome=None, *, retiring=False):
            retirements_seen.append((token.owner, token.token_id, retiring))
            return settle_impl(token, outcome, retiring=retiring)

        def frame_spy(token):
            frame_calls.append(token.token_id)
            return frame_impl(token)

        def detail_spy(token):
            detail_calls.append(token.token_id)
            return detail_impl(token)

        router._settle = settle_spy
        router._retire_frame_owner = frame_spy
        router._retire_detail_token = detail_spy
        try:
            # A stored epoch is authority; a new epoch retires the lifetime once.
            before = router._generation
            client.cursor = StreamCursor(BOOT, 4, 8)
            client.state = ClientState(verified=True, answering=True, cursor=client.cursor)
            assert router.drain() is True
        finally:
            del router._settle, router._retire_frame_owner, router._retire_detail_token

        # Frame owner: one retirement for both side tokens, one generation bump.
        assert frame_calls == [frame_sides[0]], "one frame-owner retirement"
        assert router._generation == before + 1, "one generation advance per frame owner"
        assert set(frame_sides) <= router._retired_tokens
        # Detail owner: one retirement clears the whole selected lifetime.
        assert detail_calls == [pending.token_id], "one detail-lifetime retirement"
        assert [entry for entry in retirements_seen
                if entry[0] == "detail" and entry[2]] == [
                    ("detail", pending.token_id, True)], "detail enters the settlement seam"
        assert accepted_slot.record is None, "the retirement arm cleared the accepted record"
        assert accepted_slot.stamps == {}, "the retirement arm cleared the stamp lookup"
        assert router._detail is None
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        assert router.part_state("totals", KEY, scope).state == "absent"
        assert router.part_state("series", KEY, scope).state == "absent"
        assert router.view_state.startswith("unavailable")

        # A publication-retired completion is drained and ignored; its real
        # transport occupant is released only now that it is terminal.
        client.settle(held_series, _totals_response(
            KEY, window=window, kind="series", value=series(1, window)))
        assert router.drain() is False
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
        assert router._occupant is None

        retirements = router._generation
        assert retirements == before + 1
        assert router.drain() is False
        assert router._generation == retirements, "an already retired frame is not retired again"

        # A new boot is authority too.
        router.prepare(window)
        router._local_future.result(timeout=15)
        client.settle(client.only("ranking"), {
            "kind": "ranking", "result": [total(17, KEY)], "snapshot": DIGEST,
            "next_cursor": None,
        })
        assert router.ready(window) is True
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        client.settle(client.only("totals"), _totals_response(KEY, window=window))
        assert router.drain() is True
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is not None
        client.cursor = StreamCursor("boot-b", 4, 8)
        client.state = ClientState(verified=True, answering=True, cursor=client.cursor)
        assert router.drain() is True
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None

        # A new connection generation with the same boot/epoch is a change.
        router.prepare(window)
        router._local_future.result(timeout=15)
        client.settle(client.only("ranking"), {
            "kind": "ranking", "result": [total(17, KEY)], "snapshot": DIGEST,
            "next_cursor": None,
        })
        assert router.ready(window) is True
        router.select_detail(KEY, window)
        router.request_detail("totals", presentation, window)
        client.settle(client.only("totals"), _totals_response(KEY, window=window))
        assert router.drain() is True
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is not None
        client._generation += 1
        assert router.drain() is True
        assert router.retained("totals", KEY, scope, window.end_mono_ns) is None
    finally:
        router.close()
        store.connection.close()


# ---------------------------------------------------------------------------
# Files settlement integration (§§3.3-3.4, 5.1, 5.4, 12.2)
# ---------------------------------------------------------------------------

FILE_APP = "exe:/demo"
FILE_ZERO = "exe:/zero"
FILE_BOOT = "boot-A"
FILE_WINDOW = Window("since_boot", FILE_BOOT, end_mono_ns=FILE_HOUR_NS)


def _file_frames(tmp_path, name="files-settlement.db"):
    """One committed file-capable store: T/D receipts plus hourly F rows."""

    frames = FileFrames(tmp_path / name)
    loader = VectorLoader()
    runtime = FileRuntime(
        frames.db,
        source_factory=lambda: BpfFileSource(loader),
        identity_resolver=FileIdentityResolver(lambda dev: "fs"),
    )
    runtime.start()
    loader.queue(0, events=[
        replace(file_event(inode=index, amount=10 + index), basename=basename,
                name_len=len(basename), reads=index, writes=8 - index)
        for index, basename in enumerate(
            [b"Cafe\xcc\x81", b"Caf\xc3\xa9", b"alpha", b"zeta"])])
    runtime.consume(*frames.tick(0))
    runtime.consume(*frames.tick(FILE_HOUR_NS, amount=200))
    return frames


class _LocalCountingClient:
    """A verified-not-answering client: a local outcome must issue no query."""

    state = ClientState()

    def __init__(self):
        self.queries = 0
        self.transports = 0

    def query(self, request):  # pragma: no cover - local work never asks
        self.queries += 1
        raise AssertionError(f"a local outcome must not query: {request}")

    def query_transport(self, future):  # pragma: no cover - local has no transport
        self.transports += 1
        raise AssertionError("a local outcome has no remote transport")


def _drain_until(router, predicate, timeout=20.0):
    """Owner-thread drain loop with a fixed bound; never a worker-thread wait."""

    deadline = time.monotonic() + timeout
    while not predicate():
        router.drain()
        if predicate():
            break
        assert time.monotonic() < deadline, "drain never satisfied the predicate"
        time.sleep(0.001)


def _collect_settlement(router):
    """Read one queued local settlement on the owner thread."""

    _drain_until(router, lambda: not router._completed.empty())
    return router._completed.get_nowait()


def _forged_frame_settlement(router, window, field, value):
    """One valid frame admission with exactly one routing field corrupted."""

    router.prepare(window)
    outcome = router._local_future.result(timeout=20)
    assert isinstance(outcome, LocalFrameOutcome)
    token = router._frame.local_token
    binding = router._worker_bindings[token.token_id]
    bracket = LocalBracket(binding.opening, router._closing_vector(binding))
    settlement = LocalSettlement(token.token_id, outcome=outcome, local_bracket=bracket)
    forged = copy.copy(outcome)
    object.__setattr__(forged, field, value)
    object.__setattr__(settlement, "outcome", forged)
    return token, settlement


def test_local_outcomes_share_settlement_for_frame_part_and_files(tmp_path):
    """Every local variant publishes once through the sole settlement writer.

    A real frame copy publishes all routing/file-availability evidence together,
    a selected part publishes its exact Window and canonical digest, and a local
    Files result publishes its unchanged table/coverage/served_gap_count. A
    malformed worker association or evidence fails that owner alone before any
    of its facts publish; stale drained outcomes change nothing.
    """

    frames = _file_frames(tmp_path)
    client = _LocalCountingClient()
    router = HistoryRouter(QueryService(frames.db), client, AppRegistry())
    window = FILE_WINDOW
    scope = retention_scope(FILE_APP, window)
    other_window = Window("range", FILE_BOOT, 200 * NS, 300 * NS)
    settlements: list[tuple[int, str, bool]] = []
    settle_impl = router._settle

    def settle_spy(token, outcome=None, *, retiring=False):
        changed = settle_impl(token, outcome, retiring=retiring)
        settlements.append((token.token_id, token.owner, changed))
        return changed

    router._settle = settle_spy
    try:
        # -- positive frame arm: every evidence field publishes atomically ---
        router.prepare(window)
        outcome = router._local_future.result(timeout=20)
        assert isinstance(outcome, LocalFrameOutcome)
        expected_ranking = [replace(row, app_id=router.registry.id_for(row.app_key))
                            for row in outcome.ranking]
        assert router.ready(window) is True
        presentation = router.registry.id_for(FILE_APP)
        assert router._ranking == expected_ranking
        assert router.segments == outcome.segments
        assert router.boundaries == outcome.boundaries
        assert router._gap_ranges == list(outcome.gaps)
        assert router._known_gap_ranges == list(outcome.known_gaps)
        assert router._overlap_ranges == list(outcome.overlaps)
        assert router._has_local_history is outcome.has_local_history
        assert router._not_retained is outcome.not_retained
        assert router._chronology_unproved is outcome.chronology_unproved
        assert router._file_status.available is outcome.file_available
        frame_token = router._frame.local_token
        assert router._frame.local_settled is True
        assert [entry for entry in settlements
                if entry[0] == frame_token.token_id] == [
                    (frame_token.token_id, "frame", True)]
        assert client.queries == 0 and client.transports == 0
        # A frame proves routing only: no eager first-subject/all-row detail.
        assert router._worker_jobs == {} and router._unscheduled == {}

        # -- positive selected-part arm: exact Window and canonical digest ---
        router.select_detail(FILE_APP, window)
        assert router.request_detail("totals", presentation, window).state == "pending"
        _drain_until(router, lambda: router.part_state("totals", FILE_APP, scope).state
                     != "pending")
        state = router.part_state("totals", FILE_APP, scope)
        assert state.state == "ok" and state.requested_window == window
        record = router.retained("totals", FILE_APP, scope, window.end_mono_ns)
        assert record is not None
        assert record.provenance.measured_window == window
        assert record.value.window == window
        assert record.value.app_id == presentation
        assert record.provenance.accepted_identity.data_digest == local_answer_digest(
            "totals", FILE_APP, window, record.value)
        assert [entry for entry in settlements
                if entry[0] == state.request_id] == [
                    (state.request_id, "detail", True)]
        assert client.queries == 0 and client.transports == 0

        # -- positive local Files arm: unchanged table/coverage/served gap ---
        paths = (("local", frames.db.execute("PRAGMA database_list").fetchone()[2],
                  "gui"),)
        expected = HistoryRouter._read_local_files(
            paths, window, FILE_APP, presentation, "bytes_total", "desc", None)
        future = router.request_file_view(presentation, window, "bytes_total", "desc", None)
        _drain_until(router, future.done)
        view = future.result(timeout=0)
        assert isinstance(view, FileViewResult)
        assert view.table == expected.table
        assert view.coverage == expected.coverage
        assert view.served_gap_count == expected.served_gap_count
        assert type(view.served_gap_count) is int and view.served_gap_count == 0
        assert client.queries == 0 and client.transports == 0
        assert router._worker_jobs == {}
        files_calls = [entry for entry in settlements if entry[1] == "files"]
        assert len(files_calls) == 1 and files_calls[0][2] is True
        assert [entry for entry in settlements
                if entry[0] == files_calls[0][0]] == files_calls

        # -- malformed frame arms: association then routing evidence ---------
        for label, field, value in (
            ("worker", "worker_id", 9001),
            ("request", "request", QueryRequest("ranking", Window("lifetime"))),
            ("evidence", "boundaries", ((BOOT, 0, "gui", "gui"),)),
        ):
            token, settlement = _forged_frame_settlement(router, window, field, value)
            assert router._settle(token, settlement) is True, label
            # No routing evidence from the malformed outcome published.
            assert router._ranking == [], label
            assert router.segments == () and router.boundaries == (), label
            assert router._file_status.available is False, label
            assert router.view_state == "unavailable: local history evidence", label
            assert router._frame.local_settled is False, label
            assert [entry for entry in settlements
                    if entry[0] == token.token_id] == [
                        (token.token_id, "frame", True)], label
            with pytest.raises(ValueError):
                router.ready(window)

        # A clean frame restores the supported readiness context the remaining
        # arms need; the malformed frames changed only their own owner.
        router.prepare(window)
        assert router._local_future.result(timeout=20) is not None
        assert router.ready(window) is True

        # -- malformed part arms: key, type, Window and digest ---------------
        def malformed_part(field, value):
            router.select_detail(FILE_APP, window)
            assert router.request_detail("totals", presentation, window).state == "pending"
            token, settlement = _collect_settlement(router)
            assert token.owner == "detail"
            assert isinstance(settlement, LocalSettlement)
            assert settlement.local_bracket is None
            binding = router._worker_bindings[token.token_id]
            settlement = router._close_local_settlement(settlement, binding=binding)
            forged = copy.copy(settlement.outcome)
            if field == "window":
                value_copy = copy.copy(forged.value)
                object.__setattr__(value_copy, "window", value)
                object.__setattr__(forged, "value", value_copy)
            else:
                object.__setattr__(forged, field, value)
            object.__setattr__(settlement, "outcome", forged)
            return token, settlement

        for label, field, value in (
            ("key", "app_key", OTHER_KEY),
            ("type", "value", series(1, window)),
            ("window", "window", other_window),
            ("digest", "data_digest", "b" * 64),
        ):
            token, settlement = malformed_part(field, value)
            assert router._settle(token, settlement) is True, label
            failed = router.part_state("totals", FILE_APP, scope)
            assert failed.state == "failed" and failed.reason == "invalid_result", label
            assert failed.requested_window == window, label
            assert router.retained("totals", FILE_APP, scope, window.end_mono_ns) is None, label
            assert [entry for entry in settlements
                    if entry[0] == token.token_id] == [
                        (token.token_id, "detail", True)], label
            router._reap_background_worker(token.token_id)

        # -- malformed Files arms: table, coverage and served evidence -------
        def malformed_files(field, value):
            future = router.request_file_view(
                presentation, window, "bytes_total", "desc", None)
            token, settlement = _collect_settlement(router)
            assert token.owner == "files"
            assert isinstance(settlement.outcome, LocalFilesOutcome)
            forged = copy.copy(settlement.outcome)
            object.__setattr__(forged, field, value)
            object.__setattr__(settlement, "outcome", forged)
            return future, token, settlement

        ranking_before = list(router._ranking)
        for label, field, value in (
            ("table", "table", replace(expected.table, window=Window("lifetime"))),
            ("coverage", "coverage", replace(expected.coverage, window=Window("lifetime"))),
            ("served", "served_gap_count", -1),
        ):
            future, token, settlement = malformed_files(field, value)
            assert router._settle(token, settlement) is True, label
            with pytest.raises(RequestRejected) as caught:
                future.result(timeout=0)
            assert caught.value.reason == "invalid_result", label
            assert router._not_retained is False, label
            assert router.daemon_answer_error is None, label
            assert router._ranking == ranking_before, label
            assert [entry for entry in settlements
                    if entry[0] == token.token_id] == [
                        (token.token_id, "files", True)], label
            router._reap_background_worker(token.token_id)
        assert router._file_owners == {}

        # -- a stale drained success and error publish nothing --------------
        stale_token, stale_outcome, stale_settlement = None, None, None
        router.prepare(window)
        stale_outcome = router._local_future.result(timeout=20)
        stale_token = router._frame.local_token
        assert router.ready(window) is True
        stale_settlement = router._local_settlement
        router.prepare(window)
        assert router._local_future.result(timeout=20) is not None
        before = (list(router._ranking), router.segments, router.boundaries,
                  router._not_retained, router.view_state)
        assert router._settle(stale_token, stale_settlement) is False
        assert router._settle(
            stale_token, LocalSettlement(stale_token.token_id,
                                         error=ValueError("stale local copy"))) is False
        assert (list(router._ranking), router.segments, router.boundaries,
                router._not_retained, router.view_state) == before
        assert client.queries == 0 and client.transports == 0
    finally:
        del router._settle
        router.close()
        frames.db.close()


def _remote_files_page(window, *, rows=1, app_id=1):
    """One structurally valid remote Files page for the scheduler arms."""

    overflow = UnknownOwnerOverflow(window, "overflow_owner_unknown", 0, 0, 0, coverage())
    row = FileRow(0, "alpha.txt", "resolved", False, "8:0", 11, 1, 1, 1, 17, 5, 22, BOOT, 1)
    return FileTable(app_id, window, tuple(replace(row) for _ in range(rows)),
                     "bytes_total", "desc", None, overflow, coverage())


def test_ranking_readiness_excludes_all_detail_and_files_work(tmp_path):
    """Ranking/ownership readiness is terminal before any held detail or Files.

    Each arm holds exactly one independent dependency after the frame's ranking
    and ownership evidence has settled: a remote series, remote totals, a real
    local detail computation, a Files first page, a Files continuation or the
    final file coverage. ``ready`` stays true and the held Future stays pending;
    a successful frame never prefetches a subject's parts.
    """

    for index, arm in enumerate(
            ("series", "totals", "local-detail", "files-page",
             "files-continuation", "files-coverage")):
        window = Window("since_boot", BOOT, end_mono_ns=140 * NS)
        router = store = None
        release = threading.Event()
        try:
            if arm == "local-detail":
                local = _committed_store(
                    tmp_path / f"ready-{index}.db", ((ALPHA, 17, 20),))
                store = local
                router = HistoryRouter(QueryService(local), _IdleClient(), AppRegistry())
                router.prepare(window)
                assert router._local_future.result(timeout=20) is not None
                assert router.ready(window) is True
                assert router._worker_jobs == {} and router._unscheduled == {}
                presentation = router.registry.id_for(ALPHA)
                entered = threading.Event()
                original = router._read_local_part

                def held_part(*args, **kwargs):
                    entered.set()
                    assert release.wait(20), "the local detail worker was never released"
                    return original(*args, **kwargs)

                router._read_local_part = held_part
                router.select_detail(ALPHA, window)
                assert router.request_detail("series", presentation, window).state == "pending"
                assert entered.wait(10)
                held = None
                assert router.ready(window) is True
                assert router._background_charge("detail") == 1
                assert router.pending_work is True
                release.set()
                _drain_until(router, lambda: router._background_charge("detail") == 0)
                continue

            client = _BoundClient()
            router, store = _empty_router(tmp_path, client, name=f"ready-{index}.db")
            presentation = _remote_frame(router, client, window)
            # A completed frame proves only ranking/ownership; no part exists.
            assert router._worker_jobs == {} and router._unscheduled == {}
            assert router._background_charge("detail") == 0

            if arm in ("series", "totals"):
                router.select_detail(KEY, window)
                assert router.request_detail(arm, presentation, window).state == "pending"
                held = client.only(arm)
            else:
                future = router.request_file_view(
                    presentation, window, "bytes_total", "desc", None)
                assert future.done() is False
                first = client.only("files")
                if arm == "files-page":
                    held = first
                elif arm == "files-continuation":
                    client.settle(first, {
                        "kind": "files", "result": _remote_files_page(window),
                        "snapshot": DIGEST, "next_cursor": "opaque-page-2",
                        "app_key": KEY,
                    })
                    assert router.ready(window) is True
                    held = client.only("files")
                    assert client.query_transport(held).request.cursor == "opaque-page-2"
                else:
                    client.settle(first, {
                        "kind": "files", "result": _remote_files_page(window),
                        "snapshot": DIGEST, "next_cursor": None, "app_key": KEY,
                    })
                    assert router.ready(window) is True
                    held = client.only("coverage")
                    continuation = client.query_transport(held).request
                    assert continuation.kind == "coverage"
                    assert continuation.coverage_kind == "file"

            assert held.done() is False, arm
            # The held dependency is not a readiness gate and is not settled by
            # the readiness/drain path itself.
            assert router.ready(window) is True, arm
            assert router.ready(window) is True, arm
            assert held.done() is False, arm
            assert router.pending_work is True, arm
        finally:
            release.set()
            if router is not None:
                router.close()
            if store is not None:
                store.connection.close()


def test_local_worker_capacity_counts_retired_jobs_and_keeps_frame_independent(tmp_path):
    """The background ledger is bounded and the frame lane stays independent.

    Two charged detail jobs and eight Files workers, retired jobs included,
    fill the ten-job ceiling; repeated selection changes submit no eleventh
    Future and keep at most one waiting successor per kind. Releasing one charge
    admits exactly one eligible replacement, and a retired-but-running frame
    keeps the frozen not-done gate closed instead of queueing a duplicate copy.
    """

    frames = _file_frames(tmp_path, "capacity.db")
    router = HistoryRouter(QueryService(frames.db), _IdleClient(), AppRegistry())
    window = FILE_WINDOW
    release_detail = threading.Event()
    detail_entered = threading.Event()
    release_files = threading.Event()
    files_entered = threading.Event()
    frame_release = threading.Event()
    frame_entered = threading.Event()
    original_part = router._read_local_part
    original_files = router._read_local_files
    original_read = router._read_local

    def held_part(*args, **kwargs):
        detail_entered.set()
        assert release_detail.wait(20), "the detail worker was never released"
        return original_part(*args, **kwargs)

    def held_files(*args, **kwargs):
        files_entered.set()
        assert release_files.wait(20), "the Files worker was never released"
        return original_files(*args, **kwargs)

    def held_frame(paths, window_arg):
        frame_entered.set()
        assert frame_release.wait(20), "the frame worker was never released"
        return original_read(paths, window_arg)

    router._read_local_part = held_part
    router._read_local_files = held_files
    submitted: list[object] = []
    real_submit = router._background_executor.submit

    def counted_submit(work, /, *args, **kwargs):
        submitted.append(work)
        return real_submit(work, *args, **kwargs)

    router._background_executor.submit = counted_submit
    try:
        router.prepare(window)
        assert router._local_future.result(timeout=20) is not None
        assert router.ready(window) is True
        presentation = router.registry.id_for(FILE_APP)
        assert router._worker_jobs == {} and router._background_charge("detail") == 0
        assert router._frame_executor is not router._background_executor

        # Two charged detail jobs for the selected lifetime.
        router.select_detail(FILE_APP, window)
        assert router.request_detail("series", presentation, window).state == "pending"
        assert router.request_detail("totals", presentation, window).state == "pending"
        assert detail_entered.wait(10)
        assert router._background_charge("detail") == 2
        assert len(submitted) == 2 and router._unscheduled == {}

        # Eight Files workers fill the Files budget and the ten-job ceiling.
        # They queue behind the held detail worker; admission, not execution, is
        # what the ledger bounds here.
        files_futures = [
            router.request_file_view(presentation, window, "bytes_total", "desc", None)
            for _ in range(8)
        ]
        assert router._background_charge("files") == 8
        assert len(router._worker_jobs) == 10
        assert len(submitted) == 10

        # Repeated selection changes retire owners but never grow the queue: the
        # waiting current detail stays unscheduled with at most one successor.
        for iteration in range(6):
            key = FILE_ZERO if iteration % 2 == 0 else FILE_APP
            forward = Window("since_boot", FILE_BOOT,
                             end_mono_ns=FILE_HOUR_NS + (10 + iteration) * NS)
            router.select_detail(key, forward)
            state = router.request_detail(
                "series", router.registry.id_for(key), forward)
            assert state.state == "pending", iteration
            assert len(submitted) == 10, iteration
            assert len(router._worker_jobs) <= 10, iteration
            assert router._background_charge("detail") <= 2, iteration
            assert len(router._unscheduled) <= 1, iteration

        # Releasing the charged detail jobs admits exactly one replacement, not
        # an unbounded catch-up batch for every retired selection. The held Files
        # workers keep the background lane busy, so the replacement stays queued
        # on its own charge rather than running out of budget.
        release_detail.set()
        _drain_until(router, lambda: router._background_charge("detail") == 1)
        assert len(submitted) == 11, "exactly one waiting detail was admitted"
        assert router._background_charge("files") == 8
        assert len(router._worker_jobs) == 9
        assert router._unscheduled == {}

        # The frame lane keeps its own single Future. A retired-but-running
        # frame copy keeps the frozen not-done gate closed: no duplicate frame
        # worker is queued behind it.
        router._read_local = held_frame
        frame_window = Window("range", FILE_BOOT, 0, FILE_HOUR_NS)
        router.prepare(frame_window)
        assert frame_entered.wait(10)
        running = router._local_future
        assert router.local_read_in_flight is True
        router.invalidate()
        assert router.local_read_in_flight is True
        assert router._local_future is running
        assert router._frame_executor._work_queue.qsize() == 0

        def shell_gate(wanted):
            if router.local_read_in_flight is not False:
                return False
            router.prepare(wanted)
            return True

        assert shell_gate(frame_window) is False
        assert router._local_future is running
        frame_release.set()
        _drain_until(router, lambda: router.local_read_in_flight is False)
        assert router.local_read_in_flight is False
    finally:
        release_detail.set()
        release_files.set()
        frame_release.set()
        router.close()
        frames.db.close()
