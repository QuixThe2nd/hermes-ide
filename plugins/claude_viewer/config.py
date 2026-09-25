"""Load ``delegation.claude_viewer`` settings with safe defaults."""

from __future__ import annotations

from typing import Any, Mapping

from tools.claude_viewer_url import (
    DEFAULT_BIND,
    DEFAULT_PORT,
    extra_hosts,
    viewer_bind,
    viewer_port,
)


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def load_claude_viewer_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized viewer settings from ``delegation.claude_viewer``.

    Explicit ``delegation.claude_viewer.enabled: false`` or a
    ``plugins.disabled`` entry for ``claude_viewer`` always wins — callers
    must check those gates first (see :func:`plugin_explicitly_disabled`).
    """
    if raw is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            delegation = cfg.get("delegation") or {}
            raw = (
                delegation.get("claude_viewer")
                if isinstance(delegation, Mapping)
                else None
            )
        except Exception:
            raw = None

    if not isinstance(raw, Mapping):
        raw = {}

    return {
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "bind": viewer_bind(raw),
        "port": viewer_port(raw),
        "public_host": str(raw.get("public_host") or "").strip(),
        "extra_hosts": list(extra_hosts(raw)),
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
    if isinstance(disabled, list) and "claude_viewer" in disabled:
        return True

    delegation = cfg.get("delegation") or {}
    section = (
        delegation.get("claude_viewer")
        if isinstance(delegation, Mapping)
        else None
    )
    if isinstance(section, Mapping):
        enabled = section.get("enabled")
        if enabled is not None and not _coerce_bool(enabled, True):
            return True
    return False
