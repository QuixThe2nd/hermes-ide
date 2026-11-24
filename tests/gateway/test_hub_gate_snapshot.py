"""Hub-attached Discord adapters enforce the ATTACHING profile's gate env.

Live incident (Nov 2026): under the shared Discord hub, hub-side adapters are built
from the attach payload alone — no profile scope is active in the hub process — so
connect()'s gate-env snapshot captured the hub process's os.environ (the launch/default
profile's values, e.g. a default-profile DISCORD_ALLOWED_BOTS). Secondary-profile
adapters then enforced the default profile's per-bot allowlist and silently dropped
every other bot author. The attaching gateway snapshots the gate env under the owning
profile's runtime scope and sends it in the attach payload; the hub-side snapshot must
reflect ONLY that payload, never the hub process's env.
"""

import asyncio
import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO / "internal" / "botallowlist"

_LISTED = "424242424242424242"
_UNLISTED = "131313131313131313"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    for var in (
        "DISCORD_ALLOW_BOTS", "DISCORD_ALLOWED_BOTS", "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_bridge_state():
    """The bridge caches verdicts process-wide; each test starts cold."""
    module = importlib.import_module("gateway.bot_allowlist")
    module._verdict_cache.clear()
    module._malformed_lists.clear()
    module._warned_unavailable = False
    module._warned_malformed = False
    module._bridge_failed_at = None
    yield


@pytest.fixture
def adapter_mod():
    sys.modules.pop("plugins.platforms.discord.adapter", None)
    return importlib.import_module("plugins.platforms.discord.adapter")


@pytest.fixture(scope="session")
def go_botallowlist(tmp_path_factory):
    """Build cmd/botallowlist once per session; skip when no Go toolchain."""
    if shutil.which("go") is None:
        pytest.skip("Go toolchain unavailable")
    binary = tmp_path_factory.mktemp("botallowlist") / "botallowlist"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/botallowlist"],
        cwd=_MODULE_DIR, capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/botallowlist failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture
def bridge(go_botallowlist, monkeypatch):
    """The real bridge pinned to the session-built Go binary."""
    module = importlib.import_module("gateway.bot_allowlist")
    monkeypatch.setattr(module, "_bridge_binary", go_botallowlist)
    return module


def _hub_adapter(adapter_mod, *, extra=None, gate_env=None):
    """A hub-side adapter shell: gate snapshot seeded from the attach payload only.

    ``gate_env`` is the sanitized payload map the hub pins on the adapter before
    connect(); ``{}`` models an attaching profile whose scope holds no gate vars.
    """
    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter.config = SimpleNamespace(extra=extra or {})
    adapter._gate_env_snapshot = None
    adapter._hub_gate_env = gate_env
    adapter._snapshot_gate_env()
    return adapter


# ── adapter contract: the payload is authoritative, hub os.environ never consulted ──


def test_hub_adapter_ignores_process_env_allowed_bots(adapter_mod, monkeypatch):
    """extra carries allow_bots only; a polluted hub-process DISCORD_ALLOWED_BOTS
    must not narrow another profile's bot authors."""
    monkeypatch.setenv("DISCORD_ALLOWED_BOTS", _LISTED)
    adapter = _hub_adapter(adapter_mod, extra={"allow_bots": "mentions"}, gate_env={})
    assert adapter._get_allow_bots() == "mentions"
    assert adapter._get_allowed_bots() is None
    assert adapter._allowed_bots_admits(_UNLISTED) is True


def test_hub_adapter_extra_allowed_bots_beats_process_env(adapter_mod, monkeypatch, bridge):
    """The attach payload's per-profile list wins over the hub process's env."""
    monkeypatch.setenv("DISCORD_ALLOWED_BOTS", "222")
    adapter = _hub_adapter(
        adapter_mod, extra={"allowed_bots": [_LISTED]}, gate_env={})
    assert adapter._allowed_bots_admits(_LISTED) is True
    assert adapter._allowed_bots_admits(_UNLISTED) is False
    assert adapter._allowed_bots_admits("222") is False


def test_hub_adapter_payload_env_wins_over_extra(adapter_mod, bridge):
    """A gate value the attaching gateway resolved under its scope keeps the
    documented env-over-YAML precedence hub-side; extra is the fallback."""
    adapter = _hub_adapter(
        adapter_mod,
        extra={"allowed_bots": [_LISTED]},
        gate_env={"DISCORD_ALLOWED_BOTS": "222"},
    )
    assert adapter._allowed_bots_admits("222") is True
    assert adapter._allowed_bots_admits(_LISTED) is False


def test_scoped_snapshot_wins_over_extra_on_direct_connect(adapter_mod):
    """Non-hub contract pin: a connect()-time snapshot taken under the owning
    profile's scope remains authoritative; extra stays the fallback."""
    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter.config = SimpleNamespace(extra={"allowed_bots": [_LISTED]})
    adapter._hub_gate_env = None
    adapter._gate_env_snapshot = {key: "" for key in adapter_mod._GATE_ENV_KEYS}
    adapter._gate_env_snapshot["DISCORD_ALLOWED_BOTS"] = "222"
    assert adapter._get_allowed_bots() == "222"


def test_extra_allowed_bots_list_shape_round_trips(adapter_mod, bridge):
    """A YAML-shaped list in extra admits a listed id and denies an unlisted one
    through the real _allowed_bots_admits."""
    adapter = _hub_adapter(
        adapter_mod, extra={"allowed_bots": [_LISTED]}, gate_env={})
    assert adapter._allowed_bots_admits(_LISTED) is True
    assert adapter._allowed_bots_admits(_UNLISTED) is False


# ── hub wiring: the attach payload seeds the adapter's gate snapshot ──


class _StubAdapter:
    """Discord transport stand-in; records what the hub pins on it."""

    def __init__(self, config):
        self._owner_profile = None
        self._hub_gate_credential = None

    async def connect(self):
        return True

    async def disconnect(self):
        return True

    def _capture_response_gate_credential(self):
        pass

    def _reaction_gate_start_reload_watcher(self):
        pass


@pytest.fixture
def stub_adapter_class(monkeypatch):
    import plugins.platforms.discord.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "DiscordAdapter", _StubAdapter)
    return _StubAdapter


