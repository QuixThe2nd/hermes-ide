"""Synthetic /goal continuations must carry internal provenance end to end.

Incident (Discord thread 1571721091469287526, Nov 2026): the synthetic
continuation prompt enqueued after each goal turn re-entered the inbound
pipeline as a plain (non-internal) ``MessageEvent``. ``_schedule_auto_goal_intent``
classified it like human text, the Jev classifier read "Continue working toward
this goal" as a chase, and ``_auto_goal_chase`` appended ``user says not done:
<entire continuation prompt>`` as a subgoal. The next continuation prompt
embedded that subgoal, was classified again, and appended itself again —
unbounded recursive self-appending that inflated the live context to 2.89 MB.

These tests pin the provenance contract at every layer that creates or
restores a continuation: the post-turn judge path, the ``/goal resume``
command path, the pause/clear guard's template recognition, and the drain
queue (restart) round-trip. A continuation is machinery, never human intent:
it must not park an auto-goal decision, and a chase label derived from its
text must never append subgoals.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


class _DrainProbeAdapter(BasePlatformAdapter):
    """Minimal concrete adapter with a live pending slot."""

    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.DISCORD)
        self.sent: list[str] = []

    async def start(self):  # pragma: no cover - unused
        pass

    async def stop(self):  # pragma: no cover - unused
        pass

    async def connect(self, *, is_reconnect: bool = False) -> bool:  # pragma: no cover - unused
        return True

    async def disconnect(self) -> None:  # pragma: no cover - unused
        pass

    async def get_chat_info(self, chat_id):  # pragma: no cover - unused
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)

        class _R:
            success = True
            message_id = "m1"

        return _R()

    async def send_typing(self, chat_id, metadata=None):
        pass


def _dm_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="U1",
        chat_id="1571721091469287526",
        user_name="operator",
        chat_type="dm",
    )


def _chase_decision(text: str, sid: str) -> dict:
    """A plausible Jev decision for a continuation prompt: chase, high confidence."""
    return {
        "label": "chase",
        "reason": "user is pushing for more work",
        "confidence": 0.9,
        "contract_draft": None,
        "message": text,
        "session_id": sid,
        "last_assistant": "",
        "user_previous_message": "",
        "record": {"label": "chase", "confidence": 0.9},
    }


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


def _make_runner(sid: str, adapter_key: str) -> tuple:
    """``(runner, adapter, session_entry)`` with the attrs the goal paths touch."""
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")},
    )
    runner._queued_events = {}
    entry = SessionEntry(
        session_key=adapter_key,
        session_id=sid,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = adapter_key
    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    return runner, adapter, entry


def _arm_chase_classifier(runner, sid: str) -> MagicMock:
    """Instance-patch the classifier worker to always return a chase decision."""
    async def _always_chase(text, source, platform):
        return _chase_decision(text, sid)

    mock = MagicMock(side_effect=_always_chase)
    runner._run_auto_goal_evaluation = mock

    def _noop_scope(source):
        return nullcontext()

    runner._profile_scope_for_source = _noop_scope
    runner._retain_background_task = lambda task: task
    return mock


async def _drain_pending(adapter, adapter_key: str):
    """Pop the head-slot event (what the adapter drain would hand the runner)."""
    await asyncio.sleep(0.05)  # let spawned classification tasks park decisions
    return adapter._pending_messages.pop(adapter_key, None)


@pytest.mark.asyncio
async def test_post_turn_continuation_is_internal_and_skips_classification(hermes_home):
    """The judge's continuation must be machinery: internal flag set, and the
    auto-goal gate must not park a decision for it (the incident's entry point)."""
    from hermes_cli.goals import GoalManager

    sid = f"goal-prov-{uuid.uuid4().hex[:8]}"
    src = _dm_source()
    adapter_key = f"agent:main:discord:dm:{sid}"
    runner, adapter, entry = _make_runner(sid, adapter_key)
    classifier = _arm_chase_classifier(runner, sid)

    GoalManager(sid).set("ship the release")
    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "still needs work", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=entry, source=src, final_response="partial progress"
        )
    cont = await _drain_pending(adapter, adapter_key)
    assert cont is not None, "post-turn continuation must be enqueued"

    # Provenance: the synthetic continuation is internal machinery.
    assert cont.internal is True
    assert runner._is_goal_continuation_event(cont)

    # The drained continuation must not be classified as human intent: no
    # pending decision may be parked for it, whatever the classifier would say.
    runner._schedule_auto_goal_intent(cont, adapter_key)
    await asyncio.sleep(0.05)
    assert adapter_key not in (getattr(runner, "_auto_goal_pending", None) or {})
    classifier.assert_not_called()


