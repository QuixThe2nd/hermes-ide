"""Integration proof: transparent forced-interruption recovery at the model boundary.

These tests drive the REAL ``TurnRunner.run_sync`` resume path end-to-end —
the same code the running gateway executes for a startup auto-resume event —
with a stub ``AIAgent`` standing at the provider boundary.  What the stub's
``run_conversation`` receives IS the model-facing request (current message +
conversation history + turn kwargs), and what the recording session store
receives IS the persisted transcript tail.

The contract under proof (RESUME §1/§2/§3):

* a forced victim's model-facing request contains ZERO synthetic recovery
  rows — no restart note, no synthetic user message — and enters through the
  ordinary in-loop continuation seam (``continue_interrupted_turn``);
* the genuinely unresolved victim call is re-executed LITERALLY through the
  normal dispatcher, once, with its original call data — including
  side-effecting calls like ``terminal``;
* each replacement result persists exactly once, and the persisted
  transcript carries no synthetic recovery row;
* a REAL user message arriving during recovery runs verbatim, unwrapped,
  after the completed assistant→tool batch;
* cooperative restarts keep their safe-pause guidance (regression).

Deliberately NO import from ``gateway.forced_resume_replay``: the module's
symbols changed across the fix, and importing them would turn a base-commit
run into an import error (setup failure) instead of the intended behavioral
failure.  Everything asserted here is observable through ``gateway.run``'s
public turn surface, so the file also runs — and fails for the right
reasons — against the pre-fix implementation.
"""

import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.run import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext

_INTERRUPTED_RESULT = '{"exit_code": 130, "output": "[Command interrupted]"}'


@pytest.fixture(autouse=True)
def _isolated_replay_reservations():
    """Keep the replay-execution fence out of neighboring tests.

    Reservations live in the per-session substrate (per-test SQLite for a
    real store); the stub store here has none, so its calls fence through
    the process-global fallback map — the only shared state, and all this
    clears."""
    import gateway.forced_resume_replay as _freplay

    with _freplay._FALLBACK_RESERVATION_LOCK:
        _freplay._FALLBACK_RESERVATIONS.clear()


class StubModelAgent:
    """Provider-boundary stub: records the model-facing request and plays a
    minimal normal-tool-dispatcher for the replay."""

    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.session_id = kwargs["session_id"]
        self.tools = []
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000
        )
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_reasoning_tokens = 0
        self.executions = []
        self.run_calls = []
        self._results = {}

    def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
        for call in assistant_message.tool_calls:
            self.executions.append(
                (call.function.name, call.id, call.function.arguments)
            )
            from agent.tool_dispatch_helpers import make_tool_result_message

            messages.append(
                make_tool_result_message(
                    call.function.name,
                    self._results.get(call.id, "OK"),
                    call.id,
                )
            )
        return "ok"

    def run_conversation(self, message, **kwargs):
        self.run_calls.append((message, kwargs))
        return {
            "final_response": "Deploy finished cleanly.",
            "messages": [],
            "completed": True,
            "failed": False,
            "interrupted": False,
            "api_calls": 1,
            "tools": [],
        }


class RecordingSessionStore:
    """Real-shaped minimal session store: resume entries + transcript appends
    + live-transcript rewrites (the durable text-only trim path)."""

    def __init__(self, entries=None, rewrite_ok=True):
        self._entries = entries or {}
        self.appended = []
        self.rewrites = []
        self.rewrite_ok = rewrite_ok
        self.durable = []

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.appended.append((session_id, message))
        self.durable.append(dict(message))

    def append_to_transcript_checked(self, session_id, message):
        """The SYNCHRONOUS CHECKED primitive the real SessionStore exposes
        (commit + read back → bool); the recording model commits and
        verifies against its own durable tail."""
        self.append_to_transcript(session_id, message)
        return (
            bool(self.durable)
            and self.durable[-1].get("role") == message.get("role")
            and self.durable[-1].get("tool_call_id") == message.get("tool_call_id")
        )

    def rewrite_transcript(
        self, session_id, messages, active_only=False, reject_active_turn_lease=False
    ):
        self.rewrites.append((session_id, [dict(m) for m in messages], active_only))
        if not self.rewrite_ok:
            return False
        # Model the durable side of replace_messages(active_only=True).
        self.durable = [dict(m) for m in messages]
        return True


def _gateway_runner(store):
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store = store
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    runner._agent_config_signature.return_value = ("test-signature",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_telegram_topic_lane.return_value = False
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False
    return runner


def _resume_entry(reason):
    return SimpleNamespace(
        resume_pending=True,
        resume_reason=reason,
        last_resume_marked_at=time.time(),
    )


def _interrupted_terminal_history():
    """A forced victim whose final batch issued one side-effecting terminal
    call; the result row is an explicit interrupt marker."""
    return [
        {"role": "user", "content": "deploy the service", "timestamp": time.time()},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_deploy",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command": "make deploy"}',
                    },
                }
            ],
            "timestamp": time.time(),
        },
        {
            "role": "tool",
            "tool_call_id": "call_deploy",
            "content": _INTERRUPTED_RESULT,
            "timestamp": time.time(),
        },
    ]


