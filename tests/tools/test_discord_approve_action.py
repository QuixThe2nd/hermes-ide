"""Contract tests for Discord prepared-action approval cards."""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import get_hermes_home
from tools import discord_approve_action_tool as dat
from tools.registry import registry


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(dat, "_get_bot_token", lambda: "fake-token")


@pytest.fixture(autouse=True)
def _clean_pending():
    dat._save_pending({})
    yield
    dat._save_pending({})


@pytest.fixture
def discord():
    calls = []

    def fake_request(method, path, token, params=None, body=None, timeout=15):
        calls.append((method, path, token, body))
        assert method != "DELETE", (
            "approval cards must never issue Discord DELETE requests"
        )
        if method == "POST" and body and "embeds" in body:
            return {"id": "msg-1"}
        if method == "POST":
            return {"id": "posted-1"}
        return None

    with patch.object(dat, "_discord_request", side_effect=fake_request) as request:
        yield SimpleNamespace(calls=calls, request=request)


def _propose(discord, action_kind="custom_note", payload=None, **overrides):
    if payload is None:
        payload = {"note": "follow up later"} if action_kind == "custom_note" else {}
    args = {
        "action": "propose",
        "channel_id": "123",
        "summary": "Run the prepared test action",
        "action_kind": action_kind,
        "action_payload": json.dumps(payload) if isinstance(payload, dict) else payload,
    }
    args.update(overrides)
    result = json.loads(registry.dispatch("discord_approve_action", args))
    assert result["success"] is True, result
    return result


def _message_edits(discord):
    return [
        call
        for call in discord.calls
        if call[0] == "PATCH" and call[1] == "/channels/123/messages/msg-1"
    ]


class TestPropose:
    def test_registry_posts_embed_seeds_reactions_and_registers_pending(self, discord):
        result = _propose(
            discord,
            payload={"note": "context"},
            timeout_minutes=45,
            require_comment=True,
        )

        assert result["message_id"] == "msg-1"
        assert result["timeout_minutes"] == 45
        assert result["require_comment"] is True
        method, path, token, body = discord.calls[0]
        assert (method, path, token) == ("POST", "/channels/123/messages", "fake-token")
        embed = body["embeds"][0]
        assert embed["title"] == "✅ Approve: Run the prepared test action"
        assert "✅" in embed["description"] and "execute" in embed["description"]
        assert "❌" in embed["description"] and "decline" in embed["description"]
        assert "follow-up comment" in embed["description"]

        reactions = [call for call in discord.calls if call[0] == "PUT"]
        assert len(reactions) == 2
        assert all(
            "/reactions/" in call[1] and call[1].endswith("/@me") for call in reactions
        )

        entry = dat._load_pending()["msg-1"]
        assert entry["channel_id"] == "123"
        assert entry["summary"] == "Run the prepared test action"
        assert entry["action_kind"] == "custom_note"
        assert json.loads(entry["action_payload"]) == {"note": "context"}
        assert entry["require_comment"] is True
        assert 44 * 60 < entry["expires_at"] - time.time() <= 45 * 60

    def test_default_timeout_is_seven_days(self, discord):
        result = _propose(discord)

        assert result["timeout_minutes"] == 10080
        entry = dat._load_pending()["msg-1"]
        assert 10079 * 60 < entry["expires_at"] - time.time() <= 10080 * 60

    @pytest.mark.parametrize(("configured", "expected"), [(120, 120), (999999, 10080)])
    def test_timeout_config_uses_resolve_style_clamping(
        self, monkeypatch, configured, expected
    ):
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg,
            "load_config",
            lambda: {"discord": {"approve_action_timeout_minutes": configured}},
        )
        assert dat._default_timeout_minutes() == expected

    def test_invalid_json_is_rejected_before_posting(self, discord):
        result = json.loads(
            registry.dispatch(
                "discord_approve_action",
                {
                    "action": "propose",
                    "channel_id": "123",
                    "summary": "Invalid proposal",
                    "action_kind": "custom_note",
                    "action_payload": "not-json",
                },
            )
        )

        assert result.get("success") is not True
        assert "valid JSON" in result["error"]
        assert discord.calls == []
        assert dat._load_pending() == {}

    def test_run_script_path_escape_is_rejected_before_posting(self, discord, tmp_path):
        outside = tmp_path / "outside.py"
        result = json.loads(
            registry.dispatch(
                "discord_approve_action",
                {
                    "action": "propose",
                    "channel_id": "123",
                    "summary": "Must not run",
                    "action_kind": "run_script",
                    "action_payload": json.dumps({"path": str(outside)}),
                },
            )
        )

        assert result.get("success") is not True
        assert "must live under" in result["error"]
        assert discord.calls == []
        assert dat._load_pending() == {}

    def test_reaction_seed_failure_does_not_lose_pending_card(self, discord):
        original = dat._discord_request.side_effect

        def fail_reactions(method, path, token, params=None, body=None, timeout=15):
            if method == "PUT":
                raise dat.DiscordAPIError(403, "Missing Permissions")
            return original(
                method, path, token, params=params, body=body, timeout=timeout
            )

        discord.request.side_effect = fail_reactions
        result = _propose(discord)

        assert result["success"] is True
        assert "msg-1" in dat._load_pending()