@pytest.mark.asyncio
async def test_goal_resume_continuation_is_internal_and_skips_classification(hermes_home):
    """/goal resume restarts work through the same synthetic continuation shape —
    it must carry the same internal provenance (second creation path)."""
    from hermes_cli import goals

    sid = f"goal-resume-{uuid.uuid4().hex[:8]}"
    adapter_key = f"agent:main:discord:dm:{sid}"
    runner, adapter, entry = _make_runner(sid, adapter_key)
    _arm_chase_classifier(runner, sid)

    GoalManagerCls = goals.GoalManager
    GoalManagerCls(sid).set("ship the release")
    GoalManagerCls(sid).pause(reason="user-paused")

    resume_event = MessageEvent(
        text="/goal resume",
        message_type=MessageType.TEXT,
        source=_dm_source(),
        message_id="msg-resume",
    )
    await runner._handle_goal_command(resume_event)

    cont = await _drain_pending(adapter, adapter_key)
    assert cont is not None, "/goal resume must enqueue the continuation"
    assert cont.text.startswith("[Continuing toward your standing goal]")
    assert cont.internal is True

    runner._schedule_auto_goal_intent(cont, adapter_key)
    await asyncio.sleep(0.05)
    assert adapter_key not in (getattr(runner, "_auto_goal_pending", None) or {})


@pytest.mark.asyncio
async def test_continuation_loop_never_self_appends_subgoals(hermes_home):
    """The incident's recursion, simulated turn-boundary by turn-boundary:
    a classifier that labels EVERY message chase must not be able to grow the
    goal's subgoal list from the goal loop's own continuations."""
    from hermes_cli.goals import GoalManager

    sid = f"goal-loop-{uuid.uuid4().hex[:8]}"
    src = _dm_source()
    adapter_key = f"agent:main:discord:dm:{sid}"
    runner, adapter, entry = _make_runner(sid, adapter_key)
    _arm_chase_classifier(runner, sid)

    mgr = GoalManager(sid).set("ship the release")

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "still needs work", False, None, False),
    ):
        for _cycle in range(3):
            await runner._post_turn_goal_continuation(
                session_entry=entry, source=src, final_response="partial progress"
            )
            cont = await _drain_pending(adapter, adapter_key)
            assert cont is not None

            # Turn boundary: classification parked at arrival, applied at end.
            runner._schedule_auto_goal_intent(cont, adapter_key)
            await asyncio.sleep(0.05)
            decision = await runner._take_pending_auto_goal_decision(src)
            if decision is not None:
                await runner._apply_auto_goal_decision(decision, entry, src)

            state = GoalManager(sid).state
            assert state is not None
            subgoals = state.subgoals or []
            assert not any(
                "user says not done" in (s or "") for s in subgoals
            ), f"continuation self-appended as a subgoal: {subgoals}"
            assert not subgoals, f"subgoals accumulated: {subgoals}"

    assert mgr is not None


def test_gate_failed_continuation_is_recognized_by_the_continuation_guard():
    """``_is_goal_continuation_event`` drives pause/clear cleanup AND the FIFO
    head-slot rule; it must recognize every continuation template the goal
    manager can emit, including the quality-gate-failed variant."""
    from gateway.run import GatewayRunner
    from hermes_cli.goals import CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE

    gate_failed = CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE.format(
        goal="ship it", command="pytest -q", exit_code=1, attempt=1,
        max_retries=3, output="2 failed",
    )
    assert GatewayRunner._is_goal_continuation_event(gate_failed), (
        "gate-failed continuations must be recognized as synthetic goal turns"
    )


def test_continuation_internal_flag_survives_drain_queue_roundtrip(hermes_home):
    """Queue restoration (restart/drain replay) must not strip provenance: a
    serialized internal continuation deserializes as internal, so a restored
    event can never be classified as human text either."""
    from gateway.run_drain_queue import deserialize_drain_event, serialize_drain_event

    cont = MessageEvent(
        text="[Continuing toward your standing goal]\nGoal: ship it",
        message_type=MessageType.TEXT,
        source=_dm_source(),
        internal=True,
    )
    record = serialize_drain_event("agent:main:discord:dm:C1", cont)
    _key, restored = deserialize_drain_event(record)
    assert restored.internal is True
    assert restored.text.startswith("[Continuing toward your standing goal]")
