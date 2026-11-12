"""Prompt-send TIMEOUT must not trigger the re-ask/fallback lane.

A scheduling future can hit its ``result(timeout=...)`` deadline after the
interactive card has already posted but before the connector acknowledgment
arrives. Treating that timeout as a definitive failure duplicates the prompt;
an older card can then resolve a turn that has already moved on.

Every prompt caller crossing this boundary treats TimeoutError as AMBIGUOUS:
do not fall back or re-send, and leave registration live. A definitive error
(SendResult success=False or another exception) still uses the fallback.
"""

import concurrent.futures
from unittest.mock import MagicMock


from gateway.run import _prompt_send_outcome


class _Result:
    def __init__(self, success, error=None):
        self.success = success
        self.error = error


def test_timeout_is_ambiguous_not_failure():
    fut = MagicMock()
    fut.result.side_effect = concurrent.futures.TimeoutError()
    outcome = _prompt_send_outcome(fut, timeout=0.01)
    assert outcome == "ambiguous", (
        "a send timeout re-ran the fallback; ambiguous must suppress the re-ask"
    )


def test_success_is_sent():
    fut = MagicMock()
    fut.result.return_value = _Result(True)
    assert _prompt_send_outcome(fut, timeout=1) == "sent"


def test_definitive_error_result_is_failed():
    fut = MagicMock()
    fut.result.return_value = _Result(False, "relay prompt op unavailable")
    assert _prompt_send_outcome(fut, timeout=1) == "failed"


def test_non_timeout_exception_is_failed():
    fut = MagicMock()
    fut.result.side_effect = RuntimeError("loop unavailable")
    assert _prompt_send_outcome(fut, timeout=1) == "failed"


def test_missing_future_is_failed():
    assert _prompt_send_outcome(None, timeout=1) == "failed"
