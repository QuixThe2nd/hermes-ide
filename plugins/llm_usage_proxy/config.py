"""Load ``llm_usage_proxy`` settings with safe defaults."""

from __future__ import annotations

from typing import Any, Mapping

from plugins.llm_usage_proxy.server import DEFAULT_PORT

CONFIG_SECTION = "llm_usage_proxy"


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_port(value: Any) -> int:
    try:
        port = int(value) if value is not None else DEFAULT_PORT
    except (TypeError, ValueError):
        return DEFAULT_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_PORT


def load_llm_usage_proxy_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized settings from the ``llm_usage_proxy`` config section.

    Explicit ``llm_usage_proxy.enabled: false`` or a ``plugins.disabled``
    entry always wins — callers must check those gates first (see
    :func:`plugin_explicitly_disabled`).
    """
    if raw is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            section = cfg.get(CONFIG_SECTION)
            raw = section if isinstance(section, Mapping) else None
        except Exception:
            raw = None

    if not isinstance(raw, Mapping):
        raw = {}

    upstreams_raw = raw.get("upstreams")
    upstreams: dict[str, str] = {}
    if isinstance(upstreams_raw, Mapping):
        for name, base in upstreams_raw.items():
            if isinstance(base, str) and base.strip():
                upstreams[str(name)] = base.strip()

    return {
        # On by default, per profile: this config section lives in the
        # profile's own HERMES_HOME, so each named profile decides for itself.
        # Opt out with an explicit ``enabled: false`` or a plugins.disabled
        # entry (plugin_explicitly_disabled still wins). Nothing in the
        # global environment can force routing off.
        "enabled": _coerce_bool(raw.get("enabled"), True),
        "port": _coerce_port(raw.get("port")),
        "upstreams": upstreams,
        # Key-manager mode: the proxy owns the provider keys and clients hold
        # local caller tokens. Off by default — it changes who may call the
        # proxy at all, so flipping it on has to be a decision, not a default.
        "manage_keys": _coerce_bool(raw.get("manage_keys"), False),
    }


def manage_keys_enabled(cfg: Mapping[str, Any]) -> bool:
    """Whether a config mapping asks for key-manager mode (never raises)."""
    try:
        return _coerce_bool(cfg.get("manage_keys"), False)
    except AttributeError:
        return False


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
    if isinstance(disabled, list) and CONFIG_SECTION in disabled:
        return True

    section = cfg.get(CONFIG_SECTION)
    if isinstance(section, Mapping):
        enabled = section.get("enabled")
        if enabled is not None and not _coerce_bool(enabled, False):
            return True
    return False
