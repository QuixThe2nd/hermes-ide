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
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.jev_notice import go_formatter_binary
from gateway.platforms.base import BasePlatformAdapter, SendResult
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


class _RegistryAdapter(BasePlatformAdapter):
    """Telegram adapter whose post-delivery machinery is all production code.

    ``register_post_delivery_callback`` / ``pop_post_delivery_callback`` /
    ``_fire_post_delivery_callback`` / the session guards are the real
    registry the gateway uses; only ``send`` records instead of doing I/O.
    """

    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM
        )
        self.sends: list = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from hermes_constants import get_hermes_home

        self.sends.append(
            {
                "chat_id": chat_id,
                "content": content,
                "metadata": metadata,
                "home": str(get_hermes_home()),
            }
        )
        return SendResult(success=True, message_id="1")

    def jev_sends(self) -> list:
        return [s for s in self.sends if str(s["content"]).startswith("🔎 jev")]


class _GatedPost:
    """``requests.post`` stand-in whose completion the test controls.

    The classifier thread blocks inside the attempt until ``release()``, so a
    test can fire the turn's delivery boundary while the real classification
    is provably still in flight.
    """

    def __init__(self, response):
        self._response = response
        self._gate = threading.Event()
        self.started = threading.Event()

    def __call__(self, *_args, **_kwargs):
        self.started.set()
        assert self._gate.wait(timeout=10), "test never released the gated attempt"
        return self._response

    def release(self) -> None:
        self._gate.set()


async def _poll_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached before deadline"
        await asyncio.sleep(0)


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


