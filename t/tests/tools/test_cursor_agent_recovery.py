"""Cloud-only restart recovery tests for delegate_cursor_agent."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.tools.fixtures.fake_cursor_cloud import FakeCursorCloud
from tests.tools.test_cursor_agent_tool import _WorkerFakePopen
from tools.cursor_run_receipts import (
    ReceiptValidationError,
    binding_hash,
    binding_run_lock,
    create_receipt,
    cursor_runs_dir,
    deterministic_client_agent_id,
    hash_prompt,
    read_receipt,
    receipt_path_for_binding,
    request_fingerprint,
)
from tools.cursor_agent_tool import (
    recover_delegate_cursor_agent_history,
)
from tools import cursor_agent_tool


@pytest.fixture
def cloud_env(monkeypatch, tmp_path):
    cloud = FakeCursorCloud()
    cloud.install(monkeypatch, cursor_agent_tool, tmp_path=tmp_path)
    monkeypatch.setattr(cursor_agent_tool.subprocess, "Popen", _WorkerFakePopen)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return cloud, workdir


def _dangling_history(tool_call_id: str, workdir: str, task: str = "do work") -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "delegate_cursor_agent",
                        "arguments": json.dumps({"task": task, "workdir": workdir}),
                    },
                }
            ],
        }
    ]


def _delegate(
    cloud: FakeCursorCloud,
    workdir: Path,
    *,
    session_id: str,
    tool_call_id: str,
    task: str = "cloud task",
) -> dict:
    return json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task=task,
            workdir=str(workdir.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    )


def test_deterministic_client_agent_id_is_valid_rfc_uuid():
    """Cursor Cloud rejects bcIds without standards version/variant bits."""
    client_id = deterministic_client_agent_id("sess-uuid", "call-uuid")
    assert client_id.startswith("bc-")
    assert len(client_id) == len("bc-") + 36
    parsed = uuid.UUID(client_id[len("bc-"):])
    assert parsed.version == 5
    assert parsed.variant == uuid.RFC_4122


def test_deterministic_client_agent_id_is_deterministic_for_binding():
    first = deterministic_client_agent_id("sess-det", "call-det")
    second = deterministic_client_agent_id("sess-det", "call-det")
    assert first == second
    assert first == f"bc-{uuid.UUID(first[len('bc-'):])}"


def test_deterministic_client_agent_id_distinct_for_distinct_bindings():
    ids = {
        deterministic_client_agent_id("sess-a", "call-1"),
        deterministic_client_agent_id("sess-a", "call-2"),
        deterministic_client_agent_id("sess-b", "call-1"),
        deterministic_client_agent_id("sess-b", "call-2"),
    }
    assert len(ids) == 4


def test_handler_uses_session_id_not_task_id(monkeypatch, tmp_path):
    from model_tools import handle_function_call

    captured: dict[str, str | None] = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True})

    monkeypatch.setattr("tools.cursor_agent_tool.delegate_cursor_agent", _fake_delegate)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    handle_function_call(
        "delegate_cursor_agent",
        {"task": "x", "workdir": str(workdir.resolve())},
        task_id="task-meta",
        session_id="hermes-sess-1",
        tool_call_id="call-xyz",
    )

    assert captured["session_id"] == "hermes-sess-1"
    assert captured["tool_call_id"] == "call-xyz"
    assert captured["task_id"] == "task-meta"


def test_same_task_two_sessions_do_not_collide(cloud_env):
    cloud, workdir = cloud_env
    _delegate(cloud, workdir, session_id="sess-a", tool_call_id="call-1")
    _delegate(cloud, workdir, session_id="sess-b", tool_call_id="call-1")
    assert cloud.create_calls == 2
    paths = list(cursor_runs_dir().glob("*.receipt.json"))
    assert len(paths) == 2


def test_two_calls_one_session_do_not_collide(cloud_env):
    cloud, workdir = cloud_env
    _delegate(cloud, workdir, session_id="sess-a", tool_call_id="call-1")
    _delegate(cloud, workdir, session_id="sess-a", tool_call_id="call-2")
    assert cloud.create_calls == 2


def test_receipt_write_failure_zero_create_calls(cloud_env, monkeypatch):
    cloud, workdir = cloud_env

    def _boom(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr("tools.cursor_run_receipts._atomic_write_json", _boom)
    result = _delegate(cloud, workdir, session_id="sess-fail", tool_call_id="call-fail")
    assert result["success"] is False
    assert "receipt" in result["error"].lower()
    assert cloud.create_calls == 0


def test_receipt_created_before_cloud_create(cloud_env):
    cloud, workdir = cloud_env
    seen: dict[str, bool] = {"receipt": False, "create": False}

    def _on_create(_payload):
        seen["create"] = True
        assert seen["receipt"] is True

    cloud.on_create = _on_create

    original_create = cursor_agent_tool.create_receipt

    def _wrapped_create(**kwargs):
        path, receipt = original_create(**kwargs)
        seen["receipt"] = True
        assert seen["create"] is False
        return path, receipt

    with patch.object(cursor_agent_tool, "create_receipt", side_effect=_wrapped_create):
        result = _delegate(cloud, workdir, session_id="sess-order", tool_call_id="call-order")
    assert result["success"] is True
    receipt = read_receipt(
        receipt_path_for_binding("sess-order", "call-order")
    )
    assert receipt["execution_mode"] == "cloud"
    assert receipt["client_agent_id"].startswith("bc-")


def test_terminal_while_gateway_down_recovers_without_create(cloud_env):
    cloud, workdir = cloud_env
    session_id = "sess-term"
    tool_call_id = "call-term"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-term", result_text="done from cloud")
    receipt_path, receipt = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import persist_cloud_ids, finalize_receipt

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-term")
    result_json = json.dumps({"success": True, "final_report": "done from cloud"})
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": result_json},
        cloud_agent_id=client_id,
        cloud_run_id="run-term",
    )
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 1
    assert cloud.poll_calls == 0
    assert cloud.list_calls == 0
    assert history[-1]["role"] == "tool"
    assert note and "terminal" in note.lower()


def test_running_recovery_resumes_poll_zero_create(cloud_env):
    cloud, workdir = cloud_env
    session_id = "sess-run"
    tool_call_id = "call-run"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_running(agent_id=client_id, run_id="run-live", status="CREATING")
    receipt_path, _receipt = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-live")
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert cloud.create_calls == 0
    assert cloud.poll_calls >= 1
    assert history[-1]["role"] == "tool"
    assert note and "cloud run" in note


def test_nonterminal_log_cannot_manufacture_success(cloud_env, tmp_path):
    cloud, workdir = cloud_env
    session_id = "sess-log"
    tool_call_id = "call-log"
    receipt_path, _receipt = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    log_path = workdir / "worker.log"
    log_path.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"looks done"}]}}\n')
    from tools.cursor_run_receipts import update_receipt

    update_receipt(
        receipt_path,
        state="terminal",
        outcome="success",
        terminal_result={"note": "missing result_json"},
        log_path=str(log_path),
    )
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0
    assert note and "could not be verified against Cursor Cloud" in note


def test_duplicate_recovery_appends_once(cloud_env):
    cloud, workdir = cloud_env
    session_id = "sess-dup"
    tool_call_id = "call-dup"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-dup")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import persist_cloud_ids, finalize_receipt

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-dup")
    result_json = json.dumps({"success": True, "final_report": "ok"})
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": result_json},
        cloud_agent_id=client_id,
        cloud_run_id="run-dup",
    )
    history = _dangling_history(tool_call_id, str(workdir), task="cloud task")
    first, _note1 = recover_delegate_cursor_agent_history(history, hermes_session_id=session_id)
    assert first[-1]["role"] == "tool"
    second, _note2 = recover_delegate_cursor_agent_history(first, hermes_session_id=session_id)
    assert len([m for m in second if m.get("role") == "tool"]) == 1
    assert second is first or second[-1]["content"] == first[-1]["content"]


def test_symlink_receipt_fails_closed(cloud_env, tmp_path):
    cloud, workdir = cloud_env
    session_id = "sess-symlink"
    tool_call_id = "call-symlink"
    target = tmp_path / "target.receipt.json"
    target.write_text("{}", encoding="utf-8")
    link = receipt_path_for_binding(session_id, tool_call_id)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        link.unlink()
    os.symlink(target, link)
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0
    assert note and "receipt lookup failed validation" in note


def test_cli_mode_receipt_rejected(cloud_env, tmp_path):
    cloud, workdir = cloud_env
    from tools.cursor_run_receipts import _atomic_write_json

    session_id = "sess-cli"
    tool_call_id = "call-cli"
    path = receipt_path_for_binding(session_id, tool_call_id)
    _atomic_write_json(
        path,
        {
            "schema_version": 2,
            "binding_hash": binding_hash(session_id, tool_call_id),
            "attempt_id": "a1",
            "hermes_session_id": session_id,
            "tool_call_id": tool_call_id,
            "request_fingerprint": request_fingerprint(
                task="cloud task",
                workdir=str(workdir),
                model=None,
                force=True,
                timeout_seconds=0,
                prompt_hash=hash_prompt("cloud task"),
            ),
            "workdir": str(workdir),
            "prompt_hash": hash_prompt("cloud task"),
            "state": "running",
            "outcome": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "log_path": str(workdir / "x.log"),
            "owner_pid": 1,
            "owner_boot_id": "test",
            "model": None,
            "force": True,
            "timeout_seconds": 0,
            "execution_mode": "cli",
            "client_agent_id": "bc-cli",
            "cloud_agent_id": "bc-cli",
            "cloud_run_id": "run-cli",
            "recovery_attempts": 0,
            "terminal_result": None,
        },
    )
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0


def test_fingerprint_mismatch_fails_closed(cloud_env, tmp_path):
    cloud, workdir = cloud_env
    session_id = "sess-fp"
    tool_call_id = "call-fp"
    path, receipt = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import update_receipt

    update_receipt(path, request_fingerprint="sha256:deadbeef")
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="different task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "binding mismatch" in note
    assert cloud.create_calls == 0


def test_concurrent_binding_lock_single_winner(cloud_env, tmp_path):
    cloud, workdir = cloud_env
    session_id = "sess-lock"
    tool_call_id = "call-lock"
    winner: dict = {}

    def _attempt(name: str) -> None:
        with binding_run_lock(session_id, tool_call_id) as acquired:
            if acquired:
                winner["name"] = name
                import time

                time.sleep(0.2)

    t1 = threading.Thread(target=_attempt, args=("a",))
    t2 = threading.Thread(target=_attempt, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert winner


def test_pending_create_without_ids_fail_closed_on_recovery(cloud_env):
    cloud, workdir = cloud_env
    session_id = "sess-pending"
    tool_call_id = "call-pending"
    create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    cloud.reset_counters()
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert cloud.create_calls == 0
    assert history[-1]["role"] == "tool"
    payload = json.loads(history[-1]["content"])
    assert payload["success"] is False
    assert "automatic recovery refused" in payload["error"]


def test_recovery_does_not_call_cli_helper(cloud_env):
    cloud, workdir = cloud_env
    assert not hasattr(cursor_agent_tool, "run_cursor_agent_cli_with_receipt")
    session_id = "sess-nocli"
    tool_call_id = "call-nocli"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_running(agent_id=client_id, run_id="run-nocli")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-nocli")
    recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir)),
        hermes_session_id=session_id,
    )


def test_create_receipt_requires_tool_call_id():
    with pytest.raises(ReceiptValidationError):
        create_receipt(
            hermes_session_id="sess",
            tool_call_id=None,
            workdir="/tmp/x",
            prompt_hash="sha256:x",
            log_path="/tmp/x.log",
            model=None,
            force=True,
            timeout_seconds=0,
            task="t",
        )


def test_b1a_forged_terminal_success_without_cloud_fails_closed(cloud_env):
    """B1a: forged local terminal success must not reach history without cloud read."""
    cloud, workdir = cloud_env
    session_id = "sess-forged"
    tool_call_id = "call-forged"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-missing")
    forged = json.dumps({"success": True, "final_report": "forged success"})
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": forged},
        cloud_agent_id=client_id,
        cloud_run_id="run-missing",
    )
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "could not be verified against Cursor Cloud" in note
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 0
    assert cloud.list_calls == 0


def test_b1b_terminal_receipt_uses_cloud_error_not_forged_success(cloud_env):
    """B1b: authoritative cloud ERROR overrides forged local success."""
    cloud, workdir = cloud_env
    session_id = "sess-cloud-err"
    tool_call_id = "call-cloud-err"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(
        agent_id=client_id,
        run_id="run-err",
        status="ERROR",
        result_text="cloud failed",
    )
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-err")
    forged = json.dumps({"success": True, "final_report": "forged success"})
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": forged},
        cloud_agent_id=client_id,
        cloud_run_id="run-err",
    )
    cloud.reset_counters()

    history, _note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    payload = json.loads(history[-1]["content"])
    assert payload["success"] is False
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 1
    assert cloud.list_calls == 0


def test_b1c_terminal_receipt_nonterminal_cloud_resumes_poll(cloud_env):
    """B1c: stale terminal receipt with RUNNING cloud resumes polling."""
    cloud, workdir = cloud_env
    session_id = "sess-stale-term"
    tool_call_id = "call-stale-term"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_running(agent_id=client_id, run_id="run-stale", status="RUNNING")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-stale")
    forged = json.dumps({"success": True, "final_report": "forged success"})
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": forged},
        cloud_agent_id=client_id,
        cloud_run_id="run-stale",
    )
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "tool"
    assert cloud.create_calls == 0
    assert cloud.poll_calls >= 1
    assert note and "cloud run" in note


def test_b1d_duplicate_tool_result_zero_cloud_calls(cloud_env):
    """B1d: existing tool result prevents duplicate append and cloud calls."""
    cloud, workdir = cloud_env
    session_id = "sess-dup-guard"
    tool_call_id = "call-dup-guard"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-dup-guard")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-dup-guard")
    result_json = json.dumps({"success": True, "final_report": "already present"})
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": result_json},
        cloud_agent_id=client_id,
        cloud_run_id="run-dup-guard",
    )
    history = _dangling_history(tool_call_id, str(workdir), task="cloud task")
    history.insert(
        0,
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result_json,
        },
    )
    cloud.reset_counters()

    _history, note = recover_delegate_cursor_agent_history(
        history,
        hermes_session_id=session_id,
    )
    assert note and "duplicate recovery append skipped" in note
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0
    assert cloud.get_run_calls == 0
    assert cloud.get_agent_calls == 0
    assert cloud.list_calls == 0


def test_b2_lock_symlink_raises_receipt_validation_error(cloud_env):
    """B2: LOCK_SYMLINK_ACQUIRED True must not happen — symlink lock fails closed."""
    cloud, workdir = cloud_env
    session_id = "sess-lock-symlink"
    tool_call_id = "call-lock-symlink"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import lock_path_for_binding, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-lock")
    lock_path = lock_path_for_binding(session_id, tool_call_id)
    target = workdir / "lock-target"
    target.write_text("x", encoding="utf-8")
    if lock_path.exists():
        lock_path.unlink()
    os.symlink(target, lock_path)

    with pytest.raises(ReceiptValidationError):
        with binding_run_lock(session_id, tool_call_id):
            pytest.fail("symlink lock path must not be acquired")

    cloud.reset_counters()
    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "receipt lookup failed validation" in note
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0


def test_b2_lock_directory_raises_receipt_validation_error(cloud_env):
    cloud, workdir = cloud_env
    session_id = "sess-lock-dir"
    tool_call_id = "call-lock-dir"
    from tools.cursor_run_receipts import lock_path_for_binding

    lock_path = lock_path_for_binding(session_id, tool_call_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        lock_path.unlink()
    lock_path.mkdir()

    with pytest.raises(ReceiptValidationError):
        with binding_run_lock(session_id, tool_call_id):
            pytest.fail("lock should not be acquired on directory path")


def test_b3_world_readable_receipt_fails_closed_zero_cloud(cloud_env):
    """B3: chmod 0644 receipt must fail closed with zero cloud calls."""
    cloud, workdir = cloud_env
    session_id = "sess-perms"
    tool_call_id = "call-perms"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-perms")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-perms")
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": json.dumps({"success": True})},
        cloud_agent_id=client_id,
        cloud_run_id="run-perms",
    )
    os.chmod(receipt_path, 0o644)
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "receipt lookup failed validation" in note
    assert cloud.create_calls == 0
    assert cloud.poll_calls == 0
    assert cloud.get_run_calls == 0
    assert cloud.get_agent_calls == 0
    assert cloud.list_calls == 0


def test_b3_foreign_owner_receipt_fails_closed(cloud_env, monkeypatch):
    cloud, workdir = cloud_env
    session_id = "sess-owner"
    tool_call_id = "call-owner"
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    import tools.cursor_run_receipts as receipts_mod

    real_getuid = os.getuid
    monkeypatch.setattr(receipts_mod.os, "getuid", lambda: real_getuid() + 99999)
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "receipt lookup failed validation" in note
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 0


def test_b4_symlink_extra_candidate_fails_closed(cloud_env, tmp_path):
    cloud, workdir = cloud_env
    session_id = "sess-b4-symlink"
    tool_call_id = "call-b4-symlink"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-b4")
    extra = cursor_runs_dir() / "extra.receipt.json"
    target = tmp_path / "extra-target.json"
    target.write_text("{}", encoding="utf-8")
    os.symlink(target, extra)
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "receipt lookup failed validation" in note
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 0


def test_b4_malformed_extra_candidate_fails_closed(cloud_env):
    cloud, workdir = cloud_env
    session_id = "sess-b4-bad"
    tool_call_id = "call-b4-bad"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-b4-bad")
    bad = cursor_runs_dir() / "bad-extra.receipt.json"
    bad.write_text("{not-json", encoding="utf-8")
    os.chmod(bad, 0o600)
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "receipt lookup failed validation" in note
    assert cloud.create_calls == 0


def test_b5a_in_lock_receipt_tamper_fails_closed(cloud_env, monkeypatch):
    cloud, workdir = cloud_env
    session_id = "sess-b5a"
    tool_call_id = "call-b5a"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-b5a")
    receipt_path, receipt = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-b5a")
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": json.dumps({"success": True})},
        cloud_agent_id=client_id,
        cloud_run_id="run-b5a",
    )
    real_read = read_receipt
    calls = {"n": 0}

    def _tampered_read(path):
        data = real_read(path)
        if data is None:
            return None
        calls["n"] += 1
        if calls["n"] >= 1:
            tampered = dict(data)
            tampered["request_fingerprint"] = "sha256:deadbeef"
            return tampered
        return data

    monkeypatch.setattr(cursor_agent_tool, "read_receipt", _tampered_read)
    cloud.reset_counters()

    history, note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "assistant"
    assert note and "binding mismatch" in note
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 0


def test_b5b_create_agent_id_mismatch_fails_closed(cloud_env):
    cloud, workdir = cloud_env
    cloud.mismatch_create_agent_id = True
    result = _delegate(
        cloud,
        workdir,
        session_id="sess-mismatch",
        tool_call_id="call-mismatch",
    )
    assert result["success"] is False
    assert cloud.poll_calls == 0
    receipt = read_receipt(receipt_path_for_binding("sess-mismatch", "call-mismatch"))
    assert receipt is not None
    assert receipt.get("cloud_agent_id") is None
    assert receipt.get("state") == "terminal"
    assert receipt.get("outcome") == "failed"


def test_authoritative_terminal_recovery_exact_cloud_call_counts(cloud_env):
    """Happy terminal recovery performs exactly one get_run and one get_agent."""
    cloud, workdir = cloud_env
    session_id = "sess-counts"
    tool_call_id = "call-counts"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-counts", result_text="counts ok")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-counts")
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": json.dumps({"success": True, "final_report": "counts ok"})},
        cloud_agent_id=client_id,
        cloud_run_id="run-counts",
    )
    cloud.reset_counters()

    history, _note = recover_delegate_cursor_agent_history(
        _dangling_history(tool_call_id, str(workdir), task="cloud task"),
        hermes_session_id=session_id,
    )
    assert history[-1]["role"] == "tool"
    assert cloud.create_calls == 0
    assert cloud.list_calls == 0
    assert cloud.poll_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 1


FORGED_MARKER = "FORGED_LOCAL_SUCCESS"


def _seed_forged_terminal_receipt(
    workdir: Path,
    session_id: str,
    tool_call_id: str,
    *,
    run_id: str,
) -> Path:
    """Persist a valid terminal receipt whose cached result_json is forged."""
    from tools.cursor_run_receipts import finalize_receipt, persist_cloud_ids

    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir.resolve()),
        prompt_hash=hash_prompt("cloud task"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="cloud task",
    )
    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id=run_id)
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={
            "result_json": json.dumps({"success": True, "final_report": FORGED_MARKER})
        },
        cloud_agent_id=client_id,
        cloud_run_id=run_id,
    )
    return receipt_path


def test_repeat_invoke_forged_cached_terminal_not_returned(cloud_env):
    """Verifier probe: cached terminal result_json is never authoritative."""
    cloud, workdir = cloud_env
    session_id = "sess-repeat-forged"
    tool_call_id = "call-repeat-forged"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-real", result_text="real cloud report")
    receipt_path = _seed_forged_terminal_receipt(
        workdir, session_id, tool_call_id, run_id="run-real"
    )
    cloud.fail_create = True
    cloud.reset_counters()

    result = _delegate(cloud, workdir, session_id=session_id, tool_call_id=tool_call_id)

    assert FORGED_MARKER not in json.dumps(result)
    assert result["success"] is True
    assert result["final_report"] == "real cloud report"
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 1
    assert cloud.poll_calls == 0
    assert cloud.list_calls == 0
    receipt = read_receipt(receipt_path)
    assert receipt is not None
    assert FORGED_MARKER not in json.dumps(receipt)


def test_repeat_invoke_terminal_receipt_cloud_running_resumes_poll(cloud_env):
    """Stale terminal receipt with RUNNING cloud resumes the same wait/poll."""
    cloud, workdir = cloud_env
    session_id = "sess-repeat-running"
    tool_call_id = "call-repeat-running"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_running(agent_id=client_id, run_id="run-live", status="RUNNING")
    _seed_forged_terminal_receipt(workdir, session_id, tool_call_id, run_id="run-live")
    cloud.fail_create = True
    cloud.reset_counters()

    result = _delegate(cloud, workdir, session_id=session_id, tool_call_id=tool_call_id)

    assert FORGED_MARKER not in json.dumps(result)
    assert result["success"] is True
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 2
    assert cloud.get_agent_calls == 1
    assert cloud.poll_calls == 1
    assert cloud.list_calls == 0


def test_repeat_invoke_terminal_receipt_cloud_missing_fails_closed(cloud_env):
    """Unverifiable terminal receipt fails closed without replacement work."""
    cloud, workdir = cloud_env
    session_id = "sess-repeat-missing"
    tool_call_id = "call-repeat-missing"
    _seed_forged_terminal_receipt(workdir, session_id, tool_call_id, run_id="run-missing")
    cloud.fail_create = True
    cloud.reset_counters()

    result = _delegate(cloud, workdir, session_id=session_id, tool_call_id=tool_call_id)

    assert result["success"] is False
    assert FORGED_MARKER not in json.dumps(result)
    assert "could not be verified against Cursor Cloud" in result["error"]
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 0
    assert cloud.poll_calls == 0
    assert cloud.list_calls == 0


def test_repeat_invoke_terminal_receipt_cloud_error_not_forged(cloud_env):
    """Authoritative cloud ERROR overrides the forged cached success."""
    cloud, workdir = cloud_env
    session_id = "sess-repeat-error"
    tool_call_id = "call-repeat-error"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(
        agent_id=client_id,
        run_id="run-err",
        status="ERROR",
        result_text="cloud failed",
    )
    _seed_forged_terminal_receipt(workdir, session_id, tool_call_id, run_id="run-err")
    cloud.fail_create = True
    cloud.reset_counters()

    result = _delegate(cloud, workdir, session_id=session_id, tool_call_id=tool_call_id)

    assert result["success"] is False
    assert FORGED_MARKER not in json.dumps(result)
    assert "cloud failed" in result["error"]
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 1
    assert cloud.poll_calls == 0
    assert cloud.list_calls == 0


def test_repeat_invoke_terminal_receipt_agent_lookup_missing_fails_closed(cloud_env):
    """Exact run terminal but exact agent lookup unavailable fails closed."""
    cloud, workdir = cloud_env
    session_id = "sess-repeat-noagent"
    tool_call_id = "call-repeat-noagent"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-noagent", result_text="REAL_CLOUD")
    del cloud.agents[client_id]
    _seed_forged_terminal_receipt(workdir, session_id, tool_call_id, run_id="run-noagent")
    cloud.fail_create = True
    cloud.reset_counters()

    result = _delegate(cloud, workdir, session_id=session_id, tool_call_id=tool_call_id)

    assert result["success"] is False
    assert FORGED_MARKER not in json.dumps(result)
    assert "REAL_CLOUD" not in json.dumps(result)
    assert "could not be verified against Cursor Cloud" in result["error"]
    assert cloud.create_calls == 0
    assert cloud.get_run_calls == 1
    assert cloud.get_agent_calls == 1
    assert cloud.poll_calls == 0
    assert cloud.list_calls == 0


def test_deterministic_client_agent_id_rfc4122_uuid_v5():
    import uuid

    session_id = "sess-rfc4122"
    tool_call_id = "call-rfc4122"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    assert client_id.startswith("bc-")
    parsed = uuid.UUID(client_id[3:])
    assert parsed.version == 5
    assert parsed.variant == uuid.RFC_4122
    assert deterministic_client_agent_id(session_id, tool_call_id) == client_id
    assert deterministic_client_agent_id("other-session", tool_call_id) != client_id
