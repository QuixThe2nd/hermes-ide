"""Matrix hub attach must resolve secrets like MatrixAdapter (scoped + default-profile fallback)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run_drain_queue import serialize_drain_event
from gateway.session import SessionSource
from plugins.platforms.discord.hub import DiscordSessionRouter

from agent import secret_scope as ss
from hermes_constants import (
    get_process_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from plugins.platforms.matrix.hub_client import MatrixHubClientAdapter


@pytest.fixture
def multiplex_no_scope():
    ss.set_multiplex_active(True)
    assert ss.current_secret_scope() is None
    yield
    ss.set_multiplex_active(False)


def test_attach_secret_scope_reads_default_profile_from_os_environ(multiplex_no_scope, monkeypatch):
    """Live outage: bare get_secret raises when multiplex is on and no scope is installed."""
    homeserver = "https://systemd-injected.matrix.example"
    token = "systemd-injected-matrix-token"
    monkeypatch.setenv("MATRIX_HOMESERVER", homeserver)
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", token)
    # Isolated HERMES_HOME (autouse) has no .env covering Matrix keys.
    from hermes_constants import get_hermes_home

    assert get_hermes_home().resolve() == get_process_hermes_home().resolve()

    mapping = MatrixHubClientAdapter._attach_secret_scope()

    assert mapping["MATRIX_HOMESERVER"] == homeserver
    assert mapping["MATRIX_ACCESS_TOKEN"] == token


def test_attach_secret_scope_reads_default_profile_from_env_only(
    multiplex_no_scope, monkeypatch
):
    """Gateway has HERMES_HOME but Matrix keys only in profile .env, not os.environ."""
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)

    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    assert home.resolve() == get_process_hermes_home().resolve()

    homeserver = "https://env-file-only.matrix.example"
    token = "env-file-only-matrix-token"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        f"MATRIX_HOMESERVER={homeserver}\nMATRIX_ACCESS_TOKEN={token}\n",
        encoding="utf-8",
    )
    ss.invalidate_env_file_cache(home / ".env")

    mapping = MatrixHubClientAdapter._attach_secret_scope()

    assert mapping["MATRIX_HOMESERVER"] == homeserver
    assert mapping["MATRIX_ACCESS_TOKEN"] == token


def _write_profile_env(home, *, homeserver: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(f"MATRIX_HOMESERVER={homeserver}\n", encoding="utf-8")
    ss.invalidate_env_file_cache(home / ".env")


def test_attach_secret_scope_isolates_secondary_profiles(multiplex_no_scope, tmp_path, monkeypatch):
    polluted = "https://wrong-profile-leak.matrix.example"
    monkeypatch.setenv("MATRIX_HOMESERVER", polluted)

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    hs_a = "https://profile-a.matrix.example"
    hs_b = "https://profile-b.matrix.example"
    _write_profile_env(home_a, homeserver=hs_a)
    _write_profile_env(home_b, homeserver=hs_b)

    def attach_for(home):
        token = set_hermes_home_override(home)
        try:
            ss.invalidate_env_file_cache(home / ".env")
            return MatrixHubClientAdapter._attach_secret_scope()
        finally:
            reset_hermes_home_override(token)

    m_a1 = attach_for(home_a)
    assert m_a1["MATRIX_HOMESERVER"] == hs_a
    assert m_a1["MATRIX_ACCESS_TOKEN"] == ""

    m_b = attach_for(home_b)
    assert m_b["MATRIX_HOMESERVER"] == hs_b
    assert m_b["MATRIX_HOMESERVER"] != hs_a
    assert m_b["MATRIX_HOMESERVER"] != polluted
    assert m_b["MATRIX_ACCESS_TOKEN"] == ""

    m_a2 = attach_for(home_a)
    assert m_a2["MATRIX_HOMESERVER"] == hs_a
    assert m_a2["MATRIX_HOMESERVER"] != hs_b
    assert m_a2["MATRIX_HOMESERVER"] != polluted
    assert m_a2["MATRIX_ACCESS_TOKEN"] == ""


def test_internal_turn_owns_and_releases_matrix_hub_session():
    async def scenario():
        router = DiscordSessionRouter(
            transport="Matrix", version_field="_matrix_hub_version",
        )
        adapter = MatrixHubClientAdapter(PlatformConfig(
            token="matrix-token",
            typing_indicator=False,
            extra={"homeserver": "https://matrix.example", "user_id": "@bot:matrix.example"},
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
            platform=Platform.MATRIX,
            chat_id="!room:matrix.example",
            chat_type="dm",
            user_id="@sender:matrix.example",
        )
        event = MessageEvent(
            text="background completion", source=source, message_id="$internal", internal=True
        )
        replay_completion = MagicMock()
        event._gateway_replay_completion = replay_completion
        session_key = adapter._event_session_key(event)
        successor_received = []

        async def successor_delivery(_key, record):
            successor_received.append(record)

        # The predecessor must be able to claim its own internal turn even
        # after the successor has become the hub's current generation.
        router.attach("B", successor_delivery)
        try:
            await adapter.handle_message(event)
            initial_accepted = event._gateway_accepted
            await asyncio.wait_for(first_started.wait(), timeout=2)
            initial_owner = router.owner_for(session_key)

            followup = MessageEvent(text="follow up", source=source, message_id="$followup")
            followup_record = serialize_drain_event(session_key, followup)
            followup_generation = await router.route(session_key, followup_record)
            successor_after_followup = len(successor_received)
            owner_with_followup = router.owner_for(session_key)

            finish_first.set()
            await asyncio.wait_for(first_finished.wait(), timeout=2)
            for _ in range(200):
                if len(processed) == 2 and router.owner_for(session_key) is None:
                    break
                await asyncio.sleep(0.01)
            released_owner = router.owner_for(session_key)

            next_record = serialize_drain_event(
                session_key, MessageEvent(text="next", source=source, message_id="$next")
            )
            idle_generation = await router.route(session_key, next_record)
        finally:
            finish_first.set()
            if first_started.is_set():
                await asyncio.wait_for(first_finished.wait(), timeout=2)
            if adapter._release_tasks:
                await asyncio.gather(*list(adapter._release_tasks), return_exceptions=True)
            from gateway.platforms.base import BasePlatformAdapter
            await BasePlatformAdapter.cancel_background_tasks(adapter)

        assert initial_owner == generation
        assert initial_accepted is True
        assert replay_completion.call_count == 1
        assert followup_generation == generation
        assert successor_after_followup == 0
        assert owner_with_followup == generation
        assert processed == ["background completion", "follow up"]
        assert released_owner is None
        assert idle_generation == "B"
        assert len(successor_received) == 1

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_old_release_preserves_newer_matrix_version():
    adapter = MatrixHubClientAdapter(PlatformConfig(token="fake-token"))
    session_key = "agent:main:matrix:42"
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
async def test_post_command_drain_releases_idle_matrix_ownership():
    adapter = MatrixHubClientAdapter(PlatformConfig(token="fake-token"))
    adapter._call = AsyncMock(return_value=True)
    session_key = "agent:main:matrix:42"
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
