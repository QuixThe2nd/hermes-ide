"""Successor delivery during a restart window: the API server.

The API server has no overlap/hub attachment: its HTTP port stays with the
predecessor until the process exits. A new agent-turn request that arrives
while the gateway is draining is REFUSED at the HTTP layer with a
retryable 503 ``gateway_draining`` — it never becomes a predecessor drain
queue record and never starts predecessor work; the client retries against
the successor once the port moves.
"""

import json

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run_drain_queue import drain_queue_path
from tests.gateway.restart_test_helpers import make_restart_runner


def _adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))


def test_draining_gateway_refuses_new_api_turns_with_retryable_503(monkeypatch):
    adapter = _adapter()
    runner, _adapter_wired = make_restart_runner(platform=Platform.API_SERVER)
    runner._draining = True
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

    response = adapter._draining_response()

    assert response is not None
    assert response.status == 503
    body = json.loads(response.body)
    assert body["error"]["code"] == "gateway_draining"
    assert response.headers["Retry-After"] == "1"
    # The refusal is the whole story: nothing is parked for the predecessor
    # to process later — no drain-queue record exists.
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
