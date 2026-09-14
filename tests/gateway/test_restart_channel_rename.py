"""Tests for gateway.restart_channel_rename — restart-progress channel renaming."""
import asyncio
import time

import pytest

from gateway.restart_channel_rename import (
    DEFAULT_IDLE_TEMPLATE,
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_TEMPLATE,
    _render_label,
    parse_restart_channel_rename_config,
    refresh_idle_name,
    rename_on_shutdown,
    restore_on_startup,
    schedule_idle_refresh,
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
        "min_interval_seconds": DEFAULT_MIN_INTERVAL_SECONDS,
    }


def test_parse_config_defaults():
    parsed = parse_restart_channel_rename_config({"channel_id": "42"})
    assert parsed["platform"] == "discord"
    assert parsed["base_name"] == "gateway-restarts"
    assert parsed["template"] == DEFAULT_TEMPLATE
    assert parsed["idle_template"] == DEFAULT_IDLE_TEMPLATE
    assert parsed["min_interval_seconds"] == DEFAULT_MIN_INTERVAL_SECONDS
    assert DEFAULT_MIN_INTERVAL_SECONDS == 600.0


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


# ── min-interval cooldown ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        (30, 30.0),
        (30.5, 30.5),
        ("120", 120.0),
        (0, 0.0),
        (-5, 0.0),
    ],
)
def test_parse_config_min_interval_coercion(raw_value, expected):
    parsed = parse_restart_channel_rename_config(
        {"channel_id": "42", "min_interval_seconds": raw_value}
    )
    assert parsed["min_interval_seconds"] == expected


def test_parse_config_min_interval_non_numeric_falls_back_to_default():
    parsed = parse_restart_channel_rename_config(
        {"channel_id": "42", "min_interval_seconds": "soon"}
    )
    assert parsed["min_interval_seconds"] == DEFAULT_MIN_INTERVAL_SECONDS


@pytest.mark.parametrize(
    "raw_value",
    [float("inf"), "inf", ".inf", float("-inf"), "nan"],
)
def test_parse_config_min_interval_non_finite_falls_back_to_default(raw_value):
    # YAML .inf loads as float("inf"); an infinite interval would park
    # the deferred rename task in an endless cooldown wait.
    parsed = parse_restart_channel_rename_config(
        {"channel_id": "42", "min_interval_seconds": raw_value}
    )
    assert parsed["min_interval_seconds"] == DEFAULT_MIN_INTERVAL_SECONDS


def test_idle_refresh_throttled_within_min_interval():
    # Default 600s cooldown: a second, different label must not reach the
    # adapter until the window reopens.
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter)
    asyncio.run(refresh_idle_name(runner))
    runner._agents = 4
    asyncio.run(refresh_idle_name(runner))
    assert adapter.calls == [("555", "agents-3")]


def test_idle_refresh_applies_once_interval_elapses():
    adapter = _FakeAdapter()
    runner = _FakeRunner(
        config=_config({"channel_id": "555", "min_interval_seconds": 0.05}),
        adapter=adapter,
    )

    async def scenario():
        await refresh_idle_name(runner)
        runner._agents = 4
        await refresh_idle_name(runner)  # still inside the cooldown
        await asyncio.sleep(0.06)  # cooldown expires
        await refresh_idle_name(runner)

    asyncio.run(scenario())
    assert adapter.calls == [("555", "agents-3"), ("555", "agents-4")]


def test_zero_min_interval_disables_cooldown():
    adapter = _FakeAdapter()
    runner = _FakeRunner(
        config=_config({"channel_id": "555", "min_interval_seconds": 0}),
        adapter=adapter,
    )
    asyncio.run(refresh_idle_name(runner))
    runner._agents = 4
    asyncio.run(refresh_idle_name(runner))
    assert adapter.calls == [("555", "agents-3"), ("555", "agents-4")]


def test_shutdown_rename_bypasses_cooldown():
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter, agents=2)
    runner._restart_channel_rename_last_ts = time.monotonic()  # edited just now
    asyncio.run(rename_on_shutdown(runner))
    assert adapter.calls == [("555", "restarting-2-agents")]


