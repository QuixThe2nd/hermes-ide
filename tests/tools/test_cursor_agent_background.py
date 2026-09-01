"""``delegate_cursor_agent(background=true)`` — the uniform background branch.

Focused acceptance tests for the cloud-agent half of the uniform delegation
lifecycle, plus the receipt spine (R7): a background call's tool result is the
acceptance envelope, never the run's outcome, so a restart must re-arm the
poll under the SAME ``delegation_id`` instead of skipping recovery.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from tests.tools.fixtures.fake_cursor_cloud import FakeCursorCloud
from tools import async_delegation as ad
from tools import cursor_agent_tool
from tools.cursor_run_receipts import (
    create_receipt,
    cursor_runs_dir,
    find_receipt_for_binding,
    hash_prompt,
    read_receipt,
    receipt_path_for_binding,
)

from tools.process_registry import format_process_notification, process_registry


@pytest.fixture
def cloud_env(monkeypatch, tmp_path):
    """A fake Cursor Cloud plus an isolated Hermes home for receipts."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    cloud = FakeCursorCloud()
    cloud.install(monkeypatch, cursor_agent_tool, tmp_path=tmp_path)

    # The real helper shells out to git; the fake cloud pins the origin
    # already, and a subprocess here trips the conftest Popen guard.
    monkeypatch.setattr(
        cursor_agent_tool, "detect_unpushed_head_commits", lambda workdir: None
    )

    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()

    workdir = tmp_path / "repo"
    workdir.mkdir()
    yield cloud, workdir, home

    deadline = time.monotonic() + 5.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _session_ids() -> "tuple[str, str]":
    return str(uuid.uuid4()), str(uuid.uuid4())


def _patch_delivery(monkeypatch, supported: bool):
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: supported
    )
    if not supported:
        monkeypatch.setattr(
            "tools.async_delegation._current_origin_session_id", lambda: ""
        )


