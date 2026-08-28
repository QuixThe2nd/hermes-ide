"""Tests for the bundled discord_guests plugin."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

_API_BASE = "https://discord.com/api/v10"
_TOKEN = "test-bot-token"

_PERM_VIEW_CHANNEL = 1 << 10


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
    spec = importlib.util.spec_from_file_location(
        module_name, plugin_dir / "__init__.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    # Writes are paced by default; the transport tests restore the real value.
    mod._MIN_WRITE_INTERVAL = 0.0
    return mod


@pytest.fixture
def token_env(_isolate_env):
    env_path = _isolate_env / ".env"
    env_path.write_text(f"DISCORD_BOT_TOKEN={_TOKEN}\n", encoding="utf-8")
    return env_path


def _overwrite(target_id: str, allow: int = 0, deny: int = 0) -> Dict[str, Any]:
    return {"id": str(target_id), "type": 0, "allow": str(allow), "deny": str(deny)}


def _channel(
    cid: str, name: str, ctype: int = 0, parent: Optional[str] = None, overwrites=None
) -> Dict[str, Any]:
    return {
        "id": str(cid),
        "name": name,
        "type": ctype,
        "parent_id": parent,
        "permission_overwrites": list(overwrites or []),
    }


def _member(uid: str, username: str, roles=(), global_name: str = "") -> Dict[str, Any]:
    return {
        "user": {
            "id": str(uid),
            "username": username,
            "global_name": global_name or username,
        },
        "roles": [str(r) for r in roles],
    }


class FakeClock:
    """Stands in for the stdlib time module so pacing never really sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeDiscord:
    """A stateful stand-in for the Discord REST surface the plugin touches.

    Overwrites and role memberships really are applied, so a test can assert on
    both the requests it saw and the server state it left behind.
    """

    GUILDS = re.compile(r"^/users/@me/guilds$")
    ROLES = re.compile(r"^/guilds/([^/]+)/roles$")
    CHANNELS = re.compile(r"^/guilds/([^/]+)/channels$")
    OVERWRITE = re.compile(r"^/channels/([^/]+)/permissions/([^/]+)$")
    MEMBER = re.compile(r"^/guilds/([^/]+)/members/([^/]+)$")
    MEMBER_ROLE = re.compile(r"^/guilds/([^/]+)/members/([^/]+)/roles/([^/]+)$")
    MEMBER_SEARCH = re.compile(r"^/guilds/([^/]+)/members/search$")
    MEMBERS = re.compile(r"^/guilds/([^/]+)/members$")

    def __init__(self) -> None:
        self.guild_id = "100"
        self.guilds: List[Dict[str, str]] = [{"id": "100", "name": "Test Guild"}]
        self.roles: List[Dict[str, Any]] = [
            {"id": "100", "name": "@everyone", "permissions": "0"},
            {"id": "900", "name": "Admins", "permissions": str(1 << 3)},
        ]
        self.channels: List[Dict[str, Any]] = [
            _channel("200", "General", ctype=4),
            _channel("201", "lounge", parent="200"),
            _channel(
                "202", "announcements", parent="200", overwrites=[_overwrite("100")]
            ),
            _channel("203", "staff-room"),
            _channel("204", "Voice Lounge", ctype=2),
        ]
        self.members: List[Dict[str, Any]] = [
            _member("300", "quix"),
            _member("301", "friendbot", global_name="Friend Bot"),
            _member("302", "rootadmin", roles=["900"]),
        ]
        self.next_id = 500
        self.calls: List[Dict[str, Any]] = []
        self.deny_member_list = False
        self.deny_member_search = False
        self.rate_limit_once: Dict[str, float] = {}

    def __call__(self, request):
        self.calls.append({
            "method": request.method,
            "url": request.full_url,
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": request.data.decode("utf-8") if request.data else None,
        })
        retry_after = self.rate_limit_once.pop(
            f"{request.method} {request.full_url}", None
        )
        if retry_after is not None:
            raise self._http_error(request.full_url, 429, {"retry_after": retry_after})

        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        payload = self._dispatch(request.method, request.full_url, body)
        return self._response(payload)

    def _dispatch(self, method: str, url: str, body: Dict[str, Any]):
        path = url[len(_API_BASE) :]
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)

        if method == "GET" and self.GUILDS.match(path):
            return self.guilds
        if method == "GET" and self.ROLES.match(path):
            return self.roles
        if method == "POST" and self.ROLES.match(path):
            return self._create_role(body)
        if method == "GET" and self.CHANNELS.match(path):
            return self.channels
        if method == "PUT" and (m := self.OVERWRITE.match(path)):
            return self._put_overwrite(m.group(1), m.group(2), body)
        if method == "DELETE" and (m := self.OVERWRITE.match(path)):
            return self._delete_overwrite(m.group(1), m.group(2))
        # search first: /members/search would otherwise match /members/{user_id}
        if method == "GET" and self.MEMBER_SEARCH.match(path):
            return self._search_members(query)
        if method == "GET" and (m := self.MEMBER.match(path)):
            return self._member(m.group(2))
        if method in ("PUT", "DELETE") and (m := self.MEMBER_ROLE.match(path)):
            return self._member_role(method, m.group(2), m.group(3))
        if method == "GET" and self.MEMBERS.match(path):
            if self.deny_member_list:
                raise self._http_error(url, 403, {"message": "Missing Access"})
            return self.members

        raise AssertionError(f"unexpected request: {method} {url}")

    def _create_role(self, body: Dict[str, Any]) -> Dict[str, Any]:
        self.next_id += 1
        role = {
            "id": str(self.next_id),
            "name": body.get("name"),
            "permissions": str(body.get("permissions", "0")),
            "hoist": body.get("hoist"),
            "mentionable": body.get("mentionable"),
        }
        self.roles.append(role)
        return role

    def _put_overwrite(
        self, channel_id: str, target_id: str, body: Dict[str, Any]
    ) -> Dict:
        channel = self._channel(channel_id)
        existing = next(
            (o for o in channel["permission_overwrites"] if o["id"] == target_id), None
        )
        if existing is None:
            existing = {"id": target_id, "type": 0, "allow": "0", "deny": "0"}
            channel["permission_overwrites"].append(existing)
        existing.update({
            "type": body.get("type", 0),
            "allow": body.get("allow", "0"),
            "deny": body.get("deny", "0"),
        })
        return {}

    def _delete_overwrite(self, channel_id: str, target_id: str) -> Dict:
        channel = self._channel(channel_id)
        channel["permission_overwrites"] = [
            o for o in channel["permission_overwrites"] if o["id"] != target_id
        ]
        return {}

    def _member(self, user_id: str) -> Dict[str, Any]:
        for member in self.members:
            if member["user"]["id"] == user_id:
                return member
        raise self._http_error(f"{_API_BASE}/members", 404, {"code": 10007})

    def _member_role(self, method: str, user_id: str, role_id: str) -> Dict:
        member = self._member(user_id)
        if method == "PUT":
            if role_id not in member["roles"]:
                member["roles"].append(role_id)
        else:
            member["roles"] = [r for r in member["roles"] if r != role_id]
        return {}

    def _search_members(self, query: str) -> List[Dict[str, Any]]:
        if self.deny_member_search:
            raise self._http_error(
                f"{_API_BASE}/search", 403, {"message": "Missing Access"}
            )
        from urllib.parse import parse_qs

        prefix = parse_qs(query).get("query", [""])[0].lower()
        return [
            m
            for m in self.members
            if m["user"]["username"].lower().startswith(prefix)
            or m["user"]["global_name"].lower().startswith(prefix)
        ]

    # -- helpers -------------------------------------------------------------

    def _channel(self, channel_id) -> Dict[str, Any]:
        return next(c for c in self.channels if c["id"] == str(channel_id))

    def overwrite(self, channel_id: str, target_id: str) -> Optional[Dict[str, Any]]:
        for overwrite in self._channel(channel_id)["permission_overwrites"]:
            if overwrite["id"] == str(target_id):
                return overwrite
        return None

    def guest_role(self) -> Optional[Dict[str, Any]]:
        return next((r for r in self.roles if r["name"] == "Guest"), None)

    @staticmethod
    def _http_error(url: str, code: int, payload: Dict[str, Any]):
        return urllib.error.HTTPError(
            url,
            code,
            "Error",
            hdrs=None,
            fp=BytesIO(json.dumps(payload).encode("utf-8")),
        )

    @staticmethod
    def _response(payload, status: int = 200):
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp


