"""Tests for discord.response_gate echo_channels (judge echo, no session wake)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig, ResponseGateConfig
from gateway.platforms.base import SendResult

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
from plugins.platforms.discord.response_gate import GateDecision, ResponseGateError  # noqa: E402


class _TextChannel:
    def __init__(self, channel_id: int = 100, name: str = "general"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name="Test Server", id=1)
        self.topic = None

    def history(self, *, limit, before, after=None, oldest_first=None):
        async def _empty():
            return
            yield

        return _empty()


def _make_message(*, msg_id: int = 42, channel, content: str = "hello ambient", mentions=None):
    author = SimpleNamespace(id=7, display_name="Alice", name="Alice", bot=False)
    return SimpleNamespace(
        id=msg_id,
        content=content,
        mentions=list(mentions or []),
        attachments=[],
        reference=None,
        message_snapshots=None,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        author=author,
        type=discord_platform.discord.MessageType.default,
    )


def _thread_of(monkeypatch, parent, *, thread_id: int = 9101, name: str = "side-thread"):
    """A thread channel under *parent*, with ``discord.Thread`` shimmed for isinstance.

    The thread is NOT marked in the adapter's participation tracker: callers that
    want a joined thread mark ``adapter._threads`` themselves.
    """

    class ThreadShim:
        pass

    monkeypatch.setattr(discord_platform.discord, "Thread", ThreadShim)
    thread = ThreadShim()
    thread.id = thread_id
    thread.name = name
    thread.parent = parent
    thread.parent_id = parent.id
    thread.guild = parent.guild
    return thread


@pytest.fixture
def adapter(monkeypatch):
    for var in (
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_IGNORE_NO_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")

    config = PlatformConfig(enabled=True, token="***")
    a = DiscordAdapter(config)
    bot_user = SimpleNamespace(id=999, display_name="Hermes", name="Hermes", bot=True)
    a._client = SimpleNamespace(user=bot_user)
    a._text_batch_delay_seconds = 0
    a._ready_event.set()
    a._handle_message = AsyncMock(return_value=True)
    a.send = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    return a


def _init_gate(adapter, gate_dict):
    adapter.config.response_gate = ResponseGateConfig.from_dict(gate_dict)
    adapter._response_gate_credential = "fake-test-credential"
    adapter._response_gate_init()


def _mock_decide(adapter, decision: GateDecision):
    client = MagicMock()
    client.decide = AsyncMock(return_value=decision)
    client.threshold = adapter._response_gate.config.threshold
    adapter._response_gate.client = client
    return client


@pytest.mark.asyncio
async def test_echo_channel_score_at_or_above_threshold(adapter):
    channel = _TextChannel()
    _init_gate(
        adapter,
        {"echo_channels": ["100"], "mode": "enforce", "threshold": 0.8},
    )
    client = _mock_decide(
        adapter,
        GateDecision(allowed=True, mode="enforce", score=0.96, latency_ms=12.0),
    )
    msg = _make_message(channel=channel)

    result = await adapter._dispatch_discord_message(msg)

    assert result is False
    adapter.send.assert_awaited_once()
    assert "0.96" in adapter.send.await_args.args[1]
    adapter._handle_message.assert_not_awaited()
    client.decide.assert_awaited_once()


@pytest.mark.asyncio
async def test_echo_channel_score_below_threshold(adapter):
    channel = _TextChannel()
    _init_gate(
        adapter,
        {"echo_channels": ["100"], "mode": "shadow", "threshold": 0.8},
    )
    client = _mock_decide(
        adapter,
        GateDecision(
            allowed=False, mode="shadow", score=0.31, latency_ms=8.0, reason="below_threshold",
        ),
    )
    msg = _make_message(channel=channel)

    result = await adapter._dispatch_discord_message(msg)

    assert result is False
    adapter.send.assert_awaited_once()
    body = adapter.send.await_args.args[1]
    assert "0.31" in body
    assert "(below 0.8)" in body
    adapter._handle_message.assert_not_awaited()
    client.decide.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_echo_enforce_deny_no_echo_send(adapter):
    channel = _TextChannel()
    _init_gate(
        adapter,
        {"channels": ["100"], "mode": "enforce", "threshold": 0.8},
    )
    client = _mock_decide(
        adapter,
        GateDecision(
            allowed=False, mode="enforce", score=0.31, latency_ms=5.0, reason="below_threshold",
        ),
    )
    msg = _make_message(channel=channel)

    result = await adapter._dispatch_discord_message(msg)

    assert result is False
    adapter.send.assert_not_awaited()
    adapter._handle_message.assert_not_awaited()
    client.decide.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_echo_shadow_still_dispatches_when_admitted(adapter):
    channel = _TextChannel()
    _init_gate(
        adapter,
        {"channels": ["100"], "mode": "shadow", "threshold": 0.8},
    )
    client = _mock_decide(
        adapter,
        GateDecision(allowed=True, mode="shadow", score=0.95, latency_ms=3.0),
    )
    msg = _make_message(channel=channel)

    result = await adapter._dispatch_discord_message(msg)

    assert result is True
    adapter.send.assert_not_awaited()
    adapter._handle_message.assert_awaited_once()
    client.decide.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_mention_in_echo_channel_bypasses_gate(adapter):
    channel = _TextChannel()
    _init_gate(
        adapter,
        {"echo_channels": ["100"], "mode": "enforce"},
    )
    client = MagicMock()
    client.decide = AsyncMock()
    adapter._response_gate.client = client
    bot_user = adapter._client.user
    msg = _make_message(channel=channel, content="hey", mentions=[bot_user])

    result = await adapter._dispatch_discord_message(msg)

    assert result is True
    adapter.send.assert_not_awaited()
    client.decide.assert_not_awaited()
    adapter._handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_echo_channel_judge_error(adapter):
    channel = _TextChannel()
    _init_gate(
        adapter,
        {"echo_channels": ["100"], "mode": "enforce"},
    )
    client = MagicMock()
    client.decide = AsyncMock(side_effect=ResponseGateError("timeout", "timed out"))
    adapter._response_gate.client = client
    msg = _make_message(channel=channel)

    result = await adapter._dispatch_discord_message(msg)

    assert result is False
    adapter.send.assert_awaited_once()
    assert "gate error (timeout)" in adapter.send.await_args.args[1]
    adapter._handle_message.assert_not_awaited()


def test_response_gate_config_echo_channels_validation():
    cfg = ResponseGateConfig.from_dict(
        {
            "echo_channels": [100, "100", "general"],
            "channels": ["general"],
        }
    )
    assert cfg.echo_channels == ("100", "general")
    assert cfg.channels == ("general",)
    assert cfg.enabled is True

    with pytest.raises(ValueError, match="\\*"):
        ResponseGateConfig.from_dict({"echo_channels": ["*"]})


@pytest.mark.asyncio
async def test_double_echo_guard_live_and_recovered(adapter):
    channel = _TextChannel()
    _init_gate(
        adapter,
        {"echo_channels": ["100"], "mode": "enforce"},
    )
    client = _mock_decide(
        adapter,
        GateDecision(allowed=True, mode="enforce", score=0.96, latency_ms=1.0),
    )
    msg = _make_message(msg_id=9001, channel=channel)

    first = await adapter._dispatch_discord_message(msg)
    second = await adapter._dispatch_recovered_message(msg)

    assert first is False
    assert second is False
    assert adapter.send.await_count == 1
    assert client.decide.await_count == 1


class TestResponseGateThreadsRequireMembershipConfig:
    """Strict parse of ``threads_require_membership`` (mirrors include_threads)."""

    def test_omitted_defaults_true(self):
        assert ResponseGateConfig.from_dict({"channels": ["100"]}).threads_require_membership is True
        assert ResponseGateConfig.from_dict(None).threads_require_membership is True

    def test_explicit_true_and_false(self):
        assert ResponseGateConfig.from_dict({
            "channels": ["100"], "threads_require_membership": True,
        }).threads_require_membership is True
        assert ResponseGateConfig.from_dict({
            "channels": ["100"], "threads_require_membership": False,
        }).threads_require_membership is False

    def test_garbage_string_refuses_to_load(self):
        with pytest.raises(ValueError, match="threads_require_membership"):
            ResponseGateConfig.from_dict({
                "channels": ["100"], "threads_require_membership": "maybe",
            })
        with pytest.raises(ValueError, match="threads_require_membership"):
            ResponseGateConfig.from_dict({
                "channels": ["100"], "threads_require_membership": 3,
            })

    def test_to_dict_omits_true_includes_false(self):
        on_by_default = ResponseGateConfig.from_dict({"channels": ["100"]})
        assert "threads_require_membership" not in on_by_default.to_dict()
        opted_out = ResponseGateConfig.from_dict({
            "channels": ["100"], "threads_require_membership": False,
        })
        assert opted_out.to_dict()["threads_require_membership"] is False


class TestResponseGateThreadMembership:
    """Default-on thread membership: an unjoined thread of a gated parent stays silent.

    In a thread the gate consults only after this bot has participated there (the
    same persistent tracker a live dispatch marks), so ambient thread chatter never
    reaches the judge. Explicit triggers keep their bypass: a ping in an unjoined
    thread still wakes the bot through the mention path, which never consults. The
    parent channel is never membership-gated, and ``false`` restores legacy
    judge-every-in-scope-thread listening.
    """

    GATE = {"channels": ["100"], "mode": "enforce", "threshold": 0.8}

    @pytest.mark.asyncio
    async def test_ambient_unjoined_thread_never_consults(self, adapter, monkeypatch):
        thread = _thread_of(monkeypatch, _TextChannel())
        _init_gate(adapter, self.GATE)
        client = _mock_decide(
            adapter,
            GateDecision(allowed=True, mode="enforce", score=0.96, latency_ms=1.0),
        )
        msg = _make_message(msg_id=9110, channel=thread)
        assert str(thread.id) not in adapter._threads  # unjoined, as the default assumes

        assert adapter._response_gate_consults(msg) is False
        assert adapter._response_gate_candidate(msg, admitted=True) is None
        await adapter._dispatch_discord_message(msg)

        client.decide.assert_not_awaited()  # no judge call, so no HTTP and no echo either

    @pytest.mark.asyncio
    async def test_mention_in_unjoined_thread_still_bypasses(self, adapter, monkeypatch):
        thread = _thread_of(monkeypatch, _TextChannel())
        _init_gate(adapter, {"channels": ["100"], "mode": "enforce"})
        client = MagicMock()
        client.decide = AsyncMock()
        adapter._response_gate.client = client
        msg = _make_message(
            msg_id=9111, channel=thread, content="hey", mentions=[adapter._client.user],
        )

        assert adapter._response_gate_bypass(msg) is True
        assert adapter._response_gate_candidate(msg, admitted=True) is None
        result = await adapter._dispatch_discord_message(msg)

        assert result is True  # the ping woke the bot through the unchanged mention path
        client.decide.assert_not_awaited()
        adapter._handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_after_membership_mark_ambient_consults(self, adapter, monkeypatch):
        thread = _thread_of(monkeypatch, _TextChannel())
        _init_gate(adapter, self.GATE)
        client = _mock_decide(
            adapter,
            GateDecision(allowed=True, mode="enforce", score=0.96, latency_ms=2.0),
        )
        msg = _make_message(msg_id=9112, channel=thread)
        assert adapter._response_gate_consults(msg) is False
        adapter._threads.mark(str(thread.id))  # what a live dispatch in the thread does

        assert adapter._response_gate_consults(msg) is True
        assert adapter._response_gate_candidate(msg, admitted=True) is not None
        result = await adapter._dispatch_discord_message(msg)

        assert result is True
        client.decide.assert_awaited_once()
        adapter._handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parent_channel_ambient_consults_without_mark(self, adapter, monkeypatch):
        parent = _TextChannel()
        _thread_of(monkeypatch, parent)  # Thread shim active; the parent is not a thread
        _init_gate(adapter, self.GATE)
        client = _mock_decide(
            adapter,
            GateDecision(allowed=True, mode="enforce", score=0.96, latency_ms=1.0),
        )
        msg = _make_message(msg_id=9113, channel=parent)

        assert adapter._response_gate_consults(msg) is True
        result = await adapter._dispatch_discord_message(msg)

        assert result is True
        client.decide.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flag_false_restores_legacy_thread_listening(self, adapter, monkeypatch):
        thread = _thread_of(monkeypatch, _TextChannel())
        _init_gate(adapter, {**self.GATE, "threads_require_membership": False})
        client = _mock_decide(
            adapter,
            GateDecision(allowed=True, mode="enforce", score=0.96, latency_ms=1.0),
        )
        msg = _make_message(msg_id=9114, channel=thread)
        assert str(thread.id) not in adapter._threads

        assert adapter._response_gate_consults(msg) is True
        await adapter._dispatch_discord_message(msg)

        client.decide.assert_awaited_once()
