#!/usr/bin/env python3
"""Delegate dev tasks to a Cursor My Machines Cloud Agent.

Gating
------
The tool registers only when the Cursor Agent CLI binary is available on
PATH (``agent``) or as an executable at ``~/.local/bin/agent``. The binary
is used to start a short-lived My Machines worker for ``workdir``.

Credentials
-----------
``CURSOR_API_KEY`` is loaded from
``Path.home() / ".hermes/secrets/cursor-cloud.env"`` (on this host that is
``/root/.hermes/secrets/cursor-cloud.env``). Tests inject an alternate path
by monkeypatching ``CURSOR_CLOUD_ENV_PATH``. The parent Hermes HTTP client
holds the key. The worker process and its descendants never receive
``CURSOR_API_KEY`` — they authenticate with the existing Cursor machine login
already used by local ``agent``. The key is never placed in argv,
log files, worker env, or the tool result. A missing or empty key fails
clearly — there is no silent fallback to the local CLI or to
``os.environ``.

Backend
-------
The handler resolves ``workdir``'s ``origin`` remote to a live-verified
HTTPS GitHub URL (local / ``file://`` / non-GitHub hosts are rejected),
preflights ``agent status --format json`` in that sanitized env (a missing
login returns ``Cursor My Machines worker is not authenticated; run agent
login`` and never suggests putting the API key in worker env/argv), starts
a unique short-lived
``agent worker --name … --worker-dir … --idle-release-timeout 0 start``
for that checkout, POSTs ``/v1/agents`` with ``env.type=machine``, and
polls the run until a terminal Cloud Agent status. ``force`` does not
enable pushes or PRs. ``startingRef`` is sent only when that branch exists
on the origin remote.

Authority and no-push
---------------------
Workdir authority matches the existing local executor: a same-user coding
agent is not a hard sandbox and can reach this user's Git/SSH credentials.
API ``autoCreatePR: false``, a prompt-level no-push instruction, and
process-local Git overrides are defense-in-depth only. Operators must use
isolated scratch clones or worktrees for protected repos. The caller's
``.git/config`` is not rewritten.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from tools.agent_cli_runner import _terminate_process
from tools.cursor_run_receipts import (
    ReceiptValidationError,
    MAX_RECOVERY_ATTEMPTS,
    _assert_receipt_permissions,
    _assert_regular_receipt_file,
    binding_run_lock,
    create_receipt,
    cursor_runs_dir,
    deterministic_client_agent_id,
    finalize_receipt,
    find_receipt_for_binding,
    hash_prompt,
    is_terminal_receipt,
    persist_cloud_ids,
    read_receipt,
    receipt_matches_binding,
    request_fingerprint,
    update_receipt,
    validate_receipt_binding,
)
from tools.environments.local import build_subprocess_env
from tools.registry import registry
from tools.tool_status import emit_tool_status
from agent.tool_dispatch_helpers import make_tool_result_message

from utils import is_truthy_value

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 0  # 0 = no wall-clock limit; stall watchdog still applies
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 1800
STALL_WATCHDOG_SECONDS = 600

CURSOR_API_BASE = "https://api.cursor.com"
CURSOR_CLOUD_ENV_PATH = Path.home() / ".hermes" / "secrets" / "cursor-cloud.env"
SUPPORTED_ORIGIN_HOSTS = frozenset({"github.com"})
NO_PUSH_PROMPT_PREFIX = (
    "Do not git push, create a pull request, or request reviewers. "
    "Keep all changes local to this machine checkout.\n\n"
)
DEFAULT_ORCHESTRATION_PROMPT = (
    "You are the parent orchestrator agent. Do not edit files directly yourself. "
    "Delegate all implementation work to a Task subagent pinned to model composer-2.5 "
    "(the standard variant; never use composer-2.5-fast). "
    "After implementation completes, delegate exactly one read-only review Task pinned to model "
    "cursor-grok-4.5-high. "
    "If the review reports blocking findings, fix them exactly once via the implementer subagent "
    "(one remediation pass only; do not loop). "
    "In your final report, include the actual models used by each delegation.\n\n"
)
POST_TIMEOUT_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 2.0
WORKER_LOG_MAX_BYTES = 256 * 1024
WORKER_READY_ATTEMPTS = 5
WORKER_READY_DELAY_SECONDS = 0.5
WORKER_IDLE_RELEASE_TIMEOUT = "0"
WORKER_STATUS_TIMEOUT_SECONDS = 15.0
WORKER_AUTH_ERROR = (
    "Cursor My Machines worker is not authenticated; run agent login"
)
NO_PUSH_PUSHURL = "disabled://hermes-no-push"
TERMINAL_RUN_STATUSES = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})
_NO_PUSH_ENV_KEYS = frozenset({
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN",
    "GL_TOKEN",
    "GITLAB_PRIVATE_TOKEN",
    "BITBUCKET_TOKEN",
    "BITBUCKET_USERNAME",
    "BITBUCKET_APP_PASSWORD",
    "AZURE_DEVOPS_EXT_PAT",
    "SYSTEM_ACCESSTOKEN",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_CREDENTIAL_HELPER",
    "GIT_CONFIG_PARAMETERS",
})


# ---------------------------------------------------------------------------
# Binary resolution + gating
# ---------------------------------------------------------------------------

def _local_bin_agent_path() -> Path:
    return Path.home() / ".local" / "bin" / "agent"


def resolve_cursor_agent_binary() -> Optional[str]:
    """Return the Cursor Agent CLI path, or None if not found."""
    try:
        found = shutil.which("agent")
        if found:
            return found
        local = _local_bin_agent_path()
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    except Exception:
        pass
    return None


def check_cursor_agent_requirements() -> bool:
    """Return True when the Cursor Agent CLI binary is available."""
    try:
        return resolve_cursor_agent_binary() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stream-json parsing
# ---------------------------------------------------------------------------

def _find_task_tool_calls(obj: Any) -> List[Dict[str, Any]]:
    """Recursively collect dicts that look like taskToolCall payloads."""
    found: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        if "taskToolCall" in obj and isinstance(obj["taskToolCall"], dict):
            found.append(obj["taskToolCall"])
        for value in obj.values():
            found.extend(_find_task_tool_calls(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_task_tool_calls(item))
    return found


def _extract_assistant_text(event: Dict[str, Any]) -> str:
    """Extract plain text from an assistant stream-json event."""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    parts: List[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"text", "output_text"}:
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def _is_action_required_event(event: Dict[str, Any]) -> bool:
    """Return True when a parsed JSON event structurally indicates action required."""
    if event.get("error_type") == "ActionRequiredError":
        return True

    if event.get("type") != "error":
        return False

    for key in ("error_type", "name", "code"):
        if event.get(key) == "ActionRequiredError":
            return True

    err = event.get("error")
    if isinstance(err, dict):
        for key in ("type", "name", "code", "error_type"):
            if err.get(key) == "ActionRequiredError":
                return True
    elif isinstance(err, str) and err == "ActionRequiredError":
        return True

    return False


def _extract_action_required_detail(event: Dict[str, Any]) -> str:
    for key in ("message", "error", "detail", "description"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Action required"


def _is_action_required_plain_text(text: str) -> bool:
    stripped = text.strip()
    return stripped == "ActionRequiredError" or stripped.startswith("ActionRequiredError:")


def _extract_action_required_plain_detail(text: str) -> str:
    stripped = text.strip()
    if stripped == "ActionRequiredError":
        return ""
    if stripped.startswith("ActionRequiredError:"):
        return stripped[len("ActionRequiredError:") :].strip()
    return "Action required"


def _delegation_key(record: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    return (
        record.get("description"),
        record.get("subagent_type"),
        record.get("model"),
    )


def _canonicalize_dedupe_token(value: Any) -> Any:
    """Convert a value into a deterministic hashable token for dedupe keys."""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        if value != value:
            return ("float", "nan")
        if value == float("inf"):
            return ("float", "inf")
        if value == float("-inf"):
            return ("float", "-inf")
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, bytes):
        return ("bytes", value)
    try:
        if isinstance(value, dict):
            return (
                "dict",
                tuple(
                    (_canonicalize_dedupe_token(k), _canonicalize_dedupe_token(v))
                    for k, v in sorted(
                        value.items(),
                        key=lambda item: json.dumps(item[0], sort_keys=True, default=str),
                    )
                ),
            )
        if isinstance(value, (list, tuple)):
            return ("list", tuple(_canonicalize_dedupe_token(item) for item in value))
        if isinstance(value, set):
            return (
                "set",
                tuple(
                    sorted(
                        json.dumps(_canonicalize_dedupe_token(item), sort_keys=True, default=str)
                        for item in value
                    )
                ),
            )
        return ("json", json.dumps(value, sort_keys=True, default=str))
    except Exception:
        pass
    try:
        return ("json", json.dumps(value, sort_keys=True, default=str))
    except Exception:
        pass
    try:
        return ("repr", repr(value))
    except Exception:
        return ("type", type(value).__name__)


def _delegation_dedupe_key(
    event: Dict[str, Any],
    task_call: Dict[str, Any],
    record: Dict[str, Any],
) -> Tuple[Any, ...]:
    call_id = event.get("call_id")
    if call_id is not None:
        return ("call_id", _canonicalize_dedupe_token(call_id))

    for source in (event, task_call):
        if not isinstance(source, dict):
            continue
        for key in ("toolCallId", "agentId"):
            value = source.get(key)
            if value is not None:
                return (key, _canonicalize_dedupe_token(value))

    return ("content",) + tuple(
        _canonicalize_dedupe_token(part) for part in _delegation_key(record)
    )


def parse_cursor_agent_log(log_text: str) -> Dict[str, Any]:
    """Parse a stream-json log into structured fields."""
    session_id: Optional[str] = None
    delegations: List[Dict[str, Any]] = []
    seen_delegations: Set[Tuple[Any, ...]] = set()
    final_report = ""
    action_required: Optional[Dict[str, Any]] = None

    for raw_line in log_text.splitlines():
        if not raw_line.strip():
            continue

        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            if _is_action_required_plain_text(raw_line):
                action_required = {
                    "detail": _extract_action_required_plain_detail(raw_line),
                }
            continue

        if isinstance(event, str):
            if _is_action_required_plain_text(event):
                action_required = {
                    "detail": _extract_action_required_plain_detail(event),
                }
            continue

        if not isinstance(event, dict):
            continue

        if _is_action_required_event(event):
            action_required = {
                "detail": _extract_action_required_detail(event),
            }

        if (
            session_id is None
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            init_sid = event.get("session_id")
            if isinstance(init_sid, str) and init_sid.strip():
                session_id = init_sid.strip()

        if event.get("type") == "tool_call":
            for task_call in _find_task_tool_calls(event):
                args = task_call.get("args") if isinstance(task_call.get("args"), dict) else {}
                record = {
                    "description": args.get("description"),
                    "subagent_type": args.get("subagentType") or args.get("subagent_type"),
                    "model": args.get("model"),
                }
                key = _delegation_dedupe_key(event, task_call, record)
                if key not in seen_delegations:
                    seen_delegations.add(key)
                    delegations.append(record)

        if event.get("type") == "assistant":
            text = _extract_assistant_text(event)
            if text:
                final_report = text

    return {
        "session_id": session_id,
        "delegations": delegations,
        "final_report": final_report,
        "action_required": action_required,
    }


# ---------------------------------------------------------------------------
# Cloud Agent: secrets, origin, worker, HTTP
# ---------------------------------------------------------------------------


class CursorCloudError(Exception):
    """User-visible Cloud Agent failure that is safe to put in the tool result."""


class CursorApiKeyError(CursorCloudError):
    """CURSOR_API_KEY is missing or empty. No silent fallback."""


class UnsupportedOriginError(CursorCloudError):
    """workdir origin cannot be used as a Cloud Agent repo URL."""


def _redact_secret(text: str, secret: Optional[str]) -> str:
    if not text or not secret:
        return text
    return text.replace(secret, "***")


def load_cursor_api_key(path: Optional[Path] = None) -> str:
    """Load ``CURSOR_API_KEY`` from the Cursor Cloud secrets file.

    Does not consult ``os.environ``. Missing file / missing / empty key
    raises :class:`CursorApiKeyError`. The value is never logged.
    """
    env_path = Path(path) if path is not None else CURSOR_CLOUD_ENV_PATH
    if not env_path.is_file():
        raise CursorApiKeyError(
            f"CURSOR_API_KEY is missing. Create {env_path} containing "
            "CURSOR_API_KEY=<key> (no silent fallback)."
        )
    try:
        raw_text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CursorApiKeyError(
            f"CURSOR_API_KEY could not be read from {env_path}"
        ) from exc

    key: Optional[str] = None
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if not stripped.startswith("CURSOR_API_KEY="):
            continue
        raw = stripped.split("=", 1)[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        key = raw.strip()
        break

    if not key:
        raise CursorApiKeyError(
            f"CURSOR_API_KEY is missing or empty in {env_path} "
            "(no silent fallback)."
        )
    return key


def _https_github_repo_url(host: str, path: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in SUPPORTED_ORIGIN_HOSTS:
        raise UnsupportedOriginError(
            f"unsupported git host {host!r}; only HTTPS GitHub origins are supported"
        )
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise UnsupportedOriginError("git origin is not owner/repo")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise UnsupportedOriginError("git origin is not owner/repo")
    return f"https://{host}/{owner}/{repo}"


def normalize_git_origin(raw: str) -> str:
    """Normalize a git remote URL to a live-verified HTTPS GitHub repo URL.

    Rejects local paths, ``file://`` origins, and non-GitHub hosts.
    Cursor v1 create accepts GitHub repo URLs; other hosts are not claimed.
    """
    value = (raw or "").strip()
    if not value:
        raise UnsupportedOriginError("git origin is empty")

    lowered = value.lower()
    if lowered.startswith("file:") or lowered.startswith("file://"):
        raise UnsupportedOriginError("local file:// origins are not supported")
    if re.match(r"^[A-Za-z]:[\\/]", value):
        raise UnsupportedOriginError("local path origins are not supported")
    if value.startswith("/") or value.startswith("./") or value.startswith("../"):
        raise UnsupportedOriginError("local path origins are not supported")
    if value in {".", ".."}:
        raise UnsupportedOriginError("local path origins are not supported")

    if "://" not in value:
        scp = re.match(r"^(?:[^@/\s]+@)?([^:/\s]+):(.+)$", value)
        if not scp:
            raise UnsupportedOriginError("git origin is not a supported HTTPS repo URL")
        return _https_github_repo_url(scp.group(1), scp.group(2))

    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if scheme == "file":
        raise UnsupportedOriginError("local file:// origins are not supported")
    if scheme not in {"https", "http", "ssh", "git"}:
        raise UnsupportedOriginError(
            f"unsupported git origin scheme {scheme!r}; only HTTPS GitHub origins are supported"
        )
    host = parsed.hostname or ""
    if not host:
        raise UnsupportedOriginError("git origin is missing a hostname")
    return _https_github_repo_url(host, parsed.path)


def resolve_workdir_origin(workdir: str) -> str:
    """Read ``origin`` from ``workdir`` and normalize it to HTTPS GitHub."""
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnsupportedOriginError("could not read git origin from workdir") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise UnsupportedOriginError(f"workdir has no usable git origin: {detail}")
    return normalize_git_origin(proc.stdout.strip())


def _remote_has_branch(workdir: str, ref: str, remote: str = "origin") -> bool:
    """Return True when *ref* exists as a branch on *remote* (live ls-remote)."""
    if not ref or ref == "HEAD" or "/" in remote:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "ls-remote", "--heads", remote, f"refs/heads/{ref}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    expected = f"refs/heads/{ref}"
    for line in (proc.stdout or "").splitlines():
        # ls-remote: "<sha>\trefs/heads/<name>"
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() == expected:
            return True
    return False


def resolve_workdir_starting_ref(workdir: str) -> Optional[str]:
    """Return HEAD's branch name only when that branch exists on origin.

    Local-only branches are omitted: Cloud Agents reject a ``startingRef``
    that is not present on the remote (HTTP 400).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    ref = (proc.stdout or "").strip()
    if not ref or ref == "HEAD":
        return None
    if not _remote_has_branch(workdir, ref):
        return None
    return ref


