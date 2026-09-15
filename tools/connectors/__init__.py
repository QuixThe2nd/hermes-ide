"""Connectors: managed connector accounts (Nous tool gateway) and local MCP servers behind one
model tool, ``manage_connections``.

Layout (a deep module with a narrow door):

- ``tool.py`` — the model tool: schema, ``registry.register`` (the one file discovery scans in this
  package), and the action dispatcher.
- ``targets.py`` — target and action validation shared by every leg. Pure.
- ``operation.py`` — ``ConnectionOperation`` / ``Target``: targets, server-owned deadline,
  exactly-once settlement. Pure data, no I/O.
- ``mcp.py`` — the local-MCP leg (install / enable / authorize through the approval card).
- ``managed.py`` — the managed-connector leg (status / connect / reconnect / wait).
- ``search.py`` — the ``tool_search`` / ``tool_describe`` adapter for remote connector tools.
- ``dispatch.py`` — remote connector calls re-entering ``model_tools.handle_function_call`` so
  per-tool policy runs against the composed ``connectors__<connector>__<tool>`` name.
- ``gateway/`` — the typed client for the tool gateway's connector routes; see its own docstring
  for the layering rules inside it.

The names below are the whole cross-package surface. ``model_tools`` and ``tool_search`` are
core plumbing and deep-import a few helpers past this door on purpose; nothing else should. An
importer outside this package needing a name not listed here is a design question, not a reason
to add the name.
"""

from tools.connectors.dispatch import dispatch_connector_batch, dispatch_connector_call
from tools.connectors.gateway.bridge import connector_describe, connector_search_hits
from tools.connectors.gateway.config import connectors_available
from tools.connectors.gateway.names import CONNECTOR_BATCH_SENTINEL, is_connector_name
from tools.connectors.tool import MANAGE_CONNECTIONS_SCHEMA, manage_connections

__all__ = [
    "CONNECTOR_BATCH_SENTINEL",
    "MANAGE_CONNECTIONS_SCHEMA",
    "connector_describe",
    "connector_search_hits",
    "connectors_available",
    "dispatch_connector_batch",
    "dispatch_connector_call",
    "is_connector_name",
    "manage_connections",
]
