"""Pure render models for the appio console.

Every class here is immutable data and every builder is pure: this module
imports only the standard library, :mod:`appio.contracts`, the neutral
:mod:`appio.history_types` records/helpers and the display formatters. No Qt
object, query, history handle, future or painter is stored, and no unavailable
input is ever normalised into a measured zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

from appio.contracts import (
    AppTotal,
    Coverage,
    DeviceSeriesPoint,
    DeviceShareRow,
    DiskShare,
    Series,
    SeriesPoint,
    Window,
    validate_window,
)
from appio.history_types import (
    AnswerProvenance,
    PartPhase,
    SeriesMetadata,
    applied_endpoint,
    retention_scope,
)
from appio.ui.formatting import UNAVAILABLE, format_bytes, format_peak, format_residual

KPI_CAPTIONS: tuple[str, ...] = ("TOTAL", "READ", "WRITE", "PEAK", "DEVICE")
EMPTY_RANKING_TEXT = "no applications observed"
NOT_RANKED_REASON = "not in the process ranking"
SERIES_UNAVAILABLE_REASON = "series unavailable"
SHARE_UNAVAILABLE_REASON = "task/device split unavailable"
LIFETIME_CHRONOLOGY_REASON = "lifetime chronology unavailable"
RESIDUAL_NOTE = "difference between accounting points"
COVERAGE_UNAVAILABLE = "coverage: unavailable"
DEVICE_TOTAL_UNAVAILABLE_REASON = "device total unavailable"
DEVICE_ENDPOINTS_REASON = "device measurements have different endpoints"
MAX_DEVICE_LANES = 3
SHARE_TOLERANCE = 1e-12

_NANOSECONDS = 10**9

#: The fixed bucket duration every projected point of a tier carries.
_TIER_BUCKET_SECS: dict[str, int] = {
    "raw": 10,
    "rollup_60s": 60,
    "rollup_3600s": 3_600,
}


# --------------------------------------------------------------------------
# Shared validation helpers. Public validation raises ValueError, never
# asserts, and never accepts a boolean where an integer is required.
# --------------------------------------------------------------------------


def _require_int(value: object, field: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:  # pragma: no cover - defensive
        raise ValueError(f"{field} must be UTF-8 encodable") from error
    return value


def _require_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _identity_less_provenance(
    kind: str, app_key: str, measured: Window
) -> AnswerProvenance:
    """Provenance for an exact-window legacy value with unknown identity/stamp."""

    return AnswerProvenance(
        kind,  # type: ignore[arg-type]
        app_key,
        retention_scope(app_key, measured),
        measured,
        None,
        None,
        None,
        None,
    )


def _measured_window_text(measured: Window) -> str:
    """The exact provenance disclosure for one differing measured Window."""

    if measured.kind == "since_boot":
        return f"since_boot {measured.boot_id} through {measured.end_mono_ns} ns"
    if measured.kind == "range":
        return (
            f"range {measured.boot_id} "
            f"[{measured.start_mono_ns}, {measured.end_mono_ns}) ns"
        )
    if measured.kind == "lifetime":
        return "lifetime"
    # A22 refuses launch before admission; this stays a total, not a guess.
    return measured.kind


def _device_lane_reason(
    *,
    chronology_reason: str | None,
    share: DiskShare | None,
    share_unavailable: str | None,
    series_coverage: Coverage | None,
    series_window: Window | None,
) -> str | None:
    """The first applicable device-lane reason in the frozen §7.3 order."""

    if chronology_reason is not None:
        return chronology_reason
    if share is None:
        return (
            share_unavailable
            if share_unavailable is not None
            else SHARE_UNAVAILABLE_REASON
        )
    if share.coverage.device_state != "available":
        return share.coverage.unavailable_reason or share.coverage.device_state
    if share.device_total_bytes is None:
        return DEVICE_TOTAL_UNAVAILABLE_REASON
    if series_coverage is None or series_window is None:
        # Successful chronology always carries typed series metadata; without
        # it there is no second measurement to compare.
        return SERIES_UNAVAILABLE_REASON
    if series_coverage.device_state != "available":
        return series_coverage.unavailable_reason or series_coverage.device_state
    if applied_endpoint(share.window) != applied_endpoint(series_window):
        return DEVICE_ENDPOINTS_REASON
    return None


def _check_detail_window(
    measured: Window, requested: Window, app_key: str | None, name: str
) -> None:
    """Same requested retention scope, and never a newer measured endpoint."""

    if app_key is None:
        raise ValueError(
            f"the {name} measurement requires the selected external subject"
        )
    if retention_scope(app_key, measured) != retention_scope(app_key, requested):
        raise ValueError(
            f"the {name} measured window does not share the requested retention scope"
        )
    requested_end = applied_endpoint(requested)
    if requested_end is None:
        return
    measured_end = applied_endpoint(measured)
    if measured_end is not None and measured_end > requested_end:
        raise ValueError(
            f"the {name} measured detail cannot end after the requested window"
        )


def _check_detail_provenance(
    provenance: AnswerProvenance,
    kind: str,
    app_key: str | None,
    value_window: Window,
    requested: Window,
    name: str,
) -> None:
    if provenance.kind != kind:
        raise ValueError(f"{name} provenance must describe a {kind} value")
    if app_key is None or provenance.app_key != app_key:
        raise ValueError(
            f"{name} provenance must match the selected external subject"
        )
    if provenance.measured_window != value_window:
        raise ValueError(f"{name} provenance measured window must match its value")
    _check_detail_window(value_window, requested, app_key, name)


def _validate_projected_points(
    metadata: SeriesMetadata, points: Sequence[SeriesPoint]
) -> None:
    """Tier durations of the points a model actually projects."""

    if metadata.tier == "totals_only":
        if points:
            raise ValueError("a totals_only series projects no points")
        return
    duration = _TIER_BUCKET_SECS[metadata.tier]
    for point in points:
        if point.bucket_secs != duration:
            raise ValueError(
                "every projected point duration must match the series tier"
            )


def _optional_provenance(value: object, field: str) -> AnswerProvenance | None:
    if value is None:
        return None
    if not isinstance(value, AnswerProvenance):
        raise ValueError(f"{field} must be an AnswerProvenance or None")
    # Re-run the record's own validation instead of trusting forged fields.
    AnswerProvenance.__post_init__(value)
    return value


def _optional_metadata(value: object, field: str) -> SeriesMetadata | None:
    if value is None:
        return None
    if not isinstance(value, SeriesMetadata):
        raise ValueError(f"{field} must be a SeriesMetadata or None")
    SeriesMetadata.__post_init__(value)
    return value


def _requested_window(value: object, field: str) -> Window | None:
    if value is None:
        return None
    if not isinstance(value, Window):
        raise ValueError(f"{field} must be a Window or None")
    return validate_window(value)


def _require_share(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be between 0.0 and 1.0")
    return number


# --------------------------------------------------------------------------
# Frozen render dataclasses (spec §2.1).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RankingRow:
    """One ranking row: identity, formatted label and normalised share."""

    app_id: int
    app_key: str
    label: str
    total_bytes: int
    share: float

    def __post_init__(self) -> None:
        _require_int(self.app_id, "ranking_row.app_id", minimum=1)
        _require_text(self.app_key, "ranking_row.app_key")
        _require_text(self.label, "ranking_row.label")
        _require_int(self.total_bytes, "ranking_row.total_bytes", minimum=0)
        object.__setattr__(self, "share", _require_share(self.share, "ranking_row.share"))


@dataclass(frozen=True)
class RankingModel:
    """A complete ranking state: rows, empty success text, or an error."""

    rows: tuple[RankingRow, ...]
    empty_text: str | None
    unavailable: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.rows, tuple):
            raise ValueError("ranking.rows must be a tuple")
        for row in self.rows:
            if not isinstance(row, RankingRow):
                raise ValueError("ranking.rows must contain RankingRow values")
        empty_text = _require_optional_text(self.empty_text, "ranking.empty_text")
        unavailable = _require_optional_text(self.unavailable, "ranking.unavailable")

        seen_ids: set[int] = set()
        seen_keys: set[str] = set()
        for row in self.rows:
            if row.app_id in seen_ids:
                raise ValueError(f"duplicate ranking app_id {row.app_id}")
            if row.app_key in seen_keys:
                raise ValueError(f"duplicate ranking app_key {row.app_key}")
            seen_ids.add(row.app_id)
            seen_keys.add(row.app_key)

        order = [(-row.total_bytes, row.app_key.encode("utf-8")) for row in self.rows]
        if order != sorted(order):
            raise ValueError("ranking rows must be sorted by total then app_key")

        largest = self.rows[0].total_bytes if self.rows else 0
        for row in self.rows:
            expected = row.total_bytes / largest if largest else 0.0
            if abs(row.share - expected) > SHARE_TOLERANCE:
                raise ValueError(
                    "ranking share must equal total_bytes / largest total "
                    f"({row.share!r} != {expected!r})"
                )

        if self.rows:
            if empty_text is not None or unavailable is not None:
                raise ValueError("a populated ranking carries no state text")
            return
        if unavailable is not None:
            if empty_text is not None:
                raise ValueError("an unavailable ranking carries no empty text")
            return
        if empty_text != EMPTY_RANKING_TEXT:
            raise ValueError(
                f"an empty ranking must say {EMPTY_RANKING_TEXT!r}, got {empty_text!r}"
            )


@dataclass(frozen=True)
class Kpi:
    """One KPI caption and its already-formatted value."""

    caption: str
    text: str

    def __post_init__(self) -> None:
        if self.caption not in KPI_CAPTIONS:
            raise ValueError(f"kpi.caption must be one of {KPI_CAPTIONS}, got {self.caption!r}")
        _require_text(self.text, "kpi.text")


@dataclass(frozen=True)
class DeviceLaneModel:
    """One device lane: raw token, retained points and share totals."""

    dev: str
    points: tuple[DeviceSeriesPoint, ...]
    read_bytes: int
    write_bytes: int

    def __post_init__(self) -> None:
        _require_text(self.dev, "device_lane.dev")
        _require_int(self.read_bytes, "device_lane.read_bytes", minimum=0)
        _require_int(self.write_bytes, "device_lane.write_bytes", minimum=0)
        if not isinstance(self.points, tuple):
            raise ValueError("device_lane.points must be a tuple")
        previous_end: int | None = None
        for point in self.points:
            if not isinstance(point, DeviceSeriesPoint):
                raise ValueError("device_lane.points must contain DeviceSeriesPoint values")
            if point.dev != self.dev:
                raise ValueError(
                    f"device_lane.points device {point.dev!r} does not match {self.dev!r}"
                )
            start = point.bucket_start_mono_ns
            if previous_end is not None and start < previous_end:
                raise ValueError("device_lane.points must be ordered and non-overlapping")
            previous_end = start + point.bucket_secs * _NANOSECONDS


@dataclass(frozen=True)
class OverviewModel:
    """The whole Overview pane as data: KPIs, lanes, composition and text."""

    app_id: int
    kpis: tuple[Kpi, ...]
    task_points: tuple[SeriesPoint, ...]
    device_lanes: tuple[DeviceLaneModel, ...]
    more_devices: int
    series_unavailable: str | None
    share: DiskShare | None
    share_unavailable: str | None
    reconciliation: str | None
    coverage_line: str
    detail_line: str
    coverage_diagnostic: str | None = None
    ranking_provenance: AnswerProvenance | None = None
    series_provenance: AnswerProvenance | None = None
    share_provenance: AnswerProvenance | None = None
    device_lanes_unavailable: str | None = None
    requested_window: Window | None = None
    app_key: str | None = None
    series_metadata: SeriesMetadata | None = None

    def __post_init__(self) -> None:
        _require_int(self.app_id, "overview.app_id", minimum=1)
        if not isinstance(self.kpis, tuple):
            raise ValueError("overview.kpis must be a tuple")
        for kpi in self.kpis:
            if not isinstance(kpi, Kpi):
                raise ValueError("overview.kpis must contain Kpi values")
        if tuple(kpi.caption for kpi in self.kpis) != KPI_CAPTIONS:
            raise ValueError(
                f"overview.kpis must be exactly {KPI_CAPTIONS}, "
                f"got {tuple(kpi.caption for kpi in self.kpis)}"
            )

        if not isinstance(self.task_points, tuple):
            raise ValueError("overview.task_points must be a tuple")
        previous_end: int | None = None
        for point in self.task_points:
            if not isinstance(point, SeriesPoint):
                raise ValueError("overview.task_points must contain SeriesPoint values")
            start = point.bucket_start_mono_ns
            if previous_end is not None and start < previous_end:
                raise ValueError("overview.task_points must be ordered and non-overlapping")
            previous_end = start + point.bucket_secs * _NANOSECONDS

        if not isinstance(self.device_lanes, tuple):
            raise ValueError("overview.device_lanes must be a tuple")
        if len(self.device_lanes) > MAX_DEVICE_LANES:
            raise ValueError(f"overview.device_lanes allows at most {MAX_DEVICE_LANES} lanes")
        seen_devices: set[str] = set()
        for lane in self.device_lanes:
            if not isinstance(lane, DeviceLaneModel):
                raise ValueError("overview.device_lanes must contain DeviceLaneModel values")
            if lane.dev in seen_devices:
                raise ValueError(f"duplicate overview device lane {lane.dev}")
            seen_devices.add(lane.dev)
        lane_order = [
            (-(lane.read_bytes + lane.write_bytes), lane.dev.encode("utf-8"))
            for lane in self.device_lanes
        ]
        if lane_order != sorted(lane_order):
            raise ValueError("overview.device_lanes must be ordered by bytes then device")

        _require_int(self.more_devices, "overview.more_devices", minimum=0)
        if self.more_devices and len(self.device_lanes) != MAX_DEVICE_LANES:
            raise ValueError(
                "overview.more_devices requires exactly "
                f"{MAX_DEVICE_LANES} displayed lanes"
            )

        series_unavailable = _require_optional_text(
            self.series_unavailable, "overview.series_unavailable"
        )
        share_unavailable = _require_optional_text(
            self.share_unavailable, "overview.share_unavailable"
        )
        reconciliation = _require_optional_text(
            self.reconciliation, "overview.reconciliation"
        )
        _require_optional_text(self.coverage_diagnostic, "overview.coverage_diagnostic")

        if self.share is not None:
            if not isinstance(self.share, DiskShare):
                raise ValueError("overview.share must be a DiskShare or None")
            if self.share.app_id != self.app_id:
                raise ValueError(
                    f"overview.share app_id {self.share.app_id} does not match {self.app_id}"
                )
        if share_unavailable is not None and self.share is not None:
            raise ValueError("overview cannot carry a share and its error")
        if share_unavailable is not None and reconciliation is not None:
            raise ValueError("overview cannot reconcile an unavailable share")
        if series_unavailable is not None:
            if self.task_points or self.device_lanes or self.more_devices:
                raise ValueError("overview cannot carry chronology with an unavailable series")

        device_available = (
            self.share is not None
            and self.share.coverage.device_state == "available"
            and self.share.device_total_bytes is not None
        )
        if self.device_lanes and not device_available:
            raise ValueError("overview device lanes require available device attribution")
        if reconciliation is not None:
            if not device_available:
                raise ValueError("overview reconciliation requires available device totals")
            if self.share is None or self.share.residual_total is None:
                raise ValueError("overview reconciliation requires a defined residual")

        _require_text(self.coverage_line, "overview.coverage_line")
        _require_text(self.detail_line, "overview.detail_line")

        # -- measured provenance and typed series metadata (spec §7.1) -----
        ranking_provenance = _optional_provenance(
            self.ranking_provenance, "overview.ranking_provenance"
        )
        series_provenance = _optional_provenance(
            self.series_provenance, "overview.series_provenance"
        )
        share_provenance = _optional_provenance(
            self.share_provenance, "overview.share_provenance"
        )
        lane_reason = _require_optional_text(
            self.device_lanes_unavailable, "overview.device_lanes_unavailable"
        )
        requested_window = _requested_window(
            self.requested_window, "overview.requested_window"
        )
        model_key = _require_optional_text(self.app_key, "overview.app_key")
        series_metadata = _optional_metadata(
            self.series_metadata, "overview.series_metadata"
        )

        # Legacy direct mode is exactly the all-default new-field shape. Only
        # represented data can be checked; nothing absent is inferred.
        extended = any(
            value is not None
            for value in (
                ranking_provenance,
                series_provenance,
                share_provenance,
                requested_window,
                model_key,
                series_metadata,
            )
        )
        if lane_reason is not None and not extended:
            raise ValueError(
                "overview.device_lanes_unavailable requires the extended model"
            )
        if not extended:
            return

        # -- extended direct mode ------------------------------------------
        if requested_window is None:
            raise ValueError("an extended overview model requires its requested Window")
        accepted = (
            self.share is not None
            or series_metadata is not None
            or ranking_provenance is not None
            or series_provenance is not None
            or share_provenance is not None
        )
        if model_key is None and not accepted:
            # A frozen early (refusal or file-only) outcome: no external
            # subject, no accepted detail and deliberately no lane reason.
            if lane_reason is not None:
                raise ValueError(
                    "an early overview outcome carries no device lane reason"
                )
            if self.task_points:
                raise ValueError(
                    "an early overview outcome carries no task points"
                )
            if self.device_lanes or self.more_devices:
                raise ValueError(
                    "an early overview outcome carries no device lanes"
                )
            return
        if accepted and model_key is None:
            raise ValueError(
                "overview accepted data requires the selected external app_key"
            )

        if self.share is None:
            if share_provenance is not None:
                raise ValueError("a missing share cannot carry its provenance")
        else:
            if share_provenance is not None:
                _check_detail_provenance(
                    share_provenance,
                    "totals",
                    model_key,
                    self.share.window,
                    requested_window,
                    "share",
                )
            _check_detail_window(
                self.share.window, requested_window, model_key, "share"
            )

        # A failed series is not a successful effective Series; only the
        # lifetime chronology reason describes a value that was measured.
        if series_metadata is not None:
            if series_metadata.app_id != self.app_id:
                raise ValueError("series metadata app_id must match the overview app_id")
            if model_key is not None and series_metadata.app_key != model_key:
                raise ValueError(
                    "series metadata subject must match the overview subject"
                )
            if series_unavailable not in (None, LIFETIME_CHRONOLOGY_REASON):
                raise ValueError("a failed series cannot carry typed series metadata")
            _check_detail_window(
                series_metadata.measured_window, requested_window, model_key, "series"
            )
            _validate_projected_points(series_metadata, self.task_points)
        if series_provenance is not None:
            if series_metadata is None:
                raise ValueError("a series provenance requires typed series metadata")
            _check_detail_provenance(
                series_provenance,
                "series",
                model_key,
                series_metadata.measured_window,
                requested_window,
                "series",
            )
        if series_unavailable is None and series_metadata is None:
            raise ValueError(
                "successful series chronology requires typed series metadata"
            )

        # Points carry no per-point boot identity, so a lifetime measurement
        # cannot be drawn as one continuous task timeline (spec §7.1). The
        # metadata and provenance stay valid; only the projected points fail.
        if (
            requested_window.kind == "lifetime"
            and series_unavailable is None
            and self.task_points
        ):
            raise ValueError(
                "lifetime chronology cannot project task points"
            )

        if ranking_provenance is not None:
            if model_key is None or ranking_provenance.app_key != model_key:
                raise ValueError(
                    "ranking provenance must match the selected external subject"
                )
            if ranking_provenance.kind != "ranking":
                raise ValueError("ranking provenance must describe a ranking row")
            if ranking_provenance.measured_window != requested_window:
                raise ValueError(
                    "ranking provenance must be exactly at the requested window"
                )

        # The lane reason must be the first applicable prerequisite, and a
        # reason always means zero lanes; both are construction errors when
        # they disagree with the represented measurements.
        chronology_reason = series_unavailable
        if chronology_reason is None and requested_window.kind == "lifetime":
            chronology_reason = LIFETIME_CHRONOLOGY_REASON
        expected_reason = _device_lane_reason(
            chronology_reason=chronology_reason,
            share=self.share,
            share_unavailable=share_unavailable,
            series_coverage=(
                series_metadata.coverage if series_metadata is not None else None
            ),
            series_window=(
                series_metadata.measured_window
                if series_metadata is not None
                else None
            ),
        )
        if lane_reason != expected_reason:
            raise ValueError(
                "device lanes: the unavailable reason must be the first applicable reason"
            )
        if expected_reason is not None and (self.device_lanes or self.more_devices):
            raise ValueError(
                "an unavailable device lane stack carries no lanes or more count"
            )


# --------------------------------------------------------------------------
# Builders (spec §2.2).
# --------------------------------------------------------------------------


def scope_text(window: Window) -> str:
    """Return the exact ``Window.kind`` token used by scope labels."""

    if not isinstance(window, Window):
        raise ValueError("scope_text requires a Window")
    return window.kind


def build_ranking(
    rows: Sequence[AppTotal], *, unavailable: str | None = None
) -> RankingModel:
    """Build a deterministic ranking model from process totals."""

    reason = _require_optional_text(unavailable, "ranking.unavailable")
    ordered_input: list[AppTotal] = []
    for row in rows:
        if not isinstance(row, AppTotal):
            raise ValueError("build_ranking requires AppTotal rows")
        ordered_input.append(row)
    if reason is not None:
        if ordered_input:
            raise ValueError("a failed ranking cannot carry rows")
        return RankingModel((), None, reason)

    ordered = sorted(
        ordered_input, key=lambda row: (-row.total_bytes, row.app_key.encode("utf-8"))
    )
    largest = ordered[0].total_bytes if ordered else 0
    rendered = tuple(
        RankingRow(
            row.app_id,
            row.app_key,
            f"{row.name}  {format_bytes(row.total_bytes)}",
            row.total_bytes,
            row.total_bytes / largest if largest else 0.0,
        )
        for row in ordered
    )
    if not rendered:
        return RankingModel((), EMPTY_RANKING_TEXT, None)
    return RankingModel(rendered, None, None)


def _unavailable_kpis() -> tuple[Kpi, ...]:
    return tuple(Kpi(caption, UNAVAILABLE) for caption in KPI_CAPTIONS)


def _coverage_text(
    coverage: Coverage, coverage_diagnostic: str | None = None
) -> str:
    """Render one source's coverage summary and its separate client cause."""

    state = "complete" if coverage.complete else "incomplete"
    causes = []
    if coverage.unavailable_reason is not None:
        causes.append(coverage.unavailable_reason)
    diagnostic = _require_optional_text(
        coverage_diagnostic, "overview.coverage_diagnostic"
    )
    if diagnostic is not None:
        causes.append(diagnostic)
    reason = "" if not causes else f"; reason: {causes[0]}"
    if len(causes) > 1:
        reason += f"; {causes[1]}"
    return (
        f"coverage: {state}; device attribution: {coverage.device_state}; "
        f"tier: {coverage.tier}; gaps: {coverage.gap_intervals}; "
        f"suspends: {coverage.spans_suspend}{reason}"
    )


