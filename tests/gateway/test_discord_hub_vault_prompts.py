"""Discord save-login prompts when the live gateway adapter is the hub client.

DiscordHubClientAdapter owns no discord.py transport: the modal lives in the hub
process and must be reached over the hub RPC lane. These tests pin the gateway
half — the vault_save_login_scope installs a callback for a hub-client turn, the
RPC roundtrip returns the submission to the waiting tool worker, and every
failure mode (error, timeout, disconnect, session mismatch) fails closed with no
pending request left behind. Synthetic credential values only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session_identity import identity_of
from gateway.turn_context import TurnContext
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter
from tests.gateway._discord_vault_stubs import (
    IDENTIFIER,
    ORIGIN,
    PASSWORD,
    REAL_TURN_TIMEOUT,
    RecordingStore,
    identity_dict,
)


@pytest.fixture
def hub_vault_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    adapter = DiscordHubClientAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._running = True
    adapter._token_key = "synthetic-token-key"
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: adapter}
    adapter.gateway_runner = runner
    monkeypatch.setattr(runner, "_is_session_incognito", lambda _source: False)

    source = adapter.build_source(
        chat_id="4242", chat_type="dm", user_id="777", user_name="Requesting User"
    )
    assert adapter._canonicalize(source) is not None
    session_key = adapter._source_session_key(source)
    assert session_key

    from tools import browser_vault_tool

    store = RecordingStore()
    fills = []
    observations = []
    monkeypatch.setattr(
        browser_vault_tool,
        "_eval_js",
        lambda _task_id, _expression: {"success": True, "result": f"{ORIGIN}/login"},
    )
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)

    def record_fill(handle, task_id=None):
        fills.append({"handle": handle, "task_id": task_id})
        return json.dumps({"success": True, "filled_fields": 1, "origin": ORIGIN})

    monkeypatch.setattr(browser_vault_tool, "browser_vault_fill", record_fill)

    ctx = TurnContext(
        source=source,
        session_key=session_key,
        session_id="hub-vault-session",
        message="Save this login",
        user_config={},
        _run_still_current=lambda: True,
    )
    yield SimpleNamespace(
        adapter=adapter,
        source=source,
        session_key=session_key,
        ctx=ctx,
        runner=runner,
        store=store,
        fills=fills,
        observations=observations,
        home=tmp_path / "home",
    )

    from agent.vault_backends.unlock import (
        set_code_prompt_callback,
        set_save_login_prompt_callback,
        set_unlock_prompt_callback,
    )

    set_save_login_prompt_callback(None)
    set_unlock_prompt_callback(None)
    set_code_prompt_callback(None)


class _SaveLoginAgent:
    """One model turn whose tool call is dispatched by the production registry."""

    def __init__(self, observations=None):
        self.observations = observations if observations is not None else []

    def run_conversation(self, api_message, **kwargs):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from agent.vault_backends.unlock import get_save_login_prompt_callback
        from model_tools import handle_function_call
        from tools.thread_context import propagate_context_to_thread

        parent_callback = get_save_login_prompt_callback()

        def call_tool():
            child_callback = get_save_login_prompt_callback()
            raw = handle_function_call(
                "browser_vault_save_login",
                {"label": "Example"},
                task_id=kwargs["task_id"],
            )
            return raw, child_callback

        with ThreadPoolExecutor(max_workers=1) as executor:
            raw, child_callback = executor.submit(
                propagate_context_to_thread(call_tool)
            ).result()
        self.observations.append({
            "parent_callback": parent_callback,
            "child_callback": child_callback,
        })
        return {
            "final_response": raw,
            "messages": [
                {"role": "user", "content": api_message},
                {"role": "assistant", "content": raw},
            ],
            "api_calls": 1,
        }


async def _run_turn(rig, *, agent=None):
    rig.ctx._loop_for_step = asyncio.get_running_loop()
    agent = agent or _SaveLoginAgent(rig.observations)
    worker = asyncio.create_task(
        rig.runner._run_in_executor_with_context(
            lambda: TurnRunner(rig.runner, rig.ctx)._run_conversation_with_session(
                agent, [], None, None, None
            )
        )
    )
    return await asyncio.wait_for(worker, timeout=REAL_TURN_TIMEOUT)


def _make_binding(rig, *, session_key=None):
    binding = rig.adapter.create_vault_save_login_binding(
        source=rig.source,
        identity=identity_of(rig.source),
        session_key=session_key or rig.session_key,
        loop=asyncio.get_running_loop(),
        chat_id="4242",
        metadata=None,
    )
    assert binding is not None
    return binding


def _vault_rpc_calls(calls):
    return [
        params for method, params in calls
        if method == "rpc" and (params or {}).get("method") == "vault_save_login_prompt"
    ]


@pytest.mark.asyncio
async def test_hub_client_turn_installs_callback_and_stores_synthetic_login(
    hub_vault_rig, monkeypatch, caplog
):
    """The scope must install the hub binding's callback (the base class fails closed)."""
    rig = hub_vault_rig
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, dict(params or {})))
        await asyncio.sleep(0)
        return {
            "state": "submitted",
            "answer": {"identifier": IDENTIFIER, "password": PASSWORD},
        }

    monkeypatch.setattr(rig.adapter, "_call", fake_call)
    with caplog.at_level(logging.DEBUG):
        result = await _run_turn(rig)

    payload = json.loads(result["final_response"])
    assert payload["success"] is True
    assert payload["origin"] == ORIGIN
    assert rig.store.items[0]["origin"] == ORIGIN
    assert rig.store.items[0]["payload"]["password"] == PASSWORD
    assert rig.store.items[0]["payload"]["identifier"] == IDENTIFIER
    assert rig.fills == [{"handle": "vault_synthetic_login", "task_id": "hub-vault-session"}]
    assert rig.observations[0]["parent_callback"] is not None
    assert rig.observations[0]["child_callback"] is not None

    vault_calls = _vault_rpc_calls(calls)
    assert len(vault_calls) == 1
    kwargs = vault_calls[0]["kwargs"]
    assert kwargs["source"]["user_id"] == "777"
    assert kwargs["source"]["chat_id"] == "4242"
    assert kwargs["session_key"] == rig.session_key
    assert kwargs["origin"] == ORIGIN
    assert kwargs["host"]
    assert Path(kwargs["identity"]["runtime_home"]) == Path(rig.home)
    assert PASSWORD not in caplog.text
    assert IDENTIFIER not in caplog.text


