"""Target and action validation shared by every leg of ``manage_connections``.

``connectors`` entries are bare slugs or ``{name, mcp}`` objects; bare strings and ``{name}`` are
managed connectors, ``{name, mcp: true}`` is a local MCP server. Connector verbs need managed
targets, MCP verbs need MCP targets. Pure functions, no I/O.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

CONNECTOR_ACTIONS = ("status", "connect", "reconnect", "wait")
MCP_ACTIONS = ("install", "enable", "authorize")
ALL_ACTIONS = CONNECTOR_ACTIONS + MCP_ACTIONS

_TARGET_FIELDS = frozenset({"name", "mcp"})


def normalize_targets(raw: Any) -> Tuple[List[str], List[str], Optional[str]]:
    """``connectors`` → ``(managed names, mcp names, error)``. Bare strings and ``{name}`` are
    managed; ``{name, mcp: true}`` is a local MCP. Any other field is an error."""
    if raw is None:
        return [], [], None
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return [], [], "'connectors' must be a list of names or {name, mcp} objects."
    managed: List[str] = []
    mcp: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            unknown = sorted(set(item) - _TARGET_FIELDS)
            if unknown:
                return [], [], (
                    f"unknown target field(s) {', '.join(unknown)}: a target is "
                    "{\"name\": \"<slug>\"} or {\"name\": \"<server>\", \"mcp\": true}. Transport, "
                    "URLs and credentials come from the catalog manifest, never from the call."
                )
            name = str(item.get("name") or "").strip().lower()
            is_mcp = bool(item.get("mcp", False))
        else:
            name, is_mcp = str(item or "").strip().lower(), False
        if not name:
            return [], [], "every target needs a non-empty 'name'."
        bucket = mcp if is_mcp else managed
        if name not in bucket:
            bucket.append(name)
    return managed, mcp, None


def validate_action(action: str, managed: List[str], mcp: List[str]) -> Optional[str]:
    """MCP verbs need ``mcp:true`` targets; connector verbs need managed targets."""
    if action not in ALL_ACTIONS:
        return (
            f"action must be one of {', '.join(ALL_ACTIONS)}. "
            f"{', '.join(MCP_ACTIONS)} apply to local MCP servers "
            "(targets {\"name\": ..., \"mcp\": true}); the rest apply to managed connectors. "
            "Disconnecting an account is done by the user in the Nous Portal dashboard, not "
            "through this tool."
        )
    if action in MCP_ACTIONS:
        if managed:
            return (
                f"'{action}' is an MCP action: every target must carry \"mcp\": true "
                f"(got managed connector(s) {', '.join(managed)}). Managed connectors use "
                "connect / reconnect / wait / status."
            )
        if not mcp:
            return (
                f"'{action}' requires 'connectors': the MCP server name(s), e.g. "
                "[{\"name\": \"linear\", \"mcp\": true}]."
            )
    elif mcp:
        return (
            f"'{action}' is a managed-connector action; MCP targets ({', '.join(mcp)}) use "
            f"{', '.join(MCP_ACTIONS)}."
        )
    return None
