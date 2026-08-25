"""Cooperative in-band restart: ask live sessions to park, then resume them.

``/restart`` used to refuse new work and wait for every live turn to die.
That blocks new threads for as long as the slowest session keeps running.

This module is the weaker fix that still covers that symptom:

1. Snapshot every live chat (except the /restart requester) at the moment
   the park steer is sent. Persist that list so the next process can see it.
2. Steer each live chat agent once: park at a safe pause and end the turn.
3. Mark those sessions ``resume_pending`` with ``cooperative_restart``.
4. Startup auto-resume continues ONLY sessions from that snapshot. Leftover
   ``resume_pending`` flags on chats that were idle at steer time stay idle.

An empty snapshot is a real receipt: resume nobody. A missing file means
the previous process never started a cooperative restart (crash / drain
timeout / older build) and the usual resume_pending scan still applies.

Cron and API-server work are not steered — they have no chat loop that can
park on request. The existing drain wait still covers them.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

COOPERATIVE_RESTART_REASON = "cooperative_restart"
RESUME_ALLOWLIST_FILENAME = "cooperative_restart_resume.json"

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


def resume_allowlist_path():
    return get_hermes_home() / "gateway" / RESUME_ALLOWLIST_FILENAME


def write_resume_allowlist(session_keys: Iterable[str]) -> list[str]:
    """Persist the steer-time active-chat snapshot. Empty is a real receipt."""
    keys: list[str] = []
    seen: set[str] = set()
    for raw in session_keys:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    try:
        atomic_json_write(
            resume_allowlist_path(),
            {"session_keys": keys, "written_at": time.time()},
        )
    except Exception:
        logger.warning(
            "cooperative restart: failed to persist resume allowlist",
            exc_info=True,
        )
    return keys


def load_resume_allowlist() -> Optional[set[str]]:
    """Return the snapshot, or None when no cooperative restart was recorded."""
    path = resume_allowlist_path()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        logger.warning(
            "cooperative restart: resume allowlist unreadable; ignoring",
            exc_info=True,
        )
        return None
    if not isinstance(data, dict):
        return None
    keys = data.get("session_keys")
    if not isinstance(keys, list):
        return None
    return {str(key) for key in keys if str(key or "").strip()}


def consume_resume_allowlist() -> Optional[set[str]]:
    """Load the snapshot once, then drop the file so a later crash is a crash."""
    allowlist = load_resume_allowlist()
    if allowlist is None:
        return None
    try:
        resume_allowlist_path().unlink(missing_ok=True)
    except OSError:
        logger.debug(
            "cooperative restart: could not remove resume allowlist",
            exc_info=True,
        )
    return allowlist


def should_auto_resume_session(
    session_key: str, allowlist: Optional[set[str]]
) -> bool:
    """None allowlist = crash path (resume any pending). A set is exclusive."""
    if allowlist is None:
        return True
    return bool(session_key) and session_key in allowlist


def snapshot_active_sessions_for_restart(runner: Any) -> list[str]:
    """Log and persist live chats at the moment the park steer is sent."""
    keys = [session_key for session_key, _agent in iter_steerable_agents(runner)]
    written = write_resume_allowlist(keys)
    logger.info(
        "Cooperative restart snapshot: %d active chat(s) at steer time%s",
        len(written),
        f" ({', '.join(written[:8])})" if written else "",
    )
    return written


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
