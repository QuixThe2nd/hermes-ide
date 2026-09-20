"""Scheduler self-install invariants: systemd user oneshot+timer (mocked systemctl)."""

from __future__ import annotations

import sys

from plugins.auto_update.systemd import format_exec_start
from plugins.pr_intent_watch import lifecycle as lc


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
        config={"pr_intent_watch": {"poll_seconds": 300}},
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
    assert "[Install]" not in service  # oneshot is timer-activated only
    assert _line(service, "Type=") == "Type=oneshot"
    for directive in ("Persistent=true", "WantedBy=timers.target", "AccuracySec=1s"):
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
    config = {"pr_intent_watch": {"poll_seconds": 300}}
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


def test_default_interval_maps_to_every_five_minutes(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"

    lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir,
        run_systemctl=FakeSystemctl(),
        config={},
    )

    timer = (unit_dir / lc.TIMER_NAME).read_text(encoding="utf-8")
    assert (
        _line(timer, "OnCalendar=")
        == "OnCalendar=*-*-* *:00,05,10,15,20,25,30,35,40,45,50,55:00"
    )


def test_interval_from_config_used_in_rendered_timer(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"

    lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir,
        run_systemctl=FakeSystemctl(),
        config={"pr_intent_watch": {"poll_seconds": 900}},
    )

    timer = (unit_dir / lc.TIMER_NAME).read_text(encoding="utf-8")
    assert _line(timer, "OnCalendar=") == "OnCalendar=*-*-* *:00,15,30,45:00"


def test_on_calendar_covers_the_poll_space():
    assert (
        lc.on_calendar_from_poll_seconds(300)
        == "*-*-* *:00,05,10,15,20,25,30,35,40,45,50,55:00"
    )
    assert lc.on_calendar_from_poll_seconds(3600) == "*-*-* *:00:00"
    assert lc.on_calendar_from_poll_seconds(60) == "*-*-* *:00,01,02,03,04,05,06,07,08,09,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59:00"
    # Junk never produces a broken calendar — fall back to the default cadence.
    assert lc.on_calendar_from_poll_seconds("junk") == lc.on_calendar_from_poll_seconds(300)


def test_poll_seconds_from_config_floors_at_sixty():
    assert lc.poll_seconds_from_config(None) == 300
    assert lc.poll_seconds_from_config({}) == 300
    assert lc.poll_seconds_from_config({"pr_intent_watch": {"poll_seconds": 30}}) == 60
    assert (
        lc.poll_seconds_from_config({"pr_intent_watch": {"poll_seconds": "junk"}}) == 300
    )


def test_disabled_plugin_does_not_enable_timer(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    ctl = FakeSystemctl()

    for config in (
        {"plugins": {"disabled": ["pr_intent_watch"]}},
        {"plugins": {"pr_intent_watch": {"enabled": False}}},
        {"pr_intent_watch": {"enabled": False}},
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
        config={"plugins": {"disabled": ["pr_intent_watch"]}},
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
    from plugins.pr_intent_watch import register

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
    assert hook_names == ["on_gateway_start"]
    return next(callback for name, callback in hooks if name == "on_gateway_start")


def test_register_wires_gateway_start_hook_without_tools():
    assert callable(_gateway_start_hook())


def test_register_without_hook_surface_is_a_noop():
    from plugins.pr_intent_watch import register

    class Bare:
        pass

    register(Bare())  # must not raise; nothing to register on


def test_gateway_start_hook_reconciles(tmp_path, monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(
        lc, "reconcile_scheduler_on_load", lambda **kw: recorded.append(kw) or None
    )
    ctl = FakeSystemctl()

    callback = _gateway_start_hook()
    callback(
        unit_dir=tmp_path / "units",
        run_systemctl=ctl,
        telemetry_schema_version=1,
    )

    assert recorded[0].get("unit_dir") == tmp_path / "units"
    assert recorded[0].get("run_systemctl") is ctl
    assert "telemetry_schema_version" not in recorded[0]  # unrelated kwargs dropped


def test_gateway_start_hook_swallows_reconcile_errors(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(lc, "reconcile_scheduler_on_load", boom)
    callback = _gateway_start_hook()
    callback()  # must not raise
