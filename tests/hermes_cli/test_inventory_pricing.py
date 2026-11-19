"""Tests for inventory._apply_pricing — the pricing enrichment that

feeds the desktop GUI model picker (and onboarding) so it can show $/Mtok
columns + Free/Pro badges, the same way the `hermes model` CLI picker does.
"""

from threading import Event
from time import monotonic

import hermes_cli.inventory as inv
from hermes_cli import models_pricing


def _patch_pricing(monkeypatch, *, pricing):
    monkeypatch.setattr(models_pricing, "get_pricing_for_provider", lambda slug, **kw: pricing.get(slug, {}))


def test_apply_pricing_formats_per_model_prices(monkeypatch):
    """Each model gets formatted input/output/cache + a free flag."""
    _patch_pricing(
        monkeypatch,
        pricing={
            "openrouter": {
                "a/paid": {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003"},
                "b/free": {"prompt": "0", "completion": "0"},
            }
        },
    )
    rows = [{"slug": "openrouter", "models": ["a/paid", "b/free"]}]
    inv._apply_pricing(rows)

    pricing = rows[0]["pricing"]
    assert pricing["a/paid"] == {"input": "$3.00", "output": "$15.00", "cache": "$0.30", "free": False}
    assert pricing["b/free"]["free"] is True
    assert pricing["b/free"]["input"] == "free"


def test_apply_pricing_free_models_get_flat_100_percent_sale(monkeypatch):
    """Free models show -100% chrome; was_* only when original was served."""
    _patch_pricing(
        monkeypatch,
        pricing={
            "openrouter": {
                "a/free": {
                    "prompt": "0",
                    "completion": "0",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                    },
                },
                "b/natively-free": {
                    "prompt": "0",
                    "completion": "0",
                },
            }
        },
    )
    rows = [{"slug": "openrouter", "models": ["a/free", "b/natively-free"]}]
    inv._apply_pricing(rows)
    free = rows[0]["pricing"]["a/free"]
    assert free["free"] is True
    assert free["discount_percent"] == 100
    assert free["was_input"] == "$2.00"
    assert free["was_output"] == "$10.00"
    native = rows[0]["pricing"]["b/natively-free"]
    assert native["free"] is True
    assert native["discount_percent"] == 100
    # No gateway original → no fabricated was prices.
    assert "was_input" not in native
    assert "was_output" not in native


def test_apply_pricing_omits_sale_when_original_not_cheaper(monkeypatch):
    _patch_pricing(
        monkeypatch,
        pricing={
            "openrouter": {
                "a/eq": {
                    "prompt": "0.000002",
                    "completion": "0.00001",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                    },
                },
            }
        },
    )
    rows = [{"slug": "openrouter", "models": ["a/eq"]}]
    inv._apply_pricing(rows)
    assert "discount_percent" not in rows[0]["pricing"]["a/eq"]


def test_model_options_cold_pricing_fetch_runs_off_the_request_path(monkeypatch):
    """A cold pricing endpoint must not delay the first picker payload."""
    fetch_started = Event()
    release_fetch = Event()

    def fake_pricing(_slug, *, force_refresh=False, cached_only=False):
        if cached_only:
            return {}
        fetch_started.set()
        release_fetch.wait(timeout=5)
        return {}

    row = {
        "slug": "openrouter",
        "name": "OpenRouter",
        "models": ["vendor/model"],
        "total_models": 1,
        "is_current": True,
        "is_user_defined": False,
        "source": "built-in",
    }
    monkeypatch.setattr(models_pricing, "get_pricing_for_provider", fake_pricing)
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [row],
    )
    monkeypatch.setattr(inv, "_moa_provider_row", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inv, "_apply_capabilities", lambda _rows: None)
    monkeypatch.setattr(inv, "_apply_featured", lambda _rows: None)
    monkeypatch.setattr(inv, "_pricing_prewarm_threads", {})

    try:
        started_at = monotonic()
        payload = inv.build_model_options_payload(
            inv.ConfigContext(
                current_provider="openrouter",
                current_model="vendor/model",
                current_base_url="",
                user_providers={},
                custom_providers=[],
            )
        )
        elapsed = monotonic() - started_at
        assert payload["providers"][0]["slug"] == "openrouter"
        assert "pricing" not in payload["providers"][0]
        assert elapsed < 2.0, f"cold picker blocked for {elapsed:.2f}s"
        assert fetch_started.wait(timeout=1), "pricing should prewarm in the background"
    finally:
        threads = list(inv._pricing_prewarm_threads.values())
        release_fetch.set()
        for thread in threads:
            thread.join(timeout=2)


