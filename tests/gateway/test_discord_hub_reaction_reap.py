"""Hub-side cleanup of processing 👀 reactions when a gateway generation detaches."""

import asyncio
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.hub import DiscordHubServer, _MAX_TRACKED_PROCESSING_REACTIONS, _token_key
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter


def run(coro):
    return asyncio.run(coro)


def _short_hub_socket(_tmp_path: Path) -> Path:
    """AF_UNIX bind paths are length-capped; pytest tmp dirs can exceed the limit."""
    return Path("/tmp") / f"hermes-dh-test-{os.getpid()}-{uuid.uuid4().hex[:10]}.sock"


class FakeRawMessage:
    def __init__(self):
        self.add_reaction = AsyncMock(return_value=None)
        self.remove_reaction = AsyncMock(return_value=None)


class FakeHubUser:
    pass


@pytest.mark.platforms("posix")
def test_detach_reaps_processing_reaction_via_hub_rpc(tmp_path):
    async def scenario():
        server = DiscordHubServer(_short_hub_socket(tmp_path))
        hub_user = FakeHubUser()

        class FakeDiscordConnection:
            def __init__(self):
                self._client = MagicMock(user=hub_user)

            async def disconnect(self):
                pass

        connection = FakeDiscordConnection()
        token_key = _token_key("fake-token")
        raw = FakeRawMessage()
        server._raw_messages[f"{token_key}:msg-1"] = raw

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[token_key] = connection

        server._attach_adapter = fake_attach
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            assert await client._call(
                "rpc",
                {
                    "token_key": client._token_key,
                    "method": "add_reaction",
                    "message_id": "msg-1",
                    "emoji": "👀",
                },
            )
            attached = server._clients[client._generation_id]
            assert f"{token_key}:msg-1" in attached.processing_reactions

            await client.disconnect()
            for _ in range(100):
                if client._generation_id not in server._clients:
                    break
                await asyncio.sleep(0.01)

            raw.remove_reaction.assert_awaited_once_with("👀", hub_user)
            assert client._generation_id not in server._clients
        finally:
            await server.stop()

    run(scenario())


@pytest.mark.platforms("posix")
def test_processing_reaction_tracking_is_bounded_and_dropped_on_detach(tmp_path):
    async def scenario():
        server = DiscordHubServer(_short_hub_socket(tmp_path))

        class FakeDiscordConnection:
            def __init__(self):
                self._client = MagicMock(user=FakeHubUser())

            async def disconnect(self):
                pass

        connection = FakeDiscordConnection()
        token_key = _token_key("fake-token")

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[token_key] = connection

        server._attach_adapter = fake_attach
        await server.start()
        clients = []
        try:
            for index in range(3):
                client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
                client._socket_path = server.socket_path
                assert await client.connect()
                clients.append(client)
                attached = server._clients[client._generation_id]
                for message_index in range(_MAX_TRACKED_PROCESSING_REACTIONS + 5):
                    message_id = f"g{index}-m{message_index}"
                    server._raw_messages[f"{token_key}:{message_id}"] = FakeRawMessage()
                    await client._call(
                        "rpc",
                        {
                            "token_key": client._token_key,
                            "method": "add_reaction",
                            "message_id": message_id,
                            "emoji": "👀",
                        },
                    )
                assert len(attached.processing_reactions) == _MAX_TRACKED_PROCESSING_REACTIONS
                await client.disconnect()
                for _ in range(100):
                    if client._generation_id not in server._clients:
                        break
                    await asyncio.sleep(0.01)
                assert client._generation_id not in server._clients

            assert not server._clients
            total_tracked = sum(len(c.processing_reactions) for c in server._clients.values())
            assert total_tracked == 0
        finally:
            await asyncio.gather(*(c.disconnect() for c in clients), return_exceptions=True)
            await server.stop()

    run(scenario())


@pytest.mark.platforms("posix")
def test_detach_reap_only_removes_processing_emoji_not_completion(tmp_path):
    async def scenario():
        server = DiscordHubServer(_short_hub_socket(tmp_path))
        hub_user = FakeHubUser()

        class FakeDiscordConnection:
            def __init__(self):
                self._client = MagicMock(user=hub_user)

            async def disconnect(self):
                pass

        connection = FakeDiscordConnection()
        token_key = _token_key("fake-token")
        raw = FakeRawMessage()
        server._raw_messages[f"{token_key}:msg-1"] = raw

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[token_key] = connection

        server._attach_adapter = fake_attach
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            await client._call(
                "rpc",
                {
                    "token_key": client._token_key,
                    "method": "add_reaction",
                    "message_id": "msg-1",
                    "emoji": "👀",
                },
            )
            await client.disconnect()
            for _ in range(100):
                if client._generation_id not in server._clients:
                    break
                await asyncio.sleep(0.01)

            completion_calls = [
                call
                for call in raw.add_reaction.await_args_list
                if call.args and call.args[0] in {"✅", "❌"}
            ]
            assert not completion_calls
            assert raw.remove_reaction.await_count == 1
            assert raw.remove_reaction.await_args.args[0] == "👀"
        finally:
            await server.stop()

    run(scenario())


@pytest.mark.platforms("posix")
def test_successful_remove_reaction_clears_tracking_without_detach_reap(tmp_path):
    async def scenario():
        server = DiscordHubServer(_short_hub_socket(tmp_path))
        hub_user = FakeHubUser()

        class FakeDiscordConnection:
            def __init__(self):
                self._client = MagicMock(user=hub_user)

            async def disconnect(self):
                pass

        connection = FakeDiscordConnection()
        token_key = _token_key("fake-token")
        raw = FakeRawMessage()
        server._raw_messages[f"{token_key}:msg-1"] = raw

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[token_key] = connection

        server._attach_adapter = fake_attach
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            attached = server._clients[client._generation_id]
            await client._call(
                "rpc",
                {
                    "token_key": client._token_key,
                    "method": "add_reaction",
                    "message_id": "msg-1",
                    "emoji": "👀",
                },
            )
            assert attached.processing_reactions
            await client._call(
                "rpc",
                {
                    "token_key": client._token_key,
                    "method": "remove_reaction",
                    "message_id": "msg-1",
                    "emoji": "👀",
                },
            )
            assert not attached.processing_reactions
            raw.remove_reaction.reset_mock()
            await client.disconnect()
            raw.remove_reaction.assert_not_awaited()
        finally:
            await client.disconnect()
            await server.stop()

    run(scenario())
