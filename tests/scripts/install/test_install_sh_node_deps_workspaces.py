"""A CLI install must select only the workspaces it needs.

A bare ``npm install`` at the repo root resolves every workspace, including
optional native dependencies that may require a compiler. Since #85297 a failed
npm install aborts the whole install, so constraining the workspace set is the
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


def make_checkout(root: Path, workspaces: tuple[str, ...]) -> Path:
    """Lay out a checkout with a root package and the given workspace dirs."""
    (root / "package.json").write_text('{"workspaces": ["apps/shared", "ui-tui", "web"]}')
    for workspace in ("apps/shared", *workspaces):
        directory = root / workspace
        directory.mkdir(parents=True)
        (directory / "package.json").write_text("{}")
    return root


def selected_workspaces(args: list[str]) -> list[str]:
    return [value for flag, value in zip(args, args[1:]) if flag == "--workspace"]


@pytest.mark.parametrize(
    "workspaces",
    [("ui-tui", "web"), ("ui-tui",), ("web",), ()],
    ids=["full", "tui-only", "web-only", "bare"],
)
def test_workspace_selection_is_always_constrained(tmp_path, workspaces):
    """No checkout shape may fall back to an unconstrained npm install."""
    args = workspace_args(make_checkout(tmp_path, workspaces))

    # Constraining npm is the whole point: an empty argument list would leave
    # npm resolving every workspace.
    assert args, "npm must be constrained, otherwise every workspace resolves"
    assert "--workspaces=false" in args or selected_workspaces(args)


def test_present_workspaces_are_installed_alongside_the_root(tmp_path):
    """ui-tui and web are what a CLI install needs; the root owns shared
    devDependencies that a scoped install would otherwise prune."""
    args = workspace_args(make_checkout(tmp_path, ("ui-tui", "web")))

    assert selected_workspaces(args) == ["ui-tui", "web"]
    assert "--include-workspace-root" in args


def test_absent_workspace_is_not_named(tmp_path):
    """npm fails hard on a workspace it cannot find, so a partial checkout
    must only name the workspaces that exist."""
    args = workspace_args(make_checkout(tmp_path, ("ui-tui",)))

    assert selected_workspaces(args) == ["ui-tui"]


def test_bare_checkout_installs_the_root_only(tmp_path):
    """With no installable workspace, npm still must not walk other workspaces."""
    args = workspace_args(make_checkout(tmp_path, ()))

    assert args == ["--workspaces=false"]
