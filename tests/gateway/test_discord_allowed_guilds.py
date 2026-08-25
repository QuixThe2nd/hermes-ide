"""DISCORD_ALLOWED_GUILDS: guild-level authorization gate (two layers).

Operators want per-server whitelisting: any member of a listed guild may talk
to the bot, without enumerating every channel or user ID. The gate is
enforced twice, and both layers are per-profile isolated under
``gateway.multiplex_profiles`` (issue #72348):

  Layer 1 — ``DiscordAdapter._is_allowed_user``: guild traffic from a listed
  server is an independent grant, valid whether or not the user/role
  allowlists are also set. DMs carry no guild and are NEVER granted here.
  Layer 2 — ``GatewayRunner._is_user_authorized`` (gateway/authz_mixin.py):
  the second pass after routing authorizes group/forum/channel Discord
  sources whose ``guild_id`` is listed, reading the gate through ``_auth_env``
  so a multiplexed profile consults its own secret scope.

These tests pin the gate so it cannot silently widen to DMs, regress the
existing user/role/channel paths when unset, or leak across profiles.
"""

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Discord module mock — borrowed from test_discord_slash_auth.py so this
# file runs on machines without discord.py installed.
# ---------------------------------------------------------------------------


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return  # real discord installed

    if sys.modules.get("discord") is None:
        discord_mod = MagicMock()
        discord_mod.Intents.default.return_value = MagicMock()
        discord_mod.DMChannel = type("DMChannel", (), {})
        discord_mod.Thread = type("Thread", (), {})
        discord_mod.ForumChannel = type("ForumChannel", (), {})
        discord_mod.Interaction = object

        class _FakePermissions:
            def __init__(self, value=0, **_):
                self.value = value

        discord_mod.Permissions = _FakePermissions

        class _FakeGroup:
            def __init__(self, *, name, description, parent=None):
                self.name = name
                self.description = description
                self.parent = parent
                self._children: dict[str, object] = {}
                if parent is not None:
                    parent.add_command(self)

            def add_command(self, cmd):
                self._children[cmd.name] = cmd

        class _FakeCommand:
            def __init__(self, *, name, description, callback, parent=None):
                self.name = name
                self.description = description
                self.callback = callback
                self.parent = parent
                self.default_permissions = None

        discord_mod.app_commands = SimpleNamespace(
            describe=lambda **kwargs: (lambda fn: fn),
            choices=lambda **kwargs: (lambda fn: fn),
            autocomplete=lambda **kwargs: (lambda fn: fn),
            Choice=lambda **kwargs: SimpleNamespace(**kwargs),
            Group=_FakeGroup,
            Command=_FakeCommand,
        )

        ext_mod = MagicMock()
        commands_mod = MagicMock()
        commands_mod.Bot = MagicMock
        ext_mod.commands = commands_mod

        sys.modules["discord"] = discord_mod
        sys.modules.setdefault("discord.ext", ext_mod)
        sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


GATE_VARS = [
    "DISCORD_ALLOWED_GUILDS",
    "DISCORD_ALLOWED_USERS",
    "DISCORD_ALLOWED_ROLES",
    "DISCORD_ALLOWED_CHANNELS",
    "DISCORD_IGNORED_CHANNELS",
    "DISCORD_ALLOW_ALL_USERS",
    "DISCORD_ALLOW_BOTS",
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS",
]


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    for var in GATE_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    # monkeypatch.delenv on an ABSENT var records nothing, so env writes made
    # during the test (e.g. _apply_yaml_config's legacy bridge) would leak
    # into later test modules. Scrub explicitly (same guard as
    # test_discord_gate_isolation.py).
    for var in GATE_VARS:
        os.environ.pop(var, None)


@pytest.fixture(autouse=True)
def _no_pairing_grants(monkeypatch):
    """Pin the pairing store to never-approve so the guild gate — not local
    pairing state on the host running the tests — decides the outcome."""
    import gateway.pairing as pairing

    class _FakePairingStore:
        def is_approved(self, platform, user_id):
            return False

    monkeypatch.setattr(pairing, "PairingStore", _FakePairingStore)


def _adapter(extra: dict | None = None) -> DiscordAdapter:
    """Bare adapter via object.__new__ (AGENTS.md pitfall #17) — mirrors the
    fixture in test_discord_gate_isolation.py."""
    from gateway.config import Platform

    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD  # drives the read-only `name` property
    adapter.config = SimpleNamespace(extra=dict(extra or {}))
    adapter._gate_env_snapshot = None
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    adapter._warned_fail_closed_default = False
    return adapter


