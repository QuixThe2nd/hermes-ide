"""Durable job store for the deep_research plugin.

Every artifact lives under ``$HERMES_HOME/research_jobs/<job_id>/`` with private
modes. All writes go through :func:`utils.atomic_json_write` /
:func:`utils.atomic_write_text` (temp + fsync + ``os.replace``), so a crashed
runner can never leave a torn ``status.json`` behind.

Job IDs are canonical (``rj_`` + 12 hex) and are validated on *every* access;
path parameters from the model are never joined onto the filesystem.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write, atomic_write_text

logger = logging.getLogger("hermes.plugins.deep_research.jobs")

PLUGIN_STATE_DIRNAME = "research_jobs"

JOB_ID_RE = re.compile(r"^rj_[0-9a-f]{12}$")

# Terminal states: no further transitions out of these.
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
# Non-terminal states a runner can leave behind when it dies.
ACTIVE_STATES = frozenset({"queued", "running", "synthesizing"})

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SYNTHESIZING = "synthesizing"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

LANE_PENDING = "pending"
LANE_RUNNING = "running"
LANE_SUCCEEDED = "succeeded"
LANE_FAILED = "failed"
LANE_CANCELLED = "cancelled"

# Private modes: the job dir holds the operator's brief and source ledger.
DIR_MODE = 0o700
FILE_MODE = 0o600

# Runner identity recorded in status.json (manager scope + downgrade reason
# included so cancel/liveness address the exact unit and honesty survives).
RUNNER_KEYS = (
    "runner_mode",
    "runner_unit",
    "runner_pid",
    "runner_pid_start",
    "runner_scope",
    "runner_reason",
)


def new_job_id() -> str:
    """Mint a canonical job id."""
    return f"rj_{uuid.uuid4().hex[:12]}"


def is_canonical_job_id(value: Any) -> bool:
    """True only for a canonical job id string — rejects traversal outright."""
    # fullmatch, not match: ``re``'s ``$`` would otherwise accept a trailing
    # newline (``rj_000000000000\\n``), and a newline in a path is a rename.
    return isinstance(value, str) and bool(JOB_ID_RE.fullmatch(value))


def research_jobs_root(hermes_home: Optional[Path] = None) -> Path:
    """``<HERMES_HOME>/research_jobs`` (profile-aware; never a literal home)."""
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / PLUGIN_STATE_DIRNAME


def job_dir(job_id: str, hermes_home: Optional[Path] = None) -> Path:
    """Resolve a job directory, refusing anything but a canonical id."""
    if not is_canonical_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    root = research_jobs_root(hermes_home)
    path = root / job_id
    # Defense in depth: the resolved path must sit directly under the root.
    if path.parent != root:
        raise ValueError(f"invalid job id: {job_id!r}")
    return path


def resolve_existing_job(job_id: str, hermes_home: Optional[Path] = None) -> Path:
    """Like :func:`job_dir` but also requires the job to exist on disk."""
    path = job_dir(job_id, hermes_home)
    if not path.is_dir():
        raise FileNotFoundError(f"unknown research job: {job_id}")
    return path


def _now() -> float:
    return time.time()


def _write_private_json(path: Path, data: Dict[str, Any]) -> None:
    atomic_json_write(path, data, mode=FILE_MODE)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_job(
    *,
    brief: str,
    research_questions: Optional[List[str]],
    timeout_minutes: int,
    max_parallel: int,
    worker_profile: str,
    origin: Optional[Dict[str, Any]] = None,
    hermes_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create the durable job skeleton and return ``{"job_id", "dir"}``.

    ``request.json`` is frozen at creation; later code only ever *reads* it.
    """
    job_id = new_job_id()
    root = research_jobs_root(hermes_home)
    root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(root, DIR_MODE)

    directory = root / job_id
    directory.mkdir(mode=DIR_MODE, parents=False, exist_ok=False)
    os.chmod(directory, DIR_MODE)
    for subdir in ("lanes", "prompts"):
        child = directory / subdir
        child.mkdir(mode=DIR_MODE, exist_ok=False)
        os.chmod(child, DIR_MODE)

    created_at = _now()
    request = {
        "job_id": job_id,
        "brief": brief,
        "research_questions": list(research_questions) if research_questions else None,
        "timeout_minutes": timeout_minutes,
        "max_parallel": max_parallel,
        "worker_profile": worker_profile,
        "created_at": created_at,
        # Origin identifiers are captured by the handler for completion routing.
        "origin": dict(origin or {}),
    }
    _write_private_json(directory / "request.json", request)

    questions = request["research_questions"]
    lanes = [
        {
            "index": index,
            "question": (questions[index] if questions else brief),
            "state": LANE_PENDING,
            "exit_code": None,
            "error": None,
            "updated_at": created_at,
        }
        for index in range(len(questions) if questions else 1)
    ]

    status: Dict[str, Any] = {
        "job_id": job_id,
        "state": STATE_QUEUED,
        "phase": "queued",
        "lanes": lanes,
        "created_at": created_at,
        "updated_at": created_at,
        "started_at": None,
        "completed_at": None,
        "error": None,
        "runner_mode": None,
        "runner_unit": None,
        "runner_pid": None,
        "runner_pid_start": None,
        "runner_scope": None,
        "runner_reason": None,
        "timeout_minutes": timeout_minutes,
        "max_parallel": max_parallel,
        "worker_profile": worker_profile,
        "synthesis": {"attempts": 0, "correction_used": False, "citation_errors": []},
        "notified": False,
    }
    _write_private_json(directory / "status.json", status)
    # Pre-create the evidence ledger private: the first concurrent writer must
    # never be the one that decides its mode.
    ledger = directory / "evidence.jsonl"
    fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    os.close(fd)
    return {"job_id": job_id, "dir": directory}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_request(directory: Path) -> Dict[str, Any]:
    return _read_json(directory / "request.json")


