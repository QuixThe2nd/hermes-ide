"""Validated main-model assignment write into a profile's config.yaml.

Moved from the removed dashboard's ``web_routers/profiles.py`` /
``web_server_config.py`` / ``web_server_profiles.py`` chain; the TUI gateway's
profile-create flow (``methods_profiles``) is the surviving consumer. The
``/api/model/set``-shaped validation is preserved so a pinned model goes through
the same ``switch_model`` catalog/credential checks as an interactive ``/model``.
"""

from __future__ import annotations

import contextlib
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Optional

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_cli.config import get_process_hermes_home

# One RMW span per process for config.yaml writes.
_CONFIG_MUTATION_LOCK = threading.RLock()


@contextmanager
def _hermes_home_scope(path):
    """Scope config/auth resolution to ``path`` for the block."""
    token = set_hermes_home_override(str(path))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _is_current_profile(profile: Optional[str]) -> bool:
    """None/""/"current" all mean the process's own profile."""
    requested = (profile or "").strip()
    return not requested or requested.lower() == "current"


def _scope_profile_name(path: Path) -> Optional[str]:
    """Map a profile directory onto the query name ``_config_profile_scope`` expects: None for
    the process home (current-profile semantics: launch secret scope, no home override),
    ``"default"`` for the default root (its basename -- ``.hermes`` or a custom root -- is not a
    profile name; launched from ``profiles/<name>`` the root is a *different* profile), the
    directory name for ``profiles/<name>``."""
    from hermes_constants import get_default_hermes_root
    resolved = path.resolve()
    if resolved == get_process_hermes_home().resolve():
        return None
    if resolved == get_default_hermes_root().resolve():
        return "default"
    return path.name


