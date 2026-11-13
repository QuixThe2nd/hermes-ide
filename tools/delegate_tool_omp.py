"""Synchronous OMP subprocess backend for configured delegation lanes."""

from __future__ import annotations

import json
import os
import re
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
from tools.harness_persist import (
    caller_killed_service_cgroup,
    launch_detached_service,
    register_surviving_harness,
    wait_service,
)

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


_MAX_RESTORES = 2
_NON_RECOVERABLE_MARKERS = (
    "in cooldown",
    "connection refused",
    "invalid api key",
    "invalid_api_key",
)
_RECOVERABLE_MARKERS = (
    "connection closed",
    "timeout on reading data from socket",
)
_STATUS_TOKEN = re.compile(r"(?<!\d)(401|429)(?!\d)")
_LOG_NAME = re.compile(r"^omp\..+\.(\d+)\.log$")
_MISSING_FINAL = "no final assistant text"
_LOG_SIGNALS = _RECOVERABLE_MARKERS + _NON_RECOVERABLE_MARKERS + ("429", "401", "error")


def recoverable_lane_failure(error: Any, returncode: Optional[int], session_exists: bool) -> bool:
    """Interruptions can resume; quota, auth, and refused connections cannot."""
    text = "" if error is None else str(error)
    lowered = text.lower()
    if _STATUS_TOKEN.search(lowered) or any(marker in lowered for marker in _NON_RECOVERABLE_MARKERS):
        return False
    if any(marker in lowered for marker in _RECOVERABLE_MARKERS):
        return True
    if not session_exists:
        return False
    if returncode is None:
        return True
    return error is None or not text.strip() or _MISSING_FINAL in lowered


def build_omp_argv(
    model: str,
    prompt: str,
    *,
    binary: str = "omp",
    soul: Optional[Path] = None,
    tools_allowlist: Optional[Iterable[str]] = None,
    session_dir: Optional[Path] = None,
    restore: Optional[Path] = None,
) -> list[str]:
    """Build the non-interactive argv; credentials never appear in arguments."""
    argv = [
        binary, "-p", "--mode", "json", "--auto-approve", "--model", model,
    ]
    if session_dir is not None:
        argv.extend(["--session-dir", str(session_dir)])
    if restore is not None:
        argv.extend(["--restore", str(restore)])
    if soul is not None:
        argv.extend(["--append-system-prompt", str(soul)])
    tools = [str(tool).strip() for tool in (tools_allowlist or []) if str(tool).strip()]
    if tools:
        argv.extend(["--tools", ",".join(tools)])
    if restore is None:
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



class _Completed:
    def __init__(self, returncode, stdout: str, stderr: str, pid: Optional[int] = None, pids=()) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid
        self.pids = tuple(pids)


class _PidPopen(subprocess.Popen):
    latest: Optional[int] = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        type(self).latest = self.pid


def omp_log_dir() -> Path:
    """Directory omp writes ``omp.<date>.<pid>.log`` into. Date is not fixed."""
    config_name = os.environ.get("PI_CONFIG_DIR") or ".omp"
    config_root = Path(config_name)
    if not config_root.is_absolute():
        config_root = Path.home() / config_name
    profile = (os.environ.get("OMP_PROFILE") or os.environ.get("PI_PROFILE") or "").strip()
    if profile:
        config_root = config_root / "profiles" / profile
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        xdg_root = Path(xdg) / "omp"
        if profile:
            profile_root = xdg_root / "profiles" / profile
            if profile_root.is_dir():
                return profile_root / "logs"
        if xdg_root.is_dir():
            return xdg_root / "logs"
    return config_root / "logs"


def omp_log_for_pid(pid: int, *, directory: Optional[Path] = None) -> Optional[Path]:
    root = directory or omp_log_dir()
    if not root.is_dir():
        return None
    matches = []
    for path in root.glob(f"omp.*.{pid}.log"):
        match = _LOG_NAME.match(path.name)
        if match and int(match.group(1)) == int(pid) and path.is_file():
            matches.append(path)
    if not matches:
        return None
    return max(matches, key=lambda item: item.stat().st_mtime)


def _lane_session_dir(workdir: Path, model: str) -> Path:
    root = (workdir / ".hermes-lane").resolve()
    relative = Path(model)
    parts = relative.parts
    if not parts or relative.is_absolute() or any(part in ("", ".", "..") for part in parts):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("._") or "model"
        parts = (safe,)
    session_dir = root.joinpath(*parts).resolve()
    if session_dir != root and root not in session_dir.parents:
        raise ValueError(f"model escapes lane session dir: {model}")
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _write_live(workdir: Path, session_dir: Path, model: str) -> None:
    lane = workdir / ".hermes-lane"
    lane.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"session_dir": str(session_dir), "model": model}, ensure_ascii=False)
    temporary = lane / ".live.json.tmp"
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(lane / "live.json")


