"""Tests for Discord clarify prompts rendered as numbered plain text.

Discord component views expire with their interaction token
(``approvals.discord_prompt_timeout``, default 300s, hard-capped by Discord's
15-minute expiry), which used to leave a dead button row on every prompt the
user didn't answer in time. ``send_clarify`` therefore attaches no
``discord.ui.View`` at all — the options render as a numbered list in the
message content (mirrored into the embed field) and the entry is flipped into
text-capture mode via ``mark_awaiting_text`` so the gateway intercept resolves
the next reply: a number, the option text, or free text.
"""

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Repo root importable
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# Triggers the shared discord mock from tests/gateway/conftest.py before
# importing the production module.
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(*, allowed_users=None, allowed_roles=None):
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set(allowed_users or [])
    adapter._allowed_role_ids = set(allowed_roles or [])
    return adapter


def _capture_channel(adapter, *, message_id=123456):
    """Wire a mock channel and return the kwargs its ``send`` was called with."""
    channel = MagicMock()
    sent_msg = MagicMock()
    sent_msg.id = message_id
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)
    adapter._client.fetch_channel = AsyncMock(return_value=channel)
    return channel


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


# ===========================================================================
# DiscordAdapter.send_clarify — no view, ever
# ===========================================================================

class TestDiscordSendClarifyPlaintext:
    def setup_method(self):
        _clear_clarify_state()

    def teardown_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_choice_sends_no_view(self):
        from tools import clarify_gateway as cm
        cm.register("cidM", "sk-M", "Pick a color", ["red", "green", "blue"])

        adapter = _make_adapter(allowed_users={"42"})
        channel = _capture_channel(adapter)

        result = await adapter.send_clarify(
            chat_id="9001",
            question="Pick a color",
            choices=["red", "green", "blue"],
            clarify_id="cidM",
            session_key="sk-M",
        )

        assert result.success is True
        assert result.message_id == "123456"
        channel.send.assert_called_once()
        kwargs = channel.send.call_args.kwargs
        # No buttons, no component view — plain text + embed only.
        assert "view" not in kwargs
        assert kwargs.get("view") is None
        assert "embed" in kwargs

    @pytest.mark.asyncio
    async def test_open_ended_sends_no_view(self):
        adapter = _make_adapter()
        channel = _capture_channel(adapter, message_id=222)

        result = await adapter.send_clarify(
            chat_id="9001",
            question="What is your name?",
            choices=None,
            clarify_id="cidOE",
            session_key="sk-OE",
        )

        assert result.success is True
        channel.send.assert_called_once()
        kwargs = channel.send.call_args.kwargs
        assert "view" not in kwargs
        assert kwargs.get("view") is None
        assert "embed" in kwargs


# ===========================================================================
# Numbered list rendering
# ===========================================================================