def _unavailable_coverage_text(coverage_diagnostic: str | None) -> str:
    if coverage_diagnostic is None:
        return COVERAGE_UNAVAILABLE
    return f"{COVERAGE_UNAVAILABLE}; reason: {coverage_diagnostic}"


def _reconciliation_text(share: DiskShare) -> str | None:
    """Render the signed accounting equation, or ``None`` when undefined."""

    if share.coverage.device_state != "available":
        return None
    if share.device_total_bytes is None or share.residual_total is None:
        return None
    residual = format_residual(share.residual_total, True)
    return (
        f"Residual: {residual} - {RESIDUAL_NOTE}; "
        f"{format_bytes(share.device_total_bytes)} attributed + "
        f"({format_bytes(share.residual_total)}) residual = "
        f"{format_bytes(share.task_total_bytes)} total"
    )


def _device_lanes(
    series: Series, share: DiskShare
) -> tuple[tuple[DeviceLaneModel, ...], int]:
    """Select up to three active device lanes ordered by attributable bytes."""

    ordered = sorted(
        (
            row
            for row in share.devices
            if row.total_bytes > 0
            and (share.window.boot_id is None or row.boot_id == share.window.boot_id)
        ),
        key=lambda row: (-(row.rbytes + row.wbytes), row.dev.encode("utf-8")),
    )
    seen: set[str] = set()
    selected: list[DeviceShareRow] = []
    for row in ordered:
        if row.dev in seen:
            continue
        seen.add(row.dev)
        selected.append(row)
    lanes: list[DeviceLaneModel] = []
    for row in selected[:MAX_DEVICE_LANES]:
        points = tuple(
            sorted(
                (point for point in series.device_points if point.dev == row.dev),
                key=lambda point: point.bucket_start_mono_ns,
            )
        )
        lanes.append(DeviceLaneModel(row.dev, points, row.rbytes, row.wbytes))
    more_devices = max(0, len(selected) - MAX_DEVICE_LANES)
    return tuple(lanes), more_devices


