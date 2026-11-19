"""Jev classification notices for the auto-goal intent path, through the real owning caller.

``GatewayGoalIntentMixin._run_auto_goal_evaluation`` is the runtime owner of
the goal-intent Jev requests: every real HTTP attempt (retry and failure
included) must post one concise notice line back to the originating chat via
the DIRECT outbound lane on the source's owning adapter — independent of the
turn's delivery boundary and never the shared post-delivery registry (the
registry slot is keyed by session, so ordering behind the boundary strands
late notices and drops the older one when two overlapped turns each hold
one). A no-call bypass must post nothing, a notice delivery failure must not
touch the classification, and each source's notices must reach only that
source's own delivery adapter (profile/bot origin isolation). Only external
I/O is stubbed: the Jev HTTP socket (``requests.post``) and the platform
send seam.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

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

SESSION_KEY = "agent:main:discord:dm:1001"


def _source(*, user_name="operator", profile=None, chat_id="chat-1") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
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


class _RecordingAdapter(BasePlatformAdapter):
    """Discord adapter whose turn lifecycle is all production code.

    ``_process_message_background`` — the real delivery boundary, session
    guards and queued-follow-up drain — runs unmodified; only ``send``
    records instead of doing I/O.
    """

    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, typing_indicator=False), Platform.DISCORD
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
        return [s for s in self.sends if str(s["content"]).startswith(("🔎 jev", "```"))]


def _registry_generation(adapter) -> "int | None":
    """Generation currently parked in the adapter's post-delivery registry."""
    entry = adapter._post_delivery_callbacks.get(SESSION_KEY)
    if isinstance(entry, tuple) and len(entry) == 2:
        return entry[0]
    return None


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
            "why is tdarr down", _source(), "discord"
        )
    assert decision["label"] == "task"
    assert sent == [
        "```\n🔍 Goal Intent: 0.91 Confidence\n```"
    ]


@pytest.mark.asyncio
async def test_retry_then_success_posts_one_notice_per_attempt(runner, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent = _wire_send(runner)
    responses = [_jev_http_error(502), _jev_response("chase", 0.77)]
    with patch("hermes_cli.goal_intent.requests.post", side_effect=responses) as post:
        decision = await runner._run_auto_goal_evaluation(
            "and is it still broken?", _source(), "discord"
        )
    assert post.call_count == 2
    assert decision["label"] == "chase"
    assert sent == [
        "```\n🔍 Goal Intent: failed (http_502)\n```",
        "```\n🔍 Goal Intent: 0.77 Confidence\n```",
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
            "give me the file", _source(), "discord"
        )
    assert post.call_count == 2
    assert decision["label"] == "unknown"
    assert sent == [
        "```\n🔍 Goal Intent: failed (http_500)\n```",
        "```\n🔍 Goal Intent: failed (http_500)\n```",
    ]


@pytest.mark.asyncio
async def test_chat_and_low_confidence_labels_are_visible(runner, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent = _wire_send(runner)
    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response("chat", 0.2)):
        await runner._run_auto_goal_evaluation("huh", _source(), "discord")
    assert sent == [
        "```\n🔍 Goal Intent: 0.20 Confidence\n```"
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
                "why is tdarr down", _source(), "discord"
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
            "why is tdarr down", _source(), "discord"
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

    runner.adapters = {Platform.DISCORD: _adapter("default")}
    runner._profile_adapters = {"beta": {Platform.DISCORD: _adapter("beta")}}

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
        ("default", "```\n🔍 Goal Intent: 0.90 Confidence\n```"),
        ("beta", "```\n🔍 Goal Intent: 0.90 Confidence\n```"),
        ("default", "```\n🔍 Goal Intent: 0.90 Confidence\n```"),
    ]
    by_tag = {}
    for tag, _line, home in deliveries:
        by_tag.setdefault(tag, set()).add(home)
    # The sending scope was the OWNING profile's home, never another profile's.
    assert by_tag["default"] == {str(home_default)}
    assert by_tag["beta"] == {str(home_b)}


@pytest.mark.asyncio
async def test_jev_notice_send_is_marked_non_conversational_and_interim(runner, monkeypatch):
    """The goal-path Jev notice is non-conversational interim history at the send seam.

    The real chain runs (classifier callback → direct outbound lane →
    ``_send_goal_status_notice``); only the adapter's platform send is I/O and
    records what the owning caller passed. A troubleshooting notice must never
    become the conversation boundary or leak into the model's recovered
    context on platforms that honor the flag (Discord), and must never seal a
    live native stream as if it were the turn's final message.
    """
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    sent: list = []  # (message, metadata)

    async def _send(chat_id, message, metadata=None, **_kwargs):
        sent.append((message, metadata))

    runner.adapters = {Platform.DISCORD: SimpleNamespace(send=_send)}

    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)):
        runner._schedule_auto_goal_intent(_event("why is tdarr down"), SESSION_KEY)
        await _drain(runner)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not sent and loop.time() < deadline:
            await asyncio.sleep(0)

    assert [message for message, _meta in sent] == [
        "```\n🔍 Goal Intent: 0.90 Confidence\n```"
    ]
    metadata = sent[0][1]
    assert metadata is not None
    assert metadata.get("non_conversational_history") is True
    assert metadata.get("_interim_send") is True


