"""The hub-side save-login RPC against the real DiscordAdapter modal path.

``DiscordAdapter.vault_save_login_prompt`` is what a hub-client gateway turn
reaches over the hub socket: it must run the existing button/modal transport
inside the hub process, keep every owner check, and fail closed on wrong
owner/channel, replay, cancel, expiry, disconnect, and malformed params. The
submission dict may only travel in the RPC result. Synthetic values only.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session_identity import identity_of
from tests.gateway._discord_vault_stubs import (
    IDENTIFIER,
    ORIGIN,
    PASSWORD,
    Channel,
    Interaction,
    RejectIngestion,
    assert_prompt_registries_empty,
    cancel_prompt,
    close_client,
    identity_dict,
    open_modal,
    submit_modal,
)
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

REAL_TURN_TIMEOUT = 30


@pytest.fixture
def hub_rpc_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    discord = load_plugin_adapter("discord")
    adapter = discord.DiscordAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._allowed_user_ids = {"777"}
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: adapter}
    adapter.gateway_runner = runner
    monkeypatch.setattr(runner, "_is_session_incognito", lambda _source: False)

    source = adapter.build_source(
        chat_id="4242", chat_type="dm", user_id="777", user_name="Requesting User"
    )
    assert adapter._canonicalize(source) is not None
    session_key = adapter._source_session_key(source)
    channel = Channel()

    async def resolve_channel(_chat_id):
        return channel

    monkeypatch.setattr(adapter, "_resolve_channel", resolve_channel)
    adapter._client = SimpleNamespace(add_view=lambda *_a, **_k: None, close=close_client)
    channel.client = adapter._client
    adapter.handle_message = RejectIngestion()
    yield SimpleNamespace(
        adapter=adapter,
        channel=channel,
        source=source,
        identity=identity_of(source),
        session_key=session_key,
    )
    from plugins.platforms.discord.vault_prompts import close_all_vault_prompts

    close_all_vault_prompts(adapter)


def _start_rpc(rig, *, session_key=None):
    rig.channel.prompt_available.clear()
    return asyncio.create_task(rig.adapter.vault_save_login_prompt(
        source=rig.source.to_dict(),
        identity=identity_dict(rig.identity),
        session_key=session_key or rig.session_key,
        chat_id="4242",
        metadata=None,
        origin=ORIGIN,
        host="example.test",
    ))


async def _wait_for_prompt(rig):
    await asyncio.wait_for(rig.channel.prompt_available.wait(), REAL_TURN_TIMEOUT)
    return rig.channel.sent[-1]


@pytest.mark.asyncio
async def test_hub_rpc_modal_roundtrip_returns_synthetic_submission(hub_rpc_rig, caplog):
    rig = hub_rpc_rig
    with caplog.at_level(logging.DEBUG):
        task = _start_rpc(rig)
        prompt_message = await _wait_for_prompt(rig)
        _interaction, modal = await open_modal(rig, prompt_message)
        assert modal is not None
        await submit_modal(rig, modal, prompt_message)
        result = await asyncio.wait_for(task, REAL_TURN_TIMEOUT)

    assert result["state"] == "submitted"
    assert result["answer"] == {"identifier": IDENTIFIER, "password": PASSWORD}
    assert modal.identifier.value == modal.password.value == ""
    assert rig.adapter.handle_message.called is False
    assert_prompt_registries_empty(rig.adapter)
    assert PASSWORD not in caplog.text
    assert IDENTIFIER not in caplog.text


@pytest.mark.asyncio
async def test_hub_rpc_denies_wrong_owner_and_wrong_channel(hub_rpc_rig):
    rig = hub_rpc_rig
    rig.adapter._allowed_user_ids.add("888")  # admitted to Discord, not this owner
    task = _start_rpc(rig)
    prompt_message = await _wait_for_prompt(rig)
    wrong_channel = Channel(9999)
    wrong_channel.client = rig.adapter._client

    wrong_user, wrong_user_modal = await open_modal(rig, prompt_message, user_id="888")
    assert wrong_user_modal is None
    assert "Only the requesting user" in wrong_user.response.messages[-1]["content"]

    wrong_channel_interaction, wrong_channel_modal = await open_modal(
        rig, prompt_message, channel=wrong_channel
    )
    assert wrong_channel_modal is None
    assert "original channel" in wrong_channel_interaction.response.messages[-1]["content"]
    assert not task.done()

    _interaction, modal = await open_modal(rig, prompt_message)
    attacker_password = PASSWORD + "-attacker"
    rejected = await submit_modal(
        rig, modal, prompt_message, user_id="888", password=attacker_password
    )
    assert "not owned by you" in rejected.response.messages[-1]["content"]
    assert not task.done()

    await cancel_prompt(rig, prompt_message)
    result = await asyncio.wait_for(task, REAL_TURN_TIMEOUT)
    assert result == {"state": "unavailable", "answer": None}
    assert_prompt_registries_empty(rig.adapter)
    assert attacker_password not in str([m.payload for m in rig.channel.sent])


@pytest.mark.asyncio
async def test_hub_rpc_cancel_fails_closed(hub_rpc_rig):
    rig = hub_rpc_rig
    task = _start_rpc(rig)
    prompt_message = await _wait_for_prompt(rig)
    _interaction, modal = await open_modal(rig, prompt_message)
    await cancel_prompt(rig, prompt_message)
    result = await asyncio.wait_for(task, REAL_TURN_TIMEOUT)
    assert result == {"state": "unavailable", "answer": None}
    assert modal.identifier.value == modal.password.value == ""
    assert_prompt_registries_empty(rig.adapter)


@pytest.mark.asyncio
async def test_hub_rpc_second_request_replays_fail_closed_and_cancels_first(hub_rpc_rig):
    rig = hub_rpc_rig
    first = _start_rpc(rig)
    first_message = await _wait_for_prompt(rig)
    second = _start_rpc(rig)
    second_message = await _wait_for_prompt(rig)

    first_result = await asyncio.wait_for(first, REAL_TURN_TIMEOUT)
    assert first_result == {"state": "unavailable", "answer": None}

    # The superseded prompt's buttons must no longer submit.
    _i, stale_modal = await open_modal(rig, first_message)
    assert stale_modal is None
    _interaction, modal = await open_modal(rig, second_message)
    await submit_modal(rig, modal, second_message)
    second_result = await asyncio.wait_for(second, REAL_TURN_TIMEOUT)
    assert second_result["state"] == "submitted"
    assert second_result["answer"] == {"identifier": IDENTIFIER, "password": PASSWORD}
    assert_prompt_registries_empty(rig.adapter)


@pytest.mark.asyncio
async def test_hub_rpc_expiry_fails_closed(hub_rpc_rig, monkeypatch):
    rig = hub_rpc_rig
    monkeypatch.setattr(
        "plugins.platforms.discord.vault_prompts._VAULT_PROMPT_TIMEOUT_SECONDS", 0.05
    )
    task = _start_rpc(rig)
    prompt_message = await _wait_for_prompt(rig)
    _interaction, modal = await open_modal(rig, prompt_message)
    result = await asyncio.wait_for(task, REAL_TURN_TIMEOUT)
    assert result == {"state": "unavailable", "answer": None}
    assert modal._request.state.value == "expired"
    assert modal.identifier.value == modal.password.value == ""
    assert_prompt_registries_empty(rig.adapter)


@pytest.mark.asyncio
async def test_hub_rpc_disconnect_fails_closed(hub_rpc_rig):
    rig = hub_rpc_rig
    task = _start_rpc(rig)
    prompt_message = await _wait_for_prompt(rig)
    _interaction, modal = await open_modal(rig, prompt_message)
    await rig.adapter.disconnect()
    result = await asyncio.wait_for(task, REAL_TURN_TIMEOUT)
    assert result == {"state": "unavailable", "answer": None}
    assert modal._request.state.value == "disconnected"
    assert modal.identifier.value == modal.password.value == ""
    assert_prompt_registries_empty(rig.adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"source": {}},
        {"source": {"platform": "discord", "chat_id": "4242"}},
        {"source": {"platform": "discord", "chat_id": "4242", "user_id": "777"},
         "session_key": ""},
    ],
)
async def test_hub_rpc_malformed_params_fail_closed_without_prompt(hub_rpc_rig, overrides):
    rig = hub_rpc_rig
    kwargs = dict(
        source=rig.source.to_dict(),
        identity=identity_dict(rig.identity),
        session_key=rig.session_key,
        chat_id="4242",
        metadata=None,
        origin=ORIGIN,
        host="example.test",
    )
    kwargs.update(overrides)
    result = await asyncio.wait_for(
        rig.adapter.vault_save_login_prompt(**kwargs), REAL_TURN_TIMEOUT
    )
    assert result == {"state": "failed", "answer": None}
    assert rig.channel.sent == []
    assert_prompt_registries_empty(rig.adapter)


@pytest.mark.asyncio
async def test_hub_rpc_submission_never_reaches_ingestion_or_pending_after_close(hub_rpc_rig):
    rig = hub_rpc_rig
    task = _start_rpc(rig)
    prompt_message = await _wait_for_prompt(rig)
    _interaction, modal = await open_modal(rig, prompt_message)
    await submit_modal(rig, modal, prompt_message)
    result = await asyncio.wait_for(task, REAL_TURN_TIMEOUT)
    assert result["state"] == "submitted"
    # The binding is closed with the RPC; a replayed interaction cannot reopen it.
    replay = Interaction(
        rig.channel, "777", message=prompt_message, custom_id=modal.custom_id
    )
    from tests.gateway._discord_vault_stubs import populate_modal

    populate_modal(modal, password=PASSWORD + "-replay", interaction=replay)
    await modal.on_submit(replay)
    assert "already closed" in replay.response.messages[-1]["content"]
    assert rig.adapter.handle_message.called is False
    assert_prompt_registries_empty(rig.adapter)


def test_hub_wait_covers_modal_timeout_plus_slack():
    """A short default RPC wait would cancel live prompts — the invariant, not the value."""
    discord = pytest.importorskip(
        "discord", reason="real discord.py is required for this contract"
    )
    assert getattr(discord, "__file__", None)
    from plugins.platforms.discord import hub_vault_prompts, vault_prompts

    assert (
        hub_vault_prompts._HUB_VAULT_PROMPT_WAIT_SECONDS
        >= vault_prompts._VAULT_PROMPT_TIMEOUT_SECONDS + 5.0
    )
