"""Codex Responses reasoning-only stall recovery (#67321).

Encrypted reasoning items replay byte-for-byte, so a bare continuation of a
reasoning-only ``status=incomplete`` response repeats the stall. After three
consecutive reasoning-only responses the turn must reach the configured
fallback provider (with one bounded grace call when the trigger consumed the
iteration budget) instead of ending on the internal incomplete sentinel; a
visible partial resets the local streak; a cross-protocol fallback drops the
Codex-only nudge from the wire.
"""

from __future__ import annotations

from types import SimpleNamespace

import run_agent
from agent.conversation_loop import _CODEX_INCOMPLETE_NUDGE
from agent.error_classifier import FailoverReason
from tests.agent.test_run_agent_codex_responses import (
    _build_agent,
    _codex_incomplete_message_response,
    _codex_message_response,
    _codex_reasoning_only_response,
)


def _spy_fallback(agent, monkeypatch):
    """Record fallback activations; keep ``api_mode`` on codex_responses so the
    stub fallback answer still parses through the Codex path."""
    calls = []

    def _fake(reason=None):
        calls.append(reason)
        return True

    monkeypatch.setattr(agent, "_try_activate_fallback", _fake)
    return calls


def _drive(agent, monkeypatch, responses):
    api_calls = {"n": 0}

    def _fake_api_call(api_kwargs):
        api_calls["n"] += 1
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api_call)
    return api_calls


def test_reasoning_only_streak_reaches_fallback_with_one_grace_call(monkeypatch):
    agent = _build_agent(monkeypatch)
    agent.max_iterations = 3
    agent.iteration_budget = run_agent.IterationBudget(3)
    calls = _spy_fallback(agent, monkeypatch)
    api_calls = _drive(agent, monkeypatch, [
        _codex_reasoning_only_response(encrypted_content="enc_a"),
        _codex_reasoning_only_response(encrypted_content="enc_b"),
        _codex_reasoning_only_response(encrypted_content="enc_c"),
        _codex_message_response("Fallback answered."),
    ])

    result = agent.run_conversation("keep thinking")

    assert result["completed"] is True
    assert result["final_response"] == "Fallback answered."
    assert calls == [FailoverReason.incomplete_response]
    # Three budgeted calls + exactly one grace call; the grace flag is consumed.
    assert api_calls["n"] == 4
    assert agent._budget_grace_call is False


def test_visible_partial_resets_reasoning_only_streak(monkeypatch):
    agent = _build_agent(monkeypatch)
    agent.max_iterations = 6
    agent.iteration_budget = run_agent.IterationBudget(6)
    calls = _spy_fallback(agent, monkeypatch)
    _drive(agent, monkeypatch, [
        _codex_incomplete_message_response("Partial visible progress."),
        _codex_reasoning_only_response(encrypted_content="enc_a"),
        _codex_reasoning_only_response(encrypted_content="enc_b"),
        _codex_reasoning_only_response(encrypted_content="enc_c"),
        _codex_message_response("Recovered."),
    ])

    result = agent.run_conversation("partial then stall")

    assert result["completed"] is True
    assert result["final_response"] == "Recovered."
    assert calls == [FailoverReason.incomplete_response]


def test_cross_protocol_fallback_wire_drops_codex_nudge_and_replay_state(monkeypatch):
    """The nudge and encrypted reasoning are Codex-only: once the stall falls over to a
    Chat Completions provider the assembled request must carry neither, with roles alternating."""
    agent = _build_agent(monkeypatch)
    agent.max_iterations = 6
    agent.iteration_budget = run_agent.IterationBudget(6)

    def _flip_to_chat(reason=None):
        agent.api_mode = "chat_completions"
        agent._disable_streaming = True  # the stub answer is a plain object, not a stream
        return True

    monkeypatch.setattr(agent, "_try_activate_fallback", _flip_to_chat)
    chat_answer = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Fallback answered.", tool_calls=None),
                                 finish_reason="stop")],
        model="fallback/model", usage=None,
    )
    responses = [
        _codex_reasoning_only_response(encrypted_content="enc_a"),
        _codex_reasoning_only_response(encrypted_content="enc_b"),
        _codex_reasoning_only_response(encrypted_content="enc_c"),
        chat_answer,
    ]
    wires = []

    def _fake_api_call(api_kwargs):
        wires.append((agent.api_mode, api_kwargs))
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api_call)

    result = agent.run_conversation("do it")

    assert result["final_response"] == "Fallback answered."
    assert [mode for mode, _ in wires] == ["codex_responses"] * 3 + ["chat_completions"]
    # The pre-fallback transcript did carry the nudge (replay + nudge before the third stall).
    assert any(m.get("content") == _CODEX_INCOMPLETE_NUDGE for m in agent._session_messages)
    wire = [m for m in wires[-1][1]["messages"] if m["role"] not in ("system", "developer")]
    # Thinking-only rows are dropped and adjacent users merged, so the nudge would
    # survive as a fragment of the merged user row rather than as its own row.
    assert not any(_CODEX_INCOMPLETE_NUDGE in str(m.get("content") or "") for m in wire)
    assert not any(m.get("codex_reasoning_items") for m in wire)
    roles = [m["role"] for m in wire]
    assert roles and all(a != b for a, b in zip(roles, roles[1:]))


