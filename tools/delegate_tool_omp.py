"""Synchronous OMP subprocess backend for configured delegation lanes."""

from __future__ import annotations

import json
import os
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
