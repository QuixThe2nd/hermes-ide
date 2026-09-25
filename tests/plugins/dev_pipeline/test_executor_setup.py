"""Executor systemd user-unit self-install invariants (mocked systemctl)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from plugins.auto_update.platform import InstallScope
from plugins.dev_pipeline import executor_setup as es


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(
        system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user")
    )


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home, hermes


def _fake_systemctl(calls: list[list[str]], *, code: int = 0):
    def run(args):
        calls.append(list(args))
        return code, "", ""

    return run


def _line(body: str, prefix: str) -> str:
    return next(line for line in body.splitlines() if line.startswith(prefix))


def _env_value(body: str, key: str) -> str | None:
    """Extract an Environment= value regardless of systemd quoting style."""
    for line in body.splitlines():
        for form in (f"Environment={key}=", f'Environment="{key}='):
            if line.startswith(form):
                return line[len(form) :].rstrip('"')
    return None


def test_reconcile_writes_canonical_unit(user_scope, user_home, monkeypatch):
    home, hermes = user_home
    monkeypatch.setattr(es, "platform_supported", lambda: True)
    calls: list[list[str]] = []

    result = es.reconcile_executor_on_load(
        scope=user_scope, run_systemctl=_fake_systemctl(calls)
    )

    assert result is not None and result.supported and result.changed
    body = es.executor_unit_path(user_scope).read_text(encoding="utf-8")

    # ExecStart uses the interpreter running the plugin, invoked as the
    # executor module entrypoint.
    exec_line = _line(body, "ExecStart=")
    assert sys.executable in exec_line
    assert "plugins.dev_pipeline.executor" in exec_line
    assert exec_line.endswith(" run")

    # Environment pins the homes and the repo root; WorkingDirectory is the
    # repo root (derived from the plugin file, not cwd).
    assert _env_value(body, "HOME") == str(home)
    assert _env_value(body, "HERMES_HOME") == str(hermes.resolve())
    assert _env_value(body, "PYTHONPATH") == str(es.REPO_ROOT)
    assert _line(body, "WorkingDirectory=") == f"WorkingDirectory={es.REPO_ROOT}"
    assert str(Path(sys.executable).parent) in (_env_value(body, "PATH") or "")

    for directive in (
        "Type=simple",
        "Restart=always",
        "TimeoutStopSec=30",
        "KillMode=mixed",
        "WantedBy=default.target",
    ):
        assert directive in body
    # The KillMode safety comment (transient attempt units) must survive.
    assert "transient" in body

    # daemon-reload after a write, then enable (+ best-effort start) via the
    # user scope.
    assert any("daemon-reload" in c for c in calls)
    enable_calls = [c for c in calls if "enable" in c and es.SERVICE_NAME in c]
    assert enable_calls and all("--user" in c for c in enable_calls)


def test_reconcile_never_points_at_ambient_python3(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)
    # Tripwire: even with an ambient python3 resolvable on PATH, the unit must
    # keep the running interpreter (the live broken-unit failure mode).
    ambient = "/nonexistent-ambient/bin/python3"
    monkeypatch.setattr(es.shutil, "which", lambda _name, **_kw: ambient)
    calls: list[list[str]] = []

    es.reconcile_executor_on_load(
        scope=user_scope, run_systemctl=_fake_systemctl(calls)
    )

    body = es.executor_unit_path(user_scope).read_text(encoding="utf-8")
    assert ambient not in body
    expected = es.format_exec_start(
        [sys.executable, "-m", "plugins.dev_pipeline.executor", "run"]
    )
    assert _line(body, "ExecStart=") == f"ExecStart={expected}"


def test_second_reconcile_writes_nothing(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)
    calls: list[list[str]] = []
    es.reconcile_executor_on_load(
        scope=user_scope, run_systemctl=_fake_systemctl(calls)
    )
    unit = es.executor_unit_path(user_scope)
    first = unit.read_text(encoding="utf-8")
    mtime_before = unit.stat().st_mtime_ns

    calls.clear()
    result = es.reconcile_executor_on_load(
        scope=user_scope, run_systemctl=_fake_systemctl(calls)
    )

    assert result is not None and result.changed is False
    assert unit.read_text(encoding="utf-8") == first
    assert unit.stat().st_mtime_ns == mtime_before
    # No rewrite → no daemon-reload (enable --now self-heal still runs).
    assert not any("daemon-reload" in c for c in calls)
    assert any("enable" in c for c in calls)


def test_hand_installed_legacy_unit_is_adopted(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)
    unit = es.executor_unit_path(user_scope)
    unit.write_text(
        "[Unit]\n"
        "Description=hand-installed executor\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/legacy/bin/python -m plugins.dev_pipeline.executor run\n"
        "Environment=HOME=/legacy/home\n"
        "WorkingDirectory=/legacy/hermes-agent\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    result = es.reconcile_executor_on_load(
        scope=user_scope, run_systemctl=_fake_systemctl(calls)
    )

    assert result is not None and result.supported and result.changed
    body = unit.read_text(encoding="utf-8")
    # Stale hand-edited paths are gone; the running interpreter took over.
    assert "/legacy" not in body
    assert sys.executable in _line(body, "ExecStart=")
    assert "WantedBy=default.target" in body
    assert any("daemon-reload" in c for c in calls)


def test_non_systemd_platform_skips_cleanly(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: False)
    # Gate order: platform check wins even when a scope would be detectable.
    monkeypatch.setattr(es, "detect_install_scope", lambda: user_scope)

    def boom(args):
        raise AssertionError("systemctl must not run on an unsupported platform")

    result = es.reconcile_executor_on_load(scope=user_scope, run_systemctl=boom)

    assert result is not None and result.supported is False and result.changed is False
    assert not es.executor_unit_path(user_scope).exists()


def test_missing_user_manager_skips_with_warning(user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)
    monkeypatch.setattr(es, "detect_install_scope", lambda: None)

    result = es.reconcile_executor_on_load(run_systemctl=_fake_systemctl([]))

    assert result is not None and result.supported is False
    assert any("user manager" in w for w in result.warnings)


def test_system_scope_is_not_self_installed(tmp_path, user_home, monkeypatch):
    system_scope = InstallScope(
        system=True,
        unit_dir=tmp_path / "etc-systemd",
        systemctl_prefix=("systemctl",),
    )
    monkeypatch.setattr(es, "platform_supported", lambda: True)

    def boom(args):
        raise AssertionError("systemctl must not run for system-scope installs")

    result = es.reconcile_executor_on_load(scope=system_scope, run_systemctl=boom)

    assert result is not None and result.supported is False
    assert not es.executor_unit_path(system_scope).exists()
    assert any("user scope" in w for w in result.warnings)


def test_enable_failure_warns_without_failing(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)
    calls: list[list[str]] = []

    result = es.reconcile_executor_on_load(
        scope=user_scope, run_systemctl=_fake_systemctl(calls, code=1)
    )

    assert result is not None and result.supported and result.enabled is False
    assert result.warnings
    assert es.executor_unit_path(user_scope).exists()


def test_reconcile_exception_is_contained(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)

    def boom(args):
        raise RuntimeError("user bus exploded")

    assert (
        es.reconcile_executor_on_load(scope=user_scope, run_systemctl=boom) is None
    )


def _gateway_start_hook():
    from plugins.dev_pipeline import register

    hooks: list[tuple[str, object]] = []

    class Ctx:
        def register_tool(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            hooks.append((hook_name, callback))

    register(Ctx())
    assert hooks and hooks[0][0] == "on_gateway_start"
    return hooks[0][1]


def test_register_wires_gateway_start_hook(user_home):
    callback = _gateway_start_hook()
    assert callable(callback)


def test_gateway_start_hook_reconciles_unit(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)
    calls: list[list[str]] = []

    callback = _gateway_start_hook()
    callback(
        scope=user_scope,
        run_systemctl=_fake_systemctl(calls),
        telemetry_schema_version=1,
    )

    assert es.executor_unit_path(user_scope).exists()
    assert any("enable" in c for c in calls)


def test_plugin_load_never_fails_on_reconcile_error(user_scope, user_home, monkeypatch):
    monkeypatch.setattr(es, "platform_supported", lambda: True)

    def boom():
        raise RuntimeError("scope detection exploded")

    monkeypatch.setattr(es, "detect_install_scope", boom)

    callback = _gateway_start_hook()
    # Must not raise — plugin/gateway load stays alive.
    callback()


@pytest.fixture
def repo_plugins(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    return repo_root


def test_real_discovery_registers_reconciling_hook(
    repo_plugins, user_home, user_scope, monkeypatch, tmp_path
):
    """E2E: the real plugin loader wires the executor reconcile hook."""
    from hermes_cli.plugins import PluginManager

    monkeypatch.setattr(es, "platform_supported", lambda: True)
    mgr = PluginManager()
    mgr.discover_and_load()

    ours = [
        cb
        for cb in mgr.iter_hook_callbacks("on_gateway_start")
        if "dev_pipeline" in (getattr(cb, "__module__", "") or "")
    ]
    assert ours, "dev_pipeline must register on_gateway_start through real discovery"

    calls: list[list[str]] = []
    ours[-1](scope=user_scope, run_systemctl=_fake_systemctl(calls))
    assert es.executor_unit_path(user_scope).exists()
    assert any("enable" in c and es.SERVICE_NAME in c for c in calls)
