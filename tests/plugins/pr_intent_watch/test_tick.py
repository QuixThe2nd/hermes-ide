"""Tick behavior contracts: baseline, review, idempotency, skips, rate limits."""

from __future__ import annotations

import pytest

from plugins.pr_intent_watch.core import load_state, run_tick
from tests.plugins.pr_intent_watch._helpers import (
    MARKER,
    REPO,
    FakeGitHub,
    install_fake_github,
    install_fake_review,
    make_pull,
    make_review,
    seen_entry,
    seed_state,
    write_config,
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """HERMES_HOME for the tick; the root conftest already isolates it, this
    pins it to this suite's tmp_path explicitly."""
    home_dir = tmp_path / "hermes_home"
    home_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_dir))
    return home_dir


def _run(home, fake, monkeypatch, *, section=None, result=...):
    config_path = write_config(home, section)
    install_fake_github(monkeypatch, fake)
    captured = install_fake_review(
        monkeypatch, make_review() if result is ... else result
    )
    summary = run_tick(config_path=config_path)
    return summary, captured


# ── baseline ────────────────────────────────────────────────────────────────


def test_first_tick_baselines_open_prs_and_posts_nothing(home, monkeypatch):
    fake = FakeGitHub([make_pull(1), make_pull(2)])
    summary, _ = _run(home, fake, monkeypatch)

    assert summary["baseline"] is True
    assert summary["commented"] == 0
    assert summary["reviewed"] == 0
    assert fake.posted == []
    # No metadata fetches either — baseline is a pure listing.
    assert [c for c in fake.calls if c[0] != "list_open_pulls"] == []

    state = load_state()
    assert state["baseline_complete"] is True
    assert state["repo"] == REPO
    assert state["seen"] == {
        "1": seen_entry(skipped=True),
        "2": seen_entry(skipped=True),
    }


def test_baseline_dry_run_writes_no_state(home, monkeypatch):
    fake = FakeGitHub([make_pull(3)])
    install_fake_github(monkeypatch, fake)
    install_fake_review(monkeypatch, make_review())
    summary = run_tick(config_path=write_config(home), dry_run=True)

    assert summary["baseline"] is True
    assert not (home / "state" / "pr_intent_watch.json").exists()


# ── new PRs ─────────────────────────────────────────────────────────────────


def test_second_tick_reviews_and_comments_exactly_one_new_pr(home, monkeypatch):
    seed_state(home, seen={"1": seen_entry(skipped=True)})
    fake = FakeGitHub([make_pull(1), make_pull(5)])
    summary, captured = _run(home, fake, monkeypatch)

    assert summary["new"] == 1
    assert summary["reviewed"] == 1
    assert summary["commented"] == 1
    assert [number for number, _ in fake.posted] == [5]

    number, body = fake.posted[0]
    assert number == 5
    assert body.splitlines()[0] == MARKER
    assert "## Intent review" in body
    assert "**Objective:**" in body

    metadata = captured[0]
    assert metadata["number"] == 5
    assert metadata["title"] == "Fix the widget flake"
    assert [f["filename"] for f in metadata["files"]] == ["plugins/example/core.py"]

    state = load_state()
    assert state["seen"]["5"] == seen_entry(commented=True)
    assert state["seen"]["1"]["skipped"] is True  # untouched


def test_second_tick_without_new_prs_is_quiet(home, monkeypatch):
    seed_state(home, seen={"1": seen_entry(commented=True)})
    fake = FakeGitHub([make_pull(1)])
    summary, captured = _run(home, fake, monkeypatch)

    assert summary["reviewed"] == 0
    assert summary["commented"] == 0
    assert summary["new"] == 0
    assert fake.posted == []
    assert captured == []


def test_new_push_to_commented_pr_is_not_rereviewed(home, monkeypatch):
    seed_state(home, seen={"5": seen_entry(commented=True)})
    fake = FakeGitHub([make_pull(5, head_sha="newsha999")])
    summary, captured = _run(home, fake, monkeypatch)

    assert summary["reviewed"] == 0
    assert fake.posted == []
    assert captured == []


