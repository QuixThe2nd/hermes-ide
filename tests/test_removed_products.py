"""Removed products stay removed: Honcho/OpenViking/mem0 memory providers, the
OpenClaw migration (`hermes claw`), and the Docusaurus website are not part of
this distribution. If one reappears, it drags dead registration/setup paths with
it — fail here instead."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_removed_memory_provider_dirs_absent():
    for name in ("honcho", "openviking", "mem0"):
        assert not (REPO_ROOT / "plugins" / "memory" / name).exists(), (
            f"plugins/memory/{name} was removed from this distribution"
        )


def test_removed_memory_provider_extras_absent():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    optional = data.get("project", {}).get("optional-dependencies", {})
    for name in ("honcho", "mem0", "openviking"):
        assert name not in optional, f"pyproject extra '{name}' was removed"
        assert f"hermes-agent[{name}]" not in optional.get("all", [])


def test_openclaw_migrate_command_absent():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    choices = set(_subparsers.choices)
    assert "claw" not in choices, "`hermes claw` (OpenClaw migration) was removed"


def test_openclaw_migration_skill_absent():
    assert not (REPO_ROOT / "optional-skills" / "migration" / "openclaw-migration").exists()


def test_website_dir_absent():
    assert not (REPO_ROOT / "website").exists(), "the Docusaurus site was removed"
