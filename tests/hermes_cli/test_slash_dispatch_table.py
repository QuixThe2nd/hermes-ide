"""Parity guard for the slash-command dispatch table in cli.HermesCLI.

Every canonical command that had a branch in the old if/elif chain must
resolve to a handler in ``_SLASH_DISPATCH``, and the pre-dispatch side effects
(pre_command hook, pending-resume reset, unknown-command fallthrough) must
keep their old semantics.
"""
from unittest.mock import MagicMock, patch

from cli import HermesCLI

# Command names that had an explicit branch in the pre-dispatch-table chain and
# are still registered. /goal and /subscription were retired from the registry
# (goals auto-start via the Jev classifier; usage accounting lives in /status).
OLD_CHAIN_COMMANDS = [
    "exit", "quit", "help", "palette", "whoami", "profile", "tools", "toolsets",
    "config", "redraw", "clear", "history", "title", "handoff", "new", "resume",
    "sessions", "codex-runtime", "retry",
    "prompt", "undo", "branch", "worktree", "save", "cron", "suggestions",
    "blueprint", "skills", "learn", "init", "memory",
    "platforms", "status", "context", "statusbar", "diff", "battery",
    "timestamps", "verbose", "focus", "compress", "insights", "copy",
    "debug", "update", "version", "paste", "image", "reload", "reload-mcp",
    "reload-skills", "browser", "plugins", "rollback", "snapshot",
    "export", "import", "stop", "agents", "journey", "bg", "btw", "queue",
    "refine", "review", "loop", "plan", "model",
    "subgoal", "skin", "indicator",
]

RETIRED_COMMANDS = (
    "steer", "moa", "heartbeat", "curator", "bundles", "goal", "subscription", "usage",
)


def test_every_old_branch_resolves_to_a_handler():
    for name in OLD_CHAIN_COMMANDS:
        entry = HermesCLI._slash_handler(name)
        assert entry is not None, name
        method_name, pass_arg = entry
        assert callable(getattr(HermesCLI, method_name)), name
        assert isinstance(pass_arg, bool)
    # explicit table entries are only the ones the naming convention can't cover
    for name, (method_name, pass_arg) in HermesCLI._SLASH_DISPATCH.items():
        assert name in OLD_CHAIN_COMMANDS
        assert (method_name, pass_arg) != (f"_handle_{name.replace('-', '_')}_command", True), name


def test_dispatch_table_entries_are_registered_commands():
    """Every explicit table key names a live registry command — canonical or
    alias ("exit" is /quit's alias) — and an alias key routes to exactly the
    handler of the command it resolves to, so a retired command can never
    leave a dead or divergent dispatch entry."""
    from hermes_cli.commands import resolve_command

    for name in HermesCLI._SLASH_DISPATCH:
        resolved = resolve_command(name)
        assert resolved is not None, name
        assert HermesCLI._slash_handler(name) == HermesCLI._slash_handler(resolved.name), name


def test_registry_aliases_dispatch_through_the_canonical_handler():
    """Alias round-trip, observed through real dispatch: each alias of a
    dispatchable command resolves to that command, and alias text reaches the
    canonical handler (there is never an alias-named handler)."""
    from hermes_cli.commands import COMMAND_REGISTRY, resolve_command

    dispatchable = [c for c in COMMAND_REGISTRY if HermesCLI._slash_handler(c.name) is not None]
    assert dispatchable, "the registry must expose CLI-dispatchable commands"
    aliased = [c for c in dispatchable if c.aliases]
    assert aliased, "dispatchable commands must carry aliases"
    for c in aliased:
        canonical_method = HermesCLI._slash_handler(c.name)[0]
        for alias in c.aliases:
            resolved = resolve_command(alias)
            assert resolved is not None and resolved.name == c.name, alias
            assert HermesCLI._slash_handler(resolved.name)[0] == canonical_method, alias

    # End-to-end for one alias pair: /q is /queue's alias, and the original
    # text (alias + args) is exactly what the canonical handler receives.
    c = _cli()
    with patch.object(HermesCLI, "_cmd_queue") as queue_cmd:
        assert c.process_command("/q hello world") is True
    queue_cmd.assert_called_once_with("/q hello world")


def test_gateway_only_commands_acquire_no_cli_handler():
    """Dispatch-surface split as a contract: a gateway-only registry command
    never gains a CLI handler (no naming-convention collision), while every
    other registry command stays dispatchable from the CLI. A name outside the
    registry resolves to no core handler either way."""
    from hermes_cli.commands import COMMAND_REGISTRY

    for c in COMMAND_REGISTRY:
        if c.gateway_only:
            assert HermesCLI._slash_handler(c.name) is None, c.name
        else:
            assert HermesCLI._slash_handler(c.name) is not None, c.name
    assert HermesCLI._slash_handler("definitely-not-a-command") is None



def test_retired_commands_leave_no_dispatch_entries():
    from hermes_cli.commands import resolve_command

    for name in RETIRED_COMMANDS:
        assert resolve_command(name) is None, name
        assert name not in HermesCLI._SLASH_DISPATCH, name


def _cli():
    c = HermesCLI.__new__(HermesCLI)
    c._pending_resume_sessions = ["x"]
    c.session_id = "s1"
    c.config = {}
    return c


def test_dispatch_return_semantics_and_side_effects():
    c = _cli()
    with patch("hermes_cli.plugins.fire_pre_command_hook") as hook, \
            patch.object(HermesCLI, "_show_session_status", return_value=None):
        assert c.process_command("/status") is True
        hook.assert_called_once()
        assert hook.call_args.kwargs["command"] == "status"
    assert c._pending_resume_sessions is None  # non-resume command disarms it

    c = _cli()
    with patch.object(HermesCLI, "_handle_resume_command") as m:
        assert c.process_command("/resume 2") is True
        m.assert_called_once_with("/resume 2")
    assert c._pending_resume_sessions == ["x"]

    c = _cli()
    assert c.process_command("/exit") is False
    c = _cli()
    with patch.object(HermesCLI, "_handle_handoff_command", return_value=False):
        assert c.process_command("/handoff discord") is False
    with patch.object(HermesCLI, "_handle_handoff_command", return_value=True):
        assert c.process_command("/handoff discord") is True
    with patch.object(HermesCLI, "_handle_update_command", return_value=True):
        assert c.process_command("/update") is False
    with patch.object(HermesCLI, "_handle_update_command", return_value=False):
        assert c.process_command("/update") is True


def test_unknown_command_falls_through():
    c = _cli()
    c._console_print = MagicMock()
    with patch.object(HermesCLI, "_process_unregistered_slash", return_value=True) as m:
        assert c.process_command("/definitely-not-a-command x") is True
        m.assert_called_once_with("/definitely-not-a-command x", "/definitely-not-a-command x")

def test_pre_command_hook_sees_canonical_and_alias():
    """Pre-dispatch observer coverage: the hook fires once per dispatch with
    the canonical command and the alias actually typed."""
    c = _cli()
    with patch("hermes_cli.plugins.fire_pre_command_hook") as hook, \
            patch.object(HermesCLI, "_show_context_breakdown"):
        assert c.process_command("/ctx all") is True
    hook.assert_called_once()
    assert hook.call_args.kwargs["surface"] == "cli"
    assert hook.call_args.kwargs["command"] == "context"
    assert hook.call_args.kwargs["alias_used"] == "ctx"
