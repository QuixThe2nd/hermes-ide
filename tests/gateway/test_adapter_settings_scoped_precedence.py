"""Per-profile adapter settings resolve explicit scoped env → the profile's own YAML → default.

Invariants from the #108440 post-merge review: a multiplexed secondary constructed under the real
``_profile_runtime_scope`` reads its own YAML lists/flags (Matrix authz lists, Discord mentions), never inherits the launch process's env on a scoped miss, and the central
``allow_bots`` gate agrees with the adapter; an explicit env value still beats YAML for the owning
profile (single-profile contract). Real loader, real constructors; only transport is substituted.
"""

from __future__ import annotations

import asyncio
import contextlib
import types
from unittest.mock import AsyncMock, patch

import pytest

from agent.secret_scope import set_multiplex_active
from gateway.config import Platform, load_gateway_config
from hermes_cli.plugins import discover_plugins


@pytest.fixture(autouse=True)
def _plugins():
    discover_plugins()


class _Mentions:
    """``discord.AllowedMentions`` stand-in exposing the four flags (the suite stubs ``discord``)."""

    def __init__(self, *, everyone, roles, users, replied_user):
        self.everyone, self.roles, self.users, self.replied_user = everyone, roles, users, replied_user


def _allowed_mentions(extra):
    from plugins.platforms.discord import adapter as discord_adapter
    with patch.object(discord_adapter, "DISCORD_AVAILABLE", True), \
            patch.object(discord_adapter, "discord", types.SimpleNamespace(AllowedMentions=_Mentions), create=True):
        return discord_adapter._build_allowed_mentions(extra)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """(launch_home, secondary_home); HERMES_HOME points at the launch home, multiplex off on exit."""
    launch = tmp_path / "launch"
    secondary = tmp_path / "launch" / "profiles" / "b2"
    secondary.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    yield launch, secondary
    set_multiplex_active(False)


@contextlib.contextmanager
def _secondary_scope(home):
    from gateway.run import _profile_runtime_scope
    set_multiplex_active(True)
    try:
        with _profile_runtime_scope(home, prepared_secret_scope={}):
            yield
    finally:
        set_multiplex_active(False)


def test_secondary_reads_own_yaml_and_never_the_launch_env(homes, monkeypatch):
    launch, secondary = homes
    (launch / "config.yaml").write_text(
        "matrix:\n  process_notices: true\n  session_scope: room\n"
        "discord:\n  reactions: false\n  allow_mentions:\n    everyone: true\n", encoding="utf-8")
    load_gateway_config()  # launch profile bridges its YAML into os.environ (single-profile contract)
    (secondary / "config.yaml").write_text(
        "matrix:\n  enabled: true\n  user_id: '@bot:example.org'\n"
        "  allowed_users: ['@owner:example.org']\n  ignore_user_patterns: ['^@ignored:']\n"
        "discord:\n  enabled: true\n", encoding="utf-8")
    from plugins.platforms.discord.adapter import DiscordAdapter
    from plugins.platforms.matrix.adapter import MatrixAdapter
    with _secondary_scope(secondary):
        cfg = load_gateway_config()
        m = MatrixAdapter(cfg.platforms[Platform.MATRIX])
        d = DiscordAdapter(cfg.platforms[Platform.DISCORD])
        # Omitted keys resolve to the DEFAULT, not the launch profile's bridged env.
        assert (m._process_notices, m._matrix_session_scope) == (False, "auto")
        assert d._reactions_enabled()
        assert _allowed_mentions(d.config.extra).everyone is False
        # The seeded YAML lists reach the Matrix authz consumers.
        assert m._allowed_user_ids == {"@owner:example.org"} and len(m._ignored_user_patterns) == 1


def test_explicit_env_beats_yaml_for_the_owning_profile(homes, monkeypatch):
    """Single-profile / owning-profile contract: DISCORD_ALLOW_MENTION_EVERYONE=false beats
    ``everyone: true`` (#109032)."""
    launch, _ = homes
    (launch / "config.yaml").write_text(
        "discord:\n  allow_mentions:\n    everyone: true\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_ALLOW_MENTION_EVERYONE", "false")
    cfg = load_gateway_config()
    assert _allowed_mentions(cfg.platforms[Platform.DISCORD].extra).everyone is False


