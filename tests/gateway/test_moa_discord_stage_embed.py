"""Behavior tests for the Discord self-editing MoA stage embed.

Covers the full seam end to end:

1. ``moa_ask`` / ``moa_debate`` publish ordered, allowlisted stage
   events onto the ``agent.moa_loop`` bus (stage ordering, terminal
   success/partial/degraded/failure classification, optional-revision run
   vs skip, sensitive-payload exclusion).
2. ``TurnRunner.send_tool_stage_embeds`` correlates events by invocation
   id: the first event of an invocation creates ONE embed, later events
   edit that SAME message, concurrent invocations never cross-edit, and
   send/edit failures fail soft (never raise, terminal state still lands).
3. The Discord adapter renders the structured event as an embed and edits
   it in place, mapping stage/status to title/color from the allowlisted
   fields only.
4. Non-Discord surfaces are unchanged: no subscriber means no events, and
   plain platform adapters do not expose the stage-embed capability the
   gateway gates on.
"""

from __future__ import annotations

import asyncio
import json
import queue as queue_mod
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.moa_loop import publish_tool_stage, subscribe_tool_stage_events
from agent.usage_pricing import CanonicalUsage
from gateway.platforms.base import SendResult
from gateway.turn_context import TurnContext


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_moa(monkeypatch):
    config = {
        "moa": {
            "default_preset": "homelab",
            "presets": {
                "homelab": {
                    "enabled": True,
                    "reference_models": [
                        {"provider": "xai-oauth", "model": "grok-4.5"},
                        {"provider": "minimax-oauth", "model": "minimax-m3"},
                        {"provider": "kimi-coding", "model": "kimi-k3"},
                    ],
                    "aggregator": {
                        "provider": "openai-codex",
                        "model": "gpt-5.6-sol",
                    },
                    "reference_max_tokens": 1000,
                }
            },
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    return config


class _StageCollector:
    """Subscribe to the stage bus for one session and record every event."""

    def __init__(self, session_id):
        self.events = []
        self._unsub = subscribe_tool_stage_events(session_id, self._record)

    def _record(self, event):
        self.events.append(event)

    def stages(self):
        return [e["stage"] for e in self.events]

    def close(self):
        self._unsub()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _install_consult_fakes(monkeypatch, outputs):
    from tools import moa_tool

    monkeypatch.setattr(
        moa_tool,
        "_run_references_parallel",
        lambda refs, msgs, **kw: [
            (f"slot-{i}", text, CanonicalUsage()) for i, text in enumerate(outputs)
        ],
    )


def _install_progress_fan_out(monkeypatch):
    """Swap in a fan-out that drives the real ``progress_callback`` contract.

    The canary label stands in for a provider:model slot label handed to the
    callback — it must never reach a stage event.
    """
    from tools import moa_tool

    def fake_run(
        refs, msgs, *, temperature=None, max_tokens=None, progress_callback=None, **_kw
    ):
        total = len(refs)
        if progress_callback is not None:
            for done in range(1, total + 1):
                progress_callback(done, total, "canary-provider:canary-model")
        return [(f"slot-{i}", f"advice {i}", CanonicalUsage()) for i in range(total)]

    monkeypatch.setattr(moa_tool, "_run_references_parallel", fake_run)


def _install_debate_fakes(monkeypatch, proposals):
    from tools import moa_debate

    critique_text = (
        "VERDICT: ANSWER_A | agree | low | none\n"
        "VERDICT: ANSWER_B | disagree | high | objection\n"
        "WOULD_ADOPT: ANSWER_A\n"
        "MANIPULATION: none\n"
    )
    revision_text = "STANCE: unchanged\nREASON: nothing moved me\nFinal."

    monkeypatch.setattr(
        moa_debate,
        "_run_references_parallel",
        lambda refs, msgs, **kw: [
            (f"slot-{i}", text, CanonicalUsage()) for i, text in enumerate(proposals)
        ],
    )

    def fake_fan_out(tasks, *, temperature=None, max_tokens=None):
        first = tasks[0][1][0]["content"] if tasks else ""
        if "Reassess your position" in first:
            texts = [revision_text] * len(tasks)
        else:
            texts = [critique_text] * len(tasks)
        return [(f"slot-{i}", t, CanonicalUsage()) for i, t in enumerate(texts)]

    monkeypatch.setattr(moa_debate, "_fan_out_per_slot", fake_fan_out)


class _RecordingStageAdapter:
    """Duck-typed stand-in for the Discord stage-embed renderer.

    Records every attempted send/edit with an ``ok`` flag; ``fail_sends`` /
    ``fail_edits`` count how many attempts of each kind fail before
    successes resume (fail-soft path).
    """

    def __init__(self, fail_sends=0, fail_edits=0):
        self.sends = []
        self.edits = []
        self.fail_sends = fail_sends
        self.fail_edits = fail_edits
        self._next_id = 100

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        if self.fail_sends > 0:
            self.fail_sends -= 1
            self.sends.append({"chat_id": chat_id, "stage": dict(stage), "ok": False})
            return SendResult(success=False, error="send boom")
        self._next_id += 1
        self.sends.append(
            {
                "chat_id": chat_id,
                "stage": dict(stage),
                "metadata": metadata,
                "reply_to": reply_to,
                "ok": True,
                "id": str(self._next_id),
            }
        )
        return SendResult(success=True, message_id=str(self._next_id))

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        ok = self.fail_edits <= 0
        if self.fail_edits > 0:
            self.fail_edits -= 1
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "stage": dict(stage),
                "metadata": metadata,
                "ok": ok,
            }
        )
        if not ok:
            return SendResult(success=False, error="edit boom")
        return SendResult(success=True, message_id=message_id)

    @property
    def ok_sends(self):
        return [s for s in self.sends if s["ok"]]

    @property
    def ok_edits(self):
        return [e for e in self.edits if e["ok"]]


def _stage_event(tool, invocation, stage, status=None, **counts):
    return {
        "type": "tool.stage",
        "tool": tool,
        "invocation_id": invocation,
        "stage": stage,
        "status": status,
        "terminal": status is not None,
        "task_id": "task-1",
        "counts": counts,
    }


def _make_stage_ctx(adapter, current):
    return TurnContext(
        source=SimpleNamespace(chat_id="chat-1"),
        _run_still_current=lambda: current["value"],
        stage_event_queue=queue_mod.Queue(),
        _stage_embed_adapter=adapter,
    )


class _StubGatewayRunner:
    def _adapter_for_source(self, source):
        return None


async def _run_drain(ctx, expected_deliveries, timeout=4.0):
    """Start the drain task, wait for the adapter to be called, stop it.

    ``expected_deliveries`` counts send+edit ATTEMPTS (successes and
    failures alike) so fail-soft paths can be asserted too.
    """
    from gateway.run import TurnRunner

    runner = TurnRunner(_StubGatewayRunner(), ctx)
    task = asyncio.create_task(runner.send_tool_stage_embeds())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        adapter = ctx._stage_embed_adapter
        if len(adapter.sends) + len(adapter.edits) >= expected_deliveries:
            break
        await asyncio.sleep(0.01)
    ctx._current_flag["value"] = False
    await asyncio.wait_for(task, timeout=2.0)


