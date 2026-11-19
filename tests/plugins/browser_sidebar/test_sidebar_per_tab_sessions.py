"""One Hermes session per browser tab: routing, fallback, and reply tagging.

Contract (feature: sidebar-per-tab sessions):

- A chat frame whose ``tab.id`` is a valid number derives its own session
  (``chat_id="tab-<id>"``); two tabs never share a session.
- A frame without a usable tab id falls back to the legacy single ``brave``
  session, so older extension builds keep working unchanged.
- The hub routes each tab's session key through the overlap router
  independently: a tab mid-turn on a draining predecessor stays with it while
  a new tab's first message goes straight to the successor.
- Replies (final/delta/typing) and ``run_tab_js`` requests carry the
  originating tab id back to the extension.
- ``/new`` for one tab resets only that tab's session.
"""

import asyncio
import json
import socket

import pytest

pytest.importorskip("websockets")

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.browser_sidebar import adapter as bs  # noqa: E402
from plugins.platforms.browser_sidebar import protocol  # noqa: E402
from plugins.platforms.browser_sidebar import runtime as sidebar_runtime  # noqa: E402
from plugins.platforms.discord.hub import DiscordHubServer  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.platforms("posix")]

TOKEN = "per-tab-contract-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _chat_frame(text, message_id, tab_id):
    frame = {"type": "chat", "id": message_id, "text": text}
    if tab_id is not None:
        frame["tab"] = {"id": tab_id, "url": "https://example.com", "title": "Example"}
    return frame


class _Gateway:
    """One gateway generation's sidebar adapter with a blocking turn seam."""

    def __init__(self, harness, name: str):
        self.adapter = bs.BrowserSidebarAdapter(PlatformConfig(
            enabled=True,
            extra={"token": TOKEN, "host": "127.0.0.1", "port": harness.port},
        ))
        self.adapter._socket_path = harness.server.socket_path
        self.adapter._owner_profile = "default"
        self.name = name
        self.events = []
        self.message_arrived = asyncio.Event()
        self.turn_release = asyncio.Event()
        self.adapter.set_message_handler(self._on_message)

    async def _on_message(self, event):
        self.events.append(event)
        self.message_arrived.set()
        await self.turn_release.wait()

    def finish_turn(self) -> None:
        self.turn_release.set()

    async def connect(self):
        assert await self.adapter.connect(), f"{self.name} must attach to the hub"

    async def disconnect(self):
        self.finish_turn()
        await self.adapter.disconnect()

    def session_key_for(self, frame: dict) -> str:
        """The session key the gateway derives for *frame*'s conversation."""
        return self.adapter._event_session_key(bs._chat_frame_to_event(frame))


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
        sidebar_runtime.reset()

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


