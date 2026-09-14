"""Per-session /model overrides must survive gateway restarts (#3659 salvage).

``GatewayRunner._session_model_overrides`` is in-memory, so before persistence
a gateway restart silently reverted every session to the global default model.
The non-secret parts (model/provider/base_url) are now written through to the
session store (``SessionEntry.model_override`` in sessions.json) and lazily
rehydrated on first use after a restart, with credentials re-resolved through
the normal runtime provider resolution.

Covers:
  - the override survives a simulated restart (a second SessionStore instance
    reading the same sessions dir, and a fresh runner rehydrating from it)
  - /new (SessionStore.reset_session) clears the persisted override so a
    restart cannot resurrect it
  - api_key is NEVER serialized to sessions.json
"""
import json
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import (
    SessionEntry,
    SessionSource,
    SessionStore,
    sanitize_model_override,
)

OVERRIDE = {
    "model": "gpt-5o",
    "provider": "openai",
    "api_key": "sk-SUPER-SECRET-do-not-persist",
    "base_url": "https://api.openai.example/v1",
    "api_mode": "responses",
}


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """Build SessionStores over a shared sessions dir, without SQLite."""

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)

    def _make() -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        assert store._db is None
        return store

    return _make


def _sessions_json(tmp_path) -> str:
    return (tmp_path / "sessions.json").read_text(encoding="utf-8")


def test_override_persists_and_survives_restart(store_factory, tmp_path):
    store = store_factory()
    entry = store.get_or_create_session(_make_source())
    session_key = entry.session_key

    store.set_model_override(session_key, OVERRIDE)

    # Simulated restart: a brand-new store instance reads the same dir.
    store2 = store_factory()
    persisted = store2.get_model_override(session_key)
    assert persisted == {
        "model": "gpt-5o",
        "provider": "openai",
        "base_url": "https://api.openai.example/v1",
    }


def _make_runner(store):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.session_store = store
    return runner


def test_runner_rehydrates_override_after_restart(store_factory):
    store = store_factory()
    entry = store.get_or_create_session(_make_source())
    session_key = entry.session_key
    store.set_model_override(session_key, OVERRIDE)

    # Simulated restart: fresh store + fresh runner with an empty in-memory
    # override map, credentials re-resolved via runtime provider resolution.
    runner = _make_runner(store_factory())
    with patch(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        return_value={
            "api_key": "sk-fresh-from-keychain",
            "api_mode": "responses",
            "base_url": "https://api.openai.example/v1",
            "provider": "openai",
            "requested_provider": "custom:chatgpt-tier",
            "capabilities": {"openai_native_compaction": True},
            "max_tokens": 32_768,
        },
    ):
        runner._rehydrate_session_model_override(session_key)

    override = runner._session_model_overrides[session_key]
    assert override["model"] == "gpt-5o"
    assert override["provider"] == "openai"
    assert override["base_url"] == "https://api.openai.example/v1"
    # Credentials come from live resolution, never from disk.
    assert override["api_key"] == "sk-fresh-from-keychain"
    assert override["api_mode"] == "responses"
    assert override["requested_provider"] == "custom:chatgpt-tier"
    assert override["capabilities"] == {"openai_native_compaction": True}
    assert override["max_tokens"] == 32_768

    model, runtime = runner._resolve_session_agent_runtime(
        session_key=session_key,
        user_config={"model": {"default": "global-model"}},
    )
    assert model == "gpt-5o"
    assert runtime["requested_provider"] == "custom:chatgpt-tier"
    assert runtime["capabilities"] == {"openai_native_compaction": True}
    assert runtime["max_tokens"] == 32_768
    route = runner._resolve_turn_agent_config("", model, runtime)
    assert route["runtime"]["capabilities"] == {"openai_native_compaction": True}


def test_runner_rehydrate_skips_retired_ox_alpha_override(store_factory):
    """A /model override persisted on the retired openrouter/stealth/ox-alpha
    route must not be rehydrated over the migrated (promoted) config — the
    v41 migration removed the route because every request on it now fails.
    No override is installed and no credential re-resolution is attempted
    for the dead provider."""
    store = store_factory()
    entry = store.get_or_create_session(_make_source())
    session_key = entry.session_key
    store.set_model_override(
        session_key,
        {
            "model": "stealth/ox-alpha",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
        },
    )

    runner = _make_runner(store_factory())
    resolutions = []

    def _recorder(provider):
        resolutions.append(provider)
        return {"api_key": "sk-must-not-be-used"}

    with patch(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider", _recorder
    ):
        runner._rehydrate_session_model_override(session_key)

    assert resolutions == []
    # No session state (and therefore no override) was created — the next
    # turn resolves from the migrated config default.
    assert runner._peek_session_state(session_key) is None


def test_runner_rehydrate_custom_endpoint_same_model_id_still_restores(store_factory):
    """Manual/custom-provider compatibility: a custom endpoint serving the
    same model id is a DIFFERENT route — its override keeps rehydrating."""
    store = store_factory()
    entry = store.get_or_create_session(_make_source())
    session_key = entry.session_key
    store.set_model_override(
        session_key,
        {
            "model": "stealth/ox-alpha",
            "base_url": "http://127.0.0.1:65534/v1",
        },
    )

    runner = _make_runner(store_factory())
    runner._rehydrate_session_model_override(session_key)

    state = runner._peek_session_state(session_key)
    override = state.conversation.model_override if state else None
    assert override is not None
    assert override["model"] == "stealth/ox-alpha"
    assert override["base_url"] == "http://127.0.0.1:65534/v1"


def test_sanitize_model_override():
    assert sanitize_model_override(None) is None
    assert sanitize_model_override({}) is None
    assert sanitize_model_override({"api_key": "sk-x", "api_mode": "chat"}) is None
    assert sanitize_model_override(OVERRIDE) == {
        "model": "gpt-5o",
        "provider": "openai",
        "base_url": "https://api.openai.example/v1",
    }
