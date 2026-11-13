"""Multiplex invariant: tool-side profile credentials / targets never leak from the default profile.

Under ``gateway.multiplex_profiles`` ``os.environ`` holds the DEFAULT profile's ``.env``; a secondary
profile's turn runs with a secret scope that may not define a var at all. Every reader below must
then see "unset", never the default profile's value (`agent/secret_scope.py::get_secret` contract).
"""
from __future__ import annotations

import pytest

from agent import secret_scope


@pytest.fixture
def secondary_scope(monkeypatch):
    """Multiplex ON with a secondary profile's (empty) secret scope installed."""
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({})
    try:
        yield
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)


def test_scoped_tool_credential_gates_ignore_default_profile_environ(monkeypatch, secondary_scope, tmp_path):
    from tools import browser_camofox, read_extract

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-default")
    monkeypatch.setenv("CAMOFOX_URL", "http://default-camofox:9377")
    monkeypatch.setenv("CAMOFOX_API_KEY", "camofox-default")

    enabled, api_key, _ = read_extract._hosted_ocr_config()
    assert (enabled, api_key) == (False, None)
    assert browser_camofox.get_camofox_url() == ""
    assert browser_camofox._auth_headers() == {}