def build_overview(
    app_id: int,
    window: Window,
    total: AppTotal | None,
    series: Series | None,
    share: DiskShare | None,
    *,
    ranking_error: str | None = None,
    series_error: str | None = None,
    share_error: str | None = None,
    coverage_diagnostic: str | None = None,
    ranking_provenance: AnswerProvenance | None = None,
    series_provenance: AnswerProvenance | None = None,
    share_provenance: AnswerProvenance | None = None,
) -> OverviewModel:
    """Build the Overview pane from independently available query results."""

    app_id = _require_int(app_id, "overview.app_id", minimum=1)
    if not isinstance(window, Window):
        raise ValueError("build_overview requires a Window")
    if total is not None and not isinstance(total, AppTotal):
        raise ValueError("build_overview total must be an AppTotal or None")
    if series is not None and not isinstance(series, Series):
        raise ValueError("build_overview series must be a Series or None")
    if share is not None and not isinstance(share, DiskShare):
        raise ValueError("build_overview share must be a DiskShare or None")
    ranking_reason = _require_optional_text(ranking_error, "overview.ranking_error")
    series_reason = _require_optional_text(series_error, "overview.series_error")
    share_reason = _require_optional_text(share_error, "overview.share_error")
    diagnostic = _require_optional_text(
        coverage_diagnostic, "overview.coverage_diagnostic"
    )
    ranking_prov = _optional_provenance(
        ranking_provenance, "overview.ranking_provenance"
    )
    series_prov = _optional_provenance(
        series_provenance, "overview.series_provenance"
    )
    share_prov = _optional_provenance(share_provenance, "overview.share_provenance")

    # An object supplied together with its own error is a contradiction.
    if total is not None and ranking_reason is not None:
        raise ValueError("a ranking total cannot accompany a ranking error")
    if series is not None and series_reason is not None:
        raise ValueError("a series cannot accompany its error")
    if share is not None and share_reason is not None:
        raise ValueError("a share cannot accompany its error")

    # A dependency missing or failed cannot carry its provenance, and a
    # provenance alone cannot manufacture accepted data.
    if ranking_prov is not None and (total is None or ranking_reason is not None):
        raise ValueError("ranking provenance requires a successful ranking row")
    if series_prov is not None:
        if series is None:
            raise ValueError("series provenance cannot be supplied without its value")
        if series_reason is not None:
            raise ValueError("series provenance cannot accompany a series error")
    if share_prov is not None:
        if share is None:
            raise ValueError("share provenance cannot be supplied without its value")
        if share_reason is not None:
            raise ValueError("share provenance cannot accompany a share error")

    # Every supplied input describes the same presentation subject: a ranking
    # row for another ID cannot supply this subject's key, ranking provenance
    # or PEAK.
    if total is not None and total.app_id != app_id:
        raise ValueError(
            f"ranking total app_id {total.app_id} does not match subject {app_id}"
        )

    if series is not None:
        if series.app_id != app_id:
            raise ValueError(
                f"series app_id {series.app_id} does not match subject {app_id}"
            )
        if series_prov is None and series.window != window:
            raise ValueError(
                "a series without explicit provenance must carry the exact "
                "requested window"
            )
    if share is not None:
        if share.app_id != app_id:
            raise ValueError(
                f"share app_id {share.app_id} does not match subject {app_id}"
            )
        if share_prov is None and share.window != window:
            raise ValueError(
                "a share without explicit provenance must carry the exact "
                "requested window"
            )

    if total is None and (
        series is not None
        or share is not None
        or series_prov is not None
        or share_prov is not None
    ):
        raise ValueError("a file-only subject carries no accepted dependent data")

    kind = scope_text(window)

    # A ranking failure poisons every chart and takes precedence over the
    # file-only state: nothing about this subject was measured.
    if ranking_reason is not None:
        if series is not None or share is not None:
            raise ValueError("a ranking error cannot accompany a subject result")
        return OverviewModel(
            app_id,
            _unavailable_kpis(),
            (),
            (),
            0,
            ranking_reason,
            None,
            ranking_reason,
            None,
            _unavailable_coverage_text(diagnostic),
            ranking_reason,
            diagnostic,
            requested_window=window,
        )

    # A subject outside the process ranking (for example a file-only app).
    if total is None:
        return OverviewModel(
            app_id,
            _unavailable_kpis(),
            (),
            (),
            0,
            NOT_RANKED_REASON,
            None,
            NOT_RANKED_REASON,
            None,
            _unavailable_coverage_text(diagnostic),
            NOT_RANKED_REASON,
            diagnostic,
            requested_window=window,
        )

    # KPIs: the share owns TOTAL/READ/WRITE/DEVICE, the ranking owns PEAK.
    app_key = total.app_key
    if ranking_prov is None:
        ranking_prov = _identity_less_provenance("ranking", app_key, window)
    else:
        if ranking_prov.kind != "ranking":
            raise ValueError("ranking provenance must describe a ranking row")
        if ranking_prov.app_key != app_key:
            raise ValueError(
                "ranking provenance must match the selected external subject"
            )
        if ranking_prov.measured_window != window:
            raise ValueError(
                "ranking provenance must be exactly at the requested window"
            )

    if share is not None:
        if share_prov is None:
            share_prov = _identity_less_provenance("totals", app_key, share.window)
        _check_detail_provenance(
            share_prov, "totals", app_key, share.window, window, "share"
        )
    if series is not None:
        if series_prov is None:
            series_prov = _identity_less_provenance("series", app_key, series.window)
        _check_detail_provenance(
            series_prov, "series", app_key, series.window, window, "series"
        )
    series_metadata = (
        SeriesMetadata(
            series.app_id,
            app_key,
            series.window,
            series.tier,
            series.coverage,
        )
        if series is not None
        else None
    )

    kpis: tuple[Kpi, ...]
    if share is not None:
        device_defined = (
            share.coverage.device_state == "available"
            and share.device_total_bytes is not None
        )
        kpis = (
            Kpi("TOTAL", format_bytes(share.task_total_bytes)),
            Kpi("READ", format_bytes(share.task_read_bytes)),
            Kpi("WRITE", format_bytes(share.task_write_bytes)),
            Kpi("PEAK", format_peak(total.peak_total_bps)),
            Kpi(
                "DEVICE",
                format_bytes(share.device_total_bytes) if device_defined else UNAVAILABLE,
            ),
        )
    else:
        kpis = tuple(
            Kpi(caption, format_peak(total.peak_total_bps) if caption == "PEAK" else UNAVAILABLE)
            for caption in KPI_CAPTIONS
        )

    # Chronology availability. Points carry no per-point boot identity, so a
    # lifetime window cannot be drawn as one continuous trace.
    task_points: tuple[SeriesPoint, ...] = ()
    chronology_reason: str | None = None
    if series is None:
        chronology_reason = (
            series_reason if series_reason is not None else SERIES_UNAVAILABLE_REASON
        )
    elif kind == "lifetime":
        chronology_reason = LIFETIME_CHRONOLOGY_REASON
    else:
        task_points = series.points

    if series_metadata is not None:
        _validate_projected_points(series_metadata, task_points)

    if share is None:
        share_unavailable: str | None = (
            share_reason if share_reason is not None else SHARE_UNAVAILABLE_REASON
        )
    else:
        share_unavailable = None

    device_reason = _device_lane_reason(
        chronology_reason=chronology_reason,
        share=share,
        share_unavailable=share_unavailable,
        series_coverage=(
            series_metadata.coverage if series_metadata is not None else None
        ),
        series_window=(
            series_metadata.measured_window if series_metadata is not None else None
        ),
    )
    device_lanes: tuple[DeviceLaneModel, ...] = ()
    more_devices = 0
    if device_reason is None and series is not None and share is not None:
        device_lanes, more_devices = _device_lanes(series, share)

    reconciliation = _reconciliation_text(share) if share is not None else None

    # Coverage text comes from the first successful source; differing
    # successful coverage is described separately rather than merged.
    sources: list[tuple[str, Coverage]] = []
    if share is not None:
        sources.append(("share", share.coverage))
    if series is not None:
        sources.append(("series", series.coverage))
    sources.append(("ranking", total.coverage))
    primary_name, primary_coverage = sources[0]
    coverage_line = _coverage_text(primary_coverage, diagnostic)

    detail_parts = [f"window: {kind}; tier: {primary_coverage.tier}"]
    if chronology_reason is not None:
        detail_parts.append(f"; series: {chronology_reason}")
    if share is None:
        detail_parts.append(f"; share: {share_unavailable}")
    for name, coverage in sources[1:]:
        if coverage != primary_coverage:
            detail_parts.append(f"; {name} coverage: {_coverage_text(coverage)}")
    # Provenance disclosure, fixed order share then series, only when the
    # effective detail measures a different Window than the requested one.
    if share is not None and share.window != window:
        detail_parts.append(
            f"; share measured: {_measured_window_text(share.window)}"
        )
    if series is not None and series.window != window:
        detail_parts.append(
            f"; series measured: {_measured_window_text(series.window)}"
        )
    if device_reason is not None:
        detail_parts.append(f"; device lanes: {device_reason}")

    return OverviewModel(
        app_id,
        kpis,
        task_points,
        device_lanes,
        more_devices,
        chronology_reason,
        share,
        share_unavailable,
        reconciliation,
        coverage_line,
        "".join(detail_parts),
        diagnostic,
        ranking_provenance=ranking_prov,
        series_provenance=series_prov,
        share_provenance=share_prov,
        device_lanes_unavailable=device_reason,
        requested_window=window,
        app_key=app_key,
        series_metadata=series_metadata,
    )