async def _recv(ws, timeout=5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _recv_until(ws, wanted: dict, timeout: float = 5.0) -> dict:
    """Next frame matching EVERY key of *wanted* (subset match): frames from
    the other tab's still-running turn (keep-typing refreshes/stops)
    interleave, so consumers match on identity, not strict arrival order."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        frame = await _recv(ws, max(remaining, 0.1))
        if all(frame.get(key) == value for key, value in wanted.items()):
            return frame


async def _wait_for(condition, timeout=5.0, what="condition"):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ─── pure derivation helpers ────────────────────────────────────────────────


def test_two_tab_ids_derive_distinct_session_keys():
    adapter = bs.BrowserSidebarAdapter(PlatformConfig(enabled=True))
    key_a = adapter._event_session_key(bs._chat_frame_to_event(_chat_frame("hi", "m1", 11)))
    key_b = adapter._event_session_key(bs._chat_frame_to_event(_chat_frame("hi", "m2", 22)))
    assert key_a != key_b
    assert key_a.endswith(":dm:tab-11"), key_a
    assert key_b.endswith(":dm:tab-22"), key_b


@pytest.mark.parametrize("tab_value", [None, "?", "t1", True, 3.5, {}, "tab"])
def test_unusable_tab_ids_fall_back_to_legacy_chat_id(tab_value):
    frame = {"type": "chat", "id": "m1", "text": "hi"}
    if tab_value is not None:
        frame["tab"] = {"id": tab_value}
    event = bs._chat_frame_to_event(frame)
    assert event.source.chat_id == protocol.LEGACY_CHAT_ID


@pytest.mark.parametrize("tab_value,chat_id", [(7, "tab-7"), ("7", "tab-7"), (0, "tab-0")])
def test_usable_tab_ids_derive_tab_chat_id(tab_value, chat_id):
    frame = {"type": "chat", "id": "m1", "text": "hi", "tab": {"id": tab_value}}
    assert bs._chat_frame_to_event(frame).source.chat_id == chat_id


def test_hub_key_derivation_matches_adapter_keys():
    """The hub derives per-tab keys from the attach-shipped base key; they must
    equal the keys the adapter itself derives for the same frames."""
    adapter = bs.BrowserSidebarAdapter(PlatformConfig(enabled=True))
    base = adapter._sidebar_session_key()
    assert base.endswith(f":dm:{protocol.LEGACY_CHAT_ID}")
    for frame in (_chat_frame("a", "m1", 3), _chat_frame("b", "m2", None)):
        expected = adapter._event_session_key(bs._chat_frame_to_event(frame))
        assert protocol.session_key_for_chat_id(base, protocol.chat_id_for_frame(frame)) == expected


def test_session_key_for_chat_id_only_rewrites_the_last_segment():
    base = "agent:main:browser_sidebar:dm:brave"
    assert protocol.session_key_for_chat_id(base, "tab-9") == "agent:main:browser_sidebar:dm:tab-9"
    assert protocol.session_key_for_chat_id(base, "brave") == base
    # An unexpected base shape is never mangled: legacy single-key routing.
    assert protocol.session_key_for_chat_id("weird", "tab-9") == "weird"
    assert protocol.session_key_for_chat_id("", "tab-9") == ""


# ─── live hub routing ───────────────────────────────────────────────────────


async def test_two_tabs_route_independently_and_replies_carry_tab_ids(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("gateway")
        await gateway.connect()
        ws = await hub.connect_extension()

        frame_a = _chat_frame("tab a message", "ma", 101)
        frame_b = _chat_frame("tab b message", "mb", 202)
        assert gateway.session_key_for(frame_a) != gateway.session_key_for(frame_b)

        await ws.send(json.dumps(frame_a))
        await asyncio.wait_for(gateway.message_arrived.wait(), 5)
        assert gateway.events[0].source.chat_id == "tab-101"

        gateway.message_arrived.clear()
        await ws.send(json.dumps(frame_b))
        await asyncio.wait_for(gateway.message_arrived.wait(), 5)
        assert gateway.events[1].source.chat_id == "tab-202"

        # Replies for a tab carry that tab's id back to the extension. The
        # still-running tab-101 turn keeps emitting its own typing frames, so
        # match on identity instead of consuming frames in strict order.
        await gateway.adapter.send_typing("tab-202")
        typing = await _recv_until(ws, {"type": "typing", "active": True, "tab_id": 202})
        assert typing == {"type": "typing", "active": True, "tab_id": 202}

        assert (await gateway.adapter.send_draft("tab-202", 4, "working")).success
        delta = await _recv_until(ws, {"type": "delta", "text": "working", "tab_id": 202})
        assert delta == {"type": "delta", "id": "4", "text": "working", "tab_id": 202}

        assert (await gateway.adapter.send("tab-202", "done for tab b")).success
        final = await _recv_until(ws, {"type": "final", "text": "done for tab b", "tab_id": 202})
        assert final == {"type": "final", "id": "mb", "text": "done for tab b", "tab_id": 202}

        # Legacy chat ids keep the exact old frame shape (older extension builds).
        assert (await gateway.adapter.send_draft("brave", 5, "legacy")).success
        legacy = await _recv_until(ws, {"type": "delta", "text": "legacy"})
        assert legacy == {"type": "delta", "id": "5", "text": "legacy"}
        await ws.close()


async def test_no_tab_id_frame_routes_to_the_legacy_session(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("gateway")
        await gateway.connect()
        ws = await hub.connect_extension()

        legacy_frame = _chat_frame("legacy build message", "m1", None)
        assert gateway.session_key_for(legacy_frame) == gateway.adapter._sidebar_session_key()

        await ws.send(json.dumps(legacy_frame))
        await asyncio.wait_for(gateway.message_arrived.wait(), 5)
        assert gateway.events[0].source.chat_id == "brave"
        await ws.close()


async def test_overlap_handoff_routes_per_tab_session_key(short_af_unix_path):
    """A tab mid-turn on the predecessor stays there; a new tab's session goes
    straight to the successor — per session key, never one global owner."""
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()

        turn_frame = _chat_frame("tab 1 turn", "m1", 1)
        turn_key = old.session_key_for(turn_frame)
        await ws.send(json.dumps(turn_frame))
        await asyncio.wait_for(old.message_arrived.wait(), 5)
        old.message_arrived.clear()

        successor = hub.add_gateway("successor")
        await successor.connect()

        # In-flight tab 1 follow-up queues on the running owner.
        await ws.send(json.dumps(_chat_frame("tab 1 follow-up", "m2", 1)))
        await _wait_for(
            lambda: turn_key in old.adapter._pending_messages,
            what="tab 1 follow-up queued on the predecessor",
        )
        assert successor.events == []

        # A brand-new tab is a new session: it routes to the successor now.
        await ws.send(json.dumps(_chat_frame("tab 2 first turn", "m3", 2)))
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        assert successor.events[0].source.chat_id == "tab-2"
        assert [e.source.chat_id for e in old.events] == ["tab-1"]

        # After the tab 1 turn completes, that tab's next message also moves on.
        old.finish_turn()
        await _wait_for(
            lambda: old.adapter._session_versions.get(turn_key) is None,
            what="predecessor ownership release for tab 1",
        )
        successor.finish_turn()
        await _wait_for(
            lambda: not successor.adapter._session_versions,
            what="successor ownership release",
        )
        old.message_arrived.clear()
        successor.message_arrived.clear()
        await ws.send(json.dumps(_chat_frame("tab 1 new turn", "m4", 1)))
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        assert successor.events[-1].source.chat_id == "tab-1"
        assert not old.message_arrived.is_set()
        await ws.close()


async def test_run_tab_js_request_carries_the_running_sessions_tab_id(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("gateway")
        await gateway.connect()
        ws = await hub.connect_extension()

        await ws.send(json.dumps(_chat_frame("read the page", "m1", 33)))
        await asyncio.wait_for(gateway.message_arrived.wait(), 5)
        session_key = gateway.session_key_for(_chat_frame("read the page", "m1", 33))

        # The gateway binds the turn's session key around tool dispatch;
        # run_tab_js must resolve the tab from it, not from browser focus.
        from tools.session_context import reset_current_session_key, set_current_session_key

        token = set_current_session_key(session_key)
        try:
            call = asyncio.create_task(bs._run_tab_js_entry({"code": "document.title"}))
            request = await _recv_until(ws, {"type": "tool", "tab_id": 33})
        finally:
            reset_current_session_key(token)
        assert request["type"] == "tool"
        assert request["name"] == "run_tab_js"
        assert request["tab_id"] == 33
        await ws.send(json.dumps({
            "type": "tool_result", "request_id": request["request_id"],
            "ok": True, "result": "Example",
        }))
        payload = json.loads(await asyncio.wait_for(call, 5))
        assert payload["success"] is True
        await ws.close()


async def test_run_tab_js_without_session_context_keeps_legacy_frame_shape(short_af_unix_path):
    """No ambient turn session (direct CLI/test dispatch): no tab_id on the
    frame, and the extension falls back to the focused tab — old behavior."""
    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("gateway")
        await gateway.connect()
        ws = await hub.connect_extension()

        call = asyncio.create_task(bs._run_tab_js_entry({"code": "1"}))
        request = await _recv_until(ws, {"type": "tool"})
        assert request["type"] == "tool"
        assert "tab_id" not in request
        await ws.send(json.dumps({
            "type": "tool_result", "request_id": request["request_id"],
            "ok": False, "error": "stopped",
        }))
        payload = json.loads(await asyncio.wait_for(call, 5))
        assert payload["success"] is False
        await ws.close()


async def test_new_command_routes_to_only_that_tabs_session(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("gateway")
        await gateway.connect()
        ws = await hub.connect_extension()

        # Both tabs have live sessions; the new-chat button sends /new for tab 5.
        for tab_id, text in ((5, "tab five chat"), (6, "tab six chat")):
            await ws.send(json.dumps(_chat_frame(text, f"m{tab_id}", tab_id)))
            await asyncio.wait_for(gateway.message_arrived.wait(), 5)
            gateway.message_arrived.clear()

        key_five = gateway.session_key_for(_chat_frame("", "x", 5))
        key_six = gateway.session_key_for(_chat_frame("", "x", 6))
        # Finish the running turn: /new during a turn would queue behind it
        # (the gateway's two message guards), like any other platform.
        gateway.finish_turn()
        gateway.message_arrived.clear()
        await ws.send(json.dumps(_chat_frame("/new", "reset", 5)))
        await asyncio.wait_for(gateway.message_arrived.wait(), 5)

        # The /new landed as a message on tab 5's session only.
        assert gateway.events[-1].text.endswith("/new")
        assert gateway.events[-1].source.chat_id == "tab-5"
        # The two tabs' sessions stayed distinct end to end.
        assert key_five != key_six
        assert key_five.endswith(":dm:tab-5") and key_six.endswith(":dm:tab-6")
        await ws.close()
