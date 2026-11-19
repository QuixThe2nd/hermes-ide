"""Invariant: an adapter's endpoint/identity resolves through the SAME profile scope as its secret.

Under ``gateway.multiplex_profiles`` ``os.environ`` holds the DEFAULT profile's values; a secondary
profile's ``.env`` exists only in its secret scope. Every adapter here already read its *secret*
scope-aware but read the paired homeserver / URL / client_id / from-number / webhook URL raw from
``os.environ`` — so the secondary's credential was sent to the default profile's host or identity.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import secret_scope as ss
from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

DEFAULT_ENV = {
    "MATRIX_HOMESERVER": "https://default.matrix.example", "MATRIX_USER_ID": "@default:example",
    "MATRIX_DEVICE_ID": "DEFAULTDEV", "MATRIX_ACCESS_TOKEN": "default-matrix-token",
}
SECONDARY = {
    "MATRIX_HOMESERVER": "https://bot2.matrix.example", "MATRIX_USER_ID": "@bot2:example",
    "MATRIX_DEVICE_ID": "BOT2DEV", "MATRIX_ACCESS_TOKEN": "bot2-matrix-token",
}


@pytest.fixture
def secondary_scope(monkeypatch):
    """Default profile in os.environ, multiplex on, secondary profile's scope installed."""
    for key, value in DEFAULT_ENV.items():
        monkeypatch.setenv(key, value)
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(SECONDARY)
    yield
    ss.reset_secret_scope(token)
    ss.set_multiplex_active(False)


def test_matrix_homeserver_identity_follow_the_scoped_token(secondary_scope):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(PlatformConfig(enabled=True))
    assert adapter._access_token == SECONDARY["MATRIX_ACCESS_TOKEN"]
    assert (adapter._homeserver, adapter._user_id, adapter._device_id) == (
        SECONDARY["MATRIX_HOMESERVER"], SECONDARY["MATRIX_USER_ID"], SECONDARY["MATRIX_DEVICE_ID"])


@pytest.mark.asyncio
async def test_matrix_standalone_send_posts_scoped_token_to_scoped_homeserver(secondary_scope, monkeypatch):
    import aiohttp
    from plugins.platforms.matrix import adapter as mx

    seen = {}

    class _Resp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def json(self): return {"event_id": "$x"}
        async def text(self): return ""

    class _Sess:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def put(self, url, **kw): seen["url"] = url; seen["headers"] = kw.get("headers", {}); return _Resp()

    monkeypatch.setattr(aiohttp, "ClientSession", _Sess)
    await mx._standalone_send(PlatformConfig(enabled=True), "!room:example", "hi")
    assert seen["url"].startswith(SECONDARY["MATRIX_HOMESERVER"] + "/")
    assert DEFAULT_ENV["MATRIX_HOMESERVER"] not in seen["url"]
