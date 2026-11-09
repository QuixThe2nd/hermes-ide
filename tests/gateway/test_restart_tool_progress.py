"""The ``restart`` tool renders a progress bubble like any other tool.

Suppression was originally added alongside ``clarify`` (#52374, #529) on the
theory that restart lifecycle notices owned the user-facing presentation.
Under the overlap-restart architecture the requester turn gets no lifecycle
message until after the bounce, so a suppressed bubble left the restart turn
visibly silent. Directive 2026-11-09: the tool call must be visible like any
other; ``clarify`` keeps its suppression (the interactive prompt IS the
rendering, #52374).
"""

import importlib
import sys
import time
import types

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_clarify_progress_leak import (
    ProgressCaptureAdapter,
    _make_runner,
)


class RestartClarifyThenToolAgent:
    """Emits restart + clarify tool.started events, then a normal tool."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "restart", "restart", {})
            time.sleep(0.35)
            cb(
                "tool.started",
                "clarify",
                "Which environment?",
                {
                    "question": "Which environment?",
                    "choices": ["staging", "production"],
                },
            )
            time.sleep(0.35)
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


def _install_fakes(monkeypatch, mode):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", mode)

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = RestartClarifyThenToolAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 — register terminal emoji

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"}
    )
    return gateway_run


@pytest.mark.parametrize("mode", ["all", "verbose"])
@pytest.mark.asyncio
async def test_restart_tool_renders_progress_bubble(monkeypatch, tmp_path, mode):
    """A progress bubble for restart in every mode; other rules still hold."""
    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, mode)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    source = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="dm")

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-restart-progress",
        session_key="agent:main:slack:dm:C1",
    )

    assert result["final_response"] == "done"
    all_content = "\n".join(
        [m["content"] for m in adapter.sent] + [e["content"] for e in adapter.edits]
    )
    # The restart tool call IS visible — tool name or its describe verb line.
    assert "restart" in all_content.lower() or "restarting the agent" in all_content.lower()
    # The clarify suppression beside it still holds.
    assert "clarify" not in all_content
    assert "Which environment?" not in all_content
    # The unrelated terminal tool still renders progress normally.
    assert "pwd" in all_content
