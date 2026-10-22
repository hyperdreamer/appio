"""Pure window-to-source-tier selection for the AppIO query layer."""

from __future__ import annotations

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


__all__ = ["select_tier", "validate_scope"]
