"""Per-turn phase accumulators (mem/setup/compress/save) render as footer
categories instead of one unexplained `other` bucket. Live evidence: a
Hindsight ``prefetch_all`` blocking ~10s at turn start (agent.log "Prefetch:
server retain visibility timed out after 10.0s") rendered as bare `other`."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.turn_timing import (
    PHASE_KEYS,
    add_phase_time,
    reset_turn_phase_times,
    timed_phase,
    timed_setup_exclusive,
)


class TestAccumulatorPrimitives:
    def test_add_phase_time_seeds_missing_accumulator_and_clamps_negative(self):
        agent = SimpleNamespace()
        add_phase_time(agent, "compress", 2.5)
        add_phase_time(agent, "compress", -9.0)
        assert agent._turn_compress_time == 2.5

    def test_reset_zeroes_every_category(self):
        agent = SimpleNamespace()
        for key in PHASE_KEYS:
            setattr(agent, f"_turn_{key}_time", 1.0)
        reset_turn_phase_times(agent)
        assert [getattr(agent, f"_turn_{k}_time") for k in PHASE_KEYS] == [0.0] * len(PHASE_KEYS)

    def test_timed_setup_exclusive_subtracts_mem_and_compress_claimed_inside(self):
        """The prologue bucket must not double-count time its own sub-phases
        already claimed while running inside the same block."""
        agent = SimpleNamespace()
        with timed_setup_exclusive(agent):
            with timed_phase(agent, "mem"):
                time.sleep(0.05)
            with timed_phase(agent, "compress"):
                time.sleep(0.05)
        assert agent._turn_setup_time < 0.05
        assert agent._turn_mem_time >= 0.04
        assert agent._turn_compress_time >= 0.04


@pytest.fixture()
def phase_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_run_agent's fixture)."""
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


class _SlowPrefetchManager:
    """Memory-manager stand-in whose synchronous prefetch blocks (Hindsight shape)."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def on_turn_start(self, *_args, **_kwargs) -> None:
        return None

    def prefetch_all(self, _query, **_kwargs) -> str:
        time.sleep(self._seconds)
        return "RECALLED-MEMORY"

    def describe_recall(self) -> str:
        return ""


def _run_text_turn(agent, answer: str):
    from tests.agent.test_run_agent import _mock_response

    resp = _mock_response(content=answer, finish_reason="stop")
    # Canonical full-turn seam (see test_run_agent's retry tests): replace
    # _interruptible_api_call so the loop never touches a real client.
    agent._interruptible_api_call = lambda _api_kwargs: resp
    with (
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(
            "please summarize the quarterly infrastructure report in detail"
        )


class TestTurnAccumulation:
    def test_slow_memory_prefetch_lands_in_mem_not_other(self, phase_agent):
        phase_agent._memory_manager = _SlowPrefetchManager(seconds=0.25)

        started = time.perf_counter()
        result = _run_text_turn(phase_agent, "the report is done")
        turn_seconds = time.perf_counter() - started

        assert result["final_response"] == "the report is done"
        assert result["mem_time"] >= 0.2
        assert result.get("tool_time", 0.0) == 0.0

        from gateway.runtime_footer import build_footer_line, phase_times_from_result

        footer = build_footer_line(
            model=result.get("model"),
            context_tokens=0,
            context_length=100,
            turn_time=turn_seconds,
            api_time=result["api_time"],
            tool_time=result.get("tool_time", 0.0),
            api_calls=result.get("api_calls", 1),
            tool_calls=result.get("tool_calls", 0),
            phase_times=phase_times_from_result(result),
        )
        assert " mem " in footer
        # The slow prefetch is attributed to mem; the footer may still show a
        # residual `other` for genuinely unattributed loop overhead.

    def test_turn_without_measurable_phase_work_stays_below_threshold(self, phase_agent):
        result = _run_text_turn(phase_agent, "plain answer")

        assert result["final_response"] == "plain answer"
        # Wrapper overhead only — loose bound so a busy runner's scheduling
        # jitter can't flake it; a real mis-attribution would claim whole
        # seconds of the turn.
        for key in PHASE_KEYS:
            assert result.get(f"{key}_time", 0.0) < 1.0, (key, result.get(f"{key}_time"))
