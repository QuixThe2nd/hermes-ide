"""Boundary tests: _make_agent applies provider-scoped silent defaults.

When model config is absent, _resolve_model() returns the native grok-4.6
spelling before credentials pick a provider. _make_agent must re-apply
get_preferred_silent_default_model(resolved_provider) after runtime resolution
so OpenRouter-only installs construct x-ai/grok-4.6 while xai-oauth keeps
grok-4.6. Explicit env/config and session overrides stay exact.
"""

from __future__ import annotations

import base64
import json
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home
from tui_gateway import server


def _reset_cfg_cache() -> None:
    server._cfg_cache = None
    server._cfg_mtime = None
    server._cfg_path = None


def _no_catalog_cache(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.model_catalog.get_default_model_from_cache",
        lambda provider: None,
    )


def _clear_model_env(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)


def _minimal_cfg() -> dict:
    return {"agent": {"system_prompt": "test"}}


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = {"exp": exp_epoch}
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
        .rstrip(b"=")
        .decode("utf-8")
    )
    return f"h.{encoded}.s"


def _setup_xai_oauth_auth(hermes_home: Path, *, access_token: str) -> Path:
    """Write xAI OAuth tokens into the Hermes auth store at the given root."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    state = {
        "tokens": {
            "access_token": access_token,
            "refresh_token": "refresh",
            "id_token": "",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        "last_refresh": "2026-05-14T00:00:00Z",
        "auth_mode": "oauth_pkce",
    }
    auth_store = {
        "version": 1,
        "active_provider": "xai-oauth",
        "providers": {"xai-oauth": state},
    }
    auth_file = hermes_home / "auth.json"
    auth_file.write_text(json.dumps(auth_store, indent=2))
    return auth_file


def _call_make_agent(cfg=None, **make_agent_kwargs):
    if cfg is None:
        cfg = _minimal_cfg()
    _reset_cfg_cache()
    with ExitStack() as stack:
        stack.enter_context(patch("tui_gateway.server._load_cfg", return_value=cfg))
        stack.enter_context(patch("tui_gateway.server._get_db", return_value=MagicMock()))
        stack.enter_context(
            patch("tui_gateway.server._load_tool_progress_mode", return_value="compact")
        )
        stack.enter_context(
            patch("tui_gateway.server._load_reasoning_config", return_value=None)
        )
        stack.enter_context(
            patch("tui_gateway.server._load_service_tier", return_value=None)
        )
        stack.enter_context(
            patch("tui_gateway.server._load_enabled_toolsets", return_value=None)
        )
        mock_agent = stack.enter_context(patch("run_agent.AIAgent"))
        server._make_agent("sid-1", "key-1", **make_agent_kwargs)
        return mock_agent.call_args.kwargs


def test_openrouter_only_install_uses_vendor_prefixed_silent_default(
    monkeypatch,
):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    kwargs = _call_make_agent()
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "x-ai/grok-4.6"
    assert kwargs["api_key"] == "sk-or-test-key"


def test_xai_oauth_only_install_keeps_native_silent_default(monkeypatch):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Must outlive XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS (3600s); now+3600
    # still triggers proactive refresh and hits the network.
    fresh_token = _jwt_with_exp(int(time.time()) + 86400)
    _setup_xai_oauth_auth(get_hermes_home(), access_token=fresh_token)
    kwargs = _call_make_agent()
    assert kwargs["provider"] == "xai-oauth"
    assert kwargs["model"] == "grok-4.6"
    assert kwargs["api_key"] == fresh_token


def test_explicit_config_model_stays_exact(monkeypatch):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    cfg = {
        "model": {"default": "qwen/qwen3.8-max"},
        "agent": {"system_prompt": "test"},
    }
    kwargs = _call_make_agent(cfg)
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "qwen/qwen3.8-max"


def test_explicit_env_model_stays_exact(monkeypatch):
    # Use a model id static detection won't remap to another provider; only
    # OPENROUTER_API_KEY is present so runtime must land on OpenRouter.
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("HERMES_MODEL", "my-explicit-openrouter-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    kwargs = _call_make_agent()
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "my-explicit-openrouter-model"


def test_make_agent_model_override_stays_exact(monkeypatch):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    kwargs = _call_make_agent(
        model_override="anthropic/claude-sonnet-4.6",
        provider_override="openrouter",
    )
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "anthropic/claude-sonnet-4.6"


def test_explicit_config_provider_and_model_stay_exact(monkeypatch):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    cfg = {
        "model": {"default": "openai/gpt-5.5", "provider": "openrouter"},
        "agent": {"system_prompt": "test"},
    }
    kwargs = _call_make_agent(cfg)
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "openai/gpt-5.5"


def _persist_session_row(session: dict) -> dict:
    """Run _ensure_session_db_row and return the persisted row dict."""
    created: list[dict] = []

    class _FakeDB:
        def create_session(
            self,
            key,
            source=None,
            model=None,
            model_config=None,
            parent_session_id=None,
            cwd=None,
            profile_name=None,
        ):
            created.append({"model": model, "model_config": model_config})

    with patch.object(server, "_get_db", return_value=_FakeDB()):
        server._ensure_session_db_row(session)
    assert len(created) == 1
    return created[0]


def _resume_make_agent_kwargs(row: dict, cfg=None, monkeypatch=None) -> dict:
    """Build _make_agent kwargs from a persisted row (persist → resume path)."""
    overrides = server._stored_session_runtime_overrides(
        {"model": row["model"], "model_config": row["model_config"]}
    )
    if monkeypatch is not None:
        _clear_model_env(monkeypatch)
        _no_catalog_cache(monkeypatch)
    return _call_make_agent(cfg, **overrides)


def test_openrouter_persist_resume_uses_vendor_prefixed_silent_default(
    monkeypatch,
):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")

    row = _persist_session_row({"session_key": "k1", "model_override": None})
    assert row["model"] == "x-ai/grok-4.6"
    assert row["model_config"] is None

    kwargs = _resume_make_agent_kwargs(row, monkeypatch=monkeypatch)
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "x-ai/grok-4.6"


def test_openrouter_first_construct_matches_persist_resume(monkeypatch):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")

    first = _call_make_agent()
    row = _persist_session_row({"session_key": "k1", "model_override": None})
    resumed = _resume_make_agent_kwargs(row, monkeypatch=monkeypatch)

    assert first["provider"] == resumed["provider"] == "openrouter"
    assert first["model"] == resumed["model"] == "x-ai/grok-4.6"


def test_xai_oauth_persist_resume_keeps_native_silent_default(monkeypatch):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    fresh_token = _jwt_with_exp(int(time.time()) + 86400)
    _setup_xai_oauth_auth(get_hermes_home(), access_token=fresh_token)

    row = _persist_session_row({"session_key": "k1", "model_override": None})
    assert row["model"] == "grok-4.6"
    assert row["model_config"] is None

    kwargs = _resume_make_agent_kwargs(row, monkeypatch=monkeypatch)
    assert kwargs["provider"] == "xai-oauth"
    assert kwargs["model"] == "grok-4.6"


def test_legacy_native_silent_default_row_resume_remaps_for_openrouter(
    monkeypatch,
):
    """Rows persisted before the fix (native grok-4.6, empty model_config)."""
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")

    legacy_row = {"model": "grok-4.6", "model_config": None}
    kwargs = _resume_make_agent_kwargs(legacy_row, monkeypatch=monkeypatch)
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "x-ai/grok-4.6"


def test_explicit_composer_override_persist_resume_stays_exact(monkeypatch):
    _clear_model_env(monkeypatch)
    _no_catalog_cache(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")

    session = {
        "session_key": "k1",
        "model_override": {
            "model": "openai/gpt-5.5",
            "provider": "openrouter",
        },
    }
    row = _persist_session_row(session)
    assert row["model"] == "openai/gpt-5.5"
    assert row["model_config"]["provider"] == "openrouter"

    kwargs = _resume_make_agent_kwargs(row, monkeypatch=monkeypatch)
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "openai/gpt-5.5"
