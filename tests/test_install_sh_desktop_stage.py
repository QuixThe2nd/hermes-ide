"""install.sh desktop stage: real invocation + authoritative stage list.

The desktop stage must delegate to the product CLI's parsed invocation
(``hermes desktop --build-only``) and the stage list must be authored ONCE:
``--manifest`` prints it and the no-flag ladder runs it, so
``--include-desktop`` affects the real run exactly as the manifest
advertises (audit C27's shell-side sibling).

Behavioral: the manifest is executed for real; stage_names()/stage_desktop()
are invoked after sourcing the installer with --manifest (the documented
sourcing contract — arg parsing prints the manifest and stops before main).
No product install/build happens: the venv python is a fake logging stub.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _manifest(*flags: str) -> list[str]:
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--manifest", *flags],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return [s["name"] for s in json.loads(result.stdout)["stages"]]


def test_manifest_without_desktop_lists_no_desktop() -> None:
    assert "desktop" not in _manifest()


def test_manifest_with_include_desktop_orders_desktop_before_complete() -> None:
    names = _manifest("--include-desktop")
    assert names.index("desktop") < names.index("complete")
    assert names[-1] == "complete"


def _shpath(path: Path) -> str:
    """Embed a Windows path in a bash script safely: forward slashes."""
    return str(path).replace("\\", "/")


def _source(prefix: str) -> str:
    """Source the installer (arg parsing prints the manifest and exits
    before main), so every function is defined and nothing runs."""
    return f'source "{INSTALL_SH}" --manifest >/dev/null\n{prefix}\n'


def test_stage_names_is_the_single_authoritative_list(tmp_path: Path) -> None:
    script = _source(
        f"""
INCLUDE_DESKTOP=false
printf '%s\\n' $(stage_names) > "{_shpath(tmp_path / "without.txt")}"
INCLUDE_DESKTOP=true
printf '%s\\n' $(stage_names) > "{_shpath(tmp_path / "with.txt")}"
"""
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert result.returncode == 0, result.stderr

    without = (tmp_path / "without.txt").read_text().split()
    with_desktop = (tmp_path / "with.txt").read_text().split()
    assert "desktop" not in without
    assert with_desktop.index("desktop") < with_desktop.index("complete")
    # The flag's ONLY effect on the list is inserting desktop.
    assert [n for n in with_desktop if n != "desktop"] == without


def test_stage_desktop_invokes_the_parsed_product_cli(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    venv_bin = install_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    log = tmp_path / "fake-python.log"
    fake_python = venv_bin / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{_shpath(log)}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (install_dir / "hermes").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    script = _source(
        f"""
INCLUDE_DESKTOP=true
INSTALL_DIR="{_shpath(install_dir)}"
stage_desktop
"""
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr

    lines = log.read_text().splitlines()
    assert lines[-1] == f"{_shpath(install_dir / 'hermes')} desktop --build-only", (
        "stage_desktop must invoke the parsed CLI flags, not a removed 'build' subcommand"
    )
