"""The hub record format for events that cross the restart boundary.

The durable drain snapshot/replay handover is gone (user contract, Parsa
2026-11-09): a restart never writes other chats' in-flight or drain-window
messages to ``drain_message_queue.json`` and startup never replays them —
``tests/gateway/test_restart_no_drain_snapshot.py`` covers that side, and
the boot-time disposal of legacy snapshots.

What survives is the JSON record format itself: the Discord/Matrix hub
clients carry a ``MessageEvent`` across the predecessor/successor RPC
boundary (registration, routing, handoff, processing-completion) as one of
these records. These tests pin its roundtrip fidelity — routing metadata,
thread prospects, media, channel instructions — and the trust flags that
must NEVER survive a serialization.
"""

from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_drain_queue import deserialize_drain_event, serialize_drain_event
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


def _event(
    text: str = "what needs my attention the most rn?",
    *,
    source=None,
    message_type: MessageType = MessageType.TEXT,
    media=None,
    message_id: str = "m-1",
):
    media = list(media or [])
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source or make_restart_source(),
        message_id=message_id,
        media_urls=media,
        media_types=["photo"] * len(media),
    )


def test_full_routing_metadata_and_thread_prospect_survive_the_roundtrip():
    """Routing metadata beyond the security keys (``whatsapp_from_owner``)
    and ``SessionSource.prospective_thread_id`` decide where and how a
    routed message lands — they persist too. The wire-invisible trust
    flags never do: restoring them from a record would re-grant an
    authorization the receiving process was never given."""
    runner, _adapter = make_restart_runner()
    source = make_restart_source(
        chat_id="meta-chat",
        prospective_thread_id="pm-77",
        auto_thread_created=True,
        auto_thread_initial_name="new thread",
        bot_display_name="Hermes Bot",
        delivered_via_upstream_relay=True,  # must NOT survive the record
        profile_route_rejected=True,  # must NOT survive the record
    )
    session_key = runner._session_key_for_source(source)
    event = _event("metadata please", source=source)
    event.metadata = {
        "whatsapp_from_owner": True,
        "gateway_session_key": "agent:main:whatsapp:dm:meta-chat",
        "slack_team_id": "T123",
        "hermes_plugin_id": "p-1",
    }

    record = serialize_drain_event(session_key, event)
    assert record["event"]["metadata"] == event.metadata
    assert record["source"]["prospective_thread_id"] == "pm-77"
    assert record["source"]["auto_thread_created"] is True
    assert record["source"]["bot_display_name"] == "Hermes Bot"
    assert "delivered_via_upstream_relay" not in record["source"]
    assert "profile_route_rejected" not in record["source"]

    _key, rebuilt = deserialize_drain_event(record)
    assert rebuilt.metadata == event.metadata
    assert rebuilt.source.prospective_thread_id == "pm-77"
    assert rebuilt.source.auto_thread_created is True
    assert rebuilt.source.auto_thread_initial_name == "new thread"
    assert rebuilt.source.bot_display_name == "Hermes Bot"
    assert rebuilt.source.delivered_via_upstream_relay is False
    assert rebuilt.source.profile_route_rejected is False


def test_captionless_photo_roundtrips():
    """A photo with no caption is media with ``text=""`` — the record must
    carry the empty string, because ``MessageEvent`` takes ``text`` as a
    required positional (a record without the key could never be rebuilt)."""
    source = make_restart_source(chat_id="silent-photo-chat")
    runner, _adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    event = _event(
        "",
        source=source,
        message_type=MessageType.PHOTO,
        media=["/tmp/hermes/media/silent.jpg"],
        message_id="m-silent",
    )

    record = serialize_drain_event(session_key, event)
    assert record["event"]["text"] == ""  # the key itself, not a dropped field

    key, rebuilt = deserialize_drain_event(record)
    assert key == session_key
    assert rebuilt.text == ""
    assert rebuilt.message_type == MessageType.PHOTO
    assert rebuilt.media_urls == ["/tmp/hermes/media/silent.jpg"]


def test_channel_prompt_and_context_survive_the_roundtrip():
    """Channel-scoped instructions — the per-channel system prompt and the
    history backfilled under require_mention — must reach the routed turn;
    the serialization whitelist used to drop them, so a handoff silently
    stripped the channel's instructions from the message."""
    source = make_restart_source(chat_id="channel-chat")
    runner, _adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    event = _event("channel instructions please", source=source)
    event.channel_prompt = "You are the #medicina triage assistant; keep answers clinical."
    event.channel_context = "earlier messages backfilled under require_mention"

    record = serialize_drain_event(session_key, event)
    assert record["event"]["channel_prompt"] == event.channel_prompt
    assert record["event"]["channel_context"] == event.channel_context

    _key, rebuilt = deserialize_drain_event(record)
    assert rebuilt.channel_prompt == event.channel_prompt
    assert rebuilt.channel_context == event.channel_context
