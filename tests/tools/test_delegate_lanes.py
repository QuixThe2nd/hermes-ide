"""Behavior contracts for opt-in delegation lanes and the OMP backend."""

import copy
import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as generic_delegate
from hermes_constants import get_hermes_home
from tools import delegate_lane_specs as specs
from tools import delegate_lane_tool as lane_tool
from tools import delegate_tool_omp as omp
from tools.delegate_lane_specs import LANE_NAMES, LANE_TOOL_NAMES, resolve_lane_spec
from tools.registry import registry


@pytest.fixture
def workdir(tmp_path):
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


def _configure(monkeypatch, lanes, omp_config=None, **delegation):
    config = {"lanes": lanes, "omp": omp_config or {}, **delegation}
    monkeypatch.setattr(specs, "_cfg", lambda: config)
    monkeypatch.setattr(lane_tool, "_cfg", lambda: config)


@pytest.mark.parametrize("lane_name,harness", [
    ("ui_dev", "omp"),
    ("client_dev", "claude"),
    ("backend_dev", "cursor"),
    ("reverse_engineer", "subagent"),
])
def test_each_lane_resolves_harness_and_ordered_models(lane_name, harness):
    spec = resolve_lane_spec(lane_name, {
        lane_name: {"harness": harness, "models": ["provider/first", "provider/second"]},
    })
    assert spec["name"] == lane_name
    assert spec["harness"] == harness
    assert spec["models"] == ["provider/first", "provider/second"]


def test_config_can_override_one_field_without_losing_lane_defaults():
    default = resolve_lane_spec("ui_dev", {})
    overridden = resolve_lane_spec("delegate_ui_dev", {"ui_dev": {"reviewer": "UI reviewer"}})
    assert overridden["reviewer"] == "UI reviewer"
    assert overridden["models"] == default["models"]
    assert overridden["soul"] == default["soul"]


@pytest.mark.parametrize("lane_name", LANE_NAMES)
def test_lane_soul_material_contains_single_ownership_guidance(lane_name):
    from tools.delegation_ownership import (
        CODEBASE_OWNERSHIP_GUIDANCE,
        CODEBASE_OWNERSHIP_SENTENCE,
    )

    material = lane_tool._soul_material(resolve_lane_spec(lane_name, {}))

    assert material.count(CODEBASE_OWNERSHIP_SENTENCE) == 1
    assert material.count(CODEBASE_OWNERSHIP_GUIDANCE) == 1
    if lane_name == "reverse_engineer":
        assert "Do not modify the target artifacts." in material
        assert "never to analyzed target artifacts" in material


def test_quota_error_advances_chain_and_receipt_names_winner(monkeypatch, workdir):
    _configure(monkeypatch, {"ui_dev": {"models": ["provider/limited", "provider/winner"]}})
    calls = []

    def _attempt(spec, task, directory, model, timeout, context):
        calls.append(model)
        if model.endswith("limited"):
            return {"success": False, "errorStatus": 429, "error": "quota exhausted"}
        return {"success": True, "final_report": "UI updated\nChecks passed"}

    monkeypatch.setitem(lane_tool._RUNNERS, "omp", _attempt)
    result = json.loads(registry.dispatch(
        "delegate_ui_dev", {"task": "Align the panel", "workdir": str(workdir)}
    ))
    assert calls == ["provider/limited", "provider/winner"]
    assert result["success"] is True
    assert result["model"] == "provider/winner"
    assert "model: provider/winner" in result["receipt"]
    assert result["final_report"].endswith(result["receipt"])
    assert [attempt["success"] for attempt in result["attempts"]] == [False, True]


def test_non_quota_error_does_not_advance_chain(monkeypatch, workdir):
    _configure(monkeypatch, {"backend_dev": {"models": ["first", "second"]}})
    calls = []

    def _failure(*args):
        calls.append(args[3])
        return {"success": False, "error": "migration contract test failed"}

    monkeypatch.setitem(lane_tool._RUNNERS, "omp", _failure)
    result = json.loads(registry.dispatch(
        "delegate_backend_dev", {"task": "Change the API", "workdir": str(workdir)}
    ))
    assert calls == ["first"]
    assert result["success"] is False
    assert result["final_report"].endswith(result["receipt"])


