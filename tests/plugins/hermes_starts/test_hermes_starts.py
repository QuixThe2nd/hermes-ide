"""Tests for the bundled hermes_starts plugin."""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import stat
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_API_BASE = "https://discord.com/api/v10"
_MENTION_UID = "123456789012345678"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


@pytest.fixture
def hermes_starts_module():
    repo_root = Path(__file__).resolve().parents[3]
    plugin_dir = repo_root / "plugins" / "hermes_starts"
    module_name = "hermes_starts_plugin_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "__init__.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def token_env(_isolate_env):
    env_path = _isolate_env / ".env"
    env_path.write_text("DISCORD_BOT_TOKEN=test-bot-token\n", encoding="utf-8")
    return env_path


def _write_state(home: Path, counter: int = 0, channel_id: str = "channel-existing") -> Path:
    state_path = home / "hermes_starts" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "guild_id": "guild-1",
                "channel_id": channel_id,
                "channel_name": "inbox",
                "welcome_message_id": "msg-0",
                "counter": counter,
            }
        ),
        encoding="utf-8",
    )
    return state_path


def _write_plugin_settings(home: Path, settings: Dict[str, Any]) -> None:
    """Drop a hermes_starts settings block into the isolated config.yaml."""
    config = {
        "plugins": {
            "entries": {
                "hermes_starts": {
                    "enabled": True,
                    "settings": settings,
                }
            }
        }
    }
    (home / "config.yaml").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )


