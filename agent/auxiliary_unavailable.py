"""Why an auxiliary client could not be built (missing credentials / provider)."""

import contextlib
import time
from typing import Optional


class AuxiliaryClientUnavailable(RuntimeError):
    """No auxiliary client could be built for the task (missing credentials / provider)."""


def pool_cooldown_message(provider_id: str) -> Optional[str]:
    """The "all N credentials … are cooling down" error when the provider's pool is fully benched.

    ``resolve_provider_client()`` returns ``None`` both when no credential exists and when every
    pool entry sits in a 429/quota cooldown, so the raise sites could only say "no credentials
    were found. Run hermes auth add …" — wrong on both counts for a valid OAuth grant that is
    merely rate-limited (#56810). Read the persisted pool state (no seeding, no writes) and name
    the cooldown and its reset time instead; ``None`` when the pool is empty or a credential is
    usable (the caller keeps the missing-credential diagnostic).
    """
    from agent.credential_pool import STATUS_DEAD, PooledCredential, _exhausted_until
    from hermes_cli.auth import read_credential_pool

    entries = []
    with contextlib.suppress(Exception):
        entries = [PooledCredential.from_dict(provider_id, e)
                   for e in read_credential_pool(provider_id) if isinstance(e, dict)]
    live = [e for e in entries if e.last_status != STATUS_DEAD]
    if not live:
        return None
    now = time.time()
    resets = [until for e in live
              for until in (_exhausted_until(e, sole_credential=len(live) == 1),)
              if until is not None and until > now]
    if len(resets) != len(live):
        return None
    when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(min(resets)))
    which = ("its only credential is" if len(live) == 1
             else f"all {len(live)} credentials are")
    return (f"Provider '{provider_id}' is set in config.yaml but {which} cooling down after a "
            f"rate limit / quota error (429); the next one resets at {when}. Wait for the reset, "
            f"add another credential with `hermes auth add {provider_id}`, or switch to a "
            "different provider with `hermes model`.")


def missing_provider_credentials_message(provider_id: str) -> str:
    """The "Provider 'X' is set in config.yaml but …" error for an explicit provider with no credentials.

    The remedy comes from the registry, never from the provider id: ``f"{id.upper()}_API_KEY"``
    invents names nothing reads (alibaba → ALIBABA_API_KEY instead of DASHSCOPE_API_KEY, and the
    unsettable MINIMAX-OAUTH_API_KEY for OAuth ids, #114405 / #78996). OAuth providers have no key
    env var at all, so they are pointed at the sign-in command instead. A pool whose every
    credential is cooling down is not "missing" — that case names the cooldown (#56810).
    """
    cooldown = pool_cooldown_message(provider_id)
    if cooldown:
        return cooldown
    pconfig = None
    with contextlib.suppress(Exception):
        from hermes_cli.auth import PROVIDER_REGISTRY
        pconfig = PROVIDER_REGISTRY.get(provider_id)
    env_vars = tuple(getattr(pconfig, "api_key_env_vars", None) or ())
    problem, remedy = "no API key was found", ""
    if env_vars:
        remedy = f"Set the {env_vars[0]} environment variable"
    elif pconfig is None:
        remedy = f"Set the {provider_id.upper().replace('-', '_')}_API_KEY environment variable"
    elif str(pconfig.auth_type).startswith("oauth"):
        problem, remedy = "no credentials were found", f"Run `hermes auth add {provider_id}` to sign in"
    else:
        problem = "no credentials were found"
    switch = "switch to a different provider with `hermes model`."
    return (f"Provider '{provider_id}' is set in config.yaml but {problem}. "
            + (f"{remedy}, or {switch}" if remedy else switch.capitalize()))