def _stage_ctx(adapter):
    current = {"value": True}
    ctx = _make_stage_ctx(adapter, current)
    ctx._current_flag = current
    return ctx


# ---------------------------------------------------------------------------
# 1. Tool-side stage emission
# ---------------------------------------------------------------------------


def test_moa_ask_stage_order_and_terminal_success(monkeypatch, configured_moa):
    from tools import moa_tool

    _install_consult_fakes(
        monkeypatch, ["challenge the assumption", "reuse the current stack", "third take"]
    )

    with _StageCollector("sess-consult-ok") as collector:
        result = json.loads(
            moa_tool.moa_ask(
                question="Which architecture?",
                session_id="sess-consult-ok",
                task_id="task-9",
            )
        )

    assert result["success"] is True
    assert collector.stages() == ["starting", "advisors", "aggregating", "complete"]

    terminal = collector.events[-1]
    assert terminal["terminal"] is True
    assert terminal["status"] == "success"
    assert terminal["task_id"] == "task-9"
    assert terminal["counts"]["advisors"] == 3
    assert terminal["counts"]["usable"] == 3
    assert terminal["counts"]["failed"] == 0
    # One correlation id for the whole invocation.
    assert len({e["invocation_id"] for e in collector.events}) == 1
    # Non-terminal stages never carry a status.
    assert all(e["status"] is None for e in collector.events[:-1])


def test_moa_ask_terminal_partial_when_one_advisor_fails(
    monkeypatch, configured_moa
):
    from tools import moa_tool

    _install_consult_fakes(
        monkeypatch,
        ["use option a", "[failed: provider down]", "use option b"],
    )

    with _StageCollector("sess-consult-partial") as collector:
        result = json.loads(moa_tool.moa_ask(question="q", session_id="sess-consult-partial"))

    assert result["partial"] is True
    assert collector.stages() == ["starting", "advisors", "aggregating", "complete"]
    assert collector.events[-1]["status"] == "partial"
    assert collector.events[-1]["counts"]["failed"] == 1


def test_moa_ask_terminal_failure_when_config_unusable(monkeypatch, configured_moa):
    from tools import moa_tool

    configured_moa["moa"]["presets"]["homelab"]["enabled"] = False

    with _StageCollector("sess-consult-fail") as collector:
        result = json.loads(moa_tool.moa_ask(question="q", session_id="sess-consult-fail"))

    assert result["success"] is False
    assert collector.stages() == ["starting", "complete"]
    assert collector.events[-1]["status"] == "failure"
    assert collector.events[-1]["terminal"] is True


def test_moa_ask_advisor_progress_emits_monotonic_advisors_stages(
    monkeypatch, configured_moa
):
    from tools import moa_tool

    _install_progress_fan_out(monkeypatch)

    with _StageCollector("sess-advisor-progress") as collector:
        result = json.loads(
            moa_tool.moa_ask(question="q", session_id="sess-advisor-progress")
        )

    assert result["success"] is True
    advisors = [e for e in collector.events if e["stage"] == "advisors"]
    assert advisors, "expected advisors stage events"
    completed = [e["counts"]["completed"] for e in advisors]
    totals = [e["counts"]["total"] for e in advisors]

    # Starts at 0, strictly increases, and reaches the configured total.
    assert completed[0] == 0
    assert all(b > a for a, b in zip(completed, completed[1:]))
    assert completed[-1] == totals[-1]
    assert set(totals) == {3}  # the configured advisor count, on every event
    assert {e["counts"]["advisors"] for e in advisors} == {3}
    assert {e["counts"]["models"] for e in advisors} == {3}
    # Live updates are non-terminal stages between the initial count and
    # aggregation — never a status, never a terminal.
    assert all(e["terminal"] is False for e in advisors)
    assert all(e["status"] is None for e in advisors)
    stages = collector.stages()
    assert stages[0] == "starting"
    assert stages[-2:] == ["aggregating", "complete"]
    assert set(stages[1:-2]) == {"advisors"}


def test_moa_ask_advisor_progress_never_carries_slot_labels(
    monkeypatch, configured_moa
):
    from tools import moa_tool

    _install_progress_fan_out(monkeypatch)

    with _StageCollector("sess-progress-labels") as collector:
        json.loads(moa_tool.moa_ask(question="q", session_id="sess-progress-labels"))

    # The fan-out hands its callback a provider:model label; the stage
    # payload must stay numeric-only.
    assert "canary-provider" not in json.dumps(collector.events)
    assert "canary-model" not in json.dumps(collector.events)
    for event in collector.events:
        for value in event["counts"].values():
            assert isinstance(value, int)


def test_moa_ask_advisor_progress_is_fail_soft_when_rendering_raises(
    monkeypatch, configured_moa
):
    from agent.moa_loop import subscribe_tool_stage_events
    from tools import moa_tool

    _install_progress_fan_out(monkeypatch)

    def exploding_subscriber(event):
        raise RuntimeError("rendering exploded")

    unsub = subscribe_tool_stage_events("sess-progress-boom", exploding_subscriber)
    try:
        result = json.loads(
            moa_tool.moa_ask(question="q", session_id="sess-progress-boom")
        )
    finally:
        unsub()

    assert result["success"] is True


def test_moa_debate_stage_order_with_revision_round(monkeypatch, configured_moa):
    from tools import moa_debate

    _install_debate_fakes(
        monkeypatch, ["answer a", "answer b", "answer c"]
    )

    with _StageCollector("sess-debate-rev") as collector:
        result = json.loads(
            moa_debate.moa_debate(
                question="Extend the proxy?",
                revision=True,
                session_id="sess-debate-rev",
            )
        )

    assert result["success"] is True
    assert collector.stages() == [
        "starting",
        "proposal",
        "critique",
        "revision",
        "aggregating",
        "complete",
    ]
    terminal = collector.events[-1]
    assert terminal["status"] == "success"
    assert terminal["terminal"] is True
    assert terminal["counts"]["rounds"] == 3
    assert terminal["counts"]["advisors"] == 3


def test_moa_debate_revision_skip_is_explicit_not_running(monkeypatch, configured_moa):
    from tools import moa_debate

    _install_debate_fakes(monkeypatch, ["answer a", "answer b", "answer c"])

    with _StageCollector("sess-debate-skip") as collector:
        result = json.loads(
            moa_debate.moa_debate(question="q", session_id="sess-debate-skip")
        )

    assert result["success"] is True
    assert result["rounds_completed"] == 2
    stages = collector.stages()
    # A skipped revision is represented explicitly and never reported as a
    # running revision stage.
    assert "revision" not in stages
    assert "revision_skipped" in stages
    assert stages.index("critique") < stages.index("revision_skipped")
    assert stages[-2:] == ["aggregating", "complete"]
    assert collector.events[-1]["status"] == "success"
    assert collector.events[-1]["counts"]["rounds"] == 2


