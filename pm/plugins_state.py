"""Which plugins are enabled, per profile — pm's read of the plugins
config (order-preserving for the incumbent-wins tiebreak).

pm needs two things the plugins_cmd helpers don't give: EVERY profile's
enabled list (the union is per-install, cross-profile) and the list
ORDER (config order = enable recency; enabling appends). Writes go
through the same config.yaml the plugins CLI owns — pm never invents a
second authority for enabled state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def _profiles_root() -> Path:
    # Plugin discovery and dependency publication must use the same home root.
    from hermes_cli.runtime_paths import dependency_home_root

    return dependency_home_root() / "profiles"


def _read_home_config(home: Path) -> Optional[dict[str, Any]]:
    """Parse ONE home's config.yaml, or None when absent/unparseable.

    The single parse site: every query about a home (plugins.enabled,
    memory.provider) derives from this one read — config.yaml is parsed
    once per home, not once per question.
    """
    try:
        import utils

        config_path = home / "config.yaml"
        if not config_path.is_file():
            return None
        config = utils.fast_safe_load(config_path.read_text(encoding="utf-8-sig"))
        return config if isinstance(config, dict) else None
    except Exception:
        return None


def _enabled_from_config(config: dict[str, Any]) -> list[str]:
    """plugins.enabled from an already-parsed config, ORDER-PRESERVING."""
    plugins_cfg = config.get("plugins")
    if not isinstance(plugins_cfg, dict):
        return []
    enabled = plugins_cfg.get("enabled")
    if not isinstance(enabled, list):
        return []
    disabled = plugins_cfg.get("disabled", [])
    disabled = set(disabled) if isinstance(disabled, list) else set()
    out: list[str] = []
    for name in enabled:
        if (isinstance(name, str) and name and name not in out
                and name not in disabled and name.rsplit("/", 1)[-1] not in disabled):
            out.append(name)
    return out


def _all_homes() -> list[Path]:
    """The default home + every profile home (the union's scope)."""
    homes: list[Path] = []
    try:
        from hermes_cli.runtime_paths import dependency_home_root

        homes.append(dependency_home_root())
    except Exception:
        pass
    try:
        root = _profiles_root()
        if root.is_dir():
            for profile in sorted(root.iterdir(), key=str):
                if profile.is_dir():
                    homes.append(profile)
    except OSError:
        pass
    return homes


def enabled_plugins_ordered(*, proposed_home=None, enabled=None, disabled=None) -> dict[Path, list[str]]:
    """plugins_dir → ordered enabled list, per home. Keyed by the
    PLUGINS DIR (where the member dirs live), not the home itself.

    The ACTIVE MEMORY PROVIDER joins its home's list: providers install
    via ``memory.provider`` (mnemosyne's documented path), not via
    plugins.enabled — without this, a provider's dep plugin never joins
    the union. The provider rides LAST (newest — the bisect's
    incumbent-wins tiebreak disables it before older plugins)."""
    out: dict[Path, list[str]] = {}
    for home in _all_homes():
        # ONE parse per home feeds both queries (enabled + provider).
        config = _read_home_config(home)
        config = config or {}
        if proposed_home is not None and home.resolve() == Path(proposed_home).resolve():
            config = {**config, "plugins": {"enabled": list(enabled or ()), "disabled": list(disabled or ())}}
        names = _enabled_from_config(config)
        provider = _provider_from_config(home, config)
        if provider and provider not in names:
            names.append(provider)
        if names:
            out[home / "plugins"] = names
    return out


def _provider_from_config(home: Path, config: dict[str, Any]) -> Optional[str]:
    """The ``memory.provider`` key of an already-parsed config, when its
    plugin dir exists (no dir = not a member)."""
    try:
        provider = (config.get("memory") or {}).get("provider")
        if not isinstance(provider, str) or not provider.strip():
            return None
        name = provider.strip()
        if (home / "plugins" / name).is_dir():
            return name
        return None
    except Exception:
        return None


def disable_plugins(names: list[str]) -> dict[str, list[str]]:
    """Remove names from EVERY home's enabled list (a bisect decision
    names the plugin, not the profile — disable where it's enabled).
    Returns per-home what was removed.

    Writes go through utils.atomic_roundtrip_yaml_update — the same
    atomic, comment-preserving round-trip writer the plugins CLI's
    config path uses — pointed at that home's config.yaml (explicit
    home scope; pm never derives the target from ambient state). A
    write failure RAISES: a disable that didn't land must never be
    reported as removed. An EXISTING home config that can't be parsed
    also raises — silently skipping it would report success while the
    plugin stays enabled in that home.
    """
    removed: dict[str, list[str]] = {}
    if not names:
        return removed
    name_set = set(names)

    for home in _all_homes():
        config_path = home / "config.yaml"
        if not config_path.is_file():
            continue
        config = _read_home_config(config_path.parent)
        if config is None:
            raise ValueError(
                f"could not parse existing config: {config_path}"
            )
        plugins_cfg = config.get("plugins")
        if not isinstance(plugins_cfg, dict):
            continue
        enabled = plugins_cfg.get("enabled")
        if not isinstance(enabled, list):
            continue
        hit = [n for n in enabled if isinstance(n, str) and n in name_set]
        if not hit:
            continue
        kept = [n for n in enabled if not (isinstance(n, str) and n in name_set)]
        import utils

        utils.atomic_roundtrip_yaml_update(config_path, "plugins.enabled", kept)
        removed[str(home)] = hit
    return removed
