"""Per-credential Z.AI wallet rows on the Discord Models wall."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from datetime import datetime

import pytest

from plugins.quota_channels import core as core
from plugins.quota_channels.core import run_tick, save_wallet_state, state_path, validate_quota_config
from plugins.quota_channels.zai_wallets import (
    LEGACY_ENV_WALLET_ID,
    ZaiWalletError,
    assign_wallet_ordinals,
    enumerate_zai_wallets,
    pick_best_zai_reading,
    reconcile_zai_wallet_channels,
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


def _fp(key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"sha256:{digest}"


class _WalletDiscord:
    def __init__(self, guild_channels, existing=None, missing=None, get_status=None):
        self.guild_channels = list(guild_channels)
        self.existing = set(existing or ())
        self.missing = set(missing or ())
        self.get_status = dict(get_status or ())
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
            if cid in self.get_status:
                status = self.get_status[cid]
                return status, json.dumps({"message": "error"}).encode()
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


class TestEnvSeededPoolEntries:
    def test_env_seeded_entry_resolves_via_hermes_env(self, wallet_env):
        (wallet_env / ".env").write_text('GLM_API_KEY="sk-env-alpha"\n', encoding="utf-8")
        _write_pool(
            wallet_env,
            [
                {
                    "id": "e1",
                    "label": "glm",
                    "auth_type": "api_key",
                    "source": "env:GLM_API_KEY",
                    "api_key": "",
                    "secret_fingerprint": _fp("sk-env-alpha"),
                }
            ],
        )
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert not unreadable
        assert len(wallets) == 1
        assert wallets[0].entry_id == "e1"
        assert wallets[0].runtime_api_key == "sk-env-alpha"
        assert wallets[0].pool_label == "glm"

    def test_env_seeded_fingerprint_mismatch_is_unreadable_and_blocks_deletes(
        self, wallet_env, monkeypatch
    ):
        (wallet_env / ".env").write_text("GLM_API_KEY=sk-env-rotated\n", encoding="utf-8")
        _write_pool(
            wallet_env,
            [
                {
                    "id": "e1",
                    "auth_type": "api_key",
                    "source": "env:GLM_API_KEY",
                    "api_key": "",
                    "secret_fingerprint": _fp("sk-env-original"),
                }
            ],
        )
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert unreadable
        assert wallets == []
        prior = {
            "last_quota_success": 777,
            "readings": {
                wallet_reading_key("e1"): {
                    "pct": 40,
                    "reset_seconds": DAY,
                    "label": "z.ai 1",
                },
            },
            "zai_wallet_channels": {"e1": "c3", "gone": "orphan"},
            "zai_wallet_ordinals": {"e1": 1, "gone": 2},
            "zai_wallet_ordinal_high_water": 2,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior), encoding="utf-8")
        discord = _WalletDiscord(
            [
                {"id": "c3", "position": 12},
                {"id": "orphan", "position": 13},
                {"id": "c5", "position": 14},
            ],
            existing={"c3", "orphan", "c5"},
        )
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "c3", "grok": "c5"},
                "enabled_providers": ["zai", "grok"],
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
        assert saved["zai_wallet_channels"] == prior["zai_wallet_channels"]
        assert saved["zai_wallet_ordinals"] == prior["zai_wallet_ordinals"]

    def test_env_seeded_variable_missing_from_hermes_env(self, wallet_env):
        (wallet_env / ".env").write_text("OTHER_API_KEY=sk-unrelated\n", encoding="utf-8")
        _write_pool(
            wallet_env,
            [
                {
                    "id": "e1",
                    "auth_type": "api_key",
                    "source": "env:GLM_API_KEY",
                    "api_key": "",
                    "secret_fingerprint": _fp("sk-env-alpha"),
                }
            ],
        )
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert unreadable
        assert wallets == []

    def test_two_env_seeded_entries_yield_two_wallets_in_pool_order(self, wallet_env):
        (wallet_env / ".env").write_text(
            'GLM_API_KEY="sk-env-alpha"\nZAI_API_KEY=sk-env-beta\n',
            encoding="utf-8",
        )
        _write_pool(
            wallet_env,
            [
                {
                    "id": "e1",
                    "label": "glm",
                    "auth_type": "api_key",
                    "source": "env:GLM_API_KEY",
                    "api_key": "",
                    "secret_fingerprint": _fp("sk-env-alpha"),
                },
                {
                    "id": "e2",
                    "label": "zai",
                    "auth_type": "api_key",
                    "source": "env:ZAI_API_KEY",
                    "api_key": "",
                    "secret_fingerprint": _fp("sk-env-beta"),
                },
            ],
        )
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert not unreadable
        assert [(w.entry_id, w.runtime_api_key) for w in wallets] == [
            ("e1", "sk-env-alpha"),
            ("e2", "sk-env-beta"),
        ]
        ordinals, high_water = assign_wallet_ordinals(wallets, {})
        assert ordinals == {"e1": 1, "e2": 2}
        assert high_water == 2

    def test_inline_access_token_wins_over_env_seed(self, wallet_env):
        (wallet_env / ".env").write_text("GLM_API_KEY=sk-env-alpha\n", encoding="utf-8")
        _write_pool(
            wallet_env,
            [
                {
                    "id": "e1",
                    "auth_type": "api_key",
                    "source": "env:GLM_API_KEY",
                    "access_token": "sk-inline-key",
                    "api_key": "",
                    "secret_fingerprint": _fp("sk-not-the-env-value"),
                }
            ],
        )
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert not unreadable
        assert len(wallets) == 1
        assert wallets[0].runtime_api_key == "sk-inline-key"


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
                {"id": "c3", "position": 15},
                {"id": "c4", "position": 13},
                {"id": "c5", "position": 14},
                {"id": "new901", "position": 12},
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
        w1_cid = state["zai_wallet_channels"]["w1"]
        w2_cid = state["zai_wallet_channels"]["w2"]
        assert discord.position_patch is not None
        patch_ids = {move["id"] for move in discord.position_patch}
        assert w1_cid in patch_ids
        assert w2_cid in patch_ids
        positions = {move["id"]: move["position"] for move in discord.position_patch}
        assert positions[w1_cid] < positions[w2_cid]

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
                "enabled_providers": ["zai", "grok"],
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
        assert saved["zai_wallet_ordinals"] == state["zai_wallet_ordinals"]

    def test_one_wallet_fetch_failure_isolates_sibling(self, wallet_env, monkeypatch):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "sk-secret-wallet-aaa"},
                {"id": "w2", "access_token": "sk-secret-wallet-bbb"},
            ],
        )
        prior_state = {
            "last_quota_success": 999,
            "readings": {
                wallet_reading_key("w1"): {
                    "pct": 10,
                    "reset_seconds": DAY,
                    "label": "z.ai 1",
                },
                wallet_reading_key("w2"): {
                    "pct": 55,
                    "reset_seconds": DAY,
                    "label": "z.ai 2",
                },
                "zai": {"pct": 55, "reset_seconds": DAY, "label": "z.ai"},
            },
            "zai_wallet_channels": {"w1": "c3", "w2": "new901"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior_state), encoding="utf-8")

        real = core._zai_quota_metrics

        def flaky(http_fn=None, now_fn=None, api_key=None):
            if api_key == "sk-secret-wallet-bbb":
                raise core.QuotaChannelsError(
                    "z.ai usage endpoint returned 500: sk-secret-wallet-bbb"
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
        blob = json.dumps(result)
        assert "sk-secret-wallet-aaa" not in blob
        assert "sk-secret-wallet-bbb" not in blob
        assert result["providers"]["z.ai 2"]["error"]
        assert result["providers"]["z.ai 1"]["remaining"] == 80
        assert discord.deletes == []
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"][wallet_reading_key("w1")]["pct"] == 80
        assert state["readings"][wallet_reading_key("w2")]["pct"] == 55
        renames = {cid: body["name"] for cid, body in discord.renames}
        assert "sk-secret-wallet-aaa" not in json.dumps(renames)
        assert "sk-secret-wallet-bbb" not in json.dumps(renames)
        assert "sk-secret-wallet-aaa" not in json.dumps(state)
        assert "sk-secret-wallet-bbb" not in json.dumps(state)

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
        assert unreadable
        assert len(wallets) == 1
        assert wallets[0].entry_id == LEGACY_ENV_WALLET_ID


class TestWalletReconcileRegressions:
    def test_pool_reorder_keeps_distinct_channels(self, wallet_env):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "k1"},
                {"id": "w2", "access_token": "k2"},
            ],
        )
        discord = _WalletDiscord(
            [{"id": "c3", "position": 12}],
            existing={"c3"},
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
        state = json.loads(state_path().read_text(encoding="utf-8"))
        w1_cid = state["zai_wallet_channels"]["w1"]
        w2_cid = state["zai_wallet_channels"]["w2"]
        assert w1_cid == "c3"
        assert w2_cid != "c3"
        assert len(discord.creates) == 1

        _write_pool(
            wallet_env,
            [
                {"id": "w2", "access_token": "k2"},
                {"id": "w1", "access_token": "k1"},
            ],
        )
        discord2 = _WalletDiscord(
            [
                {"id": "c3", "position": 12},
                {"id": w2_cid, "position": 13},
            ],
            existing={"c3", w2_cid},
        )
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_100.0,
            http_fn=discord2,
            sleep_fn=lambda _: None,
        )
        assert len(discord2.creates) == 0
        state2 = json.loads(state_path().read_text(encoding="utf-8"))
        assert state2["zai_wallet_channels"]["w1"] == w1_cid
        assert state2["zai_wallet_channels"]["w2"] == w2_cid

    def test_channel_get_429_does_not_create(self, wallet_env):
        _write_pool(wallet_env, [{"id": "w1", "access_token": "k1"}])
        discord = _WalletDiscord(
            [{"id": "c3", "position": 12}],
            existing={"c3"},
            get_status={"c3": 429},
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
        assert len(discord.creates) == 0
        assert result["providers"]["z.ai"]["error"]
        assert "429" in result["providers"]["z.ai"]["error"]

    def test_channel_get_500_does_not_create(self, wallet_env):
        _write_pool(wallet_env, [{"id": "w1", "access_token": "k1"}])
        discord = _WalletDiscord(
            [{"id": "c3", "position": 12}],
            existing={"c3"},
            get_status={"c3": 500},
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
        assert len(discord.creates) == 0
        assert result["providers"]["z.ai"]["error"]
        assert "500" in result["providers"]["z.ai"]["error"]

    def test_wallet_maps_persist_when_all_quota_fetches_fail(self, wallet_env, monkeypatch):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "k1"},
                {"id": "w2", "access_token": "k2"},
            ],
        )
        prior_lqs = 999_000
        prior_readings = {
            wallet_reading_key("w1"): {"pct": 10, "reset_seconds": DAY, "label": "z.ai 1"},
            "zai": {"pct": 10, "reset_seconds": DAY, "label": "z.ai"},
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(
            json.dumps(
                {
                    "last_quota_success": prior_lqs,
                    "readings": prior_readings,
                    "zai_wallet_channels": {"w1": "c3"},
                    "zai_wallet_ordinals": {"w1": 1},
                    "zai_wallet_ordinal_high_water": 1,
                }
            ),
            encoding="utf-8",
        )
        discord = _WalletDiscord([{"id": "c3", "position": 12}], existing={"c3"})

        def fail_all(http_fn=None, now_fn=None, api_key=None):
            raise core.QuotaChannelsError("z.ai usage endpoint returned 500")

        monkeypatch.setattr(core, "_zai_quota_metrics", fail_all)
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
        w2_cid = state["zai_wallet_channels"]["w2"]
        assert state["last_quota_success"] == prior_lqs
        assert state["readings"] == prior_readings

        discord2 = _WalletDiscord(
            [{"id": "c3", "position": 12}, {"id": w2_cid, "position": 13}],
            existing={"c3", w2_cid},
        )
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_100.0,
            http_fn=discord2,
            sleep_fn=lambda _: None,
        )
        assert len(discord2.creates) == 0

    def test_delete_persisted_when_quota_fetches_fail(self, wallet_env, monkeypatch):
        _write_pool(wallet_env, [{"id": "w1", "access_token": "k1"}])
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(
            json.dumps(
                {
                    "last_quota_success": 888,
                    "readings": {wallet_reading_key("w1"): {"pct": 50, "reset_seconds": DAY, "label": "z.ai 1"}},
                    "zai_wallet_channels": {"w1": "c3", "gone": "orphan"},
                    "zai_wallet_ordinals": {"w1": 1, "gone": 9},
                    "zai_wallet_ordinal_high_water": 9,
                }
            ),
            encoding="utf-8",
        )
        discord = _WalletDiscord([{"id": "c3", "position": 12}], existing={"c3", "orphan"})

        def fail_all(http_fn=None, now_fn=None, api_key=None):
            raise core.QuotaChannelsError("z.ai usage endpoint returned 500")

        monkeypatch.setattr(core, "_zai_quota_metrics", fail_all)
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
        assert "orphan" in discord.deletes
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert "gone" not in state["zai_wallet_channels"]
        assert state["last_quota_success"] == 888

        discord2 = _WalletDiscord([{"id": "c3", "position": 12}], existing={"c3"})
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_100.0,
            http_fn=discord2,
            sleep_fn=lambda _: None,
        )
        assert "orphan" not in discord2.deletes

    def test_missing_auth_preserves_channels_and_readings(self, wallet_env, monkeypatch):
        prior = {
            "last_quota_success": 777,
            "readings": {
                wallet_reading_key("w1"): {"pct": 40, "reset_seconds": DAY, "label": "z.ai 1"},
                wallet_reading_key("w2"): {"pct": 60, "reset_seconds": DAY, "label": "z.ai 2"},
                "zai": {"pct": 60, "reset_seconds": DAY, "label": "z.ai"},
            },
            "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior), encoding="utf-8")
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
                "enabled_providers": ["zai", "grok"],
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
        assert saved["zai_wallet_channels"] == prior["zai_wallet_channels"]
        assert saved["readings"][wallet_reading_key("w1")] == prior["readings"][wallet_reading_key("w1")]
        assert saved["readings"][wallet_reading_key("w2")] == prior["readings"][wallet_reading_key("w2")]
        assert saved["readings"]["zai"] == prior["readings"]["zai"]

    def test_explicit_empty_pool_deletes_owned_rows(self, wallet_env):
        _write_pool(wallet_env, [])
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(
            json.dumps(
                {
                    "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
                    "zai_wallet_ordinals": {"w1": 1, "w2": 2},
                    "zai_wallet_ordinal_high_water": 2,
                }
            ),
            encoding="utf-8",
        )
        discord = _WalletDiscord(
            [{"id": "c3", "position": 12}, {"id": "extra", "position": 13}],
            existing={"c3", "extra"},
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
        assert set(discord.deletes) == {"c3", "extra"}

    def test_legacy_env_with_missing_auth(self, wallet_env):
        secrets = wallet_env / "secrets"
        secrets.mkdir()
        (secrets / "zai.env").write_text('ZAI_API_KEY="solo-key"\n', encoding="utf-8")
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert unreadable
        assert len(wallets) == 1
        assert wallets[0].entry_id == LEGACY_ENV_WALLET_ID
        assert wallets[0].runtime_api_key == "solo-key"

    def test_malformed_pool_rows_non_destructive(self, wallet_env, monkeypatch):
        (wallet_env / "auth.json").write_text(
            json.dumps({"credential_pool": {"zai": ["garbage", 42, None]}}),
            encoding="utf-8",
        )
        prior = {
            "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
            "readings": {wallet_reading_key("w1"): {"pct": 30, "reset_seconds": DAY, "label": "z.ai 1"}},
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior), encoding="utf-8")
        discord = _WalletDiscord(
            [{"id": "c3", "position": 12}, {"id": "extra", "position": 13}],
            existing={"c3", "extra"},
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
        assert discord.deletes == []
        saved = json.loads(state_path().read_text(encoding="utf-8"))
        assert saved["zai_wallet_channels"] == prior["zai_wallet_channels"]
        assert saved["readings"] == prior["readings"]

    def _run_non_destructive_grok_tick(self, wallet_env, monkeypatch, pool_entries):
        prior = {
            "last_quota_success": 777,
            "readings": {
                wallet_reading_key("w1"): {"pct": 40, "reset_seconds": DAY, "label": "z.ai 1"},
                wallet_reading_key("w2"): {"pct": 60, "reset_seconds": DAY, "label": "z.ai 2"},
                "zai": {"pct": 60, "reset_seconds": DAY, "label": "z.ai"},
            },
            "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior), encoding="utf-8")
        (wallet_env / "auth.json").write_text(
            json.dumps({"credential_pool": {"zai": pool_entries}}),
            encoding="utf-8",
        )
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
                "enabled_providers": ["zai", "grok"],
            }
        )
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            {"grok": lambda http_fn=None, now_fn=None: (81, 5 * DAY)},
        )
        result = run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_000.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        blob = json.dumps(result)
        assert "sk-secret" not in blob
        assert discord.deletes == []
        saved = json.loads(state_path().read_text(encoding="utf-8"))
        assert saved["zai_wallet_channels"] == prior["zai_wallet_channels"]
        assert saved["readings"][wallet_reading_key("w1")] == prior["readings"][wallet_reading_key("w1")]
        assert saved["readings"][wallet_reading_key("w2")] == prior["readings"][wallet_reading_key("w2")]
        assert saved["readings"]["zai"] == prior["readings"]["zai"]
        return discord, saved

    def _run_partial_malformed_non_destructive_tick(
        self, wallet_env, monkeypatch, pool_entries
    ):
        prior = {
            "last_quota_success": 777,
            "readings": {
                wallet_reading_key("w1"): {"pct": 40, "reset_seconds": DAY, "label": "z.ai 1"},
                wallet_reading_key("w2"): {"pct": 60, "reset_seconds": DAY, "label": "z.ai 2"},
                "zai": {"pct": 60, "reset_seconds": DAY, "label": "z.ai"},
            },
            "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior), encoding="utf-8")
        (wallet_env / "auth.json").write_text(
            json.dumps({"credential_pool": {"zai": pool_entries}}),
            encoding="utf-8",
        )
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
                "enabled_providers": ["zai", "grok"],
            }
        )
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            {"grok": lambda http_fn=None, now_fn=None: (81, 5 * DAY)},
        )
        result = run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_000.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        blob = json.dumps(result)
        assert "sk-secret" not in blob
        assert discord.deletes == []
        saved = json.loads(state_path().read_text(encoding="utf-8"))
        assert saved["zai_wallet_channels"] == prior["zai_wallet_channels"]
        return discord, saved

    def test_mapping_shaped_garbage_pool_non_destructive(self, wallet_env, monkeypatch):
        self._run_non_destructive_grok_tick(
            wallet_env, monkeypatch, [{}, {"foo": 1}]
        )

    def test_id_only_pool_row_non_destructive(self, wallet_env, monkeypatch):
        self._run_non_destructive_grok_tick(
            wallet_env, monkeypatch, [{"id": "w1"}]
        )

    @pytest.mark.parametrize(
        "pool_entries",
        [
            pytest.param(
                [{"id": "w1", "access_token": "k1"}, "garbage"],
                id="valid_plus_non_mapping",
            ),
            pytest.param(
                [{"id": "w1", "access_token": "k1"}, {"id": "w2"}],
                id="valid_plus_id_without_key",
            ),
            pytest.param(
                [{"id": "w1", "access_token": "k1"}, {"access_token": "k2"}],
                id="valid_plus_key_without_id",
            ),
        ],
    )
    def test_partial_malformed_pool_enumerates_valid_and_blocks_deletes(
        self, wallet_env, monkeypatch, pool_entries
    ):
        _write_pool(wallet_env, pool_entries)
        wallets, unreadable = enumerate_zai_wallets(wallet_env)
        assert unreadable
        assert len(wallets) == 1
        assert wallets[0].entry_id == "w1"
        self._run_partial_malformed_non_destructive_tick(
            wallet_env, monkeypatch, pool_entries
        )

    def test_duplicate_key_pool_remains_readable(self, wallet_env):
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


class TestWalletSortParticipants:
    def _capture_sort_entries(self, monkeypatch):
        captured = []

        def _spy(config, entries, headers, http_fn=core.default_http):
            captured.extend(entries)
            return False

        monkeypatch.setattr(core, "sort_voice_channels", _spy)
        return captured

    def _two_wallet_prior_state(self, w2_prior_pct=55):
        return {
            "last_quota_success": 999,
            "readings": {
                wallet_reading_key("w1"): {
                    "pct": 10,
                    "reset_seconds": DAY,
                    "label": "z.ai 1",
                },
                wallet_reading_key("w2"): {
                    "pct": w2_prior_pct,
                    "reset_seconds": DAY,
                    "label": "z.ai 2",
                },
                "zai": {"pct": w2_prior_pct, "reset_seconds": DAY, "label": "z.ai"},
            },
            "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
        }

    def test_failed_wallet_with_prior_reading_included_in_sort(
        self, wallet_env, monkeypatch
    ):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "sk-secret-wallet-aaa"},
                {"id": "w2", "access_token": "sk-secret-wallet-bbb"},
            ],
        )
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(
            json.dumps(self._two_wallet_prior_state()), encoding="utf-8"
        )
        real = core._zai_quota_metrics

        def flaky(http_fn=None, now_fn=None, api_key=None):
            if api_key == "sk-secret-wallet-bbb":
                raise core.QuotaChannelsError("z.ai usage endpoint returned 500")
            return real(http_fn=http_fn, now_fn=now_fn, api_key=api_key)

        monkeypatch.setattr(core, "_zai_quota_metrics", flaky)
        captured = self._capture_sort_entries(monkeypatch)
        discord = _WalletDiscord(
            [
                {"id": "c3", "position": 12},
                {"id": "extra", "position": 13},
            ],
            existing={"c3", "extra"},
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
        sort_ids = [cid for _, cid, _ in captured]
        assert sort_ids == ["c3", "extra"]
        ranks = {cid: rank for _, cid, rank in captured}
        assert ranks["extra"] != 2 * 1e9
        assert len(captured) == 2

    def test_never_scored_failed_wallet_sorts_to_tail(
        self, wallet_env, monkeypatch
    ):
        _write_pool(
            wallet_env,
            [
                {"id": "w1", "access_token": "sk-secret-wallet-aaa"},
                {"id": "w2", "access_token": "sk-secret-wallet-bbb"},
            ],
        )
        prior = {
            "last_quota_success": 999,
            "readings": {
                wallet_reading_key("w1"): {
                    "pct": 10,
                    "reset_seconds": DAY,
                    "label": "z.ai 1",
                },
                "zai": {"pct": 10, "reset_seconds": DAY, "label": "z.ai"},
            },
            "zai_wallet_channels": {"w1": "c3", "w2": "extra"},
            "zai_wallet_ordinals": {"w1": 1, "w2": 2},
            "zai_wallet_ordinal_high_water": 2,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior), encoding="utf-8")
        real = core._zai_quota_metrics

        def flaky(http_fn=None, now_fn=None, api_key=None):
            if api_key == "sk-secret-wallet-bbb":
                raise core.QuotaChannelsError("z.ai usage endpoint returned 500")
            return real(http_fn=http_fn, now_fn=now_fn, api_key=api_key)

        monkeypatch.setattr(core, "_zai_quota_metrics", flaky)
        captured = self._capture_sort_entries(monkeypatch)
        discord = _WalletDiscord(
            [
                {"id": "c3", "position": 12},
                {"id": "extra", "position": 13},
            ],
            existing={"c3", "extra"},
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
        sort_ids = [cid for _, cid, _ in captured]
        assert sort_ids == ["c3", "extra"]
        ranks = {cid: rank for _, cid, rank in captured}
        assert ranks["c3"] < ranks["extra"]
        assert ranks["extra"] == 2 * 1e9

        captured.clear()
        run_tick(
            config,
            force=True,
            now_fn=lambda: 1_000_100.0,
            http_fn=discord,
            sleep_fn=lambda _: None,
        )
        assert [cid for _, cid, _ in captured] == ["c3", "extra"]


class TestSaveWalletState:
    def test_save_wallet_state_preserves_unknown_fields(self, wallet_env):
        prior = {
            "last_quota_success": 4242,
            "readings": {"zai": {"pct": 50, "reset_seconds": DAY, "label": "z.ai"}},
            "zai_wallet_channels": {"old": "c9"},
            "zai_wallet_ordinals": {"old": 1},
            "zai_wallet_ordinal_high_water": 1,
            "future_flag": True,
        }
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(prior), encoding="utf-8")
        save_wallet_state({"w1": "c3"}, {"w1": 2}, 2)
        saved = json.loads(state_path().read_text(encoding="utf-8"))
        assert saved["future_flag"] is True
        assert saved["last_quota_success"] == 4242
        assert saved["readings"] == prior["readings"]
        assert saved["zai_wallet_channels"] == {"w1": "c3"}
        assert saved["zai_wallet_ordinals"] == {"w1": 2}
        assert saved["zai_wallet_ordinal_high_water"] == 2
