"""End-to-end tests for scripts/check_pr_diff.py against synthetic git repos.

Every test builds a tiny repository in a tmp dir (a base commit plus a
branch commit carrying files of the right shapes) and runs the script as a
subprocess through the real ``git`` path — no mocks of git itself.

Run: python -m pytest tests/scripts/test_check_pr_diff.py
"""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_pr_diff.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "pr-diff-lint-test",
    "GIT_AUTHOR_EMAIL": "pr-diff-lint-test@example.invalid",
    "GIT_COMMITTER_NAME": "pr-diff-lint-test",
    "GIT_COMMITTER_EMAIL": "pr-diff-lint-test@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(GIT_ENV)
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def _build_repo(tmp_path: Path, branch_files: dict) -> Path:
    """A base commit with one product file and one test, then a branch
    commit adding ``branch_files`` on top of it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "hermes_cli").mkdir()
    (repo / "hermes_cli" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "pr-branch")
    for rel, content in branch_files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "pr change")
    return repo


def _run_gate(repo: Path, labels: str = ""):
    env = os.environ.copy()
    env["PR_LABELS"] = labels
    return subprocess.run(
        [sys.executable, str(SCRIPT), "main", "pr-branch"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_verdict(result, expected: str):
    verdict_lines = [
        line for line in result.stdout.splitlines() if line.startswith("VERDICT")
    ]
    assert verdict_lines, f"no VERDICT line in output:\n{result.stdout}\n{result.stderr}"
    assert verdict_lines[-1].startswith(expected), (
        f"expected {expected!r}, got {verdict_lines[-1]!r}"
        f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_product_only_change_fails_regression_gate(tmp_path):
    repo = _build_repo(tmp_path, {"hermes_cli/feature.py": "X = 1\n"})
    result = _run_gate(repo)
    assert result.returncode == 1
    _assert_verdict(result, "VERDICT FAIL")
    assert "REGRESSION-TEST" in result.stdout
    assert "hermes_cli/feature.py" in result.stdout


def test_product_and_test_change_passes(tmp_path):
    repo = _build_repo(
        tmp_path,
        {
            "hermes_cli/feature.py": "X = 1\n",
            "tests/test_feature.py": "def test_feature():\n    assert True\n",
        },
    )
    result = _run_gate(repo)
    assert result.returncode == 0
    _assert_verdict(result, "VERDICT PASS")


def test_docs_only_change_passes_via_escape_hatch(tmp_path):
    repo = _build_repo(
        tmp_path,
        {
            "docs/guide.md": "# Guide\n",
            "pyproject.toml": '[project]\nname = "x"\n',
            "skills/demo/SKILL.md": "---\nname: demo\n---\n",
        },
    )
    result = _run_gate(repo)
    assert result.returncode == 0
    _assert_verdict(result, "VERDICT PASS")
    assert "docs/config/chore" in result.stdout


def test_product_only_change_admitted_by_no_tests_needed_label(tmp_path):
    repo = _build_repo(tmp_path, {"hermes_cli/feature.py": "X = 1\n"})
    result = _run_gate(repo, labels="enhancement, no-tests-needed")
    assert result.returncode == 0
    _assert_verdict(result, "VERDICT PASS")
    assert "no-tests-needed" in result.stdout


def test_lockfile_only_change_passes_with_deps_label(tmp_path):
    repo = _build_repo(tmp_path, {"package-lock.json": '{"lockfileVersion": 3}\n'})
    result = _run_gate(repo, labels="deps")
    assert result.returncode == 0
    _assert_verdict(result, "VERDICT PASS")


def test_lockfile_minority_without_label_fails_scope_gate(tmp_path):
    # Lockfile is 1 of 2 changed files — 50%, not a strict majority — and no
    # `deps` label, so the scope gate must flag it.
    repo = _build_repo(
        tmp_path,
        {
            "package-lock.json": '{"lockfileVersion": 3}\n',
            "docs/guide.md": "# Guide\n",
        },
    )
    result = _run_gate(repo)
    assert result.returncode == 1
    _assert_verdict(result, "VERDICT FAIL")
    assert "SCOPE" in result.stdout
    assert "package-lock.json" in result.stdout


def test_product_change_with_lockfile_fails_scope_gate(tmp_path):
    repo = _build_repo(
        tmp_path,
        {
            "hermes_cli/feature.py": "X = 1\n",
            "tests/test_feature.py": "def test_feature():\n    assert True\n",
            "package-lock.json": '{"lockfileVersion": 3}\n',
        },
    )
    result = _run_gate(repo)
    assert result.returncode == 1
    _assert_verdict(result, "VERDICT FAIL")
    assert "SCOPE" in result.stdout
    assert "package-lock.json" in result.stdout


def test_majority_ci_pr_admits_workflow_change(tmp_path):
    repo = _build_repo(
        tmp_path,
        {
            ".github/workflows/lint.yml": "name: lint\n",
            ".github/workflows/tests.yml": "name: tests\n",
            "hermes_cli/feature.py": "X = 1\n",
        },
    )
    result = _run_gate(repo, labels="no-tests-needed")
    assert result.returncode == 0
    _assert_verdict(result, "VERDICT PASS")


def test_minority_workflow_change_without_label_fails_scope_gate(tmp_path):
    repo = _build_repo(
        tmp_path,
        {
            ".github/workflows/lint.yml": "name: lint\n",
            "hermes_cli/feature.py": "X = 1\n",
            "tests/test_feature.py": "def test_feature():\n    assert True\n",
        },
    )
    result = _run_gate(repo)
    assert result.returncode == 1
    _assert_verdict(result, "VERDICT FAIL")
    assert "SCOPE" in result.stdout
    assert ".github/workflows/lint.yml" in result.stdout


def test_empty_diff_passes(tmp_path):
    repo = _build_repo(tmp_path, {"docs/guide.md": "# Guide\n"})
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "main", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    _assert_verdict(result, "VERDICT PASS")
