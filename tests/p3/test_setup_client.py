"""The GUI's only elevation path: one fixed argv, a bounded decode, one operation.

The client never builds a command string, never accepts a path, and never
reports a state it did not decode or probe. A dismissal keeps the exact exit code
and first stderr line so the switch can revert with evidence instead of a promise.
"""

from __future__ import annotations

from concurrent.futures import Future
import json
import os

import pytest

from appio.collector_owner import ServiceFacts
from appio.setup_boundary import CommandResult, VERBS
from appio.setup_client import MAX_STDERR_BYTES, MAX_STDOUT_BYTES, SetupClient, SetupReply, decode_body

OFF = ServiceFacts(False, "inactive", False, False, False)


class FakeLaunch:
    """Records every argv and hands back a future the test completes by hand."""

    def __init__(self, *, result: CommandResult | None = None, pending: bool = False):
        self.calls: list[tuple[str, ...]] = []
        self.pending: list[Future] = []
        self.result = result
        self._pending = pending

    def __call__(self, argv: tuple[str, ...]) -> Future:
        self.calls.append(argv)
        done: Future = Future()
        if self._pending:
            self.pending.append(done)
            return done
        assert self.result is not None
        done.set_result(self.result)
        return done


def _body(**overrides: object) -> str:
    document: dict[str, object] = {
        "version": 1,
        "verb": "enable",
        "uid": 1000,
        "enabled": True,
        "answering": True,
        "pending_activation": True,
        "state": "on, needs activation",
        "export_path": None,
        "export_cutoff": None,
        "error": None,
        "sample_period": None,
        "sample_period_state": None,
    }
    document.update(overrides)
    return json.dumps(document, separators=(",", ":"))


def _client(launch, *, facts: ServiceFacts = OFF, expected_uid: int | None = 1000) -> SetupClient:
    return SetupClient(launch=launch, probe=lambda: facts, expected_uid=expected_uid)


def test_polkit_dismissal_retains_exact_code_stderr_and_probed_off_state():
    calls = []
    def launch(argv):
        calls.append(argv)
        done = Future()
        done.set_result(CommandResult(126, "", "Authentication dismissed\nextra"))
        return done
    client = SetupClient(launch=launch,
        probe=lambda: ServiceFacts(False, "inactive", False, False, False))
    result = client.invoke("enable").result(timeout=1)
    assert calls == [("/usr/bin/pkexec", "/usr/bin/appio-setup", "enable")]
    assert result.exit_code == 126
    assert result.stderr_first_line == "Authentication dismissed"
    assert client.refresh().enabled is False


def test_existing_verbs_keep_legacy_vectors_and_period_verb_carries_payload():
    launch = FakeLaunch(result=CommandResult(0, _body(), ""))
    client = _client(launch)
    for verb in sorted(VERBS - {"set-sample-period"}):
        assert client.invoke(verb).result(timeout=1).exit_code == 0
    assert client.invoke("set-sample-period", period_seconds=30).result(timeout=1).exit_code == 0
    assert launch.calls == [
        ("/usr/bin/pkexec", "/usr/bin/appio-setup", verb)
        for verb in sorted(VERBS - {"set-sample-period"})
    ] + [("/usr/bin/pkexec", "/usr/bin/appio-setup", "set-sample-period", "30")]


def test_period_payload_pairing_refuses_before_launch():
    launch = FakeLaunch(result=CommandResult(0, _body(), ""))
    client = _client(launch)
    with pytest.raises(ValueError):
        client.invoke("set-sample-period")
    with pytest.raises(ValueError):
        client.invoke("enable", period_seconds=10)
    assert launch.calls == []


def test_decode_body_validates_period_result_fields():
    accepted = _body(verb="set-sample-period", sample_period=10, sample_period_state="applied")
    assert decode_body(accepted, "set-sample-period", 1000) is not None
    for overrides in (
        {"sample_period": True},
        {"sample_period": 1},
        {"sample_period": 31},
        {"sample_period_state": "bad"},
        {"sample_period_state": 1},
    ):
        assert decode_body(_body(verb="set-sample-period", **overrides), "set-sample-period", 1000) is None


@pytest.mark.parametrize(
    "verb",
    ["", "shell", "enable --root /tmp", "/etc/passwd", "status\n", "enable\x00", "Export-history", " enable"],
)
def test_a_verb_outside_the_fixed_set_is_refused_before_any_launch(verb):
    launch = FakeLaunch(result=CommandResult(0, "", ""))
    client = _client(launch)
    with pytest.raises(ValueError):
        client.invoke(verb)
    assert launch.calls == []


