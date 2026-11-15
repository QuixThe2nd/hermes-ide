"""Regression tests: per-job ``skills`` reach the cron agent as prompt attachments.

Mirrors the user's live setup in shape (a rent-watch style job listing
``information-monitoring`` + ``property-listings``) without copying any of
its private prompt/delivery content: fixture skills, fixture job, mocked
agent.

Contracts under test, through the REAL ``run_job`` skill-to-prompt dispatch
(``_build_job_prompt`` → ``_load_cron_skill_parts`` → ``skill_view``):

* every listed skill's content reaches the agent INSIDE the assembled user
  message — this is skill PROMPT ATTACHMENT, not a callable property API
  (no tool schema or toolset is added for the skills);
* the job's own instruction and the standing cron hint ride the same message
  alongside the skill content;
* building/running the prompt does not mutate the job's schedule, delivery
  target, or skill list.

All network/model/delivery surfaces are mocked; the job store lives in a
temp ``HERMES_HOME``.
"""

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_MONITORING_FIXTURE = """---
name: information-monitoring
description: "Fixture monitoring instructions for isolated cron tests."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
---

# Information monitoring (fixture)

Check the configured sources, diff against the previous run, and report deltas.

- MONITORING-FIXTURE-MARKER: only report what changed since the last run.
"""

_PROPERTY_FIXTURE = """---
name: property-listings
description: "Fixture property listing presentation instructions for isolated cron tests."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
---

# Property listings (fixture)

Present each new rental listing as a compact property card.

- PROPERTY-FIXTURE-MARKER: every listing card shows price, location, and link.
"""


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with the two fixture skills planted and the job store moved."""
    home = tmp_path / ".hermes"
    skills_dir = home / "skills"
    for rel, body in (
        ("research/information-monitoring", _MONITORING_FIXTURE),
        ("productivity/property-listings", _PROPERTY_FIXTURE),
    ):
        skill_dir = skills_dir / rel
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLES_DIR", str(home / "skill-bundles"))

    # skills_tool snapshots the skills dir at import time — repoint it (same
    # pattern as tests/cron/test_cron_prompt_injection_skill.py).
    import tools.skills_tool as skills_tool
    monkeypatch.setattr(skills_tool, "HERMES_HOME", home)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_dir)
    skills_tool._SKILLS_CACHE.clear()

    import agent.skill_bundles as skill_bundles
    skill_bundles._bundles_cache = {}
    skill_bundles._bundles_cache_mtime = None

    from cron import jobs as cron_jobs
    with cron_jobs.use_cron_store(home):
        yield home

    skills_tool._SKILLS_CACHE.clear()


@contextlib.contextmanager
def _run_job_patches(tmp_path):
    """Offline run_job patches (network/model/state/delivery all mocked).

    Mirrors tests/cron/test_cron_memory_contract.py::_run_job_patches; yields
    (mock_agent_cls, mock_agent) so tests can capture what the scheduler
    actually handed the agent.
    """
    fake_db = MagicMock()
    fake_db.get_compression_tip.side_effect = lambda session_id: session_id
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "fixture report"}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("cron.scheduler._hermes_home", tmp_path))
        stack.enter_context(patch("cron.scheduler_delivery._resolve_origin", return_value=None))
        stack.enter_context(patch("hermes_cli.env_loader.load_hermes_dotenv"))
        stack.enter_context(patch("hermes_cli.env_loader.reset_secret_source_cache"))
        stack.enter_context(patch("hermes_state_registry.acquire", return_value=fake_db))
        stack.enter_context(
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
            )
        )
        agent_patch = stack.enter_context(
            patch("run_agent.AIAgent", return_value=mock_agent))
        yield agent_patch, mock_agent


def _fixture_job(cron_env):
    from cron.jobs import create_job

    return create_job(
        name="fixture-rent-watch",
        prompt=(
            "Watch for new rental listings matching the standing criteria and "
            "report the ones worth acting on."
        ),
        schedule="every 2h",
        skills=["information-monitoring", "property-listings"],
    )


def test_job_skills_reach_agent_as_prompt_attachments(cron_env):
    """Both listed skills' content lands in the single user message handed to the agent."""
    from cron.jobs import get_job
    from cron.scheduler import run_job

    job = _fixture_job(cron_env)
    with _run_job_patches(cron_env) as (agent_cls, mock_agent):
        success, _doc, final, error = run_job(get_job(job["id"]))
    assert success, f"run_job failed: {error}"
    assert agent_cls.called, "the scheduler must construct the (mocked) agent"

    prompt = mock_agent.run_conversation.call_args.args[0]
    assert "MONITORING-FIXTURE-MARKER" in prompt, (
        "the information-monitoring skill content must reach the agent"
    )
    assert "PROPERTY-FIXTURE-MARKER" in prompt, (
        "the property-listings skill content must reach the agent"
    )
    # The job's own instruction and the standing cron hint ride the same message.
    assert "standing criteria" in prompt
    assert "scheduled cron job" in prompt
    # Prompt attachment shape: each skill is announced as an invoked skill, and
    # the whole payload is ONE user message — the skills are not exposed as a
    # callable API, so no tool schema/registry surface is involved here.
    assert 'invoked the "property-listings" skill' in prompt


def test_prompt_build_does_not_mutate_schedule_or_delivery(cron_env):
    """Building + running the prompt leaves the job's schedule/delivery/skills intact."""
    from cron.jobs import get_job
    from cron.scheduler import run_job

    job = get_job(_fixture_job(cron_env)["id"])
    before = {k: json.dumps(job.get(k), sort_keys=True)
              for k in ("schedule", "deliver", "skills", "prompt", "enabled_toolsets")}

    with _run_job_patches(cron_env):
        success, _doc, _final, error = run_job(job)
    assert success, f"run_job failed: {error}"

    after_job = get_job(job["id"])
    after = {k: json.dumps(after_job.get(k), sort_keys=True)
             for k in ("schedule", "deliver", "skills", "prompt", "enabled_toolsets")}
    assert after == before, (
        "run_job must not rewrite the job's schedule, delivery target, skills, "
        f"prompt, or toolsets — diff: {before} -> {after}"
    )
