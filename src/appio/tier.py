"""Pure window-to-source-tier selection for the AppIO query layer."""

from __future__ import annotations

from collections.abc import Mapping

from appio.contracts import (
    HISTORICAL_BOOT_PREFIX_MESSAGE,
    LAUNCH_WINDOW_MESSAGE,
    MAX_RANGE_NS,
    RANGE_TOO_LONG_MESSAGE,
    Tier,
    Window,
    WindowUnavailableError,
    validate_sqlite_integer,
    validate_window,
)


def select_tier(window: Window, available_cutoff: int | None) -> Tier:
    """Select the sole process source tier permitted for ``window``.

    ``available_cutoff`` is the durable end of collected evidence for the
    requested boot. Totals represent a since-boot window only when its end is
    exactly that cutoff; a longer historical prefix cannot be reconstructed.
    """
    validate_window(window)
    if available_cutoff is not None:
        validate_sqlite_integer(available_cutoff, "available_cutoff")
    if window.kind == "launch":
        raise WindowUnavailableError("launch_attribution", LAUNCH_WINDOW_MESSAGE)
    if window.kind == "lifetime":
        return "totals_only"
    if window.kind == "range":
        duration = window.end_mono_ns - window.start_mono_ns
        if duration > MAX_RANGE_NS:
            raise WindowUnavailableError("unsupported_window", RANGE_TOO_LONG_MESSAGE)
    else:
        duration = window.end_mono_ns
        if duration > MAX_RANGE_NS:
            if available_cutoff == window.end_mono_ns:
                return "totals_only"
            raise WindowUnavailableError("unsupported_window", HISTORICAL_BOOT_PREFIX_MESSAGE)
    if duration <= 24 * 60 * 60 * 1_000_000_000:
        return "raw"
    if duration <= 30 * 24 * 60 * 60 * 1_000_000_000:
        return "rollup_60s"
    return "rollup_3600s"


def validate_scope(window: Window, available_cutoff: int | None = None) -> Tier:
    """Validate a public query scope and return its selected source tier."""
    return select_tier(window, available_cutoff)


TIER_ORDER: tuple[Tier, ...] = ("raw", "rollup_60s", "rollup_3600s")


def fit_tier(tier: Tier, counts: Mapping[Tier, int], budget: int) -> Tier:
    """Widen ``tier`` until its retained row count fits ``budget``.

    The duration rule picks the finest tier that may answer a window; this second
    step only widens it when that tier holds more rows than one query may scan.
    ``counts`` may omit tiers whose count is unknown -- an unknown count is never
    a reason to substitute, so selection stops there. The chosen tier travels with
    the result, so a coarser source is disclosed rather than silently applied.
    """
    validate_sqlite_integer(budget, "budget")
    if budget <= 0:
        raise ValueError("budget must be positive")
    if tier not in TIER_ORDER:
        return tier
    retained = counts.get(tier, 0) > 0
    for candidate in TIER_ORDER[TIER_ORDER.index(tier):]:
        count = counts.get(candidate)
        if count is None:
            return candidate
        if count <= budget:
            if count == 0 and candidate != tier and retained:
                # An unmaterialized coarser tier is not a substitute: escalating into
                # it would report "nothing was observed" while raw rows are retained.
                return tier
            return candidate
    return tier


__all__ = ["TIER_ORDER", "fit_tier", "select_tier", "validate_scope"]
