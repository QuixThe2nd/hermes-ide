"""A CLI install must constrain its npm install to the root.

A bare ``npm install`` at the repo root resolves every workspace, including
optional native dependencies that may require a compiler. Since #85297 a failed
npm install aborts the whole install, so an unconstrained install is the
difference between a working CLI install and none (#38311, #38772).

These exercise the real ``node_deps_workspace_args`` from ``scripts/install.sh``.
``--manifest`` makes the installer print its stage manifest and stop before
``main``, so sourcing it defines every function without running an install.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def workspace_args(install_dir: Path) -> list[str]:
    """Return the npm arguments install.sh would use for ``install_dir``."""
    script = (
        f'source "{INSTALL_SH}" --manifest >/dev/null\n'
        f'node_deps_workspace_args "{install_dir}"\n'
        'printf "%s\\n" "${NODE_DEPS_WORKSPACE_ARGS[@]}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.split()


def make_checkout(root: Path) -> Path:
    """Lay out a checkout with a root package."""
    (root / "package.json").write_text('{"workspaces": []}')
    return root


def selected_workspaces(args: list[str]) -> list[str]:
    return [value for flag, value in zip(args, args[1:]) if flag == "--workspace"]


def test_install_is_always_constrained_to_the_root(tmp_path):
    """No checkout shape may fall back to an unconstrained npm install.

    Constraining npm is the whole point: an empty argument list would leave
    npm resolving every workspace.
    """
    args = workspace_args(make_checkout(tmp_path))

    assert args == ["--workspaces=false"]
