"""Private-org branch-verify refusals from the Cursor Cloud Agents API.

Cursor's cloud create (``POST /v1/agents``) cannot verify that a branch
exists in a repository owned by a private organization: on GitHub the branch
is real, but Cursor's verifier has no access to the org, so the create fails
with HTTP 400 ``validation_error`` naming the branch and the repository.
Retrying the same ``startingRef``, or dropping it and retrying, never yields
a cloud agent for that org — the delegation has to move to a local executor,
not retry Cursor.

This sibling owns the refusal matcher and the Claude failover result
builder; ``tools/cursor_agent_tool.py`` only decides and finalizes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Tuple

logger = logging.getLogger(__name__)

CLAUDE_FALLBACK_NOTE = "Cursor refused the private-org branch and Claude ran"
DO_NOT_RETRY_STARTING_REF = "do not retry this startingRef"

# "Failed to verify existence of branch '<name>' in repository <owner>/<repo>".
# The backreference keeps the quote pair matched (single with single, double
# with double) and <name> may contain anything but a quote — branch names
# carry slashes. A trailing sentence period after the repo still matches:
# '.' sits inside the owner/repo character class.
_BRANCH_VERIFY_SENTENCE_RE = re.compile(
    r"Failed to verify existence of branch (?P<q>['\"])(?P<branch>[^'\"]+)(?P=q) "
    r"in repository (?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)"
)


def is_private_org_branch_verify_refusal(message: str) -> bool:
    """True only for Cursor's private-org branch-verify create refusal.

    All four must hold: the wrapped API error names ``HTTP 400``, carries
    ``validation_error``, comes from ``POST /v1/agents``, and contains the
    branch-verify sentence. Any other 400 (a bad model id), a 5xx carrying
    the same sentence, and 409 conflicts are not refusals — they keep the
    ordinary hard-failure path.
    """
    if not isinstance(message, str):
        return False
    if "HTTP 400" not in message:
        return False
    if "validation_error" not in message:
        return False
    if "POST /v1/agents" not in message:
        return False
    return _BRANCH_VERIFY_SENTENCE_RE.search(message) is not None


def _wrapper_missing(error: Any) -> bool:
    """The real missing-wrapper errors say ``wrapper binary not found``."""
    return isinstance(error, str) and "binary not found" in error


def _unavailable_error(cursor_error: str) -> str:
    text = str(cursor_error)
    if text.endswith(DO_NOT_RETRY_STARTING_REF):
        return text
    return f"{text} {DO_NOT_RETRY_STARTING_REF}"


def claude_failover_result(*, task: str, workdir: str, cursor_error: str) -> Tuple[str, str]:
    """Run Claude locally for a refusal; build ``(result_json, outcome)``.

    Claude is called exactly once, blocking, with ``acceptEdits``; the task
    and the absolute workdir pass through verbatim and no model is chosen
    for it. When Claude is unavailable — the call raises, the result is not
    a JSON object, ``success`` is not a boolean, or the missing-wrapper
    error — the original Cursor error comes back carrying the do-not-retry
    instruction and nothing claims Claude ran. Otherwise the facade JSON
    reports both sides (``fallback=claude_local``) and the outcome is
    ``success`` only when Claude succeeded. The result never re-enters the
    cloud-result builder or the evidence gate.
    """
    from tools import claude_agent_tool
    from tools.cursor_agent_tool import _make_result

    try:
        claude_text = claude_agent_tool.delegate_claude_agent(
            task=task,
            workdir=workdir,
            background=False,
            permission_mode="acceptEdits",
        )
    except Exception:
        logger.info(
            "Claude failover for a Cursor private-org refusal could not run",
            exc_info=True,
        )
        claude_text = None
    try:
        claude = json.loads(claude_text) if isinstance(claude_text, str) else None
    except json.JSONDecodeError:
        claude = None

    unavailable = (
        _make_result(success=False, error=_unavailable_error(cursor_error)),
        "failed",
    )
    if not isinstance(claude, dict):
        return unavailable
    claude_success = claude.get("success")
    if not isinstance(claude_success, bool):
        return unavailable
    if claude_success is False and _wrapper_missing(claude.get("error")):
        return unavailable

    claude_error = claude.get("error")
    if not isinstance(claude_error, str):
        claude_error = None
    final_report = claude.get("final_report")
    result_json = _make_result(
        success=claude_success,
        error=claude_error,
        final_report=final_report if isinstance(final_report, str) else "",
        fallback="claude_local",
        fallback_note=CLAUDE_FALLBACK_NOTE,
        cursor_error=cursor_error,
        claude_success=claude_success,
        claude_error=claude_error,
    )
    return result_json, "success" if claude_success else "failed"
