"""Load ``auto_update`` settings from config.yaml with safe defaults."""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_IDLE_MINUTES = 8
DEFAULT_SCHEDULE_CALENDAR = "*-*-* *:00,30:00"
DEFAULT_SCHEDULE_START_HOUR = 4
DEFAULT_SCHEDULE_END_HOUR = 8
DEFAULT_RANDOMIZED_DELAY_SEC = 0
DEFAULT_ACCURACY_SEC = "1s"


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


def _calendar_from_hour_window(start_hour: int, end_hour: int) -> str:
    hours = list(range(start_hour, end_hour))
    if not hours:
        return DEFAULT_SCHEDULE_CALENDAR
    return f"*-*-* {','.join(f'{hour:02d}' for hour in hours)}:00:00"


def default_schedule_calendar() -> str:
    return DEFAULT_SCHEDULE_CALENDAR


def resolve_schedule(raw: Mapping[str, Any]) -> str:
    """Return the systemd OnCalendar expression for auto-update."""
    explicit = raw.get("schedule") or raw.get("schedule_calendar")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    if "schedule_start_hour" in raw or "schedule_end_hour" in raw:
        start_hour = _coerce_int(
            raw.get("schedule_start_hour"), DEFAULT_SCHEDULE_START_HOUR, minimum=0
        )
        end_hour = _coerce_int(
            raw.get("schedule_end_hour"), DEFAULT_SCHEDULE_END_HOUR, minimum=0
        )
        if end_hour > start_hour:
            return _calendar_from_hour_window(start_hour, end_hour)

    return default_schedule_calendar()


def load_auto_update_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized auto-update settings.

    Explicit ``auto_update.enabled: false`` or ``plugins.disabled`` containing
    ``auto_update`` always wins — callers must check those gates first.
    """
    if raw is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}
        raw = cfg.get("auto_update") or {}

    if not isinstance(raw, Mapping):
        raw = {}

    return {
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "idle_minutes": _coerce_int(
            raw.get("idle_minutes"), DEFAULT_IDLE_MINUTES, minimum=1
        ),
        "schedule": resolve_schedule(raw),
        "randomized_delay_sec": _coerce_int(
            raw.get("randomized_delay_sec"),
            DEFAULT_RANDOMIZED_DELAY_SEC,
            minimum=0,
        ),
        "accuracy_sec": str(raw.get("accuracy_sec") or DEFAULT_ACCURACY_SEC).strip()
        or DEFAULT_ACCURACY_SEC,
        "notify_on_success": str(raw.get("notify_on_success") or "").strip(),
        "notify_on_failure": str(raw.get("notify_on_failure") or "").strip(),
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
    if isinstance(disabled, list) and "auto_update" in disabled:
        return True

    section = cfg.get("auto_update") or {}
    if isinstance(section, Mapping):
        enabled = section.get("enabled")
        if enabled is not None and not _coerce_bool(enabled, True):
            return True
    return False
