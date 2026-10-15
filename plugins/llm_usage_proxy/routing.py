"""Per-harness routing policies and bounded persistent 429 cooldowns.

Focused sibling of ``server.py`` (which stays the transport/ledger owner):
this module owns the *policy* topic — the persisted ordered route+model list
an authenticated policy caller is routed by, and the cooldown journal that
makes a rate-limited route+model step aside for a bounded time.

Hard invariants (see the implementation brief):

* Policy routing is armed ONLY by an authenticated policy caller — a real
  caller-token match against the key store's ``policy_callers`` namespace.
  The ``X-Usage-Caller`` label and the harness User-Agent are attribution,
  never credentials: a forged label can never arm, steer, or inspect a
  policy. The legacy caller-token gate is untouched by everything here.
* Policies are operator priority, never reshuffled by quota scores: the
  first usable entry wins, entries are tried in saved order, and nothing
  reorders them behind the operator's back.
* Cooldowns are scoped truthfully to ``(route, model)`` — a rate-limited
  model never marks its whole route (or any other route) exhausted.
* Malformed state fails CLOSED for the callers it affects, never silently:
  a corrupt file keeps its last-good in-memory view, refuses mutations
  (``RoutingStateError``) instead of being overwritten by a partial view,
  and — when no last-good exists — refuses opted-in requests visibly; a
  malformed saved policy refuses that caller's opted-in requests with an
  actionable error (``policy_error``). Legacy callers with no policy token
  are never affected, and a read never rewrites the file.
* Mutations are process-safe: an exclusive flock on a sibling lock file
  wraps the whole reload→mutate→save, so the proxy, the CLI and a web
  server (separate processes) can never overwrite each other's changes.

Like ``server.py`` this module is stdlib-only: the systemd unit executes
``server.py`` directly, and ``server.py`` imports this file both as a
package module and as a plain sibling script.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Mapping, Optional

logger = logging.getLogger("llm-usage-proxy.routing")

# ── API families ─────────────────────────────────────────────────────────────
#
# Policy routing resolves an explicit API family from the request path — never
# from a provider name — because native families are not interchangeable and
# there is no format translation in this version. Only exact inference paths
# are policy-eligible; probes, model catalogs, count_tokens and every other
# path stay legacy passthrough.

FAMILY_CHAT_COMPLETIONS = "chat_completions"
FAMILY_ANTHROPIC_MESSAGES = "anthropic_messages"
FAMILY_RESPONSES = "responses"
API_FAMILIES = frozenset(
    {FAMILY_CHAT_COMPLETIONS, FAMILY_ANTHROPIC_MESSAGES, FAMILY_RESPONSES}
)


def family_for_path(rest: str) -> Optional[str]:
    """The API family an inference path belongs to, or None when not inference.

    ``rest`` is the routed path after ``/p/<route>/`` (no query). Matching is
    on whole trailing segments so ``/v1/messages/count_tokens`` is NOT the
    messages endpoint and a path merely containing "responses" somewhere in
    the middle is not the Responses API.
    """
    segments = [seg for seg in str(rest or "").split("/") if seg]
    if not segments:
        return None
    if len(segments) >= 2 and segments[-2:] == ["chat", "completions"]:
        return FAMILY_CHAT_COMPLETIONS
    if segments[-1] == "messages" and "count_tokens" not in segments:
        return FAMILY_ANTHROPIC_MESSAGES
    if segments[-1] == "responses":
        return FAMILY_RESPONSES
    return None


# ── Endpoint API capabilities ────────────────────────────────────────────────
#
# Compatibility is a property of the ENDPOINT, never of a route slug: a route
# named "openai-something" does not make its upstream serve the Responses API,
# and the Codex subscription endpoint's Responses is not interchangeable with
# an arbitrary OpenAI-compatible deployment. This table maps the normalized
# base URL of each endpoint Hermes itself knows (the same bases routes.py
# registers) to the exact API families that endpoint serves. Anything else —
# custom gateways, one-off proxies, new providers — is declared explicitly by
# the operator via ``llm_usage_proxy.route_capabilities`` config (route name →
# family list), which overrides this table. A route with neither entry has NO
# usable capabilities: policies cannot target it (write-time rejection) and
# planning skips it, rather than guessing compatibility from its name.

KNOWN_ENDPOINT_FAMILIES: dict[str, frozenset[str]] = {
    # z.ai GLM: OpenAI-compatible chat completions on the paas bases…
    "https://api.z.ai/api/paas/v4": frozenset({FAMILY_CHAT_COMPLETIONS}),
    "https://api.z.ai/api/coding/paas/v4": frozenset({FAMILY_CHAT_COMPLETIONS}),
    # …and a separate Anthropic-protocol base.
    "https://api.z.ai/api/anthropic": frozenset({FAMILY_ANTHROPIC_MESSAGES}),
    # Kimi for Coding serves both the Anthropic messages API (what Claude
    # Code points at) and OpenAI-compatible chat completions; the legacy
    # Moonshot base is chat-completions only.
    "https://api.kimi.com/coding": frozenset(
        {FAMILY_ANTHROPIC_MESSAGES, FAMILY_CHAT_COMPLETIONS}
    ),
    "https://api.moonshot.ai/v1": frozenset({FAMILY_CHAT_COMPLETIONS}),
    # xAI serves chat completions and the Responses API on the same base.
    "https://api.x.ai/v1": frozenset({FAMILY_CHAT_COMPLETIONS, FAMILY_RESPONSES}),
    # The Codex subscription backend is Responses-only.
    "https://chatgpt.com/backend-api/codex": frozenset({FAMILY_RESPONSES}),
    # OpenRouter's OpenAI-compatible fronts (conservative: chat completions).
    "https://openrouter.ai/api/v1": frozenset({FAMILY_CHAT_COMPLETIONS}),
    "https://openrouter.ai/api/alpha": frozenset({FAMILY_CHAT_COMPLETIONS}),
}


def _normalize_base(base: str) -> str:
    """scheme://host[:port]/path, lowercased scheme+host, no trailing slash."""
    from urllib.parse import urlsplit

    split = urlsplit(str(base or "").strip())
    scheme = split.scheme.lower()
    host = (split.hostname or "").lower()
    try:
        port = split.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    default = 443 if scheme == "https" else 80
    netloc = host if port == default else f"{host}:{port}"
    return f"{scheme}://{netloc}{split.path.rstrip('/')}"


