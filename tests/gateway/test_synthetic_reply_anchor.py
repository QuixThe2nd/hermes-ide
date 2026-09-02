"""Synthetic injections borrow the session's last real user message id.

Background completions (async-delegation results, process notifications,
loop wakeups) and the cooperative startup-resume turn
(``_schedule_resume_pending_sessions``) are injected as
``MessageEvent(internal=True)`` with no platform message_id of their own.
Their turn-final replies therefore had no reply anchor — and on Discord a
final send without a ``MessageReference`` never pings the user. The gateway
now remembers the last REAL user message id per session and synthetic
injections fall back to it.

The startup-resume turn borrows the id as a REPLY-ONLY anchor
(``MessageEvent.reply_anchor_id``, consumed by ``_reply_anchor_for_event``
for trusted internal events): its ``message_id`` — the inbound identity —
stays None, so the synthetic turn can never claim the real user message's
platform id in turn-context persistence or transcript rows.
"""
import asyncio
import dataclasses
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _resolve_progress_thread_id
from gateway.session import AsyncSessionStore, SessionEntry, SessionSource, SessionStore


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="1000",
        chat_type="thread",
        thread_id="2000",
        user_id="42",
        user_name="parsayazdani",
    )


@pytest.mark.asyncio
async def test_inject_watch_notification_uses_remembered_anchor(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
    )
    store = SessionStore(tmp_path, runner.config)

    entry = await asyncio.to_thread(store.get_or_create_session, _source())
    await asyncio.to_thread(
        store.set_session_metadata,
        entry.session_key,
        "_last_user_message_id",
        "999888777",
    )
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)

    captured: dict = {}

    class _Adapter:
        async def handle_message(self, event):
            captured["message_id"] = event.message_id

    runner.adapters = {Platform.DISCORD: _Adapter()}
    # resolve_delivery_transport is not exercised for native adapters when
    # the literal scan finds the platform first; keep it simple.
    runner._running = True

    evt = {
        "type": "async_delegation",
        "session_key": entry.session_key,
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": "1000",
        "thread_id": "2000",
        "user_id": "42",
        "user_name": "parsayazdani",
        "message_id": "",
    }

    result = await runner._inject_watch_notification("subagent finished", evt)
    assert result is True
    assert captured["message_id"] == "999888777"


@pytest.mark.asyncio
async def test_inject_watch_notification_without_anchor_stays_none(tmp_path):
    """No remembered anchor (fresh/CLI-only session) keeps historical behaviour."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
    )
    store = SessionStore(tmp_path, runner.config)
    entry = await asyncio.to_thread(store.get_or_create_session, _source())
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)

    captured: dict = {}

    class _Adapter:
        async def handle_message(self, event):
            captured["message_id"] = event.message_id

    runner.adapters = {Platform.DISCORD: _Adapter()}

    evt = {
        "type": "completion",
        "session_key": entry.session_key,
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": "1000",
        "thread_id": "2000",
    }

    result = await runner._inject_watch_notification("process done", evt)
    assert result is True
    assert captured["message_id"] is None


def test_internal_events_never_claim_anchor_in_transcript_rows():
    """The transcript dedupe must not see the borrowed anchor as a duplicate.

    Internal turns carry display_kind and skip message_id stamping; this is
    enforced at both persist sites via ``not getattr(event, 'internal', False)``.
    """
    internal = MessageEvent(text="x", internal=True, message_id="999888777")
    assert getattr(internal, "internal", False) is True

    real = MessageEvent(text="y", internal=False, message_id="111222333")
    assert not getattr(real, "internal", False)


# ---------------------------------------------------------------------------
# Cooperative startup-resume turns (empty-text internal events from
# _schedule_resume_pending_sessions) borrow the same anchor as a REPLY-ONLY
# override: ``event.message_id`` (the inbound identity) stays None and
# ``_reply_anchor_for_event`` substitutes the remembered id for reply
# threading — so nothing downstream (inbound_message_id → turn-context
# platform_message_id, transcript message_id stamps, the #47237 platform-id
# dedupe) can mistake the borrowed id for the synthetic turn's own identity.
# ---------------------------------------------------------------------------

ANCHOR = "999888777"
SESSION_KEY = "agent:main:telegram:group:-1001:12345"


def _bootstrap_turn(monkeypatch, tmp_path, *, run_agent_result):
    """Minimal GatewayRunner setup (pattern from test_internal_notification_marker_82888)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=SESSION_KEY,
        session_id="sess-anchor",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    runner._run_agent = AsyncMock(return_value=run_agent_result)
    return runner


def _telegram_group_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


