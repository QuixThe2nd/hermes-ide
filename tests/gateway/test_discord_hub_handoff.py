"""Focused ownership tests for the multi-generation Discord hub."""

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.run_drain_queue import serialize_drain_event
from gateway.session import SessionSource
from plugins.platforms.discord.hub import DiscordHubServer, DiscordSessionRouter, _token_key
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter


class FakeWebsocket:
    def __init__(self):
        self.closed = False


class FakeHub:
    def __init__(self):
        self.router = DiscordSessionRouter()
        self.websocket = FakeWebsocket()
        self.received = {"A": [], "B": []}

    def attach(self, generation):
        async def deliver(session_key, record):
            self.received[generation].append((session_key, record["text"]))
        self.router.attach(generation, deliver)

    def detach(self, generation):
        # Client teardown owns no Discord transport operation.
        self.router.detach(generation)


def run(coro):
    return asyncio.run(coro)


def test_new_session_goes_only_to_current_generation():
    hub = FakeHub()
    hub.attach("A")
    hub.attach("B")

    assert run(hub.router.route("new", {"session_key": "new", "text": "hello"})) == "B"
    assert hub.received == {"A": [], "B": [("new", "hello")]}


def test_owned_inflight_session_stays_on_predecessor():
    hub = FakeHub()
    hub.attach("A")
    run(hub.router.route("busy", {"session_key": "busy", "text": "first"}))
    hub.attach("B")

    assert run(hub.router.route("busy", {"session_key": "busy", "text": "follow up"})) == "A"
    assert hub.received["B"] == []
    assert hub.received["A"] == [("busy", "first"), ("busy", "follow up")]


def test_unregister_moves_next_event_to_successor():
    hub = FakeHub()
    hub.attach("A")
    run(hub.router.route("busy", {"session_key": "busy", "text": "first"}))
    hub.attach("B")

    assert hub.router.unregister("busy", "A")
    assert run(hub.router.route("busy", {"session_key": "busy", "text": "next"})) == "B"
    assert hub.received["B"] == [("busy", "next")]


def test_stale_unregister_cannot_move_a_session_with_a_newer_delivery():
    hub = FakeHub()
    hub.attach("A")
    run(hub.router.route("busy", {"session_key": "busy", "text": "first"}))
    hub.attach("B")
    run(hub.router.route("busy", {"session_key": "busy", "text": "follow up"}))

    assert not hub.router.unregister("busy", "A", version=1)
    assert hub.router.owner_for("busy") == "A"
    assert hub.router.unregister("busy", "A", version=2)
    assert run(hub.router.route("busy", {"session_key": "busy", "text": "next"})) == "B"


def test_internal_and_followup_turns_own_then_release_hub_session():
    """Internal wakes take hub ownership and retain it through queued follow-ups."""
    async def scenario():
        router = DiscordSessionRouter()
        adapter = DiscordHubClientAdapter(PlatformConfig(
            token="fake-token", typing_indicator=False,
        ))
        adapter._writer = MagicMock()
        generation = adapter._generation_id

        async def deliver(session_key, record):
            await adapter._receive_event({"session_key": session_key, "record": record})

        async def rpc(method, params=None):
            params = params or {}
            if method == "register":
                return await router.register(params["session_key"], generation)
            if method == "route":
                return await router.route(params["session_key"], params["record"])
            if method == "unregister":
                return router.unregister(
                    params["session_key"], generation, params.get("version")
                )
            raise AssertionError(f"unexpected hub RPC: {method}")

        router.attach(generation, deliver)
        adapter._call = rpc
        first_started = asyncio.Event()
        first_finished = asyncio.Event()
        finish_first = asyncio.Event()
        processed = []

        async def handler(event):
            processed.append(event.text)
            if len(processed) == 1:
                first_started.set()
                try:
                    await finish_first.wait()
                finally:
                    first_finished.set()
            return None

        adapter.set_message_handler(handler)
        source = SessionSource(
            platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="7"
        )
        internal = MessageEvent(
            text="internal wake", source=source, message_id="internal-1", internal=True
        )
        replay_completion = MagicMock()
        internal._gateway_replay_completion = replay_completion
        session_key = adapter._event_session_key(internal)
        successor_received = []

        async def successor_delivery(key, record):
            successor_received.append(record)

        router.attach("B", successor_delivery)
        try:
            await adapter.handle_message(internal)
            initial_accepted = internal._gateway_accepted
            await asyncio.wait_for(first_started.wait(), timeout=2)
            initial_owner = router.owner_for(session_key)

            followup = MessageEvent(text="follow up", source=source, message_id="user-1")
            record = serialize_drain_event(session_key, followup)
            followup_generation = await router.route(session_key, record)
            successor_after_followup = len(successor_received)
            owner_with_followup = router.owner_for(session_key)

            finish_first.set()
            await asyncio.wait_for(first_finished.wait(), timeout=2)
            for _ in range(200):
                if len(processed) == 2 and router.owner_for(session_key) is None:
                    break
                await asyncio.sleep(0.01)
            idle_record = serialize_drain_event(
                session_key, MessageEvent(text="after idle", source=source, message_id="user-2")
            )
            idle_generation = await router.route(session_key, idle_record)
        finally:
            finish_first.set()
            if first_started.is_set():
                await asyncio.wait_for(first_finished.wait(), timeout=2)
            if adapter._release_tasks:
                await asyncio.gather(*list(adapter._release_tasks), return_exceptions=True)
            await BasePlatformAdapter.cancel_background_tasks(adapter)

        assert initial_owner == generation
        assert initial_accepted is True
        assert replay_completion.call_count == 1
        assert followup_generation == generation
        assert successor_after_followup == 0
        assert owner_with_followup == generation
        assert processed == ["internal wake", "follow up"]
        assert idle_generation == "B"
        assert len(successor_received) == 1

    run(scenario())


@pytest.mark.asyncio
async def test_cancel_releases_the_version_captured_at_cleanup():
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    adapter._writer = MagicMock()
    adapter._call = AsyncMock(return_value=False)
    session_key = "agent:main:discord:42"
    adapter._session_versions[session_key] = 7

    await adapter.cancel_session_processing(session_key)
    # A newer delivery must not let the old cleanup unregister its ownership.
    adapter._session_versions[session_key] = 8
    if adapter._release_tasks:
        await asyncio.gather(*list(adapter._release_tasks))

    adapter._call.assert_awaited_once_with("unregister", {
        "session_key": session_key,
        "version": 7,
    })


@pytest.mark.asyncio
async def test_successful_old_release_preserves_a_newer_cached_version():
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    session_key = "agent:main:discord:42"
    adapter._session_versions[session_key] = 7
    release_started = asyncio.Event()
    finish_release = asyncio.Event()

    async def rpc(method, params=None):
        assert method == "unregister"
        release_started.set()
        await finish_release.wait()
        return True

    adapter._call = rpc
    release = asyncio.create_task(adapter._release_ownership(session_key, 7))
    await release_started.wait()
    adapter._session_versions[session_key] = 8
    finish_release.set()
    await release

    assert adapter._session_versions[session_key] == 8


