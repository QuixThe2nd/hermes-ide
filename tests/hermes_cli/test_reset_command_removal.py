"""Behavioral contract for removing the /reset slash-command alias."""

from hermes_cli.commands import COMMANDS, GATEWAY_KNOWN_COMMANDS, resolve_command


def test_reset_alias_is_removed_and_new_still_interrupts():
    assert resolve_command("reset") is None
    assert resolve_command("/reset") is None
    assert "/reset" not in COMMANDS
    assert "reset" not in GATEWAY_KNOWN_COMMANDS

    new_command = resolve_command("new")
    assert new_command is not None
    assert new_command.name == "new"
    assert new_command.busy_policy == "interrupt_then_dispatch"
