"""Interrupt / steer / redirect control surface for ``AIAgent``.

The runtime method bodies live on ``AIAgent``. ``interrupt_issuer`` remains here for the
turn-finalization and lifecycle callers that share its attribution rules.
"""
from typing import Optional

# ``interrupt()`` categories that mean a human stopped the turn. Any other ``_tool_interrupt_reason`` was
# supplied by a system producer via ``tool_reason`` (watchdogs, lease loss, lifecycle cancellation) and is
# attributed to it in the turn exit reason instead of being booked as a user stop (#112647).
_REASON_HARD_STOP = "explicit stop requested"
_REASON_NEW_MESSAGE = "user sent a new message"
_REASON_USER_INTERRUPT = "user interrupt"
USER_INTERRUPT_REASONS = frozenset({_REASON_HARD_STOP, _REASON_NEW_MESSAGE, _REASON_USER_INTERRUPT})


def interrupt_issuer(agent) -> Optional[str]:
    """Slug of the system producer behind the pending interrupt, or ``None`` for a human stop."""
    reason = getattr(agent, "_tool_interrupt_reason", None)
    if not reason or reason in USER_INTERRUPT_REASONS:
        return None
    return str(reason).strip().replace(" ", "_")


class InterruptControlMixin:
    """interrupt()/hard_interrupt()/clear_interrupt()/steer()/redirect() composition marker."""
