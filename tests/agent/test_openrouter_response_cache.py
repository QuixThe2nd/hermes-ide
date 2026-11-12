"""Tests for OpenRouter response caching header injection."""

from types import SimpleNamespace


def test_build_or_headers_uses_fixed_response_cache():
    from agent.auxiliary_client import build_or_headers

    first = build_or_headers()
    second = build_or_headers()

    assert first["X-OpenRouter-Cache"] == "true"
    assert first["X-OpenRouter-Cache-TTL"] == "300"
    assert first is not second
    assert first == second


# ---------------------------------------------------------------------------
# _check_openrouter_cache_status
# ---------------------------------------------------------------------------

class TestCheckOpenrouterCacheStatus:
    """Test the _check_openrouter_cache_status method on AIAgent."""

    def _make_agent(self):
        """Create a minimal AIAgent-like object with just the method under test."""
        from run_agent import AIAgent

        # Use object.__new__ to skip __init__, then set the attributes we need
        agent = object.__new__(AIAgent)
        agent._or_cache_hits = 0
        return agent



    def test_no_header_is_noop(self):
        agent = self._make_agent()
        resp = SimpleNamespace(headers={})
        agent._check_openrouter_cache_status(resp)
        assert getattr(agent, "_or_cache_hits", 0) == 0



    def test_case_insensitive(self):
        agent = self._make_agent()
        resp = SimpleNamespace(headers={"x-openrouter-cache-status": "hit"})
        agent._check_openrouter_cache_status(resp)
        assert agent._or_cache_hits == 1
