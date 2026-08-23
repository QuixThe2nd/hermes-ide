"""Verification-loop helpers for the ``pre_verify`` round-end gate.

When the agent has edited code and is about to verify/finish, the loop fires the
``pre_verify`` hook (user directives resolved by
:func:`hermes_cli.plugins.get_pre_verify_continue_message`). A directive keeps
the agent going one more turn — run a check, defer it, tidy the diff — instead of
stopping immediately.

The shipped coding guidance lives on the evidence-based verification-stop nudge
(``agent/verification_stop.py``), not as a second default stop gate. That keeps
the default token cost tied to the existing "missing verification evidence"
decision while preserving ``pre_verify`` for user/plugin policy.

The sibling ``pre_turn_end`` gate (every turn, not only code edits) resolves its
directive budget here too via :func:`max_pre_turn_end_nudges`.
"""

from __future__ import annotations

from typing import Any, Optional

from utils import is_truthy_value

DEFAULT_MAX_VERIFY_NUDGES = 3

# ``pre_turn_end`` fires on EVERY turn end, so its budget must be tight by
# default (one directive per turn) and hard-capped: an unbounded value here
# would let a misbehaving hook make a single turn never finish.
DEFAULT_MAX_PRE_TURN_END_NUDGES = 1
MAX_PRE_TURN_END_NUDGE_CAP = 2

# Shipped guidance appended to the verification-stop nudge when code lacks fresh
# verification evidence. Wording mirrors the user-facing "clean your work"
# workflow, but does not create its own extra model turn.
CODING_VERIFY_GUIDANCE = (
    "[Coding] Before you run tests/linters or call this done: if this is "
    "creative UI/visual work, hold off on tests and linters until the user says "
    "they like the result or you're about to commit. And before every commit, "
    "clean your work: keep it KISS/DRY, match the surrounding code style, and be "
    "elitist, shorthand, clever, concise, efficient, and elegant."
)


def max_verify_nudges(config: Optional[dict[str, Any]] = None) -> int:
    """Bound on consecutive ``pre_verify`` continue directives per turn (>= 0)."""
    agent_cfg = _agent_cfg(config)
    raw = agent_cfg.get("max_verify_nudges")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_VERIFY_NUDGES


def max_pre_turn_end_nudges(config: Optional[dict[str, Any]] = None) -> int:
    """Bound on consecutive ``pre_turn_end`` continue directives per turn.

    Same config-resolution shape as :func:`max_verify_nudges`
    (``agent.max_pre_turn_end_nudges``) but clamped to
    ``0..MAX_PRE_TURN_END_NUDGE_CAP`` — the hook fires at every turn end, so
    a configured value above the cap is treated as the cap, not honored.
    """
    agent_cfg = _agent_cfg(config)
    raw = agent_cfg.get("max_pre_turn_end_nudges")
    try:
        value = max(0, int(raw))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_PRE_TURN_END_NUDGES
    return min(value, MAX_PRE_TURN_END_NUDGE_CAP)


def coding_verify_guidance(config: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return the optional guidance appended to verification-stop nudges."""
    if not is_truthy_value(_agent_cfg(config).get("verify_guidance", True), default=True):
        return None
    return CODING_VERIFY_GUIDANCE


def _agent_cfg(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    agent_cfg = (config or {}).get("agent") if isinstance(config, dict) else None
    return agent_cfg if isinstance(agent_cfg, dict) else {}


__all__ = [
    "CODING_VERIFY_GUIDANCE",
    "DEFAULT_MAX_PRE_TURN_END_NUDGES",
    "DEFAULT_MAX_VERIFY_NUDGES",
    "MAX_PRE_TURN_END_NUDGE_CAP",
    "coding_verify_guidance",
    "max_pre_turn_end_nudges",
    "max_verify_nudges",
]
