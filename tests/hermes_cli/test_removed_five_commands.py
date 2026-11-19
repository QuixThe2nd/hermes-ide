"""The /steer, /heartbeat, /curator, /bundles and /moa slash commands are removed."""
from hermes_cli import commands


REMOVED = ("steer", "heartbeat", "hb", "curator", "bundles", "moa")
KEPT = ("new", "stop", "retry", "queue", "undo", "compress", "restart")


def test_removed_commands_do_not_resolve():
    for name in REMOVED:
        assert commands.resolve_command(name) is None, name


def test_removed_commands_not_gateway_known():
    for name in REMOVED:
        assert name not in commands.GATEWAY_KNOWN_COMMANDS, name


def test_kept_commands_still_resolve():
    for name in KEPT:
        assert commands.resolve_command(name) is not None, name