def _make_ctx(agent_cls, history, message, session_key, store_entries,
              session_id="test-session"):
    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="test-chat",
        user_id="test-user",
    )
    return TurnContext(
        source=source,
        message=message,
        history=history,
        session_id=session_id,
        session_key=session_key,
        user_config={},
        AIAgent=agent_cls,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )


def _run_turn(agent_cls, history, message, reason):
    store = RecordingSessionStore({f"sk-{reason}": _resume_entry(reason)})
    runner = _gateway_runner(store)
    ctx = _make_ctx(agent_cls, history, message, f"sk-{reason}", store._entries)
    TurnRunner(runner, ctx).run_sync()
    return store


def _assert_pairs_strictly(history):
    issued = set()
    answered = []
    for row in history:
        if row.get("role") == "assistant" and row.get("tool_calls"):
            for call in row["tool_calls"]:
                cid = str(call.get("id") or call.get("call_id") or "")
                assert cid and cid not in issued, call
                issued.add(cid)
        elif row.get("role") == "tool":
            cid = str(row.get("tool_call_id") or "")
            assert cid in issued, cid
            answered.append(cid)
    assert len(answered) == len(set(answered)), "duplicate tool_call_id rows"


# ---------------------------------------------------------------------------
# The core integration proof: forced victim resumes transparently
# ---------------------------------------------------------------------------


