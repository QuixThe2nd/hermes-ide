"""The hygiene "did not rotate or compact in place" warning names the real cause (#71097).

The gateway's terminal branch used to blame "no session_db on the hygiene agent" for every
route into it, including attempts that aborted before any commit boundary on a DB-backed agent.
"""

from types import SimpleNamespace

from gateway.run_turn import hygiene_no_commit_reason


def _agent(**signals):
    base = {
        "_last_compression_attempt_recorded": True,
        "_last_compression_attempt_in_place": None,
        "_compression_skipped_due_to_lock": None,
        "_compression_blocked_transient": None,
        "_last_compression_timed_out": False,
        "_last_compression_summary_warning": None,
        "_session_db": object(),
    }
    base.update(signals)
    return SimpleNamespace(**base)


def test_aborted_attempt_on_db_backed_agent_is_not_blamed_on_missing_session_db():
    # codex_app_server thread interrupted / summary aborted: the compressor never reached the
    # commit boundary, so `_last_compression_attempt_in_place` stays None while `_session_db` is real.
    reason = hygiene_no_commit_reason(_agent())
    assert "no session_db" not in reason
    assert "aborted" in reason

    timed_out = hygiene_no_commit_reason(_agent(_last_compression_timed_out=True))
    assert "timed out" in timed_out and "no session_db" not in timed_out

    lock = hygiene_no_commit_reason(_agent(_compression_skipped_due_to_lock="other-sid"))
    assert "lease" in lock

    blocked = hygiene_no_commit_reason(_agent(_compression_blocked_transient="cooldown:59"))
    assert "cooldown:59" in blocked


def test_missing_session_db_is_still_named_when_it_is_the_cause():
    reason = hygiene_no_commit_reason(_agent(_last_compression_attempt_in_place=False, _session_db=None))
    assert reason == "no session_db on the hygiene agent"
    assert hygiene_no_commit_reason(_agent(_last_compression_attempt_recorded=False)) == "compression did not run"
