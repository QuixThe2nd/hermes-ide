"""Uniform delegation lifecycle — delivery-mode primitives.

Covers the Phase-0 chokepoints in ``tools/async_delegation.py``:

- ``publish_terminal_event`` is the ONLY producer of ``type="async_delegation"``
  on the shared completion queue, and it reroutes into a registered inline
  waiter instead (exactly one delivery channel);
- ``register_inline_wait`` / ``claim_inline_result`` / ``abandon_inline_wait``
  linearize the abandon-vs-completion race under ``_records_lock``;
- ``build_background_acceptance_envelope`` is the one acceptance shape;
- ``background_delivery_supported`` fails clearly on a stateless channel and
  keeps the api-server self-post escape alive;
- ``delegation_id`` / ``runner_tid`` plumbing, and the ``result_kind``-aware
  completion header.
"""

import json
import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import (
    format_process_notification,
    process_registry,
)


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _queue_empty() -> bool:
    return process_registry.completion_queue.empty()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


# ---------------------------------------------------------------------------
# publish_terminal_event — the single queue producer
# ---------------------------------------------------------------------------

def test_publish_terminal_event_puts_on_the_shared_queue():
    ok = ad.publish_terminal_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg_abc12345",
            "session_key": "agent:main:cli:dm:local",
            "status": "completed",
            "summary": "done",
        },
        {"status": "completed", "summary": "done"},
    )
    assert ok is True
    evt = _drain_one()
    assert evt is not None
    assert evt["delegation_id"] == "deleg_abc12345"


def test_publish_terminal_event_routes_to_inline_waiter_not_queue():
    waiter = ad.register_inline_wait("deleg_inline0001", session_key="s")
    delivered = ad.publish_terminal_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg_inline0001",
            "session_key": "s",
            "status": "completed",
            "summary": "inline result",
        },
        {"status": "completed", "summary": "inline result"},
    )

    # Inline hand-off: no event rail, no fresh turn.
    assert delivered is False
    assert _queue_empty()
    assert waiter.is_set()

    evt = ad.claim_inline_result("deleg_inline0001")
    assert evt is not None
    assert evt["summary"] == "inline result"
    # Exactly-once: the parked result is consumed.
    assert ad.claim_inline_result("deleg_inline0001") is None


def test_publish_terminal_event_inline_and_event_are_mutually_exclusive():
    """The mode swap and the hand-off both happen under one lock hold."""
    delivered = []

    def racer():
        for _ in range(50):
            delivered.append(
                ad.publish_terminal_event(
                    {
                        "type": "async_delegation",
                        "delegation_id": "deleg_race00001",
                        "session_key": "s",
                        "status": "completed",
                    },
                    {"status": "completed"},
                )
            )

    waiter = ad.register_inline_wait("deleg_race00001", session_key="s")
    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # While an unclaimed inline wait is registered, NO publish may reach the
    # queue — every racer linearized on the inline side of the swap.
    assert all(uploaded is False for uploaded in delivered)
    assert _queue_empty()
    assert waiter.is_set()

    # Once the waiter has claimed (and the mode reverts), a late duplicate
    # publish goes to the rail rather than vanishing.
    assert ad.claim_inline_result("deleg_race00001") is not None
    assert ad.abandon_inline_wait("deleg_race00001") is None
    assert ad.publish_terminal_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg_race00001",
            "status": "completed",
        },
        {"status": "completed"},
    ) is True
    assert _drain_one() is not None


# ---------------------------------------------------------------------------
# abandon_inline_wait — the abandon-vs-completion race
# ---------------------------------------------------------------------------

def test_abandon_with_pending_result_hands_it_back_for_the_rail():
    ad.register_inline_wait("deleg_pend00001", session_key="s")
    ad.publish_terminal_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg_pend00001",
            "status": "completed",
            "summary": "already terminal",
        },
        {"status": "completed"},
    )

    parked = ad.abandon_inline_wait("deleg_pend00001")
    assert parked is not None
    assert parked["summary"] == "already terminal"
    # The caller publishes it on the rail — still exactly one channel.
    assert ad.publish_terminal_event(parked, {"status": "completed"}) is True
    assert _drain_one()["summary"] == "already terminal"


def test_abandon_without_result_keeps_record_live_for_the_rail():
    ad.register_inline_wait("deleg_live00001", session_key="s")
    assert ad.abandon_inline_wait("deleg_live00001") is None

    # The record survived and reverted to event mode.
    assert ad.publish_terminal_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg_live00001",
            "status": "completed",
            "summary": "late terminal",
        },
        {"status": "completed"},
    ) is True
    assert _drain_one()["summary"] == "late terminal"


def test_abandon_is_idempotent_and_tolerates_unknown_ids():
    assert ad.abandon_inline_wait("deleg_nope00001") is None
    ad.register_inline_wait("deleg_twice0001", session_key="s")
    assert ad.abandon_inline_wait("deleg_twice0001") is None
    assert ad.abandon_inline_wait("deleg_twice0001") is None


# ---------------------------------------------------------------------------
# Acceptance envelope
# ---------------------------------------------------------------------------