def _guild(gid=42):
    return SimpleNamespace(id=gid, get_member=lambda _uid: None)


# ---------------------------------------------------------------------------
# Layer 1: adapter guild grant
# ---------------------------------------------------------------------------


def test_guild_in_allowlist_grants_with_no_users_or_roles(monkeypatch):
    """A member of a listed guild is authorized with nothing else configured."""
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    assert _adapter()._is_allowed_user(
        "999888777",
        author=SimpleNamespace(id=999888777),
        guild=_guild(42),
        is_dm=False,
        channel_ids={"12345"},
    ) is True


def test_guild_not_in_allowlist_denied(monkeypatch):
    """An unlisted guild gets no grant when nothing else authorizes either."""
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    assert _adapter()._is_allowed_user(
        "999888777",
        author=SimpleNamespace(id=999888777),
        guild=_guild(777),
        is_dm=False,
        channel_ids={"12345"},
    ) is False


def test_user_allowlist_still_grants_when_guild_not_listed(monkeypatch):
    """The DISCORD_ALLOWED_USERS path is unbroken by the guild gate."""
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    adapter = _adapter()
    adapter._allowed_user_ids = {"100200300"}
    assert adapter._is_allowed_user(
        "100200300",
        author=SimpleNamespace(id=100200300),
        guild=_guild(777),
        is_dm=False,
    ) is True


def test_dm_unaffected_by_guild_list(monkeypatch):
    """DMs carry no guild: a non-empty guild list must not authorize one."""
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    assert _adapter()._is_allowed_user(
        "999888777",
        author=SimpleNamespace(id=999888777),
        guild=None,
        is_dm=True,
    ) is False


def test_guild_grant_beats_user_allowlist_for_non_listed_sender(monkeypatch):
    """The guild grant is independent: it applies even when
    DISCORD_ALLOWED_USERS is configured and the sender is NOT in it."""
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    adapter = _adapter()
    adapter._allowed_user_ids = {"100200300"}
    assert adapter._is_allowed_user(
        "999888777",
        author=SimpleNamespace(id=999888777),
        guild=_guild(42),
        is_dm=False,
    ) is True


def test_wildcard_guild_grant(monkeypatch):
    '''"*" authorizes guild traffic from any server (DISCORD_ALLOWED_CHANNELS
    wildcard convention); DMs still get no grant.'''
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "*")
    adapter = _adapter()
    assert adapter._is_allowed_user(
        "999888777", author=SimpleNamespace(id=999888777),
        guild=_guild(777), is_dm=False,
    ) is True
    assert adapter._is_allowed_user(
        "999888777", author=SimpleNamespace(id=999888777),
        guild=None, is_dm=True,
    ) is False


def test_unset_guild_list_keeps_existing_paths(monkeypatch):
    """No DISCORD_ALLOWED_GUILDS → byte-identical legacy behavior."""
    adapter = _adapter()
    # User allowlist still grants.
    adapter._allowed_user_ids = {"100200300"}
    assert adapter._is_allowed_user(
        "100200300", author=SimpleNamespace(id=100200300),
        guild=_guild(42), is_dm=False,
    ) is True
    # Channel-scoped grant still works with no user/role allowlists.
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "5555")
    adapter._allowed_user_ids = set()
    assert adapter._is_allowed_user(
        "999888777", author=SimpleNamespace(id=999888777),
        guild=_guild(42), is_dm=False, channel_ids={"5555"},
    ) is True
    # And a channel outside the allowlist still fails closed.
    assert adapter._is_allowed_user(
        "999888777", author=SimpleNamespace(id=999888777),
        guild=_guild(42), is_dm=False, channel_ids={"9999"},
    ) is False


def test_fail_closed_warning_silent_with_only_guild_list(monkeypatch, caplog):
    """A configured guild list counts as an allowlist: no fail-closed warning."""
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        adapter._warn_if_fail_closed_default()
    assert adapter._warned_fail_closed_default is False
    assert not any(
        "no allowlist is configured" in r.message for r in caplog.records
    )


def test_fail_closed_warning_still_fires_with_no_gates(caplog):
    """Without the guild list (or any other gate) the warning still fires."""
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        adapter._warn_if_fail_closed_default()
    assert adapter._warned_fail_closed_default is True
    assert any(
        "no allowlist is configured" in r.message for r in caplog.records
    )


