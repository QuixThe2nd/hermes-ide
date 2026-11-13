"""litellm-codex-images fails closed and never uses Codex OAuth."""

from __future__ import annotations

import importlib

import pytest

plugin = importlib.import_module("plugins.image_gen.litellm-codex-images")


def test_missing_named_entry_fails_closed(monkeypatch):
    monkeypatch.setattr(plugin, "_named_entry", lambda: (_ for _ in ()).throw(
        RuntimeError("litellm-codex-images is not a configured providers: entry")))
    provider = plugin.LiteLLMCodexImagesProvider()
    result = provider.generate("a red circle")
    assert result["success"] is False
    assert result.get("error_type") == "auth_required" or result.get("code") == "auth_required"
    assert "chatgpt.com" not in result["error"]
    assert "OAuth" not in result["error"] or "refusing" in result["error"] or "misconfigured" in result["error"]


def test_is_available_false_when_entry_missing(monkeypatch):
    monkeypatch.setattr(plugin, "_httpx_available", lambda: True)
    monkeypatch.setattr(plugin, "_named_entry", lambda: (_ for _ in ()).throw(RuntimeError("missing")))
    assert plugin.LiteLLMCodexImagesProvider().is_available() is False


def test_unknown_model_provider_fails_closed(monkeypatch):
    import agent.auxiliary_client as aux

    monkeypatch.setattr(aux, "_routes_to_custom_endpoint", lambda name: False)
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: None,
        raising=False,
    )
    # get_provider_profile is imported inside the helper; patch the providers module.
    import providers
    monkeypatch.setattr(providers, "get_provider_profile", lambda name: None)
    assert aux._unknown_model_provider("anthropic") is True
    assert aux._unknown_model_provider("openai-codex") is True
    assert aux._unknown_model_provider("custom") is False
    assert aux._unknown_model_provider("custom:litellm") is False
    assert aux._unknown_model_provider("auto") is False
