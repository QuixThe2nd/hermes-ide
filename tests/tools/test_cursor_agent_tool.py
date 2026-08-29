"""Behavior-contract tests for delegate_cursor_agent."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

_DEFAULT_DELEGATE_SESSION = "test-session"
_DEFAULT_DELEGATE_TOOL_CALL = "test-tool-call"


def _default_client_agent_id() -> str:
    from tools.cursor_run_receipts import deterministic_client_agent_id

    return deterministic_client_agent_id(_DEFAULT_DELEGATE_SESSION, _DEFAULT_DELEGATE_TOOL_CALL)


def _delegate(task, workdir, **kwargs):
    kwargs.setdefault("session_id", _DEFAULT_DELEGATE_SESSION)
    kwargs.setdefault("tool_call_id", _DEFAULT_DELEGATE_TOOL_CALL)
    from tools import cursor_agent_tool
    return cursor_agent_tool.delegate_cursor_agent(task=task, workdir=workdir, **kwargs)


SAMPLE_STREAM_JSON = "\n".join(
    [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "sess-abc-123",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "Review auth module",
                            "subagentType": "explore",
                            "model": "kimi-k3-high",
                        }
                    }
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Partial progress update."}]
                },
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "Implement fix",
                            "subagent_type": "implementer",
                            "model": "composer-2.5-fast",
                        }
                    }
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Final implementation report."}]
                },
            }
        ),
    ]
)


class _FakeStdoutWithEof:
    def __init__(self, data: bytes = b""):
        self._stream = io.BytesIO(data)
        self.eof_reached = threading.Event()

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(4096 if size < 0 else size)
        if not chunk:
            self.eof_reached.set()
        return chunk

    def read1(self, size: int = -1) -> bytes:
        chunk = self._stream.read1(4096 if size < 0 else size)
        if not chunk:
            self.eof_reached.set()
        return chunk

    def close(self):
        self._stream.close()


class _FakePopen:
    instances: list["_FakePopen"] = []

    def __init__(
        self,
        cmd,
        *,
        cwd=None,
        stdout=None,
        stderr=None,
        stdin=None,
        start_new_session=False,
        **kwargs,
    ):
        del stderr, stdin, start_new_session, kwargs
        self.cmd = cmd
        self.cwd = cwd
        self.stdout = stdout
        self._returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.pid = 4242 + len(_FakePopen.instances)
        _FakePopen.instances.append(self)

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        del timeout
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def set_exit(self, code: int) -> None:
        self._returncode = code


class _StreamingFakePopen(_FakePopen):
    """Popen that exposes canned stream-json on stdout and exits after EOF."""

    def __init__(self, cmd, *, cwd=None, stdout=None, stderr=None, **kwargs):
        super().__init__(cmd, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)
        self.stdout = _FakeStdoutWithEof(SAMPLE_STREAM_JSON.encode("utf-8"))
        self._returncode = None

    def poll(self):
        if self._returncode is not None:
            return self._returncode
        if isinstance(self.stdout, _FakeStdoutWithEof) and self.stdout.eof_reached.is_set():
            self._returncode = 0
        return self._returncode


class _StalledFakePopen(_FakePopen):
    """Never emits stdout bytes and never exits until terminated."""

    def __init__(self, cmd, *, cwd=None, stdout=None, stderr=None, **kwargs):
        super().__init__(cmd, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)
        self.stdout = _FakeStdoutWithEof(b"")
        self._returncode = None

    def poll(self):
        return None


class _TimeoutFakePopen(_StalledFakePopen):
    pass


class _WorkerFakePopen(_StalledFakePopen):
    """Worker process stays up until terminate/kill (My Machines worker)."""

    pass


class _NonZeroExitPopen(_StreamingFakePopen):
    def poll(self):
        if self._returncode is not None:
            return self._returncode
        if isinstance(self.stdout, _FakeStdoutWithEof) and self.stdout.eof_reached.is_set():
            self._returncode = 1
        return self._returncode


@pytest.fixture(autouse=True)
def _reset_fake_popen():
    _FakePopen.instances.clear()
    yield
    _FakePopen.instances.clear()


def test_schema_registration():
    import tools.cursor_agent_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("delegate_cursor_agent")
    assert entry is not None
    assert entry.toolset == "delegation"
    assert entry.max_result_size_chars == 100_000

    schema = entry.schema
    required = set(schema["parameters"]["required"])
    assert required == {"task", "workdir"}

    props = schema["parameters"]["properties"]
    assert "default" not in props["model"]
    assert props["timeout_seconds"]["default"] == 0
    assert props["force"]["default"] is True


def test_check_fn_binary_found(monkeypatch):
    from tools.cursor_agent_tool import check_cursor_agent_requirements

    monkeypatch.setattr("tools.cursor_agent_tool.shutil.which", lambda name: "/usr/bin/agent")
    assert check_cursor_agent_requirements() is True


def test_check_fn_binary_missing(monkeypatch):
    from tools.cursor_agent_tool import check_cursor_agent_requirements

    monkeypatch.setattr("tools.cursor_agent_tool.shutil.which", lambda name: None)

    class _MissingPath(Path):
        def is_file(self):
            return False

    monkeypatch.setattr("tools.cursor_agent_tool._local_bin_agent_path", lambda: _MissingPath("/nope/agent"))
    assert check_cursor_agent_requirements() is False


def test_clamp_timeout_seconds():
    from tools.cursor_agent_tool import (
        DEFAULT_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
        MIN_TIMEOUT_SECONDS,
        _clamp_timeout_seconds,
    )

    assert _clamp_timeout_seconds(59) == MIN_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(1801) == MAX_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds("garbage") == DEFAULT_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(None) == DEFAULT_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(0) == 0
    assert _clamp_timeout_seconds(-5) == 0


def test_parse_stream_json_log():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    parsed = parse_cursor_agent_log(SAMPLE_STREAM_JSON)

    assert parsed["session_id"] == "sess-abc-123"
    assert parsed["final_report"] == "Final implementation report."
    assert len(parsed["delegations"]) == 2
    assert parsed["delegations"][0] == {
        "description": "Review auth module",
        "subagent_type": "explore",
        "model": "kimi-k3-high",
    }
    assert parsed["delegations"][1] == {
        "description": "Implement fix",
        "subagent_type": "implementer",
        "model": "composer-2.5-fast",
    }


def test_parse_dedupes_duplicate_delegations():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    duplicate = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-same-1",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Same task",
                        "subagentType": "explore",
                        "model": "kimi-k3-high",
                    }
                }
            },
        }
    )
    log = "\n".join([duplicate, duplicate])
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 1


def test_parse_dedupes_duplicate_delegations_without_identity_keys():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    duplicate = json.dumps(
        {
            "type": "tool_call",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Same task",
                        "subagentType": "explore",
                        "model": "kimi-k3-high",
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([duplicate, duplicate]))
    assert len(parsed["delegations"]) == 1


def test_action_required_structured_event_only():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    log_line = json.dumps(
        {
            "type": "error",
            "error_type": "ActionRequiredError",
            "message": "Approve file write",
        }
    )
    parsed = parse_cursor_agent_log(log_line + "\n")
    assert parsed["action_required"] is not None
    assert "Approve file write" in parsed["action_required"]["detail"]


def test_action_required_not_triggered_by_assistant_mention():
    from tools import cursor_agent_tool

    log_text = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "sess-1",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "We saw an ActionRequiredError in docs but recovered.",
                            }
                        ]
                    },
                }
            ),
        ]
    )

    parsed = cursor_agent_tool.parse_cursor_agent_log(log_text)
    assert parsed["action_required"] is None


def test_action_required_error_handler_parse_only():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    parsed = parse_cursor_agent_log(
        json.dumps(
            {
                "type": "error",
                "error_type": "ActionRequiredError",
                "message": "Needs approval",
            }
        )
    )
    assert parsed["action_required"] is not None
    assert "Needs approval" in parsed["action_required"]["detail"]


def test_validation_errors_use_full_result_shape(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr("tools.cursor_agent_tool.resolve_cursor_agent_binary", lambda: "/usr/bin/agent")

    empty = json.loads(cursor_agent_tool.delegate_cursor_agent(task="", workdir=str(tmp_path)))
    assert empty["success"] is False
    assert empty["error"]
    assert empty["log_path"] is None
    assert "final_report" in empty
    assert "delegations" in empty

    relative = json.loads(
        cursor_agent_tool.delegate_cursor_agent(task="x", workdir="relative/path")
    )
    assert relative["success"] is False
    assert "absolute path" in relative["error"]

    missing = json.loads(
        _delegate(
            task="x",
            workdir=str((tmp_path / "missing").resolve()),
        )
    )
    assert missing["success"] is False
    assert "does not exist" in missing["error"]


def _install_cloud_happy_path(
    monkeypatch,
    tmp_path,
    *,
    poll_status="FINISHED",
    result_text="Final implementation report.",
    stub_progress_notice=True,
    stub_starting_ref=True,
):
    from tools import cursor_agent_tool

    secret = tmp_path / "cursor-cloud.env"
    secret.write_text("CURSOR_API_KEY=test-secret-key\n", encoding="utf-8")
    monkeypatch.setattr(cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", secret)
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr(
        cursor_agent_tool,
        "resolve_workdir_origin",
        lambda workdir: "https://github.com/acme/demo",
    )
    if stub_starting_ref:
        monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_starting_ref", lambda workdir: "main")
    monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_ATTEMPTS", 1)
    monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_DELAY_SECONDS", 0)
    monkeypatch.setattr(cursor_agent_tool, "POLL_INTERVAL_SECONDS", 0)
    notices: list[str] = []
    if stub_progress_notice:
        monkeypatch.setattr(cursor_agent_tool, "_emit_progress_notice", lambda message: notices.append(message))
    client_id = _default_client_agent_id()
    created = {
        "agent": {
            "id": client_id,
            "name": "hermes-test",
            "url": f"https://cursor.com/agents/{client_id}",
            "latestRunId": "run-aaaa",
        },
        "run": {
            "id": "run-aaaa",
            "agentId": client_id,
            "status": "CREATING",
        },
    }
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: (created["agent"], dict(created["run"])),
    )
    monkeypatch.setattr(
        cursor_agent_tool,
        "poll_cloud_run",
        lambda **kwargs: {
            "id": "run-aaaa",
            "agentId": client_id,
            "status": poll_status,
            "result": result_text,
            "durationMs": 1234,
        },
    )
    return notices


def test_happy_path_e2e(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    notices = _install_cloud_happy_path(monkeypatch, tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    result = json.loads(
        _delegate(
            task="implement feature",
            workdir=str(workdir.resolve()),
        )
    )

    assert result["success"] is True
    assert result["final_report"] == "Final implementation report."
    assert result["delegations"] == []
    client_id = _default_client_agent_id()
    assert result["session_id"] == client_id
    assert result["error"] is None
    assert result["agent_id"] == client_id
    assert result["run_id"] == "run-aaaa"
    assert result["cloud_status"] == "FINISHED"
    assert result["progress_url"] == f"https://cursor.com/agents/{client_id}"
    assert "cursor-runs" in result["log_path"]
    assert notices == [f"Cursor Cloud Agent: https://cursor.com/agents/{client_id}"]
    assert _FakePopen.instances == []  # Cursor-hosted runs spawn no local worker
    assert "test-secret-key" not in json.dumps(result)


def test_explicit_model_adds_payload_id(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    captured = {}

    def _create(payload, api_key):
        captured["payload"] = payload
        captured["api_key"] = api_key
        return (
            {
                "id": "bc-model",
                "url": "https://cursor.com/agents/bc-model",
                "latestRunId": "run-model",
            },
            {"id": "run-model", "agentId": "bc-model", "status": "CREATING"},
        )

    _install_cloud_happy_path(monkeypatch, tmp_path)
    monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    _delegate(
        task="implement feature",
        workdir=str(workdir.resolve()),
        model="composer-2.5",
    )

    assert captured["payload"]["model"] == {"id": "composer-2.5"}
    assert captured["api_key"] == "test-secret-key"
    assert "env" not in captured["payload"]  # Cursor-hosted, no machine routing


@pytest.mark.parametrize("force_value", [False, "false", "0", True])
def test_force_does_not_enable_pushes(monkeypatch, tmp_path, force_value):
    from tools import cursor_agent_tool

    captured = {}

    def _create(payload, api_key):
        captured["payload"] = payload
        return (
            {
                "id": "bc-force",
                "url": "https://cursor.com/agents/bc-force",
                "latestRunId": "run-force",
            },
            {"id": "run-force", "agentId": "bc-force", "status": "CREATING"},
        )

    _install_cloud_happy_path(monkeypatch, tmp_path)
    monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    _delegate(
        task="no pushes",
        workdir=str(workdir.resolve()),
        force=force_value,
    )

    payload = captured["payload"]
    assert payload["prompt"]["text"].startswith(cursor_agent_tool.NO_PUSH_PROMPT_PREFIX)
    after_no_push = payload["prompt"]["text"][len(cursor_agent_tool.NO_PUSH_PROMPT_PREFIX):]
    assert after_no_push.startswith(cursor_agent_tool.DEFAULT_ORCHESTRATION_PROMPT)
    assert payload["prompt"]["text"].endswith("no pushes")
    assert payload["autoCreatePR"] is False
    assert payload["skipReviewerRequest"] is True
    assert payload["workOnCurrentBranch"] is False
    assert "env" not in captured["payload"]


def test_handler_force_string_false(monkeypatch, tmp_path):
    from tools.cursor_agent_tool import _handle_delegate_cursor_agent

    captured = {}

    def _create(payload, api_key):
        captured["payload"] = payload
        return (
            {
                "id": "bc-handler",
                "url": "https://cursor.com/agents/bc-handler",
                "latestRunId": "run-handler",
            },
            {"id": "run-handler", "agentId": "bc-handler", "status": "CREATING"},
        )

    _install_cloud_happy_path(monkeypatch, tmp_path)
    from tools import cursor_agent_tool

    monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    _handle_delegate_cursor_agent(
        {
            "task": "handler force",
            "workdir": str(workdir.resolve()),
            "force": "false",
        },
        session_id="handler-session",
        tool_call_id="handler-call",
    )

    assert captured["payload"]["autoCreatePR"] is False
    assert "env" not in captured["payload"]


def test_parse_distinct_call_ids_same_args_produce_two_records():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    def _tool_call(call_id: str) -> str:
        return json.dumps(
            {
                "type": "tool_call",
                "call_id": call_id,
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "Same task",
                            "subagentType": "explore",
                            "model": "kimi-k3-high",
                        }
                    }
                },
            }
        )

    log = "\n".join([_tool_call("call-a"), _tool_call("call-b")])
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 2
    assert parsed["delegations"][0] == parsed["delegations"][1]


def test_parse_started_and_completed_same_call_id_one_record():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    started = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-xyz",
            "subtype": "started",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Review module",
                        "subagentType": "explore",
                        "model": "kimi-k3-high",
                    }
                }
            },
        }
    )
    completed = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-xyz",
            "subtype": "completed",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Review module",
                        "subagentType": "explore",
                        "model": "kimi-k3-high",
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([started, completed]))
    assert len(parsed["delegations"]) == 1


def test_parse_camel_and_snake_subagent_type_keys():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    camel = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-camel",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Camel case",
                        "subagentType": "explore",
                        "model": "m1",
                    }
                }
            },
        }
    )
    snake = json.dumps(
        {
            "type": "tool_call",
            "call_id": "call-snake",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Snake case",
                        "subagent_type": "implementer",
                        "model": "m2",
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([camel, snake]))
    assert len(parsed["delegations"]) == 2
    assert parsed["delegations"][0]["subagent_type"] == "explore"
    assert parsed["delegations"][1]["subagent_type"] == "implementer"


def test_action_required_plain_text_line():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    line = (
        "ActionRequiredError: Named models unavailable Free plans can only use Auto. "
        "Switch to Auto or upgrade plans to continue."
    )
    parsed = parse_cursor_agent_log(line)
    assert parsed["action_required"] is not None
    assert "Named models unavailable" in parsed["action_required"]["detail"]


def test_action_required_json_string_line():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    line = (
        "ActionRequiredError: Named models unavailable Free plans can only use Auto. "
        "Switch to Auto or upgrade plans to continue."
    )
    parsed = parse_cursor_agent_log(json.dumps(line))
    assert parsed["action_required"] is not None
    assert "Named models unavailable" in parsed["action_required"]["detail"]


def test_action_required_json_string_prose_mention_not_triggered():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    line = json.dumps("We saw an ActionRequiredError in docs but recovered.")
    parsed = parse_cursor_agent_log(line)
    assert parsed["action_required"] is None


def test_action_required_malformed_lines_do_not_crash():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    log = "\n".join(['{"broken json', "random garbage", "Error: something else"])
    parsed = parse_cursor_agent_log(log)
    assert parsed["action_required"] is None


def _task_call_event(**extra_fields) -> str:
    base = {
        "type": "tool_call",
        "tool_call": {
            "taskToolCall": {
                "args": {
                    "description": "Same task",
                    "subagentType": "explore",
                    "model": "kimi-k3-high",
                }
            }
        },
    }
    base.update(extra_fields)
    return json.dumps(base)


def test_parse_dedupes_dict_valued_call_id():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    event = _task_call_event(call_id={"a": 1, "b": 2})
    parsed = parse_cursor_agent_log("\n".join([event, event]))
    assert len(parsed["delegations"]) == 1


def test_parse_dedupes_list_valued_call_id():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    event = _task_call_event(call_id=["x", 1])
    parsed = parse_cursor_agent_log("\n".join([event, event]))
    assert len(parsed["delegations"]) == 1


def test_parse_dedupes_dict_valued_call_id_key_order_stable():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    first = _task_call_event(call_id={"b": 2, "a": 1})
    second = _task_call_event(call_id={"a": 1, "b": 2})
    parsed = parse_cursor_agent_log("\n".join([first, second]))
    assert len(parsed["delegations"]) == 1


def test_parse_dedupes_nested_dict_list_identity_keys():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    tool_call_id = json.dumps(
        {
            "type": "tool_call",
            "toolCallId": {"id": "nested", "seq": [1, 2]},
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Nested id",
                        "subagentType": "explore",
                        "model": "m1",
                    }
                }
            },
        }
    )
    agent_id = json.dumps(
        {
            "type": "tool_call",
            "agentId": ["agent", 42],
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "Agent id list",
                        "subagentType": "explore",
                        "model": "m2",
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([tool_call_id, tool_call_id, agent_id, agent_id]))
    assert len(parsed["delegations"]) == 2


def test_parse_dedupes_dict_list_content_fallback():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    duplicate = json.dumps(
        {
            "type": "tool_call",
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": {"goal": "review", "scope": ["auth"]},
                        "subagentType": "explore",
                        "model": ["kimi-k3-high"],
                    }
                }
            },
        }
    )
    parsed = parse_cursor_agent_log("\n".join([duplicate, duplicate]))
    assert len(parsed["delegations"]) == 1


def test_parse_non_hashable_values_do_not_crash():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    samples = [
        {
            "type": "tool_call",
            "call_id": {"b": 2, "a": 1},
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": "x",
                        "model": "m",
                    }
                }
            },
        },
        {
            "type": "tool_call",
            "call_id": ["x", 1],
            "tool_call": {
                "taskToolCall": {
                    "args": {
                        "description": {"x": 1},
                        "model": ["m"],
                    }
                }
            },
        },
    ]
    for event in samples:
        parsed = parse_cursor_agent_log(json.dumps(event))
        assert len(parsed["delegations"]) == 1


def test_parse_distinct_scalar_call_ids():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    def _event(call_id):
        return json.dumps(
            {
                "type": "tool_call",
                "call_id": call_id,
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "scalar id",
                            "subagentType": "explore",
                            "model": "m",
                        }
                    }
                },
            }
        )

    log = "\n".join([_event(True), _event(1), _event(False), _event(0), _event(1.0)])
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 5


def test_parse_distinct_nested_scalar_call_ids():
    from tools.cursor_agent_tool import parse_cursor_agent_log

    def _event(call_id):
        return json.dumps(
            {
                "type": "tool_call",
                "call_id": call_id,
                "tool_call": {
                    "taskToolCall": {
                        "args": {
                            "description": "nested scalar id",
                            "subagentType": "explore",
                            "model": "m",
                        }
                    }
                },
            }
        )

    log = "\n".join(
        [
            _event({"flag": True}),
            _event({"flag": 1}),
            _event({"flag": False}),
            _event({"flag": 0}),
            _event({"value": 1}),
            _event({"value": 1.0}),
        ]
    )
    parsed = parse_cursor_agent_log(log)
    assert len(parsed["delegations"]) == 6


def _kill_process_group_or_pid(pgid: int | None, pid: int | None) -> None:
    import os
    import signal

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX live cleanup helper
            return
        except (OSError, ProcessLookupError):
            pass
    if pid is not None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)  # windows-footgun: ok — POSIX live cleanup helper
            return
        except (OSError, ProcessLookupError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)  # windows-footgun: ok — POSIX live cleanup helper
        except (OSError, ProcessLookupError):
            pass


@pytest.mark.live_system_guard_bypass
def test_incremental_stdout_updates_log_before_child_exit(monkeypatch, tmp_path):
    import os
    import subprocess
    import sys

    from tools import cursor_agent_tool

    monkeypatch.setattr(cursor_agent_tool, "STALL_WATCHDOG_SECONDS", 5)
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.05)

    spawn_info: dict[str, int | None] = {"pid": None, "pgid": None}
    real_popen = subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawn_info["pid"] = proc.pid
        try:
            spawn_info["pgid"] = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError):
            spawn_info["pgid"] = None
        return proc

    monkeypatch.setattr(cursor_agent_tool.subprocess, "Popen", _capturing_popen)

    child_script = (
        "import sys, time\n"
        "sys.stdout.write('chunk1\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(1.25)\n"
        "sys.stdout.write('chunk2\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.1)\n"
    )
    cmd = [sys.executable, "-c", child_script]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    result_holder: dict = {}

    from tools.agent_cli_runner import run_agent_cli

    def run() -> None:
        result_holder["result"] = run_agent_cli(
            cmd,
            workdir=str(tmp_path),
            timeout_seconds=60,
            log_dir=log_dir,
            run_timestamp="test",
        )

    thread = threading.Thread(target=run)
    thread.start()

    child_pid: int | None = None
    pgid: int | None = None
    try:
        found_chunk = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            logs = list(log_dir.glob("*.jsonl"))
            if logs:
                name_parts = logs[0].stem.rsplit("-", 1)
                if child_pid is None and len(name_parts) == 2 and name_parts[1].isdigit():
                    child_pid = int(name_parts[1])
                    try:
                        pgid = os.getpgid(child_pid)
                    except (OSError, ProcessLookupError):
                        pgid = None
                if "chunk1" in logs[0].read_text(encoding="utf-8", errors="replace"):
                    found_chunk = True
                    assert thread.is_alive(), "run finished before chunk1 reached the log"
                    assert "result" not in result_holder, "run finished before chunk1 reached the log"
                    if child_pid is not None:
                        os.kill(child_pid, 0)  # windows-footgun: ok — POSIX live liveness probe
                    break
            time.sleep(0.05)

        thread.join(timeout=10)
        assert "result" in result_holder
        error_code, _log_path, log_text, _duration, returncode = result_holder["result"]

        assert found_chunk
        if child_pid is not None:
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)  # windows-footgun: ok — POSIX live liveness probe
        assert error_code != "stalled"
        assert "chunk1" in log_text
        assert "chunk2" in log_text
        assert returncode == 0
    finally:
        cleanup_pgid = pgid if pgid is not None else spawn_info.get("pgid")
        cleanup_pid = child_pid if child_pid is not None else spawn_info.get("pid")
        _kill_process_group_or_pid(
            cleanup_pgid if isinstance(cleanup_pgid, int) else None,
            cleanup_pid if isinstance(cleanup_pid, int) else None,
        )
        thread.join(timeout=10)


@pytest.mark.live_system_guard_bypass
def test_terminate_process_kills_sigterm_resistant_descendant(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    import sys

    from tools.agent_cli_runner import _terminate_process

    monkeypatch.setattr("tools.agent_cli_runner._TERMINATE_GRACE_SECONDS", 0.5)

    desc_pid_file = tmp_path / "desc.pid"
    leader_script = f"""