def new_machine_name() -> str:
    return f"hermes-{uuid.uuid4().hex[:12]}"


def build_worker_command(binary: str, name: str, workdir: str) -> List[str]:
    """Argv for a short-lived My Machines worker. Never includes the API key.

    Cursor CLI requires worker options *before* the subcommand:
    ``agent worker --name … --worker-dir … --idle-release-timeout … start``.
    """
    return [
        binary,
        "worker",
        "--name",
        name,
        "--worker-dir",
        workdir,
        "--idle-release-timeout",
        WORKER_IDLE_RELEASE_TIMEOUT,
        "start",
    ]


def apply_worker_no_push_env(env: Dict[str, str]) -> Dict[str, str]:
    """Defense-in-depth Git overrides for the worker process only.

    Uses ``GIT_CONFIG_*`` env overrides so the caller's ``.git/config`` is not
    written. This is not a sandbox: a same-user agent can still reach this
    user's Git/SSH credentials, matching the existing local executor.
    """
    for key in list(env):
        if key.upper() in _NO_PUSH_ENV_KEYS:
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    extras = (
        ("remote.origin.pushurl", NO_PUSH_PUSHURL),
        ("credential.helper", ""),
    )
    try:
        count = int(env.get("GIT_CONFIG_COUNT") or 0)
    except (TypeError, ValueError):
        count = 0
    if count < 0:
        count = 0
    for offset, (config_key, config_value) in enumerate(extras):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = config_key
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = config_value
    env["GIT_CONFIG_COUNT"] = str(count + len(extras))
    return env


