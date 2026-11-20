"""Gateway pre/post-run work reaches the footer as setup/save, not ``other`` (live session
20261120_143237: ~0.85s agent build and ~2.0s delivery drain were unattributed)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.runtime_footer import build_footer_line, footer_phase_times
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _drive_run_sync(*, setup_sleep: float, conversation_sleep: float) -> dict:
    runner = MagicMock()
    runner._resolve_session_agent_runtime.return_value = ("m", {})
    agent = SimpleNamespace(context_compressor=None, model="m")
    ctx = TurnContext(source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
                      message="hello", history=[], session_id="sid", session_key="key",
                      user_config={}, _run_still_current=lambda: True)
    turn = TurnRunner(runner, ctx)

    def _resolve_turn_agent(*_a, **_kw):
        time.sleep(setup_sleep)
        return agent, False

    def _conversation(*_a, **_kw):
        time.sleep(conversation_sleep)
        return {"final_response": "ok", "messages": [], "api_calls": 1,
                "api_time": conversation_sleep, "tool_time": 0.0, "tool_calls": 0}

    none = lambda *a, **kw: None  # noqa: E731
    stubs = {
        "_wire_turn_agent_callbacks": none, "_finish_stream_consumer": none,
        "_setup_stream_consumer": lambda *a, **kw: (None, None, None, False),
        "_resolve_turn_agent": _resolve_turn_agent,
        "_load_turn_history": lambda *a, **kw: ([], None, []),
        "_prepare_turn_message": lambda *a, **kw: MagicMock(blocked_error=None, agent_history=[]),
        "_run_conversation_with_session": _conversation,
        "_sync_session_after_run": lambda *a, **kw: (False, "sid", 0),
    }
    for name, stub in stubs.items():
        setattr(turn, name, stub)
    return turn.run_sync()


def test_run_sync_spans_feed_footer_setup_and_save_without_double_counting():
    """Pre-run work renders as setup and the post-run delivery tail (a 0.3s stand-in for the
    stream drain) as save; the conversation's 0.2s stays api time in neither span."""
    started = time.perf_counter()
    result = _drive_run_sync(setup_sleep=0.3, conversation_sleep=0.2)
    time.sleep(0.3)  # post-run delivery drain in _run_agent_inner's finally block
    ended = time.perf_counter()

    phases = footer_phase_times(result, turn_ended_at=ended)
    assert phases["setup"] >= 0.3 and phases["save"] >= 0.3, phases
    # No double counting: the measured spans and api time never exceed the wall-clock turn.
    assert result["api_time"] + phases["setup"] + phases["save"] <= ended - started, phases
    footer = build_footer_line(model="m", context_tokens=0, context_length=100,
                               turn_time=ended - started, api_time=result["api_time"],
                               tool_time=0.0, api_calls=1, tool_calls=0, phase_times=phases)
    assert " · setup " in footer and " · save " in footer, footer
