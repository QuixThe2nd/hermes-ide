"""Fire-site coverage for the ``pre_turn_end`` end-of-turn gate.

Mirrors ``tests/run_agent/test_verification_continuation_budget.py`` (the
pre_verify loop tests): a registered hook keeps the conversation loop going
with a synthetic user nudge, the budget bounds it to one directive per turn,
and the nudge never survives into the returned message history.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _response(content="composed report"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="pre-turn-end-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def test_registered_hook_keeps_loop_going(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter([_response("first answer"), _response("second answer")])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    calls = []

    def fake_get(**kwargs):
        calls.append(kwargs)
        return "consider start_conversation" if len(calls) == 1 else None

    with (
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: name == "pre_turn_end"),
        patch(
            "hermes_cli.plugins.get_pre_turn_end_continue_message",
            side_effect=fake_get,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("what's new?")

    # The loop continued: the second model answer is the final response.
    assert result["final_response"] == "second answer"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    agent._handle_max_iterations.assert_not_called()
    # Budget: one directive per turn (agent.max_pre_turn_end_nudges default 1)
    # — the dispatcher is not even consulted for a second attempt.
    assert len(calls) == 1
    assert agent._pre_turn_end_nudges == 1
    assert calls[0]["last_user_text"] == "what's new?"
    # The synthetic nudge is stripped from the returned history.
    assert all(
        not m.get("_pre_turn_end_synthetic") for m in result["messages"]
    )


def test_budget_exhaustion_preserves_composed_report(agent, monkeypatch):
    agent._interruptible_api_call = lambda _kwargs: _response()
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: name == "pre_turn_end"),
        patch(
            "hermes_cli.plugins.get_pre_turn_end_continue_message",
            return_value="consider start_conversation",
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("hello")

    # Budget 1/turn + iteration budget 1: the nudged continuation runs out of
    # iterations and the pending candidate is preserved, not replaced.
    assert result["final_response"] == "composed report"
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["completed"] is False
    assert agent._handle_max_iterations.call_count == 0
    # The nudge is stripped, so the role sequence is [user, assistant].
    assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
    assert not result["messages"][1].get("_pre_turn_end_synthetic")


def test_no_hook_subscription_costs_nothing(agent, monkeypatch):
    agent._interruptible_api_call = lambda _kwargs: _response()
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch(
            "hermes_cli.plugins.get_pre_turn_end_continue_message",
            side_effect=AssertionError("dispatcher must not run when nothing subscribes"),
        ),
    ):
        result = agent.run_conversation("hello")

    assert result["final_response"] == "composed report"
    assert result["completed"] is True
    assert getattr(agent, "_pre_turn_end_nudges", 0) == 0


def test_counter_resets_each_turn(agent, monkeypatch):
    agent.max_iterations = 1
    agent.iteration_budget.max_total = 1
    agent._interruptible_api_call = lambda _kwargs: _response()
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: name == "pre_turn_end"),
        patch(
            "hermes_cli.plugins.get_pre_turn_end_continue_message",
            return_value="consider start_conversation",
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        agent.run_conversation("first turn")
        # A stale counter from turn one must not silence turn two's gate.
        assert agent._pre_turn_end_nudges == 1
        agent.run_conversation("second turn")
        assert agent._pre_turn_end_nudges == 1
