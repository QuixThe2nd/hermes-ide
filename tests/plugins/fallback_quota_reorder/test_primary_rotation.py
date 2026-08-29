"""Primary model slot rotation behavior contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from hermes_cli.config import load_config
from hermes_cli.fallback_config import get_fallback_chain
from plugins.fallback_quota_reorder import core
from plugins.fallback_quota_reorder.core import (
    FallbackQuotaReorderError,
    PrimarySlot,
    backup_dir,
    order_signature,
    run_reorder,
    save_state,
    write_fallback_order,
)
from plugins.fallback_quota_reorder.reliability import ReliabilityRates
from plugins.fallback_quota_reorder.run import main
from tests.plugins.fallback_quota_reorder._helpers import (
    BULLET,
    CHANNEL_IDS,
    default_channel_names,
    write_hermes_home,
    write_quota_config_path,
)


def _names(**overrides: str) -> dict[str, str]:
    names = {key: "" for key in default_channel_names()}
    names.update(overrides)
    return names


def _setup(
    monkeypatch,
    tmp_path: Path,
    *,
    fallback_providers: list[dict[str, Any]],
    model: Mapping[str, str],
) -> Path:
    write_hermes_home(
        tmp_path,
        fallback_providers=fallback_providers,
        extra_config={"model": dict(model)},
    )
    quota_config = tmp_path / "quota-config.yaml"
    write_quota_config_path(quota_config)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return quota_config


def _patch_channel_names(monkeypatch, names: Mapping[str, str]) -> None:
    monkeypatch.setattr(
        core, "fetch_channel_names", lambda channel_ids, http_fn=None: dict(names)
    )


class TestPrimaryPromotion:
    def test_top_scorer_promoted_and_displaced_primary_lands_in_chain(
        self, monkeypatch, tmp_path: Path
    ):
        names = _names(
            codex=f"Codex: 90% {BULLET} 7d left",  # 0.9 (quota at reference horizon)
            kimi=f"Kimi: 80% {BULLET} 7d left",  # 0.8
        )
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
                {"provider": "zai", "model": "zai"},
            ],
            model={"provider": "kimi-coding", "default": "kimi"},
        )
        _patch_channel_names(monkeypatch, names)

        result = run_reorder(config_path=quota_config)

        assert result["primary_desired"] == PrimarySlot(
            provider="openai-codex", model="codex"
        )
        assert result["would_change"] is True
        loaded = load_config()
        assert loaded["model"]["provider"] == "openai-codex"
        assert loaded["model"]["default"] == "codex"
        # displaced kimi joins the healthy bucket ahead of the unscored tail
        # (plain openrouter has no reading anymore, so it no longer floats)
        providers = [entry["provider"] for entry in get_fallback_chain(loaded)]
        assert providers == ["kimi-coding", "openrouter", "xai-oauth", "zai"]

    def test_top_scorer_without_chain_entry_promotes_best_with_entry(
        self, monkeypatch, tmp_path: Path
    ):
        # zai (0.92) and cursor (0.90) outscore every chain member but
        # have no desired_entries entry, so neither can source a model string
        names = _names(
            zai=f"z.ai: 92% {BULLET} 7d left",
            cursor=f"Cursor: 95%/90% {BULLET} 7d left",
            codex=f"Codex: 85% {BULLET} 7d left",  # 0.85, best WITH an entry
            kimi=f"Kimi: 40% {BULLET} 7d left",  # 0.40, current primary
            grok=f"Grok: 60% {BULLET} 7d left",  # 0.6
        )
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
            ],
            model={"provider": "kimi-coding", "default": "kimi"},
        )
        _patch_channel_names(monkeypatch, names)

        result = run_reorder(config_path=quota_config)

        assert result["primary_desired"] == PrimarySlot(
            provider="openai-codex", model="codex"
        )
        assert result["would_change"] is True
        loaded = load_config()
        assert loaded["model"]["provider"] == "openai-codex"
        assert loaded["model"]["default"] == "codex"
        providers = [entry["provider"] for entry in get_fallback_chain(loaded)]
        # displaced kimi (0.40) reinserts behind the healthier grok (0.6)
        assert providers == ["xai-oauth", "kimi-coding"]

    def test_tie_keeps_current_primary(self, monkeypatch, tmp_path: Path):
        names = _names(
            codex=f"Codex: 90% {BULLET} 7d left",
            kimi=f"Kimi: 90% {BULLET} 7d left",
        )
        # openrouter/or sits last because unscored entries tail now, so the
        # tie (not the openrouter demotion) is the only thing under test
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "kimi-coding", "model": "kimi"},
                {"provider": "openrouter", "model": "or"},
            ],
            model={"provider": "openai-codex", "default": "codex"},
        )
        _patch_channel_names(monkeypatch, names)
        original = (tmp_path / "config.yaml").read_bytes()

        result = run_reorder(config_path=quota_config)

        assert result["primary_desired"] is None
        assert result["would_change"] is False
        assert (tmp_path / "config.yaml").read_bytes() == original

    def test_untracked_primary_displaced_and_lands_at_chain_end(
        self, monkeypatch, tmp_path: Path
    ):
        names = _names(codex=f"Codex: 90% {BULLET} 7d left")
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
                {"provider": "zai", "model": "zai"},
            ],
            model={"provider": "openrouter", "default": "or"},
        )
        _patch_channel_names(monkeypatch, names)

        result = run_reorder(config_path=quota_config)

        assert result["primary_desired"] == PrimarySlot(
            provider="openai-codex", model="codex"
        )
        loaded = load_config()
        assert loaded["model"]["provider"] == "openai-codex"
        assert loaded["model"]["default"] == "codex"
        # displaced openrouter does NOT float back to the front: untracked
        # means score 0, so it lands in the unscored tail
        providers = [entry["provider"] for entry in get_fallback_chain(loaded)]
        assert providers == ["xai-oauth", "zai", "openrouter"]


class TestUnlimitedPrimaryRotation:
    """The unlimited openrouter/stealth/ox-alpha route competes for primary."""

    def test_unlimited_route_promotes_and_displaced_reinserts(
        self, monkeypatch, tmp_path: Path
    ):
        names = _names(
            codex=f"Codex: 90% {BULLET} 7d left",  # 0.9
            kimi=f"Kimi: 80% {BULLET} 7d left",  # 0.8, current primary
        )
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "stealth/ox-alpha"},
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
            ],
            model={"provider": "kimi-coding", "default": "kimi"},
        )
        _patch_channel_names(monkeypatch, names)

        result = run_reorder(config_path=quota_config)

        # synthetic full wallet at the reference horizon: exactly 1.0, so
        # ox-alpha beats codex (0.9) and the current primary kimi (0.8)
        assert result["primary_desired"] == PrimarySlot(
            provider="openrouter", model="stealth/ox-alpha"
        )
        assert result["would_change"] is True
        loaded = load_config()
        assert loaded["model"]["provider"] == "openrouter"
        assert loaded["model"]["default"] == "stealth/ox-alpha"
        providers = [entry["provider"] for entry in get_fallback_chain(loaded)]
        # the promoted entry graduates out of the chain; displaced kimi (0.8)
        # reinserts ahead of the unscored tail but behind codex (0.9)
        assert providers == ["openai-codex", "kimi-coding", "xai-oauth"]

    def test_reliability_derates_an_already_primary_unlimited_route(
        self, monkeypatch, tmp_path: Path
    ):
        names = _names(codex=f"Codex: 90% {BULLET} 7d left")  # 0.9
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
            ],
            model={"provider": "openrouter", "default": "stealth/ox-alpha"},
        )
        _patch_channel_names(monkeypatch, names)
        monkeypatch.setattr(
            core,
            "rates_for_providers",
            lambda providers, **kwargs: {
                "openrouter": ReliabilityRates(rate_24h=0.5, rate_1h=1.0)
            },
        )

        result = run_reorder(config_path=quota_config)

        # derated ox-alpha (1.0 * 0.5 = 0.5) loses the slot to codex (0.9)
        assert result["primary_desired"] == PrimarySlot(
            provider="openai-codex", model="codex"
        )
        loaded = load_config()
        assert loaded["model"]["provider"] == "openai-codex"
        providers = [entry["provider"] for entry in get_fallback_chain(loaded)]
        # displaced ox-alpha reenters by its derated synthetic 0.5, ahead
        # of the unscored tail
        assert providers == ["openrouter", "xai-oauth"]


class TestPrimaryFreezeAndNoReadings:
    def test_frozen_blocks_primary_write(self, monkeypatch, tmp_path: Path):
        names = _names(
            codex=f"Codex: 90% {BULLET} 7d left",
            kimi=f"Kimi: 80% {BULLET} 7d left",
            grok=f"Grok: 60% {BULLET} 1h left",
        )
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
            ],
            model={"provider": "kimi-coding", "default": "kimi"},
        )
        _patch_channel_names(monkeypatch, names)
        save_state(
            {
                "last_names": {key: names.get(key, "") for key in CHANNEL_IDS},
                "last_timestamp": 1_000_000,
                "consecutive_stale": 2,
            }
        )
        original = (tmp_path / "config.yaml").read_bytes()

        result = run_reorder(config_path=quota_config, now_fn=lambda: 1_000_100)

        assert result["frozen"] is True
        # the swap is still computed for visibility, but nothing is written
        assert result["primary_desired"] is not None
        assert result["would_change"] is False
        assert (tmp_path / "config.yaml").read_bytes() == original

    def test_no_readings_leaves_primary_alone(self, monkeypatch, tmp_path: Path):
        names = _names()
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "kimi-coding", "model": "kimi"},
            ],
            model={"provider": "openai-codex", "default": "codex"},
        )
        _patch_channel_names(monkeypatch, names)
        original = (tmp_path / "config.yaml").read_bytes()

        result = run_reorder(config_path=quota_config)

        assert result["readings"] == {}
        assert result["primary_desired"] is None
        assert result["would_change"] is False
        assert (tmp_path / "config.yaml").read_bytes() == original


class TestPrimaryWriteRollback:
    def test_verification_failure_restores_primary_and_chain(
        self, monkeypatch, tmp_path: Path
    ):
        _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
            ],
            model={"provider": "kimi-coding", "default": "kimi"},
        )
        from hermes_cli import config as config_module

        real_load_config = config_module.load_config
        calls = {"count": 0}

        def load_losing_primary():
            config = real_load_config()
            calls["count"] += 1
            if calls["count"] == 2:  # the post-write verification read
                config["model"] = {"provider": "kimi-coding", "default": "kimi"}
            return config

        monkeypatch.setattr(config_module, "load_config", load_losing_primary)

        desired = [
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "kimi-coding", "model": "kimi"},
        ]
        with pytest.raises(
            FallbackQuotaReorderError, match="verification failed: primary"
        ):
            write_fallback_order(
                desired,
                order_signature(desired),
                primary_slot=PrimarySlot(provider="openai-codex", model="codex"),
            )

        on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert on_disk["model"] == {"provider": "kimi-coding", "default": "kimi"}
        assert [entry["model"] for entry in on_disk["fallback_providers"]] == [
            "codex",
            "grok",
        ]
        assert list(backup_dir().glob("config-*.yaml"))


class TestCliDryRunPrimaryLine:
    def test_dry_run_prints_pending_primary_swap(
        self, monkeypatch, tmp_path: Path, capsys
    ):
        names = _names(
            codex=f"Codex: 90% {BULLET} 7d left",
            kimi=f"Kimi: 80% {BULLET} 7d left",
        )
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
                {"provider": "zai", "model": "zai"},
            ],
            model={"provider": "kimi-coding", "default": "kimi"},
        )
        _patch_channel_names(monkeypatch, names)
        original = (tmp_path / "config.yaml").read_bytes()

        exit_code = main(["--dry-run", "--config", str(quota_config)])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "PRIMARY: kimi-coding/kimi -> openai-codex/codex" in out
        assert (tmp_path / "config.yaml").read_bytes() == original

    def test_dry_run_prints_unchanged_primary(
        self, monkeypatch, tmp_path: Path, capsys
    ):
        names = _names(
            codex=f"Codex: 90% {BULLET} 7d left",
            kimi=f"Kimi: 90% {BULLET} 7d left",
        )
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "kimi-coding", "model": "kimi"},
            ],
            model={"provider": "openai-codex", "default": "codex"},
        )
        _patch_channel_names(monkeypatch, names)

        exit_code = main(["--dry-run", "--config", str(quota_config)])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "PRIMARY: unchanged openai-codex/codex" in out

    def test_dry_run_prints_no_primary_line_without_readings(
        self, monkeypatch, tmp_path: Path, capsys
    ):
        names = _names()
        quota_config = _setup(
            monkeypatch,
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "openai-codex", "model": "codex"},
            ],
            model={"provider": "openai-codex", "default": "codex"},
        )
        _patch_channel_names(monkeypatch, names)

        exit_code = main(["--dry-run", "--config", str(quota_config)])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert not any(
            line.startswith("PRIMARY:") for line in out.splitlines()
        )
