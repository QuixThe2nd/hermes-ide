"""Config for the deep_research plugin.

Reads the plugin-owned top-level ``deep_research:`` section of config.yaml the
same way ``plugins/drift_watch/config.py`` does (raw readonly load + defaults in
code), so no ``DEFAULT_CONFIG`` change is needed and both the CLI and gateway
loaders see the same values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

CONFIG_SECTION = "deep_research"

# Generic default only — the operator's profile name comes from config.yaml.
DEFAULT_WORKER_PROFILE = "researcher"

DEFAULT_TIMEOUT_MINUTES = 30
MIN_TIMEOUT_MINUTES = 5
MAX_TIMEOUT_MINUTES = 60

DEFAULT_MAX_PARALLEL = 2
MIN_MAX_PARALLEL = 1
MAX_MAX_PARALLEL = 4

DEFAULT_MEMORY_MAX = "2G"
DEFAULT_RUNNER_MODE = "auto"  # auto | systemd | fallback
DEFAULT_NOTIFY_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_RECENT_JOBS = 20
DEFAULT_WORKER_FILE_TOOLS = True

MAX_BRIEF_CHARS = 20_000
MAX_QUESTION_CHARS = 2_000
MIN_QUESTIONS = 1
MAX_QUESTIONS = 8


@dataclass(frozen=True)
class DeepResearchConfig:
    enabled: bool = True
    worker_profile: str = DEFAULT_WORKER_PROFILE
    default_timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
    max_parallel: int = DEFAULT_MAX_PARALLEL
    memory_max: str = DEFAULT_MEMORY_MAX
    runner_mode: str = DEFAULT_RUNNER_MODE
    notify_interval_seconds: float = DEFAULT_NOTIFY_INTERVAL_SECONDS
    max_recent_jobs: int = DEFAULT_MAX_RECENT_JOBS
    worker_file_tools: bool = DEFAULT_WORKER_FILE_TOOLS


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, coerced))


def _coerce_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return default
    if coerced != coerced:  # NaN
        return default
    return max(lo, min(hi, coerced))


def load_deep_research_config(raw: Dict[str, Any] | None = None) -> DeepResearchConfig:
    """Build the plugin config from ``raw`` (already the ``deep_research`` mapping).

    ``raw=None`` loads from config.yaml. Never raises: a malformed section
    degrades to defaults rather than taking the tool down.
    """
    if raw is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}
        raw = cfg.get(CONFIG_SECTION) or {}

    if not isinstance(raw, dict):
        raw = {}

    runner_mode = str(raw.get("runner_mode") or DEFAULT_RUNNER_MODE).strip().lower()
    if runner_mode not in ("auto", "systemd", "fallback"):
        runner_mode = DEFAULT_RUNNER_MODE

    memory_max = str(raw.get("memory_max") or DEFAULT_MEMORY_MAX).strip() or DEFAULT_MEMORY_MAX

    worker_profile = str(raw.get("worker_profile") or DEFAULT_WORKER_PROFILE).strip()
    worker_profile = worker_profile or DEFAULT_WORKER_PROFILE

    # Strict bool: a malformed value (string, dict, …) degrades to the default
    # (file tools on), never silently locks the workers down.
    worker_file_tools_raw = raw.get("worker_file_tools")
    worker_file_tools = (
        worker_file_tools_raw if isinstance(worker_file_tools_raw, bool) else DEFAULT_WORKER_FILE_TOOLS
    )

    return DeepResearchConfig(
        enabled=bool(raw.get("enabled", True)),
        worker_profile=worker_profile,
        default_timeout_minutes=_coerce_int(
            raw.get("default_timeout_minutes"), DEFAULT_TIMEOUT_MINUTES,
            MIN_TIMEOUT_MINUTES, MAX_TIMEOUT_MINUTES,
        ),
        max_parallel=_coerce_int(
            raw.get("max_parallel"), DEFAULT_MAX_PARALLEL, MIN_MAX_PARALLEL, MAX_MAX_PARALLEL,
        ),
        memory_max=memory_max,
        runner_mode=runner_mode,
        notify_interval_seconds=_coerce_float(
            raw.get("notify_interval_seconds"), DEFAULT_NOTIFY_INTERVAL_SECONDS, 1.0, 300.0,
        ),
        max_recent_jobs=_coerce_int(
            raw.get("max_recent_jobs"), DEFAULT_MAX_RECENT_JOBS, 1, 100,
        ),
        worker_file_tools=worker_file_tools,
    )