def test_omp_argv_has_required_flags_model_soul_and_tools(tmp_path):
    session_dir = tmp_path / "sessions"
    transcript = session_dir / "session.jsonl"
    argv = omp.build_omp_argv(
        "provider/model", "Inspect the layout", binary="omp",
        soul=Path("soul.md"), tools_allowlist=["read", "grep"],
        session_dir=session_dir,
    )
    assert "--no-session" not in argv
    assert argv[1:6] == ["-p", "--mode", "json", "--auto-approve", "--model"]
    assert argv[6] == "provider/model"
    assert argv[argv.index("--session-dir") + 1] == str(session_dir)
    assert argv[argv.index("--append-system-prompt") + 1] == "soul.md"
    assert argv[argv.index("--tools") + 1] == "read,grep"
    assert argv[-1] == "Inspect the layout"
    assert "test-key" not in " ".join(argv)

    restored = omp.build_omp_argv(
        "provider/model", "Inspect the layout", binary="omp",
        soul=Path("soul.md"), tools_allowlist=["read", "grep"],
        session_dir=session_dir, restore=transcript,
    )
    assert "--no-session" not in restored
    assert "--restore" in restored
    assert restored[restored.index("--restore") + 1] == str(transcript)
    assert "Inspect the layout" not in restored
    assert restored[restored.index("--append-system-prompt") + 1] == "soul.md"
    assert restored[restored.index("--tools") + 1] == "read,grep"
    assert "test-key" not in " ".join(restored)


def test_omp_run_loads_secret_and_stages_only_declared_mcp(monkeypatch, tmp_path, workdir):
    secrets = tmp_path / "omp.env"
    secrets.write_text("LITELLM_API_KEY=test-key\n", encoding="utf-8")
    soul = tmp_path / "soul.md"
    soul.write_text("Preserve spacing.", encoding="utf-8")
    mcp_source = tmp_path / "analysis.json"
    mcp_source.write_text('{"servers": {}}', encoding="utf-8")
    plain_workdir = tmp_path / "plain-workspace"
    plain_workdir.mkdir()
    invocations = []

    def _completed(argv, **kwargs):
        invocations.append((argv, kwargs))
        event = {"type": "turn_end", "message": {"content": [{"text": "analysis complete"}]}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(event) + "\n", stderr="")

    monkeypatch.setattr(omp.subprocess, "run", _completed)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)
    config = {"binary": "omp", "secrets_file": str(secrets)}
    re_spec = resolve_lane_spec("reverse_engineer", {"reverse_engineer": {
        "soul": str(soul), "mcp": str(mcp_source), "tools_allowlist": ["read"],
    }})
    ui_spec = resolve_lane_spec("ui_dev", {"ui_dev": {"soul": "", "mcp": None}})
    with_mcp = omp.run_omp_lane(
        re_spec, "Analyze", str(workdir), "provider/model", omp_config=config,
    )
    without_mcp = omp.run_omp_lane(
        ui_spec, "Analyze", str(plain_workdir), "provider/model", omp_config=config,
    )
    assert with_mcp["success"] is True and without_mcp["success"] is True
    assert (workdir / ".omp" / "mcp.json").read_text(encoding="utf-8") == mcp_source.read_text(
        encoding="utf-8"
    )
    assert not (plain_workdir / ".omp" / "mcp.json").exists()
    argv, kwargs = invocations[0]
    assert "--append-system-prompt" in argv
    assert kwargs["cwd"] == str(workdir)
    assert kwargs["env"]["LITELLM_API_KEY"] == "test-key"
    assert all("test-key" not in str(argument) for argument in argv)


def test_omp_jsonl_extracts_turn_end_text_and_quota_status():
    lines = [
        {"type": "turn_end", "message": {"content": [{"text": "first"}, {"text": " report"}]}},
        {"type": "error", "errorStatus": 403, "error": {"message": "quota exhausted"}},
    ]
    parsed = omp.parse_omp_events("\n".join(json.dumps(line) for line in lines))
    assert parsed["final_report"] == "first report"
    assert parsed["errorStatus"] == 403
    assert parsed["quota"] is True


def test_conditional_registration_uses_real_profile_config():
    names = set(LANE_TOOL_NAMES)
    assert registry.get_definitions(names, quiet=True) == []
    blocked = json.loads(registry.dispatch("delegate_ui_dev", {}))
    assert "delegation.lanes is empty" in blocked["error"]
    config = get_hermes_home() / "config.yaml"
    config.write_text("delegation:\n  lanes:\n    ui_dev: {}\n", encoding="utf-8")
    definitions = registry.get_definitions(names, quiet=True)
    assert {item["function"]["name"] for item in definitions} == names
    assert all(item["function"]["parameters"]["required"] == ["task", "workdir"] for item in definitions)
    from model_tools import get_tool_definitions
    exposed = get_tool_definitions(
        enabled_toolsets=["delegation"], quiet_mode=True,
    )
    assert names <= {item["function"]["name"] for item in exposed}


