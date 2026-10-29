"""Claude Code plan identity prefix for codex_responses custom-provider routes."""

from types import SimpleNamespace

from agent.anthropic_adapter import _CLAUDE_CODE_SYSTEM_PREFIX
from agent.chat_completion_helpers import build_api_kwargs
from agent.claude_code_plan_identity import resolve_claude_code_plan_identity
from hermes_cli.config_providers import providers_dict_to_custom_providers


_LITELLM_URL = "http://litellm-proxy.example/v1"
_SYSTEM = "You are Hermes Agent, a helpful assistant."
_MESSAGES = [
    {"role": "system", "content": _SYSTEM},
    {"role": "user", "content": "Ping"},
]


def _litellm_custom_providers(*, marked_model: str | None = None, list_models: bool = False):
    if list_models:
        models = ["hermes", marked_model or "claude-opus-5-5"]
    else:
        models = {
            "hermes": {},
            marked_model or "claude-opus-5-5": {"claude_code_plan_identity": True},
        }
    return providers_dict_to_custom_providers(
        {
            "litellm": {
                "name": "LiteLLM",
                "api": _LITELLM_URL,
                "transport": "codex_responses",
                "key_env": "LITELLM_API_KEY",
                "models": models,
            }
        }
    )


def _codex_agent(*, model: str, plan_identity: bool):
    agent = SimpleNamespace(
        api_mode="codex_responses",
        model=model,
        provider="litellm",
        base_url=_LITELLM_URL,
        tools=[{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}],
        max_tokens=4096,
        reasoning_config={},
        request_overrides={},
        session_id="sess-test",
        text_verbosity=None,
        _codex_reasoning_replay_enabled=True,
        _claude_code_plan_identity=plan_identity,
        _resolved_api_call_timeout=lambda: 60.0,
        _prepare_messages_for_non_vision_model=lambda msgs: msgs,
        _github_models_reasoning_extra_body=lambda: None,
    )

    class _Transport:
        def build_kwargs(self, **params):
            from agent.transports.codex import ResponsesApiTransport

            return ResponsesApiTransport().build_kwargs(**params)

    agent._get_transport = lambda: _Transport()
    return agent


def test_resolve_plan_identity_list_form_models():
    providers = _litellm_custom_providers(list_models=True)
    assert isinstance(providers[0].get("models"), dict)
    assert "hermes" in providers[0]["models"]
    assert resolve_claude_code_plan_identity(
        provider="litellm",
        model="hermes",
        base_url=_LITELLM_URL,
        api_mode="codex_responses",
        custom_providers=providers,
    ) is False


def test_marked_model_instructions_begin_with_prefix():
    agent = _codex_agent(model="claude-opus-5-5", plan_identity=True)
    kwargs = build_api_kwargs(agent, list(_MESSAGES), tools_for_api=agent.tools)
    assert kwargs["instructions"] == _CLAUDE_CODE_SYSTEM_PREFIX
    user = next(item for item in kwargs["input"] if item.get("role") == "user")
    assert _SYSTEM in str(user.get("content", ""))


def test_unmarked_model_has_no_plan_prefix():
    agent = _codex_agent(model="hermes", plan_identity=False)
    kwargs = build_api_kwargs(agent, list(_MESSAGES), tools_for_api=agent.tools)
    assert not kwargs["instructions"].startswith(_CLAUDE_CODE_SYSTEM_PREFIX)
    assert kwargs["instructions"] == _SYSTEM


def test_unmarked_model_kwargs_match_without_plan_identity_flag():
    baseline_agent = _codex_agent(model="hermes", plan_identity=False)
    baseline = build_api_kwargs(baseline_agent, list(_MESSAGES), tools_for_api=baseline_agent.tools)

    probe = _codex_agent(model="hermes", plan_identity=False)
    probe._claude_code_plan_identity = False
    current = build_api_kwargs(probe, list(_MESSAGES), tools_for_api=probe.tools)

    assert current == baseline


def test_resolve_from_per_model_config():
    providers = _litellm_custom_providers()
    assert resolve_claude_code_plan_identity(
        provider="custom:litellm",
        model="claude-opus-5-5",
        base_url=_LITELLM_URL,
        api_mode="codex_responses",
        custom_providers=providers,
    )
    assert not resolve_claude_code_plan_identity(
        provider="custom:litellm",
        model="hermes",
        base_url=_LITELLM_URL,
        api_mode="codex_responses",
        custom_providers=providers,
    )
