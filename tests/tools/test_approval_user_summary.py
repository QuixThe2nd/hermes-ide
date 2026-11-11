"""Blocked/background-task outcomes carry one plain sentence for the person at the keyboard.

The model-facing ``BLOCKED: ... Do NOT retry`` text is unchanged; surfaces render ``user_summary``
first (UX message audit, findings tools-runtime-15, -26, -31).
"""

from __future__ import annotations

import json


def test_cli_completion_line_prefers_user_summary_over_blocked_text():
    from agent.display import _detect_tool_failure
    result = json.dumps({"output": "", "exit_code": -1, "status": "blocked",
                         "error": "BLOCKED: User denied this command. The user has NOT consented ...",
                         "user_summary": "You denied this command — it did not run."})
    failed, suffix = _detect_tool_failure("terminal", result)
    assert failed and "You denied this command" in suffix and "BLOCKED" not in suffix


def test_stalled_delegation_text_has_no_issue_numbers_or_worker_jargon():
    from tools.async_delegation import _stalled_error_text
    text = _stalled_error_text({"goal": "Summarise the  weekly report", "_stall_quiet_seconds": 610.0})
    assert text.startswith('Background task "Summarise the weekly report"')
    assert "10 min" in text and "run it again" in text
    for banned in ("#60203", "completion event", "wedged", "Re-dispatch", "delegation"):
        assert banned not in text
