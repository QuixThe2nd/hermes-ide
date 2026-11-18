"""Jev classification notices for the auto-goal intent path, through the real owning caller.

``GatewayGoalIntentMixin._run_auto_goal_evaluation`` is the runtime owner of
the goal-intent Jev requests: every real HTTP attempt (retry and failure
included) must post one concise notice line back to the originating chat via
the deferred status-notice lane, a no-call bypass must post nothing, a notice
delivery failure must not touch the classification, and each source's notices
must reach only that source's own delivery adapter (profile/bot origin
isolation). Only external I/O is stubbed: the Jev HTTP socket
(``requests.post``) and the platform send seam.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.jev_notice import go_formatter_binary
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

pytestmark = pytest.mark.skipif(
    go_formatter_binary() is None,
    reason="jev notices are rendered by the Go formatter binary; no cached "
           "binary and no Go toolchain to build one",
)

SESSION_KEY = "agent:main:telegram:dm:1001"


def _source(*, user_name="operator", profile=None, chat_id="chat-1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id="user-1",
        user_name=user_name,
        is_bot=False,
        profile=profile,
    )


def _event(text: str, *, source=None) -> MessageEvent:
    return MessageEvent(
        text=text, message_type=MessageType.TEXT,
        source=source if source is not None else _source(),
    )


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture
def runner(hermes_home, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    instance = gateway_run.GatewayRunner(
        GatewayConfig(sessions_dir=hermes_home / "sessions")
    )
    monkeypatch.setattr(instance, "_is_user_authorized", lambda _source: True)
    monkeypatch.setattr(instance, "_session_key_for_source", lambda _source: SESSION_KEY)
    yield instance
    executor = getattr(instance, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


def _wire_send(runner, *, adapter=None):
    """Replace only the platform send seam; return the collected notice lines."""
    sent: list = []

    async def fake_send(source, message):
        sent.append(message)

    runner._send_goal_status_notice = fake_send
    runner._adapter_for_source = lambda _source: adapter if adapter is not None else SimpleNamespace()
    return sent


def _jev_response(label, confidence):
    return SimpleNamespace(
        status_code=200,
        json=lambda: {"answers": {"intent": {"choice": label, "confidence": confidence}}},
    )


def _jev_http_error(status):
    return SimpleNamespace(status_code=status, json=lambda: {})


async def _drain(runner) -> None:
    tasks = [task for task in runner._background_tasks if not task.done()]
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_success_attempt_posts_one_notice_line(runner, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent = _wire_send(runner)
    with patch(
        "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.91)
    ):
        decision = await runner._run_auto_goal_evaluation(
            "why is tdarr down", _source(), "telegram"
        )
    assert decision["label"] == "task"
    assert sent == [
        "🔎 jev goal-intent (attempt 1/2) user message from operator: task (confidence 0.91)"
    ]


@pytest.mark.asyncio
async def test_retry_then_success_posts_one_notice_per_attempt(runner, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent = _wire_send(runner)
    responses = [_jev_http_error(502), _jev_response("chase", 0.77)]
    with patch("hermes_cli.goal_intent.requests.post", side_effect=responses) as post:
        decision = await runner._run_auto_goal_evaluation(
            "and is it still broken?", _source(), "telegram"
        )
    assert post.call_count == 2
    assert decision["label"] == "chase"
    assert sent == [
        "🔎 jev goal-intent (attempt 1/2) user message from operator: failed (http_502)",
        "🔎 jev goal-intent (attempt 2/2) user message from operator: chase (confidence 0.77)",
    ]


@pytest.mark.asyncio
async def test_both_attempts_failing_posts_two_failure_notices_and_fails_closed(runner, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent = _wire_send(runner)
    with patch(
        "hermes_cli.goal_intent.requests.post",
        side_effect=[_jev_http_error(500), _jev_http_error(500)],
    ) as post:
        decision = await runner._run_auto_goal_evaluation(
            "give me the file", _source(), "telegram"
        )
    assert post.call_count == 2
    assert decision["label"] == "unknown"
    assert sent == [
        "🔎 jev goal-intent (attempt 1/2) user message from operator: failed (http_500)",
        "🔎 jev goal-intent (attempt 2/2) user message from operator: failed (http_500)",
    ]


@pytest.mark.asyncio
async def test_chat_and_low_confidence_labels_are_visible(runner, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent = _wire_send(runner)
    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response("chat", 0.2)):
        await runner._run_auto_goal_evaluation("huh", _source(), "telegram")
    assert sent == [
        "🔎 jev goal-intent (attempt 1/2) user message from operator: chat (confidence 0.20)"
    ]


class TestNoCallPostsNothing:
    @pytest.mark.asyncio
    async def test_suppression_phrase_makes_no_request_and_no_notice(self, runner, monkeypatch):
        sent = _wire_send(runner)
        with patch("hermes_cli.goal_intent.requests.post") as post:
            runner._schedule_auto_goal_intent(_event("just answer this please"), SESSION_KEY)
            await _drain(runner)
        post.assert_not_called()
        assert sent == []

    @pytest.mark.asyncio
    async def test_missing_credential_makes_no_request_and_no_notice(self, runner, monkeypatch):
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        sent = _wire_send(runner)
        with patch("hermes_cli.goal_intent.requests.post") as post:
            decision = await runner._run_auto_goal_evaluation(
                "why is tdarr down", _source(), "telegram"
            )
        post.assert_not_called()
        assert sent == []
        assert decision["label"] == "unknown"


@pytest.mark.asyncio
async def test_notice_delivery_failure_never_touches_the_decision(runner, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")

    async def exploding_send(source, message):
        raise RuntimeError("platform send broke")

    runner._send_goal_status_notice = exploding_send
    runner._adapter_for_source = lambda _source: SimpleNamespace()
    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)):
        decision = await runner._run_auto_goal_evaluation(
            "why is tdarr down", _source(), "telegram"
        )
    assert decision["label"] == "task"
    assert decision["confidence"] == 0.9


@pytest.mark.asyncio
async def test_notices_reach_only_the_owning_source_adapter(runner, monkeypatch):
    """A/B/A origin isolation: each source's notices go to its own adapter only."""
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    adapter_a = SimpleNamespace()
    adapter_b = SimpleNamespace()
    sent_a, sent_b = [], []

    def make_send(sink):
        async def _send(source, message):
            sink.append(message)
        return _send

    adapters = {"chat-a": adapter_a, "chat-b": adapter_b}
    sends = {"chat-a": make_send(sent_a), "chat-b": make_send(sent_b)}

    async def send_for(source, message):
        await sends[source.chat_id](source, message)

    runner._send_goal_status_notice = send_for
    runner._adapter_for_source = lambda source: adapters[source.chat_id]

    source_a1 = _source(chat_id="chat-a", user_name="A-User")
    source_b = _source(chat_id="chat-b", user_name="B-User")
    source_a2 = _source(chat_id="chat-a", user_name="A-User")

    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)):
        await runner._run_auto_goal_evaluation("fix a", source_a1, "telegram")
        await runner._run_auto_goal_evaluation("fix b", source_b, "telegram")
        await runner._run_auto_goal_evaluation("fix a again", source_a2, "telegram")

    assert len(sent_a) == 2
    assert len(sent_b) == 1
    assert all("from A-User" in line for line in sent_a)
    assert "from B-User" in sent_b[0]
