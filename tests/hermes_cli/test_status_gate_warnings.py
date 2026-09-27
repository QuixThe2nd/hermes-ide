"""Regression contract: /api/status forwards boot-time gate-config refusals.

The endpoint builds its response dict by explicit field picks, so a field added to
``_resolve_gateway_status`` never reaches the dashboard unless the endpoint forwards
it explicitly. Guards the seam the SPA banner reads ``gate_config_warnings`` from.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.web_routers.status as status_router


_RUNTIME = {
    "gateway_state": "running",
    "platforms": {"discord": {"state": "connected"}},
    "served_profiles": ["default", "beta"],
    "gate_config_warnings": [
        "beta:discord reaction_gate: emojis must list at most 32 emojis",
        "ops:discord response_gate: mode must be 'shadow' or 'enforce'",
    ],
}


class _FakeLiveness:
    running = True
    pid = 4242
    health_body = None

    def __init__(self, runtime):
        self.runtime = runtime


def _patch_runner(monkeypatch):
    monkeypatch.setattr(status_router, "read_runtime_status", lambda path=None: None)
    monkeypatch.setattr(status_router, "resolve_gateway_liveness",
                        lambda **kwargs: _FakeLiveness(dict(_RUNTIME)))
    def _none(*args, **kwargs):
        return None
    monkeypatch.setattr(status_router, "_load_configured_gateway_platforms", _none, raising=False)


def test_api_status_forwards_gate_config_warnings(monkeypatch):
    _patch_runner(monkeypatch)
    monkeypatch.setattr(status_router, "check_config_version", lambda: (1, 1), raising=False)
    monkeypatch.setattr(status_router, "_collect_profile_gateway_topology_cached",
                        lambda: {"profiles": [], "gateway_mode": "standalone", "gateways": []},
                        raising=False)
    async def _no_sessions():
        return 0
    monkeypatch.setattr(status_router, "_status_active_sessions", _no_sessions, raising=False)
    def _no_timeout():
        return 30
    monkeypatch.setattr(status_router, "_resolve_restart_drain_timeout", _no_timeout, raising=False)
    monkeypatch.setattr(status_router, "_auth_gate_status",
                        lambda: {"auth_required": True, "auth_providers": [], "auth_flows": []},
                        raising=False)
    async def _no_pressure(status, home):
        return None
    monkeypatch.setattr(status_router, "_advisory_pressure", _no_pressure, raising=False)
    monkeypatch.setattr(status_router, "_nous_session_validity", lambda: False, raising=False)
    async def _no_components(gateway):
        return {}
    monkeypatch.setattr(status_router, "_component_health", _no_components, raising=False)
    monkeypatch.setattr(status_router, "get_install_id", lambda: None, raising=False)

    app = FastAPI()
    app.include_router(status_router.router)
    body = TestClient(app).get("/api/status").json()
    assert body["gate_config_warnings"] == list(_RUNTIME["gate_config_warnings"])


def test_resolver_scopes_gate_warnings_to_requested_profile(monkeypatch, tmp_path):
    _patch_runner(monkeypatch)
    result = asyncio.run(status_router._resolve_gateway_status(tmp_path / "beta", None))
    assert result["gate_config_warnings"] == [
        "beta:discord reaction_gate: emojis must list at most 32 emojis"]


def test_resolver_unscoped_view_keeps_all_gate_warnings(monkeypatch):
    _patch_runner(monkeypatch)
    result = asyncio.run(status_router._resolve_gateway_status(None, None))
    assert result["gate_config_warnings"] == list(_RUNTIME["gate_config_warnings"])
