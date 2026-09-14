"""Focused tests for the silent-default pair.

A no-override install (empty ``model.default``, no user picker choice) must
resolve to the reference gateway's primary — provider ``xai-oauth``, model
``grok-4.6`` (the native xAI spelling; aggregators carry ``x-ai/grok-4.6``).
Explicit user values must survive every one of these paths untouched.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from hermes_cli import models as models_mod
from hermes_cli.models import (
    OPENROUTER_MODELS,
    PREFERRED_SILENT_DEFAULT_MODEL,
    PREFERRED_SILENT_DEFAULT_MODEL_IDS,
    PREFERRED_SILENT_DEFAULT_PROVIDER,
    get_default_model_for_provider,
    get_preferred_silent_default_model,
    pick_silent_default_model,
)


def _catalog_ids_for(provider: str) -> list[str]:
    """In-repo curated ids for a provider (OpenRouter keeps its own tuple list)."""
    if provider == "openrouter":
        return [mid for mid, _ in OPENROUTER_MODELS]
    return models_mod._PROVIDER_MODELS.get(provider, [])


@contextmanager
def _no_catalog_cache():
    """Simulate a fresh install: no cached catalog manifest anywhere."""
    with patch(
        "hermes_cli.model_catalog.get_default_model_from_cache",
        return_value=None,
    ):
        yield


class TestSilentDefaultPair:
    def test_pair_matches_gateway_primary(self):
        assert PREFERRED_SILENT_DEFAULT_PROVIDER == "xai-oauth"
        assert PREFERRED_SILENT_DEFAULT_MODEL == "grok-4.6"
        assert (
            PREFERRED_SILENT_DEFAULT_MODEL_IDS[PREFERRED_SILENT_DEFAULT_PROVIDER]
            == PREFERRED_SILENT_DEFAULT_MODEL
        )

    def test_aggregators_carry_vendor_prefixed_spelling(self):
        assert PREFERRED_SILENT_DEFAULT_MODEL_IDS["openrouter"] == "x-ai/grok-4.6"
        assert PREFERRED_SILENT_DEFAULT_MODEL_IDS["nous"] == "x-ai/grok-4.6"

    def test_every_spelling_exists_in_its_providers_catalog(self):
        """The silent default must never name a model the provider doesn't
        serve — a miss makes ``get_default_model_for_provider`` fall through
        to curated entry [0], the priciest flagship."""
        for provider, mid in PREFERRED_SILENT_DEFAULT_MODEL_IDS.items():
            catalog = _catalog_ids_for(provider)
            assert mid in catalog, f"{provider} catalog must carry {mid}"


class TestPreferredSilentDefaultModel:
    def test_no_override_resolves_to_grok(self):
        with _no_catalog_cache():
            assert get_preferred_silent_default_model() == "grok-4.6"

    def test_catalog_label_wins_when_cached(self):
        with patch(
            "hermes_cli.model_catalog.get_default_model_from_cache",
            return_value="grok-4.7",
        ) as labeled:
            assert get_preferred_silent_default_model() == "grok-4.7"
            # The no-override lookup targets the silent-default provider's
            # own catalog block, not an aggregator's.
            labeled.assert_called_once_with("xai-oauth")

    def test_no_override_ignores_aggregator_labels(self):
        """A cached manifest that only labels aggregator blocks must not
        leak a vendor-prefixed spelling into the no-override path."""

        def fake_lookup(provider):
            return {"openrouter": "x-ai/grok-4.6", "nous": "x-ai/grok-4.6"}.get(
                provider
            )

        with patch(
            "hermes_cli.model_catalog.get_default_model_from_cache",
            side_effect=fake_lookup,
        ):
            assert get_preferred_silent_default_model() == "grok-4.6"

    def test_provider_scoped_spelling_offline(self):
        with _no_catalog_cache():
            assert get_preferred_silent_default_model("openrouter") == "x-ai/grok-4.6"
            assert get_preferred_silent_default_model("nous") == "x-ai/grok-4.6"
            # Providers the table doesn't cover get the canonical spelling.
            assert get_preferred_silent_default_model("zai") == "grok-4.6"


class TestDefaultModelForProvider:
    def test_xai_oauth_fills_grok(self):
        with _no_catalog_cache():
            assert get_default_model_for_provider("xai-oauth") == "grok-4.6"

    def test_aggregators_get_their_own_spelling(self):
        with _no_catalog_cache():
            for provider in ("openrouter", "nous"):
                mid = get_default_model_for_provider(provider)
                assert mid == PREFERRED_SILENT_DEFAULT_MODEL_IDS[provider]
                assert mid in _catalog_ids_for(provider)

    def test_other_providers_unchanged(self):
        # Providers outside _SILENT_DEFAULT_PROVIDERS keep curated entry [0].
        assert get_default_model_for_provider("tencent-tokenhub") == "hy4-preview"


class TestPickSilentDefaultModel:
    def test_prefers_labeled_default_in_list(self):
        ids = ["anthropic/claude-fable-5.1", "x-ai/grok-4.6", "z-ai/glm-5.3"]
        with _no_catalog_cache():
            assert (
                pick_silent_default_model(ids, provider="openrouter")
                == "x-ai/grok-4.6"
            )

    def test_no_override_pick_uses_native_spelling(self):
        ids = ["grok-4.6", "grok-4.5", "grok-build-0.1"]
        with _no_catalog_cache():
            assert pick_silent_default_model(ids) == "grok-4.6"

    def test_falls_to_first_when_default_absent(self):
        ids = ["qwen/qwen3.8-max", "z-ai/glm-5.3"]
        with _no_catalog_cache():
            assert pick_silent_default_model(ids, provider="openrouter") == ids[0]

    def test_empty_list_returns_empty(self):
        with _no_catalog_cache():
            assert pick_silent_default_model([], provider="openrouter") == ""
