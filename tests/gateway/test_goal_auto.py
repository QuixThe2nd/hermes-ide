"""Behavior tests for goals.auto.mode=on: Jev decisions drive the goal loop.

Covers the auto-goal contract end to end at the gateway layer: task creates a
goal with exactly one 🎯 line, task-while-active appends, approval resumes,
chase reopens recent work, suppression phrases pause/opt out, group threads
gate non-owners, and shadow/off never mutate goals or call Jev.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

SESSION_KEY = "agent:main:telegram:dm:1001"
SID = "sid-auto-goal"


def _source(*, user_id: str = "user-1", chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type=chat_type,
        user_id=user_id,
        user_name="operator",
    )


def _event(text: str, *, source: SessionSource | None = None) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source or _source(),
    )


def _session_entry(*, origin_user: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(session_id=SID, origin=SimpleNamespace(user_id=origin_user))


def _decision(
    label: str,
    message: str,
    *,
    confidence: float = 0.9,
    last_assistant: str = "",
    contract_draft: dict | None = None,
) -> dict:
    return {
        "label": label,
        "reason": "jev decision",
        "confidence": confidence,
        "contract_draft": contract_draft,
        "message": message,
        "session_id": SID,
        "last_assistant": last_assistant,
        "user_previous_message": "",
        "record": {"label": label, "confidence": confidence},
    }


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture
def runner(hermes_home, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    (hermes_home / "config.yaml").write_text(
        "goals:\n  auto:\n    mode: on\n", encoding="utf-8"
    )
    instance = gateway_run.GatewayRunner(
        GatewayConfig(sessions_dir=hermes_home / "sessions")
    )
    monkeypatch.setattr(instance, "_is_user_authorized", lambda _source: True)
    monkeypatch.setattr(instance, "_session_key_for_source", lambda _source: SESSION_KEY)
    yield instance
    executor = getattr(instance, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


async def _drain(runner) -> None:
    tasks = [task for task in runner._background_tasks if not task.done()]
    if tasks:
        await asyncio.gather(*tasks)


def _goal_state():
    from hermes_cli.goals import GoalManager

    return GoalManager(session_id=SID).state


# ── task ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_creates_goal_with_exactly_one_target_line(runner):
    """Task + confidence creates one active goal; the turn boundary (integration
    test below) sends the single 🎯 notice through the deferred status lane."""
    notice = await runner._apply_auto_goal_decision(
        _decision("task", "why is tdarr down"), _session_entry(), _source()
    )

    assert notice == "🎯 Done when: why is tdarr down"
    state = _goal_state()
    assert state is not None and state.status == "active"
    assert state.goal == "why is tdarr down"


@pytest.mark.asyncio
async def test_task_done_when_line_overrides_outcome_verbatim(runner):
    message = "fix the cert\nDone when: cert valid AND reloaded without restart"
    notice = await runner._apply_auto_goal_decision(
        _decision("task", message), _session_entry(), _source()
    )

    assert notice == "🎯 Done when: cert valid AND reloaded without restart"
    state = _goal_state()
    assert state is not None and state.has_contract()
    assert state.contract.outcome == "cert valid AND reloaded without restart"


@pytest.mark.asyncio
async def test_task_while_active_appends_instead_of_second_goal(runner):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id=SID)
    mgr.set("original goal")

    notice = await runner._apply_auto_goal_decision(
        _decision("task", "also remove the stale route"), _session_entry(), _source()
    )

    assert notice is None  # no second 🎯 for an already-active goal
    state = _goal_state()
    assert state.goal == "original goal"
    assert "also remove the stale route" in state.subgoals


@pytest.mark.asyncio
@pytest.mark.parametrize("label,confidence", [("task", 0.59), ("chat", 0.99), ("unknown", 0.99)])
async def test_low_confidence_or_chat_or_unknown_never_creates_goal(
    runner, label, confidence
):
    notice = await runner._apply_auto_goal_decision(
        _decision(label, "some message", confidence=confidence), _session_entry(), _source()
    )

    assert notice is None
    assert _goal_state() is None


# ── approval / chase ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_resumes_paused_goal_without_papercut(runner):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id=SID)
    mgr.set("ship the release")
    mgr.pause(reason="budget")

    notice = await runner._apply_auto_goal_decision(
        _decision("approval", "yeah go ahead"), _session_entry(), _source()
    )

    assert notice is None
    state = _goal_state()
    assert state is not None and state.status == "active"
    assert "yeah go ahead" not in state.subgoals  # approval is a resume, not a subgoal


@pytest.mark.asyncio
async def test_approval_with_no_paused_goal_falls_back_to_task(runner):
    notice = await runner._apply_auto_goal_decision(
        _decision("approval", "deploy it"), _session_entry(), _source()
    )

    assert notice == "🎯 Done when: deploy it"
    assert _goal_state() is not None and _goal_state().status == "active"


@pytest.mark.asyncio
async def test_chase_reopens_recently_done_goal_with_not_done_note(runner):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id=SID)
    mgr.set("fix the proxy")
    state = mgr.state
    state.status = "done"
    state.last_turn_at = time.time()
    mgr._save()

    notice = await runner._apply_auto_goal_decision(
        _decision("chase", "again, it didnt work"), _session_entry(), _source()
    )

    assert notice is None
    state = _goal_state()
    assert state is not None and state.status == "active"  # reopened, fresh budget
    assert state.turns_used == 0
    assert "user says not done: again, it didnt work" in state.subgoals


@pytest.mark.asyncio
async def test_chase_after_window_starts_goal_from_chase_text_and_last_reply(runner):
    notice = await runner._apply_auto_goal_decision(
        _decision("chase", "status?", last_assistant="Still working on the proxy fix."),
        _session_entry(),
        _source(),
    )

    assert notice is None  # chase starts never send a 🎯 line
    state = _goal_state()
    assert state is not None and state.status == "active"
    assert state.goal.startswith("status?")
    assert "Still working on the proxy fix." in state.goal


# ── suppression ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_pauses_active_goal_and_makes_no_jev_call(runner):
    from hermes_cli.goals import GoalManager

    entry = await runner.async_session_store.get_or_create_session(_source(), touch_activity=False)
    GoalManager(session_id=entry.session_id).set("long running job")

    with patch("hermes_cli.goal_intent.requests.post") as post:
        runner._schedule_goal_intent_shadow(_event("stop"), SESSION_KEY)
        await _drain(runner)

    post.assert_not_called()
    assert not getattr(runner, "_auto_goal_pending", {})
    state = GoalManager(session_id=entry.session_id).state
    assert state is not None and state.status == "paused"


@pytest.mark.asyncio
async def test_just_answer_suppresses_classification_and_goal(runner):
    with patch("hermes_cli.goal_intent.requests.post") as post:
        runner._schedule_goal_intent_shadow(_event("just answer this one"), SESSION_KEY)
        await _drain(runner)

    post.assert_not_called()
    assert not getattr(runner, "_auto_goal_pending", {})
    assert _goal_state() is None


# ── group authorization ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_owner_in_group_thread_cannot_start_goal(runner):
    """Group/channel: only the session owner (origin user) or an operator starts goals."""
    someone_else = _source(user_id="user-2", chat_type="group")

    notice = await runner._apply_auto_goal_decision(
        _decision("task", "give me the certificate"), _session_entry(origin_user="user-1"), someone_else
    )

    assert notice is None
    assert _goal_state() is None


@pytest.mark.asyncio
async def test_session_owner_in_group_thread_can_start_goal(runner):
    owner = _source(user_id="user-1", chat_type="group")

    notice = await runner._apply_auto_goal_decision(
        _decision("task", "give me the certificate"), _session_entry(origin_user="user-1"), owner
    )

    assert notice == "🎯 Done when: give me the certificate"
    assert _goal_state() is not None


# ── failure and mode gates ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jev_http_error_fails_closed_no_goal(runner, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from hermes_cli.goal_intent import evaluate_goal_intent

    with patch(
        "hermes_cli.goal_intent.requests.post",
        return_value=SimpleNamespace(status_code=503, json=lambda: {}),
    ) as post:
        decision = evaluate_goal_intent("why is tdarr down", SID, "telegram")

    assert post.call_count == 2  # one retry, then fail closed
    assert decision["label"] == "unknown"
    assert decision["confidence"] == 0.0

    notice = await runner._apply_auto_goal_decision(decision, _session_entry(), _source())
    assert notice is None
    assert _goal_state() is None


@pytest.mark.asyncio
async def test_off_mode_makes_no_http_call(runner, monkeypatch):
    import gateway.run as gateway_run

    home = gateway_run._hermes_home
    (home / "config.yaml").write_text("goals:\n  auto:\n    mode: off\n", encoding="utf-8")

    with patch("hermes_cli.goal_intent.requests.post") as post, patch(
        "hermes_cli.goal_intent.evaluate_goal_intent"
    ) as evaluate:
        runner._schedule_goal_intent_shadow(_event("why is tdarr down?"), SESSION_KEY)
        await _drain(runner)

    post.assert_not_called()
    evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_mode_never_parks_a_decision_or_starts_a_goal(runner, monkeypatch):
    import gateway.run as gateway_run
    from pathlib import Path
    from hermes_cli.goal_intent import record_goal_intent_shadow

    home = gateway_run._hermes_home
    (home / "config.yaml").write_text("goals:\n  auto:\n    mode: shadow\n", encoding="utf-8")

    with patch(
        "hermes_cli.goal_intent.record_goal_intent_shadow",
        wraps=record_goal_intent_shadow,
    ) as record:
        runner._schedule_goal_intent_shadow(_event("why is tdarr down?"), SESSION_KEY)
        await _drain(runner)

    record.assert_called_once()
    assert not getattr(runner, "_auto_goal_pending", {})
    assert _goal_state() is None


# ── turn-boundary integration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_decision_applies_before_judge_at_turn_boundary(runner):
    """The parked decision is applied inside the turn boundary, BEFORE the judge
    runs — the judge sees the goal minted by this turn's message."""
    from hermes_cli import goals


    judge_calls = []

    def fake_evaluate(self, response, **kwargs):
        judge_calls.append(self.state.goal if self.state else None)
        return {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "continue: keep going on the goal",
            "message": "",
        }

    with (
        patch.object(goals.GoalManager, "evaluate_after_turn", fake_evaluate),
        patch.object(runner, "_defer_goal_status_notice_after_delivery", new=AsyncMock()) as defer,
        patch.object(runner, "_warm_goals_session_db", new=AsyncMock()),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=_session_entry(),
            source=_source(),
            final_response="I started looking at tdarr.",
            auto_decision=_decision("task", "why is tdarr down"),
        )

    assert judge_calls == ["why is tdarr down"]  # judge ran AFTER the apply
    assert defer.await_count == 1  # exactly one 🎯 line; the judge message was empty
    assert defer.await_args.args[1] == "🎯 Done when: why is tdarr down"
    state = _goal_state()
    assert state is not None and state.status == "active"


@pytest.mark.asyncio
async def test_empty_final_text_drops_pending_decision(runner):
    """An interrupted/errored turn (empty final text) must not mint a goal:
    _run_post_turn_hooks pops the decision before the final-text gate."""

    class _NoMessagesStore:
        async def get_or_create_session(self, source, **kwargs):
            return _session_entry()

    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=_NoMessagesStore().get_or_create_session,
    )
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    future.set_result(_decision("task", "why is tdarr down"))
    runner._auto_goal_pending = {SESSION_KEY: future}

    with (
        patch.object(runner, "_post_turn_goal_continuation", new=AsyncMock()) as continuation,
        patch.object(runner, "_post_turn_loop_completion", new=AsyncMock()),
    ):
        await runner._run_post_turn_hooks(
            agent_result={"final_response": ""},
            source=_source(),
            is_internal=False,
        )

    continuation.assert_not_awaited()
    assert SESSION_KEY not in runner._auto_goal_pending  # popped, not deferred
    assert _goal_state() is None