def test_moa_debate_degraded_and_failure_terminals(monkeypatch, configured_moa):
    from tools import moa_debate

    # One usable proposal of three: the debate degrades to a single-opinion
    # consult result — terminal status must say so.
    _install_debate_fakes(
        monkeypatch, ["only answer", "[failed: boom]", "[skipped: no credits]"]
    )
    with _StageCollector("sess-debate-degraded") as collector:
        result = json.loads(
            moa_debate.moa_debate(question="q", session_id="sess-debate-degraded")
        )
    assert result["degraded"] is True
    assert collector.stages() == ["starting", "proposal", "complete"]
    assert collector.events[-1]["status"] == "degraded"

    # All proposals fail: terminal failure.
    _install_debate_fakes(
        monkeypatch, ["[failed: x]", "[failed: y]", "[failed: z]"]
    )
    with _StageCollector("sess-debate-failure") as collector:
        result = json.loads(
            moa_debate.moa_debate(question="q", session_id="sess-debate-failure")
        )
    assert result["success"] is False
    assert collector.stages() == ["starting", "proposal", "complete"]
    assert collector.events[-1]["status"] == "failure"


def test_moa_debate_partial_when_a_later_round_degrades(monkeypatch, configured_moa):
    from tools import moa_debate

    proposals = ["answer a", "answer b", "answer c"]
    _install_debate_fakes(monkeypatch, proposals)
    # Corrupt only the critique fan-out: two of three critics fail.
    from tools import moa_debate as debate_mod

    original_fan_out = debate_mod._fan_out_per_slot

    def flaky_fan_out(tasks, *, temperature=None, max_tokens=None):
        outputs = original_fan_out(tasks, temperature=temperature, max_tokens=max_tokens)
        degraded = [(lbl, "[failed: critic down]" if i < 2 else txt, acc)
                    for i, (lbl, txt, acc) in enumerate(outputs)]
        return degraded

    monkeypatch.setattr(debate_mod, "_fan_out_per_slot", flaky_fan_out)

    with _StageCollector("sess-debate-partial") as collector:
        result = json.loads(
            moa_debate.moa_debate(question="q", session_id="sess-debate-partial")
        )

    assert result["partial"] is True
    assert collector.events[-1]["status"] == "partial"
    assert collector.events[-1]["counts"]["failed"] >= 2


def test_stage_events_never_carry_prompts_evidence_or_answers(
    monkeypatch, configured_moa
):
    from tools import moa_tool

    canary_prompt = "SECRET-QUESTION-should-never-appear"
    canary_evidence = "SECRET-EVIDENCE-should-never-appear"
    answer = "SECRET-ADVISOR-ANSWER-should-never-appear"
    _install_consult_fakes(monkeypatch, [answer, "second answer", "third answer"])

    with _StageCollector("sess-canary") as collector:
        json.loads(
            moa_tool.moa_ask(
                question=canary_prompt,
                evidence=canary_evidence,
                session_id="sess-canary",
            )
        )

    assert collector.events, "expected stage events to be published"
    dumped = json.dumps(collector.events)
    assert canary_prompt not in dumped
    assert canary_evidence not in dumped
    assert answer not in dumped
    # The event payload is an allowlist — anything outside it is a leak.
    allowed_keys = {
        "type",
        "tool",
        "invocation_id",
        "stage",
        "status",
        "terminal",
        "task_id",
        "counts",
    }
    for event in collector.events:
        assert set(event.keys()) <= allowed_keys
        for value in event["counts"].values():
            assert isinstance(value, int)


def test_tools_run_unchanged_without_any_subscriber(monkeypatch, configured_moa):
    """CLI/TUI/direct calls have no subscriber: same result, no events, no errors."""
    from tools import moa_tool

    _install_consult_fakes(monkeypatch, ["advice one", "advice two", "advice three"])

    # No subscription for this session id — publish must be a silent no-op.
    result = json.loads(
        moa_tool.moa_ask(question="q", session_id="sess-unheard", task_id="t")
    )
    assert result["success"] is True

    # And with no session at all (direct/test invocation shape).
    result = json.loads(moa_tool.moa_ask(question="q"))
    assert result["success"] is True


def test_registry_dispatch_delivers_session_id_to_stage_reporter(
    monkeypatch, configured_moa
):
    """The registry handler plumbs session_id/task_id into the reporter."""
    from tools import moa_tool
    from tools.registry import registry

    _install_consult_fakes(monkeypatch, ["advice one", "advice two", "advice three"])

    with _StageCollector("sess-registry") as collector:
        raw = registry.dispatch(
            "moa_ask",
            {"question": "Which architecture?"},
            task_id="task-reg",
            session_id="sess-registry",
        )

    result = json.loads(raw)
    assert result["success"] is True
    assert collector.stages() == ["starting", "advisors", "aggregating", "complete"]
    assert all(e["task_id"] == "task-reg" for e in collector.events)


# ---------------------------------------------------------------------------
# 2. Gateway drain task: correlation, same-message edits, fail-soft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_creates_one_embed_and_edits_same_message():
    adapter = _RecordingStageAdapter()
    ctx = _stage_ctx(adapter)
    events = [
        _stage_event("moa_ask", "inv-1", "starting"),
        _stage_event("moa_ask", "inv-1", "advisors", advisors=2, models=2),
        _stage_event("moa_ask", "inv-1", "aggregating", advisors=2, usable=2),
        _stage_event("moa_ask", "inv-1", "complete", "success", advisors=2),
    ]
    for event in events:
        ctx.stage_event_queue.put_nowait(event)

    await _run_drain(ctx, expected_deliveries=len(events))

    # Exactly one send (first event) and one edit per later event — all
    # editing the SAME message the send created.
    assert len(adapter.ok_sends) == 1
    assert len(adapter.ok_edits) == 3
    sent_id = adapter.ok_sends[0]["id"]
    assert all(edit["message_id"] == sent_id for edit in adapter.ok_edits)
    assert [edit["stage"]["stage"] for edit in adapter.ok_edits] == [
        "advisors",
        "aggregating",
        "complete",
    ]
    # Terminal state landed and the embed was never queued for cleanup.
    assert adapter.ok_edits[-1]["stage"]["terminal"] is True
    assert ctx._cleanup_msg_ids == []


@pytest.mark.asyncio
async def test_drain_correlates_concurrent_invocations():
    adapter = _RecordingStageAdapter()
    ctx = _stage_ctx(adapter)
    stream_a = [
        _stage_event("moa_ask", "inv-a", "starting"),
        _stage_event("moa_ask", "inv-a", "advisors", advisors=2),
        _stage_event("moa_ask", "inv-a", "complete", "success", advisors=2),
    ]
    stream_b = [
        _stage_event("moa_debate", "inv-b", "starting"),
        _stage_event("moa_debate", "inv-b", "proposal", advisors=3),
        _stage_event("moa_debate", "inv-b", "complete", "partial", advisors=3),
    ]
    # Interleave the two invocations so a shared-message bug would mispair.
    for a, b in zip(stream_a, stream_b):
        ctx.stage_event_queue.put_nowait(a)
        ctx.stage_event_queue.put_nowait(b)

    await _run_drain(ctx, expected_deliveries=6)

    assert len(adapter.ok_sends) == 2
    ids = {send["id"] for send in adapter.ok_sends}
    assert len(ids) == 2
    edits_by_message = {}
    for edit in adapter.ok_edits:
        edits_by_message.setdefault(edit["message_id"], []).append(edit)
    assert set(edits_by_message) == ids
    # Each embed was only ever edited with its OWN invocation's events.
    for send in adapter.ok_sends:
        for edit in edits_by_message[send["id"]]:
            assert edit["stage"]["invocation_id"] == send["stage"]["invocation_id"]
    # Both terminal states landed.
    terminal_edits = [e for e in adapter.ok_edits if e["stage"]["terminal"]]
    assert {e["stage"]["status"] for e in terminal_edits} == {"success", "partial"}


