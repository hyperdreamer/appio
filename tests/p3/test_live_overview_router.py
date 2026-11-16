"""Neutral retained-answer and source records: exact fields, phases and digests.

The single required test on this module validates every §2 record field, the
PartState truth table, the canonical local-answer digest, and that importing the
neutral module stays free of the client, router, query service and Qt.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import FrozenInstanceError, fields, replace
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
from appio.iodclient import AppRegistry, ClientState
from appio.ipc_models import QueryRequest
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
        ("detail.no-subject", {"request": ranking_request, "owner": "detail"}),
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
        ("boundaries.list", {"boundaries": list(frame_boundaries)}),
        ("boundaries.length", {"boundaries": (("boot-a", 4 * NS, "gui"),)}),
        ("boundaries.boot", {"boundaries": (("", 4 * NS, "gui", "iod"),)}),
        ("boundaries.negative", {"boundaries": (("boot-a", -1, "gui", "iod"),)}),
        ("boundaries.bool", {"boundaries": (("boot-a", True, "gui", "iod"),)}),
        ("boundaries.owner-enum", {"boundaries": (("boot-a", 4 * NS, "gui", "root"),)}),
        ("boundaries.same-owner", {"boundaries": (("boot-a", 4 * NS, "gui", "gui"),)}),
        ("boundaries.unsupported", {"boundaries": (("boot-a", 2 * NS, "gui", "iod"),)}),
        ("boundaries.other-boot", {"boundaries": (("other", 4 * NS, "gui", "iod"),)}),
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
    # A boundary needs both adjacent *proven* segments; one unproven side fails.
    _reject(
        "local_frame.boundary-unproven",
        lambda: LocalFrameOutcome(
            2, ranking_request, (total(7, KEY),),
            (HistorySegment("local", "gui", "boot-a", 0, 4 * NS, True),
             HistorySegment("unproved", "unknown", "boot-a", 4 * NS, 8 * NS, False)),
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
        entered = threading.Event()
        token = RequestToken(
            11, QueryRequest("totals", scope, ALPHA), "local",
            retention_scope(ALPHA, scope), "detail", 1, None,
            ("local", (("local", "gui", 1),)),
            LocalPositionVector((LocalPosition("local", "gui", 1, 1, 0),)),
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
        release.set()
        settled_token, settlement = router._completed.get(timeout=5)
        assert settled_token is token
        assert isinstance(settlement, LocalSettlement)
        assert settlement.worker_id == token.token_id
        assert isinstance(settlement.outcome, LocalPartOutcome)
        assert settlement.outcome.value.window == scope
        # A done callback never releases charged capacity; only an owner drain does.
        assert router._background_charge("detail") == 1
    finally:
        release.set()
        if router is not None:
            router.close()
        if root is not None:
            root.connection.close()
        local.connection.close()
