"""Concise chat notice lines for every real Jev classification request.

Troubleshooting surface only: one outbound line per actual Jev HTTP attempt
(success, negative decision, low confidence, no-reaction or failure alike),
carrying purpose, classified subject, decision/confidence and a sanitized
error class — never request/response bodies, credentials, message content or
prompt text. Notices never enter model context and never change a decision.

The canonical formatter is the Go package ``internal/jevnotices``; this Python
twin must produce byte-identical lines. Both are pinned to the shared table
``internal/jevnotices/testdata/notice_cases.json``, which each side's tests
execute, so the implementations cannot drift apart silently.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

PREFIX = "🔎 jev "

PURPOSE_GOAL_INTENT = "goal-intent"
PURPOSE_RESPONSE_GATE = "response-gate"
PURPOSE_REACTION_GATE = "reaction-gate"

#: Fixed display order of the response gate's four component nouls (the
#: judge's question order). Keys missing from ``scores`` render as 0.00.
_SCORE_KEYS = ("addresses_bot", "continues_bot_thread", "joins_bot_thread", "noise")

_TOKEN_DISALLOWED = re.compile(r"[^A-Za-z0-9_.\-]+")
_MAX_TOKEN_CHARS = 48
_MAX_TEXT_CHARS = 80


def sanitize_token(raw: Any) -> str:
    """Clean one error/label fragment (class token only; detail after ':' dropped)."""
    head = str(raw or "").split(":", 1)[0].strip()
    cleaned = _TOKEN_DISALLOWED.sub("_", head).strip("_")
    if not cleaned:
        return "error"
    return cleaned[:_MAX_TOKEN_CHARS]


def sanitize_text(raw: Any) -> str:
    """Clean one free-text fragment (the subject); ``"message"`` when unusable."""
    cleaned = " ".join(str(raw or "").split())
    return cleaned[:_MAX_TEXT_CHARS] or "message"


def sanitize_author(raw: Any) -> str:
    """Clean one display-name/emoji fragment; empty means omit.

    Controls, backticks and ``@`` are dropped: a notice fragment must never be
    actionable chat syntax (ping, code fence).
    """
    text = str(raw or "")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = re.sub(r"[`@]", "", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:_MAX_TEXT_CHARS]


def format_notice(record: Dict[str, Any]) -> str:
    """Render one notice line from a record dict (see the shared case table).

    Pure: no I/O, no clock; fails closed to a safe line on junk input.
    """
    purpose = sanitize_token(record.get("purpose"))
    parts = [PREFIX, purpose]
    attempt = int(record.get("attempt") or 0)
    attempts = int(record.get("attempts") or 0)
    if attempts > 1 and attempt > 0:
        parts.append(f" (attempt {attempt}/{attempts})")
    parts.append(" ")
    parts.append(sanitize_text(record.get("subject")))
    author = sanitize_author(record.get("author"))
    if author:
        parts.append(f" from {author}")
    parts.append(": ")
    parts.append(_verdict(record, purpose))
    return "".join(parts)


def _verdict(record: Dict[str, Any], purpose: str) -> str:
    error = record.get("error")
    if error:
        return f"failed ({sanitize_token(error)})"
    if purpose == PURPOSE_GOAL_INTENT:
        label = sanitize_token(record.get("label"))
        confidence = record.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            return f"{label} (confidence {float(confidence):.2f})"
        return f"{label} (confidence unavailable)"
    if purpose == PURPOSE_RESPONSE_GATE:
        allowed = record.get("allowed")
        if not isinstance(allowed, bool):
            return "decision unavailable"
        scores = record.get("scores") if isinstance(record.get("scores"), dict) else {}
        rendered = list(_SCORE_KEYS)
        extras = sorted(k for k in scores if k not in rendered)
        rendered.extend(extras)
        body = " ".join(f"{key}={float(scores.get(key, 0.0)):.2f}" for key in rendered)
        return f"{'allow' if allowed else 'deny'} ({body})"
    if purpose == PURPOSE_REACTION_GATE:
        emoji = sanitize_author(record.get("emoji"))
        abstain = record.get("abstain")
        suffix = ""
        if isinstance(abstain, (int, float)) and not isinstance(abstain, bool):
            suffix = f" (abstain {float(abstain):.2f})"
        if emoji:
            return f"react {emoji}{suffix}"
        return f"no reaction{suffix}"
    return "decision unavailable"


# ── record builders: one per owning call site ────────────────────────────
# Each builder turns one real decision object into a record for
# :func:`format_notice`. The no-call paths (bypass, missing credential,
# suppression) never construct a record — no notice may claim an HTTP request
# that was not made.


def goal_intent_attempt_record(
    *, attempt: int, attempts: int, label: Optional[str], confidence: Optional[float],
    error: Optional[str], subject: str = "user message", author: str = "",
) -> Dict[str, Any]:
    """One goal-intent Jev attempt: success carries label+confidence, failure the error class."""
    record: Dict[str, Any] = {
        "purpose": PURPOSE_GOAL_INTENT,
        "subject": subject,
        "author": author,
        "attempt": attempt,
        "attempts": attempts,
    }
    if error:
        record["error"] = error
    else:
        record["label"] = label or "unknown"
        record["confidence"] = confidence
    return record


def response_gate_record(decision: Any, *, author: str = "") -> Optional[Dict[str, Any]]:
    """One response-gate consultation, or None when no HTTP attempt was made.

    ``missing_credential`` is the only evaluate() outcome that never posted a
    request; every other outcome (including timeouts and HTTP errors) did.
    """
    if getattr(decision, "error", None) == "missing_credential":
        return None
    record: Dict[str, Any] = {
        "purpose": PURPOSE_RESPONSE_GATE,
        "subject": "user message",
        "author": author,
    }
    if decision.error:
        record["error"] = decision.error
        return record
    record["allowed"] = bool(decision.allowed)
    record["scores"] = dict(getattr(decision, "scores", None) or {})
    return record


def reaction_gate_record(decision: Any, *, author: str = "") -> Optional[Dict[str, Any]]:
    """One reaction-gate consultation, or None when no HTTP attempt was made."""
    if getattr(decision, "error", None) == "missing_credential":
        return None
    record: Dict[str, Any] = {
        "purpose": PURPOSE_REACTION_GATE,
        "subject": "user message",
        "author": author,
    }
    if decision.error:
        record["error"] = decision.error
        return record
    if decision.emoji is not None:
        record["emoji"] = str(decision.emoji)
    if decision.abstain_sum is not None:
        record["abstain"] = decision.abstain_sum
    return record
