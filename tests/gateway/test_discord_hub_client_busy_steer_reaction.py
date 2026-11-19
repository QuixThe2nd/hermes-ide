"""Discord hub client reaction RPCs.

`DiscordHubClientAdapter._add_reaction` / `_remove_reaction` must carry
reactions over the hub RPC lane (stubbed at `_call`) with the exact payloads,
decline without a hub message id, and swallow hub failures.
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
