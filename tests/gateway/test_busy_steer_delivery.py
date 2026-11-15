"""Discord busy-steer acceptance and actual context-delivery receipts."""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


_DELIVERED = "✅ Steer delivered — your message is now in the model’s context."


def _agent(*, tools=True, model=False):
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    agent._steer_delivery_listeners = []
    agent._steer_delivery_listeners_lock = threading.Lock()
    agent._pending_redirect = None
    agent._pending_redirect_lock = threading.Lock()
    agent._model_request_active = threading.Event()
    if model:
        agent._model_request_active.set()
    agent._executing_tools = tools
    agent._supports_active_turn_redirect = True
    agent._active_children = []
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._interrupt_thread_signal_pending = False
    agent._execution_thread_id = None
    agent._tool_worker_threads = None
    agent._tool_worker_threads_lock = None
    agent._active_request_abort = None
    agent.api_mode = "chat_completions"
    agent.get_activity_summary = MagicMock(return_value={
        "api_call_count": 3,
        "current_tool": "terminal",
        "seconds_since_activity": 0.0,
    })
    agent.interrupt = MagicMock(return_value=True)
    return agent


def _runner(agent, *, profile=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._running = True
    runner._background_tasks = set()
    runner.adapters = {}
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        multiplex_profiles=False,
    )
    runner.session_store = None
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True

    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = SimpleNamespace(extra={})
    adapter.platform = Platform.DISCORD
    adapter._reactions_enabled = lambda: True
    adapter._add_reaction = AsyncMock(return_value=True)
    runner.adapters[Platform.DISCORD] = adapter

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="dm",
        user_id="user-1",
        profile=profile,
    )
    session_key = build_session_key(source)
    runner._running_agents[session_key] = agent
    runner._gateway_loop = asyncio.get_running_loop()
    return runner, adapter, source, session_key


def _event(source, message_id, text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
        raw_message={"discord_hub_message_id": message_id},
    )


def _inject_post_tool(agent):
    messages = [{"role": "tool", "content": "tool output", "tool_call_id": "call-1"}]
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    return messages


def _inject_pre_api(agent):
    from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result

    messages = [{"role": "tool", "content": "tool output", "tool_call_id": "call-1"}]
    pending = agent._drain_pending_steer()
    assert pending
    _inject_steer_after_newest_tool_result(agent, messages, pending)
    return messages


async def _wait_for_sends(adapter, count):
    for _ in range(200):
        if adapter._send_with_retry.await_count >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} sends, got {adapter._send_with_retry.await_count}")


def _contents(adapter):
    return [call.kwargs["content"] for call in adapter._send_with_retry.await_args_list]


