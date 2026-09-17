"""Backend-proven idleness for a Desktop-pooled ``hermes serve`` child.

The Desktop caps local pooled backends (3 by default) and keeps a hard slot lease for the lifetime
of each child. Its renderer refreshes ``lastActiveAt`` every 60s for every open socket, so a
bot-tile-pinned resident is "fresh" forever even when it is doing nothing — occupied is not busy.
When a foreground open finds the pool full, the Desktop may retire one resident, but only one the
BACKEND itself can prove idle. The renderer's own turn bookkeeping cannot see cron fires
(``HERMES_DESKTOP=1`` runs the in-process ticker), messaging-platform turns served by a pooled
backend, or a session blocked on an approval, so it is never the proof.

:func:`idle_proof` reads the same ledgers the SSH idle-exit watchdog trusts
(:func:`hermes_cli.web_server_idle_exit.turn_in_flight`: running gateway sessions plus running cron
jobs) and adds the human-input ledgers (open server→client requests, unresolved gateway approvals).
It fails closed: anything it cannot read yields ``idle: None`` and the Desktop must not retire.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from hermes_cli.web_server_idle_exit import turn_in_flight

_log = logging.getLogger(__name__)

_input_probe_failure_logged = False


def pending_human_input() -> Optional[int]:
    """Count of prompts waiting on a human (clarify/approval/sudo/secret requests plus queued
    gateway approvals); ``None`` when a ledger cannot be read."""
    global _input_probe_failure_logged
    try:
        from tui_gateway import server_requests
        from tools.approval import pending_gateway_approval_count

        return server_requests.open_request_count() + pending_gateway_approval_count()
    except Exception:
        if not _input_probe_failure_logged:
            _input_probe_failure_logged = True
            _log.warning("idle-proof input probe unavailable; this backend will report indeterminate",
                         exc_info=True)
        return None


def idle_proof(turn_probe: Callable[[], Optional[bool]] = turn_in_flight,
               input_probe: Callable[[], Optional[int]] = pending_human_input) -> dict:
    """``{"idle": True | False | None, "reason": str | None}``.

    ``True`` only when no turn is in flight (session table AND cron ledger) and nothing is waiting
    on a human. ``None`` whenever either probe is indeterminate — the caller treats it exactly like
    busy.
    """
    turn = turn_probe()
    if turn is None:
        return {"idle": None, "reason": "turn_probe_unavailable"}
    if turn:
        return {"idle": False, "reason": "turn_in_flight"}
    pending = input_probe()
    if pending is None:
        return {"idle": None, "reason": "input_probe_unavailable"}
    if pending:
        return {"idle": False, "reason": "awaiting_human_input"}
    return {"idle": True, "reason": None}
