"""A gateway approval choice that lands after the deadline check is an answer, not a timeout (#112548).

``_await_gateway_decision`` polls the entry's event with a deadline; ``resolve_gateway_approval`` may
commit a choice between the poll giving up and the entry leaving the queue. The verdict must be the
choice committed under the approval lock — the client was acked ``ok`` for it — never "timeout".
"""

from tools import approval as mod
from tools import approval_gateway_wait as wait_mod


SESSION_KEY = "approval-late-choice"
APPROVAL = {"command": "rm -rf build", "description": "d", "pattern_key": "dangerous", "pattern_keys": ["dangerous"]}


def _clear():
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()


def test_choice_landing_after_the_deadline_check_is_resolved(monkeypatch):
    _clear()
    hooks: list[tuple[str, dict]] = []
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: hooks.append((name, kw)))

    def deadline_passed_then_user_answered(event, session_key, *, interrupt_log):
        # The poll loop has already decided "timeout"; the user's /approve lands before the entry leaves the queue.
        assert mod.resolve_gateway_approval(session_key, "once") == 1
        return "timeout"

    monkeypatch.setattr(wait_mod, "_poll_event", deadline_passed_then_user_answered)

    decision = wait_mod._await_gateway_decision(SESSION_KEY, lambda data: None, APPROVAL)

    assert decision == {"resolved": True, "choice": "once", "reason": None}
    assert hooks[-1][0] == "post_approval_response" and hooks[-1][1]["choice"] == "once"
    assert SESSION_KEY not in mod._gateway_queues


def test_plain_timeout_still_reports_unresolved(monkeypatch):
    _clear()
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: None)
    monkeypatch.setattr(wait_mod, "_poll_event", lambda event, session_key, *, interrupt_log: "timeout")

    decision = wait_mod._await_gateway_decision(SESSION_KEY, lambda data: None, APPROVAL)

    assert decision == {"resolved": False, "choice": None, "reason": None}
    # Nothing is left for a late /approve to hit: the client learns nothing was pending.
    assert mod.resolve_gateway_approval(SESSION_KEY, "once") == 0
