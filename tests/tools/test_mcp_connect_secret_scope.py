"""MCP connect resolves credentials under the connection owner's profile secret scope.

Discovery at gateway startup runs outside any per-turn scope. Under multiplexing an unscoped
``get_secret`` fails closed, so the stdio child-env build raised ``UnscopedSecretError`` and the
server registered zero tools (#113746).
"""
import asyncio

import pytest

from agent.secret_scope import (
    UnscopedSecretError, current_secret_scope, reset_secret_scope, set_multiplex_active,
    set_secret_scope,
)
from hermes_cli import env_loader
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_tool_discovery as discovery
from tools.mcp_tool_config import _build_safe_env

TOKEN_NAME, TOKEN_VALUE = "EXAMPLE_TOKEN", "profile-own-value"


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """A profile home whose ``.env`` holds the credential an external source tagged."""
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text(f"{TOKEN_NAME}={TOKEN_VALUE}\n")

    # An external secret source (secrets.command / bitwarden / 1password) tags names
    # process-wide; the VALUE must come from the active profile's scope.
    monkeypatch.setitem(env_loader._SECRET_SOURCES, TOKEN_NAME, "command")

    home_token = set_hermes_home_override(str(home))
    set_multiplex_active(True)
    try:
        yield home
    finally:
        set_multiplex_active(False)
        reset_hermes_home_override(home_token)


@pytest.fixture
def spawn_env(monkeypatch):
    """Stub server task: ``start()`` builds the stdio child env the way the transport does."""
    captured = {}

    class _StubServerTask:
        def __init__(self, name):
            self.name = name

        async def start(self, config):
            captured["env"] = _build_safe_env(config.get("env"))

        async def shutdown(self):
            pass

    # Origin state is read through _core; patch the origin module (see mcp_tool_discovery docstring).
    monkeypatch.setattr("tools.mcp_tool.MCPServerTask", _StubServerTask)
    return captured


def test_connect_resolves_the_owning_profiles_secret(profile_home, spawn_env):
    """Red on base: UnscopedSecretError. The child env must carry the profile's own value."""
    assert current_secret_scope() is None  # discovery runs unscoped

    asyncio.run(discovery._connect_server("demo", {"command": "true"}))

    assert spawn_env["env"][TOKEN_NAME] == TOKEN_VALUE


def test_connect_does_not_override_an_installed_scope(profile_home, spawn_env):
    """A caller that already scoped the turn owns the credentials — never re-derive (#111151)."""
    token = set_secret_scope({TOKEN_NAME: "callers-value"})
    try:
        asyncio.run(discovery._connect_server("demo", {"command": "true"}))
    finally:
        reset_secret_scope(token)

    assert spawn_env["env"][TOKEN_NAME] == "callers-value"


def test_connect_leaves_the_scope_unchanged_for_the_caller(profile_home, spawn_env):
    """The binding is the run task's, not the caller's: discovery is unscoped again afterwards."""
    asyncio.run(discovery._connect_server("demo", {"command": "true"}))

    assert current_secret_scope() is None


def test_single_profile_process_binds_nothing(tmp_path, monkeypatch, spawn_env):
    """No multiplexer, no home override: unchanged, and get_secret still reads os.environ."""
    monkeypatch.setitem(env_loader._SECRET_SOURCES, TOKEN_NAME, "command")
    monkeypatch.setenv(TOKEN_NAME, "process-env-value")
    set_multiplex_active(False)

    asyncio.run(discovery._connect_server("demo", {"command": "true"}))

    assert spawn_env["env"][TOKEN_NAME] == "process-env-value"
    assert current_secret_scope() is None


def test_unscoped_build_still_fails_closed_without_the_binding(profile_home):
    """The underlying guard is intact: the fix installs a scope, it does not widen get_secret."""
    with pytest.raises(UnscopedSecretError):
        _build_safe_env(None)
