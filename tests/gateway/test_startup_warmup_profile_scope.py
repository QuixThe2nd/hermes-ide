"""Boot probes run inside the launch profile's scope under multiplex.

``get_tool_definitions`` runs every ``check_fn``; probes may resolve
profile-scoped credentials whose overrides read through
``agent.secret_scope.get_secret``. With multiplex active and no scope on the
executor thread that read fails closed, so the override is absent. The warm-up
must run under the same binding a routed turn gets, and the binding must reach
the executor thread.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from agent import secret_scope
from gateway import run as gateway_run
from gateway.run_startup import GatewayStartupMixin

MARKER_URL = "https://profile-scoped.example.invalid"
MARKER_VAR = "HERMES_PROFILE_MARKER_URL"


@pytest.fixture
def multiplex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(f"{MARKER_VAR}={MARKER_URL}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv(MARKER_VAR, raising=False)
    secret_scope.set_multiplex_active(True)
    try:
        yield home
    finally:
        secret_scope.set_multiplex_active(False)


def _probe(seen: dict):
    def probe() -> int:
        seen["scope_installed"] = secret_scope._SECRET_SCOPE.get() is not None
        seen["marker"] = secret_scope.get_secret(MARKER_VAR)
        return 1
    return probe


class _Runner(GatewayStartupMixin):
    def __init__(self, *, multiplex: bool):
        self.config = types.SimpleNamespace(multiplex_profiles=multiplex)


def _run_warmup(monkeypatch, *, multiplex: bool) -> dict:
    """Drive ``_warm_turn_prerequisites`` with the sync warm-up replaced by a probe that records what
    the executor thread can see, then what the loop task sees once the warm-up has returned."""
    seen: dict = {}
    monkeypatch.setattr(gateway_run, "_warm_turn_machinery_sync", _probe(seen))

    async def drive() -> None:
        await _Runner(multiplex=multiplex)._warm_turn_prerequisites()
        # Same task as the warm-up (asyncio.run copies the context, so the caller's view proves nothing).
        from hermes_constants import get_hermes_home_override
        seen["scope_after"] = secret_scope._SECRET_SCOPE.get()
        seen["home_override_after"] = get_hermes_home_override()

    asyncio.run(drive())
    return seen


def test_multiplex_warmup_runs_check_fns_inside_the_launch_profile_scope(multiplex_home, monkeypatch):
    """The executor thread sees the launch profile's secret scope, so the profile-scoped override
    resolves exactly as it does on a routed turn — never the fail-closed 'absent' value. The
    binding is per activity: once the warm-up returns, the loop thread is unscoped again."""
    seen = _run_warmup(monkeypatch, multiplex=True)
    assert seen["scope_installed"] is True
    assert seen["marker"] == MARKER_URL
    assert seen["scope_after"] is None
    assert seen["home_override_after"] is None


def test_single_profile_warmup_keeps_environ_semantics(tmp_path, monkeypatch):
    """Multiplex off: no scope is installed and the process env stays the override source."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(MARKER_VAR, MARKER_URL)
    secret_scope.set_multiplex_active(False)
    seen = _run_warmup(monkeypatch, multiplex=False)
    assert seen["scope_installed"] is False
    assert seen["marker"] == MARKER_URL
