"""Bundled backend plugin: loopback usage-recording reverse proxy for Linux/systemd."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from plugins.llm_usage_proxy.cli import llm_usage_proxy_command, register_cli

    ctx.register_cli_command(
        name="llm_usage_proxy",
        help="Loopback LLM usage-recording reverse proxy (Linux/systemd)",
        setup_fn=register_cli,
        handler_fn=llm_usage_proxy_command,
        description=(
            "Run the bundled loopback reverse proxy that measures actual "
            "request tokens on the wire for z.ai, Kimi, Codex, and xAI, "
            "recording them to <HERMES_HOME>/usage-proxy/usage.sqlite. Binds "
            "127.0.0.1 only and is opt-in per profile (`hermes llm_usage_proxy "
            "enable`): provider base URLs, auth, and API modes are untouched — "
            "matching requests are rerouted to the proxy at the final HTTP "
            "transport boundary, and disabling it returns traffic to the real "
            "provider endpoints."
        ),
    )

    def _on_gateway_start(**kwargs) -> None:
        from plugins.llm_usage_proxy.lifecycle import reconcile_proxy_on_load

        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_proxy_on_load(**reconcile_kwargs)

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_gateway_start", _on_gateway_start)