def _attach(server, generation, profile, **kwargs):
    return server._attach_adapter(generation, {"token": "fake-token"}, profile, [], None, **kwargs)


def test_attach_seeds_gate_env_from_payload(stub_adapter_class):
    """The hub pins the payload's gate values on the adapter before connect();
    unknown keys and non-string values never cross onto the snapshot."""
    from plugins.platforms.discord.hub import DiscordHubServer, _token_key

    server = DiscordHubServer(None)
    run(_attach(
        server, "gen-1", "winnie",
        gate_env={
            "DISCORD_ALLOWED_BOTS": "111",
            "DISCORD_ALLOW_BOTS": "mentions",
            "DISCORD_ALLOW_ALL_USERS": 1,  # non-string: dropped
            "SOME_UNKNOWN_VAR": "x",  # not a gate key: dropped
        },
    ))
    adapter = server._adapters[_token_key("fake-token")]
    assert adapter._hub_gate_env == {
        "DISCORD_ALLOWED_BOTS": "111",
        "DISCORD_ALLOW_BOTS": "mentions",
    }


def test_legacy_attach_without_gate_env_keeps_env_snapshot(stub_adapter_class):
    """An old gateway sends no gate env: the hub must not invent one — the adapter
    keeps its legacy connect()-time env snapshot behavior."""
    from plugins.platforms.discord.hub import DiscordHubServer, _token_key

    server = DiscordHubServer(None)
    run(_attach(server, "gen-1", "default"))
    adapter = server._adapters[_token_key("fake-token")]
    assert getattr(adapter, "_hub_gate_env", None) is None


@pytest.mark.platforms("posix")
def test_scoped_gate_env_crosses_the_wire_to_the_hub_adapter(
    tmp_path, monkeypatch, short_af_unix_path, stub_adapter_class,
):
    """Incident replay over the real socket: a secondary profile's scoped
    DISCORD_ALLOWED_BOTS reaches the hub-side adapter while the hub process's
    os.environ carries the default profile's conflicting value."""
    from agent.secret_scope import set_multiplex_active
    from gateway.config import PlatformConfig
    from gateway.run import _profile_runtime_scope
    from plugins.platforms.discord.hub import DiscordHubServer, _token_key
    from plugins.platforms.discord.hub_client import DiscordHubClientAdapter

    hub_home = tmp_path / "hub"
    home_b = tmp_path / "profiles" / "beta"
    for home in (hub_home, home_b):
        home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hub_home))
    # The hub process's env holds the DEFAULT profile's value; beta's scope holds its own.
    monkeypatch.setenv("DISCORD_ALLOWED_BOTS", "999")
    set_multiplex_active(True)

    async def scenario():
        server = DiscordHubServer(short_af_unix_path)
        await server.start()
        client = DiscordHubClientAdapter(PlatformConfig(enabled=True, token="fake-token"))
        client._socket_path = server.socket_path
        client._owner_profile = "beta"
        try:
            with _profile_runtime_scope(
                home_b, prepared_secret_scope={"DISCORD_ALLOWED_BOTS": _LISTED},
            ):
                assert await client.connect()
            adapter = server._adapters[_token_key("fake-token")]
            assert adapter._hub_gate_env["DISCORD_ALLOWED_BOTS"] == _LISTED
        finally:
            await client.disconnect()
            await server.stop()

    try:
        run(scenario())
    finally:
        set_multiplex_active(False)