def _resume_event() -> MessageEvent:
    """The exact event shape ``_schedule_resume_pending_sessions`` builds for
    a session whose remembered last real user message id is ANCHOR: an
    empty-text trusted-internal event whose ``message_id`` (inbound identity)
    stays None and whose ``reply_anchor_id`` carries the borrowed anchor."""
    event = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=_telegram_group_source(),
        internal=True,
    )
    # Assigned post-construction (same field the scheduler sets) so the
    # assertions below fail on behaviour — not on a constructor TypeError —
    # when run against a tree that predates the reply-only mechanism.
    event.reply_anchor_id = ANCHOR
    return event


def _persisted_user_rows(store) -> list:
    return [
        call.args[1]
        for call in store.append_to_transcript.call_args_list
        if len(call.args) >= 2
        and isinstance(call.args[1], dict)
        and call.args[1].get("role") == "user"
    ]


@pytest.mark.asyncio
async def test_startup_resume_fallback_rows_never_claim_the_anchor(
    monkeypatch, tmp_path
):
    """Failed-turn fallback persistence: the internal turn's own user rows
    (early-failure branch) never stamp the borrowed anchor as a
    ``message_id`` — the id already belongs to the real turn's row — and the
    #47237 dedupe is never consulted for it.
    """
    runner = _bootstrap_turn(
        monkeypatch,
        tmp_path,
        run_agent_result={
            "failed": True,
            "final_response": None,
            "error": "429 Too Many Requests — rate limit exceeded",
            "messages": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        },
    )

    await runner._handle_message_with_agent(
        _resume_event(), _telegram_group_source(), SESSION_KEY, 1
    )

    rows = _persisted_user_rows(runner.session_store)
    assert rows, "expected a fallback user-row write for the failed turn"
    for row in rows:
        assert row["role"] == "user"
        assert row.get("display_kind") == "internal_notification"
        assert "message_id" not in row, (
            "the borrowed anchor must not be claimed by the internal turn's "
            "own transcript row"
        )
    # The dedupe authority is never asked about the borrowed id: the
    # duplicate-skip guard is gated on ``not internal``.
    for call in runner.session_store.has_platform_message_id.call_args_list:
        assert ANCHOR not in (str(a) for a in call.args)


@pytest.mark.asyncio
async def test_startup_resume_new_messages_rows_never_claim_the_anchor(
    monkeypatch, tmp_path
):
    """Identity leak (new_messages branch): on a successful turn the gateway
    appends the agent's new rows itself and stamps ``event.message_id`` on
    the first user row with NO internal guard — the only thing keeping the
    borrowed anchor out of that stamp is the reply-only model (message_id
    stays None). Riding the anchor on ``message_id`` would make the resume
    turn's synthetic user row claim the real user message's platform id.
    """
    runner = _bootstrap_turn(
        monkeypatch,
        tmp_path,
        run_agent_result={
            "final_response": "restored — what next?",
            "messages": [
                {"role": "user", "content": "resume"},
                {"role": "assistant", "content": "restored — what next?"},
            ],
            "tools": [],
            "history_offset": 0,  # < len(messages) → gateway-side new_messages
            "last_prompt_tokens": 0,
        },
    )

    await runner._handle_message_with_agent(
        _resume_event(), _telegram_group_source(), SESSION_KEY, 1
    )

    rows = _persisted_user_rows(runner.session_store)
    assert rows, "expected gateway-side new_messages rows for the turn"
    for row in rows:
        assert "message_id" not in row, (
            "the new_messages branch stamped the borrowed anchor onto the "
            "resume turn's synthetic user row"
        )


@pytest.mark.asyncio
async def test_startup_resume_exception_row_never_claims_the_anchor(
    monkeypatch, tmp_path
):
    """Identity leak (exception branch): when the agent turn raises, the
    gateway's except-handler persists the inbound user row and stamps
    ``event.message_id`` with NO internal guard — again only the reply-only
    model (message_id stays None) keeps the borrowed anchor out of it.
    """
    runner = _bootstrap_turn(
        monkeypatch,
        tmp_path,
        run_agent_result={
            "final_response": "never returned",
            "messages": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        },
    )
    runner._run_agent = AsyncMock(side_effect=RuntimeError("provider exploded"))

    await runner._handle_message_with_agent(
        _resume_event(), _telegram_group_source(), SESSION_KEY, 1
    )

    rows = _persisted_user_rows(runner.session_store)
    assert rows, "expected the exception-path user-row write"
    for row in rows:
        assert "message_id" not in row, (
            "the exception-persistence branch stamped the borrowed anchor "
            "onto the resume turn's user row"
        )


