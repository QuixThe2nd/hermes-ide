"""Self-install of the fallback-quota-reorder systemd user timer.

The reorder used to be a hand-added cron/Hermes-cron job pointed at a copied
script in ``HERMES_HOME/scripts``, so fresh deployments silently never ticked.
Mirroring ``plugins/auto_update`` and ``plugins/dev_pipeline`` (reconcile on
gateway start), the oneshot+timer pair is now written, daemon-reloaded when
changed, and enabled on plugin load — Linux with a reachable systemd **user**
manager only. Every other platform logs once and stays out of the way;
reconcile never raises into plugin load. ``run.py`` (the oneshot entry) must
never call back into this module.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from plugins.auto_update.systemd import default_systemctl_runner, format_exec_start
from plugins.fallback_quota_reorder.core import (
    DEFAULT_QUOTA_INTERVAL_SECONDS,
    recommended_cron_spec,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-fallback-quota-reorder.service"
TIMER_NAME = "hermes-fallback-quota-reorder.timer"
UNIT_NAMES = (SERVICE_NAME, TIMER_NAME)

# plugins/fallback_quota_reorder/lifecycle.py -> repo root — never the cwd of
# whatever process happened to load the plugin.
REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_PY = Path(__file__).resolve().parent / "run.py"

SystemctlRunner = Callable[[Sequence[str]], tuple[int, str, str]]

_skip_logged = False


@dataclass(frozen=True)
class ReconcileResult:
    changed: bool = False
    enabled: bool = False
    timer_active: bool = False
    skip_reason: str | None = None
    warnings: tuple[str, ...] = ()


def default_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def resolve_unit_python(repo_root: Path = REPO_ROOT) -> str:
    """Interpreter for the unit: the repo venv when present, else the running one.

    Never ``which("python3")`` — an ambient interpreter without the hermes tree
    importable crashes the oneshot at import. The venv ``python`` symlink is
    used as-is (resolving it yields the base interpreter, without ``yaml``).
    """
    venv_python = repo_root / "venv" / "bin" / "python"
    try:
        if venv_python.is_file():
            return str(venv_python)
    except OSError:
        pass
    return sys.executable


def on_calendar_from_cron_spec(cron_spec: str) -> str:
    """Map the minute-field cron form of ``recommended_cron_spec()`` to OnCalendar."""
    fields = cron_spec.split()
    minute_field = fields[0] if fields else "2"
    if minute_field == "*":
        return "*-*-* *:*:00"
    try:
        minutes = [f"{int(token):02d}" for token in minute_field.split(",")]
    except ValueError:
        minutes = ["02"]
    return f"*-*-* *:{','.join(minutes)}:00"


def plugin_explicitly_disabled(raw: Mapping[str, object] | None) -> bool:
    """True when config.yaml disables this plugin by name or section flag."""
    if not isinstance(raw, Mapping):
        return False
    plugins = raw.get("plugins")
    if isinstance(plugins, Mapping):
        disabled = plugins.get("disabled")
        if isinstance(disabled, list) and "fallback_quota_reorder" in disabled:
            return True
        section = plugins.get("fallback_quota_reorder")
        if isinstance(section, Mapping):
            enabled = section.get("enabled")
            if isinstance(enabled, bool) and not enabled:
                return True
    return False


def quota_interval_from_config(raw: Mapping[str, object] | None) -> int:
    """``quota_channels.quota_interval_seconds`` (default 1800), clamped to >=60."""
    section = raw.get("quota_channels") if isinstance(raw, Mapping) else None
    try:
        value = int(
            (section or {}).get("quota_interval_seconds", DEFAULT_QUOTA_INTERVAL_SECONDS)
        )
    except (TypeError, ValueError):
        return DEFAULT_QUOTA_INTERVAL_SECONDS
    return max(60, value)


def render_service_unit(*, python_executable: str, run_py: Path, repo_root: Path) -> str:
    # No [Install] section on purpose: the oneshot is activated solely by the
    # timer; enabling the service itself would add a pointless boot-time run.
    # ``%h`` is systemd's user-home specifier — expanded by the user manager,
    # so it must NOT go through the %-escaping path quoting helpers.
    return f"""[Unit]
