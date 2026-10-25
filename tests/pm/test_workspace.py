"""pm.workspace: the generated uv-workspace root for plugin deps.

The workspace root is a pm-GENERATED project (never the committed
pyproject.toml — sealed installs are read-only and member lists are
machine-specific). Its pyproject = core's pyproject verbatim +
``[tool.uv.workspace] members`` pointing at each enabled plugin dir via
relative ``../``-escaping paths (proven to resolve). ``uv lock`` unions
core + plugin deps into ONE lock; conflict = loud refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pm.workspace as ws


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A fake install: core repo with pyproject, plugin dirs, store."""
    core = tmp_path / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text(
        "[project]\n"
        'name = "hermes-agent"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["httpx==0.28.1"]\n',
        encoding="utf-8",
    )
    plugins = tmp_path / "home" / "plugins"
    plug_a = plugins / "plug-a"
    plug_a.mkdir(parents=True)
    (plug_a / "plugin.yaml").write_text("name: plug-a\n", encoding="utf-8")
    (plug_a / "pyproject.toml").write_text(
        "[project]\nname = \"plug-a\"\nversion = \"0.1.0\"\n"
        'requires-python = ">=3.11"\ndependencies = ["rich==13.9.4"]\n',
        encoding="utf-8",
    )
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(ws.paths, "repo_root", lambda: core)
    monkeypatch.setattr(ws.paths, "store_root", lambda: store)
    return tmp_path, core, plug_a, store


def test_workspace_root_lives_in_the_store(layout):
    _, _, _, store = layout
    assert ws.workspace_root() == store / ".pm-workspace"
    assert ws.workspace_root() == ws.workspace_root()  # stable


def test_build_writes_core_pyproject_verbatim(layout):
    _, core, plug_a, _ = layout
    root = ws.build_root([plug_a])
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    core_text = (core / "pyproject.toml").read_text(encoding="utf-8")
    # core's project table is carried verbatim (name, deps, requires-python)
    assert 'name = "hermes-agent"' in text
    assert 'dependencies = ["httpx==0.28.1"]' in text
    assert 'requires-python = ">=3.11"' in text
    # nothing else was invented
    for line in core_text.strip().splitlines():
        assert line in text


def test_members_are_relative_paths_escaping_the_root(layout):
    _, _, plug_a, _ = layout
    root = ws.build_root([plug_a])
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.uv.workspace]" in text
    # member must be the relative path from the root to the plugin dir
    expected = ws._member_rel(root, plug_a)
    assert f'"{expected}"' in text
    assert expected.startswith(".."), "member must escape the generated root"


def test_build_is_idempotent(layout):
    _, _, plug_a, _ = layout
    ws.build_root([plug_a])
    first = (ws.workspace_root() / "pyproject.toml").read_text(encoding="utf-8")
    ws.build_root([plug_a])
    second = (ws.workspace_root() / "pyproject.toml").read_text(encoding="utf-8")
    assert first == second


def test_zero_plugins_still_builds_a_root_with_no_members(layout):
    _, _, _, _ = layout
    root = ws.build_root([])
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "hermes-agent"' in text
    assert "[tool.uv.workspace]" not in text or "members = []" in text


def test_member_stamp_hash_changes_with_plugin_set(layout):
    _, _, plug_a, _ = layout
    stamp_empty = ws.members_stamp([])
    stamp_a = ws.members_stamp([plug_a])
    stamp_b = ws.members_stamp([plug_a, plug_a])  # dedupes to same set
    assert stamp_empty != stamp_a
    assert stamp_a == stamp_b


def test_enabled_member_dirs_finds_pyproject_and_legacy_plugins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    plugins = home / "plugins"
    plugins.mkdir(parents=True)

    # modern plugin: pyproject.toml
    modern = plugins / "modern-plug"
    modern.mkdir()
    (modern / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    # legacy plugin: pip_dependencies in plugin.yaml, no pyproject
    legacy = plugins / "legacy-plug"
    legacy.mkdir()
    (legacy / "plugin.yaml").write_text(
        "name: legacy-plug\npip_dependencies:\n  - \"requests>=2\"\n",
        encoding="utf-8",
    )

    # dep-less plugin: neither — not a member
    plain = plugins / "plain-plug"
    plain.mkdir()
    (plain / "plugin.yaml").write_text("name: plain-plug\n", encoding="utf-8")

    # not a plugin dir at all
    (plugins / "stray.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(ws, "_plugin_dir_roots", lambda: {plugins})
    found = ws.enabled_member_dirs()
    names = {p.name for p in found}
    assert names == {"modern-plug", "legacy-plug"}


def test_enabled_member_dirs_survives_unreadable_roots(tmp_path, monkeypatch):
    # an OSError mid-scan (dangling junction etc.) must not lose other roots
    good = tmp_path / "good-plugins"
    good.mkdir()
    member = good / "member"
    member.mkdir()
    (member / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    class _Broken:
        def is_dir(self):
            raise OSError("dangling junction")

    monkeypatch.setattr(ws, "_plugin_dir_roots", lambda: {good, _Broken()})
    found = ws.enabled_member_dirs()
    assert [p.name for p in found] == ["member"]
