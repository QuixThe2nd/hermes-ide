"""Removed products stay removed: Honcho/OpenViking/mem0 memory providers, the
OpenClaw migration (`hermes claw`), and the Docusaurus website are not part of
this distribution. If one reappears, it drags dead registration/setup paths with
it — fail here instead."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_removed_memory_provider_dirs_absent():
    for name in (
        "honcho",
        "openviking",
        "mem0",
        "byterover",
        "holographic",
        "supermemory",
        "retaindb",
    ):
        assert not (REPO_ROOT / "plugins" / "memory" / name).exists(), (
            f"plugins/memory/{name} was removed from this distribution"
        )


def test_removed_memory_provider_extras_absent():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    optional = data.get("project", {}).get("optional-dependencies", {})
    for name in ("honcho", "mem0", "openviking", "supermemory"):
        assert name not in optional, f"pyproject extra '{name}' was removed"
        assert f"hermes-agent[{name}]" not in optional.get("all", [])


def test_retired_memory_providers_fail_closed(tmp_path, monkeypatch):
    """Retired names do not load and are not auto-installed, even if a user
    directory or catalog source claims them. An unknown catalog miss still
    does not install."""
    from hermes_cli import memory_provider_migration as mig
    from plugins.memory import (
        find_provider_dir,
        list_memory_provider_names,
        load_memory_provider,
    )

    plugins = tmp_path / "plugins"
    for name in ("byterover", "holographic", "supermemory", "retaindb"):
        plugin = plugins / name
        plugin.mkdir(parents=True)
        (plugin / "__init__.py").write_text(
            "from agent.memory_provider import MemoryProvider\n"
            "class P(MemoryProvider):\n"
            "    @property\n"
            "    def name(self):\n"
            f"        return {name!r}\n"
            "    def is_available(self):\n"
            "        return True\n"
            "    def initialize(self, *a, **k):\n"
            "        pass\n"
            "    def get_tool_schemas(self):\n"
            "        return []\n"
            "def register(ctx):\n"
            "    ctx.register_memory_provider(P())\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("plugins.memory._get_user_plugins_dir", lambda: plugins)
    names = list_memory_provider_names()
    for name in ("byterover", "holographic", "supermemory", "retaindb"):
        assert find_provider_dir(name) is None
        assert load_memory_provider(name) is None
        assert name not in names

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: byterover\n", encoding="utf-8")
    monkeypatch.setattr(mig, "catalog_source", lambda name: name)
    monkeypatch.setattr(mig, "provider_present", lambda name, home: False)
    assert mig.migrate_home(
        home, install=lambda n: pytest.fail("must not install"), say=lambda s: None
    ) is None

    (home / "config.yaml").write_text("memory:\n  provider: not-a-real-provider\n", encoding="utf-8")
    monkeypatch.setattr(mig, "catalog_source", lambda name: None)
    assert mig.migrate_home(
        home, install=lambda n: pytest.fail("must not install"), say=lambda s: None
    ) is None
    assert load_memory_provider("not-a-real-provider") is None


def test_openclaw_migrate_command_absent():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    choices = set(_subparsers.choices)
    assert "claw" not in choices, "`hermes claw` (OpenClaw migration) was removed"


def test_openclaw_migration_skill_absent():
    assert not (REPO_ROOT / "optional-skills" / "migration" / "openclaw-migration").exists()


def test_website_dir_absent():
    assert not (REPO_ROOT / "website").exists(), "the Docusaurus site was removed"
