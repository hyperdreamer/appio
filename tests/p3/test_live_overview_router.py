"""Neutral retained-answer and source records: exact fields, phases and digests.

The single required test on this module validates every §2 record field, the
PartState truth table, the canonical local-answer digest, and that importing the
neutral module stays free of the client, router, query service and Qt.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import FrozenInstanceError, fields, replace
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from appio.contracts import (
    DeviceShareRow,
    FileCoverage,
    FileTable,
    HookHits,
    UnknownOwnerOverflow,
    Window,
    WindowUnavailableError,
)
from appio.history_router import (
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
from appio.iodclient import AppRegistry, ClientState, RequestRejected
from appio.ipc_models import QueryRequest, StreamCursor
from appio.procs import AppKeyInputs, IoReadOutcome, ProcessIdentity, StableProcRecord
from appio.query import QueryService
from appio.sampler import TickInput, aggregate_tick
from appio.store import AppIoStore
from live_overview_support import Clock, coverage, series, share, total

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

    # -- immutable field inventory ----------------------------------------- #
    inventory = {
        AcceptedIdentity: ("kind", "app_key", "window", "data_digest"),
        RemotePosition: ("boot_id", "iod_epoch", "seq"),
        LocalPosition: ("name", "owner", "registration_epoch", "data_version", "total_changes"),
        LocalPositionVector: ("sources",),
        LocalBracket: ("opening", "closing"),
        AnswerProvenance: (
            "kind",
            "app_key",
            "scope",
            "measured_window",
            "accepted_identity",
            "first_seen_at_ns",
            "answered_source",
            "local_bracket",
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

    # -- AnswerProvenance --------------------------------------------------- #
    provenance = AnswerProvenance("totals", KEY, scope, since_boot, identity, 1_000_000_000, None, None)
    assert provenance.accepted_identity == identity
    local_provenance = AnswerProvenance(
        "totals", KEY, scope, since_boot, identity, 0, tolerant, bracket
    )
    assert local_provenance.answered_source == tolerant
    raced_provenance = AnswerProvenance(
        "totals", KEY, scope, since_boot, identity, 0, None, LocalBracket(tolerant, changed)
    )
    assert raced_provenance.answered_source is None
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
    assert FrameProvenance(3, since_boot, remote, remote, None, True).certified is True
    assert FrameProvenance(3, since_boot, tolerant, tolerant, bracket, True).certified is True
    fallback = FrameProvenance(3, since_boot, remote, tolerant, bracket, False)
    assert fallback.answered_source == tolerant
    uncertified = FrameProvenance(3, since_boot, tolerant, None, LocalBracket(tolerant, changed), False)
    assert uncertified.certified is False
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
        "          'appio.identity', 'PyQt6')\n"
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
        for future in files_futures:
            future.exception(timeout=15)
        helper_token, helper_settlement = router._completed.get(timeout=15)
        assert helper_token is helper_files
        assert isinstance(helper_settlement, LocalSettlement)
        assert isinstance(helper_settlement.error, WindowUnavailableError)
        # Terminal Files charges stay charged until an owner reap: the same ledger
        # that admitted them releases them, never a worker-thread done callback.
        assert router._background_charge("files") == 8
        router._reap_terminal_background_workers()
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
