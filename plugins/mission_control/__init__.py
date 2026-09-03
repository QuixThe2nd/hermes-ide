"""Bundled backend plugin: Mission Control sessions web UI.

Registers the ``hermes mission_control serve`` CLI command. The plugin
adds no model tools and no hooks; the server only runs when a person
invokes it. See README.md in this directory for run/setup/security
notes.
"""

from __future__ import annotations


def register(ctx) -> None:
    from plugins.mission_control.cli import (
        mission_control_command,
        register_cli,
    )

    ctx.register_cli_command(
        name="mission_control",
        help="Mission Control sessions web UI (serve)",
        setup_fn=register_cli,
        handler_fn=mission_control_command,
        description=(
            "Serve the Mission Control web UI: a local, unauthenticated "
            "messenger-style view of Hermes sessions active in the last "
            "24 hours across the Hermes home's state and profile "
            "databases, with search, live updates, full transcripts, "
            "collapsed tool-call groups, sub-agent and cross-profile "
            "lineage, and a composer that starts new sessions or replies "
            "as authenticated runs on the core API server (POST "
            "/v1/runs), with clarify cards answering mid-turn questions. "
            "Binds loopback by default; a "
            "non-loopback bind is explicit and still unauthenticated."
        ),
    )
