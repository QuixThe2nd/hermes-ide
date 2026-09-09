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

import contextvars
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

# Per-attempt operation id, carried across the synchronous status_callback
# hop via a ContextVar so the gateway can attribute every lifecycle event to
# the exact attempt that emitted it (a superseded zombie attempt's late
# terminal must not overwrite its successor's episode). Set/reset inside
# emit_compression_tool_status; read by gateway/run.py at decision time.
_ATTEMPT_TOKEN_CV: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_compression_tool_attempt_token", default=None
)


def current_compression_attempt_token() -> Optional[str]:
    """Attempt token of the in-flight emit, or None outside one."""
    return _ATTEMPT_TOKEN_CV.get()

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
# Mention/markdown metacharacters must never reach a chat surface via an
# interpolated error or model label (@everyone pings, backtick fences, ...).
_UNSAFE_LABEL_CHARS_RE = re.compile(r"[^A-Za-z0-9._:/\-]+")
_UNSAFE_TEXT_CHARS_RE = re.compile(r"[@`*|~<>#]")

# Short, user-safe failure CATEGORIES. Raw exception text (URLs, paths,
# account material, multi-line dumps) stays in the logs; the chat line gets
# one calm phrase. Order matters: first match wins.
_FAILURE_CATEGORY_RULES = (
    (re.compile(r"timed?[\s_]?out|deadline", re.IGNORECASE), "summary provider timed out"),
    (
        re.compile(
            r"\b(401|403)\b|unauthori[sz]ed|forbidden|authentication|invalid api key",
            re.IGNORECASE,
        ),
        "summary provider authentication failed",
    ),
    (
        re.compile(r"\b429\b|rate[\s_]?limit|too many requests", re.IGNORECASE),
        "summary provider rate-limited",
    ),
    (
        re.compile(r"empty (content|summary|response)|returned empty", re.IGNORECASE),
        "summary provider returned an empty summary",
    ),
    (
        re.compile(r"truncat|finish.?reason|output (token )?cap", re.IGNORECASE),
        "summary was truncated by the provider",
    ),
    (
        re.compile(
            r"connection|connect|network|dns|refused|reset by peer|broken pipe|"
            r"stream (stalled|closed|error)|eof",
            re.IGNORECASE,
        ),
        "summary provider connection failed",
    ),
    (
        re.compile(r"\b5\d\d\b|internal server error|bad gateway|service unavailable|overloaded", re.IGNORECASE),
        "summary provider returned a server error",
    ),
    (
        re.compile(r"cancelled|canceled|interrupt", re.IGNORECASE),
        "summary generation was interrupted",
    ),
)


def sanitize_compression_failure_reason(error: Any, *, max_chars: int = 140) -> str:
    """Short, user-safe one-line category for a compression failure.

    Recognized failure shapes map to a fixed calm phrase (e.g. "summary
    provider timed out"); anything unrecognized degrades to the exception
    class name, or — for plain strings — redacted, injection-stripped,
    truncated text. Raw diagnostics stay in the logs.
    """
    text = _WHITESPACE_RE.sub(" ", str(error or "")).strip()
    if not text:
        return "summary generation failed"
    lowered_source = text
    for pattern, category in _FAILURE_CATEGORY_RULES:
        if pattern.search(lowered_source):
            return category
    if isinstance(error, BaseException):
        return f"summary generation failed ({type(error).__name__})"
    text = _URL_RE.sub("[url]", text)
    text = _SECRETISH_RE.sub("[redacted]", text)
    text = _PATH_RE.sub("[path]", text)
    text = _UNSAFE_TEXT_CHARS_RE.sub("", text).strip()
    if not text:
        return "summary generation failed"
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _sanitize_route_label(value: Optional[str], *, max_chars: int = 60) -> str:
    """Provider/model labels come from config or route telemetry — strip
    anything that could ping or break chat markdown before display."""
    cleaned = _UNSAFE_LABEL_CHARS_RE.sub("", str(value or "")).strip(".-/:")
    return cleaned[:max_chars]


def format_compressor_route(provider: Optional[str], model: Optional[str]) -> str:
    """``provider/model`` (or just ``model``); empty when the route is unknown."""
    provider = _sanitize_route_label(provider)
    model = _sanitize_route_label(model)
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
    local_summary: Optional[str] = None,
) -> str:
    """Terminal success line — emit ONLY when the compression boundary committed.

    Carries the actual aux route from compressor telemetry and genuine
    before/after counts when available; degrades to a concise completion
    otherwise. ``local_summary`` (``"unavailable"`` / ``"skipped"``) is set
    when the engine inserted its deterministic LOCAL summary instead of a
    provider one — that is not a provider "fallback route", so the line names
    no route for it and never credits the failed/skipped provider.
    """
    parts = [COMPRESSION_TOOL_SUCCESS_PREFIX]
    route = format_compressor_route(provider, model)
    if route and not local_summary:
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
    if local_summary == "unavailable":
        parts.append(" · provider summary unavailable — used local deterministic summary")
    elif local_summary:
        parts.append(" · local deterministic summary")
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


def compression_tool_aborted_line(
    detail: Optional[str] = None,
    *,
    context_preservation: Optional[str] = "conversation unchanged",
) -> str:
    """Terminal line for no-op/cancelled/interrupted attempts (not failures).

    Used when an opened episode ends without a commit and without a summary
    error to report (cancellation, supersession, no-progress, timeout
    unwind). Never masquerades as success. ``context_preservation`` is stated
    only where the engine guarantees the transcript survived unmodified —
    rollback-failed and mid-rotation unwind paths pass ``None`` so the line
    never overclaims.
    """
    why = f" ({sanitize_compression_failure_reason(detail, max_chars=80)})" if detail else ""
    preserved = f" — {context_preservation}" if context_preservation else ""
    return (
        f"{COMPRESSION_TOOL_FAILURE_PREFIX} stopped before finishing{why}{preserved} "
        "· /compress to retry"
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
    # One action only: /compress retries now. /new is reserved for a real
    # hard limit, not a threshold cooldown.
    return (
        f"\n⏸️ Still over the compression threshold — {phrase}. "
        "Run /compress to retry now."
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


def emit_compression_tool_status(
    agent: Any, line: str, *, attempt_token: Optional[str] = None
) -> bool:
    """Best-effort emit through the agent's status_callback; never raises.

    Routed directly through ``status_callback`` (not ``_emit_status``) so the
    CLI transcript keeps its existing lines. The surface is Discord-only:
    sessions on any other platform (CLI/TUI/Telegram/Slack/...) get NO event
    at all, keeping their status behavior byte-identical — the gateway's
    episode rail (gateway/run.py) is the sole consumer. ``attempt_token``
    identifies the emitting attempt to the gateway across the synchronous
    callback hop. Returns True when the callback accepted the line — callers
    use that to know an episode is open.
    """
    if str(getattr(agent, "platform", "") or "").strip().lower() != "discord":
        return False
    callback = getattr(agent, "status_callback", None)
    if not callable(callback):
        return False
    token = _ATTEMPT_TOKEN_CV.set(attempt_token)
    try:
        callback(COMPRESSION_TOOL_STATUS_EVENT, line)
        return True
    except Exception:
        logger.debug("status_callback error in compression tool status", exc_info=True)
        return False
    finally:
        _ATTEMPT_TOKEN_CV.reset(token)
