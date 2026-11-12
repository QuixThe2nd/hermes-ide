"""A rate-limited platform API call must not stall the hub connection."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.hub import DiscordHubServer, _token_key
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter


@pytest.mark.platforms("posix")
def test_slow_adapter_rpc_does_not_block_router_calls():
    async def scenario(socket_dir):
        server = DiscordHubServer(Path(socket_dir) / "hub.sock")

        class FakeDiscordConnection:
            async def disconnect(self):
                pass

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[_token_key(config_data["token"])] = FakeDiscordConnection()

        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_adapter_rpc(client, params):
            # Stands in for discord.py sleeping out a long 429 on a channel PATCH.
            started.set()
            await release.wait()
            return True

        server._attach_adapter = fake_attach
        server._adapter_rpc = slow_adapter_rpc
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            rename = asyncio.create_task(client.rename_thread("123", "agents-0"))
            await asyncio.wait_for(started.wait(), 2)
            # While the rename is stuck, ownership and ping must still answer.
            status = await asyncio.wait_for(client._call("ping"), 2)
            assert status["current_generation"]
            owned = await asyncio.wait_for(
                client._call("register", {"session_key": "agent:main:discord:thread:1"}), 2,
            )
            assert owned["owner"]
            assert not rename.done()
            release.set()
            assert await asyncio.wait_for(rename, 2) is True
        finally:
            release.set()
            await client.disconnect()
            await server.stop()

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="h-") as socket_dir:
        asyncio.run(scenario(socket_dir))