@contextmanager
def _config_profile_scope(profile: Optional[str]):
    """Config-only profile scope: touches ONLY the task-local HERMES_HOME
    contextvar, never process-global module attributes. None/""/"current" = no
    override. Copied from the dashboard's ``web_server_profiles`` (its only
    surviving consumer chain)."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from tui_gateway.launch_profile_policy import activate_multi_profile_hosting, launch_secret_scope

    process_home = get_process_hermes_home()
    if _is_current_profile(profile):
        profile_dir, scoped = None, None  # our own profile: no home override
    else:
        from hermes_cli import profiles as profiles_mod
        name = profile.strip()
        profiles_mod.validate_profile_name(name)
        if not profiles_mod.profile_exists(name):
            raise ValueError(f"Profile '{name}' does not exist.")
        profile_dir = Path(profiles_mod.get_profile_dir(name))
        scoped = None if profile_dir.resolve() == process_home.resolve() else profile_dir
    if scoped is not None:
        activate_multi_profile_hosting()
        hydrate_profile_secret_sources(scoped)  # first call may block on the source's fetch
        secrets = build_profile_secret_scope(scoped)
    else:
        # Our own profile: its launch-env scope (live env + .env while single-profile, so
        # systemd / op-run injection keeps resolving; frozen at activation afterwards).
        secrets = launch_secret_scope(process_home)
    with (_hermes_home_scope(profile_dir) if profile_dir is not None else nullcontext()):
        token = set_secret_scope(secrets)
        try:
            yield scoped
        finally:
            reset_secret_scope(token)


def _normalize_main_model_assignment(provider: str, model: str) -> tuple[str, str]:
    """Normalize a main-slot (provider, model) pair before persisting.

    A picker can send the model's VENDOR prefix as the provider, producing e.g.
    ``provider: anthropic`` + ``default: anthropic/claude-opus-4.6`` — an aggregator slug on
    the native provider. Vendor-name → Hermes-provider repair happens at this single
    chokepoint; see the dashboard-era docstring in git history for the full rationale."""
    from hermes_cli.config import load_config
    from hermes_cli.config import get_compatible_custom_providers
    from hermes_cli.models import _AGGREGATOR_PROVIDERS, _KNOWN_PROVIDER_NAMES, normalize_provider
    from hermes_cli.model_normalize import normalize_model_for_provider
    from hermes_cli.providers import resolve_custom_provider, resolve_user_provider

    prov_in = (provider or "").strip()
    model_in = (model or "").strip()
    canonical = normalize_provider(prov_in)

    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    user_providers = cfg.get("providers")
    declared = resolve_user_provider(
        prov_in, user_providers if isinstance(user_providers, dict) else {}
    ) or resolve_custom_provider(prov_in, get_compatible_custom_providers(cfg))
    if declared is not None:
        return declared.id, model_in

    is_custom_provider_slug = canonical == "custom" or canonical.startswith("custom:")
    if canonical not in _KNOWN_PROVIDER_NAMES and not is_custom_provider_slug and "/" in model_in:
        try:
            cur_cfg = cfg.get("model", {})
            cur_provider = (
                str(cur_cfg.get("provider", "") or "").strip().lower() if isinstance(cur_cfg, dict) else ""
            )
        except Exception:
            cur_provider = ""
        if cur_provider and normalize_provider(cur_provider) in _AGGREGATOR_PROVIDERS:
            canonical = normalize_provider(cur_provider)
            prov_in = cur_provider
        else:
            from hermes_cli.models_detect import provider_has_credentials

            # Only guess OpenRouter when the user actually holds a key for it; otherwise keep the
            # pair as sent rather than persisting a provider they never selected.
            if provider_has_credentials("openrouter"):
                canonical = prov_in = "openrouter"

    if canonical in _KNOWN_PROVIDER_NAMES and not canonical.startswith("custom"):
        try:
            model_in = normalize_model_for_provider(model_in, canonical) or model_in
        except Exception:
            pass

    return prov_in, model_in


def _validated_main_model_selection(
    cfg: dict, provider: str, model: str, base_url: str = "", api_key: str = ""
) -> "Any":
    """Route a main-slot pick through ``switch_model`` (catalog/alias/credential validation)
    seeded with the configured route, exactly like a ``/model <model> --provider <provider>
    --global``. Rejections raise ``ValueError``."""
    from hermes_cli.config import get_compatible_custom_providers
    from hermes_cli.model_switch import switch_model

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    is_bare_custom = provider.strip().lower() in {"custom", "local"}
    result = switch_model(
        raw_input=model, explicit_provider=provider, is_global=True,
        current_provider=str(model_cfg.get("provider") or ""), current_model=str(model_cfg.get("default") or ""),
        current_base_url=base_url if is_bare_custom else str(model_cfg.get("base_url") or ""),
        current_api_key=api_key if is_bare_custom else "",
        user_providers=cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {},
        custom_providers=get_compatible_custom_providers(cfg))
    if not result.success:
        raise ValueError(result.error_message or "model switch rejected")
    return result


def _apply_main_model_assignment(model_cfg: "Any", result: "Any", api_key: str = "") -> dict:
    """Apply a main-slot selection to a ``model`` config dict via the canonical /model shape
    (``hermes_cli.model_switch.apply_model_selection``)."""
    from hermes_cli.model_switch import apply_model_selection

    model_cfg = apply_model_selection(model_cfg, result)
    if api_key.strip():
        model_cfg["api_key"] = api_key.strip()
        model_cfg.pop("api", None)
    return model_cfg


def write_profile_model(profile_dir: Path, provider: str, model: str, validate_in: Optional[Path] = None) -> None:
    """Write the main model assignment into ``profile_dir``'s config.yaml (HERMES_HOME-scoped)
    through the same validated /model shape as an interactive model switch.

    ``validate_in`` is the home whose ``providers:``/``.env``/catalog vouch for the pick (default:
    ``profile_dir`` itself). Profile-create passes the caller's own home: the picker that offered
    the model read THAT catalog, and a just-created profile has no credentials yet, so validating
    in the empty profile rejected every non-env provider (anthropic, ollama, custom).

    Both spans enter ``_config_profile_scope`` (home + secret scope): once a secondary profile has
    been served, ``switch_model``'s ``key_env`` probe goes through ``get_secret``, which fails
    closed without a scope and reports the provider as unconnected."""
    from hermes_cli.config import load_config, save_config
    with _config_profile_scope(_scope_profile_name(validate_in or profile_dir)):
        provider, model = _normalize_main_model_assignment(provider, model)
        result = _validated_main_model_selection(load_config(), provider, model)
    with _config_profile_scope(_scope_profile_name(profile_dir)), _CONFIG_MUTATION_LOCK:  # RMW span
        cfg = load_config()
        cfg["model"] = _apply_main_model_assignment(cfg.get("model", {}), result)
        save_config(cfg)
