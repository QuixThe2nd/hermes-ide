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

import os
import textwrap
from unittest.mock import patch

import yaml

from hermes_cli.config import DEFAULT_CONFIG


def _write_config(tmp_path, data):
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
        # The unconfigured empty value used by DEFAULT_CONFIG (absent or "").
        assert raw.get("model") in (None, "")
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


class TestOxAlphaIdempotence:
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