def parse_capability_overrides(raw: Any) -> dict[str, frozenset[str]]:
    """Operator-declared route capabilities, validated.

    Accepts a mapping (route name → family list or comma-separated string),
    as read from ``llm_usage_proxy.route_capabilities`` config. Raises
    ValueError on unknown family names — a typo'd capability must fail loudly
    at the boundary that reads it, never silently shrink a route's abilities.
    """
    overrides: dict[str, frozenset[str]] = {}
    if raw is None:
        return overrides
    if not isinstance(raw, Mapping):
        raise ValueError("route_capabilities must be a mapping of route → families")
    for name, value in raw.items():
        route = str(name or "").strip()
        if not _ROUTE_NAME_RE.match(route):
            raise ValueError(f"route_capabilities: invalid route name {name!r}")
        if isinstance(value, str):
            families = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple, set, frozenset)):
            families = [str(part).strip() for part in value if str(part).strip()]
        else:
            raise ValueError(
                f"route_capabilities.{route}: families must be a list or a"
                " comma-separated string"
            )
        unknown = [fam for fam in families if fam not in API_FAMILIES]
        if unknown:
            raise ValueError(
                f"route_capabilities.{route}: unknown API famil"
                f"{'y' if len(unknown) == 1 else 'ies'} {', '.join(unknown)}"
                f" (known: {', '.join(sorted(API_FAMILIES))})"
            )
        if not families:
            raise ValueError(f"route_capabilities.{route}: empty family list")
        overrides[route] = frozenset(families)
    return overrides


def route_family_map(
    upstreams: Mapping[str, str],
    overrides: Optional[Mapping[str, Any]] = None,
) -> dict[str, frozenset[str]]:
    """Route name → API families the route's endpoint serves.

    Operator overrides (already validated by :func:`parse_capability_overrides`
    or validated here) win over the built-in endpoint table — the operator is
    authoritative for their own gateway/proxy endpoints. A route absent from
    both maps to the EMPTY set: no declared capabilities, so write-time
    validation rejects policy entries for it and planning skips them, instead
    of pretending every configured route serves every API.
    """
    declared = parse_capability_overrides(overrides)
    families: dict[str, frozenset[str]] = {}
    for name, base in upstreams.items():
        families[str(name)] = KNOWN_ENDPOINT_FAMILIES.get(
            _normalize_base(base), frozenset()
        )
    for name, fams in declared.items():
        families[name] = frozenset(fams)
    return families


# Endpoint-owned continuation state: a body carrying any of these top-level
# keys belongs to the provider/conversation that created the state. Moving it
# to another route would forward opaque provider state across providers. The
# Responses family is ENDPOINT-LOCAL outright (see plan_request): a Responses
# entry only ever serves requests that arrived at its own route, so arriving
# Responses state is verifiably owned by the arriving endpoint. For the other
# families a policy-controlled caller's opaque continuation is served only
# when the caller's policy could never have routed that family off the
# arriving route; anything else is ambiguous and refused with an actionable
# 409 — never decided by race-prone request history.
STATE_PINNED_KEYS = ("previous_response_id", "conversation")

# Opaque provider reasoning that must never cross a provider/auth boundary:
# Responses reasoning items replay provider-ENCRYPTED state, and Anthropic
# thinking/redacted_thinking blocks are signed by the provider account that
# created them. Detection is structural (key/type presence in content
# blocks), never value inspection, and the payload itself is forwarded
# verbatim or rejected — never scrubbed.
_OPAQUE_CONTENT_KEY = "encrypted_content"
_SIGNED_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _iter_state_blocks(obj: Mapping[str, Any]):
    """Every content block that could carry provider-owned opaque state.

    Responses: top-level ``input`` items (a replayed reasoning item carries
    ``encrypted_content``) and their nested content parts. Anthropic:
    ``messages[*].content`` blocks (``thinking``/``redacted_thinking``).
    """
    input_items = obj.get("input")
    if isinstance(input_items, list):
        for item in input_items:
            if isinstance(item, Mapping):
                yield item
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, Mapping):
                            yield part
    messages = obj.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, Mapping):
                        yield block


def request_state_pinned(body: Optional[bytes]) -> bool:
    """True when the request body carries endpoint-owned continuation state."""
    if not body:
        return False
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(obj, dict):
        return False
    if any(obj.get(key) for key in STATE_PINNED_KEYS):
        return True
    for block in _iter_state_blocks(obj):
        if block.get(_OPAQUE_CONTENT_KEY):
            return True
        if block.get("type") in _SIGNED_THINKING_TYPES:
            return True
    return False


