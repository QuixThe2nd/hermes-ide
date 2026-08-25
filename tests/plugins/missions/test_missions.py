"""Tests for the missions plugin (dispatch_agent / dispatch_assistant / end_session).

Covers DM missions (start/complete/origin wakeup, automatic origin capture,
alias-complete pairing grant+revoke) and group missions (chat_type inference,
no pairing grant, no DM-history seed, end_session from the shared group
session key).

The mission store anchors on the process ``HERMES_HOME`` (see
``plugins/missions._missions_dir``), so every test runs against its own temp
home; the plugin keeps no module-level state, so no reload dance is needed.
"""

import json
import sys
import types

import pytest


@pytest.fixture()
def missions_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME for the mission store + pairing store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    import plugins.missions as pm

    return pm


@pytest.fixture()
def origin_queue(monkeypatch):
    """Capture async_delegation wakeups instead of touching the real registry."""
    class _Q:
        def __init__(self):
            self.items = []

        def put(self, evt):
            self.items.append(evt)

    q = _Q()
    fake = types.ModuleType("tools.process_registry")
    fake.process_registry = types.SimpleNamespace(completion_queue=q)
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake)
    return q


def _start(pm, chat_id="61400000000@s.whatsapp.net", **kw):
    args = {"action": "start", "chat_id": chat_id, "goal": kw.pop("goal", "Agree picnic time")}
    args.update(kw)
    return json.loads(pm.handle_dispatch_agent(args))


def _start_origin(pm, chat_id="61400000000@s.whatsapp.net", **kw):
    args = {"chat_id": chat_id, "goal": kw.pop("goal", "Agree picnic time")}
    args.update(kw)
    return json.loads(
        pm.handle_dispatch_assistant(
            args,
            session_key="agent:main:discord:thread:abc:abc",
            session_id="sess-1",
        )
    )


class TestDispatchAgent:
    def test_start_and_lookup(self, missions_home):
        r = _start(missions_home, chat_name="Alex")
        assert r["ok"] is True
        m = missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net")
        assert m is not None
        assert m["goal"] == "Agree picnic time"
        assert m["status"] == "active"

    def test_duplicate_start_rejected(self, missions_home):
        _start(missions_home)
        dup = json.loads(
            missions_home.handle_dispatch_agent({"action": "start", "chat_id": "+61400000000", "goal": "x"})
        )
        assert dup["ok"] is False
        assert dup["error"] == "mission_exists"

    def test_complete_requires_outcome(self, missions_home):
        mid = _start(missions_home)["mission_id"]
        bad = json.loads(missions_home.handle_dispatch_agent({"action": "complete", "mission_id": mid}))
        assert bad["ok"] is False
        good = json.loads(
            missions_home.handle_dispatch_agent(
                {"action": "complete", "mission_id": mid, "outcome": "Agreed 11am"}
            )
        )
        assert good["ok"] is True
        assert missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net") is None

    def test_cancel_without_outcome(self, missions_home):
        mid = _start(missions_home)["mission_id"]
        r = json.loads(missions_home.handle_dispatch_agent({"action": "cancel", "mission_id": mid}))
        assert r["ok"] is True

    def test_status_lists_journal(self, missions_home):
        mid = _start(missions_home)["mission_id"]
        missions_home.handle_dispatch_agent(
            {"action": "complete", "mission_id": mid, "outcome": "done"}
        )
        st = json.loads(missions_home.handle_dispatch_agent({"action": "status"}))
        assert st["ok"] is True
        assert st["active_missions"] == []
        assert len(st["recent_outcomes"]) == 1

    def test_start_records_profile(self, missions_home):
        _start(missions_home, profile="assistant")
        m = missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net")
        assert m is not None
        assert m["profile"] == "assistant"

    def test_start_defaults_profile_to_assistant(self, missions_home):
        _start(missions_home)
        m = missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net")
        assert m["profile"] == "assistant"