@pytest.mark.asyncio
async def test_hub_client_binding_roundtrip_returns_submission_and_clears_pending(
    hub_vault_rig, monkeypatch
):
    rig = hub_vault_rig
    binding = _make_binding(rig)

    async def fake_call(method, params=None):
        assert (params or {}).get("method") == "vault_save_login_prompt"
        return {"state": "submitted",
                "answer": {"identifier": IDENTIFIER, "password": PASSWORD}}

    monkeypatch.setattr(rig.adapter, "_call", fake_call)
    answer = await asyncio.wait_for(
        asyncio.to_thread(binding.prompt, ORIGIN, "example.test"), 10
    )
    assert answer == {"identifier": IDENTIFIER, "password": PASSWORD}
    assert binding._pending == {}


@pytest.mark.asyncio
async def test_hub_client_binding_cancelled_hub_prompt_fails_closed(hub_vault_rig, monkeypatch):
    rig = hub_vault_rig
    binding = _make_binding(rig)

    async def fake_call(method, params=None):
        return {"state": "unavailable", "answer": None}

    monkeypatch.setattr(rig.adapter, "_call", fake_call)
    answer = await asyncio.wait_for(
        asyncio.to_thread(binding.prompt, ORIGIN, "example.test"), 10
    )
    assert answer is None
    assert binding._pending == {}


@pytest.mark.asyncio
async def test_hub_client_binding_rpc_error_fails_closed(hub_vault_rig, monkeypatch):
    rig = hub_vault_rig
    binding = _make_binding(rig)

    async def broken_call(method, params=None):
        raise ConnectionError("Discord hub connection closed")

    monkeypatch.setattr(rig.adapter, "_call", broken_call)
    answer = await asyncio.wait_for(
        asyncio.to_thread(binding.prompt, ORIGIN, "example.test"), 10
    )
    assert answer is None
    assert binding._pending == {}


