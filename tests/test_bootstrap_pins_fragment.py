"""The bootstrap pin fragments must match pm/lock.json.

The installer (scripts/install.sh) is fetched standalone (curl | sh) and
bootstraps uv BEFORE any checkout exists, so it cannot read pm/lock.json at
run time. scripts/gen-bootstrap-pins.py derives an inline fragment from the
lockfile and splices it between markers in the script. These tests enforce the
derive-don't-store contract: the stored bytes can never drift from
pm/lock.json (the same authority the pm package manager uses), and the
installers actually consume the pinned values instead of an unpinned
"latest" channel.

setup-hermes.sh is deliberately NOT a fragment carrier: it runs after a
checkout exists, so it reads the uv pin straight from pm/lock.json at run
time (the former pm.sh bootstrap).
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen-bootstrap-pins.py"
SETUP_HERMES_SH = REPO_ROOT / "setup-hermes.sh"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
LOCK = REPO_ROOT / "pm" / "lock.json"

_SH_FILES = (INSTALL_SH,)
_POSIX_TARGETS = ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64")


def _packages() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8-sig"))["packages"]


def test_fragments_match_the_pin_table():
    """--check regenerates from pm/lock.json and fails on any drift."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"bootstrap fragments drifted from pm/lock.json:\n"
        f"{result.stdout}{result.stderr}"
    )


def test_installers_carry_the_pinned_uv_version():
    """Every fragment-carrying installer holds the exact uv version pm/lock.json names."""
    version = _packages()["uv"]["version"]
    for path in _SH_FILES:
        assert f'UV_PIN_VERSION="{version}"' in path.read_text(
            encoding="utf-8"
        ), path.name


def test_dev_setup_reads_the_pin_from_the_lockfile():
    """setup-hermes.sh runs after a checkout exists, so it must NOT carry a
    duplicated fragment: it reads the uv pin from pm/lock.json at run time."""
    source = SETUP_HERMES_SH.read_text(encoding="utf-8")
    assert "UV_PIN_VERSION" not in source
    assert 'pm/lock.json' in source
    assert "sha256" in source


def test_digests_are_verified_before_any_archive_is_unpacked():
    """An unverified download here is arbitrary code execution, so the
    sha256 check must sit between the download and the extract/exec step."""
    for path in _SH_FILES:
        source = path.read_text(encoding="utf-8")
        assert "digest mismatch" in source, path.name
        # The digest computation appears before the tar extraction.
        digest_at = source.index("sha256sum")
        extract_at = source.index("tar -xzf")
        assert digest_at < extract_at, f"{path.name}: verify before extract"


def test_installers_stage_into_the_pm_store_slot():
    """The installers must stage into the pm store slot
    (<store>/<tool>-<version>-<target>/), not the astral ~/.local/bin
    layout — so pm adopts the exact same bytes."""
    for path in _SH_FILES:
        source = path.read_text(encoding="utf-8")
        assert f"uv-$UV_PIN_VERSION-$_target" in source, path.name


def test_installers_do_not_fetch_unpinned_uv():
    """The astral latest-channel installers must never come back.

    astral.sh/uv/install.sh resolves "latest" at run time,
    which defeats pins-as-repo-data: a hermes install could silently get a
    uv nobody reviewed. The only astral.sh mentions allowed are the manual
    -install docs URL (docs.astral.sh).
    """
    for path in _SH_FILES:
        source = path.read_text(encoding="utf-8-sig")
        assert "astral.sh/uv/install.sh" not in source, path.name
        assert "astral.sh/uv/install.ps1" not in source, path.name
