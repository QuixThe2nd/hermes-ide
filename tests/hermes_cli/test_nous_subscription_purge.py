"""Contract: the Nous plan /subscription surface is gone.

The /subscription (alias /upgrade) slash command, the plan/subscription
modules, and their setup prompts were removed. The Nous Tool Gateway
itself survives (``hermes_cli.nous_tools``); these tests pin that no
slash-command or import path resurrects the subscription surface.
"""

import subprocess
import sys


def test_subscription_slash_commands_unresolvable():
    from hermes_cli.commands import resolve_command

    assert resolve_command("subscription") is None
    assert resolve_command("upgrade") is None


def test_importing_cli_main_does_not_import_subscription_modules():
    # Fresh interpreter: assert the module graph reached by hermes_cli.main
    # never pulls in the deleted modules (a stale lazy import would fail
    # here with ModuleNotFoundError, and a resurrected one with the assert).
    code = (
        "import sys; import hermes_cli.main; "
        "assert 'hermes_cli.nous_subscription' not in sys.modules, 'nous_subscription imported'; "
        "assert 'agent.subscription_view' not in sys.modules, 'subscription_view imported'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
