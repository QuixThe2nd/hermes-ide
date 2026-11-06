"""Platform authorization reads must honor the per-profile secret scope (#93522).

Under ``gateway.multiplex_profiles`` every secondary profile is constructed
inside ``_profile_runtime_scope`` and its ``.env`` lives in that scope —
``gateway/run.py`` explicitly does NOT mutate ``os.environ`` with it. Raw
``os.getenv`` authorization reads therefore (a) silently miss the secondary
profile's own allowlists (fail-closed no-replies) and (b) leak the default
profile's ``GATEWAY_ALLOW_ALL_USERS`` / allowlists into secondary profiles
(fail-open admissions).
"""

import contextlib
import os

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope


@contextlib.contextmanager
def _scope(secrets):
    token = set_secret_scope(secrets)
    try:
        yield
    finally:
        reset_secret_scope(token)


@pytest.fixture
def multiplex_on(monkeypatch):
    """Simulate a multiplexed deployment: scoped misses fail closed.

    With multiplexing off, a scope miss deliberately falls through to
    os.environ (the cron-overlay contract) — the cross-profile leak only
    exists while the multiplexer is active, which is exactly the #93522
    scenario.
    """
    monkeypatch.setattr("agent.secret_scope._MULTIPLEX_ACTIVE", True)


def test_startup_guard_uses_scoped_gateway_flag(multiplex_on):
    """_own_policy_open_startup_violation must consult the scope for
    GATEWAY_ALLOW_ALL_USERS, not raw os.environ (#93522)."""
    from gateway.platforms.base import Platform
    from gateway.run import _own_policy_open_startup_violation

    class _PlatformConfig:
        enabled = True
        extra = {"dm_policy": "open"}

    class _Config:
        platforms = {Platform.WHATSAPP: _PlatformConfig()}

    cfg = _Config()
    with _scope({"GATEWAY_ALLOW_ALL_USERS": "true"}):
        # Scope says opted-in even though os.environ doesn't -> no violation.
        assert _own_policy_open_startup_violation(cfg) is None

    with _scope({}):
        # Scope lacks the opt-in while os.environ has it -> violation (the
        # default profile's flag must not satisfy this profile's check).
        monkey_env_backup = os.environ.get("GATEWAY_ALLOW_ALL_USERS")
        os.environ["GATEWAY_ALLOW_ALL_USERS"] = "true"
        try:
            reason = _own_policy_open_startup_violation(cfg)
            assert reason is not None and "whatsapp" in reason.lower()
        finally:
            if monkey_env_backup is None:
                os.environ.pop("GATEWAY_ALLOW_ALL_USERS", None)
            else:
                os.environ["GATEWAY_ALLOW_ALL_USERS"] = monkey_env_backup