@pytest.mark.parametrize("lane_name,harness", [("client_dev", "claude"), ("backend_dev", "cursor")])
def test_existing_harnesses_use_shared_dispatch_and_inject_prompt_material(
    monkeypatch, tmp_path, workdir, lane_name, harness
):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Complete skill instructions", encoding="utf-8")
    tools = ["Read", "Bash"] if harness == "claude" else []
    _configure(monkeypatch, {lane_name: {
        "harness": harness, "models": ["provider/model"], "soul": "Lane instructions",
        "skills": [str(skill)], "tools_allowlist": tools,
    }})
    captured = {}

    def _dispatch(args, **kwargs):
        captured.update(args)
        # The lane deletes the claude soul temp file once the run returns, so
        # its content must be captured here, at dispatch time.
        if args.get("system_prompt_file"):
            captured["_soul_text"] = Path(args["system_prompt_file"]).read_text(encoding="utf-8")
        return json.dumps({"success": True, "final_report": "done"})

    monkeypatch.setattr(generic_delegate, "_delegate_agent_handler", _dispatch)
    result = json.loads(registry.dispatch(
        f"delegate_{lane_name}", {"task": "Implement", "workdir": str(workdir)}
    ))
    assert result["success"] is True
    assert captured["backend"] == harness
    assert captured["model"] == "provider/model"
    assert captured["background"] is False
    assert captured.get("allowed_tools") == (",".join(tools) if tools else None)
    if harness == "claude":
        # Soul material is pinned at the harness level via a system prompt
        # file (deleted after the run), not concatenated into the goal.
        soul_path = captured.get("system_prompt_file")
        assert soul_path
        assert "Lane instructions" in captured["_soul_text"]
        assert "Complete skill instructions" in captured["_soul_text"]
        assert "# Task" in captured["goal"]
        assert "Lane instructions" not in captured["goal"]
        assert not Path(soul_path).exists()
    else:
        # Cursor cannot take an appended system prompt; soul stays in the goal.
        assert "Lane instructions" in captured["goal"]
        assert "Complete skill instructions" in captured["goal"]


def test_subagent_lane_uses_native_dispatch_and_routing_config(monkeypatch, workdir):
    _configure(
        monkeypatch,
        {"reverse_engineer": {"harness": "subagent", "models": ["provider/model"]}},
        provider="openrouter",
    )
    captured = {}
    parent = object()

    def _subagent(**kwargs):
        captured.update(kwargs)
        return json.dumps({"results": [{"status": "completed", "summary": "evidence"}]})

    monkeypatch.setattr(generic_delegate, "delegate_agent", _subagent)
    result = json.loads(registry.dispatch(
        "delegate_reverse_engineer", {"task": "Inspect statically", "workdir": str(workdir)},
        parent_agent=parent,
    ))
    assert result["success"] is True
    assert captured["parent_agent"] is parent
    assert captured["background"] is False
    assert captured["credentials_cfg"]["provider"] == "openrouter"
    assert captured["credentials_cfg"]["model"] == "provider/model"
    assert str(workdir) in captured["context"]


def test_existing_delegate_schema_and_behavior_are_untouched(monkeypatch):
    schema_before = copy.deepcopy(generic_delegate.DELEGATE_TASK_SCHEMA)
    entry = registry.get_entry("delegate_agent")
    called = {}

    def _fake_delegate(**kwargs):
        called.update(kwargs)
        return json.dumps({"results": []})

    monkeypatch.setattr(generic_delegate, "delegate_agent", _fake_delegate)
    result = json.loads(registry.dispatch(
        "delegate_agent", {"tasks": [{"goal": "keep generic behavior"}], "background": False},
        parent_agent=object(),
    ))
    assert result == {"results": []}
    assert called["tasks"] == [{"goal": "keep generic behavior"}]
    assert called["background"] is False
    assert registry.get_entry("delegate_agent") is entry
    assert generic_delegate.DELEGATE_TASK_SCHEMA == schema_before


def test_claude_lane_pins_soul_in_system_prompt_file(monkeypatch, tmp_path, workdir):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("Skill body text", encoding="utf-8")
    _configure(monkeypatch, {"client_dev": {
        "harness": "claude", "models": ["provider/model"], "soul": "Soul rules here",
        "skills": [str(skill_dir)],
    }})
    captured = {}

    def _dispatch(args, **kwargs):
        captured.update(args)
        # The lane deletes the soul temp file after the run, so capture its
        # content now, at dispatch time.
        if args.get("system_prompt_file"):
            captured["_soul_text"] = Path(args["system_prompt_file"]).read_text(encoding="utf-8")
        return json.dumps({"success": True, "final_report": "ok"})

    monkeypatch.setattr(generic_delegate, "_delegate_agent_handler", _dispatch)
    result = json.loads(registry.dispatch(
        "delegate_client_dev", {"task": "Implement", "workdir": str(workdir)}
    ))
    assert result["success"] is True
    soul_path = captured.get("system_prompt_file")
    assert soul_path
    assert Path(soul_path).suffix == ".md"
    assert Path(soul_path).name.startswith("hermes-claude-soul-")
    assert "Soul rules here" in captured["_soul_text"]
    assert "Skill body text" in captured["_soul_text"]
    assert "# Lane instructions" in captured["_soul_text"]
    assert "# Injected skill:" in captured["_soul_text"]
    assert "# Task" in captured["goal"]
    assert "Soul rules here" not in captured["goal"]
    assert not Path(soul_path).exists()


