"""Claude Code plan identity prefix for codex_responses custom-provider routes."""

from types import SimpleNamespace

from agent.anthropic_adapter import _CLAUDE_CODE_SYSTEM_PREFIX
from agent.chat_completion_helpers import build_api_kwargs
from agent.claude_code_plan_identity import resolve_claude_code_plan_identity
from hermes_cli.config_providers import providers_dict_to_custom_providers

_LITELLM_URL = "http://litellm-proxy.example/v1"
_SYSTEM = (
    "You are Hermes Agent from Nous Research. "
    "Repo NousResearch/hermes-agent is canonical. "
    "Built by the hermes-agent project. Use session_search for history."
)
_MESSAGES = [
    {"role": "system", "content": _SYSTEM},
    {"role": "user", "content": "Ping"},
]

_PLAN_IDENTITY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "session_search",
            "description": "Search past turns via session_search.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory",
            "description": "Store notes in memory.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "Run shell commands.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
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


def _codex_agent(*, model: str, plan_identity: bool, tools=None):
    tool_list = tools if tools is not None else [
        {"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}
    ]
    agent = SimpleNamespace(
        api_mode="codex_responses",
        model=model,
        provider="litellm",
        base_url=_LITELLM_URL,
        tools=tool_list,
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


def _wire_tool_names(kwargs):
    return [t.get("name") for t in kwargs.get("tools", []) if t.get("type") == "function"]


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
    relocated = str(user.get("content", ""))
    assert "Claude Code" in relocated
    assert "Anthropic" in relocated
    assert "claude-code" in relocated
    assert "chat_history_lookup" in relocated
    assert "NousResearch/hermes-agent" in relocated
    assert "Hermes Agent" not in relocated
    assert "Nous Research" not in relocated


def test_marked_route_aliases_oauth_sensitive_tools():
    agent = _codex_agent(model="claude-opus-5-5", plan_identity=True, tools=list(_PLAN_IDENTITY_TOOLS))
    kwargs = build_api_kwargs(agent, list(_MESSAGES), tools_for_api=agent.tools)
    assert _wire_tool_names(kwargs) == ["chat_history_lookup", "context_notes", "terminal"]
    ss_tool = next(t for t in kwargs["tools"] if t.get("name") == "chat_history_lookup")
    assert "chat_history_lookup" in ss_tool.get("description", "")
    assert "session_search" not in ss_tool.get("description", "")


def test_unmarked_route_keeps_tool_names():
    agent = _codex_agent(model="hermes", plan_identity=False, tools=list(_PLAN_IDENTITY_TOOLS))
    kwargs = build_api_kwargs(agent, list(_MESSAGES), tools_for_api=agent.tools)
    assert _wire_tool_names(kwargs) == ["session_search", "memory", "terminal"]


def test_plan_identity_reverse_aliases_tool_calls_on_same_transport():
    from agent.transports.codex import ResponsesApiTransport

    transport = ResponsesApiTransport()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "session_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    transport.build_kwargs(
        model="claude-opus-5-5",
        messages=list(_MESSAGES),
        tools=tools,
        claude_code_plan_identity=True,
        session_id="sess-reverse",
    )
    msg = SimpleNamespace(
        content=None,
        reasoning=None,
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                call_id="call_1",
                response_item_id="fc_1",
                function=SimpleNamespace(name="chat_history_lookup", arguments="{}"),
            ),
        ],
        codex_reasoning_items=None,
        codex_message_items=None,
        reasoning_details=None,
    )
    response = SimpleNamespace(output=[], status="completed")

    import agent.codex_responses_adapter as cra

    original = cra._normalize_codex_response

    def _fake_normalize(resp, issuer_kind=None, issuer_model=None):
        return msg, "tool_calls"

    cra._normalize_codex_response = _fake_normalize
    try:
        normalized = transport.normalize_response(response)
    finally:
        cra._normalize_codex_response = original
    assert normalized.tool_calls[0].name == "session_search"


def test_marked_route_renames_replayed_assistant_tool_calls():
    history = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_hist",
                    "type": "function",
                    "function": {"name": "session_search", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_hist", "content": "ok"},
        {"role": "user", "content": "Ping"},
    ]
    agent = _codex_agent(model="claude-opus-5-5", plan_identity=True, tools=list(_PLAN_IDENTITY_TOOLS))
    kwargs = build_api_kwargs(agent, history, tools_for_api=agent.tools)
    fc_items = [item for item in kwargs["input"] if item.get("type") == "function_call"]
    assert fc_items
    assert all(item.get("name") == "chat_history_lookup" for item in fc_items)
    assert not any(item.get("name") == "session_search" for item in fc_items)


def test_unmarked_model_has_no_plan_prefix():
    agent = _codex_agent(model="hermes", plan_identity=False)
    kwargs = build_api_kwargs(agent, list(_MESSAGES), tools_for_api=agent.tools)
    assert not kwargs["instructions"].startswith(_CLAUDE_CODE_SYSTEM_PREFIX)
    assert kwargs["instructions"] == _SYSTEM


def test_unmarked_model_kwargs_match_without_plan_identity_flag():
    baseline_agent = _codex_agent(model="hermes", plan_identity=False, tools=list(_PLAN_IDENTITY_TOOLS))
    baseline = build_api_kwargs(baseline_agent, list(_MESSAGES), tools_for_api=baseline_agent.tools)

    probe = _codex_agent(model="hermes", plan_identity=False, tools=list(_PLAN_IDENTITY_TOOLS))
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
