"""Contract tests for the source-tarball export and its recorded digest.

`packaging/arch/build-package.sh` exports the tracked tree, then refuses to build
unless the digest recorded in `PKGBUILD` matches. Nothing tested it, so the
recorded value rotted three times without anyone noticing: a `pkgver` bump that
rewrote every path in the tarball, a `.gitignore` edit (the file is inside the
export), and a transient `.worktrees/` directory. The last one is the subtle
case -- it changed the digest while changing zero packaged source files, because
the export walked the working directory instead of asking git what is tracked.

These tests pin the two properties that make the digest meaningful: ignored
scratch space cannot move it, and real source edits must. They also pin the new
refusal, because a tracked-only export would otherwise drop an untracked file
silently -- the exact failure the directory walk was hiding.

A fourth cause was found only after the first fix: the tarball timestamp came
from `git log -1`, so *any* commit moved the digest, including the commit that
records it. Recording and committing then cannot converge. The timestamp is now
fixed, and `test_recording_the_digest_does_not_invalidate_it` holds that line.

The fixture is a throwaway checkout rather than this repository: the point is
the export's behaviour, and it must not depend on the state or commit history of
the tree running the suite.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REAL_SCRIPT = (
    Path(__file__).resolve().parents[1] / "packaging/arch/build-package.sh"
)

# Must match the ignores the export relies on: the fixture needs a directory that
# git treats as scratch, so the invariance test exercises the real mechanism.
GITIGNORE = "__pycache__/\n*.pyc\n.worktrees/\n"

PKGBUILD = """\
pkgname=appio
pkgver={version}
pkgrel=1
source=("appio-$pkgver.tar.gz")
sha256sums=('{digest}')
"""

# Forced apart so a HEAD-derived timestamp has something to move: commits land in
# well under a second, and git timestamps resolve to the second.
INITIAL_COMMIT_DATE = "2026-01-01T00:00:00 +0000"
RECORD_COMMIT_DATE = "2026-01-02T00:00:00 +0000"


def _git(checkout: Path, *args: str, date: str | None = None) -> None:
    env = dict(os.environ)
    if date is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=packaging test",
            "-c",
            "user.email=packaging@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_checkout(root: Path, *, version: str = "0.1.2", digest: str = "0" * 64) -> Path:
    checkout = root / "checkout"
    (checkout / "packaging" / "arch").mkdir(parents=True)
    (checkout / "src" / "pkg").mkdir(parents=True)

    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "pkg"\nversion = "{version}"\n', encoding="utf-8"
    )
    (checkout / "src" / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (checkout / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (checkout / "packaging" / "arch" / "PKGBUILD").write_text(
        PKGBUILD.format(version=version, digest=digest), encoding="utf-8"
    )

    script = checkout / "packaging" / "arch" / "build-package.sh"
    script.write_bytes(REAL_SCRIPT.read_bytes())
    script.chmod(0o755)

    _git(checkout, "init", "-q")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "fixture", date=INITIAL_COMMIT_DATE)
    return checkout


def _run(checkout: Path, builddir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(checkout / "packaging" / "arch" / "build-package.sh"), *args],
        cwd=checkout,
        env={**os.environ, "APPIO_PKGBUILD_DIR": str(builddir)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _recorded_digest(checkout: Path) -> str:
    for line in (checkout / "packaging" / "arch" / "PKGBUILD").read_text(
        encoding="utf-8"
    ).splitlines():
        if line.startswith("sha256sums=('"):
            return line[len("sha256sums=('") : -2]
    raise AssertionError("fixture PKGBUILD has no sha256sums")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return _make_checkout(tmp_path)


def test_export_is_versioned_under_one_top_directory(
    checkout: Path, tmp_path: Path
) -> None:
    """makepkg extracts one directory and PKGBUILD cds into it by name."""
    builddir = tmp_path / "build"
    result = _run(checkout, builddir, "--update", "--check")
    assert result.returncode == 0, result.stderr

    listing = subprocess.run(
        ["tar", "-tzf", str(builddir / "appio-0.1.2.tar.gz")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert listing, "export produced an empty tarball"
    assert all(name.startswith("appio-0.1.2/") for name in listing), listing


def test_ignored_scratch_space_does_not_move_the_digest(
    checkout: Path, tmp_path: Path
) -> None:
    """A worktree dir and bytecode must not invalidate a recorded digest."""
    builddir = tmp_path / "build"
    assert _run(checkout, builddir, "--update", "--check").returncode == 0

    (checkout / ".worktrees" / "fix").mkdir(parents=True)
    (checkout / ".worktrees" / "fix" / "scratch.py").write_text("X = 1\n", encoding="utf-8")
    cache = checkout / "src" / "pkg" / "__pycache__"
    cache.mkdir()
    (cache / "pkg.cpython-314.pyc").write_bytes(b"\x00\x01")

    result = _run(checkout, builddir, "--check")
    assert result.returncode == 0, result.stderr


def test_editing_a_tracked_file_moves_the_digest(
    checkout: Path, tmp_path: Path
) -> None:
    """The inverse property: the digest must still describe real source."""
    builddir = tmp_path / "build"
    assert _run(checkout, builddir, "--update", "--check").returncode == 0

    target = checkout / "src" / "pkg" / "__init__.py"
    target.write_text(target.read_text(encoding="utf-8") + "VALUE = 2\n", encoding="utf-8")

    result = _run(checkout, builddir, "--check")
    assert result.returncode != 0
    assert "source digest changed" in result.stderr


def test_untracked_file_is_refused_rather_than_dropped(
    checkout: Path, tmp_path: Path
) -> None:
    """A tracked-only export could silently omit work, so it must abort."""
    (checkout / "NOTES.md").write_text("scratch\n", encoding="utf-8")

    result = _run(checkout, tmp_path / "build", "--check")
    assert result.returncode != 0
    assert "NOTES.md" in result.stderr
    assert "untracked" in result.stderr


def test_missing_tracked_file_is_refused(checkout: Path, tmp_path: Path) -> None:
    """A staged-delete would make tar fail mid-export; refuse up front."""
    (checkout / "src" / "pkg" / "__init__.py").unlink()

    result = _run(checkout, tmp_path / "build", "--check")
    assert result.returncode != 0
    assert "absent from the working tree" in result.stderr


def test_recording_the_digest_does_not_invalidate_it(
    checkout: Path, tmp_path: Path
) -> None:
    """The record is itself a commit; if that commit moves the digest, the
    record-and-commit workflow can never converge."""
    builddir = tmp_path / "build"
    assert _run(checkout, builddir, "--update", "--check").returncode == 0

    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "record the digest", date=RECORD_COMMIT_DATE)

    result = _run(checkout, builddir, "--check")
    assert result.returncode == 0, result.stderr


def test_update_records_a_digest_that_reproduces(
    checkout: Path, tmp_path: Path
) -> None:
    """--update must leave a value that a later --check accepts unchanged."""
    builddir = tmp_path / "build"
    assert _run(checkout, builddir, "--update", "--check").returncode == 0
    recorded = _recorded_digest(checkout)

    recheck = _run(checkout, builddir, "--check")
    assert recheck.returncode == 0, recheck.stderr
    assert recorded in recheck.stdout
