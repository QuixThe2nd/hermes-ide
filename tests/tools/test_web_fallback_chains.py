"""Tests for configurable web search/extract fallback chains.

Covers:
- _get_fallback_backends() — config parsing (list, comma/space string,
  bracketed string, dedup, primary removal, empty/whitespace handling)
- _run_search_fallbacks() — primary fails → first healthy fallback wins;
  all fail → primary error surfaced; empty chain → primary result unchanged;
  unavailable / unregistered / search-incapable candidates skipped
- web_search_tool() end-to-end — brave-free 402 → exa fallback returns results
- _run_extract_fallbacks() — primary raises → fallback result returned;
  all raise → primary exception re-raised; only non-timeout, non-5xx per-URL
  errors stay put (transient ones retry per-URL on the chain)
- web_extract_tool() end-to-end — primary raises → fallback extract used
- web_extract_tool() end-to-end — primary RETURNS a timeout/5xx → only that
  URL retries on the fallback chain; policy blocks and ordinary page errors stay
- Firecrawl _scrape_one() — timeout error names the host and the 60s cap
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from tests.tools.conftest import register_all_web_providers


# ---------------------------------------------------------------------------
# _get_fallback_backends — config parsing
# ---------------------------------------------------------------------------


class TestGetFallbackBackends:
    def test_list_value_preserved_in_order(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["exa", "tavily"]})
        assert web_tools._get_fallback_backends("search") == ["exa", "tavily"]

    def test_comma_separated_string_split(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": "exa, tavily"})
        assert web_tools._get_fallback_backends("search") == ["exa", "tavily"]

    def test_bracketed_string_parsed(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": "[exa, tavily]"})
        assert web_tools._get_fallback_backends("search") == ["exa", "tavily"]

    def test_missing_key_yields_empty_chain(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        assert web_tools._get_fallback_backends("search") == []

    def test_null_value_yields_empty_chain(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": None})
        assert web_tools._get_fallback_backends("search") == []

    def test_primary_dropped_from_chain(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["brave-free", "exa"]})
        assert web_tools._get_fallback_backends("search", primary="brave-free") == ["exa"]

    def test_duplicates_collapsed_first_wins(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["exa", "tavily", "exa"]})
        assert web_tools._get_fallback_backends("search") == ["exa", "tavily"]

    def test_whitespace_and_empty_entries_discarded(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["  ", "", "exa"]})
        assert web_tools._get_fallback_backends("search") == ["exa"]

    def test_case_normalized(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["EXA", "Tavily"]})
        assert web_tools._get_fallback_backends("search") == ["exa", "tavily"]

    def test_extract_capability_reads_its_own_key(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {
            "search_fallbacks": ["exa"],
            "extract_fallbacks": ["tavily"],
        })
        assert web_tools._get_fallback_backends("extract") == ["tavily"]


# ---------------------------------------------------------------------------
# _run_search_fallbacks — dispatch behavior
# ---------------------------------------------------------------------------


class _FakeSearchProvider:
    """Minimal registered-shaped provider for fallback dispatch tests."""

    def __init__(self, name, result=None, available=True, supports=True, raises=None):
        self._name = name
        self._result = result
        self._available = available
        self._supports = supports
        self._raises = raises
        self.calls = 0

    @property
    def name(self):
        return self._name

    def supports_search(self):
        return self._supports

    def is_available(self):
        return self._available

    def search(self, query, limit=5):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result


class TestRunSearchFallbacks:
    def _registry(self, providers):
        table = {p.name: p for p in providers}
        return lambda name: table.get(name)

    def test_first_healthy_fallback_wins(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["exa", "tavily"]})
        exa = _FakeSearchProvider("exa", result={"success": True, "data": {"web": [{"title": "from exa"}]}})
        tavily = _FakeSearchProvider("tavily", result={"success": True, "data": {"web": []}})
        primary_result = {"success": False, "error": "Brave Search returned HTTP 402"}

        out = web_tools._run_search_fallbacks(
            "q", 5, primary_result, primary="brave-free",
            _wsp_get_provider=self._registry([exa, tavily]),
        )
        assert out["success"] is True
        assert out["data"]["web"][0]["title"] == "from exa"
        assert exa.calls == 1
        assert tavily.calls == 0  # never reached

    def test_skips_failing_fallback_to_next(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["exa", "tavily"]})
        exa = _FakeSearchProvider("exa", result={"success": False, "error": "exa down"})
        tavily = _FakeSearchProvider("tavily", result={"success": True, "data": {"web": [{"title": "ok"}]}})
        primary_result = {"success": False, "error": "primary dead"}

        out = web_tools._run_search_fallbacks(
            "q", 5, primary_result, primary="brave-free",
            _wsp_get_provider=self._registry([exa, tavily]),
        )
        assert out["success"] is True
        assert exa.calls == 1
        assert tavily.calls == 1

    def test_all_fail_returns_primary_error(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["exa"]})
        exa = _FakeSearchProvider("exa", result={"success": False, "error": "exa down"})
        primary_result = {"success": False, "error": "Brave Search returned HTTP 402"}

        out = web_tools._run_search_fallbacks(
            "q", 5, primary_result, primary="brave-free",
            _wsp_get_provider=self._registry([exa]),
        )
        # Primary's error is more actionable than the fallback's.
        assert out is primary_result
        assert "402" in out["error"]

    def test_empty_chain_returns_primary_untouched(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        primary_result = {"success": False, "error": "boom"}
        out = web_tools._run_search_fallbacks(
            "q", 5, primary_result, primary="brave-free",
            _wsp_get_provider=self._registry([]),
        )
        assert out is primary_result

    def test_unavailable_fallback_skipped(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["exa", "tavily"]})
        exa = _FakeSearchProvider("exa", available=False)
        tavily = _FakeSearchProvider("tavily", result={"success": True, "data": {"web": []}})
        primary_result = {"success": False, "error": "primary dead"}

        out = web_tools._run_search_fallbacks(
            "q", 5, primary_result, primary="brave-free",
            _wsp_get_provider=self._registry([exa, tavily]),
        )
        assert out["success"] is True
        assert exa.calls == 0  # skipped — not available
        assert tavily.calls == 1

    def test_unregistered_fallback_skipped(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["ghost", "tavily"]})
        tavily = _FakeSearchProvider("tavily", result={"success": True, "data": {"web": []}})
        primary_result = {"success": False, "error": "primary dead"}

        out = web_tools._run_search_fallbacks(
            "q", 5, primary_result, primary="brave-free",
            _wsp_get_provider=self._registry([tavily]),  # "ghost" not registered
        )
        assert out["success"] is True
        assert tavily.calls == 1

    def test_raising_fallback_treated_as_failure(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_fallbacks": ["exa", "tavily"]})
        exa = _FakeSearchProvider("exa", raises=RuntimeError("kaboom"))
        tavily = _FakeSearchProvider("tavily", result={"success": True, "data": {"web": []}})
        primary_result = {"success": False, "error": "primary dead"}

        out = web_tools._run_search_fallbacks(
            "q", 5, primary_result, primary="brave-free",
            _wsp_get_provider=self._registry([exa, tavily]),
        )
        assert out["success"] is True
        assert tavily.calls == 1


# ---------------------------------------------------------------------------
# web_search_tool end-to-end — brave 402 → exa fallback
# ---------------------------------------------------------------------------


class TestWebSearchToolFallbackE2E:
    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        register_all_web_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_brave_402_falls_back_to_exa(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {
            "search_backend": "brave-free",
            "search_fallbacks": ["exa"],
        })
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAkey123")
        monkeypatch.setenv("EXA_API_KEY", "exa-key")
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        # Brave returns a 402 failure; exa returns results.
        brave_fail = {"success": False, "error": "Brave Search returned HTTP 402"}
        exa_ok = {"success": True, "data": {"web": [{"title": "exa hit", "url": "https://x", "description": "", "position": 1}]}}

        from plugins.web.brave_free.provider import BraveFreeWebSearchProvider
        from plugins.web.exa.provider import ExaWebSearchProvider
        monkeypatch.setattr(BraveFreeWebSearchProvider, "search", lambda self, q, limit=5: brave_fail)
        monkeypatch.setattr(ExaWebSearchProvider, "search", lambda self, q, limit=5: exa_ok)

        result = json.loads(web_tools.web_search_tool("test", limit=5))
        assert result["success"] is True
        assert result["data"]["web"][0]["title"] == "exa hit"

    def test_no_fallback_configured_surfaces_brave_error(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {
            "search_backend": "brave-free",
        })
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAkey123")
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        brave_fail = {"success": False, "error": "Brave Search returned HTTP 402"}
        from plugins.web.brave_free.provider import BraveFreeWebSearchProvider
        monkeypatch.setattr(BraveFreeWebSearchProvider, "search", lambda self, q, limit=5: brave_fail)

        result = json.loads(web_tools.web_search_tool("test", limit=5))
        assert result["success"] is False
        assert "402" in result["error"]

    def test_empty_successful_result_does_not_fallback(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {
            "search_backend": "brave-free",
            "search_fallbacks": ["exa"],
        })
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAkey123")
        monkeypatch.setenv("EXA_API_KEY", "exa-key")
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        brave_empty = {"success": True, "data": {"web": []}}
        from plugins.web.brave_free.provider import BraveFreeWebSearchProvider
        from plugins.web.exa.provider import ExaWebSearchProvider
        monkeypatch.setattr(BraveFreeWebSearchProvider, "search", lambda self, q, limit=5: brave_empty)

        exa_called = {"hit": False}

        def _exa_search(self, q, limit=5):
            exa_called["hit"] = True
            return {"success": True, "data": {"web": [{"title": "should not appear"}]}}

        monkeypatch.setattr(ExaWebSearchProvider, "search", _exa_search)

        result = json.loads(web_tools.web_search_tool("test", limit=5))
        assert result["success"] is True
        assert result["data"]["web"] == []
        assert exa_called["hit"] is False  # empty-but-successful = no fallback


# ---------------------------------------------------------------------------
# _run_extract_fallbacks — dispatch behavior
# ---------------------------------------------------------------------------


class _FakeExtractProvider:
    def __init__(self, name, result=None, available=True, supports=True, raises=None):
        self._name = name
        self._result = result
        self._available = available
        self._supports = supports
        self._raises = raises
        self.calls = 0

    @property
    def name(self):
        return self._name

    def supports_extract(self):
        return self._supports

    def is_available(self):
        return self._available

    def extract(self, urls, **kwargs):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result


class TestRunExtractFallbacks:
    def _registry(self, providers):
        table = {p.name: p for p in providers}
        return lambda name: table.get(name)

    def test_fallback_result_returned_when_primary_raises(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_fallbacks": ["tavily"]})
        tavily = _FakeExtractProvider("tavily", result=[{"url": "https://x", "title": "t", "content": "c"}])
        primary = _FakeExtractProvider("firecrawl")
        primary_exc = RuntimeError("firecrawl unreachable")

        out = asyncio.get_event_loop().run_until_complete(
            web_tools._run_extract_fallbacks(
                ["https://x"], None, primary, primary_exc,
                _wsp_get_provider=self._registry([tavily]),
            )
        )
        assert out[0]["title"] == "t"
        assert tavily.calls == 1

    def test_all_raise_reraises_primary(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_fallbacks": ["tavily"]})
        tavily = _FakeExtractProvider("tavily", raises=RuntimeError("tavily down"))
        primary = _FakeExtractProvider("firecrawl")
        primary_exc = RuntimeError("firecrawl unreachable")

        with pytest.raises(RuntimeError, match="firecrawl unreachable"):
            asyncio.get_event_loop().run_until_complete(
                web_tools._run_extract_fallbacks(
                    ["https://x"], None, primary, primary_exc,
                    _wsp_get_provider=self._registry([tavily]),
                )
            )

    def test_empty_chain_reraises_primary(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        primary = _FakeExtractProvider("firecrawl")
        primary_exc = RuntimeError("primary dead")

        with pytest.raises(RuntimeError, match="primary dead"):
            asyncio.get_event_loop().run_until_complete(
                web_tools._run_extract_fallbacks(
                    ["https://x"], None, primary, primary_exc,
                    _wsp_get_provider=self._registry([]),
                )
            )

    def test_unavailable_fallback_skipped(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_fallbacks": ["exa", "tavily"]})
        exa = _FakeExtractProvider("exa", available=False)
        tavily = _FakeExtractProvider("tavily", result=[{"url": "https://x", "title": "t", "content": "c"}])
        primary = _FakeExtractProvider("firecrawl")
        primary_exc = RuntimeError("primary dead")

        out = asyncio.get_event_loop().run_until_complete(
            web_tools._run_extract_fallbacks(
                ["https://x"], None, primary, primary_exc,
                _wsp_get_provider=self._registry([exa, tavily]),
            )
        )
        assert out[0]["title"] == "t"
        assert exa.calls == 0
        assert tavily.calls == 1


# ---------------------------------------------------------------------------
# web_extract_tool end-to-end — primary raises → fallback used
# ---------------------------------------------------------------------------


class TestWebExtractToolFallbackE2E:
    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        register_all_web_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_primary_raises_falls_back_to_keenable(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {
            "extract_backend": "exa",
            "extract_fallbacks": ["keenable"],
        })
        monkeypatch.setenv("EXA_API_KEY", "exa-key")
        monkeypatch.setenv("KEENABLE_API_KEY", "keen-key")

        async def _allow_ssrf(_url):
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_ssrf)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        from plugins.web.exa.provider import ExaWebSearchProvider
        from plugins.web.keenable.provider import KeenableWebSearchProvider

        def _exa_raise(self, urls, **kwargs):
            raise RuntimeError("exa backend down")

        monkeypatch.setattr(ExaWebSearchProvider, "extract", _exa_raise)
        monkeypatch.setattr(
            KeenableWebSearchProvider, "extract",
            lambda self, urls, **kwargs: [{"url": urls[0], "title": "keenable", "content": "body", "raw_content": "body"}],
        )

        result = json.loads(asyncio.get_event_loop().run_until_complete(
            web_tools.web_extract_tool(["https://example.com"])
        ))
        # Fallback content reached the post-processing pipeline.
        assert any("keenable" in (r.get("title") or "") for r in result["results"])

# ---------------------------------------------------------------------------
# web_extract_tool end-to-end — primary RETURNS a timeout/5xx → per-URL retry
# ---------------------------------------------------------------------------


class TestWebExtractReturnedErrorFallbackE2E:
    """Firecrawl-style RETURNED per-URL timeout / HTTP 5xx errors retry just
    those URLs on the configured web.extract_fallbacks chain.

    Unlike the raise path above, the primary extract came back — only some
    pages failed transiently — so the whole batch must NOT re-dispatch, the
    successful pages must NOT go to the fallback, and policy/SSRF blocks and
    ordinary page errors must stay exactly as the primary reported them.
    """

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        register_all_web_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def _run_extract(self, monkeypatch, urls, primary_entries,
                     fallbacks=("exa",), exa_behavior=None):
        """web_extract_tool with firecrawl primary / exa fallback, both stubbed.

        Returns ``(parsed_result, exa_calls)`` where ``exa_calls`` records the
        URL list of every fallback dispatch.
        """
        from tools import web_tools

        cfg = {"extract_backend": "firecrawl"}
        if fallbacks is not None:
            cfg["extract_fallbacks"] = list(fallbacks)
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: cfg)
        monkeypatch.setenv("EXA_API_KEY", "exa-key")

        async def _allow_ssrf(_url):
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_ssrf)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)
        # Keyless ring rescue stays inert: nothing here may hit the network,
        # and a served body must provably come from the fallback provider.
        monkeypatch.setattr(web_tools, "_rescue_eligible", lambda p: False)

        from plugins.web.exa.provider import ExaWebSearchProvider
        from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

        monkeypatch.setattr(
            FirecrawlWebSearchProvider, "extract",
            lambda self, urls, **kwargs: [dict(primary_entries[u]) for u in urls],
        )

        exa_calls = []

        def _exa_extract(self, urls, **kwargs):
            exa_calls.append(list(urls))
            if exa_behavior == "raise":
                raise RuntimeError("exa backend down")
            if exa_behavior == "error-entry":
                return [{"url": u, "title": "", "content": "", "error": "Exa crawl failed"}
                        for u in urls]
            return [{"url": u, "title": f"exa-title:{u}", "content": f"exa-body:{u}",
                     "raw_content": f"exa-body:{u}"} for u in urls]

        monkeypatch.setattr(ExaWebSearchProvider, "extract", _exa_extract)

        result = json.loads(asyncio.get_event_loop().run_until_complete(
            web_tools.web_extract_tool(list(urls))
        ))
        return result, exa_calls

    def test_returned_timeout_retries_that_url_on_fallback(self, monkeypatch):
        url = "https://slow.example.com/page"
        result, exa_calls = self._run_extract(
            monkeypatch, [url],
            {url: {"url": url, "title": "", "content": "",
                   "error": "Scrape timed out after 60s for slow.example.com — page may be too large or unresponsive."}},
        )
        # Fallback called exactly once, with ONLY the timed-out URL...
        assert exa_calls == [[url]]
        # ...and the body it returned is what the tool serves (not the rescue's).
        entry = result["results"][0]
        assert entry["title"] == f"exa-title:{url}"
        assert entry["content"] == f"exa-body:{url}"
        assert not entry.get("error")

    def test_generic_page_error_does_not_call_fallback(self, monkeypatch):
        url = "https://example.com/missing"
        result, exa_calls = self._run_extract(
            monkeypatch, [url],
            {url: {"url": url, "title": "", "content": "", "error": "Page not found"}},
        )
        assert exa_calls == []
        assert "Page not found" in result["results"][0]["error"]

    def test_policy_block_containing_500_is_not_retried(self, monkeypatch):
        url = "https://blocked.example.com/"
        result, exa_calls = self._run_extract(
            monkeypatch, [url],
            {url: {"url": url, "title": "", "content": "",
                   "error": "Blocked by website policy: internal 500 page",
                   "blocked_by_policy": {"host": "blocked.example.com", "rule": "test"}}},
        )
        assert exa_calls == []
        assert "Blocked by website policy" in result["results"][0]["error"]

    def test_mixed_batch_retries_only_the_timed_out_url(self, monkeypatch):
        ok_url = "https://ok.example.com/"
        slow_url = "https://slow.example.com/report"
        result, exa_calls = self._run_extract(
            monkeypatch,
            [ok_url, slow_url],
            {
                ok_url: {"url": ok_url, "title": "primary title", "content": "primary body",
                         "raw_content": "primary body"},
                slow_url: {"url": slow_url, "title": "", "content": "",
                           "error": "Scrape timed out after 60s for slow.example.com"},
            },
        )
        # The successful page is unchanged and never sent to the fallback.
        assert exa_calls == [[slow_url]]
        ok, slow = result["results"]
        assert ok["title"] == "primary title"
        assert ok["content"] == "primary body"
        assert slow["title"] == f"exa-title:{slow_url}"
        assert not slow.get("error")

    def test_http_500_retried_but_404_and_5000_are_not(self, monkeypatch):
        u500, u404, u5000 = ("https://a.example.com/", "https://b.example.com/", "https://c.example.com/")
        result, exa_calls = self._run_extract(
            monkeypatch,
            [u500, u404, u5000],
            {
                u500: {"url": u500, "title": "", "content": "",
                       "error": "500 Server Error: Internal Server Error"},
                u404: {"url": u404, "title": "", "content": "",
                       "error": "404 Client Error: Not Found"},
                u5000: {"url": u5000, "title": "", "content": "",
                        "error": "Job 5000 vanished before completion"},
            },
        )
        assert exa_calls == [[u500]]
        entries = {r["url"]: r for r in result["results"]}
        assert not entries[u500].get("error")
        assert "404" in entries[u404]["error"]
        assert "5000" in entries[u5000]["error"]

    def test_empty_fallbacks_preserves_original_timeout_error(self, monkeypatch):
        url = "https://slow.example.com/page"
        result, exa_calls = self._run_extract(
            monkeypatch, [url],
            {url: {"url": url, "title": "", "content": "", "error": "Scrape timed out after 60s"}},
            fallbacks=(),
        )
        assert exa_calls == []
        assert "timed out" in result["results"][0]["error"]

    @pytest.mark.parametrize("exa_behavior", ["raise", "error-entry"])
    def test_failed_fallback_preserves_original_error_and_tool_still_succeeds(
            self, monkeypatch, exa_behavior):
        url = "https://slow.example.com/page"
        result, exa_calls = self._run_extract(
            monkeypatch, [url],
            {url: {"url": url, "title": "", "content": "", "error": "Scrape timed out after 60s"}},
            exa_behavior=exa_behavior,
        )
        assert exa_calls  # the fallback was attempted...
        assert isinstance(result.get("results"), list)  # ...but the tool call did not fail
        assert "timed out" in result["results"][0]["error"]  # original error survives


# ---------------------------------------------------------------------------
# Firecrawl _scrape_one — timeout error text
# ---------------------------------------------------------------------------


class TestFirecrawlScrapeTimeoutMessage:
    def test_timeout_error_names_host_and_60s_without_browser_advice(self, monkeypatch):
        from types import SimpleNamespace

        from plugins.web.firecrawl import provider as fc

        monkeypatch.setattr(fc, "check_website_access", lambda url: None)
        monkeypatch.setattr(fc, "_get_firecrawl_client",
                            lambda: SimpleNamespace(scrape=lambda **kw: None))

        async def _instant_timeout(awaitable, timeout=None):
            awaitable.close()  # coroutine never starts: no worker thread, no 60s sleep
            raise asyncio.TimeoutError()

        monkeypatch.setattr("asyncio.wait_for", _instant_timeout)

        entry = asyncio.get_event_loop().run_until_complete(
            fc._scrape_one("https://slow.example.com/x", ["markdown"], None)
        )
        assert "slow.example.com" in entry["error"]
        assert "60s" in entry["error"]
        assert "browser_navigate" not in entry["error"]
