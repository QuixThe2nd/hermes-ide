"""Engine contracts on the pm pin system (Rollout 4 follow-up):

- the pinned build lives in pm/lock.json (engine_version reads it); there
  is NO user-settable tag in config to disagree with it;
- boot serves what is INSTALLED, never downloads (the ladder);
- update_available only when the local engine is enabled AND something is
  installed AND an installed engine is older than the pin;
- retention is `hermes pm gc`'s call, not one install job's.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _store(hermes_home):
    """The pm store for this temp home: HERMES_HOME points at the root, so
    the store is <home>/tools."""
    from pm import paths

    return paths.store_root()


def _install_fake_backend(hermes_home, backend: str, version: str) -> None:
    """Record an installed engine in pm's facts + store: version is the
    lockfile tag WITHOUT the b (e.g. "10290"). The fact carries the full
    digest-bound identity (target + artifact shas) the way a real pm
    install records it — is_installed() refuses identity-less facts."""
    from pm.ensure import _identity, _lockfile
    from pm.lock import Facts
    from pm.store import current_target

    store = _store(hermes_home)
    name = {"cuda": "llamacpp-cuda", "cpu": "llamacpp-cpu"}[backend]
    entry = f"{name}-{version}-win32-x64"
    (store / entry).mkdir(parents=True, exist_ok=True)
    facts = Facts(store / "facts.json")
    lockfile = _lockfile()
    facts.record(
        name,
        version,
        entry,
        {},
        store,
        target=current_target(),
        # The FULL pinned artifact list — installed() compares the exact
        # identity tuple, and a truncated one is a different identity.
        artifacts=[a["sha256"] for a in lockfile.artifacts(name, current_target())],
    )


def _load_facts(hermes_home):
    from pm.lock import Facts

    return Facts(_store(hermes_home) / "facts.json")


# ---- fork tag-runtimes contracts preserved through the a9be133aea import ----
def _install_fake_tag(home, tag: str, backend: str = "cuda") -> None:
    """A fake FORK-layout install (runtimes/<tag>/<backend> with a verified
    manifest) — the pm store path uses _install_fake_backend above."""
    d = home / "runtimes" / "llamacpp" / tag / backend
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "tag": tag, "backend": backend, "assets": {},
        "verified_version": f"version: {tag.lstrip('b')}",
    }), encoding="utf-8")
    # server_binary() looks for the executable name per-OS; give it both.
    (d / "llama-server.exe").write_bytes(b"MZ fake")
    (d / "llama-server").write_bytes(b"\x7fELF fake")


def test_installed_tags_newest_first(hermes_home):
    from hermes_cli.local_runtime.binaries import installed_tags

    assert installed_tags() == []
    _install_fake_tag(hermes_home, "b10290")
    _install_fake_tag(hermes_home, "b10412")
    assert installed_tags() == ["b10412", "b10290"]


def test_prune_keeps_n_minus_one(hermes_home):
    from hermes_cli.local_runtime.binaries import installed_tags, prune_old_tags

    for tag in ("b10100", "b10200", "b10290"):
        _install_fake_tag(hermes_home, tag)
    prune_old_tags(["b10290", "b10200"])
    assert installed_tags() == ["b10290", "b10200"]
    # downloads/ cache dir must survive pruning when present.
    downloads = hermes_home / "runtimes" / "llamacpp" / "downloads"
    downloads.mkdir(exist_ok=True)
    prune_old_tags(["b10290"])
    assert downloads.exists()


def test_installed_backends_in_ladder_order(hermes_home):
    from hermes_cli.local_runtime.binaries import installed_backends

    assert installed_backends() == []
    _install_fake_backend(hermes_home, "cuda", "10362")
    _install_fake_backend(hermes_home, "cpu", "10362")
    assert installed_backends() == ["cuda", "cpu"]


def test_installed_backends_not_version_gated(hermes_home):
    """An engine from a previous pin still serves — boot must keep working
    across a lockfile bump until the user clicks the update."""
    from hermes_cli.local_runtime.binaries import (
        engine_update_pending,
        installed_backends,
    )

    _install_fake_backend(hermes_home, "cuda", "10290")
    assert installed_backends() == ["cuda"]
    assert engine_update_pending() is True  # 10290 < pinned 10362


def test_no_tag_key_in_config(hermes_home):
    """The build is pinned in pm/lock.json, not config; a second version
    authority would only disagree with the pin."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert "tag" not in DEFAULT_CONFIG["local_runtime"]


def test_engine_version_reads_the_lockfile(hermes_home):
    from hermes_cli.local_runtime.binaries import engine_version

    assert engine_version().startswith("b")
    assert engine_version().lstrip("b").isdigit()


@pytest.mark.parametrize("pin", [None, "b10679", "b10412"])
def test_preferred_update_offer_respects_explicit_pins(hermes_home, pin):
    """Migrated from the fork's tag-runtimes contract (was
    test_preferred_b10964_update_offer_respects_explicit_pins, fixture
    _install_fake_tag) onto the pm pin system: the shipped pin in
    pm/lock.json is the default authority, an explicit user "tag" pin in
    config.yaml still wins over it, and an update offer never replaces the
    installed engine."""
    from fastapi.testclient import TestClient

    from hermes_cli import web_server
    from hermes_cli.config import load_config
    from hermes_cli.local_runtime import binaries

    runtime = {"enabled": True}
    if pin is not None:
        runtime["tag"] = pin
    (hermes_home / "config.yaml").write_text(
        json.dumps({"local_runtime": runtime}), encoding="utf-8")
    _install_fake_backend(hermes_home, "cuda", "10290")

    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    response = client.get("/api/local-models/status")
    assert response.status_code == 200, response.text
    status = response.json()
    lock_pin = binaries.engine_version()
    expected_tag = pin or lock_pin
    configured = load_config()["local_runtime"].get("tag")
    assert (configured or lock_pin) == expected_tag  # a user pin wins, never contradicts
    assert status["configured_tag"] == expected_tag
    assert status["tag"] == "b10290"  # An offer must not replace the installed engine.
    assert status["update_available"] is True  # 10290 is older than the offer