class TestDispatchAssistant:
    def test_start_captures_origin_session(self, missions_home):
        r = _start_origin(missions_home)
        assert r["ok"] is True
        m = missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net")
        assert m["created_by_session"] == "agent:main:discord:thread:abc:abc"
        assert m["origin_session_id"] == "sess-1"
        assert m["origin_parent_session_id"] == "sess-1"
        assert m["reply_target"] == "agent:main:discord:thread:abc:abc"

    def test_explicit_reply_to_wins_over_session_key(self, missions_home):
        _start_origin(missions_home, reply_to="discord:guild:1")
        m = missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net")
        assert m["reply_target"] == "discord:guild:1"
        # created_by_session still records where the dispatch happened.
        assert m["created_by_session"] == "agent:main:discord:thread:abc:abc"

    def test_complete_queues_origin_wakeup(self, missions_home, origin_queue):
        start = _start_origin(missions_home)
        done = json.loads(
            missions_home.handle_dispatch_agent(
                {
                    "action": "complete",
                    "mission_id": start["mission_id"],
                    "outcome": "Saturday 11am",
                }
            )
        )
        assert done["ok"] is True
        assert done["notified"] is True
        assert len(origin_queue.items) == 1
        evt = origin_queue.items[0]
        assert evt["type"] == "async_delegation"
        assert evt["session_key"] == "agent:main:discord:thread:abc:abc"
        assert evt["parent_session_id"] == "sess-1"
        assert "Saturday 11am" in evt["summary"]


class TestEndSession:
    def test_end_session_requires_outcome(self, missions_home):
        _start(missions_home)
        bad = json.loads(
            missions_home.handle_end_session(
                {},
                session_key="agent:assistant:whatsapp:dm:61400000000",
            )
        )
        assert bad["ok"] is False
        assert bad["error"] == "missing_outcome"

    def test_end_session_from_whatsapp_session_key_wakes_origin(
        self, missions_home, origin_queue
    ):
        start = _start_origin(missions_home)
        done = json.loads(
            missions_home.handle_end_session(
                {"outcome": "Saturday 11am"},
                session_key="agent:assistant:whatsapp:dm:61400000000",
            )
        )
        assert done["ok"] is True
        assert done["status"] == "completed"
        assert done["notified"] is True
        assert done["mission_id"] == start["mission_id"]
        assert missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net") is None
        assert len(origin_queue.items) == 1
        evt = origin_queue.items[0]
        assert evt["type"] == "async_delegation"
        assert evt["session_key"] == "agent:main:discord:thread:abc:abc"
        assert evt["parent_session_id"] == "sess-1"
        assert "Saturday 11am" in evt["summary"]

    def test_chat_id_from_whatsapp_session_key(self, missions_home):
        assert (
            missions_home._chat_id_from_session_key("agent:assistant:whatsapp:dm:61432996566")
            == "61432996566"
        )
        assert (
            missions_home._chat_id_from_session_key(
                "agent:assistant:whatsapp:group:120363024955757999@g.us"
            )
            == "120363024955757999@g.us"
        )


class TestContactHistoryBackfill:
    def test_start_seeds_prior_whatsapp_dm_into_assistant_session(self, missions_home, tmp_path):
        """A new DM mission should resume the contact's existing WhatsApp DM.

        Default-profile DMs (the long chat the human already has) must land
        in the assistant profile session as ordinary transcript rows, not a
        fresh ticket. The current goal still pins the active mission.
        """
        from hermes_state import SessionDB

        root = tmp_path
        default_db = SessionDB(db_path=root / "state.db")
        default_db.create_session(
            "prior-dm",
            "whatsapp",
            session_key="agent:main:whatsapp:dm:61400000000",
            chat_id="61400000000@s.whatsapp.net",
            chat_type="dm",
            user_id="61400000000@s.whatsapp.net",
            display_name="Alex",
        )
        default_db.append_message("prior-dm", "user", "hey, this is alex")
        default_db.append_message("prior-dm", "assistant", "hey alex, what's up")
        default_db.append_message("prior-dm", "user", "remind me about picnic later")
        default_db.close()

        started = _start(missions_home, chat_name="Alex")
        assert started["ok"] is True

        assistant_db = SessionDB(db_path=root / "state.db")
        rows = assistant_db._conn.execute(
            "SELECT id, session_key, chat_id, chat_type FROM sessions "
            "WHERE session_key = ?",
            ("agent:assistant:whatsapp:dm:61400000000",),
        ).fetchall()
        assert len(rows) == 1
        sid, skey, chat_id, chat_type = rows[0]
        assert skey == "agent:assistant:whatsapp:dm:61400000000"
        assert chat_type == "dm"
        convo = assistant_db.get_messages_as_conversation(sid)
        texts = [m.get("content") for m in convo if m.get("role") in ("user", "assistant")]
        assert "hey, this is alex" in texts
        assert "hey alex, what's up" in texts
        assert "remind me about picnic later" in texts
        assistant_db.close()

    def test_second_mission_does_not_duplicate_seeded_history(self, missions_home, tmp_path):
        from hermes_state import SessionDB

        root = tmp_path
        default_db = SessionDB(db_path=root / "state.db")
        default_db.create_session(
            "prior-dm",
            "whatsapp",
            session_key="agent:main:whatsapp:dm:61400000000",
            chat_id="61400000000@s.whatsapp.net",
            chat_type="dm",
            user_id="61400000000@s.whatsapp.net",
        )
        default_db.append_message("prior-dm", "user", "old line")
        default_db.close()

        mid = _start(missions_home)["mission_id"]
        missions_home.handle_dispatch_agent(
            {"action": "complete", "mission_id": mid, "outcome": "done"}
        )
        _start(missions_home, goal="Second picnic")

        assistant_db = SessionDB(db_path=root / "state.db")
        sid = assistant_db._conn.execute(
            "SELECT id FROM sessions WHERE session_key = ?",
            ("agent:assistant:whatsapp:dm:61400000000",),
        ).fetchone()[0]
        texts = [
            m.get("content")
            for m in assistant_db.get_messages_as_conversation(sid)
            if m.get("role") == "user"
        ]
        assert texts.count("old line") == 1
        assistant_db.close()


