"""Tests for the combined /reasoning command.

Covers both reasoning effort level management and reasoning display toggle,
plus the reasoning extraction and display pipeline from run_agent through CLI.

Combines functionality from:
- PR #789 (Aum08Desai): reasoning effort level management
- PR #790 (0xbyt4): reasoning display toggle and rendering
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import re


# ---------------------------------------------------------------------------
# Effort level parsing
# ---------------------------------------------------------------------------

class TestParseReasoningConfig(unittest.TestCase):
    """Verify _parse_reasoning_config handles all effort levels."""

    def _parse(self, effort):
        from cli import _parse_reasoning_config
        return _parse_reasoning_config(effort)

    def test_none_disables(self):
        result = self._parse("none")
        self.assertEqual(result, {"enabled": False})

    def test_valid_levels(self):
        for level in ("low", "medium", "high", "xhigh", "max", "ultra", "minimal"):
            result = self._parse(level)
            self.assertIsNotNone(result)
            self.assertTrue(result.get("enabled"))
            self.assertEqual(result["effort"], level)


    def test_unknown_returns_none(self):
        self.assertIsNone(self._parse("turbo"))



# ---------------------------------------------------------------------------
# reasoning config resolution (effort + display)
# ---------------------------------------------------------------------------

class TestReasoningConfig(unittest.TestCase):
    """Test reasoning-config resolution helpers."""

    def _make_cli(self, reasoning_config=None, show_reasoning=False):
        """Create a minimal CLI stub with the reasoning attributes."""
        stub = SimpleNamespace(
            reasoning_config=reasoning_config,
            show_reasoning=show_reasoning,
            agent=MagicMock(),
        )
        return stub

    def test_new_session_clears_session_reasoning_override(self):
        """/new and /clear must not carry a session-only effort override forward."""
        from cli import CLI_CONFIG, HermesCLI

        agent = SimpleNamespace(
            reasoning_config={"enabled": True, "effort": "high"},
            reset_session_state=MagicMock(),
        )
        stub = SimpleNamespace(
            agent=agent,
            conversation_history=[],
            session_id="old-session",
            _session_db=None,
            _pending_title=None,
            _resumed=False,
            reasoning_config={"enabled": True, "effort": "high"},
            _notify_session_boundary=MagicMock(),
        )

        with patch.dict(CLI_CONFIG.setdefault("agent", {}), {"reasoning_effort": "medium"}):
            HermesCLI.new_session(stub, silent=True)

        self.assertEqual(stub.reasoning_config, {"enabled": True, "effort": "medium"})
        self.assertEqual(agent.reasoning_config, {"enabled": True, "effort": "medium"})
        agent.reset_session_state.assert_called_once()

    def test_new_session_resets_service_tier_and_model_from_config(self):
        """/new re-derives service tier and model from config.yaml — session
        /fast and /model switches do not carry forward (#48055, #23131)."""
        from cli import CLI_CONFIG, HermesCLI

        agent = SimpleNamespace(
            reasoning_config=None,
            reset_session_state=MagicMock(),
            switch_model=MagicMock(),
        )
        stub = SimpleNamespace(
            agent=agent,
            conversation_history=[],
            session_id="old-session",
            _session_db=None,
            _pending_title=None,
            _resumed=False,
            reasoning_config=None,
            _notify_session_boundary=MagicMock(),
            # Session had switched to fast + a session-only model.
            service_tier="priority",
            _pending_one_turn_model_restore={"model": "stale"},
            model="session-switched-model",
            provider="openrouter",
            requested_provider="openrouter",
            api_key="k",
            base_url="",
            api_mode="",
        )

        fake_result = SimpleNamespace(
            success=True,
            new_model="config-default-model",
            target_provider="openrouter",
            api_key="k2",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
        )
        with patch.dict(
            CLI_CONFIG.setdefault("agent", {}),
            {"reasoning_effort": "medium", "service_tier": "normal"},
        ), patch.dict(
            CLI_CONFIG,
            {"model": {"default": "config-default-model", "provider": "openrouter"}},
        ), patch(
            "hermes_cli.model_switch.switch_model", return_value=fake_result
        ):
            HermesCLI.new_session(stub, silent=True)

        # Fast override cleared back to config default (normal → None).
        self.assertIsNone(stub.service_tier)
        # One-turn restore snapshot cleared.
        self.assertIsNone(stub._pending_one_turn_model_restore)
        # Model reset to the config default via the live agent swap.
        self.assertEqual(stub.model, "config-default-model")
        agent.switch_model.assert_called_once()


# ---------------------------------------------------------------------------
# Reasoning extraction and result dict
# ---------------------------------------------------------------------------

class TestLastReasoningInResult(unittest.TestCase):
    """Verify reasoning extraction from the messages list."""

    def _build_messages(self, reasoning=None):
        return [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "Hi there!",
                "reasoning": reasoning,
                "finish_reason": "stop",
            },
        ]

    def test_reasoning_present(self):
        messages = self._build_messages(reasoning="Let me think...")
        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break
        self.assertEqual(last_reasoning, "Let me think...")


    def test_picks_last_assistant(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "...", "reasoning": "first thought"},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "done!", "reasoning": "final thought"},
        ]
        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break
        self.assertEqual(last_reasoning, "final thought")



# ---------------------------------------------------------------------------
# Reasoning display collapse
# ---------------------------------------------------------------------------

