"""Shared emitter for ``context.injected`` progress cards (agent side).

Display plumbing must never block the model call, and sha-gated cards record
their gate BEFORE the callback fires so a broken presenter cannot re-fire a
card for a fact that already happened. A leaf module (no agent imports), so
every chokepoint can reach it without cycles.

The builders and detectors are thin twins of the canonical Go contract in
internal/injectionlabels, pinned by the shared table
(internal/injectionlabels/testdata/labels.json) and the running
cmd/injectionlabels binary in tests/gateway/test_context_injection_labels.py.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

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


# Provenance per card kind — mirrors the builders in internal/injectionlabels
# (cards.go), which the shared table pins against this module.
_CARD_SOURCES = {
    "steer": "user",
    "spillover": "tool_result",
    "subdirectory": "tool_result",
    "compaction": "session",
    "goal": "session",
    "skill": "session",
    "session": "session",
}


def event_card(kind: str, content: str, detail: str = "") -> Dict[str, Any]:
    """The ``context.injected`` payload for one injected fact of *kind*.

    *detail* names WHAT the kind carried (the invoked skill's name) and lands
    in ``meta``.
    """
    payload: Dict[str, Any] = {
        "content": content,
        "injected_chars": len(content),
        "sources": [_CARD_SOURCES[kind]],
        "kinds": [kind],
    }
    if detail:
        payload["meta"] = {kind: detail}
    return payload


def emit_card(agent: Any, payload: dict) -> bool:
    """Fire one prebuilt ``context.injected`` payload; never raises."""
    callback = getattr(agent, "tool_progress_callback", None)
    if not callable(callback):
        return False
    try:
        callback("context.injected", "context", None, payload)
        return True
    except Exception:
        logger.debug("injection card progress callback failed", exc_info=True)
        return False


def session_inventory_card(
    sections: list,
    *,
    ephemeral: bool = False,
    tool_count: int = 0,
) -> Tuple[Dict[str, Any], str]:
    """The once-per-shape session-context card and its sha gating key.

    One line per manifest section (detail in parentheses), then the
    ephemeral and tool-schema rows. Blank content means nothing to announce.
    """
    rows: List[str] = [
        f"{kind} ({detail})" if detail else str(kind) for kind, detail in sections
    ]
    if ephemeral:
        rows.append("session context (ephemeral, per-request)")
    if tool_count > 0:
        rows.append(f"{tool_count} tool schemas")
    content = "\n".join(rows)
    return event_card("session", content), hashlib.sha256(content.encode("utf-8")).hexdigest()


def spilled_stub_from_result(result: Any) -> Optional[str]:
    """The persisted-output stub inside a persisted tool result, else None."""
    from tools.tool_result_storage import PERSISTED_OUTPUT_TAG

    if isinstance(result, str):
        return result if PERSISTED_OUTPUT_TAG in result else None
    if isinstance(result, dict):
        for part in (result.get("content") or []):
            if (
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and PERSISTED_OUTPUT_TAG in part["text"]
            ):
                return part["text"]
    return None


def new_compaction_summary(before: Any, after: Any) -> Optional[str]:
    """The first summary row in *after* not already present in *before*.

    None when nothing new landed: skip paths and byte-identical preserved
    summaries stay silent.
    """
    from agent.context_compressor import ContextCompressor

    if not isinstance(after, list):
        return None
    before_texts = (
        {m.get("content") for m in before if isinstance(m, dict)}
        if isinstance(before, list)
        else set()
    )
    for message in after:
        if not ContextCompressor._is_context_summary_message(message):
            continue
        content = message.get("content")
        if content in before_texts:
            continue
        return str(content or "")
    return None
