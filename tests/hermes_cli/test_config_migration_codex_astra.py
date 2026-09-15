"""v42 migration: the canonical openai-codex/gpt-5.6-sol pair (primary
``model.default`` plus exact-matching ``fallback_providers`` entries) becomes
``gpt-6-astra`` in place; every other routing decision is untouched. Only the
v42 step runs (``run_migrations(41, ...)``; the driver stamps versions)."""

import os
from unittest.mock import patch

import yaml

from hermes_cli.config_migrations import run_migrations


def _migrated(tmp_path, config):
    """Write *config* under HERMES_HOME, run only v42, return (raw, results)."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    results = {"env_added": [], "config_added": [], "warnings": []}
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        run_migrations(41, results, quiet=True)
    return yaml.safe_load(path.read_text(encoding="utf-8")), results


def test_canonical_pair_rewritten_in_place(tmp_path):
    config = {
        "model": {"provider": "openai-codex", "default": "gpt-5.6-sol", "streaming": True},
        "fallback_providers": [
            {"provider": "zai", "model": "glm-4.7"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol", "priority": 2},
        ],
    }
    raw, results = _migrated(tmp_path, config)
    # Only the Sol ids move; sibling fields, unrelated entries, and order stay.
    assert raw["model"] == {"provider": "openai-codex", "default": "gpt-6-astra", "streaming": True}
    assert raw["fallback_providers"] == [
        {"provider": "zai", "model": "glm-4.7"},
        {"provider": "openai-codex", "model": "gpt-6-astra", "priority": 2},
    ]
    assert any("gpt-6-astra" in entry for entry in results["config_added"])
    # Idempotent: a second pass finds no canonical pair left to rewrite.
    assert _migrated(tmp_path, raw)[1]["config_added"] == []


def test_non_matching_routing_untouched(tmp_path):
    # Sol under another provider, lookalike slugs, and the legacy
    # ``fallback_model`` shape are different routing decisions — never touched.
    config = {
        "model": {"provider": "openrouter", "default": "gpt-5.6-sol"},
        "fallback_providers": [
            {"provider": "openai", "model": "gpt-5.6-sol"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol-900k"},
        ],
        "fallback_model": [{"provider": "openai-codex", "model": "gpt-5.6-sol"}],
    }
    raw, results = _migrated(tmp_path, config)
    assert results["config_added"] == []
    assert raw == config
