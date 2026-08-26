"""Tests for gateway.restart_channel_rename — restart-progress channel renaming."""
import asyncio

import pytest

from gateway.restart_channel_rename import (
    DEFAULT_IDLE_TEMPLATE,
    DEFAULT_TEMPLATE,
    _render_label,
    parse_restart_channel_rename_config,
    refresh_idle_name,
    rename_on_shutdown,
    restore_on_startup,
)


class _FakeAdapter:
    def __init__(self, *, fail: bool = False, no_method: bool = False):
        self.calls = []
        self._fail = fail
        self._no_method = no_method

    async def rename_thread(self, channel_id: str, name: str) -> bool:
        if self._no_method:
            raise AssertionError("rename_thread should not be called")
        self.calls.append((str(channel_id), name))
        return not self._fail


class _FakeRunner:
    def __init__(self, config=None, adapter=None, agents=3):
        self.config = config
        self.adapters = {"discord": adapter} if adapter is not None else {}
        self._agents = agents

    def _running_agent_count(self) -> int:
        return self._agents


def _config(raw=None):
    from gateway.config import GatewayConfig

    cfg = GatewayConfig()
    cfg.restart_channel_rename = raw
    return cfg


# ── config parsing ──────────────────────────────────────────────────────────


def test_parse_config_full():
    parsed = parse_restart_channel_rename_config(
        {
            "platform": "Discord",
            "channel_id": "123456789",
            "base_name": "gateway-restarts",
            "renamed_template": "restarting-{agents}-agents",
        }
    )
    assert parsed == {
        "platform": "discord",
        "channel_id": "123456789",
        "base_name": "gateway-restarts",
        "template": "restarting-{agents}-agents",
        "idle_template": DEFAULT_IDLE_TEMPLATE,
    }


def test_parse_config_defaults():
    parsed = parse_restart_channel_rename_config({"channel_id": "42"})
    assert parsed["platform"] == "discord"
    assert parsed["base_name"] == "gateway-restarts"
    assert parsed["template"] == DEFAULT_TEMPLATE
    assert parsed["idle_template"] == DEFAULT_IDLE_TEMPLATE


@pytest.mark.parametrize(
    "raw",
    [None, {}, "nope", {"channel_id": "abc"}, {"channel_id": ""}, {"base_name": ""}],
)
def test_parse_config_invalid_returns_empty(raw):
    assert parse_restart_channel_rename_config(raw) == {}


def test_parse_config_coerces_int_channel_id():
    # YAML often delivers bare numeric IDs as ints.
    parsed = parse_restart_channel_rename_config({"channel_id": 1541012892462223391})
    assert parsed["channel_id"] == "1541012892462223391"


def test_gateway_config_default_is_none():
    from gateway.config import GatewayConfig

    assert GatewayConfig().restart_channel_rename is None


def test_from_dict_accepts_restart_channel_rename():
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_dict(
        {"restart_channel_rename": {"channel_id": "123456789"}}
    )
    assert cfg.restart_channel_rename == {"channel_id": "123456789"}


def test_load_gateway_config_bridges_nested_key(tmp_path, monkeypatch):
    """The real startup loader must forward the nested gateway.* dict to
    from_dict (gw_data is built flat and never passes the yaml section)."""
    from gateway.config import load_gateway_config

    (tmp_path / "config.yaml").write_text(
        "gateway:\n"
        "  restart_channel_rename:\n"
        '    channel_id: "1541012892462223391"\n'
        "    base_name: gateway-restarts\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.config.get_hermes_home", lambda: tmp_path)

    cfg = load_gateway_config()
    assert cfg.restart_channel_rename == {
        "channel_id": "1541012892462223391",
        "base_name": "gateway-restarts",
    }


# ── label rendering ─────────────────────────────────────────────────────────


def test_render_label_default():
    assert _render_label(DEFAULT_TEMPLATE, 4) == "restarting-4-agents"


def test_render_label_custom_template():
    assert _render_label("drain-{agents}", 2) == "drain-2"


def test_render_label_bad_template_falls_back():
    assert _render_label("oops {missing_key}", 7) == "restarting-7-agents"
    assert _render_label("", 1) == "restarting-1-agents"


# ── shutdown rename ─────────────────────────────────────────────────────────


def test_shutdown_renames_with_agent_count():
    adapter = _FakeAdapter()
    runner = _FakeRunner(
        config=_config({"channel_id": "555"}), adapter=adapter, agents=4
    )
    asyncio.run(rename_on_shutdown(runner))
    assert adapter.calls == [("555", "restarting-4-agents")]


def test_shutdown_without_config_is_noop():
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config(None), adapter=adapter)
    asyncio.run(rename_on_shutdown(runner))
    assert adapter.calls == []


def test_shutdown_adapter_failure_does_not_raise():
    adapter = _FakeAdapter(fail=True)
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter)
    asyncio.run(rename_on_shutdown(runner))  # must not raise


# ── startup restore ─────────────────────────────────────────────────────────


def test_startup_restores_idle_name():
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter)
    asyncio.run(restore_on_startup(runner))
    assert adapter.calls == [("555", "agents-3")]


def test_idle_refresh_skips_when_draining():
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter)
    runner._draining = True
    asyncio.run(refresh_idle_name(runner))
    assert adapter.calls == []


def test_idle_refresh_skips_unchanged_label():
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter)
    asyncio.run(refresh_idle_name(runner))
    asyncio.run(refresh_idle_name(runner))
    assert adapter.calls == [("555", "agents-3")]


def test_startup_missing_adapter_is_noop():
    # No discord adapter connected: must not raise.
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=None)
    asyncio.run(restore_on_startup(runner))


def test_startup_adapter_without_rename_support_is_noop():
    class _Bare:
        pass

    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=_Bare())
    asyncio.run(restore_on_startup(runner))  # must not raise


# ── persist helper ──────────────────────────────────────────────────────────


def test_persist_restart_channel_rename_writes_nested_gateway_key():
    from gateway.config import persist_restart_channel_rename
    from hermes_cli.config import load_config

    persist_restart_channel_rename("1541012892462223391")

    raw = load_config()
    assert raw["gateway"]["restart_channel_rename"] == {
        "platform": "discord",
        "channel_id": "1541012892462223391",
        "base_name": "gateway-restarts",
        "renamed_template": DEFAULT_TEMPLATE,
        "idle_template": DEFAULT_IDLE_TEMPLATE,
    }


def test_persist_restart_channel_rename_rejects_non_numeric_id():
    from gateway.config import persist_restart_channel_rename
    from hermes_cli.config import load_config

    persist_restart_channel_rename("not-an-id")

    raw = load_config()
    assert "restart_channel_rename" not in (raw.get("gateway") or {})
