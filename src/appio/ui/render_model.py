"""Pure render models for the appio console.

Every class here is immutable data and every builder is pure: this module
imports only the standard library, :mod:`appio.contracts` and the display
formatters. No Qt object, query, history handle, future or painter is stored,
and no unavailable input is ever normalised into a measured zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from appio.contracts import (
    AppTotal,
    Coverage,
    DeviceSeriesPoint,
    DiskShare,
    Series,
    SeriesPoint,
    Window,
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
MAX_DEVICE_LANES = 3
SHARE_TOLERANCE = 1e-12

_NANOSECONDS = 10**9


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


def _coverage_text(coverage: Coverage) -> str:
    """Render one source's coverage summary, never combining two sources."""

    state = "complete" if coverage.complete else "incomplete"
    reason = (
        f"; reason: {coverage.unavailable_reason}"
        if coverage.unavailable_reason is not None
        else ""
    )
    return (
        f"coverage: {state}; device attribution: {coverage.device_state}; "
        f"tier: {coverage.tier}; gaps: {coverage.gap_intervals}; "
        f"suspends: {coverage.spans_suspend}{reason}"
    )


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

    active: dict[str, list[int]] = {}
    for row in share.devices:
        if row.total_bytes <= 0:
            continue
        totals = active.setdefault(row.dev, [0, 0])
        totals[0] += row.rbytes
        totals[1] += row.wbytes
    ordered = sorted(
        active.items(),
        key=lambda item: (-(item[1][0] + item[1][1]), item[0].encode("utf-8")),
    )
    lanes: list[DeviceLaneModel] = []
    for dev, (read_bytes, write_bytes) in ordered[:MAX_DEVICE_LANES]:
        points = tuple(
            sorted(
                (point for point in series.device_points if point.dev == dev),
                key=lambda point: point.bucket_start_mono_ns,
            )
        )
        lanes.append(DeviceLaneModel(dev, points, read_bytes, write_bytes))
    more_devices = max(0, len(ordered) - MAX_DEVICE_LANES)
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

    # An object supplied together with its own error is a contradiction.
    if total is not None and ranking_reason is not None:
        raise ValueError("a ranking total cannot accompany a ranking error")
    if series is not None and series_reason is not None:
        raise ValueError("a series cannot accompany its error")
    if share is not None and share_reason is not None:
        raise ValueError("a share cannot accompany its error")

    if series is not None:
        if series.app_id != app_id:
            raise ValueError(
                f"series app_id {series.app_id} does not match subject {app_id}"
            )
        if series.window != window:
            raise ValueError("series window does not match the requested window")
    if share is not None:
        if share.app_id != app_id:
            raise ValueError(
                f"share app_id {share.app_id} does not match subject {app_id}"
            )
        if share.window != window:
            raise ValueError("share window does not match the requested window")

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
            COVERAGE_UNAVAILABLE,
            ranking_reason,
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
            COVERAGE_UNAVAILABLE,
            NOT_RANKED_REASON,
        )

    # KPIs: the share owns TOTAL/READ/WRITE/DEVICE, the ranking owns PEAK.
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

    device_lanes: tuple[DeviceLaneModel, ...] = ()
    more_devices = 0
    if (
        series is not None
        and share is not None
        and chronology_reason is None
        and share.coverage.device_state == "available"
        and series.coverage.device_state == "available"
        and share.device_total_bytes is not None
    ):
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
    coverage_line = _coverage_text(primary_coverage)

    detail_parts = [f"window: {kind}; tier: {primary_coverage.tier}"]
    if chronology_reason is not None:
        detail_parts.append(f"; series: {chronology_reason}")
    if share is None:
        share_unavailable = share_reason if share_reason is not None else SHARE_UNAVAILABLE_REASON
        detail_parts.append(f"; share: {share_unavailable}")
    else:
        share_unavailable = None
    for name, coverage in sources[1:]:
        if coverage != primary_coverage:
            detail_parts.append(f"; {name} coverage: {_coverage_text(coverage)}")

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
    )


__all__ = [
    "COVERAGE_UNAVAILABLE",
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
    "OverviewModel",
    "RankingModel",
    "RankingRow",
    "build_overview",
    "build_ranking",
    "scope_text",
]