def _run_and_capture_agent(history, message, reason, results):
    """Run the turn and return (constructed_agent, store)."""
    constructed = []

    class _Agent(StubModelAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._results = results
            constructed.append(self)

    store = _run_turn(_Agent, history, message, reason)
    assert len(constructed) == 1
    return constructed[0], store


def test_forced_victim_model_request_is_ordinary_continuation():
    """The stub at the provider boundary must be invoked with NO current
    message and the continuation flag set — the request is the same
    "continue after tool results" call an uninterrupted loop would make —
    and the unresolved side-effecting call must have been re-executed
    literally, exactly once, through the normal dispatcher."""
    history = _interrupted_terminal_history()
    agent, store = _run_and_capture_agent(
        history, "", "restart_timeout",
        {"call_deploy": '{"exit_code": 0, "output": "deployed"}'},
    )

    # The dispatcher re-ran the exact original call ONCE (side-effecting
    # terminal call included — no read-only whitelist).
    assert agent.executions == [
        ("terminal", "call_deploy", '{"command": "make deploy"}')
    ], agent.executions

    # Exactly one model-facing call, carrying the continuation semantics.
    assert len(agent.run_calls) == 1, agent.run_calls
    run_message, kwargs = agent.run_calls[0]
    assert run_message is None, (
        f"forced victim must not receive a synthetic current message, got {run_message!r}"
    )
    assert kwargs.get("continue_interrupted_turn") is True, kwargs.keys()

    # Nothing about the recovery may be persisted as a user row.
    assert kwargs.get("persist_user_message") is None, kwargs.get("persist_user_message")

    # The model-facing history: pairs strictly, ends in the fresh tool
    # result for the replayed call, carries no synthetic user row after the
    # batch and no recovery prose anywhere.
    model_history = kwargs.get("conversation_history") or []
    assert model_history, "no conversation history reached the model boundary"
    _assert_pairs_strictly(model_history)
    assert model_history[-1].get("role") == "tool"
    assert model_history[-1].get("tool_call_id") == "call_deploy"
    assert "deployed" in str(model_history[-1].get("content"))
    assert not any(
        row.get("role") == "user"
        and (
            "[System note:" in str(row.get("content") or "")
            or "restart" in str(row.get("content") or "").lower()
        )
        for row in model_history
    ), [r for r in model_history if r.get("role") == "user"]
    for banned in ("gateway restart", "was interrupted by", "was restored"):
        assert banned not in str(run_message or "").lower()
        for row in model_history:
            assert banned not in str(row.get("content") or "").lower(), row

    # Persisted transcript: only replacement tool rows were written — zero
    # synthetic recovery rows of any kind.
    assert all(row.get("role") == "tool" for _sid, row in store.appended)
    assert [row["tool_call_id"] for _sid, row in store.appended] == ["call_deploy"]


def test_forced_victim_real_user_message_runs_verbatim_after_batch():
    """Real user text arriving while recovery closes the batch: the model
    receives it VERBATIM (no recovery wrapper) as an ordinary new user turn,
    and only after the batch was completed and persisted."""
    history = _interrupted_terminal_history()
    agent, store = _run_and_capture_agent(
        history, "What happened to the deploy?", "shutdown_timeout",
        {"call_deploy": '{"exit_code": 0, "output": "deployed"}'},
    )

    # Recovery still paired and persisted the batch first.
    assert agent.executions == [
        ("terminal", "call_deploy", '{"command": "make deploy"}')
    ]
    assert [row["tool_call_id"] for _sid, row in store.appended] == ["call_deploy"]

    run_message, kwargs = agent.run_calls[0]
    assert run_message == "What happened to the deploy?"
    assert kwargs.get("continue_interrupted_turn") is None
    # The persisted user text is the clean words, not a wrapped note.
    assert kwargs.get("persist_user_message") == "What happened to the deploy?"

    model_history = kwargs.get("conversation_history") or []
    _assert_pairs_strictly(model_history)
    # History ends with the completed tool batch; the user text is the
    # CURRENT turn (never interleaved inside the batch).
    assert model_history[-1].get("role") == "tool"
    assert model_history[-1].get("tool_call_id") == "call_deploy"


def test_cooperative_restart_keeps_pause_guidance_at_model_boundary():
    """Regression: the session that accepted the cooperative steer still
    receives its safe-pause guidance — forced transparency must not leak
    into (or from) the cooperative path."""
    history = _interrupted_terminal_history()
    agent, store = _run_and_capture_agent(history, "", "cooperative_restart", {})

    # Cooperative parked sessions do NOT re-run their interrupted calls.
    assert agent.executions == []
    assert store.appended == []

    run_message, kwargs = agent.run_calls[0]
    assert isinstance(run_message, str) and run_message.strip()
    assert "parked itself" in run_message
    assert "CONTINUE the parked task" in run_message
    assert kwargs.get("continue_interrupted_turn") is None


def test_text_only_interruption_resumes_without_note():
    """A forced victim interrupted with NO tool batch: the incomplete
    assistant text tail is EXCLUDED from both the model-facing history and
    the durable transcript, and the turn continues from the original legal
    boundary (the user row) with zero synthetic rows.  Appending a fresh
    assistant row straight after the incomplete one would persist the
    invalid user→assistant→assistant sequence strict providers reject —
    the pre-fix behavior."""
    history = [
        {"role": "user", "content": "summarize the incident", "timestamp": time.time()},
        {
            "role": "assistant",
            "content": "Working on it…",
            "timestamp": time.time(),
        },
    ]
    agent, store = _run_and_capture_agent(history, "", "restart_interrupted", {})

    assert agent.executions == []
    assert store.appended == []
    run_message, kwargs = agent.run_calls[0]
    assert run_message is None, run_message
    assert kwargs.get("continue_interrupted_turn") is True

    # Model-facing history resumes from the ORIGINAL legal boundary: the
    # incomplete assistant tail is gone and no synthetic row replaced it.
    model_history = kwargs.get("conversation_history") or []
    assert [row.get("role") for row in model_history] == ["user"]
    for row in model_history:
        assert not str(row.get("content") or "").startswith("[System note:")

    # The durable transcript was rewritten WITHOUT the incomplete tail, so
    # the continuation's new assistant row lands directly after the user
    # row (valid alternation) instead of after another assistant row.
    assert len(store.rewrites) == 1
    _sid, rewritten_rows, active_only = store.rewrites[0]
    assert _sid == "test-session"
    assert [row.get("role") for row in rewritten_rows] == ["user"]
    assert active_only is True
    assert [row.get("role") for row in store.durable] == ["user"]


def test_text_only_recovery_fails_closed_when_durable_trim_fails():
    """If the durable transcript cannot be trimmed (rewrite fails), the
    recovery must NOT run the model continuation — a new assistant row
    would append straight after the incomplete one.  Fail closed with the
    TYPED CONTROL outcome (REPAIR3 finding 6: zero recovery prose, zero
    synthetic rows for a forced victim) instead of fabricating a turn."""
    from gateway.run import _is_forced_recovery_control_outcome

    history = [
        {"role": "user", "content": "summarize the incident", "timestamp": time.time()},
        {"role": "assistant", "content": "Working on it…", "timestamp": time.time()},
    ]
    store = RecordingSessionStore(
        {f"sk-restart_interrupted": _resume_entry("restart_interrupted")},
        rewrite_ok=False,
    )
    runner = _gateway_runner(store)
    ctx = _make_ctx(
        StubModelAgent, history, "", "sk-restart_interrupted", store._entries
    )
    TurnRunner(runner, ctx).run_sync()
    result = ctx.result_holder[0]

    # The model was never invoked…
    assert result.get("failed") is True
    assert result.get("messages") == []
    # …and the failure is a typed control outcome with ZERO prose: a
    # forced victim is never told the recovery failed.
    assert not result.get("final_response")
    assert _is_forced_recovery_control_outcome(result) is True
    assert result.get("error") == "forced_resume_text_tail_trim_failed"
    # …and the durable transcript was left untouched (no fake rows).
    assert store.appended == []


def test_text_only_tail_kept_when_real_user_message_arrives():
    """A real user message after a text-only interruption appends AFTER the
    incomplete assistant tail — already legal user/assistant alternation —
    so no trim happens and the real text runs verbatim as a normal turn."""
    history = [
        {"role": "user", "content": "summarize the incident", "timestamp": time.time()},
        {"role": "assistant", "content": "Working on it…", "timestamp": time.time()},
    ]
    agent, store = _run_and_capture_agent(history, "status?", "restart_interrupted", {})

    run_message, kwargs = agent.run_calls[0]
    assert run_message == "status?"
    assert kwargs.get("continue_interrupted_turn") is None
    # No trim: the tail survives and the new turn is ordinary.
    assert store.rewrites == []
    model_history = kwargs.get("conversation_history") or []
    assert [row.get("role") for row in model_history] == ["user", "assistant"]


def test_cached_live_history_dangling_batch_still_replays():
    """Reviewer probe (cached live-history branch): when the FTS guard
    selects the cached agent's live memory — which holds exactly the
    dangling assistant(tool_calls) batch the bounce left behind — the
    replay must run against THAT authoritative history.  Skipping it (the
    pre-fix ``_live_history_selected`` shortcut) handed the provider a
    dangling, unanswerable tail."""
    import threading

    persisted = [
        {"role": "user", "content": "deploy", "timestamp": time.time()},
    ]
    live = persisted + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_live",
                    "function": {"name": "terminal", "arguments": '{"command": "make deploy"}'},
                }
            ],
            "timestamp": time.time(),
        },
    ]
    store = RecordingSessionStore(
        {f"sk-restart_timeout": _resume_entry("restart_timeout")}
    )
    runner = _gateway_runner(store)
    # A cached agent bound to this exact session, whose live memory holds
    # the unpersisted dangling batch.
    cached_agent = StubModelAgent(model="test-model", session_id="test-session")
    cached_agent._results = {"call_live": '{"exit_code": 0, "output": "deployed"}'}
    cached_agent._session_messages = live
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {
        "sk-restart_timeout": (cached_agent, ("test-signature",), None, "test-session"),
    }
    ctx = _make_ctx(
        StubModelAgent, persisted, "", "sk-restart_timeout", store._entries
    )
    TurnRunner(runner, ctx).run_sync()

    # The dangling live call was re-executed exactly once through the
    # dispatcher — the pre-fix branch executed nothing.
    assert cached_agent.executions == [
        ("terminal", "call_live", '{"command": "make deploy"}')
    ], cached_agent.executions

    # The provider request carries the repaired batch: history ends in the
    # fresh tool result — never a dangling assistant(tool_calls) tail.
    run_message, kwargs = cached_agent.run_calls[0]
    assert run_message is None
    assert kwargs.get("continue_interrupted_turn") is True
    model_history = kwargs.get("conversation_history") or []
    _assert_pairs_strictly(model_history)
    assert model_history[-1].get("role") == "tool"
    assert model_history[-1].get("tool_call_id") == "call_live"
    assert "deployed" in str(model_history[-1].get("content"))

    # The replacement result was durably appended (the lagging persisted
    # transcript catches up), with zero synthetic rows.
    assert [row["tool_call_id"] for _sid, row in store.appended] == ["call_live"]


