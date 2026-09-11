"""Table-driven config migration registry.

Each step is ``_migrate_to_N(results, quiet)``; the version gate and strict ascending order live
in :func:`run_migrations`. Every write goes through ``hermes_cli.config._persist_migration`` so a
step may only persist values that differ from the schema default (plus removals/renames).
"""

from __future__ import annotations

import copy
import functools
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Imported at module level (unlike hermes_cli.config, see the header):
# fallback_config imports nothing beyond typing, so no cycle can form.
from hermes_cli.fallback_config import (
    RETIRED_OX_ALPHA_MODEL,
    RETIRED_OX_ALPHA_PROVIDER,
    is_retired_ox_alpha_route,
)

#: Auto-migration support floor. Configs whose on-disk ``_config_version`` is
#: below this are NOT auto-migrated any more (policy decision, July 2026):
#: v12 predates roughly two years of releases, and carrying the sub-v12
#: migration steps (plus the env bridges they consumed, e.g.
#: HERMES_TOOL_PROGRESS*) forever is not worth it. Below-floor configs are
#: left byte-for-byte untouched — the process continues with the config as-is
#: (defaults deep-merged at read time, matching the non-fatal posture used
#: for unparseable configs) and a clear message tells the user how to
#: proceed. The removed steps were the <12 targets: v4 (tool-progress .env →
#: config.yaml), v5 (timezone seed), v9 (clear ANTHROPIC_TOKEN).
SUPPORT_FLOOR_VERSION = 12


def support_floor_message() -> str:
    """Human-facing explanation shown when a config is below the floor."""
    from hermes_constants import display_hermes_home

    return (
        f"This config predates version {SUPPORT_FLOOR_VERSION} (~2 years old) "
        "and can no longer be auto-migrated. Back up "
        f"{display_hermes_home()}/config.yaml and run `hermes setup` to "
        f"regenerate, or manually set _config_version: {SUPPORT_FLOOR_VERSION} "
        "after reviewing the changelog.")


def _cfg():
    """Return the live ``hermes_cli.config`` module (lazy, cycle-free, monkeypatch-friendly)."""
    from hermes_cli import config

    return config


def read_raw_config():
    return _cfg().read_raw_config()


def _persist_migration(config):
    _cfg()._persist_migration(config)


