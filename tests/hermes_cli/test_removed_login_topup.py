"""`/login` and `/topup` are removed from the chat slash surface.

The Nous sign-in (`hermes login`, `hermes auth`) and billing (`/subscription`,
portal) surfaces stay; only the chat slash commands are gone.
"""

from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, COMMAND_REGISTRY, resolve_command


def test_login_and_topup_do_not_resolve():
    assert resolve_command("login") is None
    assert resolve_command("topup") is None


def test_login_and_topup_absent_from_registry():
    assert not any(c.name in ("login", "topup") for c in COMMAND_REGISTRY)
    for c in COMMAND_REGISTRY:
        assert "login" not in (c.aliases or ())
        assert "topup" not in (c.aliases or ())


def test_login_and_topup_absent_from_gateway_known_commands():
    assert "login" not in GATEWAY_KNOWN_COMMANDS
    assert "topup" not in GATEWAY_KNOWN_COMMANDS


def test_core_session_commands_still_resolve():
    for name in ("new", "stop", "retry", "queue", "undo", "compress",
                 "restart", "status", "help"):
        assert resolve_command(name) is not None, name