def test_yaml_bridge_seeds_env_and_extra(monkeypatch):
    """discord.allowed_guilds (string or list) bridges to DISCORD_ALLOWED_GUILDS
    and seeds config.extra — the per-profile carrier under multiplex."""
    from plugins.platforms.discord.adapter import _apply_yaml_config

    seeded = _apply_yaml_config({}, {"allowed_guilds": ["42", "43"]})
    assert seeded["allowed_guilds"] == "42,43"
    assert os.environ["DISCORD_ALLOWED_GUILDS"] == "42,43"

    seeded = _apply_yaml_config({}, {"allowed_guilds": "77"})
    assert seeded["allowed_guilds"] == "77"


def test_yaml_bridge_skips_env_under_profile_scope(monkeypatch):
    """A profile-scoped load under multiplex must not write process env."""
    from agent import secret_scope
    from plugins.platforms.discord.adapter import _apply_yaml_config

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    token = secret_scope.set_secret_scope({"SOME": "scope"})
    try:
        seeded = _apply_yaml_config({}, {"allowed_guilds": "222"})
    finally:
        secret_scope.reset_secret_scope(token)

    assert seeded["allowed_guilds"] == "222"
    assert os.getenv("DISCORD_ALLOWED_GUILDS") is None
    # The seeded extra still carries the gate into the adapter.
    assert _adapter(seeded)._discord_allowed_guild_ids() == {"222"}


# ---------------------------------------------------------------------------
# Layer 2: gateway second pass (authz_mixin)
# ---------------------------------------------------------------------------


def _make_bare_runner():
    """GatewayRunner skeleton via object.__new__ (AGENTS.md pitfall #17),
    same shape as test_discord_bot_auth_bypass.py."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    return runner


def _make_discord_source(
    *, guild_id="42", chat_type="group", user_id="999888777",
):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="12345",
        chat_type=chat_type,
        user_id=user_id,
        user_name="SomeMember",
        guild_id=guild_id,
    )


def test_gateway_guild_grant_authorizes_group_source(monkeypatch):
    runner = _make_bare_runner()
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    assert runner._is_user_authorized(_make_discord_source(guild_id="42")) is True


def test_gateway_guild_grant_bypasses_user_allowlist_for_listed_guild(monkeypatch):
    """A listed guild authorizes a sender who is NOT in DISCORD_ALLOWED_USERS
    (the per-user list still gates every other server)."""
    runner = _make_bare_runner()
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "100200300")
    assert runner._is_user_authorized(_make_discord_source(guild_id="42")) is True


def test_gateway_non_matching_guild_falls_through_to_deny(monkeypatch):
    runner = _make_bare_runner()
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    assert runner._is_user_authorized(_make_discord_source(guild_id="777")) is False


def test_gateway_unset_guild_list_is_unchanged(monkeypatch):
    """Unset DISCORD_ALLOWED_GUILDS: existing logic decides — deny without
    other grants, allow for a DISCORD_ALLOWED_USERS member."""
    runner = _make_bare_runner()
    assert runner._is_user_authorized(_make_discord_source(guild_id="42")) is False
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "100200300")
    assert runner._is_user_authorized(
        _make_discord_source(guild_id="42", user_id="100200300")
    ) is True


def test_gateway_dm_source_gets_no_guild_grant(monkeypatch):
    """chat_type="dm" sources are outside the guild gate even with a
    non-empty list."""
    runner = _make_bare_runner()
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")
    assert runner._is_user_authorized(
        _make_discord_source(guild_id=None, chat_type="dm")
    ) is False


def test_gateway_wildcard_guild_grant(monkeypatch):
    runner = _make_bare_runner()
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "*")
    assert runner._is_user_authorized(_make_discord_source(guild_id="777")) is True


def test_gateway_guild_grant_reads_profile_scope_not_process_env(monkeypatch):
    """Under multiplex the gate reads the profile's secret scope: another
    profile's first-writer process-env value must not authorize this
    profile's guild traffic (issue #72348 isolation)."""
    from agent import secret_scope

    runner = _make_bare_runner()
    # Default profile bridged guild 42 into process env first.
    monkeypatch.setenv("DISCORD_ALLOWED_GUILDS", "42")

    prev = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        # Secondary profile's own scope lists a different guild.
        token = secret_scope.set_secret_scope({"DISCORD_ALLOWED_GUILDS": "777"})
        try:
            assert runner._is_user_authorized(
                _make_discord_source(guild_id="42")
            ) is False, "default profile's env bridge must not leak in"
            assert runner._is_user_authorized(
                _make_discord_source(guild_id="777")
            ) is True, "the profile's own scoped list must be honored"
        finally:
            secret_scope.reset_secret_scope(token)
    finally:
        secret_scope.set_multiplex_active(prev)
