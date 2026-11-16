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

import json
import re
from typing import Any, Dict, List, Optional

# Result-text markers (case-insensitive) that mean "the wrapper refused
# this tool call" when the event did not set is_error explicitly.
_REJECTION_MARKERS = (
    "rejected",
    "not allowed",
    "permission denied",
    "unknown tool",
    "tool not found",
    "invalid tool",
    "denied by",
)

# A short result whose text smells like a refusal is a rejection. Long
# results are real tool output — a Read dump that happens to contain the
# word "error" is data, not a refusal.
_HEURISTIC_RESULT_MAX_CHARS = 400

# Tools that change the repo. Any attempt at one means the run is making
# progress, whatever the tail looks like.
_PROGRESS_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Bash counts as progress unless the command is one of these read-only
# inspections, matched word-bounded at the start of the stripped command.
_BASH_READONLY_RE = re.compile(
    r"^(?:git status|git diff|git log|git show|git rev-parse|"
    r"ls|cat|rg|grep|find|head|tail|wc|pwd|echo)\b"
)

_MAX_REASON_CHARS = 200
_ABORT_REJECTIONS = 3
_TAIL_TOOLS = 5


def _canonical_input(tool_input: Any) -> str:
    """Deterministic text for one tool call's input (loop-identity key)."""
    if isinstance(tool_input, (dict, list)):
        return json.dumps(
            tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    return str(tool_input)


def _content_text(content: Any) -> str:
    """Flatten a tool_result content (string or block list) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
    return "".join(parts)


def _cap(text: str, limit: int = _MAX_REASON_CHARS) -> str:
    return text if len(text) <= limit else text[:limit]


def _is_rejected(result_text: Optional[str], is_error: Any) -> bool:
    if is_error is True:
        return True
    if is_error is False:
        return False
    text = result_text or ""
    if len(text) >= _HEURISTIC_RESULT_MAX_CHARS:
        return False
    folded = text.casefold()
    return any(marker in folded for marker in _REJECTION_MARKERS)


def _is_progress(name: str, tool_input: Any) -> bool:
    if name in _PROGRESS_TOOLS:
        return True
    if name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else ""
        return not _BASH_READONLY_RE.match(str(command or "").strip())
    return False


def _blocker_sentence(tool: str, reason: str, identical: bool) -> str:
    prefix = "repeated identical" if identical else "repeated"
    return (
        f"{prefix} {tool} attempts without progress: "
        f"last tool {tool}, reason {reason}"
    )


def summarize_claude_stream(log_text: str) -> Dict[str, Any]:
    """Summarize assistant activity and tool-loop evidence in a run log.

    Malformed lines and non-dict events are skipped; a system
    ``thinking_tokens`` blip is never an API call.
    """
    assistant_records = 0
    message_ids = set()
    stop_reason_records = 0
    attempts: List[Dict[str, Any]] = []
    results: Dict[str, Any] = {}

    for raw_line in (log_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if event.get("type") == "assistant":
            assistant_records += 1
            if isinstance(message, dict):
                message_id = message.get("id")
                if isinstance(message_id, str) and message_id:
                    message_ids.add(message_id)
                if message.get("stop_reason") is not None:
                    stop_reason_records += 1
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name")
                        name = name if isinstance(name, str) else str(name)
                        call_id = block.get("id")
                        tool_input = block.get("input")
                        attempts.append(
                            {
                                "name": name,
                                "call_id": str(call_id) if call_id is not None else "",
                                "signature": f"{name}\n{_canonical_input(tool_input)}",
                                "progress": _is_progress(name, tool_input),
                            }
                        )
        elif event.get("type") == "user":
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        call_id = block.get("tool_use_id")
                        if call_id is None:
                            continue
                        results[str(call_id)] = {
                            "text": _content_text(block.get("content")),
                            "is_error": block.get("is_error"),
                        }

    if message_ids:
        api_calls = len(message_ids)
    elif stop_reason_records:
        api_calls = stop_reason_records
    else:
        api_calls = assistant_records

    # Pair each attempt with its result; unpaired attempts (run killed
    # between call and result) carry no rejection evidence.
    for attempt in attempts:
        result = results.get(attempt["call_id"])
        if result is None:
            attempt["result_text"] = None
            attempt["is_error"] = None
            attempt["rejected"] = False
        else:
            attempt["result_text"] = result["text"]
            attempt["is_error"] = result["is_error"]
            attempt["rejected"] = _is_rejected(result["text"], result["is_error"])

    blocker: Optional[str] = None
    abort = False
    last_tool = attempts[-1]["name"] if attempts else None
    last_result = attempts[-1]["result_text"] if attempts else None
    last_reason = _cap(last_result) if last_result else None

    tail3 = attempts[-_ABORT_REJECTIONS:]
    if (
        len(tail3) == _ABORT_REJECTIONS
        and len({a["name"] for a in tail3}) == 1
        and all(a["rejected"] for a in tail3)
    ):
        abort = True
        reason = _cap(tail3[-1]["result_text"] or "") or "rejected"
        blocker = _blocker_sentence(
            tail3[-1]["name"], reason,
            identical=len({a["signature"] for a in tail3}) == 1,
        )
    else:
        tail5 = attempts[-_TAIL_TOOLS:]
        if (
            len(attempts) >= _TAIL_TOOLS
            and len({a["name"] for a in tail5}) == 1
            and not any(a["progress"] for a in attempts)
        ):
            # Planning bursts are legal: annotate the receipt, never kill.
            reason = _cap(attempts[-1]["result_text"] or "") or "no source change"
            blocker = _blocker_sentence(attempts[-1]["name"], reason, identical=False)

    return {
        "assistant_records": assistant_records,
        "api_calls": api_calls,
        "last_tool": last_tool,
        "last_reason": last_reason,
        "blocker": blocker,
        "abort": abort,
    }