# --------------------------------------------------------------------------
# UI-local presentation records and the pure status/age rules (spec §9).
# --------------------------------------------------------------------------

_PART_PHASES: tuple[str, ...] = ("absent", "pending", "ok", "failed")
_ANSWER_KIND_ORDER: tuple[str, ...] = ("ranking", "totals", "series")
_STATUS_ROWS: tuple[str, ...] = (
    "1", "2", "3", "4", "5a", "5b", "6a", "6b", "6c", "6d", "6e", "6f",
)
_REFUSAL_ORIGINS: tuple[str, ...] = ("shell_prepare", "direct_view")


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a bool")
    return value


def _require_phase(value: object, field: str) -> PartPhase:
    if value not in _PART_PHASES:
        raise ValueError(f"{field} must be one of {_PART_PHASES}, got {value!r}")
    return value  # type: ignore[return-value]


def _check_age_label(value: object) -> str:
    """The exact §9.3 label shapes; no guessed or collector-age wording."""

    text = _require_text(value, "overview_status_choice.age_text")
    if text == "age unknown":
        return text
    parts = text.split(" ")
    if (
        len(parts) != 5
        or parts[0] != "oldest"
        or parts[1] != "answer"
        or parts[4] != "ago"
    ):
        raise ValueError("age_text must be the exact oldest-answer label or age unknown")
    number, unit = parts[2], parts[3]
    if (
        not number.isascii()
        or not number.isdigit()
        or (len(number) > 1 and number[0] == "0")
    ):
        raise ValueError("an age label carries a canonical nonnegative integer")
    if unit not in ("s", "min", "h"):
        raise ValueError("an age label unit is exactly s, min or h")
    return text