@pytest.mark.asyncio
async def test_drain_edit_failure_fails_soft_and_recovers_terminal():
    adapter = _RecordingStageAdapter(fail_edits=1)
    ctx = _stage_ctx(adapter)
    events = [
        _stage_event("moa_debate", "inv-x", "starting"),
        _stage_event("moa_debate", "inv-x", "proposal", advisors=3),
        _stage_event("moa_debate", "inv-x", "complete", "failure", advisors=3),
    ]
    for event in events:
        ctx.stage_event_queue.put_nowait(event)

    # send + failed edit + recovery send — no exception may escape.
    await _run_drain(ctx, expected_deliveries=3)

    assert len(adapter.sends) == 2  # initial + recovery after failed edit
    failed = [e for e in adapter.edits if not e["ok"]]
    assert len(failed) == 1
    # The terminal event still became a visible embed.
    assert adapter.sends[-1]["stage"]["stage"] == "complete"
    assert adapter.sends[-1]["stage"]["status"] == "failure"


@pytest.mark.asyncio
async def test_drain_send_failure_fails_soft_and_later_events_retry():
    adapter = _RecordingStageAdapter(fail_sends=1)
    ctx = _stage_ctx(adapter)
    events = [
        _stage_event("moa_ask", "inv-y", "starting"),
        _stage_event("moa_ask", "inv-y", "advisors", advisors=2),
        _stage_event("moa_ask", "inv-y", "complete", "success", advisors=2),
    ]
    for event in events:
        ctx.stage_event_queue.put_nowait(event)

    await _run_drain(ctx, expected_deliveries=3)

    assert len(adapter.sends) == 2
    assert not adapter.sends[0]["ok"]
    assert adapter.sends[1]["ok"]
    # After the successful recovery send, the terminal edit hit that message.
    assert adapter.ok_edits, "terminal edit expected after recovery send"
    assert adapter.ok_edits[-1]["stage"]["terminal"] is True


@pytest.mark.asyncio
async def test_drain_noop_without_queue_or_adapter():
    from gateway.run import TurnRunner

    runner = TurnRunner(_StubGatewayRunner(), TurnContext(stage_event_queue=None))
    assert await runner.send_tool_stage_embeds() is None

    ctx = TurnContext(
        stage_event_queue=queue_mod.Queue(), _stage_embed_adapter=None
    )
    assert await TurnRunner(_StubGatewayRunner(), ctx).send_tool_stage_embeds() is None


def test_stage_event_callback_without_queue_is_silent():
    from gateway.run import TurnRunner

    ctx = TurnContext(stage_event_queue=None)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    # Must not raise even for a malformed event.
    runner.tool_stage_event_callback({"unexpected": "shape"})


def test_stage_event_callback_enqueues_only_while_run_is_current():
    from gateway.run import TurnRunner

    ctx = TurnContext(
        stage_event_queue=queue_mod.Queue(),
        _run_still_current=lambda: False,
    )
    TurnRunner(_StubGatewayRunner(), ctx).tool_stage_event_callback(
        _stage_event("moa_ask", "inv", "starting")
    )
    assert ctx.stage_event_queue.empty()


@pytest.mark.asyncio
async def test_full_path_real_tool_events_render_through_drain(
    monkeypatch, configured_moa
):
    """moa_ask -> bus -> queue -> drain -> adapter, one embed."""
    from gateway.run import TurnRunner
    from tools import moa_tool

    _install_consult_fakes(monkeypatch, ["advice one", "advice two", "advice three"])
    adapter = _RecordingStageAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)

    with _StageCollector("sess-full") as collector:
        # Route the real subscriber straight into the turn's queue.
        collector.close()
        unsub = subscribe_tool_stage_events(
            "sess-full", runner.tool_stage_event_callback
        )
        try:
            task = asyncio.create_task(runner.send_tool_stage_embeds())
            result = json.loads(
                moa_tool.moa_ask(
                    question="Which architecture?",
                    session_id="sess-full",
                    task_id="task-full",
                )
            )
            assert result["success"] is True
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if len(adapter.sends) + len(adapter.edits) >= 4:
                    break
                await asyncio.sleep(0.01)
        finally:
            unsub()
            ctx._current_flag["value"] = False
            await asyncio.wait_for(task, timeout=2.0)

    assert len(adapter.ok_sends) == 1
    assert len(adapter.ok_edits) == 3
    assert adapter.ok_edits[-1]["stage"]["status"] == "success"
    delivered_stages = [adapter.ok_sends[0]["stage"]["stage"]] + [
        e["stage"]["stage"] for e in adapter.ok_edits
    ]
    assert delivered_stages == ["starting", "advisors", "aggregating", "complete"]


class _BlockingTerminalStageAdapter(_RecordingStageAdapter):
    """Blocks terminal send/edit until ``release`` is set; no ok record before block."""

    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def _maybe_block(self, stage):
        if stage.get("terminal"):
            self.blocked.set()
            await self.release.wait()

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        await self._maybe_block(stage)
        return await super().send_tool_stage_embed(
            chat_id, stage, metadata=metadata, reply_to=reply_to
        )

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        await self._maybe_block(stage)
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )


class _PostSideEffectBlockAdapter(_RecordingStageAdapter):
    """Records a successful terminal side effect, then blocks before SendResult."""

    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.terminal_successful_side_effects = 0

    async def _record_terminal_side_effect_and_block(
        self, chat_id, stage, *, message_id=None, metadata=None, reply_to=None
    ):
        self._next_id += 1
        msg_id = message_id or str(self._next_id)
        record = {
            "chat_id": chat_id,
            "stage": dict(stage),
            "metadata": metadata,
            "ok": True,
            "id": msg_id,
        }
        if message_id is None:
            record["reply_to"] = reply_to
            self.sends.append(record)
        else:
            record["message_id"] = message_id
            self.edits.append(record)
        self.terminal_successful_side_effects += 1
        self.blocked.set()
        await self.release.wait()
        return SendResult(success=True, message_id=msg_id)

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        if stage.get("terminal"):
            return await self._record_terminal_side_effect_and_block(
                chat_id, stage, metadata=metadata, reply_to=reply_to
            )
        return await super().send_tool_stage_embed(
            chat_id, stage, metadata=metadata, reply_to=reply_to
        )

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        if stage.get("terminal"):
            return await self._record_terminal_side_effect_and_block(
                chat_id, stage, message_id=message_id, metadata=metadata
            )
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )


