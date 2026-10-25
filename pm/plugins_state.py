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


def _profiles_root() -> Path:
    # Profile operations are HOME-anchored by design (AGENTS.md rule 6:
    # _get_profiles_root returns Path.home()/.hermes/profiles so every
    # profile is visible regardless of which is active).
    return Path.home() / ".hermes" / "profiles"


def _enabled_list_for_home(home: Path) -> list[str]:
    """plugins.enabled for ONE hermes home, ORDER-PRESERVING."""
    try:
        import yaml

        config_path = home / "config.yaml"
        if not config_path.is_file():
            return []
        with config_path.open(encoding="utf-8-sig") as f:
            config = yaml.safe_load(f) or {}
        plugins_cfg = config.get("plugins") or {}
        if not isinstance(plugins_cfg, dict):
            return []
        enabled = plugins_cfg.get("enabled")
        if not isinstance(enabled, list):
            return []
        out: list[str] = []
        for name in enabled:
            if isinstance(name, str) and name and name not in out:
                out.append(name)
        return out
    except Exception:
        return []


def _all_homes() -> list[Path]:
    """The default home + every profile home (the union's scope)."""
    homes: list[Path] = []
    try:
        from hermes_constants import get_default_hermes_root

        homes.append(get_default_hermes_root())
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


def enabled_plugins_ordered() -> dict[Path, list[str]]:
    """plugins_dir → ordered enabled list, per home. Keyed by the
    PLUGINS DIR (where the member dirs live), not the home itself."""
    out: dict[Path, list[str]] = {}
    for home in _all_homes():
        enabled = _enabled_list_for_home(home)
        if enabled:
            out[home / "plugins"] = enabled
    return out


def disable_plugins(names: list[str]) -> dict[str, list[str]]:
    """Remove names from EVERY home's enabled list (a bisect decision
    names the plugin, not the profile — disable where it's enabled).
    Returns per-home what was removed. Writes via yaml round-trip of
    the same config.yaml the plugins CLI owns."""
    removed: dict[str, list[str]] = {}
    if not names:
        return removed
    name_set = set(names)
    import yaml

    for home in _all_homes():
        config_path = home / "config.yaml"
        if not config_path.is_file():
            continue
        try:
            with config_path.open(encoding="utf-8-sig") as f:
                config = yaml.safe_load(f) or {}
            plugins_cfg = config.get("plugins")
            if not isinstance(plugins_cfg, dict):
                continue
            enabled = plugins_cfg.get("enabled")
            if not isinstance(enabled, list):
                continue
            kept = [n for n in enabled if not (isinstance(n, str) and n in name_set)]
            hit = [n for n in enabled if isinstance(n, str) and n in name_set]
            if not hit:
                continue
            plugins_cfg["enabled"] = kept
            with config_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, default_flow_style=False)
            removed[str(home)] = hit
        except Exception:
            continue
    return removed
