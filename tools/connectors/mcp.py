"""MCP targets of ``manage_connections``: catalog validation and the approval leg. The card is
reached via ``agent.connection_callback`` through the inline executor; registry dispatch has no
callback and settles targets ``unavailable``."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from tools.connectors.operation import (
    CONNECTED,
    FAILED,
    SETTLED_ALL_RESOLVED,
    SETTLED_CONTINUE,
    SETTLED_DEADLINE,
    SETTLED_INTERRUPT,
    SETTLED_UNAVAILABLE,
    SKIPPED,
    UNAVAILABLE,
    ConnectionOperation,
    Target,
    resolve_wait_timeout,
)
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Renderer outcome → operation state. declined = Not now; error = recoverable, operation stays open.
_OUTCOME_STATES = {
    "installed": CONNECTED, "enabled": CONNECTED, "authorized": CONNECTED, "connected": CONNECTED,
    "declined": SKIPPED, "skipped": SKIPPED,
    "error": FAILED, "failed": FAILED,
}

UNAVAILABLE_HINT = "hermes mcp install {name} / hermes mcp login {name}"






def _catalog_names() -> List[str]:
    from hermes_cli.mcp_catalog import list_catalog

    return sorted(e.name for e in list_catalog())


def _configured_names() -> List[str]:
    from hermes_cli.mcp_catalog import installed_servers

    return sorted(installed_servers())


def validate_mcp_names(action: str, names: List[str]) -> Optional[str]:
    """install: catalog names only; enable/authorize: configured servers only."""
    try:
        catalog = _catalog_names()
        configured = _configured_names()
    except Exception as exc:
        return f"could not read the MCP catalog: {exc}"
    allowed = set(catalog) if action == "install" else set(configured)
    unknown = [n for n in names if n not in allowed]
    if not unknown:
        return None
    if action == "install":
        return (
            f"unknown MCP server(s) for install: {', '.join(unknown)}. Install works for "
            f"catalog entries only: {', '.join(catalog) or '(empty catalog)'}."
            + (f" Already configured (use enable/authorize): {', '.join(configured)}." if configured else "")
        )
    return (
        f"unknown MCP server(s) for {action}: {', '.join(unknown)}. {action} works for servers "
        f"already in mcp_servers: {', '.join(configured) or '(none configured)'}."
        + (f" Catalog entries you can install: {', '.join(catalog)}." if catalog else "")
    )


def _unavailable_result(operation: ConnectionOperation) -> str:
    for target in operation.targets:
        operation.record_target(
            target.name, UNAVAILABLE, "no approval surface in this session",
            hint=UNAVAILABLE_HINT.format(name=target.name),
        )
    operation.settle(SETTLED_UNAVAILABLE)
    payload = operation.result()
    payload["status"] = "unavailable"
    payload["note"] = (
        "This session has no approval card, so local MCP servers cannot be set up here. Tell "
        "the user to run the terminal commands in each target's 'hint', then continue."
    )
    return json.dumps(payload, ensure_ascii=False)


def _apply_answer(operation: ConnectionOperation, raw: str) -> str:
    """Apply the renderer's per-target answer; returns the settle reason the target states imply."""
    try:
        answer = json.loads(raw)
    except (TypeError, ValueError):
        answer = {}
    if not isinstance(answer, dict):
        answer = {}
    for entry in answer.get("targets") or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower()
        state = _OUTCOME_STATES.get(str(entry.get("state") or entry.get("status") or "").lower())
        if not name or state is None:
            continue
        extra = {k: v for k, v in entry.items() if k in ("tools",)}
        operation.record_target(name, state, str(entry.get("detail") or ""), **extra)
    # Derived from target state; the renderer's own claim is ignored.
    return SETTLED_ALL_RESOLVED if operation.all_resolved else SETTLED_CONTINUE


def run_mcp_operation(
    names: List[str],
    action: str,
    reason: str,
    *,
    connection_callback: Optional[Callable[[Dict[str, Any]], Optional[str]]],
    session_id: Optional[str],
    wait_seconds: Optional[float] = None,
) -> str:
    """One operation for the MCP targets of a call. Returns the tool's JSON string."""
    error = validate_mcp_names(action, names)
    if error:
        return tool_error(error)
    operation = ConnectionOperation(
        [Target(n, "mcp", action) for n in names],
        session_key=str(session_id or ""),
        wait_seconds=resolve_wait_timeout() if wait_seconds is None else wait_seconds,
    )
    if connection_callback is None:
        return _unavailable_result(operation)

    try:
        raw = connection_callback(operation.request_payload(reason))
    except Exception as exc:
        return tool_error(f"MCP approval flow failed: {exc}")

    settled_by = _apply_answer(operation, raw or "")
    if raw:
        operation.settle(settled_by)
    else:
        # Empty answer: deadline passed or the turn was interrupted.
        from tools.interrupt import is_interrupted

        operation.settle(SETTLED_INTERRUPT if is_interrupted() else SETTLED_DEADLINE)

    payload = operation.result()
    payload["status"] = "settled"
    payload["note"] = (
        "Settled once; do not re-ask for any target the user skipped or that timed out — "
        "continue without it or ask in chat. Tools of a newly installed or authorized server "
        "become available on your next turn."
    )
    return json.dumps(payload, ensure_ascii=False)