class _BlockingFailureTerminalAdapter(_RecordingStageAdapter):
    """Blocks a terminal send/edit until release, then reports adapter failure."""

    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def _fail_terminal_after_block(self, chat_id, stage, *, message_id=None):
        self.blocked.set()
        await self.release.wait()
        record = {"chat_id": chat_id, "stage": dict(stage), "ok": False}
        if message_id is None:
            self.sends.append(record)
        else:
            record["message_id"] = message_id
            self.edits.append(record)
        return SendResult(success=False, error="terminal boom")

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        if stage.get("terminal"):
            return await self._fail_terminal_after_block(chat_id, stage)
        return await super().send_tool_stage_embed(
            chat_id, stage, metadata=metadata, reply_to=reply_to
        )

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        if stage.get("terminal"):
            return await self._fail_terminal_after_block(
                chat_id, stage, message_id=message_id
            )
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )


class _BlockingRaiseTerminalAdapter(_RecordingStageAdapter):
    """Blocks a terminal send/edit until release, then raises."""

    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def _raise_terminal_after_block(self, chat_id, stage, *, message_id=None):
        self.blocked.set()
        await self.release.wait()
        record = {"chat_id": chat_id, "stage": dict(stage), "ok": False}
        if message_id is None:
            self.sends.append(record)
        else:
            record["message_id"] = message_id
            self.edits.append(record)
        raise RuntimeError("terminal boom")

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        if stage.get("terminal"):
            await self._raise_terminal_after_block(chat_id, stage)
        return await super().send_tool_stage_embed(
            chat_id, stage, metadata=metadata, reply_to=reply_to
        )

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        if stage.get("terminal"):
            await self._raise_terminal_after_block(
                chat_id, stage, message_id=message_id
            )
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )


def _successful_terminal_deliveries(adapter):
    return [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-cancel-inflight"
    ]


class _FastFailTerminalAdapter(_RecordingStageAdapter):
    """Terminal send/edit fails immediately with SendResult(success=False)."""

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        if stage.get("terminal"):
            self.sends.append({"chat_id": chat_id, "stage": dict(stage), "ok": False})
            return SendResult(success=False, error="terminal boom")
        return await super().send_tool_stage_embed(
            chat_id, stage, metadata=metadata, reply_to=reply_to
        )

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        if stage.get("terminal"):
            self.edits.append(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "stage": dict(stage),
                    "ok": False,
                }
            )
            return SendResult(success=False, error="terminal boom")
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )


@pytest.mark.asyncio
async def test_drain_cancel_delivers_inflight_terminal():
    """Cancel during a blocked terminal send/edit must not drop the dequeued event."""
    from gateway.run import TurnRunner

    adapter = _BlockingTerminalStageAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-cancel-inflight", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-cancel-inflight",
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)

    task.cancel()
    adapter.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    terminal_deliveries = _successful_terminal_deliveries(adapter)
    assert len(terminal_deliveries) == 1
    assert terminal_deliveries[0]["stage"]["status"] == "success"
    other_terminal = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") != "inv-cancel-inflight"
    ]
    assert other_terminal == []


@pytest.mark.asyncio
async def test_drain_cancel_after_terminal_side_effect_is_exactly_once():
    """Post-side-effect/pre-return cancel must not duplicate the terminal embed."""
    from gateway.run import TurnRunner

    adapter = _PostSideEffectBlockAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-post-side-effect", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-post-side-effect",
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)

    task.cancel()
    adapter.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.terminal_successful_side_effects == 1
    terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-post-side-effect"
    ]
    assert len(terminal_deliveries) == 1
    duplicate_terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") != "inv-post-side-effect"
    ]
    assert duplicate_terminal_deliveries == []


@pytest.mark.asyncio
async def test_drain_double_cancel_is_exactly_once():
    """Nested cancel during shield recovery must not start a duplicate delivery."""
    from gateway.run import TurnRunner

    adapter = _PostSideEffectBlockAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-double-cancel", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-double-cancel",
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)

    task.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()

    adapter.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.terminal_successful_side_effects == 1
    terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-double-cancel"
    ]
    assert len(terminal_deliveries) == 1
    duplicate_terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") != "inv-double-cancel"
    ]
    assert duplicate_terminal_deliveries == []


class _WedgedTerminalAdapter(_RecordingStageAdapter):
    """Blocks terminal send/edit until release; side effect only after release."""

    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def _block_then_deliver(
        self, chat_id, stage, *, message_id=None, metadata=None, reply_to=None
    ):
        self.blocked.set()
        await self.release.wait()
        if message_id is None:
            return await super().send_tool_stage_embed(
                chat_id, stage, metadata=metadata, reply_to=reply_to
            )
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        if stage.get("terminal"):
            return await self._block_then_deliver(
                chat_id, stage, metadata=metadata, reply_to=reply_to
            )
        return await super().send_tool_stage_embed(
            chat_id, stage, metadata=metadata, reply_to=reply_to
        )

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        if stage.get("terminal"):
            return await self._block_then_deliver(
                chat_id, stage, message_id=message_id, metadata=metadata
            )
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )


class _WedgedIntermediateAdapter(_RecordingStageAdapter):
    """Blocks a non-terminal intermediate stage until release."""

    WEDGE_STAGE = "advisors"

    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def _block_then_deliver(
        self, chat_id, stage, *, message_id=None, metadata=None, reply_to=None
    ):
        self.blocked.set()
        await self.release.wait()
        if message_id is None:
            return await super().send_tool_stage_embed(
                chat_id, stage, metadata=metadata, reply_to=reply_to
            )
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )

    async def send_tool_stage_embed(self, chat_id, stage, metadata=None, reply_to=None):
        if stage.get("stage") == self.WEDGE_STAGE:
            return await self._block_then_deliver(
                chat_id, stage, metadata=metadata, reply_to=reply_to
            )
        return await super().send_tool_stage_embed(
            chat_id, stage, metadata=metadata, reply_to=reply_to
        )

    async def edit_tool_stage_embed(self, chat_id, message_id, stage, metadata=None):
        if stage.get("stage") == self.WEDGE_STAGE:
            return await self._block_then_deliver(
                chat_id, stage, message_id=message_id, metadata=metadata
            )
        return await super().edit_tool_stage_embed(
            chat_id, message_id, stage, metadata=metadata
        )


