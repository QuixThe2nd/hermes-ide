"""GitHub REST adapter for pr_intent_watch — stdlib urllib only.

Every fetch is metadata-only by construction: the one endpoint that returns
a ``patch`` field (``/pulls/{n}/files``) is stripped here, at the adapter
boundary, so no downstream code (state, logs, LLM payload) can ever see a
diff hunk. The intent review works from titles, commit subjects, file names
and churn numbers only.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable, Mapping, Optional, Sequence

import urllib.error
import urllib.request

API_BASE = "https://api.github.com"
USER_AGENT = "hermes-pr-intent-watch"

# First line of every posted comment; also the idempotency marker checked
# against existing issue comments so a state-file loss never double-posts.
INTENT_MARKER = "<!-- hermes-pr-intent-watch -->"

#: Injectable ``urllib.request.urlopen`` stand-in for tests: any callable
#: accepting (Request, timeout=...) and returning a context manager whose
#: ``read()`` yields bytes.
UrlopenFn = Callable[..., Any]

RATE_LIMIT_STATUS = (403, 429)


class GitHubError(Exception):
    """A GitHub REST call failed (non-2xx, transport, or payload shape)."""


class GitHubRateLimit(GitHubError):
    """HTTP 403/429 — the tick should stop cleanly and retry next run."""


def resolve_token() -> Optional[str]:
    """GH_TOKEN, then GITHUB_TOKEN, then ``gh auth token``.

    Never logs or otherwise exposes the resolved value. The ``gh`` probe is
    best-effort: any failure (missing binary, not logged in, timeout) just
    means "no token from this source".
    """
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    token = (result.stdout or "").strip()
    return token or None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(
    path: str,
    token: str,
    *,
    method: str = "GET",
    body: Optional[Mapping[str, Any]] = None,
    urlopen: Optional[UrlopenFn] = None,
    timeout: float = 20.0,
) -> Any:
    """One REST call → parsed JSON. 403/429 raise GitHubRateLimit."""
    payload = None
    headers = _headers(token)
    if body is not None:
        payload = json.dumps(dict(body)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        API_BASE + path, data=payload, headers=headers, method=method
    )
    open_fn = urlopen or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        if status in RATE_LIMIT_STATUS:
            raise GitHubRateLimit(f"GitHub API rate limited (HTTP {status})") from exc
        detail = ""
        try:
            detail = exc.read().decode(errors="replace")[:200]
        except Exception:  # noqa: BLE001 — best-effort error context only
            detail = ""
        raise GitHubError(f"GitHub API HTTP {status} for {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub API unreachable for {path}: {exc.reason}") from exc
    except OSError as exc:
        raise GitHubError(f"GitHub API transport error for {path}: {exc}") from exc

    if status < 200 or status >= 300:
        raise GitHubError(f"GitHub API HTTP {status} for {path}")
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubError(f"GitHub API returned invalid JSON for {path}") from exc


def list_open_pulls(
    repo: str,
    token: str,
    *,
    per_page: int = 50,
    max_pages: int = 2,
    urlopen: Optional[UrlopenFn] = None,
) -> list[dict]:
    """Open PRs, newest-updated first, capped at ``max_pages`` pages."""
    pulls: list[dict] = []
    page = 1
    while page <= max_pages:
        data = _request(
            f"/repos/{repo}/pulls?state=open&sort=updated&direction=desc"
            f"&per_page={int(per_page)}&page={page}",
            token,
            urlopen=urlopen,
        )
        if not isinstance(data, list):
            raise GitHubError(f"GitHub API returned non-list pulls for {repo}")
        pulls.extend(item for item in data if isinstance(item, dict))
        if len(data) < per_page:
            break
        page += 1
    return pulls


def get_pull(
    repo: str,
    number: int,
    token: str,
    *,
    urlopen: Optional[UrlopenFn] = None,
) -> dict:
    data = _request(f"/repos/{repo}/pulls/{int(number)}", token, urlopen=urlopen)
    if not isinstance(data, dict):
        raise GitHubError(f"GitHub API returned non-object pull {number}")
    return data


def list_files(
    repo: str,
    number: int,
    token: str,
    *,
    max_files: int = 40,
    urlopen: Optional[UrlopenFn] = None,
) -> list[dict]:
    """File names + churn only — the ``patch`` field is dropped here."""
    data = _request(
        f"/repos/{repo}/pulls/{int(number)}/files?per_page={int(max_files)}",
        token,
        urlopen=urlopen,
    )
    if not isinstance(data, list):
        raise GitHubError(f"GitHub API returned non-list files for PR {number}")
    files: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        files.append(
            {
                "filename": str(item.get("filename") or ""),
                "status": str(item.get("status") or ""),
                "additions": int(item.get("additions") or 0),
                "deletions": int(item.get("deletions") or 0),
            }
        )
    return files[:max_files]


def list_commits(
    repo: str,
    number: int,
    token: str,
    *,
    max_commits: int = 20,
    urlopen: Optional[UrlopenFn] = None,
) -> list[str]:
    """Commit subject lines only (first line of each commit message)."""
    data = _request(
        f"/repos/{repo}/pulls/{int(number)}/commits?per_page={int(max_commits)}",
        token,
        urlopen=urlopen,
    )
    if not isinstance(data, list):
        raise GitHubError(f"GitHub API returned non-list commits for PR {number}")
    subjects: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        message = ""
        commit = item.get("commit")
        if isinstance(commit, dict):
            message = str(commit.get("message") or "")
        subjects.append(message.splitlines()[0].strip() if message.strip() else "")
    return [subject for subject in subjects if subject][:max_commits]


def list_issue_comments(
    repo: str,
    number: int,
    token: str,
    *,
    urlopen: Optional[UrlopenFn] = None,
) -> list[dict]:
    data = _request(
        f"/repos/{repo}/issues/{int(number)}/comments?per_page=100",
        token,
        urlopen=urlopen,
    )
    if not isinstance(data, list):
        raise GitHubError(f"GitHub API returned non-list comments for PR {number}")
    return [item for item in data if isinstance(item, dict)]


def has_intent_marker(comments: Sequence[Mapping[str, Any]]) -> bool:
    """True when any comment body carries the plugin's marker (substring)."""
    for comment in comments:
        if INTENT_MARKER in str((comment or {}).get("body") or ""):
            return True
    return False


def post_issue_comment(
    repo: str,
    number: int,
    token: str,
    body: str,
    *,
    urlopen: Optional[UrlopenFn] = None,
) -> dict:
    data = _request(
        f"/repos/{repo}/issues/{int(number)}/comments",
        token,
        method="POST",
        body={"body": body},
        urlopen=urlopen,
    )
    if not isinstance(data, dict):
        raise GitHubError(f"GitHub API returned non-object comment for PR {number}")
    return data
