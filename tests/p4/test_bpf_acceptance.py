"""Opt-in privileged host acceptance for the P4 per-file eBPF producer.

This module is BOTH the acceptance harness and its test suite. It is the single
opt-in privileged step of the P4 plan: nothing here runs on the default suite,
and nothing here can be reached without a human setting ``APPIO_P4_BPF_ACCEPTANCE=1``.

WHY THIS EXISTS
---------------
Tasks T-001..T-006 proved the producer, the drain decoder, attribution, hourly
persistence, the daemon F runtime, and the Files view on synthetic vectors. They
cannot prove that *this* kernel accepts the object, that all six ``fexit`` hooks
attach, that a real regular-file workload lands in the right map key, or that the
assumed ``vfs_readv``/``vfs_writev`` prototypes are the kernel's own. Only a root
run on the host can, so that run is opt-in and never automatic.

THE ASSUMED SIGNATURES (read this before interpreting a verdict)
----------------------------------------------------------------
At T-001 the two vectored prototypes were **ASSUMED** from the x86_64 VFS:

    ssize_t vfs_readv(struct file *file, const struct iovec *vec,
                      unsigned long vlen, loff_t *pos, rwf_t flags)
    ssize_t vfs_writev(struct file *file, const struct iovec *vec,
                       unsigned long vlen, loff_t *pos, rwf_t flags)

Armed with ``BPF_PROG(fileio_vfs_readv, struct file *, const struct iovec *,
unsigned long, loff_t *, rwf_t, ssize_t ret)`` the fexit trampoline only works
when that guess matches the kernel's BTF. This acceptance run CONFIRMS or REFUTES
each of the six assumptions, and the evidence is the reading of
``/sys/kernel/btf/vmlinux``: the ``FUNC`` entry for the symbol, its
``FUNC_PROTO``, and every parameter's resolved type (``PTR``/``STRUCT``/
``TYPEDEF``/``INT`` with bit width). ``--check`` prints exactly that evidence and
needs no root; the attach bitmask in ``--run`` is the corroborating witness, not
the primary one.

A "CONFIRMED" verdict therefore means: *the assumed prototype equals the kernel's
BTF prototype for that symbol on this host, and all six hooks attached.* It never
means "every hook routes every operation": a hook with zero hits is reported as
``no observed hit``, which is not proof of no activity.

WHAT THIS RUN REPORTS
---------------------
1. attach: the HELLO bitmask and a boolean per hook (six are required);
2. per-hook hit counters after the controlled workload, per target cgroup;
3. every per-file record drained (dev/inode/generation/name/reads/writes/bytes)
   and every per-cgroup stats row (hook hits, overflow, skipped);
4. the reserved unknown-owner bucket (cgroup id 0) kept separate, never folded
   into an app's numbers;
5. the signature verdicts from BTF, including the two assumed vectored hooks;
6. on failure: the loader's bounded stderr (libbpf/verifier text), the framed
   HELLO/DRAIN diagnostic, its errno, ``unprivileged_bpf_disabled``, and the
   cgroup ``io.stat`` delta as IOAccounting corroboration.

THE OPT-IN, AND WHAT HAPPENS WITHOUT ROOT OR WITHOUT BTF
--------------------------------------------------------
``run_acceptance()`` evaluates the gate itself and raises ``AcceptanceRefused``
before any process, cgroup, or file work. The gate is, in order:

1. ``APPIO_P4_BPF_ACCEPTANCE`` must be exactly ``"1"`` -- otherwise: refuse.
2. effective UID must be 0 -- otherwise: refuse (this host has
   ``unprivileged_bpf_disabled=2``, so a non-root load is not a valid test).
3. ``/sys/kernel/btf/vmlinux`` must be readable and must contain all six target
   symbols -- otherwise: refuse, naming what is missing.

A refusal is always a **skip that says NOT PERFORMED**, never a pass. Under
pytest the privileged test reports ``SKIPPED ... acceptance NOT PERFORMED`` with
the gate's reason, so CI (and the normal 2085-test suite) can never turn a skipped
privileged run into green evidence.

COMMANDS A HUMAN RUNS
---------------------
Print this runbook (no gate, no host access)::

    python tests/p4/test_bpf_acceptance.py --procedure

Read the host prerequisites and the BTF signature evidence, no root, no load::

    APPIO_P4_BPF_ACCEPTANCE=1 python tests/p4/test_bpf_acceptance.py --check

Run the full privileged acceptance (builds, loads, attaches, workload, drains)::

    sudo APPIO_P4_BPF_ACCEPTANCE=1 \\
        /path/to/venv/bin/python tests/p4/test_bpf_acceptance.py --run

Machine-readable report: add ``--json``. Exit codes: ``0`` CONFIRMED, ``1``
REFUTED/failed, ``2`` refused by the gate (acceptance NOT PERFORMED). A scratch
directory on a block-backed filesystem can be pinned with
``APPIO_P4_BPF_ACCEPTANCE_DIR``.

Run the same thing through pytest, keeping the skip visible::

    sudo APPIO_P4_BPF_ACCEPTANCE=1 PYTHONDONTWRITEBYTECODE=1 \\
        QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 \\
        python -m pytest -q tests/p4/test_bpf_acceptance.py -rs

Confirm the default suite is untouched (rootless, no BPF, one explicit skip)::

    PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 \\
        python -m pytest -q tests/p4/test_bpf_acceptance.py -rs

EXPECTED OBSERVABLES FOR A CONFIRMED RUN
----------------------------------------
* ``signatures[vfs_readv].status == "CONFIRMED"`` and the same for
  ``vfs_writev`` (and the other four hooks).
* ``attach.hook_mask == 63`` (``0x3f``) with all six ``attach.hooks`` true.
* The controlled workload writes and reads a known payload through
  ``write``/``read``/``writev``/``readv``; the drained record for the isolated
  file has the same ``dev`` (``major:minor`` of the scratch filesystem), the same
  ``inode`` as ``os.stat``, the same ``basename``, and ``bytes_w``/``bytes_r``
  equal to the sum of the workload's positive return values. On ext2/3/4 and
  other ``FILEID_INO32_GEN`` export filesystems ``i_generation`` is also read
  independently from ``name_to_handle_at(2)`` and must match; elsewhere the
  report says ``generation_evidence: "unavailable"`` instead of pretending.
* ``reads``/``writes`` equal the number of positive operations per direction.
* A second, nonpositive-only workload pass (read at EOF returns 0; ``read`` on a
  directory fd returns ``-EISDIR``) adds no record and no bytes, and the following
  drain contains no record for the workload file. Each completed drain is followed
  by a count of both event and stats inner maps in both slots; its retired maps
  must have zero keys. The active slot can contain unrelated cgroup traffic.
* ``drains[0].drained_slot != drains[1].drained_slot`` with ``complete == 1``.
* ``skipped``/``overflow`` counters are reported per cgroup; with an isolated
  cgroup they must be zero, without one they may include unrelated session
  traffic and are reported as observed (never as app rows).
* ``diagnostics.verifier`` is empty on success and carries the libbpf/verifier
  text on failure, alongside ``diagnostics.errno``.

WHEN THE RUN FAILS (AND WHAT THE FAILURE LOOKS LIKE)
-----------------------------------------------------
A failed run is a result, not a harness error: the loader's bounded stderr, the
framed HELLO diagnostic, its errno, ``unprivileged_bpf_disabled`` and the
``io.stat`` delta all appear in the report, and the exit code is ``1``. When the
failure matches a defect this acceptance already knows about, the report names the
owning task instead of leaving a bare libbpf string. The first such entry was
found by the rootless dry run of this harness::

    libbpf: bpf_object_open_opts size (0) is too small

That is ``fileio_loader.c`` passing a zero-initialised ``bpf_object_open_opts`` to
``bpf_object__open_file()``; libbpf requires ``opts.sz`` (``LIBBPF_OPTS(...)``).
It is a **T-001-owned defect**: this acceptance reports it and never edits another
task's file, so on such a host the verdict is ``FAILED`` with that explanation and
no claim of confirmation.

SAFETY AND CLEANUP
------------------
All build output, the object, the loader, the payload file, logs, and the
temporary cgroup live under one temporary directory (or a temporary child cgroup)
and are removed in ``finally`` blocks. The loader is closed (which detaches every
link and closes the object) before the cgroup or directory is removed, and a
wall-clock watchdog kills the loader so a hung verifier cannot hang the run.

Default suite contract: importing this module performs no host access. Everything
privileged lives behind ``run_acceptance()``, which refuses unless the gate above
passes.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    # The CLI must exercise the checkout, never a stale system-wide ``appio``.
    # Under pytest ``pythonpath = ["src"]`` already provides this.
    sys.path.insert(0, str(_SRC_DIR))
    _installed = sys.modules.get("appio")
    if _installed is not None and not str(getattr(_installed, "__file__", "")).startswith(
        str(_SRC_DIR)
    ):
        for _name in [name for name in sys.modules if name == "appio" or name.startswith("appio.")]:
            del sys.modules[_name]

from appio.bpf_fileio import (
    ACK_RESPONSE,
    DIAGNOSTIC_CAP,
    DRAIN_RESPONSE,
    HELLO_RESPONSE,
    BpfFileSource,
    FileSourceError,
    StreamLoader,
)
from appio.durable import Receipt

ROOT = Path(__file__).resolve().parents[2]
BPF_DIR = ROOT / "appio_iod" / "bpf"
BPF_SOURCE = BPF_DIR / "fileio.bpf.c"
BTF_PATH = Path("/sys/kernel/btf/vmlinux")
OPT_IN_ENV = "APPIO_P4_BPF_ACCEPTANCE"
SCRATCH_ENV = "APPIO_P4_BPF_ACCEPTANCE_DIR"
HOOK_SYMBOLS = (
    "vfs_read",
    "vfs_write",
    "vfs_readv",
    "vfs_writev",
    "vfs_iter_read",
    "vfs_iter_write",
)
HOOK_MASK_ALL = 0x3F
REFUSAL_PREFIX = "APPIO P4 BPF acceptance NOT PERFORMED"


# ---------------------------------------------------------------------------
# Refusal text and the opt-in gate
# ---------------------------------------------------------------------------


class AcceptanceRefused(RuntimeError):
    """The acceptance was refused before any privileged action was taken."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AcceptanceError(RuntimeError):
    """A gate-passed acceptance that could not produce a trustworthy result."""


def _refusal(detail: str) -> str:
    return f"{REFUSAL_PREFIX}: {detail}"


def _bounded(text: object, cap: int = DIAGNOSTIC_CAP) -> str:
    value = str(text)
    return value if len(value) <= cap else f"{value[: cap - 1]}\u2026"


@dataclass(frozen=True)
class Gate:
    """The outcome of the opt-in gate. ``allowed`` is the only permission."""

    opted_in: bool
    effective_uid: int
    require_root: bool
    allowed: bool
    reason: str | None


def evaluate_gate(
    *,
    environ=None,
    effective_uid=None,
    btf_loader=None,
    require_root: bool = True,
) -> Gate:
    """Decide, without side effects, whether the privileged path may run.

    Order matters and is part of the contract: the opt-in variable is checked
    before anything reads the host, root before BTF, and BTF before any build,
    spawn, cgroup or file work. Each early refusal therefore also proves that the
    later, more invasive steps were never reached.
    """
    environ = os.environ if environ is None else environ
    uid = os.geteuid() if effective_uid is None else int(effective_uid)
    opted_in = environ.get(OPT_IN_ENV) == "1"

    if not opted_in:
        observed = environ.get(OPT_IN_ENV)
        return Gate(
            opted_in=False,
            effective_uid=uid,
            require_root=require_root,
            allowed=False,
            reason=_refusal(
                f'{OPT_IN_ENV} is not "1" (observed {observed!r}); privileged BPF '
                "acceptance is opt-in, so no object was built, loaded, attached, or "
                "drained and no workload ran"
            ),
        )
    if require_root and uid != 0:
        return Gate(
            opted_in=True,
            effective_uid=uid,
            require_root=require_root,
            allowed=False,
            reason=_refusal(
                f"effective uid is {uid}, not root; this host sets "
                "unprivileged_bpf_disabled=2, so a non-root load would not be a valid "
                "test and nothing was attempted"
            ),
        )

    loader = read_btf if btf_loader is None else btf_loader
    try:
        image = loader()
    except (OSError, BtfError) as failure:
        return Gate(
            opted_in=True,
            effective_uid=uid,
            require_root=require_root,
            allowed=False,
            reason=_refusal(
                f"{BTF_PATH} could not be read or parsed ({failure}); the assumed VFS "
                "prototypes cannot be confirmed without BTF"
            ),
        )

    missing = [symbol for symbol in HOOK_SYMBOLS if not image.has_function(symbol)]
    if missing:
        return Gate(
            opted_in=True,
            effective_uid=uid,
            require_root=require_root,
            allowed=False,
            reason=_refusal(
                f"{BTF_PATH} exposes no FUNC entry for {', '.join(missing)}; this "
                "kernel cannot attach all six fexit hooks, so the acceptance cannot "
                "be performed here and is NOT CONFIRMED"
            ),
        )
    return Gate(
        opted_in=True,
        effective_uid=uid,
        require_root=require_root,
        allowed=True,
        reason=None,
    )


