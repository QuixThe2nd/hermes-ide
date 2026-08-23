#!/usr/bin/env python3
"""Delegate dev tasks to the Claude Code CLI as a subprocess (GLM or Kimi).

The requested ``model`` picks the wrapper lane: model names containing
"kimi" (case-insensitive) run via the ``claude-kimi`` wrapper (Kimi K3),
used when Kimi is the target writer; everything else — including no model
at all — runs via the ``claude-glm`` wrapper (GLM, z.ai coding plan), the
default lane for general long-running tasks.

Gating
------
The tool registers only when the GLM wrapper is resolvable — either via
the ``CLAUDE_GLM_BIN`` env override, as an executable at
``~/.local/bin/claude-glm``, or as ``claude-glm``/``claude`` on PATH.
Kimi runs additionally need the ``claude-kimi`` wrapper (``CLAUDE_KIMI_BIN``
override, ``~/.local/bin/claude-kimi``, or ``claude-kimi`` on PATH); a kimi
request never falls back to the GLM wrapper or bare ``claude``.

Credentials
-----------
The wrappers inject their provider's coding-plan credentials at runtime
from ``<HERMES_HOME>/.env`` themselves (they ``execve`` the real Claude
binary with ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_BASE_URL`` set). This tool
therefore NEVER places credentials in argv or in env additions — it only
guarantees ``HOME`` and a minimal ``PATH`` so the wrapper survives sparse
environments (it runs with ``set -u`` and dies on an unbound ``$HOME``).
``--dangerously-skip-permissions`` is deliberately NOT passed: it is refused
when the process runs as root.

Log format
----------
Stdout is streamed to ``<HERMES_HOME>/claude-runs/<timestamp>-<pid>.jsonl``.
With ``--output-format stream-json --verbose`` the CLI emits one JSON event
per line *as the run happens* — session init, tool calls, assistant turns,
retries — so the log file doubles as a live progress feed for tailers.
The final line is a ``"type": "result"`` event; the handler scans for the
last such line and extracts session metadata, cost, model usage, and
permission denials from it (same contract as the old batch ``json``
format).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from tools.agent_cli_runner import run_agent_cli
from tools.registry import registry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 0  # 0 = no wall-clock limit; stall watchdog still applies
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600
STALL_WATCHDOG_SECONDS = 600

DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"
DEFAULT_PERMISSION_MODE = "acceptEdits"
_ALLOWED_PERMISSION_MODES = ("acceptEdits", "plan")


# ---------------------------------------------------------------------------
# Model-family classification + binary resolution/gating
# ---------------------------------------------------------------------------

MODEL_FAMILY_GLM = "glm"
MODEL_FAMILY_KIMI = "kimi"


def classify_model_family(model: str | None) -> str:
    """Map a requested model name to a wrapper family.

    Any model string containing ``kimi`` (case-insensitive) belongs to the
    kimi family; everything else — including empty/None — defaults to the
    glm family.
    """
    if "kimi" in str(model or "").lower():
        return MODEL_FAMILY_KIMI
    return MODEL_FAMILY_GLM


def _local_bin_claude_glm_path() -> Path:
    return Path.home() / ".local" / "bin" / "claude-glm"


def _local_bin_claude_kimi_path() -> Path:
    return Path.home() / ".local" / "bin" / "claude-kimi"


def _resolve_glm_binary() -> Optional[str]:
    try:
        override = os.environ.get("CLAUDE_GLM_BIN")
        if override:
            override_path = Path(override).expanduser()
            if override_path.is_file() and os.access(override_path, os.X_OK):
                return str(override_path)

        local = _local_bin_claude_glm_path()
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)

        found = shutil.which("claude-glm")
        if found:
            return found

        found = shutil.which("claude")
        if found:
            return found
    except Exception:
        pass
    return None


def _resolve_kimi_binary() -> Optional[str]:
    try:
        override = os.environ.get("CLAUDE_KIMI_BIN")
        if override:
            override_path = Path(override).expanduser()
            if override_path.is_file() and os.access(override_path, os.X_OK):
                return str(override_path)

        local = _local_bin_claude_kimi_path()
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)

        found = shutil.which("claude-kimi")
        if found:
            return found
    except Exception:
        pass
    # Deliberately NO fallback to the GLM wrapper or bare claude: silently
    # routing a kimi request to another provider would run the wrong model.
    return None


def resolve_claude_binary(model: str | None = None) -> Optional[str]:
    """Return the Claude Code wrapper path for ``model``'s family, or None.

    The ``model`` name picks the lane via ``classify_model_family``; with no
    model (or any non-kimi model) this resolves the GLM wrapper exactly as
    before.

    GLM search order:
    1. ``CLAUDE_GLM_BIN`` env override (must be an executable file).
    2. ``~/.local/bin/claude-glm``.
    3. ``claude-glm`` on PATH.
    4. bare ``claude`` on PATH.

    Kimi search order (returns None when nothing resolves — never falls
    back to the GLM wrapper or bare ``claude``):
    1. ``CLAUDE_KIMI_BIN`` env override (must be an executable file).
    2. ``~/.local/bin/claude-kimi``.
    3. ``claude-kimi`` on PATH.
    """
    if classify_model_family(model) == MODEL_FAMILY_KIMI:
        return _resolve_kimi_binary()
    return _resolve_glm_binary()


def check_claude_agent_requirements() -> bool:
    """Return True when the default (GLM) Claude Code wrapper binary is available."""
    try:
        return resolve_claude_binary() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def parse_claude_agent_log(log_text: str) -> Dict[str, Any]:
    """Parse a Claude Code json log for the final ``type=result`` event.

    Scans every line for valid JSON; the last line whose parsed object has
    ``"type": "result"`` wins. Returns an empty dict when no result event is
    found (missing/malformed output).
    """
    result_event: Optional[Dict[str, Any]] = None
    for raw_line in log_text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result_event = event

    if not result_event:
        return {}

    model_usage = result_event.get("modelUsage")
    models_used: List[str] = []
    if isinstance(model_usage, dict):
        models_used = sorted(str(key) for key in model_usage.keys())

    permission_denials = result_event.get("permission_denials")
    if permission_denials is None:
        permission_denials = []

    return {
        "subtype": result_event.get("subtype"),
        "is_error": result_event.get("is_error"),
        "result": result_event.get("result"),
        "session_id": result_event.get("session_id"),
        "num_turns": result_event.get("num_turns"),
        "duration_ms": result_event.get("duration_ms"),
        "total_cost_usd": result_event.get("total_cost_usd"),
        "models_used": models_used,
        "permission_denials": permission_denials,
    }


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _clamp_timeout_seconds(timeout_seconds: int) -> int:
    try:
        value = int(timeout_seconds)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return 0  # unbounded; stall watchdog remains the dead-man switch
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


def _make_result(
    *,
    success: bool,
    final_report: str = "",
    error: Optional[str] = None,
    session_id: Optional[str] = None,
    duration_seconds: float = 0.0,
    num_turns: Optional[int] = None,
    cost_usd: Optional[float] = None,
    models_used: Optional[List[str]] = None,
    permission_denials: Optional[List[Any]] = None,
    log_path: Optional[str] = None,
    **extra: Any,
) -> str:
    payload: Dict[str, Any] = {
        "success": success,
        "error": error,
        "final_report": final_report,
        "session_id": session_id,
        "duration_seconds": duration_seconds,
        "num_turns": num_turns,
        "cost_usd": cost_usd,
        "models_used": models_used or [],
        "permission_denials": permission_denials,
        "log_path": log_path,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _run_and_stream(
    cmd: List[str],
    *,
    workdir: str,
    timeout_seconds: int,
    log_dir: Path,
    run_timestamp: str,
) -> Tuple[Optional[str], str, str, float, Optional[int]]:
    """Spawn the agent, stream stdout to a log file, enforce watchdogs.

    Returns ``(error_code, log_path, log_text, duration_seconds, returncode)``.
    """
    return run_agent_cli(
        cmd,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        stall_watchdog_seconds=STALL_WATCHDOG_SECONDS,
        log_dir=log_dir,
        run_timestamp=run_timestamp,
    )


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

def delegate_claude_agent(
    task: str,
    workdir: str,
    model: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    task_id: str | None = None,
) -> str:
    del task_id  # reserved for future correlation; not used yet

    if not task or not str(task).strip():
        return _make_result(
            success=False,
            error="task is required for delegate_claude_agent",
        )

    workdir_path = Path(workdir)
    if not workdir_path.is_absolute():
        return _make_result(
            success=False,
            error="workdir must be an absolute path",
        )
    if not workdir_path.is_dir():
        return _make_result(
            success=False,
            error=f"workdir does not exist or is not a directory: {workdir}",
        )

    mode = str(permission_mode or "").strip()
    if mode not in _ALLOWED_PERMISSION_MODES:
        return _make_result(
            success=False,
            error=(
                "permission_mode must be one of "
                f"{list(_ALLOWED_PERMISSION_MODES)}, got: {permission_mode!r}"
            ),
        )

    model_name = str(model or "").strip()
    binary = resolve_claude_binary(model_name)
    if not binary:
        if classify_model_family(model_name) == MODEL_FAMILY_KIMI:
            wrapper_error = (
                "Claude Code (Kimi) wrapper binary not found. Install the "
                "`claude-kimi` wrapper at ~/.local/bin/claude-kimi (or set "
                "CLAUDE_KIMI_BIN), or place `claude-kimi` on PATH."
            )
        else:
            wrapper_error = (
                "Claude Code (GLM) wrapper binary not found. Install the "
                "`claude-glm` wrapper at ~/.local/bin/claude-glm (or set "
                "CLAUDE_GLM_BIN), or place `claude-glm`/`claude` on PATH."
            )
        return _make_result(
            success=False,
            error=wrapper_error,
        )

    clamped_timeout = _clamp_timeout_seconds(timeout_seconds)
    tools_arg = str(allowed_tools or "").strip() or DEFAULT_ALLOWED_TOOLS

    log_dir = get_hermes_home() / "claude-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # NOTE: --dangerously-skip-permissions is intentionally omitted — it is
    # refused when running as root. The wrapper injects credentials itself.
    cmd = [
        binary,
        "-p",
    ]
    if model_name:
        cmd.extend(["--model", model_name])
    cmd.extend(
        [
            "--permission-mode",
            mode,
            "--allowedTools",
            tools_arg,
            "--output-format",
            "stream-json",
            "--verbose",
            str(task).strip(),
        ]
    )

    try:
        watchdog_error, log_path, log_text, duration, returncode = _run_and_stream(
            cmd,
            workdir=str(workdir_path),
            timeout_seconds=clamped_timeout,
            log_dir=log_dir,
            run_timestamp=run_timestamp,
        )
    except Exception as exc:
        logger.error("delegate_claude_agent spawn failed: %s", exc, exc_info=True)
        return _make_result(
            success=False,
            error=str(exc),
            duration_seconds=0.0,
        )

    parsed = parse_claude_agent_log(log_text)
    base_fields = {
        "final_report": _coerce_str(parsed.get("result")) or "",
        "session_id": parsed.get("session_id"),
        "duration_seconds": round(duration, 3),
        "num_turns": parsed.get("num_turns"),
        "cost_usd": parsed.get("total_cost_usd"),
        "models_used": parsed.get("models_used") or [],
        "permission_denials": parsed.get("permission_denials") or [],
        "log_path": log_path,
    }

    if watchdog_error:
        return _make_result(
            success=False,
            error=watchdog_error,
            **base_fields,
        )

    if returncode != 0:
        tail = log_text.strip()[-2000:] if log_text.strip() else ""
        return _make_result(
            success=False,
            error=f"Claude Code exited with code {returncode}" + (f": {tail}" if tail else ""),
            **base_fields,
        )

    if not parsed:
        return _make_result(
            success=False,
            error="no result event found in Claude Code output",
            **base_fields,
        )

    is_error = parsed.get("is_error")
    subtype = parsed.get("subtype")
    success = is_error is False and subtype == "success"

    return _make_result(
        success=success,
        error=None if success else (
            f"Claude Code result subtype={subtype!r} is_error={is_error!r}"
        ),
        **base_fields,
    )


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


DELEGATE_CLAUDE_AGENT_SCHEMA = {
    "name": "delegate_claude_agent",
    "description": (
        "Delegate a software development task to the Claude Code CLI running "
        "against a coding-model wrapper: GLM (z.ai coding plan) via the local "
        "claude-glm wrapper for general long-running tasks, or Kimi K3 via "
        "the claude-kimi wrapper when Kimi is the target writer (request a "
        "model containing 'kimi'). The CLI performs code edits, terminal "
        "commands, and multi-step dev work inside the specified repository "
        "directory. Stdout is captured as JSON in a log under the Hermes "
        "home directory. Available only when the relevant wrapper binary is "
        "installed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The coding task brief for Claude Code to perform. "
                    "May start with '/goal <condition>' to run goal mode "
                    "headless: the run auto-continues across turns until a "
                    "model judge confirms the condition is met or impossible."
                ),
            },
            "workdir": {
                "type": "string",
                "description": "Absolute path to the target git repo/workspace.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Model to use for the run. Models containing 'kimi' "
                    "route to the claude-kimi wrapper; otherwise the run "
                    "goes via the claude-glm wrapper. Omit to use the "
                    "claude-glm wrapper's pinned model."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    f"Maximum wall-clock seconds before the run is terminated. "
                    f"0 (default) means no wall-clock limit; the stall watchdog "
                    f"still terminates runs with no output for "
                    f"{STALL_WATCHDOG_SECONDS}s. Positive values clamp to "
                    f"{MIN_TIMEOUT_SECONDS}–{MAX_TIMEOUT_SECONDS}."
                ),
                "default": DEFAULT_TIMEOUT_SECONDS,
            },
            "allowed_tools": {
                "type": "string",
                "description": (
                    "Comma-separated list of tools Claude Code may use."
                ),
                "default": DEFAULT_ALLOWED_TOOLS,
            },
            "permission_mode": {
                "type": "string",
                "description": (
                    "Claude Code permission mode. 'acceptEdits' auto-approves "
                    "file edits; 'plan' only plans without writing."
                ),
                "default": DEFAULT_PERMISSION_MODE,
                "enum": list(_ALLOWED_PERMISSION_MODES),
            },
        },
        "required": ["task", "workdir"],
    },
}


def _handle_delegate_claude_agent(args, **kw):
    return delegate_claude_agent(
        task=args.get("task", ""),
        workdir=args.get("workdir", ""),
        model=args.get("model"),
        timeout_seconds=args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        allowed_tools=args.get("allowed_tools", DEFAULT_ALLOWED_TOOLS),
        permission_mode=args.get("permission_mode", DEFAULT_PERMISSION_MODE),
        task_id=kw.get("task_id"),
    )


registry.register(
    name="delegate_claude_agent",
    toolset="delegation",
    schema=DELEGATE_CLAUDE_AGENT_SCHEMA,
    handler=_handle_delegate_claude_agent,
    check_fn=check_claude_agent_requirements,
    emoji="🤖",
    max_result_size_chars=100_000,
)
