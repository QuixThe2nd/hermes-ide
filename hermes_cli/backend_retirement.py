"""Process-owned admission fence for cooperative Desktop backend retirement.

The gate belongs to the process, not a profile or transport. Diagnostic ledgers alone cannot
close the check/use race: every source of new work must reserve admission before dispatch.
"""

from contextlib import contextmanager
import logging
import secrets
import threading
import time
from typing import Callable, Optional


class RetirementFence:
    """Exclusive 30-second prepare permit; commit closes admission permanently.

    Tokens are random and recognized only by this process instance. A committed prepare
    returns the same token so a client can recover a lost commit response without reopening.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._preparing = False
        self._token = None
        self._committed = False
        self._expires_at = 0.0
        self._now = time.monotonic

    def _expire(self):
        if self._token is not None and not self._committed and self._now() >= self._expires_at:
            self._token = None

    def acquire(self):
        with self._lock:
            self._expire()
            if self._preparing or self._token is not None:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            self._active -= 1

    def active_count(self):
        with self._lock:
            return self._active

    @contextmanager
    def work(self):
        admitted = self.acquire()
        try:
            yield admitted
        finally:
            if admitted:
                self.release()

    def prepare(self):
        idle_proof = globals()["idle_proof"]

        with self._lock:
            self._expire()
            if self._committed:
                return {"ok": True, "idle": True, "token": self._token}
            if self._preparing or self._token is not None or self._active:
                return {"ok": False, "idle": False}
            self._preparing = True
        # No admissions can cross the freeze. Do not hold our lock while reading ledgers:
        # some callers already hold a session/cron lock when reserving their work.
        verdict = None
        try:
            verdict = idle_proof()["idle"]
        except Exception:
            logging.getLogger(__name__).warning("Retirement idle probe unavailable", exc_info=True)
        finally:
            with self._lock:
                self._preparing = False
                if verdict is True:
                    self._token = token = secrets.token_urlsafe(32)
                    self._expires_at = self._now() + 30.0
        if verdict is not True:
            return {"ok": False, "idle": verdict}
        return {"ok": True, "idle": True, "token": token}

    def commit(self, token):
        with self._lock:
            self._expire()
            if not token or token != self._token:
                return {"ok": False}
            self._committed = True
            return {"ok": True}

    def cancel(self, token):
        with self._lock:
            self._expire()
            if not token or token != self._token or self._committed:
                return {"ok": False}
            self._token = None
            return {"ok": True}


retirement = RetirementFence()


# --- Idle proof (moved from the removed dashboard's web_server_idle_proof /
# web_server_idle_exit; backend retirement is the surviving consumer) ---

_log = logging.getLogger(__name__)

_input_probe_failure_logged = False
_probe_failure_logged = False


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


def idle_proof(turn_probe: Callable[[], "bool | str | None"] = None,
               input_probe: Callable[[], Optional[int]] = pending_human_input) -> dict:
    """``{"idle": True | False | None, "reason": str}`` plus ``"detail"`` naming the busy ledger.

    ``True`` only when no turn is in flight (session table AND cron ledger) and nothing is waiting
    on a human. ``None`` whenever either probe is indeterminate — the caller treats it exactly like
    busy. ``turn_probe`` may answer with a bool or with :func:`busy_ledger`'s name; the name is
    surfaced as ``detail`` so a backend that refuses to retire says which work it is protecting.
    """
    if turn_probe is None:
        turn_probe = busy_ledger
    turn = turn_probe()
    if turn is None:
        return {"idle": None, "reason": "turn_probe_unavailable"}
    if turn:
        return {"idle": False, "reason": "turn_in_flight", "detail": turn if isinstance(turn, str) else None}
    pending = input_probe()
    if pending is None:
        return {"idle": None, "reason": "input_probe_unavailable"}
    if pending:
        return {"idle": False, "reason": "awaiting_human_input"}
    return {"idle": True, "reason": None}


def _session_work_in_flight(session: dict) -> bool:
    if any(session.get(key) for key in ("running", "queued_prompt", "queued_prompts", "_auto_continue_scheduled")):
        return True
    return any(thread.is_alive() for key in ("_run_thread", "_agent_build_thread")
               if (thread := session.get(key)) is not None)


def busy_ledger() -> Optional[str]:
    """Name the ledger that holds work — ``"retirement_admission"``, ``"session:<id>"``,
    ``"delegation"``, ``"cron:<job ids>"`` — ``""`` when every ledger is empty, ``None`` when one
    cannot be read. The idle probe reports this so a backend that will not retire says
    WHICH work it is protecting, instead of an opaque "turn in flight".

    Session work lives on ``tui_gateway.server``. Cron runs live outside that table
    (``cron.scheduler.get_running_job_ids``, the same ledger the gateway shutdown drain reads):
    without it a daily job mid-run reported "no turn" and the exit killed it (#107485). None keeps
    the backend alive forever, so the cause is logged once — a silent never-exits would be the
    original bug with a new face."""
    global _probe_failure_logged
    try:
        import tui_gateway.server as gateway

        if retirement.active_count():
            return "retirement_admission"
        with gateway._sessions_lock:
            busy_sessions = [sid for sid, s in gateway._sessions.items() if _session_work_in_flight(s)]
        if busy_sessions:
            return "session:" + ",".join(str(sid) for sid in busy_sessions)
        from tools.async_delegation import active_count
        from cron.scheduler import get_running_job_ids
        if active_count():
            return "delegation"
        running_jobs = get_running_job_ids()
        return "cron:" + ",".join(sorted(running_jobs)) if running_jobs else ""
    except Exception:
        if not _probe_failure_logged:
            _probe_failure_logged = True
            _log.warning("idle-exit turn probe unavailable; this backend will not self-retire", exc_info=True)
        return None