import signal
import subprocess
import sys
import time
from pathlib import Path

desc_script = '''
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(30)
'''

desc = subprocess.Popen([sys.executable, "-c", desc_script])
Path({str(desc_pid_file)!r}).write_text(str(desc.pid))
time.sleep(30)
"""

    proc = subprocess.Popen(
        [sys.executable, "-c", leader_script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = None

    desc_pid: int | None = None
    try:
        for _ in range(100):
            if desc_pid_file.is_file():
                desc_pid = int(desc_pid_file.read_text(encoding="utf-8").strip())
                break
            time.sleep(0.05)
        assert desc_pid is not None

        _terminate_process(proc, pgid)

        assert proc.poll() is not None
        assert proc.returncode == -signal.SIGTERM

        with pytest.raises(ProcessLookupError):
            os.kill(desc_pid, 0)  # windows-footgun: ok — POSIX live liveness probe
    finally:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX live cleanup
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # windows-footgun: ok — POSIX live cleanup
            except (OSError, ProcessLookupError):
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except (OSError, ProcessLookupError):
                        pass
        if desc_pid is not None:
            try:
                os.kill(desc_pid, signal.SIGKILL)  # windows-footgun: ok — POSIX live cleanup
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def test_child_env_guarantees_home_and_local_bin(monkeypatch, tmp_path):
    """With no local worker, the delegate must still keep secrets out of the payload."""
    from tools import cursor_agent_tool

    captured = {}

    def _create(payload, api_key):
        captured["payload"] = payload
        client_id = _default_client_agent_id()
        return (
            {
                "id": client_id,
                "url": f"https://cursor.com/agents/{client_id}",
                "latestRunId": "run-env",
            },
            {"id": "run-env", "agentId": client_id, "status": "CREATING"},
        )

    _install_cloud_happy_path(monkeypatch, tmp_path)
    monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        _delegate(
            task="x",
            workdir=str(workdir.resolve()),
        )
    )

    assert result["success"] is True, result.get("error")
    dumped = json.dumps(captured.get("payload", {}))
    assert "test-secret-key" not in dumped
    assert "CURSOR_API_KEY" not in dumped
    assert "test-secret-key" not in json.dumps(result)
    assert "test-secret-key" not in result.get("log_path", "")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://github.com/acme/demo.git", "https://github.com/acme/demo"),
        ("https://github.com/acme/demo/", "https://github.com/acme/demo"),
        ("https://www.github.com/acme/demo.git", "https://github.com/acme/demo"),
        ("http://github.com/acme/demo.git", "https://github.com/acme/demo"),
        ("git@github.com:acme/demo.git", "https://github.com/acme/demo"),
        ("ssh://git@github.com/acme/demo.git", "https://github.com/acme/demo"),
        ("https://user:token@github.com/acme/demo.git", "https://github.com/acme/demo"),
        ("git://github.com/acme/demo.git", "https://github.com/acme/demo"),
    ],
)
def test_normalize_git_origin_supported(raw, expected):
    from tools.cursor_agent_tool import normalize_git_origin

    assert normalize_git_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/home/user/repo",
        "./repo",
        "../repo",
        ".",
        "file:///home/user/repo",
        "file:/home/user/repo",
        "C:\\Users\\me\\repo",
        "git@gitlab.com:acme/demo.git",
        "https://gitlab.com/acme/demo.git",
        "https://bitbucket.org/acme/demo.git",
        "git@bitbucket.org:acme/demo.git",
        "https://dev.azure.com/org/project/_git/demo",
        "git@ssh.dev.azure.com:v3/org/project/demo",
        "https://org.visualstudio.com/project/_git/demo",
        "https://example.com/acme/demo.git",
        "https://github.com.evil.example/acme/demo",
        "ssh://git@localhost/acme/demo.git",
    ],
)
def test_normalize_git_origin_rejects_local_and_unsupported(raw):
    from tools.cursor_agent_tool import UnsupportedOriginError, normalize_git_origin

    with pytest.raises(UnsupportedOriginError):
        normalize_git_origin(raw)


def test_resolve_workdir_origin_uses_git_remote(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    def _run(cmd, **kwargs):
        assert cmd[:4] == ["git", "-C", str(tmp_path), "remote"]
        return type("P", (), {"returncode": 0, "stdout": "git@github.com:acme/demo.git\n", "stderr": ""})()

    monkeypatch.setattr(cursor_agent_tool.subprocess, "run", _run)
    assert cursor_agent_tool.resolve_workdir_origin(str(tmp_path)) == "https://github.com/acme/demo"


def test_resolve_workdir_origin_rejects_local_remote(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr(
        cursor_agent_tool.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "/tmp/local-repo\n", "stderr": ""})(),
    )
    with pytest.raises(cursor_agent_tool.UnsupportedOriginError):
        cursor_agent_tool.resolve_workdir_origin(str(tmp_path))


def test_missing_key_file_fails_clearly(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    missing = tmp_path / "absent.env"
    monkeypatch.setattr(cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", missing)
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setenv("CURSOR_API_KEY", "should-not-be-used")

    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        _delegate(
            task="x",
            workdir=str(workdir.resolve()),
        )
    )
    assert result["success"] is False
    assert "CURSOR_API_KEY is missing" in result["error"]
    assert "no silent fallback" in result["error"]
    assert "should-not-be-used" not in result["error"]


def test_empty_key_fails_clearly(tmp_path):
    from tools.cursor_agent_tool import CursorApiKeyError, load_cursor_api_key

    empty = tmp_path / "empty.env"
    empty.write_text("CURSOR_API_KEY=\n", encoding="utf-8")
    with pytest.raises(CursorApiKeyError, match="missing or empty"):
        load_cursor_api_key(empty)


def test_load_cursor_api_key_does_not_use_environ(monkeypatch, tmp_path):
    from tools.cursor_agent_tool import load_cursor_api_key

    env_file = tmp_path / "cursor-cloud.env"
    env_file.write_text('CURSOR_API_KEY="file-key"\n', encoding="utf-8")
    monkeypatch.setenv("CURSOR_API_KEY", "env-key")
    assert load_cursor_api_key(env_file) == "file-key"


def test_secret_not_placed_in_argv_or_result(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    _install_cloud_happy_path(monkeypatch, tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        _delegate(
            task="secret check",
            workdir=str(workdir.resolve()),
        )
    )
    dumped = json.dumps(result)
    assert "test-secret-key" not in dumped
    assert _FakePopen.instances == []  # no local worker spawned
    assert result["progress_url"].startswith("https://cursor.com/agents/")


def test_worker_command_and_cleanup(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_ATTEMPTS", 1)
    monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_DELAY_SECONDS", 0)
    monkeypatch.setattr(cursor_agent_tool.subprocess, "Popen", _WorkerFakePopen)
    monkeypatch.setattr(cursor_agent_tool, "preflight_worker_auth", lambda *a, **k: None)

    worker = cursor_agent_tool.MachineWorker(
        binary="/usr/bin/agent",
        name="hermes-abc123def456",
        workdir=str(tmp_path),
        log_path=tmp_path / "worker.log",
    )
    worker.start()
    assert worker.cmd == [
        "/usr/bin/agent",
        "worker",
        "--name",
        "hermes-abc123def456",
        "--worker-dir",
        str(tmp_path),
        "--idle-release-timeout",
        "0",
        "start",
    ]
    assert "test-secret-key" not in worker.cmd
    assert "CURSOR_API_KEY" not in worker.env
    assert not hasattr(worker, "api_key")
    worker.cleanup()
    proc = _FakePopen.instances[0]
    assert proc.terminated or proc.killed


def test_build_create_agent_payload_no_pr_side_effects():
    from tools.cursor_agent_tool import (
        DEFAULT_ORCHESTRATION_PROMPT,
        NO_PUSH_PROMPT_PREFIX,
        build_create_agent_payload,
    )

    task = "fix the flaky test"
    payload = build_create_agent_payload(
        task=task,
        repo_url="https://github.com/acme/demo",
        machine_name="hermes-abc",
        agent_id="bc-aaaa",
        model="composer-2.5",
        starting_ref="main",
        force=True,
    )
    assert "env" not in payload  # Cursor-hosted runs carry no machine routing
    assert payload["repos"] == [{"url": "https://github.com/acme/demo", "startingRef": "main"}]
    assert payload["autoCreatePR"] is False
    assert payload["skipReviewerRequest"] is True
    assert payload["workOnCurrentBranch"] is False
    assert payload["agentId"] == "bc-aaaa"
    assert payload["model"] == {"id": "composer-2.5"}
    assert NO_PUSH_PROMPT_PREFIX
    assert DEFAULT_ORCHESTRATION_PROMPT
    assert NO_PUSH_PROMPT_PREFIX != DEFAULT_ORCHESTRATION_PROMPT
    prompt_text = payload["prompt"]["text"]
    assert prompt_text == NO_PUSH_PROMPT_PREFIX + DEFAULT_ORCHESTRATION_PROMPT + task


def test_timeout_dedupe_reuses_listed_agent(monkeypatch):
    from tools import cursor_agent_tool

    calls = []

    def _http(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            raise TimeoutError("Cursor Cloud Agent request timed out: POST /v1/agents")
        if method == "GET" and path == "/v1/agents":
            return {
                "items": [
                    {
                        "id": "bc-dup",
                        "name": "hermes-abc",
                        "env": {"type": "machine", "name": "hermes-abc"},
                        "url": "https://cursor.com/agents/bc-dup",
                        "latestRunId": "run-dup",
                    }
                ]
            }
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(cursor_agent_tool, "_http_request", _http)
    agent, run = cursor_agent_tool.create_agent_with_timeout_dedupe(
        {
            "agentId": "bc-dup",
            "name": "hermes-abc",
            "repos": [{"url": "https://github.com/acme/demo"}],
        },
        api_key="test-secret-key",
    )
    assert agent["id"] == "bc-dup"
    assert agent["url"] == "https://cursor.com/agents/bc-dup"
    assert run["id"] == "run-dup"
    assert calls == [("POST", "/v1/agents"), ("GET", "/v1/agents")]


def test_timeout_dedupe_retries_when_unlisted(monkeypatch):
    from tools import cursor_agent_tool

    calls = []

    def _http(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST" and calls.count(("POST", "/v1/agents")) == 1:
            raise TimeoutError("timed out")
        if method == "GET" and path == "/v1/agents":
            return {"items": []}
        if method == "POST":
            return {
                "agent": {
                    "id": "bc-new",
                    "url": "https://cursor.com/agents/bc-new",
                    "latestRunId": "run-new",
                },
                "run": {"id": "run-new", "agentId": "bc-new", "status": "CREATING"},
            }
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(cursor_agent_tool, "_http_request", _http)
    agent, run = cursor_agent_tool.create_agent_with_timeout_dedupe(
        {
            "agentId": "bc-new",
            "name": "hermes-abc",
            "repos": [{"url": "https://github.com/acme/demo"}],
        },
        api_key="test-secret-key",
    )
    assert agent["id"] == "bc-new"
    assert run["id"] == "run-new"
    assert calls == [
        ("POST", "/v1/agents"),
        ("GET", "/v1/agents"),
        ("POST", "/v1/agents"),
    ]


@pytest.mark.parametrize(
    "status,expect_success,expect_error",
    [
        ("FINISHED", True, None),
        ("ERROR", False, "boom"),
        ("CANCELLED", False, "cancelled"),
        ("EXPIRED", False, "expired"),
    ],
)
def test_poll_statuses_map_to_final_json(monkeypatch, tmp_path, status, expect_success, expect_error):
    from tools import cursor_agent_tool

    result_text = "boom" if status == "ERROR" else "done"
    _install_cloud_happy_path(
        monkeypatch, tmp_path, poll_status=status, result_text=result_text
    )
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        _delegate(
            task="status map",
            workdir=str(workdir.resolve()),
        )
    )
    assert result["success"] is expect_success
    assert result["cloud_status"] == status
    client_id = _default_client_agent_id()
    assert result["progress_url"] == f"https://cursor.com/agents/{client_id}"
    if expect_error is None:
        assert result["error"] is None
        assert result["final_report"] == "done"
    else:
        assert result["error"] == expect_error


@pytest.mark.parametrize(
    "result_text,failure_reason,expected_error",
    [
        ("", "Provider timeout exceeded", "Provider timeout exceeded"),
        ("boom", "Provider timeout exceeded", "boom"),
        ("", "", "Cursor Cloud Agent run failed"),
        ("", "   ", "Cursor Cloud Agent run failed"),
        ("", None, "Cursor Cloud Agent run failed"),
        ("", {"code": "x"}, "Cursor Cloud Agent run failed"),
        ("   ", "diagnostic from provider", "diagnostic from provider"),
        ("", "  something failed  ", "  something failed  "),
    ],
)
def test_cloud_error_surfaces_failure_reason_when_result_empty(
    result_text, failure_reason, expected_error
):
    from tools import cursor_agent_tool

    run = {
        "id": "run-err",
        "agentId": "agent-err",
        "status": "ERROR",
        "result": result_text,
    }
    if failure_reason is not None:
        run["failureReason"] = failure_reason

    result_json, success, outcome, cloud_status = (
        cursor_agent_tool._build_cloud_tool_result_from_run(
            agent={"id": "agent-err"},
            run=run,
            duration_seconds=1.0,
            log_path=None,
        )
    )
    result = json.loads(result_json)
    assert success is False
    assert outcome == "failed"
    assert cloud_status == "ERROR"
    assert result["error"] == expected_error
    assert result["final_report"] == result_text


def test_cloud_error_failure_reason_via_delegate(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    secret = tmp_path / "cursor-cloud.env"
    secret.write_text("CURSOR_API_KEY=test-secret-key\n", encoding="utf-8")
    monkeypatch.setattr(cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", secret)
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr(
        cursor_agent_tool,
        "resolve_workdir_origin",
        lambda workdir: "https://github.com/acme/demo",
    )
    monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_starting_ref", lambda workdir: "main")
    monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_ATTEMPTS", 1)
    monkeypatch.setattr(cursor_agent_tool, "WORKER_READY_DELAY_SECONDS", 0)
    monkeypatch.setattr(cursor_agent_tool, "POLL_INTERVAL_SECONDS", 0)
    client_id = _default_client_agent_id()
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: (
            {"id": client_id, "name": "hermes-test", "latestRunId": "run-aaaa"},
            {"id": "run-aaaa", "agentId": client_id, "status": "CREATING"},
        ),
    )
    monkeypatch.setattr(
        cursor_agent_tool,
        "poll_cloud_run",
        lambda **kwargs: {
            "id": "run-aaaa",
            "agentId": client_id,
            "status": "ERROR",
            "result": "",
            "failureReason": "sandbox provisioning failed",
            "durationMs": 500,
        },
    )
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        _delegate(
            task="error with failure reason",
            workdir=str(workdir.resolve()),
        )
    )
    assert result["success"] is False
    assert result["cloud_status"] == "ERROR"
    assert result["error"] == "sandbox provisioning failed"


def test_poll_honors_timeout_and_cancels(monkeypatch):
    from tools import cursor_agent_tool

    cancelled = []
    start = time.monotonic()

    def _fake_monotonic():
        return start + 120

    def _http(method, path, **kwargs):
        if method == "GET":
            return {
                "id": "run-1",
                "agentId": "bc-1",
                "status": "RUNNING",
            }
        if method == "POST" and path.endswith("/cancel"):
            cancelled.append(path)
            return {}
        raise AssertionError(path)

    monkeypatch.setattr(cursor_agent_tool, "_http_request", _http)
    monkeypatch.setattr(cursor_agent_tool.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(cursor_agent_tool, "_check_interrupted", lambda: False)
    run = cursor_agent_tool.poll_cloud_run(
        agent_id="bc-1",
        run_id="run-1",
        api_key="test-secret-key",
        timeout_seconds=60,
        started_mono=start,
    )
    assert run["_local_error"] == "timeout"
    assert cancelled == ["/v1/agents/bc-1/runs/run-1/cancel"]


def test_poll_honors_interrupt_and_cancels(monkeypatch):
    from tools import cursor_agent_tool

    cancelled = []

    def _http(method, path, **kwargs):
        if method == "POST" and path.endswith("/cancel"):
            cancelled.append(path)
            return {}
        raise AssertionError(path)

    monkeypatch.setattr(cursor_agent_tool, "_http_request", _http)
    monkeypatch.setattr(cursor_agent_tool, "_check_interrupted", lambda: True)
    run = cursor_agent_tool.poll_cloud_run(
        agent_id="bc-1",
        run_id="run-1",
        api_key="test-secret-key",
        timeout_seconds=0,
        started_mono=time.monotonic(),
    )
    assert run["_local_error"] == "interrupted"
    assert run["status"] == "CANCELLED"
    assert cancelled == ["/v1/agents/bc-1/runs/run-1/cancel"]


def test_poll_http_error_is_clear(monkeypatch):
    from tools import cursor_agent_tool

    def _http(method, path, **kwargs):
        raise cursor_agent_tool.CursorCloudError("Cursor Cloud Agent API error (GET /v1/agents/bc-1/runs/run-1): HTTP 500")

    monkeypatch.setattr(cursor_agent_tool, "_http_request", _http)
    monkeypatch.setattr(cursor_agent_tool, "_check_interrupted", lambda: False)
    with pytest.raises(cursor_agent_tool.CursorCloudError, match="HTTP 500"):
        cursor_agent_tool.poll_cloud_run(
            agent_id="bc-1",
            run_id="run-1",
            api_key="test-secret-key",
            timeout_seconds=0,
            started_mono=time.monotonic(),
        )


def test_progress_url_is_exact_api_value(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    notices = _install_cloud_happy_path(monkeypatch, tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        _delegate(
            task="url",
            workdir=str(workdir.resolve()),
        )
    )
    expected = f"https://cursor.com/agents/{_default_client_agent_id()}"
    assert result["progress_url"] == expected
    assert notices == [f"Cursor Cloud Agent: {expected}"]


def test_is_terminal_run_status():
    from tools.cursor_agent_tool import is_terminal_run_status

    assert is_terminal_run_status("FINISHED")
    assert is_terminal_run_status("error")
    assert is_terminal_run_status("CANCELLED")
    assert is_terminal_run_status("EXPIRED")
    assert not is_terminal_run_status("RUNNING")
    assert not is_terminal_run_status("CREATING")


def test_cursor_cloud_env_path_is_portable_home():
    from tools.cursor_agent_tool import CURSOR_CLOUD_ENV_PATH

    assert CURSOR_CLOUD_ENV_PATH == Path.home() / ".hermes" / "secrets" / "cursor-cloud.env"


def test_build_worker_command_option_order_matches_cli():
    from tools.cursor_agent_tool import build_worker_command

    cmd = build_worker_command("/usr/bin/agent", "hermes-abc123def456", "/tmp/repo")
    assert cmd == [
        "/usr/bin/agent",
        "worker",
        "--name",
        "hermes-abc123def456",
        "--worker-dir",
        "/tmp/repo",
        "--idle-release-timeout",
        "0",
        "start",
    ]
    assert cmd[2] != "start"


def test_worker_cli_parser_places_options_on_worker_not_start():
    agent = shutil.which("agent") or str(Path.home() / ".local" / "bin" / "agent")
    if not agent or not Path(agent).is_file():
        pytest.skip("agent CLI not installed")

    worker = subprocess.run(
        [agent, "worker", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    worker_help = f"{worker.stdout}\n{worker.stderr}"
    assert "--name" in worker_help
    assert "--worker-dir" in worker_help
    assert "--idle-release-timeout" in worker_help

    start = subprocess.run(
        [agent, "worker", "start", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    start_help = f"{start.stdout}\n{start.stderr}"
    assert "--name" not in start_help
    assert "--worker-dir" not in start_help
    assert "--idle-release-timeout" not in start_help


def _run_git(*args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_bare_origin(tmp_path: Path, *, extra_local_branch: str | None = None) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo.mkdir()
    _run_git("init", cwd=repo)
    _run_git("config", "user.email", "t@t.example", cwd=repo)
    _run_git("config", "user.name", "tester", cwd=repo)
    (repo / "keep-me.txt").write_text("preserve\n", encoding="utf-8")
    _run_git("add", "keep-me.txt", cwd=repo)
    _run_git("commit", "-m", "init", cwd=repo)
    _run_git("branch", "-M", "main", cwd=repo)
    _run_git("remote", "add", "origin", str(remote), cwd=repo)
    _run_git("push", "-u", "origin", "main", cwd=repo)
    if extra_local_branch:
        _run_git("checkout", "-b", extra_local_branch, cwd=repo)
    return repo


def _snapshot_git_state(repo: Path) -> dict[str, str]:
    return {
        "config": (repo / ".git" / "config").read_text(encoding="utf-8"),
        "marker": (repo / "keep-me.txt").read_text(encoding="utf-8"),
    }


def _assert_no_push_env(env: dict[str, str]) -> None:
    from tools.cursor_agent_tool import NO_PUSH_PUSHURL

    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "GITLAB_TOKEN",
        "GIT_CONFIG_PARAMETERS",
    ):
        assert key not in env
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    count = int(env["GIT_CONFIG_COUNT"])
    mapped = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(count)
    }
    assert mapped.get("remote.origin.pushurl") == NO_PUSH_PUSHURL
    assert mapped.get("credential.helper") == ""
    assert "github.com" not in mapped.get("remote.origin.pushurl", "")


def test_build_worker_env_strips_push_credentials(monkeypatch):
    from tools.cursor_agent_tool import build_worker_env

    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GIT_ASKPASS", "/bin/askpass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh.sock")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key")
    env = build_worker_env()
    _assert_no_push_env(env)
    assert "CURSOR_API_KEY" not in env
    assert "cursor-api-key" not in env.values()
    assert "gh-secret" not in env.values()
    assert "github-secret" not in env.values()


def test_remote_branch_includes_starting_ref(tmp_path):
    from tools.cursor_agent_tool import resolve_workdir_starting_ref

    repo = _init_repo_with_bare_origin(tmp_path)
    assert resolve_workdir_starting_ref(str(repo)) == "main"


def test_local_only_branch_omits_starting_ref(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    repo = _init_repo_with_bare_origin(
        tmp_path, extra_local_branch="feat/cursor-cloud-progress"
    )
    assert cursor_agent_tool.resolve_workdir_starting_ref(str(repo)) is None

    captured: dict = {}

    def _create(payload, api_key):
        captured["payload"] = payload
        client_id = _default_client_agent_id()
        return (
            {
                "id": client_id,
                "url": f"https://cursor.com/agents/{client_id}",
                "latestRunId": "run-local",
            },
            {"id": "run-local", "agentId": client_id, "status": "CREATING"},
        )

    _install_cloud_happy_path(monkeypatch, tmp_path)
    monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_starting_ref", lambda workdir: None)
    monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create)
    result = json.loads(
        _delegate(
            task="local only branch",
            workdir=str(repo.resolve()),
        )
    )
    assert result["success"] is True
    assert "startingRef" not in captured["payload"]["repos"][0]


def _run_no_push_delegate(monkeypatch, tmp_path, *, mutate):
    """Cursor-hosted runs never spawn a worker; assert the caller's repo is untouched."""
    from tools import cursor_agent_tool

    repo = _init_repo_with_bare_origin(tmp_path)
    before = _snapshot_git_state(repo)
    captured: dict = {}

    def _create(payload, api_key):
        captured["payload"] = payload
        client_id = _default_client_agent_id()
        return (
            {
                "id": client_id,
                "url": f"https://cursor.com/agents/{client_id}",
                "latestRunId": "run-nopush",
            },
            {"id": "run-nopush", "agentId": client_id, "status": "CREATING"},
        )

    _install_cloud_happy_path(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "should-not-reach-worker")
    monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_origin", lambda w: "https://github.com/acme/demo")
    monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_starting_ref", lambda w: "main")
    monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create)
    mutate(cursor_agent_tool, captured)
    result = json.loads(
        _delegate(
            task="no push",
            workdir=str(repo.resolve()),
        )
    )
    after = _snapshot_git_state(repo)
    assert after == before
    assert (repo / "keep-me.txt").read_text(encoding="utf-8") == "preserve\n"
    return result, captured


