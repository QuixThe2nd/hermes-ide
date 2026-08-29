"""Behavior-contract tests for delegate_claude_agent."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

# Reusable marker for tests that spawn a real (fake-binary) subprocess and
# signal it. The conftest live-system guard allows signals inside the test's
# own process subtree, so these do not need to bypass it — but we keep the
# marker for clarity and in case a stricter guard is added later.
_REAL_SUBPROC = pytest.mark.live_system_guard_bypass


def _write_fake_binary(tmp_path: Path, name: str = "claude-glm") -> Path:
    """Write a fake wrapper binary (claude-glm/claude-kimi) driven by FAKE_CLAUDE_* env vars."""
    script = f"""#!{sys.executable}
import json
import os
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "success")

argv_out = os.environ.get("FAKE_CLAUDE_ARGV_OUT")
if argv_out:
    with open(argv_out, "w", encoding="utf-8") as fh:
        json.dump(sys.argv, fh)

env_out = os.environ.get("FAKE_CLAUDE_ENV_OUT")
if env_out:
    with open(env_out, "w", encoding="utf-8") as fh:
        json.dump({{"HOME": os.environ.get("HOME"), "PATH": os.environ.get("PATH", "")}}, fh)

if mode == "sleep":
    try:
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "30")))
    except Exception:
        pass
    sys.exit(0)

if mode == "garbage":
    sys.stdout.write("not json line 1\\nnot json line 2\\n")
    sys.stdout.flush()
    sys.exit(0)

if mode == "empty":
    sys.exit(0)

if mode == "exitnonzero":
    sys.stdout.write('{{"type":"result","subtype":"success","is_error":false,"result":"x"}}\\n')
    sys.stdout.flush()
    sys.exit(2)

models_str = os.environ.get("FAKE_CLAUDE_MODELS", "glm-5.2")
model_usage = {{}}
for _name in models_str.split(","):
    _name = _name.strip()
    if _name:
        model_usage[_name] = {{"input_tokens": 100, "output_tokens": 50}}

prelude = os.environ.get("FAKE_CLAUDE_PRELUDE")
if prelude:
    sys.stdout.write(prelude + "\\n")
    sys.stdout.flush()

