"""Restart forwarding between overlapping generations (the chain replacement).

The generation array owns restart authority: a restart request that lands
on a superseded / draining generation must reach the NEWEST live
generation, which re-queues it through the same ``queue_user_restart``
core and spawns its own successor. The old one-shot ``.restart_chain.json``
marker protocol is retired; the transport is the hub control
``restart_request`` (``gateway/restart.py::handle_forwarded_restart``).
"""

from __future__ import annotations

import json

import pytest

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway import run as gateway_run
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.restart_preflight.warn_before_user_restart",
        AsyncMock(return_value=True),
    )


def _queueable_runner():
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    return runner


@pytest.mark.asyncio
async def test_forwarded_restart_requeues_with_requester_routing(_isolated_home):
    """The newest generation persists the ORIGINAL requester's comeback routing."""
    from gateway.restart import handle_forwarded_restart

    runner = _queueable_runner()
    requester = {
        # A real Platform value: the comeback marker round-trips through
        # SessionSource, whose enum has no "telegram" in this fork.
        "platform": "discord", "chat_id": "42",
        "chat_type": "dm", "message_id": "m77",
    }
    result = await handle_forwarded_restart(runner, restart_hub=False, requester=requester)

    assert result["status"] == "restarting"
    runner.request_restart.assert_called_once()
    marker = json.loads((gateway_run._hermes_home / ".restart_notify.json").read_text(encoding="utf-8"))
    assert marker == requester


@pytest.mark.asyncio
async def test_forwarded_restart_without_requester_still_restarts(_isolated_home):
    """A forward without a requester payload (old-generation payload) still queues."""
    from gateway.restart import handle_forwarded_restart

    runner = _queueable_runner()
    result = await handle_forwarded_restart(runner, restart_hub=True, requester=None)

    assert result["status"] == "restarting"
    runner.request_restart.assert_called_once()
    marker = json.loads((gateway_run._hermes_home / ".restart_notify.json").read_text(encoding="utf-8"))
    assert marker["platform"] is None


@pytest.mark.asyncio
async def test_forwarded_restart_requeues_through_hub_control_client(_isolated_home):
    """The hub-client control entry hands the params to the wired gateway handler."""
    from plugins.platforms.discord.hub_client import HubOverlapClientMixin

    class _Client(HubOverlapClientMixin):
        def __init__(self):
            super().__init__()

    client = _Client()
    with pytest.raises(RuntimeError, match="restart handler is not wired"):
        await client._restart_request_control({})

    observed: dict = {}

    async def _handler(*, restart_hub, requester):
        observed["restart_hub"] = restart_hub
        observed["requester"] = requester
        return {"status": "restarting", "active_agents": 1}

    client.set_restart_request_handler(_handler)
    result = await client._restart_request_control({
        "restart_hub": True,
        "requester": {"platform": "telegram", "chat_id": "42", "chat_type": "dm"},
    })
    assert observed["restart_hub"] is True
    assert observed["requester"]["platform"] == "telegram"
    assert result["status"] == "restarting"


def test_runner_wires_forwarded_restart_handler_on_hub_adapters():
    """`_wire_adapter_handlers` installs the runner entry on adapters exposing the setter."""
    from gateway.platforms.base import Platform
    from gateway.run_adapters import GatewayAdapterLifecycleMixin

    class _HubAdapter(GatewayAdapterLifecycleMixin):
        """Setter surface the wiring contract touches."""

        platform = Platform.DISCORD

        def set_message_handler(self, h):
            self._message_handler = h

        def set_fatal_error_handler(self, h):
            self._fatal = h

        def set_session_store(self, s):
            self._store = s

        def set_busy_session_handler(self, h):
            self._busy = h

        def set_restart_request_handler(self, handler):
            self.restart_handler = handler

        def set_topic_recovery_fn(self, fn):
            self._topic = fn

        def set_authorization_check(self, fn):
            self._authz = fn

        def set_platform_event_handler(self, h):
            self._events = h

    from gateway.run import GatewayRunner

    adapter = _HubAdapter()
    runner = SimpleNamespace(
        _primary_message_handler=lambda: AsyncMock(),
        _handle_adapter_fatal_error=AsyncMock(),
        session_store=MagicMock(),
        _primary_busy_session_handler=lambda: AsyncMock(),
        _handle_reaction_event=AsyncMock(),
        _handle_forwarded_restart=AsyncMock(),
        _recover_telegram_topic_thread_id=MagicMock(),
        _make_adapter_auth_check=lambda _platform: AsyncMock(),
        _primary_platform_event_handler=lambda: AsyncMock(),
    )
    runner._wire_adapter_handlers = (
        GatewayRunner._wire_adapter_handlers.__get__(runner, GatewayRunner)
    )
    runner._wire_adapter_handlers(adapter)
    assert adapter.restart_handler is runner._handle_forwarded_restart


@pytest.mark.asyncio
async def test_forward_payload_round_trips_through_notify_marker(_isolated_home):
    """Relay-provenance fields survive the forward payload round trip."""
    from gateway.restart import _restart_notify_payload, handle_forwarded_restart
    from gateway.session import SessionSource

    runner = _queueable_runner()
    source: SessionSource = make_restart_source(
        chat_id="99", chat_type="group", thread_id="t5", user_id="u9",
        delivered_via_upstream_relay=True, scope_id="s1",
    )
    requester = _restart_notify_payload(source, "m1")
    result = await handle_forwarded_restart(runner, restart_hub=False, requester=requester)

    assert result["status"] == "restarting"
    marker = json.loads((gateway_run._hermes_home / ".restart_notify.json").read_text(encoding="utf-8"))
    assert marker["chat_id"] == "99"
    assert marker["thread_id"] == "t5"
    assert marker["delivered_via_upstream_relay"] is True
    assert marker["user_id"] == "u9"
    assert marker["scope_id"] == "s1"
    assert marker["message_id"] == "m1"