def test_claude_lane_without_soul_still_sends_ownership_prompt_file(monkeypatch, workdir):
    from tools.delegation_ownership import (
        CODEBASE_OWNERSHIP_GUIDANCE,
        CODEBASE_OWNERSHIP_SENTENCE,
    )

    # An empty custom soul still receives the shared ownership guidance.
    _configure(monkeypatch, {"client_dev": {
        "harness": "claude", "models": ["provider/model"], "soul": "",
    }})
    captured = {}

    def _dispatch(args, **kwargs):
        captured.update(args)
        if args.get("system_prompt_file"):
            captured["_soul_text"] = Path(args["system_prompt_file"]).read_text(encoding="utf-8")
        return json.dumps({"success": True, "final_report": "ok"})

    monkeypatch.setattr(generic_delegate, "_delegate_agent_handler", _dispatch)
    result = json.loads(registry.dispatch(
        "delegate_client_dev", {"task": "Implement", "workdir": str(workdir)}
    ))
    assert result["success"] is True
    soul_path = captured.get("system_prompt_file")
    assert soul_path
    assert captured["_soul_text"].count(CODEBASE_OWNERSHIP_SENTENCE) == 1
    assert captured["_soul_text"].count(CODEBASE_OWNERSHIP_GUIDANCE) == 1
    assert not Path(soul_path).exists()
    assert "# Task" in captured["goal"]


def test_subagent_lane_prepends_soul_to_child_system_prompt(monkeypatch, workdir):
    _configure(
        monkeypatch,
        {"reverse_engineer": {
            "harness": "subagent", "models": ["provider/model"], "soul": "RE soul rules",
        }},
        provider="openrouter",
    )
    parent = MagicMock()
    parent.base_url = ""
    parent.api_key = "x"
    parent.provider = None
    parent.api_mode = None
    parent.model = None
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._oneshot_children_spawned = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = ["terminal"]

    monkeypatch.setattr(
        generic_delegate, "_resolve_delegation_credentials",
        lambda *a, **k: {
            "provider": None, "base_url": None, "api_key": None,
            "api_mode": None, "model": None,
        },
    )
    monkeypatch.setattr(generic_delegate, "_load_config", lambda: {"max_spawn_depth": 2})

    with patch("run_agent.AIAgent") as MockAgent:
        mock_child = MagicMock()
        mock_child.run_conversation.return_value = {
            "final_response": "done", "completed": True,
            "api_calls": 1, "messages": [],
        }
        mock_child._delegate_saved_tool_names = []
        mock_child._credential_pool = None
        mock_child.session_prompt_tokens = 0
        mock_child.session_completion_tokens = 0
        mock_child.model = "test"
        MockAgent.return_value = mock_child

        result = json.loads(registry.dispatch(
            "delegate_reverse_engineer", {"task": "Inspect statically", "workdir": str(workdir)},
            parent_agent=parent,
        ))

    assert result["success"] is True
    child_prompt = MockAgent.call_args[1]["ephemeral_system_prompt"]
    assert "RE soul rules" in child_prompt
    assert "# Lane instructions" in child_prompt
    assert "You are a focused subagent" in child_prompt
    assert child_prompt.index("RE soul rules") < child_prompt.index("You are a focused subagent")


def test_claude_argv_includes_append_system_prompt_file(monkeypatch, tmp_path, workdir):
    from tests.tools.test_claude_agent_tool import _write_fake_binary

    import tools.claude_agent_tool as claude_tool
    from tools.delegation_ownership import (
        CODEBASE_OWNERSHIP_GUIDANCE,
        CODEBASE_OWNERSHIP_SENTENCE,
    )

    soul_path = tmp_path / "soul.md"
    soul_path.write_text(f"{CODEBASE_OWNERSHIP_GUIDANCE}\n\nHarness soul", encoding="utf-8")
    fake_binary = _write_fake_binary(tmp_path, name="claude-fake")
    argv_out = tmp_path / "argv.json"
    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary",
        lambda model=None: str(fake_binary),
    )
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))

    result = json.loads(claude_tool.delegate_claude_agent(
        task="do it", workdir=str(workdir), system_prompt_file=str(soul_path),
    ))
    assert result["success"] is True
    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(soul_path)
    assert "--append-system-prompt" not in argv
    assert soul_path.read_text(encoding="utf-8").count(CODEBASE_OWNERSHIP_SENTENCE) == 1
    later_flag = "--model" if "--model" in argv else "--permission-mode"
    assert argv.index("--append-system-prompt-file") < argv.index(later_flag)
    assert argv[-1] == "do it"


