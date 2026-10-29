#!/usr/bin/env python3
"""List the test files that carry a given platform marker.

Used by the marked-OS lane of ``.github/workflows/tests.yml`` to scope what
the macOS lane imports.

Why scope at all, when ``pytest -m macos_only`` already selects correctly?
Because ``-m`` filters AFTER collection, and collection IMPORTS every test
module under ``tests/``. On the Linux lane that is fine (it runs them all
anyway), but on the macOS/Windows lanes it would drag ~900 unrelated modules
through import on a host they were never expected to import on — one
unrelated ImportError would fail a job whose actual subject passed. Narrowing
the paths keeps each lane's failure signal about its own tests.

``-m`` is still passed by the workflow and remains the authoritative
selector: this script only decides which files get imported, never which
tests run. Over-selecting here is harmless (``-m`` drops the extras); the
failure mode to care about is UNDER-selecting, which is why the workflow
fails the job when zero tests end up selected.

Markers accepted:

  * the composable ``platforms`` form (e.g. ``@pytest.mark.platforms("macos")``
    or ``pytest.mark.platforms("macos", ...)``). The script matches any
    ``platforms(...)`` usage that names the platform in question — including
    negated specs (``"not macos"`` lists the file for the macOS lane: the
    lane's ``-m`` filter then decides what actually runs). Whole-word
    matching, so a ``"macos"`` spec doesn't match a hypothetical
    ``"macos_beta"`` spec.
  * the legacy trio (``linux_only`` / ``macos_only`` / ``windows_only``),
    matched as whole words in both decorator and ``pytestmark`` forms.

Usage:
    python scripts/ci/list_os_marked_tests.py macos [tests_root]

Prints one path per line (POSIX separators, repo-relative), sorted.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_VALID_MARKERS = ("linux", "macos", "windows", "linux_only", "macos_only", "windows_only")


def find_marked_files(marker: str, root: Path) -> list[Path]:
    """Return every ``test_*.py`` under *root* that references *marker*.

    Catches the composable ``platforms("...")`` spec form, the legacy
    decorator form, and the module-level ``pytestmark`` form.
    """
    if marker in ("linux", "macos", "windows"):
        # Composable form: platforms("<marker>" ...) — quoted spec, so match
        # the quoted string to avoid bare-word false positives (a variable
        # named `windows`, etc.). Also keep the legacy whole-word form.
        pattern = re.compile(
            rf'platforms\(\s*[^)]*?"[^")]*\b{re.escape(marker)}\b[^")]*"|'
            rf"\b{re.escape(marker)}_only\b"
        )
    else:
        pattern = re.compile(rf"\b{re.escape(marker)}\b")
    hits: list[Path] = []
    # os.walk, not Path.rglob: rglob raises FileNotFoundError when a directory
    # (a sibling job's __pycache__) vanishes mid-scan; os.walk skips it.
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not (fname.startswith("test_") and fname.endswith(".py")):
                continue
            path = Path(dirpath) / fname
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(path)
    return sorted(hits)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    marker = argv[1]
    if marker not in _VALID_MARKERS:
        print(
            f"unknown marker {marker!r}; valid: {', '.join(_VALID_MARKERS)}",
            file=sys.stderr,
        )
        return 2
    tests_root = Path(argv[2]) if len(argv) > 2 else Path("tests")
    if not tests_root.is_dir():
        print(f"no such directory: {tests_root}", file=sys.stderr)
        return 2
    hits = find_marked_files(marker, tests_root)
    for path in hits:
        print(path.as_posix())
    # A marker that selects nothing is almost certainly renamed or dropped;
    # the CI lane fails loudly on an empty list rather than reporting a
    # green job that ran nothing.
    if not hits:
        print(
            f"no test files carry marker {marker!r} under {tests_root} — "
            "either the marker was renamed or dropped, or selection is broken",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
