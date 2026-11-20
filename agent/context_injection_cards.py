"""Shared emitter for ``context.injected`` progress cards (agent side).

Every producer that owes the user a "this was injected into your turn" card
builds the same payload shape and owes the same guarantees: display plumbing
must never block the model call, the payload is additive display metadata
(never the API-bound bytes), and sha-gated cards must record their gate
BEFORE the callback fires so a broken presenter cannot re-fire a card for a
fact that already happened.

This module is the leaf every emitter shares — it imports nothing from the
agent package, so turn_context, tool_executor, compression paths and the
iteration prep can all reach it without import cycles. The sha store moved
here from turn_context so the session-context card and the memory card gate
through one mechanism with one bound.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("run_agent")

# Last hash announced per session id, in process scope: the gateway process
# outlives its per-turn agents (the api_server creates a fresh agent per
# turn), so instance state alone would re-announce an unchanged payload every
# turn. Bounded FIFO — an evicted long-idle session simply re-announces once,
# like a restart.
_SESSION_CARD_SHAS: Dict[str, str] = {}
_SESSION_CARD_SHAS_LOCK = threading.Lock()
_SESSION_CARD_SHAS_MAX = 1024


def session_card_sha(session_id: Any) -> Optional[str]:
    """Last hash announced for *session_id* (None if unseen)."""
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    with _SESSION_CARD_SHAS_LOCK:
        return _SESSION_CARD_SHAS.get(session_id.strip())


def remember_session_card_sha(session_id: Any, sha: str) -> None:
    """Record an announced hash for *session_id* (no-op for session-less agents)."""
    if not isinstance(session_id, str):
        return
    key = session_id.strip()
    if not key:
        return
    with _SESSION_CARD_SHAS_LOCK:
        _SESSION_CARD_SHAS[key] = sha
        while len(_SESSION_CARD_SHAS) > _SESSION_CARD_SHAS_MAX:
            _SESSION_CARD_SHAS.pop(next(iter(_SESSION_CARD_SHAS)))


def session_card_unseen(session_id: Any, sha: str) -> bool:
    """True when *sha* was never announced for *session_id* (and records it).

    Records BEFORE the caller's callback fires, so a presenter that raises
    cannot retry a card whose underlying fact already happened.
    """
    if session_card_sha(session_id) == sha:
        return False
    remember_session_card_sha(session_id, sha)
    return True


def emit_injection_card(
    agent: Any,
    kinds: list,
    content: str,
    *,
    sources: Optional[list] = None,
    meta: Optional[dict] = None,
) -> bool:
    """Emit one ``context.injected`` card through the agent's progress callback.

    Returns whether the card fired. Never raises: display plumbing must never
    block the model call. ``sources`` defaults to a single source named after
    the first kind — the payload's provenance is honest even when a producer
    has no richer notion of it.
    """
    callback = getattr(agent, "tool_progress_callback", None)
    if not callable(callback):
        return False
    try:
        payload: Dict[str, Any] = {
            "content": content,
            "injected_chars": len(content) if isinstance(content, str) else 0,
            "sources": sources or [kinds[0] if kinds else "context"],
        }
        if kinds:
            payload["kinds"] = list(kinds)
        if meta:
            payload["meta"] = dict(meta)
        callback("context.injected", "context", None, payload)
        return True
    except Exception:
        logger.debug("injection card progress callback failed", exc_info=True)
        return False
