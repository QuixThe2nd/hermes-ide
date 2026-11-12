"""Gateway wiring tests for the non-blocking goal-intent audit trail."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


SESSION_KEY = "agent:main:telegram:dm:1001"


def _source(*, bot: bool = False) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        user_id="user-1",
        user_name="operator",
        is_bot=bot,
    )


def _event(text: str, *, internal: bool = False, bot: bool = False) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(bot=bot),
        internal=internal,
    )


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture
def runner(hermes_home, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    instance = gateway_run.GatewayRunner(
        GatewayConfig(sessions_dir=hermes_home / "sessions")
    )
    monkeypatch.setattr(instance, "_is_user_authorized", lambda _source: True)
    monkeypatch.setattr(instance, "_session_key_for_source", lambda _source: SESSION_KEY)
    yield instance
    executor = getattr(instance, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


async def _drain(runner) -> None:
    tasks = [task for task in runner._background_tasks if not task.done()]
    if tasks:
        await asyncio.gather(*tasks)


def _shadow_path(home: Path) -> Path:
    return home / "goals" / "intent_shadow.jsonl"


def _rows(home: Path):
    return [json.loads(line) for line in _shadow_path(home).read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_classification_does_not_delay_or_change_reply_and_writes_bounded_record(
    runner, hermes_home
):
    started = threading.Event()
    release = threading.Event()
    text = "x" * 400

    def slow_classify(message, context):
        started.set()
        assert release.wait(timeout=5), "classifier was not released"
        return {
            "label": "chat",
            "reason": "shadow only",
            "confidence": 0.42,
            "contract_draft": None,
        }

    key = runner._session_key_for_source(_source())
    runner._session_state(key).persistent.update_prompt_pending = True
    with (
        patch("hermes_cli.lifecycle.invoke_hook", return_value=[]),
        patch("hermes_cli.goal_intent.build_goal_context", return_value={"goal_active": False}),
        patch("hermes_cli.goal_intent.classify", side_effect=slow_classify) as classify,
    ):
        try:
            result = await asyncio.wait_for(runner._handle_message(_event(text)), timeout=2)
            assert result == f"✓ Sent `{'x' * 20}…` to the update process."
            assert await asyncio.to_thread(started.wait, 2)
            assert not release.is_set(), "reply waited for classification"
        finally:
            release.set()
        await _drain(runner)

    classify.assert_called_once()
    rows = _rows(hermes_home)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "ts", "session_id", "platform", "message_excerpt", "label",
        "reason", "confidence", "contract_draft",
    }
    # The audit row (and the parked decision) key by the REAL session id —
    # the one goal state lives under — not the inbound quick key.
    assert row["session_id"] and row["session_id"] != SESSION_KEY
    assert row["platform"] == "telegram"
    assert row["message_excerpt"] == "x" * 300
    assert row["label"] == "chat"
    assert row["reason"] == "shadow only"
    assert row["confidence"] == 0.42
    assert row["contract_draft"] is None
    parked = runner._auto_goal_pending.get(SESSION_KEY)
    assert parked is not None and parked.done()
    assert parked.result()["session_id"] == row["session_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _event("[SYSTEM: background completion]", internal=True),
        _event("bot relay text", bot=True),
        _event("   \n"),
        _event("/approve"),
        _event("  /goal status"),
    ],
)
async def test_shadow_skips_internal_bot_blank_and_slash_messages(runner, hermes_home, event):
    with patch("hermes_cli.goal_intent.classify") as classify:
        runner._schedule_auto_goal_intent(event, SESSION_KEY)
        await _drain(runner)

    classify.assert_not_called()
    assert not _shadow_path(hermes_home).exists()


@pytest.mark.asyncio
async def test_eligible_message_is_classified_and_parked_for_turn_end(runner):
    # Every eligible message is classified off-path and the decision parked
    # for the turn boundary (no goal mutation at arrival).
    with patch(
        "hermes_cli.goal_intent.evaluate_goal_intent",
        side_effect=lambda text, sid, platform: {
            "label": "chat", "reason": "jev decision", "confidence": 0.9,
            "contract_draft": None, "message": text, "session_id": sid,
            "last_assistant": "", "user_previous_message": "",
            "record": {"label": "chat", "confidence": 0.9},
        },
    ) as evaluate:
        runner._schedule_auto_goal_intent(_event("hi"), SESSION_KEY)
        runner._schedule_auto_goal_intent(_event("hi again"), SESSION_KEY)
        await _drain(runner)

    assert evaluate.call_count == 2
    # The worker resolves the REAL session id (goal state key), not the quick key.
    resolved_sid = evaluate.call_args.args[1]
    assert resolved_sid and resolved_sid != SESSION_KEY
    parked = runner._auto_goal_pending.get(SESSION_KEY)
    assert parked is not None and parked.done()
    assert parked.result()["session_id"] == resolved_sid


def test_chase_papercut_contains_goal_context_and_dedupes_for_thirty_minutes(hermes_home):
    from hermes_cli.goal_intent import record_goal_intent_shadow

    classification = {
        "label": "chase", "reason": "status ping", "confidence": 0.95,
        "contract_draft": None,
    }
    with (
        patch("hermes_cli.goal_intent.classify", return_value=classification),
        patch(
            "hermes_cli.goal_intent.build_goal_context",
            return_value={"goal_active": True, "goal_paused": False},
        ),
        patch("plugins.papercuts.handle_papercuts", return_value='{"success":true}') as handle,
        patch("hermes_cli.goal_intent.time.time", return_value=1_000_000.0),
    ):
        record_goal_intent_shadow("status?", SESSION_KEY, "telegram")

    with (
        patch("hermes_cli.goal_intent.classify", return_value=classification),
        patch("hermes_cli.goal_intent.build_goal_context", return_value={"goal_active": True}),
        patch("plugins.papercuts.handle_papercuts", return_value='{"success":true}') as second,
        patch("hermes_cli.goal_intent.time.time", return_value=1_000_100.0),
    ):
        record_goal_intent_shadow("so u done?", SESSION_KEY, "telegram")
    second.assert_not_called()

    with (
        patch("hermes_cli.goal_intent.classify", return_value=classification),
        patch("hermes_cli.goal_intent.build_goal_context", return_value={"goal_active": True}),
        patch("plugins.papercuts.handle_papercuts", return_value='{"success":true}') as third,
        patch("hermes_cli.goal_intent.time.time", return_value=1_001_801.0),
    ):
        record_goal_intent_shadow("howd it go", SESSION_KEY, "telegram")

    assert handle.call_count == 1
    assert third.call_count == 1
    args = handle.call_args.args[0]
    assert args["action"] == "log"
    assert args["component"] == "auto-goal"
    assert "goal_active=True" in args["observed"]
    assert handle.call_args.kwargs["session_id"] == SESSION_KEY
    assert len(_rows(hermes_home)) == 3


def test_unavailable_papercuts_never_blocks_shadow_logging(hermes_home):
    from hermes_cli.goal_intent import record_goal_intent_shadow

    classification = {
        "label": "chase", "reason": "resume nudge", "confidence": 0.9,
        "contract_draft": None,
    }
    with (
        patch("hermes_cli.goal_intent.classify", return_value=classification),
        patch("hermes_cli.goal_intent.build_goal_context", return_value={"goal_active": True}),
        patch("plugins.papercuts.handle_papercuts", side_effect=RuntimeError("unavailable")),
    ):
        record_goal_intent_shadow("continue", SESSION_KEY, "telegram")

    rows = _rows(hermes_home)
    assert len(rows) == 1
    assert rows[0]["label"] == "chase"
