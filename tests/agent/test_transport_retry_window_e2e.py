"""E2E: the live conversation loop retries transient transport failures inside
the per-turn window (2026-11-12 incident).

Drives ``AIAgent.run_conversation`` end-to-end with a staged ``create``
side_effect, so the contract is the consumer-visible one: a provider outage
that outlasts ``agent.api_max_retries`` no longer kills the turn while the
transport window is open; window expiry and non-transient errors keep the
legacy fast-fail; an interrupt cancels the extended wait promptly.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


def _mock_response(content="Hello", finish_reason="stop"):
    """Minimal OpenAI ChatCompletion response double (same shape as
    tests/agent/test_run_agent.py::_mock_response — kept local so this file
    needs no mid-test import of the tests package under the home-isolation
    guard)."""
    msg = types.SimpleNamespace(content=content, tool_calls=None)
    choice = types.SimpleNamespace(message=msg, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_outer_error_retry_bound)
    so ``run_conversation`` drives the real retry loop."""
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
        agent.client.chat.completions.create.side_effect = (
            lambda *a, **k: _mock_response(content="ok", finish_reason="stop")
        )
        return agent


def _speed_up_backoffs(monkeypatch, agent):
    """Tiny regular + extended backoff delays so the E2E turns stay fast."""
    from agent import transport_retry

    agent._retry_base_delay = 0.01
    agent._retry_max_delay = 0.02
    monkeypatch.setattr(transport_retry, "_TRANSPORT_RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(transport_retry, "_TRANSPORT_RETRY_MAX_DELAY", 0.02)


def _run_isolated(agent):
    """Run one turn with persistence patched out (error-turn pattern from
    test_outer_error_retry_bound)."""
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("hello")


class TestTransportRetryWindowE2E:
    def test_transport_outage_recovers_inside_the_window(self, loop_agent, monkeypatch):
        """TimeoutError x5 (past api_max_retries=2) then recovery → the turn
        completes instead of dying with 'API call failed'."""
        _speed_up_backoffs(monkeypatch, loop_agent)
        loop_agent._api_max_retries = 2
        loop_agent._transport_retry_window_seconds = 60.0
        monkeypatch.setattr(loop_agent, "_try_recover_primary_transport", lambda *a, **k: False)

        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] <= 5:
                raise TimeoutError("codex stream produced no parsed stream event within 120s")
            return _mock_response(content="ok", finish_reason="stop")

        loop_agent.client.chat.completions.create.side_effect = flaky
        result = _run_isolated(loop_agent)

        assert result["completed"] is True, result
        assert result["final_response"] == "ok"
        # 2 regular attempts + 3 extended (window) attempts + 1 success = 6
        # creates; turn_stop_gates may add one nudge iteration afterwards, so
        # bound the range instead of pinning the exact tail call.
        assert 6 <= calls["n"] <= 8, f"expected 6 core attempts (+≤2 gate nudges), got {calls['n']}"

    def test_window_expiry_fails_the_turn(self, loop_agent, monkeypatch):
        """Past the window the turn fails exactly like the legacy fast-fail."""
        _speed_up_backoffs(monkeypatch, loop_agent)
        loop_agent._api_max_retries = 2
        loop_agent._transport_retry_window_seconds = 0.06
        monkeypatch.setattr(loop_agent, "_try_recover_primary_transport", lambda *a, **k: False)

        calls = {"n": 0}

        def always_timeout(*_a, **_k):
            calls["n"] += 1
            raise TimeoutError("codex stream produced no parsed stream event within 120s")

        loop_agent.client.chat.completions.create.side_effect = always_timeout
        result = _run_isolated(loop_agent)

        assert result["failed"] is True, result
        assert "API call failed" in result["final_response"]
        assert calls["n"] >= 3, f"expected at least one extended attempt, got {calls['n']}"
        assert calls["n"] <= 12, f"window must bound the extended attempts, got {calls['n']}"

    def test_nontransient_error_keeps_the_fast_fail(self, loop_agent, monkeypatch):
        """An unclassified (non-transport) error must not enter the window even
        with one configured — it stops at api_max_retries."""
        _speed_up_backoffs(monkeypatch, loop_agent)
        loop_agent._api_max_retries = 2
        loop_agent._transport_retry_window_seconds = 60.0
        monkeypatch.setattr(loop_agent, "_try_recover_primary_transport", lambda *a, **k: False)

        calls = {"n": 0}

        def always_mystery(*_a, **_k):
            calls["n"] += 1
            raise RuntimeError("mystery upstream shape")

        loop_agent.client.chat.completions.create.side_effect = always_mystery
        result = _run_isolated(loop_agent)

        assert result["failed"] is True, result
        assert calls["n"] == 2, f"non-transport errors must stop at api_max_retries, got {calls['n']}"

    def test_interrupt_cancels_the_extended_wait(self, loop_agent, monkeypatch):
        """/stop arriving during the extended wait aborts the turn promptly."""
        _speed_up_backoffs(monkeypatch, loop_agent)
        loop_agent._api_max_retries = 2
        loop_agent._transport_retry_window_seconds = 60.0
        monkeypatch.setattr(loop_agent, "_try_recover_primary_transport", lambda *a, **k: False)

        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 3:
                loop_agent._interrupt_requested = True
            raise TimeoutError("codex stream produced no parsed stream event within 120s")

        loop_agent.client.chat.completions.create.side_effect = flaky
        result = _run_isolated(loop_agent)

        assert result.get("interrupted") is True, result
        assert calls["n"] == 3, f"the interrupt must end the extended wait, got {calls['n']} attempts"