# ---------------------------------------------------------------------------
# BTF reading: the evidence for ASSUMED -> CONFIRMED/REFUTED
# ---------------------------------------------------------------------------

BTF_MAGIC = 0xEB9F
_BTF_HEADER = struct.Struct("<HBBIIIII")
_KIND_INT = 1
_KIND_PTR = 2
_KIND_ARRAY = 3
_KIND_STRUCT = 4
_KIND_UNION = 5
_KIND_ENUM = 6
_KIND_TYPEDEF = 8
_KIND_VOLATILE = 9
_KIND_CONST = 10
_KIND_RESTRICT = 11
_KIND_FUNC = 12
_KIND_FUNC_PROTO = 13
_KIND_TYPE_TAG = 18
_KIND_NAMES = {
    0: "UNKN",
    1: "INT",
    2: "PTR",
    3: "ARRAY",
    4: "STRUCT",
    5: "UNION",
    6: "ENUM",
    7: "FWD",
    8: "TYPEDEF",
    9: "VOLATILE",
    10: "CONST",
    11: "RESTRICT",
    12: "FUNC",
    13: "FUNC_PROTO",
    14: "VAR",
    15: "DATASEC",
    16: "FLOAT",
    17: "DECL_TAG",
    18: "TYPE_TAG",
    19: "ENUM64",
}


class BtfError(ValueError):
    """The BTF blob could not be parsed as the kernel's own BTF."""


@dataclass(frozen=True)
class BtfType:
    type_id: int
    name: str
    kind: int
    vlen: int
    size_or_type: int
    payload: bytes


def _extra_size(kind: int, vlen: int) -> int:
    if kind in (_KIND_STRUCT, _KIND_UNION):  # btf_member
        return vlen * 12
    if kind == _KIND_ENUM:  # btf_enum
        return vlen * 8
    if kind == _KIND_FUNC_PROTO:  # btf_param
        return vlen * 8
    if kind == 15:  # DATASEC: btf_var_secinfo
        return vlen * 12
    if kind == 19:  # ENUM64
        return vlen * 12
    return {1: 4, 3: 12, 14: 4, 17: 4}.get(kind, 0)


class BtfImage:
    """A parsed BTF blob. Only what the acceptance needs is decoded.

    Type descriptors are canonical tuples, so a comparison is a value comparison:
    ``("ptr", ("typedef", "loff_t"))`` or ``("int", False, 64)``. Integer identity
    is signedness plus bit width (never the kernel's spelling of the builtin),
    type identity is struct/union/enum name plus the typedef/pointer nesting, and
    ``const``/``volatile``/``restrict`` qualifiers are erased because the kernel
    erases them too.
    """

    def __init__(self, types: dict[int, BtfType], strings: bytes) -> None:
        self.types = types
        self.strings = strings
        self._functions: dict[str, tuple[int, ...]] = {}
        for type_id, entry in types.items():
            if entry.kind == _KIND_FUNC:
                self._functions[entry.name] = self._functions.get(entry.name, ()) + (type_id,)

    @classmethod
    def parse(cls, blob: bytes) -> "BtfImage":
        if len(blob) < _BTF_HEADER.size:
            raise BtfError(f"blob is {len(blob)} bytes, shorter than the BTF header")
        magic, version, _flags, hdr_len, type_off, type_len, str_off, str_len = (
            _BTF_HEADER.unpack_from(blob, 0)
        )
        if magic != BTF_MAGIC:
            raise BtfError(f"bad BTF magic {magic:#06x}")
        if version != 1:
            raise BtfError(f"unsupported BTF version {version}")
        type_base = hdr_len + type_off
        str_base = hdr_len + str_off
        if type_base + type_len > len(blob) or str_base + str_len > len(blob):
            raise BtfError("BTF sections extend past the end of the blob")
        strings = blob[str_base : str_base + str_len]
        if strings and not strings.endswith(b"\0"):
            raise BtfError("BTF string section is not NUL-terminated")

        def name_at(offset: int) -> str:
            if offset == 0:
                return ""
            if offset >= len(strings):
                raise BtfError(f"string offset {offset} is outside the string section")
            end = strings.find(b"\0", offset)
            if end < 0:
                raise BtfError(f"string at offset {offset} has no terminator")
            return strings[offset:end].decode("utf-8", "replace")

        types: dict[int, BtfType] = {}
        cursor = type_base
        type_id = 1
        limit = type_base + type_len
        while cursor < limit:
            if cursor + 12 > limit:
                raise BtfError(f"type record {type_id} is truncated")
            name_off, info, size_or_type = struct.unpack_from("<III", blob, cursor)
            kind = (info >> 24) & 0x1F
            vlen = info & 0xFFFF
            end = cursor + 12 + _extra_size(kind, vlen)
            if end > limit:
                raise BtfError(f"type record {type_id} overruns the type section")
            types[type_id] = BtfType(
                type_id=type_id,
                name=name_at(name_off),
                kind=kind,
                vlen=vlen,
                size_or_type=size_or_type,
                payload=blob[cursor + 12 : end],
            )
            cursor = end
            type_id += 1
        if not types:
            raise BtfError("BTF contains no types")
        return cls(types, strings)

    @property
    def type_count(self) -> int:
        return len(self.types)

    def has_function(self, name: str) -> bool:
        return bool(self._functions.get(name))

    def function_ids(self, name: str) -> tuple[int, ...]:
        return self._functions.get(name, ())

    # -- descriptor resolution ------------------------------------------------

    def resolve(self, type_id: int, _seen: frozenset[int] = frozenset()) -> tuple:
        if type_id == 0:
            return ("void",)
        entry = self.types.get(type_id)
        if entry is None:
            return ("unresolved", f"absent type {type_id}")
        if type_id in _seen:
            return ("unresolved", f"cyclic type {type_id}")
        kind, name, inner = entry.kind, entry.name, entry.size_or_type
        if kind == _KIND_INT:
            encoding = entry.payload[0] if entry.payload else 0
            return ("int", bool(encoding & 0x01), inner * 8)
        if kind == _KIND_PTR:
            return ("ptr", self.resolve(inner, _seen | {type_id}))
        if kind == _KIND_ARRAY:
            return ("array", self.resolve(inner, _seen | {type_id}))
        if kind in (_KIND_STRUCT, _KIND_UNION):
            return ("struct" if kind == _KIND_STRUCT else "union", name or "<anonymous>")
        if kind == _KIND_ENUM:
            return ("enum", name or "<anonymous>")
        if kind == _KIND_TYPEDEF:
            return ("typedef", name) if name else self.resolve(inner, _seen | {type_id})
        if kind in (_KIND_CONST, _KIND_VOLATILE, _KIND_RESTRICT, _KIND_TYPE_TAG):
            return self.resolve(inner, _seen | {type_id})
        if kind == _KIND_FUNC_PROTO:
            return ("func_proto",)
        if kind == _KIND_FUNC:
            return ("func", name)
        return ("kind", _KIND_NAMES.get(kind, str(kind)), name)

    def _string(self, offset: int) -> str:
        if offset == 0:
            return ""
        end = self.strings.find(b"\0", offset)
        return self.strings[offset:end if end >= 0 else None].decode("utf-8", "replace")

    def prototype(self, function_id: int) -> tuple[tuple, tuple[tuple[str, tuple], ...]]:
        """Return ``(return_descriptor, ((param_name, descriptor), ...))``.

        Descriptors are the *display* form: typedef aliases and integer
        signedness are preserved so the report shows the kernel's own spelling.
        Use :meth:`abi` for the comparison form.
        """
        entry = self.types.get(function_id)
        if entry is None or entry.kind != _KIND_FUNC:
            raise BtfError(f"type {function_id} is not a FUNC")
        proto = self.types.get(entry.size_or_type)
        if proto is None or proto.kind != _KIND_FUNC_PROTO:
            raise BtfError(f"FUNC {entry.name} has no FUNC_PROTO")
        params: list[tuple[str, tuple]] = []
        for index in range(proto.vlen):
            name_off, param_type = struct.unpack_from("<II", proto.payload, index * 8)
            params.append((self._string(name_off), self.resolve(param_type)))
        return self.resolve(proto.size_or_type), tuple(params)

    def abi(self, type_id: int, _seen: frozenset[int] = frozenset()) -> tuple:
        """The trampoline-relevant shape of a type, for comparison.

        The kernel's fexit trampoline is generated from the parameter count and
        the argument widths, and it erases ``__user``/``const`` qualifiers. It can
        therefore spell a program's ``char *buf`` as ``unsigned char *buf`` and
        inline a ``size_t`` into ``long unsigned int`` without changing the ABI.
        Signedness of an equally wide integer and a typedef alias are consequently
        *notes*, while arity, integer width, pointer nesting, and struct/union/enum
        names are *differences* -- those are what actually break an attach or a
        CO-RE field read.
        """
        entry = self.types.get(type_id)
        seen = _seen | {type_id}
        while entry is not None and entry.kind in (
            _KIND_TYPEDEF,
            _KIND_CONST,
            _KIND_VOLATILE,
            _KIND_RESTRICT,
            _KIND_TYPE_TAG,
        ):
            inner = entry.size_or_type
            if inner in seen:
                return ("unresolved", f"cyclic type {type_id}")
            entry = self.types.get(inner)
            if entry is None:
                return ("unresolved", f"absent type {inner}")
            seen = seen | {inner}
        if entry is None:
            return ("absent",) if type_id else ("void",)
        kind, inner = entry.kind, entry.size_or_type
        if kind == _KIND_INT:
            return ("int", inner * 8)
        if kind == _KIND_PTR:
            return ("ptr", self.abi(inner, seen))
        if kind == _KIND_ARRAY:
            return ("array", self.abi(inner, seen))
        if kind in (_KIND_STRUCT, _KIND_UNION):
            return ("struct" if kind == _KIND_STRUCT else "union", entry.name or "<anonymous>")
        if kind == _KIND_ENUM:
            return ("enum", entry.name or "<anonymous>")
        if kind == _KIND_FUNC_PROTO:
            return ("func_proto",)
        if kind == _KIND_FUNC:
            return ("func", entry.name)
        return ("kind", _KIND_NAMES.get(kind, str(kind)))

    def abi_prototype(
        self, function_id: int
    ) -> tuple[tuple, tuple[tuple[str, tuple], ...]]:
        entry = self.types.get(function_id)
        if entry is None or entry.kind != _KIND_FUNC:
            raise BtfError(f"type {function_id} is not a FUNC")
        proto = self.types.get(entry.size_or_type)
        if proto is None or proto.kind != _KIND_FUNC_PROTO:
            raise BtfError(f"FUNC {entry.name} has no FUNC_PROTO")
        params: list[tuple[str, tuple]] = []
        for index in range(proto.vlen):
            name_off, param_type = struct.unpack_from("<II", proto.payload, index * 8)
            params.append((self._string(name_off), self.abi(param_type)))
        return self.abi(proto.size_or_type), tuple(params)

    # -- spelling ------------------------------------------------------------

    @staticmethod
    def spell(descriptor: tuple) -> str:
        head = descriptor[0]
        if head == "void":
            return "void"
        if head == "int":
            _head, signed, bits = descriptor
            table = {
                (True, 8): "char",
                (False, 8): "unsigned char",
                (True, 16): "short",
                (False, 16): "unsigned short",
                (True, 32): "int",
                (False, 32): "unsigned int",
                (True, 64): "long",
                (False, 64): "unsigned long",
            }
            return table.get(
                (signed, bits), f"{'signed' if signed else 'unsigned'} {bits}-bit int"
            )
        if head == "ptr":
            return f"{BtfImage.spell(descriptor[1])} *"
        if head in ("struct", "union", "enum"):
            return f"{head} {descriptor[1]}"
        if head == "typedef":
            return descriptor[1]
        if head == "array":
            return f"{BtfImage.spell(descriptor[1])}[]"
        if head == "func_proto":
            return "func_proto"
        if head == "func":
            return f"func {descriptor[1]}"
        return " ".join(str(part) for part in descriptor)


def read_btf(path: Path | str = BTF_PATH) -> BtfImage:
    """Read and parse ``/sys/kernel/btf/vmlinux``: the acceptance' primary evidence."""
    return BtfImage.parse(Path(path).read_bytes())


# ---------------------------------------------------------------------------
# The assumed T-001 prototypes, and the verdicts
# ---------------------------------------------------------------------------

_DESCRIPTOR_SPELLINGS: dict[str, tuple] = {
    "ssize_t": ("typedef", "ssize_t"),
    "size_t": ("typedef", "size_t"),
    "rwf_t": ("typedef", "rwf_t"),
    "loff_t *": ("ptr", ("typedef", "loff_t")),
    "unsigned long": ("int", False, 64),
    "struct file *": ("ptr", ("struct", "file")),
    "char *": ("ptr", ("int", True, 8)),
    "const char *": ("ptr", ("int", True, 8)),
    "const struct iovec *": ("ptr", ("struct", "iovec")),
    "struct iov_iter *": ("ptr", ("struct", "iov_iter")),
}


def _spelling(text: str) -> str:
    return re.sub(r"\s*\*\s*", " *", re.sub(r"\s+", " ", text.strip()))


def _descriptor(text: str) -> tuple:
    key = _spelling(text)
    try:
        return _DESCRIPTOR_SPELLINGS[key]
    except KeyError:  # pragma: no cover - guards a silent signature change
        raise AcceptanceError(
            f"unknown assumed C type spelling {key!r}; extend _DESCRIPTOR_SPELLINGS "
            "before trusting a verdict"
        ) from None