def test_acceptance_envelope_shape_and_tool_provenance():
    payload = ad.build_background_acceptance_envelope(
        tool="delegate_assistant",
        result_kind="mission",
        delegation_id="mission-abc123def456",
        goals=["Talk to Sam about the launch"],
        note="running in the background",
        control_hint="dispatch_agent(action='status')",
    )
    assert payload["status"] == "dispatched"
    assert payload["mode"] == "background"
    assert payload["delegation_id"] == "mission-abc123def456"
    assert payload["tool"] == "delegate_assistant"
    assert payload["result_kind"] == "mission"
    assert payload["count"] == 1
    # Omitted optionals are absent, not null.
    assert "subagent_ids" not in payload
    assert "live_transcripts" not in payload


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------

def test_background_delivery_supported_by_default():
    ok, reason = ad.background_delivery_supported()
    assert ok is True
    assert reason == ""


def test_background_delivery_unsupported_on_stateless_channel():
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()
    try:
        ok, reason = ad.background_delivery_supported()
    finally:
        from gateway.session_context import set_session_vars

        set_session_vars(async_delivery=True)
    assert ok is False
    assert "NO WORK WAS STARTED" in reason
    assert "background=false" in reason


def test_background_delivery_supported_with_bound_wake_session_id(monkeypatch):
    """A stateless HTTP session with a bound raw session id can still be woken."""
    from gateway import session_context as sc

    monkeypatch.setattr(
        sc, "async_delivery_supported", lambda: False, raising=True
    )
    monkeypatch.setattr(
        ad,
        "_current_origin_session_id",
        lambda: "20260831_rawsid",
        raising=True,
    )
    ok, reason = ad.background_delivery_supported()
    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------------
# dispatch plumbing: delegation_id, runner_tid interrupt, provenance
# ---------------------------------------------------------------------------

def test_dispatch_honors_caller_minted_delegation_id():
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: {"status": "completed", "summary": "ok"},
        delegation_id="deleg_prefixed1",
        tool="delegate_agent",
        result_kind=ad.RESULT_KIND_SUBAGENT,
    )
    assert res["status"] == "dispatched"
    assert res["delegation_id"] == "deleg_prefixed1"
    evt = _drain_one()
    assert evt["delegation_id"] == "deleg_prefixed1"
    assert evt["tool"] == "delegate_agent"
    assert evt["result_kind"] == "subagent_batch"
    assert evt["background"] is True


def test_interrupt_all_sets_runner_thread_interrupt_bit():
    gate = threading.Event()
    observed = {}

    def runner():
        from tools.interrupt import is_interrupted

        gate.wait(timeout=30)
        observed["interrupted"] = is_interrupted()
        return {"status": "interrupted", "summary": None}

    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with ad._records_lock:
            tid = ad._records[res["delegation_id"]].get("runner_tid")
        if tid:
            break
        time.sleep(0.02)
    assert tid, "runner_tid was never captured"

    ad.interrupt_all(reason="test stop")
    gate.set()
    assert _drain_one() is not None
    assert observed.get("interrupted") is True


def test_dispatch_background_delegation_returns_envelope_or_rejection():
    payload = ad.dispatch_background_delegation(
        tool="delegate_claude_agent",
        result_kind=ad.RESULT_KIND_CLI_AGENT,
        goal="run claude",
        runner=lambda: {"status": "completed", "summary": "ok"},
        session_key="agent:main:cli:dm:local",
        goals=["run claude"],
    )
    assert payload["status"] == "dispatched"
    assert payload["mode"] == "background"
    assert payload["tool"] == "delegate_claude_agent"
    assert payload["result_kind"] == "cli_agent"
    assert _drain_one()["delegation_id"] == payload["delegation_id"]


def test_dispatch_background_delegation_at_capacity_starts_nothing():
    gate = threading.Event()
    held = ad.dispatch_async_delegation(
        goal="hold", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: gate.wait(30) or {"status": "completed"},
        max_async_children=1,
    )
    assert held["status"] == "dispatched"

    rejected = ad.dispatch_background_delegation(
        tool="delegate_claude_agent",
        result_kind=ad.RESULT_KIND_CLI_AGENT,
        goal="second",
        runner=lambda: {"status": "completed"},
        max_async_children=1,
    )
    gate.set()
    assert _drain_one() is not None
    assert rejected["status"] == "rejected"
    assert "capacity" in rejected["error"]
    assert "delegation_id" not in rejected


def test_register_background_delegation_makes_completion_durable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    res = ad.register_background_delegation(
        delegation_id="mission-abc123def456",
        session_key="agent:main:telegram:dm:1",
        goal="Reach out to Sam",
        tool="delegate_assistant",
        result_kind=ad.RESULT_KIND_MISSION,
    )
    assert res["status"] == "dispatched"
    assert ad.has_live_for_session(session_key="agent:main:telegram:dm:1")

    ok = ad.publish_terminal_event(
        {
            "type": "async_delegation",
            "delegation_id": "mission-abc123def456",
            "session_key": "agent:main:telegram:dm:1",
            "status": "completed",
            "summary": "Sam agreed",
            "tool": "delegate_assistant",
            "result_kind": "mission",
        },
        {"status": "completed", "summary": "Sam agreed"},
    )
    assert ok is True
    evt = _drain_one()
    assert evt["summary"] == "Sam agreed"

    durable = ad.get_durable_delegation("mission-abc123def456")
    assert durable is not None
    assert durable["delivery_state"] == "pending"
    assert durable["origin_session"] == "agent:main:telegram:dm:1"


