"""The Python git provider behind the code-delta serving note.

A process handoff whose predecessor ran a DIFFERENT build must tell the
conversation what changed between the two builds, but the delta is never
fabricated: a failing git renders a could-not-compute card instead, and
git trouble never breaks a turn.

The decision ladder, the note framing, and the hard cap are the shared
contract of ``internal/servingidentity/codedelta.go``: the Go twin unit-pins
them and the shared honesty table
(``internal/servingidentity/testdata/serving_cases.json``) pins the bytes on
BOTH twins — see ``test_serving_identity_contract.py`` for the executed
parity runs. What only Python has is the git provider this module wraps:
the ``git log``/``git diff`` reads, their per-build-pair memo, and the
never-raise degrades around them. Those live here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gateway.serving_codedelta import code_delta_note

_OLD_SHA = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
_NEW_SHA = "e5f60718e5f60718e5f60718e5f60718e5f60718"


def _previous(**record):
    """A parseable predecessor record; ``code_sha`` absent unless given."""
    base = {"pid": 709505, "start_time": 3001234}
    base.update(record)
    return base


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