@pytest.fixture
def discord(monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def _call(mod, args: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(mod.handle_discord_guests(args))


def _writes(fake: FakeDiscord) -> List[Dict[str, Any]]:
    return [c for c in fake.calls if c["method"] != "GET"]


def _overwrite_puts(fake: FakeDiscord) -> List[Dict[str, Any]]:
    return [
        c for c in fake.calls if c["method"] == "PUT" and "/permissions/" in c["url"]
    ]


def _overwrite_deletes(fake: FakeDiscord) -> List[Dict[str, Any]]:
    return [
        c for c in fake.calls if c["method"] == "DELETE" and "/permissions/" in c["url"]
    ]


def _paths(calls: List[Dict[str, Any]]) -> List[str]:
    return [c["url"][len(_API_BASE) :].split("?")[0] for c in calls]


def _seed_guest_role(fake: FakeDiscord, role_id: str = "888") -> str:
    fake.roles.append({
        "id": role_id,
        "name": "Guest",
        "permissions": "0",
        "hoist": False,
        "mentionable": False,
    })
    return role_id


# ── token / check_fn ────────────────────────────────────────────────────────


class TestCheckRequirements:
    def test_missing_env(self, discord_guests_module, _isolate_env):
        assert discord_guests_module.check_requirements() is False

    def test_empty_token(self, discord_guests_module, _isolate_env):
        (_isolate_env / ".env").write_text("DISCORD_BOT_TOKEN=\n", encoding="utf-8")
        assert discord_guests_module.check_requirements() is False

    def test_valid_token(self, discord_guests_module, token_env):
        assert discord_guests_module.check_requirements() is True

    def test_quoted_token(self, discord_guests_module, _isolate_env):
        (_isolate_env / ".env").write_text(
            'DISCORD_BOT_TOKEN="abc"\n', encoding="utf-8"
        )
        assert discord_guests_module.check_requirements() is True

    def test_handler_errors_without_token(
        self, discord_guests_module, _isolate_env, discord
    ):
        result = _call(discord_guests_module, {"action": "setup"})
        assert result == {"success": False, "error": "Discord bot token not configured"}
        assert discord.calls == []


# ── setup ───────────────────────────────────────────────────────────────────


class TestSetup:
    def test_creates_guest_role_and_locks_everyone_down(
        self, discord_guests_module, token_env, discord, _isolate_env
    ):
        result = _call(discord_guests_module, {"action": "setup"})

        assert result["success"] is True
        assert result["role_created"] is True
        assert result["lockdown_applied"] is True

        role = discord.guest_role()
        assert role is not None
        assert role["name"] == "Guest"
        assert role["permissions"] == "0"
        assert role["hoist"] is True
        assert role["mentionable"] is False
        assert result["role_id"] == role["id"]

        # Categories and parentless channels only; categorised children inherit.
        assert sorted(result["locked_channel_ids"]) == ["200", "203", "204"]
        for cid in ("200", "203", "204"):
            everyone = discord.overwrite(cid, discord.guild_id)
            assert everyone is not None
            assert int(everyone["deny"]) & _PERM_VIEW_CHANNEL
            assert not int(everyone["allow"]) & _PERM_VIEW_CHANNEL
        assert discord.overwrite("201", discord.guild_id) is None
        assert int(discord.overwrite("202", discord.guild_id)["deny"]) == 0

        state = json.loads(
            (_isolate_env / "discord_guests" / "state.json").read_text(encoding="utf-8")
        )
        assert state == {"guild_id": "100", "role_id": role["id"]}

    def test_lockdown_preserves_other_overwrites_and_prior_bits(
        self, discord_guests_module, token_env, discord
    ):
        send_messages = 1 << 11
        discord.channels[0]["permission_overwrites"] = [
            _overwrite("100", allow=send_messages),
            _overwrite("777", allow=_PERM_VIEW_CHANNEL, deny=send_messages),
        ]
        discord.channels[3]["permission_overwrites"] = [_overwrite("100", deny=1 << 18)]

        result = _call(discord_guests_module, {"action": "setup"})
        assert result["success"] is True

        # Another target's overwrite is untouched, and @everyone keeps its bits.
        other = discord.overwrite("200", "777")
        assert int(other["allow"]) == _PERM_VIEW_CHANNEL
        assert int(other["deny"]) == send_messages

        everyone = discord.overwrite("200", "100")
        assert int(everyone["allow"]) & send_messages
        assert not int(everyone["allow"]) & _PERM_VIEW_CHANNEL
        assert int(everyone["deny"]) & _PERM_VIEW_CHANNEL

        staff = discord.overwrite("203", "100")
        assert int(staff["deny"]) & (1 << 18)
        assert int(staff["deny"]) & _PERM_VIEW_CHANNEL

    def test_second_setup_locks_nothing_and_creates_nothing(
        self, discord_guests_module, token_env, discord
    ):
        first = _call(discord_guests_module, {"action": "setup"})
        writes_after_first = len(_writes(discord))

        second = _call(discord_guests_module, {"action": "setup"})

        assert second["success"] is True
        assert second["role_created"] is False
        assert second["lockdown_applied"] is False
        assert second["locked_channel_ids"] == []
        assert second["role_id"] == first["role_id"]
        assert len(_writes(discord)) == writes_after_first  # reads only

    def test_explicit_lockdown_reruns_but_writes_nothing_new(
        self, discord_guests_module, token_env, discord
    ):
        assert _call(discord_guests_module, {"action": "setup"})["success"] is True
        before = len(_overwrite_puts(discord))

        result = _call(discord_guests_module, {"action": "setup", "lockdown": True})

        assert result["lockdown_applied"] is True
        # Everything is already denied, so the re-run is a pure read.
        assert len(_overwrite_puts(discord)) == before

    def test_lockdown_false_skips_it_entirely(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(
            discord_guests_module,
            {"action": "setup", "lockdown": False, "guild_id": "100"},
        )
        assert result["success"] is True
        assert result["lockdown_applied"] is False
        assert result["locked_channel_ids"] == []
        assert _overwrite_puts(discord) == []

    def test_existing_guest_role_is_reused(
        self, discord_guests_module, token_env, discord
    ):
        _seed_guest_role(discord)
        result = _call(discord_guests_module, {"action": "setup"})
        assert result["role_id"] == "888"
        assert result["role_created"] is False


# ── guild resolution ────────────────────────────────────────────────────────


class TestGuildResolution:
    def test_ambiguous_guild_is_an_error(
        self, discord_guests_module, token_env, discord
    ):
        discord.guilds = [{"id": "100", "name": "One"}, {"id": "101", "name": "Two"}]
        result = _call(discord_guests_module, {"action": "setup"})
        assert result["success"] is False
        assert "multiple guilds" in result["error"]
        assert {g["id"] for g in result["guilds"]} == {"100", "101"}
        assert _writes(discord) == []

    def test_bot_in_no_guild(self, discord_guests_module, token_env, discord):
        discord.guilds = []
        result = _call(discord_guests_module, {"action": "setup"})
        assert result["success"] is False
        assert "not in any guild" in result["error"]

    def test_saved_state_is_preferred_over_discovery(
        self, discord_guests_module, token_env, discord, _isolate_env
    ):
        state_path = _isolate_env / "discord_guests" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"guild_id": "424242", "role_id": "55"}), encoding="utf-8"
        )

        result = _call(
            discord_guests_module, {"action": "grant", "channels": ["staff-room"]}
        )

        assert result["success"] is True
        assert result["guild_id"] == "424242"
        assert "/users/@me/guilds" not in _paths(discord.calls)


