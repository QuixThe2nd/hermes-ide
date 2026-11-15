"""Codex interruption recovery must finish before the gateway goal judge runs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class _FakeCreateStream:
    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def close(self):
        self.closed = True


def _codex_text_stream(text):
    item = SimpleNamespace(
        type="message",
        status="completed",
        content=[SimpleNamespace(type="output_text", text=text)],
    )
    return _FakeCreateStream([
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.output_text.delta", delta=text),
        SimpleNamespace(type="response.output_item.done", item=item),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8),
            ),
        ),
    ])


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.sends = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id="status-message")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture
def gateway(hermes_home, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    (hermes_home / "config.yaml").write_text(
        "display:\n"
        "  tool_progress: off\n"
        "  interim_assistant_messages: false\n"
        "  streaming: false\n",
        encoding="utf-8",
    )
    runner = gateway_run.GatewayRunner(
        GatewayConfig(sessions_dir=hermes_home / "sessions")
    )
    adapter = _RecordingAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **kwargs: (
            "gpt-5-codex",
            {
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_key": "codex-token",
                "api_mode": "codex_responses",
                "provider": "openai-codex",
                "skip_memory": True,
            },
        ),
    )
    yield runner, adapter
    executor = getattr(runner, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_gateway_judges_recovered_answer_and_done_enqueues_nothing(
    gateway, monkeypatch
):
    """One real gateway turn recovers, then the normal goal hook accepts its answer."""
    import run_agent
    from agent.agent_runtime_helpers import _INTERRUPTED_PLACEHOLDER
    from hermes_cli.goals import GoalManager

    runner, adapter = gateway
    source = _source()
    entry = await runner.async_session_store.get_or_create_session(
        source, touch_activity=False
    )
    GoalManager(session_id=entry.session_id).set("Return the complete answer")

    recovered = "Recovered and completed inside the original gateway turn."
    requests = []
    streams = iter([
        _codex_text_stream(_INTERRUPTED_PLACEHOLDER),
        _codex_text_stream(recovered),
    ])
    request_client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: requests.append(kwargs) or next(streams)
        )
    )
    monkeypatch.setattr(
        run_agent.AIAgent,
        "_create_request_openai_client",
        lambda self, **kwargs: request_client,
    )
    monkeypatch.setattr(
        run_agent.AIAgent,
        "_close_request_openai_client",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        run_agent.AIAgent,
        "_abort_request_openai_client",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.plugins.get_pre_turn_end_continue_message",
        lambda **kwargs: None,
    )

    judge_calls = []

    def complete_goal(goal, last_response, **kwargs):
        judge_calls.append((goal, last_response))
        return "done", "the recovered answer is complete", False, None, False

    with patch("hermes_cli.goals.judge_goal", side_effect=complete_goal) as judge:
        result = await runner._run_agent(
            message="Return the complete answer",
            context_prompt="",
            history=[],
            source=source,
            session_id=entry.session_id,
            session_key=entry.session_key,
        )
        await runner._run_post_turn_hooks(
            agent_result=result,
            source=source,
            is_internal=False,
        )

    assert result["completed"] is True
    assert result["failed"] is False
    assert result["final_response"] == recovered
    assert result["api_calls"] == 2
    assert len(requests) == 2
    assert requests[0]["instructions"] == requests[1]["instructions"]
    assert requests[0]["prompt_cache_key"] == requests[1]["prompt_cache_key"]

    judge.assert_called_once()
    assert judge_calls == [("Return the complete answer", recovered)]
    assert _INTERRUPTED_PLACEHOLDER not in judge_calls[0][1]
    state = GoalManager(session_id=entry.session_id).state
    assert state is not None
    assert state.status == "done"
    assert state.last_verdict == "done"
    assert runner._queued_events == {}
    assert adapter._pending_messages == {}
