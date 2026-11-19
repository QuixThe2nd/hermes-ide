"""steer_subagent — redirecting a live delegated child without stopping it.

Registry-level coverage for the delegation-side mirror of
interrupt_subagent(): text reaches the live child's AIAgent.steer(), and
every failure shape (unknown id, dead record, empty text, a steer that
raises) degrades to False instead of an exception.  Also covers the
missed-steer retention race (a child that finishes before the drain) and
the subagent.steer gateway RPC that fronts the helper.
"""

import threading
from unittest.mock import MagicMock

from tools.delegate_tool import (
    _register_subagent,
    _unregister_subagent,
    steer_subagent,
)


class _StubAgent:
    def __init__(self, accept: bool = True, boom: bool = False):
        self.accept = accept
        self.boom = boom
        self.steered: list[str] = []

    def steer(self, text: str) -> bool:
        if self.boom:
            raise RuntimeError("steer exploded")
        self.steered.append(text)
        return self.accept


def _with_registered(
    sid: str,
    agent,
    *,
    owner_session_id: str | None = None,
    owner_transport=None,
    owner_session_record=None,
) -> None:
    _register_subagent(
        {
            "subagent_id": sid,
            "parent_id": "root",
            "depth": 1,
            "goal": "test goal",
            "status": "running",
            "agent": agent,
            "owner_session_id": owner_session_id,
            "owner_transport": owner_transport,
            "owner_session_record": owner_session_record,
        }
    )


def test_steer_reaches_the_live_child():
    agent = _StubAgent()
    _with_registered("sid-steer-1", agent)
    try:
        assert steer_subagent("sid-steer-1", "focus on pricing instead") is True
        assert agent.steered == ["focus on pricing instead"]
    finally:
        _unregister_subagent("sid-steer-1")


def test_unknown_subagent_is_false_not_an_error():
    assert steer_subagent("sid-not-registered", "hello") is False


def test_empty_text_is_refused_without_a_lookup():
    agent = _StubAgent()
    _with_registered("sid-steer-2", agent)
    try:
        assert steer_subagent("sid-steer-2", "   ") is False
        assert agent.steered == []
    finally:
        _unregister_subagent("sid-steer-2")


def test_record_without_live_agent_is_false():
    _register_subagent({"subagent_id": "sid-steer-3", "status": "running", "agent": None})
    try:
        assert steer_subagent("sid-steer-3", "hello") is False
    finally:
        _unregister_subagent("sid-steer-3")


def test_agent_rejection_propagates_as_false():
    agent = _StubAgent(accept=False)
    _with_registered("sid-steer-4", agent)
    try:
        assert steer_subagent("sid-steer-4", "hello") is False
    finally:
        _unregister_subagent("sid-steer-4")


def test_exception_in_steer_degrades_to_false():
    agent = _StubAgent(boom=True)
    _with_registered("sid-steer-5", agent)
    try:
        assert steer_subagent("sid-steer-5", "hello") is False
    finally:
        _unregister_subagent("sid-steer-5")


def test_stale_agent_teardown_cannot_unregister_recycled_id():
    old_agent = _StubAgent()
    replacement = _StubAgent()
    _with_registered("sid-recycled-teardown", old_agent, owner_session_id="old-owner")
    _with_registered("sid-recycled-teardown", replacement, owner_session_id="new-owner")
    try:
        _unregister_subagent("sid-recycled-teardown", agent=old_agent)
        assert (
            steer_subagent(
                "sid-recycled-teardown",
                "replacement remains live",
            )
            is True
        )
        assert old_agent.steered == []
        assert replacement.steered == ["replacement remains live"]
    finally:
        _unregister_subagent("sid-recycled-teardown", agent=replacement)


def test_status_snapshot_never_leaks_owner_or_lifecycle_metadata():
    from tools.delegate_tool import list_active_subagents

    agent = _StubAgent()
    owner_transport = object()
    owner_session_record = {"session_key": "private-owner"}
    _with_registered(
        "sid-private-metadata",
        agent,
        owner_session_id="private-owner",
        owner_transport=owner_transport,
        owner_session_record=owner_session_record,
    )
    try:
        snapshot = next(
            item
            for item in list_active_subagents()
            if item["subagent_id"] == "sid-private-metadata"
        )
        assert snapshot["status"] == "running"
        assert "agent" not in snapshot
        assert "owner_session_id" not in snapshot
        assert "owner_transport" not in snapshot
        assert "owner_session_record" not in snapshot
        assert "accepting_steer" not in snapshot
        assert "private-owner" not in repr(snapshot)
        assert all(value is not owner_transport for value in snapshot.values())
        assert all(value is not owner_session_record for value in snapshot.values())
    finally:
        _unregister_subagent("sid-private-metadata", agent=agent)


