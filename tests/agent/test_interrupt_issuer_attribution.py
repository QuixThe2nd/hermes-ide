"""``interrupt()`` records who asked for the stop, so the turn exit reason can attribute it (#112647).

Every system watchdog reaches the agent through ``request_hard_interrupt(..., tool_reason=...)``;
the published ``_tool_interrupt_reason`` is the single source the exit reason is derived from.
"""

from __future__ import annotations

import logging
import threading

from agent.interrupt_compat import request_hard_interrupt
from agent.interrupt_control import interrupt_issuer
from tools.interrupt import set_interrupt


def _bare_agent():
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._tool_interrupt_reason = None
    agent._hard_interrupt_requested = threading.Event()
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.quiet_mode = True
    return agent


def test_system_producer_is_recorded_and_logged_at_publication(caplog):
    """The cron/gateway watchdog shape: the issuer survives to ``interrupt_issuer`` and ONE log line
    names it at the point ``_interrupt_requested`` is set."""
    agent = _bare_agent()
    try:
        with caplog.at_level(logging.INFO, logger="run_agent"):
            assert request_hard_interrupt(agent, "Cron job timed out (inactivity)", tool_reason="cron inactivity watchdog")
        assert agent._interrupt_requested is True
        assert interrupt_issuer(agent) == "cron_inactivity_watchdog"
        published = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Interrupt requested")]
        assert published == ["Interrupt requested (hard): cron inactivity watchdog"]
    finally:
        set_interrupt(False)


def test_human_stops_have_no_system_issuer():
    """A plain ``interrupt()`` and a reason-less hard stop (CLI/TUI /stop) stay attributed to the user."""
    agent = _bare_agent()
    try:
        agent.interrupt()
        assert interrupt_issuer(agent) is None
        agent.clear_interrupt()
        assert request_hard_interrupt(agent)
        assert interrupt_issuer(agent) is None
    finally:
        set_interrupt(False)
