"""Reasoning promoted on a reasoning-only clean stop is returned, never persisted as a reply.

A clean ``stop`` with empty content and reasoning text is promoted to ``final_response`` (the
vLLM nemotron parser files the whole answer as reasoning; re-running the empty-response ladder
re-bills the prompt). The promoted text must NOT become the assistant row's ordinary ``content``:
chain-of-thought stored as content is indistinguishable from a real reply on every history
surface (#111761). The row keeps ``content`` empty and carries the text as the ``api_content``
sidecar, so the next request still replays the answer byte-identically.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

REASONING = "嗯，长度合适。Let me check the file first."


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-reasoner",
            provider="deepseek",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def _run(agent, responses, user_message="hello", conversation_history=None):
    agent.client.chat.completions.create.side_effect = list(responses)
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(user_message, conversation_history=conversation_history)


def test_promoted_reasoning_is_returned_but_persisted_row_keeps_content_empty(loop_agent):
    from tests.agent.test_run_agent import _mock_response

    result = _run(loop_agent, [_mock_response(content="", finish_reason="stop", reasoning_content=REASONING)])

    # Return contract from the parser-compat fix survives: one call, the reasoning is the answer.
    assert result["final_response"] == REASONING
    assert result["api_calls"] == 1

    row = result["messages"][-1]
    assert row["role"] == "assistant"
    assert not row.get("content")  # never chain-of-thought as an ordinary reply
    assert row["reasoning"] == REASONING
    assert row["api_content"] == REASONING

    # The next request still replays the promoted text as the assistant's own turn.
    _run(loop_agent, [_mock_response(content="Second answer.", finish_reason="stop")],
         user_message="next", conversation_history=result["messages"])
    sent = loop_agent.client.chat.completions.create.call_args.kwargs["messages"]
    assistant_rows = [m for m in sent if m.get("role") == "assistant"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert assistant_rows[0]["content"] == REASONING
    assert "api_content" not in assistant_rows[0]


def test_stall_guard_interim_row_carries_promoted_text_as_sidecar(loop_agent):
    """Promoted reasoning that tails on an announced next action trips the stall guard; the interim
    row it appends must follow the same shape as the final row — ``content`` empty, the promoted
    text in ``api_content`` — so the continuation request replays a real assistant turn, not an
    empty one (#111761)."""
    from tests.agent.test_run_agent import _mock_response

    stalled = "The user wants the file contents. Let me now read the file."
    loop_agent.valid_tool_names = {"read_file"}
    loop_agent._stall_guards = True

    result = _run(loop_agent, [
        _mock_response(content="", finish_reason="stop", reasoning_content=stalled),
        _mock_response(content="Here is the file.", finish_reason="stop"),
    ])

    assert result["final_response"] == "Here is the file."
    interim = [m for m in result["messages"] if m.get("role") == "assistant"][0]
    assert not interim.get("content")
    assert interim["reasoning"] == stalled
    assert interim["api_content"] == stalled

    # The continuation request carried the promoted text as the interim assistant turn.
    second_call = loop_agent.client.chat.completions.create.call_args_list[1].kwargs["messages"]
    interim_wire = [m for m in second_call if m.get("role") == "assistant"][0]
    assert interim_wire["content"] == stalled
    assert "api_content" not in interim_wire


def test_reasoning_only_clean_stop_logs_warning_with_route(loop_agent, caplog):
    from tests.agent.test_run_agent import _mock_response

    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        _run(loop_agent, [_mock_response(content=None, finish_reason="stop", reasoning_content=REASONING)])

    hits = [r for r in caplog.records if r.getMessage().startswith("Reasoning-only clean stop")]
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING
    assert f"model={loop_agent.model}" in hits[0].getMessage()
    assert "provider=deepseek" in hits[0].getMessage()
    assert "tool_turns=0" in hits[0].getMessage()
