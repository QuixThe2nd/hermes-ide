"""Browser sidebar joins the instant-overlap gateway restart handoff.

Live incident (Nov 2026): overlap gateway restarts succeeded and successors
came up, but every NEW sidebar turn kept landing on the draining predecessor
(successors idle at 17:29/17:30 while old PID 1438 still answered; the only
sidebar hub attach log was the predecessor's, at 14:24). Root cause:
``BrowserSidebarAdapter`` lacked ``supports_overlap_restart``, so
``defer_adapter_until_authoritative`` held the successor's sidebar adapter
until the predecessor EXITED — during an indefinite drain that never
happens, and the hub's current generation stayed the predecessor forever.

These pin the installed contract: the successor attaches without waiting for
the predecessor, the browser WebSocket survives, NEW turns route to the
successor once the predecessor's in-flight turn completes (busy-turn policy:
a follow-up for a still-running turn stays with the running owner), and the
predecessor's pending ``run_tab_js`` result plus outbound finals stay owned
by the predecessor until it detaches. No replay file, no separate lifecycle.
"""

import asyncio
import json
import os
import socket
from types import SimpleNamespace

import pytest

pytest.importorskip("websockets")

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.browser_sidebar import adapter as bs  # noqa: E402
from plugins.platforms.browser_sidebar import runtime as sidebar_runtime  # noqa: E402
from plugins.platforms.discord.hub import DiscordHubServer  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.platforms("posix")]

TOKEN = "overlap-contract-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _chat_frame(text, message_id):
    return {
        "type": "chat", "id": message_id, "text": text,
        "tab": {"id": 1, "url": "https://example.com", "title": "Example"},
    }


class _Gateway:
    """One gateway generation's sidebar adapter, with a blocking turn seam."""

    def __init__(self, harness, name: str, profile: str = "default"):
        self.adapter = bs.BrowserSidebarAdapter(PlatformConfig(
            enabled=True,
            extra={"token": TOKEN, "host": "127.0.0.1", "port": harness.port},
        ))
        self.adapter._socket_path = harness.server.socket_path
        self.adapter._owner_profile = profile
        self.name = name
        self.profile = profile
        self.events = []
        self.message_arrived = asyncio.Event()
        self.turn_release = asyncio.Event()
        self.adapter.set_message_handler(self._on_message)

    async def _on_message(self, event):
        # A "running turn": hold the session busy until the test releases it.
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

    @property
    def session_key(self) -> str:
        return self.adapter._sidebar_session_key()


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

    def add_gateway(self, name: str, profile: str = "default") -> _Gateway:
        gateway = _Gateway(self, name, profile)
        self.gateways.append(gateway)
        return gateway

    async def connect_extension(self, token: str = TOKEN):
        from websockets.asyncio.client import connect
        from websockets.typing import Subprotocol

        return await connect(
            f"ws://127.0.0.1:{self.port}/ws", subprotocols=[Subprotocol(token)],
        )


async def _recv(ws) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def _chat(ws, text="hello", message_id="m1") -> None:
    await ws.send(json.dumps(_chat_frame(text, message_id)))


async def _wait_for(condition, timeout=5.0, what="condition"):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_sidebar_adapter_is_an_overlap_hub_transport():
    """The root cause, pinned: the sidebar must start on a pending successor."""
    from gateway.overlap_restart import defer_adapter_until_authoritative

    assert bs.BrowserSidebarAdapter.supports_overlap_restart is True
    runner = SimpleNamespace(_overlap_successor_pending=True)
    adapter = bs.BrowserSidebarAdapter(PlatformConfig(
        enabled=True, extra={"token": TOKEN},
    ))
    assert defer_adapter_until_authoritative(runner, adapter) is False


async def _recv_until(ws, wanted: dict, timeout: float = 5.0) -> dict:
    """Next frame equal to *wanted*; a live turn may interleave typing/etc."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {wanted}")
        frame = json.loads(await asyncio.wait_for(ws.recv(), remaining))
        if frame == wanted:
            return frame


async def _recv_tool_request(ws, skip_request_ids=(), timeout: float = 5.0) -> dict:
    """Next tool request frame, skipping typing/draft frames and any
    *skip_request_ids* (frames a prior generation already claimed)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError("timed out waiting for a tool request frame")
        frame = json.loads(await asyncio.wait_for(ws.recv(), remaining))
        if frame.get("type") == "tool" and frame.get("request_id") not in skip_request_ids:
            return frame