def _drain_one(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


# ---------------------------------------------------------------------------
# Foreground default: inline result, zero queue events
# ---------------------------------------------------------------------------

def test_foreground_returns_inline_result_and_emits_no_event(cloud_env):
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()

    out = cursor_agent_tool.delegate_cursor_agent(
        task="small job",
        workdir=str(workdir.resolve()),
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["final_report"] == "cloud done"
    assert "delegation_id" not in parsed
    assert parsed.get("mode") != "background"
    assert cloud.create_calls == 1
    # Exactly one delivery channel: the foreground run never touches the rail.
    assert process_registry.completion_queue.empty()


def test_schema_advertises_background_default_false():
    from tools.registry import registry

    entry = registry.get_entry("delegate_cursor_agent")
    prop = entry.schema["parameters"]["properties"]["background"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert "Blocking by default" in prop["description"]


def test_handler_forwards_background_argument(monkeypatch, tmp_path):
    seen = {}

    def _capture(*a, **kw):
        seen.update(kw)
        return "{}"

    monkeypatch.setattr(cursor_agent_tool, "delegate_cursor_agent", _capture)

    cursor_agent_tool._handle_delegate_cursor_agent(
        {"task": "t", "workdir": str(tmp_path), "background": True},
        session_id="s",
        tool_call_id="c",
        task_id="tk",
    )
    assert seen["background"] is True

    seen.clear()
    cursor_agent_tool._handle_delegate_cursor_agent(
        {"task": "t", "workdir": str(tmp_path)},
        session_id="s",
        tool_call_id="c",
        task_id="tk",
    )
    assert seen["background"] is False


# ---------------------------------------------------------------------------
# Background: shared envelope + exactly one terminal event
# ---------------------------------------------------------------------------

def test_background_returns_envelope_then_one_terminal_event(cloud_env):
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()

    out = cursor_agent_tool.delegate_cursor_agent(
        task="small job",
        workdir=str(workdir.resolve()),
        session_id=session_id,
        tool_call_id=tool_call_id,
        background=True,
    )
    envelope = json.loads(out)

    assert envelope["status"] == "dispatched"
    assert envelope["mode"] == "background"
    assert envelope["tool"] == "delegate_cursor_agent"
    assert envelope["result_kind"] == "cloud_agent"
    assert envelope["delegation_id"].startswith("deleg_")
    assert envelope["count"] == 1
    assert "final_report" not in envelope
    assert "success" not in envelope

    evt = _drain_one()
    assert evt is not None, "background cloud run never produced a terminal event"
    assert evt["delegation_id"] == envelope["delegation_id"]
    assert evt["status"] == "completed"
    assert evt["summary"] == "cloud done"
    assert evt["tool"] == "delegate_cursor_agent"
    assert evt["result_kind"] == "cloud_agent"
    assert evt["background"] is True
    assert evt["child_session_id"]
    assert process_registry.completion_queue.empty()

    rendered = format_process_notification(evt)
    assert "ASYNC CURSOR CLOUD RUN COMPLETE" in rendered
    assert "cloud done" in rendered


def test_background_rejects_on_unsupported_channel_before_any_work(
    cloud_env, monkeypatch
):
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()
    _patch_delivery(monkeypatch, False)

    out = cursor_agent_tool.delegate_cursor_agent(
        task="must not start",
        workdir=str(workdir.resolve()),
        session_id=session_id,
        tool_call_id=tool_call_id,
        background=True,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "NO WORK WAS STARTED" in parsed["error"]
    assert "background=false" in parsed["error"]
    assert cloud.create_calls == 0
    assert ad.active_count() == 0
    assert process_registry.completion_queue.empty()
    assert list(cursor_runs_dir().glob("*.receipt.json")) == []


def test_background_capacity_rejection_creates_no_cloud_run(cloud_env, monkeypatch):
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()
    monkeypatch.setattr("tools.delegate_tool._get_max_async_children", lambda: 0)

    out = cursor_agent_tool.delegate_cursor_agent(
        task="must not start",
        workdir=str(workdir.resolve()),
        session_id=session_id,
        tool_call_id=tool_call_id,
        background=True,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "NO WORK WAS STARTED" in parsed["error"]
    assert "capacity" in parsed["error"].lower()
    assert cloud.create_calls == 0
    assert ad.active_count() == 0
    assert process_registry.completion_queue.empty()


# ---------------------------------------------------------------------------
# Interrupt -> cloud cancel
# ---------------------------------------------------------------------------

def test_interrupt_all_cancels_the_cloud_run(cloud_env, monkeypatch):
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()

    cancelled: list[tuple[str, str]] = []
    release = threading.Event()

    real_cancel = cursor_agent_tool.cancel_cloud_run

    def _spy_cancel(agent_id, run_id, api_key):
        cancelled.append((agent_id, run_id))
        return real_cancel(agent_id, run_id, api_key)

    monkeypatch.setattr(cursor_agent_tool, "cancel_cloud_run", _spy_cancel)

    def _blocking_poll(**kwargs):
        release.wait(timeout=30)
        return {
            "id": kwargs["run_id"],
            "agentId": kwargs["agent_id"],
            "status": "CANCELLED",
            "result": "",
        }

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _blocking_poll)

    envelope = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="long cloud job",
            workdir=str(workdir.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
            background=True,
        )
    )
    assert envelope["status"] == "dispatched"
    delegation_id = envelope["delegation_id"]

    receipt_path = receipt_path_for_binding(session_id, tool_call_id)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        fresh = read_receipt(receipt_path) or {}
        if fresh.get("cloud_agent_id") and fresh.get("cloud_run_id"):
            break
        time.sleep(0.02)
    else:
        pytest.fail("the cloud run was never created before the interrupt")
    assert ad.active_count() == 1

    ad.interrupt_all(reason="stop requested")
    release.set()

    evt = _drain_one(timeout=15.0)
    assert evt is not None
    assert evt["delegation_id"] == delegation_id
    assert evt["status"] in ("interrupted", "cancelled")
    assert cancelled, "the cloud run was never cancelled"
    assert process_registry.completion_queue.empty()


# ---------------------------------------------------------------------------
# Receipt spine: delivery_mode + delegation_id stamped, restart re-arms
# ---------------------------------------------------------------------------

def test_receipt_is_stamped_with_delivery_mode_and_delegation_id(cloud_env):
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()

    envelope = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="spine check",
            workdir=str(workdir.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
            background=True,
        )
    )
    _drain_one()

    match = find_receipt_for_binding(session_id, tool_call_id)
    assert match is not None
    receipt_path, receipt = match
    assert receipt["delivery_mode"] == "background"
    assert receipt["delegation_id"] == envelope["delegation_id"]


def _envelope_history(tool_call_id: str, delegation_id: str, workdir: str) -> list:
    envelope = json.dumps(
        {
            "status": "dispatched",
            "mode": "background",
            "delegation_id": delegation_id,
            "tool": "delegate_cursor_agent",
            "result_kind": "cloud_agent",
            "count": 1,
            "goals": ["cloud task"],
        }
    )
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "delegate_cursor_agent",
                        "arguments": json.dumps(
                            {"task": "cloud task", "workdir": workdir}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": envelope,
        },
    ]


def test_envelope_tool_result_is_not_treated_as_terminal(cloud_env):
    """R7: an acceptance envelope answering the call must not suppress recovery."""
    from tools.cursor_agent_tool import _tool_result_already_present

    history = _envelope_history("call-1", "deleg_abc12345", "/tmp/repo")
    assert _tool_result_already_present(history, "call-1") is False

    terminal = [
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": json.dumps({"success": True, "final_report": "done"}),
        }
    ]
    assert _tool_result_already_present(terminal, "call-2") is True


