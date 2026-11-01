"""Installer delegates dependency selection to the pinned PM bootstrap.

Also covers Hermes-managed Python provenance and venv recovery on Windows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.install_ps1_fake_uv import compile_fake_uv


pytestmark = pytest.mark.platforms("windows")

_INSTALL_PS1 = Path(__file__).resolve().parents[3] / "scripts" / "install.ps1"


def test_fresh_install_manifest_orders_repo_before_checkout_scoped_python(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is required")

    install_dir = tmp_path / "install"
    run = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_INSTALL_PS1),
            "-Manifest",
            "-HermesHome",
            str(tmp_path / "hermes-home"),
            "-InstallDir",
            str(install_dir),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    manifest = json.loads(run.stdout)
    stages = [stage["name"] for stage in manifest["stages"]]
    assert (
        stages.index("repository")
        < stages.index("python")
        < stages.index("venv")
    )
    assert not install_dir.exists(), "manifest lookup must remain read-only"


def _run_venv_stage(
    powershell: str,
    tmp_path: Path,
    hermes_home: Path,
    install_dir: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_INSTALL_PS1),
            "-Stage",
            "venv",
            "-HermesHome",
            str(hermes_home),
            "-InstallDir",
            str(install_dir),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )


def test_venv_stage_rejects_third_party_python_and_uses_managed_path(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is required")

    hermes_home = tmp_path / "hermes-home"
    install_dir = tmp_path / "install"
    managed_root = install_dir / ".hermes-runtime" / "python"
    managed_python = managed_root / "cpython-3.11" / "python.exe"
    third_party = tmp_path / "KiCad" / "bin" / "python.exe"
    log = tmp_path / "uv.log"
    uv = hermes_home / "bin" / "uv.exe"
    uv.parent.mkdir(parents=True)
    install_dir.mkdir()
    managed_python.parent.mkdir(parents=True)
    third_party.parent.mkdir(parents=True)
    third_party.write_text("fake", encoding="ascii")
    compile_fake_uv(powershell, uv)
    shutil.copy2(uv, managed_python)

    env = {
        **os.environ,
        "FAKE_UV_LOG": str(log),
        "FAKE_MANAGED_PYTHON": str(managed_python),
        "FAKE_THIRD_PARTY_PYTHON": str(third_party),
        "UV_PYTHON": str(third_party),
        "UV_NO_MANAGED_PYTHON": "1",
        "UV_SYSTEM_PYTHON": "1",
    }
    run = _run_venv_stage(powershell, tmp_path, hermes_home, install_dir, env)
    installer_stdout = run.stdout
    installer_stderr = run.stderr
    frames = [
        json.loads(line) for line in installer_stdout.splitlines() if line.startswith("{")
    ]
    assert run.returncode == 0, installer_stdout + installer_stderr
    assert frames[-1]["ok"] is True
    commands = log.read_text(encoding="utf-8").splitlines()
    assert any(
        command.startswith("python find 3.11") and "--managed-python" in command
        for command in commands
    )
    venv_command = next(command for command in commands if command.startswith("venv venv"))
    assert f"--python {managed_python}" in venv_command
    assert "--managed-python" in venv_command
    assert "--no-python-downloads" in venv_command


def test_fallback_minor_is_reported_from_resolved_managed_interpreter(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is required")

    hermes_home = tmp_path / "hermes-home"
    install_dir = tmp_path / "install"
    managed_python = (
        install_dir / ".hermes-runtime" / "python" / "cpython-3.12" / "python.exe"
    )
    uv = hermes_home / "bin" / "uv.exe"
    uv.parent.mkdir(parents=True)
    managed_python.parent.mkdir(parents=True)
    install_dir.mkdir(exist_ok=True)
    compile_fake_uv(powershell, uv)
    shutil.copy2(uv, managed_python)
    env = {
        **os.environ,
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_MANAGED_PYTHON": str(managed_python),
        "FAKE_MANAGED_PYTHON_VERSION": "3.12",
        "FAKE_PYTHON_VERSION": "Python 3.12.13",
    }

    run = _run_venv_stage(powershell, tmp_path, hermes_home, install_dir, env)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "Creating virtual environment with Python 3.12" in run.stdout
    assert "Virtual environment ready (Python 3.12)" in run.stdout
    created_python = install_dir / "venv" / "Scripts" / "python.exe"
    version = subprocess.run(
        [str(created_python), "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == "Python 3.12.13"


def test_python_find_drains_large_stderr_without_deadlock(tmp_path: Path) -> None:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is required")

    hermes_home = tmp_path / "hermes-home"
    install_dir = tmp_path / "install"
    managed_python = (
        install_dir / ".hermes-runtime" / "python" / "cpython-3.11" / "python.exe"
    )
    uv = hermes_home / "bin" / "uv.exe"
    uv.parent.mkdir(parents=True)
    managed_python.parent.mkdir(parents=True)
    compile_fake_uv(powershell, uv)
    shutil.copy2(uv, managed_python)
    env = {
        **os.environ,
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_MANAGED_PYTHON": str(managed_python),
        "FAKE_UV_FIND_STDERR_BYTES": str(1024 * 1024),
    }

    run = _run_venv_stage(powershell, tmp_path, hermes_home, install_dir, env)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "Creating virtual environment with Python 3.11" in run.stdout


def test_python_find_timeout_kills_uv_and_fails_stage(tmp_path: Path) -> None:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is required")

    hermes_home = tmp_path / "hermes-home"
    install_dir = tmp_path / "install"
    uv = hermes_home / "bin" / "uv.exe"
    uv.parent.mkdir(parents=True)
    install_dir.mkdir()
    compile_fake_uv(powershell, uv)
    env = {
        **os.environ,
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_UV_FIND_DELAY_MS": "60000",
    }

    run = _run_venv_stage(powershell, tmp_path, hermes_home, install_dir, env)

    assert run.returncode != 0
    assert "uv python find 3.11 timed out after 30000 ms" in (run.stdout + run.stderr)


def test_venv_failure_fails_stage_and_restores_existing_environment(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is required")

    hermes_home = tmp_path / "hermes-home"
    install_dir = tmp_path / "install"
    managed_python = (
        install_dir / ".hermes-runtime" / "python" / "cpython-3.11" / "python.exe"
    )
    old_python = install_dir / "venv" / "Scripts" / "python.exe"
    uv = hermes_home / "bin" / "uv.exe"
    for directory in (uv.parent, managed_python.parent, old_python.parent):
        directory.mkdir(parents=True, exist_ok=True)
    old_python.write_text("previous environment", encoding="ascii")
    compile_fake_uv(powershell, uv)
    shutil.copy2(uv, managed_python)
    env = {
        **os.environ,
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_MANAGED_PYTHON": str(managed_python),
        "FAKE_UV_VENV_EXIT": "37",
    }

    run = _run_venv_stage(powershell, tmp_path, hermes_home, install_dir, env)

    assert run.returncode != 0
    assert "Failed to create virtual environment (uv venv exited with 37)" in (
        run.stdout + run.stderr
    )
    assert old_python.read_text(encoding="ascii") == "previous environment"
    frames = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    assert frames[-1]["ok"] is False


@pytest.mark.parametrize("exit_code", [0, 17])
def test_dependency_stage_delegates_to_pinned_pm_without_touching_existing_env(
    tmp_path: Path, exit_code: int
) -> None:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is required")

    install_dir = tmp_path / "install"
    (install_dir / "pm").mkdir(parents=True)
    (install_dir / "pm" / "lock.json").write_text(
        json.dumps({"packages": {"python": {"version": "3.12.8+fixture"}}})
    )
    (install_dir / "uv.lock").write_text("fixture lock\n")
    existing = install_dir / "venv" / "sentinel"
    existing.parent.mkdir(parents=True)
    existing.write_text("previous generation")
    venv_python = install_dir / "venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir()
    compile_fake_uv(powershell, venv_python)

    log = tmp_path / "uv-calls.jsonl"
    wrapper = tmp_path / "boundary.ps1"
    wrapper.write_text(
        r'''
param([string]$Installer, [string]$InstallDir, [string]$HomeDir, [string]$Log)
$ErrorActionPreference = 'Stop'
function uv {
    ConvertTo-Json -Compress -InputObject @($args) | Add-Content -Encoding UTF8 $Log
    $global:LASTEXITCODE = [int]$env:PROBE_EXIT
}
function Invoke-WebRequest { throw 'network access outside test boundary' }
& $Installer -Stage dependencies -Json -InstallDir $InstallDir -HermesHome $HomeDir
''',
        encoding="utf-8-sig",
    )
    run = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            "-Installer",
            str(_INSTALL_PS1),
            "-InstallDir",
            str(install_dir),
            "-HomeDir",
            str(tmp_path / "home"),
            "-Log",
            str(log),
        ],
        cwd=tmp_path,
        env=dict(os.environ, PROBE_EXIT=str(exit_code)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert (run.returncode == 0) == (exit_code == 0), run.stdout + run.stderr
    commands = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8-sig").splitlines()
    ]
    assert commands[0] == [
        "run",
        "--no-project",
        "--python",
        "3.12",
        "python",
        "-m",
        "pm.cli",
        "install",
    ]
    if exit_code:
        assert len(commands) > 1, "fork fallback tiers should remain available"
    assert existing.read_text() == "previous generation"
