"""Bundled backend plugin: local Claude Code run viewer for Linux/systemd."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from plugins.claude_viewer.cli import claude_viewer_command, register_cli

    ctx.register_cli_command(
        name="claude_viewer",
        help="Local Claude Code run viewer (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=claude_viewer_command,
        description=(
            "Install and serve the bundled claude-viewer web UI that tails "
            "delegate_claude_agent runs from <HERMES_HOME>/claude-runs. The "
            "Watch-live link in Discord embeds points at this machine's own "
            "address. Unauthenticated: keep it on LAN/tailnet only."
        ),
    )

    def _on_gateway_start(**kwargs) -> None:
        from plugins.claude_viewer.lifecycle import reconcile_viewer_on_load

        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_viewer_on_load(**reconcile_kwargs)

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_gateway_start", _on_gateway_start)
