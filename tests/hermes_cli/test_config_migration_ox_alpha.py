"""Focused regression tests for the v41 Ox Alpha retirement migration.

``_migrate_to_41`` strips the retired ``openrouter/stealth/ox-alpha`` route
from existing configs: retired fallback entries are removed from both
fallback shapes, a retired primary promotes the first surviving fallback in
effective chain order (``fallback_providers``, then legacy
``fallback_model``, per ``hermes_cli.fallback_config.get_fallback_chain``),
and a retired primary with no survivor is unset with a warning to run
``hermes model``. A same-model-id route under a custom provider is a
different route and must survive untouched.

The tests drive ``run_migrations(40, ...)`` directly so ONLY the v41 step
runs (the driver's separate version stamp stays out of the picture), the
same pattern TestCustomProviderCompatibility uses for floor-refused steps.
"""

import copy
import os
import textwrap
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.config import DEFAULT_CONFIG, load_config


def _write_config(tmp_path, data):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return config_path


def _run_v41(quiet=True):
    from hermes_cli.config_migrations import run_migrations

    results = {"env_added": [], "config_added": [], "warnings": []}
    run_migrations(40, results, quiet=quiet)
    return results


def _read_config(config_path):
    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


class TestOxAlphaV41Registry:
    def test_registry_and_default_version_include_41(self):
        from hermes_cli.config_migrations import (
            MIGRATIONS,
            _migrate_to_41,
        )

        assert DEFAULT_CONFIG["_config_version"] == 41
        assert MIGRATIONS[-1] == (41, _migrate_to_41)
        # Strictly ascending registry stays intact.
        versions = [target for target, _ in MIGRATIONS]
        assert versions == sorted(versions)


