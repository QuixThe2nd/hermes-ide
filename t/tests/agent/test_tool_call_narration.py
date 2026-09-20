"""Tests for the universal tool-call narration guidance block.

The guidance (agent.prompt_builder.TOOL_CALL_NARRATION_GUIDANCE) tells the
model to explain each tool call before making it and briefly note
significant results — on chat platforms a silent multi-call chain looks like
a frozen client.  Gated by ``agent.tool_call_narration_guidance``
(default True), injected only when tools are loaded.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt(agent)


class TestToolCallNarrationInjection:
    def test_present_with_tools_by_default(self):
        agent = _make_agent(valid_tool_names=["terminal", "read_file"])
        assert "Tool call narration" in _stable_prompt(agent)

    def test_absent_without_tools(self):
        agent = _make_agent(valid_tool_names=[])
        assert "Tool call narration" not in _stable_prompt(agent)

    def test_config_false_suppresses(self):
        agent = _make_agent(
            valid_tool_names=["terminal"],
            _tool_call_narration_guidance=False,
        )
        assert "Tool call narration" not in _stable_prompt(agent)

    @pytest.mark.parametrize("flag", ["_task_completion_guidance", "_parallel_tool_call_guidance"])
    def test_independent_of_sibling_gates(self, flag):
        # Turning a sibling guidance off must not silence narration.
        agent = _make_agent(
            valid_tool_names=["terminal"],
            **{flag: False},
        )
        assert "Tool call narration" in _stable_prompt(agent)

    def test_lands_in_cached_static_prefix(self):
        agent = _make_agent(valid_tool_names=["terminal"])
        prompt = _stable_prompt(agent)
        static = getattr(agent, "_cached_system_prompt_static", "")
        if not static:
            pytest.skip("cache split not active on this code path")
        assert "Tool call narration" in static

    def test_parallel_and_narration_coexist(self):
        agent = _make_agent(valid_tool_names=["terminal"])
        stable = _stable_prompt(agent)
        assert "Parallel tool calls" in stable
        assert "Tool call narration" in stable
