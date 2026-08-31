"""Completion re-entry for research jobs.

The runner is a separate host process, so it cannot touch the gateway's
in-memory queue. Instead a small daemon watcher (started from
``on_gateway_start``) notices terminal jobs whose ``notified`` flag is still
clear, flips it exactly once, and pushes an ``async_delegation``-shaped event
onto ``process_registry.completion_queue`` — the same infrastructure
``delegate_task``/missions use, so the outcome re-enters the originating
session without polling.

``start`` writes each job under the *active* ``get_hermes_home()`` — a
multiplexed or named-profile session's jobs land in that profile's home — so
the single watcher thread sweeps the process home **and** every live
named-profile home under it (:func:`watcher_hermes_homes`), never a
per-profile thread.

Losing the notification costs nothing durable: ``status``/``result`` read the
artifacts on disk, so a gateway restart (or a CLI-origin job the gateway cannot
route) still recovers the result on demand.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home, get_process_hermes_home
from plugins.deep_research import jobs

logger = logging.getLogger("hermes.plugins.deep_research.notify")

# Reuse the async-delegation delivery lane; the gateway's watcher coalesces
# these per originating session.
EVENT_TYPE = "async_delegation"
SUMMARY_CHARS = 700


def origin_context(session_id: Optional[str], task_id: Optional[str] = None) -> Dict[str, Any]:
    """Origin identifiers recorded in ``request.json`` for completion routing.

    ``session_key`` is resolved best-effort from the session store under the
    **active** home (:func:`hermes_constants.get_hermes_home`) — the same home
    ``start`` creates the job under, which for a multiplexed session is the
    profile home, not the process-default one. The resolved key and the home
    itself are frozen into ``request.json``, so completion routing survives a
    gateway restart without re-resolving anything. Without a key a CLI-origin
    job simply has no gateway route and is recovered via ``status``/``result``.
    """
    home = get_hermes_home()
    origin: Dict[str, Any] = {
        "session_id": str(session_id or ""),
        "task_id": str(task_id or ""),
        "session_key": "",
        "hermes_home": str(home),
    }
    if not origin["session_id"]:
        return origin
    try:
        from hermes_state import SessionDB

        db_path = Path(home) / "state.db"
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


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _unnotified_terminal_dirs(hermes_home: Optional[Path] = None) -> List[Path]:
    """Terminal-but-unnotified job dirs, newest first, unbounded.

    Enumerating *all* job directories before filtering is what keeps old
    completions reachable: slicing the listing to the newest N first would
    strand every older terminal job behind a wall of already-notified ones.
    """
    root = jobs.research_jobs_root(hermes_home)
    try:
        entries = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return []
    entries.sort(key=_mtime, reverse=True)
    pending: List[Path] = []
    for directory in entries:
        if not jobs.is_canonical_job_id(directory.name):
            continue
        status = jobs.read_status(directory)
        if status.get("state") not in jobs.TERMINAL_STATES:
            continue
        if status.get("notified"):
            continue
        pending.append(directory)
    return pending


def notify_pending(
    hermes_home: Optional[Path] = None,
    *,
    limit: int = 10,
    queue_put: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[str]:
    """Notify every terminal-but-unnotified job. Returns the job ids sent.

    The per-tick ``limit`` is a delivery bound applied *after* filtering to
    terminal-and-unnotified jobs, so one full tick never silently strands the
    (limit+1)-th completion (the next tick picks it up). ``mark_notified`` is
    the single-winner flip, so a concurrent watcher (or a restart racing the
    old gateway) can never double-deliver. A queue that rejects the event
    rolls the claim back, making delivery retryable. A job whose status lock
    is owned by another process is skipped unclaimed — never delivered, never
    marked — so a later sweep retries it once the holder is done.
    """
    if queue_put is None:
        try:
            from tools.process_registry import process_registry

            queue_put = process_registry.completion_queue.put
        except Exception:  # noqa: BLE001 — no queue in this process (worker/CLI)
            return []

    sent: List[str] = []
    for directory in _unnotified_terminal_dirs(hermes_home):
        if len(sent) >= max(1, limit):
            break
        job_id = directory.name
        status = jobs.read_status(directory)
        event = completion_event(directory, status, jobs.read_request(directory))
        if event is None:
            continue
        try:
            if not jobs.mark_notified(directory):
                continue  # someone else delivered it
        except jobs.StatusLockError:
            logger.warning(
                "deep research: status lock busy for job %s; deferring its notification", job_id
            )
            continue
        try:
            queue_put(event)
        except Exception:  # noqa: BLE001 — delivery must not take the watcher down
            # Roll the claim back so a later sweep retries this job instead of
            # permanently losing the completion behind a "notified" flag.
            logger.warning("deep research: completion queue rejected job %s; will retry", job_id)
            try:
                jobs.unmark_notified(directory)
            except jobs.StatusLockError:
                logger.warning(
                    "deep research: could not release the claim for job %s; lock busy", job_id
                )
            continue
        sent.append(job_id)
    return sent


# ---------------------------------------------------------------------------
# Gateway watcher
# ---------------------------------------------------------------------------


def _is_real_child_directory(entry: Path, resolved_root: Path) -> bool:
    """True only for a real directory directly inside ``resolved_root``.

    ``entry`` must be a plain directory reached under its own name — never
    through a symlink — and the resolved candidate's parent must be the
    resolved root itself, so a profile home can never resolve outside
    ``profiles/``. The symlink test is ``lstat`` (it never follows), and the
    ``stat``/``lstat`` inode pair catches a name swapped for a symlink between
    the two calls. Broken links, races, and any OS error disqualify the entry;
    nothing is ever followed.
    """
    try:
        link_stat = entry.lstat()
        if stat.S_ISLNK(link_stat.st_mode):
            return False
        followed = entry.stat()
        if (followed.st_dev, followed.st_ino) != (link_stat.st_dev, link_stat.st_ino):
            return False  # raced to a symlink; never follow the replacement
        if not stat.S_ISDIR(followed.st_mode):
            return False
        return entry.resolve().parent == resolved_root
    except OSError:
        return False


def watcher_hermes_homes(process_home: Optional[Path] = None) -> List[Path]:
    """Hermes homes the gateway watcher sweeps: the process home plus every
    live named-profile home physically under ``<process home>/profiles/``.

    ``start`` writes jobs under the *active* :func:`hermes_constants.get_hermes_home`,
    and for a multiplexed/named-profile session that is a profile home
    (``<process home>/profiles/<name>/``) — so a watcher that only swept the
    process home would never recover or completion-notify those jobs. The
    enumeration is a directory scan anchored at the process home only: it
    never consults the operator's live ``~/.hermes`` (tests point
    ``HERMES_HOME`` at a tmpdir for exactly that reason), skips tombstoned or
    missing profile homes, and ignores non-profile entries such as the
    ``.deleted`` tombstone directory.

    Symlink confinement: every swept profile home is a real directory sitting
    directly inside a non-symlink ``profiles/`` root. ``Path.is_dir()``
    *follows* symlinks, so a ``profiles/alpha`` planted at another operator's
    home would otherwise have the watcher read and deliver that home's jobs.
    A symlinked entry is therefore skipped even when its name is valid and
    its target is a real directory inside this process home, and a symlinked
    ``profiles`` root refuses the enumeration outright.
    """
    home = Path(process_home) if process_home is not None else get_process_hermes_home()
    homes = [home]
    profiles_root = home / "profiles"
    try:
        root_stat = profiles_root.lstat()
    except OSError:
        return homes  # no profiles root: the process home only
    if stat.S_ISLNK(root_stat.st_mode):
        logger.warning(
            "deep research: %s is a symlink; refusing to sweep profile homes through it",
            profiles_root,
        )
        return homes
    try:
        resolved_root = profiles_root.resolve()
        entries = sorted(profiles_root.iterdir())
    except OSError:
        return homes
    try:
        from hermes_cli.profiles import validate_profile_name

        def _is_profile_home(entry: Path) -> bool:
            if entry.name == "default":
                return False  # "default" IS the process home, not a child of profiles/
            try:
                validate_profile_name(entry.name)
            except ValueError:
                return False  # dot-dirs, tombstone dirs, stray names
            return True

        from hermes_constants import named_profile_is_deleted
    except Exception:  # noqa: BLE001 — a broken profiles module must not stop the sweep
        return homes
    for entry in entries:
        if not _is_profile_home(entry):
            continue
        if not _is_real_child_directory(entry, resolved_root):
            continue
        if named_profile_is_deleted(entry) or not entry.exists():
            continue
        homes.append(entry)
    return homes


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

    def _watch_homes(self) -> List[Path]:
        """Homes to sweep this tick: the process home plus its live profiles.

        Re-derived every tick so profiles created or deleted after the watcher
        started are picked up without a restart.
        """
        return watcher_hermes_homes(self.hermes_home)

    def _loop(self) -> None:
        # Startup recovery: fail jobs whose runner died with the old process —
        # across the process home and every live profile home under it.
        try:
            from plugins.deep_research.launcher import runner_alive

            for home in self._watch_homes():
                recovered = jobs.recover_stale_jobs(runner_alive=runner_alive, hermes_home=home)
                for job_id in recovered:
                    logger.info("deep research: job %s marked interrupted after restart", job_id)
        except Exception:  # noqa: BLE001
            logger.exception("deep research: stale-job recovery failed")
        while not self._stop.wait(self.interval_seconds):
            for home in self._watch_homes():
                try:
                    notify_pending(home, queue_put=self.queue_put)
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
