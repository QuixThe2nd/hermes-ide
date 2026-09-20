"""Sync contracts for the default SOUL seeded from four places.

The same persona text is seeded from hermes_cli.default_soul,
scripts/install.sh, scripts/install.ps1, and docker/SOUL.md. Older installers
seeded a stale scaffold instead, so any drift here resurfaces as first-run
churn or a missing credential check. These tests pin the full normalized text
of every seed site, so none of the four can drift apart silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from hermes_cli.default_soul import DEFAULT_SOUL_MD

REPO_ROOT = Path(__file__).resolve().parents[2]

PREFLIGHT_PHRASE = "Check for existing credentials before asking the user"


def _normalize(text: str) -> str:
    """Unify CRLF and collapse whitespace runs so wrapping never matters."""
    return " ".join(text.replace("\r\n", "\n").split())


def _extract_heredoc_soul(install_sh: str) -> str:
    match = re.search(r"<<\s*'SOUL_EOF'\n(.*?)\nSOUL_EOF", install_sh, re.DOTALL)
    assert match, "SOUL_EOF heredoc not found in scripts/install.sh"
    return match.group(1)


def _extract_here_string_soul(install_ps1: str) -> str:
    match = re.search(r'\$soulContent = @"\r?\n(.*?)\r?\n"@', install_ps1, re.DOTALL)
    assert match, "$soulContent here-string not found in scripts/install.ps1"
    return match.group(1)


def test_default_soul_contains_credential_preflight():
    assert PREFLIGHT_PHRASE in DEFAULT_SOUL_MD


def test_default_soul_is_stock_safe():
    # The stock-safety sweep greps the four production seed files, not this
    # test source, so the markers can appear here as plain literals. The
    # identity tests below extend this coverage to the installer and docker
    # copies, which must match DEFAULT_SOUL_MD exactly.
    for marker in ("shabi", "~/.hermes/bin/", "broker injection"):
        assert marker not in DEFAULT_SOUL_MD


def test_install_sh_soul_seed_matches_default():
    text = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert _normalize(_extract_heredoc_soul(text)) == _normalize(DEFAULT_SOUL_MD)


def test_install_ps1_soul_seed_matches_default():
    text = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    # install.ps1 must stay pure ASCII, so it seeds "--" where the canonical
    # DEFAULT_SOUL_MD uses an em dash. Runtime upgrades the ASCII seed in place.
    seeded = _extract_here_string_soul(text).replace("--", "\u2014")
    assert _normalize(seeded) == _normalize(DEFAULT_SOUL_MD)


def test_docker_soul_md_matches_default():
    text = (REPO_ROOT / "docker" / "SOUL.md").read_text(encoding="utf-8")
    assert _normalize(text) == _normalize(DEFAULT_SOUL_MD)