class TestReasoningCollapse(unittest.TestCase):
    """Verify long reasoning is collapsed to 10 lines in the box."""

    def test_short_reasoning_not_collapsed(self):
        reasoning = "\n".join(f"Line {i}" for i in range(5))
        lines = reasoning.strip().splitlines()
        self.assertLessEqual(len(lines), 10)



    def test_intermediate_callback_collapses_to_5(self):
        """_on_reasoning shows max 5 lines."""
        reasoning = "\n".join(f"Step {i}" for i in range(12))
        lines = reasoning.strip().splitlines()
        if len(lines) > 5:
            preview = "\n".join(lines[:5])
            preview += f"\n  ... ({len(lines) - 5} more lines)"
        else:
            preview = reasoning.strip()
        preview_lines = preview.splitlines()
        self.assertEqual(len(preview_lines), 6)
        self.assertIn("7 more lines", preview_lines[-1])


# ---------------------------------------------------------------------------
# Reasoning callback
# ---------------------------------------------------------------------------

class TestReasoningCallback(unittest.TestCase):
    """Verify reasoning_callback invocation."""

    def test_callback_invoked_with_reasoning(self):
        captured = []
        agent = MagicMock()
        agent.reasoning_callback = lambda t: captured.append(t)
        agent._extract_reasoning = MagicMock(return_value="deep thought")

        reasoning_text = agent._extract_reasoning(MagicMock())
        if reasoning_text and agent.reasoning_callback:
            agent.reasoning_callback(reasoning_text)
        self.assertEqual(captured, ["deep thought"])


        # No exception = pass


class TestReasoningPreviewBuffering(unittest.TestCase):
    def _make_cli(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.verbose = True
        cli._spinner_text = ""
        cli._reasoning_preview_buf = ""
        cli._invalidate = lambda *args, **kwargs: None
        return cli

class TestEndToEndPipeline(unittest.TestCase):
    """Simulate the full pipeline: extraction -> result dict -> display."""

    def test_openrouter_claude_pipeline(self):
        from run_agent import AIAgent

        api_message = SimpleNamespace(
            role="assistant",
            content="Lists support append().",
            tool_calls=None,
            reasoning=None,
            reasoning_content=None,
            reasoning_details=[
                {"type": "reasoning.summary", "summary": "Python list methods."},
            ],
        )

        reasoning = AIAgent._extract_reasoning(None, api_message)
        self.assertIsNotNone(reasoning)

        messages = [
            {"role": "user", "content": "How do I add items?"},
            {"role": "assistant", "content": api_message.content, "reasoning": reasoning},
        ]

        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break

        result = {
            "final_response": api_message.content,
            "last_reasoning": last_reasoning,
        }

        self.assertIn("last_reasoning", result)
        self.assertIn("Python list methods", result["last_reasoning"])



# ---------------------------------------------------------------------------
# Duplicate reasoning box prevention (Bug fix: 3 boxes for 1 reasoning)
# ---------------------------------------------------------------------------

class TestReasoningDeltasFiredFlag(unittest.TestCase):
    """_build_assistant_message should not re-fire reasoning_callback when
    reasoning was already streamed via _fire_reasoning_delta."""

    def _make_agent(self):
        from run_agent import AIAgent
        agent = AIAgent.__new__(AIAgent)
        agent.reasoning_callback = None
        agent.stream_delta_callback = None
        agent._stream_callback = None
        agent.verbose_logging = False
        return agent

    def test_fire_reasoning_delta_calls_callback(self):
        agent = self._make_agent()
        captured = []
        agent.reasoning_callback = lambda t: captured.append(t)
        agent._fire_reasoning_delta("thinking...")
        self.assertEqual(captured, ["thinking..."])



    def test_build_assistant_message_fires_callback_without_streaming(self):
        """When no streaming is active, callback always fires for structured
        reasoning."""
        agent = self._make_agent()
        captured = []
        agent.reasoning_callback = lambda t: captured.append(t)
        # No streaming
        agent.stream_delta_callback = None

        msg = SimpleNamespace(
            content="I'll merge that.",
            tool_calls=None,
            reasoning_content="Let me merge the PR.",
            reasoning=None,
            reasoning_details=None,
        )
        agent._build_assistant_message(msg, "stop")

        self.assertEqual(captured, ["Let me merge the PR."])


class TestReasoningShownThisTurnFlag(unittest.TestCase):
    """Post-response reasoning display should be suppressed when reasoning
    was already shown during streaming in a tool-calling loop."""

    def _make_cli(self):
        from cli import HermesCLI
        cli = HermesCLI.__new__(HermesCLI)
        cli.show_reasoning = True
        cli.streaming_enabled = True
        cli._stream_box_opened = False
        cli._reasoning_box_opened = False
        cli._reasoning_stream_started = False
        cli._reasoning_shown_this_turn = False
        cli._reasoning_buf = ""
        cli._stream_buf = ""
        cli._stream_started = False
        cli._stream_text_ansi = ""
        cli._stream_prefilt = ""
        cli._in_reasoning_block = False
        cli._reasoning_preview_buf = ""
        return cli

    @patch("cli._cprint")
    def test_streaming_reasoning_sets_turn_flag(self, mock_cprint):
        cli = self._make_cli()
        self.assertFalse(cli._reasoning_shown_this_turn)
        cli._stream_reasoning_delta("Thinking about it...")
        self.assertTrue(cli._reasoning_shown_this_turn)


    @patch("cli._cprint")
    def test_turn_flag_cleared_before_new_turn(self, mock_cprint):
        """The turn flag should be reset at the start of a new user turn.
        This happens outside _reset_stream_state, at the call site."""
        cli = self._make_cli()
        cli._reasoning_shown_this_turn = True

        # Simulate new user turn setup
        cli._reset_stream_state()
        cli._reasoning_shown_this_turn = False  # done by process_input

        self.assertFalse(cli._reasoning_shown_this_turn)


if __name__ == "__main__":
    unittest.main()
