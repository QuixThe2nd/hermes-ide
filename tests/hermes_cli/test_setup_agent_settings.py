"""Tests for agent-settings copy in the interactive setup wizard."""

from hermes_cli.config_defaults import DEFAULT_MAX_TURNS
from hermes_cli.setup import _apply_default_agent_settings, setup_agent_settings


def test_setup_agent_settings_uses_displayed_max_iterations_value(tmp_path, monkeypatch, capsys):
    """The helper text should match the value shown in the prompt.

    After PR#18413 max_turns is read exclusively from config.yaml — the
    .env `HERMES_MAX_ITERATIONS` fallback was removed because it was
    shadowing the user's current config (see the 60-vs-500 incident).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = {
        "agent": {"max_turns": 60},
        "display": {"tool_progress": "all"},
        "compression": {"threshold": 0.50},
        "session_reset": {"mode": "both", "idle_minutes": 1440, "at_hour": 4},
    }

    prompt_answers = iter(["60", "all", "0.5"])

    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_answers))
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 4)
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.remove_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda *args, **kwargs: None)

    setup_agent_settings(config)

    out = capsys.readouterr().out
    assert "Press Enter to keep 60." in out
    assert "Default is 90" not in out


def test_setup_agent_settings_prefers_config_over_stale_env(tmp_path, monkeypatch, capsys):
    """Config.yaml wins even when a stale .env value disagrees.

    Regression guard for the bug where `.env HERMES_MAX_ITERATIONS=60`
    from an old `hermes setup` run shadowed `agent.max_turns: 500` in
    config.yaml. The wizard must now display the config value.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = {
        "agent": {"max_turns": 500},  # user bumped this in config.yaml
        "display": {"tool_progress": "all"},
        "compression": {"threshold": 0.50},
        "session_reset": {"mode": "both", "idle_minutes": 1440, "at_hour": 4},
    }

    prompt_answers = iter(["500", "all", "0.5"])

    # Simulate stale .env value — the wizard must ignore this.
    monkeypatch.setattr(
        "hermes_cli.setup.get_env_value",
        lambda key: "60" if key == "HERMES_MAX_ITERATIONS" else "",
    )
    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_answers))
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 4)
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda *args, **kwargs: None)

    removed_keys: list[str] = []
    monkeypatch.setattr(
        "hermes_cli.setup.remove_env_value",
        lambda key: (removed_keys.append(key), True)[1],
    )
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda *args, **kwargs: None)

    setup_agent_settings(config)

    out = capsys.readouterr().out
    # Config value wins
    assert "Press Enter to keep 500." in out
    assert "Press Enter to keep 60." not in out
    # And the stale .env entry gets cleaned up
    assert "HERMES_MAX_ITERATIONS" in removed_keys


class TestSetupDefaultMaxTurns:
    """Ordinary setup must write the standard DEFAULT_MAX_TURNS budget.

    Regression guard: the recommended-defaults path used to hardcode 150
    while every other runtime path resolved a different number, so a fresh
    `hermes setup` produced a config that disagreed with the documented
    default. All construction paths share DEFAULT_MAX_TURNS now.
    """

    def test_apply_default_agent_settings_writes_default_max_turns(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        saved: list[dict] = []
        monkeypatch.setattr("hermes_cli.setup.save_config", lambda cfg: saved.append(cfg))
        monkeypatch.setattr(
            "hermes_cli.setup.remove_env_value", lambda key: True
        )

        config: dict = {}
        _apply_default_agent_settings(config)

        assert config["agent"]["max_turns"] == DEFAULT_MAX_TURNS
        assert DEFAULT_MAX_TURNS == 256

    def test_apply_default_agent_settings_prints_written_value(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr("hermes_cli.setup.save_config", lambda cfg: None)
        monkeypatch.setattr("hermes_cli.setup.remove_env_value", lambda key: True)

        _apply_default_agent_settings({})

        out = capsys.readouterr().out
        assert f"Max iterations: {DEFAULT_MAX_TURNS}" in out
        # The stale preset copy must not come back.
        assert "Max iterations: 150" not in out

    def test_setup_prompt_falls_back_to_default_when_unset(self, tmp_path, monkeypatch, capsys):
        """A config with no agent.max_turns shows the standard default.

        The wizard's fallback used to read `default=90`, so a fresh install
        was prompted to "keep 90" — a number that matched no runtime path.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        config = {
            "display": {"tool_progress": "all"},
            "compression": {"threshold": 0.50},
            "session_reset": {"mode": "both", "idle_minutes": 1440, "at_hour": 4},
        }
        prompt_answers = iter([str(DEFAULT_MAX_TURNS), "all", "0.5"])
        monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_answers))
        monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 4)
        monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda *args, **kwargs: None)
        monkeypatch.setattr("hermes_cli.setup.remove_env_value", lambda *args, **kwargs: None)
        monkeypatch.setattr("hermes_cli.setup.save_config", lambda *args, **kwargs: None)

        setup_agent_settings(config)

        out = capsys.readouterr().out
        assert f"Press Enter to keep {DEFAULT_MAX_TURNS}." in out
        assert "Press Enter to keep 90." not in out
        assert "Press Enter to keep 150." not in out