class TestOxAlphaPrimaryPromotion:
    """Retired primary promotes the first surviving fallback in chain order."""

    def test_modern_fallback_promotion(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "nous", "model": "kimi-k3"},
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "kimi-k3", "provider": "nous"}
        # The promoted occurrence left the chain; the survivor keeps its
        # relative order.
        assert raw["fallback_providers"] == [{"provider": "zai", "model": "glm-4.7"}]
        assert any("kimi-k3" in entry for entry in results["config_added"])
        assert results["warnings"] == []

    def test_legacy_dict_fallback_promotion(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                # Bare string primary: the same inferred OpenRouter route.
                "model": "stealth/ox-alpha",
                "fallback_model": {"provider": "zai", "model": "glm-4.7"},
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        # The single legacy entry graduated — the key is gone, not an
        # empty dict.
        assert "fallback_model" not in raw

    def test_legacy_list_fallback_promotion_keeps_rest(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_model": [
                    {"provider": "kimi-coding", "model": "kimi-k3"},
                    {"provider": "minimax", "model": "abab7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "kimi-k3", "provider": "kimi-coding"}
        assert raw["fallback_model"] == [{"provider": "minimax", "model": "abab7"}]

    def test_modern_chain_wins_over_legacy(self, tmp_path):
        """Effective order is fallback_providers first, then fallback_model."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha"},
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
                "fallback_model": {"provider": "kimi-coding", "model": "kimi-k3"},
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        # The promotee left fallback_providers; the unrelated legacy entry
        # survives as the remaining fallback.
        assert raw.get("fallback_providers", []) == []
        assert raw["fallback_model"] == {"provider": "kimi-coding", "model": "kimi-k3"}

    def test_promoted_route_not_duplicated_across_collections(self, tmp_path):
        """The graduate leaves every fallback collection it appears in.

        The same route listed under both fallback_providers and legacy
        fallback_model would otherwise stay primary AND a fallback —
        get_fallback_chain only dedups among fallbacks, not against the
        primary slot.
        """
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
                "fallback_model": {"provider": "zai", "model": "glm-4.7"},
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []
        assert "fallback_model" not in raw

    def test_promotion_carries_route_metadata(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "custom",
                        "model": "my-model",
                        "base_url": "http://localhost:8000/v1",
                        "api_mode": "anthropic",
                        "api_key": "sk-local",
                        "key_env": "MY_GATEWAY_KEY",
                        "reasoning_echo": True,
                        "priority": 7,  # fallback-only field: not carried
                    },
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {
            "default": "my-model",
            "provider": "custom",
            "base_url": "http://localhost:8000/v1",
            "api_mode": "anthropic",
            "api_key": "sk-local",
            "key_env": "MY_GATEWAY_KEY",
            "reasoning_echo": True,
        }
        assert raw.get("fallback_providers", []) == []


class TestOxAlphaPrimaryUnset:
    def test_no_surviving_fallback_unsets_model_and_warns(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    # Only fallback is the retired route itself.
                    {"provider": "openrouter", "model": "stealth/ox-alpha"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        # The route is unset (canonical default → the DEFAULT_CONFIG empty
        # value) and the route-owned keys are gone — the section itself
        # survives retirement.
        assert raw["model"] == {"default": ""}
        assert raw.get("fallback_providers", []) == []
        assert len(results["warnings"]) == 1
        assert "hermes model" in results["warnings"][0]
        assert "ox-alpha" in results["warnings"][0]
        # Nothing was invented as a replacement.
        assert results["config_added"] == [
            "removed retired 'openrouter/stealth/ox-alpha' fallback route(s)"
        ]

    def test_warning_prints_only_when_not_quiet(self, tmp_path, capsys):
        _write_config(
            tmp_path,
            {"_config_version": 40, "model": "stealth/ox-alpha"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41(quiet=False)
        assert "hermes model" in capsys.readouterr().out

        _write_config(
            tmp_path,
            {"_config_version": 40, "model": "stealth/ox-alpha"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41(quiet=True)
        assert capsys.readouterr().out == ""


class TestOxAlphaFallbackRemoval:
    def test_retired_entries_removed_from_both_shapes(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                # An openrouter primary on a DIFFERENT model is not retired.
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "nous", "model": "kimi-k3"},
                    {"provider": "openrouter", "model": "stealth/ox-alpha"},
                    {"provider": "zai", "model": "glm-4.7"},
                ],
                "fallback_model": [
                    {"provider": "openrouter", "model": "stealth/ox-alpha"},
                    {"provider": "kimi-coding", "model": "kimi-k3"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        # Primary preserved byte-for-value; unrelated fallbacks keep order.
        assert raw["model"] == {"default": "openai/gpt-5.4", "provider": "openrouter"}
        assert raw["fallback_providers"] == [
            {"provider": "nous", "model": "kimi-k3"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        assert raw["fallback_model"] == [{"provider": "kimi-coding", "model": "kimi-k3"}]
        assert any("removed retired" in entry for entry in results["config_added"])
        assert results["warnings"] == []

    def test_retired_legacy_dict_shape_drops_key(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_model": {"provider": "openrouter", "model": "stealth/ox-alpha"},
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert "fallback_model" not in raw
        assert raw["model"] == {"default": "openai/gpt-5.4", "provider": "openrouter"}

    def test_mixed_case_and_whitespace_match(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": " Stealth/OX-Alpha ", "provider": " OpenRouter "},
                "fallback_providers": [
                    {"provider": "OPENROUTER", "model": "STEALTH/ox-alpha"},
                    {"provider": "nous", "model": "kimi-k3"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "kimi-k3", "provider": "nous"}
        assert raw.get("fallback_providers", []) == []

    def test_unrelated_config_retained(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
                "providers": {
                    "my-gateway": {"name": "My Gateway", "api": "http://x/v1"}
                },
                "plugins": {"enabled": ["quota_channels"]},
                "display": {"personality": ""},
                "agent": {"max_turns": 100},
                "custom_number": 7,
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["providers"] == {"my-gateway": {"name": "My Gateway", "api": "http://x/v1"}}
        assert raw["plugins"] == {"enabled": ["quota_channels"]}
        assert raw["display"] == {"personality": ""}
        assert raw["agent"] == {"max_turns": 100}
        assert raw["custom_number"] == 7


class TestOxAlphaPromotionPreservesModelSettings:
    """Promotion replaces route-owned keys only; unrelated model controls
    survive the retirement upgrade (P1 review: streaming/max_tokens/… were
    being deleted with the rebuilt dict)."""

    def test_non_route_fields_preserved_and_route_fields_replaced(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "openrouter",
                    # Route-owned fields of the RETIRED route: replaced by the
                    # promotee's (or dropped when it carries none). The
                    # endpoint is the OFFICIAL OpenRouter host — a custom
                    # endpoint would be a user-owned route that survives.
                    "base_url": "https://openrouter.ai/api/v1",
                    "key_env": "RETIRED_ROUTE_KEY",
                    "api_mode": "anthropic_messages",
                    # Non-route model-level controls: must survive verbatim.
                    "streaming": False,
                    "max_tokens": 1234,
                    "context_length": 200000,
                    "default_headers": {"X-Custom": "yes"},
                    "lmstudio_load_mode": "always",
                    "openai_runtime": "codex_app_server",
                },
                "fallback_providers": [
                    {
                        "provider": "custom",
                        "model": "my-model",
                        "base_url": "http://localhost:8000/v1",
                        "key_env": "MY_GATEWAY_KEY",
                    },
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {
            "default": "my-model",
            "provider": "custom",
            "base_url": "http://localhost:8000/v1",
            "key_env": "MY_GATEWAY_KEY",
            "streaming": False,
            "max_tokens": 1234,
            "context_length": 200000,
            "default_headers": {"X-Custom": "yes"},
            "lmstudio_load_mode": "always",
            "openai_runtime": "codex_app_server",
        }
        assert raw.get("fallback_providers", []) == []

    def test_string_primary_promotes_into_minimal_dict(self, tmp_path):
        """A bare-string primary has no settings to preserve — fresh dict."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "fallback_providers": [
                    {"provider": "zai", "model": "glm-4.7", "priority": 3},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}

    def test_promotee_without_route_fields_drops_retired_route_fields(self, tmp_path):
        """A plain provider promotee must not inherit the retired route's
        endpoint/credential reference — those keys describe the OLD route."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "openrouter",
                    # Official OpenRouter endpoint: still the retired route.
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "sk-retired",
                    "api_mode": "anthropic_messages",
                },
                "fallback_providers": [{"provider": "nous", "model": "kimi-k3"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "kimi-k3", "provider": "nous"}


class TestOxAlphaNestedDefault:
    """The supported nested ``model.default: {provider: ..., model: ...}``
    shape is flattened by ``split_model_config_default`` at load time — the
    migration must recognize it through the SAME splitter, or v41 gets
    stamped while the dead route stays active (P2 review)."""

    def test_nested_openrouter_default_is_scrubbed_and_promotes(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "openrouter", "model": "stealth/ox-alpha"},
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        # Flattened to the canonical string default — never left nested.
        assert raw["model"]["default"] == "glm-4.7"
        assert raw["model"]["provider"] == "zai"
        assert raw.get("fallback_providers", []) == []
        assert any("promoted from fallback" in entry for entry in results["config_added"])

    def test_nested_default_without_survivor_unsets_with_warning(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": {"provider": "openrouter", "model": "stealth/ox-alpha"}},
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        # Route unset, section retained (canonical ``default`` only — the
        # nested shape had no other keys to preserve).
        assert raw["model"] == {"default": ""}
        assert len(results["warnings"]) == 1
        assert "hermes model" in results["warnings"][0]

    def test_nested_default_preserves_sibling_model_settings(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "openrouter", "model": "stealth/ox-alpha"},
                    "streaming": True,
                    "context_length": 999999,
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"]["default"] == "glm-4.7"
        assert raw["model"]["provider"] == "zai"
        assert raw["model"]["streaming"] is True
        assert raw["model"]["context_length"] == 999999

    def test_nested_bare_model_id_under_auto_outer_provider(self, tmp_path):
        """Nested ``{model: stealth/ox-alpha}`` with an outer auto provider —
        the same inferred OpenRouter route an explicit nested provider pins."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": {"model": "stealth/ox-alpha"},
                    "provider": "auto",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"]["default"] == "glm-4.7"

    def test_nested_unrelated_model_untouched(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {"default": {"provider": "zai", "model": "glm-5.3"}},
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}


class TestOxAlphaCustomEndpointPrimary:
    """``provider: auto`` (or omitted) + a custom ``base_url`` resolves as a
    custom endpoint at runtime BEFORE any OpenRouter inference, so v41 must
    preserve it — retiring it would break a working manual route
    (fresh-verifier blocker)."""

    def test_auto_provider_custom_base_url_preserved(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "auto",
                    "base_url": "http://127.0.0.1:65534/v1",
                    "api_mode": "chat_completions",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_omitted_provider_custom_base_url_preserved(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "base_url": "http://127.0.0.1:65534/v1",
                    "api_mode": "chat_completions",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_openrouter_base_url_with_auto_provider_still_retires(self, tmp_path):
        """A base_url ON openrouter.ai keeps the inferred OpenRouter route —
        the runtime's host gate does not treat the official endpoint as
        custom, so neither does the matcher."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "auto",
                    "base_url": "https://openrouter.ai/api/v1",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}

    def test_explicit_openrouter_without_base_url_retires(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}

    def test_bare_auto_primary_without_base_url_retires(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "auto"},
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}


class TestOxAlphaNotRetired:
    def test_same_model_id_under_custom_provider_preserved(self, tmp_path):
        """A custom provider serving the same model id is a different route."""
        original = textwrap.dedent(
            """\
            _config_version: 40
            model:
              default: stealth/ox-alpha
              provider: my-gateway
            fallback_providers:
            - provider: my-gateway
              model: stealth/ox-alpha
            - provider: openrouter
              model: stealth/ox-alpha
            """
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        # The custom-provider route survives everywhere it appeared…
        assert raw["model"] == {"default": "stealth/ox-alpha", "provider": "my-gateway"}
        assert raw["fallback_providers"] == [
            {"provider": "my-gateway", "model": "stealth/ox-alpha"}
        ]
        # …while the openrouter occurrence is still removed.
        assert results["config_added"] == [
            "removed retired 'openrouter/stealth/ox-alpha' fallback route(s)"
        ]

    def test_provider_auto_infers_openrouter_route(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "auto"},
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}

    def test_unrelated_openrouter_model_untouched(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {"default": "anthropic/claude-fable-5", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "openrouter", "model": "openai/gpt-5.4"}
                ],
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        # No retired route: no config-content change at all (not even a
        # rewrite), and nothing reported.
        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}


class TestOxAlphaNestedDefaultProviderPrecedence:
    """An explicit outer ``model.provider`` beats the nested default's
    provider — the precedence ``_normalize_root_model_keys`` applies when it
    flattens. Letting the nested one win retired manual outer routes
    (fix-2 review: the config loads as the outer route, so v41 must leave it
    alone)."""

    def test_explicit_outer_custom_provider_wins(self, tmp_path):
        """The literal fix-2 repro: nested openrouter default + explicit
        outer ``custom:local`` loads as the outer custom route."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "openrouter", "model": "stealth/ox-alpha"},
                    "provider": "custom:local",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        # The manual route survives untouched — no promotion, no unset.
        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_explicit_outer_custom_provider_with_endpoint_wins(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "openrouter", "model": "stealth/ox-alpha"},
                    "provider": "custom",
                    "base_url": "http://127.0.0.1:65534/v1",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_auto_outer_provider_lets_nested_openrouter_win(self, tmp_path):
        """Opposite direction, locking canonical behavior: an ``auto`` outer
        provider is exactly what the nested provider overrides at load time,
        so the route IS retired and promotes the first survivor."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "openrouter", "model": "stealth/ox-alpha"},
                    "provider": "auto",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []

    def test_padded_uppercase_auto_outer_preserves_nested_custom(self, tmp_path):
        """``auto`` is matched case-insensitively after whitespace trim — the
        normalization every route consumer applies — so a padded ``" AUTO "``
        outer provider is the merged default, not an explicit one. Gating
        case-sensitively handed the classifier an ``auto`` it resolved by
        inference, scrubbing the manual custom route it protects. The route
        still survives (no promotion, no unset); the variant sentinel is
        canonicalized (fix-3) so the persisted config no longer carries the
        spelling the load-time flattener treats as an explicit provider."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "my-gateway", "model": "stealth/ox-alpha"},
                    "provider": " AUTO ",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()
            loaded = load_config()

        raw = _read_config(config_path)
        # The nested custom route survives — no promotion, no unset — with
        # the sentinel canonicalized away (see
        # TestOxAlphaLegacyAutoSentinelCanonicalization for the mechanism).
        assert raw["model"]["provider"] == "my-gateway"
        assert raw["model"]["default"] == "stealth/ox-alpha"
        assert raw["fallback_providers"] == [{"provider": "zai", "model": "glm-4.7"}]
        assert results["warnings"] == []
        assert any("canonicalized" in entry for entry in results["config_added"])
        assert loaded["model"]["provider"] == "my-gateway"

    def test_outer_openrouter_provider_with_nested_custom_wins(self, tmp_path):
        """The precedence flips both ways: an explicit outer ``openrouter``
        retires the route even when the nested default names a custom
        provider serving the same model id."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "my-gateway", "model": "stealth/ox-alpha"},
                    "provider": "openrouter",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []


class TestOxAlphaApiBaseAliasEndpoint:
    """``model.api_base`` is the endpoint alias normalized to ``base_url`` at
    the load/save chokepoint (issue #8919) — fallback-only, never overriding
    an explicit ``base_url``. v41 must classify through the same alias, or a
    custom endpoint named the intuitive way is destructively retired
    (fix-2 review), for the primary AND fallback routes."""

    def test_auto_provider_custom_api_base_preserved(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "auto",
                    "api_base": "http://127.0.0.1:65534/v1",
                    "api_mode": "chat_completions",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_omitted_provider_custom_api_base_preserved(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "api_base": "http://127.0.0.1:65534/v1",
                    "api_mode": "chat_completions",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_explicit_base_url_wins_over_api_base_alias(self, tmp_path):
        """The alias never overrides an explicit ``base_url`` — here the
        explicit custom endpoint keeps the route alive even though the
        alias points at OpenRouter."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "auto",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_base": "https://openrouter.ai/api/v1",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_openrouter_api_base_with_auto_provider_still_retires(self, tmp_path):
        """An alias ON openrouter.ai keeps the inferred OpenRouter route —
        the alias is classified exactly as a literal ``base_url`` would be."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "auto",
                    "api_base": "https://openrouter.ai/api/v1",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}

    def test_fallback_custom_api_base_preserved(self, tmp_path):
        """A fallback entry's custom endpoint named via the alias survives,
        and unrelated entries keep their order/shape around it."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "nous", "model": "kimi-k3"},
                    {
                        "provider": "auto",
                        "model": "stealth/ox-alpha",
                        "api_base": "http://127.0.0.1:8080/v1",
                    },
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}


class TestOxAlphaLegacyAutoSentinelCanonicalization:
    """Legacy outer auto sentinel + nested custom provider (fix-3 review).

    The load-time flattener lets a nested default's provider win only over
    an absent outer provider or the EXACT string ``auto`` — so a padded or
    case-variant sentinel like ``" AUTO "`` blocked the nested custom
    provider, runtime resolution normalized the sentinel to auto, inferred
    OpenRouter for the retired vendor-namespaced id, and routed into the
    dead model. v41 canonicalizes the sentinel BEFORE classification; it
    never replaces the outer provider with the nested one and never touches
    the loader.

    Persistence note: the migration hands ``provider: auto`` + the nested
    dict to the standard write chokepoint, whose normalization graduates
    that shape to the canonical flat form (``default: stealth/ox-alpha`` +
    ``provider: my-gateway``) — the same form any other save produces and
    the exact route the loader must resolve.
    """

    @staticmethod
    def _shape(outer):
        return {
            "_config_version": 40,
            "model": {
                "default": {"provider": "my-gateway", "model": "stealth/ox-alpha"},
                "provider": outer,
            },
            "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
        }

    def test_canonicalization_sets_auto_and_keeps_nested_shape(self):
        """The helper itself (pre-persistence): exact ``auto``, never the
        nested provider, nested default left for the loader to flatten."""
        from hermes_cli.config_migrations import _canonicalize_outer_auto_sentinel

        config = self._shape(" AUTO ")
        assert _canonicalize_outer_auto_sentinel(config) is True
        assert config["model"]["provider"] == "auto"
        assert config["model"]["default"] == {
            "provider": "my-gateway",
            "model": "stealth/ox-alpha",
        }

    @pytest.mark.parametrize("outer", [" AUTO ", "Auto", "aUto"])
    def test_variant_sentinel_fixed_and_nested_custom_route_loads(self, tmp_path, outer):
        config_path = _write_config(tmp_path, self._shape(outer))
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()
            loaded = load_config()

        raw = _read_config(config_path)
        # The variant sentinel is gone from the persisted file and the route
        # is the nested custom one — never the retired inference, and the
        # fallback was not promoted.
        assert raw["model"]["provider"] == "my-gateway"
        assert raw["model"]["default"] == "stealth/ox-alpha"
        assert raw["fallback_providers"] == [{"provider": "zai", "model": "glm-4.7"}]
        assert results["warnings"] == []
        assert any("canonicalized" in entry for entry in results["config_added"])
        assert loaded["model"]["default"] == "stealth/ox-alpha"
        assert loaded["model"]["provider"] == "my-gateway"

    def test_canonical_lowercase_auto_untouched_and_loads_nested(self, tmp_path):
        """Already the exact spelling: nothing to rewrite (byte-for-byte
        untouched, nothing reported) and the raw file keeps the nested shape
        with the ``auto`` outer sentinel — loading as the nested custom
        route."""
        original = yaml.safe_dump(self._shape("auto"))
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()
            loaded = load_config()

        raw = _read_config(config_path)
        assert config_path.read_text(encoding="utf-8") == original
        assert raw["model"]["provider"] == "auto"
        assert raw["model"]["default"] == {
            "provider": "my-gateway",
            "model": "stealth/ox-alpha",
        }
        assert results == {"env_added": [], "config_added": [], "warnings": []}
        assert loaded["model"]["default"] == "stealth/ox-alpha"
        assert loaded["model"]["provider"] == "my-gateway"

    @pytest.mark.parametrize(
        "nested",
        [
            # Provider omitted: the same inferred OpenRouter route the outer
            # sentinel alone names.
            {"model": "stealth/ox-alpha"},
            # auto is the merged default, not a custom provider.
            {"provider": "auto", "model": "stealth/ox-alpha"},
            # OpenRouter is the retired route itself, not a custom provider.
            {"provider": "openrouter", "model": "stealth/ox-alpha"},
        ],
    )
    def test_without_nested_custom_provider_retired_route_still_scrubs(self, tmp_path, nested):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": nested, "provider": " AUTO "},
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        # The variant sentinel must not shield the inferred/explicit retired
        # route: the first survivor is promoted as usual.
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []

    def test_non_retired_model_scope_guard(self, tmp_path):
        """The canonicalization exists to protect the retired-model nested
        shape only — a variant sentinel on any other model id is left
        byte-for-byte alone."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "my-gateway", "model": "glm-5.3"},
                    "provider": " AUTO ",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_canonicalization_is_idempotent(self, tmp_path):
        config_path = _write_config(tmp_path, self._shape(" AUTO "))
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            after_first = _read_config(config_path)
            second = _run_v41()
            after_second = _read_config(config_path)

        assert after_first == after_second
        assert any("canonicalized" in entry for entry in first["config_added"])
        assert second == {"env_added": [], "config_added": [], "warnings": []}


class TestOxAlphaPromotionEndpointAlias:
    """Promotion must carry the promotee's effective endpoint with the same
    alias-aware precedence retirement classification uses (fix-3 review): a
    surviving fallback named its custom endpoint ``api_base``, promotion
    dropped it, and the promoted primary loaded as provider ``auto`` with no
    endpoint — the inferred, dead OpenRouter route."""

    def test_api_base_promotee_promotes_with_custom_endpoint(self, tmp_path):
        from hermes_cli.config_migrations import _primary_routes_retired_model

        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "auto",
                        "model": "stealth/ox-alpha",
                        "api_base": "http://127.0.0.1:8080/v1",
                    },
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()
            loaded = load_config()
            second = _run_v41()

        raw = _read_config(config_path)
        # Custom endpoint persisted under the canonical base_url — no
        # duplicate api_base alias key on the promoted primary.
        assert raw["model"] == {
            "default": "stealth/ox-alpha",
            "provider": "auto",
            "base_url": "http://127.0.0.1:8080/v1",
        }
        # The later, normal fallback remains the fallback.
        assert raw["fallback_providers"] == [{"provider": "zai", "model": "glm-4.7"}]
        # The promoted custom-endpoint route is not the retired one, and a
        # rerun is a no-op (idempotent).
        assert _primary_routes_retired_model(raw) is False
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert _read_config(config_path)["model"] == raw["model"]
        assert loaded["model"]["base_url"] == "http://127.0.0.1:8080/v1"
        assert loaded["model"]["default"] == "stealth/ox-alpha"

    def test_non_empty_base_url_wins_over_api_base_alias(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "auto",
                        "model": "stealth/ox-alpha",
                        "base_url": "http://127.0.0.1:1111/v1",
                        "api_base": "http://127.0.0.1:2222/v1",
                    },
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {
            "default": "stealth/ox-alpha",
            "provider": "auto",
            "base_url": "http://127.0.0.1:1111/v1",
        }
        assert raw.get("fallback_providers", []) == []

    def test_empty_base_url_falls_back_to_api_base_alias(self, tmp_path):
        """``_endpoint_url`` precedence: the alias applies only when the
        canonical key carries no non-empty value."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "auto",
                        "model": "stealth/ox-alpha",
                        "base_url": "",
                        "api_base": "http://127.0.0.1:3333/v1",
                    },
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {
            "default": "stealth/ox-alpha",
            "provider": "auto",
            "base_url": "http://127.0.0.1:3333/v1",
        }
        assert raw.get("fallback_providers", []) == []


class TestOxAlphaAutoResolvedFallbackScrub:
    """A fallback with provider ``auto`` (or omitted) + the retired model id
    resolves to OpenRouter at runtime and must be scrubbed through the same
    route-aware predicate the primary uses — literal provider matching left
    those entries in place (fix-2 review)."""

    def test_auto_provider_fallback_removed_modern(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "nous", "model": "kimi-k3"},
                    {"provider": "auto", "model": "stealth/ox-alpha"},
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        # Unrelated entries keep their relative order; primary preserved.
        assert raw["model"] == {"default": "openai/gpt-5.4", "provider": "openrouter"}
        assert raw["fallback_providers"] == [
            {"provider": "nous", "model": "kimi-k3"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        assert results["config_added"] == [
            "removed retired 'openrouter/stealth/ox-alpha' fallback route(s)"
        ]

    def test_omitted_provider_fallback_removed_legacy_list(self, tmp_path):
        """A legacy list entry without a provider never reaches
        get_fallback_chain, but the raw collection is still filtered."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_model": [
                    {"model": "stealth/ox-alpha"},
                    {"provider": "kimi-coding", "model": "kimi-k3"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["fallback_model"] == [
            {"provider": "kimi-coding", "model": "kimi-k3"}
        ]
        assert raw["model"] == {"default": "openai/gpt-5.4", "provider": "openrouter"}

    def test_auto_provider_legacy_dict_drops_key(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_model": {"provider": "auto", "model": "stealth/ox-alpha"},
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        # Single matched legacy entry — the key is gone, not an empty dict.
        assert "fallback_model" not in raw
        assert raw["model"] == {"default": "openai/gpt-5.4", "provider": "openrouter"}

    def test_auto_fallback_custom_base_url_preserved(self, tmp_path):
        """The same auto/omitted model with a custom endpoint is a different,
        still-working route and survives the scrub."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "auto",
                        "model": "stealth/ox-alpha",
                        "base_url": "http://127.0.0.1:8080/v1",
                    },
                ],
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}

    def test_auto_fallback_is_not_promoted_over_later_survivor(self, tmp_path):
        """With a retired primary, the auto-resolved entry is scrubbed from
        the chain too — promotion falls to the next real survivor."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "auto", "model": "stealth/ox-alpha"},
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []


class TestOxAlphaLegacyRootShapeRoute:
    """Legacy root-level route fields (fix-4 review, defect 1).

    Normal loading folds a root ``provider:`` / ``base_url:`` / ``api_base:``
    into the model section (``_normalize_root_model_keys``) — and the runtime
    resolves the folded endpoint as a custom route BEFORE any OpenRouter
    inference. Classification used to read ``config["model"]`` alone, so a
    supported legacy shape like ``model: stealth/ox-alpha`` + root
    ``provider: custom:local`` was mistaken for the inferred OpenRouter route:
    the custom route was un-set and a fallback promoted over it. The
    classifier must normalize the FULL raw config with the loader's own
    semantics — without rewriting the legacy shape it merely recognizes.
    """

    def test_root_custom_provider_survives_without_fallback_promotion(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "provider": "custom:local",
                "base_url": "http://127.0.0.1:9000/v1",
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            loaded = load_config()
            after_first = _read_config(config_path)
            second = _run_v41()
            after_second = _read_config(config_path)

        # The legacy shape is preserved verbatim — the custom route is never
        # destructively normalized, and the fallback is NOT promoted over it.
        assert after_first == {
            "_config_version": 40,
            "model": "stealth/ox-alpha",
            "provider": "custom:local",
            "base_url": "http://127.0.0.1:9000/v1",
            "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
        }
        assert first == {"env_added": [], "config_added": [], "warnings": []}
        # What the file loads as is the custom route, not the dead inference.
        assert loaded["model"]["default"] == "stealth/ox-alpha"
        assert loaded["model"]["provider"] == "custom:local"
        assert loaded["model"]["base_url"] == "http://127.0.0.1:9000/v1"
        assert loaded["fallback_providers"] == [{"provider": "zai", "model": "glm-4.7"}]
        # Second pass: idempotent, still untouched.
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert after_second == after_first

    def test_root_custom_base_url_and_api_base_alias_survive(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "base_url": "http://127.0.0.1:9001/v1",
                "api_base": "http://127.0.0.1:9002/v1",
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            loaded = load_config()
            after_first = _read_config(config_path)
            second = _run_v41()
            after_second = _read_config(config_path)

        # Both endpoint spellings survive untouched: the canonical root
        # base_url AND the root api_base alias.
        assert after_first == {
            "_config_version": 40,
            "model": "stealth/ox-alpha",
            "base_url": "http://127.0.0.1:9001/v1",
            "api_base": "http://127.0.0.1:9002/v1",
            "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
        }
        assert first == {"env_added": [], "config_added": [], "warnings": []}
        # The loader folds the root endpoint in (base_url wins over the
        # alias) — the resolved route is the custom endpoint.
        assert loaded["model"]["default"] == "stealth/ox-alpha"
        assert loaded["model"]["base_url"] == "http://127.0.0.1:9001/v1"
        assert loaded["fallback_providers"] == [{"provider": "zai", "model": "glm-4.7"}]
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert after_second == after_first

    def test_root_api_base_alias_alone_survives(self, tmp_path):
        """The alias alone names the custom endpoint — the same route a root
        ``base_url`` would, so it survives too."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "api_base": "http://127.0.0.1:9003/v1",
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            loaded = load_config()
            after_first = _read_config(config_path)
            second = _run_v41()

        assert after_first == {
            "_config_version": 40,
            "model": "stealth/ox-alpha",
            "api_base": "http://127.0.0.1:9003/v1",
            "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
        }
        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert loaded["model"]["base_url"] == "http://127.0.0.1:9003/v1"
        assert second == {"env_added": [], "config_added": [], "warnings": []}

    def test_root_named_custom_provider_without_endpoint_survives(self, tmp_path):
        """A named custom provider is never the INFERRED OpenRouter route —
        even with no endpoint in sight, the provider context alone changes
        what the id resolves as (the entry's own endpoint, or an explicit
        resolution error). Only a bare/``auto``/openrouter provider infers."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "provider": "custom:local",
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            second = _run_v41()

        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert _read_config(config_path) == {
            "_config_version": 40,
            "model": "stealth/ox-alpha",
            "provider": "custom:local",
            "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
        }
        assert second == {"env_added": [], "config_added": [], "warnings": []}

    def test_root_auto_provider_without_endpoint_still_retires(self, tmp_path):
        """The fold-in must not shield the inferred route: a root/outer
        ``auto`` provider with no endpoint still resolves to OpenRouter."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "provider": "auto",
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []


class TestOxAlphaExplicitOpenRouterEndpoint:
    """Endpoint host is decisive for an EXPLICIT ``provider: openrouter`` too
    (fix-4 review, defect 2). The predicate used to return True for the
    provider before ever looking at ``base_url``, so a route the user pinned
    to their own endpoint — resolved as that endpoint at runtime — was
    destroyed. An absent endpoint, or one on openrouter.ai or a subdomain,
    stays retired."""

    def test_custom_endpoint_primary_survives_as_primary_and_fallback(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "openrouter",
                    "base_url": "https://proxy.example/v1",
                    "key_env": "MY_PROXY_KEY",
                    "api_mode": "chat_completions",
                },
                "fallback_providers": [
                    {
                        "provider": "openrouter",
                        "model": "stealth/ox-alpha",
                        "base_url": "https://proxy.example/v1",
                        "key_env": "MY_PROXY_KEY",
                    },
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            loaded = load_config()
            after_first = _read_config(config_path)
            second = _run_v41()
            after_second = _read_config(config_path)

        # Both occurrences survive verbatim: primary keeps its metadata, the
        # fallback keeps its slot, and nothing was promoted or reported.
        assert after_first == {
            "_config_version": 40,
            "model": {
                "default": "stealth/ox-alpha",
                "provider": "openrouter",
                "base_url": "https://proxy.example/v1",
                "key_env": "MY_PROXY_KEY",
                "api_mode": "chat_completions",
            },
            "fallback_providers": [
                {
                    "provider": "openrouter",
                    "model": "stealth/ox-alpha",
                    "base_url": "https://proxy.example/v1",
                    "key_env": "MY_PROXY_KEY",
                },
                {"provider": "zai", "model": "glm-4.7"},
            ],
        }
        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert loaded["model"]["default"] == "stealth/ox-alpha"
        assert loaded["model"]["base_url"] == "https://proxy.example/v1"
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert after_second == after_first

    def test_custom_endpoint_fallback_survives_under_live_primary(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "nous", "model": "kimi-k3"},
                    {
                        "provider": "openrouter",
                        "model": "stealth/ox-alpha",
                        "base_url": "https://proxy.example/v1",
                        "api_key": "sk-fake-proxy",
                    },
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            after_first = _read_config(config_path)
            second = _run_v41()

        # The user-owned fallback keeps its exact slot and order.
        assert after_first == {
            "_config_version": 40,
            "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
            "fallback_providers": [
                {"provider": "nous", "model": "kimi-k3"},
                {
                    "provider": "openrouter",
                    "model": "stealth/ox-alpha",
                    "base_url": "https://proxy.example/v1",
                    "api_key": "sk-fake-proxy",
                },
                {"provider": "zai", "model": "glm-4.7"},
            ],
        }
        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert second == {"env_added": [], "config_added": [], "warnings": []}

    def test_openrouter_subdomain_endpoint_still_retires(self, tmp_path):
        """An endpoint ON openrouter.ai's host family is the official route —
        still retired."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "openrouter",
                    "base_url": "https://api.openrouter.ai/api/v1",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []

    def test_lookalike_endpoint_survives(self, tmp_path):
        """A host that merely CONTAINS the openrouter.ai string is not the
        official route — the same host-gate the runtime applies."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai.attacker.test/v1",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()

        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert _read_config(config_path)["model"] == {
            "default": "stealth/ox-alpha",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai.attacker.test/v1",
        }

    def test_explicit_openrouter_without_endpoint_still_retires(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []

    def test_explicit_openrouter_official_endpoint_still_retires(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert raw.get("fallback_providers", []) == []

    def test_predicate_is_endpoint_decisive_for_explicit_openrouter(self):
        """Direct predicate-level lock on the repaired boundary."""
        from hermes_cli.fallback_config import is_retired_ox_alpha_route

        # Custom endpoint: user-owned, survives — for every provider form.
        assert is_retired_ox_alpha_route(
            "openrouter", "stealth/ox-alpha", "https://proxy.example/v1"
        ) is False
        assert is_retired_ox_alpha_route(
            "OpenRouter", " Stealth/OX-Alpha ", "https://proxy.example/v1"
        ) is False
        # Official host family: retired.
        assert is_retired_ox_alpha_route(
            "openrouter", "stealth/ox-alpha", "https://openrouter.ai/api/v1"
        ) is True
        assert is_retired_ox_alpha_route(
            "openrouter", "stealth/ox-alpha", "https://api.openrouter.ai/api/v1"
        ) is True
        # No endpoint: retired.
        assert is_retired_ox_alpha_route("openrouter", "stealth/ox-alpha", "") is True
        assert is_retired_ox_alpha_route("openrouter", "stealth/ox-alpha") is True
        # Lookalike host: survives.
        assert is_retired_ox_alpha_route(
            "openrouter", "stealth/ox-alpha", "https://openrouter.ai.evil/v1"
        ) is False
        # Other named providers: never the retired route.
        assert is_retired_ox_alpha_route(
            "my-gateway", "stealth/ox-alpha", ""
        ) is False


class TestOxAlphaNoSurvivorKeepsModelControls:
    """Regression (brief 5, defect 1): the no-survivor branch used to assign
    the scalar DEFAULT_CONFIG model ('') to a dict ``model:`` section,
    deleting every route-independent control with the route. Retirement owns
    the ROUTE, not the section: route-owned keys leave, everything else stays
    byte/value-identical, and only the canonical ``default`` becomes ''."""

    _CONTROLS = {
        "streaming": False,
        "max_tokens": 1234,
        "context_length": 200000,
        "default_headers": {"X-Custom": "yes", "X-Trace": "abc"},
        "lmstudio_load_mode": "always",
        "openai_runtime": "codex_app_server",
        "unknown_extension": {"nested": [1, 2, {"deep": True}]},
    }

    def _write(self, tmp_path, model):
        return _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": model,
                "fallback_providers": [
                    # Only fallback is the retired route itself.
                    {"provider": "openrouter", "model": "stealth/ox-alpha"},
                ],
            },
        )

    def test_dict_primary_keeps_every_non_route_control(self, tmp_path):
        config_path = self._write(
            tmp_path,
            {
                "default": "stealth/ox-alpha",
                "provider": "openrouter",
                # Route-owned fields of the retired route itself.
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-retired",
                "key_env": "RETIRED_ROUTE_KEY",
                "api_mode": "chat_completions",
                # Legacy id aliases: route-owned, must not linger.
                "model": "stealth/ox-alpha",
                "name": "stealth/ox-alpha",
                "api_base": "https://openrouter.ai/api/v1",
                "reasoning_echo": True,
                **self._CONTROLS,
            },
        )
        controls_snapshot = copy.deepcopy(self._CONTROLS)
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()
            raw = _read_config(config_path)
            loaded = load_config()
            second = _run_v41()

        # Route-owned keys are gone (route + every alias + promotable field);
        # only the canonical default carries the unset value.
        assert raw["model"] == {"default": "", **controls_snapshot}
        assert "provider" not in raw["model"]
        assert "base_url" not in raw["model"]
        assert "api_base" not in raw["model"]
        assert "model" not in raw["model"]
        assert "name" not in raw["model"]
        assert "api_key" not in raw["model"]
        assert "key_env" not in raw["model"]
        assert "api_mode" not in raw["model"]
        assert "reasoning_echo" not in raw["model"]
        # Controls are value-identical (deep-compared via the == above) — and
        # the loaded view serves them unchanged.
        assert loaded["model"]["streaming"] is False
        assert loaded["model"]["max_tokens"] == 1234
        assert loaded["model"]["context_length"] == 200000
        assert loaded["model"]["default_headers"] == {"X-Custom": "yes", "X-Trace": "abc"}
        assert loaded["model"]["lmstudio_load_mode"] == "always"
        assert loaded["model"]["openai_runtime"] == "codex_app_server"
        assert loaded["model"]["unknown_extension"] == controls_snapshot["unknown_extension"]
        assert loaded["model"]["default"] == ""
        # Warning still points at `hermes model`.
        assert len(results["warnings"]) == 1
        assert "hermes model" in results["warnings"][0]
        assert raw.get("fallback_providers", []) == []
        # Second run: no-op.
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert _read_config(config_path) == raw

    def test_scalar_primary_keeps_minimal_empty_default(self, tmp_path):
        """A scalar primary still collapses to the scalar default — the
        minimal behavior is unchanged."""
        config_path = self._write(tmp_path, "stealth/ox-alpha")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        raw = _read_config(config_path)
        assert raw.get("model") in (None, "")
        assert len(results["warnings"]) == 1
        assert "hermes model" in results["warnings"][0]
        assert results["config_added"] == [
            "removed retired 'openrouter/stealth/ox-alpha' fallback route(s)"
        ]

    def test_scalar_primary_with_sibling_controls_not_invented(self, tmp_path):
        """No-survivor retirement never materializes unrelated defaults: a
        scalar primary stays scalar even when other top-level sections
        exist."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "agent": {"max_turns": 100},
                "custom_top_level": 7,
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw.get("model") in (None, "")
        assert raw["agent"] == {"max_turns": 100}
        assert raw["custom_top_level"] == 7


class TestOxAlphaEnvRefClassification:
    """Regression (brief 5, defect 2): routes written as ``${VAR}`` /
    ``${env:VAR}`` references load EXPANDED at runtime, but classification
    read the literal strings — a v40 route resolving to the retired hosted
    route could be stamped v41 and stay active forever. Classification now
    runs on a deep-copied, env-expanded view (the same ``_expand_env_vars``
    load_config uses); the raw file keeps its templates."""

    def test_primary_refs_resolving_to_retired_route_retire(self, tmp_path):
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "${OX_MODEL_ID}",
                    "provider": "${OX_PROVIDER}",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        env = {
            "HERMES_HOME": str(tmp_path),
            "OX_MODEL_ID": "stealth/ox-alpha",
            "OX_PROVIDER": "openrouter",
        }
        with patch.dict(os.environ, env):
            first = _run_v41()
            loaded = load_config()
            after_first = _read_config(config_path)
            second = _run_v41()

        # The route retires exactly as the literal spelling would: the
        # surviving fallback is promoted, no warning (a replacement exists).
        assert after_first["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert first["config_added"]
        assert first["warnings"] == []
        # The raw file never sees an expanded value: only the surviving
        # fallback's own literal route.
        assert "${OX_MODEL_ID}" not in config_path.read_text(encoding="utf-8")
        assert "${OX_PROVIDER}" not in config_path.read_text(encoding="utf-8")
        # Second pass: idempotent.
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert _read_config(config_path) == after_first
        # Sanity: without the env the same config would have been the
        # unresolved template (kept literal), so the expansion is what made
        # this the retired route.
        assert loaded["model"]["default"] == "glm-4.7"

    def test_primary_refs_resolving_to_custom_route_survive(self, tmp_path):
        """A custom provider expansion is a different, valid route."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "${OX_MODEL_ID}",
                    "provider": "${OX_PROVIDER}",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        env = {
            "HERMES_HOME": str(tmp_path),
            "OX_MODEL_ID": "my-model",
            "OX_PROVIDER": "custom:local",
        }
        with patch.dict(os.environ, env):
            first = _run_v41()
            loaded = load_config()

        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert config_path.read_text(encoding="utf-8") == original
        assert loaded["model"]["default"] == "my-model"
        assert loaded["model"]["provider"] == "custom:local"

    def test_primary_refs_resolving_to_openrouter_other_model_survive(self, tmp_path):
        """OpenRouter + a DIFFERENT model id is not the retired route."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "${OX_MODEL_ID}",
                    "provider": "${OX_PROVIDER}",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(tmp_path),
                "OX_MODEL_ID": "openai/gpt-5.4",
                "OX_PROVIDER": "openrouter",
            },
        ):
            first = _run_v41()

        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert config_path.read_text(encoding="utf-8") == original

    def test_unset_refs_survive_untouched(self, tmp_path):
        """Unresolved references stay literal and are preserved
        conservatively — an unprovable route is never retired."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": "${OX_MODEL_ID_DEFINITELY_UNSET}",
                    "provider": "${OX_PROVIDER_DEFINITELY_UNSET}",
                },
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        env = {"HERMES_HOME": str(tmp_path)}
        for name in ("OX_MODEL_ID_DEFINITELY_UNSET", "OX_PROVIDER_DEFINITELY_UNSET"):
            os.environ.pop(name, None)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("OX_MODEL_ID_DEFINITELY_UNSET", None)
            os.environ.pop("OX_PROVIDER_DEFINITELY_UNSET", None)
            first = _run_v41()
            loaded = load_config()

        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert config_path.read_text(encoding="utf-8") == original
        # load_config keeps the literal placeholder too (unresolved).
        assert loaded["model"]["default"] == "${OX_MODEL_ID_DEFINITELY_UNSET}"

    def test_env_colon_prefix_resolving_to_retired_route_retires(self, tmp_path):
        """``${env:VAR}`` — the documented SecretRef alias the existing
        helper supports — classifies identically."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {
                    "default": "${env:OX_MODEL_ID}",
                    "provider": "${env:OX_PROVIDER}",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(tmp_path),
                "OX_MODEL_ID": "stealth/ox-alpha",
                "OX_PROVIDER": "openrouter",
            },
        ):
            first = _run_v41()
            after_first = _read_config(config_path)

        assert after_first["model"] == {"default": "glm-4.7", "provider": "zai"}
        assert first["config_added"]
        assert "${env:OX_MODEL_ID}" not in config_path.read_text(encoding="utf-8")

    def test_endpoint_ref_resolving_to_official_host_retires(self, tmp_path):
        """An endpoint reference resolving to openrouter.ai names the hosted
        route; the same ref resolving to a custom host survives raw."""
        retired = _write_config(
            tmp_path / "a",
            {
                "_config_version": 40,
                "model": {
                    "default": "stealth/ox-alpha",
                    "provider": "openrouter",
                    "base_url": "${OX_ENDPOINT}",
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(tmp_path / "a"),
                "OX_ENDPOINT": "https://openrouter.ai/api/v1",
            },
        ):
            first = _run_v41()

        assert first["config_added"]
        assert _read_config(retired)["model"] == {
            "default": "glm-4.7",
            "provider": "zai",
        }
        assert "${OX_ENDPOINT}" not in retired.read_text(encoding="utf-8")

        # Root-level endpoint alias spellings classify through the fold-in.
        for root_key in ("base_url", "api_base"):
            home = tmp_path / f"root-{root_key}"
            custom = _write_config(
                home,
                {
                    "_config_version": 40,
                    "model": "stealth/ox-alpha",
                    root_key: "${OX_ENDPOINT}",
                },
            )
            with patch.dict(
                os.environ,
                {"HERMES_HOME": str(home), "OX_ENDPOINT": "http://127.0.0.1:9100/v1"},
            ):
                result = _run_v41()

            assert result == {"env_added": [], "config_added": [], "warnings": []}
            assert "${OX_ENDPOINT}" in custom.read_text(encoding="utf-8")

        # ...and the root-level ref resolving to the OFFICIAL host still
        # retires (the fold-in makes it the hosted route).
        home = tmp_path / "root-retired"
        hosted = _write_config(
            home,
            {
                "_config_version": 40,
                "model": "stealth/ox-alpha",
                "base_url": "${OX_ENDPOINT}",
            },
        )
        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(home), "OX_ENDPOINT": "https://api.openrouter.ai/api/v1"},
        ):
            result = _run_v41()

        assert result["warnings"]
        assert "hermes model" in result["warnings"][0]
        # Scalar primary → scalar default; save_config strips that
        # default-valued key (the raw template stays, folded into the
        # model section by the save-time normalizer).
        assert _read_config(hosted)["model"] == {"base_url": "${OX_ENDPOINT}"}

    def test_fallback_refs_resolving_to_retired_route_removed_by_position(
        self, tmp_path
    ):
        """Modern and legacy fallback entries written as ``${VAR}`` templates
        retire through the same expanded view — removed at their raw
        position, with surviving entries keeping raw templates, order, and
        metadata."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "nous",
                        "model": "kimi-k3",
                        "key_env": "${NOUS_KEY_VAR}",
                    },
                    {
                        "provider": "${FB_PROVIDER}",
                        "model": "${FB_MODEL}",
                        "priority": 5,
                    },
                    {
                        "provider": "${CUSTOM_PROVIDER_VAR}",
                        "model": "${FB_MODEL}",
                        "base_url": "${FB_ENDPOINT}",
                    },
                ],
                "fallback_model": [
                    {
                        "provider": "${LEGACY_PROVIDER}",
                        "model": "${LEGACY_MODEL}",
                        "reasoning_echo": True,
                    },
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(tmp_path),
                "FB_PROVIDER": "openrouter",
                "FB_MODEL": "stealth/ox-alpha",
                "FB_ENDPOINT": "http://127.0.0.1:9200/v1",
                "LEGACY_PROVIDER": "auto",
                "LEGACY_MODEL": "stealth/ox-alpha",
            },
        ):
            first = _run_v41()
            after_first = _read_config(config_path)
            second = _run_v41()

        # Retired entries leave their RAW positions; survivors keep order,
        # raw templates, and metadata untouched.
        assert after_first["fallback_providers"] == [
            {"provider": "nous", "model": "kimi-k3", "key_env": "${NOUS_KEY_VAR}"},
            {
                "provider": "${CUSTOM_PROVIDER_VAR}",
                "model": "${FB_MODEL}",
                "base_url": "${FB_ENDPOINT}",
            },
        ]
        assert after_first["fallback_model"] == [{"provider": "zai", "model": "glm-4.7"}]
        assert after_first["model"] == {
            "default": "openai/gpt-5.4",
            "provider": "openrouter",
        }
        assert first["config_added"] == [
            "removed retired 'openrouter/stealth/ox-alpha' fallback route(s)"
        ]
        raw_text = config_path.read_text(encoding="utf-8")
        assert "${NOUS_KEY_VAR}" in raw_text
        assert "${CUSTOM_PROVIDER_VAR}" in raw_text
        assert "${FB_ENDPOINT}" in raw_text
        # Second pass: idempotent — the survivors still classify as
        # non-retired (custom endpoint / unresolved template).
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert _read_config(config_path) == after_first

    def test_fallback_refs_resolving_to_custom_route_survive(self, tmp_path):
        """A custom expansion (named provider or custom endpoint) survives
        with its templates intact."""
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "${FB_PROVIDER}",
                        "model": "${FB_MODEL}",
                    },
                ],
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(tmp_path),
                "FB_PROVIDER": "custom:local",
                "FB_MODEL": "stealth/ox-alpha",
            },
        ):
            first = _run_v41()

        assert first == {"env_added": [], "config_added": [], "warnings": []}
        assert config_path.read_text(encoding="utf-8") == original

    def test_promotion_carries_raw_templates_not_expanded_values(self, tmp_path):
        """A surviving ``${VAR}`` fallback promoted to the primary keeps its
        RAW templates — the expanded view is classification-only."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {
                        "provider": "${GATEWAY_PROVIDER}",
                        "model": "${GATEWAY_MODEL}",
                        "base_url": "${GATEWAY_ENDPOINT}",
                        "key_env": "${GATEWAY_KEY_VAR}",
                    },
                ],
            },
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(tmp_path),
                "GATEWAY_PROVIDER": "custom:local",
                "GATEWAY_MODEL": "my-model",
                "GATEWAY_ENDPOINT": "http://127.0.0.1:9300/v1",
                "GATEWAY_KEY_VAR": "GATEWAY_KEY",
            },
        ):
            first = _run_v41()
            after_first = _read_config(config_path)
            second = _run_v41()

        assert after_first["model"] == {
            "default": "${GATEWAY_MODEL}",
            "provider": "${GATEWAY_PROVIDER}",
            "base_url": "${GATEWAY_ENDPOINT}",
            "key_env": "${GATEWAY_KEY_VAR}",
        }
        assert first["config_added"]
        raw_text = config_path.read_text(encoding="utf-8")
        for template in (
            "${GATEWAY_MODEL}",
            "${GATEWAY_PROVIDER}",
            "${GATEWAY_ENDPOINT}",
            "${GATEWAY_KEY_VAR}",
        ):
            assert template in raw_text
        assert second == {"env_added": [], "config_added": [], "warnings": []}

    def test_sentinel_canonicalization_scopes_by_expanded_model_id(self, tmp_path):
        """The variant-auto-sentinel rewrite reads the expanded model id too:
        a sentinel on a nested ${VAR} id that resolves to the retired model
        with a custom provider is canonicalized; one resolving to an
        unrelated model is untouched."""
        sentinel = _write_config(
            tmp_path / "sentinel",
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "custom:local", "model": "${OX_MODEL_ID}"},
                    "provider": " AUTO ",
                },
            },
        )
        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(tmp_path / "sentinel"), "OX_MODEL_ID": "stealth/ox-alpha"},
        ):
            first = _run_v41()

        raw = _read_config(sentinel)
        # The sentinel is canonicalized to exact ``auto``; the save-time
        # normalizer then flattens the nested id to the string default, so
        # the persisted file loads as the protected custom route — with the
        # RAW template intact (never the expanded value).
        assert raw["model"]["provider"] == "custom:local"
        assert raw["model"]["default"] == "${OX_MODEL_ID}"
        assert any("canonicalized" in entry for entry in first["config_added"])
        assert "${OX_MODEL_ID}" in sentinel.read_text(encoding="utf-8")
        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(tmp_path / "sentinel"), "OX_MODEL_ID": "stealth/ox-alpha"},
        ):
            loaded = load_config()
        assert loaded["model"]["provider"] == "custom:local"
        assert loaded["model"]["default"] == "stealth/ox-alpha"

        # Non-retired expansion: sentinel left byte-for-byte alone.
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {
                    "default": {"provider": "my-gateway", "model": "${OTHER_MODEL}"},
                    "provider": " AUTO ",
                },
            }
        )
        untouched = tmp_path / "untouched" / "config.yaml"
        untouched.parent.mkdir(parents=True)
        untouched.write_text(original, encoding="utf-8")
        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(tmp_path / "untouched"), "OTHER_MODEL": "glm-5.3"},
        ):
            result = _run_v41()

        assert result == {"env_added": [], "config_added": [], "warnings": []}
        assert untouched.read_text(encoding="utf-8") == original

    def test_classification_view_is_never_serialized(self, tmp_path):
        """Direct lock: expansion happens on a copy. After a migration pass
        over a config full of unrelated templates, every unrelated
        ``${VAR}`` value survives verbatim."""
        config_path = _write_config(
            tmp_path,
            {
                "_config_version": 40,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "custom_templates": {
                    "a": "${SECRET_A}",
                    "b": "${env:SECRET_B}",
                    "c": ["${SECRET_C}", {"d": "${SECRET_D}"}],
                },
                "fallback_providers": [{"provider": "zai", "model": "glm-4.7"}],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            _run_v41()

        raw = _read_config(config_path)
        assert raw["custom_templates"] == {
            "a": "${SECRET_A}",
            "b": "${env:SECRET_B}",
            "c": ["${SECRET_C}", {"d": "${SECRET_D}"}],
        }


    def test_second_run_is_a_no_op(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "_config_version": 41,
                "model": {"default": "stealth/ox-alpha", "provider": "openrouter"},
                "fallback_providers": [
                    {"provider": "openrouter", "model": "stealth/ox-alpha"},
                    {"provider": "zai", "model": "glm-4.7"},
                ],
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            first = _run_v41()
            after_first = _read_config(tmp_path / "config.yaml")
            second = _run_v41()
            after_second = _read_config(tmp_path / "config.yaml")

        assert after_first == after_second
        assert second == {"env_added": [], "config_added": [], "warnings": []}
        assert first["config_added"]

    def test_no_retired_route_leaves_file_untouched(self, tmp_path):
        original = yaml.safe_dump(
            {
                "_config_version": 40,
                "model": {"default": "openai/gpt-5.4", "provider": "openrouter"},
                "fallback_providers": [{"provider": "nous", "model": "kimi-k3"}],
                "agent": {"max_turns": 100},
            }
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(original, encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            results = _run_v41()

        assert config_path.read_text(encoding="utf-8") == original
        assert results == {"env_added": [], "config_added": [], "warnings": []}
