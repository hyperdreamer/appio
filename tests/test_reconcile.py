"""Contract tests for floored rates and signed task/device reconciliation.

The equations are specification section 2.4: with signed arbitrary-precision
arithmetic, ``T = sum(D) + R`` and the read/write identities hold exactly. When
device coverage is unavailable or mixed, every aggregate D and residual value is
``None`` and task totals stay numeric.
"""

from __future__ import annotations

import random

import pytest

from appio.clock import SuspensionSet, attribute_interval
from appio.contracts import SQLITE_INT_MAX
from appio.reconcile import Reconciliation, rate_bps

SECOND = 1_000_000_000
MAX = SQLITE_INT_MAX


def test_rate_bps_returns_none_for_an_undefined_rate() -> None:
    assert rate_bps(40, None) is None  # baseline or unknown duration
    assert rate_bps(None, SECOND) is None  # no delta to rate
    assert rate_bps(40, 0) is None  # zero duration
    assert rate_bps(-1, 2 * SECOND) is None  # negative bytes
    assert rate_bps(40, -2 * SECOND) is None  # negative duration


def test_a_defined_floored_zero_rate_is_integer_zero_not_none() -> None:
    assert rate_bps(1, 2 * SECOND) == 0
    assert rate_bps(1, 2 * SECOND) is not None
    assert rate_bps(0, SECOND) == 0
    assert rate_bps(0, SECOND) is not None


def test_rate_bps_is_floor_of_bytes_times_one_second_over_duration() -> None:
    cases = [
        (40, 2 * SECOND),
        (20, 2 * SECOND),
        (60, 2 * SECOND),
        (7, 3 * SECOND),
        (2, 3 * SECOND),
        (1_000_000_000, 3 * SECOND),
        (MAX, 7 * SECOND),
        (0, 7 * SECOND),
    ]
    for delta_bytes, observed_ns in cases:
        expected = delta_bytes * 1_000_000_000 // observed_ns
        assert rate_bps(delta_bytes, observed_ns) == expected

    assert rate_bps(40, 2 * SECOND) == 20
    assert rate_bps(60, 2 * SECOND) == 30


def test_rate_bps_rejects_boolean_and_float_inputs() -> None:
    with pytest.raises(ValueError):
        rate_bps(True, SECOND)
    with pytest.raises(ValueError):
        rate_bps(40, 2.0)  # type: ignore[arg-type]


def test_suspend_spanning_rates_use_measured_active_duration() -> None:
    susp = SuspensionSet()
    susp.add(180 * SECOND, 660 * SECOND)

    evidence = attribute_interval(179 * SECOND, 661 * SECOND, susp)

    assert evidence.observed_ns == 2 * SECOND
    assert (
        rate_bps(40, evidence.observed_ns),
        rate_bps(20, evidence.observed_ns),
        rate_bps(60, evidence.observed_ns),
    ) == (20, 10, 30)
    # The naive elapsed denominator floors all three rates to zero.
    assert rate_bps(40, 661 * SECOND - 179 * SECOND) == 0


def test_signed_residuals_and_exact_identities() -> None:
    reconciliation = Reconciliation(
        task_read=200,
        task_write=100,
        task_total=300,
        device_read=170,
        device_write=120,
        device_total=290,
        device_state="available",
    )

    assert (
        reconciliation.residual_read,
        reconciliation.residual_write,
        reconciliation.residual_total,
    ) == (30, -20, 10)
    assert reconciliation.task_read == reconciliation.device_read + reconciliation.residual_read
    assert reconciliation.task_write == reconciliation.device_write + reconciliation.residual_write
    assert reconciliation.task_total == reconciliation.device_total + reconciliation.residual_total
    assert reconciliation.task_total == reconciliation.task_read + reconciliation.task_write
    assert reconciliation.residual_total == reconciliation.residual_read + reconciliation.residual_write


