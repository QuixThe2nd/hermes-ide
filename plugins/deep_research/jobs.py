"""Durable job store for the deep_research plugin.

Every artifact lives under ``$HERMES_HOME/research_jobs/<job_id>/`` with private
modes. All writes go through :func:`utils.atomic_json_write` /
:func:`utils.atomic_write_text` (temp + fsync + ``os.replace``), so a crashed
runner can never leave a torn ``status.json`` behind.

Job IDs are canonical (``rj_`` + 12 hex) and are validated on *every* access;
path parameters from the model are never joined onto the filesystem.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write, atomic_write_text

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
        for key in ("runner_mode", "runner_unit", "runner_pid", "runner_pid_start"):
            if key in runner_info:
                status[key] = runner_info[key]

    update_status(directory, mutate=mutate, require_state=ACTIVE_STATES)


def record_runner_info(directory: Path, runner_info: Dict[str, Any]) -> None:
    """Record how the runner was launched *without* leaving the queued state.

    Called by ``start`` right after the spawn, so ``cancel`` can target the
    unit/PID even if the runner dies before its first status write.
    """

    def mutate(status: Dict[str, Any]) -> None:
        for key in ("runner_mode", "runner_unit", "runner_pid", "runner_pid_start"):
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


def mark_all_lanes_cancelled(directory: Path) -> None:
    def mutate(status: Dict[str, Any]) -> None:
        for lane in status.get("lanes") or []:
            if lane.get("state") in (LANE_PENDING, LANE_RUNNING):
                lane["state"] = LANE_CANCELLED
                lane["updated_at"] = _now()

    update_status(directory, mutate=mutate)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

_JOB_LOCKS: Dict[str, Any] = {}
_LOCKS_GUARD: Any = None


def _job_lock(directory: Path):
    """Per-job re-entrant lock guarding read-modify-write cycles in-process.

    Cross-process safety comes from atomic replace: two writers produce two
    complete files, never a torn one. The lock keeps the in-process runner,
    cancel path, and notify watcher from interleaving their read-modify-writes.
    """
    import threading

    global _LOCKS_GUARD
    if _LOCKS_GUARD is None:
        _LOCKS_GUARD = threading.Lock()
    key = str(directory)
    with _LOCKS_GUARD:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JOB_LOCKS[key] = lock
    return lock


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
        mark_all_lanes_cancelled(directory)
        finished = finish_job(
            directory,
            STATE_FAILED,
            error="interrupted: runner not running after restart",
            phase="interrupted",
        )
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
