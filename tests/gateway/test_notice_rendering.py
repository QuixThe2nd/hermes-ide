"""Unit tests for messaging-gateway credit-notice rendering.

Covers render_notice_line — the pure helper that turns an AgentNotice into the
single plaintext line pushed standalone over a messaging platform (no status
bar, unlike the TUI). Behavior contracts, not data snapshots.
"""
from gateway.run import render_notice_line


class AgentNotice:
    """Attribute-shaped stand-in for the structured notice payload render_notice_line consumes."""

    def __init__(self, text: str = "", level: str = "info", key: str = None):
        self.text = text
        self.level = level
        self.key = key


class TestRenderNoticeLine:
    """render_notice_line emits the notice text VERBATIM.

    The notice policy already bakes the level glyph (⚠ / • / ✕ / ✓) into the
    text, and the TUI + CLI REPL render it as-is — so messaging must NOT add a
    second glyph, which would double it ("⚠ ⚠ Credits 90% used", "⛔ ✕ Credit
    access paused").
    """

    def test_returns_text_verbatim_with_its_baked_glyph(self):
        assert (
            render_notice_line(AgentNotice(text="⚠ Credits 90% used · $20.00 cap", level="warn"))
            == "⚠ Credits 90% used · $20.00 cap"
        )
        assert (
            render_notice_line(AgentNotice(text="• Grant spent · $5.00 top-up left", level="info"))
            == "• Grant spent · $5.00 top-up left"
        )
        assert (
            render_notice_line(
                AgentNotice(text="✕ Credit access paused · run /credits to top up", level="error")
            )
            == "✕ Credit access paused · run /credits to top up"
        )

    def test_does_not_prepend_a_second_glyph(self):
        # Regression: the text already carries its glyph; the level must not add
        # another (the bug produced "⚠ ⚠ …" / "⛔ ✕ …").
        line = render_notice_line(AgentNotice(text="⚠ Credits 90% used", level="warn"))
        assert line == "⚠ Credits 90% used"
        assert "⚠ ⚠" not in line


# ── Delivery seam: a rendered notice line goes out via _deliver_platform_notice ──

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_source(platform_value="telegram", chat_id="555", user_id="u1"):
    src = MagicMock()
    plat = MagicMock()
    plat.value = platform_value
    src.platform = plat
    src.chat_id = chat_id
    src.user_id = user_id
    # Real SessionSource.profile is None (single-profile) or a str; a MagicMock
    # auto-attribute would read as a truthy "stamped profile" and trip the
    # fail-closed path in _delivery_adapter_for (see AGENTS.md pitfall #17).
    src.profile = None
    return src


def _make_runner_with_adapter(source, adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {source.platform: adapter}
    runner.config = MagicMock()
    runner.config.get_notice_delivery = MagicMock(return_value="public")
    runner._thread_metadata_for_source = MagicMock(return_value={"thread": "t"})
    return runner


class TestDeliverNoticeLine:
    """The seam between render_notice_line and the platform adapter.

    Proves a rendered credit-notice line reaches adapter.send (public) /
    send_private_notice (private) through the shared _deliver_platform_notice
    rail — the path the gateway notice_callback schedules onto the loop.
    """

    @pytest.mark.asyncio
    async def test_public_delivery_sends_rendered_line(self):
        source = _make_source()
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True))
        runner = _make_runner_with_adapter(source, adapter)

        line = render_notice_line(
            AgentNotice(text="⚠ Credits 90% used · $20.00 cap", level="warn")
        )
        await runner._deliver_platform_notice(source, line)

        adapter.send.assert_awaited_once()
        args, kwargs = adapter.send.call_args
        assert args[0] == "555"
        # Delivered verbatim — the policy's single glyph, not a doubled one.
        assert args[1] == "⚠ Credits 90% used · $20.00 cap"


