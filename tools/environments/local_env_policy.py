"""Secret-scrub policy for Hermes child processes: pure data + predicates for which env
names are Hermes-managed credentials. The env *builders* applying it (``_make_run_env``,
``_sanitize_subprocess_env``, ``hermes_subprocess_env``) live in ``tools.environments.local``."""

import os

# Prefix a caller uses in ``extra_env`` to force a blocklisted var through.
_HERMES_PROVIDER_ENV_FORCE_PREFIX = "_HERMES_FORCE_"

# Hermes-managed AWS *inference* credentials for ``auth_type="aws_sdk"`` (Bedrock):
# only the Bedrock bearer token, which no aws/terraform/boto3 toolchain uses. The
# general AWS chain stays inheritable on purpose — the local terminal is the user's
# trusted operator shell (SECURITY.md §3.2) and env_passthrough can never re-allow a
# blocklisted name (GHSA-rhgp-j443-p4rf), so blocking it would be unrecoverable.
_AWS_SDK_CREDENTIAL_ENV_VARS = frozenset({"AWS_BEARER_TOKEN_BEDROCK"})

_STATIC_PROVIDER_ENV_BLOCKLIST = frozenset({
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION", "OPENROUTER_API_KEY", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "LLM_MODEL", "GOOGLE_API_KEY",
    # Path to a GCP service-account JSON, not a bare key, so OPTIONAL_ENV_VARS
    # marks it password=False and the registry loop skips it.
    "VERTEX_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS", "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "PERPLEXITY_API_KEY",
    "COHERE_API_KEY", "FIREWORKS_API_KEY", "XAI_API_KEY", "HELICONE_API_KEY",
    "PARALLEL_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL",
    "DISCORD_REQUIRE_MENTION",
    "DISCORD_FREE_RESPONSE_CHANNELS", "DISCORD_AUTO_THREAD",
    "SLACK_ALLOWED_USERS", "WHATSAPP_ENABLED",
    "WHATSAPP_MODE", "WHATSAPP_ALLOWED_USERS", "SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT",
    "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS",
    "SIGNAL_IGNORE_STORIES", "HASS_TOKEN", "HASS_URL",
    "EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST",
    "EMAIL_HOME_ADDRESS", "EMAIL_HOME_ADDRESS_NAME", "HERMES_DASHBOARD_SESSION_TOKEN",
    "GATEWAY_ALLOWED_USERS", "GH_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY", "GATEWAY_RELAY_ID", "GATEWAY_RELAY_SECRET",
    "GATEWAY_RELAY_DELIVERY_KEY", "VERCEL_OIDC_TOKEN", "VERCEL_TOKEN",
    "VERCEL_PROJECT_ID", "VERCEL_TEAM_ID",
})


def _build_provider_env_blocklist() -> frozenset:
    """Derive the blocklist from provider, tool, and gateway config."""
    blocked: set[str] = set(_STATIC_PROVIDER_ENV_BLOCKLIST)
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        for pconfig in PROVIDER_REGISTRY.values():
            blocked.update(pconfig.api_key_env_vars)
            if pconfig.auth_type == "aws_sdk":
                blocked.update(_AWS_SDK_CREDENTIAL_ENV_VARS)
            if pconfig.base_url_env_var:
                blocked.add(pconfig.base_url_env_var)
    except ImportError:
        pass
    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS
        for name, metadata in OPTIONAL_ENV_VARS.items():
            category = metadata.get("category")
            if category in {"tool", "messaging"} or (
                    category == "setting" and metadata.get("password")):
                blocked.add(name)
    except ImportError:
        pass
    # CLAUDE_CODE_OAUTH_TOKEN (via the anthropic registry entry) belongs to the user's
    # Claude Code install, not Hermes: stripping it made agent-spawned ``claude`` CLIs
    # fall through to the shared Keychain / ~/.claude store and, on auth failure, wipe
    # it — logging the user out.
    # It is set and owned by the user's Claude Code install (subscription OAuth), not a Hermes-managed
    # inference credential — Claude subscription auth is not a working Hermes provider path. It arrives via
    # the registry loop above (anthropic api_key_env_vars), so remove it explicitly. See #55878.
    blocked.discard("CLAUDE_CODE_OAUTH_TOKEN")
    return frozenset(blocked)


_HERMES_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()


def _matches_terminal_first_party_prefix(name: str) -> bool:
    return False


def _is_terminal_first_party_env(name: str) -> bool:
    return False


