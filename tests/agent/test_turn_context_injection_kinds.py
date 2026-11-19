"""``context.injected`` payload kinds: producer→payload→formatter contract.

`build_turn_context` classifies the injections it composes (memory prefetch,
plugin context, gateway notes, surface-switch correction) and tags the
observability payload with ordered ``kinds``. The classification is pure
metadata: the API-bound bytes and the persisted transcript must be identical
with and without kind tagging.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.turn_context import build_turn_context
from tests.agent.test_gateway_turn_sidecar import _FakeAgent, _build

RESET_NOTE = (
    "[System note: The user's previous session expired due to inactivity. "
    "This is a fresh conversation with no prior context.]"
)
SURFACE_NOTE = "[Correction: this session is now served from the desktop app.]"
USER_MSG = "summarize the quarterly budget report"


def _memory_agent():
    agent = _FakeAgent()
    agent._memory_manager = SimpleNamespace(
        on_turn_start=lambda *a, **k: None,
        prefetch_all=lambda query, session_id=None: "budget fact",
    )
    return agent


def _capture(agent):
    payloads = []

    def _cb(topic, _kind, _progress, payload):
        if topic == "context.injected":
            payloads.append(payload)

    agent.tool_progress_callback = _cb
    return payloads


class TestInjectionPayloadKinds:
    def test_kinds_follow_composed_suffix_order(self):
        agent = _memory_agent()
        agent._gateway_turn_context_notes = RESET_NOTE
        agent._gateway_turn_note_kinds = ["session_reset"]
        agent._surface_switch_note = SURFACE_NOTE
        payloads = _capture(agent)
        with patch("hermes_cli.lifecycle.invoke_hook", return_value=["PLUGIN CONTEXT"]):
            build = _build(agent, user_message=USER_MSG)
        assert len(payloads) == 1
        assert payloads[0]["kinds"] == [
            "memory", "plugin", "session_reset", "surface_switch",
        ]
        assert payloads[0]["sources"] == ["memory", "plugin", "gateway"]
        assert payloads[0]["injected_chars"] == (
            len(build.messages[build.current_turn_user_idx]["api_content"])
            - len(build.messages[build.current_turn_user_idx]["content"])
        )

    def test_legacy_untagged_note_yields_unclassified_kind(self):
        agent = _FakeAgent()
        agent._gateway_turn_context_notes = RESET_NOTE
        payloads = _capture(agent)
        _build(agent, user_message=USER_MSG)
        assert len(payloads) == 1
        assert payloads[0]["kinds"] == [None]

    def test_memory_only_injection_is_classified_memory(self):
        agent = _memory_agent()
        payloads = _capture(agent)
        _build(agent, user_message=USER_MSG)
        assert len(payloads) == 1
        assert payloads[0]["kinds"] == ["memory"]
        assert payloads[0]["sources"] == ["memory"]

    def test_unclassifiable_injection_omits_the_kinds_key(self):
        # No memory, no plugin, no notes: nothing to classify, so the payload
        # stays byte-compatible with producers that predate ``kinds``.
        agent = _FakeAgent()
        payloads = _capture(agent)
        _build(agent, user_message=f"envelope note\n\n{USER_MSG}", persist_user_message=USER_MSG)
        assert len(payloads) == 1
        assert "kinds" not in payloads[0]
        assert payloads[0]["sources"] == ["gateway"]

    def test_gateway_note_kinds_channel_is_one_shot(self):
        agent = _FakeAgent()
        agent._gateway_turn_context_notes = RESET_NOTE
        agent._gateway_turn_note_kinds = ["session_reset"]
        _capture(agent)
        _build(agent, user_message=USER_MSG)
        assert agent._gateway_turn_context_notes == ""
        assert agent._gateway_turn_note_kinds == []


class TestKindTaggingNeverTouchesModelBytes:
    def test_api_and_persist_bytes_identical_with_and_without_kinds(self):
        def _run(with_kinds: bool):
            agent = _memory_agent()
            agent._gateway_turn_context_notes = RESET_NOTE
            if with_kinds:
                agent._gateway_turn_note_kinds = ["session_reset"]
            agent._surface_switch_note = SURFACE_NOTE
            payloads = _capture(agent)
            build = _build(agent, user_message=USER_MSG)
            msg = build.messages[build.current_turn_user_idx]
            return msg, payloads

        tagged_msg, tagged_payloads = _run(with_kinds=True)
        plain_msg, plain_payloads = _run(with_kinds=False)

        assert tagged_msg["content"] == plain_msg["content"]
        assert tagged_msg["api_content"] == plain_msg["api_content"]
        assert tagged_payloads[0]["content"] == plain_payloads[0]["content"]
        assert tagged_payloads[0]["injected_chars"] == plain_payloads[0]["injected_chars"]
        assert tagged_payloads[0]["kinds"] == ["memory", "session_reset", "surface_switch"]
        assert "kinds" not in plain_payloads[0] or plain_payloads[0]["kinds"] == [
            "memory", None, "surface_switch",
        ]