class MockDiscordRouter:
    """Routes the Discord REST calls the plugin makes, the way Discord answers.

    Thread-member adds really do come back as an empty 204 body, and
    ``_discord_request`` must treat that as success rather than a JSON error.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.guilds: List[Dict[str, str]] = [{"id": "guild-1", "name": "Test Guild"}]
        self.channel_counter = 0
        self.message_counter = 0
        self.thread_counter = 0
        self.fail_pin = False
        self.fail_thread_create = False
        self.fail_thread_member = False
        self.thread_returns_no_id = False

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

        if method == "GET" and url.endswith("/users/@me/guilds"):
            return self._response(self.guilds)

        if method == "POST" and "/guilds/" in url and url.endswith("/channels"):
            self.channel_counter += 1
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            return self._response(
                {
                    "id": f"channel-{self.channel_counter}",
                    "name": body.get("name", "inbox"),
                    "guild_id": url.split("/guilds/")[1].split("/")[0],
                }
            )

        if method == "POST" and "/messages/" in url and url.endswith("/threads"):
            if self.fail_thread_create:
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", hdrs=None, fp=BytesIO(b'{"message": "no thread"}')
                )
            self.thread_counter += 1
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            payload = {
                "id": f"thread-{self.thread_counter}",
                "name": body.get("name", ""),
                "type": body.get("type", 11),
            }
            if self.thread_returns_no_id:
                payload.pop("id")
            return self._response(payload)

        if method == "POST" and "/channels/" in url and url.endswith("/messages"):
            self.message_counter += 1
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            payload = {"id": f"msg-{self.message_counter}"}
            if "embeds" in body:
                payload["embeds"] = body["embeds"]
            if "content" in body:
                payload["content"] = body["content"]
            return self._response(payload)

        if method == "PUT" and "/thread-members/" in url:
            if self.fail_thread_member:
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", hdrs=None, fp=BytesIO(b'{"message": "no member"}')
                )
            return self._response({}, status=204, empty=True)

        if method == "PUT" and "/pins/" in url:
            if self.fail_pin:
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", hdrs=None, fp=BytesIO(b'{"message": "no pin"}')
                )
            return self._response({})

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
def discord_router(monkeypatch):
    router = MockDiscordRouter()
    monkeypatch.setattr("urllib.request.urlopen", router)
    return router


def _call(hermes_starts_module, args: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(hermes_starts_module.handle_start_conversation(args))


def _endpoints(router: MockDiscordRouter) -> List[Any]:
    assert all(call["url"].startswith(_API_BASE) for call in router.calls)
    return [
        (call["method"], call["url"][len(_API_BASE):])
        for call in router.calls
    ]


def _posted_contents(router: MockDiscordRouter) -> List[str]:
    """Content-bearing message bodies, in posting order (welcome embed excluded)."""
    return [
        json.loads(call["body"])["content"]
        for call in router.calls
        if call["method"] == "POST"
        and call["url"].endswith("/messages")
        and call["body"]
        and '"content"' in call["body"]
    ]


class TestMessageFormat:
    def test_compose_message_with_next_move(self, hermes_starts_module):
        text = hermes_starts_module._compose_message(
            "You have been skipping lunch again.",
            "Block 30 minutes on your calendar tomorrow.",
        )
        assert text == (
            "You have been skipping lunch again.\n"
            "\n"
            "*Where I'd take this:* Block 30 minutes on your calendar tomorrow."
        )

    def test_compose_message_without_next_move(self, hermes_starts_module):
        text = hermes_starts_module._compose_message("Why did the linter cross the road?", "")
        assert text == "Why did the linter cross the road?"
        assert "Where I'd take this" not in text


class TestSplitting:
    def test_split_on_paragraph_boundaries(self, hermes_starts_module):
        body = "A" * 1200 + "\n\n" + "B" * 1200
        parts = hermes_starts_module._split_message(body)
        assert len(parts) >= 2
        assert all(len(part) <= hermes_starts_module._MAX_MESSAGE_LEN for part in parts)
        assert "".join(parts).replace("\n\n", "") == body.replace("\n\n", "")

    def test_hard_wrap_without_paragraph_boundaries(self, hermes_starts_module):
        chunk = "x" * 4000
        parts = hermes_starts_module._split_message(chunk)
        assert len(parts) >= 3
        assert all(len(part) <= hermes_starts_module._MAX_MESSAGE_LEN for part in parts)
        assert "".join(parts) == chunk

    def test_mention_prefix_stays_with_first_part(self, hermes_starts_module):
        delivery = f"<@{_MENTION_UID}>\n" + "A" * 1200 + "\n\n" + "B" * 1200
        parts = hermes_starts_module._split_message(delivery)
        assert len(parts) == 2
        assert parts[0].startswith(f"<@{_MENTION_UID}>\n")
        assert not parts[1].startswith("<@")
        assert all(len(part) <= hermes_starts_module._MAX_MESSAGE_LEN for part in parts)

    def test_split_delivery_reserves_mention_room_near_limit(self, hermes_starts_module):
        prefix = f"<@{_MENTION_UID}>\n"
        opening = "x" * 1940

        parts = hermes_starts_module._split_delivery(opening, _MENTION_UID)

        assert parts[0].startswith(prefix)
        # The anchor carries opening text after the ping — packed into the
        # room the split reserved for the prefix — never the ping alone.
        assert set(parts[0][len(prefix):]) == {"x"}
        assert len(parts[0]) == hermes_starts_module._MAX_MESSAGE_LEN
        assert parts[0][len(prefix):] + "".join(parts[1:]) == opening
        assert all(
            len(part) <= hermes_starts_module._MAX_MESSAGE_LEN for part in parts
        )
        assert "<@" not in "".join(parts[1:])

        # Without a mention, delivery splitting is the plain split.
        assert hermes_starts_module._split_delivery(opening, "") == (
            hermes_starts_module._split_message(opening)
        )


class TestCheckRequirements:
    def test_missing_env(self, hermes_starts_module, _isolate_env):
        assert hermes_starts_module.check_requirements() is False

    def test_empty_token(self, hermes_starts_module, _isolate_env):
        (_isolate_env / ".env").write_text("DISCORD_BOT_TOKEN=\n", encoding="utf-8")
        assert hermes_starts_module.check_requirements() is False

    def test_valid_token(self, hermes_starts_module, token_env):
        assert hermes_starts_module.check_requirements() is True


class TestCounterPersistence:
    def test_counter_increments_and_persists(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        state_path = _write_state(_isolate_env, counter=4)

        first = _call(
            hermes_starts_module,
            {
                "action": "start",
                "kind": "personal",
                "message": "First",
                "next_move": "Fix one",
            },
        )
        second = _call(
            hermes_starts_module,
            {
                "action": "start",
                "kind": "compliment",
                "message": "Second",
            },
        )

        assert first["success"] is True
        assert first["start_number"] == 5
        assert second["success"] is True
        assert second["start_number"] == 6

        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["counter"] == 6


class TestCorruptStateRecovery:
    def test_corrupt_state_treated_as_unprovisioned(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        state_path = _isolate_env / "hermes_starts" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{not-json", encoding="utf-8")

        result = _call(
            hermes_starts_module,
            {
                "action": "start",
                "kind": "feedback",
                "message": "Broken state",
                "next_move": "Recreate channel",
            },
        )

        assert result["success"] is True
        assert result["start_number"] == 1
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["channel_id"] == "channel-1"
        assert saved["counter"] == 1


class TestHttpErrors:
    def test_http_error_returns_failure_without_raising(
        self, hermes_starts_module, token_env, monkeypatch, _isolate_env
    ):
        _write_state(_isolate_env)

        def _raise_http(request):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", hdrs=None, fp=BytesIO(b"{}")
            )

        monkeypatch.setattr("urllib.request.urlopen", _raise_http)

        result = _call(
            hermes_starts_module,
            {
                "action": "start",
                "kind": "business",
                "message": "boom",
                "next_move": "retry",
            },
        )
        assert result == {"success": False, "error": "HTTP error: 500"}


class TestSetupProvisioning:
    def test_happy_path_persists_state(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        result = _call(hermes_starts_module, {"action": "setup"})

        assert result["success"] is True
        assert result["guild_id"] == "guild-1"
        assert result["channel_id"] == "channel-1"
        assert result["channel_name"] == "inbox"
        assert result["welcome_message_id"] == "msg-1"

        state_path = _isolate_env / "hermes_starts" / "state.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["guild_id"] == "guild-1"
        assert saved["channel_id"] == "channel-1"
        assert saved["welcome_message_id"] == "msg-1"
        assert saved["counter"] == 0
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

        assert _endpoints(discord_router) == [
            ("GET", "/users/@me/guilds"),
            ("POST", "/guilds/guild-1/channels"),
            ("POST", "/channels/channel-1/messages"),
            ("PUT", "/channels/channel-1/pins/msg-1"),
        ]
        assert discord_router.calls[0]["headers"]["Authorization"] == "Bot test-bot-token"
        assert "test-bot-token" not in json.dumps(result)

        channel_body = json.loads(discord_router.calls[1]["body"])
        assert channel_body["name"] == "inbox"
        assert channel_body["topic"] == hermes_starts_module._CHANNEL_TOPIC

        welcome_body = json.loads(discord_router.calls[2]["body"])
        assert "embeds" in welcome_body
        embed = welcome_body["embeds"][0]
        assert embed["title"] == "📥 Inbox"
        assert "Your AI has always had a reply box. This gives it an opening line." in embed[
            "description"
        ]
        assert embed["footer"]["text"] == "Started by your Hermes agent via Hermes Starts"

    def test_single_guild_auto_detect(self, hermes_starts_module, token_env, discord_router):
        discord_router.guilds = [{"id": "solo-guild", "name": "Only Server"}]
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result["success"] is True
        assert result["guild_id"] == "solo-guild"

    def test_multi_guild_error_lists_guilds(
        self, hermes_starts_module, token_env, discord_router
    ):
        discord_router.guilds = [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result["success"] is False
        assert "multiple guilds" in result["error"]
        assert result["guilds"] == [
            {"id": "g1", "name": "Alpha"},
            {"id": "g2", "name": "Beta"},
        ]

    def test_zero_guild_error(self, hermes_starts_module, token_env, discord_router):
        discord_router.guilds = []
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result == {"success": False, "error": "bot is not in any guild"}

    def test_setup_idempotent_without_force(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        first = _call(hermes_starts_module, {"action": "setup"})
        assert first["success"] is True
        calls_after_first = len(discord_router.calls)

        second = _call(hermes_starts_module, {"action": "setup"})
        assert second["success"] is True
        assert second["already_provisioned"] is True
        assert len(discord_router.calls) == calls_after_first

    def test_force_reprovision(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _call(hermes_starts_module, {"action": "setup", "channel_name": "first-channel"})
        _call(
            hermes_starts_module,
            {"action": "setup", "channel_name": "second-channel", "force": True},
        )

        create_calls = [
            json.loads(call["body"])
            for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        assert [body["name"] for body in create_calls] == ["first-channel", "second-channel"]

        saved = json.loads((_isolate_env / "hermes_starts" / "state.json").read_text())
        assert saved["channel_name"] == "second-channel"
        assert saved["channel_id"] == "channel-2"

    def test_force_reprovision_uses_inbox_default_not_prior_channel_name(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _call(hermes_starts_module, {"action": "setup", "channel_name": "legacy-channel"})
        result = _call(hermes_starts_module, {"action": "setup", "force": True})

        assert result["success"] is True
        assert result["channel_name"] == "inbox"

        create_calls = [
            json.loads(call["body"])
            for call in discord_router.calls
            if call["method"] == "POST" and call["url"].endswith("/channels")
        ]
        assert [body["name"] for body in create_calls] == ["legacy-channel", "inbox"]

        saved = json.loads((_isolate_env / "hermes_starts" / "state.json").read_text())
        assert saved["channel_name"] == "inbox"

    def test_pin_failure_tolerated(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        discord_router.fail_pin = True
        result = _call(hermes_starts_module, {"action": "setup"})
        assert result["success"] is True
        assert "warning" in result
        assert "pin failed" in result["warning"]


class TestStartSingleMessage:
    """The opening itself is the one visible starter message, and the thread
    hangs off it — no stub, no repost of the opening inside the thread."""

    def test_short_start_posts_one_channel_message_and_anchors_thread(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)

        result = _call(
            hermes_starts_module,
            {
                "kind": "idea",
                "message": "The inbox channel should thread itself.",
                "next_move": "Anchor the thread on the opening.",
            },
        )

        assert result["success"] is True
        assert result["action"] == "start"
        assert result["start_number"] == 1
        assert result["channel_id"] == "channel-existing"
        assert result["channel_message_id"] == "msg-1"
        assert result["channel_message_ids"] == ["msg-1"]
        assert result["thread_id"] == "thread-1"
        assert result["thread_name"] == "Start #1 — idea"
        # Nothing else is posted inside the thread for a short opening.
        assert result["thread_message_ids"] == []
        assert "channel_stub_message_id" not in result
        assert "warning" not in result

        contents = _posted_contents(discord_router)
        assert len(contents) == 1
        assert contents[0] == (
            "The inbox channel should thread itself.\n"
            "\n"
            "*Where I'd take this:* Anchor the thread on the opening."
        )

        # Order matters: the opening must exist before a thread can anchor on it.
        assert _endpoints(discord_router) == [
            ("POST", "/channels/channel-existing/messages"),
            ("POST", "/channels/channel-existing/messages/msg-1/threads"),
        ]

        thread_body = json.loads(discord_router.calls[1]["body"])
        assert thread_body["name"] == "Start #1 — idea"
        assert thread_body["type"] == 11

    def test_start_without_next_move_omits_label(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)

        result = _call(
            hermes_starts_module,
            {
                "kind": "compliment",
                "message": "You shipped three things this week.",
            },
        )
        assert result["success"] is True

        contents = _posted_contents(discord_router)
        assert contents == ["You shipped three things this week."]
        assert "Where I'd take this" not in contents[0]

    def test_start_auto_provisions_when_unprovisioned(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        result = _call(
            hermes_starts_module,
            {
                "kind": "idea",
                "message": "Needs a home",
                "next_move": "Create the channel",
            },
        )

        assert result["success"] is True
        assert result["action"] == "start"
        assert result["start_number"] == 1
        assert result["channel_id"] == "channel-1"
        assert result["channel_message_ids"] == ["msg-2"]
        assert result["thread_id"] == "thread-1"

        contents = _posted_contents(discord_router)
        assert contents == ["Needs a home\n\n*Where I'd take this:* Create the channel"]

        assert _endpoints(discord_router) == [
            ("GET", "/users/@me/guilds"),
            ("POST", "/guilds/guild-1/channels"),
            ("POST", "/channels/channel-1/messages"),
            ("PUT", "/channels/channel-1/pins/msg-1"),
            ("POST", "/channels/channel-1/messages"),
            ("POST", "/channels/channel-1/messages/msg-2/threads"),
        ]

    def test_thread_marks_participation_for_gateway_replies(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)

        result = _call(
            hermes_starts_module, {"kind": "joke", "message": "A thread walks into a bar."}
        )

        assert result["success"] is True
        tracked = json.loads(
            (_isolate_env / "discord_threads.json").read_text(encoding="utf-8")
        )
        assert result["thread_id"] in tracked


class TestStartMention:
    def test_configured_mention_prefixes_anchor_and_joins_thread(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        _write_plugin_settings(_isolate_env, {"mention_user_id": _MENTION_UID})

        result = _call(
            hermes_starts_module,
            {"kind": "question", "message": "Did you see the deploy?", "next_move": "Take a look"},
        )

        assert result["success"] is True
        assert result["mentioned_user_id"] == _MENTION_UID
        assert "warning" not in result

        contents = _posted_contents(discord_router)
        assert len(contents) == 1
        assert contents[0].startswith(f"<@{_MENTION_UID}>\n")
        assert "Did you see the deploy?" in contents[0]

        assert _endpoints(discord_router) == [
            ("POST", "/channels/channel-existing/messages"),
            ("POST", "/channels/channel-existing/messages/msg-1/threads"),
            ("PUT", f"/channels/thread-1/thread-members/{_MENTION_UID}"),
        ]

    def test_quiet_hours_suppress_mention_and_member_add(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        # A window that is active right now in UTC, whatever "now" is.
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=5)).strftime("%H:%M")
        end = (now + timedelta(minutes=5)).strftime("%H:%M")
        _write_plugin_settings(
            _isolate_env,
            {"mention_user_id": _MENTION_UID, "quiet_hours": f"{start}-{end}", "quiet_tz": "UTC"},
        )

        result = _call(
            hermes_starts_module, {"kind": "observation", "message": "It is late."}
        )

        assert result["success"] is True
        assert "mentioned_user_id" not in result

        contents = _posted_contents(discord_router)
        assert contents == ["It is late."]
        assert "<@" not in contents[0]

        assert _endpoints(discord_router) == [
            ("POST", "/channels/channel-existing/messages"),
            ("POST", "/channels/channel-existing/messages/msg-1/threads"),
        ]

    def test_non_numeric_mention_id_is_ignored(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        _write_plugin_settings(_isolate_env, {"mention_user_id": "not-a-snowflake"})

        result = _call(
            hermes_starts_module, {"kind": "personal", "message": "Hello there."}
        )

        assert result["success"] is True
        assert "mentioned_user_id" not in result
        assert _posted_contents(discord_router) == ["Hello there."]


class TestQuietHoursWindow:
    @staticmethod
    def _at(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 8, 25, hour, minute, tzinfo=timezone.utc)

    def test_overnight_window_wraps_midnight(self, hermes_starts_module):
        settings = {"quiet_hours": "23:00-08:00", "quiet_tz": "UTC"}
        assert hermes_starts_module._quiet_hours_active(settings, self._at(23, 30)) is True
        assert hermes_starts_module._quiet_hours_active(settings, self._at(3, 0)) is True
        assert hermes_starts_module._quiet_hours_active(settings, self._at(7, 59)) is True
        assert hermes_starts_module._quiet_hours_active(settings, self._at(8, 0)) is False
        assert hermes_starts_module._quiet_hours_active(settings, self._at(12, 0)) is False
        assert hermes_starts_module._quiet_hours_active(settings, self._at(22, 59)) is False

    def test_daytime_window(self, hermes_starts_module):
        settings = {"quiet_hours": "09:00-17:00", "quiet_tz": "UTC"}
        assert hermes_starts_module._quiet_hours_active(settings, self._at(9, 0)) is True
        assert hermes_starts_module._quiet_hours_active(settings, self._at(16, 59)) is True
        assert hermes_starts_module._quiet_hours_active(settings, self._at(17, 0)) is False
        assert hermes_starts_module._quiet_hours_active(settings, self._at(2, 0)) is False

    def test_empty_window_disables_gate_and_misconfig_fails_open(
        self, hermes_starts_module
    ):
        assert hermes_starts_module._quiet_hours_active({"quiet_hours": ""}) is False
        assert hermes_starts_module._quiet_hours_active({"quiet_hours": "garbage"}) is False
        assert hermes_starts_module._quiet_hours_active({"quiet_tz": "Not/AZone"}) is False

    def test_unconfigured_window_defaults_to_overnight(
        self, hermes_starts_module
    ):
        assert hermes_starts_module._quiet_hours_active({}, self._at(2, 0)) is True
        assert hermes_starts_module._quiet_hours_active({}, self._at(12, 0)) is False


class TestLongOpeningSplit:
    _MESSAGE = "A" * 1200 + "\n\n" + "B" * 1200
    _NEXT_MOVE = "C" * 100

    def _composed(self) -> str:
        return (
            self._MESSAGE
            + "\n\n*Where I'd take this:* "
            + self._NEXT_MOVE
        )

    def test_first_part_is_channel_anchor_rest_go_in_thread(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        _write_plugin_settings(_isolate_env, {"mention_user_id": _MENTION_UID})

        result = _call(
            hermes_starts_module,
            {"kind": "feedback", "message": self._MESSAGE, "next_move": self._NEXT_MOVE},
        )

        assert result["success"] is True
        assert result["channel_message_ids"] == ["msg-1"]
        assert result["thread_message_ids"] == ["msg-2"]
        assert result["channel_message_id"] == "msg-1"

        contents = _posted_contents(discord_router)
        assert len(contents) == 2
        assert all(
            len(part) <= hermes_starts_module._MAX_MESSAGE_LEN for part in contents
        )
        # The whole opening survives, in order, with exactly one mention.
        assert contents[0] == f"<@{_MENTION_UID}>\n" + "A" * 1200
        assert contents[1] == "B" * 1200 + "\n\n*Where I'd take this:* " + "C" * 100

        assert _endpoints(discord_router) == [
            ("POST", "/channels/channel-existing/messages"),
            ("POST", "/channels/channel-existing/messages/msg-1/threads"),
            ("PUT", f"/channels/thread-1/thread-members/{_MENTION_UID}"),
            ("POST", "/channels/thread-1/messages"),
        ]

    def test_near_limit_mention_anchor_keeps_opening_text(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        """A mention plus an almost-limit opening must not strand the ping.

        Prefixing the delivery string before splitting used to let the
        newline after the mention become the only break point, leaving a
        bare `<@...>` as the channel anchor and pushing the entire opening
        into the thread.
        """
        _write_state(_isolate_env)
        _write_plugin_settings(_isolate_env, {"mention_user_id": _MENTION_UID})

        opening = "x" * 1940
        result = _call(
            hermes_starts_module,
            {"kind": "advice", "message": opening},
        )

        assert result["success"] is True
        assert result["mentioned_user_id"] == _MENTION_UID

        contents = _posted_contents(discord_router)
        prefix = f"<@{_MENTION_UID}>\n"
        assert len(contents) == 2
        assert result["channel_message_ids"] == ["msg-1"]
        assert result["thread_message_ids"] == ["msg-2"]

        anchor = contents[0]
        assert anchor.startswith(prefix)
        # Real opening text rides with the ping, packed right up to the limit —
        # a bare mention must never be the channel anchor.
        assert set(anchor[len(prefix):]) == {"x"}
        assert len(anchor) == hermes_starts_module._MAX_MESSAGE_LEN
        # …and only the leftover tail lands inside the thread.
        assert set(contents[1]) == {"x"}
        assert anchor[len(prefix):] + contents[1] == opening
        assert len(contents[1]) <= hermes_starts_module._MAX_MESSAGE_LEN
        assert "<@" not in contents[1]

    def test_thread_create_failure_keeps_anchor_and_finishes_in_channel(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        discord_router.fail_thread_create = True

        result = _call(
            hermes_starts_module,
            {"kind": "feedback", "message": self._MESSAGE, "next_move": self._NEXT_MOVE},
        )

        # The opening already exists in the channel, so the start still counts.
        assert result["success"] is True
        assert "thread creation failed: HTTP 403" in result["warning"]
        assert "thread_id" not in result
        assert result["thread_message_ids"] == []
        assert result["channel_message_ids"] == ["msg-1", "msg-2"]

        contents = _posted_contents(discord_router)
        assert len(contents) == 2
        # Anchor kept verbatim, tail delivered, nothing duplicated or lost.
        assert contents[0] == "A" * 1200
        assert contents[1] == "B" * 1200 + "\n\n*Where I'd take this:* " + "C" * 100

        assert _endpoints(discord_router) == [
            ("POST", "/channels/channel-existing/messages"),
            ("POST", "/channels/channel-existing/messages/msg-1/threads"),
            ("POST", "/channels/channel-existing/messages"),
        ]

    def test_short_opening_survives_thread_create_failure(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        discord_router.fail_thread_create = True

        result = _call(
            hermes_starts_module, {"kind": "idea", "message": "No thread for this one."}
        )

        assert result["success"] is True
        assert "thread creation failed" in result["warning"]
        assert _posted_contents(discord_router) == ["No thread for this one."]

    def test_thread_response_without_id_degrades_to_channel_only(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        discord_router.thread_returns_no_id = True

        result = _call(
            hermes_starts_module,
            {"kind": "feedback", "message": self._MESSAGE, "next_move": self._NEXT_MOVE},
        )

        assert result["success"] is True
        assert "returned no id" in result["warning"]
        assert "thread_id" not in result
        assert result["thread_message_ids"] == []
        assert result["channel_message_ids"] == ["msg-1", "msg-2"]
        assert len(_posted_contents(discord_router)) == 2


class TestMemberAddFailure:
    def test_member_add_failure_warns_without_duplicating_the_opening(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        _write_plugin_settings(_isolate_env, {"mention_user_id": _MENTION_UID})
        discord_router.fail_thread_member = True

        result = _call(
            hermes_starts_module,
            {"kind": "advice", "message": "Ship the small fix first.", "next_move": "Open the PR"},
        )

        assert result["success"] is True
        assert result["thread_id"] == "thread-1"
        assert result["mentioned_user_id"] == _MENTION_UID
        assert f"thread member add failed for {_MENTION_UID}: HTTP 403" in result["warning"]
        assert result["channel_message_ids"] == ["msg-1"]
        assert result["thread_message_ids"] == []

        # The opening is still exactly one message — the ping failed, the
        # start did not.
        contents = _posted_contents(discord_router)
        assert len(contents) == 1
        assert contents[0].startswith(f"<@{_MENTION_UID}>\n")

        assert _endpoints(discord_router) == [
            ("POST", "/channels/channel-existing/messages"),
            ("POST", "/channels/channel-existing/messages/msg-1/threads"),
            ("PUT", f"/channels/thread-1/thread-members/{_MENTION_UID}"),
        ]


class TestSessionSeeding:
    def test_thread_session_seeded_with_unprefixed_opening(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)
        _write_plugin_settings(_isolate_env, {"mention_user_id": _MENTION_UID})

        result = _call(
            hermes_starts_module,
            {
                "kind": "business",
                "message": "The renewal is due Friday.",
                "next_move": "Confirm the amount",
            },
        )

        assert result["success"] is True
        thread_id = result["thread_id"]
        assert result["session_seed_key"] == f"agent:main:discord:thread:{thread_id}:{thread_id}"

        from gateway.config import GatewayConfig, Platform
        from gateway.session import SessionSource, SessionStore

        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=thread_id,
            chat_name=f"Big Steve / {result['thread_name']}",
            chat_type="thread",
            user_id="1487993851930214410",
            user_name="Hermes Starts",
            thread_id=thread_id,
        )
        store = SessionStore(_isolate_env / "sessions", GatewayConfig())
        entry = store.get_or_create_session(source)
        assert entry.session_key == result["session_seed_key"]

        transcript = store.load_transcript(entry.session_id)
        openings = [
            message
            for message in transcript
            if message.get("role") == "assistant"
            and "renewal is due Friday" in str(message.get("content"))
        ]
        assert len(openings) == 1
        # The session gets the conversation, not the delivery-format ping.
        assert openings[0]["content"] == (
            "The renewal is due Friday.\n"
            "\n"
            "*Where I'd take this:* Confirm the amount"
        )


class TestKindValidation:
    @pytest.mark.parametrize(
        "kind",
        [
            "observation",
            "advice",
            "feedback",
            "complaint",
            "compliment",
            "idea",
            "question",
            "joke",
            "personal",
            "business",
        ],
    )
    def test_all_kinds_accepted(
        self, hermes_starts_module, token_env, discord_router, _isolate_env, kind
    ):
        _write_state(_isolate_env)

        result = _call(
            hermes_starts_module,
            {"kind": kind, "message": f"A {kind} opening."},
        )
        assert result["success"] is True
        assert result["start_number"] == 1
        assert result["thread_name"] == f"Start #1 — {kind}"

    def test_invalid_kind_rejected(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)

        result = _call(
            hermes_starts_module,
            {"kind": "grievance", "message": "Not allowed"},
        )
        assert result["success"] is False
        assert "invalid kind" in result["error"]

    def test_invalid_tone_rejected(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        _write_state(_isolate_env)

        result = _call(
            hermes_starts_module,
            {"kind": "idea", "message": "Tone check", "tone": "sarcastic"},
        )
        assert result["success"] is False
        assert "invalid tone" in result["error"]

    def test_missing_fields_rejected(
        self, hermes_starts_module, token_env, discord_router, _isolate_env
    ):
        result = _call(hermes_starts_module, {"kind": "idea"})
        assert result == {"success": False, "error": "missing required fields"}

        result = _call(hermes_starts_module, {"message": "No kind"})
        assert result == {"success": False, "error": "missing required fields"}


class TestForbiddenWords:
    _FORBIDDEN = re.compile(
        r"Grievance|grievance|remediation|Formal record|management",
        re.IGNORECASE,
    )

    def test_no_forbidden_words_in_plugin_surfaces(self, hermes_starts_module):
        repo_root = Path(__file__).resolve().parents[3]
        plugin_dir = repo_root / "plugins" / "hermes_starts"

        readme = (plugin_dir / "README.md").read_text(encoding="utf-8")
        manifest = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
        runtime_strings = " ".join(
            [
                hermes_starts_module.START_CONVERSATION_SCHEMA["description"],
                json.dumps(hermes_starts_module._WELCOME_EMBED),
            ]
        )

        for label, text in [
            ("README", readme),
            ("plugin.yaml", manifest),
            ("runtime", runtime_strings),
        ]:
            matches = self._FORBIDDEN.findall(text)
            assert not matches, f"{label} contains forbidden words: {matches}"


class TestPluginDiscovery:
    def test_register_via_mock_ctx(self, hermes_starts_module):
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

        hermes_starts_module.register(_Ctx())
        assert captured["name"] == "start_conversation"
        assert captured["toolset"] == "hermes_starts"

        entry = registry.get_entry("start_conversation")
        assert entry is not None
        assert entry.toolset == "hermes_starts"

    def test_discover_via_plugin_manager(self, _isolate_env):
        for key in list(sys.modules):
            if key.startswith(("plugins.hermes_starts", "hermes_cli.plugins")):
                del sys.modules[key]

        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        mgr = PluginManager()
        mgr.discover_and_load(force=True)

        assert "hermes_starts" in mgr._plugins
        loaded = mgr._plugins["hermes_starts"]
        assert loaded.enabled is True
        assert loaded.error is None

        entry = registry.get_entry("start_conversation")
        assert entry is not None
        assert entry.toolset == "hermes_starts"
