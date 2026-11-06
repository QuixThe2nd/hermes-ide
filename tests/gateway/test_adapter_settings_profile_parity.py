"""Standalone-vs-served parity for adapter SETTINGS (not credentials) under ``gateway.multiplex_profiles``.

A served secondary profile's adapter is constructed inside ``_profile_runtime_scope`` while ``os.environ``
still holds the DEFAULT profile's ``.env``. Every non-credential setting an adapter reads with a bare
``os.getenv`` (ports, hosts, mention gating, reactions, thread policy, notification targets) therefore
resolves to the default profile's value — the adapter behaves differently served than it does standalone.

The invariant: with the SAME profile ``.env`` the adapter resolves the SAME setting whether the profile is
the ambient process home (standalone) or a scoped secondary (served). Each row names the setting and the
callable that resolves it from a constructed adapter, so a regression is reported by setting name.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agent import secret_scope as ss
from gateway.config import PlatformConfig


@pytest.fixture(autouse=True)
def _multiplex_off_after():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def _served(monkeypatch, default_env: dict, secondary_env: dict, build):
    """``(standalone_value, served_value)`` for one setting: standalone = the secondary's env IS the
    process env; served = default's env in the process, secondary's only in the scope."""
    for k, v in secondary_env.items():
        monkeypatch.setenv(k, v)
    standalone = build()
    for k in secondary_env:
        monkeypatch.delenv(k, raising=False)
    for k, v in default_env.items():
        monkeypatch.setenv(k, v)
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(dict(secondary_env))
    try:
        served = build()
    finally:
        ss.reset_secret_scope(token)
    return standalone, served


# (module, env var, default-profile value, secondary value, resolver(module) -> value)
_SETTINGS = [
    ("plugins.platforms.matrix.adapter", "MATRIX_REQUIRE_MENTION", "true", "false",
     lambda m: m.MatrixAdapter._parse_require_mention(PlatformConfig(enabled=True))),
    ("plugins.platforms.matrix.adapter", "MATRIX_THREAD_REQUIRE_MENTION", "false", "true",
     lambda m: m.MatrixAdapter._parse_thread_require_mention(PlatformConfig(enabled=True))),
    ("plugins.platforms.matrix.adapter", "MATRIX_MAX_MESSAGE_LENGTH", "1111", "2222",
     lambda m: m._resolve_max_message_length(PlatformConfig(enabled=True))),
    ("plugins.platforms.matrix.adapter", "MATRIX_E2EE_MODE", "required", "off",
     lambda m: m._resolve_e2ee_mode({})),
    ("plugins.platforms.matrix.adapter", "MATRIX_ALLOW_PUBLIC_ROOMS", "true", "false",
     lambda m: m._env_truthy("MATRIX_ALLOW_PUBLIC_ROOMS")),
    ("plugins.platforms.buzz.adapter", "BUZZ_RELAY_URL", "wss://default.relay", "wss://secondary.relay",
     lambda m: (m._env_enablement() or {}).get("relay_url")),
    ("plugins.platforms.discord.adapter", "DISCORD_COMMAND_SYNC_POLICY", "full", "off",
     lambda m: _discord(m, "_get_discord_command_sync_policy")),
    ("plugins.platforms.discord.adapter", "DISCORD_ALLOW_MENTION_EVERYONE", "true", "false",
     lambda m: m._env_bool("DISCORD_ALLOW_MENTION_EVERYONE", False)),
]


def _discord(mod, method):
    adapter = object.__new__(mod.DiscordAdapter)
    adapter.config = PlatformConfig(enabled=True)
    adapter.platform = adapter.config and __import__("gateway.config", fromlist=["Platform"]).Platform.DISCORD
    return getattr(adapter, method)()


@pytest.mark.parametrize(("module", "var", "default_value", "secondary_value", "resolve"), _SETTINGS,
                         ids=[f"{m.rsplit('.', 2)[-2]}:{v}" for m, v, *_ in _SETTINGS])
def test_served_secondary_resolves_its_own_setting(monkeypatch, module, var, default_value, secondary_value, resolve):
    mod = importlib.import_module(module)
    for name in (var, "BUZZ_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    creds = {"BUZZ_PRIVATE_KEY": "k"}
    standalone, served = _served(monkeypatch, {var: default_value, **creds}, {var: secondary_value, **creds},
                                 lambda: resolve(mod))
    assert served == standalone, f"{var}: served={served!r} standalone={standalone!r}"