class TestNumberedListRendering:
    def setup_method(self):
        _clear_clarify_state()

    def teardown_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_numbered_choices_in_content(self):
        from tools import clarify_gateway as cm
        cm.register("cidN", "sk-N", "Pick", ["red", "green", "blue"])

        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick a color",
            choices=["red", "green", "blue"],
            clarify_id="cidN",
            session_key="sk-N",
        )
        content = channel.send.call_args.kwargs["content"]
        for expected in ("1. red", "2. green", "3. blue"):
            assert expected in content, f"{expected!r} missing from {content!r}"
        assert "Hermes needs your input" in content
        assert "Pick a color" in content
        assert "Reply with the number, the option text, or your own answer." in content

    @pytest.mark.asyncio
    async def test_numbered_choices_in_embed_field(self):
        """Choices render numbered in plain content, each exactly once.

        The numbered list is mirrored into an embed field as progressive
        enhancement, but the user-visible contract — the plain ``content``
        that every client shows — is what carries the assertions.
        """
        from tools import clarify_gateway as cm
        cm.register("cidF", "sk-F", "Pick", ["red", "green"])

        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick a color",
            choices=["red", "green"],
            clarify_id="cidF",
            session_key="sk-F",
        )
        kwargs = channel.send.call_args.kwargs
        # The embed mirror is still attached, just not asserted field-by-field.
        assert "embed" in kwargs
        content = kwargs["content"]
        assert "1. red" in content
        assert "2. green" in content
        # Two choices → nothing else inherits a number, no duplicates.
        assert "3. " not in content
        assert content.count("red") == 1

    @pytest.mark.asyncio
    async def test_open_ended_content_has_question_and_reply_hint(self):
        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="What is your name?",
            choices=None,
            clarify_id="cidO",
            session_key="sk-O",
        )
        kwargs = channel.send.call_args.kwargs
        assert "What is your name?" in kwargs["content"]
        assert "Reply in this channel with your answer." in kwargs["content"]
        assert "embed" in kwargs

    @pytest.mark.asyncio
    async def test_huge_choice_list_clips_embed_field_not_content(self):
        """Huge lists stay inside Discord's limits, with no view attached.

        The embed field caps at 1024 chars; the plain content caps at
        MAX_MESSAGE_LENGTH and carries as many numbered options as that
        budget allows (2000 chars ≈ the first ~35 of 60 options here).
        """
        from tools import clarify_gateway as cm
        big = [f"option-{i}-" + "x" * 40 for i in range(60)]
        cm.register("cidBIG", "sk-BIG", "Pick", big)

        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick",
            choices=big,
            clarify_id="cidBIG",
            session_key="sk-BIG",
        )
        kwargs = channel.send.call_args.kwargs
        assert "view" not in kwargs
        assert kwargs.get("view") is None
        content = kwargs["content"]
        assert len(content) <= adapter.MAX_MESSAGE_LENGTH
        rendered = re.findall(r"^\d+\. option-\d+-", content, re.MULTILINE)
        # Clipping drops whole trailing entries, so the numbering that does
        # render stays sequential from 1 with no gaps.
        numbers = [int(line.split(".", 1)[0]) for line in rendered]
        assert numbers == list(range(1, len(numbers) + 1))
        # As many options as the MAX_MESSAGE_LENGTH budget allows — the
        # 2000-char cap fits roughly 35 of these ~54-char lines.
        assert len(rendered) >= 30
        assert len(rendered) < len(big)
        assert "... [truncated]" in content

    @pytest.mark.asyncio
    async def test_multi_select_hint_differs(self):
        from tools import clarify_gateway as cm
        cm.register(
            "cidMS", "sk-MS", "Pick any", ["a", "b"],
            multi_select=True,
        )

        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick any",
            choices=["a", "b"],
            clarify_id="cidMS",
            session_key="sk-MS",
        )
        content = channel.send.call_args.kwargs["content"]
        assert "Multiple selections allowed" in content
        assert "1, 3" in content


# ===========================================================================
# Text-capture: mark_awaiting_text
# ===========================================================================

class TestMarkAwaitingText:
    def setup_method(self):
        _clear_clarify_state()

    def teardown_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_choice_marks_entry_awaiting_text(self):
        from tools import clarify_gateway as cm
        entry = cm.register("cidT", "sk-T", "Pick", ["x", "y"])
        assert entry.awaiting_text is False  # pre-condition: button-mode entry

        adapter = _make_adapter()
        _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick",
            choices=["x", "y"],
            clarify_id="cidT",
            session_key="sk-T",
        )

        with cm._lock:
            after = cm._entries.get("cidT")
        assert after is not None
        assert after.awaiting_text is True

    @pytest.mark.asyncio
    async def test_open_ended_stays_awaiting_text(self):
        from tools import clarify_gateway as cm
        cm.register("cidO2", "sk-O2", "Name?", None)

        adapter = _make_adapter()
        _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Name?",
            choices=None,
            clarify_id="cidO2",
            session_key="sk-O2",
        )

        with cm._lock:
            after = cm._entries.get("cidO2")
        assert after is not None
        assert after.awaiting_text is True

    @pytest.mark.asyncio
    async def test_awaiting_text_accepts_free_form_answer(self):
        """End-to-end contract: after the prompt, any reply resolves the wait."""
        from tools import clarify_gateway as cm
        cm.register("cidR", "sk-R", "Pick", ["red", "green"])
        adapter = _make_adapter()
        _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick",
            choices=["red", "green"],
            clarify_id="cidR",
            session_key="sk-R",
        )

        # The gateway intercept path used for typed replies.
        assert cm.attempt_text_response_for_session("sk-R", "2") == cm.TEXT_RESOLVED

    @pytest.mark.asyncio
    async def test_missing_entry_still_sends(self):
        """An unregistered clarify_id must not break the send."""
        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        result = await adapter.send_clarify(
            chat_id="9001",
            question="Pick",
            choices=["a"],
            clarify_id="cid-unknown",
            session_key="sk-unknown",
        )
        assert result.success is True
        assert "1. a" in channel.send.call_args.kwargs["content"]


