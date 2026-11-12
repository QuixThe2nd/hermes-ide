"""Camofox contract tests for the vault browser tools.

Camofox is the sole transport. These contracts cover:

- ``_eval_js`` / ``_eval_js_secret`` dispatch to the Camofox REST evaluate endpoint
  (``POST /tabs/{tabId}/evaluate``), reusing the session tab machinery browser_console uses.
- Secret-bearing expressions fail closed when the Camofox key or session tab is
  missing — no weaker fallback, no subprocess.
- Origin binding still refuses mismatched origins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools import browser_vault_tool  # noqa: E402
from tools import browser_camofox  # noqa: E402


@pytest.fixture()
def camofox_mode(monkeypatch):
    """Server URL set and session tab open for 'default'."""
    monkeypatch.setattr(browser_camofox, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(browser_camofox, "get_camofox_url", lambda: "http://camofox.lan:9377")
    monkeypatch.setattr(browser_camofox, "_get_session",
                        lambda task_id: {"user_id": "u1", "tab_id": "tab-9", "session_key": "s"})
    return monkeypatch


class TestEvalJsCamofoxDispatch:
    def test_eval_js_routes_through_camofox_evaluate(self, camofox_mode):
        posts = []

        def fake_post(path, body, timeout=None):
            posts.append((path, body))
            return {"result": "https://www.facebook.com/login"}

        with patch.object(browser_camofox, "_post", side_effect=fake_post):
            out = browser_vault_tool._eval_js("default", "window.location.href")
        assert out["success"] is True
        assert out["result"] == "https://www.facebook.com/login"
        assert posts == [("/tabs/tab-9/evaluate",
                          {"expression": "window.location.href", "userId": "u1"})]

    def test_eval_js_secret_routes_through_camofox(self, camofox_mode):
        with patch("agent.secret_scope.get_secret", return_value="test-key"), \
             patch.object(browser_camofox, "_post", return_value={"result": '{"filled": 1}'}) as post:
            out = browser_vault_tool._eval_js_secret("default", "fill('s3cret')")
        assert out["success"] is True
        assert out["result"] == {"filled": 1}  # JSON-string results are parsed
        post.assert_called_once_with("/tabs/tab-9/evaluate", {"expression": "fill('s3cret')", "userId": "u1"})

    def test_current_page_origin_resolves_via_camofox(self, camofox_mode):
        with patch.object(browser_camofox, "_post", return_value={"result": "https://www.facebook.com/login"}):
            assert browser_vault_tool._current_page_origin("default") == "https://www.facebook.com"


class TestCamofoxFailClosed:
    def test_secret_eval_fails_closed_without_key(self, camofox_mode):
        with patch("agent.secret_scope.get_secret", return_value=""), \
             patch.object(browser_camofox, "_post") as post:
            out = browser_vault_tool._eval_js_secret("default", "fill('s3cret')")
        assert out["success"] is False
        assert out["error_type"] == "camofox_session_required"
        post.assert_not_called()

    def test_secret_eval_fails_closed_without_tab(self, camofox_mode):
        with patch("agent.secret_scope.get_secret", return_value="k"), \
             patch.object(browser_camofox, "_get_session", return_value={"user_id": "u1", "tab_id": None}), \
             patch.object(browser_camofox, "_post") as post:
            out = browser_vault_tool._eval_js_secret("default", "fill('s3cret')")
        assert out["success"] is False
        assert out["error_type"] == "camofox_session_required"
        post.assert_not_called()

    def test_transport_failure_is_returned(self, camofox_mode):
        with patch.object(browser_camofox, "_post", side_effect=RuntimeError("conn refused")) as post:
            out = browser_vault_tool._eval_js("default", "window.location.href")
        assert out["success"] is False
        post.assert_called_once()


class TestOriginBindingOnCamofox:
    def test_fill_refused_on_origin_mismatch(self, camofox_mode):
        from agent.vault_store import VaultItemMeta

        meta = VaultItemMeta(
            id="vault_x", kind="login", label="FB", origin="https://facebook.com",
            created_at="2026-01-01T00:00:00Z", identifier_type="email", identifier="u@x.com",
            allowed_origins=("https://facebook.com",))

        class _Backend:
            name, display_name, needs_unlock = "local", "Local vault", False

            def is_unlocked(self):
                return True

            def get_meta(self, handle):
                return meta

            def resolve_password(self, handle):
                return "s3cret-pw"

        with patch("agent.vault_backends.backend_for_handle", return_value=_Backend()), \
             patch.object(browser_camofox, "_post", return_value={"result": "https://evil.example/login"}):
            out = json.loads(browser_vault_tool.browser_vault_fill("vault_x", task_id="default"))
        assert out["success"] is False
        assert out["error_type"] == "origin_mismatch"
        assert "s3cret-pw" not in json.dumps(out)