@pytest.mark.asyncio
async def test_post_command_drain_releases_idle_ownership():
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    adapter._call = AsyncMock(return_value=True)
    session_key = "agent:main:discord:42"
    adapter._session_versions[session_key] = 9
    command_guard = asyncio.Event()
    adapter._active_sessions[session_key] = command_guard

    await adapter._drain_pending_after_session_command(session_key, command_guard)
    if adapter._release_tasks:
        await asyncio.gather(*list(adapter._release_tasks))

    adapter._call.assert_awaited_once_with("unregister", {
        "session_key": session_key,
        "version": 9,
    })


@pytest.mark.asyncio
async def test_handoff_moves_idle_event_to_current_and_guards_loops():
    router = DiscordSessionRouter()
    received = {"A": [], "B": [], "C": []}

    def delivery(generation):
        async def deliver(_session_key, record):
            received[generation].append(record)
        return deliver

    router.attach("A", delivery("A"))
    record = {"session_key": "idle", "event": {"text": "hand me off"}}
    assert await router.route("idle", record) == "A"
    router.attach("B", delivery("B"))

    predecessor_copy = received["A"][0]
    assert await router.handoff(
        "idle", "A", predecessor_copy["_discord_hub_version"], predecessor_copy
    ) is True
    assert len(received["B"]) == 1
    assert router.owner_for("idle") == "B"
    assert router.undelivered == []

    # A stale retry cannot deliver the same event twice.
    assert await router.handoff(
        "idle", "A", predecessor_copy["_discord_hub_version"], predecessor_copy
    ) is False
    assert len(received["A"]) == len(received["B"]) == 1

    # With no unvisited generation left, the hub retains rather than ping-ponging.
    successor_copy = received["B"][0]
    assert await router.handoff(
        "idle", "B", successor_copy["_discord_hub_version"], successor_copy
    ) is True
    assert len(received["A"]) == len(received["B"]) == 1
    assert len(router.undelivered) == 1

    router.attach("C", delivery("C"))
    assert await router.replay_undelivered() == 1
    assert len(received["C"]) == 1
    assert len(received["A"]) == len(received["B"]) == 1
    assert router.undelivered == []


@pytest.mark.asyncio
async def test_targeted_handoff_returns_event_to_predecessor():
    router = DiscordSessionRouter()
    received = {"A": [], "B": []}

    def delivery(generation):
        async def deliver(_session_key, record):
            received[generation].append(record)
        return deliver

    router.attach("A", delivery("A"), gateway_pid=101)
    router.attach("B", delivery("B"), gateway_pid=202)
    record = {"session_key": "busy", "event": {"text": "join the live turn"}}
    assert await router.route("busy", record) == "B"
    successor_copy = received["B"][0]

    assert await router.handoff(
        "busy", "B", successor_copy["_discord_hub_version"], successor_copy,
        target_pid=101,
    ) is True

    assert len(received["A"]) == 1
    assert received["A"][0]["event"]["text"] == "join the live turn"
    assert router.owner_for("busy") == "A"
    assert router.undelivered == []


@pytest.mark.asyncio
async def test_targeted_handoff_refuses_a_detached_target_without_retaining():
    router = DiscordSessionRouter()
    received = {"A": [], "B": []}

    def delivery(generation):
        async def deliver(_session_key, record):
            received[generation].append(record)
        return deliver

    router.attach("A", delivery("A"), gateway_pid=101)
    router.attach("B", delivery("B"), gateway_pid=202)
    record = {"session_key": "busy", "event": {"text": "join the live turn"}}
    assert await router.route("busy", record) == "B"
    successor_copy = received["B"][0]
    router.detach("A")

    assert await router.handoff(
        "busy", "B", successor_copy["_discord_hub_version"], successor_copy,
        target_pid=101,
    ) is False
    assert router.undelivered == []
    assert len(received["B"]) == 1


@pytest.mark.asyncio
async def test_client_preserves_handoff_history_across_deserialization():
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    adapter.handle_message = AsyncMock()
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="7",
    )
    event = MessageEvent(text="do not ping-pong", source=source, message_id="busy-1")
    session_key = adapter._event_session_key(event)
    record = serialize_drain_event(session_key, event)
    record["_discord_hub_version"] = 2
    record["_discord_hub_version_handoffs"] = ["A"]

    await adapter._receive_event({"session_key": session_key, "record": record})
    received_event = adapter.handle_message.await_args.args[0]
    assert received_event._hub_handoffs == ["A"]

    adapter._call = AsyncMock(return_value=False)
    assert await adapter.handoff_event(received_event, session_key) is False
    assert received_event._gateway_accepted is False
    method, params = adapter._call.await_args.args
    assert method == "handoff"
    assert params["record"]["_discord_hub_version_handoffs"] == ["A"]


def test_client_disconnect_keeps_websocket_and_records_unavailable_delivery():
    hub = FakeHub()
    hub.attach("A")
    run(hub.router.route("owned", {"session_key": "owned", "text": "first"}))
    hub.detach("A")

    assert not hub.websocket.closed
    assert run(hub.router.route("owned", {"session_key": "owned", "text": "while detached"})) is None
    assert hub.router.undelivered == [{"session_key": "owned", "text": "while detached"}]

    hub.attach("B")
    assert run(hub.router.replay_undelivered()) == 1
    assert hub.received["B"] == [("owned", "while detached")]


def test_bounce_preserves_hub_identity_and_splits_new_from_owned_work():
    hub = FakeHub()
    hub_identity = id(hub)
    hub.attach("A")
    run(hub.router.route("inflight", {"session_key": "inflight", "text": "working"}))

    hub.attach("B")
    assert id(hub) == hub_identity
    assert run(hub.router.route("new", {"session_key": "new", "text": "new work"})) == "B"
    assert run(hub.router.route("inflight", {"session_key": "inflight", "text": "still mine"})) == "A"
    assert hub.received["B"] == [("new", "new work")]
    assert hub.received["A"][-1] == ("inflight", "still mine")


def test_current_generation_is_independent_for_each_bot_token(tmp_path):
    server = DiscordHubServer(tmp_path / "unused.sock")
    first = server._router_for("token-one")
    second = server._router_for("token-two")
    received = {name: [] for name in ("A1", "A2", "B1")}

    def delivery(name):
        async def deliver(session_key, record):
            received[name].append(session_key)
        return deliver

    first.attach("A1", delivery("A1"))
    second.attach("A2", delivery("A2"))
    first.attach("B1", delivery("B1"))

    assert run(first.route("new-one", {"session_key": "new-one"})) == "B1"
    assert run(second.route("new-two", {"session_key": "new-two"})) == "A2"
    assert received == {"A1": [], "A2": ["new-two"], "B1": ["new-one"]}


