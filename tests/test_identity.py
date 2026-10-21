"""Tests for the ratified A07 AppKey grammar, encoding, and resolution order.

The precedence tests pin the signed-off order: deepest ``app-*`` unit, nearest
``app.slice`` ancestor, desktop id, absolute executable, session scope. The B8
session vectors are reproduced exactly.
"""

from __future__ import annotations

import pytest

from appio.identity import (
    deepest_candidate,
    encode_app_key,
    resolve_app,
    session_scope_key,
    validate_app_key,
)

CHROMIUM_CGROUP = (
    "/user.slice/user-1000.slice/user@1000.service/app.slice/app-chromium@fixture.service"
)
APP_SLICE_WORKER = "/user.slice/user-1000.slice/user@1000.service/app.slice/worker"
SESSION_WORKER = "/user.slice/session-7.scope/worker"
UNKNOWN_SCOPE = "scope:/session/unknown"


# --- encoding --------------------------------------------------------------


def test_encode_app_key_percent_encodes_literal_percent_and_unicode_bytes() -> None:
    assert encode_app_key("unit", "app-100%.service") == "unit:app-100%25.service"
    assert encode_app_key("unit", "café.service") == "unit:caf%C3%A9.service"
    assert encode_app_key("exe", "/usr/bin/naïve") == "exe:/usr/bin/na%C3%AFve"
    assert encode_app_key("exe", "/opt/My App/bin") == "exe:/opt/My%20App/bin"
    assert encode_app_key("unit", "app!x.service") == "unit:app%21x.service"


def test_encode_app_key_passes_every_safe_byte_through_unescaped() -> None:
    payload = "/Az09/._:@+-/az"

    key = encode_app_key("exe", payload)

    assert key == f"exe:{payload}"
    assert "%" not in key


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("unit", "app/child.service"),
        ("desktop", "org/example/App.desktop"),
        ("exe", "usr/bin/tool"),
        ("exe", "bin/tool"),
        ("scope", "session-7.scope/worker"),
        ("scope", "user.slice"),
        ("exe", ""),
        ("unit", ""),
        ("bogus", "/x"),
    ],
)
def test_encode_app_key_rejects_slashes_in_unit_and_relative_paths(
    kind: str, payload: str
) -> None:
    with pytest.raises(ValueError):
        encode_app_key(kind, payload)


def test_encode_app_key_preserves_case_and_unicode_spelling() -> None:
    assert (
        encode_app_key("unit", "App-Chromium@Fixture.service")
        == "unit:App-Chromium@Fixture.service"
    )
    assert encode_app_key("desktop", "Org.Example.App.desktop") == (
        "desktop:Org.Example.App.desktop"
    )
    assert encode_app_key("unit", "app-É.service") != encode_app_key("unit", "app-é.service")


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("unit", "app-chromium@fixture.service"),
        ("unit", "App-Case.Service"),
        ("desktop", "org.mozilla.firefox.desktop"),
        ("exe", "/usr/lib/chromium/chromium"),
        ("exe", "/opt/na%C3%AFve%20tool"),
        ("scope", "/user.slice/session-7.scope"),
        ("scope", "/session/unknown"),
        ("exe", "/usr/bin/100%25"),
    ],
)
def test_validate_app_key_accepts_canonical_keys_and_returns_them(
    kind: str, payload: str
) -> None:
    key = encode_app_key(kind, payload)

    assert validate_app_key(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "notakey",
        "unit:",
        "Unit:app.service",
        "unit:app/child.service",
        "desktop:org/example/App.desktop",
        "exe:usr/bin/tool",
        "scope:session-7.scope/worker",
        "exe:/usr/bin/raw space",
        "exe:/usr/bin/%41",  # safe byte unnecessarily escaped
        "exe:/usr/bin/%2f",  # lowercase hexadecimal and safe byte
        "exe:/usr/bin/%FF",  # not valid UTF-8
        "exe:/usr/bin/%C3",  # truncated UTF-8 sequence
        "exe:/usr/bin/%2",
        "unit:app%2Fchild.service",  # decoded slash forbidden in a unit
        "exe:/usr/bin/é",  # raw non-safe byte must be escaped
    ],
)
def test_validate_app_key_rejects_bad_grammar_and_escapes(key: str) -> None:
    with pytest.raises(ValueError):
        validate_app_key(key)


# --- resolution precedence -------------------------------------------------


def test_resolve_app_prefers_the_deepest_app_unit_over_every_lower_input() -> None:
    assert (
        resolve_app(
            CHROMIUM_CGROUP,
            "/usr/bin/firefox",
            "org.mozilla.firefox.desktop",
        )
        == "unit:app-chromium@fixture.service"
    )


def test_resolve_app_prefers_the_deeper_app_unit_over_a_shallower_one() -> None:
    cgroup = (
        "/user.slice/user-1000.slice/user@1000.service/app.slice/"
        "app-old.service/app-new.scope/worker"
    )

    assert resolve_app(cgroup, None, None) == "unit:app-new.scope"