@pytest.mark.asyncio
async def test_production_resume_event_cannot_claim_identity_in_transcript(
    monkeypatch, tmp_path
):
    """End-to-end identity-leak detector: the REAL event built by
    ``_schedule_resume_pending_sessions`` (not a test-fabricated shape) is
    driven through the turn machinery, and none of the gateway's transcript
    branches — successful-turn new_messages writes and the agent-exception
    fallback — may stamp the borrowed anchor as a ``message_id``.

    This is the regression shape 4c67d270 shipped: the scheduler put the
    remembered anchor on ``event.message_id``, and the new_messages /
    exception persist branches (which have no internal guard) happily
    claimed it for the synthetic turn's own rows.
    """
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    sched_runner, sched_adapter = make_restart_runner()
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:dm:anchor-chat",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=make_restart_source(chat_id="anchor-chat"),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        metadata={"_last_user_message_id": ANCHOR},
    )
    sched_runner.session_store._entries = {pending_entry.session_key: pending_entry}
    sched_adapter.handle_message = AsyncMock()

    assert sched_runner._schedule_resume_pending_sessions() == 1
    await asyncio.sleep(0)
    prod_event = sched_adapter.handle_message.await_args.args[0]
    # The production event itself: reply-only borrow, no inbound identity.
    assert prod_event.internal is True
    assert prod_event.message_id is None

    # Successful turn → gateway-side new_messages rows.
    turn_runner = _bootstrap_turn(
        monkeypatch,
        tmp_path,
        run_agent_result={
            "final_response": "restored — what next?",
            "messages": [
                {"role": "user", "content": "resume"},
                {"role": "assistant", "content": "restored — what next?"},
            ],
            "tools": [],
            "history_offset": 0,  # < len(messages) → new_messages branch
            "last_prompt_tokens": 0,
        },
    )
    await turn_runner._handle_message_with_agent(
        prod_event, prod_event.source, SESSION_KEY, 1
    )
    for row in _persisted_user_rows(turn_runner.session_store):
        assert "message_id" not in row, (
            "the new_messages branch stamped the borrowed anchor onto the "
            "resume turn's synthetic user row"
        )

    # Agent exception → the except-handler's inbound-user fallback row.
    exc_runner = _bootstrap_turn(
        monkeypatch,
        tmp_path,
        run_agent_result={
            "final_response": "never returned",
            "messages": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        },
    )
    exc_runner._run_agent = AsyncMock(side_effect=RuntimeError("provider exploded"))
    await exc_runner._handle_message_with_agent(
        prod_event, prod_event.source, SESSION_KEY, 1
    )
    rows = _persisted_user_rows(exc_runner.session_store)
    assert rows, "expected the exception-path user-row write"
    for row in rows:
        assert "message_id" not in row, (
            "the exception-persistence branch stamped the borrowed anchor "
            "onto the resume turn's user row"
        )


@pytest.mark.asyncio
async def test_startup_resume_reply_only_anchor_flows_without_inbound_identity(
    monkeypatch, tmp_path
):
    """Reply delivery vs inbound identity: the borrowed id reaches the turn
    plumbing as the reply anchor only. ``_run_agent`` gets
    ``event_message_id=ANCHOR`` (reply threading / stream seed) while
    ``inbound_message_id`` stays None — the synthetic turn owns no platform
    message, so turn-context persistence must never stamp a
    ``platform_message_id`` for it.
    """
    runner = _bootstrap_turn(
        monkeypatch,
        tmp_path,
        run_agent_result={
            "final_response": "restored — what next?",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [],
            "history_offset": 1,  # equals len(messages) → no-new-messages path
            "last_prompt_tokens": 0,
        },
    )

    event = _resume_event()
    assert event.message_id is None, (
        "the resume event's inbound identity must stay None — the anchor is "
        "reply-only"
    )
    await runner._handle_message_with_agent(
        event, _telegram_group_source(), SESSION_KEY, 1
    )

    kwargs = runner._run_agent.call_args.kwargs
    # The turn-final reply anchor (reply_to / stream seed) is the borrowed id.
    assert kwargs["event_message_id"] == ANCHOR
    # The inbound identity is NOT the borrowed id — nothing downstream may
    # stamp it as the turn's platform_message_id.
    assert kwargs["inbound_message_id"] is None
    # Internal-event gating is keyed on ``internal`` and unchanged by the id.
    assert kwargs["persist_user_display_kind"] == "internal_notification"

    # The remembered anchor is only written from REAL inbound turns — the
    # internal resume turn must not rewrite it.
    for call in runner.session_store.set_session_metadata.call_args_list:
        assert "_last_user_message_id" not in (str(a) for a in call.args)


