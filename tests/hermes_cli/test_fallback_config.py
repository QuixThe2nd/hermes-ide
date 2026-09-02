"""Tests for hermes_cli/fallback_config.py — fallback entry API-key resolution
and the shared retired Ox Alpha route identity."""

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.fallback_config import is_retired_ox_alpha_route, resolve_entry_api_key


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"


    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None


    def test_whitespace_inline_key_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"api_key": "   ", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "env-key"

    def test_key_env_resolves_from_active_secret_scope_not_raw_env(self, monkeypatch):
        # Multiplexed gateway: os.environ holds another profile's key, but the
        # active per-turn secret scope holds this profile's key. The scoped
        # value must win — a raw os.getenv() would leak the other profile's
        # credential (issue #74311).
        monkeypatch.setenv("FB_KEY", "fake-other-profile-key")
        token = set_secret_scope({"FB_KEY": "fake-active-profile-key"})
        try:
            assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "fake-active-profile-key"
        finally:
            reset_secret_scope(token)

    def test_key_env_falls_back_to_env_when_no_active_scope(self, monkeypatch):
        # Non-multiplexed / single-profile behavior must be unchanged: with no
        # secret scope installed, resolution still reads os.environ.
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"


class TestIsRetiredOxAlphaRoute:
    """Exact retired-route identity shared by the v41 migration and every
    session-resume path. The exact route matches (however cased/spaced) and
    nothing else does — a custom provider or custom endpoint serving the
    same model id is a different, still-valid route."""

    def test_exact_openrouter_route_matches(self):
        assert is_retired_ox_alpha_route("openrouter", "stealth/ox-alpha")
        assert is_retired_ox_alpha_route(" OpenRouter ", " Stealth/OX-Alpha ")

    def test_bare_and_auto_provider_infer_openrouter(self):
        assert is_retired_ox_alpha_route(None, "stealth/ox-alpha")
        assert is_retired_ox_alpha_route("", "stealth/ox-alpha")
        assert is_retired_ox_alpha_route("auto", "stealth/ox-alpha")

    def test_other_model_ids_never_match(self):
        assert not is_retired_ox_alpha_route("openrouter", "openai/gpt-5.4")
        assert not is_retired_ox_alpha_route("openrouter", "ox-alpha-free")
        assert not is_retired_ox_alpha_route(None, "")
        assert not is_retired_ox_alpha_route("openrouter", None)

    def test_named_custom_provider_serving_same_model_id_is_not_retired(self):
        assert not is_retired_ox_alpha_route("my-gateway", "stealth/ox-alpha")
        assert not is_retired_ox_alpha_route("custom:local", "stealth/ox-alpha")

    def test_custom_base_url_keeps_bare_and_auto_routes_alive(self):
        # Runtime resolves provider auto/omitted + a custom base_url as a
        # custom endpoint BEFORE inferring OpenRouter — mirror that here.
        assert not is_retired_ox_alpha_route(
            "auto", "stealth/ox-alpha", "http://127.0.0.1:65534/v1"
        )
        assert not is_retired_ox_alpha_route(
            None, "stealth/ox-alpha", "http://127.0.0.1:65534/v1"
        )
        assert not is_retired_ox_alpha_route(
            "auto", "stealth/ox-alpha", "https://my-relay.example/v1"
        )

    def test_openrouter_base_url_keeps_the_inferred_route_retired(self):
        # Only the official endpoint (or a subdomain of it) keeps the
        # auto/omitted inference pointing at the retired route.
        assert is_retired_ox_alpha_route(
            "auto", "stealth/ox-alpha", "https://openrouter.ai/api/v1"
        )
        # Look-alike hosts must NOT keep it alive.
        assert not is_retired_ox_alpha_route(
            "auto", "stealth/ox-alpha", "https://openrouter.ai.evil.test/v1"
        )
        assert not is_retired_ox_alpha_route(
            "auto", "stealth/ox-alpha", "https://evil.test/openrouter.ai/v1"
        )
