"""Tests for the plugin progress bridge behind ``display.tool_progress: plugin``.

The mode is opt-in and fail-open by design: with nothing registered every call is a no-op that
returns ``False``. These tests pin that contract plus the whole-body (``__body__``) absorb.
"""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from hermes_cli import progress_bridge


@pytest.fixture(autouse=True)
def _clean_bridge():
    """The bridge is process-global; never leak registrations between tests."""
    with progress_bridge._LOCK:  # noqa: SLF001 - test needs the module's own registry
        progress_bridge._BY_SESSION.clear()  # noqa: SLF001
        progress_bridge._LAST["queue"] = None  # noqa: SLF001
        progress_bridge._LAST["at"] = 0.0  # noqa: SLF001
    yield
    with progress_bridge._LOCK:  # noqa: SLF001
        progress_bridge._BY_SESSION.clear()  # noqa: SLF001
        progress_bridge._LAST["queue"] = None  # noqa: SLF001


def test_push_without_registration_is_a_noop_and_false():
    assert progress_bridge.push_progress("line") is False
    assert progress_bridge.resolve_queue() is None


def test_push_lands_in_the_registered_queue():
    q = queue.Queue()
    progress_bridge.register_progress_queue(queue=q, session_key="discord:1:2", session_id="sid-1")
    assert progress_bridge.push_progress("hello", session_id="sid-1") is True
    assert progress_bridge.push_progress("key-addressed", session_key="discord:1:2") is True
    assert q.get_nowait() == "hello"
    assert q.get_nowait() == "key-addressed"


def test_unregistered_session_falls_back_to_the_last_live_turn():
    """A hook without a session_id (or from a concurrent chat) must not silently drop lines."""
    q = queue.Queue()
    progress_bridge.register_progress_queue(queue=q, session_key="discord:1:2", session_id="sid-1")
    assert progress_bridge.push_progress("no session given") is True
    assert q.get_nowait() == "no session given"


def test_unregister_by_queue_identity_drops_every_alias():
    q = queue.Queue()
    progress_bridge.register_progress_queue(queue=q, session_key="discord:1:2", session_id="sid-1")
    progress_bridge.unregister_progress_queue(queue=q, session_key="discord:1:2")
    assert progress_bridge.active_sessions() == {}
    assert progress_bridge.push_progress("after the turn") is False


def test_register_ignores_a_missing_queue():
    progress_bridge.register_progress_queue(queue=None, session_id="sid")
    assert progress_bridge.active_sessions() == {}


def test_push_fails_open_when_the_queue_explodes():
    class Boom:
        def put_nowait(self, _item):  # pragma: no cover - exercised via push_progress
            raise RuntimeError("queue is broken")

    progress_bridge.register_progress_queue(queue=Boom(), session_id="sid")
    assert progress_bridge.push_progress("line", session_id="sid") is False


def test_body_marker_replaces_the_whole_bubble_instead_of_appending():
    from gateway.run_turn_runner import TurnRunner

    st = SimpleNamespace(progress_lines=["old header", "old footer"])
    rendered = TurnRunner.__new__(TurnRunner)._progress_absorb(  # noqa: SLF001
        st, ("__body__", "new header\nnew footer")
    )
    assert st.progress_lines == ["new header", "new footer"]
    assert rendered == "new footer"


def test_body_marker_with_empty_body_clears_the_bubble():
    from gateway.run_turn_runner import TurnRunner

    st = SimpleNamespace(progress_lines=["stale"])
    assert TurnRunner.__new__(TurnRunner)._progress_absorb(st, ("__body__", "")) == ""  # noqa: SLF001
    assert st.progress_lines == [""]


def test_plain_lines_still_append():
    from gateway.run_turn_runner import TurnRunner

    st = SimpleNamespace(progress_lines=[])
    assert TurnRunner.__new__(TurnRunner)._progress_absorb(st, "🛠 terminal") == "🛠 terminal"  # noqa: SLF001
    assert st.progress_lines == ["🛠 terminal"]
