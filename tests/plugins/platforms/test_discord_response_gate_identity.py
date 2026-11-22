"""Discord response-gate bot identity must follow config and the connected user."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from plugins.platforms.discord.adapter import DiscordAdapter
import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.response_gate import (
    ADDRESSES_BOT_KEY,
    CONTINUES_THREAD_KEY,
    JOINS_BOT_THREAD_KEY,
    NOISE_KEY,
    JevDecisionClient,
    build_gate_questions,
)

_AUTOMATIC = object()


def test_real_config_loader_trims_and_roundtrips_bot_name(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "platforms:\n"
        "  discord:\n"
        "    enabled: true\n"
        "    response_gate:\n"
        "      channels: [lounge]\n"
        '      bot_name: "  Huhn  "\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    platform = load_gateway_config().platforms[Platform.DISCORD]

    assert platform.gate_config_warnings == ()
    assert platform.response_gate is not None
    assert getattr(platform.response_gate, "bot_name", None) == "Huhn"
    assert platform.response_gate.to_dict()["bot_name"] == "Huhn"


@pytest.mark.parametrize(
    "gate_data",
    [
        {"channels": ["lounge"]},
        {"channels": ["lounge"], "bot_name": None},
    ],
)
def test_unset_or_null_bot_name_selects_automatic_identity(gate_data):
    platform = PlatformConfig.from_dict({"response_gate": gate_data})

    assert platform.response_gate is not None
    assert getattr(platform.response_gate, "bot_name", None) is None
    assert "bot_name" not in platform.response_gate.to_dict()


@pytest.mark.parametrize(
    "bot_name",
    ["", " \t ", 42, 1.5, True, ["Huhn"], {"name": "Huhn"}],
)
def test_invalid_bot_name_is_contained_as_a_gate_config_error(bot_name, caplog):
    platform = PlatformConfig.from_dict({
        "enabled": True,
        "response_gate": {"channels": ["lounge"], "bot_name": bot_name},
    })

    assert platform.enabled is True
    assert platform.response_gate is None
    assert any(
        "response_gate.bot_name" in warning
        for warning in platform.gate_config_warnings
    )
    assert any(
        "Ignoring invalid response_gate config" in record.message
        for record in caplog.records
    )


def test_address_examples_interpolate_the_selected_name_in_true_and_false_branches():
    questions = build_gate_questions("Huhn")
    criteria = questions[ADDRESSES_BOT_KEY]["criteria"]

    assert criteria["true"]["examples"] == [
        "hey Huhn whats up",
        "Huhn can you check this",
        "what do you think Huhn?",
    ]
    assert criteria["false"]["examples"] == [
        "i saw Huhn in the other channel",
        "the example greeting is 'hi Huhn whats up'",
        "Huhn is down again lol",
    ]
    examples = criteria["true"]["examples"] + criteria["false"]["examples"]
    assert all("winnie" not in example.lower() for example in examples)


def _adapter(user, *, bot_name=_AUTOMATIC):
    gate_data: dict[str, Any] = {"channels": ["555"], "mode": "shadow"}
    if bot_name is not _AUTOMATIC:
        gate_data["bot_name"] = bot_name
    config = PlatformConfig.from_dict({
        "enabled": True,
        "token": "test-token",
        "response_gate": gate_data,
    })
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(user=user)  # type: ignore[assignment]
    adapter._response_gate_credential = "test-credential"
    adapter._response_gate_init()
    return adapter


async def _capture_decision(adapter, monkeypatch):
    captured = {}

    async def request(client, state, *, chat_id=None, questions=None):
        captured["state"] = state
        captured["questions"] = questions if questions is not None else client._questions
        scores = {
            ADDRESSES_BOT_KEY: 0.9,
            CONTINUES_THREAD_KEY: 0.0,
            JOINS_BOT_THREAD_KEY: 0.0,
            NOISE_KEY: 0.0,
        }
        return {
            "answers": {
                key: {"type": "noul", "noul": value}
                for key, value in scores.items()
            }
        }

    monkeypatch.setattr(JevDecisionClient, "_request", request)
    adapter._post_jev_notice = AsyncMock()
    channel = SimpleNamespace(id=555, parent_id=None)
    message = SimpleNamespace(
        id=42,
        channel=channel,
        content="is this for the bot?",
        author=SimpleNamespace(id=7, display_name="Alice", name="Alice"),
        attachments=[],
        reference=None,
        mentions=[],
    )
    candidate = {
        "message_id": "42",
        "conversation_id": "555",
        "channel_keys": {"555"},
    }

    await adapter._response_gate_decide(message, candidate)

    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user, configured_name, expected",
    [
        (
            SimpleNamespace(display_name="Winnie", username="winnie", name="winnie"),
            "  Huhn  ",
            "Huhn",
        ),
        (
            SimpleNamespace(display_name="Connected Display", username="connected", name="connected"),
            _AUTOMATIC,
            "Connected Display",
        ),
        (
            SimpleNamespace(display_name="", username="username-fallback", name="internal-name"),
            _AUTOMATIC,
            "username-fallback",
        ),
        (
            SimpleNamespace(display_name="", username="", name="name-fallback"),
            _AUTOMATIC,
            "name-fallback",
        ),
        (
            SimpleNamespace(display_name="", username="", name=""),
            _AUTOMATIC,
            "assistant",
        ),
    ],
)
async def test_adapter_resolves_one_identity_for_questions_and_judge_state(
    user, configured_name, expected, monkeypatch
):
    adapter = _adapter(user, bot_name=configured_name)
    # Identity is a connect-time value; later client-user changes cannot split
    # the judge state from the question set built during initialization.
    adapter._client.user.display_name = "Changed After Connect"

    captured = await _capture_decision(adapter, monkeypatch)

    assert captured["state"]["bot"]["name"] == expected
    examples = captured["questions"][ADDRESSES_BOT_KEY]["criteria"]["true"]["examples"]
    assert examples[0] == f"hey {expected} whats up"


class _ReadyGateBot:
    """Bot transport whose user appears only when START releases READY."""

    def __init__(self, user, started, **_kwargs):
        self.user = None
        self._ready_user = user
        self._started = started
        self._events = {}
        self._closed = asyncio.Event()
        self._is_closed = False
        self.release_ready = asyncio.Event()

    def event(self, callback):
        self._events[callback.__name__] = callback
        return callback

    async def start(self, _token):
        await self._started.put(self)
        await self.release_ready.wait()
        self.user = self._ready_user
        await self._events["on_ready"]()
        await self._closed.wait()

    def is_closed(self):
        return self._is_closed

    async def close(self):
        self._is_closed = True
        self.release_ready.set()
        self._closed.set()


def _real_connect_adapter(monkeypatch, users, *, bot_name=_AUTOMATIC):
    gate_data: dict[str, Any] = {"channels": ["555"], "mode": "shadow"}
    if bot_name is not _AUTOMATIC:
        gate_data["bot_name"] = bot_name
    adapter = DiscordAdapter(PlatformConfig.from_dict({
        "enabled": True,
        "token": "test-token",
        "slash_commands": False,
        "response_gate": gate_data,
    }))
    assert adapter._client is None

    started = asyncio.Queue()
    bots = []

    def bot_factory(**kwargs):
        bot = _ReadyGateBot(users[len(bots)], started, **kwargs)
        bots.append(bot)
        return bot

    monkeypatch.setattr(discord_platform.commands, "Bot", bot_factory)
    monkeypatch.setattr(discord_platform.Intents, "default", lambda: SimpleNamespace(
        message_content=False, dm_messages=False, guild_messages=False,
        members=False, voice_states=False,
    ))
    monkeypatch.setenv("LITELLM_API_KEY", "test-credential")
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *args: True)
    monkeypatch.setattr(adapter, "_release_platform_lock", lambda: None)
    monkeypatch.setattr(adapter, "_run_post_connect_initialization", AsyncMock())
    monkeypatch.setattr(adapter, "_missed_message_backfill_enabled", lambda: False)
    monkeypatch.setattr(adapter, "_start_liveness_probe", lambda: None)
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", lambda client: None)

    ready_observations = []

    async def observe_before_ready():
        question = adapter._response_gate.client._questions[ADDRESSES_BOT_KEY]
        ready_observations.append((
            adapter._response_gate_bot_name,
            question["criteria"]["true"]["examples"][0],
            adapter._ready_event.is_set(),
        ))

    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", observe_before_ready)
    return adapter, bots, started, ready_observations


async def _begin_real_connect(adapter, started, *, is_reconnect=False):
    task = asyncio.create_task(adapter.connect(is_reconnect=is_reconnect))
    bot = await asyncio.wait_for(started.get(), timeout=2)
    return task, bot


def _captured_identity(captured):
    question = captured["questions"][ADDRESSES_BOT_KEY]
    return (
        captured["state"]["bot"]["name"],
        question["criteria"]["true"]["examples"][0],
    )


def _expected_identity(name):
    return name, f"hey {name} whats up"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_name, pre_ready_name, first_name, second_name",
    [
        (_AUTOMATIC, "assistant", "First Display", "Second Display"),
        ("Configured Override", "Configured Override", "Configured Override", "Configured Override"),
    ],
)
async def test_real_connect_resolves_ready_identity_and_refreshes_reconnect(
    monkeypatch, configured_name, pre_ready_name, first_name, second_name,
):
    users = [
        SimpleNamespace(id=101, display_name="First Display", username="first", name="first"),
        SimpleNamespace(id=202, display_name="Second Display", username="second", name="second"),
    ]
    adapter, bots, started, ready_observations = _real_connect_adapter(
        monkeypatch, users, bot_name=configured_name,
    )
    connect_tasks = []
    try:
        first_connect, first_bot = await _begin_real_connect(adapter, started)
        connect_tasks.append(first_connect)
        assert adapter._client is first_bot
        assert first_bot.user is None
        assert not adapter._ready_event.is_set()
        before_first = await _capture_decision(adapter, monkeypatch)

        first_bot.release_ready.set()
        assert await asyncio.wait_for(first_connect, timeout=2) is True
        assert adapter._ready_event.is_set()
        after_first = await _capture_decision(adapter, monkeypatch)

        second_connect, second_bot = await _begin_real_connect(
            adapter, started, is_reconnect=True,
        )
        connect_tasks.append(second_connect)
        assert adapter._client is second_bot
        assert second_bot.user is None
        assert not adapter._ready_event.is_set()
        before_second = await _capture_decision(adapter, monkeypatch)

        second_bot.release_ready.set()
        assert await asyncio.wait_for(second_connect, timeout=2) is True
        assert adapter._ready_event.is_set()
        after_second = await _capture_decision(adapter, monkeypatch)

        assert [
            _captured_identity(captured)
            for captured in (before_first, after_first, before_second, after_second)
        ] == [
            _expected_identity(name)
            for name in (pre_ready_name, first_name, first_name, second_name)
        ]
        assert ready_observations == [
            (*_expected_identity(first_name), False),
            (*_expected_identity(second_name), False),
        ]
        assert adapter._response_gate_credential == "test-credential"
    finally:
        for bot in bots:
            bot.release_ready.set()
        for task in connect_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*connect_tasks, return_exceptions=True)
        await adapter.disconnect()
