"""Cron delivery for relay-fronted logical platforms.

Bug report: a deployment where Discord is fronted by the relay connector
(``GATEWAY_RELAY_PLATFORMS=discord``) could not deliver to an explicit
``discord:<chat>`` target: the delivery loop's native ``pconfig.enabled``
gate rejected the platform ("not configured/enabled") although
``resolve_delivery_transport`` had already produced a live relay transport
that fronts it — a relay-fronted logical platform is deliberately NOT
natively enabled (its credential lives in the connector).
"""

import asyncio
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

from cron.scheduler import _deliver_result
from gateway.config import Platform


# ---------------------------------------------------------------------------
# Delivery: relay transport must bypass the native enabled gate
# ---------------------------------------------------------------------------

class TestRelayDeliveryGate:
    def _relay_adapter(self):
        adapter = AsyncMock()
        adapter.fronts_platform = lambda p: p == Platform.DISCORD
        return adapter

    def _job(self):
        return {
            "id": "relay-job",
            "name": "Relay Job",
            "deliver": "discord:123",
            "origin": {"platform": "discord", "chat_id": "123"},
        }

    def _run(self, adapters, gateway_config):
        loop = MagicMock()
        loop.is_running.return_value = True

        def fake_run_coro(coro, _loop):
            future = Future()
            try:
                future.set_result(asyncio.run(coro))
            except BaseException as e:  # noqa: BLE001
                future.set_exception(e)
            return future

        router = MagicMock()

        async def _deliver_to_platform(target, content, metadata):
            return {"success": True, "raw_response": None}

        router._deliver_to_platform = _deliver_to_platform

        with patch("gateway.config.load_gateway_config",
                   return_value=gateway_config), \
             patch("cron.scheduler.load_config",
                   return_value={"cron": {"wrap_response": False}}), \
             patch("gateway.delivery.DeliveryRouter", return_value=router), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            return _deliver_result(self._job(), "Nightly report.",
                                   adapters=adapters, loop=loop)

    def _bare_config(self):
        config = MagicMock()
        config.platforms = {}  # neither discord nor relay configured natively
        return config

    def test_relay_fronted_platform_is_not_rejected(self):
        """A live relay transport that fronts Discord must deliver even though
        platforms.discord has no native config block at all."""
        result = self._run({Platform.RELAY: self._relay_adapter()}, self._bare_config())
        assert result is None  # None == delivered without errors

    def test_native_gate_preserved_without_relay(self):
        """No relay transport → the historical configured/enabled gate stays."""
        result = self._run({}, self._bare_config())
        assert result is not None

    def test_relay_fronted_without_live_gateway_errors_accurately(self, monkeypatch):
        """A relay-fronted platform with NO live relay transport (manual
        in-process `hermes cron run`) fails with the accurate 'gateway required'
        message — never the native 'not configured/enabled' gate, which
        misdiagnoses relay-fronted deployments whose credential lives in the
        connector, not natively."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "discord")
        result = self._run({}, self._bare_config())  # no live adapters
        assert result is not None
        assert "relay-fronted" in (result or "")
        assert "not configured" not in (result or "")
