"""Tool-style automatic compression lifecycle status lines.

Presentation layer for automatic context compression on chat gateways: one
``context_compress`` status line posted before the expensive summary work and
edited in place as the attempt succeeds, fails, or is blocked — instead of
unpaired raw failure/cooldown chatter.

This module is PRESENTATION-ONLY. It changes no trigger, timeout, cooldown,
fallback, transcript-integrity, commit-fencing, or retry policy. The lines
emitted here ride the existing ``status_callback`` rail
(``run_agent.AIAgent.status_callback`` → ``gateway/run.py
_status_callback_sync``) under a dedicated event type
(:data:`COMPRESSION_TOOL_STATUS_EVENT`) so the gateway can give them
send-once/edit-in-place episode semantics without touching the
Telegram/Slack ``send_or_update_status`` path.

Coupling notes:
- ``COMPRESSION_ABORT_WARNING_PREFIX`` and
  ``CONTEXT_OVERFLOW_BLOCKED_WARNING_PREFIX`` MUST stay byte-identical with
  the emission sites (``agent/conversation_compression.py`` abort notice and
  ``CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE`` — both build their text from
  these constants). The gateway folds those raw warnings into the open
  episode by matching the prefixes.
- The routine compression status templates (COMPACTION_STATUS et al.) are
  NOT reworded here; the gateway noise filter
  (``_TELEGRAM_NOISY_STATUS_RE``) and
  ``tests/gateway/test_telegram_noise_filter.py`` pinned data stay coupled
  to those, unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Dedicated status_callback event type for the tool-style lifecycle. The
# gateway keys per-chat episode state off (adapter, chat_id) and edits one
# message per episode; platforms with send_or_update_status (Telegram/Slack)
# keep their existing per-status-key editing untouched.
COMPRESSION_TOOL_STATUS_EVENT = "compression_tool"

TOOL_NAME = "context_compress"
_START_GLYPH = "🗜️"
_SUCCESS_GLYPH = "✅"
_FAILURE_GLYPH = "⚠️"

COMPRESSION_TOOL_START_PREFIX = f"{_START_GLYPH} {TOOL_NAME} ·"
COMPRESSION_TOOL_SUCCESS_PREFIX = f"{_SUCCESS_GLYPH} {TOOL_NAME} ·"
COMPRESSION_TOOL_FAILURE_PREFIX = f"{_FAILURE_GLYPH} {TOOL_NAME} ·"

# Prefixes of the two raw failure-class notices an open episode replaces on
# edit-capable chat platforms. Emission sites build from these constants so
# the gateway matcher can never drift from the emitted wording.
COMPRESSION_ABORT_WARNING_PREFIX = "⚠ Compression aborted:"
CONTEXT_OVERFLOW_BLOCKED_WARNING_PREFIX = "⚠ Context is over the compression threshold"

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRETISH_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}|bearer\s+\S+|api[_-]?key\s*[=:]\s*\S+|token\s*[=:]\s*\S+)",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_compression_failure_reason(error: Any, *, max_chars: int = 140) -> str:
    """Short, user-safe one-line summary of a compression failure.

    Raw exception text can carry URLs, file paths, account/key material, or
    multi-line dumps; those stay in the logs. The gateway line gets a flat,
    truncated, redacted reason instead.
    """
    text = _WHITESPACE_RE.sub(" ", str(error or "")).strip()
    if not text:
        return "summary generation failed"
    text = _URL_RE.sub("[url]", text)
    text = _SECRETISH_RE.sub("[redacted]", text)
    text = _PATH_RE.sub("[path]", text)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def format_compressor_route(provider: Optional[str], model: Optional[str]) -> str:
    """``provider/model`` (or just ``model``); empty when the route is unknown."""
    provider = (provider or "").strip()
    model = (model or "").strip()
    if provider and model:
        return f"{provider}/{model}"
    return model or provider


def compression_tool_start_line(
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Opening line, posted before any expensive summary work.

    The aux compressor route is only resolved inside ``call_llm`` at call
    time (see agent/context_compressor.py — pre-resolving is forbidden), so
    an unpinned attempt honestly says "selecting compressor" instead of
    guessing the chat model or configured primary. A route is only named
    when one was actually pinned for this attempt (stall-fallback retry).
    """
    route = format_compressor_route(provider, model)
    who = route or "selecting compressor"
    return f"{COMPRESSION_TOOL_START_PREFIX} {who} — compressing conversation…"


