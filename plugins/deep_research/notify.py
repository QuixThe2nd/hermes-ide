"""Completion re-entry for research jobs.

The runner is a separate host process, so it cannot touch the gateway's
in-memory queue. Instead a small daemon watcher (started from
``on_gateway_start``) notices terminal jobs whose ``notified`` flag is still
clear, flips it exactly once, and pushes an ``async_delegation``-shaped event
onto ``process_registry.completion_queue`` — the same infrastructure
``delegate_task``/missions use, so the outcome re-enters the originating
session without polling.

Losing the notification costs nothing durable: ``status``/``result`` read the
artifacts on disk, so a gateway restart (or a CLI-origin job the gateway cannot
route) still recovers the result on demand.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_process_hermes_home
from plugins.deep_research import jobs

logger = logging.getLogger("hermes.plugins.deep_research.notify")

# Reuse the async-delegation delivery lane; the gateway's watcher coalesces
# these per originating session.
EVENT_TYPE = "async_delegation"
SUMMARY_CHARS = 700


def origin_context(session_id: Optional[str], task_id: Optional[str] = None) -> Dict[str, Any]:
    """Origin identifiers recorded in ``request.json`` for completion routing.

    ``session_key`` is resolved best-effort from the session store; without it a
    CLI-origin job simply has no gateway route and is recovered via
    ``status``/``result`` instead.
    """
    origin: Dict[str, Any] = {
        "session_id": str(session_id or ""),
        "task_id": str(task_id or ""),
        "session_key": "",
    }
    if not origin["session_id"]:
        return origin
    try:
        from hermes_state import SessionDB

        db_path = get_process_hermes_home() / "state.db"
        if not db_path.exists():
            return origin
        row = SessionDB(db_path=db_path).get_session(origin["session_id"])
        if row:
            origin["session_key"] = str(row.get("session_key") or "")
    except Exception:  # noqa: BLE001 — routing hints must never break a start
        pass
    return origin


def completion_event(
    directory: Path, status: Dict[str, Any], request: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Build the gateway completion event for a terminal job, if routable."""
    job_id = str(status.get("job_id") or request.get("job_id") or "")
    state = str(status.get("state") or "")
    if not job_id or state not in jobs.TERMINAL_STATES:
        return None
    origin = request.get("origin") or {}
    session_key = str(origin.get("session_key") or "")
    session_id = str(origin.get("session_id") or "")
    if not session_key and not session_id:
        return None  # no origin recorded: nothing to wake

    error = status.get("error")
    summary = f"Deep research job {job_id} {state}." + (f" Error: {error}" if error else "")
    if state == jobs.STATE_COMPLETED and jobs.read_report(directory):
        # Bounded preview; the full report comes from the result tool / path.
        summary += " Report ready."

    return {
        "type": EVENT_TYPE,
        "delegation_id": f"research-{job_id}",
        "session_key": session_key,
        "origin_session_id": session_id,
        "parent_session_id": session_id,
        "goal": f"deep research job {job_id}",
        "status": "completed" if state == jobs.STATE_COMPLETED else "error",
        "summary": summary[:SUMMARY_CHARS],
        "error": None if state == jobs.STATE_COMPLETED else (error or state),
        "completed_at": float(status.get("completed_at") or time.time()),
        "dispatched_at": float(status.get("created_at") or time.time()),
        "research_job_id": job_id,
        "research_state": state,
    }


def notify_pending(
    hermes_home: Optional[Path] = None,
    *,
    limit: int = 10,
    queue_put: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[str]:
    """Notify every terminal-but-unnotified job. Returns the job ids sent.

    ``mark_notified`` is the single-winner flip, so a concurrent watcher (or a
    restart racing the old gateway) can never double-deliver. A queue that
    rejects the event rolls the claim back, making delivery retryable.
    """
    if queue_put is None:
        try:
            from tools.process_registry import process_registry

            queue_put = process_registry.completion_queue.put
        except Exception:  # noqa: BLE001 — no queue in this process (worker/CLI)
            return []

    sent: List[str] = []
    for summary in jobs.list_recent_jobs(limit, hermes_home):
        job_id = str(summary.get("job_id") or "")
        if not jobs.is_canonical_job_id(job_id):
            continue
        if summary.get("state") not in jobs.TERMINAL_STATES:
            continue
        try:
            directory = jobs.resolve_existing_job(job_id, hermes_home)
            status = jobs.read_status(directory)
        except (ValueError, FileNotFoundError):
            continue
        if status.get("notified"):
            continue
        event = completion_event(directory, status, jobs.read_request(directory))
        if event is None:
            continue
        if not jobs.mark_notified(directory):
            continue  # someone else delivered it
        try:
            queue_put(event)
        except Exception:  # noqa: BLE001 — delivery must not take the watcher down
            # Roll the claim back so a later sweep retries this job instead of
            # permanently losing the completion behind a "notified" flag.
            logger.warning("deep research: completion queue rejected job %s; will retry", job_id)
            jobs.unmark_notified(directory)
            continue
        sent.append(job_id)
    return sent


# ---------------------------------------------------------------------------
# Gateway watcher
# ---------------------------------------------------------------------------


class CompletionWatcher:
    """Daemon thread that forwards finished jobs into the completion queue."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        hermes_home: Optional[Path] = None,
        queue_put: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.hermes_home = hermes_home
        self.queue_put = queue_put
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="deep-research-notify", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        # Startup recovery: fail jobs whose runner died with the old process.
        try:
            from plugins.deep_research.launcher import runner_alive

            recovered = jobs.recover_stale_jobs(
                runner_alive=runner_alive, hermes_home=self.hermes_home
            )
            for job_id in recovered:
                logger.info("deep research: job %s marked interrupted after restart", job_id)
        except Exception:  # noqa: BLE001
            logger.exception("deep research: stale-job recovery failed")
        while not self._stop.wait(self.interval_seconds):
            try:
                notify_pending(self.hermes_home, queue_put=self.queue_put)
            except Exception:  # noqa: BLE001 — one bad tick must not kill the thread
                logger.exception("deep research: notify sweep failed")


_WATCHER: Optional[CompletionWatcher] = None
_WATCHER_LOCK = threading.Lock()


def start_gateway_watcher(
    *, interval_seconds: float, hermes_home: Optional[Path] = None, queue_put=None
) -> bool:
    """Start the singleton watcher. Refuses under the hermetic test latch."""
    global _WATCHER
    if os.environ.get("HERMES_TEST_ISOLATION"):
        return False
    if queue_put is None:
        try:
            from tools.process_registry import process_registry

            queue_put = process_registry.completion_queue.put
        except Exception:  # noqa: BLE001
            return False
    with _WATCHER_LOCK:
        if _WATCHER is not None and _WATCHER._thread and _WATCHER._thread.is_alive():
            return True
        _WATCHER = CompletionWatcher(
            interval_seconds=interval_seconds, hermes_home=hermes_home, queue_put=queue_put
        )
        return _WATCHER.start()


def stop_gateway_watcher() -> None:
    global _WATCHER
    with _WATCHER_LOCK:
        if _WATCHER is not None:
            _WATCHER.stop()
        _WATCHER = None
