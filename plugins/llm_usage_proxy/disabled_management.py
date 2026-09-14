"""Disabled-state management CLI for ``plugins.disabled: [llm_usage_proxy]``."""

from __future__ import annotations


def register_disabled(ctx) -> None:
    """Register the management CLI only — no proxy install capability.

    ``hermes llm_usage_proxy status`` / ``enable`` stay reachable after a
    ``plugins.disabled`` entry, which is the only way back out.
    """
    from plugins.llm_usage_proxy.cli import llm_usage_proxy_command, register_cli

    ctx.register_cli_command(
        name="llm_usage_proxy",
        help="Loopback LLM usage-recording reverse proxy (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=llm_usage_proxy_command,
        description=(
            "Manage the bundled loopback usage-recording reverse proxy while "
            "the plugin is disabled in config (status, enable, disable, "
            "reconcile, serve)."
        ),
    )

    def _on_gateway_start(**kwargs) -> None:
        # Still fires while disabled: reconcile treats an explicit disable as
        # "stop and disable whatever this profile installed" and stands any
        # in-process routing down.
        from plugins.llm_usage_proxy.lifecycle import reconcile_proxy_on_load

        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_proxy_on_load(**reconcile_kwargs)

    ctx.register_hook("on_gateway_start", _on_gateway_start)
