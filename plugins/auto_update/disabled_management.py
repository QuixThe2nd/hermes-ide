"""Disabled-state cleanup and management CLI for ``plugins.disabled: [auto_update]``."""

from __future__ import annotations


def register_disabled(ctx) -> None:
    """Register gateway cleanup and management CLI only — no updater capability."""
    from plugins.auto_update.cli import (
        management_auto_update_command,
        register_management_cli,
    )

    ctx.register_cli_command(
        name="auto_update",
        help="Unattended Hermes update scheduler (Linux/systemd)",
        setup_fn=register_management_cli,
        handler_fn=management_auto_update_command,
        description=(
            "Manage the Hermes auto-update scheduler while the plugin is disabled "
            "in config (status, enable, disable, reconcile)."
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

    ctx.register_hook("on_gateway_start", _on_gateway_start)
