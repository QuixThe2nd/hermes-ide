"""Mission handoffs follow the serving profile in the trusted session key."""

import json

import pytest


PHONE = "61403152515"
PHONE_JID = f"{PHONE}@s.whatsapp.net"
LID = "259291571294369"
LID_JID = f"{LID}@lid"
WINNIE_DM_KEY = f"agent:winnie:whatsapp:dm:{PHONE}"
ASSISTANT_DM_KEY = f"agent:assistant:whatsapp:dm:{PHONE}"
MAIN_DM_KEY = f"agent:main:whatsapp:dm:{PHONE}"


@pytest.fixture()
def missions_plugin(tmp_path, monkeypatch):
    """Load the bundled plugin through discovery against an isolated home."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_MISSION_PROFILE", raising=False)

    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins["missions"]
    assert loaded.enabled is True
    assert loaded.error is None
    yield loaded.module
    manager.unload()


def _start_mission(pm, *, chat_id=PHONE_JID, profile="winnie"):
    started = json.loads(
        pm.handle_dispatch_agent(
            {
                "action": "start",
                "chat_id": chat_id,
                "goal": "Arrange Saturday lunch",
                "profile": profile,
            }
        )
    )
    assert started["ok"] is True, started
    mission = pm.find_active_mission_for_chat(chat_id)
    assert mission is not None
    return mission


def test_active_winnie_mission_exposes_end_session(missions_plugin):
    _start_mission(missions_plugin)

    assert missions_plugin.resolve_handoff_tool_for_session(WINNIE_DM_KEY) == "end_session"


def test_default_mission_uses_gateway_main_namespace(missions_plugin):
    from gateway.session import Platform, SessionSource, build_session_key

    _start_mission(missions_plugin, profile="Default")
    source = SessionSource(
        platform=Platform.WHATSAPP, chat_id=PHONE_JID, chat_type="dm"
    )
    gateway_key = build_session_key(source, profile="default")

    assert gateway_key == MAIN_DM_KEY
    assert missions_plugin.resolve_handoff_tool_for_session(gateway_key) == "end_session"


def test_configured_winnie_profile_exposes_escalate_without_mission(
    missions_plugin, monkeypatch
):
    monkeypatch.setenv("HERMES_MISSION_PROFILE", "winnie")

    assert missions_plugin.resolve_handoff_tool_for_session(WINNIE_DM_KEY) == "escalate_task"

    import tools.bot_mode_dm as dm

    deliveries = []
    monkeypatch.setattr(
        dm,
        "start_one_way_handoff",
        lambda content, **kwargs: deliveries.append((content, kwargs)) or {"ok": True},
    )
    escalated = json.loads(
        missions_plugin.handle_escalate_task(
            {"summary": "Need a decision", "requested_action": "Review the booking"},
            session_key=WINNIE_DM_KEY,
        )
    )
    assert escalated["ok"] is True, escalated
    envelope = json.loads(
        deliveries[0][0][len(missions_plugin._ESCALATION_INSTRUCTIONS) :]
    )
    assert envelope["metadata"]["source_profile"] == "winnie"


def test_trusted_winnie_key_completes_only_winnie_mission(missions_plugin):
    mission = _start_mission(missions_plugin)

    wrong_profile = json.loads(
        missions_plugin.handle_end_session(
            {"outcome": "must not close"}, session_key=ASSISTANT_DM_KEY
        )
    )
    assert wrong_profile["ok"] is False
    assert wrong_profile["error"] == "not_available"
    assert missions_plugin.find_active_mission_for_chat(PHONE_JID) is not None

    completed = json.loads(
        missions_plugin.handle_end_session(
            {"outcome": "Saturday 11am"}, session_key=WINNIE_DM_KEY
        )
    )
    assert completed["ok"] is True, completed
    assert completed["mission_id"] == mission["mission_id"]
    assert completed["status"] == "completed"
    assert missions_plugin.find_active_mission_for_chat(PHONE_JID) is None
    assert missions_plugin._load_mission(mission["mission_id"])["status"] == "completed"


def test_lid_inbound_uses_phone_bound_winnie_mission(missions_plugin, tmp_path):
    from gateway.config import Platform
    from gateway.session import SessionSource, build_session_key
    from gateway.whatsapp_identity import expand_whatsapp_aliases

    session_dir = tmp_path / "platforms" / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / f"lid-mapping-{LID}_reverse.json").write_text(
        json.dumps(PHONE), encoding="utf-8"
    )
    _start_mission(missions_plugin)

    assert expand_whatsapp_aliases(LID_JID) == {LID, PHONE}
    source = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id=LID_JID,
        chat_type="dm",
        user_id=LID_JID,
    )
    gateway_key = build_session_key(source, profile="winnie")
    assert gateway_key == WINNIE_DM_KEY
    assert missions_plugin.resolve_handoff_tool_for_session(gateway_key) == "end_session"
    assert (
        missions_plugin.resolve_handoff_tool_for_session(
            f"agent:winnie:whatsapp:dm:{LID}"
        )
        == "end_session"
    )


def test_legacy_assistant_profile_still_works_without_env(missions_plugin):
    # HERMES_MISSION_PROFILE is unset in the fixture: the hardcoded
    # "assistant" namespace must keep behaving exactly as before.
    assert missions_plugin._default_mission_profile() == "assistant"

    assert (
        missions_plugin.resolve_handoff_tool_for_session(ASSISTANT_DM_KEY)
        == "escalate_task"
    )

    mission = _start_mission(missions_plugin, profile="assistant")
    assert mission["profile"] == "assistant"
    assert (
        missions_plugin.resolve_handoff_tool_for_session(ASSISTANT_DM_KEY)
        == "end_session"
    )
    completed = json.loads(
        missions_plugin.handle_end_session(
            {"outcome": "legacy path still closes"}, session_key=ASSISTANT_DM_KEY
        )
    )
    assert completed["ok"] is True, completed
    assert (
        missions_plugin.resolve_handoff_tool_for_session(ASSISTANT_DM_KEY)
        == "escalate_task"
    )


def test_unconfigured_main_profile_gets_neither_handoff(missions_plugin, monkeypatch):
    monkeypatch.setenv("HERMES_MISSION_PROFILE", "winnie")
    mission = _start_mission(missions_plugin)

    assert missions_plugin.resolve_handoff_tool_for_session(MAIN_DM_KEY) is None
    rejected_close = json.loads(
        missions_plugin.handle_end_session(
            {"outcome": "must not close"}, session_key=MAIN_DM_KEY
        )
    )
    assert rejected_close["error"] == "not_available"
    rejected_escalation = json.loads(
        missions_plugin.handle_escalate_task(
            {"summary": "s", "requested_action": "a"}, session_key=MAIN_DM_KEY
        )
    )
    assert rejected_escalation["error"] == "not_available"
    assert missions_plugin.find_active_mission_for_chat(PHONE_JID) is not None

    closed = json.loads(
        missions_plugin.handle_dispatch_agent(
            {
                "action": "complete",
                "mission_id": mission["mission_id"],
                "outcome": "done",
            }
        )
    )
    assert closed["ok"] is True
    assert missions_plugin.resolve_handoff_tool_for_session(MAIN_DM_KEY) is None


def test_history_seed_uses_winnie_session_key(missions_plugin, tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(
        "prior-dm",
        "whatsapp",
        session_key=MAIN_DM_KEY,
        chat_id=PHONE_JID,
        chat_type="dm",
        user_id=PHONE_JID,
        display_name="Alex",
    )
    db.append_message("prior-dm", "user", "Can we arrange lunch?")
    db.append_message("prior-dm", "assistant", "I will check the calendar.")
    db.close()

    _start_mission(missions_plugin)

    seeded_db = SessionDB(db_path=tmp_path / "state.db")
    seeded = seeded_db.find_latest_gateway_session_for_peer(
        source="whatsapp", session_key=WINNIE_DM_KEY
    )
    assert seeded is not None
    messages = seeded_db.get_messages_as_conversation(str(seeded["id"]))
    texts = [message.get("content") for message in messages]
    assert "Can we arrange lunch?" in texts
    assert "I will check the calendar." in texts
    seeded_db.close()
