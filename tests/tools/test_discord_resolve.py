"""Tests for tools/discord_resolve_tool.py (resolve_ticket).

The hard safety contract under test: closing a ticket archives the thread
and NEVER issues a DELETE, non-thread channels are refused, and the
reaction listener only acts while a pending, unexpired prompt exists.
"""

import json
import time
from unittest.mock import patch

import pytest

from tools import discord_resolve_tool as drt


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(drt, "_get_bot_token", lambda: "fake-token")


@pytest.fixture(autouse=True)
def _clean_pending():
    drt._save_pending({})
    yield
    drt._save_pending({})


def _thread_channel(archived=False, ctype=11):
    return {
        "id": "123",
        "type": ctype,
        "thread_metadata": {"archived": archived},
    }


def _seed_pending(message_id="msg-1", channel_id="123", expires_in=1800, summary="Fixed the thing"):
    pending = drt._load_pending()
    pending[message_id] = {
        "channel_id": channel_id,
        "summary": summary,
        "expires_at": time.time() + expires_in,
    }
    drt._save_pending(pending)


class TestPropose:
    def test_posts_embed_and_reactions_and_starts_listening(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            return {"id": "msg-1"}

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("propose", "123", "Fixed the thing", timeout_minutes=45))

        assert result["success"] is True
        assert result["message_id"] == "msg-1"
        assert result["timeout_minutes"] == 45

        method, path, body = calls[0]
        assert method == "POST"
        assert path == "/channels/123/messages"
        embed = body["embeds"][0]
        assert "Fixed the thing" in embed["description"]
        assert "45 minutes" in embed["description"]
        assert "never delete" in embed["footer"]["text"]

        reaction_calls = [c for c in calls if c[0] == "PUT"]
        assert len(reaction_calls) == 2
        assert all("/reactions/" in c[1] and c[1].endswith("/@me") for c in reaction_calls)

        entry = drt._load_pending()["msg-1"]
        assert entry["channel_id"] == "123"
        assert entry["summary"] == "Fixed the thing"
        assert 44 * 60 < entry["expires_at"] - time.time() <= 45 * 60

    def test_default_timeout(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            return {"id": "msg-1"}

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("propose", "123", "x"))

        assert result["timeout_minutes"] == drt._DEFAULT_TIMEOUT_MINUTES

    def test_reaction_failure_not_fatal(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            if method == "PUT":
                raise drt.DiscordAPIError(403, "Missing Permissions")
            return {"id": "msg-1"}

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("propose", "123", "x"))

        assert result["success"] is True
        assert "msg-1" in drt._load_pending()

    def test_missing_channel_id(self):
        result = json.loads(drt.resolve_ticket("propose", "", "x"))
        assert result.get("success") is not True


class TestClose:
    def test_archives_thread_and_never_deletes(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            assert method != "DELETE", "resolve_ticket must never DELETE"
            if method == "GET":
                return _thread_channel(archived=any(
                    m == "PATCH" for m, _, _ in calls
                ))
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result["success"] is True
        assert result["archived"] is True
        assert result["deleted"] is False
        patch_calls = [c for c in calls if c[0] == "PATCH"]
        assert patch_calls == [("PATCH", "/channels/123", {"archived": True})]

    def test_refuses_non_thread(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            if method == "GET":
                return _thread_channel(ctype=0)
            raise AssertionError("no write calls allowed for non-thread")

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result.get("success") is not True
        assert "not a thread" in result.get("error", result.get("message", ""))

    def test_idempotent_when_already_archived(self):
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path))
            return _thread_channel(archived=True)

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result["success"] is True
        assert result["already_archived"] is True
        assert all(m == "GET" for m, _ in calls)

    def test_farewell_failure_still_archives(self):
        state = {"archived": False}

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            if method == "GET":
                return _thread_channel(archived=state["archived"])
            if method == "POST":
                raise drt.DiscordAPIError(403, "Missing Access")
            if method == "PATCH":
                state["archived"] = True
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = json.loads(drt.resolve_ticket("close", "123"))

        assert result["success"] is True
        assert result["archived"] is True

    def test_unknown_action(self):
        result = json.loads(drt.resolve_ticket("explode", "123"))
        assert result.get("success") is not True


