"""Bundled backend plugin: safe unattended Hermes updates on Linux/systemd."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from plugins.auto_update.cli import auto_update_command, register_cli

    ctx.register_cli_command(
        name="auto_update",
        help="Unattended Hermes update scheduler (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=auto_update_command,
        description=(
            "Install and manage an independent systemd timer that prepares every "
            "available update on each tick (`hermes update --yes --defer-restart`) "
            "and restarts the fleet onto it only once Hermes is idle and the "
            "preparation is proven complete (`hermes auto_update activate`)."
        ),
    )

    def _on_gateway_start(**kwargs) -> None:
        from plugins.auto_update.lifecycle import reconcile_scheduler_on_load

        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_scheduler_on_load(**reconcile_kwargs)

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_gateway_start", _on_gateway_start)