def rewrite_request_model(body: bytes, model: str) -> Optional[bytes]:
    """``body`` with its ``model`` field replaced, or None when not JSON.

    Everything else in the body — messages, tools, tool results, stream
    flags, provider-specific fields — is preserved exactly; routing changes
    which model is asked, never what is asked for. A body that is not a JSON
    object cannot be rewritten and is reported, not silently passed through
    under a policy that claims to choose the model.
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    obj["model"] = model
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ── Cooldowns ────────────────────────────────────────────────────────────────

# A 429 without a parseable Retry-After still steps aside — briefly. The cap
# bounds what a hostile or broken Retry-After can impose.
DEFAULT_COOLDOWN_SEC = 60.0
MAX_COOLDOWN_SEC = 3600.0
MIN_COOLDOWN_SEC = 1.0


def parse_retry_after(value: Optional[str], now: float) -> Optional[float]:
    """Seconds a Retry-After header asks for, clamped, or None when unusable.

    Both forms are supported: delta-seconds and an HTTP date. The result is
    clamped to ``[MIN_COOLDOWN_SEC, MAX_COOLDOWN_SEC]`` so a ``0`` or a
    far-future date cannot produce a meaningless or unbounded cooldown.
    """
    if not value:
        return None
    text = value.strip()
    seconds: Optional[float] = None
    if re.fullmatch(r"\d+", text):
        seconds = float(int(text))
    else:
        try:
            date = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            date = None
        if date is not None:
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
    if seconds is None:
        return None
    return min(max(seconds, MIN_COOLDOWN_SEC), MAX_COOLDOWN_SEC)


def cooldown_scope(route: str, model: Optional[str]) -> tuple[str, str]:
    """The truthful cooldown scope: one route + one model, never wider."""
    return route, (model or "")


def scoped_retry_after(
    skips: list[dict[str, Any]], attempts: list[dict[str, Any]]
) -> Optional[float]:
    """Soonest retry time across THIS request's relevant candidates.

    Only cooldowns attached to this plan's own skips (entries already
    cooling when planned) and this request's 429 attempts (cooldowns just
    marked) count — never an unrelated caller's or route's cooldown, so an
    exhausted answer neither leaks global state nor tells the client a
    retry time belonging to a route its policy cannot use.
    """
    remaining = [
        float(skip["remaining_seconds"])
        for skip in skips
        if skip.get("reason") == "cooldown" and "remaining_seconds" in skip
    ]
    remaining.extend(
        float(attempt["cooldown_seconds"])
        for attempt in attempts
        if attempt.get("status") == 429 and attempt.get("cooldown_seconds")
    )
    return min(remaining) if remaining else None


def _finite_or_none(value: Any) -> Optional[float]:
    """A finite float read from stored state, or None for anything else."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ── Policy model ─────────────────────────────────────────────────────────────

MAX_POLICY_ENTRIES = 16
MODEL_MAX_CHARS = 128

_ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CALLER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _valid_route_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_ROUTE_NAME_RE.match(name))


def is_policy_caller_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_CALLER_NAME_RE.match(name))


def _valid_model(model: Any) -> bool:
    if not isinstance(model, str):
        return False
    model = model.strip()
    if not model or len(model) > MODEL_MAX_CHARS:
        return False
    return all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in model)


def validate_entries(
    entries: Any,
    known_routes: Optional[set[str]] = None,
    route_families: Optional[Mapping[str, frozenset[str]]] = None,
) -> list[dict[str, str]]:
    """Normalize a policy entry list, raising ValueError on the first defect.

    Every entry must name an explicit API family, a configured route, and a
    model. ``known_routes`` is the set of route names the proxy actually
    forwards to (route table ∪ key store); an entry pointing anywhere else is
    rejected at write time rather than discovered mid-request, so a policy
    can never aim the proxy at an unconfigured upstream.

    ``route_families`` (from :func:`route_family_map`) is the compatibility
    boundary: an entry whose family the route's endpoint does not serve is
    rejected — a route named "openai-something" is NOT proof of Responses
    support, and a route with no declared capabilities cannot be targeted at
    all (declare ``llm_usage_proxy.route_capabilities.<route>`` to fix).
    """
    if not isinstance(entries, list) or not entries:
        raise ValueError("a policy needs a non-empty list of entries")
    if len(entries) > MAX_POLICY_ENTRIES:
        raise ValueError(f"a policy holds at most {MAX_POLICY_ENTRIES} entries")
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(entries, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"entry #{index} is not an object")
        route = str(raw.get("route") or "").strip()
        model = str(raw.get("model") or "").strip()
        family = str(raw.get("family") or "").strip()
        if not _valid_route_name(route):
            raise ValueError(f"entry #{index}: invalid route name {route!r}")
        if known_routes is not None and route not in known_routes:
            raise ValueError(
                f"entry #{index}: route {route!r} is not a configured upstream"
            )
        if not _valid_model(model):
            raise ValueError(f"entry #{index}: invalid model {model!r}")
        if family not in API_FAMILIES:
            raise ValueError(
                f"entry #{index}: family must be one of"
                f" {', '.join(sorted(API_FAMILIES))}, got {family!r}"
            )
        if route_families is not None:
            supported = route_families.get(route)
            if not supported:
                raise ValueError(
                    f"entry #{index}: route {route!r} has no declared API"
                    " capabilities — declare them with"
                    f" llm_usage_proxy.route_capabilities.{route} before"
                    " routing policy traffic to it"
                )
            if family not in supported:
                raise ValueError(
                    f"entry #{index}: route {route!r} does not serve the"
                    f" {family} API (it declares:"
                    f" {', '.join(sorted(supported))}) — no format translation"
                    " exists, so this entry could never serve"
                )
        key = (route, model, family)
        if key in seen:
            raise ValueError(f"entry #{index}: duplicate of an earlier entry")
        seen.add(key)
        cleaned.append({"route": route, "model": model, "family": family})
    return cleaned


def _sanitize_loaded_entries(raw: Any) -> list[dict[str, str]]:
    """Entries from the state file, or [] when anything about them is off.

    Reads never raise and never rewrite the file: a malformed saved policy is
    treated as absent (fail-safe to legacy passthrough), and a valid policy
    saved alongside it is preserved untouched.
    """
    try:
        return validate_entries(raw, known_routes=None)
    except ValueError:
        return []


