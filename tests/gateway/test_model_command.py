"""Regression tests for the restored /model slash command (PR #674 removed the
surface, kept the switch machinery). Pins: registration + dispatch on the
gateway, CLI, Discord and the relay manifest; the no-arg listing; and the
switch commit — override set, cached agent swapped in place, a failed swap
rolls the whole commit back."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_make_source())


def _make_runner():
    """Minimal GatewayRunner stub (object.__new__, real mixin methods)."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="tok")}
    )
    runner._session_model_overrides = {}
    runner._pending_one_turn_model_restores = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._async_session_store = AsyncMock()
    runner._delivery_adapter_for = lambda source: None
    return runner


def _switch_result(**over):
    from hermes_cli.model_switch import ModelSwitchResult

    return ModelSwitchResult(
        success=True, new_model="new-model-x", target_provider="litellm",
        provider_changed=True, api_key="sk-test", base_url="https://litellm.example/v1",
        api_mode="chat_completions", provider_label="LiteLLM", **over)


def _patch_config(monkeypatch, tmp_path, model="current-model-y"):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path, raising=False)
    monkeypatch.setattr(
        gateway_run, "_load_gateway_config",
        lambda config_path=None: {"model": {"default": model, "provider": "litellm"}})


def test_model_is_a_registered_command():
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("model")
    assert cmd is not None and cmd.name == "model"
    assert cmd.busy_policy == "reject" and cmd.busy_handler == "model"
    assert not cmd.cli_only and not cmd.gateway_only


def test_gateway_dispatches_model_to_idle_handler():
    runner = _make_runner()
    handlers = runner._gateway_idle_command_handlers()
    assert handlers["model"] == runner._handle_model_command
    # Mid-run the command is rejected with a model-specific message, not the generic catch-all.
    assert "model" in GatewayRunner._BUSY_REJECT_TEXT


def test_cli_resolves_model_to_a_handler():
    from cli import HermesCLI
    entry = HermesCLI._slash_handler("model")
    assert entry is not None
    method_name, pass_arg = entry
    assert callable(getattr(HermesCLI, method_name)) and pass_arg is True


@pytest.mark.parametrize("surface", ["discord", "relay"])
def test_native_surfaces_expose_model(surface):
    if surface == "discord":
        from tests.gateway.test_discord_slash_commands import _ensure_discord_mock

        _ensure_discord_mock()
        from plugins.platforms.discord.adapter import _NATIVE_SLASH_COMMANDS

        rows = [row for row in _NATIVE_SLASH_COMMANDS if row[0] == "model"]
        assert len(rows) == 1
        _name, _desc, options, template, _followup = rows[0]
        assert (options[0][0], template) == ("name", "/model {name}")
    else:
        from gateway.relay.command_manifest import build_relay_command_manifest

        rows = [c for c in build_relay_command_manifest() if c["name"] == "model"]
        assert len(rows) == 1
        assert rows[0]["options"][0]["name"] == "name"


@pytest.mark.asyncio
async def test_no_arg_model_returns_current_model(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path)

    def _fake_listing(**kwargs):
        return [{"slug": "litellm", "name": "LiteLLM", "is_current": True,
                 "models": ["current-model-y", "other-model-z"], "total_models": 2}]

    monkeypatch.setattr(
        "hermes_cli.model_switch_providers.list_authenticated_providers", _fake_listing)

    reply = await _make_runner()._handle_model_command(_make_event("/model"))
    assert isinstance(reply, str) and "current-model-y" in reply  # current model is shown
    assert "other-model-z" in reply  # selectable models are listed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, marker",
    [("/model --once --global", "--once"),
     ("/model new-model-x --reasoning high", "--reasoning")],
)
async def test_model_flag_errors_are_rejected_with_gateway_decoration(raw, marker):
    reply = await _make_runner()._handle_model_command(_make_event(raw))
    assert isinstance(reply, str) and reply.startswith("❌")
    assert marker in reply


@pytest.mark.asyncio
async def test_model_switch_sets_override_and_swaps_cached_agent(monkeypatch, tmp_path):
    import hermes_cli.model_switch as ms

    _patch_config(monkeypatch, tmp_path)
    monkeypatch.setattr(ms, "switch_model", lambda *a, **k: _switch_result())

    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    agent = MagicMock()
    runner._cached_agent_for = lambda sk: agent if sk == session_key else None

    reply = await runner._handle_model_command(_make_event("/model new-model-x"))

    # The cached agent was swapped in place (new route forwarded).
    kwargs = agent.switch_model.call_args.kwargs
    assert kwargs["new_model"] == "new-model-x"
    assert kwargs["new_provider"] == "litellm"
    # The session override is recorded for the next turn's rebuild.
    override = runner._session_model_overrides[session_key]
    assert override["model"] == "new-model-x"
    assert override["provider"] == "litellm"
    assert isinstance(reply, str) and "new-model-x" in reply


@pytest.mark.asyncio
async def test_model_switch_failure_reports_error(monkeypatch, tmp_path):
    import hermes_cli.model_switch as ms

    _patch_config(monkeypatch, tmp_path, model="m")
    monkeypatch.setattr(
        ms, "switch_model",
        lambda *a, **k: ms.ModelSwitchResult(success=False, error_message="nope"))

    runner = _make_runner()
    reply = await runner._handle_model_command(_make_event("/model missing-model"))
    assert isinstance(reply, str) and "nope" in reply
    assert not runner._session_model_overrides


@pytest.mark.asyncio
async def test_failed_agent_swap_rolls_back_and_keeps_override_out(monkeypatch, tmp_path):
    """A cached-agent switch failure aborts the commit: no override, error mentions rollback."""
    import hermes_cli.model_switch as ms

    _patch_config(monkeypatch, tmp_path, model="m")
    monkeypatch.setattr(ms, "switch_model", lambda *a, **k: _switch_result())

    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    agent = MagicMock()
    agent.switch_model = MagicMock(side_effect=RuntimeError("provider rejected the swap"))
    runner._cached_agent_for = lambda sk: agent if sk == session_key else None

    reply = await runner._handle_model_command(_make_event("/model new-model-x"))

    assert isinstance(reply, str) and "staying on" in reply
    assert session_key not in runner._session_model_overrides
    assert not runner._pending_model_notes
