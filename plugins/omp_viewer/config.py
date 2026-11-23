"""Load ``delegation.omp.viewer`` settings with safe defaults."""

from __future__ import annotations

from typing import Any, Mapping

from plugins.auto_update.config import _coerce_bool
from plugins.omp_viewer.url import (
    extra_hosts,
    viewer_bind,
    viewer_port,
    viewer_scan_roots,
)


def load_omp_viewer_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized viewer settings from ``delegation.omp.viewer``.

    Explicit ``delegation.omp.viewer.enabled: false`` or a
    ``plugins.disabled`` entry for ``omp_viewer`` always wins — callers
    must check those gates first (see :func:`plugin_explicitly_disabled`).
    """
    if raw is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            delegation = cfg.get("delegation") or {}
            omp = (
                delegation.get("omp")
                if isinstance(delegation, Mapping)
                else None
            )
            raw = omp.get("viewer") if isinstance(omp, Mapping) else None
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
        "scan_roots": list(viewer_scan_roots(raw)),
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
    if isinstance(disabled, list) and "omp_viewer" in disabled:
        return True

    delegation = cfg.get("delegation") or {}
    omp = delegation.get("omp") if isinstance(delegation, Mapping) else None
    section = omp.get("viewer") if isinstance(omp, Mapping) else None
    if isinstance(section, Mapping):
        enabled = section.get("enabled")
        if enabled is not None and not _coerce_bool(enabled, True):
            return True
    return False
