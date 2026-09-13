"""Synthesized auto-resume turns must not claim a NEW user message exists.

Observed live (2026-09-13): a Discord session parked by a cooperative
gateway restart was auto-resumed at startup with NO user message, yet the
model-visible note read "Address the user's NEW message below FIRST ...".
The startup resume event is synthesized with ``text=""``
(``_schedule_resume_pending_sessions``), but on a shared multi-user session
(Discord thread, or any group/channel with per-user sessions off) the
inbound prep pipeline prefixed the empty text with the session owner's
``[name] `` attribution — a truthy message, so the resume note builder took
its has-message branch and claimed a NEW message that did not exist.

These tests drive the REAL pipeline (inbound prep → the resume-pending seam
call ``_prepare_resume_pending_message``) so the regression guards the
leak's actual data flow, not just the note builder's branches.
"""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner, _prepare_resume_pending_message
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    return runner


def _shared_discord_thread_source() -> SessionSource:
    """A source whose session is shared across participants (threads are
    shared by default), so sender attribution applies on the inbound path."""
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="C123",
        chat_name="dev-channel",
        chat_type="channel",
        user_id="U1",
        user_name="Quix",
        thread_id="171.000",
    )


def _synth_resume_event(source: SessionSource) -> MessageEvent:
    """The startup auto-resume event exactly as
    ``_schedule_resume_pending_sessions`` synthesizes it."""
    return MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True,
    )


async def _prepared_turn_message(runner: GatewayRunner, event: MessageEvent) -> str:
    """Run the real inbound prep for *event* (the write the resume-pending
    seam reads as ``ctx.message``) and hand it to the seam's note call."""
    prepared = await runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[],
    )
    note, _persisted = _prepare_resume_pending_message(
        "cooperative_restart", prepared,
    )
    return note


@pytest.mark.asyncio
async def test_synthesized_auto_resume_event_gets_parked_wording():
    """The leak: empty synthesized event + shared-session attribution must
    still render the parked/no-new-message wording."""
    runner = _make_runner()
    source = _shared_discord_thread_source()

    note = await _prepared_turn_message(runner, _synth_resume_event(source))

    assert "parked itself" in note
    assert "NEW message below" not in note
    # The cooperative tail guidance replaces the has-message tail verbatim.
    assert "resume from the first step" in note
    assert "resume where you left off" not in note


@pytest.mark.asyncio
async def test_synthesized_auto_resume_event_stays_empty_after_prep():
    """Inbound prep must not fabricate content for the content-less event —
    the sender prefix is attribution for a message nobody wrote."""
    runner = _make_runner()
    source = _shared_discord_thread_source()

    prepared = await runner._prepare_inbound_message_text(
        event=_synth_resume_event(source), source=source, history=[],
    )

    assert prepared == ""
    # And the seam persists the note (never a blank user row, #86580).
    _note, persisted = _prepare_resume_pending_message("cooperative_restart", prepared)
    assert persisted == _note
    assert persisted.strip()


@pytest.mark.asyncio
async def test_raced_in_real_text_resume_keeps_new_message_wording():
    """Real user text that raced in while the gateway was down keeps the
    has-message wording — it is correct for that case."""
    runner = _make_runner()
    source = _shared_discord_thread_source()
    event = MessageEvent(
        text="what did the deploy say?",
        message_type=MessageType.TEXT,
        source=source,
    )

    note = await _prepared_turn_message(runner, event)

    assert "NEW message below" in note
    assert "what did the deploy say?" in note
    assert "parked itself" not in note
    # Attribution still applies to real text in shared sessions.
    assert "[Quix]" in note
