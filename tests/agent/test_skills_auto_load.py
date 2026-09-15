"""skills.auto_load: pinned skills land in every new session's prompt, resolved once per agent."""

from __future__ import annotations

from unittest.mock import patch


def _write_skill(root, name, body):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test.\n---\n\n{body}\n")
    return skill_dir / "SKILL.md"


def _bare_agent(session_id="auto-load-test"):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.valid_tool_names = {"skills_list", "skill_view", "skill_manage"}
    agent.model = "test-model"
    agent.provider = "test"
    agent.pass_session_id = False
    agent.skip_context_files = True
    agent.load_soul_identity = False
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._memory_manager = None
    agent._memory_store = None
    agent.session_id = session_id
    agent.platform = "telegram"
    agent._tool_use_enforcement = False
    agent.ephemeral_system_prompt = None
    agent._cached_system_prompt = None
    agent._auto_load_skills_resolved = False
    agent._auto_load_skills_result = ("", [], [])
    return agent


class TestBuildAutoLoadPrompt:
    def test_loads_configured_skills_and_reports_missing(self, tmp_path):
        from agent.skill_commands import build_auto_load_prompt

        _write_skill(tmp_path, "pinned-skill", "PINNED CONTENT")
        cfg = {"skills": {"auto_load": ["pinned-skill", " pinned-skill ", "no-such-skill", 7]}}
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            prompt, loaded, missing = build_auto_load_prompt(task_id="s1", user_config=cfg)
        assert loaded == ["pinned-skill"]
        assert missing == ["no-such-skill"]
        assert "PINNED CONTENT" in prompt
        assert prompt.count("auto-loaded via config (skills.auto_load)") == 1

    def test_explicit_preload_dedupes_against_auto_loaded_names(self, tmp_path):
        from agent.skill_commands import build_preloaded_skills_prompt

        _write_skill(tmp_path, "pinned-skill", "PINNED CONTENT")
        _write_skill(tmp_path, "extra-skill", "EXTRA CONTENT")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            prompt, loaded, missing = build_preloaded_skills_prompt(
                ["pinned-skill", "extra-skill"], task_id="s1", excluded_loaded_names={"pinned-skill"},
            )
        # Still counted as resolved (so a typo elsewhere degrades gracefully), but not rendered twice.
        assert loaded == ["pinned-skill", "extra-skill"] and missing == []
        assert "PINNED CONTENT" not in prompt and "EXTRA CONTENT" in prompt


class TestSharedPromptPath:
    def test_prompt_is_byte_stable_after_config_and_skill_mutation(self, tmp_path, monkeypatch):
        """The whole point: rebuilds (model switch, compression) reuse the first resolution."""
        monkeypatch.delenv("HERMES_IGNORE_RULES", raising=False)
        skill_file = _write_skill(tmp_path, "stable-skill", "ORIGINAL SKILL BYTES")
        cfg = {"skills": {"auto_load": ["stable-skill"]}}
        agent = _bare_agent()
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path), \
             patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            first = agent._build_system_prompt()
            assert "ORIGINAL SKILL BYTES" in first and agent._auto_load_skills_result[1] == ["stable-skill"]
            skill_file.write_text("---\nname: stable-skill\ndescription: Test.\n---\n\nMUTATED BYTES\n")
            cfg["skills"]["auto_load"] = []
            agent.model = "after-switch"
            agent._cached_system_prompt = None
            rebuilt = agent._build_system_prompt()
        assert "ORIGINAL SKILL BYTES" in rebuilt and "MUTATED BYTES" not in rebuilt

    def test_ignore_rules_suppresses_auto_load(self, tmp_path, monkeypatch):
        _write_skill(tmp_path, "stable-skill", "ORIGINAL SKILL BYTES")
        cfg = {"skills": {"auto_load": ["stable-skill"]}}
        monkeypatch.setenv("HERMES_IGNORE_RULES", "1")
        agent = _bare_agent()
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path), \
             patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            prompt = agent._build_system_prompt()
        assert "ORIGINAL SKILL BYTES" not in prompt
        assert agent._auto_load_skills_resolved is True and agent._auto_load_skills_result == ("", [], [])