class RoutingPlan:
    """What one request will do: an ordered candidate list plus the why.

    ``candidates`` is always non-empty: with nothing policy-eligible it holds
    the single implicit candidate — the arriving route with no model rewrite —
    which is exactly the legacy behavior. ``evaluated`` marks requests that
    were authenticated as a policy caller; only those write decision rows.

    ``state_auth_passthrough`` is set only for a ``state_pinned`` plan whose
    caller has an enabled policy with entries for this family — i.e. the
    opaque continuation is POLICY-CONTROLLED and ownership was verifiable.
    The transport must serve such a request under the caller's own presented
    credential only: stored route keys are bound to a route, never to an
    account, and the rotator cursor moves with unrelated traffic, so
    substituting a managed key could present the provider-owned state under a
    different account than the one that created it.
    """

    __slots__ = (
        "candidates", "reason", "family", "skips", "policy_caller",
        "evaluated", "state_auth_passthrough",
    )

    def __init__(
        self,
        *,
        candidates: list[dict[str, Optional[str]]],
        reason: str,
        family: Optional[str],
        skips: Optional[list[dict[str, Any]]] = None,
        policy_caller: Optional[str] = None,
        evaluated: bool = False,
        state_auth_passthrough: bool = False,
    ):
        self.candidates = candidates
        self.reason = reason
        self.family = family
        self.skips = skips or []
        self.policy_caller = policy_caller
        self.evaluated = evaluated
        self.state_auth_passthrough = state_auth_passthrough

    @property
    def routed(self) -> bool:
        """True when the plan steers the request off its arriving route/model."""
        return self.evaluated and self.reason == "policy"