# ---------------------------------------------------------------------------
# REPAIR2 real-path contracts at the TurnRunner level
# ---------------------------------------------------------------------------


def _capture_agent_cls(results):
    """StubModelAgent subclass that records every construction."""
    constructed = []

    class _Agent(StubModelAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._results = results
            constructed.append(self)

    return _Agent, constructed


def test_failed_replay_blocks_turn_and_keeps_resume_pending():
    """REPAIR2 finding 4 + REPAIR3 finding 6: a forced victim whose replayed
    result cannot be PROVEN durable (checked append fails) must never reach
    the provider.  The turn ends in the TYPED CONTROL outcome — no
    synthetic answer, no fabricated success, and ZERO recovery prose (the
    forced-victim contract: nothing is delivered, nothing is appended as
    user/assistant/system text) — and ``resume_pending`` stays set so the
    recovery is retried (failed turns never clear the marker)."""
    from gateway.run import (
        _is_forced_recovery_control_outcome,
        _should_clear_resume_pending_after_turn,
    )

    class _UnprovableStore(RecordingSessionStore):
        def append_to_transcript_checked(self, session_id, message):
            # Record the attempted write, then report the commit/read-back
            # as unprovable — exactly the failure mode the checked
            # primitive exists to surface.
            self.appended.append((session_id, message))
            return False

    history = _interrupted_terminal_history()
    store = _UnprovableStore(
        {f"sk-restart_timeout": _resume_entry("restart_timeout")}
    )
    runner = _gateway_runner(store)
    agent_cls, constructed = _capture_agent_cls(
        {"call_deploy": '{"exit_code": 0, "output": "deployed"}'}
    )
    ctx = _make_ctx(agent_cls, history, "", "sk-restart_timeout", store._entries)
    TurnRunner(runner, ctx).run_sync()
    result = ctx.result_holder[0]

    agent = constructed[0]
    # The side effect ran exactly once — then persistence failed closed.
    assert agent.executions == [
        ("terminal", "call_deploy", '{"command": "make deploy"}')
    ]
    # ZERO provider calls: the model was never invoked on a half-persisted
    # batch, and no synthetic answer replaced the real one.
    assert agent.run_calls == []
    assert result.get("failed") is True
    assert result.get("messages") == []
    assert result.get("error") == "forced_resume_replay_failed"
    # ZERO recovery prose for a forced victim: no text to deliver, and the
    # typed control marker is what downstream boundaries gate on.
    assert not result.get("final_response")
    assert _is_forced_recovery_control_outcome(result) is True
    # No synthetic user/assistant/system row was persisted over the
    # failure — only the attempted (unprovable) tool-result write.
    assert all(row.get("role") == "tool" for _sid, row in store.appended)
    # The recovery signal survives for a bounded retry…
    assert _should_clear_resume_pending_after_turn(result) is False
    assert store._entries["sk-restart_timeout"].resume_pending is True


def test_replay_window_observes_real_approval_binding():
    """REPAIR2 finding 5: the re-run goes through the NORMAL dispatcher,
    whose approval middleware resolves the session from the approval
    contextvar and notifies through the gateway callback.  Both must be
    bound to the REAL session key for the whole replay window — and fully
    unbound once the turn ends."""
    from tools import approval as _approval

    observed = {}

    class _ObservingAgent(StubModelAgent):
        def _execute_tool_calls(self, assistant_message, messages, task_id,
                                api_call_count):
            observed["session_key"] = _approval.get_current_session_key()
            observed["notify_bound"] = (
                "sk-restart_timeout" in _approval._gateway_notify_cbs
            )
            observed["notify_cb"] = _approval._gateway_notify_cbs.get(
                "sk-restart_timeout"
            )
            return super()._execute_tool_calls(
                assistant_message, messages, task_id, api_call_count
            )

    pre_key = _approval.get_current_session_key()
    history = _interrupted_terminal_history()
    store = RecordingSessionStore(
        {f"sk-restart_timeout": _resume_entry("restart_timeout")}
    )
    runner = _gateway_runner(store)
    ctx = _make_ctx(
        _ObservingAgent, history, "", "sk-restart_timeout", store._entries
    )
    TurnRunner(runner, ctx).run_sync()

    # During the replay dispatch the approval middleware observed the real
    # session key and a registered, callable gateway notify callback.
    assert observed["session_key"] == "sk-restart_timeout", observed
    assert observed["notify_bound"] is True
    assert callable(observed["notify_cb"])
    # The replay executed and the turn completed normally.
    assert ctx.result_holder[0].get("failed") is not True
    # After the turn the binding is gone — no leaked contextvar, no stale
    # notify registration for this session key.
    assert _approval.get_current_session_key() == pre_key
    assert "sk-restart_timeout" not in _approval._gateway_notify_cbs


@pytest.mark.parametrize(
    "name,args,cid",
    [
        # Direct: the agent-side gateway lifecycle request itself.
        ("restart", {"action": "restart"}, "call_direct"),
        # Bridge: a deferred tool_call wrapper naming restart, carrying the
        # composite id shape the bridge mints.
        (
            "tool_call",
            {"name": "restart", "arguments": "{\"action\": \"restart\"}"},
            "call_bridge|item_1",
        ),
    ],
)
def test_lifecycle_only_batch_pairs_durably_without_executing(name, args, cid):
    """REPAIR2 finding 6: a forced victim whose final batch is ONLY the
    lifecycle request that caused the bounce (direct restart tool, or a
    deferred bridge wrapping it) must never re-execute that call.  The
    batch still CLOSES — one fail-closed UNKNOWN orphan row under the
    EXACT call id, durably persisted — so the reconstructed transcript
    pairs and the model continues instead of staring at an unanswered
    batch."""
    history = [
        {"role": "user", "content": "restart yourself", "timestamp": time.time()},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
            ],
            "timestamp": time.time(),
        },
    ]
    store = RecordingSessionStore(
        {f"sk-restart_timeout": _resume_entry("restart_timeout")}
    )
    runner = _gateway_runner(store)
    agent_cls, constructed = _capture_agent_cls({})
    ctx = _make_ctx(agent_cls, history, "", "sk-restart_timeout", store._entries)
    TurnRunner(runner, ctx).run_sync()

    agent = constructed[0]
    # The lifecycle request never re-ran — no second bounce from inside the
    # recovery that is fixing the last one.
    assert agent.executions == [], agent.executions

    # The batch closed durably: exactly one tool row, under the EXACT
    # original id (composite bridge id included), reporting UNKNOWN —
    # never a fabricated success.
    assert [_sid for _sid, _row in store.appended] == ["test-session"]
    row = store.appended[0][1]
    assert row.get("tool_call_id") == cid, row
    content = str(row.get("content") or "")
    assert "Orphan recovery" in content, content
    assert "UNKNOWN" in content, content

    # The model was invoked exactly once, on a strictly-paired history that
    # ends in the fail-closed answer — with zero synthetic recovery rows.
    assert len(agent.run_calls) == 1, agent.run_calls
    run_message, kwargs = agent.run_calls[0]
    assert run_message is None
    assert kwargs.get("continue_interrupted_turn") is True
    model_history = kwargs.get("conversation_history") or []
    _assert_pairs_strictly(model_history)
    assert model_history[-1].get("role") == "tool"
    assert model_history[-1].get("tool_call_id") == cid
    for banned in ("gateway restart", "was interrupted by", "was restored"):
        for row in model_history:
            assert banned not in str(row.get("content") or "").lower(), row
    assert ctx.result_holder[0].get("failed") is not True


