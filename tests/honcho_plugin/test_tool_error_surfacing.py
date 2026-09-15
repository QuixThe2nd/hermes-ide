"""Backend failures must be distinguishable from genuine empty results (#36098)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho.client import HonchoClientConfig
from plugins.memory.honcho.session import HonchoSession, HonchoSessionManager


def _make_provider(**cfg_overrides) -> HonchoMemoryProvider:
    provider = HonchoMemoryProvider()
    provider._manager = MagicMock()
    provider._session_key = "agent:main:test"
    provider._session_initialized = True
    provider._cron_skipped = False

    cfg = MagicMock()
    cfg.user_observe_me = cfg_overrides.get("user_observe_me", True)
    cfg.user_observe_others = cfg_overrides.get("user_observe_others", True)
    cfg.ai_observe_me = cfg_overrides.get("ai_observe_me", True)
    cfg.ai_observe_others = cfg_overrides.get("ai_observe_others", True)
    cfg.message_max_chars = 25000
    provider._config = cfg

    provider._dialectic_cadence = cfg_overrides.get("dialectic_cadence", 1)
    provider._turn_count = cfg_overrides.get("turn_count", 5)
    return provider


def _make_manager(peer) -> HonchoSessionManager:
    cfg = HonchoClientConfig(host="hermes", api_key="hch-at-x", enabled=True)
    mgr = HonchoSessionManager(config=cfg)
    session = HonchoSession(
        key="k", user_peer_id="u", assistant_peer_id="a", honcho_session_id="s"
    )
    mgr._cache["k"] = session
    mgr._sessions_cache["s"] = MagicMock()
    mgr._get_or_create_peer = lambda peer_id: peer
    mgr._force_reauth = lambda: True
    return mgr


class TestToolBackendErrorsDistinctFromEmpty:
    """Explicit tool calls surface backend failures as tool_error, not empty strings."""

    def test_honcho_search_backend_error_is_not_empty_result(self):
        provider = _make_provider()
        provider._manager.search_context.side_effect = ConnectionError("HTTP 500 from deriver")

        raw = provider.handle_tool_call("honcho_search", {"query": "project status"})
        payload = json.loads(raw)

        assert "error" in payload
        assert "HTTP 500 from deriver" in payload["error"]
        assert "backend error" in payload["error"].lower()
        assert payload["error"] != "No relevant context found."

    def test_honcho_search_genuine_empty_keeps_legacy_string(self):
        provider = _make_provider()
        provider._manager.search_context.return_value = ""

        raw = provider.handle_tool_call("honcho_search", {"query": "project status"})
        payload = json.loads(raw)

        assert payload == {"result": "No relevant context found."}
        assert "error" not in payload

    def test_honcho_profile_backend_error_is_not_empty_hint(self):
        provider = _make_provider()
        provider._manager.get_peer_card.side_effect = ConnectionError("connection refused")

        raw = provider.handle_tool_call("honcho_profile", {})
        payload = json.loads(raw)

        assert "error" in payload
        assert "connection refused" in payload["error"]
        assert "backend error" in payload["error"].lower()
        assert payload.get("result") != "No profile facts available yet."

    def test_honcho_profile_genuine_empty_keeps_legacy_hint(self):
        provider = _make_provider()
        provider._manager.get_peer_card.return_value = []

        raw = provider.handle_tool_call("honcho_profile", {})
        payload = json.loads(raw)

        assert payload["result"] == "No profile facts available yet."
        assert "hint" in payload
        assert "error" not in payload

    def test_honcho_context_backend_error_is_not_empty_result(self):
        provider = _make_provider()
        provider._manager.get_session_context.side_effect = ConnectionError("server error 503")

        raw = provider.handle_tool_call("honcho_context", {})
        payload = json.loads(raw)

        assert "error" in payload
        assert "server error 503" in payload["error"]
        assert "backend error" in payload["error"].lower()
        assert payload.get("result") != "No context available yet."

    def test_honcho_context_genuine_empty_keeps_legacy_string(self):
        provider = _make_provider()
        provider._manager.get_session_context.return_value = {}

        raw = provider.handle_tool_call("honcho_context", {})
        payload = json.loads(raw)

        assert payload == {"result": "No context available yet."}
        assert "error" not in payload


class TestManagerRaiseErrorsDefaultFalse:
    """Auto-injection and prefetch callers keep fail-quiet behavior."""

    def test_search_context_default_swallows_backend_failure(self, monkeypatch):
        from plugins.memory.honcho import session as session_mod

        client = MagicMock()
        client.search.side_effect = ConnectionError("HTTP 500")
        peer = MagicMock()
        peer.search.side_effect = ConnectionError("HTTP 500")
        mgr = _make_manager(peer)
        monkeypatch.setattr(session_mod, "get_honcho_client", lambda *a, **k: client)
        with patch.object(
            HonchoSessionManager, "honcho", new_callable=lambda: property(lambda s: client)
        ):
            assert mgr.search_context("k", "query") == ""

    def test_get_peer_card_default_swallows_backend_failure(self):
        class _BrokenPeer:
            def get_card(self, **kw):
                raise ConnectionError("HTTP 500")

        mgr = _make_manager(_BrokenPeer())
        assert mgr.get_peer_card("k") == []

    def test_get_session_context_default_swallows_backend_failure(self):
        class _BrokenPeer:
            def context(self, **kw):
                raise ConnectionError("HTTP 500")

            def representation(self, **kw):
                raise ConnectionError("HTTP 500")

            def get_card(self, **kw):
                raise ConnectionError("HTTP 500")

        mgr = _make_manager(_BrokenPeer())
        mgr._sessions_cache.clear()  # fallback path via _fetch_peer_context
        assert mgr.get_session_context("k") == {"representation": "", "card": []}

    def test_prefetch_context_path_swallows_backend_failure(self):
        class _BrokenPeer:
            def context(self, **kw):
                raise ConnectionError("HTTP 500")

            def representation(self, **kw):
                raise ConnectionError("HTTP 500")

            def get_card(self, **kw):
                raise ConnectionError("HTTP 500")

        mgr = _make_manager(_BrokenPeer())
        mgr._sessions_cache.clear()
        assert mgr.get_prefetch_context("k") == {
            "representation": "",
            "card": "",
            "ai_representation": "",
            "ai_card": "",
        }


class TestManagerRaiseErrorsTrue:
    """Explicit tool callers can opt into propagated backend failures."""

    def test_search_context_raises_when_both_paths_fail(self, monkeypatch):
        from plugins.memory.honcho import session as session_mod

        client = MagicMock()
        client.search.side_effect = ConnectionError("message search down")
        peer = MagicMock()
        peer.search.side_effect = ConnectionError("peer search down")
        mgr = _make_manager(peer)
        monkeypatch.setattr(session_mod, "get_honcho_client", lambda *a, **k: client)
        with patch.object(
            HonchoSessionManager, "honcho", new_callable=lambda: property(lambda s: client)
        ):
            with pytest.raises(ConnectionError, match="peer search down"):
                mgr.search_context("k", "query", raise_errors=True)

    def test_get_peer_card_raises_on_backend_failure(self):
        class _BrokenPeer:
            def get_card(self, **kw):
                raise ConnectionError("card fetch failed")

        mgr = _make_manager(_BrokenPeer())
        with pytest.raises(ConnectionError, match="card fetch failed"):
            mgr.get_peer_card("k", raise_errors=True)

    def test_get_session_context_raises_on_backend_failure(self):
        class _BrokenPeer:
            def context(self, **kw):
                raise ConnectionError("context fetch failed")

        mgr = _make_manager(_BrokenPeer())
        mgr._sessions_cache.clear()  # fallback path via _fetch_peer_context
        with pytest.raises(ConnectionError, match="context fetch failed"):
            mgr.get_session_context("k", raise_errors=True)


class TestWritePathErrorSurfacing:
    """set_peer_card failures are labeled as failed writes, not failed reads."""

    def test_card_update_backend_error_is_labeled_as_update(self):
        provider = _make_provider()
        provider._manager.set_peer_card.side_effect = ConnectionError("write 503")

        raw = provider.handle_tool_call("honcho_profile", {"card": ["fact a"]})
        payload = json.loads(raw)

        assert "error" in payload
        assert "write 503" in payload["error"]
        assert "update" in payload["error"].lower()
        assert "backend error" in payload["error"].lower()
        assert "profile fetch failed" not in payload["error"]
        assert payload["error"] != "Failed to update peer card."

    def test_card_update_unconfirmed_keeps_legacy_string(self):
        provider = _make_provider()
        provider._manager.set_peer_card.return_value = None

        raw = provider.handle_tool_call("honcho_profile", {"card": ["fact a"]})
        payload = json.loads(raw)

        assert payload["error"] == "Failed to update peer card."


class TestEnrichmentFailureStaysEmpty:
    """Once the primary peer.context() call answers, enrichment failures must
    not flip a genuine empty snapshot into a backend error."""

    def test_enrichment_failure_after_successful_empty_primary(self):
        class _EmptyPrimaryPeer:
            def context(self, **kw):
                ctx = MagicMock()
                ctx.representation = ""
                ctx.peer_representation = ""
                ctx.peer_card = []
                return ctx

            def representation(self, **kw):
                raise ConnectionError("enrichment down")

            def get_card(self, **kw):
                raise ConnectionError("enrichment down")

        mgr = _make_manager(_EmptyPrimaryPeer())
        mgr._sessions_cache.clear()  # force the _fetch_peer_context path
        result = mgr.get_session_context("k", raise_errors=True)
        assert result == {"representation": "", "card": []}
