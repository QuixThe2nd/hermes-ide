"""The /footer, /busy and /voice slash commands are removed, along with the
voice/TTS module surface and the footer/busy-mode configurability."""
import importlib.util
from pathlib import Path

from hermes_cli import commands

_REPO_ROOT = str(Path(__file__).resolve().parent.parent) + "/"

REMOVED_COMMANDS = ("footer", "busy", "voice")
REMOVED_MODULES = ("tools.tts_tool", "tools.voice_mode", "gateway.run_voice")
KEPT = ("new", "stop", "retry", "queue", "undo", "compress", "restart", "status", "help")


def _find_spec_local(mod):
    """find_spec restricted to THIS checkout: a developer venv carrying an editable
    hermes_agent install (meta-path finder or sys.path entry pointing at another
    workspace) resolves missing submodules there, which would mask the deletion
    this test asserts."""
    spec = importlib.util.find_spec(mod)
    if spec is None or not spec.origin:
        return None
    return spec if str(spec.origin).startswith(_REPO_ROOT) else None


def test_removed_commands_do_not_resolve():
    for name in REMOVED_COMMANDS:
        assert commands.resolve_command(name) is None, name


def test_removed_commands_not_gateway_known():
    for name in REMOVED_COMMANDS:
        assert name not in commands.GATEWAY_KNOWN_COMMANDS, name


def test_removed_modules_are_gone():
    for mod in REMOVED_MODULES:
        assert _find_spec_local(mod) is None, mod


def test_kept_commands_still_resolve():
    for name in KEPT:
        assert commands.resolve_command(name) is not None, name
