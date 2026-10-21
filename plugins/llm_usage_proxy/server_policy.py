"""Facade for policy-routed proxy requests (plan → transport → ledger finalize)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from plugins.llm_usage_proxy.server_policy_context import PolicyProxyState
from plugins.llm_usage_proxy.server_policy_plan import apply_routing_plan
from plugins.llm_usage_proxy.server_policy_request import finalize_proxy_request, load_proxy_request
from plugins.llm_usage_proxy.server_policy_transport import run_policy_transport

__all__ = (
    "PolicyProxyState",
    "prepare_jev_capture_for_attempt",
    "execute_proxy_request",
    "orchestrate_policy_proxy",
)


def prepare_jev_capture_for_attempt(
    capture_enabled: bool,
    upstream: str,
    model: Any,
    cap_bytes: int,
    should_capture_fn,
    jev_capture_cls,
) -> tuple[Any, Optional[str]]:
    """Per-attempt JEV capture sink — never inherited across failover candidates."""
    if not should_capture_fn(capture_enabled, upstream, model):
        return None, None
    return (
        jev_capture_cls(cap_bytes),
        datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    )


def execute_proxy_request(handler) -> None:
    """Thin orchestration entry used by UsageProxyHandler._proxy."""
    from http.client import HTTPException
    import ssl

    from plugins.llm_usage_proxy import server as _srv

    st = load_proxy_request(handler)
    if st is None:
        return
    if apply_routing_plan(handler, st):
        return

    st.effective_upstream = st.upstream_name
    st.effective_model = st.model
    st.effective_body = st.body
    st.outcome = _srv.OUTCOME_UPSTREAM_ERROR

    try:
        try:
            run_policy_transport(handler, st)
        except _srv._ClientGoneError:
            st.outcome = _srv.OUTCOME_ABORTED
            handler.close_connection = True
        except (OSError, HTTPException, ssl.SSLError, ValueError) as exc:
            st.outcome = _srv.OUTCOME_UPSTREAM_ERROR
            detail = _srv.redact_text(f"{type(exc).__name__}: {exc}")
            _srv.logger.warning("upstream %s failed: %s", st.upstream_name, detail)
            if not st.headers_sent:
                try:
                    handler._send_json(502, {"error": f"upstream unavailable: {detail}"})
                except OSError:
                    handler.close_connection = True
            else:
                handler.close_connection = True
    finally:
        finalize_proxy_request(handler, st)


def orchestrate_policy_proxy(handler, state: PolicyProxyState) -> None:
    """Transport-only phase (legacy entry for call sites that already planned)."""
    run_policy_transport(handler, state)
