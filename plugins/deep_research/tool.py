"""The ``delegate_research`` model tool.

One tool, five actions (``start`` / ``status`` / ``cancel`` / ``result`` /
``list``). ``start`` creates the durable job, spawns the host-owned runner, and
returns immediately — the caller never waits for research. Everything else is a
bounded read/write against the job's on-disk artifacts.

The schema deliberately has no command or path parameters: the only identifiers
the model can supply are canonical job ids minted here.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.deep_research import jobs, notify
from plugins.deep_research.config import (
    MAX_BRIEF_CHARS,
    MAX_MAX_PARALLEL,
    MAX_QUESTIONS,
    MAX_QUESTION_CHARS,
    MAX_TIMEOUT_MINUTES,
    MIN_MAX_PARALLEL,
    MIN_QUESTIONS,
    MIN_TIMEOUT_MINUTES,
    load_deep_research_config,
)

TOOL_NAME = "delegate_research"

# Provenance, not entailment — stated wherever a report leaves the harness.
PROVENANCE_NOTE = (
    "Citations are validated for URL provenance only: every cited URL was "
    "fetched during this job. This does not prove a cited page semantically "
    "supports the claim it is attached to."
)

DELEGATE_RESEARCH_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Delegate a substantial multi-source RESEARCH task to durable worker "
        "sessions that read full pages and return one cited report. Use it "
        "only after you have clarified the scope with the user and shown them "
        "the brief (and the lane plan, if any). Once a job is started, do NOT "
        "run your own web searches in parallel — wait for the result. Do not "
        "use this for trivia or single-lookup questions; answer those "
        "yourself. start returns immediately with a job_id; poll status, and "
        "read result for the final report when the job completes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "status", "cancel", "result", "list"],
                "description": "Which operation to run.",
            },
            "brief": {
                "type": "string",
                "description": (
                    "start only: the frozen research brief. Everything the "
                    "workers need — question, scope, constraints, desired "
                    "output shape. Cannot be changed after start."
                ),
            },
            "research_questions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": MIN_QUESTIONS,
                "maxItems": MAX_QUESTIONS,
                "description": (
                    "start only: 1-8 independent lane objectives. Omit to run "
                    "one lane on the whole brief."
                ),
            },
            "timeout_minutes": {
                "type": "integer",
                "minimum": MIN_TIMEOUT_MINUTES,
                "maximum": MAX_TIMEOUT_MINUTES,
                "description": "start only: overall job budget (default 20).",
            },
            "max_parallel": {
                "type": "integer",
                "minimum": MIN_MAX_PARALLEL,
                "maximum": MAX_MAX_PARALLEL,
                "description": "start only: lanes to run concurrently (default 2).",
            },
            "job_id": {
                "type": "string",
                "pattern": "^rj_[0-9a-f]{12}$",
                "description": "The job id returned by start (status/cancel/result).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def availability_error() -> Optional[str]:
    """Why the tool cannot run here, or ``None`` when it can.

    ``check_fn`` hides the tool on any failure; ``start`` reports the same
    reason so a caller that raced a config change learns why nothing happened.
    """
    if os.environ.get("HERMES_RESEARCH_JOB"):
        return "not available inside a research worker session"
    config = load_deep_research_config()
    if not config.enabled:
        return "deep_research is disabled in config (deep_research.enabled: false)"
    try:
        from hermes_cli.profiles import profile_exists

        if not profile_exists(config.worker_profile):
            return (
                f"worker profile {config.worker_profile!r} does not exist "
                "(configure deep_research.worker_profile)"
            )
    except Exception:  # noqa: BLE001 — a broken profiles module must hide, not crash
        return "worker profile check unavailable"
    override = os.environ.get("HERMES_BIN")
    if override and not Path(override).exists():
        return f"HERMES_BIN points at a missing file: {override}"
    return None


def check_requirements() -> bool:
    """``check_fn``: hide the tool unless the runtime can actually run jobs."""
    return availability_error() is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(**fields: Any) -> str:
    payload: Dict[str, Any] = {"ok": True}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=True)


def _error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": code, "message": message}, ensure_ascii=True)


def _iso(epoch: Any) -> Optional[str]:
    try:
        return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _bounded(text: Any, limit: int = 400) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _status_summary(directory: Path) -> Dict[str, Any]:
    status = jobs.read_status(directory)
    counts = {"total": 0, "succeeded": 0, "failed": 0, "running": 0, "pending": 0}
    for lane in status.get("lanes") or []:
        counts["total"] += 1
        if lane.get("state") in counts:
            counts[lane["state"]] += 1
    summary: Dict[str, Any] = {
        "job_id": status.get("job_id") or directory.name,
        "state": status.get("state") or "unknown",
        "phase": status.get("phase") or "",
        "lanes": counts,
        "timeout_minutes": status.get("timeout_minutes"),
        "max_parallel": status.get("max_parallel"),
        "worker_profile": status.get("worker_profile"),
        "runner_mode": status.get("runner_mode"),
        "created_at": _iso(status.get("created_at")),
        "updated_at": _iso(status.get("updated_at")),
        "error": _bounded(status.get("error")),
    }
    if status.get("state") == "queued":
        summary["blocker"] = "runner has not picked the job up yet"
    if status.get("runner_mode") == "fallback":
        summary["durability"] = (
            "fallback runner (no systemd user manager): survives a gateway "
            "process restart, not a host/cgroup supervisor stop"
        )
    return summary


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _resolve_job(args: Dict[str, Any]):
    """Validate and resolve a job_id argument. Returns (dir, error_json)."""
    job_id = str(args.get("job_id") or "").strip()
    if not jobs.is_canonical_job_id(job_id):
        return None, _error(
            "invalid_job_id",
            "job_id must be a canonical research job id (rj_ + 12 hex) issued by start",
        )
    try:
        return jobs.resolve_existing_job(job_id, _hermes_home()), None
    except FileNotFoundError:
        return None, _error("unknown_job", f"no research job {job_id} under this HERMES_HOME")


def launch_job(
    job_id: str,
    hermes_home: Path,
    config=None,
    timeout_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Spawn the durable runner. Returns the runner info recorded in status.

    Fenced off under the hermetic test latch exactly like the gateway's
    detached restart spawn: tests either monkeypatch this function or clear
    ``HERMES_TEST_ISOLATION`` to exercise the real path.
    """
    if os.environ.get("HERMES_TEST_ISOLATION"):
        raise RuntimeError("research runner spawn blocked: HERMES_TEST_ISOLATION is set")
    from plugins.deep_research import launcher

    if config is None:
        config = load_deep_research_config()
    if timeout_minutes is None:
        timeout_minutes = config.default_timeout_minutes
    result = launcher.launch(
        job_id=job_id,
        timeout_minutes=timeout_minutes,
        memory_max=config.memory_max,
        hermes_home=hermes_home,
        log_path=jobs.job_dir(job_id, hermes_home) / "runner.out",
        runner_mode=config.runner_mode,
    )
    return result.as_status()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _action_start(args: Dict[str, Any], session_id: Optional[str], task_id: Optional[str]) -> str:
    unavailable = availability_error()
    if unavailable:
        return _error("unavailable", unavailable)

    brief = args.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        return _error("invalid_brief", "brief is required and must be a non-empty string")
    if len(brief) > MAX_BRIEF_CHARS:
        return _error("invalid_brief", f"brief exceeds {MAX_BRIEF_CHARS} characters")

    questions: Optional[List[str]] = None
    raw_questions = args.get("research_questions")
    if raw_questions is not None:
        if not isinstance(raw_questions, list) or not (MIN_QUESTIONS <= len(raw_questions) <= MAX_QUESTIONS):
            return _error(
                "invalid_research_questions",
                f"research_questions must be a list of {MIN_QUESTIONS}-{MAX_QUESTIONS} lane objectives",
            )
        cleaned: List[str] = []
        for item in raw_questions:
            if not isinstance(item, str) or not item.strip():
                return _error(
                    "invalid_research_questions", "each research question must be a non-empty string"
                )
            if len(item) > MAX_QUESTION_CHARS:
                return _error(
                    "invalid_research_questions",
                    f"a research question exceeds {MAX_QUESTION_CHARS} characters",
                )
            cleaned.append(item.strip())
        questions = cleaned

    config = load_deep_research_config()
    timeout_minutes = _clamp_int(
        args.get("timeout_minutes"), config.default_timeout_minutes,
        MIN_TIMEOUT_MINUTES, MAX_TIMEOUT_MINUTES, "timeout_minutes",
    )
    if isinstance(timeout_minutes, str):
        return timeout_minutes
    max_parallel = _clamp_int(
        args.get("max_parallel"), config.max_parallel,
        MIN_MAX_PARALLEL, MAX_MAX_PARALLEL, "max_parallel",
    )
    if isinstance(max_parallel, str):
        return max_parallel

    hermes_home = _hermes_home()
    try:
        created = jobs.create_job(
            brief=brief,
            research_questions=questions,
            timeout_minutes=timeout_minutes,
            max_parallel=max_parallel,
            worker_profile=config.worker_profile,
            origin=notify.origin_context(session_id, task_id),
            hermes_home=hermes_home,
        )
    except OSError as exc:
        return _error("job_create_failed", f"could not create the job directory: {exc}")

    directory: Path = created["dir"]
    job_id: str = created["job_id"]
    try:
        runner_info = launch_job(job_id, hermes_home, config, timeout_minutes)
    except Exception as exc:  # noqa: BLE001 — the job exists; report, never crash
        jobs.mark_all_lanes_cancelled(directory)
        jobs.finish_job(
            directory, jobs.STATE_FAILED, error=f"runner launch failed: {exc}", phase="launch_failed"
        )
        return json.dumps(
            {
                "ok": False,
                "error": "launch_failed",
                "message": _bounded(exc),
                "job_id": job_id,
                "job_dir": str(directory),
                "state": jobs.STATE_FAILED,
            },
            ensure_ascii=True,
        )

    jobs.record_runner_info(directory, runner_info)

    payload = {
        "job_id": job_id,
        "state": jobs.read_status(directory).get("state") or jobs.STATE_QUEUED,
        "job_dir": str(directory),
        "runner_mode": runner_info.get("runner_mode"),
        "lanes": {"total": len(questions) if questions else 1},
        "timeout_minutes": timeout_minutes,
        "max_parallel": max_parallel,
        "worker_profile": config.worker_profile,
        "next": (
            f"job started; it re-enters this session on completion. Use "
            f"{TOOL_NAME} status/{job_id} to check progress and "
            f"{TOOL_NAME} result when finished. Do not run web searches in "
            f"parallel with this job."
        ),
    }
    if runner_info.get("runner_mode") == "fallback":
        payload["durability"] = (
            "fallback runner (no systemd user manager detected): the job "
            "survives a gateway restart but not a host supervisor stop"
        )
    return _ok(**payload)