def test_two_text_tail_workers_single_claim_single_provider_call(tmp_path):
    """REPAIR2 finding 7: two gateway workers that loaded the same
    text-only interrupted tail share ONE durable recovery.  Exactly one
    wins the SessionDB ownership claim, makes the single provider call,
    and rewrites the transcript; the loser stands down BEFORE any provider
    execution — typed blocked result, zero calls, transcript untouched."""
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    home = tmp_path / "race-home"
    (home / "sessions").mkdir(parents=True)
    import os as _os

    _prev_home = _os.environ.get("HERMES_HOME")
    _os.environ["HERMES_HOME"] = str(home)
    try:
        store = SessionStore(
            sessions_dir=home / "sessions", config=GatewayConfig()
        )
        db = SessionDB(db_path=home / "state.db")
        if store._db is not None and store._db is not db:
            store._db.close()
        store._db = db

        source = SessionSource(
            platform=Platform.LOCAL, chat_id="race-chat", user_id="race-user"
        )
        entry = store.get_or_create_session(source)
        sid = "sess-race"
        with store._lock:
            store._entries[entry.session_key].session_id = sid
        # The drain-timeout marker the restart watchdog stamps at interrupt
        # time — what makes both workers take the forced-victim branch.
        entry.resume_pending = True
        entry.resume_reason = "restart_interrupted"
        entry.last_resume_marked_at = datetime.now()
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, "user", content="summarize the incident")
        db.append_message(sid, "assistant", content="Working on it…")

        # Both workers loaded the SAME tail before either ran.
        shared_tail = db.get_messages_as_conversation(sid)
        assert [r.get("role") for r in shared_tail] == ["user", "assistant"]

        def _run_worker():
            runner = _gateway_runner(store)
            agent_cls, constructed = _capture_agent_cls({})
            ctx = _make_ctx(
                agent_cls,
                [dict(r) for r in shared_tail],
                "",
                entry.session_key,
                store._entries,
                session_id=sid,
            )
            TurnRunner(runner, ctx).run_sync()
            return ctx, constructed[0]

        winner_ctx, winner_agent = _run_worker()
        loser_ctx, loser_agent = _run_worker()

        # ONE provider call across both workers — the winner's continuation.
        assert len(winner_agent.run_calls) == 1, winner_agent.run_calls
        assert loser_agent.run_calls == [], loser_agent.run_calls
        assert winner_ctx.result_holder[0].get("failed") is not True

        loser_result = loser_ctx.result_holder[0]
        assert loser_result.get("failed") is True
        # The winner already moved the durable tail (trim + rewrite), so the
        # loser's stale plan is SUPERSEDED — either way it stood down before
        # any provider execution.
        assert loser_result.get("error") == "forced_recovery_superseded"
        assert loser_result.get("messages") == []

        # The winner's single durable recovery is exactly what survived:
        # the incomplete assistant tail was trimmed ONCE and the loser
        # neither trimmed again nor appended anything.
        durable_roles = [
            r.get("role") for r in db.get_messages_as_conversation(sid)
        ]
        assert durable_roles == ["user"], durable_roles

        # The loser's stand-down keeps the recovery signal for a retry.
        assert store._entries[entry.session_key].resume_pending is True
    finally:
        db.close()
        if _prev_home is None:
            _os.environ.pop("HERMES_HOME", None)
        else:
            _os.environ["HERMES_HOME"] = _prev_home