class TestPostDeliveryRegistryLifecycle:
    """Late classifications through the REAL adapter callback registry.

    The turn's delivery boundary (``_fire_post_delivery_callback`` in the
    turn task's finally) is the registry's only consumer, while the goal
    classifier runs detached from the turn: the boundary waits at most 20s
    for a decision and a cold formatter build can take minutes. Every real
    classification attempt must therefore still get exactly one delivery
    attempt — registered before the boundary, delivered directly after it —
    never a stranded registry entry waiting for an unrelated future turn.
    """

    @pytest.mark.asyncio
    async def test_attempt_completing_after_the_boundary_delivers_directly(
        self, runner, monkeypatch
    ):
        """The review's late scenario: the turn ends (response delivered,
        errored or interrupted) while the classification is still in flight."""
        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        adapter = _RegistryAdapter()
        runner.adapters = {Platform.TELEGRAM: adapter}
        guard = asyncio.Event()
        adapter._active_sessions[SESSION_KEY] = guard

        gated = _GatedPost(_jev_response("task", 0.91))
        with patch("hermes_cli.goal_intent.requests.post", gated):
            runner._schedule_auto_goal_intent(_event("why is tdarr down"), SESSION_KEY)
            await _poll_until(gated.started.is_set)  # classification in flight
            await adapter._fire_post_delivery_callback(SESSION_KEY, guard)
            gated.release()  # the attempt completes only after the boundary
            await _drain(runner)
            await _poll_until(lambda: adapter.jev_sends())

        assert [s["content"] for s in adapter.jev_sends()] == [
            "🔎 jev goal-intent (attempt 1/2) user message from operator: task (confidence 0.91)"
        ]
        assert adapter.jev_sends()[0]["metadata"].get("non_conversational_history") is True
        # Nothing stranded: an unrelated future boundary must not re-send.
        assert adapter._post_delivery_callbacks == {}
        await adapter._fire_post_delivery_callback(SESSION_KEY, guard)
        assert len(adapter.jev_sends()) == 1

    @pytest.mark.asyncio
    async def test_attempt_during_the_turn_defers_until_the_boundary(
        self, runner, monkeypatch
    ):
        """Normal ordering is unchanged: a notice ready while the turn is live
        waits for the boundary (it must not interleave with the response
        stream) and fires exactly once there."""
        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        adapter = _RegistryAdapter()
        runner.adapters = {Platform.TELEGRAM: adapter}
        guard = asyncio.Event()
        adapter._active_sessions[SESSION_KEY] = guard

        with patch(
            "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)
        ):
            runner._schedule_auto_goal_intent(_event("why is tdarr down"), SESSION_KEY)
            await _drain(runner)
            await _poll_until(lambda: SESSION_KEY in adapter._post_delivery_callbacks)

        assert adapter.jev_sends() == []  # deferred behind the response delivery
        await adapter._fire_post_delivery_callback(SESSION_KEY, guard)
        assert [s["content"] for s in adapter.jev_sends()] == [
            "🔎 jev goal-intent (attempt 1/2) user message from operator: task (confidence 0.90)"
        ]
        assert adapter._post_delivery_callbacks == {}

    @pytest.mark.asyncio
    async def test_attempt_after_an_errored_turn_still_delivers(self, runner, monkeypatch):
        """The boundary fires from the real turn task's finally even when the
        turn errors; a classification completing after that must still deliver
        exactly once with no next user turn."""
        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        adapter = _RegistryAdapter()
        runner.adapters = {Platform.TELEGRAM: adapter}

        async def failing_handler(event):
            raise RuntimeError("agent exploded")

        adapter.set_message_handler(failing_handler)
        guard = asyncio.Event()
        adapter._active_sessions[SESSION_KEY] = guard
        await adapter._process_message_background(_event("why is tdarr down"), SESSION_KEY)

        with patch(
            "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)
        ):
            runner._schedule_auto_goal_intent(_event("classify me"), SESSION_KEY)
            await _drain(runner)
            await _poll_until(lambda: adapter.jev_sends())

        assert [s["content"] for s in adapter.jev_sends()] == [
            "🔎 jev goal-intent (attempt 1/2) user message from operator: task (confidence 0.90)"
        ]
        assert adapter._post_delivery_callbacks == {}

    @pytest.mark.asyncio
    async def test_followup_turn_drains_and_leaves_no_stranded_work(self, runner):
        """A queued follow-up hands the guard to the drain turn; both runs
        complete through the real finally boundaries and leave the registry
        empty with the guard released (a post-turn registration is refused,
        not parked)."""
        adapter = _RegistryAdapter()
        fired: list = []

        async def registering_handler(event):
            adapter.register_post_delivery_callback(
                SESSION_KEY, lambda: fired.append("cb")
            )
            return None

        adapter.set_message_handler(registering_handler)
        guard = asyncio.Event()
        adapter._active_sessions[SESSION_KEY] = guard
        adapter._pending_messages[SESSION_KEY] = _event("queued follow-up")

        await adapter._process_message_background(_event("first"), SESSION_KEY)
        # The drain turn may interleave with run 1's finally awaits; wait for
        # the queued event to be consumed and every spawned task to finish.
        await _poll_until(lambda: not adapter._pending_messages)
        for task in list(adapter._background_tasks):
            if not task.done():
                await asyncio.wait_for(asyncio.shield(task), timeout=5)

        assert fired  # the runs' registrations fired, never stranded
        assert adapter._post_delivery_callbacks == {}
        assert adapter.register_post_delivery_callback(SESSION_KEY, lambda: None) is False

    @pytest.mark.asyncio
    async def test_late_notice_delivers_through_the_owning_profile_scope(
        self, runner, monkeypatch, tmp_path
    ):
        """A/B/A routing with REAL registry adapters: the deferred send of a
        late notice (boundary already fired, no active turn) still runs in the
        OWNING profile's scope and reaches only that profile's adapter."""
        from pathlib import Path

        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from hermes_cli.profiles import create_profile

        home_b = create_profile("beta", no_alias=True, no_skills=True)
        monkeypatch.setattr(runner.config, "multiplex_profiles", True, raising=False)
        adapter_default = _RegistryAdapter()
        adapter_b = _RegistryAdapter()
        runner.adapters = {Platform.TELEGRAM: adapter_default}
        runner._profile_adapters = {"beta": {Platform.TELEGRAM: adapter_b}}

        source_b = _source(chat_id="chat-b", user_name="B-User", profile="beta")
        with patch(
            "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)
        ):
            runner._schedule_auto_goal_intent(_event("fix b", source=source_b), SESSION_KEY)
            await _drain(runner)
            await _poll_until(lambda: adapter_b.jev_sends())

        sends = adapter_b.jev_sends()
        assert [s["content"] for s in sends] == [
            "🔎 jev goal-intent (attempt 1/2) user message from B-User: task (confidence 0.90)"
        ]
        assert sends[0]["home"] == str(home_b)  # owning profile's scope at the eventual send
        assert adapter_default.jev_sends() == []
