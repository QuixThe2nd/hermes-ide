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

Goal-condition spill
--------------------
Claude Code caps ``/goal`` conditions at ``GOAL_CONDITION_MAX_CHARS``
(4000) and refuses to start a run whose condition is longer — and in ``-p``
mode the ENTIRE remainder after ``/goal`` is the condition, not just the
first line. Callers stay free to pass one long ``task`` string: an
over-limit ``/goal`` task is rewritten before spawn, writing the original
task to ``<workdir>/.hermes-claude-goal-brief.md`` and replacing the ``-p``
argument with a short ``/goal`` condition (first line plus a pointer to
that file). Non-goal tasks are never rewritten.

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

Completion signals
------------------
A ``"type": "result"`` event with ``subtype == "success"`` and
``is_error`` false is treated as success only when it carries a non-empty
final report; a "successful" run with an empty report is surfaced as a
failure so the caller can retry instead of acting on nothing. Lines in the
log containing known degraded-run markers (currently
``unrecognized_model`` — the provider rejected the pinned model and the
CLI silently fell back) are collected into the payload's ``warnings``
list, so a technically-OK exit that burned the run on the wrong model is
visible to the caller.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from tools.agent_cli_runner import run_agent_cli
from tools.claude_viewer_url import watch_url
from tools.registry import registry
from tools.tool_status import CLAUDE_AGENT_VIEWER_STATUS_PREFIX, emit_tool_status
from utils import is_truthy_value

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 0  # 0 = no wall-clock limit
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600
# 0 = stall watchdog off. Long quiet stretches (model thinking, slow tool
# calls inside the child) are the normal case for coding delegations, so a
# silent run is never killed; only an explicit positive timeout_seconds
# bounds the run.
STALL_WATCHDOG_SECONDS = 0

DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"
DEFAULT_PERMISSION_MODE = "acceptEdits"
_ALLOWED_PERMISSION_MODES = ("acceptEdits", "plan")

# Claude Code refuses /goal conditions longer than this many characters, so
# an over-limit task is spilled to a brief file in workdir before spawn.
GOAL_CONDITION_MAX_CHARS = 4000
GOAL_BRIEF_FILENAME = ".hermes-claude-goal-brief.md"
_GOAL_BRIEF_SUFFIX = f" Full brief (must follow): {GOAL_BRIEF_FILENAME}"
_GOAL_BRIEF_FALLBACK_CONDITION = "the task described in the brief file is complete"

# The child CLI can still refuse a goal after spawn: the run exits 0 with
# subtype=success, 0 turns, and the rejection text (observed live: "Goal
# condition is limited to 4000 characters (got 7421)") as the result field.
# A success-shaped final report matching this phrasing is reclassified as a
# failure in delegate_claude_agent.
_GOAL_REFUSAL_RE = re.compile(
    r"^goal (condition|set)[:\s]|goal condition is limited to|^no goal set",
    re.IGNORECASE,
)


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


# Markers that mean the run degraded silently even when the exit code and
# result event look fine. ``unrecognized_model``: the wrapper pinned a
# model the provider does not know, and the CLI fell back to another model
# while still exiting 0 — the run then burns turns on the wrong model.
_LOG_WARNING_MARKERS = ("unrecognized_model",)
_MAX_LOG_WARNINGS = 5
_MAX_WARNING_CHARS = 300


