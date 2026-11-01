"""Installing a disabled plugin must not alter the selected dependency environment."""
from unittest.mock import Mock

import pytest

from hermes_cli import plugins_cmd as plugins


def test_install_disabled_does_not_prepare_dependencies(tmp_path, monkeypatch):
    target = tmp_path / "plug"
    target.mkdir()
    manifest = {"name": "plug", "python_dependencies": ["new-package>=1,<2"]}
    monkeypatch.setattr(plugins, "_install_plugin_core", lambda *a, **k: (target, manifest, "plug"))
    monkeypatch.setattr(plugins, "_looks_like_plugin_dir", lambda *_: True)
    monkeypatch.setattr(plugins, "_prompt_plugin_env_vars", lambda *a: None)
    monkeypatch.setattr(plugins, "_display_after_install", lambda *a: None)
    monkeypatch.setattr(plugins, "_declared_capabilities_from_manifest", lambda *a: [])
    def forbidden(*a, **k):
        raise AssertionError("disabled installation changed dependencies")
    monkeypatch.setattr(plugins, "_install_plugin_python_deps", forbidden)
    plugins.cmd_install("https://example.invalid/owner/plug", enable=False)