def _signature(return_spelling: str, params: tuple[str, ...]) -> tuple:
    return (_descriptor(return_spelling), tuple(_descriptor(param) for param in params))


#: Exactly the prototypes the six ``BPF_PROG`` declarations in ``fileio.bpf.c``
#: assume, with the fexit ``ssize_t ret`` sibling removed. The two vectored rows
#: are the ones T-001 ASSUMED rather than verified.
ASSUMED_SIGNATURES: dict[str, tuple] = {
    "vfs_read": _signature("ssize_t", ("struct file *", "char *", "size_t", "loff_t *")),
    "vfs_write": _signature(
        "ssize_t", ("struct file *", "const char *", "size_t", "loff_t *")
    ),
    "vfs_readv": _signature(
        "ssize_t",
        ("struct file *", "const struct iovec *", "unsigned long", "loff_t *", "rwf_t"),
    ),
    "vfs_writev": _signature(
        "ssize_t",
        ("struct file *", "const struct iovec *", "unsigned long", "loff_t *", "rwf_t"),
    ),
    "vfs_iter_read": _signature(
        "ssize_t", ("struct file *", "struct iov_iter *", "loff_t *", "rwf_t")
    ),
    "vfs_iter_write": _signature(
        "ssize_t", ("struct file *", "struct iov_iter *", "loff_t *", "rwf_t")
    ),
}

_SEC_PROGRAM = re.compile(
    r'SEC\("fexit/(?P<hook>[a-z_]+)"\)\s*int\s+BPF_PROG\((?P<program>\w+)\s*,(?P<params>[^)]*)\)',
    re.S,
)


def assumed_signatures_from_source(path: Path | str = BPF_SOURCE) -> dict[str, tuple]:
    """Recover the assumed prototypes from the shipped ``BPF_PROG`` declarations.

    This is what keeps :data:`ASSUMED_SIGNATURES` honest: if T-001's C declarations
    change, this parse changes with them and the rootless guard test fails until the
    table is updated.
    """
    text = Path(path).read_text(encoding="utf-8")
    found: dict[str, tuple] = {}
    for match in _SEC_PROGRAM.finditer(text):
        hook = match.group("hook")
        raw = [part.strip() for part in match.group("params").split(",") if part.strip()]
        parsed: list[tuple[str, str]] = []
        for part in raw:
            name_match = re.match(r"^(?P<type>.*?)(?P<name>[A-Za-z_]\w*)$", part)
            if name_match is None:
                raise AcceptanceError(f"cannot parse BPF_PROG parameter {part!r} for {hook}")
            parsed.append((name_match.group("name"), name_match.group("type")))
        if not parsed or parsed[-1][0] != "ret":
            raise AcceptanceError(f"fexit program for {hook} has no trailing 'ssize_t ret'")
        found[hook] = _signature(
            parsed[-1][1], tuple(param_type for _, param_type in parsed[:-1])
        )
    missing = [hook for hook in HOOK_SYMBOLS if hook not in found]
    if missing:
        raise AcceptanceError(f"{path} declares no fexit program for {', '.join(missing)}")
    return {hook: found[hook] for hook in HOOK_SYMBOLS}


@dataclass(frozen=True)
class SignatureVerdict:
    hook: str
    status: str
    kernel_return: str
    kernel_params: tuple
    assumed_return: str
    assumed_params: tuple
    evidence: str
    notes: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.status == "CONFIRMED"


_TYPEDEF_ABI: dict[str, tuple] = {
    "ssize_t": ("int", 64),
    "size_t": ("int", 64),
    "loff_t": ("int", 64),
    "rwf_t": ("int", 32),
}


def _abi_of_display(descriptor: tuple) -> tuple:
    """Reduce a display descriptor to the comparison form (see ``BtfImage.abi``)."""
    head = descriptor[0]
    if head == "int":
        return ("int", descriptor[2])
    if head == "typedef":
        return _TYPEDEF_ABI.get(descriptor[1], ("typedef", descriptor[1]))
    if head in ("ptr", "array"):
        return (head, _abi_of_display(descriptor[1]))
    return descriptor


def _format_prototype(ret: str, params: tuple[str, ...]) -> str:
    return f"{ret} ({', '.join(params)})" if params else f"{ret} (void)"


def _spelled(descriptors: tuple) -> tuple[str, ...]:
    return tuple(BtfImage.spell(descriptor) for descriptor in descriptors)


def signature_verdicts(
    image: BtfImage, assumed: dict[str, tuple] | None = None
) -> dict[str, SignatureVerdict]:
    """Compare the kernel's BTF prototypes with the T-001 assumptions.

    Comparison is on the trampoline-relevant shape (:meth:`BtfImage.abi`): arity,
    integer width, pointer nesting and struct/union names must match. A signedness
    or typedef-alias difference of the same width is ABI-equivalent, so it is
    reported as a *note* rather than a refutation -- the kernel spells a program's
    ``char *buf`` as ``unsigned char *buf`` in its own BTF. A symbol that is absent
    is ``UNVERIFIED``: it is not evidence for or against the assumption.
    """
    table = ASSUMED_SIGNATURES if assumed is None else assumed
    verdicts: dict[str, SignatureVerdict] = {}
    for hook in HOOK_SYMBOLS:
        assumed_return, assumed_params = table[hook]
        assumed_text = _format_prototype(
            BtfImage.spell(assumed_return),
            tuple(BtfImage.spell(param) for param in assumed_params),
        )
        assumed_abi_return = _abi_of_display(assumed_return)
        assumed_abi_params = tuple(_abi_of_display(param) for param in assumed_params)
        ids = image.function_ids(hook)
        if not ids:
            verdicts[hook] = SignatureVerdict(
                hook=hook,
                status="UNVERIFIED",
                kernel_return="",
                kernel_params=(),
                assumed_return=assumed_text,
                assumed_params=assumed_params,
                evidence=(
                    f"the symbol is absent from the parsed BTF (no FUNC named {hook!r}); "
                    f"the assumed prototype {assumed_text} is neither confirmed nor "
                    "refuted, and this kernel can never attach that fexit hook"
                ),
            )
            continue
        kernel_return, params = image.prototype(ids[0])
        kernel_params = tuple(descriptor for _name, descriptor in params)
        kernel_abi_return, kernel_abi = image.abi_prototype(ids[0])
        kernel_abi_params = tuple(descriptor for _name, descriptor in kernel_abi)
        kernel_text = _format_prototype(
            BtfImage.spell(kernel_return), _spelled(kernel_params)
        )
        proto_id = image.types[ids[0]].size_or_type
        differences = []
        for index, (kernel_abi, expected_abi) in enumerate(
            zip(kernel_abi_params, assumed_abi_params, strict=False)
        ):
            if kernel_abi == expected_abi:
                continue
            kernel_text_at = (
                BtfImage.spell(kernel_params[index])
                if index < len(kernel_params)
                else str(kernel_abi)
            )
            assumed_text_at = (
                BtfImage.spell(assumed_params[index])
                if index < len(assumed_params)
                else str(expected_abi)
            )
            differences.append(
                f"param {index}: kernel {kernel_text_at} != assumed {assumed_text_at}"
            )
        if len(kernel_abi_params) != len(assumed_abi_params):
            differences.append(
                f"parameter count: kernel {len(kernel_abi_params)} != assumed "
                f"{len(assumed_abi_params)}"
            )
        if kernel_abi_return != assumed_abi_return:
            differences.append(
                f"return: kernel {BtfImage.spell(kernel_return)} != assumed "
                f"{BtfImage.spell(assumed_return)}"
            )
        notes = [
            f"ABI-equivalent spelling at param {index}: kernel "
            f"{BtfImage.spell(kernel)} vs assumed {BtfImage.spell(expected)} "
            "(signedness or typedef alias only)"
            for index, (kernel, expected) in enumerate(
                zip(kernel_params, assumed_params, strict=False)
            )
            if kernel != expected
        ]
        if kernel_return != assumed_return and kernel_abi_return == assumed_abi_return:
            notes.append(
                f"ABI-equivalent spelling of the return type: kernel "
                f"{BtfImage.spell(kernel_return)} vs assumed "
                f"{BtfImage.spell(assumed_return)}"
            )
        status = "REFUTED" if differences else "CONFIRMED"
        detail = f"; {'; '.join(differences)}" if differences else ""
        note_text = f"; notes: {'; '.join(notes)}" if notes else ""
        verdicts[hook] = SignatureVerdict(
            hook=hook,
            status=status,
            kernel_return=kernel_text,
            kernel_params=kernel_params,
            assumed_return=assumed_text,
            assumed_params=assumed_params,
            evidence=(
                f"BTF FUNC#{ids[0]} {hook!r} -> FUNC_PROTO#{proto_id} with "
                f"{len(params)} parameters; kernel: {kernel_text}; assumed: "
                f"{assumed_text} -> {status}{detail}{note_text}"
            ),
            notes=tuple(notes),
        )
    return verdicts


# ---------------------------------------------------------------------------
# The controlled workload
# ---------------------------------------------------------------------------

_PAYLOAD = b"appio-p4-acceptance-payload-0123456789"

_WORKLOAD_SOURCE = r'''
import json, os, sys

path = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "full"
payload = bytes.fromhex(sys.argv[3])
result = {"mode": mode, "path": path, "payload_len": len(payload), "ops": []}


def record(name, call, detail=""):
    try:
        ret = call()
    except OSError as error:
        result["ops"].append({"op": name, "ret": None, "errno": error.errno, "detail": detail})
        return
    if isinstance(ret, (bytes, bytearray)):
        ret = len(ret)  # the syscall's return value, not the buffer
    result["ops"].append({"op": name, "ret": ret, "detail": detail})


sys.stdin.readline()  # wait for the parent's go-ahead (cgroup placement)
flags = os.O_RDWR | os.O_CREAT if mode == "full" else os.O_RDWR
fd = os.open(path, flags, 0o600)
try:
    if mode == "full":
        record("write", lambda: os.write(fd, payload))
        os.lseek(fd, 0, os.SEEK_SET)
        record("read", lambda: os.read(fd, len(payload)))
        os.lseek(fd, 0, os.SEEK_END)
        record("writev", lambda: os.writev(fd, [payload[:8], payload[8:]]))
        os.lseek(fd, 0, os.SEEK_SET)
        record("readv", lambda: os.readv(fd, [bytearray(8), bytearray(len(payload) - 8)]))
    os.lseek(fd, 0, os.SEEK_END)
    record("read", lambda: os.read(fd, 16), "eof")  # zero return
    dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        record("read", lambda: os.read(dfd, 16), "directory")  # -EISDIR
    finally:
        os.close(dfd)
finally:
    os.close(fd)
print(json.dumps(result))
'''

_READ_OPS = frozenset({"read", "readv"})
_WRITE_OPS = frozenset({"write", "writev"})


def expected_workload_counters(ops) -> dict[str, int]:
    """Count only positive VFS returns; failed and zero-length calls add nothing."""
    reads = writes = bytes_r = bytes_w = 0
    for entry in ops:
        ret = entry.get("ret")
        if type(ret) is not int or ret <= 0:
            continue
        name = entry.get("op")
        if name in _READ_OPS:
            reads += 1
            bytes_r += ret
        elif name in _WRITE_OPS:
            writes += 1
            bytes_w += ret
    return {"reads": reads, "writes": writes, "bytes_r": bytes_r, "bytes_w": bytes_w}


@dataclass(frozen=True)
class WorkloadResult:
    mode: str
    ops: list[dict]
    payload_len: int
    counters: dict[str, int]
    pid: int | None = None


def run_workload(
    path: Path,
    *,
    mode: str,
    cgroup: "CgroupContext",
    shared_cgroup: "CgroupContext",
    timeout: float,
) -> tuple[WorkloadResult, "CgroupContext"]:
    """Run one controlled workload process, in the isolated cgroup when possible.

    The process blocks on stdin until the parent has written its pid into
    ``cgroup.procs``, so every operation it performs is attributed to the expected
    cgroup rather than to the harness that started it. A refused placement falls
    back to the harness cgroup and says so in the returned context.
    """
    argv = [sys.executable, "-I", "-c", _WORKLOAD_SOURCE, str(path), mode, _PAYLOAD.hex()]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(path.parent),
    )
    effective = cgroup
    stderr = ""
    try:
        if cgroup.isolated and cgroup.path is not None:
            try:
                (cgroup.path / "cgroup.procs").write_text(
                    f"{process.pid}\n", encoding="utf-8"
                )
            except OSError as failure:
                effective = replace(
                    shared_cgroup,
                    detail=(
                        f"child cgroup {cgroup.path} refused the workload pid "
                        f"({failure.strerror}); workload ran in the harness cgroup, so "
                        "counters may include unrelated session traffic"
                    ),
                )
        assert process.stdin is not None
        process.stdin.write("go\n")
        process.stdin.flush()
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AcceptanceError(
                f"the controlled workload ({mode}) did not finish within {timeout:g}s"
            ) from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if process.returncode != 0:
        raise AcceptanceError(
            f"the controlled workload ({mode}) exited {process.returncode}: "
            f"{_bounded(stderr, 1024)}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise AcceptanceError(
            f"the controlled workload ({mode}) produced no JSON: {_bounded(stdout, 1024)}"
        ) from None
    ops = payload.get("ops") or []
    return (
        WorkloadResult(
            mode=mode,
            ops=ops,
            payload_len=int(payload.get("payload_len", 0)),
            counters=expected_workload_counters(ops),
            pid=process.pid,
        ),
        effective,
    )