# --- Reviewer pass -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({"provider": "custom:litellm", "model": "grok-4.6"},
     {"provider": "custom:litellm", "model": "grok-4.6"}),
    ({"provider": "custom:litellm", "model": "grok-4.6", "reasoning_effort": "medium"},
     {"provider": "custom:litellm", "model": "grok-4.6", "reasoning_effort": "medium"}),
    ("custom:litellm:grok-4.6", {"provider": "custom:litellm", "model": "grok-4.6"}),
    ("openrouter:grok-4.6", {"provider": "openrouter", "model": "grok-4.6"}),
    ("grok-4.6", {"provider": "", "model": "grok-4.6"}),
    ("grok-4.6-medium (moa)",
     {"provider": "", "model": "grok-4.6", "reasoning_effort": "medium"}),
    ("gpt-5.2-low", {"provider": "", "model": "gpt-5.2", "reasoning_effort": "low"}),
    ("requesting agent", None),
    ("the requesting agent will review", None),
    ("", None),
    (None, None),
])
def test_parse_reviewer_grammar(raw, expected):
    from tools.delegate_lane_review import _parse_reviewer

    assert _parse_reviewer(raw) == expected


def _review_lane(monkeypatch, workdir, reviewer, reply=None, error=None):
    """Drive one ui_dev lane attempt with a stubbed reviewer LLM call."""
    import agent.auxiliary_client as aux
    import agent.moa_loop as moa
    from tools import delegate_lane_review

    _configure(monkeypatch, {"ui_dev": {"models": ["provider/model"], "reviewer": reviewer}})
    calls = []

    monkeypatch.setitem(lane_tool._RUNNERS, "omp", lambda *args: (
        {"success": True, "final_report": "done: checks pass"}
    ))
    monkeypatch.setattr(moa, "_slot_runtime",
                        lambda slot: {"provider": slot["provider"], "model": slot["model"]})
    monkeypatch.setattr(moa, "_slot_reasoning_config", lambda slot: None)
    monkeypatch.setattr(moa, "_extract_text", lambda response: response)

    def _call_llm(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return reply

    monkeypatch.setattr(aux, "call_llm", _call_llm)
    result = json.loads(registry.dispatch(
        "delegate_ui_dev", {"task": "Align the panel", "workdir": str(workdir)}
    ))
    return result, calls, delegate_lane_review


def test_successful_attempt_with_approved_review(monkeypatch, workdir):
    result, calls, _ = _review_lane(
        monkeypatch, workdir,
        {"provider": "custom:litellm", "model": "grok-4.6"}, reply="APPROVED",
    )
    assert result["success"] is True
    assert result["review"]["status"] == "approved"
    assert "(review: approved)" in result["receipt"]
    assert len(calls) == 1
    assert calls[0]["task"] == "lane_review"
    assert calls[0]["model"] == "grok-4.6"
    assert "Agent report:\ndone: checks pass" in calls[0]["messages"][0]["content"]


def test_blocked_review_fails_the_lane(monkeypatch, workdir):
    result, calls, _ = _review_lane(
        monkeypatch, workdir,
        {"provider": "custom:litellm", "model": "grok-4.6"},
        reply="BLOCKED: no evidence the layout was verified",
    )
    assert result["success"] is False
    assert "reviewer blocked" in result["error"]
    assert "no evidence" in result["error"]
    assert result["review_blocked"] is True
    assert result["review"]["status"] == "blocked"
    assert "(review: blocked)" in result["receipt"]
    assert "reviewer blocked" in result["verdict"]
    assert result["receipt_data"]["review_detail"] == "BLOCKED: no evidence the layout was verified"


def test_prose_reviewer_is_skipped_without_an_llm_call(monkeypatch, workdir):
    result, calls, _ = _review_lane(monkeypatch, workdir, "requesting agent")
    assert result["success"] is True
    assert result["review"]["status"] == "skipped"
    assert calls == []


def test_reviewer_transport_error_never_fails_the_lane(monkeypatch, workdir):
    result, calls, _ = _review_lane(
        monkeypatch, workdir,
        {"provider": "custom:litellm", "model": "grok-4.6"},
        error=RuntimeError("provider unreachable"),
    )
    assert result["success"] is True
    assert result["review"]["status"] == "failed"
    assert "provider unreachable" in result["review"]["detail"]
    assert "(review: failed)" in result["receipt"]
    assert "provider unreachable" in result["receipt_data"]["review_detail"]


def test_failed_attempt_is_not_reviewed(monkeypatch, workdir):
    from tools import delegate_lane_review

    _configure(monkeypatch, {"ui_dev": {"models": ["provider/model"]}})
    monkeypatch.setitem(
        lane_tool._RUNNERS, "omp",
        lambda *args: {"success": False, "error": "migration contract test failed"},
    )
    invoked = []
    monkeypatch.setattr(
        delegate_lane_review, "apply_review",
        lambda *args, **kwargs: invoked.append(args) or args[2],
    )
    result = json.loads(registry.dispatch(
        "delegate_ui_dev", {"task": "Change the API", "workdir": str(workdir)}
    ))
    assert result["success"] is False
    assert invoked == []
    assert "review" not in result["backend_result"]


# Routing hint: delegate_agent's schema points lane-shaped work at the lane
# tools, but only when a lane tool is actually present in the session.

_LANE_ROUTING_SENTENCE = (
    " For UI, client-logic, backend, or reverse-engineering work, prefer the dedicated "
    "delegate_ui_dev / delegate_client_dev / delegate_backend_dev / delegate_reverse_engineer "
    "lane tools; use delegate_agent only for parallel fan-out of non-lane work or tasks that "
    "match no lane."
)


def _agent_def(description="Delegate a task."):
    return {"type": "function", "function": {"name": "delegate_agent", "description": description}}


def test_delegate_agent_routing_hint_with_lanes_present():
    from model_tools import _apply_dynamic_schemas

    defs = [_agent_def()] + [
        {"type": "function", "function": {"name": n, "description": "lane."}}
        for n in LANE_TOOL_NAMES
    ]
    out = _apply_dynamic_schemas(defs)
    agent = next(d for d in out if d["function"]["name"] == "delegate_agent")
    assert agent["function"]["description"].endswith(_LANE_ROUTING_SENTENCE)


def test_delegate_agent_routing_hint_silent_without_lanes():
    from model_tools import _apply_dynamic_schemas

    out = _apply_dynamic_schemas([_agent_def()])
    assert out[0]["function"]["description"] == "Delegate a task."


def test_lane_tool_descriptions_carry_routing_clause():
    for lane_name in LANE_NAMES:
        description = lane_tool._DESCRIPTIONS[lane_name]
        assert "Preferred over delegate_agent(backend=...) for this work class." in description
        assert "LiteLLM gateway" in description
        assert "reviewer verdict" in description
    # And the registered schemas render those descriptions.
    assert "Preferred over delegate_agent(backend=...)" in lane_tool._schema("ui_dev")["description"]


def test_tool_definitions_route_delegation_to_lanes():
    from model_tools import get_tool_definitions

    config = get_hermes_home() / "config.yaml"
    config.write_text("delegation:\n  lanes:\n    ui_dev: {}\n", encoding="utf-8")
    exposed = get_tool_definitions(
        enabled_toolsets=["delegation"], quiet_mode=True,
    )
    agent = next(d for d in exposed if d["function"]["name"] == "delegate_agent")
    assert "delegate_ui_dev" in agent["function"]["description"]


# --- Progress description (user-facing phrase; never part of the task) -------


@pytest.mark.parametrize("lane_name", LANE_NAMES)
def test_lane_schema_has_optional_progress_description(lane_name):
    properties = lane_tool._schema(lane_name)["parameters"]["properties"]
    assert "description" in properties
    assert properties["description"]["type"] == "string"
    assert "description" not in lane_tool._schema(lane_name)["parameters"]["required"]
    assert "Shown to the user as progress" in properties["description"]["description"]


def test_handler_ignores_progress_description_in_prompt(monkeypatch, workdir):
    _configure(monkeypatch, {"backend_dev": {"models": ["provider/m"]}})
    captured = {}

    def _attempt(spec, task, directory, model, timeout, context):
        captured["prompt"] = task
        return {"success": True, "final_report": "done"}

    monkeypatch.setitem(lane_tool._RUNNERS, "omp", _attempt)
    result = json.loads(registry.dispatch(
        "delegate_backend_dev",
        {
            "task": "Change the API",
            "workdir": str(workdir),
            "description": "implementing the retry gate",
        },
    ))
    assert result["success"] is True
    assert "Change the API" in captured["prompt"]
    assert "retry gate" not in captured["prompt"]
    assert "description" not in captured["prompt"]


# --- OMP session resume (same session, turns already on disk) -----------------


_MAX_OMP_SPAWNS = 3


@pytest.mark.parametrize(
    "error,returncode,session_exists,expected",
    [
        ("Connection closed", 1, False, True),
        ("Timeout on reading data from socket", 1, False, True),
        ("", 0, True, True),
        (None, 0, True, True),
        ("OMP returned no final assistant text", 0, True, True),
        (None, None, True, True),
        ("task failed", None, True, True),
        ("429 all deployments are in cooldown", 1, True, False),
        ("in cooldown", 1, True, False),
        ("connection refused", 1, True, False),
        ("401 invalid api key", 1, True, False),
        ("invalid api key", 1, True, False),
        ("", 0, False, False),
        (None, None, False, False),
        ("task failed", 1, True, False),
    ],
)
def test_recoverable_lane_failure_classifies_interruptions_not_quota(
    error, returncode, session_exists, expected
):
    assert omp.recoverable_lane_failure(error, returncode, session_exists) is expected


def _lane_secrets(tmp_path):
    secrets = tmp_path / "omp.env"
    secrets.write_text("LITELLM_API_KEY=test-key\n", encoding="utf-8")
    soul = tmp_path / "soul.md"
    soul.write_text("Preserve spacing.\n", encoding="utf-8")
    spec = resolve_lane_spec("ui_dev", {"ui_dev": {
        "soul": str(soul), "mcp": None, "tools_allowlist": ["read", "grep"],
    }})
    return spec, {"binary": "omp", "secrets_file": str(secrets)}


def _plant_omp_log(pid, body, *, date):
    directory = omp.omp_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"omp.{date}.{pid}.log"
    path.write_text(body if str(body).endswith("\n") else str(body) + "\n", encoding="utf-8")
    return path


def _turn_end(text):
    return json.dumps({
        "type": "turn_end", "message": {"content": [{"text": text}]},
    }) + "\n"


def _completed(argv, code, stdout="", stderr="", pid=None):
    proc = subprocess.CompletedProcess(argv, 0 if code is None else code, stdout=stdout, stderr=stderr)
    proc.returncode = code
    if pid is not None:
        proc.pid = pid
    return proc


def test_omp_lane_writes_live_session_before_spawn_on_success_and_failure(monkeypatch, tmp_path, workdir):
    spec, config = _lane_secrets(tmp_path)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)
    seen = {}

    def _run(argv, **kwargs):
        live = json.loads((workdir / ".hermes-lane" / "live.json").read_text(encoding="utf-8"))
        seen["live"] = live
        seen["argv"] = list(argv)
        assert Path(live["session_dir"]).is_absolute()
        assert live["session_dir"] == str((workdir / ".hermes-lane" / "provider" / "model").resolve())
        return _completed(argv, 0, stdout=_turn_end("layout aligned"))

    monkeypatch.setattr(omp.subprocess, "run", _run)
    success = omp.run_omp_lane(spec, "Align the panel", str(workdir), "provider/model", omp_config=config)
    assert success["success"] is True
    assert success["session_dir"] == seen["live"]["session_dir"]
    assert seen["live"]["model"] == "provider/model"
    assert "--no-session" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("--session-dir") + 1] == success["session_dir"]

    def _fail(argv, **kwargs):
        return _completed(argv, 1, stderr="connection refused")

    monkeypatch.setattr(omp.subprocess, "run", _fail)
    failure = omp.run_omp_lane(spec, "Align the panel", str(workdir), "provider/model", omp_config=config)
    assert failure["success"] is False
    assert failure["session_dir"] == success["session_dir"]
    assert failure["quota"] is False


