"""The code-delta note for a serving-identity transition.

A process handoff whose predecessor ran a DIFFERENT build must tell the
conversation what changed between the two builds — the commit subjects
(``git log --oneline``) and the diff stat — beside the process transition
note. The delta is never fabricated: an unrecorded/unreadable predecessor
build or a failing git renders a short "could not be computed" note instead,
and persistence or git trouble never breaks a turn.

Decision contract (shared with ``internal/servingidentity/codedelta.go``,
pinned by the shared honesty table):

- no parseable predecessor record → ``"none"`` (a first turn rides the
  delta-free path exactly as today; a corrupt record proves no
  predecessor, so it is delta-free too);
- this process's own build sha is unusable → ``"none"`` — silence, not a
  note: a build whose identity cannot be read can never name the delta,
  and pip installs (no git) would run this branch on EVERY turn, choking
  on a could-not-compute repeat nobody can settle;
- the record's ``code_sha`` is absent or blank (a pre-feature record) →
  ``"unprovable"`` — the could-not-compute note saying it was not recorded;
- the record's ``code_sha`` is present but unusable → ``"unprovable"`` —
  could not be read;
- the record's ``code_sha`` equals this process's → ``"none"`` (same build,
  or the delta channel is already settled for this build);
- otherwise → ``"delta"``.

The decision keys on the record's sha proof rather than on
``transition_state`` because the stage step rewrites pid/start to THIS
process while the sha proof stays at its previous value: a turn that
crashes before its api_content stamp must re-emit the delta next turn
(the same ack-gated proof discipline as the note proof).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# The hard cap for the whole assembled note. Head+tail truncation with an
# explicit marker, so the leading framing and the trailing terminal pointer
# both survive an enormous diff.
MAX_NOTE_CHARS = 4000

TRUNCATION_MARKER = "[… truncated — the full diff is available via terminal …]"

REASON_PREVIOUS_NOT_RECORDED = "the previous build was not recorded"
REASON_PREVIOUS_UNREADABLE = "the previous build's recorded identity could not be read"
REASON_GIT_FAILED = "git could not read this repository"

# DeltaRender: (old_sha, new_sha) -> (commits, stat) | None (git failed).
DeltaRender = Callable[[str, str], Optional[Tuple[str, str]]]

# Memoized git reads per (old, new) build pair — successes AND failures, so
# a failing git is never retried on the hot path. The delta between two
# builds cannot change within one process lifetime.
_DELTA_CACHE: dict[Tuple[str, str], Optional[Tuple[str, str]]] = {}


def usable_sha(sha: Any) -> bool:
    """A provable build identity: exactly 40 lowercase hex characters."""
    return (
        isinstance(sha, str)
        and len(sha) == 40
        and all(char in "0123456789abcdef" for char in sha)
    )


def code_delta_decision(previous: Any, current_sha: Any) -> str:
    """``"none"``, ``"unprovable"``, or ``"delta"`` for this turn.

    ``previous`` is the session's parsed served-by record (``None`` when
    absent/corrupt). Pure: no git, no store.
    """
    if not isinstance(previous, Mapping):
        return "none"
    if not usable_sha(current_sha):
        return "none"
    old_sha = previous.get("code_sha")
    if old_sha is None or old_sha == "":
        return "unprovable"
    if not usable_sha(old_sha):
        return "unprovable"
    if old_sha == current_sha:
        return "none"
    return "delta"


def _short(sha: str) -> str:
    return sha[:8] if isinstance(sha, str) else ""


def render_code_delta_note(old_sha: str, new_sha: str, commits: str, stat: str) -> str:
    """The delta framing: both builds, the commits, the stat, the terminal pointer."""
    return (
        f"[System note: The Hermes gateway build changed since the previous turns of "
        f"this conversation: they ran build {_short(old_sha)}, this process runs build "
        f"{_short(new_sha)}. Commits between the two builds:\n{commits}\n"
        f"Changed files:\n{stat}\n"
        f"The full diff is available via terminal (git diff {old_sha}..{new_sha}).]"
    )


def unavailable_note(old_sha: str, new_sha: str, reason: str) -> str:
    """A could-not-compute card — truthful about WHY, never a fabricated diff.

    A git failure still names both builds and points at the terminal command;
    an unprovable predecessor names only this build (there is nothing to
    diff against).
    """
    if reason == REASON_GIT_FAILED:
        return (
            f"[System note: The Hermes gateway build changed since the previous turns of "
            f"this conversation: they ran build {_short(old_sha)}, this process runs build "
            f"{_short(new_sha)}. The code delta between the two builds could not be "
            f"computed: {reason}. The full diff is available via terminal "
            f"(git diff {old_sha}..{new_sha}).]"
        )
    return (
        f"[System note: The code delta between the build that served the previous turns "
        f"and this process's build ({_short(new_sha)}) could not be computed: {reason}.]"
    )


def cap_head_tail(text: str, cap: int) -> str:
    """Truncate ``text`` to at most ``cap`` characters, keeping head and tail.

    The budget is characters (code points), matching the Go twin's
    rune-sliced ``CapHeadTail`` byte for byte. Head keeps 2/3 of the room,
    tail 1/3 — the leading framing and the trailing terminal pointer both
    survive.
    """
    text = text if isinstance(text, str) else ""
    if len(text) <= cap:
        return text
    keep = max(cap - len(TRUNCATION_MARKER), 0)
    head = keep * 2 // 3
    tail = keep - head
    return text[:head] + TRUNCATION_MARKER + text[len(text) - tail :]


def _git_out(args: list) -> Optional[str]:
    """One git command in the executing repo; ``None`` on any failure."""
    try:
        from hermes_cli.version_info import _resolve_repo_dir, _run_git

        repo = _resolve_repo_dir()
        if not repo:
            return None
        return _run_git(repo, *args)
    except Exception:
        return None


def _git_render(old_sha: str, new_sha: str) -> Optional[Tuple[str, str]]:
    """The memoized git provider: commits + stat for a build pair, once per process."""
    key = (old_sha, new_sha)
    if key in _DELTA_CACHE:
        return _DELTA_CACHE[key]
    commits = _git_out(["log", "--oneline", f"{old_sha}..{new_sha}"])
    stat = _git_out(["diff", "--stat", f"{old_sha}..{new_sha}"])
    rendered = (commits, stat) if commits and stat else None
    _DELTA_CACHE[key] = rendered
    return rendered


def code_delta_note(
    previous: Any, current_sha: Any, render: Optional[DeltaRender] = None
) -> Optional[str]:
    """The code-delta note for this turn, or ``None`` when nothing rides.

    ``render`` is injectable for tests and the shared table; the default is
    the memoized git provider. Never raises — an unexpected failure renders
    nothing rather than breaking the turn.
    """
    try:
        decision = code_delta_decision(previous, current_sha)
        if decision == "none":
            return None
        old_sha = ""
        if isinstance(previous, Mapping):
            value = previous.get("code_sha")
            if isinstance(value, str):
                old_sha = value
        if decision == "unprovable":
            reason = (
                REASON_PREVIOUS_NOT_RECORDED
                if old_sha == ""
                else REASON_PREVIOUS_UNREADABLE
            )
            return unavailable_note(old_sha, current_sha, reason)
        if render is None:
            render = _git_render
        rendered = render(old_sha, current_sha)
        if not rendered:
            return unavailable_note(old_sha, current_sha, REASON_GIT_FAILED)
        commits, stat = rendered
        return cap_head_tail(
            render_code_delta_note(old_sha, current_sha, commits, stat),
            MAX_NOTE_CHARS,
        )
    except Exception:
        logger.debug("code delta: note render failed", exc_info=True)
        return None
