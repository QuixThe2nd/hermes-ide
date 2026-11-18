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

    async def fake_send(source, message, **_kwargs):
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
async def test_notices_reach_only_the_owning_source_adapter(runner, monkeypatch, tmp_path):
    """A/B/A origin isolation through the REAL delivery resolver and profile scope.

    The runner's primary adapter serves the default profile; profile ``beta`` has
    its own adapter in ``_profile_adapters``. ``_adapter_for_source`` /
    ``_delivery_adapter_for`` / the profile-scope chain are the production
    code — only the platform send seam (I/O) is replaced, recording what each
    delivery saw. Each notice must leave through the OWNING profile's adapter
    with that profile's HERMES_HOME bound at send time (the scope carried by
    ``_spawn_goal_intent_task`` → ``_run_in_executor_with_context`` → the
    deferred delivery coroutine).
    """
    from pathlib import Path

    from hermes_constants import get_hermes_home

    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    # A second live profile home under the (faked) profiles root, registered
    # through the real creation path: the resolver's liveness check
    # (``named_profile_is_live``) requires an identity marker, and a bare
    # mkdir leaves a ghost shell the runtime correctly refuses (falls back to
    # the default home). Real registration = identity + layout, no mocks.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli.profiles import create_profile

    home_b = create_profile("beta", no_alias=True, no_skills=True)
    home_default = tmp_path / ".hermes"
    monkeypatch.setattr(runner.config, "multiplex_profiles", True, raising=False)

    deliveries: list = []  # (owning profile tag, notice line, HERMES_HOME at send)

    def _adapter(tag):
        async def _send(chat_id, message, metadata=None, **_kwargs):
            deliveries.append((tag, message, str(get_hermes_home())))
        return SimpleNamespace(send=_send)

    runner.adapters = {Platform.TELEGRAM: _adapter("default")}
    runner._profile_adapters = {"beta": {Platform.TELEGRAM: _adapter("beta")}}

    source_a1 = _source(chat_id="chat-a", user_name="A-User")
    source_b = _source(chat_id="chat-b", user_name="B-User", profile="beta")
    source_a2 = _source(chat_id="chat-a", user_name="A-User")

    async def _classify_and_deliver(text, source):
        runner._schedule_auto_goal_intent(_event(text, source=source), SESSION_KEY)
        await _drain(runner)
        # The deferred delivery coroutine is scheduled thread-safely from the
        # executor callback; yield the loop until it has run (bounded poll).
        loop = asyncio.get_running_loop()
        before = len(deliveries)
        deadline = loop.time() + 5.0
        while len(deliveries) <= before and loop.time() < deadline:
            await asyncio.sleep(0)

    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)):
        await _classify_and_deliver("fix a", source_a1)
        await _classify_and_deliver("fix b", source_b)
        await _classify_and_deliver("fix a again", source_a2)

    assert [(tag, line) for tag, line, _home in deliveries] == [
        ("default", "🔎 jev goal-intent (attempt 1/2) user message from A-User: task (confidence 0.90)"),
        ("beta", "🔎 jev goal-intent (attempt 1/2) user message from B-User: task (confidence 0.90)"),
        ("default", "🔎 jev goal-intent (attempt 1/2) user message from A-User: task (confidence 0.90)"),
    ]
    by_tag = {}
    for tag, _line, home in deliveries:
        by_tag.setdefault(tag, set()).add(home)
    # The sending scope was the OWNING profile's home, never another profile's.
    assert by_tag["default"] == {str(home_default)}
    assert by_tag["beta"] == {str(home_b)}


@pytest.mark.asyncio
async def test_jev_notice_send_is_marked_non_conversational_history(runner, monkeypatch):
    """The goal-path Jev notice is non-conversational history at the send seam.

    The real chain runs (classifier callback → deferred post-delivery lane →
    ``_send_goal_status_notice``); only the adapter's platform send is I/O and
    records what the owning caller passed. A troubleshooting notice must never
    become the conversation boundary or leak into the model's recovered
    context on platforms that honor the flag (Discord).
    """
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent: list = []  # (message, metadata)

    async def _send(chat_id, message, metadata=None, **_kwargs):
        sent.append((message, metadata))

    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(send=_send)}

    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)):
        runner._schedule_auto_goal_intent(_event("why is tdarr down"), SESSION_KEY)
        await _drain(runner)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not sent and loop.time() < deadline:
            await asyncio.sleep(0)

    assert [message for message, _meta in sent] == [
        "🔎 jev goal-intent (attempt 1/2) user message from operator: task (confidence 0.90)"
    ]
    metadata = sent[0][1]
    assert metadata is not None and metadata.get("non_conversational_history") is True
