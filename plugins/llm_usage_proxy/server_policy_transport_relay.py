"""Policy transport: post-loop response handling and relay."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Optional

try:
    from plugins.llm_usage_proxy import routing as _routing
except ImportError:
    import routing as _routing  # type: ignore

from plugins.llm_usage_proxy.server_policy_context import PolicyProxyState

logger = logging.getLogger("llm-usage-proxy")


def _sync_transport_state(st: PolicyProxyState, **fields) -> None:
    for key, val in fields.items():
        setattr(st, key, val)


def handle_upstream_response(handler, st: PolicyProxyState, resp) -> None:
    from plugins.llm_usage_proxy import server as _srv

    started = st.started
    upstream_name = st.upstream_name
    routed_path = st.routed_path
    model = st.model
    plan = st.plan
    routing_store = st.routing_store
    policy_caller = st.policy_caller
    caller = st.caller
    chat_type = st.chat_type
    chat_id = st.chat_id
    chat_name = st.chat_name
    conn = st.conn
    status_code = st.status_code
    request_id = st.request_id
    scanner = st.scanner
    outcome = st.outcome
    headers_sent = st.headers_sent
    effective_upstream = st.effective_upstream
    effective_model = st.effective_model
    effective_body = st.effective_body
    attempts = st.attempts
    capture = st.capture
    capture_ts = st.capture_ts
    final_row_suppressed = st.final_row_suppressed

    POLICY_FAILOVER_STATUS = _srv.POLICY_FAILOVER_STATUS
    REQUEST_ID_HEADERS = _srv.REQUEST_ID_HEADERS
    HOP_BY_HOP = _srv.HOP_BY_HOP
    NO_BODY_STATUSES = _srv.NO_BODY_STATUSES
    OUTCOME_COMPLETED = _srv.OUTCOME_COMPLETED
    OUTCOME_REJECTED = _srv.OUTCOME_REJECTED
    USAGE_MISSING = _srv.USAGE_MISSING
    UsageScanner = _srv.UsageScanner

    if resp is None:
        # Unreachable in practice (plan_request screens unwritable
        # bodies into passthrough), but never hang a request on a
        # planning defect: refuse honestly with a ledger row.
        handler._send_json(502, {"error": "no routable candidate"})
        outcome = OUTCOME_REJECTED
        status_code = 502
        _sync_transport_state(
            st,
            conn=conn,
            status_code=status_code,
            request_id=request_id,
            scanner=scanner,
            outcome=outcome,
            headers_sent=headers_sent,
            effective_upstream=effective_upstream,
            effective_model=effective_model,
            effective_body=effective_body,
            attempts=attempts,
            capture=capture,
            capture_ts=capture_ts,
            final_row_suppressed=final_row_suppressed,
        )
        return
    if (
        plan.routed
        and resp is not None
        and resp.status == POLICY_FAILOVER_STATUS
    ):
        # Every permitted candidate answered 429 — INCLUDING the
        # singleton case (a one-entry policy, or every other entry
        # skipped at planning): the last candidate still persists its
        # bounded cooldown, or it would be hammered on every request,
        # and the client gets a useful exhausted response instead of
        # a bare provider 429.
        now = time.time()
        retry_after_header = resp.getheader("Retry-After")
        cooldown_seconds = (
            _routing.parse_retry_after(retry_after_header, now)
            or _routing.DEFAULT_COOLDOWN_SEC
        )
        if routing_store is not None:
            routing_store.mark_cooldown(
                effective_upstream,
                effective_model,
                cooldown_seconds,
                reason="http_429",
                retry_after=retry_after_header,
                now=now,
            )
        last_request_id = None
        for header in REQUEST_ID_HEADERS:
            value = resp.getheader(header)
            if value:
                last_request_id = value
                break
        attempts.append(
            {
                "route": effective_upstream,
                "model": effective_model,
                "status": 429,
                "cooldown_seconds": cooldown_seconds,
            }
        )
        resp.close()
        conn.close()
        conn = None
        try:
            handler.store.insert(
                upstream=effective_upstream,
                model=effective_model,
                usage=None,
                status_code=429,
                latency_ms=int((time.monotonic() - started) * 1000),
                path=routed_path or "/",
                request_id=last_request_id,
                outcome=OUTCOME_COMPLETED,
                usage_complete=USAGE_MISSING,
                caller=caller,
                chat_type=chat_type,
                chat_id=chat_id,
                chat_name=chat_name,
            )
        except sqlite3.Error as exc:
            logger.error(
                "failed to record usage row: %s", _srv.redact_text(str(exc))
            )
        # The exhausted Retry-After is scoped to THIS request's
        # relevant candidates — cooldowns this plan already skipped
        # plus the ones its own 429 attempts just marked — never the
        # global minimum across every caller/route, which would both
        # leak unrelated state and quote a retry time belonging to a
        # route this policy cannot use.
        soonest = _routing.scoped_retry_after(plan.skips, attempts)
        payload = {
            "error": "all routed upstreams are rate-limited",
            "caller": policy_caller,
            "family": plan.family,
            "attempts": attempts,
            "skips": plan.skips,
        }
        if soonest is not None:
            payload["retry_after_seconds"] = soonest
        body_503 = json.dumps(payload).encode("utf-8")
        handler.send_response(503)
        handler.send_header("Content-Type", "application/json")
        if soonest is not None:
            handler.send_header("Retry-After", str(max(1, int(soonest))))
        handler.send_header("Content-Length", str(len(body_503)))
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body_503)
        handler._record_routing_decision(
            plan=plan,
            arrived_route=upstream_name,
            arrived_model=model,
            selected_route=None,
            selected_model=None,
            reason="exhausted",
            attempts=attempts,
        )
        # The finally below must not add a second row for this
        # request: the per-attempt rows above are the whole story.
        final_row_suppressed = True
        outcome = OUTCOME_REJECTED
        status_code = 503
        _sync_transport_state(
            st,
            conn=conn,
            status_code=status_code,
            request_id=request_id,
            scanner=scanner,
            outcome=outcome,
            headers_sent=headers_sent,
            effective_upstream=effective_upstream,
            effective_model=effective_model,
            effective_body=effective_body,
            attempts=attempts,
            capture=capture,
            capture_ts=capture_ts,
            final_row_suppressed=final_row_suppressed,
        )
        return
    if plan.evaluated:
        handler._record_routing_decision(
            plan=plan,
            arrived_route=upstream_name,
            arrived_model=model,
            selected_route=effective_upstream,
            selected_model=effective_model,
            reason=("failover" if attempts else plan.reason),
            attempts=attempts,
        )
    status_code = resp.status
    for header in REQUEST_ID_HEADERS:
        value = resp.getheader(header)
        if value:
            request_id = value
            break
    # Response hop-by-hop set: static list plus any header the
    # upstream's Connection header nominates for dropping.
    resp_connection_tokens = {
        token.strip().lower()
        for token in (resp.getheader("Connection") or "").split(",")
        if token.strip()
    }
    resp_headers = []
    raw_length = resp.getheader("Content-Length")
    try:
        content_length: Optional[int] = (
            int(raw_length) if raw_length is not None else None
        )
    except ValueError:
        content_length = None
    chunked_upstream = (
        "chunked"
        in (resp.getheader("Transfer-Encoding") or "").lower()
    )
    for name, value in resp.getheaders():
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered in resp_connection_tokens:
            continue
        if lowered == "content-length":
            continue
        resp_headers.append((name, value))
    content_type = resp.getheader("Content-Type") or ""
    content_encoding = resp.getheader("Content-Encoding") or ""
    scanner = UsageScanner(content_type, content_encoding, capture)
    handler.send_response_only(resp.status, resp.reason)
    for name, value in resp_headers:
        handler.send_header(name, value)
    framing_known = content_length is not None and not chunked_upstream
    if framing_known:
        handler.send_header("Content-Length", str(content_length))
    else:
        handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    headers_sent = True
    relay_body = (
        handler.command != "HEAD"
        and resp.status >= 200
        and resp.status not in NO_BODY_STATUSES
    )
    if not relay_body:
        resp.close()
        outcome = OUTCOME_COMPLETED
    else:
        outcome = handler._relay_response(resp, scanner, framing_known)
    _sync_transport_state(
        st,
        conn=conn,
        status_code=status_code,
        request_id=request_id,
        scanner=scanner,
        outcome=outcome,
        headers_sent=headers_sent,
        effective_upstream=effective_upstream,
        effective_model=effective_model,
        effective_body=effective_body,
        attempts=attempts,
        capture=capture,
        capture_ts=capture_ts,
        final_row_suppressed=final_row_suppressed,
    )