def test_resolve_app_uses_app_slice_when_the_slice_is_the_cgroup_leaf() -> None:
    cgroup = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    assert resolve_app(cgroup, "/usr/bin/firefox", "org.mozilla.firefox.desktop") == (
        "unit:app.slice"
    )


@pytest.mark.parametrize(
    ("cgroup", "expected"),
    [
        (SESSION_WORKER, "scope:/user.slice/session-7.scope"),
        (APP_SLICE_WORKER, UNKNOWN_SCOPE),
        (None, UNKNOWN_SCOPE),
        ("session-7.scope/worker", UNKNOWN_SCOPE),
    ],
)
def test_resolve_app_reproduces_the_exact_b8_session_cases(
    cgroup: str | None, expected: str
) -> None:
    assert resolve_app(cgroup, None, None) == expected


def test_b8_app_slice_worker_falls_through_to_desktop_then_executable() -> None:
    assert resolve_app(APP_SLICE_WORKER, "/usr/bin/firefox", "org.mozilla.firefox.desktop") == (
        "desktop:org.mozilla.firefox.desktop"
    )
    assert resolve_app(APP_SLICE_WORKER, "/usr/bin/firefox", None) == "exe:/usr/bin/firefox"


def test_resolve_app_uses_a_valid_desktop_id_verbatim_before_the_executable() -> None:
    assert resolve_app(None, "/usr/bin/firefox", "Org.Example.App.desktop") == (
        "desktop:Org.Example.App.desktop"
    )


@pytest.mark.parametrize("desktop_id", ["", "org/example/App.desktop", None])
def test_resolve_app_skips_an_invalid_desktop_id_for_the_executable(
    desktop_id: str | None,
) -> None:
    assert resolve_app(None, "/usr/bin/firefox", desktop_id) == "exe:/usr/bin/firefox"


def test_resolve_app_executable_fallback_requires_an_absolute_target() -> None:
    assert resolve_app(None, "usr/bin/firefox", None) == UNKNOWN_SCOPE
    assert resolve_app(None, "/usr/bin/firefox", None) == "exe:/usr/bin/firefox"


def test_resolve_app_preserves_and_encodes_the_executable_target() -> None:
    assert resolve_app(None, "/opt/naïve tool", None) == "exe:/opt/na%C3%AFve%20tool"
    assert resolve_app(None, "/usr/lib/chromium/chromium", None) == (
        "exe:/usr/lib/chromium/chromium"
    )


def test_resolve_app_falls_back_to_the_session_scope() -> None:
    assert resolve_app(SESSION_WORKER, None, None) == "scope:/user.slice/session-7.scope"
    assert resolve_app(None, None, None) == UNKNOWN_SCOPE


# --- session scope ---------------------------------------------------------


def test_session_scope_key_selects_the_deepest_session_ancestor_prefix() -> None:
    assert session_scope_key("/user.slice/session-7.scope/session-9.scope/worker") == (
        "scope:/user.slice/session-7.scope/session-9.scope"
    )
    assert session_scope_key("/user.slice/session-7.scope/other.scope/worker") == (
        "scope:/user.slice/session-7.scope"
    )


@pytest.mark.parametrize(
    "cgroup",
    [
        None,
        "",
        "session-7.scope/worker",
        "/user.slice/other.scope/worker",
        "/user.slice/session-7.scope-not/worker",
    ],
)
def test_session_scope_key_returns_the_exact_fallback(cgroup: str | None) -> None:
    assert session_scope_key(cgroup) == UNKNOWN_SCOPE


# --- deterministic tie ordering -------------------------------------------


def test_deepest_candidate_orders_by_depth_then_bytewise_utf8_name() -> None:
    assert deepest_candidate(
        [(1, "app-z.service"), (2, "app-b.service"), (2, "app-a.service")]
    ) == (2, "app-a.service")


def test_tie_ordering_is_bytewise_and_never_case_folded() -> None:
    # 'B' (0x42) sorts before 'a' (0x61) in UTF-8 bytes; case folding would
    # invert this and silently merge two distinct systemd units.
    assert deepest_candidate([(1, "app-a.service"), (1, "app-B.service")]) == (
        1,
        "app-B.service",
    )
    assert deepest_candidate([(2, "app-É.service"), (2, "app-a.service")]) == (
        2,
        "app-a.service",
    )
    assert deepest_candidate([]) is None


def test_resolution_results_are_valid_app_keys() -> None:
    for key in (
        resolve_app(CHROMIUM_CGROUP, None, None),
        resolve_app(None, "/usr/bin/firefox", None),
        resolve_app(None, None, "org.example.App.desktop"),
        resolve_app(SESSION_WORKER, None, None),
        resolve_app(None, None, None),
    ):
        assert validate_app_key(key) == key