def _clamp_int(value: Any, default: int, low: int, high: int, name: str) -> Any:
    """Validate an optional integer knob, clamped to the schema window."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return _error("invalid_" + name, f"{name} must be an integer between {low} and {high}")
    return max(low, min(high, value))


def _action_status(args: Dict[str, Any]) -> str:
    directory, error = _resolve_job(args)
    if error:
        return error
    return _ok(**_status_summary(directory))


def _action_cancel(args: Dict[str, Any]) -> str:
    directory, error = _resolve_job(args)
    if error:
        return error
    status = jobs.read_status(directory)
    if status.get("state") in jobs.TERMINAL_STATES:
        return _ok(
            job_id=directory.name,
            state=status.get("state"),
            already_terminal=True,
            note=f"job already finished as {status.get('state')}; nothing to cancel",
        )

    from plugins.deep_research import launcher

    stopped = launcher.cancel_runner(status)
    jobs.mark_all_lanes_cancelled(directory)
    finished = jobs.finish_job(
        directory, jobs.STATE_CANCELLED, error=None if stopped else "cancel: runner stop not confirmed"
    )
    state = (finished or jobs.read_status(directory)).get("state")
    return _ok(job_id=directory.name, state=state, runner_stopped=stopped)


def _action_result(args: Dict[str, Any]) -> str:
    directory, error = _resolve_job(args)
    if error:
        return error
    status = jobs.read_status(directory)
    state = status.get("state")
    if state != jobs.STATE_COMPLETED:
        # Never a partial report: only what is provably finished.
        payload = _status_summary(directory)
        payload["note"] = (
            "job is not completed; there is no report yet. Check status "
            "again, or cancel it if it is no longer wanted."
        )
        payload["ok"] = False
        payload["error"] = "not_completed"
        return json.dumps(payload, ensure_ascii=True)

    report = jobs.read_report(directory)
    request = jobs.read_request(directory)
    lanes = status.get("lanes") or []
    return _ok(
        job_id=directory.name,
        state=jobs.STATE_COMPLETED,
        report=report,
        report_path=str(directory / "report.md"),
        evidence_path=str(jobs.evidence_path(directory)),
        sources_recorded=len(jobs.read_evidence_urls(directory)),
        lanes=[
            {"index": lane.get("index"), "state": lane.get("state"), "question": _bounded(lane.get("question"), 200)}
            for lane in lanes
        ],
        citation_check={
            "validated": "url-provenance",
            "correction_pass_used": bool((status.get("synthesis") or {}).get("correction_used")),
            "limitation": PROVENANCE_NOTE,
        },
        brief=_bounded(request.get("brief"), 400),
        completed_at=_iso(status.get("completed_at")),
    )


def _action_list(args: Dict[str, Any]) -> str:
    config = load_deep_research_config()
    limit = args.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= config.max_recent_jobs)):
        return _error("invalid_limit", f"limit must be an integer between 1 and {config.max_recent_jobs}")
    entries = jobs.list_recent_jobs(limit or config.max_recent_jobs, _hermes_home())
    for entry in entries:
        entry["created_at"] = _iso(entry.get("created_at"))
        entry["updated_at"] = _iso(entry.get("updated_at"))
        entry["error"] = _bounded(entry.get("error"))
    return _ok(jobs=entries, count=len(entries))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handle_delegate_research(
    args: Dict[str, Any], session_id: Optional[str] = None, task_id: Optional[str] = None, **_: Any
) -> str:
    """Tool entry point: dispatch one action and return a JSON string."""
    action = str(args.get("action") or "").strip()
    if action == "start":
        return _action_start(args, session_id, task_id)
    if action == "status":
        return _action_status(args)
    if action == "cancel":
        return _action_cancel(args)
    if action == "result":
        return _action_result(args)
    if action == "list":
        return _action_list(args)
    return _error("invalid_action", "action must be one of start, status, cancel, result, list")


__all__ = [
    "DELEGATE_RESEARCH_SCHEMA",
    "PROVENANCE_NOTE",
    "TOOL_NAME",
    "availability_error",
    "check_requirements",
    "handle_delegate_research",
    "launch_job",
]
