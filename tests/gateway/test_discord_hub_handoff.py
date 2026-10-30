"""Focused ownership tests for the multi-generation Discord hub."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
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

        async def fake_attach(generation_id, config_data, profile, profile_routes):
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

        async def fake_attach(generation_id, config_data, profile, profile_routes):
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


@pytest.mark.platforms("posix")
def test_restart_control_and_pending_title_stay_on_prompt_owner(tmp_path):
    async def scenario():
        from plugins.platforms.discord.adapter import RestartPendingThreadTitle

        server = DiscordHubServer(tmp_path / "discord-hub.sock")

        class FakeDiscordConnection:
            def __init__(self):
                self.title_calls = []

            async def disconnect(self):
                pass

            async def send_restart_wind_down_offer(self, **kwargs):
                return "offer-1"

            async def capture_restart_pending_thread_title(self, thread_id):
                return RestartPendingThreadTitle(thread_id=42, original_name="original")

            async def begin_restart_pending_thread_title(self, restore):
                self.title_calls.append(("begin", restore))

            async def end_restart_pending_thread_title(self, restore):
                self.title_calls.append(("end", restore))

        connection = FakeDiscordConnection()

        async def fake_attach(generation_id, config_data, profile, profile_routes):
            server._adapters[_token_key(config_data["token"])] = connection

        server._attach_adapter = fake_attach
        await server.start()
        clients = [DiscordHubClientAdapter(PlatformConfig(token="fake-token")) for _ in range(2)]
        try:
            for client in clients:
                client._socket_path = server.socket_path
            assert await clients[0].connect()
            assert await clients[0].send_restart_wind_down_offer(
                channel_id="42", requester_user_id="7", generation=1, nonce="nonce"
            ) == "offer-1"

            # Attaching the successor must not steal the predecessor's native prompt.
            assert await clients[1].connect()
            clients[0].gateway_runner = SimpleNamespace(
                accept_restart_wind_down_opt_in=AsyncMock(return_value={"accepted": True})
            )
            clients[1].gateway_runner = SimpleNamespace(
                accept_restart_wind_down_opt_in=AsyncMock(return_value={"accepted": False})
            )
            params = {
                "message_id": "offer-1", "channel_id": "42", "requester_user_id": "7",
                "emoji": "⏸️", "generation": 1, "nonce": "nonce",
            }
            assert await server.request_control(_token_key("fake-token"), "offer-1", params) == {
                "accepted": True
            }
            clients[0].gateway_runner.accept_restart_wind_down_opt_in.assert_awaited_once_with(**params)
            clients[1].gateway_runner.accept_restart_wind_down_opt_in.assert_not_awaited()

            captured = await clients[0].capture_restart_pending_thread_title("42")
            assert captured == RestartPendingThreadTitle(thread_id=42, original_name="original")
            await clients[0].begin_restart_pending_thread_title(captured)
            await clients[0].end_restart_pending_thread_title(captured)
            assert connection.title_calls == [("begin", captured), ("end", captured)]
        finally:
            await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)
            await server.stop()

    run(scenario())


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
    adapter.spawn_overlap_successor.assert_awaited_once_with(via_service=False)
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
