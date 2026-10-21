"""Typed per-turn control seam for trusted tool results that end the turn.

The gateway ``restart`` tool is the one tool whose success means the current
process is already draining for a restart: continuing the calling turn keeps
creating work inside the drain that is waiting for that same turn to finish.
Its trusted result therefore carries a reserved exact JSON field; the tool
executors arm a per-turn agent flag from it, and the conversation loop ends
the turn before any further provider call.

The contract is keyed on the reserved field plus the exact tool name — never
on user-visible prose or substring matching — and only for statuses that mean
the drain is committed or already active.  Cancelled and failed restart calls
leave the flag unarmed and the normal provider/tool loop continues.
"""

from __future__ import annotations

import json
from typing import Any

# The only tool whose output may arm the gateway-restart turn control.
GATEWAY_RESTART_TOOL_NAME = "restart"

# Reserved exact JSON field on the trusted restart tool result.
TURN_CONTROL_FIELD = "_hermes_turn_control"

# Its sole control value today.
GATEWAY_RESTART_CONTROL = "gateway_restart"

# Restart statuses that mean the drain is committed or already active.
TERMINAL_RESTART_STATUSES = frozenset({"restarting", "already_in_progress"})


def is_terminal_restart_status(status: Any) -> bool:
    """Return True when a restart status ends the calling turn."""
    return status in TERMINAL_RESTART_STATUSES


def turn_control_field_for(status: Any) -> dict[str, str] | None:
    """Return the reserved control field to stamp for ``status``, if terminal."""
    if is_terminal_restart_status(status):
        return {TURN_CONTROL_FIELD: GATEWAY_RESTART_CONTROL}
    return None


def arm_gateway_restart_control(agent: Any, tool_name: str, result: Any) -> bool:
    """Arm the per-turn gateway-restart flag from trusted tool output.

    Returns True when the flag transitioned to armed.  Anything that is not
    an exact ``restart`` tool result carrying the reserved control value
    (cancelled and failed calls, unrelated tools, non-JSON output) is a no-op.
    """
    if tool_name != GATEWAY_RESTART_TOOL_NAME or not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get(TURN_CONTROL_FIELD) != GATEWAY_RESTART_CONTROL:
        return False
    if getattr(agent, "_turn_gateway_restart_queued", False):
        return False
    agent._turn_gateway_restart_queued = True
    return True


def is_gateway_restart_armed(agent: Any) -> bool:
    """Return True when the per-turn gateway-restart control is armed."""
    return bool(getattr(agent, "_turn_gateway_restart_queued", False))
