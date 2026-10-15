"""Policy routing transport, failover orchestration, and per-attempt JEV capture."""

from __future__ import annotations

import json
import logging
import sqlite3
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from typing import Any, Optional
from urllib.parse import urlsplit

try:
    from plugins.llm_usage_proxy import routing as _routing
except ImportError:
    import routing as _routing  # type: ignore

logger = logging.getLogger("llm-usage-proxy")


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
    key_store: Any
    plan: Any
    routing_store: Any
    policy_caller: Optional[str]
    caller: Optional[str]
    chat_type: Optional[str]
    chat_id: Optional[str]
    chat_name: Optional[str]
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


def orchestrate_policy_proxy(handler, state: PolicyProxyState) -> None:
    from plugins.llm_usage_proxy import server as _srv

    self = handler
    st = state
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

    def _sync_st() -> None:
        st.conn = conn
        st.status_code = status_code
        st.request_id = request_id
        st.scanner = scanner
        st.outcome = outcome
        st.headers_sent = headers_sent
        st.effective_upstream = effective_upstream
        st.effective_model = effective_model
        st.effective_body = effective_body
        st.attempts = attempts
        st.capture = capture
        st.capture_ts = capture_ts
        st.final_row_suppressed = final_row_suppressed

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
        conn, resp = _open_and_send(cand_split, cand_target, cand_body, cand_key,
                                    cross_route=cand_route != upstream_name)
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
    if resp is None:
        # Unreachable in practice (plan_request screens unwritable
        # bodies into passthrough), but never hang a request on a
        # planning defect: refuse honestly with a ledger row.
        self._send_json(502, {"error": "no routable candidate"})
        outcome = OUTCOME_REJECTED
        status_code = 502
        _sync_st()
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
            self.store.insert(
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
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        if soonest is not None:
            self.send_header("Retry-After", str(max(1, int(soonest))))
        self.send_header("Content-Length", str(len(body_503)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_503)
        self._record_routing_decision(
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
        _sync_st()
        return
    if plan.evaluated:
        self._record_routing_decision(
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
    self.send_response_only(resp.status, resp.reason)
    for name, value in resp_headers:
        self.send_header(name, value)
    framing_known = content_length is not None and not chunked_upstream
    if framing_known:
        self.send_header("Content-Length", str(content_length))
    else:
        self.send_header("Transfer-Encoding", "chunked")
    self.end_headers()
    headers_sent = True
    relay_body = (
        self.command != "HEAD"
        and resp.status >= 200
        and resp.status not in NO_BODY_STATUSES
    )
    if not relay_body:
        resp.close()
        outcome = OUTCOME_COMPLETED
    else:
        outcome = self._relay_response(resp, scanner, framing_known)
    _sync_st()

