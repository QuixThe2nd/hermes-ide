"""Synchronous OMP subprocess backend for configured delegation lanes."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from agent.secret_scope import load_env_file
from tools.delegate_lane_specs import resolve_lane_path, soul_path
from tools.delegate_tool_config import _cfg
from tools.environments.local import build_subprocess_env

_QUOTA_STATUSES = {403, 429}
_QUOTA_MARKERS = (
    "429", "rate limit", "rate-limit", "rate_limit", "quota",
    "too many requests", "too_many_requests",
)


def _integer_status(value: Any) -> Optional[int]:
    if isinstance(value, Mapping):
        value = value.get("status", value.get("code"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_quota_error(payload: Any) -> bool:
    """Classify only transport/quota failures as eligible for model fallback."""
    if isinstance(payload, Mapping):
        status = _integer_status(
            payload.get("errorStatus", payload.get("error_status"))
        )
        if status in _QUOTA_STATUSES:
            return True
        text = str(payload.get("error") or payload.get("message") or "")
    else:
        text = str(payload or "")
    lowered = text.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


def _event_error(event: Mapping[str, Any]) -> tuple[Optional[str], Optional[int]]:
    error = event.get("error")
    nested = error if isinstance(error, Mapping) else {}
    status = _integer_status(
        event.get("errorStatus", nested.get("errorStatus", nested.get("status")))
    )
    is_error = bool(error) or status is not None or event.get("type") == "error"
    if not is_error:
        return None, None
    if isinstance(error, str):
        message = error
    elif isinstance(nested.get("message"), str):
        message = nested["message"]
    elif isinstance(event.get("message"), str):
        message = event["message"]
    else:
        message = f"OMP error event (status {status})" if status else "OMP error event"
    return message, status


def parse_omp_events(stdout: str) -> Dict[str, Any]:
    """Read OMP JSONL, taking the last ``turn_end`` assistant text and error."""
    final_parts: list[str] = []
    error: Optional[str] = None
    status: Optional[int] = None
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        event_error, event_status = _event_error(event)
        if event_error:
            error, status = event_error, event_status
        if event.get("type") != "turn_end":
            continue
        message = event.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            str(part.get("text"))
            for part in content
            if isinstance(part, Mapping) and part.get("text") is not None
        ]
        if texts:
            final_parts = texts
    result: Dict[str, Any] = {"final_report": "".join(final_parts), "error": error}
    if status is not None:
        result["errorStatus"] = status
    result["quota"] = bool(error) and is_quota_error(result)
    return result


def build_omp_argv(
    model: str,
    prompt: str,
    *,
    binary: str = "omp",
    soul: Optional[Path] = None,
    tools_allowlist: Optional[Iterable[str]] = None,
) -> list[str]:
    """Build the non-interactive argv; credentials never appear in arguments."""
    argv = [
        binary, "-p", "--mode", "json", "--no-session", "--auto-approve",
        "--model", model,
    ]
    if soul is not None:
        argv.extend(["--append-system-prompt", str(soul)])
    tools = [str(tool).strip() for tool in (tools_allowlist or []) if str(tool).strip()]
    if tools:
        argv.extend(["--tools", ",".join(tools)])
    argv.append(prompt)
    return argv


# --- RPC goal mode ------------------------------------------------------------
#
# Open-ended lanes (reverse engineering) must keep working past one model turn.
# OMP's print mode exits after a single turn and ``--goal`` is interactive-only,
# so the goal path drives ``omp --mode rpc`` over JSONL instead: a temp config
# overlay enables goal continuation for RPC hosts, then one ``goal create``
# command owns the whole run and OMP itself decides when to continue and stop.

_GOAL_OVERLAY_YAML = "goal:\n  enabled: true\n  continuationModes:\n    - rpc\n"
# GoalStatus values from OMP's RPC wire: terminal statuses other than
# ``complete`` mean the goal stopped early and the run failed.
_GOAL_COMPLETED = "complete"
_GOAL_STOPPED = frozenset({"paused", "budget-limited", "dropped"})


def build_omp_goal_argv(
    model: str,
    *,
    binary: str = "omp",
    soul: Optional[Path] = None,
    tools_allowlist: Optional[Iterable[str]] = None,
    config_overlay: Optional[Path] = None,
    session_dir: Optional[Path] = None,
) -> list[str]:
    """Build the RPC goal-mode argv; the objective rides JSONL, not arguments."""
    argv = [binary, "--mode", "rpc", "--no-ui", "--auto-approve", "--model", model]
    if config_overlay is not None:
        argv.extend(["--config", str(config_overlay)])
    if session_dir is not None:
        argv.extend(["--session-dir", str(session_dir)])
    if soul is not None:
        argv.extend(["--append-system-prompt", str(soul)])
    tools = [str(tool).strip() for tool in (tools_allowlist or []) if str(tool).strip()]
    if tools:
        argv.extend(["--tools", ",".join(tools)])
    return argv


def _assistant_text(message: Any) -> str:
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text"))
        for part in content
        if isinstance(part, Mapping) and part.get("text") is not None
    )


def _goal_status(goal: Any) -> Optional[str]:
    return goal.get("status") if isinstance(goal, Mapping) else None


def run_omp_goal(
    spec: Mapping[str, Any],
    task: str,
    workdir: str,
    model: str,
    *,
    timeout_seconds: int = 0,
    omp_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one goal-mode attempt: continue until OMP reports the goal terminal.

    Success requires the goal to complete (status ``complete`` or a null goal
    after activation). A stopped goal (paused / budget-limited / dropped), a
    refused create, or process exit before completion all fail, keeping the
    last assistant text as the wrap-up report.
    """
    started = time.monotonic()
    base = {"model": model, "success": False, "final_report": ""}
    directory = Path(workdir)
    if not directory.is_absolute() or not directory.is_dir():
        return {**base, "error": f"workdir must be an existing absolute directory: {workdir}"}
    config = dict(omp_config if omp_config is not None else (_cfg().get("omp") or {}))
    try:
        api_key = _litellm_api_key(config)
        with _soul_file(str(spec.get("soul") or "")) as soul:
            return _run_omp_goal_process(
                spec, task, directory, model, str(config.get("binary") or "omp"),
                soul, api_key, timeout_seconds,
            )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**base, "error": str(exc), "duration_seconds": round(time.monotonic() - started, 3)}


