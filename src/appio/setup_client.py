"""The GUI's only elevation path: one fixed argv, bounded decode, one at a time.

Nothing here builds a shell string, accepts a path or a username, or invents a
state. A helper reply is trusted only when its ``version``, ``verb`` and ``uid``
agree with the invocation; anything else degrades to ``body=None`` and the caller
falls back to its own unprivileged probe. The exit status and the first stderr
line are always preserved so a dismissed prompt can be reported exactly.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import json
import os
from typing import Callable

from .collector_owner import ServiceFacts
from .setup_boundary import CommandResult, VERBS

__all__ = [
    "HELPER_ARGV",
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "SetupClient",
    "SetupReply",
    "decode_body",
    "first_stderr_line",
]

#: The one elevation vector. The verb is appended by :meth:`SetupClient.invoke`;
#: no caller may supply any other element.
HELPER_ARGV: tuple[str, str] = ("/usr/bin/pkexec", "/usr/bin/appio-setup")

MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 4 * 1024

_BOOLEAN_OR_NONE = ("enabled", "answering", "pending_activation")
_TEXT_OR_NONE = ("export_path", "error")
_SAMPLE_PERIOD_STATES = frozenset(
    {"applied", "applied_not_running", "repair", "unchanged", "service_not_running"}
)
_RESULT_KEYS = frozenset(
    {
        "version",
        "verb",
        "uid",
        "enabled",
        "answering",
        "pending_activation",
        "state",
        "export_path",
        "export_cutoff",
        "error",
        "sample_period",
        "sample_period_state",
    }
)


@dataclass(frozen=True)
class SetupReply:
    """One helper invocation, decoded or not.

    ``body`` is ``None`` when the helper produced nothing trustworthy: no stdout,
    oversized stdout, invalid JSON, or a document whose version, verb or UID do
    not match this invocation. The exit status and stderr line are always real.
    """

    exit_code: int
    stderr_first_line: str
    body: dict | None


def first_stderr_line(text: str) -> str:
    """Return the first non-empty line, bounded, and never a fabricated one."""

    if not isinstance(text, str):
        raise TypeError("stderr must be text")
    bounded = text[:MAX_STDERR_BYTES]
    for line in bounded.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:MAX_STDERR_BYTES]
    return ""


def _optional_boolean(value: object, field: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise ValueError(f"{field} must be a boolean or null")


def decode_body(text: str, verb: str, expected_uid: int) -> dict | None:
    """Decode a helper result document, or return ``None`` and never guess.

    The document is trusted only when the helper's own claims agree with what
    this process asked for. A body that fails any check is discarded whole; a
    partially trusted document would let a stale or foreign result drive a
    switch.
    """

    if not isinstance(text, str) or not text:
        return None
    if len(text.encode("utf-8", "replace")) > MAX_STDOUT_BYTES:
        return None
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    if set(document) != _RESULT_KEYS:
        return None
    if document.get("version") != 1 or type(document.get("version")) is not int:
        return None
    if document.get("verb") != verb:
        return None
    uid = document.get("uid")
    if type(uid) is not int or uid != expected_uid:
        return None
    if not isinstance(document.get("state"), str):
        return None
    try:
        for field in _BOOLEAN_OR_NONE:
            _optional_boolean(document.get(field), field)
        for field in _TEXT_OR_NONE:
            value = document.get(field)
            if value is not None and not isinstance(value, str):
                return None
        sample_period = document.get("sample_period")
        if sample_period is not None:
            if type(sample_period) is not int or not 2 <= sample_period <= 30:
                return None
        sample_period_state = document.get("sample_period_state")
        if sample_period_state is not None and not isinstance(sample_period_state, str):
            return None
        if sample_period_state is not None and sample_period_state not in _SAMPLE_PERIOD_STATES:
            return None
        if verb != "set-sample-period" and (sample_period is not None or sample_period_state is not None):
            return None
        cutoff = document.get("export_cutoff")
        if cutoff is not None and not isinstance(cutoff, dict):
            return None
    except ValueError:
        return None
    return document


class SetupClient:
    """Serialize privileged helper invocations behind one fixed argv vector.

    ``launch`` runs one argv vector and returns a :class:`Future` of
    :class:`~appio.setup_boundary.CommandResult`; production runs a bounded
    subprocess worker, and a test completes the future by hand. ``probe`` is the
    unprivileged service/socket observation used for periodic refresh, so no
    timer ever raises an authentication prompt.
    """

    def __init__(
        self,
        *,
        launch: Callable[[tuple[str, ...]], Future],
        probe: Callable[[], ServiceFacts],
        expected_uid: int | None = None,
    ) -> None:
        if not callable(launch):
            raise TypeError("launch must be callable")
        if not callable(probe):
            raise TypeError("probe must be callable")
        uid = os.getuid() if expected_uid is None else expected_uid
        if type(uid) is not int or not 0 <= uid <= 2**32 - 2:
            raise ValueError("expected_uid must be a valid uid")
        self._launch = launch
        self._probe = probe
        self._expected_uid = uid
        self._pending: Future | None = None

    @property
    def pending(self) -> bool:
        return self._pending is not None and not self._pending.done()

    def invoke(self, verb: str, *, period_seconds: int | None = None) -> Future:
        """Run one fixed helper vector, with a payload only for the period verb."""

        if not isinstance(verb, str) or verb not in VERBS:
            raise ValueError("verb is not one of the fixed setup verbs")
        if verb == "set-sample-period":
            if type(period_seconds) is not int or not 2 <= period_seconds <= 30:
                raise ValueError("set-sample-period requires a period from 2 to 30 seconds")
        elif period_seconds is not None:
            raise ValueError("period_seconds is only valid for set-sample-period")
        if self.pending:
            raise RuntimeError("a setup operation is already in progress")
        reply: Future = Future()
        self._pending = reply
        payload = (str(period_seconds),) if verb == "set-sample-period" else ()
        try:
            result = self._launch(HELPER_ARGV + (verb,) + payload)
        except BaseException as error:  # a launcher that cannot even start
            reply.set_exception(error)
            return reply
        result.add_done_callback(
            lambda done: self._finish(reply, verb, period_seconds, done)
        )
        return reply

    def refresh(self) -> ServiceFacts:
        """Observe service state without elevating; never returns a lookalike."""

        facts = self._probe()
        if not isinstance(facts, ServiceFacts):
            raise TypeError("probe must return ServiceFacts")
        return facts

    def _finish(
        self, reply: Future, verb: str, period_seconds: int | None, done: Future
    ) -> None:
        if reply.done():  # the caller cancelled; still release the slot below
            self._release(reply)
            return
        try:
            result = done.result()
            if not isinstance(result, CommandResult):
                raise TypeError("launch must produce CommandResult")
            body = decode_body(result.stdout, verb, self._expected_uid)
            if period_seconds is not None and (
                body is None or body.get("sample_period") != period_seconds
            ):
                body = None
            reply.set_result(SetupReply(
                exit_code=result.returncode,
                stderr_first_line=first_stderr_line(result.stderr),
                body=body,
            ))
        except BaseException as error:
            if not reply.done():
                reply.set_exception(error)
        finally:
            self._release(reply)

    def _release(self, reply: Future) -> None:
        if self._pending is reply:
            self._pending = None
