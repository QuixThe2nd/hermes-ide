"""Regression tests for #25106: a persisted global model switch never wrote
``model.base_url``/``model.api_mode`` to config.yaml, so a global provider
switch left the PREVIOUS provider's endpoint/wire-protocol on disk. The next
`hermes` launch re-read the stale base_url and routed the new model at the
old host.

The persistence shape lives in the canonical
``hermes_cli.model_switch.persist_model_selection`` shared by every surface
(``hermes model``, the setup wizard); these tests drive it directly.
"""

import pytest

from hermes_cli.model_switch import ModelSwitchResult, persist_model_selection


def _make_result(*, base_url="https://api.minimax.io/v1", api_mode="chat_completions", provider_changed=True):
    return ModelSwitchResult(
        success=True,
        new_model="MiniMax-M3",
        target_provider="custom:minimax",
        provider_changed=provider_changed,
        api_key="sk-minimax",
        base_url=base_url,
        api_mode=api_mode,
        warning_message="",
        provider_label="MiniMax (custom)",
        resolved_via_alias=False,
        capabilities=None,
        model_info=None,
        is_global=True,
    )


def test_global_switch_persists_base_url_and_api_mode(monkeypatch, tmp_path):
    """The core #25106 fix: a global switch to a new provider/endpoint must
    write the newly resolved base_url and api_mode, not just default/provider."""
    saved: dict[str, object] = {}
    monkeypatch.setattr("hermes_cli.config.read_user_config_raw", lambda _p: {"model": {}})
    monkeypatch.setattr(
        "hermes_cli.config.warn_unpinned_cron_jobs_after_model_config_change",
        lambda *_a, **_k: None,
    )

    def _fake_save(path, key, value):
        saved[key] = value

    monkeypatch.setattr("utils.atomic_roundtrip_yaml_update", _fake_save)
    persist_model_selection(_make_result(), config_path=tmp_path / "config.yaml")

    assert saved["model.default"] == "MiniMax-M3"
    assert saved["model.provider"] == "custom:minimax"
    assert saved["model.base_url"] == "https://api.minimax.io/v1"
    assert saved["model.api_mode"] == "chat_completions"
