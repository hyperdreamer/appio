"""Neutral retained-answer and source records: exact fields, phases and digests.

The single required test on this module validates every §2 record field, the
PartState truth table, the canonical local-answer digest, and that importing the
neutral module stays free of the client, router, query service and Qt.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from appio.contracts import (
    DeviceShareRow,
    Window,
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
from live_overview_support import Clock, coverage, series, share, total

KEY = "unit:alpha.service"
OTHER_KEY = "unit:beta.service"
DIGEST = "a" * 64


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
    _reject(
        "record.ranking",
        lambda: RetainedRecord(
            a, AnswerProvenance("ranking", KEY, scope, since_boot, identity, 0, None, None)
        ),
    )
    _reject(
        "record.window-mismatch",
        lambda: RetainedRecord(share(7, range_w), provenance),
    )
    _reject(
        "record.provenance-type",
        lambda: RetainedRecord(a, {"kind": "totals"}),  # type: ignore[arg-type]
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
        ("requested.local-without-bracket", {"requested_source": tolerant}),
        ("answered.local-without-bracket", {"requested_source": remote, "answered_source": tolerant}),
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

    # -- canonical local-answer digest ------------------------------------- #
    base_digest = local_answer_digest("totals", KEY, since_boot, a)
    assert len(base_digest) == 64
    assert set(base_digest) <= set("0123456789abcdef")
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