@dataclass(frozen=True)
class MeasurementStatus:
    """One query-free observation of the displayed Overview measurements."""

    has_displayed_values: bool
    ranking_empty: bool
    share_state: PartPhase
    series_state: PartPhase
    share_reason: str | None
    series_reason: str | None
    displayed_answers: tuple[AnswerProvenance, ...]

    def __post_init__(self) -> None:
        _require_bool(self.has_displayed_values, "measurement.has_displayed_values")
        _require_bool(self.ranking_empty, "measurement.ranking_empty")
        share_state = _require_phase(self.share_state, "measurement.share_state")
        series_state = _require_phase(self.series_state, "measurement.series_state")
        share_reason = _require_optional_text(
            self.share_reason, "measurement.share_reason"
        )
        series_reason = _require_optional_text(
            self.series_reason, "measurement.series_reason"
        )
        if (share_state == "failed") != (share_reason is not None):
            raise ValueError(
                "measurement.share_reason is present exactly for a failed state"
            )
        if (series_state == "failed") != (series_reason is not None):
            raise ValueError(
                "measurement.series_reason is present exactly for a failed state"
            )
        if not isinstance(self.displayed_answers, tuple):
            raise ValueError("measurement.displayed_answers must be a tuple")
        previous = -1
        for answer in self.displayed_answers:
            if not isinstance(answer, AnswerProvenance):
                raise ValueError(
                    "measurement.displayed_answers must contain AnswerProvenance values"
                )
            AnswerProvenance.__post_init__(answer)
            order = _ANSWER_KIND_ORDER.index(answer.kind)
            if order <= previous:
                raise ValueError(
                    "measurement.displayed_answers must be distinct dependencies in "
                    "ranking, totals, series order"
                )
            previous = order


