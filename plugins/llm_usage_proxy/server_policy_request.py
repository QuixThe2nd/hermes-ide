"""Parse proxy requests, authenticate callers, and finalize ledger rows."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from http.client import HTTPException
from typing import Optional

from plugins.llm_usage_proxy.server_policy_context import PolicyProxyState


def load_proxy_request(handler) -> Optional[PolicyProxyState]:
    from plugins.llm_usage_proxy import server as _srv

    started = time.monotonic()
    upstream_name, rest, query = _srv._split_proxy_path(handler.path)
    if not upstream_name or upstream_name not in handler.upstreams:
        handler.close_connection = True
        handler._send_json(404, {"error": f"unknown upstream '{upstream_name or ''}'"})
        return None
    routed_path = rest

    body = None
    try:
        body = handler._read_request_body()
    except (HTTPException, OSError, ValueError) as exc:
        handler.close_connection = True
        handler._send_json(400, {"error": _srv.redact_text(str(exc))})
        return None

    model = _srv.extract_request_model(body) if body else None
    if body:
        rewritten = _srv.maybe_inject_stream_options(body, routed_path)
        if rewritten is not None:
            body = rewritten

    label = handler._presented_caller_label()
    ua_caller = _srv.caller_label_from_user_agent(handler.headers.get("User-Agent"))
    caller: Optional[str] = None
    chat_type, chat_id, chat_name = handler._presented_chat_identity()
    key_store = handler.key_store if handler.manage_keys else None
    presented_token = handler._presented_caller_token() or ""
    policy_caller: Optional[str] = None
    if key_store is not None:
        policy_caller = key_store.policy_caller_for_token(presented_token)
    if key_store is not None:
        caller, auth_error = handler._authenticate_caller(key_store)
        if auth_error is not None:
            handler._send_json(401, {"error": auth_error})
            handler._record_rejection(
                upstream=upstream_name,
                path=routed_path,
                model=model,
                status=401,
                started=started,
                caller=caller or label,
                chat_type=chat_type,
                chat_id=chat_id,
                chat_name=chat_name,
            )
            return None
    if caller is None:
        caller = policy_caller or label or ua_caller

    bearer_value = _srv.bearer_token(handler.headers.get("Authorization"))
    x_api_key_value = (handler.headers.get("x-api-key") or "").strip()

    def _is_local_credential(value: Optional[str]) -> bool:
        return bool(
            value
            and key_store is not None
            and key_store.caller_for_token(value) is not None
        )

    bearer_consumed = _is_local_credential(bearer_value)
    x_api_key_consumed = _is_local_credential(x_api_key_value)
    has_independent_auth = bool(
        (x_api_key_value and not x_api_key_consumed)
        or (
            (handler.headers.get("Authorization") or "").strip()
            and not bearer_consumed
        )
    )

    if handler.manage_keys and caller is None:
        handler._send_json(
            401,
            {
                "error": (
                    "unattributed request: send a caller token, an"
                    " 'X-Usage-Caller: <label>' header, or a known harness"
                    " User-Agent"
                )
            },
        )
        handler._record_rejection(
            upstream=upstream_name,
            path=routed_path,
            model=model,
            status=401,
            started=started,
            caller=_srv.UNATTRIBUTED_CALLER,
            chat_type=chat_type,
            chat_id=chat_id,
            chat_name=chat_name,
        )
        return None

    return PolicyProxyState(
        started=started,
        upstream_name=upstream_name,
        rest=rest,
        query=query,
        routed_path=routed_path or "",
        body=body,
        model=model,
        bearer_consumed=bearer_consumed,
        x_api_key_consumed=x_api_key_consumed,
        has_independent_auth=has_independent_auth,
        key_store=key_store,
        routing_store=handler.routing_store,
        policy_caller=policy_caller,
        caller=caller,
        chat_type=chat_type,
        chat_id=chat_id,
        chat_name=chat_name,
    )


def finalize_proxy_request(handler, st: PolicyProxyState) -> None:
    from plugins.llm_usage_proxy import server as _srv

    if st.conn is not None:
        st.conn.close()
    if st.scanner is not None:
        st.scanner.finish()
    latency_ms = int((time.monotonic() - st.started) * 1000)
    usage = st.scanner.usage if st.scanner is not None else None
    completeness = (
        st.scanner.completeness if st.scanner is not None else _srv.USAGE_MISSING
    )
    if st.outcome != _srv.OUTCOME_COMPLETED and completeness == _srv.USAGE_FINAL:
        completeness = _srv.USAGE_PARTIAL
    if not st.final_row_suppressed:
        try:
            handler.store.insert(
                upstream=st.effective_upstream,
                model=(st.scanner.model if st.scanner else None) or st.effective_model,
                usage=usage,
                status_code=st.status_code,
                latency_ms=latency_ms,
                path=st.routed_path or "/",
                request_id=st.request_id,
                outcome=st.outcome,
                usage_complete=completeness,
                caller=st.caller,
                chat_type=st.chat_type,
                chat_id=st.chat_id,
                chat_name=st.chat_name,
            )
        except sqlite3.Error as exc:
            _srv.logger.error("failed to record usage row: %s", _srv.redact_text(str(exc)))
    if st.capture is not None:
        try:
            handler.store.insert_jev_body(
                ts=st.capture_ts
                or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                upstream=st.effective_upstream,
                model=st.effective_model,
                path=st.routed_path or "/",
                request_id=st.request_id,
                status_code=st.status_code,
                latency_ms=latency_ms,
                capture_state=st.capture.state(st.outcome),
                request_body=st.capture.request_text(st.effective_body),
                response_body=st.capture.response_text(st.outcome),
            )
        except sqlite3.Error as exc:
            _srv.logger.error(
                "failed to record jev body capture: %s",
                _srv.redact_text(str(exc)),
            )
