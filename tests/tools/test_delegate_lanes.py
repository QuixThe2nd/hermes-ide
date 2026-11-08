"""Behavior contracts for opt-in delegation lanes and the OMP backend."""

import copy
import json
import subprocess
from pathlib import Path

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


def test_omp_argv_has_required_flags_model_soul_and_tools():
    argv = omp.build_omp_argv(
        "provider/model", "Inspect the layout", binary="omp",
        soul=Path("soul.md"), tools_allowlist=["read", "grep"],
    )
    assert argv[1:8] == [
        "-p", "--mode", "json", "--no-session", "--auto-approve", "--model", "provider/model",
    ]
    assert argv[8:10] == ["--append-system-prompt", "soul.md"]
    assert argv[10:12] == ["--tools", "read,grep"]
    assert argv[-1] == "Inspect the layout"


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
        enabled_toolsets=["delegation"], quiet_mode=True, skip_tool_search_assembly=True,
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
        return json.dumps({"success": True, "final_report": "done"})

    monkeypatch.setattr(generic_delegate, "_delegate_agent_handler", _dispatch)
    result = json.loads(registry.dispatch(
        f"delegate_{lane_name}", {"task": "Implement", "workdir": str(workdir)}
    ))
    assert result["success"] is True
    assert captured["backend"] == harness
    assert captured["model"] == "provider/model"
    assert captured["background"] is False
    assert "Lane instructions" in captured["goal"]
    assert "Complete skill instructions" in captured["goal"]
    assert captured.get("allowed_tools") == (",".join(tools) if tools else None)


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