@dataclass(frozen=True)
class PresentationRefusal:
    """One typed refusal category for the Overview presentation."""

    origin: Literal["shell_prepare", "direct_view"]
    window: Window
    reason: str
    text: str

    def __post_init__(self) -> None:
        if self.origin not in _REFUSAL_ORIGINS:
            raise ValueError(
                f"presentation_refusal.origin must be one of {_REFUSAL_ORIGINS}"
            )
        if not isinstance(self.window, Window):
            raise ValueError("presentation_refusal.window must be a Window")
        validate_window(self.window)
        _require_text(self.reason, "presentation_refusal.reason")
        _require_text(self.text, "presentation_refusal.text")


@dataclass(frozen=True)
class OverviewStatusChoice:
    """The one selected status row, its text and the independent age text."""

    row: Literal[
        "1", "2", "3", "4", "5a", "5b", "6a", "6b", "6c", "6d", "6e", "6f"
    ]
    text: str
    age_text: str | None

    def __post_init__(self) -> None:
        if self.row not in _STATUS_ROWS:
            raise ValueError(f"overview_status_choice.row must be one of {_STATUS_ROWS}")
        if self.row == "6f":
            if self.text != "":
                raise ValueError("row 6f carries exactly the empty status text")
        else:
            _require_text(self.text, "overview_status_choice.text")
        if self.age_text is not None:
            _check_age_label(self.age_text)


