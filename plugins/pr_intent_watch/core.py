"""Core logic — config normalization, seen-state, tick orchestration."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from plugins.pr_intent_watch import github
from plugins.pr_intent_watch import review as review_module

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX (no fcntl at all)
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_REPO = "QuixThe2nd/hermes-ide"
DEFAULT_POLL_SECONDS = 300
MIN_POLL_SECONDS = 60
DEFAULT_SKIP_DRAFTS = False
DEFAULT_COMMENT = True
DEFAULT_MAX_FILE_NAMES = 40
DEFAULT_MAX_COMMITS = 20
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8645
DEFAULT_WEBHOOK_PATH = "/webhooks/pr-intent-watch"
MIN_LISTEN_PORT = 1
MAX_LISTEN_PORT = 65535

STATE_FILENAME = Path("state") / "pr_intent_watch.json"
PER_PAGE = 50
MAX_LIST_PAGES = 2
MAX_SEEN_ENTRIES = 500
# PR bodies can be enormous; the model only needs the write-up, not the essay.
MAX_BODY_CHARS = 8000


class PrIntentWatchError(Exception):
    """Raised instead of sys.exit from the CLI entrypoint."""


@dataclass(frozen=True)
class WatchConfig:
    enabled: bool = True
    repo: str = DEFAULT_REPO
    poll_seconds: int = DEFAULT_POLL_SECONDS
    skip_drafts: bool = DEFAULT_SKIP_DRAFTS
    skip_authors: tuple[str, ...] = ()
    comment: bool = DEFAULT_COMMENT
    max_file_names: int = DEFAULT_MAX_FILE_NAMES
    max_commits: int = DEFAULT_MAX_COMMITS
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    webhook_path: str = DEFAULT_WEBHOOK_PATH


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def state_path() -> Path:
    return _hermes_home() / STATE_FILENAME


# ── Config ──────────────────────────────────────────────────────────────────


def load_config_section(config_path: Path | None = None) -> dict:
    """Raw config mapping — the live one, or a YAML file for the CLI/tests."""
    if config_path is None:
        try:
            from hermes_cli.config import load_config_readonly

            raw = load_config_readonly()
        except Exception as exc:
            raise PrIntentWatchError(f"cannot load config: {exc}") from exc
    else:
        import yaml

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise PrIntentWatchError(f"cannot read {config_path}: {exc}") from exc
        except Exception as exc:
            raise PrIntentWatchError(f"cannot parse {config_path}: {exc}") from exc
    return raw if isinstance(raw, dict) else {}


def plugin_disabled_in_raw(raw: Mapping[str, Any] | None) -> bool:
    """True when config.yaml disables this plugin by name or section flag.

    The timer lifecycle and the tick both consult this, so disabling the
    plugin retires the timer AND stops a manually invoked ``run.py`` — one
    way to say off, not two.
    """
    if not isinstance(raw, Mapping):
        return False
    plugins = raw.get("plugins")
    if isinstance(plugins, Mapping):
        disabled = plugins.get("disabled")
        if isinstance(disabled, list) and "pr_intent_watch" in disabled:
            return True
        nested = plugins.get("pr_intent_watch")
        if isinstance(nested, Mapping) and nested.get("enabled") is False:
            return True
    section = raw.get("pr_intent_watch")
    return isinstance(section, Mapping) and section.get("enabled") is False


def watch_config_from_raw(raw: Mapping[str, Any] | None) -> WatchConfig:
    """Normalize the ``pr_intent_watch`` section; invalid types → defaults."""
    section = raw.get("pr_intent_watch") if isinstance(raw, Mapping) else None
    if not isinstance(section, Mapping):
        section = {}

    def _bool(key: str, default: bool) -> bool:
        value = section.get(key, default)
        return value if isinstance(value, bool) else default

    def _int(key: str, default: int) -> int:
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value

    def _str(key: str, default: str) -> str:
        value = section.get(key, default)
        return value if isinstance(value, str) and value.strip() else default

    skip_authors_raw = section.get("skip_authors", [])
    if isinstance(skip_authors_raw, list):
        skip_authors = tuple(
            str(name).strip().lower()
            for name in skip_authors_raw
            if str(name).strip()
        )
    else:
        skip_authors = ()

    poll_seconds = max(MIN_POLL_SECONDS, _int("poll_seconds", DEFAULT_POLL_SECONDS))
    repo = _str("repo", DEFAULT_REPO)
    if "/" not in repo:
        repo = DEFAULT_REPO
    listen_port = min(
        MAX_LISTEN_PORT, max(MIN_LISTEN_PORT, _int("listen_port", DEFAULT_LISTEN_PORT))
    )
    webhook_path = _str("webhook_path", DEFAULT_WEBHOOK_PATH)
    if not webhook_path.startswith("/"):
        # A path GitHub can never POST to is a config typo, not an address.
        webhook_path = DEFAULT_WEBHOOK_PATH

    return WatchConfig(
        enabled=_bool("enabled", True) and not plugin_disabled_in_raw(raw),
        repo=repo,
        poll_seconds=poll_seconds,
        skip_drafts=_bool("skip_drafts", DEFAULT_SKIP_DRAFTS),
        skip_authors=skip_authors,
        comment=_bool("comment", DEFAULT_COMMENT),
        max_file_names=max(1, _int("max_file_names", DEFAULT_MAX_FILE_NAMES)),
        max_commits=max(1, _int("max_commits", DEFAULT_MAX_COMMITS)),
        listen_host=_str("listen_host", DEFAULT_LISTEN_HOST),
        listen_port=listen_port,
        webhook_path=webhook_path,
    )


def load_watch_config(config_path: Path | None = None) -> WatchConfig:
    return watch_config_from_raw(load_config_section(config_path))


# ── State ───────────────────────────────────────────────────────────────────


def load_state() -> dict:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(state: dict) -> None:
    """Atomic state write (temp + rename); mkstemp keeps the mode at 0600.

    Guarded by a short exclusive lock so the webhook worker and a concurrent
    poll tick (separate processes or threads) cannot clobber each other's
    JSON. The lock is an flock on the state *directory* fd — a sibling lock
    file would need cleaning up, and flocking the JSON itself is worthless
    when every save replaces its inode.
    """
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_WRITE_LOCK, _lock_state_dir(path.parent):
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".pr-intent-watch.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


#: Serializes saves within one process (the lock file would not: two threads
#: hold two different dir fds, and flock availability is best-effort anyway).
_STATE_WRITE_LOCK = threading.Lock()


@contextlib.contextmanager
def _lock_state_dir(directory: Path):
    if fcntl is None:
        yield
        return
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(dir_fd, fcntl.LOCK_EX)
        except OSError:
            pass  # best-effort: the atomic rename still holds below us
        yield
    finally:
        os.close(dir_fd)  # closing releases the flock


def prune_seen(seen: Mapping[str, Any], cap: int = MAX_SEEN_ENTRIES) -> dict:
    """Keep the ``cap`` newest PR numbers when the map outgrows it."""
    if len(seen) <= cap:
        return dict(seen)

    def _number(key: str) -> int:
        try:
            return int(key)
        except (TypeError, ValueError):
            return -1

    kept = sorted(seen.items(), key=lambda item: _number(item[0]), reverse=True)
    return dict(kept[:cap])


# ── Pull-request helpers ────────────────────────────────────────────────────


def _pull_number(pull: Mapping[str, Any]) -> int | None:
    try:
        return int(pull.get("number"))
    except (TypeError, ValueError):
        return None


def _head_sha(pull: Mapping[str, Any]) -> str:
    head = pull.get("head")
    return str(head.get("sha") or "") if isinstance(head, dict) else ""


def _author(pull: Mapping[str, Any]) -> str:
    user = pull.get("user")
    return str(user.get("login") or "").strip() if isinstance(user, dict) else ""


def _ref(pull: Mapping[str, Any], side: str) -> str:
    ref = pull.get(side)
    return str(ref.get("ref") or "") if isinstance(ref, dict) else ""


def _labels(pull: Mapping[str, Any]) -> list[str]:
    labels = pull.get("labels")
    if not isinstance(labels, list):
        return []
    names = [
        str(item.get("name") or "") if isinstance(item, dict) else str(item)
        for item in labels
    ]
    return [name for name in names if name]


def _seen_entry(head_sha: str, *, commented: bool, skipped: bool) -> dict:
    return {"head_sha": head_sha or "", "commented": commented, "skipped": skipped}


def build_review_metadata(
    pull: Mapping[str, Any],
    files: list[dict],
    commits: list[str],
    *,
    max_file_names: int,
    max_commits: int,
) -> dict:
    """The metadata the model sees — names and churn only, never a patch."""
    body = str(pull.get("body") or "")
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "…"
    return {
        "number": int(pull.get("number") or 0),
        "title": str(pull.get("title") or ""),
        "body": body,
        "author": _author(pull),
        "draft": bool(pull.get("draft")),
        "labels": _labels(pull),
        "base": _ref(pull, "base"),
        "head": _ref(pull, "head"),
        "head_sha": _head_sha(pull),
        "url": str(pull.get("html_url") or ""),
        # Whitelist the churn fields — even if a caller hands us file dicts
        # straight from GitHub, no ``patch`` can ride along into the prompt.
        "files": [
            {
                "filename": str(item.get("filename") or ""),
                "status": str(item.get("status") or ""),
                "additions": int(item.get("additions") or 0),
                "deletions": int(item.get("deletions") or 0),
            }
            for item in files[:max_file_names]
        ],
        "commits": list(commits[:max_commits]),
    }


# ── One PR ──────────────────────────────────────────────────────────────────


def review_one_pr(number: int, *, config: WatchConfig, token: str, state: dict) -> dict:
    """Review a single PR — the one path the poll loop and the webhook share.

    Applies the skip rules (seen-map, draft, author, existing marker), fetches
    metadata only, asks for the intent review, and posts the marker comment.
    Mutates ``state['seen']`` in place; the CALLER persists the state, so a
    rate-limited or failed call can be left unwritten on purpose.

    Returns a per-PR summary: ``action`` (what happened), the counters
    ``run_tick`` aggregates, ``rate_limited`` (stop the poll loop early), and
    ``dirty`` (the seen-map changed and the caller should save).
    """
    summary: dict[str, Any] = {
        "action": "",
        "reviewed": 0,
        "commented": 0,
        "skipped": 0,
        "new": 0,
        "errors": 0,
        "rate_limited": False,
        "dirty": False,
    }
    key = str(number)
    seen = state.get("seen")
    if not isinstance(seen, dict):
        seen = {}
        state["seen"] = seen

    entry = seen.get(key)
    if isinstance(entry, dict) and (entry.get("commented") or entry.get("skipped")):
        summary["action"] = "already_seen"
        return summary

    def _mark(head_sha: str, *, commented: bool, skipped: bool) -> None:
        seen[key] = _seen_entry(head_sha, commented=commented, skipped=skipped)
        summary["dirty"] = True

    try:
        pull = github.get_pull(config.repo, number, token)
    except github.GitHubRateLimit as exc:
        logger.warning("pr_intent_watch: rate limited before PR %s: %s", number, exc)
        summary["action"] = "rate_limited"
        summary["rate_limited"] = True
        return summary
    except github.GitHubError as exc:
        logger.warning("pr_intent_watch: pull fetch failed on PR %s: %s", number, exc)
        summary["action"] = "fetch_failed"
        summary["errors"] = 1
        return summary

    head_sha = _head_sha(pull)
    author = _author(pull).lower()

    if bool(pull.get("draft")) and config.skip_drafts:
        _mark(head_sha, commented=False, skipped=True)
        summary["action"] = "skipped_draft"
        summary["skipped"] = 1
        summary["new"] = 1
        return summary
    if author and author in config.skip_authors:
        _mark(head_sha, commented=False, skipped=True)
        summary["action"] = "skipped_author"
        summary["skipped"] = 1
        summary["new"] = 1
        return summary

    try:
        comments = github.list_issue_comments(config.repo, number, token)
    except github.GitHubRateLimit as exc:
        logger.warning("pr_intent_watch: rate limited before PR %s: %s", number, exc)
        summary["action"] = "rate_limited"
        summary["rate_limited"] = True
        return summary
    except github.GitHubError as exc:
        logger.warning("pr_intent_watch: comments fetch failed on PR %s: %s", number, exc)
        summary["action"] = "fetch_failed"
        summary["errors"] = 1
        return summary

    if github.has_intent_marker(comments):
        # Already reviewed on GitHub (state lost) — idempotent, never
        # double-post. Recording it as commented keeps future ticks cheap.
        _mark(head_sha, commented=True, skipped=False)
        summary["action"] = "already_commented"
        summary["skipped"] = 1
        summary["new"] = 1
        return summary

    try:
        files = github.list_files(
            config.repo, number, token, max_files=config.max_file_names
        )
        commits = github.list_commits(
            config.repo, number, token, max_commits=config.max_commits
        )
    except github.GitHubRateLimit as exc:
        logger.warning("pr_intent_watch: rate limited before PR %s: %s", number, exc)
        summary["action"] = "rate_limited"
        summary["rate_limited"] = True
        return summary
    except github.GitHubError as exc:
        logger.warning("pr_intent_watch: metadata fetch failed on PR %s: %s", number, exc)
        summary["action"] = "fetch_failed"
        summary["errors"] = 1
        return summary

    metadata = build_review_metadata(
        pull,
        files,
        commits,
        max_file_names=config.max_file_names,
        max_commits=config.max_commits,
    )
    result = review_module.review_intent(metadata)
    if result is None:
        # No usable review — leave unmarked so the next attempt retries.
        _mark(head_sha, commented=False, skipped=False)
        summary["action"] = "review_failed"
        summary["errors"] = 1
        return summary

    summary["reviewed"] = 1
    if config.comment:
        try:
            github.post_issue_comment(
                config.repo, number, token, review_module.format_comment(result)
            )
        except github.GitHubRateLimit as exc:
            logger.warning("pr_intent_watch: rate limited on PR %s: %s", number, exc)
            summary["action"] = "rate_limited"
            summary["rate_limited"] = True
            return summary
        except github.GitHubError as exc:
            logger.warning("pr_intent_watch: comment post failed on PR %s: %s", number, exc)
            # Left unseen so the next attempt retries the POST.
            summary["action"] = "post_failed"
            summary["errors"] = 1
            return summary
        _mark(head_sha, commented=True, skipped=False)
        summary["action"] = "commented"
        summary["commented"] = 1
    else:
        # comment:false: the review is the whole job. Record it handled so
        # the next pass does not burn another LLM call on the same PR.
        _mark(head_sha, commented=False, skipped=True)
        summary["action"] = "reviewed"
    summary["new"] = 1
    return summary


# ── Tick ────────────────────────────────────────────────────────────────────


def run_tick(*, config_path: Path | None = None, dry_run: bool = False) -> dict:
    """One watch pass. Never raises for expected failure modes.

    ``dry_run`` implies ``comment=false`` AND performs no state writes, so a
    dry run can never advance the baseline or mark a PR handled.
    """
    summary: dict[str, Any] = {
        "disabled": False,
        "no_token": False,
        "baseline": False,
        "new": 0,
        "reviewed": 0,
        "commented": 0,
        "skipped": 0,
        "errors": 0,
    }

    try:
        raw = load_config_section(config_path)
    except PrIntentWatchError as exc:
        # Default-on must not fail odd hosts; unreadable config = defaults.
        logger.warning("pr_intent_watch could not read config (%s); using defaults", exc)
        raw = {}
    config = watch_config_from_raw(raw)

    if not config.enabled:
        logger.debug("pr_intent_watch disabled; skipping tick")
        summary["disabled"] = True
        return summary

    token = github.resolve_token()
    if not token:
        logger.warning(
            "pr_intent_watch: no GitHub token (GH_TOKEN, GITHUB_TOKEN, or "
            "`gh auth token`); skipping tick"
        )
        summary["no_token"] = True
        return summary

    state = load_state()
    seen = state.get("seen") if isinstance(state.get("seen"), dict) else {}
    prior_repo = state.get("repo")
    baseline_complete = bool(state.get("baseline_complete")) and prior_repo in (
        None,
        "",
        config.repo,
    )
    if prior_repo not in (None, "", config.repo):
        # Watching a different repo now: its PR numbers mean nothing here, so
        # re-baseline (records current open PRs, posts nothing) instead of
        # trusting a seen-map from another repository.
        seen = {}
        baseline_complete = False

    try:
        pulls = github.list_open_pulls(
            config.repo, token, per_page=PER_PAGE, max_pages=MAX_LIST_PAGES
        )
    except github.GitHubRateLimit as exc:
        logger.warning("pr_intent_watch: rate limited listing pulls: %s", exc)
        return summary
    except github.GitHubError as exc:
        logger.warning("pr_intent_watch: could not list open pulls: %s", exc)
        return summary

    if not baseline_complete:
        # First run: record every currently open PR and comment NOTHING —
        # enabling the watch must never replay history.
        for pull in pulls:
            number = _pull_number(pull)
            if number is None:
                continue
            seen[str(number)] = _seen_entry(
                _head_sha(pull), commented=False, skipped=True
            )
        summary["baseline"] = True
        summary["new"] = len(seen)
        logger.info(
            "pr_intent_watch baseline: recorded %d open PR(s), posted no comments",
            len(seen),
        )
        if not dry_run:
            state["repo"] = config.repo
            state["seen"] = prune_seen(seen)
            state["baseline_complete"] = True
            save_state(state)
        return summary

    candidates = []
    for pull in pulls:
        number = _pull_number(pull)
        if number is None:
            continue
        entry = seen.get(str(number))
        if isinstance(entry, dict) and (
            entry.get("commented") or entry.get("skipped")
        ):
            continue
        candidates.append(pull)
    # Oldest first: a rate limit mid-tick still comments the earlier PRs.
    candidates.sort(key=lambda pull: int(pull.get("number") or 0))

    # A dry run reviews but never posts: comment=false is the whole toggle.
    effective = config if not dry_run else replace(config, comment=False)
    for pull in candidates:
        number = _pull_number(pull)
        if number is None:
            continue
        result = review_one_pr(number, config=effective, token=token, state=state)
        for field in ("reviewed", "commented", "skipped", "new", "errors"):
            summary[field] += int(result.get(field, 0))
        if result.get("rate_limited"):
            break

    if not dry_run:
        state["repo"] = config.repo
        state["seen"] = prune_seen(state.get("seen") or {})
        state["baseline_complete"] = True
        save_state(state)
    return summary