@pytest.mark.platforms("posix")
def test_unix_hub_supports_two_clients_rpc_and_detach(tmp_path):
    async def scenario():
        server = DiscordHubServer(tmp_path / "discord-hub.sock")

        class FakeDiscordConnection:
            def __init__(self):
                self.sent = []
                self.disconnects = 0

            async def send(self, chat_id, content, **kwargs):
                self.sent.append((chat_id, content, kwargs))
                return SendResult(success=True, message_id="m1")

            async def disconnect(self):
                self.disconnects += 1

        connection = FakeDiscordConnection()

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[_token_key(config_data["token"])] = connection

        server._attach_adapter = fake_attach
        await server.start()
        clients = [DiscordHubClientAdapter(PlatformConfig(token="fake-token")) for _ in range(2)]
        received = {"A": [], "B": []}
        try:
            for client, generation in zip(clients, ("A", "B")):
                client._socket_path = server.socket_path

                async def receive(event, generation=generation):
                    received[generation].append(event.text)

                client.handle_message = receive

            assert await clients[0].connect()
            first_hub_pid = clients[0].hub_pid

            source = SessionSource(platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="7")
            event = MessageEvent(text="first", source=source, message_id="d1")
            record = serialize_drain_event("agent:main:discord:42", event)
            assert await server.router.route(record["session_key"], record) is not None

            assert await clients[1].connect()
            assert clients[1].hub_pid == first_hub_pid
            await server.router.route(record["session_key"], record)
            new_record = serialize_drain_event(
                "agent:main:discord:new", MessageEvent(text="new", source=source, message_id="d2"))
            await server.router.route(new_record["session_key"], new_record)

            result = await clients[1].send("42", "outbound")
            assert result.success and result.message_id == "m1"
            assert connection.sent[-1][0:2] == ("42", "outbound")

            for _ in range(100):
                if len(received["A"]) >= 2 and received["B"]:
                    break
                await asyncio.sleep(0.01)
            assert received["A"] == ["first", "first"]
            assert received["B"] == ["new"]

            await clients[0].disconnect()
            assert connection.disconnects == 0
            assert (await clients[1]._call("ping"))["pid"] == first_hub_pid
        finally:
            await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)
            await server.stop()

    run(scenario())


@pytest.mark.platforms("posix")
def test_overlap_admission_opens_only_after_successor_attaches(tmp_path):
    async def scenario():
        server = DiscordHubServer(tmp_path / "discord-hub.sock")

        class FakeDiscordConnection:
            async def disconnect(self):
                pass

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[_token_key(config_data["token"])] = FakeDiscordConnection()

        server._attach_adapter = fake_attach
        await server.start()
        clients = [DiscordHubClientAdapter(PlatformConfig(token="fake-token")) for _ in range(2)]
        successor_attach = None
        try:
            for client in clients:
                client._socket_path = server.socket_path
            assert await clients[0].connect()
            assert not clients[0].admits_owned_work_while_draining()

            async def fake_spawn(params):
                nonlocal successor_attach
                successor_attach = asyncio.create_task(clients[1].connect())
                return {"pid": 5150}

            server._spawn_gateway = fake_spawn
            assert await clients[0].spawn_overlap_successor() == 5150
            assert await successor_attach
            assert server.router.current_generation == clients[1]._generation_id
            assert clients[0].admits_owned_work_while_draining()
        finally:
            if successor_attach is not None:
                await asyncio.gather(successor_attach, return_exceptions=True)
            await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)
            await server.stop()

    run(scenario())


@pytest.mark.asyncio
async def test_active_busy_event_before_successor_attach_parks_at_hub():
    """Pre-attach overlap window: a busy-session follow-up on a draining
    predecessor is handed to the hub, which retains it for the successor —
    no drain-queue snapshot, no re-delivery banner."""
    from gateway.run_drain_queue import drain_queue_path
    from tests.gateway.restart_test_helpers import make_restart_runner

    router = DiscordSessionRouter()
    predecessor_received = []
    successor_received = []

    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    router.attach(adapter._generation_id, lambda key, record: _hub_sink_record(
        predecessor_received, key, record))
    runner, _ = make_restart_runner(adapter, platform=Platform.DISCORD)
    adapter.gateway_runner = runner
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    adapter._running = True
    # Successor NOT attached yet: readiness gate is shut, the hub is the park.
    adapter._call = _router_rpc(router, adapter)
    adapter._send_with_retry = AsyncMock()

    source = SessionSource(
        platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="7"
    )
    event = MessageEvent(text="during the pre-attach window", source=source, message_id="busy-1")
    session_key = adapter._event_session_key(event)
    assert await router.route(session_key, {
        "session_key": session_key, "event": {"text": "original delivery"},
    }) == adapter._generation_id
    adapter._session_versions[session_key] = (
        predecessor_received[0][1]["_discord_hub_version"])

    await adapter._handle_message_while_active(event, session_key)

    assert event._gateway_accepted is True
    assert len(router.undelivered) == 1  # parked at the hub, unacknowledged
    assert len(predecessor_received) == 1  # never re-delivered to the predecessor
    assert not drain_queue_path().exists()
    adapter._send_with_retry.assert_not_awaited()

    # The successor attaches and claims the parked record.
    router.attach("successor-gen", lambda key, record: _hub_sink_record(
        successor_received, key, record))
    assert await router.replay_undelivered() == 1
    assert len(successor_received) == 1
    assert successor_received[0][1]["event"]["text"] == "during the pre-attach window"


async def _hub_sink_record(sink, session_key, record):
    sink.append((session_key, record))


@pytest.mark.asyncio
async def test_unready_handoff_retries_until_attach_deadline_then_falls_through():
    """A hub that keeps refusing with no attached successor is retried up to
    the successor-attach deadline; only then may the event fall through to
    the caller's durable drain queue (the crash-only fallback)."""
    from gateway.overlap_restart import handoff_overlap_event

    attempts = []

    async def refusing_handoff(event, session_key, target_pid=None):
        attempts.append(session_key)
        return False

    adapter = SimpleNamespace(
        supports_overlap_restart=True,
        admits_owned_work_while_draining=lambda source=None: False,
        handoff_event=refusing_handoff,
        overlap_successor_attach_timeout=0.03,
    )
    runner = SimpleNamespace(
        _intake_adapter_for=lambda source: adapter,
        _session_key_for_source=lambda source: "agent:main:discord:42",
    )
    event = SimpleNamespace(source=SimpleNamespace())

    assert not await handoff_overlap_event(
        runner, event, "agent:main:discord:42", require_successor_ready=True,
    )
    assert len(attempts) >= 2  # first refusal did not fall through immediately
    assert event._gateway_accepted is False


def _router_rpc(router, adapter):
    async def rpc(method, params=None):
        params = params or {}
        if method == "handoff":
            return await router.handoff(
                params["session_key"], adapter._generation_id,
                int(params["version"]), params["record"],
                target_pid=params.get("target_pid"),
            )
        if method == "route":
            return await router.route(params["session_key"], params["record"])
        if method == "register":
            return await router.register(params["session_key"], adapter._generation_id)
        if method == "unregister":
            return router.unregister(
                params["session_key"], adapter._generation_id, params.get("version")
            )
        if method == "ping":
            return {"pid": os.getpid(), "current_generation": router.current_generation}
        raise AssertionError(f"unexpected hub RPC: {method}")
    return rpc


