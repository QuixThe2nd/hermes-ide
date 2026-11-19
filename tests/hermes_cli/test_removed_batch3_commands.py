"""Removed batch-3 slash commands: /reset alias, /reasoning, /model, /personality, /fast."""


from hermes_cli import commands as cmd_mod

REMOVED = ["reasoning", "model", "personality", "fast"]


def test_removed_commands_do_not_resolve():
    for name in REMOVED:
        assert cmd_mod.resolve_command(name) is None, name


def test_reset_alias_removed_but_new_kept():
    new = cmd_mod.resolve_command("new")
    assert new is not None
    assert "reset" not in (new.aliases or ())


def test_removed_not_in_gateway_known_commands():
    for name in REMOVED + ["reset"]:
        assert name not in cmd_mod.GATEWAY_KNOWN_COMMANDS, name


def test_kept_commands_still_resolve():
    for name in [
        "new",
        "stop",
        "retry",
        "queue",
        "undo",
        "compress",
        "resume",
        "title",
        "status",
        "help",
        "reload-mcp",
        "restart",
    ]:
        assert cmd_mod.resolve_command(name) is not None, name
