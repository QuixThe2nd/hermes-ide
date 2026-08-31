"""Behavior contracts for the /sethomeserver slash command.

Guards pinned here: Discord-only rejection (including a relay-fronted source,
matching the /sethome relay posture), the confirm-to-move flow, and that
authorization rides the same slash gates sethome uses.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.config import Platform


def _ensure_discord_mock():
    """Borrowed from tests/gateway/test_discord_slash_auth.py so this file runs
    without discord.py installed."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault(
        "discord.ext", MagicMock(commands=MagicMock(Bot=MagicMock))
    )


_ensure_discord_mock()


def _make_runner(tmp_path):
    """Bare GatewayRunner, the tests/gateway/test_voice_command.py pattern."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._session_db = None
    runner.session_store = MagicMock()
    runner._is_user_authorized = lambda source: True
    return runner


def _event(platform, *, scope_id=None, args="", chat_id="c1", via_relay=False):
    source = SessionSource(
        chat_id=chat_id,
        user_id="user1",
        platform=platform,
        scope_id=scope_id,
    )
    if via_relay:
        source.delivered_via_upstream_relay = True
    event = MessageEvent(text="/sethomeserver", message_type=MessageType.TEXT, source=source)
    event.get_command_args = lambda: args
    return event


@pytest.fixture
def runner(tmp_path):
    return _make_runner(tmp_path)


@pytest.fixture
def stub_reconcile(monkeypatch):
    """Replace the plugin's reconcile with a recorder (no network)."""
    import plugins.home_server.core as core

    calls = []

    def fake_reconcile(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "enabled": True,
            "guild_id": "g1",
            "created": ["category:Lounges", "channel:inbox"],
            "embeds_posted": ["Lounges/inbox"],
            "wired": {"hermes_starts": "wired", "quota_channels": True},
            "home_channel": "set",
            "notification_channel": "set",
            "modules": {
                "chat": True,
                "notifications": True,
                "memory": True,
                "quotas": True,
                "speeds": True,
            },
        }

    monkeypatch.setattr(core, "reconcile", fake_reconcile)
    return calls


@pytest.mark.asyncio
async def test_non_discord_platform_is_rejected(runner, stub_reconcile):
    result = await runner._handle_set_home_server_command(
        _event(Platform.TELEGRAM, scope_id="g1")
    )
    assert "only works on Discord" in result
    assert stub_reconcile == []


@pytest.mark.asyncio
async def test_relay_fronted_source_is_rejected(runner, stub_reconcile):
    """Discord-only implies relay-fronted deliveries are refused too — a relay
    cannot authenticate the guild the way a native Discord message can."""
    result = await runner._handle_set_home_server_command(
        _event(Platform.RELAY, scope_id="g1")
    )
    assert "only works on Discord" in result
    assert stub_reconcile == []


@pytest.mark.asyncio
async def test_relay_delivered_discord_source_is_rejected(runner, stub_reconcile):
    """Relay-guard parity with /sethome: a Discord-platform source that
    arrived via an upstream relay passes the platform check, so it must be
    refused unless the relay actually fronts Discord AND authenticated the
    sender. Here no relay adapter fronts Discord, so nothing may run."""
    runner._adapter_for_source = lambda source: None
    result = await runner._handle_set_home_server_command(
        _event(Platform.DISCORD, scope_id="900000000000000001", via_relay=True)
    )
    assert "relay" in result.lower()
    assert stub_reconcile == []

    # And an authenticating relay still needs a sender identity.
    fronting = MagicMock()
    fronting.fronts_platform = lambda platform: platform is Platform.DISCORD
    runner._adapter_for_source = lambda source: fronting
    no_user = _event(Platform.DISCORD, scope_id="900000000000000001", via_relay=True)
    no_user.source.user_id = None
    result = await runner._handle_set_home_server_command(no_user)
    assert "relay" in result.lower()
    assert stub_reconcile == []


