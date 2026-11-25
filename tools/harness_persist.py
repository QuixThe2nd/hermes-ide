"""Keep an unrelated tool harness alive across a gateway unit stop.

KillMode=mixed plus gateway.cgroup_cleanup SIGKILLs every process left in the
gateway service cgroup. subprocess.run and systemd-run --scope both stay in
that cgroup. A transient system service does not, so a restart can adopt its
pid from the process checkpoint instead of losing the tool call.
"""

from __future__ import annotations

import os
import sys
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Mapping, Optional


def caller_killed_service_cgroup() -> Optional[str]:
    """Cgroup path when a unit stop will kill this process, else None."""
    if not os.environ.get("INVOCATION_ID"):
        return None
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8-sig")
    except OSError:
        return None
    match = re.search(r"^0::(.+)$", text, re.MULTILINE)
    if not match:
        return None
    path = match.group(1).strip()
    return path if path.endswith(".service") else None


def _user_bus_env() -> Optional[dict]:
    try:
        from tools.process_registry import systemd_user_bus_env
    except Exception:
        return None
    return systemd_user_bus_env()


def _systemctl_show(unit: str, prop: str) -> str:
    # ``systemctl show`` exits 0 with default values (ActiveState=inactive) for a unit the
    # manager has never heard of, so the system manager would mask a user-manager unit.
    # Skip a manager whose LoadState is not-found.
    for prefix in ([], ["--user"]):
        try:
            result = subprocess.run(
                ["systemctl", *prefix, "show", unit, "-p", "LoadState", "-p", prop],
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                encoding="utf-8", errors="replace", env=_user_bus_env() if prefix else None,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        values = dict(
            line.split("=", 1) for line in (result.stdout or "").splitlines() if "=" in line
        )
        if values.get("LoadState", "").strip() == "not-found":
            continue
        value = values.get(prop, "").strip()
        if value:
            return value
    return ""


def active_service_main_pid(unit: str) -> Optional[int]:
    """Main pid of a still-running transient service, or None."""
    if not unit or _systemctl_show(unit, "ActiveState") not in ("active", "activating"):
        return None
    try:
        pid = int(_systemctl_show(unit, "MainPID") or "0")
    except ValueError:
        return None
    return pid or None


def _start_transient_service(cmd: list[str]) -> None:
    """Run a ``systemd-run`` transient-service argv on the system manager, then the user manager.

    A system-level gateway (``User=`` unit) may start a system transient service; a gateway that
    runs as a *user* service is refused by polkit ("Access denied") on the system manager, which
    made every detached harness die in 0s. A user-manager transient service is still outside the
    gateway service cgroup, so it survives a gateway unit stop just the same.
    """
    def _run(argv: list[str], env: Optional[Mapping[str, str]] = None) -> None:
        subprocess.run(argv, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30, env=env)

    try:
        _run(cmd)
        return
    except subprocess.CalledProcessError as system_exc:
        first_error = system_exc

    user_cmd = [cmd[0], "--user", *cmd[1:]]
    try:
        _run(user_cmd, _user_bus_env())
    except subprocess.CalledProcessError:
        raise first_error from None


def launch_detached_service(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    unit: Optional[str] = None,
) -> dict:
    """Start argv in its own transient service (system manager, else user manager). The returned pid is not in the caller cgroup."""
    unit_name = unit or f"hermes-harness-{uuid.uuid4().hex[:12]}.service"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    code_path = stdout_path.with_name(stdout_path.name + ".code")
    if code_path.exists():
        code_path.unlink()
    # No ".py" suffix: the wrapper lands next to the stdout log, which can be a
    # checkout root (agent_cli_runner's ".hermes-agent-cli.out"); a "*.py" file
    # there is scanned as a root module by setup.py. It runs as
    # ``sys.executable <path>``, so the suffix is not needed.
    runner = stdout_path.with_name(stdout_path.name + ".run")
    runner.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "proc = subprocess.run(sys.argv[2:])\n"
        "Path(sys.argv[1]).write_text(str(proc.returncode))\n"
        "raise SystemExit(proc.returncode)\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(runner), str(code_path), *argv]
    cmd = [
        "systemd-run", "--collect", f"--unit={unit_name}", "--property=Type=exec",
        f"--working-directory={cwd}",
        f"--property=StandardOutput=file:{stdout_path}",
        f"--property=StandardError=file:{stderr_path}",
    ]
    for key in sorted(env):
        value = env[key]
        if value is None or any(char in str(value) for char in ("\n", "\x00")):
            continue
        cmd.append(f"--setenv={key}={value}")
    cmd.append("--")
    cmd.extend(str(part) for part in argv)
    _start_transient_service(cmd)
    deadline = time.time() + 15
    pid = None
    while time.time() < deadline:
        pid = active_service_main_pid(unit_name)
        if pid:
            break
        state = _systemctl_show(unit_name, "ActiveState")
        if state in ("inactive", "failed"):
            raw = _systemctl_show(unit_name, "ExecMainPID") or "0"
            pid = int(raw) if raw.isdigit() and int(raw) else None
            break
        if state == "":
            # systemd-run returned, so the unit was started; unknown to every manager now
            # means --collect already reaped a fast exit.
            state = "inactive"
            break
        time.sleep(0.1)
    else:
        state = ""
    if pid is None and state not in ("inactive", "failed"):
        raise RuntimeError(f"harness unit {unit_name} did not report a main pid")
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8-sig").strip()
    except OSError:
        cgroup = "exited"
    return {
        "unit": unit_name,
        "pid": pid,
        "cgroup": cgroup,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "code_path": str(code_path),
    }


def wait_service(unit: str, timeout_seconds: int = 0) -> int:
    """Block until unit leaves the running state. Stop it if timeout_seconds elapses."""
    started = time.monotonic()
    while True:
        state = _systemctl_show(unit, "ActiveState")
        if state in ("inactive", "failed"):
            raw = _systemctl_show(unit, "ExecMainStatus") or "0"
            try:
                return int(raw)
            except ValueError:
                return 1
        if state == "" and (time.monotonic() - started) > 2:
            return 0
        if timeout_seconds and (time.monotonic() - started) > timeout_seconds:
            from tools.process_registry import _stop_systemd_unit

            _stop_systemd_unit(unit)
            raise subprocess.TimeoutExpired(unit, timeout_seconds)
        time.sleep(0.5)


def register_surviving_harness(*, command: str, pid: int, unit: str, cwd: str) -> str:
    """Checkpoint the service pid so gateway startup adopts it instead of losing the tool call."""
    from gateway.session_context import get_session_env
    from tools.process_registry import process_registry
    from tools.session_context import get_current_session_key
    from tools.terminal_tool_background import _stamp_gateway_routing

    session = process_registry._new_session(
        command, "", "", get_current_session_key(default="") or "", cwd,
    )
    session.pid = pid
    session.host_start_time = process_registry._safe_host_start_time(pid)
    session.systemd_unit = unit
    session.notify_on_complete = True
    session.watcher_interval = 5
    session.pid_scope = "host"
    _stamp_gateway_routing(session, get_session_env)
    with process_registry._lock:
        process_registry._running[session.id] = session
    process_registry._write_checkpoint()
    return session.id
