"""Stream-progress summary for a Claude Code stream-json run log.

``summarize_claude_stream`` derives activity and tool-loop evidence from the
JSONL stream a delegated Claude Code run writes while it works — the data a
timed-out (killed) run leaves behind, since a killed run never emits its
final ``type=result`` event (the only place ``parse_claude_agent_log`` finds
``num_turns``). Two questions it answers:

- How much model activity happened? Distinct assistant ``message.id``
  values are API calls; a system ``thinking_tokens`` blip is not.
- Is the run stuck retrying one rejected tool, or parked on one
  non-progress tool with no source change? Three consecutive rejections of
  the same tool abort the live run ("tool_loop"); a five-deep same-tool
  tail with zero progress-tool attempts anywhere only annotates the
  timeout receipt (planning bursts are legal, so that case never kills).

Go twin: internal/claudestream/progress.go — same rules, independently
tested. Production Python never calls it.
"""

from __future__ import annotations

from typing import Any, Dict


def summarize_claude_stream(log_text: str) -> Dict[str, Any]:
    """Summarize assistant activity and tool-loop evidence in a run log.

    Stub: returns the zero summary so every call site imports cleanly and
    existing behavior (empty log ⇒ no activity, no blocker) is unchanged.
    """
    return {
        "assistant_records": 0,
        "api_calls": 0,
        "last_tool": None,
        "last_reason": None,
        "blocker": None,
        "abort": False,
    }
