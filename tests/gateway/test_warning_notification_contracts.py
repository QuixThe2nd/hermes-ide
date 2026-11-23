"""Diagnostic suppression preserves real producer outcomes and control paths."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent import empty_response_guard
from agent.status_output import StatusOutputMixin
from agent.turn_empty_response import _terminal_empty
from gateway.config import Platform
from gateway.run import GatewayRunner, _load_gateway_config
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


class Agent(StatusOutputMixin):
    suppress_status_output = True
    log_prefix = ""
    provider = "openrouter"
    model = "model"
    base_url = "https://example.invalid/v1"
    _fallback_chain = []
    _empty_content_retries = 3

    def _try_activate_fallback(self):
        return False

    def _persist_session(self, *args):
        self.persisted = True

    def _extract_reasoning(self, *args):
        return ""

    def _drop_trailing_empty_response_scaffolding(self, *args):
        pass

    def _build_assistant_message(self, *args):
        return {"role": "assistant"}


@pytest.mark.parametrize("enabled", [False, True])
def test_real_retry_producers_keep_final_failure_and_persistence(tmp_path, monkeypatch, caplog, enabled):
    from gateway import run

    (tmp_path / "config.yaml").write_text(f"display: {{suppress_warning_notifications: {str(not enabled).lower()}}}")
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    sent = []
    async def send(chat_id, text, **kwargs):
        sent.append(text)
        return SimpleNamespace(success=True)
    adapter = SimpleNamespace(send=send)
    source = SessionSource(platform=Platform.SLACK, chat_id="D1", user_id="U1")
    gateway = object.__new__(GatewayRunner)
    ctx = TurnContext(source=source, user_config=_load_gateway_config(), _run_still_current=lambda: True,
                      _status_adapter_snapshot=adapter, _status_chat_id="D1", _status_thread_metadata={})
    agent = Agent()
    agent.status_callback = TurnRunner(gateway, ctx)._status_callback_sync
    monkeypatch.setattr(run, "safe_schedule_threadsafe", lambda coro, *a, **k: asyncio.run(coro))
    setattr(agent, empty_response_guard._STREAK_COST_ATTR, Decimal("1.25"))
    messages = []
    final = _terminal_empty(agent, SimpleNamespace(), "stop", messages)
    assert bool(sent) is enabled
    assert messages[-1]["_empty_terminal_sentinel"] is True
    assert "Empty response" in caplog.text
    result = {"final_response": final, "messages": messages, "failed": True}
    response, silent, _ = asyncio.run(gateway._hmwa_shape_agent_response(
        result, source, [], SimpleNamespace(session_id="session"), None, None, None, "session", "slack", 0))
    assert response and response != "(empty)" and not silent


@pytest.mark.parametrize("yaml_text,expected", [
    ("display: broken", True), ("display: [broken]", True),
    ("display: {platforms: broken}", True),
    ("display: {suppress_warning_notifications: true, platforms: broken}", False),
])
def test_bad_config_containers_do_not_abort_startup_warning(tmp_path, monkeypatch, yaml_text, expected):
    from gateway import run

    (tmp_path / "config.yaml").write_text(yaml_text)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    gateway = object.__new__(GatewayRunner)
    gateway._session_db_init_error = "database is locked"
    gateway._session_db_handle_cache = None
    gateway._notification_channel_transports = lambda: [(Platform.SLACK, None, None, None)]
    gateway._send_notification_channel_message = AsyncMock()
    asyncio.run(gateway._send_session_db_warning_notifications())
    assert gateway._send_notification_channel_message.await_count == int(expected)