def read_status(directory: Path) -> Dict[str, Any]:
    return _read_json(directory / "status.json")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        import json

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_report(directory: Path) -> str:
    try:
        return (directory / "report.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def evidence_path(directory: Path) -> Path:
    return directory / "evidence.jsonl"


def read_evidence_urls(directory: Path) -> List[str]:
    """Normalized URLs recorded in the ledger, in first-seen order."""
    seen: Dict[str, None] = {}
    path = evidence_path(directory)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    import json

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        url = record.get("normalized_url") or record.get("url")
        if isinstance(url, str) and url:
            seen.setdefault(url, None)
    return list(seen)


def list_recent_jobs(limit: int, hermes_home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Newest-first summary of recent jobs, bounded to ``limit``."""
    root = research_jobs_root(hermes_home)
    try:
        entries = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return []
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    summaries: List[Dict[str, Any]] = []
    for directory in entries[: max(1, limit)]:
        status = read_status(directory)
        request = read_request(directory)
        if not status and not request:
            continue
        summaries.append(
            {
                "job_id": directory.name,
                "state": status.get("state") or "unknown",
                "phase": status.get("phase") or "",
                "lanes": _lane_counts(status),
                "created_at": status.get("created_at") or request.get("created_at"),
                "updated_at": status.get("updated_at"),
                "error": status.get("error"),
            }
        )
    return summaries


def _lane_counts(status: Dict[str, Any]) -> Dict[str, int]:
    counts = {"total": 0, "succeeded": 0, "failed": 0, "running": 0, "pending": 0}
    for lane in status.get("lanes") or []:
        counts["total"] += 1
        state = lane.get("state")
        if state in counts:
            counts[state] += 1
    return counts


# ---------------------------------------------------------------------------
# State transitions (all atomic, all guarded)
# ---------------------------------------------------------------------------


def update_status(
    directory: Path,
    *,
    mutate: Callable[[Dict[str, Any]], None],
    require_state: Optional[frozenset] = None,
    guard: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically apply ``mutate`` to ``status.json``.

    ``require_state`` makes the transition conditional: when the current state
    is not in the allowed set the write is refused and ``None`` returned. This
    is what makes cancel-vs-complete races single-winner. ``guard`` is an
    additional predicate over the current status with the same refuse semantics.

    A cross-process lock that cannot be owned raises :class:`StatusLockError`
    (:class:`StatusLockTimeout` for the bounded wait) *before* anything is
    read or written, so no caller can mistake a lock refusal for a refused
    transition — or worse, land a write built on a stale snapshot.
    """
    path = directory / "status.json"
    with _job_lock(directory):
        status = _read_json(path)
        if not status:
            return None
        if require_state is not None and status.get("state") not in require_state:
            return None
        if guard is not None and not guard(status):
            return None
        mutate(status)
        status["updated_at"] = _now()
        _write_private_json(path, status)
        return status


def mark_running(directory: Path, runner_info: Dict[str, Any]) -> None:
    def mutate(status: Dict[str, Any]) -> None:
        status["state"] = STATE_RUNNING
        status["phase"] = "running lanes"
        status["started_at"] = status.get("started_at") or _now()
        for key in RUNNER_KEYS:
            if key in runner_info:
                status[key] = runner_info[key]

    update_status(directory, mutate=mutate, require_state=ACTIVE_STATES)


def record_runner_info(directory: Path, runner_info: Dict[str, Any]) -> None:
    """Record how the runner was launched *without* leaving the queued state.

    Called by ``start`` right after the spawn, so ``cancel`` can target the
    unit/PID even if the runner dies before its first status write.
    """

    def mutate(status: Dict[str, Any]) -> None:
        for key in RUNNER_KEYS:
            if key in runner_info:
                status[key] = runner_info[key]

    update_status(directory, mutate=mutate, require_state=ACTIVE_STATES)


def set_phase(directory: Path, phase: str, state: Optional[str] = None) -> None:
    allowed = ACTIVE_STATES if state in ACTIVE_STATES else None

    def mutate(status: Dict[str, Any]) -> None:
        if state is not None:
            status["state"] = state
        status["phase"] = phase

    update_status(directory, mutate=mutate, require_state=allowed)


def update_lane(directory: Path, index: int, **fields: Any) -> None:
    def mutate(status: Dict[str, Any]) -> None:
        for lane in status.get("lanes") or []:
            if lane.get("index") == index:
                lane.update(fields)
                lane["updated_at"] = _now()
                return

    update_status(directory, mutate=mutate, require_state=ACTIVE_STATES)


def finish_job(
    directory: Path,
    state: str,
    *,
    error: Optional[str] = None,
    phase: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Move the job to a terminal state. Refused from an already-terminal state."""
    if state not in TERMINAL_STATES:
        raise ValueError(f"not a terminal state: {state!r}")

    def mutate(status: Dict[str, Any]) -> None:
        status["state"] = state
        status["phase"] = phase or state
        status["completed_at"] = _now()
        status["error"] = error

    return update_status(directory, mutate=mutate, require_state=ACTIVE_STATES)


def mark_notified(directory: Path) -> bool:
    """Flip ``notified`` exactly once. Returns False if already notified."""

    def mutate(status: Dict[str, Any]) -> None:
        status["notified"] = True

    return (
        update_status(
            directory,
            mutate=mutate,
            require_state=TERMINAL_STATES,
            guard=lambda status: not status.get("notified"),
        )
        is not None
    )


def unmark_notified(directory: Path) -> bool:
    """Release a notification claim after a failed delivery.

    The watcher flips ``notified`` *before* queueing the event; if the queue
    rejects it, the claim must come back off so a later sweep retries instead
    of silently losing the completion forever.
    """

    def mutate(status: Dict[str, Any]) -> None:
        status["notified"] = False

    return (
        update_status(
            directory,
            mutate=mutate,
            require_state=TERMINAL_STATES,
            guard=lambda status: bool(status.get("notified")),
        )
        is not None
    )


def mark_all_lanes_cancelled(directory: Path) -> None:
    def mutate(status: Dict[str, Any]) -> None:
        for lane in status.get("lanes") or []:
            if lane.get("state") in (LANE_PENDING, LANE_RUNNING):
                lane["state"] = LANE_CANCELLED
                lane["updated_at"] = _now()

    update_status(directory, mutate=mutate)


def mark_lanes_failed(directory: Path, error: str) -> None:
    """Fail lanes still pending/running — the job ended without them.

    Used when the job's own budget runs out mid-run, so a terminal ``failed``
    job never reports a lane as ``running`` forever.
    """

    def mutate(status: Dict[str, Any]) -> None:
        for lane in status.get("lanes") or []:
            if lane.get("state") in (LANE_PENDING, LANE_RUNNING):
                lane["state"] = LANE_FAILED
                lane["error"] = error
                lane["updated_at"] = _now()

    update_status(directory, mutate=mutate)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

# fcntl is Unix-only; on Windows fall back to msvcrt. A platform with neither
# primitive is the one documented degraded mode: _job_lock() then provides
# in-process locking only — the same shape as cron/jobs.py. Everywhere else
# the cross-process lock is mandatory and fails closed (see StatusLockError).
try:
    import fcntl  # windows-footgun: ok — msvcrt fallback below
except ImportError:  # pragma: no cover — POSIX always provides fcntl
    fcntl = None
try:
    import msvcrt  # windows-footgun: ok — POSIX never imports this branch
except ImportError:  # pragma: no cover — Windows-only module
    msvcrt = None


class StatusLockError(RuntimeError):
    """A status transaction was refused: the cross-process lock was not owned.

    Raised before any read/guard/mutate/write runs, when a locking primitive
    exists (POSIX ``fcntl`` or Windows ``msvcrt``) but the exclusive
    ``.status.lock`` could not be taken. Callers must treat the transaction
    as *not performed* — never as a refused transition (``None``) and never
    as success. The kernel releases the flock when its holder dies, so a
    crashed process cannot wedge the lock; retrying later is always safe.
    """


class StatusLockTimeout(StatusLockError):
    """Bounded acquisition of the status lock timed out (another holder)."""


_JOB_LOCKS: Dict[str, Any] = {}
_LOCKS_GUARD: Any = None
_LOCK_DEPTH = threading.local()
_LOCK_FILE_NAME = ".status.lock"
# Bounded flock acquisition for the same reason cron bounds its jobs lock
# (#60703): an unbounded blocking lock held by a wedged process would freeze
# every status write — cancel, finish, and the notify watcher alike.
_JOB_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.1
# flock contention is EWOULDBLOCK (EAGAIN alias); any other errno is a real
# acquisition failure and fails closed immediately instead of polling.
_FLOCK_CONTENTION_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN})


@contextlib.contextmanager
def _job_lock(directory: Path):
    """Serialize one job's read-modify-write status cycles.

    Layer 1 is the per-job RLock: the runner, the cancel path, and the notify
    watcher can all live in the gateway process, and their read-guard-mutate-
    write cycles must not interleave. Layer 2 is an advisory exclusive lock on
    ``<job>/.status.lock``: the gateway watcher and a runner transient service
    are *separate processes* that share no memory, so only a file lock makes
    cancel-vs-finish and mark_notified single-winner across them. Atomic
    replace still provides the durability (a crashed writer leaves a whole
    file); the lock serializes the transaction, it does not replace the write.

    Mirrors the fcntl + msvcrt pattern in ``cron/jobs.py`` and
    ``tools/memory_tool.py``. The flock poll is bounded and **fails closed**:
    a timeout — or any other acquisition failure while a primitive exists —
    raises :class:`StatusLockError` before the transaction starts, because
    proceeding on a stale snapshot is exactly how one writer's update
    silently overwrites another's. The only degraded mode is a platform with
    neither ``fcntl`` nor ``msvcrt``. The kernel releases the flock when its
    holder dies, so a crashed process can never wedge the lock permanently.
    Re-entrant per thread like the cron lock, so a nested acquisition reuses
    the held lock instead of deadlocking on its own flock.
    """
    global _LOCKS_GUARD
    if _LOCKS_GUARD is None:
        _LOCKS_GUARD = threading.Lock()
    key = str(directory)
    with _LOCKS_GUARD:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JOB_LOCKS[key] = lock

    depths = getattr(_LOCK_DEPTH, "depths", None)
    if depths is None:
        depths = {}
        _LOCK_DEPTH.depths = depths
    if depths.get(key):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    with lock:
        depths[key] = 1
        try:
            release = _acquire_status_file_lock(directory)
            try:
                yield
            finally:
                if release is not None:
                    release()
        finally:
            depths[key] = 0


def _acquire_status_file_lock(directory: Path) -> Optional[Callable[[], None]]:
    """Take the cross-process advisory lock; return its releaser.

    Fails closed. When a locking primitive exists (POSIX ``fcntl``, Windows
    ``msvcrt``) the caller may only run its read-modify-write transaction
    *owning* this lock, so every refusal — a bounded-acquisition timeout, an
    unopenable lock file, or an OS-level acquisition error — raises
    :class:`StatusLockError` (a timeout raises :class:`StatusLockTimeout`)
    instead of degrading to the in-process lock alone: that fail-open path is
    how a writer holding a stale snapshot clobbered the other process's
    update. ``None``, the documented degraded mode, is returned only on a
    platform with neither primitive.
    """
    if fcntl is None and msvcrt is None:
        return None
    lock_path = directory / _LOCK_FILE_NAME
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        raise StatusLockError(f"could not open the status lock {lock_path}: {exc}") from exc
    acquired = False
    try:
        if fcntl is not None:
            _flock_exclusive(handle, lock_path)
        else:
            try:
                handle.seek(0)
                getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
            except OSError as exc:
                raise StatusLockError(
                    f"could not acquire the status lock {lock_path}: {exc}"
                ) from exc
        acquired = True
    finally:
        if not acquired:
            try:
                handle.close()
            except OSError:
                pass

    def _release() -> None:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
        except OSError:
            pass
        finally:
            handle.close()

    return _release


def _flock_exclusive(handle: Any, lock_path: Path) -> None:
    """Bounded exclusive flock: contention polls, anything else fails closed."""
    deadline = time.monotonic() + _JOB_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in _FLOCK_CONTENTION_ERRNOS:
                raise StatusLockError(
                    f"the status lock {lock_path} could not be acquired (errno {exc.errno}): {exc}"
                ) from exc
            if time.monotonic() >= deadline:
                logger.error(
                    "Timed out after %.1fs waiting for the status lock (%s) — another "
                    "process is holding it. Refusing this status transaction instead of "
                    "writing on a stale snapshot.",
                    _JOB_LOCK_TIMEOUT_SECONDS,
                    lock_path,
                )
                raise StatusLockTimeout(
                    f"the status lock {lock_path} was still held after the "
                    f"{_JOB_LOCK_TIMEOUT_SECONDS:.1f}s bounded wait"
                ) from None
            time.sleep(min(_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


# ---------------------------------------------------------------------------
# Stale recovery
# ---------------------------------------------------------------------------


def recover_stale_jobs(
    *,
    runner_alive: Callable[[Dict[str, Any]], Optional[bool]],
    grace_seconds: float = 90.0,
    hermes_home: Optional[Path] = None,
    now: Optional[Callable[[], float]] = None,
) -> List[str]:
    """Fail non-terminal jobs whose runner is verifiably gone.

    ``runner_alive(status)`` returns ``None`` when liveness cannot be determined
    (treated as alive — never fail a job on an inconclusive probe). Jobs touched
    within ``grace_seconds`` are skipped so a freshly launched runner is not
    reaped mid-startup.
    """
    clock = now or _now
    root = research_jobs_root(hermes_home)
    recovered: List[str] = []
    try:
        entries = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return recovered

    for directory in entries:
        if not is_canonical_job_id(directory.name):
            continue
        status = read_status(directory)
        if status.get("state") not in ACTIVE_STATES:
            continue
        updated = status.get("updated_at") or 0
        try:
            age = clock() - float(updated)
        except (TypeError, ValueError):
            age = 0.0
        if age < grace_seconds:
            continue
        alive = runner_alive(status)
        if alive is None or alive:
            continue
        try:
            mark_all_lanes_cancelled(directory)
            finished = finish_job(
                directory,
                STATE_FAILED,
                error="interrupted: runner not running after restart",
                phase="interrupted",
            )
        except StatusLockError:
            # Another process owns this job's status right now: skip it (no
            # recovery is claimed) and let a later pass reconcile it.
            logger.warning(
                "deep research: status lock busy; not recovering %s this pass", directory.name
            )
            continue
        if finished is not None:
            recovered.append(directory.name)
    return recovered


# ---------------------------------------------------------------------------
# Misc artifacts
# ---------------------------------------------------------------------------


def write_prompt(directory: Path, name: str, text: str) -> Path:
    """Write a private prompt file (the argv-free transport for ``--query-file``)."""
    # Must start with an alphanumeric so ``.``/``..`` style names are refused.
    if not re.fullmatch(r"[0-9a-z][0-9a-z_.-]*", name):
        raise ValueError(f"invalid prompt name: {name!r}")
    path = directory / "prompts" / f"{name}.md"
    atomic_write_text(path, text, create_mode=FILE_MODE)
    os.chmod(path, FILE_MODE)
    return path


def write_lane_report(directory: Path, index: int, text: str) -> Path:
    path = directory / "lanes" / f"{index}.md"
    atomic_write_text(path, text, create_mode=FILE_MODE)
    os.chmod(path, FILE_MODE)
    return path


def read_lane_report(directory: Path, index: int) -> str:
    try:
        return (directory / "lanes" / f"{index}.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def publish_report(directory: Path, text: str) -> Path:
    """Publish the final report. Only ever called after citation validation."""
    path = directory / "report.md"
    atomic_write_text(path, text, create_mode=FILE_MODE)
    os.chmod(path, FILE_MODE)
    return path


def preserve_draft(directory: Path, text: str) -> Path:
    path = directory / "report.draft.md"
    atomic_write_text(path, text, create_mode=FILE_MODE)
    os.chmod(path, FILE_MODE)
    return path
