"""Non-oneshot install/reconcile hook for the auto-update scheduler."""

from __future__ import annotations

import logging
import sys
from typing import Callable, Sequence

from plugins.auto_update.config import load_auto_update_config, plugin_explicitly_disabled
from plugins.auto_update.platform import platform_supported
from plugins.auto_update.systemd import ReconcileResult, reconcile_units

logger = logging.getLogger(__name__)


def is_oneshot_run_invocation(argv: Sequence[str] | None = None) -> bool:
    """True when argv targets ``hermes auto_update run`` (systemd oneshot entry)."""
    tokens = [str(tok) for tok in (argv or sys.argv)]
    for idx, tok in enumerate(tokens):
        if tok != "auto_update":
            continue
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if nxt == "run":
            return True
    return False


def reconcile_scheduler_on_load(
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    scope=None,
) -> ReconcileResult | None:
    """Install/reconcile hook — never call from ``register()`` or oneshot ``run``.

    Explicit disablement (``auto_update.enabled: false`` or ``plugins.disabled``)
    stops any installed timer. Enabled installs reconcile idempotently.
    """
    if is_oneshot_run_invocation():
        return None
    if not platform_supported():
        return None

    cfg = load_auto_update_config()
    enabled = not plugin_explicitly_disabled() and bool(cfg.get("enabled", True))
    kwargs = {}
    if run_systemctl is not None:
        kwargs["run_systemctl"] = run_systemctl
    if scope is not None:
        kwargs["scope"] = scope
    try:
        return reconcile_units(cfg, enabled=enabled, **kwargs)
    except Exception as exc:
        logger.warning(
            "auto_update scheduler reconcile skipped: %s", exc, exc_info=True
        )
        return None
