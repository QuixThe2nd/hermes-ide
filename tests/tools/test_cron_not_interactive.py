"""Unattended approval contexts never resolve as interactive (#110932).

A gateway sets HERMES_EXEC_ASK=1 at startup and hands its environ to every external cron
worker; interactive launches export HERMES_INTERACTIVE=1. Inside cron (or a programmatic
platform session) nobody can answer the card, so ``_presence()`` must clear the trio and let
the gate resolve from ``approvals.cron_mode`` / ``approvals.unattended_mode``.
"""

import pytest

from tools import approval as approval_mod


@pytest.fixture
def leaked_presence(monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)


@pytest.mark.parametrize(
    "unattended_env",
    [{"HERMES_CRON_SESSION": "1"}, {"HERMES_SESSION_PLATFORM": "webhook"}],
    ids=["cron", "webhook"],
)
def test_unattended_context_clears_leaked_presence(monkeypatch, leaked_presence, unattended_env):
    for key, value in unattended_env.items():
        monkeypatch.setenv(key, value)
    _, is_cli, is_gateway, is_ask = approval_mod._presence()
    assert (is_cli, is_gateway, is_ask) == (False, False, False)


def test_interactive_session_keeps_presence(monkeypatch, leaked_presence):
    _, is_cli, is_gateway, is_ask = approval_mod._presence()
    assert (is_cli, is_gateway, is_ask) == (True, True, True)
