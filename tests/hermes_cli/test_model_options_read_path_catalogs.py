"""``model.options`` is a READ path: opening the picker must not wait on a provider catalog probe.

A normal open (``refresh=False``) used to run live ``/v1/models`` fetches inline — a cold cache
serialized every authed provider and one degraded provider (hanging endpoint, failed auth probe)
held the whole picker for as long as its probe took (#114215). The open now serves cached/curated
rows, marks the ones still warming (``catalog_pending``) and refreshes them off-thread; only an
explicit refresh (``refresh=True``, the "Refresh Models" action) is allowed to block on probes.
"""

import threading
import time

import hermes_cli.models as models_mod
from hermes_cli.inventory import build_model_options_payload, load_picker_context

_DEAD_PROVIDER = "deepseek"


def _picker_env(monkeypatch, tmp_path, *, hung=None):
    """One authed provider visible to the picker, no real network, isolated model-id cache.

    ``hung`` is a slug whose probe blocks on the returned event — a stand-in for a degraded
    provider. Returns ``(live_calls, release_event)``; ``live_calls`` records
    ``(provider, thread_name)`` for every live probe.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    release = threading.Event()
    live_calls: list[tuple[str, str]] = []

    def fake_provider_model_ids(provider, *, force_refresh=False):
        live_calls.append((provider, threading.current_thread().name))
        if provider == hung:
            release.wait(30)
        return ["live-model"]

    monkeypatch.setattr(models_mod, "provider_model_ids", fake_provider_model_ids)
    monkeypatch.setattr(
        "agent.models_dev.fetch_models_dev",
        lambda *a, **k: {_DEAD_PROVIDER: {"env": ["DEEPSEEK_API_KEY"], "name": "DeepSeek"}},
    )
    monkeypatch.setattr("agent.models_dev.PROVIDER_TO_MODELS_DEV", {_DEAD_PROVIDER: _DEAD_PROVIDER})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    return live_calls, release


def _drain_background_warms(timeout=10.0) -> None:
    """Let spawned catalog warms finish so a tmp HERMES_HOME can be torn down with no writers left."""
    refreshing = getattr(models_mod, "provider_catalogs_refreshing", None)
    if refreshing is None:  # pre-fix builds have no inflight registry to consult
        time.sleep(0.3)
        return
    deadline = time.time() + timeout
    while time.time() < deadline and refreshing():
        time.sleep(0.02)


def _row(payload, slug):
    return next((row for row in payload["providers"] if row["slug"] == slug), None)


def test_normal_open_never_probes_in_the_calling_thread(monkeypatch, tmp_path):
    """The open returns without running one live catalog fetch on the caller: rows come from the
    disk cache / curated list and rows still warming say so."""
    live_calls, _ = _picker_env(monkeypatch, tmp_path)

    payload = build_model_options_payload(load_picker_context())

    caller_thread = threading.current_thread().name
    assert [call for call in live_calls if call[1] == caller_thread] == []
    row = _row(payload, _DEAD_PROVIDER)
    assert row is not None, "the provider must still be offered from its cached/curated list"
    assert "live-model" not in row["models"]
    _drain_background_warms()


def test_degraded_provider_cannot_stall_the_open(monkeypatch, tmp_path):
    """One provider whose probe hangs must not hold the picker: the payload comes back while the
    probe is still in flight. (Pre-fix this assertion only ever ran after the probe returned.)"""
    live_calls, release = _picker_env(monkeypatch, tmp_path, hung=_DEAD_PROVIDER)

    box: dict = {}

    def _open():
        box["payload"] = build_model_options_payload(load_picker_context())

    opener = threading.Thread(target=_open, daemon=True)
    opener.start()
    opener.join(timeout=15)
    returned_while_hung = "payload" in box
    release.set()  # never leave the probe hanging, whatever the outcome
    opener.join(timeout=15)
    _drain_background_warms()

    assert returned_while_hung, "the open waited on a degraded provider's catalog probe"
    row = _row(box["payload"], _DEAD_PROVIDER)
    assert isinstance(row, dict), "the degraded row must still render"
    assert row.get("catalog_pending") is True, "a row whose catalog is still warming must say so"
    assert live_calls, "the degraded provider is still probed — off the read path"
    _drain_background_warms()


def test_explicit_refresh_still_probes_providers(monkeypatch, tmp_path):
    """``refresh=True`` is the explicit "Refresh Models" action: it is allowed to run live probes."""
    live_calls, _ = _picker_env(monkeypatch, tmp_path)

    payload = build_model_options_payload(load_picker_context(), refresh=True)

    assert live_calls, "an explicit refresh must still probe provider catalogs"
    row = _row(payload, _DEAD_PROVIDER)
    assert row is not None
    assert "live-model" in row["models"]
    assert not row.get("catalog_pending")
    _drain_background_warms()


def test_non_blocking_cache_read_serves_stale_entry_and_warms(monkeypatch, tmp_path):
    """Beyond the stale-serve window a non-blocking read still returns the cached row (rather than
    blocking on a probe) and kicks off the off-thread refresh."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    started = threading.Event()
    release = threading.Event()

    def fake_provider_model_ids(provider, *, force_refresh=False):
        started.set()
        release.wait(15)
        return ["live-model"]

    monkeypatch.setattr(models_mod, "provider_model_ids", fake_provider_model_ids)
    models_mod._store_cache_entry(
        _DEAD_PROVIDER,
        {"fp": models_mod._credential_fingerprint(_DEAD_PROVIDER),
         "at": time.time() - (models_mod._PROVIDER_MODELS_STALE_SERVE_MAX + 60),
         "models": ["cached-model"]},
    )

    try:
        served = models_mod.cached_provider_model_ids(_DEAD_PROVIDER, non_blocking=True)
        assert served == ["cached-model"]
        assert started.wait(10), "the stale row must be refreshed off-thread"
    finally:
        release.set()
        _drain_background_warms()


def test_warm_cache_open_is_served_from_disk_without_probing(monkeypatch, tmp_path):
    """A fresh cache entry means the next open runs no probe at all and marks nothing pending.

    ``_credential_fingerprint`` is pinned: any write to ``auth.json`` during a build (credential-pool
    seeding) legitimately changes it, which would make this test race its own background warms."""
    live_calls, _ = _picker_env(monkeypatch, tmp_path)
    monkeypatch.setattr(models_mod, "_credential_fingerprint", lambda provider: "pinned")
    build_model_options_payload(load_picker_context())
    _drain_background_warms()  # first open warmed the disk cache
    live_calls.clear()

    payload = build_model_options_payload(load_picker_context())

    assert live_calls == []
    assert not _row(payload, _DEAD_PROVIDER).get("catalog_pending")