def test_failed_review_is_retried_next_tick(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5)])

    summary, _ = _run(home, fake, monkeypatch, result=None)
    assert summary["reviewed"] == 0
    assert summary["commented"] == 0
    assert fake.posted == []
    assert load_state()["seen"]["5"]["commented"] is False  # not marked → retry

    summary, _ = _run(home, fake, monkeypatch)  # review works now
    assert summary["commented"] == 1
    assert [number for number, _ in fake.posted] == [5]
    assert load_state()["seen"]["5"]["commented"] is True


# ── idempotency across state loss ──────────────────────────────────────────


def test_marker_on_github_prevents_repost_when_seen_entry_lost(home, monkeypatch):
    # baseline_complete survived but this PR's entry did not (e.g. cap prune).
    seed_state(home, seen={})
    fake = FakeGitHub(
        [make_pull(5)],
        comments={5: [{"body": f"earlier note\n{MARKER}\nthanks"}]},
    )
    summary, captured = _run(home, fake, monkeypatch)

    assert summary["skipped"] == 1
    assert fake.posted == []
    assert captured == []
    # Recorded as handled, so future ticks do not even fetch comments.
    assert load_state()["seen"]["5"]["commented"] is True


# ── skips ──────────────────────────────────────────────────────────────────


def test_draft_skipped_only_when_configured(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5, draft=True)])

    summary, captured = _run(home, fake, monkeypatch, section={"skip_drafts": True})
    assert summary["skipped"] == 1
    assert fake.posted == [] and captured == []
    assert load_state()["seen"]["5"]["skipped"] is True

    seed_state(home, seen={})  # reset; drafts reviewed when skip_drafts is false
    fake = FakeGitHub([make_pull(5, draft=True)])
    summary, _ = _run(home, fake, monkeypatch, section={"skip_drafts": False})
    assert summary["reviewed"] == 1
    assert [number for number, _ in fake.posted] == [5]


def test_skip_authors_is_case_insensitive(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5, author="Dependabot[bot]")])
    summary, captured = _run(
        home, fake, monkeypatch, section={"skip_authors": ["dependabot[BOT]"]}
    )

    assert summary["skipped"] == 1
    assert fake.posted == [] and captured == []
    assert load_state()["seen"]["5"]["skipped"] is True


# ── no patch ever reaches the model ─────────────────────────────────────────


def test_metadata_sent_to_review_has_no_patch_key(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5)])  # FakeGitHub.list_files returns patchy dicts
    _, captured = _run(home, fake, monkeypatch)

    for file_entry in captured[0]["files"]:
        assert "patch" not in file_entry
        assert set(file_entry) == {"filename", "status", "additions", "deletions"}


def test_body_and_commits_are_bounded(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5, body="x" * 20000)])
    fake.commits[5] = [f"c{i}" for i in range(50)]
    _, captured = _run(home, fake, monkeypatch, section={"max_commits": 3})

    assert len(captured[0]["body"]) <= 8001
    assert captured[0]["commits"] == ["c0", "c1", "c2"]


# ── comment:false and dry-run ───────────────────────────────────────────────


def test_comment_false_reviews_but_never_posts(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5)])
    summary, _ = _run(home, fake, monkeypatch, section={"comment": False})

    assert summary["reviewed"] == 1
    assert summary["commented"] == 0
    assert fake.posted == []
    assert not any(call[0] == "post_issue_comment" for call in fake.calls)

    # The review is the whole job under comment:false — record it handled so
    # later ticks do not burn another LLM call on the same PR.
    state = load_state()
    assert state["seen"]["5"]["skipped"] is True
    assert state["seen"]["5"]["commented"] is False

    summary, captured = _run(home, fake, monkeypatch, section={"comment": False})
    assert summary["reviewed"] == 0
    assert summary["commented"] == 0
    assert captured == []  # no second review of PR 5
    assert fake.posted == []
    assert not any(call[0] == "post_issue_comment" for call in fake.calls)


def test_dry_run_posts_nothing_and_writes_no_state(home, monkeypatch):
    seed_state(home, seen={"1": seen_entry(skipped=True)})
    fake = FakeGitHub([make_pull(1), make_pull(5)])
    install_fake_github(monkeypatch, fake)
    install_fake_review(monkeypatch, make_review())
    summary = run_tick(config_path=write_config(home), dry_run=True)

    assert summary["reviewed"] == 1
    assert summary["commented"] == 0
    assert fake.posted == []
    # State untouched: still only the seeded entry, baseline not advanced.
    assert load_state()["seen"] == {"1": seen_entry(skipped=True)}


