"""Tests for agent-settings copy in the interactive setup wizard."""

import pytest
import yaml

from hermes_cli.config import resolve_turn_limit, TURN_LIMIT_UNLIMITED
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


def _run_agent_settings_wizard(config, max_iterations_answer, monkeypatch, tmp_path):
    """Drive setup_agent_settings with every prompt stubbed, one answer at a time.

    `max_iterations_answer` is what the stubbed `prompt()` yields for the Max
    iterations question (the real prompt returns the current value on Enter,
    which the tests simulate by passing it here).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompt_answers = iter([max_iterations_answer, "all", "0.5"])
    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_answers))
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 4)
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.remove_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda *args, **kwargs: None)

    setup_agent_settings(config)


class TestMaxIterationsTurnLimitParser:
    """The Max iterations prompt routes through the shared turn-limit parser.

    Regression guard: the wizard used to parse the answer with bare
    ``int()`` + ``max_iter > 0``, so every "unlimited" spelling the help
    text advertised ('none' = unlimited) landed in the ValueError branch
    and silently kept the old *limited* value. The prompt now goes through
    ``resolve_turn_limit`` — the single normalization point — so all of its
    unlimited spellings are accepted and persisted, and positive integers
    are kept exact.
    """

    BASE_CONFIG = {
        "agent": {"max_turns": 500},
        "display": {"tool_progress": "all"},
        "compression": {"threshold": 0.50},
        "session_reset": {"mode": "both", "idle_minutes": 1440, "at_hour": 4},
    }

    @pytest.mark.parametrize("spelling", [
        "none", "None", "NONE",
        "null",
        "unlimited", "UNLIMITED",
        "infinite", "infinity", "inf", "∞",
        "0", "-1", "-42",
    ])
    def test_unlimited_spelling_accepted_and_persisted(
        self, spelling, tmp_path, monkeypatch, capsys
    ):
        config = {k: dict(v) for k, v in self.BASE_CONFIG.items()}

        _run_agent_settings_wizard(config, spelling, monkeypatch, tmp_path)

        # Persisted as a *supported* unlimited spelling, not the raw input
        # and not the sys.maxsize sentinel.
        assert config["agent"]["max_turns"] == "none"
        assert config["agent"]["max_turns"] != TURN_LIMIT_UNLIMITED
        # …and what lands in config.yaml still resolves to unlimited after a
        # YAML round-trip (save_config serializes via yaml.safe_dump).
        persisted = yaml.safe_load(yaml.safe_dump(config))["agent"]["max_turns"]
        assert resolve_turn_limit(persisted) == TURN_LIMIT_UNLIMITED
        out = capsys.readouterr().out
        assert "unlimited" in out  # and NOT rejected as the old int() parse did
        assert "Invalid number" not in out

    def test_positive_integer_kept_exact(self, tmp_path, monkeypatch, capsys):
        config = {k: dict(v) for k, v in self.BASE_CONFIG.items()}

        _run_agent_settings_wizard(config, "137", monkeypatch, tmp_path)

        assert config["agent"]["max_turns"] == 137
        assert isinstance(config["agent"]["max_turns"], int)
        assert "Max iterations set to 137" in capsys.readouterr().out

    def test_enter_keeps_default_max_turns(self, tmp_path, monkeypatch, capsys):
        """Enter keeps the current value — DEFAULT_MAX_TURNS when unset."""
        config = {
            "display": {"tool_progress": "all"},
            "compression": {"threshold": 0.50},
            "session_reset": {"mode": "both", "idle_minutes": 1440, "at_hour": 4},
        }

        # The real prompt() returns the shown default on Enter.
        _run_agent_settings_wizard(config, str(DEFAULT_MAX_TURNS), monkeypatch, tmp_path)

        assert config["agent"]["max_turns"] == DEFAULT_MAX_TURNS
        assert DEFAULT_MAX_TURNS == 256
        assert f"Press Enter to keep {DEFAULT_MAX_TURNS}." in capsys.readouterr().out

    def test_garbage_input_keeps_current_value(self, tmp_path, monkeypatch, capsys):
        config = {k: dict(v) for k, v in self.BASE_CONFIG.items()}

        _run_agent_settings_wizard(config, "not-a-number", monkeypatch, tmp_path)

        assert config["agent"]["max_turns"] == 500
        out = capsys.readouterr().out
        assert "Invalid number, keeping current value" in out
        assert "Max iterations set to" not in out

    @pytest.mark.parametrize("answer", ["1e309", "+inf", "-inf", "1e400", "nan"])
    def test_overflow_input_keeps_current_value(self, answer, tmp_path, monkeypatch, capsys):
        """An overflow-like answer must not crash the wizard nor change the limit.

        Regression guard: the resolver's string path did ``int(float(s))`` with
        only ``except ValueError``, so ``int(float("1e309"))`` raised
        OverflowError out of ``setup_agent_settings`` — the wizard died on the
        Max iterations prompt instead of keeping the current value like it
        does for any other unusable answer.
        """
        config = {k: dict(v) for k, v in self.BASE_CONFIG.items()}

        _run_agent_settings_wizard(config, answer, monkeypatch, tmp_path)

        assert config["agent"]["max_turns"] == 500
        out = capsys.readouterr().out
        assert "Invalid number, keeping current value" in out
        assert "Max iterations set to" not in out

    def test_legacy_zero_config_normalized_to_supported_spelling(
        self, tmp_path, monkeypatch, capsys
    ):
        """A hand-edited `max_turns: 0` kept on Enter is re-persisted as 'none'.

        Runtime already treats 0 as unlimited; the wizard normalizes it to
        the spelling config.yaml documents.
        """
        config = {k: dict(v) for k, v in self.BASE_CONFIG.items()}
        config["agent"]["max_turns"] = 0

        # prompt() returns str(0) == "0" when the user presses Enter.
        _run_agent_settings_wizard(config, "0", monkeypatch, tmp_path)

        assert config["agent"]["max_turns"] == "none"
        assert resolve_turn_limit(config["agent"]["max_turns"]) == TURN_LIMIT_UNLIMITED
