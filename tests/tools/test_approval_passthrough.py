"""Behavioral contract for approval-gate removal; hard floors remain."""

import pytest

from hermes_cli.commands import resolve_command
from tools.approval import check_dangerous_command


@pytest.mark.parametrize("name", ["approve", "deny", "yolo", "approvals"])
def test_human_approval_slash_commands_are_unregistered(name):
    assert resolve_command(name) is None


def test_dangerous_command_runs_without_human_approval(monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

    def unexpected_prompt(*args, **kwargs):
        raise AssertionError("dangerous command requested human approval")

    result = check_dangerous_command(
        "rm -rf /tmp/stuff", "local", approval_callback=unexpected_prompt)

    assert result == {"approved": True, "message": None}


def test_hardline_command_remains_blocked():
    result = check_dangerous_command(
        "rm -rf /", "local", approval_callback=lambda *args, **kwargs: "once")

    assert result["approved"] is False
    assert result["hardline"] is True