# Active-venv markers that must NOT leak: VIRTUAL_ENV/CONDA_PREFIX make uv/poetry sync
# ANOTHER project's deps into the Hermes venv (still reachable via PATH, so stripping
# is safe); PYTHONHOME redirects a child interpreter's stdlib to the Hermes venv
# (version-mismatch crashes). PYTHONPATH is handled separately (Hermes-owned entries only).
# The gateway runs inside its own venv, so its process environment carries VIRTUAL_ENV (and possibly
# CONDA_PREFIX). If those leak into commands the agent runs against OTHER Python projects, tools like
# ``uv``/``poetry`` treat the inherited value as the active environment and build/sync that other project's
# dependencies into the Hermes venv path instead of the project's own ``.venv`` — silently clobbering the
# Hermes environment (e.g. a project pinned to a different Python version overwrites it and breaks the
# gateway). PYTHONHOME is included because a gateway-inherited value redirects the standard-library search
# of ANY child interpreter — including unrelated system/venv Pythons — to the Hermes venv's stdlib, which
# crashes with version-mismatch errors before a child script even imports a package (#75018). Hermes itself
# treats PYTHONHOME as contamination in its own child processes (managed_uv.py, sqlite_runtime.py), so
# stripping it from subprocess envs is consistent. Users who need PYTHONHOME for a specific child can set it
# explicitly in the command. PYTHONPATH is NOT included here — it's handled by
# _strip_hermes_owned_pythonpath() which removes only Hermes-owned entries, preserving user-set paths.
_ACTIVE_VENV_MARKER_VARS = ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME")


def _is_hermes_internal_secret(key: str) -> bool:
    """True for Hermes-internal secrets injected under *dynamic* names the static
    blocklist cannot enumerate: ``AUXILIARY_<TASK>_API_KEY``/``_BASE_URL`` (per-task
    side-LLM credentials) and ``GATEWAY_RELAY_*_SECRET``/``_KEY``/``_TOKEN`` (relay
    auth; non-secret routing hints stay visible). Stripped on every spawn path
    regardless of env_passthrough registration or ``inherit_credentials``."""
    upper = key.upper()
    if upper.startswith("AUXILIARY_") and upper.endswith(("_API_KEY", "_BASE_URL")):
        return True
    return upper.startswith("GATEWAY_RELAY_") and upper.endswith(("_SECRET", "_KEY", "_TOKEN"))


# Authorization gates: the env names platform adapters read to decide WHO may talk to the
# agent (allow/deny lists, allow-all opt-ins, bot policy, channel scoping). Not credentials, so
# no secret scrub touches them, and most profiles' ``.env`` files do not define them, so the
# child's own dotenv load never overwrites an inherited value: a child spawned FOR profile B
# from a process that loaded profile A's gates (or a unit-file ``Environment=``) would enforce
# A's channel/user/role list as its own (#113270). Matched by shape so a gate added to any
# adapter is covered without a second edit; ``HERMES_*`` never counts (``HERMES_MEDIA_ALLOW_DIRS``,
# ``HERMES_ALLOW_PRIVATE_URLS`` are process settings, not adapter gates).
_PROFILE_GATE_ENV_MARKERS = (
    "_ALLOWED_", "_ALLOW_ALL_", "_ALLOW_FROM", "_ALLOW_BOTS", "_ALLOW_PUBLIC_", "_IGNORED_CHANNELS",
    "_NO_THREAD_CHANNELS", "_FREE_RESPONSE_CHANNELS", "_BACKFILL_CHANNELS", "_GROUP_ALLOWED",
)


def is_profile_gate_env(name: str) -> bool:
    """True for a platform authorization gate (``DISCORD_ALLOWED_CHANNELS``, ``TELEGRAM_ALLOW_ALL_USERS``,
    ``GATEWAY_ALLOWED_USERS``, ``WHATSAPP_GROUP_ALLOW_FROM`` ...) — profile-scoped policy a child acting
    for ANOTHER profile must never inherit."""
    upper = name.upper()
    if upper.startswith("HERMES_") or upper.startswith("_"):
        return False
    return any(marker in upper for marker in _PROFILE_GATE_ENV_MARKERS)


def strip_profile_gate_env(env: dict) -> dict:
    """Drop every authorization gate from *env* in place (see :func:`is_profile_gate_env`)."""
    for key in [k for k in env if is_profile_gate_env(k)]:
        del env[key]
    return env


def _plugin_terminal_env_strip_keys() -> frozenset:
    """Credential env keys owned by plugin-registered terminal backends (Tier-1:
    stripped from every spawned subprocess). Computed at call time because plugins
    register after import; fail-soft to empty."""
    try:
        from agent.terminal_env_registry import plugin_strip_env_keys

        return plugin_strip_env_keys()
    except Exception:
        return frozenset()


# Tier-1 secrets: stripped from EVERY spawned subprocess even under inherit_credentials
# (claude/codex/gemini). Not provider credentials — no child needs them and they are the
# highest-value secrets to keep from a compromised dependency. Provider keys = Tier 2.
_ALWAYS_STRIP_KEYS: frozenset[str] = frozenset({
    # GitHub auth
    "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID",
    # Gateway / messaging bot tokens and access control
    "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
    # Gateway relay auth triplet. _SECRET/_DELIVERY_KEY are also matched by
    # _is_hermes_internal_secret, but _ID has no secret suffix, so it must be
    # enumerated here to stay stripped on the inherit_credentials=True path.
    "GATEWAY_RELAY_ID", "GATEWAY_RELAY_SECRET", "GATEWAY_RELAY_DELIVERY_KEY",
    "HASS_TOKEN", "EMAIL_PASSWORD", "HERMES_DASHBOARD_SESSION_TOKEN",
    # Remote-compute / infrastructure secrets
    "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "DAYTONA_API_KEY",
})
