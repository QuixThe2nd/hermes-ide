"""Combined quota+token channel enrichment tests for quota_channels."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from plugins.quota_channels.core import (
    QuotaChannelsError,
    fetch_codex_tokens_7d,
    format_codex_name,
    format_compact_tokens,
    format_cursor_name,
    format_zai_name,
    redact_secrets,
    run_tick,
    validate_quota_config,
)
from plugins.quota_channels.tool import handle_quota_channels_tick


@pytest.fixture(autouse=True)
def _restore_plugin_modules():
    prefixes = ("plugins.quota_channels", "hermes_cli.plugins")
    saved = {k: m for k, m in sys.modules.items() if k.startswith(prefixes)}
    yield
    for key in list(sys.modules):
        if key.startswith(prefixes):
            del sys.modules[key]
    sys.modules.update(saved)
    for key, mod in saved.items():
        if "." in key:
            parent_name, attr = key.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attr, mod)


def _request_method(req) -> str:
    return getattr(req, "method", None) or req.get_method()


def _discord_ok(req, timeout=25.0):
    method = _request_method(req)
    if "discord.com" not in req.full_url:
        raise AssertionError(f"unexpected non-discord request: {req.full_url}")
    if method == "GET":
        return 200, json.dumps({"name": "old-name"}).encode()
    if method == "PATCH":
        return 200, json.dumps({"name": "patched"}).encode()
    raise AssertionError((method, req.full_url))


class HttpRecorder:
    def __init__(self, handler=_discord_ok):
        self.urls: list[str] = []
        self.handler = handler

    def __call__(self, req, timeout=25.0):
        self.urls.append(req.full_url)
        return self.handler(req, timeout)


def _patch_run_tick_http(monkeypatch, fake_http):
    import plugins.quota_channels.core as core

    monkeypatch.setattr(core, "default_http", fake_http)
    original = core.run_tick

    def bound_run_tick(
        config,
        *,
        force=False,
        sleep_fn=core.time.sleep,
        now_fn=core.time.time,
        http_fn=fake_http,
    ):
        return original(
            config,
            force=force,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            http_fn=http_fn,
        )

    monkeypatch.setattr(core, "run_tick", bound_run_tick)
    monkeypatch.setattr("plugins.quota_channels.tool.run_tick", bound_run_tick)


def _base_section(**overrides):
    section = {
        "guild_id": "100",
        "category_id": "200",
        "channel_ids": {
            "codex": "301",
            "kimi": "302",
            "zai": "303",
            "cursor": "304",
            "grok": "305",
        },
        "enabled_providers": ["codex", "kimi", "zai", "cursor", "grok"],
    }
    section.update(overrides)
    return section


def _write_tick_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "discord.env").write_text("DISCORD_BOT_TOKEN=bot-test-token\n")
    (secrets / "zai.env").write_text("ZAI_API_KEY=zai-secret-key\n")
    (tmp_path / ".env").write_text("KIMI_API_KEY=kimi-secret-key\n")
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "codex-access-token",
                            "refresh_token": "codex-refresh-token",
                        }
                    },
                    "xai-oauth": {
                        "tokens": {
                            "access_token": "grok-access-token",
                            "refresh_token": "grok-refresh-token",
                        }
                    },
                }
            }
        )
    )
    cursor_dir = tmp_path / ".config" / "cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "auth.json").write_text(
        json.dumps({"accessToken": "cursor-access-token"})
    )
    monkeypatch.setattr("plugins.quota_channels.core.Path.home", lambda: tmp_path)


TOKEN_ENDPOINT_MARKERS = (
    "profiles/me",
    "model-usage",
    "GetAggregatedUsageEvents",
)


def _provider_http_router(
    *,
    codex_tokens: int = 2_234_567_890,
    zai_tokens: int = 250_000_000,
    cursor_tokens: int = 49_000_000,
    channel_names: dict[str, str] | None = None,
    fail_token: set[str] | None = None,
    fail_quota: set[str] | None = None,
):
    channel_names = channel_names or {}
    fail_token = fail_token or set()
    fail_quota = fail_quota or set()
    fixed_now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    fixed_ts = fixed_now.timestamp()

    def fake_http(req, timeout=25.0):
        url = req.full_url
        method = _request_method(req)

        if "wham/usage" in url:
            if "codex" in fail_quota:
                return 500, b"codex quota down"
            return 200, json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 1,
                            "reset_after_seconds": 604800,
                        }
                    }
                }
            ).encode()

        if "api.kimi.com" in url:
            if "kimi" in fail_quota:
                return 500, b"kimi quota down"
            reset_at = (fixed_now + timedelta(days=4)).strftime("%Y-%m-%dT00:00:00Z")
            return 200, json.dumps(
                {"usage": {"remaining": 74, "resetTime": reset_at}}
            ).encode()

        if "quota/limit" in url:
            if "zai" in fail_quota:
                return 500, b"zai quota down"
            return 200, json.dumps(
                {
                    "data": {
                        "limits": [
                            {
                                "percentage": 26,
                                "nextResetTime": int((fixed_ts + 345600) * 1000),
                            }
                        ]
                    }
                }
            ).encode()

        if "GetCurrentPeriodUsage" in url:
            if "cursor" in fail_quota:
                return 500, b"cursor quota down"
            return 200, json.dumps(
                {
                    "planUsage": {"autoPercentUsed": 12, "apiPercentUsed": 15},
                    "billingCycleEnd": int((fixed_ts + 2332800) * 1000),
                }
            ).encode()

        if "GetGrokCreditsConfig" in url:
            if "grok" in fail_quota:
                return 500, b"grok quota down"
            from tests.plugins.quota_channels.test_parsers import _build_grok_grpc_body

            return 200, _build_grok_grpc_body(24.0, int(fixed_ts + 600))

        if "profiles/me" in url:
            if "codex" in fail_token:
                return 503, b"codex token down"
            body = json.dumps(
                {
                    "stats": {
                        "daily_usage_buckets": [
                            {
                                "start_date": fixed_now.date().isoformat(),
                                "tokens": codex_tokens,
                            }
                        ]
                    }
                }
            ).encode()
            return 200, body

        if "model-usage" in url and "quota/limit" not in url:
            if "zai" in fail_token:
                return 503, b"zai token down"
            body = json.dumps(
                {
                    "code": 200,
                    "data": {"totalUsage": {"totalTokensUsage": zai_tokens}},
                }
            ).encode()
            return 200, body

        if "GetAggregatedUsageEvents" in url:
            if "cursor" in fail_token:
                return 503, b"cursor token down"
            body = json.dumps(
                {
                    "totalInputTokens": cursor_tokens - 1_000_000,
                    "totalOutputTokens": 1_000_000,
                }
            ).encode()
            return 200, body

        if method == "GET" and url.endswith("/guilds/100/channels"):
            return 200, json.dumps(
                [
                    {"id": "301", "position": 10},
                    {"id": "302", "position": 11},
                    {"id": "303", "position": 12},
                    {"id": "304", "position": 13},
                    {"id": "305", "position": 14},
                ]
            ).encode()

        if "discord.com" in url and method == "GET":
            for cid, name in channel_names.items():
                if f"/channels/{cid}" in url:
                    return 200, json.dumps({"name": name}).encode()
            return 200, json.dumps({"name": "old-name"}).encode()

        if method == "PATCH" and url.endswith("/guilds/100/channels"):
            return 204, b""

        if method == "PATCH":
            return 200, json.dumps({"name": "patched"}).encode()

        raise AssertionError((method, url))

    return fake_http, fixed_now


class TestV1ConfigAutoEnrichment:
    FIXED_NOW = 1_700_000_000.0
    LAST_SUCCESS = 1_700_000_000

    def test_v1_config_loads_without_token_usage_key(self):
        config = validate_quota_config(_base_section())
        assert "token_usage" not in config
        assert config["channel_ids"]["codex"] == "301"

    def test_v1_config_tick_enriches_combined_labels(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(_base_section())
        fake_http, fixed_now = _provider_http_router()
        rename_bodies = []

        def recording_http(req, timeout=25.0):
            method = _request_method(req)
            url = req.full_url
            if (
                method == "PATCH"
                and "discord.com" in url
                and not url.endswith("/guilds/100/channels")
            ):
                rename_bodies.append(json.loads(req.data.decode()))
            return fake_http(req, timeout)

        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state",
            lambda *args, **kwargs: self.LAST_SUCCESS,
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )

        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=recording_http,
            now_fn=lambda: fixed_now.timestamp(),
        )

        assert result["success"] is True
        assert "token_usage" not in result
        names = [body["name"] for body in rename_bodies]
        assert "Codex: 99% \u2022 2.2B tok/7d \u2022 7d left" in names
        assert "z.ai: 74% \u2022 250.0M tok/7d \u2022 4d left" in names
        assert "Cursor: 88%/85% \u2022 49.0M tok/7d \u2022 27d left" in names
        assert "Kimi: 74% \u2022 4d left" in names
        assert result["providers"]["Codex"]["tokens_7d"] == 2_234_567_890
        assert result["providers"]["z.ai"]["tokens_7d"] == 250_000_000
        assert result["providers"]["Cursor"]["tokens_7d"] == 49_000_000
        assert "tokens_7d" not in result["providers"]["Kimi"]
        assert "tokens_7d" not in result["providers"]["Grok"]


class TestCombinedLabelFormats:
    def test_format_codex_with_tokens(self):
        assert (
            format_codex_name(99, 604800, tokens_7d=2_234_567_890)
            == "Codex: 99% \u2022 2.2B tok/7d \u2022 7d left"
        )

    def test_format_zai_with_tokens(self):
        assert (
            format_zai_name(74, 345600, tokens_7d=250_000_000)
            == "z.ai: 74% \u2022 250.0M tok/7d \u2022 4d left"
        )

    def test_format_cursor_with_tokens(self):
        assert (
            format_cursor_name(88, 85, 2_332_800, tokens_7d=49_000_000)
            == "Cursor: 88%/85% \u2022 49.0M tok/7d \u2022 27d left"
        )

    def test_quota_only_when_tokens_none(self):
        assert format_codex_name(99, 604800) == "Codex: 99% \u2022 7d left"


class TestKimiGrokNoTokenHttp:
    def test_kimi_grok_quota_only_and_no_token_endpoints(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(_base_section())
        patches = []

        def recording_http(req, timeout=25.0):
            method = _request_method(req)
            url = req.full_url
            if method == "PATCH" and "/channels/" in url:
                patches.append(json.loads(req.data.decode()))
            return _provider_http_router()[0](req, timeout)

        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )

        recorder = HttpRecorder(recording_http)
        run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=recorder,
            now_fn=lambda: datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp(),
        )

        names = [p["name"] for p in patches if "name" in p]
        assert any(n.startswith("Kimi:") and "tok/7d" not in n for n in names)
        assert any(n.startswith("Grok:") and "tok/7d" not in n for n in names)
        assert not any(u for u in recorder.urls if "profiles/me" in u and "kimi" in u)
        assert not any(u for u in recorder.urls if "profiles/me" in u and "grok" in u)
        assert len([u for u in recorder.urls if "profiles/me" in u]) == 1
        assert len([u for u in recorder.urls if "model-usage" in u and "quota" not in u]) == 1
        assert len([u for u in recorder.urls if "GetAggregatedUsageEvents" in u]) == 1


class TestTokenFailurePreservesSegment:
    def test_preserves_existing_token_segment_on_fetch_failure(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(_base_section(enabled_providers=["codex"]))
        fake_http, fixed_now = _provider_http_router(
            fail_token={"codex"},
            channel_names={
                "301": "Codex: 50% \u2022 1.8B tok/7d \u2022 2d left",
            },
        )
        patches = []

        def recording_http(req, timeout=25.0):
            method = _request_method(req)
            url = req.full_url
            if method == "PATCH" and "/channels/301" in url:
                patches.append(json.loads(req.data.decode()))
            return fake_http(req, timeout)

        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )

        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=recording_http,
            now_fn=lambda: fixed_now.timestamp(),
        )

        assert patches == [{"name": "Codex: 99% \u2022 1.8B tok/7d \u2022 7d left"}]
        assert result["providers"]["Codex"]["tokens_7d"] == "preserved"
        assert "token_error" in result["providers"]["Codex"]

    def test_quota_only_rename_when_no_segment_and_token_fails(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(_base_section(enabled_providers=["codex"]))
        fake_http, fixed_now = _provider_http_router(fail_token={"codex"})
        patches = []

        def recording_http(req, timeout=25.0):
            method = _request_method(req)
            url = req.full_url
            if method == "PATCH" and "/channels/301" in url:
                patches.append(json.loads(req.data.decode()))
            return fake_http(req, timeout)

        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )

        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=recording_http,
            now_fn=lambda: fixed_now.timestamp(),
        )

        assert patches == [{"name": "Codex: 99% \u2022 7d left"}]
        assert "tokens_7d" not in result["providers"]["Codex"]
        assert "token_error" in result["providers"]["Codex"]


class TestQuotaFailureIsolation:
    def test_quota_failure_leaves_channel_untouched(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(
            _base_section(enabled_providers=["codex", "zai"])
        )
        fake_http, fixed_now = _provider_http_router(fail_quota={"codex"})
        patch_urls = []

        def recording_http(req, timeout=25.0):
            method = _request_method(req)
            url = req.full_url
            if method == "PATCH" and "/channels/" in url:
                patch_urls.append(url)
            return fake_http(req, timeout)

        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )

        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=recording_http,
            now_fn=lambda: fixed_now.timestamp(),
        )

        assert not any("/channels/301" in u for u in patch_urls)
        assert any("/channels/303" in u for u in patch_urls)
        assert "error" in result["providers"]["Codex"]
        assert result["providers"]["z.ai"]["tokens_7d"] == 250_000_000


class TestSortingUnchanged:
    def test_sorts_only_quota_channels_by_reset_seconds(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(_base_section())
        fake_http, fixed_now = _provider_http_router()
        sort_entries = []

        def fake_sort(cfg, entries, headers, http_fn=None):
            sort_entries.extend(entries)
            return False

        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", fake_sort
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )

        run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: fixed_now.timestamp(),
        )

        assert len(sort_entries) == 5
        ordered = sorted(sort_entries, key=lambda item: item[2])
        assert ordered[0][1] == "305"
        assert ordered[-1][1] == "304"
        assert set(cid for _, cid, _ in sort_entries) == {
            "301",
            "302",
            "303",
            "304",
            "305",
        }
        channel_ids = {entry[1] for entry in sort_entries}
        assert channel_ids == {"301", "302", "303", "304", "305"}


class TestNoTokenUsageReferences:
    def test_validate_quota_config_has_no_token_usage_key(self):
        config = validate_quota_config(_base_section())
        assert "token_usage" not in config

    def test_tick_result_has_no_top_level_token_usage(self, monkeypatch, tmp_path):
        _write_tick_credentials(monkeypatch, tmp_path)
        config = validate_quota_config(_base_section())
        fake_http, fixed_now = _provider_http_router()
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )
        result = run_tick(
            config,
            force=True,
            sleep_fn=lambda _: None,
            http_fn=fake_http,
            now_fn=lambda: fixed_now.timestamp(),
        )
        assert "token_usage" not in result

    def test_legacy_token_usage_config_key_is_ignored(self):
        section = _base_section(
            token_usage={"enabled": True, "channel_ids": {"codex": "999"}},
        )
        config = validate_quota_config(section)
        assert "token_usage" not in config


class TestTokenParserHelpers:
    FIXED_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)

    def test_fetch_codex_tokens_7d_refresh_on_401(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {
                                "access_token": "old-access",
                                "refresh_token": "old-refresh",
                            }
                        }
                    }
                }
            )
        )
        profile_calls = []

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "oauth/token" in url:
                return 200, json.dumps({"access_token": "new-access"}).encode()
            if "profiles/me" in url:
                profile_calls.append(req.headers.get("Authorization"))
                if len(profile_calls) == 1:
                    return 401, b"unauthorized"
                body = json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {
                                    "start_date": self.FIXED_NOW.date().isoformat(),
                                    "tokens": 42,
                                }
                            ]
                        }
                    }
                ).encode()
                return 200, body
            raise AssertionError(url)

        total = fetch_codex_tokens_7d(
            http_fn=fake_http, now_fn=lambda: self.FIXED_NOW.timestamp()
        )
        assert total == 42
        assert profile_calls[1] == "Bearer new-access"

    def test_fetch_codex_tokens_redacts_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {
                                "access_token": "leak-access",
                                "refresh_token": "leak-refresh",
                            }
                        }
                    }
                }
            )
        )

        def fake_http(req, timeout=25.0):
            if "profiles/me" in req.full_url:
                return 500, b"error mentioning leak-access and leak-refresh"
            raise AssertionError(req.full_url)

        with pytest.raises(QuotaChannelsError) as exc:
            fetch_codex_tokens_7d(http_fn=fake_http)
        msg = str(exc.value)
        assert "leak-access" not in msg
        assert "[redacted]" in msg


class TestToolRunParity:
    def test_tool_cli_and_direct_match(self, monkeypatch, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        (hermes_home / "config.yaml").write_text(
            yaml.safe_dump({"quota_channels": _base_section(enabled_providers=["codex"])})
        )
        secrets = hermes_home / "secrets"
        secrets.mkdir()
        (secrets / "discord.env").write_text("DISCORD_BOT_TOKEN=bot-test\n")
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openai-codex": {
                            "tokens": {"access_token": "tok", "refresh_token": "ref"}
                        }
                    }
                }
            )
        )
        fake_http, fixed_now = _provider_http_router()
        _patch_run_tick_http(monkeypatch, fake_http)
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 0},
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )

        from plugins.quota_channels.core import load_quota_config, run_tick
        from plugins.quota_channels.run import main

        kwargs = {
            "force": True,
            "sleep_fn": lambda _: None,
            "http_fn": fake_http,
            "now_fn": lambda: fixed_now.timestamp(),
        }
        direct = run_tick(load_quota_config(hermes_home / "config.yaml"), **kwargs)
        tool = json.loads(handle_quota_channels_tick({"force": True}))

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--config",
                    str(hermes_home / "config.yaml"),
                    "--force-quota",
                    "--debug",
                ]
            )
        assert rc == 0
        cli = json.loads(buf.getvalue().strip())

        for result in (tool, cli, direct):
            assert result["success"] is True
            assert "token_usage" not in result
            assert "tokens_7d" in result["providers"]["Codex"]


class TestFormattingHelpers:
    @pytest.mark.parametrize(
        "count, expected",
        [
            (999, "999"),
            (1000, "1.0K"),
            (999_950, "1000.0K"),
            (1_000_000_000, "1.0B"),
        ],
    )
    def test_format_compact_tokens(self, count, expected):
        assert format_compact_tokens(count) == expected

    def test_redact_secrets_replaces_values(self):
        text = "failed with secret-key and other text"
        assert redact_secrets(text, ("secret-key",)) == "failed with [redacted] and other text"
