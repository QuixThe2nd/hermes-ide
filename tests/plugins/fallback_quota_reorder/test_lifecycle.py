"""Scheduler self-install invariants: systemd user oneshot+timer (mocked systemctl)."""

from __future__ import annotations

import sys
from pathlib import Path

from plugins.auto_update.systemd import format_exec_start
from plugins.fallback_quota_reorder import lifecycle as lc


class FakeSystemctl:
    """Stateful systemctl --user double: probes reflect enable/stop actions."""

    def __init__(self, *, system_running: bool = True):
        self.calls: list[list[str]] = []
        self.system_running = system_running
        self.timer_enabled = False
        self.timer_active = False

    def __call__(self, args):
        self.calls.append(list(args))
        if "is-system-running" in args:
            if self.system_running:
                return 0, "running\n", ""
            return 1, "", "Failed to connect to bus: No such file or directory\n"
        if "is-enabled" in args:
            if self.timer_enabled:
                return 0, "enabled\n", ""
            return 1, "disabled\n", ""
        if "is-active" in args:
            if self.timer_active:
                return 0, "active\n", ""
            return 3, "inactive\n", ""
        if "enable" in args and "--now" in args:
            self.timer_enabled = True
            self.timer_active = True
            return 0, "", ""
        if "stop" in args:
            self.timer_active = False
        if "disable" in args:
            self.timer_enabled = False
        return 0, "", ""


def _line(body: str, prefix: str) -> str:
    return next(line for line in body.splitlines() if line.startswith(prefix))


def test_reconcile_writes_both_units_pointing_at_run_py(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    ctl = FakeSystemctl()

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir,
        run_systemctl=ctl,
        config={"quota_channels": {"quota_interval_seconds": 1800}},
    )

    assert result is not None and result.skip_reason is None and result.changed
    service = (unit_dir / lc.SERVICE_NAME).read_text(encoding="utf-8")
    timer = (unit_dir / lc.TIMER_NAME).read_text(encoding="utf-8")

    # ExecStart targets run.py in the repo — never a leftover copied script.
    python = lc.resolve_unit_python()
    assert (
        _line(service, "ExecStart=")
        == f"ExecStart={format_exec_start([python, str(lc.RUN_PY)])}"
    )
    assert "scripts/quota_reorder.py" not in service
    for directive in ("Type=oneshot", "Persistent=true", "WantedBy=timers.target"):
        assert directive in service + timer
    assert _line(service, "Environment=HERMES_HOME=") == "Environment=HERMES_HOME=%h/.hermes"
    assert _line(service, "WorkingDirectory=") == f"WorkingDirectory={lc.REPO_ROOT}"
    assert _line(timer, "Unit=") == f"Unit={lc.SERVICE_NAME}"

    # Writes trigger daemon-reload, then enable --now of the timer only.
    assert any("daemon-reload" in c for c in ctl.calls)
    enable_calls = [c for c in ctl.calls if "enable" in c]
    assert enable_calls and all(c[-1] == lc.TIMER_NAME for c in enable_calls)
    assert result.enabled and result.timer_active


def test_second_reconcile_with_identical_content_is_noop(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    ctl = FakeSystemctl()
    config = {"quota_channels": {"quota_interval_seconds": 1800}}
    lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=ctl, config=config
    )
    units = [unit_dir / name for name in lc.UNIT_NAMES]
    first_bodies = [u.read_text(encoding="utf-8") for u in units]
    mtimes = [u.stat().st_mtime_ns for u in units]

    ctl.calls.clear()
    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=ctl, config=config
    )

    assert result is not None and result.changed is False
    assert [u.read_text(encoding="utf-8") for u in units] == first_bodies
    assert [u.stat().st_mtime_ns for u in units] == mtimes
    # Unchanged units + already enabled/active timer → no reload, no enable.
    assert not any("daemon-reload" in c for c in ctl.calls)
    assert not any("enable" in c for c in ctl.calls)


def test_default_interval_maps_to_half_hour_on_calendar(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"

    lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir,
        run_systemctl=FakeSystemctl(),
        config={"quota_channels": {"quota_interval_seconds": 1800}},
    )

    timer = (unit_dir / lc.TIMER_NAME).read_text(encoding="utf-8")
    assert _line(timer, "OnCalendar=") == "OnCalendar=*-*-* *:02,32:00"


def test_quarter_hour_interval_maps_to_quarterly_minutes():
    assert (
        lc.on_calendar_from_cron_spec(lc.recommended_cron_spec(900))
        == "*-*-* *:02,17,32,47:00"
    )


