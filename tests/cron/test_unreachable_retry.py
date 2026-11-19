"""Cowork-inspired bounded automatic re-runs for cron fires that never reached the model.

Contract (cron/unreachable_retry.py): a recurring job whose run fails with a transient
network/DNS error before ANY model call gets its ``next_run_at`` pulled earlier along a
bounded ladder (5/15/30 min); a run that reaches the model resets the ladder, and the
ladder never fires past its last rung.
"""

from datetime import datetime, timedelta, timezone

import pytest

from cron import unreachable_retry as ur
from cron.jobs import create_job, get_job, mark_job_run


@pytest.fixture
def tmp_cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_unreachable_failure_pulls_next_run_earlier_then_ladder_exhausts(tmp_cron_home):
    """Failed-unreachable runs re-fire on the 5/15/30-minute ladder instead of waiting a
    full period, and the ladder stops after its last rung (falls back to the schedule)."""
    # Interval, not a cron expression: the natural next fire is always a full day out. A
    # fixed clock time ("0 3 * * *") makes the 30-minute rung land past the natural fire
    # in the half hour before it, and plan_retry rightly yields to the schedule (CI red).
    job = create_job("nightly report", "every 24h")
    job_id = job["id"]

    now = datetime.now(timezone.utc)
    for i, delay in enumerate(ur.RETRY_DELAYS_SECONDS):
        assert mark_job_run(job_id, False, "ConnectError: dns", model_unreachable=True)
        j = get_job(job_id)
        nxt = datetime.fromisoformat(j["next_run_at"])
        # Pulled to roughly now + ladder delay, far before the daily occurrence.
        assert timedelta(0) < nxt - now <= timedelta(seconds=delay + 120), (
            f"attempt {i}: expected retry ~{delay}s out, got {nxt - now}")
        assert j[ur.STATE_KEY]["attempt"] == i + 1

    # Ladder exhausted: the next unreachable failure keeps the natural schedule.
    assert mark_job_run(job_id, False, "ConnectError: dns", model_unreachable=True)
    j = get_job(job_id)
    assert j.get(ur.STATE_KEY) is None
    assert datetime.fromisoformat(j["next_run_at"]) - now > timedelta(hours=1)


def test_reaching_the_model_resets_ladder_and_oneshots_never_retry(tmp_cron_home):
    """Any run that reached the model clears retry state; one-shots (pre-claimed
    dispatch, at-most-times #38758) never enter the ladder."""
    job = create_job("hourly sync", "every 12h")
    job_id = job["id"]
    assert mark_job_run(job_id, False, "ConnectError: dns", model_unreachable=True)
    assert get_job(job_id)[ur.STATE_KEY]["attempt"] == 1

    # A normal failed run (model reached) resets the ladder and stays on schedule.
    assert mark_job_run(job_id, False, "agent error")
    j = get_job(job_id)
    assert j.get(ur.STATE_KEY) is None
    now = datetime.now(timezone.utc)
    assert datetime.fromisoformat(j["next_run_at"]) - now > timedelta(hours=11)

    # One-shot: flag is ignored, no retry state, no resurrection.
    once = create_job("one shot", _iso(datetime.now(timezone.utc) + timedelta(minutes=1)))
    assert mark_job_run(once["id"], False, "ConnectError: dns", model_unreachable=True)
    remaining = get_job(once["id"])
    assert remaining is None or remaining.get(ur.STATE_KEY) is None

# ---------------------------------------------------------------------------
# is_model_unreachable_failure — recognition of the shapes the scheduler raises
# ---------------------------------------------------------------------------


class _Agent:
    """Agent double: only ``session_api_calls`` (completed calls) matters to the guard."""

    def __init__(self, session_api_calls: int = 0):
        self.session_api_calls = session_api_calls


@pytest.mark.parametrize("text", [
    "Connection error.",
    "Connection refused",
    "Connection reset by peer",
    "server disconnected without response",
    "Network is unreachable",
    "Temporary failure in name resolution",
    "upstream request timed out",
    "Codex stream produced no parsed stream event within 120s (TTFB threshold: 120s)",
])
def test_wrapped_transport_error_texts_are_recognized_at_zero_calls(text):
    """The scheduler's failure path raises ``RuntimeError(<agent error text>)``
    (``cron/scheduler.py::_final_response_from_result``): the transport exception
    TYPE is gone, only the prose survives. A transient transport text at zero
    completed API calls must flag the run or the re-run ladder is dead."""
    assert ur.is_model_unreachable_failure(RuntimeError(text), _Agent(0))