def test_omp_lane_restores_session_when_log_reports_connection_closed(monkeypatch, tmp_path, workdir):
    spec, config = _lane_secrets(tmp_path)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)
    pid = 424242101
    decoy_pid = 424242102
    planted = []
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        live = json.loads((workdir / ".hermes-lane" / "live.json").read_text(encoding="utf-8"))
        session_dir = Path(live["session_dir"])
        (session_dir / "session.jsonl").write_text('{"type":"turn"}\n', encoding="utf-8")
        if len(calls) == 1:
            planted.append(_plant_omp_log(
                pid, '{"pid": %s, "errorMessage": "Connection closed"}\n' % pid, date="1999-03-07",
            ))
            planted.append(_plant_omp_log(
                decoy_pid, '{"pid": %s, "errorMessage": "429 in cooldown"}\n' % decoy_pid, date="2042-12-31",
            ))
            return _completed(argv, 0, pid=pid)
        return _completed(argv, 0, stdout=_turn_end("resumed from disk"))

    monkeypatch.setattr(omp.subprocess, "run", _run)
    try:
        result = omp.run_omp_lane(
            spec, "Align the panel", str(workdir), "provider/model", omp_config=config,
        )
    finally:
        for path in planted:
            path.unlink(missing_ok=True)
    assert result["success"] is True
    assert result["final_report"] == "resumed from disk"
    assert result["session_dir"] == str((workdir / ".hermes-lane" / "provider" / "model").resolve())
    assert len(calls) == 2
    assert "--restore" not in calls[0]
    assert calls[0][-1] == "Align the panel"
    restore = calls[1]
    assert restore[restore.index("--restore") + 1] == str(Path(result["session_dir"]) / "session.jsonl")
    assert "Align the panel" not in restore
    assert "--append-system-prompt" in restore and "--tools" in restore
    assert all("test-key" not in argument for argv in calls for argument in argv)