@pytest.fixture(autouse=True)
def _receipt_env(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", raising=False)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})


@pytest.mark.asyncio
async def test_tool_steer_ack_precedes_real_injection_receipt():
    agent = _agent(tools=True)
    runner, adapter, source, session_key = _runner(agent)
    event = _event(source, "m-1", "check the migration")

    assert await runner._handle_active_session_busy_message(event, session_key) is True

    assert len(agent._steer_delivery_listeners) == 1
    assert _contents(adapter)[0].startswith("⏩ Steered into current run (")
    assert _DELIVERED not in _contents(adapter)
    messages = await asyncio.to_thread(_inject_post_tool, agent)
    await _wait_for_sends(adapter, 2)

    assert messages[-1]["role"] == "user"
    assert "check the migration" in messages[-1]["content"]
    delivered = adapter._send_with_retry.await_args_list[1].kwargs
    assert delivered["content"] == _DELIVERED
    assert delivered["reply_to"] == "m-1"
    assert delivered["metadata"]["_interim_send"] is True
    assert adapter._send_with_retry.await_args_list[0].kwargs["metadata"]["_interim_send"] is True
    assert agent._steer_delivery_listeners == []


@pytest.mark.asyncio
async def test_batched_rapid_steers_each_get_their_own_message_anchor():
    agent = _agent(tools=True)
    runner, adapter, source, session_key = _runner(agent)

    await runner._handle_active_session_busy_message(_event(source, "m-1", "first"), session_key)
    await runner._handle_active_session_busy_message(_event(source, "m-2", "second"), session_key)
    assert len(agent._steer_delivery_listeners) == 2
    assert _contents(adapter) == [
        content for content in _contents(adapter) if content.startswith("⏩")
    ]

    messages = await asyncio.to_thread(_inject_post_tool, agent)
    await _wait_for_sends(adapter, 4)

    deliveries = adapter._send_with_retry.await_args_list[2:]
    assert [call.kwargs["content"] for call in deliveries] == [_DELIVERED, _DELIVERED]
    assert [call.kwargs["reply_to"] for call in deliveries] == ["m-1", "m-2"]
    assert "first" in messages[-1]["content"]
    assert "second" in messages[-1]["content"]
    assert agent._steer_delivery_listeners == []

    # Replayed listener input cannot mint a second receipt for either ID.
    agent._notify_steer_delivery_listeners(messages[-1]["content"])
    await asyncio.sleep(0)
    assert _contents(adapter).count(_DELIVERED) == 2


@pytest.mark.asyncio
async def test_batched_delivery_uses_each_source_profile_a_b_a(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    home = tmp_path / ".hermes"
    alpha = home / "profiles" / "alpha"
    beta = home / "profiles" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (alpha / "config.yaml").write_text("{}\n", encoding="utf-8")
    (beta / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _agent(tools=True)
    runner, adapter, source_a, key_a = _runner(agent, profile="alpha")
    runner.config.multiplex_profiles = True
    runner._profile_adapters = {
        "alpha": {Platform.DISCORD: adapter},
        "beta": {Platform.DISCORD: adapter},
    }
    source_b = SessionSource(
        platform=Platform.DISCORD, chat_id="channel-1", chat_type="dm",
        user_id="user-1", profile="beta",
    )
    key_b = build_session_key(source_b)
    runner._running_agents[key_b] = agent
    observed = {}

    async def record_delivery(**kwargs):
        if kwargs["content"] == _DELIVERED:
            observed[kwargs["reply_to"]] = get_hermes_home()

    adapter._send_with_retry = AsyncMock(side_effect=record_delivery)
    for source, key, message_id, text in (
        (source_a, key_a, "m-a1", "alpha one"),
        (source_b, key_b, "m-b", "beta"),
        (source_a, key_a, "m-a2", "alpha two"),
    ):
        await runner._handle_active_session_busy_message(_event(source, message_id, text), key)

    assert len(agent._steer_delivery_listeners) == 3
    await asyncio.to_thread(_inject_post_tool, agent)
    await _wait_for_sends(adapter, 6)

    assert observed == {"m-a1": alpha, "m-b": beta, "m-a2": alpha}



@pytest.mark.asyncio
async def test_pre_api_injection_also_confirms_real_delivery():
    agent = _agent(tools=True)
    runner, adapter, source, session_key = _runner(agent)
    await runner._handle_active_session_busy_message(_event(source, "m-pre", "before the request"), session_key)

    messages = _inject_pre_api(agent)
    await _wait_for_sends(adapter, 2)

    assert messages[-1]["role"] == "user"
    assert _contents(adapter)[1] == _DELIVERED
    assert adapter._send_with_retry.await_args_list[1].kwargs["reply_to"] == "m-pre"


@pytest.mark.asyncio
async def test_immediate_injection_during_redirect_still_orders_acceptance_first():
    agent = _agent(tools=True)
    real_redirect = agent.redirect

    def redirect_and_inject(text):
        accepted = real_redirect(text)
        if accepted:
            _inject_post_tool(agent)
        return accepted

    agent.redirect = redirect_and_inject
    runner, adapter, source, session_key = _runner(agent)

    await runner._handle_active_session_busy_message(_event(source, "m-race", "now"), session_key)
    await _wait_for_sends(adapter, 2)

    contents = _contents(adapter)
    assert contents[0].startswith("⏩ Steered into current run (")
    assert contents[1] == _DELIVERED


@pytest.mark.asyncio
async def test_model_redirect_never_emits_steer_delivery_receipt_on_acceptance():
    agent = _agent(tools=False, model=True)
    runner, adapter, source, session_key = _runner(agent)
    event = _event(source, "m-model", "change course")

    await runner._handle_active_session_busy_message(event, session_key)
    await asyncio.sleep(0)

    assert agent._pending_redirect is not None
    assert agent._steer_delivery_listeners == []
    assert len(_contents(adapter)) == 1
    assert _contents(adapter)[0].startswith("↪ Redirected current run")


@pytest.mark.asyncio
@pytest.mark.parametrize("text, refused", [("", False), ("correct this", True)])
async def test_empty_or_refused_input_has_no_delivery_binding(text, refused):
    agent = _agent(tools=True)
    if refused:
        agent.redirect = MagicMock(return_value=False)
    runner, adapter, source, session_key = _runner(agent)
    event = _event(source, "m-refused", text)

    await runner._handle_active_session_busy_message(event, session_key)
    agent._notify_steer_delivery_listeners(agent._pending_steer or text)
    await asyncio.sleep(0)

    assert agent._steer_delivery_listeners == []
    assert _DELIVERED not in _contents(adapter)
    assert adapter._pending_messages[session_key] is event


@pytest.mark.asyncio
async def test_put_back_and_turn_release_leave_no_listener_or_late_receipt():
    agent = _agent(tools=True)
    runner, adapter, source, session_key = _runner(agent)
    await runner._handle_active_session_busy_message(_event(source, "m-putback", "wait"), session_key)

    messages = [{"role": "user", "content": "not a tool result"}]
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    await asyncio.sleep(0)
    assert _DELIVERED not in _contents(adapter)
    assert len(agent._steer_delivery_listeners) == 1

    # The finalizer hands an undelivered leftover to the next turn, not to the listener.
    leftover = agent._drain_pending_steer()
    assert leftover and "wait" in leftover
    assert runner._release_running_agent_state(session_key) is True
    assert agent._steer_delivery_listeners == []

    agent._notify_steer_delivery_listeners(leftover)
    await asyncio.sleep(0)
    assert _DELIVERED not in _contents(adapter)

    # A cached agent can accept the next turn, but only the new binding remains.
    runner._running_agents[session_key] = agent
    await runner._handle_active_session_busy_message(_event(source, "m-next", "next turn"), session_key)
    assert len(agent._steer_delivery_listeners) == 1
    await asyncio.to_thread(_inject_post_tool, agent)
    await _wait_for_sends(adapter, 3)
    delivered_calls = [
        call for call in adapter._send_with_retry.await_args_list
        if call.kwargs["content"] == _DELIVERED
    ]
    assert [call.kwargs["reply_to"] for call in delivered_calls] == ["m-next"]


@pytest.mark.asyncio
async def test_delivery_send_failure_is_best_effort_and_not_replayed():
    agent = _agent(tools=True)
    runner, adapter, source, session_key = _runner(agent)
    original_send = adapter._send_with_retry

    async def fail_delivery(**kwargs):
        if kwargs["content"] == _DELIVERED:
            raise ConnectionError("Discord unavailable")
        return await original_send(**kwargs)

    adapter._send_with_retry = AsyncMock(side_effect=fail_delivery)
    await runner._handle_active_session_busy_message(_event(source, "m-send", "continue"), session_key)
    messages = _inject_post_tool(agent)
    await _wait_for_sends(adapter, 2)

    assert agent._pending_steer is None
    assert agent._steer_delivery_listeners == []
    agent._notify_steer_delivery_listeners(messages[-1]["content"])
    await asyncio.sleep(0)
    assert _contents(adapter).count(_DELIVERED) == 1


@pytest.mark.asyncio
async def test_global_opt_out_leaves_no_delivery_listener(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    agent = _agent(tools=True)
    runner, adapter, source, session_key = _runner(agent)
    await runner._handle_active_session_busy_message(_event(source, "m-off", "quiet"), session_key)

    _inject_post_tool(agent)
    await asyncio.sleep(0)

    assert agent._steer_delivery_listeners == []
    assert adapter._send_with_retry.await_count == 0
    adapter._add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_priority_tool_steer_uses_the_same_delivery_receipt(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "0")
    agent = _agent(tools=True)
    runner, adapter, source, session_key = _runner(agent)
    event = _event(source, "m-priority", "priority correction")

    assert await runner._handle_message(event) is None
    _inject_post_tool(agent)
    await _wait_for_sends(adapter, 2)

    assert adapter._send_with_retry.await_args_list[0].kwargs["reply_to"] == "m-priority"
    assert adapter._send_with_retry.await_args_list[1].kwargs["reply_to"] == "m-priority"
    assert _contents(adapter)[1] == _DELIVERED


@pytest.mark.asyncio
async def test_real_discord_replay_cannot_duplicate_acceptance_or_delivery(monkeypatch):
    """Admission dedupe and per-message delivery bindings compose end to end."""
    from gateway.config import PlatformConfig
    from plugins.platforms.discord import adapter as discord_platform

    for name, value in (
        ("DISCORD_REQUIRE_MENTION", "false"),
        ("DISCORD_AUTO_THREAD", "false"),
        ("DISCORD_IGNORE_NO_MENTION", "false"),
        ("DISCORD_HISTORY_BACKFILL", "false"),
    ):
        monkeypatch.setenv(name, value)
    agent = _agent(tools=True)
    runner, _unused_adapter, _source, _session_key = _runner(agent)
    adapter = discord_platform.DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter._allowed_user_ids = {"7"}
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    adapter._ready_event.set()
    adapter._text_batch_delay_seconds = 0
    adapter._send_with_retry = AsyncMock()
    adapter._reactions_enabled = lambda: True
    adapter._add_reaction = AsyncMock(return_value=True)
    runner.adapters[Platform.DISCORD] = adapter

    async def dispatch(event):
        session_key = build_session_key(event.source)
        runner._running_agents[session_key] = agent
        await runner._handle_active_session_busy_message(event, session_key)

    adapter.handle_message = dispatch
    channel = SimpleNamespace(id=123, name="general", guild=None, topic=None)

    def raw(message_id, text):
        return SimpleNamespace(
            id=message_id,
            content=text,
            mentions=[],
            attachments=[],
            reference=None,
            message_snapshots=None,
            created_at=None,
            channel=channel,
            guild=None,
            author=SimpleNamespace(id=7, display_name="Alice", name="Alice", bot=False),
            type=discord_platform.discord.MessageType.default,
        )

    first = raw(101, "first correction")
    second = raw(102, "second correction")
    replay = raw(101, "first correction")
    results = await asyncio.gather(
        adapter._dispatch_discord_message(first),
        adapter._dispatch_discord_message(second),
        adapter._dispatch_discord_message(replay),
    )

    assert results.count(True) == 2
    assert results.count(False) == 1
    assert len(agent._steer_delivery_listeners) == 2
    assert "first correction" in agent._pending_steer
    assert "second correction" in agent._pending_steer
    messages = await asyncio.to_thread(_inject_post_tool, agent)
    await _wait_for_sends(adapter, 4)

    delivered = [
        call for call in adapter._send_with_retry.await_args_list
        if call.kwargs["content"] == _DELIVERED
    ]
    assert sorted(call.kwargs["reply_to"] for call in delivered) == ["101", "102"]
    assert len(delivered) == 2
    assert sorted(str(call.args[0].id) for call in adapter._add_reaction.await_args_list) == [
        "101", "102",
    ]
    assert all(call.args[1] == "⏩" for call in adapter._add_reaction.await_args_list)
    assert "first correction" in messages[-1]["content"]
    assert "second correction" in messages[-1]["content"]
    assert agent._steer_delivery_listeners == []


@pytest.mark.asyncio
async def test_same_message_id_stays_in_its_session_adapter_and_profile(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    home = tmp_path / ".hermes"
    alpha = home / "profiles" / "alpha"
    beta = home / "profiles" / "beta"
    for profile_home in (alpha, beta):
        profile_home.mkdir(parents=True)
        (profile_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent_a = _agent(tools=True)
    agent_b = _agent(tools=True)
    runner, adapter_a, source_a, key_a = _runner(agent_a, profile="alpha")
    runner.config.multiplex_profiles = True
    key_a = runner._session_key_for_source(source_a)
    runner._running_agents = {key_a: agent_a}
    adapter_b = MagicMock()
    adapter_b._send_with_retry = AsyncMock()
    runner._profile_adapters = {
        "alpha": {Platform.DISCORD: adapter_a},
        "beta": {Platform.DISCORD: adapter_b},
    }
    source_b = SessionSource(
        platform=Platform.DISCORD, chat_id="channel-1", chat_type="dm",
        user_id="user-1", profile="beta",
    )
    key_b = runner._session_key_for_source(source_b)
    assert key_a != key_b
    runner._running_agents[key_b] = agent_b
    observed = {}

    def record(profile):
        async def send(**kwargs):
            if kwargs["content"] == _DELIVERED:
                observed[profile] = (get_hermes_home(), kwargs["reply_to"])
        return send

    adapter_a._send_with_retry = AsyncMock(side_effect=record("alpha"))
    adapter_b._send_with_retry = AsyncMock(side_effect=record("beta"))
    await runner._handle_active_session_busy_message(_event(source_a, "same-id", "same text"), key_a)
    await runner._handle_active_session_busy_message(_event(source_b, "same-id", "same text"), key_b)
    assert len(agent_a._steer_delivery_listeners) == 1
    assert len(agent_b._steer_delivery_listeners) == 1

    await asyncio.to_thread(_inject_post_tool, agent_a)
    assert agent_a._steer_delivery_listeners == []
    await _wait_for_sends(adapter_a, 2)
    await asyncio.sleep(0)
    assert adapter_b._send_with_retry.await_count == 1
    assert observed == {"alpha": (alpha, "same-id")}

    await asyncio.to_thread(_inject_post_tool, agent_b)
    await _wait_for_sends(adapter_b, 2)
    assert observed == {"alpha": (alpha, "same-id"), "beta": (beta, "same-id")}
    assert agent_a._steer_delivery_listeners == []
    assert agent_b._steer_delivery_listeners == []


@pytest.mark.asyncio
async def test_acceptance_send_failure_does_not_lose_truthful_delivery():
    agent = _agent(tools=True)
    real_redirect = agent.redirect

    def redirect_and_inject(text):
        accepted = real_redirect(text)
        if accepted:
            _inject_post_tool(agent)
        return accepted

    agent.redirect = redirect_and_inject
    runner, adapter, source, session_key = _runner(agent)
    original_send = adapter._send_with_retry

    async def fail_acceptance(**kwargs):
        if kwargs["content"].startswith("⏩"):
            raise ConnectionError("acceptance send failed")
        return await original_send(**kwargs)

    adapter._send_with_retry = AsyncMock(side_effect=fail_acceptance)
    await runner._handle_active_session_busy_message(_event(source, "m-ack-error", "continue"), session_key)
    await _wait_for_sends(adapter, 2)

    assert agent._pending_steer is None
    assert agent._steer_delivery_listeners == []
    assert _contents(adapter)[0].startswith("⏩")
    assert _contents(adapter)[1] == _DELIVERED
    agent._notify_steer_delivery_listeners("continue")
    await asyncio.sleep(0)
    assert _contents(adapter).count(_DELIVERED) == 1