def extract_log_warnings(log_text: str) -> List[str]:
    """Collect degraded-run warning lines from a Claude Code log.

    Scans every line (JSON event or plain text) for
    ``_LOG_WARNING_MARKERS`` and returns one deduplicated, length-capped
    entry per distinct matching line, oldest first, capped at
    ``_MAX_LOG_WARNINGS``. Returns an empty list for clean logs.
    """
    warnings: List[str] = []
    seen = set()
    for raw_line in (log_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not any(marker in line for marker in _LOG_WARNING_MARKERS):
            continue
        if line in seen:
            continue
        seen.add(line)
        if len(line) > _MAX_WARNING_CHARS:
            line = line[:_MAX_WARNING_CHARS] + "..."
        warnings.append(line)
        if len(warnings) >= _MAX_LOG_WARNINGS:
            break
    return warnings


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

# Live-progress viewer that tails <HERMES_HOME>/claude-runs/<stem>.jsonl.
# The Discord adapter renders a spawned-run notice as a branded embed only
# when the URL passes is_allowed_watch_url (tools/claude_viewer_url.py) —
# scheme http/https, path "/" only, fragment fullmatching
# ``[0-9]{8}-[0-9]{6}-[0-9]+``, and a private-network host. The delegate
# scheme guarantees the stem; the host comes from that same module so the
# emitted URL always points at *this* machine (config or auto-detected LAN/
# Tailscale address) rather than a hardcoded deployment.


def _claude_viewer_status_line(stem: str) -> str:
    """Return the mid-tool status line pointing at a run's live viewer page."""
    return f"{CLAUDE_AGENT_VIEWER_STATUS_PREFIX}{watch_url(stem)}"


def _emit_viewer_progress_notice(log_path: Path) -> None:
    """Announce a freshly spawned run's viewer page via ``emit_tool_status``.

    Emitted the moment the log path is known, so the user gets a
    watch-live link while the run is still going rather than only in the
    final tool result. Unbound dispatch is a no-op (the exact URL lands in
    the result payload anyway); the emit is still defensive — a broken
    callback context must never take the delegation down with it.
    """
    try:
        emit_tool_status(_claude_viewer_status_line(Path(log_path).stem))
    except Exception:
        logger.debug("claude viewer progress notice failed", exc_info=True)


def _clamp_timeout_seconds(timeout_seconds: int) -> int:
    try:
        value = int(timeout_seconds)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return 0  # unbounded: no wall-clock limit and no stall kill
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
    warnings: Optional[List[str]] = None,
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
        "warnings": list(warnings or []),
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
    on_spawn: Optional[Callable[[Path], None]] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
) -> Tuple[Optional[str], str, str, float, Optional[int]]:
    """Spawn the agent, stream stdout to a log file, enforce watchdogs.

    ``on_spawn`` is handed straight to ``run_agent_cli`` — invoked once with
    the resolved log path, right after spawn. ``on_proc`` likewise receives
    the ``Popen`` itself (the handle a background run's interrupt kills).

    Returns ``(error_code, log_path, log_text, duration_seconds, returncode)``.
    """
    return run_agent_cli(
        cmd,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        stall_watchdog_seconds=STALL_WATCHDOG_SECONDS,
        log_dir=log_dir,
        run_timestamp=run_timestamp,
        on_spawn=on_spawn,
        on_proc=on_proc,
    )


# ---------------------------------------------------------------------------
# /goal condition spill
# ---------------------------------------------------------------------------

def _extract_goal_condition(prompt: str) -> Optional[str]:
    """Return the ``/goal`` condition inside ``prompt``, or None.

    Detection is case-insensitive on the ``/goal`` token after leading
    whitespace, and only when the token ends there: the next character must
    be whitespace or nothing at all, so ``/goalkeeper`` or ``/goal-foo`` is
    an ordinary task, not a goal. Any Unicode whitespace counts as the
    separator, not just ASCII spaces and newlines. The condition is
    everything after that one separator — in ``-p`` mode the whole remainder
    is the condition, not just the first line; a bare ``/goal`` yields an
    empty condition.
    """
    body = prompt.lstrip()
    if not body.lower().startswith("/goal"):
        return None
    remainder = body[len("/goal"):]
    if not remainder:
        return ""
    if not remainder[0].isspace():
        return None
    return remainder[1:]


