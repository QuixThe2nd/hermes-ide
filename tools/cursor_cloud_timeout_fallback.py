"""One-shot local fallback for a Cursor Cloud Agent poll timeout.

A Cloud Agent edits a remote checkout built from pushed refs, not this
host's workdir, so a Cloud poll timeout leaves the scratch clone untouched —
that is what makes a single fall-through to a local CLI run safe. The gate
is a pair of ``git status --porcelain=v1`` snapshots around
``_execute_cloud_delegation``: both inspected successfully and byte-identical
(including both empty) means the tree is untouched and the local fallback
may write to it; anything else returns the timeout with an explicit
do-not-retry-Cloud note and writes nothing locally.

Only the two poll-timeout shapes reach this module: the
``GET /v1/agents/{id}/runs/{runId}`` request timeout raised as
``CursorCloudError``, and the wall-clock ``_local_error == "timeout"`` run.
HTTP errors, interrupts, and every other failure keep their existing
cloud-only result. This does not weaken the missing-API-key rule: a missing
or empty ``CURSOR_API_KEY`` fails clearly before any cloud work starts, long
before a poll timeout can occur.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from tools.agent_cli_runner import run_agent_cli
from tools.cursor_run_receipts import cursor_runs_dir
from tools.delegate_evidence_gate import apply_evidence_gate
from tools.delegate_tool_config import _cfg

GIT_STATUS_TIMEOUT_SECONDS = 10
CLOUD_TIMEOUT_NOTE = (
    "A Cloud timeout leaves the scratch clone untouched: the Cursor Cloud "
    "Agent edits a remote checkout built from pushed refs, not this workdir."
)
DO_NOT_RETRY_CLOUD_NOTE = "Do not retry Cloud for this task."
FALLBACK_TOOL_CALL_SUFFIX = ":cloud-timeout-fallback"

# Exactly the message _http_request builds for a GET /runs poll timeout;
# HTTP errors ("Cursor Cloud Agent API error (GET …): HTTP 500") and every
# other CursorCloudError value must NOT match.
_GET_POLL_TIMEOUT_RE = re.compile(
    r"^Cursor Cloud Agent request timed out: GET /v1/agents/[^/\s]+/runs/[^/\s]+$"
)


def snapshot_workdir_status(workdir: str) -> Status:
    """Return ``(inspected, text)`` from ``git status --porcelain=v1``.

    Fail-closed by contract: ANY failure — non-git directory, vanished
    workdir, git error, broken spawn — reports ``(False, "")`` so the
    caller does not fall through to a local writer it cannot prove safe.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
            timeout=GIT_STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return False, ""
    if proc.returncode != 0:
        return False, ""
    return True, proc.stdout or ""


def is_poll_get_timeout_error(exc: BaseException) -> bool:
    """True only for the GET /runs poll-timeout ``CursorCloudError``."""
    return bool(_GET_POLL_TIMEOUT_RE.match(str(exc)))


def is_wall_clock_poll_timeout(run: Any) -> bool:
    """True for a run dict ``poll_cloud_run`` ended on its wall clock."""
    return isinstance(run, dict) and str(run.get("_local_error") or "") == "timeout"


def build_local_cursor_argv(
    binary: str,
    *,
    task: str,
    model: Optional[str] = None,
) -> List[str]:
    """Argv for one local ``agent -p`` run. Never the API key, never --worktree."""
    from tools.cursor_agent_tool import NO_PUSH_PROMPT_PREFIX

    cmd = [binary, "-p", "--force", "--trust", "--output-format", "text"]
    model_name = str(model or "").strip()
    if model_name:
        cmd.extend(["--model", model_name])
    cmd.append(f"{NO_PUSH_PROMPT_PREFIX}{task}")
    return cmd