@pytest.mark.asyncio
async def test_drain_unknown_pin_parks_then_delivers_terminal_once(monkeypatch):
    """Intermediate UNKNOWN pin must not drop a later terminal stage event."""
    from gateway.run import TurnRunner

    monkeypatch.setattr(TurnRunner, "_STAGE_DELIVERY_RECOVER_TIMEOUT", 0.2)

    adapter = _WedgedIntermediateAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    invocation = "inv-parked-terminal"
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", invocation, "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", invocation, "advisors", advisors=2)
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            invocation,
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)

    await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)
    assert task.done() is False

    intermediate_attempts_while_wedged = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("stage") == _WedgedIntermediateAdapter.WEDGE_STAGE
        and record["stage"].get("invocation_id") == invocation
    ]
    assert len(intermediate_attempts_while_wedged) == 0

    terminal_attempts_while_wedged = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == invocation
    ]
    assert terminal_attempts_while_wedged == []

    ctx._current_flag["value"] = False
    adapter.release.set()
    await asyncio.wait_for(task, timeout=2.0)

    intermediate_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("stage") == _WedgedIntermediateAdapter.WEDGE_STAGE
        and record["stage"].get("invocation_id") == invocation
    ]
    assert len(intermediate_deliveries) == 1

    terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == invocation
    ]
    assert len(terminal_deliveries) == 1
    assert terminal_deliveries[-1]["stage"]["stage"] == "complete"


@pytest.mark.asyncio
async def test_drain_harvest_timeout_pins_delivery_no_retry(monkeypatch):
    """Harvest timeout pins the live task; no duplicate retry."""
    from gateway.run import TurnRunner

    monkeypatch.setattr(TurnRunner, "_STAGE_DELIVERY_RECOVER_TIMEOUT", 0.2)

    adapter = _WedgedTerminalAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-harvest-timeout", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-harvest-timeout",
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)

    await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)
    assert task.done() is False

    terminal_attempts_while_wedged = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-harvest-timeout"
    ]
    assert terminal_attempts_while_wedged == []

    detached = getattr(runner, "_stage_delivery_detached_tasks", set())
    pinned = [t for t in detached if not t.done()]
    assert len(pinned) == 1

    ctx._current_flag["value"] = False
    adapter.release.set()
    await asyncio.wait_for(task, timeout=2.0)

    terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-harvest-timeout"
    ]
    assert len(terminal_deliveries) == 1


@pytest.mark.asyncio
async def test_stop_drain_unknown_pin_bounded_returns_without_teardown_block(
    monkeypatch,
):
    """Stop drain must return on UNKNOWN pin within _STAGE_STOP_DRAIN_TIMEOUT."""
    from gateway.run import TurnRunner

    monkeypatch.setattr(TurnRunner, "_STAGE_DELIVERY_RECOVER_TIMEOUT", 0.2)
    monkeypatch.setattr(TurnRunner, "_STAGE_STOP_DRAIN_TIMEOUT", 0.3)

    adapter = _WedgedIntermediateAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    invocation = "inv-stop-drain-timeout"
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", invocation, "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", invocation, "advisors", advisors=2)
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            invocation,
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    verify_no_exception_leak = False
    try:
        await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)

        await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)
        assert task.done() is False

        ctx._current_flag["value"] = False
        await asyncio.wait_for(task, timeout=3.0)

        intermediate_side_effects = [
            record
            for record in adapter.ok_sends + adapter.ok_edits
            if record.get("ok")
            and record["stage"].get("stage") == _WedgedIntermediateAdapter.WEDGE_STAGE
            and record["stage"].get("invocation_id") == invocation
        ]
        assert intermediate_side_effects == []
        assert adapter.blocked.is_set()

        intermediate_attempts = [
            record
            for record in adapter.sends + adapter.edits
            if record["stage"].get("stage") == _WedgedIntermediateAdapter.WEDGE_STAGE
            and record["stage"].get("invocation_id") == invocation
        ]
        assert len(intermediate_attempts) == 0

        terminal_attempts = [
            record
            for record in adapter.sends + adapter.edits
            if record["stage"].get("terminal")
            and record["stage"].get("invocation_id") == invocation
        ]
        assert terminal_attempts == []

        detached = getattr(runner, "_stage_delivery_detached_tasks", set())
        pinned = [t for t in detached if not t.done()]
        assert len(detached) == 1
        assert len(pinned) == 1
        verify_no_exception_leak = True
    finally:
        adapter.release.set()
        detached = getattr(runner, "_stage_delivery_detached_tasks", None) or set()
        for delivery_task in list(detached):
            if not delivery_task.done():
                try:
                    await asyncio.wait_for(delivery_task, timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    if verify_no_exception_leak:
        detached = getattr(runner, "_stage_delivery_detached_tasks", set())
        for delivery_task in detached:
            assert delivery_task.done()
            assert delivery_task.exception() is None


@pytest.mark.asyncio
async def test_drain_unknown_timeout_pins_delivery_no_retry(monkeypatch):
    """Cancel with a live delivery pins UNKNOWN immediately; no duplicate retry."""
    from gateway.run import TurnRunner

    monkeypatch.setattr(TurnRunner, "_STAGE_DELIVERY_RECOVER_TIMEOUT", 0.2)

    adapter = _WedgedTerminalAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-unknown", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-unknown",
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    terminal_attempts_while_wedged = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-unknown"
    ]
    assert terminal_attempts_while_wedged == []

    detached = getattr(runner, "_stage_delivery_detached_tasks", set())
    pinned = [t for t in detached if not t.done()]
    assert len(pinned) == 1

    adapter.release.set()
    await asyncio.wait_for(pinned[0], timeout=2.0)

    terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-unknown"
    ]
    assert len(terminal_deliveries) == 1


@pytest.mark.asyncio
async def test_drain_terminal_failure_retries_exactly_once_in_normal_path():
    """Known terminal failure in the normal path is retried exactly once."""
    adapter = _FastFailTerminalAdapter()
    ctx = _stage_ctx(adapter)
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-normal-retry", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-normal-retry",
            "complete",
            "success",
            advisors=2,
        )
    )

    await _run_drain(ctx, expected_deliveries=3)

    terminal_attempts = [
        record
        for record in adapter.sends + adapter.edits
        if record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-normal-retry"
    ]
    assert len(terminal_attempts) == 2
    assert all(not record.get("ok") for record in terminal_attempts)


@pytest.mark.asyncio
async def test_drain_terminal_second_failure_has_no_third_attempt():
    """FAILED_FINAL after attempt 2 must not spawn a third delivery."""
    adapter = _FastFailTerminalAdapter()
    ctx = _stage_ctx(adapter)
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-no-third", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-no-third",
            "complete",
            "success",
            advisors=2,
        )
    )

    await _run_drain(ctx, expected_deliveries=3)

    terminal_attempts = [
        record
        for record in adapter.sends + adapter.edits
        if record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-no-third"
    ]
    assert len(terminal_attempts) == 2


