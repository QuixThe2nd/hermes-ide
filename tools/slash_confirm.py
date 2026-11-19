"""Generic slash-command confirmation primitive (gateway-side).

Adapters render the registered choices and call ``resolve()``; text-only adapters use the
gateway's reply interception. State is module-level so adapters need no runner backreference.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# session_key -> {"confirm_id", "command", "handler", "created_at", "allowed_choices"}
_pending: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()

# Older pending confirms are discarded when the session's next message arrives (buttons live
# as long as the adapter keeps callback_data).
DEFAULT_TIMEOUT_SECONDS = 300


def register(session_key: str, confirm_id: str, command: str,
             handler: Callable[[str], Awaitable[Optional[str]]],
             allowed_choices: tuple[str, ...] = ("once", "always", "cancel")) -> None:
    """Register a pending confirm, superseding any prior one for the session."""
    with _lock:
        _pending[session_key] = {
            "confirm_id": confirm_id, "command": command, "handler": handler,
            "created_at": time.time(), "allowed_choices": allowed_choices,
        }


def get_pending(session_key: str) -> Optional[Dict[str, Any]]:
    """Return a copy of the pending confirm dict for a session, or None."""
    with _lock:
        entry = _pending.get(session_key)
        return dict(entry) if entry else None


def clear(session_key: str) -> None:
    """Drop the pending confirm for ``session_key`` without running it."""
    with _lock:
        _pending.pop(session_key, None)


def _is_stale(entry: Dict[str, Any], timeout: float) -> bool:
    return time.time() - float(entry.get("created_at", 0) or 0) > timeout


def clear_if_stale(session_key: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Drop the pending confirm if older than ``timeout`` seconds; True if dropped."""
    with _lock:
        entry = _pending.get(session_key)
        stale = bool(entry and _is_stale(entry, timeout))
        if stale:
            _pending.pop(session_key, None)
        return stale


async def resolve(session_key: str, confirm_id: str, choice: str,
                  timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Optional[str]:
    """Run a pending handler with an allowed choice."""
    with _lock:
        entry = _pending.get(session_key)
        if not entry or entry.get("confirm_id") != confirm_id:
            return None
        if choice not in entry.get("allowed_choices", ("once", "always", "cancel")):
            return "This prompt does not offer that choice. Reply /cancel to dismiss it."
        # Pop before running so duplicate callbacks cannot run it twice.
        _pending.pop(session_key, None)
        if _is_stale(entry, timeout):
            return None
        handler = entry.get("handler")
        command = entry.get("command", "?")

    if not handler:
        return None
    try:
        result = await handler(choice)
    except Exception as exc:
        logger.error("Slash-confirm handler for /%s raised: %s", command, exc, exc_info=True)
        return f"❌ Error handling confirmation: {exc}"
    return result if isinstance(result, str) else None


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import asyncio  # noqa: F401,E402

def resolve_sync_compat(
    loop: asyncio.AbstractEventLoop,
    session_key: str,
    confirm_id: str,
    choice: str,
) -> Optional[str]:
    """Synchronous helper: schedule resolve() on a loop and wait for the result.

    Used by platform callback paths that run on a different thread than the
    event loop (e.g. Discord's button click handler in some configurations).
    Prefer the async ``resolve()`` from an async context.
    """
    try:
        from agent.async_utils import safe_schedule_threadsafe
        fut = safe_schedule_threadsafe(
            resolve(session_key, confirm_id, choice), loop,
            logger=logger,
            log_message="resolve_sync_compat scheduling failed",
        )
        if fut is None:
            return None
        return fut.result(timeout=30)
    except Exception as exc:
        logger.error("resolve_sync_compat failed: %s", exc)
        return None
# ---- END PLUGIN-COMPAT ----