def plan_request(
    store: "RoutingStore",
    *,
    policy_caller: Optional[str],
    arriving_route: str,
    rest: str,
    body: Optional[bytes],
    configured_routes: set[str],
    route_key_counts: Mapping[str, int],
    route_families: Optional[Mapping[str, frozenset[str]]] = None,
    arriving_auth_independent: bool = True,
    now: Optional[float] = None,
) -> RoutingPlan:
    """Resolve the ordered usable candidate list for one request.

    Selection rules, in order:

    * No authenticated policy caller → legacy passthrough, unevaluated.
    * The path is not an exact inference path → passthrough.
    * The routing state file is unreadable/corrupt and no last-good view
      exists → the opted-in caller's inference request is REFUSED visibly
      (``state_unreadable``): silently falling back to passthrough would be
      an invisible bypass of the operator's routing intent. Legacy callers
      (no policy token) never reach this branch.
    * The caller's saved policy is malformed → refused visibly
      (``malformed_policy``) with a re-save hint, for the same reason.
    * The body carries endpoint-owned continuation state → the request stays
      on its arriving route (``state_pinned``) when ownership is VERIFIABLE:
      Responses state always is (the family is endpoint-local — see below),
      and other families are when the caller has no enabled policy entries
      that could route this family to a different route. Anything else is
      ``ambiguous_state``: a policy-controlled opaque continuation whose
      owner cannot be verified is refused (the transport answers an
      actionable 409) rather than being silently labelled safe by request
      history — concurrent conversations from one harness have different
      owners, so a caller+family journal heuristic is not ownership
      evidence. When an enabled policy DOES control the family's opaque
      continuation, the request additionally requires independently
      verifiable upstream auth: serving it under a managed stored key would
      substitute a credential whose ACCOUNT is not verifiable (stored keys
      are route-bound, never account-bound, and the rotator cursor moves
      with unrelated traffic), so a policy-controlled pinned request with no
      independent caller auth on a stored-key route is refused
      (``unverifiable_state_auth``, 409 before any send), and a served one
      is marked ``state_auth_passthrough`` so the transport forwards it
      under the caller's own credential instead of a managed key. Legacy
      (no policy / disabled policy / no entries for the family) pinned
      flows keep the pre-policy managed-key passthrough unchanged.
      Cross-provider state moves are never attempted, and the payload is
      forwarded verbatim or rejected — never scrubbed.
    * The caller has no policy, a disabled policy, or no entries for this
      API family → passthrough (legacy behavior for everything the operator
      has not explicitly listed).
    * Entries for the family are tried in saved (operator-priority) order,
      skipping entries whose route is no longer configured, entries whose
      family the route's endpoint does not serve (``incompatible_family``),
      Responses entries for a route other than the arriving one
      (``endpoint_local`` — Codex subscription Responses is NOT
      interchangeable with a generic Responses deployment, and no format
      translation exists, so even a fresh Responses body never crosses
      endpoints), cross-route entries whose target holds no stored
      credential (a client's credential is never forwarded to a different
      provider), same-route entries with no stored credential when the
      caller's own auth was consumed as a LOCAL token (``no_upstream_auth``
      — a local caller token never travels to a provider), and entries under
      an active cooldown. Every skip is recorded with its reason.
    * Entries needing a model rewrite require a JSON object body; if the
      body cannot carry the rewrite the plan degrades to passthrough rather
      than routing a request whose model it cannot actually choose.
    """
    now = time.time() if now is None else now
    implicit = {"route": arriving_route, "model": None}
    family = family_for_path(rest)
    if not policy_caller:
        return RoutingPlan(candidates=[implicit], reason="no_policy_caller", family=family)
    if family is None:
        return RoutingPlan(
            candidates=[implicit],
            reason="not_inference_path",
            family=None,
            policy_caller=policy_caller,
            evaluated=True,
        )
    if store is not None:
        state_error = getattr(store, "state_error", None)
        if state_error is not None and not getattr(store, "has_last_good", False):
            return RoutingPlan(
                candidates=[],
                reason="state_unreadable",
                family=family,
                skips=[{"reason": "state_unreadable", "detail": state_error}],
                policy_caller=policy_caller,
                evaluated=True,
            )
        policy_error = store.policy_error(policy_caller)
        if policy_error is not None:
            return RoutingPlan(
                candidates=[],
                reason="malformed_policy",
                family=family,
                skips=[{"reason": "malformed_policy", "detail": policy_error}],
                policy_caller=policy_caller,
                evaluated=True,
            )
    policy = store.policy_for(policy_caller) if store is not None else None
    if request_state_pinned(body):
        policy_controlled = False
        if policy and policy.get("enabled"):
            family_entries = [
                entry for entry in policy["entries"] if entry["family"] == family
            ]
            if family != FAMILY_RESPONSES:
                # The arriving route is only safe for endpoint-owned state
                # when the policy could never have routed this family
                # anywhere else. Anything else is unverifiable (an earlier
                # request MAY have been routed away — per-conversation
                # ownership is not knowable from caller-level history), so
                # refuse visibly instead of guessing.
                other_routes = sorted(
                    {
                        entry["route"]
                        for entry in family_entries
                        if entry["route"] != arriving_route
                    }
                )
                if other_routes:
                    return RoutingPlan(
                        candidates=[],
                        reason="ambiguous_state",
                        family=family,
                        skips=[
                            {
                                "reason": "ambiguous_state",
                                "routes": other_routes,
                                "detail": (
                                    "the policy can route this family to"
                                    f" {', '.join(other_routes)}, so which endpoint"
                                    " owns this continuation state is not verifiable"
                                ),
                            }
                        ],
                        policy_caller=policy_caller,
                        evaluated=True,
                    )
            # An enabled policy with entries for this family CONTROLS the
            # routing of this opaque continuation (it is pinned to the
            # arriving route only because the policy keeps it there).
            policy_controlled = bool(family_entries)
        if (
            policy_controlled
            and not arriving_auth_independent
            and route_key_counts.get(arriving_route)
        ):
            # Serving would substitute one of the route's stored keys — but a
            # stored key is bound to a ROUTE, never to an ACCOUNT, and the
            # rotator cursor moves with unrelated traffic, so which account
            # the issued credential belongs to is not verifiable (one key /
            # one route is not proof of identity either). Presenting opaque
            # continuation state under the wrong account's credential crosses
            # the very boundary the pin exists to protect, so refuse visibly
            # BEFORE any send. The fix is the caller presenting its own
            # provider credential (independently verifiable), or the operator
            # disabling/restricting the policy.
            return RoutingPlan(
                candidates=[],
                reason="unverifiable_state_auth",
                family=family,
                skips=[
                    {
                        "reason": "unverifiable_state_auth",
                        "route": arriving_route,
                        "detail": (
                            f"the policy controls {family} routing and the"
                            f" route '{arriving_route}' holds managed keys,"
                            " but stored keys are not account-bound, so which"
                            " account would receive this continuation state is"
                            " not verifiable"
                        ),
                    }
                ],
                policy_caller=policy_caller,
                evaluated=True,
            )
        return RoutingPlan(
            candidates=[implicit],
            reason="state_pinned",
            family=family,
            policy_caller=policy_caller,
            evaluated=True,
            state_auth_passthrough=policy_controlled,
        )
    if policy is None:
        return RoutingPlan(
            candidates=[implicit],
            reason="no_policy",
            family=family,
            policy_caller=policy_caller,
            evaluated=True,
        )
    if not policy.get("enabled"):
        return RoutingPlan(
            candidates=[implicit],
            reason="policy_disabled",
            family=family,
            policy_caller=policy_caller,
            evaluated=True,
        )
    entries = [e for e in policy["entries"] if e["family"] == family]
    if not entries:
        return RoutingPlan(
            candidates=[implicit],
            reason="no_matching_entries",
            family=family,
            policy_caller=policy_caller,
            evaluated=True,
        )

    candidates: list[dict[str, Optional[str]]] = []
    skips: list[dict[str, Any]] = []
    for entry in entries:
        route, model = entry["route"], entry["model"]
        if route not in configured_routes:
            skips.append({"route": route, "model": model, "reason": "unknown_route"})
            continue
        if route_families is not None and family not in route_families.get(
            route, frozenset()
        ):
            # The endpoint does not serve this API family (or declares no
            # capabilities at all): skip, never guess from the route's name.
            skips.append(
                {"route": route, "model": model, "reason": "incompatible_family"}
            )
            continue
        if family == FAMILY_RESPONSES and route != arriving_route:
            # Responses is endpoint-local: provider-owned continuation state
            # (previous_response_id, encrypted reasoning) can never be
            # verified across Responses deployments, and two routes whose
            # tables both say "responses" are still distinct APIs — the Codex
            # subscription backend is not interchangeable with a generic
            # Responses provider. With no format translation, even a FRESH
            # Responses body never crosses endpoints; a Responses entry only
            # serves requests that arrived at its own route.
            skips.append({"route": route, "model": model, "reason": "endpoint_local"})
            continue
        if route != arriving_route and not route_key_counts.get(route):
            # Cross-provider moves need the target route's own stored
            # credential; the caller's incoming credential (or token) is
            # never forwarded to a different provider.
            skips.append({"route": route, "model": model, "reason": "no_credentials"})
            continue
        if (
            route == arriving_route
            and not route_key_counts.get(route)
            and not arriving_auth_independent
        ):
            # Same-route candidate whose upstream auth would be the caller's
            # own bearer — which this request presented as a LOCAL caller
            # token. A local token never travels to a provider, and an
            # anonymous policy request is worse than a visible skip.
            skips.append(
                {"route": route, "model": model, "reason": "no_upstream_auth"}
            )
            continue
        cooldown = store.active_cooldown(route, model, now)
        if cooldown is not None:
            skips.append(
                {
                    "route": route,
                    "model": model,
                    "reason": "cooldown",
                    "remaining_seconds": round(cooldown["until"] - now, 1),
                }
            )
            continue
        candidates.append({"route": route, "model": model})

    if not candidates:
        return RoutingPlan(
            candidates=[],
            reason="exhausted",
            family=family,
            skips=skips,
            policy_caller=policy_caller,
            evaluated=True,
        )
    if body and any(c["model"] is not None for c in candidates):
        probe = candidates[0]
        if probe["model"] is not None and rewrite_request_model(body, probe["model"]) is None:
            return RoutingPlan(
                candidates=[implicit],
                reason="unwritable_body",
                family=family,
                skips=skips,
                policy_caller=policy_caller,
                evaluated=True,
            )
    return RoutingPlan(
        candidates=candidates,
        reason="policy",
        family=family,
        skips=skips,
        policy_caller=policy_caller,
        evaluated=True,
    )


# ── Persistent store ─────────────────────────────────────────────────────────

