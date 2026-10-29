"""Tests for ``scripts/ci/list_os_marked_tests.py``.

The helper decides which files the macOS / Windows CI lanes import. Its
failure modes matter more than its happy path: if it silently returned an
empty list, the OS lane would run zero tests and still report green — the
exact silent-coverage-loss the lanes exist to prevent. So the contracts under
test are "finds real markers", "refuses to emit nothing", and "rejects an
unknown marker".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "list_os_marked_tests.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )


def _write(root: Path, relpath: str, body: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "marker",
    ["linux", "macos", "windows", "linux_only", "macos_only", "windows_only"],
)
def test_finds_decorator_and_pytestmark_forms(tmp_path, marker):
    """Decorator and module-level forms are detected, legacy and composable.

    Short names (linux/macos/windows) match the composable
    ``platforms("...")`` spec form; the ``_only`` names match the legacy
    decorator/``pytestmark`` forms. Both must also select the other form's
    files when present, so the macOS lane keeps working during the
    incremental migration.
    """
    short = marker in ("linux", "macos", "windows")
    # The canonical form for this marker.
    if short:
        decorated = f'import pytest\n\n\n@pytest.mark.platforms("{marker}")\ndef test_x():\n    pass\n'
        pytestmark_body = f'import pytest\n\npytestmark = pytest.mark.platforms("{marker}")\n\n\ndef test_y():\n    pass\n'
        query = marker
    else:
        decorated = f"import pytest\n\n\n@pytest.mark.{marker}\ndef test_x():\n    pass\n"
        pytestmark_body = f"import pytest\n\npytestmark = pytest.mark.{marker}\n\n\ndef test_y():\n    pass\n"
        query = marker
    _write(tmp_path, "test_decorated.py", decorated)
    _write(tmp_path, "nested/test_module_level.py", pytestmark_body)
    # A file with no marker at all must not be selected.
    _write(tmp_path, "test_plain.py", "def test_z():\n    pass\n")
    # A platforms() file naming a DIFFERENT platform must not be selected.
    other = "windows" if marker.rstrip("_only") != "windows" else "linux"
    _write(
        tmp_path,
        "test_other_platform.py",
        f'import pytest\n\n\n@pytest.mark.platforms("{other}")\ndef test_w():\n    pass\n',
    )

    result = _run(query, str(tmp_path))

    assert result.returncode == 0, result.stderr
    listed = result.stdout.split()
    assert any(p.endswith("test_decorated.py") for p in listed)
    assert any(p.endswith("test_module_level.py") for p in listed)
    assert not any(p.endswith("test_plain.py") for p in listed)
    assert not any(p.endswith("test_other_platform.py") for p in listed)


def test_negated_spec_lists_for_that_lane(tmp_path):
    """``platforms("not macos")`` lists the file for the macOS lane import.

    The script only decides which files are IMPORTED; the lane's ``-m``
    filter remains authoritative. A negated spec file may still contain
    other tests, so it must be imported on the macOS lane — under-selecting
    is the failure mode this tool exists to prevent.
    """
    _write(
        tmp_path,
        "test_negated.py",
        'import pytest\n\n\n@pytest.mark.platforms("not macos")\ndef test_x():\n    pass\n',
    )

    result = _run("macos", str(tmp_path))

    assert result.returncode == 0, result.stderr
    listed = result.stdout.split()
    assert any(p.endswith("test_negated.py") for p in listed)


def test_marker_matched_as_whole_word(tmp_path):
    """``macos_only`` must not match a longer identifier that contains it."""
    _write(
        tmp_path,
        "test_lookalike.py",
        "import pytest\n\n\n@pytest.mark.macos_only_extra\ndef test_x():\n    pass\n",
    )

    result = _run("macos_only", str(tmp_path))

    # No genuine match: the helper must fail rather than emit nothing.
    assert result.returncode == 1
    assert "macos_only" in result.stderr


def test_exits_nonzero_when_no_file_carries_the_marker(tmp_path):
    """The load-bearing guard: an empty result is an error, never a silent pass."""
    _write(tmp_path, "test_plain.py", "def test_z():\n    pass\n")

    result = _run("windows_only", str(tmp_path))

    assert result.returncode == 1
    assert result.stdout.strip() == ""
    assert "renamed or dropped" in result.stderr


def test_rejects_unknown_marker(tmp_path):
    result = _run("bsd_only", str(tmp_path))

    assert result.returncode == 2
    assert "unknown marker" in result.stderr


def test_rejects_missing_root():
    result = _run("macos_only", "/nonexistent/path/for/this/test")

    assert result.returncode == 2
    assert "no such directory" in result.stderr


def test_emits_repo_relative_posix_paths():
    """Output feeds a bash command line on the Windows runner, so separators
    must be POSIX and paths repo-relative.

    Asserted against the real ``tests/`` tree, which is the only case CI
    exercises — an out-of-repo root can't be made repo-relative and is
    emitted absolute instead.
    """
    result = _run("windows_only")

    assert result.returncode == 0, result.stderr
    listed = result.stdout.split()
    assert listed
    for line in listed:
        assert "\\" not in line
        assert not Path(line).is_absolute()


def test_real_tree_selects_files_for_every_marker():
    """Against the actual ``tests/`` tree each marker resolves to real files.

    This is the invariant the CI lanes depend on — not a snapshot of which
    files those are, only that each marker is in use and every listed path
    exists.
    """
    for marker in ("linux_only", "macos_only", "windows_only"):
        result = _run(marker)
        assert result.returncode == 0, f"{marker}: {result.stderr}"
        listed = result.stdout.split()
        assert listed, f"{marker} selected no files"
        for rel in listed:
            assert (REPO_ROOT / rel).is_file(), f"{marker} listed missing {rel}"
