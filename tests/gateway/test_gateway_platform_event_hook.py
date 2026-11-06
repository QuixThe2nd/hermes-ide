"""Tests for the ``gateway_platform_event`` observer hook (#64176's observer half).

Covers the runner-owned half of the normalized-envelope pattern:
* only ``gateway_platform_event`` is registered in ``VALID_HOOKS`` (no inert
  hook surface without a concrete fire site)
* the adapter forwards normalized events to a runner-owned callback; the runner
  performs the authoritative post-auth check before invoking plugins
* the profile-scoped handlers stamp the routed profile before dispatch

The adapter-side normalization/fire-site tests lived with the Telegram adapter
and were removed alongside it.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.profile_routing import ProfileRoute
from gateway.session import SessionSource

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.run import GatewayRunner  # noqa: E402
from hermes_cli.plugins import VALID_HOOKS  # noqa: E402


@pytest.fixture(autouse=True)
def _observer_available(monkeypatch):
    """Most dispatch tests exercise the subscribed path explicitly."""
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)


def _source(user_id="777", profile=None):
    """Synthetic SessionSource standing in for an adapter-derived event actor."""
    src = SessionSource(
        platform=Platform.DISCORD,
        user_id=user_id,
        chat_id="123",
        chat_type="dm",
        profile=profile,
    )
    return src


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------

class TestHookRegistration:
    def test_gateway_platform_event_registered_reserved_absent(self):
        """register_hook rejects names not in VALID_HOOKS, so the implemented
        hook must be present. The reserved gateway_* names are deliberately
        absent (no inert surface without a concrete fire site); lock that in."""
        assert "gateway_platform_event" in VALID_HOOKS
        assert "gateway_session_titled" not in VALID_HOOKS
        assert "gateway_message_delivered" not in VALID_HOOKS
        assert "gateway_thread_created" not in VALID_HOOKS


# ---------------------------------------------------------------------------
# Runner-owned dispatch — authoritative post-auth gate + isolation
# ---------------------------------------------------------------------------

class TestRunnerDispatch:
    def test_authorized_event_routes_normalized_envelope(self):
        runner = object.__new__(GatewayRunner)
        runner._is_user_authorized = lambda source: source.user_id == "777"
        invoke = MagicMock()
        source = _source(user_id="777")
        event = {
            "platform": "discord",
            "event_type": "reaction",
            "payload": {"chat_id": "123", "message_id": "456", "emojis": ["x"]},
        }

        with patch("hermes_cli.lifecycle.invoke_hook", invoke):
            asyncio.run(runner._handle_gateway_platform_event(event, source))

        invoke.assert_called_once_with("gateway_platform_event", **event)

    def test_unauthorized_event_never_reaches_hooks(self):
        runner = object.__new__(GatewayRunner)
        runner._is_user_authorized = lambda source: False
        invoke = MagicMock()

        with patch("hermes_cli.lifecycle.invoke_hook", invoke):
            asyncio.run(runner._handle_gateway_platform_event(
                {"platform": "discord", "event_type": "reaction", "payload": {}},
                _source(),
            ))

        invoke.assert_not_called()

    def test_skips_dispatch_when_no_subscriber(self):
        runner = object.__new__(GatewayRunner)
        authorized = MagicMock(return_value=True)
        runner._is_user_authorized = authorized
        invoke = MagicMock()

        with patch("hermes_cli.lifecycle.has_hook", return_value=False), patch(
            "hermes_cli.lifecycle.invoke_hook", invoke
        ):
            asyncio.run(runner._handle_gateway_platform_event(
                {"platform": "discord", "event_type": "reaction", "payload": {}},
                _source(),
            ))

        authorized.assert_not_called()
        invoke.assert_not_called()

    def test_plugin_layer_error_is_isolated(self):
        runner = object.__new__(GatewayRunner)
        runner._is_user_authorized = lambda source: True
        invoke = MagicMock(side_effect=RuntimeError("plugin boom"))

        with patch("hermes_cli.lifecycle.invoke_hook", invoke):
            asyncio.run(runner._handle_gateway_platform_event(
                {"platform": "discord", "event_type": "reaction", "payload": {}},
                _source(),
            ))  # no raise


class TestProfileScopedPlatformEventHandler:
    def test_primary_shared_transport_uses_profile_route_and_transport_provenance(self):
        runner = object.__new__(GatewayRunner)
        runner.config = SimpleNamespace(  # type: ignore[assignment]
            multiplex_profiles=True,
            profile_routes=[
                ProfileRoute(
                    name="work-chat",
                    platform="discord",
                    profile="work",
                    chat_id="123",
                )
            ],
        )
        adapter = SimpleNamespace(name="discord-primary")

        # Main now rejects routes to unserved profiles (_profile_name_for_source
        # checks _multiplex_profile_homes); declare "work" as served so the
        # route stamps rather than fail-closing to profile=None.
        with patch(
            "gateway.run._multiplex_profile_homes",
            return_value=[("work", Path("/profiles/work"))],
        ):
            source = _source(profile="work")
            source._transport_adapter_ref = lambda: adapter

        assert source.profile == "work"
        assert source._transport_adapter_ref() is adapter

        resolver = MagicMock(return_value=Path("/profiles/work"))
        runner._resolve_profile_home_for_source = resolver
        dispatch = AsyncMock()
        runner._handle_gateway_platform_event = dispatch
        handler = runner._make_default_profile_platform_event_handler()
        with patch("gateway.run._profile_runtime_scope", side_effect=lambda _home: nullcontext()):
            asyncio.run(handler({"event_type": "reaction"}, source))

        resolver.assert_called_once_with(source)
        dispatch.assert_awaited_once_with({"event_type": "reaction"}, source)

    def test_secondary_handler_stamps_profile_before_dispatch(self, monkeypatch):
        runner = object.__new__(GatewayRunner)
        captured = {}

        async def dispatch(event, source):
            captured["event"] = event
            captured["profile"] = source.profile

        runner._handle_gateway_platform_event = dispatch
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda name: None,
        )
        handler = runner._make_profile_platform_event_handler("work")

        asyncio.run(handler(
            {"platform": "discord", "event_type": "reaction", "payload": {}},
            _source(profile="work"),
        ))

        assert captured["profile"] == "work"