# ---------------------------------------------------------------------------
# claim_inline_takeover — the durable cross-process exactly-once win
# ---------------------------------------------------------------------------

def test_no_row_takeover_writes_a_delivered_tombstone(tmp_path, monkeypatch):
    """A no-row win must be DURABLE, not just an in-memory "yes".

    The pre-persist race: the closing process wrote the terminal mission
    file but has not yet run its durable registration / completion persist,
    so the waiter's atomic takeover finds NO row. Returning True alone left
    nothing for the late publisher to collide with — it would then create a
    fresh pending row and deliver the outcome a SECOND time. The takeover
    now inserts a terminal ``delivered`` tombstone atomically with the win.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert ad.get_durable_delegation("deleg_tomb0001") is None

    assert ad.claim_inline_takeover("deleg_tomb0001", "completed") is True

    tomb = ad.get_durable_delegation("deleg_tomb0001")
    assert tomb is not None
    assert tomb["state"] == "completed"
    assert tomb["delivery_state"] == "delivered"

    # The late publisher resumes: the idempotent insert no-ops, the
    # completion UPDATE refuses to resurrect a delivered row, no consumer
    # can claim the queued event, and a restart replay finds nothing.
    ad.ensure_durable_delegation(
        delegation_id="deleg_tomb0001",
        session_key="agent:main:discord:thread:abc:abc",
        tool="delegate_assistant",
        result_kind=ad.RESULT_KIND_MISSION,
    )
    ad._persist_completion(
        {"type": "async_delegation", "delegation_id": "deleg_tomb0001",
         "status": "completed", "summary": "late rail copy"},
        {"status": "completed", "summary": "late rail copy"},
    )
    tomb = ad.get_durable_delegation("deleg_tomb0001")
    assert tomb["delivery_state"] == "delivered"
    assert tomb["result"] is None  # the resurrection really did not land
    assert ad.claim_completion_delivery("deleg_tomb0001", "rail-consumer") is False
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 0
    assert replay.empty()


def test_register_inline_wait_creates_durable_external_row(tmp_path, monkeypatch):
    """A foreground mission wait registers its row BEFORE blocking.

    The wait's durable ``running`` row is what a cross-process takeover
    retires atomically, and it must be marked ``external`` so a restart
    never classifies the still-live mission as outcome-unknown.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad.register_inline_wait(
        "deleg_reg00001",
        session_key="agent:main:discord:thread:abc:abc",
        origin_session_id="sess-1",
        tool="delegate_assistant",
        result_kind=ad.RESULT_KIND_MISSION,
    )

    row = ad.get_durable_delegation("deleg_reg00001")
    assert row is not None
    assert row["state"] == "running"
    assert row["delivery_state"] == "pending"
    assert row["origin_session"] == "agent:main:discord:thread:abc:abc"
    with ad._transaction() as conn:
        task = json.loads(conn.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id=?",
            ("deleg_reg00001",),
        ).fetchone()[0])
    assert task["external"] is True
    # External + running: nothing to recover, nothing to replay yet.
    assert ad.recover_abandoned_delegations() == 0
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 0

    # And the registration never overwrites a row the closing process
    # already inserted (INSERT OR IGNORE keeps the closer's row intact).
    ad.ensure_durable_delegation(
        delegation_id="deleg_reg00002",
        session_key="agent:main:discord:thread:zzz:zzz",
        goal="closer wrote this row first",
    )
    ad.register_inline_wait(
        "deleg_reg00002",
        session_key="agent:main:discord:thread:abc:abc",
        goal="waiter's copy must not land",
    )
    row2 = ad.get_durable_delegation("deleg_reg00002")
    assert row2["origin_session"] == "agent:main:discord:thread:zzz:zzz"


# ---------------------------------------------------------------------------
# Completion rendering vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kind,tool,expected",
    [
        ("cli_agent", "delegate_claude_agent", "ASYNC CLAUDE CODE RUN COMPLETE"),
        ("cloud_agent", "delegate_cursor_agent", "ASYNC CURSOR CLOUD RUN COMPLETE"),
        ("mission", "delegate_assistant", "ASSISTANT MISSION COMPLETE"),
        (None, None, "ASYNC DELEGATION COMPLETE"),
    ],
)
def test_completion_header_is_result_kind_aware(kind, tool, expected):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_render001",
        "status": "completed",
        "summary": "the outcome",
        "dispatched_at": time.time(),
        "completed_at": time.time(),
    }
    if kind:
        evt["result_kind"] = kind
    if tool:
        evt["tool"] = tool
    text = format_process_notification(evt)
    assert f"[{expected} — deleg_render001]" in text
    assert "the outcome" in text