def _session_jsonl(session_dir: Path) -> Optional[Path]:
    if not session_dir.is_dir():
        return None
    files = [path for path in session_dir.rglob("*.jsonl") if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def _child_pids(completed) -> list[int]:
    pids = []
    raw = getattr(completed, "pids", None) or ()
    for value in (*raw, getattr(completed, "pid", None)):
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _descendant_pids(pid: Optional[int]) -> list[int]:
    if not pid:
        return []
    found = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rparen = stat.rfind(")")
        fields = stat[rparen + 2:].split() if rparen >= 0 else []
        if len(fields) > 1 and fields[1] == str(pid):
            child = int(entry.name)
            found.append(child)
            found.extend(_descendant_pids(child))
    return found


def _diagnostic_line(path: Path) -> Optional[str]:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return None
    if not lines:
        return None
    for line in reversed(lines):
        lowered = line.lower()
        if any(signal in lowered for signal in _LOG_SIGNALS):
            return line[-2000:]
    return lines[-1][-2000:]


def _log_error_for_pids(pids) -> Optional[str]:
    chosen = None
    chosen_mtime = -1.0
    for pid in pids:
        path = omp_log_for_pid(pid)
        if path is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= chosen_mtime:
            chosen = path
            chosen_mtime = mtime
    if chosen is None:
        return None
    return _diagnostic_line(chosen)


def _attach_pid(completed, pid: Optional[int], pids=()):
    try:
        if getattr(completed, "pid", None) is None and pid:
            completed.pid = pid
        if pids and not getattr(completed, "pids", None):
            completed.pids = tuple(pids)
        return completed
    except Exception:
        wrapped = _Completed(completed.returncode, completed.stdout, completed.stderr, pid=pid, pids=pids)
        return wrapped


def _run_in_process(argv, directory: Path, env: dict, timeout_seconds: int):
    runner = subprocess.run
    kwargs = dict(
        cwd=str(directory), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        timeout=timeout_seconds or None, check=False,
    )
    if getattr(runner, "__module__", "") != "subprocess":
        return runner(argv, **kwargs)
    original = subprocess.Popen
    subprocess.Popen = _PidPopen
    try:
        completed = runner(argv, **kwargs)
    finally:
        subprocess.Popen = original
    return _attach_pid(completed, _PidPopen.latest)


def _run_omp_process(argv, directory: Path, env: dict, *, timeout_seconds: int):
    """Run omp in-process, or outside the gateway cgroup when a unit stop would kill it."""
    if not caller_killed_service_cgroup():
        return _run_in_process(argv, directory, env, timeout_seconds)
    stdout_path = directory / ".hermes-harness.stdout"
    stderr_path = directory / ".hermes-harness.stderr"
    launched = launch_detached_service(
        argv, cwd=str(directory), env=env, stdout_path=stdout_path, stderr_path=stderr_path,
    )
    wrapper_pid = launched.get("pid")
    descendants = _descendant_pids(wrapper_pid)
    register_surviving_harness(
        command=" ".join(argv[:8]), pid=wrapper_pid, unit=launched["unit"], cwd=str(directory),
    )
    code = wait_service(launched["unit"], timeout_seconds=timeout_seconds)
    code_path = Path(str(stdout_path) + ".code")
    if code_path.exists():
        try:
            code = int(code_path.read_text(encoding="utf-8").strip())
        except ValueError:
            code = 1
    return _Completed(
        code,
        stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else "",
        stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else "",
        pid=wrapper_pid,
        pids=descendants,
    )


def _result_from_completed(base, completed, started, mcp_path) -> Dict[str, Any]:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    code = completed.returncode
    parsed = parse_omp_events(stdout)
    error = parsed.get("error")
    if not stdout.strip() and not stderr.strip() and code in (0, None) and not error:
        log_error = _log_error_for_pids(_child_pids(completed))
        if log_error:
            error = log_error
    if code not in (0, None) and not error:
        detail = stderr.strip()[-2000:]
        error = f"OMP exited with code {code}" + (f": {detail}" if detail else "")
    elif code in (0, None) and not error and not (parsed.get("final_report") or "").strip():
        detail = stderr.strip()[-2000:]
        error = detail or "OMP returned no final assistant text"
    result = {
        **base,
        "success": code == 0 and not error,
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


def run_omp_lane(
    spec: Mapping[str, Any],
    task: str,
    workdir: str,
    model: str,
    *,
    timeout_seconds: int = 0,
    omp_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one model attempt, resuming the same omp session after an interruption."""
    started = time.monotonic()
    base = {"model": model, "success": False, "final_report": ""}
    directory = Path(workdir)
    if not directory.is_absolute() or not directory.is_dir():
        return {**base, "error": f"workdir must be an existing absolute directory: {workdir}"}
    session_dir = _lane_session_dir(directory, model)
    _write_live(directory, session_dir, model)
    base["session_dir"] = str(session_dir)
    config = dict(omp_config if omp_config is not None else (_cfg().get("omp") or {}))
    if spec.get("goal") is True:
        return run_omp_goal(
            spec, task, workdir, model,
            timeout_seconds=timeout_seconds, omp_config=config,
        )
    try:
        api_key = _litellm_api_key(config)
        with _soul_file(str(spec.get("soul") or "")) as soul:
            mcp_path = stage_mcp_config(directory, spec.get("mcp"))
            env = build_subprocess_env(extra={"LITELLM_API_KEY": api_key})
            binary = str(config.get("binary") or "omp")
            timeout = int(timeout_seconds) if int(timeout_seconds) > 0 else 0
            restore_path = None
            restores = 0
            while True:
                argv = build_omp_argv(
                    model, task, binary=binary, soul=soul,
                    tools_allowlist=spec.get("tools_allowlist"),
                    session_dir=session_dir, restore=restore_path,
                )
                completed = _run_omp_process(argv, directory, env, timeout_seconds=timeout)
                result = _result_from_completed(base, completed, started, mcp_path)
                transcript = _session_jsonl(session_dir)
                if (
                    result["success"]
                    or restores >= _MAX_RESTORES
                    or transcript is None
                    or not recoverable_lane_failure(result.get("error"), completed.returncode, True)
                ):
                    return result
                restores += 1
                restore_path = transcript
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**base, "error": str(exc), "duration_seconds": time.monotonic() - started, "quota": False}
