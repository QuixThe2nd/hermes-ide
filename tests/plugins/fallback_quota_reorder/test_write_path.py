"""Config write, backup, and restore behavior contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli.config import load_config
from hermes_cli.fallback_config import get_fallback_chain, _entry_identity
from plugins.fallback_quota_reorder.core import (
    FallbackQuotaReorderError,
    backup_dir,
    chain_signature,
    order_signature,
    run_reorder,
    write_fallback_order,
)
from tests.plugins.fallback_quota_reorder._helpers import (
    default_channel_names,
    fake_http_for_names,
    write_hermes_home,
    write_quota_config_path,
)


class TestWriteFallbackOrderValidation:
    def test_rejects_deprecated_default_key_without_backup(
        self, monkeypatch, tmp_path: Path
    ):
        write_hermes_home(
            tmp_path,
            fallback_providers=[
                {"provider": "xai-oauth", "default": "grok-4"},
            ],
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        original = (tmp_path / "config.yaml").read_bytes()
        with pytest.raises(FallbackQuotaReorderError, match='deprecated key "default"'):
            write_fallback_order(
                [{"provider": "xai-oauth", "model": "grok-4"}],
                (("xai-oauth", "grok-4", ""),),
            )
        assert (tmp_path / "config.yaml").read_bytes() == original
        backup_root = tmp_path / "config-backups" / "fallback_quota_reorder"
        assert not backup_root.exists() or not any(backup_root.iterdir())


class TestRunReorderWritePath:
    def test_no_change_skips_backup_and_config_write(self, monkeypatch, tmp_path: Path):
        names = default_channel_names()
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "kimi-coding", "model": "kimi"},
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "zai", "model": "zai"},
            {"provider": "cursor", "model": "cursor"},
        ]
        write_hermes_home(tmp_path, fallback_providers=entries)
        quota_config = tmp_path / "quota-config.yaml"
        write_quota_config_path(quota_config)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        original = (tmp_path / "config.yaml").read_bytes()

        run_reorder(
            config_path=quota_config,
            http_fn=fake_http_for_names(names),
        )
        assert (tmp_path / "config.yaml").read_bytes() == original
        backup_root = tmp_path / "config-backups" / "fallback_quota_reorder"
        assert not backup_root.exists() or not any(backup_root.iterdir())

    def test_successful_reorder_matches_desired_signature(
        self, monkeypatch, tmp_path: Path
    ):
        names = default_channel_names()
        names["grok"] = "Grok: 60% • 1h left"
        names["kimi"] = "Kimi: 80% • 7d left"
        names["codex"] = "Codex: 90% • 7d left"
        current_entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "kimi-coding", "model": "kimi"},
        ]
        write_hermes_home(tmp_path, fallback_providers=current_entries)
        quota_config = tmp_path / "quota-config.yaml"
        write_quota_config_path(quota_config)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = run_reorder(
            config_path=quota_config,
            http_fn=fake_http_for_names(names),
        )
        assert result["would_change"] is True
        loaded = load_config()
        assert chain_signature(loaded) == result["desired_signature"]
        assert order_signature(get_fallback_chain(loaded)) == result["desired_signature"]

    def test_verification_failure_restores_backup(self, monkeypatch, tmp_path: Path):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "kimi-coding", "model": "kimi"},
        ]
        write_hermes_home(tmp_path, fallback_providers=entries)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        original_chain = [
            _entry_identity(entry) for entry in get_fallback_chain(load_config())
        ]
        desired = [
            {"provider": "kimi-coding", "model": "kimi"},
            {"provider": "openrouter", "model": "or"},
            {"provider": "xai-oauth", "model": "grok"},
        ]
        bogus_signature = tuple(reversed(order_signature(entries)))

        with pytest.raises(FallbackQuotaReorderError, match="verification failed"):
            write_fallback_order(desired, bogus_signature)

        restored_chain = [
            _entry_identity(entry) for entry in get_fallback_chain(load_config())
        ]
        assert restored_chain == original_chain
        backups = list(backup_dir().glob("config-*.yaml"))
        assert backups
