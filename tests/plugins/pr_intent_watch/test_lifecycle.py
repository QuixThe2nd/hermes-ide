"""Scheduler self-install invariants: systemd user Type=simple serve service
(mocked systemctl), plus retirement of the legacy oneshot+timer pair."""

from __future__ import annotations

import sys

from plugins.auto_update.systemd import format_exec_start
from plugins.pr_intent_watch import lifecycle as lc


class FakeSystemctl:
    """Stateful systemctl --user double: probes reflect enable/stop actions."""

    def __init__(self, *, system_running: bool = True):
        self.calls: list[list[str]] = []
        self.system_running = system_running
        self.enabled: set[str] = set()
        self.active: set[str] = set()

    def __call__(self, args):
        self.calls.append(list(args))
        if "is-system-running" in args:
            if self.system_running:
                return 0, "running\n", ""
            return 1, "", "Failed to connect to bus: No such file or directory\n"
        if "is-enabled" in args:
            name = args[-1]
            return (0, "enabled\n", "") if name in self.enabled else (1, "disabled\n", "")
        if "is-active" in args:
            name = args[-1]
            return (0, "active\n", "") if name in self.active else (3, "inactive\n", "")
        if "enable" in args and "--now" in args:
            self.enabled.add(args[-1])
            self.active.add(args[-1])
            return 0, "", ""
        if "stop" in args:
            self.active.discard(args[-1])
        if "disable" in args:
            self.enabled.discard(args[-1])
        return 0, "", ""


# The pre-serve model's unit: oneshot, no [Install], activated by a timer.
LEGACY_ONESHOT_BODY = """[Unit]
Description=Hermes PR intent watch (oneshot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/hermes/run.py
WorkingDirectory=/opt/hermes
Environment=HERMES_HOME=%h/.hermes
StandardOutput=journal
StandardError=journal
"""


def _line(body: str, prefix: str) -> str:
    return next(line for line in body.splitlines() if line.startswith(prefix))


def _expected_service_body() -> str:
    return lc.render_service_unit(
        python_executable=lc.resolve_unit_python(),
        run_py=lc.RUN_PY,
        repo_root=lc.REPO_ROOT,
    )


def test_reconcile_writes_simple_serve_service(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    ctl = FakeSystemctl()

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=ctl, config={}
    )

    assert result is not None and result.skip_reason is None and result.changed
    service = (unit_dir / lc.SERVICE_NAME).read_text(encoding="utf-8")

    # ExecStart targets run.py --serve in the repo — the long-running process.
    python = lc.resolve_unit_python()
    assert (
        _line(service, "ExecStart=")
        == f"ExecStart={format_exec_start([python, str(lc.RUN_PY), '--serve'])}"
    )
    assert _line(service, "Type=") == "Type=simple"
    assert _line(service, "Restart=") == "Restart=on-failure"
    assert _line(service, "Environment=HERMES_HOME=") == "Environment=HERMES_HOME=%h/.hermes"
    assert _line(service, "WorkingDirectory=") == f"WorkingDirectory={lc.REPO_ROOT}"
    # enable --now (the gateway-start self-heal) needs an install target.
    assert _line(service, "WantedBy=") == "WantedBy=default.target"

    # Writes trigger daemon-reload, then enable --now of the SERVICE only.
    assert any("daemon-reload" in c for c in ctl.calls)
    enable_calls = [c for c in ctl.calls if "enable" in c]
    assert enable_calls and all(c[-1] == lc.SERVICE_NAME for c in enable_calls)
    assert result.enabled and result.active
    # The poll lives inside --serve — no timer unit is written anymore.
    assert not (unit_dir / lc.TIMER_NAME).exists()


def test_leftover_oneshot_unit_is_rewritten_as_serve_unit(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / lc.SERVICE_NAME).write_text(LEGACY_ONESHOT_BODY, encoding="utf-8")
    ctl = FakeSystemctl()

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=ctl, config={}
    )

    service = (unit_dir / lc.SERVICE_NAME).read_text(encoding="utf-8")
    assert service == _expected_service_body()
    assert _line(service, "Type=") == "Type=simple"
    assert result.changed and result.enabled and result.active
    assert any("daemon-reload" in c for c in ctl.calls)


def test_leftover_timer_is_stopped_disabled_and_removed(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / lc.TIMER_NAME).write_text("[Unit]\n", encoding="utf-8")
    ctl = FakeSystemctl()

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=ctl, config={}
    )

    # A firing timer would double-poll next to --serve's in-process poll.
    assert result is not None and result.timer_retired
    assert not (unit_dir / lc.TIMER_NAME).exists()
    assert any("stop" in c and c[-1] == lc.TIMER_NAME for c in ctl.calls)
    assert any("disable" in c and c[-1] == lc.TIMER_NAME for c in ctl.calls)
    assert any("daemon-reload" in c for c in ctl.calls)
    # The serve service is still armed.
    assert result.enabled and result.active
    assert (unit_dir / lc.SERVICE_NAME).is_file()


def test_second_reconcile_with_identical_content_is_noop(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    ctl = FakeSystemctl()
    lc.reconcile_scheduler_on_load(unit_dir=unit_dir, run_systemctl=ctl, config={})
    service_path = unit_dir / lc.SERVICE_NAME
    first_body = service_path.read_text(encoding="utf-8")
    mtime = service_path.stat().st_mtime_ns

    ctl.calls.clear()
    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=ctl, config={}
    )

    assert result is not None and result.changed is False
    assert service_path.read_text(encoding="utf-8") == first_body
    assert service_path.stat().st_mtime_ns == mtime
    # Unchanged unit + already enabled/active service → no reload, no enable.
    assert not any("daemon-reload" in c for c in ctl.calls)
    assert not any("enable" in c for c in ctl.calls)


def test_inactive_service_is_reenabled(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    ctl = FakeSystemctl()
    lc.reconcile_scheduler_on_load(unit_dir=unit_dir, run_systemctl=ctl, config={})
    ctl.calls.clear()
    ctl.active.discard(lc.SERVICE_NAME)  # crashed and not restarted

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir, run_systemctl=ctl, config={}
    )

    assert result is not None and result.active
    assert any("enable" in c and c[-1] == lc.SERVICE_NAME for c in ctl.calls)


def test_disabled_plugin_does_not_enable_service(tmp_path):
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
        assert ctl.calls == []  # nothing installed → nothing to touch
        assert not (unit_dir / lc.SERVICE_NAME).exists()
        assert not (unit_dir / lc.TIMER_NAME).exists()


def test_disabled_plugin_stops_and_disables_service_and_timer(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in lc.UNIT_NAMES:
        (unit_dir / name).write_text("[Unit]\n", encoding="utf-8")
    ctl = FakeSystemctl()

    result = lc.reconcile_scheduler_on_load(
        unit_dir=unit_dir,
        run_systemctl=ctl,
        config={"plugins": {"disabled": ["pr_intent_watch"]}},
    )

    assert result is not None and result.skip_reason
    for name in lc.UNIT_NAMES:
        assert any("stop" in c and c[-1] == name for c in ctl.calls), name
        assert any("disable" in c and c[-1] == name for c in ctl.calls), name
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