def test_no_push_restored_on_success(monkeypatch, tmp_path):
    result, captured = _run_no_push_delegate(monkeypatch, tmp_path, mutate=lambda *_: None)
    assert result["success"] is True, result.get("error")
    # no local process runs git on the caller's behalf anymore
    assert "GH_TOKEN" not in json.dumps(captured["payload"])


def test_no_push_restored_on_worker_start_failure(monkeypatch, tmp_path):
    """No worker exists in the Cursor-hosted lane; create failures keep the repo clean."""
    from tools import cursor_agent_tool

    def _mutate(mod, _captured):
        def _boom(*_a, **_k):
            raise mod.CursorCloudError("create failed hard")

        monkeypatch.setattr(mod, "create_agent_with_timeout_dedupe", _boom)

    result, captured = _run_no_push_delegate(monkeypatch, tmp_path, mutate=_mutate)
    assert result["success"] is False
    assert "create failed hard" in result["error"]

def test_no_push_restored_on_create_failure(monkeypatch, tmp_path):
    def _mutate(mod, _captured):
        def _create(*_a, **_k):
            raise mod.CursorCloudError("create failed")

        monkeypatch.setattr(mod, "create_agent_with_timeout_dedupe", _create)

    result, captured = _run_no_push_delegate(monkeypatch, tmp_path, mutate=_mutate)
    assert result["success"] is False
    assert "create failed" in result["error"]


