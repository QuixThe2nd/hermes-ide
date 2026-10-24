"""Ambient binding of the live agent for registry-level delegate dispatch.

Why this exists: the agent-turn loop (agent/tool_executor.py) intercepts
``delegate_agent``/``delegate_task`` for the subagent fast path, but
backend-routed (claude/cursor) calls deliberately fall through to registry
dispatch - and child sessions, cron agent sessions and some resumed sessions
dispatch delegate calls with no turn-loop interceptor at all. The unified
handler (tools/delegate_tool.py::_delegate_agent_handler) needs the live agent
as ``parent_agent`` for the subagent engine; the external backends need only
the session/tool-call ids that ``_execute_tool`` already supplies.

Seams that own a live agent bind it around dispatch
(``bound_agent_dispatch_context``); ``model_tools._execute_tool`` forwards the
bound agent as ``parent_agent`` so interceptor-less paths reach the real
handler. A caller with no bound agent dispatches with ``parent_agent=None``
and the handler answers with its own clear error - the agent-loop stub must
never surface for delegate names. Nothing here executes delegation;
tools/delegate_tool.py stays the only decision-maker.
"""

from contextvars import ContextVar
from contextlib import contextmanager
from typing import Any, Optional

_current_agent: ContextVar[Optional[Any]] = ContextVar(
    "hermes_delegate_dispatch_agent", default=None)


def bind_agent_dispatch_context(agent: Any) -> Any:
    """Bind *agent* as the delegate-dispatch parent for this context. Returns a token."""
    return _current_agent.set(agent)


def reset_agent_dispatch_context(token: Any) -> None:
    """Restore the previous binding (pair with ``bind_agent_dispatch_context``)."""
    _current_agent.reset(token)


@contextmanager
def bound_agent_dispatch_context(agent: Any):
    """Context manager: *agent* is the delegate-dispatch parent inside the block."""
    token = bind_agent_dispatch_context(agent)
    try:
        yield agent
    finally:
        reset_agent_dispatch_context(token)


def get_agent_dispatch_context() -> Optional[Any]:
    """The bound live agent, or None when no agent owns this dispatch (bare registry caller)."""
    return _current_agent.get()
