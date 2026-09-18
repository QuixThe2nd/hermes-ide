"""prompt.submit writes the user's message at send time, and the turn that follows adopts that row instead of
writing a second one (#111868: a Desktop freeze during a slow first agent build left a session row with no message)."""

from types import SimpleNamespace

from agent.turn_context import _stage_turn_user_message
from hermes_state import SessionDB
from run_agent import AIAgent
from tui_gateway import server


def _desktop_session(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    resp = server.handle_request({"id": "c", "method": "session.create", "params": {"cols": 96, "source": "desktop"}})
    assert "result" in resp, resp
    return resp["result"]["session_id"], resp["result"]["stored_session_id"]


def _flush_agent(db, key):
    """Agent shell owning the real flush (the crash persist at turn start runs this same code)."""
    agent = SimpleNamespace(
        _session_db=db, _session_db_created=True, _persist_disabled=False, session_id=key,
        _session_persist_lock=None, _flushed_db_message_ids=set(), _flushed_db_message_session_id=None,
        _last_flushed_db_idx=0, _persist_user_message_idx=None, _persist_user_message_override=None,
        _persist_user_message_timestamp=None, _pending_cli_user_message=None)
    agent._ensure_db_session = lambda: None
    agent._flush_messages_to_session_db = AIAgent._flush_messages_to_session_db.__get__(agent, AIAgent)
    agent._flush_messages_to_session_db_unlocked = AIAgent._flush_messages_to_session_db_unlocked.__get__(agent, AIAgent)
    return agent


def test_user_message_is_durable_at_submit_before_any_agent_turn(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    try:
        with session["history_lock"]:
            session["running"] = True
            server._start_inflight_turn(session, "please refactor the login page")
        assert server._persist_session_row_for_submit("rid", session, "please refactor the login page", None) is None
        # The agent build has not even started: the transcript already resumes with the sent message.
        assert [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] == [
            ("user", "please refactor the login page")]
        assert db.get_session(key)["message_count"] == 1
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_turn_adopts_the_submit_row_and_writes_no_duplicate(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    try:
        with session["history_lock"]:
            session["running"] = True
            server._start_inflight_turn(session, "look at @notes.md")
        assert server._persist_session_row_for_submit("rid", session, "look at @notes.md", None) is None
        agent = _flush_agent(db, key)
        # The prologue rewrote the persisted prompt (@-expansion): the early row follows it.
        expanded = "look at @notes.md\n\n<file notes.md>todo</file>"
        server._adopt_submit_user_row(session, agent, expanded)
        assert "_submit_user_row" not in session
        user_msg, _pending = _stage_turn_user_message(agent, expanded, expanded, None, None, None, None)
        assert user_msg is agent._pending_cli_user_message  # adopted by identity, not rebuilt
        messages = [user_msg]
        agent._persist_user_message_idx = 0
        agent._flush_messages_to_session_db(messages, [])  # the turn-start crash persist
        agent._flush_messages_to_session_db(messages + [{"role": "assistant", "content": "done"}], [])  # turn end
        rows = db.get_messages_as_conversation(key, include_inactive=True)
        assert [(r["role"], r["content"]) for r in rows] == [("user", expanded), ("assistant", "done")]
    finally:
        server._sessions.pop(sid, None)
        db.close()