def _run_omp_goal_process(
    spec, task, directory, model, binary, soul, api_key, timeout_seconds,
) -> Dict[str, Any]:
    started = time.monotonic()
    base = {"model": model, "success": False, "final_report": ""}
    omp_dir = directory / ".omp"
    omp_dir.mkdir(parents=True, exist_ok=True)
    # Temp config overlay: continuation is enabled per-run, never by writing
    # the user's omp config.
    overlay = omp_dir / "goal-overlay.yml"
    overlay.write_text(_GOAL_OVERLAY_YAML, encoding="utf-8")
    session_dir = omp_dir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    mcp_path = stage_mcp_config(directory, spec.get("mcp"))
    argv = build_omp_goal_argv(
        model, binary=binary, soul=soul,
        tools_allowlist=spec.get("tools_allowlist"),
        config_overlay=overlay, session_dir=session_dir,
    )
    env = build_subprocess_env(extra={"LITELLM_API_KEY": api_key})
    process = subprocess.Popen(
        argv, cwd=str(directory), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    state = {"final_report": "", "error": None, "completed": False, "active": False}
    try:
        # timeout_seconds > 0 bounds the process; 0 means no subprocess timeout.
        deadline = time.monotonic() + int(timeout_seconds) if int(timeout_seconds) > 0 else None
        _goal_session(process, task, deadline, state)
    finally:
        _terminate(process)
    error = state["error"]
    if state["completed"] and not error and not state["final_report"]:
        error = "OMP goal completed without a final assistant text"
    result = {
        **base,
        "success": state["completed"] and not error,
        "final_report": state["final_report"] or "",
        "error": error,
        "duration_seconds": round(time.monotonic() - started, 3),
        "quota": bool(error) and is_quota_error({"error": error}),
    }
    if mcp_path is not None:
        result["mcp_path"] = str(mcp_path)
    return result


def _goal_session(process: subprocess.Popen, task: str, deadline, state: dict) -> None:
    """Speak JSONL with the RPC child until the goal reaches a terminal state.

    Mutates ``state`` (final_report / error / completed / active). One command
    is sent — ``goal create`` with the objective — after the ready frame, and
    no user prompt; the last ``turn_end`` assistant text is kept as the
    wrap-up report whatever the outcome.
    """
    command_id = 1
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = ""
    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state["error"] = state["error"] or "OMP goal run exceeded timeout_seconds"
                return
            events = selector.select(remaining)
        else:
            events = selector.select()
        if not events:
            continue
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            state["error"] = state["error"] or (
                "OMP exited before the goal completed"
                if process.poll() is None else
                f"OMP exited with code {process.returncode} before the goal completed"
            )
            return
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(frame, dict):
                continue
            if _handle_goal_frame(process, frame, command_id, task, state) is not None:
                return


def _handle_goal_frame(process, frame, command_id, task, state) -> Optional[bool]:
    """Apply one RPC frame; return a truthy sentinel when the drive is over."""
    kind = frame.get("type")
    if kind == "ready":
        _send_goal_create(process, command_id, task)
        return None
    if kind == "turn_end":
        text = _assistant_text(frame.get("message"))
        if text:
            state["final_report"] = text
        return None
    if kind == "response" and frame.get("id") == command_id:
        if frame.get("success") is False:
            message = frame.get("message") or frame.get("error") or "unknown error"
            state["error"] = state["error"] or f"OMP goal create refused: {message}"
            return True
        data = frame.get("data")
        goal = data.get("goal") if isinstance(data, Mapping) else None
        return _apply_goal_status(goal, state)
    if kind == "goal_updated":
        return _apply_goal_status(frame.get("goal"), state)
    frame_error, _status = _event_error(frame)
    if frame_error and not state["error"]:
        # Sticky: the run may still complete, but a failing exit keeps it.
        state["error"] = frame_error
    return None


def _apply_goal_status(goal: Any, state: dict) -> Optional[bool]:
    """Classify one goal sighting; truthy return ends the drive."""
    if goal is None:
        if state["active"]:
            # The goal object goes null once goal mode exits after completion.
            state["completed"] = True
            return True
        return None
    status = _goal_status(goal)
    if status == "active":
        state["active"] = True
        return None
    if status == _GOAL_COMPLETED:
        state["completed"] = True
        return True
    if status in _GOAL_STOPPED:
        state["error"] = state["error"] or f"OMP goal stopped early: {status}"
        return True
    return None


def _send_goal_create(process: subprocess.Popen, command_id: int, objective: str) -> None:
    command = {"id": command_id, "type": "goal", "op": "create", "objective": objective}
    process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def stage_mcp_config(workdir: Path, mcp: Any) -> Optional[Path]:
    """Stage only a declared config-named MCP source into ``.omp/mcp.json``."""
    if not mcp:
        return None
    source_value = mcp.get("source") if isinstance(mcp, Mapping) else mcp
    if not isinstance(source_value, str) or not source_value.strip():
        raise ValueError("lane mcp must be a source file path or {'source': path}")
    source = resolve_lane_path(source_value)
    if not source.is_file():
        raise FileNotFoundError(f"lane MCP source file does not exist: {source}")
    target = workdir / ".omp" / "mcp.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    return target


def _litellm_api_key(omp_config: Mapping[str, Any]) -> str:
    configured = omp_config.get("secrets_file")
    if not configured:
        raise ValueError(
            "delegation.omp.secrets_file must name a dotenv file containing LITELLM_API_KEY"
        )
    path = resolve_lane_path(str(configured))
    key = load_env_file(path).get("LITELLM_API_KEY", "").strip()
    if not key:
        raise ValueError(f"LITELLM_API_KEY is missing or empty in {path}")
    return key


@contextmanager
def _soul_file(soul: str):
    path = soul_path(soul)
    if path is not None or not soul:
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="hermes-omp-soul-") as temp_dir:
        literal = Path(temp_dir) / "soul.md"
        literal.write_text(soul, encoding="utf-8")
        yield literal