class TestMissedSteerRetention:
    """The final-answer race: a steer with no boundary left is NAMED, not lost."""

    def test_pending_steer_lands_in_completion_entry(self):
        import json
        from unittest.mock import MagicMock, patch

        from tools.delegate_tool import delegate_agent

        parent = MagicMock()
        parent._delegate_depth = 0
        parent._session_db = None
        parent.model = "test-model"
        parent.interactive_mode = False

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "test-model"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [],
                # The finalizer's undelivered-steer hand-back
                # (turn_finalizer.py "pending_steer").
                "pending_steer": "focus on pricing instead",
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_agent(goal="race test", parent_agent=parent, background=False))
            entry = result["results"][0]

        assert entry["missed_steer"] == "focus on pricing instead"
        assert "steer did not land" in entry["summary"]
        assert "focus on pricing instead" in entry["summary"]
        # The race must not corrupt the outcome of the work itself.
        assert entry["status"] == "completed"

    def test_no_pending_steer_leaves_entry_untouched(self):
        import json
        from unittest.mock import MagicMock, patch

        from tools.delegate_tool import delegate_agent

        parent = MagicMock()
        parent._delegate_depth = 0
        parent._session_db = None
        parent.model = "test-model"
        parent.interactive_mode = False

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "test-model"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_agent(goal="clean run", parent_agent=parent, background=False))
            entry = result["results"][0]

        assert "missed_steer" not in entry
        assert "steer did not land" not in entry["summary"]

    def test_accepted_steer_racing_completion_is_durably_retained(self):
        """Acceptance wins the registry race, so completion must retain its text."""
        from tools.delegate_tool import _run_single_child

        running = threading.Event()
        allow_return = threading.Event()
        steer_entered = threading.Event()
        allow_steer = threading.Event()
        pending: list[str] = []

        child = MagicMock()
        child._subagent_id = "sid-linearized-accept"
        child._delegate_depth = 1
        child.model = "test-model"
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0

        def run_conversation(**_kwargs):
            running.set()
            assert allow_return.wait(5)
            return {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [],
            }

        def steer(text: str) -> bool:
            steer_entered.set()
            assert allow_steer.wait(5)
            pending.append(text)
            return True

        def drain():
            if not pending:
                return None
            text = "\n".join(pending)
            pending.clear()
            return text

        child.run_conversation.side_effect = run_conversation
        child.steer.side_effect = steer
        child._drain_pending_steer.side_effect = drain
        parent = MagicMock()
        parent._session_db = None

        result_box: dict = {}
        runner = threading.Thread(
            target=lambda: result_box.setdefault(
                "result",
                _run_single_child(0, "race", child=child, parent_agent=parent),
            )
        )
        runner.start()
        assert running.wait(5)

        accepted_box: dict = {}
        steering = threading.Thread(
            target=lambda: accepted_box.setdefault(
                "accepted", steer_subagent(child._subagent_id, "retain this exact text")
            )
        )
        steering.start()
        assert steer_entered.wait(5)
        allow_return.set()
        allow_steer.set()
        steering.join(5)
        runner.join(5)

        assert not steering.is_alive()
        assert not runner.is_alive()
        assert accepted_box["accepted"] is True
        assert result_box["result"]["missed_steer"] == "retain this exact text"

    def test_steer_after_run_return_is_rejected_before_completion_callback(self):
        """Once the child returns, a blocked completion callback cannot extend acceptance."""
        from tools.delegate_tool import _run_single_child

        callback_entered = threading.Event()
        release_callback = threading.Event()

        def progress(_event: str, **_kwargs) -> None:
            return None

        def flush() -> None:
            callback_entered.set()
            assert release_callback.wait(5)

        progress._flush = flush  # type: ignore[attr-defined]
        child = MagicMock()
        child._subagent_id = "sid-closed-before-callback"
        child._delegate_depth = 1
        child.model = "test-model"
        child.tool_progress_callback = progress
        child.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        runner = threading.Thread(
            target=lambda: _run_single_child(
                0,
                "late",
                child=child,
                parent_agent=MagicMock(_session_db=None),
            )
        )
        runner.start()
        assert callback_entered.wait(5)
        try:
            assert steer_subagent(child._subagent_id, "too late") is False
            child.steer.assert_not_called()
        finally:
            release_callback.set()
            runner.join(5)
        assert not runner.is_alive()
