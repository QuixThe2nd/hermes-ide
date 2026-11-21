"""Gateway pre/post-run work reaches the footer as setup/save, not ``other`` (live session
20261120_143237: ~0.85s agent build and ~2.0s delivery drain were unattributed)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.runtime_footer import build_footer_line, footer_phase_times
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _drive_run_sync(*, clock, setup_seconds: float, conversation_seconds: float,
                    turn_started_at: float | None = None, barrier=None,
                    resolution_error: Exception | None = None) -> dict:
    runner = MagicMock()
    runner._resolve_session_agent_runtime.return_value = ("m", {})
    runner._resolve_session_agent_runtime.side_effect = resolution_error
    agent = SimpleNamespace(context_compressor=None, model="m")
    ctx = TurnContext(source=SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="u"),
                      message="hello", history=[], session_id="sid", session_key="key",
                      user_config={}, _run_still_current=lambda: True,
                      turn_started_at=turn_started_at)
    turn = TurnRunner(runner, ctx)

    def _resolve_turn_agent(*_a, **_kw):
        if barrier is not None:
            barrier.wait(timeout=5)
        clock.advance(setup_seconds)
        return agent, False

    def _conversation(*_a, **_kw):
        clock.advance(conversation_seconds)
        return {"final_response": "ok", "messages": [], "api_calls": 1,
                "api_time": conversation_seconds, "tool_time": 0.0, "tool_calls": 0}

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


def test_run_sync_spans_feed_footer_setup_and_save_without_double_counting(monkeypatch):
    """Legacy contexts without an intake stamp retain run-local setup accounting."""
    clock = _Clock()
    monkeypatch.setattr("gateway.run_turn_runner.time.perf_counter", clock)
    started = clock()
    result = _drive_run_sync(clock=clock, setup_seconds=0.3, conversation_seconds=0.2)
    clock.advance(0.3)  # post-run delivery drain in _run_agent_inner's finally block
    ended = clock()

    phases = footer_phase_times(result, turn_ended_at=ended)
    assert phases["setup"] == pytest.approx(0.3)
    assert phases["save"] == pytest.approx(0.3)
    assert result["api_time"] + sum(phases.values()) == pytest.approx(ended - started)
    footer = build_footer_line(model="m", context_tokens=0, context_length=100,
                               turn_time=ended - started, api_time=result["api_time"],
                               tool_time=0.0, api_calls=1, tool_calls=0, phase_times=phases)
    assert " · setup " in footer and " · save " in footer, footer




def test_concurrent_turns_keep_intake_and_response_timestamps_separate(monkeypatch):
    """Two interleaved run_sync calls use their context's clock values, never a shared turn start."""
    local = threading.local()
    real_perf_counter = time.perf_counter

    def _perf_counter():
        clock = getattr(local, "clock", None)
        return clock() if clock is not None else real_perf_counter()

    monkeypatch.setattr("gateway.run_turn_runner.time.perf_counter", _perf_counter)
    # turn start, run_sync start, agent setup, conversation, response tail
    specs = [(10.0, 15.0, 2.0, 3.0, 4.0), (20.0, 28.0, 4.0, 5.0, 1.0)]
    barrier = threading.Barrier(len(specs))

    def _run(spec):
        turn_started, run_started, setup, conversation, save = spec
        clock = _Clock(run_started)
        local.clock = clock
        result = _drive_run_sync(clock=clock, setup_seconds=setup,
                                 conversation_seconds=conversation,
                                 turn_started_at=turn_started, barrier=barrier)
        clock.advance(save)
        return result, clock(), turn_started, run_started + setup - turn_started

    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        outcomes = list(pool.map(_run, specs))

    for result, ended, turn_started, expected_setup in outcomes:
        phases = footer_phase_times(result, turn_ended_at=ended)
        assert result["gateway_setup_time"] == pytest.approx(expected_setup)
        assert phases["setup"] == pytest.approx(expected_setup)
        assert result["api_time"] + sum(phases.values()) == pytest.approx(ended - turn_started)
        footer = build_footer_line(model="m", context_tokens=0, context_length=100,
                                   turn_time=ended - turn_started, api_time=result["api_time"],
                                   tool_time=0.0, api_calls=1, tool_calls=0, phase_times=phases)
        assert " other " not in footer


def test_model_resolution_error_has_no_conversation_save_span(monkeypatch):
    clock = _Clock(15.0)
    monkeypatch.setattr("gateway.run_turn_runner.time.perf_counter", clock)
    result = _drive_run_sync(clock=clock, setup_seconds=0.0, conversation_seconds=0.0,
                             turn_started_at=10.0, resolution_error=RuntimeError("unavailable"))

    assert result["api_calls"] == 0
    assert "conversation_ended_at" not in result
    assert footer_phase_times(result, turn_ended_at=clock()) is None
