"""OAuth resolver headers merge through the same path as live proxy forwarding."""

from __future__ import annotations

import time

from plugins.llm_usage_proxy.server import (
    OAuthCredentialProvider,
    KeyStore,
    apply_oauth_headers,
    apply_upstream_auth,
)


def test_oauth_credential_provider_merges_headers_and_caches(tmp_path):
    keys_path = str(tmp_path / "keys.json")
    store = KeyStore(keys_path)
    store.set_oauth_route("anthropic", "anthropic")

    calls = {"n": 0}

    def fake_helper(provider: str) -> dict:
        calls["n"] += 1
        assert provider == "anthropic"
        return {
            "access_token": "tok-1",
            "expires_at": time.time() + 3600,
            "headers": {
                "anthropic-beta": "oauth-2025-04-20",
                "authorization": "Bearer evil",
                "x-other": "keep",
            },
        }

    oauth = OAuthCredentialProvider(store, helper=fake_helper)
    token, extra = oauth.credential_for("anthropic")
    assert calls["n"] == 1
    assert token == "tok-1"
    assert extra["anthropic-beta"] == "oauth-2025-04-20"
    assert extra["x-other"] == "keep"

    token2, extra2 = oauth.credential_for("anthropic")
    assert calls["n"] == 1
    assert token2 == token
    assert extra2 == extra

    oauth.invalidate("anthropic")
    oauth.credential_for("anthropic")
    assert calls["n"] == 2

    client_headers = [
        ("Authorization", "Bearer client-token"),
        ("anthropic-beta", "old-beta"),
        ("x-other", "client-keep"),
    ]
    merged = apply_oauth_headers(apply_upstream_auth(client_headers, token), extra)
    merged_map = {name.lower(): value for name, value in merged}
    assert merged_map["authorization"] == "Bearer tok-1"
    assert merged_map["anthropic-beta"] == "oauth-2025-04-20"
    assert merged_map["x-other"] == "keep"