@pytest.mark.asyncio
async def test_hub_client_binding_timeout_cancels_rpc_and_fails_closed(
    hub_vault_rig, monkeypatch
):
    rig = hub_vault_rig
    binding = _make_binding(rig)
    entered = asyncio.Event()
    outcomes = []

    async def hanging_call(method, params=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcomes.append("cancelled")
            raise
        return {"state": "submitted", "answer": {"identifier": IDENTIFIER, "password": PASSWORD}}

    monkeypatch.setattr(rig.adapter, "_call", hanging_call)
    monkeypatch.setattr(
        "plugins.platforms.discord.hub_vault_prompts._HUB_VAULT_PROMPT_WAIT_SECONDS", 0.2
    )
    answer = await asyncio.wait_for(
        asyncio.to_thread(binding.prompt, ORIGIN, "example.test"), 10
    )
    assert answer is None
    assert binding._pending == {}
    for _ in range(20):
        if outcomes:
            break
        await asyncio.sleep(0.05)
    assert outcomes == ["cancelled"]


@pytest.mark.asyncio
async def test_hub_client_binding_fails_closed_before_rpc_when_detached(
    hub_vault_rig, monkeypatch
):
    rig = hub_vault_rig
    rig.adapter._running = False
    binding = _make_binding(rig)
    calls = []

    async def unexpected_call(method, params=None):
        calls.append(method)
        raise AssertionError("a detached adapter must not schedule the vault RPC")

    monkeypatch.setattr(rig.adapter, "_call", unexpected_call)
    assert binding.prompt(ORIGIN, "example.test") is None
    assert calls == []
    assert binding._pending == {}


@pytest.mark.asyncio
async def test_hub_client_rejects_source_session_mismatch(hub_vault_rig):
    rig = hub_vault_rig
    assert (
        rig.adapter.create_vault_save_login_binding(
            source=rig.source,
            identity=identity_of(rig.source),
            session_key="agent:main:discord:dm:not-the-source",
            loop=asyncio.get_running_loop(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_hub_client_turn_session_mismatch_leaves_no_callback(hub_vault_rig, monkeypatch):
    rig = hub_vault_rig
    rig.ctx.session_key = "agent:main:discord:dm:not-the-source"
    calls = []

    async def unexpected_call(method, params=None):
        calls.append(method)
        raise AssertionError("a mismatched source must not reach the hub vault RPC")

    monkeypatch.setattr(rig.adapter, "_call", unexpected_call)
    result = await _run_turn(rig)
    payload = json.loads(result["final_response"])
    assert payload["error_type"] == "prompt_unavailable"
    assert calls == []
    assert rig.store.items == []


def _hub_client_with_source(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    adapter = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: adapter}
    adapter.gateway_runner = runner
    monkeypatch.setattr(runner, "_is_session_incognito", lambda _source: False)
    source = adapter.build_source(
        chat_id="4242", chat_type="dm", user_id="777", user_name="Hub Socket User"
    )
    assert adapter._canonicalize(source) is not None
    return adapter, source, adapter._source_session_key(source)


@pytest.mark.platforms("posix")
def test_hub_socket_roundtrip_returns_synthetic_submission(tmp_path, monkeypatch, short_af_unix_path):
    async def scenario():
        from plugins.platforms.discord.hub import DiscordHubServer, _token_key

        # pytest tmp dirs can exceed macOS's 104-byte sun_path limit; the
        # shared fixture binds in a short, test-owned directory.
        server = DiscordHubServer(short_af_unix_path)
        captured = []

        class FakeHubAdapter:
            async def disconnect(self):
                return None

            async def vault_save_login_prompt(self, **kwargs):
                captured.append(dict(kwargs))
                return {"state": "submitted",
                        "answer": {"identifier": IDENTIFIER, "password": PASSWORD}}

        async def fake_attach(generation_id, config_data, profile, profile_routes,
                              gate_credential=None, reaction_gate_reload=None, hermes_home=None):
            server._adapters[_token_key(config_data["token"])] = FakeHubAdapter()

        monkeypatch.setattr(server, "_attach_adapter", fake_attach)
        await server.start()
        client, source, session_key = _hub_client_with_source(monkeypatch, tmp_path)
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            binding = client.create_vault_save_login_binding(
                source=source,
                identity=identity_of(source),
                session_key=session_key,
                loop=asyncio.get_running_loop(),
                chat_id="4242",
                metadata=None,
            )
            assert binding is not None
            answer = await asyncio.wait_for(
                asyncio.to_thread(binding.prompt, ORIGIN, "example.test"), 15
            )
            assert answer == {"identifier": IDENTIFIER, "password": PASSWORD}
            assert binding._pending == {}
            kwargs = captured[0]
            assert kwargs["source"]["user_id"] == "777"
            assert kwargs["source"]["chat_id"] == "4242"
            assert kwargs["session_key"] == session_key
            assert kwargs["origin"] == ORIGIN
        finally:
            await client.disconnect()
            await server.stop()

    asyncio.run(scenario())


@pytest.mark.platforms("posix")
def test_hub_socket_disconnect_wakes_waiter_and_fails_closed(tmp_path, monkeypatch, short_af_unix_path):
    async def scenario():
        from plugins.platforms.discord.hub import DiscordHubServer, _token_key

        # pytest tmp dirs can exceed macOS's 104-byte sun_path limit; the
        # shared fixture binds in a short, test-owned directory.
        server = DiscordHubServer(short_af_unix_path)
        started = asyncio.Event()
        release = asyncio.Event()

        class HangingHubAdapter:
            async def disconnect(self):
                return None

            async def vault_save_login_prompt(self, **kwargs):
                started.set()
                await release.wait()
                return {"state": "submitted",
                        "answer": {"identifier": IDENTIFIER, "password": PASSWORD}}

        async def fake_attach(generation_id, config_data, profile, profile_routes,
                              gate_credential=None, reaction_gate_reload=None, hermes_home=None):
            server._adapters[_token_key(config_data["token"])] = HangingHubAdapter()

        monkeypatch.setattr(server, "_attach_adapter", fake_attach)
        await server.start()
        client, source, session_key = _hub_client_with_source(monkeypatch, tmp_path)
        client._socket_path = server.socket_path
        try:
            assert await client.connect()
            binding = client.create_vault_save_login_binding(
                source=source,
                identity=identity_of(source),
                session_key=session_key,
                loop=asyncio.get_running_loop(),
                chat_id="4242",
                metadata=None,
            )
            assert binding is not None
            waiter = asyncio.create_task(
                asyncio.to_thread(binding.prompt, ORIGIN, "example.test")
            )
            await asyncio.wait_for(started.wait(), 10)
            await client.disconnect()
            answer = await asyncio.wait_for(waiter, 10)
            assert answer is None
            assert binding._pending == {}
        finally:
            release.set()
            await client.disconnect()
            await server.stop()

    asyncio.run(scenario())