def test_startup_restore_bypasses_cooldown():
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter)
    runner._restart_channel_rename_last_ts = time.monotonic()  # edited just now
    asyncio.run(restore_on_startup(runner))
    assert adapter.calls == [("555", "agents-3")]


def test_shutdown_rename_stamps_shared_cooldown_clock():
    # The exempt drain edit must still update the shared last-edit
    # timestamp, throttling the next idle refresh.
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter, agents=2)
    asyncio.run(rename_on_shutdown(runner))
    assert adapter.calls == [("555", "restarting-2-agents")]
    asyncio.run(refresh_idle_name(runner))  # different label, inside cooldown
    assert adapter.calls == [("555", "restarting-2-agents")]


def test_startup_restore_stamps_shared_cooldown_clock():
    # The exempt boot edit must still update the shared last-edit
    # timestamp, throttling the next idle refresh.
    adapter = _FakeAdapter()
    runner = _FakeRunner(config=_config({"channel_id": "555"}), adapter=adapter)
    asyncio.run(restore_on_startup(runner))
    assert adapter.calls == [("555", "agents-3")]
    runner._agents = 4
    asyncio.run(refresh_idle_name(runner))  # different label, inside cooldown
    assert adapter.calls == [("555", "agents-3")]


def test_scheduled_refresh_applies_immediately_without_prior_edit():
    # No last-edit timestamp yet (fresh boot, never renamed): the
    # scheduled task must apply right away, not wait out the interval.
    adapter = _FakeAdapter()
    runner = _FakeRunner(
        config=_config({"channel_id": "555", "min_interval_seconds": 600}),
        adapter=adapter,
    )

    async def scenario():
        schedule_idle_refresh(runner)
        await asyncio.wait_for(runner._idle_channel_rename_task, timeout=1)

    asyncio.run(scenario())
    assert adapter.calls == [("555", "agents-3")]


def test_scheduled_refresh_defers_cooldown_refresh_and_applies_newest():
    # A refresh requested during the cooldown is queued, not dropped: the
    # task waits for the window and then applies the newest count without
    # needing a third external trigger.
    adapter = _FakeAdapter()
    runner = _FakeRunner(
        config=_config({"channel_id": "555", "min_interval_seconds": 0.25}),
        adapter=adapter,
    )

    async def scenario():
        schedule_idle_refresh(runner)
        await runner._idle_channel_rename_task
        assert adapter.calls == [("555", "agents-3")]
        runner._agents = 4
        schedule_idle_refresh(runner)  # inside cooldown: deferred, not dropped
        await asyncio.sleep(0.02)  # task is now parked in the cooldown wait
        runner._agents = 5
        schedule_idle_refresh(runner)  # marks dirty while it waits
        await asyncio.wait_for(runner._idle_channel_rename_task, timeout=5)

    asyncio.run(scenario())
    assert adapter.calls == [("555", "agents-3"), ("555", "agents-5")]


def test_scheduled_refresh_dirty_during_wait_needs_no_second_cooldown_lap():
    # A dirty mark set while the task waits out the cooldown must be
    # satisfied by the edit that follows; the task then exits instead of
    # idling through one more full interval for a post-apply no-op lap.
    # With a 0.5s interval one window is enough (task done ~0.5s); a
    # lingering second lap would need ~1.0s and blow the 0.9s bound.
    adapter = _FakeAdapter()
    runner = _FakeRunner(
        config=_config({"channel_id": "555", "min_interval_seconds": 0.5}),
        adapter=adapter,
    )

    async def scenario():
        schedule_idle_refresh(runner)
        await runner._idle_channel_rename_task
        assert adapter.calls == [("555", "agents-3")]
        runner._agents = 4
        schedule_idle_refresh(runner)  # inside cooldown: deferred, not dropped
        await asyncio.sleep(0.05)  # task is now parked in the cooldown wait
        runner._agents = 5
        schedule_idle_refresh(runner)  # marks dirty while it waits
        await asyncio.wait_for(runner._idle_channel_rename_task, timeout=0.9)
        assert runner._idle_channel_rename_task.done()

    asyncio.run(scenario())
    assert adapter.calls == [("555", "agents-3"), ("555", "agents-5")]


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