# ---------------------------------------------------------------------------
# REPAIR3: real-TurnRunner + real-SessionStore/SessionDB regressions for the
# fresh adversarial review's blocking findings.  Unlike the
# RecordingSessionStore tests above, these run the resume path against a REAL
# gateway SessionStore backed by a REAL per-test SQLite SessionDB — the same
# substrate the reviewer's exit-42/43/46 probes drove — so the durable
# claim/reservation/supersede boundaries are exercised, not modeled.  These
# tests import the (now committed) gateway.forced_resume_replay seam helpers
# to pre-state the durable fences exactly as another worker would.
# ---------------------------------------------------------------------------


def _real_store_env(tmp_path, history, reason):
    """A REAL SessionStore + SessionDB pair holding ``history`` durably, with
    a routing entry stamped as a forced-interruption victim.

    Returns ``(store, db, entry, sid, restore)``; ``restore()`` puts the
    ambient HERMES_HOME back (call from a finally).
    """
    import os as _os

    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    home = tmp_path / f"repair3-home-{reason}"
    (home / "sessions").mkdir(parents=True)
    _prev_home = _os.environ.get("HERMES_HOME")
    _os.environ["HERMES_HOME"] = str(home)

    def _restore():
        db.close()
        if _prev_home is None:
            _os.environ.pop("HERMES_HOME", None)
        else:
            _os.environ["HERMES_HOME"] = _prev_home

    store = SessionStore(sessions_dir=home / "sessions", config=GatewayConfig())
    db = SessionDB(db_path=home / "state.db")
    if store._db is not None and store._db is not db:
        store._db.close()
    store._db = db

    source = SessionSource(
        platform=Platform.LOCAL, chat_id="repair3-chat", user_id="repair3-user"
    )
    entry = store.get_or_create_session(source)
    sid = f"sess-repair3-{reason}"
    with store._lock:
        store._entries[entry.session_key].session_id = sid
    # The drain-timeout marker the restart watchdog stamps at interrupt time.
    entry.resume_pending = True
    entry.resume_reason = reason
    entry.last_resume_marked_at = datetime.now()

    db.create_session(session_id=sid, source="cli")
    for row in history:
        db.append_message(
            sid,
            row["role"],
            content=row.get("content"),
            tool_calls=row.get("tool_calls"),
            tool_call_id=row.get("tool_call_id"),
            tool_name=row.get("tool_name") or row.get("name"),
        )
    return store, db, entry, sid, _restore