def test_update_available_requires_enabled_and_installed(hermes_home, monkeypatch):
    """The flag's truth table: enabled AND something installed AND an
    installed engine older than the pin."""
    from fastapi.testclient import TestClient

    from hermes_cli import web_server

    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    def status():
        r = client.get("/api/local-models/status")
        assert r.status_code == 200, r.text
        return r.json()

    import hermes_cli.web_routers.local_models as lm

    # Case 1: enabled + installed at an older pin -> update available.
    monkeypatch.setattr(lm, "_runtime_section", lambda: {"enabled": True})
    _install_fake_backend(hermes_home, "cuda", "10290")
    s = status()
    assert s["update_available"] is True
    assert s["configured_tag"] == "b10362"    # the pin
    assert s["tag"] == "b10290"               # serving what's installed

    # Case 2: installed at the current pin -> no update.
    _install_fake_backend(hermes_home, "cuda", "10362")
    s = status()
    assert s["update_available"] is False
    assert s["tag"] == "b10362"

    # Case 3: disabled -> never flagged, even with a stale engine.
    monkeypatch.setattr(lm, "_runtime_section", lambda: {"enabled": False})
    assert status()["update_available"] is False


@pytest.mark.parametrize("pin", [None, "b10412"])
def test_boot_never_installs_missing_pin(hermes_home, monkeypatch, pin):
    """Boot serves what is INSTALLED; nothing installed means no boot (and
    NO install/download either way — installing is a click in the pane).
    Counted at BOTH install boundaries (fork tag installs and pm engine
    installs): a raising stub would be swallowed by boot's never-raise
    contract, so the counters are the assertion."""
    from hermes_cli.local_runtime import bootstrap

    tag_installs, engine_installs = [], []
    monkeypatch.setattr(
        "hermes_cli.local_runtime.binaries.ensure_runtime_installed",
        lambda *a, **kw: tag_installs.append(a))
    monkeypatch.setattr(
        "hermes_cli.local_runtime.binaries.ensure_engine",
        lambda *a, **kw: engine_installs.append(a))

    # Stage a model so the residency gate passes and boot actually reaches
    # the install ladder — with nothing installed it must stop there.
    mdir = bootstrap.models_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "probe.gguf").write_bytes(b"gguf probe")

    runtime = {"enabled": True}
    if pin is not None:
        runtime["tag"] = pin  # an explicit pin that is NOT installed
    assert bootstrap.ensure_local_runtime({"local_runtime": runtime}) is None
    assert tag_installs == []
    assert engine_installs == []


def test_boot_never_downloads(hermes_home, monkeypatch):
    """Boot serves what is INSTALLED; nothing installed means no boot (and
    NO download either way — installing is a click in the pane)."""
    from hermes_cli.local_runtime import bootstrap

    calls = []
    monkeypatch.setattr(
        "hermes_cli.local_runtime.binaries.ensure_engine",
        lambda *a, **kw: calls.append(True) or (_ for _ in ()).throw(
            AssertionError("boot must never install the engine")))

    # Nothing installed: returns None before any install attempt.
    cfg = {"local_runtime": {"enabled": True}}
    assert bootstrap.ensure_local_runtime(cfg) is None
    assert calls == []


def _status_client():
    from fastapi.testclient import TestClient

    from hermes_cli import web_server

    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


@pytest.mark.parametrize("pin", [None, "b10679", "b10412"])
def test_update_offer_matrix_fork_tag_layout(hermes_home, pin):
    """The full explicit-pin matrix on the fork tag layout (a verified
    runtimes/<tag> install, no pm engine): unpinned inherits the shipped
    pin, a config pin equal to the installed build means NO update, and a
    missing explicit pin offers the update while still serving what is
    installed."""
    from hermes_cli.local_runtime import binaries

    runtime = {"enabled": True}
    if pin is not None:
        runtime["tag"] = pin
    (hermes_home / "config.yaml").write_text(
        json.dumps({"local_runtime": runtime}), encoding="utf-8")
    _install_fake_tag(hermes_home, "b10679")

    response = _status_client().get("/api/local-models/status")
    assert response.status_code == 200, response.text
    status = response.json()
    expected_tag = pin or binaries.engine_version()
    assert status["configured_tag"] == expected_tag
    assert status["tag"] == "b10679"  # An offer must not replace the installed engine.
    assert status["update_available"] is (expected_tag != "b10679")


@pytest.mark.parametrize("pin", [None, "b10679", "b10412"])
def test_update_offer_matrix_pm_layout(hermes_home, pin):
    """The same explicit-pin matrix on the pm engine layout (lockfile-pinned
    packages in the pm store): an explicit config pin is honored against the
    INSTALLED engine — matching pin means no update — while an unpinned
    config stays governed by the pm lock."""
    from hermes_cli.local_runtime import binaries

    runtime = {"enabled": True}
    if pin is not None:
        runtime["tag"] = pin
    (hermes_home / "config.yaml").write_text(
        json.dumps({"local_runtime": runtime}), encoding="utf-8")
    _install_fake_backend(hermes_home, "cuda", "10679")

    response = _status_client().get("/api/local-models/status")
    assert response.status_code == 200, response.text
    status = response.json()
    expected_tag = pin or binaries.engine_version()
    assert status["configured_tag"] == expected_tag
    assert status["tag"] == "b10679"  # An offer must not replace the installed engine.
    assert status["update_available"] is (expected_tag != "b10679")
