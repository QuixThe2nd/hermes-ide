"""Per-credential Z.AI wallet rows on the Discord Models wall."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime

import pytest

from plugins.quota_channels import core as core
from plugins.quota_channels.core import run_tick, state_path, validate_quota_config
from plugins.quota_channels.zai_wallets import (
    LEGACY_ENV_WALLET_ID,
    assign_wallet_ordinals,
    enumerate_zai_wallets,
    pick_best_zai_reading,
    wallet_reading_key,
)


DAY = 86400
BULLET = "\u2022"


def _five_id_section() -> dict:
    return {
        "guild_id": "guild",
        "category_id": "cat",
        "channel_ids": {
            "codex": "c1",
            "kimi": "c2",
            "zai": "c3",
            "cursor": "c4",
            "grok": "c5",
        },
    }


def _write_pool(tmp_path, entries):
    auth = {"credential_pool": {"zai": entries}}
    (tmp_path / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


class _WalletDiscord:
    def __init__(self, guild_channels, existing=None, missing=None):
        self.guild_channels = list(guild_channels)
        self.existing = set(existing or ())
        self.missing = set(missing or ())
        self.renames = []
        self.creates = []
        self.deletes = []
        self.position_patch = None
        self._next_id = 900

    def __call__(self, req, timeout=25.0):
        method = (getattr(req, "method", None) or req.get_method()).upper()
        path = urllib.parse.urlsplit(req.full_url).path
        body = json.loads(req.data.decode()) if req.data else None
        if method == "GET" and path.endswith("/channels"):
            return 200, json.dumps(self.guild_channels).encode()
        if method == "GET":
            cid = path.rsplit("/", 1)[-1]
            if cid in self.missing:
                return 404, b'{"message": "Unknown Channel"}'
            return 200, json.dumps({"name": "old-name"}).encode()
        if method == "POST" and "/guilds/" in path and path.endswith("/channels"):
            self._next_id += 1
            cid = f"new{self._next_id}"
            self.creates.append(body)
            self.guild_channels.append({"id": cid, "position": 20 + len(self.creates)})
            self.existing.add(cid)
            return 201, json.dumps({"id": cid}).encode()
        if method == "DELETE":
            cid = path.rsplit("/", 1)[-1]
            self.deletes.append(cid)
            self.guild_channels = [c for c in self.guild_channels if c["id"] != cid]
            return 204, b""
        if method == "PATCH" and path.endswith("/channels"):
            self.position_patch = body
            return 200, b"[]"
        if method == "PATCH":
            self.renames.append((path.rsplit("/", 1)[-1], body))
            return 200, json.dumps({"name": body.get("name")}).encode()
        raise AssertionError((method, path))


def _metrics_for_key(key: str):
    if key == "sk-secret-wallet-aaa":
        return (80, 2 * DAY, None, None)
    if key == "sk-secret-wallet-bbb":
        return (40, 5 * DAY, None, None)
    return (50, 3 * DAY, None, None)


@pytest.fixture
def wallet_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
    monkeypatch.setattr(core, "TOKEN_FETCHERS", {})

    def fake_metrics(http_fn=None, now_fn=None, api_key=None):
        return _metrics_for_key(api_key or "")

    monkeypatch.setattr(core, "_zai_quota_metrics", fake_metrics)
    monkeypatch.setattr(
        core,
        "QUOTA_METRICS",
        {
            "codex": lambda http_fn=None, now_fn=None: (95, DAY),
            "kimi": lambda http_fn=None, now_fn=None: (60, DAY),
            "cursor": lambda http_fn=None, now_fn=None: (38, 50, 22 * DAY),
            "grok": lambda http_fn=None, now_fn=None: (81, 5 * DAY),
            "zai": lambda http_fn=None, now_fn=None: (23, 3 * DAY),
        },
    )
    return tmp_path


class TestEnumerateAndOrdinals:
    def test_duplicate_runtime_keys_deduped_in_memory_only(self, wallet_env):
        _write_pool(
            wallet_env,
            [
                {"id": "a1", "access_token": "same-key", "label": "first"},
                {"id": "a2", "access_token": "same-key", "label": "second"},
            ],
        )
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert not unreadable
        assert len(wallets) == 1
        assert wallets[0].entry_id == "a1"
        (wallet_env / "quota_channels_state.json").write_text("{}", encoding="utf-8")
        assert "same-key" not in (wallet_env / "quota_channels_state.json").read_text()

    def test_stable_ordinals_across_reorder_and_relabel(self, wallet_env):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "k1", "label": "alpha"},
                {"id": "w2", "access_token": "k2", "label": "beta"},
            ],
        )
        ordinals1, hw1 = assign_wallet_ordinals(
            enumerate_zai_wallets(wallet_env)[0], {}
        )
        assert ordinals1 == {"w1": 1, "w2": 2}
        _write_pool(
            wallet_env,
            [
                {"id": "w2", "access_token": "k2", "label": "renamed"},
                {"id": "w1", "access_token": "k1", "label": "alpha"},
            ],
        )
        wallets2, _ = enumerate_zai_wallets(wallet_env)
        ordinals2, hw2 = assign_wallet_ordinals(
            wallets2, {"zai_wallet_ordinals": ordinals1, "zai_wallet_ordinal_high_water": hw1}
        )
        assert ordinals2 == {"w1": 1, "w2": 2}
        assert hw2 == 2


class TestWalletTick:
    def test_two_wallets_independent_ranks(self, wallet_env):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "sk-secret-wallet-aaa"},
                {"id": "w2", "access_token": "sk-secret-wallet-bbb"},
            ],
        )
        discord = _WalletDiscord(
            [
                {"id": "c1", "position": 10},
                {"id": "c2", "position": 11},
                {"id": "c3", "position": 12},
                {"id": "c4", "position": 13},
                {"id": "c5", "position": 14},
                {"id": "new901", "position": 15},
            ],
            existing={"c3", "new901"},
        )
        config = validate_quota_config(_five_id_section())
        now = datetime(2026, 8, 25, 14, 0, 0).timestamp()
        result = run_tick(
            config, force=True, now_fn=lambda: now, http_fn=discord, sleep_fn=lambda _: None
        )
        assert result["success"]
        renames = {cid: body["name"] for cid, body in discord.renames}
        assert "sk-secret-wallet-aaa" not in json.dumps(renames)
        assert "sk-secret-wallet-bbb" not in json.dumps(renames)
        assert any(name.startswith("z.ai 1:") for name in renames.values())
        assert any(name.startswith("z.ai 2:") for name in renames.values())
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert wallet_reading_key("w1") in state["readings"]
        assert wallet_reading_key("w2") in state["readings"]
        assert state["readings"]["zai"]["pct"] == 80

    def test_legacy_migration_binds_first_wallet_and_creates_second(self, wallet_env):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "k1"},
                {"id": "w2", "access_token": "k2"},
            ],
        )
        discord = _WalletDiscord(
            [
                {"id": "c3", "position": 12},
                {"id": "c5", "position": 14},
            ],
            existing={"c3"},
        )
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "c3", "grok": "c5"},
                "enabled_providers": ["zai", "grok"],
            }
        )
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_000.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        assert len(discord.creates) == 1
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["zai_wallet_channels"]["w1"] == "c3"
        assert "w2" in state["zai_wallet_channels"]
        discord2 = _WalletDiscord(
            [
                {"id": "c3", "position": 12},
                {"id": state["zai_wallet_channels"]["w2"], "position": 16},
                {"id": "c5", "position": 14},
            ],
            existing={"c3", state["zai_wallet_channels"]["w2"]},
        )
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_100.0,
            http_fn=discord2,
            sleep_fn=lambda _: None,
        )
        assert len(discord2.creates) == 0

    def test_removed_wallet_deletes_owned_channel_when_pool_readable(self, wallet_env):
        _write_pool(
            wallet_env,
            [{"id": "w1", "access_token": "k1"}],
        )
        discord = _WalletDiscord([{"id": "c3", "position": 12}], existing={"c3"})
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "c3"},
                "enabled_providers": ["zai"],
            }
        )
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_000.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        state = json.loads(state_path().read_text(encoding="utf-8"))
        state["zai_wallet_channels"]["gone"] = "orphan"
        state["zai_wallet_ordinals"]["gone"] = 9
        state_path().write_text(json.dumps(state), encoding="utf-8")
        _write_pool(wallet_env, [{"id": "w1", "access_token": "k1"}])
        discord2 = _WalletDiscord([{"id": "c3", "position": 12}], existing={"c3"})
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_200.0,
            http_fn=discord2,
            sleep_fn=lambda _: None,
        )
        assert "orphan" in discord2.deletes

    def test_unreadable_pool_is_non_destructive(self, wallet_env, monkeypatch):
        state = {
            "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(state), encoding="utf-8")
        (wallet_env / "auth.json").write_text("{not-json", encoding="utf-8")
        discord = _WalletDiscord(
            [
                {"id": "c3", "position": 12},
                {"id": "extra", "position": 13},
                {"id": "c5", "position": 14},
            ],
            existing={"c3", "extra", "c5"},
        )
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "c3", "grok": "c5"},
                "enabled_providers": ["grok"],
            }
        )
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            {"grok": lambda http_fn=None, now_fn=None: (81, 5 * DAY)},
        )
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_000.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        assert discord.deletes == []
        saved = json.loads(state_path().read_text(encoding="utf-8"))
        assert saved["zai_wallet_channels"] == state["zai_wallet_channels"]

    def test_one_wallet_fetch_failure_isolates_sibling(self, wallet_env, monkeypatch):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "sk-secret-wallet-aaa"},
                {"id": "w2", "access_token": "sk-secret-wallet-bbb"},
            ],
        )

        real = core._zai_quota_metrics

        def flaky(http_fn=None, now_fn=None, api_key=None):
            if api_key == "sk-secret-wallet-bbb":
                raise core.QuotaChannelsError(
                    f"z.ai usage endpoint returned 500: sk-secret-wallet-bbb"
                )
            return real(http_fn=http_fn, now_fn=now_fn, api_key=api_key)

        monkeypatch.setattr(core, "_zai_quota_metrics", flaky)
        discord = _WalletDiscord(
            [{"id": "c3", "position": 12}, {"id": "new901", "position": 13}],
            existing={"c3", "new901"},
        )
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "c3"},
                "enabled_providers": ["zai"],
            }
        )
        result = run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_000.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        assert "sk-secret-wallet-bbb" not in json.dumps(result)
        assert result["providers"]["z.ai 2"]["error"]
        assert result["providers"]["z.ai 1"]["remaining"] == 80
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"][wallet_reading_key("w1")]["pct"] == 80

    def test_missing_channel_recreated(self, wallet_env):
        _write_pool(wallet_env, [{"id": "w1", "access_token": "k1"}])
        discord = _WalletDiscord(
            [{"id": "c3", "position": 12}],
            existing=set(),
            missing={"c3"},
        )
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "c3"},
                "enabled_providers": ["zai"],
            }
        )
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_000.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        assert len(discord.creates) == 1
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["zai_wallet_channels"]["w1"] != "c3"


class TestBestWalletAlias:
    def test_healthy_beats_sunk_then_score(self):
        readings = {
            "zai:w1": {"pct": 2, "reset_seconds": 60, "label": "z.ai 1"},
            "zai:w2": {"pct": 50, "reset_seconds": DAY, "label": "z.ai 2"},
        }
        alias = pick_best_zai_reading(readings)
        assert alias is not None
        assert alias["pct"] == 50

    def test_higher_score_wins_among_healthy(self):
        readings = {
            "zai:w1": {"pct": 30, "reset_seconds": DAY, "label": "z.ai 1"},
            "zai:w2": {"pct": 90, "reset_seconds": DAY, "label": "z.ai 2"},
        }
        alias = pick_best_zai_reading(readings)
        assert alias["pct"] == 90


class TestLegacySingleWallet:
    def test_env_only_legacy_path(self, wallet_env):
        secrets = wallet_env / "secrets"
        secrets.mkdir()
        (secrets / "zai.env").write_text('ZAI_API_KEY="solo-key"\n', encoding="utf-8")
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert not unreadable
        assert len(wallets) == 1
        assert wallets[0].entry_id == LEGACY_ENV_WALLET_ID