def test_preheld_reservation_conflict_blocks_below_provider(tmp_path):
    """REPAIR3 finding 3 (verifier exit 43): a pre-held DURABLE execution
    reservation means another worker owns the unresolved batch.  The loser
    stands down without provider invocation, without adding UNKNOWN, without
    clearing resume pending, and without user-visible prose — a typed
    blocked result."""
    from gateway.forced_resume_replay import (
        ReplayExecutionLedger,
        build_victim_replay_plan,
    )
    from gateway.run import (
        _is_forced_recovery_control_outcome,
        _should_clear_resume_pending_after_turn,
    )

    history = _interrupted_terminal_history()
    store, db, entry, sid, _restore = _real_store_env(
        tmp_path, history, "restart_timeout"
    )
    try:
        # Another worker already executed this exact call identity and its
        # result never became durable: the reservation is HELD.
        plan = build_victim_replay_plan(history)
        assert [c.call_id for c in plan.replay_calls] == ["call_deploy"]
        reserved, why = ReplayExecutionLedger(store, sid).reserve_execution(
            plan.replay_calls[0]
        )
        assert reserved is True, why

        runner = _gateway_runner(store)
        agent_cls, constructed = _capture_agent_cls(
            {"call_deploy": '{"exit_code": 0, "output": "deployed"}'}
        )
        ctx = _make_ctx(
            agent_cls, history, "", entry.session_key, store._entries, session_id=sid
        )
        TurnRunner(runner, ctx).run_sync()
        result = ctx.result_holder[0]
        agent = constructed[0]

        # ZERO executions and ZERO provider calls.
        assert agent.executions == []
        assert agent.run_calls == []
        # Typed blocked result: failed, no messages, no prose.
        assert result.get("failed") is True
        assert result.get("messages") == []
        assert not result.get("final_response")
        assert _is_forced_recovery_control_outcome(result) is True
        assert result.get("error") == "forced_resume_replay_failed"
        # Not clearable: the recovery signal survives.
        assert _should_clear_resume_pending_after_turn(result) is False
        assert store._entries[entry.session_key].resume_pending is True
        # No UNKNOWN row was fabricated over the unresolved side effect:
        # the stale interrupted marker is still the only durable tool row.
        tool_rows = [
            r
            for r in db.get_messages_as_conversation(sid)
            if r.get("role") == "tool"
        ]
        assert [r.get("tool_call_id") for r in tool_rows] == ["call_deploy"]
        assert "interrupted" in str(tool_rows[0].get("content") or "").lower()
    finally:
        _restore()


