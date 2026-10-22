"""OAuth access-token resolver for the ``llm_usage_proxy`` key manager.

The proxy process (``plugins/llm_usage_proxy/server.py``) is deliberately
stdlib-only, so it cannot resolve or refresh upstream OAuth credentials
itself. For a route marked OAuth-managed in the proxy's key store it spawns
this module instead:

    python -m hermes_cli.llm_proxy_oauth <provider-id>

Resolution goes through the *existing* runtime credential resolvers
(``resolve_codex_runtime_credentials`` / ``resolve_xai_oauth_runtime_credentials``),
which read — and, when the access token is expiring, refresh under the
canonical cross-process store locks — the profile's own ``auth.json``. Nothing
here keeps a separate token copy: a single-use refresh token is only ever
consumed inside those locked resolvers, and a peer process that already
rotated the grant is adopted by them.

Wire contract (stdout, exactly one JSON object — the access token is never
logged by either side):

* success: ``{"access_token": str, "expires_at": float|None, "headers": {str: str}}``
  — ``headers`` carries the account/workspace headers the upstream derives
  from the token (e.g. ``ChatGPT-Account-ID`` for Codex) so the proxy can
  inject them alongside the credential;
* failure (exit status 1): ``{"error": str}`` — auth-layer error text only,
  which never contains token material.

``HERMES_LLM_PROXY_OAUTH_TOKEN_URL`` overrides the Codex token-endpoint URL
before resolution. It exists for the out-of-band proof harness (a fake
token endpoint on loopback); production deployments never set it.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any, Dict, Optional

# Provider ids the proxy's key store may name in its "oauth" section.
OAUTH_PROVIDERS = ("openai-codex", "xai-oauth")

_TOKEN_URL_OVERRIDE_ENV = "HERMES_LLM_PROXY_OAUTH_TOKEN_URL"


def _jwt_exp(token: str) -> Optional[float]:
    """The JWT ``exp`` claim as an epoch float, or None when undecodable."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def _resolve_codex() -> Dict[str, Any]:
    override = os.environ.get(_TOKEN_URL_OVERRIDE_ENV, "").strip()
    if override:
        # Proof-harness seam: the refresh POST goes to a fake loopback token
        # endpoint. auth_codex bound the constant at import; patch its module
        # global (read at call time) rather than the constants module.
        import hermes_cli.auth_codex as _codex_mod

        _codex_mod.CODEX_OAUTH_TOKEN_URL = override
    from hermes_cli.auth import resolve_codex_runtime_credentials

    creds = resolve_codex_runtime_credentials()
    token = str(creds.get("api_key") or "").strip()
    if not token:
        raise RuntimeError("Codex credential resolution returned no access token")
    from agent.codex_headers import codex_account_headers

    return {
        "access_token": token,
        "expires_at": _jwt_exp(token),
        "headers": codex_account_headers(token),
    }


def _resolve_xai_oauth() -> Dict[str, Any]:
    from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

    creds = resolve_xai_oauth_runtime_credentials()
    token = str(creds.get("api_key") or "").strip()
    if not token:
        raise RuntimeError("xAI OAuth credential resolution returned no access token")
    return {"access_token": token, "expires_at": _jwt_exp(token), "headers": {}}


_RESOLVERS = {
    "openai-codex": _resolve_codex,
    "xai-oauth": _resolve_xai_oauth,
}


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    provider = args[0].strip() if args else ""
    resolver = _RESOLVERS.get(provider)
    if resolver is None:
        print(
            json.dumps(
                {
                    "error": (
                        f"unknown OAuth provider {provider!r}; expected one of"
                        f" {', '.join(OAUTH_PROVIDERS)}"
                    )
                }
            )
        )
        return 1
    try:
        payload = resolver()
    except Exception as exc:
        # Auth-layer error text (AuthError included) carries no token material;
        # bound the length so a pathological message cannot flood the journal.
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:500]}))
        return 1
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
