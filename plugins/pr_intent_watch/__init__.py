"""pr_intent_watch plugin — intent review comments on new fork PRs."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """No model tools; ticks come from the self-installed systemd timer."""
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        return

    def _on_gateway_start(**kwargs) -> None:
        from plugins.pr_intent_watch.lifecycle import reconcile_scheduler_on_load

        reconcile_kwargs = {}
        if "unit_dir" in kwargs:
            reconcile_kwargs["unit_dir"] = kwargs["unit_dir"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        try:
            reconcile_scheduler_on_load(**reconcile_kwargs)
        except Exception as exc:
            # Reconcile already contains its own, but plugin load must never
            # die because the scheduler self-install hiccuped.
            logger.warning("pr_intent_watch scheduler reconcile failed: %s", exc)

    register_hook("on_gateway_start", _on_gateway_start)