def run_omp_lane(
    spec: Mapping[str, Any],
    task: str,
    workdir: str,
    model: str,
    *,
    timeout_seconds: int = 0,
    omp_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one model attempt and return a normalized, JSON-serializable result."""
    started = time.monotonic()
    base = {"model": model, "success": False, "final_report": ""}
    directory = Path(workdir)
    if not directory.is_absolute() or not directory.is_dir():
        return {**base, "error": f"workdir must be an existing absolute directory: {workdir}"}
    config = dict(omp_config if omp_config is not None else (_cfg().get("omp") or {}))
    if spec.get("goal") is True:
        return run_omp_goal(
            spec, task, workdir, model,
            timeout_seconds=timeout_seconds, omp_config=config,
        )
    try:
        api_key = _litellm_api_key(config)
        with _soul_file(str(spec.get("soul") or "")) as soul:
            argv = build_omp_argv(
                model, task, binary=str(config.get("binary") or "omp"), soul=soul,
                tools_allowlist=spec.get("tools_allowlist"),
            )
            mcp_path = stage_mcp_config(directory, spec.get("mcp"))
            env = build_subprocess_env(extra={"LITELLM_API_KEY": api_key})
            completed = subprocess.run(
                argv, cwd=str(directory), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                timeout=int(timeout_seconds) if int(timeout_seconds) > 0 else None,
                check=False,
            )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**base, "error": str(exc), "duration_seconds": time.monotonic() - started}
    parsed = parse_omp_events(completed.stdout)
    error = parsed.get("error")
    if completed.returncode != 0 and not error:
        detail = (completed.stderr or "").strip()[-2000:]
        error = f"OMP exited with code {completed.returncode}" + (f": {detail}" if detail else "")
    if completed.returncode == 0 and not error and not parsed.get("final_report"):
        error = "OMP returned no final assistant text"
    result = {
        **base,
        "success": completed.returncode == 0 and not error,
        "final_report": parsed.get("final_report") or "",
        "error": error,
        "duration_seconds": round(time.monotonic() - started, 3),
        "quota": bool(error) and is_quota_error({**parsed, "error": error}),
    }
    if parsed.get("errorStatus") is not None:
        result["errorStatus"] = parsed["errorStatus"]
    if mcp_path is not None:
        result["mcp_path"] = str(mcp_path)
    return result
