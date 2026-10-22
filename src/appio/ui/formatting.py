"""Display formatting for the thin application window.

Every helper is pure and deterministic so an offscreen test can pin exact text.
Unavailable values are rendered as the literal word ``unavailable``; they are
never coerced to ``0`` or to an empty string.
"""

from __future__ import annotations

from appio.contracts import MAX_RANGE_NS, Window

A22_LAUNCH_REASON = (
    "Launch window disabled: device and file activity are not stored per launch."
)
A22_RANGE_REASON = (
    "Ranges longer than 365 days are disabled: retained rollups cannot represent "
    "arbitrary ranges."
)
UNAVAILABLE = "unavailable"

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def _require_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def format_bytes(value: int) -> str:
    """Render an exact byte count with binary units and no float rounding."""

    value = _require_integer(value, "value")
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude < 1024:
        return f"{sign}{magnitude} B"

    unit = 0
    scale = 1
    while unit < len(_UNITS) - 1 and magnitude >= scale * 1024:
        scale *= 1024
        unit += 1

    tenths = (magnitude * 10) // scale
    whole, fraction = divmod(tenths, 10)
    if fraction:
        return f"{sign}{whole}.{fraction} {_UNITS[unit]}"
    return f"{sign}{whole} {_UNITS[unit]}"


def format_peak(value: int | None) -> str:
    """Render a bytes-per-second rate, or ``unavailable`` when undefined."""

    if value is None:
        return UNAVAILABLE
    value = _require_integer(value, "value")
    return f"{format_bytes(value)}/s"


def format_residual(value: int | None, complete: bool) -> str:
    """Render the signed accounting residual without substituting zero.

    ``None`` and incomplete device coverage both render ``unavailable``: the
    window never performs arithmetic on an unavailable device attribution.
    """

    if value is None or not complete:
        return UNAVAILABLE
    value = _require_integer(value, "value")
    if value < 0:
        return f"attributed exceeds task accounting by {format_bytes(-value)}"
    if value > 0:
        return f"+{format_bytes(value)}"
    return format_bytes(0)


def window_disabled_reason(window: Window) -> str | None:
    """Return the exact A22 reason for a disabled window, or ``None``."""

    if window.kind == "launch":
        return A22_LAUNCH_REASON
    if (
        window.kind == "range"
        and window.end_mono_ns - window.start_mono_ns > MAX_RANGE_NS
    ):
        return A22_RANGE_REASON
    return None


__all__ = [
    "A22_LAUNCH_REASON",
    "A22_RANGE_REASON",
    "UNAVAILABLE",
    "format_bytes",
    "format_peak",
    "format_residual",
    "window_disabled_reason",
]