@pytest.mark.asyncio
async def test_drain_cancel_while_blocked_creates_no_new_deliveries():
    """Cancellation flush must not claim or deliver queued events."""
    from gateway.run import TurnRunner

    adapter = _BlockingTerminalStageAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    claim_calls = {"count": 0}
    real_claim = runner._claim_tool_stage_delivery

    def counting_claim(*args, **kwargs):
        claim_calls["count"] += 1
        return real_claim(*args, **kwargs)

    runner._claim_tool_stage_delivery = counting_claim

    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-cancel-no-flush", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask",
            "inv-cancel-no-flush",
            "complete",
            "success",
            advisors=2,
        )
    )

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(adapter.blocked.wait(), timeout=2.0)
    claims_before_cancel = claim_calls["count"]
    attempts_before_cancel = len(adapter.sends) + len(adapter.edits)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert claim_calls["count"] == claims_before_cancel
    assert len(adapter.sends) + len(adapter.edits) == attempts_before_cancel

    adapter.release.set()
    detached = getattr(runner, "_stage_delivery_detached_tasks", set())
    pinned = [t for t in detached if not t.done()]
    if pinned:
        await asyncio.wait_for(pinned[0], timeout=2.0)

    terminal_deliveries = [
        record
        for record in adapter.ok_sends + adapter.ok_edits
        if record.get("ok")
        and record["stage"].get("terminal")
        and record["stage"].get("invocation_id") == "inv-cancel-no-flush"
    ]
    assert len(terminal_deliveries) == 1
    assert claim_calls["count"] == claims_before_cancel


@pytest.mark.asyncio
async def test_drain_cancel_during_idle_sleep_creates_no_deliveries(monkeypatch):
    """Teardown cancel during idle sleep must not create deliveries from the queue."""
    from gateway.run import TurnRunner

    adapter = _RecordingStageAdapter()
    ctx = _stage_ctx(adapter)
    runner = TurnRunner(_StubGatewayRunner(), ctx)
    real_sleep = asyncio.sleep
    sleep_entered = asyncio.Event()

    async def slow_sleep(delay):
        if delay == 0.2:
            sleep_entered.set()
            await real_sleep(10)
        else:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", slow_sleep)

    task = asyncio.create_task(runner.send_tool_stage_embeds())
    await asyncio.wait_for(sleep_entered.wait(), timeout=2.0)

    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-cancel", "starting")
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-cancel", "complete", "success", advisors=2)
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.ok_sends == []
    assert adapter.ok_edits == []


@pytest.mark.asyncio
async def test_drain_stop_path_delivers_terminal_not_discarding():
    """When the turn stops, flush terminal state instead of discarding the queue."""
    from gateway.run import TurnRunner

    adapter = _RecordingStageAdapter()
    ctx = _stage_ctx(adapter)
    ctx.stage_event_queue.put_nowait(_stage_event("moa_ask", "inv-stop", "starting"))
    ctx.stage_event_queue.put_nowait(
        _stage_event("moa_ask", "inv-stop", "advisors", advisors=2)
    )
    ctx.stage_event_queue.put_nowait(
        _stage_event(
            "moa_ask", "inv-stop", "complete", "partial", advisors=2, failed=1
        )
    )
    ctx._current_flag["value"] = False

    await TurnRunner(_StubGatewayRunner(), ctx).send_tool_stage_embeds()

    assert len(adapter.ok_sends) == 1
    assert adapter.ok_sends[0]["stage"]["stage"] == "complete"
    assert adapter.ok_sends[0]["stage"]["status"] == "partial"
    assert len(adapter.ok_edits) == 0


def test_moa_ask_unexpected_exception_still_reports_terminal_failure(
    monkeypatch, configured_moa
):
    from tools import moa_tool

    _install_consult_fakes(monkeypatch, ["a", "b", "c"])

    def boom(**kwargs):
        raise RuntimeError("unexpected post-processing")

    monkeypatch.setattr(moa_tool, "tool_result", boom)

    with _StageCollector("sess-boom") as collector:
        with pytest.raises(RuntimeError, match="unexpected"):
            moa_tool.moa_ask(question="q", session_id="sess-boom")

    terminals = [e for e in collector.events if e.get("terminal")]
    assert len(terminals) == 1
    assert terminals[0]["status"] == "failure"
    assert collector.stages()[-1] == "complete"


def test_moa_debate_unexpected_exception_still_reports_terminal_failure(
    monkeypatch, configured_moa
):
    from tools import moa_debate

    _install_debate_fakes(monkeypatch, ["answer a", "answer b", "answer c"])

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected agreement")

    monkeypatch.setattr(moa_debate, "_derive_agreement", boom)

    with _StageCollector("sess-debate-boom") as collector:
        with pytest.raises(RuntimeError, match="unexpected"):
            moa_debate.moa_debate(question="q", session_id="sess-debate-boom")

    terminals = [e for e in collector.events if e.get("terminal")]
    assert len(terminals) == 1
    assert terminals[0]["status"] == "failure"
    assert collector.stages()[-1] == "complete"


def test_nested_subscribe_unsubscribe_restores_previous():
    events_a = []
    events_b = []

    unsub_a = subscribe_tool_stage_events("sess-nested", events_a.append)
    unsub_b = subscribe_tool_stage_events("sess-nested", events_b.append)

    publish_tool_stage("sess-nested", {"invocation_id": "b"})
    assert events_b == [{"invocation_id": "b"}]
    assert events_a == []

    unsub_b()
    publish_tool_stage("sess-nested", {"invocation_id": "a"})
    assert events_a == [{"invocation_id": "a"}]
    assert events_b == [{"invocation_id": "b"}]

    unsub_a()
    publish_tool_stage("sess-nested", {"invocation_id": "orphan"})
    assert events_a == [{"invocation_id": "a"}]
    assert events_b == [{"invocation_id": "b"}]


# ---------------------------------------------------------------------------
# 3. Discord adapter rendering
# ---------------------------------------------------------------------------


def _make_discord_stage_adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = {}
    edited = {}

    async def _fake_send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=4242)

    async def _fake_edit(**kwargs):
        edited.update(kwargs)

    channel = SimpleNamespace(
        send=AsyncMock(side_effect=_fake_send),
        get_partial_message=lambda mid: SimpleNamespace(edit=_fake_edit),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel,
        fetch_channel=AsyncMock(),
    )
    return adapter, sent, edited


@pytest.mark.asyncio
async def test_discord_send_and_edit_stage_embed_same_message():
    adapter, sent, edited = _make_discord_stage_adapter()

    running = _stage_event("moa_ask", "inv-abc", "advisors", advisors=2, models=2)
    result = await adapter.send_tool_stage_embed("555", running, reply_to=None)
    assert result.success is True
    assert result.message_id == "4242"
    embed = sent["embed"]
    assert "moa_ask" in embed.title
    assert "advisors running" in embed.title
    assert "2 advisors" in embed.description
    assert "inv-abc"[:8] in embed.footer["text"]

    terminal = _stage_event("moa_ask", "inv-abc", "complete", "success", advisors=2)
    edit_result = await adapter.edit_tool_stage_embed("555", "4242", terminal)
    assert edit_result.success is True
    assert edited["embed"].title.startswith("moa_ask — ✅ complete")


@pytest.mark.asyncio
async def test_discord_stage_embed_terminal_marks_and_colors():
    adapter, sent, _ = _make_discord_stage_adapter()
    from plugins.platforms.discord import adapter as discord_adapter_mod

    cases = [
        ("success", "✅", "green"),
        ("partial", "⚠️", "orange"),
        ("degraded", "⚠️", "orange"),
        ("failure", "❌", "red"),
    ]
    for status, mark, color_name in cases:
        event = _stage_event("moa_debate", "inv-1", "complete", status, advisors=3)
        await adapter.send_tool_stage_embed("555", event)
        embed = sent["embed"]
        assert mark in embed.title, status
        expected_color = getattr(discord_adapter_mod.discord.Color, color_name)()
        assert embed.color == expected_color, status