# ---------------------------------------------------------------------------
# Host context: cgroup, scratch directory, file identity, IOAccounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CgroupContext:
    path: Path | None
    cgroup_id: int | None
    isolated: bool
    detail: str


def _cgroup_inode(path: Path) -> int | None:
    """cgroup v2 exposes the cgroup id as the kernfs inode number."""
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def _current_cgroup() -> Path | None:
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            relative = line[3:].lstrip("/")
            return Path("/sys/fs/cgroup") / relative if relative else Path("/sys/fs/cgroup")
    return None


def prepare_cgroups() -> tuple[CgroupContext, CgroupContext, object]:
    """Return ``(isolated_or_shared, shared, cleanup)`` for the workload."""

    def nothing() -> None:
        return None

    base = _current_cgroup()
    if base is None:
        shared = CgroupContext(None, None, False, "cgroup v2 path unavailable")
        return shared, shared, nothing
    shared = CgroupContext(
        base,
        _cgroup_inode(base),
        False,
        "workload shares the harness cgroup; counters may include unrelated session traffic",
    )
    child = base / f"appio-acceptance-{os.getpid()}-{os.urandom(3).hex()}"
    try:
        child.mkdir(mode=0o755)
    except OSError as failure:
        shared = replace(
            shared,
            detail=f"{shared.detail} (child cgroup mkdir failed: {failure.strerror})",
        )
        return shared, shared, nothing

    def cleanup() -> None:
        with contextlib.suppress(OSError):
            (child / "cgroup.kill").write_text("1\n", encoding="utf-8")
        deadline = time.monotonic() + 2.0
        while True:
            try:
                child.rmdir()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    return  # the leftover is reported instead of hidden
                time.sleep(0.02)

    return (
        CgroupContext(
            child, _cgroup_inode(child), True, "workload runs in an isolated child cgroup"
        ),
        shared,
        cleanup,
    )


def _is_block_backed(path: Path) -> bool:
    try:
        status = os.stat(path)
    except OSError:
        return False
    if os.major(status.st_dev) == 0:
        return False  # tmpfs and friends have no block device
    return Path(f"/sys/dev/block/{os.major(status.st_dev)}:{os.minor(status.st_dev)}").exists()


def choose_scratch_dir(environ=None) -> Path:
    """Pick a block-backed directory: the producer requires ``sb->s_bdev != NULL``.

    The checkout is deliberately not a candidate: the payload, its temporary
    directory and the object must never appear as untracked files in the tree.
    """
    environ = os.environ if environ is None else environ
    candidates: list[Path] = []
    override = environ.get(SCRATCH_ENV)
    if override:
        candidates.append(Path(override))
    candidates.extend(
        [Path.home(), Path("/var/tmp"), Path(tempfile.gettempdir()), Path("/tmp")]
    )
    for candidate in candidates:
        if candidate.is_dir() and _is_block_backed(candidate):
            return candidate
    raise AcceptanceError(
        "no block-backed scratch directory among "
        f"{', '.join(str(path) for path in candidates)}; the producer counts only "
        "files whose superblock has a block device, so tmpfs cannot be used. Set "
        f"{SCRATCH_ENV} to a directory on a real block device."
    )


def _file_identity(path: Path) -> tuple[str, int]:
    status = os.stat(path)
    return f"{os.major(status.st_dev)}:{os.minor(status.st_dev)}", status.st_ino


class _FileHandle(ctypes.Structure):
    _fields_ = [
        ("handle_bytes", ctypes.c_uint),
        ("handle_type", ctypes.c_int),
        ("f_handle", ctypes.c_ubyte * 128),
    ]


_FILEID_INO32_GEN = 1


