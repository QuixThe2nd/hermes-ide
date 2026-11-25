"""Tests for setup.py configuration flows."""
import os
from pathlib import Path

import pytest

from hermes_cli.config import load_config, save_config, get_env_value
from hermes_cli import setup as setup_mod
from hermes_cli.setup import setup_model_provider


def _maybe_keep_current_tts(question, choices):
    if question != "Select TTS provider:":
        return None
    assert choices[-1].startswith("Keep current (")
    return len(choices) - 1


def _clear_provider_env(monkeypatch):
    for key in (
        "NOUS_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)



def _stub_tts(monkeypatch):
    """Stub out TTS prompts so setup_model_provider doesn't block."""
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda q, c, d=0: (
        _maybe_keep_current_tts(q, c) if _maybe_keep_current_tts(q, c) is not None
        else d
    ))
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **kw: False)


def _write_model_config(tmp_path, provider, base_url="", model_name="test-model"):
    """Simulate what a _model_flow_* function writes to disk."""
    cfg = load_config()
    m = cfg.get("model")
    if not isinstance(m, dict):
        m = {"default": m} if m else {}
        cfg["model"] = m
    m["provider"] = provider
    if base_url:
        m["base_url"] = base_url
    if model_name:
        m["default"] = model_name
    save_config(cfg)


def test_setup_delegates_to_select_provider_and_model(tmp_path, monkeypatch):
    """setup_model_provider calls select_provider_and_model and syncs config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    config = load_config()

    def fake_select():
        _write_model_config(tmp_path, "custom", "http://localhost:11434/v1", "qwen3.5:32b")

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)

    setup_model_provider(config)
    save_config(config)

    reloaded = load_config()
    assert isinstance(reloaded["model"], dict)
    assert reloaded["model"]["provider"] == "custom"
    assert reloaded["model"]["base_url"] == "http://localhost:11434/v1"
    assert reloaded["model"]["default"] == "qwen3.5:32b"






def test_select_provider_and_model_warns_if_named_custom_provider_disappears(
    tmp_path, monkeypatch, capsys
):
    """If a saved custom provider is deleted mid-selection, show a warning instead of silently doing nothing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)

    cfg = load_config()
    cfg["custom_providers"] = [{"name": "Local", "base_url": "http://localhost:8080/v1"}]
    save_config(cfg)

    def fake_prompt_provider_choice(choices, default=0):
        current = load_config()
        current["custom_providers"] = []
        save_config(current)
        return next(i for i, label in enumerate(choices) if label.startswith("Local (localhost:8080/v1)"))

    monkeypatch.setattr("hermes_cli.auth.resolve_provider", lambda provider: None)
    monkeypatch.setattr("hermes_cli.main._prompt_provider_choice", fake_prompt_provider_choice)
    monkeypatch.setattr(
        "hermes_cli.main._model_flow_named_custom",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("named custom flow should not run")),
    )

    from hermes_cli.main import select_provider_and_model

    select_provider_and_model()

    out = capsys.readouterr().out
    assert "selected saved custom provider is no longer available" in out








def _pick_terminal_backend(wanted):
    """prompt_choice fake that selects a backend menu entry by LABEL — never a hardcoded
    index — and records the menu the wizard actually offered."""
    menus = []

    def fake_prompt_choice(question, choices, default=0):
        assert question == "Select terminal backend:", question
        menus.append(list(choices))
        return next(i for i, label in enumerate(choices) if wanted in label)

    return menus, fake_prompt_choice


def _assert_local_ssh_menu(menu):
    """The picker offers exactly Local, SSH and Keep current: the sandbox backends
    (docker/modal/daytona/singularity/vercel) left with c7442dd1745 (#708). The
    Keep-current label echoes whatever legacy value the config holds, so retired-name
    absence is asserted on the selectable entries only."""
    assert len(menu) == 3
    assert menu[0].startswith("Local -") and menu[1].startswith("SSH -")
    assert menu[-1].startswith("Keep current")
    assert not any(
        retired in label.lower()
        for label in menu[:-1]
        for retired in ("docker", "modal", "daytona", "singularity", "vercel"))


def test_local_backend_setup_routes_config_and_env(tmp_path, monkeypatch):
    """Live routing contract for the surviving Local backend (the modal/vercel_sandbox
    wizard flows were removed with the backends in c7442dd1745, #708): selecting Local
    writes terminal.backend, defaults terminal.cwd, and persists TERMINAL_ENV."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_config()
    menus, pick = _pick_terminal_backend("Local -")

    monkeypatch.setattr("hermes_cli.setup.prompt_choice", pick)

    from hermes_cli.setup import setup_terminal_backend

    setup_terminal_backend(config)

    _assert_local_ssh_menu(menus[0])
    assert config["terminal"]["backend"] == "local"
    assert isinstance(config["terminal"].get("cwd"), str) and config["terminal"]["cwd"]
    assert os.environ["TERMINAL_ENV"] == "local"
    assert get_env_value("TERMINAL_ENV") == "local"


# test_setup_slack_* moved to tests/gateway/test_slack_plugin_setup.py — the
# _setup_slack wizard migrated to the slack plugin's interactive_setup (#41112).


def test_ssh_setup_replaces_retired_backend(tmp_path, monkeypatch):
    """A legacy config naming a retired backend (c7442dd1745, #708) still loads; rerunning
    the wizard and picking SSH replaces it and routes the connection fields to .env."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_config()
    config["terminal"]["backend"] = "vercel_sandbox"  # legacy value predating the strip
    menus, pick = _pick_terminal_backend("SSH -")

    answers = iter(["myhost", "me", "2222", ""])
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", pick)
    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *a, **kw: next(answers))
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **kw: False)

    from hermes_cli.setup import setup_terminal_backend

    setup_terminal_backend(config)

    _assert_local_ssh_menu(menus[0])
    assert config["terminal"]["backend"] == "ssh"
    assert os.environ["TERMINAL_ENV"] == "ssh"
    assert os.environ["TERMINAL_SSH_HOST"] == "myhost"
    assert os.environ["TERMINAL_SSH_USER"] == "me"
    assert os.environ["TERMINAL_SSH_PORT"] == "2222"
    assert get_env_value("TERMINAL_SSH_HOST") == "myhost"
    assert get_env_value("TERMINAL_SSH_PORT") == "2222"


def test_retired_backend_names_fail_closed_at_runtime():
    """Unsupported-input safety vs retired modalities: a retired backend name surviving in a
    legacy config must never execute — the requirement check rejects it and environment
    creation raises, each naming the supported set (local, ssh)."""
    from tools.terminal_tool_backends import (
        _check_plugin_requirements, _create_environment, terminal_backend_unavailable_reason,
    )

    for retired in ("modal", "docker", "daytona", "singularity", "vercel_sandbox"):
        assert _check_plugin_requirements({"env_type": retired}) is False
        reason = terminal_backend_unavailable_reason() or ""
        assert "Unknown TERMINAL_ENV" in reason and retired in reason
        with pytest.raises(ValueError) as excinfo:
            _create_environment(retired, image="", cwd=".", timeout=5)
        message = str(excinfo.value)
        assert "Unknown environment type" in message
        assert "'local'" in message and "'ssh'" in message