def test_no_push_restored_on_poll_failure(monkeypatch, tmp_path):
    def _mutate(mod, _captured):
        def _poll(**_k):
            raise mod.CursorCloudError("poll failed")

        monkeypatch.setattr(mod, "poll_cloud_run", _poll)

    result, captured = _run_no_push_delegate(monkeypatch, tmp_path, mutate=_mutate)
    assert result["success"] is False
    assert "poll failed" in result["error"]
    assert "GH_TOKEN" not in json.dumps(captured["payload"])


def test_progress_url_emitted_once_before_poll_via_handle_function_call(monkeypatch, tmp_path):
    from model_tools import handle_function_call
    from tools import cursor_agent_tool
    from tools.cursor_run_receipts import deterministic_client_agent_id
    from tools.tool_status import emit_tool_status, tool_status_scope

    received: list[str] = []
    poll_saw: list[int] = []
    session_id = "progress-session"
    tool_call_id = "progress-call"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    expected = f"https://cursor.com/agents/{client_id}"

    def _poll(**_kwargs):
        poll_saw.append(len(received))
        return {
            "id": "run-aaaa",
            "agentId": client_id,
            "status": "FINISHED",
            "result": "done",
            "durationMs": 10,
        }

    _install_cloud_happy_path(monkeypatch, tmp_path, stub_progress_notice=False)
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: (
            {
                "id": client_id,
                "name": "hermes-test",
                "url": expected,
                "latestRunId": "run-aaaa",
            },
            {"id": "run-aaaa", "agentId": client_id, "status": "CREATING"},
        ),
    )
    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    with tool_status_scope(received.append):
        raw = handle_function_call(
            "delegate_cursor_agent",
            {"task": "ship it", "workdir": str(workdir.resolve())},
            session_id=session_id,
            tool_call_id=tool_call_id,
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

    result = json.loads(raw)
    assert result["success"] is True
    assert result["progress_url"] == expected
    assert received == [f"Cursor Cloud Agent: {expected}"]
    assert poll_saw == [1]
    assert emit_tool_status("after-scope") is False
    assert received == [f"Cursor Cloud Agent: {expected}"]


def test_progress_notice_is_noop_without_tool_status_context(monkeypatch, tmp_path):
    from model_tools import handle_function_call
    from tools import cursor_agent_tool
    from tools.cursor_run_receipts import deterministic_client_agent_id

    poll_saw: list[int] = []
    session_id = "noop-session"
    tool_call_id = "noop-call"
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    expected = f"https://cursor.com/agents/{client_id}"

    def _poll(**_kwargs):
        from tools.tool_status import get_tool_status_callback

        poll_saw.append(1 if get_tool_status_callback() else 0)
        return {
            "id": "run-aaaa",
            "agentId": client_id,
            "status": "FINISHED",
            "result": "done",
            "durationMs": 10,
        }

    _install_cloud_happy_path(monkeypatch, tmp_path, stub_progress_notice=False)
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: (
            {
                "id": client_id,
                "name": "hermes-test",
                "url": expected,
                "latestRunId": "run-aaaa",
            },
            {"id": "run-aaaa", "agentId": client_id, "status": "CREATING"},
        ),
    )
    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    raw = handle_function_call(
        "delegate_cursor_agent",
        {"task": "no context", "workdir": str(workdir.resolve())},
        session_id=session_id,
        tool_call_id=tool_call_id,
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["progress_url"] == expected
    assert poll_saw == [0]


def _exception_chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def test_machine_worker_has_no_api_key_attribute(tmp_path):
    from tools.cursor_agent_tool import MachineWorker

    worker = MachineWorker(
        binary="/usr/bin/agent",
        name="hermes-no-key",
        workdir=str(tmp_path),
        log_path=tmp_path / "w.log",
    )
    assert "api_key" not in worker.__dict__
    assert not hasattr(worker, "api_key")
    assert "CURSOR_API_KEY" not in worker.env


def test_real_subprocess_worker_env_omits_cursor_api_key(monkeypatch):
    import os

    from tools.cursor_agent_tool import (
        build_worker_env,
        worker_env_contains_cursor_api_key,
    )

    monkeypatch.setenv("CURSOR_API_KEY", "parent-secret-key")
    env = build_worker_env()
    assert "CURSOR_API_KEY" not in env
    assert worker_env_contains_cursor_api_key(env) is False
    assert os.environ["CURSOR_API_KEY"] == "parent-secret-key"


def test_http_status_error_has_no_exception_chain(monkeypatch):
    import httpx
    from tools import cursor_agent_tool

    leaked = "leaked-api-key"

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            del args, kwargs
            req = httpx.Request(
                "GET",
                "https://api.cursor.com/v1/x",
                headers={"Authorization": f"Basic {leaked}"},
            )
            resp = httpx.Response(401, request=req, text="unauthorized")
            raise httpx.HTTPStatusError("boom", request=req, response=resp)

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(cursor_agent_tool.CursorCloudError) as caught:
        cursor_agent_tool._http_request("GET", "/v1/x", api_key=leaked)
    exc = caught.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert leaked not in str(exc)
    assert "Authorization" not in str(exc)
    assert not any(isinstance(item, httpx.HTTPStatusError) for item in _exception_chain(exc))
    assert not any(isinstance(item, httpx.RequestError) for item in _exception_chain(exc))


def test_http_timeout_has_no_exception_chain(monkeypatch):
    import httpx
    from tools import cursor_agent_tool

    leaked = "timeout-secret-key"

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            del args, kwargs
            req = httpx.Request(
                "POST",
                "https://api.cursor.com/v1/agents",
                headers={"Authorization": f"Basic {leaked}"},
            )
            raise httpx.TimeoutException("slow", request=req)

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(TimeoutError) as caught:
        cursor_agent_tool._http_request("POST", "/v1/agents", api_key=leaked)
    exc = caught.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert leaked not in str(exc)
    assert "Authorization" not in str(exc)
    assert not any(isinstance(item, httpx.TimeoutException) for item in _exception_chain(exc))


def test_contract_docs_are_honest():
    from tools import cursor_agent_tool

    doc = cursor_agent_tool.__doc__ or ""
    lowered = " ".join(doc.lower().split())
    assert "not a hard sandbox" in lowered
    assert "isolated scratch" in lowered or "scratch clones" in lowered
    assert "worktrees" in lowered
    assert "github" in lowered
    assert "gitlab" not in lowered
    assert "bitbucket" not in lowered
    assert "azure" not in lowered
    assert "CURSOR_API_KEY" in doc
    assert "never receive" in lowered or "never includes" in lowered
    assert "machine login" in lowered
    schema = cursor_agent_tool.CURSOR_AGENT_SCHEMA["description"]
    assert "GitHub" in schema
    assert "GitLab" not in schema
    assert "Bitbucket" not in schema
    assert "Azure" not in schema


def test_diagnose_agent_status_authenticated_json():
    from tools.cursor_agent_tool import diagnose_agent_status

    raw = json.dumps(
        {
            "status": "authenticated",
            "isAuthenticated": True,
            "hasAccessToken": True,
            "userInfo": {"email": "user@example.com"},
        }
    )
    assert diagnose_agent_status(0, raw) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "unauthenticated", "isAuthenticated": False},
        {"isAuthenticated": False},
        {"status": "loggedOut"},
        {"status": "error", "isAuthenticated": False},
    ],
)
def test_diagnose_agent_status_unauthenticated_json(payload):
    from tools.cursor_agent_tool import WORKER_AUTH_ERROR, diagnose_agent_status

    assert diagnose_agent_status(0, json.dumps(payload)) == WORKER_AUTH_ERROR


