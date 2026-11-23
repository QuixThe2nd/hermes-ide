"""The code-delta note for a serving-identity transition.

A process handoff whose predecessor ran a DIFFERENT build must tell the
conversation what changed between the two builds — the commit subjects and
the diff stat — on its own injection card. The delta is never fabricated:
an unrecorded/unreadable predecessor build or a failing git renders a
short "could not be computed" note instead, and persistence/git trouble
never breaks a turn.

Decision contract (shared with ``internal/servingidentity/codedelta.go``):

- no parseable predecessor record → ``"none"`` (a first turn rides the
  delta-free path exactly as today; a corrupt record proves no
  predecessor, so it is delta-free too);
- this process's own build sha is unusable → ``"none"`` — silence, not a
  note: a build whose identity cannot be read can never name the delta,
  and pip installs (no git) must stay quiet on EVERY turn, not choke on a
  could-not-compute repeat nobody can settle;
- the record's ``code_sha`` is absent or blank (a pre-feature record) →
  ``"unprovable"`` — the could-not-compute note saying it was not
  recorded;
- the record's ``code_sha`` is present but unusable → ``"unprovable"`` —
  could not be read;
- the record's ``code_sha`` equals this process's → ``"none"`` (same
  build, or the delta channel is already settled for this build);
- otherwise → ``"delta"``.

The decision keys on the sha proof rather than on ``transition_state``
because the stage step rewrites pid/start to THIS process while the sha
proof stays at its previous value: a turn that crashes before its stamp
must re-emit the delta on the next turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.serving_codedelta import (
    MAX_NOTE_CHARS,
    REASON_GIT_FAILED,
    REASON_PREVIOUS_NOT_RECORDED,
    REASON_PREVIOUS_UNREADABLE,
    cap_head_tail,
    code_delta_decision,
    code_delta_note,
    render_code_delta_note,
    unavailable_note,
)

_REPO = Path(__file__).resolve().parents[2]
_OLD_SHA = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
_NEW_SHA = "e5f60718e5f60718e5f60718e5f60718e5f60718"


def _previous(**record):
    """A parseable predecessor record; ``code_sha`` absent unless given."""
    base = {"pid": 709505, "start_time": 3001234}
    base.update(record)
    return base


# ── decision matrix ────────────────────────────────────────────────────────


def test_no_previous_record_is_delta_free():
    assert code_delta_decision(None, _NEW_SHA) == "none"
    assert code_delta_decision("not-json{", _NEW_SHA) == "none"
    assert code_delta_decision(42, _NEW_SHA) == "none"


def test_same_sha_is_none_and_differing_sha_is_delta():
    assert code_delta_decision(_previous(code_sha=_OLD_SHA), _OLD_SHA) == "none"
    assert code_delta_decision(_previous(code_sha=_OLD_SHA), _NEW_SHA) == "delta"


def test_absent_or_blank_old_sha_is_unprovable():
    # A pre-feature record carries no code_sha field at all — the previous
    # build was never recorded, so the delta can only be "could not compute".
    assert code_delta_decision(_previous(), _NEW_SHA) == "unprovable"
    assert code_delta_decision(_previous(code_sha=None), _NEW_SHA) == "unprovable"
    assert code_delta_decision(_previous(code_sha=""), _NEW_SHA) == "unprovable"


def test_garbage_old_sha_is_unprovable():
    for bad in ("abc", "nothex!", "z" * 7, "a" * 65, 12345):
        assert code_delta_decision(
            _previous(code_sha=bad), _NEW_SHA) == "unprovable", bad


def test_unusable_current_sha_is_delta_free_silence():
    """This build's own identity unreadable → quiet, never a note.

    The delta can never be computed OR named against an unknown build, and
    pip installs (no git) run this branch on every turn: a could-not-compute
    card there would repeat forever, because the ack has no usable sha to
    settle the channel with. Silence is the fail-safe.
    """
    assert code_delta_decision(_previous(code_sha=_OLD_SHA), None) == "none"
    assert code_delta_decision(_previous(code_sha=_OLD_SHA), "short") == "none"
    # Even a legacy record stays quiet — no fabricated "not recorded" noise.
    assert code_delta_decision(_previous(), None) == "none"


# ── note framing (byte-parity twin of internal/servingidentity) ───────────


def test_delta_note_names_both_builds_commits_stat_and_terminal_pointer():
    note = code_delta_note(
        _previous(code_sha=_OLD_SHA), _NEW_SHA,
        render=lambda old, new: (
            "abc123 fix gateway\ndef456 fix tests", " 2 files changed, 3 insertions(+)"),
    )
    assert note.startswith("[System note: The Hermes gateway build changed")
    assert _OLD_SHA[:8] in note and _NEW_SHA[:8] in note
    assert "abc123 fix gateway" in note and "def456 fix tests" in note
    assert "2 files changed" in note
    # The one-line pointer to the full diff via terminal.
    assert f"git diff {_OLD_SHA}..{_NEW_SHA}" in note


def test_render_code_delta_note_is_the_framing_twin():
    note = render_code_delta_note(
        _OLD_SHA, _NEW_SHA, "abc123 fix gateway", " 2 files changed")
    assert note.startswith("[System note: The Hermes gateway build changed")
    assert f"git diff {_OLD_SHA}..{_NEW_SHA}" in note


def test_failed_git_render_is_could_not_compute_never_fabricated():
    note = code_delta_note(
        _previous(code_sha=_OLD_SHA), _NEW_SHA, render=lambda old, new: None)
    assert "could not be computed" in note
    # No fabricated commit or diff lines.
    assert "Commits between the two builds" not in note
    assert f"git diff {_OLD_SHA}..{_NEW_SHA}" in note


def test_unavailable_notes_are_short_and_truthful():
    not_recorded = unavailable_note(
        "", _NEW_SHA, REASON_PREVIOUS_NOT_RECORDED)
    assert "could not be computed" in not_recorded
    assert "was not recorded" in not_recorded
    assert _NEW_SHA[:8] in not_recorded
    unreadable = unavailable_note(
        _OLD_SHA, _NEW_SHA, REASON_PREVIOUS_UNREADABLE)
    assert "could not be computed" in unreadable
    assert "could not be read" in unreadable
    git_failed = unavailable_note(_OLD_SHA, _NEW_SHA, REASON_GIT_FAILED)
    assert "could not be computed" in git_failed
    assert _OLD_SHA[:8] in git_failed and _NEW_SHA[:8] in git_failed
    assert f"git diff {_OLD_SHA}..{_NEW_SHA}" in git_failed


def test_unprovable_decision_renders_the_matching_unavailable_note():
    note = code_delta_note(_previous(), _NEW_SHA, render=lambda old, new: None)
    assert "could not be computed" in note
    assert "was not recorded" in note
    corrupt = code_delta_note(
        _previous(code_sha="garbage"), _NEW_SHA, render=lambda old, new: None)
    assert "could not be computed" in corrupt
    assert "could not be read" in corrupt


# ── hard cap: head + tail with an explicit truncation marker ──────────────


def test_cap_keeps_short_text_byte_identical():
    assert cap_head_tail("short note", 4000) == "short note"
    assert cap_head_tail("x" * MAX_NOTE_CHARS, MAX_NOTE_CHARS) == "x" * MAX_NOTE_CHARS


def test_cap_truncates_head_and_tail_with_marker():
    body = "HEAD" + ("a" * 3000) + ("b" * 3000) + "TAIL"
    capped = cap_head_tail(body, MAX_NOTE_CHARS)
    assert len(capped) <= MAX_NOTE_CHARS
    assert capped.startswith("HEAD")
    assert capped.endswith("TAIL")
    assert "truncated" in capped and "terminal" in capped
    # Both ends survive: content from before AND after the elided middle.
    assert "aaa" in capped and "bbb" in capped


def test_capped_delta_note_stays_within_the_hard_cap():
    commits = "\n".join(f"commit{i} subject line" for i in range(400))
    stat = "\n".join(f" src/file{i}.py | 10 ++++++ " for i in range(400))
    note = code_delta_note(
        _previous(code_sha=_OLD_SHA), _NEW_SHA,
        render=lambda old, new: (commits, stat),
    )
    assert len(note) <= MAX_NOTE_CHARS
    assert "truncated" in note
    assert f"git diff {_OLD_SHA}..{_NEW_SHA}" in note


# ── the git provider: at most once per build pair, never raises ───────────


def test_git_render_is_memoized_per_build_pair():
    import gateway.serving_codedelta as cd

    calls = []

    def _fake_git_out(args):
        calls.append(args)
        return "abc123 subject\n" if args[0] == "log" else " 1 file changed\n"

    cd._DELTA_CACHE.clear()
    with patch.object(cd, "_git_out", _fake_git_out):
        first = cd._git_render(_OLD_SHA, _NEW_SHA)
        second = cd._git_render(_OLD_SHA, _NEW_SHA)
    assert first == second
    assert len(calls) == 2  # log + diff once, then the memo answers


def test_git_failure_degrades_to_could_not_compute():
    import gateway.serving_codedelta as cd

    cd._DELTA_CACHE.clear()
    with patch.object(cd, "_git_out", lambda args: None):
        assert cd._git_render(_OLD_SHA, _NEW_SHA) is None
        note = code_delta_note(_previous(code_sha=_OLD_SHA), _NEW_SHA)
    assert "could not be computed" in note


def test_unusable_previous_sha_is_rejected_before_git():
    """A junk sha from a corrupt record never reaches a git command line."""
    import gateway.serving_codedelta as cd

    cd._DELTA_CACHE.clear()
    with patch.object(cd, "_git_out", lambda args: pytest.fail("git must not run")):
        note = code_delta_note(_previous(code_sha="not a sha at all !"), _NEW_SHA)
    assert "could not be computed" in note


# ── shared honesty table: the runtime bytes ARE the table's bytes ─────────


def test_runtime_delta_note_bytes_match_the_shared_table():
    """The framing the runtime renders is the shared table's expectation —
    the same bytes internal/servingidentity renders (see
    test_serving_identity_contract.py for the executed twin)."""
    table = json.loads(
        (_REPO / "internal/servingidentity/testdata/serving_cases.json")
        .read_text(encoding="utf-8")
    )
    cases = {case["name"]: case for case in table["cases"]}
    case = cases["code_delta_note_renders_commits_stat_and_pointer"]
    request = case["input"]
    stored = request.get("stored")
    note = code_delta_note(
        json.loads(stored) if isinstance(stored, str) else None, request["code_sha"],
        render=lambda old, new: (request["commits"], request["stat"]),
    )
    assert note == case["expect"]
