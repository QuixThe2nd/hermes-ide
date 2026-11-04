"""Trusted wakes cross the real adapter busy boundary, not external-user auth."""
import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class WakeAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.sent = []

    @property
    def name(self):
        return "telegram"

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


async def unused_handler(event):
    raise AssertionError("busy event must not start a concurrent turn")


def busy_gateway():
    adapter = WakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._draining = False
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
    key = adapter._event_session_key(MessageEvent(text="", message_type=MessageType.TEXT, source=source))
    adapter.set_message_handler(unused_handler)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    adapter._active_sessions[key] = asyncio.Event()
    return adapter, runner, source, key


@pytest.mark.asyncio
async def test_identityless_external_input_is_still_rejected():
    adapter, runner, source, key = busy_gateway()
    for _ in range(2):
        await adapter.handle_message(MessageEvent(text="external", message_type=MessageType.TEXT, source=source))
    assert key not in adapter._pending_messages
    assert not runner._overflow_queue(key)
    assert not adapter.sent
