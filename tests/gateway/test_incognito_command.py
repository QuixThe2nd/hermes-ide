"""Behavior contracts for the gateway /incognito command (new incognito sessions)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command


def _source(chat_id: str = "chat-a") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id=f"user-{chat_id}",
    )


def _store(tmp_path) -> SessionStore:
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None  # Exercise the legacy mirror as well as the routing-index API.
    return store


def _runner(store: SessionStore) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = store.config
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    # _handle_reset_command rig (mirrors tests/gateway/test_session_model_reset.py).
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""
    return runner


@pytest.mark.asyncio
async def test_incognito_starts_new_flagged_session_and_status_reports_it(tmp_path):
    store = _store(tmp_path)
    runner = _runner(store)
    source = _source()
    old_entry = store.get_or_create_session(source)
    old_session_id = old_entry.session_id

    off_status = await runner._handle_incognito_command(
        MessageEvent(text="/incognito status", source=source)
    )
    assert "not incognito" in str(getattr(off_status, "text", off_status))

    reply = await runner._handle_incognito_command(MessageEvent(text="/incognito", source=source))

    assert runner._is_session_incognito(source) is True
    new_entry = store._entries[old_entry.session_key]
    assert new_entry.session_id != old_session_id
    # The reply mirrors /new's reset banner plus the incognito note.
    reply_text = str(getattr(reply, "text", reply))
    assert "Incognito session" in reply_text

    on_status = await runner._handle_incognito_command(
        MessageEvent(text="/incognito status", source=source)
    )
    assert "incognito" in str(getattr(on_status, "text", on_status)).lower()
    assert on_status != off_status

    # status only reports: the session id and flag are unchanged.
    assert store._entries[old_entry.session_key].session_id == new_entry.session_id
    assert runner._is_session_incognito(source) is True

    # /new afterwards produces a normal session (fresh metadata).
    await runner._handle_reset_command(MessageEvent(text="/new", source=source))
    assert runner._is_session_incognito(source) is False
    assert store._entries[old_entry.session_key].session_id != new_entry.session_id
    # The previous (non-incognito) session content was never flagged in the DB either.
    assert store.get_session_metadata(old_entry.session_key, "incognito", False) is False


@pytest.mark.asyncio
async def test_incognito_flag_survives_store_reload_and_is_session_scoped(tmp_path):
    store = _store(tmp_path)
    runner = _runner(store)
    source_a = _source("chat-a")
    source_b = _source("chat-b")
    entry_a = store.get_or_create_session(source_a)
    entry_b_session = store.get_or_create_session(source_b).session_id

    await runner._handle_incognito_command(MessageEvent(text="/incognito", source=source_a))

    assert runner._is_session_incognito(source_a) is True
    # The other chat's session is untouched: not incognito, same session id.
    assert runner._is_session_incognito(source_b) is False
    assert store._entries[
        store.get_or_create_session(source_b).session_key
    ].session_id == entry_b_session

    reloaded = _store(tmp_path)
    reloaded_runner = _runner(reloaded)
    assert reloaded_runner._is_session_incognito(source_a) is True
    assert reloaded_runner._is_session_incognito(source_b) is False
    assert reloaded.get_session_metadata(entry_a.session_key, "incognito") is True


@pytest.mark.asyncio
async def test_incognito_reports_persistence_failure_without_flagging_session(tmp_path):
    store = _store(tmp_path)
    runner = _runner(store)
    source = _source()
    store.get_or_create_session(source)

    runner.async_session_store.set_session_metadata = AsyncMock(return_value=False)

    response = await runner._handle_incognito_command(MessageEvent(text="/incognito", source=source))

    assert "could not persist" in str(getattr(response, "text", response)).lower()
    assert runner._is_session_incognito(source) is False


def test_incognito_is_registered_only_for_gateway():
    command = resolve_command("incognito")
    assert command is not None
    assert command.gateway_only is True
    assert command.cli_only is False
    assert command.args_hint == "[name|status]"
    assert command.subcommands == ("status",)
    assert command.busy_policy == "interrupt_then_dispatch"
    assert command.busy_handler == "incognito"
    assert "incognito" in GATEWAY_KNOWN_COMMANDS


def test_turn_runner_sets_and_resets_agent_incognito_from_session_metadata(tmp_path):
    store = _store(tmp_path)
    runner = _runner(store)
    source = _source()
    entry = store.get_or_create_session(source)
    ctx = SimpleNamespace(
        session_key=entry.session_key,
        session_id=entry.session_id,
        source=source,
        message="current message",
        persist_user_display_kind=None,
        persist_user_display_metadata=None,
        moa_config=None,
        inbound_message_id=None,
        mute_notification_reply=False,
    )
    turn = object.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = ctx
    turn._native_image_run_message = lambda: ctx.message

    class Agent:
        def __init__(self):
            self.seen = []

        def run_conversation(self, _message, **_kwargs):
            self.seen.append(getattr(self, "_incognito", None))
            return {"final_response": "ok", "messages": [], "completed": True}

    agent = Agent()
    store.set_session_metadata(entry.session_key, "incognito", True)
    turn._run_conversation_with_approval(agent, [], None, None, None)
    store.set_session_metadata(entry.session_key, "incognito", False)
    turn._run_conversation_with_approval(agent, [], None, None, None)

    assert agent.seen == [True, False]


@pytest.mark.asyncio
async def test_inbound_drops_unbounded_backfill_and_reply_text_when_incognito(tmp_path):
    store = _store(tmp_path)
    runner = _runner(store)
    source = _source()
    entry = store.get_or_create_session(source)
    store.set_session_metadata(entry.session_key, "incognito", True)
    event = MessageEvent(
        text="current",
        source=source,
        channel_context="older channel messages",
        reply_to_message_id="old-1",
        reply_to_text="older reply text",
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[], session_key=entry.session_key,
    )

    assert result == "current"


@pytest.mark.asyncio
async def test_inbound_preserves_backfill_and_reply_text_when_not_incognito(tmp_path):
    store = _store(tmp_path)
    runner = _runner(store)
    source = _source()
    entry = store.get_or_create_session(source)
    event = MessageEvent(
        text="current",
        source=source,
        channel_context="older channel messages",
        reply_to_message_id="old-1",
        reply_to_text="older reply text",
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[], session_key=entry.session_key,
    )

    assert result is not None
    assert "older channel messages\n\n[New message]\n" in result
    assert 'Replying to: "older reply text"' in result
    assert result.endswith("current")


@pytest.mark.asyncio
async def test_inbound_allows_only_adapter_bounded_context_when_incognito(tmp_path):
    store = _store(tmp_path)
    runner = _runner(store)
    source = _source()
    entry = store.get_or_create_session(source)
    store.set_session_metadata(entry.session_key, "incognito", True)
    event = MessageEvent(
        text="current",
        source=source,
        channel_context="thread starter and later",
        reply_to_message_id="200",
        reply_to_text="thread starter",
    )
    event._incognito_context_bounded = True
    event._incognito_reply_bounded = True

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[], session_key=entry.session_key,
    )

    assert result is not None
    assert "thread starter and later\n\n[New message]\n" in result
    assert 'Replying to: "thread starter"' in result
    assert result.endswith("current")