def _chat_answer(text="Chat answered."):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None),
                                 finish_reason="stop")],
        model="fallback/model", usage=None,
    )


def test_user_text_identical_to_codex_nudge_survives_normal_wire(monkeypatch):
    """A genuine user message whose text is byte-identical to the Codex nudge literal
    must reach a chat-completions provider untouched. The cross-protocol drop keys on
    the transcript-private ``_codex_incomplete_nudge`` flag, never on content."""
    agent = _build_agent(monkeypatch)
    agent.api_mode = "chat_completions"
    agent._disable_streaming = True  # the stub answer is a plain object, not a stream
    wires = []

    def _fake_api_call(api_kwargs):
        wires.append(api_kwargs)
        return _chat_answer()

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api_call)

    result = agent.run_conversation(_CODEX_INCOMPLETE_NUDGE)

    assert result["completed"] is True
    assert result["final_response"] == "Chat answered."
    wire = [m for m in wires[0]["messages"] if m["role"] not in ("system", "developer")]
    user_rows = [m for m in wire if m["role"] == "user"]
    assert [m["content"] for m in user_rows] == [_CODEX_INCOMPLETE_NUDGE]
    # Untouched means byte-exact and unmerged; no private marker rides the wire.
    assert not any(m.get("_codex_incomplete_nudge") for m in wires[0]["messages"])


def test_user_text_identical_to_codex_nudge_survives_cross_protocol_fallback_wire(monkeypatch):
    """After a reasoning-only stall falls over to a Chat Completions provider, the
    system-produced nudge row is dropped and the genuine user row with the SAME literal
    text survives — exactly one copy on the wire, no opaque replay, roles alternating."""
    agent = _build_agent(monkeypatch)
    agent.max_iterations = 6
    agent.iteration_budget = run_agent.IterationBudget(6)

    def _flip_to_chat(reason=None):
        agent.api_mode = "chat_completions"
        agent._disable_streaming = True  # the stub answer is a plain object, not a stream
        return True

    monkeypatch.setattr(agent, "_try_activate_fallback", _flip_to_chat)
    responses = [
        _codex_reasoning_only_response(encrypted_content="enc_a"),
        _codex_reasoning_only_response(encrypted_content="enc_b"),
        _codex_reasoning_only_response(encrypted_content="enc_c"),
        _chat_answer("Fallback answered."),
    ]
    wires = []

    def _fake_api_call(api_kwargs):
        wires.append((agent.api_mode, api_kwargs))
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api_call)

    result = agent.run_conversation(_CODEX_INCOMPLETE_NUDGE)

    assert result["final_response"] == "Fallback answered."
    assert [mode for mode, _ in wires] == ["codex_responses"] * 3 + ["chat_completions"]
    # The pre-fallback transcript carried BOTH rows: the genuine user turn and the
    # flag-tagged synthetic nudge. Only the flag tells them apart.
    assert any(m.get("_codex_incomplete_nudge") for m in agent._session_messages)
    wire = [m for m in wires[-1][1]["messages"] if m["role"] not in ("system", "developer")]
    literal_rows = [m for m in wire if m.get("content") == _CODEX_INCOMPLETE_NUDGE]
    assert [m["role"] for m in literal_rows] == ["user"]  # synthetic copy absent
    assert not any(m.get("_codex_incomplete_nudge") for m in wires[-1][1]["messages"])
    assert not any(m.get("codex_reasoning_items") for m in wire)
    roles = [m["role"] for m in wire]
    assert roles and all(a != b for a, b in zip(roles, roles[1:]))


def test_flagged_nudge_row_never_serializes_private_marker_to_responses_wire():
    """The Codex Responses converter emits fresh role/content items, so the
    transcript-private flag cannot leak onto the one wire that still carries the nudge."""
    from agent.codex_responses_adapter import _chat_messages_to_responses_input

    items = _chat_messages_to_responses_input([
        {"role": "user", "content": _CODEX_INCOMPLETE_NUDGE, "_codex_incomplete_nudge": True},
    ])

    assert items and items[0].get("content") == _CODEX_INCOMPLETE_NUDGE
    assert not any(
        isinstance(key, str) and key.startswith("_")
        for item in items for key in item
    )
