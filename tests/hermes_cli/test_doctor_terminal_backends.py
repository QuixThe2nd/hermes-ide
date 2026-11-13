"""Invariants for the terminal-backend doctor lines: each failure names the backend in
plain words and points at `hermes setup terminal`, never at raw TERMINAL_* env vars."""

import pytest

from hermes_cli import doctor_tools


@pytest.fixture
def issues():
    return []


def _joined(capsys) -> str:
    return capsys.readouterr().out


def test_ssh_host_missing_line_points_at_setup_terminal(monkeypatch, capsys, issues):
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)
    doctor_tools._check_ssh_backend(issues)
    out = _joined(capsys)
    assert "SSH host not configured" in out and "'ssh' terminal backend" in out
    assert "TERMINAL_SSH_HOST" not in out
    assert issues and "hermes setup terminal" in issues[0] and "SSH host and user" in issues[0]
