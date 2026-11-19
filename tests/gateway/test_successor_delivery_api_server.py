"""Successor delivery during a restart window: the API server.

The API server has no overlap/hub attachment: its HTTP port stays with the
predecessor until the process exits, so the successor never serves it during
the window. A new agent-turn request that arrives while the gateway is
draining for an overlap restart is therefore ADMITTED at the HTTP layer —
the draining predecessor is the API's only server, and its request
reservation already holds the drain's active-work count above zero. Only an
operator quiesce (``_external_drain_active``) or a terminal shutdown
(``stop()`` begun: ``_running`` cleared or ``_stop_task`` tasked) still
answers with the retryable 503 ``gateway_draining``.
"""
import json
from unittest.mock import MagicMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run_drain_queue import drain_queue_path
from tests.gateway.restart_test_helpers import make_restart_runner


def _adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))


def test_draining_gateway_admits_new_api_turns(monkeypatch):
    """Overlap-restart drain: the predecessor still owns the HTTP port, so a
    request is served locally instead of bouncing a spurious 503."""
    adapter = _adapter()
    runner, _adapter_wired = make_restart_runner(platform=Platform.API_SERVER)
    runner._draining = True
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

    assert adapter._draining_response() is None
    # Nothing is parked for the predecessor to process later — the request
    # runs inline; no drain-queue record exists.
    assert not drain_queue_path().exists()


def test_terminal_shutdown_drain_still_refuses_with_retryable_503(monkeypatch):
    """Once ``stop()`` is tasked the adapters are disconnecting: the same
    request gets the retryable refusal instead of a doomed turn."""
    adapter = _adapter()
    runner, _adapter_wired = make_restart_runner(platform=Platform.API_SERVER)
    runner._draining = True
    runner._stop_task = MagicMock()
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

    response = adapter._draining_response()

    assert response is not None
    assert response.status == 503
    body = json.loads(response.body)
    assert body["error"]["code"] == "gateway_draining"
    assert response.headers["Retry-After"] == "1"
    assert not drain_queue_path().exists()


def test_external_drain_quiesce_refuses_the_same_way(monkeypatch):
    """``_external_drain_active`` (an operator quiesce) closes API admission
    with the same retryable refusal."""
    adapter = _adapter()
    runner, _adapter_wired = make_restart_runner(platform=Platform.API_SERVER)
    runner._external_drain_active = True
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

    assert adapter._draining_response() is not None
    assert not drain_queue_path().exists()


def test_open_admission_passes_the_drain_check_through(monkeypatch):
    """Control: a non-draining gateway admits API requests (None = proceed)."""
    adapter = _adapter()
    runner, _adapter_wired = make_restart_runner(platform=Platform.API_SERVER)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

    assert adapter._draining_response() is None
    assert not drain_queue_path().exists()