def generation_from_handle(path: Path, inode: int) -> tuple[int | None, str]:
    """Independent ``i_generation`` evidence from ``name_to_handle_at(2)``.

    ext2/3/4 and other ``FILEID_INO32_GEN`` filesystems return the inode and the
    generation in an 8-byte handle, which lets the acceptance check a field that
    ``stat(2)`` does not expose. Any other filesystem reports "unavailable": the
    run must not claim corroboration it does not have.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        function = libc.name_to_handle_at
    except (OSError, AttributeError) as failure:
        return None, f"unavailable ({failure})"
    handle = _FileHandle()
    handle.handle_bytes = 128
    mount_id = ctypes.c_int()
    result = function(
        ctypes.c_int(-100),  # AT_FDCWD
        str(path).encode(),
        ctypes.byref(handle),
        ctypes.byref(mount_id),
        ctypes.c_int(0),
    )
    if result != 0:
        return None, f"unavailable ({os.strerror(ctypes.get_errno())})"
    if handle.handle_type != _FILEID_INO32_GEN or handle.handle_bytes != 8:
        return None, (
            f"unavailable (handle_type {handle.handle_type}, "
            f"{handle.handle_bytes} bytes)"
        )
    raw = bytes(handle.f_handle[: handle.handle_bytes])
    handle_inode, generation = struct.unpack("<II", raw)
    if handle_inode != inode:
        return None, f"unavailable (handle inode {handle_inode} != stat inode {inode})"
    return generation, "name_to_handle_at(FILEID_INO32_GEN)"


def read_io_stat(cgroup: CgroupContext) -> dict[str, dict[str, int]]:
    """Cgroup v2 ``io.stat``, used only as IOAccounting corroboration."""
    if cgroup.path is None:
        return {}
    try:
        text = (cgroup.path / "io.stat").read_text(encoding="utf-8")
    except OSError:
        return {}
    entries: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        values: dict[str, int] = {}
        for field in fields[1:]:
            key, _, value = field.partition("=")
            try:
                values[key] = int(value)
            except ValueError:
                continue
        if values:
            entries[fields[0]] = values
    return entries


def _io_delta(before: dict, after: dict, device: str) -> dict[str, int | None]:
    start, end = before.get(device, {}), after.get(device, {})
    if not start and not end:
        return {"delta_rbytes": None, "delta_wbytes": None}
    return {
        "delta_rbytes": end.get("rbytes", 0) - start.get("rbytes", 0),
        "delta_wbytes": end.get("wbytes", 0) - start.get("wbytes", 0),
    }


# ---------------------------------------------------------------------------
# Loader session
# ---------------------------------------------------------------------------


def _hello_evidence(frame: bytes) -> dict:
    """Decode a HELLO frame defensively, so a failure is still reportable."""
    evidence: dict = {"frame_bytes": len(frame)}
    try:
        (abi, status, hook_mask, map_capacity, error, verifier_len) = HELLO_RESPONSE.unpack_from(
            frame
        )
    except struct.error:
        evidence.update(
            abi=None,
            status=None,
            hook_mask=None,
            map_capacity=None,
            error=None,
            verifier=None,
        )
        return evidence
    verifier = None
    if 0 < verifier_len <= DIAGNOSTIC_CAP:
        verifier = frame[HELLO_RESPONSE.size : HELLO_RESPONSE.size + verifier_len].decode(
            "utf-8", "replace"
        )
    evidence.update(
        abi=abi,
        status=status,
        hook_mask=hook_mask,
        map_capacity=map_capacity,
        error=error,
        verifier=verifier,
    )
    return evidence


def _ack_evidence(frame: bytes) -> dict:
    """Decode a bare ACK (CLOSE) frame defensively."""
    try:
        abi, status, error, diagnostic_len = ACK_RESPONSE.unpack_from(frame)
    except struct.error:
        return {"frame_bytes": len(frame)}
    diagnostic = None
    if 0 < diagnostic_len <= DIAGNOSTIC_CAP:
        diagnostic = frame[ACK_RESPONSE.size : ACK_RESPONSE.size + diagnostic_len].decode(
            "utf-8", "replace"
        )
    return {
        "frame_bytes": len(frame),
        "abi": abi,
        "status": status,
        "error": error,
        "diagnostic": diagnostic,
    }


#: Loader failures this acceptance recognises, with the owning task. The acceptance
#: reports the defect and never repairs another task's file.
_KNOWN_BLOCKERS = (
    (
        "bpf_object_open_opts size",
        "the T-001 loader calls bpf_object__open_file() with a zero-initialised "
        "bpf_object_open_opts, but libbpf requires opts.sz (use "
        "LIBBPF_OPTS(bpf_object_open_opts, options)). T-001-owned file "
        "appio_iod/bpf/fileio_loader.c is outside this task's allowlist, so the "
        "acceptance reports the block instead of repairing it.",
    ),
)


def _known_blockers(stderr: str | None) -> list[str]:
    if not stderr:
        return []
    return [explanation for fragment, explanation in _KNOWN_BLOCKERS if fragment in stderr]


def _hook_bits(mask: int | None) -> dict[str, bool]:
    return {
        hook: bool(mask is not None and mask & (1 << index))
        for index, hook in enumerate(HOOK_SYMBOLS)
    }


def _drain_slot(frame: bytes) -> int | None:
    """The retired slot, straight from the wire response the decoder accepted."""
    try:
        return int(DRAIN_RESPONSE.unpack_from(frame)[5])
    except struct.error:
        return None


class _MapInfo(ctypes.Structure):
    # The stable prefix of Linux struct bpf_map_info; the kernel copies info_len.
    _fields_ = [
        ("type", ctypes.c_uint32), ("id", ctypes.c_uint32),
        ("key_size", ctypes.c_uint32), ("value_size", ctypes.c_uint32),
        ("max_entries", ctypes.c_uint32), ("map_flags", ctypes.c_uint32),
        ("name", ctypes.c_char * 16),
    ]


def _post_drain_slot_counts(pid: int, *, fdinfo_root=None, libbpf=None):
    """Inspect this loader's real inner maps, only from the opt-in run path.

    Counts in the active slot are observational: other cgroups keep writing.
    The retired slot must be empty after the completed drain acknowledgement.
    Tests inject both fdinfo and libbpf; they never access /proc or live maps.
    """
    if libbpf is None:
        libbpf = ctypes.CDLL("libbpf.so.1", use_errno=True)
        for name, args in {
            "bpf_map_get_fd_by_id": [ctypes.c_uint32],
            "bpf_obj_get_info_by_fd": [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p],
            "bpf_map_lookup_elem": [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p],
            "bpf_map_get_next_key": [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p],
        }.items():
            function = getattr(libbpf, name)
            function.argtypes = args
            function.restype = ctypes.c_int
    if fdinfo_root is None:
        fdinfo_root = Path(f"/proc/{pid}/fdinfo")

    def checked(result, operation):
        if result < 0:
            raise AcceptanceError(f"map count {operation}: errno={ctypes.get_errno()}")
        return result

    def info(fd):
        value = _MapInfo()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        checked(libbpf.bpf_obj_get_info_by_fd(fd, ctypes.byref(value), ctypes.byref(size)),
                "get info")
        return value

    def count(fd, metadata):
        current = None
        total = 0
        while True:
            following = ctypes.create_string_buffer(metadata.key_size)
            ctypes.set_errno(0)
            result = libbpf.bpf_map_get_next_key(fd, current, following)
            if result < 0 and ctypes.get_errno() == errno.ENOENT:
                return total
            checked(result, "get next key")
            total += 1
            if total > metadata.max_entries:
                raise AcceptanceError("map count exceeded map capacity during iteration")
            current = following

    names = {"fileio_slots": "events", "fileio_stats_slots"[:15]: "stats"}
    slots = [{}, {}]
    seen = set()
    found = set()
    for fdinfo in Path(fdinfo_root).iterdir():
        match = re.search(r"^map_id:\s*(\d+)$", fdinfo.read_text(), re.MULTILINE)
        if match is None:
            continue
        map_id = int(match[1])
        if map_id in seen:
            continue
        seen.add(map_id)
        outer_fd = checked(libbpf.bpf_map_get_fd_by_id(map_id), "open outer")
        try:
            metadata = info(outer_fd)
            name = metadata.name.decode("ascii")
            # Inner stats names share the same 15-byte prefix as their outer.
            if name not in names or metadata.type != 12:  # ARRAY_OF_MAPS
                continue
            if metadata.max_entries != 2:
                raise AcceptanceError(f"unexpected outer map layout for {name}")
            label = names[name]
            if label in found:
                raise AcceptanceError(f"ambiguous loader map {name}")
            found.add(label)
            for slot in range(2):
                key = ctypes.c_uint32(slot)
                inner_id = ctypes.c_uint32()
                checked(libbpf.bpf_map_lookup_elem(outer_fd, ctypes.byref(key),
                                                  ctypes.byref(inner_id)), "lookup inner id")
                # Outer lookup returns an ID, never a process fd.
                inner_fd = checked(libbpf.bpf_map_get_fd_by_id(inner_id.value), "open inner")
                try:
                    inner_info = info(inner_fd)
                    slots[slot][label] = count(inner_fd, inner_info)
                    slots[slot][f"{label}_map_id"] = inner_id.value
                finally:
                    os.close(inner_fd)
        finally:
            os.close(outer_fd)
    if found != {"events", "stats"}:
        raise AcceptanceError(f"could not find both loader outer maps: {sorted(found)}")
    return slots


class _RecordingLoader:
    """A ``LoaderProtocol`` that keeps every raw frame and delegates to the real one.

    The decoder in :mod:`appio.bpf_fileio` owns all interpretation; recording the
    frames only lets the report show the wire evidence (hook bitmask, drained
    slot, errno, verifier text) that the typed objects deliberately discard.
    """

    def __init__(self, loader) -> None:
        self._loader = loader
        self.frames: list[tuple[str, bytes]] = []

    def hello(self) -> bytes:
        frame = self._loader.hello()
        self.frames.append(("hello", frame))
        return frame

    def flip_drain(self, epoch: int, seq: int) -> bytes:
        frame = self._loader.flip_drain(epoch, seq)
        self.frames.append(("drain", frame))
        return frame

    def close(self) -> bytes:
        frame = self._loader.close()
        self.frames.append(("close", frame))
        return frame

    def last(self, kind: str) -> bytes:
        for frame_kind, frame in reversed(self.frames):
            if frame_kind == kind:
                return frame
        return b""


def build_object(workdir: Path) -> tuple[Path, Path]:
    """Compile the T-001 object and loader into a temporary directory."""
    out = workdir / "build"
    try:
        done = subprocess.run(
            ["make", "-C", str(BPF_DIR), "check", f"OUT_DIR={out}"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as failure:
        raise AcceptanceError(f"could not run the BPF build: {failure}") from None
    if done.returncode != 0:
        raise AcceptanceError(
            f"make check failed with exit {done.returncode}: {_bounded(done.stderr, 2048)}"
        )
    obj, loader = out / "fileio.bpf.o", out / "fileio_loader"
    for artifact in (obj, loader):
        if not artifact.is_file():
            raise AcceptanceError(f"make check produced no {artifact.name} under {out}")
    return obj, loader


def _read_bounded(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    text = data.decode("utf-8", "replace").strip()
    return _bounded(text, 4096) if text else None


def _event_dict(event) -> dict:
    return {
        "cgroup_id": event.cgroup_id,
        "dev": event.dev,
        "inode": event.inode,
        "generation": event.generation,
        "basename": event.basename.decode("utf-8", "replace"),
        "name_len": event.name_len,
        "reads": event.reads,
        "writes": event.writes,
        "bytes_r": event.bytes_r,
        "bytes_w": event.bytes_w,
    }


def _stats_dict(stats) -> dict:
    hits = stats.hook_hits
    return {
        "cgroup_id": stats.cgroup_id,
        "hook_hits": {hook: getattr(hits, hook) for hook in HOOK_SYMBOLS},
        "overflow_files": stats.overflow_files,
        "overflow_bytes_r": stats.overflow_bytes_r,
        "overflow_bytes_w": stats.overflow_bytes_w,
        "skipped_nonfile": stats.skipped_nonfile,
        "skipped_unsupported_fs": stats.skipped_unsupported_fs,
        "is_unknown_owner": stats.is_unknown_owner,
    }


# ---------------------------------------------------------------------------
# The acceptance itself
# ---------------------------------------------------------------------------


def _host_facts() -> dict:
    facts: dict = {"kernel": os.uname().release, "btf_path": str(BTF_PATH)}
    for name, argv in (
        ("clang", ["clang", "--version"]),
        ("libbpf", ["pkg-config", "--modversion", "libbpf"]),
    ):
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
            first_line = (done.stdout.strip() or done.stderr.strip()).splitlines()
            facts[name] = _bounded(first_line[0] if first_line else "", 120) or None
        except (OSError, subprocess.TimeoutExpired):
            facts[name] = None
    try:
        facts["unprivileged_bpf_disabled"] = (
            Path("/proc/sys/kernel/unprivileged_bpf_disabled")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        facts["unprivileged_bpf_disabled"] = None
    facts["btf_bytes"] = BTF_PATH.stat().st_size if BTF_PATH.is_file() else None
    return facts


def run_acceptance(
    *,
    environ=None,
    effective_uid=None,
    btf_loader=None,
    workroot=None,
    deadline: float = 120.0,
    workload_timeout: float = 60.0,
) -> dict:
    """Run the privileged acceptance, or refuse before any privileged action.

    The gate is evaluated here, inside the only entry point that can load BPF, so
    a caller cannot bypass it by calling this function directly instead of the
    pytest wrapper.
    """
    environ = os.environ if environ is None else environ
    uid = os.geteuid() if effective_uid is None else int(effective_uid)
    gate = evaluate_gate(environ=environ, effective_uid=uid, btf_loader=btf_loader)
    if not gate.allowed:
        raise AcceptanceRefused(gate.reason)

    image = read_btf() if btf_loader is None else btf_loader()
    verdicts = signature_verdicts(image)
    report: dict = {
        "acceptance": "appio-p4-bpf-file-workload",
        "verdict": "UNKNOWN",
        "gate": {
            "opt_in_env": OPT_IN_ENV,
            "opted_in": gate.opted_in,
            "effective_uid": gate.effective_uid,
            "allowed": gate.allowed,
        },
        "host": _host_facts(),
        "signatures": {
            hook: {
                "status": verdict.status,
                "kernel": verdict.kernel_return,
                "assumed": verdict.assumed_return,
                "notes": list(verdict.notes),
                "evidence": verdict.evidence,
            }
            for hook, verdict in verdicts.items()
        },
        "attach": {"hook_mask": None, "hooks": {}, "verifier": None, "errno": None},
        "workload": {},
        "per_hook_hits": {},
        "per_cgroup": [],
        "per_file": [],
        "skipped": {},
        "overflow": {},
        "unknown_owner": None,
        "drains": [],
        "io_accounting": {},
        "diagnostics": {"verifier": None, "errno": None, "loader_stderr": None},
        "checks": [],
        "failures": [],
        "warnings": [],
    }

    def check(name: str, passed: bool, detail: str = "") -> bool:
        report["checks"].append(
            {"name": name, "passed": bool(passed), "detail": _bounded(detail, 400)}
        )
        if not passed:
            report["failures"].append(f"{name}: {_bounded(detail, 400)}")
        return bool(passed)

    for hook, verdict in verdicts.items():
        check(f"signature_{hook}", verdict.confirmed, verdict.evidence)

    workdir = Path(tempfile.mkdtemp(prefix="appio-p4-acceptance-", dir=workroot))
    payload_root: Path | None = None
    cleanup_cgroup = lambda: None  # noqa: E731 - replaced once a cgroup is prepared
    try:
        scratch = choose_scratch_dir(environ)
        # The payload must live on a block-backed filesystem, so it gets its own
        # temporary directory there rather than inside the (possibly tmpfs) workdir.
        payload_root = Path(
            tempfile.mkdtemp(prefix="appio-p4-acceptance-payload-", dir=scratch)
        )
        payload = payload_root / "acceptance-payload.bin"
        report["workload"].update(
            {
                "payload_path": str(payload),
                "payload_bytes": len(_PAYLOAD),
                "scratch_dir": str(scratch),
                "cgroup_isolated": False,
                "second_pass_records": [],
            }
        )

        obj, loader = build_object(workdir)
        report["host"]["object"] = str(obj)
        report["host"]["loader"] = str(loader)

        cgroup, shared_cgroup, cleanup_cgroup = prepare_cgroups()
        stderr_path = workdir / "loader.stderr"
        recorder = None
        with open(stderr_path, "w+b") as stderr_file:
            process = subprocess.Popen(
                [str(loader), str(obj)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                bufsize=0,
            )
            watchdog = threading.Timer(deadline, process.kill)
            watchdog.daemon = True
            watchdog.start()
            recorder = _RecordingLoader(StreamLoader(process.stdout, process.stdin))
            source = BpfFileSource(recorder)
            try:
                availability = source.start()
                hello = _hello_evidence(recorder.last("hello"))
                report["diagnostics"]["verifier"] = hello.get("verifier")
                report["diagnostics"]["errno"] = hello.get("error")
                mask = hello.get("hook_mask")
                report["attach"] = {
                    "hook_mask": mask,
                    "hooks": _hook_bits(mask),
                    "verifier": hello.get("verifier"),
                    "errno": hello.get("error"),
                    "file_io": availability.file_io,
                    "bpf_loaded": availability.bpf_loaded,
                    "diagnostic": availability.diagnostic,
                    "abi": hello.get("abi"),
                    "map_capacity": hello.get("map_capacity"),
                }
                missing = [
                    hook
                    for hook, attached in report["attach"]["hooks"].items()
                    if not attached
                ]
                check(
                    "attach_six_hooks",
                    mask == HOOK_MASK_ALL and not missing,
                    f"HELLO hook_mask {mask!r}; missing: {', '.join(missing) or 'none'}",
                )
                check(
                    "attach_advertised",
                    availability.file_io and availability.bpf_loaded,
                    availability.diagnostic or "loader reported file_io and bpf_loaded",
                )

                if availability.file_io:
                    before = read_io_stat(cgroup)
                    first_pass, cgroup = run_workload(
                        payload,
                        mode="full",
                        cgroup=cgroup,
                        shared_cgroup=shared_cgroup,
                        timeout=workload_timeout,
                    )
                    identity_dev, identity_inode = _file_identity(payload)
                    generation, generation_evidence = generation_from_handle(
                        payload, identity_inode
                    )
                    report["workload"].update(
                        {
                            "ops": first_pass.ops,
                            "expected": first_pass.counters,
                            "expected_dev": identity_dev,
                            "expected_inode": identity_inode,
                            "expected_basename": payload.name,
                            "generation_expected": generation,
                            "generation_evidence": generation_evidence,
                            "cgroup_id": cgroup.cgroup_id,
                            "cgroup_path": str(cgroup.path) if cgroup.path else None,
                            "cgroup_isolated": cgroup.isolated,
                            "cgroup_detail": cgroup.detail,
                        }
                    )

                    first = source.drain(Receipt("iod", 1, 1))
                    report["drains"].append(
                        {
                            "receipt": [first.receipt.epoch, first.receipt.seq],
                            "drained_slot": _drain_slot(recorder.last("drain")),
                            "complete": 1 if first.complete else 0,
                            "events": [_event_dict(event) for event in first.events],
                            "stats": [_stats_dict(stats) for stats in first.stats],
                            "diagnostic": first.diagnostic,
                            "post_drain_slots": _post_drain_slot_counts(process.pid),
                        }
                    )

                    nonpositive_pass, cgroup = run_workload(
                        payload,
                        mode="nonpositive",
                        cgroup=cgroup,
                        shared_cgroup=shared_cgroup,
                        timeout=workload_timeout,
                    )
                    second = source.drain(Receipt("iod", 1, 2))
                    report["drains"].append(
                        {
                            "receipt": [second.receipt.epoch, second.receipt.seq],
                            "drained_slot": _drain_slot(recorder.last("drain")),
                            "complete": 1 if second.complete else 0,
                            "events": [_event_dict(event) for event in second.events],
                            "stats": [_stats_dict(stats) for stats in second.stats],
                            "diagnostic": second.diagnostic,
                            "post_drain_slots": _post_drain_slot_counts(process.pid),
                        }
                    )
                    after = read_io_stat(cgroup)
                    report["io_accounting"] = {
                        "device": identity_dev,
                        "cgroup_path": str(cgroup.path) if cgroup.path else None,
                        "before": before,
                        "after": after,
                        **_io_delta(before, after, identity_dev),
                    }
                    _evaluate_drains(
                        report, check, identity_dev, identity_inode, generation
                    )
                    _evaluate_counters(report, check, cgroup, first_pass, nonpositive_pass)
            except (AcceptanceError, FileSourceError, OSError, struct.error) as failure:
                report["failures"].append(_bounded(failure, 1024))
                report["diagnostics"]["error"] = _bounded(failure, 1024)
            finally:
                watchdog.cancel()
                with contextlib.suppress(FileSourceError, OSError):
                    source.close()
                if process.poll() is None:
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)
                with contextlib.suppress(struct.error, OSError):
                    report["diagnostics"]["close"] = _ack_evidence(recorder.last("close"))
                report["diagnostics"]["loader_stderr"] = _read_bounded(stderr_path)
                blockers = _known_blockers(report["diagnostics"]["loader_stderr"])
                if blockers:
                    report["diagnostics"]["known_blockers"] = blockers
    finally:
        cleanup_cgroup()
        cgroup_path = report["workload"].get("cgroup_path")
        if report["workload"].get("cgroup_isolated") and cgroup_path:
            if Path(cgroup_path).exists():
                report["warnings"].append(f"temporary cgroup not removed: {cgroup_path}")
        if payload_root is not None:
            shutil.rmtree(payload_root, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)

    if any(verdict.status == "REFUTED" for verdict in verdicts.values()):
        report["verdict"] = "REFUTED"
    elif report["failures"]:
        report["verdict"] = "FAILED"
    else:
        report["verdict"] = "CONFIRMED"
    return report


def _evaluate_drains(report, check, dev: str, inode: int, generation: int | None) -> None:
    first, second = report["drains"]
    check(
        "drain_complete",
        first["complete"] == 1 and second["complete"] == 1,
        f"drain completion {first['complete']}/{second['complete']}",
    )
    check(
        "drain_slot_rotated",
        first["drained_slot"] != second["drained_slot"],
        f"retired slots {first['drained_slot']} then {second['drained_slot']}",
    )
    matching = [
        event for event in first["events"] if event["dev"] == dev and event["inode"] == inode
    ]
    check(
        "file_record_present",
        len(matching) == 1,
        f"{len(matching)} drained records for {dev}:{inode}",
    )
    second_matching = [
        event for event in second["events"] if event["dev"] == dev and event["inode"] == inode
    ]
    report["workload"]["second_pass_records"] = second_matching
    check(
        "nonpositive_adds_no_record",
        not second_matching,
        f"{len(second_matching)} records after the nonpositive pass",
    )
    retired_counts = []
    for drained in (first, second):
        slot = drained["drained_slot"]
        snapshots = drained.get("post_drain_slots", [])
        counts = snapshots[slot] if slot in (0, 1) and len(snapshots) == 2 else {}
        retired_counts.append(counts)
    check(
        "retired_slot_cleared",
        all(counts.get("events") == 0 and counts.get("stats") == 0
            for counts in retired_counts),
        f"post-drain retired event/stats counts: {retired_counts}",
    )
    if len(matching) != 1:
        return
    record = matching[0]
    report["workload"]["record"] = record
    expected = report["workload"]["expected"]
    check(
        "file_name_matches",
        record["basename"] == report["workload"]["expected_basename"],
        f"record name {record['basename']!r}",
    )
    check(
        "file_read_bytes",
        record["bytes_r"] == expected["bytes_r"],
        f"record {record['bytes_r']} vs workload {expected['bytes_r']}",
    )
    check(
        "file_write_bytes",
        record["bytes_w"] == expected["bytes_w"],
        f"record {record['bytes_w']} vs workload {expected['bytes_w']}",
    )
    check(
        "file_read_ops",
        record["reads"] == expected["reads"],
        f"record {record['reads']} vs workload {expected['reads']}",
    )
    check(
        "file_write_ops",
        record["writes"] == expected["writes"],
        f"record {record['writes']} vs workload {expected['writes']}",
    )
    if generation is not None:
        check(
            "file_generation",
            record["generation"] == generation,
            f"record {record['generation']} vs name_to_handle_at {generation}",
        )
    expected_cgroup = report["workload"].get("cgroup_id")
    if expected_cgroup is not None:
        check(
            "file_cgroup_attribution",
            record["cgroup_id"] == expected_cgroup,
            f"record cgroup {record['cgroup_id']} vs workload cgroup {expected_cgroup}",
        )


def _evaluate_counters(report, check, cgroup, first_pass, nonpositive_pass) -> None:
    target = report["workload"].get("cgroup_id")
    rows = [row for row in report["drains"][0]["stats"] if row["cgroup_id"] == target]
    unknown = [row for row in report["drains"][0]["stats"] if row["is_unknown_owner"]]
    report["per_cgroup"] = report["drains"][0]["stats"]
    report["per_file"] = report["drains"][0]["events"]
    report["unknown_owner"] = unknown[0] if unknown else None
    check(
        "cgroup_stats_present",
        len(rows) == 1,
        f"{len(rows)} stats rows for cgroup {target}",
    )
    row = rows[0] if rows else {
        "hook_hits": {hook: 0 for hook in HOOK_SYMBOLS},
        "overflow_files": 0,
        "overflow_bytes_r": 0,
        "overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
    }
    report["per_hook_hits"] = dict(row["hook_hits"])
    report["overflow"] = {
        "overflow_files": row["overflow_files"],
        "overflow_bytes_r": row["overflow_bytes_r"],
        "overflow_bytes_w": row["overflow_bytes_w"],
    }
    report["skipped"] = {
        "skipped_nonfile": row["skipped_nonfile"],
        "skipped_unsupported_fs": row["skipped_unsupported_fs"],
    }
    positive_ops = first_pass.counters["reads"] + first_pass.counters["writes"]
    hits = row["hook_hits"]
    check("hook_hits_observed", sum(hits.values()) >= 1, f"hook hits {hits}")
    check(
        "hook_hits_internally_consistent",
        all(value >= 0 for value in hits.values()) and sum(hits.values()) >= positive_ops,
        f"hook hits {hits} vs {positive_ops} positive operations",
    )
    check(
        "nonpositive_counted_nothing",
        nonpositive_pass.counters
        == {"reads": 0, "writes": 0, "bytes_r": 0, "bytes_w": 0},
        "nonpositive pass counters "
        f"{nonpositive_pass.counters} from {nonpositive_pass.ops}",
    )
    if cgroup.isolated:
        check(
            "no_overflow_in_isolated_cgroup",
            row["overflow_files"] == 0
            and row["overflow_bytes_r"] == 0
            and row["overflow_bytes_w"] == 0,
            f"overflow {report['overflow']}",
        )
    report["workload"]["nonpositive_expected"] = nonpositive_pass.counters


def procedure_text() -> str:
    """The documented procedure, printed by ``--procedure``."""
    return (__doc__ or "").strip()


def format_report(report: dict) -> str:
    """Render the report as the operator-facing evidence block."""
    host = report["host"]
    lines = [
        f"verdict: {report['verdict']}",
        f"host: kernel {host.get('kernel')} clang {host.get('clang')} "
        f"libbpf {host.get('libbpf')} "
        f"unprivileged_bpf_disabled={host.get('unprivileged_bpf_disabled')}",
        f"btf: {host.get('btf_path')} ({host.get('btf_bytes')} bytes)",
        "",
        "signature verdicts (evidence: /sys/kernel/btf/vmlinux FUNC/FUNC_PROTO):",
    ]
    for hook, entry in report["signatures"].items():
        lines.append(f"  {hook}: {entry['status']}")
        lines.append(f"    {entry['evidence']}")
        for note in entry.get("notes") or ():
            lines.append(f"    note: {note}")
    attach = report["attach"]
    lines.extend(
        ["", f"attach: hook_mask={attach.get('hook_mask')!r} abi={attach.get('abi')!r}"]
    )
    for hook, attached in (attach.get("hooks") or {}).items():
        lines.append(f"  {hook}: {'ATTACHED' if attached else 'MISSING'}")
    if attach.get("verifier"):
        lines.append(f"  verifier: {attach['verifier']}")
    if attach.get("errno"):
        lines.append(f"  errno: {attach['errno']}")
    if report["per_hook_hits"]:
        lines.extend(["", "per-hook hits (target cgroup):"])
        for hook, value in report["per_hook_hits"].items():
            lines.append(f"  {hook}: {value}{'' if value else ' (no observed hit)'}")
    work = report.get("workload", {})
    if work.get("record"):
        record = work["record"]
        lines.extend(
            [
                "",
                "workload record: "
                f"dev={record['dev']} inode={record['inode']} "
                f"generation={record['generation']} name={record['basename']!r} "
                f"reads={record['reads']} writes={record['writes']} "
                f"bytes_r={record['bytes_r']} bytes_w={record['bytes_w']}",
                f"workload expected: {work.get('expected')} "
                f"(generation evidence: {work.get('generation_evidence')})",
            ]
        )
    if report["per_file"]:
        lines.extend(["", f"drained file records: {len(report['per_file'])}"])
        for event in report["per_file"][:20]:
            lines.append(
                f"  {event['dev']}:{event['inode']} gen={event['generation']} "
                f"{event['basename']!r} r={event['reads']}/{event['bytes_r']} "
                f"w={event['writes']}/{event['bytes_w']}"
            )
        if len(report["per_file"]) > 20:
            lines.append(f"  ... {len(report['per_file']) - 20} more")
    if report["per_cgroup"]:
        lines.extend(["", f"drained cgroup stats rows: {len(report['per_cgroup'])}"])
        for row in report["per_cgroup"]:
            owner = " (unknown owner)" if row["is_unknown_owner"] else ""
            lines.append(
                f"  cgroup {row['cgroup_id']}{owner}: hits={row['hook_hits']} "
                f"overflow={row['overflow_files']}/{row['overflow_bytes_r']}/"
                f"{row['overflow_bytes_w']} skipped={row['skipped_nonfile']}/"
                f"{row['skipped_unsupported_fs']}"
            )
    if report.get("checks"):
        lines.extend(["", "checks:"])
        for entry in report["checks"]:
            lines.append(f"  [{'ok' if entry['passed'] else 'FAIL'}] {entry['name']}: {entry['detail']}")
    if report.get("failures"):
        lines.extend(["", "failures:"])
        lines.extend(f"  - {failure}" for failure in report["failures"])
    diagnostics = report.get("diagnostics") or {}
    if diagnostics.get("verifier"):
        lines.extend(["", f"verifier diagnostics: {diagnostics['verifier']}"])
    if diagnostics.get("loader_stderr"):
        lines.extend(["", f"loader stderr (bounded): {diagnostics['loader_stderr']}"])
    for blocker in diagnostics.get("known_blockers") or ():
        lines.extend(["", f"known blocker: {blocker}"])
    if diagnostics.get("error"):
        lines.extend(["", f"error: {diagnostics['error']}"])
    accounting = report.get("io_accounting") or {}
    if accounting:
        lines.extend(
            [
                "",
                f"io accounting: device {accounting.get('device')} rbytes delta "
                f"{accounting.get('delta_rbytes')} wbytes delta "
                f"{accounting.get('delta_wbytes')}",
            ]
        )
    lines.extend(f"warning: {warning}" for warning in report.get("warnings") or [])
    return "\n".join(lines)


def main(argv=None, environ=None) -> int:
    """The operator entry point; every verb except ``--procedure`` gates itself."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Opt-in privileged acceptance for the P4 per-file eBPF producer."
    )
    verbs = parser.add_mutually_exclusive_group()
    verbs.add_argument("--procedure", action="store_true", help="print the runbook and exit")
    verbs.add_argument(
        "--check",
        action="store_true",
        help="prerequisites plus BTF signature evidence (opt-in, no root, no load)",
    )
    verbs.add_argument("--run", action="store_true", help="run the privileged acceptance")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if args.procedure:
        print(procedure_text())
        return 0

    environ = os.environ if environ is None else environ
    if args.check:
        gate = evaluate_gate(environ=environ, effective_uid=os.geteuid(), require_root=False)
        if not gate.allowed:
            print(gate.reason)
            return 2
        try:
            verdicts = signature_verdicts(read_btf())
        except (OSError, BtfError) as failure:
            print(_refusal(f"{BTF_PATH} could not be read: {failure}"))
            return 2
        if args.json:
            print(
                json.dumps(
                    {
                        "host": _host_facts(),
                        "signatures": {
                            hook: {"status": verdict.status, "evidence": verdict.evidence}
                            for hook, verdict in verdicts.items()
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            facts = _host_facts()
            print(
                f"host: kernel {facts['kernel']} clang {facts['clang']} "
                f"libbpf {facts['libbpf']}"
            )
            print(f"btf: {facts['btf_path']} ({facts['btf_bytes']} bytes, parsed)")
            print("signature verdicts:")
            for hook, verdict in verdicts.items():
                print(f"  {hook}: {verdict.status}")
                print(f"    {verdict.evidence}")
        return 0 if all(verdict.confirmed for verdict in verdicts.values()) else 1

    if not args.run:
        parser.print_usage(sys.stderr)
        print(
            "choose --procedure (runbook), --check (BTF evidence, no root) or --run "
            "(privileged acceptance)",
            file=sys.stderr,
        )
        return 2

    try:
        report = run_acceptance(environ=environ, effective_uid=os.geteuid())
    except AcceptanceRefused as refused:
        print(refused.reason)
        return 2
    print(
        json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report)
    )
    return 0 if report["verdict"] == "CONFIRMED" else 1


# ---------------------------------------------------------------------------
# Synthetic BTF, so the confirmation and refutation logic is falsifiable rootless
# ---------------------------------------------------------------------------


def _btf_strings(names: list[str]) -> tuple[bytes, dict[str, int]]:
    blob = bytearray(b"\0")
    offsets: dict[str, int] = {}
    for name in names:
        if not name:
            continue
        offsets[name] = len(blob)
        blob.extend(name.encode() + b"\0")
    return bytes(blob), offsets


class _BtfBuilder:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.types: list[tuple[str, int, int, int, bytes]] = []

    def add(self, name: str, kind: int, vlen: int, size_or_type: int, payload: bytes = b"") -> int:
        if name and name not in self.names:
            self.names.append(name)
        self.types.append((name, kind, vlen, size_or_type, payload))
        return len(self.types)

    def int_type(self, name: str, size: int, signed: bool) -> int:
        return self.add(name, _KIND_INT, 0, size, bytes([1 if signed else 0, 0, 0, 0]))

    def struct_type(self, name: str) -> int:
        return self.add(name, _KIND_STRUCT, 0, 4)

    def ptr(self, inner: int) -> int:
        return self.add("", _KIND_PTR, 0, inner)

    def const(self, inner: int) -> int:
        return self.add("", _KIND_CONST, 0, inner)

    def typedef(self, name: str, inner: int) -> int:
        return self.add(name, _KIND_TYPEDEF, 0, inner)

    def func_proto(self, return_type: int, params: list[tuple[str, int]]) -> int:
        payload = b"".join(struct.pack("<II", 0, type_id) for _name, type_id in params)
        return self.add("", _KIND_FUNC_PROTO, len(params), return_type, payload)

    def func(self, name: str, proto: int) -> int:
        return self.add(name, _KIND_FUNC, 0, proto)

    def build(self) -> BtfImage:
        strings, offsets = _btf_strings(self.names)
        body = bytearray()
        for name, kind, vlen, size_or_type, payload in self.types:
            body.extend(
                struct.pack(
                    "<III", offsets.get(name, 0), (vlen & 0xFFFF) | (kind << 24), size_or_type
                )
            )
            body.extend(payload)
        header = _BTF_HEADER.pack(
            BTF_MAGIC, 1, 0, _BTF_HEADER.size, 0, len(body), len(body), len(strings)
        )
        return BtfImage.parse(header + bytes(body) + strings)


def synthetic_btf(
    hooks,
    *,
    drop_flags: tuple[str, ...] = (),
    narrow_vlen: tuple[str, ...] = (),
    unsigned_buf: bool = False,
    swapped_file_pointer: bool = False,
) -> BtfImage:
    """Build a miniature BTF that mirrors the six kernel prototypes.

    Used only by the rootless tests: it proves the confirmation *and* the
    refutation path without reading live BTF or loading BPF.
    """
    builder = _BtfBuilder()
    long_int = builder.int_type("long int", 8, True)
    ssize_t = builder.typedef("ssize_t", long_int)
    file_ptr = builder.ptr(builder.struct_type("file"))
    iovec_ptr = builder.ptr(builder.const(builder.struct_type("iovec")))
    unsigned_long = builder.int_type("long unsigned int", 8, False)
    unsigned_int = builder.int_type("unsigned int", 4, False)
    loff_t_ptr = builder.ptr(builder.typedef("loff_t", builder.int_type("long long int", 8, True)))
    rwf_t = builder.typedef("rwf_t", unsigned_int)
    char_ptr = builder.ptr(builder.int_type("char", 1, True))
    const_char_ptr = builder.ptr(builder.const(builder.int_type("char", 1, True)))
    unsigned_char_ptr = builder.ptr(builder.int_type("unsigned char", 1, False))
    inode_ptr = builder.ptr(builder.struct_type("inode"))
    size_t = builder.typedef("size_t", unsigned_long)
    iov_iter_ptr = builder.ptr(builder.struct_type("iov_iter"))
    narrowed_int = builder.int_type("int", 4, False)

    file_pointer = inode_ptr if swapped_file_pointer else file_ptr
    buf_pointer = unsigned_char_ptr if unsigned_buf else char_ptr
    write_buf_pointer = (
        builder.ptr(builder.const(builder.int_type("unsigned char", 1, False)))
        if unsigned_buf
        else const_char_ptr
    )
    read_four = [
        ("file", file_pointer),
        ("buf", buf_pointer),
        ("count", size_t),
        ("pos", loff_t_ptr),
    ]
    write_four = [
        ("file", file_pointer),
        ("buf", write_buf_pointer),
        ("count", size_t),
        ("pos", loff_t_ptr),
    ]
    five = [
        ("file", file_ptr),
        ("vec", iovec_ptr),
        ("vlen", unsigned_long),
        ("pos", loff_t_ptr),
        ("flags", rwf_t),
    ]
    iter_four = [("file", file_ptr), ("iter", iov_iter_ptr), ("ppos", loff_t_ptr), ("flags", rwf_t)]
    for hook in hooks:
        if hook == "vfs_read":
            params = read_four
        elif hook == "vfs_write":
            params = write_four
        elif hook in ("vfs_readv", "vfs_writev"):
            params = five
            if hook in narrow_vlen:
                params = [five[0], five[1], ("vlen", narrowed_int), five[3], five[4]]
            if hook in drop_flags:
                params = params[:-1]
        elif hook in ("vfs_iter_read", "vfs_iter_write"):
            params = iter_four
        else:  # pragma: no cover - guards a typo in a test
            raise AssertionError(f"unknown hook {hook}")
        builder.func(hook, builder.func_proto(ssize_t, params))
    return builder.build()


# ---------------------------------------------------------------------------
# Rootless test suite: the gate, the BTF verdicts, and T/D independence
# ---------------------------------------------------------------------------


def test_acceptance_harness_refuses_without_opt_in(monkeypatch) -> None:
    """Falsifier: deleting the opt-in check makes the default suite run BPF."""
    spawned: list[object] = []

    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        spawned.append((args, kwargs))
        raise AssertionError("the harness spawned a process without the opt-in variable")

    def forbidden_btf(path=BTF_PATH):  # pragma: no cover - must never run
        raise AssertionError("the harness read live BTF without the opt-in variable")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    gate = evaluate_gate(environ={}, effective_uid=0, btf_loader=forbidden_btf)
    assert gate.allowed is False
    assert gate.opted_in is False
    assert OPT_IN_ENV in gate.reason
    assert "NOT PERFORMED" in gate.reason

    with pytest.raises(AcceptanceRefused) as refused:
        run_acceptance(environ={}, effective_uid=0, btf_loader=forbidden_btf)
    assert "NOT PERFORMED" in str(refused.value)
    assert spawned == []

    assert main(["--run"], environ={}) == 2
    assert spawned == []


def test_acceptance_harness_refuses_non_root_even_when_opted_in(monkeypatch) -> None:
    """Falsifier: dropping the effective-UID check makes a non-root load look valid."""

    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the harness spawned a process without root")

    def forbidden_btf(path=BTF_PATH):  # pragma: no cover - must never run
        raise AssertionError("the harness read BTF for a non-root run")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    gate = evaluate_gate(environ={OPT_IN_ENV: "1"}, effective_uid=1000, btf_loader=forbidden_btf)
    assert gate.opted_in is True
    assert gate.allowed is False
    assert "root" in gate.reason.lower()
    assert "NOT PERFORMED" in gate.reason

    with pytest.raises(AcceptanceRefused):
        run_acceptance(environ={OPT_IN_ENV: "1"}, effective_uid=1000, btf_loader=forbidden_btf)


def test_acceptance_harness_refuses_when_btf_is_unreadable() -> None:
    """Falsifier: treating an unreadable BTF as confirmation rather than refusal."""

    def unreadable(path=BTF_PATH):
        raise OSError(errno.EACCES, "Permission denied", str(path))

    gate = evaluate_gate(environ={OPT_IN_ENV: "1"}, effective_uid=0, btf_loader=unreadable)
    assert gate.allowed is False
    assert "btf" in gate.reason.lower()
    assert "NOT PERFORMED" in gate.reason

    with pytest.raises(AcceptanceRefused):
        run_acceptance(environ={OPT_IN_ENV: "1"}, effective_uid=0, btf_loader=unreadable)


def test_acceptance_harness_refuses_when_a_target_symbol_is_absent() -> None:
    """Falsifier: accepting a kernel that cannot resolve all six fexit targets."""
    image = synthetic_btf(("vfs_read", "vfs_write", "vfs_iter_read", "vfs_iter_write"))
    gate = evaluate_gate(
        environ={OPT_IN_ENV: "1"}, effective_uid=0, btf_loader=lambda path=BTF_PATH: image
    )
    assert gate.allowed is False
    assert "vfs_readv" in gate.reason and "vfs_writev" in gate.reason
    assert "NOT PERFORMED" in gate.reason


def test_btf_signature_check_confirms_assumed_vectored_prototypes() -> None:
    """The reading of BTF is the evidence: confirm when it matches T-001's guess."""
    image = synthetic_btf(HOOK_SYMBOLS)
    verdicts = signature_verdicts(image)
    assert set(verdicts) == set(HOOK_SYMBOLS)
    for hook in ("vfs_readv", "vfs_writev"):
        verdict = verdicts[hook]
        assert verdict.status == "CONFIRMED", verdict.evidence
        assert verdict.kernel_params[-1] == verdict.assumed_params[-1]
        assert "FUNC" in verdict.evidence and "FUNC_PROTO" in verdict.evidence
    assert all(verdict.status == "CONFIRMED" for verdict in verdicts.values())


def test_btf_signature_check_refutes_a_dropped_flags_parameter() -> None:
    """Falsifier: a positional mismatch must REFUTE, never silently confirm."""
    image = synthetic_btf(HOOK_SYMBOLS, drop_flags=("vfs_readv", "vfs_writev"))
    verdicts = signature_verdicts(image)
    for hook in ("vfs_readv", "vfs_writev"):
        verdict = verdicts[hook]
        assert verdict.status == "REFUTED", verdict.evidence
        assert len(verdict.kernel_params) == 4
        assert len(verdict.assumed_params) == 5
        assert "REFUTED" in verdict.evidence


def test_btf_signature_check_refutes_a_narrowed_vlen_parameter() -> None:
    """Falsifier: comparing names instead of resolved kinds/widths would miss this."""
    image = synthetic_btf(HOOK_SYMBOLS, narrow_vlen=("vfs_readv",))
    verdict = signature_verdicts(image)["vfs_readv"]
    assert verdict.status == "REFUTED", verdict.evidence
    assert verdict.kernel_params[2] != verdict.assumed_params[2]


def test_btf_signature_check_reports_absent_symbol_as_unverified() -> None:
    """A missing symbol is UNVERIFIED, not CONFIRMED and not REFUTED."""
    image = synthetic_btf(("vfs_read", "vfs_write", "vfs_iter_read", "vfs_iter_write"))
    verdicts = signature_verdicts(image)
    assert verdicts["vfs_readv"].status == "UNVERIFIED"
    assert "absent" in verdicts["vfs_readv"].evidence.lower()


def test_btf_parser_rejects_a_truncated_blob() -> None:
    """A short BTF must be refused, not partially trusted as evidence."""
    with pytest.raises(BtfError):
        BtfImage.parse(b"\x9f\xeb\x01\x00")
    whole = synthetic_btf(HOOK_SYMBOLS)
    header = _BTF_HEADER.pack(
        BTF_MAGIC, 1, 0, _BTF_HEADER.size, 0, 4096, 4096, len(whole.strings)
    )
    with pytest.raises(BtfError):
        BtfImage.parse(header + b"\0" * 8)


def test_btf_signature_check_accepts_a_signedness_only_difference() -> None:
    """The kernel spells ``char *buf`` as ``unsigned char *``: ABI-equal, noted.

    Falsifier for the opposite error: refuting an attachable prototype would make
    every run on a real kernel look like a signature failure.
    """
    verdicts = signature_verdicts(synthetic_btf(HOOK_SYMBOLS, unsigned_buf=True))
    for hook in ("vfs_read", "vfs_write"):
        verdict = verdicts[hook]
        assert verdict.status == "CONFIRMED", verdict.evidence
        assert verdict.notes and "ABI-equivalent spelling at param 1" in verdict.notes[0]
    assert "unsigned char *" in verdicts["vfs_read"].evidence


def test_btf_signature_check_refutes_a_swapped_struct_pointer() -> None:
    """A different pointee struct is a real difference, not a spelling note.

    Falsifier: treating every pointer difference as ABI-equivalent would accept a
    prototype whose CO-RE field reads cannot work.
    """
    verdict = signature_verdicts(
        synthetic_btf(HOOK_SYMBOLS, swapped_file_pointer=True)
    )["vfs_read"]
    assert verdict.status == "REFUTED", verdict.evidence
    assert "param 0" in verdict.evidence and "struct inode" in verdict.evidence


def test_known_blocker_names_the_owning_task_for_a_libbpf_opts_failure() -> None:
    """Falsifier: a bare libbpf string with no owner leaves the operator stuck."""
    blockers = _known_blockers("libbpf: bpf_object_open_opts size (0) is too small")
    assert blockers and "T-001" in blockers[0] and "LIBBPF_OPTS" in blockers[0]
    assert _known_blockers(None) == []
    assert _known_blockers("unrelated libbpf message") == []


def test_assumed_signature_table_matches_the_task_1_bpf_source() -> None:
    """Falsifier: hardcoding a different guess than the shipped BPF programs."""
    from_source = assumed_signatures_from_source(BPF_SOURCE)
    assert from_source == ASSUMED_SIGNATURES


def test_workload_accounting_counts_only_positive_returns() -> None:
    """Falsifier: counting a failed or zero-length VFS operation as traffic.

    The zero/negative returns deliberately use counted operation *names*, so
    deleting the ``ret <= 0`` guard changes the expected totals instead of being
    masked by an unrecognised name.
    """
    ops = [
        {"op": "write", "ret": 40},
        {"op": "read", "ret": 40},
        {"op": "writev", "ret": 40},
        {"op": "readv", "ret": 40},
        {"op": "read", "ret": 0, "detail": "eof"},
        {"op": "read", "ret": None, "errno": errno.EISDIR, "detail": "directory"},
        {"op": "readv", "ret": -errno.EISDIR},
        {"op": "write", "ret": 0},
    ]
    counters = expected_workload_counters(ops)
    assert counters == {"reads": 2, "writes": 2, "bytes_r": 80, "bytes_w": 80}


def test_choose_scratch_dir_refuses_a_tmpfs_only_host(monkeypatch) -> None:
    """Falsifier: silently accepting a tmpfs scratch, which the producer skips."""
    monkeypatch.setattr(sys.modules[__name__], "_is_block_backed", lambda path: False)
    with pytest.raises(AcceptanceError) as refused:
        choose_scratch_dir({SCRATCH_ENV: "/tmp"})
    assert "block-backed" in str(refused.value)


def test_unavailable_file_source_keeps_process_and_device_stream_answering(
    tmp_path, monkeypatch
) -> None:
    """Falsifier: F's absence advertising zeroes or making T/D fail."""
    from appio.bpf_fileio import BpfFileSource, VectorLoader
    from appio.iod import Daemon, DaemonPaths
    from appio.ipc_protocol import FrameDecoder
    from tests.p3.test_iod import Clock, DeferredWorker, FakeFilesystem, PollSelector, write_io

    def forbidden():  # pragma: no cover - must never run
        raise AssertionError("an acceptance test reached the installed privileged loader")

    monkeypatch.setattr("appio.file_runtime.default_file_source", forbidden)

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    shutil.copytree(fixtures / "proc", proc, symlinks=True)
    shutil.copytree(fixtures / "cgroup/normal-t0", cgroup)
    shutil.rmtree(proc / "500")
    shutil.copyfile(fixtures / "cgroup/proc/diskstats", proc / "diskstats")
    write_io(proc, 100)
    state, run = tmp_path / "state", tmp_path / "run"
    state.mkdir(mode=0o711)
    run.mkdir(mode=0o750)
    clock = Clock()
    # Five of six hooks: exactly the attach failure this acceptance must survive.
    loader = VectorLoader(hook_mask=0x1F)
    daemon = Daemon(
        DaemonPaths(state / "iod.db", run / "iod.sock", proc, cgroup),
        clock=clock,
        filesystem=FakeFilesystem(),
        selector=PollSelector(),
        query_worker=DeferredWorker(),
        file_source_factory=lambda: BpfFileSource(loader),
    )
    try:
        daemon.start()
        first = daemon.committed_cursor
        write_io(proc, 200)
        clock.advance(10)
        daemon.step()
        message = FrameDecoder().feed(daemon._current.frame)[0]
        assert daemon.committed_cursor.seq > first.seq
        assert message["procs"] and message["devs"]
        assert message["capabilities"]["file_io"] is False
        assert message["capabilities"]["bpf_loaded"] is False
        source = message["coverage"]["file_source"]
        assert source["reason"] == "file_source_unavailable"
        assert source["diagnostic"] and len(source["diagnostic"]) <= 512
        assert daemon.last_error is None
    finally:
        daemon.stop()
        daemon.selector.close()


@pytest.fixture
def drain_evaluation():
    """Falsifiers for the drain assertions that only run on a privileged host.

    A lost ``i_generation``, a count that includes a failed operation, a slot that
    was not cleared (so a replayed or nonpositive record still appears), or an
    attribution to the wrong cgroup must each produce a named failure.
    """
    good = {
        "cgroup_id": 7,
        "dev": "259:6",
        "inode": 42,
        "generation": 99,
        "basename": "acceptance-payload.bin",
        "reads": 2,
        "writes": 2,
        "bytes_r": 76,
        "bytes_w": 76,
    }

    def evaluate(first, second, *, isolated=True, slots=(0, 1), complete=(1, 1),
                 retired_events=(0, 0), retired_stats=(0, 0)):
        post_drain = []
        for index, slot in enumerate(slots):
            counts = [{"events": 0, "stats": 0}, {"events": 0, "stats": 0}]
            counts[slot] = {"events": retired_events[index], "stats": retired_stats[index]}
            post_drain.append(counts)
        report = {
            "workload": {
                "expected": {"reads": 2, "writes": 2, "bytes_r": 76, "bytes_w": 76},
                "expected_basename": "acceptance-payload.bin",
                "expected_dev": "259:6",
                "expected_inode": 42,
                "cgroup_id": 7,
                "cgroup_isolated": isolated,
                "second_pass_records": [],
            },
            "drains": [
                {
                    "complete": complete[0],
                    "drained_slot": slots[0],
                    "events": first,
                    "post_drain_slots": post_drain[0],
                    "stats": [],
                },
                {
                    "complete": complete[1],
                    "drained_slot": slots[1],
                    "events": second,
                    "post_drain_slots": post_drain[1],
                    "stats": [],
                },
            ],
        }
        failures: list[str] = []

        def check(name, passed, detail=""):
            if not passed:
                failures.append(name)
            return passed

        _evaluate_drains(report, check, "259:6", 42, 99)
        return failures

    return good, evaluate


def test_drain_evaluation_rejects_identity_byte_slot_and_ownership_regressions(drain_evaluation) -> None:
    good, evaluate = drain_evaluation
    assert evaluate([dict(good)], []) == []
    assert "file_generation" in evaluate([dict(good, generation=1)], [])
    assert "file_inode" not in evaluate([dict(good)], [])  # guarded by the record match
    assert "file_read_bytes" in evaluate([dict(good, bytes_r=114, reads=3)], [])
    assert "file_write_ops" in evaluate([dict(good, writes=3)], [])
    assert "file_name_matches" in evaluate([dict(good, basename="other.bin")], [])
    assert "file_cgroup_attribution" in evaluate([dict(good, cgroup_id=8)], [])
    assert "nonpositive_adds_no_record" in evaluate([dict(good)], [dict(good)])
    assert "retired_slot_cleared" not in evaluate([dict(good)], [dict(good)])
    assert "retired_slot_cleared" in evaluate([dict(good)], [], isolated=False, retired_events=(0, 55))
    assert "file_record_present" in evaluate([], [])
    assert "drain_slot_rotated" in evaluate([dict(good)], [], slots=(1, 1))
    assert "drain_complete" in evaluate([dict(good)], [], complete=(1, 0))


def test_post_drain_counts_follow_inner_ids_and_count_each_slot(tmp_path, monkeypatch) -> None:
    # The two stats inner names collide with the outer's kernel-truncated name.
    layout = {
        11: (b"fileio_slots", 12, 4, 2, None),
        12: (b"fileio_stats_sl", 12, 4, 2, None),
        21: (b"fileio_slot_0", 1, 32, 262144, 0),
        22: (b"fileio_slot_1", 1, 32, 262144, 7),
        31: (b"fileio_stats_sl", 1, 8, 65536, 0),
        32: (b"fileio_stats_sl", 1, 8, 65536, 1),
    }
    for index, map_id in enumerate((31, 11, 12, 32, 11)):
        (tmp_path / str(index)).write_text(f"map_id:\t{map_id}\n")
    (tmp_path / "socket").write_text("ino:\t123\n")
    opened, closed = [], []
    monkeypatch.setattr(os, "close", closed.append)

    class FakeMaps:
        def bpf_map_get_fd_by_id(self, map_id):
            assert map_id in layout
            fd = map_id + 1000  # Deliberately distinct from the kernel map ID.
            opened.append(fd)
            return fd

        def bpf_obj_get_info_by_fd(self, fd, value, size):
            name, kind, key_size, capacity, _ = layout[fd - 1000]
            value = ctypes.cast(value, ctypes.POINTER(_MapInfo)).contents
            value.name, value.type, value.key_size, value.max_entries = name, kind, key_size, capacity
            assert ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32)).contents.value == 40
            return 0

        def bpf_map_lookup_elem(self, fd, key, value):
            assert fd in (1011, 1012)
            slot = ctypes.cast(key, ctypes.POINTER(ctypes.c_uint32)).contents.value
            ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32)).contents.value = (
                (21, 22) if fd == 1011 else (31, 32)
            )[slot]
            return 0

        def bpf_map_get_next_key(self, fd, current, following):
            _, _, key_size, _, count = layout[fd - 1000]
            assert count is not None
            index = 0 if current is None else int.from_bytes(ctypes.string_at(current, key_size), "little") + 1
            if index == count:
                ctypes.set_errno(errno.ENOENT)
                return -errno.ENOENT
            ctypes.memmove(following, index.to_bytes(key_size, "little"), key_size)
            return 0

    counts = _post_drain_slot_counts(123, fdinfo_root=tmp_path, libbpf=FakeMaps())
    assert counts == [
        {"events": 0, "events_map_id": 21, "stats": 0, "stats_map_id": 31},
        {"events": 7, "events_map_id": 22, "stats": 1, "stats_map_id": 32},
    ]
    assert sorted(opened) == sorted(closed)


