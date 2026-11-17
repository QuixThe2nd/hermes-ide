"""Startup contracts for automatic fork conventions in coding sub-harnesses."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import delegate_lane_specs as specs
from tools import delegate_lane_tool as lane_tool
from tools import delegate_tool as generic_delegate
from tools import delegate_tool_omp as omp
from tools import delegation_ownership
from tools.registry import registry
from tests.tools.test_claude_agent_tool import _write_fake_binary
from tests.tools.test_delegate_lanes import _write_fake_omp


CODEBASE_OWNERSHIP_GUIDANCE = delegation_ownership.CODEBASE_OWNERSHIP_GUIDANCE
CODEBASE_OWNERSHIP_SENTENCE = delegation_ownership.CODEBASE_OWNERSHIP_SENTENCE
ensure_codebase_ownership = delegation_ownership.ensure_codebase_ownership

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOUL_PATH = _PROJECT_ROOT / "skills/dev-lanes/SOUL.md"
# Derive the expectation from the document, not the production import: pristine
# baselines can then reach behavioral assertions instead of failing collection.
FORK_CODING_CONVENTIONS = _SOUL_PATH.read_text(encoding="utf-8").strip()

_COMPATIBILITY_SKILL = "skills/dev-lanes/fork-coding-conventions"
_SKILL_MODES = [
    pytest.param([], id="skills-empty"),
    pytest.param([_COMPATIBILITY_SKILL], id="compatibility-skill"),
]
_REQUIRED_CONVENTION_MARKERS = (
    "# Fork coding conventions (lane edition)",
    "owns merging and PR mechanics; you own the code and its tests.",
    "## Ownership",
    "## Commits",
    "Never amend or force-push a branch another agent may have fetched.",
    "## Tests",
    "Tests BEFORE (reproduce/fail on current code) + AFTER",
    "scripts/run_tests.sh tests/<area>/ -q",
    "No change-detector tests",
    "## Diff discipline",
    "Respect facade + siblings layout",
    "## Reporting",
    "State file:line when citing code.",
    "never to analyzed target artifacts.",
)
_TASK_CONTEXT = "Preserve this additional context."
_TASK = "Implement the scoped change."
_TASK_PROMPT = f"# Additional context\n{_TASK_CONTEXT}\n\n# Task\n{_TASK}"
_LANE_SOUL = "Lane soul sentinel."
_POINTER_TEXT = "loading this compatibility skill is not required."


@pytest.fixture(autouse=True)
def _isolate_process_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _assert_conventions_once(*parts: str) -> None:
    combined = "\n".join(parts)
    assert combined.count(FORK_CODING_CONVENTIONS) == 1
    assert combined.count(CODEBASE_OWNERSHIP_SENTENCE) == 1
    for marker in _REQUIRED_CONVENTION_MARKERS:
        assert combined.count(marker) == 1, marker


def _assert_skill_mode(text: str, skills: list[str]) -> None:
    assert _LANE_SOUL in text
    # Empty skills and the compatibility skill both use canonical injection.
    assert _POINTER_TEXT not in text


def _configure_lane(
    monkeypatch, lane_name: str, harness: str, skills: list[str], *, goal: bool = False
) -> None:
    config = {
        "lanes": {
            lane_name: {
                "harness": harness,
                "models": ["provider/model"],
                "soul": _LANE_SOUL,
                "skills": skills,
                "goal": goal,
            }
        },
        "omp": {},
    }
    monkeypatch.setattr(specs, "_cfg", lambda: config)
    monkeypatch.setattr(lane_tool, "_cfg", lambda: config)


def _dispatch_lane(lane_name: str, workdir: Path, **context):
    result = registry.dispatch(
        f"delegate_{lane_name}",
        {"task": _TASK, "context": _TASK_CONTEXT, "workdir": str(workdir)},
        **context,
    )
    return json.loads(result) if isinstance(result, str) else result


def test_single_soul_source_contains_complete_conventions():
    _assert_conventions_once(FORK_CODING_CONVENTIONS)
    assert delegation_ownership.FORK_CODING_CONVENTIONS == FORK_CODING_CONVENTIONS
    ownership_start = FORK_CODING_CONVENTIONS.index("## Ownership")
    commits_start = FORK_CODING_CONVENTIONS.index("## Commits")
    guidance_start = FORK_CODING_CONVENTIONS.index(CODEBASE_OWNERSHIP_GUIDANCE)
    assert ownership_start < guidance_start < commits_start
    assert CODEBASE_OWNERSHIP_GUIDANCE.startswith(CODEBASE_OWNERSHIP_SENTENCE)
    assert not CODEBASE_OWNERSHIP_GUIDANCE.startswith("# Fork coding conventions")


def test_legacy_ownership_material_expands_to_full_conventions_once():
    material = ensure_codebase_ownership(
        f"{CODEBASE_OWNERSHIP_GUIDANCE}\n\nExisting lane context."
    )
    _assert_conventions_once(material)
    assert material.count("Existing lane context.") == 1


def test_pre_soul_single_paragraph_guidance_expands_without_leftovers():
    legacy_guidance = " ".join(CODEBASE_OWNERSHIP_GUIDANCE.split("\n\n")).replace(
        ", so the publishing agent can see what went beyond the brief", ""
    )
    material = ensure_codebase_ownership(
        f"{legacy_guidance}\n\nExisting lane context."
    )
    _assert_conventions_once(material)
    assert material.count("Existing lane context.") == 1


def test_malformed_soul_reports_the_required_ownership_section(tmp_path):
    malformed = tmp_path / "SOUL.md"
    malformed.write_text(
        "# Fork coding conventions (lane edition)\n\n## Tests\n\nRun tests.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="## Ownership") as exc_info:
        delegation_ownership._read_conventions(malformed)

    assert str(malformed) in str(exc_info.value)


def _legacy_full_conventions() -> str:
    legacy_document = FORK_CODING_CONVENTIONS.replace(
        " For static analysis, ownership applies to the working repository, "
        "never to analyzed target artifacts.",
        "",
    )
    assert legacy_document != FORK_CODING_CONVENTIONS
    return legacy_document


def test_lane_soul_dedupes_legacy_full_compatibility_skill(tmp_path):
    skill_dir = tmp_path / "fork-coding-conventions"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        _legacy_full_conventions(), encoding="utf-8"
    )

    material = lane_tool._soul_material({
        "soul": FORK_CODING_CONVENTIONS,
        "skills": [str(skill_dir)],
    })

    _assert_conventions_once(material)
    assert material.count("## Commits") == 1


def test_legacy_conventions_preserve_unheaded_trailing_instructions():
    trailing = "Keep these unheaded trailing task instructions."

    material = ensure_codebase_ownership(
        f"{_legacy_full_conventions()}\n\n{trailing}"
    )

    assert material.count(trailing) == 1


def test_sealed_package_resolves_soul_through_bundled_skills(tmp_path):
    site_tools = tmp_path / "site-packages/tools"
    site_tools.mkdir(parents=True)
    (site_tools / "__init__.py").write_text("", encoding="utf-8")
    bundled_skills = tmp_path / "package/share/hermes-agent/skills"
    (bundled_skills / "dev-lanes").mkdir(parents=True)
    env = os.environ.copy()
    env.update({
        "HERMES_BUNDLED_SKILLS": str(bundled_skills),
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(tmp_path / "site-packages"), str(_PROJECT_ROOT))),
        "TMPDIR": str(tmp_path),
    })
    setup_and_import = (
        "from pathlib import Path; import shutil, sys; "
        "root = Path(sys.argv[1]); "
        "shutil.copy2(Path(sys.argv[2]), "
        "root / 'site-packages/tools/delegation_ownership.py'); "
        "shutil.copy2(Path(sys.argv[3]), "
        "root / 'package/share/hermes-agent/skills/dev-lanes/SOUL.md'); "
        "from tools.delegation_ownership import FORK_CODING_CONVENTIONS; "
        "print(FORK_CODING_CONVENTIONS, end='')"
    )

    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            setup_and_import,
            str(tmp_path),
            delegation_ownership.__file__,
            str(_SOUL_PATH),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert imported.returncode == 0, imported.stderr
    assert imported.stdout == FORK_CODING_CONVENTIONS


@pytest.mark.parametrize("skills", _SKILL_MODES)
def test_omp_real_runner_reaches_argv_and_system_file(monkeypatch, tmp_path, skills):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    secrets = tmp_path / "omp.env"
    secrets.write_text("LITELLM_API_KEY=test-key\n", encoding="utf-8")
    _configure_lane(monkeypatch, "ui_dev", "omp", skills)
    monkeypatch.setattr(lane_tool, "_cfg", lambda: {
        "omp": {"binary": "omp", "secrets_file": str(secrets)}
    })
    captured = {}

    def _completed(argv, **kwargs):
        system_text = ""
        if "--append-system-prompt" in argv:
            system_path = Path(argv[argv.index("--append-system-prompt") + 1])
            system_text = system_path.read_text(encoding="utf-8")
        captured.update(argv=list(argv), system_text=system_text)
        event = {"type": "turn_end", "message": {"content": [{"text": "done"}]}}
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(event) + "\n", stderr=""
        )

    monkeypatch.setattr(omp.subprocess, "run", _completed)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)

    result = _dispatch_lane("ui_dev", workdir)

    assert result["success"] is True, result
    argv = captured["argv"]
    task_prompt = argv[-1]
    assert task_prompt.endswith(_TASK_PROMPT)
    assert captured["system_text"] == _LANE_SOUL
    initial_context = task_prompt + "\n" + captured["system_text"]
    _assert_conventions_once(initial_context)
    _assert_skill_mode(initial_context, skills)


@pytest.mark.parametrize("skills", _SKILL_MODES)
def test_omp_goal_runner_reaches_rpc_objective_and_system_file(
    monkeypatch, tmp_path, skills
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    secrets = tmp_path / "omp.env"
    secrets.write_text("LITELLM_API_KEY=test-key\n", encoding="utf-8")
    _configure_lane(monkeypatch, "reverse_engineer", "omp", skills, goal=True)
    fake = _write_fake_omp(tmp_path)
    monkeypatch.setattr(lane_tool, "_cfg", lambda: {
        "omp": {"binary": str(fake), "secrets_file": str(secrets)}
    })
    argv_out = tmp_path / "omp-goal-argv.json"
    objective_out = tmp_path / "omp-goal-objective.md"
    system_out = tmp_path / "omp-goal-system.md"
    monkeypatch.setenv("FAKE_OMP_ARGV_OUT", str(argv_out))
    monkeypatch.setenv("FAKE_OMP_OBJECTIVE_OUT", str(objective_out))
    monkeypatch.setenv("FAKE_OMP_SYSTEM_OUT", str(system_out))

    result = _dispatch_lane("reverse_engineer", workdir)

    assert result["success"] is True, result
    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert argv[argv.index("--mode") + 1] == "rpc"
    objective = objective_out.read_text(encoding="utf-8")
    system_text = system_out.read_text(encoding="utf-8")
    assert objective.endswith(_TASK_PROMPT)
    assert system_text == _LANE_SOUL
    initial_context = objective + "\n" + system_text
    _assert_conventions_once(initial_context)
    _assert_skill_mode(initial_context, skills)


@pytest.mark.parametrize("skills", _SKILL_MODES)
def test_claude_real_runner_reaches_command_and_append_system_file(
    monkeypatch, tmp_path, skills
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    _configure_lane(monkeypatch, "client_dev", "claude", skills)
    fake_binary = _write_fake_binary(tmp_path)
    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary",
        lambda model=None: str(fake_binary),
    )
    argv_out = tmp_path / "claude-argv.json"
    system_out = tmp_path / "claude-system.md"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_OUT", str(argv_out))
    monkeypatch.setenv("FAKE_CLAUDE_SYSTEM_OUT", str(system_out))

    result = _dispatch_lane("client_dev", workdir)

    assert result["success"] is True, result
    argv = json.loads(argv_out.read_text(encoding="utf-8"))
    assert "--append-system-prompt" not in argv
    prompt_path = Path(argv[argv.index("--append-system-prompt-file") + 1])
    assert argv.index("--append-system-prompt-file") < argv.index("--model")
    assert not prompt_path.exists()
    assert argv[-1] == _TASK_PROMPT
    system_text = system_out.read_text(encoding="utf-8")
    _assert_conventions_once(system_text)
    _assert_skill_mode(system_text, skills)



def _native_parent():
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "provider/model"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.request_overrides = {}
    parent.prefill_messages = None
    parent.enabled_toolsets = []
    parent.disabled_toolsets = []
    parent.session_id = "parent-native"
    parent._session_db = None
    parent._delegate_depth = 0
    parent._oneshot_children_spawned = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent._subagent_id = None
    parent._current_turn_id = ""
    parent._interrupt_requested = False
    parent._subdirectory_hints = None
    parent.terminal_cwd = None
    parent.cwd = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _mock_native_model_boundary(monkeypatch):
    captured = []
    client = MagicMock()

    def _create(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        message = SimpleNamespace(content="done", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], model="provider/model", usage=None)

    client.chat.completions.create.side_effect = _create
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(generic_delegate, "_resolve_delegation_credentials", lambda *args, **kwargs: {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "test-key",
        "api_mode": "chat_completions",
        "model": "provider/model",
        "command": None,
        "args": None,
    })
    monkeypatch.setattr(generic_delegate, "_load_config", lambda: {
        "max_iterations": 2, "max_spawn_depth": 1,
    })
    return captured


def _effective_system_and_user(captured):
    assert captured
    # A streaming health retry may follow; the contract under test is the
    # initial outbound request, before any provider-boundary adaptation.
    messages = captured[0]["messages"]
    assert messages[0]["role"] == "system"
    user_text = "\n".join(
        str(message.get("content") or "")
        for message in messages[1:] if message.get("role") == "user"
    )
    return str(messages[0]["content"]), user_text


@pytest.mark.parametrize("skills", _SKILL_MODES)
def test_native_lane_reaches_effective_initial_system_context(
    monkeypatch, tmp_path, skills
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    _configure_lane(monkeypatch, "reverse_engineer", "subagent", skills)
    captured = _mock_native_model_boundary(monkeypatch)

    result = _dispatch_lane("reverse_engineer", workdir, parent_agent=_native_parent())

    assert result["success"] is True, result
    system_text, user_text = _effective_system_and_user(captured)
    assert "You are a focused subagent working on a specific delegated task." in system_text
    assert f"Run all workspace operations in this existing directory: {workdir}" in system_text
    assert user_text == _TASK_PROMPT
    _assert_conventions_once(system_text)
    _assert_skill_mode(system_text, skills)


def test_direct_native_subagent_bootstraps_effective_initial_system_context(monkeypatch):
    captured = _mock_native_model_boundary(monkeypatch)

    result = registry.dispatch(
        "delegate_agent",
        {
            "goal": "Implement directly.",
            "context": "Direct native context.",
            "background": False,
        },
        parent_agent=_native_parent(),
    )
    if isinstance(result, str):
        result = json.loads(result)

    assert result["results"][0]["status"] == "completed", result
    system_text, user_text = _effective_system_and_user(captured)
    assert "You are a focused subagent working on a specific delegated task." in system_text
    assert "CONTEXT:\nDirect native context." in system_text
    assert user_text == "Implement directly."
    _assert_conventions_once(system_text)


def test_direct_native_subagent_preserves_per_task_soul_material(monkeypatch):
    captured = _mock_native_model_boundary(monkeypatch)

    result = registry.dispatch(
        "delegate_agent",
        {
            "tasks": [{
                "goal": "Implement with the supplied native soul.",
                "ephemeral_system_prompt": "Existing per-task native soul.",
            }],
            "background": False,
        },
        parent_agent=_native_parent(),
    )
    if isinstance(result, str):
        result = json.loads(result)

    assert result["results"][0]["status"] == "completed", result
    system_text, user_text = _effective_system_and_user(captured)
    assert system_text.count("Existing per-task native soul.") == 1
    assert user_text == "Implement with the supplied native soul."
    _assert_conventions_once(system_text)