def test_borrowed_anchor_keeps_interim_progress_standalone():
    """Interim vs final routing: the borrowed anchor rides the turn-final
    (reply anchor) but does not attach interim tool-progress bubbles to it on
    the platforms whose finals need the anchor — progress stays standalone,
    exactly as for a real inbound message id.
    """
    event = _resume_event()

    # Turn-final: the reply anchor resolver returns the borrowed id even
    # though the event's inbound identity (message_id) is None.
    assert GatewayRunner._reply_anchor_for_event(event) == ANCHOR
    assert event.message_id is None

    # Interim tool-progress bubbles: no thread/reply target is synthesized
    # from the anchor on reply-semantic platforms (Discord/Telegram) — the
    # anchor only threads progress on the platforms that already thread
    # every real message the same way (Slack/Mattermost/Buzz).
    assert _resolve_progress_thread_id(Platform.DISCORD, None, ANCHOR) is None
    assert _resolve_progress_thread_id(Platform.TELEGRAM, None, ANCHOR) is None
    assert _resolve_progress_thread_id(Platform.SLACK, None, ANCHOR) == ANCHOR

    # And with no remembered anchor, the final keeps the historical
    # no-anchor behaviour rather than inventing one.
    no_anchor = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=_telegram_group_source(),
        internal=True,
    )
    assert GatewayRunner._reply_anchor_for_event(no_anchor) is None
    assert getattr(no_anchor, "reply_anchor_id", None) is None


def test_reply_anchor_override_is_trusted_internal_only():
    """The reply-only override is consumed for trusted internal events only.

    An inbound (untrusted) event carrying the override — or an internal
    event that already has a real platform message_id, like the async/process
    synthetic injections — must resolve exactly as before: the override
    never substitutes a non-internal event's anchor.
    """
    # Trusted internal + override → the borrowed anchor threads the reply.
    assert GatewayRunner._reply_anchor_for_event(_resume_event()) == ANCHOR

    # Untrusted inbound + override → ignored (no anchor invented from it).
    untrusted = MessageEvent(
        text="user text",
        message_type=MessageType.TEXT,
        source=_telegram_group_source(),
        internal=False,
    )
    untrusted.reply_anchor_id = ANCHOR
    assert GatewayRunner._reply_anchor_for_event(untrusted) is None

    # Untrusted inbound with its own message_id → that id wins; the override
    # cannot displace a real inbound anchor.
    untrusted_with_id = MessageEvent(
        text="user text",
        message_type=MessageType.TEXT,
        source=_telegram_group_source(),
        internal=False,
        message_id="111222333",
    )
    untrusted_with_id.reply_anchor_id = ANCHOR
    assert GatewayRunner._reply_anchor_for_event(untrusted_with_id) == "111222333"

    # Internal synthetic event with a real message_id (the already-shipping
    # async/process injections) → unchanged; the override is not consulted.
    legacy_internal = MessageEvent(
        text="process done",
        message_type=MessageType.TEXT,
        source=_telegram_group_source(),
        internal=True,
        message_id="444555666",
    )
    assert GatewayRunner._reply_anchor_for_event(legacy_internal) == "444555666"


def test_reply_anchor_override_preserves_telegram_thread_semantics():
    """Existing Telegram semantics are unchanged by the override: DM topic
    lanes reply to the triggering message (the anchor), while forum/supergroup
    topics stay routed by topic metadata (None) even when an anchor exists.
    """
    dm_lane = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="77",
            chat_type="dm",
            thread_id="88",
            user_id="12345",
        ),
        internal=True,
    )
    dm_lane.reply_anchor_id = ANCHOR
    assert GatewayRunner._reply_anchor_for_event(dm_lane) == ANCHOR

    forum = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="99",
            user_id="12345",
        ),
        internal=True,
    )
    forum.reply_anchor_id = ANCHOR
    assert GatewayRunner._reply_anchor_for_event(forum) is None


