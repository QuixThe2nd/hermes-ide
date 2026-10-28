"""Wire contract for ``hermes_cli.llm_proxy_oauth`` OAuth helper subprocess."""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import llm_proxy_oauth


def test_main_anthropic_success(capsys, monkeypatch):
    expires_ms = int(time.time() * 1000) + 3_600_000

    def fake_resolve(**kwargs):
        return {
            "provider": "anthropic",
            "api_key": "sk-ant-oat01-test",
            "expires_at_ms": expires_ms,
            "base_url": "https://api.anthropic.com",
            "source": "manual:hermes_pkce",
            "auth_mode": "oauth_pkce",
        }

    monkeypatch.setattr(
        "hermes_cli.auth_anthropic.resolve_anthropic_oauth_runtime_credentials",
        fake_resolve,
    )
    code = llm_proxy_oauth.main(["anthropic"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["access_token"] == "sk-ant-oat01-test"
    assert payload["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert payload["expires_at"] == pytest.approx(expires_ms / 1000.0, rel=1e-3)


def test_main_anthropic_missing_expiry_ms_uses_zero(capsys, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.auth_anthropic.resolve_anthropic_oauth_runtime_credentials",
        lambda **kw: {
            "provider": "anthropic",
            "api_key": "sk-ant-oat01-test",
            "expires_at_ms": None,
        },
    )
    code = llm_proxy_oauth.main(["anthropic"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["expires_at"] == 0


def test_main_unknown_provider_lists_all_three(capsys):
    code = llm_proxy_oauth.main(["bogus"])
    err = json.loads(capsys.readouterr().out)
    assert code == 1
    message = err["error"]
    for name in llm_proxy_oauth.OAUTH_PROVIDERS:
        assert name in message


def test_main_codex_contract_unchanged(capsys, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_codex_runtime_credentials",
        lambda: {"api_key": "codex-access-token"},
    )
    monkeypatch.setattr(
        "agent.codex_headers.codex_account_headers",
        lambda token: {"ChatGPT-Account-ID": "acct-1", "OpenAI-Organization": "org-1"},
    )
    code = llm_proxy_oauth.main(["openai-codex"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["access_token"] == "codex-access-token"
    assert payload["headers"] == {
        "ChatGPT-Account-ID": "acct-1",
        "OpenAI-Organization": "org-1",
    }
    assert payload["expires_at"] is None


def test_main_xai_contract_unchanged(capsys, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_xai_oauth_runtime_credentials",
        lambda: {"api_key": "xai-access-token"},
    )
    code = llm_proxy_oauth.main(["xai-oauth"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["access_token"] == "xai-access-token"
    assert payload["headers"] == {}