def test_drain_evaluation_allows_background_records_in_the_other_slot(drain_evaluation) -> None:
    good, evaluate = drain_evaluation
    background = dict(good, cgroup_id=999, inode=9999, basename="unrelated.log")
    assert evaluate([dict(good)], [background]) == []


def test_drain_evaluation_requires_the_retired_inner_maps_to_be_empty(drain_evaluation) -> None:
    good, evaluate = drain_evaluation
    assert "retired_slot_cleared" in evaluate([dict(good)], [], retired_events=(55, 0))
    assert "retired_slot_cleared" in evaluate([dict(good)], [], retired_stats=(0, 1))


def test_counter_evaluation_rejects_a_dead_cgroup_row_and_hidden_overflow() -> None:
    """Falsifiers for the counter assertions: silent zero hits, hidden overflow."""
    hits = {"vfs_read": 2, "vfs_write": 2, "vfs_readv": 1, "vfs_writev": 1,
            "vfs_iter_read": 0, "vfs_iter_write": 0}
    row = {
        "cgroup_id": 7,
        "is_unknown_owner": False,
        "hook_hits": dict(hits),
        "overflow_files": 0,
        "overflow_bytes_r": 0,
        "overflow_bytes_w": 0,
        "skipped_nonfile": 0,
        "skipped_unsupported_fs": 0,
    }
    workload = WorkloadResult(
        mode="full",
        ops=[],
        payload_len=38,
        counters={"reads": 2, "writes": 2, "bytes_r": 76, "bytes_w": 76},
    )
    nonpositive = WorkloadResult(
        mode="nonpositive",
        ops=[{"op": "read", "ret": 0}, {"op": "read", "ret": None, "errno": errno.EISDIR}],
        payload_len=38,
        counters={"reads": 0, "writes": 0, "bytes_r": 0, "bytes_w": 0},
    )
    cgroup = CgroupContext(Path("/sys/fs/cgroup/x"), 7, True, "isolated")

    def evaluate(stats, *, isolated=True):
        report = {
            "workload": {"cgroup_id": 7},
            "drains": [{"stats": stats, "events": []}],
            "per_cgroup": [],
            "per_file": [],
            "unknown_owner": None,
            "per_hook_hits": {},
            "overflow": {},
            "skipped": {},
        }
        failures: list[str] = []

        def check(name, passed, detail=""):
            if not passed:
                failures.append(name)
            return passed

        _evaluate_counters(
            report,
            check,
            replace(cgroup, isolated=isolated),
            workload,
            nonpositive,
        )
        return failures, report

    failures, report = evaluate([dict(row)])
    assert failures == [], failures
    assert report["per_hook_hits"] == hits
    assert "cgroup_stats_present" in evaluate([])[0]
    assert "hook_hits_observed" in evaluate([dict(row, hook_hits=dict.fromkeys(hits, 0))])[0]
    assert "hook_hits_internally_consistent" in evaluate([dict(row, hook_hits={"vfs_read": 1, "vfs_write": 0, "vfs_readv": 0, "vfs_writev": 0, "vfs_iter_read": 0, "vfs_iter_write": 0})])[0]
    assert "no_overflow_in_isolated_cgroup" in evaluate([dict(row, overflow_files=1)])[0]
    assert "no_overflow_in_isolated_cgroup" not in evaluate([dict(row, overflow_files=1)], isolated=False)[0]
    assert report["unknown_owner"] is None
    unknown_row = dict(row, cgroup_id=0, is_unknown_owner=True, overflow_files=3)
    _failures, report = evaluate([dict(row), unknown_row])
    assert report["unknown_owner"]["overflow_files"] == 3
    assert report["unknown_owner"]["is_unknown_owner"] is True


