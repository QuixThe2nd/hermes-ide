"""GitHub adapter contracts: auth, error mapping, pagination, patch stripping."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from plugins.pr_intent_watch import github as gh
from tests.plugins.pr_intent_watch._helpers import MARKER, REPO, FakeUrlopen, make_pull

TOKEN = "unit-test-token"


def _http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", {}, io.BytesIO(body)
    )


# ── resolve_token ───────────────────────────────────────────────────────────


def test_env_tokens_win_in_order(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "from-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "from-github")
    assert gh.resolve_token() == "from-gh"
    monkeypatch.delenv("GH_TOKEN")
    assert gh.resolve_token() == "from-github"


def test_gh_cli_token_used_when_env_absent(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "cli-token\n"
        stderr = ""

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    recorded: list[list[str]] = []

    def fake_run(args, **kwargs):
        assert kwargs.get("timeout") == 5
        recorded.append(list(args))
        return _Result()

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    assert gh.resolve_token() == "cli-token"
    assert recorded == [["gh", "auth", "token"]]


def test_gh_cli_failure_is_swallowed(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "not logged in"

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        gh.subprocess, "run", lambda args, **kw: _Result()
    )
    assert gh.resolve_token() is None


def test_gh_cli_missing_binary_is_swallowed(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def boom(args, **kwargs):
        raise FileNotFoundError("no gh")

    monkeypatch.setattr(gh.subprocess, "run", boom)
    assert gh.resolve_token() is None


def test_no_token_source_anywhere(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gh.subprocess, "run", lambda args, **kw: _Result())
    assert gh.resolve_token() is None


# ── request plumbing ────────────────────────────────────────────────────────


def test_headers_and_user_agent():
    headers = gh._headers(TOKEN)
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["User-Agent"] == "hermes-pr-intent-watch"


def test_request_sends_token_never_in_url():
    opener = FakeUrlopen(pages={"/repos/x": {"ok": True}})
    gh._request("/repos/x", TOKEN, urlopen=opener)
    request = opener.requests[0]
    assert TOKEN not in request.full_url
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"


def test_http_403_and_429_map_to_rate_limit():
    for code in (403, 429):
        with pytest.raises(gh.GitHubRateLimit):
            gh._request(
                "/repos/x", TOKEN, urlopen=FakeUrlopen(raiser=lambda r: (_ for _ in ()).throw(_http_error(code)))
            )


def test_other_http_errors_map_to_github_error():
    with pytest.raises(gh.GitHubError) as exc_info:
        gh._request(
            "/repos/x",
            TOKEN,
            urlopen=FakeUrlopen(raiser=lambda r: (_ for _ in ()).throw(_http_error(500, b"boom"))),
        )
    assert not isinstance(exc_info.value, gh.GitHubRateLimit)
    assert "500" in str(exc_info.value)


def test_rate_limit_is_a_github_error_subclass():
    assert issubclass(gh.GitHubRateLimit, gh.GitHubError)


def test_transport_errors_map_to_github_error():
    def raise_urlerror(request):
        raise urllib.error.URLError("no route to host")

    with pytest.raises(gh.GitHubError) as exc_info:
        gh._request("/repos/x", TOKEN, urlopen=FakeUrlopen(raiser=raise_urlerror))
    assert not isinstance(exc_info.value, gh.GitHubRateLimit)


def test_invalid_json_maps_to_github_error():
    opener = FakeUrlopen(pages={"/repos/x": b"<html>not json</html>"})
    with pytest.raises(gh.GitHubError):
        gh._request("/repos/x", TOKEN, urlopen=opener)


# ── endpoints ───────────────────────────────────────────────────────────────


def test_list_open_pulls_query_and_single_page():
    pulls = [make_pull(1), make_pull(2)]
    opener = FakeUrlopen(pages={"/pulls?": pulls})
    assert gh.list_open_pulls(REPO, TOKEN, urlopen=opener) == pulls
    url = opener.requests[0].full_url
    assert f"/repos/{REPO}/pulls" in url
    assert "state=open" in url
    assert "sort=updated" in url
    assert "direction=desc" in url
    assert "per_page=50" in url
    assert "page=1" in url


def test_list_open_pulls_paginates_up_to_max_pages():
    full_page = [make_pull(n) for n in range(50)]
    opener = FakeUrlopen(pages={"/pulls?": full_page})  # every page full
    pulls = gh.list_open_pulls(REPO, TOKEN, urlopen=opener)
    assert len(pulls) == 100  # 2 pages × 50 — capped there
    assert len(opener.requests) == 2
    assert "page=2" in opener.requests[1].full_url


def test_list_open_pulls_stops_on_short_page():
    opener = FakeUrlopen(pages={"/pulls?": [make_pull(1)]})
    pulls = gh.list_open_pulls(REPO, TOKEN, urlopen=opener)
    assert len(pulls) == 1
    assert len(opener.requests) == 1


def test_get_pull_returns_object():
    opener = FakeUrlopen(pages={f"/repos/{REPO}/pulls/7": make_pull(7)})
    assert gh.get_pull(REPO, 7, TOKEN, urlopen=opener)["number"] == 7


def test_list_files_strips_patch_and_keeps_churn():
    payload = [
        {
            "filename": "a.py",
            "status": "modified",
            "additions": 5,
            "deletions": 2,
            "patch": "@@ -1,2 +1,3 @@\n+leaked",
        }
    ]
    opener = FakeUrlopen(pages={"/files": payload})
    files = gh.list_files(REPO, 9, TOKEN, urlopen=opener)
    assert files == [
        {"filename": "a.py", "status": "modified", "additions": 5, "deletions": 2}
    ]
    assert "patch" not in files[0]


def test_list_files_caps_max_files():
    payload = [
        {"filename": f"f{n}.py", "status": "added", "additions": 1, "deletions": 0}
        for n in range(10)
    ]
    opener = FakeUrlopen(pages={"/files": payload})
    assert len(gh.list_files(REPO, 9, TOKEN, max_files=3, urlopen=opener)) == 3


def test_list_commits_takes_subject_lines_only():
    payload = [
        {"commit": {"message": "fix: the thing\n\nLong body explaining.\nMore."}},
        {"commit": {"message": "chore: bump"}},
        {"commit": {}},  # no message → dropped
    ]
    opener = FakeUrlopen(pages={"/commits": payload})
    assert gh.list_commits(REPO, 9, TOKEN, urlopen=opener) == [
        "fix: the thing",
        "chore: bump",
    ]


def test_list_issue_comments_returns_dicts():
    opener = FakeUrlopen(pages={"/comments": [{"body": "hi"}]})
    comments = gh.list_issue_comments(REPO, 9, TOKEN, urlopen=opener)
    assert comments == [{"body": "hi"}]


def test_post_issue_comment_posts_body_json():
    opener = FakeUrlopen(pages={"/comments": {"id": 1}})
    result = gh.post_issue_comment(REPO, 9, TOKEN, "review!", urlopen=opener)
    assert result == {"id": 1}
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert json.loads(request.data.decode("utf-8")) == {"body": "review!"}


# ── marker detection (substring on the comment body) ────────────────────────


def test_has_intent_marker_matches_substring_anywhere():
    comments = [
        {"body": "some earlier comment"},
        {"body": f"preface {MARKER} suffix"},
    ]
    assert gh.has_intent_marker(comments) is True


def test_has_intent_marker_false_without_marker():
    assert gh.has_intent_marker([{"body": "plain review"}]) is False
    assert gh.has_intent_marker([]) is False
    assert gh.has_intent_marker([{"body": ""}, {}]) is False