def test_diagnose_agent_status_nonzero_does_not_leak_raw_log():
    from tools.cursor_agent_tool import WORKER_AUTH_ERROR, diagnose_agent_status

    raw = (
        "Authentication required for worker mode. Please run 'agent login', "
        "or provide an API key SECRET-XYZ"
    )
    error = diagnose_agent_status(1, raw, raw)
    assert error == WORKER_AUTH_ERROR
    assert "SECRET-XYZ" not in error
    assert "API key" not in error
    assert raw not in error


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not-json",
        "[]",
        '{"status":',
        '{"status": "unknown"}',
        "Authentication required for worker mode. Please run 'agent login'",
    ],
)
def test_diagnose_agent_status_malformed_does_not_leak_raw(stdout):
    from tools.cursor_agent_tool import WORKER_AUTH_ERROR, diagnose_agent_status

    error = diagnose_agent_status(0, stdout)
    assert error == WORKER_AUTH_ERROR
    assert "API key" not in error
    if stdout:
        assert stdout not in error


def test_preflight_worker_auth_uses_sanitized_env(monkeypatch):
    from tools import cursor_agent_tool

    captured: dict = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return type(
            "P",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps({"status": "authenticated", "isAuthenticated": True}),
                "stderr": "",
            },
        )()

    monkeypatch.setattr(cursor_agent_tool.subprocess, "run", _run)
    env = cursor_agent_tool.build_worker_env()
    env["CURSOR_API_KEY"] = "must-not-be-used"
    sanitized = dict(env)
    sanitized.pop("CURSOR_API_KEY", None)
    cursor_agent_tool.preflight_worker_auth("/usr/bin/agent", sanitized)
    assert captured["cmd"] == ["/usr/bin/agent", "status", "--format", "json"]
    assert captured["env"] is sanitized
    assert "CURSOR_API_KEY" not in captured["env"]
    assert "must-not-be-used" not in captured["cmd"]


