"""Self-install of the pr_intent_watch systemd user service.

Mirrors ``plugins/fallback_quota_reorder/lifecycle.py`` (itself mirroring
``plugins/auto_update``): on gateway start, write the long-running
``Type=simple`` unit into ``~/.config/systemd/user`` — ``run.py --serve``,
which is the live webhook listener plus the in-process poll backup —
daemon-reload when the content changed, and enable ``--now`` the service.
A leftover oneshot+timer pair from the old model is rewritten/retired: with
the poll living inside ``--serve``, a firing timer would double-poll.

Linux with a reachable systemd **user** manager only. Everything else logs
once and stays out of the way; reconcile never raises into plugin load.
``run.py`` (including ``--serve``) must never call back into this module, or
a tick could re-arm its own scheduler.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from plugins.auto_update.systemd import default_systemctl_runner, format_exec_start
from plugins.pr_intent_watch.core import plugin_disabled_in_raw

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-pr-intent-watch.service"
TIMER_NAME = "hermes-pr-intent-watch.timer"  # retired, but still cleaned up
UNIT_NAMES = (SERVICE_NAME, TIMER_NAME)

# plugins/pr_intent_watch/lifecycle.py -> repo root — never the cwd of
# whatever process happened to load the plugin.
REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_PY = Path(__file__).resolve().parent / "run.py"

SystemctlRunner = Callable[[Sequence[str]], tuple[int, str, str]]

_skip_logged = False


@dataclass(frozen=True)
class ReconcileResult:
    changed: bool = False
    enabled: bool = False
    active: bool = False
    timer_retired: bool = False
    skip_reason: str | None = None
    warnings: tuple[str, ...] = ()


def default_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def resolve_unit_python(repo_root: Path = REPO_ROOT) -> str:
    """Interpreter for the unit: the repo venv when present, else the running one.

    Never ``which("python3")`` — an ambient interpreter without the hermes tree
    importable crashes the service at import. The venv ``python`` symlink is
    used as-is (resolving it yields the base interpreter, without ``yaml``).
    """
    venv_python = repo_root / "venv" / "bin" / "python"
    try:
        if venv_python.is_file():
            return str(venv_python)
    except OSError:
        pass
    return sys.executable


def plugin_explicitly_disabled(raw: object | None) -> bool:
    """True when config.yaml disables this plugin by name or section flag.

    The disable rules live in ``core`` (the tick consults the same ones);
    this name mirrors the sibling plugins' lifecycle surface.
    """
    return plugin_disabled_in_raw(raw)  # type: ignore[arg-type]


def render_service_unit(*, python_executable: str, run_py: Path, repo_root: Path) -> str:
    # ``Type=simple``: the process serves the webhook and polls in-process,
    # so it stays up between events; ``Restart=on-failure`` rides out crashes.
    # ``%h`` is systemd's user-home specifier — expanded by the user manager,
    # so it must NOT go through the %-escaping path quoting helpers.
    return f"""[Unit]
Description=Hermes PR intent watch (live webhook + poll)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={format_exec_start([python_executable, str(run_py), "--serve"])}
WorkingDirectory={_format_path(str(repo_root))}
Environment=HERMES_HOME=%h/.hermes
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
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
    """Probe the user manager; a degraded-but-alive instance still runs units."""
    code, out, _err = runner(["systemctl", "--user", "is-system-running"])
    if code == 0:
        return True
    return code == 1 and "degraded" in (out or "").lower()


def _unit_enabled(runner: SystemctlRunner, name: str) -> bool:
    code, out, _err = runner(["systemctl", "--user", "is-enabled", name])
    return code == 0 and (out or "").strip() in {"enabled", "static"}


def _unit_active(runner: SystemctlRunner, name: str) -> bool:
    code, out, _err = runner(["systemctl", "--user", "is-active", name])
    return code == 0 and (out or "").strip() == "active"


def _unit_exists(unit_dir: Path, name: str) -> bool:
    try:
        return (unit_dir / name).is_file()
    except OSError:
        return False


def _stop_unit(runner: SystemctlRunner, name: str) -> list[str]:
    warnings: list[str] = []
    for action in ("stop", "disable"):
        code, _, err = runner(["systemctl", "--user", action, name])
        if code != 0:
            warnings.append(f"failed to {action} {name}: {err.strip() or code}")
    return warnings


