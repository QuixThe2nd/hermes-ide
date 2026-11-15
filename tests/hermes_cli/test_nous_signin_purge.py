"""Contract: Nous account sign-in, guest upgrade, and portal commands are gone."""

import subprocess
import sys


_REMOVED_COMMANDS = {"login", "topup", "subscription", "upgrade"}
_REMOVED_MODULES = {
    "agent.nous_rate_guard",
    "agent.nous_wire",
    "agent.portal_tags",
    "hermes_cli.anon_auth",
    "hermes_cli.anon_sign_in",
    "hermes_cli.anon_sign_in_cli",
    "hermes_cli.auth_nous",
    "hermes_cli.cli_billing_mixin",
    "hermes_cli.nous_account",
    "hermes_cli.nous_auth_keepalive",
    "hermes_cli.nous_tools",
    "hermes_cli.portal_cli",
    "hermes_cli.proxy.adapters.nous_portal",
    "hermes_cli.subcommands.login",
}


def test_nous_account_commands_are_not_registered():
    from hermes_cli.commands import COMMAND_REGISTRY, resolve_command

    assert all(resolve_command(name) is None for name in _REMOVED_COMMANDS)
    assert all(
        not _REMOVED_COMMANDS.intersection({command.name, *(command.aliases or ())})
        for command in COMMAND_REGISTRY
    )


def test_importing_cli_main_does_not_import_nous_account_modules():
    code = """
import sys
import hermes_cli.main as main
removed = %r
imported = removed.intersection(sys.modules)
assert not imported, f"deleted Nous account modules imported: {sorted(imported)}"
assert not {"login", "portal"}.intersection(main._BUILTIN_SUBCOMMANDS)
""" % _REMOVED_MODULES
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
