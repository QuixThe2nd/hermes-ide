"""Shared per-request state for policy-routed proxy handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PolicyProxyState:
    started: float
    upstream_name: str
    rest: str
    query: str
    routed_path: str
    body: Optional[bytes]
    model: Any
    bearer_consumed: bool
    x_api_key_consumed: bool
    has_independent_auth: bool
    key_store: Any
    routing_store: Any
    policy_caller: Optional[str]
    caller: Optional[str]
    chat_type: Optional[str]
    chat_id: Optional[str]
    chat_name: Optional[str]
    plan: Any = None
    conn: Any = None
    status_code: Optional[int] = None
    request_id: Optional[str] = None
    scanner: Any = None
    outcome: str = ""
    headers_sent: bool = False
    effective_upstream: str = ""
    effective_model: Any = None
    effective_body: Optional[bytes] = None
    attempts: list = field(default_factory=list)
    capture: Any = None
    capture_ts: Optional[str] = None
    final_row_suppressed: bool = False
