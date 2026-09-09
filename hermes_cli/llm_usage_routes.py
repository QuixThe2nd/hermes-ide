"""Profile-scoped loopback usage-route registry consulted at HTTP-client build.

This is the minimal core seam that lets the bundled ``llm_usage_proxy`` plugin
measure provider-reported token usage on the wire *without* touching provider
identity, authentication, API mode, or profile resolution:

* Provider resolution (profile config → env → credential store → pool →
  registry default) is untouched and still decides every base URL.
* When — and only when — the plugin has registered a route table and verified
  the loopback proxy's identity, httpx transports built through
  ``agent.process_bootstrap.build_keepalive_http_client`` (and the Anthropic
  SDK construction path) gain a thin wrapper that rewrites the **final request
  destination** for requests whose origin+path fall under a registered route
  base, sending them to ``http://127.0.0.1:<port>/p/<name>/<rest>`` instead.
  Everything else about the request (method, headers, auth, body, query) and
  about the response (``response.request`` keeps the logical provider URL) is
  preserved, so SDK-visible behaviour is unchanged.
* Routing decisions are per-request and re-checked against the registry's
  active flag, so a late activation/deactivation applies to already-built
  clients without rebuilding them.

Nothing in this module discovers plugins or reads ``sys.argv``: registering a
table is always the plugin lifecycle's decision. The one exception is the
*lazy bootstrap* below — because ``main()`` skips plugin discovery for the
built-in subcommands (``hermes chat`` included), a plain CLI run never loads
the plugin and would build every provider client unrouted. The HTTP-client
boundary is the seam that *does* run on every real client, so the first
client built for a profile with no routing state yet asks the plugin's
existing guarded lifecycle to reconcile; see :func:`_lazy_bootstrap_routing`.

State is scoped to a single Hermes profile — the identity
:func:`agent.relay_runtime.current_profile_key` already derives from
``get_hermes_home()`` for every other runtime-isolation need in this process.
One process can host several profiles (multiplexed children, a CLI driving a
profile switch, tests), and a proxy is a per-profile service: profile A's
route table and its verified loopback port say nothing about profile B's.
Every public entry point therefore resolves its profile the same way, and the
transport wrappers capture the profile of the code that *built* the client, so
a request can never inherit whatever profile happens to be active when the
request is issued. Registering or clearing a table under one profile leaves
every other profile's table untouched.

The wrapper never retries, replays, or fails over: a routed request that
errors surfaces exactly the error a direct request would have surfaced (never
a silent second attempt that could double-bill an upstream).

Deliberately stdlib-only at import time (httpx and the profile resolver are
imported lazily inside the functions that need them) so bootstrap-critical
modules can import it cheaply.
"""

from __future__ import annotations

import ssl
import threading
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

_LOCK = threading.Lock()
_INACTIVE_REASON = "no route table registered"

# The proxy is loopback-only by construction (the plugin binds 127.0.0.1), so
# a non-loopback origin is never a table this process verified.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Safety valve: one entry per profile that ever registered a table. Profiles
# are few (named profiles + default), but a long-lived multiplexer process
# should not be able to grow this without bound.
_MAX_PROFILE_STATES = 64


class _RoutingState:
    """Route table plus its trust flag for exactly one Hermes profile."""

    __slots__ = ("proxy_origin", "routes", "active", "inactive_reason")

    def __init__(self) -> None:
        self.proxy_origin = ""
        self.routes: dict[str, str] = {}
        self.active = False
        self.inactive_reason = _INACTIVE_REASON


# profile key → state. The dict is only mutated under ``_LOCK``, and a
# registered ``routes`` mapping is never mutated after assignment, so readers
# can safely take a reference and match outside the lock.
_STATES: dict[str, _RoutingState] = {}


def resolve_profile_key(profile: Optional[str] = None) -> str:
    """Return the profile this call is scoped to.

    ``profile`` (an explicit key, used by the transport wrappers) wins;
    otherwise the current context is resolved with
    :func:`agent.relay_runtime.current_profile_key` — the existing
    ``get_hermes_home()``-derived identity Hermes already uses to isolate
    Relay runtimes — so routing state can never span profiles.
    """
    if profile:
        return str(profile)
    try:
        from agent.relay_runtime import current_profile_key
    except Exception:
        from hermes_constants import get_hermes_home

        # Mirror current_profile_key() exactly so the fallback cannot split a
        # single profile into two identities (or merge two into one).
        return str(get_hermes_home().expanduser().resolve())
    return str(current_profile_key())