def test_wrapped_transport_error_after_reaching_the_model_is_not_recognized():
    """Zero-API-calls guard: a run that completed model calls may have partial work
    and side effects — it must never auto-retry, whatever the error text says."""
    assert not ur.is_model_unreachable_failure(RuntimeError("Connection error."), _Agent(3))


@pytest.mark.parametrize(("reason", "retryable"), [
    ("rate_limit", True),
    ("billing", False),
    ("auth", True),
    ("auth_permanent", False),
    ("content_policy_blocked", False),
    ("context_overflow", False),
])
def test_nontransient_failures_are_never_recognized(reason, retryable):
    """Walls a re-run cannot move — rate limit, billing/quota, auth, content policy,
    context overflow — must surface immediately, both as wrapped text and as the
    agent result's structured verdict."""
    text = {
        "rate_limit": "Rate limit exceeded (429): too many requests, quota exhausted",
        "billing": "Billing or credits exhausted: 402 Payment Required",
        "auth": "401 Unauthorized: Incorrect API key provided",
        "auth_permanent": "403 Forbidden: credential revoked",
        "content_policy_blocked": "content_policy_blocked: I can't help with that request",
        "context_overflow": "Context overflow: prompt is too large for the model window",
    }[reason]
    assert not ur.is_model_unreachable_failure(RuntimeError(text), _Agent(0))
    result = {"failed": True, "completed": False, "failure_reason": reason,
              "failure_retryable": retryable}
    assert not ur.is_model_unreachable_failure(RuntimeError(text), _Agent(0), result=result)


def test_structured_verdict_flags_transient_provider_reasons():
    """The agent result carries the classifier's verdict (``failure_reason`` /
    ``failure_retryable``, or a ``turn_exit_reason`` the finalizer's table maps);
    when present it is authoritative, so recognition no longer depends on the
    wrapped prose matching a needle."""
    classified = {"failed": True, "completed": False, "error": "Connection error.",
                  "api_calls": 3, "failure_reason": "timeout", "failure_retryable": True}
    assert ur.is_model_unreachable_failure(
        RuntimeError("Connection error."), _Agent(0), result=classified)
    # Finalizer shape: verdict derived from turn_exit_reason (all_retries_exhausted_
    # no_response -> server_error, retryable), no explicit failure_reason.
    exit_reason_shaped = {"failed": True, "completed": False,
                          "turn_exit_reason": "all_retries_exhausted_no_response"}
    assert ur.is_model_unreachable_failure(
        RuntimeError("provider didn't answer"), _Agent(0), result=exit_reason_shaped)
    # 503/529 provider overload is the same zero-spend transient family (retryable).
    overloaded = {"failed": True, "completed": False,
                  "failure_reason": "overloaded", "failure_retryable": True}
    assert ur.is_model_unreachable_failure(
        RuntimeError("503 overloaded"), _Agent(0), result=overloaded)


def test_structured_verdict_excludes_transport_looking_context_failures():
    """A context-compression timeout says "timed out" but is a context wall, not a
    transport blip; its own exit-reason verdict must exclude it from the ladder."""
    result = {"failed": True, "completed": False,
              "error": "Turn-start preflight compression timed out",
              "turn_exit_reason": "context_compression_timeout"}
    assert not ur.is_model_unreachable_failure(
        RuntimeError("Turn-start preflight compression timed out"), _Agent(0), result=result)


def test_structured_verdict_with_completed_calls_is_not_recognized():
    """The structured verdict never bypasses the zero-completed-calls guard."""
    result = {"failed": True, "completed": False, "error": "Connection error.",
              "failure_reason": "timeout", "failure_retryable": True}
    assert not ur.is_model_unreachable_failure(
        RuntimeError("Connection error."), _Agent(2), result=result)


def test_raw_transport_exception_types_are_still_recognized():
    """Preflight/DNS failures raise raw transport exceptions (not wrapped prose);
    the existing type/DNS-text matching in ``_is_transient_provider_resolve_error``
    keeps working unchanged through the delegation."""
    import socket

    import httpx

    assert ur.is_model_unreachable_failure(httpx.ConnectError("dns blip"), _Agent(0))
    assert ur.is_model_unreachable_failure(
        socket.gaierror(-2, "Name or service not known"), _Agent(0))
    assert ur.is_model_unreachable_failure(
        RuntimeError("name or service not known"), _Agent(0))
