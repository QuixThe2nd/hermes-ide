"""Absence contract for the LiteLLM-only model-provider strip.

These assertions fail while bundled non-LiteLLM model providers or the
openai-codex image plugin are still in the tree, and pass after the strip.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _plugin_dirs(path: Path) -> list[str]:
    return sorted(
        child.name
        for child in path.iterdir()
        if child.is_dir() and not child.name.startswith((".", "_"))
    )


def test_only_custom_model_provider_plugin_remains():
    names = _plugin_dirs(ROOT / "plugins" / "model-providers")
    assert names == ["custom"], names


def test_openai_codex_plugins_absent():
    assert not (ROOT / "plugins" / "image_gen" / "openai-codex").exists()
    assert not (ROOT / "plugins" / "model-providers" / "openai-codex").exists()


def test_litellm_codex_images_provider_present():
    path = ROOT / "plugins" / "image_gen" / "litellm-codex-images"
    assert path.is_dir()
    assert (path / "__init__.py").is_file()
    assert (path / "plugin.yaml").is_file()