def _state_for_write_locked(key: str) -> _RoutingState:
    state = _STATES.get(key)
    if state is None:
        if len(_STATES) >= _MAX_PROFILE_STATES:
            for stale, candidate in list(_STATES.items()):
                if len(_STATES) < _MAX_PROFILE_STATES:
                    break
                if not candidate.active and not candidate.routes:
                    _STATES.pop(stale, None)
        state = _RoutingState()
        _STATES[key] = state
    return state


def _valid_route_base(base: str) -> Optional[str]:
    """Return an error string when *base* is unusable as a route target."""
    raw = str(base or "").strip()
    if not raw:
        return "empty base URL"
    split = urlsplit(raw)
    if split.scheme not in ("http", "https"):
        return f"scheme must be http or https, got {split.scheme!r}"
    if not split.netloc:
        return "missing host"
    if "@" in split.netloc:
        return "userinfo in URL is not allowed"
    if split.query or split.fragment:
        return "query strings and fragments are not allowed"
    try:
        _ = split.port  # raises ValueError for invalid ports
    except ValueError:
        return "invalid port"
    if split.username or split.password:
        return "credentials in URL are not allowed"
    return None


def _proxy_origin_error(origin: str) -> Optional[str]:
    """Return an error string when *origin* is not a usable loopback origin.

    The registry only ever speaks for a proxy this process can verify, and the
    plugin binds loopback only: the origin must name a loopback host on an
    explicit port and carry no path/query/fragment (an *origin*, not a URL).
    """
    split = urlsplit(origin)
    host = (split.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return (
            "proxy origin must be loopback (127.0.0.1, ::1 or localhost),"
            f" got {host or 'none'!r}"
        )
    try:
        port = split.port  # raises ValueError for invalid ports
    except ValueError:
        return "invalid proxy port"
    if port is None:
        return "proxy origin must carry an explicit port"
    path = split.path.rstrip("/")
    if path:
        return "proxy origin must not carry a path"
    return None


def _origin_of(url: str) -> str:
    split = urlsplit(url)
    return f"{split.scheme}://{split.netloc}"


def _canonical_origin(url: str) -> str:
    """scheme + lowercased host + explicit port, so that origin comparisons
    (self-routing in particular) cannot be dodged by case or alias spelling."""
    split = urlsplit(str(url or ""))
    host = (split.hostname or "").lower()
    try:
        port = split.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if split.scheme == "https" else 80
    host_part = f"[{host}]" if ":" in host else host
    return f"{(split.scheme or '').lower()}://{host_part}:{port}"


def register_route_table(
    proxy_origin: str,
    routes: Mapping[str, str],
    *,
    profile: Optional[str] = None,
) -> list[str]:
    """Install the route table (name → real upstream base URL) for a profile.

    Returns the list of validation errors (empty = success). A failed call
    leaves that profile's previously registered table untouched (and leaves
    every *other* profile's table untouched in all cases). Does NOT activate
    routing — :func:`activate_routing` does, after the proxy's identity has
    been verified.
    """
    errors: list[str] = []
    origin = str(proxy_origin or "").strip().rstrip("/")
    if not origin:
        return ["proxy origin is empty"]
    if _valid_route_base(origin):
        return [f"invalid proxy origin {proxy_origin!r}"]
    problem = _proxy_origin_error(origin)
    if problem:
        errors.append(problem)
        return errors

    cleaned: dict[str, str] = {}
    for name, base in (routes or {}).items():
        key = str(name or "").strip()
        raw = str(base or "").strip().rstrip("/")
        problem = _valid_route_base(raw)
        if problem:
            errors.append(f"route {key!r}: {problem}")
            continue
        if _canonical_origin(raw) == _canonical_origin(origin):
            errors.append(f"route {key!r}: target is the proxy itself")
            continue
        cleaned[key] = raw
    if not cleaned and not errors:
        errors.append("route table is empty")
    if errors:
        return errors

    with _LOCK:
        state = _state_for_write_locked(resolve_profile_key(profile))
        state.proxy_origin = origin
        state.routes = dict(cleaned)
        # Re-verify activation against the new table from the caller.
        state.active = False
        state.inactive_reason = "route table registered; proxy identity not verified"
    return []


def activate_routing(*, profile: Optional[str] = None) -> None:
    """Mark the calling profile's registered table as trusted for live traffic."""
    with _LOCK:
        state = _STATES.get(resolve_profile_key(profile))
        if state is not None and state.routes:
            state.active = True
            state.inactive_reason = ""


def deactivate_routing(reason: str, *, profile: Optional[str] = None) -> None:
    """Stand routing down (disable, identity mismatch, shutdown). No-op on flags
    when no table is registered; the reason is always recorded for status."""
    key = resolve_profile_key(profile)
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            state = _STATES[key] = _RoutingState()
        state.active = False
        state.inactive_reason = str(reason or "routing deactivated")


def clear_route_table(reason: str = "route table cleared", *, profile: Optional[str] = None) -> None:
    """Drop a profile's table entirely (plugin disable / teardown).

    Only the calling profile's table is dropped; clients built under other
    profiles keep routing to their own proxy. The reason is kept for status,
    exactly as a deactivated table's is.
    """
    key = resolve_profile_key(profile)
    with _LOCK:
        state = _RoutingState()
        state.inactive_reason = str(reason or "route table cleared")
        _STATES[key] = state


# ── lazy first-use bootstrap ─────────────────────────────────────────────────

# Bookkeeping for the one-shot bootstrap below. Deliberately a *separate* lock
# from ``_LOCK`` (and always taken before it): the lifecycle callback calls
# ``register_route_table``/``activate_routing``, which take ``_LOCK``, so
# holding ``_LOCK`` across the callback would deadlock on a non-reentrant lock.
# Reentrant, and held *across* the reconcile callback, because the first
# initialization has to be serialized: a second client built while it runs must
# wait for the decision instead of constructing a direct client. The registry
# lock is never held across the callback.
_BOOTSTRAP_LOCK = threading.RLock()
# Profiles whose bootstrap already ran (or was refused). One attempt per
# profile per process: a client build is a terrible place to retry service
# reconciliation, and the reason for standing down stays visible in status.
# Written only once an attempt is over, under ``_BOOTSTRAP_LOCK`` — never
# before it starts.
_BOOTSTRAP_SEEN: set[str] = set()
# Re-entrancy guard: the reconcile makes HTTP calls, so a wrapper built inside
# it must consult the registry as it stands, not recurse into bootstrapping.
_BOOTSTRAP_TLS = threading.local()


def _lazy_bootstrap_routing(key: str) -> None:
    """Reconcile once for *key* when nothing has decided routing for it yet.

    ``main()`` skips plugin discovery for the built-in subcommands
    (``hermes chat`` included) to save startup time, so a plain CLI run never
    calls the plugin's ``register()`` and — before this seam existed — built
    every provider client unrouted, however the profile was configured. The
    HTTP-client boundary is what every real main/aux/CLI client goes through
    regardless of plugin discovery, so the first client build consults the
    plugin's existing guarded lifecycle here
    (``lifecycle.reconcile_proxy_on_load``): an enabled profile verifies
    its sidecar's identity and route table and activates in time for the client
    being built. Disabled profiles never get that far — the config gates below
    return before any systemd work is even attempted.

    Deliberately narrow:

    * one attempt per profile per process, and the first initialization is
      serialized: a concurrent first client waits for the decision instead of
      slipping through unrouted;
    * an entry already present for the profile — registered, active,
      deactivated, or cleared — is an explicit decision that is never
      second-guessed;
    * never raises, and never blocks a second client behind a failed attempt.
    """
    if getattr(_BOOTSTRAP_TLS, "active", False):
        return
    with _BOOTSTRAP_LOCK:
        if key in _BOOTSTRAP_SEEN:
            return
        with _LOCK:
            _BOOTSTRAP_SEEN.add(key)
        _BOOTSTRAP_TLS.active = True
        try:
            with _LOCK:
                # A registered, active, deactivated or cleared table is an
                # explicit decision this bootstrap must not second-guess.
                if _STATES.get(key) is not None:
                    return

            from plugins.llm_usage_proxy import lifecycle
            from plugins.llm_usage_proxy.config import (
                load_llm_usage_proxy_config,
                plugin_explicitly_disabled,
            )

            # Denied by the plugins deny-list, or simply not enabled in this
            # profile's config: stand down here, so the lifecycle — and with
            # it every systemctl call — is never reached.
            if plugin_explicitly_disabled() or not load_llm_usage_proxy_config().get(
                "enabled"
            ):
                return

            lifecycle.reconcile_proxy_on_load()
        except Exception:
            # The lifecycle never raises; if anything else blows up the traffic
            # simply stays direct and unmetered rather than breaking client
            # construction.
            pass
        finally:
            _BOOTSTRAP_TLS.active = False
            # Recorded only now, while still holding the bootstrap lock: a
            # concurrent client that waited above either sees the decision
            # this attempt made, or gets the next attempt — it never skips
            # initialization and builds a direct client.
            _BOOTSTRAP_SEEN.add(key)


def _entry_profile(profile: Optional[str]) -> str:
    """Resolve the calling profile, lazily bootstrapping routing for it.

    Only a profile resolved from the *current context* may bootstrap: the
    lifecycle reconciles the current profile's config, service and identity,
    so a caller that passed an explicit profile must never trigger (or inherit)
    another profile's reconcile.
    """
    key = resolve_profile_key(profile)
    if profile is None:
        _lazy_bootstrap_routing(key)
    return key


def _default_routing_state() -> dict[str, Any]:
    return {
        "active": False,
        "reason": _INACTIVE_REASON,
        "proxy_origin": "",
        "routes": {},
    }


def routing_state(*, profile: Optional[str] = None) -> dict[str, Any]:
    """Snapshot for status surfacing (safe: no credentials involved)."""
    snapshot = _default_routing_state()
    snapshot["profile"] = resolve_profile_key(profile)
    with _LOCK:
        state = _STATES.get(snapshot["profile"])
        if state is None:
            return snapshot
        snapshot["active"] = state.active
        snapshot["reason"] = "" if state.active else state.inactive_reason
        snapshot["proxy_origin"] = state.proxy_origin
        snapshot["routes"] = dict(state.routes)
        return snapshot


def _match_route(routes: Mapping[str, str], url: str) -> Optional[tuple[str, str, str]]:
    """Longest-prefix boundary match of *url* against a route table.

    Returns ``(name, rest_path, query)`` where *rest_path* keeps its leading
    ``/`` (or is empty when the URL is exactly the route base). Requires the
    scheme and netloc to match exactly and the base path to end on a real
    path-segment boundary, so ``/v1`` never matches ``/v10/x``.
    """
    split = urlsplit(str(url or ""))
    best: Optional[tuple[int, str, str, str]] = None
    for name, base in routes.items():
        base_split = urlsplit(base)
        if split.scheme != base_split.scheme:
            continue
        if split.netloc != base_split.netloc:
            continue
        base_path = base_split.path.rstrip("/")
        path = split.path
        if base_path:
            if path == base_path:
                rest = ""
            elif path.startswith(base_path + "/"):
                rest = path[len(base_path) :]
            else:
                continue
        else:
            rest = path if path.startswith("/") else "/" + path
        if best is None or len(base_path) > best[0]:
            best = (len(base_path), name, rest, split.query)
    if best is None:
        return None
    return best[1], best[2], best[3]


def reroute_url(url: Any, *, profile: Optional[str] = None) -> Optional[str]:
    """Return the loopback proxy URL for *url*, or None to send it direct.

    None whenever the profile's routing is inactive, no table is registered,
    or the URL does not fall under a registered route base. Pure function of
    the registry: an explicit *profile* (what the transport wrappers pass) is
    honoured as-is, and ``None`` means "the caller's current profile", never
    "some other profile that happens to be active".
    """
    key = resolve_profile_key(profile)
    with _LOCK:
        state = _STATES.get(key)
        if state is None or not state.active:
            return None
        routes, origin = state.routes, state.proxy_origin
    matched = _match_route(routes, str(url))
    if matched is None:
        return None
    name, rest, query = matched
    target = f"{origin}/p/{name}{rest}"
    if query:
        target += "?" + query
    return target


def base_url_routable(base_url: Any, *, profile: Optional[str] = None) -> bool:
    """True when a client built for *base_url* could produce routed requests.

    Cheap construct-time eligibility probe used by the HTTP-client seams; the
    authoritative decision stays per-request in :func:`reroute_url`.
    """
    key = _entry_profile(profile)
    with _LOCK:
        state = _STATES.get(key)
        if state is None or not state.routes:
            return False
        routes = state.routes
    return _match_route(routes, str(base_url or "")) is not None


# ── httpx transport wrappers ─────────────────────────────────────────────────


def _verified_tls_policy(verify: Any) -> bool:
    """True when *verify* is a TLS policy that still verifies the upstream.

    Both the literal default (``True``) and an explicit ``ssl.SSLContext`` —
    what ``agent.ssl_verify.resolve_httpx_verify`` returns whenever a CA bundle
    is in play (``HERMES_CA_BUNDLE``, ``SSL_CERT_FILE``, a per-provider
    ``ssl_ca_cert``), i.e. what the real provider clients are built with —
    keep certificate verification on, so rerouting them changes nothing about
    the trust decision. ``verify=False`` turns verification off: that policy
    must stay direct and unmetered rather than be traded for the proxy's own
    upstream TLS settings.
    """
    return verify is True or isinstance(verify, ssl.SSLContext)


def _proxied_request(request: Any, target: str) -> Any:
    """Build the request actually sent to the proxy from the logical one.

    Same method, headers, body stream and extensions — only the destination
    differs. ``Host`` is pinned to the logical provider netloc so the wire
    request stays honest about where the client thinks it is going; the proxy
    rewrites Host to the upstream netloc when forwarding.
    """
    import httpx

    headers = request.headers.copy()
    logical = urlsplit(str(request.url))
    if logical.netloc:
        headers["Host"] = logical.netloc
    return httpx.Request(
        request.method,
        target,
        headers=headers,
        stream=request.stream,
        extensions=dict(request.extensions),
    )


def _make_sync_wrapper() -> type:
    import httpx

    class _RoutedSyncTransport(httpx.BaseTransport):
        """Sync transport view that reroutes matching requests to the proxy."""

        __slots__ = ("_inner", "_profile")

        def __init__(self, inner: Any, profile: str) -> None:
            self._inner = inner
            # Bound at construction: this transport reroutes only under the
            # profile that built its client, whatever is active at request time.
            self._profile = str(profile)

        # Introspection seams (socket-abort sweeps, tests) look at ``_pool``.
        @property
        def _pool(self) -> Any:
            return getattr(self._inner, "_pool", None)

        @property
        def profile(self) -> str:
            """Profile whose route table this transport consults."""
            return self._profile

        def handle_request(self, request: Any) -> Any:
            target = reroute_url(request.url, profile=self._profile)
            if target is None:
                return self._inner.handle_request(request)
            response = self._inner.handle_request(_proxied_request(request, target))
            # Keep the SDK-visible request pointing at the logical provider
            # URL (error messages, retry decisions, logging).
            response.request = request
            return response

        def close(self) -> None:
            self._inner.close()

    return _RoutedSyncTransport


def _make_async_wrapper() -> type:
    import httpx

    class _RoutedAsyncTransport(httpx.AsyncBaseTransport):
        """Async transport view that reroutes matching requests to the proxy."""

        __slots__ = ("_inner", "_profile")

        def __init__(self, inner: Any, profile: str) -> None:
            self._inner = inner
            self._profile = str(profile)

        @property
        def _pool(self) -> Any:
            return getattr(self._inner, "_pool", None)

        @property
        def profile(self) -> str:
            """Profile whose route table this transport consults."""
            return self._profile

        async def handle_async_request(self, request: Any) -> Any:
            target = reroute_url(request.url, profile=self._profile)
            if target is None:
                return await self._inner.handle_async_request(request)
            response = await self._inner.handle_async_request(
                _proxied_request(request, target)
            )
            response.request = request
            return response

        async def aclose(self) -> None:
            await self._inner.aclose()

    return _RoutedAsyncTransport


_SYNC_WRAPPER: Optional[type] = None
_ASYNC_WRAPPER: Optional[type] = None


def wrap_mounts_for_usage_routing(
    mounts: dict[str, Any],
    *,
    base_url: str,
    verify: Any,
    async_mode: bool,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """Wrap an httpx mount table for loopback usage routing, or return it as-is.

    Called from ``agent.process_bootstrap.build_keepalive_http_client`` with
    the mounts it just built. Wrapping is skipped — traffic stays direct and
    honestly unmetered — unless routing could apply *for the profile building
    this client*:

    * a route table is registered whose bases cover this client's base URL
      (per-request matching stays authoritative for redirects etc.);
    * TLS policy still verifies the upstream (``verify is True`` or an
      ``ssl.SSLContext``, see :func:`_verified_tls_policy`): a disabled
      verification (``verify=False``) must not be silently traded for the
      proxy's own upstream TLS settings;
    * the caller passed no env proxy for this base (a pinned HTTPS_PROXY is
      a policy the proxy leg would silently bypass).

    Every wrapper is bound to the constructing profile, so a request issued
    later — from a coroutine, another thread, or after a profile switch —
    consults that profile's table and no other's. The wrapper delegates to the
    caller's transport objects (shared-pool views included), so pool sharing
    and per-client close semantics are preserved exactly.
    """
    try:
        if not _verified_tls_policy(verify) or not mounts:
            return mounts
        # May run the one-shot lazy bootstrap, so a CLI that never loaded the
        # plugin still gets its enabled profile's verified routing in time for
        # this client.
        key = _entry_profile(profile)
        if not base_url_routable(base_url, profile=key):
            return mounts
        global _SYNC_WRAPPER, _ASYNC_WRAPPER
        if async_mode:
            if _ASYNC_WRAPPER is None:
                _ASYNC_WRAPPER = _make_async_wrapper()
            wrapper_cls = _ASYNC_WRAPPER
        else:
            if _SYNC_WRAPPER is None:
                _SYNC_WRAPPER = _make_sync_wrapper()
            wrapper_cls = _SYNC_WRAPPER
        return {
            scheme: wrapper_cls(transport, key) for scheme, transport in mounts.items()
        }
    except Exception:
        # A routing failure must never take client construction down; the
        # traffic simply stays unmetered (visible via routing_state()).
        return mounts


def build_sync_routed_client(
    base_url: str, *, timeout: Any = None, profile: Optional[str] = None
) -> Optional[Any]:
    """httpx.Client for SDK paths that build their own transport (Anthropic).

    Returned only when this *base_url* is routable under default TLS policy
    for the profile constructing the client; ``None`` means "let the SDK build
    its default client" (direct, unmetered). The client's transport is bound
    to that profile for its whole lifetime. Like the mount-wrapper seam, this
    may run the one-shot lazy bootstrap first, so an enabled profile whose
    plugin never loaded still gets routing in time for this client.
    """
    try:
        import httpx

        key = _entry_profile(profile)
        if not base_url_routable(base_url, profile=key):
            return None
        global _SYNC_WRAPPER
        if _SYNC_WRAPPER is None:
            _SYNC_WRAPPER = _make_sync_wrapper()
        kwargs: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        return httpx.Client(
            transport=_SYNC_WRAPPER(httpx.HTTPTransport(), key), **kwargs
        )
    except Exception:
        return None


def _reset_registry() -> None:
    """Drop every profile's state (test teardown only)."""
    with _BOOTSTRAP_LOCK:
        _BOOTSTRAP_SEEN.clear()
    with _LOCK:
        _STATES.clear()


__all__ = [
    "activate_routing",
    "base_url_routable",
    "build_sync_routed_client",
    "clear_route_table",
    "deactivate_routing",
    "register_route_table",
    "reroute_url",
    "resolve_profile_key",
    "routing_state",
    "wrap_mounts_for_usage_routing",
]
