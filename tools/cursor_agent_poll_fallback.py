"""Immediate local recovery for Cursor Cloud run-poll transport timeouts."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional, Tuple

_GIT_STATUS_TIMEOUT_SECONDS = 5.0
_GIT_STATUS_OUTPUT_LIMIT = 4000
_FALLBACK_REASON = "cloud_poll_timeout"
_POLL_TIMEOUT_STATUS = "POLL_TIMEOUT"

_FALLBACK_CONTEXT = """Cloud polling timed out after the Cursor Cloud run was created. Cloud timeouts leave the Cursor scratch clone untouched. You are now running locally in the original workdir, which may contain pre-existing changes. Do not reset, clean, check out, or otherwise discard those changes. Complete the original task above."""


def _bounded_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    if len(text) <= _GIT_STATUS_OUTPUT_LIMIT:
        return text
    return text[:_GIT_STATUS_OUTPUT_LIMIT] + "\n...[truncated]"


def observe_workdir_git_status(workdir: str) -> Dict[str, Any]:
    """Take a bounded, read-only porcelain snapshot of the original workdir."""
    observation: Dict[str, Any] = {
        "command": "git status --porcelain=v1",
        "state": "error",
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=workdir,
            timeout=_GIT_STATUS_TIMEOUT_SECONDS,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        observation.update(
            state="timeout",
            stdout=_bounded_text(exc.stdout),
            stderr=_bounded_text(exc.stderr),
            error=(
                "git status --porcelain=v1 exceeded "
                f"{_GIT_STATUS_TIMEOUT_SECONDS:g} seconds"
            ),
        )
        return observation
    except OSError as exc:
        observation["error"] = _bounded_text(exc)
        return observation

    stdout = _bounded_text(completed.stdout)
    stderr = _bounded_text(completed.stderr)
    observation.update(
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if completed.returncode != 0:
        observation["error"] = stderr or (
            f"git status exited with status {completed.returncode}"
        )
    else:
        observation["state"] = "dirty" if stdout.strip() else "clean"
    return observation


def format_workspace_observation(observation: Dict[str, Any]) -> str:
    state = str(observation.get("state") or "error")
    lines = [f"Original workdir git status before local fallback: {state}."]
    stdout = str(observation.get("stdout") or "").rstrip()
    stderr = str(observation.get("stderr") or "").rstrip()
    error = str(observation.get("error") or "")
    if stdout:
        lines.append(f"git status --porcelain=v1 output:\n{stdout}")
    if stderr:
        lines.append(f"git status stderr:\n{stderr}")
    if error and error != stderr:
        lines.append(f"Workspace status diagnostic: {error}")
    return "\n".join(lines)


def build_local_fallback_task(task: str, observation: Dict[str, Any]) -> str:
    body = task.lstrip()
    has_goal = body.lower().startswith("/goal") and (
        len(body) == len("/goal") or body[len("/goal")].isspace()
    )
    prompt = task if has_goal else f"/goal {task}"
    return (
        f"{prompt}\n\n{_FALLBACK_CONTEXT}\n\n"
        f"{format_workspace_observation(observation)}"
    )


def _provenance(
    cloud_poll_error: str,
    observation: Dict[str, Any],
) -> str:
    return (
        "Cloud polling timed out: "
        f"{cloud_poll_error}. Cloud timeouts leave the Cursor scratch clone "
        "untouched; this result comes from local Claude in the original workdir.\n"
        f"{format_workspace_observation(observation)}"
    )


def _base_payload(
    *,
    cloud_agent_id: str,
    cloud_run_id: str,
    cloud_poll_error: str,
    progress_url: Optional[str],
    attempt_id: Optional[str],
    observation: Dict[str, Any],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "agent_id": cloud_agent_id,
        "run_id": cloud_run_id,
        "cloud_status": _POLL_TIMEOUT_STATUS,
        "cloud_poll_error": cloud_poll_error,
        "fallback_reason": _FALLBACK_REASON,
        "workspace_git_status": observation,
    }
    if progress_url:
        payload["progress_url"] = progress_url
    if attempt_id:
        payload["attempt_id"] = attempt_id
    return payload


def _outcome_for_local_payload(payload: Dict[str, Any]) -> str:
    if payload.get("success") is True:
        return "success"
    error = payload.get("error")
    if error == "timeout":
        return "timeout"
    if error == "interrupted":
        return "interrupted"
    return "failed"


def run_local_claude_fallback(
    *,
    task: str,
    workdir: str,
    timeout_seconds: int,
    hermes_session_id: str,
    tool_call_id: Optional[str],
    cloud_agent_id: str,
    cloud_run_id: str,
    cloud_poll_error: str,
    progress_url: Optional[str],
    attempt_id: Optional[str],
    cloud_log_path: str,
    delivery_mode: str,
    observation: Dict[str, Any],
) -> Tuple[Dict[str, Any], str]:
    """Return the actual Claude result, enriched with cloud/workspace provenance."""
    provenance = _provenance(cloud_poll_error, observation)
    cloud_fields = _base_payload(
        cloud_agent_id=cloud_agent_id,
        cloud_run_id=cloud_run_id,
        cloud_poll_error=cloud_poll_error,
        progress_url=progress_url,
        attempt_id=attempt_id,
        observation=observation,
    )
    if delivery_mode == "background":
        error = (
            f"Cursor Cloud Agent run polling timed out ({cloud_poll_error}); "
            "automatic local Claude fallback is refused for background delivery "
            "because the existing cloud interrupt handle cannot terminate a "
            "transferred local process. NO LOCAL WORK WAS STARTED. Retry with "
            "background=false to permit the immediate local fallback."
        )
        payload: Dict[str, Any] = {
            "success": False,
            "final_report": f"{provenance}\n{error}",
            "delegations": [],
            "duration_seconds": 0.0,
            "session_id": None,
            "log_path": cloud_log_path or None,
            "error": error,
            **cloud_fields,
            "fallback_backend": None,
            "fallback_refused": True,
        }
        return payload, "failed"

    from tools.claude_agent_tool import delegate_claude_agent

    local_task = build_local_fallback_task(task, observation)
    try:
        raw_result = delegate_claude_agent(
            task=local_task,
            workdir=workdir,
            timeout_seconds=timeout_seconds,
            background=False,
            session_id=hermes_session_id,
            tool_call_id=tool_call_id,
        )
    except Exception as exc:
        raw_result = json.dumps(
            {"success": False, "error": str(exc), "final_report": ""}
        )

    try:
        decoded = json.loads(raw_result)
        if not isinstance(decoded, dict):
            raise ValueError("local Claude result was not an object")
        local_payload: Dict[str, Any] = dict(decoded)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        local_payload = {
            "success": False,
            "error": f"local Claude fallback returned an invalid result: {exc}",
            "final_report": "",
        }

    local_payload.setdefault("delegations", [])
    local_payload.setdefault("duration_seconds", 0.0)
    local_payload.setdefault("session_id", None)
    if not local_payload.get("log_path"):
        local_payload["log_path"] = cloud_log_path or None
    if local_payload.get("success") is not True:
        local_error = local_payload.get("error")
        if local_error not in ("timeout", "interrupted"):
            detail = str(local_error or "local Claude failed without an error detail")
            local_payload["error"] = (
                f"Cursor Cloud Agent run polling timed out ({cloud_poll_error}); "
                f"local Claude fallback failed: {detail}"
            )
    local_payload.update(cloud_fields)
    local_payload["fallback_backend"] = "claude"
    local_report = str(local_payload.get("final_report") or "")
    local_payload["final_report"] = provenance + (
        f"\n\n{local_report}" if local_report else ""
    )
    return local_payload, _outcome_for_local_payload(local_payload)


def finalized_local_fallback_result(
    receipt: Dict[str, Any],
) -> Optional[str]:
    """Return only a structurally complete, finalized local-fallback result."""
    terminal = receipt.get("terminal_result")
    if not isinstance(terminal, dict):
        return None
    result_json = terminal.get("result_json")
    if not isinstance(result_json, str):
        return None
    try:
        payload = json.loads(result_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("fallback_reason") != _FALLBACK_REASON
        or payload.get("fallback_backend") != "claude"
        or payload.get("cloud_status") != _POLL_TIMEOUT_STATUS
        or not isinstance(payload.get("success"), bool)
        or not isinstance(payload.get("workspace_git_status"), dict)
        or not str(payload.get("cloud_poll_error") or "").strip()
        or payload.get("agent_id") != receipt.get("cloud_agent_id")
        or payload.get("run_id") != receipt.get("cloud_run_id")
        or receipt.get("outcome") != _outcome_for_local_payload(payload)
    ):
        return None
    return result_json