def oldest_answer_age(
    displayed_answers: Sequence[AnswerProvenance], now_ns: int
) -> str | None:
    """The exact oldest displayed dependency age, or ``None``/``age unknown``."""

    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
        raise ValueError("oldest_answer_age requires a nonnegative integer now_ns")
    answers: list[AnswerProvenance] = []
    for answer in displayed_answers:
        if not isinstance(answer, AnswerProvenance):
            raise ValueError(
                "oldest_answer_age requires AnswerProvenance displayed answers"
            )
        AnswerProvenance.__post_init__(answer)
        answers.append(answer)
    if not answers:
        return None
    if any(
        answer.accepted_identity is None or answer.first_seen_at_ns is None
        for answer in answers
    ):
        return "age unknown"
    first_seen = min(
        answer.first_seen_at_ns
        for answer in answers
        if answer.first_seen_at_ns is not None
    )
    elapsed = max(0, now_ns - first_seen)
    # The threshold uses the raw elapsed nanoseconds; only then is the value
    # ceiling-divided in the chosen unit (spec §9.3).
    if elapsed < 90 * _NANOSECONDS:
        unit, label = _NANOSECONDS, "s"
    elif elapsed < 90 * 60 * _NANOSECONDS:
        unit, label = 60 * _NANOSECONDS, "min"
    else:
        unit, label = 3_600 * _NANOSECONDS, "h"
    value = (elapsed + unit - 1) // unit
    return f"oldest answer {value} {label} ago"