def test_omp_cooldown_log_does_not_restore_and_still_advances_model_chain(monkeypatch, tmp_path, workdir):
    spec, config = _lane_secrets(tmp_path)
    _configure(
        monkeypatch,
        {"ui_dev": {
            "models": ["provider/limited", "provider/winner"],
            "soul": spec["soul"], "mcp": None, "tools_allowlist": ["read", "grep"],
        }},
        omp_config=config,
    )
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)
    pid = 424242201
    decoy_pid = 424242202
    planted = []
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        model = argv[argv.index("--model") + 1]
        if not model.endswith("limited"):
            return _completed(argv, 0, stdout=_turn_end("UI updated"))
        session_dir = (workdir / ".hermes-lane" / "provider" / "limited").resolve()
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
        if len(calls) == 1:
            planted.append(_plant_omp_log(
                pid, '{"pid": %s, "errorMessage": "429 all deployments are in cooldown"}\n' % pid,
                date="2001-01-09",
            ))
            decoy = _plant_omp_log(
                decoy_pid, '{"pid": %s, "errorMessage": "Connection closed"}\n' % decoy_pid,
                date="2041-08-08",
            )
            os.utime(decoy, (decoy.stat().st_atime, decoy.stat().st_mtime + 50))
            planted.append(decoy)
        return _completed(argv, 0, pid=pid)

    monkeypatch.setattr(omp.subprocess, "run", _run)
    try:
        result = json.loads(registry.dispatch(
            "delegate_ui_dev", {"task": "Align the panel", "workdir": str(workdir)},
        ))
    finally:
        for path in planted:
            path.unlink(missing_ok=True)
    limited = [argv for argv in calls if "provider/limited" in argv]
    assert len(limited) == 1
    assert "--restore" not in limited[0]
    assert result["success"] is True
    assert result["model"] == "provider/winner"
    assert result["attempts"][0]["quota"] is True


