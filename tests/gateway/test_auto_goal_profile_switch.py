"""``goals.auto.enabled`` and ``display.jev_notices`` on the goal-intent lane, read from the
profile that owns the source (real ``_resolve_profile_home_for_source``), never the default."""

import asyncio
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.jev_notice import go_formatter_binary
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

WINNIE_OFF = "display:\n  jev_notices: false\ngoals:\n  auto:\n    enabled: false\n"


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """Multiplexed runner serving ``default`` (switches unset) and ``winnie`` (both off)."""
    from gateway.run import GatewayRunner

    root = tmp_path / "hh"
    (root / "profiles" / "winnie").mkdir(parents=True)
    (root / "config.yaml").write_text("goals:\n  max_turns: 5\n", encoding="utf-8")
    (root / "profiles" / "winnie" / "config.yaml").write_text(WINNIE_OFF, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    instance = object.__new__(GatewayRunner)
    instance.config = GatewayConfig(multiplex_profiles=True)
    instance.spawned, instance.winnie_config = [], root / "profiles" / "winnie" / "config.yaml"

    def _spawn(source, coro, session_key, *, park):
        coro.close()
        instance.spawned.append((source.profile, park))

    instance._spawn_goal_intent_task = _spawn
    with patch("hermes_cli.profiles.get_profile_dir",
               side_effect=lambda n: root if n == "default" else root / "profiles" / n), \
            patch("hermes_cli.profiles.profile_exists", return_value=True):
        yield instance


def _source(profile):
    return SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1", user_name="Alice",
                         profile=profile)


def _schedule(runner, profile, text="please refactor the parser"):
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(profile))
    with patch("hermes_cli.goal_intent.evaluate_goal_intent") as evaluate:
        runner._schedule_auto_goal_intent(event, f"agent:{profile}:discord:c1")
    evaluate.assert_not_called()  # scheduling never classifies inline


@pytest.mark.parametrize("profile, text, spawned", [
    ("default", "please refactor the parser", [("default", True)]),
    ("winnie", "please refactor the parser", []),  # no Jev call, no parked decision
    ("winnie", "stop", []),  # the whole automatic layer is off, its stop pause included
])
def test_auto_goal_schedules_only_where_the_owning_profile_enables_it(runner, profile, text, spawned):
    _schedule(runner, profile, text)

    assert runner.spawned == spawned


def test_leftover_auto_mode_key_keeps_auto_goals_on(runner):
    runner.winnie_config.write_text("goals:\n  auto:\n    mode: off\n", encoding="utf-8")

    _schedule(runner, "winnie")

    assert runner.spawned == [("winnie", True)]


def test_retrospective_gate_reads_the_owning_profile_config(runner):
    runner.winnie_config.write_text(
        "goals:\n"
        "  auto:\n"
        "    enabled: true\n"
        "    retrospective:\n"
        "      enabled: false\n",
        encoding="utf-8",
    )

    assert runner._retrospective_goals_enabled(_source("default")) is True
    assert runner._retrospective_goals_enabled(_source("winnie")) is False
    assert runner._retrospective_goals_enabled(_source("default")) is True


@pytest.mark.skipif(go_formatter_binary() is None, reason="jev notices need the Go formatter binary")
@pytest.mark.asyncio
@pytest.mark.parametrize("profile, posts", [("default", True), ("winnie", False)])
async def test_goal_intent_jev_notice_follows_the_owning_profile(runner, profile, posts):
    sent = []

    async def _notice(source, line, non_conversational=False):
        sent.append(line)

    runner._send_goal_status_notice = _notice
    on_attempt = runner._jev_attempt_notifier(_source(profile), asyncio.get_running_loop())
    await asyncio.to_thread(on_attempt, 1, 2, "task", 0.9, None)
    await asyncio.sleep(0.05)

    assert bool(sent) is posts
