"""The venv-style activate scripts (./activate, ./activate.ps1).

`source ./activate` must put the composed pm env into the CURRENT shell
without ever invoking uv: it runs the pm store's pinned python (fallback:
the repo venv) to emit `python -m pm.cli env`, then exports the result,
with a venv-activate-style `deactivate` that restores the prior state.
These are real input->output checks against a fake store (same layout the
pm store uses: facts.json + a python-<version>-<target> entry), following
tests/pm conventions for faking the store.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


from pm.store import current_target

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVATE = REPO_ROOT / "activate"
SETUP_HERMES_SH = REPO_ROOT / "setup-hermes.sh"
CANARY = "HERMES_PM_ACTIVATE_CANARY"


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _child_env() -> dict:
    return os.environ.copy()


def _fake_store(tmp_path: Path) -> tuple[Path, Path]:
    """A store laid out like pm's: facts.json marking the python package
    installed (identity matches pm/lock.json) with a canary env export, and
    an entry whose interpreter is a wrapper that runs the real python."""
    lock = json.loads(
        (REPO_ROOT / "pm" / "lock.json").read_text(encoding="utf-8-sig")
    )["packages"]
    target = current_target()
    python_pkg = lock["python"]
    sha = python_pkg["artifacts"][target]["sha256"]

    store = tmp_path / "store"
    entry = store / f"python-{python_pkg['version']}-{target}"
    entry.mkdir(parents=True)

    # The store interpreter only needs to run `python -m pm.cli env` —
    # delegate to the real interpreter via a #!/bin/sh wrapper (a copied
    # CPython would miss its stdlib; the wrapper is the honest minimal fake).
    interpreter = entry / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    real = Path(sys.executable)
    wrapper = "#!/bin/sh\nexec '%s' \"$@\"\n" % _posix(real)
    interpreter.write_text(wrapper, encoding="utf-8")
    interpreter.chmod(0o755)

    (store / "facts.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "packages": {
                    "python": {
                        "entry": entry.name,
                        "version": python_pkg["version"],
                        "env": {CANARY: "env-ok"},
                        "target": target,
                        "artifacts": [sha],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return store, entry


def _bash_env(store: Path) -> dict:
    env = os.environ.copy()
    env["HERMES_RUNTIME_DIR"] = _posix(store)
    # Keep the real env out of the composed pm output so the canary export
    # is the only thing activate adds beyond the ambient environment.
    env.pop(CANARY, None)
    return env


def test_bash_scripts_pass_syntax_check():
    for script in (ACTIVATE, SETUP_HERMES_SH):
        result = subprocess.run(
            [_bash(), "-n", _posix(script)], capture_output=True, text=True, env=_child_env()
        )
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_activate_never_invokes_uv():
    """The fast path must run the provisioned python directly — setup is
    the only place uv bootstrap logic lives."""
    source = ACTIVATE.read_text(encoding="utf-8")
    assert "uv run" not in source
    assert "ensure_pinned_uv" not in source


def test_source_activate_exports_the_pm_env(tmp_path: Path):
    store, _ = _fake_store(tmp_path)
    script = (
        f'source "{_posix(ACTIVATE)}" && '
        f'test -n "$__HERMES_ACTIVATED" && '
        f'printf "%s" "${CANARY}"'
    )
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=_posix(REPO_ROOT),
        env=_bash_env(store),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "env-ok"


def test_deactivate_restores_the_prior_shell(tmp_path: Path):
    store, _ = _fake_store(tmp_path)
    script = (
        f'source "{_posix(ACTIVATE)}" && deactivate && '
        f'test -z "${{{CANARY}+set}}" && '
        f'test -z "${{__HERMES_ACTIVATED+set}}" && '
        f"! declare -F deactivate >/dev/null && "
        f'echo restored'
    )
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=_posix(REPO_ROOT),
        env=_bash_env(store),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "restored"


def test_activate_fails_cleanly_without_a_store(tmp_path: Path):
    env = os.environ.copy()
    env["HERMES_RUNTIME_DIR"] = _posix(tmp_path / "empty-store")
    env.pop(CANARY, None)
    isolated = tmp_path / "no-install" / "activate"
    isolated.parent.mkdir()
    shutil.copy2(ACTIVATE, isolated)
    script = (
        f'source "{_posix(isolated)}" 2>/dev/null; '
        f'test $? -ne 0 && echo refused'
    )
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        cwd=_posix(REPO_ROOT),
        env=env,
    )
    # Without any provisioned python the source must refuse — never
    # silently no-op with a half-activated shell.
    assert "refused" in result.stdout
