"""Bridge the gateway's native turn-progress bubble to plugins.

Why this exists
---------------
The native per-turn progress bubble (``gateway/run_turn_runner.py``) is the only progress
surface Hermes owns end to end: it edits one message in place, throttles its own edits
(``EDIT_INTERVAL``), splits overflow at the platform text limit, honours
``cleanup_progress`` and survives mid-run restarts. Plugins had no way to feed it: there is
no outbound-delivery hook, ``send_message`` is deliberately not registered as a tool, and
``ctx.platform_actions`` v1 only knows ``add_reaction`` / ``set_thread_title``.

This module registers the running turn's queue so a plugin hook can append pre-formatted
lines — or swap the whole body with ``("__body__", text)`` — through ``ctx.progress()``.

Contract
--------
Enabled by ``display.tool_progress: plugin`` (per platform:
``display.platforms.<platform>.tool_progress: plugin``); in that mode the core stops
queueing its own tool lines and the plugin renders the content. Fail-open by design: with
no queue registered every call is a no-op returning ``False``, so CLI sessions, plugin work
outside a turn, and a tree without the mode all keep working.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_cli.progress_bridge")

_LOCK = threading.Lock()
# session_id (agent hooks) / session_key (gateway routing) -> the turn's progress queue
_BY_SESSION: Dict[str, Any] = {}
# Fallback for callers that cannot name a session: the most recently started turn.
_LAST: Dict[str, Any] = {"queue": None, "at": 0.0}


def register_progress_queue(*, queue: Any, session_key: Optional[str] = None,
                            session_id: Optional[str] = None) -> None:
    """Register the live progress queue of a running turn (called by the gateway)."""
    if queue is None:
        return
    with _LOCK:
        for key in (session_key, session_id):
            if key:
                _BY_SESSION[str(key)] = queue
        _LAST["queue"] = queue
        _LAST["at"] = time.time()


def unregister_progress_queue(*, queue: Any = None, session_key: Optional[str] = None,
                              session_id: Optional[str] = None) -> None:
    """Drop the registry entry when the turn ends (idempotent, never raises)."""
    with _LOCK:
        for key in (session_key, session_id):
            if key:
                _BY_SESSION.pop(str(key), None)
        if queue is not None:
            for key in [k for k, v in _BY_SESSION.items() if v is queue]:
                _BY_SESSION.pop(key, None)
            if _LAST["queue"] is queue:
                _LAST["queue"] = None


def resolve_queue(*, session_key: Optional[str] = None, session_id: Optional[str] = None) -> Any:
    with _LOCK:
        for key in (session_id, session_key):
            if key and str(key) in _BY_SESSION:
                return _BY_SESSION[str(key)]
        return _LAST["queue"] if _LAST["queue"] is not None else None


def push_progress(text: Any, *, session_key: Optional[str] = None,
                  session_id: Optional[str] = None) -> bool:
    """Append one item (plain line or native event dict) to the turn's progress bubble.

    Returns True when the item was queued. Never raises: a plugin must not be able to
    break a turn through this bridge.
    """
    try:
        queue = resolve_queue(session_key=session_key, session_id=session_id)
        if queue is None:
            return False
        queue.put_nowait(text)
        return True
    except Exception:  # pragma: no cover - defensive, bridge must fail open
        logger.debug("progress bridge push failed", exc_info=True)
        return False


def active_sessions() -> Dict[str, Any]:
    """Diagnostics only: which sessions currently have a live progress queue."""
    with _LOCK:
        return dict(_BY_SESSION)
