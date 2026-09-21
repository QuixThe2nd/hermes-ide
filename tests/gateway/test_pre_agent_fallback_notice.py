"""A fallback resolved during gateway credential resolution (before any AIAgent exists) must carry a
user-visible notice through the agent's one-shot fallback-notice mechanism (#74349).

Fork seam: the gateway has no ``_try_resolve_fallback_provider`` loop; credential fallback walks the
shared ``hermes_cli.runtime_provider.resolve_runtime_with_fallback`` (#81209), so the notice is
stamped in ``gateway.run._resolve_runtime_agent_kwargs`` when a ``fallback_entry`` comes back, popped
by the runner-owned resolver, and attached to the fresh agent by the turn runner. The second test
drives the production entry point (``gateway.run.GatewayRunner._resolve_session_agent_runtime`` bound
to the runner, then the monolith ``TurnRunner.run_sync``) so the pop and the attach are both pinned.
"""
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gateway.run as gateway_run
from gateway.session import Platform, SessionSource
from gateway.turn_context import TurnContext
from hermes_cli.auth import AuthError


def test_credential_resolution_fallback_carries_notice():
    primary = {"provider": "openai-codex", "default": "gpt-5.6-sol"}
    fallback_entry = {"provider": "anthropic", "model": "claude-sonnet-5"}

    def _fake_resolve(requested=None, target_model=None, explicit_base_url=None,
                      explicit_api_key=None, **_kwargs):
        if requested == "anthropic":
            return {"provider": "anthropic", "model": "claude-sonnet-5",
                    "api_key": explicit_api_key, "base_url": explicit_base_url}
        raise AuthError("expired")

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=_fake_resolve), \
         patch("hermes_cli.runtime_provider._get_model_config",
               return_value=primary), \
         patch("hermes_cli.fallback_config.get_fallback_chain",
               return_value=[fallback_entry]), \
         patch("hermes_cli.fallback_config.resolve_entry_api_key",
               return_value="k"):
        kwargs = gateway_run._resolve_runtime_agent_kwargs()

    notice = kwargs["_fallback_notice"]
    assert "openai-codex/gpt-5.6-sol" in notice and "anthropic/claude-sonnet-5" in notice


class _RecordingAgent:
    built_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).built_kwargs = kwargs
        self.model = kwargs["model"]
        self.session_id = kwargs.get("session_id")
        self.tools = []
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=200_000)
        self.session_prompt_tokens = self.session_completion_tokens = 0

    def run_conversation(self, _message, **_kwargs):
        return {"final_response": "ok", "messages": []}


def _runner_with_real_runtime_resolution():
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._agent_config_signature.return_value = ("sig",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    # Production resolution: the real GatewayRunner method on a stubbed runner,
    # with the /model-override and channel-override lookups empty.
    runner._resolve_session_key_or_none.return_value = "test-session-key"
    runner._rehydrate_session_model_override.return_value = None
    runner._peek_session_state.return_value = None
    runner._sessions_map.return_value = {}
    runner._session_state.return_value = SimpleNamespace(
        conversation=SimpleNamespace(last_resolved_model=None))
    runner._resolve_session_agent_runtime = types.MethodType(
        gateway_run.GatewayRunner._resolve_session_agent_runtime, runner)
    # Real route builder too: it filters runtime to the constructor-safe key set
    # (request_overrides travels separately) exactly as in production.
    runner._resolve_turn_agent_config = types.MethodType(
        gateway_run.GatewayRunner._resolve_turn_agent_config, runner)
    return runner


def test_credential_resolution_fallback_reaches_agent_notice_not_agent_kwargs():
    primary = {"provider": "openai-codex", "default": "gpt-5.6-sol"}
    fallback_entry = {"provider": "anthropic", "model": "claude-sonnet-5"}

    def _fake_resolve(requested=None, target_model=None, explicit_base_url=None,
                      explicit_api_key=None, **_kwargs):
        if requested == "anthropic":
            return {"provider": "anthropic", "model": "claude-sonnet-5",
                    "api_key": explicit_api_key, "base_url": explicit_base_url}
        raise AuthError("expired")

    runner = _runner_with_real_runtime_resolution()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
        message="hi", history=[], session_id="sid", session_key="test-session-key",
        user_config={}, AIAgent=_RecordingAgent,
        resolve_display_setting=lambda *_a, **_k: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=_fake_resolve), \
         patch("hermes_cli.runtime_provider._get_model_config", return_value=primary), \
         patch("hermes_cli.fallback_config.get_fallback_chain", return_value=[fallback_entry]), \
         patch("hermes_cli.fallback_config.resolve_entry_api_key", return_value="k"):
        result = gateway_run.TurnRunner(runner, ctx).run_sync()

    assert result["final_response"] == "ok"
    agent = ctx.agent_holder[0]
    notice = agent._pending_fallback_notice
    assert "openai-codex/gpt-5.6-sol" in notice and "anthropic/claude-sonnet-5" in notice
    assert "_fallback_notice" not in _RecordingAgent.built_kwargs
    # Consumed by the turn: a later resolution without fallback must not re-attach a stale notice.
    assert runner._pre_agent_fallback_notice is None


def test_model_override_fast_path_clears_stale_notice():
    """The /model-override fast path returns before the pop; the entry reset means a notice stashed by
    an earlier resolution (hygiene, inbound, another session) never survives to this session's turn."""
    runner = _runner_with_real_runtime_resolution()
    runner._pre_agent_fallback_notice = "⚠️ Provider fallback: stale"
    override = {"model": "claude-sonnet-5", "provider": "anthropic", "api_key": "k", "base_url": "u"}
    runner._peek_session_state.return_value = SimpleNamespace(
        conversation=SimpleNamespace(model_override=override))
    with patch("gateway.run._credential_pool_for_provider", return_value=None):
        model, runtime = runner._resolve_session_agent_runtime(session_key="test-session-key")
    assert (model, runtime["provider"]) == ("claude-sonnet-5", "anthropic")
    assert runner._pre_agent_fallback_notice is None