@pytest.mark.asyncio
async def test_active_busy_event_hands_off_before_drain_ack():
    from gateway.run_drain_queue import drain_queue_path
    from tests.gateway.restart_test_helpers import make_restart_runner

    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    runner, _ = make_restart_runner(adapter, platform=Platform.DISCORD)
    adapter.gateway_runner = runner
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    adapter._running = True
    adapter._overlap_successor_ready = True
    adapter._call = AsyncMock(return_value=True)
    adapter._send_with_retry = AsyncMock()

    source = SessionSource(
        platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="7"
    )
    event = MessageEvent(text="do not queue me", source=source, message_id="busy-1")
    session_key = adapter._event_session_key(event)
    adapter._session_versions[session_key] = 3

    await adapter._handle_message_while_active(event, session_key)

    method, params = adapter._call.await_args.args
    assert method == "handoff"
    assert params["session_key"] == session_key
    assert params["version"] == 3
    assert params["record"]["event"]["text"] == "do not queue me"
    assert params["target_pid"] is None
    adapter._send_with_retry.assert_not_awaited()
    assert event._gateway_accepted is True
    assert not drain_queue_path().exists()


def test_systemd_successor_is_spawned_by_predecessor_and_registered_with_hub(monkeypatch, tmp_path):
    """A service successor must remain in the gateway unit, not the hub unit."""
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    monkeypatch.setenv("INVOCATION_ID", "gateway-unit")
    monkeypatch.setattr("plugins.platforms.discord.hub_client.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.restart.detached_restart_spawn_blocked", lambda: False)
    local_spawn = MagicMock(return_value=SimpleNamespace(pid=5150))
    monkeypatch.setattr("gateway.restart.spawn_replacement_gateway", local_spawn)
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        return {"pid": 5150}

    adapter._call = rpc
    adapter.wait_for_overlap_successor = AsyncMock()

    assert run(adapter.spawn_overlap_successor(via_service=True, predecessor_pid=4242)) == 5150

    local_spawn.assert_called_once()
    assert calls == [("register_spawned_gateway", {
        "pid": 5150, "hermes_home": str(tmp_path), "predecessor_pid": 4242,
    })]
    assert adapter._last_overlap_successor_service_managed is True


def test_systemd_successor_is_predecessor_local_even_for_detached_request(monkeypatch, tmp_path):
    """Actual gateway supervision, not the request flag, selects the unit cgroup."""
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    monkeypatch.setenv("INVOCATION_ID", "gateway-unit")
    monkeypatch.setattr("plugins.platforms.discord.hub_client.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.restart.detached_restart_spawn_blocked", lambda: False)
    local_spawn = MagicMock(return_value=SimpleNamespace(pid=5150))
    monkeypatch.setattr("gateway.restart.spawn_replacement_gateway", local_spawn)
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        return {"pid": 5150}

    adapter._call = rpc
    adapter.wait_for_overlap_successor = AsyncMock()

    assert run(adapter.spawn_overlap_successor(via_service=False, predecessor_pid=4242)) == 5150

    local_spawn.assert_called_once()
    assert calls == [("register_spawned_gateway", {
        "pid": 5150, "hermes_home": str(tmp_path), "predecessor_pid": 4242,
    })]
    assert adapter._last_overlap_successor_service_managed is True


def test_overlap_spawn_supervisor_env_is_scoped_to_launcher(monkeypatch, tmp_path):
    """A hub child must not inherit the hub unit's restart/notify identity."""
    import subprocess
    from plugins.platforms.discord.overlap_spawn import spawn_gateway_successor

    monkeypatch.setattr("gateway.restart.detached_restart_spawn_blocked", lambda: False)
    supervisor_env = {
        "PATH": "/usr/bin",
        "INVOCATION_ID": "hub-unit",
        "NOTIFY_SOCKET": "/run/systemd/notify",
        "WATCHDOG_USEC": "1000000",
        "WATCHDOG_PID": "4242",
        "XPC_SERVICE_NAME": "hub-launchd-job",
        "HERMES_SUPERVISED_CHILD": "1",
    }
    monkeypatch.setattr(
        "tools.environments.local.served_profile_child_env",
        lambda **kwargs: dict(supervisor_env),
    )
    captured = []

    def fake_popen(argv, **kwargs):
        captured.append(kwargs["env"])
        return SimpleNamespace(pid=5150)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_gateway_successor(home=str(tmp_path), predecessor_pid=4242)
    for marker in supervisor_env.keys() - {"PATH"}:
        assert marker not in captured[-1]

    spawn_gateway_successor(
        home=str(tmp_path), predecessor_pid=4242, preserve_supervisor=True,
    )
    for marker, value in supervisor_env.items():
        if marker != "PATH":
            assert captured[-1][marker] == value
    assert captured[-1]["PATH"].endswith(supervisor_env["PATH"])


@pytest.mark.platforms("posix")
def test_hub_registers_local_successor_for_the_attached_predecessor(tmp_path):
    async def scenario(socket_dir):
        server = DiscordHubServer(Path(socket_dir) / "hub.sock")

        class FakeDiscordConnection:
            async def disconnect(self):
                pass

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[_token_key(config_data["token"])] = FakeDiscordConnection()

        server._attach_adapter = fake_attach
        record_spawn = MagicMock()
        server._successors.record_spawn = record_spawn
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            result = await client._call("register_spawned_gateway", {
                "pid": 5150, "hermes_home": str(tmp_path),
                "predecessor_pid": os.getpid(),
            })
            assert result == {"pid": 5150}
            record_spawn.assert_called_once_with(5150, str(tmp_path), os.getpid())
        finally:
            await client.disconnect()
            await server.stop()

    import os
    import tempfile
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="h-") as socket_dir:
        run(scenario(socket_dir))


def test_hub_rejects_local_successor_registration_from_another_peer(tmp_path):
    server = DiscordHubServer(tmp_path / "unused.sock")
    client = SimpleNamespace(gateway_pid=4242, peer_pid=9999)
    server._successors.record_spawn = MagicMock()

    with pytest.raises(RuntimeError, match="socket peer is not the claimed predecessor"):
        run(server._dispatch(client, "register_spawned_gateway", {
            "pid": 5150, "hermes_home": str(tmp_path), "predecessor_pid": 4242,
        }))
    server._successors.record_spawn.assert_not_called()


def test_hub_marks_successor_with_predecessor_pid(monkeypatch, tmp_path):
    import subprocess
    from gateway.overlap_restart import OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV

    monkeypatch.setattr("gateway.restart.detached_restart_spawn_blocked", lambda: False)
    monkeypatch.setattr(
        "tools.environments.local.served_profile_child_env",
        lambda **kwargs: {"PYTHONPATH": "", "HERMES_GATEWAY_MODE": "inherited"},
    )
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(pid=5150)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    server = DiscordHubServer(tmp_path / "unused.sock")
    result = run(server._spawn_gateway({
        "hermes_home": str(tmp_path), "predecessor_pid": 4242,
    }))

    assert result == {"pid": 5150}
    env = captured["env"]
    assert env[OVERLAP_SUCCESSOR_ENV] == "1"
    assert env[OVERLAP_PREDECESSOR_PID_ENV] == "4242"
    assert "HERMES_GATEWAY_MODE" not in env


def test_marked_successor_boots_pending_without_touching_authority(monkeypatch):
    from gateway.overlap_restart import (
        GATEWAY_CLAIM_SUCCESSOR_PENDING, OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV,
    )
    import gateway.run as gateway_run

    monkeypatch.setenv(OVERLAP_SUCCESSOR_ENV, "1")
    monkeypatch.setenv(OVERLAP_PREDECESSOR_PID_ENV, "4242")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    acquire = MagicMock()
    write = MagicMock()
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", acquire)
    monkeypatch.setattr("gateway.status.write_pid_file", write)

    assert gateway_run._start_gateway_claim_pid_file() == GATEWAY_CLAIM_SUCCESSOR_PENDING
    acquire.assert_not_called()
    write.assert_not_called()