def test_preflight_worker_auth_unauthenticated_json(monkeypatch):
    from tools import cursor_agent_tool

    def _run(cmd, **kwargs):
        del cmd, kwargs
        return type(
            "P",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {"status": "unauthenticated", "isAuthenticated": False}
                ),
                "stderr": "Authentication required. provide an API key SECRET-XYZ",
            },
        )()

    monkeypatch.setattr(cursor_agent_tool.subprocess, "run", _run)
    with pytest.raises(cursor_agent_tool.CursorCloudError) as caught:
        cursor_agent_tool.preflight_worker_auth("/usr/bin/agent", {"PATH": "/usr/bin"})
    assert str(caught.value) == cursor_agent_tool.WORKER_AUTH_ERROR
    assert "SECRET-XYZ" not in str(caught.value)
    assert "API key" not in str(caught.value)


def test_preflight_worker_auth_nonzero_status(monkeypatch):
    from tools import cursor_agent_tool

    def _run(cmd, **kwargs):
        del cmd, kwargs
        return type(
            "P",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "Authentication required for worker mode. Please run 'agent login'",
            },
        )()

    monkeypatch.setattr(cursor_agent_tool.subprocess, "run", _run)
    with pytest.raises(cursor_agent_tool.CursorCloudError) as caught:
        cursor_agent_tool.preflight_worker_auth("/usr/bin/agent", {"PATH": "/usr/bin"})
    assert str(caught.value) == cursor_agent_tool.WORKER_AUTH_ERROR
    assert "API key" not in str(caught.value)


