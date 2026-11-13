"""Unit tests for tools/tool_backend_helpers.py.

Tests cover:
- managed_nous_tools_enabled() subscription-based gate
- prefers_gateway() gateway routing
"""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli.nous_account import NousPaidServiceAccessInfo, NousPortalAccountInfo
from tools.tool_backend_helpers import (
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    prefers_gateway,
)


def _raise_import():
    raise ImportError("simulated missing module")


# ---------------------------------------------------------------------------
# managed_nous_tools_enabled
# ---------------------------------------------------------------------------
class TestManagedNousToolsEnabled:
    """Subscription-based gate: True for paid Nous subscribers."""

    def test_disabled_when_not_logged_in(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.nous_account.get_nous_portal_account_info",
            lambda: NousPortalAccountInfo(logged_in=False, source="none", fresh=False),
        )
        assert managed_nous_tools_enabled() is False


    def test_returns_false_on_exception(self, monkeypatch):
        """Should never crash — returns False on any exception."""
        monkeypatch.setattr(
            "hermes_cli.nous_account.get_nous_portal_account_info",
            _raise_import,
        )
        assert managed_nous_tools_enabled() is False


class TestNousToolGatewayUnavailableMessage:
    def test_uses_entitlement_reason_for_logged_in_user(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.nous_account.get_nous_portal_account_info",
            lambda force_fresh=False: NousPortalAccountInfo(
                logged_in=True,
                source="account_api",
                fresh=True,
                paid_service_access=False,
                portal_base_url="https://portal.example.test",
                paid_service_access_info=NousPaidServiceAccessInfo(
                    allowed=False,
                    reason="no_usable_credits",
                    has_active_subscription=True,
                    active_subscription_is_paid=True,
                    subscription_credits_remaining=0,
                    purchased_credits_remaining=0,
                    total_usable_credits=0,
                ),
            ),
        )

        message = nous_tool_gateway_unavailable_message("managed image generation")

        assert "credits are exhausted" in message
        assert "managed image generation" in message
        assert "https://portal.example.test/billing" in message


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