@pytest.mark.asyncio
async def test_discord_stage_embed_renders_only_allowlisted_fields():
    adapter, sent, _ = _make_discord_stage_adapter()

    event = _stage_event("moa_ask", "inv-2", "advisors", advisors=2)
    # A hostile/buggy extra key must never reach the rendered embed.
    event["prompt"] = "SECRET-PROMPT-never-render"
    event["raw_args"] = {"evidence": "SECRET-EVIDENCE-never-render"}

    await adapter.send_tool_stage_embed("555", event)
    embed = sent["embed"]
    rendered = " ".join(
        [str(embed.title), str(embed.description), str(embed.footer["text"])]
    )
    assert "SECRET-PROMPT-never-render" not in rendered
    assert "SECRET-EVIDENCE-never-render" not in rendered


@pytest.mark.asyncio
async def test_discord_stage_embeds_fail_soft_when_disconnected():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = None
    event = _stage_event("moa_ask", "inv-3", "starting")

    result = await adapter.send_tool_stage_embed("555", event)
    assert result.success is False

    result = await adapter.edit_tool_stage_embed("555", "1", event)
    assert result.success is False


def test_tool_stage_appearance_maps_all_documented_stages():
    from plugins.platforms.discord.adapter import _tool_stage_appearance

    # Consult stages render a human label; terminal statuses a marked title.
    title, desc, color = _tool_stage_appearance(
        "moa_ask", "starting", None, {}
    )
    assert title == "moa_ask — starting"
    assert color == "running"

    title, _, color = _tool_stage_appearance("moa_ask", "complete", "partial", {"failed": 1})
    assert "⚠️ partial" in title
    assert color == "warn"

    # Unknown tool/stage falls back to a bounded neutral rendering instead
    # of crashing or leaking anything.
    title, desc, _ = _tool_stage_appearance("future_tool", "weird_stage", None, {"advisors": 5})
    assert "future_tool" in title
    assert "5 advisors" in desc

    # Titles are capped so a pathological stage id cannot blow the embed.
    title, _, _ = _tool_stage_appearance("t", "x" * 500, None, {})
    assert len(title) <= 100

    # Counts render only when safely present — strings and bools are dropped.
    _, desc, _ = _tool_stage_appearance("moa_ask", "advisors", None, {"advisors": 2, "junk": "text"})
    assert "2 advisors" in desc
    assert "junk" not in desc


def test_discord_stage_renders_advisor_completion_fraction():
    from plugins.platforms.discord.adapter import _tool_stage_appearance

    title, description, color = _tool_stage_appearance(
        "moa_ask",
        "advisors",
        None,
        {"advisors": 4, "models": 4, "completed": 2, "total": 4},
    )
    assert title == "moa_ask — advisors running"
    assert description == "2/4 advisors complete · 4 models"
    assert color == "running"

    # Nothing completed yet still shows the live total.
    _, initial, _ = _tool_stage_appearance(
        "moa_ask", "advisors", None,
        {"advisors": 3, "models": 2, "completed": 0, "total": 3},
    )
    assert initial == "0/3 advisors complete · 2 models"


def test_discord_stage_fraction_only_renders_with_both_counts_valid():
    from plugins.platforms.discord.adapter import _tool_stage_appearance

    # A missing, non-numeric, or inconsistent half falls back to the plain
    # per-key summary — never a half-rendered fraction.
    invalid_counts = [
        {"advisors": 4, "completed": 2},  # no total
        {"advisors": 4, "total": 4},  # no completed
        {"advisors": 4, "completed": "2", "total": 4},
        {"advisors": 4, "completed": True, "total": 4},
        {"advisors": 4, "completed": 5, "total": 4},  # over total
        {"advisors": 4, "completed": 2, "total": 0},
    ]
    for counts in invalid_counts:
        _, description, _ = _tool_stage_appearance("moa_ask", "advisors", None, counts)
        assert "advisors complete" not in description, counts
        assert "4 advisors" in description, counts


def test_discord_advisor_fraction_is_exclusive_to_moa_ask_advisors_stage():
    """Only moa_ask's running advisors stage may say ``N/T advisors complete``.

    ``completed``/``total`` are generic count names a future or unknown tool
    could easily publish. Regression: any other tool, any other stage of a
    known tool, or a terminal event carrying valid integer counts must render
    the plain allowlisted summary — never the advisor-progress label.
    """
    from plugins.platforms.discord.adapter import _tool_stage_appearance

    live_counts = {"advisors": 4, "models": 2, "completed": 2, "total": 4}
    collisions = [
        ("future_tool", "advisors", None),  # unknown tool, borrowed stage id
        ("future_tool", "weird_stage", None),  # unknown tool and stage
        ("moa_debate", "advisors", None),  # known tool, not an advisors publisher
        ("moa_ask", "aggregating", None),  # another stage of the owning tool
        ("moa_ask", "advisors", "success"),  # terminal event on the owning stage
        ("moa_ask", "complete", "success"),  # terminal stage, valid counts
    ]
    for tool, stage, status in collisions:
        _, description, _ = _tool_stage_appearance(tool, stage, status, live_counts)
        assert "advisors complete" not in description, (tool, stage, status)
        assert "2/4" not in description, (tool, stage, status)
        assert "4 advisors" in description, (tool, stage, status)

    # The one intended pair keeps its live-progress rendering.
    _, description, _ = _tool_stage_appearance(
        "moa_ask", "advisors", None, dict(live_counts)
    )
    assert description == "2/4 advisors complete · 2 models"


@pytest.mark.asyncio
async def test_discord_advisor_progress_edits_same_embed():
    adapter, sent, edited = _make_discord_stage_adapter()

    first = _stage_event(
        "moa_ask", "inv-live", "advisors", advisors=4, models=4, completed=0, total=4
    )
    result = await adapter.send_tool_stage_embed("555", first)
    assert result.success is True
    assert "0/4 advisors complete" in sent["embed"].description

    for done in (1, 2, 3, 4):
        event = _stage_event(
            "moa_ask",
            "inv-live",
            "advisors",
            advisors=4,
            models=4,
            completed=done,
            total=4,
        )
        edit = await adapter.edit_tool_stage_embed("555", "4242", event)
        assert edit.success is True

    assert edited["embed"].description == "4/4 advisors complete · 4 models"


def test_plain_platform_adapters_do_not_expose_stage_embed_capability():
    """The gateway gate selects Discord only: the base adapter must not
    grow the renderer, so Telegram/Slack/etc. never match the hasattr check."""
    from gateway.platforms.base import BasePlatformAdapter

    assert not hasattr(BasePlatformAdapter, "send_tool_stage_embed")
    assert not hasattr(BasePlatformAdapter, "edit_tool_stage_embed")
