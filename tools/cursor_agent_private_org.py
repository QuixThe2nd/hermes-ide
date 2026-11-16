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

from typing import Tuple

CLAUDE_FALLBACK_NOTE = "Cursor refused the private-org branch and Claude ran"
DO_NOT_RETRY_STARTING_REF = "do not retry this startingRef"


def is_private_org_branch_verify_refusal(message: str) -> bool:
    """True only for Cursor's private-org branch-verify create refusal.

    PIN: the matcher lands in the fix commit; until then every message is
    treated as an ordinary failure, keeping today's hard-failure path.
    """
    del message
    return False


def claude_failover_result(*, task: str, workdir: str, cursor_error: str) -> Tuple[str, str]:
    """Run the Claude failover for a refusal; build ``(result_json, outcome)``.

    PIN: unreachable while the matcher stub returns False. It keeps today's
    hard-failure shape so wiring it into the handler changes no behavior;
    the fix commit replaces it with the real Claude call and result.
    """
    from tools.cursor_agent_tool import _make_result

    del task, workdir
    return _make_result(success=False, error=cursor_error), "failed"