class TestMissionPairingLifecycle:
    """DM missions pre-approve/revoke the serving profile's pairing store."""

    def _assistant_store(self):
        from gateway.pairing import PairingStore

        return PairingStore(profile="assistant")

    def _write_lid_mapping(self, tmp_path, phone: str, lid: str):
        session_dir = tmp_path / "platforms" / "whatsapp" / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / f"lid-mapping-{phone}.json").write_text(
            json.dumps(f"{lid}@lid"), encoding="utf-8"
        )

    def test_dm_mission_grants_and_revokes_all_alias_forms(
        self, missions_home, tmp_path
    ):
        self._write_lid_mapping(tmp_path, "61400000000", "999999999999999")
        store = self._assistant_store()

        _start(missions_home, chat_id="61400000000")

        # Grant covers phone, JID, and LID forms of the same contact.
        assert store.is_approved("whatsapp", "61400000000") is True
        assert store.is_approved("whatsapp", "61400000000@s.whatsapp.net") is True
        assert store.is_approved("whatsapp", "999999999999999@lid") is True

        mid = missions_home.find_active_mission_for_chat(
            "61400000000@s.whatsapp.net"
        )["mission_id"]
        done = json.loads(
            missions_home.handle_dispatch_agent(
                {"action": "complete", "mission_id": mid, "outcome": "done"}
            )
        )
        assert done["ok"] is True

        # Alias-complete revoke: every form the grant covered is gone.
        assert store.is_approved("whatsapp", "61400000000") is False
        assert store.is_approved("whatsapp", "61400000000@s.whatsapp.net") is False
        assert store.is_approved("whatsapp", "999999999999999@lid") is False
        assert store.is_approved("whatsapp", "61400000000:47@s.whatsapp.net") is False


