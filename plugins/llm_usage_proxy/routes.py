"""Route table construction: which real provider endpoints get measured.

The proxy forwards ``/p/<name>/<rest>`` to the base URL registered under
``name``. Routing decisions in this process (``hermes_cli.llm_usage_routes``)
match by origin+path against the same table, so a request is only ever
rerouted when its true destination is a base the server forwards to
*unchanged* — provider identity, account, and API mode cannot change because
of the proxy.

Endpoints are captured from the profile's own provider resolution surfaces,
never from a process-global env sweep:

* explicit ``llm_usage_proxy.upstreams`` config (operator-authored, wins),
* the well-known provider defaults Hermes itself uses,
* the provider's own declared base-URL env override (``GLM_BASE_URL`` etc. —
  the same var the runtime resolver reads),
* every credential-pool entry's base URL (rotation accounts may pin their
  own endpoint — each distinct base becomes its own route).

Nothing here mutates credential state: no pool selection, no token refresh,
no endpoint probing (both well-known z.ai endpoints are registered, so
whichever the runtime's probe settles on is covered).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Provider id → canonical proxy route name + the well-known bases Hermes
# itself uses for that provider. Distinct resolved bases get suffixed names.
PROVIDER_ROUTES: dict[str, tuple[str, ...]] = {
    "zai": ("https://api.z.ai/api/paas/v4", "https://api.z.ai/api/coding/paas/v4"),
    "kimi-coding": ("https://api.kimi.com/coding", "https://api.moonshot.ai/v1"),
    "xai": ("https://api.x.ai/v1",),
    "openai-codex": ("https://chatgpt.com/backend-api/codex",),
    "xai-oauth": (),
}

# Anthropic-protocol GLM endpoint: used by profiles configured for
# anthropic_messages against z.ai. Not a registry provider id of its own, so
# it is registered directly (URL matching makes any client using it routable).
EXTRA_ROUTES: dict[str, str] = {
    "zai-anthropic": "https://api.z.ai/api/anthropic",
}


def _routable(base: str) -> bool:
    """http(s), has a host, and is not a loopback/local endpoint."""
    raw = str(base or "").strip().rstrip("/")
    if not raw:
        return False
    split = urlsplit(raw)
    if split.scheme not in ("http", "https") or not split.netloc:
        return False
    host = (split.hostname or "").lower()
    return host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _dedup(bases: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for base in bases:
        raw = str(base or "").strip().rstrip("/")
        if raw and raw not in seen and _routable(raw):
            seen.add(raw)
            ordered.append(raw)
    return ordered


def _env_override_base(provider_id: str, environ: Optional[Mapping[str, str]]) -> list[str]:
    """The provider's own declared base-URL env var, if the profile set one.

    Resolution is profile-scoped: the process ``os.environ`` may carry a
    stale shell export or a sibling profile's value under the multiplexed
    gateway, so the unset case reads through the credential layer's scoped
    resolver (profile ``.env`` first, then the secret scope). Pass
    ``environ`` explicitly to pin the mapping — nothing here writes to the
    environment.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:  # pragma: no cover - hermes_cli always present in-product
        return []
    config = PROVIDER_REGISTRY.get(provider_id)
    var = getattr(config, "base_url_env_var", None) if config else None
    if not var:
        return []
    if environ is not None:
        value = str(environ.get(var) or "").strip().rstrip("/")
    else:
        try:
            from hermes_cli.config import get_env_value_prefer_dotenv
        except Exception:  # pragma: no cover - config layer always present
            return []
        value = str(get_env_value_prefer_dotenv(var) or "").strip().rstrip("/")
    return [value] if value else []


def _pool_entry_bases(provider_id: str) -> list[str]:
    """Base URLs pinned by rotation accounts — read-only, no selection.

    ``read_credential_pool`` returns the persisted rows (profile-authoritative
    with a read-only global-root fallback). ``load_pool`` is deliberately
    avoided here: it seeds from singletons/env, prunes, and can write the pool
    back, so route discovery would become a credential mutation.
    """
    try:
        from agent.credential_pool import PooledCredential
        from hermes_cli.auth import read_credential_pool

        raw_entries = read_credential_pool(provider_id)
        entries = [
            PooledCredential.from_dict(provider_id, payload)
            for payload in raw_entries or []
            if isinstance(payload, dict)
        ]
    except Exception:
        return []
    bases = []
    for entry in entries:
        base = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None)
        if base:
            bases.append(str(base))
    return bases