def run_local_cursor_cli(
    *,
    task: str,
    workdir: str,
    model: Optional[str],
    timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    """Run the local Cursor CLI once in ``workdir``.

    Returns a result payload, or ``None`` when the runner cannot start
    (missing binary, missing CLI login, auth preflight failure) so the
    caller can fall through to the Claude CLI.
    """
    from tools.cursor_agent_tool import (
        build_worker_env,
        preflight_worker_auth,
        resolve_cursor_agent_binary,
    )

    binary = resolve_cursor_agent_binary()
    if not binary:
        return None
    env = build_worker_env()
    try:
        preflight_worker_auth(binary, env)
    except Exception:
        return None

    cmd = build_local_cursor_argv(binary, task=task, model=model)
    error_code, log_path, log_text, duration, returncode = run_agent_cli(
        cmd,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        stall_watchdog_seconds=0.0,
        log_dir=cursor_runs_dir(),
        run_timestamp=datetime.now().strftime("%Y%m%d-%H%M%S"),
        env=env,
    )
    success = error_code is None and returncode == 0
    return {
        "success": success,
        "final_report": log_text.strip(),
        "delegations": [],
        "duration_seconds": round(duration, 3),
        "session_id": None,
        "log_path": log_path,
        "error": error_code
        or (None if success else f"local Cursor CLI exited with code {returncode}"),
    }


def run_local_claude_cli(
    *,
    task: str,
    workdir: str,
    timeout_seconds: int,
    hermes_session_id: str,
    tool_call_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Run one blocking local Claude CLI delegation for the same task.

    Returns its result payload, or ``None`` when the Claude CLI is not
    installed. The spawn-correlation ``tool_call_id`` is suffixed so the two
    delegation tools' receipts cannot collide.
    """
    from tools.claude_agent_tool import delegate_claude_agent, resolve_claude_binary

    if not resolve_claude_binary(None):
        return None
    result_json = delegate_claude_agent(
        task=task,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        background=False,
        session_id=hermes_session_id or None,
        tool_call_id=(
            f"{tool_call_id}{FALLBACK_TOOL_CALL_SUFFIX}" if tool_call_id else None
        ),
    )
    try:
        payload = json.loads(result_json)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        payload = {
            "success": False,
            "final_report": "",
            "error": "local Claude CLI returned an unreadable result",
        }
    return payload


def _timeout_refusal_result(
    *,
    error_detail: str,
    workdir_git_status: str,
    workdir_untouched: bool,
    reason: str,
    log_path: Optional[str],
) -> Tuple[str, str]:
    payload = {
        "success": False,
        "final_report": "",
        "delegations": [],
        "duration_seconds": 0.0,
        "session_id": None,
        "log_path": log_path,
        "error": (
            f"Cursor Cloud Agent poll timed out ({error_detail}); {reason}. "
            + DO_NOT_RETRY_CLOUD_NOTE
        ),
        "cloud_timeout": True,
        "do_not_retry_cloud": True,
        "workdir_git_status": workdir_git_status,
        "workdir_untouched": workdir_untouched,
        "fallback": None,
        "note": CLOUD_TIMEOUT_NOTE,
    }
    return json.dumps(payload, ensure_ascii=False), "failed"


def _run_local_fallback(
    *,
    task: str,
    workdir: str,
    model: Optional[str],
    timeout_seconds: int,
    hermes_session_id: str,
    tool_call_id: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Cursor CLI first, then (only if it cannot start) the Claude CLI once."""
    cursor_payload = run_local_cursor_cli(
        task=task, workdir=workdir, model=model, timeout_seconds=timeout_seconds
    )
    if cursor_payload is not None:
        return cursor_payload, "local_cursor_cli"
    claude_payload = run_local_claude_cli(
        task=task,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        hermes_session_id=hermes_session_id,
        tool_call_id=tool_call_id,
    )
    if claude_payload is not None:
        return claude_payload, "local_claude_cli"
    return None, None


def handle_cloud_poll_timeout(
    *,
    task: str,
    workdir: str,
    model: Optional[str],
    timeout_seconds: int,
    start_status: Status,
    error_detail: str,
    agent_id: str,
    run_id: str,
    api_key: str,
    hermes_session_id: str,
    tool_call_id: Optional[str],
    log_path: Optional[str] = None,
) -> Tuple[str, str]:
    """Cancel the cloud run, gate on git status, run ONE local fallback.

    Returns ``(result_json, receipt_outcome)``. Never retries Cloud: no
    second create, no second poll. ``timeout_seconds`` is the ORIGINAL
    clamped budget, handed to the local runner fresh — the cloud wall clock
    may already have consumed it (0 keeps its unlimited meaning).
    """
    from tools.cursor_agent_tool import cancel_cloud_run

    # Best-effort cancel before any local write, so a still-running cloud
    # agent cannot race the local writer. The wall-clock poll already
    # cancelled; cancelling again is idempotent and failures are swallowed.
    cancel_cloud_run(agent_id, run_id, api_key)

    inspected_now, status_now = snapshot_workdir_status(workdir)
    if not (start_status[0] and inspected_now):
        return _timeout_refusal_result(
            error_detail=error_detail,
            workdir_git_status=status_now,
            workdir_untouched=False,
            reason=(
                "the workdir git status could not be inspected, so the local "
                "fallback was suppressed"
            ),
            log_path=log_path,
        )

    if start_status[1] != status_now:
        return _timeout_refusal_result(
            error_detail=error_detail,
            workdir_git_status=status_now,
            workdir_untouched=False,
            reason=(
                "the workdir git status changed since the delegation started, "
                "so the local fallback was suppressed to avoid clobbering "
                "those changes"
            ),
            log_path=log_path,
        )

    runner_payload, fallback_name = _run_local_fallback(
        task=task,
        workdir=workdir,
        model=model,
        timeout_seconds=timeout_seconds,
        hermes_session_id=hermes_session_id,
        tool_call_id=tool_call_id,
    )
    if runner_payload is None:
        return _timeout_refusal_result(
            error_detail=error_detail,
            workdir_git_status=status_now,
            workdir_untouched=True,
            reason=(
                "no local fallback runner was available (the local Cursor CLI "
                "could not start and the local Claude CLI is unavailable)"
            ),
            log_path=log_path,
        )

    payload = dict(runner_payload)
    payload.update(
        {
            "cloud_timeout": True,
            "do_not_retry_cloud": True,
            "workdir_git_status": status_now,
            "workdir_untouched": True,
            "fallback": fallback_name,
            "note": CLOUD_TIMEOUT_NOTE,
        }
    )
    if payload.get("success") is True:
        gated = apply_evidence_gate(payload, task_text=str(task), config=_cfg())
        if gated is not payload:
            payload = gated
    outcome = "success" if payload.get("success") is True else "failed"
    return json.dumps(payload, ensure_ascii=False), outcome
