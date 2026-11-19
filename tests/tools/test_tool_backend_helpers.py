"""Unit tests for tools/tool_backend_helpers.py.

Tests cover:
- prefers_gateway() gateway routing
- read_selection() vendor-name semantics
"""

from __future__ import annotations

from tools.tool_backend_helpers import prefers_gateway, read_selection


# ---------------------------------------------------------------------------
# prefers_gateway
# ---------------------------------------------------------------------------
class TestPrefersGateway:
    """Honor bool-ish config values for tool gateway routing."""

    def test_returns_false_for_quoted_false(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"web": {"use_gateway": "false"}},
        )
        assert prefers_gateway("web") is False

    def test_returns_true_for_quoted_true(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"web": {"use_gateway": "true"}},
        )
        assert prefers_gateway("web") is True


# ---------------------------------------------------------------------------
# read_selection
# ---------------------------------------------------------------------------
class TestReadSelection:
    def test_returns_vendor_name_from_raw_section(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config_readonly",
            lambda: {"web": {"backend": "Tavily"}},
        )
        assert read_selection("web") == "tavily"

    def test_returns_none_when_never_configured(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config_readonly",
            lambda: {},
        )
        assert read_selection("web") is None

    def test_returns_none_with_only_a_stale_use_gateway_flag(self, monkeypatch):
        """The legacy ``use_gateway`` shim is gone: it maps to no vendor selection."""
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config_readonly",
            lambda: {"web": {"use_gateway": True}},
        )
        assert read_selection("web") is None