def test_central_allow_bots_gate_honours_a_secondary_yaml_policy(homes):
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from plugins.platforms.discord.adapter import DiscordAdapter
    _, secondary = homes
    (secondary / "config.yaml").write_text(
        "discord:\n  enabled: true\n  allow_bots: all\n", encoding="utf-8")
    with _secondary_scope(secondary):
        cfg = load_gateway_config()
        d = DiscordAdapter(cfg.platforms[Platform.DISCORD])
        runner = object.__new__(GatewayRunner)
        runner.config, runner._primary_profile_name, runner.adapters = cfg, "default", {}
        runner._profile_adapters = {"b2": {Platform.DISCORD: d}}
        for platform, user_id in ((Platform.DISCORD, "123"),):
            src = SessionSource(platform=platform, chat_id="C_TEST", chat_type="group", user_id=user_id,
                                is_bot=True, profile="b2")
            assert runner._is_user_authorized(src), platform


def test_matrix_yaml_lists_gate_intake_and_approval(homes):
    from plugins.platforms.matrix.adapter import MatrixAdapter
    _, secondary = homes
    (secondary / "config.yaml").write_text(
        "matrix:\n  enabled: true\n  user_id: '@bot:example.org'\n"
        "  allowed_users: ['@owner:example.org']\n  ignore_user_patterns: ['^@ignored:']\n", encoding="utf-8")
    with _secondary_scope(secondary):
        a = MatrixAdapter(load_gateway_config().platforms[Platform.MATRIX])
        a._user_id = "@bot:example.org"
        a._is_allowed_matrix_room_event = AsyncMock(return_value=True)
        a._handle_text_message = AsyncMock()
        a.send = AsyncMock()
        event = types.SimpleNamespace(room_id="!r:example.org", sender="@ignored:example.org", event_id="$1",
                                      content={"msgtype": "m.text", "body": "hello"})
        asyncio.run(a._on_room_message(event))
        prompt = types.SimpleNamespace(requester_user_id="@owner:example.org")
        owner_ok = asyncio.run(a._validate_matrix_prompt_reactor("!r:example.org", "$a", "@owner:example.org", prompt, "approval"))
        other_ok = asyncio.run(a._validate_matrix_prompt_reactor("!r:example.org", "$a", "@other:example.org", prompt, "approval"))
    assert a._handle_text_message.await_count == 0 and owner_ok and not other_ok


def test_whatsapp_bridge_env_carries_the_secondary_effective_policy(homes, monkeypatch):
    """bridge.js gates DMs and group intake before Python: it must receive the adapter's resolved
    dm_policy/allow_from/group_policy, not the launch process's WHATSAPP_* values."""
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    _, secondary = homes
    monkeypatch.setenv("WHATSAPP_DM_POLICY", "allowlist")
    monkeypatch.setenv("WHATSAPP_GROUP_POLICY", "disabled")
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "15550001111")
    monkeypatch.setenv("WHATSAPP_GROUP_ALLOWED_USERS", "120363000000000000@g.us")
    (secondary / "config.yaml").write_text(
        "whatsapp:\n  enabled: true\n  dm_policy: pairing\n  group_policy: allowlist\n"
        "  group_allow_from: [120363001234567890@g.us]\n", encoding="utf-8")
    with _secondary_scope(secondary):
        a = WhatsAppAdapter(load_gateway_config().platforms[Platform.WHATSAPP])
        env = a._bridge_env()
    assert a._dm_policy == "pairing" == env["WHATSAPP_DM_POLICY"]
    assert a._group_policy == "allowlist" == env["WHATSAPP_GROUP_POLICY"]
    assert env["WHATSAPP_GROUP_ALLOWED_USERS"] == "120363001234567890@g.us"
    assert "WHATSAPP_ALLOWED_USERS" not in env
