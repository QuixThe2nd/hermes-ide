"""Windows binary resolution must prefer the runnable ``.cmd``/``.exe``/``.bat`` wrapper over npm's
POSIX ``#!/bin/sh`` shim, which shares the bare name and fails ``CreateProcess`` with WinError 193."""

import subprocess
from pathlib import Path

import pytest

from agent.lsp import install


def test_windows_candidates_order_wrappers_before_bare_shim(tmp_path: Path):
    base = tmp_path / "pyright-langserver"
    win = [p.name for p in install._native_binary_candidates(base, is_windows=True)]
    assert win == ["pyright-langserver.cmd", "pyright-langserver.exe", "pyright-langserver.bat", "pyright-langserver"]
    assert install._native_binary_candidates(base, is_windows=False) == [base]


@pytest.mark.windows_only
def test_existing_binary_resolves_runnable_cmd_over_posix_shim(tmp_path: Path, monkeypatch):
    """Live: staging dir holds npm's shim AND its .cmd; the resolved path must actually run."""
    monkeypatch.setattr(install, "hermes_lsp_bin_dir", lambda: tmp_path)
    shim = tmp_path / "tool"
    shim.write_text("#!/bin/sh\nexec node \"$0.js\" \"$@\"\n")
    (tmp_path / "tool.cmd").write_text("@echo off\r\necho wrapper-ran\r\n")

    resolved = install._existing_binary("tool")

    assert resolved == str(tmp_path / "tool.cmd")
    out = subprocess.run([str(resolved)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         check=True, stdin=subprocess.DEVNULL)
    assert "wrapper-ran" in out.stdout
