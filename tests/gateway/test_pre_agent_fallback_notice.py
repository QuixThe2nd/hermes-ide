"""A fallback resolved during gateway credential resolution (before any AIAgent exists) must carry a
user-visible notice through the agent's one-shot fallback-notice mechanism (#74349).

Fork seam: the gateway has no ``_try_resolve_fallback_provider`` loop; credential fallback walks the
shared ``hermes_cli.runtime_provider.resolve_runtime_with_fallback`` (#81209), so the notice is
stamped in ``gateway.run._resolve_runtime_agent_kwargs`` when a ``fallback_entry`` comes back.
"""
from unittest.mock import patch

import gateway.run as gateway_run
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


def test_primary_success_carries_no_notice():
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={"provider": "openai-codex", "model": "gpt-5.6-sol",
                             "api_key": "k", "base_url": "u"}), \
         patch("hermes_cli.runtime_provider._get_model_config",
               return_value={"provider": "openai-codex", "default": "gpt-5.6-sol"}):
        kwargs = gateway_run._resolve_runtime_agent_kwargs()
    assert "_fallback_notice" not in kwargs
