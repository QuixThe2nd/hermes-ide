"""Tests for Discord auto-thread 429 queueing (#20243 follow-up).

``_auto_create_thread()`` used to retry exactly twice with a 0.75s backoff —
hopeless against Discord 429 rate limits whose ``retry_after`` runs 30-590s,
so the user's message was dropped with a "could not create a thread" notice.

Fix: when both the direct and seed-message fallback thread creations are
rate-limited, the message is visibly queued (a ⏳ reaction), creation sleeps
for the reported ``retry_after`` (bounded in total by
``AUTO_THREAD_RATE_LIMIT_MAX_WAIT_SECONDS``), and the reaction is cleared
once the thread exists. Non-rate-limit failures keep the original
two-attempt contract.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig


# ---------------------------------------------------------------------------
# Discord mock setup
# The tests/gateway/conftest.py already installs a comprehensive discord
# mock at collection time (a no-op when the real library is installed).
# We import the adapter AFTER that is done.
# ---------------------------------------------------------------------------

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


WAITING_EMOJI = "⏳"


# ---------------------------------------------------------------------------
# Fake exceptions / objects
#
# The fakes duck-type discord.py's real exceptions (RateLimited renders as
# "Too many requests. Retry in X seconds.") so the tests behave identically
# whether the real library or the conftest mock is active.
# ---------------------------------------------------------------------------

class _RateLimited(Exception):
    """Mirrors discord.RateLimited: retry_after attr + canonical message."""

    def __init__(self, retry_after):
        super().__init__(f"Too many requests. Retry in {retry_after} seconds.")
        self.retry_after = retry_after


class _Http429(Exception):
    """Mirrors a 429 HTTPException carrying the wait in response headers."""

    def __init__(self, retry_after_header="12"):
        super().__init__("429 Too many requests (error code: 0)")
        self.response = SimpleNamespace(
            status=429,
            headers={"Retry-After": retry_after_header},
        )


class _Channel:
    """Fake Discord text channel whose seed-message thread creation fails."""

    def __init__(self, seed_create_thread_side_effect=None):
        self.id = 100
        self.name = "general"
        self.send = AsyncMock(return_value=SimpleNamespace(
            create_thread=AsyncMock(side_effect=seed_create_thread_side_effect),
        ))


def _make_message(channel, *, content="hello bot", create_thread_side_effect=None):
    """Fake user message with reaction/call-order tracking."""
    events = []

    async def _create_thread(**_kwargs):
        if isinstance(create_thread_side_effect, list):
            if not create_thread_side_effect:
                raise AssertionError("create_thread called more times than scripted")
            effect = create_thread_side_effect.pop(0)
        else:
            effect = create_thread_side_effect
        if isinstance(effect, BaseException):
            raise effect
        return effect

    async def _add_reaction(emoji):
        events.append(("add", emoji))

    async def _remove_reaction(emoji, user):
        events.append(("remove", emoji))

    return SimpleNamespace(
        id=42,
        content=content,
        channel=channel,
        author=SimpleNamespace(id=7, display_name="Alice", name="Alice", bot=False),
        create_thread=_create_thread,
        add_reaction=_add_reaction,
        remove_reaction=_remove_reaction,
        events=events,
    )


def _make_thread(thread_id=55555):
    return SimpleNamespace(id=thread_id, name="hello bot")


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.delenv("DISCORD_REACTIONS", raising=False)
    config = PlatformConfig(enabled=True, token="***")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    return a


# ---------------------------------------------------------------------------
# _extract_rate_limit_delay
# ---------------------------------------------------------------------------

class TestExtractRateLimitDelay:
    def test_rate_limited_instance_uses_retry_after_attribute(self):
        assert DiscordAdapter._extract_rate_limit_delay(_RateLimited(0.05)) == 0.05

    def test_regex_path_when_no_delay_attribute(self):
        exc = Exception("429 Too many requests. Retry in 7 seconds.")
        assert DiscordAdapter._extract_rate_limit_delay(exc) == 7.0

    def test_http_exception_uses_response_headers(self):
        assert DiscordAdapter._extract_rate_limit_delay(_Http429()) == 12.0

    def test_single_wait_clamped_to_budget_cap(self):
        assert DiscordAdapter._extract_rate_limit_delay(_RateLimited(5000.0)) == 900.0

    def test_non_rate_limit_returns_none(self):
        exc = Exception("Cannot connect to host discord.com:443")
        assert DiscordAdapter._extract_rate_limit_delay(exc) is None

    def test_unusable_retry_after_and_no_regex_match_returns_none(self):
        # "Retry in unknown seconds" has no parseable number anywhere.
        assert DiscordAdapter._extract_rate_limit_delay(_RateLimited("unknown")) is None


# ---------------------------------------------------------------------------
# _auto_create_thread rate-limit queueing
# ---------------------------------------------------------------------------

class TestAutoThreadRateLimitQueue:
    @pytest.mark.asyncio
    async def test_rate_limit_then_success_adds_and_removes_waiting_reaction(
        self, adapter, caplog
    ):
        """A 429 on the first attempt queues the message; the retry succeeds.

        The ⏳ reaction must be added while waiting and removed once the
        thread exists, with no failure warning — the message was processed,
        just delayed.
        """
        channel = _Channel(seed_create_thread_side_effect=_RateLimited(0.05))
        thread = _make_thread()
        message = _make_message(
            channel,
            create_thread_side_effect=[_RateLimited(0.05), thread],
        )

        with caplog.at_level(logging.WARNING):
            result = await adapter._auto_create_thread(message)

        assert result is thread
        assert message.events == [("add", WAITING_EMOJI), ("remove", WAITING_EMOJI)]
        assert "Auto-thread creation failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_budget_exhaustion_returns_none_with_warning(
        self, adapter, monkeypatch, caplog
    ):
        """Once the total wait budget is spent, creation fails like before."""
        monkeypatch.setattr(
            discord_platform, "AUTO_THREAD_RATE_LIMIT_MAX_WAIT_SECONDS", 0.02
        )
        channel = _Channel(seed_create_thread_side_effect=_RateLimited(0.05))
        message = _make_message(channel, create_thread_side_effect=_RateLimited(0.05))

        with caplog.at_level(logging.WARNING):
            result = await adapter._auto_create_thread(message)

        assert result is None
        assert "Auto-thread creation failed" in caplog.text
        # Two attempts: one wait consumed the budget, the second saw none left.
        assert message.events == [("add", WAITING_EMOJI)]

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_keeps_two_attempt_contract(
        self, adapter, caplog
    ):
        """Connect-style errors still get exactly two attempts, no reaction."""
        connect_error = Exception("Cannot connect to host discord.com:443")
        channel = _Channel(seed_create_thread_side_effect=connect_error)
        attempts = []

        async def _create_thread(**_kwargs):
            attempts.append(1)
            raise connect_error

        message = _make_message(channel)
        message.create_thread = _create_thread

        with caplog.at_level(logging.WARNING):
            result = await adapter._auto_create_thread(message)

        assert result is None
        assert len(attempts) == 2, "non-rate-limit failures must retry exactly once"
        assert message.events == [], "no queue reaction for non-rate-limit errors"
        assert "Auto-thread creation failed" in caplog.text

    @pytest.mark.asyncio
    async def test_success_on_first_try_never_touches_reactions(self, adapter, caplog):
        channel = _Channel()
        thread = _make_thread()
        message = _make_message(channel, create_thread_side_effect=thread)

        with caplog.at_level(logging.WARNING):
            result = await adapter._auto_create_thread(message)

        assert result is thread
        assert message.events == []
        channel.send.assert_not_awaited()
        assert "Auto-thread creation failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_probe_wait_when_later_rate_limit_exposes_no_delay(
        self, adapter, monkeypatch, caplog
    ):
        """A later 429 with no parseable delay falls back to a short probe.

        The probe wait is monkeypatched tiny so the test stays fast; without
        the probe path this scenario would burn the non-rate-limit contract
        and give up after two attempts.
        """
        monkeypatch.setattr(
            discord_platform, "_AUTO_THREAD_RATE_LIMIT_PROBE_SECONDS", 0.02
        )
        channel = _Channel(
            seed_create_thread_side_effect=[
                _RateLimited(0.05),
                _RateLimited("unknown"),
            ]
        )
        thread = _make_thread()
        message = _make_message(
            channel,
            create_thread_side_effect=[
                _RateLimited(0.05),
                _RateLimited("unknown"),
                thread,
            ],
        )

        with caplog.at_level(logging.WARNING):
            result = await adapter._auto_create_thread(message)

        assert result is thread
        assert message.events == [("add", WAITING_EMOJI), ("remove", WAITING_EMOJI)]
        assert "Auto-thread creation failed" not in caplog.text
