"""Tests for Discord auto-thread adoption of an already-existing thread.

When one message @mentions two Hermes bots, both adapters auto-thread the
mention via ``message.create_thread()``, but Discord allows exactly one
thread per message: the first bot wins, the second gets HTTP 400 JSON code
160004 ("A thread has already been created for this message"). The loser now
ADOPTS the existing thread (its id equals the source message id) instead of
seeding a second thread or dropping the request. The classification rule
lives in ``internal/autothread`` (Go); both suites pin the shared table
``internal/autothread/testdata/classification_cases.json``. Adopted threads
carry ``_hermes_auto_thread_adopted`` so this bot's semantic auto-title
rename never clobbers the creator's name.
"""

import importlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

# tests/gateway/conftest.py installs the discord mock at collection time
# (a no-op with the real library); the adapter imports AFTER that.

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
from gateway.run_topics import GatewayTopicThreadsMixin  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO / "internal" / "autothread"
_TABLE = json.loads(
    (_MODULE_DIR / "testdata" / "classification_cases.json").read_text()
)["cases"]

WAITING_EMOJI = "⏳"


# Fake exceptions — duck-typed mirrors of discord.py's errors. The adapter
# must key on status/code/text, never on the concrete class.

class _ThreadAlreadyExists(Exception):
    """Mirrors discord.HTTPException for JSON error code 160004."""

    def __init__(self):
        super().__init__(
            "A thread has already been created for this message (error code: 160004)")
        self.status = 400
        self.code = 160004


class _OtherHttp400(Exception):
    """A durable 400 that is NOT 160004 (e.g. invalid form body)."""

    def __init__(self):
        super().__init__("Invalid Form Body (error code: 50035)")
        self.status = 400
        self.code = 50035


class _RateLimited(Exception):
    """Mirrors discord.RateLimited: retry_after attr + canonical message."""

    def __init__(self, retry_after):
        super().__init__(f"Too many requests. Retry in {retry_after} seconds.")
        self.retry_after = retry_after


class _TextChannel:
    """Fake Discord text channel (not a DM, not a Thread class)."""

    def __init__(self, channel_id=100, seed_create_thread_side_effect=None):
        self.id = channel_id
        self.name = "general"
        self.guild = SimpleNamespace(name="Test Server", id=1)
        self.topic = None
        self.seed_message = SimpleNamespace(
            id=4242,
            create_thread=AsyncMock(side_effect=seed_create_thread_side_effect),
            delete=AsyncMock(),
        )
        self.send = AsyncMock(return_value=self.seed_message)


def _existing_thread(thread_id, name="other bot's thread", parent=None):
    """The thread the FIRST bot already created from the same message."""
    return SimpleNamespace(
        id=thread_id, name=name, parent=parent, parent_id=getattr(parent, "id", None),
        guild=getattr(parent, "guild", None), topic=None)


def _make_message(channel, *, msg_id=42, content="<@999> hello bots",
                  create_thread_side_effect=None, thread_marker=None):
    """Fake user message whose create_thread is scripted per attempt."""
    calls = []

    async def _create_thread(**_kwargs):
        calls.append(1)
        effect = create_thread_side_effect
        if isinstance(effect, list):
            if not effect:
                raise AssertionError("create_thread called more times than scripted")
            effect = effect.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    events = []

    async def _add_reaction(emoji):
        events.append(("add", emoji))

    async def _remove_reaction(emoji, user):
        events.append(("remove", emoji))

    message = SimpleNamespace(
        id=msg_id, content=content, mentions=[SimpleNamespace(id=999)],
        attachments=[], reference=None, created_at=datetime.now(timezone.utc),
        channel=channel,
        author=SimpleNamespace(id=7, display_name="Alice", name="Alice", bot=False),
        type=discord_platform.discord.MessageType.default,
        create_thread=_create_thread,
        add_reaction=_add_reaction, remove_reaction=_remove_reaction)
    if thread_marker is not None:
        message.thread = thread_marker
    return message, calls, events


