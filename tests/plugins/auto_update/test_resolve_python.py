"""resolve_python_executable must keep the venv symlink path, not resolve it."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins.auto_update.platform import resolve_python_executable


def test_venv_symlink_path_is_preserved(tmp_path):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.symlink_to("/usr/bin/python3")
    with mock.patch.dict(os.environ, {"VIRTUAL_ENV": str(tmp_path / "venv")}):
        result = resolve_python_executable()
    assert result == str(python)
    # The unit interpreter must be the venv path itself: resolving would point
    # at a base interpreter without hermes_cli installed.
    assert not os.path.islink(result) or Path(result).parent.name == "bin"


def test_venv_without_python_falls_back(tmp_path):
    (tmp_path / "bin").mkdir()
    with mock.patch.dict(os.environ, {"VIRTUAL_ENV": str(tmp_path)}):
        fallback = shutil_which_py3() or sys.executable
        assert resolve_python_executable() == fallback


def shutil_which_py3():
    import shutil

    return shutil.which("python3")
