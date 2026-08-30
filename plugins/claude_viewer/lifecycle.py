"""``on_gateway_start`` install/reconcile hook for the bundled claude-viewer."""

from __future__ import annotations

import logging
from typing import Callable, Sequence

from plugins.auto_update.platform import platform_supported
from plugins.claude_viewer.config import (
    load_claude_viewer_config,
    plugin_explicitly_disabled,
)
from plugins.claude_viewer.systemd import ReconcileResult, reconcile_service

logger = logging.getLogger(__name__)


def reconcile_viewer_on_load(
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    scope=None,
) -> ReconcileResult | None:
    """Install/start the viewer when the gateway comes up. Never raises.

    Explicit disablement (``delegation.claude_viewer.enabled: false`` or
    ``plugins.disabled``) stops any unit this plugin installed. A port already
    served by another viewer is a stand-down, not an error.
    """
    if not platform_supported():
        return None

    cfg = load_claude_viewer_config()
    enabled = not plugin_explicitly_disabled() and bool(cfg.get("enabled", True))
    kwargs: dict = {}
    if run_systemctl is not None:
        kwargs["run_systemctl"] = run_systemctl
    if scope is not None:
        kwargs["scope"] = scope
    try:
        return reconcile_service(cfg, enabled=enabled, **kwargs)
    except Exception as exc:
        logger.warning(
            "claude_viewer reconcile skipped: %s", exc, exc_info=True
        )
        return None
