"""Post-turn retrospective goal-intent classification and recovery contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.jev_notice import go_formatter_binary
from gateway.session import SessionSource

SESSION_KEY = "agent:main:discord:dm:1001"


def _source(*, bot: bool = False) -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
        user_name="operator",
        is_bot=bot,
    )


def _event(
    text: str = "fix the proxy", *, internal: bool = False, bot: bool = False
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(bot=bot),
        internal=internal,
    )


def _verdict(
    label: str, confidence: float, *, recover: bool = False, error: str = ""
) -> dict:
    comparison = "unavailable" if error else "changed"
    notice = (
        f"```\n🔍 Retrospective Intent: failed ({error})\n```"
        if error
        else f"```\n🔍 Retrospective Intent: {label.title()} ({confidence:.2f})\n```"
    )
    return {
        "label": label,
        "confidence": confidence,
        "comparison": comparison,
        "recover": recover,
        "notice": notice,
        "error": error,
    }


@pytest.mark.skipif(
    go_formatter_binary() is None, reason="retrospective bridge needs the Go binary"
)
def test_real_go_bridge_fails_closed_without_a_credential(monkeypatch):
    from gateway.jev_retrospective import classify_retrospective

    monkeypatch.setattr("hermes_cli.goal_intent._litellm_credential", lambda: "")
    result = classify_retrospective(
        "fix the proxy",
        "I only explained DNS.",
        initial_label="chat",
        initial_confidence=0.4,
    )

    assert result["label"] == "unknown"
    assert result["recover"] is False
    assert result["error"] == "missing_credential"
    assert "failed (missing_credential)" in result["notice"]


@pytest.mark.parametrize(
    "payload, initial_label",
    [
        (_verdict("unknown", 0, error="http_500 private detail"), "chat"),
        (_verdict("task", 0.99, recover=True), "task"),
    ],
)
def test_bridge_rejects_unsafe_errors_and_forbidden_recovery(
    monkeypatch, payload, initial_label
):
    from gateway.jev_retrospective import classify_retrospective

    monkeypatch.setattr(
        "gateway.jev_notice.go_formatter_binary", lambda: "/fake/jevnotice"
    )
    monkeypatch.setattr(
        "hermes_cli.goal_intent._litellm_credential", lambda: "scoped-key"
    )
    monkeypatch.setattr(
        "gateway.jev_retrospective.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload)
        ),
    )

    result = classify_retrospective("message", "response", initial_label=initial_label)

    assert result["error"] == "invalid_bridge_result"
    assert result["recover"] is False
    assert (
        result["notice"]
        == "```\n🔍 Retrospective Intent: failed (invalid_bridge_result)\n```"
    )


@pytest.mark.parametrize("error", ["bridge_unavailable", "bridge_exit", "bridge_error"])
def test_local_bridge_failures_are_visible_and_fail_closed(monkeypatch, error):
    from gateway.jev_retrospective import classify_retrospective

    monkeypatch.setattr(
        "gateway.jev_notice.go_formatter_binary",
        lambda: None if error == "bridge_unavailable" else "/fake/jevnotice",
    )
    monkeypatch.setattr(
        "hermes_cli.goal_intent._litellm_credential", lambda: "scoped-key"
    )
    if error == "bridge_exit":
        monkeypatch.setattr(
            "gateway.jev_retrospective.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout=""),
        )
    elif error == "bridge_error":
        monkeypatch.setattr(
            "gateway.jev_retrospective.subprocess.run",
            Mock(side_effect=TimeoutError("timed out")),
        )

    result = classify_retrospective("message", "response", initial_label="chat")

    assert result["label"] == "unknown"
    assert result["recover"] is False
    assert result["error"] == error
    assert (
        result["notice"] == f"```\n🔍 Retrospective Intent: failed ({error})\n```"
    )


def test_bridge_uses_each_owning_profile_credential_across_switches(
    tmp_path, monkeypatch
):
    from agent import secret_scope
    from gateway.jev_retrospective import classify_retrospective
    from gateway.run import _profile_runtime_scope

    homes = {}
    for name, key in [("a", "key-a"), ("b", "key-b")]:
        home = tmp_path / name
        home.mkdir()
        (home / ".env").write_text(f"LITELLM_API_KEY={key}\n", encoding="utf-8")
        homes[name] = home
    monkeypatch.setenv("LITELLM_API_KEY", "default-key")
    monkeypatch.setattr(
        "gateway.jev_notice.go_formatter_binary", lambda: "/fake/jevnotice"
    )
    seen = []

    def _run(*args, **kwargs):
        seen.append(json.loads(kwargs["input"])["credential"])
        return SimpleNamespace(returncode=0, stdout=json.dumps(_verdict("chat", 0.9)))

    monkeypatch.setattr("gateway.jev_retrospective.subprocess.run", _run)
    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        for name in ["a", "b", "a"]:
            with _profile_runtime_scope(homes[name]):
                result = classify_retrospective("message", "response")
            assert result["label"] == "chat"
    finally:
        secret_scope.set_multiplex_active(previous)

    assert seen == ["key-a", "key-b", "key-a"]


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
    monkeypatch.setattr(
        instance, "_session_key_for_source", lambda _source: SESSION_KEY
    )
    yield instance
    executor = getattr(instance, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


def _goal_state(session_id: str):
    from hermes_cli.goals import GoalManager

    return GoalManager(session_id=session_id).state


def _audit_rows(home) -> list[dict]:
    path = home / "goals" / "intent_shadow.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _run_turn(
    runner,
    *,
    initial_label: str = "chat",
    initial_confidence: float = 0.9,
    retrospective: dict | None = None,
    final_response: str = "I only explained the likely cause.",
    event: MessageEvent | None = None,
    verdict: Mock | None = None,
) -> dict:
    """Drive the real turn boundary; stub only the retrospective Go bridge and judge."""
    from hermes_cli.goals import GoalContract, GoalManager

    event = event or _event()
    source = event.source
    entry = await runner.async_session_store.get_or_create_session(
        source, touch_activity=False
    )
    initial = {
        "label": initial_label,
        "confidence": initial_confidence,
        "contract_draft": None,
        "message": event.text,
        "session_id": entry.session_id,
        "last_assistant": "",
    }
    future = asyncio.get_running_loop().create_future()
    future.set_result(initial)
    runner._auto_goal_pending = {SESSION_KEY: future}

    verdict = verdict or Mock(return_value=retrospective or _verdict("chat", 0.91))
    bridge = SimpleNamespace(classify_retrospective=verdict)
    judge_calls = []
    deferred = AsyncMock()

    def _judge(self, response, **kwargs):
        judge_calls.append((self.state.goal if self.state else None, response))
        return {"should_continue": False, "message": ""}

    with (
        patch.dict(sys.modules, {"gateway.jev_retrospective": bridge}),
        patch(
            "hermes_cli.goals.draft_contract",
            return_value=GoalContract(outcome="proxy fixed"),
        ),
        patch.object(GoalManager, "evaluate_after_turn", _judge),
        patch.object(runner, "_defer_goal_status_notice_after_delivery", deferred),
        patch.object(runner, "_post_turn_loop_completion", AsyncMock()),
    ):
        await runner._run_post_turn_hooks(
            agent_result={"final_response": final_response},
            source=source,
            is_internal=bool(event.internal),
            event=event,
        )
    return {
        "entry": entry,
        "verdict": verdict,
        "judge_calls": judge_calls,
        "deferred": deferred,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_label", ["chat", "task", "approval", "chase"])
async def test_every_initial_label_gets_visible_retrospective_and_audit(
    runner, hermes_home, initial_label
):
    retrospective = _verdict(initial_label, 0.88)
    result = await _run_turn(
        runner,
        initial_label=initial_label,
        initial_confidence=0.87,
        retrospective=retrospective,
    )

    call = result["verdict"].call_args
    assert call.args[:2] == ("fix the proxy", "I only explained the likely cause.")
    assert call.kwargs["initial_label"] == initial_label
    assert call.kwargs["initial_confidence"] == 0.87
    notices = [
        call
        for call in result["deferred"].await_args_list
        if "Retrospective Intent" in call.args[1]
    ]
    assert len(notices) == 1
    assert notices[0].kwargs["non_conversational"] is True
    rows = _audit_rows(hermes_home)
    assert len(rows) == 1
    assert rows[0]["phase"] == "retrospective"
    assert rows[0]["initial_label"] == initial_label
    assert rows[0]["initial_confidence"] == 0.87
    assert rows[0]["label"] == initial_label
    assert rows[0]["confidence"] == 0.88
    assert rows[0]["comparison"] == "changed"


@pytest.mark.asyncio
async def test_high_confidence_task_recovers_goal_and_judges_same_response(runner):
    result = await _run_turn(
        runner,
        initial_label="chat",
        retrospective=_verdict("task", 0.93, recover=True),
    )

    state = _goal_state(result["entry"].session_id)
    assert state is not None and state.status == "active"
    assert state.goal == "fix the proxy"
    assert state.subgoals == []
    assert result["judge_calls"] == [
        ("fix the proxy", "I only explained the likely cause.")
    ]


@pytest.mark.asyncio
async def test_unknown_initial_decision_can_be_recovered(runner):
    result = await _run_turn(
        runner,
        initial_label="unknown",
        initial_confidence=0.0,
        retrospective=_verdict("task", 0.9, recover=True),
    )

    state = _goal_state(result["entry"].session_id)
    assert state is not None and state.goal == "fix the proxy"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "paused", "done"])
async def test_recovery_never_modifies_an_existing_goal(runner, status):
    event = _event()
    entry = await runner.async_session_store.get_or_create_session(
        event.source, touch_activity=False
    )
    from hermes_cli.goals import GoalManager

    manager = GoalManager(session_id=entry.session_id)
    manager.set("original goal")
    manager.add_subgoal("original criterion")
    assert manager.state is not None
    manager.state.status = status
    manager._save()

    result = await _run_turn(
        runner,
        event=event,
        initial_label="chat",
        retrospective=_verdict("task", 0.99, recover=True),
    )

    state = _goal_state(entry.session_id)
    assert state.goal == "original goal"
    assert state.status == status
    assert state.subgoals == ["original criterion"]
    assert result["verdict"].call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retrospective",
    [
        _verdict("chat", 0.99),
        _verdict("approval", 0.99),
        _verdict("chase", 0.99),
        _verdict("task", 0.59),
        _verdict("unknown", 0.0, error="http_500"),
    ],
)
async def test_non_task_low_confidence_and_failure_never_start_goal(
    runner, retrospective
):
    result = await _run_turn(runner, retrospective=retrospective)

    assert _goal_state(result["entry"].session_id) is None
    assert result["judge_calls"] == []
    assert result["verdict"].call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_label", ["task", "approval", "chase"])
async def test_recovery_cannot_replace_an_already_handled_initial_decision(
    runner, initial_label
):
    result = await _run_turn(
        runner,
        initial_label=initial_label,
        retrospective=_verdict("task", 0.99, recover=True),
    )

    state = _goal_state(result["entry"].session_id)
    if initial_label == "chase":
        assert state is None
    else:
        assert state.goal == "fix the proxy"
        assert state.subgoals == []
    assert result["verdict"].call_count == 1


@pytest.mark.asyncio
async def test_final_response_is_required(runner, hermes_home):
    verdict = Mock(return_value=_verdict("task", 0.99, recover=True))
    result = await _run_turn(runner, final_response="", verdict=verdict)

    verdict.assert_not_called()
    assert result["deferred"].await_count == 0
    assert _audit_rows(hermes_home) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _event("🔍 Retrospective Intent: Task (0.99)", internal=True),
        _event("🔍 Retrospective Intent: Task (0.99)", bot=True),
        _event("/goal status"),
        _event("just answer this one"),
    ],
)
async def test_internal_bot_slash_and_suppressed_messages_do_not_self_classify(
    runner, event
):
    verdict = Mock(return_value=_verdict("task", 0.99, recover=True))
    result = await _run_turn(runner, event=event, verdict=verdict)

    verdict.assert_not_called()
    assert not [
        call
        for call in result["deferred"].await_args_list
        if "Retrospective Intent" in call.args[1]
    ]


@pytest.mark.asyncio
async def test_retrospective_config_opt_out_preserves_initial_decision(
    runner, hermes_home
):
    (hermes_home / "config.yaml").write_text(
        "goals:\n  auto:\n    retrospective:\n      enabled: false\n",
        encoding="utf-8",
    )
    verdict = Mock(return_value=_verdict("task", 0.99, recover=True))
    result = await _run_turn(runner, verdict=verdict)

    verdict.assert_not_called()
    assert _goal_state(result["entry"].session_id) is None


@pytest.mark.asyncio
async def test_arrival_notice_opt_out_does_not_hide_retrospective_status(
    runner, hermes_home
):
    (hermes_home / "config.yaml").write_text(
        "display:\n  jev_notices: false\n", encoding="utf-8"
    )

    result = await _run_turn(runner, retrospective=_verdict("chat", 0.9))

    assert any(
        "Retrospective Intent: Chat" in call.args[1]
        for call in result["deferred"].await_args_list
    )


@pytest.mark.asyncio
async def test_failure_is_visible_audited_and_inert_with_arrival_notices_off(
    runner, hermes_home
):
    (hermes_home / "config.yaml").write_text(
        "display:\n  jev_notices: false\n", encoding="utf-8"
    )

    result = await _run_turn(
        runner, retrospective=_verdict("unknown", 0.0, error="bridge_exit")
    )

    assert _goal_state(result["entry"].session_id) is None
    notices = [
        call
        for call in result["deferred"].await_args_list
        if "Retrospective Intent" in call.args[1]
    ]
    assert len(notices) == 1
    assert "failed (bridge_exit)" in notices[0].args[1]
    assert notices[0].kwargs["non_conversational"] is True
    rows = _audit_rows(hermes_home)
    assert len(rows) == 1
    assert rows[0]["comparison"] == "unavailable"
    assert rows[0]["reason"] == "bridge_exit"
    assert rows[0]["recovery_requested"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("registered_callback", [False, True])
async def test_deferred_retrospective_notice_is_non_conversational_interim(
    runner, registered_callback
):
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=True)), _active_sessions={}
    )
    callbacks = {}
    if registered_callback:
        adapter.register_post_delivery_callback = (
            lambda session_key, callback, *, generation=None: callbacks.setdefault(
                session_key, callback
            )
        )
    runner.adapters = {Platform.DISCORD: adapter}

    await runner._defer_goal_status_notice_after_delivery(
        _source(), "🔍 Retrospective Intent: Chat (0.90)", non_conversational=True
    )

    if registered_callback:
        adapter.send.assert_not_awaited()
        await callbacks[SESSION_KEY]()
    metadata = adapter.send.await_args.kwargs["metadata"]
    assert metadata["non_conversational_history"] is True
    assert metadata["_interim_send"] is True
