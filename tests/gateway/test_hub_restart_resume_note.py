"""Restart-resume injection: the hub flags an owner-generation change, the gateway turns it into a
one-shot ephemeral turn note.

Behavior contract:
- the router stamps the delivery record ONLY when the session's routed generation differs from its
  previous owner (a restart happened between turns);
- same-generation turns and first-ever deliveries carry no flag;
- the flag fires exactly once (the owner-change delivery), not on subsequent turns;
- the gateway composes the ``[System note: Hermes restarted ...]`` text from the event-stamped info.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import PlatformConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.session import SessionSource
from plugins.platforms.discord.hub import DiscordSessionRouter
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter


class FakeHub:
    def __init__(self):
        self.router = DiscordSessionRouter()
        self.received = {}

    def attach(self, generation, pid=None):
        self.received.setdefault(generation, [])

        async def deliver(session_key, record):
            self.received[generation].append(record)

        self.router.attach(generation, deliver, gateway_pid=pid)

    def detach(self, generation):
        self.router.detach(generation)


def run(coro):
    return asyncio.run(coro)


def test_generation_change_turn_carries_restart_flag():
    hub = FakeHub()
    hub.attach("A", pid=111)
    run(hub.router.route("sk", {"session_key": "sk", "text": "before restart"}))
    # Restart: predecessor detaches, successor takes the session.
    hub.detach("A")
    hub.attach("B", pid=222)

    assert run(hub.router.route("sk", {"session_key": "sk", "text": "after restart"})) == "B"
    record = hub.received["B"][-1]
    assert record.get("_hub_generation_changed") is True
    assert record.get("_hub_previous_generation") == "A"


def test_live_handoff_to_successor_carries_previous_pid():
    # Overlap restart: the still-attached predecessor hands the event off to
    # its successor while it still owns the session, so its pid is known.
    hub = FakeHub()
    hub.attach("A", pid=111)
    run(hub.router.route("sk", {"session_key": "sk", "text": "one"}))
    hub.attach("B", pid=222)
    version = 1

    assert run(hub.router.handoff(
        "sk", "A", version, {"session_key": "sk", "text": "two"},
    )) is True
    record = hub.received["B"][-1]
    assert record.get("_hub_generation_changed") is True
    assert record.get("_hub_previous_generation") == "A"
    assert record.get("_hub_previous_pid") == 111


def test_same_generation_turn_has_no_restart_flag():
    hub = FakeHub()
    hub.attach("A")
    run(hub.router.route("sk", {"session_key": "sk", "text": "one"}))

    assert run(hub.router.route("sk", {"session_key": "sk", "text": "two"})) == "A"
    assert "_hub_generation_changed" not in hub.received["A"][-1]


def test_first_ever_delivery_has_no_restart_flag():
    hub = FakeHub()
    hub.attach("A")

    run(hub.router.route("sk", {"session_key": "sk", "text": "hello"}))
    assert "_hub_generation_changed" not in hub.received["A"][-1]


def test_restart_flag_fires_once_not_every_turn():
    hub = FakeHub()
    hub.attach("A", pid=111)
    run(hub.router.route("sk", {"session_key": "sk", "text": "before restart"}))
    hub.detach("A")
    hub.attach("B")

    run(hub.router.route("sk", {"session_key": "sk", "text": "resume"}))
    assert hub.received["B"][0].get("_hub_generation_changed") is True

    run(hub.router.route("sk", {"session_key": "sk", "text": "next"}))
    assert "_hub_generation_changed" not in hub.received["B"][1]


def test_hub_client_stamps_restart_info_on_event():
    from gateway.run_drain_queue import serialize_drain_event

    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    captured = {}
    adapter.handle_message = AsyncMock(side_effect=lambda ev: captured.setdefault("event", ev))

    source = SessionSource(
        platform=Platform.DISCORD, profile="default", user_id="u", chat_id="c",
    )
    event = MessageEvent(source=source, text="resume", message_id="m1")
    record = serialize_drain_event("sk", event)
    record["_hub_generation_changed"] = True
    record["_hub_previous_generation"] = "gen-old"
    record["_hub_previous_pid"] = 111

    asyncio.run(adapter._receive_event({"record": record, "session_key": "sk"}))
    info = getattr(captured["event"], "_hub_generation_restart", None)
    assert isinstance(info, dict)
    assert info["previous_generation"] == "gen-old"
    assert info["previous_pid"] == 111
    assert info["generation"] == adapter._generation_id


def _note(event):
    return GatewayAgentCacheMixin._hub_restart_sidecar_note(SimpleNamespace(), event)


def test_restart_note_composed_from_event_info():
    event = SimpleNamespace(_hub_generation_restart={"pid": 4242})
    note = _note(event)
    assert note is not None
    assert note.startswith(
        "[System note: Hermes restarted since your last message in this chat —"
    )
    assert "new gateway instance" in note
    assert "pid 4242" in note
    assert "Conversation history is intact" in note
    assert "re-verify environment assumptions" in note


def test_no_restart_info_yields_no_note():
    assert _note(SimpleNamespace()) is None
    assert _note(SimpleNamespace(_hub_generation_restart=None)) is None
    assert _note(SimpleNamespace(_hub_generation_restart="junk")) is None