@pytest.fixture(autouse=True)
def _hermetic_discord_gate_env(monkeypatch):
    """Clear the operator's live gate env so fixture channels are admitted."""
    for var in (
        "DISCORD_REQUIRE_MENTION", "DISCORD_AUTO_THREAD",
        "DISCORD_NO_THREAD_CHANNELS", "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS", "DISCORD_IGNORED_CHANNELS",
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOWED_GUILDS",
        "DISCORD_ALLOWED_ROLES", "DISCORD_ALLOW_ALL_USERS",
        "DISCORD_REACTIONS", "DISCORD_HISTORY_BACKFILL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="session")
def go_autothread(tmp_path_factory):
    """Build cmd/autothread once per session; skip when no Go toolchain."""
    if shutil.which("go") is None:
        pytest.skip("Go toolchain unavailable")
    binary = tmp_path_factory.mktemp("autothread") / "autothread"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/autothread"],
        cwd=_MODULE_DIR, capture_output=True, text=True, timeout=300)
    if build.returncode != 0:
        pytest.fail(f"go build cmd/autothread failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture
def bridge(go_autothread, monkeypatch):
    """The real bridge pinned to the session-built Go binary."""
    module = importlib.import_module("gateway.auto_thread_errors")
    monkeypatch.setattr(module, "_bridge_binary", go_autothread)
    monkeypatch.setattr(module, "_bridge_failed_at", None)
    monkeypatch.setattr(module, "_warned_unavailable", False)
    return module


@pytest.fixture
def adapter(bridge):
    config = PlatformConfig(enabled=True, token="***")
    a = DiscordAdapter(config)
    a._text_batch_delay_seconds = 0  # disable batching: dispatch is synchronous
    a.handle_message = AsyncMock()
    return a


def _client_with(cached, *, fetch_return=None, fetch_side_effect=None):
    """Fake Discord client: cache hit/miss plus a scripted fetch_channel."""
    client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    client.get_channel = lambda channel_id: cached
    client.fetch_channel = AsyncMock(
        side_effect=fetch_side_effect, return_value=fetch_return)
    return client


class TestSharedClassificationTable:
    """The Python bridge and the Go package pin the same case table."""

    def test_python_bridge_agrees_with_go_table(self, bridge):
        for case in _TABLE:
            decision = bridge.classify(case["status"], case["code"], case["message"])
            assert decision == case["decision"], (
                f"case {case['name']!r}: bridge returned {decision!r}, "
                f"table says {case['decision']!r}")

    def test_error_field_extraction_duck_typed(self, bridge):
        """status/code come off the exception; missing attrs degrade to 0."""
        assert bridge.classify_thread_create_error(_ThreadAlreadyExists()) == "adopt"
        assert bridge.classify_thread_create_error(_OtherHttp400()) == "fail"
        assert bridge.classify_thread_create_error(Exception("boom")) == "fail"


class TestAutoThreadAdoption:
    """_auto_create_thread: 160004 adopts; every other failure is unchanged."""

    @pytest.mark.asyncio
    async def test_adopts_existing_thread_from_client_cache(self, adapter, caplog):
        """160004 → the existing thread (id == message.id) is returned; no
        seed placeholder is posted and create_thread is not retried."""
        channel = _TextChannel()
        existing = _existing_thread(42, parent=channel)
        adapter._client = _client_with(existing)
        message, calls, _ = _make_message(
            channel, create_thread_side_effect=_ThreadAlreadyExists())

        with caplog.at_level(logging.INFO):
            result = await adapter._auto_create_thread(message)

        assert result is existing
        assert result.id == message.id
        assert len(calls) == 1, "the losing create_thread must not be retried"
        channel.send.assert_not_called()  # no seed placeholder on the adopt path
        channel.seed_message.create_thread.assert_not_called()
        assert "[Discord] Adopted existing thread 42 for message 42 " \
               "(created by another client)" in caplog.text
        assert getattr(result, "_hermes_auto_thread_adopted", False) is True
        assert getattr(result, "_hermes_auto_thread_initial_name", None) is None

    @pytest.mark.asyncio
    async def test_get_channel_miss_falls_back_to_fetch_channel(self, adapter):
        """A cache miss resolves the thread via fetch_channel (same id)."""
        channel = _TextChannel()
        existing = _existing_thread(42, parent=channel)
        adapter._client = _client_with(None, fetch_return=existing)
        message, _, _ = _make_message(
            channel, create_thread_side_effect=_ThreadAlreadyExists())

        result = await adapter._auto_create_thread(message)

        assert result is existing
        adapter._client.fetch_channel.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_message_thread_attribute_short_circuits_client(self, adapter):
        """When the event already carries message.thread, no lookup runs."""
        channel = _TextChannel()
        existing = _existing_thread(42, parent=channel)
        adapter._client = _client_with(None)
        message, _, _ = _make_message(
            channel, create_thread_side_effect=_ThreadAlreadyExists(),
            thread_marker=existing)

        result = await adapter._auto_create_thread(message)

        assert result is existing
        adapter._client.fetch_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_failure_retries_briefly_then_fails(self, adapter):
        """A transient fetch failure gets one retry; a persistent failure
        takes the existing failure path (warning + None) WITHOUT seeding a
        second thread."""
        channel = _TextChannel()
        adapter._client = _client_with(
            None, fetch_side_effect=Exception("Cannot connect to host discord.com:443"))
        message, calls, _ = _make_message(
            channel, create_thread_side_effect=_ThreadAlreadyExists())

        result = await adapter._auto_create_thread(message)  # one real 0.5s retry sleep

        assert result is None
        assert adapter._client.fetch_channel.await_count == 2  # one retry, then give up
        assert len(calls) == 1
        channel.send.assert_not_called()  # no seed thread off an adoptable message

    @pytest.mark.asyncio
    async def test_helper_unavailable_keeps_historical_seed_fallback(
        self, adapter, bridge, monkeypatch
    ):
        """Without the Go helper a 160004 degrades to the OLD behavior (seed
        placeholder + second thread): adoption is additive, never a regression."""
        monkeypatch.setattr(bridge, "_bridge_binary", None)
        monkeypatch.setattr(bridge, "_bridge_failed_at", time.monotonic())
        channel = _TextChannel()
        seed_thread = _existing_thread(4242, parent=channel)
        channel.seed_message.create_thread = AsyncMock(return_value=seed_thread)
        adapter._client = _client_with(None)
        message, _, _ = _make_message(
            channel, create_thread_side_effect=_ThreadAlreadyExists())

        result = await adapter._auto_create_thread(message)

        assert result is seed_thread
        channel.send.assert_awaited_once()  # historical behavior preserved
        assert getattr(result, "_hermes_auto_thread_adopted", False) is False

    @pytest.mark.asyncio
    async def test_non_160004_400_still_uses_seed_fallback(self, adapter):
        """Any other 400 keeps the seed-fallback path byte-for-byte."""
        channel = _TextChannel()
        seed_thread = _existing_thread(4242, parent=channel)
        channel.seed_message.create_thread = AsyncMock(return_value=seed_thread)
        adapter._client = _client_with(None)
        message, calls, _ = _make_message(
            channel, create_thread_side_effect=_OtherHttp400())

        result = await adapter._auto_create_thread(message)

        assert result is seed_thread
        assert len(calls) == 1
        channel.send.assert_awaited_once()
        channel.seed_message.delete.assert_awaited_once()  # placeholder cleaned up
        assert getattr(result, "_hermes_auto_thread_adopted", False) is False
        assert getattr(result, "_hermes_auto_thread_initial_name", None) is not None

    @pytest.mark.asyncio
    async def test_429_still_waits_out_rate_limit(self, adapter):
        """A 429 queues (⏳ reaction), waits retry_after, then creates the
        thread — adoption must not intercept rate limiting."""
        channel = _TextChannel(seed_create_thread_side_effect=_RateLimited(0.05))
        created = _existing_thread(55555, parent=channel)
        adapter._client = _client_with(None)
        message, _, events = _make_message(
            channel, create_thread_side_effect=[_RateLimited(0.05), created])

        result = await adapter._auto_create_thread(message)

        assert result is created
        assert events == [("add", WAITING_EMOJI), ("remove", WAITING_EMOJI)]
        assert getattr(result, "_hermes_auto_thread_adopted", False) is False


class _RenameProbe(GatewayTopicThreadsMixin):
    """GatewayTopicThreadsMixin with the scheduling seam recorded."""

    def __init__(self):
        self.scheduled = []

    def _schedule_rename_from_title_thread(self, source, make_coro, label):
        self.scheduled.append(label)


class TestAdoptedThreadRouting:
    """_handle_message: the adopted thread routes like a created one."""

    async def _run_adopted_mention(self, adapter):
        channel = _TextChannel(channel_id=100)
        existing = _existing_thread(42, parent=channel)
        adapter._client = _client_with(existing)
        message, _, _ = _make_message(
            channel, create_thread_side_effect=_ThreadAlreadyExists())
        handled = await adapter._handle_message(message)
        return channel, existing, handled

    @pytest.mark.asyncio
    async def test_handle_message_routes_into_adopted_thread(self, adapter):
        """160004 routes the session into the shared thread: marked in
        _threads, dedup pre-seeded, no "could not create a thread" notice."""
        channel, existing, handled = await self._run_adopted_mention(adapter)

        assert handled is True
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.source.chat_type == "thread"
        assert event.source.chat_id == "42"
        assert event.source.thread_id == "42"
        assert event.source.auto_thread_created is True
        assert "42" in adapter._threads  # ambient follow-ups are admitted
        # The thread-starter duplicate event is pre-seeded in dedup.
        assert adapter._dedup.is_duplicate("42") is True
        channel.send.assert_not_called()  # no failure notice, no seed placeholder
        # The rename-lane marker stays unset: the other bot named the thread.
        assert event.source.auto_thread_initial_name is None

    @pytest.mark.asyncio
    async def test_adopted_thread_is_not_semantic_renamed(self, adapter):
        """The real gateway rename lane never schedules a rename for an
        adopted thread — and still schedules one for a thread we created."""
        _, _, handled = await self._run_adopted_mention(adapter)
        assert handled is True
        adopted_source = adapter.handle_message.await_args.args[0].source

        probe = _RenameProbe()
        probe._schedule_discord_semantic_thread_rename(
            adopted_source, "session-1", "A Semantic Title")
        assert probe.scheduled == [], "adopted threads must not be renamed"

        # Positive control: the same source WITH an initial name (a thread
        # this bot created) does enter the rename lane.
        own_source = replace(adopted_source, auto_thread_initial_name="hello bots")
        probe._schedule_discord_semantic_thread_rename(
            own_source, "session-1", "A Semantic Title")
        assert len(probe.scheduled) == 1
