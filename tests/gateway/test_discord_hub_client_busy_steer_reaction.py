"""Discord hub deployments get the busy-steer ⏩ receipt, not a silent skip.

`gateway/run_busy.py::_maybe_react_discord_busy_steer` requires a callable
`adapter._add_reaction`. The hub-side adapter (`DiscordHubClientAdapter`) lacked
one, so every busy-steer on a hub deployment got no reaction. These tests drive
the real receipt helper through a real `DiscordHubClientAdapter` (hub RPC lane
stubbed at `_call`) and assert the RPC payloads.
"""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter


def _hub_adapter() -> DiscordHubClientAdapter:
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    adapter._reactions_enabled = lambda: True
    return adapter


def _steer_event():
    source = SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1", chat_type="dm")
    event = MessageEvent(text="adjust course", source=source, message_id="m-42")
    event.raw_message = {"discord_hub_message_id": "m-42", "discord_hub_token": "tk"}
    return event, source


@pytest.mark.asyncio
async def test_busy_steer_through_hub_client_emits_fast_forward_reaction_rpc(monkeypatch, tmp_path):
    """BEFORE fix: fails — hub adapter had no _add_reaction, reaction silently skipped."""
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)

    adapter = _hub_adapter()
    rpcs = []

    async def fake_call(method, params=None):
        rpcs.append((method, params))
        return True

    adapter._call = fake_call

    runner = GatewayRunner(config=GatewayConfig())
    runner._delivery_adapter_for = lambda source: adapter
    runner._adapter_for_source = lambda source: adapter

    event, source = _steer_event()
    reacted = await runner._maybe_react_discord_busy_steer(
        event, adapter, "key", steered=True, is_steer_mode=True,
    )

    assert reacted is True
    assert rpcs == [(
        "rpc",
        {"token_key": adapter._token_key, "method": "add_reaction",
         "message_id": "m-42", "emoji": "⏩"},
    )]


@pytest.mark.asyncio
async def test_add_reaction_without_message_id_returns_false_without_rpc():
    adapter = _hub_adapter()

    async def fail_call(method, params=None):
        raise AssertionError("no RPC expected")

    adapter._call = fail_call

    assert await adapter._add_reaction({"discord_hub_token": "tk"}, "⏩") is False
    assert await adapter._add_reaction(None, "⏩") is False


@pytest.mark.asyncio
async def test_add_reaction_swallows_rpc_failure():
    adapter = _hub_adapter()

    async def broken_call(method, params=None):
        raise ConnectionError("hub detached")

    adapter._call = broken_call

    assert await adapter._add_reaction({"discord_hub_message_id": "m-1"}, "⏩") is False


@pytest.mark.asyncio
async def test_remove_reaction_emits_remove_rpc():
    adapter = _hub_adapter()
    rpcs = []

    async def fake_call(method, params=None):
        rpcs.append((method, params))
        return True

    adapter._call = fake_call

    assert await adapter._remove_reaction({"discord_hub_message_id": "m-7"}, "👀") is True
    assert rpcs == [(
        "rpc",
        {"token_key": adapter._token_key, "method": "remove_reaction",
         "message_id": "m-7", "emoji": "👀"},
    )]
