"""Hub-created Discord adapters keep thread participation in the owning profile's home.

Regression: every hub-hosted adapter resolved ``discord_threads.json`` against the
hub process's own home, so one bot's thread participation admitted unmentioned
messages for every other hub-hosted bot. The owning gateway's home now travels in
the attach payload; a legacy attach without it keeps the hub-local behavior.
"""

import asyncio
import json

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.helpers import ThreadParticipationTracker
from plugins.platforms.discord.hub import DiscordHubServer, _token_key
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter


def run(coro):
    return asyncio.run(coro)


class StubAdapter:
    """Discord transport stand-in; the tracker under test is the real one."""

    def __init__(self, config):
        self._owner_profile = None
        self._hub_gate_credential = None

    async def connect(self):
        return True

    async def disconnect(self):
        return True

    def _capture_response_gate_credential(self):
        pass

    def _reaction_gate_start_reload_watcher(self):
        pass


@pytest.fixture
def stub_adapter_class(monkeypatch):
    import plugins.platforms.discord.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "DiscordAdapter", StubAdapter)
    return StubAdapter


def _attach(server, generation, profile, **kwargs):
    return server._attach_adapter(generation, {"token": "fake-token"}, profile, [], None, **kwargs)


@pytest.mark.platforms("posix")
def test_hub_adapters_keep_thread_state_in_the_owning_profile_home(
    tmp_path, monkeypatch, short_af_unix_path, stub_adapter_class,
):
    """End to end over the socket: two profiles, two bots, two thread files."""
    from gateway.run import _profile_runtime_scope

    hub_home = tmp_path / "hub"
    home_a = tmp_path / "profiles" / "alpha"
    home_b = tmp_path / "profiles" / "beta"
    for home in (hub_home, home_a, home_b):
        home.mkdir(parents=True)
    # The hub process's own home: nothing per-profile may land here.
    monkeypatch.setenv("HERMES_HOME", str(hub_home))

    async def scenario():
        server = DiscordHubServer(short_af_unix_path)
        await server.start()
        clients = []

        async def attach(profile, home, token):
            client = DiscordHubClientAdapter(PlatformConfig(enabled=True, token=token))
            client._socket_path = server.socket_path
            client._owner_profile = profile
            clients.append(client)
            with _profile_runtime_scope(home, prepared_secret_scope={}):
                assert await client.connect()

        try:
            await attach("alpha", home_a, "token-a")
            await attach("beta", home_b, "token-b")
            adapter_a = server._adapters[_token_key("token-a")]
            adapter_b = server._adapters[_token_key("token-b")]
            assert adapter_a._threads._state_path() == home_a / "discord_threads.json"
            assert adapter_b._threads._state_path() == home_b / "discord_threads.json"

            adapter_a._threads.mark("thread-one")
            adapter_b._threads.mark("thread-two")
            assert "thread-one" in adapter_a._threads
            assert "thread-one" not in adapter_b._threads
            assert "thread-two" in adapter_b._threads
            assert "thread-two" not in adapter_a._threads
            assert json.loads(
                (home_a / "discord_threads.json").read_text(encoding="utf-8")) == ["thread-one"]
            assert json.loads(
                (home_b / "discord_threads.json").read_text(encoding="utf-8")) == ["thread-two"]
            assert not (hub_home / "discord_threads.json").exists()
        finally:
            for client in clients:
                await client.disconnect()
            await server.stop()

    run(scenario())


def test_cached_adapter_rebind_moves_thread_state_to_the_new_owner_home(
    tmp_path, monkeypatch, stub_adapter_class,
):
    """A named profile adopting a shared token rebinds the tracker to its own home."""
    hub_home = tmp_path / "hub"
    default_home = tmp_path / "default"
    named_home = tmp_path / "profiles" / "named"
    sibling_home = tmp_path / "profiles" / "sibling"
    for home in (hub_home, default_home, named_home, sibling_home):
        home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hub_home))

    async def scenario():
        server = DiscordHubServer(None)
        await _attach(server, "gen-1", "default", hermes_home=str(default_home))
        adapter = server._adapters[_token_key("fake-token")]
        assert adapter._threads._state_path() == default_home / "discord_threads.json"
        adapter._threads.mark("shared-thread")

        await _attach(server, "gen-2", "named", hermes_home=str(named_home))
        assert adapter._threads._state_path() == named_home / "discord_threads.json"
        # The new owner's tracker reloads from its own file; the previous
        # owner's in-memory set must not be carried across.
        assert "shared-thread" not in adapter._threads
        adapter._threads.mark("named-thread")
        assert json.loads(
            (named_home / "discord_threads.json").read_text(encoding="utf-8")) == ["named-thread"]

        # A sibling reconnect cannot steal the named owner's tracker.
        await _attach(server, "gen-3", "sibling", hermes_home=str(sibling_home))
        assert adapter._threads._state_path() == named_home / "discord_threads.json"
        assert not (sibling_home / "discord_threads.json").exists()

    run(scenario())


def test_legacy_attach_without_home_keeps_hub_local_thread_state(
    tmp_path, monkeypatch, stub_adapter_class,
):
    """An old gateway sends no home: the hub must not invent one."""
    hub_home = tmp_path / "hub"
    hub_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hub_home))

    async def scenario():
        server = DiscordHubServer(None)
        await _attach(server, "gen-1", "default")
        adapter = server._adapters[_token_key("fake-token")]
        # No home was supplied, so the hub leaves the adapter's own
        # (hub-local, get_hermes_home()-resolved) tracker untouched.
        assert not hasattr(adapter, "_threads")

    run(scenario())


def test_attach_rejects_a_blank_home(tmp_path, stub_adapter_class):
    server = DiscordHubServer(None)
    with pytest.raises(ValueError, match="hermes_home"):
        run(_attach(server, "gen-1", "default", hermes_home="   "))


def test_tracker_default_still_follows_get_hermes_home(tmp_path, monkeypatch):
    """In-process construction with no pinned home (plugins, tests) is unchanged."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tracker = ThreadParticipationTracker("discord")
    assert tracker._state_path() == tmp_path / "discord_threads.json"