def test_omp_lane_caps_restores_at_two(monkeypatch, tmp_path, workdir):
    spec, config = _lane_secrets(tmp_path)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        live = json.loads((workdir / ".hermes-lane" / "live.json").read_text(encoding="utf-8"))
        Path(live["session_dir"]).joinpath("session.jsonl").write_text("{}\n", encoding="utf-8")
        if len(calls) > _MAX_OMP_SPAWNS:
            return _completed(argv, 1, stderr="connection refused")
        return _completed(argv, None, stderr="Connection closed")

    monkeypatch.setattr(omp.subprocess, "run", _run)
    result = omp.run_omp_lane(spec, "Align the panel", str(workdir), "provider/model", omp_config=config)
    assert len(calls) == _MAX_OMP_SPAWNS
    assert result["success"] is False
    assert "Connection closed" in result["error"]
    assert "--restore" not in calls[0]
    assert calls[0][-1] == "Align the panel"
    for argv in calls[1:]:
        assert "--restore" in argv
        assert "Align the panel" not in argv
    assert result["session_dir"] == str((workdir / ".hermes-lane" / "provider" / "model").resolve())


@pytest.mark.parametrize("stderr,code", [
    ("connection refused", 1),
    ("401 invalid api key", 1),
])
def test_omp_lane_does_not_restore_terminal_failures(monkeypatch, tmp_path, workdir, stderr, code):
    spec, config = _lane_secrets(tmp_path)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        live = json.loads((workdir / ".hermes-lane" / "live.json").read_text(encoding="utf-8"))
        Path(live["session_dir"]).joinpath("session.jsonl").write_text("{}\n", encoding="utf-8")
        return _completed(argv, code, stderr=stderr)

    monkeypatch.setattr(omp.subprocess, "run", _run)
    result = omp.run_omp_lane(spec, "Align the panel", str(workdir), "provider/model", omp_config=config)
    assert len(calls) == 1
    assert result["success"] is False
    assert result["quota"] is False
    assert "--restore" not in calls[0]


def test_omp_lane_does_not_restore_empty_result_without_a_session(monkeypatch, tmp_path, workdir):
    spec, config = _lane_secrets(tmp_path)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        return _completed(argv, 0)

    monkeypatch.setattr(omp.subprocess, "run", _run)
    result = omp.run_omp_lane(spec, "Align the panel", str(workdir), "provider/model", omp_config=config)
    assert len(calls) == 1
    assert result["success"] is False
    assert result["quota"] is False
    assert "session_dir" in result