def _retire_timer(runner: SystemctlRunner, unit_dir: Path) -> tuple[list[str], bool]:
    """Stop+disable+remove the legacy timer. Returns (warnings, did_something)."""
    if not _unit_exists(unit_dir, TIMER_NAME):
        return [], False
    warnings = _stop_unit(runner, TIMER_NAME)
    try:
        (unit_dir / TIMER_NAME).unlink()
    except OSError as exc:
        warnings.append(f"failed to remove {TIMER_NAME}: {exc}")
        return warnings, True
    return warnings, True


def _log_skip_once(reason: str) -> None:
    global _skip_logged
    if _skip_logged:
        return
    _skip_logged = True
    logger.info("pr_intent_watch scheduler self-install skipped: %s", reason)


def _load_raw_config() -> object | None:
    try:
        from plugins.pr_intent_watch.core import load_config_section

        return load_config_section(None)
    except Exception as exc:
        logger.warning(
            "pr_intent_watch scheduler could not read config (%s); using defaults",
            exc,
        )
        return None


def _reconcile(
    *,
    unit_dir: Path | None,
    run_systemctl: SystemctlRunner | None,
    config: object | None,
) -> ReconcileResult:
    raw = config if config is not None else _load_raw_config()

    if plugin_explicitly_disabled(raw):
        # The plugin owns the service, so disabling the plugin must retire a
        # previously installed one — otherwise it would serve forever.
        _log_skip_once("plugin explicitly disabled in config")
        target_dir = unit_dir or default_unit_dir()
        runner = run_systemctl or default_systemctl_runner
        warnings: list[str] = []
        for name in UNIT_NAMES:
            if _unit_exists(target_dir, name):
                warnings.extend(_stop_unit(runner, name))
        return ReconcileResult(
            skip_reason="plugin explicitly disabled", warnings=tuple(warnings)
        )

    if not is_linux():
        _log_skip_once("not Linux (systemd user manager unavailable)")
        return ReconcileResult(skip_reason="not Linux")

    runner = run_systemctl or default_systemctl_runner
    if not user_systemd_available(runner):
        _log_skip_once("systemd user manager unavailable")
        return ReconcileResult(skip_reason="systemd user manager unavailable")

    target_dir = unit_dir or default_unit_dir()
    # A leftover oneshot unit from the timer model is simply rewritten —
    # write_unit_if_changed swaps in the serve unit and triggers a reload.
    changed = write_unit_if_changed(
        target_dir / SERVICE_NAME,
        render_service_unit(
            python_executable=resolve_unit_python(),
            run_py=RUN_PY,
            repo_root=REPO_ROOT,
        ),
    )

    # The poll lives inside --serve now; a still-installed timer would
    # double-poll, so retire it and drop the unit file.
    timer_warnings, timer_touched = _retire_timer(runner, target_dir)
    changed = changed or timer_touched
    warnings = list(timer_warnings)

    if changed:
        code, _, err = runner(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            warnings.append(f"failed to daemon-reload: {err.strip() or code}")

    enabled = _unit_enabled(runner, SERVICE_NAME)
    active = _unit_active(runner, SERVICE_NAME)
    if not (enabled and active):
        # Idempotent self-heal: enable --now is safe on an already-enabled
        # service and starts it when it is merely inactive.
        code, _, err = runner(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
        if code != 0:
            warnings.append(f"failed to enable service: {err.strip() or code}")
        else:
            enabled = True
            active = True

    return ReconcileResult(
        changed=changed,
        enabled=enabled,
        active=active,
        timer_retired=timer_touched,
        warnings=tuple(warnings),
    )


def reconcile_scheduler_on_load(
    *,
    unit_dir: Path | None = None,
    run_systemctl: SystemctlRunner | None = None,
    config: object | None = None,
) -> ReconcileResult | None:
    """Install/reconcile the user service. Never raises into plugin load.

    Never call from ``run.py`` — the serve process must not re-reconcile its
    own scheduler. Returns None only when an unexpected error was contained.
    """
    try:
        return _reconcile(
            unit_dir=unit_dir, run_systemctl=run_systemctl, config=config
        )
    except Exception as exc:
        logger.warning(
            "pr_intent_watch scheduler reconcile skipped: %s",
            exc,
            exc_info=True,
        )
        return None