def test_a_decoded_body_must_agree_with_version_verb_and_uid():
    accept = _client(FakeLaunch(result=CommandResult(0, _body(), "")))
    reply = accept.invoke("enable").result(timeout=1)
    assert isinstance(reply, SetupReply)
    assert reply.body is not None
    assert reply.body["state"] == "on, needs activation"
    assert reply.body["export_path"] is None

    rejected = {
        "version": _body(version=2),
        "verb": _body(verb="disable"),
        "uid": _body(uid=1001),
        "missing-uid": json.dumps({"version": 1, "verb": "enable"}, separators=(",", ":")),
        "not-an-object": "[1,2,3]",
        "trailing-garbage": _body() + " {}",
        "duplicate-verb": '{"version":1,"verb":"enable","verb":"disable","uid":1000}',
    }
    for label, payload in rejected.items():
        client = _client(FakeLaunch(result=CommandResult(0, payload, "")))
        reply = client.invoke("enable").result(timeout=1)
        assert reply.body is None, label
        assert reply.exit_code == 0
    assert rejected  # every case above is a distinct consistency rule


def test_an_oversized_stdout_is_not_decoded_but_the_exit_status_survives():
    huge = '{"version":1,"verb":"enable","uid":1000,"state":"off","pad":"' + "x" * MAX_STDOUT_BYTES + '"}'
    client = _client(FakeLaunch(result=CommandResult(3, huge, "too big")))
    reply = client.invoke("enable").result(timeout=1)
    assert reply.body is None
    assert reply.exit_code == 3
    assert reply.stderr_first_line == "too big"


def test_stderr_is_bounded_and_reports_the_first_real_line_only():
    noisy = "\n\nAuthentication dismissed\nsecond line\n" + "x" * (4 * MAX_STDERR_BYTES)
    client = _client(FakeLaunch(result=CommandResult(126, "", noisy)))
    reply = client.invoke("enable").result(timeout=1)
    assert reply.stderr_first_line == "Authentication dismissed"
    assert len(reply.stderr_first_line) <= MAX_STDERR_BYTES

    blank = _client(FakeLaunch(result=CommandResult(1, "", "\n\n\n")))
    assert blank.invoke("enable").result(timeout=1).stderr_first_line == ""


def test_refresh_probes_without_ever_elevating():
    launch = FakeLaunch(result=CommandResult(0, "", ""))
    client = _client(launch, facts=ServiceFacts(True, "active", True, True, False))
    assert client.refresh().enabled is True
    assert launch.calls == []


def test_a_probe_must_return_service_facts_rather_than_a_lookalike():
    client = SetupClient(launch=FakeLaunch(result=CommandResult(0, "", "")),
                         probe=lambda: {"enabled": True}, expected_uid=1000)
    with pytest.raises(TypeError):
        client.refresh()


def test_a_second_operation_is_rejected_while_one_is_pending():
    launch = FakeLaunch(pending=True)
    client = _client(launch)
    first = client.invoke("install-delegation")
    with pytest.raises(RuntimeError):
        client.invoke("enable")
    assert launch.calls == [("/usr/bin/pkexec", "/usr/bin/appio-setup", "install-delegation")]
    launch.pending[0].set_result(CommandResult(0, "", ""))
    assert first.result(timeout=1).exit_code == 0
    second = client.invoke("enable")
    assert len(launch.calls) == 2
    launch.pending[1].set_result(CommandResult(0, "", ""))
    assert second.result(timeout=1).exit_code == 0


def test_invoke_returns_before_the_helper_finishes():
    launch = FakeLaunch(pending=True)
    client = _client(launch)
    reply = client.invoke("status")
    assert reply.done() is False
    launch.pending[0].set_result(CommandResult(0, _body(verb="status"), ""))
    assert reply.result(timeout=1).body["verb"] == "status"


def test_a_worker_failure_reaches_the_caller_and_releases_the_slot():
    launch = FakeLaunch(pending=True)
    client = _client(launch)
    reply = client.invoke("enable")
    launch.pending[0].set_exception(OSError("worker died"))
    with pytest.raises(OSError):
        reply.result(timeout=1)
    assert client.invoke("status").done() is False
    launch.pending[1].set_result(CommandResult(0, "", ""))
    assert len(launch.calls) == 2


def test_the_expected_uid_defaults_to_the_process_uid():
    client = SetupClient(launch=FakeLaunch(result=CommandResult(0, _body(uid=os.getuid()), "")),
                         probe=lambda: OFF)
    assert client.invoke("enable").result(timeout=1).body is not None
    mismatch = SetupClient(launch=FakeLaunch(result=CommandResult(0, _body(uid=os.getuid() + 1), "")),
                           probe=lambda: OFF)
    assert mismatch.invoke("enable").result(timeout=1).body is None
