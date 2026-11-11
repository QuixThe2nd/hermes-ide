"""Shared wording for the exec-approval prompt every messaging surface renders.

The button card (``BasePlatformAdapter._format_exec_approval``) and the plain-text
``/approve`` fallback (``gateway.run._format_exec_approval_fallback``) must tell the user
the same three things: what Hermes wants to run, why it was flagged, and that silence means
the command does NOT run once the approval window elapses. Keeping the text here means one
edit changes every platform; adapters only wrap these strings in their own markup.

No imports from ``gateway.platforms.base`` or ``gateway.run`` — both import this module.
"""

from __future__ import annotations

# Bare strings; adapters add their own bold/HTML around them.
EA_HEADER_TEXT = "Hermes wants to run a command that needs your OK"
EA_REASON_LABEL_TEXT = "Why it was flagged"

# Timeout notice posted when nobody answered the prompt (``{window}`` = "5 minutes").
APPROVAL_TIMED_OUT_NOTICE = (
    "⌛ Approval timed out after {window} — the command was NOT run. "
    "Ask me to try again if you still want it.")


def approval_timeout_seconds() -> int:
    # Fixed 300s window — the configurable approval-timeout key no longer exists.
    return 300


def format_approval_window(seconds: int) -> str:
    """Human wording for a timeout (300 → "5 minutes")."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} hour{'s' if seconds != 3600 else ''}"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} minute{'s' if seconds != 60 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def format_approval_deadline_line(timeout_s: int) -> str:
    """The last line of every approval prompt: doing nothing is a safe no."""
    return f"If you don't answer within {format_approval_window(timeout_s)} it will NOT run."


def format_approval_timed_out_notice(timeout_s: int) -> str:
    return APPROVAL_TIMED_OUT_NOTICE.format(window=format_approval_window(timeout_s))