ROUTING_FILENAME = "routing.json"


class RoutingStateError(RuntimeError):
    """The routing state file is unreadable/corrupt — mutations refuse.

    Raised instead of silently overwriting the on-disk state with a partial
    in-memory view: a broken ``routing.json`` keeps the operator's last-known
    intent on disk until a human inspects it, and the error says exactly
    where that file is.
    """


@contextmanager
def file_mutex(lock_path: str) -> Iterator[None]:
    """Exclusive advisory lock across PROCESSES for one read-modify-write.

    The proxy, the CLI and the future web server are separate processes
    sharing one state file; a per-instance threading.Lock cannot stop a
    read/update/os.replace in one process from overwriting another writer's
    change. An flock on a sibling ``<file>.lock`` (never the state file
    itself — atomic replace swaps its inode out from under the lock) holds
    the whole reload→mutate→save sequence atomic. On platforms without
    fcntl this degrades to the in-process lock only.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX-only in practice
        yield
        return
    directory = os.path.dirname(lock_path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def default_routing_path(db_path: Optional[str] = None) -> str:
    """``routing.json`` next to the ledger DB (same root-only directory)."""
    if db_path:
        directory = os.path.dirname(os.path.abspath(db_path))
    else:
        root = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        directory = os.path.join(root, "usage-proxy")
    return os.path.join(directory, ROUTING_FILENAME)


def _ensure_mode_0600(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class RoutingStore:
    """One root-only JSON file: per-caller policies + (route, model) cooldowns.

    Mirrors the KeyStore discipline, plus the cross-process piece a shared
    state file needs: reads re-read the file when it changed on disk (CLI
    writes take effect on a running proxy without a restart), and every
    mutation holds an exclusive flock on a sibling lock file across the
    whole reload→mutate→save, so the proxy, the CLI and a web server can
    never overwrite each other's changes (atomic replace alone prevents
    torn files, not lost updates).

    A file that exists but cannot be parsed is never "repaired" by a read
    and never silently overwritten by a later mutation: the store keeps
    serving its last-good in-memory view (``has_last_good``), exposes the
    failure via ``state_error``, and refuses mutations with
    :class:`RoutingStateError` until the file is fixed. A single malformed
    policy inside a readable file is reported per caller via
    ``policy_error`` so planning can refuse that caller's opted-in requests
    visibly instead of silently bypassing the operator's routing intent.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = os.path.abspath(str(path or default_routing_path()))
        self._lock = threading.Lock()
        self._policies: dict[str, dict[str, Any]] = {}
        self._cooldowns: dict[str, dict[str, dict[str, Any]]] = {}
        self._policy_errors: dict[str, str] = {}
        self._error: Optional[str] = None
        self._loaded_ok = False
        self._stamp: Optional[tuple[int, int]] = None
        self._load()

    # ── state diagnostics ────────────────────────────────────────────────

    @property
    def state_error(self) -> Optional[str]:
        """Why the state file failed to load, or None when it is healthy."""
        return self._error

    @property
    def has_last_good(self) -> bool:
        """True once the file has loaded successfully at least once."""
        return self._loaded_ok

    def policy_error(self, caller: str) -> Optional[str]:
        """Why *caller*'s saved policy is unusable, or None when fine."""
        if not caller:
            return None
        with self._lock:
            self._maybe_reload()
            return self._policy_errors.get(caller)

    # ── reads ────────────────────────────────────────────────────────────

    def policy_for(self, caller: str) -> Optional[dict[str, Any]]:
        """The caller's validated policy (enabled flag + entries), or None."""
        if not caller:
            return None
        with self._lock:
            self._maybe_reload()
            policy = self._policies.get(caller)
            if policy is None:
                return None
            return {
                "enabled": bool(policy.get("enabled")),
                "entries": [dict(entry) for entry in policy.get("entries") or []],
            }

    def policies_snapshot(self) -> dict[str, dict[str, Any]]:
        """Every saved policy, validated, for readback endpoints and the CLI."""
        with self._lock:
            self._maybe_reload()
            return {
                caller: {
                    "enabled": bool(policy.get("enabled")),
                    "entries": [dict(entry) for entry in policy.get("entries") or []],
                }
                for caller, policy in sorted(self._policies.items())
            }

    def active_cooldown(
        self, route: str, model: Optional[str], now: Optional[float] = None
    ) -> Optional[dict[str, Any]]:
        """The live cooldown for exactly ``(route, model)``, or None."""
        now = time.time() if now is None else now
        scope_route, scope_model = cooldown_scope(route, model)
        with self._lock:
            self._maybe_reload()
            entry = (self._cooldowns.get(scope_route) or {}).get(scope_model)
            if entry is None:
                return None
            if float(entry.get("until") or 0) <= now:
                return None
            return dict(entry)

    def cooldowns_snapshot(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        """All live cooldowns, flat, oldest first; expired ones are omitted."""
        now = time.time() if now is None else now
        with self._lock:
            self._maybe_reload()
            rows: list[dict[str, Any]] = []
            for route, models in sorted(self._cooldowns.items()):
                for model, entry in sorted(models.items()):
                    until = float(entry.get("until") or 0)
                    if until <= now:
                        continue
                    rows.append(
                        {
                            "route": route,
                            "model": model or None,
                            "until": until,
                            "until_iso": datetime.fromtimestamp(
                                until, tz=timezone.utc
                            ).isoformat(timespec="seconds"),
                            "remaining_seconds": round(until - now, 1),
                            "reason": str(entry.get("reason") or ""),
                            "retry_after": entry.get("retry_after"),
                        }
                    )
            rows.sort(key=lambda row: row["until"])
            return rows

    # ── writes ───────────────────────────────────────────────────────────

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """The one discipline every mutation goes through.

        Exclusive flock (cross-process) → instance lock (cross-thread) →
        unconditional reload (another writer may have saved while we waited
        for the flock; the mtime shortcut is NOT trusted here) → mutate →
        save. A reload failure raises :class:`RoutingStateError` BEFORE any
        write, so a corrupt file is never overwritten by a partial view.
        """
        with file_mutex(self.path + ".lock"):
            with self._lock:
                self._load()
                if self._error is not None:
                    raise RoutingStateError(
                        f"routing store {self.path} is unreadable"
                        f" ({self._error}); refusing to overwrite it — inspect"
                        " or repair the file (or remove it to start over),"
                        " then retry"
                    )
                yield

    def set_policy(
        self,
        caller: str,
        entries: Any,
        *,
        enabled: bool = True,
        known_routes: Optional[set[str]] = None,
        route_families: Optional[Mapping[str, frozenset[str]]] = None,
    ) -> dict[str, Any]:
        """Replace a caller's policy; validates BEFORE touching the file."""
        if not is_policy_caller_name(caller):
            raise ValueError(f"invalid policy caller name: {caller!r}")
        cleaned = validate_entries(entries, known_routes, route_families)
        with self._mutation():
            self._policies[caller] = {"enabled": bool(enabled), "entries": cleaned}
            self._policy_errors.pop(caller, None)
            self._save()
            return {"enabled": bool(enabled), "entries": [dict(e) for e in cleaned]}

    def set_policy_family(
        self,
        caller: str,
        family: str,
        entries: Any,
        *,
        enabled: Optional[bool] = None,
        known_routes: Optional[set[str]] = None,
        route_families: Optional[Mapping[str, frozenset[str]]] = None,
    ) -> dict[str, Any]:
        """Replace one API family's slice of a policy — atomically.

        The CLI's family-scoped ``routing set`` reads the caller's existing
        entries, swaps this family's slice, and writes the merged policy.
        Doing that read-merge-write OUTSIDE the store would lose a
        concurrent writer's change (another CLI, the proxy's cooldowns), so
        the merge happens inside the mutation lock like every other write.
        """
        if family not in API_FAMILIES:
            raise ValueError(
                f"family must be one of {', '.join(sorted(API_FAMILIES))},"
                f" got {family!r}"
            )
        if not is_policy_caller_name(caller):
            raise ValueError(f"invalid policy caller name: {caller!r}")
        new_entries = validate_entries(entries, known_routes, route_families)
        with self._mutation():
            existing = self._policies.get(caller)
            creating = existing is None
            if creating:
                existing = {"enabled": True, "entries": []}
            if creating:
                switch = bool(enabled) if enabled is not None else True
            else:
                switch = bool(existing["enabled"])
            merged = [e for e in existing["entries"] if e["family"] != family]
            merged.extend(new_entries)
            # The merged policy must still validate as a whole (cross-family
            # duplicates, entry cap); known_routes/families already passed
            # for both halves, so revalidate shape only.
            final = validate_entries(merged)
            self._policies[caller] = {"enabled": switch, "entries": final}
            self._policy_errors.pop(caller, None)
            self._save()
            return {"enabled": switch, "entries": [dict(e) for e in final]}

    def move_policy_entry(
        self,
        caller: str,
        family: str,
        index: int,
        direction: int,
        *,
        known_routes: Optional[set[str]] = None,
        route_families: Optional[Mapping[str, frozenset[str]]] = None,
    ) -> dict[str, Any]:
        """Reorder one entry within a family without touching the policy switch."""
        if family not in API_FAMILIES:
            raise ValueError(
                f"family must be one of {', '.join(sorted(API_FAMILIES))},"
                f" got {family!r}"
            )
        if not is_policy_caller_name(caller):
            raise ValueError(f"invalid policy caller name: {caller!r}")
        target = index + direction
        with self._mutation():
            existing = self._policies.get(caller)
            if existing is None:
                raise ValueError(f"{caller}: no saved policy")
            family_entries = [e for e in existing["entries"] if e["family"] == family]
            if not (0 <= index < len(family_entries)) or not (
                0 <= target < len(family_entries)
            ):
                raise ValueError("move out of range")
            family_entries[index], family_entries[target] = (
                family_entries[target],
                family_entries[index],
            )
            merged = [e for e in existing["entries"] if e["family"] != family]
            merged.extend(family_entries)
            final = validate_entries(merged, known_routes, route_families)
            switch = bool(existing["enabled"])
            self._policies[caller] = {"enabled": switch, "entries": final}
            self._policy_errors.pop(caller, None)
            self._save()
            return {"enabled": switch, "entries": [dict(e) for e in final]}

    def set_policy_enabled(self, caller: str, enabled: bool) -> bool:
        """Flip a policy's switch without disturbing its entry order."""
        with self._mutation():
            policy = self._policies.get(caller)
            if policy is None:
                return False
            policy["enabled"] = bool(enabled)
            self._save()
            return True

    def remove_policy(self, caller: str) -> bool:
        with self._mutation():
            existed = caller in self._policies or caller in self._policy_errors
            self._policies.pop(caller, None)
            self._policy_errors.pop(caller, None)
            if not existed:
                return False
            self._save()
            return True

    def mark_cooldown(
        self,
        route: str,
        model: Optional[str],
        seconds: float,
        *,
        reason: str,
        retry_after: Optional[str] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Persist a bounded cooldown for exactly ``(route, model)``.

        ``seconds`` is already clamped by the caller (parse_retry_after or
        the default); it is clamped again here so no caller can persist an
        unbounded cooldown by bypassing the parser.
        """
        now = time.time() if now is None else now
        bounded = min(max(float(seconds), MIN_COOLDOWN_SEC), MAX_COOLDOWN_SEC)
        scope_route, scope_model = cooldown_scope(route, model)
        entry = {
            "until": now + bounded,
            "set_at": now,
            "seconds": bounded,
            "reason": str(reason or ""),
            "retry_after": retry_after,
        }
        with self._mutation():
            self._prune_expired(now)
            self._cooldowns.setdefault(scope_route, {})[scope_model] = entry
            self._save()
        return dict(entry)

    def clear_cooldowns(
        self, route: Optional[str] = None, model: Optional[str] = None
    ) -> int:
        """Remove cooldowns (all, one route's, or one route+model's)."""
        removed = 0
        with self._mutation():
            if route is None:
                removed = sum(len(models) for models in self._cooldowns.values())
                self._cooldowns = {}
            elif route in self._cooldowns:
                if model is None:
                    removed = len(self._cooldowns[route])
                    del self._cooldowns[route]
                else:
                    scope_model = cooldown_scope(route, model)[1]
                    if scope_model in self._cooldowns[route]:
                        del self._cooldowns[route][scope_model]
                        removed = 1
                        if not self._cooldowns[route]:
                            del self._cooldowns[route]
            if removed:
                self._save()
        return removed

    # ── file plumbing ────────────────────────────────────────────────────

    def _stamp_file(self) -> Optional[tuple[int, int]]:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _maybe_reload(self) -> None:
        """Caller holds the lock. Pick up CLI/webui writes meanwhile."""
        if self._stamp_file() != self._stamp:
            self._load()

    def _prune_expired(self, now: float) -> None:
        """Caller holds the lock. Drop expired cooldowns in memory."""
        for route in list(self._cooldowns):
            models = self._cooldowns[route]
            for model in list(models):
                if float(models[model].get("until") or 0) <= now:
                    del models[model]
            if not models:
                del self._cooldowns[route]

    def _load(self) -> None:
        self._stamp = self._stamp_file()
        if self._stamp is None:
            # No file at all is a legitimate empty state, not an error.
            self._policies, self._cooldowns = {}, {}
            self._policy_errors = {}
            self._error = None
            self._loaded_ok = True
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            # Corrupt/unreadable: keep serving the last-good in-memory view
            # and expose the failure. Mutations refuse (see _mutation), so
            # the broken file is never overwritten by a partial view.
            self._error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "routing store %s unreadable (%s); keeping last-good in-memory"
                " state and refusing mutations until repaired",
                self.path,
                self._error,
            )
            return
        if not isinstance(raw, Mapping):
            self._error = (
                f"top level is {type(raw).__name__}, expected an object"
            )
            logger.error(
                "routing store %s malformed (%s); keeping last-good in-memory"
                " state and refusing mutations until repaired",
                self.path,
                self._error,
            )
            return
        _ensure_mode_0600(self.path)
        policies: dict[str, dict[str, Any]] = {}
        policy_errors: dict[str, str] = {}
        raw_policies = raw.get("policies")
        if isinstance(raw_policies, Mapping):
            for caller, policy in raw_policies.items():
                if not is_policy_caller_name(caller) or not isinstance(
                    policy, Mapping
                ):
                    logger.warning(
                        "routing store %s: ignoring malformed policy entry for"
                        " %r", self.path, caller,
                    )
                    continue
                entries = _sanitize_loaded_entries(policy.get("entries"))
                if entries:
                    policies[str(caller)] = {
                        "enabled": bool(policy.get("enabled")),
                        "entries": entries,
                    }
                else:
                    # A policy we cannot parse must not become an invisible
                    # bypass of the operator's routing intent: the caller's
                    # opted-in requests are refused visibly (plan_request
                    # surfaces this through policy_error) until the policy is
                    # re-saved or removed.
                    policy_errors[str(caller)] = (
                        "saved policy has no valid entries (the file was"
                        " edited or written by an incompatible version);"
                        " re-save it with `hermes llm_usage_proxy routing set`"
                        " or drop it with `routing remove`"
                    )
        cooldowns: dict[str, dict[str, dict[str, Any]]] = {}
        raw_cooldowns = raw.get("cooldowns")
        if isinstance(raw_cooldowns, Mapping):
            now = time.time()
            for route, models in raw_cooldowns.items():
                if not _valid_route_name(route) or not isinstance(
                    models, Mapping
                ):
                    continue
                for model, entry in models.items():
                    if not isinstance(entry, Mapping):
                        continue
                    try:
                        until = float(entry.get("until") or 0)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(until):
                        # A hand-edited or corrupt cooldown must never load
                        # as an unbounded one.
                        continue
                    if until <= now:
                        continue  # expired while the file sat on disk
                    # Bound on LOAD, not only on write: an out-of-range
                    # stored value is clamped to the same cap writers use.
                    until = min(until, now + MAX_COOLDOWN_SEC)
                    cooldowns.setdefault(str(route), {})[str(model)] = {
                        "until": until,
                        "set_at": _finite_or_none(entry.get("set_at")),
                        "seconds": _finite_or_none(entry.get("seconds")),
                        "reason": str(entry.get("reason") or ""),
                        "retry_after": entry.get("retry_after"),
                    }
        self._policies, self._cooldowns = policies, cooldowns
        self._policy_errors = policy_errors
        self._error = None
        self._loaded_ok = True

    def _save(self) -> None:
        """Caller holds the lock. Atomic replace keeps readers on one version."""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        payload = {
            "version": 1,
            "policies": self._policies,
            "cooldowns": self._cooldowns,
        }
        handle_fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".routing-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _ensure_mode_0600(self.path)
        self._stamp = self._stamp_file()


__all__ = [
    "API_FAMILIES",
    "DEFAULT_COOLDOWN_SEC",
    "FAMILY_ANTHROPIC_MESSAGES",
    "FAMILY_CHAT_COMPLETIONS",
    "FAMILY_RESPONSES",
    "KNOWN_ENDPOINT_FAMILIES",
    "MAX_COOLDOWN_SEC",
    "ROUTING_FILENAME",
    "RoutingPlan",
    "RoutingStateError",
    "RoutingStore",
    "cooldown_scope",
    "default_routing_path",
    "family_for_path",
    "file_mutex",
    "is_policy_caller_name",
    "parse_capability_overrides",
    "parse_retry_after",
    "plan_request",
    "request_state_pinned",
    "rewrite_request_model",
    "route_family_map",
    "scoped_retry_after",
    "validate_entries",
]
