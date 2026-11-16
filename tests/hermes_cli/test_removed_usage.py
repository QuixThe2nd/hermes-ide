"""The /usage slash command and `hermes usage` subcommand are gone (removed
from the product surface; token accounting itself lives on in
agent/account_usage.py, /status and API usage payloads)."""

from hermes_cli.commands import (
    COMMANDS,
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    resolve_command,
)


def test_usage_no_longer_resolves():
    assert resolve_command("usage") is None


def test_usage_absent_from_registries():
    names = {cmd.name for cmd in COMMAND_REGISTRY}
    assert "usage" not in names
    assert "usage" not in COMMANDS
    assert "usage" not in GATEWAY_KNOWN_COMMANDS


def test_cli_parser_has_no_usage_subcommand():
    import hermes_cli.main as main_mod

    parser = main_mod._build_cli_parser()[0]
    subparsers = next(
        a for a in parser._actions
        if isinstance(a.choices, dict) and a.dest in ("command", "cmd")
    )
    assert "usage" not in subparsers.choices
    # The builtin set drives argv pre-scanning; it must not keep the entry either.
    assert "usage" not in main_mod._BUILTIN_SUBCOMMANDS


def test_other_commands_still_resolve():
    assert resolve_command("new") is not None
    assert resolve_command("status") is not None