# ===========================================================================
# Discord niceties that must survive the button removal
# ===========================================================================

class TestPlaintextNiceties:
    def setup_method(self):
        _clear_clarify_state()

    def teardown_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_thread_id_metadata_retargets_channel(self):
        from tools import clarify_gateway as cm
        cm.register("cidTH", "sk-TH", "Pick", ["a"])

        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick",
            choices=["a"],
            clarify_id="cidTH",
            session_key="sk-TH",
            metadata={"thread_id": "4242"},
        )
        adapter._client.get_channel.assert_called_once_with(4242)
        channel.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_mention_prepended_with_allowed_mentions(self):
        from tools import clarify_gateway as cm
        cm.register("cidMN", "sk-MN", "Pick", ["a"])

        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick",
            choices=["a"],
            clarify_id="cidMN",
            session_key="sk-MN",
            metadata={"mention_user_id": "111222333444555666"},
        )
        kwargs = channel.send.call_args.kwargs
        assert kwargs["content"].startswith("<@111222333444555666>\n")
        assert "1. a" in kwargs["content"]
        assert "allowed_mentions" in kwargs


# ===========================================================================
# Choice normalisation (dict flattening)
# ===========================================================================

class TestChoiceFlattening:
    def setup_method(self):
        _clear_clarify_state()

    def teardown_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_unwrap_does_not_pick_value_or_name_alone(self):
        # 'name' and 'value' are Discord-component-shaped fields that could
        # accidentally appear in dicts not intended as choices (e.g., a
        # developer-error in the gateway wiring). The renderer should not
        # surface them as list entries — only the well-known LLM tool-call
        # keys (label, description, text, title) should win.
        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="?",
            choices=[
                {"name": "only_name_here"},    # should be filtered out
                {"value": "only_value_here"},  # should be filtered out
                {"description": "real choice"},
            ],
            clarify_id="cidNV",
            session_key="sk-NV",
        )
        content = channel.send.call_args.kwargs["content"]
        assert "real choice" in content
        assert "only_name_here" not in content, f"name leaked: {content!r}"
        assert "only_value_here" not in content, f"value leaked: {content!r}"
        # Only the well-formed dict survived → renumbered from 1.
        assert "1. real choice" in content
        assert "2. " not in content

    @pytest.mark.asyncio
    async def test_dict_key_precedence(self):
        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="?",
            choices=[
                {"label": "L", "description": "D", "text": "T", "title": "Ti"},
                {"text": "T2"},
                ["nested", "tuple"],
            ],
            clarify_id="cidP",
            session_key="sk-P",
        )
        content = channel.send.call_args.kwargs["content"]
        assert "1. L" in content       # label wins
        assert "2. T2" in content      # text fallback
        assert "3. nested tuple" in content  # list/tuple joined

    @pytest.mark.asyncio
    async def test_no_24_choice_button_cap(self):
        """The old cap existed to fit Discord's 25-button row budget."""
        from tools import clarify_gateway as cm
        many = [f"choice-{i}" for i in range(30)]
        cm.register("cid30", "sk-30", "Pick", many)

        adapter = _make_adapter()
        channel = _capture_channel(adapter)

        await adapter.send_clarify(
            chat_id="9001",
            question="Pick",
            choices=many,
            clarify_id="cid30",
            session_key="sk-30",
        )
        content = channel.send.call_args.kwargs["content"]
        assert "1. choice-0" in content
        assert "30. choice-29" in content