def compression_tool_success_line(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    *,
    before_messages: Optional[int] = None,
    after_messages: Optional[int] = None,
    before_tokens: Optional[int] = None,
    after_tokens: Optional[int] = None,
    fallback_used: bool = False,
) -> str:
    """Terminal success line — emit ONLY when the compression boundary committed.

    Carries the actual aux route from compressor telemetry and genuine
    before/after counts when available; degrades to a concise completion
    otherwise. ``fallback_used`` names the fallback only when the engine
    actually started it (telemetry flag), never as a prediction.
    """
    parts = [COMPRESSION_TOOL_SUCCESS_PREFIX]
    route = format_compressor_route(provider, model)
    if route:
        parts.append(f" {route} ·")
    if (
        isinstance(before_messages, int)
        and isinstance(after_messages, int)
        and before_messages > 0
        and after_messages >= 0
    ):
        parts.append(f" {before_messages} → {after_messages} messages")
        if (
            isinstance(before_tokens, int)
            and isinstance(after_tokens, int)
            and before_tokens > 0
            and after_tokens > 0
        ):
            parts.append(f" (~{before_tokens:,} → ~{after_tokens:,} tokens)")
    else:
        parts.append(" done")
    if fallback_used:
        parts.append(" · via fallback route")
    return "".join(parts)


def compression_tool_failure_line(
    reason: Any,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    *,
    context_preservation: Optional[str] = "conversation unchanged",
) -> str:
    """Terminal failure line with sanitized reason + preservation fact + action.

    ``context_preservation`` is stated only where the engine guarantees it:
    the abort path returns the input transcript unmodified and performs no
    session rotation. Paths without that guarantee (mid-flight exceptions)
    pass ``None`` so the line never overclaims.
    """
    route = format_compressor_route(provider, model)
    who = f" {route} ·" if route else ""
    preserved = f" — {context_preservation}" if context_preservation else ""
    return (
        f"{COMPRESSION_TOOL_FAILURE_PREFIX}{who} failed: "
        f"{sanitize_compression_failure_reason(reason)}{preserved} "
        "· /compress to retry"
    )


def compression_tool_aborted_line(detail: Optional[str] = None) -> str:
    """Terminal line for no-op/cancelled/interrupted attempts (not failures).

    Used when an opened episode ends without a commit and without a summary
    error to report (cancellation, supersession, no-progress, timeout
    unwind). Never masquerades as success.
    """
    why = f" ({sanitize_compression_failure_reason(detail, max_chars=80)})" if detail else ""
    return (
        f"{COMPRESSION_TOOL_FAILURE_PREFIX} stopped before finishing{why} — "
        "conversation unchanged · /compress to retry"
    )


def cooldown_fold_suffix(reason: Optional[str] = None) -> str:
    """Suffix appended to the failed episode when the same attempt's cooldown
    warning arrives — joined into the episode instead of a second message.

    No countdown-to-retry: the raw reason's ``:30``-style seconds are
    dropped; only the block KIND is surfaced.
    """
    kind = (reason or "").split(":", 1)[0].strip().lower()
    if kind == "cooldown":
        phrase = "automatic compression is paused briefly (cooldown)"
    elif kind == "ineffective":
        phrase = "automatic compression is paused (recent attempts made no progress)"
    else:
        phrase = "automatic compression is temporarily blocked"
    return (
        f"\n⏸️ Still over the compression threshold — {phrase}. "
        "Run /compress to retry now, or /new to start a fresh session."
    )


def is_compression_tool_line(text: Any) -> bool:
    """True for any line of the tool-style lifecycle (start or terminal)."""
    text = str(text or "")
    return (
        text.startswith(COMPRESSION_TOOL_START_PREFIX)
        or text.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
        or text.startswith(COMPRESSION_TOOL_FAILURE_PREFIX)
    )


def is_compression_tool_terminal_line(text: Any) -> bool:
    """True for the terminal (success/failure/abort) lines only."""
    text = str(text or "")
    return text.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX) or text.startswith(
        COMPRESSION_TOOL_FAILURE_PREFIX
    )


def is_compression_tool_failure_line(text: Any) -> bool:
    """True for terminal lines that did NOT commit (failure/stopped)."""
    return str(text or "").startswith(COMPRESSION_TOOL_FAILURE_PREFIX)


def emit_compression_tool_status(agent: Any, line: str) -> bool:
    """Best-effort emit through the agent's status_callback; never raises.

    Routed directly through ``status_callback`` (not ``_emit_status``) so the
    CLI transcript keeps its existing lines and only callback-driven surfaces
    (gateways, TUI) see the tool-style episode. Returns True when the
    callback accepted the line — callers use that to know an episode is open.
    """
    callback = getattr(agent, "status_callback", None)
    if not callable(callback):
        return False
    try:
        callback(COMPRESSION_TOOL_STATUS_EVENT, line)
        return True
    except Exception:
        logger.debug("status_callback error in compression tool status", exc_info=True)
        return False
