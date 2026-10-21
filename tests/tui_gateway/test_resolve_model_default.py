"""The TUI/desktop gateway's model resolution honors the silent-default pair.

An empty config model (no env seed, no user picker choice) resolves to the
reference gateway's primary — ``grok-4.6`` — while explicit env/config values
pass through exactly as typed.
"""

from __future__ import annotations

import sys

from tui_gateway import server


def _no_env_seed(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)


def _no_catalog_cache(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.model_catalog.get_default_model_from_cache",
        lambda provider: None,
    )


def test_empty_config_resolves_to_silent_default(monkeypatch):
    _no_env_seed(monkeypatch)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": ""})
    _no_catalog_cache(monkeypatch)
    assert server._resolve_model() == "grok-4.6"


def test_missing_model_section_resolves_to_silent_default(monkeypatch):
    _no_env_seed(monkeypatch)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    _no_catalog_cache(monkeypatch)
    assert server._resolve_model() == "grok-4.6"


def test_explicit_env_model_stays_exact(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL", "z-ai/glm-5.3")
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    assert server._resolve_model() == "z-ai/glm-5.3"


def test_explicit_config_model_stays_exact(monkeypatch):
    _no_env_seed(monkeypatch)
    monkeypatch.setattr(
        server, "_load_cfg", lambda: {"model": {"default": "qwen/qwen3.8-max"}}
    )
    assert server._resolve_model() == "qwen/qwen3.8-max"


def test_last_ditch_literal_matches_constant(monkeypatch):
    """If even the hermes_cli import fails, the except-branch literal must
    equal the shared constant — it cannot be allowed to drift."""
    from hermes_cli.models import PREFERRED_SILENT_DEFAULT_MODEL

    assert PREFERRED_SILENT_DEFAULT_MODEL == "grok-4.6"
    _no_env_seed(monkeypatch)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    # A None sys.modules entry makes ``from hermes_cli.models import ...``
    # raise ImportError inside _resolve_model's try block.
    monkeypatch.setitem(sys.modules, "hermes_cli.models", None)
    assert server._resolve_model() == "grok-4.6"