def _shorten_goal_condition(condition: str) -> str:
    """Build a condition within ``GOAL_CONDITION_MAX_CHARS`` pointing at the brief.

    Keeps the first line of the original condition when it fits alongside
    the follow-file suffix; otherwise falls back to a generic completion
    phrase (fallback + suffix is always far under the limit).
    """
    first_line = condition.split("\n", 1)[0].strip()
    if len(first_line) + len(_GOAL_BRIEF_SUFFIX) > GOAL_CONDITION_MAX_CHARS:
        first_line = _GOAL_BRIEF_FALLBACK_CONDITION
    return first_line + _GOAL_BRIEF_SUFFIX


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
    background: bool = False,
    session_id: str | None = None,
    tool_call_id: str | None = None,
) -> str:
    """Delegate one dev task to the Claude Code CLI.

    Uniform delegation lifecycle: the mode comes ONLY from the explicit
    ``background`` argument. Omitted/false blocks until the CLI exits and
    returns the run's result inline. ``background=true`` first checks the
    session can receive a late completion — failing clearly (starting
    nothing) when it cannot — and otherwise returns the shared acceptance
    envelope; the terminal result later re-enters the conversation through
    the completion rail.
    """
    del task_id, session_id, tool_call_id  # reserved for correlation; unused

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

    # Uniform lifecycle gate — BEFORE any side effect (the /goal brief
    # write, the log directory, the subprocess). An unsupported channel is
    # a hard error, never a silent foreground run the model did not ask
    # for, and never work that starts and cannot be delivered.
    if background:
        from tools.async_delegation import background_delivery_supported

        bg_ok, bg_reason = background_delivery_supported()
        if not bg_ok:
            logger.info(
                "delegate_claude_agent: background=true rejected before "
                "spawn: %s",
                bg_reason,
            )
            return _make_result(success=False, error=bg_reason)

    # Pre-flight /goal rewrite: the child CLI caps /goal conditions at
    # GOAL_CONDITION_MAX_CHARS and never starts an over-limit run, so spill
    # the full task to a brief file in workdir and hand the child a short
    # condition pointing at it. Runs before binary resolution/spawn so a
    # failed write never leaves a half-started child.
    prompt = str(task).strip()
    goal_brief_path: Optional[str] = None
    goal_condition = _extract_goal_condition(prompt)
    if goal_condition is not None and len(goal_condition) > GOAL_CONDITION_MAX_CHARS:
        brief_path = workdir_path / GOAL_BRIEF_FILENAME
        try:
            brief_path.write_text(str(task), encoding="utf-8")
            os.chmod(brief_path, 0o644)
        except OSError as exc:
            return _make_result(
                success=False,
                error=f"failed to write /goal brief file {brief_path}: {exc}",
            )
        prompt = "/goal " + _shorten_goal_condition(goal_condition)
        goal_brief_path = str(brief_path)

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
            prompt,
        ]
    )

    if background:
        return _dispatch_claude_background(
            cmd,
            workdir=str(workdir_path),
            clamped_timeout=clamped_timeout,
            log_dir=log_dir,
            run_timestamp=run_timestamp,
            goal_brief_path=goal_brief_path,
            goal=prompt,
            model_name=model_name,
        )

    return _run_claude_agent_sync(
        cmd,
        workdir=str(workdir_path),
        clamped_timeout=clamped_timeout,
        log_dir=log_dir,
        run_timestamp=run_timestamp,
        goal_brief_path=goal_brief_path,
    )


