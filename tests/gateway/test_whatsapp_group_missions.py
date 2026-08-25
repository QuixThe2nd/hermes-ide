"""Goal-bound WhatsApp GROUP missions across the inbound pipeline.

Covers the three layers a group mission must unlock together:

- **Adapter intake** (``WhatsAppBehaviorMixin``): while a mission is active
  for the exact group chat, the group is admitted even when ``group_policy``
  is ``disabled``/excludes it, and messages need no mention/reply. Other
  groups stay blocked; closing the mission blocks the group again without a
  gateway restart (the mission store is read live).
- **Gateway authorization** (``_is_user_authorized``): admission by the exact
  group chat id only — never a participant ``user_id`` — and participant DMs
  stay denied unless they have their own DM mission.
- **Routing + session keys**: the group routes to the mission's profile and
  every member lands in ONE shared session/batch key
  (``agent:<profile>:whatsapp:group:<group-chat>``), plus the prompt-cache
  contract (Active Mission section + change-key digest moving only on
  mission start/close for that chat).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from gateway.session import (
    SessionContext,
    SessionSource,
    build_session_key,
    build_session_context_prompt,
)

GROUP = "120363024955757999@g.us"
OTHER_GROUP = "111111111111111111@g.us"
MEMBER = "61400000000@s.whatsapp.net"
MEMBER_TWO = "61499999999@s.whatsapp.net"


@pytest.fixture()
def missions_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so mission files never touch a real install."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    import plugins.missions as pm

    return pm


def _start_group_mission(pm, chat_id=GROUP, *, profile="assistant"):
    result = json.loads(
        pm.handle_dispatch_assistant(
            {
                "chat_id": chat_id,
                "chat_name": "Picnic Crew",
                "goal": "Agree a picnic date",
                **({"profile": profile} if profile else {}),
            },
            session_key="agent:main:discord:thread:abc:abc",
            session_id="sess-1",
        )
    )
    assert result["ok"] is True, result
    return result


def _close_mission(pm, mission_id):
    result = json.loads(
        pm.handle_dispatch_agent(
            {"action": "complete", "mission_id": mission_id, "outcome": "done"}
        )
    )
    assert result["ok"] is True, result


class _IntakeAdapter(WhatsAppBehaviorMixin):
    """Minimal behavior-layer adapter: gating only, no transport."""

    def __init__(self, *, group_policy="disabled", require_mention=True):
        self.config = SimpleNamespace(
            extra={"group_policy": group_policy, "require_mention": require_mention}
        )
        self.name = "whatsapp-test"
        self._group_policy = group_policy
        self._group_allow_from = set()
        self._mention_patterns = []
        self._reply_prefix = None


def _group_message(chat_id=GROUP, sender=MEMBER, body="plain message, no mention"):
    return {
        "chatId": chat_id,
        "chatName": "Picnic Crew",
        "isGroup": True,
        "senderId": sender,
        "senderName": "Alex",
        "body": body,
        "botIds": ["15550000000@s.whatsapp.net"],
        "mentionedIds": [],
        "quotedParticipant": None,
    }


def _group_source(chat_id=GROUP, sender=MEMBER):
    return SessionSource(
        platform=Platform.WHATSAPP,
        chat_id=chat_id,
        chat_name="Picnic Crew",
        chat_type="group",
        user_id=sender,
        user_name="Alex",
    )


# ---------------------------------------------------------------------------
# Adapter intake
# ---------------------------------------------------------------------------


class TestGroupMissionIntake:
    def test_active_group_bypasses_disabled_policy_and_mention_requirement(self, missions_home):
        _start_group_mission(missions_home)
        adapter = _IntakeAdapter(group_policy="disabled", require_mention=True)

        # group_policy=disabled and the message has no mention or reply, yet
        # the active mission alone admits the group.
        assert adapter._is_group_allowed(GROUP) is True
        assert adapter._should_process_message(_group_message()) is True
        assert adapter._should_process_message(_group_message()) is True
        # Every member reaches the assistant, mention or not.
        assert adapter._should_process_message(_group_message(sender=MEMBER_TWO)) is True

    def test_unrelated_group_stays_blocked(self, missions_home):
        _start_group_mission(missions_home)
        adapter = _IntakeAdapter(group_policy="disabled", require_mention=False)
        assert adapter._should_process_message(_group_message(chat_id=OTHER_GROUP)) is False
        assert adapter._is_group_allowed(OTHER_GROUP) is False

    def test_closed_mission_blocks_again_without_restart(self, missions_home):
        started = _start_group_mission(missions_home)
        adapter = _IntakeAdapter(group_policy="disabled", require_mention=True)
        assert adapter._should_process_message(_group_message()) is True

        _close_mission(missions_home, started["mission_id"])

        # Same adapter instance, no restart: the group is blocked again.
        assert adapter._should_process_message(_group_message()) is False
        assert adapter._is_group_allowed(GROUP) is False

    def test_no_mission_keeps_configured_policy(self, missions_home):
        adapter = _IntakeAdapter(group_policy="disabled", require_mention=False)
        assert adapter._should_process_message(_group_message()) is False

    def test_allowlist_policy_excluding_group_admits_mission_group(self, missions_home):
        _start_group_mission(missions_home)
        adapter = _IntakeAdapter(group_policy="allowlist", require_mention=True)
        adapter._group_allow_from = {OTHER_GROUP}

        assert adapter._is_group_allowed(GROUP) is True
        assert adapter._is_group_allowed(OTHER_GROUP) is True
        adapter2 = _IntakeAdapter(group_policy="allowlist", require_mention=True)
        adapter2._group_allow_from = {OTHER_GROUP}
        # Close → only the allowlisted group remains admitted.
        started = missions_home.find_active_group_mission(GROUP)
        _close_mission(missions_home, started["mission_id"])
        assert adapter2._is_group_allowed(GROUP) is False
        assert adapter2._is_group_allowed(OTHER_GROUP) is True


# ---------------------------------------------------------------------------
# Gateway authorization
# ---------------------------------------------------------------------------


def _make_runner(*, extra=None, mission_only_dms=False):
    from gateway.run import GatewayRunner

    platform_extra = {"group_policy": "disabled"}
    if extra:
        platform_extra.update(extra)
    if mission_only_dms:
        platform_extra["mission_only_dms"] = True
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.WHATSAPP: PlatformConfig(enabled=True, extra=platform_extra)
        }
    )
    runner.adapters = {Platform.WHATSAPP: _IntakeAdapter(group_policy="disabled")}
    runner.adapters[Platform.WHATSAPP].config.extra.update(platform_extra)
    runner.pairing_store = None
    runner.pairing_stores = {}
    return runner


class TestGroupMissionAuthorization:
    def test_active_group_authorized_by_exact_chat_id(self, missions_home, monkeypatch):
        for var in ("WHATSAPP_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS", "WHATSAPP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(var, raising=False)
        _start_group_mission(missions_home)
        runner = _make_runner()

        # A complete stranger posting in the mission group is admitted by
        # the chat id alone — group_policy is "disabled" and no allowlist
        # exists, so the only thing authorizing this is the mission.
        assert runner._is_user_authorized(_group_source(sender=MEMBER_TWO)) is True

    def test_closed_mission_denies_again(self, missions_home, monkeypatch):
        for var in ("WHATSAPP_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS", "WHATSAPP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(var, raising=False)
        started = _start_group_mission(missions_home)
        runner = _make_runner()
        assert runner._is_user_authorized(_group_source()) is True

        _close_mission(missions_home, started["mission_id"])
        assert runner._is_user_authorized(_group_source()) is False

    def test_participant_dm_mission_never_admits_a_group(self, missions_home, monkeypatch):
        """Group admission is by the exact group chat id, never user_id."""
        for var in ("WHATSAPP_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS", "WHATSAPP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(var, raising=False)
        json.loads(
            missions_home.handle_dispatch_assistant(
                {"chat_id": MEMBER, "goal": "dm mission"},
                session_key="agent:main:discord:thread:abc:abc",
                session_id="sess-1",
            )
        )
        runner = _make_runner(mission_only_dms=True)

        # MEMBER has an active DM mission but posts in a group with NO group
        # mission: the participant's user_id must not admit the group.
        assert runner._is_user_authorized(_group_source(chat_id=OTHER_GROUP, sender=MEMBER)) is False

    def test_group_mission_does_not_authorize_participant_dm(self, missions_home, monkeypatch):
        """DMs from group members stay denied without their own DM mission."""
        for var in ("WHATSAPP_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS", "WHATSAPP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(var, raising=False)
        _start_group_mission(missions_home)
        runner = _make_runner(mission_only_dms=True)

        dm_source = SessionSource(
            platform=Platform.WHATSAPP,
            chat_id=MEMBER,
            chat_type="dm",
            user_id=MEMBER,
            user_name="Alex",
        )
        assert runner._is_user_authorized(dm_source) is False


# ---------------------------------------------------------------------------
# Routing + session keys
# ---------------------------------------------------------------------------


class TestGroupMissionRoutingAndSessionKeys:
    def _routing_runner(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            multiplex_profiles=True, platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)}
        )
        return runner

    def test_active_group_routes_to_mission_profile(self, missions_home):
        _start_group_mission(missions_home)
        runner = self._routing_runner()
        assert runner._profile_name_for_source(_group_source()) == "assistant"

    def test_active_dm_routes_to_mission_profile(self, missions_home):
        json.loads(
            missions_home.handle_dispatch_agent(
                {"action": "start", "chat_id": MEMBER, "goal": "g", "profile": "assistant"}
            )
        )
        runner = self._routing_runner()
        dm_source = SessionSource(
            platform=Platform.WHATSAPP,
            chat_id=MEMBER,
            chat_type="dm",
            user_id=MEMBER,
        )
        assert runner._profile_name_for_source(dm_source) == "assistant"

    def test_unknown_chat_falls_through_to_routes(self, missions_home):
        runner = self._routing_runner()
        assert runner._profile_name_for_source(_group_source(chat_id=OTHER_GROUP)) is None

    def test_all_senders_share_one_group_session_key(self, missions_home):
        _start_group_mission(missions_home)
        expected = f"agent:assistant:whatsapp:group:{GROUP}"
        for sender in (MEMBER, MEMBER_TWO):
            key = build_session_key(
                _group_source(sender=sender),
                group_sessions_per_user=True,
                profile="assistant",
            )
            assert key == expected

    def test_without_mission_group_sessions_stay_per_sender(self, missions_home):
        key_one = build_session_key(
            _group_source(sender=MEMBER), group_sessions_per_user=True, profile="assistant"
        )
        key_two = build_session_key(
            _group_source(sender=MEMBER_TWO), group_sessions_per_user=True, profile="assistant"
        )
        assert key_one != key_two
        assert key_one == f"agent:assistant:whatsapp:group:{GROUP}:{MEMBER.split('@')[0]}"

    def test_closed_mission_restores_per_sender_keys(self, missions_home):
        started = _start_group_mission(missions_home)
        assert build_session_key(
            _group_source(sender=MEMBER), group_sessions_per_user=True, profile="assistant"
        ) == f"agent:assistant:whatsapp:group:{GROUP}"
        _close_mission(missions_home, started["mission_id"])
        assert build_session_key(
            _group_source(sender=MEMBER), group_sessions_per_user=True, profile="assistant"
        ) == f"agent:assistant:whatsapp:group:{GROUP}:{MEMBER.split('@')[0]}"

    def test_all_senders_share_one_debounce_batch_key(self, missions_home):
        """Adapter-level text batching keys must not split per sender either."""
        from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(
            PlatformConfig(enabled=True, extra={"group_sessions_per_user": True})
        )
        _start_group_mission(missions_home)

        def _batch_key(sender):
            event = MessageEvent(
                text="hi",
                message_type=MessageType.TEXT,
                source=_group_source(sender=sender),
            )
            return adapter._text_batch_key(event)

        key_one = _batch_key(MEMBER)
        key_two = _batch_key(MEMBER_TWO)
        assert key_one == key_two
        assert key_one == f"agent:main:whatsapp:group:{GROUP}"

        started = missions_home.find_active_group_mission(GROUP)
        _close_mission(missions_home, started["mission_id"])
        assert _batch_key(MEMBER) != _batch_key(MEMBER_TWO)


# ---------------------------------------------------------------------------
# Prompt + prompt-cache contract
# ---------------------------------------------------------------------------


def _context_for(source):
    return SessionContext(source=source, connected_platforms=[], home_channels={})


class TestGroupMissionPrompt:
    def test_group_session_prompt_carries_active_mission(self, missions_home):
        _start_group_mission(missions_home)
        prompt = build_session_context_prompt(_context_for(_group_source()))
        assert "## Active Mission" in prompt
        assert "Agree a picnic date" in prompt
        assert "Picnic Crew" in prompt

    def test_dm_without_mission_has_no_active_mission_section(self, missions_home):
        _start_group_mission(missions_home)
        dm_source = SessionSource(
            platform=Platform.WHATSAPP,
            chat_id=MEMBER,
            chat_type="dm",
            user_id=MEMBER,
        )
        assert "## Active Mission" not in build_session_context_prompt(_context_for(dm_source))

    def test_change_key_moves_only_on_mission_start_and_close(self, missions_home):
        """Prompt-cache parity: the digest is stable while the mission runs."""
        from gateway.run import GatewayRunner

        other_context = _context_for(_group_source(chat_id=OTHER_GROUP))
        context = _context_for(_group_source())

        before = GatewayRunner._ephemeral_change_key(context, redact_pii=False)
        other_before = GatewayRunner._ephemeral_change_key(other_context, redact_pii=False)

        started = _start_group_mission(missions_home)
        during = GatewayRunner._ephemeral_change_key(context, redact_pii=False)
        during_again = GatewayRunner._ephemeral_change_key(context, redact_pii=False)
        other_during = GatewayRunner._ephemeral_change_key(other_context, redact_pii=False)

        assert before != during  # start re-renders exactly once for this chat
        assert during == during_again  # stable across turns while active
        assert other_before == other_during  # other chats keep their cache

        _close_mission(missions_home, started["mission_id"])
        after = GatewayRunner._ephemeral_change_key(context, redact_pii=False)
        assert after != during  # close re-renders
        assert after == before  # back to the no-mission baseline digest