# ── rate limiting ──────────────────────────────────────────────────────────


def test_rate_limit_mid_tick_keeps_earlier_posts_and_leaves_rest_unseen(
    home, monkeypatch
):
    seed_state(home, seen={})
    fake = FakeGitHub(
        [make_pull(5), make_pull(6)],
        rate_limit_comments_on={6},  # 6 is processed second (oldest-first)
    )
    summary, _ = _run(home, fake, monkeypatch)

    assert [number for number, _ in fake.posted] == [5]
    state = load_state()
    assert state["seen"]["5"]["commented"] is True
    assert "6" not in state["seen"]  # unprocessed → retried next tick


def test_rate_limit_on_listing_posts_nothing_and_keeps_state(home, monkeypatch):
    seed_state(home, seen={"1": seen_entry(skipped=True)})

    class RateLimited(FakeGitHub):
        def list_open_pulls(self, repo, token, **kwargs):
            from plugins.pr_intent_watch.github import GitHubRateLimit

            raise GitHubRateLimit("listing throttled")

    fake = RateLimited([])
    summary, _ = _run(home, fake, monkeypatch)

    assert summary["commented"] == 0
    assert load_state()["seen"] == {"1": seen_entry(skipped=True)}


# ── gating ─────────────────────────────────────────────────────────────────


def test_disabled_tick_makes_no_github_calls(home, monkeypatch):
    fake = FakeGitHub([make_pull(5)])
    summary, captured = _run(
        home, fake, monkeypatch, section={"enabled": False}
    )

    assert summary["disabled"] is True
    assert fake.calls == []
    assert fake.posted == []
    assert captured == []
    assert not (home / "state").exists()


def test_disabled_via_plugins_list_also_skips(home, monkeypatch):
    fake = FakeGitHub([make_pull(5)])
    config_path = write_config(
        home, {"enabled": True}, extra={"plugins": {"disabled": ["pr_intent_watch"]}}
    )
    install_fake_github(monkeypatch, fake)
    install_fake_review(monkeypatch, make_review())
    summary = run_tick(config_path=config_path)

    assert summary["disabled"] is True
    assert fake.calls == []


def test_no_token_skips_tick_cleanly(home, monkeypatch):
    from plugins.pr_intent_watch import github as gh

    fake = FakeGitHub([make_pull(5)])
    install_fake_github(monkeypatch, fake)
    monkeypatch.setattr(gh, "resolve_token", lambda: None)
    install_fake_review(monkeypatch, make_review())

    summary = run_tick(config_path=write_config(home))

    assert summary["no_token"] is True
    assert summary["commented"] == 0
    assert fake.calls == []  # never even listed
    assert not (home / "state").exists()


def test_repo_change_rebaselines_instead_of_trusting_foreign_seen(home, monkeypatch):
    seed_state(home, repo="someone/else", seen={"5": seen_entry(commented=True)})
    fake = FakeGitHub([make_pull(9)])
    summary, _ = _run(home, fake, monkeypatch)

    assert summary["baseline"] is True
    assert fake.posted == []  # never replays history onto the new repo
    state = load_state()
    assert state["repo"] == REPO
    assert state["seen"] == {"9": seen_entry(skipped=True)}


def test_seen_map_is_capped_at_500_newest(home, monkeypatch):
    stale = {str(n): seen_entry(commented=True) for n in range(600)}
    seed_state(home, seen=stale)
    fake = FakeGitHub([])
    _run(home, fake, monkeypatch)

    state = load_state()
    assert len(state["seen"]) == 500
    assert "599" in state["seen"]
    assert "99" not in state["seen"]


def test_corrupt_state_file_is_treated_as_first_run(home, monkeypatch):
    (home / "state").mkdir()
    (home / "state" / "pr_intent_watch.json").write_text("{broken", encoding="utf-8")
    fake = FakeGitHub([make_pull(4)])
    summary, _ = _run(home, fake, monkeypatch)

    assert summary["baseline"] is True
    assert fake.posted == []
    assert load_state()["baseline_complete"] is True