def test_new_user_text_racing_unclosed_batch_is_queued_not_run(tmp_path):
    """REPAIR3 finding 4 (verifier exit 42): while another worker holds the
    durable recovery claim and the interrupted batch is still open, the
    losing worker must make ZERO provider calls and preserve the text
    byte-for-byte; after the winner durably closes the exact batch, the
    text runs ONCE at the legal boundary, byte-exact."""
    from agent.tool_dispatch_helpers import make_tool_result_message
    from gateway.run import _is_forced_recovery_control_outcome
    from hermes_state import forced_recovery_tail_digest

    history = _interrupted_terminal_history()
    store, db, entry, sid, _restore = _real_store_env(
        tmp_path, history, "restart_timeout"
    )
    try:
        # Another worker owns the recovery of THIS exact tail.
        assert (
            db.claim_forced_recovery_tail(
                sid, forced_recovery_tail_digest(history[-1])
            )
            == "claimed"
        )

        text = "  NEXT\nUSER  BYTES  "
        runner = _gateway_runner(store)
        agent_cls, constructed = _capture_agent_cls({})
        ctx = _make_ctx(
            agent_cls, history, text, entry.session_key, store._entries,
            session_id=sid,
        )
        TurnRunner(runner, ctx).run_sync()
        result = ctx.result_holder[0]

        # ZERO provider calls while the claim is held and the batch open.
        loser_agent = constructed[0]
        assert loser_agent.run_calls == []
        assert loser_agent.executions == []
        # Typed blocked control outcome — no prose delivered.
        assert result.get("failed") is True
        assert not result.get("final_response")
        assert _is_forced_recovery_control_outcome(result) is True
        assert result.get("error") == "forced_recovery_already_claimed_batch_open"
        # The text is preserved BYTE-FOR-BYTE for the legal boundary.
        assert runner._pending_resume_user_text[entry.session_key] == text
        # The durable transcript is untouched: the stale marker is still
        # the only tool row for the call.
        tool_rows = [
            r
            for r in db.get_messages_as_conversation(sid)
            if r.get("role") == "tool"
        ]
        assert [r.get("tool_call_id") for r in tool_rows] == ["call_deploy"]

        # The winner durably closes the exact batch (one transaction:
        # archive the stale marker, land the fresh result) and completes.
        db.supersede_tool_results(
            sid,
            [
                make_tool_result_message(
                    "terminal",
                    '{"exit_code": 0, "output": "deployed"}',
                    "call_deploy",
                )
            ],
            ["call_deploy"],
        )
        entry.resume_pending = False

        # The queued text now appears ONCE, byte-exact, as an ordinary
        # turn on the closed transcript (timestamps aged past the
        # freshness window, as they are by the next real message).
        closed_history = db.get_messages_as_conversation(sid)
        stale_ts = time.time() - 7200
        for row in closed_history:
            row["timestamp"] = stale_ts
        ctx2 = _make_ctx(
            agent_cls, closed_history, "", entry.session_key, store._entries,
            session_id=sid,
        )
        TurnRunner(runner, ctx2).run_sync()
        result2 = ctx2.result_holder[0]

        assert result2.get("failed") is not True
        assert len(constructed) == 2
        winner_agent = constructed[1]
        assert len(winner_agent.run_calls) == 1, winner_agent.run_calls
        run_message, kwargs = winner_agent.run_calls[0]
        assert run_message == text  # byte-exact, exactly once
        assert kwargs.get("continue_interrupted_turn") is None
        # The queue is drained — the text will not run twice.
        assert entry.session_key not in runner._pending_resume_user_text
    finally:
        _restore()


def test_duplicate_call_ids_block_below_provider_through_real_runner(tmp_path):
    """REPAIR3 finding 5 (verifier exit 46): duplicate call ids are
    malformed batch identity.  Through the real runner/store: zero
    executions, zero provider calls, no new durable result rows, not
    clearable, no forced-recovery prose."""
    from gateway.run import (
        _is_forced_recovery_control_outcome,
        _should_clear_resume_pending_after_turn,
    )

    history = [
        {"role": "user", "content": "read both", "timestamp": time.time()},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "dup",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "a"}',
                    },
                },
                {
                    "id": "dup",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "b"}',
                    },
                },
            ],
            "timestamp": time.time(),
        },
    ]
    store, db, entry, sid, _restore = _real_store_env(
        tmp_path, history, "restart_timeout"
    )
    try:
        rows_before = db.get_messages_as_conversation(sid)
        runner = _gateway_runner(store)
        agent_cls, constructed = _capture_agent_cls({"dup": "MUST NOT RUN"})
        ctx = _make_ctx(
            agent_cls, history, "", entry.session_key, store._entries,
            session_id=sid,
        )
        TurnRunner(runner, ctx).run_sync()
        result = ctx.result_holder[0]
        agent = constructed[0]

        # Zero execution, zero provider calls.
        assert agent.executions == []
        assert agent.run_calls == []
        # No new durable result rows — the transcript is byte-identical.
        assert db.get_messages_as_conversation(sid) == rows_before
        # Typed blocked control outcome, no prose.
        assert result.get("failed") is True
        assert result.get("messages") == []
        assert not result.get("final_response")
        assert _is_forced_recovery_control_outcome(result) is True
        assert result.get("error") == "forced_resume_replay_failed"
        # Not clearable.
        assert _should_clear_resume_pending_after_turn(result) is False
        assert store._entries[entry.session_key].resume_pending is True
    finally:
        _restore()