async def test_successor_attaches_while_predecessor_turn_runs(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        await _chat(ws, "first turn", "m1")
        await asyncio.wait_for(old.message_arrived.wait(), 5)
        old.message_arrived.clear()


        successor = hub.add_gateway("successor")
        await successor.connect()
        assert successor.adapter._browser_connected is True

        # The browser socket survived the successor attach.
        await ws.send(json.dumps({"type": "hello"}))
        assert await _recv_until(ws, {"type": "ready"}) == {"type": "ready"}

        # Busy-turn policy: a follow-up for the still-running turn queues on
        # the running owner, never on the successor.
        await _chat(ws, "follow up mid-turn", "m2")
        await _wait_for(
            lambda: old.session_key in old.adapter._pending_messages,
            what="follow-up queued on the predecessor",
        )
        assert successor.events == []

        # Turn completes: ownership releases, the NEXT new turn goes to the
        # successor immediately — not after the predecessor exits.
        old.finish_turn()
        await _wait_for(
            lambda: old.adapter._session_versions.get(old.session_key) is None,
            what="predecessor ownership release",
        )
        await _chat(ws, "new turn for the successor", "m3")
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        assert successor.events[0].text.endswith("new turn for the successor")
        # The queued follow-up (m2) ran on the predecessor after its turn;
        # only the successor's turn (m3) ever reached the successor.
        assert [e.text.splitlines()[-1] for e in old.events] == ["first turn", "follow up mid-turn"]
        assert len(successor.events) == 1
        await ws.close()


async def test_predecessor_midturn_sends_and_pending_js_stay_owned(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        await _chat(ws, "first turn", "m1")
        await asyncio.wait_for(old.message_arrived.wait(), 5)

        # A run_tab_js request issued by the predecessor's running turn.
        js_call = asyncio.create_task(bs._run_tab_js_entry({"code": "document.title"}))
        request = await _recv_tool_request(ws)
        assert request["name"] == "run_tab_js"

        successor = hub.add_gateway("successor")
        await successor.connect()

        # Predecessor outbound still flows after the successor attached: the
        # browser must not see a mid-turn disconnect, and the final lands.
        assert (await old.adapter.send_draft("brave", 1, "working")).success
        assert await _recv_until(ws, {"type": "delta", "id": "1", "text": "working"})
        assert (await old.adapter.send("brave", "final from predecessor")).success
        assert await _recv_until(ws, {"type": "final", "id": "m1", "text": "final from predecessor"})

        # The pending JS result routes to the generation that issued the
        # request, not to the newly-attached successor.
        await ws.send(json.dumps({
            "type": "tool_result", "request_id": request["request_id"],
            "ok": True, "result": "Example",
        }))
        result = json.loads(await asyncio.wait_for(js_call, 5))
        assert result == {"success": True, "result": "Example"}
        assert successor.events == []
        await ws.close()


async def test_detached_predecessor_cannot_reclaim_or_send(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        successor = hub.add_gateway("successor")
        await successor.connect()

        await old.disconnect()
        late = await old.adapter.send("brave", "late")
        assert not late.success
        assert not await old.adapter.connect(), "a detached generation must not reclaim"

        await _chat(ws, "after detach", "m9")
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        assert successor.events[0].text.endswith("after detach")
        await ws.close()


async def test_disconnect_cleanup_drops_dead_tool_ownership(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        successor = hub.add_gateway("successor")
        await successor.connect()

        # The predecessor issues a request and detaches before the result
        # lands: ownership must die with it, not leak to the successor.
        # (No exception = the hub accepted it and recorded tool ownership.)
        await old.adapter._send_browser_frame({
            "type": "tool", "name": "run_tab_js",
            "args": {"code": "1"}, "request_id": "dead-owner-1",
        })
        await old.disconnect()

        await ws.send(json.dumps({
            "type": "tool_result", "request_id": "dead-owner-1",
            "ok": True, "result": "nobody home",
        }))

        # The successor's own request still round-trips cleanly afterwards.
        js_call = asyncio.create_task(bs._run_tab_js_entry({"code": "1+1"}))
        request = await _recv_tool_request(ws, skip_request_ids=("dead-owner-1",))
        assert request["request_id"] != "dead-owner-1"
        await ws.send(json.dumps({
            "type": "tool_result", "request_id": request["request_id"],
            "ok": True, "result": "2",
        }))
        result = json.loads(await asyncio.wait_for(js_call, 5))
        assert result == {"success": True, "result": "2"}
        await ws.close()


async def test_stale_release_cannot_steal_successor_ownership(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        await _chat(ws, "first turn", "m1")
        await asyncio.wait_for(old.message_arrived.wait(), 5)
        old_version = old.adapter._session_versions.get(old.session_key)
        assert old_version is not None

        successor = hub.add_gateway("successor")
        await successor.connect()
        old.finish_turn()
        await _wait_for(
            lambda: old.adapter._session_versions.get(old.session_key) is None,
            what="predecessor ownership release",
        )

        await _chat(ws, "successor's turn", "m2")
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        successor_version = successor.adapter._session_versions.get(successor.session_key)
        assert successor_version is not None
        # A clean release resets the fence; the successor's claim is fresh.
        assert successor_version == 1

        # A stale unregister from the old generation (version fence from the
        # pre-handoff delivery) must not move the session.
        released = await old.adapter._call("unregister", {
            "session_key": successor.session_key, "version": old_version,
        })
        assert released is not True

        successor.finish_turn()
        await _wait_for(
            lambda: successor.adapter._session_versions.get(successor.session_key) is None,
            what="successor ownership release",
        )
        await _chat(ws, "still the successor's", "m3")
        successor.message_arrived.clear()
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        assert successor.events[-1].text.endswith("still the successor's")
        await ws.close()


async def test_chat_with_all_generations_detached_is_retained_for_the_next_attach(
    short_af_unix_path,
):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        await old.disconnect()

        # No live generation: the chat parks at the hub instead of erroring,
        # and replays to whichever generation attaches next.
        await _chat(ws, "parked message", "m1")

        successor = hub.add_gateway("successor")
        await successor.connect()
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        assert successor.events[0].text.endswith("parked message")
        await ws.close()


async def test_profile_routers_do_not_mix(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        first = hub.add_gateway("first", profile="default")
        await first.connect()
        ws = await hub.connect_extension()
        await _chat(ws, "first profile turn", "m1")
        await asyncio.wait_for(first.message_arrived.wait(), 5)

        # A different profile attaching supersedes the listener owner; the
        # retired profile's generation must not receive the new traffic and
        # must not be able to re-attach.
        second = hub.add_gateway("second", profile="satellite")
        await second.connect()
        assert first.session_key != second.session_key

        first.finish_turn()
        await first.disconnect()
        await _chat(ws, "satellite profile turn", "m2")
        await asyncio.wait_for(second.message_arrived.wait(), 5)
        assert second.events[0].text.endswith("satellite profile turn")
        assert len(first.events) == 1
        await ws.close()


async def test_runtime_diagnostics_report_generation_and_pids(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        gateway = hub.add_gateway("only")
        await gateway.connect()

        diag = sidebar_runtime.diagnostics()
        entry = next(e for e in diag["entries"] if e["profile"])
        assert entry["generation_id"] == gateway.adapter._generation_id
        assert entry["gateway_pid"] == os.getpid()
        assert entry["hub_pid"] == gateway.adapter.hub_pid
        assert TOKEN not in json.dumps(diag)


async def test_draining_predecessor_hands_off_unowned_sidebar_event(short_af_unix_path):
    """Drain-window handoff: an idle-session event on a draining predecessor
    goes back to the hub and routes to the attached successor (no queueing on
    a process that is about to exit)."""
    from gateway.overlap_restart import admits_owned_work

    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        # The session must be hub-owned by the predecessor first: a real
        # routed turn (still running) records the version fence the handoff
        # will be checked against.
        await _chat(ws, "first turn", "m1")
        await asyncio.wait_for(old.message_arrived.wait(), 5)
        assert old.adapter._session_versions.get(old.session_key) is not None

        successor = hub.add_gateway("successor")
        await successor.connect()

        old.adapter.gateway_runner = SimpleNamespace(
            _draining=True,
            _running_agents={},
            _session_key_for_source=lambda source: old.session_key,
            _intake_adapter_for=lambda source: old.adapter,
        )
        old.adapter._overlap_successor_ready = True

        assert admits_owned_work(old.adapter.gateway_runner, SimpleNamespace()) is False

        event = bs._chat_frame_to_event(_chat_frame("hand me off", "m5"))
        await old.adapter._handle_message_while_active(event, old.session_key)
        assert event._gateway_accepted is True
        assert old.session_key not in old.adapter._pending_messages

        successor.message_arrived.clear()
        await asyncio.wait_for(successor.message_arrived.wait(), 5)
        assert successor.events[-1].text.endswith("hand me off")
        await ws.close()


async def test_status_rpc_reports_nonsecret_generation_state(short_af_unix_path):
    async with _HubSidebar(short_af_unix_path) as hub:
        old = hub.add_gateway("old")
        await old.connect()
        ws = await hub.connect_extension()
        successor = hub.add_gateway("successor")
        await successor.connect()

        reader, writer = await asyncio.open_unix_connection(str(hub.server.socket_path))
        writer.write((json.dumps({
            "id": "diag-1", "method": "browser_sidebar_status", "params": {},
        }) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        payload = json.loads(line)
        assert "error" not in payload
        status = payload["result"]
        assert status["browser_connected"] is True
        generations = {g["generation_id"] for g in status["generations"]}
        assert generations == {
            old.adapter._generation_id, successor.adapter._generation_id,
        }
        by_generation = {g["generation_id"]: g for g in status["generations"]}
        assert by_generation[successor.adapter._generation_id]["profile"] == "default"
        assert status["current_generation"] == successor.adapter._generation_id
        assert TOKEN not in json.dumps(status)
        await ws.close()
