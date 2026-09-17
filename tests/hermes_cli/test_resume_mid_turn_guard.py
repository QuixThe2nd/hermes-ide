"""regression tests for refusing /resume during an active agent turn."""

import sys
from types import SimpleNamespace

from hermes_cli import cli_commands_mixin


def _make_cli(agent_running: bool):
    cli = object.__new__(cli_commands_mixin.CLICommandsMixin)
    cli._agent_running = agent_running
    cli._session_db = None
    cli._pending_resume_sessions = None
    return cli


def test_resume_refuses_mid_turn(monkeypatch):
    printed = []
    monkeypatch.setattr(cli_commands_mixin, "_cp", lambda *lines: printed.extend(lines))
    cli = _make_cli(agent_running=True)

    cli._handle_resume_command("/resume previous-session")

    assert printed == [
        "  Agent is busy. Wait for the current turn to finish, then retry /resume."
    ]


def test_resume_keeps_existing_idle_behavior(monkeypatch):
    printed = []
    monkeypatch.setattr(cli_commands_mixin, "_cp", lambda *lines: printed.extend(lines))
    monkeypatch.setitem(sys.modules, "cli", SimpleNamespace(_sync_process_session_id=lambda *a, **k: None))
    cli = _make_cli(agent_running=False)

    cli._handle_resume_command("/resume previous-session")

    assert printed
    assert "busy" not in " ".join(printed).lower()