class TestIndependentDeliveryLifecycle:
    """Diagnostic emission is independent of the turn's delivery boundary.

    The goal-intent classifier runs detached from the turn on an executor
    thread; each real attempt's notice goes out through the DIRECT outbound
    lane on the owning adapter and never the shared post-delivery registry.
    The registry slot is keyed by session while each turn stamps its own
    ``_hermes_run_generation`` on the shared guard, so ordering behind the
    boundary strands notices whose turn already ended (no boundary left to
    fire them) and silently drops the older notice when two overlapped turns
    each hold one — a newer generation replaces the older slot outright
    (the review4 acceptance blocker). A status line may therefore arrive
    BEFORE the answer; that is intended.
    """

    @pytest.mark.asyncio
    async def test_notice_is_delivered_while_the_response_is_still_blocked(
        self, runner, monkeypatch
    ):
        """A classification completing mid-turn does not wait for the turn:
        the notice is on the wire while the handler has not even returned."""
        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        adapter = _RecordingAdapter()
        runner.adapters = {Platform.DISCORD: adapter}
        registrations: list = []
        original_register = adapter.register_post_delivery_callback

        def _spying_register(session_key, callback, *, generation=None):
            registrations.append(generation)
            return original_register(session_key, callback, generation=generation)

        adapter.register_post_delivery_callback = _spying_register
        handler_release = asyncio.Event()

        async def handler(event):
            runner._schedule_auto_goal_intent(event, SESSION_KEY)
            await handler_release.wait()
            return "the answer"

        adapter.set_message_handler(handler)
        notice = "```\n🔍 Goal Intent: 0.90 Confidence\n```"
        with patch(
            "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)
        ):
            task = asyncio.create_task(
                adapter._process_message_background(
                    _event("why is tdarr down"), SESSION_KEY
                )
            )
            await _poll_until(lambda: adapter.jev_sends())  # turn still inside handler

        assert handler_release.is_set() is False  # delivered before the answer existed
        assert [s["content"] for s in adapter.jev_sends()] == [notice]
        assert adapter._post_delivery_callbacks == {}

        handler_release.set()
        await asyncio.wait_for(task, 10)
        contents = [s["content"] for s in adapter.sends]
        assert contents.index(notice) < contents.index("the answer")
        assert registrations == []  # the notice path never touched the shared registry
        assert adapter._post_delivery_callbacks == {}

    @pytest.mark.asyncio
    async def test_notice_completing_after_the_boundary_is_delivered_once(
        self, runner, monkeypatch
    ):
        """The turn fully completed first; the classification's notice still
        arrives exactly once, with no future boundary to double-fire it."""
        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        adapter = _RecordingAdapter()
        runner.adapters = {Platform.DISCORD: adapter}

        async def handler(event):
            return "the answer"

        adapter.set_message_handler(handler)
        await adapter._process_message_background(
            _event("why is tdarr down"), SESSION_KEY
        )
        assert adapter.jev_sends() == []  # boundary fired, nothing stranded

        notice = "```\n🔍 Goal Intent: 0.90 Confidence\n```"
        with patch(
            "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)
        ):
            runner._schedule_auto_goal_intent(_event("classify me now"), SESSION_KEY)
            await _drain(runner)
            await _poll_until(lambda: adapter.jev_sends())

        assert [s["content"] for s in adapter.jev_sends()] == [notice]
        assert adapter._post_delivery_callbacks == {}
        guard = adapter._active_sessions.get(SESSION_KEY)
        if guard is not None:
            await adapter._fire_post_delivery_callback(SESSION_KEY, guard)
        assert len(adapter.jev_sends()) == 1

    @pytest.mark.asyncio
    async def test_notice_after_an_interrupted_turn_is_delivered_once(
        self, runner, monkeypatch
    ):
        """The turn was interrupted mid-handler; the classification's notice
        still arrives exactly once with no registry entry left behind."""
        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        adapter = _RecordingAdapter()
        runner.adapters = {Platform.DISCORD: adapter}
        started = asyncio.Event()
        handler_release = asyncio.Event()

        async def handler(event):
            started.set()
            await handler_release.wait()
            return "never delivered"

        adapter.set_message_handler(handler)
        with patch(
            "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)
        ):
            task = asyncio.create_task(
                adapter._process_message_background(
                    _event("why is tdarr down"), SESSION_KEY
                )
            )
            await _poll_until(started.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            runner._schedule_auto_goal_intent(_event("classify me now"), SESSION_KEY)
            await _drain(runner)
            await _poll_until(lambda: adapter.jev_sends())

        notice = "```\n🔍 Goal Intent: 0.90 Confidence\n```"
        assert [s["content"] for s in adapter.jev_sends()] == [notice]
        assert adapter._post_delivery_callbacks == {}

    @pytest.mark.asyncio
    async def test_two_overlapped_turn_requests_both_emit(self, runner, monkeypatch):
        """The review4 acceptance blocker, through the real notifier path.

        Two turns overlap on one session (a queued follow-up drains onto the
        reused guard while the older turn's finally is still running), each
        turn stamps its own generation and each holds a classification.
        Ordering the notices behind the delivery boundary dropped the older
        one (a newer registration replaced its registry slot); direct
        outbound delivery must emit BOTH, exactly once each, without the
        registry ever holding an entry.
        """
        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        adapter = _RecordingAdapter()
        runner.adapters = {Platform.DISCORD: adapter}
        registrations: list = []
        original_register = adapter.register_post_delivery_callback

        def _spying_register(session_key, callback, *, generation=None):
            registrations.append(generation)
            return original_register(session_key, callback, generation=generation)

        adapter.register_post_delivery_callback = _spying_register

        def _notice_landed(generation: int) -> bool:
            marker = "0.90 Confidence" if generation == 1 else "0.80 Confidence"
            return any(
                marker in str(s["content"]) for s in adapter.jev_sends()
            ) or _registry_generation(adapter) == generation

        async def handler(event):
            # Stamp this run's generation exactly as the gateway does per
            # turn (run_agent_cache), so the two turns are distinguishable
            # the way production makes them.
            guard = adapter._active_sessions[SESSION_KEY]
            generation = 1 if event.text == "first" else 2
            guard._hermes_run_generation = generation
            runner._schedule_auto_goal_intent(event, SESSION_KEY)
            await _poll_until(lambda: _notice_landed(generation), timeout=10.0)
            return f"answer {generation}"

        adapter.set_message_handler(handler)

        def _routed_post(_url, json=None, **_kwargs):
            message = str((json or {}).get("state", {}).get("message") or "")
            if message == "first":
                return _jev_response("task", 0.9)
            return _jev_response("chase", 0.8)

        # Hold run 1's finally between its typing cleanup and its delivery
        # boundary until run 2's notice has landed — the exact window the
        # acceptance blocker lived in (bounded by _poll_until's deadline).
        original_stop = adapter._stop_typing_refresh
        stop_calls = {"n": 0}
        first_coro: list = []

        async def _stalling_stop(chat_id, typing_task=None, *, metadata=None, **kwargs):
            coro = asyncio.current_task().get_coro()
            if first_coro and coro is first_coro[0]:
                stop_calls["n"] += 1
                if stop_calls["n"] == 2:
                    await _poll_until(lambda: _notice_landed(2), timeout=10.0)
            return await original_stop(chat_id, typing_task, metadata=metadata, **kwargs)

        adapter._stop_typing_refresh = _stalling_stop
        adapter._pending_messages[SESSION_KEY] = _event("second")

        with patch("hermes_cli.goal_intent.requests.post", side_effect=_routed_post):
            first_task = asyncio.create_task(
                adapter._process_message_background(_event("first"), SESSION_KEY)
            )
            first_coro.append(first_task.get_coro())
            await asyncio.wait_for(first_task, 20)
            tasks = {t for t in adapter._session_tasks.values() if not t.done()}
            tasks |= {t for t in adapter._background_tasks if not t.done()}
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 20)
            await _drain(runner)

        lines = sorted(s["content"] for s in adapter.jev_sends())
        assert lines == sorted([
            "```\n🔍 Goal Intent: 0.90 Confidence\n```",
            "```\n🔍 Goal Intent: 0.80 Confidence\n```",
        ])
        assert registrations == []  # both notices bypassed the shared registry
        assert adapter._post_delivery_callbacks == {}

    @pytest.mark.asyncio
    async def test_notice_delivers_through_the_owning_profile_scope(
        self, runner, monkeypatch, tmp_path
    ):
        """A/B/A routing with real lifecycle adapters: the notice send runs
        in the OWNING profile's scope and reaches only that profile's
        adapter."""
        from pathlib import Path

        monkeypatch.setenv("LITELLM_API_KEY", "test-key")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from hermes_cli.profiles import create_profile

        home_b = create_profile("beta", no_alias=True, no_skills=True)
        monkeypatch.setattr(runner.config, "multiplex_profiles", True, raising=False)
        adapter_default = _RecordingAdapter()
        adapter_b = _RecordingAdapter()
        runner.adapters = {Platform.DISCORD: adapter_default}
        runner._profile_adapters = {"beta": {Platform.DISCORD: adapter_b}}

        source_b = _source(chat_id="chat-b", user_name="B-User", profile="beta")
        with patch(
            "hermes_cli.goal_intent.requests.post", return_value=_jev_response("task", 0.9)
        ):
            runner._schedule_auto_goal_intent(_event("fix b", source=source_b), SESSION_KEY)
            await _drain(runner)
            await _poll_until(lambda: adapter_b.jev_sends())

        sends = adapter_b.jev_sends()
        assert [s["content"] for s in sends] == [
            "```\n🔍 Goal Intent: 0.90 Confidence\n```"
        ]
        assert sends[0]["home"] == str(home_b)  # owning profile's scope at the send
        assert adapter_default.jev_sends() == []