def provider_route_bases(
    provider_id: str, *, environ: Optional[Mapping[str, str]] = None
) -> list[str]:
    """Distinct candidate bases for *provider_id*, deterministic order."""
    route_name = PROVIDER_ROUTES.get(provider_id)
    if route_name is None:
        return []
    return _dedup(
        [
            *PROVIDER_ROUTES[provider_id],
            *_env_override_base(provider_id, environ),
            *_pool_entry_bases(provider_id),
        ]
    )


def build_route_table(
    cfg: Mapping[str, object],
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Name → real upstream base URL for this profile.

    Explicit config entries are authoritative for their name. Additional
    resolved bases join under deterministic suffixed names so one account's
    endpoint never shadows another's (GLM generic vs coding, Kimi /coding vs
    moonshot legacy, per-account pool pins all coexist).
    """
    explicit: dict[str, str] = {}
    raw_upstreams = cfg.get("upstreams") if isinstance(cfg, Mapping) else None
    if isinstance(raw_upstreams, Mapping):
        for name, base in raw_upstreams.items():
            raw = str(base or "").strip().rstrip("/")
            if raw:
                explicit[str(name).strip()] = raw

    routes: dict[str, str] = {}
    taken_bases: set[str] = set()

    def _assign(wanted_name: str, base: str) -> None:
        if base in taken_bases or base in routes.values():
            return
        if wanted_name in routes:
            digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:6]
            wanted_name = f"{wanted_name}-{digest}"
            if wanted_name in routes:
                return
        routes[wanted_name] = base
        taken_bases.add(base)

    for name, base in sorted(explicit.items()):
        if _routable(base):
            routes[name] = base
            taken_bases.add(base)

    for name, base in sorted(EXTRA_ROUTES.items()):
        _assign(name, base)

    for provider_id in sorted(PROVIDER_ROUTES):
        canonical = _canonical_route_name(provider_id)
        for base in provider_route_bases(provider_id, environ=environ):
            _assign(canonical, base)

    return routes


def _canonical_route_name(provider_id: str) -> str:
    return {
        "zai": "zai",
        "kimi-coding": "kimi",
        "xai": "xai",
        "xai-oauth": "xai",
        "openai-codex": "openai-codex",
    }[provider_id]


def build_route_table_scoped(
    cfg: Mapping[str, object],
    *,
    environ: Optional[Mapping[str, str]] = None,
    hermes_home=None,
) -> dict[str, str]:
    """``build_route_table`` with a profile secret scope active around it.

    The provider base-URL env override resolves through
    ``hermes_cli.config.get_env_value_prefer_dotenv`` →
    ``agent.secret_scope.get_secret``, which fails closed under the
    multiplexed gateway when no scope is installed. Wrapping the build in
    ``set_secret_scope`` keeps that guard honest rather than working around
    it: with no scope active, this profile's own ``.env``/secret-source
    snapshot is installed for the duration of the build and reset after, so
    the read resolves *this* profile's endpoints and can never reach for
    another profile's ``os.environ``. An already-active scope (the per-turn
    profile scope) is used as-is. Resolution with multiplexing off is
    unchanged — there the scope is a ``.env`` overlay over the process
    environment, exactly what the credential layer already fell back to.
    """
    from agent.secret_scope import (
        build_profile_secret_scope,
        current_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )

    token = None
    if current_secret_scope() is None:
        from hermes_constants import get_hermes_home

        home = hermes_home if hermes_home is not None else get_hermes_home()
        token = set_secret_scope(build_profile_secret_scope(home))
    try:
        return build_route_table(cfg, environ=environ)
    finally:
        if token is not None:
            reset_secret_scope(token)


def proxy_origin(port: int) -> str:
    from plugins.llm_usage_proxy.server import BIND_HOST

    return f"http://{BIND_HOST}:{int(port)}"


__all__ = [
    "EXTRA_ROUTES",
    "PROVIDER_ROUTES",
    "build_route_table",
    "build_route_table_scoped",
    "provider_route_bases",
    "proxy_origin",
]
