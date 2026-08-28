"""Tests for the bundled discord_guests plugin."""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import stat
import sys
import time
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_API_BASE = "https://discord.com/api/v10"

# Extra allow bit used to verify overwrite merge preserves unrelated permissions.
_EXTRA_EVERYONE_ALLOW = 1 << 11  # SEND_MESSAGES

# Discord permission flags.
_VIEW_CHANNEL = 1 << 10
_ADMINISTRATOR = 1 << 3
# The exact allow mask the plugin must write for a guest: view, send, history,
# reactions, embeds, attachments, in-thread sends — and nothing else.
_EXPECTED_GUEST_ALLOW = (
    (1 << 6)      # ADD_REACTIONS
    | (1 << 10)   # VIEW_CHANNEL
    | (1 << 11)   # SEND_MESSAGES
    | (1 << 14)   # EMBED_LINKS
    | (1 << 15)   # ATTACH_FILES
    | (1 << 16)   # READ_MESSAGE_HISTORY
    | (1 << 38)   # SEND_MESSAGES_IN_THREADS
)
# Bits the plugin must never grant: everything administrative.
_FORBIDDEN_BITS = (
    (1 << 1)      # KICK_MEMBERS
    | (1 << 2)    # BAN_MEMBERS
    | _ADMINISTRATOR
    | (1 << 4)    # MANAGE_CHANNELS
    | (1 << 5)    # MANAGE_GUILD
    | (1 << 13)   # MANAGE_MESSAGES
    | (1 << 17)   # MENTION_EVERYONE
    | (1 << 28)   # MANAGE_NICKNAMES
    | (1 << 29)   # MANAGE_WEBHOOKS
    | (1 << 34)   # MANAGE_THREADS
    | (1 << 40)   # MODERATE_MEMBERS
)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


@pytest.fixture
def discord_guests_module():
    repo_root = Path(__file__).resolve().parents[3]
    plugin_dir = repo_root / "plugins" / "discord_guests"
    module_name = "discord_guests_plugin_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "__init__.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    # Write pacing is real behavior — asserted directly in TestPacing — but is
    # zeroed here so the REST-mocked suite stays fast and deterministic.
    mod._WRITE_PACE_SECONDS = 0.0
    mod._last_write_at = 0.0
    return mod


@pytest.fixture
def token_env(_isolate_env):
    env_path = _isolate_env / ".env"
    env_path.write_text("DISCORD_BOT_TOKEN=test-bot-token\n", encoding="utf-8")
    return env_path


