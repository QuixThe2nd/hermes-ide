"""Classic-CLI busy-path dispatch for slash commands typed mid-run.

While the agent runs, the process loop is blocked inside ``self.chat()``, so
commands that must act mid-run are dispatched inline on the UI thread
(``_tui_enter_inline_command`` → ``process_command``). The inline set is the
registry's ``busy_policy="dispatch"`` commands — /bg and /btw. /steer was
retired (55d9ace19a5) and must not regain inline treatment: no detector hit,
no ``agent.steer()``, no mid-run injection. Anything else typed mid-run is
routed by ``_tui_handle_enter``: plain text goes to redirect/interrupt, while
slash input (local dispatch) queues through ``_pending_input`` and runs via
``process_command`` once the turn finishes.

These tests exercise the detectors and the routing without starting a
prompt_toolkit app.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli import HermesCLI


def _make_cli():
    c = HermesCLI.__new__(HermesCLI)
    c.config = {}
    c._agent_running = False
    c.agent = MagicMock()
    c._pending_input = MagicMock()
    c._interrupt_queue = MagicMock()
    c._attached_images = []
    c._tui_multiline_shortcuts = False
    c._tui_last_text_change = 0.0
    return c


def _enter_event(text):
    buf = SimpleNamespace(
        text=text, cursor_position=len(text), insert_text=MagicMock(), reset=MagicMock())
    return SimpleNamespace(app=SimpleNamespace(current_buffer=buf, invalidate=MagicMock()))


def _submit(c, text):
    with patch.object(HermesCLI, "_tui_enter_overlay", return_value=False), \
            patch.object(HermesCLI, "_inline_pastes"):
        HermesCLI._tui_handle_enter(c, _enter_event(text))


class TestBusyInlineDetector:
    """_should_handle_background_command_inline gates the busy-path fast dispatch."""

    def test_detects_background_commands_when_agent_running(self):
        c = _make_cli()
        c._agent_running = True
        assert c._should_handle_background_command_inline("/bg npm test") is True
        assert c._should_handle_background_command_inline("/btw check the logs") is True

    def test_ignores_background_commands_when_agent_idle(self):
        """Idle input falls through to the normal process_loop dispatch."""
        c = _make_cli()
        assert c._should_handle_background_command_inline("/bg npm test") is False

    def test_ignores_non_slash_input_and_attached_images(self):
        c = _make_cli()
        c._agent_running = True
        assert c._should_handle_background_command_inline("bg without slash") is False
        assert c._should_handle_background_command_inline("") is False
        # Image payloads take the normal path; /bg does not accept images.
        assert c._should_handle_background_command_inline("/bg text", has_images=True) is False

    def test_ignores_other_slash_commands(self):
        c = _make_cli()
        c._agent_running = True
        assert c._should_handle_background_command_inline("/queue hello") is False
        assert c._should_handle_background_command_inline("/stop") is False
        assert c._should_handle_background_command_inline("/help") is False

    def test_retired_steer_gets_no_inline_treatment(self):
        """/steer is gone from the registry (55d9ace19a5), so both busy-path
        detectors refuse it even while the agent runs."""
        c = _make_cli()
        c._agent_running = True
        assert c._should_handle_steer_command_inline("/steer focus on error handling") is False
        assert c._should_handle_background_command_inline("/steer focus on error handling") is False


class TestBusyPathDispatch:
    def test_inline_dispatch_runs_handler_immediately(self):
        """With _agent_running=True, /bg dispatches inline on the UI thread: the
        busy-path handler runs right away and nothing is queued."""
        c = _make_cli()
        c._agent_running = True
        with patch.object(HermesCLI, "_handle_background_command") as handler:
            assert c.process_command("/bg npm test") is True
        handler.assert_called_once_with("/bg npm test")
        c._pending_input.put.assert_not_called()

    def test_retired_steer_dispatches_as_unregistered(self):
        """/steer mid-run reaches no core handler: no agent.steer(), no queue
        mutation — the foreground turn is left alone and the unknown-command
        notice is the only effect."""
        c = _make_cli()
        c._agent_running = True
        assert c.process_command("/steer focus on errors") is True
        c.agent.steer.assert_not_called()
        c._pending_input.put.assert_not_called()
        c._interrupt_queue.put.assert_not_called()

    def test_enter_routes_supported_command_inline(self):
        """_tui_handle_enter dispatches a busy_policy=dispatch command typed
        mid-run through _tui_enter_inline_command instead of queueing it."""
        c = _make_cli()
        c._agent_running = True
        with patch.object(
                HermesCLI, "_should_handle_background_command_inline", return_value=True), \
                patch.object(HermesCLI, "process_command") as dispatch:
            _submit(c, "/bg npm test")
        dispatch.assert_called_once_with("/bg npm test")
        c._pending_input.put.assert_not_called()

    def test_enter_while_busy_redirects_plain_text(self):
        """Plain text typed while the agent runs goes to the redirect path
        (active-turn redirect when the agent supports it), never the queue."""
        c = _make_cli()
        c._agent_running = True
        c.agent._supports_active_turn_redirect = True
        c.agent.redirect = MagicMock(return_value=True)
        _submit(c, "a follow-up question")
        c.agent.redirect.assert_called_once_with("a follow-up question")
        c._pending_input.put.assert_not_called()
        c._interrupt_queue.put.assert_not_called()

    def test_enter_queues_unavailable_slash_for_next_turn(self):
        """An unavailable slash typed mid-run is local dispatch: it queues in
        _pending_input (drained to process_command after the turn) and is never
        redirected into the running turn as a prompt."""
        c = _make_cli()
        c._agent_running = True
        c.agent._supports_active_turn_redirect = True
        c.agent.redirect = MagicMock(return_value=True)
        _submit(c, "/steer would-be-next-turn")
        c._pending_input.put.assert_called_once_with("/steer would-be-next-turn")
        c.agent.redirect.assert_not_called()

    def test_enter_queues_plain_text_when_idle(self):
        """Control — idle input takes the ordinary next-turn queue."""
        c = _make_cli()
        _submit(c, "hello there")
        c._pending_input.put.assert_called_once_with("hello there")
        c.agent.redirect.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