@pytest.mark.asyncio
async def test_authenticating_relay_fronting_discord_is_allowed(
    runner, hermes, stub_reconcile
):
    """The other side of parity: a relay that fronts Discord and authenticated
    the sender may drive provisioning, exactly as it may set a home channel."""
    fronting = MagicMock()
    fronting.fronts_platform = lambda platform: platform is Platform.DISCORD
    runner._adapter_for_source = lambda source: fronting
    result = await runner._handle_set_home_server_command(
        _event(Platform.DISCORD, scope_id="900000000000000001", via_relay=True)
    )
    assert len(stub_reconcile) == 1
    assert "900000000000000001" in result


@pytest.mark.asyncio
async def test_discord_runs_provisioning_and_reports(runner, hermes, stub_reconcile):
    result = await runner._handle_set_home_server_command(
        _event(Platform.DISCORD, scope_id="900000000000000001")
    )
    assert len(stub_reconcile) == 1
    assert "900000000000000001" in result
    assert "Lounges" in result and "inbox" in result
    assert "hermes_starts" in result

    import yaml

    raw = yaml.safe_load((hermes / "config.yaml").read_text(encoding="utf-8"))
    assert raw["discord_home_server"]["guild_id"] == "900000000000000001"


@pytest.mark.asyncio
async def test_moving_guild_requires_confirm(runner, hermes, write_config, stub_reconcile):
    write_config({"guild_id": "111"})
    result = await runner._handle_set_home_server_command(
        _event(Platform.DISCORD, scope_id="222")
    )
    assert "confirm" in result.lower()
    assert stub_reconcile == []
    # The stored guild is untouched until confirmed.
    import yaml

    raw = yaml.safe_load((hermes / "config.yaml").read_text(encoding="utf-8"))
    assert raw["discord_home_server"]["guild_id"] == "111"


@pytest.mark.asyncio
async def test_confirm_moves_the_guild(runner, hermes, write_config, stub_reconcile):
    write_config({"guild_id": "111"})
    result = await runner._handle_set_home_server_command(
        _event(Platform.DISCORD, scope_id="222", args="confirm")
    )
    assert len(stub_reconcile) == 1
    import yaml

    raw = yaml.safe_load((hermes / "config.yaml").read_text(encoding="utf-8"))
    assert raw["discord_home_server"]["guild_id"] == "222"


@pytest.mark.asyncio
async def test_same_guild_needs_no_confirm(runner, hermes, write_config, stub_reconcile):
    write_config({"guild_id": "222"})
    await runner._handle_set_home_server_command(
        _event(Platform.DISCORD, scope_id="222")
    )
    assert len(stub_reconcile) == 1


@pytest.mark.asyncio
async def test_no_resolvable_guild_is_refused(runner, stub_reconcile):
    result = await runner._handle_set_home_server_command(_event(Platform.DISCORD))
    assert "server" in result.lower()
    assert stub_reconcile == []


@pytest.mark.asyncio
async def test_provisioning_failure_is_surfaced(runner, hermes, monkeypatch):
    import plugins.home_server.core as core

    def boom(**kwargs):
        raise core.HomeServerError("discord returned 403 for POST /guilds/x/channels")

    monkeypatch.setattr(core, "reconcile", boom)
    result = await runner._handle_set_home_server_command(
        _event(Platform.DISCORD, scope_id="900000000000000001")
    )
    assert "failed" in result.lower()
    assert "403" in result
    # The failing message must not leak the bot token.
    assert "t0ken" not in result


@pytest.mark.asyncio
async def test_sethome_pointer_is_discord_only(runner, hermes):
    """The /sethomeserver hint rides set_home.success on Discord only, so
    non-Discord platform copy is byte-identical to before."""
    # sethome also updates the in-memory gateway config.
    from gateway.config import GatewayConfig

    runner.config = GatewayConfig()
    discord_result = await runner._handle_set_home_command(
        _event(Platform.DISCORD, scope_id="g1", chat_id="c9")
    )
    telegram_result = await runner._handle_set_home_command(
        _event(Platform.TELEGRAM, chat_id="c9")
    )

    assert "/sethomeserver" in discord_result
    assert "/sethomeserver" not in telegram_result
    # The shared success line still reaches both platforms.
    for result in (discord_result, telegram_result):
        assert "Home channel set" in result
