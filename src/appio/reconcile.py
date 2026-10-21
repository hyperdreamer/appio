"""Floored byte rates and signed task-minus-device reconciliation.

The equations are specification section 2.4 and are evaluated with signed Python
integers, so a negative residual means "attributed device bytes exceed task
accounting", never a wrapped or saturated counter. When device attribution is
unavailable or mixed, every aggregate ``D`` and residual value is ``None`` while
the task totals stay numeric; zero is never substituted for an unknown ``D``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from appio.contracts import DeviceState, validate_enum, validate_sqlite_integer

__all__ = ["Reconciliation", "rate_bps"]

_NS_PER_SECOND: Final = 1_000_000_000
_DEVICE_STATES: Final[tuple[str, ...]] = ("available", "not_delegated", "incomplete")


class _DeriveSentinel:
    """Marker meaning "derive this residual from the exact equations"."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<derive>"


_DERIVE: Final = _DeriveSentinel()


def rate_bps(delta_bytes: int | None, observed_ns: int | None) -> int | None:
    """Return ``floor(delta_bytes * 1_000_000_000 / observed_ns)`` or ``None``.

    ``None`` means the rate is undefined: a baseline (no interval evidence), an
    unknown duration, zero duration, or a negative input. A defined rate that floors
    to zero is the integer ``0``, which is a different fact from ``None``.
    """

    if delta_bytes is None or observed_ns is None:
        return None
    if isinstance(delta_bytes, bool) or not isinstance(delta_bytes, int):
        raise ValueError(
            f"delta_bytes must be an integer or None, got {type(delta_bytes).__name__}"
        )
    if isinstance(observed_ns, bool) or not isinstance(observed_ns, int):
        raise ValueError(
            f"observed_ns must be an integer or None, got {type(observed_ns).__name__}"
        )
    if delta_bytes < 0 or observed_ns <= 0:
        return None
    return (delta_bytes * _NS_PER_SECOND) // observed_ns


@dataclass(frozen=True)
class Reconciliation:
    """Signed task/device reconciliation for one covered half-open window.

    ``task_*`` values are always numeric. ``device_*`` and ``residual_*`` values are
    ``None`` unless ``device_state == "available"``. The three residual fields are
    derived from the exact equations when omitted; a supplied value is checked
    against the derived one rather than trusted.
    """

    task_read: int
    task_write: int
    task_total: int
    device_read: int | None = None
    device_write: int | None = None
    device_total: int | None = None
    residual_read: int | None = _DERIVE  # type: ignore[assignment]
    residual_write: int | None = _DERIVE  # type: ignore[assignment]
    residual_total: int | None = _DERIVE  # type: ignore[assignment]
    device_state: DeviceState = "available"

    def __post_init__(self) -> None:
        task_read = validate_sqlite_integer(self.task_read, "reconciliation.task_read")
        task_write = validate_sqlite_integer(self.task_write, "reconciliation.task_write")
        task_total = validate_sqlite_integer(self.task_total, "reconciliation.task_total")
        if task_total != task_read + task_write:
            raise ValueError(
                "reconciliation.task_total must equal task_read + task_write, "
                f"got {task_total} != {task_read} + {task_write}"
            )

        state = validate_enum(
            self.device_state, _DEVICE_STATES, "reconciliation.device_state"
        )

        if state != "available":
            for name in (
                "device_read",
                "device_write",
                "device_total",
                "residual_read",
                "residual_write",
                "residual_total",
            ):
                value = getattr(self, name)
                if value is not None and value is not _DERIVE:
                    raise ValueError(
                        f"reconciliation.{name} must be None when device_state is {state}"
                    )
                object.__setattr__(self, name, None)
            return

        for name in ("device_read", "device_write", "device_total"):
            if getattr(self, name) is None:
                raise ValueError(
                    f"reconciliation.{name} is required when device_state is available"
                )
        device_read = validate_sqlite_integer(
            self.device_read, "reconciliation.device_read"
        )
        device_write = validate_sqlite_integer(
            self.device_write, "reconciliation.device_write"
        )
        device_total = validate_sqlite_integer(
            self.device_total, "reconciliation.device_total"
        )
        if device_total != device_read + device_write:
            raise ValueError(
                "reconciliation.device_total must equal device_read + device_write, "
                f"got {device_total} != {device_read} + {device_write}"
            )

        derived = {
            "residual_read": task_read - device_read,
            "residual_write": task_write - device_write,
            "residual_total": task_total - device_total,
        }
        for name, value in derived.items():
            supplied = getattr(self, name)
            if supplied is not _DERIVE:
                if supplied is None:
                    raise ValueError(
                        f"reconciliation.{name} must be an integer when device_state is available"
                    )
                checked = validate_sqlite_integer(
                    supplied, f"reconciliation.{name}", nonnegative=False
                )
                if checked != value:
                    raise ValueError(
                        f"reconciliation.{name} must equal the exact equation result "
                        f"{value}, got {checked}"
                    )
            object.__setattr__(
                self, name, validate_sqlite_integer(value, f"reconciliation.{name}", nonnegative=False)
            )
