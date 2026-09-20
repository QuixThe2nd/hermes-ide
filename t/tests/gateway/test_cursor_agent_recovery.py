"""Gateway integration for delegate_cursor_agent cloud restart recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.run import _build_gateway_agent_history
from tests.tools.fixtures.fake_cursor_cloud import FakeCursorCloud
from tests.tools.test_cursor_agent_tool import _WorkerFakePopen
from tools.cursor_run_receipts import (
    create_receipt,
    deterministic_client_agent_id,
    finalize_receipt,
    hash_prompt,
    persist_cloud_ids,
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


def _history(tool_call_id: str, workdir: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "delegate_cursor_agent",
                        "arguments": json.dumps({"task": "gw", "workdir": workdir}),
                    },
                }
            ],
        }
    ]


def test_build_gateway_history_injects_terminal_cloud_result(cloud_env):
    cloud, workdir = cloud_env
    session_id = "gw-session"
    tool_call_id = "gw-call"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_terminal(agent_id=client_id, run_id="run-gw", result_text="gw done")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("gw"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="gw",
    )
    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-gw")
    result_json = json.dumps({"success": True, "final_report": "gw done"})
    finalize_receipt(
        receipt_path,
        outcome="success",
        terminal_result={"result_json": result_json},
        cloud_agent_id=client_id,
        cloud_run_id="run-gw",
    )
    cloud.reset_counters()

    agent_history, _observed, note = _build_gateway_agent_history(
        _history(tool_call_id, str(workdir)),
        hermes_session_id=session_id,
    )
    assert cloud.create_calls == 0
    assert note and "terminal" in note.lower()
    assert agent_history[-1]["role"] == "tool"
    assert agent_history[-2]["role"] == "assistant"


def test_build_gateway_history_resumes_running_cloud_run(cloud_env):
    cloud, workdir = cloud_env
    session_id = "gw-resume-session"
    tool_call_id = "gw-resume-call"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    cloud.seed_running(agent_id=client_id, run_id="run-resume")
    receipt_path, _ = create_receipt(
        hermes_session_id=session_id,
        tool_call_id=tool_call_id,
        workdir=str(workdir),
        prompt_hash=hash_prompt("gw"),
        log_path=str(workdir / "worker.log"),
        model=None,
        force=True,
        timeout_seconds=0,
        task="gw",
    )
    persist_cloud_ids(receipt_path, cloud_agent_id=client_id, cloud_run_id="run-resume")
    cloud.reset_counters()

    agent_history, _observed, note = _build_gateway_agent_history(
        _history(tool_call_id, str(workdir)),
        hermes_session_id=session_id,
    )
    assert cloud.create_calls == 0
    assert cloud.poll_calls >= 1
    assert agent_history[-1]["role"] == "tool"
    assert note and "cloud run" in note


def test_build_gateway_history_skips_unrelated_tool_call(cloud_env):
    cloud, workdir = cloud_env
    json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task="gw",
            workdir=str(workdir.resolve()),
            session_id="gw-session",
            tool_call_id="real-call",
        )
    )
    agent_history, _observed, note = _build_gateway_agent_history(
        _history("other-call", str(workdir)),
        hermes_session_id="gw-session",
    )
    assert agent_history[-1]["role"] == "assistant"
    assert note is None or "no matching" in note
