"""Cooperative in-band restart: ask live sessions to park, then resume them.

``/restart`` used to refuse new work and wait for every live turn to die.
That blocks new threads for as long as the slowest session keeps running.

This module is the weaker fix that still covers that symptom:

1. Steer each live chat agent once: park at a safe pause and end the turn.
2. Mark those sessions ``resume_pending`` with ``cooperative_restart``.
3. Startup auto-resume continues them after the new gateway is up.

Cron and API-server work are not steered — they have no chat loop that can
park on request. The existing drain wait still covers them.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

COOPERATIVE_RESTART_REASON = "cooperative_restart"

COOPERATIVE_RESTART_STEER = (
    "A gateway restart was requested in another session. "
    "Park this turn at a safe pause point now: finish or checkpoint the "
    "current tool batch, do not start new long work, then end this turn. "
    "The gateway will come back and continue this session automatically. "
    "Do not wait for the user. Do not ask questions. Stop after you have "
    "reached a pausable state."
)


def is_cooperative_restart_reason(reason: Optional[str]) -> bool:
    return reason == COOPERATIVE_RESTART_REASON


def should_preserve_cooperative_restart_marker(
    *,
    draining: bool,
    resume_reason: Optional[str],
) -> bool:
    """Keep the marker when a park-turn finishes during the restart drain.

    Clearing it here would lose the startup auto-continue signal — the whole
    point of asking the session to wind itself down.
    """
    return bool(draining and is_cooperative_restart_reason(resume_reason))


def requester_session_key(runner: Any) -> Optional[str]:
    source = getattr(runner, "_restart_command_source", None)
    if source is None:
        return None
    builder = getattr(runner, "_session_key_for_source", None)
    if not callable(builder):
        return None
    try:
        key = builder(source)
    except Exception:
        logger.debug("cooperative restart: requester session key failed", exc_info=True)
        return None
    return str(key) if key else None


def iter_steerable_agents(runner: Any) -> Iterable[tuple[str, Any]]:
    snapshot = getattr(runner, "_snapshot_running_agents", None)
    agents = snapshot() if callable(snapshot) else {}
    if not isinstance(agents, dict):
        return ()
    skip = requester_session_key(runner)
    for session_key, agent in agents.items():
        if not session_key or agent is None:
            continue
        if skip and session_key == skip:
            continue
        yield session_key, agent


def steer_running_agents_for_restart(runner: Any) -> list[str]:
    """Inject a one-shot park steer into each live chat agent.

    Returns the session keys that accepted a steer. Never interrupts. A
    missing ``steer()`` or a rejected empty agent is skipped.
    """
    steered: list[str] = []
    already = getattr(runner, "_cooperative_restart_sessions", None)
    seen = set(already) if already else set()
    for session_key, agent in iter_steerable_agents(runner):
        if session_key in seen:
            continue
        steer = getattr(agent, "steer", None)
        if not callable(steer):
            continue
        try:
            accepted = bool(steer(COOPERATIVE_RESTART_STEER))
        except Exception:
            logger.debug(
                "cooperative restart: steer failed for %s",
                session_key,
                exc_info=True,
            )
            continue
        if accepted:
            steered.append(session_key)
    return steered


def mark_cooperative_restart_sessions(runner: Any, session_keys: Iterable[str]) -> int:
    """Persist resume_pending so startup auto-continues parked sessions."""
    store = getattr(runner, "session_store", None)
    mark = getattr(store, "mark_resume_pending", None) if store is not None else None
    if not callable(mark):
        return 0
    marked = 0
    for session_key in session_keys:
        try:
            if mark(session_key, COOPERATIVE_RESTART_REASON):
                marked += 1
            else:
                # MagicMock / stores that return None still counted as attempted.
                marked += 1
        except Exception:
            logger.debug(
                "cooperative restart: mark_resume_pending failed for %s",
                session_key,
                exc_info=True,
            )
    return marked