class TestApprovedActions:
    def test_post_message_executes_and_marks_card_approved(self, discord):
        _propose(
            discord, "post_message", {"channel_id": "999", "content": "Approved update"}
        )
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "✅")

        assert result["acted"] is True
        assert result["decision"] == "approved"
        assert result["success"] is True
        assert result["result"]["message_id"] == "posted-1"
        assert (
            "POST",
            "/channels/999/messages",
            "fake-token",
            {"content": "Approved update"},
        ) in discord.calls
        edit = _message_edits(discord)[0][3]["embeds"][0]
        assert edit["title"].startswith("✅ Approved — Message posted")
        assert edit["color"] == dat._EMBED_COLOR
        assert "msg-1" not in dat._load_pending()

    def test_schedule_cron_uses_existing_store_and_registers_job(
        self, discord, monkeypatch
    ):
        import cron.jobs as jobs
        import cron.scheduler_provider as provider

        job = {
            "id": "job-1",
            "name": "Approved job",
            "enabled": True,
            "next_run_at": "2026-11-08T00:00:00+00:00",
        }
        create = MagicMock(return_value=job)
        scheduler = MagicMock()
        monkeypatch.setattr(jobs, "create_job", create)
        monkeypatch.setattr(provider, "resolve_cron_scheduler", lambda: scheduler)
        _propose(
            discord,
            "schedule_cron",
            {
                "schedule": "every 2h",
                "prompt": "Post the approved status update",
                "name": "Approved job",
                "deliver": "local",
            },
        )
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "✅")

        assert result["success"] is True
        create.assert_called_once_with(
            schedule="every 2h",
            prompt="Post the approved status update",
            name="Approved job",
            deliver="local",
            paused=False,
        )
        scheduler.register_job.assert_called_once_with(job)
        assert result["result"]["job_id"] == "job-1"
        assert _message_edits(discord)[0][3]["embeds"][0]["color"] == dat._EMBED_COLOR
        assert "msg-1" not in dat._load_pending()

    def test_run_script_invokes_subprocess_with_ten_minute_timeout(
        self, discord, monkeypatch
    ):
        scripts = get_hermes_home() / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        script = scripts / "approved.py"
        script.write_text("print('approved')\n", encoding="utf-8")
        completed = SimpleNamespace(returncode=0, stdout="approved\n", stderr="")
        run = MagicMock(return_value=completed)
        monkeypatch.setattr(dat.subprocess, "run", run)
        _propose(discord, "run_script", {"path": "approved.py", "args": ["--yes"]})
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "✅")

        assert result["success"] is True
        assert result["result"]["stdout"] == "approved\n"
        command = run.call_args.args[0]
        assert command[1:] == [str(script.resolve()), "--yes"]
        assert run.call_args.kwargs["timeout"] == 600
        assert run.call_args.kwargs["cwd"] == str(script.parent)
        assert run.call_args.kwargs["env"]["HERMES_HOME"] == str(get_hermes_home())
        assert _message_edits(discord)[0][3]["embeds"][0]["title"].startswith(
            "✅ Approved — Script completed successfully"
        )
        assert "msg-1" not in dat._load_pending()

    def test_custom_note_records_decision_without_execution(self, discord):
        _propose(discord, "custom_note", {"note": "agent follow-up", "ticket": 7})
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "✅")

        assert result["success"] is True
        assert result["result"]["note"] == {"note": "agent follow-up", "ticket": 7}
        assert len(discord.calls) == 1
        assert discord.calls[0][0] == "PATCH"
        assert "Decision recorded" in discord.calls[0][3]["embeds"][0]["title"]
        assert "msg-1" not in dat._load_pending()

    def test_execution_time_path_escape_is_rejected_without_subprocess(
        self, discord, monkeypatch, tmp_path
    ):
        outside = tmp_path / "outside.py"
        pending = dat._load_pending()
        pending["msg-1"] = {
            "channel_id": "123",
            "summary": "Tampered pending entry",
            "action_kind": "run_script",
            "action_payload": json.dumps({"path": str(outside)}),
            "expires_at": time.time() + 600,
        }
        dat._save_pending(pending)
        run = MagicMock()
        monkeypatch.setattr(dat.subprocess, "run", run)
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "✅")

        assert result["acted"] is True
        assert result["success"] is False
        assert "must live under" in result["result"]["message"]
        run.assert_not_called()
        assert "msg-1" not in dat._load_pending()
        assert _message_edits(discord)[0][3]["embeds"][0]["title"].startswith(
            "✅ Approved — Action failed"
        )


