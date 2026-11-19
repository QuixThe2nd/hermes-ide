"""Time-bounded extended retry budget for transport-class API failures.

2026-11-12 incident: a ~25-minute WAN outage made every turn burn its
``api_max_retries`` (4 attempts, ~8 min) and die, even though the link
recovered intermittently. Transport/transient verdicts (timeout /
server_error / overloaded, retryable) now earn extra attempts inside a
configurable time window (``agent.transport_retry_window_seconds``, default
25 min) with capped exponential backoff + jitter, clamped to the remaining
window. Non-transient verdicts (401/403 auth, 400 format, context-length,
rate limits) keep the existing fast-fail.

The budget bookkeeping lives on the AGENT (``_transport_retry_deadline`` /
``_transport_retry_attempts``), not on ``TurnRetryState`` — a fresh state is
built per API-call iteration, while the window spans the whole user turn.
The turn prolog resets both fields at the top of every user turn.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from agent.error_classifier import FailoverReason
from agent.retry_utils import jittered_backoff

logger = logging.getLogger("agent.conversation_loop")

# Infrastructure-shaped failures: the same request can succeed once the link
# or the upstream recovers. Everything else (auth, format, context-length,
# rate limits, billing) keeps its own policy.
_TRANSPORT_RETRY_REASONS = frozenset({
    FailoverReason.timeout,
    FailoverReason.server_error,
    FailoverReason.overloaded,
})

DEFAULT_TRANSPORT_RETRY_WINDOW_SECONDS = 1500.0
_TRANSPORT_RETRY_BASE_DELAY = 5.0
_TRANSPORT_RETRY_MAX_DELAY = 120.0


def is_transport_retry_classified(classified: Any) -> bool:
    """True when a classified error is a transient transport failure the
    extended window may keep retrying."""
    reason = getattr(classified, "reason", None)
    return bool(getattr(classified, "retryable", False)) and reason in _TRANSPORT_RETRY_REASONS


def next_transport_retry(agent: Any, _retry: Any, classified: Any, now: Optional[float] = None) -> Optional[float]:
    """Schedule the next extended attempt: capped exponential backoff + jitter
    clamped to the remaining window.

    Returns the wait in seconds, or ``None`` when the budget is closed (error
    not transport-class, window disabled/``<= 0``, or the window expired).
    The deadline is stamped on the first qualifying call and the attempts
    counter increments on every scheduled wait."""
    if not is_transport_retry_classified(classified):
        return None
    window = float(getattr(agent, "_transport_retry_window_seconds", 0.0) or 0.0)
    if window <= 0:
        return None
    now = time.monotonic() if now is None else now
    deadline = getattr(agent, "_transport_retry_deadline", None)
    if deadline is None:
        deadline = now + window
        agent._transport_retry_deadline = deadline
    remaining = deadline - now
    if remaining <= 0:
        return None

    attempts = getattr(agent, "_transport_retry_attempts", 0) + 1
    agent._transport_retry_attempts = attempts
    wait = jittered_backoff(
        attempts, base_delay=_TRANSPORT_RETRY_BASE_DELAY, max_delay=_TRANSPORT_RETRY_MAX_DELAY)
    return min(wait, remaining)
