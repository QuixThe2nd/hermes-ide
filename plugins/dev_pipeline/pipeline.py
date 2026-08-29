"""Shared dev-pipeline logic (stdlib only).

Pure helpers used by ``delegate_development``, the dev executor, and unit
tests. Nothing here imports systemd, Cursor, or gateway code.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlparse

from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plan contract schema
# ---------------------------------------------------------------------------

REQUIRED_PLAN_CONTRACT_KEYS: frozenset[str] = frozenset(
    {
        "task_summary",
        "lane_hint",
        "estimated_minutes",
        "allowed_paths",
        "acceptance_commands",
        "broad_flags",
        "blocked_reasons",
        "step_plan",
        "assumptions",
    }
)

BROAD_FLAG_KEYS: frozenset[str] = frozenset(
    {
        "migration",
        "repo_wide_change",
        "toolchain_change",
        "multi_subsystem",
        "long_verification",
    }
)

VALID_LANE_HINTS: frozenset[str] = frozenset({"cursor", "broad"})

VALID_BLOCKED_REASONS: frozenset[str] = frozenset(
    {
        "missing_credentials",
        "missing_product_input",
        "infra_broken",
        "acceptance_unverifiable",
    }
)

OPEN_DEDUP_STATUSES: frozenset[str] = frozenset(
    {"triage", "todo", "ready", "running", "blocked"}
)

TERMINAL_RESUBMIT_STATUSES: frozenset[str] = frozenset({"done"})

MAX_ACCEPTANCE_COMMAND_CHARS = 500
MIN_ESTIMATED_MINUTES = 1
MAX_ESTIMATED_MINUTES = 480
CURSOR_LANE_MAX_MINUTES = 30

# systemd scope vocabulary shared by the config accessor and the executor.
SYSTEMD_SCOPE_USER = "user"
SYSTEMD_SCOPE_SYSTEM = "system"
VALID_SYSTEMD_SCOPES = frozenset({SYSTEMD_SCOPE_USER, SYSTEMD_SCOPE_SYSTEM})


def normalize_systemd_scope(value: Any) -> str | None:
    """Return ``"user"``/``"system"`` for a recognized scope value, else ``None``.

    Case-insensitive and whitespace-tolerant; anything unrecognized reads as
    "unset" so the executor's env/euid fallback tiers decide (a typo in
    config.yaml must not silently pin scope).
    """
    if not isinstance(value, str):
        return None
    scope = value.strip().lower()
    return scope if scope in VALID_SYSTEMD_SCOPES else None


# Config code defaults (not written to DEFAULT_CONFIG — merged at read time).
_DEFAULT_DEV_PIPELINE_ENABLED = False
_DEFAULT_DEV_PIPELINE_BOARD = "dev"
_DEFAULT_CURSOR_TIMEOUT_SECONDS = 1800
_MAX_CURSOR_TIMEOUT_SECONDS = 2400
_DEFAULT_CLAUDE_TIMEOUT_SECONDS = 7200
_MAX_CLAUDE_TIMEOUT_SECONDS = 21600
_DEFAULT_DEV_EXECUTOR_TICK_SECONDS = 15
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_VERIFY_COMMAND_TIMEOUT = 600
_DEFAULT_PROGRESS_NOTIFICATIONS = True
# Wake the submitting agent (not just the human subscriber) when a job blocks
# with an actionable dev-pipeline block kind. On by default: a delegated job
# that fails is exactly the case where the delegating agent should handle it.
_DEFAULT_AGENT_WAKE_ON_BLOCK = True
# cgroup memory ceiling for each attempt unit. Historically hardcoded in the
# systemd-run argv; the default keeps the spawned property byte-identical.
_DEFAULT_ATTEMPT_MEMORY_MAX = "6G"
# Public alias: the executor's systemd-run seam uses it as the parameter
# default so an unset config and a direct call agree on the same ceiling.
DEFAULT_ATTEMPT_MEMORY_MAX = _DEFAULT_ATTEMPT_MEMORY_MAX

# What systemd's ``MemoryMax=`` property parser accepts: a number with an
# optional base-1000/base-1024 suffix, or the literal ``infinity``/``max``
# (no limit). Rejecting anything else here keeps a typo from reaching
# ``systemd-run`` and surfacing as an ``infra_broken`` block.
_MEMORY_MAX_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?\s*[kKmMgGtTpP](?:[iI][bB])?[bB]?|infinity|max)\s*$"
)


def normalize_memory_max(raw: Any, default: str = _DEFAULT_ATTEMPT_MEMORY_MAX) -> str:
    """Validate a configured ``MemoryMax`` value, falling back to *default*.

    Unset, empty, or non-size input resolves to the historical hardcoded
    ``6G``, so an unset knob keeps the spawned property list byte-for-byte
    identical to pre-config behaviour.
    """
    value = str(raw or "").strip()
    if value and _MEMORY_MAX_RE.match(value):
        return value
    if value:
        logger.warning(
            "dev_pipeline.attempt_memory_max=%r is not a systemd size; using %s",
            raw, default,
        )
    return default


def validate_plan_contract(data: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate a MoA plan contract document against the strict slice-1 schema.

    Each ``acceptance_commands`` entry is one timed shell command. Chaining
    with ``&&`` or ``;`` is allowed — the entire string is executed and timed
    as a single command.

    Returns:
        ``(contract, errors)`` — on success ``errors`` is empty and
        ``contract`` is the normalized dict; on failure ``contract`` is
        ``None`` and ``errors`` lists human-readable problems.
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return None, ["plan contract must be a JSON object"]

    unknown = set(data.keys()) - REQUIRED_PLAN_CONTRACT_KEYS
    if unknown:
        errors.append(
            "unknown top-level keys: "
            + ", ".join(sorted(unknown))
        )

    missing = REQUIRED_PLAN_CONTRACT_KEYS - set(data.keys())
    if missing:
        errors.append(
            "missing required keys: " + ", ".join(sorted(missing))
        )

    if errors:
        return None, errors

    contract: dict[str, Any] = {}

    task_summary = data["task_summary"]
    if not isinstance(task_summary, str) or not task_summary.strip():
        errors.append("task_summary must be a non-empty string")
    else:
        contract["task_summary"] = task_summary

    lane_hint = data["lane_hint"]
    if lane_hint not in VALID_LANE_HINTS:
        errors.append(
            f"lane_hint must be one of {sorted(VALID_LANE_HINTS)}, got {lane_hint!r}"
        )
    else:
        contract["lane_hint"] = lane_hint

    estimated = data["estimated_minutes"]
    if not isinstance(estimated, int) or isinstance(estimated, bool):
        errors.append("estimated_minutes must be an integer")
    elif estimated < MIN_ESTIMATED_MINUTES or estimated > MAX_ESTIMATED_MINUTES:
        errors.append(
            f"estimated_minutes must be between {MIN_ESTIMATED_MINUTES} "
            f"and {MAX_ESTIMATED_MINUTES}, got {estimated}"
        )
    else:
        contract["estimated_minutes"] = estimated

    allowed_paths = data["allowed_paths"]
    if not isinstance(allowed_paths, list):
        errors.append("allowed_paths must be a list")
    else:
        normalized_paths: list[str] = []
        for idx, path in enumerate(allowed_paths):
            if not isinstance(path, str) or not path.strip():
                errors.append(f"allowed_paths[{idx}] must be a non-empty string")
                continue
            if path.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", path):
                errors.append(f"allowed_paths[{idx}] must be relative, not absolute")
            elif ".." in path.split("/"):
                errors.append(f"allowed_paths[{idx}] must not contain '..'")
            else:
                normalized_paths.append(path)
        contract["allowed_paths"] = normalized_paths

    acceptance_commands = data["acceptance_commands"]
    if not isinstance(acceptance_commands, list) or not acceptance_commands:
        errors.append("acceptance_commands must be a non-empty list")
    else:
        normalized_cmds: list[str] = []
        for idx, cmd in enumerate(acceptance_commands):
            if not isinstance(cmd, str) or not cmd.strip():
                errors.append(
                    f"acceptance_commands[{idx}] must be a non-empty string"
                )
                continue
            if len(cmd) > MAX_ACCEPTANCE_COMMAND_CHARS:
                errors.append(
                    f"acceptance_commands[{idx}] exceeds "
                    f"{MAX_ACCEPTANCE_COMMAND_CHARS} characters"
                )
            else:
                normalized_cmds.append(cmd)
        contract["acceptance_commands"] = normalized_cmds

    broad_flags = data["broad_flags"]
    if not isinstance(broad_flags, dict):
        errors.append("broad_flags must be an object")
    else:
        flag_keys = set(broad_flags.keys())
        if flag_keys != BROAD_FLAG_KEYS:
            missing_flags = BROAD_FLAG_KEYS - flag_keys
            extra_flags = flag_keys - BROAD_FLAG_KEYS
            if missing_flags:
                errors.append(
                    "broad_flags missing keys: "
                    + ", ".join(sorted(missing_flags))
                )
            if extra_flags:
                errors.append(
                    "broad_flags unknown keys: "
                    + ", ".join(sorted(extra_flags))
                )
        normalized_flags: dict[str, bool] = {}
        for key in sorted(BROAD_FLAG_KEYS):
            value = broad_flags.get(key)
            if not isinstance(value, bool):
                errors.append(f"broad_flags.{key} must be a boolean")
            else:
                normalized_flags[key] = value
        contract["broad_flags"] = normalized_flags

    blocked_reasons = data["blocked_reasons"]
    if not isinstance(blocked_reasons, list):
        errors.append("blocked_reasons must be a list")
    else:
        normalized_reasons: list[str] = []
        for idx, reason in enumerate(blocked_reasons):
            if not isinstance(reason, str):
                errors.append(f"blocked_reasons[{idx}] must be a string")
                continue
            if reason not in VALID_BLOCKED_REASONS:
                errors.append(
                    f"blocked_reasons[{idx}] invalid value {reason!r}; "
                    f"must be one of {sorted(VALID_BLOCKED_REASONS)}"
                )
            else:
                normalized_reasons.append(reason)
        contract["blocked_reasons"] = normalized_reasons

    step_plan = data["step_plan"]
    if not isinstance(step_plan, list):
        errors.append("step_plan must be a list")
    else:
        normalized_steps: list[dict[str, Any]] = []
        for idx, step in enumerate(step_plan):
            if not isinstance(step, dict):
                errors.append(f"step_plan[{idx}] must be an object")
                continue
            step_id = step.get("id")
            description = step.get("description")
            verifiable = step.get("verifiable")
            if not isinstance(step_id, str) or not step_id.strip():
                errors.append(f"step_plan[{idx}].id must be a non-empty string")
            if not isinstance(description, str) or not description.strip():
                errors.append(
                    f"step_plan[{idx}].description must be a non-empty string"
                )
            if not isinstance(verifiable, bool):
                errors.append(f"step_plan[{idx}].verifiable must be a boolean")
            if (
                isinstance(step_id, str)
                and step_id.strip()
                and isinstance(description, str)
                and description.strip()
                and isinstance(verifiable, bool)
            ):
                normalized_steps.append(
                    {
                        "id": step_id,
                        "description": description,
                        "verifiable": verifiable,
                    }
                )
        contract["step_plan"] = normalized_steps

    assumptions = data["assumptions"]
    if not isinstance(assumptions, list):
        errors.append("assumptions must be a list")
    else:
        normalized_assumptions: list[str] = []
        for idx, item in enumerate(assumptions):
            if not isinstance(item, str):
                errors.append(f"assumptions[{idx}] must be a string")
            else:
                normalized_assumptions.append(item)
        contract["assumptions"] = normalized_assumptions

    if errors:
        return None, errors
    return contract, []


def route_plan_contract(
    contract: Mapping[str, Any],
) -> tuple[str, str | None, str | None]:
    """Apply ROUTING rules to a validated plan contract.

    Returns:
        ``(decision, block_kind, reason_text)`` where *decision* is
        ``"block"``, ``"cursor"``, or ``"claude"``. For non-block decisions,
        *block_kind* and *reason_text* are ``None``.
    """
    blocked_reasons = contract.get("blocked_reasons") or []
    if blocked_reasons:
        first = blocked_reasons[0]
        return "block", first, f"Task blocked: {first}"

    lane_hint = contract.get("lane_hint")
    broad_flags = contract.get("broad_flags") or {}
    estimated = contract.get("estimated_minutes", 0)

    broad_active = any(broad_flags.get(k) for k in BROAD_FLAG_KEYS)
    if (
        lane_hint != "cursor"
        or broad_active
        or (isinstance(estimated, int) and estimated > CURSOR_LANE_MAX_MINUTES)
    ):
        return "claude", None, None

    return "cursor", None, None


def compute_idempotency_key(repo: str, branch: str, task: str) -> str:
    """Return sha256 hex digest of ``repo|branch|task``."""
    payload = f"{repo}|{branch}|{task}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Secret scanning (PUBLISHING phase)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key_pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.MULTILINE,
        ),
    ),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("generic_sk_prefix", re.compile(r"\bsk-[A-Za-z0-9-]{10,}\b")),
    ("slack_bot_token", re.compile(r"\bxoxb-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "env_sensitive_assignment",
        re.compile(
            r"(?i)"
            r"(?:[A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|APIKEY)|[A-Z0-9_]+_TOKEN)"
            r"\s*=\s*\S+"
        ),
    ),
]


def scan_diff_for_secrets(diff_text: str) -> list[dict[str, str]]:
    """Scan unified diff text for conservative secret patterns.

    Returns a list of findings, each ``{"pattern": <name>, "location": <hint>}``.
    Matched secret values are never included in findings.
    """
    if not diff_text:
        return []

    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for line_no, line in enumerate(diff_text.splitlines(), start=1):
        content = line[1:] if line[:1] in {"+", "-", " "} else line
        for pattern_name, pattern in _SECRET_PATTERNS:
            if not pattern.search(content):
                continue
            location = f"line {line_no}"
            key = (pattern_name, location)
            if key in seen:
                continue
            seen.add(key)
            findings.append({"pattern": pattern_name, "location": location})

    return findings


# ---------------------------------------------------------------------------
# Attempt environment (RUNNING phase)
# ---------------------------------------------------------------------------

_ATTEMPT_ENV_ALLOWLIST_EXACT = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "XDG_CONFIG_HOME",
    }
)


def _attempt_env_key_allowed(key: str) -> bool:
    if key in _ATTEMPT_ENV_ALLOWLIST_EXACT:
        return True
    if key.startswith("LC_"):
        return True
    if key.startswith("CURSOR_"):
        return True
    return False


def _attempt_env_key_stripped(key: str) -> bool:
    upper = key.upper()
    if upper in {"GH_TOKEN", "GITHUB_TOKEN"}:
        return True
    if upper.endswith("_API_KEY"):
        return True
    if "_OAUTH" in upper:
        return True
    return False


def build_attempt_env(
    base_env: Mapping[str, str],
    *,
    lane: str = "cursor-bounded",
) -> dict[str, str]:
    """Build a sanitized subprocess environment for a dev-pipeline attempt.

    Supports ``cursor-bounded`` and ``claude-endurance``. Both lanes use the
    same allowlist/strip rules; neither injects credentials into the child env.
    """
    if lane not in {"cursor-bounded", "claude-endurance"}:
        raise ValueError(f"unknown dev-pipeline lane: {lane!r}")

    sanitized: dict[str, str] = {}
    for key, value in base_env.items():
        if _attempt_env_key_stripped(key):
            continue
        if _attempt_env_key_allowed(key):
            sanitized[key] = value
    return sanitized


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------


def get_dev_pipeline_config() -> dict[str, Any]:
    """Return dev-pipeline settings with code defaults applied.

    Defaults live here (not in ``DEFAULT_CONFIG``) so the feature stays
    opt-in without a config-version bump. ``load_config()`` deep-merges user
    YAML at read time; absent keys fall back to the values below.
    """
    cfg = load_config()

    raw_timeout = cfg_get(
        cfg, "dev_pipeline", "cursor_timeout_seconds", default=_DEFAULT_CURSOR_TIMEOUT_SECONDS
    )
    try:
        cursor_timeout = int(raw_timeout)
    except (TypeError, ValueError):
        cursor_timeout = _DEFAULT_CURSOR_TIMEOUT_SECONDS
    cursor_timeout = min(max(cursor_timeout, 1), _MAX_CURSOR_TIMEOUT_SECONDS)

    raw_claude_timeout = cfg_get(
        cfg, "dev_pipeline", "claude_timeout_seconds", default=_DEFAULT_CLAUDE_TIMEOUT_SECONDS
    )
    try:
        claude_timeout = int(raw_claude_timeout)
    except (TypeError, ValueError):
        claude_timeout = _DEFAULT_CLAUDE_TIMEOUT_SECONDS
    claude_timeout = min(max(claude_timeout, 60), _MAX_CLAUDE_TIMEOUT_SECONDS)

    raw_tick = cfg_get(cfg, "dev_executor", "tick_seconds", default=_DEFAULT_DEV_EXECUTOR_TICK_SECONDS)
    try:
        tick_seconds = int(raw_tick)
    except (TypeError, ValueError):
        tick_seconds = _DEFAULT_DEV_EXECUTOR_TICK_SECONDS

    raw_max_attempts = cfg_get(cfg, "dev_pipeline", "max_attempts", default=_DEFAULT_MAX_ATTEMPTS)
    try:
        max_attempts = int(raw_max_attempts)
    except (TypeError, ValueError):
        max_attempts = _DEFAULT_MAX_ATTEMPTS

    raw_verify_timeout = cfg_get(
        cfg, "dev_pipeline", "verify_command_timeout", default=_DEFAULT_VERIFY_COMMAND_TIMEOUT
    )
    try:
        verify_timeout = int(raw_verify_timeout)
    except (TypeError, ValueError):
        verify_timeout = _DEFAULT_VERIFY_COMMAND_TIMEOUT

    return {
        "enabled": bool(cfg_get(cfg, "dev_pipeline", "enabled", default=_DEFAULT_DEV_PIPELINE_ENABLED)),
        "board": str(cfg_get(cfg, "dev_pipeline", "board", default=_DEFAULT_DEV_PIPELINE_BOARD) or _DEFAULT_DEV_PIPELINE_BOARD),
        "cursor_timeout_seconds": cursor_timeout,
        "claude_timeout_seconds": claude_timeout,
        "tick_seconds": tick_seconds,
        "max_attempts": max_attempts,
        "verify_command_timeout": verify_timeout,
        # None when unset/invalid → the executor's env/euid tiers decide
        # (see resolve_systemd_scope in executor.py).
        "systemd_scope": normalize_systemd_scope(
            cfg_get(cfg, "dev_pipeline", "systemd_scope")
        ),
        "progress_notifications": bool(
            cfg_get(
                cfg,
                "dev_pipeline",
                "progress_notifications",
                default=_DEFAULT_PROGRESS_NOTIFICATIONS,
            )
        ),
        # Read live by the kanban notifier on every tick, so flipping it to
        # false halts agent wakes without a gateway restart.
        "agent_wake_on_block": bool(
            cfg_get(
                cfg,
                "dev_pipeline",
                "agent_wake_on_block",
                default=_DEFAULT_AGENT_WAKE_ON_BLOCK,
            )
        ),
        # Validated in the executor's systemd-run seam; an unset or invalid
        # value resolves to the historical hardcoded MemoryMax.
        "attempt_memory_max": normalize_memory_max(
            cfg_get(cfg, "dev_pipeline", "attempt_memory_max")
        ),
    }


def resolve_default_branch(repo: str, *, is_local_path: bool) -> str:
    """Resolve the default git branch for *repo*.

    Local paths: ``git -C <path> symbolic-ref refs/remotes/origin/HEAD``,
    falling back to ``main``. HTTPS URLs default to ``main``.
    """
    if not is_local_path:
        return "main"

    import subprocess

    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                repo,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            ref = proc.stdout.strip()
            if ref.startswith("refs/remotes/origin/"):
                branch = ref[len("refs/remotes/origin/") :]
                if branch:
                    return branch
    except (OSError, subprocess.SubprocessError):
        pass
    return "main"


def is_local_git_repo(path: str) -> bool:
    """Return True when *path* exists and is a git repository."""
    from pathlib import Path

    repo_path = Path(path)
    if not repo_path.is_dir():
        return False
    if (repo_path / ".git").exists():
        return True

    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def is_https_repo_url(repo: str) -> bool:
    """Return True when *repo* is an https URL with a host."""
    parsed = urlparse(repo)
    return parsed.scheme == "https" and bool(parsed.netloc)


def has_url_credentials(repo: str) -> bool:
    """Return True when an https URL embeds userinfo (``user:token@host``).

    Credentialed clone URLs are rejected: the token would persist in the
    workspace ``.git/config`` where the attempt agent runs, and the agent
    could push with it (verifier finding, 2026-08-10).
    """
    parsed = urlparse(repo)
    return bool(parsed.username or parsed.password)


def validate_repo_input(repo: str) -> tuple[bool, str | None]:
    """Validate repo parameter; return ``(ok, error_message)``."""
    repo = (repo or "").strip()
    if not repo:
        return False, "repo is required"

    if is_https_repo_url(repo):
        if has_url_credentials(repo):
            return False, (
                "repo URL must not embed credentials (user:token@host); "
                "the token would leak into the workspace git config"
            )
        return True, None

    if repo.startswith("http://") or repo.startswith("git@"):
        return False, "repo must be an absolute local path or https URL"

    from pathlib import Path

    path = Path(repo)
    if not path.is_absolute():
        return False, "local repo path must be absolute"

    if is_local_git_repo(repo):
        return True, None

    if not path.exists():
        return False, f"local repo path does not exist: {repo}"
    return False, f"local path is not a git repository: {repo}"
