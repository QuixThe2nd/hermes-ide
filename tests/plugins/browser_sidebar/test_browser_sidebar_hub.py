"""Browser sidebar transport stays hub-owned across gateway generations."""

import asyncio
import json
import socket

import pytest

pytest.importorskip("websockets")

from gateway.config import PlatformConfig
from plugins.platforms.browser_sidebar import adapter as bs
from plugins.platforms.discord.hub import DiscordHubServer

pytestmark = [pytest.mark.asyncio, pytest.mark.platforms("posix")]

TOKEN = "hub-contract-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Gateway:
    def __init__(self, harness, name: str):
        self.adapter = bs.BrowserSidebarAdapter(PlatformConfig(
            enabled=True,
            extra={"token": TOKEN, "host": "127.0.0.1", "port": harness.port},
        ))
        self.adapter._socket_path = harness.server.socket_path
        self.adapter._owner_profile = "Big Steve"
        self.name = name
        self.events = []
        self.message_arrived = asyncio.Event()
        self.adapter.set_message_handler(self._on_message)

    async def _on_message(self, event):
        self.events.append(event)
        self.message_arrived.set()

    async def connect(self):
        assert await self.adapter.connect(), f"{self.name} must attach to the hub"

    async def disconnect(self):
        await self.adapter.disconnect()


class _HubSidebar:
    def __init__(self, socket_path):
        self.port = _free_port()
        self.server = DiscordHubServer(socket_path)
        self.gateways = []

    async def __aenter__(self):
        await self.server.start()
        return self

    async def __aexit__(self, *exc):
        for gateway in reversed(self.gateways):
            await gateway.disconnect()
        await self.server.stop()
        bs._ADAPTERS.clear()

    def add_gateway(self, name: str) -> _Gateway:
        gateway = _Gateway(self, name)
        self.gateways.append(gateway)
        return gateway

    async def connect_extension(self, token: str = TOKEN):
        from websockets.asyncio.client import connect
        from websockets.typing import Subprotocol

        return await connect(
            f"ws://127.0.0.1:{self.port}/ws", subprotocols=[Subprotocol(token)],
        )


async def _recv(ws) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), 2))


async def _chat(ws, text="hello", message_id="m1") -> None:
    await ws.send(json.dumps({
        "type": "chat", "id": message_id, "text": text,
        "tab": {"id": 1, "url": "https://example.com", "title": "Example"},
    }))


async def test_attach_replacement_routes_all_traffic_only_to_current_gateway(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        current = hub.add_gateway("current")
        await current.connect()

        stale_send = await old.adapter.send("brave", "stale")
        assert not stale_send.success
        await old.disconnect()
        assert not await old.adapter.connect(), "a superseded generation must not reclaim the hub"
        await _chat(ws)
        await asyncio.wait_for(current.message_arrived.wait(), 2)
        assert old.events == []
        assert current.events[0].text.endswith("hello")

        assert (await current.adapter.send_draft("brave", 4, "working")).success
        assert await _recv(ws) == {"type": "delta", "id": "4", "text": "working"}
        assert (await current.adapter.send("brave", "done")).success
        assert await _recv(ws) == {"type": "final", "id": "m1", "text": "done"}
        await ws.close()


async def test_gateway_disconnect_and_successor_do_not_drop_extension_socket(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        await ws.send(json.dumps({"type": "hello"}))
        assert await _recv(ws) == {"type": "ready"}

        await old.disconnect()
        await ws.send(json.dumps({"type": "hello"}))
        assert await _recv(ws) == {"type": "ready"}

        current = hub.add_gateway("current")
        await current.connect()
        await _chat(ws, "after restart")
        await asyncio.wait_for(current.message_arrived.wait(), 2)
        assert current.events[0].text.endswith("after restart")
        await ws.close()


async def test_tool_result_from_hub_socket_reaches_current_gateway(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        current = hub.add_gateway("current")
        await current.connect()

        call = asyncio.create_task(bs._run_tab_js_entry({"code": "document.title"}))
        request = await _recv(ws)
        assert request["type"] == "tool"
        assert request["name"] == "run_tab_js"
        await ws.send(json.dumps({
            "type": "tool_result", "request_id": request["request_id"],
            "ok": True, "result": "Example",
        }))
        result = json.loads(await asyncio.wait_for(call, 2))
        assert result == {"success": True, "result": "Example"}
        await ws.close()


async def test_unauthorized_websocket_is_rejected_by_hub(short_af_unix_path):
    from websockets.exceptions import InvalidHandshake

    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("gateway")
        await gateway.connect()
        with pytest.raises(InvalidHandshake):
            await hub.connect_extension(token="wrong-token")


async def test_extension_reconnect_remains_on_current_gateway(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("gateway")
        await gateway.connect()
        first = await hub.connect_extension()
        await first.close()

        second = await hub.connect_extension()
        await second.send(json.dumps({"type": "hello"}))
        assert await _recv(second) == {"type": "ready"}
        await _chat(second, "reconnected")
        await asyncio.wait_for(gateway.message_arrived.wait(), 2)
        assert gateway.events[0].text.endswith("reconnected")
        await second.close()
