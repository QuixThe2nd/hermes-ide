"""Managed workspace tooling wins; PATH is only a pre-install fallback."""
import importlib
import shutil

from pm.workspace import _uv_binary


def test_workspace_uv_prefers_managed_tool_without_path_probe(monkeypatch):
    ensure = importlib.import_module("pm.ensure")
    monkeypatch.setattr(ensure, "uv", lambda **kwargs: ("managed/uv", {}))
    def reject_path(*args, **kwargs):
        raise AssertionError("managed uv must win before PATH")
    monkeypatch.setattr(shutil, "which", reject_path)
    assert _uv_binary() == "managed/uv"


def test_workspace_uv_accepts_developer_path_only_when_store_absent(monkeypatch):
    ensure = importlib.import_module("pm.ensure")
    monkeypatch.setattr(ensure, "uv", lambda **kwargs: (None, {}))
    monkeypatch.setattr(shutil, "which", lambda name: "developer/uv" if name == "uv" else None)
    assert _uv_binary() == "developer/uv"
