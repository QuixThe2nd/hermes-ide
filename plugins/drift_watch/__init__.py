"""Bundled backend plugin: uncommitted-drift watch for the live Hermes checkout."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from plugins.drift_watch.cli import drift_watch_command, register_cli

    ctx.register_cli_command(
        name="drift_watch",
        help="Uncommitted-drift watch for the live checkout (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=drift_watch_command,
        description=(
            "Inventory the live Hermes checkout's uncommitted drift on a timer, "
            "auto-capture a patch plus untracked copies whenever the drift set "
            "changes, and attribute writes via auditd where available. Read-only "
            "toward git state: it never reverts or deletes drift."
        ),
    )

    def _on_gateway_start(**kwargs) -> None:
        from plugins.drift_watch.systemd import reconcile_scheduler_on_load

        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_scheduler_on_load(**reconcile_kwargs)

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_gateway_start", _on_gateway_start)