# ── add ─────────────────────────────────────────────────────────────────────


class TestAdd:
    def test_assigns_role_by_id(self, discord_guests_module, token_env, discord):
        result = _call(discord_guests_module, {"action": "add", "member": "301"})

        assert result["success"] is True
        assert result["member_id"] == "301"
        assert result["role_id"] == discord.guest_role()["id"]
        assert result["granted_channel_ids"] == []
        assert discord.guest_role()["id"] in discord.members[1]["roles"]

    def test_resolves_name_prefix_case_insensitively(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(discord_guests_module, {"action": "add", "member": "FriendBot"})
        assert result["success"] is True
        assert result["member_id"] == "301"

    def test_grants_view_only_on_named_channels(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(
            discord_guests_module,
            {"action": "add", "member": "301", "channels": ["lounge", "204"]},
        )
        assert result["success"] is True
        assert result["granted_channel_ids"] == ["201", "204"]

        role_id = result["role_id"]
        for cid in ("201", "204"):
            overwrite = discord.overwrite(cid, role_id)
            assert int(overwrite["allow"]) == _PERM_VIEW_CHANNEL
            assert int(overwrite["deny"]) == 0

    def test_admin_member_is_refused(self, discord_guests_module, token_env, discord):
        result = _call(discord_guests_module, {"action": "add", "member": "302"})

        assert result["success"] is False
        assert result["refused"] is True
        assert "ADMINISTRATOR" in result["error"]
        # No role was assigned and nothing was granted.
        assert _writes(discord) == []
        assert discord.members[2]["roles"] == ["900"]

    def test_admin_via_everyone_role_is_refused_too(
        self, discord_guests_module, token_env, discord
    ):
        discord.roles[0]["permissions"] = str(1 << 3)
        result = _call(discord_guests_module, {"action": "add", "member": "300"})
        assert result["success"] is False
        assert result["refused"] is True

    def test_unknown_member_id(self, discord_guests_module, token_env, discord):
        result = _call(discord_guests_module, {"action": "add", "member": "999"})
        assert result["success"] is False
        assert "not in this guild" in result["error"]

    def test_ambiguous_prefix(self, discord_guests_module, token_env, discord):
        discord.members.append(_member("305", "friend"))
        discord.members.append(_member("306", "friend-two"))

        result = _call(discord_guests_module, {"action": "add", "member": "friend"})
        assert result["success"] is False
        assert "several members" in result["error"]

    def test_no_prefix_match(self, discord_guests_module, token_env, discord):
        result = _call(discord_guests_module, {"action": "add", "member": "nobody"})
        assert result["success"] is False
        assert "no guild member matches" in result["error"]

    def test_member_search_denied_says_what_is_missing(
        self, discord_guests_module, token_env, discord
    ):
        discord.deny_member_search = True
        result = _call(discord_guests_module, {"action": "add", "member": "friendbot"})
        assert result["success"] is False
        assert "Members Intent" in result["error"]

    def test_missing_member_argument(self, discord_guests_module, token_env, discord):
        result = _call(discord_guests_module, {"action": "add"})
        assert result["success"] is False
        assert "member is required" in result["error"]


class TestRemove:
    def test_drops_role_only(self, discord_guests_module, token_env, discord):
        role_id = _call(discord_guests_module, {"action": "add", "member": "301"})[
            "role_id"
        ]
        staff = discord._channel("203")
        staff["permission_overwrites"] = [
            _overwrite("100", deny=_PERM_VIEW_CHANNEL),
            _overwrite(role_id, allow=_PERM_VIEW_CHANNEL),
        ]

        result = _call(
            discord_guests_module, {"action": "remove", "member": "friendbot"}
        )

        assert result["success"] is True
        assert result["member_id"] == "301"
        assert result["role_id"] == role_id
        assert role_id not in discord.members[1]["roles"]

        # The role survives, its channel overwrites survive, @everyone stays.
        assert discord.guest_role() is not None
        assert int(discord.overwrite("203", role_id)["allow"]) & _PERM_VIEW_CHANNEL
        assert int(discord.overwrite("203", "100")["deny"]) & _PERM_VIEW_CHANNEL
        assert _overwrite_deletes(discord) == []


# ── grant / revoke ──────────────────────────────────────────────────────────


class TestGrant:
    def test_allow_bits_without_operator_bits(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(
            discord_guests_module, {"action": "grant", "channels": ["staff-room"]}
        )

        assert result["success"] is True
        assert result["granted_channel_ids"] == ["203"]
        assert result["allow"] == str(discord_guests_module._GUEST_ALLOW)

        overwrite = discord.overwrite("203", result["role_id"])
        assert int(overwrite["allow"]) == discord_guests_module._GUEST_ALLOW
        assert int(overwrite["deny"]) == 0
        assert int(overwrite["allow"]) & _PERM_VIEW_CHANNEL

        for bit in (
            1 << 1,  # KICK MEMBERS
            1 << 2,  # BAN MEMBERS
            1 << 3,  # ADMINISTRATOR
            1 << 4,  # MANAGE_CHANNELS
            1 << 5,  # MANAGE_GUILD
            1 << 17,  # MENTION_EVERYONE
            1 << 28,  # MANAGE_ROLES
        ):
            assert not int(overwrite["allow"]) & bit

        body = json.loads(_overwrite_puts(discord)[0]["body"])
        assert body == {
            "id": result["role_id"],
            "type": 0,
            "allow": str(discord_guests_module._GUEST_ALLOW),
            "deny": "0",
        }

    def test_category_grant_writes_synced_children_through_the_category(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(
            discord_guests_module, {"action": "grant", "channels": ["General"]}
        )

        assert result["success"] is True
        assert sorted(result["granted_channel_ids"]) == ["200", "202"]
        assert _paths(_overwrite_puts(discord)) == [
            f"/channels/200/permissions/{result['role_id']}",
            f"/channels/202/permissions/{result['role_id']}",
        ]

    def test_channel_names_resolve_case_insensitively(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(
            discord_guests_module, {"action": "grant", "channels": ["STAFF-ROOM"]}
        )
        assert result["granted_channel_ids"] == ["203"]

    def test_unknown_channel_is_an_error(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(discord_guests_module, {"action": "grant", "channels": ["nope"]})
        assert result["success"] is False
        assert "unknown channel" in result["error"]
        # The role may have been created, but nothing was granted anywhere.
        assert _overwrite_puts(discord) == []

    def test_channels_required(self, discord_guests_module, token_env, discord):
        result = _call(discord_guests_module, {"action": "grant"})
        assert result["success"] is False
        assert "channels is required" in result["error"]

    def test_regrant_writes_nothing(self, discord_guests_module, token_env, discord):
        first = _call(
            discord_guests_module, {"action": "grant", "channels": ["staff-room"]}
        )
        writes = len(_overwrite_puts(discord))

        again = _call(discord_guests_module, {"action": "grant", "channels": ["203"]})

        assert again["success"] is True
        assert again["role_id"] == first["role_id"]
        assert again["granted_channel_ids"] == []
        assert len(_overwrite_puts(discord)) == writes

    def test_forbidden_masks_are_refused(self, discord_guests_module):
        with pytest.raises(ValueError):
            discord_guests_module._assert_safe_allow(1 << 3)
        with pytest.raises(ValueError):
            discord_guests_module._assert_safe_allow(
                discord_guests_module._GUEST_ALLOW | (1 << 28)
            )
        discord_guests_module._assert_safe_allow(discord_guests_module._GUEST_ALLOW)


class TestRevoke:
    def test_deletes_overwrite_and_leaves_the_role(
        self, discord_guests_module, token_env, discord
    ):
        role_id = _call(
            discord_guests_module, {"action": "grant", "channels": ["staff-room"]}
        )["role_id"]

        result = _call(
            discord_guests_module, {"action": "revoke", "channels": ["staff-room"]}
        )

        assert result["success"] is True
        assert result["revoked_channel_ids"] == ["203"]
        assert discord.overwrite("203", role_id) is None
        assert discord.guest_role() is not None

    def test_revoke_keeps_the_everyone_deny(
        self, discord_guests_module, token_env, discord
    ):
        assert _call(discord_guests_module, {"action": "setup"})["success"] is True
        assert _call(discord_guests_module, {"action": "grant", "channels": ["203"]})[
            "success"
        ]

        result = _call(discord_guests_module, {"action": "revoke", "channels": ["203"]})

        assert result["success"] is True
        assert int(discord.overwrite("203", "100")["deny"]) & _PERM_VIEW_CHANNEL

    def test_revoke_mirrors_grant_on_unsynced_children(
        self, discord_guests_module, token_env, discord
    ):
        assert _call(
            discord_guests_module, {"action": "grant", "channels": ["General"]}
        )["success"]
        role_id = discord.guest_role()["id"]

        result = _call(discord_guests_module, {"action": "revoke", "channels": ["200"]})

        assert sorted(result["revoked_channel_ids"]) == ["200", "202"]
        assert discord.overwrite("200", role_id) is None
        assert discord.overwrite("202", role_id) is None

    def test_revoke_without_overwrite_is_still_success(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(
            discord_guests_module, {"action": "revoke", "channels": ["lounge"]}
        )
        assert result["success"] is True
        assert result["revoked_channel_ids"] == []
        assert _overwrite_deletes(discord) == []


# ── list ────────────────────────────────────────────────────────────────────


class TestList:
    def test_lists_role_members_and_allowed_channels(
        self, discord_guests_module, token_env, discord
    ):
        assert _call(discord_guests_module, {"action": "setup"})["success"] is True
        assert _call(discord_guests_module, {"action": "add", "member": "301"})[
            "success"
        ]
        assert _call(
            discord_guests_module, {"action": "grant", "channels": ["General"]}
        )["success"]
        role_id = discord.guest_role()["id"]

        result = _call(discord_guests_module, {"action": "list"})

        assert result["success"] is True
        assert result["role_id"] == role_id
        assert result["members"] == [{"id": "301", "name": "Friend Bot"}]
        assert {(c["id"], c["type"]) for c in result["allowed_channels"]} == {
            ("200", 4),
            ("202", 0),
        }
        assert "note" not in result

    def test_member_list_denied_reports_the_intent(
        self, discord_guests_module, token_env, discord
    ):
        _seed_guest_role(discord)
        discord.deny_member_list = True

        result = _call(discord_guests_module, {"action": "list"})

        assert result["success"] is True
        assert result["members"] == []
        assert "Members Intent" in result["note"]
        assert result["role_id"] == "888"

    def test_list_without_a_guest_role_says_setup(
        self, discord_guests_module, token_env, discord
    ):
        result = _call(discord_guests_module, {"action": "list"})
        assert result["success"] is False
        assert "setup" in result["error"]


# ── dispatch ────────────────────────────────────────────────────────────────


class TestDispatch:
    def test_unknown_action(self, discord_guests_module, token_env, discord):
        result = _call(discord_guests_module, {"action": "teleport"})
        assert result["success"] is False
        assert "action must be one of" in result["error"]

    def test_token_never_appears_in_an_answer(
        self, discord_guests_module, token_env, discord, monkeypatch
    ):
        def _boom(request):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", hdrs=None, fp=BytesIO(b"{}")
            )

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        for action in ("setup", "add", "remove", "grant", "revoke", "list"):
            result = _call(
                discord_guests_module,
                {"action": action, "member": "300", "channels": ["x"]},
            )
            assert result["success"] is False
            assert _TOKEN not in json.dumps(result)

    def test_request_headers(self, discord_guests_module, token_env, discord):
        _call(discord_guests_module, {"action": "setup"})
        assert discord.calls
        for call in discord.calls:
            assert call["headers"]["authorization"] == f"Bot {_TOKEN}"
            assert call["headers"]["user-agent"] == (
                "DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)"
            )

    def test_schema_shape(self, discord_guests_module):
        schema = discord_guests_module.DISCORD_GUESTS_SCHEMA
        assert schema["name"] == "discord_guests"
        assert schema["parameters"]["required"] == ["action"]
        assert sorted(schema["parameters"]["properties"]["action"]["enum"]) == [
            "add",
            "grant",
            "list",
            "remove",
            "revoke",
            "setup",
        ]
        assert schema["parameters"]["additionalProperties"] is False


# ── transport ───────────────────────────────────────────────────────────────


class TestTransport:
    def test_rate_limit_is_honoured_and_retried(
        self, discord_guests_module, token_env, discord, monkeypatch
    ):
        clock = FakeClock()
        monkeypatch.setattr(discord_guests_module, "time", clock)
        # The role-create call is rate limited once, with a 0.05s retry_after.
        discord.rate_limit_once[f"POST {_API_BASE}/guilds/100/roles"] = 0.05

        result = _call(discord_guests_module, {"action": "setup"})

        assert result["success"] is True
        assert clock.slept == [0.05]
        role_posts = [
            c for c in discord.calls if c["method"] == "POST" and "/roles" in c["url"]
        ]
        assert len(role_posts) == 2

    def test_writes_are_paced(
        self, discord_guests_module, token_env, discord, monkeypatch
    ):
        clock = FakeClock()
        monkeypatch.setattr(discord_guests_module, "time", clock)
        discord_guests_module._MIN_WRITE_INTERVAL = 0.3

        result = _call(
            discord_guests_module,
            {"action": "add", "member": "301", "channels": ["lounge", "staff-room"]},
        )
        assert result["success"] is True

        assert len(_writes(discord)) == 4  # role create, member role, two allows
        # One gap per write after the first, each a full interval.
        assert clock.slept == pytest.approx([0.3, 0.3, 0.3])

    def test_reads_are_not_paced(
        self, discord_guests_module, token_env, discord, monkeypatch
    ):
        clock = FakeClock()
        monkeypatch.setattr(discord_guests_module, "time", clock)
        discord_guests_module._MIN_WRITE_INTERVAL = 0.3
        _seed_guest_role(discord)

        result = _call(discord_guests_module, {"action": "list"})

        assert result["success"] is True
        assert len(discord.calls) >= 3  # roles, members, channels
        assert clock.slept == []


# ── plugin wiring ───────────────────────────────────────────────────────────


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
        assert (
            captured["kwargs"]["check_fn"] is discord_guests_module.check_requirements
        )

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

    def test_manifest_fields(self):
        repo_root = Path(__file__).resolve().parents[3]
        manifest = (repo_root / "plugins" / "discord_guests" / "plugin.yaml").read_text(
            encoding="utf-8"
        )
        for expected in (
            "name: discord_guests",
            "default_enabled: true",
            "kind: standalone",
            "author: Hermes Agent",
            "- discord_guests",
        ):
            assert expected in manifest, manifest
