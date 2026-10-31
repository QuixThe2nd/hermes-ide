"""Focused ownership tests for the multi-generation Discord hub."""

import asyncio
import logging
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

        async def fake_attach(generation_id, config_data, profile, profile_routes, gate_credential=None):
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


def test_hub_client_exposes_the_gateway_discord_adapter_surface():
    from plugins.platforms.discord.adapter import DiscordAdapter

    async_methods = (
        "send_clarify", "send_tool_stage_embed", "edit_tool_stage_embed",
        "send_slash_confirm", "send_update_prompt", "refresh_skill_group",
        "cancel_background_tasks", "play_tts", "play_ack_in_voice",
        "join_voice_channel", "leave_voice_channel", "play_in_voice_channel",
        "get_user_voice_channel", "send_model_picker", "send_choice_picker",
    )
    sync_methods = (
        "get_bot_display_name", "resolved_allowlist_user_ids",
        "prefers_fresh_final_streaming", "format_tool_preview",
        "claim_startup_resume_inbound", "voice_mixer_active",
        "is_in_voice_channel", "get_voice_channel_info", "get_voice_channel_context",
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
        human_delay=0.25, reply_to="root-message",
    ))

    assert result.success
    client._rpc.assert_awaited_once_with(
        "send_multiple_images", "42", ["/tmp/image.png"],
        metadata={"thread_id": "43"}, human_delay=0.25, reply_to="root-message",
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

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-profile-key")
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

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-roundtrip-key")

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
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        self.adapter._hub_gate_credential = "sk-hub-key"
        self.adapter._capture_response_gate_credential()
        assert self.adapter._response_gate_credential == "sk-hub-key"

    def test_hub_pin_empty_fails_closed_even_with_env_key(self, monkeypatch):
        # The hub process env may hold the default profile's first-writer-wins key;
        # a profile with no key must stay credential-less, not borrow it.
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-default-profile-leak")
        self.adapter._hub_gate_credential = ""
        self.adapter._capture_response_gate_credential()
        assert self.adapter._response_gate_credential is None

    def test_in_process_adapter_reads_the_scope(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-scoped-key")
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
            "no OPENROUTER_API_KEY" in record.message and record.levelno == logging.ERROR
            for record in caplog.records
        )