class TestReactionHandler:
    def test_tick_edits_embed_archives_and_stops_listening(self):
        _seed_pending()
        calls = []
        state = {"archived": False}

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            assert method != "DELETE", "reaction handler must never DELETE"
            if method == "GET" and path == "/channels/123":
                return _thread_channel(archived=state["archived"])
            if method == "PATCH" and path == "/channels/123":
                state["archived"] = True
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "✅")

        assert result == {"acted": True, "decision": "closed", "archived": True}
        embed_edit = next(c for c in calls if c[0] == "PATCH" and c[1].endswith("/messages/msg-1"))
        assert "• closed" in embed_edit[2]["embeds"][0]["footer"]["text"]
        assert ("PATCH", "/channels/123", {"archived": True}) in calls
        assert "msg-1" not in drt._load_pending(), "listener must stop after a decision"

    def test_cross_edits_embed_and_does_not_archive(self):
        _seed_pending()
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            assert not (method == "PATCH" and path == "/channels/123"), "cross must not archive"
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "❌")

        assert result == {"acted": True, "decision": "kept_open"}
        embed_edit = next(c for c in calls if c[0] == "PATCH")
        assert "• kept open" in embed_edit[2]["embeds"][0]["footer"]["text"]
        assert "msg-1" not in drt._load_pending()

    def test_ignores_unrelated_emoji(self):
        _seed_pending()
        assert drt.handle_resolve_reaction("123", "msg-1", "🔥")["acted"] is False
        assert "msg-1" in drt._load_pending(), "unrelated emoji must not stop the listener"

    def test_not_listening_without_pending_entry(self):
        def fake_request(method, path, token, params=None, body=None, timeout=15):
            raise AssertionError("no API calls expected when not listening")

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "✅")

        assert result == {"acted": False, "reason": "not_listening"}

    def test_expired_prompt_times_out_and_stops_listening(self):
        _seed_pending(expires_in=-60)
        calls = []

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            calls.append((method, path, body))
            assert not (method == "PATCH" and path == "/channels/123"), "timeout must not archive"
            return None

        with patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "✅")

        assert result == {"acted": False, "reason": "timed_out"}
        embed_edit = next(c for c in calls if c[0] == "PATCH")
        assert "• timed out" in embed_edit[2]["embeds"][0]["footer"]["text"]
        assert "msg-1" not in drt._load_pending()

    def test_explicit_token_bypasses_secret_scope(self):
        """Regression: raw gateway reaction events run outside the per-turn
        profile secret scope, so _get_bot_token() raises there. The adapter
        passes its configured token explicitly."""
        _seed_pending()

        def explode():
            raise RuntimeError("UnscopedSecretError")

        def fake_request(method, path, token, params=None, body=None, timeout=15):
            assert token == "adapter-token"
            return None

        with patch.object(drt, "_get_bot_token", explode), \
                patch.object(drt, "_discord_request", fake_request):
            result = drt.handle_resolve_reaction("123", "msg-1", "❌", token="adapter-token")

        assert result == {"acted": True, "decision": "kept_open"}


class TestTimeoutConfig:
    def test_config_value_used(self, monkeypatch):
        import hermes_cli.config as cfg

        monkeypatch.setattr(cfg, "load_config", lambda: {"discord": {"resolve_timeout_minutes": 120}})
        assert drt._default_timeout_minutes() == 120

    def test_config_value_clamped(self, monkeypatch):
        import hermes_cli.config as cfg

        monkeypatch.setattr(cfg, "load_config", lambda: {"discord": {"resolve_timeout_minutes": 99999999}})
        assert drt._default_timeout_minutes() == drt._MAX_TIMEOUT_MINUTES

    def test_config_error_falls_back(self, monkeypatch):
        import hermes_cli.config as cfg

        def explode():
            raise RuntimeError("no config")

        monkeypatch.setattr(cfg, "load_config", explode)
        assert drt._default_timeout_minutes() == drt._DEFAULT_TIMEOUT_MINUTES


class TestRegistration:
    def test_registered_in_discord_toolset(self):
        from tools.registry import registry
        from toolsets import TOOLSETS

        assert "resolve_ticket" in TOOLSETS["discord"]["tools"]
        entry = registry.get_entry("resolve_ticket")
        assert entry is not None
        assert entry.toolset == "discord"

    def test_propose_schema_requires_terminal_turn(self):
        from tools.registry import registry

        entry = registry.get_entry("resolve_ticket")
        assert entry is not None
        description = entry.schema["description"].lower()
        assert "propose" in description
        assert "terminal" in description
        assert "no follow-up" in description or "no follow up" in description
        assert "no further tool call" in description