def test_restart_rearm_uses_the_same_delegation_id(cloud_env, monkeypatch):
    """A restart re-arms the background poll under the ORIGINAL handle.

    The receipt is left non-terminal (the process died mid-poll) and the
    history carries only the envelope. Recovery must register a live
    delegation with the stamped id and must NOT append a tool result — the
    call is already answered by the envelope.
    """
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()

    # Keep the cloud run non-terminal so the re-armed poll has work to do.
    armed = threading.Event()

    def _slow_poll(**kwargs):
        armed.set()
        return {
            "id": kwargs["run_id"],
            "agentId": kwargs["agent_id"],
            "status": "FINISHED",
            "result": "cloud done after restart",
        }

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _slow_poll)

    # Seed the receipt exactly as a killed-mid-poll background run leaves it:
    # cloud ids persisted, state running, delivery_mode background.
    envelope_id = f"deleg_{uuid.uuid4().hex[:8]}"
    receipt_path, receipt = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir.resolve()),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "run.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    agent_id = cursor_agent_tool.deterministic_client_agent_id(
        session_id, tool_call_id
    )
    run_id = "run-restart-1"
    cloud.seed_running(agent_id=agent_id, run_id=run_id, status="RUNNING")
    from tools.cursor_run_receipts import persist_cloud_ids, update_receipt

    persist_cloud_ids(
        receipt_path, cloud_agent_id=agent_id, cloud_run_id=run_id
    )
    update_receipt(
        receipt_path, delivery_mode="background", delegation_id=envelope_id
    )

    history = _envelope_history(tool_call_id, envelope_id, str(workdir.resolve()))

    new_history, note = cursor_agent_tool.recover_delegate_cursor_agent_history(
        history, hermes_session_id=session_id
    )

    # No tool result was appended — the envelope already answered the call.
    assert new_history == history
    assert note is not None
    assert envelope_id in note

    # The SAME delegation_id is now live in the registry.
    assert armed.wait(timeout=10.0)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        records = {r["delegation_id"]: r for r in ad.list_async_delegations()}
        if envelope_id in records:
            break
        time.sleep(0.02)
    assert envelope_id in {r["delegation_id"] for r in ad.list_async_delegations()}

    evt = _drain_one(timeout=15.0)
    assert evt is not None
    assert evt["delegation_id"] == envelope_id
    assert evt["status"] == "completed"
    assert evt["summary"] == "cloud done after restart"

    # The receipt is terminal now, and a second recovery pass is a no-op.
    fresh = read_receipt(receipt_path)
    assert fresh["state"] == "terminal"
    again_history, again_note = cursor_agent_tool.recover_delegate_cursor_agent_history(
        history, hermes_session_id=session_id
    )
    assert again_history == history
    assert again_note is None or "Re-armed" not in again_note


def test_repeated_background_call_returns_envelope_not_terminal_inline(cloud_env):
    """Idempotent repeat of a background call: envelope inline, outcome on the rail."""
    cloud, workdir, _home = cloud_env
    session_id, tool_call_id = _session_ids()

    first = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="repeat me",
            workdir=str(workdir.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
            background=True,
        )
    )
    assert first["status"] == "dispatched"
    first_evt = _drain_one()
    assert first_evt is not None
    assert first_evt["delegation_id"] == first["delegation_id"]

    second = json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="repeat me",
            workdir=str(workdir.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
            background=True,
        )
    )
    # Still no terminal result inline — the cached outcome goes to the rail.
    assert second.get("status") == "dispatched"
    assert "final_report" not in second
    assert second["delegation_id"] == first["delegation_id"]

    evt = _drain_one(timeout=15.0)
    assert evt is not None
    assert evt["delegation_id"] == first["delegation_id"]
    assert evt["status"] == "completed"
    assert cloud.create_calls == 1, "a repeat must not create a second cloud run"