def _run_claude_agent_sync(
    cmd: List[str],
    *,
    workdir: str,
    clamped_timeout: int,
    log_dir: Path,
    run_timestamp: str,
    goal_brief_path: Optional[str],
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
) -> str:
    """Spawn the CLI, wait for it to exit, and return its result inline."""
    try:
        watchdog_error, log_path, log_text, duration, returncode = _run_and_stream(
            cmd,
            workdir=workdir,
            timeout_seconds=clamped_timeout,
            log_dir=log_dir,
            run_timestamp=run_timestamp,
            on_spawn=_emit_viewer_progress_notice,
            on_proc=on_proc,
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
        "warnings": extract_log_warnings(log_text),
        "goal_brief_path": goal_brief_path,
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
    error = None if success else (
        f"Claude Code result subtype={subtype!r} is_error={is_error!r}"
    )

    if success and not base_fields["final_report"].strip():
        # A "successful" run with an empty final report hands the caller no
        # usable outcome (observed with GLM-pinned delegations that stalled
        # or fell back after an unrecognized_model warning). Surface it as
        # a failure so the caller can retry or investigate instead of
        # treating silence as a completed delegation.
        success = False
        error = "Claude Code reported success but returned an empty final report"

    if success and _GOAL_REFUSAL_RE.search(base_fields["final_report"]):
        # A goal-mode refusal surfaces as subtype=success with the rejection
        # text in `result` and 0 turns (observed live with "Goal condition
        # is limited to 4000 characters (got 7421)") — the run never
        # started, so classifying it as success hands the caller a
        # completed-looking no-op. Reclassify as failure so the dispatch
        # can be retried instead of trusted.
        success = False
        error = (
            "Claude Code reported success but the final report is a goal "
            f"refusal: {base_fields['final_report'][:200]}"
        )

    return _make_result(
        success=success,
        error=error,
        **base_fields,
    )


def _dispatch_claude_background(
    cmd: List[str],
    *,
    workdir: str,
    clamped_timeout: int,
    log_dir: Path,
    run_timestamp: str,
    goal_brief_path: Optional[str],
    goal: str,
    model_name: str,
) -> str:
    """Return the background acceptance envelope, or a clear rejection.

    The subprocess is NOT spawned here: the runner spawns it on the async
    registry's daemon worker, so a capacity rejection leaves nothing
    running and nothing to tear down.
    """
    from tools.async_delegation import (
        RESULT_KIND_CLI_AGENT,
        dispatch_background_delegation,
    )

    live: Dict[str, subprocess.Popen] = {}

    def _runner() -> Dict[str, Any]:
        # Runs on the async registry's daemon thread. On /stop or gateway
        # shutdown, `_signal_runner_interrupt` sets THIS thread's interrupt
        # bit before `interrupt_fn` fires, so `run_agent_cli`'s cooperative
        # poll unwinds and kills the process group with no per-tool code.
        def _on_proc(proc: subprocess.Popen) -> None:
            live["proc"] = proc

        out = _run_claude_agent_sync(
            cmd,
            workdir=workdir,
            clamped_timeout=clamped_timeout,
            log_dir=log_dir,
            run_timestamp=run_timestamp,
            goal_brief_path=goal_brief_path,
            on_proc=_on_proc,
        )
        payload = json.loads(out)
        status = "completed" if payload.get("success") else "error"
        if payload.get("error") in ("interrupted", "timeout"):
            status = payload["error"]
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                # This thread's interrupt bit is only ever set by
                # `_signal_runner_interrupt`, so a run that ended because of
                # a stop request reports `interrupted` even when the direct
                # process kill won the race against the cooperative poll.
                status = "interrupted"
        except Exception:
            pass
        # Same vocabulary `_run_single_child` produces, so the shared
        # completion block renders a claude run exactly like any other
        # delegation; the run artifacts ride along additively.
        return {
            "status": status,
            "summary": payload.get("final_report") or "",
            "error": payload.get("error"),
            "duration_seconds": payload.get("duration_seconds"),
            "model": model_name or None,
            "log_path": payload.get("log_path"),
            "child_session_id": payload.get("session_id"),
            "cost_usd": payload.get("cost_usd"),
            "models_used": payload.get("models_used") or [],
            "warnings": payload.get("warnings") or [],
            "exit_reason": status if status != "completed" else None,
        }

    def _interrupt() -> None:
        # Best-effort direct kill on top of the cooperative thread bit: the
        # worker may be descheduled inside the spawn/reader handshake.
        proc = live.get("proc")
        if proc is None:
            return
        try:
            from tools.agent_cli_runner import _terminate_process

            try:
                pgid = os.getpgid(proc.pid)
            except (OSError, ProcessLookupError):
                pgid = None
            _terminate_process(proc, pgid)
        except Exception:
            logger.debug("claude background interrupt failed", exc_info=True)

    try:
        from tools.delegate_tool import _get_max_async_children
        from tools.approval import get_current_session_key
    except Exception:  # pragma: no cover — both always importable in-tree
        _get_max_async_children = None
        get_current_session_key = None

    session_key = ""
    parent_session_id: Optional[str] = None
    try:
        if get_current_session_key is not None:
            session_key = get_current_session_key(default="")
        from gateway.session_context import get_session_env

        # The spawning session's durable id routes the completion back to
        # the right conversation when the platform session key rotates.
        parent_session_id = get_session_env("HERMES_SESSION_ID", "") or None
    except Exception:
        parent_session_id = None

    max_children = _get_max_async_children() if _get_max_async_children else 3

    dispatch = dispatch_background_delegation(
        tool="delegate_claude_agent",
        result_kind=RESULT_KIND_CLI_AGENT,
        goal=goal,
        goals=[goal],
        runner=_runner,
        interrupt_fn=_interrupt,
        session_key=session_key,
        parent_session_id=parent_session_id,
        max_async_children=max_children,
        model=model_name or None,
        note=(
            "The Claude Code run is underway in the background. You and the "
            "user can keep working; its result re-enters the conversation as "
            "a new message when the run finishes. Do not wait or poll — just "
            "continue."
        ),
        control_hint=(
            "The run streams to a JSONL log under the Hermes home directory; "
            "its path is delivered with the result."
        ),
    )

    if dispatch.get("status") != "dispatched":
        # Capacity unavailable — the runner never started, so the subprocess
        # was never spawned. Fail clearly rather than silently running inline.
        logger.info(
            "delegate_claude_agent: background dispatch rejected (%s); "
            "no work started.",
            dispatch.get("error", "rejected"),
        )
        return _make_result(
            success=False,
            error=(
                "background=true was rejected and NO WORK WAS STARTED: "
                + str(
                    dispatch.get(
                        "error", "the background delegation pool is at capacity"
                    )
                )
                + " Omit `background` (or pass background=false) to run the "
                "task in the foreground this turn instead."
            ),
        )

    return json.dumps(dispatch, ensure_ascii=False)


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
        "Lane rule: use for MEDIUM TO LARGE jobs; small to medium goes to "
        "delegate_cursor_agent. Default the task to /goal <observable done "
        "condition> — goal mode auto-continues until done; a one-shot prompt "
        "is the exception. "
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
                    "Start it with '/goal <condition>' to run goal mode "
                    "headless — the default: the run auto-continues across "
                    "turns until a model judge confirms the condition is "
                    "met or impossible. A task not starting with /goal "
                    "should still be phrased as a verifiable done condition. "
                    "The /goal condition is capped at 4000 characters; "
                    "longer tasks are auto-spilled to "
                    "<workdir>/.hermes-claude-goal-brief.md with a short "
                    "condition pointing at it."
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
                    f"0 (default) means no limit at all — a quiet run is never "
                    f"killed for silence. Positive values clamp to "
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
            "background": {
                "type": "boolean",
                "description": (
                    "Blocking by default: omitted or false runs the CLI to "
                    "completion and returns its final report inline. Pass "
                    "true to return a background handle immediately and keep "
                    "working; the terminal result re-enters the conversation "
                    "as a new message when the run finishes. The mode depends "
                    "only on this argument."
                ),
                "default": False,
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
        background=is_truthy_value(args.get("background"), default=False),
        session_id=kw.get("session_id"),
        tool_call_id=kw.get("tool_call_id"),
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
