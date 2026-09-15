"""CLI for ``hermes papercuts autofix install|uninstall|status``."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hermes_constants import get_hermes_home

JOB_NAME = "daily-papercuts-autofix"
DEFAULT_SCHEDULE = "30 8 * * *"
DEFAULT_DELIVER = "local"
DEFAULT_MAX_FIXES = 3
DEFAULT_REMOTE = "origin"
DEFAULT_COMMIT_NAME = "Hermes Agent"
DEFAULT_COMMIT_EMAIL = "hermes-autofix@localhost"
ENABLED_TOOLSETS = ["papercuts", "file", "terminal", "skills", "session_search"]

_TEMPLATE_PATH = Path(__file__).resolve().parent / "autofix_prompt.md"
_PLACEHOLDERS = (
    "repo_path",
    "pr_remote",
    "base_repo",
    "commit_name",
    "commit_email",
    "max_fixes",
    "scratch_dir",
)

_GITHUB_HTTPS_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    re.IGNORECASE,
)


def default_repo_path() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_github_repo(url: str) -> Optional[str]:
    """Return ``owner/name`` from a GitHub remote URL, or None."""
    url = (url or "").strip()
    for pattern in (_GITHUB_HTTPS_RE, _GITHUB_SSH_RE):
        match = pattern.match(url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def render_template(values: Dict[str, Any]) -> str:
    """Render ``autofix_prompt.md`` with all placeholders substituted."""
    missing = [key for key in _PLACEHOLDERS if key not in values]
    if missing:
        raise ValueError(f"missing template values: {', '.join(sorted(missing))}")
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = template.format(**values)
    if "{" in rendered:
        raise ValueError("template rendering left unresolved placeholders")
    return rendered


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_config(repo: Path, key: str) -> str:
    result = _run_git(repo, "config", "--get", key)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def resolve_commit_identity(repo: Path) -> Tuple[str, str]:
    name = _git_config(repo, "user.name") or DEFAULT_COMMIT_NAME
    email = _git_config(repo, "user.email") or DEFAULT_COMMIT_EMAIL
    return name, email


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _verify_git_repo(repo: Path) -> Optional[str]:
    if not repo.is_dir():
        return f"--repo is not a directory: {repo}"
    if _run_git(repo, "rev-parse", "--git-dir").returncode != 0:
        return f"--repo is not a git checkout: {repo}"
    return None


def _verify_remote(repo: Path, remote: str) -> Tuple[Optional[str], Optional[str]]:
    result = _run_git(repo, "remote", "get-url", remote)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return None, f"git remote '{remote}' not found: {detail or 'unknown error'}"
    return (result.stdout or "").strip(), None


def _verify_gh() -> Optional[str]:
    if not shutil.which("gh"):
        return "gh CLI not found on PATH (install from https://cli.github.com/)"
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"gh is not authenticated: {detail or 'run gh auth login'}"
    return None


def find_job_by_name(name: str) -> Optional[Dict[str, Any]]:
    from cron.jobs import list_jobs

    matches = [job for job in list_jobs(include_disabled=True) if job.get("name") == name]
    if not matches:
        return None
    if len(matches) > 1:
        ids = ", ".join(job.get("id", "?") for job in matches)
        raise RuntimeError(f"multiple cron jobs named '{name}': {ids}")
    return matches[0]


def _resolve_install_params(args: argparse.Namespace) -> Tuple[Dict[str, Any], Optional[int]]:
    repo = Path(args.repo).resolve()
    err = _verify_git_repo(repo)
    if err:
        return {}, _fail(err)

    remote_url, err = _verify_remote(repo, args.remote)
    if err:
        return {}, _fail(err)

    base_repo = (args.base_repo or "").strip()
    if not base_repo:
        base_repo = parse_github_repo(remote_url or "")
        if not base_repo:
            return {}, _fail("only GitHub PR targets are supported")
    elif "://" in (remote_url or "") or (remote_url or "").startswith("git@"):
        # Explicit override: still require the remote itself to be GitHub when
        # the URL is parseable, so `gh pr create` does not fail at runtime.
        if parse_github_repo(remote_url or "") is None:
            return {}, _fail("only GitHub PR targets are supported")

    if int(args.max_fixes) < 1:
        return {}, _fail("--max-fixes must be >= 1")

    err = _verify_gh()
    if err:
        return {}, _fail(err)

    commit_name = (args.commit_name or "").strip()
    commit_email = (args.commit_email or "").strip()
    if not commit_name or not commit_email:
        default_name, default_email = resolve_commit_identity(repo)
        commit_name = commit_name or default_name
        commit_email = commit_email or default_email

    scratch_dir = str(get_hermes_home() / "scratch")
    values = {
        "repo_path": str(repo),
        "pr_remote": args.remote,
        "base_repo": base_repo,
        "commit_name": commit_name,
        "commit_email": commit_email,
        "max_fixes": int(args.max_fixes),
        "scratch_dir": scratch_dir,
    }
    return {
        "repo": repo,
        "base_repo": base_repo,
        "values": values,
        "schedule": args.schedule,
        "deliver": args.deliver,
    }, None


def cmd_install(args: argparse.Namespace) -> int:
    params, err_code = _resolve_install_params(args)
    if err_code is not None:
        return err_code

    try:
        prompt = render_template(params["values"])
    except ValueError as exc:
        return _fail(str(exc))

    from cron.jobs import create_job, update_job

    try:
        existing = find_job_by_name(JOB_NAME)
        if existing:
            job_id = existing["id"]
            updated = update_job(
                job_id,
                {
                    "prompt": prompt,
                    "schedule": params["schedule"],
                    "deliver": params["deliver"],
                    "enabled_toolsets": ENABLED_TOOLSETS,
                },
            )
            if updated is None:
                return _fail(f"failed to update existing cron job '{JOB_NAME}'")
            job = updated
            action = "updated"
        else:
            job = create_job(
                prompt=prompt,
                schedule=params["schedule"],
                name=JOB_NAME,
                deliver=params["deliver"],
                enabled_toolsets=ENABLED_TOOLSETS,
            )
            action = "created"
    except (ValueError, RuntimeError) as exc:
        return _fail(str(exc))

    print(f"Papercuts autofix cron job {action}.")
    print(f"  Job id:     {job.get('id')}")
    print(f"  Schedule:   {params['schedule']}")
    print(f"  Delivery:   {params['deliver']}")
    print(f"  Repo:       {params['repo']}")
    print(f"  PR target:  {params['base_repo']} (remote {params['values']['pr_remote']})")
    return 0


def cmd_uninstall() -> int:
    from cron.jobs import remove_job

    job = find_job_by_name(JOB_NAME)
    if job is None:
        print(f"No papercuts autofix cron job named '{JOB_NAME}'.")
        return 0

    job_id = job["id"]
    if not remove_job(job_id):
        return _fail(f"failed to remove cron job '{JOB_NAME}' ({job_id})")

    print(f"Removed papercuts autofix cron job '{JOB_NAME}' ({job_id}).")
    return 0


def cmd_status() -> int:
    job = find_job_by_name(JOB_NAME)
    if job is None:
        print(f"No papercuts autofix cron job named '{JOB_NAME}'.")
        print("Install with: hermes papercuts autofix install")
        return 0

    schedule = job.get("schedule_display") or job.get("schedule", {}).get("display") or job.get("schedule", {}).get("expr", "?")
    last_run = job.get("last_run_at") or "(never)"
    last_status = job.get("last_status") or "(none)"
    next_run = job.get("next_run_at") or "?"

    print(f"Papercuts autofix cron job '{JOB_NAME}' ({job.get('id')})")
    print(f"  Schedule:    {schedule}")
    print(f"  Last run:    {last_run}")
    print(f"  Last status: {last_status}")
    print(f"  Next run:    {next_run}")
    return 0


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes papercuts`` argparse tree."""
    subs = subparser.add_subparsers(dest="papercuts_command")
    autofix = subs.add_parser("autofix", help="Install or manage the daily autofix cron job")
    autofix_subs = autofix.add_subparsers(dest="autofix_command")

    install = autofix_subs.add_parser("install", help="Create or update the daily autofix cron job")
    install.add_argument(
        "--repo",
        default=str(default_repo_path()),
        help="Git checkout to clone from (default: repo containing this plugin)",
    )
    install.add_argument("--remote", default=DEFAULT_REMOTE, help="Git remote to push branches to")
    install.add_argument(
        "--base-repo",
        default=None,
        help="GitHub owner/name for gh pr create (default: parsed from --remote URL)",
    )
    install.add_argument("--schedule", default=DEFAULT_SCHEDULE, help="Cron schedule expression")
    install.add_argument("--deliver", default=DEFAULT_DELIVER, help="Cron delivery target")
    install.add_argument("--max-fixes", type=int, default=DEFAULT_MAX_FIXES, help="Max fixes per run")
    install.add_argument("--commit-name", default=None, help="Git commit author name")
    install.add_argument("--commit-email", default=None, help="Git commit author email")

    autofix_subs.add_parser("uninstall", help="Remove the daily autofix cron job")
    autofix_subs.add_parser("status", help="Show autofix cron job status")


def papercuts_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "papercuts_command", None)
    if sub != "autofix":
        print("usage: hermes papercuts autofix {install,uninstall,status}")
        return 2

    autofix = getattr(args, "autofix_command", None)
    if autofix == "install":
        return cmd_install(args)
    if autofix == "uninstall":
        return cmd_uninstall()
    if autofix == "status":
        return cmd_status()

    print("usage: hermes papercuts autofix {install,uninstall,status}")
    return 2