def test_preflight_worker_auth_malformed_status(monkeypatch):
    from tools import cursor_agent_tool

    def _run(cmd, **kwargs):
        del cmd, kwargs
        return type("P", (), {"returncode": 0, "stdout": "not-json {", "stderr": ""})()

    monkeypatch.setattr(cursor_agent_tool.subprocess, "run", _run)
    with pytest.raises(cursor_agent_tool.CursorCloudError) as caught:
        cursor_agent_tool.preflight_worker_auth("/usr/bin/agent", {"PATH": "/usr/bin"})
    assert str(caught.value) == cursor_agent_tool.WORKER_AUTH_ERROR
    assert "not-json" not in str(caught.value)


def test_delegate_requires_cursor_login_for_cloud_api(monkeypatch, tmp_path):
    """Cloud-hosted runs authenticate via the API key file; a missing login is
    no longer fatal, but the tool must still refuse without credentials."""
    from tools import cursor_agent_tool

    monkeypatch.setattr(
        cursor_agent_tool,
        "CURSOR_CLOUD_ENV_PATH",
        tmp_path / "missing.env",
    )
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = json.loads(
        _delegate(
            task="needs key",
            workdir=str(workdir.resolve()),
        )
    )
    assert result["success"] is False
    assert "API key" not in (result.get("error") or "") or result["error"]
    assert _FakePopen.instances == []