class MockDiscordRouter:
    """Routes the Discord REST calls the plugin makes, the way Discord answers.

    Channel creation is stateful (created lounges join the channel list so a
    second add finds them), permission overwrites are recorded per
    (channel, target), and destructive or out-of-contract requests — deleting
    a channel, writing to a role endpoint — fail the test outright.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.guilds: List[Dict[str, str]] = [{"id": "guild-1", "name": "Test Guild"}]
        self.roles: List[Dict[str, str]] = [
            {"id": "guild-1", "name": "@everyone", "permissions": "0"},
            {"id": "role-admin", "name": "Admins", "permissions": "8"},
            {"id": "role-mod", "name": "Mods", "permissions": "1088"},
        ]
        self.channels: List[Dict[str, Any]] = [
            {"id": "cat-chat", "name": "Chat", "type": 4, "parent_id": None},
            {"id": "cat-other", "name": "Other", "type": 4, "parent_id": None},
            {"id": "chan-inbox", "name": "inbox", "type": 0, "parent_id": "cat-chat"},
            {"id": "chan-general", "name": "general", "type": 0, "parent_id": "cat-other"},
            {"id": "chan-top", "name": "top-level", "type": 0, "parent_id": None},
        ]
        self.members_by_id: Dict[str, Dict[str, Any]] = {
            "111": {
                "nick": None,
                "roles": [],
                "user": {"id": "111", "username": "ada", "global_name": "Ada Lovelace"},
            },
            "222": {
                "nick": "Boss",
                "roles": ["role-admin"],
                "user": {"id": "222", "username": "grace", "global_name": None},
            },
            "333": {
                "nick": None,
                "roles": [],
                "user": {"id": "333", "username": "bob", "global_name": None},
            },
            "444": {
                "nick": None,
                "roles": [],
                "user": {"id": "444", "username": "winnie", "global_name": "Winnie"},
            },
        }
        # The bot itself, as /guilds/{gid}/members/@me answers.
        self.self_member: Dict[str, Any] = {
            "nick": "Big Steve",
            "roles": [],
            "user": {"id": "9001", "username": "hermes-agent", "global_name": "Hermes"},
        }
        self.search_results: List[Dict[str, Any]] = []
        self.overwrites: Dict[tuple, Dict[str, Any]] = {}
        self.deleted_channels: List[str] = []
        self.channel_counter = 0
        self.rate_limit_pending = 0
        self.permission_delete_404: set[tuple[str, str]] = set()

    def _sync_overwrite_to_channel(
        self, channel_id: str, overwrite_id: str, body: Dict[str, Any]
    ) -> None:
        for channel in self.channels:
            if str(channel.get("id") or "") != channel_id:
                continue
            overwrites = list(channel.get("permission_overwrites") or [])
            updated = False
            for idx, entry in enumerate(overwrites):
                if str(entry.get("id") or "") == overwrite_id:
                    overwrites[idx] = dict(body)
                    updated = True
                    break
            if not updated:
                overwrites.append(dict(body))
            channel["permission_overwrites"] = overwrites
            return

    def _remove_overwrite_from_channel(self, channel_id: str, overwrite_id: str) -> None:
        for channel in self.channels:
            if str(channel.get("id") or "") != channel_id:
                continue
            overwrites = [
                entry
                for entry in (channel.get("permission_overwrites") or [])
                if str(entry.get("id") or "") != overwrite_id
            ]
            channel["permission_overwrites"] = overwrites
            return

    def __call__(self, request):
        self.calls.append(
            {
                "method": request.method,
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": request.data.decode("utf-8") if request.data else None,
            }
        )
        url = request.full_url
        method = request.method

        if self.rate_limit_pending > 0 and method in {"POST", "PUT", "PATCH", "DELETE"}:
            self.rate_limit_pending -= 1
            raise urllib.error.HTTPError(
                url,
                429,
                "Too Many Requests",
                hdrs=None,
                fp=BytesIO(json.dumps({"retry_after": 0}).encode("utf-8")),
            )

        if method == "GET" and url.endswith("/users/@me/guilds"):
            return self._response(self.guilds)

        if method == "GET" and "/members/search" in url:
            return self._response(self.search_results)

        if method == "GET" and "/guilds/" in url and "/members/@me" in url:
            return self._response(self.self_member)

        if method == "GET" and "/guilds/" in url and "/members/" in url:
            user_id = url.split("/members/")[1].split("/")[0].split("?")[0]
            return self._response(self.members_by_id.get(user_id, {}))

        if method == "GET" and url.endswith("/roles"):
            return self._response(self.roles)

        if method == "GET" and "/guilds/" in url and url.endswith("/channels"):
            return self._response(self.channels)

        if method == "POST" and "/guilds/" in url and url.endswith("/channels"):
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            self.channel_counter += 1
            channel = {
                "id": f"chan-new-{self.channel_counter}",
                "name": body.get("name", ""),
                "type": body.get("type", 0),
                "parent_id": body.get("parent_id"),
                "permission_overwrites": [],
            }
            self.channels.append(channel)
            return self._response(channel)

        if method == "POST" and "/roles" in url:
            raise AssertionError(f"role creation attempted: {url}")

        if method in {"PUT", "DELETE"} and "/permissions/" in url:
            target_path = url.split("/channels/")[1]
            channel_id, _, overwrite_id = target_path.split("/")
            if method == "PUT":
                body = json.loads(request.data.decode("utf-8")) if request.data else {}
                self.overwrites[(channel_id, overwrite_id)] = body
                self._sync_overwrite_to_channel(channel_id, overwrite_id, body)
                return self._response({}, status=204, empty=True)
            if (channel_id, overwrite_id) in self.permission_delete_404:
                raise urllib.error.HTTPError(
                    url,
                    404,
                    "Not Found",
                    hdrs=None,
                    fp=BytesIO(b"{}"),
                )
            self.overwrites.pop((channel_id, overwrite_id), None)
            self._remove_overwrite_from_channel(channel_id, overwrite_id)
            return self._response({}, status=204, empty=True)

        if method == "DELETE" and re.fullmatch(rf"{re.escape(_API_BASE)}/channels/[^/]+", url):
            raise AssertionError(f"channel deletion attempted: {url}")

        raise AssertionError(f"unexpected request: {method} {url}")

    @staticmethod
    def _response(payload, status: int = 200, empty: bool = False):
        mock_resp = MagicMock()
        mock_resp.status = status
        raw = b"" if empty else json.dumps(payload).encode("utf-8")
        mock_resp.read.return_value = raw
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp


@pytest.fixture
def discord_router(monkeypatch, discord_guests_module):
    router = MockDiscordRouter()
    monkeypatch.setattr("urllib.request.urlopen", router)
    return router


def _call(discord_guests_module, args: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(discord_guests_module.handle_discord_guests(args))


def _endpoints(router: MockDiscordRouter) -> List[Any]:
    assert all(call["url"].startswith(_API_BASE) for call in router.calls)
    return [(call["method"], call["url"][len(_API_BASE):]) for call in router.calls]


def _write_state(home: Path, state: Dict[str, Any]) -> Path:
    state_path = home / "discord_guests" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def _read_state(home: Path) -> Dict[str, Any]:
    return json.loads((home / "discord_guests" / "state.json").read_text(encoding="utf-8"))


def _write_host_slug_setting(home: Path, host_slug: str) -> None:
    """Set plugins.entries.discord_guests.settings.host_slug in config.yaml."""
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  entries:\n"
        "    discord_guests:\n"
        "      settings:\n"
        f"        host_slug: {host_slug}\n",
        encoding="utf-8",
    )


def _fetched_self_member(router: MockDiscordRouter) -> bool:
    return any("/members/@me" in url for _, url in _endpoints(router))


class TestCheckRequirements:
    def test_missing_env(self, discord_guests_module, _isolate_env):
        assert discord_guests_module.check_requirements() is False

    def test_empty_token(self, discord_guests_module, _isolate_env):
        (_isolate_env / ".env").write_text("DISCORD_BOT_TOKEN=\n", encoding="utf-8")
        assert discord_guests_module.check_requirements() is False

    def test_valid_token(self, discord_guests_module, token_env):
        assert discord_guests_module.check_requirements() is True

    def test_missing_token_blocks_handler(
        self, discord_guests_module, _isolate_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "add", "user_id": "111"})
        assert result == {"success": False, "error": "Discord bot token not configured"}
        assert discord_router.calls == []


class TestSlugAndNaming:
    def test_slugify_lowercases_and_hyphenates(self, discord_guests_module):
        slug = discord_guests_module._slugify("  Ada *Lovelace* -- Countess!  ")
        assert slug == "ada-lovelace-countess"

    def test_slugify_collapses_and_trims_to_80(self, discord_guests_module):
        assert discord_guests_module._slugify("a---b") == "a-b"
        assert discord_guests_module._slugify("-~-") == ""
        assert len(discord_guests_module._slugify("x" * 200)) == 80
        assert discord_guests_module._slugify("x" * 200) == "x" * 80

    def test_display_name_precedence(self, discord_guests_module):
        def member(nick, global_name, username):
            return {
                "nick": nick,
                "user": {"id": "1", "username": username, "global_name": global_name},
            }

        assert discord_guests_module._member_display_name(member("Nick", "Global", "user")) == "Nick"
        assert discord_guests_module._member_display_name(member(None, "Global", "user")) == "Global"
        assert discord_guests_module._member_display_name(member(None, None, "user")) == "user"

    def test_lounge_name_host_and_slug_collapse(self, discord_guests_module):
        assert discord_guests_module._lounge_channel_name("ada", "") == "ada-agent-lounge"
        assert discord_guests_module._lounge_channel_name("ada", "Quix") == "ada-quix-lounge"
        # Guest and host slugs matching collapses — no stutter.
        assert discord_guests_module._lounge_channel_name("ada", "ada") == "ada-lounge"

    def test_default_host_slug_is_agent(self, discord_guests_module):
        assert discord_guests_module._DEFAULT_HOST_SLUG == "agent"


class TestSetup:
    def test_first_setup_locks_down_and_persists(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        result = _call(discord_guests_module, {"action": "setup"})

        assert result["success"] is True
        assert result["guild_id"] == "guild-1"
        assert result["chat_category_id"] == "cat-chat"
        assert result["lockdown"] is True
        # Categories and parentless channels only — children inherit.
        assert result["everyone_denied_view_on"] == ["cat-chat", "cat-other", "chan-top"]
        assert "chan-inbox" not in result["everyone_denied_view_on"]
        assert "chan-general" not in result["everyone_denied_view_on"]

        for target in ["cat-chat", "cat-other", "chan-top"]:
            assert discord_router.overwrites[(target, "guild-1")] == {
                "id": "guild-1",
                "type": 0,
                "allow": 0,
                "deny": _VIEW_CHANNEL,
            }
        assert ("chan-inbox", "guild-1") not in discord_router.overwrites

        saved = _read_state(_isolate_env)
        assert saved["guild_id"] == "guild-1"
        assert saved["chat_category_id"] == "cat-chat"
        assert saved["guests"] == []
        assert stat.S_IMODE(
            (_isolate_env / "discord_guests" / "state.json").stat().st_mode
        ) == 0o600

    def test_second_setup_is_lockdown_noop_unless_explicit(
        self, discord_guests_module, token_env, discord_router
    ):
        assert _call(discord_guests_module, {"action": "setup"})["success"] is True
        overwrite_count = len(discord_router.overwrites)
        calls_before = len(discord_router.calls)

        again = _call(discord_guests_module, {"action": "setup"})

        assert again["success"] is True
        assert again["lockdown"] is False
        assert "everyone_denied_view_on" not in again
        assert len(discord_router.overwrites) == overwrite_count
        assert all(call["method"] == "GET" for call in discord_router.calls[calls_before:])

        explicit = _call(discord_guests_module, {"action": "setup", "lockdown": True})
        assert explicit["lockdown"] is True
        assert explicit["everyone_denied_view_on"] == ["cat-chat", "cat-other", "chan-top"]

    def test_setup_without_chat_category_errors(
        self, discord_guests_module, token_env, discord_router
    ):
        discord_router.channels = [
            ch for ch in discord_router.channels if ch["name"].lower() != "chat"
        ]
        result = _call(discord_guests_module, {"action": "setup"})
        assert result["success"] is False
        assert "no Chat category" in result["error"]
        assert discord_router.overwrites == {}

    def test_setup_with_explicit_category_id_skips_name_lookup(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        discord_router.channels = [
            ch for ch in discord_router.channels if ch["name"].lower() != "chat"
        ]
        result = _call(
            discord_guests_module,
            {"action": "setup", "chat_category_id": "cat-42", "lockdown": False},
        )
        assert result["success"] is True
        assert result["chat_category_id"] == "cat-42"
        assert _read_state(_isolate_env)["chat_category_id"] == "cat-42"


class TestOverwriteMerge:
    def test_setup_lockdown_merges_existing_everyone_overwrite(
        self, discord_guests_module, token_env, discord_router
    ):
        extra_allow = _EXTRA_EVERYONE_ALLOW
        discord_router.channels[0]["permission_overwrites"] = [
            {
                "id": "guild-1",
                "type": 0,
                "allow": extra_allow,
                "deny": 0,
            }
        ]

        result = _call(discord_guests_module, {"action": "setup"})

        assert result["success"] is True
        merged = discord_router.overwrites[("cat-chat", "guild-1")]
        assert merged["deny"] & _VIEW_CHANNEL
        assert merged["allow"] & extra_allow == extra_allow
        assert merged["allow"] & _VIEW_CHANNEL == 0

    def test_add_reuses_lounge_and_merges_everyone_overwrite(
        self, discord_guests_module, token_env, discord_router
    ):
        extra_allow = _EXTRA_EVERYONE_ALLOW
        discord_router.channels.append(
            {
                "id": "chan-preseed",
                "name": "bob-big-steve-lounge",
                "type": 0,
                "parent_id": "cat-chat",
                "permission_overwrites": [
                    {
                        "id": "guild-1",
                        "type": 0,
                        "allow": extra_allow,
                        "deny": 0,
                    }
                ],
            }
        )

        result = _call(discord_guests_module, {"action": "add", "user_id": "333"})

        assert result["success"] is True
        assert result["channel_id"] == "chan-preseed"
        assert result["created"] is False
        merged = discord_router.overwrites[("chan-preseed", "guild-1")]
        assert merged["deny"] & _VIEW_CHANNEL
        assert merged["allow"] & extra_allow == extra_allow


class TestLoungeIdentity:
    def test_same_display_name_gets_distinct_lounges(
        self, discord_guests_module, token_env, discord_router
    ):
        discord_router.members_by_id["555"] = {
            "nick": None,
            "roles": [],
            "user": {"id": "555", "username": "bob2", "global_name": "Bob"},
        }

        first = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        second = _call(discord_guests_module, {"action": "add", "user_id": "555"})

        assert first["success"] is True and second["success"] is True
        assert first["channel_id"] != second["channel_id"]
        assert second["channel_name"] == "bob-big-steve-lounge-555"
        assert ("chan-new-1", "333") in discord_router.overwrites
        assert ("chan-new-2", "555") in discord_router.overwrites
        assert ("chan-new-1", "555") not in discord_router.overwrites

    def test_readd_after_nick_change_reuses_tracked_channel(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        first = _call(discord_guests_module, {"action": "add", "user_id": "111"})
        assert first["success"] is True
        channel_id = first["channel_id"]

        discord_router.members_by_id["111"]["user"]["global_name"] = "Ada L"
        discord_router.channels = [
            ch if ch["id"] != channel_id else {**ch, "name": "old-name-lounge"}
            for ch in discord_router.channels
        ]

        second = _call(discord_guests_module, {"action": "add", "user_id": "111"})

        assert second["success"] is True
        assert second["channel_id"] == channel_id
        assert second["created"] is False
        create_calls = [
            call for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        assert len(create_calls) == 1

    def test_readd_to_different_channel_revokes_old_member_overwrite(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        discord_router.channels.extend(
            [
                {
                    "id": "chan-old",
                    "name": "ada-lovelace-big-steve-lounge",
                    "type": 0,
                    "parent_id": "cat-other",
                },
                {
                    "id": "chan-taken",
                    "name": "ada-l-big-steve-lounge",
                    "type": 0,
                    "parent_id": "cat-chat",
                },
            ]
        )
        discord_router.overwrites[("chan-old", "111")] = {
            "id": "111",
            "type": 1,
            "allow": _EXPECTED_GUEST_ALLOW,
            "deny": 0,
        }
        _write_state(
            _isolate_env,
            {
                "guild_id": "guild-1",
                "chat_category_id": "cat-chat",
                "guests": [
                    {
                        "user_id": "111",
                        "name": "Ada Lovelace",
                        "channel_id": "chan-old",
                    },
                    {
                        "user_id": "222",
                        "name": "Ada L",
                        "channel_id": "chan-taken",
                    },
                ],
            },
        )
        discord_router.members_by_id["111"]["user"]["global_name"] = "Ada L"

        result = _call(discord_guests_module, {"action": "add", "user_id": "111"})

        assert result["success"] is True
        assert result["channel_id"] != "chan-old"
        assert ("chan-old", "111") not in discord_router.overwrites
        assert (
            "DELETE",
            "/channels/chan-old/permissions/111",
        ) in _endpoints(discord_router)
        saved = _read_state(_isolate_env)
        guest_111 = next(g for g in saved["guests"] if g["user_id"] == "111")
        assert guest_111["channel_id"] == result["channel_id"]

    def test_readd_revoke_treats_missing_old_overwrite_as_success(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        _write_state(
            _isolate_env,
            {
                "guild_id": "guild-1",
                "chat_category_id": "cat-chat",
                "guests": [
                    {
                        "user_id": "111",
                        "name": "Ada Lovelace",
                        "channel_id": "chan-gone",
                    },
                    {
                        "user_id": "222",
                        "name": "Ada L",
                        "channel_id": "chan-taken",
                    },
                ],
            },
        )
        discord_router.channels.append(
            {
                "id": "chan-taken",
                "name": "ada-l-big-steve-lounge",
                "type": 0,
                "parent_id": "cat-chat",
            }
        )
        discord_router.channels.append(
            {
                "id": "chan-gone",
                "name": "stale-lounge",
                "type": 0,
                "parent_id": "cat-other",
            }
        )
        discord_router.permission_delete_404.add(("chan-gone", "111"))
        discord_router.members_by_id["111"]["user"]["global_name"] = "Ada L"

        result = _call(discord_guests_module, {"action": "add", "user_id": "111"})

        assert result["success"] is True
        saved = _read_state(_isolate_env)
        guest_111 = next(g for g in saved["guests"] if g["user_id"] == "111")
        assert guest_111["channel_id"] == result["channel_id"]
        assert result["channel_id"] != "chan-gone"


class TestTokenResolution:
    def test_token_from_get_secret_without_env_file(
        self, discord_guests_module, _isolate_env, discord_router, monkeypatch
    ):
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        token = set_secret_scope({"DISCORD_BOT_TOKEN": "scoped-token"})
        try:
            result = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        finally:
            reset_secret_scope(token)

        assert result["success"] is True
        assert discord_router.calls[0]["headers"]["Authorization"] == "Bot scoped-token"

    def test_token_from_process_env_without_env_file(
        self, discord_guests_module, _isolate_env, discord_router, monkeypatch
    ):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        result = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        assert result["success"] is True
        assert discord_router.calls[0]["headers"]["Authorization"] == "Bot env-token"

    def test_env_file_fallback_still_works(self, discord_guests_module, token_env, discord_router):
        result = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        assert result["success"] is True
        assert discord_router.calls[0]["headers"]["Authorization"] == "Bot test-bot-token"

    def test_unscoped_secret_error_fails_closed(
        self, discord_guests_module, _isolate_env, discord_router, monkeypatch
    ):
        from agent.secret_scope import UnscopedSecretError

        def _raise_unscoped(name, default=None):
            raise UnscopedSecretError("no scope")

        monkeypatch.setattr("agent.secret_scope.get_secret", _raise_unscoped)
        (_isolate_env / ".env").write_text(
            "DISCORD_BOT_TOKEN=should-not-use\n", encoding="utf-8"
        )

        assert discord_guests_module.check_requirements() is False
        result = _call(discord_guests_module, {"action": "add", "user_id": "111"})
        assert result == {"success": False, "error": "Discord bot token not configured"}
        assert discord_router.calls == []


class TestGuildCategoryIsolation:
    def test_add_with_different_guild_does_not_use_saved_category(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        _write_state(
            _isolate_env,
            {
                "guild_id": "guild-1",
                "chat_category_id": "cat-chat",
                "guests": [],
            },
        )
        discord_router.guilds = [
            {"id": "guild-1", "name": "Test Guild"},
            {"id": "guild-2", "name": "Other Guild"},
        ]
        discord_router.channels = [
            {"id": "cat-g2-chat", "name": "Chat", "type": 4, "parent_id": None},
        ]

        result = _call(
            discord_guests_module,
            {"action": "add", "user_id": "333", "guild_id": "guild-2"},
        )

        assert result["success"] is True
        assert result["guild_id"] == "guild-2"
        assert result["chat_category_id"] == "cat-g2-chat"
        create_body = json.loads(
            next(
                call["body"]
                for call in discord_router.calls
                if call["method"] == "POST" and call["url"].endswith("/channels")
            )
        )
        assert create_body["parent_id"] == "cat-g2-chat"
        assert create_body["parent_id"] != "cat-chat"

    def test_setup_with_different_guild_resolves_local_chat_category(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        _write_state(
            _isolate_env,
            {
                "guild_id": "guild-1",
                "chat_category_id": "cat-chat",
                "guests": [],
            },
        )
        discord_router.guilds = [
            {"id": "guild-1", "name": "Test Guild"},
            {"id": "guild-2", "name": "Other Guild"},
        ]
        discord_router.channels = [
            {"id": "cat-g2-chat", "name": "Chat", "type": 4, "parent_id": None},
        ]

        result = _call(
            discord_guests_module,
            {"action": "setup", "guild_id": "guild-2", "lockdown": False},
        )

        assert result["success"] is True
        assert result["guild_id"] == "guild-2"
        assert result["chat_category_id"] == "cat-g2-chat"
        assert _read_state(_isolate_env)["chat_category_id"] == "cat-g2-chat"


class TestAdd:
    def test_add_creates_lounge_with_member_allow_and_everyone_deny(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        discord_router.search_results = [discord_router.members_by_id["111"]]

        result = _call(discord_guests_module, {"action": "add", "member": "Ada"})

        assert result["success"] is True
        assert result["action"] == "add"
        assert result["user_id"] == "111"
        assert result["name"] == "Ada Lovelace"
        assert result["channel_id"] == "chan-new-1"
        assert result["channel_name"] == "ada-lovelace-big-steve-lounge"
        assert result["created"] is True
        assert result["chat_category_id"] == "cat-chat"
        assert result["matched_by"] == "name prefix"

        assert _endpoints(discord_router) == [
            ("GET", "/users/@me/guilds"),
            ("GET", "/guilds/guild-1/members/search?query=Ada"),
            ("GET", "/guilds/guild-1/roles"),
            ("GET", "/guilds/guild-1/channels"),
            ("GET", "/guilds/guild-1/members/@me"),
            ("POST", "/guilds/guild-1/channels"),
            ("PUT", "/channels/chan-new-1/permissions/111"),
            ("PUT", "/channels/chan-new-1/permissions/guild-1"),
        ]
        assert discord_router.calls[0]["headers"]["Authorization"] == "Bot test-bot-token"
        assert "test-bot-token" not in json.dumps(result)

        create_body = json.loads(discord_router.calls[5]["body"])
        assert create_body == {
            "name": "ada-lovelace-big-steve-lounge",
            "type": 0,
            "parent_id": "cat-chat",
        }

        member_overwrite = discord_router.overwrites[("chan-new-1", "111")]
        assert member_overwrite == {
            "id": "111",
            "type": 1,
            "allow": _EXPECTED_GUEST_ALLOW,
            "deny": 0,
        }
        assert member_overwrite["allow"] & _FORBIDDEN_BITS == 0

        assert discord_router.overwrites[("chan-new-1", "guild-1")] == {
            "id": "guild-1",
            "type": 0,
            "allow": 0,
            "deny": _VIEW_CHANNEL,
        }

        saved = _read_state(_isolate_env)
        assert saved["guild_id"] == "guild-1"
        assert saved["chat_category_id"] == "cat-chat"
        assert saved["guests"] == [
            {
                "user_id": "111",
                "name": "Ada Lovelace",
                "channel_id": "chan-new-1",
            }
        ]

    def test_add_by_user_id_slugs_plain_username(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        assert result["success"] is True
        assert result["channel_name"] == "bob-big-steve-lounge"
        assert "matched_by" not in result

    def test_host_slug_derived_from_bot_nickname(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "add", "user_id": "444"})
        assert result["success"] is True
        assert result["channel_name"] == "winnie-big-steve-lounge"
        assert _fetched_self_member(discord_router) is True

    def test_host_slug_falls_back_to_global_name_then_username(
        self, discord_guests_module, token_env, discord_router
    ):
        discord_router.self_member = {
            "nick": None,
            "roles": [],
            "user": {"id": "9001", "username": "hermes-agent", "global_name": "Hermes Bot"},
        }
        result = _call(discord_guests_module, {"action": "add", "user_id": "444"})
        assert result["success"] is True
        assert result["channel_name"] == "winnie-hermes-bot-lounge"

        discord_router.self_member["user"]["global_name"] = None
        first_channel_id = result["channel_id"]
        # Re-add reuses the tracked lounge even when the derived host slug changes.
        result = _call(discord_guests_module, {"action": "add", "user_id": "444", "host": ""})
        assert result["success"] is True
        assert result["created"] is False
        assert result["channel_id"] == first_channel_id

    def test_host_slug_setting_overrides_derivation(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        _write_host_slug_setting(_isolate_env, "quix")
        result = _call(discord_guests_module, {"action": "add", "user_id": "444"})
        assert result["success"] is True
        assert result["channel_name"] == "winnie-quix-lounge"
        # Settings short-circuit before any identity lookup.
        assert _fetched_self_member(discord_router) is False

    def test_host_arg_overrides_setting_and_derivation(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        _write_host_slug_setting(_isolate_env, "quix")
        result = _call(
            discord_guests_module, {"action": "add", "user_id": "333", "host": "Boss"}
        )
        assert result["success"] is True
        assert result["channel_name"] == "bob-boss-lounge"
        assert _fetched_self_member(discord_router) is False

    def test_matching_guest_and_host_slugs_collapse(
        self, discord_guests_module, token_env, discord_router
    ):
        # Bot nickname "Winnie" derives the same slug as the guest's name.
        discord_router.self_member["nick"] = "Winnie"
        result = _call(discord_guests_module, {"action": "add", "user_id": "444"})
        assert result["success"] is True
        assert result["channel_name"] == "winnie-lounge"

    def test_add_is_idempotent(
        self, discord_guests_module, token_env, discord_router
    ):
        first = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        second = _call(discord_guests_module, {"action": "add", "user_id": "333"})

        assert first["success"] is True and second["success"] is True
        assert first["created"] is True
        assert second["created"] is False
        assert second["channel_id"] == first["channel_id"] == "chan-new-1"

        create_calls = [
            call for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        assert len(create_calls) == 1
        # Second add reuses state's guild and re-asserts both overwrites.
        assert ("chan-new-1", "333") in discord_router.overwrites
        assert ("chan-new-1", "guild-1") in discord_router.overwrites

    def test_admin_member_refused_before_any_write(
        self, discord_guests_module, token_env, discord_router
    ):
        discord_router.search_results = [discord_router.members_by_id["222"]]

        result = _call(discord_guests_module, {"action": "add", "member": "Boss"})

        assert result["success"] is False
        assert "ADMINISTRATOR" in result["error"]
        # Refused during resolution: reads only, nothing provisioned.
        assert discord_router.overwrites == {}
        assert all(call["method"] == "GET" for call in discord_router.calls)
        assert not any(
            call["method"] == "POST" for call in discord_router.calls
        )

    def test_admin_via_everyone_role_refused(
        self, discord_guests_module, token_env, discord_router
    ):
        discord_router.roles = [
            {"id": "guild-1", "name": "@everyone", "permissions": "8"},
        ]
        result = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        assert result["success"] is False
        assert "ADMINISTRATOR" in result["error"]

    def test_add_requires_member_identification(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "add"})
        assert result["success"] is False
        assert "member required" in result["error"]
        assert discord_router.calls == []

    def test_add_unknown_member_errors(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "add", "user_id": "999"})
        assert result["success"] is False
        assert "999" in result["error"]

    def test_add_name_prefix_without_match_errors(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "add", "member": "nobody"})
        assert result["success"] is False
        assert "no guild member matches" in result["error"]

    def test_add_without_chat_category_errors(
        self, discord_guests_module, token_env, discord_router
    ):
        discord_router.channels = [
            ch for ch in discord_router.channels if ch["name"].lower() != "chat"
        ]
        result = _call(discord_guests_module, {"action": "add", "user_id": "333"})
        assert result["success"] is False
        assert "no Chat category" in result["error"]
        assert discord_router.overwrites == {}


class TestRemove:
    def test_remove_drops_member_overwrite_not_the_channel(
        self, discord_guests_module, token_env, discord_router, _isolate_env
    ):
        added = _call(discord_guests_module, {"action": "add", "user_id": "111"})
        assert added["success"] is True
        channel_id = added["channel_id"]

        result = _call(discord_guests_module, {"action": "remove", "user_id": "111"})

        assert result["success"] is True
        assert result["action"] == "remove"
        assert result["user_id"] == "111"
        assert result["channel_id"] == channel_id
        assert result["channel_kept"] is True

        assert (
            "DELETE",
            f"/channels/{channel_id}/permissions/111",
        ) in _endpoints(discord_router)
        assert (channel_id, "111") not in discord_router.overwrites
        # The lounge itself — and its history — is never deleted.
        assert any(ch["id"] == channel_id for ch in discord_router.channels)
        assert discord_router.deleted_channels == []
        assert not any(
            call["method"] == "DELETE" and call["url"].rstrip("/").endswith(channel_id)
            and "/permissions/" not in call["url"]
            for call in discord_router.calls
        )

        saved = _read_state(_isolate_env)
        assert saved["guests"] == []

    def test_remove_by_name_prefix_of_tracked_guest(
        self, discord_guests_module, token_env, discord_router
    ):
        assert _call(discord_guests_module, {"action": "add", "user_id": "111"})["success"]
        result = _call(discord_guests_module, {"action": "remove", "member": "ada love"})
        assert result["success"] is True
        assert result["user_id"] == "111"
        assert ("chan-new-1", "111") not in discord_router.overwrites

    def test_remove_untracked_member_still_finds_lounge_by_name(
        self, discord_guests_module, token_env, discord_router
    ):
        # A lounge exists (say, from a previous install) but no state entry.
        discord_router.channels.append(
            {"id": "chan-orphan", "name": "bob-big-steve-lounge", "type": 0, "parent_id": "cat-chat"}
        )

        result = _call(discord_guests_module, {"action": "remove", "user_id": "333"})

        assert result["success"] is True
        assert result["channel_id"] == "chan-orphan"
        assert ("chan-orphan", "333") not in discord_router.overwrites
        assert any(ch["id"] == "chan-orphan" for ch in discord_router.channels)

    def test_remove_without_lounge_errors(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "remove", "user_id": "333"})
        assert result["success"] is False
        assert "no lounge" in result["error"]


class TestList:
    def test_list_reports_guests_with_live_channel_existence(
        self, discord_guests_module, token_env, discord_router
    ):
        assert _call(discord_guests_module, {"action": "add", "user_id": "111"})["success"]

        listed = _call(discord_guests_module, {"action": "list"})
        assert listed["success"] is True
        assert listed["guild_id"] == "guild-1"
        assert listed["chat_category_id"] == "cat-chat"
        assert listed["guests"] == [
            {
                "user_id": "111",
                "name": "Ada Lovelace",
                "channel_id": "chan-new-1",
                "channel_exists": True,
            }
        ]

        # The channel vanishes out from under state — list must reflect that.
        discord_router.channels = [
            ch for ch in discord_router.channels if ch["id"] != "chan-new-1"
        ]
        listed = _call(discord_guests_module, {"action": "list"})
        assert listed["guests"][0]["channel_exists"] is False

    def test_list_empty_state_skips_channel_fetch(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(discord_guests_module, {"action": "list"})
        assert result == {
            "success": True,
            "action": "list",
            "guild_id": "guild-1",
            "chat_category_id": "",
            "guests": [],
        }
        assert _endpoints(discord_router) == [("GET", "/users/@me/guilds")]

    def test_default_action_is_list(self, discord_guests_module, token_env, discord_router):
        result = _call(discord_guests_module, {})
        assert result["action"] == "list"


class TestGuildResolution:
    def test_ambiguous_guild_errors(self, discord_guests_module, token_env, discord_router):
        discord_router.guilds = [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]
        result = _call(discord_guests_module, {"action": "add", "user_id": "111"})
        assert result["success"] is False
        assert "multiple guilds" in result["error"]
        assert result["guilds"] == [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]

    def test_explicit_guild_id_wins(
        self, discord_guests_module, token_env, discord_router
    ):
        result = _call(
            discord_guests_module, {"action": "add", "user_id": "111", "guild_id": "g7"}
        )
        assert result["success"] is True
        assert result["guild_id"] == "g7"


class TestContract:
    def test_no_role_endpoints_are_ever_written(
        self, discord_guests_module, token_env, discord_router
    ):
        """Across every action, no write ever targets a role endpoint."""
        discord_router.search_results = [discord_router.members_by_id["111"]]
        for args in [
            {"action": "setup"},
            {"action": "add", "member": "Ada"},
            {"action": "list"},
            {"action": "remove", "user_id": "111"},
            {"action": "setup", "lockdown": True},
        ]:
            assert _call(discord_guests_module, args)["success"] is True

        write_urls = [
            call["url"] for call in discord_router.calls if call["method"] != "GET"
        ]
        assert write_urls  # the run did write — channels and overwrites only
        for url in write_urls:
            assert "/roles" not in url, url

    def test_never_creates_anything_but_channels(
        self, discord_guests_module, token_env, discord_router
    ):
        """The only POSTs are guild-channel creations."""
        discord_router.search_results = [discord_router.members_by_id["111"]]
        _call(discord_guests_module, {"action": "setup"})
        _call(discord_guests_module, {"action": "add", "member": "Ada"})

        posts = [call["url"] for call in discord_router.calls if call["method"] == "POST"]
        assert posts
        assert all(re.fullmatch(rf"{re.escape(_API_BASE)}/guilds/[^/]+/channels", url)
                   for url in posts)

    def test_http_error_returns_failure_without_raising(
        self, discord_guests_module, token_env, monkeypatch, _isolate_env
    ):
        def _raise_http(request):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", hdrs=None, fp=BytesIO(b"{}")
            )

        monkeypatch.setattr("urllib.request.urlopen", _raise_http)
        result = _call(discord_guests_module, {"action": "add", "user_id": "111"})
        assert result == {"success": False, "error": "HTTP error: 500"}


class TestRateLimit:
    def test_429_retry_after_is_honoured_and_write_retried(
        self, discord_guests_module, token_env, discord_router, monkeypatch
    ):
        sleeps: List[float] = []
        monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
        discord_router.rate_limit_pending = 1

        result = _call(discord_guests_module, {"action": "add", "user_id": "333"})

        assert result["success"] is True
        assert result["channel_id"] == "chan-new-1"
        # retry_after=0 from the body was slept before the retry, and the
        # write went through on the second attempt.
        assert 0.0 in sleeps
        create_attempts = [
            call for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        # One rejected attempt plus one successful retry — exactly one lounge
        # actually created.
        assert len(create_attempts) == 2
        assert discord_router.channel_counter == 1
        assert ("chan-new-1", "333") in discord_router.overwrites


class TestPacing:
    def test_writes_are_paced_at_least_the_minimum_gap(self, discord_guests_module, monkeypatch):
        sleeps: List[float] = []
        monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
        discord_guests_module._WRITE_PACE_SECONDS = 0.3

        # Immediately after a write, the next write must wait out the gap…
        discord_guests_module._last_write_at = time.monotonic()
        discord_guests_module._pace_write()
        assert sleeps and 0 < sleeps[-1] <= 0.3

        # …but a long-stale last write pays nothing.
        sleeps.clear()
        discord_guests_module._last_write_at = time.monotonic() - 10
        discord_guests_module._pace_write()
        assert sleeps == []


class TestNoGuestRoleMentions:
    """The user rejected a role-based design; nothing may surface it."""

    def test_no_role_wording_in_plugin_surfaces(self, discord_guests_module):
        repo_root = Path(__file__).resolve().parents[3]
        plugin_dir = repo_root / "plugins" / "discord_guests"

        readme = (plugin_dir / "README.md").read_text(encoding="utf-8")
        manifest = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
        runtime_strings = " ".join(
            [
                discord_guests_module.DISCORD_GUESTS_SCHEMA["description"],
                json.dumps(discord_guests_module.DISCORD_GUESTS_SCHEMA["parameters"]),
            ]
        )

        pattern = re.compile(r"guest\s+role", re.IGNORECASE)
        for label, text in [
            ("README", readme),
            ("plugin.yaml", manifest),
            ("runtime", runtime_strings),
        ]:
            matches = pattern.findall(text)
            assert not matches, f"{label} surfaces rejected wording: {matches}"


class TestPluginDiscovery:
    def test_register_via_mock_ctx(self, discord_guests_module):
        from tools.registry import registry

        captured = {}

        class _Ctx:
            def register_tool(self, name, toolset, schema, handler, **kwargs):
                captured["name"] = name
                captured["toolset"] = toolset
                captured["schema"] = schema
                captured["handler"] = handler
                captured["kwargs"] = kwargs
                registry.register(
                    name=name,
                    toolset=toolset,
                    schema=schema,
                    handler=handler,
                    check_fn=kwargs.get("check_fn"),
                    emoji=kwargs.get("emoji"),
                )

        discord_guests_module.register(_Ctx())
        assert captured["name"] == "discord_guests"
        assert captured["toolset"] == "discord_guests"
        assert captured["kwargs"]["emoji"] == "🪪"

        entry = registry.get_entry("discord_guests")
        assert entry is not None
        assert entry.toolset == "discord_guests"

    def test_discover_via_plugin_manager(self, _isolate_env):
        for key in list(sys.modules):
            if key.startswith(("plugins.discord_guests", "hermes_cli.plugins")):
                del sys.modules[key]

        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        mgr = PluginManager()
        mgr.discover_and_load(force=True)

        assert "discord_guests" in mgr._plugins
        loaded = mgr._plugins["discord_guests"]
        assert loaded.enabled is True
        assert loaded.error is None

        entry = registry.get_entry("discord_guests")
        assert entry is not None
        assert entry.toolset == "discord_guests"