def test_plain_second_gateway_still_refuses_live_pid(monkeypatch):
    from gateway.overlap_restart import OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV
    import gateway.run as gateway_run

    monkeypatch.delenv(OVERLAP_SUCCESSOR_ENV, raising=False)
    monkeypatch.delenv(OVERLAP_PREDECESSOR_PID_ENV, raising=False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    acquire = MagicMock()
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", acquire)

    assert gateway_run._start_gateway_claim_pid_file() is False
    acquire.assert_not_called()


def test_hub_spawn_pins_the_checkout_venv_and_sanitizes_the_child_env(monkeypatch, tmp_path):
    import subprocess
    import gateway.restart as gateway_restart
    from gateway.overlap_restart import OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV

    checkout = tmp_path / "repo"
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!stub\n")
    monkeypatch.setattr(gateway_restart, "detached_restart_spawn_blocked", lambda: False)
    # Deterministic venv resolution regardless of whether this test checkout ships a .venv.
    monkeypatch.setattr(
        gateway_restart, "_replacement_interpreter",
        lambda root: (str(venv_bin / "python"), venv_bin))
    monkeypatch.setattr(
        "tools.environments.local.served_profile_child_env",
        lambda **kwargs: {
            "PYTHONPATH": "/foreign/site-packages", "VIRTUAL_ENV": "/other/venv",
            "PATH": "/other/venv/bin:/usr/bin:/bin", "HERMES_GATEWAY_MODE": "inherited",
        },
    )
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(pid=5150)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    server = DiscordHubServer(tmp_path / "unused.sock")
    result = run(server._spawn_gateway({
        "hermes_home": str(tmp_path), "predecessor_pid": 4242,
    }))

    assert result == {"pid": 5150}
    assert captured["argv"][0] == str(venv_bin / "python")
    env = captured["env"]
    # The cffi-class regression: a foreign VIRTUAL_ENV/PYTHONPATH inherited from the
    # spawning process must not survive into the child env — that mixed site-packages
    # gave one process pure-python cffi 2.0.0 plus a C _cffi_backend 2.1.1.
    assert env["VIRTUAL_ENV"] == str(checkout / ".venv")
    assert env["PATH"].startswith(str(venv_bin) + ":")
    import plugins.platforms.discord.hub as hub_module
    assert env["PYTHONPATH"] == str(Path(hub_module.__file__).resolve().parents[3])
    assert "HERMES_GATEWAY_MODE" not in env
    assert env[OVERLAP_SUCCESSOR_ENV] == "1"
    assert env[OVERLAP_PREDECESSOR_PID_ENV] == "4242"


def test_spawn_replacement_falls_back_to_current_interpreter_without_a_checkout_venv(tmp_path):
    import sys as _sys
    from gateway.restart import spawn_replacement_gateway

    calls = []

    def recording_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=1)

    spawn_replacement_gateway(str(tmp_path), popen=recording_popen)

    argv, kwargs = calls[0]
    assert argv[0] == _sys.executable
    assert "checkout_venv_bin_dir" not in kwargs


def test_spawn_replacement_uses_the_checkout_venv_interpreter(tmp_path):
    from gateway.restart import spawn_replacement_gateway

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text("#!stub\n")

    calls = []

    def recording_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=1)

    spawn_replacement_gateway(str(tmp_path), popen=recording_popen)

    argv, kwargs = calls[0]
    assert argv[0] == str(python)
    assert kwargs["checkout_venv_bin_dir"] == str(venv_bin)


@pytest.mark.asyncio
async def test_deferred_successor_claim_retries_then_becomes_authoritative(monkeypatch):
    from gateway.overlap_restart import (
        GATEWAY_CLAIM_SUCCESSOR_PENDING, claim_when_predecessor_exits,
    )

    alive = iter((True, False))
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: next(alive))
    claims = iter((GATEWAY_CLAIM_SUCCESSOR_PENDING, True))
    monkeypatch.setattr("gateway.run._start_gateway_claim_pid_file", lambda *_: next(claims))
    runner = SimpleNamespace(
        _overlap_successor_pending=True,
        _shutdown_event=asyncio.Event(),
        stop=AsyncMock(),
    )
    on_authoritative = AsyncMock()

    assert await claim_when_predecessor_exits(
        runner, 4242, on_authoritative, poll_seconds=0,
    )
    assert runner._overlap_successor_pending is False
    on_authoritative.assert_awaited_once_with()
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_successor_exits_cleanly_when_another_gateway_wins(monkeypatch, caplog):
    from gateway.overlap_restart import claim_when_predecessor_exits

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    monkeypatch.setattr("gateway.run._start_gateway_claim_pid_file", lambda *_: False)
    adapter = SimpleNamespace(
        supports_overlap_restart=True,
        disconnect=AsyncMock(),
    )
    matrix_adapter = SimpleNamespace(
        supports_overlap_restart=True,
        disconnect=AsyncMock(),
    )
    runner = SimpleNamespace(
        _overlap_successor_pending=True,
        _shutdown_event=asyncio.Event(),
        adapters={Platform.DISCORD: adapter, Platform.MATRIX: matrix_adapter},
        stop=AsyncMock(),
    )

    with caplog.at_level(logging.WARNING):
        assert not await claim_when_predecessor_exits(
            runner, 4242, AsyncMock(), poll_seconds=0,
        )
    assert runner._exit_cleanly is True
    assert "lost the gateway lock/PID claim" in runner._exit_reason
    adapter.disconnect.assert_awaited_once_with()
    matrix_adapter.disconnect.assert_awaited_once_with()
    assert any(
        record.levelno == logging.WARNING
        and "lost the gateway lock/PID claim" in record.getMessage()
        for record in caplog.records
    )
    runner.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_deferred_claim_error_does_not_leave_a_pending_zombie(monkeypatch):
    from gateway.overlap_restart import claim_when_predecessor_exits

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)

    def fail_claim(_pid):
        raise OSError("lock probe failed")

    monkeypatch.setattr("gateway.run._start_gateway_claim_pid_file", fail_claim)
    runner = SimpleNamespace(
        _overlap_successor_pending=True,
        _shutdown_event=asyncio.Event(),
        stop=AsyncMock(),
    )

    assert not await claim_when_predecessor_exits(runner, 4242, AsyncMock(), poll_seconds=0)
    assert runner._exit_with_failure is True
    runner.stop.assert_awaited_once_with()


def test_successor_boot_does_not_count_toward_restart_loop_breaker(monkeypatch):
    from contextlib import nullcontext
    from gateway.overlap_restart import OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV
    from gateway import restart_loop_guard
    import gateway.run as gateway_run

    monkeypatch.setenv(OVERLAP_SUCCESSOR_ENV, "1")
    monkeypatch.setenv(OVERLAP_PREDECESSOR_PID_ENV, "4242")
    entry = SimpleNamespace(
        resume_pending=True, suspended=False, origin=object(), resume_reason="restart_interrupted",
    )
    runner = SimpleNamespace(
        _overlap_successor_pending=True,
        session_store=SimpleNamespace(
            _lock=nullcontext(), _ensure_loaded_locked=lambda: None, _entries={"session": entry},
        ),
        _AUTO_RESUME_REASONS={"restart_interrupted"},
        _restart_loop_guard_config=lambda: (3, 600, 3600),
    )
    check = MagicMock()
    monkeypatch.setattr(restart_loop_guard, "check_and_record", check)

    assert gateway_run.GatewayRunner._resume_pending_candidates(runner) == [entry]
    check.assert_not_called()


