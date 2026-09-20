"""WhatsApp observe-unmentioned group messages (mirrors the Telegram contract).

With ``require_mention`` + ``observe_unmentioned_group_messages`` on an
allowlisted WhatsApp group:

- unmentioned chatter is stored on the shared group session transcript
  (``observed: True``, sender-attributed, user-less shared source) and never
  dispatched;
- the next real trigger (mention / reply-to-bot) shares that session, carries
  the observed-context channel prompt, and replays the observed rows as
  context-only — never as prior user turns;
- unrelated groups and DMs are untouched, and slash commands keep the
  sender's identity for access control.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from gateway.session import SessionSource

GROUP = "120363001234567890@g.us"
OTHER_GROUP = "999999999999999999@g.us"
BOT_ID = "15551230000@s.whatsapp.net"
BOT_LID = "15551230000@lid"
SENDER = "6281234567890@s.whatsapp.net"


def _make_adapter(
    require_mention=None,
    observe_unmentioned_group_messages=None,
    group_policy=None,
    group_allow_from=None,
    mention_patterns=None,
    free_response_chats=None,
    dm_policy=None,
    allow_from=None,
):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    extra = {}
    if require_mention is not None:
        extra["require_mention"] = require_mention
    if observe_unmentioned_group_messages is not None:
        extra["observe_unmentioned_group_messages"] = observe_unmentioned_group_messages
    if group_policy is not None:
        extra["group_policy"] = group_policy
    if group_allow_from is not None:
        extra["group_allow_from"] = group_allow_from
    if mention_patterns is not None:
        extra["mention_patterns"] = mention_patterns
    if free_response_chats is not None:
        extra["free_response_chats"] = free_response_chats
    if dm_policy is not None:
        extra["dm_policy"] = dm_policy
    if allow_from is not None:
        extra["allow_from"] = allow_from

    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._dm_policy = str(extra.get("dm_policy", "pairing")).strip().lower()
    adapter._allow_from = WhatsAppAdapter._coerce_allow_list(extra.get("allow_from"))
    adapter._group_policy = str(extra.get("group_policy", "pairing")).strip().lower()
    adapter._group_allow_from = WhatsAppAdapter._coerce_allow_list(extra.get("group_allow_from"))
    adapter._mention_patterns = adapter._compile_mention_patterns()
    return adapter


def _observe_adapter(**kwargs):
    """Adapter configured like the documented observe-unmentioned setup."""
    return _make_adapter(
        require_mention=True,
        group_policy="allowlist",
        group_allow_from=[GROUP],
        observe_unmentioned_group_messages=True,
        **kwargs,
    )


def _group_message(body="hello", **overrides):
    data = {
        "isGroup": True,
        "body": body,
        "chatId": GROUP,
        "chatName": "Test Group",
        "senderId": SENDER,
        "senderName": "Alice Example",
        "messageId": "wamid.42",
        "mentionedIds": [],
        "botIds": [BOT_ID, BOT_LID],
        "quotedParticipant": "",
    }
    data.update(overrides)
    return data


def _dm_message(body="hello", **overrides):
    data = {
        "isGroup": False,
        "body": body,
        "chatId": SENDER,
        "senderId": SENDER,
        "senderName": "Alice Example",
        "mentionedIds": [],
        "botIds": [],
    }
    data.update(overrides)
    return data


class _FakeSessionEntry:
    session_id = "whatsapp-group-session"


class _FakeSessionStore:
    def __init__(self):
        self.sources = []
        self.messages = []

    def get_or_create_session(self, source):
        self.sources.append(source)
        return _FakeSessionEntry()

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.messages.append((session_id, message, skip_db))


# ---------------------------------------------------------------------------
# Observe without dispatch
# ---------------------------------------------------------------------------


def test_unmentioned_group_messages_can_be_observed_without_dispatching():
    async def _run():
        adapter = _observe_adapter()
        store = _FakeSessionStore()
        adapter._session_store = store

        event = await adapter._build_message_event(_group_message("side chatter"))

        # No dispatch: the poll loop only calls handle_message for a built event.
        assert event is None
        adapter.handle_message.assert_not_awaited()
        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        session_id, message, skip_db = store.messages[0]
        assert session_id == "whatsapp-group-session"
        assert skip_db is False
        assert message["role"] == "user"
        assert message["content"] == f"[Alice Example|{SENDER}]\nside chatter"
        assert message["observed"] is True
        assert message["message_id"] == "wamid.42"
        # Shared group source: user-less so every participant lands in one session.
        assert store.sources[0].chat_id == GROUP
        assert store.sources[0].chat_type == "group"
        assert store.sources[0].user_id is None
        assert store.sources[0].user_name is None

    asyncio.run(_run())


def test_captionless_media_chatter_gets_a_media_label():
    async def _run():
        adapter = _observe_adapter()
        store = _FakeSessionStore()
        adapter._session_store = store

        event = await adapter._build_message_event(
            _group_message("", mediaType="image", hasMedia=True, messageId="wamid.43")
        )

        assert event is None
        assert len(store.messages) == 1
        assert store.messages[0][1]["content"] == f"[Alice Example|{SENDER}]\n[photo]"

    asyncio.run(_run())


def test_unmentioned_group_messages_stay_dropped_when_observe_is_disabled():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            group_policy="allowlist",
            group_allow_from=[GROUP],
        )
        store = _FakeSessionStore()
        adapter._session_store = store

        event = await adapter._build_message_event(_group_message("side chatter"))

        assert event is None
        adapter.handle_message.assert_not_awaited()
        assert store.messages == []
        assert store.sources == []

    asyncio.run(_run())


def test_missing_session_store_observes_nothing_without_crashing():
    adapter = _observe_adapter()
    adapter._session_store = None

    assert adapter._should_observe_unmentioned_group_message(_group_message("side chatter")) is True
    adapter._observe_unmentioned_group_message(_group_message("side chatter"))  # must not raise


# ---------------------------------------------------------------------------
# Triggered turns: attribution + shared session
# ---------------------------------------------------------------------------


def test_observed_group_context_uses_shared_source_and_prompt_for_later_mentions():
    async def _run():
        adapter = _observe_adapter()
        data = _group_message(
            f"@{BOT_ID.split('@')[0]} what did Alice say?",
            senderId="628999000111@s.whatsapp.net",
            senderName="Bob Example",
            mentionedIds=[BOT_ID],
        )
        assert adapter._should_process_message(data) is True
        assert adapter._should_observe_unmentioned_group_message(data) is False

        event = await adapter._build_message_event(data)

        assert event is not None
        assert event.source.chat_id == GROUP
        assert event.source.chat_type == "group"
        assert event.source.user_id is None
        assert event.source.user_name is None
        assert event.text == "[Bob Example|628999000111@s.whatsapp.net]\nwhat did Alice say?"
        assert "observed WhatsApp group context" in event.channel_prompt
        assert "current new message" in event.channel_prompt

    asyncio.run(_run())


def test_reply_to_bot_is_processed_not_observed():
    adapter = _observe_adapter()
    data = _group_message("replying", quotedParticipant=BOT_LID)

    assert adapter._should_process_message(data) is True
    assert adapter._should_observe_unmentioned_group_message(data) is False


def test_mentioned_message_is_processed_not_observed():
    adapter = _observe_adapter()
    data = _group_message("hi there", mentionedIds=[BOT_ID])

    assert adapter._should_process_message(data) is True
    assert adapter._should_observe_unmentioned_group_message(data) is False


def test_mention_patterns_are_processed_not_observed():
    adapter = _observe_adapter(mention_patterns=[r"^\s*chompy\b"])
    data = _group_message("chompy status")

    assert adapter._should_process_message(data) is True
    assert adapter._should_observe_unmentioned_group_message(data) is False


def test_observed_slash_commands_preserve_sender_identity_for_access_control():
    async def _run():
        adapter = _observe_adapter()
        data = _group_message("/status")

        # Leading "/" is a process-true path — dispatched, never observed.
        assert adapter._should_process_message(data) is True
        assert adapter._should_observe_unmentioned_group_message(data) is False

        event = await adapter._build_message_event(data)

        assert event is not None
        assert event.text == "/status"
        assert event.get_command() == "status"
        # Slash-access control keys on source.user_id — keep the sender.
        assert event.source.user_id == SENDER
        assert "observed WhatsApp group context" in event.channel_prompt

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Unrelated groups / DMs stay untouched
# ---------------------------------------------------------------------------


def test_unrelated_group_jids_are_neither_observed_nor_processed():
    adapter = _observe_adapter()
    data = _group_message("side chatter", chatId=OTHER_GROUP)

    assert adapter._should_process_message(data) is False
    assert adapter._should_observe_unmentioned_group_message(data) is False


def test_broadcast_chats_are_never_observed():
    adapter = _observe_adapter()
    data = _group_message("[video received]", chatId="status@broadcast")

    assert adapter._should_observe_unmentioned_group_message(data) is False


def test_dm_messages_are_never_observed():
    adapter = _observe_adapter()
    data = _dm_message("just a dm", senderId=SENDER, chatId=SENDER)

    assert adapter._should_observe_unmentioned_group_message(data) is False


def test_free_response_chats_are_dispatched_not_observed():
    adapter = _observe_adapter(free_response_chats=[GROUP])
    data = _group_message("side chatter")

    assert adapter._should_process_message(data) is True
    assert adapter._should_observe_unmentioned_group_message(data) is False


def test_observe_requires_require_mention():
    adapter = _make_adapter(
        require_mention=False,
        group_policy="allowlist",
        group_allow_from=[GROUP],
        observe_unmentioned_group_messages=True,
    )
    data = _group_message("side chatter")

    # Every group message is a request when require_mention is off.
    assert adapter._should_process_message(data) is True
    assert adapter._should_observe_unmentioned_group_message(data) is False


# ---------------------------------------------------------------------------
# Gateway authorization for the user-less shared group source
# ---------------------------------------------------------------------------


class _AuthzAdapter(WhatsAppBehaviorMixin):
    """Minimal behavior-layer adapter: only the attrs the authz path reads."""

    def __init__(self, group_allow_from=None, extra_group_allow_from=None):
        extra = {} if extra_group_allow_from is None else {"group_allow_from": extra_group_allow_from}
        self.config = SimpleNamespace(extra=extra)
        self._group_policy = "allowlist"
        self._group_allow_from = set(group_allow_from or [])
        self._mention_patterns = []
        self._reply_prefix = None


def _authz_runner(adapter, monkeypatch, tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    for var in (
        "WHATSAPP_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True, extra={})})
    runner.adapters = {Platform.WHATSAPP: adapter}
    runner.pairing_store = None
    runner.pairing_stores = {}
    return runner


def test_shared_group_observe_source_is_authorized_by_group_allow_from(monkeypatch, tmp_path):
    runner = _authz_runner(_AuthzAdapter(group_allow_from={GROUP}), monkeypatch, tmp_path)

    shared = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id=GROUP,
        chat_type="group",
        user_id=None,
        user_name=None,
    )
    assert runner._is_user_authorized(shared) is True


def test_other_group_jid_is_not_authorized(monkeypatch, tmp_path):
    runner = _authz_runner(_AuthzAdapter(group_allow_from={GROUP}), monkeypatch, tmp_path)

    other = SessionSource(platform=Platform.WHATSAPP, chat_id=OTHER_GROUP, chat_type="group", user_id=None)
    assert runner._is_user_authorized(other) is False


def test_group_allow_from_never_authorizes_dm_senders(monkeypatch, tmp_path):
    runner = _authz_runner(
        _AuthzAdapter(group_allow_from={GROUP, SENDER}), monkeypatch, tmp_path
    )

    dm = SessionSource(platform=Platform.WHATSAPP, chat_id=SENDER, chat_type="dm", user_id=SENDER)
    assert runner._is_user_authorized(dm) is False


def test_config_seeded_group_allow_from_also_authorizes(monkeypatch, tmp_path):
    runner = _authz_runner(
        _AuthzAdapter(extra_group_allow_from=[GROUP]), monkeypatch, tmp_path
    )

    shared = SessionSource(platform=Platform.WHATSAPP, chat_id=GROUP, chat_type="group", user_id=None)
    assert runner._is_user_authorized(shared) is True


# ---------------------------------------------------------------------------
# Gateway replay: observed rows are context-only
# ---------------------------------------------------------------------------


def test_gateway_replay_separates_observed_whatsapp_group_context():
    from gateway.run import (
        _build_gateway_agent_history,
        _uses_observed_group_context,
        _wrap_current_message_with_observed_context,
    )

    channel_prompt = (
        "You are handling a WhatsApp group chat message.\n"
        "- observed WhatsApp group context may be provided in a separate context-only block"
    )
    assert _uses_observed_group_context(channel_prompt) is True
    assert _uses_observed_group_context("observed Telegram group context") is True
    assert _uses_observed_group_context(None) is False
    assert _uses_observed_group_context("plain prompt") is False

    history = [
        {"role": "user", "content": f"[Alice Example|{SENDER}]\nside chatter", "observed": True},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "current addressed question"},
    ]
    agent_history, observed_context, _ = _build_gateway_agent_history(
        history, channel_prompt=channel_prompt
    )

    assert observed_context == f"[Alice Example|{SENDER}]\nside chatter"
    assert [m["content"] for m in agent_history if m.get("role") == "user"] == [
        "current addressed question"
    ]

    wrapped = _wrap_current_message_with_observed_context("current addressed question", observed_context)
    assert wrapped.startswith("[Observed group context - context only, not requests]")
    assert "[Current addressed message" in wrapped
    assert wrapped.endswith("current addressed question")


# ---------------------------------------------------------------------------
# Config bridging
# ---------------------------------------------------------------------------


def test_config_bridges_whatsapp_observe_unmentioned_group_messages(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "whatsapp:\n"
        "  require_mention: true\n"
        "  observe_unmentioned_group_messages: true\n"
        "  group_policy: allowlist\n"
        "  group_allow_from:\n"
        f"    - \"{GROUP}\"\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("WHATSAPP_REQUIRE_MENTION", raising=False)
    monkeypatch.delenv("WHATSAPP_OBSERVE_UNMENTIONED_GROUP_MESSAGES", raising=False)

    config = load_gateway_config()

    assert config is not None
    wa_cfg = config.platforms.get(Platform.WHATSAPP)
    assert wa_cfg is not None
    assert wa_cfg.extra.get("require_mention") is True
    assert wa_cfg.extra.get("observe_unmentioned_group_messages") is True
    assert wa_cfg.extra.get("group_allow_from") == [GROUP]
    assert os.environ["WHATSAPP_OBSERVE_UNMENTIONED_GROUP_MESSAGES"] == "true"