def _dict_at(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    """``config[key]`` when it is a mapping (same object, so writes alias), else a fresh ``{}``."""
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def _commit(
    config: Dict[str, Any],
    results: Dict[str, Any],
    quiet: bool,
    added: Optional[str],
    message: Optional[str]) -> None:
    """Persist *config*, record *added* under ``config_added`` and print *message* unless quiet."""
    _persist_migration(config)
    if added:
        results["config_added"].append(added)
    if message and not quiet:
        print(message)


def _rewrite_key(
    results: Dict[str, Any],
    quiet: bool,
    *,
    section: str,
    key: str,
    match: Callable[[Any], bool],
    new: Any,
    added: str,
    message: str,
    extra_guard: Callable[[Dict[str, Any]], bool] = lambda _m: True,
    create_section: bool = False) -> None:
    """Rewrite ``<section>.<key>`` to *new* (None = delete) when ``match(current)`` holds; a
    missing section is skipped unless *create_section*."""
    config = read_raw_config()
    raw = config.get(section)
    if not isinstance(raw, dict):
        if not create_section:
            return
        raw = {}
    if match(raw.get(key)) and extra_guard(raw):
        if new is None:
            del raw[key]
        else:
            raw[key] = new
        config[section] = raw
        _commit(config, results, quiet, added, message)


def _rewrite_stale_default(*, old: Any, **kw: Any) -> Callable[[Dict[str, Any], bool], None]:
    """Step rewriting a key only while it still equals the OLD default — never clobbers a value
    the user customized; unset keys inherit the new default at read time."""
    return functools.partial(_rewrite_key, match=lambda cur: cur == old, **kw)


def _lower_is(word: str) -> Callable[[Any], bool]:
    return lambda cur: isinstance(cur, str) and cur.strip().lower() == word


def _migrate_to_12(results: Dict[str, Any], quiet: bool) -> None:
    # 11 → 12: custom_providers list → providers dict.
    _custom_provider_entry_to_provider_config = _cfg()._custom_provider_entry_to_provider_config

    config = read_raw_config()
    custom_list = config.get("custom_providers")
    if not (isinstance(custom_list, list) and custom_list):
        return
    providers_dict = _dict_at(config, "providers")
    migrated_count = 0
    for entry in custom_list:
        if not isinstance(entry, dict):
            continue
        old_name = entry.get("name", "")
        old_url = entry.get("base_url", "") or entry.get("url", "") or entry.get("api", "") or ""
        if not old_url:
            continue

        # kebab-case key from the display name; fall back to the URL hostname.
        key = old_name.strip().lower().replace(" ", "-").replace("(", "").replace(")", "")
        key = re.sub(r"-{2,}", "-", key).strip("-")
        if not key:
            try:
                key = (urlparse(old_url).hostname or "endpoint").replace(".", "-")
            except Exception:
                key = f"endpoint-{migrated_count}"

        # Don't overwrite existing entries
        base_key = key
        suffix = migrated_count
        while key in providers_dict:
            key = f"{base_key}-{suffix}"
            suffix += 1

        new_entry = _custom_provider_entry_to_provider_config(entry, provider_key=key)
        if new_entry is None:
            continue
        if not old_name:
            new_entry.pop("name", None)
        if new_entry.get("api_key") in {"no-key", "no-key-required", ""}:
            new_entry.pop("api_key", None)

        providers_dict[key] = new_entry
        migrated_count += 1

    if migrated_count > 0:
        config["providers"] = providers_dict
        # Runtime reads the list view via get_compatible_custom_providers().
        config.pop("custom_providers", None)
        _persist_migration(config)
        if not quiet:
            print(f"  ✓ Migrated {migrated_count} custom provider(s) to providers: section")
            for key in list(providers_dict.keys())[-migrated_count:]:
                print(f"    → {key}: {providers_dict[key].get('api', '')}")


def _migrate_to_13(results: Dict[str, Any], quiet: bool) -> None:
    # 12 → 13: clear dead LLM_MODEL / OPENAI_MODEL from .env (written by the old setup wizard;
    # nothing reads them — config.yaml is the sole source of truth).
    _c = _cfg()
    for dead_var in ("LLM_MODEL", "OPENAI_MODEL"):
        try:
            if _c.get_env_value(dead_var):
                _c.save_env_value(dead_var, "")
                if not quiet:
                    print(f"  ✓ Cleared {dead_var} from .env (no longer used — config.yaml is source of truth)")
        except Exception:
            pass


_LOCAL_WHISPER_MODELS = frozenset({
    "tiny.en", "tiny", "base.en", "base", "small.en", "small",
    "medium.en", "medium", "large-v1", "large-v2", "large-v3",
    "large", "distil-large-v2", "distil-medium.en",
    "distil-small.en", "distil-large-v3", "distil-large-v3.5",
    "large-v3-turbo", "turbo"})


def _migrate_to_14(results: Dict[str, Any], quiet: bool) -> None:
    # 13 → 14: legacy flat stt.model → provider section. A provider-agnostic `stt.model` fed
    # OpenAI names to faster-whisper ("Invalid model size"). Only the raw (user-written) config
    # decides; a nested model the user already set is never overwritten.
    raw_stt = read_raw_config().get("stt", {})
    if not (isinstance(raw_stt, dict) and "model" in raw_stt):
        return
    legacy_model = raw_stt["model"]
    provider = raw_stt.get("provider", "local")
    config = read_raw_config()
    stt = config.get("stt", {})
    stt.pop("model", None)

    def _place(section: str) -> None:
        existing = raw_stt.get(section, {})
        if not isinstance(existing, dict) or "model" not in existing:
            stt.setdefault(section, {})["model"] = legacy_model

    if provider in {"local", "local_command"}:
        # An OpenAI model name is dropped; the local section already defaults to "base".
        if legacy_model in _LOCAL_WHISPER_MODELS:
            _place("local")
    else:
        _place(provider)
    config["stt"] = stt
    _commit(
        config, results, quiet, None, "  ✓ Migrated legacy stt.model to provider-specific config")


def _migrate_to_16(results: Dict[str, Any], quiet: bool) -> None:
    # 15 → 16: display.tool_progress_overrides → display.platforms.<plat>.tool_progress.
    config = read_raw_config()
    display = _dict_at(config, "display")
    old_overrides = display.get("tool_progress_overrides")
    if not (isinstance(old_overrides, dict) and old_overrides):
        return
    platforms = _dict_at(display, "platforms")
    for plat, mode in old_overrides.items():
        if plat not in platforms:
            platforms[plat] = {}
        if "tool_progress" not in platforms[plat]:
            platforms[plat]["tool_progress"] = mode
    display["platforms"] = platforms
    config["display"] = display
    migrated = ", ".join(f"{p}={m}" for p, m in old_overrides.items())
    _commit(
        config, results, quiet,
        "display.platforms (migrated from tool_progress_overrides)",
        f"  ✓ Migrated tool_progress_overrides → display.platforms: {migrated}")


def _migrate_to_17(results: Dict[str, Any], quiet: bool) -> None:
    # 16 → 17: remove legacy compression.summary_* keys; non-empty, non-default values move to
    # auxiliary.compression without overriding an explicit (non-"auto") aux value.
    config = read_raw_config()
    comp = config.get("compression", {})
    if not isinstance(comp, dict):
        return
    legacy = {k: comp.pop(f"summary_{k}", None) for k in ("model", "provider", "base_url")}
    migrated_keys = []
    for k, raw in legacy.items():
        val = str(raw).strip() if raw else ""
        if not val or (k == "provider" and val == "auto"):
            continue
        aux_comp = config.setdefault("auxiliary", {}).setdefault("compression", {})
        cur = aux_comp.get(k)
        if not cur or (k == "provider" and cur == "auto"):
            aux_comp[k] = val
            migrated_keys.append(f"{k}={raw}")
    if migrated_keys or any(v is not None for v in legacy.values()):
        config["compression"] = comp
        message = (
            "  ✓ Migrated compression.summary_* → auxiliary.compression: "
            f"{', '.join(migrated_keys)}"
            if migrated_keys else "  ✓ Removed unused compression.summary_* keys")
        _commit(config, results, quiet, None, message)


def _installed_user_plugins(disabled: set) -> List[str]:
    """Names of plugins under ``$HERMES_HOME/plugins/`` with a manifest, minus *disabled*."""
    _c = _cfg()
    found: List[str] = []
    try:
        user_plugins_dir = _c.get_hermes_home() / "plugins"
        if user_plugins_dir.is_dir():
            for child in sorted(user_plugins_dir.iterdir()):
                if not child.is_dir():
                    continue
                manifest_file = child / "plugin.yaml"
                if not manifest_file.exists():
                    manifest_file = child / "plugin.yml"
                if not manifest_file.exists():
                    continue
                try:
                    with open(manifest_file, encoding="utf-8") as _mf:
                        manifest = _c.fast_safe_load(_mf) or {}
                except Exception:
                    manifest = {}
                name = manifest.get("name") or child.name
                if name not in disabled:
                    found.append(name)
    except Exception:
        return []
    return found


def _migrate_to_21(results: Dict[str, Any], quiet: bool) -> None:
    # 20 → 21: plugins are now opt-in (loader requires ``plugins.enabled``). Grandfather installed
    # user plugins not already disabled; bundled plugins ship off and need explicit opt-in.
    config = read_raw_config()
    plugins_cfg = _dict_at(config, "plugins")
    if "enabled" in plugins_cfg:
        return
    disabled = plugins_cfg.get("disabled", []) or []
    grandfathered = _installed_user_plugins(set(disabled) if isinstance(disabled, list) else set())
    plugins_cfg["enabled"] = grandfathered
    config["plugins"] = plugins_cfg
    message = (
        f"  ✓ Plugins now opt-in: grandfathered "
        f"{len(grandfathered)} existing plugin(s) into plugins.enabled"
        if grandfathered else
        "  ✓ Plugins now opt-in: no existing plugins to grandfather. "
        "Use `hermes plugins enable <name>` to activate.")
    _commit(
        config, results, quiet,
        f"plugins.enabled (opt-in allow-list, {len(grandfathered)} grandfathered)", message)


def _migrate_to_23(results: Dict[str, Any], quiet: bool) -> None:
    # 22 → 23: seed curator defaults + create logs/curator/. Older configs never wrote the curator
    # section; deep-merge made it work but users could not see/edit it and `hermes curator status`
    # had no stable logs dir. Only keys the user hasn't set are written.
    _c = _cfg()
    DEFAULT_CONFIG = _c.DEFAULT_CONFIG

    try:
        curator_dir = _c.get_hermes_home() / "logs" / "curator"
        curator_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        results["warnings"].append(f"Could not create {curator_dir}: {e}")

    config = read_raw_config()

    def _seed_missing(section: Dict[str, Any], defaults: Dict[str, Any]) -> List[str]:
        added = [k for k in defaults if k not in section]
        for k in added:
            section[k] = copy.deepcopy(defaults[k])
        return added

    raw_curator = _dict_at(config, "curator")
    added_curator = _seed_missing(raw_curator, DEFAULT_CONFIG.get("curator", {}))
    if added_curator:
        config["curator"] = raw_curator

    raw_aux = _dict_at(config, "auxiliary")
    raw_aux_curator = _dict_at(raw_aux, "curator")
    added_aux = _seed_missing(
        raw_aux_curator, DEFAULT_CONFIG.get("auxiliary", {}).get("curator", {}))
    if added_aux:
        raw_aux["curator"] = raw_aux_curator
        config["auxiliary"] = raw_aux

    if added_curator or added_aux:
        _persist_migration(config)
        for label, added in (("curator", added_curator), ("auxiliary.curator", added_aux)):
            if not added:
                continue
            results["config_added"].append(f"{label} ({len(added)} default key(s))")
            if not quiet:
                print(
                    f"  ✓ {'Curator' if label == 'curator' else label} settings now available "
                    f"({', '.join(added)}) — edit via `hermes config set`")


def _migrate_to_29(results: Dict[str, Any], quiet: bool) -> None:
    # 28 → 29: memory/skills tri-state write_mode (on|off|approve) → boolean write_approval.
    # Only "approve" carried gating intent → true; the old "off = block writes" mode is dropped
    # (memory_enabled: false disables memory). Only a persisted key is rewritten.
    config = read_raw_config()
    touched = False
    for subsystem in ("memory", "skills"):
        sub = config.get(subsystem)
        if not isinstance(sub, dict) or "write_mode" not in sub:
            continue
        old = sub.pop("write_mode")
        old_norm = old.strip().lower() if isinstance(old, str) else old
        sub["write_approval"] = (old_norm == "approve")
        config[subsystem] = sub
        touched = True
        results["config_added"].append(
            f"{subsystem}.write_mode → write_approval={sub['write_approval']}")
    if touched:
        _commit(config, results, quiet, None,
                "  ✓ Renamed write_mode → write_approval (boolean gate)")


# 29 → 30 (curator.consolidate defaults to false) is schema-default-only: deep-merge supplies it
# at read time and persisting a default would only bloat a lean config. No registry entry.


def _migrate_to_33(results: Dict[str, Any], quiet: bool) -> None:
    # 32 → 33: max_async_children is deprecated; fold a raised value into max_concurrent_children
    # (take the max so nobody loses headroom), then drop it.
    config = read_raw_config()
    raw_deleg = config.get("delegation")
    if not (isinstance(raw_deleg, dict) and "max_async_children" in raw_deleg):
        return
    old_async = raw_deleg.pop("max_async_children")
    try:
        old_async_i = int(old_async)
    except (TypeError, ValueError):
        old_async_i = None
    if old_async_i is not None and old_async_i > 3:
        try:
            cur_children = int(raw_deleg.get("max_concurrent_children", 3))
        except (TypeError, ValueError):
            cur_children = 3
        if old_async_i > cur_children:
            raw_deleg["max_concurrent_children"] = old_async_i
            results["config_added"].append(
                f"delegation.max_concurrent_children={old_async_i} "
                f"(folded from deprecated max_async_children)")
    config["delegation"] = raw_deleg
    _commit(
        config, results, quiet, None,
        "  ✓ Removed deprecated delegation.max_async_children — "
        "delegation.max_concurrent_children now caps background "
        "delegations too.")


def _migrate_to_34(results: Dict[str, Any], quiet: bool) -> None:
    # 33 → 34: one-time personality reset. Persistence used to be split (TUI/desktop wrote the
    # NAME to display.personality, CLI/gateway wrote rendered TEXT to agent.system_prompt), so
    # once display.personality became authoritative, stale names resurrected personalities users
    # had turned off. Reset display.personality → "" and scrub agent.system_prompt ONLY when it
    # verbatim-equals a known personality's rendered text; any other text is user-owned.
    from hermes_cli.personality import (
        available_personalities, normalize_personality_name, prompt_text, render_personality_prompt)

    config = read_raw_config()
    touched = False

    raw_display = config.get("display")
    old_name = ""
    if isinstance(raw_display, dict):
        old_name = normalize_personality_name(raw_display.get("personality", ""))
        if old_name:
            raw_display["personality"] = ""
            config["display"] = raw_display
            touched = True

    raw_agent = config.get("agent")
    scrubbed_text = False
    if isinstance(raw_agent, dict):
        manual = prompt_text(raw_agent.get("system_prompt", ""))
        if manual:
            rendered = {
                render_personality_prompt(defn) for defn in available_personalities(config).values()
            }
            if manual in rendered:
                raw_agent["system_prompt"] = ""
                config["agent"] = raw_agent
                touched = True
                scrubbed_text = True

    if not touched:
        return
    _commit(config, results, quiet, "display.personality=none (one-time reset)", None)
    if quiet:
        return
    if old_name:
        print(
            f"  ✓ Personality reset to none (was '{old_name}'). Personality "
            "state was previously saved inconsistently across surfaces and "
            "could re-enable a personality you had turned off. "
            f"Run /personality {old_name} to turn it back on.")
    if scrubbed_text:
        print(
            "  ✓ Removed personality text from agent.system_prompt (written "
            "by an older /personality). That field is now reserved for "
            "manual system prompts; personalities live in display.personality.")


def _migrate_to_38(results: Dict[str, Any], quiet: bool) -> None:
    # 37 → 38: the bundled observability/nemo_relay plugin was removed (Relay lifecycle moved
    # into the agent core); drop it from plugins.enabled.
    from hermes_cli.relay_plugin_cutover import legacy_relay_plugin_keys

    config = read_raw_config()
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return
    enabled = plugins.get("enabled")
    removed = legacy_relay_plugin_keys(enabled)
    if not removed or not isinstance(enabled, list):
        return

    plugins["enabled"] = [value for value in enabled if value not in removed]
    config["plugins"] = plugins
    _persist_migration(config)
    message = (
        "Removed legacy Relay plugin from plugins.enabled: "
        f"{', '.join(removed)}. Configure native Relay plugins with "
        "HERMES_NEMO_RELAY_PLUGINS_TOML.")
    results["warnings"].append(message)
    if not quiet:
        print(f"  ⚠ {message}")


def _migrate_to_39(results: Dict[str, Any], quiet: bool) -> None:
    # 38 → 39: strip the retired `bfl` toolset wherever a backfill/picker save wrote it, so stale
    # config can't resurrect an unknown toolset.
    config = read_raw_config()
    changed = False
    for section in ("platform_toolsets", "known_builtin_toolsets"):
        mapping = config.get(section)
        if not isinstance(mapping, dict):
            continue
        for platform, toolsets in mapping.items():
            if isinstance(toolsets, list) and "bfl" in toolsets:
                mapping[platform] = [ts for ts in toolsets if ts != "bfl"]
                changed = True
        if changed:
            config[section] = mapping
    if changed:
        _commit(
            config, results, quiet,
            "removed retired 'bfl' toolset from saved toolset lists",
            "  ✓ Removed the retired BFL FLUX 3 toolset from saved toolset "
            "lists — video generation now lives under `hermes tools` → "
            "Video Generation (Nous Subscription or FAL).")


def _migrate_to_41_soul_cleanup(results: Dict[str, Any], quiet: bool) -> None:
    # 40 → 41: drop the plugin-era "## Messaging other agents" append from every SOUL.md. The
    # server injects the live Bot Mode section in Bot Chat sessions; the frozen SOUL copy taxed
    # every other session (~600 tok) and shadowed the live roster in Bot Chat itself.
    from hermes_constants import get_hermes_home
    from tools.bot_mode_probe import _PROTOCOL_HEADING, _hermes_root, _roster, strip_legacy_protocol

    cleaned: List[str] = []
    for name, profile_dir in _roster(_hermes_root(get_hermes_home())):
        soul = profile_dir / "SOUL.md"
        try:
            text = soul.read_text(encoding="utf-8") if soul.is_file() else ""
            if _PROTOCOL_HEADING in text:
                soul.write_text(strip_legacy_protocol(text), encoding="utf-8")
                cleaned.append(name)
        except OSError:
            continue
    if cleaned:
        results["config_added"].append(f"removed legacy Bot Mode section from SOUL.md ({', '.join(cleaned)})")
        if not quiet:
            print(f"  ✓ Removed the plugin-era 'Messaging other agents' section from SOUL.md "
                  f"({', '.join(cleaned)}) — Bot Chat sessions now get the live roster instead.")


def _migrate_to_40(results: Dict[str, Any], quiet: bool) -> None:
    # ── Version 39 → 40: model_catalog.ttl_hours → ttl_minutes (default 20) ──
    # The picker catalogs now refresh every 20 minutes (and the gateway
    # refreshes them in the background on that cadence). Only the OLD default
    # (ttl_hours: 1, written by the v25 migration) is dropped so the new
    # default applies; any other explicit ttl_hours is a deliberate choice
    # and stays honoured by the loader.
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration

    config = read_raw_config()
    raw_mc = config.get("model_catalog")
    if isinstance(raw_mc, dict) and raw_mc.get("ttl_hours") == 1 and "ttl_minutes" not in raw_mc:
        del raw_mc["ttl_hours"]
        config["model_catalog"] = raw_mc
        _persist_migration(config)
        results["config_added"].append("model_catalog.ttl_hours 1 → ttl_minutes 20 (default)")
        if not quiet:
            print("  ✓ Model catalog now refreshes every 20 minutes (model_catalog.ttl_minutes)")


# ── Retired Ox Alpha preview route ─────────────────────────────────────
# openrouter/stealth/ox-alpha was a one-week experiment whose server-side
# preview has ended: the route still resolves but every request fails, and
# the quota plugins no longer give it synthetic unlimited-wallet treatment.
# The v41 migration strips it from existing configs so upgraded installs
# stop routing into a dead model. The route identity (and the shared
# predicate the session-resume paths consult) lives in
# hermes_cli.fallback_config — module-level import is safe here: that
# module imports nothing beyond typing, so the deliberate absence of a
# module-level ``hermes_cli.config`` import (see the header) is preserved.

#: Fallback-entry fields that keep meaning when the entry graduates to the
#: primary ``model:`` section (endpoint, credential reference, per-route
#: replay policy). Everything else on a fallback entry is fallback-specific.
_PROMOTABLE_ROUTE_FIELDS = ("base_url", "api_mode", "api_key", "key_env", "api_key_env", "reasoning_echo")

#: Model-section keys owned by the primary route itself: the model id under
#: the canonical ``default`` key and its legacy aliases, the provider, the
#: ``api``/``api_base`` endpoint-credential aliases, and every promotable
#: route field. Promotion replaces these and preserves everything else —
#: an upgrade must not reset unrelated model-level controls such as
#: ``streaming``, ``max_tokens``, ``context_length``, ``default_headers``,
#: ``lmstudio_load_mode``, or ``openai_runtime``.
_ROUTE_OWNED_MODEL_KEYS = (
    ("default", "model", "name", "provider", "api", "api_base")
    + _PROMOTABLE_ROUTE_FIELDS
)


def _endpoint_url(holder: Any) -> Any:
    """Endpoint a route mapping carries, with the ``api_base`` alias applied.

    ``api_base`` → ``base_url`` is normalized at the load/save chokepoint
    (``_normalize_root_model_keys``, issue #8919) — fallback-only, never
    overriding an explicit ``base_url``. Retirement classification must look
    through the same alias: a custom endpoint named the intuitive way loads
    as that endpoint, and retiring it as the inferred OpenRouter route would
    destroy a working manual route.
    """
    if not isinstance(holder, dict):
        return None
    base_url = holder.get("base_url")
    if base_url:
        return base_url
    return holder.get("api_base")


def _classification_view(raw: Any) -> Any:
    """Deep-copied, env-expanded view of *raw* for route classification ONLY.

    Runtime ``load_config()`` resolves ``${VAR}`` / ``${env:VAR}`` references
    through ``hermes_cli.config._expand_env_vars``, so a route whose provider,
    model id, or endpoint is written as a reference LOADS as whatever the
    environment names. Classifying the raw strings instead would read a
    different route than the one that runs: a v40 config whose model id is
    ``${RETIRED_MODEL}`` with the variable set to ``stealth/ox-alpha`` would
    be stamped v41 while the retired route stayed active (same for a fallback
    entry, and for an endpoint reference pointing at openrouter.ai). The view
    reuses that same expander — no second interpolation syntax — and is a
    disposable copy: expansion happens for classification only and is NEVER
    serialized, so surviving raw objects keep their ``${VAR}`` templates
    verbatim. Unresolved references come back literal from ``_expand_env_vars``
    and therefore classify conservatively: a route that cannot be proven to
    resolve to the retired one survives.
    """
    from hermes_cli.config import _expand_env_vars

    return _expand_env_vars(copy.deepcopy(raw))


def _is_retired_ox_alpha_entry(entry: Any) -> bool:
    """True only for the exact retired ``openrouter/stealth/ox-alpha`` route.

    Classified on an env-expanded copy of the entry (``_classification_view``)
    so a ``${VAR}``-written provider/model/endpoint matches the route it
    resolves to at runtime — while the raw entry itself is left untouched.

    Provider and model are case/whitespace normalized, so ``OpenRouter`` /
    `` Stealth/OX-Alpha `` also matches — but no other openrouter model
    (and no other provider) ever does. An ``auto`` provider — or an omitted
    one, for raw collection entries ``get_fallback_chain`` never sees —
    names the same route too when the model id is the retired one and no
    custom endpoint re-routes it: the runtime resolves that
    vendor-namespaced id to OpenRouter. A custom ``base_url``/``api_base``
    endpoint keeps the entry alive, exactly as for the primary route.
    """
    if not isinstance(entry, dict):
        return False
    view = _classification_view(entry)
    return is_retired_ox_alpha_route(
        view.get("provider"), view.get("model"), _endpoint_url(view)
    )


def _normalized_primary_route(config: Any) -> Tuple[str, str, Any]:
    """Classify the primary route from a NORMALIZED COPY of the raw config.

    Returns ``(model_id, provider, endpoint)`` — the route the config actually
    LOADS as, computed with the same root/nested/alias semantics
    ``_normalize_root_model_keys`` applies at the load/save chokepoint:

    * a scalar root ``model:`` is the id of an otherwise-empty model section;
    * the id is read under the canonical ``default`` key, then the legacy
      ``model``/``name`` aliases, and a dict-valued id is flattened with the
      canonical ``split_model_config_default`` splitter (the same one
      ``_get_model_config`` uses at load time);
    * provider precedence: an explicit ``model.provider`` wins, else the
      nested default's provider, else the outer/merged default — and a
      ROOT-level ``provider:`` folds in first whenever the model section
      carries none, exactly as the load-time normalizer does. Without that
      fold a supported legacy shape (``model: <retired id>`` + root
      ``provider: custom:local``) would classify from the model id alone and
      retire the user's custom route;
    * the endpoint follows the loader's precedence — ``model.base_url``, then
      a root ``base_url``, then the ``api_base`` alias inside the model
      section, then a root ``api_base`` — because a root-level endpoint (or
      its alias) is what makes the runtime resolve a custom endpoint instead
      of inferring OpenRouter.

    The input is first mapped through ``_classification_view`` (a deep-copied,
    env-expanded view), so a route written with ``${VAR}`` references is read
    as the route load_config() actually serves. The view exists ONLY for
    classification: nothing here writes back, so a valid custom route named in
    a legacy shape (or through a template) is never destructively rewritten
    merely to be recognized.
    """
    if not isinstance(config, dict):
        return ("", "", None)
    config = _classification_view(config)
    raw_model = config.get("model")
    if isinstance(raw_model, dict):
        model_section: Dict[str, Any] = dict(raw_model)
    elif raw_model not in (None, ""):
        model_section = {"default": raw_model}
    else:
        model_section = {}

    raw_id: Any = None
    for key in ("default", "model", "name"):
        if model_section.get(key) not in (None, ""):
            raw_id = model_section.get(key)
            break
    from hermes_cli.config import split_model_config_default

    model_id, nested_provider = split_model_config_default(raw_id)

    outer_provider = str(model_section.get("provider") or "").strip()
    if nested_provider and (not outer_provider or outer_provider.lower() == "auto"):
        # Nested provider wins over an absent/``auto`` outer one — the
        # load-time flattener's rule.
        provider: str = nested_provider
    else:
        provider = outer_provider
    if not provider:
        # Root-level ``provider:`` folds into the model section at load time
        # only when the model section still carries none — same gate here.
        provider = str(config.get("provider") or "").strip()

    # Endpoint, in the loader's precedence order: model.base_url, then the
    # root-level fold-in, then the api_base alias (root before the model
    # section, matching _normalize_root_model_keys' alias loop).
    endpoint = (
        model_section.get("base_url")
        or config.get("base_url")
        or config.get("api_base")
        or model_section.get("api_base")
    )
    return (str(model_id or "").strip(), provider, endpoint)


def _primary_routes_retired_model(config: Any) -> bool:
    """True when the primary route the raw config loads as is the retired one.

    OpenRouter's ``vendor/model`` id namespacing is what makes the bare id
    ``stealth/ox-alpha`` resolvable at all, so a bare primary string — or a
    model section whose id (``default``/``model``/``name``) is the retired
    model with the provider omitted, or explicitly ``auto`` — infers the same
    OpenRouter route an explicit ``provider: openrouter`` pins. A named
    custom provider serving the same model id is a different route and is
    never matched here; so is a route whose custom ``base_url`` (or its
    ``api_base`` alias) makes the runtime resolve a user-owned endpoint
    (e.g. a local server) instead of inferring OpenRouter — v41 must
    preserve that route, for the modern nested shape AND the legacy
    root-level one (``_normalized_primary_route`` folds both in).

    A dict-valued id (``model.default: {provider: ..., model: ...}``) is
    flattened with the canonical ``split_model_config_default`` splitter —
    the same one ``_get_model_config`` uses at load time — so the supported
    nested shape is recognized here too and cannot slip past the scrub only
    to be flattened back into the dead route after the version is stamped.
    Provider precedence mirrors ``_normalize_root_model_keys``' flattening:
    the nested provider wins only over an absent or ``auto`` outer
    provider — an explicitly configured outer provider pins the
    manual route the config actually loads as, which is never the retired
    one, so it must survive the scrub untouched. ``auto`` is matched
    case-insensitively after whitespace trim, the same normalization every
    route consumer applies (``resolve_requested_provider``,
    ``is_retired_ox_alpha_route``): a padded ``" AUTO "`` is the merged
    default, not an explicit provider — gating on it case-sensitively would
    hand the classifier an ``auto`` it then resolves by inference,
    scrubbing the manual nested route it was supposed to protect.
    """
    model_id, provider, endpoint = _normalized_primary_route(config)
    return is_retired_ox_alpha_route(provider, model_id, endpoint)


def _canonicalize_outer_auto_sentinel(config: Dict[str, Any]) -> bool:
    """Canonicalize a legacy outer ``model.provider`` auto sentinel in place.

    True when the sentinel was rewritten. The load-time flattener
    (``_normalize_root_model_keys``) lets a nested ``model.default``
    provider win only over an absent outer provider or the EXACT string
    ``auto`` — every other spelling counts as explicit. So a legacy
    sentinel like ``" AUTO "`` (trim+case-normalizing to auto, exactly as
    every route consumer reads it) defeats the flattening: the config
    loads with the sentinel as the provider, runtime resolution normalizes
    it to auto and infers OpenRouter for the retired vendor-namespaced id,
    and the custom provider the user actually configured never routes.
    v41's classifier already normalizes trim+case (see
    ``_primary_routes_retired_model``) and correctly protects that manual
    route — but it then persisted the broken sentinel verbatim. Rewriting
    it to exact lowercase ``auto`` — the only change; the nested shape is
    left for the loader to flatten, never replaced here — makes the
    migrated file load as the route the classifier protected.

    Narrowly scoped: only the retired Ox Alpha model id, only an outer
    provider that trim+case normalizes to auto without already being the
    exact canonical spelling (a no-op rewrite must not force a persist),
    and only an explicit nested provider that is neither auto nor
    OpenRouter — the inferred/explicit retired shapes keep their existing
    classification paths untouched. The retired-id test reads the same
    env-expanded classification view the primary classifier uses, so a
    nested id written as ``${VAR}`` is scoped by the model it resolves to;
    the rewrite itself still touches only the sentinel key on the RAW dict.
    """
    model = config.get("model")
    if not isinstance(model, dict):
        return False
    if str(model.get("provider") or "").strip().lower() != "auto":
        return False
    if model.get("provider") == "auto":
        return False
    view = _classification_view(model)
    raw_id: Any = None
    for key in ("default", "model", "name"):
        if view.get(key) not in (None, ""):
            raw_id = view.get(key)
            break
    from hermes_cli.config import split_model_config_default

    nested_model, nested_provider = split_model_config_default(raw_id)
    if nested_model.lower() != RETIRED_OX_ALPHA_MODEL:
        return False
    if nested_provider.lower() in ("", "auto", RETIRED_OX_ALPHA_PROVIDER):
        return False
    model["provider"] = "auto"
    return True


def _emptied_model_config(previous: Any, unset_value: Any) -> Any:
    """Retire a dict primary's route without deleting its other controls.

    The no-survivor branch used to assign ``DEFAULT_CONFIG["model"]`` — the
    scalar default ``''`` — to a dict-shaped ``model:`` section, deleting
    every route-independent control the user had set alongside the route:
    ``streaming``, ``max_tokens``, ``context_length``, ``default_headers``,
    ``lmstudio_load_mode``, ``openai_runtime``, and any unknown extension.
    The retirement owns the ROUTE, not the section: keep the dict shape when
    there was one, drop exactly the route-owned keys (``_ROUTE_OWNED_MODEL_KEYS``)
    — the id and its legacy aliases, the provider, the endpoint aliases, and
    the promotable route fields — and set only the canonical ``default`` key
    to the unset value (currently ``''``). A scalar primary keeps the scalar
    default it always collapsed to.
    """
    if not isinstance(previous, dict):
        return unset_value
    emptied = {
        key: value
        for key, value in previous.items()
        if key not in _ROUTE_OWNED_MODEL_KEYS
    }
    emptied["default"] = unset_value
    return emptied


def _filter_fallback_collection(
    raw: Any, predicate: Callable[[Any], bool]
) -> Tuple[Any, int]:
    """Drop entries matching *predicate* from one fallback collection.

    Order and shape are preserved: a list-shaped collection stays a list of
    its surviving entries (in order), and a dict-shaped single-entry
    collection whose entry matched returns ``None`` so the caller removes
    the key — an empty dict is not a valid legacy fallback shape.
    """
    if isinstance(raw, list):
        kept = [entry for entry in raw if not predicate(entry)]
        return kept, len(raw) - len(kept)
    if isinstance(raw, dict) and predicate(raw):
        return None, 1
    return raw, 0


def _promoted_model_config(previous: Any, promotee: Dict[str, Any]) -> Dict[str, Any]:
    """Build the promoted ``model`` section from the retired primary's one.

    Starts from the previous dict (when the primary was dict-shaped) and
    replaces only the route-owned keys (``_ROUTE_OWNED_MODEL_KEYS``): the id
    becomes the promotee's model under the canonical ``default`` key — a
    nested or aliased previous id is flattened/removed, never left behind as
    the retired route — the provider becomes the promotee's, and each
    promotable route field is set from the promotee or dropped when the
    promotee doesn't carry it, so the retired route's endpoint cannot leak
    onto the promoted one. The endpoint itself is set from the promotee's
    EFFECTIVE one — ``_endpoint_url``'s precedence, so a promotee that names
    it under the legacy ``api_base`` alias keeps its custom endpoint,
    persisted under the canonical ``base_url`` without a duplicate alias
    key. Every non-route field (``streaming``, ``max_tokens``,
    ``context_length``, ``default_headers``, ``lmstudio_load_mode``,
    ``openai_runtime``, …) is preserved as-is. A string-shaped or absent
    previous primary yields the fresh minimal dict.
    """
    new_model: Dict[str, Any] = {}
    if isinstance(previous, dict):
        new_model = {
            key: value
            for key, value in previous.items()
            if key not in _ROUTE_OWNED_MODEL_KEYS
        }
    new_model["default"] = promotee["model"]
    new_model["provider"] = promotee["provider"]
    for field in _PROMOTABLE_ROUTE_FIELDS:
        if field == "base_url":
            # Set below through the alias-aware precedence — a plain copy
            # would drop the endpoint of a promotee that names it api_base.
            continue
        if field in promotee:
            new_model[field] = promotee[field]
    effective_endpoint = _endpoint_url(promotee)
    if effective_endpoint:
        new_model["base_url"] = effective_endpoint
    return new_model


def _migrate_to_41(results: Dict[str, Any], quiet: bool) -> None:
    # ── Version 40 → 41: retire the openrouter/stealth/ox-alpha route ──
    # See the retired-route section above. Three scrub rules, plus one
    # canonicalization:
    #
    # * retired fallback entries are removed from BOTH fallback_providers
    #   (modern list shape) and fallback_model (legacy dict/list shape),
    #   preserving order, shape, and every unrelated entry — the route-aware
    #   predicate also catches auto/omitted-provider entries that resolve to
    #   OpenRouter, while custom-endpoint ones survive;
    # * a retired primary promotes the first surviving fallback in effective
    #   chain order — fallback_providers first, then fallback_model, exactly
    #   as hermes_cli.fallback_config.get_fallback_chain defines it — into
    #   model.default, carrying its provider and route metadata (route-owned
    #   keys only — unrelated model-level controls survive the promotion),
    #   and that route's occurrences leave the fallback collections so it is
    #   not simultaneously primary and fallback #1;
    # * a retired primary with no surviving fallback is unset to the
    #   DEFAULT_CONFIG empty model value with a warning to run
    #   `hermes model` — a replacement provider/credential is never guessed.
    #   Only the route is unset: a dict-shaped section keeps every
    #   route-independent control and gets just ``default: ''``; a scalar
    #   primary collapses to the scalar default. Every route field here is
    #   classified through the env-expanded view load_config() serves, so a
    #   ``${VAR}``-written route retires (or survives) as the route it
    #   actually resolves to — never the literal string;
    # * a legacy padded/case-variant outer ``model.provider`` auto sentinel on
    #   the retired-model nested shape is canonicalized to exact ``auto``
    #   BEFORE classification, so what v41 persists is a sentinel the
    #   load-time flattener recognizes and the nested custom provider the
    #   classifier protected actually routes.
    #
    # A config without the route is left untouched (no persist here — the
    # driver's normal version stamp is the only write), which also makes the
    # step idempotent.
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration

    from hermes_cli.fallback_config import _entry_identity, get_fallback_chain

    config = read_raw_config()

    canonicalized_auto_sentinel = _canonicalize_outer_auto_sentinel(config)
    primary_retired = _primary_routes_retired_model(config)

    # Effective chain (fallback_providers in order, then legacy
    # fallback_model, deduped by route identity) minus the retired route.
    surviving_chain = [
        entry
        for entry in get_fallback_chain(config)
        if not _is_retired_ox_alpha_entry(entry)
    ]
    promotee = surviving_chain[0] if primary_retired and surviving_chain else None
    promotee_identity = _entry_identity(promotee) if promotee else None

    def _drops(entry: Any) -> bool:
        if _is_retired_ox_alpha_entry(entry):
            return True
        return (
            promotee_identity is not None
            and isinstance(entry, dict)
            and _entry_identity(entry) == promotee_identity
        )

    removed_retired = 0
    touched = canonicalized_auto_sentinel
    for key in ("fallback_providers", "fallback_model"):
        raw_value = config.get(key)
        # Count only retired-route removals for the report; the promotee's
        # departure from its collection is reported by the promotion line.
        _, retired_here = _filter_fallback_collection(
            raw_value, _is_retired_ox_alpha_entry
        )
        removed_retired += retired_here
        filtered, removed = _filter_fallback_collection(raw_value, _drops)
        if removed:
            touched = True
            if filtered is None:
                config.pop(key, None)
            else:
                config[key] = filtered

    if primary_retired:
        touched = True
        if promotee is not None:
            config["model"] = _promoted_model_config(config.get("model"), promotee)
        else:
            # No survivor: unset the route, not the section. A dict primary
            # keeps every non-route key it carried (streaming, max_tokens,
            # default_headers, …) and only its canonical ``default`` becomes
            # the DEFAULT_CONFIG empty value; a scalar primary collapses to
            # that scalar default exactly as before.
            config["model"] = _emptied_model_config(
                config.get("model"), _c.DEFAULT_CONFIG["model"]
            )

    if not touched:
        return

    _persist_migration(config)
    if removed_retired:
        results["config_added"].append(
            "removed retired 'openrouter/stealth/ox-alpha' fallback route(s)"
        )
    if canonicalized_auto_sentinel:
        results["config_added"].append(
            "model.provider 'auto' sentinel canonicalized (nested default provider preserved)"
        )
        if not quiet:
            print(
                "  ✓ Canonicalized the outer model.provider 'auto' sentinel — "
                "the nested default's custom provider now applies."
            )
    if promotee is not None:
        results["config_added"].append(
            f"model.default={promotee['model']} (provider {promotee['provider']}, "
            "promoted from fallback — Ox Alpha retired)"
        )
        if not quiet:
            print(
                "  ✓ Retired Ox Alpha primary (openrouter/stealth/ox-alpha) — "
                f"promoted fallback {promotee['provider']}/{promotee['model']} "
                "to primary."
            )
    elif primary_retired:
        message = (
            "Primary model openrouter/stealth/ox-alpha was retired (the Ox "
            "Alpha preview ended) and no fallback model remained. Run "
            "`hermes model` to choose a new primary model."
        )
        results["warnings"].append(message)
        if not quiet:
            print(f"  ⚠ {message}")
    if removed_retired and not quiet:
        print(
            "  ✓ Removed the retired Ox Alpha route "
            "(openrouter/stealth/ox-alpha) from the fallback chain."
        )


# v42: advance the Codex rotation default gpt-5.6-sol → gpt-6-astra, rewriting
# only the exact canonical pair (``model.default`` plus matching
# ``fallback_providers`` entries) in place — no legacy/normalized/inferred
# shapes. Unmatched configs are not persisted (idempotent; Sol stays choosable).


def _migrate_to_42(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    config = _c.read_raw_config()
    model_section = config.get("model")
    fallbacks = config.get("fallback_providers")
    routes = [(model_section, "default")] if isinstance(model_section, dict) else []
    if isinstance(fallbacks, list):
        routes += [(entry, "model") for entry in fallbacks if isinstance(entry, dict)]
    touched = False
    for entry, key in routes:
        if entry.get("provider") == "openai-codex" and entry.get(key) == "gpt-5.6-sol":
            entry[key] = "gpt-6-astra"
            touched = True
    if not touched:
        return
    _c._persist_migration(config)
    results["config_added"].append("openai-codex rotation default gpt-5.6-sol → gpt-6-astra")
    if not quiet:
        print("  ✓ Codex rotation default: gpt-5.6-sol → gpt-6-astra (Sol stays selectable).")


#: Registry of (target_version, migration_fn), strictly ascending. The driver
#: applies every entry whose target version is greater than the on-disk
#: observe earlier steps' writes via read_raw_config() (filesystem state).
MIGRATIONS: Tuple[Tuple[int, Callable[[Dict[str, Any], bool], None]], ...] = (
    (12, _migrate_to_12),
    (13, _migrate_to_13),
    (14, _migrate_to_14),
    (16, _migrate_to_16),
    (17, _migrate_to_17),
    (21, _migrate_to_21),
    (23, _migrate_to_23),
    # 24 → 25: model_catalog TTL 24h → 1h (only the OLD default 24).
    (25, _rewrite_stale_default(
        section="model_catalog", key="ttl_hours", old=24, new=1,
        added="model_catalog.ttl_hours 24→1",
        message="  ✓ Lowered model_catalog.ttl_hours to 1 (hourly picker refresh)")),
    (29, _migrate_to_29),
    # 30 → 31: verify_on_stop OFF (one-time). The "auto" sentinel was more noise than signal.
    # Rewrite only when missing or still "auto" — an explicit user true/false is preserved.
    (31, functools.partial(
        _rewrite_key, section="agent", key="verify_on_stop", new=False, create_section=True,
        match=lambda cur: cur is None or _lower_is("auto")(cur),
        added="agent.verify_on_stop=false",
        message=(
            "  ✓ Turned off verify-on-stop (agent.verify_on_stop: false). "
            "Set it to true to re-enable, or \"auto\" for the legacy "
            "surface-aware behavior."))),
    # 31 → 32: flip the BAKED-IN literal true to OFF (one-time). v30 defaulted verify_on_stop to a
    # literal True and migrate_config persisted defaults, so installs that updated through v30 have
    # `verify_on_stop: true` written literally — never a user choice (no off-switch existed until
    # v31). A true set AFTER v32 is never touched.
    (32, _rewrite_stale_default(
        section="agent", key="verify_on_stop", old=True, new=False,
        added="agent.verify_on_stop=false",
        message=(
            "  ✓ Turned off verify-on-stop (agent.verify_on_stop: false) — "
            "the old default was written into your config as a literal "
            "true. Set it to true again to re-enable, or \"auto\" for the "
            "legacy surface-aware behavior."),
        extra_guard=lambda raw: raw.get("verify_on_stop") is True)),
    (33, _migrate_to_33),
    (34, _migrate_to_34),
    # 34 → 35: background_process_notifications 'all' (old implicit default, rarely chosen on
    # purpose) → 'concise'. Explicit result/error/off choices are preserved.
    (35, functools.partial(
        _rewrite_key, section="display", key="background_process_notifications",
        match=_lower_is("all"), new="concise",
        added="display.background_process_notifications=concise (was: all)",
        message=(
            "  ✓ Background process notifications switched from 'all' to "
            "'concise' — completions now show a one-line status message "
            "instead of the raw output dump. Set "
            "display.background_process_notifications: all to restore "
            "the old behavior."))),
    # 35 → 36: subagent iteration cap 50 → 250 (50 truncated substantial delegated work).
    (36, _rewrite_stale_default(
        section="delegation", key="max_iterations", old=50, new=250,
        added="delegation.max_iterations=250 (was: 50)",
        message=(
            "  ✓ Raised delegation.max_iterations from 50 to 250 — subagents "
            "now get a larger per-child tool-call budget so delegated work "
            "finishes instead of truncating. Set delegation.max_iterations "
            "back to 50 to restore the old cap."))),
    # 36 → 37: delegation concurrency 3 → 10 (stays at/below the high-cost warning threshold).
    (37, _rewrite_stale_default(
        section="delegation", key="max_concurrent_children", old=3, new=10,
        added="delegation.max_concurrent_children=10 (was: 3)",
        message=(
            "  ✓ Raised delegation.max_concurrent_children from 3 to 10 — "
            "independent delegated children now fan out wider in parallel. "
            "Each child consumes API tokens independently; set "
            "delegation.max_concurrent_children back to 3 to restore the old cap."))),
    (38, _migrate_to_38),
    (39, _migrate_to_39),
    (40, _migrate_to_40),
    (41, _migrate_to_41),
    # Companion v41 step adopted from upstream (same slot; the fork's ox-alpha
    # retirement stays the module-level _migrate_to_41 the tests pin): drop the
    # plugin-era "## Messaging other agents" append from every SOUL.md — the
    # live Bot Mode roster replaced the frozen copy.
    (41, _migrate_to_41_soul_cleanup),
    (42, _migrate_to_42),
    # Companion v42 step adopted from upstream (same slot): cron.model_drift_guard
    # is gone from the codebase — unpinned jobs run on their creation snapshot
    # instead of failing closed when the global model changes.
    (42, functools.partial(
        _rewrite_key, section="cron", key="model_drift_guard", new=None,
        match=lambda cur: cur is not None,
        added="removed cron.model_drift_guard",
        message=(
            "  ✓ Removed cron.model_drift_guard — unpinned cron jobs now keep running on the "
            "model/provider they were created under when the global default changes, instead "
            "of being skipped. Pin a job or set cron.model to move it."))),
)


def run_migrations(current_ver: int, results: Dict[str, Any], quiet: bool) -> None:
    """Apply every registered migration whose target version exceeds *current_ver*.

    *current_ver* is the on-disk schema version captured ONCE before any step runs and does not
    advance between steps — each step is gated on the same initial value.
    """
    for target_ver, migration_fn in MIGRATIONS:
        if current_ver < target_ver:
            migration_fn(results, quiet)
