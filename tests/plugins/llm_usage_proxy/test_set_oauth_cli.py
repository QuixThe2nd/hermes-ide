"""CLI ``keys set-oauth`` accepts providers from ``OAUTH_PROVIDERS``."""

from __future__ import annotations

import argparse

import pytest

from plugins.llm_usage_proxy.cli import cmd_keys
from plugins.llm_usage_proxy.server import KeyStore


def test_set_oauth_anthropic_route(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    code = cmd_keys(
        argparse.Namespace(
            keys_command="set-oauth",
            route="anthropic",
            provider="anthropic",
            index=None,
        )
    )
    assert code == 0
    assert KeyStore().oauth_provider_for("anthropic") == "anthropic"


def test_set_oauth_rejects_unknown_provider(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    code = cmd_keys(
        argparse.Namespace(
            keys_command="set-oauth",
            route="anthropic",
            provider="bogus",
            index=None,
        )
    )
    assert code == 2
    assert KeyStore().oauth_provider_for("anthropic") is None