def test_prewarm_preserves_context_and_runs_once_per_profile(tmp_path, monkeypatch):
    """Concurrent multiplex profiles retain their own home and secret scope."""
    from agent.secret_scope import (
        current_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import (
        hermes_home_key,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    monkeypatch.setattr(inv, "_pricing_prewarm_threads", {})
    release = Event()
    started = {"a": Event(), "b": Event()}
    observed = {}

    def capture_context(_rows):
        scope = current_secret_scope()
        label = scope["PROFILE_MARKER"]
        observed[label] = (hermes_home_key(), dict(scope))
        started[label].set()
        release.wait(timeout=5)

    monkeypatch.setattr(inv, "_apply_pricing", capture_context)

    threads = []
    try:
        for label in ("a", "b"):
            home = tmp_path / label
            home_token = set_hermes_home_override(str(home))
            secret_token = set_secret_scope({"PROFILE_MARKER": label})
            try:
                threads.append(inv._prewarm_pricing_async([{"models": []}]))
            finally:
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)

        assert threads[0] is not threads[1]
        assert started["a"].wait(timeout=1)
        assert started["b"].wait(timeout=1)
        assert observed["a"] == (
            hermes_home_key(tmp_path / "a"),
            {"PROFILE_MARKER": "a"},
        )
        assert observed["b"] == (
            hermes_home_key(tmp_path / "b"),
            {"PROFILE_MARKER": "b"},
        )
    finally:
        release.set()
        for thread in threads:
            if thread is not None:
                thread.join(timeout=2)


def test_prewarm_deduplicates_inflight_scope_and_cleans_up(monkeypatch):
    """Rapid opens share one worker, then a completed scope can run again."""
    monkeypatch.setattr(inv, "_pricing_prewarm_threads", {})
    started = Event()
    release = Event()
    calls = []

    def blocked_prewarm(_rows):
        calls.append(None)
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(inv, "_apply_pricing", blocked_prewarm)
    rows = [{"slug": "openrouter", "models": ["vendor/model"]}]

    first = inv._prewarm_pricing_async(rows)
    try:
        assert started.wait(timeout=1)
        second = inv._prewarm_pricing_async(rows)
        assert second is first
        assert len(calls) == 1
    finally:
        release.set()
        first.join(timeout=2)

    assert not first.is_alive()
    assert inv._pricing_prewarm_threads == {}

    retry = inv._prewarm_pricing_async(rows)
    retry.join(timeout=2)
    assert retry is not first
    assert len(calls) == 2
    assert inv._pricing_prewarm_threads == {}


def test_cached_only_pricing_returns_a_warm_value_without_fetching(monkeypatch):
    """Cache-only picker reads preserve pricing once the prewarm completes."""
    cache_key = "https://openrouter.ai/api"
    expected = {"vendor/model": {"prompt": "0.000001", "completion": "0.000002"}}
    monkeypatch.setattr(models_pricing, "_pricing_cache", {cache_key: expected})
    monkeypatch.setattr(models_pricing, "_pricing_cache_retry_after", {})
    monkeypatch.setattr(models_pricing, "_pricing_provider_cache_keys", {})
    monkeypatch.setattr(models_pricing, "fetch_models_with_pricing",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network fetch started")),
    )

    assert models_pricing.get_pricing_for_provider(
        "openrouter", cached_only=True
    ) == expected