def build_worker_env() -> Dict[str, str]:
    """Sanitized worker env. Never includes ``CURSOR_API_KEY``.

    The worker authenticates with the existing Cursor machine login used by
    local ``agent``. Git overrides are defense-in-depth, not containment.
    """
    env = build_subprocess_env(scrub_secrets=True)
    if not env.get("HOME"):
        env["HOME"] = str(Path.home())
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    env = apply_worker_no_push_env(env)
    env.pop("CURSOR_API_KEY", None)
    return env


def worker_env_contains_cursor_api_key(env: Optional[Dict[str, str]] = None) -> bool:
    """Spawn a real child with *env* and report whether ``CURSOR_API_KEY`` is set.

    Used by tests to prove the worker environment cannot see the parent key.
    """
    probe_env = dict(env if env is not None else build_worker_env())
    script = (
        "import os, sys\n"
        "sys.stdout.write('present' if os.environ.get('CURSOR_API_KEY') else 'absent')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=probe_env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return proc.stdout.strip() == "present"


def diagnose_agent_status(
    returncode: int,
    stdout: str,
    stderr: str = "",
) -> Optional[str]:
    """Return the allowlisted auth error, or ``None`` if authenticated.

    Never includes raw CLI output, API-key advice, or status payload fields.
    """
    del stderr  # inspected only for presence; never copied into errors
    if returncode != 0:
        return WORKER_AUTH_ERROR
    text = (stdout or "").strip()
    if not text:
        return WORKER_AUTH_ERROR
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return WORKER_AUTH_ERROR
    if not isinstance(payload, dict):
        return WORKER_AUTH_ERROR
    flag = payload.get("isAuthenticated")
    status = str(payload.get("status") or "").strip().lower()
    if flag is True or status == "authenticated":
        return None
    return WORKER_AUTH_ERROR


def preflight_worker_auth(binary: str, env: Dict[str, str]) -> None:
    """Require an existing Cursor CLI login before spawning the worker.

    Runs ``agent status --format json`` in the same sanitized worker env.
    Does not inject ``CURSOR_API_KEY`` and never suggests doing so.
    """
    try:
        proc = subprocess.run(
            [binary, "status", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=WORKER_STATUS_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        raise CursorCloudError(WORKER_AUTH_ERROR)
    error = diagnose_agent_status(proc.returncode, proc.stdout or "", proc.stderr or "")
    if error:
        raise CursorCloudError(error)


def build_create_agent_payload(
    *,
    task: str,
    repo_url: str,
    machine_name: str,
    agent_id: str,
    model: Optional[str] = None,
    starting_ref: Optional[str] = None,
    force: bool = True,
) -> Dict[str, Any]:
    """POST /v1/agents body. ``force`` must not enable pushes or PRs.

    Runs are Cursor-hosted (no ``env`` field): self-hosted ``machine``
    routing silently queues forever on accounts without self-hosted
    entitlements, while Cursor-hosted runs execute and expose a live
    ``cursor.com/agents/<id>`` progress page.
    """
    del force  # reserved; never maps to autoCreatePR / workOnCurrentBranch / pushes
    payload: Dict[str, Any] = {
        "prompt": {"text": f"{NO_PUSH_PROMPT_PREFIX}{DEFAULT_ORCHESTRATION_PROMPT}{task}"},
        "name": machine_name,
        "agentId": agent_id,
        "repos": [{"url": repo_url}],
        "autoCreatePR": False,
        "skipReviewerRequest": True,
        "workOnCurrentBranch": False,
    }
    if starting_ref:
        payload["repos"][0]["startingRef"] = starting_ref
    model_name = str(model or "").strip()
    if model_name:
        payload["model"] = {"id": model_name}
    return payload


def _emit_progress_notice(message: str) -> bool:
    """Emit a mid-tool status line through the generic tool-status context.

    Bound on CLI/gateway conversation turns (``invoke_tool`` /
    ``run_conversation``). Unbound dispatch is a no-op — the exact URL still
    lands in the tool result. No platform imports and no conversation-message
    mutation.
    """
    return emit_tool_status(message)


def _http_request(
    method: str,
    path: str,
    *,
    api_key: str,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Authenticated Cloud Agents API call.

    The key is never logged. Failures are raised as fresh exceptions with no
    ``__cause__`` / ``__context__`` so httpx request/Authorization objects
    cannot leak through exception chains.
    """
    import httpx

    url = f"{CURSOR_API_BASE}{path}"
    timeout_msg: Optional[str] = None
    error_msg: Optional[str] = None
    parsed: Any = None
    succeeded = False
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method,
                url,
                auth=(api_key, ""),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json=json_body,
                params=params,
            )
            response.raise_for_status()
            if not response.content:
                parsed = {}
            else:
                parsed = response.json()
            succeeded = True
    except httpx.TimeoutException:
        timeout_msg = f"Cursor Cloud Agent request timed out: {method} {path}"
    except httpx.HTTPStatusError as exc:
        status = "?"
        body = ""
        try:
            if exc.response is not None:
                status = exc.response.status_code
                body = (exc.response.text or "")[:300]
        except Exception:
            body = ""
        detail = f"HTTP {status}"
        if body:
            detail = f"{detail}: {body}"
        error_msg = _redact_secret(
            f"Cursor Cloud Agent API error ({method} {path}): {detail}", api_key
        )
    except Exception as exc:
        error_msg = _redact_secret(
            f"Cursor Cloud Agent API error ({method} {path}): {type(exc).__name__}",
            api_key,
        )
    if succeeded:
        return parsed
    if timeout_msg:
        raise TimeoutError(timeout_msg)
    raise CursorCloudError(error_msg or f"Cursor Cloud Agent API error ({method} {path})")


def extract_progress_url(agent_obj: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(agent_obj, dict):
        return None
    url = agent_obj.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def dedupe_created_agent(
    items: Any,
    *,
    agent_id: str,
    machine_name: str,
    repo_url: str,
) -> Optional[Dict[str, Any]]:
    """Find an agent created by a timed-out POST before retrying."""
    if not isinstance(items, list):
        return None
    repo_normalized = repo_url.rstrip("/").lower()
    by_id: Optional[Dict[str, Any]] = None
    by_name: Optional[Dict[str, Any]] = None
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if item_id == agent_id:
            by_id = item
            break
        if str(item.get("name") or "") != machine_name:
            continue
        env = item.get("env") if isinstance(item.get("env"), dict) else {}
        if str(env.get("type") or "") != "machine":
            continue
        if str(env.get("name") or "") not in {"", machine_name}:
            continue
        repos = item.get("repos")
        if isinstance(repos, list) and repos:
            first = repos[0] if isinstance(repos[0], dict) else {}
            item_repo = str(first.get("url") or "").rstrip("/").lower()
            if item_repo and item_repo != repo_normalized:
                continue
        by_name = item
    return by_id or by_name


def create_agent_with_timeout_dedupe(
    payload: Dict[str, Any],
    *,
    api_key: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """POST /v1/agents; on timeout, list/dedupe before a single retry."""
    agent_id = str(payload.get("agentId") or "")
    machine_name = str(payload.get("name") or "")
    repos = payload.get("repos") if isinstance(payload.get("repos"), list) else []
    repo_url = ""
    if repos and isinstance(repos[0], dict):
        repo_url = str(repos[0].get("url") or "")

    def _from_create(body: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not isinstance(body, dict):
            raise CursorCloudError("Cursor Cloud Agent create returned a non-object")
        agent = body.get("agent") if isinstance(body.get("agent"), dict) else None
        run = body.get("run") if isinstance(body.get("run"), dict) else None
        if agent is None:
            raise CursorCloudError("Cursor Cloud Agent create response missing agent")
        if run is None:
            run_id = agent.get("latestRunId")
            if not run_id:
                raise CursorCloudError("Cursor Cloud Agent create response missing run")
            run = {"id": run_id, "agentId": agent.get("id"), "status": "CREATING"}
        return agent, run

    try:
        return _from_create(
            _http_request(
                "POST",
                "/v1/agents",
                api_key=api_key,
                json_body=payload,
                timeout=POST_TIMEOUT_SECONDS,
            )
        )
    except TimeoutError:
        logger.info("Cursor Cloud Agent POST timed out; listing agents to dedupe")
    except CursorCloudError as exc:
        if "HTTP 409" not in str(exc):
            raise
        logger.info("Cursor Cloud Agent POST conflict; listing agents to dedupe")

    listed = _http_request(
        "GET",
        "/v1/agents",
        api_key=api_key,
        params={"limit": 50, "includeArchived": False},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    items = listed.get("items") if isinstance(listed, dict) else None
    found = dedupe_created_agent(
        items, agent_id=agent_id, machine_name=machine_name, repo_url=repo_url
    )
    if found is not None:
        agent_id_found = str(found.get("id") or agent_id)
        detail = found
        if not extract_progress_url(detail) or not detail.get("latestRunId"):
            try:
                fetched = _http_request(
                    "GET",
                    f"/v1/agents/{agent_id_found}",
                    api_key=api_key,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                if isinstance(fetched, dict):
                    detail = fetched
            except CursorCloudError:
                pass
        run_id = detail.get("latestRunId")
        run = {
            "id": run_id,
            "agentId": detail.get("id") or agent_id_found,
            "status": detail.get("status") or "CREATING",
        }
        return detail, run

    return _from_create(
        _http_request(
            "POST",
            "/v1/agents",
            api_key=api_key,
            json_body=payload,
            timeout=POST_TIMEOUT_SECONDS,
        )
    )


def is_terminal_run_status(status: Any) -> bool:
    return str(status or "").strip().upper() in TERMINAL_RUN_STATUSES


def _check_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted

        return is_interrupted()
    except Exception:
        return False


def cancel_cloud_run(agent_id: str, run_id: str, api_key: str) -> None:
    if not agent_id or not run_id:
        return
    try:
        _http_request(
            "POST",
            f"/v1/agents/{agent_id}/runs/{run_id}/cancel",
            api_key=api_key,
            json_body={},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.debug("Cursor Cloud Agent cancel failed", exc_info=True)


def poll_cloud_run(
    *,
    agent_id: str,
    run_id: str,
    api_key: str,
    timeout_seconds: int,
    started_mono: float,
) -> Dict[str, Any]:
    """Poll GET /v1/agents/{id}/runs/{runId} until a terminal status."""
    last: Dict[str, Any] = {"id": run_id, "agentId": agent_id, "status": "CREATING"}
    while True:
        if _check_interrupted():
            cancel_cloud_run(agent_id, run_id, api_key)
            last["status"] = "CANCELLED"
            last["_local_error"] = "interrupted"
            return last
        if timeout_seconds > 0 and (time.monotonic() - started_mono) >= timeout_seconds:
            cancel_cloud_run(agent_id, run_id, api_key)
            last["status"] = last.get("status") or "CANCELLED"
            last["_local_error"] = "timeout"
            return last

        try:
            last = _http_request(
                "GET",
                f"/v1/agents/{agent_id}/runs/{run_id}",
                api_key=api_key,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise CursorCloudError(str(exc)) from None
        if not isinstance(last, dict):
            raise CursorCloudError("Cursor Cloud Agent poll returned a non-object")
        status = str(last.get("status") or "").strip().upper()
        last["status"] = status
        if is_terminal_run_status(status):
            return last
        time.sleep(POLL_INTERVAL_SECONDS)


class MachineWorker:
    """Unique short-lived My Machines worker with bounded log + pg cleanup."""

    def __init__(
        self,
        *,
        binary: str,
        name: str,
        workdir: str,
        log_path: Path,
    ) -> None:
        self.binary = binary
        self.name = name
        self.workdir = workdir
        self.log_path = log_path
        self.cmd = build_worker_command(binary, name, workdir)
        self.env = build_worker_env()
        self.proc: Optional[subprocess.Popen] = None
        self.pgid: Optional[int] = None
        self._reader: Optional[threading.Thread] = None

    def start(self) -> None:
        preflight_worker_auth(self.binary, self.env)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=self.env,
        )
        try:
            getpgid = getattr(os, "getpgid", None)
            if getpgid is not None:
                self.pgid = getpgid(self.proc.pid)  # windows-footgun: ok — getattr-gated POSIX pgid
        except (OSError, ProcessLookupError, AttributeError):
            self.pgid = None
        self._reader = threading.Thread(target=self._read_bounded_log, daemon=True)
        self._reader.start()
        self._wait_until_ready()

    def _wait_until_ready(self) -> None:
        for _ in range(max(1, WORKER_READY_ATTEMPTS)):
            if self.proc is not None and self.proc.poll() is not None:
                raise CursorCloudError(
                    f"Cursor My Machines worker exited early with code {self.proc.returncode}"
                )
            time.sleep(WORKER_READY_DELAY_SECONDS)

    def _read_bounded_log(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        written = 0
        try:
            with open(self.log_path, "wb") as log_file:
                while True:
                    try:
                        chunk = self.proc.stdout.read1(4096)
                    except (OSError, ValueError):
                        break
                    if not chunk:
                        break
                    if written < WORKER_LOG_MAX_BYTES:
                        take = chunk[: WORKER_LOG_MAX_BYTES - written]
                        log_file.write(take)
                        log_file.flush()
                        written += len(take)
        finally:
            try:
                if self.proc.stdout is not None:
                    self.proc.stdout.close()
            except Exception:
                pass

    def cleanup(self) -> None:
        if self.proc is None:
            return
        try:
            _terminate_process(self.proc, self.pgid)
        except Exception:
            logger.debug("Cursor worker cleanup failed", exc_info=True)
        if self._reader is not None:
            self._reader.join(timeout=3.0)


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
    delegations: Optional[List[Dict[str, Any]]] = None,
    duration_seconds: float = 0.0,
    session_id: Optional[str] = None,
    log_path: Optional[str] = None,
    error: Optional[str] = None,
    **extra: Any,
) -> str:
    payload: Dict[str, Any] = {
        "success": success,
        "final_report": final_report,
        "delegations": delegations or [],
        "duration_seconds": duration_seconds,
        "session_id": session_id,
        "log_path": log_path,
        "error": error,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _terminal_summary_from_result_json(result_json: str) -> Dict[str, Any]:
    try:
        payload = json.loads(result_json)
    except json.JSONDecodeError:
        return {"raw": result_json[:500]}
    if not isinstance(payload, dict):
        return {}
    keep = (
        "success",
        "error",
        "final_report",
        "session_id",
        "outcome",
        "attempt_id",
        "cloud_status",
        "agent_id",
        "run_id",
    )
    return {key: payload.get(key) for key in keep if key in payload}


def _revalidate_receipt_for_recovery(
    receipt_path: Path,
    receipt: Dict[str, Any],
    *,
    hermes_session_id: str,
    tool_call_id: str,
    request_fingerprint_value: str,
) -> None:
    """Fail-closed TOCTOU revalidation under the binding lock."""
    _assert_regular_receipt_file(receipt_path, must_exist=True)
    _assert_receipt_permissions(receipt_path)
    validate_receipt_binding(
        receipt,
        hermes_session_id=hermes_session_id,
        tool_call_id=tool_call_id,
        request_fingerprint_value=request_fingerprint_value,
    )


def _authoritative_terminal_reconcile(
    receipt: Dict[str, Any],
    receipt_path: Path,
    *,
    api_key: str,
) -> Tuple[Optional[str], bool]:
    """Reconcile a terminal receipt from Cursor Cloud under the binding lock.

    Returns ``(result_json, cloud_still_running)``. When ``result_json`` is set,
    append it as the authoritative tool result. When ``cloud_still_running`` is
    True, the local terminal claim was stale and polling should resume. When
    both indicate failure, fail closed without appending.
    """
    cloud_agent_id = str(receipt.get("cloud_agent_id") or "")
    cloud_run_id = str(receipt.get("cloud_run_id") or "")
    if not cloud_agent_id or not cloud_run_id:
        return None, False

    client_id = str(receipt.get("client_agent_id") or "")
    if client_id and cloud_agent_id != client_id:
        return None, False

    try:
        run_obj = fetch_cloud_run(cloud_agent_id, cloud_run_id, api_key)
    except CursorCloudError:
        return None, False

    if not is_terminal_run_status(run_obj.get("status")):
        return None, True

    try:
        agent_obj = fetch_cloud_agent(cloud_agent_id, api_key)
    except CursorCloudError:
        return None, False
    if str(agent_obj.get("id") or "") != cloud_agent_id:
        return None, False

    result_json, _, outcome, _status = _build_cloud_tool_result_from_run(
        agent=agent_obj,
        run=run_obj,
        duration_seconds=0.0,
        log_path=str(receipt.get("log_path") or ""),
        attempt_id=str(receipt.get("attempt_id") or ""),
    )
    finalize_receipt(
        receipt_path,
        outcome=outcome,
        terminal_result={"result_json": result_json},
        log_path=str(receipt.get("log_path") or ""),
        cloud_agent_id=cloud_agent_id,
        cloud_run_id=cloud_run_id,
    )
    return result_json, False


def fetch_cloud_run(agent_id: str, run_id: str, api_key: str) -> Dict[str, Any]:
    body = _http_request(
        "GET",
        f"/v1/agents/{agent_id}/runs/{run_id}",
        api_key=api_key,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if not isinstance(body, dict):
        raise CursorCloudError("Cursor Cloud Agent poll returned a non-object")
    status = str(body.get("status") or "").strip().upper()
    body["status"] = status
    return body


def fetch_cloud_agent(agent_id: str, api_key: str) -> Dict[str, Any]:
    body = _http_request(
        "GET",
        f"/v1/agents/{agent_id}",
        api_key=api_key,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if not isinstance(body, dict):
        raise CursorCloudError("Cursor Cloud Agent lookup returned a non-object")
    return body


def _discover_cloud_ids_from_receipt(
    receipt: Dict[str, Any],
    *,
    api_key: str,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Resolve authoritative cloud agent/run ids for a pending receipt."""
    client_id = str(receipt.get("client_agent_id") or "")
    if not client_id:
        return None, None, None

    agent_obj: Optional[Dict[str, Any]] = None
    try:
        agent_obj = fetch_cloud_agent(client_id, api_key)
    except CursorCloudError:
        listed = _http_request(
            "GET",
            "/v1/agents",
            api_key=api_key,
            params={"limit": 50, "includeArchived": False},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        items = listed.get("items") if isinstance(listed, dict) else None
        repo_url = ""
        workdir = str(receipt.get("workdir") or "")
        if workdir:
            try:
                repo_url = resolve_workdir_origin(workdir)
            except UnsupportedOriginError:
                repo_url = ""
        agent_obj = dedupe_created_agent(
            items,
            agent_id=client_id,
            machine_name="",
            repo_url=repo_url,
        )

    if agent_obj is None:
        return None, None, None

    resolved_agent_id = str(agent_obj.get("id") or client_id)
    resolved_run_id = str(receipt.get("cloud_run_id") or agent_obj.get("latestRunId") or "")
    if not resolved_run_id:
        return resolved_agent_id, None, agent_obj
    return resolved_agent_id, resolved_run_id, agent_obj


def _tool_result_already_present(
    agent_history: List[Dict[str, Any]],
    tool_call_id: str,
) -> bool:
    for msg in agent_history:
        if msg.get("role") == "tool" and str(msg.get("tool_call_id") or "") == tool_call_id:
            return True
    return False


_CLOUD_ERROR_FALLBACK = "Cursor Cloud Agent run failed"


def _cloud_error_detail(final_report: str, run: Dict[str, Any]) -> str:
    """Pick a user-visible ERROR detail from run.result or provider diagnostics."""
    if isinstance(final_report, str) and final_report.strip():
        return final_report
    if isinstance(run, dict):
        reason = run.get("failureReason")
        if isinstance(reason, str) and reason.strip():
            return reason
    return _CLOUD_ERROR_FALLBACK


def _build_cloud_tool_result_from_run(
    *,
    agent: Optional[Dict[str, Any]],
    run: Dict[str, Any],
    duration_seconds: float,
    log_path: Optional[str],
    progress_url: Optional[str] = None,
    attempt_id: Optional[str] = None,
) -> Tuple[str, bool, str, Optional[str]]:
    fields = _cloud_result_fields(
        agent=agent,
        run=run,
        duration_seconds=duration_seconds,
        log_path=log_path,
        progress_url=progress_url,
    )
    local_error = run.get("_local_error")
    cloud_status = fields.get("cloud_status")
    outcome = _cloud_outcome_from_run(local_error=local_error, cloud_status=cloud_status)
    if local_error:
        return (
            _make_cloud_tool_result(
                success=False,
                error=str(local_error),
                attempt_id=attempt_id,
                **fields,
            ),
            False,
            outcome,
            cloud_status,
        )
    if cloud_status == "FINISHED":
        return (
            _make_cloud_tool_result(success=True, error=None, attempt_id=attempt_id, **fields),
            True,
            outcome,
            cloud_status,
        )
    if cloud_status == "ERROR":
        detail = _cloud_error_detail(fields.get("final_report") or "", run)
        return (
            _make_cloud_tool_result(success=False, error=detail, attempt_id=attempt_id, **fields),
            False,
            outcome,
            cloud_status,
        )
    if cloud_status == "CANCELLED":
        return (
            _make_cloud_tool_result(success=False, error="cancelled", attempt_id=attempt_id, **fields),
            False,
            outcome,
            cloud_status,
        )
    if cloud_status == "EXPIRED":
        return (
            _make_cloud_tool_result(success=False, error="expired", attempt_id=attempt_id, **fields),
            False,
            outcome,
            cloud_status,
        )
    return (
        _make_cloud_tool_result(
            success=False,
            error=f"Cursor Cloud Agent ended with status {cloud_status}",
            attempt_id=attempt_id,
            **fields,
        ),
        False,
        outcome,
        cloud_status,
    )


def _execute_cloud_delegation(
    *,
    task: str,
    workdir: str,
    model: Optional[str],
    timeout_seconds: int,
    force: bool,
    hermes_session_id: str,
    tool_call_id: Optional[str],
    receipt_path: Path,
    receipt: Dict[str, Any],
    api_key: str,
    recovery_mode: bool = False,
) -> str:
    """Create or resume a cloud delegation under an existing receipt + binding lock."""
    clamped_timeout = _clamp_timeout_seconds(timeout_seconds)
    workdir_path = Path(workdir)
    repo_url = resolve_workdir_origin(str(workdir_path))
    starting_ref = resolve_workdir_starting_ref(str(workdir_path))
    model_name = str(model or receipt.get("model") or "").strip() or None
    force_enabled = is_truthy_value(force, default=True)

    log_path = Path(str(receipt.get("log_path") or ""))
    log_dir = log_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    machine_name = new_machine_name()
    client_agent_id = str(
        receipt.get("client_agent_id")
        or deterministic_client_agent_id(hermes_session_id, tool_call_id)
    )

    cloud_agent_id = str(receipt.get("cloud_agent_id") or "")
    cloud_run_id = str(receipt.get("cloud_run_id") or "")
    agent_obj: Optional[Dict[str, Any]] = None
    run_obj: Dict[str, Any] = {}
    started_mono = time.monotonic()

    if cloud_agent_id and cloud_run_id:
        run_obj = fetch_cloud_run(cloud_agent_id, cloud_run_id, api_key)
        if is_terminal_run_status(run_obj.get("status")):
            result_json, _, outcome, _status = _build_cloud_tool_result_from_run(
                agent={"id": cloud_agent_id, "latestRunId": cloud_run_id},
                run=run_obj,
                duration_seconds=0.0,
                log_path=str(log_path),
                attempt_id=str(receipt.get("attempt_id") or ""),
            )
            finalize_receipt(
                receipt_path,
                outcome=outcome,
                terminal_result={"result_json": result_json},
                log_path=str(log_path),
                cloud_agent_id=cloud_agent_id,
                cloud_run_id=cloud_run_id,
            )
            return result_json
        try:
            agent_obj = fetch_cloud_agent(cloud_agent_id, api_key)
        except CursorCloudError:
            agent_obj = {"id": cloud_agent_id, "latestRunId": cloud_run_id}
    elif recovery_mode:
        discovered_agent_id, discovered_run_id, agent_obj = _discover_cloud_ids_from_receipt(
            receipt,
            api_key=api_key,
        )
        if discovered_agent_id and discovered_run_id:
            persist_cloud_ids(
                receipt_path,
                cloud_agent_id=discovered_agent_id,
                cloud_run_id=discovered_run_id,
            )
            receipt = read_receipt(receipt_path) or receipt
            cloud_agent_id = discovered_agent_id
            cloud_run_id = discovered_run_id
            run_obj = fetch_cloud_run(cloud_agent_id, cloud_run_id, api_key)
            if is_terminal_run_status(run_obj.get("status")):
                result_json, _, outcome, _status = _build_cloud_tool_result_from_run(
                    agent=agent_obj or {"id": cloud_agent_id},
                    run=run_obj,
                    duration_seconds=0.0,
                    log_path=str(log_path),
                    attempt_id=str(receipt.get("attempt_id") or ""),
                )
                finalize_receipt(
                    receipt_path,
                    outcome=outcome,
                    terminal_result={"result_json": result_json},
                    log_path=str(log_path),
                    cloud_agent_id=cloud_agent_id,
                    cloud_run_id=cloud_run_id,
                )
                return result_json
        else:
            return _make_result(
                success=False,
                error=(
                    "Interrupted delegate_cursor_agent has a pending cloud receipt "
                    "without persisted run ids and no authoritative cloud match; "
                    "automatic recovery refused."
                ),
            )

    if not cloud_agent_id or not cloud_run_id:
        payload = build_create_agent_payload(
            task=str(task).strip(),
            repo_url=repo_url,
            machine_name=machine_name,
            agent_id=client_agent_id,
            model=model_name,
            starting_ref=starting_ref,
            force=force_enabled,
        )
        try:
            agent_obj, run_obj = create_agent_with_timeout_dedupe(payload, api_key=api_key)
        except TimeoutError as exc:
            result_json = _make_result(success=False, error=str(exc))
            finalize_receipt(
                receipt_path,
                outcome="timeout",
                terminal_result={"result_json": result_json},
                log_path=str(log_path),
            )
            return result_json
        except CursorCloudError as exc:
            result_json = _make_result(success=False, error=str(exc))
            finalize_receipt(
                receipt_path,
                outcome="failed",
                terminal_result={"result_json": result_json},
                log_path=str(log_path),
            )
            return result_json
        except Exception as exc:
            result_json = _make_result(
                success=False,
                error=_redact_secret(str(exc), api_key),
            )
            finalize_receipt(
                receipt_path,
                outcome="failed",
                terminal_result={"result_json": result_json},
                log_path=str(log_path),
            )
            return result_json

        returned_agent_id = str(agent_obj.get("id") or "")
        if returned_agent_id != client_agent_id:
            result_json = _make_result(
                success=False,
                error=(
                    "Cursor Cloud Agent create returned an agent id that does not "
                    "match the deterministic binding id"
                ),
            )
            finalize_receipt(
                receipt_path,
                outcome="failed",
                terminal_result={"result_json": result_json},
                log_path=str(log_path),
            )
            return result_json

        progress_url = extract_progress_url(agent_obj)
        if progress_url:
            _emit_progress_notice(f"Cursor Cloud Agent: {progress_url}")

        cloud_agent_id = str(agent_obj.get("id") or client_agent_id)
        cloud_run_id = str(run_obj.get("id") or agent_obj.get("latestRunId") or "")
        if not cloud_run_id:
            result_json = _make_result(
                success=False,
                error="Cursor Cloud Agent create did not return a run id",
            )
            finalize_receipt(
                receipt_path,
                outcome="failed",
                terminal_result={"result_json": result_json},
                log_path=str(log_path),
            )
            return result_json
        persist_cloud_ids(
            receipt_path,
            cloud_agent_id=cloud_agent_id,
            cloud_run_id=cloud_run_id,
        )
        receipt = read_receipt(receipt_path) or receipt
        started_mono = time.monotonic()

    progress_url = extract_progress_url(agent_obj)
    if not run_obj or not is_terminal_run_status(run_obj.get("status")):
        try:
            run_obj = poll_cloud_run(
                agent_id=cloud_agent_id,
                run_id=cloud_run_id,
                api_key=api_key,
                timeout_seconds=clamped_timeout,
                started_mono=started_mono,
            )
        except CursorCloudError as exc:
            result_json = _make_result(success=False, error=str(exc))
            finalize_receipt(
                receipt_path,
                outcome="failed",
                terminal_result={"result_json": result_json},
                log_path=str(log_path),
                cloud_agent_id=cloud_agent_id,
                cloud_run_id=cloud_run_id,
            )
            return result_json

    result_json, _success, outcome, _cloud_status = _build_cloud_tool_result_from_run(
        agent=agent_obj,
        run=run_obj,
        duration_seconds=time.monotonic() - started_mono,
        log_path=str(log_path),
        progress_url=progress_url,
        attempt_id=str(receipt.get("attempt_id") or ""),
    )
    finalize_receipt(
        receipt_path,
        outcome=outcome,
        terminal_result={"result_json": result_json},
        log_path=str(log_path),
        cloud_agent_id=cloud_agent_id,
        cloud_run_id=cloud_run_id,
    )
    return result_json


def _find_dangling_delegate_cursor_call(
    agent_history: List[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    if not agent_history:
        return None
    last = agent_history[-1]
    if not (
        isinstance(last, dict)
        and last.get("role") == "assistant"
        and last.get("tool_calls")
    ):
        return None
    pending_ids = {
        str(call.get("id") or call.get("call_id") or "")
        for call in (last.get("tool_calls") or [])
    }
    answered = {
        str(msg.get("tool_call_id") or "")
        for msg in agent_history
        if msg.get("role") == "tool"
    }
    unanswered = [cid for cid in pending_ids if cid and cid not in answered]
    if not unanswered:
        return None
    for call in last.get("tool_calls") or []:
        function = call.get("function") or {}
        if str(function.get("name") or "") != "delegate_cursor_agent":
            continue
        call_id = str(call.get("id") or call.get("call_id") or "")
        if call_id in unanswered:
            return last, call
    return None


def recover_delegate_cursor_agent_history(
    agent_history: List[Dict[str, Any]],
    *,
    hermes_session_id: str,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Reconcile or resume an interrupted ``delegate_cursor_agent`` cloud run."""
    found = _find_dangling_delegate_cursor_call(agent_history)
    if not found:
        last_call = None
        last = agent_history[-1] if agent_history else None
        if isinstance(last, dict) and last.get("role") == "assistant":
            for call in last.get("tool_calls") or []:
                function = call.get("function") or {}
                if str(function.get("name") or "") == "delegate_cursor_agent":
                    last_call = call
                    break
        if last_call is not None:
            existing_id = str(last_call.get("id") or last_call.get("call_id") or "")
            if existing_id and _tool_result_already_present(agent_history, existing_id):
                return agent_history, (
                    "[System note: delegate_cursor_agent tool result already present; "
                    "duplicate recovery append skipped.]"
                )
        return agent_history, None
    _assistant_msg, call = found
    tool_call_id = str(call.get("id") or call.get("call_id") or "")
    if _tool_result_already_present(agent_history, tool_call_id):
        return agent_history, (
            "[System note: delegate_cursor_agent tool result already present; "
            "duplicate recovery append skipped.]"
        )
    try:
        args = json.loads(str((call.get("function") or {}).get("arguments") or "{}"))
    except json.JSONDecodeError:
        args = {}

    try:
        match = find_receipt_for_binding(hermes_session_id, tool_call_id or None)
    except ReceiptValidationError:
        return agent_history, (
            "[System note: delegate_cursor_agent receipt lookup failed validation; "
            "automatic recovery refused.]"
        )
    if match is None:
        return agent_history, (
            "[System note: An interrupted delegate_cursor_agent call has no "
            "matching restart receipt; automatic recovery was not attempted.]"
        )

    receipt_path, receipt = match
    fingerprint = request_fingerprint(
        task=str(args.get("task") or ""),
        workdir=str(args.get("workdir") or receipt.get("workdir") or ""),
        model=args.get("model"),
        force=is_truthy_value(args.get("force", receipt.get("force", True)), default=True),
        timeout_seconds=int(args.get("timeout_seconds", receipt.get("timeout_seconds") or 0)),
        prompt_hash=str(receipt.get("prompt_hash") or hash_prompt(str(args.get("task") or ""))),
    )
    if not receipt_matches_binding(
        receipt,
        hermes_session_id=hermes_session_id,
        tool_call_id=tool_call_id or None,
        request_fingerprint_value=fingerprint,
    ):
        return agent_history, (
            "[System note: delegate_cursor_agent receipt binding mismatch; "
            "automatic recovery refused.]"
        )

    recovery_attempts = int(receipt.get("recovery_attempts") or 0)
    if recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
        return agent_history, (
            "[System note: delegate_cursor_agent recovery attempt cap reached; "
            "automatic recovery refused.]"
        )

    workdir = str(receipt.get("workdir") or args.get("workdir") or "")
    if not workdir:
        return agent_history, (
            "[System note: delegate_cursor_agent receipt missing workdir; "
            "automatic recovery refused.]"
        )

    try:
        api_key = load_cursor_api_key()
    except CursorApiKeyError as exc:
        return agent_history, (
            f"[System note: delegate_cursor_agent recovery refused: {exc}]"
        )

    try:
        with binding_run_lock(hermes_session_id, tool_call_id) as acquired:
            if not acquired:
                return agent_history, (
                    "[System note: Another Hermes process is recovering the "
                    "same delegate_cursor_agent run; this session skipped recovery.]"
                )
            fresh = read_receipt(receipt_path)
            if fresh is None:
                return agent_history, (
                    "[System note: delegate_cursor_agent receipt disappeared during recovery.]"
                )
            try:
                _revalidate_receipt_for_recovery(
                    receipt_path,
                    fresh,
                    hermes_session_id=hermes_session_id,
                    tool_call_id=tool_call_id,
                    request_fingerprint_value=fingerprint,
                )
            except ReceiptValidationError:
                return agent_history, (
                    "[System note: delegate_cursor_agent receipt binding mismatch; "
                    "automatic recovery refused.]"
                )

            cloud_still_running = False
            if is_terminal_receipt(fresh):
                result_json, cloud_still_running = _authoritative_terminal_reconcile(
                    fresh,
                    receipt_path,
                    api_key=api_key,
                )
                if result_json:
                    history = list(agent_history)
                    history.append(
                        make_tool_result_message(
                            "delegate_cursor_agent",
                            result_json,
                            tool_call_id,
                            effect_disposition="unknown",
                        )
                    )
                    return history, (
                        "[System note: Recovered a terminal delegate_cursor_agent result "
                        "from authoritative Cursor Cloud evidence without re-invoking create.]"
                    )
                if not cloud_still_running:
                    return agent_history, (
                        "[System note: delegate_cursor_agent terminal receipt could not "
                        "be verified against Cursor Cloud; automatic recovery refused.]"
                    )

            update_receipt(
                receipt_path,
                recovery_attempts=recovery_attempts + 1,
            )
            result_json = _execute_cloud_delegation(
                task=str(args.get("task") or ""),
                workdir=workdir,
                model=args.get("model"),
                timeout_seconds=int(
                    args.get("timeout_seconds", fresh.get("timeout_seconds") or 0)
                ),
                force=is_truthy_value(args.get("force", fresh.get("force", True)), default=True),
                hermes_session_id=hermes_session_id,
                tool_call_id=tool_call_id or None,
                receipt_path=receipt_path,
                receipt=fresh,
                api_key=api_key,
                recovery_mode=True,
            )
    except ReceiptValidationError:
        return agent_history, (
            "[System note: delegate_cursor_agent receipt lookup failed validation; "
            "automatic recovery refused.]"
        )

    history = list(agent_history)
    history.append(
        make_tool_result_message(
            "delegate_cursor_agent",
            result_json,
            tool_call_id,
            effect_disposition="unknown",
        )
    )
    agent_id = str((read_receipt(receipt_path) or {}).get("cloud_agent_id") or "")
    run_id = str((read_receipt(receipt_path) or {}).get("cloud_run_id") or "")
    return history, (
        "[System note: Automatically resumed an interrupted delegate_cursor_agent "
        f"cloud run (agent={agent_id}, run={run_id}).]"
    )


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

def _cloud_result_fields(
    *,
    agent: Optional[Dict[str, Any]] = None,
    run: Optional[Dict[str, Any]] = None,
    duration_seconds: float = 0.0,
    log_path: Optional[str] = None,
    progress_url: Optional[str] = None,
) -> Dict[str, Any]:
    agent = agent if isinstance(agent, dict) else {}
    run = run if isinstance(run, dict) else {}
    agent_id = agent.get("id") or run.get("agentId")
    run_id = run.get("id") or agent.get("latestRunId")
    cloud_status = str(run.get("status") or "").strip().upper() or None
    url = progress_url or extract_progress_url(agent)
    final_report = ""
    result_text = run.get("result")
    if isinstance(result_text, str):
        final_report = result_text
    duration_ms = run.get("durationMs")
    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        duration_seconds = float(duration_ms) / 1000.0
    return {
        "final_report": final_report,
        "delegations": [],
        "duration_seconds": round(float(duration_seconds), 3),
        "session_id": agent_id,
        "log_path": log_path,
        "agent_id": agent_id,
        "run_id": run_id,
        "cloud_status": cloud_status,
        "progress_url": url,
    }


def _cloud_outcome_from_run(
    *,
    local_error: Optional[str],
    cloud_status: Optional[str],
) -> str:
    if local_error == "interrupted":
        return "interrupted"
    if local_error == "timeout":
        return "timeout"
    if cloud_status == "FINISHED":
        return "success"
    if cloud_status == "ERROR":
        return "failed"
    if cloud_status == "CANCELLED":
        return "cancelled"
    if cloud_status == "EXPIRED":
        return "expired"
    if local_error:
        return "error"
    return "error"


def _make_cloud_tool_result(
    *,
    receipt_run_id: Optional[str] = None,
    **fields: Any,
) -> str:
    if receipt_run_id:
        fields["receipt_run_id"] = receipt_run_id
    return _make_result(**fields)


def delegate_cursor_agent(
    task: str,
    workdir: str,
    model: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    force: bool = True,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    task_id: str | None = None,
) -> str:
    del task_id  # metadata only; Hermes session identity comes from session_id

    if not task or not str(task).strip():
        return _make_result(
            success=False,
            error="task is required for delegate_cursor_agent",
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

    hermes_session_id = str(session_id or "").strip()
    if not hermes_session_id:
        return _make_result(
            success=False,
            error="session_id is required for delegate_cursor_agent",
        )
    if not tool_call_id or not str(tool_call_id).strip():
        return _make_result(
            success=False,
            error="tool_call_id is required for delegate_cursor_agent",
        )

    if not resolve_cursor_agent_binary():
        return _make_result(
            success=False,
            error=(
                "Cursor Agent CLI binary not found. Install the `agent` CLI and "
                "ensure it is on PATH or at ~/.local/bin/agent."
            ),
        )

    try:
        api_key = load_cursor_api_key()
    except CursorApiKeyError as exc:
        return _make_result(success=False, error=str(exc))

    try:
        resolve_workdir_origin(str(workdir_path))
    except UnsupportedOriginError as exc:
        return _make_result(success=False, error=str(exc))

    clamped_timeout = _clamp_timeout_seconds(timeout_seconds)
    force_enabled = is_truthy_value(force, default=True)
    model_name = str(model or "").strip() or None
    task_text = str(task).strip()
    prompt_hash = hash_prompt(task_text)

    log_dir = cursor_runs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    machine_name = new_machine_name()
    log_path = log_dir / f"{run_timestamp}-{os.getpid()}-{machine_name}.log"

    try:
        with binding_run_lock(hermes_session_id, tool_call_id) as acquired:
            if not acquired:
                return _make_result(
                    success=False,
                    error=(
                        "Another Hermes process is already creating or recovering "
                        "this delegate_cursor_agent binding."
                    ),
                )
            try:
                receipt_path, receipt = create_receipt(
                    hermes_session_id=hermes_session_id,
                    tool_call_id=tool_call_id,
                    workdir=str(workdir_path),
                    prompt_hash=prompt_hash,
                    log_path=str(log_path),
                    model=model_name,
                    force=force_enabled,
                    timeout_seconds=clamped_timeout,
                    task=task_text,
                )
            except ReceiptValidationError as exc:
                return _make_result(
                    success=False,
                    error=f"delegate_cursor_agent receipt creation refused: {exc}",
                )

            if is_terminal_receipt(receipt):
                fresh = read_receipt(receipt_path)
                if fresh is None:
                    return _make_result(
                        success=False,
                        error=(
                            "delegate_cursor_agent receipt disappeared during "
                            "repeat invocation; refusing to create replacement work."
                        ),
                    )
                try:
                    _revalidate_receipt_for_recovery(
                        receipt_path,
                        fresh,
                        hermes_session_id=hermes_session_id,
                        tool_call_id=tool_call_id,
                        request_fingerprint_value=request_fingerprint(
                            task=task_text,
                            workdir=str(workdir_path),
                            model=model_name,
                            force=force_enabled,
                            timeout_seconds=clamped_timeout,
                            prompt_hash=prompt_hash,
                        ),
                    )
                except ReceiptValidationError as exc:
                    return _make_result(
                        success=False,
                        error=(
                            "delegate_cursor_agent receipt revalidation refused: "
                            f"{exc}"
                        ),
                    )
                result_json, cloud_still_running = _authoritative_terminal_reconcile(
                    fresh,
                    receipt_path,
                    api_key=api_key,
                )
                if result_json:
                    return result_json
                if not cloud_still_running:
                    return _make_result(
                        success=False,
                        error=(
                            "delegate_cursor_agent terminal receipt could not be "
                            "verified against Cursor Cloud; refusing to create "
                            "replacement work."
                        ),
                    )
                receipt = fresh

            return _execute_cloud_delegation(
                task=task_text,
                workdir=str(workdir_path),
                model=model_name,
                timeout_seconds=clamped_timeout,
                force=force_enabled,
                hermes_session_id=hermes_session_id,
                tool_call_id=tool_call_id,
                receipt_path=receipt_path,
                receipt=receipt,
                api_key=api_key,
                recovery_mode=False,
            )
    except ReceiptValidationError as exc:
        return _make_result(
            success=False,
            error=f"delegate_cursor_agent receipt lock refused: {exc}",
        )


CURSOR_AGENT_SCHEMA = {
    "name": "delegate_cursor_agent",
    "description": (
        "Delegate a software development task to a Cursor My Machines Cloud "
        "Agent on this host. Starts a short-lived worker in workdir, POSTs "
        "/v1/agents with env.type=machine for the checkout's GitHub origin, "
        "and waits for the run to finish. Does not open a PR or request "
        "reviewers. Available only when the Cursor Agent CLI binary is installed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The development task for Cursor Agent to perform.",
            },
            "workdir": {
                "type": "string",
                "description": "Absolute path to the target project directory.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Cursor Agent model to use for the run. Omit to use "
                    "whatever model is selected in the Cursor CLI's own "
                    "config (~/.cursor/cli-config.json)."
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
            "force": {
                "type": "boolean",
                "description": (
                    "Accepted for compatibility. Does not enable API-side "
                    "auto-created PRs or reviewer requests."
                ),
                "default": True,
            },
        },
        "required": ["task", "workdir"],
    },
}


def _handle_delegate_cursor_agent(args, **kw):
    return delegate_cursor_agent(
        task=args.get("task", ""),
        workdir=args.get("workdir", ""),
        model=args.get("model"),
        timeout_seconds=args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        force=is_truthy_value(args.get("force", True), default=True),
        session_id=kw.get("session_id"),
        tool_call_id=kw.get("tool_call_id"),
        task_id=kw.get("task_id"),
    )


registry.register(
    name="delegate_cursor_agent",
    toolset="delegation",
    schema=CURSOR_AGENT_SCHEMA,
    handler=_handle_delegate_cursor_agent,
    check_fn=check_cursor_agent_requirements,
    emoji="🖥️",
    max_result_size_chars=100_000,
)
