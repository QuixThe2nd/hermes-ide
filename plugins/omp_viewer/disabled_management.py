"""Disabled-state management CLI for ``plugins.disabled: [omp_viewer]``."""

from __future__ import annotations


def register_disabled(ctx) -> None:
    """Register the management CLI only — no viewer install capability.

    ``hermes omp_viewer status`` / ``enable`` stay reachable after a
    ``plugins.disabled`` entry, which is the only way back out.
    """
    from plugins.omp_viewer.cli import omp_viewer_command, register_cli

    ctx.register_cli_command(
        name="omp_viewer",
        help="Local OMP delegate-lane session viewer (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=omp_viewer_command,
        description=(
            "Manage the bundled OMP lane session viewer while the plugin is "
            "disabled in config (status, enable, disable, reconcile)."
        ),
    )

    def _on_gateway_start(**kwargs) -> None:
        # Still fires while disabled: reconcile treats an explicit disable as
        # "stop and disable whatever this plugin installed".
        from plugins.omp_viewer.lifecycle import reconcile_viewer_on_load

        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_viewer_on_load(**reconcile_kwargs)

    ctx.register_hook("on_gateway_start", _on_gateway_start)
