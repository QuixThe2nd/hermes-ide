"""Dev Pipeline plugin — durable automated development jobs.

Registers ``delegate_development`` (toolset ``dev-pipeline``): submit a repo +
task and Hermes plans via the MoA council, executes bounded work through the
Cursor CLI, verifies mechanically, reviews with Kimi K3 + Grok 4.5, and opens
a draft PR on pass. Jobs live in the Kanban DB and survive gateway/executor
restarts and host reboots.

The executor systemd user service self-installs on plugin load
(Linux/systemd user scope — see ``executor_setup.py``).
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the delegate_development tool. Called once by the plugin loader."""
    from plugins.dev_pipeline.tool import (
        DELEGATE_DEVELOPMENT_SCHEMA,
        DEV_PIPELINE_STATUS_SCHEMA,
        _handle_delegate_development,
        _handle_dev_pipeline_status,
        check_dev_pipeline_requirements,
    )

    # PARKED 2026-08-28 — re-enable when the user unparks.
    # ctx.register_tool(
    #     name="delegate_development",
    #     toolset="dev-pipeline",
    #     schema=DELEGATE_DEVELOMENT_SCHEMA,
    #     handler=_handle_delegate_development,
    #     check_fn=check_dev_pipeline_requirements,
    #     emoji="🏗️",
    # )
    ctx.register_tool(
        name="dev_pipeline_status",
        toolset="dev-pipeline",
        schema=DEV_PIPELINE_STATUS_SCHEMA,
        handler=_handle_dev_pipeline_status,
        check_fn=check_dev_pipeline_requirements,
        emoji="📊",
    )

    def _on_gateway_start(**kwargs) -> None:
        from plugins.dev_pipeline.executor_setup import reconcile_executor_on_load

        # Runs regardless of dev_pipeline.enabled — the flag gates work-claiming
        # inside the executor, not whether the unit exists. Reconcile never
        # raises, so plugin/gateway load cannot fail because of it.
        reconcile_kwargs = {}
        if "scope" in kwargs:
            reconcile_kwargs["scope"] = kwargs["scope"]
        if "run_systemctl" in kwargs:
            reconcile_kwargs["run_systemctl"] = kwargs["run_systemctl"]
        reconcile_executor_on_load(**reconcile_kwargs)

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_gateway_start", _on_gateway_start)
