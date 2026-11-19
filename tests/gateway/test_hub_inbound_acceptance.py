"""Hub replacement ``handle_message`` handlers must stamp the acceptance receipt.

The base contract (``gateway/platforms/base.py`` ``handle_message``) resets
``event._gateway_accepted`` and sets it True when the event is handed to a
live gateway generation (scheduled/queued — not model execution).
``MatrixHub`` and ``DiscordHubServer`` replace ``adapter.handle_message``
with their own inbound ``_deliver``; the replacement must honour the same
receipt so downstream consumers (the Matrix inbox router's dispatch gate)
can tell "a live generation took it" from "nobody was attached".
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.platforms.discord.hub import DiscordHubServer
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter
from plugins.platforms.matrix.hub import MatrixHub


def _event(platform: Platform) -> MessageEvent:
    return MessageEvent(
        text="hello", message_type=MessageType.TEXT, message_id="$evt1",
        source=SessionSource(
            platform=platform, chat_id="!room:example.org", chat_type="dm",
            user_id="@sender:example.org"))


@pytest.mark.asyncio
class TestMatrixHubInboundAcceptance:
    async def test_route_to_live_generation_marks_event_accepted(self, tmp_path):
        hub = MatrixHub(None)
        hub.router = SimpleNamespace(route=AsyncMock(return_value="gen-1"))
        attachment = SimpleNamespace(
            key="matrix-key", home=tmp_path, secrets={},
            adapter=SimpleNamespace(_event_session_key=lambda event: "matrix:s1"))
        deliver = hub._make_inbound_handler(attachment)

        event = _event(Platform.MATRIX)
        await deliver(event)

        assert event._gateway_accepted is True

    async def test_undelivered_record_resets_and_leaves_event_unaccepted(self, tmp_path):
        hub = MatrixHub(None)
        hub.router = SimpleNamespace(route=AsyncMock(return_value=None))
        attachment = SimpleNamespace(
            key="matrix-key", home=tmp_path, secrets={},
            adapter=SimpleNamespace(_event_session_key=lambda event: "matrix:s1"))
        deliver = hub._make_inbound_handler(attachment)

        event = _event(Platform.MATRIX)
        event._gateway_accepted = True  # a stale receipt must be reset like the base does
        await deliver(event)

        assert event._gateway_accepted is False


@pytest.mark.asyncio
class TestDiscordHubInboundAcceptance:
    async def test_route_to_live_generation_marks_event_accepted(self, tmp_path):
        server = DiscordHubServer(tmp_path / "unused.sock")
        server.router = SimpleNamespace(route=AsyncMock(return_value="gen-1"))
        adapter = SimpleNamespace(_event_session_key=lambda event: "discord:s1")
        deliver = server._make_inbound_handler(adapter, "token-key")

        event = _event(Platform.DISCORD)
        await deliver(event)

        assert event._gateway_accepted is True

    async def test_undelivered_record_resets_and_leaves_event_unaccepted(self, tmp_path):
        server = DiscordHubServer(tmp_path / "unused.sock")
        server.router = SimpleNamespace(route=AsyncMock(return_value=None))
        adapter = SimpleNamespace(_event_session_key=lambda event: "discord:s1")
        deliver = server._make_inbound_handler(adapter, "token-key")

        event = _event(Platform.DISCORD)
        event._gateway_accepted = True
        await deliver(event)

        assert event._gateway_accepted is False

    async def test_local_internal_event_is_not_accepted_without_a_hub_generation(self):
        adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        adapter._writer = SimpleNamespace()
        adapter._call = AsyncMock(return_value=None)
        handler = AsyncMock(return_value=None)
        adapter.set_message_handler(handler)
        event = _event(Platform.DISCORD)
        event.internal = True

        await adapter.handle_message(event)

        assert event._gateway_accepted is False
        adapter._call.assert_awaited_once()
        assert adapter._call.await_args.args[0] == "register"
        handler.assert_not_awaited()

    async def test_remote_owned_replay_is_declined_without_losing_its_completion(self):
        adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        adapter._writer = SimpleNamespace()
        adapter._call = AsyncMock(return_value={"owner": "other", "version": None})
        handler = AsyncMock(return_value=None)
        adapter.set_message_handler(handler)
        event = _event(Platform.DISCORD)
        event.internal = True
        event._gateway_replay_completion = lambda _outcome: None

        await adapter.handle_message(event)

        assert event._gateway_accepted is False
        adapter._call.assert_awaited_once_with("register", {
            "session_key": adapter._event_session_key(event),
        })
        handler.assert_not_awaited()
