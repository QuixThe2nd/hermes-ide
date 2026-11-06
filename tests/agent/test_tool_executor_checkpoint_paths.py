"""Behavioral coverage for checkpoint path resolution across terminal backends."""

import json
import os
from types import SimpleNamespace

import pytest

from agent.tool_executor import _ToolCallRef, _begin_tool_execution, _ensure_file_checkpoint
from agent.turn_explainers import TurnExplainersMixin
from tools.checkpoint_manager import CheckpointManager
from tools.terminal_tool import _active_environments, _env_lock


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", tmp_path / "checkpoints")
    return CheckpointManager(enabled=True)


@pytest.fixture
def container_task_id(monkeypatch):
    class FakeDockerEnvironment:  # class-name hint is how file_tools_paths classifies live envs
        pass

    task_id = "container-checkpoint-task"
    with _env_lock:
        monkeypatch.setitem(_active_environments, task_id, FakeDockerEnvironment())
    yield task_id
    with _env_lock:
        _active_environments.pop(task_id, None)


def test_relative_file_checkpoint_uses_task_workspace(tmp_path, monkeypatch):
    """Checkpoint lookup must use the same cwd as a relative file mutation."""
    process_cwd = tmp_path / "opt" / "hermes"
    workspace_cwd = tmp_path / "opt" / "data" / "workspace"
    process_cwd.mkdir(parents=True)
    workspace_cwd.mkdir(parents=True)

    # Both directories contain content so checkpointing the wrong one would
    # still succeed and remain observable as the regression did in Docker.
    (process_cwd / "pyproject.toml").write_text("[project]\nname = 'hermes'\n")
    (workspace_cwd / "pyproject.toml").write_text("[project]\nname = 'workspace'\n")
    (workspace_cwd / "existing.txt").write_text("before\n")

    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace_cwd))
    monkeypatch.setattr(
        "tools.checkpoint_manager.CHECKPOINT_BASE",
        tmp_path / "checkpoints",
    )

    manager = CheckpointManager(enabled=True)
    agent = SimpleNamespace(_checkpoint_mgr=manager)

    _ensure_file_checkpoint(
        agent,
        "write_file",
        {"path": "test_permissions2.txt"},
        "gateway-session",
    )

    assert manager.list_checkpoints(str(workspace_cwd))
    assert manager.list_checkpoints(str(process_cwd)) == []


def test_container_backend_task_leaves_host_store_untouched(tmp_path, manager, container_task_id):
    """A container-backed task's paths are container paths: no host snapshot from the file or
    destructive-terminal hooks and no host ledger entry, even when an unrelated host tree shares
    the spelling. A local task on the same manager still gets its checkpoint (control)."""
    host_file = tmp_path / "workspace" / "project" / "a.txt"
    host_file.parent.mkdir(parents=True)
    host_file.write_text("host content\n", encoding="utf-8")
    path = str(host_file)
    agent = SimpleNamespace(
        _checkpoint_mgr=manager, _turn_failed_file_mutations={}, _turn_file_mutation_paths=set(),
        quiet_mode=True, tool_progress_callback=None, tool_start_callback=None, _touch_activity=lambda *_: None,
    )

    _ensure_file_checkpoint(agent, "write_file", {"path": path}, container_task_id)
    ref = _ToolCallRef("terminal", {"command": f"rm -rf {host_file.parent}"}, container_task_id, "call-1", [])
    _begin_tool_execution(agent, ref, None)
    TurnExplainersMixin._record_file_mutation_result(
        agent, "write_file", {"path": path, "content": "x"},
        json.dumps({"bytes_written": 1, "resolved_path": path}), False, task_id=container_task_id,
    )
    assert manager.list_checkpoints(str(host_file.parent)) == []
    assert not (tmp_path / "checkpoints").exists()

    _ensure_file_checkpoint(agent, "write_file", {"path": path}, "local-task")
    assert len(manager.list_checkpoints(str(host_file.parent))) == 1
