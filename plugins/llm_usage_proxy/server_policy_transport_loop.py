"""Policy transport: candidate loop (send, key-retry, mid-429 failover)."""

from __future__ import annotations

import logging
import sqlite3
import ssl
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from http.client import HTTPConnection, HTTPSConnection

try:
    from plugins.llm_usage_proxy import routing as _routing
except ImportError:
    import routing as _routing  # type: ignore

from plugins.llm_usage_proxy.server_policy_context import PolicyProxyState

logger = logging.getLogger("llm-usage-proxy")


def _sync_transport_state(st: PolicyProxyState, **fields) -> None:
    for key, val in fields.items():
        setattr(st, key, val)


def run_candidate_loop(handler, st: PolicyProxyState):
    from plugins.llm_usage_proxy import server as _srv
    from plugins.llm_usage_proxy.server_policy import prepare_jev_capture_for_attempt

    self = handler
    started = st.started
    upstream_name = st.upstream_name
    rest = st.rest
    query = st.query
    routed_path = st.routed_path
    body = st.body
    model = st.model
    bearer_consumed = st.bearer_consumed
    x_api_key_consumed = st.x_api_key_consumed
    key_store = st.key_store
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

    CONNECT_TIMEOUT_SEC = _srv.CONNECT_TIMEOUT_SEC
    STREAM_TIMEOUT_SEC = _srv.STREAM_TIMEOUT_SEC
    MAX_POLICY_ATTEMPTS = _srv.MAX_POLICY_ATTEMPTS
    MAX_POLICY_SENDS = _srv.MAX_POLICY_SENDS
    KEY_RETRY_STATUSES = _srv.KEY_RETRY_STATUSES
    POLICY_FAILOVER_STATUS = _srv.POLICY_FAILOVER_STATUS
    REQUEST_ID_HEADERS = _srv.REQUEST_ID_HEADERS
    HOP_BY_HOP = _srv.HOP_BY_HOP
    NO_BODY_STATUSES = _srv.NO_BODY_STATUSES
    OUTCOME_COMPLETED = _srv.OUTCOME_COMPLETED
    OUTCOME_REJECTED = _srv.OUTCOME_REJECTED
    USAGE_MISSING = _srv.USAGE_MISSING
    UsageScanner = _srv.UsageScanner
    apply_upstream_auth = _srv.apply_upstream_auth
    _upstream_request_target = _srv._upstream_request_target
    key_fingerprint = _srv.key_fingerprint
    MAX_REQUEST_BODY_BYTES = _srv.MAX_REQUEST_BODY_BYTES

    def _open_and_send(
        cand_split: Any,
        cand_target: str,
        cand_body: Optional[bytes],
        key: Optional[str],
        *,
        cross_route: bool,
    ) -> tuple[HTTPConnection, Any]:
        nonlocal conn
        if cand_split.scheme == "https":
            connection: HTTPConnection = HTTPSConnection(
                cand_split.netloc,
                timeout=CONNECT_TIMEOUT_SEC,
                context=ssl.create_default_context(),
            )
        else:
            connection = HTTPConnection(
                cand_split.netloc, timeout=CONNECT_TIMEOUT_SEC
            )
        # Bound to the outer name before the request so the finally
        # below closes this connection even if connect/request raises.
        conn = connection
        headers = self._forward_headers(
            cand_split.netloc,
            drop_authorization=bearer_consumed,
            drop_x_api_key=x_api_key_consumed,
            drop_account_bound=cross_route,
        )
        if key is not None:
            headers = apply_upstream_auth(headers, key)
        body_for_request = cand_body if cand_body is not None else (
            b"" if self.command in ("POST", "PUT", "PATCH") else None
        )
        if cand_body is not None:
            headers.append(("Content-Length", str(len(cand_body))))
        # http.client applies the socket timeout to connects AND reads;
        # stretch it once connected so long streams are not cut short.
        connection.request(
            self.command, cand_target, body=body_for_request, headers=dict(headers)
        )
        if connection.sock is not None:
            connection.sock.settimeout(STREAM_TIMEOUT_SEC)
        return connection, connection.getresponse()
    resp = None
    sends = 0  # actual HTTP sends — the enforced attempt bound
    candidates = plan.candidates[:MAX_POLICY_ATTEMPTS]
    for attempt_index, candidate in enumerate(candidates):
        cand_route = str(candidate["route"])
        cand_model = candidate.get("model")
        if sends >= MAX_POLICY_SENDS:
            # The documented attempt bound counts ACTUAL HTTP SENDS,
            # alternate-key rotation included — a long policy plus
            # multi-key routes must never hammer the fleet on one
            # request. Whatever candidates remain are skipped here.
            attempts.append(
                {"route": cand_route, "model": cand_model,
                 "reason": "attempt_bound"}
            )
            break
        cand_base = self.upstreams[cand_route]
        cand_split = urlsplit(cand_base)
        cross_route_attempt = cand_route != upstream_name
        cand_target = _upstream_request_target(
            cand_base,
            rest,
            query,
            policy_cross_route_chat_completions=(
                cross_route_attempt
                and plan.family == _routing.FAMILY_CHAT_COMPLETIONS
            ),
        )
        cand_body = body
        if cand_model is not None and body:
            rewritten_body = _routing.rewrite_request_model(body, cand_model)
            if rewritten_body is None:
                # plan_request already screened this; belt and braces:
                # never send a request whose model the policy claims
                # to have chosen but did not rewrite.
                attempts.append(
                    {"route": cand_route, "model": cand_model,
                     "reason": "unwritable_body"}
                )
                continue
            cand_body = rewritten_body
        cand_key: Optional[str] = None
        cand_position = -1
        cand_key_count = 0
        if key_store is not None:
            route_keys = key_store.route_keys(cand_route)
            cand_key_count = len(route_keys)
            if (
                route_keys
                and self.rotator is not None
                and not plan.state_auth_passthrough
            ):
                # state_auth_passthrough: a policy-controlled opaque
                # continuation served under the caller's OWN presented
                # credential. A stored key is route-bound, never
                # account-bound (and the rotator cursor moves with
                # unrelated traffic), so substituting one here could
                # re-present provider-owned state under a different
                # account than the one that created it — the managed
                # key stays out of this request entirely.
                cand_key, cand_position = self.rotator.issue(cand_route)
        # The current attempt IS the effective request from here on:
        # if the send itself fails, the ledger row and the capture
        # must name this candidate, not the arriving route.
        effective_upstream = cand_route
        if cand_model is not None:
            effective_model = cand_model
        effective_body = cand_body
        # Opt-in jev body capture: the sink exists only for requests
        # that passed the gate, so everything else streams through
        # untouched. Decided on the EFFECTIVE route/model (the
        # candidate being attempted) so a rerouted request is
        # captured only when it truly is jev traffic. Set up BEFORE
        # the send: a refused connection still gets its capture row
        # (request body + incomplete marker). ``ts`` is the request's
        # own start time; the row itself is written at finish().
        capture, capture_ts = prepare_jev_capture_for_attempt(
            self.capture_jev_bodies,
            effective_upstream,
            effective_model,
            MAX_REQUEST_BODY_BYTES,
            _srv.should_capture_jev_body,
            _srv.JevBodyCapture,
        )
        _sync_transport_state(
            st,
            conn=conn,
            effective_upstream=effective_upstream,
            effective_model=effective_model,
            effective_body=effective_body,
            capture=capture,
            capture_ts=capture_ts,
        )
        conn, resp = _open_and_send(
            cand_split, cand_target, cand_body, cand_key,
            cross_route=cand_route != upstream_name,
        )
        st.conn = conn
        sends += 1
        if (
            cand_key is not None
            and resp.status in KEY_RETRY_STATUSES
            and cand_key_count > 1
            and self.rotator is not None
            and sends < MAX_POLICY_SENDS
            and plan.reason != "state_pinned"
        ):
            # One retry per request with the route's next key: a rotated-out
            # or revoked credential should not surface to the client, and a
            # rate-limited one gets a second key before the client backs off.
            # Never for a state-pinned request: re-presenting provider-owned
            # opaque state (signed thinking, encrypted reasoning) under a
            # SECOND credential could carry it across an account boundary —
            # the raw 401/429 relays instead.
            retry_key, retry_position = self.rotator.step(
                cand_route, cand_position
            )
            if retry_key is not None:
                logger.info(
                    "upstream %s answered %s with key %s; retrying with its next key",
                    cand_route,
                    resp.status,
                    key_fingerprint(cand_key),
                )
                conn.close()
                conn, resp = _open_and_send(
                    cand_split, cand_target, cand_body, retry_key,
                    cross_route=cand_route != upstream_name,
                )
                sends += 1
        # Failover: only a 429 received BEFORE anything was sent
        # downstream, only for a genuinely policy-routed request
        # (plan.routed — never legacy/policy-disabled/non-inference/
        # state-pinned passthrough), and only while candidates
        # remain. The LAST candidate's 429 is the exhausted branch
        # below — it persists its cooldown too, so a one-entry
        # policy is not hammered on every request. Auth failures,
        # 400s, 5xx and network errors never mark a cooldown and
        # never retry — the response (or the honest 502) goes to the
        # client as it always did.
        last_candidate = attempt_index == len(candidates) - 1
        if (
            plan.routed
            and resp.status == POLICY_FAILOVER_STATUS
            and not last_candidate
        ):
            now = time.time()
            retry_after_header = resp.getheader("Retry-After")
            cooldown_seconds = _routing.parse_retry_after(
                retry_after_header, now
            )
            if cooldown_seconds is None:
                cooldown_seconds = _routing.DEFAULT_COOLDOWN_SEC
            failed_model = cand_model or model
            if routing_store is not None:
                routing_store.mark_cooldown(
                    cand_route,
                    failed_model,
                    cooldown_seconds,
                    reason="http_429",
                    retry_after=retry_after_header,
                    now=now,
                )
            # The failed attempt is a real HTTP attempt: it gets its
            # own ledger row (status 429, usage honestly missing).
            attempt_request_id = None
            for header in REQUEST_ID_HEADERS:
                value = resp.getheader(header)
                if value:
                    attempt_request_id = value
                    break
            resp.close()
            conn.close()
            conn = None
            # Drop the closed response: a later candidate's
            # unwritable-body skip must not see this stale 429 and
            # re-enter the exhausted branch for a route that already
            # recorded its attempt.
            resp = None
            attempts.append(
                {
                    "route": cand_route,
                    "model": failed_model,
                    "status": 429,
                    "cooldown_seconds": cooldown_seconds,
                }
            )
            try:
                self.store.insert(
                    upstream=cand_route,
                    model=failed_model,
                    usage=None,
                    status_code=429,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    path=routed_path or "/",
                    request_id=attempt_request_id,
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
            continue
        break
    _sync_transport_state(
        st,
        conn=conn,
        effective_upstream=effective_upstream,
        effective_model=effective_model,
        effective_body=effective_body,
        attempts=attempts,
        capture=capture,
        capture_ts=capture_ts,
    )
    return resp
