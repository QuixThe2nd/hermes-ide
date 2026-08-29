"""Load ``drift_watch`` settings from config.yaml with safe defaults."""

from __future__ import annotations

import os
from typing import Any, Mapping

DEFAULT_SCHEDULE_CALENDAR = "*-*-* *:07,37:00"
DEFAULT_RETAIN_DAYS = 90
DEFAULT_MAX_CAPTURES = 50


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def default_tree() -> str:
    """Live checkout this plugin watches — ``HERMES_PROJECT`` or empty (inert)."""
    return os.environ.get("HERMES_PROJECT", "").strip()


def default_schedule_calendar() -> str:
    return DEFAULT_SCHEDULE_CALENDAR


def default_state_dir() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home() / "state" / "drift-watch")


def resolve_state_dir(raw: Mapping[str, Any]) -> str:
    explicit = raw.get("state_dir")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return default_state_dir()


def load_drift_watch_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized drift-watch settings.

    Explicit ``drift_watch.enabled: false`` or ``plugins.disabled`` containing
    ``drift_watch`` always wins — callers must check those gates first.
    """
    if raw is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}
        raw = cfg.get("drift_watch") or {}

    if not isinstance(raw, Mapping):
        raw = {}

    return {
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "tree": str(raw.get("tree") or default_tree()).strip(),
        "state_dir": resolve_state_dir(raw),
        "schedule": str(raw.get("schedule") or DEFAULT_SCHEDULE_CALENDAR).strip()
        or DEFAULT_SCHEDULE_CALENDAR,
        "retain_days": _coerce_int(
            raw.get("retain_days"), DEFAULT_RETAIN_DAYS, minimum=1
        ),
        "max_captures": _coerce_int(
            raw.get("max_captures"), DEFAULT_MAX_CAPTURES, minimum=1
        ),
    }


def plugin_explicitly_disabled(cfg: Mapping[str, Any] | None = None) -> bool:
    """True when config or the plugins deny-list disables this plugin."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}

    plugins = cfg.get("plugins") or {}
    disabled = plugins.get("disabled") or []
    if isinstance(disabled, list) and "drift_watch" in disabled:
        return True

    section = cfg.get("drift_watch") or {}
    if isinstance(section, Mapping):
        enabled = section.get("enabled")
        if enabled is not None and not _coerce_bool(enabled, True):
            return True
    return False
