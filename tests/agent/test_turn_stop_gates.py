"""Stop-gate contracts for text-response turn endings.

First gate: a model that ends the turn by echoing the restart tool result —
the rich ``Restarted successfully / Old process / New process`` notice the
successor process rewrites into the transcript (PR #667) — is quoting
context, not answering. The echo must be dropped (not emitted, not
persisted, not a budget fallback) and the turn continues once via a
synthetic user nudge after the tool result, the same legal tool→user
boundary /steer uses. A second echo ends the turn as the answer: no loop.
"""

from __future__ import annotations

import pytest

from agent.turn_stop_gates import apply_stop_gates


@pytest.fixture(autouse=True)
def _no_verify_on_stop(monkeypatch):
    """Keep the env-gated verify-on-stop gate out of these tests."""
    monkeypatch.delenv("HERMES_VERIFY_ON_STOP", raising=False)


class _FakeAgent:
    """Records emissions/flushes so a leak (echo reaching the user or the
    transcript) fails the test instead of silently passing."""

    def __init__(self):
        self.emitted = []
        self.flushes = []
        self._session_messages = None

    def _emit_interim_assistant_message(self, msg):
        self.emitted.append(msg)

    def _flush_messages_to_session_db(self, messages, history):
        self.flushes.append(list(messages))

    def _interim_content_was_streamed(self, text):
        return False


_OLD = "pid 3376901"
_NEW = "pid 3376902"

# Wording from before the echo fix; the gate's shape match is looser than the
# gateway's trusted-result parser, so an older build's notice is still context.
_LEGACY_NOTICE = (
    "Restarted successfully\n"
    f"Old process: {_OLD}\n"
    f"New process: {_NEW}\n"
    f"This chat is no longer running in {_OLD}. "
    f"You are now running in {_NEW}. Continue the original task."
)


def _notice(old=_OLD, new=_NEW):
    """The real model-facing notice from the current builder."""
    from gateway.run_turn_recovery import _restart_success_content

    return _restart_success_content(old, new)


