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

The payload builders below (``steer_card``, ``spillover_card``,
``subdirectory_card``, ``compaction_card``, ``assembled_user_card``,
``session_inventory_card``) and the detectors (``spilled_stub_from_result``,
``new_compaction_summary``) are the Python twins of the canonical Go
contract in internal/injectionlabels: the composition logic lives there,
pinned against this module by the shared table
(internal/injectionlabels/testdata/labels.json) and the running
cmd/injectionlabels binary via tests/gateway/test_context_injection_labels.py.
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


def card_payload(
    kinds: list,
    content: str,
    *,
    sources: Optional[list] = None,
    meta: Optional[dict] = None,
) -> Dict[str, Any]:
    """The canonical ``context.injected`` payload shape.

    ``sources`` defaults to a single source named after the first kind —
    the payload's provenance is honest even when a producer has no richer
    notion of it. Mirrors the Card struct in internal/injectionlabels.
    """
    payload: Dict[str, Any] = {
        "content": content,
        "injected_chars": len(content) if isinstance(content, str) else 0,
        "sources": sources or [kinds[0] if kinds else "context"],
    }
    if kinds:
        payload["kinds"] = list(kinds)
    if meta:
        payload["meta"] = dict(meta)
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


# ---------------------------------------------------------------------------
# Per-event payload builders — thin twins of the canonical Go contract
# (internal/injectionlabels); the shared table pins both sides byte-for-byte.
# ---------------------------------------------------------------------------


def steer_card(text: str) -> dict:
    """A mid-turn steering row the user sent while a tool round was in flight."""
    return card_payload(["steer"], text, sources=["user"])


def spillover_card(stub: str) -> dict:
    """The persisted-output stub an oversized tool result left behind."""
    return card_payload(["spillover"], stub, sources=["tool_result"])


def subdirectory_card(hints: str) -> dict:
    """A directory AGENTS.md hint appended to a tool result on first touch."""
    return card_payload(["subdirectory"], hints, sources=["tool_result"])


def compaction_card(summary: str) -> dict:
    """The conversation summary a committed compression inserted."""
    return card_payload(["compaction"], summary, sources=["session"])


def assembled_user_card(kind: str, detail: str, content: str) -> dict:
    """A user message assembled by a subsystem rather than typed by the user
    (standing-goal continuation, skill slash-command body); *detail* names
    WHAT the kind carried (the invoked skill's name) and lands in meta."""
    return card_payload(
        [kind], content, sources=["session"], meta={kind: detail} if detail else None
    )


# Rows of the session-inventory card that are not prompt sections — the
# per-request ephemeral session context and the tool-schema count ride the
# same card because they change with the same shape as the manifest.
_EPHEMERAL_ROW = "session context (ephemeral, per-request)"


def session_card(content: str) -> dict:
    """The once-per-shape session-context inventory card (composed content)."""
    return card_payload(["session"], content, sources=["session"])


def session_inventory_card(
    sections: list,
    *,
    ephemeral: bool = False,
    tool_count: int = 0,
) -> Tuple[str, str]:
    """Compose the once-per-shape session-context card from the manifest.

    Returns ``(content, sha)``: one line per section that landed (in prompt
    order, detail in parentheses when present), then the ephemeral and
    tool-schema rows when present. The sha — sha256 of the UTF-8 bytes — is
    the gating key: the first turn and the first turn after the prompt's
    SHAPE changes each fire once, steady-state turns stay silent. An empty
    composition (no manifest, no ephemeral, no tools) returns blank content
    and the caller stays silent. Mirrors SessionInventoryCard in
    internal/injectionlabels.
    """
    rows: List[str] = [
        f"{kind} ({detail})" if detail else str(kind) for kind, detail in sections
    ]
    if ephemeral:
        rows.append(_EPHEMERAL_ROW)
    if tool_count > 0:
        rows.append(f"{tool_count} tool schemas")
    content = "\n".join(rows)
    return content, hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Detectors — thin twins of the Go detection contract
# ---------------------------------------------------------------------------


def spilled_stub_from_result(result: Any) -> Optional[str]:
    """The persisted-output stub inside a (already persisted) tool result.

    A string result that IS a stub returns itself; a multimodal result
    returns the first content part whose text was replaced by a stub.
    Anything else (plain output) returns None — no spillover happened.
    Mirrors IsSpilledStub / SpilledStubFromParts in internal/injectionlabels.
    """
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
    """The conversation summary a COMMITTED compression inserted, if any.

    The first summary row among *after* whose content was not already
    present byte-for-byte in *before*; None when nothing new landed — a
    skip path (compression returned its input) and a byte-identical
    preserved summary both stay silent. Mirrors NewCompactionSummary in
    internal/injectionlabels.
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
