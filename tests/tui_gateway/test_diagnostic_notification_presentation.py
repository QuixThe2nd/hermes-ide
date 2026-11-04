"""Diagnostic wake execution and structured controls survive a presentation veto."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.notification_presentation import notification_turn
from tui_gateway import server


@pytest.mark.parametrize("muted", [False, True])
def test_real_tui_emitter_keeps_control_frames_and_restores_callbacks(monkeypatch, muted):
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    callback = Mock()
    agent = SimpleNamespace(status_callback=callback, clarify_callback=callback)
    with notification_turn(agent, muted=muted, session_id="session"):
        server._emit("message.delta", "session", {"text": "diagnostic echoed by model"})
        server._emit("message.complete", "session", {"text": "diagnostic echoed by model"})
        agent.clarify_callback("question", ["choice"])
        server._emit("notification.clear", "session", {"key": "cleared"})
    assert [frame["params"]["type"] for frame in frames] == (
        ["notification.clear"] if muted else ["message.delta", "message.complete", "notification.clear"])
    assert agent.status_callback is callback
    callback.assert_called_once_with("question", ["choice"])
    server._emit("message.delta", "session", {"text": "next human result"})
    assert frames[-1]["params"]["payload"]["text"] == "next human result"
