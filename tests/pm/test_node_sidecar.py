"""Tests: install_node_sidecar — the npm ci executor for plugin package.json
sidecars (plugin-deps plan §B item 2, wired). Hermetic: runner + binary
injected, lazy-gate patched."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pm.workspace as ws


@pytest.fixture
def lazy_on(monkeypatch):
    import sys

    if "pm.ensure" not in sys.modules:
        import importlib

        importlib.import_module("pm.ensure")
    ensure_mod = sys.modules["pm.ensure"]
    monkeypatch.setattr(ensure_mod, "lazy_installs_allowed", lambda: True)


def _plug(tmp_path: Path, with_lock: bool = False) -> Path:
    plug = tmp_path / "node-plug"
    plug.mkdir()
    (plug / "package.json").write_text('{"name": "node-plug"}\n', encoding="utf-8")
    if with_lock:
        (plug / "package-lock.json").write_text("{}\n", encoding="utf-8")
    return plug


def test_no_package_json_is_a_noop(tmp_path, lazy_on):
    plug = tmp_path / "plain"
    plug.mkdir()
    assert ws.install_node_sidecar(plug, npm_bin="npm") is None


def test_ci_when_lockfile_present(tmp_path, lazy_on):
    plug = _plug(tmp_path, with_lock=True)
    calls = []

    def runner(cmd, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert ws.install_node_sidecar(plug, npm_bin="npm", runner=runner) is None
    assert calls == [["npm", "ci", "--no-audit", "--no-fund"]]


def test_install_without_lockfile(tmp_path, lazy_on):
    plug = _plug(tmp_path)  # no package-lock.json
    calls = []

    def runner(cmd, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert ws.install_node_sidecar(plug, npm_bin="npm", runner=runner) is None
    assert calls == [["npm", "install", "--no-audit", "--no-fund"]]


def test_lazy_off_refuses(tmp_path, monkeypatch):
    import sys

    if "pm.ensure" not in sys.modules:
        import importlib

        importlib.import_module("pm.ensure")
    ensure_mod = sys.modules["pm.ensure"]
    monkeypatch.setattr(ensure_mod, "lazy_installs_allowed", lambda: False)
    plug = _plug(tmp_path)
    reason = ws.install_node_sidecar(plug, npm_bin="npm")
    assert reason and "disabled" in reason


def test_npm_failure_returns_reason_not_raise(tmp_path, lazy_on):
    plug = _plug(tmp_path)

    def runner(cmd, **k):
        return SimpleNamespace(returncode=1, stdout="", stderr="ERESOLVE unable to resolve dependency tree")

    reason = ws.install_node_sidecar(plug, npm_bin="npm", runner=runner)
    assert "exited 1" in reason and "ERESOLVE" in reason


def test_real_npm_installs_locked_sidecar_and_keeps_parent_unchanged(tmp_path, lazy_on):
    import json
    import os
    import shutil
    import subprocess

    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    node = shutil.which("node")
    if not npm or not node:
        pytest.skip("npm and node are required")
    dependency = tmp_path / "side-dep"
    dependency.mkdir()
    (dependency / "package.json").write_text(json.dumps({"name": "side-dep", "version": "1.0.0", "main": "index.js"}))
    (dependency / "index.js").write_text("module.exports = 'isolated';")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "package.json").write_text(json.dumps({"name": "plugin", "version": "1.0.0", "dependencies": {"side-dep": "file:../side-dep"}}))
    ambient = dict(os.environ)
    assert ws.install_node_sidecar(plugin, npm_bin=npm) is None
    lock = (plugin / "package-lock.json").read_bytes()
    assert ws.install_node_sidecar(plugin, npm_bin=npm) is None
    assert (plugin / "package-lock.json").read_bytes() == lock
    assert dict(os.environ) == ambient
    result = subprocess.run([node, "-e", "console.log(require('side-dep'))"], cwd=plugin, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"
    assert not (tmp_path / "node_modules").exists()


def test_runner_explosion_is_a_reason(tmp_path, lazy_on):
    plug = _plug(tmp_path)

    def runner(cmd, **k):
        raise OSError("spawn denied")

    reason = ws.install_node_sidecar(plug, npm_bin="npm", runner=runner)
    assert "failed to run" in reason
