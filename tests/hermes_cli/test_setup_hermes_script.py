from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "setup-hermes.sh"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"


def test_setup_hermes_script_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_script_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_script_has_termux_path():
    # The Termux installer branch lives in scripts/install.sh (setup-hermes.sh
    # is now a dev-environment wrapper that delegates to pm; there is no pip
    # fallback tier there on purpose). The retained Android path keeps its
    # is_termux detection, pip-based extras tiers and $PREFIX/bin command link.
    content = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "is_termux()" in content
    assert ".[termux]" in content
    assert ".[termux-all]" in content
    assert "$PREFIX/bin" in content
    # The installer must not reference constraints-termux.txt: the file was
    # deleted by the upstream package-manager migration and pip aborts on a
    # missing constraint file before resolving any Termux tier.
    assert "pip install -e '.[termux-all]' -c constraints-termux.txt" not in content