result = {{
    "type": "result",
    "subtype": os.environ.get("FAKE_CLAUDE_SUBTYPE", "success"),
    "is_error": os.environ.get("FAKE_CLAUDE_IS_ERROR", "false").lower() == "true",
    "result": os.environ.get("FAKE_CLAUDE_RESULT_TEXT", "Done."),
    "session_id": os.environ.get("FAKE_CLAUDE_SESSION_ID", "sess-claude-1"),
    "num_turns": int(os.environ.get("FAKE_CLAUDE_NUM_TURNS", "3")),
    "duration_ms": int(os.environ.get("FAKE_CLAUDE_DURATION_MS", "12345")),
    "total_cost_usd": float(os.environ.get("FAKE_CLAUDE_COST", "0.0123")),
    "modelUsage": model_usage,
    "permission_denials": json.loads(os.environ.get("FAKE_CLAUDE_DENIALS", "[]")),
}}
sys.stdout.write(json.dumps(result) + "\\n")
sys.stdout.flush()
"""
    binary = tmp_path / name
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    return _write_fake_binary(tmp_path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return workdir


def _patch_binary(monkeypatch, binary: Path) -> None:
    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary",
        lambda model=None: str(binary),
    )


# ---------------------------------------------------------------------------
# Schema + registration
# ---------------------------------------------------------------------------

def test_schema_registration():
    import tools.claude_agent_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("delegate_claude_agent")
    assert entry is not None
    assert entry.toolset == "delegation"
    assert entry.max_result_size_chars == 100_000

    schema = entry.schema
    required = set(schema["parameters"]["required"])
    assert required == {"task", "workdir"}

    props = schema["parameters"]["properties"]
    assert "default" not in props["model"]
    assert props["timeout_seconds"]["default"] == 0
    assert props["allowed_tools"]["default"] == "Read,Write,Edit,Glob,Grep,Bash"
    assert props["permission_mode"]["default"] == "acceptEdits"
    assert set(props["permission_mode"]["enum"]) == {"acceptEdits", "plan"}


# ---------------------------------------------------------------------------
# Lane-rule schema contract (claude / cursor / file tools)
# ---------------------------------------------------------------------------

def test_claude_schema_description_states_lane_rule():
    import tools.claude_agent_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("delegate_claude_agent")
    assert entry is not None
    description = entry.schema["description"]
    assert "MEDIUM TO LARGE" in description
    assert "delegate_cursor_agent" in description
    assert "/goal" in description


def test_claude_task_description_requires_verifiable_done_condition():
    import tools.claude_agent_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("delegate_claude_agent")
    assert entry is not None
    task = entry.schema["parameters"]["properties"]["task"]["description"]
    assert "/goal" in task
    assert "done condition" in task


def test_cursor_schema_description_states_lane_rule():
    import tools.cursor_agent_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("delegate_cursor_agent")
    assert entry is not None
    description = entry.schema["description"]
    assert "SMALL TO MEDIUM" in description
    assert "delegate_claude_agent" in description


def test_file_tool_descriptions_route_source_edits_to_delegate_lanes():
    import tools.file_tools  # noqa: F401

    from tools.file_tools import PATCH_SCHEMA, WRITE_FILE_SCHEMA

    for schema in (WRITE_FILE_SCHEMA, PATCH_SCHEMA):
        description = schema["description"]
        assert "delegate_cursor_agent" in description, schema["name"]
        assert "delegate_claude_agent" in description, schema["name"]


# ---------------------------------------------------------------------------
# Binary resolution + gating
# ---------------------------------------------------------------------------

def test_check_fn_binary_found(monkeypatch, fake_binary):
    from tools.claude_agent_tool import check_claude_agent_requirements

    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary", lambda: str(fake_binary)
    )
    assert check_claude_agent_requirements() is True


def test_check_fn_binary_missing(monkeypatch):
    from tools.claude_agent_tool import check_claude_agent_requirements

    monkeypatch.setattr("tools.claude_agent_tool.resolve_claude_binary", lambda: None)
    assert check_claude_agent_requirements() is False


def test_resolve_env_override(monkeypatch, fake_binary, tmp_path):
    from tools.claude_agent_tool import resolve_claude_binary

    monkeypatch.setenv("CLAUDE_GLM_BIN", str(fake_binary))
    assert resolve_claude_binary() == str(fake_binary)


def test_resolve_env_override_must_be_executable_file(monkeypatch, tmp_path):
    from tools.claude_agent_tool import resolve_claude_binary

    bogus = tmp_path / "nope"
    monkeypatch.setenv("CLAUDE_GLM_BIN", str(bogus))
    # Override points at a non-existent file → fall through to the rest.
    monkeypatch.setattr("tools.claude_agent_tool.shutil.which", lambda name: None)
    # A plain non-existent Path: is_file() is naturally False, exercising the
    # real branch without the _flavour breakage a Path subclass would cause.
    monkeypatch.setattr(
        "tools.claude_agent_tool._local_bin_claude_glm_path",
        lambda: Path("/nope/claude-glm"),
    )
    assert resolve_claude_binary() is None


def test_resolve_local_bin(monkeypatch, fake_binary):
    from tools.claude_agent_tool import resolve_claude_binary

    monkeypatch.delenv("CLAUDE_GLM_BIN", raising=False)
    monkeypatch.setattr("tools.claude_agent_tool.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "tools.claude_agent_tool._local_bin_claude_glm_path", lambda: fake_binary
    )
    assert resolve_claude_binary() == str(fake_binary)


def test_resolve_path_fallbacks(monkeypatch, tmp_path):
    from tools.claude_agent_tool import resolve_claude_binary

    monkeypatch.delenv("CLAUDE_GLM_BIN", raising=False)
    monkeypatch.setattr(
        "tools.claude_agent_tool._local_bin_claude_glm_path",
        lambda: Path("/nope/claude-glm"),
    )
    # claude-glm on PATH wins over bare claude.
    monkeypatch.setattr(
        "tools.claude_agent_tool.shutil.which",
        lambda name: "/usr/bin/claude-glm" if name == "claude-glm" else "/usr/bin/claude",
    )
    assert resolve_claude_binary() == "/usr/bin/claude-glm"

    # Falls back to bare claude when claude-glm is absent.
    monkeypatch.setattr(
        "tools.claude_agent_tool.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    assert resolve_claude_binary() == "/usr/bin/claude"

    # Nothing resolvable → None.
    monkeypatch.setattr("tools.claude_agent_tool.shutil.which", lambda name: None)
    assert resolve_claude_binary() is None


def test_classify_model_family():
    from tools.claude_agent_tool import (
        MODEL_FAMILY_GLM,
        MODEL_FAMILY_KIMI,
        classify_model_family,
    )

    assert classify_model_family(None) == MODEL_FAMILY_GLM
    assert classify_model_family("") == MODEL_FAMILY_GLM
    assert classify_model_family("glm-5.2") == MODEL_FAMILY_GLM
    assert classify_model_family("kimi-k3") == MODEL_FAMILY_KIMI
    # Family detection is case-insensitive.
    assert classify_model_family("Kimi-K3") == MODEL_FAMILY_KIMI
    assert classify_model_family("KIMI-K3") == MODEL_FAMILY_KIMI


def test_resolve_kimi_env_override(monkeypatch, tmp_path):
    from tools.claude_agent_tool import resolve_claude_binary

    kimi_bin = _write_fake_binary(tmp_path, name="claude-kimi")
    monkeypatch.setenv("CLAUDE_KIMI_BIN", str(kimi_bin))
    assert resolve_claude_binary("kimi-k3") == str(kimi_bin)
    # Case-insensitive family detection routes "Kimi-K3" the same way.
    assert resolve_claude_binary("Kimi-K3") == str(kimi_bin)


def test_resolve_glm_default_ignores_kimi_env(monkeypatch, fake_binary, tmp_path):
    from tools.claude_agent_tool import resolve_claude_binary

    kimi_bin = _write_fake_binary(tmp_path, name="claude-kimi")
    monkeypatch.setenv("CLAUDE_GLM_BIN", str(fake_binary))
    monkeypatch.setenv("CLAUDE_KIMI_BIN", str(kimi_bin))
    # No model / glm-family models keep using the GLM lane exactly as before,
    # even when a kimi wrapper is configured.
    assert resolve_claude_binary() == str(fake_binary)
    assert resolve_claude_binary("glm-5.2") == str(fake_binary)


def test_resolve_kimi_never_falls_back_to_glm(monkeypatch, fake_binary):
    from tools.claude_agent_tool import resolve_claude_binary

    monkeypatch.setenv("CLAUDE_GLM_BIN", str(fake_binary))
    monkeypatch.delenv("CLAUDE_KIMI_BIN", raising=False)
    monkeypatch.setattr(
        "tools.claude_agent_tool._local_bin_claude_kimi_path",
        lambda: Path("/nope/claude-kimi"),
    )
    monkeypatch.setattr("tools.claude_agent_tool.shutil.which", lambda name: None)
    # Even with a perfectly good GLM wrapper available, a kimi request
    # resolves to nothing rather than silently running the wrong provider.
    assert resolve_claude_binary("kimi-k3") is None


# ---------------------------------------------------------------------------
# Timeout clamping
# ---------------------------------------------------------------------------

def test_clamp_timeout_seconds():
    from tools.claude_agent_tool import (
        DEFAULT_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
        MIN_TIMEOUT_SECONDS,
        _clamp_timeout_seconds,
    )

    assert _clamp_timeout_seconds(59) == MIN_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(3601) == MAX_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds("garbage") == DEFAULT_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(None) == DEFAULT_TIMEOUT_SECONDS
    assert _clamp_timeout_seconds(0) == 0
    assert _clamp_timeout_seconds(-5) == 0


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def test_parse_result_extracts_fields():
    from tools.claude_agent_tool import parse_claude_agent_log

    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "All done.",
            "session_id": "sess-1",
            "num_turns": 4,
            "duration_ms": 999,
            "total_cost_usd": 0.05,
            "modelUsage": {
                "glm-5.2": {"input_tokens": 10},
                "claude-haiku-4-5": {"input_tokens": 5},
            },
            "permission_denials": [{"tool": "Bash"}],
        }
    )
    parsed = parse_claude_agent_log(line + "\n")
    assert parsed["subtype"] == "success"
    assert parsed["is_error"] is False
    assert parsed["result"] == "All done."
    assert parsed["session_id"] == "sess-1"
    assert parsed["num_turns"] == 4
    assert parsed["duration_ms"] == 999
    assert parsed["total_cost_usd"] == 0.05
    assert parsed["models_used"] == ["claude-haiku-4-5", "glm-5.2"]
    assert parsed["permission_denials"] == [{"tool": "Bash"}]


def test_parse_last_result_line_wins():
    from tools.claude_agent_tool import parse_claude_agent_log

    first = json.dumps({"type": "result", "subtype": "success", "result": "old"})
    second = json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "new"})
    parsed = parse_claude_agent_log("\n".join([first, second]))
    assert parsed["result"] == "new"
    assert parsed["subtype"] == "error"


def test_parse_no_result_event_returns_empty():
    from tools.claude_agent_tool import parse_claude_agent_log

    assert parse_claude_agent_log("not json\n") == {}
    assert parse_claude_agent_log("") == {}
    assert parse_claude_agent_log('{"type":"assistant","message":{}}') == {}


def test_parse_stream_json_skips_events_finds_final_result():
    """stream-json logs carry many event types before the final result line."""
    from tools.claude_agent_tool import parse_claude_agent_log

    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-9"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y.py"}}
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
    ]
    result = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done via stream.",
            "session_id": "sess-9",
            "num_turns": 3,
            "total_cost_usd": 0.11,
            "modelUsage": {"kimi-k3": {"input_tokens": 1}},
        }
    )
    log_text = "\n".join(json.dumps(e) for e in events) + "\n" + result + "\n"
    parsed = parse_claude_agent_log(log_text)
    assert parsed["result"] == "Done via stream."
    assert parsed["session_id"] == "sess-9"
    assert parsed["models_used"] == ["kimi-k3"]


# ---------------------------------------------------------------------------
# Validation paths (no subprocess spawned)
# ---------------------------------------------------------------------------

def test_validation_errors_use_full_result_shape(monkeypatch, tmp_path, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)

    empty = json.loads(claude_agent_tool.delegate_claude_agent(task="", workdir=str(tmp_path)))
    assert empty["success"] is False
    assert empty["error"]
    assert empty["log_path"] is None
    assert "final_report" in empty
    assert "models_used" in empty
    assert "permission_denials" in empty

    relative = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir="relative/path")
    )
    assert relative["success"] is False
    assert "absolute path" in relative["error"]

    missing = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="x",
            workdir=str((tmp_path / "missing").resolve()),
        )
    )
    assert missing["success"] is False
    assert "does not exist" in missing["error"]


def test_permission_mode_validation_rejects_unknown(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="x",
            workdir=str(repo),
            permission_mode="yolo",
        )
    )
    assert result["success"] is False
    assert "permission_mode" in result["error"]
    assert "acceptEdits" in result["error"]


def test_binary_missing_returns_error(monkeypatch, repo):
    from tools import claude_agent_tool

    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary", lambda model=None: None
    )
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "not found" in result["error"]


def test_kimi_binary_missing_names_kimi_wrapper(monkeypatch, repo):
    from tools import claude_agent_tool

    # Seal every kimi resolution route so the real ~/.local/bin/claude-kimi
    # on this machine cannot leak in.
    monkeypatch.delenv("CLAUDE_KIMI_BIN", raising=False)
    monkeypatch.setattr(
        "tools.claude_agent_tool._local_bin_claude_kimi_path",
        lambda: Path("/nope/claude-kimi"),
    )
    monkeypatch.setattr("tools.claude_agent_tool.shutil.which", lambda name: None)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="x", workdir=str(repo), model="kimi-k3"
        )
    )
    assert result["success"] is False
    assert "not found" in result["error"]
    assert "claude-kimi" in result["error"]


# ---------------------------------------------------------------------------
# Happy path + result parsing (real fake-binary subprocess)
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_happy_path_e2e(monkeypatch, repo, fake_binary, tmp_path):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "Implemented feature.")
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_ID", "sess-claude-xyz")
    monkeypatch.setenv("FAKE_CLAUDE_NUM_TURNS", "5")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.0775")
    monkeypatch.setenv("FAKE_CLAUDE_MODELS", "glm-5.2,claude-haiku-4-5")
    monkeypatch.setenv("FAKE_CLAUDE_DENIALS", '[{"tool":"Bash","reason":"nope"}]')
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="implement feature",
            workdir=str(repo),
        )
    )

    assert result["success"] is True
    assert result["error"] is None
    assert result["final_report"] == "Implemented feature."
    assert result["session_id"] == "sess-claude-xyz"
    assert result["num_turns"] == 5
    assert result["cost_usd"] == 0.0775
    assert result["models_used"] == ["claude-haiku-4-5", "glm-5.2"]
    assert result["permission_denials"] == [{"tool": "Bash", "reason": "nope"}]
    assert "claude-runs" in result["log_path"]
    assert Path(result["log_path"]).is_file()

    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[0] == str(fake_binary)
    assert "-p" in argv
    assert "--model" not in argv
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Write,Edit,Glob,Grep,Bash"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[-1] == "implement feature"
    # --dangerously-skip-permissions must never be passed (refused under root).
    assert "--dangerously-skip-permissions" not in argv


@_REAL_SUBPROC
def test_kimi_model_runs_kimi_wrapper(monkeypatch, repo, tmp_path):
    from tools import claude_agent_tool

    kimi_bin = _write_fake_binary(tmp_path, name="claude-kimi")
    monkeypatch.setenv("CLAUDE_KIMI_BIN", str(kimi_bin))
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="write kimi docs",
            workdir=str(repo),
            model="kimi-k3",
        )
    )
    assert result["success"] is True

    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[0] == str(kimi_bin)
    assert argv[argv.index("--model") + 1] == "kimi-k3"


@_REAL_SUBPROC
def test_plan_permission_mode_passed_through(monkeypatch, repo, fake_binary, tmp_path):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))

    claude_agent_tool.delegate_claude_agent(
        task="plan something",
        workdir=str(repo),
        permission_mode="plan",
    )
    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[argv.index("--permission-mode") + 1] == "plan"


@_REAL_SUBPROC
def test_is_error_path(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_SUBTYPE", "error")
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "true")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "Boom.")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert result["final_report"] == "Boom."
    assert "error" in result["error"] or "is_error" in result["error"]


@_REAL_SUBPROC
def test_malformed_output_no_result_event(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "no result event" in result["error"]
    assert Path(result["log_path"]).is_file()


@_REAL_SUBPROC
def test_nonzero_exit_reports_failure(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exitnonzero")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "exited with code 2" in result["error"]


@_REAL_SUBPROC
def test_timeout_kills_process_group(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")

    # Fake a fast wall-clock so timeout_seconds=60 trips within milliseconds.
    start = time.monotonic()
    calls = {"n": 0}

    def _fake_monotonic():
        calls["n"] += 1
        return start + calls["n"] * 40

    monkeypatch.setattr("tools.claude_agent_tool.time.monotonic", _fake_monotonic)
    monkeypatch.setattr(claude_agent_tool, "STALL_WATCHDOG_SECONDS", 9999)
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.001)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="timeout test",
            workdir=str(repo),
            timeout_seconds=60,
        )
    )
    assert result["success"] is False
    assert result["error"] == "timeout"


@_REAL_SUBPROC
def test_child_env_guarantees_home_and_local_bin(monkeypatch, repo, fake_binary, tmp_path):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.delenv("HOME", raising=False)
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CLAUDE_ENV_OUT", str(env_out))

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is True

    captured = json.loads(env_out.read_text(encoding="utf-8"))
    # HOME must be present and non-empty even though the caller env lacked it;
    # the wrapper runs with `set -u` and dies on an unbound $HOME.
    assert captured["HOME"] == str(Path.home())
    # ~/.local/bin is prepended so binary resolution works under minimal PATH.
    assert captured["PATH"].split(os.pathsep)[0] == str(Path.home() / ".local" / "bin")


# ---------------------------------------------------------------------------
# Completion signals: empty-report guard + degraded-run warnings
# ---------------------------------------------------------------------------

def test_extract_log_warnings_finds_unrecognized_model_line():
    from tools.claude_agent_tool import extract_log_warnings

    log_text = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s"}),
            'warning: unrecognized_model "glm-5.3", falling back to default model',
            json.dumps({"type": "result", "subtype": "success", "result": "Done."}),
        ]
    )
    warnings = extract_log_warnings(log_text)
    assert len(warnings) == 1
    assert "unrecognized_model" in warnings[0]


def test_extract_log_warnings_clean_log_is_empty():
    from tools.claude_agent_tool import extract_log_warnings

    assert extract_log_warnings("") == []
    assert extract_log_warnings("not json\n") == []
    assert extract_log_warnings('{"type": "result", "result": "ok"}\n') == []


def test_extract_log_warnings_dedupes_and_caps():
    from tools.claude_agent_tool import extract_log_warnings

    line = 'warning: unrecognized_model "glm-5.3"'
    warnings = extract_log_warnings("\n".join([line, line, line]))
    assert warnings == [line]

    many = [f'warning: unrecognized_model "m{i}"' for i in range(10)]
    assert len(extract_log_warnings("\n".join(many))) == 5


@_REAL_SUBPROC
def test_success_with_empty_final_report_is_failure(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "empty final report" in result["error"]


@_REAL_SUBPROC
def test_success_with_whitespace_final_report_is_failure(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "   ")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "empty final report" in result["error"]


@_REAL_SUBPROC
def test_error_subtype_keeps_original_error_for_empty_report(
    monkeypatch, repo, fake_binary
):
    """The empty-report guard must not rewrite genuine failure results."""
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv("FAKE_CLAUDE_SUBTYPE", "error")
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "true")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "")

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is False
    assert "subtype" in result["error"]


@_REAL_SUBPROC
def test_unrecognized_model_warning_surfaced_in_result(
    monkeypatch, repo, fake_binary
):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    monkeypatch.setenv(
        "FAKE_CLAUDE_PRELUDE",
        'warning: unrecognized_model "glm-5.3", falling back to default model',
    )

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is True
    assert any("unrecognized_model" in w for w in result["warnings"])


@_REAL_SUBPROC
def test_clean_run_has_empty_warnings(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is True
    assert result["warnings"] == []


# ---------------------------------------------------------------------------
# Live-progress viewer notice (spawn-time status line)
# ---------------------------------------------------------------------------

# Same run-stem shape the Discord adapter fullmatches before rendering the
# notice as a branded embed (plugins/platforms/discord/adapter.py).
_RUN_STEM_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]+")


def test_viewer_status_line_prefix_and_stem_roundtrip():
    from tools.claude_agent_tool import (
        CLAUDE_VIEWER_HOST,
        CLAUDE_VIEWER_PORT,
        _claude_viewer_status_line,
    )

    # The host/port are the fork-deployment constants the adapter allowlists.
    assert CLAUDE_VIEWER_HOST == "192.168.30.20"
    assert CLAUDE_VIEWER_PORT == 8787

    stem = "20260829-115024-2006506"
    line = _claude_viewer_status_line(stem)
    assert line == "Claude Code Agent: http://192.168.30.20:8787/#" + stem
    assert line.startswith("Claude Code Agent: http://192.168.30.20:8787/#")
    # The stem comes back out unchanged, in the adapter's run-stem format.
    assert line.rsplit("#", 1)[1] == stem
    assert _RUN_STEM_RE.fullmatch(line.rsplit("#", 1)[1])


@_REAL_SUBPROC
def test_run_agent_cli_invokes_on_spawn_once_with_resolved_log_path(tmp_path):
    from tools.agent_cli_runner import run_agent_cli

    spawned: list = []

    error_code, log_path, log_text, duration, returncode = run_agent_cli(
        [sys.executable, "-c", "print('hi')"],
        workdir=str(tmp_path),
        log_dir=tmp_path / "logs",
        run_timestamp="20260829-115024",
        on_spawn=spawned.append,
    )

    assert error_code is None
    assert returncode == 0
    assert log_text == "hi\n"
    # Exactly one call, carrying the very log path the run streams into.
    assert spawned == [Path(log_path)]
    assert Path(log_path).is_file()


@_REAL_SUBPROC
def test_run_agent_cli_on_spawn_raising_leaves_run_unaffected(tmp_path):
    from tools.agent_cli_runner import run_agent_cli

    def _boom(log_path: Path) -> None:
        raise RuntimeError("callback exploded")

    error_code, log_path, log_text, duration, returncode = run_agent_cli(
        [sys.executable, "-c", "print('hi')"],
        workdir=str(tmp_path),
        log_dir=tmp_path / "logs",
        run_timestamp="20260829-115024",
        on_spawn=_boom,
    )

    # A broken callback is swallowed: the run itself still completes intact.
    assert error_code is None
    assert returncode == 0
    assert log_text == "hi\n"
    assert Path(log_path).is_file()


@_REAL_SUBPROC
def test_delegate_emits_viewer_notice_once_at_spawn(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool
    from tools.tool_status import tool_status_scope

    _patch_binary(monkeypatch, fake_binary)
    emitted: list = []

    with tool_status_scope(emitted.append):
        result = json.loads(
            claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
        )

    assert result["success"] is True
    assert len(emitted) == 1
    # The notice names exactly the run's own log stem in adapter-stem format.
    stem = Path(result["log_path"]).stem
    assert emitted[0] == claude_agent_tool._claude_viewer_status_line(stem)
    assert _RUN_STEM_RE.fullmatch(emitted[0].rsplit("#", 1)[1])


@_REAL_SUBPROC
def test_delegate_run_unaffected_when_no_status_callback_bound(
    monkeypatch, repo, fake_binary
):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    # No tool_status_scope bound: the emit is a no-op and the run succeeds.
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task="x", workdir=str(repo))
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# /goal condition 4000-char pre-flight spill
# ---------------------------------------------------------------------------

GOAL_BRIEF_NAME = ".hermes-claude-goal-brief.md"


def _patch_spawn(monkeypatch, captured: dict) -> None:
    """Mock the spawn layer, capturing argv; no real CLI is invoked."""
    from tools import claude_agent_tool

    def _fake_run_and_stream(
        cmd, *, workdir, timeout_seconds, log_dir, run_timestamp, on_spawn=None
    ):
        captured["cmd"] = list(cmd)
        captured["workdir"] = workdir
        log_text = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Done.",
            }
        )
        return None, str(log_dir / "fake-log.jsonl"), log_text, 0.5, 0

    monkeypatch.setattr(claude_agent_tool, "_run_and_stream", _fake_run_and_stream)


def _p_value(cmd: list) -> str:
    # The task prompt is the trailing positional argument after the flags.
    return cmd[-1]


def _goal_condition(cmd: list) -> str:
    return _p_value(cmd)[len("/goal "):]


def test_goal_condition_exactly_at_limit_passes_through(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    condition = "x" * 4000
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="/goal " + condition, workdir=str(repo)
        )
    )
    assert result["success"] is True
    # At exactly 4000 chars the task is handed to the CLI untouched.
    assert _p_value(captured["cmd"]) == "/goal " + condition
    assert not (repo / GOAL_BRIEF_NAME).exists()
    assert result["goal_brief_path"] is None


def test_goal_condition_4001_spills_to_brief(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    task = "/goal " + "x" * 4001
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task=task, workdir=str(repo))
    )
    assert result["success"] is True

    brief = repo / GOAL_BRIEF_NAME
    assert brief.is_file()
    assert brief.read_text(encoding="utf-8") == task
    assert (brief.stat().st_mode & 0o777) == 0o644

    p_value = _p_value(captured["cmd"])
    assert p_value.startswith("/goal ")
    condition = _goal_condition(captured["cmd"])
    assert len(condition) <= 4000
    assert GOAL_BRIEF_NAME in condition
    assert result["goal_brief_path"] == str(brief)


def test_goal_spill_preserves_first_line_and_keeps_full_brief(
    monkeypatch, repo, fake_binary
):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    task = "/goal tests are green\n" + "y" * 5000
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task=task, workdir=str(repo))
    )
    assert result["success"] is True

    condition = _goal_condition(captured["cmd"])
    assert condition.startswith("tests are green ")
    assert len(condition) <= 4000
    assert GOAL_BRIEF_NAME in condition

    # The file is the source of truth: full original task, long remainder
    # included.
    brief = repo / GOAL_BRIEF_NAME
    assert brief.read_text(encoding="utf-8") == task


def test_goal_detection_ignores_leading_whitespace(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    task = "\n   /goal " + "x" * 4001
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task=task, workdir=str(repo))
    )
    assert result["success"] is True
    assert _p_value(captured["cmd"]).startswith("/goal ")
    assert len(_goal_condition(captured["cmd"])) <= 4000
    brief = repo / GOAL_BRIEF_NAME
    assert brief.is_file()
    # The brief holds the original task unmodified, leading whitespace and all.
    assert brief.read_text(encoding="utf-8") == task


def test_goal_detection_is_case_insensitive(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    task = "/GOAL " + "x" * 4001
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task=task, workdir=str(repo))
    )
    assert result["success"] is True
    assert (repo / GOAL_BRIEF_NAME).is_file()
    assert _p_value(captured["cmd"]).startswith("/goal ")
    assert len(_goal_condition(captured["cmd"])) <= 4000


@pytest.mark.parametrize("separator", ["\v", "\f", " "])
def test_goal_spills_with_unicode_whitespace_separator(
    monkeypatch, repo, fake_binary, separator
):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    task = "/goal" + separator + "x" * 4001
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task=task, workdir=str(repo))
    )
    assert result["success"] is True
    # Any str.isspace() character separates /goal from its condition, so the
    # overlong condition spills to the brief like a plain space would.
    assert (repo / GOAL_BRIEF_NAME).is_file()
    assert (repo / GOAL_BRIEF_NAME).read_text(encoding="utf-8") == task
    assert _p_value(captured["cmd"]).startswith("/goal ")
    assert len(_goal_condition(captured["cmd"])) <= 4000
    assert result["goal_brief_path"] == str(repo / GOAL_BRIEF_NAME)


def test_goalkeeper_task_not_treated_as_goal(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    task = "/goalkeeper " + "x" * 5000
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task=task, workdir=str(repo))
    )
    assert result["success"] is True
    # "/goal" must be a whole token: "/goalkeeper" is a different command,
    # so the long task passes through with no spill or rewrite.
    assert _p_value(captured["cmd"]) == task
    assert not (repo / GOAL_BRIEF_NAME).exists()
    assert result["goal_brief_path"] is None


def test_non_goal_long_task_untouched(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    task = "z" * 10_000
    result = json.loads(
        claude_agent_tool.delegate_claude_agent(task=task, workdir=str(repo))
    )
    assert result["success"] is True
    # One-shot tasks get no length check, no file, no rewrite.
    assert _p_value(captured["cmd"]) == task
    assert not (repo / GOAL_BRIEF_NAME).exists()
    assert result["goal_brief_path"] is None


def test_goal_spill_write_failure_aborts_before_spawn(monkeypatch, repo, fake_binary):
    from tools import claude_agent_tool

    captured: dict = {}
    _patch_spawn(monkeypatch, captured)
    _patch_binary(monkeypatch, fake_binary)

    def _raise(self, *args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _raise)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="/goal " + "x" * 4001, workdir=str(repo)
        )
    )
    assert result["success"] is False
    assert str(repo / GOAL_BRIEF_NAME) in result["error"]
    assert "read-only filesystem" in result["error"]
    # The write failure must abort before anything is spawned.
    assert captured == {}