def test_opt_in_privileged_acceptance_confirms_the_real_object_and_workload() -> None:
    """The single privileged step: skipped, never passed, unless a human opts in."""
    gate = evaluate_gate(environ=os.environ, effective_uid=os.geteuid())
    if not gate.allowed:
        pytest.skip(gate.reason)

    report = run_acceptance(environ=os.environ, effective_uid=os.geteuid())

    assert report["verdict"] == "CONFIRMED", format_report(report)
    assert report["attach"]["hook_mask"] == HOOK_MASK_ALL
    assert all(report["attach"]["hooks"].values())
    for hook in HOOK_SYMBOLS:
        assert report["signatures"][hook]["status"] == "CONFIRMED", report["signatures"][hook]
    first, second = report["drains"]
    assert first["complete"] == 1 and second["complete"] == 1
    assert first["drained_slot"] != second["drained_slot"]
    assert report["workload"]["second_pass_records"] == []
    for drained in (first, second):
        retired = drained["post_drain_slots"][drained["drained_slot"]]
        assert retired["events"] == 0 and retired["stats"] == 0
    record = report["workload"]["record"]
    expected = report["workload"]["expected"]
    assert record["dev"] == report["workload"]["expected_dev"]
    assert record["inode"] == report["workload"]["expected_inode"]
    assert record["basename"] == report["workload"]["expected_basename"]
    assert record["bytes_r"] == expected["bytes_r"]
    assert record["bytes_w"] == expected["bytes_w"]
    assert record["reads"] == expected["reads"]
    assert record["writes"] == expected["writes"]
    hits = report["per_hook_hits"]
    assert set(hits) == set(HOOK_SYMBOLS)
    assert all(isinstance(value, int) and value >= 0 for value in hits.values())
    assert report["diagnostics"]["verifier"] in (None, "")


if __name__ == "__main__":  # pragma: no cover - the operator entry point
    raise SystemExit(main())
