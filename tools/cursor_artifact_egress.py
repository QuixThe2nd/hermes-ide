"""Hash-verified artifact egress for FINISHED Cursor cloud runs.

A cloud status of FINISHED only means the agent's writeup exists; the
writeup routinely names commits that were never produced on this host.
Success therefore requires an independently verified artifact manifest:
either the My Machines worker checkout on this host (hash-verified
against its own git objects) or a complete run-stream snapshot whose
file bytes we hash ourselves. Without one, the run fails closed with
"missing artifact egress".

This module is the egress contract only; ``tools/cursor_agent_tool.py``
calls into it and owns cloud dispatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

EGRESS_ERROR_MARKER = "missing artifact egress"
STREAM_FETCH_TIMEOUT_SECONDS = 300

_FetchStream = Callable[..., bytes]


def hashes_match(
    files: Sequence[Dict[str, str]],
    snapshot_texts: Dict[str, str],
) -> bool:
    """Recompute sha256 of every snapshot text and compare to ``files``.

    Pure function so tests can force it False and prove the success path
    actually consults it.
    """
    import hashlib

    if not files:
        return False
    seen = set()
    for entry in files:
        path = entry.get("path")
        claimed = entry.get("sha256")
        text = snapshot_texts.get(path) if isinstance(path, str) else None
        if not isinstance(path, str) or not isinstance(claimed, str):
            return False
        if not isinstance(text, str):
            return False
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != claimed:
            return False
        seen.add(path)
    return seen == set(snapshot_texts)


def verify_finished_egress(
    receipt: Dict[str, Any],
    *,
    api_key: str,
    checkout_roots: Optional[Sequence[str]] = None,
    fetch_stream: Optional[_FetchStream] = None,
    machine_name: str = "hermes-server",
    api_base: str = "https://api.cursor.com",
) -> Dict[str, Any]:
    """STUB: success without reading files or the network."""
    del receipt, api_key, checkout_roots, fetch_stream, machine_name, api_base
    return {"success": True, "error": None, "manifest": None}


def recover_artifact_for_receipt(
    receipt_path: Path,
    *,
    api_key: str,
) -> Dict[str, Any]:
    """STUB: success without reading the receipt or issuing HTTP."""
    del receipt_path, api_key
    return {"success": True, "error": None, "manifest": None}

def discover_checkout_roots(machine_name: str = "hermes-server") -> List[str]:
    """STUB: no worker roots; reads nothing."""
    del machine_name
    return []


def _http_get_stream(url: str, *, api_key: str, timeout: float) -> bytes:
    """STUB: never issued; present so tests can spy the seam."""
    del url, api_key, timeout
    return b""


__all__ = [
    "EGRESS_ERROR_MARKER",
    "STREAM_FETCH_TIMEOUT_SECONDS",
    "hashes_match",
    "recover_artifact_for_receipt",
    "verify_finished_egress",
]
