"""Routing plan resolution and pre-transport policy rejections."""

from __future__ import annotations

try:
    from plugins.llm_usage_proxy import routing as _routing
except ImportError:
    import routing as _routing  # type: ignore

from plugins.llm_usage_proxy.server_policy_context import PolicyProxyState


def build_routing_plan(handler, st: PolicyProxyState):
    routing_store = st.routing_store
    if routing_store is None:
        return _routing.RoutingPlan(
            candidates=[{"route": st.upstream_name, "model": None}],
            reason="no_policy_caller",
            family=_routing.family_for_path(st.routed_path),
        )
    return _routing.plan_request(
        routing_store,
        policy_caller=st.policy_caller,
        arriving_route=st.upstream_name,
        rest=st.routed_path,
        body=st.body,
        configured_routes=set(handler.upstreams),
        route_key_counts=(
            {
                name: (
                    len(st.key_store.route_keys(name))
                    or (1 if st.key_store.oauth_provider_for(name) else 0)
                )
                for name in handler.upstreams
            }
            if st.key_store is not None
            else {}
        ),
        route_families=handler.route_families,
        arriving_auth_independent=st.has_independent_auth,
    )


def _reject_empty_plan(handler, st: PolicyProxyState) -> bool:
    from plugins.llm_usage_proxy import server as _srv

    plan = st.plan
    retry_after = _routing.scoped_retry_after(plan.skips, [])
    headers = (
        [("Retry-After", str(max(1, int(retry_after))))]
        if retry_after is not None
        else None
    )
    if plan.reason == "state_unreadable":
        detail = plan.skips[0].get("detail") if plan.skips else None
        status, payload = 503, {
            "error": "routing state is unreadable — refusing to"
            " silently bypass the saved routing intent",
            "detail": detail,
            "action": "inspect or repair the routing state file"
            " (or remove it to start over), then retry",
        }
        headers = None
    elif plan.reason == "malformed_policy":
        detail = plan.skips[0].get("detail") if plan.skips else None
        status, payload = 503, {
            "error": f"the saved routing policy for '{st.policy_caller}'"
            " is malformed — refusing to silently bypass it",
            "detail": detail,
            "action": "re-save the policy with `hermes llm_usage_proxy"
            " routing set` or drop it with `routing remove`",
        }
        headers = None
    elif plan.reason == "ambiguous_state":
        detail = plan.skips[0].get("detail") if plan.skips else None
        status, payload = 409, {
            "error": (
                f"this {plan.family} request carries provider-owned"
                " continuation state, and the routing policy for"
                f" '{st.policy_caller}' can route {plan.family} traffic to"
                " another endpoint — which endpoint owns this state is"
                " not verifiable, and opaque continuations never cross"
                " provider boundaries"
            ),
            "detail": detail,
            "action": (
                "start a fresh conversation without the continuation"
                " state, or restrict this policy's entries for the"
                f" {plan.family} family to the route the conversation"
                " lives on (or disable the policy) so state ownership"
                " stays verifiable"
            ),
        }
        headers = None
    elif plan.reason == "unverifiable_state_auth":
        detail = plan.skips[0].get("detail") if plan.skips else None
        status, payload = 409, {
            "error": (
                f"this {plan.family} request carries provider-owned"
                " continuation state and the routing policy for"
                f" '{st.policy_caller}' controls {plan.family} routing,"
                " but the arriving route's managed keys are bound to"
                " the route, not to an account — which account would"
                " receive this state under a substituted key is not"
                " verifiable, and opaque continuations never cross"
                " account boundaries"
            ),
            "detail": detail,
            "action": (
                "present the provider credential that owns this"
                " conversation (Authorization/x-api-key) alongside the"
                f" {_srv.CALLER_TOKEN_HEADER} header so the continuation is"
                " served under verifiable auth, or disable/restrict"
                " the policy so it no longer controls this family's"
                " opaque continuations, or start a fresh conversation"
                " without the continuation state"
            ),
        }
        headers = None
    else:
        status, payload = 503, {
            "error": "no usable routed upstream: every policy entry for"
            " this API family is cooling down or unavailable",
            "caller": st.policy_caller,
            "family": plan.family,
            "skips": plan.skips,
        }
        if retry_after is not None:
            payload["retry_after_seconds"] = retry_after
    handler._send_json(status, payload, headers=headers)
    handler._record_routing_decision(
        plan=plan,
        arrived_route=st.upstream_name,
        arrived_model=st.model,
        selected_route=None,
        selected_model=None,
        reason=plan.reason,
        attempts=[],
    )
    handler._record_rejection(
        upstream=st.upstream_name,
        path=st.routed_path,
        model=st.model,
        status=status,
        started=st.started,
        caller=st.caller,
        chat_type=st.chat_type,
        chat_id=st.chat_id,
        chat_name=st.chat_name,
    )
    return True


def _reject_local_token_without_route_key(handler, st: PolicyProxyState) -> bool:
    from plugins.llm_usage_proxy import server as _srv

    plan = st.plan
    if not (
        (st.bearer_consumed or st.x_api_key_consumed)
        and not plan.routed
        and not st.has_independent_auth
        and st.key_store is not None
        and not st.key_store.route_keys(st.upstream_name)
        and not st.key_store.oauth_provider_for(st.upstream_name)
    ):
        return False
    handler._send_json(
        401,
        {
            "error": "a presented credential is a local caller token,"
            " not provider auth, and this route holds no stored"
            " provider key",
            "action": "present the caller token via the"
            f" {_srv.CALLER_TOKEN_HEADER} header and provider auth via"
            " Authorization/x-api-key, or store provider keys with"
            " `hermes llm_usage_proxy keys set`",
        },
    )
    if plan.evaluated:
        handler._record_routing_decision(
            plan=plan,
            arrived_route=st.upstream_name,
            arrived_model=st.model,
            selected_route=None,
            selected_model=None,
            reason=plan.reason,
            attempts=[],
        )
    handler._record_rejection(
        upstream=st.upstream_name,
        path=st.routed_path,
        model=st.model,
        status=401,
        started=st.started,
        caller=st.caller,
        chat_type=st.chat_type,
        chat_id=st.chat_id,
        chat_name=st.chat_name,
    )
    return True


def apply_routing_plan(handler, st: PolicyProxyState) -> bool:
    """Resolve plan and handle rejections. True when the request is finished."""
    st.plan = build_routing_plan(handler, st)
    if st.plan.evaluated and not st.plan.candidates:
        return _reject_empty_plan(handler, st)
    if _reject_local_token_without_route_key(handler, st):
        return True
    return False