@pytest.mark.asyncio
async def test_successor_attach_window_covers_a_five_minute_cold_boot(monkeypatch):
    import plugins.platforms.discord.hub_client as hub_client_module

    now = [0.0]

    async def advance_clock(_seconds):
        now[0] += 1.0

    monkeypatch.setattr(hub_client_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(hub_client_module.asyncio, "sleep", advance_clock)
    client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    client._generation_id = "predecessor"

    async def ping(_method):
        return {"current_generation": "successor" if now[0] >= 300 else "predecessor"}

    client._call = ping
    await client.wait_for_overlap_successor()
    assert client._overlap_successor_ready is True


def test_only_hub_capable_adapters_start_while_successor_is_pending():
    from gateway.overlap_restart import defer_adapter_until_authoritative

    runner = SimpleNamespace(_overlap_successor_pending=True)
    assert not defer_adapter_until_authoritative(
        runner, SimpleNamespace(supports_overlap_restart=True),
    )
    assert defer_adapter_until_authoritative(runner, SimpleNamespace())
    runner._overlap_successor_pending = False
    assert not defer_adapter_until_authoritative(runner, SimpleNamespace())


@pytest.mark.asyncio
async def test_claim_starts_deferred_primary_and_secondary_adapters():
    from gateway.overlap_restart import start_deferred_adapters

    pending = [(SimpleNamespace(value="api_server"), object(), object())]
    runner = SimpleNamespace(
        _overlap_deferred_primary_adapters=pending,
        _overlap_deferred_secondary_adapters=True,
        _start_connect_pending=AsyncMock(return_value=[("raw-result")]),
        _start_aggregate_connect_results=AsyncMock(),
        _start_secondary_profile_adapters=AsyncMock(return_value=1),
    )

    await start_deferred_adapters(runner)
    runner._start_connect_pending.assert_awaited_once_with(pending)
    runner._start_aggregate_connect_results.assert_awaited_once()
    runner._start_secondary_profile_adapters.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_start_gateway_attaches_before_deferred_authority_claim(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig
    from gateway.overlap_restart import (
        GATEWAY_CLAIM_SUCCESSOR_PENDING, OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV,
    )
    import gateway.run as gateway_run

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(OVERLAP_SUCCESSOR_ENV, "1")
    monkeypatch.setenv(OVERLAP_PREDECESSOR_PID_ENV, "4242")
    monkeypatch.setattr("hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    monkeypatch.setattr("gateway.code_skew.record_boot_fingerprint", lambda: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_configure_logging", lambda verbosity: None)
    monkeypatch.setattr(gateway_run, "_enable_multiplex_log_routing", lambda config: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_claim_pid_file", lambda: GATEWAY_CLAIM_SUCCESSOR_PENDING)
    monkeypatch.setattr(gateway_run, "_discover_gateway_mcp_tools", AsyncMock())
    monkeypatch.setattr(gateway_run, "_ensure_windows_gateway_venv_imports", lambda: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("gateway.shutdown_flush.recover_pending_to_db", lambda **kwargs: 0)
    replace = AsyncMock()
    monkeypatch.setattr(gateway_run, "_start_gateway_replace_existing_instance", replace)
    events = []

    class FakeRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self.session_store = SimpleNamespace(resolve_session_id_for_key=lambda key: key)
            self.should_exit_cleanly = False
            self.exit_reason = None
            self.exit_code = None
            self._running = True
            self._shutdown_event = asyncio.Event()

        async def start(self):
            import os
            assert self._overlap_successor_pending is True
            assert self._launched_as_overlap_successor is True
            assert OVERLAP_SUCCESSOR_ENV not in os.environ
            assert OVERLAP_PREDECESSOR_PID_ENV not in os.environ
            events.append("hub-adapter-start")
            return True

        async def wait_for_shutdown(self):
            await self._shutdown_event.wait()

        def _start_systemd_watchdog(self):
            events.append("systemd-watchdog")

        async def _send_restart_notification(self):
            events.append("restart-notification")

    control = SimpleNamespace()
    cron_provider = SimpleNamespace()

    async def start_control(runner):
        events.append("control-socket")
        return control

    def start_cron(runner):
        events.append("cron")
        return threading_event, cron_provider, None, None

    import threading
    threading_event = threading.Event()
    deferred = AsyncMock(side_effect=lambda: events.append("deferred-adapters"))
    monkeypatch.setattr(gateway_run, "GatewayRunner", FakeRunner)
    monkeypatch.setattr(gateway_run, "_start_gateway_start_control_socket", start_control)
    monkeypatch.setattr(gateway_run, "_start_gateway_start_cron_and_housekeeping", start_cron)
    monkeypatch.setattr("gateway.overlap_restart.start_deferred_adapters", deferred)

    async def fake_claim(runner, predecessor_pid, on_authoritative, **kwargs):
        assert predecessor_pid == 4242
        assert "control-socket" not in events and "cron" not in events
        events.append("predecessor-exited")
        await on_authoritative()
        runner._shutdown_event.set()
        return True

    monkeypatch.setattr("gateway.overlap_restart.claim_when_predecessor_exits", fake_claim)
    tail = {}

    async def fake_tail(*args):
        tail["control"] = args[1]
        tail["cron_provider"] = args[3]
        args[6].set()
        args[7].join(timeout=1)
        return True

    monkeypatch.setattr(gateway_run, "_start_gateway_shutdown_tail", fake_tail)
    monkeypatch.setattr("gateway.lifecycle_ledger.record_startup", lambda: events.append("lifecycle"))

    assert await gateway_run.start_gateway(config=GatewayConfig(), verbosity=None)
    replace.assert_not_awaited()
    assert events == [
        "hub-adapter-start", "predecessor-exited", "control-socket", "lifecycle",
        "deferred-adapters", "restart-notification", "cron", "systemd-watchdog",
    ]
    deferred.assert_awaited_once()
    assert tail == {"control": control, "cron_provider": cron_provider}


@pytest.mark.asyncio
async def test_overlap_successor_starts_before_predecessor_finishes_owned_work():
    from tests.gateway.restart_test_helpers import RestartTestAdapter, make_restart_runner

    class OverlapAdapter(RestartTestAdapter):
        supports_overlap_restart = True

    adapter = OverlapAdapter(Platform.DISCORD)
    adapter.spawn_overlap_successor = AsyncMock(return_value=5150)
    runner, _ = make_restart_runner(adapter, platform=Platform.DISCORD)
    secondary = OverlapAdapter(Platform.DISCORD)
    secondary.spawn_overlap_successor = AsyncMock(return_value=5151)
    secondary.mark_overlap_successor_ready = MagicMock()
    runner._profile_adapters = {"secondary": {Platform.DISCORD: secondary}}
    runner.stop = AsyncMock()
    runner._running_agents["owned-session"] = MagicMock()

    assert runner.request_restart(detached=True, via_service=False)
    for _ in range(100):
        if adapter.spawn_overlap_successor.await_count:
            break
        await asyncio.sleep(0.01)
    import os
    adapter.spawn_overlap_successor.assert_awaited_once_with(
        via_service=False, predecessor_pid=os.getpid(),
    )
    secondary.spawn_overlap_successor.assert_not_awaited()
    secondary.mark_overlap_successor_ready.assert_called_once_with()
    runner.stop.assert_not_awaited()
    assert "owned-session" in runner._running_agents

    del runner._running_agents["owned-session"]
    await asyncio.wait_for(runner._restart_task, timeout=5)
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=True, service_restart=False
    )


def test_discord_hub_systemd_unit_is_independent_and_gateway_does_not_start_third_generation():
    import configparser
    from hermes_cli.gateway import (
        generate_discord_hub_systemd_unit,
        generate_systemd_unit,
        get_discord_hub_service_name,
        get_service_name,
    )

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(generate_discord_hub_systemd_unit())
    assert parser["Service"]["ExecStart"].endswith("-m plugins.platforms.discord.hub")
    assert parser["Service"]["Type"] == "simple"
    assert parser["Service"]["Restart"] == "always"
    assert parser["Service"]["KillMode"] == "process"
    assert get_discord_hub_service_name() != get_service_name()

    gateway = generate_systemd_unit()
    assert "SuccessExitStatus=75 76" in gateway
    assert "RestartPreventExitStatus=78 76" in gateway


def test_hub_startup_and_unit_install_respect_test_isolation(monkeypatch, tmp_path):
    from hermes_cli import gateway

    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(tmp_path / "isolated"))
    unit_path = tmp_path / "hermes-discord-hub.service"
    monkeypatch.setattr(gateway, "get_discord_hub_systemd_unit_path", lambda system=False: unit_path)
    systemctl = MagicMock()
    monkeypatch.setattr(gateway, "_run_systemctl", systemctl)
    gateway._ensure_discord_hub_systemd_unit(
        system=False, run_as_user=None, enable_on_startup=True)
    assert not unit_path.exists()
    systemctl.assert_not_called()

    client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    popen = MagicMock()
    monkeypatch.setattr("plugins.platforms.discord.hub_client.subprocess.Popen", popen)
    assert not run(client._start_hub())
    popen.assert_not_called()


def test_hub_client_exposes_the_gateway_discord_adapter_surface():
    from plugins.platforms.discord.adapter import DiscordAdapter

    async_methods = (
        "send_clarify", "send_tool_stage_embed", "edit_tool_stage_embed",
        "send_slash_confirm", "send_update_prompt", "refresh_skill_group",
        "cancel_background_tasks", "send_model_picker", "send_choice_picker",
    )
    sync_methods = (
        "get_bot_display_name", "resolved_allowlist_user_ids",
        "prefers_fresh_final_streaming", "format_tool_preview",
        "claim_startup_resume_inbound",
    )
    for name in async_methods:
        assert asyncio.iscoroutinefunction(getattr(DiscordAdapter, name))
        assert asyncio.iscoroutinefunction(getattr(DiscordHubClientAdapter, name))
    for name in sync_methods:
        assert callable(getattr(DiscordAdapter, name))
        assert not asyncio.iscoroutinefunction(getattr(DiscordHubClientAdapter, name))


@pytest.mark.platforms("posix")
def test_picker_callbacks_round_trip_to_the_attached_gateway(tmp_path):
    async def scenario():
        server = DiscordHubServer(tmp_path / "discord-hub.sock")

        class FakeDiscordConnection:
            def __init__(self):
                self.callbacks = {}

            async def disconnect(self):
                pass

            async def send_model_picker(self, chat_id, providers, current_model, current_provider,
                                        session_key, on_model_selected, metadata=None):
                self.callbacks["model"] = on_model_selected
                return SendResult(success=True, message_id="model-message")

            async def send_choice_picker(self, chat_id, title, choices, session_key,
                                         on_choice_selected, metadata=None):
                self.callbacks["choice"] = on_choice_selected
                return SendResult(success=True, message_id="choice-message")

        connection = FakeDiscordConnection()

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[_token_key(config_data["token"])] = connection

        server._attach_adapter = fake_attach
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            model_calls = []
            choice_calls = []

            async def model_selected(chat_id, model_id, provider):
                model_calls.append((chat_id, model_id, provider))
                return "model changed"

            async def choice_selected(chat_id, value):
                choice_calls.append((chat_id, value))
                return "choice changed"

            assert (await client.send_model_picker(
                "42", [{"slug": "provider", "models": ["model"]}], "old", "old-provider",
                "session", model_selected,
            )).success
            assert (await client.send_choice_picker(
                "42", "Reasoning", [{"value": "high", "label": "High"}], "session", choice_selected,
            )).success

            assert await connection.callbacks["model"]("42", "model", "provider") == "model changed"
            assert await connection.callbacks["choice"]("42", "high") == "choice changed"
            assert model_calls == [("42", "model", "provider")]
            assert choice_calls == [("42", "high")]
        finally:
            await client.disconnect()
            await server.stop()

    run(scenario())


def test_send_multiple_images_forwards_reply_to():
    client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    client._rpc = AsyncMock(return_value={"success": True, "message_id": "image-message"})

    result = run(client.send_multiple_images(
        "42", ["/tmp/image.png"], metadata={"thread_id": "43"},
        reply_to="root-message",
    ))

    assert result.success
    client._rpc.assert_awaited_once_with(
        "send_multiple_images", "42", ["/tmp/image.png"],
        metadata={"thread_id": "43"}, reply_to="root-message",
    )


@pytest.mark.platforms("posix")
def test_sync_queries_and_voice_channel_handles_use_the_hub(tmp_path):
    async def scenario():
        from agent.display import ToolPreview

        server = DiscordHubServer(tmp_path / "discord-hub.sock")
        voice_channel = SimpleNamespace(id=99, name="stage", guild=SimpleNamespace(id=77))

        class FakeDiscordConnection:
            def __init__(self):
                self._client = SimpleNamespace(get_channel=lambda channel_id: (
                    voice_channel if channel_id == voice_channel.id else None
                ))
                self.claims = []
                self.joins = []

            async def disconnect(self):
                pass

            def get_bot_display_name(self, *, chat_id=None, guild_id=None):
                return "Hub nickname"

            def resolved_allowlist_user_ids(self):
                return {"7", "8"}

            def prefers_fresh_final_streaming(self, content, metadata=None):
                return True

            def format_tool_preview(self, preview):
                assert isinstance(preview, ToolPreview)
                return f"linked:{preview.text}"

            def claim_startup_resume_inbound(self, message_id, source):
                self.claims.append((message_id, source.chat_id))

            def voice_mixer_active(self, guild_id):
                return guild_id == 77

            def is_in_voice_channel(self, guild_id):
                return guild_id == 77

            def get_voice_channel_info(self, guild_id):
                return {"channel_name": "stage", "member_count": 1}

            def get_voice_channel_context(self, guild_id):
                return "[Voice channel: #stage — 1 participant(s)]"

            async def get_user_voice_channel(self, guild_id, user_id):
                return voice_channel

            async def join_voice_channel(self, channel, *, text_channel_id=None, source=None):
                self.joins.append((channel, text_channel_id, source))
                return True

        connection = FakeDiscordConnection()

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
            server._adapters[_token_key(config_data["token"])] = connection

        server._attach_adapter = fake_attach
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        source = SessionSource(platform=Platform.DISCORD, chat_id="42", chat_type="channel", user_id="7")
        try:
            assert await client.connect()

            def check_sync_surface():
                assert client.get_bot_display_name(chat_id="42", guild_id="77") == "Hub nickname"
                assert client.resolved_allowlist_user_ids() == {"7", "8"}
                assert client.prefers_fresh_final_streaming("answer")
                assert client.format_tool_preview(ToolPreview(text="docs", url="https://example.com")) == "linked:docs"
                client.claim_startup_resume_inbound("message-1", source)
                assert client.voice_mixer_active(77)
                assert client.is_in_voice_channel(77)
                assert client.get_voice_channel_info(77)["channel_name"] == "stage"
                assert "stage" in client.get_voice_channel_context(77)

            # The test hub shares this loop; production hub queries block only the caller,
            # never the separate hub process's event loop.
            await asyncio.to_thread(check_sync_surface)
            assert connection.claims == [("message-1", "42")]

            handle = await client.get_user_voice_channel(77, "7")
            assert handle.id == 99 and handle.name == "stage" and handle.guild_id == 77
            assert await client.join_voice_channel(handle, text_channel_id=42, source=source.to_dict())
            assert connection.joins == [(voice_channel, 42, source.to_dict())]
        finally:
            await client.disconnect()
            await server.stop()

    run(scenario())


class TestHubGateCredentialHandoff:
    """The owning profile's judge credential travels in the attach payload.

    Regression (#508): the hub-side adapter runs with no profile secret scope, so
    its own scoped capture returned None and the enforce-mode response/reaction
    gates failed closed with error=missing_credential for every attached profile.
    """

    def test_client_resolves_and_sends_the_scoped_credential(self, monkeypatch):

        monkeypatch.setenv("LITELLM_API_KEY", "sk-profile-key")
        captured = {}

        async def fake_call(method, params=None):
            captured["method"] = method
            captured["params"] = params
            return {"pid": 1, "token_key": "k"}

        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._open_socket = AsyncMock()
        client._call = fake_call
        assert run(client.connect())
        assert captured["method"] == "attach"
        assert captured["params"]["gate_credential"] == "sk-profile-key"

    def test_client_sends_none_without_a_key(self, monkeypatch):

        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        captured = {}

        async def fake_call(method, params=None):
            captured["params"] = params
            return {"pid": 1, "token_key": "k"}

        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._open_socket = AsyncMock()
        client._call = fake_call
        assert run(client.connect())
        assert captured["params"]["gate_credential"] is None

    def test_attach_adapter_pins_the_credential_before_connect(self, monkeypatch):
        import plugins.platforms.discord.adapter as adapter_module

        pinned = []

        class StubAdapter:
            def __init__(self, config):
                self._hub_gate_credential = None
                self._owner_profile = None

            async def connect(self):
                # connect() must already see the pinned credential.
                pinned.append(self._hub_gate_credential)
                return True

        monkeypatch.setattr(adapter_module, "DiscordAdapter", StubAdapter)
        server = DiscordHubServer(None)
        config = {"token": "fake-token"}

        run(server._attach_adapter("gen-1", config, "profile-a", [], "sk-hub-key"))
        assert pinned == ["sk-hub-key"]
        assert server._adapters[_token_key("fake-token")]._hub_gate_credential == "sk-hub-key"

        # No key for the profile pins "" (fail closed), never an env fallback.
        server2 = DiscordHubServer(None)
        run(server2._attach_adapter("gen-2", config, "profile-b", [], None))
        assert pinned == ["sk-hub-key", ""]


@pytest.mark.platforms("posix")
def test_unix_attach_round_trips_the_gate_credential(tmp_path, monkeypatch):
    """End to end over the socket: scoped key → attach payload → hub-side adapter."""
    import plugins.platforms.discord.adapter as adapter_module

    monkeypatch.setenv("LITELLM_API_KEY", "sk-roundtrip-key")

    class StubAdapter:
        def __init__(self, config):
            self._hub_gate_credential = None
            self._owner_profile = None

        async def connect(self):
            return True

        async def disconnect(self):
            pass

    async def scenario():
        server = DiscordHubServer(tmp_path / "hub.sock")
        monkeypatch.setattr(adapter_module, "DiscordAdapter", StubAdapter)
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            adapter = server._adapters[_token_key("fake-token")]
            assert adapter._hub_gate_credential == "sk-roundtrip-key"
        finally:
            await client.disconnect()
            await server.stop()

    run(scenario())


class TestHubAdapterCredentialCapture:
    """The hub pin wins over both the profile scope and the hub process's env."""

    def setup_method(self):
        pytest.importorskip("discord")
        from plugins.platforms.discord.adapter import DiscordAdapter

        config = PlatformConfig(enabled=True, token="x")
        self.adapter = DiscordAdapter(config)

    def test_hub_pin_is_used_verbatim(self, monkeypatch):
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        self.adapter._hub_gate_credential = "sk-hub-key"
        self.adapter._capture_response_gate_credential()
        assert self.adapter._response_gate_credential == "sk-hub-key"

    def test_hub_pin_empty_fails_closed_even_with_env_key(self, monkeypatch):
        # The hub process env may hold the default profile's first-writer-wins key;
        # a profile with no key must stay credential-less, not borrow it.
        monkeypatch.setenv("LITELLM_API_KEY", "sk-default-profile-leak")
        self.adapter._hub_gate_credential = ""
        self.adapter._capture_response_gate_credential()
        assert self.adapter._response_gate_credential is None

    def test_in_process_adapter_reads_the_scope(self, monkeypatch):
        monkeypatch.setenv("LITELLM_API_KEY", "sk-scoped-key")
        self.adapter._capture_response_gate_credential()
        assert self.adapter._response_gate_credential == "sk-scoped-key"

    def test_response_gate_init_builds_client_with_credential(self, caplog):
        from gateway.config import ResponseGateConfig

        self.adapter.config.response_gate = ResponseGateConfig.from_dict(
            {"enabled": True, "channels": ["555"], "mode": "enforce"})
        self.adapter._response_gate_credential = "sk-hub-key"
        self.adapter._response_gate_init()
        assert self.adapter._response_gate is not None
        assert self.adapter._response_gate.client is not None

    def test_response_gate_init_stays_silent_client_without_credential(self, caplog):
        from gateway.config import ResponseGateConfig

        self.adapter.config.response_gate = ResponseGateConfig.from_dict(
            {"enabled": True, "channels": ["555"], "mode": "enforce"})
        self.adapter._response_gate_credential = None
        self.adapter._response_gate_init()
        assert self.adapter._response_gate is not None
        assert self.adapter._response_gate.client is None
        assert any(
            "no LITELLM_API_KEY" in record.message and record.levelno == logging.ERROR
            for record in caplog.records
        )