@pytest.mark.asyncio
async def test_resume_anchor_threads_only_the_discord_final_notify():
    """Adapter bridge: the borrowed anchor (resolved through the seam) reaches
    the Discord turn-final as a MessageReference on the notify-marked send,
    while an interim-marked send carrying the same anchor stays standalone —
    exactly the delivery split a real inbound message id produces.
    """
    from tests.gateway.test_discord_stream_final_reply import _make_adapter

    event = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="1000",
            chat_type="thread",
            thread_id="2000",
            user_id="42",
        ),
        internal=True,
    )
    event.reply_anchor_id = ANCHOR
    anchor = GatewayRunner._reply_anchor_for_event(event)
    assert anchor == ANCHOR

    adapter, _channel, sent_messages, _delete_calls = _make_adapter()

    interim = await adapter.send(
        "555", "Using browser tool...", reply_to=anchor,
        metadata={"_interim_send": True},
    )
    assert interim.success is True
    assert sent_messages[0]["reference"] is None, (
        "interim tool output must stay standalone — no anchor ping"
    )

    final = await adapter.send(
        "555", "restored — what next?", reply_to=anchor,
        metadata={"notify": True},
    )
    assert final.success is True
    assert sent_messages[1]["reference"] is not None, (
        "the turn-final notify send must keep the borrowed anchor"
    )
    assert sent_messages[1]["reference"].message_id == int(ANCHOR)


# ---------------------------------------------------------------------------
# Positional-constructor ABI: ``reply_anchor_id`` is appended AFTER the
# legacy fields (and is keyword-set in practice), so the historical
# 24-positional-argument ``MessageEvent(...)`` construction keeps binding
# every argument to the field it always bound.
# ---------------------------------------------------------------------------

# The parent ABI (field order frozen the moment before reply_anchor_id was
# introduced). Regression guard for inserting new fields mid-dataclass.
LEGACY_EVENT_FIELDS = (
    "text",
    "message_type",
    "user_id",
    "user_name",
    "source",
    "raw_message",
    "message_id",
    "platform_update_id",
    "media_urls",
    "media_types",
    "media_text_inlined",
    "reply_to_message_id",
    "reply_to_text",
    "reply_to_author_id",
    "reply_to_author_name",
    "reply_to_is_own_message",
    "prompt_response",
    "auto_skill",
    "channel_prompt",
    "channel_context",
    "internal",
    "metadata",
    "timestamp",
    "allow_gateway_control",
)


def test_reply_anchor_id_preserves_legacy_positional_event_abi():
    """A mid-dataclass insertion of ``reply_anchor_id`` rebound the tail of
    every legacy 24-positional-argument construction (old arg 22 ``metadata``
    → reply_anchor_id, old arg 23 ``timestamp`` → metadata, old arg 24
    ``allow_gateway_control`` → timestamp). The field now sits after the
    legacy fields: each positional argument keeps binding its historical
    field, and the new field is reachable by keyword with a None default.
    """
    current = tuple(f.name for f in dataclasses.fields(MessageEvent))
    assert current[: len(LEGACY_EVENT_FIELDS)] == LEGACY_EVENT_FIELDS
    assert "reply_anchor_id" not in LEGACY_EVENT_FIELDS
    assert "reply_anchor_id" in current[len(LEGACY_EVENT_FIELDS) :]

    stamp = datetime.now()
    metadata = {"whatsapp_from_owner": True}
    values = {
        "text": "hello",
        "message_type": MessageType.TEXT,
        "user_id": "42",
        "user_name": "parsayazdani",
        "source": _telegram_group_source(),
        "raw_message": {"id": "raw"},
        "message_id": "111222333",
        "platform_update_id": 7,
        "media_urls": ["/tmp/a.png"],
        "media_types": ["image"],
        "media_text_inlined": [None],
        "reply_to_message_id": "444555666",
        "reply_to_text": "earlier",
        "reply_to_author_id": "7",
        "reply_to_author_name": "someone",
        "reply_to_is_own_message": True,
        "prompt_response": {"prompt_id": "p", "option_id": "o"},
        "auto_skill": "topic-skill",
        "channel_prompt": "be terse",
        "channel_context": "missed messages",
        "internal": True,
        "metadata": metadata,
        "timestamp": stamp,
        "allow_gateway_control": False,
    }
    assert tuple(values) == LEGACY_EVENT_FIELDS

    event = MessageEvent(*[values[name] for name in LEGACY_EVENT_FIELDS])
    for name in LEGACY_EVENT_FIELDS:
        assert getattr(event, name) == values[name], (
            f"legacy positional argument {name} rebound to another field"
        )

    # The exact tail the mid-class insertion used to corrupt: identity (not
    # just equality) so a shifted binding of any of them fails loudly.
    assert event.metadata is metadata
    assert event.timestamp is stamp
    assert event.allow_gateway_control is False

    # The appended field itself: positional construction leaves it at its
    # None default, and keyword use still reaches it.
    assert event.reply_anchor_id is None
    assert MessageEvent(text="x", reply_anchor_id=ANCHOR).reply_anchor_id == ANCHOR
