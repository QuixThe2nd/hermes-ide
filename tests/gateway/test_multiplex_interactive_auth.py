"""Multiplex interactive-auth regressions (#86296, #92840, #72657, #87240 egress).

Real ``GatewayRunner`` methods on an ``object.__new__`` runner, real
``PairingStore`` files under a temp HERMES_HOME, multiplex active.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.profile_routing import ProfileRoute


@pytest.fixture
def mux_home(tmp_path, monkeypatch):
    from agent import secret_scope

    home = tmp_path / "hh"
    (home / "profiles" / "secondary").mkdir(parents=True)
    (home / ".env").write_text("")
    (home / "profiles" / "secondary" / ".env").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in (
        "DISCORD_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(key, raising=False)
    prev = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    yield home
    secret_scope.set_multiplex_active(prev)


def _runner(home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.profile_routes = [
        ProfileRoute(name="r", platform="discord", chat_id="-100555", profile="secondary")
    ]
    runner.config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {
        "default": runner.pairing_store,
        "secondary": PairingStore(profile="secondary"),
    }
    runner._primary_profile_name = "default"
    runner._profile_adapters = {"secondary": {}}
    return runner


def test_authorization_adapter_ignores_per_turn_active_profile(mux_home):
    """#87240 egress half: inside a secondary profile's runtime scope the
    default bot must not be handed to that profile (fail-closed None); the
    launch profile still resolves ``self.adapters``. The secondary's own bot
    is down (reconnect pending), so it is not a route-only satellite."""
    from gateway.run import _profile_runtime_scope

    runner = _runner(mux_home)
    default_bot = object()
    runner.adapters = {Platform.DISCORD: default_bot}
    runner._profile_failed_platforms = {"secondary": {Platform.DISCORD: object()}}

    with _profile_runtime_scope(mux_home / "profiles" / "secondary"):
        assert runner._authorization_adapter(Platform.DISCORD, profile="secondary") is None
    assert runner._authorization_adapter(Platform.DISCORD, profile="default") is default_bot


def test_channel_directory_path_follows_current_home(mux_home):
    """#87240: the directory file resolves against the CURRENT profile home,
    not the home that happened to import the module."""
    import gateway.channel_directory as cd
    from gateway.run import _profile_runtime_scope

    assert cd.DIRECTORY_PATH is None
    with _profile_runtime_scope(mux_home / "profiles" / "secondary"):
        assert cd._directory_path() == Path(mux_home / "profiles" / "secondary" / "channel_directory.json")
    assert cd._directory_path() == Path(mux_home / "channel_directory.json")