def test_zero_and_negative_residuals_are_sign_preserving() -> None:
    exact = Reconciliation(200, 100, 300, 200, 100, 300, device_state="available")
    assert (
        exact.residual_read,
        exact.residual_write,
        exact.residual_total,
    ) == (0, 0, 0)

    over_attributed = Reconciliation(100, 50, 150, 150, 100, 250, device_state="available")
    assert (
        over_attributed.residual_read,
        over_attributed.residual_write,
        over_attributed.residual_total,
    ) == (-50, -50, -100)
    assert over_attributed.task_read == over_attributed.device_read + over_attributed.residual_read


@pytest.mark.parametrize("device_state", ["not_delegated", "incomplete"])
def test_unavailable_device_state_nulls_device_and_residual_fields(device_state: str) -> None:
    reconciliation = Reconciliation(
        task_read=200,
        task_write=100,
        task_total=300,
        device_state=device_state,  # type: ignore[arg-type]
    )

    assert reconciliation.device_state == device_state
    assert reconciliation.device_read is None
    assert reconciliation.device_write is None
    assert reconciliation.device_total is None
    assert reconciliation.residual_read is None
    assert reconciliation.residual_write is None
    assert reconciliation.residual_total is None
    assert (reconciliation.task_read, reconciliation.task_write, reconciliation.task_total) == (
        200,
        100,
        300,
    )


def test_unavailable_device_state_refuses_numeric_device_bytes() -> None:
    # Zero is not a substitute for unavailable device attribution.
    with pytest.raises(ValueError):
        Reconciliation(200, 100, 300, 0, 0, 0, device_state="incomplete")
    with pytest.raises(ValueError):
        Reconciliation(
            200, 100, 300, None, None, None, 30, -20, 10, "not_delegated"
        )


def test_supplied_residuals_must_match_the_exact_equations() -> None:
    supplied = Reconciliation(200, 100, 300, 170, 120, 290, 30, -20, 10, "available")
    assert supplied.residual_total == 10

    with pytest.raises(ValueError):
        Reconciliation(200, 100, 300, 170, 120, 290, 30, -20, 9, "available")
    with pytest.raises(ValueError):
        Reconciliation(200, 100, 300, 170, 120, 290, 29, -20, 10, "available")


def test_reconciliation_rejects_inconsistent_or_out_of_range_inputs() -> None:
    with pytest.raises(ValueError):  # task total is not read + write
        Reconciliation(200, 100, 301, 170, 120, 290, device_state="available")
    with pytest.raises(ValueError):  # device total is not read + write
        Reconciliation(200, 100, 300, 170, 121, 290, device_state="available")
    with pytest.raises(ValueError):  # device total missing while available
        Reconciliation(200, 100, 300, 170, 120, device_state="available")
    with pytest.raises(ValueError):  # negative task counter
        Reconciliation(-1, 100, 99, device_state="incomplete")
    with pytest.raises(ValueError):  # boolean is not an integer counter
        Reconciliation(True, 100, 101, device_state="incomplete")
    with pytest.raises(ValueError):  # unknown device state
        Reconciliation(200, 100, 300, device_state="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError):  # counter above the SQLite range
        Reconciliation(MAX + 1, 0, MAX + 1, device_state="incomplete")


def test_reconciliation_equations_hold_for_signed_results() -> None:
    rng = random.Random(20261021)
    for _ in range(200):
        task_read = rng.randrange(0, MAX + 1)
        task_write = rng.randrange(0, MAX + 1 - task_read)
        device_read = rng.randrange(0, MAX + 1)
        device_write = rng.randrange(0, MAX + 1 - device_read)
        task_total = task_read + task_write
        device_total = device_read + device_write

        reconciliation = Reconciliation(
            task_read=task_read,
            task_write=task_write,
            task_total=task_total,
            device_read=device_read,
            device_write=device_write,
            device_total=device_total,
            device_state="available",
        )

        assert reconciliation.residual_read == task_read - device_read
        assert reconciliation.residual_write == task_write - device_write
        assert reconciliation.residual_total == task_total - device_total
        assert device_read + reconciliation.residual_read == task_read
        assert device_write + reconciliation.residual_write == task_write
        assert device_total + reconciliation.residual_total == task_total
        assert reconciliation.residual_read + reconciliation.residual_write == (
            reconciliation.residual_total
        )
