"""Responses-wire ``status=incomplete`` continuation must change the request when reasoning
consumed the whole output budget (#90393): a retry with the same ``max_output_tokens`` and the
same effort re-burns the budget identically and the turn can never converge. A reasoning-only
``status=completed`` response (Codex "still thinking") keeps today's bare-retry behaviour."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.turn_truncation import continue_codex_incomplete


def _agent(max_tokens=2000):
    agent = MagicMock()
    agent.max_tokens = max_tokens
    agent.quiet_mode = True
    agent.log_prefix = ""
    agent._codex_incomplete_retries = 0
    agent._ephemeral_reasoning_off = False
    agent._ephemeral_max_output_tokens = None
    agent._build_assistant_message.side_effect = lambda msg, fr: {
        "role": "assistant", "content": msg.content or "", "finish_reason": fr,
        "codex_reasoning_items": msg.codex_reasoning_items,
    }
    agent._interim_assistant_visible_text.return_value = ""
    return agent


def _reasoning_only_message():
    return SimpleNamespace(
        content="", tool_calls=None, reasoning=None,
        codex_reasoning_items=[{"type": "reasoning", "id": "rs_1", "encrypted_content": "x"}],
    )


def _run(agent, response):
    messages = [{"role": "user", "content": "write an essay"}]
    return continue_codex_incomplete(
        agent, _reasoning_only_message(), "incomplete", messages=messages,
        conversation_history=None, api_call_count=1, response=response,
    )


def test_budget_exhausted_empty_fragment_raises_cap_and_drops_reasoning():
    agent = _agent()
    exhausted = SimpleNamespace(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"},
        usage=SimpleNamespace(output_tokens=2000, output_tokens_details={"reasoning_tokens": 1997}),
    )
    assert _run(agent, exhausted) is None
    assert agent._ephemeral_reasoning_off is True
    first_boost = agent._ephemeral_max_output_tokens
    assert first_boost > 2000
    agent._ephemeral_max_output_tokens = None  # request builder consumes it
    assert _run(agent, exhausted) is None
    assert agent._ephemeral_max_output_tokens > first_boost


def test_reasoning_only_completed_response_keeps_bare_retry():
    agent = _agent()
    still_thinking = SimpleNamespace(status="completed", incomplete_details=None)
    assert _run(agent, still_thinking) is None
    assert agent._ephemeral_reasoning_off is False
    assert agent._ephemeral_max_output_tokens is None
