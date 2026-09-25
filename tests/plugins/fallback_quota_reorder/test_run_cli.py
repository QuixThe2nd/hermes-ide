"""CLI dry-run output and no-write contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.fallback_quota_reorder.core import QuotaReading, run_reorder, state_path
from plugins.fallback_quota_reorder.run import main
from tests.plugins.fallback_quota_reorder._helpers import (
    default_channel_names,
    fake_http_for_names,
    write_hermes_home,
    write_quota_config_path,
)


class TestCliDryRun:
    def test_dry_run_prints_sections_and_passes_flag(self, monkeypatch, capsys):
        captured: dict = {}

        def fake_run_reorder(**kwargs):
            captured.update(kwargs)
            return {
                "names": {},
                "readings": {
                    "xai-oauth": QuotaReading(
                        channel_key="grok",
                        provider="xai-oauth",
                        channel_name="Grok: 60% • 1h left",
                        pct=60,
                        reset_seconds=3600,
                    )
                },
                "current_entries": [{"provider": "openrouter", "model": "or"}],
                "desired_entries": [{"provider": "xai-oauth", "model": "grok"}],
                "current_signature": (("openrouter", "or", ""),),
                "desired_signature": (("xai-oauth", "grok", ""),),
                "would_change": True,
                "frozen": False,
                "consecutive_stale": 0,
            }

        monkeypatch.setattr(
            "plugins.fallback_quota_reorder.core.run_reorder", fake_run_reorder
        )
        exit_code = main(["--dry-run"])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert captured.get("dry_run") is True
        assert any(line.startswith("READINGS:") for line in out.splitlines())
        assert any(line.startswith("CURRENT:") for line in out.splitlines())
        assert any(line.startswith("DESIRED:") for line in out.splitlines())
        assert any(line.startswith("CHANGE:") for line in out.splitlines())

    def test_dry_run_frozen_prints_staleness_message(self, monkeypatch, capsys):
        def fake_run_reorder(**kwargs):
            return {
                "names": {},
                "readings": {},
                "current_entries": [{"provider": "openrouter", "model": "or"}],
                "desired_entries": [{"provider": "xai-oauth", "model": "grok"}],
                "current_signature": (("openrouter", "or", ""),),
                "desired_signature": (("xai-oauth", "grok", ""),),
                "would_change": False,
                "frozen": True,
                "consecutive_stale": 2,
            }

        monkeypatch.setattr(
            "plugins.fallback_quota_reorder.core.run_reorder", fake_run_reorder
        )
        exit_code = main(["--dry-run"])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "CHANGE: no (staleness freeze active)" in out


class TestRunReorderDryRun:
    def test_dry_run_writes_no_state_or_config(self, monkeypatch, tmp_path: Path):
        names = default_channel_names()
        write_hermes_home(
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "xai-oauth", "model": "grok"},
                {"provider": "kimi-coding", "model": "kimi"},
            ],
        )
        quota_config = tmp_path / "quota-config.yaml"
        write_quota_config_path(quota_config)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        original = (tmp_path / "config.yaml").read_bytes()

        run_reorder(
            config_path=quota_config,
            dry_run=True,
            http_fn=fake_http_for_names(names),
        )

        assert not state_path().exists()
        assert (tmp_path / "config.yaml").read_bytes() == original