def test_interval_from_config_used_in_rendered_timer(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"

    lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir,
        run_systemctl=FakeSystemctl(),
        config={"quota_channels": {"quota_interval_seconds": 900}},
    )

    timer = (unit_dir / lc.TIMER_NAME).read_text(encoding="utf-8")
    assert _line(timer, "OnCalendar=") == "OnCalendar=*-*-* *:02,17,32,47:00"


def test_missing_quota_channels_section_uses_default_interval(tmp_path):
    assert lc.quota_interval_from_config(None) == 1800
    assert lc.quota_interval_from_config({}) == 1800
    assert (
        lc.quota_interval_from_config({"quota_channels": {"quota_interval_seconds": "junk"}})
        == 1800
    )


def test_disabled_plugin_does_not_enable_timer(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    ctl = FakeSystemctl()

    for config in (
        {"plugins": {"disabled": ["fallback_quota_reorder"]}},
        {"plugins": {"fallback_quota_reorder": {"enabled": False}}},
    ):
        ctl.calls.clear()
        result = lc.reconcile_scheduler_on_load(
            unit_dir=unit_dir, run_systemctl=ctl, config=config
        )
        assert result is not None
        assert result.skip_reason == "plugin explicitly disabled"
        assert not result.enabled
        assert not any("enable" in c for c in ctl.calls)
        assert not (unit_dir / lc.SERVICE_NAME).exists()
        assert not (unit_dir / lc.TIMER_NAME).exists()


def test_disabled_plugin_retires_leftover_timer(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / lc.TIMER_NAME).write_text("[Unit]\n", encoding="utf-8")
    ctl = FakeSystemctl()

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir,
        run_systemctl=ctl,
        config={"plugins": {"disabled": ["fallback_quota_reorder"]}},
    )

    assert result is not None and result.skip_reason
    assert any("disable" in c and c[-1] == lc.TIMER_NAME for c in ctl.calls)
    assert not any("enable" in c for c in ctl.calls)


def test_non_linux_is_skip_not_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    unit_dir = tmp_path / "systemd" / "user"

    def boom(args):
        raise AssertionError("systemctl must not run on non-Linux")

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=boom, config={}
    )

    assert result is not None
    assert result.skip_reason == "not Linux"
    assert not unit_dir.exists()


def test_missing_user_systemd_is_skip_not_exception(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=FakeSystemctl(system_running=False), config={}
    )

    assert result is not None
    assert result.skip_reason == "systemd user manager unavailable"
    assert not (unit_dir / lc.SERVICE_NAME).exists()
    assert not (unit_dir / lc.TIMER_NAME).exists()


def test_reconcile_never_raises_into_plugin_load(tmp_path):
    def boom(args):
        raise RuntimeError("user bus exploded")

    assert (
        lc.reconcile_scheduler_on_load(
            unit_dir=tmp_path / "units", run_systemctl=boom, config={}
        )
        is None
    )


def test_unit_python_prefers_repo_venv(tmp_path):
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    assert lc.resolve_unit_python(repo_root=tmp_path) == str(venv_python)
    assert lc.resolve_unit_python(repo_root=tmp_path / "elsewhere") == sys.executable


def _gateway_start_hook():
    from plugins.fallback_quota_reorder import register

    hooks: list[tuple[str, object]] = []

    class Ctx:
        tools: list[dict] = []
        hooks_ref = hooks

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_hook(self, hook_name, callback):
            self.hooks_ref.append((hook_name, callback))

    ctx = Ctx()
    register(ctx)
    assert ctx.tools == []  # no model tools, ever
    hook_names = [name for name, _ in hooks]
    assert hook_names == [
        "on_gateway_start",
        "post_api_request",
        "api_request_error",
    ]
    return next(callback for name, callback in hooks if name == "on_gateway_start")


def test_register_wires_gateway_start_hook_without_tools():
    assert callable(_gateway_start_hook())


def test_gateway_start_hook_reconciles(tmp_path, monkeypatch):
    recorded: list[dict] = {}
    monkeypatch.setattr(
        lc, "reconcile_scheduler_on_load", lambda **kw: recorded.update(kw) or None
    )
    ctl = FakeSystemctl()

    callback = _gateway_start_hook()
    callback(
        unit_dir=tmp_path / "units",
        run_systemctl=ctl,
        telemetry_schema_version=1,
    )

    assert recorded.get("unit_dir") == tmp_path / "units"
    assert recorded.get("run_systemctl") is ctl


def test_gateway_start_hook_swallows_reconcile_errors(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(lc, "reconcile_scheduler_on_load", boom)
    callback = _gateway_start_hook()
    callback()  # must not raise