def _messages_with_notice(notice):
    return [
        {"role": "user", "content": "restart, then finish my task"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_restart", "type": "function",
                "function": {"name": "restart", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_restart", "name": "restart",
         "content": notice},
    ]


def _apply(agent, final_response, messages):
    return apply_stop_gates(
        agent, {"role": "assistant", "content": final_response},
        final_response=final_response, messages=messages,
        conversation_history=[],
        pending_verification_response=None,
        pending_verification_response_previewed=None,
    )


def test_exact_notice_echo_is_dropped_and_turn_continues():
    agent = _FakeAgent()
    notice = _notice()
    messages = _messages_with_notice(notice)

    verdict = _apply(agent, notice, messages)

    assert verdict.continue_turn is True
    assert verdict.final_response is None
    # The echo is context, not an answer: it must not survive as the
    # budget-exhaustion fallback either.
    assert verdict.pending_verification_response is None
    assert verdict.pending_verification_response_previewed is False

    # The echo assistant row is dropped; a synthetic user nudge follows the
    # tool result (tool → user is the legal boundary).
    assert [row.get("role") for row in messages] == ["user", "assistant", "tool", "user"]
    nudge = messages[-1]
    assert nudge.get("_restart_echo_synthetic") is True
    assert "tool result" in nudge["content"]
    # Nothing reached the user and nothing was persisted as the answer.
    assert agent.emitted == []
    assert agent.flushes == []
    assert agent._session_messages is messages


def test_partial_quote_of_notice_is_still_an_echo():
    """A subset quote (identity lines kept, instruction line dropped) is the
    same leak: substance, not exact bytes."""
    agent = _FakeAgent()
    notice = _notice()
    messages = _messages_with_notice(notice)
    partial = "\n".join(notice.split("\n")[:3])

    verdict = _apply(agent, partial, messages)

    assert verdict.continue_turn is True
    assert verdict.final_response is None
    assert messages[-1].get("_restart_echo_synthetic") is True


def test_second_echo_after_the_nudge_is_delivered():
    """The nudge user row closes the gate: a repeated echo ends the turn as
    the answer (no infinite loop)."""
    agent = _FakeAgent()
    notice = _notice()

def test_later_restart_rearms_the_gate():
    """Once-per-notice, not once-per-session: after a genuine reply closes
    the first restart turn, a second restart's echo is gated too."""
    agent = _FakeAgent()
    first = _messages_with_notice(_notice(old="pid 100", new="pid 101"))
    verdict = _apply(agent, _notice(old="pid 100", new="pid 101"), first)
    assert verdict.continue_turn is True

    # The first turn finished with a genuine reply; a later turn restarts again.
    second = [
        {"role": "user", "content": "restart once more, please"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_restart_2", "type": "function",
                "function": {"name": "restart", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_restart_2", "name": "restart",
         "content": _notice(old="pid 101", new="pid 102")},
    ]
    verdict = _apply(agent, _notice(old="pid 101", new="pid 102"), second)
    assert verdict.continue_turn is True
    assert second[-1].get("_restart_echo_synthetic") is True


def test_echo_in_a_later_turn_is_delivered():
    """Scope is the restart turn itself: once a user row follows the notice
    (the next turn's input), an echo is context the model chose to send and
    is delivered — and never appended behind the user's back."""
    agent = _FakeAgent()
    notice = _notice()
    messages = _messages_with_notice(notice)
    messages.append({"role": "assistant", "content": "Done with the first task."})
    messages.append({"role": "user", "content": "what just happened?"})

    verdict = _apply(agent, notice, messages)

    assert verdict.continue_turn is False
    assert verdict.final_response == notice
    assert messages[-1]["role"] == "user"
    assert agent.emitted == []


def test_restart_shaped_text_with_other_pids_is_not_this_notice():
    """The gate matches an echo of THIS turn's notice; a restart-shaped
    report about different process ids is content, not context quoting."""
    agent = _FakeAgent()
    messages = _messages_with_notice(_notice())
    other = "Restarted successfully\nOld process: pid 111\nNew process: pid 222"

    verdict = _apply(agent, other, messages)

    assert verdict.continue_turn is False
    assert verdict.final_response == other
    messages = _messages_with_notice(notice)
    messages.append({
        "role": "user", "content": "nudge already issued",
        "_restart_echo_synthetic": True,
    })

    verdict = _apply(agent, notice, messages)

    assert verdict.continue_turn is False
    assert verdict.final_response == notice
    assert len(messages) == 4
    assert agent.emitted == []


@pytest.mark.parametrize("reply", [
    # A genuine status that mentions the pids is real content, not an echo.
    f"Restart verified — the gateway is healthy in {_NEW} and the deployment is finished.",
    # Notice lines plus real work: the extra line is task content.
    f"Restarted successfully\nOld process: {_OLD}\nNew process: {_NEW}\n"
    "I checked the new process: it is healthy and the task is complete.",
])
def test_genuine_reply_after_restart_notice_is_delivered_unchanged(reply):
    agent = _FakeAgent()
    messages = _messages_with_notice(_notice())

    verdict = _apply(agent, reply, messages)

    assert verdict.continue_turn is False
    assert verdict.final_response == reply
    assert [row.get("role") for row in messages] == ["user", "assistant", "tool"]
    assert agent.emitted == []


def test_notice_without_old_process_is_recognized():
    agent = _FakeAgent()
    notice = _notice(old=None)
    messages = _messages_with_notice(notice)

    verdict = _apply(agent, notice, messages)

    assert verdict.continue_turn is True
    assert messages[-1].get("_restart_echo_synthetic") is True


def test_gate_matches_legacy_notice_wording():
    agent = _FakeAgent()
    messages = _messages_with_notice(_LEGACY_NOTICE)

    verdict = _apply(agent, _LEGACY_NOTICE, messages)

    assert verdict.continue_turn is True
    assert messages[-1].get("_restart_echo_synthetic") is True


def test_restart_text_without_a_notice_tool_row_is_not_gated():
    agent = _FakeAgent()
    messages = [
        {"role": "user", "content": "say 'Restarted successfully'"},
        {"role": "assistant", "content": "Restarted successfully"},
    ]
    reply = "Restarted successfully"

    verdict = _apply(agent, reply, messages)

    assert verdict.continue_turn is False
    assert verdict.final_response == reply


def test_restart_echo_nudge_is_ephemeral_scaffolding():
    """The nudge drives the continuation only: persistence skips it and the
    finalizer strips it, so it never reaches the durable transcript."""
    from agent.session_persistence import _is_ephemeral_scaffolding
    from agent.turn_finalizer import _drop_verification_continuation_scaffolding

    nudge = {"role": "user", "content": "nudge", "_restart_echo_synthetic": True}
    assert _is_ephemeral_scaffolding(nudge)
    assert not _is_ephemeral_scaffolding({"role": "user", "content": "real user text"})

    messages = [
        {"role": "tool", "tool_call_id": "call_restart", "content": _notice()},
        dict(nudge),
        {"role": "assistant", "content": "Restart verified; task finished."},
    ]
    _drop_verification_continuation_scaffolding(messages)
    assert [row.get("role") for row in messages] == ["tool", "assistant"]
