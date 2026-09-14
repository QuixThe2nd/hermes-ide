"""Tests for papercuts autofix CLI installer."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "papercuts"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def hermes_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with Path.home() redirected."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def git_repo(tmp_path):
    """Minimal git checkout with a GitHub origin remote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/widget.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _sample_values(repo: Path) -> dict:
    return {
        "repo_path": str(repo),
        "pr_remote": "origin",
        "base_repo": "acme/widget",
        "commit_name": "Hermes Agent",
        "commit_email": "hermes-autofix@localhost",
        "max_fixes": 3,
        "scratch_dir": "/tmp/scratch",
    }


def _install_args(repo: Path, **overrides) -> argparse.Namespace:
    defaults = {
        "repo": str(repo),
        "remote": "origin",
        "base_repo": None,
        "schedule": "every 1h",
        "deliver": "local",
        "max_fixes": 3,
        "commit_name": None,
        "commit_email": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _gh_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    real_run = subprocess.run

    def _run(cmd, **kwargs):
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "gh" and cmd[1] == "auth":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)


class TestTemplateRendering:
    def test_renders_all_placeholders(self, git_repo):
        from plugins.papercuts.cli import render_template

        rendered = render_template(_sample_values(git_repo))
        for token in (
            str(git_repo),
            "origin",
            "acme/widget",
            "Hermes Agent",
            "hermes-autofix@localhost",
            "3",
            "/tmp/scratch",
        ):
            assert token in rendered
        assert "{" not in rendered

    def test_missing_value_raises(self, git_repo):
        from plugins.papercuts.cli import render_template

        values = _sample_values(git_repo)
        del values["base_repo"]
        with pytest.raises(ValueError, match="missing template values"):
            render_template(values)


class TestGitHubUrlParsing:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/owner/repo", "owner/repo"),
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("https://gitlab.com/owner/repo.git", None),
        ],
    )
    def test_parse_github_repo(self, url, expected):
        from plugins.papercuts.cli import parse_github_repo

        assert parse_github_repo(url) == expected


class TestInstall:
    def test_install_creates_cron_job(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import JOB_NAME, cmd_install

        _gh_ok(monkeypatch)
        assert cmd_install(_install_args(git_repo)) == 0

        jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["name"] == JOB_NAME
        assert job["enabled_toolsets"] == [
            "papercuts",
            "file",
            "terminal",
            "skills",
            "session_search",
        ]
        assert str(git_repo) in job["prompt"]
        assert "acme/widget" in job["prompt"]

    def test_install_is_idempotent(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install

        _gh_ok(monkeypatch)
        assert cmd_install(_install_args(git_repo)) == 0
        first_id = list_jobs(include_disabled=True)[0]["id"]

        assert cmd_install(_install_args(git_repo, schedule="every 2h")) == 0
        jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 1
        assert jobs[0]["id"] == first_id
        assert jobs[0]["schedule"]["minutes"] == 120


class TestUninstallAndStatus:
    def test_uninstall_removes_job(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install, cmd_uninstall

        _gh_ok(monkeypatch)
        cmd_install(_install_args(git_repo))
        assert cmd_uninstall() == 0
        assert list_jobs(include_disabled=True) == []

    def test_status_when_no_job(self, hermes_env, capsys):
        from plugins.papercuts.cli import cmd_status

        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "No papercuts autofix cron job" in out


class TestPreflightFailures:
    def test_non_git_repo_fails(self, hermes_env, tmp_path, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install

        _gh_ok(monkeypatch)
        not_git = tmp_path / "not-git"
        not_git.mkdir()
        rc = cmd_install(_install_args(not_git))
        assert rc != 0
        assert list_jobs(include_disabled=True) == []

    def test_missing_remote_fails(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install

        _gh_ok(monkeypatch)
        rc = cmd_install(_install_args(git_repo, remote="missing"))
        assert rc != 0
        assert list_jobs(include_disabled=True) == []

    def test_gh_absent_fails(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        rc = cmd_install(_install_args(git_repo))
        assert rc != 0
        assert list_jobs(include_disabled=True) == []

    def test_gh_not_authed_fails(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
        real_run = subprocess.run

        def _run(cmd, **kwargs):
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "gh" and cmd[1] == "auth":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", _run)
        rc = cmd_install(_install_args(git_repo))
        assert rc != 0
        assert list_jobs(include_disabled=True) == []

    def test_explicit_base_repo_on_non_github_remote_fails(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install

        _gh_ok(monkeypatch)
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://gitlab.com/acme/widget.git"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        rc = cmd_install(_install_args(git_repo, base_repo="acme/widget"))
        assert rc != 0
        assert list_jobs(include_disabled=True) == []

    def test_negative_max_fixes_fails(self, hermes_env, git_repo, monkeypatch):
        from cron.jobs import list_jobs
        from plugins.papercuts.cli import cmd_install

        _gh_ok(monkeypatch)
        rc = cmd_install(_install_args(git_repo, max_fixes=-5))
        assert rc != 0
        assert list_jobs(include_disabled=True) == []


class TestPluginDiscovery:
    def test_discover_and_load_registers_cli_command(self, hermes_env):
        import yaml
        from hermes_cli.plugins import PluginManager

        cfg = hermes_env / "config.yaml"
        cfg.write_text(
            yaml.safe_dump({"plugins": {"enabled": ["papercuts"]}}),
            encoding="utf-8",
        )

        manager = PluginManager()
        manager.discover_and_load(force=True)

        assert "papercuts" in manager._cli_commands
        entry = manager._cli_commands["papercuts"]
        assert entry["name"] == "papercuts"
        assert entry["plugin"] == "papercuts"
        assert callable(entry["setup_fn"])
        assert callable(entry["handler_fn"])

        from tools.registry import registry

        tool_entry = registry.get_entry("papercuts")
        assert tool_entry is not None
        assert tool_entry.toolset == "papercuts"
