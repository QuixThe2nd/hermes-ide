"""The response gate's evidence buffer includes this bot's own final replies.

Live symptom (2026-09-25, enforce mode): a human follow-up to the bot's own
answer scored 0.15-0.31 and was denied, because the judge saw only the human
half of the conversation — inbound observation dropped every ``author.bot``
message and nothing observed the bot's own sends, so the instructions' rule
(3) ("the assistant just asked...") referenced evidence that could not exist.

Behavior pinned here: the bot's own delivered FINAL reply, observed at the
outbound send seam, appears in the next evaluation's ``state.recent_messages``
under the bot's display name (the exact value ``state.bot.name`` carries), so
follow-ups to the bot's own answers are judged with the assistant's half of
the conversation present. Previews/interims/notices, other bots' messages,
echo-channel posts and unscoped conversations stay out of the evidence.
"""

import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig, ResponseGateConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.response_gate import GateRuntime, JevDecisionClient

GATE_ENV_VARS = [
    "DISCORD_ALLOWED_CHANNELS",
    "DISCORD_IGNORED_CHANNELS",
    "DISCORD_FREE_RESPONSE_CHANNELS",
    "DISCORD_ALLOWED_USERS",
    "DISCORD_ALLOW_ALL_USERS",
]

BOT_DISPLAY_NAME = "Winnie"


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    """Keep the dev shell's channel gates out of scope resolution (see test_discord_gate_isolation)."""
    for var in GATE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    for var in GATE_ENV_VARS:
        os.environ.pop(var, None)


@pytest.fixture
def no_response_ledger(monkeypatch):
    """Keep the send-seam tests off the recovery DB (not what they assert).

    ``send`` returns the recorder's return value, so the stub passes the
    SendResult through untouched.
    """

    async def _skip(_self, _reply_to, result, *_args, **_kwargs):
        return result

    monkeypatch.setattr(DiscordAdapter, "_record_response_async", _skip)


def _channel(channel_id: int, *, send=None, partial_message=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=channel_id,
        name=f"chan-{channel_id}",
        send=send if send is not None else AsyncMock(return_value=SimpleNamespace(id=7001)),
        get_partial_message=lambda _mid: partial_message
        or SimpleNamespace(edit=AsyncMock()),
    )


def _gate_adapter(*, channels=("555",), echo_channels=(), wire=()) -> DiscordAdapter:
    """Real adapter with a live gate over ``channels`` and a fake connected client.

    ``wire`` registers channel objects with the fake client's channel cache so
    ``send``/``edit_message`` resolve them like a connected Discord client would.
    """
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    gate_cfg = ResponseGateConfig(
        channels=tuple(str(c) for c in channels),
        echo_channels=tuple(str(c) for c in echo_channels),
        mode="enforce",
    )
    adapter._response_gate = GateRuntime.build(
        gate_cfg, "test-credential", bot_name=BOT_DISPLAY_NAME,
        logger=logging.getLogger(__name__),
    )
    channel_by_id = {chan.id: chan for chan in wire}

    def _get_channel(raw_id):
        return channel_by_id.get(raw_id)

    async def _fetch_channel(raw_id):
        return channel_by_id.get(raw_id)

    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=1, name="winnie", display_name=BOT_DISPLAY_NAME),
        get_channel=_get_channel,
        fetch_channel=_fetch_channel,
    )
    return adapter


def _human_message(text: str, *, channel: SimpleNamespace, author=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        content=text,
        channel=channel,
        author=author or SimpleNamespace(id=2, name="tek", display_name="Tek", bot=False),
        reference=None,
        attachments=[],
        mentions=[],
    )


class TestOwnFinalReplyBecomesEvidence:
    """The delivered final reply shows up in the NEXT evaluation's evidence."""

    @pytest.mark.asyncio
    async def test_send_seam_final_reply_in_next_evaluation(self, monkeypatch, no_response_ledger):
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])
        final_text = "jev 1.13 is the latest; it shipped Aug 12."
        result = await adapter.send("555", final_text, metadata={"notify": True})
        assert result.success is True

        captured: list[dict] = []

        async def fake_request(_self, state, chat_id=None):
            captured.append(state)
            return {"answers": {"should_reply": {"type": "noul", "noul": 0.9}}}

        monkeypatch.setattr(JevDecisionClient, "_request", fake_request)
        follow_up = _human_message("any word on when 1.14 is coming out?", channel=channel)
        await adapter._response_gate.evaluate(
            follow_up, channel=channel, bot_name=BOT_DISPLAY_NAME, bot_id=1,
        )

        assert len(captured) == 1
        state = captured[0]
        # The bot's own reply is in the evidence under the exact name the judge
        # matches authors against — rule (3)'s premise exists now.
        assert state["recent_messages"] == [{"author": BOT_DISPLAY_NAME, "content": final_text}]
        assert state["recent_messages"][-1]["author"] == state["bot"]["name"]

    @pytest.mark.asyncio
    async def test_finalize_edit_reply_is_evidence(self, no_response_ledger):
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])
        final_text = "here is the completed answer, delivered by editing the streamed draft"

        result = await adapter.edit_message("555", "7001", final_text, finalize=True)

        assert result.success is True
        assert adapter._response_gate.snapshot_context(channel) == [
            {"author": BOT_DISPLAY_NAME, "content": final_text}
        ]

    @pytest.mark.asyncio
    async def test_human_then_bot_ordering(self, no_response_ledger):
        """Evidence keeps conversation order: human question, then the bot's answer."""
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])
        adapter._response_gate.observe(channel, "Tek", "check the latest jev version")
        await adapter.send("555", "jev 1.13.", metadata={"notify": True})

        snapshot = adapter._response_gate.snapshot_context(channel)
        assert [entry["author"] for entry in snapshot] == ["Tek", BOT_DISPLAY_NAME]


