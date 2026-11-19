"""``resolve_ticket`` is removed from the fork; the sibling Discord tools
must survive the removal and no toolset may still advertise the removed
tool. The adapter must not retain the raw-reaction dispatch whose only
consumer was the deleted module.
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.registry import discover_builtin_tools, registry
from toolsets import TOOLSETS

import tools

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


class TestResolveTicketRemoved:
    def test_not_registered_after_discovery(self):
        discover_builtin_tools()
        assert registry.get_entry("resolve_ticket") is None

    def test_absent_from_discord_toolsets(self):
        assert "resolve_ticket" not in TOOLSETS["discord"]["tools"]
        assert "resolve_ticket" not in TOOLSETS["hermes-discord"]["tools"]
        # The description must not advertise a capability no toolset offers.
        assert "resolve" not in TOOLSETS["discord"]["description"].lower()

    def test_module_not_importable(self):
        # Discovery may have cached the module before removal; a cached
        # import is not "removed".
        sys.modules.pop("tools.discord_resolve_tool", None)
        try:
            module = importlib.import_module("tools.discord_resolve_tool")
        except ImportError:
            return
        # The import succeeded: acceptable ONLY when a deployment venv's
        # editable finder served a stale copy from a foreign tree — this
        # repo must no longer provide the module.
        assert not Path(module.__file__).resolve().is_relative_to(_REPO_ROOT / "tools")

    def test_adapter_has_no_raw_reaction_dispatch(self):
        _ensure_discord_mock()
        from plugins.platforms.discord.adapter import DiscordAdapter

        assert not hasattr(DiscordAdapter, "_dispatch_raw_reaction")

    def test_discord_siblings_remain(self):
        discover_builtin_tools()
        for tool in ("discord", "discord_admin"):
            assert registry.get_entry(tool) is not None
        assert "discord" in TOOLSETS["discord"]["tools"]
        assert "discord_admin" in TOOLSETS["discord_admin"]["tools"]
        for tool in ("discord", "discord_admin"):
            assert tool in TOOLSETS["hermes-discord"]["tools"]
