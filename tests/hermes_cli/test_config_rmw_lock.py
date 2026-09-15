"""Concurrent dashboard config writers must not drop each other's mutations.

Only ``PUT /api/config`` used to hold ``_CONFIG_MUTATION_LOCK``. ``POST /api/model/set``,
``PUT /api/model/moa``, the custom-endpoint handlers and the memory-provider saves ran their
load→mutate→save cycles unlocked on worker threads, so a model assignment racing the desktop's
debounced whole-record autosave interleaved as::

    T1 load (model=A)      T2 load (model=A)
    T1 mutate model=B      T2 mutate display.x
    T1 save (model=B)      T2 save (model=A + display.x)   <- T1's write erased

Both tests provoke that interleaving with a slowed ``save_config`` and assert both writes land.
"""

from __future__ import annotations

import threading
import time

import pytest
import yaml


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient

    from hermes_cli.config import load_config, save_config
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    monkeypatch.setattr("hermes_cli.model_cost_guard.expensive_model_warning", lambda *_a, **_k: None)
    cfg = load_config()
    cfg["model"] = {"provider": "openrouter", "default": "openai/gpt-5.5"}
    save_config(cfg)

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def _slow_saves(monkeypatch, delay: float = 0.2) -> None:
    """Widen the load→save window so an unlocked pair reliably loses a write."""
    import hermes_cli.config as cfg_mod

    real_save = cfg_mod.save_config

    def slow_save(config, *args, **kwargs):
        time.sleep(delay)
        return real_save(config, *args, **kwargs)

    monkeypatch.setattr(cfg_mod, "save_config", slow_save)


def _race(*calls):
    results: list = [None] * len(calls)

    def run(i, fn):
        results[i] = fn()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def _model_block(home) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def test_model_set_racing_config_autosave_keeps_both_writes(client, monkeypatch, _isolate_hermes_home):
    """``applyMainModel`` (POST /api/model/set) while the settings autosave (PUT /api/config) is
    in flight: the model assignment AND the autosaved field both survive."""
    _slow_saves(monkeypatch)

    set_model = lambda: client.post(  # noqa: E731
        "/api/model/set", json={"scope": "main", "provider": "openrouter", "model": "anthropic/claude-sonnet-4"})
    autosave = lambda: client.put(  # noqa: E731
        "/api/config", json={"config": {"display": {"personality": "canary"}}})

    r1, r2 = _race(set_model, autosave)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    on_disk = _model_block(_isolate_hermes_home)
    assert on_disk["model"]["default"] == "anthropic/claude-sonnet-4"
    assert on_disk["display"]["personality"] == "canary"


def test_custom_endpoint_upsert_racing_moa_save_keeps_both_writes(client, monkeypatch, _isolate_hermes_home):
    """Two sync-def writers on worker threads (custom-endpoint upsert vs MoA save) serialize
    through the same lock — neither top-level section is lost."""
    _slow_saves(monkeypatch)

    upsert = lambda: client.post(  # noqa: E731
        "/api/providers/custom-endpoints",
        json={"id": "racebox", "name": "racebox", "base_url": "http://racebox:8000/v1", "model": "race-model",
              "discover_models": False})
    moa = lambda: client.put(  # noqa: E731
        "/api/model/moa",
        json={"reference_models": [{"provider": "openrouter", "model": "openai/gpt-5.5"}],
              "aggregator": {"provider": "openrouter", "model": "openai/gpt-5.5"}})

    r1, r2 = _race(upsert, moa)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    on_disk = _model_block(_isolate_hermes_home)
    assert "racebox" in on_disk["providers"]
    assert on_disk["moa"]["aggregator"]["model"] == "openai/gpt-5.5"
