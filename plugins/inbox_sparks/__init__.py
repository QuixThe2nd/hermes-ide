"""Inbox Sparks — end-of-turn prompt to consider starting conversations.

Subscribes to the ``pre_turn_end`` gate (fired by ``agent/conversation_loop.py``
right before a turn finalizes) and, at most once per cooldown window, returns
one continuation directive asking the agent to decide whether anything in the
conversation or its wider context is worth starting a new conversation with
the user about via the ``start_conversation`` tool shipped by the
``hermes_starts`` plugin.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Rate limit: the directive fires at most once per window, persisted in a
# 0600 state file so every Hermes process on the host shares one budget.
DEFAULT_COOLDOWN_MINUTES = 240
_STATE_FILENAME = "state.json"

DIRECTIVE = (
    "Before finishing, decide whether anything in this conversation or your "
    "wider context is worth starting a new conversation with the user about "
    "using the start_conversation tool. You may call it zero or more times. "
    "If nothing clears the bar of 'mildly interesting', do nothing."
)


def _state_path() -> Path:
    return get_hermes_home() / "inbox_sparks" / _STATE_FILENAME


def _read_last_directive_at() -> float:
    try:
        with _state_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return 0.0
        value = data.get("last_directive_at")
        return float(value) if isinstance(value, (int, float)) else 0.0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def _write_last_directive_at(timestamp: float) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump({"last_directive_at": timestamp}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _cooldown_active(cooldown_minutes: int, now: Optional[float] = None) -> bool:
    if cooldown_minutes <= 0:
        return False
    last = _read_last_directive_at()
    if last <= 0.0:
        return False
    return (now if now is not None else time.time()) - last < cooldown_minutes * 60


def _handle_pre_turn_end(cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the one-per-window continuation directive, or ``None``.

    Never raises: the end-of-turn gate must not be able to break the loop.
    The directive is stamped BEFORE it is returned, so a state-write failure
    means no directive this turn (retry next turn) rather than an
    unpersisted directive that could fire every turn.
    """
    try:
        if _cooldown_active(cooldown_minutes):
            return None
        _write_last_directive_at(time.time())
        logger.debug(
            "inbox_sparks directive issued (session=%s)",
            kwargs.get("session_id") or "",
        )
        return {"action": "continue", "message": DIRECTIVE}
    except Exception:
        logger.debug("inbox_sparks pre_turn_end handler failed", exc_info=True)
        return None


def _resolve_cooldown_minutes(ctx: Any = None) -> int:
    """Plugin settings win (``cooldown_minutes``), else the default."""
    get_config = getattr(ctx, "get_config", None) if ctx is not None else None
    raw = ""
    if callable(get_config):
        try:
            raw = str(get_config("cooldown_minutes", "") or "").strip()
        except Exception:
            raw = ""
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_MINUTES


def register(ctx) -> None:
    cooldown_minutes = _resolve_cooldown_minutes(ctx)

    def _on_pre_turn_end(**kwargs: Any) -> Optional[Dict[str, Any]]:
        return _handle_pre_turn_end(cooldown_minutes=cooldown_minutes, **kwargs)

    ctx.register_hook("pre_turn_end", _on_pre_turn_end)
