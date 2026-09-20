"""CLI entrypoint subprocess tests for quota_channels.run."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUN_SCRIPT = _REPO_ROOT / "plugins" / "quota_channels" / "run.py"


def _subprocess_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def test_run_script_absolute_path_help_from_foreign_cwd(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(_RUN_SCRIPT), "--help"],
        cwd=tmp_path,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "quota_channels" in result.stdout or "usage:" in result.stdout


def test_run_module_help_from_repo_root() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "plugins.quota_channels.run", "--help"],
        cwd=_REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "quota_channels" in result.stdout or "usage:" in result.stdout
