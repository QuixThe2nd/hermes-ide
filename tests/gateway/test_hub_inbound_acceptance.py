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

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.platforms.discord.hub import DiscordHubServer
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
