"""Policy transport orchestration (loop → relay)."""

from __future__ import annotations

from plugins.llm_usage_proxy.server_policy_context import PolicyProxyState
from plugins.llm_usage_proxy.server_policy_transport_loop import run_candidate_loop
from plugins.llm_usage_proxy.server_policy_transport_relay import handle_upstream_response


def run_policy_transport(handler, state: PolicyProxyState) -> None:
    resp = run_candidate_loop(handler, state)
    handle_upstream_response(handler, state, resp)