def _choose_overview_status(
    *,
    refusal: PresentationRefusal | None,
    prepare_error: str | None,
    busy: bool,
    retry_after: int | None,
    now_ns: int,
    has_frame: bool,
    has_target: bool,
    has_sample: bool,
    any_work: bool,
    view_state: str,
    measurement: MeasurementStatus,
    age_now_ns: int,
) -> OverviewStatusChoice:
    """The single ordered decision tree for Overview status (spec §9.2)."""

    if refusal is not None:
        if not isinstance(refusal, PresentationRefusal):
            raise ValueError("status.refusal must be a PresentationRefusal or None")
        PresentationRefusal.__post_init__(refusal)
    error = _require_optional_text(prepare_error, "status.prepare_error")
    busy = _require_bool(busy, "status.busy")
    has_frame = _require_bool(has_frame, "status.has_frame")
    has_target = _require_bool(has_target, "status.has_target")
    has_sample = _require_bool(has_sample, "status.has_sample")
    any_work = _require_bool(any_work, "status.any_work")
    now = _require_int(now_ns, "status.now_ns", minimum=0)
    retry = (
        None
        if retry_after is None
        else _require_int(retry_after, "status.retry_after", minimum=0)
    )
    state = _require_text(view_state, "status.view_state")
    if not isinstance(measurement, MeasurementStatus):
        raise ValueError("status.measurement must be a MeasurementStatus")
    MeasurementStatus.__post_init__(measurement)
    age_now = _require_int(age_now_ns, "status.age_now_ns", minimum=0)

    age = oldest_answer_age(measurement.displayed_answers, age_now)
    if refusal is not None:
        return OverviewStatusChoice("1", refusal.text, age)
    if error is not None:
        return OverviewStatusChoice("2", error, age)
    if busy:
        return OverviewStatusChoice(
            "3",
            f"history read has not completed ({state}); the reader is still busy",
            age,
        )
    if retry is not None and now < retry:
        n = (retry - now + 999_999_999) // 1_000_000_000
        return OverviewStatusChoice(
            "4", f"history read has not completed ({state}); retrying in {n} s", age
        )
    if not has_frame:
        if not has_target or not has_sample:
            return OverviewStatusChoice("5a", "waiting for the first sample", age)
        return OverviewStatusChoice("5b", f"loading history ({state})", age)
    if measurement.has_displayed_values:
        if any_work:
            return OverviewStatusChoice("6a", f"updating ({state})", age)
        return OverviewStatusChoice("6b", "live", age)
    if measurement.ranking_empty:
        return OverviewStatusChoice("6c", "no applications observed", age)
    if "pending" in (measurement.share_state, measurement.series_state):
        return OverviewStatusChoice("6d", f"loading measurements ({state})", age)
    for phase, reason in (
        (measurement.share_state, measurement.share_reason),
        (measurement.series_state, measurement.series_reason),
    ):
        if phase == "failed":
            return OverviewStatusChoice(
                "6e", f"measurements unavailable: {reason}", age
            )
    return OverviewStatusChoice("6f", "", age)


__all__ = [
    "COVERAGE_UNAVAILABLE",
    "DEVICE_ENDPOINTS_REASON",
    "DEVICE_TOTAL_UNAVAILABLE_REASON",
    "EMPTY_RANKING_TEXT",
    "KPI_CAPTIONS",
    "LIFETIME_CHRONOLOGY_REASON",
    "MAX_DEVICE_LANES",
    "NOT_RANKED_REASON",
    "RESIDUAL_NOTE",
    "SERIES_UNAVAILABLE_REASON",
    "SHARE_UNAVAILABLE_REASON",
    "DeviceLaneModel",
    "Kpi",
    "MeasurementStatus",
    "OverviewModel",
    "OverviewStatusChoice",
    "PresentationRefusal",
    "RankingModel",
    "RankingRow",
    "build_overview",
    "build_ranking",
    "oldest_answer_age",
    "scope_text",
]
