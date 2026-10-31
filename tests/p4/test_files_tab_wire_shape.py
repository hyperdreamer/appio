"""Pin the literal nested ``FileCoverage`` transport inventories (spec §2.3-2.5, §8).

The durable/IPC field sets below are copied **literally** from specification
§2.5. They are the required test data: they must not be derived from
``appio.iod_history._RECORDS``, ``dataclasses.fields()``, or the encoder's own
output, otherwise a changed encoder would silently redefine its own expectation.

Every assertion here is a *mechanism* pin, and each named probe was falsified
against a production mutation (applied only in a disposable copy of the tree):

* ``M_wire_encodes_property`` (encoder injects the computed ``served_interval``)
  must fail ``test_literal_file_wire_sets_and_round_trip`` at the literal
  top-level set comparison.
* ``M_decode_relax_<Class>`` (decoder accepts a wrong key set for exactly one
  record class) must fail ``test_each_nested_record_rejects_extra_and_missing_fields``
  for every path that reaches that class.
* ``M_served_interval_is_a_field`` (the computed property becomes a dataclass
  field) must fail the local field enumeration in
  ``test_local_field_enumeration_excludes_the_computed_property``.
* ``M_property_overwrites_requested_window`` (the property rewrites
  ``FileCoverage.window`` with the served bounds, which §2.3 forbids) must fail
  the same test's equality/enumeration checks.

No test opens a database, loads BPF, reads ``/proc`` or touches the network.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, replace
import json

import pytest

from appio.contracts import Window
from appio.iod_history import from_wire, to_wire
from tests.p4.test_files_view import BOOT, file_coverage

HOUR = 3_600_000_000_000
APP_KEY = "unit:app-demo.service"

# --------------------------------------------------------------------------- #
# Literal expectations, copied from specification §2.5 (do not derive)
# --------------------------------------------------------------------------- #

WINDOW_FIELDS = {
    "record", "kind", "boot_id", "start_mono_ns", "end_mono_ns",
}
HOOK_HITS_FIELDS = {
    "record", "vfs_read", "vfs_write", "vfs_readv", "vfs_writev",
    "vfs_iter_read", "vfs_iter_write",
}
COVERAGE_FIELDS = {
    "record", "tier", "complete", "unavailable_reason", "inaccessible_pids",
    "resets", "reap_corrections", "reap_unapplied", "reap_unobserved",
    "partial_intervals", "gap_intervals", "overlap_uncertain_intervals",
    "spans_suspend_intervals", "spans_suspend", "reap_incomplete",
    "counter_overflow", "device_state", "counted_omitted_files",
    "counted_omitted_bytes_r", "counted_omitted_bytes_w", "uncounted_overflow_files",
    "uncounted_overflow_bytes_r", "uncounted_overflow_bytes_w", "skipped_nonfile",
    "skipped_unsupported_fs", "weak_identity_rows", "hook_hits",
}
UNKNOWN_OWNER_FIELDS = {
    "record", "window", "owner_key", "overflow_files", "overflow_bytes_r",
    "overflow_bytes_w", "coverage",
}
FILE_COVERAGE_FIELDS = {
    "record", "window", "counted_omitted_files", "counted_omitted_bytes_r",
    "counted_omitted_bytes_w", "uncounted_overflow_files",
    "uncounted_overflow_bytes_r", "uncounted_overflow_bytes_w", "skipped_nonfile",
    "skipped_unsupported_fs", "weak_identity_rows", "hook_hits", "observed_bytes_r",
    "observed_bytes_w", "unknown_owner_overflow", "coverage", "app_key",
}

# The local (non-transported) field names §2.5 says must stay unchanged.
LOCAL_FILE_COVERAGE_FIELDS = FILE_COVERAGE_FIELDS - {"record", "app_key"} | {"app_id"}

# Each record tag drives decoder dispatch, so the literal value is part of the pin.
RECORD_NAMES = {
    "FILE_COVERAGE_FIELDS": "FileCoverage",
    "WINDOW_FIELDS": "Window",
    "COVERAGE_FIELDS": "Coverage",
    "HOOK_HITS_FIELDS": "HookHits",
    "UNKNOWN_OWNER_FIELDS": "UnknownOwnerOverflow",
}

# Paths into the encoded tree. ``hook_hits`` appears both directly on
# ``FileCoverage`` and nested inside the two coverage records.
PATHS = (
    ((), "FILE_COVERAGE_FIELDS"),
    (("window",), "WINDOW_FIELDS"),
    (("coverage",), "COVERAGE_FIELDS"),
    (("hook_hits",), "HOOK_HITS_FIELDS"),
    (("coverage", "hook_hits"), "HOOK_HITS_FIELDS"),
    (("unknown_owner_overflow",), "UNKNOWN_OWNER_FIELDS"),
    (("unknown_owner_overflow", "window"), "WINDOW_FIELDS"),
    (("unknown_owner_overflow", "coverage"), "COVERAGE_FIELDS"),
    (("unknown_owner_overflow", "coverage", "hook_hits"), "HOOK_HITS_FIELDS"),
)

SERVED_KEYS = ("served_interval", "served_gap_count", "gaps_in_served_interval")

REQUESTED = Window(kind="range", boot_id=BOOT, start_mono_ns=0, end_mono_ns=6 * HOUR)
SERVED = Window(kind="range", boot_id=BOOT, start_mono_ns=2 * HOUR, end_mono_ns=4 * HOUR)


def at(value: object, path: tuple[str, ...]) -> object:
    for key in path:
        value = value[key]  # type: ignore[index]
    return value


def encoded(cov=None) -> dict:
    value = file_coverage() if cov is None else cov
    return deepcopy(to_wire(value, app_keys={1: APP_KEY}))


def test_literal_file_wire_sets_and_round_trip():
    cov = file_coverage()
    wire = to_wire(cov, app_keys={1: APP_KEY})
    for path, name in PATHS:
        node = at(wire, path)
        assert isinstance(node, dict)
        assert set(node) == globals()[name], f"{name} at {path!r}"
        assert node["record"] == RECORD_NAMES[name], f"record tag at {path!r}"
    # A computed property has no serialized representation (§2.3, §2.5).
    assert not any(key in repr(wire) for key in SERVED_KEYS)
    # Durable JSON only: the encoded tree must survive a literal JSON round trip.
    assert json.loads(json.dumps(wire)) == wire
    assert from_wire(wire, app_ids=lambda key: 1, result_kind="coverage") == cov


@pytest.mark.parametrize("path,name", PATHS, ids=[f"{p or 'root'}:{n}" for p, n in PATHS])
@pytest.mark.parametrize("operation", ["add", "remove"])
def test_each_nested_record_rejects_extra_and_missing_fields(path, name, operation):
    wire = encoded()
    node = at(wire, path)
    if operation == "add":
        node["served_gap_count"] = 0
    else:
        node.pop(sorted(globals()[name] - {"record"})[0])
    with pytest.raises(ValueError):
        from_wire(wire, app_ids=lambda key: 1, result_kind="coverage")


def test_local_field_enumeration_excludes_the_computed_property():
    cov = file_coverage(window=REQUESTED)
    shortened = replace(
        cov, unknown_owner_overflow=replace(cov.unknown_owner_overflow, window=SERVED)
    )
    # The courtesy carrier is read through the honest property name.
    assert shortened.served_interval == SERVED
    assert shortened.window == REQUESTED

    before = asdict(shortened)
    assert set(before) == LOCAL_FILE_COVERAGE_FIELDS
    assert "served_interval" not in before
    assert before["window"] == asdict(REQUESTED)
    assert {field.name for field in fields(shortened)} == LOCAL_FILE_COVERAGE_FIELDS

    # Property access must be a pure read: no field appears, changes value, or
    # overwrites the requested ``window`` with the served bounds.
    snapshot = deepcopy(shortened)
    assert shortened.served_interval == SERVED
    assert shortened.served_interval == SERVED
    assert shortened == snapshot
    assert shortened.window == REQUESTED
    assert asdict(shortened) == before
