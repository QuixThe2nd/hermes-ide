"""Generic mid-tool status/notice callback context.

Tools may emit a user-visible status line *while they are still running*
without mutating conversation messages (prompt-cache safe) and without
importing any messaging platform.

Bind a callback for the duration of a conversation turn or a single
``handle_function_call`` / ``invoke_tool`` dispatch. Unbound calls are a
no-op — the tool result can still carry the same information.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)

ToolStatusCallback = Callable[[str], None]

_tool_status_callback: ContextVar[Optional[ToolStatusCallback]] = ContextVar(
    "hermes_tool_status_callback",
    default=None,
)

# Branded agent-viewer status lines. ``delegate_claude_agent`` and
# ``delegate_cursor_agent`` announce a freshly spawned run's live viewer page
# through this callback while the run is still going; platform adapters render
# those exact lines as branded cards (Discord converts them into embeds). The
# prefixes are the producer/consumer contract: the emitting tools build their
# line from them, and the gateway uses them to recognize the line so it can be
# ordered against the tool-progress row. The adapters' own URL validators
# (``_claude_agent_status_url`` / ``_cursor_cloud_agent_status_url`` in
# plugins/platforms/discord/adapter.py) keep their copies for validation.
CLAUDE_AGENT_VIEWER_STATUS_PREFIX = "Claude Code Agent: "
CURSOR_AGENT_VIEWER_STATUS_PREFIX = "Cursor Cloud Agent: "

AGENT_VIEWER_STATUS_PREFIXES = (
    CLAUDE_AGENT_VIEWER_STATUS_PREFIX,
    CURSOR_AGENT_VIEWER_STATUS_PREFIX,
)


def is_agent_viewer_status_line(message: object) -> bool:
    """True when *message* is a branded agent-viewer status line."""
    text = str(message or "").strip()
    return any(text.startswith(prefix) for prefix in AGENT_VIEWER_STATUS_PREFIXES)


def get_tool_status_callback() -> Optional[ToolStatusCallback]:
    """Return the callback bound for this task, or ``None``."""
    return _tool_status_callback.get()


@contextmanager
def tool_status_scope(
    callback: Optional[ToolStatusCallback],
) -> Iterator[None]:
    """Bind *callback* for the current task. ``None`` leaves emits as no-ops."""
    token = _tool_status_callback.set(callback)
    try:
        yield
    finally:
        _tool_status_callback.reset(token)


def emit_tool_status(message: str) -> bool:
    """Invoke the bound mid-tool status callback.

    Returns True when a callback was present and accepted the message.
    Never raises. Does not append conversation messages.
    """
    callback = _tool_status_callback.get()
    if not callable(callback):
        return False
    try:
        callback(message)
        return True
    except Exception:
        logger.debug("tool status callback failed", exc_info=True)
        return False
