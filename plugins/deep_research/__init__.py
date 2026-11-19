"""Bundled plugin: durable multi-lane deep research via ``delegate_research``.

Layout::

    plugin.yaml     manifest (default-enabled, standalone)
    config.py       the frozen ``deep_research:`` config section
    jobs.py         durable job store under $HERMES_HOME/research_jobs/
    tool.py         the ``delegate_research`` model tool (web toolset)
    evidence.py     post_tool_call hook recording fetched sources
    citations.py    report-vs-ledger provenance validation
    prompts.py      untrusted-data-fenced lane/synthesis/correction prompts
    launcher.py     systemd transient user service (+ detached fallback)
    runner.py       the host-owned job runner (python -m …runner)
    notify.py       completion re-entry + stale-job recovery watcher
    provision.py    on-demand worker-profile provisioning (Go decision table)

See README.md in this directory for the operator view.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("hermes.plugins.deep_research")

# The plugin-owned toolset the synthesis writer runs under when the caller's
# config disables worker file tools (``deep_research.worker_file_tools:
# false``). Deliberately empty and sealed: synthesis-only — no retrieval, no
# files; the lane reports are already injected into the writer's prompt.
# Registered through ``ctx.register_toolset()`` in ``register()`` below so the
# toolset exists only while this plugin is enabled; ``runner.py`` passes it to
# the writer session as ``-t research_writer`` (NO_FILE_WRITER_TOOLSETS).
WRITER_NO_FILE_TOOLSET = "research_writer"


def _on_gateway_start(**_kwargs) -> None:
    """Provision the worker profile (best-effort), then start the watcher.

    Provisioning happens both here and lazily at ``start``: this covers the
    common case before the first job, while the lazy path is the authority.
    A provisioning failure never takes the watcher down — ``start`` reports
    the error to the caller instead.
    """
    from plugins.deep_research import notify, provision
    from plugins.deep_research.config import load_deep_research_config

    config = load_deep_research_config()
    try:
        provision.ensure_worker_profile(config.worker_profile)
    except provision.ProvisionError as exc:
        logger.warning("deep_research: %s", exc)
    started = notify.start_gateway_watcher(interval_seconds=config.notify_interval_seconds)
    if started:
        logger.info(
            "deep_research: completion watcher started (profile=%s)",
            config.worker_profile,
        )


def register(ctx) -> None:
    from plugins.deep_research import jobs, tool
    from plugins.deep_research.evidence import handle_post_tool_call

    ctx.register_tool(
        name=tool.TOOL_NAME,
        toolset="web",
        schema=tool.DELEGATE_RESEARCH_SCHEMA,
        handler=tool.handle_delegate_research,
        check_fn=tool.check_requirements,
        emoji="🔬",
    )

    # The no-file writer's zero-tool boundary is owned here, not by the core
    # toolset catalog: sealed keeps registry/plugin overlays from widening it,
    # and disabling this plugin makes the toolset invalid everywhere at once.
    ctx.register_toolset(
        WRITER_NO_FILE_TOOLSET,
        description=(
            "Synthesis-only writer: no retrieval, no files "
            "(deep_research with worker_file_tools: false)"
        ),
        tools=[],
        includes=[],
        sealed=True,
    )

    # Evidence ledger: a strict no-op outside a runner-spawned worker session.
    ctx.register_hook("post_tool_call", handle_post_tool_call)

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("on_gateway_start", _on_gateway_start)


__all__ = ["register", "jobs"]