class TestOtherReactionOutcomes:
    def test_decline_edits_gray_and_does_not_execute(self, discord):
        _propose(
            discord, "post_message", {"channel_id": "999", "content": "Must not post"}
        )
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "❌")

        assert result == {"acted": True, "decision": "declined"}
        assert len(discord.calls) == 1
        embed = discord.calls[0][3]["embeds"][0]
        assert embed["title"] == "❌ Declined"
        assert embed["color"] == dat._EMBED_COLOR_MUTED
        assert "not executed" in embed["description"]
        assert "msg-1" not in dat._load_pending()

    def test_expired_card_is_settled_without_execution(self, discord):
        _propose(
            discord, "post_message", {"channel_id": "999", "content": "Must not post"}
        )
        pending = dat._load_pending()
        pending["msg-1"]["expires_at"] = time.time() - 60
        dat._save_pending(pending)
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "✅")

        assert result == {"acted": False, "reason": "timed_out"}
        assert len(discord.calls) == 1
        embed = discord.calls[0][3]["embeds"][0]
        assert "⌛" in embed["title"] and "expired" in embed["title"].lower()
        assert embed["color"] == dat._EMBED_COLOR_MUTED
        assert "msg-1" not in dat._load_pending()

    def test_non_pending_message_is_ignored(self, discord):
        result = dat.handle_approve_reaction("123", "other-message", "✅")

        assert result == {"acted": False, "reason": "not_listening"}
        assert discord.calls == []

    def test_wrong_emoji_is_ignored_and_pending_survives(self, discord):
        _propose(discord)
        discord.calls.clear()

        result = dat.handle_approve_reaction("123", "msg-1", "🔥")

        assert result == {"acted": False, "reason": "unrelated_emoji"}
        assert "msg-1" in dat._load_pending()
        assert discord.calls == []

    def test_explicit_adapter_token_bypasses_secret_scope(self, discord, monkeypatch):
        _propose(discord)
        discord.calls.clear()

        def explode():
            raise RuntimeError("UnscopedSecretError")

        monkeypatch.setattr(dat, "_get_bot_token", explode)
        result = dat.handle_approve_reaction(
            "123", "msg-1", "❌", token="adapter-token"
        )

        assert result["decision"] == "declined"
        assert discord.calls[0][2] == "adapter-token"


class TestRegistration:
    def test_registry_and_toolsets_expose_the_service_gated_tool(self):
        from tools.discord_tool import check_discord_tool_requirements
        from toolsets import TOOLSETS

        entry = registry.get_entry("discord_approve_action")
        assert entry is not None
        assert entry.toolset == "discord"
        assert entry.check_fn is check_discord_tool_requirements
        assert entry.requires_env == ["DISCORD_BOT_TOKEN"]
        assert "discord_approve_action" in TOOLSETS["discord"]["tools"]
        assert "discord_approve_action" in TOOLSETS["hermes-discord"]["tools"]
        assert (
            entry.schema["parameters"]["properties"]["require_comment"]["default"]
            is False
        )