def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Create a real repo with a bare `origin` remote, both in tmp_path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    git("add", "file.txt")
    git("commit", "-m", "c1")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        check=True, capture_output=True, text=True,
    )
    git("remote", "add", "origin", str(bare))
    git("push", "-u", "origin", "main")
    return repo, bare


def test_detect_unpushed_head_commits_synced(tmp_path):
    from tools import cursor_agent_tool

    repo, _bare = _init_repo_with_origin(tmp_path)
    assert cursor_agent_tool.detect_unpushed_head_commits(str(repo)) is None


def test_detect_unpushed_head_commits_ahead(tmp_path):
    from tools import cursor_agent_tool

    repo, _bare = _init_repo_with_origin(tmp_path)
    (repo / "file.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(
        ["git", "commit", "-am", "c2"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    reason = cursor_agent_tool.detect_unpushed_head_commits(str(repo))
    assert reason is not None
    assert "1 unpushed commit" in reason
    assert "'main'" in reason


def test_detect_unpushed_head_commits_behind_or_non_repo(tmp_path):
    from tools import cursor_agent_tool

    repo, _bare = _init_repo_with_origin(tmp_path)
    # Behind origin: local HEAD older than origin tip -> not unpushed work.
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    (repo / "file.txt").write_text("local-only\n", encoding="utf-8")
    subprocess.run(
        ["git", "commit", "-am", "c2"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "reset", "--hard", head_sha],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    assert cursor_agent_tool.detect_unpushed_head_commits(str(repo)) is None
    # Not a git repo at all -> cannot determine, no rejection.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cursor_agent_tool.detect_unpushed_head_commits(str(plain)) is None


def test_delegate_refuses_unpushed_workdir(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    monkeypatch.setattr(
        cursor_agent_tool,
        "CURSOR_CLOUD_ENV_PATH",
        tmp_path / "cursor-cloud.env",
    )
    (tmp_path / "cursor-cloud.env").write_text(
        "CURSOR_API_KEY=test-secret-key\n", encoding="utf-8"
    )
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr(
        cursor_agent_tool,
        "resolve_workdir_origin",
        lambda workdir: "https://github.com/acme/demo",
    )
    called = []
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: called.append(payload) or ({}, {}),
    )
    repo, _bare = _init_repo_with_origin(tmp_path)
    (repo / "file.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(
        ["git", "commit", "-am", "c2"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    result = json.loads(
        _delegate(task="should refuse", workdir=str(repo.resolve()))
    )
    assert result["success"] is False
    assert "unpushed commit" in result["error"]
    assert called == []
