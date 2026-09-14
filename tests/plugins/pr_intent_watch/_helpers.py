"""Shared helpers for pr_intent_watch behavior-contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import yaml

REPO = "QuixThe2nd/hermes-ide"

MARKER = "<!-- hermes-pr-intent-watch -->"

# A file payload exactly as GitHub returns it — patch field included — so the
# tests prove the adapter/metadata path strips it.
RAW_FILE = {
    "filename": "plugins/example/core.py",
    "status": "modified",
    "additions": 12,
    "deletions": 3,
    "patch": "@@ -10,4 +10,6 @@ def dangerous():\n-    old()\n+    new()",
}


def make_pull(
    number: int,
    *,
    title: str = "Fix the widget flake",
    body: str = "Widgets flake when idle. Reproducible: run X, see Y.",
    author: str = "contributor",
    draft: bool = False,
    labels: tuple[str, ...] = ("bug",),
    head_sha: str = "abc123",
    head_ref: str = "fix/widget",
    base_ref: str = "main",
    updated: str = "2026-08-28T00:00:00Z",
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "draft": draft,
        "user": {"login": author},
        "labels": [{"name": name} for name in labels],
        "head": {"sha": head_sha, "ref": head_ref},
        "base": {"ref": base_ref},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "updated_at": updated,
    }


def make_review(
    *,
    objective: str = "Make widgets stop flaking when idle.",
    worth: str = "yes",
    real_bug: str = "yes",
    rationale: str = "The symptom is described concretely and reproducibly.",
) -> dict:
    return {
        "objective": objective,
        "worth_considering": worth,
        "real_bug": real_bug,
        "rationale": rationale,
    }


def write_config(
    home: Path,
    section: Optional[Mapping[str, Any]] = None,
    *,
    extra: Optional[Mapping[str, Any]] = None,
) -> Path:
    config: dict[str, Any] = {}
    if section is not None:
        config["pr_intent_watch"] = dict(section)
    if extra:
        config.update(dict(extra))
    path = home / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def seed_state(
    home: Path,
    *,
    repo: str = REPO,
    seen: Optional[Mapping[str, Mapping[str, Any]]] = None,
    baseline_complete: bool = True,
) -> None:
    import json

    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "pr_intent_watch.json").write_text(
        json.dumps(
            {
                "repo": repo,
                "seen": dict(seen or {}),
                "baseline_complete": baseline_complete,
            }
        ),
        encoding="utf-8",
    )


def seen_entry(
    *,
    head_sha: str = "abc123",
    commented: bool = False,
    skipped: bool = False,
) -> dict:
    return {"head_sha": head_sha, "commented": commented, "skipped": skipped}


class FakeGitHub:
    """Stand-in for the ``github`` module surface ``run_tick`` calls.

    File payloads deliberately carry a ``patch`` key: the metadata the model
    receives must be clean even when the adapter boundary is bypassed.
    """

    def __init__(
        self,
        pulls: Iterable[dict],
        *,
        comments: Optional[dict[int, list[dict]]] = None,
        files: Optional[dict[int, list[dict]]] = None,
        commits: Optional[dict[int, list[str]]] = None,
        rate_limit_comments_on: Iterable[int] = (),
    ) -> None:
        self.pulls = list(pulls)
        self.comments = comments or {}
        self.files = files or {}
        self.commits = commits or {}
        self.rate_limit_comments_on = set(rate_limit_comments_on)
        self.posted: list[tuple[int, str]] = []
        self.calls: list[tuple[str, Any]] = []

    def resolve_token(self) -> str:
        return "gh-test-token"

    def list_open_pulls(self, repo, token, **kwargs):
        self.calls.append(("list_open_pulls", repo))
        return [dict(pull) for pull in self.pulls]

    def get_pull(self, repo, number, token, **kwargs):
        self.calls.append(("get_pull", number))
        for pull in self.pulls:
            if pull.get("number") == number:
                return dict(pull)
        return make_pull(number)

    def list_files(self, repo, number, token, **kwargs):
        self.calls.append(("list_files", number))
        return [dict(item) for item in self.files.get(number, [dict(RAW_FILE)])]

    def list_commits(self, repo, number, token, **kwargs):
        self.calls.append(("list_commits", number))
        return list(self.commits.get(number, ["fix: calm the widget flake"]))

    def list_issue_comments(self, repo, number, token, **kwargs):
        self.calls.append(("list_issue_comments", number))
        if number in self.rate_limit_comments_on:
            from plugins.pr_intent_watch.github import GitHubRateLimit

            raise GitHubRateLimit("rate limited (test)")
        # Marker detection stays the REAL github.has_intent_marker — run_tick
        # calls it directly, so the idempotency contract is exercised for real.
        return [dict(comment) for comment in self.comments.get(number, [])]

    def post_issue_comment(self, repo, number, token, body, **kwargs):
        self.calls.append(("post_issue_comment", number))
        self.posted.append((number, body))
        return {"id": len(self.posted), "body": body}


def install_fake_github(monkeypatch, fake: FakeGitHub) -> None:
    from plugins.pr_intent_watch import github as gh

    for name in (
        "resolve_token",
        "list_open_pulls",
        "get_pull",
        "list_files",
        "list_commits",
        "list_issue_comments",
        "post_issue_comment",
    ):
        monkeypatch.setattr(gh, name, getattr(fake, name), raising=True)


def install_fake_review(
    monkeypatch, result: Optional[dict]
) -> list[dict]:
    """Replace ``review.review_intent``; returns the captured metadata list."""
    from plugins.pr_intent_watch import review as review_module

    captured: list[dict] = []

    def _fake_review(metadata, **kwargs):
        captured.append(metadata)
        return result

    monkeypatch.setattr(review_module, "review_intent", _fake_review, raising=True)
    return captured


def fake_llm_response(text: str):
    """A minimal chat-completion-shaped object the real extractor understands."""
    import types

    message = types.SimpleNamespace(content=text, reasoning=None, reasoning_content=None)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message)],
        model="test-model",
    )


class FakeUrlopen:
    """Injectable ``urlopen``: canned JSON per URL substring, or a raiser."""

    def __init__(
        self,
        *,
        pages: Optional[dict[str, Any]] = None,
        raiser: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.pages = pages or {}
        self.raiser = raiser
        self.requests: list[Any] = []

    def __call__(self, request, timeout=None, **kwargs):
        self.requests.append(request)
        if self.raiser is not None:
            self.raiser(request)
        url = request.full_url
        for fragment, payload in self.pages.items():
            if fragment in url:
                return _FakeResponse(payload)
        return _FakeResponse([])


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        import json as _json

        self.status = 200
        self._raw = (
            payload if isinstance(payload, (bytes, bytearray)) else _json.dumps(payload).encode()
        )

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False
