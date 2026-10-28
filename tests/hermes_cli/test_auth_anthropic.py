"""Runtime resolver for Anthropic OAuth credentials (LLM proxy helper)."""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli.auth_constants import AuthError
from hermes_cli.auth_anthropic import resolve_anthropic_oauth_runtime_credentials


def _write_auth(path, store):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store), encoding="utf-8")


def test_resolver_returns_non_expiring_pool_row(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda pid: True)
    monkeypatch.setattr(
        "agent.anthropic_credentials.refresh_anthropic_oauth_pure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refresh must not run")),
    )
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)

    far_future = int(time.time() * 1000) + 86_400_000
    _write_auth(
        hermes_home / "auth.json",
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "manual-1",
                        "label": "cred",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:hermes_pkce",
                        "access_token": "sk-ant-oat01-x",
                        "refresh_token": "rt-x",
                        "expires_at_ms": far_future,
                    }
                ]
            },
        },
    )

    creds = resolve_anthropic_oauth_runtime_credentials()
    assert creds["provider"] == "anthropic"
    assert creds["api_key"] == "sk-ant-oat01-x"
    assert creds["base_url"] == "https://api.anthropic.com"
    assert creds["auth_mode"] == "oauth_pkce"


def test_resolver_seeds_from_singleton(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda pid: True)
    monkeypatch.setattr(
        "agent.anthropic_credentials.refresh_anthropic_oauth_pure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refresh must not run")),
    )
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)

    far_future = int(time.time() * 1000) + 86_400_000
    (hermes_home / ".anthropic_oauth.json").write_text(
        json.dumps(
            {
                "accessToken": "sk-ant-oat-singleton",
                "refreshToken": "rt-singleton",
                "expiresAt": far_future,
            }
        ),
        encoding="utf-8",
    )
    _write_auth(hermes_home / "auth.json", {"version": 1, "providers": {}})

    creds = resolve_anthropic_oauth_runtime_credentials()
    assert creds["api_key"] == "sk-ant-oat-singleton"


def test_resolver_refreshes_expiring_singleton_and_writes_back(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda pid: True)
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(
        "agent.anthropic_credentials.refresh_anthropic_oauth_pure",
        lambda refresh_token, use_json=False: {
            "access_token": "sk-ant-oat-rotated",
            "refresh_token": "rt-rotated",
            "expires_at_ms": int(time.time() * 1000) + 3_600_000,
        },
    )

    oauth_file = hermes_home / ".anthropic_oauth.json"
    oauth_file.write_text(
        json.dumps(
            {"accessToken": "sk-ant-oat-stale", "refreshToken": "rt-stale", "expiresAt": 0}
        ),
        encoding="utf-8",
    )
    _write_auth(hermes_home / "auth.json", {"version": 1, "providers": {}})

    creds = resolve_anthropic_oauth_runtime_credentials()
    assert creds["api_key"] == "sk-ant-oat-rotated"
    on_disk = json.loads(oauth_file.read_text(encoding="utf-8"))
    assert on_disk["refreshToken"] == "rt-rotated"
    assert on_disk["accessToken"] == "sk-ant-oat-rotated"


def test_resolver_missing_credentials_raises_auth_error(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda pid: True)
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    _write_auth(hermes_home / "auth.json", {"version": 1, "providers": {}})

    with pytest.raises(AuthError) as exc:
        resolve_anthropic_oauth_runtime_credentials()
    assert "hermes auth add anthropic" in str(exc.value)
