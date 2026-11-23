"""Bundled backend plugin: local OMP delegate-lane session viewer (Linux/systemd)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from plugins.omp_viewer.cli import omp_viewer_command, register_cli

    ctx.register_cli_command(
        name="omp_viewer",
        help="Local OMP delegate-lane session viewer (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=omp_viewer_command,
        description=(
            "Install and serve the bundled omp-viewer web UI that tails OMP "
            "delegate-lane session transcripts from <workdir>/.hermes-lane "
            "directories under the configured scan roots. Unauthenticated: "
            "binds loopback by default; keep it on LAN/tailnet only."
        ),
    )

    def _on_gateway_start(**kwargs) -> None:
        from plugins.omp_viewer.lifecycle import reconcile_viewer_on_load

        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_viewer_on_load(**reconcile_kwargs)

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_gateway_start", _on_gateway_start)
