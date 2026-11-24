"""Pricing catalog fetch: failure TTL and success caching."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import hermes_cli.models as models_mod
from hermes_cli import models_pricing
from hermes_cli.models_pricing import fetch_models_with_pricing


def test_a_failed_catalog_fetch_is_not_cached_forever(monkeypatch):
    """A blip must not disable live model discovery for the whole process.

    The empty result is cached so a dead endpoint isn't re-dialed on every
    call, but it expires — the processes that read this run for weeks, and
    every caller silently falls back to a curated list meanwhile.
    """
    models_pricing._pricing_cache.clear()
    models_pricing._pricing_cache_retry_after.clear()

    calls = []

    def _fail(req, timeout=8.0):
        calls.append(req)
        raise OSError("connection refused")

    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", _fail)

    assert fetch_models_with_pricing(base_url="https://example.test") == {}
    # Inside the window the failure is cached: no second dial.
    assert fetch_models_with_pricing(base_url="https://example.test") == {}
    assert len(calls) == 1

    now = models_mod.time.monotonic()
    monkeypatch.setattr(
        models_mod.time,
        "monotonic",
        lambda: now + models_pricing._FAILED_CATALOG_TTL_SECONDS + 1,
    )
    assert fetch_models_with_pricing(base_url="https://example.test") == {}
    assert len(calls) == 2


def test_a_successful_catalog_fetch_stays_cached(monkeypatch):
    """Only the failures expire; a real catalog is still fetched once."""
    models_pricing._pricing_cache.clear()
    models_pricing._pricing_cache_retry_after.clear()

    calls = []
    body = json.dumps(
        {"data": [{"id": "a/b", "pricing": {"prompt": "1", "completion": "2"}}]}
    ).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda *a: False

    def _ok(req, timeout=8.0):
        calls.append(req)
        return resp

    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", _ok)

    assert "a/b" in fetch_models_with_pricing(base_url="https://example.test")
    now = models_mod.time.monotonic()
    monkeypatch.setattr(
        models_mod.time,
        "monotonic",
        lambda: now + models_pricing._FAILED_CATALOG_TTL_SECONDS + 1,
    )
    assert "a/b" in fetch_models_with_pricing(base_url="https://example.test")
    assert len(calls) == 1