class TestNonFinalSendsStayOutOfEvidence:
    """Only turn-final text is evidence — never previews, interims, acks or notices."""

    @pytest.mark.asyncio
    async def test_interim_send_not_buffered(self, no_response_ledger):
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])

        result = await adapter.send(
            "555", "still working on it…", metadata={"_interim_send": True},
        )

        assert result.success is True
        assert adapter._response_gate.snapshot_context(channel) == []

    @pytest.mark.asyncio
    async def test_metadataless_send_not_buffered(self, no_response_ledger):
        """Judge echo lines, busy acks and restart notices go out with no notify marker."""
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])

        result = await adapter.send("555", "gate error (timeout)")

        assert result.success is True
        assert adapter._response_gate.snapshot_context(channel) == []

    @pytest.mark.asyncio
    async def test_stream_preview_edit_not_buffered(self, no_response_ledger):
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])

        result = await adapter.edit_message("555", "7001", "partial answer so far…", finalize=False)

        assert result.success is True
        assert adapter._response_gate.snapshot_context(channel) == []


class TestOtherBotsStayExcluded:
    """Other bots' chatter never becomes evidence; humans and this bot do."""

    def test_other_bot_message_not_observed(self):
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])
        other_bot = SimpleNamespace(id=9, name="OtherBot", display_name="OtherBot", bot=True)
        adapter._response_gate_observe(
            _human_message("cool tool!", channel=channel, author=other_bot)
        )
        assert adapter._response_gate.snapshot_context(channel) == []

    def test_human_message_observed(self):
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])
        adapter._response_gate_observe(_human_message("hello room", channel=channel))
        snapshot = adapter._response_gate.snapshot_context(channel)
        assert [entry["author"] for entry in snapshot] == ["Tek"]

    def test_own_inbound_echo_is_observed(self):
        """The inbound exclusion is narrowed to OTHER bots: our own echoed message,
        should one ever bypass admission, is conversation evidence too."""
        channel = _channel(555)
        adapter = _gate_adapter(wire=[channel])
        own_user = SimpleNamespace(id=1, name="winnie", display_name=BOT_DISPLAY_NAME, bot=True)
        adapter._client = SimpleNamespace(
            user=own_user, get_channel=lambda _i: None, fetch_channel=AsyncMock(),
        )
        adapter._response_gate_observe(
            _human_message("my own answer echoed back", channel=channel, author=own_user)
        )
        snapshot = adapter._response_gate.snapshot_context(channel)
        assert [entry["author"] for entry in snapshot] == [BOT_DISPLAY_NAME]


class TestGateInactiveOrUnscoped:
    """No gate, an unscoped channel, or an echo channel: the send path is unchanged."""

    @pytest.mark.asyncio
    async def test_gate_off_send_succeeds(self, no_response_ledger):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
        assert adapter._response_gate is None
        channel = _channel(555)
        adapter._client = SimpleNamespace(
            user=SimpleNamespace(id=1, name="winnie", display_name=BOT_DISPLAY_NAME),
            get_channel=lambda _i: channel,
            fetch_channel=AsyncMock(return_value=channel),
        )

        result = await adapter.send("555", "plain answer", metadata={"notify": True})

        assert result.success is True

    @pytest.mark.asyncio
    async def test_unscoped_channel_not_buffered(self, no_response_ledger):
        outside = _channel(999)
        adapter = _gate_adapter(channels=("555",), wire=[outside])

        result = await adapter.send("999", "final in an unscoped channel", metadata={"notify": True})

        assert result.success is True
        assert adapter._response_gate.snapshot_context(outside) == []

    @pytest.mark.asyncio
    async def test_echo_channel_final_not_buffered(self, no_response_ledger):
        echo_chan = _channel(888)
        adapter = _gate_adapter(channels=(), echo_channels=("888",), wire=[echo_chan])

        result = await adapter.send("888", "a reply in an echo channel", metadata={"notify": True})

        assert result.success is True
        assert adapter._response_gate.snapshot_context(echo_chan) == []