class TestGroupMissions:
    GROUP = "120363024955757999@g.us"
    MEMBER = "61400000000@s.whatsapp.net"

    def test_group_start_infers_and_stores_chat_type_group(self, missions_home):
        r = _start(missions_home, chat_id=self.GROUP, chat_name="Picnic Crew")
        assert r["ok"] is True
        assert r["chat_type"] == "group"
        m = missions_home.find_active_group_mission(self.GROUP)
        assert m is not None
        assert m["chat_type"] == "group"
        assert m["status"] == "active"
        # The generic lookup resolves group ids to group missions too.
        assert missions_home.find_active_mission_for_chat(self.GROUP)["mission_id"] == m["mission_id"]

    def test_dm_start_stores_chat_type_dm(self, missions_home):
        r = _start(missions_home)
        assert r["chat_type"] == "dm"
        m = missions_home.find_active_mission_for_chat("61400000000@s.whatsapp.net")
        assert m["chat_type"] == "dm"
        assert missions_home.find_active_group_mission("61400000000@s.whatsapp.net") is None

    def test_group_match_is_exact_not_canonical(self, missions_home):
        """No alias/canonical matching for groups — the @g.us domain is the id."""
        _start(missions_home, chat_id=self.GROUP)
        # canonical_whatsapp_identifier strips @g.us; that bare form must NOT match.
        assert missions_home.find_active_group_mission("120363024955757999") is None
        assert missions_home.find_active_mission_for_chat("120363024955757999") is None
        # A different group never matches.
        assert missions_home.find_active_group_mission("111111111111111111@g.us") is None

    def test_one_active_mission_per_exact_group_chat(self, missions_home):
        _start(missions_home, chat_id=self.GROUP)
        dup = json.loads(
            missions_home.handle_dispatch_agent(
                {"action": "start", "chat_id": self.GROUP, "goal": "again"}
            )
        )
        assert dup["ok"] is False
        assert dup["error"] == "mission_exists"

    def test_group_mission_and_member_dm_mission_coexist(self, missions_home):
        """A member's DM mission and the group's mission are different chats."""
        assert _start(missions_home, chat_id=self.MEMBER)["ok"] is True
        assert _start(missions_home, chat_id=self.GROUP)["ok"] is True
        assert missions_home.find_active_mission_for_chat(self.MEMBER)["chat_type"] == "dm"
        assert missions_home.find_active_group_mission(self.GROUP) is not None

    def test_group_start_skips_pairing_grant(self, missions_home):
        from gateway.pairing import PairingStore

        _start(missions_home, chat_id=self.GROUP, chat_name="Picnic Crew")
        store = PairingStore(profile="assistant")
        # Neither the group chat id nor any participant appears in the DM
        # pairing store.
        assert store.is_approved("whatsapp", self.GROUP) is False
        assert store.is_approved("whatsapp", self.MEMBER) is False
        assert store.list_approved("whatsapp") == []

    def test_group_start_skips_member_dm_history_seed(self, missions_home, tmp_path):
        """Group missions never copy a member's DM history."""
        from hermes_state import SessionDB

        default_db = SessionDB(db_path=tmp_path / "state.db")
        default_db.create_session(
            "prior-dm",
            "whatsapp",
            session_key="agent:main:whatsapp:dm:61400000000",
            chat_id=self.MEMBER,
            chat_type="dm",
            user_id=self.MEMBER,
        )
        default_db.append_message("prior-dm", "user", "private dm line")
        default_db.close()

        assert _start(missions_home, chat_id=self.GROUP)["ok"] is True

        db = SessionDB(db_path=tmp_path / "state.db")
        rows = db._conn.execute(
            "SELECT id FROM sessions WHERE session_key IN (?, ?)",
            (
                "agent:assistant:whatsapp:group:" + self.GROUP,
                "agent:assistant:whatsapp:dm:61400000000",
            ),
        ).fetchall()
        assert rows == []
        assert db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 'prior-dm'"
        ).fetchone()[0] == 1
        db.close()

    def test_group_end_session_parses_group_key_and_queues_origin(
        self, missions_home, origin_queue
    ):
        start = _start_origin(missions_home, chat_id=self.GROUP, chat_name="Picnic Crew")
        assert start["chat_type"] == "group"

        # The shared group session key end_session recovers the chat id from.
        group_session_key = f"agent:assistant:whatsapp:group:{self.GROUP}"
        done = json.loads(
            missions_home.handle_end_session(
                {"outcome": "Picnic arranged for Saturday"},
                session_key=group_session_key,
            )
        )
        assert done["ok"] is True
        assert done["status"] == "completed"
        assert done["mission_id"] == start["mission_id"]
        assert done["notified"] is True

        # Admission is gone immediately — the store is read live.
        assert missions_home.find_active_group_mission(self.GROUP) is None
        assert missions_home.find_active_mission_for_chat(self.GROUP) is None

        assert len(origin_queue.items) == 1
        evt = origin_queue.items[0]
        assert evt["type"] == "async_delegation"
        assert evt["session_key"] == "agent:main:discord:thread:abc:abc"
        assert evt["parent_session_id"] == "sess-1"
        assert "Picnic arranged for Saturday" in evt["summary"]

    def test_group_close_by_chat_id(self, missions_home):
        _start(missions_home, chat_id=self.GROUP)
        done = json.loads(
            missions_home.handle_dispatch_agent(
                {"action": "cancel", "chat_id": self.GROUP}
            )
        )
        assert done["ok"] is True
        assert missions_home.find_active_group_mission(self.GROUP) is None
