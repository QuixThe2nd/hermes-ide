"""Discord response-gate bot identity must follow config and the connected user."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from plugins.platforms.discord.adapter import DiscordAdapter
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