Description=Hermes fallback quota reorder (oneshot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={format_exec_start([python_executable, str(run_py)])}
WorkingDirectory={_format_path(str(repo_root))}
Environment=HERMES_HOME=%h/.hermes
StandardOutput=journal
StandardError=journal
"""


def render_timer_unit(*, on_calendar: str) -> str:
    return f"""[Unit]
Description=Hermes fallback quota reorder schedule

[Timer]
OnCalendar={on_calendar}
# AccuracySec=1s keeps the ~120s post-quota-refresh offset meaningful;
# systemd's 1min default could eat most of it.
AccuracySec=1s
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def _format_path(value: str) -> str:
    """Quote a bare path directive; escape ``%`` so systemd does not read specifiers."""
    escaped = value.replace("%", "%%")
    if any(ch in escaped for ch in ' \t\n"\\$'):
        inner = escaped.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{inner}"'
    return escaped


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_unit_if_changed(path: Path, content: str) -> bool:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing == content:
        return False
    atomic_write(path, content)
    return True


def user_systemd_available(runner: SystemctlRunner) -> bool:
    """Probe the user manager; a degraded-but-alive instance still runs timers."""
    code, out, _err = runner(["systemctl", "--user", "is-system-running"])
    if code == 0:
        return True
    return code == 1 and "degraded" in (out or "").lower()


def _timer_enabled(runner: SystemctlRunner) -> bool:
    code, out, _err = runner(["systemctl", "--user", "is-enabled", TIMER_NAME])
    return code == 0 and (out or "").strip() in {"enabled", "static"}


def _timer_active(runner: SystemctlRunner) -> bool:
    code, out, _err = runner(["systemctl", "--user", "is-active", TIMER_NAME])
    return code == 0 and (out or "").strip() == "active"


def _stop_timer(runner: SystemctlRunner) -> list[str]:
    warnings: list[str] = []
    for action in ("stop", "disable"):
        code, _, err = runner(["systemctl", "--user", action, TIMER_NAME])
        if code != 0:
            warnings.append(f"failed to {action} {TIMER_NAME}: {err.strip() or code}")
    return warnings


def _log_skip_once(reason: str) -> None:
    global _skip_logged
    if _skip_logged:
        return
    _skip_logged = True
    logger.info("fallback_quota_reorder scheduler self-install skipped: %s", reason)


def _load_raw_config() -> Mapping[str, object] | None:
    try:
        from plugins.fallback_quota_reorder.core import load_config_section

        return load_config_section(None)
    except Exception as exc:
        logger.warning(
            "fallback_quota_reorder scheduler could not read config (%s); "
            "using defaults",
            exc,
        )
        return None


def _reconcile(
    *,
    unit_dir: Path | None,
    run_systemctl: SystemctlRunner | None,
    config: Mapping[str, object] | None,
) -> ReconcileResult:
    raw = config if config is not None else _load_raw_config()

    if plugin_explicitly_disabled(raw):
        # The plugin owns the timer, so disabling the plugin must retire a
        # previously installed one — otherwise it would tick forever.
        _log_skip_once("plugin explicitly disabled in config")
        warnings: list[str] = []
        timer_unit = (unit_dir or default_unit_dir()) / TIMER_NAME
        try:
            leftover = timer_unit.is_file()
        except OSError:
            leftover = False
        if leftover:
            runner = run_systemctl or default_systemctl_runner
            warnings.extend(_stop_timer(runner))
        return ReconcileResult(skip_reason="plugin explicitly disabled", warnings=tuple(warnings))

    if not is_linux():
        _log_skip_once("not Linux (systemd user timer unavailable)")
        return ReconcileResult(skip_reason="not Linux")

    runner = run_systemctl or default_systemctl_runner
    if not user_systemd_available(runner):
        _log_skip_once("systemd user manager unavailable")
        return ReconcileResult(skip_reason="systemd user manager unavailable")

    target_dir = unit_dir or default_unit_dir()
    on_calendar = on_calendar_from_cron_spec(
        recommended_cron_spec(quota_interval_from_config(raw))
    )
    service_body = render_service_unit(
        python_executable=resolve_unit_python(),
        run_py=RUN_PY,
        repo_root=REPO_ROOT,
    )
    timer_body = render_timer_unit(on_calendar=on_calendar)

    changed = False
    for name, body in ((SERVICE_NAME, service_body), (TIMER_NAME, timer_body)):
        if write_unit_if_changed(target_dir / name, body):
            changed = True

    warnings = []
    if changed:
        code, _, err = runner(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            warnings.append(f"failed to daemon-reload: {err.strip() or code}")

    enabled = _timer_enabled(runner)
    timer_active = _timer_active(runner)
    if not (enabled and timer_active):
        # Idempotent self-heal: enable --now is safe on an already-enabled
        # timer and starts it when it is merely inactive.
        code, _, err = runner(["systemctl", "--user", "enable", "--now", TIMER_NAME])
        if code != 0:
            warnings.append(f"failed to enable timer: {err.strip() or code}")
        else:
            # enable --now on a timer enables and starts it.
            enabled = True
            timer_active = True

    return ReconcileResult(
        changed=changed,
        enabled=enabled,
        timer_active=timer_active,
        warnings=tuple(warnings),
    )


def reconcile_scheduler_on_load(
    *,
    unit_dir: Path | None = None,
    run_systemctl: SystemctlRunner | None = None,
    config: Mapping[str, object] | None = None,
) -> ReconcileResult | None:
    """Install/reconcile the user timer pair. Never raises into plugin load.

    Never call from ``run.py`` — the oneshot must not re-reconcile its own
    scheduler. Returns None only when an unexpected error was contained.
    """
    try:
        return _reconcile(
            unit_dir=unit_dir, run_systemctl=run_systemctl, config=config
        )
    except Exception as exc:
        logger.warning(
            "fallback_quota_reorder scheduler reconcile skipped: %s",
            exc,
            exc_info=True,
        )
        return None
