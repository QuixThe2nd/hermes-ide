"""Tests for the Honcho Discord notification patch packaging.

Covers manifest integrity, patch hashing, the apply/check/verify/rollback
script against the pristine fixture tree, refusal on modified targets,
preservation of unrelated local files, and secret non-exposure.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "plugins" / "memory" / "honcho" / "discord_notifications"
SCRIPT = PACKAGE_DIR / "apply_patch.py"
MANIFEST_PATH = PACKAGE_DIR / "manifest.json"
FIXTURE_TREE = (
    REPO_ROOT / "tests" / "plugins" / "memory" / "fixtures" / "honcho-3.0.10"
)

SECRET_VALUE = "super-secret-webhook-token-do-not-print-xyz"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def target(tmp_path: Path) -> Path:
    checkout = tmp_path / "honcho"
    shutil.copytree(FIXTURE_TREE, checkout)
    return checkout


def _run_script(target: Path, command: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--target", str(target), command],
        capture_output=True,
        text=True,
        env=env,
    )


def _git_apply_reverse_check(target: Path, patch: Path) -> bool:
    result = subprocess.run(
        ["git", "apply", "--check", "-R", str(patch)],
        cwd=target,
        capture_output=True,
    )
    return result.returncode == 0


def test_manifest_integrity_against_package_and_fixture(manifest: dict) -> None:
    patch_path = PACKAGE_DIR / manifest["patch_file"]
    assert patch_path.is_file()
    assert _sha256(patch_path) == manifest["patch_sha256"]

    for rel_path, expected in manifest["pristine_sha256"].items():
        fixture_file = FIXTURE_TREE / rel_path
        assert fixture_file.is_file(), f"fixture missing {rel_path}"
        assert _sha256(fixture_file) == expected, f"hash drift for {rel_path}"

    # Every pre-existing touched file must have a pristine hash recorded.
    for rel_path in manifest["touched_files"]:
        fixture_file = FIXTURE_TREE / rel_path
        if fixture_file.exists():
            assert rel_path in manifest["pristine_sha256"]

    for rel_path in manifest["ignored_local_files"]:
        assert (FIXTURE_TREE / rel_path).is_file()
        assert rel_path not in manifest["pristine_sha256"]

    assert manifest["target_version"] == "3.0.10"
    assert len(manifest["discord_webhook_env_vars"]) == 4


def test_patch_applies_cleanly_to_pristine_fixture(manifest: dict, target: Path) -> None:
    patch_path = PACKAGE_DIR / manifest["patch_file"]
    result = subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=target,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_check_command_accepts_pristine_fixture(manifest: dict, target: Path) -> None:
    result = _run_script(target, "check")
    assert result.returncode == 0, result.stderr
    assert "pristine" in result.stdout
    assert "Patch currently applied: no" in result.stdout


def test_apply_happy_path_and_verify(manifest: dict, target: Path) -> None:
    result = _run_script(target, "apply")
    assert result.returncode == 0, result.stderr

    patch_path = PACKAGE_DIR / manifest["patch_file"]
    assert _git_apply_reverse_check(target, patch_path)

    # The patch adds a new test file in the target tree.
    for rel_path in manifest["touched_files"]:
        assert (target / rel_path).exists(), f"missing after apply: {rel_path}"

    verify = _run_script(target, "verify")
    assert verify.returncode == 0, verify.stderr

    # Re-apply is idempotent.
    again = _run_script(target, "apply")
    assert again.returncode == 0
    assert "already applied" in again.stdout


def test_apply_refuses_modified_pristine_file(manifest: dict, target: Path) -> None:
    victim = target / "src" / "crud" / "document.py"
    original = victim.read_bytes()
    victim.write_bytes(original + b"\n# local edit\n")

    result = _run_script(target, "apply")
    assert result.returncode == 1
    assert "src/crud/document.py" in result.stderr

    # Tree must be untouched: file restored content intact, no new files.
    assert victim.read_bytes() == original + b"\n# local edit\n"
    assert not (target / "tests" / "webhooks" / "test_conclusion_created.py").exists()


def test_apply_refuses_wrong_version(manifest: dict, target: Path) -> None:
    pyproject = target / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'version = "3.0.10"', 'version = "3.0.11"'
        ),
        encoding="utf-8",
    )
    result = _run_script(target, "apply")
    assert result.returncode == 1
    assert "3.0.11" in result.stderr


def test_apply_preserves_unrelated_local_files(manifest: dict, target: Path) -> None:
    local_file = target / "src" / "llm" / "structured_output.py"
    local_edit = local_file.read_bytes() + b"\n# local customization\n"
    local_file.write_bytes(local_edit)

    result = _run_script(target, "apply")
    assert result.returncode == 0, result.stderr
    assert local_file.read_bytes() == local_edit
    assert "structured_output.py" in result.stdout


def test_rollback_restores_pristine_hashes(manifest: dict, target: Path) -> None:
    assert _run_script(target, "apply").returncode == 0
    result = _run_script(target, "rollback")
    assert result.returncode == 0, result.stderr

    for rel_path, expected in manifest["pristine_sha256"].items():
        assert _sha256(target / rel_path) == expected
    assert not (target / "tests" / "webhooks" / "test_conclusion_created.py").exists()

    verify = _run_script(target, "verify")
    assert verify.returncode == 1


def test_rollback_refused_when_not_applied(manifest: dict, target: Path) -> None:
    result = _run_script(target, "rollback")
    assert result.returncode == 1


@pytest.mark.parametrize("command", ["check", "apply"])
def test_secret_values_never_appear_in_output(manifest: dict, target: Path, command: str) -> None:
    env = dict(os.environ)
    for name in manifest["discord_webhook_env_vars"]:
        env[name] = f"https://discord.example/{SECRET_VALUE}"

    result = _run_script(target, command, env=env)
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert SECRET_VALUE not in combined
    assert "discord.example" not in combined
    # Names may be reported, values never.
    for name in manifest["discord_webhook_env_vars"]:
        if name in combined:
            assert f"{name}: set" in combined
